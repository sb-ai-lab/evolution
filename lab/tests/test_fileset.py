"""One file-set rule for Bundle copies and project inventories (ADR-0017 p. 6)."""
import os
from pathlib import Path

import pytest

from evolution_lab._fileset import entry_fields, excluded_name, excluded_path


@pytest.mark.parametrize("name,directory,expected", [
    (".git", True, True), (".git", False, True),  # a worktree's .git is a file
    ("__pycache__", True, True), (".pytest_cache", True, True),
    (".mypy_cache", True, True), (".ruff_cache", True, True),
    ("m.pyc", False, True), ("m.pyo", False, True),
    ("m.pyc", True, False), ("src", True, False), ("data.jsonl", False, False),
    (".gitignore", False, False), (".venv", True, True),
    ("venv", True, False),  # only the dotted name is surely an environment; ``venv/`` may be code
])
def test_excluded_name(name, directory, expected):
    assert excluded_name(name, directory=directory) is expected


@pytest.mark.parametrize("relative,expected", [
    ("inputs/a/.git/HEAD", True), ("pkg/__pycache__/m.cpython-314.pyc", True),
    ("pkg/m.pyc", True), ("pkg/m.py", False), ("lab/corpus.jsonl", False), ("", False),
])
def test_excluded_path(relative, expected):
    assert excluded_path(relative) is expected


def test_entry_fields_shape(tmp_path):
    (tmp_path / "d").mkdir()
    script = tmp_path / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    os.chmod(script, 0o755)
    directory = entry_fields("d", (tmp_path / "d").lstat(), sha256=None)
    file = entry_fields("run.sh", script.lstat(), sha256="ab" * 32)
    assert directory == {"path": "d", "type": "directory", "executable": 0o111 & (tmp_path / "d").lstat().st_mode, "size": None, "sha256": None}
    assert file == {"path": "run.sh", "type": "file", "executable": 0o111, "size": 10, "sha256": "ab" * 32}


@pytest.mark.parametrize("path,directory,expected", [
    ("/w/.git", True, True), ("/w/.git", False, True), ("/w/.git/hooks", True, True),
    ("/w/.pytest_cache/v/cache.json", False, True), ("/w/m.pyc", False, True),
    ("/w/d.pyc", True, False), ("/w/d.pyc/sub", True, False), ("/w/lab", True, False),
    ("/w/.gitignore", False, False),
])
def test_excluded_source(path, directory, expected):
    """One rule at the top-level boundary for Bundle roots and inventory sources: an excluded
    ancestor, or the leaf's own name with its directory flag (codex r2 follow-up)."""
    from evolution_lab._fileset import excluded_source
    assert excluded_source(Path(path), directory=directory) is expected
