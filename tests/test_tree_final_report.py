"""The family-14 report blocks (F0 M4, M5; H12b plan choices 3-8).

Unit tests over hand-built `AttemptState`s and `RoundState`s, plus
lifecycle-built prefixes (`tests.campaign_fakes.Family14Life`) where a test
needs a consistent one. Native final-report assertions pin the current blocks.
"""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import copy
from dataclasses import replace

import pytest

from evolution.tree.campaign_schema import AttemptState, SchemaError
from tests.campaign_drivers import observe_barrier  # noqa: F401
from tests.campaign_fakes import estimate

METHOD = {"version": "f13-bootstrap/1", "draws": 20000, "quantile": "linear", "noise_level": 0.995}


def _attempt(**fields):
    base = AttemptState(
        attempt_id="r01-a01", round=1, index=1, parent_version="v0", parent_digest="0" * 64
    )
    return replace(base, **fields)


def _comparison(seq=20, **overrides):
    payload = {
        "round": 1,
        "attempt_id": "r01-a01",
        "supersedes": None,
        "mode": "statistical",
        "epoch": 0,
        "candidate_measurement_id": "m-r01-a01",
        "parent_measurement_id": "m-r01-a01-parent",
        "baseline_measurement_id": "m-r00-champion",
        "parent_source": "fresh_parent",
        "focus": [1],
        "causal": [0, 2],
        "retained": {"focus": [1], "causal": [0, 2]},
        "exclusions": None,
        "repeats": 4,
        "looks": [3, 4],
        "confidence": {"focus": 0.9, "causal": 0.9},
        "tolerance": {"kind": "measured_noise"},
        "method": dict(METHOD),
        "seq": seq,
        "digest": "c" * 64,
    }
    payload.update(overrides)
    return payload


def _calibration_result(**overrides):
    payload = {
        "round": 1,
        "attempt_id": "r01-a01",
        "binding": 21,
        "prefix": "measurements/m-r01-a01-parent/events.jsonl#bytes=0:4096",
        "allowance": 0.05,
        "degenerate": False,
        "unavailable": None,
        "result_digest": "d" * 64,
        "seq": 30,
    }
    payload.update(overrides)
    return payload


def _look(look, *, comparison=20, final, **overrides):
    payload = {
        "round": 1,
        "attempt_id": "r01-a01",
        "comparison": comparison,
        "look": look,
        "final": final,
        "prefixes": {
            "candidate": "measurements/m-r01-a01/events.jsonl#bytes=0:4096",
            "parent": "measurements/m-r01-a01-parent/events.jsonl#bytes=0:4096",
        },
        "focus": estimate(0.25, 0.05, 0.45, 0.9),
        "causal": None,
        "overall": None,
        "tolerance": {"source": "measured_noise", "value": 0.05},
        "decision": "continue",
        "reason": None,
        "would_pass_point": None,
        "caveats": ["intervals_empirical", "noise_allowance_empirical"],
        "seq": 40 + look,
        "record_digest": "e" * 64,
    }
    payload.update(overrides)
    return payload


OBSERVED = "not_requested_in_observe_mode"


def _estimate14(point, lower=None, upper=None, level=None, unavailable=None, exact=None):
    """R22: family 13's five keys plus `exact` (null exactly without a level)."""

    return {**estimate(point, lower, upper, level, unavailable), "exact": exact}


def _comparison14(**overrides):
    return _comparison(**{"margin": 0.0, **overrides})


def _interim14(look=3, **overrides):
    return _look(
        look,
        final=False,
        **{
            "focus": _estimate14(0.25, 0.05, 0.45, 0.9, exact=False),
            "caveats": ["intervals_empirical", "noise_allowance_empirical"],
            **overrides,
        },
    )


def _final14(look=4, **overrides):
    return _look(
        look,
        final=True,
        **{
            "focus": _estimate14(0.25, 0.05, None, 0.9, exact=False),
            "causal": _estimate14(0.0, 0.0, None, 0.9, exact=True),
            "overall": estimate(0.2, 0.02, None, 0.9),
            "decision": "finalist",
            "reason": "gate_passed",
            "caveats": ["exact_contrast", "intervals_empirical", "noise_allowance_empirical"],
            **overrides,
        },
    )


def _statistical_attempt(**fields):
    return _attempt(
        comparison=_comparison14(),
        calibration_result=_calibration_result(),
        # Inserted out of order: the block is in look order.
        looks={4: _final14(), 3: _interim14()},
        **fields,
    )


def _observe_attempt(**look_overrides):
    comparison = _comparison14(
        mode="observe",
        repeats=1,
        looks=[1],
        confidence=None,
        margin=-0.125,
    )
    look = _look(
        1,
        final=True,
        **{
            "focus": _estimate14(0.25, unavailable=OBSERVED),
            "causal": _estimate14(0.0, unavailable=OBSERVED),
            "overall": estimate(0.2, unavailable=OBSERVED),
            "tolerance": {"source": "measured_noise", "value": 0},
            "decision": "finalist",
            "reason": "observe_finalist",
            "would_pass_point": True,
            "caveats": ["noise_not_observed", "observe_gate_not_enforced"],
            **look_overrides,
        },
    )
    return _attempt(
        comparison=comparison,
        calibration_result=_calibration_result(allowance=0, degenerate=True),
        looks={1: look},
    )


# -- Task 2: the family-14 Attempt `confidence` block (plan choice 3) -----------


def test_the_family14_key_sets_extend_family_13s():
    from evolution.tree.confidence_report import CONFIDENCE_REPORT_KEYS
    from evolution.tree.final_report import CONFIDENCE_REPORT_KEYS14, ESTIMATE_KEYS14

    assert CONFIDENCE_REPORT_KEYS14 == CONFIDENCE_REPORT_KEYS | {"margin"}
    assert ESTIMATE_KEYS14 == {"point", "lower", "upper", "level", "unavailable", "exact"}
    # Family 13's constants are untouched (also pinned by the registry digest).
    assert "margin" not in CONFIDENCE_REPORT_KEYS and len(CONFIDENCE_REPORT_KEYS) == 13


def test_no_comparison_no_block():
    from evolution.tree.final_report import attempt_confidence_entry14

    assert attempt_confidence_entry14(_attempt()) is None


def test_a_statistical_block_carries_the_margin_and_six_key_estimates():
    from evolution.tree.confidence_report import EVALUATED_KEYS
    from evolution.tree.final_report import (
        CONFIDENCE_REPORT_KEYS14,
        attempt_confidence_entry14,
        validate_confidence_block14,
    )

    block = attempt_confidence_entry14(_statistical_attempt())
    assert block is not None and set(block) == CONFIDENCE_REPORT_KEYS14
    assert block["margin"] == 0.0 and block["method"] == METHOD
    assert block["confidence"] == {"focus": 0.9, "causal": 0.9}
    interim, final = block["evaluated"]
    assert (interim["look"], final["look"]) == (3, 4)
    assert set(interim) == set(final) == EVALUATED_KEYS
    # Looks are copied unchanged: six-key estimates, five-key overall.
    assert interim["focus"] == _estimate14(0.25, 0.05, 0.45, 0.9, exact=False)
    assert final["causal"] == _estimate14(0.0, 0.0, None, 0.9, exact=True)
    assert final["overall"] == estimate(0.2, 0.02, None, 0.9)
    assert (final["decision"], final["reason"]) == ("finalist", "gate_passed")
    assert final["caveats"] == [
        "exact_contrast",
        "intervals_empirical",
        "noise_allowance_empirical",
    ]
    for hidden in ("evidence", "prefixes", "seq", "record_digest", "digest", "exclusions"):
        assert hidden not in block and hidden not in final
    validate_confidence_block14(block)


def test_an_exact_two_sided_interim_estimate_validates():
    from evolution.tree.final_report import attempt_confidence_entry14, validate_confidence_block14

    look = _interim14(focus=_estimate14(0.25, 0.25, 0.25, 0.9, exact=True))
    block = attempt_confidence_entry14(_attempt(comparison=_comparison14(), looks={3: look}))
    validate_confidence_block14(block)


def test_an_observe_block_with_a_degenerate_calibration():
    # R25: no Parent variation gives allowance 0, `degenerate: true` and the
    # look caveat `noise_not_observed` in place of `observe_calibration_degenerate`.
    from evolution.tree.final_report import attempt_confidence_entry14, validate_confidence_block14

    block = attempt_confidence_entry14(_observe_attempt())
    assert block["mode"] == "observe" and block["confidence"] is None
    assert block["margin"] == -0.125
    assert block["calibration"] == {"allowance": 0, "degenerate": True, "unavailable": None}
    (look,) = block["evaluated"]
    assert look["focus"]["exact"] is None and look["causal"]["exact"] is None
    assert "exact" not in look["overall"]
    assert (look["decision"], look["reason"]) == ("finalist", "observe_finalist")
    assert look["caveats"] == ["noise_not_observed", "observe_gate_not_enforced"]
    validate_confidence_block14(block)


def test_a_rejected_look_names_focus_below_margin():
    from evolution.tree.final_report import attempt_confidence_entry14, validate_confidence_block14

    look = _final14(
        focus=_estimate14(0.0, 0.0, None, 0.9, exact=True),
        decision="rejected",
        reason="focus_below_margin",
    )
    block = attempt_confidence_entry14(
        _attempt(comparison=_comparison14(margin=0.0625), looks={4: look})
    )
    assert block["evaluated"][0]["reason"] == "focus_below_margin"
    validate_confidence_block14(block)


def test_the_block_does_not_alias_the_snapshot():
    from evolution.tree.final_report import attempt_confidence_entry14

    state = _statistical_attempt()
    block = attempt_confidence_entry14(state)
    block["evaluated"][0]["focus"]["exact"] = True
    block["evaluated"][1]["caveats"].append("x")
    again = attempt_confidence_entry14(state)
    assert again["evaluated"][0]["focus"]["exact"] is False
    assert len(again["evaluated"][1]["caveats"]) == 3


def test_a_lifecycle_family14_attempt_projects_and_validates(observe_barrier):
    from evolution.tree.final_report import attempt_confidence_entry14, validate_confidence_block14
    from tests.test_tree_campaign_family14 import _closed_life

    life = _closed_life(observe_barrier)
    attempt = life.attempt(life.sealed)
    block = attempt_confidence_entry14(attempt)
    assert block["margin"] == attempt.comparison["margin"]
    assert block["evaluated"][0]["decision"] == "finalist"
    validate_confidence_block14(block)
    # Family 13's validator refuses it: the blocks are family-separated.
    from evolution.tree.confidence_report import validate_confidence_block

    with pytest.raises(SchemaError):
        validate_confidence_block(block)


def _valid_statistical():
    from evolution.tree.final_report import attempt_confidence_entry14

    return attempt_confidence_entry14(_statistical_attempt())


def _valid_observe():
    from evolution.tree.final_report import attempt_confidence_entry14

    return attempt_confidence_entry14(_observe_attempt())


def _without_exact(estimate_value):
    estimate_value.pop("exact")


@pytest.mark.parametrize(
    "build,edit",
    [
        (_valid_statistical, lambda b: b.pop("margin")),
        (_valid_statistical, lambda b: b.update(margin=None)),
        (_valid_statistical, lambda b: b.update(margin=True)),
        (_valid_statistical, lambda b: b.update(margin=float("inf"))),
        (_valid_statistical, lambda b: b.update(extra=1)),
        # A five-key focus estimate (family 13's shape).
        (_valid_statistical, lambda b: _without_exact(b["evaluated"][0]["focus"])),
        (_valid_statistical, lambda b: _without_exact(b["evaluated"][1]["causal"])),
        # `overall` keeps five keys.
        (_valid_statistical, lambda b: b["evaluated"][1]["overall"].update(exact=False)),
        # R22: no family-14 estimate is ever `degenerate_interval`.
        (
            _valid_statistical,
            lambda b: b["evaluated"][0]["focus"].update(unavailable="degenerate_interval"),
        ),
        (
            _valid_statistical,
            lambda b: b["evaluated"][1]["overall"].update(unavailable="degenerate_interval"),
        ),
        # Family 13's decision and reasons.
        (_valid_statistical, lambda b: b["evaluated"][1].update(decision="accepted")),
        (
            _valid_statistical,
            lambda b: b["evaluated"][1].update(
                decision="rejected", reason="focus_lower_not_positive"
            ),
        ),
        (
            _valid_statistical,
            lambda b: b["evaluated"][1].update(
                decision="rejected", reason="focus_upper_nonpositive"
            ),
        ),
        (
            _valid_statistical,
            lambda b: b["evaluated"][1].update(caveats=["observe_calibration_degenerate"]),
        ),
        # R21: an exact estimate's lower (and two-sided upper) is its point.
        (_valid_statistical, lambda b: b["evaluated"][1]["causal"].update(lower=-0.125)),
        (_valid_statistical, lambda b: b["evaluated"][0]["focus"].update(exact=True)),
        (
            _valid_statistical,
            lambda b: b["evaluated"][1]["causal"].update(unavailable="insufficient_repeats"),
        ),
        (_valid_statistical, lambda b: b["evaluated"][0]["focus"].update(exact="no")),
        # Exact is null exactly without a level.
        (_valid_statistical, lambda b: b["evaluated"][0]["focus"].update(exact=None)),
        (_valid_statistical, lambda b: b["evaluated"][1]["caveats"].reverse()),
        (_valid_statistical, lambda b: b["evaluated"][1].update(caveats=["completion_regression"])),
        (_valid_statistical, lambda b: b["evaluated"].reverse()),
        (_valid_statistical, lambda b: b["evaluated"][0].update(look=0)),
        (_valid_statistical, lambda b: b.update(confidence=None)),
        (_valid_statistical, lambda b: b["method"].pop("draws")),
        # Observe: no exact claim on an estimate without a bound.
        (_valid_observe, lambda b: b["evaluated"][0]["focus"].update(exact=True)),
        (_valid_observe, lambda b: b["evaluated"][0]["causal"].update(exact=False)),
        # Even with a level, an observe estimate makes no exactness claim.
        (_valid_observe, lambda b: b["evaluated"][0]["focus"].update(level=0.9, exact=False)),
        (_valid_observe, lambda b: b["evaluated"][0].update(caveats=["noise_not_observed"] * 2)),
        (
            _valid_observe,
            lambda b: b["evaluated"][0].update(
                caveats=["observe_calibration_degenerate", "observe_gate_not_enforced"]
            ),
        ),
        (_valid_observe, lambda b: b["calibration"].update(degenerate=1)),
        # A malformed (unhashable) value is a refused shape, not a TypeError.
        (_valid_statistical, lambda b: b["evaluated"][0]["focus"].update(unavailable=[])),
        (_valid_statistical, lambda b: b["evaluated"][1]["overall"].update(unavailable={})),
        (_valid_statistical, lambda b: b["evaluated"][0]["tolerance"].update(source=[])),
    ],
)
def test_validate_confidence_block14_refuses_a_bad_shape(build, edit):
    from evolution.tree.final_report import validate_confidence_block14

    block = build()
    validate_confidence_block14(block)
    edit(block)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_confidence_block14(block)


def test_validate_confidence_block14_refuses_a_family13_block():
    from evolution.tree.confidence_report import attempt_confidence_entry
    from evolution.tree.final_report import validate_confidence_block14

    block = attempt_confidence_entry(
        _attempt(comparison=_comparison(), looks={3: _look(3, final=False)})
    )
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_confidence_block14(block)
    block["margin"] = 0
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_confidence_block14(copy.deepcopy(block))


def test_a_negative_margin_and_a_numeric_tolerance_validate():
    from evolution.tree.final_report import validate_confidence_block14

    block = _valid_statistical()
    block.update(margin=-0.25, tolerance={"kind": "numeric", "value": 0.02})
    validate_confidence_block14(block)


# -- Task 3: the round's `final` list (M5; plan choices 4-8) ---------------------

STORED = "m-r00-champion"
OBSERVE_UNAVAILABLE = "bound_not_requested_in_observe_mode"


def _attempt_id(index):
    return f"r01-a{index:02d}"


def _member_id(ordinal, index):
    from evolution.tree.campaign_identity import final_measurement_id_for

    return final_measurement_id_for(1, ordinal, index)


def _binding(ordinal, indexes, *, seq, stability="unstable", mode="observe", global_mode="point"):
    """A `finalists_bound` as `RoundState.finals` keeps it: no `evidence`, plus
    `seq`, `digest` and the policy's `stability`."""

    fresh = stability == "unstable"
    return {
        "round": 1,
        "champion": {"version": "v0", "digest": "0" * 64},
        "finalists": [
            {"attempt_id": _attempt_id(index), "candidate_digest": "1" * 64, "level": None}
            for index in indexes
        ],
        "members": ([_member_id(ordinal, None)] if fresh else [])
        + [_member_id(ordinal, index) for index in indexes],
        "global": global_mode,
        "mode": mode,
        "repeats": 1,
        "looks": [1],
        "seed_mode": "exact",
        "epoch": 0,
        "order": "case_repeat_member",
        "seq": seq,
        "digest": "f" * 64,
        "stability": stability,
    }


def _done14(measurement_id, seq, **overrides):
    return {
        "round": 1,
        "measurement_id": measurement_id,
        "prefix": f"measurements/{measurement_id}/events.jsonl#bytes=0:4096",
        "complete": True,
        "point": 0.5,
        "missing": 0,
        "critical": False,
        "seq": seq,
        **overrides,
    }


def _contrast14(index, finalist, champion, seq, *, stored=False, **overrides):
    return {
        "round": 1,
        "attempt_id": _attempt_id(index),
        "finalist_measurement_id": finalist,
        "champion_measurement_id": champion,
        "point": 0.25,
        "lower": None,
        "level": None,
        "unavailable": OBSERVE_UNAVAILABLE,
        "pairing": "exact",
        "accepted": True,
        "reason": "global_point_improved",
        "exact": None,
        "champion_side": "stored" if stored else "fresh",
        "removed_pairs": [],
        "removed_cases": [],
        "seq": seq,
        "record_digest": "e" * 64,
        "record_locator": "campaign.jsonl#bytes=0:1",
        **overrides,
    }


def _rejected(**overrides):
    return {
        "accepted": False,
        "reason": "global_point_not_improved",
        "point": -0.125,
        **overrides,
    }


def _selection14(measurement_id, attempt_id, reason):
    return {
        "round": 1,
        "attempt_id": attempt_id,
        "candidate_digest": None if attempt_id is None else "1" * 64,
        "dependency_digest": None if attempt_id is None else "2" * 64,
        "measurement_id": measurement_id,
        "global": "point",
        "reason": reason,
        "ranking": [],
    }


def _round14(**fields):
    from evolution.tree.campaign_schema import RoundState

    return RoundState(round=1, baseline_measurement_id=STORED, baseline_complete=True, **fields)


def _snapshot14(state, *, condition_changes=(), measurements=None):
    """By default every member and the stored baseline are registered in epoch 0."""

    from evolution.tree.campaign_schema import CampaignSnapshot

    if measurements is None:
        names = {STORED, *(mid for members in state.finals_current for mid in members)}
        measurements = {name: {"epoch": 0} for name in names}
    return CampaignSnapshot(
        identity={},
        run_id="c-test",
        alpha_plan={},
        rounds={state.round: state},
        champion=("v0", "0" * 64),
        champion_dependency_digest="0" * 64,
        measurements=measurements,
        accounted=frozenset(),
        counts={"target": 0, "environment": 0, "scorer": 0, "rollout": 0},
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=1,
        family_version=14,
        condition_changes=tuple(condition_changes),
    )


def _change(seq, *, effect="continue", number=1):
    return {
        "seq": seq,
        "effect": effect,
        "fields": ["target.settings"],
        "round": number,
        "authorization_id": "auth-1",
        "identity_digest": "9" * 64,
    }


def _first_accepted(stability, *, winner=True, closed=True, **fields):
    """`first_accepted`: Attempt 1 is not accepted, Attempt 2 wins (or not)."""

    stored = stability == "stable"
    finals, current, completed, comparisons = [], [], {}, {}
    seq = 50
    for ordinal in (1, 2):
        binding = _binding(ordinal, [ordinal], seq=seq, stability=stability)
        members = tuple(binding["members"])
        finals.append(binding)
        current.append(members)
        for offset, mid in enumerate(members, start=1):
            completed[mid] = _done14(mid, seq + offset)
        champion = STORED if stored else members[0]
        accepted = winner and ordinal == 2
        comparisons[_attempt_id(ordinal)] = _contrast14(
            ordinal,
            members[-1],
            champion,
            seq + 5,
            stored=stored,
            **({} if accepted else _rejected()),
        )
        seq += 10
    last = finals[-1]["members"]
    if winner:
        selection = _selection14(last[-1], _attempt_id(2), "finalist_selected")
        outcome = "promoted"
    else:
        selection = _selection14(STORED if stored else last[0], None, "no_accepted_finalist")
        outcome = "retained"
    decided = {
        "finals": tuple(finals),
        "finals_current": tuple(current),
        "cohort_current": current[-1],
        "cohort_completed": completed,
        "global_comparisons": comparisons,
        "selection": selection,
        "closed": {"outcome": outcome, "reason": None} if closed else None,
    }
    return _round14(**{**decided, **fields})


def _stable_all(**contrast):
    """`stable` + `all_attempts`: one binding of two finalists, no Champion member."""

    binding = _binding(1, [1, 2], seq=50, stability="stable")
    members = tuple(binding["members"])
    return _round14(
        finals=(binding,),
        finals_current=(members,),
        cohort_current=members,
        cohort_completed={mid: _done14(mid, 60 + i) for i, mid in enumerate(members)},
        global_comparisons={
            _attempt_id(1): _contrast14(1, members[0], STORED, 70, stored=True, **contrast),
        },
    )


def test_the_final_key_sets():
    from evolution.tree.confidence_report import COHORT_REPORT_KEYS
    from evolution.tree.final_report import FINAL_CONTRAST_KEYS, FINAL_REPORT_KEYS

    assert FINAL_REPORT_KEYS == COHORT_REPORT_KEYS | {
        "ordinal",
        "stability",
        "champion_side",
        "stored_measurement_id",
    }
    assert FINAL_CONTRAST_KEYS == {
        "attempt_id",
        "point",
        "lower",
        "level",
        "unavailable",
        "pairing",
        "accepted",
        "reason",
        "exact",
        "removed_pairs",
        "removed_cases",
        "condition_changed_in_place",
    }
    assert "eligible" not in FINAL_CONTRAST_KEYS and "cohort" not in FINAL_REPORT_KEYS


def test_the_selection_reasons_follow_the_family14_vocabulary():
    from evolution.tree.confidence_statistics import SELECTION_REASONS
    from evolution.tree.final_report import SELECTION_REASONS14

    assert SELECTION_REASONS14 == (SELECTION_REASONS - {"no_eligible_finalist"}) | {
        "no_accepted_finalist"
    }


def test_no_binding_no_entries():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = _round14(
        selection=_selection14(None, None, "no_finalists"),
        closed={"outcome": "stalled", "reason": None},
    )
    assert round_final_entries(_snapshot14(state), 1) == []
    assert round_final_entries(_snapshot14(_round14()), 1) == []
    validate_final_entries([])


def test_unstable_all_attempts_from_the_lifecycle(observe_barrier):
    from evolution.tree.campaign_schema import cohort_selection, selection_records
    from evolution.tree.final_report import round_final_entries, validate_final_entries
    from tests.campaign_drivers import _open_round
    from tests.test_tree_campaign_family14 import _closed_life
    from tests.test_tree_campaign_family14_final import _comparison, _done

    life = _closed_life(observe_barrier)
    champion, finalist = _done(life)
    life.accept("global_comparison_completed", **_comparison(life))
    number, state = _open_round(life)
    selection = cohort_selection(state, life.state.policy.global_mode, family_version=14)
    evidence = [life.reference(source["seq"]) for source in selection_records(state)]
    life.accept("selection_decided", **selection, evidence=evidence)
    (entry,) = round_final_entries(life.snapshot, number)
    assert (entry["ordinal"], entry["stability"], entry["champion_side"]) == (
        1,
        "unstable",
        "fresh",
    )
    assert entry["stored_measurement_id"] is None
    assert [(m["measurement_id"], m["role"]) for m in entry["members"]] == [
        (champion, "champion"),
        (finalist, "finalist"),
    ]
    assert entry["members"][1]["attempt_id"] == life.sealed
    (contrast,) = entry["contrasts"]
    assert contrast == {
        "attempt_id": life.sealed,
        "point": 0.25,
        "lower": None,
        "level": None,
        "unavailable": OBSERVE_UNAVAILABLE,
        "pairing": "unpaired",
        "accepted": True,
        "reason": "global_off",
        "exact": None,
        "removed_pairs": 0,
        "removed_cases": 0,
        "condition_changed_in_place": False,
    }
    assert entry["selection"] == {
        "reason": "finalist_selected",
        "attempt_id": life.sealed,
        "measurement_id": finalist,
    }
    # Selected but not closed: no next baseline yet.
    assert entry["next_baseline_measurement_id"] is None
    assert (entry["listed"], entry["condition_changed_in_place"]) == ([], False)
    validate_final_entries([entry])


def test_stable_all_attempts_from_the_lifecycle(observe_barrier):
    from evolution.tree.campaign_schema import cohort_selection, selection_records
    from evolution.tree.final_report import round_final_entries, validate_final_entries
    from tests.campaign_drivers import _open_round
    from tests.test_tree_campaign_family14_stored import _stable_done, _stable_global

    life = _stable_done(observe_barrier, critical=True)
    life.accept("global_comparison_completed", **_stable_global(life))
    number, state = _open_round(life)
    selection = cohort_selection(state, life.state.policy.global_mode, family_version=14)
    evidence = [life.reference(source["seq"]) for source in selection_records(state)]
    life.accept("selection_decided", **selection, evidence=evidence)
    state = _open_round(life)[1]
    (entry,) = round_final_entries(life.snapshot, number)
    (member,) = state.cohort_current
    assert (entry["stability"], entry["champion_side"]) == ("stable", "stored")
    assert entry["stored_measurement_id"] == state.baseline_measurement_id
    assert [(m["measurement_id"], m["role"]) for m in entry["members"]] == [(member, "finalist")]
    (contrast,) = entry["contrasts"]
    assert (contrast["accepted"], contrast["reason"]) == (False, "critical_failure")
    assert (contrast["removed_pairs"], contrast["removed_cases"]) == (0, 0)
    # R34: the retained selection names the stored measurement.
    assert entry["selection"] == {
        "reason": "no_accepted_finalist",
        "attempt_id": None,
        "measurement_id": state.baseline_measurement_id,
    }
    validate_final_entries([entry])


def test_unstable_contrasts_follow_finalist_order():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    binding = _binding(1, [1, 2], seq=50)
    champion, first, second = binding["members"]
    state = _round14(
        finals=(binding,),
        finals_current=(tuple(binding["members"]),),
        cohort_current=tuple(binding["members"]),
        cohort_completed={mid: _done14(mid, 60 + i) for i, mid in enumerate(binding["members"])},
        # Written in reverse: the report follows the binding's finalist order.
        global_comparisons={
            _attempt_id(2): _contrast14(2, second, champion, 71, **_rejected()),
            _attempt_id(1): _contrast14(1, first, champion, 70),
        },
    )
    (entry,) = round_final_entries(_snapshot14(state), 1)
    assert [c["attempt_id"] for c in entry["contrasts"]] == [_attempt_id(1), _attempt_id(2)]
    assert [c["accepted"] for c in entry["contrasts"]] == [True, False]
    assert [m["role"] for m in entry["members"]] == ["champion", "finalist", "finalist"]
    assert (entry["selection"], entry["next_baseline_measurement_id"]) == (None, None)
    validate_final_entries([entry])


@pytest.mark.parametrize("stability", ["unstable", "stable"])
def test_first_accepted_has_one_entry_per_final_with_the_winner_last(stability):
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = _first_accepted(stability)
    entries = round_final_entries(_snapshot14(state), 1)
    assert [entry["ordinal"] for entry in entries] == [1, 2]
    assert all(entry["stability"] == stability for entry in entries)
    first, last = entries
    assert (first["selection"], first["next_baseline_measurement_id"]) == (None, None)
    winner = _member_id(2, 2)
    assert last["selection"] == {
        "reason": "finalist_selected",
        "attempt_id": _attempt_id(2),
        "measurement_id": winner,
    }
    assert last["next_baseline_measurement_id"] == winner
    assert [[c["attempt_id"] for c in entry["contrasts"]] for entry in entries] == [
        [_attempt_id(1)],
        [_attempt_id(2)],
    ]
    assert [entry["contrasts"][0]["accepted"] for entry in entries] == [False, True]
    if stability == "unstable":
        # Each finalist gets its own Champion member (R30).
        assert [entry["members"][0]["measurement_id"] for entry in entries] == [
            _member_id(1, None),
            _member_id(2, None),
        ]
        assert all(entry["stored_measurement_id"] is None for entry in entries)
    else:
        assert all([m["role"] for m in entry["members"]] == ["finalist"] for entry in entries)
        assert all(entry["stored_measurement_id"] == STORED for entry in entries)
    validate_final_entries(entries)


@pytest.mark.parametrize("stability", ["unstable", "stable"])
def test_first_accepted_with_no_accepted_finalist_retains_on_the_last_entry(stability):
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = _first_accepted(stability, winner=False)
    first, last = round_final_entries(_snapshot14(state), 1)
    retained = STORED if stability == "stable" else _member_id(2, None)
    assert last["selection"] == {
        "reason": "no_accepted_finalist",
        "attempt_id": None,
        "measurement_id": retained,
    }
    assert last["next_baseline_measurement_id"] == retained
    assert first["selection"] is None and first["next_baseline_measurement_id"] is None
    assert not any(entry["contrasts"][0]["accepted"] for entry in (first, last))
    validate_final_entries([first, last])
    # Selected but not yet closed: the selection shows, no next baseline.
    open_entries = round_final_entries(
        _snapshot14(_first_accepted(stability, winner=False, closed=False)), 1
    )
    assert open_entries[-1]["selection"] is not None
    assert open_entries[-1]["next_baseline_measurement_id"] is None
    validate_final_entries(open_entries)


def test_a_first_accepted_round_before_its_selection():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = replace(_first_accepted("unstable"), selection=None, closed=None)
    entries = round_final_entries(_snapshot14(state), 1)
    assert all(e["selection"] is None for e in entries)
    validate_final_entries(entries)


def test_removal_lists_and_summaries_are_reported_as_counts():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    summary = {"count": 65, "digest": "a" * 64}
    state = _stable_all(removed_pairs=summary, removed_cases=["c1", "c2"])
    (entry,) = round_final_entries(_snapshot14(state), 1)
    (contrast,) = entry["contrasts"]
    assert (contrast["removed_pairs"], contrast["removed_cases"]) == (65, 2)
    validate_final_entries([entry])
    state = _stable_all(removed_pairs=[["c1", 1], ["c2", 3]], removed_cases={**summary, "count": 3})
    (entry,) = round_final_entries(_snapshot14(state), 1)
    assert (entry["contrasts"][0]["removed_pairs"], entry["contrasts"][0]["removed_cases"]) == (
        2,
        3,
    )
    validate_final_entries([entry])


def test_a_rebound_member_and_a_continue_change_the_entry_in_place():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    binding = _binding(1, [1], seq=50)
    champion, finalist = binding["members"]
    moved = f"{finalist}@e1"
    state = _round14(
        finals=(binding,),
        finals_current=((champion, moved),),
        cohort_current=(champion, moved),
        cohort_completed={champion: _done14(champion, 60), moved: _done14(moved, 61)},
        global_comparisons={_attempt_id(1): _contrast14(1, moved, champion, 70)},
    )

    def entry(*changes):
        (built,) = round_final_entries(_snapshot14(state, condition_changes=changes), 1)
        validate_final_entries([built])
        return built

    assert entry(_change(55))["members"][1]["measurement_id"] == moved
    flags = [
        (e["condition_changed_in_place"], e["contrasts"][0]["condition_changed_in_place"])
        for e in (entry(), entry(_change(55)), entry(_change(75)), entry(_change(40)))
    ]
    assert flags == [(False, False), (True, True), (True, False), (False, False)]
    assert entry(_change(55, number=2))["condition_changed_in_place"] is False
    assert entry(_change(55, effect="rebaseline"))["condition_changed_in_place"] is False


@pytest.mark.parametrize("finalist_epoch,changed", [(1, True), (0, False)])
def test_a_stored_measurement_from_an_older_epoch_changes_the_entry_and_contrast(
    finalist_epoch, changed
):
    # Under `stable` the stored
    # measurement's older condition epoch is itself an in-place change, with
    # no `continue` here, and the entry covers its contrasts as in family 13.
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = _stable_all()
    measurements = {
        STORED: {"epoch": 0},
        _member_id(1, 1): {"epoch": finalist_epoch},
        _member_id(1, 2): {"epoch": finalist_epoch},
    }
    (entry,) = round_final_entries(_snapshot14(state, measurements=measurements), 1)
    assert entry["contrasts"][0]["condition_changed_in_place"] is changed
    assert entry["condition_changed_in_place"] is changed
    validate_final_entries([entry])


def test_a_stable_entry_changes_from_the_stored_epoch_before_any_contrast():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    state = replace(_stable_all(), global_comparisons={})
    first, second = _member_id(1, 1), _member_id(1, 2)
    # The second member is not bound yet: it has no registry entry and no epoch.
    measurements = {STORED: {"epoch": 0}, first: {"epoch": 1}}
    (entry,) = round_final_entries(_snapshot14(state, measurements=measurements), 1)
    assert entry["contrasts"] == []
    assert entry["condition_changed_in_place"] is True
    validate_final_entries([entry])
    same = {STORED: {"epoch": 0}, first: {"epoch": 0}}
    (entry,) = round_final_entries(_snapshot14(state, measurements=same), 1)
    assert entry["condition_changed_in_place"] is False
    assert second not in same


def test_listed_rows_are_filtered_per_entry():
    from evolution.tree.final_report import round_final_entries, validate_final_entries

    rows = tuple(
        {
            "measurement_id": mid,
            "case_id": "c1",
            "sample_id": "01",
            "retry_ordinal": ordinal,
            "owner": "target",
        }
        for ordinal, mid in enumerate((_member_id(1, 1), _member_id(2, None), STORED))
    )
    state = replace(_first_accepted("unstable"), cohort_listed=rows)
    first, last = round_final_entries(_snapshot14(state), 1)
    assert first["listed"] == [dict(rows[0])]
    assert last["listed"] == [dict(rows[1])]
    validate_final_entries([first, last])
    # A copy: the document never aliases the snapshot.
    first["listed"][0]["retry_ordinal"] = 9
    assert rows[0]["retry_ordinal"] == 0


def _valid_entries(stability="unstable"):
    from evolution.tree.final_report import round_final_entries

    rows = (
        {
            "measurement_id": _member_id(2, 2),
            "case_id": "c1",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "rollout",
        },
    )
    state = replace(_first_accepted(stability), cohort_listed=rows)
    return round_final_entries(_snapshot14(state), 1)


def _champion_entry():
    return {
        "measurement_id": _member_id(1, None),
        "role": "champion",
        "attempt_id": None,
        "complete": True,
        "point": 0.5,
        "missing": 0,
        "critical": False,
    }


def _rename(mapping, old, new):
    mapping[new] = mapping.pop(old)


@pytest.mark.parametrize(
    "stability,edit",
    [
        ("unstable", lambda e: e.append(None)),
        ("unstable", lambda e: e[0].update(cohort=None)),
        ("unstable", lambda e: e[0].pop("ordinal")),
        ("unstable", lambda e: _rename(e[0]["contrasts"][0], "accepted", "eligible")),
        ("unstable", lambda e: e[1]["contrasts"][0].update(unavailable="cohort_incomplete")),
        ("unstable", lambda e: e[0]["contrasts"][0].update(reason="cohort_incomplete")),
        ("unstable", lambda e: e[1]["contrasts"][0].update(reason="global_point_not_improved")),
        ("unstable", lambda e: e[0]["members"].pop(0)),  # a missing Champion
        ("unstable", lambda e: e[0]["members"][1].update(role="champion", attempt_id=None)),
        ("unstable", lambda e: e[0]["members"][1].update(complete=False)),  # with a point
        ("unstable", lambda e: e[0].update(stored_measurement_id=STORED)),
        ("unstable", lambda e: e[0].update(champion_side="stored")),
        ("unstable", lambda e: e[0].update(stability="steady")),
        ("unstable", lambda e: e[1].update(ordinal=1)),  # duplicate ordinals
        ("unstable", lambda e: e[0].update(ordinal=0)),
        ("unstable", lambda e: e.reverse()),
        ("unstable", lambda e: e[0].update(selection=dict(e[1]["selection"]))),
        ("unstable", lambda e: e[0].update(next_baseline_measurement_id=_member_id(1, 1))),
        ("unstable", lambda e: e[1]["selection"].update(measurement_id=_member_id(1, 1))),
        ("unstable", lambda e: e[1]["selection"].update(measurement_id=STORED)),
        ("unstable", lambda e: e[1]["selection"].update(attempt_id=_attempt_id(1))),
        ("unstable", lambda e: e[1]["selection"].update(reason="no_eligible_finalist")),
        ("unstable", lambda e: e[1].update(next_baseline_measurement_id=_member_id(2, None))),
        ("unstable", lambda e: e[1]["contrasts"][0].update(removed_pairs=1)),  # fresh removes none
        ("unstable", lambda e: e[1]["contrasts"][0].update(removed_cases=[])),
        ("unstable", lambda e: e[1]["contrasts"][0].update(exact=True)),  # without a level
        ("unstable", lambda e: e[1]["contrasts"][0].update(condition_changed_in_place=True)),
        ("unstable", lambda e: e[1]["contrasts"].append(dict(e[1]["contrasts"][0]))),
        ("unstable", lambda e: e[1]["contrasts"][0].update(attempt_id=_attempt_id(1))),
        ("unstable", lambda e: e[1]["listed"][0].update(measurement_id=_member_id(1, 1))),
        ("unstable", lambda e: e[1]["listed"][0].update(owner="editor")),
        ("unstable", lambda e: e[1].update(condition_changed_in_place=None)),
        ("stable", lambda e: e[0]["members"].insert(0, _champion_entry())),
        ("stable", lambda e: e[0].update(stored_measurement_id=None)),
        ("stable", lambda e: e[0].update(champion_side="fresh")),
        ("stable", lambda e: e[1]["selection"].update(measurement_id=_member_id(1, 1))),
        ("stable", lambda e: e[1]["contrasts"][0].update(removed_pairs=-1)),
        # Family 13's implication holds under `stable` too.
        ("stable", lambda e: e[0]["contrasts"][0].update(condition_changed_in_place=True)),
        # R34: the stored id names a retention only, never a selected finalist.
        ("stable", lambda e: e[1]["selection"].update(measurement_id=STORED)),
        ("stable", lambda e: e[1]["contrasts"][0].update(unavailable=["final_incomplete"])),
        ("stable", lambda e: e[1]["contrasts"][0].update(removed_cases=True)),
    ],
)
def test_validate_final_entries_refuses_a_bad_shape(stability, edit):
    from evolution.tree.final_report import validate_final_entries

    entries = _valid_entries(stability)
    validate_final_entries(entries)
    edit(entries)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_final_entries(entries)


@pytest.mark.parametrize("value", [None, {}, ()])
def test_the_final_block_is_a_list_never_null(value):
    from evolution.tree.final_report import validate_final_entries

    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_final_entries(value)


def test_validate_final_entries_admits_stored_shapes():
    from evolution.tree.final_report import validate_final_entries

    entries = _valid_entries("stable")
    # Plan choice 8: a stored contrast changed in place, and so its entry.
    entries[0]["contrasts"][0]["condition_changed_in_place"] = True
    entries[0]["condition_changed_in_place"] = True
    # Plan choice 5: removals are counts; an incomplete finalist leads a stable entry.
    entries[1]["contrasts"][0].update(removed_pairs=65, removed_cases=2)
    entries[0]["members"][0].update(complete=False, point=None, missing=1)
    validate_final_entries(entries)
    # R34: a stable retained selection may name the stored measurement.
    entries[1]["selection"].update(
        measurement_id=STORED, attempt_id=None, reason="no_accepted_finalist"
    )
    entries[1]["next_baseline_measurement_id"] = STORED
    validate_final_entries(entries)


def test_an_exact_statistical_contrast_validates():
    from evolution.tree.final_report import validate_final_entries

    entries = _valid_entries()
    entries[1]["contrasts"][0].update(
        lower=0.25, level=0.9, unavailable=None, exact=True, reason="global_bound_positive"
    )
    validate_final_entries(entries)
    entries[1]["contrasts"][0].update(lower=0.125)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_final_entries(entries)


# -- Task 4: the `roles`, `settings` and `connection` blocks (choices 9-11) ------

CHAMPION0 = "m-r00-champion"
SHA = "a" * 64


def _snapshot_of(**fields):
    """A hand-built family-14 snapshot; the blocks read only what is named."""

    from evolution.tree.campaign_schema import CampaignSnapshot

    base = {
        "identity": {},
        "run_id": "c-test",
        "alpha_plan": {},
        "rounds": {},
        "champion": ("v0", "0" * 64),
        "champion_dependency_digest": "0" * 64,
        "measurements": {},
        "accounted": frozenset(),
        "counts": {"target": 0, "environment": 0, "scorer": 0, "rollout": 0},
        "denominator": 0,
        "alpha_rounds": {},
        "threshold": None,
        "finished": None,
        "capacity": None,
        "first_round": 1,
        "total_rounds": 6,
        "family_version": 14,
    }
    return CampaignSnapshot(**{**base, **fields})


def test_roles_block_reads_the_three_kinds():
    from evolution.tree.final_report import ROLES_REPORT_KEYS, roles_block, validate_roles_block

    snapshot = _snapshot_of(
        identity={"reflector": {"kind": "agent"}, "editor": {"kind": "chat"}},
        grouper_kind="agent_cli",
    )
    block = roles_block(snapshot)
    assert set(block) == ROLES_REPORT_KEYS == {"grouper", "reflector", "editor"}
    assert block == {
        "grouper": {"kind": "agent_cli"},
        "reflector": {"kind": "agent"},
        "editor": {"kind": "chat"},
    }
    validate_roles_block(block)


def _valid_roles():
    return {
        "grouper": {"kind": "fields"},
        "reflector": {"kind": "internal"},
        "editor": {"kind": "internal"},
    }


@pytest.mark.parametrize(
    "edit",
    [
        lambda b: b.pop("grouper"),
        lambda b: b.update(extra={"kind": "x"}),
        lambda b: b.update(grouper=None),
        lambda b: b.update(grouper="fields"),
        lambda b: b["editor"].update(digest=SHA),
        lambda b: b["reflector"].pop("kind"),
        lambda b: b["grouper"].update(kind=None),
        lambda b: b["grouper"].update(kind=""),
        lambda b: b["grouper"].update(kind=3),
        lambda b: b["grouper"].update(kind="two words"),
    ],
)
def test_validate_roles_block_refuses_a_bad_shape(edit):
    from evolution.tree.final_report import validate_roles_block

    block = _valid_roles()
    validate_roles_block(block)
    edit(block)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_roles_block(block)


def _policy14(stability="unstable", round_stop="all_attempts", refresh=2):
    return {
        "gate": {"stability": stability},
        "round_stop": round_stop,
        "baseline_refresh_after_rounds": refresh,
    }


def _r(number, baseline, outcome=None, selected=None):
    """A round on `baseline`, closed `outcome` (open when `None`), selecting `selected`."""

    from evolution.tree.campaign_schema import RoundState

    return RoundState(
        round=number,
        baseline_measurement_id=baseline,
        baseline_complete=True,
        closed=None if outcome is None else {"outcome": outcome, "reason": None},
        selection=(
            None
            if selected is None
            else {"attempt_id": None, "measurement_id": selected, "reason": "x"}
        ),
    )


def _refresh(number, *, completed=False, replaced=CHAMPION0):
    from evolution.tree.campaign_identity import refresh_measurement_id_for

    mid = refresh_measurement_id_for(number)
    return {
        "round": number,
        "measurement_id": mid,
        "baseline_refresh_after_rounds": 2,
        "counted_rounds": {"first": number - 2, "last": number - 1},
        "replaced": {"measurement_id": replaced, "prefix_digest": "e" * 64},
        "seq": 10 * number,
        "completed": {"measurement_id": mid, "seq": 10 * number + 5} if completed else None,
    }


def _settings_snapshot(*rounds, refreshes=(), rebaseline_from_round=None, epochs=1, **policy):
    return _snapshot_of(
        identity={"policy": _policy14(**policy)},
        rounds={state.round: state for state in rounds},
        refreshes=tuple(refreshes),
        rebaseline_from_round=rebaseline_from_round,
        epochs=tuple({} for _ in range(epochs)),
    )


def _final_member(ordinal, index):
    from evolution.tree.campaign_identity import final_measurement_id_for

    return final_measurement_id_for(1, ordinal, index)


REFRESH3 = "m-r03-refresh"
FRESH3 = "m-r03-champion@e1"
#: Plan choice 10: the count of closed rounds on the current stored Champion
#: measurement, for N = 2, with the planner's own next-baseline rule.
_COUNTS = {
    "no round": ((), {}, 0),
    "round 1 open": ((_r(1, CHAMPION0),), {}, 0),
    "one close": ((_r(1, CHAMPION0, "stalled"),), {}, 1),
    "one close, round 2 open": ((_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0)), {}, 1),
    "two closes": ((_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "stalled")), {}, 2),
    "a stable retention keeps the stored measurement": (
        (_r(1, CHAMPION0, "retained", CHAMPION0),),
        {},
        1,
    ),
    "a Promotion": ((_r(1, CHAMPION0, "promoted", _final_member(1, 1)),), {}, 0),
    "an unstable retention": ((_r(1, CHAMPION0, "retained", _final_member(1, None)),), {}, 0),
    "a bound refresh before its baseline": (
        (_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "stalled")),
        {"refreshes": [_refresh(3)]},
        0,
    ),
    "the refresh round open": (
        (_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "stalled"), _r(3, REFRESH3)),
        {"refreshes": [_refresh(3)]},
        0,
    ),
    "a completed refresh's round closed": (
        (_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "stalled"), _r(3, REFRESH3, "stalled")),
        {"refreshes": [_refresh(3, completed=True)]},
        1,
    ),
    "a rebaseline before round 3": (
        (_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "condition_changed")),
        {"rebaseline_from_round": 3, "epochs": 2},
        0,
    ),
    "the rebaseline round closed": (
        (
            _r(1, CHAMPION0, "stalled"),
            _r(2, CHAMPION0, "condition_changed"),
            _r(3, FRESH3, "stalled"),
        ),
        {"rebaseline_from_round": 3, "epochs": 2},
        1,
    ),
    "a continue keeps the count": (
        (_r(1, CHAMPION0, "stalled"), _r(2, f"{CHAMPION0}@e1", "stalled")),
        {"epochs": 2},
        2,
    ),
}


@pytest.mark.parametrize("name", list(_COUNTS))
def test_the_refresh_count_follows_the_stored_measurement(name):
    from evolution.tree.final_report import settings_block, validate_settings_block

    rounds, extra, count = _COUNTS[name]
    block = settings_block(_settings_snapshot(*rounds, **extra))
    assert block["baseline_refresh"]["every_rounds"] == 2
    assert block["baseline_refresh"]["count"] == count
    validate_settings_block(block)


def test_settings_off_without_a_refresh():
    from evolution.tree.final_report import (
        SETTINGS_REPORT_KEYS,
        settings_block,
        validate_settings_block,
    )

    snapshot = _settings_snapshot(_r(1, CHAMPION0, "stalled"), refresh=0)
    block = settings_block(snapshot)
    assert set(block) == SETTINGS_REPORT_KEYS
    # ADR-0032: `baseline_refresh_after_rounds: 0` is reported as `baseline_refresh: off`.
    assert block == {
        "stability": "unstable",
        "round_stop": "all_attempts",
        "baseline_refresh": "off",
    }
    validate_settings_block(block)


def test_settings_show_the_performed_refreshes_and_the_policy_in_force():
    from evolution.tree.final_report import settings_block, validate_settings_block

    closes = (_r(1, CHAMPION0, "stalled"), _r(2, CHAMPION0, "stalled"))
    bound = settings_block(
        _settings_snapshot(
            *closes, refreshes=[_refresh(3)], stability="stable", round_stop="first_accepted"
        )
    )
    assert (bound["stability"], bound["round_stop"]) == ("stable", "first_accepted")
    assert bound["baseline_refresh"] == {
        "every_rounds": 2,
        "count": 0,
        "performed": [
            {
                "round": 3,
                "measurement_id": REFRESH3,
                "replaced_measurement_id": CHAMPION0,
                "completed": False,
            }
        ],
    }
    validate_settings_block(bound)
    done = settings_block(
        _settings_snapshot(*closes, _r(3, REFRESH3), refreshes=[_refresh(3, completed=True)])
    )
    assert done["baseline_refresh"]["performed"][0]["completed"] is True
    validate_settings_block(done)


def test_the_settings_block_does_not_alias_the_snapshot():
    from evolution.tree.final_report import settings_block

    snapshot = _settings_snapshot(_r(1, CHAMPION0, "stalled"), refreshes=[_refresh(2)])
    settings_block(snapshot)["baseline_refresh"]["performed"][0]["round"] = 9
    assert settings_block(snapshot)["baseline_refresh"]["performed"][0]["round"] == 2


def _valid_settings():
    return {
        "stability": "stable",
        "round_stop": "first_accepted",
        "baseline_refresh": {
            "every_rounds": 2,
            "count": 1,
            "performed": [
                {
                    "round": 3,
                    "measurement_id": REFRESH3,
                    "replaced_measurement_id": CHAMPION0,
                    "completed": True,
                },
                {
                    "round": 6,
                    "measurement_id": "m-r06-refresh",
                    "replaced_measurement_id": REFRESH3,
                    "completed": False,
                },
            ],
        },
    }


def _refresh_of(block):
    return block["baseline_refresh"]


@pytest.mark.parametrize(
    "edit",
    [
        lambda b: b.pop("round_stop"),
        lambda b: b.update(extra=1),
        lambda b: b.update(stability="volatile"),
        lambda b: b.update(round_stop="first"),
        lambda b: b.update(baseline_refresh="on"),
        lambda b: b.update(baseline_refresh=None),
        lambda b: b.update(baseline_refresh=0),
        lambda b: _refresh_of(b).pop("performed"),
        lambda b: _refresh_of(b).update(every_rounds=0),
        lambda b: _refresh_of(b).update(every_rounds=True),
        lambda b: _refresh_of(b).update(count=-1),
        lambda b: _refresh_of(b).update(count=1.0),
        lambda b: _refresh_of(b).update(performed=None),
        lambda b: _refresh_of(b)["performed"][0].pop("completed"),
        lambda b: _refresh_of(b)["performed"][0].update(completed=None),
        lambda b: _refresh_of(b)["performed"][0].update(round=0),
        lambda b: _refresh_of(b)["performed"][0].update(measurement_id=None),
        lambda b: _refresh_of(b)["performed"][1].update(replaced_measurement_id=""),
        lambda b: _refresh_of(b)["performed"].reverse(),
    ],
)
def test_validate_settings_block_refuses_a_bad_shape(edit):
    from evolution.tree.final_report import validate_settings_block

    block = _valid_settings()
    validate_settings_block(block)
    edit(block)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_settings_block(block)


def _connection(state="not_started", outcome=None, detail=None, attempts=None):
    started = state != "not_started"
    return {
        "state": state,
        "binding_digest": SHA if started else None,
        "operation_id": "cp-0001" if started else None,
        "attempts": (1 if started else 0) if attempts is None else attempts,
        "outcome": outcome,
        "detail": detail,
    }


INCOMPATIBLE_DETAIL = {
    "code": "connection_field_missing",
    "metric": "quality",
    "manifest_path": "scorer.metric_contract.target_fields[0]",
    "observed_path": "events[event=assistant_message,last]/content",
}
VALID_CONNECTIONS = {
    "not started": _connection(),
    "open start": _connection("running"),
    "compatible": _connection("compatible", "compatible", attempts=2),
    "interrupted": _connection("interrupted", "interrupted"),
    "owed incompatible": _connection("running", "connection_incompatible", INCOMPATIBLE_DETAIL),
    "suspended incompatible": _connection(
        "suspended", "connection_incompatible", INCOMPATIBLE_DETAIL
    ),
    "suspended unavailable": _connection(
        "suspended", "connection_preflight_unavailable", {"cause_code": "cleanup_failed"}
    ),
    "owed inconclusive": _connection(
        "running", "connection_preflight_inconclusive", {"cause_code": "turn_budget"}
    ),
}


@pytest.mark.parametrize("name", list(VALID_CONNECTIONS))
def test_validate_connection_block_admits_c63s_shapes(name):
    from evolution.tree.final_report import CONNECTION_REPORT_KEYS, validate_connection_block

    block = copy.deepcopy(VALID_CONNECTIONS[name])
    assert set(block) == CONNECTION_REPORT_KEYS
    validate_connection_block(block)


@pytest.mark.parametrize(
    ("name", "edit"),
    [
        ("compatible", lambda b: b.pop("detail")),
        ("compatible", lambda b: b.update(extra=None)),
        ("compatible", lambda b: b.update(state="ready")),
        ("compatible", lambda b: b.update(outcome="passed")),
        ("compatible", lambda b: b.update(outcome="interrupted")),
        ("compatible", lambda b: b.update(detail={"cause_code": "turn_budget"})),
        ("compatible", lambda b: b.update(attempts=-1)),
        ("compatible", lambda b: b.update(attempts=0)),
        ("compatible", lambda b: b.update(attempts=True)),
        ("compatible", lambda b: b.update(binding_digest="A" * 64)),
        ("compatible", lambda b: b.update(binding_digest=None)),
        ("compatible", lambda b: b.update(operation_id=None)),
        ("not started", lambda b: b.update(attempts=1)),
        ("not started", lambda b: b.update(operation_id="cp-0001")),
        ("not started", lambda b: b.update(outcome="compatible")),
        ("open start", lambda b: b.update(outcome="compatible")),
        ("interrupted", lambda b: b.update(outcome="compatible")),
        ("interrupted", lambda b: b.update(detail={"cause_code": "cancelled"})),
        ("suspended incompatible", lambda b: b.update(outcome=None, detail=None)),
        ("suspended incompatible", lambda b: b.update(detail={"cause_code": "metric_type"})),
        ("suspended incompatible", lambda b: b.update(detail=None)),
        ("suspended incompatible", lambda b: b["detail"].update(code="connection_unknown")),
        ("suspended incompatible", lambda b: b["detail"].update(metric=None)),
        ("suspended incompatible", lambda b: b["detail"].update(observed_path="x" * 2049)),
        ("suspended incompatible", lambda b: b["detail"].pop("manifest_path")),
        ("suspended incompatible", lambda b: b["detail"].update(cause_code=None)),
        ("suspended unavailable", lambda b: b.update(detail=INCOMPATIBLE_DETAIL)),
        ("suspended unavailable", lambda b: b.update(detail={"cause_code": "turn_budget"})),
        ("suspended unavailable", lambda b: b.update(detail={"cause_code": None})),
        ("suspended unavailable", lambda b: b.update(detail=None)),
        ("owed inconclusive", lambda b: b.update(detail={"cause_code": "target_failed"})),
        ("owed inconclusive", lambda b: b.update(state="compatible")),
    ],
)
def test_validate_connection_block_refuses_a_bad_shape(name, edit):
    from evolution.tree.final_report import validate_connection_block

    block = copy.deepcopy(VALID_CONNECTIONS[name])
    edit(block)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        validate_connection_block(block)


def test_a_path_of_exactly_2048_bytes_validates():
    from evolution.tree.final_report import validate_connection_block

    block = copy.deepcopy(VALID_CONNECTIONS["suspended incompatible"])
    block["detail"]["observed_path"] = "é" * 1024
    validate_connection_block(block)


# -- Task 5: family-14 caveats round-trip through their validator (choices 12-13) -


def test_family14_caveats_round_trip_through_the_family14_validator():
    from types import SimpleNamespace

    from evolution.tree.affected_report import affected_caveats
    from evolution.tree.campaign_projections import _validate_caveats

    state = _first_accepted("stable")
    comparisons = dict(state.global_comparisons)
    first, second = _attempt_id(1), _attempt_id(2)
    # Attempt 1's stored contrast removed a pair; Attempt 2 won exactly.
    comparisons[first] = {**comparisons[first], "removed_pairs": [["c1", 0]]}
    comparisons[second] = {
        **comparisons[second],
        "lower": 0.25,
        "level": 0.9,
        "unavailable": None,
        "exact": True,
    }
    looks = {1: {"caveats": ["intervals_empirical"]}}
    attempts = {
        name: _attempt(attempt_id=name, index=index, looks=looks)
        for index, name in enumerate((first, second), start=1)
    }
    finals = tuple({**final, "mode": "statistical"} for final in state.finals)
    state = replace(
        state,
        finals=finals,
        cohort=finals[-1],
        global_comparisons=comparisons,
        attempts=attempts,
    )
    snapshot = _snapshot14(state)
    policies = {1: SimpleNamespace(fresh_parent=True)}
    caveats = affected_caveats(snapshot, policies, {}, final=True)
    assert caveats == [
        {"kind": "champion_not_contemporaneous", "round": None, "attempt_id": None},
        {"kind": "intervals_empirical", "round": 1, "attempt_id": first},
        {
            "kind": "stored_pairs_excluded",
            "round": 1,
            "attempt_id": first,
            "measurement_id": STORED,
            "count": 1,
        },
        {"kind": "intervals_empirical", "round": 1, "attempt_id": second},
        {"kind": "exact_contrast", "round": 1, "attempt_id": second},
    ]
    order = {first: (1, 0), second: (1, 1)}
    _validate_caveats(caveats, attempt_order=order, confidence=True, final=True)
    with pytest.raises(SchemaError, match="^invalid public projection$"):
        _validate_caveats(caveats, attempt_order=order, confidence=True)
