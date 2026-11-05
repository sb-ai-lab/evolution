"""Pure recovery reducers over artifact-ledger records.

Nothing here touches the filesystem or the store; it answers only "what did the
ledger say happened to the last copy aimed at this destination", which is the one
fact the table needs beyond `ArtifactLifecycle`'s own indexes. Keeping it pure
keeps recovery decisions testable without a run root.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from evolution.tree.storage.envelope import Envelope

# A copy that ran out of space or hit an I/O error is retriable exactly like an
# interrupted one; only `artifact_unsafe` is a terminal verdict.
RETRIABLE_FAILURES = frozenset({"storage_capacity_exhausted", "io_failure"})

__all__ = ["RETRIABLE_FAILURES", "CopyOutcome", "RecoveryReport", "copy_outcomes"]


@dataclass(frozen=True)
class RecoveryReport:
    """Recovery actions and derived workspace cleanup from one resume."""

    actions: tuple[str, ...] = ()
    removed_workspaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class CopyOutcome:
    """The last `artifact_copy` operation aimed at one destination, and its end."""

    started: Envelope
    operation_id: str
    terminal: str | None
    code: str | None

    @property
    def retriable(self) -> bool:
        """Open, interrupted, or failed for a reason that need not recur."""
        if self.terminal is None or self.terminal == "operation_interrupted":
            return True
        return self.terminal == "artifact_copy_failed" and self.code in RETRIABLE_FAILURES

    @property
    def failed_unsafe(self) -> bool:
        """An unsafe copy failure is terminal; a finished copy is not a failure."""
        return self.terminal == "artifact_copy_failed" and self.code == "artifact_unsafe"


def copy_outcomes(records: Iterable[Envelope]) -> dict[str, CopyOutcome]:
    """Index copy operations by `"<kind>/<artifact_id>"`, the last one winning.

    Recovery always re-runs a copy as a *new* operation, so only the newest
    operation for a destination still describes what is on disk there.
    """
    destinations: dict[str, str] = {}
    outcomes: dict[str, CopyOutcome] = {}
    for record in records:
        operation_id = record.operation_id
        if operation_id is None:
            continue
        if record.event == "artifact_copy_started":
            payload = record.payload
            destination = f"{payload['kind']}/{payload['artifact_id']}"
            destinations[operation_id] = destination
            outcomes[destination] = CopyOutcome(record, operation_id, None, None)
            continue
        outcome = outcomes.get(destinations.get(operation_id, ""))
        if outcome is not None and outcome.operation_id == operation_id:
            outcomes[destinations[operation_id]] = CopyOutcome(
                outcome.started, operation_id, record.event, record.payload.get("code")
            )
    return outcomes
