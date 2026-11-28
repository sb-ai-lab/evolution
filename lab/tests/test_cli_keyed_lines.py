"""Stable keys preserve the campaign/status text captured before the Task 2 refactor."""
import io
import json
from pathlib import Path

import pytest

from evolution_lab.cli import output
from evolution_lab.owner.independent_view import independent_view
from test_owner_independent_view import PREFLIGHT_BLOCK
from test_service_cli import FAMILY14, _family14_connection_suspended, _status, _view


@pytest.mark.parametrize("state", [
    "waiting", "open", "passed", "rejected", "unavailable", "not_run", "incomplete", "unknown",
])
def test_stages_text_restates_the_shared_states(state):
    from evolution_lab._independent_text import stages_text

    assert stages_text([{"scope": "focus", "state": "passed"}, {"scope": "causal", "state": state}]) == (
        f"stages: focus passed · causal {state}")


def family14():
    return {**_view(json.loads((FAMILY14 / 'observe-result.json').read_text())), "lab_notices_state": "present"}


def family15():
    routes = json.loads(Path('tests/fixtures/panel/family15/routes.json').read_text())
    from panel_fixtures import current_form
    value = current_form(next(route['body'] for route in routes.values()
                 if isinstance(route.get('body'), dict) and route['body'].get('schema') == 'lab-campaign-view/10'))
    value['lab_notices_state'] = 'present'
    value['independent']['attainability']['preflight'] = independent_view(PREFLIGHT_BLOCK)['attainability']['preflight']
    return value


def status():
    value = _status({'suspended': True, 'suspensions': [
        {'seq': 4, 'reason': 'editor_failing', 'owner': 'editor', 'evidence_kind': 'stage_record'}]},
        refusal={'code': 'corpus_inside_role_workspace', 'path': 'lab/corpus', 'class': 'hard', 'instance_digest': None})
    value['warnings'] = [{'code': 'first', 'detail': {'count': 2, 'missing': None}},
                         'ignored', {'code': 'second', 'detail': None}]
    return value


EXPECTED_FAMILY14 = ['outcome: completed/rounds_exhausted',
 'reflector probe: unavailable (unreadable)',
 'gate: observe — not evidence of quality (observe_gate_not_enforced, uncalibrated_point_estimates, '
 'champion_not_contemporaneous)',
 'settings: stability=stable round_stop=first_accepted refresh=every 3, performed 0',
 'connection: compatible compatible attempts=1',
 'details unavailable: core_raw_unsupported',
 'round 1 promoted baseline=- reflection=- final=selected:r01-a01/finalist_selected',
 '  r01-a01 accepted observe_finalist gate=finalist/observe_finalist focus=1 [-, -] exact=- margin=0 '
 'decision=accepted/global_off',
 'round 2 clean baseline=- reflection=-']


EXPECTED_FAMILY15 = ['outcome: completed/rounds_exhausted',
 'reflector probe: unavailable (unreadable)',
 'settings: stability=stable round_stop=first_accepted refresh=every 3, performed 0',
 'connection: compatible compatible attempts=1',
 'round 1 promoted baseline=v0 hyp=2',
 '  r01-a01 accepted passed',
 'round 2 retained baseline=v1 reflection=absent',
 '  r02-a01 not_enough_data structurally_unattainable',
 'independent: mode=confidence R=1 verification=reuse noise=negligible allowance=0.00 (assumed_negligible) '
 'stability=stable fresh_parent=no global=point',
 'round 1 preflight impossible (stored_reference)',
 'r01-a01 accepted finalist/passed',
 '  gate binding (core_raw): focus=bound, causal=bound',
 '  global binding (core_raw): global=point',
 '  gate passed/passed (focus) · vs round champion · stored measurement · ref m-r00-champi',
 '  focus: result rule=bound · gain 1.00 (estimate); with 10% confidence not below 0.8744; threshold > -0.90 → passed',
 '  causal: result rule=bound · skipped',
 '  global passed/passed (global) · vs round champion · stored measurement · ref m-r00-champi',
 '  global: result rule=point · gain 1.00; threshold > 0.00 → passed',
 '  eligible final score 1.00',
 'r02-a01 not_enough_data rejected/structurally_unattainable · no comparison',
 '  preflight impossible: global maximal gain 0.00 vs threshold > 0.00 (cannot pass)']


EXPECTED_INDEPENDENT = ['independent: mode=confidence R=1 verification=reuse noise=negligible allowance=0.00 (assumed_negligible) '
 'stability=stable fresh_parent=no global=point',
 'round 1 preflight impossible (stored_reference)',
 'r01-a01 accepted finalist/passed',
 '  gate binding (core_raw): focus=bound, causal=bound',
 '  global binding (core_raw): global=point',
 '  gate passed/passed (focus) · vs round champion · stored measurement · ref m-r00-champi',
 '  focus: result rule=bound · gain 1.00 (estimate); with 10% confidence not below 0.8744; threshold > -0.90 → passed',
 '  causal: result rule=bound · skipped',
 '  global passed/passed (global) · vs round champion · stored measurement · ref m-r00-champi',
 '  global: result rule=point · gain 1.00; threshold > 0.00 → passed',
 '  eligible final score 1.00',
 'r02-a01 not_enough_data rejected/structurally_unattainable · no comparison',
 '  preflight impossible: global maximal gain 0.00 vs threshold > 0.00 (cannot pass)']


EXPECTED_MASTER_TEXT = (
    'lab-operation-status/8 op-1 finished process=exit:1\n'
    'refused: corpus_inside_role_workspace at lab/corpus (hard)\n'
    'suspended: editor_failing (owner=editor, evidence=stage_record)\n'
    "hint: the Editor keeps failing outside its edit (a timeout, a crash or an external problem such as network, host resources or an agent CLI's login); fix the external cause and continue - changing the Editor's program, prompt, limits or environment needs a new Bundle and a new campaign\n"
    'continuation: lab resume op-1 --target <ready target of the same bundle> --generation <owner generation>\n'
    'warning: first count=2 missing=-\n'
    'warning: second\n'
)


def test_keyed_texts_equal_campaign_lines_family_14():
    value = family14()
    texts = [text for _, text in output.campaign_keyed_lines(value)]
    assert texts == output._campaign_lines(value) == EXPECTED_FAMILY14


def test_keyed_texts_equal_campaign_lines_family_15():
    value = family15()
    texts = [text for _, text in output.campaign_keyed_lines(value)]
    assert texts == output._campaign_lines(value) == EXPECTED_FAMILY15


def test_family_14_keys():
    assert [key for key, _ in output.campaign_keyed_lines(family14())] == [
        "outcome", "reflector_probe", "gate", "settings", "connection", "details", "round:1", "attempt:1:r01-a01", "round:2"]


def test_family_15_keys():
    assert [key for key, _ in output.campaign_keyed_lines(family15())] == [
        "outcome", "reflector_probe", "settings", "connection", "round:1", "attempt:1:r01-a01", "round:2", "attempt:2:r02-a01",
        "independent", "preflight:1", "ind:1:r01-a01", "ind:1:r01-a01:gate:binding",
        "ind:1:r01-a01:global:binding", "ind:1:r01-a01:gate",
        "ind:1:r01-a01:gate:scope:focus", "ind:1:r01-a01:gate:scope:causal",
        "ind:1:r01-a01:global", "ind:1:r01-a01:global:scope:global",
        "ind:1:r01-a01:eligible", "ind:2:r02-a01", "ind:2:r02-a01:preflight"]


def test_keys_are_unique_after_numbering():
    value = family15()
    value["lab_notices"] = [{"code": "not_measured"}] * 3
    value["rounds"] *= 3
    value["independent"]["attempts"] *= 3
    value["independent"]["attainability"]["preflight"] *= 3
    comparison = value["independent"]["attempts"][0]["global_comparison"]
    comparison["scopes"] *= 3
    pairs = output.campaign_keyed_lines(value)
    keys = [key for key, _ in pairs]
    assert len(set(keys)) == len(keys)
    independent_keys = [key for key, _ in output.independent_keyed_lines(value["independent"])]
    assert len(set(independent_keys)) == len(independent_keys)
    for key in ("notice:not_measured", "round:1", "attempt:1:r01-a01", "preflight:1", "ind:1:r01-a01",
                "ind:1:r01-a01:global", "ind:1:r01-a01:global:scope:global", "ind:2:r02-a01:preflight"):
        assert key in keys and key + "#2" in keys and key + "#3" in keys
    assert output._numbered([("same", "a"), ("other", "b"), ("same", "c"), ("same", "d")]) == [
        ("same", "a"), ("other", "b"), ("same#2", "c"), ("same#3", "d")]


def test_suspension_keys_follow_notices():
    value = _view(_family14_connection_suspended(), {"phase": "suspended"})
    value["lab_notices"] = [{"code": "not_measured"}]
    keys = [key for key, _ in output.campaign_keyed_lines(value)]
    assert keys[-3:] == ["notice:not_measured", "suspended", "suspended-hint"]


def test_other_schemas_have_no_campaign_keys():
    assert output.campaign_keyed_lines({"schema": "lab-campaign-view/7"}) == []


def test_independent_text_wrapper_and_report_caller_keep_their_text():
    block = family15()["independent"]
    assert [text for _, text in output.independent_keyed_lines(block)] == EXPECTED_INDEPENDENT
    assert output.independent_lines(block) == EXPECTED_INDEPENDENT
    report = {"schema": "lab-owner-report/15", "report": {
        "operation": "campaign", "core": {"family_version": 15}, "lab_independent": block}}
    assert output._report_lines(report) == ["outcome: -"] + EXPECTED_INDEPENDENT
    assert output.independent_keyed_lines(None) == []
    block["evidence"] = "unavailable"
    assert output.independent_keyed_lines(block)[1] == ("comparisons", "comparisons unavailable (unavailable)")


def test_write_value_status_text_unchanged():
    value = status()
    stream = io.StringIO()
    output.write_value(stream, value, machine=False)
    assert stream.getvalue() == EXPECTED_MASTER_TEXT
    assert (output.refusal_lines(value) + output.suspension_lines(value) + output.warning_lines(value)
            == EXPECTED_MASTER_TEXT.splitlines()[1:])


def test_status_tail_helpers_are_empty_without_tail_evidence():
    assert output.refusal_lines({"refusal": None}) == []
    assert output.suspension_lines({"result": None}) == []
    assert output.warning_lines({"warnings": "unavailable"}) == []


def test_gate_only_attempt_has_its_own_keys_and_reference_id():
    from test_owner_independent_view import BLOCK
    block = independent_view(BLOCK)
    pairs = dict(output.independent_keyed_lines(block))
    key = "ind:1:r01-a01"
    assert pairs[key] == "r01-a01 in_progress finalist/passed"
    assert pairs[key + ":gate"] == ("  gate passed/passed (focus) · vs round champion · stored measurement"
                                    " · ref m-reference")
    assert key + ":gate:scope:focus" in pairs
    assert key + ":global" not in pairs and key + ":comparison" not in pairs


def test_compatibility_comparison_never_creates_a_cli_layer():
    from test_owner_independent_view import BLOCK
    block = independent_view(BLOCK)
    block["attempts"][0].update(gate_comparison=None, global_comparison=None)
    pairs = dict(output.independent_keyed_lines(block))
    assert pairs["ind:1:r01-a01"].endswith(" · no comparison")
    assert not any(key.endswith((":gate", ":global", ":comparison")) for key in pairs)


def test_both_layers_have_distinct_keys_scopes_and_short_reference_ids():
    from test_owner_independent_view import ATTEMPT, COMPARISON, GLOBAL
    from evolution_lab.owner.independent_view import _attempt
    global_ = dict(COMPARISON, deciding_layer="global", scopes=[GLOBAL], reference="fresh",
                   reference_measurement_id="measurement-global-123", outcome="comparison_not_passed",
                   reason="critical_failed")
    attempt = _attempt(dict(ATTEMPT, global_comparison=global_))
    pairs = dict(output.independent_keyed_lines({"attempts": [attempt]}))
    key = "ind:1:r01-a01"
    assert pairs[key] == "r01-a01 agent_worse finalist/passed"
    assert pairs[key + ":gate"].endswith("stored measurement · ref m-reference")
    assert pairs[key + ":global"] == ("  global comparison_not_passed/critical_failed (global) · "
                                      "vs round champion · fresh measurement · ref measurement-")
    assert key + ":gate:scope:focus" in pairs and key + ":global:scope:global" in pairs
    assert key + ":comparison" not in pairs
