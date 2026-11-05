"""Interaction for tree execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar, get_args

from evolution.tree.execution.candidate_integrity import (
    ArtifactError,
    SnapshotError,
    artifact_digest,
)
from evolution.tree.execution.sample_outcomes import (
    SampleDisposition,
    Terminal,
    disposition_for,
)
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import validate_turn_provenance, validate_turn_reason

SeedMode = Literal["exact", "unpaired", "none"]


MaterializationStrategy = Literal[
    "adapter_apply_manifest", "filesystem_overlay", "artifact_root_injection"
]


class CandidateIntegrityError(RuntimeError):
    """A read-only Candidate view no longer matches its sealed digest."""


class RolloutInfrastructureError(RuntimeError):
    """The target boundary failed outside Candidate-controlled behavior."""

    def __init__(
        self,
        message: str = "",
        code: str = "infrastructure",
        *,
        retryable: bool = False,
        primary_failure: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.primary_failure = primary_failure


AttestationCode = Literal["attestation_missing", "attestation_mismatch", "strategy_unsupported"]


#: The closed codes an `AttestationError` carries: what a refused
#: materialization's `target_materialize_failed` records as `error_code`.
ATTESTATION_CODES: frozenset[str] = frozenset(get_args(AttestationCode))


class AttestationError(RolloutInfrastructureError):
    """A `RolloutInfrastructureError` with a closed attestation failure code."""

    def __init__(self, message: str, code: AttestationCode) -> None:
        super().__init__(message, code)


class RolloutTargetUnstable(RuntimeError):
    """A transient target-call failure exhausted its attempts.

    Indeterminate, not a fault: `durable_rollout` closes the sample
    `rollout_failed(target_failed)` -- owned by `target`, reason
    `target_unstable` (ADR-0019) -- and the retry, threshold and
    baseline-exclusion machinery applies to it exactly as it does to a
    target-reported `target_failed` turn outcome. Raised only when
    `budget.call_retries is not None`; every earlier manifest still raises
    `RolloutInfrastructureError` for the same failure.
    """

    def __init__(self, message: str = "", code: str = "target_unstable") -> None:
        super().__init__(message)
        self.code = code


class RolloutEnvironmentError(RuntimeError):
    """The Environment boundary failed while producing the next message."""


class RolloutRecoveryError(RuntimeError):
    """Durable rollout evidence is malformed or ambiguous."""


class RolloutTimeBudgetExceeded(TimeoutError):
    """The core deadline expired before a valid Trajectory could be produced."""

    terminal: Literal["time_budget"] = "time_budget"
    disposition: SampleDisposition = disposition_for("time_budget")


@dataclass(frozen=True)
class AdapterCapabilities:
    seed_mode: SeedMode
    max_concurrent_sessions: int
    instantiable: bool
    materialization: frozenset[MaterializationStrategy]
    max_instances: int = 1

    def __post_init__(self) -> None:
        if self.seed_mode not in {"exact", "unpaired", "none"}:
            raise ValueError("adapter seed_mode is invalid")
        if (
            not isinstance(self.max_concurrent_sessions, int)
            or isinstance(self.max_concurrent_sessions, bool)
            or self.max_concurrent_sessions < 1
        ):
            raise ValueError("max_concurrent_sessions must be positive")
        if type(self.instantiable) is not bool:
            raise ValueError("instantiable must be a boolean")
        materialization = frozenset(self.materialization)
        if not materialization <= {
            "adapter_apply_manifest",
            "filesystem_overlay",
            "artifact_root_injection",
        }:
            raise ValueError("adapter materialization strategy is invalid")
        if (
            not isinstance(self.max_instances, int)
            or isinstance(self.max_instances, bool)
            or self.max_instances < 1
        ):
            raise ValueError("max_instances must be positive")
        object.__setattr__(self, "materialization", materialization)


@dataclass(frozen=True)
class Message:
    content: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("message content must be a string")
        object.__setattr__(self, "fields", _frozen_mapping(self.fields))

    def to_payload(self) -> dict[str, Any]:
        return {"content": self.content, "fields": _payload(self.fields)}


@dataclass(frozen=True)
class Turn:
    index: int
    message: Message
    events: tuple[Mapping[str, Any], ...]
    usage: Usage | None
    latency_ms: int
    outcome: Literal["ok", "target_failed", "candidate_failed"]
    reason: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, Message):
            raise ValueError("turn message must be a Message")
        if self.usage is not None and not isinstance(self.usage, Usage):
            raise ValueError("turn usage must be Usage or None")
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 0:
            raise ValueError("turn index must be a nonnegative integer")
        if (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("turn latency_ms must be a nonnegative integer")
        if self.outcome not in {"ok", "target_failed", "candidate_failed"}:
            raise ValueError("turn outcome is invalid")
        events = tuple(_frozen_mapping(event) for event in self.events)
        if any(
            not isinstance(event.get("type"), str) or not str(event["type"]).strip()
            for event in events
        ):
            raise ValueError("every event type must be a nonblank string")
        object.__setattr__(self, "events", events)
        # The wire validator owns the reason grammar (PDR-0014).
        # The validated *copy* is what this frozen turn keeps, exactly as
        # `events` keeps its frozen mappings above: a producer that still
        # holds the dict it passed cannot change what was validated.
        object.__setattr__(self, "reason", validate_turn_reason(self.reason))

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "message": self.message.to_payload(),
            "events": [_payload(event) for event in self.events],
            "usage": self.usage.to_payload() if self.usage else None,
            "latency_ms": self.latency_ms,
            "outcome": self.outcome,
            "reason": dict(self.reason) if self.reason else None,
        }


@dataclass(frozen=True)
class TurnResult:
    events: tuple[Mapping[str, Any], ...]
    usage: Usage | None
    latency_ms: int
    outcome: Literal["ok", "target_failed", "candidate_failed"]
    reason: Mapping[str, str] | None = None
    #: What the agent had in context on this turn (ADR-0024, G1). Carried to
    #: the caller only: `Turn` does not hold it, so no rollout record of
    #: families 5-10 changes. Family 12 decides where it is persisted.
    provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        validated = Turn(
            0, Message(""), self.events, self.usage, self.latency_ms, self.outcome, self.reason
        )
        object.__setattr__(self, "events", validated.events)
        # `Turn` detached the reason while validating it; keep that copy, so a
        # backend that mutates the dict it put in its own `TurnResult` cannot
        # change what the server is about to serialize into the turn body.
        object.__setattr__(self, "reason", validated.reason)
        object.__setattr__(self, "provenance", validate_turn_provenance(self.provenance))


@dataclass(frozen=True)
class History:
    turns: tuple[Turn, ...] = ()

    def __post_init__(self) -> None:
        turns = tuple(self.turns)
        if any(turn.index != index for index, turn in enumerate(turns)):
            raise ValueError("turn indexes must be contiguous from zero")
        object.__setattr__(self, "turns", turns)

    def to_payload(self) -> dict[str, Any]:
        return {"turns": [_payload(turn) for turn in self.turns]}


@dataclass(frozen=True)
class EnvironmentView:
    case_id: str
    public: Mapping[str, Any]
    private: Mapping[str, Any]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    group_id: str
    stratum: str
    tags: tuple[str, ...]
    public: Mapping[str, Any]
    private: Mapping[str, Any]
    oracle: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_nonblank("case_id", self.case_id)
        _require_nonblank("group_id", self.group_id)
        _require_nonblank("stratum", self.stratum)
        tags = tuple(self.tags)
        if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("tags must contain nonblank strings")
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "public", _frozen_mapping(self.public))
        object.__setattr__(self, "private", _frozen_mapping(self.private))
        object.__setattr__(self, "oracle", _frozen_mapping(self.oracle))

    def environment_view(self) -> EnvironmentView:
        return EnvironmentView(self.case_id, self.public, self.private)


@dataclass(frozen=True)
class SemanticIdentity:
    contract_digest: str
    dependency_digest: str
    settings_hash: str
    seed_mode: SeedMode
    protocol_version: str
    runtime_ref_digest: str

    def __post_init__(self) -> None:
        for name in (
            "contract_digest",
            "dependency_digest",
            "settings_hash",
            "protocol_version",
            "runtime_ref_digest",
        ):
            _require_nonblank(name, getattr(self, name))
        if self.seed_mode not in {"exact", "unpaired", "none"}:
            raise ValueError("seed_mode is invalid")


@dataclass(frozen=True)
class InstanceProvenance:
    boot_id: str
    loaded_candidate_digest: str

    def __post_init__(self) -> None:
        _require_nonblank("boot_id", self.boot_id)
        _require_nonblank("loaded_candidate_digest", self.loaded_candidate_digest)


_SEMANTIC_FIELDS: tuple[str, ...] = (
    "contract_digest",
    "dependency_digest",
    "settings_hash",
    "seed_mode",
    "protocol_version",
    "runtime_ref_digest",
)


_INSTANCE_FIELDS: tuple[str, ...] = ("boot_id", "loaded_candidate_digest")


def _exact_keys(value: object, names: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value.keys()) != set(names):
        raise ValueError("identity payload keys do not match the expected set")
    return value


@dataclass(frozen=True)
class TargetIdentity:
    semantic: SemanticIdentity
    instance: InstanceProvenance
    attested: frozenset[str]

    def __post_init__(self) -> None:
        attested = frozenset(self.attested)
        if any(not isinstance(item, str) or not item.strip() for item in attested):
            raise ValueError("attested fields must not be blank")
        object.__setattr__(self, "attested", attested)

    def to_payload(self) -> dict[str, Any]:
        """The durable shape the campaign writes into `instance_attested`.

        `attested` is a sorted list, so one identity always serializes to the
        same bytes and two runs of the same manifest write the same record.
        """

        return {
            "semantic": {name: getattr(self.semantic, name) for name in _SEMANTIC_FIELDS},
            "instance": {name: getattr(self.instance, name) for name in _INSTANCE_FIELDS},
            "attested": sorted(self.attested),
        }

    @classmethod
    def from_payload(cls, payload: object) -> TargetIdentity:
        """Rebuild what `to_payload` wrote; anything else is a `ValueError`.

        Strict on purpose: the payload is read back out of a journal whose writer
        C1 does not own, so an unknown key, a missing one, a repeated `attested`
        field or a non-string member is refused rather than quietly dropped —
        the verifier that runs over the result must not be fed a guess.
        """

        body = _exact_keys(payload, ("semantic", "instance", "attested"))
        semantic = _exact_keys(body["semantic"], _SEMANTIC_FIELDS)
        instance = _exact_keys(body["instance"], _INSTANCE_FIELDS)
        attested = body["attested"]
        if not isinstance(attested, list | tuple) or any(
            not isinstance(item, str) for item in attested
        ):
            raise ValueError("attested must be a list of strings")
        if len(set(attested)) != len(attested):
            raise ValueError("attested must not repeat a field")
        return cls(
            SemanticIdentity(**{name: semantic[name] for name in _SEMANTIC_FIELDS}),
            InstanceProvenance(**{name: instance[name] for name in _INSTANCE_FIELDS}),
            frozenset(attested),
        )


@dataclass(frozen=True)
class EnvironmentIdentity:
    kind: str
    digest: str

    def __post_init__(self) -> None:
        _require_nonblank("environment kind", self.kind)
        _require_nonblank("environment digest", self.digest)


class Environment(Protocol):
    identity: EnvironmentIdentity

    async def next(self, case: EnvironmentView, history: History) -> Message | None: ...


class TargetSession(Protocol):
    session_id: str
    requested_seed: int | None
    effective_seed: int | None


class TargetInstance(Protocol):
    instance_id: str

    async def identity(self) -> TargetIdentity: ...

    async def open(
        self, case_public: Mapping[str, Any], seed: int | None, request_id: str
    ) -> TargetSession: ...

    async def send(
        self, session: TargetSession, message: Message, request_id: str
    ) -> TurnResult: ...

    async def close(self, session: TargetSession) -> None: ...


class DescribingTargetInstance(Protocol):
    """The optional `describe()`-derived member an instance may carry (PDR-0014).

    A sibling of `TargetInstance`, not a member of it: a `Protocol` member is
    required by mypy even with a default value -- a defaulted attribute still
    has to be *there*, so putting `description` on `TargetInstance` itself
    would fail every adapter whose instance class declares nothing, which is
    exactly the break this record fixes (`target_server.DescribingInstance`
    applies the identical reasoning on the backend side). Detection stays
    structural: every reader (`journal_rollout._BoundInstance`,
    `campaign_measurement.py`) uses `getattr(instance, "description", None)`,
    so a `TargetInstance` is never required to satisfy `isinstance(instance,
    DescribingTargetInstance)`.

    What it carries: the backend's optional `describe()` published on *this
    instance's* creation body, or `None` from an adapter that reports none.
    It is operator-facing provenance, never identity -- captured once at
    materialization, never compared, and never re-read from a later
    `identity()` call, since a value two answers could be compared across
    would be identity by another name (ADR-0007).
    """

    description: Mapping[str, Any] | None


CandidateRefT = TypeVar("CandidateRefT", contravariant=True)


MaterializedCandidateT = TypeVar("MaterializedCandidateT", bound="CandidateReference")


class CandidateReference(Protocol):
    digest: str


class TargetAdapter(Protocol[CandidateRefT]):
    capabilities: AdapterCapabilities

    async def materialize(
        self, candidate: CandidateRefT, strategy: MaterializationStrategy, instance_id: str
    ) -> TargetInstance: ...

    async def release(self, instance_id: str) -> None: ...


class RolloutJournal(Protocol):
    def append(self, event: dict[str, Any]) -> int: ...


@dataclass(frozen=True)
class ScorerIdentity:
    kind: str
    digest: str

    def __post_init__(self) -> None:
        _require_nonblank("scorer kind", self.kind)
        _require_nonblank("scorer digest", self.digest)


@dataclass(frozen=True)
class Provenance:
    target: TargetIdentity
    requested_seed: int | None
    effective_seed: int | None

    def __post_init__(self) -> None:
        mode = self.target.semantic.seed_mode
        if mode == "exact":
            if (
                not isinstance(self.requested_seed, int)
                or isinstance(self.requested_seed, bool)
                or not isinstance(self.effective_seed, int)
                or isinstance(self.effective_seed, bool)
                or self.effective_seed != self.requested_seed
            ):
                raise ValueError("exact seed provenance must confirm the requested seed")
        elif mode == "unpaired":
            if self.requested_seed is not None:
                raise ValueError("unpaired seed provenance must not claim a requested seed")
            _validate_optional_seed("effective_seed", self.effective_seed)
        elif self.requested_seed is not None or self.effective_seed is not None:
            raise ValueError("seedless provenance must not claim a seed")


@dataclass(frozen=True)
class SampleIdentity:
    case_id: str
    repeat: int
    candidate_digest: str
    requested_seed: int | None
    effective_seed: int | None

    def __post_init__(self) -> None:
        _require_nonblank("case_id", self.case_id)
        _require_nonblank("candidate_digest", self.candidate_digest)
        if not isinstance(self.repeat, int) or isinstance(self.repeat, bool) or self.repeat < 1:
            raise ValueError("repeat must be a positive integer")
        _validate_optional_seed("requested_seed", self.requested_seed)
        _validate_optional_seed("effective_seed", self.effective_seed)


@dataclass(frozen=True)
class CandidateView:
    """A frozen Candidate root and the digest its bytes must still reproduce.

    Two digest strategies share one view. Without a `verifier` the digest is
    `artifact_digest` over an explicit `artifact_paths` list. With a `verifier`
    the strategy decides membership itself (as the S2 artifact-set digest does),
    so the path list is empty and the callable is the only thing that reads the
    tree.
    """

    root: Path
    digest: str
    artifact_paths: tuple[str, ...]
    verifier: Callable[[Path], str] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        try:
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Candidate root is invalid: {exc}") from exc
        if not root.is_dir():
            raise ValueError("Candidate root must be a directory")
        _require_nonblank("Candidate digest", self.digest)
        paths = tuple(self.artifact_paths)
        if len(set(paths)) != len(paths) or any(not path for path in paths):
            raise ValueError("artifact_paths must be unique nonblank paths")
        if not paths and self.verifier is None:
            raise ValueError("artifact_paths is required without a verifier")
        if self.verifier is not None and not callable(self.verifier):
            raise ValueError("verifier must be callable")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "artifact_paths", paths)

    def verify(self) -> None:
        try:
            actual = (
                artifact_digest(self.root, self.artifact_paths)
                if self.verifier is None
                else self.verifier(self.root)
            )
        except (OSError, SnapshotError, ArtifactError) as exc:
            raise CandidateIntegrityError(f"Candidate bytes changed: {exc}") from exc
        if actual != self.digest:
            raise CandidateIntegrityError("Candidate bytes changed after sealing")


@dataclass(frozen=True)
class RolloutSample:
    sample_id: str
    candidate_digest: str
    requested_seed: int | None

    def __post_init__(self) -> None:
        _require_nonblank("sample_id", self.sample_id)
        _require_nonblank("candidate_digest", self.candidate_digest)
        if self.requested_seed is not None and (
            not isinstance(self.requested_seed, int) or isinstance(self.requested_seed, bool)
        ):
            raise ValueError("requested_seed must be an integer or None")


@dataclass(frozen=True)
class RolloutKey:
    case_id: str
    sample_id: str
    candidate_digest: str

    def __post_init__(self) -> None:
        for name in ("case_id", "sample_id", "candidate_digest"):
            _require_nonblank(name, getattr(self, name))

    def to_payload(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "sample_id": self.sample_id,
            "candidate_digest": self.candidate_digest,
        }


@dataclass(frozen=True)
class ResumeDisposition:
    key: RolloutKey
    action: Literal["reuse", "interrupt", "schedule"]
    terminal: Terminal | None
    sample_disposition: SampleDisposition | None

    def __post_init__(self) -> None:
        if self.action not in {"reuse", "interrupt", "schedule"}:
            raise ValueError("resume action is invalid")
        if self.action == "interrupt":
            if self.terminal != "interrupted" or self.sample_disposition != disposition_for(
                "interrupted"
            ):
                raise ValueError("interrupted resume state must be indeterminate")
        elif self.sample_disposition is not None:
            raise ValueError("only interrupted resume state is indeterminate here")
        if self.action == "schedule" and self.terminal is not None:
            raise ValueError("unstarted samples do not have a terminal")
        if self.action == "reuse" and self.terminal is None:
            raise ValueError("reused samples require a terminal")


@dataclass(frozen=True)
class InstanceRecovery:
    instance_id: str
    candidate_digest: str
    strategy: MaterializationStrategy

    def __post_init__(self) -> None:
        _require_nonblank("instance_id", self.instance_id)
        _require_nonblank("candidate_digest", self.candidate_digest)
        if self.strategy not in {
            "adapter_apply_manifest",
            "filesystem_overlay",
            "artifact_root_injection",
        }:
            raise ValueError("instance recovery strategy is invalid")


@dataclass(frozen=True)
class CallRetries:
    """How often one target call is attempted (manifest `/8`).

    `max_attempts` counts the first attempt of every target call except the
    agent's turn; `turn_max_attempts` does the same for `send`, and a value
    above 1 is the operator's statement that resending a failed turn is sound
    for their agent.
    """

    max_attempts: int
    turn_max_attempts: int

    def __post_init__(self) -> None:
        for name in ("max_attempts", "turn_max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10:
                raise ValueError(f"call_retries.{name} must be an integer in 1..10")


@dataclass(frozen=True)
class RolloutBudget:
    max_turns: int
    timeout_seconds: float
    #: How many *consecutive* `candidate_failed` turns the rollout tolerates
    #: before it ends on the `candidate_failed` terminal. `1` terminates on
    #: the first failure, which is the behaviour every manifest before
    #: `evolution-v2-manifest/7` gets; `/7` requires the operator to state it
    #: (PDR-0005, PDR-0014).
    max_failed_turns: int = 1
    #: `None` means no call retries and today's classification — the gate
    #: every Phase A behaviour checks. `evolution-v2-manifest/8` requires the
    #: operator to state it (PDR-0005).
    call_retries: CallRetries | None = None
    cancelled: Callable[[], bool] = field(default=lambda: False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_turns, int)
            or isinstance(self.max_turns, bool)
            or self.max_turns < 1
        ):
            raise ValueError("max_turns must be positive")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if (
            not isinstance(self.max_failed_turns, int)
            or isinstance(self.max_failed_turns, bool)
            or self.max_failed_turns < 1
        ):
            raise ValueError("max_failed_turns must be positive")
        if self.call_retries is not None and not isinstance(self.call_retries, CallRetries):
            raise ValueError("call_retries must be a CallRetries or None")


class StaticInputEnvironment:
    def __init__(self, input_text_key: str = "text") -> None:
        _require_nonblank("input_text_key", input_text_key)
        self.input_text_key = input_text_key
        self.identity = EnvironmentIdentity(
            "static", _json_digest({"kind": "static", "input_text_key": input_text_key})
        )

    async def next(self, case: EnvironmentView, history: History) -> Message | None:
        if history.turns:
            return None
        content = case.public.get(self.input_text_key, "")
        if not isinstance(content, str):
            raise ValueError("static environment text input must be a string")
        return Message(content, case.public)


class ScriptedEnvironment:
    def __init__(self) -> None:
        self.identity = EnvironmentIdentity(
            "scripted",
            _json_digest(
                {
                    "kind": "scripted",
                    "schema_version": 1,
                    "entry": {"content": "string", "fields": "mapping-or-absent"},
                }
            ),
        )

    async def next(self, case: EnvironmentView, history: History) -> Message | None:
        script = case.private.get("script")
        if not isinstance(script, tuple):
            raise ValueError('scripted environment requires private["script"] array')
        index = len(history.turns)
        if index >= len(script):
            return None
        entry = script[index]
        if not isinstance(entry, Mapping) or not set(entry) <= {"content", "fields"}:
            raise ValueError("script entries require content and optional fields")
        if "content" not in entry:
            raise ValueError("script entries require content and optional fields")
        fields = entry.get("fields", {})
        if not isinstance(fields, Mapping):
            raise ValueError("script entry fields must be a mapping")
        return Message(entry["content"], fields)


class ScoringError(RuntimeError):
    """A scorer failed its contract.

    `code` classifies the failure for the family-14 connection probe:
    `process_failed`, `timeout`, `output_invalid`, `metric_type` or
    `metric_nonfinite`. `metric` names the offending metric and is set only for
    the two metric codes. Both default to `None`, which is every other failure.
    The message text is never derived from either. The class lives here, beside
    `ScoreResult`, so that `ScoreResultError` can subclass it without a cycle;
    `rollout_scoring` re-exports it.
    """

    def __init__(self, message: str = "", *, code: str | None = None, metric: str | None = None):
        super().__init__(message)
        self.code = code
        self.metric = metric


class ScoreResultError(ScoringError, ValueError):
    """A `ScoreResult` metric refusal: still a `ValueError`, now also typed."""


@dataclass(frozen=True)
class ScoreResult:
    metrics: Mapping[str, float]
    critical_pass: bool | None = None
    verdicts: tuple[Mapping[str, Any], ...] = ()
    public_feedback: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    #: Family 8 only (ADR-0021): the scorer's Reflector-addressed object, or None.
    diagnostics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        metrics: dict[str, float] = {}
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("metric names must not be blank")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ScoreResultError(
                    "metric values must be finite numbers", code="metric_type", metric=name
                )
            try:
                number = float(value)
            except OverflowError:
                # #202: an `int` too large for a float (`10**400`) is no finite
                # metric, exactly like `1e999`, which JSON already parses to inf.
                number = float("inf")
            if not isfinite(number):
                raise ScoreResultError(
                    "metric values must be finite numbers", code="metric_nonfinite", metric=name
                )
            metrics[name] = number
        if not metrics:
            raise ValueError("metrics must not be empty")
        if self.critical_pass is not None and type(self.critical_pass) is not bool:
            raise ValueError("critical_pass must be a boolean or None")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(
            self, "verdicts", tuple(_frozen_mapping(verdict) for verdict in self.verdicts)
        )
        object.__setattr__(self, "public_feedback", _frozen_mapping(self.public_feedback))
        object.__setattr__(self, "details", _frozen_mapping(self.details))
        if self.diagnostics is not None:
            object.__setattr__(self, "diagnostics", _frozen_mapping(self.diagnostics))


@dataclass(frozen=True)
class ScoringView:
    history: History
    terminal: Terminal
    target: SemanticIdentity

    def to_payload(self) -> dict[str, Any]:
        return {
            "history": self.history.to_payload(),
            "terminal": self.terminal,
            "target": {
                "contract_digest": self.target.contract_digest,
                "dependency_digest": self.target.dependency_digest,
                "settings_hash": self.target.settings_hash,
                "seed_mode": self.target.seed_mode,
                "protocol_version": self.target.protocol_version,
                "runtime_ref_digest": self.target.runtime_ref_digest,
            },
        }


class Scorer(Protocol):
    identity: ScorerIdentity

    async def score(
        self, case: EvalCase, view: ScoringView, candidate: CandidateView
    ) -> ScoreResult: ...


class TracingScorer(Protocol):
    """A `Scorer` that can name its private trace by the caller's id.

    `durable_score` calls `score_traced` with `pack_id=journal.run_id` and
    `trace_id=score_request_id` when a scorer offers it and `score` otherwise,
    so no existing `Scorer` changes. The id is the only link from a sample's
    `scoring_started` record to the scorer's trace; nothing about the trace
    is journaled.
    """

    identity: ScorerIdentity

    async def score_traced(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        *,
        trace_id: str,
        pack_id: str,
    ) -> ScoreResult: ...


@dataclass(frozen=True)
class Trajectory:
    case_id: str
    sample_id: str
    candidate_digest: str
    environment_id: str
    history: History
    terminal: Terminal
    provenance: Provenance
    disposition: SampleDisposition = field(init=False)

    def __post_init__(self) -> None:
        for name in ("case_id", "sample_id", "candidate_digest", "environment_id"):
            _require_nonblank(name, getattr(self, name))
        if self.terminal not in {
            "completed",
            "turn_budget",
            "time_budget",
            "target_failed",
            "candidate_failed",
            "cancelled",
            "interrupted",
        }:
            raise ValueError("trajectory terminal is invalid")
        if self.provenance.target.instance.loaded_candidate_digest != self.candidate_digest:
            raise ValueError("trajectory candidate differs from the loaded candidate")
        object.__setattr__(self, "disposition", disposition_for(self.terminal))

    def scoring_view(self) -> ScoringView:
        return ScoringView(self.history, self.terminal, self.provenance.target.semantic)


def condition_key(
    target: TargetIdentity,
    environment: EnvironmentIdentity,
    scorer: ScorerIdentity,
    evaluation_set_id: str,
    repeats: int,
    seed_schedule: tuple[int | None, ...],
    metric_plan_identity: str,
) -> tuple[Any, ...]:
    _require_nonblank("evaluation_set_id", evaluation_set_id)
    _require_nonblank("metric_plan_identity", metric_plan_identity)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    if len(seed_schedule) != repeats:
        raise ValueError("seed schedule length must match repeats")
    for seed in seed_schedule:
        _validate_optional_seed("seed schedule value", seed)
    if target.semantic.seed_mode == "exact" and any(seed is None for seed in seed_schedule):
        raise ValueError("exact seed schedule must contain only integers")
    if target.semantic.seed_mode != "exact" and any(seed is not None for seed in seed_schedule):
        raise ValueError("unpaired seed schedule must contain only None")
    semantic = target.semantic
    return (
        semantic.contract_digest,
        semantic.dependency_digest,
        semantic.settings_hash,
        semantic.seed_mode,
        semantic.protocol_version,
        semantic.runtime_ref_digest,
        environment.kind,
        environment.digest,
        scorer.kind,
        scorer.digest,
        evaluation_set_id,
        repeats,
        seed_schedule,
        metric_plan_identity,
    )


def are_samples_paired(left: SampleIdentity, right: SampleIdentity, seed_mode: SeedMode) -> bool:
    if seed_mode != "exact":
        return False
    return (
        left.candidate_digest != right.candidate_digest
        and left.case_id == right.case_id
        and left.repeat == right.repeat
        and left.requested_seed is not None
        and left.requested_seed == right.requested_seed
        and left.effective_seed == left.requested_seed
        and right.effective_seed == right.requested_seed
    )


def validate_score_result(case: EvalCase, result: ScoreResult) -> None:
    critical = case.oracle.get("critical") is True
    if critical and type(result.critical_pass) is not bool:
        raise ValueError("critical case requires a boolean critical_pass")
    if not critical and result.critical_pass is not None:
        raise ValueError("noncritical case must not return critical_pass")


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if any(not isinstance(key, str) for key in value):
        raise ValueError("mapping keys must be strings")
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _frozen_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise ValueError("boundary values must be JSON-compatible and finite")


def _payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_payload(item) for item in value]
    to_payload = getattr(value, "to_payload", None)
    return to_payload() if callable(to_payload) else value


def _require_nonblank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_optional_seed(name: str, value: int | None) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{name} must be an integer or None")


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
