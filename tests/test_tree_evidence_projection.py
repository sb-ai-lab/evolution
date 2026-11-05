"""Pinned full-view inputs preserve the unpinned compatibility path."""

import hashlib
import json
from dataclasses import replace

from evolution.tree.execution.check_modes import ENFORCE_ALL
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor_contracts import observation_payload, reflector_observation_payload
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from tests.campaign_fakes import PRIMARY
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace


def pin(**settings):
    return EvidencePin(replace(DEFAULT_EVIDENCE, **settings), ENFORCE_ALL.input_modes())


def inputs(trace, evidence_pin=None, secrets=()):
    return FrozenReflectionInputs(
        baseline_binding=trace.baseline.digest,
        rows=(_reflect_row("s1", 1),),
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=cases(),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        pin=evidence_pin,
        secrets=secrets,
    )


def test_allowlist_view_reproduces_b12_projection_bytes(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        old = canonical_json(observation_payload(trace.project()[("c", "s1")]))
        new = canonical_json(
            observation_payload(trace.project(pin=pin(view="allowlist"))[("c", "s1")])
        )
        assert new == old
        assert (
            hashlib.sha256(old).hexdigest()
            == "c11ceb2fb574856fc89bed2bf1aaf60088215f661edd0083f75f114bbe85aa3d"
        )
    finally:
        trace.close()


def test_full_view_reaches_editor_reflector_and_the_grouping_observation_section(tmp_path):
    events = (
        {"type": "tool_call", "name": "list_service_requests", "arguments": {"status": "open"}},
        {"type": "tool_result", "name": "list_service_requests", "status": "ok", "result": []},
    )
    trace = _Trace(tmp_path, turns=1, events=events)
    try:
        observation = trace.project(pin=pin())[("c", "s1")]
        payload = observation_payload(observation)
        assert payload["view"] == "full"
        assert payload["turns"][0]["events"][0]["arguments"] == '{"status":"open"}'
        assert payload["turns"][0]["events"][1]["result"] == "[]"
        assert reflector_observation_payload(observation) == payload
        source = inputs(trace, pin())
        section = json.loads(
            source.read_grouping_input({"index": 0, "section": "observation"})["content"]
        )
        assert section["behavior"] == payload
        old = json.loads(
            inputs(trace).read_grouping_input({"index": 0, "section": "observation"})["content"]
        )
        assert {k: v for k, v in section.items() if k != "behavior"} == {
            k: v for k, v in old.items() if k != "behavior"
        }
        id_ = source.registry.records[0].evidence_id
        shown = source.read_evidence({"evidence_id": id_})
        assert "list_service_requests" in shown["content"]
        assert "arguments" in shown["content"]
    finally:
        trace.close()


def test_scripted_received_fields_and_reason_are_visible(tmp_path):
    def mangle(view):
        for t in view["history"]["turns"]:
            t["message"]["fields"] = {"received": t["index"]}
            t["reason"] = {"category": "candidate", "code": "bad", "extra": [1]}

    trace = _Trace(tmp_path, turns=2, environment="scripted", mangle=mangle)
    try:
        shown = observation_payload(trace.project(pin=pin())[("c", "s1")])
        assert len(shown["turns"]) == 2
        assert shown["turns"][1]["input"]["fields"] == '{"received":1}'
        assert shown["turns"][0]["reason"]["extra"] == "[1]"
        assert shown["turns"][0]["input"]["content"].startswith("turn 0")
    finally:
        trace.close()


def test_run_secrets_are_redacted_before_cuts_in_every_role_input(tmp_path):
    secret = "secret-run-abcdef"
    trace = _Trace(
        tmp_path,
        turns=1,
        events=(
            {"type": "tool_call", "name": "t", "arguments": {"key": secret}},
            {"type": "tool_result", "name": "t", "status": "ok", "result": secret},
            {"type": "assistant_message", "content": "a" * 30 + secret + "z" * 70},
        ),
    )
    try:
        for view in ("full", "allowlist"):
            selected = pin(view=view, text_chars=32)
            payload = observation_payload(
                trace.project(pin=selected, secrets=(secret,))[("c", "s1")]
            )
            text = canonical_json(payload).decode()
            assert secret not in text
            source = inputs(trace, selected, (secret,))
            shown = source.read_grouping_input({"index": 0, "section": "observation"})["content"]
            assert secret not in shown
    finally:
        trace.close()


def test_the_chat_grouper_representative_payload_keeps_its_b12_bytes(tmp_path):
    from evolution.tree.roles.grouping.model_grouping import _representative_payload

    trace = _Trace(tmp_path, turns=10)
    try:
        old = inputs(trace)
        full = inputs(trace, pin(turns=1, events_per_turn=2))
        assert canonical_json(
            _representative_payload(old.registry.records[0], 4096)
        ) == canonical_json(_representative_payload(full.registry.records[0], 4096))
    finally:
        trace.close()


def test_allowlist_redaction_keeps_a_shown_identifier_and_redacts_before_text_cut(tmp_path):
    secret = "secret_run_abcdef"
    trace = _Trace(
        tmp_path,
        turns=1,
        events=(
            {"type": "tool_call", "name": secret},
            {"type": "assistant_message", "content": "a" * 30 + secret + "z" * 70},
        ),
    )
    try:
        shown = observation_payload(
            trace.project(pin=pin(view="allowlist", text_chars=32), secrets=(secret,))[("c", "s1")]
        )
        assert shown["turns"][0]["events"][0] == {"type": "tool_call", "name": "[REDACTED]"}
        assert shown["turns"][0]["events"][1]["content"] == "a" * 30 + "[R"
    finally:
        trace.close()


def test_allowlist_limits_do_not_change_derived_grouping_fields(tmp_path):
    trace = _Trace(tmp_path, turns=2, events=({"type": "assistant_message", "content": "a" * 64},))
    try:
        old = inputs(trace)
        narrow = inputs(trace, pin(view="allowlist", turns=1, text_chars=32))
        arguments = {"index": 0, "section": "observation"}
        before = json.loads(old.read_grouping_input(arguments)["content"])
        after = json.loads(narrow.read_grouping_input(arguments)["content"])
        assert before["behavior"] != after["behavior"]
        assert {key: value for key, value in before.items() if key != "behavior"} == {
            key: value for key, value in after.items() if key != "behavior"
        }
    finally:
        trace.close()


def test_full_view_refuses_invalid_turns_before_rendering(tmp_path):
    def malformed(view):
        view["history"]["turns"][0]["events"] = None

    trace = _Trace(tmp_path, turns=1, mangle=malformed)
    try:
        observation = trace.project(pin=pin())[("c", "s1")]
        assert observation.status == "unavailable"
    finally:
        trace.close()
