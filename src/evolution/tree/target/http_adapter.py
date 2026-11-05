"""`HttpSessionAdapter`, `HttpInstance` and the `/evolution/v2` transport.

The adapter is the only place where the R1 protocols meet a socket. Three rules
shape it:

*The rollout owns the clock and the lifecycle.* The transport bounds its own
worker (`connect_seconds + read_seconds`), but the authority over a call is the
rollout's `asyncio.timeout`; a callee timeout that fires first is an adapter
failure (ADR-0001), never a score. The adapter itself still does not retry:
`request_id`s exist for idempotent recovery and conformance replay, not for
hidden re-sends, so every `open`/`send` carries exactly the id the rollout
computed. What it does instead is classify -- every failure carries a `retryable`
flag -- and `rollout._external_call` is the one place that spends it on a retry
(I1, `call_failures.py`).

*Preflight freezes what materialize proves.* `preflight` reads `/health` once
and freezes the declared `SemanticIdentity`, the capabilities and the epoch
(`boot_id` plus the six semantic fields). S1 needs that identity before its
first journal write, so it must exist before any instance does. Every instance
create and get is then checked against the frozen epoch, and the attested
digests are compared by the transport-neutral `attestation.verify_attestation`.
The declared identity is a claim; the attestation is the proof.

*Failures travel as codes, never as text.* Every failure is a
`RolloutInfrastructureError` (or its `AttestationError` subclass) carrying a
closed `code`; messages are module literals. A wire body, a candidate root, a
public input or a session id never reaches an exception message, because a
caller may log it and the contract forbids that.

The worker-bound argument, since it is the subtle one: `request` runs in
`asyncio.to_thread`, and cancelling the awaiting coroutine does not cancel the
thread. So the thread carries its own absolute deadline, `connect_seconds +
read_seconds`, and it enforces it at the one place every received byte must
pass: the socket. `_DeadlineSocket` wraps `connection.sock` immediately after
`connect()`, and before every `recv`/`recv_into` it recomputes `remaining` and
either raises `TimeoutError` or sets the socket timeout to `min(read_seconds,
remaining)`.

The wrapper, rather than a check between calls, because `getresponse()` reads
the status line and the headers — and, on a chunked reply, every chunk-size
line — with `readline()` loops of raw `recv`s that consult nothing but the
socket timeout: a target dripping *faster* than `read_seconds` resets that
timeout on every byte and would hold the worker for as long as it liked, with
the deadline never consulted. `socket.makefile` would bind `HTTPResponse.fp` to
the *real* socket and step around the wrapper, so the wrapper hands out its own
buffered reader over its own `recv_into`.

The body is still read with `read1`, not `read`: `HTTPResponse.read(n)` loops
until it has `n` bytes or EOF, so `max_response_bytes` could only be checked
after the whole read had already been buffered; `read1(n)` performs at most one
raw read and returns what arrived. The worker therefore cannot outlive
`connect_seconds + read_seconds` by more than the one raw read already in
flight, which the socket timeout caps at `read_seconds`: a target that goes
silent, one dripping slower than `read_seconds`, one dripping faster, and one
dripping its status line, its headers or its chunk framing are all cut off.

*Settlement.* A transport built with a `TargetCallLedger` (ADR-0014) admits
each `POST` and `DELETE` before handing it to the worker, and the worker boxes
its own result so the coroutine can tell the worker's outcome from an
abandoned delivery (`CancelledError`, or an executor that never ran it). The
worker marks its I/O end right after `connection.close()` and before the
recorder. A body shorter than its `Content-Length` is still returned as it
arrived — the adapter's parser refuses it as before — but the ledger does not
count it as a response. A chunked body cut off before its last chunk, trailer
and final CRLF, and a reply cut before the empty line that ends its
header section, is an `IncompleteRead` `transport_error`
(`_FramingReader`).

*An answer before the body is written.* A Target may answer and close before
it has read the whole request — the reference target refuses an oversized body
with a 413 and closes — and the close can break the write still in progress
while the answer already sits in the socket. `_request` then still reads the
response through the same deadline-aware socket: a complete answer within
`max_response_bytes` is the Target's answer and the ledger counts it as a
response; anything else is the broken write's `transport_error`, as before
(ADR-0014).
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast, get_args
from urllib.parse import urlsplit
from uuid import uuid4

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactRef, ExpectedAttestation
from evolution.tree.artifacts.attestation import TargetPreflight, verify_attestation
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    AttestationError,
    InstanceProvenance,
    MaterializationStrategy,
    Message,
    RolloutInfrastructureError,
    SemanticIdentity,
    TargetIdentity,
    TargetSession,
    TurnResult,
)
from evolution.tree.execution.wire_v2 import (
    IDENTIFIER,
    PREFIX,
    PROTOCOL_VERSION,
    REQUIRED_ATTESTS,
    STRATEGY,
    Health,
    InstanceIdentity,
    canonical_body,
    is_instance_id,
    parse_close,
    parse_error,
    parse_health,
    parse_instance,
    parse_release,
    parse_session,
    parse_turn,
)
from evolution.tree.target.config import TargetBinding
from evolution.tree.target.target_calls import TargetCallLedger, _Call, network_outcome

_CHUNK = 65536
_KNOWN_STRATEGIES: frozenset[str] = frozenset(get_args(MaterializationStrategy))
_REQUIRED_ATTESTS = frozenset(REQUIRED_ATTESTS)
_T = TypeVar("_T")


class AdapterNotPreflighted(RolloutInfrastructureError):
    """An adapter call that needs the frozen preflight ran before `preflight`."""

    def __init__(self, message: str = "adapter has not completed preflight") -> None:
        super().__init__(message, "not_preflighted")


def _fail(message: str, code: str, retryable: bool = False) -> RolloutInfrastructureError:
    return RolloutInfrastructureError(message, code, retryable=retryable)


def _known_strategies(declared: tuple[str, ...]) -> frozenset[MaterializationStrategy]:
    """R1's strategy set is closed, so a strategy it cannot name is dropped rather
    than refused: it is one this core could never ask for anyway."""
    return frozenset(
        cast(MaterializationStrategy, strategy)
        for strategy in declared
        if strategy in _KNOWN_STRATEGIES
    )


def is_json_media(content_type: str) -> bool:
    """The contract requires `Content-Type: application/json` on every wire response.

    Only the media type is compared: parameters (`; charset=utf-8`) are part of
    a conforming header, and the type itself is case-insensitive.
    """
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _plain(value: Any) -> Any:
    """A JSON-encodable copy: R1 freezes boundary values into `MappingProxyType`
    and tuples, neither of which `json.dumps` can serialize."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class Exchange:
    """One completed request/response pair, handed to the transport's recorder.

    Recorded for every completed exchange, a non-200 included, so the wire
    conformance suite can replay the exact bytes that crossed the socket.
    """

    method: str
    path: str
    request_bytes: bytes
    status: int
    response_bytes: bytes
    content_type: str


class _DeadlineReader(io.RawIOBase):
    """The raw file `http.client` reads a response through.

    `socket.makefile` builds its reader on the *real* socket, so a response read
    through it would step around `_DeadlineSocket` entirely. This one performs
    exactly one armed `recv_into` per `readinto`.
    """

    def __init__(self, sock: _DeadlineSocket) -> None:
        super().__init__()
        self._sock = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        return self._sock.recv_into(buffer)


class _FramingReader(io.BufferedReader):
    """The buffered reader `http.client` reads a response through.

    `http.client` reads every framing line with `readline`: the status line and
    the header lines (`_read_status`, `parse_headers`), and a chunked body's
    chunk-size lines, last chunk and trailer section. It takes a line that ends
    at EOF as a whole line and an EOF as the empty line that ends a header block
    or a trailer section. So a reply cut anywhere in its header section looked
    complete when its body was empty or close-delimited, and a chunked
    body cut off at `0` or `0\\r\\n` looked complete too.

    A framing line that ends at EOF rather than at its LF therefore raises
    `IncompleteRead`, the error `http.client` itself raises for a body cut off
    mid-chunk. A bare LF still ends a line (RFC 9112 §2.2), as it does for
    `http.client`. A line cut off by the caller's size limit is left alone, so
    `http.client` still raises its own `LineTooLong`. One EOF is left alone too:
    one before the first byte of the reply, which `http.client` reports as
    `RemoteDisconnected` (the Target closed without answering).

    The body itself is read with `read1`/`read`, never `readline`, so a
    close-delimited body still ends at EOF. The CRLF after chunk data is read by
    `http.client`'s `_safe_read(2)` and is not validated (existing behavior).
    """

    _replied = False

    def readline(self, size: int | None = -1, /) -> bytes:
        line = super().readline(size)
        if not line.endswith(b"\n") and (self._replied or line):
            if size is None or size < 0 or len(line) < size:
                # `b""`, not `line`: wire bytes never ride on an exception.
                raise http.client.IncompleteRead(b"")
        self._replied = True
        return line


class _DeadlineSocket:
    """A socket view that will not start a read past the worker's deadline.

    Installed on `HTTPConnection.sock` after `connect()`, so the status line, the
    headers, a chunked reply's framing and the body all arrive through it. Every
    other attribute — `sendall`, `fileno`, `_decref_socketios` — is the real
    socket's; `close` is this class's own no-op (see below).

    `close` does *not* forward: `HTTPConnection.getresponse()` calls it as soon
    as the response decides it `will_close` — an HTTP/1.0 reply, or an HTTP/1.1
    one carrying `Connection: close`, with or without a `Content-Length` —
    before that reply's body has been read, because it hands the socket to the
    response and considers its own job done. Forwarding would close the real
    fd out from under the body still being read through this same wrapper.
    `_request` closes the real socket itself, once, after the body is read or
    the call has failed.
    """

    def __init__(self, sock: Any, deadline: float, read: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._read = read

    def _arm(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("callee deadline elapsed")
        self._sock.settimeout(min(self._read, remaining))

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        self._arm()
        return cast(bytes, self._sock.recv(*args, **kwargs))

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        self._arm()
        return cast(int, self._sock.recv_into(*args, **kwargs))

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def close(self) -> None:
        pass  # see the class docstring: the real close happens in `_request`

    def makefile(self, *args: Any, **kwargs: Any) -> io.BufferedReader:
        # `http.client` only ever asks for a buffered binary reader.
        return _FramingReader(_DeadlineReader(self), _CHUNK)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


def _read_response(
    connection: http.client.HTTPConnection, limit: int, *, length_framed: bool = False
) -> tuple[int, str, list[bytes], bool]:
    """`(status, media type, body chunks, complete)` for the response on
    `connection`; `complete` is whether the body reached the end of its
    HTTP/1.1 framing. A header section cut before the empty line that ends it
    raises `IncompleteRead` (`_FramingReader`): a close-delimited or empty
    body is complete only after an intact header section (RFC 9112 §8). With
    `length_framed` (the broken-write path, ADR-0014 as amended 2026-10-04), only
    a response with an explicit `Content-Length` header above zero, not chunked,
    whose whole body was read can be complete.

    A chunked body is complete only at the end of its framing — the last chunk,
    the trailer section and the final CRLF (RFC 9112 §7.1): its framing lines
    must end in LF (`_FramingReader`), and `http.client` closes the response only
    once it has read the last chunk and the trailer."""
    response = connection.getresponse()
    framed = isinstance(response.fp, _FramingReader)
    has_length = not response.chunked and response.length is not None
    if length_framed:
        declared = (response.msg.get("Content-Length") or "").strip()
        has_length = not response.chunked and declared.isdigit() and int(declared) > 0
    status = response.status
    content_type = response.getheader("Content-Type") or ""
    chunks: list[bytes] = []
    size = 0
    while True:
        # `read1`, never `read`: one raw read per call, so `limit` is
        # checked against what arrived rather than after the whole body.
        data = response.read1(_CHUNK)
        if not data:
            break
        size += len(data)
        if size > limit:
            raise _fail("target response exceeded the response limit", "response_too_large")
        chunks.append(data)
    # `read1` returns `b""` silently on an EOF before `Content-Length` is
    # satisfied; `length` is what is still owed (None when the body is chunked
    # or close-delimited, whose EOF is their own framing).
    complete = not response.length and (has_length or not length_framed)
    if response.chunked:
        # A chunked reply read without `_FramingReader` cannot prove its end.
        # `isclosed()` is defence in depth only: the read loop above ends a
        # chunked reply only after `http.client` closed it, so it is no second check.
        complete = complete and framed and response.isclosed()
    return status, content_type, chunks, complete


def _answer_after_broken_write(
    connection: http.client.HTTPConnection, limit: int, broken: ConnectionError
) -> tuple[int, str, list[bytes], bool]:
    """The response a Target sent before it stopped reading the request.

    HTTP lets a Target answer early and close — a 413 refuses the body unread —
    and the close can break the write still in progress while the answer
    already sits in the socket (RFC 9112 §9.6). Only an answer with an explicit
    `Content-Length` above zero, whose whole body was read within `limit`, is the
    Target's; anything else (no or zero length, 204/304, chunked, close-delimited,
    partial, oversize) is the broken write's own `transport_error`.
    """
    try:
        status, content_type, chunks, complete = _read_response(
            connection, limit, length_framed=True
        )
    except (OSError, http.client.HTTPException, RolloutInfrastructureError):
        complete = False
    if not complete:
        raise _fail("target transport failed", "transport_error", True) from broken
    return status, content_type, chunks, complete


class HttpTransport:
    """One `/evolution/v2` call per connection, bounded by the binding's timeouts."""

    def __init__(
        self,
        binding: TargetBinding,
        *,
        recorder: Callable[[Exchange], None] | None = None,
        ledger: TargetCallLedger | None = None,
    ) -> None:
        self.binding = binding
        self._recorder = recorder
        # Private and exposed read-only: a transport's ledger is fixed at
        # construction, so every call it makes is accounted by the ledger it
        # was bound to (ADR-0014).
        self._ledger = ledger
        if ledger is not None:
            ledger._bind_transport()
        # `TargetBinding` proved the loopback rule and normalized `base_url` to
        # `http://host:port` in its `__post_init__`, so there is nothing left to
        # check here; `hostname` unwraps an IPv6 literal (`[::1]` → `::1`), which
        # is what `HTTPConnection` wants.
        parts = urlsplit(binding.base_url)
        self.host = cast(str, parts.hostname)
        self.port = cast(int, parts.port)

    @property
    def ledger(self) -> TargetCallLedger | None:
        """The ledger bound at construction; it cannot be rebound."""
        return self._ledger

    async def request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> tuple[int, bytes, str]:
        """`(status, body bytes, response media type)`.

        The media type travels with the bytes rather than being enforced here:
        the adapter refuses anything but `application/json` on a body it parses,
        and the wire conformance suite asserts the header itself (check 1W).
        """
        # Read once and threaded down to the worker as an explicit argument.
        ledger = self._ledger
        if ledger is None:
            # No ledger, no admission accounting to protect: serialization runs
            # inside the worker thread exactly as at the base revision
            # (679f5714), so a caller without a ledger never blocks the event
            # loop on a large or slow-to-serialize body (ADR-0014).
            return await asyncio.to_thread(self._unledgered_request, method, path, body)
        # With a ledger bound, the body is serialized here, before the call is
        # admitted, so a call's open I/O — what `settle()` waits on — is socket
        # I/O only (ADR-0014).
        try:
            payload = b"" if body is None else canonical_body(body)
        except ValueError as exc:
            # Admission first, so a closed ledger's refusal takes precedence;
            # an admitted call never reached a socket.
            refused = ledger._admit(method, path, body)
            if refused is not None:
                ledger._io_end(refused, "not_sent", None)
                ledger._deliver(refused, abandoned=False)
            raise _fail("request body is not JSON-serializable", "invalid_request") from exc
        call = ledger._admit(method, path, body)
        if call is None:
            return await asyncio.to_thread(self._request, method, path, body, None, payload=payload)
        try:
            boxed = await asyncio.to_thread(
                self._settled_request, method, path, body, payload, call, ledger
            )
        except BaseException:
            ledger._deliver(call, abandoned=True)
            raise
        ledger._deliver(call, abandoned=False)
        if isinstance(boxed, BaseException):
            raise boxed
        return boxed

    def _unledgered_request(
        self, method: str, path: str, body: Mapping[str, Any] | None
    ) -> tuple[int, bytes, str]:
        """The no-ledger worker: serializes `body` here, inside the thread, the
        same way `_request` did at the base revision (679f5714), then calls
        `_request` with the already-serialized payload."""
        try:
            payload = b"" if body is None else canonical_body(body)
        except ValueError as exc:
            raise _fail("request body is not JSON-serializable", "invalid_request") from exc
        return self._request(method, path, body, None, payload=payload)

    def _settled_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        payload: bytes,
        call: _Call,
        ledger: TargetCallLedger,
    ) -> tuple[int, bytes, str] | BaseException:
        """The worker for a ledgered call: never raises, so an exception that
        reaches `request` is known not to be the worker's own."""
        if not ledger._start(call):
            return _fail("target call was withdrawn", "admission_closed")
        try:
            return self._request(method, path, body, call, ledger=ledger, payload=payload)
        except BaseException as exc:  # noqa: BLE001 - boxed for the coroutine
            return exc

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        call: _Call | None,
        *,
        ledger: TargetCallLedger | None = None,
        payload: bytes,
    ) -> tuple[int, bytes, str]:
        """The worker: `payload` is `body` already serialized, either by
        `request` on the loop (ledgered call, ADR-0014) or by
        `_unledgered_request` in this same thread (no ledger)."""
        sent = False
        complete = False
        status = 0
        content_type = ""
        chunks: list[bytes] = []
        try:
            connect = float(self.binding.timeouts.connect_seconds)
            read = float(self.binding.timeouts.read_seconds)
            limit = self.binding.max_response_bytes
            deadline = time.monotonic() + connect + read
            connection = http.client.HTTPConnection(self.host, self.port, timeout=connect)
            raw_sock: Any = None
            try:
                headers = {"Content-Type": "application/json"} if body is not None else {}
                # Connect first, then install the deadline-aware view: `getresponse`
                # reads the status line, the headers and a chunked reply's framing
                # long before `read1` is ever called, and those reads must be bounded
                # too (see the module note).
                connection.connect()
                sent = True  # from here request bytes may leave
                raw_sock = connection.sock
                connection.sock = cast(Any, _DeadlineSocket(raw_sock, deadline, read))
                try:
                    connection.request(method, path, body=payload, headers=headers)
                except ConnectionError as broken:
                    status, content_type, chunks, complete = _answer_after_broken_write(
                        connection, limit, broken
                    )
                else:
                    status, content_type, chunks, complete = _read_response(connection, limit)
            except TimeoutError as exc:  # `socket.timeout` is an alias since 3.10
                raise _fail(
                    "target call exceeded the callee deadline", "callee_timeout", True
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise _fail("target transport failed", "transport_error", True) from exc
            finally:
                # No redirects and no connection reuse: one call, one connection.
                # `_DeadlineSocket.close()` is a no-op (see its docstring), so the
                # real fd is closed here, once, after the body is read or the call
                # has failed — never by `getresponse()`'s early close of a
                # `will_close` reply, which would cut the body off mid-read.
                connection.close()
                if raw_sock is not None:
                    raw_sock.close()
        except BaseException as exc:
            if call is not None:
                cast(TargetCallLedger, ledger)._io_end(call, *network_outcome(sent, exc, False))
            raise
        if call is not None:
            cast(TargetCallLedger, ledger)._io_end(call, *network_outcome(sent, None, complete))
        received = b"".join(chunks)
        if self._recorder is not None:
            self._recorder(Exchange(method, path, payload, status, received, content_type))
        if 300 <= status < 400:
            raise _fail("target answered with a redirect", "invalid_response")
        return status, received, content_type


def _require_json_media(content_type: str) -> None:
    if not is_json_media(content_type):
        raise _fail("target answered under another media type", "invalid_response")


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _wire_error_or_none(status: int, obj: Any) -> Any:
    """`parse_error(status, obj)` when `obj` looks like a wire error body
    (a dict carrying an `"error"` key), `None` otherwise -- so a non-JSON or
    non-wire-shaped body never masquerades as the parser's own `internal`
    fallback for a malformed *known* error body."""
    if isinstance(obj, dict) and "error" in obj:
        return parse_error(status, obj)
    return None


async def _json_call(
    transport: HttpTransport,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
    parse: Callable[[Any], _T],
) -> _T:
    """One call whose 200 body must parse as a `/evolution/v2` document.

    The status is read before the media type: a non-200 whose body is not a
    wire error (wrong media type, or JSON without an `"error"` key) is a proxy
    or gateway answering on the target's behalf, not the target itself, so a
    `5xx`/`429` becomes `upstream_unavailable` (retryable) rather than
    `invalid_response`; any other status without a wire body is
    `invalid_response`. A wire error keeps its own code and `retryable`. A 200
    that does not parse is `invalid_response`, and a 200 requires JSON media.
    """
    status, raw, media = await transport.request(method, path, body)
    if status != 200:
        decoded = _decode(raw) if is_json_media(media) else None
        wire = _wire_error_or_none(status, decoded)
        if wire is None:
            if status >= 500 or status == 429:
                raise _fail(f"target answered HTTP {status}", "upstream_unavailable", True)
            raise _fail("target answered under another media type", "invalid_response")
        raise _fail(str(wire), wire.code, wire.retryable)
    _require_json_media(media)
    try:
        return parse(_decode(raw))
    except ValueError as exc:
        raise _fail("target returned a malformed body", "invalid_response") from exc


def _released_id(obj: Any) -> str:
    return parse_release(obj).instance_id


def _closed_id(obj: Any) -> str:
    return parse_close(obj).session_id


async def _delete(
    transport: HttpTransport,
    path: str,
    absent: str,
    resource_id: str,
    parse: Callable[[Any], str],
) -> None:
    """A delete whose *flag* carries no decision: `released`/`closed` false means
    the resource was already gone, which is the outcome the caller wanted.

    The *body* does carry one, and it is checked. The contract closes both delete
    responses (`{instance_id, released}`, `{session_id, closed}`), so a 200 that
    is empty, malformed, wrongly shaped or addressed to another id never showed
    that the target reached the resource this caller owns — and `release_target`
    would durably record a release that may not have happened. Anything but the
    closed body echoing `resource_id` is `invalid_response`.

    Only `absent` — the one wire code that says the addressed resource is not
    there — is tolerated. Every other refusal, a 404 `invalid_request` from a
    mis-addressed path included, is raised: silently reporting success for a
    delete that never reached the resource would leave it alive and owned. As
    in `_json_call`, a non-200 whose body is not a wire error is `5xx`/`429` →
    `upstream_unavailable` (retryable), else `invalid_response`.
    """
    status, raw, media = await transport.request("DELETE", path, None)
    if status == 200:
        _require_json_media(media)
        try:
            echoed = parse(_decode(raw))
        except ValueError as exc:
            raise _fail("target returned a malformed body", "invalid_response") from exc
        if echoed != resource_id:
            raise _fail("target answered for another resource", "invalid_response")
        return
    decoded = _decode(raw) if is_json_media(media) else None
    wire = _wire_error_or_none(status, decoded)
    if wire is None:
        if status >= 500 or status == 429:
            raise _fail(f"target answered HTTP {status}", "upstream_unavailable", True)
        raise _fail("target answered under another media type", "invalid_response")
    if status == 404 and wire.code == absent:
        return
    raise _fail(str(wire), wire.code, wire.retryable)


def _instances(instance_id: str) -> str:
    return f"{PREFIX}/instances/{instance_id}"


def _epoch_guard(reported: InstanceIdentity, boot_id: str, semantic: SemanticIdentity) -> None:
    """Prove the instance belongs to the preflighted epoch.

    All six semantic fields, `dependency_digest` included: ADR-0007 makes
    preflight the identity S1 journals and materialization its proof, so an
    instance reporting another dependency identity has left the frozen epoch
    whatever it goes on to attest.
    """
    if (
        reported.boot_id != boot_id
        or reported.contract_digest != semantic.contract_digest
        or reported.dependency_digest != semantic.dependency_digest
        or reported.settings_hash != semantic.settings_hash
        or reported.runtime_ref_digest != semantic.runtime_ref_digest
        or reported.seed_mode != semantic.seed_mode
        or reported.protocol_version != semantic.protocol_version
    ):
        raise _fail("target epoch changed since preflight", "target_epoch_changed")


def _identity_of(reported: InstanceIdentity) -> TargetIdentity:
    """Build the R1 identity from what the target reported, not from what the
    caller expected: the comparison is the point."""
    return TargetIdentity(
        SemanticIdentity(
            reported.contract_digest,
            reported.dependency_digest,
            reported.settings_hash,
            reported.seed_mode,
            reported.protocol_version,
            reported.runtime_ref_digest,
        ),
        InstanceProvenance(reported.boot_id, reported.loaded_candidate_digest),
        frozenset(reported.attested),
    )


@dataclass(frozen=True)
class HttpSession:
    """The R1 `TargetSession` shape over a `/evolution/v2` session."""

    session_id: str
    requested_seed: int | None
    effective_seed: int | None


class HttpInstance:
    """One materialized instance, bound to the identity materialization proved."""

    def __init__(
        self,
        transport: HttpTransport,
        instance_id: str,
        expected: TargetIdentity,
        description: Mapping[str, Any] | None = None,
        components: Mapping[str, Any] | None = None,
    ) -> None:
        self._transport = transport
        self.instance_id = instance_id
        #: The creation body's `description` (PDR-0014), captured once and held
        #: for the life of the handle. `identity()` may see the target repeat
        #: it on a later `GET`; this value is never replaced from there and the
        #: two are never compared, because a compared description would be
        #: identity by another name (ADR-0007).
        self.description = description
        #: The creation body's `components` (ADR-0024, G1), captured once like
        #: `description`: never replaced from a later `GET`, never compared,
        #: and not part of identity.
        self.components = components
        self._expected = expected
        self._attestation = ExpectedAttestation(
            expected.instance.loaded_candidate_digest, expected.semantic.dependency_digest
        )

    async def identity(self) -> TargetIdentity:
        reported = await _json_call(
            self._transport, "GET", _instances(self.instance_id), None, parse_instance
        )
        _epoch_guard(reported, self._expected.instance.boot_id, self._expected.semantic)
        identity = _identity_of(reported)
        verify_attestation(identity, self._attestation)
        return identity

    async def open(
        self, case_public: Mapping[str, Any], seed: int | None, request_id: str
    ) -> HttpSession:
        created = await _json_call(
            self._transport,
            "POST",
            f"{_instances(self.instance_id)}/sessions",
            {"request_id": request_id, "public": _plain(case_public), "seed": seed},
            parse_session,
        )
        mode = self._expected.semantic.seed_mode
        if mode == "exact":
            agreed = created.requested_seed == seed and created.effective_seed == seed
        elif mode == "unpaired":
            # The target picked the seed, so it must say which one: `None` is
            # not an unpaired session but provenance the rollout cannot record.
            # `type(...) is int` rather than `isinstance`, because `True` is an
            # `int` and a boolean seed is not a seed.
            agreed = created.requested_seed is None and type(created.effective_seed) is int
        else:
            agreed = created.requested_seed is None and created.effective_seed is None
        if not agreed:
            raise _fail("session seeds contradict the target's seed mode", "invalid_response")
        return HttpSession(created.session_id, created.requested_seed, created.effective_seed)

    async def send(self, session: TargetSession, message: Message, request_id: str) -> TurnResult:
        turn = await _json_call(
            self._transport,
            "POST",
            f"{_instances(self.instance_id)}/sessions/{session.session_id}/turns",
            {"request_id": request_id, "message": message.to_payload()},
            parse_turn,
        )
        events: tuple[Mapping[str, Any], ...] = turn.events
        if turn.truncated:
            # A boolean field on the R1 `TurnResult` would change its shape; an
            # event keeps the flag where the Scorer already looks.
            events = (*events, {"type": "output_truncated"})
        return TurnResult(
            events, turn.usage, turn.latency_ms, turn.outcome, turn.reason, turn.provenance
        )

    async def close(self, session: TargetSession) -> None:
        await _delete(
            self._transport,
            f"{_instances(self.instance_id)}/sessions/{session.session_id}",
            "unknown_session",
            session.session_id,
            _closed_id,
        )


class HttpSessionAdapter:
    """A promotion-capable `TargetAdapter` over one loopback `/evolution/v2` target."""

    # Annotations only: both are set by `preflight`, and `__getattr__` turns an
    # access before that into `AdapterNotPreflighted`. A read-only property would
    # not satisfy the protocol's settable `capabilities` member.
    # Note that the guard is not an `AttributeError`, so `hasattr(adapter,
    # "capabilities")` and `getattr(..., default)` raise rather than answer.
    capabilities: AdapterCapabilities
    allowed_artifact_root: Path

    def __init__(
        self,
        binding: TargetBinding,
        policy: ArtifactPolicyV1,
        *,
        run_id: str,
        recorder: Callable[[Exchange], None] | None = None,
        ledger: TargetCallLedger | None = None,
    ) -> None:
        self.binding = binding
        self.policy = policy
        self.run_id = run_id
        # The namespace first: a ledger bound to another operation is refused
        # before this adapter owns a transport.
        if ledger is not None:
            ledger.bind_namespace(run_id)
        self.transport = HttpTransport(binding, recorder=recorder, ledger=ledger)
        self._preflight: TargetPreflight | None = None
        self._health: Health | None = None

    def __getattr__(self, name: str) -> Any:
        if name in ("capabilities", "allowed_artifact_root"):
            raise AdapterNotPreflighted()
        raise AttributeError(name)

    @property
    def health(self) -> Health:
        """The frozen health body; the conformance suite reports it verbatim."""
        health = self._health
        if health is None:
            raise AdapterNotPreflighted()
        return health

    def _frozen(self) -> TargetPreflight:
        preflight = self._preflight
        if preflight is None:
            raise AdapterNotPreflighted()
        return preflight

    async def preflight(self, expected: ExpectedAttestation) -> TargetPreflight:
        """Validate `/health` against the binding, then freeze identity and epoch."""
        health = await _json_call(self.transport, "GET", f"{PREFIX}/health", None, parse_health)
        binding = self.binding
        if not health.ready:
            raise _fail("target is not ready", "not_ready")
        # `parse_health` already rejects any other protocol version; the check
        # stays because the binding, not the parser, is the authority here.
        if health.protocol_version != PROTOCOL_VERSION:
            raise _fail("target speaks another protocol version", "pin_mismatch")
        if health.seed_mode != binding.seed_mode:
            raise _fail("target seed mode differs from the binding", "pin_mismatch")
        if (
            binding.contract_digest is not None
            and health.contract_digest != binding.contract_digest
        ):
            raise _fail("target contract digest differs from the pin", "pin_mismatch")
        if binding.settings_hash is not None and health.settings_hash != binding.settings_hash:
            raise _fail("target settings hash differs from the pin", "pin_mismatch")
        capabilities = health.capabilities
        if STRATEGY not in capabilities.materialization:
            raise AttestationError(
                "target does not offer artifact root injection", "strategy_unsupported"
            )
        if frozenset(capabilities.attests) != _REQUIRED_ATTESTS:
            raise AttestationError(
                "target does not attest the required identity fields", "attestation_missing"
            )
        if capabilities.max_output_bytes > binding.max_response_bytes:
            raise _fail("target may answer beyond the response limit", "limits_incompatible")
        if capabilities.max_input_bytes < binding.min_input_bytes:
            raise _fail("target accepts less input than the binding needs", "limits_incompatible")
        if health.policy_digest != self.policy.digest:
            raise _fail("target is bound to another artifact policy", "policy_mismatch")

        frozen = TargetPreflight(
            SemanticIdentity(
                health.contract_digest,
                # The one field health cannot declare: S2's sealing rule makes the
                # champion's dependency digest the chain's, and materialization
                # attests it.
                expected.dependency_digest,
                health.settings_hash,
                health.seed_mode,
                health.protocol_version,
                health.runtime_ref_digest,
            ),
            AdapterCapabilities(
                health.seed_mode,
                capabilities.max_concurrent_sessions,
                capabilities.instantiable,
                _known_strategies(capabilities.materialization),
                capabilities.max_instances,
            ),
            health.boot_id,
        )
        self._health = health
        self._preflight = frozen
        self.capabilities = frozen.capabilities
        self.allowed_artifact_root = Path(health.allowed_artifact_root)
        return frozen

    async def materialize(
        self,
        candidate: ArtifactRef,
        strategy: MaterializationStrategy,
        instance_id: str,
        *,
        request_id: str | None = None,
    ) -> HttpInstance:
        """Create one instance. `request_id` (I1) is the caller's creation id,
        resent unchanged on a retry so the target replays one creation instead
        of making a second; `None` mints a fresh one, as before."""
        frozen = self._frozen()
        if strategy != STRATEGY:
            raise AttestationError(
                "adapter supports artifact root injection only", "strategy_unsupported"
            )
        if not is_instance_id(instance_id, self.run_id):
            raise _fail("invalid instance id", "invalid_request")
        # ADR-0007: preflight froze the identity S1 journals, so a Candidate
        # sealed against another dependency set could only produce an identity
        # contradicting it. Refused before the wire, not after: the target would
        # otherwise load bytes this adapter can never accept.
        if candidate.expected_attestation().dependency_digest != frozen.semantic.dependency_digest:
            raise AttestationError(
                "candidate dependency digest differs from the preflighted identity",
                "attestation_mismatch",
            )
        # The contract gives the target authority over `candidate_root`, and it would
        # answer 403 `root_not_allowed`. The adapter refuses first, so a store
        # created outside the root the target published never reaches the wire
        # and never names a path the target may log.
        if (
            not Path(candidate.root)
            .resolve()
            .is_relative_to(Path(self.allowed_artifact_root).resolve())
        ):
            raise _fail("candidate root outside the target's allowed root", "root_not_allowed")
        # The sealed bytes are proved here, before the target is asked to load
        # them; the target then attests what it loaded and the two must agree.
        candidate.verify()
        reported = await _json_call(
            self.transport,
            "POST",
            f"{PREFIX}/instances",
            {
                "request_id": uuid4().hex if request_id is None else request_id,
                "instance_id": instance_id,
                "candidate_root": str(candidate.root),
                "policy": self.policy.to_payload(),
                "strategy": strategy,
            },
            parse_instance,
        )
        if reported.instance_id != instance_id:
            raise _fail("target answered for another instance", "invalid_response")
        _epoch_guard(reported, frozen.epoch, frozen.semantic)
        identity = _identity_of(reported)
        verify_attestation(identity, candidate.expected_attestation())
        # The creation body is the only source of the description: it is the one
        # answer the target gives about the instance it just built.
        return HttpInstance(
            self.transport, instance_id, identity, reported.description, reported.components
        )

    async def release(self, instance_id: str) -> None:
        self._frozen()
        if not isinstance(instance_id, str) or IDENTIFIER.match(instance_id) is None:
            raise _fail("invalid instance id", "invalid_request")
        await _delete(
            self.transport, _instances(instance_id), "unknown_instance", instance_id, _released_id
        )
