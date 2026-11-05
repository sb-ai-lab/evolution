"""rev E1: the rejected-edit buffer is scoped per cumulative row. A rejection
recorded for one cell's row must not surface in another cell's reflector
prompt — ``row_id`` is constant across rounds within a cell, distinct across
cells."""

from __future__ import annotations

from pathlib import Path

from evolution.evolution.meta_skill import (
    build_rejected_edits_context,
    build_rejected_entry,
    write_round_record,
)


def _skill_path(tmp_path: Path) -> Path:
    p = tmp_path / ".agents" / "skills" / "test-skill"
    p.mkdir(parents=True)
    return p


def _seed_two_rows(skill_path: Path) -> None:
    write_round_record(
        skill_path,
        {
            "timestamp": "20260601_120000_000001",
            "round": 1,
            "rejected": build_rejected_entry(
                row_id="row3__de.sql",
                candidate_status="leak_rejected",
                new_headings=["## SQL"],
            ),
        },
    )
    write_round_record(
        skill_path,
        {
            "timestamp": "20260601_120001_000002",
            "round": 1,
            "rejected": build_rejected_entry(
                row_id="row4__pm.pdf",
                candidate_status="size_violation",
            ),
        },
    )


def test_rejected_buffer_is_row_scoped(tmp_path):
    sp = _skill_path(tmp_path)
    _seed_two_rows(sp)

    sql_blob = build_rejected_edits_context(sp, row_id="row3__de.sql")
    assert "leak_rejected" in sql_blob
    assert "size_violation" not in sql_blob

    pdf_blob = build_rejected_edits_context(sp, row_id="row4__pm.pdf")
    assert "size_violation" in pdf_blob
    assert "leak_rejected" not in pdf_blob


def test_unfiltered_buffer_includes_all_rows(tmp_path):
    sp = _skill_path(tmp_path)
    _seed_two_rows(sp)

    blob = build_rejected_edits_context(sp, row_id="")
    assert "leak_rejected" in blob
    assert "size_violation" in blob


def test_empty_when_no_rejections(tmp_path):
    sp = _skill_path(tmp_path)
    assert build_rejected_edits_context(sp, row_id="row3__de.sql") == ""
