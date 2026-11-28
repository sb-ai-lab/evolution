"""MCP approval is decided before service admission, with the shown state in the request."""
from dataclasses import dataclass
import json

import anyio
import httpx
import pytest
from mcp import Client, MCPError, types

from evolution_lab.commands.errors import ServiceFailure
from evolution_lab.execution_policy import policy_digest
from evolution_lab.mcp.server import build_server
from evolution_lab.mcp.transport import AsyncServiceTransport
from panel_support import uds_service
from test_mcp_facts import AUTO_POLICY, D, TARGET, start_reads
from test_mcp_server import _stdio, fake_service
from test_service_approvals_admission import approval_count, headers, service


@dataclass(frozen=True)
class Action:
    name: str
    arguments: dict
    path: str
    schema: str
    observed: int | None = None
    author: bool = False


OP = "/v1/operations/op"
PROJECT = "/v1/projects/project"
SHA = "c" * 40
RESOURCE = {"kind": "operation", "id": "op"}
EXPECTED = [{"id": "fact", "from": {"kind": "operation", "id": "op"}, "to": {"digest": D}}]
ATTESTATION = {"project_id": "project", "implementation": None, "author": "Tool Author"}
ACCEPT = {**ATTESTATION, "only": None, "across_pairs": False, "reason": "reviewed", "expected": EXPECTED}
POLICY = {"policy": AUTO_POLICY, "expected_digest": policy_digest(AUTO_POLICY), "author": "Tool Author", "note": None}
ACTIONS = [
    Action("operation_start", {"prepared_digest": D, "target_ref": TARGET}, "/v1/operations",
           "lab-service-start-request/4"),
    Action("project_start", {"project_id": "project", "implementation": None,
                             "prepared_digest": D, "target_ref": TARGET}, PROJECT + "/operations",
           "lab-service-project-start-request/2"),
    Action("operation_resume", {"operation_id": "op", "target_ref": TARGET}, OP + "/resume",
           "lab-service-resume-request/3", observed=3),
    Action("delete_results", {"operation_id": "op"}, OP + "/results",
           "lab-service-delete-results-request/1", observed=3),
    Action("release", {"ref": RESOURCE, "reason": "inspected"}, "/v1/releases",
           "lab-service-release-request/2", observed=3),
    Action("cleanup", {"ref": RESOURCE, "wait_seconds": None}, "/v1/cleanup",
           "lab-service-cleanup-request/2", observed=3),
    Action("project_sync", {"project_id": "project", "upstream": SHA}, PROJECT + "/sync",
           "lab-service-project-sync-request/2"),
    Action("policy_confirm", POLICY, "/v1/execution-policies",
           "lab-service-policy-confirm-request/2", author=True),
    Action("project_confirm", {**ATTESTATION, "claim": "checked", "attests": {}, "linked": [], "note": None},
           PROJECT + "/confirmations", "lab-service-confirm-request/2", author=True),
    Action("project_accept_commit", ACCEPT, PROJECT + "/acceptances",
           "lab-service-accept-request/3", author=True),
    Action("project_revoke_confirmation", {"project_id": "project", "confirmation_id": "confirmation", "reason": "old"},
           PROJECT + "/confirmations/confirmation/revoke", "lab-service-revoke-request/2"),
    Action("project_revoke_acceptance", {"project_id": "project", "acceptance_id": "acceptance", "reason": "old"},
           PROJECT + "/acceptances/acceptance/revoke", "lab-service-revoke-request/2"),
]


class ApprovalTransport:
    def __init__(self, refusal=None):
        self.requests = []
        self.refusal = refusal
        self.start = 3

    async def call(self, request):
        self.requests.append(request)
        if request.mutation_key is not None:
            if self.refusal is not None:
                raise ServiceFailure(self.refusal)
            return {"schema": request.schema}
        reads = {
            **start_reads(AUTO_POLICY, kind="campaign"),
            OP: {"starts": [{"start": self.start}], "prepared_digest": D, "auto_resume": {"used": 0}},
            PROJECT + "/sync/check": {"upstream": {"sha": SHA},
                                      "commits": [{"sha": SHA, "subject": "Change the docs"}],
                                      "commits_truncated": False},
        }
        return reads[request.path]


@pytest.mark.parametrize("action", ACTIONS, ids=lambda action: action.name)
@pytest.mark.parametrize("autonomy, callback, via", [
    ("ask", True, "form"), ("ask", False, "client_rules"), ("yolo", True, "none"),
])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_every_asking_tool_builds_the_approval_from_the_observed_state(
    tmp_path, action, autonomy, callback, via, mode,
):
    transport = ApprovalTransport()
    shown = []

    async def elicit(_ctx, params):
        assert all(request.mutation_key is None for request in transport.requests)
        shown.append(params.message)
        content = {"confirm": True}
        if action.author:
            content["author"] = "Form Author"
        return types.ElicitResult(action="accept", content=content)

    server = build_server(tmp_path / "none.sock", timeout=1, transport=transport, autonomy=autonomy)
    async with Client(server, mode=mode, elicitation_callback=elicit if callback else None) as client:
        result = await client.call_tool(action.name, {
            "request_id": "key", "owner_generation": "generation", **action.arguments,
        })
    assert not result.is_error, result.structured_content
    assert len(shown) == (1 if via == "form" else 0)
    *reads, mutation = transport.requests
    assert all(read.mutation_key is None for read in reads)
    assert mutation.path == action.path and mutation.mutation_key == ("key", "generation")
    assert mutation.payload["schema"] == action.schema
    assert mutation.payload["approval"] == {"via": via, "autonomy": autonomy, "tool": action.name}
    if action.observed is not None:
        assert reads[0].path == OP
        assert mutation.payload["observed_start"] == action.observed
        if shown:
            assert f"start {action.observed}" in shown[0]
    if action.author:
        assert mutation.payload["author"] == ("Form Author" if via == "form" else "Tool Author")
        assert mutation.payload["channel"] == "mcp"
    if action.name == "project_accept_commit":
        assert mutation.payload["expected"] == EXPECTED and mutation.payload["dry_run"] is False
    if action.name == "project_sync":
        assert [read.path for read in reads] == [PROJECT + "/sync/check"] * (2 if via == "form" and mode == "auto" else 1)
        assert mutation.payload["upstream"] == SHA
        if shown:
            assert "Change the docs" in shown[0]


@pytest.mark.parametrize("answer, code", [
    ("decline", "approval_declined"), ("cancel", "approval_cancelled"),
    ("false", "approval_declined"), ("missing_author", "approval_failed"),
    ("error", "approval_failed"), ("timeout", "approval_timeout"),
])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_failed_form_sends_no_mutation(tmp_path, answer, code, mode):
    transport = ApprovalTransport()

    async def elicit(_ctx, _params):
        if answer == "error":
            return types.ErrorData(code=types.INTERNAL_ERROR, message="Client failed")
        if answer == "timeout":
            if mode == "legacy":
                await anyio.sleep_forever()
            await anyio.sleep(0.15)
            return types.ElicitResult(action="accept", content={"confirm": True, "author": "Late Author"})
        if answer in {"false", "missing_author"}:
            return types.ElicitResult(action="accept", content={"confirm": answer != "false"})
        return types.ElicitResult(action=answer)

    server = build_server(tmp_path / "none.sock", timeout=1, approval_timeout=0.1, transport=transport)
    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        fields = {"request_id": "key", "owner_generation": "generation", **POLICY}
        if mode == "auto" and answer == "error":
            # The SDK aborts before sending a second round when the callback itself fails.
            with pytest.raises(MCPError) as error:
                await client.call_tool("policy_confirm", fields)
            assert error.value.code == types.INTERNAL_ERROR
            assert transport.requests == []
            return
        result = await client.call_tool("policy_confirm", fields)
    assert result.is_error
    assert result.structured_content["error"]["code"] == code
    assert result.structured_content["error"]["stage"] == "approval"
    assert transport.requests == []


@pytest.mark.parametrize("tool, fields", [
    ("operation_stop", {"operation_id": "op"}),
    ("project_accept_preview", {name: value for name, value in ACCEPT.items() if name != "expected"}),
])
async def test_non_asking_tools_never_elicit(tmp_path, tool, fields):
    transport = ApprovalTransport()

    async def elicit(_ctx, _params):
        pytest.fail("a non-asking tool requested a form")

    server = build_server(tmp_path / "none.sock", timeout=1, transport=transport)
    async with Client(server, elicitation_callback=elicit) as client:
        result = await client.call_tool(tool, {
            "request_id": "key", "owner_generation": "generation", **fields,
        })
    assert not result.is_error
    request, = transport.requests
    assert "approval" not in request.payload


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_form_has_its_own_deadline(tmp_path, mode):
    transport = ApprovalTransport()

    async def elicit(_ctx, _params):
        await anyio.sleep(0.15)
        return types.ElicitResult(action="accept", content={"confirm": True, "author": "Form Author"})

    server = build_server(tmp_path / "none.sock", timeout=0.06, approval_timeout=2, transport=transport)
    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = await client.call_tool("policy_confirm", {
            "request_id": "key", "owner_generation": "generation", **POLICY,
        })
    assert not result.is_error, result.structured_content
    assert len(transport.requests) == 1


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_declined_form_leaves_no_service_mutation_entry_or_approval(tmp_path, service, mode):
    owner, http = service
    key = "declined-policy"
    transport = AsyncServiceTransport(tmp_path / "none.sock", timeout=1, transport=httpx.MockTransport(http.send))

    async def decline(_ctx, _params):
        return types.ElicitResult(action="decline")

    try:
        server = build_server(tmp_path / "none.sock", timeout=1, transport=transport)
        async with Client(server, mode=mode, elicitation_callback=decline) as client:
            result = await client.call_tool("policy_confirm", {
                "request_id": key, "owner_generation": owner.owner_generation, **POLICY,
            })
        assert result.structured_content["error"]["code"] == "approval_declined"
        assert await approval_count(owner) == 0
        assert await owner.execution_policies() == ()
        # A different body under the same key would conflict with any mutation-table entry.
        direct = await http.post("/v1/execution-policies", headers=headers(owner, key), json={
            **POLICY, "schema": "lab-service-policy-confirm-request/1", "channel": "cli", "author": "Direct Author",
        })
        assert direct.status_code == 200
        assert direct.json()["author"] == "Direct Author"
        assert await approval_count(owner) == 0
    finally:
        await transport.aclose()


@pytest.mark.parametrize("code", ["state_changed", "digest_mismatch", "inputs_changed"])
async def test_accept_state_refusals_keep_the_service_error_and_give_the_same_guidance(tmp_path, code):
    refusal = {"error": {"code": code, "stage": "project", "message": "Saved service message."}}
    transport = ApprovalTransport(refusal)
    server = build_server(tmp_path / "none.sock", timeout=1, transport=transport)
    async with Client(server) as client:
        result = await client.call_tool("project_accept_commit", {
            "request_id": "key", "owner_generation": "generation", **ACCEPT,
        })
    assert result.is_error
    assert result.structured_content["error"] == refusal["error"]
    assert result.structured_content["hint"] == "Read the current state before asking again. Use a new request ID."


@pytest.mark.parametrize("declared, via", [(None, "client_rules"), ({}, "form"), ({"url": {}}, "client_rules")])
async def test_only_declared_form_capability_controls_fallback(declared, via):
    from evolution_lab.mcp.approval import obtain, via_form
    from evolution_lab.mcp.facts import Facts

    forms = []

    class Session:
        client_capabilities = types.ClientCapabilities(elicitation=declared)

        async def elicit_form(self, message, schema):
            forms.append((message, schema))
            return types.ElicitResult(action="accept", content={"confirm": True})

    facts = Facts("Ask", "An object", None, None, None, [], [], [], False)
    result = await obtain(Session(), facts, autonomy="ask", tool="release", timeout=1)
    assert via_form(Session(), "ask") == (via == "form")
    assert result == {"approval": {"via": via, "autonomy": "ask", "tool": "release"}}
    assert len(forms) == (1 if via == "form" else 0)


async def test_modern_resume_rereads_facts_and_asks_again_when_the_form_changes(tmp_path):
    transport = ApprovalTransport()
    fields = {"request_id": "resume", "owner_generation": "generation", "operation_id": "op", "target_ref": TARGET}
    shown = []

    async def elicit(_ctx, params):
        assert all(request.mutation_key is None for request in transport.requests)
        shown.append(params.message)
        if len(shown) == 1:
            transport.start = 4
        return types.ElicitResult(action="accept", content={"confirm": True})

    server = build_server(tmp_path / "none.sock", timeout=2, transport=transport)
    async with Client(server, elicitation_callback=elicit) as client:
        result = await client.call_tool("operation_resume", fields)
    assert not result.is_error
    assert len(shown) == 2 and "start 3" in shown[0] and "start 4" in shown[1]
    mutation, = [request for request in transport.requests if request.mutation_key is not None]
    assert mutation.payload["observed_start"] == 4


async def test_legacy_resume_uses_the_start_shown_before_the_answer(tmp_path):
    refusal = {"error": {"code": "state_changed", "stage": "before_start", "message": "Start changed."}}
    transport = ApprovalTransport(refusal)
    fields = {"request_id": "resume", "owner_generation": "generation", "operation_id": "op", "target_ref": TARGET}
    shown = []

    async def elicit(_ctx, params):
        shown.append(params.message)
        transport.start = 4
        return types.ElicitResult(action="accept", content={"confirm": True})

    server = build_server(tmp_path / "none.sock", timeout=2, transport=transport)
    async with Client(server, mode="legacy", elicitation_callback=elicit) as client:
        refused = await client.call_tool("operation_resume", fields)
        assert refused.structured_content["error"] == refusal["error"]
        transport.refusal = None
        fresh = await client.call_tool("operation_resume", {**fields, "request_id": "next"})
    assert not fresh.is_error
    assert len(shown) == 2 and "start 3" in shown[0] and "start 4" in shown[1]
    requests = [request for request in transport.requests if request.mutation_key is not None]
    assert [request.payload["observed_start"] for request in requests] == [3, 4]


async def test_modern_round_state_is_sealed_and_bound_to_tool_arguments(tmp_path):
    transport = ApprovalTransport()

    async def unused_callback(_ctx, _params):
        pytest.fail("the low-level SDK client returns the form for inspection")

    server = build_server(tmp_path / "none.sock", timeout=2, transport=transport)
    fields = {"request_id": "policy", "owner_generation": "generation", **POLICY}
    async with Client(server, elicitation_callback=unused_callback) as client:
        form = await client.session.call_tool("policy_confirm", fields, allow_input_required=True)
        assert isinstance(form, types.InputRequiredResult)
        assert form.request_state
        name, = form.input_requests
        answer = {name: types.ElicitResult(action="accept", content={"confirm": True, "author": "Human"})}
        for new_fields, state in [
            (fields, "unsigned-form-state"),
            ({**fields, "request_id": "another"}, form.request_state),
        ]:
            with pytest.raises(MCPError) as caught:
                await client.session.call_tool("policy_confirm", new_fields, allow_input_required=True,
                                               request_state=state, input_responses=answer)
            assert caught.value.code == types.INVALID_PARAMS
        forged = await client.session.call_tool("policy_confirm", fields, allow_input_required=True,
                                                input_responses=answer)
        assert isinstance(forged, types.InputRequiredResult) or forged.is_error
    assert transport.requests == []


async def test_modern_form_rereads_secrets_without_putting_values_in_round_state(tmp_path):
    transport = ApprovalTransport()
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"TOKEN": "initial-fixture-value"}))
    fields = {"request_id": "resume", "owner_generation": "generation", "operation_id": "op", "target_ref": TARGET,
              "secrets_file": str(path)}
    questions = []

    async def elicit(_ctx, params):
        questions.append(params.message)
        path.write_text(json.dumps({"TOKEN": "updated-fixture-value"}))
        return types.ElicitResult(action="accept", content={"confirm": True})

    server = build_server(tmp_path / "none.sock", timeout=2, transport=transport)
    async with Client(server, elicitation_callback=elicit) as client:
        result = await client.call_tool("operation_resume", fields)
    assert not result.is_error
    message, = questions
    assert "TOKEN" in message and "fixture-value" not in message
    mutation, = [request for request in transport.requests if request.mutation_key is not None]
    assert mutation.payload["secrets"] == {"TOKEN": "updated-fixture-value"}


@pytest.mark.parametrize("mode, version", [("legacy", "2025-11-25"), ("auto", "2026-07-28")])
async def test_stdio_shows_the_approval_form_on_both_protocols(mode, version):
    app, seen = fake_service()
    questions = []

    async def decline(_ctx, params):
        questions.append(params.message)
        return types.ElicitResult(action="decline")

    async with uds_service(app) as sock:
        async with Client(_stdio(sock), mode=mode, elicitation_callback=decline) as client:
            assert client.protocol_version == version
            result = await client.call_tool("policy_confirm", {
                "request_id": "policy", "owner_generation": "generation", **POLICY,
            })
    assert result.structured_content["error"]["code"] == "approval_declined"
    assert len(questions) == 1
    assert seen == ["/v1/health"]
