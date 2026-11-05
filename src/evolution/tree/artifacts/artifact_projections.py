"""Pure S2 lineage projection over one artifact ledger snapshot.

The projection exposes only what the ledger observed: the ordered champion chain,
the Candidate list with its lifecycle state, the current champion and the store
state. Whether a sealed Candidate lost a round is a fact of the campaign journal,
so nothing here labels a non-winner. Locators are run-relative, never a
filesystem path, so a projection is safe to serialize and to ship out of the run. Reducing a snapshot never writes, never takes the ledger lease and
never authorizes a promotion; `promotion_committed` alone asserts one.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError, ArtifactUnsafeError
from evolution.tree.artifacts.artifact_payloads import (
    AuxiliaryDeclaration,
    AuxiliarySourceReference,
    verify_directory,
)
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, entry_set, scan_tree
from evolution.tree.artifacts.artifact_schema import ArtifactLifecycle
from evolution.tree.storage.append_log import Snapshot
from evolution.tree.storage.envelope import _keys, is_identifier
from evolution.tree.storage.envelope import _require as _publication_require

CHAMPION_LOCATOR = "artifacts/champions/"
CANDIDATE_LOCATOR = "artifacts/candidates/"
STORE_STATES = ("initializing", "active", "stopped")
CANDIDATE_STATES = ("opening", "editing", "sealed", "abandoned", "promoted", "failed")
CORRUPTION_CODES = (
    "artifact_missing",
    "artifact_unsafe",
    "artifact_digest_mismatch",
    # A transient read failure on the copy, not a claim about its bytes: the reader
    # retries rather than treating it as corruption.
    "unreadable",
)
StoreState = Literal["initializing", "active", "stopped"]
CandidateState = Literal["opening", "editing", "sealed", "abandoned", "promoted", "failed"]
# The ledger's workspace vocabulary mapped onto the six S2-observed Candidate states.
_STATE_OF: dict[str, CandidateState] = {
    "opening": "opening",
    "ready": "editing",
    "sealing": "editing",
    "sealed": "sealed",
    "abandoned": "abandoned",
    "promoted": "promoted",
    "failed": "failed",
}
_SHA256 = re.compile("[a-f0-9]{64}")

__all__ = [
    "AuxiliaryPublication",
    "project_auxiliary",
    "inspect_auxiliary",
    "ArtifactReference",
    "CandidateEntry",
    "ChampionEntry",
    "LineageProjection",
    "corruption_code",
    "project_lineage",
]


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid artifact projection")


def _identifier(value: Any) -> None:
    _require(type(value) is str and is_identifier(value))


def _sha(value: Any) -> None:
    _require(type(value) is str and _SHA256.fullmatch(value) is not None)


def corruption_code(root: Path) -> str:
    """A published root that cannot be scanned: absent, or present but unsafe."""
    return "artifact_unsafe" if os.path.lexists(root) else "artifact_missing"


@dataclass(frozen=True)
class ArtifactReference:
    """Version 1 artifact identity and location reference.

    It proves identity and location only, never execution, validation or promotion.
    The locator is run-relative and its tail is a validated identifier, so joining
    it onto a run root can never leave that root.
    """

    version: Literal[1]
    kind: Literal["artifact"]
    run_id: str
    locator: str
    content_digest: str
    dependency_digest: str

    def __post_init__(self) -> None:
        _require(type(self.version) is int and self.version == 1)
        _require(self.kind == "artifact")
        _identifier(self.run_id)
        _require(type(self.locator) is str)
        prefixes = [p for p in (CHAMPION_LOCATOR, CANDIDATE_LOCATOR) if self.locator.startswith(p)]
        _require(len(prefixes) == 1)
        _identifier(self.locator[len(prefixes[0]) :])
        _sha(self.content_digest)
        _sha(self.dependency_digest)


@dataclass(frozen=True)
class ChampionEntry:
    """One link of the champion chain. `v0` has no attempt, round or decision."""

    version_id: str
    attempt_id: str | None
    round_id: int | None
    candidate_digest: str
    dependency_digest: str
    decision_ref: dict[str, Any] | None
    reference: ArtifactReference
    corruption: str | None

    def __post_init__(self) -> None:
        _identifier(self.version_id)
        _require(self.attempt_id is None or is_identifier(self.attempt_id))
        _require(self.round_id is None or type(self.round_id) is int)
        _require((self.attempt_id is None) == (self.round_id is None))
        _sha(self.candidate_digest)
        _sha(self.dependency_digest)
        _require(self.decision_ref is None or type(self.decision_ref) is dict)
        _require(self.reference.locator == CHAMPION_LOCATOR + self.version_id)
        _require(self.reference.content_digest == self.candidate_digest)
        _require(self.corruption is None or self.corruption in CORRUPTION_CODES)


@dataclass(frozen=True)
class CandidateEntry:
    """One attempt. Digests and a reference exist only once its copy is published."""

    attempt_id: str
    parent_id: str
    state: CandidateState
    candidate_digest: str | None
    dependency_digest: str | None
    reference: ArtifactReference | None
    corruption: str | None

    def __post_init__(self) -> None:
        _identifier(self.attempt_id)
        _identifier(self.parent_id)
        _require(self.state in CANDIDATE_STATES)
        published = self.reference is not None
        _require(published == (self.candidate_digest is not None))
        _require(published == (self.dependency_digest is not None))
        if self.reference is not None:
            _require(self.reference.locator == CANDIDATE_LOCATOR + self.attempt_id)
            _require(self.reference.content_digest == self.candidate_digest)
        else:
            _require(self.corruption is None)
        _require(self.corruption is None or self.corruption in CORRUPTION_CODES)


@dataclass(frozen=True)
class LineageProjection:
    """A versioned public view carrying the S1 `consistency` provenance."""

    run_id: str
    consistency: Literal["observed", "durable"]
    sequence: int
    prefix_digest: str
    store_state: StoreState
    stopped_code: str | None
    champion: ChampionEntry | None
    champions: tuple[ChampionEntry, ...]
    candidates: tuple[CandidateEntry, ...]

    def __post_init__(self) -> None:
        _identifier(self.run_id)
        _require(self.consistency in ("observed", "durable"))
        _require(type(self.sequence) is int and self.sequence > 0)
        _sha(self.prefix_digest)
        _require(self.store_state in STORE_STATES)
        _require((self.store_state == "stopped") == (self.stopped_code is not None))
        _require(type(self.champions) is tuple and type(self.candidates) is tuple)
        # `initializing` is the prefix in which `champion/v0` is not yet published;
        # a store stopped in that prefix keeps an empty chain and says so instead.
        _require(not (self.store_state == "initializing" and self.champions))
        _require(not (self.store_state == "active" and not self.champions))
        _require(self.champion == (self.champions[-1] if self.champions else None))


def _reference(
    run_id: str, prefix: str, artifact_id: str, payload: dict[str, Any]
) -> ArtifactReference:
    return ArtifactReference(
        1,
        "artifact",
        run_id,
        prefix + artifact_id,
        payload["candidate_digest"],
        payload["dependency_digest"],
    )


def _corruption(
    run_root: Path, policy: ArtifactPolicyV1, reference: ArtifactReference
) -> str | None:
    """Recompute one published copy's digests. Corruption is reported, never raised.

    Recovery does not verify a non-winning sealed Candidate, so this is
    the only site that proves a published copy still is what the ledger published.
    An unreadable copy is reported as `unreadable`, never as one of the three
    corruption codes: inspection must not invent corruption for a transient error.
    """
    root = run_root / reference.locator
    try:
        scan = scan_tree(root, policy)
        entries = entry_set(root)
    except ArtifactUnsafeError:
        return corruption_code(root)
    except OSError:
        return "unreadable"
    # The digests cover only policy-included entries, so an excluded entry added
    # inside a published copy is invisible to them. Such an entry is forbidden, and the
    # whole on-disk entry set is what sees it.
    if entries != {e.path for e in scan.artifact + scan.dependency}:
        return "artifact_unsafe"
    if (
        scan.candidate_digest != reference.content_digest
        or scan.dependency_digest != reference.dependency_digest
    ):
        return "artifact_digest_mismatch"
    return None


def project_lineage(
    snapshot: Snapshot,
    *,
    run_root: Path | None = None,
    policy: ArtifactPolicyV1 | None = None,
    verify: bool = False,
) -> LineageProjection:
    """Reduce a ledger snapshot to observed artifact publication state.

    `verify=True` recomputes the digests of every published copy against the policy
    the store persisted, and reports the outcome per copy; it needs `run_root` and
    reports rather than raises, because a corrupt non-winner never stops a store.
    """
    life = ArtifactLifecycle()
    for record in snapshot.records:
        life.accept(record)
    _require(life.created is not None and life.run_id is not None)
    run_id = str(life.run_id)
    root: Path | None = None
    if verify:
        _require(run_root is not None)
        assert run_root is not None
        root = Path(run_root)
        if policy is None:
            policy = ArtifactPolicyV1.from_payload(life.identity["policy"])

    def corruption(reference: ArtifactReference) -> str | None:
        if root is None or policy is None:
            return None
        return _corruption(root, policy, reference)

    # `promotion_committed` does not carry the decision; its round's pending does.
    decisions = {
        record.payload["round_id"]: dict(record.payload["decision_ref"])
        for record in snapshot.records
        if record.event == "promotion_pending"
    }

    def champion_entry(
        version_id: str, attempt_id: str | None, round_id: int | None
    ) -> ChampionEntry:
        payload = life.published[f"champion/{version_id}"].payload
        reference = _reference(run_id, CHAMPION_LOCATOR, version_id, payload)
        return ChampionEntry(
            version_id,
            attempt_id,
            round_id,
            reference.content_digest,
            reference.dependency_digest,
            None if round_id is None else decisions.get(round_id),
            reference,
            corruption(reference),
        )

    champions: list[ChampionEntry] = []
    if "champion/v0" in life.published:
        champions.append(champion_entry("v0", None, None))
        for record in life.committed:
            p = record.payload
            champions.append(champion_entry(p["version_id"], p["attempt_id"], p["round_id"]))

    candidates: list[CandidateEntry] = []
    for attempt, workspace_state in life.workspaces.items():
        published = life.published.get(f"candidate/{attempt}")
        reference = (
            None
            if published is None
            else _reference(run_id, CANDIDATE_LOCATOR, attempt, published.payload)
        )
        candidates.append(
            CandidateEntry(
                attempt,
                life.workspace_parents[attempt],
                _STATE_OF[workspace_state],
                None if reference is None else reference.content_digest,
                None if reference is None else reference.dependency_digest,
                reference,
                None if reference is None else corruption(reference),
            )
        )

    store_state: StoreState
    if life.stopped is not None:
        store_state, stopped_code = "stopped", life.stopped
    else:
        store_state = "active" if champions else "initializing"
        stopped_code = None
    return LineageProjection(
        run_id,
        snapshot.consistency,
        snapshot.sequence,
        snapshot.prefix_digest,
        store_state,
        stopped_code,
        champions[-1] if champions else None,
        tuple(champions),
        tuple(candidates),
    )


@dataclass(frozen=True)
class AuxiliaryPublication:
    declaration: AuxiliaryDeclaration
    publication_ref: AuxiliarySourceReference

    def __post_init__(self) -> None:
        _publication_require(isinstance(self.declaration, AuxiliaryDeclaration))
        _publication_require(isinstance(self.publication_ref, AuxiliarySourceReference))
        _publication_require(self.publication_ref.kind == "ledger_record")

    @property
    def locator(self) -> str:
        return self.declaration.locator

    def to_payload(self) -> dict[str, Any]:
        return dict(
            declaration=self.declaration.to_payload(),
            publication_ref=self.publication_ref.to_payload(),
        )

    @classmethod
    def from_payload(cls, value: Any) -> AuxiliaryPublication:
        p = _keys(value, {"declaration", "publication_ref"})
        return cls(
            AuxiliaryDeclaration.from_payload(p["declaration"]),
            AuxiliarySourceReference.from_payload(p["publication_ref"]),
        )

    def verify(self, run_root: Path) -> None:
        """Reject foreign/stale ledger refs before reading their claimed content."""
        if self not in inspect_auxiliary(run_root):
            raise ArtifactCorruptionError("artifact_digest_mismatch")
        verify_directory(run_root / self.locator, self.declaration, committed=True)


def project_auxiliary(snapshot: Snapshot) -> tuple[AuxiliaryPublication, ...]:
    """Pure ledger projection. The start carries identity; terminal owns visibility."""
    from evolution.tree.artifacts.artifact_schema import ArtifactLifecycle

    if not snapshot.complete:
        raise ArtifactCorruptionError("artifact_unsafe")
    life = ArtifactLifecycle()
    declarations: dict[str, AuxiliaryDeclaration] = {}
    published = []
    offset = 0
    for record in snapshot.records:
        life.accept(record)
        raw = record.encode()
        if record.event == "auxiliary_publish_started":
            declarations[str(record.operation_id)] = AuxiliaryDeclaration.from_payload(
                dict(record.payload)
            )
        elif record.event == "auxiliary_publish_finished":
            published.append(
                AuxiliaryPublication(
                    declarations[str(record.operation_id)],
                    AuxiliarySourceReference(
                        1,
                        "ledger_record",
                        str(record.to_payload()["run_id"]),
                        f"artifacts.jsonl#bytes={offset}:{offset + len(raw)}",
                        hashlib.sha256(raw).hexdigest(),
                    ),
                )
            )
        offset += len(raw)
    return tuple(published)


def inspect_auxiliary(run_root: Path, *, verify: bool = False) -> tuple[AuxiliaryPublication, ...]:
    """Read-only publication inspection; never creates a path or takes a lease."""
    from evolution.tree.artifacts.artifact_ledger import inspect_ledger

    publications = project_auxiliary(inspect_ledger(run_root))
    if verify:
        for publication in publications:
            verify_directory(
                run_root / publication.locator, publication.declaration, committed=True
            )
    return publications
