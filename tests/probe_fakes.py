"""Real loopback fixtures for the probe session tests."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class ProbeCall:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        server: ProbeHTTPServer = self.server  # type: ignore[assignment]
        with server.lock:
            server.active += 1
            server.max_active = max(server.max_active, server.active)
            server.calls.append(
                ProbeCall(self.command, self.path, dict(self.headers.items()), body)
            )
            server.called.set()
        try:
            if self.path == "/drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path.startswith("/slow"):
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                time.sleep(server.delay)
                self._write(b"slow")
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.send_header("Content-Length", "8")
                self.end_headers()
                self._write(b"redirect")
                return
            if self.path == "/final":
                self._reply(200, b"followed")
                return
            if self.path == "/large":
                self._reply(200, b"x" * (1024 * 1024 + 4096))
                return
            if self.path == "/chunked":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._write(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
                return
            if self.path == "/invalid-utf8":
                self._reply(200, b"ok\xfftail")
                return
            if self.path == "/secret-response":
                self._reply(200, b"response has top-secret-value")
                return
            if self.path == "/secret-boundary":
                self._reply(200, b"x" * (64 * 1024 - 4) + b"top-secret-value")
                return
            if self.path == "/secret-transport-boundary":
                opening = b"<analysis>"
                ending = b"</analysis>top-"
                retained = opening + b"x" * (1024 * 1024 - len(opening) - len(ending)) + ending
                self._reply(200, retained + b"secret-value")
                return
            if self.path == "/secret-self-overlap-boundary":
                opening = b"<analysis>"
                ending = b"</analysis>abab"
                retained = opening + b"x" * (1024 * 1024 - len(opening) - len(ending)) + ending
                self._reply(200, retained + b"!")
                return
            if self.path == "/secret-cross-overlap-boundary":
                opening = b"<analysis>"
                ending = b"</analysis>tokenXYZ"
                retained = opening + b"x" * (1024 * 1024 - len(opening) - len(ending)) + ending
                self._reply(200, retained + b"!")
                return
            if self.path == "/secret-utf8-boundary":
                opening = b"<analysis>"
                ending = b"</analysis>top-\xe2"
                retained = opening + b"x" * (1024 * 1024 - len(opening) - len(ending)) + ending
                self._reply(200, retained + b"\x82\xacdone")
                return
            if self.path.startswith("/status/"):
                status = int(self.path.rsplit("/", 1)[-1])
                self._reply(status, f"status-{status}".encode())
                return
            if self.path == "/health-bad":
                self._reply(503, b"no")
                return
            if self.path == "/health":
                self._reply(204, b"")
                return
            self._reply(200, body or b"ok")
        finally:
            with server.lock:
                server.active -= 1

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    do_DELETE = _handle
    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle


class ProbeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, delay: float = 0.2) -> None:
        self.delay = delay
        self.calls: list[ProbeCall] = []
        self.called = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        super().__init__(("127.0.0.1", 0), _ProbeHandler)
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self) -> ProbeHTTPServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


class RefusingOrigin:
    """Reserve a loopback port without listening, so connect fails deterministically."""

    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.socket.getsockname()[1]}"

    def close(self) -> None:
        self.socket.close()


def decode_body(call: ProbeCall) -> Any:
    return json.loads(call.body)
