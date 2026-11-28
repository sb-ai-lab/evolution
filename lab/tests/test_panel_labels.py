"""Every closed code set the panel shows has exactly one label and tone (spec 2026-09-30 §2.4).

Drift guard, by kind:
- Lab or core constants (imported; a change of the service fails here): phase, attempt_group, stale_code,
  control_state, resume_state, settlement, failure_stage, reflection_state, target_state, round_outcome,
  outcome, results_state, section_state, project_state, unavailable_reason, target_requirement, gate_mark, comparison,
  confidence_decision, cohort_role, suspension_reason (`owner.core_raw`, pinned core codes); the family-15 kinds
  attempt15_outcome, comparison15_outcome, comparison15_reason, scope_status, scope_rule, independent_evidence,
  gate15_mode (`core_raw.family15_*_vocabulary()`), comparison_reference and comparison_layer (Lab's words of the
  `independent` block, `owner.independent_view`).
  (`owner.measurements._COMPARISONS` plus `unknown`, which the module emits as a literal: source-checked).
- Pinned to docs/api/projects.md without a code set to check against: fact_holds (the boolean `holds`).
- Pinned to the emitting module with a source check: nontext_kind (`binary`, `mode` in owner/results.py).
- Pinned to docs/api/projects.md with a source check (a removed or renamed code fails here; a code ADDED to
  the emitting module is not detected): step_state, scenario_state, blocked_code, operation_state. Lab keeps
  these as literals in the module that emits them; the test asserts every pinned code still occurs there as
  a quoted string literal.
"""
import json
from pathlib import Path
import re

import pytest

import evolution_lab
import evolution_lab.panel as panel_pkg
from evolution.tree.campaign_schema import CAMPAIGN_OUTCOMES, _MODEL_GROUPING_ROUND_OUTCOMES
from evolution_lab.owner.measurements import _COMPARISONS
from evolution_lab.owner import core_raw, independent_view
from evolution_lab.owner.focus_stages import STATES as STAGE_STATES
from evolution_lab.owner.campaign_views import ATTEMPT_STAGE, GROUPS, OBSERVE_CAVEAT, REFLECTION_STATE
from evolution_lab.owner.models import (
    RESULTS_STATES, RESUME_PAIRS, SECTION_STATES, TARGET_REQUIREMENTS, TARGET_ROW_STATES, TIMES_REASONS, _SETTLEMENTS,
    _TARGET_STATES)
from evolution_lab.owner.progress import PHASES
from evolution_lab.owner.store import _OPERATION_STATES
from evolution_lab.projects.facts import ACCEPTABLE_CODES
from evolution_lab.projects.models import PROJECT_STATES
from evolution_lab.projects.status import BLOCKED_CODES

TONES = {"blue", "green", "amber", "violet", "neutral"}
SOURCE = Path(panel_pkg.__file__).parent / "static" / "lib" / "labels.js"


def labels() -> dict:
    text = SOURCE.read_text(encoding="utf-8")
    block = re.search(r"/\* LABELS-JSON-BEGIN \*/\s*export const LABELS = (.*?)\s*/\* LABELS-JSON-END \*/", text, re.S)
    assert block, "labels.js must keep the LABELS object between the JSON markers"
    return json.loads(block.group(1).rstrip().rstrip(";"))


EXPECTED = {
    # -- imported constants ------------------------------------------------------------------------
    "phase": set(PHASES),
    "attempt_group": set(GROUPS),
    "stale_code": set(ACCEPTABLE_CODES),
    # Lab's durable control states (owner/store.py `_OPERATION_STATES`, the same set models.py validates).
    "control_state": set(_OPERATION_STATES),
    # The states of a resume verdict: the first element of `owner.models.RESUME_PAIRS`.
    "resume_state": {state for state, _reason in RESUME_PAIRS},
    "settlement": set(_SETTLEMENTS),
    # Where an attempt that never reached a decision stopped: the stage of `campaign_views.ATTEMPT_STAGE`,
    # plus `unknown` (`failure_of` for a reason the table lacks; campaign_views.py `ATTEMPT_STAGE.get`).
    "failure_stage": {stage for stage, _role in ATTEMPT_STAGE.values()} | {"unknown"},
    # Values of `REFLECTION_STATE` plus the three `reflection_of` sets without a counted entry
    # (owner/campaign_views.py `reflection_of`: `absent`, `in_progress`, `unsettled`).
    "reflection_state": set(REFLECTION_STATE.values()) | {"unsettled", "in_progress", "absent"},
    # Resume states that carry a target requirement (`models._TARGET_STATES`) plus the states of a target
    # row (`models.TARGET_ROW_STATES`, the validator of `lab-owner-target/1`).
    "target_state": set(_TARGET_STATES) | set(TARGET_ROW_STATES),
    # Core a24's closed outcome of a round, the widest table (family 9; unchanged since a21): `promoted stalled clean` +
    # `unscored` + `unmeasurable` + `condition_changed` + `unplanned`
    # (evolution/tree/campaign_schema.py `_MODEL_GROUPING_ROUND_OUTCOMES`; a private core name, like the
    # tables Lab's own owner/core_raw.py reads; re-check on a core re-pin).
    "round_outcome": set(_MODEL_GROUPING_ROUND_OUTCOMES),
    # Core's public closed set of campaign outcomes; docs/guide/results.md "`campaign_outcome` ... `outcome`".
    "outcome": set(CAMPAIGN_OUTCOMES),
    # `owner.models.RESULTS_STATES` (validator of `OperationStatus.results_state`).
    "results_state": set(RESULTS_STATES),
    # `owner.models.SECTION_STATES`: `progress_state`, `times_state` (docs/api/service-s5.md:382-386); the same
    # words serve `policy_state`, `details_state`, `champions_state`.
    "section_state": set(SECTION_STATES),
    # `projects.models.PROJECT_STATES` (docs/api/projects.md:105).
    "project_state": set(PROJECT_STATES),
    # Why a section is unavailable: `details_reason` and `times_reason` (spec §2.4). `owner.models.TIMES_REASONS`;
    # `details_reason` takes the same two `CoreRawUnavailable` codes (owner/core_raw.py:34, :65;
    # owner/campaign_views.py `details_unavailable`, docs/api/owner-s4.md "`details_reason`").
    "unavailable_reason": set(TIMES_REASONS),
    # -- docs-pinned, source-checked (see PINNED_IN_SOURCE) ------------------------------------------
    # docs/api/projects.md:630-636 "Step state": done | stale | ready | blocked.
    "step_state": {"done", "stale", "ready", "blocked"},
    # docs/api/projects.md:625-628 "Scenario state": unavailable | available | produced.
    "scenario_state": {"unavailable", "available", "produced"},
    # `projects.status.BLOCKED_CODES`: why a step cannot be done as it is (docs/api/projects.md "Status", /5).
    "blocked_code": set(BLOCKED_CODES),
    # docs/api/projects.md:895-899 "`operation_state` is `present` ..., `other_pair` ... or `unavailable`".
    "operation_state": {"present", "other_pair", "unavailable"},
    # -- docs-pinned, no code set in Lab ----------------------------------------------------------------
    # A fact's `holds` is a boolean (docs/api/projects.md:364-375, lab-project-facts/4 "`holds` is `true`
    # when `value` is truthy ... Clients should read `holds`"): the panel labels String(holds).
    "fact_holds": {"true", "false"},
    # -- diff / measurement views (branch 2) ---------------------------------------------------------------
    # `owner.measurements._COMPARISONS` plus the literal "unknown" (measurements.py, source-checked below).
    "comparison": set(_COMPARISONS) | {"unknown"},
    # A `nontext_changes` entry's kind: owner/results.py emits 'binary' and 'mode' (source-checked below).
    "nontext_kind": {"binary", "mode"},
    # `owner.models.TARGET_REQUIREMENTS`: the requirement a resume verdict carries.
    "target_requirement": set(TARGET_REQUIREMENTS),
    # The observe mark of lab-campaign-view/7 (spec 2026-10-09 §6.1): core's caveat kind, `owner.campaign_views`;
    # `unknown` is the panel's fail-closed mark for family 14 with a null gate mode (final review I1).
    "gate_mark": {OBSERVE_CAVEAT, "unknown", "statistical"},
    # Family 14 through the adapter (ADR-0019): look decisions, shared final member roles and suspensions.
    # The historical cohort_role label key retains champion/finalist, also used by core's family-14 final.
    "confidence_decision": set(core_raw.confidence_codes()["decisions"]),
    "cohort_role": set(core_raw.confidence_codes()["cohort_roles"]),
    "selection_reason": set(core_raw.selection_reasons()),
    "suspension_reason": set(core_raw.suspension_reasons()),
    "caveat": set(core_raw.caveat_kinds()),
    # -- family 15, the `independent` block of lab-campaign-view/9 (spec 2026-10-19 D8) ---------------------
    # Core's codes, read through the pinned adapter (ADR-0019).
    "attempt15_outcome": set(core_raw.family15_attempt_vocabulary()["outcomes"]),
    "comparison15_outcome": set(core_raw.family15_comparison_vocabulary()["outcomes"]),
    "comparison15_reason": set(core_raw.family15_comparison_vocabulary()["reasons"]),
    "scope_status": set(core_raw.family15_scope_vocabulary()["statuses"]),
    "stage_state": set(STAGE_STATES),
    "scope_rule": set(core_raw.family15_scope_vocabulary()["rules"]),
    "independent_evidence": set(core_raw.family15_scope_vocabulary()["evidence"]),
    "gate15_mode": set(core_raw.family15_scope_vocabulary()["modes"]),
    # Lab's words: `reference` (stored | fresh | unknown, from Core's reference kind or null) and `layer`
    # (gate | global, from Core's deciding layer); the panel labels these, never Core's raw kinds.
    "comparison_reference": set(independent_view.REFERENCES),
    "comparison_layer": set(independent_view.LAYERS),
}


SRC = Path(evolution_lab.__file__).parent
PINNED_IN_SOURCE = {  # kind -> the module that emits its codes
    "step_state": "projects/status.py",
    "scenario_state": "projects/status.py",
    "blocked_code": "projects/status.py",
    "operation_state": "projects/session.py",
}


def test_comparison_unknown_and_nontext_kinds_are_still_emitted():
    measurements = (SRC / "owner/measurements.py").read_text(encoding="utf-8")
    # The comparison literal itself (`_compare`), not the unrelated `usage.get("unknown")`.
    assert re.search(r'def _compare\(.*?return "unknown"', measurements, re.S)
    assert '"comparison": "unknown"' in measurements
    results = (SRC / "owner/results.py").read_text(encoding="utf-8")
    for code in EXPECTED["nontext_kind"]:
        assert f"'{code}'" in results or f'"{code}"' in results, code


@pytest.mark.parametrize("kind", sorted(PINNED_IN_SOURCE))
def test_pinned_codes_are_still_emitted(kind):
    text = (SRC / PINNED_IN_SOURCE[kind]).read_text(encoding="utf-8")
    for code in EXPECTED[kind]:
        assert f'"{code}"' in text, (kind, code, PINNED_IN_SOURCE[kind])


def test_every_kind_of_the_brief_is_covered():
    assert set(labels()) == set(EXPECTED)


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_every_code_has_exactly_one_label(kind):
    table = labels()[kind]
    assert set(table) == EXPECTED[kind]
    for code, (text, tone) in table.items():
        assert text and tone in TONES, (kind, code)


def test_tones_follow_the_design():
    table = labels()
    assert table["attempt_group"]["accepted"][1] == "green"
    assert table["attempt_group"]["infrastructure"][1] == "violet"
    assert table["attempt_group"]["not_enough_data"][1] == "amber"
    assert table["attempt_group"]["agent_worse"][1] == "neutral"
    assert table["attempt_group"]["in_progress"][1] == "blue"
    assert table["resume_state"]["cleanup_first"][1] == "amber"
    assert table["section_state"]["present"][1] == "green"
    assert table["section_state"]["unavailable"][1] == "amber"
    assert table["results_state"]["delete_failed"][1] == "violet"
    assert table["control_state"]["orphaned"][1] == "violet"
    assert table["phase"]["suspended"][1] == "amber"
    assert table["stale_code"]["inputs_changed"][1] == "amber"
    assert table["fact_holds"]["true"][1] == "green" and table["fact_holds"]["false"][1] == "amber"
    assert {tone for _text, tone in table["unavailable_reason"].values()} == {"amber"}
    assert {tone for _text, tone in table["gate_mark"].values()} == {"neutral"}
    assert {tone for _text, tone in table["caveat"].values()} == {"neutral"}


def test_confidence_decision_tones():
    table = labels()["confidence_decision"]
    assert table["finalist"][0] == "Finalist (entered final)" and table["rejected"][0] == "Rejected"
    assert table["finalist"][1] == "neutral" and table["insufficient_evidence"][1] == "amber"
    assert table["rejected"][1] == "neutral" and table["continue"][1] == "blue"


def test_family15_tones_follow_the_scale():
    """Passed green, not passed neutral, unavailable amber (spec 2026-10-19 D8)."""
    table = labels()
    assert [table["comparison15_outcome"][k][1] for k in ("passed", "comparison_not_passed", "comparison_unavailable")] \
        == ["green", "neutral", "amber"]
    unavailable = core_raw.family15_comparison_vocabulary()["unavailable"]
    assert {table["comparison15_reason"][k][1] for k in unavailable} == {"amber"}
    assert table["comparison15_reason"]["passed"][1] == "green"
    assert table["scope_status"]["unavailable"][1] == "amber"
    assert table["independent_evidence"]["authenticated_publications"][1] == "green"
    assert {table["independent_evidence"][k][1] for k in ("snapshot_only", "unavailable")} == {"amber"}


def test_every_scope_display_is_read_by_the_panel():
    """``independent_view.DISPLAY`` is the closed set the panel's statement switch reads (lib/views/independent.js):
    a display added in Owner fails here until the panel states it."""
    text = (Path(panel_pkg.__file__).parent / "static" / "lib" / "views" / "independent.js").read_text(encoding="utf-8")
    for display in independent_view.DISPLAY:
        assert f'"{display}"' in text, display


def test_independent_layer_and_reference_words():
    table = labels()
    assert table["comparison_layer"] == {
        "gate": ["Gate comparison", "neutral"], "global": ["Global comparison", "neutral"]}
    assert table["comparison_reference"] == {
        "stored": ["stored measurement", "neutral"], "fresh": ["fresh measurement", "neutral"],
        "unknown": ["measurement not recorded", "amber"]}
