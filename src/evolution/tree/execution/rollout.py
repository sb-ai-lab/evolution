"""Execute, recover and retry transport-neutral tree interactions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from math import isfinite
from typing import Any, TypeVar
from uuid import uuid4

from evolution.tree.execution.interaction import (
    Environment,
    EvalCase,
    History,
    InstanceRecovery,
    MaterializationStrategy,
    MaterializedCandidateT,
    Message,
    Provenance,
    ResumeDisposition,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutJournal,
    RolloutKey,
    RolloutRecoveryError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    SeedMode,
    TargetAdapter,
    TargetIdentity,
    TargetInstance,
    TargetSession,
    Trajectory,
    Turn,
    TurnResult,
    _require_nonblank,
    _validate_optional_seed,
)

# The one error-code shape: the writer spells a journaled `error_code` with
# the same regex the S1 schema admits it by, so `_code_of` can never produce a
# code `journal_schema` would refuse (which would be a journal fault rather
# than a diagnosable error).
from evolution.tree.execution.journal_schema import ERROR_CODE as _ERROR_CODE
from evolution.tree.execution.sample_outcomes import (
    Terminal,
    disposition_for,
)
from evolution.tree.storage.journal_errors import JournalBoundaryError


class _RolloutDeadlineExceeded(TimeoutError):
    """Private signal proving that the core's own deadline expired."""


def classify_rollout_resume(
    planned: tuple[RolloutKey, ...], records: tuple[Mapping[str, Any], ...]
) -> tuple[ResumeDisposition, ...]:
    """Classify planned samples without ever replaying a partly executed session."""

    if len(set(planned)) != len(planned):
        raise RolloutRecoveryError("planned rollout keys must be unique")
    evidence: dict[RolloutKey, dict[str, Any]] = {
        key: {"starts": 0, "finishes": []} for key in planned
    }
    for record in records:
        event = record.get("event")
        if event not in {"rollout_started", "rollout_finished"}:
            continue
        try:
            key = RolloutKey(record["case_id"], record["sample_id"], record["candidate_digest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutRecoveryError(f"malformed {event} identity") from exc
        if key not in evidence:
            continue
        if event == "rollout_started":
            evidence[key]["starts"] += 1
            continue
        terminal = record.get("terminal")
        turns = record.get("turns")
        duration = record.get("duration_seconds")
        if terminal not in {
            "completed",
            "turn_budget",
            "time_budget",
            "target_failed",
            "candidate_failed",
            "cancelled",
            "interrupted",
        }:
            raise RolloutRecoveryError("rollout_finished has invalid terminal")
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
            raise RolloutRecoveryError("rollout_finished has invalid turn count")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not isfinite(float(duration))
            or duration < 0
        ):
            raise RolloutRecoveryError("rollout_finished has invalid duration")
        evidence[key]["finishes"].append(terminal)

    dispositions: list[ResumeDisposition] = []
    for key in planned:
        starts = evidence[key]["starts"]
        finishes = evidence[key]["finishes"]
        if starts > 1 or len(finishes) > 1 or (finishes and starts != 1):
            raise RolloutRecoveryError("ambiguous rollout lifecycle evidence")
        if finishes:
            dispositions.append(ResumeDisposition(key, "reuse", finishes[0], None))
        elif starts == 1:
            dispositions.append(
                ResumeDisposition(
                    key,
                    "interrupt",
                    "interrupted",
                    disposition_for("interrupted"),
                )
            )
        else:
            dispositions.append(ResumeDisposition(key, "schedule", None, None))
    return tuple(dispositions)


async def materialize_target(
    adapter: TargetAdapter[MaterializedCandidateT],
    candidate: MaterializedCandidateT,
    strategy: MaterializationStrategy,
    instance_id: str,
    journal: RolloutJournal,
    *,
    attempts: int | None = None,
) -> TargetInstance:
    """Materialize an instance after durably recording ownership of its id.

    `attempts` (`call_retries.max_attempts`; `None` before `/8`) resends
    a transient failure under the same `instance_id` and, for an adapter whose
    `materialize` accepts a `request_id` keyword, the same `request_id`, so a
    creation the target completed but whose answer was lost is replayed rather
    than duplicated.
    """

    _require_nonblank("instance_id", instance_id)
    _require_nonblank("candidate digest", candidate.digest)
    if strategy not in adapter.capabilities.materialization:
        raise ValueError("adapter does not support the materialization strategy")
    identity = {
        "instance_id": instance_id,
        "candidate_digest": candidate.digest,
        "strategy": strategy,
    }
    materialize: Callable[[], Awaitable[TargetInstance]] = partial(
        adapter.materialize, candidate, strategy, instance_id
    )
    if _accepts_keyword(adapter.materialize, "request_id"):
        # The protocol does not declare the keyword (a custom adapter need not
        # take it); an adapter whose signature does is handed one id to resend.
        keyed: Callable[..., Awaitable[TargetInstance]] = adapter.materialize
        materialize = partial(keyed, candidate, strategy, instance_id, request_id=uuid4().hex)

    async def attempt_once(attempt: int | None) -> TargetInstance:
        metadata = {"operation_id": uuid4().hex, **identity, **_attempt_evidence(attempt)}
        started = asyncio.get_running_loop().time()
        journal.append({"event": "target_materialize_started", **metadata})
        try:
            instance = await materialize()
            if instance.instance_id != instance_id:
                raise RolloutInfrastructureError(
                    "adapter returned a different instance id", "invalid_response"
                )
        except JournalBoundaryError:
            raise
        except BaseException as exc:
            journal.append(
                {
                    "event": "target_materialize_failed",
                    **metadata,
                    "latency_ms": _elapsed_ms(started),
                    "error_class": type(exc).__name__,
                    **_failure_evidence(attempt, exc),
                }
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            if isinstance(exc, RolloutInfrastructureError):
                raise
            raise RolloutInfrastructureError("target materialize failed") from exc
        journal.append(
            {
                "event": "target_materialize_finished",
                **metadata,
                "latency_ms": _elapsed_ms(started),
            }
        )
        return instance

    return await _attempt_loop(attempt_once, attempts, None)


async def release_target(
    adapter: TargetAdapter[Any],
    instance_id: str,
    candidate_digest: str,
    strategy: MaterializationStrategy,
    journal: RolloutJournal,
    *,
    attempts: int | None = None,
) -> None:
    """Release an instance by durable id, including after core restart."""

    recovery = InstanceRecovery(instance_id, candidate_digest, strategy)
    identity = {
        "instance_id": recovery.instance_id,
        "candidate_digest": recovery.candidate_digest,
        "strategy": recovery.strategy,
    }

    async def attempt_once(attempt: int | None) -> None:
        metadata = {"operation_id": uuid4().hex, **identity, **_attempt_evidence(attempt)}
        started = asyncio.get_running_loop().time()
        journal.append({"event": "target_release_started", **metadata})
        try:
            await adapter.release(instance_id)
        except JournalBoundaryError:
            raise
        except BaseException as exc:
            journal.append(
                {
                    "event": "target_release_failed",
                    **metadata,
                    "latency_ms": _elapsed_ms(started),
                    "error_class": type(exc).__name__,
                    **_failure_evidence(attempt, exc),
                }
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise RolloutInfrastructureError("target release failed") from exc
        journal.append(
            {
                "event": "target_release_finished",
                **metadata,
                "latency_ms": _elapsed_ms(started),
            }
        )

    await _attempt_loop(attempt_once, attempts, None)


async def recover_target_instances(
    adapter: TargetAdapter[Any],
    records: tuple[Mapping[str, Any], ...],
    journal: RolloutJournal,
) -> tuple[str, ...]:
    """Release every instance whose durable ownership has no finished release."""

    recoveries = _instances_requiring_release(records)
    failures: list[tuple[str, RolloutInfrastructureError]] = []
    for recovery in recoveries:
        try:
            await release_target(
                adapter,
                recovery.instance_id,
                recovery.candidate_digest,
                recovery.strategy,
                journal,
            )
        except JournalBoundaryError:
            raise
        except RolloutInfrastructureError as exc:
            failures.append((recovery.instance_id, exc))
    if failures:
        failed_ids = ", ".join(instance_id for instance_id, _ in failures)
        failure = RolloutInfrastructureError(
            f"target recovery failed for instances: {failed_ids}",
            primary_failure=failures[0][1],
        )
        for instance_id, recorded_failure in failures:
            failure.add_note(f"{instance_id}: {recorded_failure}")
        raise failure from failures[0][1]
    return tuple(recovery.instance_id for recovery in recoveries)


def _instances_requiring_release(
    records: tuple[Mapping[str, Any], ...],
) -> tuple[InstanceRecovery, ...]:
    lifecycle_events = {
        "target_materialize_started",
        "target_materialize_finished",
        "target_materialize_failed",
        "target_release_started",
        "target_release_finished",
        "target_release_failed",
    }
    states: dict[str, dict[str, Any]] = {}
    for record in records:
        event = record.get("event")
        if event not in lifecycle_events:
            continue
        try:
            recovery = InstanceRecovery(
                record["instance_id"],
                record["candidate_digest"],
                record["strategy"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutRecoveryError(f"malformed {event} identity") from exc
        state = states.get(recovery.instance_id)
        if event == "target_materialize_started":
            if state is None:
                states[recovery.instance_id] = {
                    "recovery": recovery,
                    "materialize_starts": 1,
                    "materialize_terminals": 0,
                    "release_starts": 0,
                    "release_terminals": 0,
                    "released": False,
                }
            else:
                if recovery != state["recovery"]:
                    raise RolloutRecoveryError("instance lifecycle identity changed")
                if state["released"] or state["release_starts"]:
                    raise RolloutRecoveryError("materialize retry followed release")
                state["materialize_starts"] += 1
            continue
        if state is None:
            raise RolloutRecoveryError(f"{event} without materialize evidence")
        if recovery != state["recovery"]:
            raise RolloutRecoveryError("instance lifecycle identity changed")
        if state["released"]:
            raise RolloutRecoveryError("instance lifecycle continued after release")
        if event in {"target_materialize_finished", "target_materialize_failed"}:
            if (
                state["materialize_terminals"] >= state["materialize_starts"]
                or state["release_starts"]
            ):
                raise RolloutRecoveryError("ambiguous materialize evidence")
            state["materialize_terminals"] += 1
        elif event == "target_release_started":
            state["release_starts"] += 1
        else:
            if state["release_starts"] <= state["release_terminals"]:
                raise RolloutRecoveryError(f"{event} without release start")
            state["release_terminals"] += 1
            if event == "target_release_finished":
                state["released"] = True
    return tuple(state["recovery"] for state in states.values() if not state["released"])


class _ProvenanceUnion:
    """A sample's provenance: the union of `used` over its
    turns, bounded while the rollout runs (ADR-0024 and its 2026-10-01
    amendment).

    Three states. Collecting keeps the identifiers in `ids`. Unknown (a turn
    reported none) is sticky and wins over overflow, as the writer's rule
    does: no stored keys. Overflow (the union passed `PROVENANCE_MAX_USED`
    identifiers or `PROVENANCE_MAX_BYTES` of canonical JSON) drops the
    identifiers and is sticky until a turn makes the sample unknown.

    The canonical JSON of a list of strings is `[`, each string's own
    encoding joined by `,`, and `]`, whatever the order, so a running sum of
    the new identifiers' encoded lengths is that encoding's length. It is
    confirmed by one exact encode before switching to overflow.
    """

    __slots__ = ("ids", "unknown", "overflow", "_bytes")

    def __init__(self) -> None:
        self.ids: set[str] | None = set()
        self.unknown = False
        self.overflow = False
        self._bytes = 2

    def add(self, used: list[str] | None) -> None:
        if self.unknown:
            return
        if used is None:
            self.ids = None
            self.unknown = True
            self.overflow = False
            return
        if self.ids is None:
            return
        from evolution.tree.execution.wire_v2 import PROVENANCE_MAX_BYTES, PROVENANCE_MAX_USED

        for item in used:
            if item not in self.ids:
                self._bytes += len(_canonical_string(item)) + (1 if self.ids else 0)
                self.ids.add(item)
        if len(self.ids) > PROVENANCE_MAX_USED:
            self._drop()
        elif self._bytes > PROVENANCE_MAX_BYTES:
            self._bytes = len(_canonical_list(sorted(self.ids)))
            if self._bytes > PROVENANCE_MAX_BYTES:
                self._drop()

    def _drop(self) -> None:
        self.ids = None
        self.overflow = True

    def event_fields(self) -> dict[str, Any]:
        if self.overflow:
            return {"used": None, "used_overflow": True}
        return {"used": None if self.ids is None else sorted(self.ids)}


def _canonical_string(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _canonical_list(value: list[str]) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def rollout(
    case: EvalCase,
    sample: RolloutSample,
    instance: TargetInstance,
    environment: Environment,
    budget: RolloutBudget,
    journal: RolloutJournal,
) -> Trajectory:
    """Execute one active sample after the caller acquires concurrency permits.

    The wall-clock deadline starts on entry. Scheduler queue time must be measured
    by the caller and must not be deducted from ``budget.timeout_seconds``.
    """

    loop = asyncio.get_running_loop()
    rollout_started_at = loop.time()
    max_turns = _effective_max_turns(case, budget)
    environment_view = case.environment_view()
    event_base = {
        "operation_id": uuid4().hex,
        "case_id": case.case_id,
        "sample_id": sample.sample_id,
        "candidate_digest": sample.candidate_digest,
        "instance_id": instance.instance_id,
    }
    deadline = rollout_started_at + float(budget.timeout_seconds)
    journal.append({"event": "rollout_started", **event_base})
    request_scope = uuid4().hex
    history = History()
    session: TargetSession | None = None
    terminal: Terminal | None = None
    identity: TargetIdentity | None = None
    consecutive_failed = 0
    # The identifiers the agent reported using, over all turns,
    # collected only for a journal that persists them (a family-12 adapter).
    used = _ProvenanceUnion() if getattr(journal, "collects_provenance", False) else None
    try:
        try:
            validate_identity = partial(_validate_target_for_sample, sample=sample)
            identity_check = _core_check(budget, validate_identity)
            identity = await _external_call(
                instance.identity,
                deadline,
                journal,
                "target_identity",
                event_base,
                identity_check,
                attempts=_call_attempts(budget),
                cancelled=budget.cancelled,
            )
            if identity_check is None:
                validate_identity(identity)
        except _RolloutDeadlineExceeded as exc:
            raise RolloutTimeBudgetExceeded("target identity exceeded rollout time budget") from exc
        except JournalBoundaryError:
            raise
        except Exception as exc:
            raise _target_call_failure("target identity failed", exc, budget) from exc
        open_request_id = _request_id(request_scope, case.case_id, sample, "open")
        seed_mode = identity.semantic.seed_mode

        def keep_and_validate_session(opened: Any) -> None:
            # The session the target opened is this rollout's to close even
            # when the core check rejects it, so it is kept before the check
            # runs: the cleanup path closes whatever `session` holds.
            nonlocal session
            session = opened
            _validate_session_for_sample(opened, sample, seed_mode)

        try:
            session_check = _core_check(budget, keep_and_validate_session)
            session = await _external_call(
                partial(instance.open, case.public, sample.requested_seed, open_request_id),
                deadline,
                journal,
                "target_open",
                {**event_base, "request_id": open_request_id},
                session_check,
                attempts=_call_attempts(budget),
                cancelled=budget.cancelled,
            )
            if session_check is None:
                _validate_session_for_sample(session, sample, seed_mode)
        except _RolloutDeadlineExceeded as exc:
            raise RolloutTimeBudgetExceeded("target open exceeded rollout time budget") from exc
        except JournalBoundaryError:
            raise
        except Exception as exc:
            raise _target_call_failure("target open failed", exc, budget) from exc
        while True:
            if budget.cancelled():
                terminal = "cancelled"
                break
            try:
                message = await _external_call(
                    partial(environment.next, environment_view, history),
                    deadline,
                    journal,
                    "environment_next",
                    {**event_base, "turn": len(history.turns)},
                )
            except _RolloutDeadlineExceeded:
                terminal = "time_budget"
                break
            except JournalBoundaryError:
                raise
            except Exception as exc:
                raise RolloutEnvironmentError("Environment failed") from exc
            if message is None:
                terminal = "completed"
                break
            if not isinstance(message, Message):
                raise RolloutEnvironmentError("Environment returned an invalid Message")
            if budget.cancelled():
                terminal = "cancelled"
                break
            if len(history.turns) >= max_turns:
                terminal = "turn_budget"
                break
            request_id = _request_id(
                request_scope, case.case_id, sample, "send", len(history.turns)
            )
            try:
                result = await _external_call(
                    partial(instance.send, session, message, request_id),
                    deadline,
                    journal,
                    "target_send",
                    {**event_base, "request_id": request_id, "turn": len(history.turns)},
                    result_summary=_turn_result_summary,
                    attempts=_call_attempts(budget, turn=True),
                    cancelled=budget.cancelled,
                )
            except _RolloutDeadlineExceeded:
                terminal = "time_budget"
                break
            except JournalBoundaryError:
                raise
            except Exception as exc:
                raise _target_call_failure("target send failed", exc, budget) from exc
            history = History(
                (
                    *history.turns,
                    Turn(
                        len(history.turns),
                        message,
                        result.events,
                        result.usage,
                        result.latency_ms,
                        result.outcome,
                        result.reason,
                    ),
                )
            )
            if used is not None:
                used.add(None if result.provenance is None else result.provenance["used"])
            if result.outcome == "target_failed":
                terminal = "target_failed"
                break
            if result.outcome == "candidate_failed":
                consecutive_failed += 1
                if consecutive_failed >= budget.max_failed_turns:
                    terminal = "candidate_failed"
                    break
            else:
                consecutive_failed = 0
    except JournalBoundaryError:
        raise
    except BaseException as primary_failure:
        if session is not None:
            await _close_session(
                instance,
                session,
                journal,
                _session_event_metadata(event_base, session),
                float(budget.timeout_seconds),
                budget=budget,
                primary_failure=primary_failure,
            )
        raise
    else:
        if session is not None:
            await _close_session(
                instance,
                session,
                journal,
                _session_event_metadata(event_base, session),
                float(budget.timeout_seconds),
                budget=budget,
            )
    if identity is None or terminal is None:
        raise RolloutInfrastructureError("rollout did not produce identity and terminal state")
    validate_drift = partial(_require_stable_identity, expected=identity)
    drift_check = _core_check(budget, validate_drift)
    try:
        final_identity = await _external_call(
            instance.identity,
            loop.time() + float(budget.timeout_seconds),
            journal,
            "target_identity",
            {**event_base, "phase": "after_close"},
            drift_check,
            attempts=_call_attempts(budget),
            cancelled=budget.cancelled,
        )
    except JournalBoundaryError:
        raise
    except Exception as exc:
        raise _target_call_failure("target identity failed after close", exc, budget) from exc
    if drift_check is None:
        validate_drift(final_identity)
    trajectory = Trajectory(
        case.case_id,
        sample.sample_id,
        sample.candidate_digest,
        environment.identity.digest,
        history,
        terminal,
        Provenance(identity, session.requested_seed, session.effective_seed),
    )
    journal.append(
        {
            "event": "rollout_finished",
            **event_base,
            "terminal": terminal,
            "turns": len(history.turns),
            "duration_seconds": loop.time() - rollout_started_at,
            **({} if used is None else used.event_fields()),
        }
    )
    return trajectory


def _elapsed_ms(started: float) -> int:
    elapsed = asyncio.get_running_loop().time() - started
    return max(0, int(elapsed * 1000))


#: Seams for the back-off (tests replace them). A cancelled task is never
#: retried: `asyncio.CancelledError` is excluded from `_may_resend`, and the
#: back-off sleep is itself cancellable.
_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep
_JITTER: Callable[[], float] = random.random
_T = TypeVar("_T")


def backoff_delay(attempt: int, jitter: float) -> float:
    """Full-jitter exponential back-off before attempt `attempt + 1`."""
    return jitter * min(30.0, 0.5 * 2 ** (attempt - 1))


def _call_attempts(budget: RolloutBudget, *, turn: bool = False) -> int | None:
    """How often a target call is attempted: `None` (once, no evidence keys)
    without `call_retries`, else the declared count -- `1` included, so a `/8`
    journal always carries `call_attempt` and `error_code`."""
    retries = budget.call_retries
    if retries is None:
        return None
    return retries.turn_max_attempts if turn else retries.max_attempts


def _may_resend(exc: BaseException) -> bool:
    """The one retry rule: the failure's code decides (ADR-0019), and
    cancellation, interruption and a journal fault are never resent."""
    if isinstance(
        exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, JournalBoundaryError)
    ):
        return False
    from evolution.tree.execution.call_failures import may_retry

    return may_retry(exc)


def _attempt_evidence(attempt: int | None) -> dict[str, Any]:
    """`call_attempt` is written only when `call_retries` is configured (`/8`)."""
    return {} if attempt is None else {"call_attempt": attempt}


def _code_of(exc: BaseException) -> str:
    """The typed code a failure is coded by (`call_failures._typed`, when it
    matches `_ERROR_CODE`), else the exception's class name. Shared by the
    journaled `error_code` (`_failure_evidence`) and the exhausted-call
    classification gate (`_target_call_failure`, ADR-0019)."""
    from evolution.tree.execution.call_failures import _typed

    typed = _typed(exc)
    code = getattr(typed, "code", None)
    if not isinstance(code, str) or _ERROR_CODE.match(code) is None:
        return type(exc).__name__
    return code


def _failure_evidence(attempt: int | None, exc: BaseException | str) -> dict[str, Any]:
    """`error_code` on a `_failed` record: the typed code, else the class name."""
    if attempt is None:
        return {}
    if isinstance(exc, str):
        return {"error_code": exc}
    return {"error_code": _code_of(exc)}


def _core_check(
    budget: RolloutBudget, check: Callable[[Any], None]
) -> Callable[[Any], Mapping[str, Any]] | None:
    """Run a deterministic core check inside the call whose result it judges (`/8`).

    The three checks that read a *successful* call's value -- the sample's
    Candidate identity, the session's seed contract, and the identity drift
    after close -- are the core's own, never target instability.
    Under `call_retries` they run as that call's `result_summary`, so a failure
    is journaled as the operation's own `_failed` record carrying the check's
    `error_code`: a fault row's class must be recoverable from the journal,
    and `identity_changed` is what makes a drift suspend as
    `target_changed` rather than `target_contract_violated`.

    `None` below `/8`: the caller runs the same check after the call and the
    journal keeps the `_finished` record every earlier family writes. Neither
    code is transient, so a check that raises inside the attempt loop is never
    resent (`call_failures.may_retry`).
    """

    if budget.call_retries is None:
        return None

    def summary(result: Any) -> Mapping[str, Any]:
        check(result)
        return {}

    return summary


def _target_call_failure(message: str, exc: BaseException, budget: RolloutBudget) -> BaseException:
    """The wrap for an exhausted target-call boundary failure:
    `RolloutTargetUnstable` for a transient class under `call_retries`, else
    the plain `RolloutInfrastructureError` every earlier manifest still gets.
    Callers `raise _target_call_failure(...) from exc`."""
    from evolution.tree.execution.call_failures import classify_failure

    if budget.call_retries is not None and classify_failure(exc) == "transient":
        return RolloutTargetUnstable(message, _code_of(exc))
    return RolloutInfrastructureError(message)


def _accepts_keyword(function: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


async def _attempt_loop(
    attempt_once: Callable[[int | None], Awaitable[_T]],
    attempts: int | None,
    deadline: float | None,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    jitter: Callable[[], float] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> _T:
    """Run `attempt_once` up to `attempts` times; the retry rule lives here.

    `attempt_once` receives the 1-based attempt number whenever `attempts` is
    declared (`call_retries` configured, even at `1`), else `None`: no
    evidence keys, the records every earlier manifest writes. A failure is
    resent only when `_may_resend` admits it, attempts remain, the jittered
    back-off ends before `deadline` (the rollout's; `None` for
    materialize/release), and
    -- when the caller passes a `cancelled` check (`rollout`'s own calls,
    never materialize/release, which have no budget) -- `cancelled()` is not
    yet true. A cancelled campaign starts no further retry. The last failure
    is what a cancellation re-raises, not a synthetic signal.
    """
    if attempts is not None and attempts < 1:
        raise ValueError("attempts must be positive")
    limit = 1 if attempts is None else attempts
    attempt = 1
    while True:
        try:
            return await attempt_once(None if attempts is None else attempt)
        except BaseException as exc:
            if attempt >= limit or not _may_resend(exc):
                raise
            if cancelled is not None and cancelled():
                raise
            delay = backoff_delay(attempt, (jitter or _JITTER)())
            if deadline is not None and deadline - asyncio.get_running_loop().time() <= delay:
                raise
        await (sleep or _SLEEP)(delay)
        attempt += 1


async def _external_call(
    call: Callable[[], Awaitable[Any]],
    deadline: float,
    journal: RolloutJournal,
    operation: str,
    metadata: Mapping[str, Any],
    result_summary: Callable[[Any], Mapping[str, Any]] | None = None,
    *,
    attempts: int | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    jitter: Callable[[], float] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Any:
    """One journaled boundary call, attempted up to `attempts` times.

    Each attempt is its own `<operation>_started`/`_failed`/`_finished` pair
    with a fresh `operation_id`; with `attempts` declared every record
    carries `call_attempt` and a `_failed` record `error_code`. A cancelled task is
    never retried: `CancelledError` is excluded by `_may_resend`, and passing
    `cancelled` (the rollout's `budget.cancelled`) stops a further resend once
    the campaign is cancelled between attempts.
    """

    async def attempt_once(attempt: int | None) -> Any:
        return await _external_attempt(
            call, deadline, journal, operation, metadata, result_summary, attempt
        )

    return await _attempt_loop(
        attempt_once, attempts, deadline, sleep=sleep, jitter=jitter, cancelled=cancelled
    )


async def _external_attempt(
    call: Callable[[], Awaitable[Any]],
    deadline: float,
    journal: RolloutJournal,
    operation: str,
    metadata: Mapping[str, Any],
    result_summary: Callable[[Any], Mapping[str, Any]] | None,
    attempt: int | None,
) -> Any:
    started = asyncio.get_running_loop().time()
    metadata = {**metadata, "operation_id": uuid4().hex, **_attempt_evidence(attempt)}
    journal.append({"event": f"{operation}_started", **metadata})
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        journal.append(
            {
                "event": f"{operation}_failed",
                **metadata,
                "latency_ms": 0,
                "error_class": "deadline_timeout",
                **_failure_evidence(attempt, "deadline_timeout"),
            }
        )
        raise _RolloutDeadlineExceeded(f"{operation} exceeded rollout deadline")
    try:
        awaitable = call()
        # `asyncio.timeout`, not `wait_for`: on 3.11 `wait_for` returns a reply
        # that finished in the same step as a stop and drops the cancellation (#164).
        async with asyncio.timeout(remaining):
            result = await awaitable
        summary = dict(result_summary(result)) if result_summary else {}
    except TimeoutError as exc:
        deadline_expired = asyncio.get_running_loop().time() >= deadline
        journal.append(
            {
                "event": f"{operation}_failed",
                **metadata,
                "latency_ms": _elapsed_ms(started),
                "error_class": "deadline_timeout" if deadline_expired else type(exc).__name__,
                **_failure_evidence(attempt, "deadline_timeout" if deadline_expired else exc),
            }
        )
        if deadline_expired:
            raise _RolloutDeadlineExceeded(f"{operation} exceeded rollout deadline") from exc
        raise
    except JournalBoundaryError:
        raise
    except BaseException as exc:
        journal.append(
            {
                "event": f"{operation}_failed",
                **metadata,
                "latency_ms": _elapsed_ms(started),
                "error_class": type(exc).__name__,
                **_failure_evidence(attempt, exc),
            }
        )
        raise
    journal.append(
        {
            "event": f"{operation}_finished",
            **metadata,
            "latency_ms": _elapsed_ms(started),
            **summary,
        }
    )
    return result


async def _close_session(
    instance: TargetInstance,
    session: TargetSession,
    journal: RolloutJournal,
    metadata: Mapping[str, Any],
    timeout_seconds: float,
    *,
    budget: RolloutBudget,
    primary_failure: BaseException | None = None,
) -> None:
    """Close the session, and decide what a close failure means.

    The scorer-free part of close is a target call like the other four: under
    `/8` an exhausted *transient* close ends the sample `target_failed`
    instead of becoming a fault row. That reclassification is only for the
    cleanup that follows a *successful* rollout; when a primary failure is
    already propagating, the close failure keeps carrying it, so a broken
    close never masks what really ended the sample.
    """

    try:
        await _external_call(
            partial(instance.close, session),
            asyncio.get_running_loop().time() + timeout_seconds,
            journal,
            "target_close",
            metadata,
            attempts=_call_attempts(budget),
        )
    except JournalBoundaryError:
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, asyncio.CancelledError) and primary_failure is None:
            raise
        if primary_failure is None:
            raise _target_call_failure("target close failed", exc, budget) from exc
        failure = RolloutInfrastructureError("target close failed", primary_failure=primary_failure)
        failure.add_note(f"Primary failure before cleanup: {type(primary_failure).__name__}")
        raise failure from exc


def _session_event_metadata(base: Mapping[str, Any], session: TargetSession) -> dict[str, Any]:
    metadata = dict(base)
    session_id = getattr(session, "session_id", None)
    if isinstance(session_id, str) and session_id.strip():
        metadata["session_id"] = session_id
    return metadata


def _effective_max_turns(case: EvalCase, budget: RolloutBudget) -> int:
    scenario_limit = case.private.get("max_turns")
    if scenario_limit is None:
        return budget.max_turns
    if (
        not isinstance(scenario_limit, int)
        or isinstance(scenario_limit, bool)
        or scenario_limit < 1
    ):
        raise ValueError('private["max_turns"] must be a positive integer')
    return min(budget.max_turns, scenario_limit)


def _validate_session_for_sample(
    session: TargetSession,
    sample: RolloutSample,
    seed_mode: SeedMode,
) -> None:
    _require_nonblank("session_id", session.session_id)
    if session.requested_seed != sample.requested_seed:
        raise RolloutInfrastructureError(
            "session requested seed does not match sample", "invalid_response"
        )
    if seed_mode == "exact" and session.effective_seed != sample.requested_seed:
        raise RolloutInfrastructureError(
            "session did not confirm the exact seed", "invalid_response"
        )
    if seed_mode == "unpaired":
        try:
            _validate_optional_seed("session effective_seed", session.effective_seed)
        except ValueError as exc:
            raise RolloutInfrastructureError(
                "unpaired session reported an invalid seed", "invalid_response"
            ) from exc
    if seed_mode == "none" and session.effective_seed is not None:
        raise RolloutInfrastructureError(
            "seedless session must not claim an effective seed", "invalid_response"
        )


def _require_stable_identity(found: TargetIdentity, *, expected: TargetIdentity) -> None:
    # A deterministic core check over the after-close identity call: an
    # instance that changed under the rollout invalidates its evidence, and
    # `identity_changed` is structural.
    if found != expected:
        raise RolloutInfrastructureError(
            "target identity changed during rollout", "identity_changed"
        )


def _validate_target_for_sample(identity: TargetIdentity, sample: RolloutSample) -> None:
    # A deterministic core check, never target instability: an identity
    # mismatch is structural, a seed-contract mismatch is
    # contract, whichever code the target itself reported nothing about.
    if identity.instance.loaded_candidate_digest != sample.candidate_digest:
        raise RolloutInfrastructureError("target loaded a different Candidate", "identity_changed")
    if identity.semantic.seed_mode == "exact" and sample.requested_seed is None:
        raise RolloutInfrastructureError(
            "exact target requires a requested seed", "invalid_response"
        )
    if identity.semantic.seed_mode != "exact" and sample.requested_seed is not None:
        raise RolloutInfrastructureError(
            "unpaired target must not receive a seed", "invalid_response"
        )


def _request_id(
    request_scope: str,
    case_id: str,
    sample: RolloutSample,
    operation: str,
    turn: int | None = None,
) -> str:
    raw = json.dumps(
        [request_scope, sample.candidate_digest, case_id, sample.sample_id, operation, turn],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _turn_result_summary(result: TurnResult) -> Mapping[str, Any]:
    if not isinstance(result, TurnResult):
        # The core's own deterministic check, same standing as an identity or
        # seed-contract violation: never target instability.
        raise RolloutInfrastructureError("target send must return TurnResult", "invalid_response")
    return {
        "outcome": result.outcome,
        "target_latency_ms": result.latency_ms,
        "event_count": len(result.events),
        "usage": result.usage.to_payload() if result.usage else None,
    }
