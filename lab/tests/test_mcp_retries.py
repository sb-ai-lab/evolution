"""Retries preserve a sent request, and lost mutation responses have an explicit outcome."""
import asyncio
import json

import anyio
import httpx
import pytest
from mcp import Client, types

from evolution_lab.commands.mutations import operation_resume
from evolution_lab.mcp import approval as mcp_approval
from evolution_lab.mcp.server import build_server
from evolution_lab.mcp.transport import AsyncServiceTransport
from evolution_lab.preparation.models import PreparedSummary, RuntimePolicy, TargetBinding
from test_mcp_approval import ACCEPT, POLICY, ApprovalTransport
from test_mcp_facts import TARGET
from test_owner_store_starts import DIGEST, ONE_DIGEST, ZERO_DIGEST
from test_service_approvals_admission import (
    approval, headers, offline_target, operation_approvals, service, start_without_approval,
)


@pytest.mark.parametrize("tool", ["operation_resume", "project_accept_commit"])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_same_session_replays_the_original_approved_request(tmp_path, tool, mode):
    transport = ApprovalTransport()
    forms = []
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"TOKEN": "initial-fixture-value"}))
    fields = (
        {"operation_id": "op", "target_ref": TARGET, "secrets_file": str(path)}
        if tool == "operation_resume" else ACCEPT
    )
    arguments = {"request_id": "same", "owner_generation": "generation", **fields}

    async def elicit(_ctx, params):
        forms.append(params.message)
        return types.ElicitResult(action="accept", content={
            "confirm": True, **({"author": "Form Author"} if tool == "project_accept_commit" else {}),
        })

    server = build_server(tmp_path / "none.sock", timeout=1, transport=transport)
    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        first = await client.call_tool(tool, arguments)
        read_count = sum(request.mutation_key is None for request in transport.requests)
        path.unlink()
        identical = dict(reversed(list(arguments.items())))
        retry = await client.call_tool(tool, identical)
        different = await client.call_tool(tool, {**arguments, "owner_generation": "other"})
    assert first.structured_content == retry.structured_content
    assert not first.is_error and not retry.is_error
    assert different.structured_content["error"]["code"] == "request_conflict"
    assert sum(request.mutation_key is None for request in transport.requests) == read_count
    assert len(forms) == 1
    first_request, repeated = [request for request in transport.requests if request.mutation_key is not None]
    assert first_request == repeated
    assert first_request.payload["approval"] == {"via": "form", "autonomy": "ask", "tool": tool}
    if tool == "operation_resume":
        assert first_request.payload["secrets"] == {"TOKEN": "initial-fixture-value"}
        assert first_request.payload["observed_start"] == 3
        assert "initial-fixture-value" not in forms[0]
    else:
        assert first_request.payload["author"] == "Form Author"


async def test_concurrent_calls_for_one_key_share_the_prepared_request(tmp_path):
    transport = ApprovalTransport()
    asked = anyio.Event()
    answer = anyio.Event()
    questions = []

    async def elicit(_ctx, params):
        questions.append(params.message)
        asked.set()
        await answer.wait()
        return types.ElicitResult(action="accept", content={"confirm": True, "author": "First Author"})

    args = {"request_id": "one", "owner_generation": "generation", **POLICY}
    results = []
    server = build_server(tmp_path / "none.sock", timeout=1, transport=transport)
    async with Client(server, mode="legacy", elicitation_callback=elicit) as client:
        async def call():
            results.append(await client.call_tool("policy_confirm", args))

        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await asked.wait()
            group.start_soon(call)
            await anyio.wait_all_tasks_blocked()
            answer.set()
    assert len(results) == 2 and all(not result.is_error for result in results)
    assert len(questions) == 1
    first, second = transport.requests
    assert first == second


async def _settle(owner, operation):
    runtime = owner._operations.get(operation)
    if runtime is not None:
        await runtime.done
    await asyncio.gather(*tuple(owner._owned))


@pytest.mark.parametrize("rebuild", ["same_key_changed", "new_key_stale", "same_key_identical"])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_restart_rebuild_does_not_admit_another_resume(tmp_path, service, offline_target, rebuild, mode):
    owner, http = service
    operation = await start_without_approval(service, offline_target)
    target = offline_target.to_payload()
    key = "first-resume"
    first = operation_resume(key, owner.owner_generation, operation, target, {},
                             approval=approval("operation_resume"), observed_start=1)

    async def apply_first():
        answer = await http.post(first.path, headers=headers(owner, key), json=first.payload)
        assert answer.status_code == 202, answer.text
        await _settle(owner, operation)
        return answer.json()

    prepared = PreparedSummary(
        DIGEST, ZERO_DIGEST, "campaign",
        TargetBinding(ONE_DIGEST, ZERO_DIGEST, "http://127.0.0.1:8123", DIGEST, "target",
                      None, None, None, "required"),
        ZERO_DIGEST, (), RuntimePolicy(1, 1),
    ).to_payload()
    observed_requests = []
    first_response = None
    if rebuild == "same_key_changed":
        first_response = await apply_first()

    async def service_exchange(request):
        nonlocal first_response
        if request.url.path == f"/v1/prepared-executions/{DIGEST}":
            return httpx.Response(200, json=prepared)
        if request.url.path == first.path:
            observed_requests.append(json.loads(request.content))
            if first_response is None:
                # Interleave the original admission after the modern round's fresh fact read.
                first_response = await apply_first()
        return await http.send(request)

    forms = []

    async def elicit(_ctx, params):
        nonlocal first_response
        forms.append(params.message)
        if mode == "legacy" and first_response is None:
            # The restarted adapter read start 1 before the original request reached admission.
            first_response = await apply_first()
        return types.ElicitResult(action="accept", content={"confirm": True})

    socket = tmp_path / "none.sock"
    transport = AsyncServiceTransport(socket, timeout=1, transport=httpx.MockTransport(service_exchange))
    try:
        fresh = build_server(socket, timeout=1, transport=transport)
        async with Client(fresh, mode=mode, elicitation_callback=elicit) as client:
            rebuilt = await client.call_tool("operation_resume", {
                "request_id": "new-resume" if rebuild == "new_key_stale" else key,
                "owner_generation": owner.owner_generation, "operation_id": operation, "target_ref": target,
            })
    finally:
        await transport.aclose()

    assert len(forms) == 1
    (rebuilt_body,) = observed_requests
    if rebuild == "same_key_changed":
        assert "start 2" in forms[0] and rebuilt_body == {**first.payload, "observed_start": 2}
        assert rebuilt.structured_content["error"]["code"] == "request_conflict"
    elif rebuild == "new_key_stale":
        assert "start 1" in forms[0] and rebuilt_body == first.payload
        assert rebuilt.structured_content["error"]["code"] == "state_changed"
    else:
        assert "start 1" in forms[0] and rebuilt_body == first.payload
        assert not rebuilt.is_error and rebuilt.structured_content == first_response
    status = await http.get(first.path.removesuffix("/resume"))
    assert [row["start"] for row in status.json()["starts"]] == [1, 2]
    record, = await operation_approvals(owner, operation)
    assert record["request_id"] == key and record["start"] == 2


@pytest.mark.parametrize("failure", ["reset", "deadline"])
async def test_mutation_transport_failure_is_acceptance_unknown(tmp_path, failure):
    observed = []

    async def service_exchange(request):
        observed.append((request.method, request.headers["lab-request-id"], request.headers["lab-owner-generation"]))
        if failure == "reset":
            raise httpx.ReadError("connection reset after the mutation body", request=request)
        await anyio.sleep_forever()

    socket = tmp_path / "none.sock"
    transport = AsyncServiceTransport(socket, timeout=1, transport=httpx.MockTransport(service_exchange))
    try:
        server = build_server(socket, timeout=0.05, transport=transport)
        async with Client(server) as client:
            result = await client.call_tool("operation_stop", {
                "operation_id": "op", "request_id": "stop", "owner_generation": "generation",
            })
    finally:
        await transport.aclose()
    assert observed == [("POST", "stop", "generation")]
    assert result.is_error
    assert result.structured_content["error"]["code"] == "acceptance_unknown"
    assert result.structured_content["error"]["stage"] == "transport"
    assert "same request ID" in result.structured_content["hint"]


async def test_late_modern_answer_is_not_an_ordinary_retry_of_a_sent_request(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mcp_approval, "monotonic", lambda: now[0])
    transport = ApprovalTransport()
    fields = {"request_id": "policy", "owner_generation": "generation", **POLICY}

    async def unused(_ctx, _params):
        pytest.fail("the low-level client returns forms and sends answers explicitly")

    server = build_server(tmp_path / "none.sock", timeout=1, approval_timeout=5, transport=transport)
    async with Client(server, elicitation_callback=unused) as client:
        first = await client.session.call_tool("policy_confirm", fields, allow_input_required=True)
        form_key, = first.input_requests
        answer = {form_key: types.ElicitResult(action="accept", content={"confirm": True, "author": "Author"})}
        result = await client.session.call_tool("policy_confirm", fields,
                                               request_state=first.request_state, input_responses=answer)
        assert not result.is_error
        now[0] += 6
        expired = await client.session.call_tool("policy_confirm", fields,
                                                request_state=first.request_state, input_responses=answer)
        assert expired.structured_content["error"]["code"] == "approval_timeout"
    request, = transport.requests
    assert request.mutation_key == ("policy", "generation")
