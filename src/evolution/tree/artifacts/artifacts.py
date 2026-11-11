"""The S2 artifact store: policy, ledger and copy engine composed into a lifecycle.

The store owns three durable facts and nothing else: what the ledger says, what is
frozen under `artifacts/champions` and `artifacts/candidates`, and which unpublished
tree is still being worked on. A published copy exists when and only when its
`artifact_copy_finished` record is durable; every failure path here either
reaches that record or leaves nothing the ledger references.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from evolution.lineage.state_io import fsync_directory
from evolution.tree.artifacts.artifact_copy import (
    check_destination_parent,
    map_os_error,
    publish,
    remove_tree,
    restore_writable,
    stage_copy,
)
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactStoreStopped,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_ledger import ARTIFACTS_DIR, MAX_LEDGER_BYTES, ArtifactLedger
from evolution.tree.artifacts.artifact_payloads import (
    AuxiliaryDeclaration,
    destination_present,
    finish_directory,
    publish_directory,
    verify_directory,
)
from evolution.tree.artifacts.artifact_policy import (
    ArtifactPolicyV1,
    TreeScan,
    changes,
    entry_set,
    scan_tree,
    tree_inodes,
)
from evolution.tree.artifacts.artifact_projections import (
    AuxiliaryPublication,
    LineageProjection,
    corruption_code,
    project_auxiliary,
    project_lineage,
)
from evolution.tree.artifacts.artifact_recovery import CopyOutcome, RecoveryReport, copy_outcomes
from evolution.tree.artifacts.artifact_schema import (
    ARTIFACT_FAMILY,
    STARTS,
    ArtifactLifecycle,
    ArtifactStoreIdentityV1,
)
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.interaction import CandidateView
from evolution.tree.storage.envelope import Envelope, is_identifier
from evolution.tree.storage.inode_floor import InodeNeed
from evolution.tree.storage.journal_errors import JournalFault

CHAMPIONS = "champions"
CANDIDATES = "candidates"
WORKSPACES = ".workspaces"
STAGING = ".staging"
_SUBDIRECTORIES = (CHAMPIONS, CANDIDATES, WORKSPACES, STAGING)
# A workspace whose seal did not publish is abandonable too: `sealing` is what a
# capacity- or unsafety-failed seal copy leaves behind, and the schema admits it.
_ABANDONABLE = frozenset({"ready", "failed", "sealing"})
# Workspace states no attempt still needs: their bytes are derived, not durable.
_DERIVED = frozenset({"sealed", "promoted", "abandoned"})

Kind = Literal["champion", "candidate"]
Source = Literal["seed", "workspace", "candidate"]

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "CapacityEstimate",
    "ExpectedAttestation",
    "LineageProjection",
    "RecoveryReport",
    "SelectionDecision",
]


@dataclass(frozen=True)
class CapacityEstimate:
    v0_bytes: int
    required_bytes: int
    free_bytes: int
    #: ADR-0040 D8: free inodes and the need checked, `None` when not checked.
    free_inodes: int | None = None
    inode_need: InodeNeed | None = None


@dataclass(frozen=True)
class ExpectedAttestation:
    """The Candidate and dependency digests a target must attest."""

    candidate_digest: str
    dependency_digest: str


def _scan_published(root: Path, policy: ArtifactPolicyV1) -> TreeScan:
    """Re-scan a published root, translating unsafety into corruption.

    Absence and unsafety are permanent facts about the copy, so they are corruption
    and they stop the store. A transient read failure is not a fact about the bytes
    at all: it stays the store's own I/O boundary (`io_failure`), leaves the store
    open and is retried by the caller. Conflating the two would let one `EIO` write
    a permanent `store_stopped(artifact_corrupted)` that nothing can undo.
    """
    try:
        scan = scan_tree(root, policy)
        if entry_set(root) != {e.path for e in scan.artifact + scan.dependency}:
            # The digests cover only what the policy includes, so an *excluded* entry
            # added inside a published copy (a `.env`, a `__pycache__`) reproduces both
            # digests exactly while the copy is no longer the tree that was published:
            # "a champion or Candidate copy never contains an excluded entry". Only the
            # whole on-disk entry set sees it, and it is a permanent fact about the
            # copy, so it is corruption like any other unsafety.
            raise ArtifactCorruptionError("artifact_unsafe")
        return scan
    except ArtifactUnsafeError:
        raise ArtifactCorruptionError(corruption_code(root)) from None
    except OSError:
        raise ArtifactError("io_failure") from None


def _scan_seed(seed_root: Path, policy: ArtifactPolicyV1) -> TreeScan:
    """Pre-scan a caller-supplied seed behind the typed artifact boundary.

    Untranslated, an unreadable seed file raises a raw `PermissionError` carrying
    the operator's absolute seed path into a caller diagnostic. The root is
    proved to be a directory with `lstat`, so a symlinked root is `seed_missing`
    rather than a scan of whatever it points at.
    """
    try:
        info = seed_root.lstat()
    except OSError:
        raise ArtifactError("seed_missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ArtifactError("seed_missing")
    try:
        return scan_tree(seed_root, policy)
    except OSError:
        raise ArtifactError("io_failure") from None


@dataclass(frozen=True)
class ArtifactRef:
    """A published, frozen copy. `_policy` is carried so `verify` needs no caller input."""

    kind: Kind
    artifact_id: str
    root: Path
    digest: str
    dependency_digest: str
    _policy: ArtifactPolicyV1 = field(repr=False, compare=False)

    def verify(self) -> None:
        scan = _scan_published(self.root, self._policy)
        if scan.candidate_digest != self.digest or scan.dependency_digest != self.dependency_digest:
            raise ArtifactCorruptionError("artifact_digest_mismatch")

    def expected_attestation(self) -> ExpectedAttestation:
        return ExpectedAttestation(self.digest, self.dependency_digest)

    def candidate_view(self) -> CandidateView:
        """A `CandidateView` that verifies the complete published artifact tree.

        The strategy closure is built here, not in `rollout`, so execution keeps
        no import of the store; the view carries no readable-path list because the
        policy, not a caller-supplied list, decides membership.
        """

        def verify(root: Path) -> str:
            # The published-root check itself, not a weaker restatement of it. A bare
            # `scan_tree` prunes excluded entries, so both digests reproduce exactly
            # while the copy carries a `.env` that a published copy must never
            # holds; and the artifact-set digest alone leaves a tampered `uv.lock`
            # unseen. `ArtifactRef.verify` is the one check that covers the safe
            # tree, the whole entry set and both digests, so the Candidate integrity check
            # calls it rather than reimplementing part of it. Its
            # `ArtifactError`/`ArtifactCorruptionError` reach `CandidateView.verify`,
            # which translates them into `CandidateIntegrityError`.
            del root  # `self.root` is the published root the ref was built with.
            self.verify()
            return self.digest

        return CandidateView(root=self.root, digest=self.digest, artifact_paths=(), verifier=verify)


@dataclass(frozen=True)
class SelectionDecision:
    """The caller's durable selection; `decision_ref` is validated by the S2 schema."""

    round_id: int
    attempt_id: str
    candidate_digest: str
    dependency_digest: str
    decision_ref: dict[str, Any]


def _estimate(
    run_root: Path,
    scan: TreeScan,
    parallel_attempts: int,
    max_ledger_bytes: int,
    *,
    inodes: Callable[[int], InodeNeed] | None = None,
) -> CapacityEstimate:
    """Pure start check: never writes, never deletes, never re-evaluated.

    `inodes` maps F, the scanned tree's inode count, to the run's inode need
    (ADR-0040 D8); a filesystem with no fixed inode limit (`f_files == 0`) is
    not checked.
    """
    if parallel_attempts < 0:
        raise ArtifactAdmissionRefused("invalid_parallel_attempts")
    required = scan.byte_count * (1 + parallel_attempts) + max_ledger_bytes
    probe = run_root if run_root.parts else Path(".")
    while not os.path.exists(probe) and probe != probe.parent:
        probe = probe.parent
    # Module attribute lookup: the free-space source is a documented test seam.
    stats = os.statvfs(probe)
    free = stats.f_bavail * stats.f_frsize
    if free < required:
        raise ArtifactCapacityError("storage_capacity_insufficient")
    if inodes is None or stats.f_files == 0:
        return CapacityEstimate(scan.byte_count, required, free)
    need = inodes(tree_inodes(scan))
    return CapacityEstimate(scan.byte_count, required, free, stats.f_favail, need)


def _observe_estimate(
    estimate: CapacityEstimate, observer: Callable[[CapacityEstimate], None] | None
) -> None:
    # Optional output cannot turn a numerical estimate into a policy veto.
    if observer is not None:
        try:
            observer(estimate)
        except Exception:  # noqa: BLE001 -- interrupts/cancellation still propagate
            pass


class ArtifactStore:
    """One owner session over one run root's artifacts.

    Public methods are the only writers; each one either reaches a durable terminal
    record for the operation it started or raises with nothing left referenced.
    """

    def __init__(
        self,
        run_root: Path,
        policy: ArtifactPolicyV1,
        ledger: ArtifactLedger,
        *,
        hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> None:
        self._run_root = run_root
        self._policy = policy
        self._ledger = ledger
        # A plain attribute: crash fixtures assign it after construction.
        self._hooks: Mapping[str, Callable[[], None]] = dict(hooks) if hooks else {}
        # Filesystem exhaustion is session-scoped and resumable; it never writes a
        # permanent `store_stopped`, so it is kept apart from the ledger's.
        self._session_stopped = False
        # What the last `resume` had to repair; empty for a freshly created store.
        self.last_recovery = RecoveryReport()
        # The start check `create` made (ADR-0040 D8); `None` for a resumed store.
        self.capacity_estimate: CapacityEstimate | None = None
        # One coarse mutex over each public lifecycle method. Admission decisions read
        # ledger state and then append on it, and the ledger's own lock covers only the
        # append: without this, two callers racing on one attempt id could both pass
        # admission and the loser's refused record would poison the writer. This also
        # serializes the copy inside `materialize_candidate` and `seal`
        # as well, so parallel attempts are opened one after another.
        self._lock = threading.RLock()

    # -- construction ----------------------------------------------------------

    @staticmethod
    def preflight(
        run_root: Path,
        seed_root: Path,
        policy: ArtifactPolicyV1,
        parallel_attempts: int,
        *,
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
        inodes: Callable[[int], InodeNeed] | None = None,
        _estimate_observer: Callable[[CapacityEstimate], None] | None = None,
    ) -> CapacityEstimate:
        estimate = _estimate(
            Path(run_root),
            _scan_seed(Path(seed_root), policy),
            parallel_attempts,
            max_ledger_bytes,
            inodes=inodes,
        )
        _observe_estimate(estimate, _estimate_observer)
        return estimate

    @classmethod
    def create(
        cls,
        run_root: Path,
        seed_root: Path,
        policy: ArtifactPolicyV1,
        *,
        parallel_attempts: int,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
        expected: ArtifactStoreIdentityV1 | None = None,
        owed_rebases: Sequence[Mapping[str, str]] = (),
        inodes: Callable[[int], InodeNeed] | None = None,
        _estimate_observer: Callable[[CapacityEstimate], None] | None = None,
        _hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> ArtifactStore:
        """One seed pre-scan yields the store identity, the preflight size and the
        `v0` expectation, so a seed changed after that scan cannot be published.

        `expected` is the declare-then-create boundary: a caller that already
        declared this store's identity elsewhere (e.g. a sibling campaign journal)
        passes the identity it declared, and a seed that scans to something else is
        refused before anything is written or estimated.

        `owed_rebases` are the campaign rebases this ledger owes a `store_rebased`
        for (ADR-0015). A run whose store never reached birth can still have
        declared a rebase in its campaign journal -- the declaration is written
        before siblings are adopted -- and ADR-0015 puts the completion "first":
        the rows are appended in declaration order directly after `store_created`
        and before `v0` or anything else this store writes.
        """
        run_root, seed_root = Path(run_root), Path(seed_root)
        seed_scan = _scan_seed(seed_root, policy)
        identity = ArtifactStoreIdentityV1(
            policy, seed_scan.candidate_digest, seed_scan.dependency_digest
        )
        if expected is not None and expected.digest != identity.digest:
            raise ArtifactAdmissionRefused("run_identity_changed")
        estimate = _estimate(
            run_root, seed_scan, parallel_attempts, max_ledger_bytes, inodes=inodes
        )
        _observe_estimate(estimate, _estimate_observer)
        ledger = ArtifactLedger.create(
            run_root,
            identity,
            version_resolver=version_resolver,
            secrets=secrets,
            max_ledger_bytes=max_ledger_bytes,
        )
        store = cls(run_root, policy, ledger, hooks=_hooks)
        store.capacity_estimate = estimate
        try:
            store._write_owed_rebases(identity, owed_rebases)
            store._hook("after_store_created")
            store._make_layout()
            store._copy(
                "champion",
                "v0",
                None,
                "seed",
                seed_root,
                store._destination("champion", "v0"),
                expected=seed_scan,
                freeze=True,
            )
        except BaseException:
            ledger.close()
            raise
        return store

    @classmethod
    def resume(
        cls,
        run_root: Path,
        policy: ArtifactPolicyV1,
        *,
        seed_root: Path | None = None,
        version_resolver: Callable[[str], str] | None = None,
        secrets: Sequence[str] = (),
        max_ledger_bytes: int = MAX_LEDGER_BYTES,
        recover_editor_workspaces: frozenset[str] = frozenset(),
        admitted_versions: Collection[str] = (),
        owed_rebases: Sequence[Mapping[str, str]] = (),
        _hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> ArtifactStore:
        """Reopen a run root and recover its durable prefix.

        The ledger's own resume takes the lease, proves the store identity and
        repairs a torn tail; recovery then proceeds in ledger order
        under one lock, then the derived cleanup of workspaces nothing references.
        No hook fires for the store's creation: this store was created elsewhere.

        `owed_rebases` are the campaign rebases this ledger owes a `store_rebased`
        for (ADR-0015), in declaration order. They are appended as the first
        records of this session -- after the ledger's own resume has proved the
        identity and taken the lease, and before `_recover` writes so much as one
        `operation_interrupted` -- because ADR-0015 says a resume that finds the
        declaration without its completion "writes the second first". Completing
        them after recovery would wedge this session's repair records between a
        declaration and the completion that belongs beside it.
        """
        run_root = Path(run_root)
        ledger = ArtifactLedger.resume(
            run_root,
            policy,
            version_resolver=version_resolver,
            secrets=secrets,
            max_ledger_bytes=max_ledger_bytes,
            admitted_versions=admitted_versions,
        )
        store = cls(run_root, policy, ledger, hooks=_hooks)
        try:
            # The ledger's debt, before this session's own first record. The
            # identity is the one the ledger just proved. A rebase must not
            # from moving the store's identity, so the stamp moves and nothing
            # else does.
            if owed_rebases:
                store._write_owed_rebases(
                    ArtifactStoreIdentityV1.from_payload(ledger.state.identity), owed_rebases
                )
            # A crash between `store_created` and the layout leaves nothing to
            # open into; the layout is rebuilt idempotently before anything reads.
            store._make_layout()
            store._clear_staging()
            with store._lock:
                actions = store._recover(
                    None if seed_root is None else Path(seed_root), recover_editor_workspaces
                )
                store.last_recovery = RecoveryReport(actions, store._derived_cleanup())
        except BaseException:
            ledger.close()
            raise
        return store

    @staticmethod
    def inspect(run_root: Path, *, verify: bool = False) -> LineageProjection:
        """Read-only lineage of any run root: no lease, no write, no repair.

        `verify=True` recomputes every published copy's digests with the policy the
        store persisted in `store_created`, so a reader needs no caller input, and
        reports corruption per copy instead of raising: a corrupt non-winner is an
        inspection finding, never a stop.
        """
        run_root = Path(run_root)
        return project_lineage(ArtifactLedger.inspect(run_root), run_root=run_root, verify=verify)

    # -- accessors -------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._ledger.run_id

    @property
    def ledger(self) -> ArtifactLedger:
        """Read-only access to the owned ledger; callers must not append through it."""
        return self._ledger

    def rebase_ledger(
        self,
        identity: ArtifactStoreIdentityV1,
        *,
        campaign_rebase: str,
        authorization_id: str,
        producing_version: str,
    ) -> int:
        """Re-stamp the ledger for an authorized campaign rebase (PDR-0010, ADR-0015).

        The store's own writing seam for the one record that is not about an
        artifact: the campaign journal declared the rebase, this completes it.
        It takes the store lock like every other write, so it cannot interleave
        with a copy or a publication, and it is the reason `ledger` itself stays
        read-only.

        `producing_version` belongs to the campaign record being completed and
        is forwarded unchanged; the store never substitutes the running
        package's version for it.
        """

        with self._lock:
            return self._ledger.rebase(
                identity,
                campaign_rebase=campaign_rebase,
                authorization_id=authorization_id,
                producing_version=producing_version,
            )

    def _write_owed_rebases(
        self, identity: ArtifactStoreIdentityV1, owed: Sequence[Mapping[str, str]]
    ) -> None:
        """Every owed completion, in declaration order, through the store's own seam."""

        for entry in owed:
            self.rebase_ledger(
                identity,
                campaign_rebase=entry["campaign_rebase"],
                authorization_id=entry["authorization_id"],
                producing_version=entry["producing_version"],
            )

    @property
    def stopped(self) -> str | None:
        return self._ledger.state.stopped

    def close(self) -> None:
        self._ledger.close()

    def __enter__(self) -> ArtifactStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- lifecycle -------------------------------------------------------------

    def champion(self) -> ArtifactRef:
        with self._lock:
            return self._champion()[0]

    def published_ref(self, kind: Kind, artifact_id: str) -> ArtifactRef:
        """A published copy as the ledger describes it, without scanning it.

        The reference carries the ledger's digests and the copy's root; the
        caller verifies (`ArtifactRef.verify`) when it reads. Unlike the store's
        own `_verified`, a corrupt copy found this way stops nothing — the
        brief's history reports such a copy as
        `unavailable`, and a non-winner's corruption is an inspection finding,
        never a stop.
        """

        with self._lock:
            self._check_open()
            record = self._ledger.state.published.get(f"{kind}/{artifact_id}")
            if record is None:
                raise ArtifactAdmissionRefused("not_published")
            payload = record.payload
            return ArtifactRef(
                kind,
                artifact_id,
                self._destination(kind, artifact_id),
                payload["candidate_digest"],
                payload["dependency_digest"],
                self._policy,
            )

    def restore_candidate_workspace(
        self, attempt_id: str, *, parent_id: str, parent_digest: str
    ) -> Path:
        """Reset an interrupted editor's disposable workspace to its frozen Parent.

        The existing ready workspace remains the ledger authority. Replacing its
        mutable bytes adds neither another attempt nor a publication, and a crash
        during replacement is safely repeatable from the same verified Parent.
        """
        with self._lock:
            self._check_copies()
            _check_identifier(attempt_id)
            state = self._ledger.state
            if (
                state.workspaces.get(attempt_id) != "ready"
                or f"candidate/{attempt_id}" in state.published
            ):
                raise ArtifactAdmissionRefused("workspace_not_ready")
            if state.workspace_parents.get(attempt_id) != parent_id:
                raise ArtifactAdmissionRefused("parent_changed")
            parent, parent_scan = self._champion()
            if parent.artifact_id != parent_id or parent.digest != parent_digest:
                raise ArtifactAdmissionRefused("parent_changed")
            workspace = self._workspace(attempt_id)
            staging_root: Path | None = None
            try:
                staged = stage_copy(
                    parent.root, parent_scan, self._policy, self._staging, freeze=False
                )
                staging_root = staged.root
                remove_tree(workspace)
                os.replace(staged.root, workspace)
                staging_root = None
                fsync_directory(workspace.parent)
                restore_writable(workspace)
            except OSError as exc:
                if map_os_error(exc) == "storage_capacity_exhausted":
                    self._session_stopped = True
                    raise ArtifactCapacityError("storage_capacity_exhausted") from None
                raise ArtifactError("io_failure") from None
            finally:
                if staging_root is not None:
                    try:
                        remove_tree(staging_root)
                    except (OSError, ArtifactError):
                        # Recovery owns orphaned staging; cleanup must not hide
                        # the typed failure that stopped this restoration.
                        pass
            return workspace

    def materialize_candidate(self, attempt_id: str) -> Path:
        """Copy the verified Champion into a mutable, unpublished workspace.

        The workspace is not an artifact: it has no identity, no destination guard and
        no copy operation, only the `workspace` operation pair that bounds its cleanup.
        """
        with self._lock:
            self._check_copies()
            _check_identifier(attempt_id)
            state = self._ledger.state
            if attempt_id in state.workspaces or f"candidate/{attempt_id}" in state.published:
                raise ArtifactAdmissionRefused("attempt_known")
            parent, parent_scan = self._champion()
            workspace = self._workspace(attempt_id)
            operation = uuid4().hex
            self._ledger.append(
                "workspace_opened",
                dict(attempt_id=attempt_id, parent_id=parent.artifact_id),
                operation,
            )
            self._hook("after_workspace_opened")
            staging_root: Path | None = None
            try:
                staged = stage_copy(
                    parent.root, parent_scan, self._policy, self._staging, freeze=False
                )
                staging_root = staged.root
                os.replace(staged.root, workspace)
                staging_root = None
                fsync_directory(workspace.parent)
                restore_writable(workspace)
            except (OSError, ArtifactUnsafeError) as exc:
                code = (
                    "artifact_unsafe" if isinstance(exc, ArtifactUnsafeError) else map_os_error(exc)
                )
                # Closed first, before any step that can fail on its own: the session
                # refuses further copies from the moment exhaustion is known.
                self._session_stopped = self._session_stopped or (
                    code == "storage_capacity_exhausted"
                )
                try:
                    # Same boundary as the success terminal (see `_copy`).
                    self._append_terminal("workspace_failed", dict(code=code), operation)
                finally:
                    if staging_root is not None:
                        remove_tree(staging_root)
                    remove_tree(workspace)
                if code == "storage_capacity_exhausted":
                    raise ArtifactCapacityError("storage_capacity_exhausted") from None
                if isinstance(exc, ArtifactUnsafeError):
                    raise
                raise ArtifactError("io_failure") from None
            except BaseException:
                # A ledger fault or an interrupt leaves the operation open for recovery,
                # but never leaves staging behind for it to adopt.
                if staging_root is not None:
                    remove_tree(staging_root)
                raise
            self._append_terminal("workspace_ready", {}, operation)
            self._hook("after_workspace_ready")
            return workspace

    def abandon(self, attempt_id: str) -> None:
        """Abandon an attempt so it can never be sealed."""
        with self._lock:
            # Abandonment publishes nothing, so it stays available to a session that
            # ran out of space: it is how the caller releases the workspace bytes.
            self._check_open()
            state = self._ledger.state
            if state.workspaces.get(attempt_id) not in _ABANDONABLE:
                raise ArtifactAdmissionRefused("workspace_not_abandonable")
            if f"candidate/{attempt_id}" in state.published:
                raise ArtifactAdmissionRefused("candidate_published")
            self._ledger.append(
                "candidate_abandoned", dict(attempt_id=attempt_id, reason="caller_abandoned")
            )
            try:
                remove_tree(self._workspace(attempt_id))
            except OSError:
                raise ArtifactError("io_failure") from None

    def seal(self, attempt_id: str) -> ArtifactRef:
        """Validate workspace changes and publish the Candidate exactly once.

        A rejected workspace stays `ready` and writes no ledger record, so the caller
        can inspect it and then `abandon` it.
        """
        with self._lock:
            self._check_copies()
            state = self._ledger.state
            if state.workspaces.get(attempt_id) != "ready":
                raise ArtifactAdmissionRefused("workspace_not_ready")
            workspace = self._workspace(attempt_id)
            parent_id = state.workspace_parents[attempt_id]
            parent, parent_scan = self._verified("champion", parent_id)
            try:
                # An unsafe workspace tree is the editor's fault, not the store's: the
                # code ("symlink"|"special_file"|"hard_link"|...) reaches the caller
                # unchanged. A read failure while scanning or diffing is the store's
                # own I/O boundary, and neither leaves a ledger record: the workspace
                # stays `ready`, so the caller may retry or abandon it.
                child = scan_tree(workspace, self._policy)
                diff = changes(parent_scan, child, parent.root, workspace)
            except OSError:
                raise ArtifactError("io_failure") from None
            if child.dependency_digest != parent_scan.dependency_digest or not all(
                self._policy.is_writable(PurePosixPath(path)) for path in diff
            ):
                raise ArtifactUnsafeError("artifact_unsafe")
            ref = self._copy(
                "candidate",
                attempt_id,
                parent_id,
                "workspace",
                workspace,
                self._destination("candidate", attempt_id),
                expected=child,
                freeze=True,
            )
            self._hook("after_seal_finished")
            # Derived cleanup: the Candidate is already published, so a failure here
            # loses nothing that is referenced.
            try:
                remove_tree(workspace)
            except OSError:
                raise ArtifactError("io_failure") from None
            return ref

    def promote(self, decision: SelectionDecision) -> ArtifactRef:
        """Publish the selected Candidate as the next Champion.

        Only `promotion_committed` moves the champion pointer; a repeat of the same
        decision completes the pending promotion instead of starting a new one.
        """
        with self._lock:
            self._check_copies()
            state = self._ledger.state
            # The whole decision is validated before any state is keyed on it: an
            # unhashable `round_id` or a malformed `decision_ref` is refused here
            # rather than raising an untyped error on a lookup, or reaching the
            # envelope check inside the ledger, which would poison its writer.
            payload = _pending_payload(decision, len(state.committed))
            published = state.published.get(f"candidate/{decision.attempt_id}")
            if published is None:
                raise ArtifactAdmissionRefused("candidate_not_published")
            if (
                published.payload["candidate_digest"] != decision.candidate_digest
                or published.payload["dependency_digest"] != decision.dependency_digest
            ):
                raise ArtifactAdmissionRefused("decision_digest_mismatch")
            pending = state.promotions.get(decision.round_id)
            if pending is not None:
                if (
                    pending.payload["attempt_id"] != decision.attempt_id
                    or pending.payload["decision_ref"] != payload["decision_ref"]
                ):
                    raise ArtifactAdmissionRefused("round_decided")
                return self._complete_promotion(pending)
            for record in state.committed:
                if record.payload["round_id"] == decision.round_id:
                    # A decided round is answered only for the decision that decided
                    # it; `promotion_committed` does not carry the reference, so the
                    # round's `promotion_pending` record is the authority.
                    if (
                        record.payload["attempt_id"] != decision.attempt_id
                        or self._decision_ref(decision.round_id) != payload["decision_ref"]
                    ):
                        raise ArtifactAdmissionRefused("round_decided")
                    return self._verified("champion", record.payload["version_id"])[0]
            self._ledger.append("promotion_pending", payload)
            self._hook("after_pending")
            return self._complete_promotion(self._ledger.state.promotions[decision.round_id])

    @property
    def owed_auxiliary(self) -> tuple[AuxiliaryDeclaration, ...]:
        """Declared content still needing publication, with no inferred source path.

        A campaign recovery owner must supply precisely these prepared bytes. This
        property does not mean a baseline or comparison is ready to use.
        """
        state = self._ledger.state
        return tuple(
            AuxiliaryDeclaration.from_payload(dict(record.payload))
            for locator, record in state.auxiliary_declared.items()
            if locator not in state.auxiliary_published
        )

    def publish_auxiliary(
        self, declaration: AuxiliaryDeclaration, prepared_directory: Path
    ) -> AuxiliaryPublication:
        """Publish a caller-durable family-15 declaration under this store's lease.

        Like promotion's SelectionDecision, declaration_ref is a caller assertion:
        the campaign evidence audit owns same-run existence/family/semantic proof.
        This store enforces closed types, one campaign reference namespace, exact
        identity across retries, local bytes and the sole ledger publication.
        """
        with self._lock:
            self._check_copies()
            if not isinstance(declaration, AuxiliaryDeclaration):
                raise ArtifactAdmissionRefused("invalid_auxiliary_declaration")
            state = self._ledger.state
            if state.auxiliary_campaign_run_id not in (None, declaration.declaration_ref.run_id):
                raise ArtifactAdmissionRefused("foreign_auxiliary_declaration")
            original = state.auxiliary_declared.get(declaration.locator)
            if original is not None and original.payload != declaration.to_payload():
                raise ArtifactAdmissionRefused("auxiliary_declaration_conflict")
            if declaration.locator in state.auxiliary_published:
                return self._verified_auxiliary(declaration)
            if any(
                record.event == "auxiliary_publish_started"
                and AuxiliaryDeclaration.from_payload(dict(record.payload)).locator
                == declaration.locator
                for record in state.pending.values()
            ):
                raise ArtifactAdmissionRefused("auxiliary_recovery_required")
            destination = self._run_root / declaration.locator
            try:
                if destination_present(self._run_root, declaration):
                    if original is None:
                        raise ArtifactAdmissionRefused("auxiliary_destination_exists")
                    verify_directory(destination, declaration, committed=True)
                else:
                    # Pre-scan before declaration: no durable debt for bad source data.
                    verify_directory(Path(prepared_directory), declaration, committed=False)
                return self._publish_auxiliary(declaration, Path(prepared_directory))
            except ArtifactCorruptionError:
                self._stop("artifact_corrupted")
                raise
            except OSError:
                raise ArtifactError("io_failure") from None

    def _verified_auxiliary(self, declaration: AuxiliaryDeclaration) -> AuxiliaryPublication:
        try:
            verify_directory(self._run_root / declaration.locator, declaration, committed=True)
        except ArtifactCorruptionError:
            self._stop("artifact_corrupted")
            raise
        except OSError:
            raise ArtifactError("io_failure") from None
        return next(
            p for p in project_auxiliary(self._ledger.snapshot()) if p.declaration == declaration
        )

    def _publish_auxiliary(
        self, declaration: AuxiliaryDeclaration, source: Path | None
    ) -> AuxiliaryPublication:
        operation = uuid4().hex
        self._ledger.append("auxiliary_publish_started", declaration.to_payload(), operation)
        self._hook("after_auxiliary_started")
        try:
            if source is not None and not destination_present(self._run_root, declaration):
                publish_directory(self._run_root, source, declaration)
            else:
                # A crash after rename but before freeze/parent fsync still owes durability.
                finish_directory(self._run_root, declaration)
        except (OSError, ArtifactUnsafeError) as exc:
            code = "artifact_unsafe" if isinstance(exc, ArtifactUnsafeError) else map_os_error(exc)
            self._session_stopped = self._session_stopped or code == "storage_capacity_exhausted"
            self._append_terminal(
                "auxiliary_publish_failed",
                dict(declaration_digest=declaration.digest, code=code),
                operation,
            )
            if code == "storage_capacity_exhausted":
                raise ArtifactCapacityError(code) from None
            if isinstance(exc, ArtifactUnsafeError):
                raise
            raise ArtifactError("io_failure") from None
        self._hook("after_auxiliary_replace")
        self._append_terminal(
            "auxiliary_publish_finished", dict(declaration_digest=declaration.digest), operation
        )
        self._hook("after_auxiliary_finished")
        return self._verified_auxiliary(declaration)

    def _recover_auxiliary(self) -> list[str]:
        """Verify committed bytes; finish renamed debts, leave absent debts explicit."""
        state = self._ledger.state
        actions = []
        for locator, record in list(state.auxiliary_declared.items()):
            declaration = AuxiliaryDeclaration.from_payload(dict(record.payload))
            try:
                if locator in state.auxiliary_published:
                    self._verified_auxiliary(declaration)
                elif destination_present(self._run_root, declaration):
                    verify_directory(self._run_root / locator, declaration, committed=True)
                    self._publish_auxiliary(declaration, None)
                    actions.append("auxiliary_completed:" + locator)
                else:
                    actions.append("auxiliary_source_required:" + locator)
            except ArtifactUnsafeError:
                self._stop("artifact_corrupted")
                raise ArtifactCorruptionError("artifact_unsafe") from None
            except ArtifactCorruptionError:
                self._stop("artifact_corrupted")
                raise
            except OSError:
                raise ArtifactError("io_failure") from None
        return actions

    # -- internals -------------------------------------------------------------

    def _complete_promotion(self, pending: Envelope) -> ArtifactRef:
        """Idempotent completion: the champion copy, then the committing record."""
        p = pending.payload
        version_id, attempt_id = p["version_id"], p["attempt_id"]
        # Unconditional, before anything commits: a corrupt promotion source stops the
        # store permanently, and the champion copy being already published
        # does not make the source healthy. Verifying only on the unpublished path let
        # a crash between `artifact_copy_finished` and `promotion_committed` commit a
        # promotion whose selected Candidate is corrupt at recovery time.
        _, scan = self._verified("candidate", attempt_id)
        champion: ArtifactRef | None = None
        if f"champion/{version_id}" not in self._ledger.state.published:
            champion = self._copy(
                "champion",
                version_id,
                attempt_id,
                "candidate",
                self._destination("candidate", attempt_id),
                self._destination("champion", version_id),
                expected=scan,
                freeze=True,
            )
        self._ledger.append(
            "promotion_committed",
            dict(
                round_id=p["round_id"],
                attempt_id=attempt_id,
                version_id=version_id,
                candidate_digest=p["candidate_digest"],
                dependency_digest=p["dependency_digest"],
            ),
        )
        return champion if champion is not None else self._verified("champion", version_id)[0]

    def _decision_ref(self, round_id: int) -> dict[str, Any] | None:
        for record in reversed(self._ledger.snapshot().records):
            payload = record.payload
            if record.event == "promotion_pending" and payload["round_id"] == round_id:
                return dict(payload["decision_ref"])
        return None

    # -- recovery --------------------------------------------------------------

    def _recover(
        self, seed_root: Path | None, recover_editor_workspaces: frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Recover interrupted operations in ledger order, appending through the ledger.

        One recovery terminal per interrupted operation; every forward completion
        runs as a fresh operation with its own started/terminal pair.
        """
        state = self._ledger.state
        published_v0 = "champion/v0" in state.published
        if seed_root is not None and published_v0:
            raise ArtifactAdmissionRefused("seed_root_rejected")
        stopped = state.stopped is not None
        seed: tuple[Path, TreeScan] | None = None
        if not stopped and not published_v0:
            # Until `v0` is published the store owns no source bytes of its own. The
            # seed is resolved before the first append, so a refused seed still leaves
            # the ledger exactly as this `resume` found it.
            if seed_root is None:
                raise ArtifactAdmissionRefused("seed_root_required")
            seed = (seed_root, self._seed_scan(seed_root, state))
        # Every open operation is closed, a stopped store included: `operation_interrupted`
        # is a reserved terminal the ledger admits after a stop, and an operation left
        # open forever would be reported as still in flight by every later resume and
        # by inspection. Closing them is the only thing a stopped store repairs.
        for operation_id, start in list(state.pending.items()):
            self._ledger.append(
                "operation_interrupted", dict(operation=STARTS[start.event]), operation_id
            )
        if stopped:
            # A permanent stop is never repaired beyond that: inspection stays
            # available and `_check_open` refuses every copy. Nothing below writes.
            return ()
        # Published auxiliary evidence must be sound before an owed Promotion
        # can publish a tree based on it. Older runs have no auxiliary records.
        actions: list[str] = self._recover_auxiliary()
        if seed is not None:
            actions.append(self._recover_v0(*seed))
        actions.extend(self._recover_workspaces(state, recover_editor_workspaces))
        actions.extend(self._recover_promotions(state))
        return tuple(actions)

    def _seed_scan(self, seed_root: Path, state: ArtifactLifecycle) -> TreeScan:
        """Prove the supplied seed is the one `store_created` recorded.

        This runs before any recovery record is appended, so a refused seed leaves
        the ledger exactly as it was found.
        """
        try:
            scan = scan_tree(seed_root, self._policy)
        except ArtifactUnsafeError:
            # An unsafe seed cannot be the tree whose digests the identity holds.
            raise ArtifactCorruptionError("seed_mismatch") from None
        except OSError:
            raise ArtifactError("io_failure") from None
        identity = state.identity
        if (
            scan.candidate_digest != identity["seed_candidate_digest"]
            or scan.dependency_digest != identity["seed_dependency_digest"]
        ):
            raise ArtifactCorruptionError("seed_mismatch")
        return scan

    def _recover_v0(self, seed_root: Path, seed_scan: TreeScan) -> str:
        try:
            self._copy(
                "champion",
                "v0",
                None,
                "seed",
                seed_root,
                self._destination("champion", "v0"),
                expected=seed_scan,
                freeze=True,
            )
        except ArtifactUnsafeError:
            # The destination holds bytes that are not the verified seed's, so the
            # store cannot claim them under its identity.
            raise ArtifactCorruptionError("seed_mismatch") from None
        return "v0_rerun"

    def _recover_workspaces(
        self, state: ArtifactLifecycle, recover_editor_workspaces: frozenset[str] = frozenset()
    ) -> list[str]:
        """Recover workspaces in the order their attempts opened.

        A sealed Candidate with no promotion has no row here: verifying the digests
        of a non-winner is owned by `inspect(verify=True)`, not by recovery.
        """
        outcomes = copy_outcomes(self._ledger.snapshot().records)
        actions: list[str] = []
        for attempt, workspace_state in list(state.workspaces.items()):
            if workspace_state == "ready" and attempt in recover_editor_workspaces:
                continue
            if workspace_state == "sealing":
                actions.append(self._recover_seal(attempt, outcomes[f"candidate/{attempt}"]))
                continue
            if workspace_state in ("opening", "failed"):
                reason = "workspace_not_ready"
            elif workspace_state == "ready":
                # A seal start moves the workspace to `sealing`, so `ready` at
                # recovery means no seal was ever attempted for this attempt.
                reason = "unsealed_at_recovery"
            else:
                continue
            actions.append(self._abandon_at_recovery(attempt, reason))
        return actions

    def _recover_seal(self, attempt: str, outcome: CopyOutcome) -> str:
        """Re-run a retriable seal copy only from a workspace that still is what
        its `artifact_copy_started` promised; anything else loses the attempt.
        """
        started = outcome.started.payload
        workspace = self._workspace(attempt)
        scan: TreeScan | None = None
        if outcome.retriable:
            try:
                scan = scan_tree(workspace, self._policy)
            except (ArtifactUnsafeError, OSError):
                scan = None
        if (
            scan is not None
            and scan.candidate_digest == started["candidate_digest"]
            and scan.dependency_digest == started["dependency_digest"]
        ):
            self._copy(
                "candidate",
                attempt,
                started["parent_id"],
                "workspace",
                workspace,
                self._destination("candidate", attempt),
                expected=scan,
                freeze=True,
            )
            return "seal_rerun"
        return self._abandon_at_recovery(attempt, "seal_not_published")

    def _abandon_at_recovery(self, attempt_id: str, reason: str) -> str:
        # The workspace bytes are removed by derived cleanup, which runs next and
        # is the single, idempotent removal site for every abandoned attempt.
        self._ledger.append("candidate_abandoned", dict(attempt_id=attempt_id, reason=reason))
        return f"abandoned_{reason}"

    def _recover_promotions(self, state: ArtifactLifecycle) -> list[str]:
        """A durable `promotion_pending` resolves to its own winner, or stops.

        The caller's durable selection is never reversed: either the same
        `version_id` is published and committed, or the store stops permanently.
        """
        outcomes = copy_outcomes(self._ledger.snapshot().records)
        actions: list[str] = []
        for pending in list(state.promotions.values()):
            outcome = outcomes.get(f"champion/{pending.payload['version_id']}")
            if outcome is not None and outcome.failed_unsafe:
                raise self._corrupt_promotion()
            try:
                # `_verified` inside already stops the store on a corrupt source.
                self._complete_promotion(pending)
            except ArtifactUnsafeError:
                raise self._corrupt_promotion() from None
            actions.append("promotion_completed")
        return actions

    def _corrupt_promotion(self) -> ArtifactCorruptionError:
        self._stop("artifact_corrupted")
        return ArtifactCorruptionError("artifact_unsafe")

    def _clear_staging(self) -> None:
        """Owner startup discards every staged tree; none of them is referenced."""
        try:
            entries = list(self._staging.iterdir())
        except OSError:
            raise ArtifactError("io_failure") from None
        failed = False
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                else:
                    remove_tree(entry)
            except OSError:
                # One unremovable tree must not strand the rest: staging is discarded
                # as a whole, and the next `resume` retries whatever is left.
                failed = True
        if failed:
            raise ArtifactError("io_failure")

    def _derived_cleanup(self) -> tuple[str, ...]:
        """Remove every workspace whose attempt is published or abandoned.

        Removal is idempotent and never a ledger event; a failure is reported as an
        operational `io_failure` from this call and retried by the next `resume`,
        so a workspace outlives its attempt only until then.
        """
        state = self._ledger.state
        removed: list[str] = []
        failed = False
        try:
            entries = sorted(self._artifacts.joinpath(WORKSPACES).iterdir())
        except OSError:
            raise ArtifactError("io_failure") from None
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if state.workspaces.get(entry.name) not in _DERIVED:
                continue
            try:
                remove_tree(entry)
            except OSError:
                failed = True
                continue
            removed.append(entry.name)
        if failed:
            raise ArtifactError("io_failure")
        return tuple(removed)

    def _copy(
        self,
        kind: Kind,
        artifact_id: str,
        parent_id: str | None,
        source: Source,
        source_root: Path,
        destination: Path,
        *,
        expected: TreeScan,
        freeze: bool,
    ) -> ArtifactRef:
        """Publish one copy against its caller's pre-scan.

        The source is pre-scanned by the caller, because every caller already needed
        that scan to decide the copy was legal; `expected` is the digest that the
        staged bytes must reproduce.
        """
        check_destination_parent(destination.parent, self._run_root)
        operation = uuid4().hex
        self._ledger.append(
            "artifact_copy_started",
            dict(
                kind=kind,
                artifact_id=artifact_id,
                parent_id=parent_id,
                source=source,
                candidate_digest=expected.candidate_digest,
                dependency_digest=expected.dependency_digest,
                file_count=expected.file_count,
                byte_count=expected.byte_count,
            ),
            operation,
        )
        self._hook("after_started")
        started = time.monotonic()
        staging_root: Path | None = None
        try:
            staged = stage_copy(source_root, expected, self._policy, self._staging, freeze=freeze)
            staging_root = staged.root
            # `publish` owns the durability barrier and consumes the staging root.
            publish(staged, destination, expected, self._policy)
            staging_root = None
        except (OSError, ArtifactUnsafeError) as exc:
            code = "artifact_unsafe" if isinstance(exc, ArtifactUnsafeError) else map_os_error(exc)
            # Closed first, before the terminal append that can fail on its own: the
            # session refuses further copies from the moment exhaustion is known.
            self._session_stopped = self._session_stopped or code == "storage_capacity_exhausted"
            # Through the same boundary as the success terminal: a full filesystem can
            # fail this append too, and a raw `JournalFault` from a recorded-failure
            # path would escape the typed artifact surface.
            self._append_terminal("artifact_copy_failed", dict(code=code), operation)
            if code == "storage_capacity_exhausted":
                raise ArtifactCapacityError("storage_capacity_exhausted") from None
            if isinstance(exc, ArtifactUnsafeError):
                raise
            raise ArtifactError("io_failure") from None
        except BaseException:
            # An unusable writer (a ledger fault) cannot record a terminal, and an
            # interrupt records nothing; neither may leave staged bytes behind.
            if staging_root is not None:
                remove_tree(staging_root)
            raise
        self._hook("after_replace")
        self._append_terminal(
            "artifact_copy_finished",
            dict(latency_ms=max(0, int((time.monotonic() - started) * 1000))),
            operation,
        )
        self._hook("after_finished")
        return ArtifactRef(
            kind,
            artifact_id,
            destination,
            expected.candidate_digest,
            expected.dependency_digest,
            self._policy,
        )

    def _append_terminal(self, event: str, payload: dict[str, Any], operation: str) -> None:
        """Append an operation terminal, translating filesystem exhaustion.

        A full filesystem stops the ledger from recording the terminal, so the last
        durable boundary is the operation's own `started` record: the operation stays
        open for recovery to repeat, this session refuses further copies, and the caller sees
        the typed capacity error instead of a raw `JournalFault` from a code path that
        already succeeded on disk. Any other fault is an unusable writer and is left
        untranslated, exactly as the enclosing `except BaseException` expects.

        Failure terminals (`artifact_copy_failed`, `workspace_failed`) go through here
        too. Exhaustion is precisely the condition that makes a copy fail *and* makes
        recording that failure fail, so the second fault must reach the caller as the
        same typed capacity error rather than as a raw `JournalFault`.
        """
        try:
            self._ledger.append(event, payload, operation)
        except JournalFault as exc:
            if exc.code != "storage_capacity_exhausted":
                raise
            self._session_stopped = True
            raise ArtifactCapacityError("storage_capacity_exhausted") from None

    def _champion(self) -> tuple[ArtifactRef, TreeScan]:
        self._check_open()
        champion_id = self._ledger.state.champion_id
        if champion_id is None:
            raise ArtifactAdmissionRefused("initializing")
        return self._verified("champion", champion_id)

    def _verified(self, kind: Kind, artifact_id: str) -> tuple[ArtifactRef, TreeScan]:
        """Re-scan a published copy and prove it still is what the ledger published."""
        record = self._ledger.state.published.get(f"{kind}/{artifact_id}")
        if record is None:
            raise ArtifactAdmissionRefused("not_published")
        payload = record.payload
        root = self._destination(kind, artifact_id)
        try:
            scan = _scan_published(root, self._policy)
            if (
                scan.candidate_digest != payload["candidate_digest"]
                or scan.dependency_digest != payload["dependency_digest"]
            ):
                raise ArtifactCorruptionError("artifact_digest_mismatch")
        except ArtifactCorruptionError as exc:
            self._stop("artifact_corrupted")
            raise ArtifactCorruptionError(exc.code) from None
        ref = ArtifactRef(
            kind,
            artifact_id,
            root,
            payload["candidate_digest"],
            payload["dependency_digest"],
            self._policy,
        )
        return ref, scan

    def _stop(self, code: str) -> None:
        if self._ledger.state.stopped is None:
            self._ledger.append("store_stopped", dict(code=code))

    def _check_open(self) -> None:
        """A permanently stopped store admits nothing new."""
        stopped = self._ledger.state.stopped
        if stopped is not None:
            raise ArtifactStoreStopped(stopped)

    def _check_copies(self) -> None:
        """Exhaustion refuses *copies* for this owner session; reading the
        champion and abandoning a workspace stay available, since neither copies.
        """
        self._check_open()
        if self._session_stopped:
            raise ArtifactAdmissionRefused("storage_capacity_exhausted")

    def _make_layout(self) -> None:
        """Create or adopt the run root's artifact layout, following no symlink.

        `artifacts/` first, then its children, so a tampered root is refused before any
        child path under it is even resolved. This runs on `create` and on every
        `resume`, ahead of `_clear_staging` and everything that reads.
        """
        for path in (self._artifacts, *(self._artifacts / name for name in _SUBDIRECTORIES)):
            _make_private_directory(path)
        fsync_directory(self._artifacts)
        fsync_directory(self._run_root)

    def _hook(self, name: str) -> None:
        hook = self._hooks.get(name)
        if hook is not None:
            hook()

    @property
    def _artifacts(self) -> Path:
        return self._run_root / ARTIFACTS_DIR

    @property
    def _staging(self) -> Path:
        return self._artifacts / STAGING

    def _workspace(self, attempt_id: str) -> Path:
        return self._artifacts / WORKSPACES / attempt_id

    def _destination(self, kind: Kind, artifact_id: str) -> Path:
        return self._artifacts / (CHAMPIONS if kind == "champion" else CANDIDATES) / artifact_id


def _make_private_directory(path: Path) -> None:
    """Create `path` 0o700, or adopt an existing caller-owned real directory.

    `mkdir(exist_ok=True)` accepts a symlink that already stands where the directory
    should be, and the `chmod` that follows would then follow it: a `run/artifacts` or
    `run/artifacts/champions` replaced between sessions would silently redirect every
    later create, publish and cleanup into an external tree. So presence is proved with
    `lstat` and rejected before any mutation, and absence is the only case that creates.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactUnsafeError("escape")
        if info.st_uid != os.geteuid():
            raise ArtifactUnsafeError("foreign_owner")
    # `mkdir`'s mode is masked by the process umask; the layout is private.
    os.chmod(path, 0o700)


def _check_identifier(value: str) -> None:
    """Path segments come from validated identifiers only; the schema owns the rule."""
    if not is_identifier(value):
        raise ArtifactAdmissionRefused("invalid_identifier")


def _pending_payload(decision: SelectionDecision, committed: int) -> dict[str, Any]:
    """Build and validate the `promotion_pending` payload from a caller's decision."""
    try:
        payload = dict(
            round_id=decision.round_id,
            attempt_id=decision.attempt_id,
            version_id=f"v{committed + 1}",
            candidate_digest=decision.candidate_digest,
            dependency_digest=decision.dependency_digest,
            decision_ref=dict(decision.decision_ref),
        )
        ARTIFACT_FAMILY.validate_payload("promotion_pending", payload)
    except (ValueError, TypeError):
        raise ArtifactAdmissionRefused("invalid_decision") from None
    return payload
