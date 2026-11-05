"""The family-14 Reflector's whole-run aggregate.

A pure function of one `FactRow` per Attempt and one `RoundRow` per closed
round. It reports what happened together, never what caused it: counts,
closed codes, ids, visible paths, change fingerprints and their bounded
descriptions. Every list has one total order, so permuting the rows changes no
byte; every list is capped by a named constant and its `*_total` counts what
the cap hid. A deterministic guard drops lowest-ranked entries if a form ever
exceeds its maximum, which the field bounds rule out.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.reflector.reflect_change import PathChange, path_display
from evolution.tree.roles.reflector_contracts import (
    AGGREGATE_FULL_LIMITS,
    AGGREGATE_MAX_CHARS,
    AGGREGATE_REDUCED_LIMITS,
    AGGREGATE_REDUCED_MAX_CHARS,
    CHANGE_FINGERPRINT_SCHEME,
    REPEAT_MIN_ROUNDS,
)

__all__ = [
    "AGGREGATE_VERSION",
    "AggregateForms",
    "FactRow",
    "RoundRow",
    "aggregate_forms",
    "build_aggregate",
    "core_aggregate",
    "round_range",
    "serialized_chars",
]

AGGREGATE_VERSION = "reflect_history_aggregate/1"
_PRESENT = "present"
_PATH_TIERS = (
    "repeated_regression",
    "repeated_regression_distinct",
    "promoted",
    "promoted_and_regressed",
    "repeated_no_promotion",
    "single",
)
_CHANGE_TIERS = {"repeated_regression": 0, "promoted": 1, "promoted_and_regressed": 2}
_GUARD_ORDER = ("paths", "reasons", "gains", "promotions")


@dataclass(frozen=True)
class RoundRow:
    """One closed round: `round_closed`'s outcome and reason, the selected
    Attempt, the diagnosis status (`present`, `absent`, `unavailable`; a round
    that never opened a reflection is `absent`) and whether a `diagnosis:`
    record exists for it."""

    round: int
    outcome: str
    reason: str | None
    selected_attempt: str | None
    diagnosis: str
    diagnosed: bool


@dataclass(frozen=True)
class FactRow:
    """One Attempt of a closed round. `paths` are the visible
    changed paths within the 32-path cap, `changes` the aligned fingerprints."""

    round: int
    attempt_id: str
    index: int
    outcome: str | None
    reason: str | None
    code: str | None
    promoted: bool
    measurement: str
    exam_mean_delta: float | None
    rest_mean_delta: float | None
    changes_status: str
    paths: tuple[str, ...] = ()
    changes: tuple[PathChange, ...] = ()

    def __post_init__(self) -> None:
        if len(self.paths) != len(self.changes):
            raise ValueError("changes must align with paths")

    @property
    def gained(self) -> bool:
        return (
            self.measurement == _PRESENT
            and self.exam_mean_delta is not None
            and self.exam_mean_delta > 0
        )

    @property
    def regressed(self) -> bool:
        return (
            self.measurement == _PRESENT
            and self.exam_mean_delta is not None
            and self.exam_mean_delta < 0
        )


@dataclass(frozen=True)
class AggregateForms:
    full: Mapping[str, Any]
    reduced: Mapping[str, Any]
    core: Mapping[str, Any]

    def form(self, name: str) -> Mapping[str, Any]:
        return {"full": self.full, "reduced": self.reduced, "core": self.core}[name]


def serialized_chars(value: object) -> int:
    return len(canonical_json(value).decode("utf-8"))


def round_range(numbers: Sequence[int]) -> dict[str, int | None]:
    if not numbers:
        return {"count": 0, "first": None, "last": None}
    return {"count": len(numbers), "first": min(numbers), "last": max(numbers)}


def _nulls_first(value: str | None) -> tuple[bool, str]:
    return (value is not None, "" if value is None else value)


def _newest(rows: Sequence[FactRow]) -> FactRow:
    return max(rows, key=lambda row: (row.round, row.index))


def _ref(row: FactRow) -> str:
    return f"attempt:{row.attempt_id}"


def _tally(rows: Sequence[FactRow]) -> dict[str, int]:
    rounds = {row.round for row in rows}
    return {
        "rounds": len(rounds),
        "attempts": len(rows),
        "measured": sum(row.measurement == _PRESENT for row in rows),
        "promoted": sum(row.promoted for row in rows),
        "gained": sum(row.gained for row in rows),
        "regressed_rounds": len({row.round for row in rows if row.regressed}),
        "last_round": max(rounds),
    }


def _change_pattern(tally: Mapping[str, int]) -> str:
    if (
        tally["promoted"] == 0
        and tally["regressed_rounds"] >= REPEAT_MIN_ROUNDS
        and 2 * tally["regressed_rounds"] > tally["rounds"]
    ):
        return "repeated_regression"
    if tally["promoted"] >= 1:
        return "promoted" if tally["regressed_rounds"] == 0 else "promoted_and_regressed"
    if tally["rounds"] >= REPEAT_MIN_ROUNDS:
        return "repeated_no_promotion"
    return "single"


def _path_pattern(tally: Mapping[str, int], changes: Sequence[Mapping[str, Any]]) -> str:
    if any(change["pattern"] == "repeated_regression" for change in changes):
        return "repeated_regression"
    regressed_changes = sum(change["regressed_rounds"] >= 1 for change in changes)
    if (
        tally["promoted"] == 0
        and regressed_changes >= REPEAT_MIN_ROUNDS
        and 2 * tally["regressed_rounds"] > tally["rounds"]
    ):
        return "repeated_regression_distinct"
    if tally["promoted"] >= 1:
        return "promoted" if tally["regressed_rounds"] == 0 else "promoted_and_regressed"
    if tally["rounds"] >= REPEAT_MIN_ROUNDS:
        return "repeated_no_promotion"
    return "single"


def _witnesses(rows: Sequence[FactRow], cap: int) -> list[dict[str, Any]]:
    chosen: list[tuple[FactRow, str]] = []
    taken: set[str] = set()

    def take(row: FactRow, mark: str) -> None:
        chosen.append((row, mark))
        taken.add(row.attempt_id)

    promoted = [row for row in rows if row.promoted]
    if promoted and cap > 0:
        newest = _newest(promoted)
        take(newest, "promoted_and_regressed" if newest.regressed else "promoted")
    for number in sorted({row.round for row in rows if row.regressed}, reverse=True):
        if len(chosen) >= cap:
            break
        candidates = [
            row
            for row in rows
            if row.round == number and row.regressed and row.attempt_id not in taken
        ]
        if candidates:
            take(max(candidates, key=lambda row: row.index), "regressed")
    witnessed = {row.round for row, _ in chosen}
    for number in sorted({row.round for row in rows} - witnessed, reverse=True):
        if len(chosen) >= cap:
            break
        candidates = [row for row in rows if row.round == number and row.attempt_id not in taken]
        if candidates:
            take(max(candidates, key=lambda row: row.index), "other")
    chosen.sort(key=lambda item: (-item[0].round, item[0].index))
    return [{"round": row.round, "attempt": _ref(row), "mark": mark} for row, mark in chosen]


def _pointer(rows: Sequence[FactRow]) -> dict[str, Any] | None:
    if not rows:
        return None
    newest = _newest(rows)
    return {"round": newest.round, "attempt": _ref(newest)}


def _path_entry(
    path: str, pairs: Sequence[tuple[FactRow, PathChange]], limits: Mapping[str, int]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    rows = [row for row, _ in pairs]
    tally = _tally(rows)
    by_fingerprint: dict[str, list[tuple[FactRow, PathChange]]] = defaultdict(list)
    unfingerprinted = 0
    for row, fact in pairs:
        if fact.fingerprint is None:
            unfingerprinted += 1
        else:
            by_fingerprint[fact.fingerprint].append((row, fact))
    changes: list[dict[str, Any]] = []
    for fingerprint, members in by_fingerprint.items():
        member_rows = [row for row, _ in members]
        member_tally = _tally(member_rows)
        newest_row = _newest(member_rows)
        description = next(fact.change for row, fact in members if row is newest_row)
        changes.append(
            {
                "fingerprint": fingerprint,
                "pattern": _change_pattern(member_tally),
                "change": dict(description or {}),
                "rounds": member_tally["rounds"],
                "attempts": member_tally["attempts"],
                "measured": member_tally["measured"],
                "gained": member_tally["gained"],
                "promoted": member_tally["promoted"],
                "regressed_rounds": member_tally["regressed_rounds"],
                "last_round": member_tally["last_round"],
                "witnesses": _witnesses(member_rows, limits["change_witnesses"]),
            }
        )
    pattern = _path_pattern(tally, changes)
    changes.sort(
        key=lambda entry: (
            _CHANGE_TIERS.get(entry["pattern"], 3),
            -entry["regressed_rounds"],
            -entry["rounds"],
            -entry["last_round"],
            entry["fingerprint"],
        )
    )
    rounds_desc = sorted({row.round for row in rows}, reverse=True)
    entry = {
        **path_display(path),
        "pattern": pattern,
        "rounds": tally["rounds"],
        "attempts": tally["attempts"],
        "measured": tally["measured"],
        "promoted": tally["promoted"],
        "gained": tally["gained"],
        "regressed_rounds": tally["regressed_rounds"],
        "first_round": min(rounds_desc),
        "last_round": tally["last_round"],
        "recent_rounds": rounds_desc[: limits["recent_rounds"]],
        "last_attempt": _ref(_newest(rows)),
        "regressed_witness": _pointer([row for row in rows if row.regressed]),
        "promoted_witness": _pointer([row for row in rows if row.promoted]),
        "changes": changes[: limits["changes"]],
        "changes_total": len(changes),
        "changes_unfingerprinted": unfingerprinted,
    }
    key = (_PATH_TIERS.index(pattern), -tally["rounds"], -tally["last_round"], path)
    return key, entry


def _guard(aggregate: dict[str, Any], max_chars: int) -> dict[str, Any]:
    while serialized_chars(aggregate) > max_chars:
        for name in _GUARD_ORDER:
            if aggregate[name]:
                aggregate[name] = aggregate[name][:-1]
                break
        else:
            break
    return aggregate


def build_aggregate(
    rounds: Sequence[RoundRow],
    rows: Sequence[FactRow],
    *,
    limits: Mapping[str, int] = AGGREGATE_FULL_LIMITS,
    form: str = "full",
    max_chars: int = AGGREGATE_MAX_CHARS,
) -> dict[str, Any]:
    diagnosed = {entry.round for entry in rounds if entry.diagnosed}

    def details(row: FactRow) -> list[str]:
        return [_ref(row), *([f"diagnosis:{row.round}"] if row.round in diagnosed else [])]

    counted = Counter((entry.outcome, entry.reason) for entry in rounds)
    round_outcomes = [
        {"outcome": outcome, "reason": reason, "count": count}
        for (outcome, reason), count in sorted(
            counted.items(), key=lambda item: (-item[1], item[0][0], _nulls_first(item[0][1]))
        )
    ]
    promoted = sorted(
        (row for row in rows if row.promoted), key=lambda row: (-row.round, row.index)
    )
    gains = sorted(
        (row for row in rows if row.gained and not row.promoted),
        key=lambda row: (-(row.exam_mean_delta or 0.0), -row.round, row.index),
    )
    grouped: dict[tuple[str | None, str | None, str | None], list[FactRow]] = defaultdict(list)
    for row in rows:
        if not row.promoted:
            grouped[(row.outcome, row.reason, row.code)].append(row)
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for (outcome, reason, code), members in grouped.items():
        newest = _newest(members)
        rounds_count = len({member.round for member in members})
        ranked.append(
            (
                (
                    -len(members),
                    -rounds_count,
                    -newest.round,
                    _nulls_first(outcome),
                    _nulls_first(reason),
                    _nulls_first(code),
                ),
                {
                    "outcome": outcome,
                    "reason": reason,
                    "code": code,
                    "attempts": len(members),
                    "rounds": rounds_count,
                    "last_round": newest.round,
                    "last_attempt": _ref(newest),
                },
            )
        )
    reasons = [entry for _, entry in sorted(ranked, key=lambda item: item[0])]
    by_path: dict[str, list[tuple[FactRow, PathChange]]] = defaultdict(list)
    for row in rows:
        for path, fact in zip(row.paths, row.changes, strict=True):
            by_path[path].append((row, fact))
    paths = [
        entry
        for _, entry in sorted(
            (_path_entry(p, pairs, limits) for p, pairs in by_path.items()),
            key=lambda item: item[0],
        )
    ]
    aggregate: dict[str, Any] = {
        "version": AGGREGATE_VERSION,
        "form": form,
        "fingerprint_scheme": CHANGE_FINGERPRINT_SCHEME,
        "limits": dict(limits),
        "rounds": round_range([entry.round for entry in rounds]),
        "attempts": {
            "opened": len(rows),
            "measured": sum(row.measurement == _PRESENT for row in rows),
            "measurement_unavailable": sum(row.measurement == "unavailable" for row in rows),
            "changes_unavailable": sum(row.changes_status == "unavailable" for row in rows),
            "promoted": len(promoted),
        },
        "round_outcomes": round_outcomes[: limits["round_outcomes"]],
        "diagnoses": {
            status: sum(entry.diagnosis == status for entry in rounds)
            for status in ("present", "absent", "unavailable")
        },
        "promotions": [
            {
                "round": row.round,
                "attempt_id": row.attempt_id,
                "exam_mean_delta": row.exam_mean_delta,
                "rest_mean_delta": row.rest_mean_delta,
                "changed_paths": [path_display(p) for p in row.paths[: limits["promotion_paths"]]],
                "changed_paths_truncated": len(row.paths) > limits["promotion_paths"],
                "details": details(row),
            }
            for row in promoted[: limits["promotions"]]
        ],
        "promotions_total": len(promoted),
        "gains": [
            {
                "round": row.round,
                "attempt_id": row.attempt_id,
                "outcome": row.outcome,
                "reason": row.reason,
                "exam_mean_delta": row.exam_mean_delta,
                "rest_mean_delta": row.rest_mean_delta,
                "details": details(row),
            }
            for row in gains[: limits["gains"]]
        ],
        "gains_total": len(gains),
        "reasons": reasons[: limits["reasons"]],
        "reasons_total": len(reasons),
        "paths": paths[: limits["paths"]],
        "paths_total": len(paths),
    }
    return _guard(aggregate, max_chars)


def core_aggregate(full: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only. Its `limits` are the reduced caps (routine)."""

    return {
        "version": AGGREGATE_VERSION,
        "form": "core",
        "limits": dict(AGGREGATE_REDUCED_LIMITS),
        "rounds": dict(full["rounds"]),
        "attempts": dict(full["attempts"]),
        "round_outcomes": list(full["round_outcomes"])[
            : AGGREGATE_REDUCED_LIMITS["round_outcomes"]
        ],
        "diagnoses": dict(full["diagnoses"]),
    }


def aggregate_forms(
    rounds: Sequence[RoundRow],
    rows: Sequence[FactRow],
    *,
    max_chars: int = AGGREGATE_MAX_CHARS,
    reduced_max_chars: int = AGGREGATE_REDUCED_MAX_CHARS,
) -> AggregateForms:
    full = build_aggregate(rounds, rows, max_chars=max_chars)
    reduced = build_aggregate(
        rounds, rows, limits=AGGREGATE_REDUCED_LIMITS, form="reduced", max_chars=reduced_max_chars
    )
    return AggregateForms(full, reduced, core_aggregate(full))
