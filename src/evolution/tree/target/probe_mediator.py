"""Bounded asyncio loopback bridge from external roles to ``ProbeSession``."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import re
import secrets
from collections.abc import Mapping
from typing import Final, cast

from evolution.tree.target.probe_session import ProbeResult, ProbeSession, ProbeSessionAborted
from evolution.tree.target.probe_wire import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES

__all__ = [
    "MAX_ACTIVE_HANDLERS",
    "MAX_HEADER_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "ProbeMediator",
]

MAX_ACTIVE_HANDLERS: Final = 8
MAX_HEADER_BYTES: Final = 16 * 1024
_LISTEN_BACKLOG: Final = MAX_ACTIVE_HANDLERS
_READ_TIMEOUT_SECONDS: Final = 5.0
_WRITE_TIMEOUT_SECONDS: Final = 1.0
_DEFAULT_CLOSE_GRACE_SECONDS: Final = 5.0
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,256}")
_REJECTED: Final[ProbeResult] = {
    "error": "rejected",
    "detail": "probe request rejected",
}


class _FramingRejected(ValueError):
    pass


def _capability(value: str | None) -> str:
    selected = f"cap_{secrets.token_urlsafe(32)}" if value is None else value
    if type(selected) is not str or _CAPABILITY.fullmatch(selected) is None:
        raise ValueError("probe capability is invalid")
    return selected


def _json_object(raw: bytes) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _FramingRejected
            result[key] = value
        return result

    def constant(_value: str) -> object:
        raise _FramingRejected

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=constant
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise _FramingRejected from exc


def _header_values(lines: list[bytes]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in lines:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise _FramingRejected
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise _FramingRejected from exc
        if not name or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in name
        ):
            raise _FramingRejected
        result.setdefault(name, []).append(value)
    return result


class ProbeMediator:
    """One capability endpoint, optionally bound after listener preparation."""

    def __init__(
        self,
        session: ProbeSession | None = None,
        *,
        capability: str | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._capability: str | None = _capability(capability)
        self._path: str | None = f"/{self._capability}/probe"
        self._session = session
        self._bound = session is not None
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._url: str | None = None
        self._started = False
        self._closed = False
        self._internal_fault = False
        self._handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._transport_closers: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[bool] | None = None

    def _ensure_loop(self) -> None:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is not self._loop:
            raise RuntimeError("probe mediator used from a non-owning event loop")

    @property
    def capability(self) -> str:
        if self._capability is None:
            raise RuntimeError("probe mediator is closed")
        return self._capability

    @property
    def path(self) -> str:
        if self._path is None:
            raise RuntimeError("probe mediator is closed")
        return self._path

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("probe mediator is not started")
        return self._url

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("probe mediator is not started")
        return self._port

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_handlers(self) -> int:
        return len(self._handlers)

    async def start(self) -> str:
        """Prepare the listener; binding a deadline-owned session may happen later."""

        self._ensure_loop()
        if self._closed:
            raise RuntimeError("probe mediator is closed")
        if self._started:
            return self.url
        try:
            self._server = await asyncio.start_server(
                self._accept,
                "127.0.0.1",
                0,
                backlog=_LISTEN_BACKLOG,
                limit=MAX_HEADER_BYTES + 1,
            )
            socket = self._server.sockets[0]
            self._port = cast(tuple[str, int], socket.getsockname())[:2][1]
            self._url = f"http://127.0.0.1:{self._port}{self.path}"
            self._started = True
            return self._url
        except BaseException:
            self._closed = True
            self._capability = None
            self._path = None
            if self._session is not None:
                self._session.abort()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            raise

    def bind_session(self, session: ProbeSession) -> None:
        """Bind exactly one session after its shared role deadline has begun."""

        self._ensure_loop()
        if self._closed:
            raise RuntimeError("probe mediator is closed")
        if not self._started:
            raise RuntimeError("probe mediator is not started")
        if self._bound:
            raise RuntimeError("probe mediator is already bound")
        if not isinstance(session, ProbeSession):
            raise TypeError("probe session must be a ProbeSession")
        self._session = session
        self._bound = True

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        if self._closed or self._session is None or len(self._handlers) >= MAX_ACTIVE_HANDLERS:
            writer.close()
            if len(self._transport_closers) >= MAX_ACTIVE_HANDLERS:
                self.abort()
                return
            closer = self._loop.create_task(self._finish_writer(writer))
            self._transport_closers.add(closer)
            closer.add_done_callback(self._transport_done)
            return
        task = self._loop.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handler_done)

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._internal_fault = True
            self.abort()

    def _transport_done(self, task: asyncio.Task[None]) -> None:
        self._transport_closers.discard(task)
        if not task.cancelled():
            task.exception()

    async def _finish_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.wait_closed()
        finally:
            self._writers.discard(writer)

    async def _read_request(self, reader: asyncio.StreamReader) -> object:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                block = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as exc:
            raise _FramingRejected from exc
        if len(block) > MAX_HEADER_BYTES:
            raise _FramingRejected
        lines = block[:-4].split(b"\r\n")
        try:
            method, target, version = lines[0].split(b" ")
        except ValueError as exc:
            raise _FramingRejected from exc
        if method != b"POST" or version not in {b"HTTP/1.0", b"HTTP/1.1"}:
            raise _FramingRejected
        expected = self._path
        if expected is None or not hmac.compare_digest(target, expected.encode("ascii")):
            raise PermissionError
        headers = _header_values(lines[1:])
        if "transfer-encoding" in headers:
            raise _FramingRejected
        lengths = headers.get("content-length", [])
        content_types = headers.get("content-type", [])
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or len(content_types) != 1
            or content_types[0].lower() != "application/json"
        ):
            raise _FramingRejected
        if len(lengths[0]) > len(str(MAX_REQUEST_BYTES)):
            raise _FramingRejected
        try:
            size = int(lengths[0])
        except ValueError as exc:
            raise _FramingRejected from exc
        if size > MAX_REQUEST_BYTES:
            raise _FramingRejected
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                body = await reader.readexactly(size)
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            raise _FramingRejected from exc
        return _json_object(body)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                value = await self._read_request(reader)
            except PermissionError:
                return
            except _FramingRejected:
                await self._send(writer, _REJECTED, status=400)
                return
            session = self._session
            if self._closed or session is None:
                return
            # Content-Length frames exactly one request. EOF (including a send
            # half-close) or more bytes abandons it; clients keep their send side
            # open until the response. Cancellation removes queued admission,
            # while ProbeSession shields any already dispatched execution.
            if reader.at_eof():
                return
            request = self._loop.create_task(session.request(value))

            async def watch_peer() -> None:
                try:
                    await reader.read(1)
                finally:
                    request.cancel()

            peer = self._loop.create_task(watch_peer())
            try:
                try:
                    result = await request
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if peer.done() and current is not None and not current.cancelling():
                        return
                    raise
            except ProbeSessionAborted:
                self.abort()
                return
            finally:
                request.cancel()
                peer.cancel()
                # Neither task owns dispatched transport. Both await only
                # cancellable local stream/lock operations, so joining them
                # cannot wait for the endpoint or leave a disconnect watcher.
                await asyncio.gather(request, peer, return_exceptions=True)
            if self._closed:
                return
            await self._send(writer, result, status=200)
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            closed = False
            with contextlib.suppress(OSError, TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
                    await writer.wait_closed()
                    closed = True
            if closed:
                self._writers.discard(writer)

    async def _send(
        self, writer: asyncio.StreamWriter, payload: Mapping[str, object], *, status: int
    ) -> None:
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise RuntimeError("probe mediator response exceeded its fixed bound")
        reason = "OK" if status == 200 else "Bad Request"
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(encoded)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + encoded
        )
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            await writer.drain()

    def abort(self) -> None:
        """Immediately expire access, close sockets, and cut off bound transport."""

        self._ensure_loop()
        if self._closed:
            return
        self._closed = True
        self._capability = None
        self._path = None
        if self._server is not None:
            self._server.close()
        for writer in tuple(self._writers):
            writer.close()
        if self._session is not None:
            self._session.abort()

    async def close(self, *, grace_seconds: float = _DEFAULT_CLOSE_GRACE_SECONDS) -> bool:
        """Expire the listener and drain the bound session and admitted handlers."""

        self._ensure_loop()
        if (
            type(grace_seconds) not in (int, float)
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("probe mediator close grace is invalid")
        if self._close_task is None or (self._close_task.done() and not self._close_task.result()):
            self.abort()
            self._close_task = self._loop.create_task(self._close_impl(float(grace_seconds)))
        return await asyncio.shield(self._close_task)

    async def _close_impl(self, grace_seconds: float) -> bool:
        deadline = self._loop.time() + grace_seconds
        owned_waiters: set[asyncio.Task[object]] = set()
        server_waiter: asyncio.Task[object] | None = None
        if self._server is not None:
            server_waiter = self._loop.create_task(self._server.wait_closed())
            owned_waiters.add(server_waiter)
        session_waiter: asyncio.Task[object] | None = None
        if self._session is not None:
            remaining = max(0.0, deadline - self._loop.time())
            session_waiter = self._loop.create_task(self._session.close(grace_seconds=remaining))
            owned_waiters.add(session_waiter)
        current = asyncio.current_task()
        handlers = tuple(task for task in self._handlers if task is not current)
        closers = tuple(self._transport_closers)

        async def wait_transports() -> None:
            for writer in tuple(self._writers):
                await writer.wait_closed()
                self._writers.discard(writer)

        transport_waiter: asyncio.Task[object] | None = None
        if self._writers:
            transport_waiter = self._loop.create_task(wait_transports())
            owned_waiters.add(transport_waiter)
        waiters = {*owned_waiters, *handlers, *closers}
        if waiters:
            done, pending = await asyncio.wait(
                waiters, timeout=max(0.0, deadline - self._loop.time())
            )
        else:
            done, pending = set(), set()

        session_closed = self._session is None
        if session_waiter is not None and session_waiter in done:
            try:
                session_closed = session_waiter.result() is True
            except BaseException:
                session_closed = False

        def completed_without_error(task: asyncio.Task[object] | None) -> bool:
            if task is None:
                return True
            if task not in done:
                return False
            try:
                task.result()
            except BaseException:
                return False
            return True

        server_closed = completed_without_error(server_waiter)
        handlers_closed = all(
            task in done and not task.cancelled() and task.exception() is None for task in handlers
        )
        transports_closed = (
            all(
                task in done and not task.cancelled() and task.exception() is None
                for task in closers
            )
            and completed_without_error(transport_waiter)
            and not self._writers
        )
        for task in owned_waiters & pending:
            task.cancel()
        if owned_waiters & pending:
            await asyncio.gather(*(owned_waiters & pending), return_exceptions=True)
        closed = (
            server_closed
            and session_closed
            and handlers_closed
            and transports_closed
            and not self._internal_fault
        )
        if not closed and self._session is not None:
            self._session.revoke_event_authority()
        return closed
