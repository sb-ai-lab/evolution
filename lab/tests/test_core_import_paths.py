"""Reject removed Core imports, including runtime targets and embedded programs.

The inventory is generated from Core's five b15 CHANGELOG migration tables.
Historical panel captures and their unimported producer scripts retain provenance.
"""
import ast
from functools import lru_cache
import importlib
from pathlib import Path
import re
import pytest

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
CORE = "evolution.tree"
DOTTED = re.compile(r"\bevolution\.tree(?:\.[A-Za-z_]\w*)+")
MOVED = {
    tuple(line.split(":"))
    for line in (ROOT / "tests/data/core-b15-moved-paths.txt").read_text().splitlines()
    if line and not line.startswith("#")
}
# These facade modules still exist; only the table-listed members moved.
RETAINED_MODULES = {
    CORE + ".bindings", CORE + ".campaign_identity",
    CORE + ".campaign_editor", CORE + ".campaign_reflector",
}
OLD_MODULES = {module for module, _ in MOVED} - RETAINED_MODULES
IMPORTED_PANEL_HELPERS = {"capture.py", "fake_chat.py", "capture_b15.py"}


def _python_files():
    for directory in ("src", "tests", "examples"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            # capture/fake_chat are imported; capture_b15 is the current CLI producer.
            # Other producer scripts retain their historical wheel's paths.
            if (path.is_relative_to(ROOT / "tests/fixtures/panel")
                    and path.name not in IMPORTED_PANEL_HELPERS):
                continue
            yield path


def _tree_references(tree, historical_literals=frozenset()):
    """Yield module/name imports and dotted string references, with source lines."""
    for node in ast.walk(tree):
        if node in historical_literals:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module == CORE or node.module.startswith(CORE + "."):
                for alias in node.names:
                    yield node.lineno, node.module, alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == CORE or alias.name.startswith(CORE + "."):
                    yield node.lineno, alias.name, None
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in DOTTED.finditer(node.value):
                yield node.lineno, match.group(), None
            if CORE in node.value:
                # Installed-service fixtures also carry complete Python programs.
                # Incomplete template strings still get the dotted-path scan above.
                try:
                    embedded = ast.parse(node.value)
                except SyntaxError:
                    continue
                for line, module, name in _tree_references(embedded):
                    yield node.lineno + line - 1, module, name


def _references():
    for path in _python_files():
        tree = ast.parse(path.read_text())
        historical_literals = set()
        if path == ROOT / "tests/test_owner_core_raw_contract.py":
            # b13 capture metadata records its producing Core's function names.
            # Only this literal data assignment is exempt; live imports and string
            # targets anywhere else in the same test file must still resolve.
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "B13_EDITOR6_CORE_FUNCTIONS"):
                    assert isinstance(node.value, ast.List)
                    assert all(isinstance(value, ast.Constant) and isinstance(value.value, str)
                               for value in node.value.elts)
                    historical_literals.update(node.value.elts)
        for line, module, name in _tree_references(tree, historical_literals):
            yield path.relative_to(ROOT), line, module, name
    for path in sorted((ROOT / "src/evolution_lab/guides").rglob("*.md")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for match in DOTTED.finditer(line):
                yield path.relative_to(ROOT), number, match.group(), None


def _moved(module, name):
    if (module, name) in MOVED:
        return True
    dotted = module + ("." + name if name else "")
    if dotted in OLD_MODULES:
        return True
    # An old module can become a new package (artifacts): only an old member,
    # not every new submodule under that package, denotes the removed API.
    return any(dotted == old + "." + member or dotted.startswith(old + "." + member + ".")
               for old, member in MOVED)


@lru_cache(maxsize=None)
def _resolve(dotted):
    """Import the longest module prefix, then resolve its attribute suffix."""
    parts = dotted.split(".")
    last_error = None
    for end in range(len(parts), 1, -1):
        try:
            value = importlib.import_module(".".join(parts[:end]))
        except ModuleNotFoundError as error:
            last_error = error
            continue
        for name in parts[end:]:
            value = getattr(value, name)
        return value
    raise last_error


def test_no_lab_reference_uses_a_moved_core_path():
    failures = [f"{path}:{line}: {module}" + (f":{name}" if name else "")
                for path, line, module, name in _references() if _moved(module, name)]
    assert not failures, "Removed Core paths:\n" + "\n".join(sorted(set(failures)))


def test_every_core_import_resolves_on_installed_core():
    failures = []
    for path, line, module, name in _references():
        dotted = module + ("." + name if name else "")
        try:
            _resolve(dotted)
        except (ImportError, AttributeError) as error:
            failures.append(f"{path}:{line}: {dotted}: {error}")
    assert not failures, "Unresolvable Core references:\n" + "\n".join(sorted(set(failures)))
