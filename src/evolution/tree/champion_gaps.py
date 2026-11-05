"""The Champion measurement's cancelled-gap rows and `champion_rows` evidence (#194).

Pure functions over a measurement's schedule and latest-try rows. They write
no file and import nothing from the executor, so the `close_measurement`
writer and the audit read one implementation, as `confidence_cohort` does for
the final cohort.
"""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence
from typing import Any, Final

from evolution.tree.campaign_schema import MAX_FAULT_ROWS, cohort_rows_digest
from evolution.tree.campaign_statistics import SampleRow, arbitrate_owner
from evolution.tree.confidence_cohort import cohort_gap, gap_owner

#: The suspension reason and evidence kind of a Champion measurement left with gaps.
CHAMPION_INCOMPLETE: Final = "champion_incomplete"
CHAMPION_ROWS: Final = "champion_rows"


def champion_gap_rows(
    measurement_id: str,
    schedule: Sequence[Mapping[str, Any]],
    rows: Sequence[SampleRow],
    declined: Container[tuple[str, str, int]],
) -> tuple[dict[str, Any], ...]:
    """The pairs a Champion measurement closes without a score, in schedule order.

    A gap is a pair `cohort_gap` names: unscored and owed no try (a `cancelled`
    row, or a try whose retry the cancelled run declined). An exhausted row
    keeps `pairs_excluded`, an imported row is a score, and an
    `infrastructure_failed` row is a fault row that suspends first (G4), so
    none of them is listed.
    """

    by_key = {(row.case_id, row.sample_id): row for row in rows}
    found: list[dict[str, Any]] = []
    for entry in schedule:
        row = by_key.get((str(entry["case_id"]), str(entry["sample_id"])))
        if row is None or row.imported or row.exhausted or row.outcome == "infrastructure_failed":
            continue
        if not cohort_gap(row, declined):
            continue
        found.append(
            {
                "measurement_id": measurement_id,
                "case_id": row.case_id,
                "sample_id": row.sample_id,
                "retry_ordinal": row.retry_ordinal,
                "owner": gap_owner(row),
            }
        )
    return tuple(found)


def champion_rows_evidence(
    measurement_id: str, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The `champion_rows` evidence: first 256 rows, whole count and digest."""

    if not rows:
        raise ValueError("a champion_rows suspension names at least one pair")
    return {
        "evidence_kind": CHAMPION_ROWS,
        "measurement_id": measurement_id,
        "count": len(rows),
        "rows_digest": cohort_rows_digest(rows),
        "truncated": len(rows) > MAX_FAULT_ROWS,
        "rows": [dict(row) for row in rows[:MAX_FAULT_ROWS]],
    }


def champion_owner(rows: Sequence[Mapping[str, Any]]) -> str:
    """The suspension's owner: `arbitrate_owner` over the rows' owners."""

    return arbitrate_owner([str(row["owner"]) for row in rows])
