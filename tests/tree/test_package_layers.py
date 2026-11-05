"""Enforce the package dependency rules from ADR-0045 as packages move."""

import ast
from collections import defaultdict
from dataclasses import dataclass
from graphlib import TopologicalSorter
from importlib.util import resolve_name
from pathlib import Path

import pytest

LAYERS: tuple[str, ...] = (
    "storage",
    "execution",
    "artifacts",
    "target",
    "roles",
    "manifest",
    "campaign",
    "evaluation",
    "observe",
)
TYPE_CHECKING_UPWARD: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    line: int
    kind: str


def upward_edges(src_root: Path) -> list[Edge]:
    """Return upward imports and edges participating in sibling-package cycles.

    Flat modules remain unplaced during the migration. The aggregate package
    itself is always the top layer; importing its exports from below is upward.
    """
    modules = {}
    packages = set()
    for path in sorted(src_root.rglob("*.py")):
        parts = path.relative_to(src_root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(("evolution", "tree", *parts))
        modules[module] = path
        if path.name == "__init__.py":
            packages.add(module)

    moved = {name for name in LAYERS if f"evolution.tree.{name}" in packages}

    def layer(module: str) -> int | None:
        if module == "evolution.tree":
            return len(LAYERS)
        parts = module.split(".")
        if len(parts) > 2 and parts[:2] == ["evolution", "tree"] and parts[2] in moved:
            return LAYERS.index(parts[2])
        return None

    imports = []
    for module, path in modules.items():
        package = module if module in packages else module.rpartition(".")[0]
        visitor = _Imports(module, package, set(modules))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        imports.extend(visitor.edges)
    imports = [
        edge
        for edge in imports
        if not (
            edge.kind == "type_checking" and TYPE_CHECKING_UPWARD.get((edge.source, edge.target))
        )
    ]
    violations = set()
    # Each sibling group has its own graph, including nested subpackages.
    graphs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    sibling_edges: list[tuple[str, str, str, Edge]] = []
    for edge in imports:
        source_layer, target_layer = layer(edge.source), layer(edge.target)
        if source_layer is None or target_layer is None:
            continue
        if source_layer < target_layer:
            violations.add(edge)
        elif source_layer == target_layer:
            source_parts, target_parts = edge.source.split("."), edge.target.split(".")
            for index in range(3, min(len(source_parts), len(target_parts))):
                if source_parts[index] == target_parts[index]:
                    continue
                parent = ".".join(source_parts[:index])
                source = ".".join(source_parts[: index + 1])
                target = ".".join(target_parts[: index + 1])
                if source in packages and target in packages:
                    graphs[parent][source].add(target)
                    sibling_edges.append((parent, source, target, edge))
                break

    for parent, source, target, edge in sibling_edges:
        pending, seen = [target], set()
        while pending:
            node = pending.pop()
            if node == source:
                violations.add(edge)
                break
            if node not in seen:
                seen.add(node)
                pending.extend(graphs[parent].get(node, ()))
    return sorted(violations, key=lambda edge: (edge.source, edge.line, edge.target, edge.kind))


class _Imports(ast.NodeVisitor):
    """Resolve static imports and literal dynamic imports without importing code."""

    def __init__(self, module: str, package: str, modules: set[str]) -> None:
        self.module = module
        self.package = package
        self.modules = modules
        self.aliases: dict[str, str] = {}
        # Methods close over module/function bindings, never class-body bindings.
        self.lexical_aliases = self.aliases
        self.edges: list[Edge] = []
        self.local = False
        self.type_checking = False

    def _name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return self._name(node.value) + "." + node.attr
        return ""

    def _add(self, target: str, node: ast.AST, *, dynamic: bool = False) -> None:
        if target != "evolution.tree" and not target.startswith("evolution.tree."):
            return
        kind = (
            "type_checking"
            if self.type_checking
            else "dynamic"
            if dynamic
            else "local"
            if self.local
            else "import"
        )
        self.edges.append(Edge(self.module, target, node.lineno, kind))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )
            self._add(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = resolve_name("." * node.level + (node.module or ""), self.package)
        for alias in node.names:
            candidate = f"{base}.{alias.name}"
            self.aliases[alias.asname or alias.name] = candidate
            self._add(candidate if candidate in self.modules else base, node)

    def visit_If(self, node: ast.If) -> None:
        previous = self.type_checking
        self.visit(node.test)
        self.type_checking = previous or self._name(node.test) == "typing.TYPE_CHECKING"
        for statement in node.body:
            self.visit(statement)
        self.type_checking = previous
        for statement in node.orelse:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.local, self.aliases, self.lexical_aliases
        self.aliases = self.lexical_aliases.copy()
        self.lexical_aliases = self.aliases
        self.local = True
        self.generic_visit(node)
        self.local, self.aliases, self.lexical_aliases = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        previous = self.aliases
        self.aliases = self.lexical_aliases.copy()
        self.generic_visit(node)
        self.aliases = previous

    def visit_Call(self, node: ast.Call) -> None:
        name = self._name(node.func)
        if name in {"importlib.import_module", "importlib.resources.files", "resources.files"}:
            arguments = {keyword.arg: keyword.value for keyword in node.keywords}
            target = node.args[0] if node.args else arguments.get("name")
            if name != "importlib.import_module" and not node.args:
                target = arguments.get("anchor", arguments.get("package"))
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                resolved = target.value
                if name == "importlib.import_module" and resolved.startswith("."):
                    package = node.args[1] if len(node.args) > 1 else arguments.get("package")
                    if isinstance(package, ast.Constant) and isinstance(package.value, str):
                        resolved = resolve_name(resolved, package.value)
                self._add(resolved, node, dynamic=True)
        self.generic_visit(node)


def storage_external_edges(src_root: Path) -> list[Edge]:
    """Return every tree import leaving storage, including unplaced modules."""
    modules = {}
    for path in sorted(src_root.rglob("*.py")):
        parts = path.relative_to(src_root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(("evolution", "tree", *parts))] = path

    edges = []
    for module, path in modules.items():
        if module != "evolution.tree.storage" and not module.startswith("evolution.tree.storage."):
            continue
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        visitor = _Imports(module, package, set(modules))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        edges.extend(
            edge
            for edge in visitor.edges
            if edge.target != "evolution.tree.storage"
            and not edge.target.startswith("evolution.tree.storage.")
        )
    return edges


def package_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "evolution" / "tree"
    root.mkdir(parents=True)
    (root / "__init__.py").touch()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in path.parents:
            if parent == root:
                break
            (parent / "__init__.py").touch()
        path.write_text(content, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "statement",
    [
        "import evolution.tree.execution.b",
        "import evolution.tree.execution.b as b",
        "from evolution.tree.execution import b",
        "from evolution.tree.execution.b import Value",
    ],
)
def test_moved_to_moved_upward_fails(tmp_path: Path, statement: str) -> None:
    root = package_tree(tmp_path, {"storage/a.py": statement, "execution/b.py": ""})
    assert upward_edges(root) == [
        Edge("evolution.tree.storage.a", "evolution.tree.execution.b", 1, "import")
    ]


def test_moved_to_unplaced_allowed(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {"storage/a.py": "from evolution.tree import journal_schema", "journal_schema.py": ""},
    )
    assert upward_edges(root) == []


def test_layer_name_without_package_is_unplaced(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {"storage/a.py": "import evolution.tree.execution", "execution.py": ""},
    )
    assert upward_edges(root) == []


def test_function_local_import_counts(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "storage/a.py": "async def load():\n    import evolution.tree.execution.b\n",
            "execution/b.py": "",
        },
    )
    assert upward_edges(root) == [
        Edge("evolution.tree.storage.a", "evolution.tree.execution.b", 2, "local")
    ]


@pytest.mark.parametrize(
    ("filename", "statement"),
    [
        ("storage/a.py", "from ..execution import b"),
        ("storage/a.py", "from ..execution.b import Value"),
        ("storage/__init__.py", "from ..execution import b"),
        ("storage/nested/a.py", "from ...execution import b"),
    ],
)
def test_relative_import_resolved(tmp_path: Path, filename: str, statement: str) -> None:
    root = package_tree(tmp_path, {filename: statement, "execution/b.py": ""})
    source = "evolution.tree." + filename.removesuffix(".py").replace("/", ".")
    source = source.removesuffix(".__init__")
    assert upward_edges(root) == [Edge(source, "evolution.tree.execution.b", 1, "import")]


@pytest.mark.parametrize(
    ("header", "guard"),
    [
        ("from typing import TYPE_CHECKING", "TYPE_CHECKING"),
        ("import typing", "typing.TYPE_CHECKING"),
        ("import typing as t", "t.TYPE_CHECKING"),
        ("from typing import TYPE_CHECKING as TC", "TC"),
    ],
)
def test_type_checking_needs_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str, guard: str
) -> None:
    root = package_tree(
        tmp_path,
        {
            "storage/a.py": f"{header}\nif {guard}:\n    from ..execution import b\n",
            "execution/b.py": "",
        },
    )
    source, target = "evolution.tree.storage.a", "evolution.tree.execution.b"
    assert upward_edges(root) == [Edge(source, target, 3, "type_checking")]
    monkeypatch.setitem(TYPE_CHECKING_UPWARD, (source, target), "Annotation only")
    assert upward_edges(root) == []


def test_type_checking_listing_does_not_allow_runtime_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = package_tree(
        tmp_path, {"storage/a.py": "from ..execution import b", "execution/b.py": ""}
    )
    source, target = "evolution.tree.storage.a", "evolution.tree.execution.b"
    monkeypatch.setitem(TYPE_CHECKING_UPWARD, (source, target), "Annotation only")
    assert upward_edges(root) == [Edge(source, target, 1, "import")]


def test_type_checking_else_is_runtime(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "storage/a.py": (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\n"
                "else:\n    from ..execution import b\n"
            ),
            "execution/b.py": "",
        },
    )
    assert upward_edges(root) == [
        Edge("evolution.tree.storage.a", "evolution.tree.execution.b", 5, "import")
    ]


@pytest.mark.parametrize(
    ("header", "call", "target"),
    [
        (
            "import importlib",
            'importlib.import_module("evolution.tree.execution.b")',
            "execution.b",
        ),
        ("import importlib as il", 'il.import_module("evolution.tree.execution.b")', "execution.b"),
        (
            "from importlib import import_module as load",
            'load("evolution.tree.execution.b")',
            "execution.b",
        ),
        (
            "from importlib import resources",
            'resources.files("evolution.tree.execution")',
            "execution",
        ),
        (
            "import importlib.resources as r",
            'r.files(anchor="evolution.tree.execution")',
            "execution",
        ),
        (
            "from importlib.resources import files",
            'files(package="evolution.tree.execution")',
            "execution",
        ),
        (
            "import importlib",
            'importlib.import_module(".execution.b", package="evolution.tree")',
            "execution.b",
        ),
    ],
)
def test_importlib_and_resources_strings(
    tmp_path: Path, header: str, call: str, target: str
) -> None:
    root = package_tree(tmp_path, {"storage/a.py": f"{header}\n{call}\n", "execution/b.py": ""})
    assert upward_edges(root) == [
        Edge("evolution.tree.storage.a", "evolution.tree." + target, 2, "dynamic")
    ]


@pytest.mark.parametrize(
    ("source", "line"),
    [
        (
            "import importlib\ndef other():\n    import json as importlib\n"
            "def load():\n    return importlib.import_module('evolution.tree.execution.b')\n",
            5,
        ),
        (
            "import importlib\nclass Other:\n    import json as importlib\n"
            "def load():\n    return importlib.import_module('evolution.tree.execution.b')\n",
            5,
        ),
        (
            "def load():\n    import importlib\n    def other():\n"
            "        import json as importlib\n"
            "    return importlib.import_module('evolution.tree.execution.b')\n",
            5,
        ),
        (
            "import importlib\nclass Loader:\n    import json as importlib\n"
            "    def load(self):\n"
            "        return importlib.import_module('evolution.tree.execution.b')\n",
            5,
        ),
    ],
)
def test_dynamic_import_aliases_respect_lexical_scope(
    tmp_path: Path, source: str, line: int
) -> None:
    root = package_tree(tmp_path, {"storage/a.py": source, "execution/b.py": ""})
    assert upward_edges(root) == [
        Edge("evolution.tree.storage.a", "evolution.tree.execution.b", line, "dynamic")
    ]


def test_subpackages_acyclic(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "roles/grouping/a.py": "from ..editor import b",
            "roles/editor/b.py": "from ..grouping import a",
        },
    )
    assert upward_edges(root) == [
        Edge("evolution.tree.roles.editor.b", "evolution.tree.roles.grouping.a", 1, "import"),
        Edge("evolution.tree.roles.grouping.a", "evolution.tree.roles.editor.b", 1, "import"),
    ]


def test_acyclic_subpackages_and_downward_imports_allowed(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "roles/grouping/a.py": "from ..editor import b\nfrom ...storage import c",
            "roles/editor/b.py": "from .. import shared",
            "roles/shared.py": "",
            "storage/c.py": "import pathlib\nimport evolution.other",
        },
    )
    assert upward_edges(root) == []


@pytest.mark.parametrize("statement", ["import evolution.tree", "from evolution.tree import API"])
def test_moved_to_root_is_upward(tmp_path: Path, statement: str) -> None:
    root = package_tree(tmp_path, {"storage/a.py": statement})
    assert upward_edges(root) == [Edge("evolution.tree.storage.a", "evolution.tree", 1, "import")]


def test_root_and_unplaced_sources_can_import_moved_packages(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "__init__.py": "from .storage import a",
            "legacy.py": "from .storage import a",
            "storage/a.py": "",
        },
    )
    assert upward_edges(root) == []


def test_tree_has_no_upward_edges() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "evolution" / "tree"
    edges = upward_edges(root)
    assert not edges, "\n".join(f"{edge.source}:{edge.line} -> {edge.target}" for edge in edges)


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("execution.durable_reference", ("storage", "execution")),
        ("target.connection_probe", ("storage", "execution", "artifacts", "target")),
    ],
)
def test_durable_reference_consumers_have_no_unplaced_dependencies(module, allowed):
    root = Path(__file__).resolve().parents[2] / "src" / "evolution" / "tree"
    source = "evolution.tree." + module
    path = root.joinpath(*module.split(".")).with_suffix(".py")
    visitor = _Imports(source, source.rpartition(".")[0], set())
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    prefixes = tuple("evolution.tree." + package + "." for package in allowed)
    assert not [edge for edge in visitor.edges if not (edge.target + ".").startswith(prefixes)]


@pytest.mark.parametrize(
    ("statement", "target", "line", "kind"),
    [
        ("import evolution.tree.legacy", "evolution.tree.legacy", 1, "import"),
        ("from ..legacy import Value", "evolution.tree.legacy", 1, "import"),
        ("from evolution.tree import API", "evolution.tree", 1, "import"),
        ("import evolution.tree", "evolution.tree", 1, "import"),
        (
            "def load():\n    from .. import legacy",
            "evolution.tree.legacy",
            2,
            "local",
        ),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .. import legacy",
            "evolution.tree.legacy",
            3,
            "type_checking",
        ),
        (
            "import importlib\nimportlib.import_module('evolution.tree.legacy')",
            "evolution.tree.legacy",
            2,
            "dynamic",
        ),
        (
            "from importlib import import_module as load\nload('evolution.tree')",
            "evolution.tree",
            2,
            "dynamic",
        ),
    ],
)
def test_storage_closure_rejects_external_edges(
    tmp_path: Path, statement: str, target: str, line: int, kind: str
) -> None:
    root = package_tree(tmp_path, {"storage/a.py": statement, "legacy.py": ""})
    assert storage_external_edges(root) == [Edge("evolution.tree.storage.a", target, line, kind)]


def test_storage_closure_allows_internal_and_external_library_imports(tmp_path: Path) -> None:
    root = package_tree(
        tmp_path,
        {
            "storage/a.py": "from . import b\nimport pathlib\nimport evolution.other",
            "storage/b.py": "",
        },
    )
    assert storage_external_edges(root) == []


def test_storage_is_closed() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "evolution" / "tree"
    edges = storage_external_edges(root)
    assert not edges, "\n".join(f"{edge.source}:{edge.line} -> {edge.target}" for edge in edges)


@pytest.mark.parametrize(
    "package_name", ["storage", "execution", "artifacts", "target", "roles", "manifest"]
)
def test_moved_package_has_no_module_cycles(package_name: str) -> None:
    root = Path(__file__).resolve().parents[2] / "src/evolution/tree"
    modules = {}
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(("evolution", "tree", *parts))] = path
    prefix = f"evolution.tree.{package_name}"
    graph = {}
    for module, path in modules.items():
        if module != prefix and not module.startswith(prefix + "."):
            continue
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        visitor = _Imports(module, package, set(modules))
        visitor.visit(ast.parse(path.read_text()))
        graph[module] = {
            edge.target for edge in visitor.edges if edge.target.startswith(prefix + ".")
        }
    assert tuple(TopologicalSorter(graph).static_order())
