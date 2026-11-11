"""The Candidate inspection's own S1 journal (G2 spec §4.5).

In component mode the campaign loads the Candidate once before its measurement
is bound, reads the component map the Target declares, and releases the
instance. The instance is recorded in a journal of its own under
`inspections/<attempt_id>-i<k>/`, so a crash before `candidate_inspected`
leaves an owner that S1's recovery releases, as `Measurement.resume` does for a
measurement: the next inspection of the same id does it first and then replaces
the journal with a fresh one, and the campaign's open releases every journal no `candidate_inspected` cites
(`release_leftover`, under the journal's own stored identity). The journal is never
a measurement: the campaign binds nothing for it, and only its
`components_declared` is ever read back (by the audit).

It is deliberately not under `measurements/`: admission refuses any child of
that directory the campaign journal does not declare
(`_Campaign._admit_siblings_read_only`), and an inspection is never declared
there.

Outcomes:

- the screened map (or `None` when the instance declared none) and its withheld count;
- `InspectionFailed` when the Target could not be inspected (a materialize or
  identity failure, an attestation refusal, or an instance an earlier
  interrupted inspection left that the Target will not release) -- the
  Attempt then falls back to all cases;
- an interruption (`asyncio.CancelledError`, `KeyboardInterrupt`,
  `SystemExit`) or a journal fault propagates unchanged; a crash leaves the
  instance owned in the journal, and the next call releases it first.

A release refused after a successful inspection is suppressed: the map is
already durable, and the instance stays owned in the journal, where the next
inspection of the same id would try to release it again.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Collection, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.artifacts.attestation import verify_attestation
from evolution.tree.execution.interaction import (
    EvalCase,
    MaterializationStrategy,
    RolloutInfrastructureError,
    RolloutTargetUnstable,
    TargetAdapter,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    Journal,
)
from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1
from evolution.tree.execution.journal_recovery import close_interrupted_operations
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle, publish_components
from evolution.tree.execution.rollout import (
    materialize_target,
    recover_target_instances,
    release_target,
)
from evolution.tree.execution.wire_v2 import new_instance_id
from evolution.tree.storage.append_log import Snapshot, _fault, inspect_log
from evolution.tree.storage.journal_errors import JournalBoundaryError, JournalRecoveryRequired

__all__ = [
    "INSPECTIONS_DIR",
    "InspectionFailed",
    "inspect_candidate",
    "inspection_born",
    "inspection_id",
    "inspection_path",
    "release_leftover",
    "stored_identity",
]

INSPECTIONS_DIR = "inspections"

#: What a Target failure raises during an inspection. `AttestationError` is a
#: `RolloutInfrastructureError`, so an attestation refusal is one of them.
_TARGET_FAILURES = (RolloutInfrastructureError, RolloutTargetUnstable)


class InspectionFailed(Exception):
    """The Target could not be inspected; the Attempt falls back to all cases.

    The cause is chained (`__cause__`) and its message is this one's.
    """


def inspection_id(attempt_id: str, inspection: int) -> str:
    """The journal's run id and directory name: `<attempt_id>-i<inspection>`."""

    return f"{attempt_id}-i{inspection}"


def inspection_path(run_root: Path, attempt_id: str, inspection: int) -> Path:
    return Path(run_root) / INSPECTIONS_DIR / inspection_id(attempt_id, inspection)


async def inspect_candidate(
    *,
    run_root: Path,
    campaign_run_id: str,
    attempt_id: str,
    inspection: int,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[ArtifactRef],
    candidate: ArtifactRef,
    strategy: MaterializationStrategy,
    call_attempts: int | None,
    admitted_versions: Collection[str] = (),
) -> tuple[Mapping[str, Any] | None, int]:
    """Materialize, attest, publish the screened map, and release `candidate` once.

    Return the published map and withheld count; no map is `(None, 0)`.

    `identity` must be a family-12 measurement identity (`row_provenance`):
    S1 admits `components_declared` only under it. `call_attempts` is passed
    to `materialize_target` and `release_target` as their `attempts`.
    `admitted_versions` is passed to `Journal.resume`, as `Measurement.resume`
    does, so an inspection interrupted under an older admitted package version
    is reopened (and its leftover instance released) rather than refused.
    """

    path = inspection_path(run_root, attempt_id, inspection)
    withheld = 0
    journal = await _open(
        path,
        inspection_id(attempt_id, inspection),
        identity,
        corpus,
        secrets,
        adapter,
        admitted_versions,
    )
    try:
        instance_id = new_instance_id(campaign_run_id)
        try:
            # See `Measurement._materialize`: a frozen `ArtifactRef` is not a
            # structural match for R1's settable `CandidateReference.digest`.
            instance = await materialize_target(
                adapter,  # type: ignore[type-var]
                candidate,
                strategy,
                instance_id,
                JournalRolloutAdapter(journal),
                attempts=call_attempts,
            )
        except _TARGET_FAILURES as exc:
            # R1 still counts a failed materialize as owned (a lost answer, an
            # exhausted transient): the Target may have created the instance.
            # Journal k is never reopened once the Attempt falls back, so the
            # release is attempted now; a refusal stays owned (known limit).
            fault = await _release(
                adapter, instance_id, candidate.digest, strategy, journal, call_attempts
            )
            if fault is not None:
                raise fault from exc
            raise InspectionFailed(str(exc)) from exc
        except JournalBoundaryError:
            # The Target may have created the instance and its outcome record
            # (`target_materialize_finished` or `_failed`) could not be
            # written. The adapter's answer is lost here, so the instance is
            # given back by the id this call minted, through `_release`'s
            # journal-fault fallback; the fault already travelling stays the
            # one raised. Interruptions are not caught here.
            await _release(adapter, instance_id, candidate.digest, strategy, journal, call_attempts)
            raise
        try:
            try:
                reported = await instance.identity()
                verify_attestation(reported, candidate.expected_attestation())
            except _TARGET_FAILURES as exc:
                raise InspectionFailed(str(exc)) from exc
            # `getattr`: `components` is not a `TargetInstance` member; the
            # HTTP adapter's handle carries the creation body's validated map.
            components = getattr(instance, "components", None)
            if components is not None:
                published = publish_components(components, secrets)
                # The journal is fresh (`_open` replaces a leftover), so the
                # map is declared exactly once.
                journal.append("components_declared", published.payload())
                components, withheld = published.components, published.withheld
        except InspectionFailed as exc:
            # As before the fallback: a journal fault while recording the
            # release outranks the Target failure the Attempt would fall back on.
            fault = await _release(
                adapter, instance_id, candidate.digest, strategy, journal, call_attempts
            )
            if fault is not None:
                raise fault from exc
            raise
        except BaseException:
            # Interruptions, journal faults and other errors still propagate
            # after the release attempt, unchanged: a journal fault the release
            # itself meets does not replace the one already travelling.
            await _release(adapter, instance_id, candidate.digest, strategy, journal, call_attempts)
            raise
        fault = await _release(
            adapter, instance_id, candidate.digest, strategy, journal, call_attempts
        )
        if fault is not None:
            raise fault
        return components, withheld
    finally:
        try:
            if withheld:
                print(
                    f"measurement {inspection_id(attempt_id, inspection)}: provenance withheld for "
                    f"0 sample(s) and {withheld} component(s) left out of "
                    "published maps because they contain a run secret",
                    file=sys.stderr,
                )
        finally:
            journal.close()


async def _release(
    adapter: TargetAdapter[ArtifactRef],
    instance_id: str,
    candidate_digest: str,
    strategy: MaterializationStrategy,
    journal: Journal,
    call_attempts: int | None,
) -> JournalBoundaryError | None:
    """Give the inspection's instance back; return a journal fault, never raise one.

    `Measurement.release_all`'s journal-fault fallback (codex round 1,
    `inspection-storage-fault-skips-release`): the journaled path is
    `release_target`, whose first act is to append `target_release_started`.
    A journal a boundary fault has poisoned refuses that append before the
    adapter is called, so an unhealthy journal releases through the adapter
    alone, and a fault raised during a journaled release falls through to the
    same call. The journal still owns the instance then, so a later reopen
    releases it again by id, as after a crash. The Target's refusal is
    swallowed either way (the instance stays owned in the journal); an
    interruption travels. The caller re-raises the returned fault unless
    another error is already propagating.
    """

    fault: JournalBoundaryError | None = None
    if journal.healthy:
        try:
            await release_target(
                adapter,
                instance_id,
                candidate_digest,
                strategy,
                JournalRolloutAdapter(journal),
                attempts=call_attempts,
            )
            return None
        except _TARGET_FAILURES:
            return None
        except JournalBoundaryError as exc:
            fault = exc
    try:
        await adapter.release(instance_id)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - the Target refused; the journal still owns it
        pass
    return fault


async def _open(
    path: Path,
    name: str,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[ArtifactRef],
    admitted_versions: Collection[str],
) -> Journal:
    """Create the journal, or reopen a born one and release what it still owns.

    The born/unborn test is `Measurement.classify`'s; the reopen closes S1's
    interrupted operations before it recovers owned Target instances. A
    directory that holds no record holds
    nothing durable and is replaced, as `Measurement.remove_unborn` does.

    A born journal was left by a crash before its `candidate_inspected`. The
    caller opens journal `k` only while no `candidate_inspected` cites it, so
    nothing durable refers to it: it is reopened under its own stored identity
    (an accepted I2 `continue` may have moved the derived identity since), its
    instances are released, and it is replaced by a fresh journal under
    `identity`. It is replaced under an unchanged identity too, so a map the
    aborted load declared never stands beside the retry's result: the audit
    reads every declaration of the cited journal as the inspected map (codex
    round 1, `inspection-recovery-retains-aborted-maps`). A leftover it cannot
    release, or cannot reopen, is `InspectionFailed`.

    Directory work raises `JournalFault`, as the journal's own file work does,
    so the campaign's `_boundary` classifies a full disk here too.
    """

    if inspection_born(path):
        same_identity = stored_identity(path).digest == identity.digest
        try:
            released = await release_leftover(
                path, name, corpus, secrets, adapter, admitted_versions
            )
        except IdentityError as exc:
            if same_identity:
                # As `Journal.resume` refuses it: a producing version the run
                # never admitted is the operator's to admit, not a fallback.
                raise
            raise InspectionFailed("inspection_instance_unreleased") from exc
        if not released:
            raise InspectionFailed("inspection_instance_unreleased")
    try:
        if path.is_dir():
            shutil.rmtree(path)
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise _fault(exc) from exc
    return Journal.create(path, name, identity, corpus, secrets=secrets)


def stored_identity(path: Path) -> RunIdentityV1:
    """The identity a born inspection journal's `run_created` holds."""

    first = _snapshot(path).records[0]
    return RunIdentityV1(first.payload["identity"])


async def release_leftover(
    path: Path,
    name: str,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[ArtifactRef],
    admitted_versions: Collection[str] = (),
) -> bool:
    """Release what a born inspection journal still owns, under its own identity.

    The journal is reopened as written, never re-derived from the session, so
    an identity the campaign has since moved past does not refuse it. Returns
    `True` when the journal owns nothing afterwards, `False` when the Target
    refused a release (the instance stays owned in the journal). Raises
    `IdentityError` when the journal cannot be reopened at all (a producing
    version the run never admitted, or a corpus it no longer binds).
    """

    try:
        journal = await _resume(
            path, name, stored_identity(path), corpus, secrets, adapter, admitted_versions
        )
    except InspectionFailed:
        return False
    journal.close()
    return True


def inspection_born(path: Path) -> bool:
    """`Measurement.classify(...) == "born"` for an inspection journal's directory."""

    if not path.is_dir() or not (path / EVENTS_FILE).exists():
        return False
    return bool(_snapshot(path).records)


def _snapshot(path: Path) -> Snapshot:
    return inspect_log(
        path,
        file_name=EVENTS_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=Lifecycle,
        envelope_family=S1_FAMILY,
    )


async def _resume(
    path: Path,
    name: str,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[ArtifactRef],
    admitted_versions: Collection[str] = (),
) -> Journal:
    journal = Journal.resume(
        path, name, identity, corpus, secrets=secrets, admitted_versions=admitted_versions
    )
    try:
        records = tuple(
            {"event": record.event, **record.payload}
            for record in journal.snapshot().records
            if record.event.startswith(("target_materialize_", "target_release_"))
        )
        refused: RolloutInfrastructureError | None = None
        close_interrupted_operations(journal)
        # Only `RolloutInfrastructureError`, as in `Measurement.resume`:
        # recovery releases with `attempts=None` (one call, no I1 resend), and
        # `release_target` wraps every adapter error but an interruption into
        # `RolloutInfrastructureError`; `RolloutTargetUnstable` is raised only
        # by session calls (`_target_call_failure`), never by a release.
        try:
            await recover_target_instances(adapter, records, JournalRolloutAdapter(journal))
        except RolloutInfrastructureError as exc:
            refused = exc
        if journal.recovery_required:
            with suppress(JournalRecoveryRequired):
                journal.finish_recovery()
        if journal.recovery_required:
            # S1 will not leave recovery while it owns an instance the Target
            # refused to release, so no new instance can be recorded here.
            raise InspectionFailed("inspection_instance_unreleased") from refused
    except BaseException:
        journal.close()
        raise
    return journal
