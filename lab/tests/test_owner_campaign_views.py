"""Campaign view /12 (family 14 content unchanged, family 15 independent), groups and notices (PDR-0025)."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from evolution_lab.cli import responses

from evolution_lab.owner.campaign_views import (ATTEMPT_GROUP, ATTEMPT_STAGE, DECISION_GROUP, GROUPS,
                                                attempt_group, campaign_view, details_unavailable, failure_of,
                                                _measurement_14, final_decisions, lab_notices,
                                                reflection_of)
from evolution_lab.owner.progress import CampaignPlan

from test_owner_core_raw import finished_campaign  # noqa: F401 — module fixture
from test_owner_measurements import service_cli_with_finished_campaign  # noqa: F401 — fixture


def test_unknown_future_reason_is_unknown_group():
    assert attempt_group("abandoned", "some_future_reason") == "unknown"


@pytest.mark.parametrize("reason,group", [
    ("no_evaluable_focus", "not_enough_data"), ("infrastructure_failed", "infrastructure"),
    ("editor_no_change", "no_candidate"), ("seal_refused", "stopped"), ("cancelled", "stopped"),
    ("condition_changed", "stopped"),
    ("gate_passed", "accepted"), ("observe_finalist", "accepted"), ("causal_below_tolerance", "agent_worse"), ("critical_failure", "agent_worse"),
     ("insufficient_repeats", "not_enough_data"),
     ("insufficient_calibration", "not_enough_data"),
    ("no_exam_impact", "no_candidate"),
])
def test_groups(reason, group):
    assert attempt_group("x", reason) == group


def test_open_and_unknown():
    assert attempt_group(None, None) == "in_progress"
    assert attempt_group("rejected", "brand_new_code") == "unknown"
    assert set(GROUPS) >= set(ATTEMPT_GROUP.values()) | {"in_progress", "unknown"}


_CORE = {"campaign_outcome": {"outcome": "completed", "reason": "rounds_exhausted", "continuation": None},
         "rounds": [{"round": 1, "outcome": "promoted", "baseline_measurement_id": "m-r00-champion",
                     "attempts": [{"attempt_id": "r01-a01", "outcome": "rejected",
                                   "reason": "causal_below_tolerance", "look": 3,
                                   "measurement_id": "m-r01-a01"},
                                  {"attempt_id": "r01-a02", "outcome": "abandoned", "reason": "editor_failed",
                                   "look": None, "measurement_id": None}]}],
         "suspensions": [], "caveats": [],
         "roles": {name: {"kind": "subprocess"} for name in ("editor", "reflector", "grouper")},
         "settings": {"stability": "stable", "round_stop": "first_accepted", "baseline_refresh": None},
         "connection": {"state": "pending", "outcome": None, "attempts": 0, "binding_digest": None,
                        "operation_id": None, "detail": None}}
_CORE["rounds"][0]["final"] = []
for _attempt in _CORE["rounds"][0]["attempts"]:
    _attempt["confidence"] = None
_GROUP = {"root_cause": "The agent skips the refund branch.", "suggested_fix": "Route refunds first.",
          "evidence_ids": ["cohort:c1"], "affected_cases": ["case-7"], "target_paths": ["prompts/main.md"],
          "probe_refs": [], "counterevidence": "c", "regression_risks": "r", "why_this_layer": "w",
          "document_ids": ["d1"]}
_DETAILS = {"journal": "present", "rounds": {1: {
    "baseline_version": "v0",
    "diagnosis": {"reflection_id": "r01-x01", "status": "reflected", "groups": [_GROUP],
                  "error_class": None, "reason": None},
    "last_reflection": {"reflection_id": "r01-x01", "status": "reflected", "groups": [_GROUP],
                        "error_class": None, "reason": None},
    "attempts": {"r01-a01": {"editor_error": None, "editor_reason": None, "code": None},
                 "r01-a02": {"editor_error": "reported", "editor_reason": "turns_exhausted", "code": None}}}}}
_PLAN = CampaignPlan("c.json", 5, 4, "score")
_CHAMPIONS = [{"version": "v0", "digest": "d", "origin": "initial", "attempt_id": None,
              "measurement_id": "m-r00-champion"}]


def test_campaign_view_keeps_legacy_measurement_keys_and_standalone_rows_keep_notices():
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import CORE

    rows = measurement_rows(CORE, "campaign", {}, "score")
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset(), rows, _DETAILS)
    assert view["measurements"]
    assert all("lab_notices" not in row and "lab_notices_state" not in row
               for row in view["measurements"])
    assert all(row["lab_notices"] == [] and row["lab_notices_state"] == "present" for row in rows)
    responses.validate(view, "lab-campaign-view/15")
    responses.validate({"schema": "lab-measurement-list/4", "operation_id": "op1", "measurements": rows},
                       "lab-measurement-list/4")


def test_campaign_decoder_accepts_legacy_rows_and_rejects_notice_fields():
    from panel_fixtures import load_snapshot

    view = deepcopy(next(route["body"] for route in load_snapshot("family14")["routes"].values()
                         if route.get("body", {}).get("schema") == "lab-campaign-view/15"))
    for row in view["measurements"]:
        row.pop("lab_notices", None)
        row.pop("lab_notices_state", None)
    assert view["measurements"]
    responses.validate(view, "lab-campaign-view/15")
    for key, value in (("lab_notices", []), ("lab_notices_state", "present")):
        bad = deepcopy(view)
        bad["measurements"][0][key] = value
        with pytest.raises(ValueError):
            responses.validate(bad, "lab-campaign-view/15")


@pytest.mark.parametrize("schema", ["lab-measurement/4", "lab-measurement-list/4"])
@pytest.mark.parametrize("missing", ["lab_notices", "lab_notices_state"])
def test_standalone_measurement_decoder_requires_notice_fields(schema, missing):
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import CORE

    detail = schema == "lab-measurement/4"
    rows = measurement_rows(CORE, "campaign", {}, "score", detail="m-r01-a01" if detail else None)
    del rows[0][missing]
    body = {"schema": schema, "operation_id": "op1",
            **({"measurement": rows[0]} if detail else {"measurements": rows})}
    with pytest.raises(ValueError):
        responses.validate(body, schema)


def test_view_shape():
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], _DETAILS)
    assert view["schema"] == "lab-campaign-view/15"
    assert set(view) == {"schema", "operation_id", "policy", "progress", "outcome", "champions",
                         "champions_state", "rounds", "measurements", "suspensions",
                         "details_state", "details_reason", "details_source", "family", "gate_mode", "caveats",
                         "lab_notices", "lab_notices_state", "roles", "settings", "connection",
                         "reflector_probe", "reflector_probe_state", "reflector_probe_reason"}
    assert (view["family"], view["gate_mode"], view["caveats"]) == (14, "statistical", [])
    assert view["lab_notices"] == []  # _CORE has a rejected measured candidate
    assert (view["details_state"], view["details_reason"], view["details_source"]) == ("present", None, "core_raw")
    row = view["rounds"][0]
    assert row["baseline_version"] == "v0"
    assert row["reflection"] == {"state": "present", "reflection_id": "r01-x01", "error_class": None,
                                 "reason": None, "hypotheses": [{
                                     "index": 1, "root_cause": "The agent skips the refund branch.",
                                     "suggested_fix": "Route refunds first.", "affected_cases": ["case-7"],
                                     "target_paths": ["prompts/main.md"], "counterevidence": "c",
                                     "regression_risks": "r", "why_this_layer": "w"}]}
    assert "cohort" not in row and row["final"] == []
    measured, failed = row["attempts"]
    assert measured["confidence"] is None and failed["confidence"] is None
    assert measured["failure"] is None and measured["group"] == "agent_worse"
    assert failed["failure"] == {"stage": "editor", "role": "editor", "error_class": "reported",
                                 "role_reason": "turns_exhausted", "code": None}


def test_stage_table_covers_every_closing_reason_of_the_pin():
    """Every reason that reaches ``failure_of`` (outcomes abandoned / insufficient_evidence) has a stage; a family-14
    finalist or focus_below_margin is grouped by core's own fields and never reaches it."""
    from evolution_lab.owner import core_raw
    reasons = {r for outcome, rs in core_raw.attempt_outcome_table(14).items()
               if outcome in {"abandoned", "insufficient_evidence"} for r in rs}
    assert reasons <= set(ATTEMPT_STAGE)


@pytest.mark.parametrize("reason,stage,role", [
    ("editor_failed", "editor", "editor"), ("editor_unresponsive", "editor", "editor"),
    ("editor_declined", "editor", "editor"), ("editor_no_change", "editor", "editor"),
    ("seal_refused", "seal", None), ("incomplete_sample", "measurement", None),
    ("scorer_metric_missing", "measurement", None), ("infrastructure_failed", "measurement", None),
    ("no_evaluable_pairs", "measurement", None), ("no_evaluable_focus", "measurement", None),
    ("no_exam_impact", "measurement", None), ("insufficient_calibration", "measurement", None),
    ("insufficient_repeats", "measurement", None),
    ("interrupted", "lifecycle", None), ("cancelled", "lifecycle", None),
    ("condition_changed", "lifecycle", None), ("brand_new_code", "unknown", None),
])
def test_failure_stage(reason, stage, role):
    failure = failure_of("abandoned", reason, {"editor_error": None, "editor_reason": None, "code": None})
    assert (failure["stage"], failure["role"]) == (stage, role)


def test_failure_only_for_closed_abandoned_or_insufficient():
    raw = {"editor_error": "timeout", "editor_reason": None, "code": None}
    assert failure_of("finalist", "gate_passed", raw) is None
    assert failure_of("rejected", "causal_below_tolerance", raw) is None
    assert failure_of(None, None, raw) is None
    assert failure_of("insufficient_evidence", "incomplete_sample", raw)["stage"] == "measurement"


def test_declined_after_an_earlier_editor_failure_carries_no_error_class():
    """The guard ensures a decline never carries an Editor error class, even if a future core kept an old
    ``editor_error`` on a resumed attempt (a21's reducer clears it); a decline is not that failure (Review Focus)."""
    raw = {"editor_error": "reported", "editor_reason": "program_failed", "code": None}
    assert failure_of("abandoned", "editor_declined", raw) == {
        "stage": "editor", "role": "editor", "error_class": None, "role_reason": None, "code": None}


def test_seal_refusal_keeps_its_code_only():
    raw = {"editor_error": "reported", "editor_reason": None, "code": "oracle_leak"}
    assert failure_of("abandoned", "seal_refused", raw) == {
        "stage": "seal", "role": None, "error_class": None, "role_reason": None, "code": "oracle_leak"}


def test_missing_raw_attempt_gives_null_fields():
    assert failure_of("abandoned", "editor_failed", None) == {
        "stage": "editor", "role": "editor", "error_class": None, "role_reason": None, "code": None}


def _entry(status, error_class=None, reason=None, groups=()):
    return {"reflection_id": "r02-x01", "status": status, "groups": list(groups),
            "error_class": error_class, "reason": reason}


@pytest.mark.parametrize("raw,expected", [
    (None, {"state": "absent", "reflection_id": None, "error_class": None, "reason": None, "hypotheses": []}),
    ({"diagnosis": None, "last_reflection": None},
     {"state": "absent", "reflection_id": None, "error_class": None, "reason": None, "hypotheses": []}),
    ({"diagnosis": _entry("declined"), "last_reflection": _entry("declined")},
     {"state": "declined", "reflection_id": "r02-x01", "error_class": None, "reason": None, "hypotheses": []}),
    ({"diagnosis": _entry("failed", "reported", "invalid_result"), "last_reflection": _entry("failed", "reported", "invalid_result")},
     {"state": "failed", "reflection_id": "r02-x01", "error_class": "reported", "reason": "invalid_result", "hypotheses": []}),
    ({"diagnosis": None, "last_reflection": _entry("started")},
     {"state": "in_progress", "reflection_id": "r02-x01", "error_class": None, "reason": None, "hypotheses": []}),
    ({"diagnosis": None, "last_reflection": _entry("failed", "cancelled")},
     {"state": "unsettled", "reflection_id": "r02-x01", "error_class": "cancelled", "reason": None, "hypotheses": []}),
    ({"diagnosis": None, "last_reflection": _entry("failed", "reported", "provider_exhausted")},
     {"state": "unsettled", "reflection_id": "r02-x01", "error_class": "reported", "reason": "provider_exhausted", "hypotheses": []}),
    ({"diagnosis": _entry("reflected"), "last_reflection": _entry("reflected")},
     {"state": "present", "reflection_id": "r02-x01", "error_class": None, "reason": None, "hypotheses": []}),
])
def test_reflection_states(raw, expected):
    assert reflection_of(raw) == expected


def test_b5_reflection_input_too_large_keeps_journal_codes(tmp_path, monkeypatch):
    """Core's internal unknown class must not replace journal codes in existing readers."""
    from evolution.tree.roles.role_failures import reflection_class
    from evolution_lab._suspension import suspension_hint
    from evolution_lab.cli.output import _campaign_lines
    from evolution_lab.owner import core_raw
    from evolution_lab.report import _lab_fields

    assert reflection_class("input_too_large", "input_too_large") == "unknown"
    entry = _entry("failed", "input_too_large", "input_too_large")
    details = {"journal": "present", "rounds": {1: {
        "baseline_version": "v0", "diagnosis": entry,
        "last_reflection": entry, "attempts": {}}}}
    suspension = {"seq": 9, "reason": "reflector_failing", "owner": "reflector",
                  "evidence_kind": "none"}
    core = {**_CORE, "campaign_outcome": None, "rounds": [_round(1)],
            "suspensions": [suspension]}
    view = campaign_view("op1", core, _PLAN, None, [], frozenset(), [], details)
    reflection = view["rounds"][0]["reflection"]
    assert (reflection["state"], reflection["error_class"], reflection["reason"]) == (
        "failed", "input_too_large", "input_too_large")
    assert view["suspensions"] == [suspension]
    assert "new Bundle and a new campaign" in suspension_hint("reflector_failing", "en")
    responses.validate(view, "lab-campaign-view/15")
    assert any("reflection=failed(input_too_large: input_too_large)" in line
               for line in _campaign_lines(view))

    monkeypatch.setattr(core_raw, "campaign_details", lambda root: details)
    report = _lab_fields(tmp_path, core)
    assert report["lab_rounds"] == [{"round": 1, "source": "core_raw", "reflection": {
        "state": "failed", "error_class": "input_too_large", "reason": "input_too_large"}}]
    assert view["outcome"] is None and view["lab_notices"] == report["lab_notices"] == []


def test_hypotheses_keep_journal_order_and_optional_sentences():
    plain = {k: v for k, v in _GROUP.items() if k not in {"counterevidence", "regression_risks", "why_this_layer"}}
    second = dict(plain, root_cause="Second.")
    got = reflection_of({"diagnosis": _entry("reflected", groups=[_GROUP, second]),
                         "last_reflection": _entry("reflected", groups=[_GROUP, second])})["hypotheses"]
    assert [h["index"] for h in got] == [1, 2] and got[1]["root_cause"] == "Second."
    assert got[1]["counterevidence"] is None and got[1]["why_this_layer"] is None
    assert not {"evidence_ids", "probe_refs", "document_ids", "mass"} & set(got[0])


def test_unavailable_details_null_only_the_new_fields():
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [],
                         details_unavailable("core_raw_unsupported"))
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "core_raw_unsupported")
    row = view["rounds"][0]
    assert row["baseline_version"] is None and row["reflection"] is None
    assert all(a["failure"] is None for a in row["attempts"])
    assert row["attempts"][0]["group"] == "agent_worse" and view["champions_state"] == "present"


def _unreadable_fallback(details):
    """What `campaign_view` must answer when the details it was given cannot be turned into the form."""
    expected = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [],
                             details_unavailable("unreadable"))
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], details)
    assert view == expected
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    row = view["rounds"][0]
    assert row["baseline_version"] is None and row["reflection"] is None
    assert all(a["failure"] is None for a in row["attempts"])
    # The rest of the form stays intact (ADR-0019 p.3: a failed read is a state, not a route error).
    assert row["attempts"][0]["group"] == "agent_worse" and view["champions_state"] == "present"
    assert view["champions"][0]["measurement_id"] == "m-r00-champion"


def test_diagnosis_status_unknown_to_lab_is_unreadable_not_a_route_error():
    partial = dict(_DETAILS["rounds"][1]["diagnosis"], status="partial")
    round_1 = dict(_DETAILS["rounds"][1], diagnosis=partial)
    _unreadable_fallback({"journal": "present", "rounds": {1: round_1}})


def test_reflected_group_without_root_cause_is_unreadable_not_a_route_error():
    group = {k: v for k, v in _GROUP.items() if k != "root_cause"}
    diagnosis = dict(_DETAILS["rounds"][1]["diagnosis"], groups=[group])
    round_1 = dict(_DETAILS["rounds"][1], diagnosis=diagnosis)
    _unreadable_fallback({"journal": "present", "rounds": {1: round_1}})


def test_reflection_state_map_equals_core_terminal_statuses_of_the_pin():
    """A new terminal reflection status in core must reach `REFLECTION_STATE` on a re-pin."""
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.campaign_views import REFLECTION_STATE
    assert set(REFLECTION_STATE) == core_raw.reflection_terminal_statuses()


def test_missing_journal_before_start_is_present_and_empty():
    view = campaign_view("op1", {**_CORE, "rounds": [], "suspensions": []}, _PLAN, None, [], frozenset(), [],
                         {"journal": "missing", "rounds": {}})
    assert (view["details_state"], view["details_reason"], view["rounds"]) == ("present", None, [])


def test_missing_journal_with_known_rounds_is_unreadable():
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset(), [],
                         {"journal": "missing", "rounds": {}})
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert view["rounds"][0]["reflection"] is None


def test_round_unknown_to_the_journal_is_absent():
    details = {"journal": "present", "rounds": {}}
    row = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset(), [], details)["rounds"][0]
    assert row["baseline_version"] is None and row["reflection"]["state"] == "absent"
    assert row["attempts"][1]["failure"]["error_class"] is None


def test_campaign_text_lines():
    from evolution_lab.cli.output import _campaign_lines
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], _DETAILS)
    assert _campaign_lines(view) == [
        "outcome: completed/rounds_exhausted",
        "reflector probe: unavailable (unreadable)",
        "gate: statistical",
        "settings: stability=stable round_stop=first_accepted refresh=None",
        "connection: pending - attempts=0",
        "Baseline provenance notices are unavailable.",
        "round 1 promoted baseline=v0 hyp=1",
        "  r01-a01 agent_worse causal_below_tolerance",
        "  r01-a02 stopped editor_failed editor:turns_exhausted",
    ]
    down = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset(), [], details_unavailable("unreadable"))
    lines = _campaign_lines(down)
    assert "details unavailable: unreadable" in lines
    assert "round 1 promoted baseline=- reflection=-" in lines


_OUTCOME = {"outcome": "completed", "reason": "rounds_exhausted"}


def _round(n, reason=None, attempts=()):
    return {"round": n, "outcome": "unplanned" if not attempts else "clean", "reason": reason, "final": [],
            "attempts": [{"attempt_id": f"r0{n}-a0{i}", "outcome": o, "reason": r}
                         for i, (o, r) in enumerate(attempts, 1)]}


def _failed(error_class, reason):
    return {"state": "failed", "reflection_id": "x", "error_class": error_class, "reason": reason, "hypotheses": []}


def test_notice_when_no_round_had_a_measured_candidate():
    rounds = [_round(1, "reflection_failed"), _round(2, "reflection_failed"),
              _round(3, None, [("abandoned", "editor_failed")])]
    reflections = {1: _failed("oracle_leak", "oracle_leak"), 2: _failed("reported", "invalid_result"),
                   3: {"state": "present", "reflection_id": "y", "error_class": None, "reason": None,
                       "hypotheses": []}}
    assert lab_notices(_OUTCOME, rounds, reflections) == [{
        "code": "no_candidate_measured", "rounds": 3,
        "by_reason": {"editor_failed": 1, "reflection_failed": 2},
        "reflection_errors": {"oracle_leak": 1, "reported": 1}}]


def test_notice_without_details_has_no_reflection_errors():
    notice = lab_notices(_OUTCOME, [_round(1, "reflection_failed")], None)
    assert notice == [{"code": "no_candidate_measured", "rounds": 1, "by_reason": {"reflection_failed": 1}}]


def test_notice_counts_infrastructure_with_a_measurement_id_as_not_measured():
    rounds = [_round(1, None, [("insufficient_evidence", "incomplete_sample")])]
    rounds[0]["attempts"][0]["measurement_id"] = "m-r01-a01"
    assert lab_notices(_OUTCOME, rounds, {})[0]["by_reason"] == {"incomplete_sample": 1}


def test_no_notice_when_a_candidate_was_measured():
    for outcome, reason in (("finalist", "gate_passed"), ("rejected", "causal_below_tolerance"),
                            ("insufficient_evidence", "insufficient_repeats")):
        rounds = [_round(1, None, [(outcome, reason)])]
        decisions = {"r01-a01": {"state": "accepted", "reason": "global_off"}} if outcome == "finalist" else None
        assert lab_notices(_OUTCOME, rounds, {}, decisions=decisions) == []


def test_notice_suppressed_by_unknown_attempt():
    assert lab_notices(_OUTCOME, [_round(1, None, [("abandoned", "a_new_core_reason")])], {}) == []


def test_no_notice_without_core_outcome_or_while_in_progress():
    assert lab_notices(None, [_round(1, "reflection_failed")], {}) == []
    assert lab_notices(_OUTCOME, [_round(1, None, [(None, None)])], {}) == []


def test_view_carries_the_notice():
    core = {**_CORE, "rounds": [_round(1, "reflection_failed")]}
    details = {"journal": "present", "rounds": {1: {"baseline_version": "v0", "diagnosis": _entry(
        "failed", "oracle_leak", "oracle_leak"), "last_reflection": None, "attempts": {}}}}
    view = campaign_view("op1", core, _PLAN, None, _CHAMPIONS, frozenset(), [], details)
    assert view["lab_notices"][0]["reflection_errors"] == {"oracle_leak": 1}
    responses.validate(view, "lab-campaign-view/15")


def test_view_notice_without_details_has_no_reflection_errors():
    core = {**_CORE, "rounds": [_round(1, "reflection_failed")]}
    view = campaign_view("op1", core, _PLAN, None, _CHAMPIONS, frozenset(), [], details_unavailable("unreadable"))
    assert view["lab_notices"] == [{"code": "no_candidate_measured", "rounds": 1,
                                    "by_reason": {"reflection_failed": 1}}]
    responses.validate(view, "lab-campaign-view/15")


def test_view_report_and_markdown_give_one_notice(tmp_path, monkeypatch):
    """Spec dev46 §4 tests, final review I2: one core result and one set of round details give the same notice in the
    view `/7`, the report body and `report.md`."""
    from evolution_lab.owner import core_raw
    from evolution_lab.report import Report, _lab_fields, _notice_md, build_report
    from test_report import campaign
    data = build_report(campaign(), comparison_scope="in-sample").data
    first = data["core"]["rounds"][0]
    data["core"]["rounds"] = [
        {**first, "round": 1, "outcome": "unplanned", "reason": "reflection_failed", "attempts": []},
        {**first, "round": 2, "attempts": [{**first["attempts"][0], "attempt_id": "r02-a01",
                                            "outcome": "abandoned", "reason": "editor_failed"}]}]
    details = {"journal": "present", "rounds": {
        1: {"baseline_version": "v0", "diagnosis": _entry("failed", "oracle_leak", "oracle_leak"),
            "last_reflection": None, "attempts": {}},
        2: {"baseline_version": "v0", "diagnosis": _entry("reflected", groups=[_GROUP]), "last_reflection": None,
            "attempts": {"r02-a01": {"editor_error": "reported", "editor_reason": "turns_exhausted", "code": None}}}}}
    monkeypatch.setattr(core_raw, "campaign_details", lambda root: details)
    data.update(_lab_fields(tmp_path, data["core"]))
    view = campaign_view("op1", data["core"], _PLAN, None, _CHAMPIONS, frozenset(), [], details)
    expected = [{"code": "no_candidate_measured", "rounds": 2,
                 "by_reason": {"editor_failed": 1, "reflection_failed": 1}, "reflection_errors": {"oracle_leak": 1}}]
    assert view["lab_notices"] == data["lab_notices"] == expected
    text = Report(json.dumps(data, ensure_ascii=False, sort_keys=True)).markdown()
    assert _notice_md(expected[0]) in text
    assert "| 1 | unplanned | reflection_failed | oracle_leak |" in text


@pytest.mark.parametrize("label,mutate", [
    ("caveats not a list", lambda core: core.__setitem__("caveats", 5)),
    ("final not an entry list", lambda core: core["rounds"][0].__setitem__("final", ["x"])),
])
def test_view_notice_keeps_reflection_errors_when_only_family_14_blocks_are_unreadable(label, mutate):
    """Final review I2: the notice depends only on the round details, like the report's; an unreadable family-14
    final or caveats block (details_state unavailable) does not drop `reflection_errors` read from the details."""
    core = _family14("observe-status.json")
    for attempt in core["rounds"][0]["attempts"]:
        attempt.update(outcome="abandoned", reason="editor_failed")
    mutate(core)
    details = {"journal": "present", "rounds": {
        1: _ROUND_1, 2: {"baseline_version": "v1", "diagnosis": _entry("failed", "oracle_leak", "oracle_leak"),
                         "last_reflection": None, "attempts": {}}}}
    view = campaign_view("op14", core, _PLAN, None, _CHAMPIONS_14, frozenset({"r01-a01"}), [], details)
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable"), label
    assert view["lab_notices"] == [{"code": "no_candidate_measured", "rounds": 2,
                                    "by_reason": {"clean": 1, "editor_failed": 1},
                                    "reflection_errors": {"oracle_leak": 1}}], label
    responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("notices", [
    None, {}, [{"code": "something_else", "rounds": 1, "by_reason": {}}],
    [{"code": "no_candidate_measured", "rounds": 1}],
    [{"code": "no_candidate_measured", "rounds": 1, "by_reason": {}, "text": "no candidate"}],
])
def test_closed_decoder_refuses_a_bad_notice(notices):
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], _DETAILS)
    with pytest.raises(ValueError):
        responses.validate(dict(view, lab_notices=notices), "lab-campaign-view/15")


def test_closed_decoder_refuses_a_view_without_notices():
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], _DETAILS)
    del view["lab_notices"]
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")


def test_campaign_text_names_the_reflection_error_and_the_notice():
    from evolution_lab.cli.output import _campaign_lines
    view = {"schema": "lab-campaign-view/15", "family": 14, "gate_mode": "statistical", "caveats": [],
            "details_state": "present", "lab_notices": [{"code": "no_candidate_measured", "rounds": 2,
            "by_reason": {"reflection_failed": 2}, "reflection_errors": {"oracle_leak": 1, "reported": 1}}],
            "rounds": [{"round": 1, "outcome": "unplanned", "baseline_version": "v0", "attempts": [],
                        "reflection": _failed("oracle_leak", "oracle_leak")}]}
    lines = _campaign_lines(view)
    assert "round 1 unplanned baseline=v0 reflection=failed(oracle_leak: oracle_leak)" in lines
    assert ("no candidate was measured: reflection_failed 2; reflection errors: oracle_leak 1, reported 1"
            in lines)


def test_owner_campaign_route_matches_cli(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    view = cli("campaign", operation)[-1]
    assert view["schema"] == "lab-campaign-view/15" and view == route(f"/v1/operations/{operation}/campaign")
    assert (view["family"], view["gate_mode"]) == (14, "observe")
    assert {"kind": "observe_gate_not_enforced", "round": None, "attempt_id": None} in view["caveats"]
    assert all("cohort" not in r and isinstance(r["final"], list) for r in view["rounds"])
    assert view["champions"][0]["origin"] == "initial"
    attempts = [a for r in view["rounds"] for a in r["attempts"]]
    assert attempts and any(a["confidence"] is not None for a in attempts)
    assert all("alpha_total" not in a for a in attempts)
    assert all(a["group"] in GROUPS for a in attempts)
    assert view["details_state"] == "present" and view["details_source"] == "core_raw"
    assert view["rounds"][0]["baseline_version"] == "v0"
    assert all(r["reflection"]["state"] in {"present", "declined", "failed", "unsettled", "in_progress", "absent"}
               for r in view["rounds"])


def test_other_core_gives_unavailable_details(service_cli_with_finished_campaign, monkeypatch):
    from evolution_lab.owner import core_raw
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    cli, route, operation = service_cli_with_finished_campaign
    view = cli("campaign", operation)[-1]
    assert view == route(f"/v1/operations/{operation}/campaign")
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "core_raw_unsupported")
    assert view["rounds"] and all(r["reflection"] is None for r in view["rounds"])


def test_unread_lineage_is_null_next_to_its_state(service_cli_with_finished_campaign, monkeypatch):
    """A failed lineage read is not "nothing promoted": champions and every has_tree are null."""
    from evolution_lab.owner import campaign_views

    def broken(run):
        raise OSError("lineage unreadable")

    monkeypatch.setattr(campaign_views, "lineage_champions", broken)
    cli, route, operation = service_cli_with_finished_campaign
    view = cli("campaign", operation)[-1]  # the CLI validates the closed form
    assert view == route(f"/v1/operations/{operation}/campaign")
    assert view["champions_state"] == "unavailable" and view["champions"] is None
    attempts = [a for r in view["rounds"] for a in r["attempts"]]
    assert attempts and all(a["has_tree"] is None for a in attempts)


def test_campaign_refuses_an_evaluation(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from test_owner_evaluation import _finished, _prepared, _ready_target

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target,
                                         secrets={"SCORER_TOKEN": "private-neutral-scorer"})
            ref = ResourceRef("operation", accepted.id)
            assert (await _finished(owner, ref)).kind == "evaluation"
            with pytest.raises(OwnerError) as caught:
                await owner.campaign(ref)
            assert caught.value.code == "invalid_argument"

    asyncio.run(scenario())


def test_validator_ties_has_tree_to_champions_state():
    from evolution_lab.cli.responses import _campaign_view
    core = {**_CORE, "rounds": [{"round": 1, "outcome": "promoted", "baseline_measurement_id": None, "final": [],
                        "attempts": [{"attempt_id": "r01-a01", "outcome": None, "confidence": None}]}]}
    plan = CampaignPlan("c.json", 1, 1, None)
    details = {"journal": "present", "rounds": {}}
    unread = campaign_view("op1", core, plan, None, None, None, [], details)
    assert _campaign_view(unread)["rounds"][0]["attempts"][0]["has_tree"] is None
    read = campaign_view("op1", core, plan, None, [], frozenset(), [], details)
    assert _campaign_view(read)["champions"] == []
    for bad in (dict(unread, champions=[]), dict(read, champions=None),
                dict(read, champions_state="missing")):
        with pytest.raises(ValueError):
            _campaign_view(bad)
    unread["rounds"][0]["attempts"][0]["has_tree"] = False
    with pytest.raises(ValueError):
        _campaign_view(unread)


def test_validator_ties_details_to_their_state():
    from evolution_lab.cli.responses import _campaign_view
    good = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [], _DETAILS)
    assert _campaign_view(good) is good
    down = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), [],
                         details_unavailable("unreadable"))
    assert _campaign_view(down) is down
    import copy
    bad = [dict(good, details_reason="unreadable"), dict(down, details_reason=None),
           dict(good, details_source="core_public"), dict(good, details_state="partial")]
    broken = copy.deepcopy(good); broken["rounds"][0]["reflection"]["state"] = "maybe"; bad.append(broken)
    broken = copy.deepcopy(good); broken["rounds"][0]["reflection"]["hypotheses"][0]["mass"] = 3; bad.append(broken)
    broken = copy.deepcopy(good); broken["rounds"][0]["attempts"][1]["failure"]["stage"] = "x"; bad.append(broken)
    broken = copy.deepcopy(down); broken["rounds"][0]["reflection"] = reflection_of(None); bad.append(broken)
    broken = copy.deepcopy(down)
    broken["rounds"][0]["attempts"][1]["failure"] = good["rounds"][0]["attempts"][1]["failure"]; bad.append(broken)
    broken = copy.deepcopy(down); broken["rounds"][0]["baseline_version"] = "v0"; bad.append(broken)
    for value in bad:
        with pytest.raises(ValueError):
            _campaign_view(value)


def test_campaign_view_4_validates_rows_with_repairs_when_details_are_unavailable():
    from evolution_lab.cli import responses
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import BASE, CAND, CORE
    rows = measurement_rows(dict(CORE, measurements=[dict(BASE, repair_tries=1, repairs_scored=1), CAND]),
                            "campaign", {}, "score")
    assert rows[0]["repairs"] == {"tries": 1, "pairs_scored": 1}
    view = campaign_view("op1", _CORE, _PLAN, None, _CHAMPIONS, frozenset({"r01-a01"}), rows,
                         details_unavailable("core_raw_unsupported"))
    assert view["schema"] == "lab-campaign-view/15"
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "core_raw_unsupported")
    responses.validate(view, "lab-campaign-view/15")


_CHAMPIONS_14 = [
    {"version": "v0", "digest": "d0", "origin": "initial", "attempt_id": None},
    {"version": "v1", "digest": "d1", "origin": "promoted_attempt", "attempt_id": "r01-a01"}]
_ROUND_1 = {"baseline_version": "v0", "diagnosis": None, "last_reflection": None,
            "attempts": {"r01-a01": {"editor_error": None, "editor_reason": None, "code": None}}}
_DETAILS_14 = {"journal": "present", "rounds": {
    1: _ROUND_1, 2: {"baseline_version": "v1", "diagnosis": None, "last_reflection": None, "attempts": {}}}}


def test_view_7_on_the_observe_smoke_run():
    core = _family14("observe-result.json")
    view = campaign_view("op14", core, _PLAN, None, _CHAMPIONS_14, frozenset({"r01-a01"}), [], _DETAILS_14)
    assert (view["family"], view["gate_mode"], view["caveats"]) == (14, "observe", core["caveats"])
    first, second = view["rounds"]
    attempt, = first["attempts"]
    assert (attempt["outcome"], attempt["reason"], attempt["group"], attempt["look"]) == (
        "finalist", "observe_finalist", "accepted", 1)
    assert "alpha_total" not in attempt and "cohort" not in first
    assert attempt["confidence"]["margin"] == 0 and attempt["confidence"]["latest"]["focus"]["exact"] is None
    assert first["final"][-1]["selection"]["attempt_id"] == "r01-a01"
    assert (second["outcome"], second["final"], second["attempts"]) == ("clean", [], [])
    assert [c["measurement_id"] for c in view["champions"]] == ["m-r00-champion", "m-r01-final-01-a01"]
    responses.validate(view, "lab-campaign-view/15")


def test_view_7_on_a_final_left_unmeasured():
    core = _family14("observe-status.json")
    core.update(complete=False, suspended=True, campaign_outcome=None,
                suspensions=[{"seq": 39, "reason": "final_incomplete", "owner": "scorer", "evidence_kind": "final_rows"}])
    core["rounds"] = core["rounds"][:1]
    core["rounds"][0]["outcome"] = None
    entry = core["rounds"][0]["final"][-1]
    entry.update(selection=None, next_baseline_measurement_id=None, listed=[{}, {}])
    entry["members"][0].update(complete=False, missing=2)
    entry["contrasts"][0].update(accepted=None, reason="final_incomplete", unavailable="final_incomplete")
    view = _view(core)
    assert view["rounds"][0]["final"][-1]["listed"] == 2
    attempt = view["rounds"][0]["attempts"][0]
    assert (attempt["final_decision"]["state"], attempt["group"]) == ("unavailable", "not_enough_data")
    assert view["suspensions"][-1]["reason"] == "final_incomplete"
    responses.validate(view, "lab-campaign-view/15")


_MALFORMED_BLOCKS = [
    ("final not an entry list", lambda core: core["rounds"][0].__setitem__("final", ["x"])),
    ("selection not an object", lambda core: core["rounds"][0]["final"][0].__setitem__("selection", ["x"])),
    ("round without its number", lambda core: core["rounds"][1].__delitem__("round")),
    ("final without contrasts", lambda core: core["rounds"][0]["final"][0].__delitem__("contrasts")),
    ("confidence without margin", lambda core: core["rounds"][0]["attempts"][0]["confidence"].__delitem__("margin")),
]


@pytest.mark.parametrize("fixture", ["observe-status.json", "observe-result.json"])
@pytest.mark.parametrize("label,mutate", _MALFORMED_BLOCKS)
def test_view_7_malformed_final_keeps_readable_gate_mode_and_caveats(fixture, label, mutate):
    core = _family14(fixture)
    expected = core["caveats"]
    mutate(core)
    view = campaign_view("op14", core, _PLAN, None, _CHAMPIONS_14, frozenset(), [], _DETAILS_14)
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable"), label
    assert (view["family"], view["gate_mode"], view["caveats"]) == (14, "observe", expected)
    assert [c["measurement_id"] for c in view["champions"]] == [None, None]
    assert all(r["final"] is None and all(a["confidence"] is None for a in r["attempts"]) for r in view["rounds"])
    responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("caveats", [5, [{"round": None}], None])
def test_view_7_unreadable_caveats_leave_gate_unknown_and_keep_final(caveats):
    core = _family14("observe-status.json")
    core["caveats"] = caveats
    view = _view(core)
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert (view["family"], view["gate_mode"], view["caveats"]) == (14, None, [])
    assert view["rounds"][0]["final"][-1]["selection"]["attempt_id"] == "r01-a01"
    responses.validate(view, "lab-campaign-view/15")


def test_view_7_missing_caveats_are_unreadable():
    core = _family14("observe-status.json")
    del core["caveats"]
    view = _view(core)
    assert (view["gate_mode"], view["details_reason"]) == (None, "unreadable")
    responses.validate(view, "lab-campaign-view/15")


def test_validator_7_null_gate_mode_only_when_unreadable():
    core = _family14("observe-status.json")
    good = campaign_view("op14", core, _PLAN, None, _CHAMPIONS_14, frozenset(), [], _DETAILS_14)
    with pytest.raises(ValueError):
        responses._campaign_view(dict(good, gate_mode=None))
    down = _view(core)
    assert down["gate_mode"] == "observe"
    with pytest.raises(ValueError):
        responses._campaign_view(dict(down, gate_mode=None))


def test_statistical_family_14_without_observe_caveat():
    view = _view(dict(_family14("observe-status.json"), caveats=[]))
    assert (view["family"], view["gate_mode"], view["caveats"]) == (14, "statistical", [])


def test_validator_7_refuses_open_confidence_and_final_shapes():
    import copy
    good = _view(_family14("observe-status.json"))
    bad = [dict(good, gate_mode="maybe"), dict(good, caveats=[{"kind": "x"}])]
    broken = copy.deepcopy(good); broken["rounds"][0]["attempts"][0]["confidence"]["latest"]["extra"] = 1; bad.append(broken)
    broken = copy.deepcopy(good); broken["rounds"][0]["final"][0]["listed"] = -1; bad.append(broken)
    for value in bad:
        with pytest.raises(ValueError):
            responses._campaign_view(value)


def test_campaign_text_lines_of_family_14_and_incomplete_final():
    from evolution_lab.cli.output import _campaign_lines
    view = campaign_view("op14", _family14("observe-status.json"), _PLAN, None, _CHAMPIONS_14,
                         frozenset({"r01-a01"}), [], _DETAILS_14)
    lines = _campaign_lines(view)
    assert "round 1 promoted baseline=v0 reflection=absent final=selected:r01-a01/finalist_selected" in lines
    assert any("accepted observe_finalist gate=finalist/observe_finalist focus=1 [-, -] exact=- margin=0 decision=accepted/global_off" in line for line in lines)
    entry = view["rounds"][0]["final"][-1]
    entry.update(selection=None, listed=2)
    assert any("final=incomplete listed=2" in line for line in _campaign_lines(view))


def test_campaign_text_lines_of_family_14_with_unknown_gate():
    from evolution_lab.cli.output import _campaign_lines, gate_line
    core = _family14("observe-status.json")
    core["caveats"] = 5
    lines = _campaign_lines(_view(core))
    assert "gate: unknown (unreadable) — not evidence of quality" in lines
    assert "details unavailable: unreadable" in lines
    assert gate_line(14, None, []) == "gate: unknown (unreadable) — not evidence of quality"
    assert gate_line(14, "statistical", []) == "gate: statistical"


@pytest.fixture(scope="module")
def finished_campaign_15(tmp_path_factory):
    """One real /15 observe campaign through Owner.

    Campaign preparation checks target capacity through core (spec §4.1)."""
    from scaffold_15 import scaffold_15
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished
    from test_owner_target import make_bundle, ready_target

    state, bundle, _ = make_bundle(tmp_path_factory.mktemp("campaign15"), scaffold=scaffold_15)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", accepted.id)
            status = await finished(owner, ref, seconds=120)
            return status, await owner.campaign(ref)

    return asyncio.run(scenario())


def test_owner_campaign_view_4_on_a_real_observe_campaign(finished_campaign_15):
    status, view = finished_campaign_15
    assert status.control_state == "finished", status.to_payload()
    assert (view["schema"], view["family"], view["gate_mode"]) == ("lab-campaign-view/15", 14, "observe")
    assert {"kind": "observe_gate_not_enforced", "round": None, "attempt_id": None} in view["caveats"]
    attempts = [a for r in view["rounds"] for a in r["attempts"]]
    assert attempts and all("alpha_total" not in a and a["group"] != "unknown" for a in attempts)
    assert any(a["confidence"] is not None for a in attempts), attempts
    assert all(a["confidence"]["mode"] == "observe" and a["confidence"]["repeats"] == 1
               for a in attempts if a["confidence"] is not None)
    # Family 14 selects Champions from the final; the initial Champion may retain its baseline.
    gate = {a["measurement_id"] for a in attempts}
    final = {m["measurement_id"] for r in view["rounds"] for f in r["final"] for m in f["members"]}
    assert final and all("cohort" not in r for r in view["rounds"]), view["rounds"]
    for champion in view["champions"]:
        allowed = final | ({view["rounds"][0]["baseline_measurement_id"]} if champion["origin"] == "initial" else set())
        assert champion["measurement_id"] in allowed and champion["measurement_id"] not in gate, view["champions"]
    assert any(c["measurement_id"] in final for c in view["champions"]), view["champions"]
    responses.validate(view, "lab-campaign-view/15")


FAMILY14 = Path(__file__).parent / "fixtures" / "family14"


def _family14(name: str) -> dict:
    return json.loads((FAMILY14 / name).read_text(encoding="utf-8"))


def _finalist_core(contrast: dict | None, *, outcome=None, suspended=None, round_outcome=None,
                   round_reason=None) -> dict:
    """A minimal family-14 projection: one round, one finalist r01-a01, its final entry with ``contrast``."""
    final = [] if contrast is None else [{"contrasts": [dict(contrast, attempt_id="r01-a01")]}]
    return {"settings": {}, "campaign_outcome": outcome, "suspended": suspended is not None,
            "suspensions": [] if suspended is None else [{"seq": 9, "reason": suspended, "owner": "target",
                                                          "evidence_kind": "final_rows"}],
            "rounds": [{"round": 1, "outcome": round_outcome, "reason": round_reason, "final": final,
                        "attempts": [{"attempt_id": "r01-a01", "outcome": "finalist", "reason": "observe_finalist"}]}]}


@pytest.mark.parametrize("contrast, outcome, suspended, expected, closed", [
    ({"accepted": True, "reason": "global_off", "unavailable": None}, None, None, ("accepted", "global_off"), False),
    ({"accepted": False, "reason": "global_point_not_improved", "unavailable": None}, None, None,
     ("rejected", "global_point_not_improved"), False),
    ({"accepted": False, "reason": "critical_failure", "unavailable": None}, None, None,
     ("rejected", "critical_failure"), False),
    ({"accepted": False, "reason": "final_incomplete", "unavailable": "final_incomplete"}, None, None,
     ("unavailable", "final_incomplete"), False),
    (None, None, None, ("pending", None), False),
    (None, None, "final_incomplete", ("unavailable", "final_incomplete"), False),
    (None, {"outcome": "finished", "reason": "rounds_exhausted"}, None, ("unavailable", None), False),
    (None, None, None, ("unavailable", "round_closed"), True),
])
def test_final_decision_of_a_finalist_is_cores_contrast(contrast, outcome, suspended, expected, closed):
    """Spec 2026-10-13 §6.3 (tenet one-campaign-authority): a finalist's group comes from core's own final contrast,
    matched by attempt_id; Lab recomputes nothing."""
    core = _finalist_core(contrast, outcome=outcome, suspended=suspended,
                          round_outcome="retained" if closed else None, round_reason="round_closed" if closed else None)
    decision = final_decisions(core)["r01-a01"]
    assert (decision["state"], decision["reason"]) == expected
    group = attempt_group("finalist", "observe_finalist", decision=decision)
    assert group == DECISION_GROUP[expected[0]]


@pytest.mark.parametrize("complete, outcome", [(True, None), (False, {"outcome": "cancelled"})])
def test_family_14_pending_finalist_in_finished_campaign_is_unavailable(complete, outcome):
    core = _finalist_core(None, outcome=outcome, round_reason="cancelled")
    core["complete"] = complete
    assert final_decisions(core)["r01-a01"] == {"state": "unavailable", "reason": "cancelled"}


@pytest.mark.parametrize("complete, outcome", [(True, None), (False, {"outcome": "cancelled"})])
def test_family_14_attempt_without_outcome_in_finished_campaign_is_unknown(complete, outcome):
    core = deepcopy(_CORE)
    core.update(complete=complete, campaign_outcome=outcome)
    core["rounds"][0]["outcome"] = None
    core["rounds"][0]["attempts"][0].update(outcome=None, reason=None)
    view = campaign_view("op1", core, _PLAN, None, _CHAMPIONS, frozenset(), [], _DETAILS)
    assert view["rounds"][0]["attempts"][0]["group"] == "unknown"


def test_family_14_running_campaign_keeps_in_progress_and_pending():
    core = deepcopy(_CORE)
    core.update(complete=False, campaign_outcome=None)
    core["rounds"][0].update(outcome=None, reason=None)
    core["rounds"][0]["attempts"][0].update(outcome="finalist", reason="observe_finalist")
    core["rounds"][0]["attempts"][1].update(outcome=None, reason=None)
    view = campaign_view("op1", core, _PLAN, None, _CHAMPIONS, frozenset(), [], _DETAILS)
    finalist, opened = view["rounds"][0]["attempts"]
    assert finalist["final_decision"] == {"state": "pending", "reason": None}
    assert finalist["group"] == "in_progress"
    assert opened["group"] == "in_progress"


def test_campaign_finished_is_shared():
    from evolution_lab.owner import campaign_views, independent_view

    assert campaign_views.campaign_finished is independent_view.campaign_finished


def test_finalist_without_a_decision_is_unknown():
    """The finalist outcome exists only in family 14: no reason-table fallback to "accepted"."""
    assert attempt_group("finalist", "observe_finalist") == "unknown"
    assert attempt_group("finalist", "gate_passed") == "unknown"


def test_a_finalist_without_a_decision_is_never_accepted():
    """Review Focus 1: the reason table says gate_passed/observe_finalist → accepted; for family 14 that is wrong."""
    assert attempt_group("finalist", "gate_passed", decision=None) == "unknown"
    assert attempt_group("finalist", "observe_finalist",
                         decision={"state": "pending", "reason": None}) == "in_progress"


def _margin_block(final: bool, point, margin=0.0) -> dict:
    return {"margin": margin, "evaluated": [{"final": final, "focus": {"point": point}}]}


@pytest.mark.parametrize("final, point, expected", [
    (False, 0.4, "agent_worse"),      # early look: the upper bound is at or under the margin
    (True, 0.2, "not_enough_data"),   # final look, point above the margin: positive, not confirmed
    (True, 0.0, "agent_worse"),       # final look, point at the margin
    (True, -0.1, "agent_worse"),
])
def test_focus_below_margin_by_look(final, point, expected):
    assert attempt_group("rejected", "focus_below_margin",
                         confidence=_margin_block(final, point)) == expected


@pytest.mark.parametrize("block", [None, {}, {"margin": 0, "evaluated": []},
                                   {"margin": 0, "evaluated": [{"final": "yes", "focus": {"point": 1}}]},
                                   {"margin": None, "evaluated": [{"final": True, "focus": {"point": 1}}]}])
def test_focus_below_margin_without_the_fields_is_unknown(block):
    """Spec §6.3: no look, final flag, point or margin in core's projection — unknown, never a guess."""
    assert attempt_group("rejected", "focus_below_margin", confidence=block) == "unknown"


def test_lab_notices_count_a_family_14_finalist_as_measured():
    core = _finalist_core({"accepted": False, "reason": "global_point_not_improved", "unavailable": None},
                          outcome={"outcome": "finished", "reason": "rounds_exhausted"})
    assert lab_notices(core["campaign_outcome"], core["rounds"], None,
                       decisions=final_decisions(core)) == []


def test_real_family_14_observe_run_groups():
    """The Task 0 smoke: every finalist's group is its contrast's; nothing is unknown."""
    core = _family14("observe-status.json")
    decisions = final_decisions(core)
    assert decisions and all(d["state"] in DECISION_GROUP for d in decisions.values())
    for row in core["rounds"]:
        for a in row["attempts"]:
            group = attempt_group(a["outcome"], a["reason"], decision=decisions.get(a["attempt_id"]),
                                  confidence=a.get("confidence"))
            assert group != "unknown", a


def _view(core):
    """The view of a core projection without lineage or round details (they are not what these tests check)."""
    return campaign_view("op14", core, _PLAN, None, None, None, [], details_unavailable("core_raw_unsupported"))


def test_view_6_on_the_family_14_observe_run():
    core = _family14("observe-result.json")
    view = _view(core)
    assert view["schema"] == "lab-campaign-view/15" and view["family"] == 14 and view["gate_mode"] == "observe"
    assert view["roles"] == {name: {"kind": core["roles"][name]["kind"]} for name in ("editor", "reflector", "grouper")}
    assert view["settings"] == {"stability": "stable", "round_stop": "first_accepted",
                                "baseline_refresh": core["settings"]["baseline_refresh"]}
    assert view["connection"]["outcome"] == "compatible" and set(view["connection"]) == {
        "state", "outcome", "attempts", "binding_digest", "operation_id", "detail"}
    promoted = next(r for r in view["rounds"] if r["outcome"] == "promoted")
    assert "cohort" not in promoted and isinstance(promoted["final"], list) and promoted["final"]
    entry = promoted["final"][-1]
    assert isinstance(entry["listed"], int) and entry["selection"] is not None
    finalist = next(a for a in promoted["attempts"] if a["outcome"] == "finalist")
    assert finalist["final_decision"]["state"] == "accepted" and finalist["group"] == "accepted"
    assert finalist["confidence"]["margin"] == 0
    responses.validate(view, "lab-campaign-view/15")


def test_view_6_unreadable_family_14_block_is_a_state_not_a_route_error():
    core = _family14("observe-result.json")
    del core["rounds"][0]["final"]
    view = _view(core)
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert view["roles"] is None and all(r["final"] is None for r in view["rounds"])
    responses.validate(view, "lab-campaign-view/15")


def _reign_core(rounds, *, final_champion=None, performed=()):
    return {"settings": {"stability": "unstable", "round_stop": "first_accepted",
                         "baseline_refresh": {"every_rounds": 1, "count": len(performed),
                                              "performed": [{"round": n, "measurement_id": m,
                                                             "replaced_measurement_id": "x", "completed": True}
                                                            for n, m in performed]}},
            "final_champion": final_champion, "rounds": rounds}


def _reign_round(n, outcome, *, selected=None, next_baseline=None, baseline=None):
    entry = {"selection": None if selected is None else {"attempt_id": selected, "reason": "finalist_selected",
                                                         "measurement_id": next_baseline},
             "next_baseline_measurement_id": next_baseline, "contrasts": []}
    return {"round": n, "outcome": outcome, "baseline_measurement_id": baseline, "final": [entry], "attempts": []}


def test_measurement_14_promotion_retained_and_refresh():
    initial = {"version": "v0", "origin": "initial", "attempt_id": None}
    promoted = {"version": "v1", "origin": "promoted_attempt", "attempt_id": "r01-a01"}
    rounds = [_reign_round(1, "promoted", selected="r01-a01", next_baseline="m-r01-final-01-a01", baseline="m-r00-champion"),
              _reign_round(2, "retained", next_baseline="m-r02-final-01-champion"),
              _reign_round(3, "retained", next_baseline=None)]
    assert _measurement_14(initial, rounds, _reign_core(rounds)) == "m-r00-champion"
    assert _measurement_14(promoted, rounds, _reign_core(rounds)) == "m-r02-final-01-champion"
    refreshed = _reign_core(rounds, performed=[(3, "m-r03-refresh")])
    assert _measurement_14(promoted, rounds, refreshed) == "m-r03-refresh"
    final = _reign_core(rounds, final_champion={"version": "v1", "measurement_id": "m-final"})
    assert _measurement_14(promoted, rounds, final) == "m-final"
    assert _measurement_14({"version": "v9", "origin": "promoted_attempt", "attempt_id": "r09-a01"},
                           rounds, _reign_core(rounds)) is None


def test_measurement_14_stable_retained_keeps_the_stored_side():
    promoted = {"version": "v1", "origin": "promoted_attempt", "attempt_id": "r01-a01"}
    rounds = [_reign_round(1, "promoted", selected="r01-a01", next_baseline="m-r01-final-01-a01"),
              _reign_round(2, "retained", next_baseline="m-r01-final-01-a01")]   # stable: core keeps the stored measurement
    assert _measurement_14(promoted, rounds, _reign_core(rounds)) == "m-r01-final-01-a01"


def test_measurement_14_on_the_real_runs():
    """Task 0 smoke: the final champion is core's final_champion.measurement_id; the initial one is round 1's base."""
    for name in ("observe-result.json", "unstable-result.json"):
        core = _family14(name)
        rounds = core["rounds"]
        initial = {"version": "v0", "origin": "initial", "attempt_id": None}
        if core["final_champion"]["version"] != "v0":
            assert _measurement_14(initial, rounds, core) == rounds[0]["baseline_measurement_id"]
        # The walk agrees with core: without final_champion, the final champion's reign gives core's measurement
        # (on the unstable run through the baseline refresh, m-r03-refresh).
        final = core["final_champion"]
        assert (final["version"], final["baseline_origin"]) == ("v1", "promoted_attempt"), final
        promoted = [r for r in rounds if r["outcome"] == "promoted"][-1]
        champion = {"version": "v1", "origin": "promoted_attempt",
                    "attempt_id": promoted["final"][-1]["selection"]["attempt_id"]}
        assert _measurement_14(champion, rounds, dict(core, final_champion=None)) == final["measurement_id"], name


def _view_with_lineage(core, champions):
    return campaign_view("op14", core, _PLAN, None, champions, frozenset(), [],
                         details_unavailable("core_raw_unsupported"))


def test_view_6_wires_family_14_champion_measurements():
    core = _family14("observe-result.json")
    view = _view_with_lineage(core, [dict(c) for c in _CHAMPIONS_14])
    measurements = {c["version"]: c["measurement_id"] for c in view["champions"]}
    assert measurements == {"v0": "m-r00-champion", "v1": core["final_champion"]["measurement_id"]}
    assert view["details_state"] == "unavailable" and view["details_reason"] == "core_raw_unsupported"
    responses.validate(view, "lab-campaign-view/15")


def test_view_6_unplaceable_family_14_champion_makes_details_unreadable():
    """Spec 2026-10-13 §7.1: a Champion core's blocks cannot place has no measurement, and the details are unreadable;
    core's readable family-14 blocks stay."""
    stray = {"version": "v9", "digest": "d9", "origin": "promoted_attempt", "attempt_id": "r09-a01"}
    view = _view_with_lineage(_family14("observe-result.json"), [dict(c) for c in _CHAMPIONS_14] + [stray])
    assert next(c for c in view["champions"] if c["version"] == "v9")["measurement_id"] is None
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert view["roles"] is not None and any(r["final"] for r in view["rounds"])
    responses.validate(view, "lab-campaign-view/15")


def _family_14_validator_case(case):
    import copy
    if case == "unreadable_caveats_keep_blocks":
        core = _family14("observe-result.json")
        core["caveats"] = None
        return _view(core)
    if case == "family_14_settings_alone_null":
        view = _view(_family14("observe-result.json"))
        view["settings"] = None
        return view
    if case == "family_14_unknown_final_decision":
        view = copy.deepcopy(_view(_family14("observe-result.json")))
        finalist = next(a for r in view["rounds"] for a in r["attempts"] if a["final_decision"] is not None)
        finalist["final_decision"]["state"] = "maybe"
        return view
    raise AssertionError(case)


@pytest.mark.parametrize("case, valid", [
    ("unreadable_caveats_keep_blocks", True),
    ("family_14_settings_alone_null", False),
    ("family_14_unknown_final_decision", False),
])
def test_validator_6_family_14_branches(case, valid):
    view = _family_14_validator_case(case)
    if case == "unreadable_caveats_keep_blocks":
        assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
        assert view["roles"] is not None and view["settings"] is not None and view["connection"] is not None
    if valid:
        responses.validate(view, "lab-campaign-view/15")
    else:
        with pytest.raises(ValueError):
            responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("schema", ["lab-campaign-view/5", "lab-campaign-view/6", "lab-campaign-view/7", "lab-campaign-view/8", "lab-campaign-view/14"])
def test_validator_7_rejects_old_schemas(schema):
    view = dict(_view(_family14("observe-result.json")), schema=schema)
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")
    with pytest.raises(ValueError):
        responses.validate(view, schema)


@pytest.mark.parametrize("family", [10, 13])
def test_validator_7_rejects_old_families(family):
    view = dict(_view(_family14("observe-result.json")), family=family)
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("key", ["cohort", "alpha_total"])
def test_validator_7_rejects_removed_keys_even_when_null(key):
    view = _view(_family14("observe-result.json"))
    row = view["rounds"][0] if key == "cohort" else view["rounds"][0]["attempts"][0]
    row[key] = None
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("count,final,expected", [
    (1, {"version": "v1", "measurement_id": "m-r01-final"}, ["m-r00-champion", "m-r01-final", None]),
    (2, None, ["m-r00-champion", "m-r02-final", None]),
    (3, None, ["m-r00-champion", "m-r02-final", "m-r03-final"]),
    (4, None, ["m-r00-champion", "m-r02-final", "m-r04-final"]),
    (4, {"version": "v2", "measurement_id": "m-final"}, ["m-r00-champion", "m-r02-final", "m-final"]),
])
def test_family_14_champion_measurement_over_several_reigns(count, final, expected):
    rounds = [
        _reign_round(1, "promoted", selected="r01-a01", next_baseline="m-r01-final", baseline="m-r00-champion"),
        _reign_round(2, "retained", next_baseline="m-r02-final"),
        _reign_round(3, "promoted", selected="r03-a02", next_baseline="m-r03-final"),
        _reign_round(4, "retained", next_baseline="m-r04-final"),
    ][:count]
    champions = [{"version": "v0", "origin": "initial", "attempt_id": None},
                 {"version": "v1", "origin": "promoted_attempt", "attempt_id": "r01-a01"},
                 {"version": "v2", "origin": "promoted_attempt", "attempt_id": "r03-a02"}]
    core = _reign_core(rounds, final_champion=final)
    assert [_measurement_14(c, rounds, core) for c in champions] == expected


def test_initial_champion_retained_in_round_1_takes_final_measurement():
    rounds = [_reign_round(1, "retained", next_baseline="m-r01-final", baseline="m-r00-champion")]
    assert _measurement_14({"version": "v0", "origin": "initial", "attempt_id": None}, rounds,
                           _reign_core(rounds)) == "m-r01-final"


# --- lab-campaign-view/15: family 14 keeps /7's content, family 15 carries the independent block (spec D4) ---

VIEW7_KEYS = {"schema", "operation_id", "policy", "progress", "outcome", "champions", "champions_state", "rounds",
              "measurements", "suspensions", "details_state", "details_reason", "details_source", "family",
              "gate_mode", "caveats", "lab_notices", "lab_notices_state", "roles", "settings", "connection",
                         "reflector_probe", "reflector_probe_state", "reflector_probe_reason"}
VIEW15_KEYS = (VIEW7_KEYS - {"gate_mode"}) | {"independent", "checks", "checks_state", "checks_reason"}


@pytest.mark.parametrize("missing_bindings", [False, True])
def test_b13_check_modes_survive_binding_failure(missing_bindings):
    core = _core15()
    core["independent"].update(check_modes={"seal_oracle_leak": "off"},
                               shadow_hits={"seal_oracle_leak": 2})
    details = dict(_DETAILS, bindings_reason="unreadable") if missing_bindings else _DETAILS
    view = _view15(core, details)
    assert view["checks"] == {"modes": {"seal_oracle_leak": "off"},
                              "shadow_hits": {"seal_oracle_leak": 2}}
    assert (view["checks_state"], view["checks_reason"]) == ("present", None)
    assert (view["independent"] is None) == missing_bindings
    assert "check_modes" not in (view["independent"] or {})
    responses.validate(view, "lab-campaign-view/15")
    from evolution_lab.cli.output import _campaign_lines
    lines = _campaign_lines(view)
    index = lines.index("checks: seal_oracle_leak=off")
    assert lines[index + 1].startswith("independent:")
    assert lines[index + 2] == "shadow hits: seal_oracle_leak · count 2"


@pytest.mark.parametrize("bad", [False, True])
def test_b13_checks_absent_or_unreadable(bad):
    core = _core15()
    if bad:
        core["independent"]["check_modes"] = {"unknown_check": "off"}
    view = _view15(core)
    assert view["checks"] is None
    assert (view["checks_state"], view["checks_reason"]) == (
        ("unavailable", "unreadable") if bad else ("present", None))
    responses.validate(view, "lab-campaign-view/15")
    from evolution_lab.cli.output import _campaign_lines
    assert [line for line in _campaign_lines(view) if line.startswith("checks:")] == (
        ["checks: unavailable (unreadable)"] if bad else [])


def _core15(block=None):
    """A family-15 status payload: Core sets ``family_version`` and ``independent`` only for 15
    (campaign_projections.py:2133); ``rounds[].final`` is always [] and ``look``/``confidence`` null."""
    import copy
    from test_owner_independent_view import BLOCK
    core = copy.deepcopy(_CORE)
    core.update(family_version=15, independent=copy.deepcopy(BLOCK if block is None else block),
                campaign_outcome=None, final_champion={"version": "v0", "digest": "d",
                                                       "measurement_id": "m-r00-champion"})
    core["rounds"][0]["outcome"] = None
    first, second = core["rounds"][0]["attempts"]
    first.update(outcome="finalist", reason="passed", look=None, confidence=None)
    second.update(look=None, confidence=None)
    return core


def _view15(core=None, details=None):
    return campaign_view("op15", _core15() if core is None else core, _PLAN, None, _CHAMPIONS,
                         frozenset({"r01-a01"}), [], _DETAILS if details is None else details)


@pytest.mark.parametrize("details", [details_unavailable("unreadable"),
    {**_DETAILS, "bindings": None, "bindings_reason": "unreadable"}])
def test_binding_read_failure_makes_independent_unreadable_not_inferred(details):
    view = _view15(details=details)
    assert view["independent"] is None
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered", "statistical"])
def test_family15_gate_mode_is_its_recorded_settings_not_a_family14_caveat(mode):
    from evolution_lab.owner.campaign_views import gate_mode_of
    core = _core15()
    core["independent"]["settings"]["mode"] = mode
    core["caveats"] = [{"kind": "observe_gate_not_enforced", "round": None}]
    assert gate_mode_of(core) == mode


def test_view_8_family_14_is_view_7_with_a_new_schema():
    view = _view(_family14("observe-result.json"))
    assert set(view) == VIEW7_KEYS and view["schema"] == "lab-campaign-view/15" and view["family"] == 14
    assert all("final" in r for r in view["rounds"])
    assert all({"look", "confidence", "final_decision"} <= set(a) for r in view["rounds"] for a in r["attempts"])
    responses.validate(view, "lab-campaign-view/15")


def test_view_8_family_15_replaces_confidence_and_final_with_independent():
    view = _view15()
    assert view["family"] == 15 and "gate_mode" not in view and "independent" in view
    assert all("final" not in r for r in view["rounds"])
    assert set(view) == VIEW15_KEYS and view["schema"] == "lab-campaign-view/15"
    finalist, abandoned = view["rounds"][0]["attempts"]
    assert set(finalist) == {"attempt_id", "outcome", "reason", "group", "measurement_id", "has_tree", "failure"}
    assert (finalist["group"], abandoned["group"]) == ("in_progress", "stopped")
    assert abandoned["failure"]["stage"] == "editor" and finalist["has_tree"] is True
    assert view["independent"]["attempts"][0]["comparison"]["scopes"][0]["display"] == "bound"
    assert view["independent"]["attempts"][0]["group"] == "in_progress"
    assert (view["details_state"], view["details_reason"]) == ("present", None)
    assert view["roles"] is not None and view["settings"] is not None and view["connection"] is not None
    assert view["champions"][0]["measurement_id"] == "m-r00-champion"     # Core's final_champion
    responses.validate(view, "lab-campaign-view/15")


def test_view_8_family_15_groups_a_finalist_by_cores_global_comparison():
    import copy
    from test_owner_independent_view import BLOCK, GLOBAL, R
    block = copy.deepcopy(BLOCK)
    block["attempts"][0]["comparison"].update(deciding_layer="global", scopes=[GLOBAL], confidence_scope=None)
    block["attempts"][0]["global_comparison"] = block["attempts"][0]["comparison"]
    block["attempts"][0]["eligible_final_score"] = R(7, 10)
    view = _view15(_core15(block))
    assert view["rounds"][0]["attempts"][0]["group"] == "accepted"
    assert view["independent"]["attempts"][0]["group"] == "accepted"
    responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("mutate", [
    lambda core: core["independent"]["attempts"][0]["comparison"]["scopes"][0].__setitem__(
        "point", {"format": "rational-hex/2", "numerator": "1", "denominator": "1"}),
    lambda core: core.__delitem__("independent"),
    lambda core: core["independent"].__setitem__("settings", None),
], ids=["bad-rational", "no-block", "settings-null"])
def test_view_8_family_15_malformed_block_is_unreadable_not_a_route_error(mutate):
    core = _core15()
    mutate(core)
    view = _view15(core)
    assert view["independent"] is None
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert view["rounds"][0]["attempts"][0]["outcome"] == "finalist"
    responses.validate(view, "lab-campaign-view/15")


def test_view_8_family_15_unauthenticated_evidence_is_a_state():
    from test_owner_independent_view import BLOCK
    view = _view15(_core15({**BLOCK, "evidence": "unavailable", "attempts": [], "committed_work": None,
                            "remaining_work": None, "next_action": "repair_evidence"}))
    assert view["independent"]["evidence"] == "unavailable" and view["independent"]["attempts"] == []
    assert view["details_state"] == "present"
    responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("details", [
    {**_DETAILS, "bindings": None, "bindings_reason": "unreadable"},
    details_unavailable("unreadable"),
])
def test_c58r1_core_unavailable_does_not_need_binding_publications(details):
    from test_owner_independent_view import BLOCK
    block = {**BLOCK, "evidence": "unavailable", "attempts": [], "committed_work": None,
             "remaining_work": None, "next_action": "repair_evidence"}
    view = _view15(_core15(block), details=details)
    assert view["independent"] is not None
    assert view["independent"]["evidence"] == "unavailable"
    assert view["independent"]["next_action"] == "repair_evidence"
    assert view["independent"]["attempts"] == []
    responses.validate(view, "lab-campaign-view/15")


def test_c58r1_required_binding_failure_keeps_known_round_facts():
    good = _view15()
    bad = _view15(details={**_DETAILS, "bindings": None, "bindings_reason": "unreadable"})
    assert bad["independent"] is None
    assert (bad["details_state"], bad["details_reason"]) == ("unavailable", "unreadable")
    assert bad["rounds"] == good["rounds"]
    responses.validate(bad, "lab-campaign-view/15")


@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__("baseline_version", 7),
    lambda r: r.__setitem__("reflection", {"state": "future_state"}),
    lambda r: r["attempts"][1].__setitem__("failure", {"stage": "future_stage"}),
    lambda r: r.__setitem__("reflection", None),  # cannot pair a known baseline/failure with unread round facts
])
def test_c58r1_partial_round_decoder_still_refuses_invalid_facts(mutate):
    view = _view15()
    view.update(independent=None, details_state="unavailable", details_reason="unreadable")
    mutate(view["rounds"][0])
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("family", [14, 15])
def test_c58r1_partial_round_exception_is_only_for_independent_failure(family):
    view = (_view15() if family == 15 else
            campaign_view("op14", _CORE, _PLAN, None, _CHAMPIONS, frozenset(), [], _DETAILS))
    assert view["rounds"][0]["reflection"] is not None
    view.update(details_state="unavailable", details_reason="unreadable")
    # A readable independent block (or family14) does not activate this narrow
    # exception to the old unavailable-round correlation.
    with pytest.raises(ValueError):
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("method", ["campaign", "_content_campaign"])
def test_c58r1_owner_view_callers_share_the_acquired_read(monkeypatch, tmp_path, method):
    from types import SimpleNamespace
    from evolution_lab.owner import core_raw, readers
    from evolution_lab.owner.models import ResourceRef
    core = _core15()
    read = core_raw.CampaignRead(SimpleNamespace(), 15, None, None,
        bindings={"r01-a01": {"gate_binding": {"source": "core_raw", "scopes": [
            {"name": "focus", "rule": "bound"}]}, "global_binding": None}})
    seen = []

    def acquire(root, operation, **kwargs):
        seen.append(("acquire", root, operation, kwargs))
        return read

    def details(run, **kwargs):
        assert kwargs == {"read": read}
        seen.append(("details", read))
        return {**_DETAILS, "bindings": read.bindings, "bindings_reason": None}

    class Reader(readers.ReaderSupport):
        state_directory = tmp_path
        _result_io = SimpleNamespace(acquire_read=lambda *a: None)

        def __init__(self):
            self._baseline_provenance = core_raw.BaselineProvenanceCache()

        def _assert_loop(self):
            pass

        async def _db(self, name, *args):
            assert name == "operation_status"
            return SimpleNamespace(kind="campaign")

        async def _readable_status(self, ref, acquired=None):
            assert acquired is read
            return SimpleNamespace(prepared_digest="d" * 64, progress=None), core

        async def _campaign_plan(self, digest):
            return _PLAN

        async def _rows_of(self, *args):
            return []

        async def _result_thread(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

        async def _result_work(self, work, lease):
            return await work()

    monkeypatch.setattr(readers, "acquire_campaign", acquire)
    monkeypatch.setattr(core_raw, "campaign_details", details)
    monkeypatch.setattr("evolution_lab.owner.campaign_views.lineage_champions", lambda run: (None, None))
    result = asyncio.run(getattr(Reader(), method)(ResourceRef("operation", "op15")))
    view = result if method == "campaign" else result[2]
    assert len([item for item in seen if item[0] == "acquire"]) == 1
    assert seen[0][-1] == {"with_result": False}
    assert view["independent"]["attempts"][0]["gate_binding"] == read.bindings["r01-a01"]["gate_binding"]
    if method == "campaign":
        # This binding-only acquisition supplies no snapshot for baseline evidence.
        assert view["lab_notices_state"] == "unavailable"
    responses.validate(view, "lab-campaign-view/15")


def _bf_cached_reader(monkeypatch, tmp_path, *, family=14):
    """A failed sibling acquisition with a retained, previously readable Core projection.

    Keep the real _project_campaign fallback and all view/route builders; replace
    only storage/thread boundaries and the unrelated role-content reads.
    """
    from dataclasses import dataclass
    from types import SimpleNamespace
    from evolution_lab.owner import core_raw, readers, sample_view
    from evolution_lab.owner.models import OperationResult, ProcessFacts

    core = deepcopy(_CORE) if family == 14 else _core15()
    assert core.get("family_version", 14) == family
    process = ProcessFacts(launched=True, exit_code=0, cleanup_complete=True, forced=False)

    @dataclass
    class CachedStatus:
        id: str = "cached-op"
        kind: str = "campaign"
        prepared_digest: str = "d" * 64
        control_state: str = "finished"
        process: object = None
        result: object = None
        results_state: str = "present"
        progress: object = None

    cached = CachedStatus(process=process, result=OperationResult("d" * 64, process, core=core))
    failure = FileNotFoundError("measurement sibling unavailable")
    details_calls = []

    def details(run, **kwargs):
        details_calls.append(kwargs)
        assert kwargs == {}, "family14 details remain independent of a failed acquisition"
        return deepcopy(_DETAILS)

    class Reader(readers.ReaderSupport):
        state_directory = tmp_path
        _result_io = SimpleNamespace(acquire_read=lambda *a: None)

        def __init__(self):
            self._baseline_provenance = core_raw.BaselineProvenanceCache()

        def _assert_loop(self):
            pass

        async def _db(self, name, *args):
            assert name == "operation_status"
            return cached

        async def _status_projection(self, ref, acquired=None):
            assert acquired is failure
            projected = readers._project_campaign(tmp_path, cached, acquired)
            assert projected.results_state == "unavailable"
            assert projected.result.core == cached.result.core  # the actual cached projection fallback
            return projected

        async def _campaign_plan(self, digest):
            return _PLAN

        async def _rows_of(self, *args):
            return []

        async def _result_thread(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

        async def _result_work(self, work, lease):
            return await work()

        async def _role_kind(self, *args):
            return "subprocess"

        async def _content_facts(self, run):
            return {}

    monkeypatch.setattr(readers, "acquire_campaign", lambda *a, **k: failure)
    monkeypatch.setattr(core_raw, "campaign_details", details)
    monkeypatch.setattr("evolution_lab.owner.campaign_views.lineage_champions", lambda run: (None, None))
    monkeypatch.setattr(sample_view, "reflection_view", lambda op, reflection, round_no, *a:
                        {"reflection_id": reflection, "round": round_no})
    monkeypatch.setattr(sample_view, "attempt_view", lambda op, attempt, round_no, confidence, failure, *a:
                        {"attempt_id": attempt, "round": round_no, "failure": failure})
    return Reader(), details_calls


@pytest.mark.parametrize("method", ["campaign", "_content_campaign", "attempt", "reflection"])
def test_bf1_cached_family14_keeps_independent_details_and_routes(monkeypatch, tmp_path, method):
    from evolution_lab.owner.models import ResourceRef
    reader, calls = _bf_cached_reader(monkeypatch, tmp_path)
    ref = ResourceRef("operation", "cached-op")
    args = ("r01-a02",) if method == "attempt" else ("r01-x01",) if method == "reflection" else ()
    result = asyncio.run(getattr(reader, method)(ref, *args))
    assert calls == [{}]
    if method == "attempt":
        assert result["failure"]["error_class"] == "reported"
    elif method == "reflection":
        assert result == {"reflection_id": "r01-x01", "round": 1}
    else:
        view = result if method == "campaign" else result[2]
        row = view["rounds"][0]
        assert row["baseline_version"] == "v0"
        assert row["reflection"]["reflection_id"] == "r01-x01"
        assert row["attempts"][1]["failure"]["error_class"] == "reported"
        assert view["details_state"] == "present" and view["family"] == 14
        if method == "campaign":
            assert view["lab_notices_state"] == "unavailable"
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("method", ["campaign", "_content_campaign"])
def test_bf1_cached_family15_still_requires_shared_acquisition(monkeypatch, tmp_path, method):
    from evolution_lab.owner.models import ResourceRef
    reader, calls = _bf_cached_reader(monkeypatch, tmp_path, family=15)
    result = asyncio.run(getattr(reader, method)(ResourceRef("operation", "cached-op")))
    view = result if method == "campaign" else result[2]
    assert calls == []
    assert view["independent"] is None and view["details_reason"] == "unreadable"
    assert view["rounds"][0]["reflection"] is None
    if method == "campaign":
        assert view["lab_notices_state"] == "unavailable"
    responses.validate(view, "lab-campaign-view/15")


def test_view_8_family_15_notice_uses_the_family_15_groups():
    core = _core15()
    core["campaign_outcome"] = {"outcome": "completed", "reason": "rounds_exhausted"}
    core["rounds"][0]["attempts"][0].update(outcome="rejected", reason="structurally_unattainable")
    view = _view15(core)
    assert view["lab_notices"] == [{"code": "no_candidate_measured", "rounds": 1,
                                    "by_reason": {"editor_failed": 1, "structurally_unattainable": 1},
                                    "reflection_errors": {}}]


def _bad_views_of_family_15(view15, view14):
    import copy

    def changed(base, edit):
        value = copy.deepcopy(base)
        edit(value)
        return value

    comparison = lambda v: v["independent"]["attempts"][0]["comparison"]  # noqa: E731
    return {
        "family-14-with-independent": dict(view14, independent=None),
        "family-15-with-gate-mode": dict(view15, gate_mode="statistical"),
        "family-15-labelled-14": dict(view15, family=14),
        "family-14-labelled-15": dict(view14, family=15),
        "family-15-round-final": changed(view15, lambda v: v["rounds"][0].__setitem__("final", [])),
        "family-15-attempt-confidence": changed(view15, lambda v: v["rounds"][0]["attempts"][0].__setitem__(
            "confidence", None)),
        "unknown-display": changed(view15, lambda v: comparison(v)["scopes"][0].__setitem__("display", "maybe")),
        "parent-reference": changed(view15, lambda v: comparison(v).__setitem__("reference", "parent")),
        "layer-not-gate-or-global": changed(view15, lambda v: comparison(v).__setitem__("layer", "focus")),
        "string-value": changed(view15, lambda v: comparison(v)["scopes"][0]["point"].__setitem__("value", "0.12")),
        "infinite-value": changed(view15, lambda v: comparison(v)["scopes"][0]["point"].__setitem__(
            "value", float("inf"))),
        "bare-rational": changed(view15, lambda v: comparison(v)["scopes"][0].__setitem__(
            "point", comparison(v)["scopes"][0]["point"]["exact"])),
        "extra-scope-key": changed(view15, lambda v: comparison(v)["scopes"][0].__setitem__("upper", None)),
        "null-block-with-details": dict(view15, independent=None),
        "unknown-group": changed(view15, lambda v: v["independent"]["attempts"][0].__setitem__("group", "x")),
    }


def test_cli_validator_accepts_both_families_and_refuses_mixed_keys():
    view14 = _view(_family14("observe-result.json"))
    view15 = _view15()
    assert responses.validate(view14, "lab-campaign-view/15") is view14
    assert responses.validate(view15, "lab-campaign-view/15") is view15
    for label, bad in _bad_views_of_family_15(view15, view14).items():
        with pytest.raises(ValueError):
            responses.validate(bad, "lab-campaign-view/15")
            pytest.fail(label)


def test_campaign_text_lines_of_family_15():
    import copy
    from evolution_lab.cli.output import _campaign_lines
    view = _view15()
    lines = _campaign_lines(view)
    assert "  focus: gain 0.12 (estimate); with 90% confidence not below 0.03; threshold ≥ 0.00 → passed" in lines
    assert "  global: gain 0.05; threshold ≥ 0.00" in lines
    assert ("independent: mode=confidence R=1 verification=reuse noise=negligible allowance=0.00 "
            "(assumed_negligible) stability=stable fresh_parent=no global=point") in lines
    assert "r01-a01 in_progress finalist/passed" in lines
    assert "  gate passed/passed (focus) · vs round champion · stored measurement · ref m-reference" in lines
    attempt_line = next(line for line in lines if "vs round champion" in line)
    assert "fresh_parent=" not in attempt_line
    assert not any("gate:" in line for line in lines) and not any(".030000" in line for line in lines)
    out_of_range = copy.deepcopy(view)
    out_of_range["independent"]["attempts"][0]["gate_comparison"]["scopes"][1]["threshold"]["value"] = None
    assert ("  global: gain 0.05; threshold ≥ outside display range (exact value kept)"
            in _campaign_lines(out_of_range))
    from test_owner_independent_view import BLOCK
    unavailable = _view15(_core15({**BLOCK, "evidence": "snapshot_only", "attempts": []}))
    lines = _campaign_lines(unavailable)
    assert "comparisons unavailable (snapshot_only)" in lines and not any("no attempts" in line for line in lines)


def test_campaign_text_lines_of_family_15_preflight_and_other_displays():
    from evolution_lab.cli.output import independent_lines
    from test_owner_independent_view import FOCUS, GLOBAL, PREFLIGHT_BLOCK, _with_scopes
    from evolution_lab.owner.independent_view import independent_view
    lines = independent_lines(independent_view(PREFLIGHT_BLOCK))
    assert "r01-a02 not_enough_data rejected/structurally_unattainable · no comparison" in lines
    assert ("  preflight impossible: focus maximal confidence 50% vs requested 90% (cannot pass); "
            "global maximal gain 1.00 vs threshold > 0.00 (can pass)") in lines
    assert "round 1 preflight impossible (stored_reference)" in lines
    off = dict(FOCUS, rule="off", threshold=None, strict=None, confidence=None, enclosure=None)
    skipped = dict(GLOBAL, status="unavailable", unavailable="empty_support")
    lines = independent_lines(independent_view(_with_scopes(off, skipped, mode="unfiltered")))
    assert "  focus: gain 0.12 · no threshold → passed" in lines
    assert "  global: unavailable (empty_support)" in lines


def test_view_8_family_15_finalist_without_a_global_comparison_is_unknown_once_its_round_is_closed():
    """The round's closed state (Core's round outcome) decides: Core cannot close a round before every finalist has
    a global result, so a closed round's finalist Lab cannot see a comparison for is unknown, not in progress."""
    import copy

    def finalist_group(view):
        return (view["rounds"][0]["attempts"][0]["group"], view["independent"]["attempts"][0]["group"])

    block = copy.deepcopy(_core15()["independent"])
    block["attempts"][0]["comparison"] = None
    open_core = _core15(block)
    assert finalist_group(_view15(open_core)) == ("in_progress", "in_progress")
    closed_core = copy.deepcopy(open_core)
    closed_core["rounds"][0]["outcome"] = "retained"
    assert finalist_group(_view15(closed_core)) == ("unknown", "unknown")
    # an unreadable block: no comparison is visible either
    broken = copy.deepcopy(closed_core)
    broken["independent"]["evidence"] = "maybe"
    assert _view15(broken)["rounds"][0]["attempts"][0]["group"] == "unknown"


def test_view_8_family_15_finalist_of_an_open_round_is_unknown_once_the_campaign_finished():
    """Core admits ``stopped``/``cancelled`` with the round open (no ``round_closed``): a gate finalist cancelled
    during candidate completion keeps ``round.outcome`` null and only its gate comparison. In the finished campaign
    (Core's ``complete``/``campaign_outcome``) it is unknown, not in progress forever; a recorded global wins."""
    import copy

    def finalist_group(view):
        return (view["rounds"][0]["attempts"][0]["group"], view["independent"]["attempts"][0]["group"])

    running = _core15()
    assert running["rounds"][0]["outcome"] is None
    assert finalist_group(_view15(running)) == ("in_progress", "in_progress")
    finished = copy.deepcopy(running)
    finished.update(complete=True, campaign_outcome={"outcome": "stopped", "reason": "cancelled",
                                                    "continuation": {"kind": "none", "reason": None}})
    assert finalist_group(_view15(finished)) == ("unknown", "unknown")
    responses.validate(_view15(finished), "lab-campaign-view/15")
    accepted = copy.deepcopy(finished)
    accepted["independent"]["attempts"][0]["comparison"]["deciding_layer"] = "global"    # Core's global passed
    accepted["independent"]["attempts"][0]["global_comparison"] = accepted["independent"]["attempts"][0]["comparison"]
    assert _view15(accepted)["rounds"][0]["attempts"][0]["group"] == "accepted"


def test_campaign_and_report_text_print_the_deciding_comparisons_reason():
    """Codex r2: a gate finalist keeps finalist/passed while its global comparison is comparison_not_passed/
    critical_failed, whatever the gain. The comparison's outcome and reason are their own line, apart from the
    attempt's outcome/reason (CLI ``lab campaign`` and ``lab report``; ``report.md`` already says it)."""
    import copy
    from evolution_lab.cli.output import _campaign_lines, _report_lines
    from evolution_lab.owner.independent_view import independent_view
    from test_owner_independent_view import BLOCK
    block = copy.deepcopy(BLOCK)
    block["attempts"][0]["comparison"].update(outcome="comparison_not_passed", reason="critical_failed",
                                              deciding_layer="global", eligible=False)
    block["attempts"][0]["global_comparison"] = block["attempts"][0]["comparison"]
    lines = _campaign_lines(_view15(_core15(block)))
    assert "  global comparison_not_passed/critical_failed (global) · vs round champion · stored measurement · ref m-reference" in lines
    assert any(line.startswith("r01-a01 ") and "finalist/passed" in line for line in lines)
    report = {"operation": "campaign", "lab_independent": independent_view(block),
              "core": {"family_version": 15, "campaign_outcome": None, "caveats": [], "rounds": []}}
    assert "  global comparison_not_passed/critical_failed (global) · vs round champion · stored measurement · ref m-reference" in _report_lines(
        {"schema": "lab-owner-report/15", "report": report})


@pytest.mark.parametrize("location,field", [
    ("round", "outcome"), ("round", "reason"),
    ("independent", "outcome"), ("independent", "reason"), ("comparison", "reason"),
])
@pytest.mark.parametrize("bad", [1, True, [], {}])
def test_view_8_family_15_decoder_refuses_non_string_attempt_and_comparison_codes(location, field, bad):
    view = _view15()
    if location == "round":
        row = view["rounds"][0]["attempts"][0]
    else:
        row = view["independent"]["attempts"][0]
        if location == "comparison":
            row = row["comparison"]
    row[field] = bad
    with pytest.raises(ValueError, match="invalid public response"):
        responses.validate(view, "lab-campaign-view/15")


@pytest.mark.parametrize("code", [None, "recorded_code"])
def test_view_8_family_15_decoder_accepts_string_or_null_attempt_and_comparison_codes(code):
    view = _view15()
    for row in (view["rounds"][0]["attempts"][0], view["independent"]["attempts"][0]):
        row.update(outcome=code, reason=code)
    view["independent"]["attempts"][0]["comparison"]["reason"] = code
    assert responses.validate(view, "lab-campaign-view/15") is view


def test_family_15_early_global_attempt_projects_as_generation():
    from evolution_lab.owner.progress import campaign_progress

    core = _core15()
    core["actions"] = [{"kind": "OpenAttempt", "path": "early_global", "round": 1}]
    progress = campaign_progress(core, _PLAN, None)
    view = campaign_view("op15", core, _PLAN, progress, _CHAMPIONS,
                         frozenset({"r01-a01"}), [], _DETAILS)
    assert view["progress"]["phase"] == "generation"
    assert view["progress"]["stages"] == ["generation"]
    responses.validate(view, "lab-campaign-view/15")


def test_family15_view_is_9_and_b8_core_block_is_unreadable():
    core = _core15()
    del core["independent"]["attempts"][0]["gate_comparison"]
    view = _view15(core)
    assert view["schema"] == "lab-campaign-view/15"
    assert view["independent"] is None
    assert (view["details_state"], view["details_reason"]) == ("unavailable", "unreadable")
    assert view["rounds"][0]["attempts"][0]["outcome"] == "finalist"


@pytest.mark.parametrize("global_present,closed,group", [(False, False, "in_progress"),
    (False, True, "unknown"), (True, False, "agent_worse")])
def test_round_rows_group_finalist_by_global_comparison(global_present, closed, group):
    core = _core15()
    attempt = core["independent"]["attempts"][0]
    # The compatibility summary still says passed; only the explicit global field decides the group.
    attempt["global_comparison"] = (dict(attempt["comparison"], deciding_layer="global",
        outcome="comparison_not_passed", reason="critical_failed", eligible=False) if global_present else None)
    core["rounds"][0]["outcome"] = "retained" if closed else None
    view = _view15(core)
    assert view["rounds"][0]["attempts"][0]["group"] == group
    assert view["independent"]["attempts"][0]["group"] == group


@pytest.mark.parametrize("carrier", [
    "grouping_finished", "grouping_failed", "reflection_finished", "reflection_failed",
    "attempt_input_frozen", "editor_finished", "editor_failed", "attempt_closed",
])
def test_b13_shadowed_carriers_are_not_content_fields(monkeypatch, tmp_path, carrier):
    from types import SimpleNamespace
    from evolution.tree import campaign_schema
    from evolution.tree.storage import append_log
    from evolution_lab.owner import core_raw, sample_view

    # Unit boundary: Core's scanner/reducer owns journal admission (Task7 tests
    # real journals). Exercise every b13 carrier through Lab's actual extraction.
    terminal = {"round": 1, "reflection_id": "r01-x01", "status": "ok", "groups": []}
    payload = {"round": 1, "attempt_id": "r01-a01", "shadowed": [{"check": "seal_oracle_leak", "code": "oracle_leak", "count": 1}]}
    if carrier.startswith("reflection_"):
        payload.update(terminal)
        if carrier == "reflection_failed":
            payload.update(error_class="reported", reason="invalid_result", detail={"category": "protocol"})
    envelopes = [{"event": "reflection_finished", "payload": terminal},
                 {"event": "attempt_closed", "payload": {"attempt_id": "r01-a01"}},
                 {"event": carrier, "payload": payload},
                 {"event": "check_shadowed", "payload": {"check": "seal_oracle_leak", "code": "oracle_leak", "count": 1}}]
    records = [SimpleNamespace(to_payload=lambda row=row: row) for row in envelopes]
    monkeypatch.setattr(core_raw, "read_inside", lambda *args: b"unit journal boundary")
    monkeypatch.setattr(core_raw, "_journal_family", lambda *args: 15)
    monkeypatch.setattr(append_log, "_scan", lambda *args: (SimpleNamespace(records=records, journal_integrity="complete"), None))
    monkeypatch.setattr(campaign_schema, "reduce", lambda *args, **kwargs: SimpleNamespace(identity=True, family_version=15))
    facts = core_raw.campaign_content(tmp_path)
    assert facts["closed"] == {"r01-a01"}
    assert "shadowed" not in facts["reflections"]["r01-x01"]
    monkeypatch.setattr(core_raw, "role_log", lambda *args: ([{"phase": "model_request", "messages": [
        {"role": "user", "content": "neutral opening"}]}, {"event": "check_shadowed"}], False))
    for disclose in (False, True):
        attempt = sample_view.attempt_view("op", "r01-a01", 1, None, None, "chat", tmp_path, facts, disclose)
        reflection = sample_view.reflection_view("op", "r01-x01", 1, "chat", tmp_path, facts, disclose)
        responses.validate(attempt, "lab-attempt/1")
        responses.validate(reflection, "lab-reflection/1")
        assert reflection["skipped"] == 1 and reflection["state"] == "available"
        assert "shadowed" not in json.dumps([attempt, reflection])
        if carrier == "reflection_failed":
            assert reflection["events"][-1]["text"] == ("protocol" if disclose else {"withheld": "private"})


@pytest.mark.parametrize("scenario,expected", [
    ("focus-first", [("rejected", "not_run"), ("passed", "passed")]),
    ("focus-open", [("open", "waiting")]),
    ("focus-decided-open", [("rejected", "not_run")]),
    ("causal-open", [("passed", "open")]),
])
def test_stages_on_real_captures(scenario, expected):
    from dev63_default_outputs import read_outputs
    from evolution_lab.owner import core_raw

    run = Path(__file__).parent / "fixtures/panel/b15" / scenario / "run"
    outputs = read_outputs(run, include_panel=False)
    read = core_raw.campaign_read(run, families=core_raw.FAMILIES, with_result=True, with_bindings=True)
    source = read.status.to_payload()["independent"]["attempts"]
    view, report = outputs["campaign.json"], outputs["owner-report.json"]
    assert view["schema"] == "lab-campaign-view/15"
    assert report["schema"] == "lab-owner-report/15"
    assert view["details_state"] == report["report"]["lab_independent_state"] == "present"
    assert view["independent"] == report["report"]["lab_independent"]
    assert [tuple(s["state"] for s in a["stages"]) for a in view["independent"]["attempts"]] == expected
    for actual, core in zip(view["independent"]["attempts"], source, strict=True):
        assert actual["slots"] == core["slots"]
        assert [s["scope"] for s in actual["stages"]] == ["focus", "causal"]
    responses.validate(view, "lab-campaign-view/15")
    responses.validate(report, "lab-owner-report/15")


@pytest.mark.parametrize("scenario,fixture", [("agent-failed", "b14/agent-failed"),
                                              ("single-stage", "b15/insufficient-evidence")])
def test_dev64_default_outputs_equal_dev63(scenario, fixture):
    from dev63_default_outputs import read_outputs
    import report_md_compat

    tests = Path(__file__).parent
    outputs = read_outputs(tests / "fixtures/panel" / fixture / "run", include_panel=False)
    for member, actual in outputs.items():
        # dev72 adds unanswered evidence; compare the preserved dev63 fields separately.
        if member == "owner-report.json":
            for measurement in actual["report"]["measurements"]:
                for field in ("unanswered", "unanswered_state", "answered_reported"):
                    measurement.pop(field)
        expected = (tests / "data/dev63-default-outputs" / scenario / member).read_text()
        if member == "report.md":
            assert report_md_compat.current(actual) == report_md_compat.legacy(expected)
            continue
        if member.endswith(".json"):
            actual = report_md_compat.without_operator_progress(actual)
            baseline = json.loads(expected)
            baseline["schema"] = {"lab-campaign-view/13": "lab-campaign-view/15",
                                  "lab-owner-report/11": "lab-owner-report/15"}[baseline["schema"]]
            if member == "campaign.json":
                baseline["lab_notices_state"] = "present"
            elif member == "owner-report.json":
                baseline["report"]["lab_notices_state"] = "present"
                baseline["report"]["core"]["baseline_import"] = None
            assert actual == baseline
            continue
        elif member == "cli-campaign.txt":
            header, separator, body = expected.partition("\n")
            assert header == "lab-campaign-view/13 " + "a" * 32
            expected = "lab-campaign-view/15 " + "a" * 32 + separator + body
        actual = actual if isinstance(actual, str) else json.dumps(actual, ensure_ascii=False, indent=2) + "\n"
        assert actual == expected


def test_unavailable_stage_facts_keep_view_and_report_readable(monkeypatch):
    from dev63_default_outputs import read_outputs
    from evolution_lab.owner import core_raw

    def unavailable(*args, **kwargs):
        raise core_raw.CoreRawUnavailable("unreadable")

    monkeypatch.setattr(core_raw, "family15_stage_facts", unavailable)
    run = Path(__file__).parent / "fixtures/panel/b15/focus-first/run"
    outputs = read_outputs(run, include_panel=False)
    view, report = outputs["campaign.json"], outputs["owner-report.json"]["report"]
    assert view["details_state"] == report["lab_independent_state"] == "present"
    assert view["independent"] == report["lab_independent"]
    for row in view["independent"]["attempts"]:
        assert row["slots"]
        assert row["stages"] == [{"scope": "focus", "state": "unknown"}, {"scope": "causal", "state": "unknown"}]
