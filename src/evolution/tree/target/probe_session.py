"""Serialized, bounded HTTP probes owned by one campaign event loop.

``diagnostic_root`` is the already admitted ``reflector-logs`` directory for a
campaign.  The default writer creates and re-admits, one component at a time,
``probes/<reflection_id>/<seq>.json`` below it.  The root is never an endpoint
or capability and no target address is written to the diagnostic document.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast
from urllib.parse import quote, urlsplit

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.private_files import open_private_file
from evolution.tree.storage.redaction import safe_record
from evolution.tree.target.config import ProbeSection, _normalized_loopback, _validate_probe_path

__all__ = [
    "AppendCallback",
    "DiagnosticWriter",
    "ProbeResult",
    "ProbeSession",
    "ProbeSessionAborted",
    "UNAVAILABLE_RESULT",
    "check_probe_health",
    "write_probe_diagnostic",
]

_REQUEST_ENVELOPE_BYTES: Final = 128 * 1024
_REQUEST_BODY_BYTES: Final = 64 * 1024
_RESPONSE_BYTES: Final = 1024 * 1024
_PRIVATE_RESPONSE_BYTES: Final = 64 * 1024
_ROLE_RESPONSE_CHARS: Final = 4000
_HEADER_BYTES: Final = 64 * 1024
_CHUNK_LINE_BYTES: Final = 8192
_JSON_DEPTH: Final = 32
_DEFAULT_CLOSE_GRACE_SECONDS: Final = 5.0
_SECRET_SCRUB_PASSES: Final = 32

ProbeResult: TypeAlias = dict[str, object]
AppendCallback: TypeAlias = Callable[[dict[str, object]], None]
DiagnosticWriter: TypeAlias = Callable[[Path, Mapping[str, object]], None]

_DETAILS: Final[dict[str, str]] = {
    "rejected": "probe request rejected",
    "transport": "probe transport failed",
    "budget_exhausted": "probe budget exhausted",
    "instance_died": "probe instance is unavailable",
    "context_exhausted": "probe request context exhausted",
    "unavailable": "probe unavailable: continue without it or retry later",
}
UNAVAILABLE_RESULT: Final[ProbeResult] = {"error": "unavailable", "detail": _DETAILS["unavailable"]}
_FAULT = object()


class ProbeSessionAborted(RuntimeError):
    """A local caller cannot receive a result after required persistence failed."""

    def __init__(self) -> None:
        super().__init__("probe session aborted")


class _TransportFailure(Exception):
    pass


class _Unreachable(Exception):
    """The outbound TCP connection was never established."""


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    body: object
    body_bytes: bytes
    retained_body: object
    body_parsed: bool = False


@dataclass(frozen=True)
class _Response:
    status: int
    body: bytes
    bytes_truncated: bool


def _error(code: str) -> ProbeResult:
    return {"error": code, "detail": _DETAILS[code]}


def _validated_config(config: ProbeSection) -> tuple[ProbeSection, str]:
    if not isinstance(config, ProbeSection):
        raise TypeError("probe config must be a ProbeSection")
    if config.string_body not in (None, "send", "parse"):
        raise ValueError("probe string_body is invalid")
    try:
        origin = _normalized_loopback(config.base_url)
    except (TypeError, ValueError):
        raise ValueError("probe base_url is not a loopback http origin") from None
    try:
        _validate_probe_path(config.health_path, "probe.health_path")
    except ValueError:
        raise ValueError("probe health_path is invalid") from None
    if (
        not isinstance(config.methods, tuple)
        or not config.methods
        or any(type(method) is not str for method in config.methods)
        or len(set(config.methods)) != len(config.methods)
        or any(method not in {"GET", "POST", "PUT", "DELETE"} for method in config.methods)
    ):
        raise ValueError("probe methods are invalid")
    if type(config.budget) is not int or config.budget <= 0:
        raise ValueError("probe budget is invalid")
    if (
        type(config.request_timeout_seconds) not in (int, float)
        or not math.isfinite(config.request_timeout_seconds)
        or config.request_timeout_seconds <= 0
    ):
        raise ValueError("probe request timeout is invalid")
    if config.description is not None:
        if not isinstance(config.description, str):
            raise ValueError("probe description is invalid")
        try:
            description_bytes = config.description.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("probe description is invalid") from None
        if len(description_bytes) > 8192:
            raise ValueError("probe description is invalid")
    return config, origin


def _json_depth_and_shape(value: object, *, depth: int = 0, seen: set[int] | None = None) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return
    if type(value) not in (dict, list):
        raise ValueError("not a JSON value")
    if depth >= _JSON_DEPTH:
        raise ValueError("JSON body is too deep")
    identities = seen if seen is not None else set()
    identity = id(value)
    if identity in identities:
        raise ValueError("cyclic JSON value")
    identities.add(identity)
    try:
        if type(value) is dict:
            for key, item in cast(dict[object, object], value).items():
                if type(key) is not str:
                    raise ValueError("JSON object key is not a string")
                _json_depth_and_shape(item, depth=depth + 1, seen=identities)
        else:
            for item in cast(list[object], value):
                _json_depth_and_shape(item, depth=depth + 1, seen=identities)
    finally:
        identities.remove(identity)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _invalid_json_constant(value: str) -> None:
    raise ValueError("not a JSON constant")


def _request(value: object, config: ProbeSection, secrets: tuple[str, ...]) -> _Request:
    if type(value) is not dict:
        raise ValueError("request is not an object")
    request = cast(dict[object, object], value)
    if set(request) != {"method", "path", "body"}:
        raise ValueError("request keys are invalid")
    method = request["method"]
    path = request["path"]
    body = request["body"]
    if type(method) is not str or method not in config.methods:
        raise ValueError("method is invalid")
    try:
        path = _validate_probe_path(path, "probe.path")
    except ValueError:
        raise ValueError("path is invalid") from None
    if any(secret and (secret in method or secret in path) for secret in secrets):
        raise ValueError("journal field contains a secret")
    body_parsed = False
    if config.string_body == "parse" and isinstance(body, str):
        try:
            decoded = json.loads(body, parse_constant=_invalid_json_constant)
        except (ValueError, RecursionError):
            pass
        else:
            body = decoded
            body_parsed = True
    _json_depth_and_shape(body)
    try:
        body_bytes = b"" if body is None else _json_bytes(body)
        retained_body = safe_record(body, secrets)
        _json_depth_and_shape(retained_body)
        retained_body_bytes = b"" if retained_body is None else _json_bytes(retained_body)
        envelope_bytes = _json_bytes({"method": method, "path": path, "body": body})
        retained_envelope_bytes = _json_bytes(
            {"method": method, "path": path, "body": retained_body}
        )
    except (OverflowError, UnicodeEncodeError, ValueError):
        raise ValueError("request is not bounded JSON") from None
    if (
        len(body_bytes) > _REQUEST_BODY_BYTES
        or len(retained_body_bytes) > _REQUEST_BODY_BYTES
        or len(envelope_bytes) > _REQUEST_ENVELOPE_BYTES
        or len(retained_envelope_bytes) > _REQUEST_ENVELOPE_BYTES
    ):
        raise ValueError("request exceeds a byte limit")
    return _Request(method, path, body, body_bytes, retained_body, body_parsed)


def _clip_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def _safe_text(value: str, secrets: tuple[str, ...]) -> str:
    """Redact bounded untrusted text even if JSON parsing hits a recursion/value guard."""

    try:
        return cast(str, safe_record(value, secrets))
    except (RecursionError, ValueError):
        # Prefixing makes `safe_record` take its plain-text path, which still
        # applies hidden-reasoning and explicit-secret redaction.
        return cast(str, safe_record("x" + value, secrets))[1:]


def _secret_prefix_overlap(value: bytes, secret: bytes) -> int:
    """Return the longest non-empty suffix of value that prefixes secret, in linear time."""

    if len(secret) < 2 or not value:
        return 0
    # Only a proper secret prefix needs special handling: a complete secret is
    # visible to `safe_record`. Cap the pattern at one byte beyond retained
    # input, keeping working memory within the response bound even if an
    # operator supplied a much larger secret value.
    pattern = secret[: min(len(secret), len(value) + 1)]
    tail = value[-min(len(value), len(secret) - 1) :]
    failure = [0] * len(pattern)
    matched = 0
    for index in range(1, len(pattern)):
        while matched and pattern[index] != pattern[matched]:
            matched = failure[matched - 1]
        if pattern[index] == pattern[matched]:
            matched += 1
        failure[index] = matched
    matched = 0
    for octet in tail:
        while matched and octet != pattern[matched]:
            matched = failure[matched - 1]
        if octet == pattern[matched]:
            matched += 1
        if matched == len(pattern):
            matched = failure[matched - 1]
    return matched


def _secret_suffix_overlap(value: bytes, secrets: tuple[str, ...]) -> int:
    """Find the longest retained suffix that could start a cutoff secret."""

    overlap = 0
    for secret in secrets:
        if not secret:
            continue
        try:
            encoded = secret.encode("utf-8")
        except UnicodeEncodeError:
            continue
        overlap = max(overlap, _secret_prefix_overlap(value, encoded))
    return overlap


def _scrub_redacted_secret_suffix(value: str, secrets: tuple[str, ...]) -> str:
    """Remove newly exposed secret prefixes to a bounded fixed point."""

    current = value
    for _ in range(_SECRET_SCRUB_PASSES):
        encoded = current.encode("utf-8")
        overlap = _secret_suffix_overlap(encoded, secrets)
        if overlap == 0:
            return current
        # Re-run complete-value and reasoning redaction after every cut, so a
        # newly exposed full secret cannot be treated as another partial one.
        current = _safe_text(encoded[:-overlap].decode("utf-8", "ignore"), secrets)
    # A malicious overlap chain cannot make boundary work unbounded. The
    # response is already truncated; retaining nothing is the safe fallback.
    return ""


def _bounded_response_text(value: bytes, secrets: tuple[str, ...], *, truncated: bool) -> str:
    """Decode/redact a bounded response, then resolve cutoff ambiguity."""

    decoded = value.decode("utf-8", "replace")
    redacted = _safe_text(decoded, secrets)
    if not truncated:
        return redacted

    overlap = _secret_suffix_overlap(value, secrets)
    if overlap:
        # Preserve raw-boundary knowledge through replacement decoding. For
        # example, `top-\xe2` is the retained prefix of `top-€...`, but its
        # decoded `top-�` suffix no longer text-matches the secret.
        ambiguous = value[-overlap:].decode("utf-8", "replace")
        if ambiguous and redacted.endswith(ambiguous):
            redacted = redacted[: -len(ambiguous)]
            redacted = _safe_text(redacted, secrets)
    return _scrub_redacted_secret_suffix(redacted, secrets)


def write_probe_diagnostic(path: Path, document: Mapping[str, object]) -> None:
    """Write one bounded owner-only diagnostic below an admitted reflector log root."""

    target = Path(path)
    root = target.parents[2]
    probes = target.parents[1]
    reflection = target.parent
    ensure_private_directory(root)
    ensure_private_directory(probes)
    ensure_private_directory(reflection)
    descriptor = open_private_file(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        refusal=f"probe diagnostic is not a private regular file: {target.name}",
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_json_bytes(dict(document)) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _encoded_target(path: str) -> str:
    # Existing valid percent escapes stay literal; raw Unicode becomes its UTF-8
    # percent encoding. The validator already refused ambiguous percent forms.
    return quote(path, safe="/%?&=;:@,+$!~*'()-._")


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    try:
        block = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise _TransportFailure from exc
    if len(block) > _HEADER_BYTES:
        raise _TransportFailure
    lines = block[:-4].split(b"\r\n")
    try:
        version, raw_status, _reason = lines[0].split(b" ", 2)
        status = int(raw_status)
    except (ValueError, IndexError) as exc:
        raise _TransportFailure from exc
    if version not in {b"HTTP/1.0", b"HTTP/1.1"} or not 100 <= status <= 599:
        raise _TransportFailure
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise _TransportFailure
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").strip().lower()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise _TransportFailure from exc
        if not name or (name in headers and name in {"content-length", "transfer-encoding"}):
            raise _TransportFailure
        headers[name] = f"{headers[name]},{value}" if name in headers else value
    return status, headers


async def _read_chunked(reader: asyncio.StreamReader) -> tuple[bytes, bool]:
    output = bytearray()
    while True:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise _TransportFailure from exc
        if not line or len(line) > _CHUNK_LINE_BYTES or not line.endswith(b"\r\n"):
            raise _TransportFailure
        try:
            size = int(line[:-2].split(b";", 1)[0], 16)
        except ValueError as exc:
            raise _TransportFailure from exc
        if size < 0:
            raise _TransportFailure
        if size == 0:
            # A no-trailer terminator is one blank line. Bound a real trailer
            # block line-by-line rather than waiting for two more CRLF pairs.
            trailer_bytes = 0
            while True:
                try:
                    trailer_line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise _TransportFailure from exc
                trailer_bytes += len(trailer_line)
                if (
                    not trailer_line
                    or not trailer_line.endswith(b"\r\n")
                    or trailer_bytes > _HEADER_BYTES
                ):
                    raise _TransportFailure
                if trailer_line == b"\r\n":
                    return bytes(output), False
        remaining = _RESPONSE_BYTES + 1 - len(output)
        take = min(size, max(0, remaining))
        try:
            output.extend(await reader.readexactly(take))
            if take < size:
                return bytes(output[:_RESPONSE_BYTES]), True
            if await reader.readexactly(2) != b"\r\n":
                raise _TransportFailure
        except asyncio.IncompleteReadError as exc:
            raise _TransportFailure from exc
        if len(output) > _RESPONSE_BYTES:
            return bytes(output[:_RESPONSE_BYTES]), True


async def _read_body(
    reader: asyncio.StreamReader, status: int, headers: Mapping[str, str]
) -> tuple[bytes, bool]:
    if 100 <= status < 200 or status in {204, 304}:
        return b"", False
    transfer = headers.get("transfer-encoding", "").lower()
    if transfer:
        if transfer != "chunked":
            raise _TransportFailure
        return await _read_chunked(reader)
    raw_length = headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _TransportFailure from exc
        if length < 0:
            raise _TransportFailure
        read_size = min(length, _RESPONSE_BYTES + 1)
        try:
            body = await reader.readexactly(read_size)
        except asyncio.IncompleteReadError as exc:
            raise _TransportFailure from exc
        return body[:_RESPONSE_BYTES], length > _RESPONSE_BYTES
    output = bytearray()
    while len(output) <= _RESPONSE_BYTES:
        part = await reader.read(min(65536, _RESPONSE_BYTES + 1 - len(output)))
        if not part:
            return bytes(output), False
        output.extend(part)
    return bytes(output[:_RESPONSE_BYTES]), True


async def _http_exchange(
    origin: str,
    method: str,
    path: str,
    body: bytes,
    *,
    has_body: bool,
    deadline_at: float,
) -> _Response:
    parts = urlsplit(origin)
    host = cast(str, parts.hostname)
    port = cast(int, parts.port)
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout_at(deadline_at):
            reader, writer = await asyncio.open_connection(host, port, limit=_HEADER_BYTES + 1)
            lines = [
                f"{method} {_encoded_target(path)} HTTP/1.1",
                f"Host: {parts.netloc}",
                "Connection: close",
            ]
            if has_body:
                lines.extend(
                    [
                        "Content-Type: application/json",
                        f"Content-Length: {len(body)}",
                    ]
                )
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
            await writer.drain()
            status, headers = await _read_headers(reader)
            response_body, clipped = await _read_body(reader, status, headers)
            return _Response(status, response_body, clipped)
    except asyncio.CancelledError:
        raise
    except (OSError, TimeoutError) as exc:
        if writer is None:
            raise _Unreachable from exc
        raise _TransportFailure from exc
    except (_TransportFailure, http.client.HTTPException) as exc:
        raise _TransportFailure from exc
    finally:
        if writer is not None:
            writer.close()
            cleanup_seconds = min(0.1, max(0.0, deadline_at - asyncio.get_running_loop().time()))
            if cleanup_seconds > 0:
                with contextlib.suppress(OSError, TimeoutError, asyncio.CancelledError):
                    async with asyncio.timeout(cleanup_seconds):
                        await writer.wait_closed()


class ProbeSession:
    """One serialized probe budget, bound to the loop that constructs it."""

    def __init__(
        self,
        config: ProbeSection,
        *,
        round: int,
        reflection_id: str,
        diagnostic_root: Path,
        deadline_at: float,
        append: AppendCallback,
        secrets: Sequence[str] = (),
        diagnostic_writer: DiagnosticWriter = write_probe_diagnostic,
        on_unavailable: Literal["suspend", "continue"] = "suspend",
        disabled: bool = False,
    ) -> None:
        checked, origin = _validated_config(config)
        if on_unavailable not in ("suspend", "continue") or type(disabled) is not bool:
            raise ValueError("probe availability configuration is invalid")
        self._on_unavailable = on_unavailable
        self._disabled = disabled
        self._unreachable_count = 0
        self._transport_failure_count = 0
        if type(round) is not int or round <= 0:
            raise ValueError("probe round is invalid")
        if (
            type(reflection_id) is not str
            or not reflection_id
            or len(reflection_id) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in reflection_id
            )
        ):
            raise ValueError("probe reflection_id is invalid")
        if not callable(append) or not callable(diagnostic_writer):
            raise TypeError("probe callbacks must be callable")
        if type(deadline_at) not in (int, float) or not math.isfinite(deadline_at):
            raise ValueError("probe deadline is invalid")
        secret_values = tuple(secrets)
        if any(type(secret) is not str for secret in secret_values):
            raise TypeError("probe secrets must be strings")
        self._config = checked
        self._origin = origin
        self._round = round
        self._reflection_id = reflection_id
        self._diagnostic_root = Path(diagnostic_root)
        self._deadline_at = float(deadline_at)
        self._append = append
        self._secrets = secret_values
        self._diagnostic_writer = diagnostic_writer
        self._loop = asyncio.get_running_loop()
        self._serial = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._budget_remaining = checked.budget
        self._next_seq = 1
        self._transport_failures = 0
        self._dead = False
        self._closed = False
        self._fault: BaseException | None = None
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._transport_task: asyncio.Task[_Response] | None = None
        self._abort_requested = False
        self._append_revoked = False
        self._close_task: asyncio.Task[bool] | None = None

    @property
    def config(self) -> ProbeSection:
        return self._config

    @property
    def deadline_at(self) -> float:
        return self._deadline_at

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def fault(self) -> BaseException | None:
        return self._fault

    @property
    def budget_remaining(self) -> int:
        return self._budget_remaining

    @property
    def active(self) -> bool:
        return bool(self._active_tasks)

    def availability(self) -> dict[str, int]:
        return {
            "unreachable": self._unreachable_count,
            "transport_failures": self._transport_failure_count,
        }

    async def record_unavailable_calls(self, requests: Sequence[object]) -> None:
        """Admit prior helper claims through the disabled session before closing.

        The role has already ended. Its elapsed deadline must not discard calls
        it made earlier; only this local, bounded bookkeeping receives time.
        No network dispatch is possible and ordinary validation still applies.
        """
        self._ensure_loop()
        if not self._disabled or self._on_unavailable != "continue":
            raise ValueError("helper claims require a disabled continue session")
        deadline = self._deadline_at
        try:
            for request in requests:
                self._deadline_at = max(deadline, self._loop.time() + 1.0)
                await self.request(request)
        finally:
            self._deadline_at = deadline

    def _ensure_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("probe session used from a non-owning event loop")

    async def request(self, value: object) -> ProbeResult:
        """Validate, serialize and dispatch one request without caller-driven resend."""

        self._ensure_loop()
        if self._fault is not None:
            raise ProbeSessionAborted
        try:
            admitted = _request(value, self._config, self._secrets)
        except ValueError:
            return _error("rejected")
        remaining = self._deadline_at - self._loop.time()
        if remaining <= 0:
            return _error("context_exhausted")
        acquire = self._loop.create_task(self._serial.acquire())
        stopped = self._loop.create_task(self._stopped.wait())
        try:
            done, _pending = await asyncio.wait(
                {acquire, stopped}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            if acquire.done() and not acquire.cancelled():
                with contextlib.suppress(BaseException):
                    if acquire.result():
                        self._serial.release()
            acquire.cancel()
            stopped.cancel()
            await asyncio.gather(acquire, stopped, return_exceptions=True)
            raise
        if acquire not in done:
            acquire.cancel()
            stopped.cancel()
            await asyncio.gather(acquire, stopped, return_exceptions=True)
            if self._fault is not None:
                raise ProbeSessionAborted
            return _error("context_exhausted")
        stopped.cancel()
        if self._fault is not None:
            self._serial.release()
            raise ProbeSessionAborted
        if self._closed or self._loop.time() >= self._deadline_at:
            self._serial.release()
            return _error("context_exhausted")
        if self._dead:
            self._serial.release()
            return _error("instance_died")
        if self._budget_remaining == 0:
            self._serial.release()
            return _error("budget_exhausted")
        seq = self._next_seq
        self._next_seq += 1
        self._budget_remaining -= 1
        task = self._loop.create_task(self._execute(admitted, seq))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        outcome = await asyncio.shield(task)
        if outcome is _FAULT:
            raise ProbeSessionAborted
        return cast(ProbeResult, outcome)

    async def _execute(self, request: _Request, seq: int) -> ProbeResult | object:
        endpoint_deadline = self._loop.time() + self._config.request_timeout_seconds
        cutoff_is_context = self._deadline_at <= endpoint_deadline
        deadline = min(endpoint_deadline, self._deadline_at)
        response: _Response | None = None
        error_code: str | None = None
        try:
            if self._abort_requested:
                error_code = "context_exhausted"
            elif self._disabled:
                error_code = "unreachable"
            else:
                self._transport_task = self._loop.create_task(
                    _http_exchange(
                        self._origin,
                        request.method,
                        request.path,
                        request.body_bytes,
                        has_body=request.body is not None,
                        deadline_at=deadline,
                    )
                )
                try:
                    response = await self._transport_task
                except asyncio.CancelledError:
                    error_code = "context_exhausted"
                except TimeoutError:
                    error_code = "context_exhausted" if cutoff_is_context else "transport"
                except (_Unreachable, _TransportFailure) as exc:
                    if cutoff_is_context and isinstance(exc.__cause__, TimeoutError):
                        error_code = "context_exhausted"
                    elif isinstance(exc, _Unreachable) and self._on_unavailable == "continue":
                        error_code = "unreachable"
                    else:
                        error_code = "transport"
                finally:
                    self._transport_task = None

            if response is not None:
                self._transport_failures = 0
                result, document = self._response_result(request, seq, response)
            else:
                assert error_code is not None
                if error_code == "transport":
                    self._transport_failures += 1
                    if self._transport_failures == 3 and self._on_unavailable == "suspend":
                        error_code = "instance_died"
                        self._dead = True
                if error_code == "unreachable":
                    self._budget_remaining += 1
                result = (
                    dict(UNAVAILABLE_RESULT)
                    if error_code == "unreachable"
                    or (error_code == "transport" and self._on_unavailable == "continue")
                    else _error(error_code)
                )
                document = {
                    "seq": seq,
                    "request": {
                        "method": request.method,
                        "path": request.path,
                        "body": request.retained_body,
                    },
                    "path_clipped": len(request.path) > 256,
                    "outcome": {**result, "truncated": False},
                }
            if request.body_parsed:
                document["body_parsed"] = True
            event_path = request.path if len(request.path) <= 256 else request.path[:255] + "…"
            event: dict[str, object] = {
                "round": self._round,
                "reflection_id": self._reflection_id,
                "seq": seq,
                "method": request.method,
                "path": event_path,
                "status": response.status if response is not None else None,
                "error": error_code,
            }
            if self._append_revoked:
                return _FAULT
            try:
                self._diagnostic_writer(self._diagnostic_path(seq), document)
                self._append(event)
                if error_code == "unreachable":
                    self._unreachable_count += 1
                elif error_code in ("transport", "instance_died"):
                    self._transport_failure_count += 1
            except Exception as exc:
                self._set_fault(exc)
                return _FAULT
            return result
        finally:
            self._serial.release()

    def _response_result(
        self, request: _Request, seq: int, response: _Response
    ) -> tuple[ProbeResult, dict[str, object]]:
        retained_text = _bounded_response_text(
            response.body,
            self._secrets,
            truncated=response.bytes_truncated,
        )
        role_body = retained_text[:_ROLE_RESPONSE_CHARS]
        private_raw_clipped = len(response.body) > _PRIVATE_RESPONSE_BYTES
        # Redact the complete bounded response before private clipping. Cutting
        # raw bytes first could retain the prefix of a known secret spanning
        # the 64 KiB boundary.
        private_redacted = retained_text
        private_body, private_redaction_clipped = _clip_utf8(
            private_redacted, _PRIVATE_RESPONSE_BYTES
        )
        truncated = (
            response.bytes_truncated
            or len(retained_text) > _ROLE_RESPONSE_CHARS
            or private_raw_clipped
            or private_redaction_clipped
        )
        result: ProbeResult = {
            "seq": seq,
            "status": response.status,
            "body": role_body,
            "truncated": truncated,
        }
        document: dict[str, object] = {
            "seq": seq,
            "request": {
                "method": request.method,
                "path": request.path,
                "body": request.retained_body,
            },
            "path_clipped": len(request.path) > 256,
            "outcome": {
                "status": response.status,
                "body": private_body,
                "truncated": truncated,
                "response_bytes_truncated": response.bytes_truncated or private_raw_clipped,
                "redaction_truncated": private_redaction_clipped,
            },
        }
        return result, document

    def _diagnostic_path(self, seq: int) -> Path:
        return self._diagnostic_root / "probes" / self._reflection_id / f"{seq}.json"

    def _set_fault(self, exc: BaseException) -> None:
        if self._fault is None:
            self._fault = exc
        self._closed = True
        self._stopped.set()

    def stop_admission(self) -> None:
        """Immediately reject queued and future requests without touching active I/O."""

        self._closed = True
        if self._loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            self._stopped.set()
        else:
            self._loop.call_soon_threadsafe(self._stopped.set)

    def abort(self) -> None:
        """Immediately stop admission and cancel active I/O on the owning loop."""

        self.stop_admission()
        self._abort_requested = True

        def cancel_active() -> None:
            if self._transport_task is not None:
                self._transport_task.cancel()

        if self._loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            cancel_active()
        else:
            self._loop.call_soon_threadsafe(cancel_active)

    def revoke_event_authority(self) -> None:
        """Synchronously forbid future appends for an unresponsive terminal path."""

        self._ensure_loop()
        self._append_revoked = True
        self.abort()

    async def close(self, *, grace_seconds: float = _DEFAULT_CLOSE_GRACE_SECONDS) -> bool:
        """Idempotently abort and drain owned work; return whether it quiesced."""

        self._ensure_loop()
        if (
            type(grace_seconds) not in (int, float)
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("probe close grace is invalid")
        self.abort()
        if self._close_task is None or (self._close_task.done() and not self._close_task.result()):
            self._close_task = self._loop.create_task(self._close_impl(grace_seconds))
        return await asyncio.shield(self._close_task)

    async def _close_impl(self, grace_seconds: float) -> bool:
        tasks = tuple(self._active_tasks)
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            if pending:
                # The campaign may now admit an unresponsive terminal. Revoke
                # event authority so a late worker cannot append after it.
                self._append_revoked = True
                return False
            self._active_tasks.difference_update(tasks)
        return not self._active_tasks

    def raise_if_failed(self) -> None:
        """Rethrow the first required diagnostic or journal persistence failure."""

        if self._fault is not None:
            raise self._fault


async def check_probe_health(
    config: ProbeSection,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> bool:
    """Run one bounded GET health check without budget, journal or diagnostics."""

    checked, origin = _validated_config(config)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + checked.request_timeout_seconds
    task = loop.create_task(
        _http_exchange(
            origin,
            "GET",
            checked.health_path,
            b"",
            has_body=False,
            deadline_at=deadline,
        )
    )
    try:
        while not task.done():
            if cancelled():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise asyncio.CancelledError
            await asyncio.wait({task}, timeout=0.01)
        try:
            response = await task
        except (TimeoutError, _TransportFailure, _Unreachable):
            return False
        return 200 <= response.status < 300
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
