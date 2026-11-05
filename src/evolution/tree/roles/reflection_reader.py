"""Campaign-owned, operation-scoped read dispatch for reflection roles.

The reader deliberately knows nothing about evidence, documents, grouping, or
the filesystem.  A campaign builds a :class:`FrozenReflectionView` from its
already-verified facts and registers small, allowlisted handlers.  Native users
and loopback clients then go through exactly the same admission, integrity,
argument, activity-summary, and lifecycle code.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final, cast
from urllib.parse import urlparse

GROUPING_PAGE_SIZE: Final = 128
CONTENT_CHARS: Final = 16_000

MAX_ARGUMENT_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 128 * 1024
MAX_STREAM_FRAME_BYTES: Final = 128 * 1024
MAX_SUMMARY_ENTRIES: Final = 256
MAX_SUMMARY_BYTES: Final = 64 * 1024
BODY_CACHE_BYTES: Final = 64 * 1024 * 1024

__all__ = [
    "BODY_CACHE_BYTES",
    "MAX_ARGUMENT_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_STREAM_FRAME_BYTES",
    "MAX_SUMMARY_BYTES",
    "MAX_SUMMARY_ENTRIES",
    "ActivitySummary",
    "ByteLruCache",
    "ClientDescriptor",
    "FrozenReflectionView",
    "ReaderArgumentError",
    "ReaderError",
    "ReaderIntegrityError",
    "ReflectionReadSession",
]


class ReaderError(ValueError):
    """A bounded reader protocol failure safe to expose to a role."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReaderArgumentError(ReaderError):
    """An ordinary recoverable client argument or lifecycle error."""


class ReaderIntegrityError(ReaderError):
    """An immutable-source/control-plane error which poisons this operation."""


@dataclass(frozen=True)
class ActivitySummary:
    """Bounded observed interface activity, with no request or body content."""

    entries: tuple[Mapping[str, str], ...]
    omitted_reads: int
    omitted_errors: int
    byte_size: int

    def to_payload(self) -> dict[str, object]:
        """Return the complete durable summary envelope, without body data."""

        return {
            "entries": [dict(entry) for entry in self.entries],
            "omitted_reads": self.omitted_reads,
            "omitted_errors": self.omitted_errors,
        }


class ByteLruCache:
    """A disposable byte-bounded LRU suitable for lazily read immutable bodies."""

    def __init__(self, capacity: int = BODY_CACHE_BYTES) -> None:
        if type(capacity) is not int or capacity < 0:
            raise ValueError("cache_capacity")
        self.capacity = capacity
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        if type(key) is not str or not isinstance(value, bytes):
            raise TypeError("cache_key_or_value")
        if len(value) > self.capacity:
            return
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._size -= len(previous)
            self._items[key] = value
            self._size += len(value)
            while self._size > self.capacity:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


Handler = Callable[[Mapping[str, Any]], Any]
IntegrityCheck = Callable[[], None]


class FrozenReflectionView:
    """Verified campaign read view with operation-owned handler registrations.

    ``handlers`` is deliberately the only data boundary in this core.  Handler
    closures retain source locators inside the campaign process; clients receive
    only an ephemeral URL, capability, and operation method names.
    """

    def __init__(
        self,
        handlers: Mapping[str, Handler],
        *,
        integrity_check: IntegrityCheck | None = None,
        body_cache: ByteLruCache | None = None,
    ) -> None:
        if not handlers or any(
            not _valid_operation(name) or not callable(fn) for name, fn in handlers.items()
        ):
            raise ValueError("reader_handlers")
        self._handlers = dict(handlers)
        self._integrity_check = integrity_check
        self.body_cache = body_cache or ByteLruCache()

    def open_operation(
        self, operation_id: str, methods: set[str] | frozenset[str]
    ) -> ReflectionReadSession:
        if not _valid_operation_id(operation_id):
            raise ValueError("operation_id")
        allowed = frozenset(methods)
        if not allowed or not allowed <= self._handlers.keys():
            raise ValueError("operation_methods")
        return ReflectionReadSession(self, operation_id, allowed)


@dataclass(frozen=True)
class ClientDescriptor:
    """Runtime-only external client material. Never include it in semantic IDs."""

    url: str
    capability: str
    methods: tuple[str, ...]


class ReflectionReadSession:
    """One operation's native/HTTP facade and sticky integrity-failure owner."""

    def __init__(
        self, view: FrozenReflectionView, operation_id: str, methods: frozenset[str]
    ) -> None:
        self.operation_id = operation_id
        self.methods = methods
        self._view = view
        # Prefix makes the opaque capability safe as a positional command-line
        # value even when ``token_urlsafe`` itself begins with a dash.
        self._capability = f"cap_{secrets.token_urlsafe(32)}"
        self._closed = False
        self._integrity_fault: str | None = None
        self._entries: list[Mapping[str, str]] = []
        self._summary_bytes = 0
        self._omitted_reads = 0
        self._omitted_errors = 0
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._active_streams: list[Any] = []
        self._server: _LoopbackServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def integrity_fault(self) -> str | None:
        return self._integrity_fault

    @property
    def summary(self) -> ActivitySummary:
        with self._lock:
            payload = self._summary_payload()
            return ActivitySummary(
                tuple(self._entries),
                self._omitted_reads,
                self._omitted_errors,
                _summary_size(payload),
            )

    def verify_admission(self, check: Callable[[], None]) -> None:
        """Run one operation admission check and make integrity faults sticky."""

        from evolution.tree.baseline_diagnostics import DiagnosticCorruptionError

        with self._lock:
            self._ensure_open()
            if self._integrity_fault is not None:
                raise ReaderIntegrityError(self._integrity_fault)
            try:
                check()
            except DiagnosticCorruptionError as exc:
                self._integrity_fault = "diagnostic_bundle_changed"
                self._record("admission", {}, None, self._integrity_fault)
                raise ReaderIntegrityError(self._integrity_fault) from exc
            except ReaderIntegrityError as exc:
                self._integrity_fault = exc.code
                self._record("admission", {}, None, exc.code)
                raise

    def __enter__(self) -> ReflectionReadSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def dispatch(
        self, method: str, arguments: Mapping[str, Any], *, capability: str | None = None
    ) -> Any:
        """Dispatch a finite JSON response through the same path as HTTP clients."""

        result = self._call(method, arguments, capability=capability)
        try:
            _json_bytes(result, MAX_RESPONSE_BYTES, "response_too_large")
        except ReaderArgumentError as exc:
            with self._lock:
                self._record(method, arguments, None, exc.code)
            raise
        with self._lock:
            self._record(method, arguments, result, None)
        return result

    def stream(
        self, method: str, arguments: Mapping[str, Any], *, capability: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield bounded NDJSON-equivalent frames without aggregate buffering."""

        from evolution.tree.baseline_diagnostics import DiagnosticCorruptionError

        result = self._call(method, arguments, capability=capability)
        if isinstance(result, Mapping) or isinstance(result, (str, bytes)):
            with self._lock:
                self._record(method, arguments, None, "not_streaming_operation")
            raise ReaderArgumentError("not_streaming_operation")
        try:
            iterator = iter(result)
        except TypeError as exc:
            with self._lock:
                self._record(method, arguments, None, "not_streaming_operation")
            raise ReaderArgumentError("not_streaming_operation") from exc
        with self._lock:
            self._active_streams.append(iterator)
        digest = hashlib.sha256()

        def frame(payload: dict[str, Any]) -> dict[str, Any]:
            encoded = _json_bytes(payload, MAX_STREAM_FRAME_BYTES, "stream_frame_too_large")
            digest.update(encoded)
            digest.update(b"\n")
            return payload

        try:
            yield frame({"kind": "header", "operation": method})
            while True:
                with self._lock:
                    self._ensure_open()
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                except DiagnosticCorruptionError as exc:
                    with self._lock:
                        self._integrity_fault = "diagnostic_bundle_changed"
                        self._record(method, arguments, None, self._integrity_fault)
                    raise ReaderIntegrityError(self._integrity_fault) from exc
                except ReaderIntegrityError as exc:
                    with self._lock:
                        self._integrity_fault = exc.code
                        self._record(method, arguments, None, exc.code)
                    raise
                except ReaderError as exc:
                    with self._lock:
                        self._record(method, arguments, None, exc.code)
                    raise
                except Exception:
                    with self._lock:
                        self._record(method, arguments, None, "handler_failed")
                    raise ReaderArgumentError("handler_failed") from None
                try:
                    with self._lock:
                        self._ensure_open()
                    yield frame({"kind": "data", "value": value})
                except ReaderArgumentError as exc:
                    with self._lock:
                        self._record(method, arguments, None, exc.code)
                    raise
            with self._lock:
                self._ensure_open()
            yield frame({"kind": "final", "operation": method})
            with self._lock:
                self._record(method, arguments, None, None, response_digest=digest.hexdigest())
        finally:
            closer = getattr(iterator, "close", None)
            if callable(closer):
                try:
                    closer()
                except (RuntimeError, ValueError):
                    pass
            with self._lock:
                if iterator in self._active_streams:
                    self._active_streams.remove(iterator)

    def client_descriptor(self) -> ClientDescriptor:
        """Start the operation's loopback transport once and return its capability."""

        with self._lock:
            self._ensure_open()
            if self._server is None:
                self._server = _LoopbackServer(("127.0.0.1", 0), _RequestHandler, self)
                self._thread = threading.Thread(
                    target=self._server.serve_forever,
                    name=f"reflection-reader-{self.operation_id}",
                    daemon=True,
                )
                self._thread.start()
            host, port = self._server.server_address[:2]
            if isinstance(host, bytes):
                host = host.decode("ascii")
            return ClientDescriptor(
                f"http://{host}:{port}/read", self._capability, tuple(sorted(self.methods))
            )

    def close(self) -> None:
        """Invalidate capability and stop the mediator without late frames.

        ``close`` closes all active loopback sockets and iterator objects it can
        close immediately.  Python cannot asynchronously interrupt arbitrary
        handler code already blocked inside ``next(iterator)``; such a handler
        must return or observe its own cancellation mechanism.  Once it does,
        this session checks its closed state before emitting another frame and
        closes the iterator in ``finally``, so it cannot publish late data.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancelled.set()
            server, thread = self._server, self._thread
            streams = tuple(self._active_streams)
            self._server = None
            self._thread = None
        for stream in streams:
            closer = getattr(stream, "close", None)
            if callable(closer):
                try:
                    closer()
                except (RuntimeError, ValueError):
                    # A generator executing on the mediator thread observes
                    # ``_closed`` before its next frame; it cannot be closed
                    # re-entrantly from this thread.
                    pass
        if server is not None:
            server.close_active_connections()
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._view.body_cache.clear()

    def _call(self, method: str, arguments: Mapping[str, Any], *, capability: str | None) -> Any:
        from evolution.tree.baseline_diagnostics import DiagnosticCorruptionError

        with self._lock:
            self._ensure_open()
            if capability is not None and not secrets.compare_digest(capability, self._capability):
                self._record(method, arguments, None, "bad_capability")
                raise ReaderArgumentError("bad_capability")
            if self._integrity_fault is not None:
                self._record(method, arguments, None, self._integrity_fault)
                raise ReaderIntegrityError(self._integrity_fault)
            self._validate(method, arguments)
            try:
                if self._view._integrity_check is not None:
                    self._view._integrity_check()
                result = self._view._handlers[method](arguments)
            except DiagnosticCorruptionError as exc:
                self._integrity_fault = "diagnostic_bundle_changed"
                self._record(method, arguments, None, self._integrity_fault)
                raise ReaderIntegrityError(self._integrity_fault) from exc
            except ReaderIntegrityError as exc:
                self._integrity_fault = exc.code
                self._record(method, arguments, None, exc.code)
                raise
            except ReaderError as exc:
                self._record(method, arguments, None, exc.code)
                raise
            except Exception:
                self._record(method, arguments, None, "handler_failed")
                raise ReaderArgumentError("handler_failed") from None
            return result

    def _ensure_open(self) -> None:
        if self._closed:
            raise ReaderArgumentError("operation_closed")

    def _validate(self, method: str, arguments: Mapping[str, Any]) -> None:
        if not _valid_operation(method) or method not in self.methods:
            self._record(method, arguments, None, "unknown_operation")
            raise ReaderArgumentError("unknown_operation")
        if not isinstance(arguments, Mapping):
            self._record(method, {}, None, "invalid_arguments")
            raise ReaderArgumentError("invalid_arguments")
        try:
            _reject_routing_arguments(arguments)
            _json_bytes(arguments, MAX_ARGUMENT_BYTES, "arguments_too_large")
        except ReaderArgumentError as exc:
            self._record(method, arguments, None, exc.code)
            raise

    def _record(
        self,
        method: str,
        arguments: Mapping[str, Any],
        response: Any,
        error: str | None,
        *,
        response_digest: str | None = None,
    ) -> None:
        argument_digest = _digest_or_empty(arguments)
        entry: dict[str, str] = {
            "operation": method[:128],
            "arguments_digest": argument_digest,
            **_activity_fields(arguments),
        }
        if error is None:
            entry["response_digest"] = response_digest or _digest_or_empty(response)
        else:
            entry["error"] = error[:128]
        _json_bytes(entry, MAX_SUMMARY_BYTES, "summary_entry")
        candidate = self._summary_payload(entries=[*self._entries, entry])
        if (
            len(self._entries) >= MAX_SUMMARY_ENTRIES
            or _summary_size(candidate) > MAX_SUMMARY_BYTES
        ):
            self._omit_summary_entry(error)
            return
        self._entries.append(entry)
        self._summary_bytes = _summary_size(self._summary_payload())

    def _summary_payload(
        self, *, entries: list[Mapping[str, str]] | None = None
    ) -> dict[str, object]:
        selected = self._entries if entries is None else entries
        return {
            "entries": [dict(entry) for entry in selected],
            "omitted_reads": self._omitted_reads,
            "omitted_errors": self._omitted_errors,
        }

    def _omit_summary_entry(self, error: str | None) -> None:
        if error is None:
            self._omitted_reads += 1
        else:
            self._omitted_errors += 1
        while self._entries and _summary_size(self._summary_payload()) > MAX_SUMMARY_BYTES:
            dropped = self._entries.pop()
            self._omit_summary_entry(dropped.get("error"))
        self._summary_bytes = _summary_size(self._summary_payload())


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[_RequestHandler],
        session: ReflectionReadSession,
    ):
        super().__init__(address, handler)
        self.session = session
        self._connections: set[Any] = set()
        self._connections_lock = threading.Lock()

    def add_connection(self, connection: Any) -> None:
        with self._connections_lock:
            self._connections.add(connection)

    def remove_connection(self, connection: Any) -> None:
        with self._connections_lock:
            self._connections.discard(connection)

    def close_active_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(2)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        cast(_LoopbackServer, self.server).add_connection(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            cast(_LoopbackServer, self.server).remove_connection(self.connection)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/read":
            self._error(HTTPStatus.NOT_FOUND, ReaderArgumentError("unknown_endpoint"))
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if size < 0 or size > MAX_ARGUMENT_BYTES + 1024:
                raise ReaderArgumentError("arguments_too_large")
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "operation",
                "arguments",
                "stream",
            }:
                raise ReaderArgumentError("invalid_request")
            capability = self.headers.get("Authorization", "").removeprefix("Bearer ")
            method = payload["operation"]
            arguments = payload["arguments"]
            streaming = payload["stream"]
            if type(method) is not str or type(streaming) is not bool:
                raise ReaderArgumentError("invalid_request")
            server = cast(_LoopbackServer, self.server)
            if streaming:
                self._stream(server.session.stream(method, arguments, capability=capability))
            else:
                result = server.session.dispatch(method, arguments, capability=capability)
                self._json(HTTPStatus.OK, result)
        except ReaderIntegrityError as exc:
            self._error(HTTPStatus.CONFLICT, exc)
        except ReaderArgumentError as exc:
            status = (
                HTTPStatus.FORBIDDEN if exc.code == "bad_capability" else HTTPStatus.BAD_REQUEST
            )
            self._error(status, exc)
        except (UnicodeDecodeError, ValueError, TypeError):
            self._error(HTTPStatus.BAD_REQUEST, ReaderArgumentError("invalid_request"))

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        encoded = _json_bytes(payload, MAX_RESPONSE_BYTES, "response_too_large")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _stream(self, frames: Iterator[dict[str, Any]]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for frame in frames:
                self.wfile.write(
                    _json_bytes(frame, MAX_STREAM_FRAME_BYTES, "stream_frame_too_large") + b"\n"
                )
                self.wfile.flush()
        except ReaderIntegrityError as exc:
            self.wfile.write(
                _json_bytes(
                    {"kind": "error", "error": {"code": exc.code}, "integrity": True},
                    MAX_STREAM_FRAME_BYTES,
                    "error",
                )
                + b"\n"
            )
            self.wfile.flush()
        except ReaderArgumentError as exc:
            self.wfile.write(
                _json_bytes(
                    {"kind": "error", "error": {"code": exc.code}, "integrity": False},
                    MAX_STREAM_FRAME_BYTES,
                    "error",
                )
                + b"\n"
            )
            self.wfile.flush()
        self.close_connection = True

    def _error(self, status: HTTPStatus, error: ReaderError) -> None:
        self._json(status, {"error": {"code": error.code}})

    def log_message(self, _format: str, *_args: object) -> None:
        """The reader is internal transport; HTTP access logs are not role output."""


def _valid_operation(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 128
        and value.replace("_", "").isalnum()
    )


def _valid_operation_id(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 128
        and value.replace("_", "").replace("-", "").isalnum()
    )


def _reject_routing_arguments(arguments: Mapping[str, Any]) -> None:
    """Keep client JSON declarative: IDs/ranges only, never locations/routes."""

    forbidden = ("path", "url", "destination", "directory", "filename", "host")
    for key in arguments:
        if type(key) is not str or any(word in key.casefold() for word in forbidden):
            raise ReaderArgumentError("forbidden_routing_argument")


def _activity_fields(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Retain useful read coordinates, never request bodies or route data."""

    allowed = frozenset(
        {
            "record_id",
            "history_id",
            "document_id",
            "evidence_id",
            "index",
            "offset",
            "count",
            "start",
            "stop",
            "section",
        }
    )
    fields: dict[str, str] = {}
    for key in allowed & arguments.keys():
        value = arguments[key]
        if type(value) in {str, int} and len(str(value)) <= 128:
            fields[key] = str(value)
    return fields


def _json_bytes(value: Any, limit: int, error: str) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReaderArgumentError(error) from exc
    if len(encoded) > limit:
        raise ReaderArgumentError(error)
    return encoded


def _summary_size(value: object) -> int:
    """The exact canonical JSON size used for the durable summary envelope."""

    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def _digest_or_empty(value: Any) -> str:
    try:
        return hashlib.sha256(_json_bytes(value, MAX_RESPONSE_BYTES, "digest")).hexdigest()
    except ReaderArgumentError:
        return "unavailable"
