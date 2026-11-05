"""Bounded owner journal for a standalone evaluation and its S1 measurement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evolution.tree.execution.journal_identity import RunIdentityV1
from evolution.tree.storage.append_log import AppendLog, Snapshot, inspect_log
from evolution.tree.storage.envelope import Envelope, EnvelopeFamily, LifecycleTransition

EVALUATION_FILE = "evaluation.jsonl"
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
_KEYS = {
    "evaluation_created": {"identity", "identity_digest"},
    "measurement_bound": {"identity", "identity_digest"},
    "instance_attested": {"instance_id", "semantic", "instance", "attested"},
    "evaluation_suspended": {"reason"},
    "evaluation_completed": {"measurement_prefix"},
}


def _validate(event: str, payload: dict[str, Any]) -> None:
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    if event not in _KEYS or set(payload) != _KEYS[event]:
        raise ValueError("invalid_evaluation_event")
    if event in ("evaluation_created", "measurement_bound"):
        if (
            hashlib.sha256(canonical_json(payload["identity"])).hexdigest()
            != payload["identity_digest"]
        ):
            raise ValueError("invalid_evaluation_identity")
        if event == "measurement_bound":
            RunIdentityV1(payload["identity"])
    elif event == "instance_attested":
        from evolution.tree.execution.interaction import (
            InstanceProvenance,
            SemanticIdentity,
            TargetIdentity,
        )

        TargetIdentity(
            SemanticIdentity(**payload["semantic"]),
            InstanceProvenance(**payload["instance"]),
            frozenset(payload["attested"]),
        )
        if not isinstance(payload["instance_id"], str) or not payload["instance_id"]:
            raise ValueError("invalid_evaluation_attestation")
    elif event == "evaluation_suspended":
        if payload["reason"] not in ("incomplete", "cancelled", "infrastructure"):
            raise ValueError("invalid_evaluation_reason")
    elif (
        not isinstance(payload["measurement_prefix"], str)
        or len(payload["measurement_prefix"]) != 64
    ):
        raise ValueError("invalid_evaluation_prefix")


EVALUATION_FAMILY = EnvelopeFamily(_validate, frozenset(_KEYS), lambda event: False)


class EvaluationState:
    def __init__(self) -> None:
        self.sequence = 0
        self.run_id: str | None = None
        self.bound = False
        self.finished = False

    @property
    def reserved_records(self) -> int:
        return 0

    def prepare(self, record: Envelope) -> LifecycleTransition:
        if record.to_payload()["sequence"] != self.sequence + 1 or self.finished:
            raise ValueError("invalid_evaluation_order")
        if self.sequence == 0:
            if record.event != "evaluation_created":
                raise ValueError("evaluation_not_created")
        elif record.to_payload()["run_id"] != self.run_id or record.event == "evaluation_created":
            raise ValueError("invalid_evaluation_order")
        if record.event == "measurement_bound" and self.bound:
            raise ValueError("duplicate_evaluation_binding")
        if record.event in ("evaluation_completed", "instance_attested") and not self.bound:
            raise ValueError("evaluation_not_bound")

        def apply() -> None:
            self.sequence = record.to_payload()["sequence"]
            self.run_id = record.to_payload()["run_id"]
            self.bound |= record.event == "measurement_bound"
            self.finished = record.event == "evaluation_completed"

        return LifecycleTransition(0, apply)

    def accept(self, record: Envelope) -> None:
        self.prepare(record).commit()


def open_evaluation_journal(root: Path, secrets: tuple[str, ...], initial: Envelope) -> AppendLog:
    _recover_unborn(root)
    created = not (root / "evaluation.lock").exists()
    log = AppendLog.open(
        root,
        file_name=EVALUATION_FILE,
        lock_name="evaluation.lock",
        create=created,
        state_factory=EvaluationState,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_LOG_BYTES,
        secrets=secrets,
        envelope_family=EVALUATION_FAMILY,
        initial=initial.encode() if created else None,
    )
    if not created:
        from evolution.tree.execution.output_admission import EvolveRefused

        def validate(snapshot: Snapshot) -> None:
            if snapshot.records[0].payload != initial.payload:
                raise EvolveRefused("run_identity_changed")

        try:
            log.scan(validate_prefix=validate)
        except BaseException:
            log.close()
            raise
    return log


def inspect_evaluation_journal(root: Path) -> Snapshot:
    return inspect_log(
        root,
        file_name=EVALUATION_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_LOG_BYTES,
        state_factory=EvaluationState,
        envelope_family=EVALUATION_FAMILY,
    )


def _recover_unborn(root: Path) -> None:
    """Reclaim only an interrupted atomic birth, while holding its original lease."""
    import fcntl
    import os

    from evolution.tree.storage.journal_errors import JournalFault

    if (root / EVALUATION_FILE).exists() or not (root / "evaluation.lock").exists():
        return
    fd = os.open(root / "evaluation.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JournalFault("journal_busy") from None
        if (root / EVALUATION_FILE).exists():
            return
        if set(path.name for path in root.iterdir()) - {
            "evaluation.lock",
            EVALUATION_FILE + ".partial",
        }:
            raise JournalFault("journal_unavailable")
        (root / (EVALUATION_FILE + ".partial")).unlink(missing_ok=True)
        (root / "evaluation.lock").unlink()
    finally:
        os.close(fd)
