"""Strict private-input hashing and immutable native journal run identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from typing import Any

from evolution import __version__
from evolution.tree.execution.interaction import (
    EnvironmentIdentity,
    EvalCase,
    InstanceProvenance,
    RolloutBudget,
    RolloutSample,
    ScorerIdentity,
    SemanticIdentity,
    TargetIdentity,
    condition_key,
)
from evolution.tree.storage.envelope import is_identifier


class IdentityError(ValueError):
    """A safe, value-free identity boundary diagnostic."""


class ScorerRecoveryMode(StrEnum):
    DETERMINISTIC = "deterministic"
    IDEMPOTENT = "idempotent"
    NON_REPLAYABLE = "non_replayable"


def _json_value(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise IdentityError("invalid_identity_json")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise IdentityError("invalid_identity_json")


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityError("invalid_identity_json") from None


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _closed(value: Any, fields: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields.split()):
        raise IdentityError("invalid_identity_fields")
    return dict(value)


def _text(value: Any) -> None:
    if type(value) is not str or not value.strip() or any(ord(c) < 32 for c in value):
        raise IdentityError("invalid_identity_identifier")


def _identifier(value: Any) -> None:
    if type(value) is not str or not is_identifier(value):
        raise IdentityError("invalid_identity_identifier")


def _positive(value: Any) -> None:
    if type(value) is not int or value < 1:
        raise IdentityError("invalid_identity_integer")


def _sha(value: Any) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise IdentityError("invalid_identity_digest")


def case_payload(case: EvalCase) -> dict[str, Any]:
    if not isinstance(case, EvalCase):
        raise IdentityError("invalid_eval_case")
    return _json_value(
        {
            name: getattr(case, name)
            for name in ("case_id", "group_id", "stratum", "tags", "public", "private", "oracle")
        }
    )


def corpus_digest(cases: Sequence[EvalCase]) -> str:
    payloads = [case_payload(case) for case in cases]
    if not payloads or len({case["case_id"] for case in payloads}) != len(payloads):
        raise IdentityError("invalid_corpus")
    return _digest(sorted(payloads, key=lambda case: case["case_id"]))


def _scorer_payload(value: Any) -> dict[str, str]:
    result = _closed(value, "kind digest recovery")
    _identifier(result["kind"])
    _identifier(result["digest"])
    mode = result["recovery"]
    if isinstance(mode, ScorerRecoveryMode):
        mode = mode.value
    if type(mode) is not str or mode not in {item.value for item in ScorerRecoveryMode}:
        raise IdentityError("invalid_scorer_recovery")
    result["recovery"] = mode
    return result


def scorer_binding(scorer: Any) -> dict[str, str]:
    try:
        identity = scorer.identity
        return _scorer_payload(
            dict(
                kind=identity.kind,
                digest=identity.digest,
                recovery=getattr(scorer, "recovery_mode", ScorerRecoveryMode.NON_REPLAYABLE),
            )
        )
    except IdentityError:
        raise
    except Exception:
        raise IdentityError("invalid_scorer_binding") from None


def effective_scorer_identity(scorer: Mapping[str, Any]) -> ScorerIdentity:
    data = _scorer_payload(scorer)
    return ScorerIdentity(
        data["kind"],
        _digest(dict(schema_version=1, component_digest=data["digest"], recovery=data["recovery"])),
    )


def scoring_input_digest(
    case: EvalCase, view: Any, candidate: Any, scorer: Mapping[str, Any]
) -> str:
    paths = tuple(candidate.artifact_paths)
    # A view carrying its own verifier decides membership itself and so names no
    # paths; it encodes an empty list, which no path-mode input can produce, since
    # that mode always names at least one path. Without a verifier an empty list is
    # still an invalid input, and the path-mode encoding is unchanged either way.
    verifier_mode = not paths and getattr(candidate, "verifier", None) is not None
    if not verifier_mode and (
        not paths
        or any(
            type(path) is not str
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            for path in paths
        )
        or len(set(paths)) != len(paths)
    ):
        raise IdentityError("invalid_artifact_path")
    _text(candidate.digest)
    candidate.verify()
    return _digest(
        dict(
            # This five-key envelope has its own schema version. It is distinct
            # from scoring_started.input_schema_version, which versions the
            # trajectory shape rather than this scoring request identity.
            schema_version=1,
            case=case_payload(case),
            view=view.to_payload(),
            candidate=dict(digest=candidate.digest, artifact_paths=sorted(paths)),
            scorer=_scorer_payload(scorer),
        )
    )


def resolve_package_version(resolver: Callable[[str], str] | None = None) -> str:
    try:
        version = (resolver or metadata.version)("evo-skills")
    except Exception:
        raise IdentityError("package_version_unavailable") from None
    if type(version) is not str or version != __version__:
        raise IdentityError("package_version_mismatch")
    return version


_FIELDS = (
    "profile candidate_digest target environment scorer evaluation_set_id "
    "corpus_digest metric_plan_digest schedule budget max_concurrency "
    "max_scorer_concurrency materialization_strategy"
)
# Optional fields enter the canonical payload only when set, so an identity that
# never declares them keeps its pre-existing digest.
_OPTIONAL = (
    "sample_retries",
    "case_subset",
    "case_repeats",
    "row_provenance",
    "connection_binding",
)
#: The connection probe's own S1 journal. It schedules one
#: row, the first train case, which a `case_subset` names by corpus index.
CONNECTION_PREFLIGHT_PROFILE = "connection_preflight/1"
MAX_SAMPLE_RETRIES = 8
_TARGET = (
    "contract_digest dependency_digest settings_hash seed_mode protocol_version runtime_ref_digest"
)


def _case_subset(value: object) -> list[int]:
    """A non-empty, strictly increasing list of non-negative indices.

    Indices point into the campaign identity's `corpus_cases`; `bool` is
    refused although it is an `int`, and nothing is coerced.
    """

    if not isinstance(value, (list, tuple)) or not value:
        raise IdentityError("invalid_case_subset")
    indices = list(value)
    if any(type(index) is not int or index < 0 for index in indices) or any(
        later <= earlier for earlier, later in zip(indices, indices[1:], strict=False)
    ):
        raise IdentityError("invalid_case_subset")
    return indices


def _case_repeats(value: object) -> list[list[int]]:
    """A non-empty list of `[index, count]` integer pairs.

    Sorted by index, each index once, each index non-negative and each count
    at least 2 (it must exceed a base repeat count of at least 1). Whether
    the indices name the identity's cases and the schedule follows the counts
    is `_check_case_repeats`'s. `bool` is refused although it is an `int`.
    """

    if not isinstance(value, (list, tuple)) or not value:
        raise IdentityError("invalid_case_repeats")
    pairs: list[list[int]] = []
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or any(type(number) is not int for number in item)
            or item[0] < 0
            or item[1] < 2
        ):
            raise IdentityError("invalid_case_repeats")
        pairs.append([item[0], item[1]])
    if any(later[0] <= earlier[0] for earlier, later in zip(pairs, pairs[1:], strict=False)):
        raise IdentityError("invalid_case_repeats")
    return pairs


def _check_case_repeats(
    pairs: Sequence[Sequence[int]],
    subset: Sequence[int] | None,
    groups: Mapping[str, Mapping[int, int | None]],
) -> None:
    """The schedule a `case_repeats` selector admits, over its rows alone.

    An index names a case of the identity's case set: a `case_subset` entry,
    or a corpus index without one; the schedule lists its cases in that order
    (corpus order, which `validate_corpus` confirms against the corpus). R is
    the common repeat count of the unlisted cases, at least one of which must
    exist. Every unlisted case has exactly repeats `1..R`, every listed case
    exactly `1..count` with `count > R`, and a repeat's requested seed is the
    same on every case, as in a uniform schedule.
    """

    cases = list(groups)
    indices = list(subset) if subset is not None else list(range(len(cases)))
    position = {index: offset for offset, index in enumerate(indices)}
    if any(index not in position for index, _count in pairs):
        raise IdentityError("invalid_case_repeats")
    listed = {cases[position[index]]: count for index, count in pairs}
    unlisted = [groups[case_id] for case_id in cases if case_id not in listed]
    if not unlisted:
        raise IdentityError("invalid_case_repeats")
    base = unlisted[0]
    repeats = len(base)
    if any(group != base for group in unlisted) or set(base) != set(range(1, repeats + 1)):
        raise IdentityError("invalid_case_repeats")
    seeds = dict(base)
    for case_id, count in listed.items():
        group = groups[case_id]
        if count <= repeats or set(group) != set(range(1, count + 1)):
            raise IdentityError("invalid_case_repeats")
        if any(seeds.setdefault(repeat, seed) != seed for repeat, seed in group.items()):
            raise IdentityError("invalid_case_repeats")


def checked_case_subset(value: object, corpus_size: int) -> tuple[int, ...]:
    """A well-formed selector whose every index names one of `corpus_size` corpus cases."""

    indices = _case_subset(value)
    if indices[-1] >= corpus_size:
        raise IdentityError("invalid_case_subset")
    return tuple(indices)


def checked_case_repeats(value: object, indices: Collection[int]) -> list[list[int]]:
    """A well-formed `case_repeats` whose every index is one of `indices`, the identity's cases."""

    pairs = _case_repeats(value)
    if any(index not in indices for index, _count in pairs):
        raise IdentityError("invalid_case_repeats")
    return pairs


@dataclass(frozen=True, init=False)
class RunIdentityV1:
    """Canonical bytes prevent mutation through nested objects or caller aliases."""

    _canonical: bytes

    def __init__(self, payload: Mapping[str, Any]):
        if not isinstance(payload, Mapping):
            raise IdentityError("invalid_identity_fields")
        optional = {name: payload[name] for name in _OPTIONAL if name in payload}
        data = _closed({k: v for k, v in payload.items() if k not in optional}, _FIELDS)
        if "sample_retries" in optional:
            retries = optional["sample_retries"]
            if (
                type(retries) is not int
                or retries < 0
                or (
                    data["profile"] != CONNECTION_PREFLIGHT_PROFILE and retries > MAX_SAMPLE_RETRIES
                )
            ):
                raise IdentityError("invalid_identity_integer")
            data["sample_retries"] = retries
        if "case_subset" in optional:
            data["case_subset"] = _case_subset(optional["case_subset"])
        if "case_repeats" in optional:
            data["case_repeats"] = _case_repeats(optional["case_repeats"])
        if "row_provenance" in optional:
            # A family-12 measurement's rows may carry provenance.
            # `1` is the only value; `True` is refused although it equals 1.
            if type(optional["row_provenance"]) is not int or optional["row_provenance"] != 1:
                raise IdentityError("invalid_identity_fields")
            data["row_provenance"] = 1
        if "connection_binding" in optional:
            # A family-14 measurement's starts name their
            # connection binding. `1` is the only value, as for `row_provenance`.
            if type(optional["connection_binding"]) is not int or (
                optional["connection_binding"] != 1
            ):
                raise IdentityError("invalid_identity_fields")
            data["connection_binding"] = 1
        if data["profile"] not in (
            "rollout_measurement",
            "rollout_evaluation/1",
            CONNECTION_PREFLIGHT_PROFILE,
        ):
            raise IdentityError("invalid_identity_profile")
        if "connection_binding" in data and data["profile"] != "rollout_measurement":
            # Only a measurement has an admission boundary; the probe journal is
            # what admits, and standalone evaluation `/1` is unchanged.
            raise IdentityError("invalid_identity_fields")
        if data["profile"] == CONNECTION_PREFLIGHT_PROFILE and "case_subset" not in data:
            raise IdentityError("invalid_case_subset")
        for name in ("candidate_digest", "evaluation_set_id"):
            _identifier(data[name])
        for name in ("corpus_digest", "metric_plan_digest"):
            _sha(data[name])
        target = _closed(data["target"], _TARGET)
        for value in target.values():
            _identifier(value)
        if target["seed_mode"] not in ("exact", "unpaired", "none"):
            raise IdentityError("invalid_seed_mode")
        env = _closed(data["environment"], "kind digest")
        for value in env.values():
            _identifier(value)
        data["scorer"] = _scorer_payload(data["scorer"])
        budget = _closed(data["budget"], "max_turns timeout_seconds")
        try:
            RolloutBudget(**budget)
        except (ValueError, TypeError, OverflowError):
            raise IdentityError("invalid_identity_budget") from None
        for name in ("max_concurrency", "max_scorer_concurrency"):
            _positive(data[name])
        if data["materialization_strategy"] not in (
            "adapter_apply_manifest",
            "filesystem_overlay",
            "artifact_root_injection",
        ):
            raise IdentityError("invalid_materialization_strategy")
        schedule = data["schedule"]
        if not isinstance(schedule, (list, tuple)) or not schedule:
            raise IdentityError("invalid_schedule")
        if data["profile"] == CONNECTION_PREFLIGHT_PROFILE and len(schedule) != 1:
            raise IdentityError("invalid_schedule")
        seen: set[tuple[str, str]] = set()
        groups: dict[str, dict[int, int | None]] = {}
        for item in schedule:
            row = _closed(item, "case_id sample_id repeat requested_seed")
            _identifier(row["case_id"])
            _identifier(row["sample_id"])
            _positive(row["repeat"])
            seed = row["requested_seed"]
            if seed is not None and type(seed) is not int:
                raise IdentityError("invalid_seed")
            if (target["seed_mode"] == "exact") != (seed is not None):
                raise IdentityError("invalid_seed")
            key = (row["case_id"], row["sample_id"])
            repeats = groups.setdefault(row["case_id"], {})
            if key in seen or row["repeat"] in repeats:
                raise IdentityError("duplicate_schedule_entry")
            seen.add(key)
            repeats[row["repeat"]] = seed
        subset = data.get("case_subset")
        if "case_repeats" in data:
            # The selector replaces the uniform-schedule rule. The
            # case count is checked first, since the selector's indices name
            # the identity's cases by position.
            if subset is not None and len(subset) != len(groups):
                raise IdentityError("invalid_case_subset")
            _check_case_repeats(data["case_repeats"], subset, groups)
        else:
            expected = next(iter(groups.values()))
            if any(group != expected for group in groups.values()) or set(expected) != set(
                range(1, len(expected) + 1)
            ):
                raise IdentityError("nonuniform_schedule")
            if subset is not None and len(subset) != len(groups):
                raise IdentityError("invalid_case_subset")
        object.__setattr__(self, "_canonical", canonical_json(data))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RunIdentityV1:
        return cls(payload)

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def sample_retries(self) -> int | None:
        """Further tries admitted per scheduled sample; `None` admits none."""
        value = self.to_payload().get("sample_retries")
        return value if type(value) is int else None

    @property
    def case_subset(self) -> tuple[int, ...] | None:
        """The selected `corpus_cases` indices; `None` for a full-corpus identity."""

        value = self.to_payload().get("case_subset")
        return None if value is None else tuple(value)

    @property
    def row_provenance(self) -> bool:
        """Whether this measurement's rows may carry provenance (family 12)."""

        return self.to_payload().get("row_provenance") == 1

    @property
    def connection_binding(self) -> bool:
        """Whether this measurement's starts name their connection binding (family 14)."""

        return self.to_payload().get("connection_binding") == 1

    def require_retry_ordinal(self, retry_ordinal: int | None, *, repair: bool = False) -> None:
        """Refuse an ordinal this identity cannot journal, before anything is written.

        An identity without `sample_retries` numbers no try (`None`); one with it
        numbers every sample try in `0..sample_retries`, and a repair try the
        next ordinal past its sample's closed fault, which is outside
        `sample_retries` and which the journal's own lifecycle bounds. A caller
        passing anything else is a bug, and a plain `ValueError` names it instead
        of the journal's admission fault poisoning the writer.
        """
        retries = self.sample_retries
        if retries is None:
            if retry_ordinal is None and not repair:
                return
        elif type(retry_ordinal) is int and (
            retry_ordinal >= 1 if repair else 0 <= retry_ordinal <= retries
        ):
            return
        raise ValueError("retry_ordinal_inconsistent")

    def validate_corpus(self, cases: Sequence[EvalCase]) -> dict[str, EvalCase]:
        """Bind `cases` to the corpus commitment and return the cases this identity schedules.

        A subset identity still commits to the whole corpus
        (`corpus_digest`); its `case_subset` selects the scheduled cases, and
        only those are returned, so a call or a score for any other case is
        refused by its caller as today. The selector's indices are positions in
        the campaign's corpus order (`corpus_cases`); a corpus supplied in
        another order with the same digest selects other positions and is
        refused (`run_identity_changed`), never silently misread.
        """

        data = self.to_payload()
        if corpus_digest(cases) != data["corpus_digest"]:
            raise IdentityError("run_identity_changed")
        subset = data.get("case_subset")
        selected = (
            list(cases)
            if subset is None
            else [cases[index] for index in checked_case_subset(subset, len(cases))]
        )
        result = {case.case_id: case for case in selected}
        if set(result) != {row["case_id"] for row in data["schedule"]}:
            raise IdentityError("run_identity_changed")
        pairs = data.get("case_repeats")
        if pairs is not None:
            # Each case's repeat count is the derived one -- its
            # listed count, or the unlisted cases' R -- over the corpus itself,
            # so a schedule whose case order departs from the corpus' is refused.
            counts = {case_id: 0 for case_id in result}
            for row in data["schedule"]:
                counts[row["case_id"]] += 1
            listed = {cases[index].case_id: count for index, count in pairs}
            base = min(counts.values())
            if counts != {case_id: listed.get(case_id, base) for case_id in result}:
                raise IdentityError("run_identity_changed")
        return result

    def validate_call(
        self,
        *,
        case: EvalCase,
        sample: RolloutSample,
        target: SemanticIdentity | TargetIdentity,
        environment: EnvironmentIdentity,
        scorer: Any,
        budget: RolloutBudget,
        corpus: Sequence[EvalCase],
    ) -> None:
        """Bind actual in-memory inputs before a durable start authorizes dispatch."""
        bound = self.validate_corpus(corpus)
        data = self.to_payload()
        semantic = target.semantic if isinstance(target, TargetIdentity) else target
        rows = [
            row
            for row in data["schedule"]
            if row["case_id"] == case.case_id and row["sample_id"] == sample.sample_id
        ]
        if (
            case.case_id not in bound
            or canonical_json(case_payload(case))
            != canonical_json(case_payload(bound[case.case_id]))
            or len(rows) != 1
            or sample.candidate_digest != data["candidate_digest"]
            or sample.requested_seed != rows[0]["requested_seed"]
            or semantic != SemanticIdentity(**data["target"])
            or environment != EnvironmentIdentity(**data["environment"])
            or scorer_binding(scorer) != data["scorer"]
            or canonical_json(
                dict(max_turns=budget.max_turns, timeout_seconds=budget.timeout_seconds)
            )
            != canonical_json(data["budget"])
        ):
            raise IdentityError("run_identity_changed")

    def condition_key(self, target: TargetIdentity | None = None) -> tuple[Any, ...]:
        data = self.to_payload()
        semantic = SemanticIdentity(**data["target"])
        if target is not None and target.semantic != semantic:
            raise IdentityError("run_identity_changed")
        # Instance provenance is deliberately absent from the existing comparator.
        if target is None:
            target = TargetIdentity(
                semantic,
                InstanceProvenance("comparison-only", data["candidate_digest"]),
                frozenset(),
            )
        # The base schedule's seeds: under `case_repeats` a listed
        # case has more repeats than R, so the key reads a case with the fewest,
        # which without the selector is the first case, as always.
        counts: dict[str, int] = {}
        for row in data["schedule"]:
            counts[row["case_id"]] = counts.get(row["case_id"], 0) + 1
        base = min(counts, key=counts.__getitem__)
        rows = [row for row in data["schedule"] if row["case_id"] == base]
        seeds = tuple(row["requested_seed"] for row in sorted(rows, key=lambda row: row["repeat"]))
        return condition_key(
            target,
            EnvironmentIdentity(**data["environment"]),
            effective_scorer_identity(data["scorer"]),
            data["corpus_digest"],
            len(seeds),
            seeds,
            data["metric_plan_digest"],
        )

    def measurement_key(self, target: TargetIdentity | None = None) -> tuple[Any, ...]:
        return (self.condition_key(target), self.to_payload()["candidate_digest"])

    def validate_resume(
        self,
        requested: RunIdentityV1,
        *,
        stored_digest: str,
        stored_run_id: str,
        requested_run_id: str,
        producing_version: str,
        cases: Sequence[EvalCase],
        version_resolver: Callable[[str], str] | None = None,
        admitted_versions: Collection[str] = (),
    ) -> None:
        _identifier(stored_run_id)
        _identifier(requested_run_id)
        if self.digest != stored_digest:
            raise IdentityError("stored_identity_digest_mismatch")
        # ADR-0015: a sibling written by an older version resumes only when the
        # campaign's own run-level rebase records admitted that version.
        current = resolve_package_version(version_resolver)
        if producing_version != current and producing_version not in admitted_versions:
            raise IdentityError("package_version_mismatch")
        requested.validate_corpus(cases)
        if stored_run_id != requested_run_id or self._canonical != requested._canonical:
            raise IdentityError("run_identity_changed")
