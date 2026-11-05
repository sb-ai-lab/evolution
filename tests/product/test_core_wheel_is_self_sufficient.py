"""The core wheel must stand alone, and every optional import must be declared.

``test_core_only_imports.py`` sweeps the *source tree*, where every optional
dependency happens to be installed. That proves the import fence holds; it cannot
prove the wheel is installable on its own, because nothing there is absent.

So this builds the wheel, installs it into an empty virtual environment with no
extras, and imports every module in there. Two distinct failures become visible
only at this altitude:

* **a dependency used but never declared.** A module importing ``pandas`` while
  no extra requires it works in every developer checkout and fails for the first
  user who installs the wheel;
* **a dependency declared optional but imported unconditionally.** The package
  then cannot be installed lean at all, and ``evo --help`` fails on a clean
  machine.

The assertion is deliberately not "every module imports". Some must not: the GUI
needs Streamlit, and requiring it in the core wheel is the thing the two-wheel
split exists to avoid. The invariant is narrower and truer -- **a module may fail
to import only for a dependency that some declared extra provides** -- which
distinguishes an optional feature from a packaging defect.

Marked ``slow``: it builds a wheel and creates a virtual environment. It skips
rather than fails when the environment cannot install packages at all, because a
missing index is a statement about the machine and not about this repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

FOREIGN_ROOTS = ("agentsamo", "skill_evolution", "langchain", "gigachat")

#: Executable wrappers that ``sys.exit`` at module scope; see the source sweep.
EXECUTABLE_WRAPPERS = (
    "evolution.eval._guard_scripts.command_wrapper",
    "evolution.eval._guard_scripts.destructive_wrapper",
)

MINIMUM_MODULES = 80

_SWEEP = """
import importlib, json, pkgutil, sys
import evolution

skip = set({skip!r})
names = sorted(m.name for m in pkgutil.walk_packages(evolution.__path__, prefix="evolution."))
missing, failures = {{}}, {{}}
for name in names:
    if name in skip:
        continue
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing[name] = exc.name
    except BaseException as exc:
        failures[name] = type(exc).__name__ + ": " + str(exc)[:160]

foreign = sorted({{m.split(".")[0] for m in sys.modules if m.split(".")[0] in {foreign!r}}})
print("<<<" + json.dumps({{
    "discovered": names,
    "missing_dependency": missing,
    "other_failures": failures,
    "foreign_roots_loaded": foreign,
}}) + ">>>")
"""


def declared_extra_packages() -> set[str]:
    """Distribution names any optional extra provides, normalised for comparison."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"].get("optional-dependencies") or {}
    names: set[str] = set()
    for requirements in extras.values():
        for requirement in requirements:
            name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0]
            names.add(name.replace("-", "_").lower())
    return names


def installed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # The installed-wheel checks must not import the source checkout.
    environment.pop("PYTHONPATH", None)
    return environment


@pytest.fixture(scope="module")
def core_environment(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Build the wheel, install it alone, and sweep the result."""
    workspace = tmp_path_factory.mktemp("core-wheel")
    dist = workspace / "dist"

    # Build in isolation, which is how the release workflow builds. Reusing this
    # environment's packages instead required the backend to be installed beside
    # the test; CI installs `evo-skills` and the test tools only, so the build
    # failed there for a reason that had nothing to do with the wheel.
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-w", str(dist)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if build.returncode != 0:
        combined = build.stdout + build.stderr
        # Fetching the build backend needs an index. That is a property of the
        # machine; the wheel's own defects are what this gate exists to find.
        if any(
            marker in combined
            for marker in (
                "Temporary failure in name resolution",
                "Network is unreachable",
                "Connection refused",
                "Read timed out",
                "Failed to establish a new connection",
                "retries exceeded",
            )
        ):
            pytest.skip(f"no package index reachable to fetch the build backend: {combined[-400:]}")
        pytest.fail(
            f"wheel build failed:\n{build.stdout[-2000:]}\n{build.stderr[-2000:]}", pytrace=False
        )
    wheels = sorted(dist.glob("evo_skills-*.whl"))
    assert wheels, f"no evo_skills wheel in {dist}: {sorted(p.name for p in dist.iterdir())}"

    venv = workspace / "venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=600
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create a virtual environment here: {created.stderr[-400:]}")

    python = venv / "bin" / "python"
    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheels[0])],
        env=installed_environment(),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if install.returncode != 0:
        # An unreachable index is a property of the machine; unsatisfiable or
        # malformed metadata is a property of the wheel, and is exactly the
        # defect this gate exists to find. Skipping both made the gate report
        # "not applicable here" for its own subject failing.
        stderr = install.stderr
        offline = any(
            marker in stderr
            for marker in (
                "Temporary failure in name resolution",
                "Network is unreachable",
                "Connection refused",
                "Read timed out",
                "Failed to establish a new connection",
                "retries exceeded",
                "proxy",
            )
        )
        if offline:
            pytest.skip(f"no package index reachable from this machine: {stderr[-400:]}")
        pytest.fail(
            f"the core wheel does not install into an empty environment. This is a "
            f"packaging defect in this repository, not a property of this machine:\n"
            f"{stderr[-1200:]}",
            pytrace=False,
        )

    sweep = subprocess.run(
        [
            str(python),
            "-c",
            _SWEEP.format(skip=list(EXECUTABLE_WRAPPERS), foreign=list(FOREIGN_ROOTS)),
        ],
        env=installed_environment(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert "<<<" in sweep.stdout, f"sweep produced no report:\n{sweep.stderr[-2000:]}"
    report = json.loads(sweep.stdout.split("<<<", 1)[1].rsplit(">>>", 1)[0])
    report["python"] = str(python)
    return report


pytestmark = pytest.mark.slow


def test_the_installed_wheel_exposes_the_whole_package(core_environment: dict[str, Any]) -> None:
    assert len(core_environment["discovered"]) >= MINIMUM_MODULES


def test_no_module_fails_for_any_reason_but_a_missing_dependency(
    core_environment: dict[str, Any],
) -> None:
    """A syntax error or a bad relative import would surface only here."""
    assert core_environment["other_failures"] == {}


def test_every_optional_import_is_backed_by_a_declared_extra(
    core_environment: dict[str, Any],
) -> None:
    """The packaging defect this file exists for: a dependency used but not declared."""
    provided = declared_extra_packages()
    undeclared = {
        module: dependency
        for module, dependency in core_environment["missing_dependency"].items()
        if dependency.split(".")[0].replace("-", "_").lower() not in provided
    }

    assert undeclared == {}, (
        f"these modules need a dependency no extra provides: {undeclared}. "
        f"Declared across all extras: {sorted(provided)}"
    )


def test_the_core_wheel_reaches_no_foreign_runtime(core_environment: dict[str, Any]) -> None:
    assert core_environment["foreign_roots_loaded"] == []


def test_the_console_script_runs_without_any_extra(core_environment: dict[str, Any]) -> None:
    """``evo --help`` on a clean machine is the first thing a new user does."""
    evo = Path(core_environment["python"]).with_name("evo")
    result = subprocess.run(
        [str(evo), "--help"],
        env=installed_environment(),
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "Usage: evo" in result.stdout
