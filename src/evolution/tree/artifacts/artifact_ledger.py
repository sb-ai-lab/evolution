"""Owned, bounded S2 artifact ledger. Inspection never acquires a lease or writes.

Descriptors, leases, bounded appends and hashing live in `AppendLog`; this module
keeps the S2 store vocabulary: identity proof on resume, the run-root layout guard
and the permanent stop the ledger bound produces.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError, ArtifactStoreStopped
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_schema import (
    ARTIFACT_FAMILY,
    RESERVED_TERMINALS,
    ArtifactLifecycle,
    ArtifactStoreIdentityV1,
)
from evolution.tree.execution.journal_identity import IdentityError, resolve_package_version
from evolution.tree.storage.append_log import AppendLog, Snapshot, _fault, inspect_log
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalAdmissionRefused, JournalFault

MAX_LEDGER_RECORD_BYTES = 64 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
LEDGER_FILE = "artifacts.jsonl"
LEDGER_LOCK = ".artifacts.lock"
ARTIFACTS_DIR = "artifacts"
# Run-root entries of the pre-v2 snapshot store: their presence proves a foreign run.
FOREIGN_LAYOUT = ("versions", "rejected", "candidates")

__all__ = [
    "LEDGER_FILE",
    "LEDGER_LOCK",
    "MAX_LEDGER_BYTES",
    "MAX_LEDGER_RECORD_BYTES",
    "ArtifactLedger",
    "inspect_ledger",
]


def _record_cap(max_ledger_bytes: int) -> int:
    """A per-record cap the total bound can actually afford.

    Reserved terminals are counted at the per-record cap, so a bound smaller than a
    few caps would refuse its own `store_created`. Below 256 KiB the cap follows the
    bound and the store simply holds fewer concurrent operations.
    """
    return min(MAX_LEDGER_RECORD_BYTES, max(1, max_ledger_bytes // 4))


def _guard_layout(run_root: Path, *, create: bool) -> None:
    """S2 owns only its own names in the run root; an old layout is not a v2 run."""
    for name in FOREIGN_LAYOUT:
        if os.path.lexists(run_root / name):
            raise JournalFault("journal_unavailable")
    if create:
        for name in (LEDGER_FILE, LEDGER_LOCK, ARTIFACTS_DIR):
            if os.path.lexists(run_root / name):
                raise JournalFault("journal_unavailable")


def inspect_ledger(
    path: str | Path,
    *,
    max_record_bytes: int = MAX_LEDGER_RECORD_BYTES,
    max_ledger_bytes: int = MAX_LEDGER_BYTES,
) -> Snapshot:
    return inspect_log(
        path,
        file_name=LEDGER_FILE,
        max_record_bytes=max_record_bytes,
        max_log_bytes=max_ledger_bytes,
        state_factory=ArtifactLifecycle,
        envelope_family=ARTIFACT_FAMILY,
    )


class ArtifactLedger:
    """One process-bound lease and one mutex for each store-lifetime writer."""

    def __init__(self, log: AppendLog, run_id: str) -> None:
        self._log = log
        self._run_id = run_id

    @classmethod
    def create(
        cls,
        run_root: str | Path,
        identity: ArtifactStoreIdentityV1,
        *,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
    ) -> ArtifactLedger:
        version = _package_version(version_resolver)
        run_id = uuid4().hex
        payload = dict(
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
                event="store_created",
                operation_id=None,
                payload=payload,
            ),
            secrets=secrets,
            family=ARTIFACT_FAMILY,
        )
        root = Path(run_root)
        _guard_layout(root, create=True)
        ledger = cls._open(root, create=True, secrets=secrets, max_ledger_bytes=max_ledger_bytes)
        ledger._run_id = run_id
        try:
            ledger.append("store_created", payload)
            os.fsync(ledger._log.directory_fd)
            return ledger
        except OSError as exc:
            ledger.close()
            raise _fault(exc) from None
        except BaseException:
            ledger.close()
            raise

    @staticmethod
    def latest_store_record(snapshot: Snapshot) -> Envelope:
        """The record a resume proves its identity and version against.

        `store_created` until an authorized rebase re-stamps the ledger; the
        last `store_rebased` afterwards (PDR-0010).
        """
        latest = snapshot.records[0]
        for record in snapshot.records:
            if record.event == "store_rebased":
                latest = record
        return latest

    def rebase(
        self,
        identity: ArtifactStoreIdentityV1,
        *,
        campaign_rebase: str,
        authorization_id: str,
        producing_version: str,
    ) -> int:
        """Re-stamp this ledger for one declared campaign rebase, under its authorization.

        `campaign_rebase` is the `identity_digest` of the `identity_rebased`
        record this completes and `authorization_id` the decision that
        admitted it, so an audit can pair the sibling's stamp with the campaign
        record that caused it.

        `producing_version` is that campaign record's own version, not the
        running package's: a resume that finds a rebase declared by `0.3.0a12`
        and never completed writes the ledger half the crashed invocation owed,
        and it must be stamped `0.3.0a12` however late it arrives. The caller
        reads it from the record it is completing (ADR-0015).
        """
        return self.append(
            "store_rebased",
            dict(
                identity=identity.to_payload(),
                identity_digest=identity.digest,
                producing_version=producing_version,
                campaign_rebase=campaign_rebase,
                authorization_id=authorization_id,
            ),
        )

    @classmethod
    def resume(
        cls,
        run_root: str | Path,
        policy: ArtifactPolicyV1,
        *,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
        admitted_versions: Collection[str] = (),
    ) -> ArtifactLedger:
        root = Path(run_root)
        _guard_layout(root, create=False)
        ledger = cls._open(root, create=False, secrets=secrets, max_ledger_bytes=max_ledger_bytes)
        try:
            # Identity is proved before any tail repair: a refused prefix stays untouched.
            def validate_prefix(snapshot: Snapshot) -> None:
                first = snapshot.records[0]
                if first.event != "store_created":
                    raise JournalFault("journal_unavailable")
                # An authorized rebase moves the stamp: the latest store record
                # decides, and `admitted_versions` names the older producing
                # versions the campaign accepted for this run (ADR-0015).
                p = cls.latest_store_record(snapshot).payload
                current = _package_version(version_resolver)
                if p["producing_version"] != current and (
                    p["producing_version"] not in admitted_versions
                ):
                    raise JournalFault("journal_unavailable")
                try:
                    stored = ArtifactStoreIdentityV1.from_payload(p["identity"])
                except ValueError:
                    raise JournalFault("journal_unavailable") from None
                if stored.digest != p["identity_digest"]:
                    raise JournalFault("journal_unavailable")
                if stored.policy != policy:
                    raise ArtifactCorruptionError("policy_mismatch")

            snapshot = ledger._log.scan(validate_prefix=validate_prefix)[0]
            ledger._run_id = str(snapshot.records[0].to_payload()["run_id"])
            return ledger
        except OSError as exc:
            ledger.close()
            raise _fault(exc) from None
        except BaseException:
            ledger.close()
            raise

    @classmethod
    def inspect(
        cls,
        run_root: str | Path,
        *,
        max_record_bytes: int = MAX_LEDGER_RECORD_BYTES,
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
    ) -> Snapshot:
        return inspect_ledger(
            run_root, max_record_bytes=max_record_bytes, max_ledger_bytes=max_ledger_bytes
        )

    @classmethod
    def _open(
        cls, run_root: Path, *, create: bool, secrets: Sequence[str], max_ledger_bytes: int
    ) -> ArtifactLedger:
        return cls(
            AppendLog.open(
                run_root,
                file_name=LEDGER_FILE,
                lock_name=LEDGER_LOCK,
                create=create,
                state_factory=ArtifactLifecycle,
                max_record_bytes=_record_cap(max_ledger_bytes),
                max_log_bytes=max_ledger_bytes,
                secrets=secrets,
                envelope_family=ARTIFACT_FAMILY,
                # The run root also holds measurement journals and the campaign file.
                require_empty=False,
            ),
            "",
        )

    def append(self, event: str, payload: dict[str, Any], operation_id: str | None = None) -> int:
        with self._log.guard() as state:
            life = cast(ArtifactLifecycle, state)
            # A stopped store admits only the terminals it reserved; refusing the
            # rest here keeps a caller's mistake from poisoning a healthy writer.
            reserved = event in RESERVED_TERMINALS and operation_id in life.pending
            if life.stopped is not None and not reserved:
                raise ArtifactStoreStopped(life.stopped)

            def stop() -> None:
                # The bound reserves this record; open operations keep their terminals.
                if life.stopped is None:
                    self.append("store_stopped", dict(code="ledger_limit_exceeded"))

            try:
                return self._log.append(self._run_id, event, payload, operation_id, on_limit=stop)
            except JournalAdmissionRefused:
                raise ArtifactStoreStopped("ledger_limit_exceeded") from None

    @property
    def state(self) -> ArtifactLifecycle:
        return cast(ArtifactLifecycle, self._log.state)

    @property
    def run_id(self) -> str:
        return self._run_id

    def snapshot(self) -> Snapshot:
        return self._log.snapshot()

    def invalidate(self) -> None:
        """Poison this owner after a boundary translation fault, without writing."""
        self._log.invalidate()

    def close(self) -> None:
        self._log.close()

    def __enter__(self) -> ArtifactLedger:
        self._log.check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _package_version(version_resolver: Callable[[str], str] | None) -> str:
    try:
        return resolve_package_version(version_resolver)
    except IdentityError:
        raise JournalFault("journal_unavailable") from None
