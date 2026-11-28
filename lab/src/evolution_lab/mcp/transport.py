"""Async UDS transport for ``lab mcp``: one request under a wall-clock deadline, cancelled by anyio (PDR-0061)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

import anyio
import httpx

from evolution_lab.commands._core import Request, check_response
from evolution_lab.commands.errors import LocalFailure, ServiceFailure

INVALID_HINT = "the Lab service and this lab mcp may be different versions"


def transport_failure(request: Request, code: str, hint: str) -> LocalFailure:
    if request.mutation_key is not None:
        return LocalFailure("acceptance_unknown",
                            hint="Read the current state before retrying. If retrying, use the same request ID and Owner generation.")
    return LocalFailure(code, hint=hint)


def log_reason(what: str, error: BaseException, *, trace: bool = False) -> None:
    """One line on stderr naming why a generic result was returned; stdout belongs to the MCP protocol."""
    try:
        sys.stderr.write(f"lab mcp: {what}: {type(error).__name__}: {error}\n")
        if trace:
            traceback.print_exception(error, file=sys.stderr)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - logging never fails a call
        pass


class AsyncServiceTransport:
    """Sends command ``Request`` values to the Lab service; never retries."""

    def __init__(self, socket_path: Path, *, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=str(self.socket_path), retries=0),
            base_url="http://lab", trust_env=False, timeout=httpx.Timeout(None, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, request: Request) -> dict | str | bytes:
        headers: dict[str, str] = {}
        content = None
        if request.payload is not None:
            headers["content-type"] = "application/json"
            content = json.dumps(request.payload, separators=(",", ":"), allow_nan=False).encode()
        if request.mutation_key is not None:
            headers["lab-request-id"], headers["lab-owner-generation"] = request.mutation_key
        try:
            with anyio.fail_after(self.timeout):
                async with self._client.stream(request.method, request.path, headers=headers,
                                               content=content) as response:
                    body = await response.aread()
                    status_ok = response.is_success
                    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        except (TimeoutError, httpx.TimeoutException):
            raise transport_failure(request, "request_timeout", "retry") from None
        except (httpx.ConnectError, FileNotFoundError, ConnectionRefusedError):
            raise transport_failure(request, "service_unavailable", self._unavailable_hint()) from None
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            raise transport_failure(request, "request_interrupted", "retry") from None
        except httpx.DecodingError:
            raise LocalFailure("invalid_service_response", hint=INVALID_HINT) from None
        except httpx.HTTPError:
            raise transport_failure(request, "service_unavailable", self._unavailable_hint()) from None
        return self._decode(request, status_ok, media, body)

    def _unavailable_hint(self) -> str:
        return f"start the service: lab serve --state DIR (socket {self.socket_path})"

    @staticmethod
    def _decode(request: Request, status_ok: bool, media: str, body: bytes) -> dict | str | bytes:
        from evolution_lab.cli.responses import error as is_error_envelope
        from evolution_lab.service.codecs import decode_json_object

        invalid = LocalFailure("invalid_service_response", hint=INVALID_HINT)
        raw_ok = status_ok and request.kind != "json"
        value = None
        if not raw_ok:
            try:
                value = decode_json_object(body, max_bytes=None)
            except Exception as error:
                log_reason("invalid_service_response (undecodable body)", error)
                raise invalid from None
            if is_error_envelope(value):
                raise ServiceFailure(value)
            if not status_ok:
                log_reason("invalid_service_response", ValueError("non-success status without an error envelope"))
                raise invalid
            try:
                return check_response(request, value)
            except Exception as error:
                log_reason("invalid_service_response (schema check)", error)
                raise invalid from None
        if media != (request.content_type or "").lower():
            raise invalid
        if request.kind == "bytes":
            return body
        try:
            return body.decode("utf-8")
        except UnicodeError:
            raise invalid from None
