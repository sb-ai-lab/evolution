"""#194: the `champion_incomplete` suspension payload (families 13 and 14)."""

from __future__ import annotations

from evolution.tree.campaign_schema import (
    cohort_rows_digest,
    validate_payload,
)

MID = "m-r01-champion"


def _row(case: str = "c1", sample: str = "01", ordinal: int = 0, owner: str = "target"):
    return {
        "measurement_id": MID,
        "case_id": case,
        "sample_id": sample,
        "retry_ordinal": ordinal,
        "owner": owner,
    }


def _payload(owner: str = "target", rows=None, count=None):
    rows = [_row()] if rows is None else rows
    return {
        "reason": "champion_incomplete",
        "owner": owner,
        "evidence": {
            "evidence_kind": "champion_rows",
            "measurement_id": MID,
            "count": len(rows) if count is None else count,
            "rows_digest": cohort_rows_digest(rows),
            "truncated": False,
            "rows": rows,
        },
    }


def test_it_is_admitted_in_family_14():
    validate_payload("campaign_suspended", _payload(), final=True)
