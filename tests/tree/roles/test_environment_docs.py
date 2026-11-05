"""Contracts for the Reflector's environment-documentation rendering and snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evolution.tree.roles.environment_docs import (
    HEADER,
    EnvironmentDocsError,
    _v2_catalog_page,
    _v2_from_files,
    _v2_source_files,
    render_environment_docs,
    snapshot_state,
    write_snapshot,
)
from evolution.tree.roles.reflection_reader import MAX_RESPONSE_BYTES


def docs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "api").mkdir(parents=True)
    (root / "api" / "people.md").write_text("GET /api/v1/people\n")
    (root / "README.md").write_text("Catalog\n")
    return root


def test_render_is_sorted_fenced_and_headed(tmp_path):
    docs = render_environment_docs(docs_dir(tmp_path), budget=60_000)
    assert docs.text.startswith(HEADER)
    assert docs.text.index("=== ENVIRONMENT DOC: README.md ===") < docs.text.index(
        "=== ENVIRONMENT DOC: api/people.md ==="
    )
    assert docs.digest == hashlib.sha256(docs.text.encode("utf-8")).hexdigest()
    assert [name for name, _ in docs.files] == ["README.md", "api/people.md"]


def test_budget_overflow_is_a_refusal_not_a_truncation(tmp_path):
    with pytest.raises(EnvironmentDocsError) as info:
        render_environment_docs(docs_dir(tmp_path), budget=10)
    assert info.value.code == "budget_exceeded"


def test_non_utf8_and_symlinks_refused(tmp_path):
    root = docs_dir(tmp_path)
    (root / "bin.dat").write_bytes(b"\xff\xfe")
    with pytest.raises(EnvironmentDocsError) as info:
        render_environment_docs(root, budget=60_000)
    assert info.value.code == "not_utf8"
    (root / "bin.dat").unlink()
    (root / "link.md").symlink_to(root / "README.md")
    with pytest.raises(EnvironmentDocsError) as info:
        render_environment_docs(root, budget=60_000)
    assert info.value.code == "not_regular"


def test_symlinked_subdirectory_is_a_refusal_not_a_silent_drop(tmp_path):
    """`followlinks=False` lists a symlinked directory and declines to walk it.

    Without the
    `dirnames` inspection `api ->../catalog` renders successfully and silently
    excludes the whole subtree, which is the hidden truncation this module
    refuses everywhere else.
    """

    root = docs_dir(tmp_path)
    elsewhere = tmp_path / "catalog"
    elsewhere.mkdir()
    (elsewhere / "people.md").write_text("GET /api/v1/people\n")
    (root / "linked").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(EnvironmentDocsError) as info:
        render_environment_docs(root, budget=60_000)

    assert (info.value.code, info.value.detail) == ("not_regular", "linked")


def test_unreadable_file_is_a_refusal_not_a_traceback(tmp_path):
    root = docs_dir(tmp_path)
    secret = root / "secret.md"
    secret.write_text("locked\n")
    secret.chmod(0o000)
    try:
        with pytest.raises(EnvironmentDocsError) as info:
            render_environment_docs(root, budget=60_000)
    finally:
        secret.chmod(0o600)
    assert (info.value.code, info.value.detail) == ("unreadable", "secret.md")


def test_unreadable_subdirectory_is_a_refusal_not_a_silent_drop(tmp_path):
    root = docs_dir(tmp_path)
    closed = root / "closed"
    closed.mkdir()
    (closed / "hidden.md").write_text("hidden\n")
    closed.chmod(0o000)
    try:
        with pytest.raises(EnvironmentDocsError) as info:
            render_environment_docs(root, budget=60_000)
    finally:
        closed.chmod(0o700)
    assert (info.value.code, info.value.detail) == ("unreadable", "closed")


def test_empty_directory_renders_to_the_header_alone(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    docs = render_environment_docs(root, budget=60_000)
    assert docs.text == HEADER and docs.files == ()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_snapshot(docs, run_dir)
    again = snapshot_state(run_dir, budget=60_000)
    assert again is not None and again.digest == docs.digest


def test_snapshot_round_trips_and_is_owner_only(tmp_path):
    docs = render_environment_docs(docs_dir(tmp_path), budget=60_000)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = write_snapshot(docs, run_dir)
    assert root == run_dir / "environment-docs"
    assert (root.stat().st_mode & 0o077) == 0
    again = snapshot_state(run_dir, budget=60_000)
    assert again is not None and again.digest == docs.digest
    assert snapshot_state(tmp_path / "other", budget=60_000) is None


def test_v2_catalog_pages_deep_control_character_paths_within_reader_bound(tmp_path) -> None:
    root = tmp_path / "deep-docs"
    paths: list[str] = []
    for index in range(32):
        parts = [f"{index:02d}" + "d" * 238, *("n" * 240 for _ in range(14))]
        document = root.joinpath(*parts, f"\x01{index:02d}" + "f" * 236)
        document.parent.mkdir(parents=True)
        document.write_text("x", encoding="utf-8")
        paths.append(document.relative_to(root).as_posix())
    snapshot = _v2_from_files(_v2_source_files(root))

    first = _v2_catalog_page(snapshot)
    assert 0 < len(first.entries) < 32
    response = {
        "entries": [entry.__dict__ for entry in first.entries],
        "total": first.total,
        "next_offset": first.next_offset,
    }
    assert (
        len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode())
        <= MAX_RESPONSE_BYTES
    )

    received = list(first.entries)
    offset = first.next_offset
    while offset is not None:
        page = _v2_catalog_page(snapshot, offset=offset)
        received.extend(page.entries)
        offset = page.next_offset
    assert [entry.path for entry in received] == sorted(paths)
