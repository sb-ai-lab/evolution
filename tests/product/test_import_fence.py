"""No ``evolution`` source may name a retired external runtime package.

This static fence complements the import sweep. It checks imports, dynamic
import calls, and module-path strings. The negative controls prove that the
scanner still detects each supported form.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "evolution"

FORBIDDEN_ROOTS = frozenset({"agentsamo", "skill_evolution"})

#: A floor, so a discovery bug cannot make an empty scan look clean.
MINIMUM_FILES = 80


def _root_of(dotted: str) -> str:
    """The top-level package of a dotted path, or of a ``module:Class`` spec."""
    return dotted.split(":", 1)[0].split(".", 1)[0]


#: Callables whose string argument the interpreter turns into an import. A bare
#: package name is only read as an import here, because a bare name anywhere
#: else is ambiguous -- and in this repository, provably so: ``skill_evolution``
#: is also the name of a manifest scope (beside ``dev`` and ``reporting``) and
#: appears as a plain string in nine product modules that import nothing.
IMPORT_SINKS = frozenset(
    {
        "import_module",
        "__import__",
        "spec_from_file_location",
        "find_spec",
        "module_from_spec",
        "entry_points",
        "load_entry_point",
    }
)


def _roots_in_text(text: str, *, at_import_sink: bool) -> set[str]:
    """The package roots a string could name, or nothing if it is prose.

    A module specification never contains whitespace. Testing that first is what
    keeps an ordinary diagnostic -- ``"skill_evolution.runtime: unavailable"`` is
    a realistic thing to write in an error message -- from being read as an
    import and failing the fence for saying a true sentence.

    A file path is considered as well as a dotted path, because
    ``spec_from_file_location`` takes one and the module name beside it can be
    anything the author likes.
    """
    if not text or any(character.isspace() for character in text):
        return set()
    stripped = text.strip()
    if stripped.startswith("."):
        return set()
    candidates = set()
    if "." in stripped or ":" in stripped:
        candidates.add(_root_of(stripped))
    if "/" in stripped:
        candidates.add(_root_of(stripped.split("/", 1)[0]))
    if at_import_sink and stripped.isidentifier():
        # A whole-package import names no submodule, so it arrives as a bare
        # identifier and the shape rules above see nothing.
        candidates.add(stripped)
    return {candidate for candidate in candidates if candidate.isidentifier()}


def _sink_name(node: ast.Call) -> str | None:
    """The called name, whether it is called bare or through its module."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _fold(node: ast.AST, bindings: dict[str, str]) -> str | None:
    """The string this expression evaluates to, when that is knowable statically.

    Without this the fence reads ``ast.Constant`` and nothing else, so
    ``family = "agent" + "_harness"`` followed by ``import_module(f"{family}.core")``
    walks straight through it -- which an auditor demonstrated against the real
    scanner. Neither half of that is exotic; both are how a person writes a
    dynamic import they want to keep configurable.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, bindings)
        right = _fold(node.right, bindings)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            folded = _fold(piece, bindings)
            if folded is None:
                return None
            parts.append(folded)
        return "".join(parts)
    if isinstance(node, ast.FormattedValue):
        return _fold(node.value, bindings)
    return None


def _literal_bindings(tree: ast.AST) -> dict[str, str]:
    """Names bound to a statically knowable string, anywhere in the module.

    Scope is deliberately ignored. A fence may over-approximate -- the cost of
    reading one module's ``NAME`` as another's is a false positive an author can
    see and rename -- but it may not under-approximate, because the thing it
    under-approximates is the rule the whole merge rests on.
    """
    bindings: dict[str, str] = {}
    for _ in range(3):  # a binding may be built from an earlier binding
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            folded = _fold(node.value, bindings)
            if folded is not None:
                bindings[target.id] = folded
    return bindings


def imported_roots(tree: ast.AST) -> set[str]:
    """Every package root the module names, by import statement or by string.

    Relative imports are skipped: ``node.module`` is ``None`` for ``from . import
    x``, and a relative import cannot reach another distribution anyway.
    """
    bindings = _literal_bindings(tree)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(_root_of(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(_root_of(node.module))
        elif isinstance(node, ast.Constant | ast.BinOp | ast.JoinedStr):
            # A module path travelling as data: an entry point, a runtime_spec,
            # or the argument to a dynamic import, assembled or not.
            folded = _fold(node, bindings)
            if folded is not None:
                roots |= _roots_in_text(folded, at_import_sink=False)
        elif isinstance(node, ast.Call) and _sink_name(node) in IMPORT_SINKS:
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                folded = _fold(argument, bindings)
                if folded is not None:
                    roots |= _roots_in_text(folded, at_import_sink=True)
    return roots


def source_files() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.fixture(scope="module")
def offenders() -> dict[str, set[str]]:
    """Every product file that names a forbidden root, and which roots."""
    found: dict[str, set[str]] = {}
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = imported_roots(tree) & FORBIDDEN_ROOTS
        if hits:
            found[str(path.relative_to(SRC))] = hits
    return found


def test_no_product_module_names_a_foreign_runtime(offenders: dict[str, set[str]]) -> None:
    assert offenders == {}


def test_the_scan_actually_read_the_tree() -> None:
    """A fence over zero files passes every assertion above it."""
    assert len(source_files()) >= MINIMUM_FILES


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("import skill_evolution", id="import"),
        pytest.param("from skill_evolution.tree import validate", id="from-import"),
        pytest.param("import skill_evolution.core as core", id="import-as"),
        pytest.param("importlib.import_module('skill_evolution.core')", id="dynamic-import"),
        pytest.param("SPEC = 'skill_evolution.runtimes.agentsamo:Runtime'", id="runtime-spec"),
        pytest.param("ENTRY = 'skill_evolution.cli:main'", id="entry-point-string"),
        # The four below were demonstrated against the earlier scanner, which
        # read ``ast.Constant`` and nothing else. Each is a way somebody keeps a
        # dynamic import configurable, not a way somebody hides one.
        pytest.param(
            "NAME = 'skill' + '_evolution'\nimportlib.import_module(NAME)",
            id="concatenated-name",
        ),
        pytest.param(
            "FAMILY = 'skill_evolution'\nimportlib.import_module(f'{FAMILY}.runtime')",
            id="f-string-from-binding",
        ),
        pytest.param("__import__('agentsamo' + '.core')", id="dunder-import-concatenated"),
        pytest.param(
            "spec_from_file_location('runtime', 'skill_evolution/runtime.py')",
            id="path-with-innocent-alias",
        ),
    ],
)
def test_the_fence_catches_each_way_a_module_path_can_travel(snippet: str) -> None:
    """Without this, "no offenders" is indistinguishable from "scanner broken"."""
    hits = imported_roots(ast.parse(snippet)) & FORBIDDEN_ROOTS

    assert hits, f"the fence did not catch: {snippet}"


def test_the_fence_does_not_fire_on_ordinary_prose() -> None:
    """A docstring mentioning the merge must not read as an import."""
    benign = ast.parse('"""The skill_evolution package is documented elsewhere."""\nX = 1\n')

    assert imported_roots(benign) & FORBIDDEN_ROOTS == set()


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(
            'raise RuntimeError("skill_evolution.runtime: Runtime unavailable")',
            id="diagnostic-naming-the-module",
        ),
        pytest.param(
            'LOG = "skill_evolution.core: not installed, falling back"',
            id="log-line-naming-the-module",
        ),
        pytest.param('HELP = "see skill_evolution.cli for the old command"', id="help-text"),
    ],
)
def test_the_fence_does_not_fire_on_a_message_that_names_a_module(snippet: str) -> None:
    """Saying a module's name in an error is not importing it.

    The first version scanned every string holding a ``.`` or a ``:``, so the
    honest diagnostic a person writes when a foreign runtime is absent -- naming
    exactly which module is absent -- failed the fence. A gate that punishes the
    clearest possible error message gets worked around, not obeyed.
    """
    assert imported_roots(ast.parse(snippet)) & FORBIDDEN_ROOTS == set()


def test_the_manifest_scope_named_skill_evolution_is_not_read_as_an_import() -> None:
    """A measured name collision, locked so the fence does not relitigate it.

    ``skill_evolution`` is a forbidden package root and a legacy manifest scope.
    The scope is data, not an import. A fence that treats every bare identifier
    as an import would reject valid product code.
    """
    scope_use = ast.parse(
        'def run(scope: str = "skill_evolution") -> str:\n'
        '    return scope if scope != "skill_evolution" else "dev"\n'
    )

    assert imported_roots(scope_use) & FORBIDDEN_ROOTS == set()


def test_the_core_distribution_declares_no_foreign_runtime_import() -> None:
    """An entry point is an import the interpreter performs, written in TOML.

    Nothing in ``src/`` has to name a foreign runtime for the installed product
    to load it: ``[project.entry-points]`` in ``pyproject.toml`` is enough, and
    an AST scan over Python files cannot see a line of TOML.

    Only the keys that *cause an import* are read. A first version scanned the
    whole file as text and failed the moment the repository grew a vendored tree
    the linter had to exclude by path -- a build-tool setting that imports
    nothing. A fence that fires on tool configuration teaches people to
    special-case the fence.
    """
    manifest = SRC.parents[1] / "pyproject.toml"
    project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})

    declared: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    declared.extend(project.get("scripts", {}).values())
    for group in project.get("entry-points", {}).values():
        declared.extend(group.values())

    named = sorted(
        {
            root
            for value in declared
            for root in _roots_in_text(str(value), at_import_sink=True)
            if root in FORBIDDEN_ROOTS
        }
    )

    assert named == [], (
        f"the core distribution declares {named}. A dependency, script or entry "
        f"point naming a foreign runtime makes the core wheel depend on it, which "
        f"no test over src/ can see."
    )
