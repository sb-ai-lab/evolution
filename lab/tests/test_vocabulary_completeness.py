"""Every code of core's supported family-14 and family-15 vocabularies has a place in Lab (dev50 spec §5.1,
ADR-0019): a stage, a hint, a group or a label. A new core code without a place fails here, on the re-pin."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evolution_lab._suspension import FINAL_SUSPENSIONS, suspension_hint
from evolution_lab.owner import core_raw
from evolution_lab.owner.progress import STAGE_OF_ACTION

#: Families whose action and suspension vocabularies Lab reads (family 15 shares family 14's; Core b8).
FAMILIES = (14, 15)
#: Vocabularies that exist for family 14 only (look decisions, selection reasons, the attempt outcome table).
FAMILY14 = (14,)
LABELS_JS = Path(__file__).parents[1] / "src/evolution_lab/panel/static/lib/labels.js"


def _labels() -> dict:
    text = LABELS_JS.read_text(encoding="utf-8")
    body = re.search(r"/\* LABELS-JSON-BEGIN \*/(.*)/\* LABELS-JSON-END \*/", text, re.S).group(1)
    return json.loads(body[body.index("{"):body.rindex("}") + 1])


@pytest.mark.parametrize("family", FAMILIES)
def test_every_status_action_has_a_stage(family):
    assert core_raw.status_action_names(family) <= set(STAGE_OF_ACTION)


@pytest.mark.parametrize("complete,suspended,phase", [
    (False, False, "evaluation"), (False, True, "suspended"), (True, False, "finished"),
])
def test_focus_decision_is_attempt_evaluation_without_changing_terminal_phase(complete, suspended, phase):
    """Core decides focus after attempt measurement, before causal evaluation or attempt closure."""
    from dataclasses import asdict
    from evolution.tree.campaign_view import DecideFocus
    from evolution_lab.owner.progress import CampaignPlan, campaign_progress, validate_progress

    action = DecideFocus("r01-a01")
    core = {"actions": [{"kind": type(action).__name__, "fields": asdict(action)}],
            "complete": complete, "suspended": suspended}
    progress = campaign_progress(core, CampaignPlan(None, None, None, None), None)
    assert progress["stages"] == ["evaluation"]
    assert progress["phase"] == phase
    validate_progress(progress, "campaign")


def test_family_14_renames_the_cohort_actions():
    names = core_raw.status_action_names(14)
    assert {"BindFinalMember", "RunFinalStage", "CompleteFinalMember"} <= names
    assert not {"BindCohortMember", "RunCohortStage", "CompleteCohortMember"} & names
    assert {STAGE_OF_ACTION[n] for n in ("BindFinalMember", "RunFinalStage", "CompleteFinalMember")} == {"selection"}


@pytest.mark.parametrize("family", FAMILY14)
def test_every_look_decision_has_a_label(family):
    assert core_raw.confidence_codes(family)["decisions"] <= set(_labels()["confidence_decision"])


@pytest.mark.parametrize("family", FAMILY14)
def test_every_selection_reason_has_a_label(family):
    assert core_raw.selection_reasons(family) <= set(_labels()["selection_reason"])


@pytest.mark.parametrize("family", FAMILIES)
def test_every_suspension_has_a_label(family):
    assert core_raw.suspension_reasons(family) <= set(_labels()["suspension_reason"])


def test_every_family_14_suspension_has_a_hint():
    """Family-14 final, connection and Grouper pauses each have a hint in both languages."""
    expected = {"final_incomplete", "connection_incompatible", "connection_preflight_unavailable",
                "connection_preflight_inconclusive", "grouper_provider_failed", "grouper_failing"}
    assert set(FINAL_SUSPENSIONS) == expected
    assert expected <= core_raw.suspension_reasons(14)
    for reason in expected:
        assert suspension_hint(reason, "en") and suspension_hint(reason, "ru"), reason


@pytest.mark.parametrize("family", FAMILIES)
def test_every_caveat_has_a_label(family):
    assert core_raw.caveat_kinds(family) <= set(_labels()["caveat"])


def test_unknown_family_is_refused():
    with pytest.raises(ValueError):
        core_raw.suspension_reasons(12)


@pytest.mark.parametrize("family", FAMILY14)
def test_every_attempt_reason_has_a_group(family):
    """Spec §6.6: a reason has a group in ATTEMPT_GROUP, or family 14 groups it by core's own fields: the finalist
    outcome by its final contrast, focus_below_margin by its look."""
    from evolution_lab.owner.campaign_views import ATTEMPT_GROUP
    words = core_raw.final_vocabulary()
    for outcome, reasons in core_raw.attempt_outcome_table(family).items():
        for reason in reasons:
            placed = (reason in ATTEMPT_GROUP
                      or family == 14 and (outcome == words["gate_pass_outcome"]
                                           or reason == words["focus_rejection_reason"]))
            assert placed, (family, outcome, reason)


def test_family15_vocabularies_are_separate_and_cores():
    from evolution.tree.campaign_schema import feedback_outcomes
    from evolution.tree.family15_schema import MODES, OUTCOMES, REASONS, UNAVAILABLE
    attempt, comparison = core_raw.family15_attempt_vocabulary(), core_raw.family15_comparison_vocabulary()
    assert comparison["outcomes"] == {"passed", "comparison_not_passed", "comparison_unavailable"}
    assert comparison == {"outcomes": OUTCOMES, "reasons": REASONS, "unavailable": UNAVAILABLE}
    assert "structurally_unattainable" in attempt["reasons"]
    assert "structurally_unattainable" not in comparison["reasons"]
    assert "comparison_preflight_failed" not in attempt["reasons"]      # a Core event name, not a reason
    table = feedback_outcomes(15)
    assert attempt["reasons"] == frozenset().union(*table.values())
    assert attempt["outcomes"] == set(table) | {"declined", "failed", "promoted", "retained"}
    assert core_raw.family15_scope_vocabulary() == {
        "rules": {"point", "bound", "off", "bootstrap"},
        "statuses": {"skipped", "available", "unavailable", "not_measured"},
        "layers": {"focus", "causal", "global"}, "references": {"stored_non_contemporaneous", "fresh"},
        "evidence": {"snapshot_only", "unavailable", "authenticated_publications"},
        "preflight_statuses": {"skipped", "calculated", "uncalibrated"},
        "modes": {"point", "confidence", "unfiltered", "statistical"}}
    assert core_raw.family15_scope_vocabulary()["rules"] == {"point", "bound", "off", "bootstrap"}
    assert core_raw.family15_scope_vocabulary()["modes"] == MODES


def test_family14_only_vocabularies_refuse_family_15():
    for reader in (core_raw.attempt_outcome_table, core_raw.confidence_codes, core_raw.selection_reasons):
        with pytest.raises(ValueError):
            reader(15)


def test_every_family15_attempt_reason_and_outcome_has_a_group():
    """Task 5's ``attempt15_group``: no family-15 attempt reason or closed outcome falls to ``unknown``."""
    from evolution_lab.owner.independent_view import attempt15_group, reason_group
    vocabulary = core_raw.family15_attempt_vocabulary()
    for reason in vocabulary["reasons"]:
        # An explicit mapping, not the outcome fallback: a new Core reason must fail here (``passed`` is a finalist's).
        assert reason == "passed" or reason_group(reason) is not None, reason
    for outcome in vocabulary["outcomes"]:
        assert attempt15_group(outcome, None) != "unknown", outcome


def test_every_family15_comparison_code_has_a_label():
    """Task 8's ``labels.js`` kinds: every comparison outcome and reason of family 15 has a label."""
    labels = _labels()
    comparison = core_raw.family15_comparison_vocabulary()
    assert comparison["outcomes"] <= set(labels["comparison15_outcome"])
    assert comparison["reasons"] <= set(labels["comparison15_reason"])


def test_lab_words_of_family_14_are_cores():
    words = core_raw.final_vocabulary()
    assert (words["gate_pass_outcome"], words["incomplete_reason"], words["focus_rejection_reason"]) == \
        ("finalist", "final_incomplete", "focus_below_margin")


def _action_kinds(value) -> set[str]:
    """Every ``kind`` of an ``actions`` list of core's status projection, wherever it sits in an answer."""
    if isinstance(value, dict):
        found = {row["kind"] for row in value.get("actions") or () if isinstance(row, dict) and "kind" in row} \
            if isinstance(value.get("actions"), list) else set()
        return found.union(*(_action_kinds(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_action_kinds(item) for item in value))
    return set()


def test_status_actions_seen_in_the_family_14_snapshot_have_a_stage():
    """Spec §6.6: every action name of a real family-14 status projection has a stage. Progress carries stages only
    and the snapshot's campaigns are finished (``actions: []``), so the names come from where the Owner status holds
    them — ``result.core.actions`` — in the snapshot's answers, its stream frames and the status kept at the pause."""
    fixtures = Path(__file__).parent / "fixtures"
    routes = json.loads((fixtures / "panel/family14/routes.json").read_text(encoding="utf-8"))
    events = json.loads((fixtures / "panel/family14/events.json").read_text(encoding="utf-8"))
    frames = [json.loads(line[len("data:"):]) for stream in events.values() for frame in stream
              for line in frame.splitlines() if line.startswith("data:")]
    paused = json.loads((fixtures / "family14/connection-suspended-status.json").read_text(encoding="utf-8"))
    seen = _action_kinds(routes) | _action_kinds(frames) | _action_kinds(paused)
    assert seen, "no action kinds in the family14 snapshot: look where progress carries them"
    assert seen <= set(STAGE_OF_ACTION), seen - set(STAGE_OF_ACTION)
