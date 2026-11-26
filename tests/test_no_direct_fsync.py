"""Keep filesystem durability policy at its owning boundary."""

import ast
from pathlib import Path


def test_no_direct_fsync():
    root = Path(__file__).resolve().parents[1] / "src" / "evolution"
    offenders = []
    for path in root.rglob("*.py"):
        if path == root / "lineage" / "state_io.py":
            continue
        tree = ast.parse(path.read_text())
        os_names = {"os"}
        sync_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                os_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "os"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                sync_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in {"fsync", "fdatasync"}
                )

        def direct(node, os_names=os_names):
            return (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in os_names
                and node.attr in {"fsync", "fdatasync"}
            )

        bindings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                bindings.append((node.targets, node.value))
            elif isinstance(node, ast.AnnAssign):
                bindings.append(([node.target], node.value))
        changed = True
        while changed:
            previous = set(sync_names)
            for targets, value in bindings:
                if direct(value) or isinstance(value, ast.Name) and value.id in sync_names:
                    sync_names.update(t.id for t in targets if isinstance(t, ast.Name))
            changed = sync_names != previous
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                direct(node.func) or isinstance(node.func, ast.Name) and node.func.id in sync_names
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, "\n".join(offenders)
