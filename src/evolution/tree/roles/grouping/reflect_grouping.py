"""Pure, typed grouping primitives for the family-4 reflector.

Public API for the campaign integration:

* :class:`GroupingRecord` is one frozen baseline observation.  It deliberately
  keeps the four permitted projections separate in ``sources``.
* :func:`build_registry` canonicalizes observations and binds their ordered IDs
  to a baseline.
* :func:`builtin_grouping` implements field/JSON-Pointer grouping; custom
  commands return :class:`GroupingResult`, admitted by
  :func:`admit_grouping_result` using the same result rules.
* :func:`cohorts_from_grouping` and :func:`select_portion` derive per-case
  cohorts and a size-first, cycle-aware reading portion.

This module reads no corpus and has no persistence or subprocess dependency.
The caller supplies already-admitted projections and leak predicates.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, NoReturn

from evolution.tree.execution.check_modes import (
    check_field,
    grouping_field_locations,
    quiet_forbidden,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.json_pointer import MISSING, _pointer_tokens, pointer_value

MAX_REGISTRY_RECORDS: Final = 32768
MAX_GROUP_NUMBER: Final = 32767
MAX_RESULT_BYTES: Final = 512 * 1024
MAX_SPARSE_MAP_BYTES: Final = 64 * 1024
_SOURCES: Final = frozenset(("case", "public", "observation", "feedback"))
#: `FieldFeature.source` alone admits one further value beyond the four
#: runtime projections `GroupingRecord.sources` carries: `"diagnostics"`
#: (scorer diagnostics channel design), gated to manifest schema
#: `evolution-v2-manifest/9` by `manifest_v2._parse_grouping`, not by this
#: module. `GroupingRecord`'s own `_SOURCES` stays exactly the four
#: projections until the family-8 runtime wires a fifth.
_FEATURE_SOURCES: Final = _SOURCES | frozenset({"diagnostics"})
_GROUPING_VERSION: Final = "reflect_grouping_result/1"


class GroupingAdmissionError(ValueError):
    """A compact grouping result cannot be safely admitted.

    ``str(error)`` is a stable, bounded machine-facing refusal code.  Callers
    should journal the code, never the untrusted result value.
    """


def _fail(code: str) -> NoReturn:
    raise GroupingAdmissionError(code)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("invalid_json_value") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_text(value: object, code: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or (maximum is not None and len(value) > maximum):
        raise ValueError(code)
    return value


@dataclass(frozen=True)
class GroupingRecord:
    """One individual baseline record and its permitted, frozen projections."""

    evidence_id: str
    case_id: str
    repeat: int
    failing: bool
    available: bool
    sources: Mapping[str, Any]
    #: Optional role-projection lookup for controller-built field text only.
    #: Grouping and its registry identity always use the unfiltered sources.
    role_observation_value: Callable[[str], Any] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "invalid_evidence_id")
        _require_text(self.case_id, "invalid_case_id")
        if isinstance(self.repeat, bool) or not isinstance(self.repeat, int) or self.repeat < 1:
            raise ValueError("invalid_repeat")
        if not isinstance(self.failing, bool) or not isinstance(self.available, bool):
            raise ValueError("invalid_record_flags")
        # Four sources always; a fifth, `diagnostics`, only under family 8
        # (ADR-0021), where the runtime registry's own `_Sources` admits it.
        if not isinstance(self.sources, Mapping) or set(self.sources) not in (
            _SOURCES,
            _FEATURE_SOURCES,
        ):
            raise ValueError("invalid_sources")
        # ``sources`` can be a frozen lazy mapping over substantial baseline
        # material.  Registry construction must never fetch or normalize those
        # projections; selected built-in features do that on demand instead.
        object.__setattr__(self, "sources", MappingProxyType(self.sources))


@dataclass(frozen=True)
class GroupingRegistry:
    """Canonical baseline-bound record registry used by both grouping paths."""

    baseline_binding: str
    records: tuple[GroupingRecord, ...]
    digest: str


def build_registry(baseline_binding: str, records: Sequence[GroupingRecord]) -> GroupingRegistry:
    """Return records in canonical ``(case_id, repeat)`` order.

    Capacity is deliberately checked here too, so every policy path has the
    same hard protocol limit even when manifest preflight was bypassed.
    """

    _require_text(baseline_binding, "invalid_baseline_binding")
    if isinstance(records, (str, bytes, bytearray)):
        raise ValueError("invalid_registry_records")
    try:
        canonical = tuple(sorted(records, key=lambda row: (row.case_id, row.repeat)))
    except (AttributeError, TypeError) as exc:
        raise ValueError("invalid_registry_records") from exc
    if len(canonical) > MAX_REGISTRY_RECORDS:
        raise ValueError(f"reflection_capacity:{len(canonical)}")
    seen_ids: set[str] = set()
    seen_positions: set[tuple[str, int]] = set()
    for row in canonical:
        if not isinstance(row, GroupingRecord):
            raise ValueError("invalid_registry_record")
        if row.evidence_id in seen_ids:
            raise ValueError("duplicate evidence_id")
        position = (row.case_id, row.repeat)
        if position in seen_positions:
            raise ValueError("duplicate case_id/repeat")
        seen_ids.add(row.evidence_id)
        seen_positions.add(position)
    digest = _digest(
        {
            "baseline_binding": baseline_binding,
            "records": [
                {
                    "evidence_id": row.evidence_id,
                    "case_id": row.case_id,
                    "repeat": row.repeat,
                    "failing": row.failing,
                    "available": row.available,
                }
                for row in canonical
            ],
        }
    )
    return GroupingRegistry(baseline_binding, canonical, digest)


@dataclass(frozen=True)
class FieldFeature:
    """A declared typed JSON-Pointer feature for built-in field grouping."""

    name: str
    source: Literal["case", "public", "observation", "feedback", "diagnostics"]
    path: str
    description: str

    def __post_init__(self) -> None:
        _require_text(self.name, "invalid_feature_name", maximum=128)
        if self.source not in _FEATURE_SOURCES:
            raise ValueError("invalid_feature_source")
        if not isinstance(self.path, str) or (self.path and not self.path.startswith("/")):
            raise ValueError("invalid_json_pointer")
        _pointer_tokens(self.path)
        if not isinstance(self.description, str) or len(self.description) > 512:
            raise ValueError("invalid_feature_description")


DEFAULT_FIELD_FEATURES: Final = (
    FieldFeature("failed_criteria", "observation", "/failed_criteria", "Failed criteria."),
    FieldFeature("terminal", "observation", "/terminal", "Terminal observation status."),
    FieldFeature("critical", "observation", "/critical", "Whether the failure is critical."),
)


def typed_json_key(value: Any) -> tuple[Any, ...]:
    """A hashable canonical JSON value with JSON types preserved.

    The missing sentinel and JSON ``null`` remain separate; Python booleans are
    handled before numbers so ``True`` cannot equal ``1``.
    """

    try:
        return _typed_json_key(value)
    except RecursionError as exc:
        raise ValueError("invalid_json_value") from exc


def _typed_json_key(value: Any) -> tuple[Any, ...]:

    if value is MISSING:
        return ("missing",)
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid_json_value")
        return ("number", _canonical_json(value).decode("utf-8"))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_typed_json_key(item) for item in value))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("invalid_json_value")
        return (
            "object",
            tuple((key, _typed_json_key(value[key])) for key in sorted(value)),
        )
    raise ValueError("invalid_json_value")


@dataclass(frozen=True)
class GroupDescription:
    label: str
    description: str


@dataclass(frozen=True)
class GroupingResult:
    """The compact result shared by built-in and command grouping."""

    registry_digest: str
    assignments: Sequence[int | None]
    descriptions: Mapping[int | str, Mapping[str, Any]] = field(default_factory=dict)
    variants: Mapping[int | str, str] = field(default_factory=dict)
    version: str = _GROUPING_VERSION


@dataclass(frozen=True)
class Neighborhood:
    """A canonical failure group.  Its ID ignores local numbers and labels."""

    group_id: str
    group_number: int
    member_indices: tuple[int, ...]
    member_ids: tuple[str, ...]
    description: GroupDescription | None


@dataclass(frozen=True)
class AdmittedGrouping:
    registry: GroupingRegistry
    policy_identity: str
    result: GroupingResult
    neighborhoods: tuple[Neighborhood, ...]
    variants: Mapping[int, str]


def _mapping_key(value: object, code: str) -> int:
    if isinstance(value, bool):
        _fail(code)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    _fail(code)


def _checked_sparse_bytes(value: object, code: str) -> None:
    try:
        if len(_canonical_json(value)) > MAX_SPARSE_MAP_BYTES:
            _fail(code)
    except ValueError:
        _fail(code)


def _scan_strings(
    value: object,
    predicates: Sequence[tuple[str, Callable[[str], bool]]],
    *,
    location: str | None = None,
) -> None:
    if isinstance(value, str):
        for code, predicate in predicates:
            try:
                with check_field(location):
                    forbidden = predicate(value)
            except Exception:
                _fail("invalid_forbidden_string_predicate")
            if forbidden:
                _fail(code)
    elif isinstance(value, Mapping):
        for nested in value.values():
            _scan_strings(nested, predicates)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _scan_strings(nested, predicates)


def _parse_descriptions(
    raw: object,
    used: set[int],
    predicates: Sequence[tuple[str, Callable[[str], bool]]],
    locations: Mapping[tuple[str, ...], str],
) -> dict[int, GroupDescription]:
    if not isinstance(raw, Mapping):
        _fail("invalid_descriptions")
    _checked_sparse_bytes(raw, "description_size")
    parsed: dict[int, GroupDescription] = {}
    for key, entry in raw.items():
        number = _mapping_key(key, "invalid_description_key")
        if number not in used:
            _fail("unused_description")
        if not isinstance(entry, Mapping) or set(entry) != {"label", "description"}:
            _fail("invalid_description")
        label = entry.get("label")
        description = entry.get("description")
        if not isinstance(label, str) or len(label) > 128:
            _fail("invalid_description")
        if not isinstance(description, str) or len(description) > 512:
            _fail("invalid_description")
        _scan_strings(
            label, predicates, location=locations.get(("descriptions", str(key), "label"))
        )
        _scan_strings(
            description,
            predicates,
            location=locations.get(("descriptions", str(key), "description")),
        )
        parsed[number] = GroupDescription(label, description)
    return parsed


def _parse_variants(
    raw: object,
    records: Sequence[GroupingRecord],
    predicates: Sequence[tuple[str, Callable[[str], bool]]],
    *,
    unavailable_unassigned: bool = False,
) -> dict[int, str]:
    if not isinstance(raw, Mapping):
        _fail("invalid_variants")
    _checked_sparse_bytes(raw, "variant_size")
    parsed: dict[int, str] = {}
    for key, label in raw.items():
        index = _mapping_key(key, "variant_index")
        if index < 0 or index >= len(records) or not records[index].failing:
            _fail("variant_index")
        if unavailable_unassigned and not records[index].available:
            _fail("variant_index")
        if not isinstance(label, str) or len(label) > 64:
            _fail("invalid_variant")
        _scan_strings(label, predicates, location=f"variant/{records[index].evidence_id}")
        parsed[index] = label
    return parsed


def _validate_result_shape(result: object) -> GroupingResult:
    if not isinstance(result, GroupingResult):
        _fail("invalid_grouping_result")
    if result.version != _GROUPING_VERSION:
        _fail("unsupported_grouping_version")
    if not isinstance(result.registry_digest, str):
        # Not the evidence check below: a malformed shape needs no baseline evidence.
        _fail("registry_digest_type")
    if isinstance(result.assignments, (str, bytes, bytearray)) or not isinstance(
        result.assignments, Sequence
    ):
        _fail("assignments")
    try:
        if (
            len(
                _canonical_json(
                    {
                        "version": result.version,
                        "registry_digest": result.registry_digest,
                        "assignments": list(result.assignments),
                        "descriptions": result.descriptions,
                        "variants": result.variants,
                    }
                )
            )
            > MAX_RESULT_BYTES
        ):
            _fail("result_size")
    except ValueError:
        _fail("invalid_grouping_result")
    return result


def admit_grouping_result(
    registry: GroupingRegistry,
    result: GroupingResult,
    *,
    policy_identity: str,
    forbidden_string_predicates: Sequence[tuple[str, Callable[[str], bool]]] = (),
    unavailable_unassigned: bool = False,
) -> AdmittedGrouping:
    """Validate, canonicalize, and expand a compact result without corpus reads.

    ``unavailable_unassigned`` is set only by the model grouping policy
    (an unavailable observation belongs to no
    error group). A failing record that is not available must then carry
    `None` and is left out of every neighborhood instead of becoming a
    singleton. The default keeps every earlier policy and family unchanged.
    """

    _require_text(policy_identity, "invalid_policy_identity")
    result = _validate_result_shape(result)
    if result.registry_digest != registry.digest:
        _fail("registry_digest")
    if len(result.assignments) != len(registry.records):
        _fail("assignment_count")
    predicates = tuple(forbidden_string_predicates)
    for pair in predicates:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not callable(pair[1])
        ):
            _fail("invalid_forbidden_string_predicate")
    assignments: list[int | None] = []
    used: set[int] = set()
    for row, value in zip(registry.records, result.assignments, strict=True):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            _fail("invalid_assignment")
        if value is not None and not 0 <= value <= MAX_GROUP_NUMBER:
            _fail("invalid_assignment")
        if not row.failing and value is not None:
            _fail("nonfailing_membership")
        if unavailable_unassigned and not row.available and value is not None:
            _fail("unavailable_membership")
        if row.failing and value is not None:
            used.add(value)
        assignments.append(value)
    locations = grouping_field_locations(
        {"assignments": result.assignments, "descriptions": result.descriptions},
        tuple(row.evidence_id for row in registry.records),
    )
    descriptions = _parse_descriptions(result.descriptions, used, predicates, locations)
    variants = _parse_variants(
        result.variants,
        registry.records,
        predicates,
        unavailable_unassigned=unavailable_unassigned,
    )

    grouped: dict[tuple[str, int], list[int]] = {}
    for index, (row, assignment) in enumerate(zip(registry.records, assignments, strict=True)):
        if not row.failing or (unavailable_unassigned and not row.available):
            continue
        key = ("assigned", assignment) if assignment is not None else ("singleton", index)
        grouped.setdefault(key, []).append(index)
    provisional: list[tuple[str, tuple[int, ...], tuple[str, ...], GroupDescription | None]] = []
    for key, indices in grouped.items():
        member_indices = tuple(indices)
        member_ids = tuple(registry.records[index].evidence_id for index in member_indices)
        group_id = _digest(
            {
                "policy_identity": policy_identity,
                "baseline_binding": registry.baseline_binding,
                "member_ids": sorted(member_ids),
            }
        )
        description = descriptions.get(key[1]) if key[0] == "assigned" else None
        provisional.append((group_id, member_indices, member_ids, description))
    for indices in grouped.values():
        if len({variants[index] for index in indices if index in variants}) > 16:
            _fail("variant_count")
    provisional.sort(key=lambda item: item[0])
    neighborhoods = tuple(
        Neighborhood(group_id, number, indices, member_ids, description)
        for number, (group_id, indices, member_ids, description) in enumerate(provisional)
    )
    canonical_assignments: list[int | None] = [None] * len(registry.records)
    for neighborhood in neighborhoods:
        for index in neighborhood.member_indices:
            canonical_assignments[index] = neighborhood.group_number
    canonical_descriptions: dict[int | str, Mapping[str, Any]] = {
        neighborhood.group_number: {
            "label": neighborhood.description.label,
            "description": neighborhood.description.description,
        }
        for neighborhood in neighborhoods
        if neighborhood.description is not None
    }
    _checked_sparse_bytes(canonical_descriptions, "description_size")
    canonical_result = GroupingResult(
        registry.digest,
        tuple(canonical_assignments),
        canonical_descriptions,
        dict(sorted(variants.items())),
    )
    try:
        if (
            len(
                _canonical_json(
                    {
                        "version": canonical_result.version,
                        "registry_digest": canonical_result.registry_digest,
                        "assignments": list(canonical_result.assignments),
                        "descriptions": canonical_result.descriptions,
                        "variants": canonical_result.variants,
                    }
                )
            )
            > MAX_RESULT_BYTES
        ):
            _fail("result_size")
    except ValueError:
        _fail("invalid_grouping_result")
    return AdmittedGrouping(
        registry, policy_identity, canonical_result, neighborhoods, MappingProxyType(variants)
    )


def short_labels(labels: Iterable[str], *, width: int) -> dict[str, str]:
    """Deterministically shorten labels while preserving distinctions."""
    distinct = sorted(set(labels))
    short = {label: label[:width] for label in distinct}
    if len(set(short.values())) == len(distinct):
        return short
    head = width - 1 - max(2, len(str(len(distinct))))
    return {label: f"{label[:head]}~{n}" for n, label in enumerate(distinct, start=1)}


def fields_group_texts(
    registry: GroupingRegistry,
    assignments: Sequence[int | None],
    features: Sequence[FieldFeature],
    *,
    first_number: int,
    reserved: Mapping[int | str, Mapping[str, Any]],
    screens: Sequence[Callable[[str], bool]],
) -> dict[int, dict[str, str]]:
    """Optional field summaries fitted beside existing model descriptions.

    Group membership is already settled. Missing, unsafe or oversized text
    must never invalidate it; admission performs the final, counted scan.
    """
    members: dict[int, list[GroupingRecord]] = {}
    for row, number in zip(registry.records, assignments, strict=True):
        if number is not None and number >= first_number:
            members.setdefault(number, []).append(row)
    keys: dict[int, str] = {}
    for number, rows in members.items():
        if any(
            typed_json_key(row.role_observation_value(feature.path))
            != typed_json_key(pointer_value(row.sources["observation"], feature.path))
            for row in rows
            if row.role_observation_value is not None
            for feature in features
            if feature.source == "observation"
        ):
            continue
        parts = []
        for feature in features:
            value = pointer_value(rows[0].sources[feature.source], feature.path)
            rendered = "<absent>" if value is MISSING else canonical_json(value).decode("utf-8")
            parts.append(f"{feature.name}={rendered}")
        keys[number] = ", ".join(parts)
    labels = short_labels(keys.values(), width=128)
    width = len(str(max(len(registry.records) - 1, 0)))

    def member_size(number: int | str, value: Mapping[str, Any]) -> int:
        return len(_canonical_json({f"{int(number):0{width}d}": value})) - 2

    count = len(reserved)
    total = sum(member_size(number, value) for number, value in reserved.items())
    texts: dict[int, dict[str, str]] = {}
    question = ". What do they have in common, and why do they fail?"
    for number in sorted(keys):
        rows = members[number]
        cases = len({row.case_id for row in rows})
        prefix = (
            f"{len(rows)} failing observation{'s' if len(rows) != 1 else ''} "
            f"in {cases} case{'s' if cases != 1 else ''} share "
        )
        key = keys[number]
        room = 512 - len(prefix) - len(question)
        if len(key) > room:
            key = key[: room - len("…[cut]")] + "…[cut]"
        entry = {"label": labels[keys[number]], "description": prefix + key + question}
        if any(quiet_forbidden(screen, text) for screen in screens for text in entry.values()):
            continue
        size = member_size(number, entry)
        if 2 + total + size + count > MAX_SPARSE_MAP_BYTES:
            break
        texts[number] = entry
        total += size
        count += 1
    return texts


def builtin_grouping(
    registry: GroupingRegistry,
    features: Sequence[FieldFeature] | None = None,
    *,
    policy_identity: str,
    forbidden_string_predicates: Sequence[tuple[str, Callable[[str], bool]]] = (),
    describe: bool = False,
) -> AdmittedGrouping:
    """Group failing records by the full typed values of declared features."""

    if features is None:
        features = DEFAULT_FIELD_FEATURES
    if isinstance(features, (str, bytes, bytearray)) or not isinstance(features, Sequence):
        raise ValueError("invalid_features")
    if not 1 <= len(features) <= 16 or not all(isinstance(item, FieldFeature) for item in features):
        raise ValueError("invalid_features")
    if len({feature.name for feature in features}) != len(features):
        raise ValueError("duplicate_feature_name")
    groups: dict[tuple[tuple[Any, ...], ...], int] = {}
    assignments: list[int | None] = []
    for row in registry.records:
        if not row.failing:
            assignments.append(None)
            continue
        key = tuple(
            typed_json_key(pointer_value(row.sources[feature.source], feature.path))
            for feature in features
        )
        assignments.append(groups.setdefault(key, len(groups)))
    return admit_grouping_result(
        registry,
        GroupingResult(
            registry.digest,
            tuple(assignments),
            dict(
                fields_group_texts(
                    registry,
                    assignments,
                    features,
                    first_number=0,
                    reserved={},
                    screens=tuple(predicate for _, predicate in forbidden_string_predicates),
                ).items()
            )
            if describe
            else {},
        ),
        policy_identity=policy_identity,
        forbidden_string_predicates=forbidden_string_predicates,
    )


@dataclass(frozen=True)
class Cohort:
    """The records from one case inside one admitted neighborhood.

    `cohort_id` is the durable identity the journal carries and cross-round
    `visited` bookkeeping keys on; it descends from `group_id`, which mixes the
    run's own `baseline_binding` into its digest. `group_key` is the same
    neighborhood's identity derived from its member evidence ids alone, with no
    run-specific bytes, and is what `select_portion` breaks ties by: two
    otherwise identical campaigns must focus the same cases (PDR-0009, ADR-0016
    "equal-size cohorts break ties by cohort content alone", codex round 1). It is never journaled, so families 3 to 5 reduce, audit and
    project exactly as before.
    """

    cohort_id: str
    group_id: str
    case_id: str
    member_indices: tuple[int, ...]
    member_ids: tuple[str, ...]
    variants: Mapping[int, str] = field(default_factory=dict)
    group_key: str = ""


def cohorts_from_grouping(
    grouping: AdmittedGrouping, *, excluded: frozenset[tuple[str, int]] = frozenset()
) -> tuple[Cohort, ...]:
    """Intersect admitted groups with case IDs only after grouping is complete.

    `excluded` names `(case_id, repeat)` pairs the round's baseline could not
    measure: they leave
    every cohort before the focus is selected, and a cohort left with no member
    is dropped, so a round's focus never lands on a case no look can compare.
    The registry, the grouping and the Reflector's own evidence are untouched --
    those samples still count as unavailable there.
    """

    cohorts: list[Cohort] = []
    for neighborhood in grouping.neighborhoods:
        # The neighborhood's content-only identity: its members as the admitted
        # grouping named them, with nothing of this run's own measurement in it.
        group_key = _digest({"member_ids": sorted(neighborhood.member_ids)})
        by_case: dict[str, list[int]] = {}
        for index in neighborhood.member_indices:
            record = grouping.registry.records[index]
            if (record.case_id, record.repeat) in excluded:
                continue
            by_case.setdefault(record.case_id, []).append(index)
        for case_id, indices in by_case.items():
            member_indices = tuple(indices)
            member_ids = tuple(
                grouping.registry.records[index].evidence_id for index in member_indices
            )
            cohort_id = _digest(
                {
                    "group_id": neighborhood.group_id,
                    "case_id": case_id,
                    "member_ids": sorted(member_ids),
                }
            )
            variants = MappingProxyType(
                {
                    index: grouping.variants[index]
                    for index in member_indices
                    if index in grouping.variants
                }
            )
            cohorts.append(
                Cohort(
                    cohort_id,
                    neighborhood.group_id,
                    case_id,
                    member_indices,
                    member_ids,
                    variants,
                    group_key,
                )
            )
    return tuple(
        sorted(cohorts, key=lambda cohort: (cohort.group_key, cohort.case_id, cohort.member_ids))
    )


@dataclass(frozen=True)
class PortionCohort:
    cohort: Cohort
    exemplars: tuple[GroupingRecord, ...]
    passing_contrast: GroupingRecord | None
    omitted_variant_count: int
    unrepresented_variant_ids: tuple[str, ...]


@dataclass(frozen=True)
class PortionSelection:
    cohorts: tuple[Cohort, ...]
    portions: tuple[PortionCohort, ...]
    selected_cohort_ids: tuple[str, ...]
    cycle_reset: bool


def observation_signature(value: Mapping[str, Any]) -> tuple[object, object, object]:
    """Raw `(terminal, critical, failed_criteria)` of an observation, `MISSING` when absent."""
    return (
        value.get("terminal", MISSING),
        value.get("critical", MISSING),
        value.get("failed_criteria", MISSING),
    )


def _variant_signature(row: GroupingRecord, variant: str | None) -> tuple[Any, ...]:
    observation = row.sources["observation"]
    parts = observation_signature(observation if isinstance(observation, Mapping) else {})
    return (
        *(typed_json_key(part) for part in parts),
        typed_json_key(variant if variant is not None else MISSING),
    )


def _portion_for_cohort(
    cohort: Cohort,
    registry: GroupingRegistry,
    variants: Mapping[int, str],
    representatives_per_case: int,
) -> PortionCohort:
    members = [registry.records[index] for index in cohort.member_indices]
    representatives: dict[tuple[Any, ...], GroupingRecord] = {}
    for index, row in zip(cohort.member_indices, members, strict=True):
        signature = _variant_signature(row, variants.get(index))
        current = representatives.get(signature)
        if current is None or (row.repeat, row.evidence_id) < (current.repeat, current.evidence_id):
            representatives[signature] = row
    ordered = tuple(representatives[key] for key in sorted(representatives, key=repr))
    exemplars = ordered[:representatives_per_case]
    contrast = next(
        (
            row
            for row in registry.records
            if row.case_id == cohort.case_id and row.available and not row.failing
        ),
        None,
    )
    unrepresented = tuple(row.evidence_id for row in ordered[representatives_per_case:])
    return PortionCohort(cohort, exemplars, contrast, len(unrepresented), unrepresented)


def select_portion(
    cohorts: Sequence[Cohort],
    *,
    registry: GroupingRegistry,
    visited_in_cycle: frozenset[str] = frozenset(),
    focus_cases: int = 16,
    representatives_per_case: int = 2,
    variants: Mapping[int, str] | None = None,
    rank_observations: bool = False,
) -> PortionSelection:
    """Pick one size-first neighborhood and its first eligible case cohorts.

    If all cohorts have been visited, this pure function clears the effective
    visit set and marks the returned selection as a new cycle.  The campaign is
    responsible for assigning/persisting the next cycle number.

    ``rank_observations`` (family 9, model error grouping) ranks by
    ``(-distinct eligible cases, -eligible failing observations, group_key)``;
    without it the observation count is not consulted, as before.
    """

    if not 1 <= focus_cases <= 64 or not 1 <= representatives_per_case <= 8:
        raise ValueError("invalid_selection_limits")
    if isinstance(cohorts, (str, bytes, bytearray)) or not all(
        isinstance(item, Cohort) for item in cohorts
    ):
        raise ValueError("invalid_cohorts")
    by_group: dict[str, list[Cohort]] = {}
    for cohort in cohorts:
        by_group.setdefault(cohort.group_key, []).append(cohort)
    effective_visited = set(visited_in_cycle)
    unvisited = [cohort for cohort in cohorts if cohort.cohort_id not in effective_visited]
    cycle_reset = bool(cohorts) and not unvisited
    if cycle_reset:
        effective_visited.clear()
    ranked: list[tuple[int, int, str, list[Cohort]]] = []
    for group_key, group_cohorts in by_group.items():
        eligible = [cohort for cohort in group_cohorts if cohort.cohort_id not in effective_visited]
        if eligible:
            # Size first, then the neighborhood's *content* key: the run-specific
            # `group_id` would let two identical campaigns rank equal-size
            # neighborhoods in opposite orders (PDR-0009).
            observations = (
                -sum(
                    registry.records[index].failing
                    for cohort in eligible
                    for index in cohort.member_indices
                )
                if rank_observations
                else 0
            )
            ranked.append(
                (-len({cohort.case_id for cohort in eligible}), observations, group_key, eligible)
            )
    if not ranked:
        return PortionSelection((), (), (), cycle_reset)
    _, _, _, eligible = min(ranked, key=lambda value: (value[0], value[1], value[2]))
    selected = tuple(sorted(eligible, key=lambda cohort: cohort.case_id)[:focus_cases])
    portions = tuple(
        _portion_for_cohort(
            cohort,
            registry,
            cohort.variants if variants is None else variants,
            representatives_per_case,
        )
        for cohort in selected
    )
    return PortionSelection(
        selected, portions, tuple(cohort.cohort_id for cohort in selected), cycle_reset
    )
