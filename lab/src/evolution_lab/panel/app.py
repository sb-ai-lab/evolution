"""The panel's ASGI app: Host -> method -> token -> route, then static, health or proxy.

Nothing here reads the Lab home or parses service JSON (ADR-0018 p. 1).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from . import allowlist

ERRORS = {
    "misdirected_request": (421, "The panel answers only on its own launch host name and port."),
    "method_not_allowed": (405, "The panel is read-only."),
    "forbidden": (403, "Open the panel with the link printed by lab panel."),
    "not_found": (404, "The panel does not serve this path."),
    "service_unavailable": (503, "The Lab service is not reachable."),
}
HEALTH_SCHEMA = "lab-panel-health/1"
CONNECT_TIMEOUT, READ_TIMEOUT, HEALTH_TIMEOUT = 5.0, 60.0, 2.0
_PASSED_RESPONSE_HEADERS = (b"content-type",)
# Any transport failure before the response headers: connect errors, a stale keep-alive after a
# service restart, pool or read timeouts. All mean "the service did not answer".
_UNREACHABLE = (httpx.TransportError, OSError)
SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


@dataclass(frozen=True)
class PanelConfig:
    token: str
    host: str  # the per-launch p<random>.localhost name (ADR-0018 p. 3, amended)
    port: int
    started_at: str
    home: Path
    socket_path: Path


def cookie_name(port: int) -> str:
    return f"lab_panel_{port}"


def _hint(config) -> str:
    return f"lab serve --home {config.home}"


def _header(scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value
    return None


def _cookie(scope, name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if raw is None:
        return None
    for part in raw.decode("latin-1").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _same(given: str | None, token: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), token.encode())


async def send_error(send, code: str, **fields) -> None:
    status, message = ERRORS[code]
    body = json.dumps({"error": {"code": code, "stage": "panel", "message": message}, **fields},
                      separators=(",", ":")).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        *SECURITY_HEADERS, (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def create_panel_app(config: PanelConfig, *, upstream=None, static_dir: Path | None = None):
    hosts = {f"{config.host}:{config.port}".encode()}
    transport = upstream or httpx.AsyncHTTPTransport(uds=str(config.socket_path), retries=0)
    client = httpx.AsyncClient(transport=transport, base_url="http://lab", trust_env=False,
                               follow_redirects=False)
    static = Path(__file__).parent / "static" if static_dir is None else static_dir
    # Closed map, built once: a request path is looked up verbatim, never resolved against the disk.
    files = {"/static/" + p.relative_to(static).as_posix(): p for p in static.rglob("*") if p.is_file()}
    files["/"] = static / "index.html"

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            # uvicorn runs with lifespan='off'; nothing to do.
            return
        if scope["type"] != "http":
            return
        if _header(scope, b"host") not in hosts:
            return await send_error(send, "misdirected_request")
        if scope["method"] not in {"GET", "HEAD"}:
            return await send_error(send, "method_not_allowed")
        raw_path = scope.get("raw_path") or scope["path"].encode("utf-8")
        path = raw_path.decode("latin-1")
        query = scope.get("query_string", b"")
        if path == "/" and b"token=" in query:
            given = parse_qs(query.decode("latin-1")).get("token", [None])[0]
            if not _same(given, config.token):
                return await send_error(send, "forbidden")
            # Host-only on purpose: no Domain attribute, so the cookie goes back to this launch
            # host name alone (ADR-0018 p. 3, amended).
            await send({"type": "http.response.start", "status": 303, "headers": [
                *SECURITY_HEADERS, (b"location", b"/"), (b"content-length", b"0"),
                (b"set-cookie", f"{cookie_name(config.port)}={config.token}; HttpOnly; SameSite=Strict; Path=/".encode())]})
            return await send({"type": "http.response.body", "body": b""})
        if not _same(_cookie(scope, cookie_name(config.port)), config.token):
            return await send_error(send, "forbidden")
        if path == "/api/health":
            return await _health(config, client, send)
        if path == "/" or path.startswith("/static/"):
            return await _static(files, path, scope, send)
        if path.startswith("/v1/") and allowlist.allowed(path):
            return await _proxy(client, config, scope, raw_path, query, receive, send)
        return await send_error(send, "not_found")

    app.client = client  # server.py closes it on shutdown
    return app


async def _health(config, client, send):
    try:
        response = await client.get("/v1/health", timeout=HEALTH_TIMEOUT)
        service = {"state": "ok"} if response.status_code == 200 else None
    except (httpx.HTTPError, OSError):
        service = None
    if service is None:
        service = {"state": "service_unavailable", "hint": _hint(config)}
    body = json.dumps({"schema": HEALTH_SCHEMA, "started_at": config.started_at, "home": str(config.home),
                       "service": service}, separators=(",", ":")).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [
        *SECURITY_HEADERS, (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}


async def _static(files, path, scope, send):
    file = files.get(path)
    if file is None:
        return await send_error(send, "not_found")
    body = file.read_bytes()
    kind = _CONTENT_TYPES.get(file.suffix, "text/plain; charset=utf-8")
    await send({"type": "http.response.start", "status": 200, "headers": [
        *SECURITY_HEADERS, (b"content-type", kind.encode()), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})


async def _proxy(client, config, scope, raw_path, query, receive, send):
    target = raw_path + (b"?" + query if query else b"")
    accept = _header(scope, b"accept")
    headers = {"accept": accept.decode("latin-1")} if accept else {}
    streaming = raw_path.endswith(b"/events")
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=None if streaming else READ_TIMEOUT,
                            write=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT)
    request = client.build_request(scope["method"], httpx.URL("http://lab").copy_with(raw_path=target),
                                   headers=headers, timeout=timeout)
    try:
        response = await client.send(request, stream=True)
    except _UNREACHABLE:
        return await send_error(send, "service_unavailable", hint=_hint(config))

    async def disconnected():
        # uvicorn's send() returns silently once the browser is gone; only receive() says so.
        while (await receive())["type"] != "http.disconnect":
            pass

    async def relay():
        passed = [(key.encode(), value.encode()) for key, value in response.headers.items()
                  if key.encode() in _PASSED_RESPONSE_HEADERS]
        await send({"type": "http.response.start", "status": response.status_code,
                    "headers": [*SECURITY_HEADERS, *passed]})
        async for chunk in response.aiter_bytes():  # decoded: content-encoding is not passed on
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    tasks = [asyncio.create_task(relay()), asyncio.create_task(disconnected())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await response.aclose()  # closes the stream to the service
    # OSError/httpx.HTTPError: the browser or the service went away mid-body; nothing more to send.
    # CancelledError: relay() was stopped because the browser disconnected.
    if isinstance(results[0], Exception) and not isinstance(results[0], (OSError, httpx.HTTPError)):
        raise results[0]
