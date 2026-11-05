"""The transport half of the reference `/evolution/v2` target.

The transport consumes a dispatch protocol. The state machine imports this
module; this module does not import the state machine. It validates HTTP
framing, calls dispatch and writes the response.

The framing hooks live here for the same reason: `slow_drip_seconds`,
`slow_drip_headers_seconds`, `chunked_response`, `response_content_type` and
`error_content_type` are properties of how the bytes leave, not of what they
say. The three drip and chunk hooks cover the three places a non-conforming
target can stall a caller — the status line and headers, a chunked reply's size
lines, and the body — so the adapter's absolute worker deadline can be proven
against each of them; the last two answer a well-formed body under the wrong
media type, which the contract forbids and the adapter must refuse — on every response or
on the refusals alone.
"""

from __future__ import annotations

import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from evolution.tree.execution.wire_v2 import canonical_body

# The transport and configuration admission share one loopback set.
from evolution.tree.target.config import _LOOPBACK_HOSTS as LOOPBACK_HOSTS

RETRYABLE: frozenset[str] = frozenset({"instances_exhausted", "internal", "not_ready"})


class _DispatchTarget(Protocol):
    @property
    def max_input_bytes(self) -> int: ...

    @property
    def misbehave(self) -> Any: ...

    def dispatch(self, method: str, path: str, body: bytes) -> tuple[int, bytes]: ...


def _error_payload(code: str, detail: str) -> bytes:
    return canonical_body(
        {"error": {"code": code, "message": detail, "retryable": code in RETRYABLE}}
    )


_CHUNKS = 4

# A refused body is discarded, never kept: at most this many bytes, for at most
# this long, before the connection closes as it did before. Constants,
# not flags (PDR-0004): the bound only has to outlast a well-behaved client.
_DRAIN_BYTES = 64 << 20
_DRAIN_SECONDS = 2.0
_DRAIN_CHUNK = 65536


class _TargetServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], target: _DispatchTarget) -> None:
        self.target = target
        # `::1` is a loopback host the manifest loader accepts; the stdlib
        # server is IPv4 unless told otherwise, and `TCPServer.__init__` reads
        # the instance attribute, so it must be set before `super().__init__`.
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, _Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client hanging up mid-exchange is not a fixture error.

        `dispatch` turns every application failure into a typed wire error, so the
        only exceptions that reach here are transport teardowns — chiefly a
        client that resets while a request is read or a reply is written. The
        drain of a refused body handles its own resets and never gets here. The
        default prints a traceback to stderr, which would make every conforming
        run look like a crash; anything that is not an
        `OSError` still surfaces.
        """
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    """The thinnest possible glue: length guard, body read, dispatch, write."""

    protocol_version = "HTTP/1.1"
    server_version = "evolution-reference-target/2"
    # Built lazily by `send_response_only` and undeclared in typeshed; annotated
    # here because `_end_headers` writes what `end_headers` would have flushed.
    _headers_buffer: list[bytes]

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log (a fixture must not pollute test output)."""

    @property
    def _target(self) -> _DispatchTarget:
        server = self.server
        assert isinstance(server, _TargetServer)
        return server.target

    def _handle(self, method: str) -> None:
        target = self._target
        raw_length = self.headers.get("Content-Length")
        length = 0
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                length = -1
        if length < 0:
            self._reply(400, _error_payload("invalid_request", "content_length"), close=True)
            self._drain_refused_body()
            return
        if length > target.max_input_bytes:
            # Refused before reading: the body is never pulled into memory —
            # it is discarded in bounded chunks so the refusal is not lost to
            # a reset — and the connection is not reused.
            self._reply(413, _error_payload("invalid_request", "content_length"), close=True)
            self._drain_refused_body()
            return
        body = self.rfile.read(length) if length else b""
        status, payload = target.dispatch(method, self.path, body)
        # Every 200, not only a turn: the adapter's `response_too_large` and
        # read-timeout paths must be provable on create and identity too.
        misbehave = target.misbehave
        ok = status == 200
        self._reply(
            status,
            payload,
            drip=misbehave.slow_drip_seconds if ok else 0.0,
            header_drip=misbehave.slow_drip_headers_seconds if ok else 0.0,
            chunked=misbehave.chunked_response and ok,
        )

    def _reply(
        self,
        status: int,
        payload: bytes,
        *,
        close: bool = False,
        drip: float = 0.0,
        header_drip: float = 0.0,
        chunked: bool = False,
    ) -> None:
        self.send_response(status)
        # A hook, not a constant: the contract requires `application/json`, and the
        # adapter must be provable against a target that answers valid JSON
        # under another media type. `error_content_type` narrows that to the
        # refusals alone, which only a probe that *expects* an error can catch.
        misbehave = self._target.misbehave
        media = misbehave.response_content_type
        if status != 200 and misbehave.error_content_type is not None:
            media = misbehave.error_content_type
        self.send_header("Content-Type", media)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(payload)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self._end_headers(header_drip)
        if chunked:
            self._write_chunked(payload, drip)
            return
        if drip > 0:
            self._drip(payload, drip)
            return
        self.wfile.write(payload)

    def _drain_refused_body(self) -> None:
        """Half-close, then read and discard what the client is still sending.

        Closing a socket with unread input makes the kernel reset the
        connection, and the reset can reach the client before it has read the
        refusal: the client sees a broken write and no answer (RFC 9112 §9.6).
        Shutting the write side first sends the refusal and a FIN; draining
        lets the client's write finish, so the close that follows sends no
        reset. Past `_DRAIN_BYTES` or `_DRAIN_SECONDS` the connection closes as
        before, and a client that reads the answer after a broken write (the
        adapter, from 0.3.0a29) still gets it.
        """
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + _DRAIN_SECONDS
        drained = 0
        try:
            while drained < _DRAIN_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.connection.settimeout(remaining)
                data = self.rfile.read1(_DRAIN_CHUNK)
                if not data:
                    return
                drained += len(data)
        except OSError:  # a timeout or the client's own reset ends the drain
            return

    def _end_headers(self, header_drip: float) -> None:
        """`end_headers` flushes the status line and the whole header block in
        one write; dripping it instead proves the caller bounds the bytes
        `getresponse()` reads before a body exists at all."""
        if header_drip <= 0:
            self.end_headers()
            return
        self._headers_buffer.append(b"\r\n")
        head = b"".join(self._headers_buffer)
        self._headers_buffer = []
        self._drip(head, header_drip)

    def _drip(self, raw: bytes, seconds: float) -> None:
        for index in range(len(raw)):
            self.wfile.write(raw[index : index + 1])
            self.wfile.flush()
            time.sleep(seconds)

    def _write_chunked(self, payload: bytes, drip: float) -> None:
        """`_CHUNKS` pieces, so the reply carries real chunk framing; the
        size lines drip when the fixture was asked to drip, because those are the
        bytes `read1` never sees and `HTTPResponse` reads with `readline`."""
        size = max(1, -(-len(payload) // _CHUNKS))
        for start in range(0, len(payload), size):
            piece = payload[start : start + size]
            line = b"%x\r\n" % len(piece)
            if drip > 0:
                self._drip(line, drip)
            else:
                self.wfile.write(line)
            self.wfile.write(piece + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")


def serve(target: _DispatchTarget, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Bind a threading HTTP server for `target`; the caller runs `serve_forever`.

    `port=0` takes an ephemeral port, readable afterwards as
    `server.server_address[1]`. Threads are daemons, so `server_close` never
    blocks on a client that left a keep-alive connection open.

    Run it as `Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})`:
    the stdlib default polls every 0.5 s, which is 0.5 s of dead time in every
    `shutdown()` and the dominant cost of a test suite that starts a target per
    case.

    `host` may be `127.0.0.1`, `localhost` or `::1`; the address family follows
    the host. Any other host — including `0.0.0.0`, `::` or a LAN address —
    is refused before the socket is ever constructed, so a misconfigured
    caller cannot bind a target that non-loopback network peers could reach.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError("reference target host must be loopback: 127.0.0.1, localhost or ::1")
    return _TargetServer((host, port), target)
