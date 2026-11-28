"""Ordered dev64 §3 stage rules; inputs are small Owner-level projection facts."""
import pytest

from evolution_lab.owner.focus_stages import STATES, attempt_stages


def facts(decision=None, *, focus=True, causal=False):
    return {"focus_opened": focus, "causal_opened": causal, "focus_decided": decision}


def attempt(outcome=None, *, causal_status=None, comparison_outcome=None, deciding_layer="focus"):
    comparison = None if causal_status is None else {
        "outcome": comparison_outcome, "deciding_layer": deciding_layer,
        "scopes": [{"name": "causal", "status": causal_status}],
    }
    return {"outcome": outcome, "gate_comparison": comparison}


@pytest.mark.parametrize("projection,raw,state", [
    (attempt(), facts("passed"), "passed"),
    (attempt(), facts("comparison_not_passed"), "rejected"),
    (attempt("rejected"), facts("comparison_unavailable"), "unavailable"),
    (attempt("insufficient_evidence"), facts(), "incomplete"),
    (attempt(), facts(), "open"),
    (attempt(), facts(focus=False), "waiting"),
    (attempt(), facts("future_decision"), "unknown"),
], ids=["decided-passed", "decided-rejected-open-attempt", "decided-unavailable-closed-rejected",
        "closed-incomplete", "opened", "not-opened", "unknown-combination"])
def test_focus_rules(projection, raw, state):
    assert attempt_stages(projection, raw)[0] == {"scope": "focus", "state": state}


@pytest.mark.parametrize("projection,raw,state", [
    (attempt(causal_status="skipped"), facts("passed"), "not_run"),
    (attempt(), facts("comparison_not_passed"), "not_run"),
    (attempt("finalist", causal_status="available", comparison_outcome="passed"), facts("passed"), "passed"),
    (attempt("rejected", causal_status="available", comparison_outcome="comparison_not_passed",
             deciding_layer="causal"), facts("passed"), "rejected"),
    (attempt("rejected", causal_status="unavailable", comparison_outcome="comparison_unavailable",
             deciding_layer="causal"), facts("passed"), "unavailable"),
    (attempt("rejected", causal_status="unavailable", comparison_outcome="comparison_not_passed",
             deciding_layer="focus"), facts("passed", causal=True), "rejected"),
    (attempt("insufficient_evidence"), facts("passed", causal=True), "incomplete"),
    (attempt(), facts("passed", causal=True), "open"),
    (attempt(), facts("passed"), "waiting"),
    (attempt("abandoned"), facts("passed"), "unknown"),
], ids=["empty-C", "focus-rejected-open-attempt", "passed-focus-deciding-layer", "rejected", "unavailable",
        "critical-rejected-unavailable-estimate", "opened-incomplete", "opened", "continue-barrier-without-opening", "closed-not-opened"])
def test_causal_rules(projection, raw, state):
    assert attempt_stages(projection, raw)[1] == {"scope": "causal", "state": state}


@pytest.mark.parametrize("decision", ["comparison_not_passed", "comparison_unavailable", None])
def test_focus_failure_precedes_causal_outcome(decision):
    projection = attempt("insufficient_evidence" if decision is None else "rejected",
                         causal_status="available", comparison_outcome="passed")
    assert attempt_stages(projection, facts(decision, causal=True))[1]["state"] == "not_run"


def test_empty_c_precedes_focus_rejection():
    assert attempt_stages(attempt(causal_status="skipped"), facts("comparison_not_passed")) == [
        {"scope": "focus", "state": "rejected"}, {"scope": "causal", "state": "not_run"}]


def test_missing_facts_stays_unknown_even_with_a_complete_comparison():
    assert attempt_stages(attempt("finalist", causal_status="available", comparison_outcome="passed"), None) == [
        {"scope": "focus", "state": "unknown"}, {"scope": "causal", "state": "unknown"}]


def test_deciding_layer_alone_never_supplies_causal_result():
    assert attempt_stages(attempt(causal_status="not_measured", comparison_outcome="passed",
                                  deciding_layer="causal"), facts("passed"))[1]["state"] == "waiting"


def test_closed_state_vocabulary():
    assert STATES == ("waiting", "open", "passed", "rejected", "unavailable", "not_run", "incomplete", "unknown")
