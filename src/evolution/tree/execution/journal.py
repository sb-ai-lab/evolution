"""Owned, bounded native journal. Inspection never acquires a lease or mutates files.

Descriptors, leases, bounded appends and hashing live in `AppendLog`; this module
keeps the S1 run vocabulary: identity, recovery and dispatch-stop admission.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_identity import RunIdentityV1, resolve_package_version
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
from evolution.tree.storage.append_log import AppendLog, Snapshot, _fault, _read, inspect_log
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalFault,
    JournalRecoveryRequired,
)

MAX_RECORD_BYTES = 1024 * 1024
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
EVENTS_FILE = "events.jsonl"
LEASE_FILE = ".owner.lock"
PARTIAL_FILE = f"{EVENTS_FILE}.partial"

__all__ = [
    "MAX_JOURNAL_BYTES",
    "MAX_RECORD_BYTES",
    "PARTIAL_FILE",
    "Journal",
    "Snapshot",
    "inspect_journal",
]


def inspect_journal(
    path: str | Path,
    *,
    max_record_bytes: int = MAX_RECORD_BYTES,
    max_journal_bytes: int = MAX_JOURNAL_BYTES,
) -> Snapshot:
    return inspect_log(
        path,
        file_name=EVENTS_FILE,
        max_record_bytes=max_record_bytes,
        max_log_bytes=max_journal_bytes,
        state_factory=Lifecycle,
        envelope_family=S1_FAMILY,
        read=_read,  # resolved per call: durability tests substitute an unstable reader
    )


class Journal:
    """One process-bound lease and one mutex for each run-lifetime writer."""

    _identity: RunIdentityV1
    _corpus: tuple[EvalCase, ...]
    _run_id: str

    def __init__(self, log: AppendLog) -> None:
        self._log = log
        self._recovery_required = False

    @classmethod
    def create(
        cls,
        path: str | Path,
        run_id: str,
        identity: RunIdentityV1,
        corpus: Sequence[EvalCase],
        *,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_record_bytes: int = MAX_RECORD_BYTES,
        max_journal_bytes: int = MAX_JOURNAL_BYTES,
        imported: Sequence[Mapping[str, Any]] = (),
    ) -> Journal:
        version = resolve_package_version(version_resolver)
        identity.validate_corpus(corpus)
        run_created_payload = dict(
            identity=identity.to_payload(),
            identity_digest=identity.digest,
            producing_version=version,
        )
        # Schema/secret validation precedes even directory creation.
        Envelope.from_payload(
            dict(
                schema_version=1,
                run_id=run_id,
                sequence=1,
                timestamp="2026-01-01T00:00:00Z",
                event="run_created",
                operation_id=None,
                payload=run_created_payload,
            ),
            secrets=secrets,
            family=S1_FAMILY,
        )
        initial: bytes | None = None
        if imported:
            initial = cls._birth_bytes(
                run_id,
                run_created_payload,
                imported,
                secrets=secrets,
                max_record_bytes=max_record_bytes,
                max_journal_bytes=max_journal_bytes,
            )
        journal = cls._open(
            Path(path),
            create=True,
            max_record_bytes=max_record_bytes,
            max_journal_bytes=max_journal_bytes,
            secrets=secrets,
            initial=initial,
        )
        try:
            journal._run_id = run_id
            journal._identity = identity
            journal._corpus = tuple(corpus)
            if initial is None:
                journal.append("run_created", run_created_payload)
                os.fsync(journal._log.directory_fd)
            return journal
        except OSError as exc:
            journal.close()
            raise _fault(exc) from None
        except BaseException:
            journal.close()
            raise

    @staticmethod
    def _birth_bytes(
        run_id: str,
        run_created_payload: dict[str, Any],
        imported: Sequence[Mapping[str, Any]],
        *,
        secrets: Sequence[str],
        max_record_bytes: int,
        max_journal_bytes: int,
    ) -> bytes:
        """The whole content of a journal born with carried rows, pre-validated.

        `run_created` plus every `sample_imported` row are admitted to a fresh
        `Lifecycle` before a byte is written, so a payload the schema or the
        lifecycle refuses never reaches the directory-creation step. All records
        share one timestamp: the moment this journal was born.
        """

        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        records = [
            Envelope.from_payload(
                dict(
                    schema_version=1,
                    run_id=run_id,
                    sequence=1,
                    timestamp=timestamp,
                    event="run_created",
                    operation_id=None,
                    payload=run_created_payload,
                ),
                secrets=secrets,
                family=S1_FAMILY,
            )
        ]
        for sequence, row in enumerate(imported, start=2):
            records.append(
                Envelope.from_payload(
                    dict(
                        schema_version=1,
                        run_id=run_id,
                        sequence=sequence,
                        timestamp=timestamp,
                        event="sample_imported",
                        operation_id=None,
                        payload=dict(row),
                    ),
                    secrets=secrets,
                    family=S1_FAMILY,
                )
            )
        lifecycle = Lifecycle()
        for record in records:
            lifecycle.prepare(record).commit()
        encoded = [record.encode() for record in records]
        for chunk in encoded:
            if len(chunk) > max_record_bytes:
                raise JournalFault("journal_limit_exceeded")
        initial = b"".join(encoded)
        if len(initial) > max_journal_bytes:
            raise JournalFault("journal_limit_exceeded")
        return initial

    @classmethod
    def resume(
        cls,
        path: str | Path,
        run_id: str,
        identity: RunIdentityV1,
        corpus: Sequence[EvalCase],
        *,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_record_bytes: int = MAX_RECORD_BYTES,
        max_journal_bytes: int = MAX_JOURNAL_BYTES,
        admitted_versions: Collection[str] = (),
    ) -> Journal:
        resolve_package_version(version_resolver)
        identity.validate_corpus(corpus)
        journal = cls._open(
            Path(path),
            create=False,
            max_record_bytes=max_record_bytes,
            max_journal_bytes=max_journal_bytes,
            secrets=secrets,
        )
        try:
            # Identity is proved before any tail repair: a refused prefix stays untouched.
            def validate_prefix(snapshot: Snapshot) -> None:
                first = snapshot.records[0]
                stored = RunIdentityV1(first.payload["identity"])
                stored.validate_resume(
                    identity,
                    stored_digest=first.payload["identity_digest"],
                    stored_run_id=first.to_payload()["run_id"],
                    requested_run_id=run_id,
                    producing_version=first.payload["producing_version"],
                    cases=corpus,
                    version_resolver=version_resolver,
                    admitted_versions=admitted_versions,
                )

            lifecycle = cast(Lifecycle, journal._log.scan(validate_prefix=validate_prefix)[1])
            journal._run_id = run_id
            journal._identity = identity
            journal._corpus = tuple(corpus)
            journal._recovery_required = bool(lifecycle.pending or lifecycle.instances)
            return journal
        except OSError as exc:
            journal.close()
            raise _fault(exc) from None
        except BaseException:
            journal.close()
            raise

    @classmethod
    def _open(
        cls,
        path: Path,
        *,
        create: bool,
        max_record_bytes: int,
        max_journal_bytes: int,
        secrets: Sequence[str],
        initial: bytes | None = None,
    ) -> Journal:
        return cls(
            AppendLog.open(
                path,
                file_name=EVENTS_FILE,
                lock_name=LEASE_FILE,
                create=create,
                state_factory=Lifecycle,
                max_record_bytes=max_record_bytes,
                max_log_bytes=max_journal_bytes,
                secrets=secrets,
                envelope_family=S1_FAMILY,
                initial=initial,
            )
        )

    @property
    def _lifecycle(self) -> Lifecycle:
        return cast(Lifecycle, self._log.state)

    @property
    def _fd(self) -> int:
        return self._log.fd

    @property
    def _mutex(self) -> threading.RLock:
        return self._log.mutex

    def append(self, event: str, payload: dict[str, Any], operation_id: str | None = None) -> int:
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            # Persisted stop refuses new work without poisoning reserved closures.
            reserved_release = (
                event == "target_release_started"
                and payload.get("instance_id") in lifecycle.cleanup_credits
            )
            recovery_release = (
                event == "target_release_started"
                and payload.get("instance_id") in lifecycle.instances
            )
            # A component declaration admits new work as a start does: a
            # stopped or recovering journal refuses it before the lifecycle
            # would, so the refusal never poisons the writer.
            admits_work = event.endswith("_started") or event == "components_declared"
            if self.recovery_required and admits_work and not recovery_release:
                raise JournalRecoveryRequired()
            if lifecycle.dispatch_stopped and admits_work and not reserved_release:
                raise JournalAdmissionRefused()

            def stop_dispatch() -> None:
                if not lifecycle.dispatch_stopped:
                    self.append("journal_dispatch_stopped", dict(code="journal_limit_exceeded"))

            return self._log.append(
                self._run_id, event, payload, operation_id, on_limit=stop_dispatch
            )

    @property
    def identity(self) -> RunIdentityV1:
        return self._identity

    @property
    def corpus(self) -> tuple[EvalCase, ...]:
        return self._corpus

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def secrets(self) -> tuple[str, ...]:
        """The writer's secret set, so a callee summary is pre-validated against it."""
        return self._log.secrets

    def finish_recovery(self) -> None:
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            if lifecycle.pending or lifecycle.instances:
                raise JournalRecoveryRequired()
            self._recovery_required = False

    def invalidate(self) -> None:
        """Poison this owner after a boundary translation fault, without writing."""
        self._log.invalidate()

    @property
    def healthy(self) -> bool:
        """Can this writer still append? False once a boundary fault poisoned it.

        A read-only accessor for the flag `AppendLog` already exposes; an owner
        that must still act on an instance the journal can no longer record —
        C1's terminal barrier — asks before it tries.
        """
        return self._log.healthy

    def sample_records(
        self, case_id: str, sample_id: str
    ) -> tuple[Envelope | None, Envelope | None]:
        """Read immutable execution/scoring records without copying the history."""
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            key = (case_id, sample_id)
            return lifecycle.executions.get(key), lifecycle.scoring.get(key)

    def imported_record(self, case_id: str, sample_id: str) -> Envelope | None:
        """The `sample_imported` record carried for this pair, or `None`."""
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            return lifecycle.imported.get((case_id, sample_id))

    def declared_components(self) -> Mapping[str, Envelope]:
        """Each `components_declared` record by its map's digest; a copy."""
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            return MappingProxyType(dict(lifecycle.declared_components))

    def sample_tries(
        self, case_id: str, sample_id: str
    ) -> tuple[tuple[Envelope, Envelope | None], ...]:
        """Every try of one sample in order, latest last; empty when never started."""
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            key = (case_id, sample_id)
            if key not in lifecycle.executions:
                return ()
            latest = (lifecycle.executions[key], lifecycle.scoring.get(key))
            return (*lifecycle.tries.get(key, ()), latest)

    def sample_repairs(self, case_id: str, sample_id: str) -> int:
        """How many repair tries one sample started."""
        with self._log.guard() as state:
            lifecycle = cast(Lifecycle, state)
            return lifecycle.repairs.get((case_id, sample_id), 0)

    def snapshot(self) -> Snapshot:
        return self._log.snapshot()

    def close(self) -> None:
        self._log.close()

    def __enter__(self) -> Journal:
        self._log.check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
