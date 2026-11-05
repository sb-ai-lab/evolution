"""Shared Table 3 contracts and split semantics.

This module is intentionally small and stdlib-only. It defines the parts
of the experiment that must not be reinterpreted by each runner:

* the eight Table 3 rows;
* the four metrics;
* source-artifact vs target-task semantics, especially GenR.

The key invariant is that GenR evaluates a source cell's produced
artifact on the anchor partner's held-out test tasks. It must not load
the partner cell's produced artifact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

METRICS = ("spec", "gent", "genr", "genm")


@dataclass(frozen=True)
class Table3Row:
    row_id: int
    label: str
    kind: str
    task_diversity: str | None = None
    model_diversity: str | None = None
    skill_source: str | None = None
    anchor_only: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


TABLE3_ROWS: tuple[Table3Row, ...] = (
    Table3Row(1, "Base Skill", "baseline", skill_source="default_skill"),
    Table3Row(2, "No skill", "baseline", skill_source="description_only"),
    Table3Row(3, "T1 x Single", "produce", "T1", "Single"),
    Table3Row(4, "T1 x Cross", "produce", "T1", "Cross"),
    Table3Row(5, "T2 x Single", "produce", "T2", "Single"),
    Table3Row(6, "T2 x Cross", "produce", "T2", "Cross"),
    Table3Row(7, "T3 x Single", "produce", "T3", "Single", anchor_only=True),
    Table3Row(8, "T3 x Cross", "produce", "T3", "Cross", anchor_only=True),
)


@dataclass(frozen=True)
class EvalUnit:
    """One planned eval unit before trial expansion."""

    row_id: int
    row_label: str
    metric: str
    source_cell_id: str
    source_artifact_cell_id: str
    target_cell_id: str
    task_id: str
    eval_model_role: str

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


def row_by_id(row_id: int) -> Table3Row:
    for row in TABLE3_ROWS:
        if row.row_id == row_id:
            return row
    raise KeyError(f"unknown Table 3 row_id={row_id}")


def cell_lookup(cells_resolved: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["cell_id"]: c for c in cells_resolved.get("cells", [])}


def eval_units_for_cell(
    *,
    row: Table3Row,
    source_cell_id: str,
    cells_resolved: dict[str, Any],
    metrics: tuple[str, ...] = METRICS,
) -> list[EvalUnit]:
    """Return eval units for one Table 3 row and source cell.

    Semantics:
    * Spec: source artifact from source cell, val tasks from source cell.
    * GenT: source artifact from source cell, test tasks from source cell.
    * GenM: same target tasks as GenT, held-out model.
    * GenR: source artifact from source cell, test tasks from anchor partner.
      Non-anchor cells produce no GenR units.
    """
    lookup = cell_lookup(cells_resolved)
    if source_cell_id not in lookup:
        raise KeyError(f"source cell not found: {source_cell_id}")
    source_cell = lookup[source_cell_id]
    out: list[EvalUnit] = []
    for metric in metrics:
        if metric not in METRICS:
            raise ValueError(f"unknown Table 3 metric: {metric!r}")
        if metric == "spec":
            target_cell_id = source_cell_id
            task_ids = source_cell.get("streams", {}).get("val", [])
            eval_model_role = "primary_eval_solver"
        elif metric == "gent":
            target_cell_id = source_cell_id
            task_ids = source_cell.get("streams", {}).get("test", [])
            eval_model_role = "primary_eval_solver"
        elif metric == "genm":
            target_cell_id = source_cell_id
            task_ids = source_cell.get("streams", {}).get("test", [])
            eval_model_role = "heldout_genm_model"
        else:
            if not source_cell.get("is_anchor"):
                continue
            target_cell_id = source_cell.get("anchor_partner") or ""
            if target_cell_id not in lookup:
                raise ValueError(
                    f"anchor cell {source_cell_id!r} has missing partner {target_cell_id!r}"
                )
            task_ids = lookup[target_cell_id].get("streams", {}).get("test", [])
            eval_model_role = "primary_eval_solver"

        for task_id in task_ids:
            out.append(
                EvalUnit(
                    row_id=row.row_id,
                    row_label=row.label,
                    metric=metric,
                    source_cell_id=source_cell_id,
                    source_artifact_cell_id=source_cell_id,
                    target_cell_id=target_cell_id,
                    task_id=task_id,
                    eval_model_role=eval_model_role,
                )
            )
    return out


def build_eval_plan(
    *,
    cells_resolved: dict[str, Any],
    rows: tuple[Table3Row, ...] = TABLE3_ROWS,
    metrics: tuple[str, ...] = METRICS,
) -> list[EvalUnit]:
    lookup = cell_lookup(cells_resolved)
    units: list[EvalUnit] = []
    anchor_pair = set(cells_resolved.get("anchor_pair") or [])
    for row in rows:
        for cell_id in sorted(lookup):
            if row.anchor_only and cell_id not in anchor_pair:
                continue
            units.extend(
                eval_units_for_cell(
                    row=row,
                    source_cell_id=cell_id,
                    cells_resolved=cells_resolved,
                    metrics=metrics,
                )
            )
    return units


def assert_genr_uses_source_artifact(eval_units: list[EvalUnit]) -> None:
    """Mechanical guard against the historical GenR inversion bug."""
    for unit in eval_units:
        if unit.metric != "genr":
            continue
        if unit.source_artifact_cell_id != unit.source_cell_id:
            raise AssertionError(
                "GenR source artifact mismatch: "
                f"source={unit.source_cell_id} artifact={unit.source_artifact_cell_id}"
            )
        if unit.target_cell_id == unit.source_artifact_cell_id:
            raise AssertionError(
                "GenR target should be the anchor partner, not the source cell: "
                f"{unit.to_jsonable()}"
            )


__all__ = [
    "EvalUnit",
    "METRICS",
    "TABLE3_ROWS",
    "Table3Row",
    "assert_genr_uses_source_artifact",
    "build_eval_plan",
    "cell_lookup",
    "eval_units_for_cell",
    "row_by_id",
]
