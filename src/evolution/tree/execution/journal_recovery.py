"""Native journal reduction: preserve known outcomes, never replay unknown calls."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evolution.tree.execution.interaction import MaterializationStrategy
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.execution.sample_outcomes import (
    SampleDisposition,
    SampleOutcome,
    disposition_for,
)
from evolution.tree.storage.envelope import Envelope, SchemaError


@dataclass(frozen=True)
class ExecutionResumeDecision:
    """One scheduled sample's reduced state.

    `imported` marks a pair carried in by `sample_imported`: it holds the
    carried outcome, `scoring_state` `finished` and a `score` evaluation, and it
    has no execution of its own to resume.
    """

    case_id: str
    sample_id: str
    candidate_digest: str
    outcome: SampleOutcome | None
    scoring_state: str
    evaluation: SampleDisposition | None
    imported: bool = False

    @property
    def execution(self) -> SampleDisposition | None:
        return disposition_for(self.outcome) if self.outcome is not None else None


@dataclass(frozen=True)
class RecoveryAction:
    event: str
    operation_id: str
    _payload: bytes

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload)


@dataclass(frozen=True)
class OwnedCleanup:
    instance_id: str
    candidate_digest: str
    strategy: MaterializationStrategy


@dataclass(frozen=True)
class RecoveryPlan:
    executions: tuple[ExecutionResumeDecision, ...]
    actions: tuple[RecoveryAction, ...]
    cleanup: tuple[OwnedCleanup, ...]


def recovery_plan(records: Sequence[Envelope]) -> RecoveryPlan:
    """Describe actions only; a projection cannot fabricate their completion."""
    state = Lifecycle()
    for record in records:
        if not isinstance(record, Envelope):
            raise SchemaError("native envelopes required")
        state.accept(record)
    if state.created is None:
        raise SchemaError("incomplete journal creation")
    decisions = []
    identity = state.created.payload["identity"]
    for sample in identity["schedule"]:
        key = sample["case_id"], sample["sample_id"]
        carried = state.imported.get(key)
        if carried is not None:
            # The lifecycle admits no try on a carried pair: it is scored.
            outcome = carried.payload["outcome"]
            decisions.append(
                ExecutionResumeDecision(
                    *key,
                    identity["candidate_digest"],
                    outcome,
                    "finished",
                    disposition_for(outcome),
                    imported=True,
                )
            )
            continue
        execution = state.executions.get(key)
        score = state.scoring.get(key)
        outcome = execution.payload.get("outcome") if execution else None
        disposition = disposition_for(outcome) if outcome else None
        final = disposition if disposition and disposition.kind != "score" else None
        scoring_state = "not_started"
        if score:
            scoring_state = score.event.removeprefix("scoring_")
            if scoring_state == "finished":
                final = disposition
            elif scoring_state == "failed":
                final = disposition_for("scorer_failed")
            elif scoring_state == "interrupted":
                final = disposition_for("interrupted")
        decisions.append(
            ExecutionResumeDecision(
                *key, identity["candidate_digest"], outcome, scoring_state, final
            )
        )
    actions = []
    # Child external lifecycles close before their enclosing execution.
    pending = sorted(
        state.pending.values(),
        key=lambda e: (e.event.startswith(("rollout_", "scoring_")), e.to_payload()["sequence"]),
    )
    for start in pending:
        family = start.event.removesuffix("_started")
        if family in ("rollout", "scoring"):
            payload: dict[str, Any] = {
                key: start.payload[key]
                for key in ("case_id", "sample_id", "candidate_digest", "retry_ordinal")
                if key in start.payload
            }
            event = family + "_interrupted"
            if family == "rollout":
                payload["outcome"] = "interrupted"
            else:
                payload["score_request_id"] = start.payload["score_request_id"]
        else:
            event = "operation_interrupted"
            payload = {"operation": family}
        assert start.operation_id is not None
        actions.append(RecoveryAction(event, start.operation_id, json.dumps(payload).encode()))
    cleanup = tuple(
        OwnedCleanup(instance, start.payload["candidate_digest"], start.payload["strategy"])
        for instance, start in sorted(state.instances.items())
    )
    return RecoveryPlan(tuple(decisions), tuple(actions), cleanup)


def close_interrupted_operations(journal: Any) -> RecoveryPlan:
    """Close prior-process calls before recovery starts new cleanup calls."""
    plan = recovery_plan(journal.snapshot().records)
    if plan.actions and not journal.recovery_required:
        raise ValueError("reopen before recovering in-flight work")
    for action in plan.actions:
        journal.append(action.event, action.payload, action.operation_id)
    return recovery_plan(journal.snapshot().records)


async def recover_journal(journal: Any, adapter: Any = None) -> RecoveryPlan:
    """Close interrupted lifecycles under ownership, then release owned instances.

    A live owner's in-flight work cannot be declared crashed. Close the executor
    and reopen its journal before recovering outstanding calls.
    """
    from evolution.tree.execution.journal_rollout import JournalRolloutAdapter
    from evolution.tree.execution.rollout import release_target

    plan = close_interrupted_operations(journal)
    if plan.cleanup and adapter is None:
        raise ValueError("owned instance recovery requires its adapter")
    for owned in plan.cleanup:
        await release_target(
            adapter,
            owned.instance_id,
            owned.candidate_digest,
            owned.strategy,
            JournalRolloutAdapter(journal),
        )
    journal.finish_recovery()
    return recovery_plan(journal.snapshot().records)
