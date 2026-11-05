"""Row requirements used by the current historical-closure audit.

The audit retains these selections to prove infrastructure-failed closes over
pre-fix evidence. The retired family-12 gate evaluator is not retained.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evolution.tree.campaign_schema import AffectedLayers
from evolution.tree.campaign_statistics import (
    SampleRow,
    exclude_pairs,
)


@dataclass(frozen=True)
class LookSides:
    """The rows one family-12 look reads, each side named by its measurement."""

    baseline_id: str
    baseline: Sequence[SampleRow]
    candidate_id: str
    candidate: Sequence[SampleRow]
    parent_id: str | None
    parent: Sequence[SampleRow]
    excluded: frozenset[tuple[str, int]]


def _owed(
    cases: Sequence[str], look: int, excluded: frozenset[tuple[str, int]]
) -> set[tuple[str, int]]:
    return {
        (case_id, repeat)
        for case_id in cases
        for repeat in range(1, look + 1)
        if (case_id, repeat) not in excluded
    }


def _requirements(
    layers: AffectedLayers, sides: LookSides, look: int
) -> list[tuple[str, Sequence[SampleRow], set[tuple[str, int]]]]:
    """Each side's owed `(case_id, repeat)` pairs: the union over the layers it enters.

    Excluded baseline pairs leave the layers that read baseline rows (layer 2,
    global, and layer 1 without a fresh Parent) and no other.
    """

    fresh = sides.parent_id is not None
    none: frozenset[tuple[str, int]] = frozenset()
    baseline: set[tuple[str, int]] = _owed(layers.layer_two, look, sides.excluded)
    candidate: set[tuple[str, int]] = set(baseline)
    if layers.global_layer:
        corpus = _owed(layers.corpus, look, sides.excluded)
        baseline |= corpus
        candidate |= corpus
    if fresh:
        first = _owed(layers.layer_one, look, none)
        candidate |= first
    else:
        first = _owed(layers.layer_one, look, sides.excluded)
        baseline |= first
        candidate |= first
    owed = [
        (sides.baseline_id, sides.baseline, baseline),
        (sides.candidate_id, sides.candidate, candidate),
    ]
    if fresh:
        assert sides.parent_id is not None
        owed.append((sides.parent_id, sides.parent, _owed(layers.layer_one, look, none)))
    return owed


def gate_input_rows(
    layers: AffectedLayers | None, sides: LookSides, look: int
) -> tuple[tuple[str, tuple[SampleRow, ...]], ...]:
    """Each side's rows the look's gate reads, by measurement (ADR-0024, ADR-0019).

    Family 12 (`layers` given): exactly the pairs `affected_gate_at_look`
    consumes -- layer 1 compares the Candidate with the fresh Parent (or,
    without one, the baseline) on the layer-1 cases, layer 2 the Candidate
    with the baseline on the other affected cases, and the global layer, when
    on, the two over the corpus; excluded baseline pairs leave only the layers
    that read baseline rows (`_requirements`). Families 7-11 (`layers` is
    `None`): the baseline's and the Candidate's rows up to `look`, less the
    excluded pairs, which is what their gate reads.

    The one selection the executor's historical closure, the audit's
    `_historical_closure` and the audit's out-of-range check share, so none of
    them reads a row the gate never consumed.
    """

    if layers is None:
        return (
            (sides.baseline_id, exclude_pairs(_upto(sides.baseline, look), sides.excluded)),
            (sides.candidate_id, exclude_pairs(_upto(sides.candidate, look), sides.excluded)),
        )
    return tuple(
        (
            measurement_id,
            tuple(row for row in rows if row.repeat <= look and (row.case_id, row.repeat) in owed),
        )
        for measurement_id, rows, owed in _requirements(layers, sides, look)
    )


def _upto(rows: Sequence[SampleRow], look: int) -> tuple[SampleRow, ...]:
    return tuple(row for row in rows if row.repeat <= look)
