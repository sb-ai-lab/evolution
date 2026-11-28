"""Shared suspension transport contract; synthetic Owner state, no experiments."""
import asyncio
from copy import deepcopy
import io
import json

import httpx
import pytest

from evolution_lab.cli import main, responses
from evolution_lab.cli.client import ServiceClient
from evolution_lab.cli.output import write_value
from evolution_lab.owner import Owner, ResourceRef, SuspensionEvidenceResult, core_raw
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from test_owner_delete import register, REF


SCHEMA = "lab-suspension-evidence/1"
PATH = f"/v1/operations/{REF.id}/suspensions"


def history():
    ids = {"measurement_id": "m1", "case_id": "case", "sample_id": "sample", "retry_ordinal": 2}
    metadata = {"count": 300, "rows_digest": "a" * 64, "truncated": True}
    forms = [
        ("fault_rows", metadata, [dict(ids, reason="future_reason", code=None),
                                  dict(ids, sample_id="sample2", reason="future_reason2", code="future_code")]),
        ("final_rows", metadata, [dict(ids, owner="future_owner")]),
        ("champion_rows", dict(metadata, measurement_id="m1"), [dict(ids, owner="target")]),
        ("threshold", {"crossing": {"owner": "target", "kind": "future_kind", "count": 2,
            "denominator": 3, "max_count": 1, "max_rate": "0.5"}, "window_start": None}, None),
        ("stage_record", {"seq": 2, "operation_id": None,
            "prefix": "measurements/m1/events.jsonl#bytes=0:129"}, None),
    ]
    return [{"seq": i + 3, "reason": "future_pause", "owner": "future_pause_owner",
             "evidence_kind": kind, "evidence": deepcopy(evidence), "rows": deepcopy(rows)}
            for i, (kind, evidence, rows) in enumerate(forms)]


def payload(rows=None, *, reason=None):
    return SuspensionEvidenceResult(REF.id, "available" if reason is None else "unavailable",
                                    rows if reason is None else None, reason).to_payload()


@pytest.fixture
async def service(tmp_path):
    async with Owner.open(tmp_path / "state") as owner:
        runtime = ServiceRuntime(ServiceConfig("/tmp/lab-suspensions.sock", tmp_path / "state"))
        runtime.owner, runtime._admission_open = owner, True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                     base_url="http://lab") as client:
            yield owner, runtime, client


async def cli(client, *args, machine=True):
    loop = asyncio.get_running_loop()

    async def forward(request):
        response = await client.request(request.method, request.url.raw_path.decode(),
                                        headers=request.headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    transport = httpx.MockTransport(lambda request: asyncio.run_coroutine_threadsafe(
        forward(request), loop).result(timeout=10))
    stdout, stderr = io.StringIO(), io.StringIO()
    # Exercise the documented suffix spelling as well as the ordinary CLI client/validator.
    argv = ["--socket", "/tmp/lab-suspensions.sock", "suspensions", REF.id, *args]
    if machine:
        argv.append("--json")
    code = await asyncio.to_thread(main, argv, stdout=stdout, stderr=stderr,
        client_factory=lambda **kwargs: ServiceClient(transport=transport, **kwargs))
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("rows", [[], history()])
async def test_python_http_cli_same_closed_history(service, monkeypatch, rows):
    owner, _, client = service
    await register(owner)
    monkeypatch.setattr(core_raw, "campaign_suspensions", lambda run: rows)
    expected = (await owner.suspensions(REF)).to_payload()
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.json() == expected == payload(rows)
    code, stdout, stderr = await cli(client)
    assert code == 0, stderr
    assert json.loads(stdout) == expected
    assert "diagnostics" not in stdout and "oracle" not in stdout
    code, text, stderr = await cli(client, machine=False)
    assert code == 0, stderr
    if not rows:
        assert "no suspensions" in text
    else:
        for token in ("seq=3", "future_pause", "future_pause_owner", "fault_rows", "count=300",
                      "truncated=true", "sample2", "code=null", "future_reason2", "future_code",
                      "future_owner", "champion_rows", "max_rate", "0.5", "window_start", "null",
                      "operation_id", "prefix", "measurements/m1/events.jsonl#bytes=0:129"):
            assert token in text
        # Every returned row appears, even when count exceeds the returned rows.
        assert text.count("retry_ordinal=2") == 4


@pytest.mark.parametrize("reason", ["core_raw_unsupported", "campaign_family_unsupported",
    "campaign_missing", "campaign_unreadable", "unsupported_schema"])
async def test_nullable_unavailable_is_200_and_shared(service, monkeypatch, reason):
    owner, _, client = service
    await register(owner)
    def unavailable(run):
        raise core_raw.CoreRawUnavailable(reason)
    monkeypatch.setattr(core_raw, "campaign_suspensions", unavailable)
    response = await client.get(PATH)
    assert response.status_code == 200 and response.json() == payload(reason=reason)
    code, stdout, stderr = await cli(client)
    assert code == 0, stderr
    assert json.loads(stdout) == response.json()
    code, text, stderr = await cli(client, machine=False)
    assert code == 0 and reason in text and "no suspensions" not in text


async def test_real_evaluation_without_results_is_not_campaign(service, monkeypatch):
    owner, _, client = service
    await register(owner, kind="evaluation", finish=False)
    monkeypatch.setattr(core_raw, "campaign_suspensions", lambda run: pytest.fail("read evaluation journal"))
    response = await client.get(PATH)
    assert response.status_code == 200 and response.json() == payload(reason="not_campaign")
    code, stdout, stderr = await cli(client)
    assert code == 0, stderr
    assert json.loads(stdout) == response.json()


@pytest.mark.parametrize("state,status,code", [(None, 404, "not_found"),
    ("deleted", 409, "results_deleted"), ("deleting", 404, "not_found"),
    ("delete_failed", 404, "not_found")])
async def test_missing_deleted_are_transport_errors(service, monkeypatch, state, status, code):
    owner, _, client = service
    if state is not None:
        await register(owner, kind="evaluation")
        await owner._db("update_operation", REF.id, results_state=state)
    monkeypatch.setattr(core_raw, "campaign_suspensions", lambda run: pytest.fail("read deleted journal"))
    response = await client.get(PATH)
    assert response.status_code == status and response.json()["error"]["code"] == code
    result, stdout, stderr = await cli(client)
    assert result == 1 and not stdout
    assert json.loads(stderr)["error"]["code"] == code


@pytest.mark.parametrize("path,content,headers", [
    (PATH, b"{}", {}), (PATH + "?disclose=private", None, {}),
    (PATH, None, {"Lab-Request-Id": "r1"}),
    (PATH, None, {"Lab-Owner-Generation": "g1"}),
    ("/v1/operations/bad!/suspensions", None, {}),
])
async def test_get_rejects_body_queries_keys_and_invalid_id(service, monkeypatch, path, content, headers):
    owner, _, client = service
    async def forbidden(ref):
        pytest.fail("invalid transport called Owner")
    monkeypatch.setattr(owner, "suspensions", forbidden)
    response = await client.request("GET", path, content=content, headers=headers)
    assert response.status_code == 400


async def test_request_cancellation_keeps_service_owned_read(service, monkeypatch):
    owner, runtime, client = service
    entered, finish, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def delayed(ref):
        assert ref == REF
        entered.set()
        await finish.wait()
        completed.set()
        return SuspensionEvidenceResult(REF.id, "available", [], None)
    monkeypatch.setattr(owner, "suspensions", delayed)
    request = asyncio.create_task(client.get(PATH))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(runtime._owner_calls) == 1 and not completed.is_set()
    finally:
        finish.set()
        await asyncio.gather(*runtime._owner_calls)
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert completed.is_set()


@pytest.mark.parametrize("reason", ["not_campaign", "core_raw_unsupported", "campaign_family_unsupported",
    "campaign_missing", "campaign_unreadable", "unsupported_schema"])
def test_response_validator_accepts_nullable_states(reason):
    value = payload(reason=reason)
    assert responses.validate(value, SCHEMA).to_payload() == value


def test_response_validator_accepts_all_closed_forms():
    value = payload(history())
    assert responses.validate(value, SCHEMA).to_payload() == value


@pytest.mark.parametrize("mutation", ["extra", "schema", "source", "state", "reason", "unavailable_rows",
    "extra_pause", "extra_evidence", "extra_row", "nested_code", "extra_crossing", "unknown_kind",
    "missing_code", "numeric_max_rate"])
def test_response_validator_atomically_refuses_unknown_or_private_shape(mutation):
    value = payload(history())
    first = value["suspensions"][0]
    if mutation == "extra": value["private"] = "PRIVATE"
    elif mutation == "schema": value["schema"] = "lab-suspension-evidence/2"
    elif mutation == "source": value["source"] = "projection"
    elif mutation == "state": value["state"] = "future"
    elif mutation == "reason": value["reason"] = "results_deleted"
    elif mutation == "unavailable_rows": value.update(state="unavailable", reason="campaign_missing")
    elif mutation == "extra_pause": first["diagnostics"] = "PRIVATE"
    elif mutation == "extra_evidence": first["evidence"]["diagnostics"] = "PRIVATE"
    elif mutation == "extra_row": first["rows"][0]["oracle"] = "PRIVATE"
    elif mutation == "nested_code": first["rows"][0]["code"] = {"diagnostics": "PRIVATE"}
    elif mutation == "extra_crossing": value["suspensions"][3]["evidence"]["crossing"]["details"] = "PRIVATE"
    elif mutation == "unknown_kind": first["evidence_kind"] = "future_kind"
    elif mutation == "missing_code": del first["rows"][0]["code"]
    else: value["suspensions"][3]["evidence"]["crossing"]["max_rate"] = 0.5
    with pytest.raises(ValueError, match="invalid public response"):
        responses.validate(value, SCHEMA)


def test_cli_refuses_unvalidated_response_before_output():
    value = payload(history())
    value["suspensions"][-1]["evidence"]["private"] = "PRIVATE"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=value))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(["--socket", "/tmp/lab-suspensions.sock", "--json", "suspensions", REF.id],
        stdout=stdout, stderr=stderr,
        client_factory=lambda **kwargs: ServiceClient(transport=transport, **kwargs))
    assert code == 1 and not stdout.getvalue()
    assert "PRIVATE" not in stderr.getvalue()


def test_text_prints_complete_nonrow_evidence():
    stream = io.StringIO()
    write_value(stream, payload(history()), machine=False)
    text = stream.getvalue()
    for token in ("crossing", "denominator", "max_count", "window_start", "operation_id", "prefix"):
        assert token in text
