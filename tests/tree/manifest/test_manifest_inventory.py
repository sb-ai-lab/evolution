"""The manifest dependency inventory (spec 2026-09-16): pure, lexical, complete over the parsed document."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from evolution.tree.manifest.manifest_inventory import (
    Dependency,
    DependencyInventory,
    manifest_dependencies,
)
from evolution.tree.manifest.manifest_v2 import (
    load_manifest_v2,
)
from evolution.tree.reference_target_cli import scaffold


def _by_field(inventory: DependencyInventory) -> dict[str, Dependency]:
    return {dep.field: dep for dep in inventory.dependencies}


def test_the_scaffold_inventory_is_the_golden_snapshot(tmp_path: Path) -> None:
    """`evo-target init`'s `/15` sample, every record in document order."""
    result = scaffold(tmp_path / "demo")
    manifest = load_manifest_v2(result.manifest)
    inventory = manifest_dependencies(manifest)
    root = result.manifest.parent

    assert inventory.schema == "evolution-v2-manifest/15"
    assert inventory.manifest_path == result.manifest
    assert [
        (d.role, d.field, d.kind, d.declared, d.base, d.resolved) for d in inventory.dependencies
    ] == [
        ("seed", "seed_root", "tree", "seed", "manifest_dir", root / "seed"),
        (
            "artifacts",
            "artifacts.dependency_paths[0]",
            "file",
            "uv.lock",
            "seed_root",
            root / "seed" / "uv.lock",
        ),
        ("scorer", "scorer.argv[0]", "executable", "python3", "path_lookup", None),
        (
            "scorer",
            "scorer.argv[1]",
            "argv_operand",
            "score.py",
            "cwd",
            root / "scorer" / "score.py",
        ),
        ("scorer", "scorer.cwd", "cwd", "scorer", "manifest_dir", root / "scorer"),
        ("editor", "editor.argv[0]", "executable", "python3", "path_lookup", None),
        ("editor", "editor.argv[1]", "argv_operand", "edit.py", "cwd", root / "editor" / "edit.py"),
        ("editor", "editor.cwd", "cwd", "editor", "manifest_dir", root / "editor"),
        ("reflector", "reflector.argv[0]", "executable", "python3", "path_lookup", None),
        (
            "reflector",
            "reflector.argv[1]",
            "argv_operand",
            "reflect.py",
            "cwd",
            root / "reflector" / "reflect.py",
        ),
        ("reflector", "reflector.cwd", "cwd", "reflector", "manifest_dir", root / "reflector"),
        ("corpus", "corpus.path", "file", "corpus.jsonl", "manifest_dir", root / "corpus.jsonl"),
    ]
    assert inventory.environment == ()
    assert inventory.boundary == (
        "runtime_loads_unknown",
        "argv_operands_heuristic",
        "path_lookup_at_bind",
    )


def test_argv0_takes_three_shapes(tmp_path: Path) -> None:
    """Absolute → absolute; with a slash → cwd; bare → path_lookup."""
    result = scaffold(tmp_path / "demo")
    doc = json.loads(result.manifest.read_text())
    absolute = str((result.manifest.parent / "scorer" / "score.py").resolve())
    doc["scorer"]["argv"] = [absolute, "in.json"]
    doc["editor"]["argv"] = ["./edit.py"]
    doc["reflector"]["argv"] = ["python3", "reflect.py"]
    result.manifest.write_text(json.dumps(doc))
    deps = _by_field(manifest_dependencies(load_manifest_v2(result.manifest)))
    root = result.manifest.parent
    assert (deps["scorer.argv[0]"].base, deps["scorer.argv[0]"].resolved) == (
        "absolute",
        Path(absolute),
    )
    assert (deps["editor.argv[0]"].base, deps["editor.argv[0]"].resolved) == (
        "cwd",
        root / "editor" / "./edit.py",
    )
    assert (deps["reflector.argv[0]"].base, deps["reflector.argv[0]"].resolved) == (
        "path_lookup",
        None,
    )
    assert deps["scorer.argv[1]"].resolved == root / "scorer" / "in.json"


def test_the_absolute_base_overrides_a_declared_path_and_an_argv_operand(
    tmp_path: Path,
) -> None:
    """`_Walk.declared_path`'s `base = "absolute"` override, on a command's `cwd`
     and on a `dependency_paths` entry, plus an absolute `argv[1:]` operand
    ."""
    result = scaffold(tmp_path / "demo")
    root = result.manifest.parent
    doc = json.loads(result.manifest.read_text())

    reflector_cwd = str((root / "reflector").resolve())
    dependency_file = root / "scorer" / "extra.lock"
    dependency_file.write_text("x\n")
    dependency_abs = str(dependency_file.resolve())
    operand_file = root / "scorer" / "operand.json"
    operand_file.write_text("{}\n")
    operand_abs = str(operand_file.resolve())

    doc["reflector"]["cwd"] = reflector_cwd
    doc["scorer"]["dependency_paths"] = [dependency_abs]
    doc["scorer"]["argv"] = ["python3", "score.py", operand_abs]
    result.manifest.write_text(json.dumps(doc))

    deps = _by_field(manifest_dependencies(load_manifest_v2(result.manifest)))

    assert deps["reflector.cwd"].base == "absolute"
    assert deps["reflector.cwd"].declared == reflector_cwd
    assert deps["reflector.cwd"].resolved == Path(reflector_cwd)

    assert deps["scorer.dependency_paths[0]"].base == "absolute"

    assert deps["scorer.argv[2]"].base == "absolute"
    assert deps["scorer.argv[2]"].resolved == Path(operand_abs)


def test_the_inventory_touches_nothing_outside_the_parsed_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Purity (spec «Границы»): the filesystem, PATH and the environment are never consulted."""
    result = scaffold(tmp_path / "demo")
    manifest = load_manifest_v2(result.manifest)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the inventory must not touch the filesystem or the environment")

    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(os.path, "realpath", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    monkeypatch.setattr(os, "environ", {})
    first = manifest_dependencies(manifest)
    second = manifest_dependencies(manifest)
    assert first == second
    assert first.dependencies
