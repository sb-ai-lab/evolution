"""Closed S2 artifact-ledger vocabulary: store identity, event family and lifecycle.

The family owns payload shape only. The lifecycle owns admission over a contiguous
record prefix: what may follow what, which destination is already published, which
workspace may be sealed and which promotion may commit. Neither ever holds bytes of
a copied tree; the ledger stores identifiers, digests and counters only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from evolution.tree.artifacts.artifact_payloads import AuxiliaryDeclaration
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    LifecycleTransition,
    _identifier,
    _integer,
    _keys,
    _require,
)

STORE_PROFILE = "artifact_store"
KINDS = frozenset({"champion", "candidate"})
SOURCES = frozenset({"seed", "workspace", "candidate"})
COPY_FAILURES = frozenset({"storage_capacity_exhausted", "artifact_unsafe", "io_failure"})
STOP_CODES = frozenset({"artifact_corrupted", "ledger_limit_exceeded"})
ABANDON_REASONS = frozenset(
    {"caller_abandoned", "workspace_not_ready", "unsealed_at_recovery", "seal_not_published"}
)
OPERATIONS = frozenset({"artifact_copy", "workspace", "auxiliary_publish"})
# One start event per operation family, each reserving the bytes of its terminal.
STARTS = {
    "artifact_copy_started": "artifact_copy",
    "workspace_opened": "workspace",
    "auxiliary_publish_started": "auxiliary_publish",
}
TERMINALS = {
    "auxiliary_publish": frozenset({"auxiliary_publish_finished", "auxiliary_publish_failed"}),
    "artifact_copy": frozenset({"artifact_copy_finished", "artifact_copy_failed"}),
    "workspace": frozenset({"workspace_ready", "workspace_failed"}),
}
RESERVED_TERMINALS = frozenset(
    {"operation_interrupted"} | {event for events in TERMINALS.values() for event in events}
)
NO_OPERATION_EVENTS = frozenset(
    {
        "store_created",
        "store_rebased",
        "candidate_abandoned",
        "promotion_pending",
        "promotion_committed",
        "store_stopped",
    }
)
_SHA256 = re.compile("[a-f0-9]{64}")
_UNSEALED = frozenset({"opening", "ready", "failed", "sealing"})


def _sha(value: object) -> None:
    _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None)


def _locator(value: object) -> None:
    """A bounded, printable-ASCII relative reference; never a path out of a run."""
    _require(isinstance(value, str))
    assert isinstance(value, str)
    _require(0 < len(value) <= 512)
    _require(all(0x20 <= ord(char) <= 0x7E for char in value))
    _require(not value.startswith("/") and ".." not in value.split("/"))


@dataclass(frozen=True)
class ArtifactStoreIdentityV1:
    """The immutable store identity: the policy itself plus the seed's digests.

    Persisting the policy lets a read-only reader partition any root and recompute
    digests without caller input; it carries patterns and relative paths only.
    """

    policy: ArtifactPolicyV1
    seed_candidate_digest: str
    seed_dependency_digest: str

    def __post_init__(self) -> None:
        for digest in (self.seed_candidate_digest, self.seed_dependency_digest):
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ValueError("seed digest is not sha-256 hex")

    def to_payload(self) -> dict[str, Any]:
        return dict(
            profile=STORE_PROFILE,
            policy=self.policy.to_payload(),
            policy_digest=self.policy.digest,
            seed_candidate_digest=self.seed_candidate_digest,
            seed_dependency_digest=self.seed_dependency_digest,
        )

    @classmethod
    def from_payload(cls, payload: Any) -> ArtifactStoreIdentityV1:
        if not isinstance(payload, dict) or set(payload) != {
            "profile",
            "policy",
            "policy_digest",
            "seed_candidate_digest",
            "seed_dependency_digest",
        }:
            raise ValueError("store identity payload keys")
        if payload["profile"] != STORE_PROFILE:
            raise ValueError("store identity profile")
        policy = ArtifactPolicyV1.from_payload(payload["policy"])
        if policy.digest != payload["policy_digest"]:
            raise ValueError("store identity policy digest")
        return cls(policy, payload["seed_candidate_digest"], payload["seed_dependency_digest"])

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


def _validate_payload(event: str, p: dict[str, Any]) -> None:
    """One closed payload allowlist per event; no other event is admitted."""
    if event == "store_created":
        _keys(p, {"identity", "identity_digest", "producing_version"})
        identity = ArtifactStoreIdentityV1.from_payload(p["identity"])
        _require(identity.digest == p["identity_digest"])
        _identifier(p["producing_version"])
    elif event == "store_rebased":
        # PDR-0010: the campaign rebased its identity under an authorization and
        # re-stamped this ledger with the producing version that resumes it. The
        # store identity itself never changes; only the stamp does.
        _keys(
            p,
            {
                "identity",
                "identity_digest",
                "producing_version",
                "campaign_rebase",
                "authorization_id",
            },
        )
        identity = ArtifactStoreIdentityV1.from_payload(p["identity"])
        _require(identity.digest == p["identity_digest"])
        _identifier(p["producing_version"])
        _sha(p["campaign_rebase"])
        _sha(p["authorization_id"])
    elif event == "auxiliary_publish_started":
        AuxiliaryDeclaration.from_payload(p)
    elif event in ("auxiliary_publish_finished", "auxiliary_publish_failed"):
        _keys(p, {"declaration_digest"} | ({"code"} if event.endswith("failed") else set()))
        _sha(p["declaration_digest"])
        if event.endswith("failed"):
            _require(p["code"] in COPY_FAILURES)
    elif event == "artifact_copy_started":
        _keys(
            p,
            {
                "kind",
                "artifact_id",
                "parent_id",
                "source",
                "candidate_digest",
                "dependency_digest",
                "file_count",
                "byte_count",
            },
        )
        _require(p["kind"] in KINDS and p["source"] in SOURCES)
        _identifier(p["artifact_id"])
        if p["parent_id"] is not None:
            _identifier(p["parent_id"])
        _sha(p["candidate_digest"])
        _sha(p["dependency_digest"])
        _integer(p["file_count"])
        _integer(p["byte_count"])
    elif event == "artifact_copy_finished":
        _keys(p, {"latency_ms"})
        _integer(p["latency_ms"])
    elif event in ("artifact_copy_failed", "workspace_failed"):
        _keys(p, {"code"})
        _require(p["code"] in COPY_FAILURES)
    elif event == "operation_interrupted":
        _keys(p, {"operation"})
        _require(p["operation"] in OPERATIONS)
    elif event == "workspace_opened":
        _keys(p, {"attempt_id", "parent_id"})
        _identifier(p["attempt_id"])
        _identifier(p["parent_id"])
    elif event == "workspace_ready":
        _keys(p, frozenset())
    elif event == "candidate_abandoned":
        _keys(p, {"attempt_id", "reason"})
        _identifier(p["attempt_id"])
        _require(p["reason"] in ABANDON_REASONS)
    elif event in ("promotion_pending", "promotion_committed"):
        required = {
            "round_id",
            "attempt_id",
            "version_id",
            "candidate_digest",
            "dependency_digest",
        }
        _keys(p, required | {"decision_ref"} if event == "promotion_pending" else required)
        _integer(p["round_id"])
        _identifier(p["attempt_id"])
        _identifier(p["version_id"])
        _sha(p["candidate_digest"])
        _sha(p["dependency_digest"])
        if event == "promotion_pending":
            reference = _keys(
                p["decision_ref"], {"version", "kind", "run_id", "locator", "content_digest"}
            )
            _require(type(reference["version"]) is int and reference["version"] == 1)
            _identifier(reference["kind"])
            _identifier(reference["run_id"])
            _locator(reference["locator"])
            _sha(reference["content_digest"])
    elif event == "store_stopped":
        _keys(p, {"code"})
        _require(p["code"] in STOP_CODES)
    else:
        _require(False)


ARTIFACT_FAMILY = EnvelopeFamily(
    _validate_payload,
    NO_OPERATION_EVENTS,
    lambda event: event.endswith("_started") or event == "workspace_opened",
)


class ArtifactLifecycle:
    """Contiguous-prefix indexes with atomic, constant-history-cost transitions.

    Preparing a record does not mutate authoritative indexes. The ledger commits
    its delta only after fsync; a pure reducer prepares and commits immediately.
    """

    def __init__(self) -> None:
        self.sequence = 0
        self.run_id: str | None = None
        self.created: Envelope | None = None
        self.published: dict[str, Envelope] = {}
        # Auxiliary declaration survives failure/interruption; only its exact bytes
        # can finish an owed publication. Tree state and Champion never depend on it.
        self.auxiliary_declared: dict[str, Envelope] = {}
        self.auxiliary_published: dict[str, Envelope] = {}
        self.auxiliary_campaign_run_id: str | None = None
        self.pending: dict[str, Envelope] = {}
        self.operations: set[str] = set()
        self.workspaces: dict[str, str] = {}
        self.workspace_parents: dict[str, str] = {}
        self.promotions: dict[int, Envelope] = {}
        self.committed: list[Envelope] = []
        self.stopped: str | None = None
        self.champion_id: str | None = None
        self._identity: dict[str, Any] = {}

    @property
    def identity(self) -> dict[str, Any]:
        return self._identity

    @property
    def reserved_records(self) -> int:
        # Every open operation keeps its terminal, and a live store keeps one stop.
        return len(self.pending) + int(self.stopped is None)

    def accept(self, record: Envelope) -> None:
        self.prepare(record).commit()

    def prepare(self, record: Envelope) -> LifecycleTransition:
        v = record.to_payload()
        p, event = v["payload"], v["event"]
        sequence = self.sequence
        _require(v["sequence"] == sequence + 1)
        if sequence == 0:
            _require(event == "store_created")
            identity = p["identity"]

            def create() -> None:
                _require(self.sequence == sequence)
                self.created, self.run_id = record, v["run_id"]
                self._identity = identity
                self.sequence += 1

            return LifecycleTransition(1, create)
        _require(v["run_id"] == self.run_id and event != "store_created")
        if event == "store_rebased":
            # The guard is the whole transition: a rebase moves the stamp and no
            # index. Which record a resume proves its version against is read
            # from the snapshot by `ArtifactLedger.latest_store_record`, over the
            # records that resume actually holds -- one reading of the stamp
            # rather than a cached one this reducer would have to keep agreeing
            # with.
            _require(self.stopped is None and p["identity"] == self._identity)
            return LifecycleTransition(
                self.reserved_records, self._sequenced(sequence, lambda: None)
            )
        if event == "store_stopped":
            _require(self.stopped is None)
            code = p["code"]

            def stop() -> None:
                _require(self.sequence == sequence)
                self.stopped = code
                self.sequence += 1

            return LifecycleTransition(len(self.pending), stop)
        op = v["operation_id"]
        if op is None:
            return self._prepare_unpaired(record, event, p, sequence)
        if event in STARTS:
            return self._prepare_start(record, event, p, op, sequence)
        return self._prepare_terminal(record, event, p, op, sequence)

    def _prepare_unpaired(
        self, record: Envelope, event: str, p: dict[str, Any], sequence: int
    ) -> LifecycleTransition:
        """`candidate_abandoned` and the two promotion records carry no operation."""
        _require(self.stopped is None)
        attempt = p["attempt_id"]
        apply: Callable[[], None]
        if event == "candidate_abandoned":
            _require(self.workspaces.get(attempt) in _UNSEALED)

            def abandon() -> None:
                self.workspaces[attempt] = "abandoned"

            apply = abandon
        elif event == "promotion_pending":
            round_id, version_id = p["round_id"], p["version_id"]
            published = self.published.get(f"candidate/{attempt}")
            _require(published is not None)
            assert published is not None
            for name in ("candidate_digest", "dependency_digest"):
                _require(p[name] == published.payload[name])
            _require(round_id not in self.promotions)
            _require(all(e.payload["round_id"] != round_id for e in self.committed))
            _require(all(e.payload["version_id"] != version_id for e in self.promotions.values()))
            # The champion chain is dense: the next promotion is always v<committed+1>.
            _require(version_id == f"v{len(self.committed) + 1}")

            def pend() -> None:
                self.promotions[round_id] = record

            apply = pend
        else:
            _require(event == "promotion_committed")
            round_id, version_id = p["round_id"], p["version_id"]
            promotion = self.promotions.get(round_id)
            _require(promotion is not None)
            assert promotion is not None
            for name in ("version_id", "attempt_id", "candidate_digest", "dependency_digest"):
                _require(p[name] == promotion.payload[name])
            _require(f"champion/{version_id}" in self.published)

            def commit() -> None:
                del self.promotions[round_id]
                self.committed.append(record)
                self.champion_id = version_id
                if attempt in self.workspaces:
                    self.workspaces[attempt] = "promoted"

            apply = commit
        return LifecycleTransition(self.reserved_records, self._sequenced(sequence, apply))

    def _prepare_start(
        self, record: Envelope, event: str, p: dict[str, Any], op: str, sequence: int
    ) -> LifecycleTransition:
        _require(self.stopped is None)
        _require(op not in self.operations)
        apply: Callable[[], None] | None = None
        if event == "auxiliary_publish_started":
            declaration = AuxiliaryDeclaration.from_payload(p)
            destination = declaration.locator
            _require(destination not in self.auxiliary_published)
            _require(all(self._destination(e) != destination for e in self.pending.values()))
            original = self.auxiliary_declared.get(destination)
            _require(original is None or original.payload == p)
            _require(self.auxiliary_campaign_run_id in (None, declaration.declaration_ref.run_id))

            def declare() -> None:
                self.auxiliary_declared.setdefault(destination, record)
                self.auxiliary_campaign_run_id = declaration.declaration_ref.run_id

            apply = declare
        elif event == "workspace_opened":
            attempt, parent = p["attempt_id"], p["parent_id"]
            _require(attempt not in self.workspaces)
            # Every attempt starts from the frozen champion, so no workspace exists
            # before `champion/v0` is published and none names another parent.
            _require(parent == self.champion_id)

            def open_workspace() -> None:
                self.workspaces[attempt] = "opening"
                self.workspace_parents[attempt] = parent

            apply = open_workspace
        else:
            artifact_id, source = p["artifact_id"], p["source"]
            destination = f"{p['kind']}/{artifact_id}"
            _require(destination not in self.published)
            _require(all(self._destination(e) != destination for e in self.pending.values()))
            if source == "seed":
                # A `v0` copy may only claim the seed identity `store_created` proved.
                _require(p["kind"] == "champion" and artifact_id == "v0")
                _require(p["parent_id"] is None)
                _require(p["candidate_digest"] == self._identity["seed_candidate_digest"])
                _require(p["dependency_digest"] == self._identity["seed_dependency_digest"])
            elif source == "workspace":
                _require(p["kind"] == "candidate")
                _require(self.workspaces.get(artifact_id) in ("ready", "sealing"))
                _require(p["parent_id"] == self.workspace_parents.get(artifact_id))

                def seal() -> None:
                    self.workspaces[artifact_id] = "sealing"

                apply = seal
            else:
                # A champion copy from a Candidate needs its promotion already durable.
                _require(p["kind"] == "champion")
                _require(
                    any(
                        e.payload["version_id"] == artifact_id
                        and e.payload["attempt_id"] == p["parent_id"]
                        and e.payload["candidate_digest"] == p["candidate_digest"]
                        and e.payload["dependency_digest"] == p["dependency_digest"]
                        for e in self.promotions.values()
                    )
                )

        def start() -> None:
            self.operations.add(op)
            self.pending[op] = record
            if apply is not None:
                apply()

        return LifecycleTransition(len(self.pending) + 2, self._sequenced(sequence, start))

    def _prepare_terminal(
        self, record: Envelope, event: str, p: dict[str, Any], op: str, sequence: int
    ) -> LifecycleTransition:
        """A terminal is reserved: it is admitted even after a durable stop."""
        _require(op in self.pending)
        start = self.pending[op]
        started = start.payload
        family = STARTS[start.event]
        if event == "operation_interrupted":
            _require(p["operation"] == family)
        else:
            _require(event in TERMINALS[family])
            for name, value in started.items():
                if name in p:
                    _require(p[name] == value)
        apply: Callable[[], None] | None = None
        if event in ("auxiliary_publish_finished", "auxiliary_publish_failed"):
            declaration = AuxiliaryDeclaration.from_payload(dict(started))
            _require(p["declaration_digest"] == declaration.digest)
            if event == "auxiliary_publish_finished":
                _require(declaration.locator not in self.auxiliary_published)

                def publish_auxiliary() -> None:
                    self.auxiliary_published[declaration.locator] = start

                apply = publish_auxiliary
        elif event == "artifact_copy_finished":
            destination = f"{started['kind']}/{started['artifact_id']}"
            _require(destination not in self.published)
            kind, artifact_id = started["kind"], started["artifact_id"]
            # An abandoned attempt is never sealed, whatever order recovery took.
            if kind == "candidate":
                _require(self.workspaces.get(artifact_id) != "abandoned")

            def publish() -> None:
                self.published[destination] = start
                if kind == "champion" and artifact_id == "v0":
                    self.champion_id = "v0"
                elif kind == "candidate":
                    self.workspaces[artifact_id] = "sealed"

            apply = publish
        elif event in ("workspace_ready", "workspace_failed"):
            attempt, state = started["attempt_id"], event.removeprefix("workspace_")

            def settle() -> None:
                self.workspaces[attempt] = state

            apply = settle

        def close() -> None:
            del self.pending[op]
            if apply is not None:
                apply()

        return LifecycleTransition(
            len(self.pending) - 1 + int(self.stopped is None), self._sequenced(sequence, close)
        )

    @staticmethod
    def _destination(start: Envelope) -> str | None:
        p = start.payload
        if start.event == "auxiliary_publish_started":
            return AuxiliaryDeclaration.from_payload(dict(p)).locator
        return f"{p['kind']}/{p['artifact_id']}" if start.event == "artifact_copy_started" else None

    def _sequenced(self, sequence: int, apply: Callable[[], None]) -> Callable[[], None]:
        def commit() -> None:
            _require(self.sequence == sequence)
            apply()
            self.sequence += 1

        return commit
