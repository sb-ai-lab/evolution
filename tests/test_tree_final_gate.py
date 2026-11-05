"""Family-14 gate estimates derive exactness from retained sample rows."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from evolution.tree.campaign_schema import SchemaError, campaign_family_for
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.confidence_gate import GateInvariant, look_record

KEYS = {"point", "lower", "upper", "level", "unavailable"}
PREFIXES = {
    "candidate": "measurements/m-candidate/events.jsonl#bytes=0:4096",
    "parent": "measurements/m-parent/events.jsonl#bytes=0:4096",
}


def _rows(values, *, paired=False):
    return [
        SampleRow(
            case_id=case_id,
            sample_id=f"{repeat:02d}",
            repeat=repeat,
            requested_seed=repeat if paired else None,
            effective_seed=repeat if paired else None,
            boot_id="boot",
            dependency_digest="deps",
            outcome="rollout_finished",
            disposition="score",
            metrics={"quality": score},
            critical_pass=True,
        )
        for case_id, scores in values.items()
        for repeat, score in enumerate(scores, 1)
    ]


def _look(
    candidate,
    parent,
    *,
    look=3,
    mode="statistical",
    paired=False,
    excluded=(),
    focus=(0,),
    causal=(1,),
    family_version=14,
    draws=64,
    margin=0,
    tolerance=0.5,
    calibration=None,
    critical_case=None,
):
    corpus = list(candidate)
    comparison = {
        "round": 1,
        "attempt_id": "r01-a01",
        "seq": 7,
        "digest": "a" * 64,
        "mode": mode,
        "margin": margin,
        "parent_source": "fresh_parent",
        "focus": list(focus),
        "causal": list(causal),
        "retained": {"focus": list(focus), "causal": list(causal)},
        "looks": [3] if mode == "observe" else [2, 3],
        "confidence": None if mode == "observe" else {"focus": 0.9, "causal": 0.9},
        "tolerance": (
            {"kind": "measured_noise"}
            if tolerance is None
            else {"kind": "numeric", "value": tolerance}
        ),
    }
    context = {} if family_version is None else {"family_version": family_version}
    record = look_record(
        comparison,
        calibration,
        look=look,
        candidate_rows=[
            replace(row, critical_pass=False) if row.case_id == critical_case else row
            for row in _rows(candidate, paired=paired)
        ],
        parent_rows=_rows(parent, paired=paired),
        excluded=frozenset(excluded),
        groups={case: (f"g-{case}", "s") for case in corpus},
        corpus_ids=corpus,
        primary="quality",
        prefixes=PREFIXES,
        campaign_seed=11,
        draws=draws,
        **context,
    )
    record["evidence"] = [
        {
            "version": 1,
            "kind": "journal_prefix",
            "run_id": "test-run",
            "locator": locator,
            "content_digest": "b" * 64,
        }
        for locator in PREFIXES.values()
    ]
    return record


@pytest.mark.parametrize("look", [2, 3])
@pytest.mark.parametrize("paired", [False, True])
def test_constant_sides_publish_exact_focus_and_causal_without_overall_flag(look, paired):
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5] * 3},
        {"f": [0.25] * 3, "c": [0.25] * 3},
        look=look,
        paired=paired,
    )
    assert payload["focus"] == {
        "point": 0.5,
        "lower": 0.5,
        "upper": 0.5 if look == 2 else None,
        "level": 0.9,
        "unavailable": None,
        "exact": True,
    }
    if look == 3:
        assert payload["causal"] == {
            "point": 0.25,
            "lower": 0.25,
            "upper": None,
            "level": 0.9,
            "unavailable": None,
            "exact": True,
        }
        assert set(payload["overall"]) == KEYS
        assert payload["overall"]["unavailable"] is None
        assert payload["decision"] == "finalist"
    else:
        assert payload["causal"] is payload["overall"] is None
        assert payload["decision"] == "continue"
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("look", [2, 3])
def test_paired_varying_sides_with_constant_difference_are_exact(look):
    payload = _look(
        {"f": [0.5, 0.75, 1], "c": [0.25, 0.5, 0.75]},
        {"f": [0.25, 0.5, 0.75], "c": [0.25, 0.5, 0.75]},
        paired=True,
        look=look,
    )
    assert payload["focus"]["exact"] is True
    assert payload["focus"]["lower"] == payload["focus"]["point"] == 0.25
    if look == 3:
        assert payload["causal"]["exact"] is True
        assert payload["causal"]["lower"] == payload["causal"]["point"] == 0


@pytest.mark.parametrize("exact_focus", [True, False])
def test_exactness_is_per_scope_in_a_shared_final_draw_stream(exact_focus):
    constant = ([0.75] * 3, [0.25] * 3)
    varying = ([0.5, 0.75, 1], [0.25, 0.5, 0.25])
    f, c = (constant, varying) if exact_focus else (varying, constant)
    payload = _look({"f": f[0], "c": c[0]}, {"f": f[1], "c": c[1]})
    assert payload["focus"]["exact"] is exact_focus
    assert payload["causal"]["exact"] is (not exact_focus)
    assert payload["focus"]["unavailable"] is payload["causal"]["unavailable"] is None


@pytest.mark.parametrize("look", [2, 3])
def test_unpaired_equal_multisets_still_have_variable_contrasts(look):
    payload = _look(
        {"f": [0, 1, 0.5], "c": [0, 1, 0.5]}, {"f": [0, 1, 0.5], "c": [0, 1, 0.5]}, look=look
    )
    assert payload["focus"]["exact"] is False
    if look == 3:
        assert payload["causal"]["exact"] is False


def test_equal_sampled_endpoints_do_not_establish_exactness():
    payload = _look({"f": [0, 1, 0.5]}, {"f": [0, 1, 0.5]}, causal=(), draws=1)
    assert payload["focus"]["exact"] is False
    assert payload["focus"]["unavailable"] is None


def test_observe_estimates_have_null_exact_and_overall_has_five_keys():
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5] * 3}, {"f": [0.25] * 3, "c": [0.25] * 3}, mode="observe"
    )
    assert payload["focus"]["exact"] is payload["causal"]["exact"] is None
    assert set(payload["overall"]) == KEYS
    assert payload["decision"] == "finalist"
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("observed", [True, False])
def test_unobserved_interim_case_uses_renormalized_point_and_false_exact(observed):
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5] * 3},
        {"f": [0.25] * 3, "c": [0.25] * 3},
        look=2,
        focus=(0, 1),
        causal=(),
        excluded=[("c", 1), ("c", 2)] + ([] if observed else [("f", 1), ("f", 2)]),
    )
    assert payload["focus"] == {
        "point": 0.5 if observed else None,
        "lower": None,
        "upper": None,
        "level": 0.9,
        "unavailable": "insufficient_repeats",
        "exact": False,
    }
    assert (payload["decision"], payload["reason"]) == ("continue", "interval_unavailable")
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


def test_only_one_usable_repeat_is_insufficient_and_never_exact():
    payload = _look({"f": [0.75] * 3}, {"f": [0.25] * 3}, causal=(), excluded=[("f", 1), ("f", 2)])
    assert payload["focus"]["unavailable"] == "insufficient_repeats"
    assert payload["focus"]["exact"] is False
    assert payload["decision"] == "insufficient_evidence"


def test_final_unobserved_case_is_an_invariant_instead_of_fabricated_score():
    with pytest.raises(GateInvariant, match="final look has an unobserved retained case"):
        _look(
            {"f": [0.75] * 3}, {"f": [0.25] * 3}, causal=(), excluded=[("f", 1), ("f", 2), ("f", 3)]
        )


def _published():
    """Independent literal six-key payload for closed grammar validation."""
    return {
        "round": 1,
        "attempt_id": "r01-a01",
        "comparison": 7,
        "look": 3,
        "final": True,
        "prefixes": dict(PREFIXES),
        "focus": {
            "point": 0.5,
            "lower": 0.5,
            "upper": None,
            "level": 0.9,
            "unavailable": None,
            "exact": True,
        },
        "causal": None,
        "overall": {"point": 0.5, "lower": 0.5, "upper": None, "level": 0.9, "unavailable": None},
        "tolerance": {"source": "numeric", "value": 0},
        "decision": "finalist",
        "reason": "gate_passed",
        "would_pass_point": None,
        "caveats": ["intervals_empirical"],
        "evidence": [
            {
                "version": 1,
                "kind": "journal_prefix",
                "run_id": "test-run",
                "locator": locator,
                "content_digest": "b" * 64,
            }
            for locator in PREFIXES.values()
        ],
    }


def test_family14_six_key_gate_shape_and_five_key_overall_are_admitted():
    campaign_family_for(14).validate_payload("comparison_look_evaluated", _published())


@pytest.mark.parametrize(
    "changes",
    [
        {"exact": 1},
        {"exact": None},
        {"exact": "true"},
        {"lower": 0.4},
        {"upper": 0.6},
        {"point": True},
        {"lower": True},
        {"upper": True},
        {"level": True},
        {"lower": float("nan")},
        {"lower": float("inf")},
        {"exact": False, "lower": 0.6, "upper": 0.4},
        {"unavailable": "degenerate_interval"},
        {"unavailable": "insufficient_repeats", "lower": None, "exact": True},
        {"unknown": False},
    ],
)
def test_family14_refuses_inconsistent_or_invalid_gate_estimates(changes):
    payload = _published()
    payload["focus"].update(changes)
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("scope", ["focus", "causal", "overall"])
def test_estimate_exact_field_presence_is_closed_by_role_and_family(scope):
    payload = _published()
    payload["causal"] = copy.deepcopy(payload["focus"])
    if scope == "overall":
        payload[scope]["exact"] = True
    else:
        del payload[scope]["exact"]
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("scope", ["focus", "causal", "overall"])
@pytest.mark.parametrize("unavailable", ["final_incomplete", "bound_not_requested_in_observe_mode"])
def test_gate_estimates_refuse_global_only_unavailability(scope, unavailable):
    payload = _published()
    payload["causal"] = copy.deepcopy(payload["focus"])
    payload[scope].update(unavailable=unavailable)
    if scope != "overall":
        payload[scope]["exact"] = False
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


def test_family14_negative_row_result_uses_its_closed_rejection_vocabulary():
    payload = _look({"f": [0, 1, 0.5]}, {"f": [0, 1, 0.5]}, causal=())
    assert (payload["decision"], payload["reason"]) == ("rejected", "focus_below_margin")
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("unavailable", [[], {}, False, 0])
def test_nonstring_unavailability_is_a_schema_refusal(unavailable):
    payload = _published()
    payload["focus"].update(exact=False, unavailable=unavailable)
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("look", [2, 3])
def test_exclusions_remove_variation_before_exactness_is_computed(look):
    payload = _look(
        {"f": [0.75, 0.75, 0]}, {"f": [0.25, 0.25, 1]}, causal=(), excluded=[("f", 3)], look=look
    )
    assert payload["focus"]["exact"] is True
    assert payload["focus"]["point"] == payload["focus"]["lower"] == 0.5


def test_different_constant_case_contrasts_still_form_an_exact_scope():
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5] * 3},
        {"f": [0.25] * 3, "c": [0.25] * 3},
        focus=(0, 1),
        causal=(),
    )
    assert payload["focus"]["exact"] is True
    assert payload["focus"]["point"] == payload["focus"]["lower"] == 0.375


@pytest.mark.parametrize("margin", [-0.25, 0, 0.25])
@pytest.mark.parametrize("look", [2, 3], ids=["interim", "final"])
@pytest.mark.parametrize("exact", [False, True], ids=["variable", "exact"])
@pytest.mark.parametrize(
    "offset,final_decision,interim_decision,reason",
    [
        (-0.0625, "rejected", "rejected", "focus_below_margin"),
        (0, "rejected", "rejected", "focus_below_margin"),
        (0.0625, "finalist", "continue", None),
    ],
    ids=["below", "equal", "above"],
)
def test_real_rows_decide_focus_at_bound_margin(
    margin, look, exact, offset, final_decision, interim_decision, reason
):
    # Catch using zero instead of the binding, >= at final, or early acceptance.
    boundary = margin + offset
    parent = 0.625 if look == 2 else 0.375
    differences = (
        [boundary] * 3
        if exact
        else [boundary - 0.25, boundary]
        if look == 2
        else [boundary, boundary + 0.25, boundary]
    )
    payload = _look(
        {"f": [parent + value for value in differences]},
        {"f": [parent] * len(differences)},
        look=look,
        causal=(),
        margin=margin,
    )
    estimate = payload["focus"]
    assert estimate["exact"] is exact
    assert estimate["point" if exact else "upper" if look == 2 else "lower"] == boundary
    assert estimate["unavailable"] is None
    expected = final_decision if look == 3 else interim_decision
    expected_reason = "gate_passed" if expected == "finalist" else reason
    assert (payload["decision"], payload["reason"]) == (expected, expected_reason)
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize("exact", [False, True], ids=["variable", "exact"])
@pytest.mark.parametrize("tolerance", [0, 0.25])
@pytest.mark.parametrize(
    "offset,decision,reason",
    [
        (-0.0625, "rejected", "causal_below_tolerance"),
        (0, "finalist", "gate_passed"),
        (0.0625, "finalist", "gate_passed"),
    ],
    ids=["below", "equal", "above"],
)
def test_real_rows_causal_tolerance_boundary_is_inclusive(
    exact, tolerance, offset, decision, reason
):
    boundary = -tolerance + offset
    differences = [boundary] * 3 if exact else [boundary, boundary + 0.25, boundary]
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5 + value for value in differences]},
        {"f": [0.25] * 3, "c": [0.5] * len(differences)},
        margin=0.25,
        tolerance=tolerance,
    )
    assert payload["causal"]["exact"] is exact
    assert payload["causal"]["lower"] == boundary
    assert (payload["decision"], payload["reason"]) == (decision, reason)


@pytest.mark.parametrize(
    "critical,insufficient,focus,causal,decision,reason",
    [
        (True, "both", 0, -0.5, "rejected", "critical_failure"),
        (False, "focus", 0, -0.5, "insufficient_evidence", "insufficient_repeats"),
        (False, "causal", 0, -0.5, "insufficient_evidence", "insufficient_repeats"),
        (False, None, 0, -0.5, "rejected", "focus_below_margin"),
        (False, None, 0.5, -0.5, "rejected", "causal_below_tolerance"),
        (False, None, 0.5, -0.25, "finalist", "gate_passed"),
    ],
)
def test_real_rows_gate_priority(critical, insufficient, focus, causal, decision, reason):
    excluded = [
        (case, repeat)
        for case, scope in (("f", "focus"), ("c", "causal"))
        if insufficient in (scope, "both")
        for repeat in (1, 2)
    ]
    payload = _look(
        {"f": [0.25 + focus] * 3, "c": [0.75 + causal] * 3, "guard": [0.5] * 3},
        {"f": [0.25] * 3, "c": [0.75] * 3, "guard": [0.5] * 3},
        excluded=excluded,
        tolerance=0.25,
        critical_case="guard" if critical else None,
    )
    assert (payload["decision"], payload["reason"]) == (decision, reason)


@pytest.mark.parametrize("critical", [False, True])
def test_real_rows_interim_critical_precedes_insufficient_interval(critical):
    payload = _look(
        {"f": [0.25] * 3},
        {"f": [0.75] * 3},
        look=2,
        causal=(),
        excluded=[("f", 1)],
        critical_case="f" if critical else None,
    )
    assert payload["focus"]["unavailable"] == "insufficient_repeats"
    assert (payload["decision"], payload["reason"]) == (
        ("rejected", "critical_failure") if critical else ("continue", "interval_unavailable")
    )


@pytest.mark.parametrize("margin", [-0.25, 0, 0.25])
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("diagnostic", [False, True, None])
def test_real_rows_observe_diagnostic_cannot_select_outcome(margin, critical, diagnostic):
    focus = margin + (0.125 if diagnostic is True else 0)
    unavailable = {
        "allowance": None,
        "degenerate": False,
        "unavailable": "insufficient_calibration_repeats",
    }
    payload = _look(
        {"f": [0.5 + focus] * 3, "c": [0.5] * 3},
        {"f": [0.5] * 3, "c": [0.5] * 3},
        mode="observe",
        margin=margin,
        tolerance=None if diagnostic is None else 0,
        calibration=unavailable if diagnostic is None else None,
        critical_case="f" if critical else None,
    )
    assert payload["would_pass_point"] is diagnostic
    assert payload["focus"]["exact"] is payload["causal"]["exact"] is None
    assert (payload["decision"], payload["reason"]) == (
        ("rejected", "critical_failure") if critical else ("finalist", "observe_finalist")
    )


@pytest.mark.parametrize("tolerance", [0, 0.25])
@pytest.mark.parametrize("offset,diagnostic", [(-0.0625, False), (0, True), (0.0625, True)])
def test_real_rows_observe_causal_point_boundary_is_inclusive(tolerance, offset, diagnostic):
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5 - tolerance + offset] * 3},
        {"f": [0.25] * 3, "c": [0.5] * 3},
        mode="observe",
        margin=0.25,
        tolerance=tolerance,
    )
    assert payload["would_pass_point"] is diagnostic
    assert (payload["decision"], payload["reason"]) == ("finalist", "observe_finalist")


@pytest.mark.parametrize("margin,diagnostic", [(-0.25, True), (0, False), (0.25, False)])
def test_observe_empty_causal_scope_needs_no_tolerance(margin, diagnostic):
    payload = _look(
        {"f": [0.5] * 3}, {"f": [0.5] * 3}, mode="observe", margin=margin, causal=(), tolerance=None
    )
    assert payload["causal"] is None
    assert payload["would_pass_point"] is diagnostic
    assert (payload["decision"], payload["reason"]) == ("finalist", "observe_finalist")


@pytest.mark.parametrize("mode", ["statistical", "observe"])
@pytest.mark.parametrize(
    "degenerate,allowance,want_tolerance,noise_caveats",
    [
        (True, 0.125, 0, ["noise_not_observed"]),
        (False, 0, 0, []),
        (False, 0.125, 0.125, ["noise_allowance_empirical"]),
    ],
)
def test_family14_producer_selects_calibration_tolerance_and_caveats(
    mode, degenerate, allowance, want_tolerance, noise_caveats
):
    payload = _look(
        {"f": [0.75] * 3, "c": [0.5] * 3},
        {"f": [0.25] * 3, "c": [0.5] * 3},
        mode=mode,
        tolerance=None,
        calibration={"allowance": allowance, "degenerate": degenerate, "unavailable": None},
    )
    assert payload["tolerance"] == {"source": "measured_noise", "value": want_tolerance}
    assert payload["caveats"] == sorted(
        (
            ["exact_contrast", "intervals_empirical"]
            if mode == "statistical"
            else ["observe_gate_not_enforced", "uncalibrated_point_estimates"]
        )
        + noise_caveats
    )
    assert payload["decision"] == "finalist"
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


@pytest.mark.parametrize(
    "focus_exact,causal_exact",
    [
        ("absent", "absent"),
        (None, None),
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_exact_caveat_requires_a_true_statistical_focus_or_causal_flag(
    focus_exact, causal_exact, mode
):
    from evolution.tree.campaign_schema import look_caveats

    comparison = {"mode": mode, "parent_source": "fresh_parent", "causal": [1]}
    focus = {"point": 0.25, "lower": 0.25, "upper": 0.25}
    if focus_exact != "absent":
        focus["exact"] = focus_exact
    causal = {} if causal_exact == "absent" else {"exact": causal_exact}
    caveats = look_caveats(
        comparison,
        {"source": "numeric", "value": 0},
        None,
        family_version=14,
        focus=focus,
        causal=causal,
    )
    assert caveats == (
        sorted(
            ["intervals_empirical"]
            + (["exact_contrast"] if focus_exact is True or causal_exact is True else [])
        )
        if mode == "statistical"
        else ["observe_gate_not_enforced", "uncalibrated_point_estimates"]
    )


@pytest.mark.parametrize("tolerance,causal", [(0, (1,)), (None, ())])
def test_family14_numeric_zero_and_empty_causal_scope_have_no_noise_caveat(tolerance, causal):
    payload = _look(
        {"f": [0.5, 0.75, 1], "c": [0.5] * 3},
        {"f": [0, 0.25, 0.5], "c": [0.5] * 3},
        causal=causal,
        tolerance=tolerance,
    )
    assert payload["caveats"] == (
        ["exact_contrast", "intervals_empirical"] if causal else ["intervals_empirical"]
    )


@pytest.mark.parametrize(
    "candidate_causal,decision,reason",
    [
        ([0.25, 0.5, 0.75], "rejected", "causal_below_tolerance"),
        ([0.75, 0.875, 1], "finalist", "gate_passed"),
    ],
)
def test_degenerate_parent_noise_does_not_make_varying_candidate_contrasts_exact(
    candidate_causal, decision, reason
):
    payload = _look(
        {"f": [0.75, 0.875, 1], "c": candidate_causal},
        {"f": [0.25] * 3, "c": [0.5] * 3},
        tolerance=None,
        calibration={"allowance": 0, "degenerate": True, "unavailable": None},
    )
    assert payload["focus"]["exact"] is payload["causal"]["exact"] is False
    assert payload["tolerance"]["value"] == 0
    assert payload["caveats"] == ["intervals_empirical", "noise_not_observed"]
    assert (payload["decision"], payload["reason"]) == (decision, reason)
