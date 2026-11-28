import hashlib
import json

import pytest

from evolution_lab.owner import content


def test_mask_credential_keys_and_hidden_reasoning():
    value = {"Authorization": "Bearer x", "api_key": "k", "apiKey": "k", "tokens": 5, "token": "t",
             "nested": [{"password": "p", "note": "ok <think>outer <think>inner</think>outer-secret "
                        "<analysis>outer <analysis>inner</analysis>analysis-secret</analysis> "
                        "<reasoning>outer <reasoning>inner</reasoning>reasoning-secret</reasoning></think> done"}],
             "oracle": "kept: the private flag governs it", "private_context": "kept"}
    masked = content.mask(value)
    assert masked["Authorization"] == masked["api_key"] == masked["apiKey"] == masked["token"] == "[REDACTED]"
    assert masked["tokens"] == 5
    assert masked["nested"][0]["password"] == "[REDACTED]"
    assert masked["nested"][0]["note"] == "ok [REDACTED] done"
    assert masked["oracle"] == "kept: the private flag governs it" and masked["private_context"] == "kept"


def test_mask_reaches_inside_json_strings():
    arguments = json.dumps({"path": "a.md", "api_key": "sk-1"})
    assert json.loads(content.mask({"arguments": arguments})["arguments"]) == {"path": "a.md", "api_key": "[REDACTED]"}
    untouched = json.dumps({"path": "a.md"}, indent=2)
    assert content.mask(untouched) == untouched
    assert content.mask("[not json") == "[not json"


def test_lab_keys_survive_masking():
    # Lab's own field names never match the credential rule: masking never destroys the form.
    keys = ["schema", "operation_id", "measurement_id", "case_id", "sample_id", "source", "disclosure", "masking",
            "execution_state", "trace", "state", "reason", "try", "score_request_id", "retry_ordinal", "tries_total",
            "input", "public", "turns", "index", "message", "content", "fields", "events", "type", "text", "name",
            "arguments", "result", "truncated", "size", "usage", "latency_ms", "outcome", "terminal", "scoring",
            "metrics", "critical_pass", "verdicts", "criterion", "passed", "public_feedback", "private", "oracle",
            "diagnostics", "details", "verdict_notes", "note", "scorer_output", "stdout", "exit_status",
            "attempt_id", "round", "confidence", "editor", "kind", "tool_calls", "changed", "changed_count", "failure",
            "error_class", "text_source", "stderr_tail", "reflection_id", "skipped", "turn", "id", "status", "groups",
            "tool_turns", "file_changes", "command_failures", "last_message", "error", "withheld", "turns_omitted",
            "events_omitted", "looks", "look", "final", "decision", "would_pass_point", "caveats", "focus", "causal",
            "overall", "point", "lower", "upper", "level", "unavailable", "method", "levels", "tolerance", "retained",
            "parent_source", "repeats", "focus_cases", "causal_cases", "calibration"]
    body = {key: "plain" for key in keys}
    assert content.mask(body) == body


def test_clip_text_keeps_head_and_tail():
    text = "a" * 10 + "b" * 10
    clipped, cut = content.clip_text(text, 8)
    assert cut and clipped.startswith("aaaa") and clipped.endswith("bbbb") and "12 characters omitted" in clipped
    assert content.clip_text("short", 8) == ("short", False)


def test_clip_value_and_list():
    value, cut = content.clip_value({"a": ["x" * 20, 1, None]}, 8)
    assert cut and value["a"][1:] == [1, None] and "omitted" in value["a"][0]
    items, omitted = content.clip_list(list(range(10)), 4)
    assert items == [0, 1, 8, 9] and omitted == 6
    assert content.clip_list([1, 2], 4) == ([1, 2], 0)


def test_private_flag():
    assert content.private({"oracle": 1}, False) == {"withheld": "private"}
    assert content.private({"oracle": 1}, True) == {"oracle": 1}


def test_fit_steps_down_until_the_answer_fits(monkeypatch):
    response_max = content.RESPONSE_MAX
    monkeypatch.setattr(content, "RESPONSE_MAX", 2000)
    seen = []

    def build(field_max, list_max):
        seen.append((field_max, list_max))
        text, cut = content.clip_text("x" * 50_000, field_max)
        return {"text": text, "truncated": cut}

    body = content.fit(build)
    assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= 2000 and body["truncated"] is True
    assert seen[0] == (content.FIELD_MAX, content.LIST_MAX) and len(seen) > 1

    # Every string fits even the last tier; only whole-section fallback can fit
    # this actual >1 MiB response. Unicode makes the byte count significant.
    monkeypatch.setattr(content, "RESPONSE_MAX", response_max)
    seen.clear()
    public = {f"field_{index}": "я" * 600 for index in range(1000)}
    envelope = {
        "schema": "lab-sample/1", "operation_id": "op-1", "measurement_id": "m-1",
        "case_id": "case-1", "sample_id": "sample-1", "source": "core_raw",
        "disclosure": "public", "masking": "keys", "execution_state": "finished",
        "trace": {"state": "available"},
        "try": {"score_request_id": "request-1", "retry_ordinal": 0, "tries_total": 1},
        "input": {"public": public}, "turns": [], "terminal": {"outcome": "ok"},
        "scoring": {"state": "admitted", "metrics": {}, "critical_pass": True,
                    "verdicts": [], "public_feedback": None},
        "private": {"withheld": "private"}, "truncated": False,
    }
    assert len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > response_max

    def build_short_fields(field_max, list_max):
        seen.append((field_max, list_max))
        return dict(envelope)

    body = content.fit(build_short_fields)
    assert seen == list(content.TIERS)
    assert body == {**envelope, "input": {"state": "unavailable", "reason": "too_large"}, "truncated": True}
    assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= response_max
    assert envelope["input"] == {"public": public} and envelope["truncated"] is False


CHAT = [
    {"phase": "preflight", "tool": "list_files", "request_style": "chat"},
    {"phase": "model_request", "turn": 0, "request_style": "chat", "message_count": 2, "messages_after": 0,
     "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "Call list_files now; do not edit anything."}],
     "tool_names": ["list_files"]},
    {"phase": "model_response", "turn": 0, "content": "", "usage": {"total_tokens": 9},
     "calls": [{"id": "c0", "name": "list_files", "arguments": "{}", "format": "json"}]},
    {"phase": "model_request", "turn": 1, "messages_after": 0,
     "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "BRIEF: fix the prompt"}],
     "tool_names": ["list_files", "write_file"]},
    {"phase": "model_response", "turn": 1, "content": "I will write.", "usage": {"total_tokens": 9},
     "calls": [{"id": "c1", "name": "write_file", "arguments": "{\"path\":\"a.md\"}", "format": "json"}]},
    {"phase": "tool_call", "turn": 1, "tool": "write_file", "arguments": {"path": "a.md"}, "result": {"ok": True}},
    {"phase": "provider_attempt", "turn": 1, "attempt": 1, "outcome": "succeeded"},
    {"phase": "model_response", "turn": 2, "content": "", "calls": [{"id": "c2", "name": "read_file", "arguments": "{}"}]},
    {"phase": "tool_error", "turn": 2, "tool": "read_file", "error": "no such file"},
    {"phase": "finish_reflection_rejected", "turn": 3, "error": "group 0: no evidence", "arguments": "{}"},
    {"phase": "refused", "error_class": "workspace_changed", "error": "workspace changed"},
    {"phase": "failed", "error_class": "provider", "note": "http 402"},
    {"phase": "something_new"},
    {"no_phase": True},
]


def test_chat_projection_events_in_order():
    projected = content.project_role("chat", CHAT)
    assert projected["tool_calls"] == "recorded" and projected["skipped"] == 2
    types = [e["type"] for e in projected["events"]]
    assert types == ["input", "tool_call", "model_message", "tool_call", "tool_result", "tool_call", "tool_result",
                     "rejected", "rejected", "failed"]
    events = projected["events"]
    assert events[0] == {"type": "input", "text": "BRIEF: fix the prompt"}
    assert events[2] == {"type": "model_message", "turn": 1, "text": "I will write."}
    assert events[4] == {"type": "tool_result", "turn": 1, "id": "c1", "name": "write_file", "text": '{"ok": true}'}
    assert events[6]["error"] is True and events[6]["id"] == "c2"
    assert events[7] == {"type": "rejected", "turn": 3, "reason": "finish_reflection_rejected",
                         "text": "group 0: no evidence"}
    assert events[8] == {"type": "rejected", "turn": None, "reason": "refused", "error_class": "workspace_changed",
                         "text": "workspace changed"}
    assert events[9] == {"type": "failed", "error_class": "provider", "text": "http 402"}
    assert not any("usage" in e for e in events)
    # The retained preflight user is not the opening, including content parts.
    retained = [dict(record) for record in CHAT[:6]]
    retained[3]["messages_after"] = 2
    retained[3]["messages"] = [{"role": "user", "content": [{"type": "text", "text": "BRIEF: parts"}]}]
    assert content.project_role("chat", retained)["events"][0] == {"type": "input", "text": "BRIEF: parts"}


def test_chat_projection_keeps_a_replayed_session():
    projected = content.project_role("chat", CHAT[:6] + CHAT[:6])
    session = content.project_role("chat", CHAT[:6])["events"]
    assert projected["events"] == session + session
    # A replay with no matching model call cannot borrow the previous session's id.
    replay = [CHAT[1], CHAT[3], CHAT[5]]
    projected = content.project_role("chat", CHAT[:6] + replay)
    assert projected["events"][len(session)]["type"] == "input"
    assert projected["events"][-1]["id"] is None


AGENT = [
    {"phase": "request", "call": 0, "model": "m", "system_sha256": "0" * 64, "opening": "OPEN", "timeout_s": 60},
    {"phase": "response", "call": 0, "content": "done", "tool_turns": 7,
     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_turns": 7, "wall_time_ms": 5},
     "diagnostics": {"file_changes": ["a.md"], "command_failures": [{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                     "last_message": "bye", "stderr_tail": "tail", "returncode": 0}},
    {"phase": "self_check", "call": 0, "issues": []},
    {"phase": "published", "changed": ["a.md"]},
    {"phase": "reflected", "status": "reflected", "groups": 2},
]


def test_agent_cli_projection():
    projected = content.project_role("agent_cli", AGENT)
    assert projected["tool_calls"] == "not_recorded" and projected["skipped"] == 0
    assert [e["type"] for e in projected["events"]] == ["input", "model_message", "agent_summary", "published",
                                                        "reflected"]
    assert projected["events"][2] == {"type": "agent_summary", "turn": 0, "tool_turns": 7, "file_changes": ["a.md"],
                                      "command_failures": [{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                                      "last_message": "bye", "stderr_tail": "tail"}


def test_reflector_disclosure_follows_the_table():
    events = content.project_role("chat", CHAT)["events"] + content.project_role("agent_cli", AGENT)["events"]
    hidden = content.disclose_events("reflector", events, False)
    by_type = {}
    for event in hidden:
        by_type.setdefault(event["type"], []).append(event)
    assert by_type["input"][0] == {"type": "input", "withheld": "private"}
    assert by_type["model_message"][0] == {"type": "model_message", "withheld": "private"}
    assert by_type["tool_call"][0]["arguments"] == {"withheld": "private"} and by_type["tool_call"][0]["name"] == "list_files"
    assert by_type["tool_result"][0]["text"] == {"withheld": "private"} and by_type["tool_result"][0]["id"] == "c1"
    assert by_type["rejected"][0] == {"type": "rejected", "turn": 3, "reason": {"withheld": "private"},
                                      "text": {"withheld": "private"}}
    assert by_type["failed"][0] == {"type": "failed", "error_class": "provider", "text": {"withheld": "private"}}
    summary = by_type["agent_summary"][0]
    assert summary["tool_turns"] == 7 and summary["last_message"] == {"withheld": "private"}
    assert by_type["reflected"][0] == {"type": "reflected", "status": "reflected", "groups": 2}
    assert content.disclose_events("reflector", events, True) == events
    assert content.disclose_events("editor", events, False) == events


def test_failure_text_from_the_log():
    assert content.failure_text("chat", CHAT) == ("http 402", "editor_log")
    assert content.failure_text("chat", CHAT[:3]) is None
    assert content.failure_text("chat", CHAT[:-3]) == ("workspace changed", "editor_log")
    assert content.failure_text("agent_cli", AGENT + [{"phase": "exception", "error": "crashed"}]) == (
        "crashed", "editor_log")


# Exercise sample assembly through the real journal and exact-b5 trace reader.
def _sample_trace(root, *, mid="m-r01-a01", state="finished", layout="pack",
                  stdout=b'{"public_feedback":"feedback","details":"private details"}', returncode=7):
    from scorer_trace_pack import scorer_pack
    rid = "a" * 32
    row = {"case_id": "case", "sample_id": "00", "execution_state": "finished"}
    payload = {"case_id": "case", "public": {"question": "hello", "api_key": "secret credential"},
               "oracle": "private oracle", "view": {"history": {"turns": [
                   {"message": {"content": "dialogue <analysis>hidden thought</analysis>"}}]},
                   "terminal": "completed"}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    request = json.dumps({"scorer": "neutral", "trace_id": rid, "input": payload,
                          "input_sha256": digest}).encode()
    if layout == "pack":
        # Deliberately contradict failed journal state: reader status is metadata.
        scorer_pack(root, mid, rid, request=request, stdout=stdout, returncode=returncode,
                    status="completed")
    else:
        directory = root / "scorer-traces" / "scorer_traces" / rid
        directory.mkdir(parents=True)
        (directory / "request.json").write_bytes(request)
        if stdout is not None:
            (directory / "stdout").write_bytes(stdout)
        if layout == "unsafe_stdout":
            outside = root / "private-stream"
            outside.write_bytes(b"secret stream")
            (directory / "stdout").unlink(missing_ok=True)
            (directory / "stdout").symlink_to(outside)
    base = {"case_id": "case", "sample_id": "00", "score_request_id": rid}
    events = []
    if state == "imported":
        events.append(("sample_imported", base))
    elif state != "not_scored":
        events.append(("scoring_started", {**base, "retry_ordinal": 0}))
        if state in ("finished", "failed"):
            events.append(("scoring_" + state, {**base, "metrics": {"score": 1.0}, "critical_pass": True}))
    journal = root / "events.jsonl"
    journal.write_text("".join(json.dumps({"event": event, "payload": data}) + "\n"
                               for event, data in events))
    return row, journal


@pytest.mark.parametrize("state", ["finished", "failed"])
@pytest.mark.parametrize("layout,reason", [("pack", "trace_missing"), ("unsafe_stdout", "unreadable")])
def test_sample_partial_stdout_keeps_verified_dialogue_and_reason(tmp_path, state, layout, reason):
    from evolution_lab.cli import responses
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path, state=state, layout=layout, stdout=None)
    for disclose in (False, True):
        body = sample_view("op", "m-r01-a01", row, tmp_path, journal, disclose)
        responses.validate(body, "lab-sample/1")
        assert body["trace"] == {"state": "unavailable", "reason": reason}
        assert body["input"] == {"public": {"question": "hello", "api_key": "[REDACTED]"}}
        assert body["turns"] == [{"message": {"content": "dialogue [REDACTED]"}}]
        assert body["terminal"] == "completed"
        if state == "finished":
            assert body["scoring"] == {"state": "admitted", "metrics": {"score": 1.0},
                                       "critical_pass": True, "verdicts": [], "public_feedback": None}
        else:
            assert body["scoring"]["state"] == "failed"
            if disclose:
                assert body["private"]["scorer_output"]["stdout"] is None
        if disclose:
            assert body["private"]["oracle"] == "private oracle"
        else:
            assert body["private"] == {"withheld": "private"}
            assert "private oracle" not in json.dumps(body)


@pytest.mark.parametrize("layout,exit_status", [("pack", 7), ("directory", None)])
def test_failed_empty_stdout_is_available_and_exit_status_is_private(tmp_path, layout, exit_status):
    from evolution_lab.cli import responses
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path, state="failed", layout=layout, stdout=b"")
    private = sample_view("op", "m-r01-a01", row, tmp_path, journal, True)
    public = sample_view("op", "m-r01-a01", row, tmp_path, journal, False)
    for body in (private, public):
        responses.validate(body, "lab-sample/1")
        assert body["trace"] == {"state": "available"}
        assert body["scoring"] == {"state": "failed", "metrics": None, "critical_pass": None,
                                   "verdicts": None, "public_feedback": None}
    assert private["private"]["scorer_output"] == {"stdout": "", "exit_status": exit_status}
    assert public["private"] == {"withheld": "private"}
    assert "exit_status" not in json.dumps(public)


@pytest.mark.parametrize("stdout", [b"", b"not JSON"])
def test_admitted_invalid_stdout_retains_journal_metrics(tmp_path, stdout):
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path, stdout=stdout)
    body = sample_view("op", "m-r01-a01", row, tmp_path, journal, True)
    assert body["trace"] == {"state": "unavailable", "reason": "unreadable"}
    assert body["input"] is not None and body["turns"]
    assert body["scoring"]["metrics"] == {"score": 1.0}
    assert body["scoring"]["state"] == "admitted"


@pytest.mark.parametrize("state", ["not_scored", "imported", "incomplete", "broken"])
def test_sample_journal_gate_prevents_trace_disclosure(tmp_path, state):
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path, state=state)
    if state == "broken":
        journal.write_text("broken journal\n")
    body = sample_view("op", "m-r01-a01", row, tmp_path, journal, True)
    assert body["trace"] == {"state": "unavailable", "reason": "unreadable" if state == "broken" else state}
    assert body["input"] is None and body["turns"] == []
    assert body["private"]["oracle"] is None
    assert body["try"] is None


def test_sample_uses_last_completed_try_despite_later_unfinished_try(tmp_path):
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path)
    with journal.open("a") as stream:
        stream.write(json.dumps({"event": "scoring_started", "payload": {
            "case_id": "case", "sample_id": "00", "score_request_id": "b" * 32, "retry_ordinal": 1}}) + "\n")
    body = sample_view("op", "m-r01-a01", row, tmp_path, journal, False)
    assert body["trace"] == {"state": "available"}
    assert body["try"] == {"score_request_id": "a" * 32, "retry_ordinal": 0, "tries_total": 2}
    assert body["scoring"]["public_feedback"] == "feedback"


def test_sample_whole_trace_too_large_hides_input_and_dialogue(monkeypatch, tmp_path):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import sample_view
    row, journal = _sample_trace(tmp_path, stdout=b"x" * 4097)
    # Task2 covers the actual 32 MiB cap; isolate projection with a small cap.
    monkeypatch.setattr(core_raw, "FILE_MAX", 4096)
    body = sample_view("op", "m-r01-a01", row, tmp_path, journal, True)
    assert body["trace"] == {"state": "unavailable", "reason": "too_large"}
    assert body["input"] is None and body["turns"] == [] and body["terminal"] is None
    assert body["private"]["oracle"] is None
    assert body["scoring"]["metrics"] == {"score": 1.0}


def test_b12_reflector_result_correction_keeps_existing_event_and_privacy():
    records = [{"phase": "finish_reflection_rejected", "turn": 2,
                "error": "affected_cases names an unknown case",
                "correction": "groups[0].affected_cases[0]: expected case_id",
                "arguments": {"groups": [{"affected_cases": ["rejected-value"]}]}}]
    projected = content.project_role("chat", records)
    assert projected["skipped"] == 0
    event, = projected["events"]
    assert event == {"type": "rejected", "turn": 2, "reason": "finish_reflection_rejected",
                     "text": "affected_cases names an unknown case; groups[0].affected_cases[0]: expected case_id"}
    assert "rejected-value" not in json.dumps(event)
    assert content.disclose_events("reflector", [event], False) == [
        {**event, "reason": {"withheld": "private"}, "text": {"withheld": "private"}}]


def test_b12_editor_plan_corrections_and_normalizations_keep_actual_tool_results():
    records = [
        {"phase": "model_response", "turn": 1, "content": "", "calls": [
            {"id": "c1", "name": "record_plan", "arguments": "{}"}]},
        {"phase": "plan_correction_requested", "turn": 1, "category": "plan_delete_missing",
         "error": "cannot delete a missing file"},
        {"phase": "model_response", "turn": 2, "content": "", "calls": [
            {"id": "c2", "name": "record_plan", "arguments": "{}"}]},
        {"phase": "plan_normalized", "turn": 2, "path": "notes.md", "declared": "create", "recorded": "update"},
        {"phase": "tool_call", "turn": 2, "tool": "record_plan", "result": {"ok": True}},
    ]
    projected = content.project_role("chat", records)
    assert projected["skipped"] == 0
    assert projected["events"][1] == {
        "type": "rejected", "turn": 1, "reason": "plan_correction_requested", "text": "cannot delete a missing file"}
    assert projected["events"][3] == {
        "type": "tool_result", "turn": 2, "id": "c2", "name": "record_plan",
        "text": 'plan_normalized: {"path": "notes.md", "declared": "create", "recorded": "update"}'}
    assert projected["events"][4] == {
        "type": "tool_result", "turn": 2, "id": "c2", "name": "record_plan", "text": '{"ok": true}'}


def _b13_trace_records():
    # Neither page is a complete JSON document. Page two starts inside both a
    # credential value and hidden-reasoning block; key/tag masking cannot help.
    pages = ['{"password": "boundary-password',
             '-tail", "message": "<think>boundary-reasoning',
             '-tail</think>"}']
    records = []
    offset = 0
    for turn, page in enumerate(pages, 1):
        end = offset + len(page)
        records.extend([
            {"phase": "model_response", "turn": turn, "calls": [
                {"id": f"trace-{turn}", "name": "read_trace",
                 "arguments": json.dumps({"evidence_id": "case:00", "offset": offset})}]},
            {"phase": "tool_call", "turn": turn, "tool": "read_trace",
             "arguments": {"evidence_id": "case:00", "offset": offset},
             "result": {"evidence_id": "case:00", "content": page, "offset": offset,
                        "total_characters": sum(map(len, pages)),
                        "next_offset": end if turn < len(pages) else None, "content_digest": "a" * 64}},
        ])
        offset = end
    records.extend([
        {"phase": "tool_error", "turn": 4, "tool": "read_trace", "error": "bad_offset",
         "arguments": {"evidence_id": "error-argument-secret"}},
        {"phase": "tool_error", "turn": 5, "tool": "read_trace",
         "error": "bad_offset: error-text-secret", "arguments": {"evidence_id": "error-argument-secret"}},
        {"event": "check_shadowed", "check": "seal_oracle_leak", "count": 1},
    ])
    return records


def test_b13_read_trace_projection_keeps_only_metadata_and_error_code():
    projected = content.project_role("chat", _b13_trace_records())
    results = [event for event in projected["events"] if event["type"] == "tool_result"]
    metadata = json.loads(results[0]["text"])
    assert metadata == {"evidence_id": "case:00", "offset": 0, "total_characters": 91,
                        "next_offset": 31, "content_digest": "a" * 64}
    assert all(set(json.loads(event["text"])) == {
        "evidence_id", "offset", "total_characters", "next_offset", "content_digest"} for event in results[:3])
    assert results[3]["text"] == "bad_offset" and results[3]["error"] is True
    assert results[4]["text"] == "" and results[4]["error"] is True
    assert projected["skipped"] == 1
    serialized = json.dumps(projected)
    for secret in ("boundary-password", "-tail", "boundary-reasoning", "error-text-secret", "error-argument-secret"):
        assert secret not in serialized
    assert [event["name"] for event in projected["events"] if event["type"] == "tool_call"] == ["read_trace"] * 3


def _b13_character_page_records():
    records = _b13_trace_records()
    # Split credential keys as well as values, and split the opening hidden tag.
    pages = ['{"pass', 'word":"boundary-password","api_',
             'key":"boundary-api-key","message":"<thi',
             'nk>boundary-reasoning', '-tail</think>"}']
    for tool, editor_shape in (("read_evidence", False), ("read_document", False),
                               ("read_history", False), ("future_page_reader", True)):
        offset = 0
        for page in pages:
            end = offset + len(page)
            result = {"content": page, "next_offset": end if end < 112 else None,
                      "extension": {"text": "extension-secret"}}
            if editor_shape:
                result.update(id="doc:00", total_chars=112)
            else:
                result.update(evidence_id="case:00", offset=offset, total_characters=112,
                              content_digest="b" * 64)
                if tool == "read_document":
                    result["document_id"] = result.pop("evidence_id")
            records.append({"phase": "tool_call", "tool": tool, "result": result})
            offset = end
        records.extend([
            {"phase": "tool_error", "tool": tool, "error": "bad_offset"},
            {"phase": "tool_error", "tool": tool, "error": "bad_offset: error-text-secret"},
        ] if tool != "future_page_reader" else [])
    return records


def test_b13_character_pages_keep_only_metadata_before_disclosure():
    events = content.project_role("chat", _b13_character_page_records())["events"]
    for role in ("editor", "reflector"):
        for disclose in (False, True):
            disclosed = content.disclose_events(role, events, disclose)
            serialized = json.dumps(content.mask(disclosed))
            for secret in ("boundary-password", "boundary-api-key", "boundary-reasoning", "-tail",
                           "extension-secret", "error-text-secret", "error-argument-secret"):
                assert secret not in serialized
            results = [e for e in disclosed if e["type"] == "tool_result"]
            if role == "reflector" and not disclose:
                assert all(e["text"] == {"withheld": "private"} for e in results)
                continue
            evidence = [e for e in results if e["name"] == "read_evidence"]
            assert json.loads(evidence[0]["text"]) == {
                "evidence_id": "case:00", "offset": 0, "total_characters": 112,
                "next_offset": 6, "content_digest": "b" * 64}
            document = [e for e in results if e["name"] == "read_document"]
            assert json.loads(document[0]["text"]) == {
                "document_id": "case:00", "offset": 0, "total_characters": 112,
                "next_offset": 6, "content_digest": "b" * 64}
            assert json.loads(document[4]["text"])["next_offset"] is None
            history = next(e for e in results if e["name"] == "read_history")
            assert json.loads(history["text"])["content_digest"] == "b" * 64
            future = [e for e in results if e["name"] == "future_page_reader"]
            assert json.loads(future[0]["text"]) == {"id": "doc:00", "total_chars": 112, "next_offset": 6}
            assert json.loads(future[-1]["text"])["next_offset"] is None
            assert evidence[-2]["text"] == "bad_offset" and evidence[-1]["text"] == ""
            assert document[-2]["text"] == "bad_offset" and document[-1]["text"] == ""


@pytest.mark.parametrize("tool", ["read_trace", "read_evidence", "read_document", "read_history"])
def test_known_character_readers_fail_closed_on_unexpected_results(tool):
    records = [{"phase": "tool_call", "tool": tool, "result": result} for result in (
        "raw-secret", ["list-secret"], None, {"content": "partial-secret", "extra": "extension-secret"},
        {"id": {"text": "nested-secret"}, "offset": "offset-secret", "total_chars": ["length-secret"],
         "next_offset": True, "content_digest": {"text": "digest-secret"}},
    )]
    assert [e["text"] for e in content.project_role("chat", records)["events"]] == ["{}"] * 5


def test_nonpaged_tool_results_and_errors_keep_their_existing_projection():
    records = [{"phase": "tool_call", "tool": "read_file", "result": {"content": "ordinary file"}},
               {"phase": "tool_call", "tool": "list_evidence",
                "result": {"records": [{"id": "r1"}], "next_offset": None, "total": 1}},
               {"phase": "tool_error", "tool": "read_file", "error": "file does not exist"}]
    events = content.project_role("chat", records)["events"]
    assert [e["text"] for e in events] == [
        '{"content": "ordinary file"}', '{"records": [{"id": "r1"}], "next_offset": null, "total": 1}',
        "file does not exist"]


@pytest.mark.parametrize("version,opening", [
    ("edit_brief/6", 'Writable paths: prompt.md\nCatalog: 1 records; read "summary" first, then the cases you plan to address.'),
    ("edit_brief/7", 'Writable paths: prompt.md\nCatalog: 1 records; read "summary" first, then the cases you plan to address.'),
    ("reflect_brief/6", "Investigate the supplied focus and read any baseline case through list_evidence and read_evidence. "
     "Use explicit offsets to continue; omitted offsets always start at zero. "
     "Cite observations for every affected case; supporting citations do not expand a claim."),
])
def test_b13_opening_reconstruction_skips_preflight(version, opening):
    # Exact opening excerpts: b13 editor_brief.render_opening and
    # reflect_brief.render_reflect_opening. Real complete logs belong to Task7.
    records = CHAT[:3] + [{"phase": "model_request", "turn": 1, "messages_after": 2,
        "messages": [{"role": "user", "content": opening}],
        "tool_names": ["list_files", "read_trace", "read_evidence"]}]
    projected = content.project_role("chat", records)
    assert projected["events"][0] == {"type": "input", "text": opening}, version


def test_provider_refusal_is_private_service_record():
    sentinel = "PRIVATE_PROVIDER_REFUSAL_MESSAGE"
    records = [{"phase": "provider_refused", "request_style": "modern_single", "turn": 1,
                "attempt": 1, "http_status": 200, "error_code": "unsupported",
                "provider_name": "example", "message": sentinel}]
    projected = content.project_role("chat", records)
    assert projected["events"] == []
    assert projected["skipped"] == 0
    assert sentinel not in json.dumps(projected)
    assert content.failure_text("chat", records) is None
