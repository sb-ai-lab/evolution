"""Every ``evolution.*`` module must import without reaching a foreign runtime.

This is the dynamic half of the core import fence. It imports every product
module, then checks loaded package roots. A static fence cannot see every
dynamic import or entry-point string.

Two failure modes are guarded explicitly, because both would let this pass while
testing nothing:

* **a vacuous sweep.** If discovery breaks and finds no modules, importing all of
  them trivially succeeds. The module count is asserted against a floor;
* **a rotting exclusion list.** Executable wrappers that ``sys.exit`` at module
  scope and the distribution-excluded GUI subtree each have a narrowly tested reason not to be
  imported. The wrapper modules must still exist, and the GUI exception must
  match the package exclusions, so neither exception can quietly widen.

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
import tomllib
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

#: Modules outside the distribution are not part of the installed import surface.
DISTRIBUTION_EXCLUDED = ("evolution.gui",)

#: A floor, not a target. The tree held 107 modules when this landed; the check
#: exists so a discovery bug cannot make an empty sweep look green.
MINIMUM_MODULES = 80

_PROBE = """
import importlib, json, sys
from pathlib import Path
sys.path.insert(0, {src!r})
import evolution

excluded = set({excluded!r})
distribution_excluded = tuple({distribution_excluded!r})
package = Path({src!r}) / "evolution"
names = []
for source in package.rglob("*.py"):
    parts = source.relative_to(package).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts:
        names.append("evolution." + ".".join(parts))
names.sort()
failures = {{}}
missing = {{}}
source_only = []
for name in names:
    if name in excluded:
        continue
    if any(name == root or name.startswith(root + ".") for root in distribution_excluded):
        source_only.append(name)
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
private_imports = sorted(
    name
    for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in distribution_excluded)
)
print("<<<" + json.dumps({{
    "discovered": names,
    "failures": failures,
    "missing_dependency": missing,
    "distribution_excluded": source_only,
    "excluded_source_imports": private_imports,
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
                distribution_excluded=list(DISTRIBUTION_EXCLUDED),
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
    """Every swept product module imports, or names a dependency an extra declares.

    The sweep runs wherever the suite runs, including CI, which installs the
    core and nothing else. Distribution-excluded modules are not swept because no
    installed product can contain them; all remaining failures are defects, and
    the extras claim is checked below rather than assumed.
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


def test_excluded_modules_are_not_imported_by_the_source_sweep(sweep: dict[str, Any]) -> None:
    """Excluded source modules are outside the installed import surface."""
    gui_modules = {
        module
        for module in sweep["discovered"]
        if module == "evolution.gui" or module.startswith("evolution.gui.")
    }

    assert DISTRIBUTION_EXCLUDED == ("evolution.gui",)
    assert set(sweep["distribution_excluded"]) == gui_modules
    assert sweep["excluded_source_imports"] == []

    if (SRC / "evolution" / "gui").is_dir():
        config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        assert (
            "src/evolution/gui" in config["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
        )
        assert (
            "/src/evolution/gui" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        )


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
