"""The shared role-call retry contract, `role_call_retry/1` (PDR-0015 as amended 2026-09-25).

One logical provider request is retried here and nowhere else: a backend
invocation performs exactly one network attempt (the grouping client is built
with ``max_retries=0``), so the manifest's ``max_attempts`` is the real count.
Retries repeat only the failed request; the caller's conversation, map/reduce
state and workspace are untouched. Family 9 applies it to model grouping first;
Reflector and Editor adopt the same contract later (LH2-016).
"""

from __future__ import annotations

import asyncio
import random
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeVar

VERSION: Final = "role_call_retry/1"
ProviderErrorClass = Literal[
    "transient", "protocol", "terminal"
]  # "protocol" reserved for caller's repair path
T = TypeVar("T")

_KEYS: Final = frozenset({"version", "max_attempts", "total_budget_seconds", "backoff"})
_BACKOFF_KEYS: Final = frozenset({"initial_seconds", "multiplier", "max_seconds", "jitter"})


def _number(value: object, path: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(path)
    return float(value)


def _find_ssl_error_in_chain(exc: BaseException) -> bool:
    """Walk __cause__ and __context__ chain (cycle-safe, bounded) for ssl.SSLError."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break  # cycle detected
        seen.add(exc_id)
        if isinstance(current, ssl.SSLError):
            return True
        # Try __cause__ first, then __context__ (as per PEP 3134)
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class RoleCallRetry:
    max_attempts: int
    total_budget_seconds: float
    initial_seconds: float
    multiplier: float
    max_seconds: float
    jitter: Literal["full"] = "full"

    @classmethod
    def from_payload(cls, obj: object) -> RoleCallRetry:
        if not isinstance(obj, Mapping):
            raise ValueError("retry")
        if set(obj) - _KEYS:
            raise ValueError("retry.unknown")
        if set(_KEYS) - set(obj):
            raise ValueError("retry.missing")
        if obj["version"] != VERSION:
            raise ValueError("retry.version")
        attempts = obj["max_attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
            raise ValueError("retry.max_attempts")
        backoff = obj["backoff"]
        if not isinstance(backoff, Mapping) or set(backoff) != _BACKOFF_KEYS:
            raise ValueError("retry.backoff")
        if backoff["jitter"] != "full":
            raise ValueError("retry.backoff.jitter")
        initial = _number(backoff["initial_seconds"], "retry.backoff.initial_seconds", 0, 60)
        maximum = _number(backoff["max_seconds"], "retry.backoff.max_seconds", initial, 300)
        return cls(
            attempts,
            _number(obj["total_budget_seconds"], "retry.total_budget_seconds", 1, 3600),
            initial,
            _number(backoff["multiplier"], "retry.backoff.multiplier", 1, 10),
            maximum,
        )

    def to_payload(self) -> dict[str, Any]:
        def plain(value: float) -> int | float:
            return int(value) if float(value).is_integer() else value

        return {
            "version": VERSION,
            "max_attempts": self.max_attempts,
            "total_budget_seconds": plain(self.total_budget_seconds),
            "backoff": {
                "initial_seconds": plain(self.initial_seconds),
                "multiplier": plain(self.multiplier),
                "max_seconds": plain(self.max_seconds),
                "jitter": self.jitter,
            },
        }

    def ceiling(self, n: int) -> float:
        return min(self.max_seconds, self.initial_seconds * self.multiplier ** (n - 1))


class RouterRefusal(Exception):
    """A router error body; only the private refusal record may read its message."""

    def __init__(
        self,
        *,
        http_status: int,
        inner_code: int | str | None,
        provider_name: Any = None,
        message: str | None = None,
    ) -> None:
        super().__init__("router_refusal")
        self.http_status = http_status
        self.inner_code = inner_code
        self.provider_name = provider_name
        self.message = message

    @property
    def code_text(self) -> str | None:
        if type(self.inner_code) is int:
            return str(self.inner_code)
        if isinstance(self.inner_code, str) and _PROVIDER_CODE.fullmatch(self.inner_code):
            return self.inner_code
        return None

    @property
    def safe_provider(self) -> str | None:
        if isinstance(self.provider_name, str) and re.fullmatch(
            r"[A-Za-z0-9_.:/ -]{1,64}", self.provider_name
        ):
            return self.provider_name
        return None


def error_body(response: Any) -> Mapping[str, Any] | None:
    """Read an error only from a response without any completion choices."""
    if isinstance(response, Mapping):
        if response.get("choices"):
            return None
        error = response.get("error")
    else:
        if getattr(response, "choices", None):
            return None
        error = getattr(response, "error", None)
        if not isinstance(error, Mapping):
            extra = getattr(response, "model_extra", None)
            error = extra.get("error") if isinstance(extra, Mapping) else None
    return error if isinstance(error, Mapping) else None


def status_error_body(body: Any) -> Mapping[str, Any] | None:
    """Read a status-error body, including the SDK's unwrapped error object."""
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    return error if isinstance(error, Mapping) else body


def router_refusal(body: Mapping[str, Any], *, http_status: int) -> RouterRefusal:
    code = body.get("code")
    metadata = body.get("metadata")
    message = body.get("message")
    return RouterRefusal(
        http_status=http_status,
        inner_code=code if isinstance(code, (int, str)) else None,
        provider_name=metadata.get("provider_name") if isinstance(metadata, Mapping) else None,
        message=message if isinstance(message, str) else None,
    )


def _status_class(status: int) -> tuple[ProviderErrorClass, str]:
    if status == 408:
        return "transient", "http_408"
    if status == 429:
        return "transient", "http_429"
    if status >= 500:
        return "transient", "http_5xx"
    if status in (401, 403):
        return "terminal", "credential"
    return "terminal", "http_4xx"


def classify_provider_error(exc: BaseException) -> tuple[ProviderErrorClass, str]:
    # `httpx` and `openai` belong to the `tree` extra, while this module is
    # reached from the manifest loader (`RoleCallRetry`) on a core install, so
    # they are imported only where a provider error is classified.
    import httpx
    import openai

    if isinstance(exc, asyncio.CancelledError):
        return "terminal", "cancelled"
    if isinstance(exc, openai.APITimeoutError | asyncio.TimeoutError | httpx.TimeoutException):
        return "transient", "timeout"
    if isinstance(exc, RouterRefusal):
        if not 200 <= exc.http_status < 300:
            return _status_class(exc.http_status)
        if type(exc.inner_code) is int:
            return _status_class(exc.inner_code)
        return "terminal", "unknown"
    if isinstance(exc, openai.APIStatusError):
        return _status_class(exc.status_code)
    if isinstance(exc, ssl.SSLError):
        return "terminal", "tls"
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        if _find_ssl_error_in_chain(exc):
            return "terminal", "tls"
        return "transient", "connection"
    return "terminal", "unknown"


def _provider_exceptions() -> tuple[type[BaseException], ...]:
    """What a provider or transport raises; anything else is a programming error.

    Resolved on use, like `classify_provider_error`'s imports, so a core
    install still loads this module.
    """

    import httpx
    import openai

    return (openai.OpenAIError, httpx.HTTPError, TimeoutError, OSError, RouterRefusal)


_PROVIDER_CODE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def provider_error_detail(exc: BaseException) -> tuple[int | None, str | None]:
    """The HTTP status and a safe provider error code of a request failure
    (the stable provider classification), found on the exception's cause chain; never message
    text (ADR-0039).

    The status is the first integer `status_code` on the chain (at most eight
    links); the code is that exception's `body["error"]` or `body` `code` or
    `type`, the first one matching `_PROVIDER_CODE`. Both are what the SDK
    already parsed from the response. A failure without a response (timeout,
    connection, TLS) is `(None, None)`.
    """

    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, RouterRefusal):
            return current.http_status, current.code_text
        status = getattr(current, "status_code", None)
        if type(status) is int:
            body = getattr(current, "body", None)
            error = body.get("error") if isinstance(body, Mapping) else None
            for source in (error, body):
                if not isinstance(source, Mapping):
                    continue
                for key in ("code", "type"):
                    value = source.get(key)
                    if isinstance(value, str) and _PROVIDER_CODE.fullmatch(value):
                        return status, value
            return status, None
        current = current.__cause__ or current.__context__
    return None, None


@dataclass(frozen=True)
class AttemptTelemetry:
    """One provider attempt, for the private role logs.

    `http_status` is the status of a failed attempt that got a response, else
    `None`; `provider_code` the provider's safe `error.code`/`error.type`
    (`provider_error_detail`). No body or message text, ever (ADR-0039); the
    names avoid `storage.redaction.safe_record`'s secret-key pattern.
    """

    role: str
    request_id: str
    attempt: int
    duration_seconds: float
    outcome: str
    http_status: int | None = None
    provider_code: str | None = None


class RoleCallExhausted(Exception):
    """Raised when call_with_retry exhausts its retry budget.

    Attributes:
        reason: why exhaustion occurred ("deadline_exhausted", "provider_unavailable", "provider_rejected")
        attempts: number of attempts made
        code: error code from the last attempt (classification outcome, e.g. "http_429", "tls", "timeout")
    """

    def __init__(self, reason: str, attempts: int, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts
        self.code = code


#: How many failed attempts an operator line lists before `…`.
ATTEMPT_LINE_STATUSES: Final = 8
_CREDENTIAL_STATUSES: Final = frozenset({401, 403})
_ATTEMPT_LINE: Final = re.compile(
    r"(?:editor|reflector|grouper) \S+ (?:needed provider retries \(|provider attempts: )"
)


@dataclass(frozen=True)
class AttemptSummary:
    """The failed provider attempts of one role invocation.

    `failed` holds, in order, `(http_status, provider_code)` for a failed
    attempt that got a response and `(None, outcome)` for one that did not
    (`timeout`, `connection`,...): what `AttemptTelemetry` recorded, nothing
    else (C4). `preflight` says every one of them was the tool-dialect
    preflight's. Python only: never journaled, never in the stdout document.
    """

    failed: tuple[tuple[int | None, str | None], ...]
    preflight: bool


def _credential_attempt(status: int | None, detail: str | None) -> bool:
    return status in _CREDENTIAL_STATUSES or (
        status is not None and 200 <= status < 300 and detail in {"401", "403"}
    )


def _attempt_status(status: int | None, detail: str | None) -> str:
    if status is None:
        return detail or "unknown"
    text = f"http {status}" if detail is None else f"http {status} {detail}"
    return f"{text} (credential)" if _credential_attempt(status, detail) else text


def attempt_line(
    role: str,
    operation: str,
    summary: AttemptSummary,
    *,
    failure: str | None,
    unauthenticated: bool,
) -> str | None:
    """The operator's one line for a role invocation's failed attempts, or
    `None` when nothing failed.

    `failure` is the `RoleCallExhausted` reason that ended the invocation,
    `None` when it went on. Statuses and provider codes only,
    at most `ATTEMPT_LINE_STATUSES` of them then `…`; never request content
    or provider text (C4).
    """

    if not summary.failed:
        return None
    shown = [_attempt_status(*item) for item in summary.failed[:ATTEMPT_LINE_STATUSES]]
    if len(summary.failed) > ATTEMPT_LINE_STATUSES:
        shown.append("…")
    statuses = ", ".join(shown)
    if failure is None:
        count = len(summary.failed)
        noun = "attempt" if count == 1 else "attempts"
        line = f"{role} {operation} needed provider retries ({count} failed {noun}: {statuses})"
        line += "; it continued"
        return line + " (tool-dialect preflight)" if summary.preflight else line
    line = f"{role} {operation} provider attempts: {statuses} ({failure})"
    if unauthenticated and any(
        _credential_attempt(status, detail) for status, detail in summary.failed
    ):
        line += "; the endpoint asked for a credential; this role declares no api_key_env"
    return line


def is_attempt_line(text: str) -> bool:
    """Whether a note is an `attempt_line`: the CLI prints it without `note:`."""

    return _ATTEMPT_LINE.match(text) is not None


@dataclass(frozen=True)
class AttemptReport:
    """What one role invocation carries out about its failed attempts.

    `summary` holds every failed attempt of the invocation in order, the
    preflight's included, and is `preflight` only when all of them were the
    preflight's. `failure`, when set, is the `RoleCallExhausted` reason that
    ended the invocation. Python only, like `AttemptSummary`.
    """

    summary: AttemptSummary
    failure: str | None
    unauthenticated: bool

    def lines(self, role: str, operation: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """`(recovered, failed)`: at most one line in all (PDR-0015, as
        amended 2026-10-21: the invocation tells the operator once). It is
        recovered when the invocation went on, failed when it ended in a
        provider failure."""

        line = attempt_line(
            role,
            operation,
            self.summary,
            failure=self.failure,
            unauthenticated=self.unauthenticated,
        )
        if line is None:
            return (), ()
        return ((), (line,)) if self.failure is not None else ((line,), ())


class AttemptRecorder:
    """One role invocation's failed provider attempts.

    Fed the same `AttemptTelemetry` the private role log records; held in
    memory for the invocation and turned into an `AttemptReport`. Never
    journaled. Each failed attempt remembers whether the tool-dialect
    preflight made it.
    """

    def __init__(self) -> None:
        #: Set by the role that makes the attempts: it declares no `api_key_env`.
        self.unauthenticated = False
        self._failed: list[tuple[int | None, str | None, bool]] = []
        self._preflight = False
        #: Each exhaustion seen, with the index of the attempt that ended it.
        self._exhausted: list[tuple[RoleCallExhausted, int | None]] = []
        self._failure: str | None = None

    @contextmanager
    def preflight(self) -> Iterator[None]:
        """Attribute the attempts made inside to the tool-dialect preflight."""

        previous, self._preflight = self._preflight, True
        try:
            yield
        finally:
            self._preflight = previous

    def observe(self, telemetry: AttemptTelemetry) -> None:
        if telemetry.outcome == "ok":
            return
        status = telemetry.http_status
        detail = telemetry.provider_code if status is not None else telemetry.outcome
        self._failed.append((status, detail, self._preflight))

    def exhausted(self, exc: RoleCallExhausted) -> None:
        """A logical request's retry layer gave up on its last observed
        attempt. A caller may still go on (`withdraw`); `end` says whether this
        exhaustion ended the invocation."""

        self._exhausted.append((exc, len(self._failed) - 1 if self._failed else None))

    def withdraw(self, exc: BaseException | None) -> None:
        """The attempt that ended exhaustion `exc` was negotiation, not a
        failure: a preflight's dialect rejection the caller answers with its
        next style. Earlier failed attempts of the same request still count."""

        for index, (seen, last) in enumerate(self._exhausted):
            if seen is exc:
                del self._exhausted[index]
                if last is not None:
                    del self._failed[last]
                    self._exhausted = [
                        (other, None if at is None else at - (at > last))
                        for other, at in self._exhausted
                    ]
                return

    def end(self, exc: BaseException) -> None:
        """The invocation ended on `exc`: a provider failure when `exc` is, or
        is directly caused by, a `RoleCallExhausted`."""

        cause = exc if isinstance(exc, RoleCallExhausted) else exc.__cause__
        if isinstance(cause, RoleCallExhausted):
            self._failure = cause.reason

    def fail(self, reason: str) -> None:
        """The invocation ended in a failure no exhaustion raised: an
        operation deadline passing before a request, or the runner ending the
        role at its deadline or on a stop."""

        self._failure = reason

    @property
    def failure(self) -> str | None:
        return self._failure

    def report(self, *, unauthenticated: bool | None = None) -> AttemptReport | None:
        """The report, or `None` when no attempt failed and nothing ended in a
        provider failure. `unauthenticated` defaults to the role's mark."""

        if unauthenticated is None:
            unauthenticated = self.unauthenticated

        if not self._failed and self._failure is None:
            return None
        summary = AttemptSummary(
            tuple((status, detail) for status, detail, _ in self._failed),
            preflight=bool(self._failed) and all(pre for _, _, pre in self._failed),
        )
        return AttemptReport(summary, self._failure, unauthenticated)


async def call_with_retry(
    send: Callable[[float], Awaitable[T]],
    *,
    policy: RoleCallRetry,
    role: str,
    request_id: str,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    on_attempt: Callable[[AttemptTelemetry], None] = lambda _: None,
) -> T:
    budget_end = min(deadline, clock() + policy.total_budget_seconds)
    attempt = 0
    while True:
        attempt += 1
        remaining = budget_end - clock()
        if remaining <= 0:
            raise RoleCallExhausted("deadline_exhausted", attempt - 1, "timeout")
        started = clock()
        try:
            result = await send(remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A defect inside `send` (a `TypeError`, say) is not the provider
            # refusing: it propagates instead of becoming a silent fallback.
            if not isinstance(exc, _provider_exceptions()):
                raise
            kind, code = classify_provider_error(exc)
            on_attempt(
                AttemptTelemetry(
                    role,
                    request_id,
                    attempt,
                    clock() - started,
                    code,
                    *provider_error_detail(exc),
                )
            )
            if kind != "transient":
                raise RoleCallExhausted("provider_rejected", attempt, code) from exc
            # The enclosing operation deadline passing during the attempt is
            # the operation's exhaustion, not provider unavailability; running
            # out of `max_attempts` or the role's own budget stays the latter.
            if clock() >= deadline:
                raise RoleCallExhausted("deadline_exhausted", attempt, code) from exc
            if attempt >= policy.max_attempts:
                raise RoleCallExhausted("provider_unavailable", attempt, code) from exc
            wait = rng() * policy.ceiling(attempt)
            if budget_end - clock() <= wait:
                raise RoleCallExhausted("provider_unavailable", attempt, code) from exc
            await sleep(wait)
            continue
        on_attempt(AttemptTelemetry(role, request_id, attempt, clock() - started, "ok"))
        return result
