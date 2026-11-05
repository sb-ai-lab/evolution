from __future__ import annotations

import pytest

from evolution.tree.campaign_schema import cohort_rows_digest
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.champion_gaps import (
    champion_gap_rows,
    champion_owner,
    champion_rows_evidence,
)

MID = "m-r01-champion"


def _entry(case_id, sample_id):
    return {"case_id": case_id, "sample_id": sample_id}


def _row(
    case_id,
    sample_id,
    *,
    outcome,
    ordinal=0,
    exhausted=False,
    imported=False,
    disposition=None,
    score=None,
):
    if disposition is None:
        disposition = {
            "completed": "score",
            "cancelled": "exclude",
            "target_failed": "indeterminate",
            "infrastructure_failed": "indeterminate",
        }[outcome]
    return SampleRow(
        case_id=case_id,
        sample_id=sample_id,
        repeat=int(sample_id),
        requested_seed=None,
        effective_seed=None,
        boot_id="",
        dependency_digest="d" * 64,
        outcome=outcome,
        disposition=disposition,
        metrics={"score": score} if disposition == "score" else None,
        critical_pass=True if disposition == "score" else None,
        exhausted=exhausted,
        retry_ordinal=ordinal,
        imported=imported,
    )


def _gaps(row, declined=()):
    return champion_gap_rows(MID, [_entry(row.case_id, row.sample_id)], [row], declined)


def test_a_cancelled_try_is_a_rollout_gap_at_its_ordinal():
    (found,) = _gaps(_row("c1", "01", outcome="cancelled"))
    assert found == {
        "measurement_id": MID,
        "case_id": "c1",
        "sample_id": "01",
        "retry_ordinal": 0,
        "owner": "rollout",
    }


def test_a_declined_target_failure_is_a_target_gap():
    row = _row("c1", "01", outcome="target_failed")
    (found,) = _gaps(row, declined={("c1", "01", 0)})
    assert found["owner"] == "target"


def test_an_undeclined_target_failure_below_the_limit_owes_a_try():
    assert _gaps(_row("c1", "01", outcome="target_failed")) == ()


def test_an_exhausted_target_failure_is_not_a_gap():
    row = _row("c1", "01", outcome="target_failed", exhausted=True)
    assert _gaps(row, declined={("c1", "01", 0)}) == ()


def test_an_infrastructure_failure_is_a_fault_row_not_a_gap():
    row = _row("c1", "01", outcome="infrastructure_failed")
    assert _gaps(row, declined={("c1", "01", 0)}) == ()


def test_imported_and_scored_rows_are_not_gaps():
    assert _gaps(_row("c1", "01", outcome="completed", score=1.0, imported=True)) == ()
    assert _gaps(_row("c1", "01", outcome="completed", score=1.0)) == ()


def test_a_declined_unscored_row_is_a_rollout_gap():
    row = _row("c1", "01", outcome="completed", disposition="unscored")
    (found,) = _gaps(row, declined={("c1", "01", 0)})
    assert found["owner"] == "rollout"


def test_a_cancelled_repair_try_is_listed_at_its_ordinal():
    (found,) = _gaps(_row("c1", "01", outcome="cancelled", ordinal=2))
    assert found["retry_ordinal"] == 2


def test_a_cancelled_and_a_declined_try_are_gaps_in_schedule_order():
    schedule = [_entry("c2", "01"), _entry("c1", "01")]
    rows = [
        _row("c1", "01", outcome="cancelled", ordinal=0),
        _row("c2", "01", outcome="target_failed", ordinal=0),
    ]
    found = champion_gap_rows(MID, schedule, rows, declined={("c2", "01", 0)})
    assert [(r["case_id"], r["owner"], r["retry_ordinal"]) for r in found] == [
        ("c2", "target", 0),
        ("c1", "rollout", 0),
    ]


def test_evidence_over_many_gaps_is_bounded_but_digests_the_whole_set():
    rows = [
        {
            "measurement_id": MID,
            "case_id": f"c{i:03d}",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "rollout",
        }
        for i in range(300)
    ]
    evidence = champion_rows_evidence(MID, rows)
    assert set(evidence) == {
        "evidence_kind",
        "measurement_id",
        "count",
        "rows_digest",
        "truncated",
        "rows",
    }
    assert evidence["evidence_kind"] == "champion_rows"
    assert evidence["count"] == 300
    assert evidence["truncated"] is True
    assert len(evidence["rows"]) == 256
    assert evidence["rows_digest"] == cohort_rows_digest(rows)


def test_evidence_without_rows_is_refused():
    with pytest.raises(ValueError):
        champion_rows_evidence(MID, [])


def test_owner_is_arbitrated_over_the_rows():
    rows = [{"owner": "rollout"}, {"owner": "target"}]
    assert champion_owner(rows) == "target"
