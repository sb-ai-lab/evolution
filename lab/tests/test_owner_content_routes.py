"""Task 5's shared Owner/service/CLI content contract and bounded forms."""
import copy
import json
from urllib.parse import quote

import pytest

from evolution_lab.cli import responses
from test_owner_measurements import finished_campaign, service_cli_with_finished_campaign  # noqa: F401

pytestmark = pytest.mark.slow


def _first_sample(route, operation):
    listed = route(f"/v1/operations/{operation}/measurements")
    mid = next(m["measurement_id"] for m in listed["measurements"] if m.get("samples_total", 1))
    detail = route(f"/v1/operations/{operation}/measurements/{mid}")
    sample = detail["measurement"]["samples"][0]
    return mid, sample["case_id"], sample["sample_id"]


def test_sample_route_cli_and_schema(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    mid, case, sample = _first_sample(route, operation)
    path = f"/v1/operations/{operation}/measurements/{mid}/cases/{quote(case, safe='')}/samples/{quote(sample, safe='')}"
    public = route(path)
    assert public == cli("sample", operation, mid, case, sample)[-1]
    responses.validate(public, "lab-sample/1")
    assert public["source"] == "core_raw" and public["disclosure"] == "public" and public["masking"] == "keys"
    assert public["private"] == {"withheld": "private"} and public["truncation"] == []
    assert public == route.run(route.owner.sample(route.ref, mid, case, sample))
    disclosed = route(path + "?disclose=private")
    assert disclosed == cli("sample", operation, mid, case, sample, "--disclose-private")[-1]
    assert disclosed["disclosure"] == "private"
    assert set(disclosed["private"]) >= {"oracle", "diagnostics", "details", "verdict_notes"}


def test_attempt_and_reflection_routes(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    view = route(f"/v1/operations/{operation}/campaign")
    attempt = view["rounds"][0]["attempts"][0]["attempt_id"]
    reflection = next(r["reflection"]["reflection_id"] for r in view["rounds"] if r.get("reflection"))
    for command, identifier, plural, schema in (("attempt", attempt, "attempts", "lab-attempt/1"),
                                                ("reflection", reflection, "reflections", "lab-reflection/1")):
        path = f"/v1/operations/{operation}/{plural}/{identifier}"
        body = route(path)
        assert body == cli(command, operation, identifier)[-1]
        assert body == route.run(getattr(route.owner, command)(route.ref, identifier))
        responses.validate(body, schema)
        disclosed = route(path + "?disclose=private")
        assert disclosed == cli(command, operation, identifier, "--disclose-private")[-1]
    a = route(f"/v1/operations/{operation}/attempts/{attempt}")
    assert a["editor"]["kind"] == "subprocess" and a["editor"]["events"] == []
    x = route(f"/v1/operations/{operation}/reflections/{reflection}")
    assert x["stderr_tail"] in ({"withheld": "private"}, None)


@pytest.mark.parametrize("tail", ["/attempts/r99-a99", "/reflections/r99-x99"])
def test_unknown_ids_are_not_found(service_cli_with_finished_campaign, tail):
    _, route, operation = service_cli_with_finished_campaign
    assert route(f"/v1/operations/{operation}{tail}", status=404)["error"]["code"] == "not_found"


def test_failed_scoring_trace_and_private_stdout(monkeypatch, tmp_path):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import sample_view
    stdout = json.dumps({"verdicts": [{"criterion": "c", "passed": True}], "public_feedback": "invalid result"})
    monkeypatch.setattr(core_raw, "scoring_of", lambda *a: {"state": "failed", "score_request_id": "s1",
                        "retry_ordinal": 0, "tries_total": 1, "metrics": {"x": 1}, "critical_pass": True})
    monkeypatch.setattr(core_raw, "scorer_trace", lambda *a, **k: {"input": {"public": "hello", "oracle": "oracle",
                        "view": {"history": {"turns": [{"message": {"content": "dialogue"}}]}}}, "stdout": stdout, "output_reason": None, "returncode": None})
    row = {"case_id": "case", "sample_id": "00", "execution_state": "finished"}
    for disclose in (False, True):
        body = sample_view("op", "m", row, tmp_path, tmp_path / "events.jsonl", disclose)
        responses.validate(body, "lab-sample/1")
        assert body["trace"] == {"state": "available"} and body["turns"][0]["message"]["content"] == "dialogue"
        assert body["scoring"] == {"state": "failed", "metrics": None, "critical_pass": None,
                                   "verdicts": None, "public_feedback": None}
        if disclose:
            assert body["private"]["scorer_output"]["stdout"] == stdout
        else:
            assert body["private"] == {"withheld": "private"} and "invalid result" not in json.dumps(body)


def _forms(monkeypatch, tmp_path, text="hello"):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import sample_view, attempt_view, reflection_view
    monkeypatch.setattr(core_raw, "scoring_of", lambda *a: {"state": "finished", "score_request_id": "s1",
                        "retry_ordinal": 0, "tries_total": 1, "metrics": {"x": 1}, "critical_pass": True})
    monkeypatch.setattr(core_raw, "scorer_trace", lambda *a, **k: {"input": {"public": {"a/b~c": text}, "oracle": text,
                        "view": {"history": {"turns": [{"message": {"content": text}}]}}},
                        "stdout": json.dumps({"public_feedback": text, "details": text}),
                        "output_reason": None, "returncode": None})
    monkeypatch.setattr(core_raw, "role_log", lambda *a: ([{"phase": "model_request", "messages": [
                        {"role": "user", "content": text}]}, {"phase": "model_response", "turn": 1, "content": text}], False))
    monkeypatch.setattr(core_raw, "role_stderr", lambda *a: text)
    facts = {"closed": {"r01-a01"}, "reflections": {"r01-x01": {"event": "reflection_finished", "status": "ok", "groups": [{"root_cause": text}]}},
             "changed": {"r01-a01": 2}}
    row = {"case_id": "case", "sample_id": "00", "execution_state": "finished"}
    confidence = {"method": "m", "confidence": {}, "evaluated": [{"look": 1, "focus": {"point": 1, "exact": True}},
                                                                 {"look": 2, "focus": None}]}
    return [sample_view("op", "m", row, tmp_path, tmp_path / "events.jsonl", True),
            attempt_view("op", "r01-a01", 1, confidence, None, "chat", tmp_path, facts, False),
            reflection_view("op", "r01-x01", 1, "chat", tmp_path, facts, False)]


def test_content_forms_closed_privacy_and_section_unions(monkeypatch, tmp_path):
    from evolution_lab.owner import content
    forms = _forms(monkeypatch, tmp_path)
    assert len(forms[1]["confidence"]["looks"]) == 2
    assert "exact" not in forms[1]["confidence"]["looks"][0]["focus"]
    assert forms[1]["editor"]["changed"] is None and forms[1]["editor"]["changed_count"] == 2
    assert forms[2]["events"][0] == {"type": "input", "withheld": "private"}
    for body in forms:
        responses.validate(body, body["schema"])
        extra = dict(body, unexpected=True)
        with pytest.raises(ValueError):
            responses.validate(extra, body["schema"])
        for path in content._SECTION_PATHS[body["schema"]]:
            variant = copy.deepcopy(body)
            parent = variant
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = {"state": "unavailable", "reason": "too_large"}
            variant["truncated"] = True
            responses.validate(variant, body["schema"])
            parent[path[-1]]["extra"] = True
            with pytest.raises(ValueError):
                responses.validate(variant, body["schema"])
    bad = copy.deepcopy(forms[2]); bad["events"][0] = {"type": "input", "text": "private"}
    with pytest.raises(ValueError):
        responses.validate(bad, bad["schema"])
    bad = copy.deepcopy(forms[0]); bad["disclosure"] = "public"
    with pytest.raises(ValueError):
        responses.validate(bad, bad["schema"])


def test_content_metadata_and_hard_cap_keep_valid_forms(monkeypatch, tmp_path):
    from evolution_lab.owner import content
    forms = _forms(monkeypatch, tmp_path, "я" * (content.FIELD_MAX + 1))
    sample = forms[0]
    assert {"path": "/input/public/a~1b~0c", "size": content.FIELD_MAX + 1} in sample["truncation"]
    assert forms[2]["truncation"] == []  # withheld text has no metadata
    for body in forms:
        assert len(json.dumps(body, ensure_ascii=False).encode()) <= content.RESPONSE_MAX
        assert body["truncated"] == bool(body["truncation"])
        responses.validate(body, body["schema"])
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import attempt_view, reflection_view
    # A hidden block crossing the tail boundary must be masked in the full stderr first.
    raw = "prefix" + "a" * 20000 + "<analysis>" + "private" * 4000 + "</analysis>tail"
    monkeypatch.setattr(core_raw, "role_stderr", lambda *a: raw)
    facts = {"closed": {"r01-a01"}, "reflections": {"r01-x01": {"event": "reflection_finished"}}, "changed": {}}
    a = attempt_view("op", "r01-a01", 1, None, {"stage": "editor", "role": "editor", "error_class": "runtime", "code": "failed"},
                     "subprocess", tmp_path, facts, False)
    masked = content.mask(raw)
    assert a["editor"]["stderr_tail"] == masked[-content.FIELD_MAX:]
    assert a["editor"]["failure"]["text"] == a["editor"]["stderr_tail"]
    assert {"path": "/editor/stderr_tail", "size": len(masked)} in a["truncation"]
    assert {"path": "/editor/failure/text", "size": len(masked)} in a["truncation"]
    responses.validate(a, a["schema"])
    x = reflection_view("op", "r01-x01", 1, "subprocess", tmp_path, facts, False)
    assert x["stderr_tail"] == {"withheld": "private"} and x["truncation"] == []
    private = reflection_view("op", "r01-x01", 1, "subprocess", tmp_path, facts, True)
    assert private["stderr_tail"] == masked[-content.FIELD_MAX:]
    assert private["truncation"] == [{"path": "/stderr_tail", "size": len(masked)}]
    responses.validate(private, private["schema"])
    # Many individually small fields still require section fallback; metadata disappears with its section.
    body = copy.deepcopy(sample)
    body["input"] = {"public": {str(i): "a" * 1000 for i in range(1200)}}
    body["truncation"] = [{"path": "/input/public/0", "size": 20000}]
    fitted = content.fit(lambda *a: body)
    assert fitted["input"] == {"state": "unavailable", "reason": "too_large"}
    assert fitted["truncation"] == [] and fitted["operation_id"] == "op"
    assert fitted["disclosure"] == "private" and fitted["masking"] == "keys"
    responses.validate(fitted, fitted["schema"])

    for original in forms[1:]:
        body = copy.deepcopy(original)
        part = body["editor"] if body["schema"] == "lab-attempt/1" else body
        part["events"] = [{"type": "model_message", "turn": i, "text": "b" * 1000} for i in range(1200)]
        pointer = "/editor/events" if body["schema"] == "lab-attempt/1" else "/events"
        body["truncation"] = [{"path": pointer + "/0/text", "size": 20000}]
        fitted = content.fit(lambda *a: body)
        part = fitted["editor"] if "editor" in fitted else fitted
        assert part["events"] == {"state": "unavailable", "reason": "too_large"}
        assert fitted["truncation"] == []
        assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= content.RESPONSE_MAX
        responses.validate(fitted, fitted["schema"])


@pytest.mark.parametrize("kind", ["chat", "agent_cli"])
@pytest.mark.parametrize("terminal", ["measurement", "failed", "no_text"])
def test_attempt_failure_text_uses_terminal_editor_session(monkeypatch, tmp_path, kind, terminal):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.campaign_views import failure_of
    from evolution_lab.owner.sample_view import attempt_view

    def request(text):
        return ({"phase": "model_request", "messages_after": 0,
                 "messages": [{"role": "user", "content": text}]} if kind == "chat" else
                {"phase": "request", "call": 0, "opening": text})

    records = [request("first session"), {"phase": "failed", "note": "recovered provider failure"},
               request("resumed session")]
    if terminal == "measurement":
        records.append({"phase": "published", "changed": ["a.md"]})
    elif terminal == "failed":
        records.append({"phase": "failed", "note": "terminal Editor failure"})
    monkeypatch.setattr(core_raw, "role_log", lambda *a: (records, False))
    failure = failure_of("insufficient_evidence", "degenerate_interval", {}) if terminal == "measurement" else (
        failure_of("abandoned", "editor_failed", {"editor_error": "reported", "editor_reason": "exception"}))
    body = attempt_view("op", "r01-a01", 1, None, failure, kind, tmp_path,
                        {"closed": {"r01-a01"}, "changed": {}}, False)
    responses.validate(body, "lab-attempt/1")
    editor = body["editor"]
    assert editor["events"][0]["text"] == "first session"
    assert any(e.get("text") == "recovered provider failure" for e in editor["events"])
    assert any(e.get("text") == "resumed session" for e in editor["events"])
    if terminal == "failed":
        assert editor["failure"]["text"] == "terminal Editor failure"
        assert editor["failure"]["text_source"] == "editor_log"
    else:
        assert "text" not in editor["failure"] and "text_source" not in editor["failure"]


def test_measurement_failure_does_not_use_successful_subprocess_stderr(monkeypatch, tmp_path):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.campaign_views import failure_of
    from evolution_lab.owner.sample_view import attempt_view
    monkeypatch.setattr(core_raw, "role_stderr", lambda *a: "ordinary successful Editor stderr")
    failure = failure_of("insufficient_evidence", "degenerate_interval", {})
    body = attempt_view("op", "r01-a01", 1, None, failure, "subprocess", tmp_path,
                        {"closed": {"r01-a01"}, "changed": {"r01-a01": 1}}, False)
    responses.validate(body, "lab-attempt/1")
    assert body["editor"]["stderr_tail"] == "ordinary successful Editor stderr"
    assert "text" not in body["editor"]["failure"] and "text_source" not in body["editor"]["failure"]


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
def test_owner_sample_resolves_pack_root_and_measurement_from_projection(tmp_path, kind):
    import asyncio
    from types import SimpleNamespace
    from evolution_lab.owner import OwnerError, ResourceRef
    from evolution_lab.owner.measurements import measurement_rows
    from evolution_lab.owner.readers import ReaderSupport
    from test_owner_content import _sample_trace

    operation = "operation-1"
    mid = "c-journal-run" if kind == "evaluation" else "m-r03-a02"
    run = tmp_path / "artifacts" / operation / operation
    root = run / "measurement" if kind == "evaluation" else run
    root.mkdir(parents=True)
    row, journal = _sample_trace(root, mid=mid)
    core = ({"run_id": mid, "samples": [row]} if kind == "evaluation" else {
        "measurements": [{"measurement_id": mid, "locator": journal.relative_to(run).as_posix(),
                          "status": {"samples": [row]}}]})

    class ProjectionReader(ReaderSupport):
        state_directory = tmp_path
        _result_io = SimpleNamespace(acquire_read=lambda *args: None)

        def _assert_loop(self):
            pass

        async def _readable_status(self, ref):
            return SimpleNamespace(id=operation, kind=kind, prepared_digest="d" * 64), core

        async def _campaign_plan(self, digest):
            return SimpleNamespace(metric_primary=None)

        async def _rows_of(self, status, projection, metric_primary, *, detail=None):
            return measurement_rows(projection, status.kind, {}, metric_primary, detail=detail)

        async def _result_work(self, work, lease):
            return await work()

        async def _result_thread(self, func, *args):
            return func(*args)

    async def scenario():
        reader = ProjectionReader()
        ref = ResourceRef("operation", operation)
        body = await reader.sample(ref, mid, "case", "00")
        responses.validate(body, "lab-sample/1")
        assert body["measurement_id"] == mid
        assert body["trace"] == {"state": "available"}
        assert body["input"]["public"]["question"] == "hello"
        assert body["scoring"]["public_feedback"] == "feedback"
        # An operation id or arbitrary URL id never selects a pack by guessing.
        for unknown in (operation, "m-unknown"):
            with pytest.raises(OwnerError) as caught:
                await reader.sample(ref, unknown, "case", "00", disclose=True)
            assert caught.value.code == "not_found"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["chat", "agent_cli", "subprocess"])
@pytest.mark.parametrize("disclose", [False, True])
@pytest.mark.parametrize("detail,text", [
    ({"category": "not_a_case_id", "path": "groups[0].affected_cases[0]"},
     "not_a_case_id: groups[0].affected_cases[0]"),
    ({"category": "protocol"}, "protocol"),
])
def test_b12_reflection_failure_detail_uses_existing_private_event(monkeypatch, tmp_path, kind, disclose, detail, text):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import reflection_view
    monkeypatch.setattr(core_raw, "role_log", lambda *args: ([
        {"phase": "failed", "error_class": "turns_exhausted", "note": "role turn limit reached"}], False))
    monkeypatch.setattr(core_raw, "role_stderr", lambda *args: "role stderr")
    terminal = {"event": "reflection_failed", "round": 1, "reflection_id": "r01-x01",
                "error_class": "reported", "reason": "turns_exhausted", "detail": detail}
    facts = {"closed": set(), "reflections": {"r01-x01": terminal}, "changed": {}}
    body = reflection_view("op", "r01-x01", 1, kind, tmp_path, facts, disclose)
    assert body["state"] == "available"
    assert body["reason"] == ("not_recorded" if kind == "subprocess" else None)
    assert body["events"][-1] == {"type": "failed", "error_class": "reported",
                                  "text": text if disclose else {"withheld": "private"}}
    responses.validate(body, "lab-reflection/1")


def test_b12_reflection_detail_preserves_unavailable_log(monkeypatch, tmp_path):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.sample_view import reflection_view
    def missing(*args):
        raise core_raw.CoreRawUnavailable("missing")
    monkeypatch.setattr(core_raw, "role_log", missing)
    facts = {"closed": set(), "changed": {}, "reflections": {"r01-x01": {
        "event": "reflection_failed", "error_class": "reported", "reason": "invalid_result",
        "detail": {"category": "protocol"}}}}
    body = reflection_view("op", "r01-x01", 1, "chat", tmp_path, facts, True)
    assert body["state"] == "unavailable" and body["reason"] == "log_missing" and body["events"] == []
    responses.validate(body, "lab-reflection/1")


def test_b13_character_pages_never_reach_public_or_private_routes(service_cli_with_finished_campaign, monkeypatch):
    from evolution_lab.owner import core_raw
    from test_owner_content import _b13_character_page_records

    cli, route, operation = service_cli_with_finished_campaign
    view = route(f"/v1/operations/{operation}/campaign")
    attempt = view["rounds"][0]["attempts"][0]["attempt_id"]
    reflection = next(r["reflection"]["reflection_id"] for r in view["rounds"] if r.get("reflection"))
    monkeypatch.setattr(core_raw, "role_log", lambda *args: (_b13_character_page_records(), False))
    # Feed chat kinds at the existing view-builder seam; the Owner and routes stay real.
    from evolution_lab.owner import sample_view
    attempt_view, reflection_view = sample_view.attempt_view, sample_view.reflection_view
    monkeypatch.setattr(sample_view, "attempt_view", lambda op, aid, rn, confidence, failure, kind, *args:
        attempt_view(op, aid, rn, confidence, failure, "chat", *args))
    monkeypatch.setattr(sample_view, "reflection_view", lambda op, rid, rn, kind, *args:
        reflection_view(op, rid, rn, "chat", *args))
    for command, identifier, plural, schema in (
            ("attempt", attempt, "attempts", "lab-attempt/1"),
            ("reflection", reflection, "reflections", "lab-reflection/1")):
        for disclose in (False, True):
            path = f"/v1/operations/{operation}/{plural}/{identifier}"
            body = route(path + ("?disclose=private" if disclose else ""))
            responses.validate(body, schema)
            argv = ("--disclose-private",) if disclose else ()
            assert body == cli(command, operation, identifier, *argv)[-1]
            assert body == route.run(getattr(route.owner, command)(route.ref, identifier, disclose=disclose))
            part = body["editor"] if command == "attempt" else body
            assert part["state"] == "available"
            results = [event for event in part["events"] if event["type"] == "tool_result"]
            if command == "attempt" or disclose:
                assert json.loads(results[0]["text"])["evidence_id"] == "case:00"
                for tool in ("read_trace", "read_evidence", "read_document", "read_history"):
                    named = [event for event in results if event["name"] == tool]
                    assert named[-2]["text"] == "bad_offset" and named[-1]["text"] == ""
                assert json.loads(next(e["text"] for e in results if e["name"] == "read_document")) == {
                    "document_id": "case:00", "offset": 0, "total_characters": 112,
                    "next_offset": 6, "content_digest": "b" * 64}
                assert all("content" not in json.loads(e["text"]) for e in results if not e.get("error"))
            else:
                assert results[0]["text"] == {"withheld": "private"}
            serialized = json.dumps(body)
            for secret in ("boundary-password", "boundary-api-key", "-tail", "boundary-reasoning",
                           "extension-secret", "error-text-secret", "error-argument-secret"):
                assert secret not in serialized
            if command == "reflection":
                assert body["skipped"] == 1
