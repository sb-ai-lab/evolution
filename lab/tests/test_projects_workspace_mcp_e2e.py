"""The workspace's generated ``.mcp.json`` starts a working Lab MCP server (the real client path)."""
from __future__ import annotations

import json

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from evolution_lab.mcp.tools import PUBLISHED  # noqa: E402
from evolution_lab.projects.models import Pair  # noqa: E402
from evolution_lab.projects.registry import Projects  # noqa: E402
from evolution_lab.projects.workspace import SERVER_NAME, Launcher, build_workspace  # noqa: E402
from panel_fixtures import load_snapshot  # noqa: E402
from panel_support import uds_service  # noqa: E402
from test_mcp_server import _health  # noqa: E402

pytestmark = pytest.mark.slow


def _app(status: dict) -> Starlette:
    async def health(request):
        return JSONResponse(_health())

    async def project_status(request):
        return JSONResponse(status)

    return Starlette(routes=[Route("/v1/health", health), Route("/v1/projects/demo/status", project_status)])


async def _exercise(workspace, env=None):
    entry = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    params = StdioServerParameters(command=entry["command"], args=entry["args"], cwd=workspace, env=env)
    with anyio.fail_after(60):
        async with Client(params) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            health = await client.call_tool("health", {})
            status = await client.call_tool("project_status", {"project": "demo"})
    return names, health, status


@pytest.mark.asyncio
@pytest.mark.parametrize("poisoned", [False, True], ids=["plain", "poisoned-pythonpath"])
async def test_server_from_mcp_json_reads_the_workspace_service(tmp_path, poisoned):
    tmp_path.chmod(0o700)
    status = load_snapshot("final")["routes"]["/v1/projects/demo/status"]["body"]
    wc = tmp_path / "wc"
    (wc / "lab").mkdir(parents=True)
    (wc / "agent").mkdir()
    env = None
    if poisoned:  # -E makes the server ignore PYTHONPATH, so this shadowing package is never imported
        fake = tmp_path / "fake" / "evolution_lab"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text("raise RuntimeError('PYTHONPATH leaked into the server')\n")
        env = {"PYTHONPATH": str(fake.parent)}
    home = tmp_path / "home"
    async with uds_service(_app(status)) as sock:
        with Projects.open(home) as projects:
            project = projects.create(id="demo", name="Demo", path=str(wc), materials_root="lab",
                                      implementations=[{"id": "agent", "place": "agent"}])
        workspace = build_workspace(home, project, pair=Pair("0.0.3.dev28", "0.3.0a17"), socket_path=sock,
                                    now="t", launcher=Launcher.current(), with_mcp=True)
        names, health, result = await _exercise(workspace, env)
    assert names == {spec.name for spec in PUBLISHED}
    assert health.is_error is not True and health.structured_content == _health()
    assert result.is_error is not True and result.structured_content == status
