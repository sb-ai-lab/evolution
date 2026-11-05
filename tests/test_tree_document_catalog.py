"""Private v2 contracts for optional, snapshot-backed document access."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evolution.tree.roles.environment_docs import (
    _V2_MAX_BYTES,
    ENVIRONMENT_DOCS_ROOT,
    EnvironmentDocsError,
    _v2_catalog_page,
    _v2_check_document_snapshot,
    _v2_create_document_snapshot,
    _v2_load_document_snapshot,
    _v2_read_document_page,
    _v2_repair_document_snapshot,
)


def _docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "api").mkdir(parents=True)
    (root / "README.md").write_text("overview\n", encoding="utf-8")
    (root / "api" / "people.md").write_text("АБВ😀ГДЕ", encoding="utf-8")
    return root


def test_v2_catalog_has_sorted_opaque_entries_and_never_includes_bodies(tmp_path: Path) -> None:
    snapshot = _v2_create_document_snapshot(_docs(tmp_path))

    first = _v2_catalog_page(snapshot, offset=0)

    assert first.error is None
    assert first.total == 2 and first.next_offset is None
    assert [(entry.path, entry.title, entry.size) for entry in first.entries] == [
        ("README.md", "README.md", 9),
        ("api/people.md", "people.md", 16),
    ]
    assert all(entry.document_id.startswith("doc_") for entry in first.entries)
    assert all(
        len(entry.document_id) == 68 and set(entry.document_id[4:]) <= set("0123456789abcdef")
        for entry in first.entries
    )
    assert all(
        not entry.path.startswith("/") and ".." not in entry.path.split("/")
        for entry in first.entries
    )
    assert all(
        "overview" not in repr(entry) and "АБВ" not in repr(entry) for entry in first.entries
    )
    assert len(snapshot.digest) == 64


def test_v2_document_ids_are_stable_for_same_path_and_bytes_and_change_for_either(
    tmp_path: Path,
) -> None:
    root = _docs(tmp_path)
    original = _v2_create_document_snapshot(root)
    original_id = _v2_catalog_page(original).entries[1].document_id

    assert (
        original_id == _v2_catalog_page(_v2_create_document_snapshot(root)).entries[1].document_id
    )
    (root / "api" / "people.md").write_text("changed", encoding="utf-8")
    assert (
        original_id != _v2_catalog_page(_v2_create_document_snapshot(root)).entries[1].document_id
    )
    (root / "api" / "people.md").rename(root / "api" / "accounts.md")
    assert (
        original_id != _v2_catalog_page(_v2_create_document_snapshot(root)).entries[1].document_id
    )


def test_v2_catalog_and_document_pages_use_explicit_offsets_and_unicode_characters(
    tmp_path: Path,
) -> None:
    snapshot = _v2_create_document_snapshot(_docs(tmp_path))
    document_id = _v2_catalog_page(snapshot).entries[1].document_id

    page = _v2_read_document_page(snapshot, document_id=document_id, offset=1)
    eof = _v2_read_document_page(snapshot, document_id=document_id, offset=7)

    assert (page.content, page.total_chars, page.next_offset, page.digest, page.error) == (
        "БВ😀ГДЕ",
        7,
        None,
        _v2_catalog_page(snapshot).entries[1].digest,
        None,
    )
    assert (eof.content, eof.total_chars, eof.next_offset, eof.error) == ("", 7, None, None)


def test_v2_catalog_is_paged_at_32_entries_and_document_bodies_at_16000_characters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    for number in range(33):
        (root / f"{number:02}.md").write_text("x", encoding="utf-8")
    (root / "32.md").write_text("z" * 16_001, encoding="utf-8")
    snapshot = _v2_create_document_snapshot(root)

    first = _v2_catalog_page(snapshot)
    second = _v2_catalog_page(snapshot, offset=32)
    body = _v2_read_document_page(snapshot, document_id=second.entries[0].document_id)

    assert (len(first.entries), first.total, first.next_offset) == (32, 33, 32)
    assert (len(second.entries), second.next_offset) == (1, None)
    assert (len(body.content or ""), body.total_chars, body.next_offset) == (16_000, 16_001, 16_000)


@pytest.mark.parametrize(
    ("operation", "kwargs", "code"),
    [
        ("catalog", {"offset": -1}, "invalid_offset"),
        ("catalog", {"offset": 3}, "invalid_offset"),
        ("read", {"document_id": "bad", "offset": 0}, "unknown_document_id"),
        ("read", {"document_id": "bad", "offset": -1}, "invalid_offset"),
    ],
)
def test_v2_page_errors_are_bounded_and_recoverable(
    tmp_path: Path, operation, kwargs, code
) -> None:
    snapshot = _v2_create_document_snapshot(_docs(tmp_path))
    page = (
        _v2_catalog_page(snapshot, **kwargs)
        if operation == "catalog"
        else _v2_read_document_page(snapshot, **kwargs)
    )

    assert page.error == code
    assert page.next_offset is None
    assert len(repr(page)) < 500


def test_v2_refuses_empty_or_unsafe_source_collections(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EnvironmentDocsError) as empty_error:
        _v2_create_document_snapshot(empty)
    assert empty_error.value.code == "empty"

    root = _docs(tmp_path)
    (root / "link.md").symlink_to(root / "README.md")
    with pytest.raises(EnvironmentDocsError) as link_error:
        _v2_create_document_snapshot(root)
    assert link_error.value.code == "not_regular"

    (root / "link.md").unlink()
    (root / "bad.md").write_bytes(b"\xff")
    with pytest.raises(EnvironmentDocsError) as encoding_error:
        _v2_create_document_snapshot(root)
    assert encoding_error.value.code == "not_utf8"


def test_v2_refuses_source_bytes_over_its_snapshot_capacity(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "large.md").write_bytes(b"x" * 11)
    monkeypatch.setattr("evolution.tree.roles.environment_docs._V2_MAX_BYTES", 10)

    with pytest.raises(EnvironmentDocsError) as oversized:
        _v2_create_document_snapshot(root)

    assert oversized.value.code == "too_many_bytes"
    assert _V2_MAX_BYTES == 64 * 1024 * 1024


def test_v2_stops_a_capacity_read_without_calling_an_unbounded_file_reader(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "large.md").write_bytes(b"x" * 11)
    monkeypatch.setattr("evolution.tree.roles.environment_docs._V2_MAX_BYTES", 10)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError()))

    with pytest.raises(EnvironmentDocsError) as oversized:
        _v2_create_document_snapshot(root)

    assert oversized.value.code == "too_many_bytes"


def test_v2_refuses_source_file_counts_over_its_snapshot_capacity(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "first.md").write_text("first", encoding="utf-8")
    (root / "second.md").write_text("second", encoding="utf-8")
    monkeypatch.setattr("evolution.tree.roles.environment_docs._V2_MAX_FILES", 1)

    with pytest.raises(EnvironmentDocsError) as oversized:
        _v2_create_document_snapshot(root)

    assert oversized.value.code == "too_many_files"


def test_v2_snapshot_load_detects_changed_bytes_and_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = _docs(tmp_path)
    source = _v2_create_document_snapshot(root)
    _v2_create_document_snapshot(root, run_dir=run_dir)
    stored = _v2_load_document_snapshot(run_dir, expected_digest=source.digest)
    assert stored.digest == source.digest

    snapshot_file = run_dir / ENVIRONMENT_DOCS_ROOT / "README.md"
    snapshot_file.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(EnvironmentDocsError) as changed:
        _v2_load_document_snapshot(run_dir, expected_digest=source.digest)
    assert changed.value.code == "snapshot_invalid"

    snapshot_file.write_text("overview\n", encoding="utf-8")
    (run_dir / ENVIRONMENT_DOCS_ROOT / "extra.md").write_text("stale\n", encoding="utf-8")
    with pytest.raises(EnvironmentDocsError) as extra:
        _v2_load_document_snapshot(run_dir, expected_digest=source.digest)
    assert extra.value.code == "snapshot_invalid"


def test_v2_resume_repairs_only_matching_live_bytes_without_merging_snapshot(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = _docs(tmp_path)
    expected = _v2_create_document_snapshot(root, run_dir=run_dir)
    snapshot_root = run_dir / ENVIRONMENT_DOCS_ROOT
    (snapshot_root / "README.md").write_text("damaged\n", encoding="utf-8")
    (snapshot_root / "stale.md").write_text("must disappear\n", encoding="utf-8")

    live, repair_needed = _v2_check_document_snapshot(
        root, run_dir, expected_digest=expected.digest
    )

    # The check is read-only: the damage is still there until the repair runs.
    assert repair_needed
    assert (snapshot_root / "README.md").read_text(encoding="utf-8") == "damaged\n"

    repaired = _v2_repair_document_snapshot(live, run_dir)

    assert repaired.digest == expected.digest
    assert (snapshot_root / "README.md").read_text(encoding="utf-8") == "overview\n"
    assert not (snapshot_root / "stale.md").exists()
    assert (
        _v2_load_document_snapshot(run_dir, expected_digest=expected.digest).digest
        == expected.digest
    )


@pytest.mark.parametrize("filename", [b"bad\xff.md", b"\xc3\xa9\xff.md"])
def test_v2_resume_repairs_a_snapshot_with_a_non_utf8_filename(
    tmp_path: Path, filename: bytes
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = _docs(tmp_path)
    expected = _v2_create_document_snapshot(root, run_dir=run_dir)
    bad_name = os.fsdecode(filename)
    bad_path = os.fsencode(run_dir / ENVIRONMENT_DOCS_ROOT) + b"/" + os.fsencode(bad_name)
    with open(bad_path, "wb") as handle:
        handle.write(b"invalid filename")

    live, repair_needed = _v2_check_document_snapshot(
        root, run_dir, expected_digest=expected.digest
    )
    assert repair_needed
    repaired = _v2_repair_document_snapshot(live, run_dir)

    assert repaired.digest == expected.digest
    assert not os.path.lexists(bad_path)


def test_v2_resume_refuses_changed_live_source_without_mutating_damaged_snapshot(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = _docs(tmp_path)
    expected = _v2_create_document_snapshot(root, run_dir=run_dir)
    snapshot_file = run_dir / ENVIRONMENT_DOCS_ROOT / "README.md"
    snapshot_file.write_text("damaged\n", encoding="utf-8")
    before = snapshot_file.read_bytes()
    (root / "README.md").write_text("changed live\n", encoding="utf-8")

    with pytest.raises(EnvironmentDocsError) as refusal:
        _v2_check_document_snapshot(root, run_dir, expected_digest=expected.digest)

    assert refusal.value.code == "identity_changed"
    assert snapshot_file.read_bytes() == before


def test_v2_resume_requires_live_source_even_when_snapshot_is_intact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = _docs(tmp_path)
    expected = _v2_create_document_snapshot(root, run_dir=run_dir)
    original = (run_dir / ENVIRONMENT_DOCS_ROOT / "README.md").read_bytes()
    (root / "README.md").unlink()

    with pytest.raises(EnvironmentDocsError) as refusal:
        _v2_check_document_snapshot(root, run_dir, expected_digest=expected.digest)

    assert refusal.value.code == "identity_changed"
    assert (run_dir / ENVIRONMENT_DOCS_ROOT / "README.md").read_bytes() == original
