"""Settlement facts for mutating `/evolution/v2` calls (ADR-0014).

A `TargetCallLedger` belongs to the caller and to one operation — a campaign or
a conformance run. Every `HttpTransport` built with it admits, classifies and
counts the `POST` and `DELETE` calls it makes; `GET` reads the target and is
neither counted nor refused. Nothing here is journaled: these are facts of this
process's execution, not measurements (ADR-0008).

*Two events, one class.* A call's I/O ends when its worker has closed the
connection and knows the network outcome — a complete HTTP/1.1 response, a
failure before the first request byte could leave (`not_sent`), or anything else
(`uncertain`, with a reason). Its delivery happens when the awaiting coroutine
resumes: with the worker's own result (a return or an exception the worker
raised), or abandoned (`CancelledError`, or an executor that never ran the
worker). The class is assigned once both have happened and never changes:

    worker result + response   -> completed
    abandoned     + response   -> uncertain, "abandoned"  (nobody read the reply)
    any           + not_sent   -> not_sent
    any           + uncertain  -> uncertain, the network reason

Until then the call is `pending`. An abandoned delivery before the worker starts
marks the call withdrawn: its I/O ends as `not_sent` on the spot, and a worker
the executor starts later sees the mark and opens no socket.

*What it cannot say.* The ledger proves only local facts. A request whose reply
never arrived may or may not have been processed by the target, and a late
reply never clears uncertainty. A completed call has not released what it
created; that is still `release`'s and the conformance sweep's job.

*Close and settle.* `close()` refuses every later mutating call before its
socket with `admission_closed`, cleanup included, so its supported point is
after the operation returned. The transport serializes a call's body before
admitting it, so `in_flight` counts socket I/O only, admitted calls whose worker
the executor has not started yet included, and `settle()` waits for that I/O
alone, which the binding's timeouts bound (at most
`3 * connect_seconds + read_seconds` from the worker's start, name resolution of
`localhost` aside); I/O end is marked before the transport's recorder runs, so a
blocked recorder shows as `pending`, not `in_flight`.
"""

from __future__ import annotations

import asyncio
import http.client
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from evolution.tree.execution.interaction import RolloutInfrastructureError
from evolution.tree.execution.wire_v2 import PREFIX, is_instance_id

__all__ = [
    "NetworkOutcome",
    "Operation",
    "RecordState",
    "TargetCallLedger",
    "TargetCallRecord",
    "TargetCallSettlement",
    "UncertainReason",
    "network_outcome",
]

Operation = Literal[
    "create_instance", "release_instance", "open_session", "close_session", "turn", "unknown"
]
UncertainReason = Literal[
    "abandoned", "callee_timeout", "transport_error", "response_too_large", "incomplete_response"
]
NetworkOutcome = Literal["response", "not_sent", "uncertain"]
RecordState = Literal["pending", "uncertain"]
_Class = Literal["completed", "not_sent", "uncertain"]

_MUTATING = frozenset({"POST", "DELETE"})
_INSTANCES = f"{PREFIX}/instances"
# (method, path segments after `/instances/`) -> operation; segment names are
# checked separately.
_SHAPES: dict[tuple[str, int], Operation] = {
    ("DELETE", 1): "release_instance",
    ("POST", 2): "open_session",
    ("DELETE", 3): "close_session",
    ("POST", 4): "turn",
}


@dataclass(frozen=True)
class TargetCallRecord:
    """One mutating call that is still `pending` or ended `uncertain`."""

    operation: Operation
    instance_id: str | None
    state: RecordState
    abandoned: bool
    reason: UncertainReason | None


@dataclass(frozen=True)
class TargetCallSettlement:
    """A snapshot of one ledger; see the module docstring for the classes."""

    namespace: str | None
    admission_closed: bool
    admitted: int
    completed: int
    not_sent: int
    uncertain: int
    pending: int
    in_flight: int
    refused: int
    uncertain_any: bool
    quiescent: bool
    bound_transports: int
    coverage: Literal["bound_transports"]
    records: tuple[TargetCallRecord, ...]


class _Call:
    __slots__ = (
        "abandoned",
        "delivered",
        "io",
        "klass",
        "operation",
        "raw_id",
        "reason",
        "started",
    )

    def __init__(self, operation: Operation, raw_id: str | None) -> None:
        self.operation = operation
        self.raw_id = raw_id
        self.started = False
        self.io: NetworkOutcome | None = None
        self.reason: UncertainReason | None = None
        self.delivered = False
        self.abandoned = False
        self.klass: _Class | None = None


def _parse(method: str, path: str, body: Mapping[str, Any] | None) -> tuple[Operation, str | None]:
    if method == "POST" and path == _INSTANCES:
        raw = body.get("instance_id") if isinstance(body, Mapping) else None
        return "create_instance", raw if isinstance(raw, str) else None
    if not path.startswith(f"{_INSTANCES}/"):
        return "unknown", None
    parts = path[len(_INSTANCES) + 1 :].split("/")
    operation = _SHAPES.get((method, len(parts)))
    if (
        operation is None
        or (len(parts) >= 2 and parts[1] != "sessions")
        or (len(parts) == 4 and parts[3] != "turns")
    ):
        return "unknown", None
    return operation, parts[0]


def network_outcome(
    sent: bool, failure: BaseException | None, complete: bool
) -> tuple[NetworkOutcome, UncertainReason | None]:
    """Classify one worker's I/O.

    `sent` is whether `connect()` returned, so request bytes may have left;
    `failure` is what the worker raised, if anything; `complete` is whether the
    body reached the end of its HTTP/1.1 framing.
    """
    if not sent:
        return "not_sent", None
    if failure is None:
        return ("response", None) if complete else ("uncertain", "incomplete_response")
    code = (
        getattr(failure, "code", None) if isinstance(failure, RolloutInfrastructureError) else None
    )
    if code == "callee_timeout":
        return "uncertain", "callee_timeout"
    if code == "response_too_large":
        return "uncertain", "response_too_large"
    if code == "transport_error":
        if isinstance(failure.__cause__, http.client.IncompleteRead):
            return "uncertain", "incomplete_response"
        return "uncertain", "transport_error"
    return "uncertain", "incomplete_response"


class TargetCallLedger:
    """The mutating calls of one operation against the managed target."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._namespace: str | None = None
        self._closed = False
        self._calls: list[_Call] = []
        self._io_open = 0
        self._refused = 0
        self._transports = 0

    # -- public ---------------------------------------------------------------

    def bind_namespace(self, namespace: str) -> None:
        """Bind the operation's `run_id`; the same value again is a no-op."""
        with self._cond:
            if self._namespace is None:
                self._namespace = namespace
            elif self._namespace != namespace:
                raise ValueError("the ledger is bound to another namespace")

    def close(self) -> None:
        """Refuse every later mutating call before its socket. Idempotent."""
        with self._cond:
            self._closed = True

    def report(self) -> TargetCallSettlement:
        with self._cond:
            return self._snapshot()

    async def settle(self, timeout: float | None = None) -> TargetCallSettlement:
        """Wait until no call's socket I/O is open (bodies are serialized before
        admission, so this never waits on serialization), or `timeout` elapses;
        never raises on timeout. Uses its own thread, not the loop's default executor.
        Cancellation-safe: cancelling the awaiting coroutine wakes the waiter
        thread and lets it end promptly instead of blocking forever."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TargetCallSettlement] = loop.create_future()
        cancelled = threading.Event()

        def wait() -> None:
            with self._cond:
                self._cond.wait_for(lambda: self._io_open == 0 or cancelled.is_set(), timeout)
                snapshot = self._snapshot()

            def resolve() -> None:
                if not future.done():
                    future.set_result(snapshot)

            try:
                loop.call_soon_threadsafe(resolve)
            except RuntimeError:
                pass  # the loop is already closed; nobody is left to resolve

        threading.Thread(target=wait, name="target-call-settle", daemon=True).start()
        try:
            return await future
        except asyncio.CancelledError:
            with self._cond:
                cancelled.set()
                self._cond.notify_all()
            raise

    # -- transport hooks ------------------------------------------------------

    def _bind_transport(self) -> None:
        with self._cond:
            self._transports += 1

    def _admit(self, method: str, path: str, body: Mapping[str, Any] | None) -> _Call | None:
        if method not in _MUTATING:
            return None
        with self._cond:
            if self._closed:
                self._refused += 1
                raise RolloutInfrastructureError(
                    "target call admission is closed", "admission_closed"
                )
            call = _Call(*_parse(method, path, body))
            self._calls.append(call)
            self._io_open += 1
            return call

    def _start(self, call: _Call) -> bool:
        with self._cond:
            if call.delivered:  # withdrawn: abandoned before the worker ran
                return False
            call.started = True
            return True

    def _io_end(self, call: _Call, outcome: NetworkOutcome, reason: UncertainReason | None) -> None:
        with self._cond:
            if call.io is not None:
                return
            call.io, call.reason = outcome, reason
            self._io_open -= 1
            self._finalize(call)
            self._cond.notify_all()

    def _deliver(self, call: _Call, *, abandoned: bool) -> None:
        with self._cond:
            if call.delivered:
                return
            call.delivered, call.abandoned = True, abandoned
            if abandoned and not call.started and call.io is None:
                call.io = "not_sent"
                self._io_open -= 1
            self._finalize(call)
            self._cond.notify_all()

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _finalize(call: _Call) -> None:
        if call.klass is not None or call.io is None or not call.delivered:
            return
        if call.io == "not_sent":
            call.klass, call.reason = "not_sent", None
        elif call.io == "response":
            if call.abandoned:
                call.klass, call.reason = "uncertain", "abandoned"
            else:
                call.klass = "completed"
        else:
            call.klass = "uncertain"

    def _visible(self, raw_id: str | None) -> str | None:
        namespace = self._namespace
        if raw_id is None or namespace is None or not is_instance_id(raw_id, namespace):
            return None
        return raw_id

    def _snapshot(self) -> TargetCallSettlement:
        counts = {"completed": 0, "not_sent": 0, "uncertain": 0}
        pending = 0
        records: list[TargetCallRecord] = []
        for call in self._calls:
            if call.klass is None:
                pending += 1
                records.append(
                    TargetCallRecord(
                        call.operation, self._visible(call.raw_id), "pending", call.abandoned, None
                    )
                )
                continue
            counts[call.klass] += 1
            if call.klass == "uncertain":
                records.append(
                    TargetCallRecord(
                        call.operation,
                        self._visible(call.raw_id),
                        "uncertain",
                        call.abandoned,
                        call.reason,
                    )
                )
        return TargetCallSettlement(
            namespace=self._namespace,
            admission_closed=self._closed,
            admitted=len(self._calls),
            completed=counts["completed"],
            not_sent=counts["not_sent"],
            uncertain=counts["uncertain"],
            pending=pending,
            in_flight=self._io_open,
            refused=self._refused,
            uncertain_any=counts["uncertain"] > 0,
            quiescent=self._closed and self._io_open == 0 and pending == 0,
            bound_transports=self._transports,
            coverage="bound_transports",
            records=tuple(records),
        )
