"""Async UDS transport: values, envelopes, deadline and cancellation (PDR-0061)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from evolution_lab.commands._core import Request
from evolution_lab.commands.errors import LocalFailure, ServiceFailure
from evolution_lab.mcp.transport import AsyncServiceTransport

from panel_support import uds_service

ENVELOPE = {"error": {"code": "invalid_request", "stage": "transport", "message": "The service request is invalid."}}
REF = {"schema": "lab-task-ref/1", "id": "t1", "owner_generation": "g1"}
disconnected: list[bool] = []


async def ref(request):
    return JSONResponse(REF)


async def envelope(request):
    return JSONResponse(ENVELOPE, status_code=404)


async def bad_gateway(request):
    return PlainTextResponse("bad gateway", status_code=502)


async def wrong(request):
    return JSONResponse({"schema": "other/1"})


async def stall(request):
    for _ in range(50):
        await asyncio.sleep(0.1)
        if await request.is_disconnected():
            disconnected.append(True)
            return Response()
    return JSONResponse(REF)


async def drip(request):
    async def body():
        for _ in range(25):
            yield b" "
            await asyncio.sleep(0.2)
    return StreamingResponse(body(), media_type="application/json")


async def markdown(request):
    return PlainTextResponse("# Report\n", media_type="text/markdown")


async def blob(request):
    return Response(b"PK\x00\x01", media_type="application/zip")


async def health(request):
    return JSONResponse(REF)


def raw(body: bytes, status=200):
    async def handler(request):
        return Response(body, status_code=status, media_type="application/json")
    return handler


NESTED = b"[" * 100000 + b"]" * 100000
RAW = {"/env200": raw(__import__("json").dumps(ENVELOPE).encode()), "/list": raw(b"[1]"), "/nan": raw(b'{"a": NaN}'),
       "/dup": raw(b'{"a": 1, "a": 2}'), "/deep": raw(NESTED)}

APP = Starlette(routes=[Route(p, f) for p, f in [*RAW.items(),
    ("/ref", ref), ("/envelope", envelope), ("/bad", bad_gateway), ("/wrong", wrong),
    ("/stall", stall), ("/drip", drip), ("/md", markdown), ("/zip", blob), ("/health", health)]])


def req(path, schema="lab-task-ref/1", kind="json", content_type=None):
    return Request("GET", path, schema, kind=kind, content_type=content_type)


async def test_json_read_returns_service_value():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        assert await client.call(req("/ref")) == REF
        await client.aclose()


async def test_service_error_envelope_passes_through_whole():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        with pytest.raises(ServiceFailure) as info:
            await client.call(req("/envelope"))
        assert info.value.envelope == ENVELOPE
        await client.aclose()


async def test_plain_text_502_is_invalid_service_response():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        with pytest.raises(LocalFailure) as info:
            await client.call(req("/bad"))
        assert info.value.envelope["error"]["code"] == "invalid_service_response"
        assert info.value.envelope["error"]["stage"] == "client"
        assert "different versions" in info.value.hint
        await client.aclose()


async def test_schema_mismatch_is_invalid_service_response():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        with pytest.raises(LocalFailure) as info:
            await client.call(req("/wrong"))
        assert info.value.envelope["error"]["code"] == "invalid_service_response"
        await client.aclose()


async def test_missing_socket_is_service_unavailable_with_socket_in_hint(tmp_path):
    sock = tmp_path / "none.sock"
    client = AsyncServiceTransport(sock, timeout=2)
    with pytest.raises(LocalFailure) as info:
        await client.call(req("/ref"))
    assert info.value.envelope["error"]["code"] == "service_unavailable"
    assert info.value.envelope["error"]["stage"] == "client"
    assert str(sock) in info.value.hint
    await client.aclose()


async def test_stalled_response_times_out_at_deadline():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=0.5)
        start = time.monotonic()
        with pytest.raises(LocalFailure) as info:
            await client.call(req("/stall"))
        assert time.monotonic() - start < 2
        assert info.value.envelope == {"error": {
            "code": "request_timeout", "stage": "transport", "message": "The service request timed out."}}
        await client.aclose()


async def test_slow_streaming_body_times_out_at_wall_clock():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=1.0)
        start = time.monotonic()
        with pytest.raises(LocalFailure) as info:
            await client.call(req("/drip"))
        assert time.monotonic() - start < 2.5
        assert info.value.envelope["error"]["code"] == "request_timeout"
        await client.aclose()


async def test_cancelled_call_closes_connection_and_next_call_succeeds():
    disconnected.clear()
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=5)
        with anyio.move_on_after(0.4) as scope:
            await client.call(req("/stall"))
        assert scope.cancelled_caught
        assert await client.call(req("/health")) == REF
        for _ in range(20):
            if disconnected:
                break
            await asyncio.sleep(0.1)
        assert disconnected
        await client.aclose()


async def test_markdown_text_kind():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        assert await client.call(req("/md", None, "text", "text/markdown")) == "# Report\n"
        await client.aclose()


async def test_schema_bytes_kind():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        assert await client.call(req("/zip", None, "bytes", "application/zip")) == b"PK\x00\x01"
        await client.aclose()


async def test_call_after_timeout_succeeds():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=0.5)
        with pytest.raises(LocalFailure):
            await client.call(req("/stall"))
        assert await client.call(req("/health")) == REF
        await client.aclose()


async def test_2xx_error_envelope_is_service_failure():
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        with pytest.raises(ServiceFailure) as info:
            await client.call(req("/env200"))
        assert info.value.envelope == ENVELOPE
        await client.aclose()


@pytest.mark.parametrize("path", ["/list", "/nan", "/dup", "/deep"])
async def test_malformed_json_is_invalid_service_response(path):
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        with pytest.raises(LocalFailure) as info:
            await client.call(req(path, None))
        assert info.value.envelope["error"]["code"] == "invalid_service_response"
        await client.aclose()


@pytest.mark.parametrize(("path", "kind"), [("/zip", "text"), ("/md", "bytes")])
async def test_wrong_media_type_is_invalid_service_response(path, kind):
    async with uds_service(APP) as sock:
        client = AsyncServiceTransport(sock, timeout=2)
        expected = "text/markdown" if kind == "text" else "application/zip"
        with pytest.raises(LocalFailure) as info:
            await client.call(req(path, None, kind, expected))
        assert info.value.envelope["error"]["code"] == "invalid_service_response"
        await client.aclose()
