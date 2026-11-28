"""Catalogue tools against their authority (ADR-0070): the real Lab service app's routes and the shipped files.

The MCP session reads the service app in process: the server's own async transport over an ASGI transport, so
every byte goes through the same decoding as over the socket. Each tool's value is compared with the service's
response to the same route, fetched directly, and ``scenario_schema``'s text with the shipped schema file.
"""
from __future__ import annotations

import contextlib
import json

import httpx
import jsonschema
from mcp import Client

from evolution_lab import guides
from evolution_lab.mcp.server import build_server
from evolution_lab.mcp.tools import PUBLISHED, output_schema
from evolution_lab.mcp.transport import AsyncServiceTransport
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

SPECS = {spec.name: spec for spec in PUBLISHED}


@contextlib.asynccontextmanager
async def _session(tmp_path):
    """An MCP client over the real service app, and an HTTP client reading the same app directly."""
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2))
    runtime._admission_open = True
    app = create_app(runtime)
    transport = AsyncServiceTransport(tmp_path / "s.sock", timeout=10.0, transport=httpx.ASGITransport(app=app))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lab") as direct:
            async with Client(build_server(tmp_path / "s.sock", timeout=10.0, transport=transport)) as client:
                yield client, direct
    finally:
        await transport.aclose()


async def _value(client: Client, name: str, arguments: dict | None = None) -> dict:
    result = await client.call_tool(name, arguments or {})
    assert result.is_error is False, result.structured_content
    jsonschema.Draft202012Validator(output_schema(SPECS[name])).validate(result.structured_content)
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


async def _served(direct: httpx.AsyncClient, path: str) -> dict:
    response = await direct.get(path)
    assert response.status_code == 200
    return response.json()


async def test_scenarios_list_is_the_service_response(tmp_path):
    async with _session(tmp_path) as (client, direct):
        value = await _value(client, "scenarios_list")
        served = await _served(direct, "/v1/scenarios")
    assert value == served
    assert value == guides.scenarios().list_payload()


async def test_scenario_show_is_the_service_response_for_every_scenario(tmp_path):
    identifiers = [descriptor["id"] for descriptor in guides.scenarios().descriptors]
    assert identifiers
    async with _session(tmp_path) as (client, direct):
        for identifier in identifiers:
            value = await _value(client, "scenario_show", {"scenario": identifier})
            assert value == await _served(direct, f"/v1/scenarios/{identifier}"), identifier


async def test_scenario_schema_is_the_shipped_file_for_every_declared_schema(tmp_path):
    catalogue = guides.scenarios()
    declared = [(descriptor["id"], name) for descriptor in catalogue.descriptors
                for name in descriptor.get("schemas", {})]
    assert declared
    async with _session(tmp_path) as (client, direct):
        for identifier, name in declared:
            result = await client.call_tool("scenario_schema", {"scenario": identifier, "schema": name})
            assert result.is_error is False, result.structured_content
            shipped = catalogue.schema_path(identifier, name).read_bytes()
            served = await direct.get(f"/v1/scenarios/{identifier}/schemas/{name}")
            assert served.status_code == 200 and served.content == shipped
            assert result.structured_content == {"text": shipped.decode("utf-8")}
            assert [item.text for item in result.content] == [shipped.decode("utf-8")]


async def test_requirements_is_the_service_response(tmp_path):
    async with _session(tmp_path) as (client, direct):
        value = await _value(client, "requirements")
        served = await _served(direct, "/v1/guides/requirements")
    assert value == served
    assert value == guides.requirements()
