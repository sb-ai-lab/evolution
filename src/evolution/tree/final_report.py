"""What family 14 adds to the status and result documents (F0 M4, M5; R22-R25, R34, R35).

Family 13's blocks live in `confidence_report`, whose constants are inside the
frozen family-13 registry digest, so family 14's own key sets, builders and
validators live here (H12b plan choice 1). This module imports that one's
pure helpers and frozen key sets and never edits them.

Each family-14 Attempt carries a `confidence` block: family 13's thirteen
keys plus the bound `margin` (R23). Its evaluated looks are copied unchanged,
so `focus` and a non-null `causal` estimate carry `exact` (R22) while
`overall` keeps family 13's five keys.

Each family-14 round carries a `final` list (M5): `[]` before its first
`finalists_bound`, else one entry per binding in `RoundState.finals`, in
journal order. Under `all_attempts` that is one entry holding every
finalist; under `first_accepted` one entry per finalist. Each entry has
family 13's `cohort` fields, a contrast says `accepted` where family 13 says
`eligible` (M4), and an entry adds its `ordinal`, the binding's `stability`,
its `champion_side` and, under `stable`, the stored Champion measurement
(plan choices 4-8).

Family-14 status, result and inspection also carry three top-level blocks,
each a pure function of the snapshot: `roles` (M7, choice 9), the kinds of
the Grouper, the Reflector and the Editor in force; `settings` (choice 10),
the policy's `stability`, `round_stop` and baseline refresh with its derived
count (R42); and `connection` (C§6.3, choice 11), the latest preflight.

As in family 13, nothing is recomputed here: decisions are taken on
published values, so the report has no second source for one. Nothing here
changes a statistic, a gate decision, an outcome or an exit code, and nothing
here reaches the editor's or the Reflector's inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evolution.tree.campaign_epochs import epoch_of, family14_next_baseline, refresh_count
from evolution.tree.campaign_policy import GLOBAL_MODES, ROUND_STOPS, STABILITIES
from evolution.tree.campaign_schema import (
    GATE_MODES,
    LOOK_CAVEATS14,
    MAX_FAULT_ROWS,
    TOLERANCE_SOURCES,
    AttemptState,
    CampaignSnapshot,
    RoundState,
    member_identity,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.confidence_report import (
    _COMPLETION_KEYS,
    _SELECTION_KEYS,
    COHORT_REPORT_KEYS,
    CONFIDENCE_REPORT_KEYS,
    EVALUATED_KEYS,
    _changed_in_place,
    _count,
    _identifier,
    _increasing,
    _keys,
    _number,
    _optional_number,
    _plain,
    _require,
    _validate_listed,
    _validate_member,
    attempt_confidence_entry,
    validate_confidence_block,
)
from evolution.tree.confidence_statistics import SELECTION_REASONS
from evolution.tree.final_statistics import ATTEMPT_DECISIONS14, GLOBAL_REASONS14, UNAVAILABLE14
from evolution.tree.target.connection_probe import CAUSE_CODES, DETAIL_CODES, MAX_PATH_BYTES

#: Plan choice 3: family 13's per-Attempt keys plus the bound margin (R23).
CONFIDENCE_REPORT_KEYS14 = CONFIDENCE_REPORT_KEYS | {"margin"}
#: R22: a family-14 `focus` or `causal` estimate; `overall` keeps five keys.
ESTIMATE_KEYS14 = frozenset({"point", "lower", "upper", "level", "unavailable", "exact"})
_OVERALL_KEYS = ESTIMATE_KEYS14 - {"exact"}
#: M5, plan choice 4: one `final` entry -- family 13's `cohort` fields plus these.
FINAL_REPORT_KEYS = COHORT_REPORT_KEYS | {
    "ordinal",
    "stability",
    "champion_side",
    "stored_measurement_id",
}
#: One `global_comparison_completed`'s published values, `accepted` for
#: family 13's `eligible` (M4), plus `exact` and the removal counts (choice 5).
_FINAL_CONTRAST_VALUES = (
    "point",
    "lower",
    "level",
    "unavailable",
    "pairing",
    "accepted",
    "reason",
    "exact",
)
FINAL_CONTRAST_KEYS = frozenset(
    {
        "attempt_id",
        *_FINAL_CONTRAST_VALUES,
        "removed_pairs",
        "removed_cases",
        "condition_changed_in_place",
    }
)
#: M4: family 13's selection reasons with `no_eligible_finalist` renamed,
#: derived as `campaign_schema` validates a family-14 `selection_decided`.
SELECTION_REASONS14 = (SELECTION_REASONS - {"no_eligible_finalist"}) | {
    campaign_vocabulary_for(14).selection_retained_reason
}
#: A binding's stability names its Champion side (F0 §5, R31).
_CHAMPION_SIDES: Mapping[str, str] = {"unstable": "fresh", "stable": "stored"}


def attempt_confidence_entry14(attempt: AttemptState) -> dict[str, Any] | None:
    """Family 13's block of one Attempt plus its bound `margin`, or `None` before `comparison_bound`.

    The looks are copied unchanged, so their estimates keep the six keys the
    family-14 records carry.
    """

    block = attempt_confidence_entry(attempt)
    if block is None:
        return None
    comparison = attempt.comparison
    assert comparison is not None
    block["margin"] = comparison["margin"]
    return block


def removed_count(value: Any) -> int:
    """Plan choice 5: a canonical removal list or its closed `{count, digest}` summary, counted."""

    return int(value["count"]) if isinstance(value, Mapping) else len(value)


def _member14(
    state: RoundState, final: Mapping[str, Any], stability: str, position: int, name: str
) -> dict[str, Any]:
    completion = state.cohort_completed.get(name)
    role, attempt_id = member_identity(final, position, stability=stability)
    return {
        "measurement_id": name,
        "role": role,
        "attempt_id": attempt_id,
        **{key: None if completion is None else completion[key] for key in _COMPLETION_KEYS},
    }


def _final_entry(
    snapshot: CampaignSnapshot, number: int, position: int, *, last: bool
) -> dict[str, Any]:
    state = snapshot.rounds[number]
    final = state.finals[position]
    stability = str(final["stability"])
    stored = stability == "stable"
    bound = int(final["seq"])
    current = state.finals_current[position]
    contrasts = []
    for finalist in final["finalists"]:
        contrast = state.global_comparisons.get(str(finalist["attempt_id"]))
        if contrast is None:
            continue
        # Plan choice 8 (F0 R35): a stored contrast also compares across a
        # condition change when its stored measurement is from an older epoch.
        changed = _changed_in_place(snapshot, number, bound, int(contrast["seq"])) or (
            stored
            and epoch_of(snapshot, str(contrast["champion_measurement_id"]))
            != epoch_of(snapshot, str(contrast["finalist_measurement_id"]))
        )
        contrasts.append(
            {
                "attempt_id": contrast["attempt_id"],
                **{name: contrast[name] for name in _FINAL_CONTRAST_VALUES},
                "removed_pairs": removed_count(contrast["removed_pairs"]),
                "removed_cases": removed_count(contrast["removed_cases"]),
                "condition_changed_in_place": changed,
            }
        )
    # Plan choice 6: the selection follows the round's latest final, so it is
    # shown on the last entry only, once `selection_decided` exists.
    selection = state.selection if last else None
    closed = state.closed
    # Family 13's rule: the selection's measurement once the round closed
    # `promoted` or `retained`. This is the next baseline without a refresh
    # (`family14_next_baseline`); a performed refresh is shown in
    # `settings.baseline_refresh.performed`, never here.
    next_baseline = (
        selection["measurement_id"]
        if selection is not None
        and closed is not None
        and closed["outcome"] in ("promoted", "retained")
        else None
    )
    stored_id = state.baseline_measurement_id if stored else None
    # Family 13's rule, and under `stable` (F0 R35, as family 13 reads a
    # binding's own changes) also a bound finalist member measured in another
    # epoch than the stored measurement. A member not yet bound has no epoch.
    changed = _changed_in_place(snapshot, number, bound, float("inf")) or (
        stored_id is not None
        and any(
            name in snapshot.measurements
            and epoch_of(snapshot, name) != epoch_of(snapshot, stored_id)
            for name in current
        )
    )
    return {
        "ordinal": position + 1,
        "stability": stability,
        "champion_side": _CHAMPION_SIDES[stability],
        # R34: the stored Champion is the round's complete baseline, which the
        # lifecycle requires every stored member to cite and never re-binds.
        "stored_measurement_id": stored_id,
        "mode": final["mode"],
        "global": final["global"],
        "members": [
            _member14(state, final, stability, index, name) for index, name in enumerate(current)
        ],
        "contrasts": contrasts,
        "selection": (
            None if selection is None else {name: selection[name] for name in _SELECTION_KEYS}
        ),
        "next_baseline_measurement_id": next_baseline,
        # Plan choice 7: the round's latest `final_rows` listing, per entry.
        "listed": [_plain(row) for row in state.cohort_listed if row["measurement_id"] in current],
        "condition_changed_in_place": changed,
    }


def round_final_entries(snapshot: CampaignSnapshot, number: int) -> list[dict[str, Any]]:
    """M5: the `final` list of round `number`, one entry per final binding in journal order.

    `[]` before the round's first `finalists_bound` (never `null`). Members
    are each binding's current ids (`RoundState.finals_current`), the
    Champion first under `unstable` only; completions, contrasts, listing and
    in-place changes are projected as family 13's `round_cohort_entry` does.
    """

    state = snapshot.rounds[number]
    count = len(state.finals)
    return [
        _final_entry(snapshot, number, position, last=position == count - 1)
        for position in range(count)
    ]


#: M7, plan choice 9: one `{kind}` per role.
ROLES_REPORT_KEYS = frozenset({"grouper", "reflector", "editor"})
#: Plan choice 10: the policy in force.
SETTINGS_REPORT_KEYS = frozenset({"stability", "round_stop", "baseline_refresh"})
#: ADR-0032's literal for `baseline_refresh_after_rounds: 0`.
BASELINE_REFRESH_OFF = "off"
BASELINE_REFRESH_KEYS = frozenset({"every_rounds", "count", "performed"})
PERFORMED_REFRESH_KEYS = frozenset(
    {"round", "measurement_id", "replaced_measurement_id", "completed"}
)
#: C§6.3, plan choice 11: the closed `connection` object.
CONNECTION_REPORT_KEYS = frozenset(
    {"state", "binding_digest", "operation_id", "attempts", "outcome", "detail"}
)
CONNECTION_STATES = ("not_started", "running", "compatible", "interrupted", "suspended")
#: C§4.3's three failure classes, in `connection_probe`'s order.
CONNECTION_FAILURES = tuple(CAUSE_CODES)
CONNECTION_OUTCOMES = ("compatible", *CONNECTION_FAILURES, "interrupted")
INCOMPATIBLE_DETAIL_KEYS = frozenset({"code", "metric", "manifest_path", "observed_path"})
#: C§6.3: the outcomes each state admits. `running` is an open start or a
#: failed terminal whose suspension is owed; `suspended` a failed one after it.
_OUTCOMES_OF_STATE: Mapping[str, tuple[str | None, ...]] = {
    "not_started": (None,),
    "running": (None, *CONNECTION_FAILURES),
    "compatible": ("compatible",),
    "interrupted": ("interrupted",),
    "suspended": CONNECTION_FAILURES,
}
_HEX = frozenset("0123456789abcdef")


def roles_block(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """M7: the kinds of the three roles in force (plan choice 9).

    The Reflector's and the Editor's from the identity in force; the
    Grouper's from the derived `CampaignSnapshot.grouper_kind`.
    """

    identity = snapshot.identity
    return {
        "grouper": {"kind": snapshot.grouper_kind},
        "reflector": {"kind": identity["reflector"]["kind"]},
        "editor": {"kind": identity["editor"]["kind"]},
    }


def _refresh_count(snapshot: CampaignSnapshot) -> int:
    """F0 R42 as the planner derives it: closed rounds on the current stored Champion.

    Over the open round's baseline, or, between rounds, over the next round's
    (`family14_next_baseline` with the latest epoch, as `campaign_view`
    names it). A refresh bound for the next round replaces the count before
    its `baseline_bound` (the `rebaseline` round binds its fresh id instead).
    """

    numbers = sorted(number for number in snapshot.rounds if number >= snapshot.first_round)
    if numbers and snapshot.rounds[numbers[-1]].closed is None:
        number = numbers[-1]
        current = str(snapshot.rounds[number].baseline_measurement_id)
    else:
        number = numbers[-1] + 1 if numbers else snapshot.first_round
        if number != snapshot.rebaseline_from_round and any(
            entry["round"] == number for entry in snapshot.refreshes
        ):
            return 0
        current = family14_next_baseline(
            snapshot.rounds,
            first_round=snapshot.first_round,
            rebaseline_from_round=snapshot.rebaseline_from_round,
            epoch=len(snapshot.epochs) - 1,
            round_number=number,
        )
    return refresh_count(
        snapshot.rounds,
        first_round=snapshot.first_round,
        round_number=number,
        next_baseline=current,
    )


def settings_block(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """Plan choice 10: `stability`, `round_stop` and `baseline_refresh` in force.

    `baseline_refresh` is `"off"` when N = 0, else the period, the derived
    count and every bound refresh in journal order with whether it completed.
    """

    policy = snapshot.identity["policy"]
    after = int(policy["baseline_refresh_after_rounds"])
    refresh: str | dict[str, Any] = BASELINE_REFRESH_OFF
    if after:
        refresh = {
            "every_rounds": after,
            "count": _refresh_count(snapshot),
            "performed": [
                {
                    "round": entry["round"],
                    "measurement_id": entry["measurement_id"],
                    "replaced_measurement_id": entry["replaced"]["measurement_id"],
                    "completed": entry["completed"] is not None,
                }
                for entry in snapshot.refreshes
            ],
        }
    return {
        "stability": policy["gate"]["stability"],
        "round_stop": policy["round_stop"],
        "baseline_refresh": refresh,
    }


def connection_block(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """C§6.3, plan choice 11: the latest connection preflight of the run.

    `running` while a start is open or a failed terminal owes its suspension;
    otherwise the latest operation's outcome names the state (`suspended` for
    a failed one). After a campaign terminal the state is kept (ruling Q1).
    """

    view = snapshot.connection
    if not view.operations:
        return {
            "state": "not_started",
            "binding_digest": None,
            "operation_id": None,
            "attempts": 0,
            "outcome": None,
            "detail": None,
        }
    latest = view.operations[-1]
    outcome = latest["outcome"]
    terminal = latest["terminal"]
    if view.open is not None or view.owes_suspension is not None:
        state = "running"
    elif outcome in ("compatible", "interrupted"):
        state = str(outcome)
    else:
        state = "suspended"
    detail: dict[str, Any] | None = None
    if outcome == "connection_incompatible":
        epoch = snapshot.epochs[int(latest["epoch"])]
        detail = {
            "code": terminal["detail_code"],
            "metric": epoch["policy"]["metric"]["primary"],
            "manifest_path": terminal["manifest_path"],
            "observed_path": terminal["observed_path"],
        }
    elif outcome in CONNECTION_FAILURES:
        detail = {"cause_code": terminal["cause_code"]}
    return {
        "state": state,
        "binding_digest": latest["binding_digest"],
        "operation_id": latest["operation_id"],
        "attempts": view.attempts,
        "outcome": outcome,
        "detail": detail,
    }


# -- validation ------------------------------------------------------------------


def _unavailable14(value: Any) -> None:
    _require(value is None or (type(value) is str and value in UNAVAILABLE14))


def _estimate14(value: Any, *, observe: bool) -> None:
    _keys(value, ESTIMATE_KEYS14)
    for name in ("point", "lower", "upper", "level"):
        _optional_number(value[name])
    _unavailable14(value["unavailable"])
    exact = value["exact"]
    # R22: `exact` is null exactly when no bound is requested (no level, and
    # always in `observe`), a boolean otherwise.
    _require(exact is None or type(exact) is bool)
    _require((exact is None) == (value["level"] is None))
    _require(not observe or exact is None)
    if exact:
        # R21: an exact estimate is available and its bounds are its point.
        _require(
            value["unavailable"] is None
            and value["point"] is not None
            and value["lower"] == value["point"]
            and value["upper"] in (None, value["point"])
        )


def _overall14(value: Any) -> None:
    _keys(value, _OVERALL_KEYS)
    for name in ("point", "lower", "upper", "level"):
        _optional_number(value[name])
    _unavailable14(value["unavailable"])


def _look14(entry: Any, *, observe: bool) -> None:
    _keys(entry, EVALUATED_KEYS)
    _count(entry["look"], least=1)
    _count(entry["comparison"], least=1)
    _require(type(entry["final"]) is bool)
    _estimate14(entry["focus"], observe=observe)
    if entry["causal"] is not None:
        _estimate14(entry["causal"], observe=observe)
    if entry["overall"] is not None:
        _overall14(entry["overall"])
    tolerance = entry["tolerance"]
    _keys(tolerance, frozenset({"source", "value"}))
    _require(type(tolerance["source"]) is str and tolerance["source"] in TOLERANCE_SOURCES)
    _optional_number(tolerance["value"])
    decision, reason = entry["decision"], entry["reason"]
    _require(
        type(decision) is str
        and decision in ATTEMPT_DECISIONS14
        and (reason is None or type(reason) is str)
        and reason in ATTEMPT_DECISIONS14[decision]
    )
    _require(entry["would_pass_point"] is None or type(entry["would_pass_point"]) is bool)
    caveats = entry["caveats"]
    _require(type(caveats) is list and all(type(kind) is str for kind in caveats))
    _require(set(caveats) <= LOOK_CAVEATS14 and caveats == sorted(set(caveats)))


def validate_confidence_block14(block: Any) -> None:
    """The closed shape of a non-null family-14 `confidence` block (plan choice 3).

    The twelve fields family 14 shares with family 13 keep family 13's rules:
    family 13's validator checks them over the block without `margin` and
    with no looks. `margin` is any finite number (a negative one is admitted,
    D8), and every evaluated look follows the family-14 vocabulary. A shape
    check only, as family 13's.
    """

    _keys(block, CONFIDENCE_REPORT_KEYS14)
    shared = {name: value for name, value in block.items() if name != "margin"}
    validate_confidence_block({**shared, "evaluated": []})
    _require(_number(block["margin"]))
    evaluated = block["evaluated"]
    _require(type(evaluated) is list)
    observe = block["mode"] == "observe"
    for entry in evaluated:
        _look14(entry, observe=observe)
    _require(_increasing([entry["look"] for entry in evaluated]))


def _validate_contrast14(contrast: Any, *, fresh: bool) -> None:
    _keys(contrast, FINAL_CONTRAST_KEYS)
    _identifier(contrast["attempt_id"])
    for name in ("point", "lower", "level"):
        _optional_number(contrast[name])
    unavailable = contrast["unavailable"]
    _unavailable14(unavailable)
    _require(contrast["pairing"] in ("exact", "unpaired"))
    accepted, reason = contrast["accepted"], contrast["reason"]
    _require(
        type(accepted) is bool and type(reason) is str and reason in GLOBAL_REASONS14[accepted]
    )
    exact = contrast["exact"]
    _require(
        (exact is None or type(exact) is bool) and (exact is None) == (contrast["level"] is None)
    )
    if exact:
        # R28: an exact global contrast is available and its bound is its point.
        _require(
            unavailable is None
            and contrast["point"] is not None
            and contrast["lower"] == contrast["point"]
        )
    for name in ("removed_pairs", "removed_cases"):
        _count(contrast[name])
        # A fresh comparison removes no pair or case.
        _require(not fresh or contrast[name] == 0)
    _require(type(contrast["condition_changed_in_place"]) is bool)


def _validate_final_entry(entry: Any) -> None:
    _keys(entry, FINAL_REPORT_KEYS)
    _count(entry["ordinal"], least=1)
    stability = entry["stability"]
    _require(type(stability) is str and stability in _CHAMPION_SIDES)
    fresh = stability == "unstable"
    _require(entry["champion_side"] == _CHAMPION_SIDES[stability])
    stored = entry["stored_measurement_id"]
    if fresh:
        _require(stored is None)
    else:
        _identifier(stored)
    _require(type(entry["mode"]) is str and entry["mode"] in GATE_MODES)
    _require(type(entry["global"]) is str and entry["global"] in GLOBAL_MODES)
    members = entry["members"]
    # R31: the Champion member exists only under `unstable`, at position 0.
    _require(type(members) is list and len(members) >= 1 + int(fresh))
    for position, member in enumerate(members):
        # Under `stable` there is no Champion slot: every member validates as a finalist.
        _validate_member(member, position + int(not fresh))
    names = [member["measurement_id"] for member in members]
    finalists = [member["attempt_id"] for member in members[int(fresh) :]]
    _require(len(set(names)) == len(names) and len(set(finalists)) == len(finalists))
    _require(stored not in names)
    contrasts = entry["contrasts"]
    _require(type(contrasts) is list)
    for contrast in contrasts:
        _validate_contrast14(contrast, fresh=fresh)
    places = [
        finalists.index(c["attempt_id"]) if c["attempt_id"] in finalists else -1 for c in contrasts
    ]
    _require(all(place >= 0 for place in places) and _increasing(places))
    changed = entry["condition_changed_in_place"]
    _require(type(changed) is bool)
    # Family 13's rule: a changed contrast implies a changed entry (under
    # `stable` the entry also covers the stored measurement's epoch, R35).
    _require(changed or not any(c["condition_changed_in_place"] for c in contrasts))
    selection = entry["selection"]
    if selection is not None:
        _keys(selection, frozenset(_SELECTION_KEYS))
        _require(type(selection["reason"]) is str and selection["reason"] in SELECTION_REASONS14)
        _require(selection["attempt_id"] is None or selection["attempt_id"] in finalists)
        # R34: only a `stable` retention names the stored measurement.
        retained = selection["attempt_id"] is None and selection["reason"] != "finalist_selected"
        _require(
            selection["measurement_id"] in names
            or (not fresh and retained and selection["measurement_id"] == stored)
        )
    following = entry["next_baseline_measurement_id"]
    _require(
        following is None or (selection is not None and following == selection["measurement_id"])
    )
    listed = entry["listed"]
    _require(type(listed) is list and len(listed) <= MAX_FAULT_ROWS)
    for row in listed:
        _validate_listed(row)
        # Plan choice 7: an entry lists only its own members' rows.
        _require(row["measurement_id"] in names)


def validate_final_entries(entries: Any) -> None:
    """The closed shape of a round's `final` list (M5; plan choices 4-8).

    A list, never `null`. Ordinals count the bindings from 1 in journal order,
    each finalist appears in one entry, and only the last entry may carry the
    selection and the next baseline (choice 6). A shape check only, as family
    13's: whether the values follow from the records is the lifecycle's and
    the audit's.
    """

    _require(type(entries) is list)
    for entry in entries:
        _validate_final_entry(entry)
    _require([entry["ordinal"] for entry in entries] == list(range(1, len(entries) + 1)))
    for entry in entries[:-1]:
        _require(entry["selection"] is None and entry["next_baseline_measurement_id"] is None)
    finalists = [
        member["attempt_id"]
        for entry in entries
        for member in entry["members"]
        if member["role"] == "finalist"
    ]
    _require(len(set(finalists)) == len(finalists))


def validate_roles_block(block: Any) -> None:
    """The closed `roles` block (M7). Kinds are identifiers, not closed sets,
    because a library caller may bind its own role implementation (choice 9)."""

    _keys(block, ROLES_REPORT_KEYS)
    for role in block.values():
        _keys(role, frozenset({"kind"}))
        _identifier(role["kind"])


def validate_settings_block(block: Any) -> None:
    """The closed `settings` block (plan choice 10). A shape check only."""

    _keys(block, SETTINGS_REPORT_KEYS)
    _require(type(block["stability"]) is str and block["stability"] in STABILITIES)
    _require(type(block["round_stop"]) is str and block["round_stop"] in ROUND_STOPS)
    refresh = block["baseline_refresh"]
    if type(refresh) is str:
        _require(refresh == BASELINE_REFRESH_OFF)
        return
    _keys(refresh, BASELINE_REFRESH_KEYS)
    _count(refresh["every_rounds"], least=1)
    _count(refresh["count"])
    performed = refresh["performed"]
    _require(type(performed) is list)
    for entry in performed:
        _keys(entry, PERFORMED_REFRESH_KEYS)
        _count(entry["round"], least=1)
        _identifier(entry["measurement_id"])
        _identifier(entry["replaced_measurement_id"])
        _require(type(entry["completed"]) is bool)
    # One refresh per round, in journal order.
    _require(_increasing([entry["round"] for entry in performed]))


def _path(value: Any) -> None:
    _require(type(value) is str and 0 < len(value.encode("utf-8")) <= MAX_PATH_BYTES)


def validate_connection_block(block: Any) -> None:
    """The closed `connection` object (C§6.3, plan choice 11). A shape check only.

    The state agrees with the outcome; an operation is named exactly when one
    started; `detail` is the incompatibility's bounded diagnostic or the
    other failure classes' `cause_code` from that class's closed vocabulary
    (ADR-0029 amendment 2026-10-12), and `null` otherwise.
    """

    _keys(block, CONNECTION_REPORT_KEYS)
    state, outcome, detail = block["state"], block["outcome"], block["detail"]
    _require(type(state) is str and state in CONNECTION_STATES)
    _require(outcome is None or (type(outcome) is str and outcome in CONNECTION_OUTCOMES))
    _count(block["attempts"])
    started = block["attempts"] > 0
    _require(started == (state != "not_started"))
    if started:
        digest = block["binding_digest"]
        _require(type(digest) is str and len(digest) == 64 and set(digest) <= _HEX)
        _identifier(block["operation_id"])
    else:
        _require(block["binding_digest"] is None and block["operation_id"] is None)
    _require(outcome in _OUTCOMES_OF_STATE[state])
    if outcome == "connection_incompatible":
        _keys(detail, INCOMPATIBLE_DETAIL_KEYS)
        _require(type(detail["code"]) is str and detail["code"] in DETAIL_CODES)
        _identifier(detail["metric"])
        _path(detail["manifest_path"])
        _path(detail["observed_path"])
    elif outcome in CONNECTION_FAILURES:
        _keys(detail, frozenset({"cause_code"}))
        cause = detail["cause_code"]
        _require(type(cause) is str and cause in CAUSE_CODES[outcome])
    else:
        _require(detail is None)
