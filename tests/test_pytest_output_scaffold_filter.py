"""``pytest_output.txt`` (runner.py) must stay out of produced_artifacts by
NAME, not by the incidental ``"test_"`` substring inside ``py``test_``output``.
A rename of that file must not silently re-admit it to the leakage corpus."""

from __future__ import annotations

from pathlib import Path

from evolution.eval.evidence import (
    _FORBIDDEN_SUBSTR,
    _SCAFFOLD_NAMES,
    _is_scaffold_or_forbidden,
    _produced_manifest,
)


def test_pytest_output_is_scaffold_filtered_by_explicit_name():
    assert "pytest_output.txt" in _SCAFFOLD_NAMES
    assert _is_scaffold_or_forbidden("pytest_output.txt")


def test_filter_does_not_rely_on_test_substring_coincidence():
    # If the only thing catching it were the "test_" substring, dropping that
    # token would re-admit the file; the explicit name must catch it regardless.
    assert _is_scaffold_or_forbidden("pytest_output.txt")
    assert not any(s in "pytest_output.txt" for s in _FORBIDDEN_SUBSTR if s != "test_")


def test_pytest_output_excluded_from_produced_manifest(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "output.txt").write_text("hello\n", encoding="utf-8")
    (workspace / "pytest_output.txt").write_text("1 passed\n", encoding="utf-8")

    manifest = _produced_manifest(workspace, pre={})
    assert isinstance(manifest, list)
    paths = {e.get("path") for e in manifest if isinstance(e, dict)}
    assert "output.txt" in paths
    assert "pytest_output.txt" not in paths


def test_underscore_prefixed_dir_is_scaffold_filtered():
    # _workspace_guard_bin/ holds the write-guard's PATH shims (chmod, ls, ...);
    # their basenames don't start with "_", so the prefix check must look at any
    # path component, not just the basename. Otherwise the shims flood the
    # produced-artifact evidence and mislead the reflector about output paths.
    assert _is_scaffold_or_forbidden("_workspace_guard_bin/chmod")
    assert _is_scaffold_or_forbidden("output/_workspace_guard_bin/grep")
    assert not _is_scaffold_or_forbidden("output/clean.csv")


def test_guard_bin_shims_excluded_from_produced_manifest(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "_workspace_guard_bin").mkdir(parents=True)
    for cmd in ("chmod", "grep", "ls", "rm"):
        (workspace / "_workspace_guard_bin" / cmd).write_text("#!/bin/sh\n", encoding="utf-8")
    (workspace / "clean.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    manifest = _produced_manifest(workspace, pre={})
    assert isinstance(manifest, list)
    paths = {e.get("path") for e in manifest if isinstance(e, dict)}
    assert "clean.csv" in paths
    assert not any("_workspace_guard_bin" in p for p in paths if p)
