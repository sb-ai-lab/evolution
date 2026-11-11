"""Pure half of model grouping (PDR-0018, ADR-0022): no I/O, no provider, no
clock. Every order is canonical so the result is independent of concurrency
and provider response order.

This module builds the deterministic case profiles, per-case bundles and
character-bounded chunks that feed the dedicated grouping-model role
It reads only the three cheap ``observation``
header fields (``terminal``, ``critical``, ``failed_criteria``) for every
row, plus ``primary_score`` for every *available* row -- both are the same
lazy paths the campaign runtime already uses (`reflection_inputs.py`). It
never reads a full observation body, ``feedback``, ``public`` or
``diagnostics`` except for the small set of chosen representative and
contrast observations, and never for an unavailable row.

The second half parses a grouping model's map and reduce answers into error
groups, validates exact failing-observation coverage, resolves overlaps and
merges, and projects the admitted groups -- plus a declared ``fields`` or a
singleton fallback -- into the existing compact `GroupingResult` through one
`admit_grouping_result` call. Defects it
reports are short machine codes that echo at most a validated identifier,
never the model's free text. The async send runtime lives
in `model_grouping_runtime.py` and only imports from here.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any, Final, Literal, Protocol

from evolution.tree.execution.json_pointer import pointer_value
from evolution.tree.roles.grouping.reflect_grouping import (
    DESCRIPTION_CHARS,
    DESCRIPTION_FIELD_CHARS,
    DESCRIPTION_FIELD_PREFIXES,
    FAMILY15_DESCRIPTION_CHARS,
    MAX_SPARSE_MAP_BYTES,
    AdmittedGrouping,
    FieldFeature,
    GroupingRecord,
    GroupingRegistry,
    GroupingResult,
    _canonical_json,
    _cut,
    _digest,
    admit_grouping_result,
    fields_group_texts,
    observation_signature,
    short_labels,
    typed_json_key,
)

__all__ = [
    "BUNDLE_OVERHEAD",
    "REPRESENTATIVE_CHARS_FLOOR",
    "REPRESENTATIVE_CHARS_START",
    "ObservationRow",
    "CaseProfile",
    "CaseBundle",
    "InputTooLarge",
    "build_case_profiles",
    "build_bundle",
    "chunk_bundles",
    "ModelGroup",
    "ParsedResponse",
    "FallbackOutcome",
    "parse_map_response",
    "parse_portion_answer",
    "covered",
    "resolve_overlaps",
    "parse_reduce_response",
    "AppliedMerges",
    "apply_merges",
    "project",
]

#: No target publishes provenance in this delivery: every case
#: profile carries this fixed value rather than a per-case read. A plain
#: `dict` (not `MappingProxyType`) so it stays directly JSON-serializable
#: when a bundle renders it; nothing here ever mutates it.
_PROVENANCE_UNAVAILABLE: Final[Mapping[str, Any]] = {"state": "unavailable"}

#: Per-representative text budget (`verdicts`, `answer_text`, `feedback`,
#: `diagnostics`): starts generous and halves until the bundle fits, then
#: representatives are dropped from the end instead of shrinking further.
REPRESENTATIVE_CHARS_START: Final = 4000
REPRESENTATIVE_CHARS_FLOOR: Final = 256

#: Fixed per-bundle allowance `chunk_bundles` charges on top of a bundle's own
#: text length, covering the chunk's own JSON array/object punctuation.
BUNDLE_OVERHEAD: Final = 64

_TRUNCATION_MARKER: Final = "[truncated]"


@dataclass(frozen=True)
class ObservationRow:
    """One observation's cheap header projection plus its finite metric.

    ``terminal``, ``critical`` and ``failed_criteria`` are read from the
    authenticated observation header for every row, including unavailable
    ones -- that read never realizes a trace body. ``metric`` is the
    observation's ``primary_score`` when finite, read only for available
    rows; an unavailable row's ``metric`` is always `None` without any read.
    """

    evidence_id: str
    repeat: int
    state: Literal["failing", "passing", "unavailable"]
    terminal: object
    critical: object
    failed_criteria: object
    metric: float | None
    signature: str


@dataclass(frozen=True)
class CaseProfile:
    """A deterministic aggregate of every observation for one case."""

    case_id: str
    total: int
    available: int
    passing: int
    failing: int
    unavailable: int
    terminal_counts: dict[str, int]
    failed_criterion_counts: dict[str, int]
    metric_name: str
    metric_values: list[tuple[float, int]]
    rows: tuple[ObservationRow, ...]
    representatives: tuple[str, ...]
    contrast: str | None
    omitted_failing: int
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class CaseBundle:
    """One case's bounded, canonical-JSON grouping-model input text."""

    case_id: str
    text: str
    failing_ids: tuple[str, ...]


class InputTooLarge(ValueError):
    """A case bundle exceeds ``max_chars`` even with no representatives."""


def _text_key(value: object) -> str:
    """A stable string dict key for an arbitrary header value.

    Plain strings (the common case: terminal states, criterion names) are
    used as-is; anything else falls back to its canonical JSON rendering so
    the key stays deterministic and JSON-round-trippable.
    """

    if isinstance(value, str):
        return value
    return _canonical_json(value).decode("utf-8")


def _finite_metric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _observation_row(record: GroupingRecord) -> ObservationRow:
    observation = record.sources["observation"]
    terminal, critical, failed_criteria = signature_parts = observation_signature(observation)
    signature = _digest(list(signature_parts))
    if not record.available:
        return ObservationRow(
            record.evidence_id,
            record.repeat,
            "unavailable",
            terminal,
            critical,
            failed_criteria,
            None,
            signature,
        )
    state: Literal["failing", "passing"] = "failing" if record.failing else "passing"
    metric = _finite_metric(observation["primary_score"])
    return ObservationRow(
        record.evidence_id,
        record.repeat,
        state,
        terminal,
        critical,
        failed_criteria,
        metric,
        signature,
    )


def _terminal_counts(rows: Sequence[ObservationRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.state == "unavailable":
            continue
        key = _text_key(row.terminal)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _failed_criterion_counts(rows: Sequence[ObservationRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.state != "failing":
            continue
        criteria = row.failed_criteria
        if not isinstance(criteria, (list, tuple)):
            continue
        for item in criteria:
            key = _text_key(item)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _metric_values(rows: Sequence[ObservationRow]) -> list[tuple[float, int]]:
    counts: dict[float, int] = {}
    for row in rows:
        if row.metric is not None:
            counts[row.metric] = counts.get(row.metric, 0) + 1
    return sorted(counts.items())


def _select_representatives(
    failing_rows: Sequence[ObservationRow], representatives_per_case: int
) -> tuple[tuple[str, ...], int]:
    """Rank signatures by (failing count desc, signature asc); fill, then cap.

    One representative -- the lowest ``(repeat, evidence_id)`` row -- is taken
    per signature in that rank order. Any remaining capacity is filled with
    the other failing rows in ``(repeat, evidence_id)`` order.
    """

    by_signature: dict[str, list[ObservationRow]] = {}
    for row in failing_rows:
        by_signature.setdefault(row.signature, []).append(row)
    ranked_signatures = sorted(by_signature, key=lambda sig: (-len(by_signature[sig]), sig))
    chosen: list[str] = []
    chosen_ids: set[str] = set()
    for signature in ranked_signatures:
        if len(chosen) >= representatives_per_case:
            break
        representative = min(by_signature[signature], key=lambda row: (row.repeat, row.evidence_id))
        chosen.append(representative.evidence_id)
        chosen_ids.add(representative.evidence_id)
    if len(chosen) < representatives_per_case:
        remaining = sorted(
            (row for row in failing_rows if row.evidence_id not in chosen_ids),
            key=lambda row: (row.repeat, row.evidence_id),
        )
        for row in remaining:
            if len(chosen) >= representatives_per_case:
                break
            chosen.append(row.evidence_id)
    return tuple(chosen), len(failing_rows) - len(chosen)


def _select_contrast(rows: Sequence[ObservationRow]) -> str | None:
    passing = [row for row in rows if row.state == "passing"]
    if not passing:
        return None
    return min(passing, key=lambda row: (row.repeat, row.evidence_id)).evidence_id


def build_case_profiles(
    registry: GroupingRegistry, *, metric_primary: str, representatives_per_case: int
) -> tuple[CaseProfile, ...]:
    """Return one profile per case with at least one failing row, by ``case_id``.

    ``registry.records`` is already canonical ``(case_id, repeat)`` order
    (`build_registry`), so grouping by ``case_id`` here preserves it.
    """

    if isinstance(representatives_per_case, bool) or not isinstance(representatives_per_case, int):
        raise ValueError("invalid_representatives_per_case")
    if representatives_per_case < 1:
        raise ValueError("invalid_representatives_per_case")
    profiles: list[CaseProfile] = []
    for case_id, group in groupby(registry.records, key=lambda record: record.case_id):
        rows = tuple(_observation_row(record) for record in group)
        failing_rows = [row for row in rows if row.state == "failing"]
        if not failing_rows:
            continue
        passing = sum(1 for row in rows if row.state == "passing")
        unavailable = sum(1 for row in rows if row.state == "unavailable")
        representatives, omitted_failing = _select_representatives(
            failing_rows, representatives_per_case
        )
        profiles.append(
            CaseProfile(
                case_id=case_id,
                total=len(rows),
                available=len(rows) - unavailable,
                passing=passing,
                failing=len(failing_rows),
                unavailable=unavailable,
                terminal_counts=_terminal_counts(rows),
                failed_criterion_counts=_failed_criterion_counts(rows),
                metric_name=metric_primary,
                metric_values=_metric_values(rows),
                rows=rows,
                representatives=representatives,
                contrast=_select_contrast(rows),
                omitted_failing=omitted_failing,
                provenance=_PROVENANCE_UNAVAILABLE,
            )
        )
    return tuple(profiles)


def _compact_row(row: ObservationRow) -> dict[str, Any]:
    return {
        "evidence_id": row.evidence_id,
        "repeat": row.repeat,
        "state": row.state,
        "terminal": row.terminal,
        "critical": row.critical,
        "failed_criteria": row.failed_criteria,
        "metric": row.metric,
        "signature": row.signature,
    }


def _render_text(value: object, per_rep: int) -> str:
    rendered = _canonical_json(value).decode("utf-8")
    if len(rendered) > per_rep:
        return rendered[:per_rep] + _TRUNCATION_MARKER
    return rendered


def _representative_payload(record: GroupingRecord, per_rep: int) -> dict[str, Any]:
    observation = record.sources["observation"]
    payload: dict[str, Any] = {
        "evidence_id": record.evidence_id,
        "verdicts": _render_text(observation["verdicts"], per_rep),
        "failed_criteria": observation["failed_criteria"],
        "answer_text": _render_text(observation["answer_text"], per_rep),
        "feedback": _render_text(record.sources["feedback"], per_rep),
    }
    if "diagnostics" in record.sources:
        payload["diagnostics"] = _render_text(record.sources["diagnostics"], per_rep)
    return payload


def build_bundle(profile: CaseProfile, registry: GroupingRegistry, *, max_chars: int) -> CaseBundle:
    """Render one case's bounded canonical-JSON grouping-model input.

    Compact per-observation rows (and the failing IDs they cover) are never
    dropped or shrunk. Compaction then proceeds in a fixed order, cheapest
    evidence first: (1) representative
    and contrast text fields shrink together from `REPRESENTATIVE_CHARS_START`,
    halving to `REPRESENTATIVE_CHARS_FLOOR`; (2) at the floor and still too
    large, the optional same-case contrast is dropped entirely (`contrast:
    null`, `contrast_omitted: true`) -- it is supporting context, not failing
    evidence; (3) still too large, representatives are
    dropped from the end (raising `omitted_failing`) until it fits. If the
    body with no contrast and no representatives still exceeds `max_chars`,
    `InputTooLarge` is raised.
    """

    records_by_id = {
        record.evidence_id: record
        for record in registry.records
        if record.case_id == profile.case_id
    }
    compact_rows = [_compact_row(row) for row in profile.rows]
    failing_ids = tuple(row.evidence_id for row in profile.rows if row.state == "failing")
    representative_ids = list(profile.representatives)
    omitted_failing = profile.omitted_failing
    has_contrast = profile.contrast is not None
    contrast_dropped = False
    per_rep = REPRESENTATIVE_CHARS_START
    while True:
        representatives_payload = [
            _representative_payload(records_by_id[evidence_id], per_rep)
            for evidence_id in representative_ids
        ]
        contrast_payload = (
            _representative_payload(records_by_id[profile.contrast], per_rep)
            if profile.contrast is not None and not contrast_dropped
            else None
        )
        body = {
            "case_id": profile.case_id,
            "total": profile.total,
            "available": profile.available,
            "passing": profile.passing,
            "failing": profile.failing,
            "unavailable": profile.unavailable,
            "terminal_counts": profile.terminal_counts,
            "failed_criterion_counts": profile.failed_criterion_counts,
            "metric_name": profile.metric_name,
            "metric_values": profile.metric_values,
            "rows": compact_rows,
            "representatives": representatives_payload,
            "contrast": contrast_payload,
            "contrast_omitted": has_contrast and contrast_dropped,
            "omitted_failing": omitted_failing,
            "provenance": profile.provenance,
        }
        text = _canonical_json(body).decode("utf-8")
        if len(text) <= max_chars:
            return CaseBundle(profile.case_id, text, failing_ids)
        if per_rep > REPRESENTATIVE_CHARS_FLOOR:
            per_rep = max(REPRESENTATIVE_CHARS_FLOOR, per_rep // 2)
            continue
        if has_contrast and not contrast_dropped:
            contrast_dropped = True
            continue
        if representative_ids:
            representative_ids.pop()
            omitted_failing += 1
            continue
        raise InputTooLarge(f"case_bundle_too_large:{profile.case_id}")


def chunk_bundles(
    bundles: Sequence[CaseBundle], *, max_chars: int
) -> tuple[tuple[CaseBundle, ...], ...]:
    """Greedily pack bundles into order-preserving chunks; never split one.

    Each bundle costs its text length plus `BUNDLE_OVERHEAD`. A chunk with at
    least one bundle already in it is closed before a bundle that would push
    it over `max_chars`; an oversized single bundle still forms its own
    chunk rather than being split.
    """

    chunks: list[tuple[CaseBundle, ...]] = []
    current: list[CaseBundle] = []
    current_cost = 0
    for bundle in bundles:
        cost = len(bundle.text) + BUNDLE_OVERHEAD
        if current and current_cost + cost > max_chars:
            chunks.append(tuple(current))
            current = []
            current_cost = 0
        current.append(bundle)
        current_cost += cost
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


# --- Response parsing, admission and projection --------

_TITLE_CHARS: Final = 128
#: Each described header field's admitted bound in the Grouper's answer: the
#: same constant the family-15 description bound derives from, so the answer
#: bounds and the projection bound cannot drift (ADR-0057).
_HEADER_CHARS: Final = DESCRIPTION_FIELD_CHARS
_PRIORITY_CHARS: Final = 256
_VARIANT_CHARS: Final = 64
_MAX_VARIANTS: Final = 16
_MAX_CITATIONS: Final = 16
_CITATION_CHARS: Final = 256
_MAX_METRICS: Final = 16
_METRIC_NAME_CHARS: Final = 128
_PROVENANCE_CHARS: Final = 512
#: The description cut outside family 15 (ADR-0057 removes it for family 15,
#: which projects whole and admits up to `FAMILY15_DESCRIPTION_CHARS`).
_DESCRIPTION_CHARS: Final = DESCRIPTION_CHARS
_TOKEN_CHARS: Final = 64
_MAX_DEFECTS: Final = 32

_REQUIRED_KEYS: Final = frozenset(
    (
        "id",
        "title",
        "failure_mode",
        "expected_behaviour",
        "question",
        "members",
        "affected_cases",
        "citations",
        "priority",
    )
)
_OPTIONAL_KEYS: Final = frozenset(("metrics", "provenance"))
_HEADER_BOUNDS: Final = (
    ("title", _TITLE_CHARS),
    ("failure_mode", _HEADER_CHARS),
    ("expected_behaviour", _HEADER_CHARS),
    ("question", _HEADER_CHARS),
)
_REDUCE_HEADER_KEYS: Final = frozenset(
    ("title", "failure_mode", "expected_behaviour", "question", "priority")
)
#: Only a string made of these characters is ever echoed in a defect; any
#: other untrusted text (prose, whitespace, quotes) is named by code alone.
_SAFE_TOKEN: Final = re.compile(r"[A-Za-z0-9_.:#%~+/@=-]+")
_FENCE: Final = re.compile(r"\A\s*```(?:json)?[ \t]*\n(.*)\n[ \t]*```\s*\Z", re.DOTALL)


@dataclass(frozen=True)
class ModelGroup:
    """One error group as the grouping model described it.

    ``members`` holds ``(evidence_id, variant_label)`` pairs; ``member_cases``
    is the parallel tuple of each member's ``case_id``, kept so that trimming a
    member (`resolve_overlaps`) can recompute ``affected_cases`` exactly.
    ``local_id`` is the model's own label and never a durable identity.
    """

    local_id: str
    title: str
    failure_mode: str
    expected_behaviour: str
    question: str
    members: tuple[tuple[str, str], ...]
    affected_cases: tuple[str, ...]
    citations: tuple[str, ...]
    priority: str
    metrics: tuple[tuple[str, float], ...]
    member_cases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.member_cases) != len(self.members):
            raise ValueError("member_cases")


@dataclass(frozen=True)
class ParsedResponse:
    groups: tuple[ModelGroup, ...]
    defects: tuple[str, ...]
    #: `model_grouping/2` only: the optional `provenance` hint of each kept
    #: group, as ``(local_id, text)`` in group order. A `ModelGroup` carries
    #: no provenance: it is kept in the portion checkpoint, never projected.
    provenance: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FallbackOutcome:
    kind: Literal["fields", "singletons"]
    scope: Literal["uncovered", "all"]
    reason: str


def _token(value: object) -> str | None:
    """A validated short identifier for a defect, or `None` if unsafe."""

    if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
        return value[:_TOKEN_CHARS]
    return None


def _named(code: str, value: object) -> str:
    token = _token(value)
    return code if token is None else f"{code}:{token}"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_key")
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("non_finite_number")


_DECODER: Final = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
)


def _extract_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """The first top-level ``{...}`` after stripping one surrounding fence."""

    fenced = _FENCE.match(text)
    body = fenced.group(1) if fenced else text
    start = body.find("{")
    if start < 0:
        try:
            _DECODER.decode(body.strip())
        except ValueError:
            return None, "invalid_json"
        return None, "not_an_object"
    try:
        value, _ = _DECODER.raw_decode(body, start)
    except (ValueError, RecursionError):
        return None, "invalid_json"
    return value, None


def _bounded_text(value: object, maximum: int, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and (allow_empty or bool(value)) and len(value) <= maximum


def _parse_metrics(value: object) -> tuple[tuple[str, float], ...] | None:
    """Optional ``{name: finite number}``; uninterpreted, no direction."""

    if value is None:
        return ()
    if not isinstance(value, dict) or len(value) > _MAX_METRICS:
        return None
    metrics: list[tuple[str, float]] = []
    for name, number in value.items():
        if not _bounded_text(name, _METRIC_NAME_CHARS):
            return None
        finite = _finite_metric(number)
        if finite is None:
            return None
        metrics.append((name, finite))
    return tuple(sorted(metrics))


def _parse_citations(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > _MAX_CITATIONS:
        return None
    if not all(_bounded_text(item, _CITATION_CHARS, allow_empty=True) for item in value):
        return None
    return tuple(value)


@dataclass(frozen=True)
class _ChunkContext:
    failing: frozenset[str]
    case_of: Mapping[str, str]
    provenance_available: bool


def _parse_members(
    value: object, local: str, ctx: _ChunkContext, defects: list[str]
) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, list) or not value:
        defects.append(f"invalid_members:{local}")
        return None
    members: list[tuple[str, str]] = []
    seen: set[str] = set()
    ok = True
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"evidence_id", "variant"}:
            defects.append(f"invalid_members:{local}")
            return None
        evidence_id, variant = entry["evidence_id"], entry["variant"]
        if not isinstance(evidence_id, str) or evidence_id not in ctx.case_of:
            defects.append(_named("unknown_member", evidence_id))
            ok = False
        elif evidence_id not in ctx.failing:
            defects.append(_named("ineligible_member", evidence_id))
            ok = False
        elif evidence_id in seen:
            defects.append(_named("repeated_member", evidence_id))
            ok = False
        if not _bounded_text(variant, _VARIANT_CHARS):
            defects.append(f"invalid_variant:{local}")
            ok = False
        if isinstance(evidence_id, str):
            seen.add(evidence_id)
        members.append((evidence_id, variant))
    if ok and len({variant for _, variant in members}) > _MAX_VARIANTS:
        defects.append(f"variant_count:{local}")
        ok = False
    return tuple(members) if ok else None


class _PortionUnit(Protocol):
    """What `parse_portion_answer` reads of a `grouping_portions.Unit`."""

    @property
    def ref(self) -> str: ...
    @property
    def case_id(self) -> str: ...
    @property
    def observations(self) -> tuple[int, ...]: ...


class _Portion(Protocol):
    """What `parse_portion_answer` reads of a `grouping_portions.MapPortion`
    (the planner imports this module, so it is named structurally)."""

    @property
    def units(self) -> Sequence[_PortionUnit]: ...
    def references(self) -> dict[str, tuple[int, ...]]: ...


class _RefTable(Protocol):
    """What `parse_portion_answer` reads of a `grouping_portions.RefTable`."""

    @property
    def observations(self) -> Sequence[str]: ...
    def o_ref(self, i: int) -> str: ...


#: `model_grouping/2` (b16 spec §6.3): cases follow from the members.
_PORTION_REQUIRED_KEYS: Final = _REQUIRED_KEYS - {"affected_cases"}
#: What may separate the component IDs a group's `provenance` names.
_PROVENANCE_SEPARATORS: Final = frozenset(" \t\r\n,;")


def _parse_portion_members(
    value: object, local: str, refs: Mapping[str, tuple[int, ...]], defects: list[str]
) -> tuple[tuple[int, str], ...] | None:
    """A `model_grouping/2` group's members expanded to ``(o-ref index, variant)``.

    A set reference stands for every member, in o-ref order. A reference
    that is not a string or not valid in the portion is ``unknown_ref``; one
    whose observations an earlier reference of the group already listed (the
    same reference again, or a set and one of its own o-refs, in either
    order) is ``repeated_member``. Either invalidates the group, as
    `_parse_members`' ``unknown_member`` does.
    """

    if not isinstance(value, list) or not value:
        defects.append(f"invalid_members:{local}")
        return None
    expanded: list[tuple[int, str]] = []
    claimed: set[int] = set()
    ok = True
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"ref", "variant"}:
            defects.append(f"invalid_members:{local}")
            return None
        ref, variant = entry["ref"], entry["variant"]
        indices = refs.get(ref) if isinstance(ref, str) else None
        if indices is None:
            defects.append(_named("unknown_ref", ref))
            ok = False
        elif claimed.intersection(indices):
            defects.append(_named("repeated_member", ref))
            ok = False
        if not _bounded_text(variant, _VARIANT_CHARS):
            defects.append(f"invalid_variant:{local}")
            ok = False
        if indices is not None:
            claimed.update(indices)
            expanded.extend((index, variant) for index in indices)
    if ok and len({variant for _, variant in expanded}) > _MAX_VARIANTS:
        defects.append(f"variant_count:{local}")
        ok = False
    return tuple(expanded) if ok else None


def _names_only(text: str, components: frozenset[str]) -> bool:
    """Whether ``text`` is reported component IDs and separators, nothing else.

    Longest IDs are tried first, and an ID must end at a separator or the
    end, so a prefix of a reported ID never passes for it.
    """

    ordered = sorted(components, key=lambda component: (-len(component), component))
    position = 0
    while True:
        while position < len(text) and text[position] in _PROVENANCE_SEPARATORS:
            position += 1
        if position == len(text):
            return True
        for component in ordered:
            end = position + len(component)
            if text.startswith(component, position) and (
                end == len(text) or text[end] in _PROVENANCE_SEPARATORS
            ):
                position = end
                break
        else:
            return False


def _portion_provenance(
    raw: dict[str, Any], local: str, reported: frozenset[str], defects: list[str]
) -> tuple[bool, str | None]:
    """``(ok, provenance)``: the optional hint, naming only reported IDs (§5.3)."""

    value = raw.get("provenance")
    if value is None:
        return True, None
    if not _bounded_text(value, _PROVENANCE_CHARS, allow_empty=True):
        defects.append(f"invalid_field:{local}:provenance")
        return False, None
    if not _names_only(value, reported):
        defects.append(f"invented_provenance:{local}")
        return False, None
    return True, value


def _parse_portion_group(
    raw: object,
    position: int,
    *,
    refs: Mapping[str, tuple[int, ...]],
    observations: Sequence[str],
    case_of_index: Mapping[int, str],
    reported: Mapping[str, frozenset[str]],
    defects: list[str],
) -> tuple[ModelGroup, tuple[int, ...], str | None] | None:
    if not isinstance(raw, dict):
        defects.append(f"invalid_group:{position}")
        return None
    if not _check_keys(raw, defects, _PORTION_REQUIRED_KEYS):
        return None
    local = _token(raw["id"])
    if local is None or local != raw["id"]:
        defects.append(f"invalid_id:{position}")
        return None
    ok = _check_header(raw, local, defects)
    citations = _parse_citations(raw["citations"])
    metrics = _parse_metrics(raw.get("metrics"))
    for name, parsed in (("citations", citations), ("metrics", metrics)):
        if parsed is None:
            defects.append(f"invalid_field:{local}:{name}")
            ok = False
    expanded = _parse_portion_members(raw["members"], local, refs, defects)
    if expanded is None:
        return None
    members = tuple((observations[index], variant) for index, variant in expanded)
    components = frozenset(
        component for evidence_id, _ in members for component in reported.get(evidence_id, ())
    )
    provenance_ok, provenance = _portion_provenance(raw, local, components, defects)
    if not ok or not provenance_ok or citations is None or metrics is None:
        return None
    member_cases = tuple(case_of_index[index] for index, _ in expanded)
    group = ModelGroup(
        local_id=local,
        title=raw["title"],
        failure_mode=raw["failure_mode"],
        expected_behaviour=raw["expected_behaviour"],
        question=raw["question"],
        members=members,
        affected_cases=tuple(sorted(set(member_cases))),
        citations=citations,
        priority=raw["priority"],
        metrics=metrics,
        member_cases=member_cases,
    )
    return group, tuple(index for index, _ in expanded), provenance


def _portion_coverage_defects(
    indices: Iterable[tuple[int, ...]], portion: _Portion, table: _RefTable
) -> list[str]:
    """``overlap`` and ``uncovered`` defects of a portion, named by reference.

    An observation claimed twice is named by its o-ref. A unit none of whose
    observations is covered is named once, by its own reference; a set
    covered only in part names its uncovered o-refs.
    """

    claims: dict[int, int] = {}
    for group in indices:
        for index in group:
            claims[index] = claims.get(index, 0) + 1
    overlaps: list[str] = []
    uncovered: list[str] = []
    for unit in portion.units:
        overlaps += [f"overlap:{table.o_ref(i)}" for i in unit.observations if claims.get(i, 0) > 1]
        missing = [i for i in unit.observations if i not in claims]
        if len(missing) == len(unit.observations):
            uncovered.append(_named("uncovered", unit.ref))
        else:
            uncovered += [f"uncovered:{table.o_ref(i)}" for i in missing]
    return overlaps + uncovered


def parse_portion_answer(
    groups: object,
    *,
    portion: _Portion,
    table: _RefTable,
    reported: Mapping[str, frozenset[str]],
) -> ParsedResponse:
    """A `model_grouping/2` `finish_portion` answer, with references expanded.

    ``groups`` is the call's `groups` argument. Each group's members are
    ``{"ref", "variant"}`` objects naming o-refs and s-refs of ``portion``;
    they expand to the same ``(evidence_id, variant)`` members, in listing
    order, as the equivalent `/15` answer, and the cases follow from them
    (§6.3, §6.4). Group defects are those of `parse_map_response`, with
    `unknown_ref` and `repeated_member` in place of the evidence-ID member
    checks and no `affected_cases`; every defect that names an observation
    names it by reference, never by evidence ID.

    ``reported`` maps an evidence ID to the component IDs reported for it, as
    the case headers show them (absent or empty: none). A group's optional
    `provenance` may name only IDs reported for its members; anything else is
    `invented_provenance` (§5.3). Group texts are not screened for component
    IDs. Coverage is checked against the portion's failing observations;
    overlaps are reported, not settled: `resolve_overlaps` settles them.
    """

    if not isinstance(groups, list):
        return ParsedResponse((), ("no_groups",))
    refs = portion.references()
    case_of_index = {index: unit.case_id for unit in portion.units for index in unit.observations}
    defects: list[str] = []
    parsed = [
        _parse_portion_group(
            raw,
            position,
            refs=refs,
            observations=table.observations,
            case_of_index=case_of_index,
            reported=reported,
            defects=defects,
        )
        for position, raw in enumerate(groups)
    ]
    valid = [item for item in parsed if item is not None]
    counts: dict[str, int] = {}
    for group, _, _ in valid:
        counts[group.local_id] = counts.get(group.local_id, 0) + 1
    defects += [f"duplicate_id:{local}" for local in sorted(counts) if counts[local] > 1]
    kept = [item for item in valid if counts[item[0].local_id] == 1]
    defects += _portion_coverage_defects((indices for _, indices, _ in kept), portion, table)
    return ParsedResponse(
        tuple(group for group, _, _ in kept),
        _bounded_defects(defects),
        tuple((group.local_id, text) for group, _, text in kept if text is not None),
    )


def _check_keys(
    raw: dict[str, Any], defects: list[str], required: frozenset[str] = _REQUIRED_KEYS
) -> bool:
    before = len(defects)
    for key in sorted(set(raw) - required - _OPTIONAL_KEYS, key=str):
        defects.append(_named("forbidden_key", key))
    for key in sorted(required - set(raw)):
        defects.append(f"missing_key:{key}")
    return len(defects) == before


def _check_header(raw: dict[str, Any], local: str, defects: list[str]) -> bool:
    before = len(defects)
    for name, maximum in _HEADER_BOUNDS:
        if not _bounded_text(raw[name], maximum):
            defects.append(f"invalid_field:{local}:{name}")
    if not _bounded_text(raw["priority"], _PRIORITY_CHARS, allow_empty=True):
        defects.append(f"invalid_field:{local}:priority")
    return len(defects) == before


def _check_provenance(raw: dict[str, Any], local: str, ctx: _ChunkContext) -> str | None:
    value = raw.get("provenance")
    if value is None:
        return None
    if not ctx.provenance_available:
        # Never invent a path when provenance is unavailable.
        return f"invented_provenance:{local}"
    if not _bounded_text(value, _PROVENANCE_CHARS, allow_empty=True):
        return f"invalid_field:{local}:provenance"
    return None


def _parse_group(
    raw: object, position: int, ctx: _ChunkContext, defects: list[str]
) -> ModelGroup | None:
    if not isinstance(raw, dict):
        defects.append(f"invalid_group:{position}")
        return None
    if not _check_keys(raw, defects):
        return None
    local = _token(raw["id"])
    if local is None or local != raw["id"]:
        defects.append(f"invalid_id:{position}")
        return None
    ok = _check_header(raw, local, defects)
    citations = _parse_citations(raw["citations"])
    metrics = _parse_metrics(raw.get("metrics"))
    for name, parsed in (("citations", citations), ("metrics", metrics)):
        if parsed is None:
            defects.append(f"invalid_field:{local}:{name}")
            ok = False
    provenance_defect = _check_provenance(raw, local, ctx)
    if provenance_defect is not None:
        defects.append(provenance_defect)
        ok = False
    members = _parse_members(raw["members"], local, ctx, defects)
    if members is None or not ok or citations is None or metrics is None:
        return None
    member_cases = tuple(ctx.case_of[evidence_id] for evidence_id, _ in members)
    affected = raw["affected_cases"]
    if not isinstance(affected, list) or not all(isinstance(case, str) for case in affected):
        defects.append(f"invalid_field:{local}:affected_cases")
        return None
    if len(affected) != len(set(affected)) or set(affected) != set(member_cases):
        defects.append(f"affected_cases_mismatch:{local}")
        return None
    return ModelGroup(
        local_id=local,
        title=raw["title"],
        failure_mode=raw["failure_mode"],
        expected_behaviour=raw["expected_behaviour"],
        question=raw["question"],
        members=members,
        affected_cases=tuple(sorted(set(member_cases))),
        citations=citations,
        priority=raw["priority"],
        metrics=metrics,
        member_cases=member_cases,
    )


def _coverage_defects(groups: Sequence[ModelGroup], chunk_failing: Sequence[str]) -> list[str]:
    claims: dict[str, int] = {}
    for group in groups:
        for evidence_id, _ in group.members:
            claims[evidence_id] = claims.get(evidence_id, 0) + 1
    defects = [_named("overlap", eid) for eid in chunk_failing if claims.get(eid, 0) > 1]
    defects += [_named("uncovered", eid) for eid in chunk_failing if eid not in claims]
    return defects


def _bounded_defects(defects: Sequence[str]) -> tuple[str, ...]:
    if len(defects) <= _MAX_DEFECTS:
        return tuple(defects)
    return (*defects[: _MAX_DEFECTS - 1], f"more_defects:{len(defects) - _MAX_DEFECTS + 1}")


def parse_map_response(
    text: str,
    *,
    chunk_failing: Sequence[str],
    case_of: Mapping[str, str],
    provenance_available: bool,
) -> ParsedResponse:
    """Every structurally valid group of one map answer, plus its defects.

    A group is kept only when all its members are known failing IDs of this
    chunk; the caller lists only failing *available* IDs in
    ``chunk_failing``, since an unavailable observation joins no group. Overlaps between valid groups and uncovered chunk
    IDs are reported as defects (they drive protocol repair) but do not drop
    a group: `resolve_overlaps` settles overlaps after the repair budget.
    """

    value, error = _extract_object(text)
    if value is None:
        return ParsedResponse((), (error or "invalid_json",))
    if not isinstance(value, dict):
        return ParsedResponse((), ("not_an_object",))
    raw_groups = value.get("groups")
    if not isinstance(raw_groups, list):
        return ParsedResponse((), ("no_groups",))
    defects = [_named("unexpected_key", key) for key in sorted(set(value) - {"groups"})]
    ctx = _ChunkContext(frozenset(chunk_failing), case_of, provenance_available)
    parsed = [_parse_group(raw, position, ctx, defects) for position, raw in enumerate(raw_groups)]
    valid = [group for group in parsed if group is not None]
    counts: dict[str, int] = {}
    for group in valid:
        counts[group.local_id] = counts.get(group.local_id, 0) + 1
    # Every group sharing a duplicated local id is dropped, so which one
    # survives never depends on response order.
    defects += [f"duplicate_id:{local}" for local in sorted(counts) if counts[local] > 1]
    groups = [group for group in valid if counts[group.local_id] == 1]
    defects += _coverage_defects(groups, chunk_failing)
    return ParsedResponse(tuple(groups), _bounded_defects(defects))


def covered(groups: Iterable[ModelGroup]) -> frozenset[str]:
    return frozenset(evidence_id for group in groups for evidence_id, _ in group.members)


def _admission_key(group: ModelGroup) -> tuple[int, int, str, str]:
    """The contract order: distinct cases desc, observations desc, member digest.

    A final content digest breaks the tie between two groups with identical
    membership, so response order never decides which header survives.
    """

    member_ids = sorted(evidence_id for evidence_id, _ in group.members)
    content = [
        group.title,
        group.failure_mode,
        group.expected_behaviour,
        group.question,
        group.priority,
        sorted(group.members),
    ]
    return (
        -len(set(group.member_cases)),
        -len(group.members),
        _digest({"member_ids": member_ids}),
        _digest(content),
    )


def _admission_order(groups: Iterable[ModelGroup]) -> list[ModelGroup]:
    return sorted(groups, key=_admission_key)


def _keep_members(group: ModelGroup, keep: Sequence[int]) -> ModelGroup:
    members = tuple(group.members[index] for index in keep)
    member_cases = tuple(group.member_cases[index] for index in keep)
    return ModelGroup(
        local_id=group.local_id,
        title=group.title,
        failure_mode=group.failure_mode,
        expected_behaviour=group.expected_behaviour,
        question=group.question,
        members=members,
        affected_cases=tuple(sorted(set(member_cases))),
        citations=group.citations,
        priority=group.priority,
        metrics=group.metrics,
        member_cases=member_cases,
    )


def resolve_overlaps(groups: Iterable[ModelGroup]) -> tuple[ModelGroup, ...]:
    """Give each contested ID to the group ranking first in admission order.

    A lower-ranked group loses only the contested members (its
    ``affected_cases`` shrinks with them) and is dropped once empty.
    """

    claimed: set[str] = set()
    kept: list[ModelGroup] = []
    for group in _admission_order(groups):
        keep = [i for i, (eid, _) in enumerate(group.members) if eid not in claimed]
        if not keep:
            continue
        claimed.update(group.members[index][0] for index in keep)
        kept.append(group if len(keep) == len(group.members) else _keep_members(group, keep))
    return tuple(kept)


def _parse_merge_header(raw: dict[str, Any], position: int) -> ModelGroup | None | bool:
    """`None` for an absent header, a header-only group, or `False` if invalid."""

    present = _REDUCE_HEADER_KEYS & set(raw)
    if not present:
        return None
    if present != _REDUCE_HEADER_KEYS:
        return False
    for name, maximum in _HEADER_BOUNDS:
        if not _bounded_text(raw[name], maximum):
            return False
    if not _bounded_text(raw["priority"], _PRIORITY_CHARS, allow_empty=True):
        return False
    return ModelGroup(
        local_id=f"r{position}",
        title=raw["title"],
        failure_mode=raw["failure_mode"],
        expected_behaviour=raw["expected_behaviour"],
        question=raw["question"],
        members=(),
        affected_cases=(),
        citations=(),
        priority=raw["priority"],
        metrics=(),
    )


def _parse_merge(
    raw: object, position: int, known: frozenset[str], used: set[str]
) -> tuple[tuple[str, ...], ModelGroup | None] | None:
    if not isinstance(raw, dict) or set(raw) - _REDUCE_HEADER_KEYS - {"groups"}:
        return None
    ids = raw.get("groups")
    if not isinstance(ids, list) or len(ids) < 2:
        return None
    if not all(isinstance(item, str) and item in known for item in ids):
        return None
    if len(set(ids)) != len(ids) or used & set(ids):
        return None
    header = _parse_merge_header(raw, position)
    if header is False:
        return None
    used.update(ids)
    return tuple(ids), header if isinstance(header, ModelGroup) else None


def parse_reduce_response(
    text: str, *, map_ids: Sequence[str]
) -> tuple[tuple[tuple[str, ...], ModelGroup | None], ...] | None:
    """``(member_map_ids, merged_header)`` merges, or `None` if invalid.

    Each merge consumes two or more whole, distinct, known map groups, and a
    map group joins at most one merge. The header keys are all
    present or all absent; an absent header keeps the first-ranked map
    group's header in `apply_merges`.
    """

    value, _ = _extract_object(text)
    if not isinstance(value, dict) or set(value) != {"merges"}:
        return None
    raw_merges = value["merges"]
    if not isinstance(raw_merges, list):
        return None
    known = frozenset(map_ids)
    used: set[str] = set()
    merges: list[tuple[tuple[str, ...], ModelGroup | None]] = []
    for position, raw in enumerate(raw_merges):
        merge = _parse_merge(raw, position, known, used)
        if merge is None:
            return None
        merges.append(merge)
    return tuple(merges)


def _unique(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(dict.fromkeys(values))


def _merge_groups(parts: Sequence[ModelGroup], header: ModelGroup | None) -> ModelGroup | None:
    pairs = sorted(
        (member, case)
        for part in parts
        for member, case in zip(part.members, part.member_cases, strict=True)
    )
    if len({variant for (_, variant), _ in pairs}) > _MAX_VARIANTS:
        return None
    source = header if header is not None else _admission_order(parts)[0]
    member_cases = tuple(case for _, case in pairs)
    return ModelGroup(
        local_id="+".join(part.local_id for part in parts),
        title=source.title,
        failure_mode=source.failure_mode,
        expected_behaviour=source.expected_behaviour,
        question=source.question,
        members=tuple(member for member, _ in pairs),
        affected_cases=tuple(sorted(set(member_cases))),
        citations=_unique(c for part in parts for c in part.citations)[:_MAX_CITATIONS],
        priority=source.priority,
        metrics=tuple(sorted(_unique(m for part in parts for m in part.metrics)))[:_MAX_METRICS],
        member_cases=member_cases,
    )


@dataclass(frozen=True)
class AppliedMerges:
    groups: tuple[ModelGroup, ...]
    skipped: int


def apply_merges(
    map_groups: Sequence[ModelGroup],
    merges: Sequence[tuple[tuple[str, ...], ModelGroup | None]],
) -> AppliedMerges:
    """Replace each merged set of whole map groups by their member union.

    Unmentioned map groups stay as they are. A merge whose union would carry
    more than the admission limit of distinct variants is not applied: its
    map groups stay separate rather than failing admission later, and it is
    counted in ``skipped`` so the runtime can record it.
    """

    by_id = {group.local_id: group for group in map_groups}
    if len(by_id) != len(map_groups):
        raise ValueError("duplicate_map_id")
    replaced: dict[str, ModelGroup | None] = {}
    skipped = 0
    for ids, header in merges:
        if any(local not in by_id or local in replaced for local in ids):
            raise ValueError("invalid_merge")
        merged = _merge_groups([by_id[local] for local in ids], header)
        if merged is None:
            skipped += 1
            continue
        replaced[ids[0]] = merged
        replaced.update({local: None for local in ids[1:]})
    result: list[ModelGroup] = []
    for group in map_groups:
        substitute = replaced.get(group.local_id, group)
        if substitute is not None:
            result.append(substitute)
    return AppliedMerges(tuple(result), skipped)


#: Bounds a description is re-rendered with when the canonical descriptions
#: map would exceed `MAX_SPARSE_MAP_BYTES` (the contract: a large but valid grouping
#: keeps its groups; only description text is trimmed or removed).
_TRIMMED_TITLE_CHARS: Final = 64
_TRIMMED_FIELD_CHARS: Final = 96
#: Variant labels are shortened to this many characters on the same condition.
_TRIMMED_VARIANT_CHARS: Final = 12


def _clip(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else text[: maximum - 1] + "\u2026"


def _description(
    group: ModelGroup, *, trimmed: bool = False, whole: bool = False, family15: bool = False
) -> dict[str, str] | None:
    """Label and observed-error summary; none for a blanked header.

    The runtime blanks all four header texts to drop a description carrying
    an oracle needle ("invalid descriptions are removed"). ``whole`` (family
    15, ADR-0057) renders every field and the whole title: nothing is cut.
    ``trimmed`` renders the shorter form `_fit_descriptions` falls back to;
    with ``family15`` that form marks each shortened field (`…[cut]`) and
    keeps the question whole.
    """

    if not (group.title or group.failure_mode or group.expected_behaviour or group.question):
        return None
    title, observed, expected, question = (
        group.title,
        group.failure_mode,
        group.expected_behaviour,
        group.question,
    )
    if trimmed:
        title = _clip(title, _TRIMMED_TITLE_CHARS)
        if family15:
            observed = _cut(observed, _TRIMMED_FIELD_CHARS)
            expected = _cut(expected, _TRIMMED_FIELD_CHARS)
        else:
            observed, expected, question = (
                _clip(value, _TRIMMED_FIELD_CHARS) for value in (observed, expected, question)
            )
    text = "\n".join(
        f"{prefix}{value}"
        for prefix, value in zip(
            DESCRIPTION_FIELD_PREFIXES, (observed, expected, question), strict=True
        )
    )
    if whole or trimmed:
        # A whole (family 15) projection cuts nothing (ADR-0057): admission
        # bounds it by `FAMILY15_DESCRIPTION_CHARS`. A trimmed entry is
        # bounded by its own form -- 384 characters in family 14; the
        # family-15 form keeps the question whole and stays within the
        # family-15 bound -- so the description cut never applies to it.
        return {"label": title, "description": text}
    return {"label": title[:_TITLE_CHARS], "description": text[:_DESCRIPTION_CHARS]}


def _check_features(features: Sequence[FieldFeature]) -> None:
    # The same closed schema `builtin_grouping` enforces.
    if isinstance(features, (str, bytes, bytearray)) or not isinstance(features, Sequence):
        raise ValueError("invalid_features")
    if not 1 <= len(features) <= 16 or not all(isinstance(item, FieldFeature) for item in features):
        raise ValueError("invalid_features")
    if len({feature.name for feature in features}) != len(features):
        raise ValueError("duplicate_feature_name")


def _field_assignments(
    registry: GroupingRegistry,
    indices: Sequence[int],
    features: Sequence[FieldFeature],
    first_number: int,
) -> dict[int, int]:
    """Field keys exactly as `builtin_grouping` builds them, over a subset."""

    _check_features(features)
    numbers: dict[tuple[tuple[Any, ...], ...], int] = {}
    assigned: dict[int, int] = {}
    for index in indices:
        row = registry.records[index]
        key = tuple(
            typed_json_key(pointer_value(row.sources[feature.source], feature.path))
            for feature in features
        )
        assigned[index] = numbers.setdefault(key, first_number + len(numbers))
    return assigned


@dataclass
class _Projection:
    assignments: list[int | None]
    descriptions: dict[int | str, Mapping[str, Any]]
    variants: dict[int | str, str]
    #: The model groups in admission order (their projection numbers) and
    #: each one's registry indices, for `_fit_sparse_maps`.
    groups: list[ModelGroup] = field(default_factory=list)
    members: list[list[int]] = field(default_factory=list)


def _eligible(record: GroupingRecord) -> bool:
    """The contract: only a failing *available* observation joins an error group."""

    return record.failing and record.available


def _project_model_groups(
    registry: GroupingRegistry, groups: Sequence[ModelGroup], *, family15: bool = False
) -> _Projection:
    index_of = {record.evidence_id: index for index, record in enumerate(registry.records)}
    projection = _Projection([None] * len(registry.records), {}, {})
    for number, group in enumerate(_admission_order(groups)):
        projection.groups.append(group)
        projection.members.append([])
        for evidence_id, variant in group.members:
            index = index_of.get(evidence_id)
            if index is None or not _eligible(registry.records[index]):
                raise ValueError("unknown_member")
            if projection.assignments[index] is not None:
                raise ValueError("overlap")
            projection.assignments[index] = number
            projection.variants[index] = variant
            projection.members[number].append(index)
        description = _description(group, whole=family15)
        if description is not None:
            projection.descriptions[number] = description
    return projection


class _SparseMap:
    """The canonical JSON byte size of a sparse map, kept per entry.

    Canonical JSON is compact, so an object costs its two braces, one comma
    between members, and each ``"key":value`` member on its own.
    """

    def __init__(self, entries: Mapping[int | str, Any], key: Callable[[int], str]) -> None:
        self._key = key
        self._sizes = {number: self._member(number, value) for number, value in entries.items()}
        self._total = sum(self._sizes.values())

    def _member(self, number: int | str, value: Any) -> int:
        return len(_canonical_json({self._key(int(number)): value})) - 2

    def fits(self) -> bool:
        return 2 + self._total + max(len(self._sizes) - 1, 0) <= MAX_SPARSE_MAP_BYTES

    def put(self, number: int, value: Any) -> None:
        self._total += self._member(number, value) - self._sizes.get(number, 0)
        self._sizes[number] = self._member(number, value)

    def drop(self, number: int) -> None:
        self._total -= self._sizes.pop(number, 0)


def _short_labels(labels: Iterable[str]) -> dict[str, str]:
    """Deterministically shorter labels that keep distinct labels distinct."""

    return short_labels(labels, width=_TRIMMED_VARIANT_CHARS)


def _fit_descriptions(
    projection: _Projection, key_width: int, *, family15: bool = False
) -> tuple[int, int]:
    """Truncate, then remove, descriptions from the lowest-ranked group up.

    Keys are padded to the widest group number admission can assign, so the
    estimate bounds the canonical map whatever numbers the groups receive.
    ``family15`` re-renders with the family-15 trimmed form (ADR-0057): the
    shortened fields carry the `…[cut]` mark and the question stays whole.
    """

    sizes = _SparseMap(projection.descriptions, lambda n: f"{n:0{key_width}d}")
    truncated = dropped = 0
    ranked = [n for n in reversed(range(len(projection.groups))) if n in projection.descriptions]
    for number in ranked:
        if sizes.fits():
            return truncated, dropped
        shorter = _description(projection.groups[number], trimmed=True, family15=family15)
        if shorter is not None and shorter != projection.descriptions[number]:
            projection.descriptions[number] = shorter
            sizes.put(number, shorter)
            truncated += 1
    for number in ranked:
        if sizes.fits():
            break
        del projection.descriptions[number]
        sizes.drop(number)
        dropped += 1
    return truncated, dropped


def _fit_variants(projection: _Projection) -> tuple[int, int]:
    """Shorten, then remove, variant labels from the lowest-ranked group up."""

    sizes = _SparseMap(projection.variants, str)
    shortened = dropped = 0
    ranked = list(reversed(range(len(projection.groups))))
    for number in ranked:
        if sizes.fits():
            return shortened, dropped
        indices = projection.members[number]
        short = _short_labels(projection.variants[index] for index in indices)
        if any(
            short[projection.variants[index]] != projection.variants[index] for index in indices
        ):
            for index in indices:
                projection.variants[index] = short[projection.variants[index]]
                sizes.put(index, projection.variants[index])
            shortened += 1
    for number in ranked:
        if sizes.fits():
            break
        for index in projection.members[number]:
            del projection.variants[index]
            sizes.drop(index)
        dropped += 1
    return shortened, dropped


def _fit_sparse_maps(
    projection: _Projection, record_count: int, *, family15: bool = False
) -> dict[str, int] | None:
    """Fit the model groups' descriptions and variants under the sparse caps.

    A large but valid grouping -- many groups with long or non-ASCII text --
    would otherwise be refused whole (``description_size``/``variant_size``)
    and fall back entirely, while the contract preserves individually valid groups and
    only removes descriptions. Group membership never changes here; the
    lowest-ranked groups in admission order lose their text first. Returns
    the trimming counts, or `None` if nothing was trimmed.
    """

    key_width = len(str(max(record_count - 1, 0)))
    truncated, dropped = _fit_descriptions(projection, key_width, family15=family15)
    shortened, variants_dropped = _fit_variants(projection)
    if not (truncated or dropped or shortened or variants_dropped):
        return None
    return {
        "descriptions_truncated": truncated,
        "descriptions_dropped": dropped,
        "variants_shortened": shortened,
        "variants_dropped": variants_dropped,
    }


def project(
    registry: GroupingRegistry,
    groups: Sequence[ModelGroup],
    *,
    policy_identity: str,
    fallback_features: Sequence[FieldFeature] | None,
    fallback: tuple[Literal["uncovered", "all"], str] | None,
    forbidden_string_predicates: Sequence[tuple[str, Callable[[str], bool]]],
    on_trim: Callable[[Mapping[str, int]], None] | None = None,
    describe_fields: bool = False,
    fields_screens: Sequence[Callable[[str], bool]] = (),
    family15: bool = False,
    on_full_descriptions: Callable[[Mapping[int, Mapping[str, str]]], None] | None = None,
) -> tuple[AdmittedGrouping, FallbackOutcome | None]:
    """Project model groups plus any fallback through one admission.

    Model groups take numbers ``0..k-1`` in admission order. With
    ``("uncovered", reason)`` only the failing records no model group covers
    fall back; with ``("all", reason)`` the model groups are ignored. A
    ``fields`` fallback numbers its keys above ``k``, so it never joins a
    model group; a singleton fallback leaves those assignments `None`, which
    admission turns into exact singletons. A failing but unavailable
    observation is never coverage-eligible under any scope: it stays `None`
    and admission (``unavailable_unassigned``) keeps it out of every group
    A ``("uncovered", reason)`` request with nothing uncovered
    returns no outcome. `groups` must not overlap
    (`resolve_overlaps` first); without a fallback, every failing record
    must be covered.

    ``family15`` (campaign family 15, manifest `/16`; ADR-0057) projects each
    model group's description whole, trims with the family-15 trimmed form,
    bounds `fields` descriptions and the admission by
    `FAMILY15_DESCRIPTION_CHARS` instead of `DESCRIPTION_CHARS`.

    Descriptions and variant labels that would exceed the sparse-map caps
    are trimmed first (`_fit_sparse_maps`); ``on_trim`` receives the counts
    when anything was trimmed, for the private log only. A family-15 fit
    that trimmed or dropped a map entry hands the whole description of that
    group to ``on_full_descriptions``, keyed by the group number the
    admitted result uses -- the campaign journals it as a full-description
    record (ADR-0057).
    """

    description_chars = FAMILY15_DESCRIPTION_CHARS if family15 else DESCRIPTION_CHARS
    scope = fallback[0] if fallback is not None else None
    projection = _project_model_groups(
        registry, () if scope == "all" else groups, family15=family15
    )
    whole: dict[int, dict[str, str]] = (
        {
            number: dict(entry)
            for number, entry in projection.descriptions.items()
            if isinstance(number, int)
        }
        if family15
        else {}
    )
    trimmed = _fit_sparse_maps(projection, len(registry.records), family15=family15)
    overflow = sorted(n for n in whole if projection.descriptions.get(n) != whole[n])
    if trimmed is not None and on_trim is not None:
        on_trim({**trimmed, **({"full_descriptions": len(overflow)} if overflow else {})})
    uncovered = [
        index
        for index, record in enumerate(registry.records)
        if _eligible(record) and projection.assignments[index] is None
    ]
    outcome: FallbackOutcome | None = None
    if fallback is None:
        if uncovered:
            raise ValueError("uncovered")
    elif not uncovered and scope == "uncovered":
        pass  # nothing fell back: no spurious caveat
    elif fallback_features is None:
        outcome = FallbackOutcome("singletons", fallback[0], fallback[1])
    else:
        first = max((n for n in projection.assignments if n is not None), default=-1) + 1
        for index, number in _field_assignments(
            registry, uncovered, fallback_features, first
        ).items():
            projection.assignments[index] = number
        if describe_fields:
            projection.descriptions.update(
                fields_group_texts(
                    registry,
                    projection.assignments,
                    fallback_features,
                    first_number=first,
                    reserved=projection.descriptions,
                    screens=fields_screens,
                    description_chars=description_chars,
                ).items()
            )
        outcome = FallbackOutcome("fields", fallback[0], fallback[1])
    result = GroupingResult(
        registry.digest,
        tuple(projection.assignments),
        projection.descriptions,
        projection.variants,
    )
    admitted = admit_grouping_result(
        registry,
        result,
        policy_identity=policy_identity,
        forbidden_string_predicates=forbidden_string_predicates,
        unavailable_unassigned=True,
        description_chars=description_chars,
    )
    if overflow and on_full_descriptions is not None:
        # The fit keyed its trim and drop by projection number; the record
        # binds the number the admitted result uses. Membership identifies
        # the group across the renumbering.
        numbers = {
            frozenset(neighborhood.member_ids): neighborhood.group_number
            for neighborhood in admitted.neighborhoods
        }
        full: dict[int, Mapping[str, str]] = {}
        for number in overflow:
            members = frozenset(
                registry.records[index].evidence_id for index in projection.members[number]
            )
            group_number = numbers.get(members)
            if group_number is not None:
                full[group_number] = whole[number]
        if full:
            on_full_descriptions(full)
    return admitted, outcome
