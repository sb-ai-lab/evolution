"""Pure portion planner of the family-15 chat Grouper (spec §2, §5; ADR-0055).

No I/O, no provider, no clock, no asyncio. Every function is a pure function
of the grouping registry and the limits passed in, so the reference table,
the glimpses, the sets and the portion plan are the same whatever the
concurrency and whichever process builds them.

The pieces, in the order a caller uses them:

* `reference_table` names every failing, available observation `o<n>` and
  every case with one `c<n>` (§2). `RefTable.with_sets` adds the `s<n>` sets
  that `build_units` formed; `RefTable.to_log` is the `ref_registry` record.
* `glimpse` is a bounded excerpt of one observation (§5.2).
* `provenance_counts` and `case_header` build a case's bounded header
  (§5.2, §5.3).
* `build_units` forms sets of observations with one signature and one glimpse
  and leaves the rest as single units (§2).
* `plan_portions` fills portions unit by unit within capacity, the input
  budget and the checkpoint record budget, cutting a set that does not fit
  (§5.1). `split_portion` halves a portion after a cut answer (§6.5).
* `render_portion` is a portion's model input. It holds references, never
  evidence IDs, signatures or case IDs (§5.2).

A set cut by the plan or halved by `split_portion` keeps the name of the set
it came from with a piece number: `s4` is cut into `s4.1`, `s4.2`, ... in
o-ref order, and halving `s4.2` gives `s4.2.1` and `s4.2.2`. A piece of one
observation is that observation's o-ref. The `ref_registry` record lists the
sets `build_units` formed; the members of every piece are in the portion's
checkpoint as full evidence IDs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import groupby
from typing import Any, Final

from evolution.tree.roles.grouping.model_grouping import (
    REPRESENTATIVE_CHARS_START,
    CaseProfile,
    InputTooLarge,
    _representative_payload,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    GroupingRecord,
    GroupingRegistry,
    _canonical_json,
    _digest,
    observation_signature,
    typed_json_key,
)

__all__ = [
    "CHECKPOINT_ENVELOPE_BYTES",
    "MapPortion",
    "PortionPlan",
    "RefTable",
    "Unit",
    "bounded_histograms",
    "build_units",
    "case_header",
    "glimpse",
    "member_record_bytes",
    "plan_portions",
    "portion_input_size",
    "provenance_counts",
    "redacted_counts",
    "reference_table",
    "render_portion",
    "representative_payload",
    "split_portion",
]

#: Bytes of a `grouping_portion_finished` record outside its members: the
#: journal envelope and the fixed fields (grouping id, portion id, plan and
#: input digests, outcome, request count), about 1 KiB, plus room for the
#: group texts of a usual answer. The caller passes
#: ``family15_schema.MAX_RECORD_BYTES - CHECKPOINT_ENVELOPE_BYTES`` as
#: `plan_portions`'s ``record_budget`` (Builder decision 4). Group texts are
#: bounded per group by the answer rules, not by the plan; an answer whose
#: texts would still push the record over the ceiling is the caller's to
#: split.
CHECKPOINT_ENVELOPE_BYTES: Final = 8 * 1024

#: A variant label is at most 64 characters (`model_grouping._VARIANT_CHARS`);
#: canonical JSON writes a control character as six bytes (``\\u00XX``) and
#: no character as more, plus the two quotes.
_VARIANT_JSON_BYTES: Final = 64 * 6 + 2
#: JSON punctuation of one member object (``{"evidence_id":…,"variant":…}``),
#: one entry of its group's case list and their separators, with slack for
#: the group's own brackets.
_MEMBER_FRAME_BYTES: Final = 64

_PLAN_FORMAT: Final = "grouping_portion_plan/1"
_CUT_GAP: Final = " … "
_ELLIPSIS: Final = "…"
_GLIMPSE_LABELS: Final = ("failed_criteria: ", "\nfeedback: ", "\nanswer: ")
#: Shares of a cut glimpse: failed criteria, feedback, answer. A part that
#: needs less than its share leaves the rest to the others.
_GLIMPSE_WEIGHTS: Final = (1, 1, 2)


def _o_ref(index: int) -> str:
    return f"o{index + 1}"


def _json_text(value: object) -> str:
    return _canonical_json(value).decode("utf-8")


def portion_input_size(text: str) -> int:
    """Characters `text` adds to a request as `chat_loop.chat_request_size` counts it.

    The request size counts the JSON of every message, so a portion input is
    measured as a JSON string: quotes, backslashes and control characters
    count as their escapes.
    """

    return len(json.dumps(text, ensure_ascii=False))


# --- References --------------------------------------------------------------


@dataclass(frozen=True)
class RefTable:
    """o-refs, c-refs and s-refs of one grouping operation (§2).

    ``observations[i]`` is the evidence ID of o-ref ``o<i+1>``, numbered over
    cases in ``case_id`` order and then rows in ``(repeat, evidence_id)``
    order. ``cases[j]`` is the case of ``c<j+1>``. ``sets[k]`` holds the
    observation indices of ``s<k+1>``.
    """

    observations: tuple[str, ...]
    cases: tuple[str, ...]
    sets: tuple[tuple[int, ...], ...]
    _case_index: Mapping[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_case_index", {case: j for j, case in enumerate(self.cases)})

    def o_ref(self, i: int) -> str:
        return _o_ref(i)

    def c_ref(self, j: int) -> str:
        return f"c{j + 1}"

    def s_ref(self, k: int) -> str:
        return f"s{k + 1}"

    def case_ref(self, case_id: str) -> str:
        return self.c_ref(self._case_index[case_id])

    def with_sets(self, units: Sequence[Unit]) -> RefTable:
        """This table with the sets among ``units``, which `build_units` numbered."""

        sets = tuple(unit.observations for unit in units if len(unit.observations) > 1)
        for k, unit in enumerate(u for u in units if len(u.observations) > 1):
            if unit.ref != self.s_ref(k):
                raise ValueError("set_numbering")
        return RefTable(self.observations, self.cases, sets)

    def to_log(self) -> dict[str, Any]:
        """The private log's ``ref_registry`` record body."""

        return {
            "observations": {_o_ref(i): eid for i, eid in enumerate(self.observations)},
            "cases": {self.c_ref(j): case for j, case in enumerate(self.cases)},
            "sets": {
                self.s_ref(k): [_o_ref(i) for i in members] for k, members in enumerate(self.sets)
            },
        }


def _grouped(registry: GroupingRegistry) -> list[tuple[str, list[GroupingRecord]]]:
    """Failing, available records per case, cases in order, rows in o-ref order."""

    cases: list[tuple[str, list[GroupingRecord]]] = []
    for case_id, rows in groupby(registry.records, key=lambda record: record.case_id):
        failing = sorted(
            (record for record in rows if record.failing and record.available),
            key=lambda record: (record.repeat, record.evidence_id),
        )
        if failing:
            cases.append((case_id, failing))
    return cases


def reference_table(registry: GroupingRegistry) -> RefTable:
    """The table of o-refs and c-refs; it has no sets until `RefTable.with_sets`.

    Only cases with a failing, available observation get a c-ref; they are
    the cases `model_grouping.build_case_profiles` profiles.
    """

    cases = _grouped(registry)
    return RefTable(
        observations=tuple(record.evidence_id for _, rows in cases for record in rows),
        cases=tuple(case_id for case_id, _ in cases),
        sets=(),
    )


# --- Glimpses and representatives --------------------------------------------


def _answer_text(value: object) -> str:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    if isinstance(value, str):
        return value
    return _json_text(value)


def _clip(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    if chars <= len(_ELLIPSIS):
        return text[: max(chars, 0)]
    return text[: chars - len(_ELLIPSIS)] + _ELLIPSIS


def _head_tail(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    if chars <= len(_CUT_GAP) + 1:
        return _clip(text, chars)
    room = chars - len(_CUT_GAP)
    head = (room + 1) // 2
    tail = room - head
    return text[:head] + _CUT_GAP + text[len(text) - tail :]


def _shares(lengths: Sequence[int], weights: Sequence[int], budget: int) -> list[int]:
    """Weighted shares of ``budget``; a part that needs less frees the rest."""

    shares = [0] * len(lengths)
    active = list(range(len(lengths)))
    remaining = max(budget, 0)
    while active:
        total = sum(weights[i] for i in active)
        satisfied = [i for i in active if lengths[i] * total <= remaining * weights[i]]
        if not satisfied:
            for i in active:
                shares[i] = remaining * weights[i] // total
            # Integer division leaves at most a few characters; the last part takes them.
            shares[active[-1]] += remaining - sum(shares[i] for i in active)
            return shares
        for i in satisfied:
            shares[i] = lengths[i]
            remaining -= lengths[i]
        active = [i for i in active if i not in satisfied]
    return shares


def glimpse(record: GroupingRecord, *, chars: int) -> str:
    """A bounded excerpt of one observation (§5.2), built by code.

    Its parts, in order: the failed criteria, the scorer feedback for this
    observation, and the answer text. When all three do not fit ``chars``,
    each gets a weighted share (1:1:2) of what the labels and the cut marker
    leave; a part that needs less leaves the rest to the others. The answer
    keeps its beginning and its end. A cut glimpse ends with
    ``…[cut; full length N]``, where N is the length of the uncut glimpse, so
    the model knows there is more to read. A glimpse is at most ``chars``
    characters, or the labels and the marker when ``chars`` is smaller.
    """

    observation = record.sources["observation"]
    criteria = _json_text(observation.get("failed_criteria"))
    feedback = _json_text(record.sources.get("feedback"))
    answer = _answer_text(observation.get("answer_text"))
    labels = _GLIMPSE_LABELS
    full = f"{labels[0]}{criteria}{labels[1]}{feedback}{labels[2]}{answer}"
    if len(full) <= chars:
        return full
    marker = f"{_ELLIPSIS}[cut; full length {len(full)}]"
    budget = chars - len(marker) - sum(len(label) for label in labels)
    shares = _shares([len(criteria), len(feedback), len(answer)], _GLIMPSE_WEIGHTS, budget)
    return (
        f"{labels[0]}{_clip(criteria, shares[0])}"
        f"{labels[1]}{_clip(feedback, shares[1])}"
        f"{labels[2]}{_head_tail(answer, shares[2])}{marker}"
    )


def representative_payload(
    record: GroupingRecord, *, chars: int = REPRESENTATIVE_CHARS_START
) -> dict[str, Any]:
    """The `/15` representative text of one observation, without its evidence ID."""

    payload = _representative_payload(record, chars)
    del payload["evidence_id"]
    return payload


# --- Case headers -------------------------------------------------------------


def provenance_counts(
    profile: CaseProfile, used: Mapping[str, tuple[str, ...] | None]
) -> tuple[dict[str, int] | None, int]:
    """Per reported component, the failing observations of the case that used it.

    ``used`` maps an evidence ID to its stored `SampleProvenance.used` union,
    `None` when the target reported none. The second value counts the case's
    failing observations whose provenance is unknown (null or absent). The
    first is `None` when no failing observation reported provenance, so the
    header shows no provenance line (§5.3).
    """

    counts: dict[str, int] = {}
    unknown = 0
    reported = False
    for row in profile.rows:
        if row.state != "failing":
            continue
        components = used.get(row.evidence_id)
        if components is None:
            unknown += 1
            continue
        reported = True
        for component in sorted(set(components)):
            counts[component] = counts.get(component, 0) + 1
    return (counts if reported else None), unknown


def _ranked(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _listed(ranked: Sequence[tuple[str, int]], shown: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {"name": name, "count": count} for name, count in ranked[:shown]
    ]
    rest = ranked[shown:]
    if rest:
        entries.append({"more": len(rest), "count": sum(count for _, count in rest)})
    return entries


def _metric_summary(name: str, values: Sequence[tuple[float, int]]) -> dict[str, Any]:
    total = sum(count for _, count in values)
    if not total:
        return {"name": name, "count": 0, "min": None, "median": None, "max": None}

    def nth(position: int) -> float:
        seen = 0
        for value, count in values:
            seen += count
            if position < seen:
                return value
        raise AssertionError("unreachable")

    middle = total // 2
    median = nth(middle) if total % 2 else (nth(middle - 1) + nth(middle)) / 2
    return {
        "name": name,
        "count": total,
        "min": values[0][0],
        "median": median,
        "max": values[-1][0],
    }


def case_header(
    profile: CaseProfile,
    *,
    provenance: Mapping[str, int] | None,
    unknown: int,
    chars: int,
    redact: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """A case's bounded header: the `/15` profile counts, metric summary and provenance.

    ``terminal_counts``, ``failed_criterion_counts`` and the provenance
    components are listed by count descending, then name. They are filled in
    turn, one entry of each list at a time, while the header's canonical JSON
    stays within ``chars``; each list's rest is one ``{"more": n, "count": c}``
    entry. The header is therefore within ``chars``, or within its fixed
    minimum of counts and remainders when ``chars`` is smaller, whatever R and
    the scorer emit. Metric values become ``{name, count, min, median, max}``.
    ``provenance`` comes from `provenance_counts`; `None` means no failing
    observation reported provenance and the header has no ``provenance`` key.
    ``redact`` (the campaign's run-secret redaction) is applied to every
    component ID before it is counted, so equal redacted IDs add up.
    """

    counts: list[Mapping[str, int]] = [profile.terminal_counts, profile.failed_criterion_counts]
    if provenance is not None:
        counts.append(redacted_counts(provenance, redact))

    def build(listed: Sequence[list[dict[str, Any]]]) -> dict[str, Any]:
        header: dict[str, Any] = {
            "total": profile.total,
            "available": profile.available,
            "passing": profile.passing,
            "failing": profile.failing,
            "unavailable": profile.unavailable,
            "terminal_counts": listed[0],
            "failed_criterion_counts": listed[1],
            "metric": _metric_summary(profile.metric_name, profile.metric_values),
        }
        if provenance is not None:
            header["provenance"] = {"components": listed[2], "unknown": unknown}
        return header

    return bounded_histograms(counts, build, chars=chars)


def redacted_counts(
    counts: Mapping[str, int], redact: Callable[[str], str] | None
) -> dict[str, int]:
    """``counts`` with ``redact`` applied to every name; equal redacted names add up."""

    if redact is None:
        return dict(counts)
    redacted: dict[str, int] = {}
    for name, count in counts.items():
        key = redact(name)
        redacted[key] = redacted.get(key, 0) + count
    return redacted


def bounded_histograms(
    counts: Sequence[Mapping[str, int]],
    build: Callable[[Sequence[list[dict[str, Any]]]], dict[str, Any]],
    *,
    chars: int,
) -> dict[str, Any]:
    """``build`` over bounded listings of ``counts``, within ``chars`` (§5.2, §7.1).

    Each mapping is listed by count descending, then name, as
    ``{"name", "count"}`` entries. The lists are filled in turn, one entry of
    each at a time, while the canonical JSON of ``build(listed)`` stays within
    ``chars``; each list's rest is one ``{"more": n, "count": c}`` entry. The
    case header (`case_header`) and the consolidation card share this rule.
    """

    lists = [_ranked(mapping) for mapping in counts]

    def listing(shown: Sequence[int]) -> list[list[dict[str, Any]]]:
        return [_listed(ranked, n) for ranked, n in zip(lists, shown, strict=True)]

    shown = [0] * len(lists)
    open_lists = [i for i, ranked in enumerate(lists) if ranked]
    result = build(listing(shown))
    while open_lists:
        for i in list(open_lists):
            trial = list(shown)
            trial[i] += 1
            candidate = build(listing(trial))
            if len(_json_text(candidate)) > chars:
                open_lists.remove(i)
                continue
            shown, result = trial, candidate
            if shown[i] == len(lists[i]):
                open_lists.remove(i)
    return result


# --- Units and sets -------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """One set, or one failing observation in no set (§2).

    ``ref`` is its o-ref, or its s-ref for a set. ``observations`` are o-ref
    indices in o-ref order and ``evidence_ids`` their full evidence IDs. A
    ``representative`` is shown with its representative text instead of its
    glimpse; it is always a single observation.
    """

    ref: str
    case_id: str
    observations: tuple[int, ...]
    evidence_ids: tuple[str, ...]
    representative: bool = False

    def __post_init__(self) -> None:
        if not self.observations or len(self.observations) != len(self.evidence_ids):
            raise ValueError("invalid_unit")
        if self.representative and len(self.observations) != 1:
            raise ValueError("invalid_unit")


def build_units(
    registry: GroupingRegistry,
    table: RefTable,
    glimpses: Mapping[str, str],
    *,
    max_set_members: int,
    representatives: Collection[str] = frozenset(),
) -> tuple[Unit, ...]:
    """Sets and single observations, cases in order, units by their first o-ref.

    A set holds two to ``max_set_members`` failing, available observations of
    one case with the same signature (`observation_signature`: terminal,
    critical and failed criteria) and byte-identical glimpses. A longer run of
    such observations forms several sets in o-ref order; a last piece of one
    observation stays single. Sets are numbered ``s1, s2, ...`` in unit
    order. A representative stays single, since it shows more than its glimpse.
    """

    if isinstance(max_set_members, bool) or not isinstance(max_set_members, int):
        raise ValueError("invalid_max_set_members")
    if max_set_members < 2:
        raise ValueError("invalid_max_set_members")
    index_of = {eid: i for i, eid in enumerate(table.observations)}
    chosen = frozenset(representatives)
    units: list[Unit] = []
    set_count = 0
    for case_id, rows in _grouped(registry):
        classes: dict[tuple[Any, ...], list[GroupingRecord]] = {}
        pieces: list[tuple[int, list[GroupingRecord], bool]] = []
        for record in rows:
            if record.evidence_id in chosen:
                pieces.append((index_of[record.evidence_id], [record], True))
                continue
            signature = observation_signature(record.sources["observation"])
            key = (*(typed_json_key(part) for part in signature), glimpses[record.evidence_id])
            classes.setdefault(key, []).append(record)
        for members in classes.values():
            for start in range(0, len(members), max_set_members):
                piece = members[start : start + max_set_members]
                pieces.append((index_of[piece[0].evidence_id], piece, False))
        pieces.sort(key=lambda item: item[0])
        for _, piece, representative in pieces:
            observations = tuple(index_of[record.evidence_id] for record in piece)
            evidence_ids = tuple(record.evidence_id for record in piece)
            if len(piece) > 1:
                set_count += 1
                ref = f"s{set_count}"
            else:
                ref = _o_ref(observations[0])
            units.append(Unit(ref, case_id, observations, evidence_ids, representative))
    return tuple(units)


def _piece(unit: Unit, start: int, stop: int, ref: str) -> Unit:
    observations = unit.observations[start:stop]
    if len(observations) == 1:
        ref = _o_ref(observations[0])
    return Unit(ref, unit.case_id, observations, unit.evidence_ids[start:stop])


# --- Portions -----------------------------------------------------------------------


@dataclass(frozen=True)
class MapPortion:
    """The units one map session receives, and their cases in order."""

    id: str
    units: tuple[Unit, ...]
    case_ids: tuple[str, ...]

    def references(self) -> dict[str, tuple[int, ...]]:
        """Every reference valid in this portion: unit refs and the o-refs of set members."""

        refs: dict[str, tuple[int, ...]] = {}
        for unit in self.units:
            refs[unit.ref] = unit.observations
            if len(unit.observations) > 1:
                for index in unit.observations:
                    refs[_o_ref(index)] = (index,)
        return refs


def _portion(portion_id: str, units: Sequence[Unit]) -> MapPortion:
    case_ids = tuple(dict.fromkeys(unit.case_id for unit in units))
    return MapPortion(portion_id, tuple(units), case_ids)


@dataclass(frozen=True)
class PortionPlan:
    """The portions in plan order and the digest that binds their checkpoints (§9)."""

    portions: tuple[MapPortion, ...]
    digest: str


def member_record_bytes(evidence_id: str, case_id: str) -> int:
    """The most bytes one member adds to its portion's checkpoint.

    The member object with its full evidence ID and the longest variant
    label, one case-list entry for it, and their punctuation.
    """

    return (
        len(_canonical_json(evidence_id))
        + len(_canonical_json(case_id))
        + _VARIANT_JSON_BYTES
        + _MEMBER_FRAME_BYTES
    )


def plan_portions(
    units: Sequence[Unit],
    *,
    capacity: int,
    input_budget: int,
    record_budget: int,
    render: Callable[[MapPortion], str],
    registry_digest: str,
    identity_digest: str,
) -> PortionPlan:
    """Fill portions unit by unit within capacity, input and checkpoint budgets (§5.1).

    The caller computes the limits:

    * ``capacity = max(1, max_output_tokens // group_output_tokens)`` units;
    * ``input_budget = max_input_chars - opening - reserve``, with the
      opening the system message, the portion lead and the tool definitions
      as `chat_loop.chat_request_size` counts them, and ``reserve =
      read_chars + RESULT_ENVELOPE_CHARS + REPAIR_RESERVE``, the room a
      session keeps for one tool result and a repair (§6.2);
    * ``record_budget = family15_schema.MAX_RECORD_BYTES -
      CHECKPOINT_ENVELOPE_BYTES``.

    A portion's input is ``portion_input_size(render(portion))``; its
    checkpoint is the sum of `member_record_bytes` over its members.

    Units keep their order. A portion is closed before a unit that would
    raise its units above ``capacity``, its input above ``input_budget`` or
    its members above ``record_budget``. A unit that does not fit an empty
    portion is reduced: a representative falls back to its glimpse, and a set
    is cut in o-ref order into the largest consecutive pieces that fit
    (``s4.1``, ``s4.2``, ...). A single observation that does not fit alone
    raises `InputTooLarge`.

    The plan is a pure function of the units, the limits and ``render``. Its
    digest covers the registry digest, the identity digest, the limits and
    every portion's unit list.
    """

    for name, value in (
        ("capacity", capacity),
        ("input_budget", input_budget),
        ("record_budget", record_budget),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid_{name}")

    portions: list[MapPortion] = []
    current: list[Unit] = []
    # Each queued unit carries the set name its pieces take and the next piece
    # number; a unit placed whole keeps its own ref.
    queue: list[tuple[Unit, str, int]] = [(unit, unit.ref, 1) for unit in reversed(units)]
    member_bytes: dict[str, int] = {}

    def portion_id() -> str:
        return f"p{len(portions) + 1}"

    def over_bounds(candidate: Sequence[Unit]) -> bool:
        """Cheap early refusals: record bytes, and the o-refs a set must list.

        Both sums stop as soon as they pass their limit, so a large set is
        refused in time proportional to what fits, not to its size.
        """

        record = 0
        chars = 0
        for unit in candidate:
            listed = len(unit.observations) > 1
            for index, eid in zip(unit.observations, unit.evidence_ids, strict=True):
                cost = member_bytes.get(eid)
                if cost is None:
                    cost = member_bytes[eid] = member_record_bytes(eid, unit.case_id)
                record += cost
                if record > record_budget:
                    return True
                if listed:
                    # `"o<n>",` inside a JSON string: two escaped quotes and a comma.
                    chars += len(_o_ref(index)) + 5
                    if chars > input_budget:
                        return True
        return False

    def fits(candidate: Sequence[Unit]) -> bool:
        if len(candidate) > capacity or over_bounds(candidate):
            return False
        return portion_input_size(render(_portion(portion_id(), candidate))) <= input_budget

    while queue:
        unit, base, number = queue.pop()
        if fits([*current, unit]):
            current.append(unit)
            continue
        if current:
            portions.append(_portion(portion_id(), current))
            current = []
            queue.append((unit, base, number))
        elif unit.representative:
            queue.append((replace(unit, representative=False), base, number))
        elif len(unit.observations) == 1:
            raise InputTooLarge(f"portion_unit_too_large:{unit.ref}")
        else:
            ref = f"{base}.{number}"
            size = _largest_piece(unit, ref, fits)
            rest = _piece(unit, size, len(unit.observations), f"{base}.{number + 1}")
            queue.append((rest, base, number + 1))
            queue.append((_piece(unit, 0, size, ref), base, number))
    if current:
        portions.append(_portion(portion_id(), current))
    plan = tuple(portions)
    digest = _digest(
        {
            "format": _PLAN_FORMAT,
            "registry_digest": registry_digest,
            "identity_digest": identity_digest,
            "limits": {
                "capacity": capacity,
                "input_budget": input_budget,
                "record_budget": record_budget,
            },
            "portions": [
                {
                    "id": portion.id,
                    "units": [
                        [unit.ref, list(unit.observations), unit.representative]
                        for unit in portion.units
                    ],
                }
                for portion in plan
            ],
        }
    )
    return PortionPlan(plan, digest)


def _largest_piece(unit: Unit, ref: str, fits: Callable[[Sequence[Unit]], bool]) -> int:
    """The size of the largest prefix of a set, named ``ref``, that fits alone.

    Size grows the render and the record bytes monotonically, so a doubling
    search and a bisection find it in logarithmically many renders. The whole
    set does not fit, so the prefix is shorter than the set.
    """

    def ok(size: int) -> bool:
        return fits([_piece(unit, 0, size, ref)])

    total = len(unit.observations)
    if not ok(1):
        raise InputTooLarge(f"portion_unit_too_large:{_o_ref(unit.observations[0])}")
    low, high = 1, min(2, total)
    while high < total and ok(high):
        low, high = high, min(high * 2, total)
    if high == low:
        return low
    while high - low > 1:
        middle = (low + high) // 2
        if ok(middle):
            low = middle
        else:
            high = middle
    return low


def split_portion(portion: MapPortion) -> tuple[MapPortion, MapPortion]:
    """Halve a portion by unit order, or halve its lone set in o-ref order (§6.5).

    The halves are ``<id>.1`` and ``<id>.2``. A halved set ``s4`` gives
    ``s4.1`` and ``s4.2``; a half of one observation is its o-ref. A portion
    of one observation cannot be split.
    """

    units = portion.units
    if len(units) > 1:
        middle = (len(units) + 1) // 2
        halves: tuple[Sequence[Unit], Sequence[Unit]] = (units[:middle], units[middle:])
    else:
        (unit,) = units
        size = len(unit.observations)
        if size < 2:
            raise ValueError("portion_unsplittable")
        middle = (size + 1) // 2
        halves = (
            (_piece(unit, 0, middle, f"{unit.ref}.1"),),
            (_piece(unit, middle, size, f"{unit.ref}.2"),),
        )
    return (
        _portion(f"{portion.id}.1", halves[0]),
        _portion(f"{portion.id}.2", halves[1]),
    )


def render_portion(
    portion: MapPortion,
    table: RefTable,
    headers: Mapping[str, Mapping[str, Any]],
    glimpses: Mapping[str, str],
    representatives: Mapping[str, Mapping[str, Any]],
    contrast: Mapping[str, str],
) -> str:
    """A portion's model input as canonical JSON (§5.2).

    Per case, in portion order: its c-ref, its header (`case_header`), the
    glimpse of its contrast (one passing observation, or null) and its units.
    A set shows its s-ref, its members' o-refs and its one shared glimpse; a
    single observation shows its o-ref and its glimpse, or its representative
    text (`representative_payload`) when it is a representative. The input
    holds no evidence IDs, signatures or case IDs: references replace them.

    ``headers`` and ``contrast`` are keyed by case ID; ``glimpses`` and
    ``representatives`` by evidence ID.
    """

    by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in portion.case_ids}
    for unit in portion.units:
        entry: dict[str, Any] = {"ref": unit.ref}
        if len(unit.observations) > 1:
            entry["members"] = [table.o_ref(index) for index in unit.observations]
        if unit.representative:
            payload = dict(representatives[unit.evidence_ids[0]])
            payload.pop("evidence_id", None)
            entry["representative"] = payload
        else:
            entry["glimpse"] = glimpses[unit.evidence_ids[0]]
        by_case[unit.case_id].append(entry)
    body = {
        "cases": [
            {
                "case": table.case_ref(case_id),
                "header": headers[case_id],
                "contrast": contrast.get(case_id),
                "units": by_case[case_id],
            }
            for case_id in portion.case_ids
        ]
    }
    return _json_text(body)
