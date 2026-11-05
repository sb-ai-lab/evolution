"""Family-14 bound Candidate-gate settings and their family boundary."""

from __future__ import annotations

import copy
import math

import pytest

from evolution.tree.campaign_schema import AttemptState, SchemaError, campaign_family_for
from evolution.tree.confidence_gate import comparison_record
from tests.campaign_drivers import sealed_prefix  # noqa: F401
from tests.campaign_fakes import Family14Life, v11_policy


def _record(policy, **context):
    return comparison_record(
        policy,
        attempt=AttemptState(
            attempt_id="r01-a01",
            round=1,
            index=1,
            parent_version="seed",
            parent_digest="a" * 64,
            measurement_id="m-r01-a01",
            parent_measurement_id="m-r01-a01-parent" if policy.fresh_parent else None,
            selection={"mode": "path", "cases": [0, 1], "unknown_cases": []},
        ),
        baseline_measurement_id="m-baseline",
        corpus_cases=[{"case_id": "c1"}, {"case_id": "c2"}],
        exam=["c1"],
        epoch=0,
        baseline_schedule=[(case, repeat) for case in ("c1", "c2") for repeat in (1, 2, 3)],
        excluded=frozenset(),
        exclusions=None,
        evidence=[
            {
                "version": 1,
                "kind": "journal_record",
                "run_id": "test-run",
                "locator": "campaign.jsonl#bytes=0:1",
                "content_digest": "b" * 64,
            }
        ],
        **context,
    )


@pytest.mark.parametrize("mode", ["statistical", "observe"])
@pytest.mark.parametrize("margin", [-0.2, -0.0, 0.2])
def test_family14_producer_binds_normalized_margin_in_both_modes(mode, margin):
    payload = _record(v11_policy(mode=mode, margin=margin), family_version=14)
    assert payload["margin"] == margin
    if margin == 0:
        assert math.copysign(1, payload["margin"]) == 1
    campaign_family_for(14).validate_payload("comparison_bound", payload)


@pytest.mark.parametrize("mode", ["statistical", "observe"])
@pytest.mark.parametrize(
    "margin", [None, True, False, "0.2", [], {}, float("nan"), float("inf"), -float("inf")]
)
def test_family14_refuses_nonfinite_or_nonnumber_bound_margin(mode, margin):
    payload = _record(v11_policy(mode=mode))
    payload["margin"] = margin
    with pytest.raises(SchemaError, match="margin is not a finite number"):
        campaign_family_for(14).validate_payload("comparison_bound", payload)


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_requires_bound_margin(mode):
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_bound", _record(v11_policy(mode=mode)))


def _bound_life(prefix, *, mode="statistical", margin=0.2, **policy):
    life = Family14Life(prefix, transcode=True, mode=mode, margin=margin, **policy)
    attempt = life.sealed
    life.select(attempt, [0, 1, 2])
    life.bind(attempt)
    return life, attempt


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_initial_family14_binding_matches_declared_margin(sealed_prefix, mode):  # noqa: F811
    life, attempt = _bound_life(sealed_prefix, mode=mode)
    payload = life.comparison_payload(attempt, margin=0.1)
    with pytest.raises(SchemaError, match="margin is not the policy's"):
        life.accept("comparison_bound", **payload)
    life.accept("comparison_bound", **life.comparison_payload(attempt, margin=0.2))
    assert life.attempt(attempt).comparison["margin"] == 0.2


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_superseding_binding_freezes_margin_and_other_settings(sealed_prefix, mode):  # noqa: F811
    life, attempt = _bound_life(sealed_prefix, mode=mode)
    life.accept("comparison_bound", **life.comparison_payload(attempt, margin=0.2))
    original = copy.deepcopy(life.envelopes[-1].payload)
    governing = life.attempt(attempt).comparison
    state = life.attempt(attempt)
    sources = [side for side in (state.measurement_id, state.parent_measurement_id) if side]
    rebound = life.continue_block(sources)
    payload = {
        **original,
        "supersedes": governing["seq"],
        "epoch": 1,
        "candidate_measurement_id": rebound[0][1],
        "parent_measurement_id": rebound[1][1] if len(rebound) > 1 else None,
    }
    with pytest.raises(
        SchemaError, match="a superseding comparison changes only its measurements and epoch"
    ):
        life.accept("comparison_bound", **{**payload, "margin": 0.1})
    life.accept("comparison_bound", **payload)
    assert life.attempt(attempt).comparison["margin"] == 0.2
    changed = {"supersedes", "epoch", "candidate_measurement_id", "parent_measurement_id"}
    assert {key: value for key, value in payload.items() if key not in changed} == {
        key: value for key, value in original.items() if key not in changed
    }


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["statistical", "observe"])
async def test_real_family14_executor_journals_margin_before_gate_looks(
    tmp_path, monkeypatch, mode
):
    from tests.test_tree_campaign_family14_run import OBSERVE, STATISTICAL, family14_run, payloads

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, OBSERVE if mode == "observe" else STATISTICAL
    )
    comparisons = payloads(run, "comparison_bound")
    assert comparisons
    assert all(comparison["margin"] == 0.0 for comparison in comparisons)
    assert snapshot.identity["policy"]["gate"]["focus"]["margin"] == 0.0


@pytest.mark.parametrize("scope", ["focus", "causal", "overall"])
def test_family14_lifecycle_refuses_a_two_sided_final_bound(sealed_prefix, scope):  # noqa: F811
    life, attempt = _bound_life(sealed_prefix, margin=0)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.stages(attempt, 1, 3)
    life.accept("noise_calibration_completed", **life.calibration_completed_payload(attempt))
    payload = life.look_payload(attempt, 3)
    invalid = copy.deepcopy(payload)
    invalid[scope]["upper"] = invalid[scope]["point"]
    with pytest.raises(SchemaError, match="a final statistical bound is one-sided"):
        life.accept("comparison_look_evaluated", **invalid)
    life.accept("comparison_look_evaluated", **payload)
    assert life.attempt(attempt).looks[3]["decision"] == "finalist"


def test_family14_lifecycle_refuses_null_final_points(sealed_prefix):  # noqa: F811
    life, attempt = _bound_life(sealed_prefix, margin=0)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.stages(attempt, 1, 3)
    life.accept("noise_calibration_completed", **life.calibration_completed_payload(attempt))
    payload = life.look_payload(attempt, 3)
    payload["focus"].update(point=None, lower=None, unavailable="insufficient_repeats")
    with pytest.raises(SchemaError, match="a null point belongs to an interim statistical focus"):
        life.accept("comparison_look_evaluated", **payload)


def _numeric_look_life(prefix, *, margin=0, interim=False, mode="statistical", tolerance=0.25):
    """Real lifecycle, synthetic published evidence; row proof belongs to audit tests."""
    life, attempt = _bound_life(
        prefix, margin=margin, mode=mode, tolerance=tolerance, repeats=4 if interim else 3
    )
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.stages(attempt, 1, 3)
    return life, attempt


def _boundary_estimate(boundary, *, exact, final):
    # Binary rational fixtures make equality exact in the published JSON numbers.
    return {
        "point": boundary if exact else boundary + (0.125 if final else -0.125),
        "lower": boundary if final or exact else boundary - 0.25,
        "upper": None if final else boundary,
        "level": 0.9,
        "unavailable": None,
        "exact": exact,
    }


def _accept_and_close(life, attempt, payload, *, layer=None):
    life.accept("comparison_look_evaluated", **payload)
    close = {
        "attempt_id": attempt,
        "outcome": payload["decision"],
        "reason": payload["reason"],
        "look": payload["look"],
        "code": None,
    }
    with pytest.raises(SchemaError, match="a look-decided close names an evaluated look"):
        life.accept("attempt_closed", **{**close, "look": None})
    with pytest.raises(SchemaError):
        life.accept(
            "attempt_closed",
            **{
                **close,
                "reason": "causal_below_tolerance" if layer != "causal" else "focus_below_margin",
                "outcome": "rejected",
            },
        )
    life.accept("attempt_closed", **close)
    state = life.attempt(attempt)
    assert (state.outcome, state.reason, state.look) == (
        payload["decision"],
        payload["reason"],
        payload["look"],
    )
    assert state.looks[payload["look"]].get("rejection_layer") == layer


@pytest.mark.parametrize("margin", [-0.25, 0, 0.25])
@pytest.mark.parametrize("interim", [False, True], ids=["final", "interim"])
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
def test_lifecycle_margin_boundary_and_look_decided_close(
    sealed_prefix,  # noqa: F811
    margin,
    interim,
    exact,
    offset,
    final_decision,
    interim_decision,
    reason,
):
    life, attempt = _numeric_look_life(sealed_prefix, margin=margin, interim=interim)
    decision = interim_decision if interim else final_decision
    expected_reason = "gate_passed" if decision == "finalist" else reason
    payload = life.look_payload(
        attempt,
        3,
        focus=_boundary_estimate(margin + offset, exact=exact, final=not interim),
        decision=decision,
        reason=expected_reason,
    )
    # A numerically wrong, otherwise admitted decision must not settle this look.
    wrong = (
        {"decision": "continue", "reason": None}
        if decision == "rejected" and interim
        else {"decision": "finalist", "reason": "gate_passed"}
        if decision == "rejected"
        else {"decision": "rejected", "reason": "focus_below_margin"}
    )
    with pytest.raises(SchemaError, match="the decision does not follow the published values"):
        life.accept("comparison_look_evaluated", **{**payload, **wrong})
    if decision != "continue":
        _accept_and_close(life, attempt, payload, layer="focus" if decision == "rejected" else None)
        return
    life.accept("comparison_look_evaluated", **payload)
    with pytest.raises(SchemaError, match="the close does not follow its look's decision"):
        life.accept(
            "attempt_closed",
            attempt_id=attempt,
            outcome="finalist",
            reason="gate_passed",
            look=3,
            code=None,
        )
    # Passing an interim permits the later final stage; it never accepts early.
    life.stages(attempt, 2, 4)
    final_payload = life.look_payload(
        attempt,
        4,
        focus=_boundary_estimate(margin + 0.0625, exact=exact, final=True),
        decision="finalist",
        reason="gate_passed",
    )
    _accept_and_close(life, attempt, final_payload)


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
def test_lifecycle_causal_boundary_and_close(
    sealed_prefix,  # noqa: F811
    exact,
    tolerance,
    offset,
    decision,
    reason,
):
    life, attempt = _numeric_look_life(sealed_prefix, tolerance=tolerance)
    payload = life.look_payload(
        attempt,
        3,
        causal=_boundary_estimate(-tolerance + offset, exact=exact, final=True),
        decision=decision,
        reason=reason,
    )
    wrong = (
        {"decision": "finalist", "reason": "gate_passed"}
        if decision == "rejected"
        else {"decision": "rejected", "reason": "causal_below_tolerance"}
    )
    with pytest.raises(SchemaError, match="the decision does not follow the published values"):
        life.accept("comparison_look_evaluated", **{**payload, **wrong})
    _accept_and_close(life, attempt, payload, layer="causal" if decision == "rejected" else None)


@pytest.mark.parametrize(
    "insufficient,focus_boundary,causal_boundary,decision,reason",
    [
        ("focus", 0, -0.5, "insufficient_evidence", "insufficient_repeats"),
        ("causal", 0, -0.5, "insufficient_evidence", "insufficient_repeats"),
        (None, 0, -0.5, "rejected", "focus_below_margin"),
        (None, 0.125, -0.5, "rejected", "causal_below_tolerance"),
    ],
)
@pytest.mark.parametrize("critical", [False, True])
def test_lifecycle_priority_and_legal_critical_override(
    sealed_prefix,  # noqa: F811
    insufficient,
    focus_boundary,
    causal_boundary,
    decision,
    reason,
    critical,
):
    life, attempt = _numeric_look_life(sealed_prefix)
    focus = _boundary_estimate(focus_boundary, exact=False, final=True)
    causal = _boundary_estimate(causal_boundary, exact=False, final=True)
    if insufficient:
        estimate = focus if insufficient == "focus" else causal
        estimate.update(lower=None, unavailable="insufficient_repeats")
    if critical:
        # Lifecycle permits the override; only the row-backed audit can prove it.
        decision, reason = "rejected", "critical_failure"
    payload = life.look_payload(
        attempt, 3, focus=focus, causal=causal, decision=decision, reason=reason
    )
    _accept_and_close(
        life,
        attempt,
        payload,
        layer="focus"
        if reason == "focus_below_margin"
        else "causal"
        if reason == "causal_below_tolerance"
        else None,
    )


@pytest.mark.parametrize("margin", [-0.25, 0, 0.25])
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("diagnostic", [False, True, None])
def test_lifecycle_observe_diagnostic_is_not_an_outcome(
    sealed_prefix,  # noqa: F811
    margin,
    critical,
    diagnostic,
):
    if diagnostic is None:
        life, attempt = _bound_life(sealed_prefix, margin=margin, mode="observe")
        life.accept("comparison_bound", **life.comparison_payload(attempt))
        life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
        life.stages(attempt, 1, 3)
        life.accept(
            "noise_calibration_completed",
            **life.calibration_completed_payload(
                attempt,
                allowance=None,
                unavailable="insufficient_calibration_repeats",
                degenerate=False,
                result_digest=None,
            ),
        )
    else:
        life, attempt = _numeric_look_life(sealed_prefix, margin=margin, mode="observe")
    payload = life.look_payload(attempt, 3)
    payload["focus"]["point"] = margin + (0.125 if diagnostic is True else 0)
    payload.update(
        would_pass_point=diagnostic,
        decision="rejected" if critical else "finalist",
        reason="critical_failure" if critical else "observe_finalist",
    )
    with pytest.raises(SchemaError, match="would_pass_point does not follow the point values"):
        life.accept("comparison_look_evaluated", **{**payload, "would_pass_point": not diagnostic})
    _accept_and_close(life, attempt, payload)


@pytest.mark.parametrize(
    "family,reason",
    [
        (14, "focus_upper_nonpositive"),
        (14, "focus_lower_not_positive"),
        (14, "degenerate_interval"),
    ],
)
def test_gate_reason_vocabulary_is_closed_for_looks_and_attempts(sealed_prefix, family, reason):  # noqa: F811
    from tests.campaign_fakes import LifecycleFixture

    life, attempt = _numeric_look_life(sealed_prefix)
    payload = life.look_payload(attempt, 3, decision="rejected", reason=reason)
    if family == 13:
        old = LifecycleFixture(sealed_prefix, tolerance=0.25)
        attempt = old.sealed
        old.select(attempt, [0, 1, 2])
        old.bind(attempt)
        old.accept("comparison_bound", **old.comparison_payload(attempt))
        old.stages(attempt, 1, 3)
        payload = old.look_payload(attempt, 3, decision="rejected", reason=reason)
    with pytest.raises(SchemaError, match="decision and reason are not in the registry"):
        campaign_family_for(family).validate_payload("comparison_look_evaluated", payload)
    with pytest.raises(SchemaError, match="reason does not match the outcome"):
        campaign_family_for(family).validate_payload(
            "attempt_closed",
            {
                "attempt_id": attempt,
                "outcome": "rejected",
                "reason": reason,
                "look": 3,
                "code": None,
            },
        )


@pytest.mark.parametrize("tolerance", [0, 0.25])
@pytest.mark.parametrize("offset,diagnostic", [(-0.0625, False), (0, True), (0.0625, True)])
def test_lifecycle_observe_causal_point_boundary(sealed_prefix, tolerance, offset, diagnostic):  # noqa: F811
    life, attempt = _numeric_look_life(sealed_prefix, mode="observe", tolerance=tolerance)
    payload = life.look_payload(attempt, 3)
    payload["causal"]["point"] = -tolerance + offset
    payload["would_pass_point"] = diagnostic
    with pytest.raises(SchemaError, match="would_pass_point does not follow the point values"):
        life.accept("comparison_look_evaluated", **{**payload, "would_pass_point": not diagnostic})
    _accept_and_close(life, attempt, payload)


@pytest.mark.parametrize(
    "margin,boundary,family14_decision,family14_reason,legacy_result",
    [
        (-0.25, -0.125, "finalist", "gate_passed", ("rejected", "focus_lower_not_positive")),
        (0.25, 0.125, "rejected", "focus_below_margin", ("accepted", "gate_passed")),
    ],
)
def test_published_reader_uses_only_bound_margin_with_explicit_family(
    margin, boundary, family14_decision, family14_reason, legacy_result
):
    from evolution.tree.campaign_schema import look_decision

    # No current policy is an input: an authorized future policy cannot reinterpret it.
    comparison = {"mode": "statistical", "margin": margin}
    focus = _boundary_estimate(boundary, exact=True, final=True)
    decision = look_decision(
        comparison, final=True, focus=focus, causal=None, tolerance=None, family_version=14
    )
    assert (decision.decision, decision.reason) == (family14_decision, family14_reason)
    # Merely seeing `margin` or `exact` must not switch the compatibility default.
    legacy = look_decision(comparison, final=True, focus=focus, causal=None, tolerance=None)
    assert (legacy.decision, legacy.reason) == legacy_result


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_degenerate_calibration_permits_a_look_and_planner_evaluation(sealed_prefix, mode):  # noqa: F811
    from evolution.tree.campaign_schema import owed_close
    from evolution.tree.campaign_view import CampaignView, EvaluateLook, _confidence_action

    life, attempt = _bound_life(sealed_prefix, margin=0, mode=mode)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.stages(attempt, 1, 3)
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(attempt, degenerate=True, allowance=0),
    )
    assert _confidence_action(
        CampaignView(life.snapshot, {}, None, {}), life.state.policy, life.attempt(attempt)
    ) == EvaluateLook(attempt, 3)
    assert owed_close(life.attempt(attempt), family_version=14) is None
    payload = life.look_payload(attempt, 3)
    assert payload["tolerance"] == {"source": "measured_noise", "value": 0}
    assert "noise_not_observed" in payload["caveats"]
    assert "noise_allowance_empirical" not in payload["caveats"]
    assert "observe_calibration_degenerate" not in payload["caveats"]
    _accept_and_close(life, attempt, payload)


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_baseline_degenerate_calibration_permits_the_first_stage(sealed_prefix, mode):  # noqa: F811
    from evolution.tree.campaign_view import CampaignView, RunAttemptStage, _confidence_action

    life, attempt = _bound_life(sealed_prefix, margin=0, mode=mode, fresh_parent=False)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(attempt, degenerate=True, allowance=0),
    )
    assert _confidence_action(
        CampaignView(life.snapshot, {}, None, {}), life.state.policy, life.attempt(attempt)
    ) == RunAttemptStage(attempt, 1)
    with pytest.raises(SchemaError, match="insufficient_calibration needs a closing calibration"):
        life.accept(
            "attempt_closed",
            attempt_id=attempt,
            outcome="insufficient_evidence",
            reason="insufficient_calibration",
            look=None,
            code=None,
        )
    life.stages(attempt, 1, 3)


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_unavailable_baseline_calibration_closes_only_statistical(sealed_prefix, mode):  # noqa: F811
    from evolution.tree.campaign_view import (
        CampaignView,
        CloseAttempt,
        RunAttemptStage,
        _confidence_action,
    )

    life, attempt = _bound_life(sealed_prefix, margin=0, mode=mode, fresh_parent=False)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(
            attempt,
            allowance=None,
            unavailable="insufficient_calibration_repeats",
            result_digest=None,
        ),
    )
    action = _confidence_action(
        CampaignView(life.snapshot, {}, None, {}), life.state.policy, life.attempt(attempt)
    )
    if mode == "statistical":
        assert action == CloseAttempt(
            attempt, "insufficient_evidence", "insufficient_calibration", None
        )
        with pytest.raises(SchemaError, match="a closing calibration opens no stage"):
            life.stages(attempt, 1, 3)
        life.accept(
            "attempt_closed",
            attempt_id=attempt,
            outcome="insufficient_evidence",
            reason="insufficient_calibration",
            look=None,
            code=None,
        )
    else:
        assert action == RunAttemptStage(attempt, 1)
        life.stages(attempt, 1, 3)
        payload = life.look_payload(attempt, 3)
        assert payload["would_pass_point"] is None
        assert payload["caveats"] == [
            "causal_parent_not_contemporaneous",
            "noise_allowance_unavailable",
            "observe_gate_not_enforced",
            "uncalibrated_point_estimates",
        ]
        _accept_and_close(life, attempt, payload)


@pytest.mark.parametrize("unavailable", [False, True])
def test_family14_rebound_after_calibration_respects_nested_close_consequence(
    sealed_prefix,  # noqa: F811
    unavailable,
):
    from evolution.tree.campaign_schema import owed_close, superseding_comparison_owed
    from evolution.tree.campaign_view import (
        BindComparison,
        CampaignView,
        CloseAttempt,
        _confidence_action,
    )

    life, attempt = _bound_life(sealed_prefix, margin=0, fresh_parent=False)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    original = copy.deepcopy(life.envelopes[-1].payload)
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(
            attempt,
            allowance=None if unavailable else 0,
            degenerate=not unavailable,
            unavailable="insufficient_calibration_repeats" if unavailable else None,
            result_digest=None if unavailable else "d" * 64,
        ),
    )
    comparison_seq = life.attempt(attempt).comparison["seq"]
    life.continue_block([life.attempt(attempt).measurement_id])
    state = life.attempt(attempt)
    view = CampaignView(life.snapshot, {}, None, {})
    payload = {
        **original,
        "supersedes": comparison_seq,
        "epoch": 1,
        "candidate_measurement_id": state.measurement_id,
    }
    if unavailable:
        close = owed_close(state, family_version=14)
        assert (close[0].decision, close[0].reason, close[1]) == (
            "insufficient_evidence",
            "insufficient_calibration",
            None,
        )
        assert superseding_comparison_owed(state, family_version=14) is False
        assert _confidence_action(view, life.state.policy, state) == CloseAttempt(
            attempt, "insufficient_evidence", "insufficient_calibration", None
        )
        with pytest.raises(SchemaError, match="an undecided comparison is re-bound"):
            life.accept("comparison_bound", **payload)
    else:
        assert owed_close(state, family_version=14) is None
        assert superseding_comparison_owed(state, family_version=14) is True
        assert _confidence_action(view, life.state.policy, state) == BindComparison(attempt)
        with pytest.raises(SchemaError, match="a superseding comparison precedes further work"):
            life.stages(attempt, 1, 3)
        life.accept("comparison_bound", **payload)
        life.stages(attempt, 1, 3)


def test_family14_overall_only_point_bound_does_not_add_exact_caveat(sealed_prefix):  # noqa: F811
    life, attempt = _numeric_look_life(sealed_prefix)
    payload = life.look_payload(attempt, 3)
    payload["overall"]["lower"] = payload["overall"]["point"]
    assert payload["focus"]["exact"] is payload["causal"]["exact"] is False
    assert payload["caveats"] == ["intervals_empirical"]
    life.accept("comparison_look_evaluated", **payload)


def test_family14_lifecycle_refuses_wrong_explicit_exact_caveats(sealed_prefix):  # noqa: F811
    life, attempt = _numeric_look_life(sealed_prefix, tolerance=0)
    payload = life.look_payload(
        attempt,
        3,
        focus=_boundary_estimate(0.25, exact=True, final=True),
        caveats=["intervals_empirical"],
    )
    assert payload["caveats"] == ["intervals_empirical"]
    with pytest.raises(SchemaError, match="the caveats are not the ones in force"):
        life.accept("comparison_look_evaluated", **payload)
    for caveats in (
        ["observe_calibration_degenerate"],
        ["noise_not_observed", "intervals_empirical"],
        ["exact_contrast", "exact_contrast", "intervals_empirical"],
    ):
        with pytest.raises(SchemaError):
            life.accept("comparison_look_evaluated", **{**payload, "caveats": caveats})


@pytest.mark.parametrize("mode", ["statistical", "observe"])
def test_family14_unavailable_fresh_calibration_forbids_only_statistical_looks(sealed_prefix, mode):  # noqa: F811
    life, attempt = _bound_life(sealed_prefix, margin=0, mode=mode)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.stages(attempt, 1, 3)
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(
            attempt,
            allowance=None,
            unavailable="insufficient_calibration_repeats",
            result_digest=None,
        ),
    )
    if mode == "statistical":
        from tests.campaign_fakes import LifecycleFixture

        # No numerical decision can be produced without the owed tolerance.
        # Supply a synthetic look to prove the lifecycle refuses it first.
        payload = LifecycleFixture.look_payload(life, attempt, 3)
        for scope in ("focus", "causal"):
            payload[scope]["exact"] = False
        payload["decision"] = "finalist"
        with pytest.raises(SchemaError, match="a closing calibration evaluates no look"):
            life.accept("comparison_look_evaluated", **payload)
        life.accept(
            "attempt_closed",
            attempt_id=attempt,
            outcome="insufficient_evidence",
            reason="insufficient_calibration",
            look=None,
            code=None,
        )
    else:
        payload = life.look_payload(attempt, 3)
        assert payload["would_pass_point"] is None
        assert payload["caveats"] == [
            "noise_allowance_unavailable",
            "observe_gate_not_enforced",
            "uncalibrated_point_estimates",
        ]
        _accept_and_close(life, attempt, payload)


def test_family14_degenerate_baseline_calibration_allows_next_stage_after_interim(sealed_prefix):  # noqa: F811
    from evolution.tree.campaign_view import CampaignView, RunAttemptStage, _confidence_action

    life, attempt = _bound_life(sealed_prefix, margin=0, fresh_parent=False, repeats=4)
    life.accept("comparison_bound", **life.comparison_payload(attempt))
    life.accept("noise_calibration_bound", **life.calibration_bound_payload(attempt))
    life.accept(
        "noise_calibration_completed",
        **life.calibration_completed_payload(attempt, degenerate=True, allowance=0),
    )
    life.stages(attempt, 1, 3)
    payload = life.look_payload(attempt, 3)
    assert (payload["decision"], payload["reason"]) == ("continue", None)
    assert payload["caveats"] == [
        "causal_parent_not_contemporaneous",
        "intervals_empirical",
        "noise_not_observed",
    ]
    life.accept("comparison_look_evaluated", **payload)
    assert _confidence_action(
        CampaignView(life.snapshot, {}, None, {}), life.state.policy, life.attempt(attempt)
    ) == RunAttemptStage(attempt, 2)
    life.stages(attempt, 2, 4)
    _accept_and_close(life, attempt, life.look_payload(attempt, 4))
