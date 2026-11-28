"""Shared helpers for the panel tests (Tasks 2-5)."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import shutil
import socket
import tempfile
from typing import AsyncIterator
from urllib.parse import unquote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from evolution_lab.panel.app import PanelConfig, cookie_name, create_panel_app

HOST = "p0123456789abcdef.localhost"
CONFIG_DEFAULTS = {"token": "k" * 43, "host": HOST, "port": 4321, "started_at": "2026-09-29T00:00:00Z"}


def make_config(tmp_path: Path, **overrides) -> PanelConfig:
    values = {**CONFIG_DEFAULTS, "home": tmp_path / "home", "socket_path": tmp_path / "service.sock", **overrides}
    return PanelConfig(**values)


def _with_raw_path(app, raw_path: bytes, *, drop_host: bool = False):
    """httpx normalizes ``.``/``..`` segments in URLs (``//`` and ``%2F`` survive), so the exact
    raw path under test is injected into the ASGI scope, as a real server would deliver it."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "raw_path": raw_path, "path": unquote(raw_path.decode("latin-1"))}
            if drop_host:  # httpx fills Host from the URL; make the header truly absent
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"host"]
        return await app(scope, receive, send)
    return wrapped


async def request(app, method, path, *, host=f"{HOST}:4321", cookie=True, token=None, headers=None):
    """Send one request through ASGITransport with the raw path preserved verbatim."""
    config_token = token if token is not None else CONFIG_DEFAULTS["token"]
    merged = dict(headers or {})
    if host:
        merged["host"] = host
    if cookie:
        merged.setdefault("cookie", f"{cookie_name(4321)}={config_token}")
    raw_path = path.split("?", 1)[0].encode("latin-1")
    transport = httpx.ASGITransport(app=_with_raw_path(app, raw_path, drop_host=not host))
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.request(method, f"http://{host or 'x'}{path}", headers=merged)


class RecordingUpstream(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


def _server(app, **kwargs) -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False,
                                         ws="none", timeout_graceful_shutdown=2, **kwargs))


async def _started(server: uvicorn.Server, task: asyncio.Task) -> None:
    for _ in range(500):
        if server.started:
            return
        if task.done():
            await task
            raise RuntimeError("uvicorn exited during startup")
        await asyncio.sleep(0.01)
    raise RuntimeError("uvicorn did not start")


async def _stop(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    await task


@contextlib.asynccontextmanager
async def uds_service(app, socket_path: Path | None = None) -> AsyncIterator[Path]:
    """Run ``app`` under uvicorn on a Unix socket in a short /tmp directory (sun_path limit).

    With an explicit ``socket_path`` the service comes back on the same socket; its parent
    directory is recreated if an earlier ``uds_service`` already removed it."""
    if socket_path is None:
        directory = Path(tempfile.mkdtemp(prefix="pnl-", dir="/tmp"))
        path, created = directory / "s.sock", True
    else:
        path = Path(socket_path)
        directory, created = path.parent, not path.parent.exists()
        if created:
            directory.mkdir(mode=0o700)
    try:
        server = _server(app, uds=str(path))
        task = asyncio.create_task(server.serve())
        try:
            await _started(server, task)
            yield path
        finally:
            await _stop(server, task)
    finally:
        if created:
            shutil.rmtree(directory, ignore_errors=True)


@contextlib.asynccontextmanager
async def panel_server(config_factory) -> AsyncIterator[tuple[str, PanelConfig]]:
    """Run the panel on 127.0.0.1 with a real port; ``config_factory(port)`` builds its config.

    The yielded base URL uses the launch host name, as a browser would; ``*.localhost`` resolves
    to ::1 first here, and the client falls back to 127.0.0.1 where the panel listens."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = config_factory(port)
    app = create_panel_app(config)
    server = _server(app)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _started(server, task)
        yield f"http://{config.host}:{port}", config
    finally:
        await _stop(server, task)
        await app.client.aclose()
        listener.close()


class _Events(Response):
    """SSE like the service's: a status frame, then a frame every 50 ms up to 5 frames, then the
    stream stays open until the client goes away. A disconnect is recorded in ``disconnected``."""

    def __init__(self, disconnected: asyncio.Event):
        super().__init__(media_type="text/event-stream", headers={"Cache-Control": "no-store"})
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self._disconnected = disconnected

    async def __call__(self, scope, receive, send):
        async def watch():
            while (await receive())["type"] != "http.disconnect":
                pass
            self._disconnected.set()

        watcher = asyncio.create_task(watch())
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": b"event: status\ndata: {}\n\n", "more_body": True})
            for index in range(1, 5):
                await asyncio.sleep(0.05)
                if watcher.done():
                    break
                await send({"type": "http.response.body", "body": f"event: tick\ndata: {index}\n\n".encode(),
                            "more_body": True})
            await watcher
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def fake_service() -> Starlette:
    async def health(request: Request):
        return JSONResponse({"schema": "lab-service-health/2"})

    async def operations(request: Request):
        return JSONResponse({"query": request.scope["query_string"].decode("latin-1"),
                             "accept": request.headers.get("accept"), "cookie": request.headers.get("cookie"),
                             "host": request.headers.get("host"),
                             "last_event_id": request.headers.get("last-event-id")})

    async def missing(request: Request):
        return JSONResponse({"error": {"code": "not_found", "stage": "service", "message": "No such operation."}},
                            status_code=404)

    async def report_markdown(request: Request):
        return Response("# Кампания\n".encode("utf-8"), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="report-op1.md"'})

    async def events(request: Request):
        return _Events(request.app.state.disconnected)

    app = Starlette(routes=[Route("/v1/health", health), Route("/v1/operations", operations),
                            Route("/v1/operations/missing", missing),
                            Route("/v1/operations/{operation_id}/report.md", report_markdown),
                            Route("/v1/operations/{operation_id}/events", events)])
    app.state.disconnected = asyncio.Event()
    return app
