"""The MCP server over the command layer: tools, errors, deadline, guides, resources and the version notice."""
from __future__ import annotations

import asyncio
import dataclasses
from importlib import metadata
import io
import json
import sys
import time

import jsonschema
import pytest
from mcp import Client, MCPError, StdioServerParameters
from mcp import types as mcp_types
from starlette.applications import Starlette
from starlette.requests import Request as HttpRequest
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from evolution_lab import guides
from evolution_lab.cli.output import write_json
from evolution_lab.commands.errors import ServiceFailure
from evolution_lab.mcp import server as mcp_server
from evolution_lab.mcp.server import build_server, read_service_version
from evolution_lab.mcp.tools import PUBLISHED, output_schema

from panel_support import uds_service

INSTALLED = metadata.version("evolution-lab")
D = "a" * 64
STATUS = {
    "schema": "lab-operation-status/8", "id": "operation", "owner_generation": "generation", "prepared_digest": D,
    "kind": "campaign", "target_ref": {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "generation"},
    "control_state": "finished",
    "process": {"launched": True, "exit_code": 1, "cleanup_complete": True, "forced": False},
    "target_cleanup": {"state": "confirmed", "complete": True}, "result": None, "failure": None, "refusal": None,
    "core_terminal": None, "results_state": "present",
    "executor": {"kind": "lab-campaign-worker/1", "environment_digest": D, "lab_wheel_sha256": D,
                 "core_wheel_sha256": D, "module_hashes": {}},
    "retained_summary": None, "starts": [], "resume": None, "entrypoint": None, "progress": None,
    "progress_state": "not_started", "times": None, "times_state": "not_started", "times_source": None,
    "times_reason": None,
    "auto_resume": {"policy_digest": None, "state": "off", "next_at": None, "used": 0, "max": None, "blocked": None,
                    "detail": None},
}
SPECS = {spec.name: spec for spec in PUBLISHED}


def _health(lab_version: str = INSTALLED) -> dict:
    return {"schema": "lab-service-health/2", "lab_version": lab_version, "core_version": "c", "distributions": [],
            "owner_generation": "gen-1", "active_operation_id": None, "blockers": [], "service_state": "running",
            "service_environment": None}


def fake_service(lab_version: str = INSTALLED, *, stall_health: bool = False, health_schema: str | None = None):
    """A Starlette service: health, one operation status and a stalled route; ``seen`` records request paths.

    ``health_schema`` serves a health of another schema version (``lab-service-health/1``) with the same fields.
    """
    seen: list[str] = []

    async def health(request: HttpRequest):
        seen.append(request.url.path)
        if stall_health:
            return await stall(request)
        value = _health(lab_version)
        if health_schema is not None:
            value["schema"] = health_schema
        return JSONResponse(value)

    async def status(request: HttpRequest):
        seen.append(request.url.path)
        return JSONResponse(STATUS)

    async def stall(request: HttpRequest):
        seen.append(request.url.path)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if await request.is_disconnected():
                return Response()
        return JSONResponse(STATUS)

    app = Starlette(routes=[Route("/v1/health", health), Route("/v1/operations/operation", status),
                            Route("/v1/operations/stall", stall)])
    return app, seen


def _cli_json(value) -> str:
    stream = io.StringIO()
    write_json(stream, value)
    return stream.getvalue()


def _checked(name: str, result: mcp_types.CallToolResult) -> mcp_types.CallToolResult:
    """Every result, error results included, fits the tool's advertised output schema (the SDK skips errors)."""
    jsonschema.Draft202012Validator(output_schema(SPECS[name])).validate(result.structured_content)
    return result


async def _call(client: Client, name: str, arguments: dict | None = None) -> mcp_types.CallToolResult:
    return _checked(name, await client.call_tool(name, arguments or {}))


def _usage_error(result: mcp_types.CallToolResult) -> dict:
    assert result.is_error is True
    error = result.structured_content["error"]
    assert error["code"] == "usage_error" and error["stage"] == "client"
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


class FakeTransport:
    """A transport returning canned values per request path, to reach the server's own decoding."""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    async def call(self, request):
        value = self.values[request.path]
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self) -> None:
        return None


async def test_list_tools_matches_published(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        tools = (await client.list_tools()).tools
    assert [tool.name for tool in tools] == [spec.name for spec in PUBLISHED]
    for tool, spec in zip(tools, PUBLISHED):
        assert tool.description == spec.description
        assert tool.input_schema == spec.input_schema
        assert tool.output_schema == output_schema(spec)


async def test_every_listed_schema_has_an_object_root(tmp_path):
    """The MCP schema (and the TypeScript SDK) require ``type: object`` at the root of both schemas."""
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        tools = (await client.list_tools()).tools
    assert tools
    for tool in tools:
        assert tool.input_schema["type"] == "object", tool.name
        assert tool.output_schema["type"] == "object", tool.name


async def test_server_info_names_the_installed_version(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        assert client.server_info.name == "evolution-lab"
        assert client.server_info.version == INSTALLED


async def test_call_operation_status_returns_service_object():
    app, seen = fake_service()
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            result = await _call(client, "operation_status", {"operation": "operation"})
    assert result.is_error is False
    assert result.structured_content == STATUS
    assert [item.text for item in result.content] == [_cli_json(STATUS)]
    assert seen == ["/v1/operations/operation"]


async def test_call_with_service_down_returns_service_unavailable_result(tmp_path):
    sock = tmp_path / "none.sock"
    async with Client(build_server(sock, timeout=1.0)) as client:
        result = await _call(client, "operation_status", {"operation": "operation"})
    assert result.is_error is True
    assert result.structured_content["error"] == {
        "code": "service_unavailable", "stage": "client", "message": "The Lab service is not reachable."}
    assert str(sock) in result.structured_content["hint"]
    assert json.loads(result.content[0].text) == result.structured_content


async def test_bad_id_is_usage_error_not_protocol_error():
    app, seen = fake_service()
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            operation = _usage_error(await _call(client, "operation_status", {"operation": "a/b"}))
            digest = _usage_error(await _call(client, "bundle_show", {"digest": "zz"}))
    assert "operation" in operation["hint"]
    assert "digest" in digest["hint"]
    assert seen == []


async def test_unknown_argument_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "operation_status", {"operation": "op", "extra": "x"}))
    assert envelope["error"]["message"] == "The request arguments are invalid."
    assert "extra" in envelope["hint"]


async def test_empty_project_id_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "project_show", {"project": ""}))
    assert "project" in envelope["hint"]


async def test_empty_scenario_id_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "scenario_show", {"scenario": ""}))
    assert "scenario" in envelope["hint"]


async def test_empty_query_arguments_reach_the_service_as_the_cli_sends_them():
    """An empty query argument is not refused by the adapter: the builder and the service decide, as for the CLI."""
    invalid = {"error": {"code": "invalid_request", "stage": "transport", "message": "The service request is invalid."}}
    queries: list[tuple[str, str]] = []

    async def refuse(request: HttpRequest):
        queries.append((request.url.path, request.url.query))
        return JSONResponse(invalid, status_code=400)

    app = Starlette(routes=[Route("/v1/projects/p/tree", refuse), Route("/v1/projects/p/operations", refuse)])
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            tree = await _call(client, "project_tree", {"project": "p", "implementation": "i", "path": ""})
            listed = await _call(client, "project_operations", {"project": "p", "implementation": ""})
    assert tree.is_error is True and tree.structured_content == invalid
    assert listed.is_error is True and listed.structured_content == invalid
    assert queries == [("/v1/projects/p/tree", "implementation=i&path="), ("/v1/projects/p/operations", "")]


async def test_non_string_argument_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "operation_status", {"operation": 5}))
        flag = _usage_error(await _call(client, "projects_list", {"archived": "yes"}))
    assert "operation" in envelope["hint"]
    assert "archived" in flag["hint"]


async def test_missing_argument_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "operation_status", {}))
    assert "operation" in envelope["hint"]


async def test_unknown_tool_is_a_protocol_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        with pytest.raises(MCPError) as info:
            await client.call_tool("operation_delete", {})
    assert info.value.code == mcp_types.INVALID_PARAMS


async def test_stalled_service_hits_deadline_and_next_call_works():
    app, seen = fake_service()
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=0.5)) as client:
            start = time.monotonic()
            stalled = await _call(client, "operation_status", {"operation": "stall"})
            elapsed = time.monotonic() - start
            after = await _call(client, "health")
    assert elapsed < 2
    assert stalled.is_error is True
    assert stalled.structured_content == {
        "error": {"code": "request_timeout", "stage": "transport", "message": "The service request timed out."},
        "hint": "retry"}
    assert after.is_error is False
    assert after.structured_content == _health()


async def test_guides_without_service(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        listed = await _call(client, "guides_list")
        read = await _call(client, "guide_read", {"name": "evolution-lab-budget"})
    expected = [{"name": g.name, "description": g.description, "path": g.path} for g in guides.list_guides()]
    assert listed.is_error is False
    assert listed.structured_content == {"value": expected}
    assert [item.text for item in listed.content] == [_cli_json(expected)]
    text = (guides.root() / "budget" / "SKILL.md").read_text(encoding="utf-8")
    assert read.is_error is False
    assert read.structured_content == {"name": "evolution-lab-budget", "file": "SKILL.md", "text": text}
    assert len(read.content) == 1


async def test_unknown_guide_file_is_usage_error(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        envelope = _usage_error(await _call(client, "guide_read", {"name": "budget", "file": "../pitfalls/SKILL.md"}))
    assert envelope["error"]["message"] == "unknown guide file"


async def test_version_mismatch_notice_on_guide_results():
    app, seen = fake_service(lab_version="0.0.0")
    notice = f"Guides come from evolution-lab {INSTALLED} installed with lab mcp; the service runs 0.0.0."
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            listed = await _call(client, "guides_list")
            read = await _call(client, "guide_read", {"name": "budget"})
    assert [item.text for item in listed.content][1:] == [notice]
    assert [item.text for item in read.content][1:] == [notice]
    assert json.loads(listed.content[0].text) == listed.structured_content["value"]
    assert seen == ["/v1/health"]


async def test_matching_version_adds_no_notice():
    app, seen = fake_service()
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            listed = await _call(client, "guides_list")
            await _call(client, "guides_list")
    assert len(listed.content) == 1
    assert seen == ["/v1/health"]


async def test_health_call_records_the_service_version():
    app, seen = fake_service(lab_version="0.0.0")
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=1.0)) as client:
            await _call(client, "health")
            listed = await _call(client, "guides_list")
    assert len(listed.content) == 2
    assert seen == ["/v1/health"]


async def test_scenario_schema_bytes_become_text(tmp_path):
    path = "/v1/scenarios/connect/schemas/spec-answers/1"
    transport = FakeTransport({path: '{"type": "object"}\n'.encode()})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "scenario_schema", {"scenario": "connect", "schema": "spec-answers/1"})
    assert result.is_error is False
    assert result.structured_content == {"text": '{"type": "object"}\n'}
    assert [item.text for item in result.content] == ['{"type": "object"}\n']


async def test_undecodable_schema_bytes_are_invalid_service_response(tmp_path):
    path = "/v1/scenarios/connect/schemas/spec-answers/1"
    transport = FakeTransport({path: b"\xff\xfe"})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "scenario_schema", {"scenario": "connect", "schema": "spec-answers/1"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_service_response"
    assert "different versions" in result.structured_content["hint"]


async def test_value_of_the_wrong_shape_is_invalid_service_response(tmp_path):
    transport = FakeTransport({"/v1/health": "not an object"})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "health")
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_service_response"


async def test_a_service_error_envelope_is_passed_through_unchanged(tmp_path):
    envelope = {"error": {"code": "operation_not_found", "stage": "service", "message": "No such operation."}}
    transport = FakeTransport({"/v1/operations/operation": ServiceFailure(envelope)})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "operation_status", {"operation": "operation"})
    assert result.is_error is True
    assert result.structured_content == envelope
    assert json.loads(result.content[0].text) == envelope


async def test_internal_error_names_its_reason_on_stderr_only(tmp_path, capsys):
    transport = FakeTransport({"/v1/health": RuntimeError("boom")})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "health")
    captured = capsys.readouterr()
    assert result.structured_content["error"]["code"] == "internal_error"
    assert "boom" not in result.content[0].text
    assert "internal_error in health: RuntimeError: boom" in captured.err
    assert captured.out == ""


async def test_invalid_shape_names_its_reason_on_stderr_only(tmp_path, capsys):
    transport = FakeTransport({"/v1/health": "not an object"})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        await _call(client, "health")
    captured = capsys.readouterr()
    assert "invalid_service_response" in captured.err
    assert captured.out == ""


async def test_non_utf8_guide_file_is_an_internal_error_not_usage(tmp_path, monkeypatch, capsys):
    def broken(name, file="SKILL.md"):
        b"\xff".decode("utf-8")

    monkeypatch.setitem(mcp_server.REGISTRY, "guide_read", dataclasses.replace(mcp_server.REGISTRY["guide_read"], run=broken))
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        result = await _call(client, "guide_read", {"name": "budget"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "internal_error"
    captured = capsys.readouterr()
    assert "UnicodeDecodeError" in captured.err
    assert captured.out == ""


async def test_unexpected_exception_is_an_error_result(tmp_path):
    transport = FakeTransport({"/v1/health": RuntimeError("boom")})
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        result = await _call(client, "health")
    assert result.is_error is True
    assert result.structured_content["error"] == {
        "code": "internal_error", "stage": "client", "message": "The MCP server failed to run the command."}
    assert "boom" not in result.content[0].text


async def test_read_resource_skill_md(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        resources = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        read = await client.read_resource("lab://guides/evolution-lab-budget/SKILL.md")
        scenario = await client.read_resource("lab://guides/scenarios/connect/SKILL.md")
        encoded = await client.read_resource("lab://guides/scenarios%2Fconnect/SKILL.md")
    scenarios = [f"lab://guides/scenarios/{d['id']}/{d['skill']}" for d in guides.scenarios().descriptors]
    assert [r.uri for r in resources] == [f"lab://guides/{g.name}/SKILL.md" for g in guides.list_guides()] + scenarios
    assert "lab://guides/scenarios/connect/SKILL.md" in scenarios
    assert all(r.mime_type == "text/markdown" for r in resources)
    assert [(t.uri_template, t.mime_type) for t in templates] == [("lab://guides/{name}/{file}", "text/markdown")]
    (content,) = read.contents
    assert isinstance(content, mcp_types.TextResourceContents)
    assert content.uri == "lab://guides/evolution-lab-budget/SKILL.md"
    assert content.mime_type == "text/markdown"
    assert content.text == (guides.root() / "budget" / "SKILL.md").read_text(encoding="utf-8")
    connect = (guides.root() / "scenarios" / "connect" / "SKILL.md").read_text(encoding="utf-8")
    assert scenario.contents[0].text == connect
    assert encoded.contents[0].text == connect


@pytest.mark.parametrize("uri", [
    "lab://guides/evolution-lab-budget/../pitfalls/SKILL.md",
    "lab://guides/budget/%2E%2E/%2E%2E/__init__.py",
    "lab://guides/../guides/budget/SKILL.md",
    "lab://guides/nope/SKILL.md",
    "lab://other/budget/SKILL.md",
    "file:///etc/passwd",
])
async def test_read_resource_refuses_escape(tmp_path, uri):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        with pytest.raises(MCPError) as info:
            await client.read_resource(uri)
    assert info.value.code == mcp_types.INVALID_PARAMS


async def test_instructions_name_the_socket(tmp_path):
    sock = tmp_path / "lab.sock"
    async with Client(build_server(sock, timeout=1.0)) as client:
        instructions = client.instructions
    assert str(sock) in instructions
    assert "only read" not in instructions
    assert "Autonomy is ask" in instructions
    assert "Lab asks the human before" in instructions
    assert "ask the human in the conversation" in instructions
    assert "operations_list" in instructions
    assert "CLI" in instructions


async def test_yolo_instructions_name_the_level_and_omit_asking(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, autonomy="yolo")) as client:
        instructions = client.instructions
    assert "Autonomy is yolo" in instructions
    assert "Lab does not ask" in instructions
    assert "ask the human in the conversation" not in instructions


NOTICE = f"Guides come from evolution-lab {INSTALLED} installed with lab mcp; the service runs 0.0.0."


async def test_startup_version_read_puts_the_notice_in_instructions_and_is_reused():
    app, seen = fake_service(lab_version="0.0.0")
    async with uds_service(app) as sock:
        version = await read_service_version(sock, timeout=1.0)
        async with Client(build_server(sock, timeout=1.0, service_version=version)) as client:
            instructions = client.instructions
            listed = await _call(client, "guides_list")
    assert version == "0.0.0"
    assert instructions.endswith(NOTICE)
    assert [item.text for item in listed.content][1:] == [NOTICE]
    assert seen == ["/v1/health"]


async def test_startup_version_read_fails_fast_without_a_socket(tmp_path):
    start = time.monotonic()
    assert await read_service_version(tmp_path / "none.sock", timeout=60.0) is None
    assert time.monotonic() - start < 1


async def test_startup_version_read_is_bounded_by_the_call_deadline():
    app, _ = fake_service(stall_health=True)
    async with uds_service(app) as sock:
        start = time.monotonic()
        assert await read_service_version(sock, timeout=0.5) is None
    assert time.monotonic() - start < 2


def _stdio(sock, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "evolution_lab.cli", "mcp", "--socket", str(sock), *extra])


async def test_lab_mcp_instructions_carry_the_notice_on_a_version_mismatch():
    app, seen = fake_service(lab_version="0.0.0")
    async with uds_service(app) as sock:
        async with Client(_stdio(sock)) as client:
            instructions = client.instructions
            listed = await client.call_tool("guides_list", {})
    assert str(sock) in instructions
    assert instructions.endswith(NOTICE)
    assert [item.text for item in listed.content][1:] == [NOTICE]
    assert seen == ["/v1/health"]


async def test_lab_mcp_starts_without_a_socket_and_without_a_notice(tmp_path):
    sock = tmp_path / "none.sock"
    async with Client(_stdio(sock)) as client:
        instructions = client.instructions
        listed = await client.call_tool("guides_list", {})
    assert str(sock) in instructions
    assert "Guides come from" not in instructions
    assert listed.is_error is False and len(listed.content) == 1


async def test_lab_mcp_starts_when_the_service_stalls():
    app, _ = fake_service(stall_health=True)
    async with uds_service(app) as sock:
        start = time.monotonic()
        async with Client(_stdio(sock, "--timeout", "0.5")) as client:
            instructions = client.instructions
        elapsed = time.monotonic() - start
    assert "Guides come from" not in instructions
    assert elapsed < 10


async def test_every_listed_resource_is_readable(tmp_path):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0)) as client:
        resources = (await client.list_resources()).resources
        for resource in resources:
            (content,) = (await client.read_resource(resource.uri)).contents
            assert content.text.strip()


def test_startup_deadline_caps_the_read_at_five_seconds():
    assert mcp_server.STARTUP_DEADLINE == 5.0


async def test_startup_version_read_is_capped_by_the_startup_deadline(monkeypatch):
    monkeypatch.setattr(mcp_server, "STARTUP_DEADLINE", 0.5)
    app, _ = fake_service(stall_health=True)
    async with uds_service(app) as sock:
        start = time.monotonic()
        assert await read_service_version(sock, timeout=60.0) is None
    assert time.monotonic() - start < 2


async def test_guides_do_not_wait_the_tool_deadline_on_a_stalled_service(monkeypatch):
    monkeypatch.setattr(mcp_server, "STARTUP_DEADLINE", 0.5)
    app, _ = fake_service(stall_health=True)
    async with uds_service(app) as sock:
        async with Client(build_server(sock, timeout=60.0)) as client:
            start = time.monotonic()
            listed = await _call(client, "guides_list")
            elapsed = time.monotonic() - start
            again = await _call(client, "guide_read", {"name": "budget"})
    assert elapsed < 2
    assert listed.is_error is False and len(listed.content) == 1
    assert again.is_error is False and len(again.content) == 1


# One deadline per call (PDR-0061: every call ends by --timeout with a typed error).

TIMEOUT = {"error": {"code": "request_timeout", "stage": "transport", "message": "The service request timed out."},
           "hint": "retry"}


def _slow(runner, seconds: float):
    def slow(*args, **kwargs):
        time.sleep(seconds)
        return runner(*args, **kwargs)
    return slow


async def test_slow_offline_read_hits_the_call_deadline_and_the_next_call_works(tmp_path, monkeypatch):
    entry = mcp_server.REGISTRY["guide_read"]
    monkeypatch.setitem(mcp_server.REGISTRY, "guide_read", dataclasses.replace(entry, run=_slow(entry.run, 0.6)))
    async with Client(build_server(tmp_path / "none.sock", timeout=0.15, service_version=INSTALLED)) as client:
        start = time.monotonic()
        stalled = await _call(client, "guide_read", {"name": "budget"})
        elapsed = time.monotonic() - start
        after = await _call(client, "guides_list")
    assert elapsed < 0.45
    assert stalled.is_error is True and stalled.structured_content == TIMEOUT
    assert json.loads(stalled.content[0].text) == TIMEOUT
    assert after.is_error is False


class SlowHealth(FakeTransport):
    """``health`` answers after ``seconds``; ``calls`` counts the reads."""

    def __init__(self, seconds: float, lab_version: str = "0.0.0") -> None:
        super().__init__({"/v1/health": _health(lab_version)})
        self.seconds = seconds
        self.calls = 0

    async def call(self, request):
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return await super().call(request)


async def test_a_slow_version_read_never_costs_the_guide_its_result(tmp_path, monkeypatch):
    """A slow guide read plus a stalled version read: the read is cut off before the deadline, the guide stands."""
    entry = mcp_server.REGISTRY["guide_read"]
    monkeypatch.setitem(mcp_server.REGISTRY, "guide_read", dataclasses.replace(entry, run=_slow(entry.run, 0.2)))
    transport = SlowHealth(5.0)
    async with Client(build_server(tmp_path / "none.sock", timeout=3.0, transport=transport)) as client:
        # The SDK lazily fetches output schemas outside the timed server request.
        await client.list_tools()
        start = time.monotonic()
        read = await _call(client, "guide_read", {"name": "budget"})
        elapsed = time.monotonic() - start
        after = await _call(client, "guides_list")
    assert elapsed < 4.0
    assert read.is_error is False and len(read.content) == 1
    assert read.structured_content["file"] == "SKILL.md"
    assert transport.calls == 1
    assert after.is_error is False and len(after.content) == 1


async def test_first_guide_call_after_a_failed_startup_read_succeeds_on_a_stalled_service():
    """Timeout <= 5 s, the startup read failed, health stalls: the first guide call is the guide, no notice."""
    app, seen = fake_service(lab_version="0.0.0", stall_health=True)
    async with uds_service(app) as sock:
        version = await read_service_version(sock, timeout=0.4)
        async with Client(build_server(sock, timeout=0.4, service_version=version)) as client:
            start = time.monotonic()
            listed = await _call(client, "guides_list")
            elapsed = time.monotonic() - start
            read = await _call(client, "guide_read", {"name": "budget"})
    assert version is None
    assert elapsed < 0.4
    assert listed.is_error is False and len(listed.content) == 1
    assert read.is_error is False and len(read.content) == 1
    assert set(seen) == {"/v1/health"}


async def test_a_version_read_within_the_deadline_still_adds_the_notice(tmp_path):
    transport = SlowHealth(0.05)
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
        listed = await _call(client, "guides_list")
    assert [item.text for item in listed.content][1:] == [NOTICE]


# The advisory version read takes lab_version from a health of any schema; the health tool stays strict.

async def test_another_health_schema_still_yields_the_notice_in_instructions_and_on_guides():
    app, seen = fake_service(lab_version="0.0.0", health_schema="lab-service-health/1")
    async with uds_service(app) as sock:
        version = await read_service_version(sock, timeout=1.0)
        async with Client(build_server(sock, timeout=1.0, service_version=version)) as client:
            instructions = client.instructions
            listed = await _call(client, "guides_list")
        async with Client(build_server(sock, timeout=1.0)) as client:
            lazily = await _call(client, "guide_read", {"name": "budget"})
            health = await _call(client, "health")
    assert version == "0.0.0"
    assert instructions.endswith(NOTICE)
    assert [item.text for item in listed.content][1:] == [NOTICE]
    assert [item.text for item in lazily.content][1:] == [NOTICE]
    assert health.is_error is True
    assert health.structured_content["error"]["code"] == "invalid_service_response"


async def test_an_empty_or_missing_lab_version_yields_no_notice(tmp_path):
    for health in ({**_health(), "lab_version": ""}, {"schema": "lab-service-health/1"}):
        transport = FakeTransport({"/v1/health": health})
        async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=transport)) as client:
            listed = await _call(client, "guides_list")
        assert len(listed.content) == 1
