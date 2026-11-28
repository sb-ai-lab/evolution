"""Family-15 ``independent`` block of ``lab-campaign-view/9`` (spec 2026-10-19 D4, Q1, Q2 as corrected, Q3, Q5).

Literal, agent-neutral Core payloads in the shape of Core's public projection (family15_report.py at the dev Core
v2.0.0b9 (3882340bd7f7)); rationals come from Core's public ``encode_rational``. Lab copies, labels and wraps; it never decides.
"""
import copy
from fractions import Fraction

import pytest
from evolution.tree.family15_schema import encode_rational

from evolution_lab.owner.campaign_views import GROUPS, lab_notices
from evolution_lab.owner.independent_view import (DISPLAY, attempt15_group, comparison_layer, comparison_reference,
                                                  independent_view, scope_display, scope_row)


def R(numerator, denominator=1):
    return encode_rational(Fraction(numerator, denominator))


def _upper(numerator, denominator):
    """Core's enclosure upper: ``min(1, L + 2^-63)`` (case_comparison.py:324-327), never a confidence bound."""
    return encode_rational(min(Fraction(1), Fraction(numerator, denominator) + Fraction(1, 2 ** 63)))


SETTINGS = {"mode": "confidence", "verification": "reuse", "stability": "stable", "fresh_parent": False,
            "global_rule": "point", "train_cases": 8, "repeats": 1, "train_slots": 8, "execution_noise": "negligible",
            "allowance": R(0), "allowance_source": None, "allowance_source_kind": "assumed_negligible",
            "method": "bounded-weighted-mean/1", "unit": "case"}
PLANNED = {"basis": "full_initial_baseline_and_conditional_future_stage_scenarios", "baseline_slots": 8,
           "admitted_baseline_imports": 0, "affected_cases": None, "candidate_final_slots_if_gate_passes": None,
           "parent_gate_slots": 0, "champion_slots_per_scope": 0, "champion_scope": "reached_finalist",
           "slots_per_refresh": 8, "connection_preflight": "separate_no_measurement_credit",
           "probes_and_retries": "separate", "noise_diagnostic_slots": 0}
GUIDANCE = {"status": "conditional_guidance", "minimum_units": 4,
            "assumptions": ["equal_independent_unit_weights", "zero_threshold", "all_ones_contrast_envelope"],
            "interpretation": "not_an_admission_or_power_claim"}
COMMITTED = {"measurement": {"scenario_slots": 14, "imported_slots": 0, "target_turns": 14, "transport_retries": 0,
                             "scorer_executions": 14},
             "connection_preflight": None, "role_requests": 3, "reflector_probe_requests": 0,
             "interpretation": "journaled_work_not_tokens_currency_or_provider_call_estimates"}
REMAINING = {"basis": "conditional_same_bound_conditions_complete_scoring_without_retries",
             "scope": "current_baseline_and_opened_attempts_only", "baseline_slots": 0, "candidate_gate_slots": 0,
             "parent_gate_slots": 0, "candidate_final_slots_if_gates_pass": 2,
             "champion_slots_for_reached_finalists": 0, "remaining_refreshes": None, "refresh_scenario_slots": None,
             "probes_and_retries": "separate", "noise_diagnostic_slots": 0}
COST = {"basis": "full_scenario_complete_scoring_without_retries", "baseline_slots": 0, "candidate_gate_slots": 6,
        "candidate_final_slots": 2, "candidate_total_slots": 8, "parent_slots": 0, "champion_slots": 0,
        "champion_basis": "per_reached_finalist", "slots_per_refresh": 8, "connection_preflight_slots": 0,
        "probes_and_retries": "separate", "noise_diagnostic_slots": 0, "simplest_passing_path_slots": 8}
#: Focus rule ``bound`` in a ``confidence`` campaign (point .12, confidence .9, L .03, threshold 0, not strict).
FOCUS = {"name": "focus", "rule": "bound", "status": "available", "declared_count": 6, "unit_count": 6,
         "mass": R(1), "excluded_mass": R(0), "unavailable": None, "point": R(12, 100), "threshold": R(0),
         "strict": False, "confidence": R(9, 10),
         "enclosure": {"lower": R(3, 100), "upper": _upper(3, 100), "steps": 64}}
#: Global rule ``point`` of the same campaign: confidence and enclosure are null keys (family15_report.py:224-225;
#: Core test_confidence_campaign_global_point_or_off_has_no_bound).
GLOBAL = {"name": "global", "rule": "point", "status": "available", "declared_count": 8, "unit_count": 8,
          "mass": R(1), "excluded_mass": R(0), "unavailable": None, "point": R(5, 100), "threshold": R(0),
          "strict": False, "confidence": None, "enclosure": None}
NOISE = {"kind": "negligible", "allowance": R(0), "source": None, "source_kind": "assumed_negligible",
         "variation": "not_requested", "diagnostic": "candidate_primary_score_repeat_variance",
         "units": "score_squared", "observed_cases": 0, "extra_measurements": 0}
COMPARISON = {"outcome": "passed", "reason": "passed", "deciding_layer": "focus", "eligible": True,
              "mode": "confidence", "scopes": [FOCUS, GLOBAL],
              "coverage": {"candidate": {"required": 6, "settled": 6, "admissible": 6},
                           "reference": {"required": 6, "settled": 6, "admissible": 6}},
              "excluded_reference_slots": 0, "method": "bounded-weighted-mean/1", "unit": "case",
              "confidence_scope": "conditional_fixed_scope_independent_bounded_units",
              "reference": "stored_non_contemporaneous", "reference_measurement_id": "m-reference", "verification": None,
              "selection_bias_possible": ["focus_selection", "stored_reference"], "noise": NOISE}
ATTEMPT = {"round": 1, "attempt_id": "r01-a01", "outcome": "finalist", "reason": "passed",
           "comparison": COMPARISON, "gate_comparison": COMPARISON, "global_comparison": None, "preflight": None, "conditional_cost": COST, "eligible_final_score": None}
BLOCK = {"next_action": "continue_evolve", "settings": SETTINGS, "planned_work": PLANNED,
         "local_guidance": GUIDANCE, "preflight": [], "attempts": [ATTEMPT], "diagnostics": [],
         "committed_work": COMMITTED, "remaining_work": REMAINING, "evidence": "authenticated_publications"}


@pytest.mark.parametrize("extra", [
    {"check_modes": {"seal_oracle_leak": "off"}},
    {"shadow_hits": {"connection_preflight": 2}},
    {"check_modes": {"connection_preflight": "shadow"}, "shadow_hits": {"connection_preflight": 3}},
])
def test_check_projection_keys_do_not_enter_independent_block(extra):
    assert independent_view({**BLOCK, **extra}) == independent_view(BLOCK)


def test_probe_availability_does_not_hide_independent_comparisons():
    block = {**BLOCK, 'probe_availability': [{'round': 1, 'health': 'unavailable',
        'mediator': 'failed', 'unreachable': 2, 'transport_failures': 0}]}
    assert independent_view(block) == independent_view(BLOCK)


EXPLANATION = {"status": "impossible", "basis": "stored_reference", "profile_digest": None,
               "interpretation": "score_range_ceiling_not_a_success_or_power_prediction",
               "scopes": [{"name": "focus", "status": "calculated", "rule": "bound", "threshold": R(0),
                           "strict": False, "unit_count": 1, "passes": False, "confidence": R(9, 10),
                           "delta_max": R(1), "c_max": R(1, 2), "e_max": {"kind": "finite", "value": R(1, 2)}},
                          {"name": "causal", "status": "skipped"},
                          {"name": "global", "status": "calculated", "rule": "point", "threshold": R(0),
                           "strict": True, "unit_count": 8, "passes": True, "delta_max": R(1),
                           "e_max": {"kind": "positive_infinity"}}]}
REFRESH = {"scenario": "retain_champion_through_remaining_round_budget", "remaining_refreshes": 0,
           "slots_per_refresh": 8, "scenario_slots": 0, "retry_ceiling_separate": True,
           "connection_preflight_credit": "separate"}
#: An attempt closed in preflight (campaign.py:8593-8604; family15_lifecycle.py:541): no comparison, its ceilings
#: kept, no conditional cost (no comparison profile, family15_report.py:462, :491-500).
PREFLIGHT_ATTEMPT = {"round": 1, "attempt_id": "r01-a02", "outcome": "rejected",
                     "reason": "structurally_unattainable", "comparison": None, "gate_comparison": None,
                     "global_comparison": None, "preflight": EXPLANATION,
                     "conditional_cost": None, "eligible_final_score": None}
PREFLIGHT_BLOCK = {**BLOCK, "attempts": [PREFLIGHT_ATTEMPT],
                   "preflight": [{"round": 1, "explanation": EXPLANATION, "refresh": REFRESH}]}


def _with_scopes(*scopes, mode="confidence"):
    block = copy.deepcopy(BLOCK)
    block["attempts"][0]["comparison"].update(scopes=list(scopes), mode=mode)
    return block


@pytest.mark.parametrize("block", [BLOCK, PREFLIGHT_BLOCK, _with_scopes(FOCUS, mode="confidence")],
                         ids=["comparison", "preflight", "focus-only"])
def test_independent_fixtures_are_cores_public_grammar(block):
    """The literal payloads above are what Core's own closed validator admits (fixture shape, not Lab code)."""
    from evolution.tree.family15_report import validate_campaign15_projection
    validate_campaign15_projection(block)


def test_confidence_scope_and_point_global_get_their_own_display():
    rows = independent_view(BLOCK)["attempts"][0]["comparison"]["scopes"]
    assert [r["display"] for r in rows] == ["bound", "point"]
    assert rows[0]["point"]["value"] == 0.12 and rows[0]["enclosure"]["lower"]["value"] == 0.03
    assert rows[0]["confidence"]["value"] == 0.9 and rows[0]["enclosure"]["upper"]["exact"] == _upper(3, 100)
    assert rows[1]["confidence"] is None and rows[1]["enclosure"] is None
    assert set(rows[0]) == {"name", "rule", "status", "unavailable", "display", "point", "threshold", "strict",
                            "confidence", "enclosure", "declared_count", "unit_count", "mass", "excluded_mass",
                            "fallback", "lower", "exact", "pairing"}


def test_unfiltered_off_and_skipped_scopes():
    off = dict(FOCUS, rule="off", threshold=None, strict=None, confidence=None, enclosure=None)
    skipped = dict(GLOBAL, status="skipped", point=None, declared_count=0, unit_count=0)
    unavailable = dict(GLOBAL, name="causal", status="unavailable", unavailable="empty_support", point=None)
    rows = independent_view(_with_scopes(off, unavailable, skipped, mode="unfiltered"))[
        "attempts"][0]["comparison"]["scopes"]
    assert [r["display"] for r in rows] == ["no_threshold", "status", "status"]
    assert rows[0]["point"] == {"value": 0.12, "exact": R(12, 100)} and rows[0]["threshold"] is None
    assert rows[1]["unavailable"] == "empty_support" and rows[1]["threshold"] == {"value": 0.0, "exact": R(0)}
    assert rows[2]["declared_count"] == 0 and rows[2]["point"] is None


def test_scope_display_is_by_the_scopes_own_rule_and_status_never_the_campaign_mode():
    assert DISPLAY == ("bound", "point", "bootstrap", "no_threshold", "status")
    assert scope_display(dict(GLOBAL, threshold=None)) == "no_threshold"      # rule point without a threshold
    assert scope_display(dict(FOCUS, status="unavailable")) == "status"
    assert scope_display(dict(FOCUS, rule="bound", enclosure=None)) == "bound"
    for mode in ("point", "confidence", "unfiltered"):                       # mode never changes a scope's display
        rows = independent_view(_with_scopes(FOCUS, GLOBAL, mode=mode))["attempts"][0]["comparison"]["scopes"]
        assert [r["display"] for r in rows] == ["bound", "point"]
    assert scope_row(GLOBAL)["display"] == "point"


def test_reference_is_the_round_champion_measurement():
    assert comparison_reference({"deciding_layer": "focus", "reference": "fresh"}) == "fresh"
    assert comparison_reference({"deciding_layer": "global", "reference": "stored_non_contemporaneous"}) == "stored"
    assert comparison_reference({"deciding_layer": "global", "reference": None}) == "unknown"
    assert comparison_layer({"deciding_layer": "causal"}) == "gate"
    assert comparison_layer({"deciding_layer": "focus"}) == "gate"
    assert comparison_layer({"deciding_layer": "global"}) == "global"
    comparison = independent_view(BLOCK)["attempts"][0]["comparison"]
    # The derived words sit next to Core's recorded values; no reference digest, no "parent".
    assert (comparison["layer"], comparison["deciding_layer"]) == ("gate", "focus")
    assert (comparison["reference"], comparison["reference_kind"]) == ("stored", "stored_non_contemporaneous")
    assert "parent" not in repr(comparison).lower()


def test_reference_refuses_a_value_core_does_not_record():
    with pytest.raises(ValueError):
        comparison_reference({"deciding_layer": "focus", "reference": "parent"})
    with pytest.raises(ValueError):
        comparison_layer({"deciding_layer": "rest"})


def test_attempt_closed_in_preflight_has_no_comparison_and_keeps_its_ceilings():
    view = independent_view(PREFLIGHT_BLOCK)
    attempt = view["attempts"][0]
    assert attempt["reason"] == "structurally_unattainable" and attempt["comparison"] is None
    assert attempt["preflight"]["scopes"][0]["delta_max"]["exact"] == encode_rational(Fraction(1))
    assert attempt["group"] == "not_enough_data" and attempt["conditional_cost"] is None
    focus, causal, global_ = attempt["preflight"]["scopes"]
    assert focus["c_max"] == {"value": 0.5, "exact": R(1, 2)} and focus["e_max"] == {
        "kind": "finite", "value": {"value": 0.5, "exact": R(1, 2)}}
    # A skipped preflight scope keeps only Core's name and status; every other key is null (closed shape).
    assert causal == {"name": "causal", "status": "skipped", "rule": None, "threshold": None, "strict": None,
                      "unit_count": None, "passes": None, "confidence": None, "delta_max": None, "c_max": None,
                      "e_max": None, "fallback": None}
    assert global_["e_max"] == {"kind": "positive_infinity", "value": None} and global_["confidence"] is None
    round_preflight, = view["attainability"]["preflight"]
    assert round_preflight["round"] == 1 and round_preflight["refresh"] == REFRESH
    assert round_preflight["explanation"]["status"] == "impossible"


def test_preflight_scope_statuses_are_their_own_vocabulary():
    from evolution_lab.owner import core_raw
    vocabulary = core_raw.family15_scope_vocabulary()
    assert vocabulary["preflight_statuses"] == {"skipped", "calculated", "uncalibrated"}
    with pytest.raises(ValueError):
        independent_view({**PREFLIGHT_BLOCK, "attempts": [dict(PREFLIGHT_ATTEMPT, preflight=dict(
            EXPLANATION, scopes=[{"name": "focus", "status": "available"}]))]})


def test_unavailable_evidence_is_explicit():
    view = independent_view({**BLOCK, "evidence": "unavailable", "attempts": []})
    assert view["evidence"] == "unavailable" and view["attempts"] == []
    view = independent_view({**BLOCK, "evidence": "snapshot_only", "attempts": [], "committed_work": None,
                             "remaining_work": None})
    assert view["evidence"] == "snapshot_only" and view["work"]["committed"] is None


def test_counts_are_plain_and_quantities_wrapped():
    view = independent_view(BLOCK)
    assert view["settings"]["execution_noise"] == {"kind": "negligible", "allowance": {"value": 0.0, "exact": R(0)},
                                                   "source": None, "source_kind": "assumed_negligible"}
    assert view["settings"]["statistics"] == {"method": "bounded-weighted-mean/1", "unit": "case"}
    assert (view["settings"]["repeats"], view["settings"]["train_slots"]) == (1, 8)
    attempt = view["attempts"][0]
    assert attempt["conditional_cost"] == COST and all(
        type(v) is int for k, v in attempt["conditional_cost"].items()
        if k not in {"basis", "probes_and_retries", "champion_basis"})
    comparison = attempt["comparison"]
    assert comparison["noise"]["allowance"] == {"value": 0.0, "exact": R(0)}
    assert comparison["coverage"]["candidate"] == {"required": 6, "settled": 6, "admissible": 6}
    assert comparison["excluded_reference_slots"] == 0 and comparison["scopes"][0]["unit_count"] == 6
    assert comparison["scopes"][0]["mass"] == {"value": 1.0, "exact": R(1)}
    assert view["work"] == {"planned": PLANNED, "committed": COMMITTED, "remaining": REMAINING}
    assert view["attainability"]["local_guidance"] == GUIDANCE


def test_independent_view_key_sets_are_closed():
    view = independent_view(BLOCK)
    assert set(view) == {"evidence", "next_action", "settings", "attempts", "attainability", "work"}
    assert set(view["settings"]) == {"mode", "repeats", "verification", "stability", "fresh_parent", "global_rule",
                                     "train_cases", "train_slots", "execution_noise", "statistics", "bound_fallback"}
    assert set(view["attempts"][0]) == {"round", "attempt_id", "outcome", "reason", "group", "eligible_final_score",
                                        "comparison", "gate_comparison", "global_comparison", "preflight", "conditional_cost",
                                        "gate_binding", "global_binding"}
    assert set(view["attempts"][0]["comparison"]) == {
        "outcome", "reason", "layer", "deciding_layer", "reference", "reference_kind", "reference_measurement_id",
        "verification", "eligible",
        "mode", "confidence_scope", "selection_bias_possible", "excluded_reference_slots", "coverage", "noise",
        "scopes", "method", "unit", "calibration", "caveats", "bound_fallback"}
    assert set(view["attempts"][0]["comparison"]["noise"]) == {
        "kind", "allowance", "source", "source_kind", "variation", "units", "observed_cases", "extra_measurements"}
    assert "diagnostics" not in view          # Core's diagnostics[] is not in D4


def test_eligible_final_score_is_wrapped_and_the_global_pass_is_accepted():
    block = copy.deepcopy(BLOCK)
    attempt = block["attempts"][0]
    attempt["comparison"].update(deciding_layer="global", scopes=[GLOBAL], confidence_scope=None,
                                 verification="reused_evidence")
    attempt["global_comparison"] = attempt["comparison"]
    attempt["eligible_final_score"] = R(7, 10)
    row = independent_view(block)["attempts"][0]
    assert row["eligible_final_score"] == {"value": 0.7, "exact": R(7, 10)}
    assert (row["comparison"]["layer"], row["group"]) == ("global", "accepted")


@pytest.mark.parametrize("mutate", [
    lambda b: b["attempts"][0]["comparison"]["scopes"][0].__setitem__("point", {"format": "rational-hex/2",
                                                                                 "numerator": "1",
                                                                                 "denominator": "1"}),
    lambda b: b["attempts"][0]["comparison"].__delitem__("noise"),
    lambda b: b["attempts"][0]["comparison"]["scopes"][0].__setitem__("rule", "confidence"),
    lambda b: b["settings"].__setitem__("allowance", "0"),
    lambda b: b.__setitem__("evidence", "maybe"),
], ids=["bad-rational", "missing-noise", "unknown-rule", "allowance-not-rational", "unknown-evidence"])
def test_malformed_independent_block_raises(mutate):
    block = copy.deepcopy(BLOCK)
    mutate(block)
    with pytest.raises((ValueError, KeyError, TypeError)):
        independent_view(block)


@pytest.mark.parametrize("outcome,reason,group", [
    (None, None, "in_progress"),
    ("finalist", "passed", "in_progress"),            # gate passed, global pending
    ("promoted", None, "accepted"),
    ("rejected", "threshold_not_passed", "agent_worse"), ("rejected", "critical_failed", "agent_worse"),
    ("rejected", "empty_support", "not_enough_data"),
    ("rejected", "structurally_unattainable", "not_enough_data"),
    ("rejected", "candidate_incomplete", "infrastructure"), ("rejected", "reference_incomplete", "infrastructure"),
    ("rejected", "provenance_unavailable", "infrastructure"), ("rejected", "provenance_failed", "infrastructure"),
    ("abandoned", "editor_failed", "stopped"), ("abandoned", "editor_no_change", "no_candidate"),
    ("insufficient_evidence", "incomplete_sample", "infrastructure"),
    ("rejected", None, "agent_worse"), ("abandoned", None, "stopped"),
    ("insufficient_evidence", None, "not_enough_data"), ("declined", None, "no_candidate"),
    ("failed", None, "stopped"), ("retained", None, "agent_worse"),
    ("abandoned", "a_future_reason", "stopped"),
])
def test_attempt15_group_by_outcome_then_reason(outcome, reason, group):
    assert attempt15_group(outcome, reason) == group
    assert group in GROUPS


def test_attempt15_group_of_a_finalist_follows_cores_global_comparison():
    gate = {"outcome": "passed", "reason": "passed", "deciding_layer": "focus"}
    assert attempt15_group("finalist", "passed", global_comparison=None) == "in_progress"
    passed = {"outcome": "passed", "reason": "passed", "deciding_layer": "global"}
    worse = {"outcome": "comparison_not_passed", "reason": "threshold_not_passed", "deciding_layer": "global"}
    missing = {"outcome": "comparison_unavailable", "reason": "candidate_incomplete", "deciding_layer": "global"}
    assert [attempt15_group("finalist", "passed", global_comparison=c) for c in (passed, worse, missing)] == [
        "accepted", "agent_worse", "infrastructure"]
    assert attempt15_group("outlier", None) == "unknown"     # an outcome Core's projection does not admit


def test_family_15_notice_counts_an_attempt_closed_in_preflight_as_not_measured():
    outcome = {"outcome": "completed", "reason": "rounds_exhausted"}
    rounds = [{"round": 1, "outcome": "retained", "reason": None,
               "attempts": [{"attempt_id": "r01-a01", "outcome": "rejected", "reason": "structurally_unattainable"},
                            {"attempt_id": "r01-a02", "outcome": "abandoned", "reason": "editor_no_change"}]}]
    assert lab_notices(outcome, rounds, None, family=15) == [{
        "code": "no_candidate_measured", "rounds": 1,
        "by_reason": {"editor_no_change": 1, "structurally_unattainable": 1}}]
    measured = [dict(rounds[0], attempts=[{"attempt_id": "r01-a01", "outcome": "rejected",
                                           "reason": "threshold_not_passed"}])]
    assert lab_notices(outcome, measured, None, family=15) == []


def test_independent_evidence_words_are_cores():
    """The CLI decoder's copy of Core's evidence kinds equals ``core_raw``'s (family15_report.py:963)."""
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.independent_view import EVIDENCE
    assert set(EVIDENCE) == core_raw.family15_scope_vocabulary()["evidence"]


def test_attempt15_group_of_a_finalist_without_a_visible_global_comparison_depends_on_its_round():
    """Core cannot close a round before every finalist has a global result (family15_lifecycle.py:136-143), so a
    finalist Lab sees no global comparison for is in progress only while its round is open; in a closed round it
    is unknown (Lab cannot tell), never in progress."""
    gate = {"outcome": "passed", "reason": "passed", "deciding_layer": "focus"}
    for comparison in (None,):
        assert attempt15_group("finalist", "passed", global_comparison=comparison) == "in_progress"
        assert attempt15_group("finalist", "passed", global_comparison=comparison, round_closed=False) == "in_progress"
        assert attempt15_group("finalist", "passed", global_comparison=comparison, round_closed=True) == "unknown"
    passed = {"outcome": "passed", "reason": "passed", "deciding_layer": "global"}
    assert attempt15_group("finalist", "passed", global_comparison=passed, round_closed=True) == "accepted"
    assert attempt15_group(None, None, round_closed=True) == "in_progress"      # only a finalist is reclassified


def test_independent_view_groups_an_unseen_finalist_of_a_closed_round_as_unknown():
    block = copy.deepcopy(BLOCK)
    attempt = block["attempts"][0]
    attempt["outcome"], attempt["reason"], attempt["comparison"] = "finalist", "passed", None
    open_group = independent_view(block)["attempts"][0]["group"]
    closed = independent_view(block, closed_rounds=frozenset({attempt["round"]}))["attempts"][0]["group"]
    assert (open_group, closed) == ("in_progress", "unknown")


def test_attempt15_group_in_a_finished_campaign_is_never_in_progress():
    """Core admits ``campaign_finished`` ``stopped``/``cancelled`` with the current round open (campaign_schema.py
    ``_check_finished``: only ``completed`` needs every round closed) and writes the cancellation terminal with no
    Attempt reconciled (campaign.py, the owed-cancellation terminal). A finished campaign's unresolved attempt is
    therefore ``unknown``, never in progress; a recorded global comparison still gives its group."""
    gate = {"outcome": "passed", "reason": "passed", "deciding_layer": "focus"}
    for comparison in (None,):
        assert attempt15_group("finalist", "passed", global_comparison=comparison) == "in_progress"       # running
        assert attempt15_group("finalist", "passed", global_comparison=comparison, campaign_finished=True) == "unknown"
    assert attempt15_group(None, None) == "in_progress"
    assert attempt15_group(None, None, campaign_finished=True) == "unknown"
    passed = {"outcome": "passed", "reason": "passed", "deciding_layer": "global"}
    worse = {"outcome": "comparison_not_passed", "reason": "threshold_not_passed", "deciding_layer": "global"}
    assert [attempt15_group("finalist", "passed", global_comparison=c, campaign_finished=True) for c in (passed, worse)] == [
        "accepted", "agent_worse"]
    assert attempt15_group("rejected", "threshold_not_passed", campaign_finished=True) == "agent_worse"
    assert attempt15_group("abandoned", "editor_failed", campaign_finished=True) == "stopped"


def test_campaign_finished_is_cores_recorded_terminal():
    from evolution_lab.owner.independent_view import campaign_finished
    cancelled = {"outcome": "stopped", "reason": "cancelled", "continuation": {"kind": "none", "reason": None}}
    assert campaign_finished({"complete": True, "campaign_outcome": cancelled}) is True
    assert campaign_finished({"complete": False, "campaign_outcome": None}) is False
    assert campaign_finished({"campaign_outcome": cancelled}) is True        # either recorded fact suffices
    assert campaign_finished({}) is False and campaign_finished(None) is False


def test_independent_view_groups_an_open_rounds_finalist_of_a_finished_campaign_as_unknown():
    block = copy.deepcopy(BLOCK)
    block["attempts"][0]["comparison"] = None            # a gate finalist cancelled before its global comparison
    running = independent_view(block)["attempts"][0]["group"]
    finished = independent_view(block, campaign_finished=True)["attempts"][0]["group"]
    assert (running, finished) == ("in_progress", "unknown")


def test_attempt_carries_three_comparisons_from_their_own_core_fields():
    from evolution_lab.owner.independent_view import _attempt, _comparison
    gate = copy.deepcopy(COMPARISON)
    global_ = dict(COMPARISON, deciding_layer="global", scopes=[GLOBAL], reference_measurement_id="m-global")
    # Deliberately distinct summary: Lab copies each field rather than selecting or repairing one.
    summary = dict(gate, reason="critical_failed", outcome="comparison_not_passed")
    row = _attempt(dict(ATTEMPT, comparison=summary, gate_comparison=gate, global_comparison=global_))
    assert row["comparison"] == _comparison(summary)
    assert row["gate_comparison"] == _comparison(gate)
    assert row["global_comparison"] == _comparison(global_)
    assert row["group"] == "accepted"
    assert (row["gate_comparison"]["layer"], row["global_comparison"]["layer"]) == ("gate", "global")


def test_null_layer_stays_null():
    from evolution_lab.owner.independent_view import _attempt
    row = _attempt(ATTEMPT)
    assert row["global_comparison"] is None
    assert row["comparison"] == row["gate_comparison"]
    # A summary alone never supplies either explicit layer or a finalist's group.
    row = _attempt(dict(ATTEMPT, gate_comparison=None, global_comparison=None))
    assert row["comparison"] is not None and row["gate_comparison"] is row["global_comparison"] is None
    assert row["group"] == "in_progress"


@pytest.mark.parametrize("reference", ["m-7", "", None])
def test_reference_measurement_id_copied_as_is(reference):
    from evolution_lab.owner.independent_view import _comparison
    assert _comparison(dict(COMPARISON, reference_measurement_id=reference))["reference_measurement_id"] == reference


@pytest.mark.parametrize("reference", [7, True, [], {}])
def test_reference_measurement_id_refuses_non_string_values(reference):
    from evolution_lab.owner.independent_view import _comparison
    with pytest.raises(ValueError):
        _comparison(dict(COMPARISON, reference_measurement_id=reference))


def test_b8_shaped_core_attempt_is_unreadable():
    block = copy.deepcopy(BLOCK)
    del block["attempts"][0]["gate_comparison"]
    with pytest.raises(ValueError):
        independent_view(block)


def b10_block():
    block = copy.deepcopy(BLOCK)
    block["settings"]["bound_fallback"] = "point"
    comparison = block["attempts"][0]["comparison"]
    comparison.update(bound_fallback="point", caveats=["noise_allowance_empirical"])
    comparison["scopes"][0].update(rule="point", confidence=None, enclosure=None,
                                     fallback={"reason": "focus_unattainable"})
    return block


def test_b10_fallback_is_retained_in_settings_comparison_and_scope():
    view = independent_view(b10_block())
    assert view["settings"]["bound_fallback"] == "point"
    comparison = view["attempts"][0]["comparison"]
    assert comparison["bound_fallback"] == "point" and comparison["caveats"] == ["noise_allowance_empirical"]
    assert comparison["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}


@pytest.mark.parametrize("mutate", [
    lambda b: b["settings"].__setitem__("bound_fallback", "bound"),
    lambda b: b["settings"].__setitem__("new_field", 1),
    lambda b: b["attempts"][0]["comparison"].__setitem__("new_field", 1),
    lambda b: b["attempts"][0]["comparison"].__setitem__("caveats", ["future_caveat"]),
    lambda b: b["attempts"][0]["comparison"]["scopes"][0].__setitem__("lower", R(0)),
    lambda b: b["attempts"][0]["comparison"]["scopes"][0]["fallback"].__setitem__("reason", "future_reason"),
    lambda b: b["attempts"][0]["comparison"]["scopes"][0]["fallback"].__setitem__("new_field", 1),
    lambda b: b["settings"].__setitem__("mode", "future_mode"),
    lambda b: b["attempts"][0].__setitem__("reason", "future_reason"),
    lambda b: b["attempts"][0]["comparison"]["scopes"][0].__setitem__("status", "uncalibrated"),
    lambda b: b["attempts"][0]["comparison"]["noise"].__setitem__("new_field", 1),
])
def test_b10_unknown_keys_and_closed_values_are_not_dropped(mutate):
    block = b10_block()
    mutate(block)
    with pytest.raises((ValueError, KeyError, TypeError)):
        independent_view(block)


def test_binding_is_recorded_separately_never_inferred_from_a_result():
    bindings = {"r01-a01": {"gate_binding": {"source": "core_raw", "scopes": [
        {"name": "focus", "rule": "bound"}]}, "global_binding": None}}
    row = independent_view(b10_block(), bindings=bindings)["attempts"][0]
    assert row["gate_binding"] == bindings["r01-a01"]["gate_binding"]
    assert row["gate_comparison"]["scopes"][0]["rule"] == "point"
    assert row["global_binding"] is None
    assert independent_view(BLOCK)["attempts"][0]["gate_binding"] is None


def test_bootstrap_lower_and_confidence_are_copied_not_recomputed():
    block = copy.deepcopy(BLOCK)
    block["settings"].update(mode="statistical", repeats=2, train_slots=16, method="f13-bootstrap/1", unit=None)
    comparison = block["attempts"][0]["comparison"]
    comparison.update(mode="statistical", method="f13-bootstrap/1", unit=None, caveats=["intervals_empirical"])
    comparison["scopes"][0].update(rule="bootstrap", lower=R(-7, 100), exact=False, pairing="unpaired", enclosure=None)
    scope = independent_view(block)["attempts"][0]["gate_comparison"]["scopes"][0]
    assert scope["lower"] == {"value": -0.07, "exact": R(-7, 100)}
    assert scope["confidence"] == {"value": 0.9, "exact": R(9, 10)}
    from evolution_lab._independent_text import scope_statement
    assert "bootstrap 90%, lower -0.07" in scope_statement(scope)
    assert "bootstrap 90%, нижняя граница -0.07" in scope_statement(scope, "ru")


def test_null_threshold_and_uncalibrated_preflight_are_not_zero():
    from evolution_lab._independent_text import preflight_parts, scope_statement
    scope = scope_row(dict(FOCUS, threshold=None))
    assert "порог ещё не откалиброван" in scope_statement(scope, "ru")
    block = copy.deepcopy(PREFLIGHT_BLOCK)
    block["attempts"][0]["preflight"]["scopes"][0] = {"name": "focus", "status": "uncalibrated"}
    explanation = independent_view(block)["attempts"][0]["preflight"]
    assert explanation["scopes"][0]["threshold"] is None
    assert preflight_parts(explanation, "ru")[0] == "focus: порог ещё не откалиброван"


@pytest.mark.parametrize("binding", [
    {"source": "core_public", "scopes": []},
    {"source": "core_raw", "scopes": [{"name": "causal", "rule": "future_rule"}]},
    {"source": "core_raw", "scopes": [{"name": "future_scope", "rule": "bound"}]},
    {"source": "core_raw", "scopes": [{"name": "causal", "rule": "bound", "future_field": 0}]},
    {"source": "core_raw", "scopes": [], "future_field": 0},
])
def test_unknown_binding_shape_makes_view_unreadable(binding):
    with pytest.raises(ValueError):
        independent_view(BLOCK, bindings={"r01-a01": {"gate_binding": binding, "global_binding": None}})


@pytest.mark.parametrize("focus_first,slots", [(False, False), (False, True), (True, False), (True, True)])
def test_stages_only_with_focus_first_and_slots(focus_first, slots):
    from evolution_lab.cli.responses import _independent

    block = copy.deepcopy(BLOCK)
    if focus_first:
        block["settings"]["stages"] = "focus_first"
    if slots:
        block["attempts"][0]["slots"] = {"candidate": {"scheduled": 2, "dispatched": 1, "not_owed": 1}}
    raw = {ATTEMPT["attempt_id"]: {"focus_opened": True, "causal_opened": False, "focus_decided": "passed"}}
    view = independent_view(block, stages=raw)
    _independent(view)
    assert ("stages" in view["attempts"][0]) == (focus_first and slots)
    if not (focus_first and slots):
        view["attempts"][0]["stages"] = [{"scope": "focus", "state": "passed"},
                                            {"scope": "causal", "state": "waiting"}]
        with pytest.raises(ValueError):
            _independent(view)


@pytest.mark.parametrize("bad", [
    None, [], [{"scope": "focus", "state": "passed"}],
    [{"scope": "causal", "state": "waiting"}, {"scope": "focus", "state": "passed"}],
    [{"scope": "focus", "state": "future"}, {"scope": "causal", "state": "waiting"}],
    [{"scope": "focus", "state": "passed", "extra": None}, {"scope": "causal", "state": "waiting"}],
    [{"scope": "focus"}, {"scope": "causal", "state": "waiting"}],
], ids=["null", "empty", "missing-cell", "scope-order", "unknown-state", "extra-key", "missing-state"])
def test_stages_decoder_is_closed(bad):
    from evolution_lab.cli.responses import _independent

    block = copy.deepcopy(BLOCK)
    block["settings"]["stages"] = "focus_first"
    block["attempts"][0]["slots"] = {"candidate": {"scheduled": 2, "dispatched": 1, "not_owed": 1}}
    view = independent_view(block)
    _independent(view)
    view["attempts"][0]["stages"] = bad
    with pytest.raises(ValueError):
        _independent(view)
    del view["attempts"][0]["stages"]
    with pytest.raises(ValueError):
        _independent(view)
