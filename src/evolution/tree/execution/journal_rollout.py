"""Native durable wrapper around the transport-neutral rollout kernel."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from evolution.tree.execution.interaction import (
    Environment,
    EvalCase,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    SemanticIdentity,
    TargetIdentity,
    TargetInstance,
    Trajectory,
)
from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1, canonical_json
from evolution.tree.execution.journal_schema import EXTERNAL_OPERATIONS, SAMPLE_FIELDS, SHA256
from evolution.tree.execution.rollout import rollout
from evolution.tree.execution.wire_v2 import PROVENANCE_MAX_BYTES, PROVENANCE_MAX_USED
from evolution.tree.storage.envelope import is_identifier
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalBoundaryError,
    JournalFault,
    JournalRecoveryRequired,
)


class NativeJournal(Protocol):
    def invalidate(self) -> None: ...

    @property
    def identity(self) -> RunIdentityV1: ...

    @property
    def corpus(self) -> Sequence[EvalCase]: ...

    def append(
        self, event: str, payload: dict[str, Any], *, operation_id: str | None = None
    ) -> Any: ...


def _failure(operation: str, error: Any) -> str:
    if error == "deadline_timeout":
        return "deadline_timeout"
    if error in ("CancelledError", "KeyboardInterrupt", "SystemExit"):
        return "cancelled"
    if operation == "environment_next":
        return "environment_error"
    if operation in (
        "target_materialize",
        "target_release",
        "target_identity",
        "target_open",
        "target_close",
    ):
        return "infrastructure_error"
    return "target_error"


class ConnectionAdmission:
    """One controller invocation's admission of one connection binding.

    The campaign creates it after a compatible connection preflight and hands
    it to every rollout under that binding. Each start names
    `binding_digest`; `claim()` returns `{operation_id,
    controller_invocation_id}` exactly once, for the first start, and `None`
    afterwards. The claim needs no lock: `rollout()` appends `rollout_started`
    synchronously on entry, so no other task runs between a claim and its
    write. A start the journal refuses before writing it returns the claim
    (`_restore`); any other failure keeps it spent, because the start may be
    durable and a binding has at most one admission.

    `spent(binding_digest)` is the binding-only token: every start under it
    names the binding and carries `connection_admission: null`. The campaign
    uses it once the binding's boundary is already durable (a resume after
    the first admitted start), so S1's marker rule -- both keys on every
    start -- holds without a second admission. The identity fields are
    read-only after construction.
    """

    __slots__ = ("_binding_digest", "_operation_id", "_invocation_id", "_claimed")

    def __init__(self, binding_digest: str, operation_id: str, invocation_id: str) -> None:
        if not (
            _digest(binding_digest)
            and all(is_identifier(value) for value in (operation_id, invocation_id))
        ):
            raise ValueError("invalid_connection_admission")
        self._binding_digest = binding_digest
        self._operation_id: str | None = operation_id
        self._invocation_id: str | None = invocation_id
        self._claimed = False

    @classmethod
    def spent(cls, binding_digest: str) -> ConnectionAdmission:
        """A token that names `binding_digest` and never admits (`claim()` is `None`)."""

        if not _digest(binding_digest):
            raise ValueError("invalid_connection_admission")
        token = cls.__new__(cls)
        token._binding_digest = binding_digest
        token._operation_id = None
        token._invocation_id = None
        token._claimed = True
        return token

    @property
    def binding_digest(self) -> str:
        return self._binding_digest

    @property
    def operation_id(self) -> str | None:
        """The compatible preflight's operation id; `None` for a spent token."""
        return self._operation_id

    @property
    def invocation_id(self) -> str | None:
        """The admitting invocation; `None` for a spent token."""
        return self._invocation_id

    def claim(self) -> dict[str, str] | None:
        """The admission for the first start under this binding, then `None`."""

        if self._claimed or self._operation_id is None or self._invocation_id is None:
            return None
        self._claimed = True
        return {
            "operation_id": self._operation_id,
            "controller_invocation_id": self._invocation_id,
        }

    def _restore(self) -> None:
        if self._operation_id is not None:
            self._claimed = False


def _digest(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


class JournalRolloutAdapter:
    """Allowlisted translation for kernel callbacks; core owns operation IDs."""

    def __init__(
        self,
        journal: NativeJournal,
        retry_ordinal: int | None = None,
        *,
        repair: bool = False,
        row_provenance: bool = False,
        components_digest: str | None = None,
        connection: ConnectionAdmission | None = None,
        admit: bool = True,
    ):
        self.journal = journal
        # With a token, the written `rollout_started`
        # names the binding and carries the token's one admission. `admit=False`
        # (`refuse_rows`' synthetic starts) names the binding and never claims,
        # so such a start carries `connection_admission: null`.
        self._connection = connection
        self._admit = admit
        # `None`: the identity declares no `sample_retries`, so rollout records
        # carry no ordinal and operation records stay at 0.
        self._retry_ordinal = retry_ordinal
        # A repair try says so on its `rollout_started` only.
        self._repair = repair
        # Only a family-12 measurement (the identity's
        # `row_provenance` marker) writes a row's provenance and the digest of
        # the component map its instance declared; others write neither.
        self._row_provenance = row_provenance
        self._components_digest = components_digest
        self.rollout_operation_id: str | None = None
        self.sample_payload: dict[str, Any] | None = None
        self.started = False
        self.terminal = False

    @property
    def collects_provenance(self) -> bool:
        """`rollout` reads it: only a marked journal's rollout gathers `used`."""
        return self._row_provenance

    def append(self, event: dict[str, Any]) -> int:
        try:
            return self._append(event)
        except JournalFault:
            self.journal.invalidate()
            raise
        except JournalBoundaryError:
            raise
        except Exception:
            self.journal.invalidate()
            raise JournalFault() from None

    def _append(self, event: dict[str, Any]) -> int:
        name = event["event"]
        operation_id = event["operation_id"]
        if name in ("rollout_started", "rollout_finished"):
            payload = {key: event[key] for key in SAMPLE_FIELDS}
            if name == "rollout_finished":
                payload["outcome"] = event["terminal"]
                if self._row_provenance:
                    used = event.get("used")
                    # Over the count or the byte budget is unknown, never
                    # truncated (ADR-0024 and its 2026-10-01 amendment). The
                    # rollout drops an oversized union as it runs; the checks
                    # here stay as the final guard.
                    if event.get("used_overflow") is True:
                        payload["provenance_overflow"] = True
                    elif used is not None and (
                        len(used) > PROVENANCE_MAX_USED
                        or len(canonical_json(list(used))) > PROVENANCE_MAX_BYTES
                    ):
                        payload["provenance_overflow"] = True
                    elif used is not None:
                        payload["provenance"] = list(used)
                    if self._components_digest is not None:
                        payload["components_digest"] = self._components_digest
            if self._retry_ordinal is not None:
                payload["retry_ordinal"] = self._retry_ordinal
        else:
            operation, _, suffix = name.rpartition("_")
            if operation not in EXTERNAL_OPERATIONS or suffix not in (
                "started",
                "finished",
                "failed",
            ):
                raise JournalFault()
            keys = (
                {"instance_id", "candidate_digest", "strategy"}
                if operation in ("target_materialize", "target_release")
                else set(SAMPLE_FIELDS) | {"instance_id"}
            )
            if operation in ("target_open", "target_send"):
                keys.add("request_id")
            if operation in ("target_send", "environment_next"):
                keys.add("turn")
            if suffix != "started":
                keys.add("latency_ms")
            payload = {key: event[key] for key in keys}
            owned = operation in ("target_materialize", "target_release")
            payload["retry_ordinal"] = 0 if owned else self._retry_ordinal or 0
            # Call-retry evidence, present only when retries are configured.
            if "call_attempt" in event:
                payload["call_attempt"] = event["call_attempt"]
            if suffix == "failed":
                payload["failure"] = _failure(operation, event.get("error_class"))
                if "error_code" in event:
                    payload["error_code"] = event["error_code"]
            if operation == "target_send" and suffix == "finished":
                if "target_latency_ms" in event:
                    payload["target_latency_ms"] = event["target_latency_ms"]
                usage = event.get("usage") or {}
                prompt, completion = usage.get("prompt"), usage.get("completion")
                payload["usage"] = {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "unknown": prompt is None or completion is None,
                }
        # The terminal a `close_execution` derives from `sample_payload` must
        # not carry the start-only `repair` and connection keys, so they are
        # added to the written payload alone.
        written = (
            {**payload, "repair": True} if name == "rollout_started" and self._repair else payload
        )
        claimed = None
        if name == "rollout_started" and self._connection is not None:
            claimed = self._connection.claim() if self._admit else None
            written = {
                **written,
                "connection_binding_digest": self._connection.binding_digest,
                "connection_admission": claimed,
            }
        try:
            result = self.journal.append(name, written, operation_id=operation_id)
        except (JournalAdmissionRefused, JournalRecoveryRequired):
            # Refused before the start was written: the admission is not spent.
            if claimed is not None:
                assert self._connection is not None
                self._connection._restore()
            raise
        if name == "rollout_started":
            self.rollout_operation_id = operation_id
            self.sample_payload = payload
            self.started = True
        elif name == "rollout_finished":
            self.terminal = True
        return result if type(result) is int else getattr(result, "sequence", 0)

    def close_execution(self, event: str, outcome: str) -> None:
        if self.started and not self.terminal:
            assert self.sample_payload is not None
            self.journal.append(
                event,
                {**self.sample_payload, "outcome": outcome},
                operation_id=self.rollout_operation_id,
            )
            self.terminal = True


class _BoundInstance:
    def __init__(self, instance: TargetInstance, expected: SemanticIdentity):
        self._instance = instance
        self._expected = expected
        self.instance_id = instance.instance_id
        # A transparent wrapper: the bound instance is the same instance, and
        # the description it carries is whatever the adapter captured at
        # materialization (PDR-0014). `getattr` because `description` is not a
        # member of `TargetInstance` at all -- it lives on the sibling
        # `DescribingTargetInstance` (rollout.py) -- so an adapter that
        # declares none still binds here.
        self.description = getattr(instance, "description", None)

    async def identity(self) -> TargetIdentity:
        result = await self._instance.identity()
        if result.semantic != self._expected:
            raise RolloutInfrastructureError("target identity changed", "identity_changed")
        return result

    async def open(self, case_public: Mapping[str, Any], seed: int | None, request_id: str) -> Any:
        return await self._instance.open(case_public, seed, request_id)

    async def send(self, session: Any, message: Any, request_id: str) -> Any:
        return await self._instance.send(session, message, request_id)

    async def close(self, session: Any) -> None:
        await self._instance.close(session)


async def durable_rollout(
    *,
    journal: NativeJournal,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    case: EvalCase,
    sample: RolloutSample,
    instance: TargetInstance,
    environment: Environment,
    scorer: Any,
    budget: RolloutBudget,
    expected_target: TargetIdentity | SemanticIdentity,
    retry_ordinal: int | None = None,
    repair: bool = False,
    components_digest: str | None = None,
    connection: ConnectionAdmission | None = None,
) -> Trajectory:
    """Verify frozen inputs, then persist a single execution lifecycle without replay.

    `repair` marks the try a repair try: one more try of a
    sample whose latest try was a fault row a suspension named.

    `components_digest` names the component map `instance` declared;
    a family-12 row records it, any other row ignores it.

    `connection` is the family-14 admission token: the start names its
    binding and, if it is the binding's first start, carries the admission.
    """
    if journal.identity.digest != identity.digest:
        raise IdentityError("run_identity_changed")
    identity.require_retry_ordinal(retry_ordinal, repair=repair)
    journal.identity.validate_corpus(corpus)
    identity.validate_call(
        case=case,
        sample=sample,
        target=expected_target,
        environment=environment.identity,
        scorer=scorer,
        budget=budget,
        corpus=journal.corpus,
    )
    if getattr(journal, "recovery_required", False):
        raise JournalRecoveryRequired()
    semantic = (
        expected_target.semantic if isinstance(expected_target, TargetIdentity) else expected_target
    )
    adapter = JournalRolloutAdapter(
        journal,
        retry_ordinal,
        repair=repair,
        row_provenance=identity.row_provenance,
        components_digest=components_digest,
        connection=connection,
    )
    try:
        return await rollout(
            case, sample, _BoundInstance(instance, semantic), environment, budget, adapter
        )
    except JournalFault:
        raise
    except JournalAdmissionRefused:
        adapter.close_execution("rollout_interrupted", "interrupted")
        raise
    except JournalBoundaryError:
        raise
    except asyncio.CancelledError:
        adapter.close_execution("rollout_interrupted", "interrupted")
        raise
    except RolloutEnvironmentError:
        adapter.close_execution("rollout_failed", "environment_failed")
        raise
    except RolloutTimeBudgetExceeded:
        adapter.close_execution("rollout_failed", "time_budget")
        raise
    except RolloutTargetUnstable:
        adapter.close_execution("rollout_failed", "target_failed")
        raise
    except RolloutInfrastructureError:
        adapter.close_execution("rollout_failed", "infrastructure_failed")
        raise
