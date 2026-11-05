"""Every ``evolution.*`` module must import without reaching a foreign runtime.

This is the dynamic half of the core import fence. It imports every product
module, then checks loaded package roots. A static fence cannot see every
dynamic import or entry-point string.

Two failure modes are guarded explicitly, because both would let this pass while
testing nothing:

* **a vacuous sweep.** If discovery breaks and finds no modules, importing all of
  them trivially succeeds. The module count is asserted against a floor;
* **a rotting exclusion list.** The only excluded modules are executable wrappers
  that ``sys.exit`` at module scope. Each is asserted to still exist, so an
  exclusion that outlives its module fails instead of quietly widening.

What this does **not** yet cover, stated so the gate is not over-read: the plan
also wants the sweep run inside a venv with only the core wheel installed, which
is what proves an optional dependency is really optional. That needs the two-wheel
build from M2. Here the sweep runs against the source tree, where extras are
present, so it proves reachability and fence integrity -- not minimal dependencies.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.product.test_core_wheel_is_self_sufficient import declared_extra_packages

SRC = Path(__file__).resolve().parents[2] / "src"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: Roots that must never appear once every product module has been imported.
FOREIGN_ROOTS = ("agentsamo", "skill_evolution", "langchain", "gigachat")

#: Executable wrappers, not importable modules. Each is a shim installed onto a
#: sandboxed ``PATH`` under the command's own name; it resolves the real binary
#: and ``exec``s it, and exits 127 at module scope when there is none. Importing
#: one is a category error, not a defect.
EXECUTABLE_WRAPPERS = (
    "evolution.eval._guard_scripts.command_wrapper",
    "evolution.eval._guard_scripts.destructive_wrapper",
)

#: A floor, not a target. The tree held 107 modules when this landed; the check
#: exists so a discovery bug cannot make an empty sweep look green.
MINIMUM_MODULES = 80

_PROBE = """
import importlib, json, pkgutil, sys
sys.path.insert(0, {src!r})
import evolution

excluded = set({excluded!r})
names = sorted(m.name for m in pkgutil.walk_packages(evolution.__path__, prefix="evolution."))
failures = {{}}
missing = {{}}
for name in names:
    if name in excluded:
        continue
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        absent = (exc.name or "").split(".")[0]
        # A module the product itself should provide is a real defect. A
        # third-party one may be an optional extra, which the caller checks
        # against the declared extras rather than accepting blindly.
        if absent and absent != "evolution":
            missing[name] = absent
        else:
            failures[name] = type(exc).__name__ + ": " + str(exc)[:200]
    except BaseException as exc:
        failures[name] = type(exc).__name__ + ": " + str(exc)[:200]

foreign = sorted({{m.split(".")[0] for m in sys.modules if m.split(".")[0] in {foreign!r}}})
print("<<<" + json.dumps({{
    "discovered": names,
    "failures": failures,
    "missing_dependency": missing,
    "foreign_roots_loaded": foreign,
}}) + ">>>")
"""


@pytest.fixture(scope="module")
def sweep() -> dict[str, Any]:
    """Import every product module in a clean subprocess and report the outcome.

    Out of process for the same reason the import-purity lock is: importing the
    whole tree loads optional heavy dependencies and mutates the interpreter, and
    a gate that contaminates the suite it runs in is not a gate.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(
                src=str(SRC),
                excluded=list(EXECUTABLE_WRAPPERS),
                foreign=list(FOREIGN_ROOTS),
            ),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if "<<<" not in result.stdout:
        pytest.fail(
            f"sweep probe produced no report (rc={result.returncode})\n{result.stderr[-2000:]}"
        )
    payload = result.stdout.split("<<<", 1)[1].rsplit(">>>", 1)[0]
    return json.loads(payload)


def test_every_product_module_imports(sweep: dict[str, Any]) -> None:
    """Every module imports, or names a dependency an extra declares.

    The sweep runs wherever the suite runs, including CI, which installs the
    core and nothing else. The GUI views then fail on ``streamlit`` -- which is
    correct behaviour for an optional extra, not a defect, and demanding an
    empty failure map made this gate red on a machine that was doing everything
    right. Anything else still fails, and the extras claim is checked below
    rather than assumed.
    """
    assert sweep["failures"] == {}


def test_every_optional_import_is_backed_by_a_declared_extra(sweep: dict[str, Any]) -> None:
    """An import that no extra provides is unreachable for every installer.

    Without this, the tolerance above would launder a genuinely missing
    dependency into an "optional" one.
    """
    missing = sweep["missing_dependency"]
    undeclared = {
        module: dependency
        for module, dependency in missing.items()
        if dependency.replace("-", "_").lower() not in declared_extra_packages()
    }

    assert undeclared == {}


def test_no_foreign_runtime_is_reachable(sweep: dict[str, Any]) -> None:
    """The half an AST fence cannot see: a dynamic import or an entry-point string."""
    assert sweep["foreign_roots_loaded"] == []


def test_the_sweep_actually_found_the_tree(sweep: dict[str, Any]) -> None:
    """A discovery bug must fail here rather than pass everything else vacuously."""
    assert len(sweep["discovered"]) >= MINIMUM_MODULES


def test_every_excluded_module_still_exists(sweep: dict[str, Any]) -> None:
    """An exclusion that outlives its module is coverage quietly lost."""
    assert set(EXECUTABLE_WRAPPERS) <= set(sweep["discovered"])


def test_the_exclusion_list_is_exactly_the_two_it_was_written_for() -> None:
    """The allowlist may not grow without somebody saying so here.

    The test above only asks that each excluded module still exists, so the
    cheapest way to make this gate green after breaking the rule is to add the
    offending module to ``EXECUTABLE_WRAPPERS``: the exclusion is honest, the
    module exists, and the sweep stops looking at it. Naming the exact contents
    means widening the list is a visible edit to a test, not a quiet edit to a
    constant.
    """
    assert set(EXECUTABLE_WRAPPERS) == {
        "evolution.eval._guard_scripts.command_wrapper",
        "evolution.eval._guard_scripts.destructive_wrapper",
    }, (
        f"the sweep now skips {sorted(EXECUTABLE_WRAPPERS)}. Each exclusion is a "
        f"module the import rule is no longer enforced on: justify it here, in "
        f"this test, where a reviewer sees it."
    )
