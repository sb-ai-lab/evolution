"""The family-14 connection probe.

Two parts. The runner at the end (`run_probe`, `release_probe_leftover`,
`probe_settlement`) runs one real Champion Target -> Scorer probe in its own S1
journal `connection/<operation_id>/` and writes an owner-only trace under
`connection-logs/<operation_id>/`; the campaign records only cite them. The
pure core before it does no I/O and writes no journal. It holds:

- the `connection_binding/1` identity (`binding_payload`, `binding_digest`), which
  is exactly the contract's field list, and the deterministic probe seed (`probe_seed`)
  derived from it, the Champion's Candidate digest and `connection_preflight/1`;
- the representative case: the first case of the bound train corpus, index 0;
- the Target-field check over a completed rollout: for each declared
  field in manifest order, the last event of the declared type across the whole
  rollout (so a trailing synthetic `output_truncated` never hides it), read with
  `reflect_grouping.pointer_value`, and checked against the value rule. The
  first failure is reported;
- the Scorer primary-metric check, and the mapping of a typed Scorer failure
  (`ScoringError.code`) onto connection failure codes;
- the description check: the created instance's `description`, tested
  with the campaign journal's own secret predicate (`storage.envelope.secret_match`)
  before the rollout, named by the manifest path of the matched secret's name;
- `classify`, which turns the stages a probe went through into one
  `ProbeOutcome` (R56): `compatible`, or one of the three failure classes with
  its detail code, paths and bounded cause.

A diagnostic carries closed codes and paths only. Every path is built from
manifest-declared strings (the event type, the pointer, the primary metric
name), never from Target or Scorer output, and is at most 2,048 UTF-8 bytes. A
non-primary metric's name comes from the Scorer, so it is not put in a path.
The bounded observed type (Q2) is returned beside a field diagnostic for the
owner-only trace only; it is never part of the diagnostic.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import os
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar, cast, get_args

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.artifacts.attestation import verify_attestation
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.check_modes import ENFORCE_ALL, CheckModes, HitSink
from evolution.tree.execution.interaction import (
    ATTESTATION_CODES,
    Environment,
    EvalCase,
    History,
    MaterializationStrategy,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    Scorer,
    ScoreResult,
    ScoringError,
    SemanticIdentity,
    TargetAdapter,
    TargetIdentity,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    Journal,
)
from evolution.tree.execution.journal_identity import (
    IdentityError,
    RunIdentityV1,
    canonical_json,
    corpus_digest,
)
from evolution.tree.execution.journal_recovery import close_interrupted_operations, recovery_plan
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter, durable_rollout
from evolution.tree.execution.journal_schema import (
    ERROR_CODE,
    S1_FAMILY,
    Lifecycle,
    try_is_retryable,
)
from evolution.tree.execution.journal_scoring import durable_score
from evolution.tree.execution.json_pointer import MISSING, pointer_value
from evolution.tree.execution.private_files import WorkspaceError, open_private_file
from evolution.tree.execution.rollout import (
    materialize_target,
    recover_target_instances,
    release_target,
)
from evolution.tree.execution.rollout_scoring import _close_scorer_traces
from evolution.tree.execution.wire_v2 import ERROR_CODES as WIRE_ERROR_CODES
from evolution.tree.execution.wire_v2 import new_instance_id
from evolution.tree.storage.append_log import Snapshot, _fault, inspect_log
from evolution.tree.storage.envelope import Envelope, SecretMatch, secret_match
from evolution.tree.storage.journal_errors import (
    JournalBoundaryError,
    JournalFault,
    JournalRecoveryRequired,
)
from evolution.tree.storage.redaction import safe_record
from evolution.tree.target.connection_contract import MetricContract, ValueRule

if TYPE_CHECKING:
    from evolution.tree.artifacts.artifacts import ArtifactRef

CONNECTION_BINDING_VERSION: Final = "connection_binding/1"
PROBE_PROFILE: Final = "connection_preflight/1"
MAX_PATH_BYTES: Final = 2048

DetailCode = Literal[
    "connection_event_missing",
    "connection_field_missing",
    "connection_field_empty",
    "connection_field_type",
    "connection_field_nonfinite",
    "connection_primary_missing",
    "connection_primary_type",
    "connection_primary_nonfinite",
    "connection_scorer_output_invalid",
    "connection_description_secret",
]
DETAIL_CODES: Final[tuple[str, ...]] = get_args(DetailCode)
OutcomeStatus = Literal[
    "compatible",
    "connection_incompatible",
    "connection_preflight_unavailable",
    "connection_preflight_inconclusive",
]
OUTCOME_STATUSES: Final[tuple[str, ...]] = get_args(OutcomeStatus)
Cleanup = Literal["released", "not_needed", "failed"]
CLEANUP_STATES: Final[tuple[str, ...]] = get_args(Cleanup)
ObservedType = Literal["string", "number", "boolean", "null", "object", "array", "missing"]
ExpectedType = Literal["string", "number", "boolean", "object", "array", "json"]

#: The manifest and observed paths of the Scorer checks.
PRIMARY_MANIFEST_PATH: Final = "campaign.metric.primary"
SCORER_MANIFEST_PATH: Final = "scorer"
SCORER_OUTPUT_PATH: Final = "scorer.output"
SCORER_METRICS_PATH: Final = "scorer.metrics"
#: The observed path of a run secret in the instance description: a
#: literal, because the description's keys are the Target's.
DESCRIPTION_OBSERVED_PATH: Final = "target.description"

#: The Scorer codes (R55) and what each means for the probe.
_SCORER_OPERATIONAL: Final = frozenset({"process_failed", "timeout"})
_SCORER_METRIC: Final = frozenset({"metric_type", "metric_nonfinite"})
_SCORER_OUTPUT: Final = frozenset({"output_invalid"}) | _SCORER_METRIC
#: A Scorer failure with no code (or an unknown one), Ruling 8: never an
#: incompatibility claim, so `unavailable`.
SCORER_FAILED_CAUSE: Final = "scorer_failed"
CLEANUP_FAILED_CAUSE: Final = "cleanup_failed"
#: The fallback for an infrastructure code outside the bounded code grammar:
#: `RolloutInfrastructureError`'s own default.
INFRASTRUCTURE_CAUSE: Final = "infrastructure"

#: Rollout terminals (S1 `rollout_finished` / `rollout_failed` outcomes) by class.
#: Only `completed` is a scoreable probe terminal (R56).
_UNAVAILABLE_TERMINALS: Final = frozenset(
    {"target_failed", "environment_failed", "infrastructure_failed"}
)
_INCONCLUSIVE_TERMINALS: Final = frozenset({"turn_budget", "time_budget", "candidate_failed"})

#: The infrastructure codes a probe's materialize or identity step can raise
#: through the product's own target path: `materialize_target`
#: (`invalid_response`, and `infrastructure` for an untyped failure), the HTTP
#: adapter and its call ledger, the attestation check and the Target Service's
#: wire error codes. A custom adapter's own code is outside it, and `classify`
#: records it as `infrastructure`; the bounded trace keeps the code itself.
INFRASTRUCTURE_CAUSES: Final = (
    frozenset(
        {
            INFRASTRUCTURE_CAUSE,
            "invalid_response",
            "invalid_request",
            "root_not_allowed",
            "not_preflighted",
            "response_too_large",
            "callee_timeout",
            "transport_error",
            "admission_closed",
            "upstream_unavailable",
            "target_epoch_changed",
        }
    )
    | ATTESTATION_CODES
    | WIRE_ERROR_CODES
)
#: The closed `cause_code` vocabulary of each failure class (maintainer ruling
#: 2026-10-12, ADR-0029 amendment): exactly what `classify` and the unsettled
#: recovery terminal write. `connection_incompatible` carries a cause only for
#: `connection_scorer_output_invalid` (`_check_cause`).
CAUSE_CODES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "connection_incompatible": _SCORER_OUTPUT,
        "connection_preflight_unavailable": (
            _SCORER_OPERATIONAL
            | {SCORER_FAILED_CAUSE, CLEANUP_FAILED_CAUSE}
            | _UNAVAILABLE_TERMINALS
            | INFRASTRUCTURE_CAUSES
        ),
        "connection_preflight_inconclusive": _INCONCLUSIVE_TERMINALS,
    }
)
_PROBE_TERMINALS: Final = (
    frozenset({"completed"}) | _UNAVAILABLE_TERMINALS | _INCONCLUSIVE_TERMINALS
)

_EXPECTED: Final[Mapping[str, ExpectedType]] = {
    "nonempty_string": "string",
    "string": "string",
    "finite_number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "finite_json": "json",
}

_CaseT = TypeVar("_CaseT")


def cap_path(path: str) -> str:
    """`path` cut to at most `MAX_PATH_BYTES` UTF-8 bytes, never mid-character.

    A character that cannot be encoded (a lone surrogate) becomes `?`.
    """

    encoded = path.encode("utf-8", "replace")
    if len(encoded) <= MAX_PATH_BYTES:
        return encoded.decode("utf-8")
    return encoded[:MAX_PATH_BYTES].decode("utf-8", "ignore")


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8", "replace"))


def target_field_manifest_path(index: int) -> str:
    return f"scorer.metric_contract.target_fields[{index}]"


def target_field_observed_path(event: str, pointer: str) -> str:
    return cap_path(f"events[event={event},last]{pointer}")


def primary_observed_path(primary_metric: str) -> str:
    return cap_path(f"{SCORER_METRICS_PATH}.{primary_metric}")


@dataclass(frozen=True)
class Diagnostic:
    """One connection incompatibility: closed codes and paths, never a value."""

    detail_code: DetailCode
    manifest_path: str
    observed_path: str
    cause_code: str | None = None

    def __post_init__(self) -> None:
        if self.detail_code not in DETAIL_CODES:
            raise ValueError("diagnostic detail code is not a connection code")
        for path in (self.manifest_path, self.observed_path):
            if not isinstance(path, str) or not path or _utf8_bytes(path) > MAX_PATH_BYTES:
                raise ValueError("diagnostic path is not a bounded string")
        _check_cause(self.detail_code, self.cause_code)


def _check_cause(detail_code: str, cause_code: str | None) -> None:
    if detail_code == "connection_scorer_output_invalid":
        if cause_code not in _SCORER_OUTPUT:
            raise ValueError("scorer output diagnostic needs its Scorer code")
    elif cause_code is not None:
        raise ValueError("only a scorer output diagnostic carries a cause")


def secret_path(index: int, secret_paths: Sequence[str]) -> str:
    """The manifest path of the run secret at `index`, or `secrets[<index>]`.

    `secret_paths` is `evolve._run_secret_paths`, aligned with the run's
    secrets; a library caller of `run_campaign` may pass secrets with no
    paths, and a secret beyond them is named by its position.
    """

    return secret_paths[index] if 0 <= index < len(secret_paths) else f"secrets[{index}]"


def journalable_path(path: str, secrets: Sequence[str]) -> str:
    """`path`, or its role prefix when `path` itself matches a run secret.

    A diagnostic is journaled, so its manifest path obeys the journal's own
    predicate. A name-bearing path that matches (a secret whose value holds
    its own environment name) falls back to its list key
    (`scorer.allowed_env`, `grouper.allowed_env`), any other to its role
    (`editor`), and `secrets[<index>]` to the literal `secrets`. A secret that
    matches the fallback too is a fixed literal's hazard, which every journal
    record already has .
    """

    if secret_match({"payload": {"path": path}}, secrets) is None:
        return path
    if path.startswith("secrets["):
        return "secrets"
    head, separator, _name = path.partition(".allowed_env.")
    if separator:
        return f"{head}.allowed_env"
    return path.split(".", 1)[0]


#: ADR-0029 amendment 2026-10-21: the fixed role order of the first match.
_ROLE_RANK: Final = {"scorer": 0, "editor": 1, "reflector": 2, "grouper": 3}


def secret_order(
    index: int, secret_paths: Sequence[str], secret_names: Sequence[str | None] = ()
) -> tuple[int, int, str, int]:
    """The sort key of the run secret at `index` among the matching ones.

    ADR-0029 as amended 2026-10-21: Scorer, Editor, Reflector, Grouper, names
    sorted within a role. The role is the manifest path's first segment
    (`secret_path`); the name is `secret_names[index]` (`evolve._run_secret_names`,
    which carries the names a `chat` role's `api_key_env`/`proxy_env` path does
    not spell), else the `<NAME>` of an `.allowed_env.<NAME>` path. A secret
    with no name -- a bare-role path for an undeclared bound-environment value
    -- sorts after its role's named ones, and a `secrets[<index>]` library path
    after every role; ties fall back to the index.
    """

    path = secret_path(index, secret_paths)
    rank = _ROLE_RANK.get(path.split(".", 1)[0], len(_ROLE_RANK))
    name = secret_names[index] if 0 <= index < len(secret_names) else None
    if name is None:
        _head, separator, tail = path.partition(".allowed_env.")
        name = tail if separator else None
    return (rank, 0, name, index) if name is not None else (rank, 1, "", index)


def description_match(
    description: object,
    secrets: Sequence[str],
    secret_paths: Sequence[str] = (),
    secret_names: Sequence[str | None] = (),
) -> SecretMatch | None:
    """The first run secret in role order in `description`, and where it is.

    Both sites, the connection probe and the campaign's refusal, ask
    this. `secret_match` decides whether anything matches; among every secret
    that matches alone, the first by `secret_order` is chosen, and its
    location is asked with that secret alone, because the joint walk's
    `location` can be another secret's. The location is payload-relative:
    `description.<path>`, or `record-key` for a key name. Without paths or
    names the order is the index, `secret_match`'s own.
    """

    value = {"payload": {"description": description}}
    if secret_match(value, secrets) is None:
        return None
    located = {
        index: own
        for index, secret in enumerate(secrets)
        if (own := secret_match(value, (secret,))) is not None
    }
    # Some secret matched, and a secret that matches anywhere matches alone.
    chosen = min(located, key=lambda index: secret_order(index, secret_paths, secret_names))
    return SecretMatch(located[chosen].location, chosen)


def utf8_bounded(text: str) -> str:
    """`text` cut to `MAX_TRACE_TEXT_BYTES` of UTF-8, a lone surrogate as `?`.

    The cut never splits a character. A caller that must keep a secret out
    redacts before cutting (`bounded_location`), or tests the uncut text, since
    a cut can leave a secret's prefix.
    """

    encoded = text.encode("utf-8", "replace")[:MAX_TRACE_TEXT_BYTES]
    return encoded.decode("utf-8", "ignore")


def bounded_location(location: str, secrets: Sequence[str]) -> str:
    """A Target-controlled key path made safe to write .

    Redacted, then cut by `utf8_bounded`, as every other Target-controlled
    trace text is (`_bounded_text`): a lone surrogate in a library adapter's
    key becomes `?` instead of failing the trace write, and a very long key
    cannot make the field unbounded.
    """

    return utf8_bounded(str(safe_record(location, tuple(secret for secret in secrets if secret))))


def description_secret(
    description: object,
    secrets: Sequence[str],
    secret_paths: Sequence[str],
    secret_names: Sequence[str | None] = (),
) -> tuple[Diagnostic, str] | None:
    """The refusal of an instance description holding a run secret.

    `secret_match` is the campaign journal's own predicate: every key and
    every string value, at every depth, tested for each secret as a
    substring; so a description that passes here passes `instance_attested`
    too. The diagnostic names the first matching secret in role order, names
    sorted within a role (`secret_order`), by its manifest path; the second
    element is that secret's description key path (`description_match`) in
    the `secret_location` grammar (`description.<path>`, or `record-key`),
    bounded (`bounded_location`), for the owner-only trace only. `None` when
    `description` is `None` or holds no secret.
    """

    if description is None:
        return None
    found = description_match(description, secrets, secret_paths, secret_names)
    if found is None:
        return None
    path = cap_path(journalable_path(secret_path(found.index, secret_paths), secrets))
    diagnostic = Diagnostic("connection_description_secret", path, DESCRIPTION_OBSERVED_PATH)
    return diagnostic, bounded_location(found.location, secrets)


@dataclass(frozen=True)
class FieldFailure:
    """A Target-field diagnostic, plus the bounded types for the owner-only trace.

    `expected_type` and `observed_type` go into the private trace summary only
    (Q2); the campaign record carries `diagnostic`'s codes and paths.
    `observed_type` is `None` for a value that is not JSON at all, which a
    frozen `History` cannot hold.
    """

    diagnostic: Diagnostic
    expected_type: ExpectedType
    observed_type: ObservedType | None


@dataclass(frozen=True)
class ScorerFailure:
    """The bounded part of a Scorer `ScoringError`: its code and metric name only."""

    code: str | None
    metric: str | None = None

    @classmethod
    def from_error(cls, error: ScoringError) -> ScorerFailure:
        return cls(error.code, error.metric)


@dataclass(frozen=True)
class ProbeOutcome:
    """What one probe established.

    `compatible` carries nothing else. `connection_incompatible` carries a
    detail code and both paths, and a cause only for
    `connection_scorer_output_invalid`. The other two classes carry only a
    bounded `cause_code`.
    """

    status: OutcomeStatus
    detail_code: DetailCode | None
    manifest_path: str | None
    observed_path: str | None
    cause_code: str | None

    def __post_init__(self) -> None:
        if self.status not in OUTCOME_STATUSES:
            raise ValueError("probe outcome status is not a connection status")
        if self.status == "connection_incompatible":
            if self.detail_code is None or self.manifest_path is None or self.observed_path is None:
                raise ValueError("an incompatible outcome needs its detail code and paths")
            Diagnostic(self.detail_code, self.manifest_path, self.observed_path, self.cause_code)
            return
        if (
            self.detail_code is not None
            or self.manifest_path is not None
            or self.observed_path is not None
        ):
            raise ValueError("only an incompatible outcome carries a detail code and paths")
        if self.status == "compatible":
            if self.cause_code is not None:
                raise ValueError("a compatible outcome carries no cause")
        elif self.cause_code not in CAUSE_CODES[self.status]:
            raise ValueError("an unavailable or inconclusive outcome needs a cause of its class")

    @classmethod
    def incompatible(cls, diagnostic: Diagnostic) -> ProbeOutcome:
        return cls(
            "connection_incompatible",
            diagnostic.detail_code,
            diagnostic.manifest_path,
            diagnostic.observed_path,
            diagnostic.cause_code,
        )


# --- binding identity, seed and case (choices 4 and 6) ---------------------------------


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _index(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def binding_payload(
    *,
    epoch: int,
    target: str,
    environment: str,
    scorer: str,
    metric_contract: str,
    primary_metric: str,
    corpus_digest: str,
    case_id: str,
    case_index: int,
) -> dict[str, Any]:
    """The canonical `connection_binding/1` object.

    `target`, `environment` and `scorer` are the digests of the epoch identity's
    sections, `metric_contract` the canonical `metric_contract/1` digest, and
    `corpus_digest` the bound train corpus digest. The Champion is not part of
    it: a Champion change keeps the binding and changes only the seed.
    """

    return {
        "version": CONNECTION_BINDING_VERSION,
        "epoch": _index(epoch, "epoch"),
        "target": _nonblank(target, "target"),
        "environment": _nonblank(environment, "environment"),
        "scorer": _nonblank(scorer, "scorer"),
        "metric_contract": _nonblank(metric_contract, "metric_contract"),
        "primary_metric": _nonblank(primary_metric, "primary_metric"),
        "corpus_digest": _nonblank(corpus_digest, "corpus_digest"),
        "case_id": _nonblank(case_id, "case_id"),
        "case_index": _index(case_index, "case_index"),
    }


def binding_digest(
    *,
    epoch: int,
    target: str,
    environment: str,
    scorer: str,
    metric_contract: str,
    primary_metric: str,
    corpus_digest: str,
    case_id: str,
    case_index: int,
) -> str:
    """`D`: the SHA-256 of the canonical `connection_binding/1` object."""

    payload = binding_payload(
        epoch=epoch,
        target=target,
        environment=environment,
        scorer=scorer,
        metric_contract=metric_contract,
        primary_metric=primary_metric,
        corpus_digest=corpus_digest,
        case_id=case_id,
        case_index=case_index,
    )
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def section_digest(section: Mapping[str, Any]) -> str:
    """The SHA-256 of one identity section's canonical JSON (choice 4)."""

    return hashlib.sha256(canonical_json(dict(section))).hexdigest()


def epoch_binding(epoch: int, identity: Mapping[str, Any]) -> dict[str, Any]:
    """`binding_digest`'s keyword fields for condition epoch `epoch` (choice 4).

    `identity` is that epoch's campaign identity payload. `target`,
    `environment` and `scorer` are the digests of its sections, the same
    sections every measurement identity of the epoch copies;
    `metric_contract` is its bound contract digest, `primary_metric` its
    policy's primary metric, `corpus_digest` the bound train corpus and the
    case its first train case (index 0). The campaign's gate and the audit
    both derive `D` here.
    """

    case = identity["corpus_cases"][0]
    return dict(
        epoch=epoch,
        target=section_digest(identity["target"]),
        environment=section_digest(identity["environment"]),
        scorer=section_digest(identity["scorer"]),
        metric_contract=identity["metric_contract"],
        primary_metric=identity["policy"]["metric"]["primary"],
        corpus_digest=identity["corpus_digest"],
        case_id=str(case["case_id"]),
        case_index=0,
    )


def probe_seed(binding: str, champion: str) -> int:
    """The probe's deterministic 31-bit seed.

    Derived from exactly the binding digest, the Champion's Candidate digest and
    `connection_preflight/1`; it fits the wire's seed range.
    """

    payload = {
        "version": PROBE_PROFILE,
        "binding": _nonblank(binding, "binding"),
        "candidate_digest": _nonblank(champion, "candidate_digest"),
    }
    return int.from_bytes(hashlib.sha256(canonical_json(payload)).digest()[:4], "big") & 0x7FFFFFFF


def representative_case(corpus: Sequence[_CaseT]) -> tuple[_CaseT, int]:
    """The first case of the bound train corpus and its index, 0."""

    if not corpus:
        raise ValueError("the train corpus has no case to probe")
    return corpus[0], 0


# --- the Target-field check ------------------------------------------


def history_events(history: History) -> Iterator[Mapping[str, Any]]:
    """Every event of a completed rollout, across all turns, in emission order."""

    for turn in history.turns:
        yield from turn.events


def _json_kind(value: object) -> ObservedType | None:
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return None


def observed_type(value: object) -> ObservedType | None:
    """The bounded JSON type of an observed value, for the owner-only trace (Q2).

    `missing` is the pointer evaluator's `MISSING`; `None` is a value that is
    not JSON at all.
    """

    return _json_kind(value)


def expected_type(rule: ValueRule) -> ExpectedType:
    """The JSON type a value rule expects (`json` for `finite_json`)."""

    return _EXPECTED[rule]


def _finite_json(value: object) -> DetailCode | None:
    """The first non-JSON or non-finite value inside `value`, depth first, iteratively."""

    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, (bool, str, int)):
            continue
        if isinstance(item, float):
            if not isfinite(item):
                return "connection_field_nonfinite"
            continue
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                return "connection_field_type"
            stack.extend(reversed(list(item.values())))
            continue
        if isinstance(item, (list, tuple)):
            stack.extend(reversed(item))
            continue
        return "connection_field_type"
    return None


def _value_failure(rule: ValueRule, value: object) -> DetailCode | None:
    """The detail code for `value` under `rule`, or `None` when it satisfies it.

    `null` never satisfies a rule. An empty string, object or array fails a
    rule of its own type (and `finite_json`) as empty; `nonempty_string` also
    needs a non-whitespace character.
    """

    if value is None:
        return "connection_field_type"
    kind = _json_kind(value)
    if rule in ("nonempty_string", "string"):
        if kind != "string":
            return "connection_field_type"
        assert isinstance(value, str)
        if not value or (rule == "nonempty_string" and not value.strip()):
            return "connection_field_empty"
        return None
    if rule == "finite_number":
        if kind != "number":
            return "connection_field_type"
        # An integer is exact and finite at any size (`isfinite` would overflow).
        if isinstance(value, float) and not isfinite(value):
            return "connection_field_nonfinite"
        return None
    if rule == "boolean":
        return None if kind == "boolean" else "connection_field_type"
    if rule == "object":
        if kind != "object":
            return "connection_field_type"
        return None if value else "connection_field_empty"
    if rule == "array":
        if kind != "array":
            return "connection_field_type"
        return None if value else "connection_field_empty"
    # finite_json: any JSON value, non-empty at the root, finite throughout.
    if kind is None:
        return "connection_field_type"
    if kind in ("string", "object", "array") and not value:
        return "connection_field_empty"
    return _finite_json(value)


def check_event_fields(
    events: Iterable[Mapping[str, Any]], contract: MetricContract
) -> FieldFailure | None:
    """The first declared Target field, in manifest order, that `events` violates.

    `events` is the whole completed rollout in emission order. For each field
    the selected event is the last one whose `type` is the declared event; the
    pointer is evaluated against that complete event object.
    """

    stream = list(events)
    for index, field in enumerate(contract.target_fields):
        selected = next(
            (event for event in reversed(stream) if event.get("type") == field.event), MISSING
        )
        if selected is MISSING:
            code: DetailCode | None = "connection_event_missing"
            value: object = MISSING
        else:
            value = pointer_value(selected, field.pointer)
            code = (
                "connection_field_missing"
                if value is MISSING
                else _value_failure(field.value, value)
            )
        if code is not None:
            return FieldFailure(
                Diagnostic(
                    code,
                    target_field_manifest_path(index),
                    target_field_observed_path(field.event, field.pointer),
                ),
                expected_type=expected_type(field.value),
                observed_type=observed_type(value),
            )
    return None


def check_target_fields(history: History, contract: MetricContract) -> FieldFailure | None:
    """`check_event_fields` over a completed rollout's `History`."""

    return check_event_fields(history_events(history), contract)


# --- the Scorer checks ----------------------------------------------------


def _primary(code: DetailCode, primary_metric: str) -> Diagnostic:
    return Diagnostic(code, PRIMARY_MANIFEST_PATH, primary_observed_path(primary_metric))


def check_primary(metrics: Mapping[str, object], primary_metric: str) -> Diagnostic | None:
    """The primary-metric check on a successful Scorer result's metrics.

    A `ScoreResult` already refuses a boolean or non-finite metric when it is
    built (those arrive as a typed `ScoringError`); a raw mapping is checked in
    full anyway, but without converting integers, so a huge integer passes here;
    `ScoreResult` refuses it first as `metric_nonfinite`.
    """

    if primary_metric not in metrics:
        return _primary("connection_primary_missing", primary_metric)
    value = metrics[primary_metric]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _primary("connection_primary_type", primary_metric)
    if isinstance(value, float) and not isfinite(value):
        return _primary("connection_primary_nonfinite", primary_metric)
    return None


def _scorer_outcome(failure: ScorerFailure, primary_metric: str) -> ProbeOutcome:
    code = failure.code
    if code in _SCORER_METRIC:
        if failure.metric is not None and failure.metric == primary_metric:
            detail: DetailCode = (
                "connection_primary_type"
                if code == "metric_type"
                else "connection_primary_nonfinite"
            )
            return ProbeOutcome.incompatible(_primary(detail, primary_metric))
        return ProbeOutcome.incompatible(
            Diagnostic(
                "connection_scorer_output_invalid",
                SCORER_MANIFEST_PATH,
                SCORER_METRICS_PATH,
                code,
            )
        )
    if code == "output_invalid":
        return ProbeOutcome.incompatible(
            Diagnostic(
                "connection_scorer_output_invalid", SCORER_MANIFEST_PATH, SCORER_OUTPUT_PATH, code
            )
        )
    if code in _SCORER_OPERATIONAL:
        return ProbeOutcome("connection_preflight_unavailable", None, None, None, code)
    return ProbeOutcome("connection_preflight_unavailable", None, None, None, SCORER_FAILED_CAUSE)


# --- classification (choice 8, R56) ----------------------------------------------------


def _unavailable(cause: str) -> ProbeOutcome:
    return ProbeOutcome("connection_preflight_unavailable", None, None, None, cause)


def classify(
    *,
    primary_metric: str,
    cleanup: Cleanup,
    infrastructure_code: str | None = None,
    terminal: str | None = None,
    field_failure: FieldFailure | None = None,
    scorer_failure: ScorerFailure | None = None,
    score: ScoreResult | None = None,
    description_secret: Diagnostic | None = None,
) -> ProbeOutcome:
    """The one outcome of a probe, from the stages it reached, in protocol order.

    1. A refused release (`cleanup: failed`) makes any outcome `unavailable`
       with `cleanup_failed`.
    2. An infrastructure or attestation failure at materialize or identity is
       `unavailable` with its code (`infrastructure` when the code is outside
       `INFRASTRUCTURE_CAUSES`, the closed set the product's target path raises).
       An attested instance whose description holds a run secret
       (`description_secret`) is `connection_incompatible`: the probe
       ran no rollout.
    3. The rollout terminal: `completed` goes on; `target_failed`,
       `environment_failed` and `infrastructure_failed` are `unavailable`;
       `turn_budget`, `time_budget` and `candidate_failed` are `inconclusive`.
       `cancelled` and `interrupted` are not outcomes: the caller writes
       `connection_preflight_interrupted`.
    4. A Target-field failure is `connection_incompatible`.
    5. A Scorer failure: `process_failed` / `timeout` are `unavailable`; an
       output code is `connection_incompatible`; no code is `unavailable` with
       `scorer_failed`.
    6. The Scorer result (`score`, a `ScoreResult`): a missing primary is
       `connection_primary_missing`; otherwise `compatible`, which needs
       `cleanup: released`.
    """

    if cleanup not in CLEANUP_STATES:
        raise ValueError("cleanup is not a connection cleanup state")
    if terminal is not None and terminal not in _PROBE_TERMINALS:
        raise ValueError("terminal is not a probe outcome")
    if score is not None and scorer_failure is not None:
        raise ValueError("a Scorer result and a Scorer failure are exclusive")
    if cleanup == "failed":
        return _unavailable(CLEANUP_FAILED_CAUSE)
    if infrastructure_code is not None:
        known = infrastructure_code in INFRASTRUCTURE_CAUSES
        return _unavailable(infrastructure_code if known else INFRASTRUCTURE_CAUSE)
    if description_secret is not None:
        return ProbeOutcome.incompatible(description_secret)
    if terminal is None:
        raise ValueError("a probe without an infrastructure failure needs its terminal")
    if terminal in _UNAVAILABLE_TERMINALS:
        return _unavailable(terminal)
    if terminal in _INCONCLUSIVE_TERMINALS:
        return ProbeOutcome("connection_preflight_inconclusive", None, None, None, terminal)
    if field_failure is not None:
        return ProbeOutcome.incompatible(field_failure.diagnostic)
    if scorer_failure is not None:
        return _scorer_outcome(scorer_failure, primary_metric)
    if score is None:
        raise ValueError("a completed probe needs its Scorer result or failure")
    diagnostic = check_primary(score.metrics, primary_metric)
    if diagnostic is not None:
        return ProbeOutcome.incompatible(diagnostic)
    if cleanup != "released":
        raise ValueError("a compatible probe must have released its instance")
    return ProbeOutcome("compatible", None, None, None, None)


# --- the probe runner (R53, R54, R63) ---------------------------------------------------

#: The probe's S1 journal lives at `connection/<operation_id>/` (R53, choice 12).
CONNECTION_DIR: Final = "connection"
#: Its owner-only trace root, one of `bindings.FAMILY14_OUTPUT_ROOTS` (R63, choice 11).
CONNECTION_LOGS_DIR: Final = "connection-logs"
#: The bounded trace summary whose SHA-256 the campaign records cite.
TRACE_FILE: Final = "summary.json"
TRACE_VERSION: Final = "connection_preflight_trace/1"
PROBE_SAMPLE_ID: Final = "probe"
#: The bound on Scorer- or Target-controlled text the trace keeps (a metric name).
MAX_TRACE_TEXT_BYTES: Final = 256
MAX_TRACE_LIST: Final = 64
_MAX_SEED: Final = 0x7FFFFFFF
#: Optional identity fields a probe never carries: it has one try, no repeats,
#: no row provenance, and it is not a measurement with an admission (R59).
_NOT_PROBE_FIELDS: Final = (
    "sample_retries",
    "case_subset",
    "case_repeats",
    "row_provenance",
    "connection_binding",
)
_INTERRUPTIONS: Final = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
#: What a Target failure raises at materialize or identity; `AttestationError`
#: is a `RolloutInfrastructureError`.
_TARGET_FAILURES: Final = (RolloutInfrastructureError, RolloutTargetUnstable)
#: A rollout stopped by `budget.cancelled()`: not an outcome (R56).
_STOPPED_TERMINALS: Final = frozenset({"cancelled"})

_ScorerT = TypeVar("_ScorerT")


def probe_path(run_root: Path, operation_id: str) -> Path:
    """`<run_root>/connection/<operation_id>`: the probe's own S1 journal directory."""

    return Path(run_root) / CONNECTION_DIR / operation_id


def trace_path(run_root: Path, operation_id: str) -> Path:
    """`<run_root>/connection-logs/<operation_id>`: the probe's owner-only trace root."""

    return Path(run_root) / CONNECTION_LOGS_DIR / operation_id


def probe_identity(
    base: RunIdentityV1, corpus: Sequence[EvalCase], seed: int, *, sample_retries: int | None = None
) -> RunIdentityV1:
    """Choice 5's probe identity, derived from the Champion's measurement identity.

    `base` binds the full train corpus, the Champion's Candidate digest and the
    run's Target, Environment, Scorer, budget and materialization strategy.
    The probe keeps all of those, takes profile `connection_preflight/1`, names
    the first train case by index (`case_subset: [0]`) and schedules exactly one
    row `{case_id, sample_id: "probe", repeat: 1, requested_seed}`. The seed is
    requested only under `seed_mode: exact` (the S1 seed rule); it is still the
    start record's in every mode. It never carries `connection_binding`,
    `row_provenance` or `case_repeats`. Retries are bound only when explicitly supplied.
    """

    if type(seed) is not int or not 0 <= seed <= _MAX_SEED:
        raise ValueError("probe seed must be a 31-bit integer")
    data = base.to_payload()
    if corpus_digest(corpus) != data["corpus_digest"]:
        raise IdentityError("run_identity_changed")
    case, index = representative_case(corpus)
    for name in _NOT_PROBE_FIELDS:
        data.pop(name, None)
    exact = data["target"]["seed_mode"] == "exact"
    data["profile"] = PROBE_PROFILE
    if sample_retries is not None:
        data["sample_retries"] = sample_retries
    data["case_subset"] = [index]
    data["schedule"] = [
        {
            "case_id": case.case_id,
            "sample_id": PROBE_SAMPLE_ID,
            "repeat": 1,
            "requested_seed": seed if exact else None,
        }
    ]
    return RunIdentityV1.from_payload(data)


def scorer_with_trace_root(scorer: _ScorerT, root: Path) -> _ScorerT:
    """`scorer` with its private traces under `root` instead of `scorer-traces/` (R63).

    A dataclass with an init `trace_root` field (`SubprocessScorer`) is
    `dataclasses.replace`d, which keeps its sealed identity; another object
    with a `trace_root` attribute is copied and re-pointed; a scorer without
    one is returned unchanged. The original is never modified.
    """

    if dataclasses.is_dataclass(scorer) and not isinstance(scorer, type):
        if any(item.name == "trace_root" and item.init for item in dataclasses.fields(scorer)):
            return cast(_ScorerT, dataclasses.replace(scorer, trace_root=Path(root)))
        return scorer
    if hasattr(scorer, "trace_root"):
        clone = copy.copy(scorer)
        setattr(clone, "trace_root", Path(root))  # noqa: B010 - an attribute of an unknown type
        return clone
    return scorer


@dataclass(frozen=True)
class ProbeResult:
    """What a probe leaves for the campaign records: an outcome, cleanup and digests.

    `outcome` is `None` for a probe stopped before it established anything (a
    rollout stopped by `budget.cancelled()`, or a settlement read back after an
    interruption); the caller then writes `connection_preflight_interrupted`.
    `trace_digest` is the SHA-256 of the written trace summary bytes (`None`
    when no summary was written). `evidence` holds the one `journal_prefix`
    reference to `connection/<op>/events.jsonl` (empty when no probe journal
    was born). The probe's reply and score are never part of it.
    """

    outcome: ProbeOutcome | None
    cleanup: Cleanup
    trace_digest: str | None
    evidence: tuple[Mapping[str, Any], ...]

    def _evidence(self) -> list[dict[str, Any]]:
        return [dict(reference) for reference in self.evidence]

    def terminal(self) -> tuple[str, dict[str, Any]]:
        """The `connection_preflight_finished` or `_failed` record (choice 7)."""

        outcome = self.outcome
        if outcome is None or self.trace_digest is None:
            raise ValueError("an interrupted probe has no outcome terminal")
        if outcome.status == "compatible":
            return "connection_preflight_finished", {
                "status": "compatible",
                "trace_digest": self.trace_digest,
                "cleanup": self.cleanup,
                "evidence": self._evidence(),
            }
        return "connection_preflight_failed", {
            "status": outcome.status,
            "detail_code": outcome.detail_code,
            "manifest_path": outcome.manifest_path,
            "observed_path": outcome.observed_path,
            "cause_code": outcome.cause_code,
            "trace_digest": self.trace_digest,
            "cleanup": self.cleanup,
            "evidence": self._evidence(),
        }

    def interrupted(self, reason: Literal["cancelled", "interrupted"]) -> dict[str, Any]:
        """The `connection_preflight_interrupted` payload (choice 7)."""

        if reason not in ("cancelled", "interrupted"):
            raise ValueError("interruption reason is not a connection reason")
        return {
            "reason": reason,
            "trace_digest": self.trace_digest,
            "cleanup": self.cleanup,
            "evidence": self._evidence(),
        }

    def unsettled(self) -> tuple[str, dict[str, Any]]:
        """Recovery's terminal when ownership cannot be settled.

        `connection_preflight_failed / connection_preflight_unavailable` with
        `cleanup_failed`; only for `cleanup: failed`.
        """

        if self.cleanup != "failed":
            raise ValueError("only an unsettled cleanup is a cleanup failure")
        return "connection_preflight_failed", {
            "status": "connection_preflight_unavailable",
            "detail_code": None,
            "manifest_path": None,
            "observed_path": None,
            "cause_code": CLEANUP_FAILED_CAUSE,
            "trace_digest": self.trace_digest,
            "cleanup": "failed",
            "evidence": self._evidence(),
        }


@dataclass
class _Stages:
    """How far one probe got: the inputs `classify` and the trace summary read."""

    materialize: bool = False
    infrastructure_code: str | None = None
    terminal: str | None = None
    field_failure: FieldFailure | None = None
    scorer_failure: ScorerFailure | None = None
    score: ScoreResult | None = None
    #: the refusal of a description holding a run secret, and the
    #: description key path that held it (owner-only trace only).
    description_secret: Diagnostic | None = None
    description_location: str | None = None
    error: str | None = None
    interrupted: bool = False

    def outcome(self, primary_metric: str, cleanup: Cleanup) -> ProbeOutcome | None:
        if self.terminal in _STOPPED_TERMINALS and self.infrastructure_code is None:
            return None
        return classify(
            primary_metric=primary_metric,
            cleanup=cleanup,
            infrastructure_code=self.infrastructure_code,
            terminal=self.terminal,
            field_failure=self.field_failure,
            scorer_failure=self.scorer_failure,
            score=self.score,
            description_secret=self.description_secret,
        )


async def run_probe(
    *,
    run_root: Path,
    operation_id: str,
    campaign_run_id: str,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[ArtifactRef],
    candidate: ArtifactRef,
    strategy: MaterializationStrategy,
    call_attempts: int | None,
    environment: Environment,
    scorer: Scorer,
    budget: RolloutBudget,
    expected: TargetIdentity | SemanticIdentity,
    contract: MetricContract,
    primary_metric: str,
    modes: CheckModes = ENFORCE_ALL,
    hits: HitSink | None = None,
    on_born: Callable[[], None] | None = None,
    secret_paths: Sequence[str] = (),
    secret_names: Sequence[str | None] = (),
) -> ProbeResult:
    """Run one Champion Target -> Scorer probe in its own S1 journal (R53, R54).

    Called after `connection_preflight_started` is durable. `identity` is
    `probe_identity(...)`; `candidate` the admitted Champion; `call_attempts`
    the run's `call_retries.max_attempts` (or `None`); `expected` the
    admitted Target identity, as a measurement passes it. `secret_paths`
    names each of `secrets` by its manifest path (`secret_path`), and
    `secret_names` by its declared environment name (`secret_order`).

    Sequence: create `connection/<op>/` (refused when it already holds
    anything: `JournalFault`); materialize and attest one instance; refuse
    its description when it holds a run secret; run the
    one probe row through `durable_rollout`; check the declared Target
    fields; score through `durable_score` with traces under
    `connection-logs/<op>/`; check the primary metric; release the instance
    on every path (`campaign_inspection._release`'s journal-fault fallback); and
    write the bounded owner-only trace summary. A refused release makes any
    outcome `connection_preflight_unavailable / cleanup_failed`.

    An interruption (`CancelledError`, `KeyboardInterrupt`, `SystemExit`), a
    journal fault or any unexpected error propagates after the release
    attempt and a best-effort trace; `probe_settlement` reads back what it left.

    `on_born` is called once the probe journal is born, before any Target call
    (the campaign's `after_connection_probe_journal_born` seam).
    """

    data = identity.to_payload()
    if data["profile"] != PROBE_PROFILE or len(data["schedule"]) != 1:
        raise IdentityError("invalid_identity_profile")
    row = data["schedule"][0]
    case = identity.validate_corpus(corpus)[row["case_id"]]
    root = Path(run_root)
    path = probe_path(root, operation_id)
    try:
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise _fault(exc) from exc
    journal = Journal.create(path, operation_id, identity, corpus, secrets=secrets)
    probe_scorer = None
    try:
        if on_born is not None:
            on_born()
        try:
            traces = ensure_private_directory(
                ensure_private_directory(root / CONNECTION_LOGS_DIR) / operation_id
            )
        except (OSError, ValueError) as exc:
            raise _storage_fault(exc) from exc
        probe_scorer = scorer_with_trace_root(scorer, traces)
        instance_id = new_instance_id(campaign_run_id)
        stages = _Stages()
        propagating: BaseException | None = None
        try:
            await _probe(
                stages,
                journal=journal,
                identity=identity,
                corpus=corpus,
                case=case,
                seed=row["requested_seed"],
                adapter=adapter,
                candidate=candidate,
                strategy=strategy,
                instance_id=instance_id,
                call_attempts=call_attempts,
                environment=environment,
                scorer=probe_scorer,
                budget=budget,
                expected=expected,
                contract=contract,
                primary_metric=primary_metric,
                modes=modes,
                hits=hits,
                secrets=secrets,
                secret_paths=secret_paths,
                secret_names=secret_names,
            )
        except BaseException as exc:  # noqa: BLE001 - released, traced, then re-raised
            propagating = exc
            stages.interrupted = isinstance(exc, _INTERRUPTIONS)
            stages.error = type(exc).__name__
        fault = (
            await _release(adapter, instance_id, candidate.digest, strategy, journal, call_attempts)
            if stages.materialize
            else None
        )
        failure = propagating or fault
        cleanup, evidence, release_failures = _settled(journal, operation_id)
        outcome = masked = None
        if failure is None:
            outcome = stages.outcome(primary_metric, cleanup)
            if cleanup == "failed" and outcome is not None:
                # The cleanup failure is the cause; the trace keeps
                # what it masked.
                masked = stages.outcome(primary_metric, "released")
        if failure is not None:
            status = "interrupted" if stages.interrupted else "error"
        else:
            status = "interrupted" if outcome is None else outcome.status
        document = _trace_document(
            operation_id,
            status,
            outcome,
            masked,
            cleanup,
            stages,
            release_failures,
            evidence,
            secrets,
        )
        if failure is not None:
            # Best effort: a trace that cannot be written (a classified
            # `JournalFault`, or anything else) never replaces the
            # interruption or fault already travelling.
            with suppress(Exception):
                _write_trace(traces / TRACE_FILE, document)
            raise failure
        digest = _write_trace(traces / TRACE_FILE, document)
        return ProbeResult(outcome, cleanup, digest, evidence)
    finally:
        if probe_scorer is not scorer:
            _close_scorer_traces(probe_scorer)
        journal.close()


async def _probe(
    stages: _Stages,
    *,
    journal: Journal,
    identity: RunIdentityV1,
    corpus: Sequence[EvalCase],
    case: EvalCase,
    seed: int | None,
    adapter: TargetAdapter[ArtifactRef],
    candidate: ArtifactRef,
    strategy: MaterializationStrategy,
    instance_id: str,
    call_attempts: int | None,
    environment: Environment,
    scorer: Any,
    budget: RolloutBudget,
    expected: TargetIdentity | SemanticIdentity,
    contract: MetricContract,
    primary_metric: str,
    modes: CheckModes,
    hits: HitSink | None,
    secrets: Sequence[str],
    secret_paths: Sequence[str],
    secret_names: Sequence[str | None],
) -> None:
    """R54's stages, recording into `stages`; the caller releases and classifies."""

    stages.materialize = True
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
        reported = await instance.identity()
        verify_attestation(reported, candidate.expected_attestation())
    except _TARGET_FAILURES as exc:
        stages.infrastructure_code = exc.code
        return
    # the description `instance_attested` would journal, read as the
    # measurement reads it (`getattr`: an adapter that describes nothing is
    # still a `TargetInstance`), and refused before any rollout or score.
    mode = modes.mode("connection_description_secret")
    found = (
        None
        if mode == "off"
        else description_secret(
            getattr(instance, "description", None), secrets, secret_paths, secret_names
        )
    )
    if found is not None:
        if mode == "enforce":
            stages.description_secret, stages.description_location = found
            return
        if hits is not None:
            hits.hit("connection_description_secret", "connection_description_secret")

    async def measure(retry_options: dict[str, Any]) -> None:
        try:
            trajectory = await durable_rollout(
                journal=journal,
                identity=identity,
                corpus=corpus,
                case=case,
                sample=RolloutSample(PROBE_SAMPLE_ID, candidate.digest, seed),
                instance=instance,
                environment=environment,
                scorer=scorer,
                budget=budget,
                expected_target=expected,
                **retry_options,
            )
        # `durable_rollout` has closed the rollout with exactly these outcomes.
        except RolloutEnvironmentError:
            stages.terminal = "environment_failed"
            return
        except RolloutTimeBudgetExceeded:
            stages.terminal = "time_budget"
            return
        except RolloutTargetUnstable:
            stages.terminal = "target_failed"
            return
        except RolloutInfrastructureError:
            stages.terminal = "infrastructure_failed"
            return
        stages.terminal = trajectory.terminal
        if trajectory.terminal != "completed":
            return
        stages.field_failure = check_target_fields(trajectory.history, contract)
        if stages.field_failure is not None:
            return
        try:
            stages.score = await durable_score(
                journal, scorer, case, trajectory, candidate.candidate_view(), **retry_options
            )
        except ScoringError as exc:
            stages.scorer_failure = ScorerFailure.from_error(exc)

    retries = identity.to_payload().get("sample_retries")
    for ordinal in range(1 + (retries or 0)):
        stages.terminal = None
        stages.field_failure = None
        stages.scorer_failure = None
        stages.score = None
        await measure({} if retries is None else {"retry_ordinal": ordinal})
        if ordinal == (retries or 0) or stages.scorer_failure is not None:
            return
        outcome = stages.outcome(primary_metric, "released")
        if outcome is None or outcome.status in {"compatible", "connection_incompatible"}:
            return
        lifecycle = Lifecycle()
        for record in journal.snapshot().records:
            lifecycle.accept(record)
        key = (case.case_id, PROBE_SAMPLE_ID)
        execution = lifecycle.executions.get(key)
        if execution is None or not try_is_retryable(execution, lifecycle.scoring.get(key)):
            return


async def _release(
    adapter: TargetAdapter[ArtifactRef],
    instance_id: str,
    candidate_digest: str,
    strategy: MaterializationStrategy,
    journal: Journal,
    call_attempts: int | None,
) -> JournalBoundaryError | None:
    """Give the probe's instance back; return a journal fault, never raise one.

    `campaign_inspection._release`, with two probe rules. A journal that never
    recorded the instance (materialize refused before its start record) or
    already released it needs nothing. A journal with an open operation (an
    interruption S1 does not close in-process) cannot journal a release, so,
    like an unhealthy journal, it releases through the adapter alone; the
    journal still owns the instance and `release_probe_leftover` settles it.
    A Target refusal is swallowed (the journal keeps the ownership, which is
    what `cleanup: failed` reads); an interruption travels.
    """

    fault: JournalBoundaryError | None = None
    if journal.healthy:
        plan = recovery_plan(journal.snapshot().records)
        if instance_id not in {owned.instance_id for owned in plan.cleanup}:
            return None
        if not plan.actions:
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
    except _INTERRUPTIONS:
        raise
    except Exception:  # noqa: BLE001 - the Target refused; the journal still owns it
        pass
    return fault


def _cleanup_of(records: Sequence[Envelope]) -> Cleanup:
    """`not_needed` before any materialize, else `failed` while an instance is owned."""

    if not any(record.event == "target_materialize_started" for record in records):
        return "not_needed"
    return "failed" if recovery_plan(records).cleanup else "released"


def _release_failures(records: Sequence[Envelope]) -> list[dict[str, Any]]:
    """Every release failure's closed codes, in protocol order. The trace keeps the rest."""

    keys: tuple[str, ...] = ("failure", "error_code")
    failures: list[dict[str, Any]] = [
        {key: record.payload[key] for key in keys if key in record.payload}
        for record in records
        if record.event == "target_release_failed"
    ]
    return failures[:MAX_TRACE_LIST]


def _reference(operation_id: str, snapshot: Snapshot) -> Mapping[str, Any]:
    """A `journal_prefix` over the probe journal's whole durable prefix (choice 7)."""

    reference = _durable_reference.DurableReference(
        version=1,
        kind="journal_prefix",
        run_id=operation_id,
        locator=f"{CONNECTION_DIR}/{operation_id}/{EVENTS_FILE}#bytes=0:{snapshot.valid_bytes}",
        content_digest=snapshot.prefix_digest,
    )
    return MappingProxyType(reference.to_payload())


def _settled(
    journal: Journal, operation_id: str
) -> tuple[Cleanup, tuple[Mapping[str, Any], ...], list[dict[str, Any]]]:
    """Cleanup, evidence and release failures as the live journal records them."""

    try:
        snapshot = journal.snapshot()
        return (
            _cleanup_of(snapshot.records),
            (_reference(operation_id, snapshot),),
            _release_failures(snapshot.records),
        )
    except Exception:  # noqa: BLE001 - a faulted journal: the fault travels anyway
        return "failed", (), []


def _bounded_code(value: object) -> str | None:
    return value if isinstance(value, str) and ERROR_CODE.match(value) else None


def _bounded_text(value: object, secrets: tuple[str, ...]) -> str | None:
    """Scorer- or Target-controlled text, redacted first, then cut to 256 bytes."""

    if not isinstance(value, str):
        return None
    return utf8_bounded(str(safe_record(value, secrets)))


def _outcome_fields(outcome: ProbeOutcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "status": outcome.status,
        "detail_code": outcome.detail_code,
        "manifest_path": outcome.manifest_path,
        "observed_path": outcome.observed_path,
        "cause_code": outcome.cause_code,
    }


def _trace_document(
    operation_id: str,
    status: str,
    outcome: ProbeOutcome | None,
    masked: ProbeOutcome | None,
    cleanup: Cleanup,
    stages: _Stages,
    release_failures: list[dict[str, Any]],
    evidence: tuple[Mapping[str, Any], ...],
    secrets: Sequence[str],
) -> dict[str, Any]:
    """The bounded owner-only summary: codes, paths, types and digests, never a value.

    It holds what the campaign record leaves out: the expected and bounded
    observed type of a field failure (Q2), the Scorer's code and bounded,
    redacted metric name, the diagnosis a cleanup failure masked, and every
    release failure. A description that held a run secret adds one key,
    `description_location`, the Target's key path in the `secret_location`
    grammar; without one the key set and bytes are unchanged.
    """

    redactions = tuple(secret for secret in secrets if secret)
    fields = _outcome_fields(outcome) or {}
    failure = stages.field_failure
    scorer = stages.scorer_failure
    document: dict[str, Any] = {
        "version": TRACE_VERSION,
        "operation_id": operation_id,
        "status": status,
        "detail_code": fields.get("detail_code"),
        "manifest_path": fields.get("manifest_path"),
        "observed_path": fields.get("observed_path"),
        "cause_code": fields.get("cause_code"),
        "cleanup": cleanup,
        "infrastructure_code": (
            None
            if stages.infrastructure_code is None
            else _bounded_code(stages.infrastructure_code) or INFRASTRUCTURE_CAUSE
        ),
        "rollout_terminal": stages.terminal,
        "field": (
            None
            if failure is None
            else {"expected_type": failure.expected_type, "observed_type": failure.observed_type}
        ),
        "scorer": (
            None
            if scorer is None
            else {
                "code": _bounded_code(scorer.code),
                "metric": _bounded_text(scorer.metric, redactions),
            }
        ),
        "masked": _outcome_fields(masked),
        "cleanup_failures": release_failures,
        "error_class": _bounded_text(stages.error, redactions),
        "probe_journal": (
            None
            if not evidence
            else {
                "locator": evidence[0]["locator"],
                "content_digest": evidence[0]["content_digest"],
            }
        ),
    }
    if stages.description_location is not None:
        document["description_location"] = stages.description_location
    return cast(dict[str, Any], safe_record(document, redactions))


def _write_trace(path: Path, document: Mapping[str, Any]) -> str:
    """Write the summary once, owner-only; return the SHA-256 of the written bytes."""

    payload = (
        json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )
    try:
        descriptor = open_private_file(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            refusal=f"connection trace is not a private regular file: {path.name}",
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, WorkspaceError) as exc:
        raise _storage_fault(exc) from exc
    return hashlib.sha256(payload).hexdigest()


def _storage_fault(exc: OSError | WorkspaceError | ValueError) -> JournalFault:
    """The directory work's classification for any probe storage failure.

    An `OSError` is `append_log._fault`'s (`storage_capacity_exhausted` for a
    full disk or quota, else `journal_unavailable`); a refused private entry
    (a symlink, a non-directory, a non-regular file) is `journal_unavailable`,
    so the campaign's `_boundary` classifies the probe's trace and trace
    directory as it does the probe journal itself.
    """

    if isinstance(exc, OSError):
        return _fault(exc)
    return JournalFault("journal_unavailable")


def _snapshot(path: Path) -> Snapshot:
    return inspect_log(
        path,
        file_name=EVENTS_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=Lifecycle,
        envelope_family=S1_FAMILY,
    )


def _born(path: Path) -> bool:
    """`Measurement.classify(...) == "born"` for a probe journal's directory."""

    if not path.is_dir() or not (path / EVENTS_FILE).exists():
        return False
    return bool(_snapshot(path).records)


def _trace_digest(path: Path) -> str | None:
    """The SHA-256 of a written trace summary, or `None` when there is none."""

    if not path.exists():
        return None
    try:
        descriptor = open_private_file(
            path,
            os.O_RDONLY,
            refusal=f"connection trace is not a private regular file: {path.name}",
        )
        with os.fdopen(descriptor, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except (OSError, WorkspaceError) as exc:
        raise _storage_fault(exc) from exc


def probe_settlement(run_root: Path, operation_id: str) -> ProbeResult:
    """What a probe left on disk, read back without writing (`outcome` is `None`).

    For the gate's `connection_preflight_interrupted` after an interruption,
    and recovery's terminal after `release_probe_leftover`: `cleanup` from the
    probe journal (`not_needed` when nothing was ever materialized or no
    journal was born, `failed` while it still owns an instance, `released`
    otherwise), the trace summary's digest (`None` without one) and the
    journal-prefix evidence (empty without a born journal).
    """

    path = probe_path(run_root, operation_id)
    cleanup: Cleanup = "not_needed"
    evidence: tuple[Mapping[str, Any], ...] = ()
    if _born(path):
        snapshot = _snapshot(path)
        cleanup = _cleanup_of(snapshot.records)
        evidence = (_reference(operation_id, snapshot),)
    digest = _trace_digest(trace_path(run_root, operation_id) / TRACE_FILE)
    return ProbeResult(None, cleanup, digest, evidence)


async def release_probe_leftover(
    path: Path,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    adapter: TargetAdapter[Any],
    admitted_versions: Collection[str] = (),
) -> Cleanup:
    """Settle what a born probe journal still owns, under its own stored identity.

    As `campaign_inspection.release_leftover`: the journal is reopened as
    written (never re-derived), S1's interrupted operations are closed, and
    owned Target instances are recovered. Returns the journal's cleanup state
    afterwards: `not_needed` (absent, unborn, or never materialized),
    `released`, or `failed` (the Target refused, so the instance stays
    owned). A journal with nothing open and nothing owned is not reopened. A
    journal fault, and an `IdentityError` when the journal cannot be reopened
    under its stored identity, propagate with nothing written.
    """

    if not _born(path):
        return "not_needed"
    snapshot = _snapshot(path)
    plan = recovery_plan(snapshot.records)
    if not plan.actions and not plan.cleanup:
        return _cleanup_of(snapshot.records)
    first = snapshot.records[0]
    stored = RunIdentityV1(first.payload["identity"])
    # An `IdentityError` (a producing version the run never admitted, or a
    # corpus it no longer binds) propagates, as `campaign_inspection.
    # release_leftover` lets it: nothing is written, and the operator admits
    # the version and resumes (Ruling 16).
    journal = Journal.resume(
        path,
        str(first.to_payload()["run_id"]),
        stored,
        corpus,
        secrets=secrets,
        admitted_versions=admitted_versions,
    )
    try:
        close_interrupted_operations(journal)
        records = tuple(
            {"event": record.event, **record.payload}
            for record in journal.snapshot().records
            if record.event.startswith(("target_materialize_", "target_release_"))
        )
        # As `campaign_inspection._resume`: one call per instance, no I1
        # resend; a refusal leaves the instance owned in the journal.
        with suppress(RolloutInfrastructureError):
            await recover_target_instances(adapter, records, JournalRolloutAdapter(journal))
        if journal.recovery_required:
            with suppress(JournalRecoveryRequired):
                journal.finish_recovery()
        return _cleanup_of(journal.snapshot().records)
    finally:
        journal.close()
