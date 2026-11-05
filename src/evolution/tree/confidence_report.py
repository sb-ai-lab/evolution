"""What family 13 adds to the status and result documents (design §10).

Each Attempt carries a `confidence` block once its `comparison_bound` is
journaled. The block *projects* the records the lifecycle keeps
(`AttemptState.comparison`, `.calibration_result` and `.looks`): case lists
become counts, the method object and every evaluated look's published
estimates are copied unchanged, and `evidence`, `prefixes` and digests are
left out (`inspect_campaign(verify=True)` is the place to check those).
Each round carries a `cohort` block once its `finalists_bound` is
journaled, projected the same way from `RoundState.cohort`,
`.cohort_current`, `.cohort_completed`, `.global_comparisons`,
`.selection` and `.cohort_listed`.
Nothing is recomputed here: decisions are taken on published values
(ADR-0027), so the report has no second source for one.

Nothing here changes a statistic, a gate decision, an outcome or an exit
code, and nothing here reaches the editor's or the Reflector's inputs.

`campaign_projections` imports this module, so the validator raises its own
`SchemaError("invalid public projection")` rather than importing that
module's `_require`.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from evolution.tree.campaign_policy import GLOBAL_MODES
from evolution.tree.campaign_schema import (
    ESTIMATE_UNAVAILABLE,
    GATE_MODES,
    LOOK_CAVEATS,
    MAX_FAULT_ROWS,
    OWNERS,
    PARENT_SOURCES,
    TOLERANCE_SOURCES,
    AttemptState,
    CampaignSnapshot,
    RoundState,
    member_identity,
)
from evolution.tree.confidence_statistics import (
    ATTEMPT_DECISIONS,
    BOUND_NOT_REQUESTED_IN_OBSERVE,
    GLOBAL_REASONS,
    INSUFFICIENT_CALIBRATION_REPEATS,
    SELECTION_REASONS,
)
from evolution.tree.storage.envelope import SchemaError, is_identifier

#: Design §10, frozen §9.4: the per-Attempt block.
CONFIDENCE_REPORT_KEYS = frozenset(
    {
        "mode",
        "parent_source",
        "parent_measurement_id",
        "focus_cases",
        "causal_cases",
        "retained",
        "repeats",
        "looks",
        "confidence",
        "tolerance",
        "method",
        "calibration",
        "evaluated",
    }
)
#: One `comparison_look_evaluated`, as reported: the record without
#: `round`, `attempt_id`, `prefixes` and `evidence`.
EVALUATED_KEYS = frozenset(
    {
        "look",
        "comparison",
        "final",
        "focus",
        "causal",
        "overall",
        "tolerance",
        "decision",
        "reason",
        "would_pass_point",
        "caveats",
    }
)
#: Design §10: the per-round block, `null` before the round's `finalists_bound`.
COHORT_REPORT_KEYS = frozenset(
    {
        "mode",
        "global",
        "members",
        "contrasts",
        "selection",
        "next_baseline_measurement_id",
        "listed",
        "condition_changed_in_place",
    }
)
#: One member: its current id, its role, and its `cohort_measurement_completed`
#: facts (`null` until that record).
_MEMBER_KEYS = frozenset(
    {"measurement_id", "role", "attempt_id", "complete", "point", "missing", "critical"}
)
_COMPLETION_KEYS = ("complete", "point", "missing", "critical")
#: One `global_comparison_completed`'s published values, plus the flag of choice 11.
_CONTRAST_VALUES = (
    "point",
    "lower",
    "level",
    "unavailable",
    "pairing",
    "eligible",
    "reason",
)
_CONTRAST_KEYS = frozenset({"attempt_id", *_CONTRAST_VALUES, "condition_changed_in_place"})
_SELECTION_KEYS = ("reason", "attempt_id", "measurement_id")
#: One `cohort_rows` row, as the suspension's evidence lists it (ADR-0019 as
#: amended 2026-10-06: with its `retry_ordinal`).
_LISTED_KEYS = frozenset({"measurement_id", "case_id", "sample_id", "retry_ordinal", "owner"})
_CONTRAST_UNAVAILABLE = frozenset({None, BOUND_NOT_REQUESTED_IN_OBSERVE, "cohort_incomplete"})
_ESTIMATE_KEYS = frozenset({"point", "lower", "upper", "level", "unavailable"})
_METHOD_KEYS = frozenset({"draws", "noise_level", "quantile", "version"})
_CALIBRATION_KEYS = ("allowance", "degenerate", "unavailable")


def _plain(value: Any) -> Any:
    """A JSON value copied out of the snapshot, so the document never aliases it."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _evaluated(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _plain(entry[name]) for name in sorted(EVALUATED_KEYS)}


def attempt_confidence_entry(attempt: AttemptState) -> dict[str, Any] | None:
    """The design §10 facts of one Attempt, or `None` before its `comparison_bound`.

    The block's fields are the governing comparison's. `evaluated` holds every
    evaluated look in look order, each with the `comparison` it cites: a look
    decided under a comparison a `continue` later superseded stays decided
    (G11), so it stays in the report.
    """

    comparison = attempt.comparison
    if comparison is None:
        return None
    retained = comparison["retained"]
    result = attempt.calibration_result
    return {
        "mode": comparison["mode"],
        "parent_source": comparison["parent_source"],
        "parent_measurement_id": comparison["parent_measurement_id"],
        "focus_cases": len(comparison["focus"]),
        "causal_cases": len(comparison["causal"]),
        "retained": {"focus": len(retained["focus"]), "causal": len(retained["causal"])},
        "repeats": comparison["repeats"],
        "looks": _plain(comparison["looks"]),
        "confidence": _plain(comparison["confidence"]),
        "tolerance": _plain(comparison["tolerance"]),
        "method": _plain(comparison["method"]),
        "calibration": (
            None if result is None else {name: result[name] for name in _CALIBRATION_KEYS}
        ),
        "evaluated": [_evaluated(attempt.looks[look]) for look in sorted(attempt.looks)],
    }


def _changed_in_place(snapshot: CampaignSnapshot, number: int, after: int, before: float) -> bool:
    """Whether an applied `continue` of round `number` lies strictly between two seqs."""

    return any(
        change["effect"] == "continue"
        and change["round"] == number
        and after < change["seq"] < before
        for change in snapshot.condition_changes
    )


def _member(state: RoundState, position: int, measurement_id: str) -> dict[str, Any]:
    cohort = state.cohort
    assert cohort is not None
    completion = state.cohort_completed.get(measurement_id)
    role, attempt_id = member_identity(cohort, position)
    return {
        "measurement_id": measurement_id,
        "role": role,
        "attempt_id": attempt_id,
        **{name: None if completion is None else completion[name] for name in _COMPLETION_KEYS},
    }


def round_cohort_entry(snapshot: CampaignSnapshot, number: int) -> dict[str, Any] | None:
    """The design §10 cohort facts of round `number`, or `None` without `finalists_bound`.

    Members are the current ids (`RoundState.cohort_current`, plan choice 9),
    Champion first, with roles and Attempt ids from the binding; their
    completeness, point, missing count and critical flag come from each
    member's `cohort_measurement_completed`, never from the registry's
    `complete` flag, and are `null` until it. Contrasts follow the finalist
    order, one per `global_comparison_completed` written. `listed` is the
    round's latest `cohort_incomplete` listing (`RoundState.cohort_listed`,
    choice 10). `condition_changed_in_place` marks an applied `continue` of
    this round after the binding; a contrast carries it when that `continue`
    precedes the contrast (choice 11).
    """

    state = snapshot.rounds[number]
    cohort = state.cohort
    if cohort is None:
        return None
    bound = int(cohort["seq"])
    current = state.cohort_current if state.cohort_current is not None else ()
    contrasts = []
    for finalist in cohort["finalists"]:
        contrast = state.global_comparisons.get(str(finalist["attempt_id"]))
        if contrast is None:
            continue
        contrasts.append(
            {
                "attempt_id": contrast["attempt_id"],
                **{name: contrast[name] for name in _CONTRAST_VALUES},
                "condition_changed_in_place": _changed_in_place(
                    snapshot, number, bound, int(contrast["seq"])
                ),
            }
        )
    selection = state.selection
    closed = state.closed
    # Design §6.12: the next round's baseline is the selection's measurement
    # when the round closed `promoted` or `retained`.
    next_baseline = (
        selection["measurement_id"]
        if selection is not None
        and closed is not None
        and closed["outcome"] in ("promoted", "retained")
        else None
    )
    return {
        "mode": cohort["mode"],
        "global": cohort["global"],
        "members": [_member(state, position, name) for position, name in enumerate(current)],
        "contrasts": contrasts,
        "selection": (
            None if selection is None else {name: selection[name] for name in _SELECTION_KEYS}
        ),
        "next_baseline_measurement_id": next_baseline,
        "listed": [_plain(row) for row in state.cohort_listed],
        "condition_changed_in_place": _changed_in_place(snapshot, number, bound, float("inf")),
    }


# -- validation ------------------------------------------------------------------


def _require(condition: bool) -> None:
    if not condition:
        raise SchemaError("invalid public projection")


def _keys(value: Any, names: frozenset[str]) -> None:
    _require(type(value) is dict and set(value) == names)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(value)


def _count(value: Any, *, least: int = 0) -> None:
    _require(type(value) is int and value >= least)


def _optional_number(value: Any) -> None:
    _require(value is None or _number(value))


def _increasing(values: list[int]) -> bool:
    return all(earlier < later for earlier, later in zip(values, values[1:], strict=False))


def _estimate(value: Any) -> None:
    _keys(value, _ESTIMATE_KEYS)
    for name in ("point", "lower", "upper", "level"):
        _optional_number(value[name])
    _require(value["unavailable"] is None or value["unavailable"] in ESTIMATE_UNAVAILABLE)


def _look(entry: Any) -> None:
    _keys(entry, EVALUATED_KEYS)
    _count(entry["look"], least=1)
    _count(entry["comparison"], least=1)
    _require(type(entry["final"]) is bool)
    _estimate(entry["focus"])
    for name in ("causal", "overall"):
        if entry[name] is not None:
            _estimate(entry[name])
    tolerance = entry["tolerance"]
    _keys(tolerance, frozenset({"source", "value"}))
    _require(tolerance["source"] in TOLERANCE_SOURCES)
    _optional_number(tolerance["value"])
    decision, reason = entry["decision"], entry["reason"]
    _require(
        type(decision) is str
        and decision in ATTEMPT_DECISIONS
        and (reason is None or type(reason) is str)
        and reason in ATTEMPT_DECISIONS[decision]
    )
    _require(entry["would_pass_point"] is None or type(entry["would_pass_point"]) is bool)
    caveats = entry["caveats"]
    _require(type(caveats) is list and all(type(kind) is str for kind in caveats))
    _require(set(caveats) <= LOOK_CAVEATS and caveats == sorted(set(caveats)))


def validate_confidence_block(block: Any) -> None:
    """The closed shape of a non-null `confidence` block (design §10).

    A shape check only: whether the values follow from the records is the
    lifecycle's and the audit's, never re-derived from a report.
    """

    _keys(block, CONFIDENCE_REPORT_KEYS)
    mode = block["mode"]
    _require(type(mode) is str and mode in GATE_MODES)
    _require(type(block["parent_source"]) is str and block["parent_source"] in PARENT_SOURCES)
    parent = block["parent_measurement_id"]
    _require(parent is None or (type(parent) is str and is_identifier(parent)))
    _count(block["focus_cases"])
    _count(block["causal_cases"])
    retained = block["retained"]
    _keys(retained, frozenset({"focus", "causal"}))
    for scope in ("focus", "causal"):
        _count(retained[scope])
        _require(retained[scope] <= block[f"{scope}_cases"])
    _count(block["repeats"], least=1)
    looks = block["looks"]
    _require(type(looks) is list and bool(looks))
    for look in looks:
        _count(look, least=1)
    _require(_increasing(looks))
    confidence = block["confidence"]
    if mode == "observe":
        _require(confidence is None)
    else:
        _keys(confidence, frozenset({"focus", "causal"}))
        _require(all(_number(level) and 0 < level < 1 for level in confidence.values()))
    tolerance = block["tolerance"]
    _require(type(tolerance) is dict)
    if tolerance.get("kind") == "numeric":
        _keys(tolerance, frozenset({"kind", "value"}))
        _require(_number(tolerance["value"]) and tolerance["value"] >= 0)
    else:
        _require(tolerance == {"kind": "measured_noise"})
    method = block["method"]
    _keys(method, _METHOD_KEYS)
    _count(method["draws"], least=1)
    _require(_number(method["noise_level"]))
    _require(type(method["quantile"]) is str and type(method["version"]) is str)
    calibration = block["calibration"]
    if calibration is not None:
        _keys(calibration, frozenset(_CALIBRATION_KEYS))
        _optional_number(calibration["allowance"])
        _require(type(calibration["degenerate"]) is bool)
        _require(calibration["unavailable"] in (None, INSUFFICIENT_CALIBRATION_REPEATS))
    evaluated = block["evaluated"]
    _require(type(evaluated) is list)
    for entry in evaluated:
        _look(entry)
    _require(_increasing([entry["look"] for entry in evaluated]))


def _identifier(value: Any) -> None:
    _require(type(value) is str and is_identifier(value))


def _validate_member(member: Any, position: int) -> None:
    _keys(member, _MEMBER_KEYS)
    _identifier(member["measurement_id"])
    if position == 0:
        _require(member["role"] == "champion" and member["attempt_id"] is None)
    else:
        _require(member["role"] == "finalist")
        _identifier(member["attempt_id"])
    complete = member["complete"]
    if complete is None:
        _require(all(member[name] is None for name in _COMPLETION_KEYS))
        return
    _require(type(complete) is bool and type(member["critical"]) is bool)
    _count(member["missing"])
    # Design §6.9: a complete member has a point and no missing pair; an
    # incomplete one (D7, a finalist only) names its missing pairs, no point.
    if complete:
        _require(_number(member["point"]) and member["missing"] == 0)
    else:
        _require(position > 0 and member["point"] is None and member["missing"] > 0)


def _validate_contrast(contrast: Any) -> None:
    _keys(contrast, _CONTRAST_KEYS)
    _identifier(contrast["attempt_id"])
    for name in ("point", "lower", "level"):
        _optional_number(contrast[name])
    _require(contrast["unavailable"] in _CONTRAST_UNAVAILABLE)
    _require(contrast["pairing"] in ("exact", "unpaired"))
    eligible = contrast["eligible"]
    _require(type(eligible) is bool and contrast["reason"] in GLOBAL_REASONS[eligible])
    _require(type(contrast["condition_changed_in_place"]) is bool)


def _validate_listed(row: Any) -> None:
    _keys(row, _LISTED_KEYS)
    _identifier(row["measurement_id"])
    _require(type(row["case_id"]) is str and type(row["sample_id"]) is str)
    _count(row["retry_ordinal"])
    _require(row["owner"] in OWNERS)


def validate_cohort_block(block: Any) -> None:
    """The closed shape of a non-null round `cohort` block (design §10).

    A shape check only, as `validate_confidence_block`: whether the values
    follow from the records is the lifecycle's and the audit's.
    """

    _keys(block, COHORT_REPORT_KEYS)
    _require(block["mode"] in GATE_MODES and block["global"] in GLOBAL_MODES)
    members = block["members"]
    _require(type(members) is list and len(members) >= 2)
    for position, member in enumerate(members):
        _validate_member(member, position)
    names = [member["measurement_id"] for member in members]
    finalists = [member["attempt_id"] for member in members[1:]]
    _require(len(set(names)) == len(names) and len(set(finalists)) == len(finalists))
    contrasts = block["contrasts"]
    _require(type(contrasts) is list)
    for contrast in contrasts:
        _validate_contrast(contrast)
    places = [
        finalists.index(c["attempt_id"]) if c["attempt_id"] in finalists else -1 for c in contrasts
    ]
    _require(all(place >= 0 for place in places) and _increasing(places))
    changed = block["condition_changed_in_place"]
    _require(type(changed) is bool)
    _require(changed or not any(c["condition_changed_in_place"] for c in contrasts))
    selection = block["selection"]
    if selection is not None:
        _keys(selection, frozenset(_SELECTION_KEYS))
        _require(type(selection["reason"]) is str and selection["reason"] in SELECTION_REASONS)
        _require(selection["attempt_id"] is None or selection["attempt_id"] in finalists)
        _require(selection["measurement_id"] in names)
    following = block["next_baseline_measurement_id"]
    _require(
        following is None or (selection is not None and following == selection["measurement_id"])
    )
    listed = block["listed"]
    _require(type(listed) is list and len(listed) <= MAX_FAULT_ROWS)
    for row in listed:
        _validate_listed(row)
