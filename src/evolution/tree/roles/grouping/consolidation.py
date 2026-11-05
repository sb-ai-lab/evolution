"""Batched multi-level consolidation of the family-15 chat Grouper (spec §7, ADR-0055).

Portions form their groups apart, so groups that describe one failure can
come from different portions. Consolidation compares them in bounded
batches and merges them with today's reduce rules.

* A `Card` is built by code from one group (`build_card`, §7.1). It shows the
  group's header, its member, case and per-criterion counts, the reported
  provenance counts of its members and the glimpses of its members with the
  lowest o-refs. The counts are bounded as a case header's are
  (`grouping_portions.bounded_histograms`), so a card does not grow with its
  members. Membership stays in the portion checkpoints, outside the model's
  context.
* `plan_batches` orders the cards by the failed criteria most common among
  their members, then by local id, and fills batches in that order within
  ``2 x capacity`` cards and the input budget (§7.2). Levels alternate
  between unshifted boundaries and boundaries shifted by half a batch.
* `consolidate` runs the levels. Each batch is one request without tools,
  made by the caller's ``ask``; its merges are applied with `apply_merges`,
  in batch order whatever the completion order (§7.3). A batch whose answer
  is cut, times out or whose checkpoint would exceed the record budget is
  split in two, as a portion is; a batch of two cards that still fails this
  way makes no merge (``batch_unsplittable``).

Batch ids follow the portion ids: ``b1, b2, ...`` per level in plan order,
and the halves of ``b3`` are ``b3.1`` and ``b3.2``. Every finished leaf
batch is reported once through ``on_batch``, as it finishes, so C7 can
checkpoint it (`batch_body`); an adopted leaf whose input digest matches is
replayed without calling ``ask``.

Group local ids are relabelled ``g1, g2, ...`` in input order, and a merged
group keeps the id of its first card, so card ids stay short at every level
and the checkpointed merges stay bounded. Local ids are never durable.

This module is in `roles/` and imports nothing from the campaign (ADR-0045).
"""

from __future__ import annotations

import asyncio
import dataclasses
import heapq
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from evolution.tree.roles.grouping.grouping_portions import (
    RefTable,
    _json_text,
    bounded_histograms,
    portion_input_size,
    redacted_counts,
)
from evolution.tree.roles.grouping.model_grouping import (
    ModelGroup,
    ObservationRow,
    _failed_criterion_counts,
    _text_key,
    apply_merges,
)
from evolution.tree.roles.grouping.portion_session import MaxRequestsReached
from evolution.tree.roles.grouping.reflect_grouping import _canonical_json, _digest

if TYPE_CHECKING:
    from evolution.tree.roles.grouping.model_grouping import CaseProfile

__all__ = [
    "BATCH_LEAD",
    "INVALID",
    "SPLIT_OUTCOMES",
    "UNSPLITTABLE",
    "Ask",
    "BatchAnswer",
    "BatchCheckpoint",
    "Card",
    "CardEvidence",
    "Consolidated",
    "ConsolidationLimits",
    "Merges",
    "batch_body",
    "batch_digest",
    "batch_from_body",
    "batch_input",
    "batch_input_size",
    "batch_record_size",
    "build_card",
    "card_evidence",
    "card_order",
    "consolidate",
    "normalized_answer",
    "plan_batches",
]

#: The user message of a batch request is this lead plus the cards' JSON.
BATCH_LEAD: Final = "Group cards to consider merging:\n"
_CARDS_OPEN: Final = '{"cards":['
_CARDS_CLOSE: Final = "]}"
#: ``ask`` outcomes that split a batch (§7.2): an answer cut at the output
#: limit, or a request that reached ``timeout_seconds`` with its repairs.
SPLIT_OUTCOMES: Final = frozenset({"cut", "session_timeout"})
#: Outcome of a valid answer whose merge list could not be admitted after its
#: repairs (today's reduce ``invalid``); it makes no merge.
INVALID: Final = "invalid"
#: Outcome of a two-card batch that was still cut, timed out or over the
#: record budget; it makes no merge and is checkpointed so a resume does not
#: pay for it again.
UNSPLITTABLE: Final = "batch_unsplittable"
_RECORD_BUDGET: Final = "record_budget"
_BATCH_FORMAT: Final = "grouping_batch_input/1"
_HEADER_KEYS: Final = ("title", "failure_mode", "expected_behaviour", "question", "priority")

#: ``(card ids, merged header or None)`` per merge, as `parse_reduce_response` returns.
Merges = tuple[tuple[tuple[str, ...], ModelGroup | None], ...]


# --- Cards ----------------------------------------------------------------------


@dataclass(frozen=True)
class CardEvidence:
    """Per-observation facts cards are built from, keyed by evidence ID.

    ``rows`` are the case profiles' `ObservationRow`s, ``glimpses`` the
    portions' glimpses, ``order`` each failing observation's o-ref index and
    ``used`` the stored `SampleProvenance.used` unions (`None` or absent when
    the target reported none), as `grouping_portions.provenance_counts` reads
    them.
    """

    rows: Mapping[str, ObservationRow]
    glimpses: Mapping[str, str]
    order: Mapping[str, int]
    used: Mapping[str, tuple[str, ...] | None]


def card_evidence(
    profiles: Iterable[CaseProfile],
    table: RefTable,
    *,
    glimpses: Mapping[str, str],
    used: Mapping[str, tuple[str, ...] | None],
) -> CardEvidence:
    """The `CardEvidence` of one grouping, from what the portion planner built."""

    return CardEvidence(
        rows={row.evidence_id: row for profile in profiles for row in profile.rows},
        glimpses=glimpses,
        order={evidence_id: i for i, evidence_id in enumerate(table.observations)},
        used=used,
    )


@dataclass(frozen=True)
class Card:
    """One group as a batch shows it: its id, its canonical JSON and its order key."""

    local_id: str
    text: str
    criteria_key: tuple[str, ...]


def _criteria_key(rows: Iterable[ObservationRow]) -> tuple[str, ...]:
    """The failed-criteria list most common among the rows, the smallest on a tie."""

    counts: dict[tuple[str, ...], int] = {}
    for row in rows:
        criteria = row.failed_criteria
        items = criteria if isinstance(criteria, (list, tuple)) else ()
        key = tuple(sorted({_text_key(item) for item in items}))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ()
    return min(counts, key=lambda key: (-counts[key], key))


def build_card(
    group: ModelGroup,
    *,
    evidence: CardEvidence,
    examples: int,
    chars: int,
    redact: Callable[[str], str] | None = None,
) -> Card:
    """The card of one group (§7.1); its size does not depend on its member count.

    The card holds the group's id, ``title``, ``failure_mode``,
    ``expected_behaviour`` and ``question`` (within today's bounds), a
    ``counts`` object and ``examples``. ``counts`` holds the member and case
    counts, the failing members per failed criterion and, when a member
    reported provenance, the members per reported component and the unknown
    count; its canonical JSON is within ``chars``, filled as a case header's
    histograms are, with the rest counted in one remainder entry. ``redact``
    is applied to every component ID before it is counted, as in
    `grouping_portions.case_header`. ``examples`` are the glimpses of the
    ``examples`` members with the lowest o-refs (0 allowed).
    """

    member_ids = [evidence_id for evidence_id, _ in group.members]
    rows = [evidence.rows[evidence_id] for evidence_id in member_ids]
    components: dict[str, int] = {}
    unknown = 0
    reported = False
    for evidence_id in member_ids:
        used = evidence.used.get(evidence_id)
        if used is None:
            unknown += 1
            continue
        reported = True
        for component in sorted(set(used)):
            components[component] = components.get(component, 0) + 1
    lists: list[Mapping[str, int]] = [_failed_criterion_counts(rows)]
    if reported:
        lists.append(redacted_counts(components, redact))

    def build(listed: Sequence[list[dict[str, Any]]]) -> dict[str, Any]:
        counts: dict[str, Any] = {
            "members": len(member_ids),
            "cases": len(group.affected_cases),
            "failed_criterion_counts": listed[0],
        }
        if reported:
            counts["provenance"] = {"components": listed[1], "unknown": unknown}
        return counts

    lowest = heapq.nsmallest(max(examples, 0), member_ids, key=lambda eid: evidence.order[eid])
    body = {
        "id": group.local_id,
        "title": group.title,
        "failure_mode": group.failure_mode,
        "expected_behaviour": group.expected_behaviour,
        "question": group.question,
        "counts": bounded_histograms(lists, build, chars=chars),
        "examples": [evidence.glimpses[evidence_id] for evidence_id in lowest],
    }
    return Card(group.local_id, _json_text(body), _criteria_key(rows))


def card_order(card: Card) -> tuple[tuple[str, ...], int, str]:
    """Cards in order of their failed criteria, then of their local ids (``g2`` before ``g10``)."""

    return card.criteria_key, len(card.local_id), card.local_id


# --- Batches ----------------------------------------------------------------------


def batch_input(batch: Sequence[Card]) -> str:
    """A batch's user message: the lead and ``{"cards": [...]}`` (the cards' JSON)."""

    return BATCH_LEAD + _CARDS_OPEN + ",".join(card.text for card in batch) + _CARDS_CLOSE


def _escaped(text: str) -> int:
    """Characters ``text`` takes inside a JSON string, without the quotes."""

    return portion_input_size(text) - 2


_FRAME: Final = 2 + _escaped(BATCH_LEAD + _CARDS_OPEN) + _escaped(_CARDS_CLOSE)


def batch_input_size(batch: Sequence[Card]) -> int:
    """`portion_input_size` of `batch_input`: what the batch adds to a request.

    JSON escapes each character on its own, so the size is the frame plus each
    card's escaped text and one separator between cards.
    """

    return _FRAME + sum(_escaped(card.text) for card in batch) + max(len(batch) - 1, 0)


def batch_digest(batch: Sequence[Card]) -> str:
    """The input digest of a batch, which binds its checkpoint (§9)."""

    return _digest({"format": _BATCH_FORMAT, "input": batch_input(batch)})


def plan_batches(
    cards: Sequence[Card], *, capacity: int, input_budget: int, shifted: bool
) -> tuple[tuple[Card, ...], ...]:
    """The batches of one level (§7.2), a pure function of the cards and limits.

    Cards are put in `card_order`. Code fills a batch in that order until one
    more card would raise it above ``2 x capacity`` cards or its
    `batch_input_size` above ``input_budget``, so every possible merge can
    carry a header within the output limit. A card that does not fit even
    alone is a batch by itself, which is never sent.

    A ``shifted`` level moves the boundaries by half a batch: its first batch
    holds half the cards an unshifted first batch would, and the rest is
    filled as usual, so neighbours across an unshifted boundary meet. When
    every card fits one batch, both arrangements are that one batch.
    """

    for name, value in (("capacity", capacity), ("input_budget", input_budget)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid_{name}")
    ordered = sorted(cards, key=card_order)
    sizes = [_escaped(card.text) for card in ordered]
    most = 2 * capacity

    def fill(start: int, limit: int) -> int:
        """The end of a batch that starts at ``start`` and holds at most ``limit`` cards."""

        end = start + 1
        size = _FRAME + sizes[start]
        while end < len(ordered) and end - start < limit:
            size += 1 + sizes[end]
            if size > input_budget:
                break
            end += 1
        return end

    batches: list[tuple[Card, ...]] = []
    start = 0
    if shifted and ordered:
        first = fill(0, most)
        if first < len(ordered) and first // 2 >= 1:
            start = first // 2
            batches.append(tuple(ordered[:start]))
    while start < len(ordered):
        end = fill(start, most)
        batches.append(tuple(ordered[start:end]))
        start = end
    return tuple(batches)


# --- Answers and checkpoints --------------------------------------------------------


@dataclass(frozen=True)
class BatchAnswer:
    """One batch request's admitted decision, as ``ask`` returns it.

    ``merges`` are `parse_reduce_response`'s merges over the batch's card ids
    when ``outcome`` is `None`, and empty otherwise. ``outcome`` is `None`
    for an admitted answer, `INVALID` for an answer invalid after its repairs,
    or one of `SPLIT_OUTCOMES` (from ``ask`` only: the batch splits).
    Consolidation records `UNSPLITTABLE` for a two-card batch that cannot
    split. ``skipped`` counts merges over the variant limit; consolidation
    computes it when it applies the merges. ``requests`` are the requests
    sent for this batch, repairs included.
    """

    merges: Merges
    skipped: int
    outcome: str | None
    requests: int


@dataclass(frozen=True)
class BatchCheckpoint:
    """A finished leaf batch: what ``on_batch`` receives and what is adopted."""

    level: int
    batch_id: str
    input_digest: str
    answer: BatchAnswer


def _merge_body(ids: tuple[str, ...], header: ModelGroup | None) -> dict[str, Any]:
    body: dict[str, Any] = {"groups": list(ids)}
    if header is not None:
        body.update({key: getattr(header, key) for key in _HEADER_KEYS})
    return body


def batch_body(checkpoint: BatchCheckpoint) -> dict[str, Any]:
    """The variable part of a ``grouping_batch_finished`` record (§9).

    ``merges`` have the reduce answer's shape (``groups`` and the optional
    header keys); ``outcome`` is present when the answer was not admitted.
    The journal envelope, the grouping id and the plan digest are the rest
    of the record, which `grouping_portions.CHECKPOINT_ENVELOPE_BYTES`
    reserves. ``adopted`` checkpoints reach `consolidate` already filtered
    to the current plan (`model_grouping_v2`).
    """

    answer = checkpoint.answer
    body: dict[str, Any] = {
        "level": checkpoint.level,
        "batch_id": checkpoint.batch_id,
        "input_digest": checkpoint.input_digest,
        "merges": [_merge_body(ids, header) for ids, header in answer.merges],
        "skipped": answer.skipped,
        "requests": answer.requests,
    }
    if answer.outcome is not None:
        body["outcome"] = answer.outcome
    return body


def _merges_from_body(raw_merges: Sequence[Mapping[str, Any]]) -> Merges:
    """The merges of a `batch_body`, as a resumed grouping reads them."""

    merges: list[tuple[tuple[str, ...], ModelGroup | None]] = []
    for position, raw in enumerate(raw_merges):
        header = None
        if "title" in raw:
            header = ModelGroup(
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
        merges.append((tuple(raw["groups"]), header))
    return tuple(merges)


def batch_from_body(body: Mapping[str, Any]) -> BatchCheckpoint:
    """The `BatchCheckpoint` of a `batch_body`, for adoption on resume."""

    answer = BatchAnswer(
        _merges_from_body(body["merges"]), body["skipped"], body.get("outcome"), body["requests"]
    )
    return BatchCheckpoint(body["level"], body["batch_id"], body["input_digest"], answer)


def normalized_answer(answer: BatchAnswer, normalize: Callable[[Any], Any]) -> BatchAnswer:
    """``answer`` in the form its checkpoint persists and a resume reads (PDR-0044).

    ``normalize`` maps the merges as `batch_body` writes them (the run-secret
    redaction of the journal); they are read back as `batch_from_body` reads
    them, so a merged header is the same whether replayed or just answered.
    """

    merges = [_merge_body(ids, header) for ids, header in answer.merges]
    return dataclasses.replace(answer, merges=_merges_from_body(normalize(merges)))


def batch_record_size(checkpoint: BatchCheckpoint) -> int:
    """Bytes of `batch_body` as canonical JSON, measured against ``record_budget``."""

    return len(_canonical_json(batch_body(checkpoint)))


# --- Consolidation ----------------------------------------------------------------------


@dataclass(frozen=True)
class ConsolidationLimits:
    """The limits one consolidation runs within; the caller computes them.

    * ``capacity``: `plan_portions`' capacity,
      ``max(1, max_output_tokens // group_output_tokens)``;
    * ``input_budget``: what the cards may add to a batch request,
      ``max_input_chars`` less the reduce system prompt and the repair
      reserve;
    * ``record_budget``: ``MAX_RECORD_BYTES - CHECKPOINT_ENVELOPE_BYTES``;
    * ``concurrency``: batch requests at once (``section.concurrency``);
    * ``max_levels``: ``max_reduce_levels``;
    * ``card_examples`` and ``card_chars`` (``glimpse_chars``): `build_card`'s.
    """

    capacity: int
    input_budget: int
    record_budget: int
    concurrency: int
    max_levels: int
    card_examples: int
    card_chars: int


@dataclass(frozen=True)
class Consolidated:
    """The groups after consolidation and how it went.

    ``levels`` counts the levels entered, a level stopped by ``max_requests``
    included. ``reduce_state`` is `calls`' ``reduce`` (Builder decision 7):
    ``none`` when no batch had two cards, ``done`` when a batch answer was
    admitted, ``invalid`` when batches were answered but none was admitted,
    ``skipped`` when ``max_requests`` stopped it before any batch was
    answered. ``skipped`` counts the merges over the variant limit.
    """

    groups: tuple[ModelGroup, ...]
    levels: int
    reduce_state: str
    skipped: int = 0


Ask = Callable[[tuple[Card, ...], int, str], Awaitable[BatchAnswer]]


def _halves(batch: tuple[Card, ...]) -> tuple[tuple[Card, ...], tuple[Card, ...]]:
    middle = (len(batch) + 1) // 2
    return batch[:middle], batch[middle:]


def _apply(
    by_id: Mapping[str, ModelGroup], ids: Sequence[str], merges: Merges
) -> tuple[list[ModelGroup], int, int]:
    """Apply one leaf's merges to its groups: ``(groups, applied, skipped)``.

    `apply_merges` names a merged group by joining its parts' ids with
    ``+``; no card id holds ``+``, so the merged group is renamed to its first
    card's id, which is unique and short.
    """

    applied = apply_merges([by_id[local] for local in ids], merges)
    groups: list[ModelGroup] = []
    merged = 0
    for group in applied.groups:
        if "+" in group.local_id:
            merged += 1
            group = dataclasses.replace(group, local_id=group.local_id.split("+", 1)[0])
        groups.append(group)
    return groups, merged, applied.skipped


class _Level:
    """One level's batches: asking, splitting, adoption and checkpoints.

    ``stopped`` turns true when ``ask`` raised `MaxRequestsReached` or any
    other exception; no further batch of the level is asked after that. The
    other exception is re-raised and ends consolidation.
    """

    def __init__(
        self,
        level: int,
        by_id: Mapping[str, ModelGroup],
        *,
        ask: Ask,
        limits: ConsolidationLimits,
        adopted: Mapping[tuple[int, str], BatchCheckpoint],
        on_batch: Callable[[BatchCheckpoint], None],
        log: Callable[[Mapping[str, Any]], None],
        semaphore: asyncio.Semaphore,
    ) -> None:
        self.level = level
        self.by_id = by_id
        self.ask = ask
        self.limits = limits
        self.adopted = adopted
        self.on_batch = on_batch
        self.log = log
        self.semaphore = semaphore
        self.stopped = False
        self.split_before = {
            parent for key_level, key in adopted if key_level == level for parent in _ancestors(key)
        }

    async def run(self, batches: Sequence[tuple[Card, ...]]) -> list[list[BatchAnswer]]:
        """Each batch's finished leaf answers in leaf order; empty if not answered."""

        async def top(batch: tuple[Card, ...], batch_id: str) -> list[BatchAnswer]:
            return await self._batch(batch, batch_id) or []

        return await _gather([top(batch, f"b{n}") for n, batch in enumerate(batches, 1)])

    async def _ask(self, batch: tuple[Card, ...], batch_id: str) -> BatchAnswer | None:
        if self.stopped:
            return None
        async with self.semaphore:
            if self.stopped:
                return None
            try:
                return await self.ask(batch, self.level, batch_id)
            except MaxRequestsReached:
                self.stopped = True
                return None
            except BaseException:
                # Before the slot is released: a queued batch must not start
                # after a provider failure (Builder decision 10), as the map
                # scheduler's sessions do not.
                self.stopped = True
                raise

    def _finish(self, batch_id: str, digest: str, answer: BatchAnswer) -> list[BatchAnswer]:
        self.on_batch(BatchCheckpoint(self.level, batch_id, digest, answer))
        return [answer]

    async def _batch(self, batch: tuple[Card, ...], batch_id: str) -> list[BatchAnswer] | None:
        """The finished leaves of one batch, or `None` if one of them was not answered.

        A batch of fewer than two cards can merge nothing: it is never asked
        and never checkpointed, whether it is planned so or is a half.
        """

        if len(batch) < 2:
            return []
        digest = batch_digest(batch)
        prior = self.adopted.get((self.level, batch_id))
        if prior is not None and prior.input_digest == digest:
            return [prior.answer]
        reason: str | None = "adopted_split"
        requests = 0
        if batch_id not in self.split_before or len(batch) <= 2:
            answer = await self._ask(batch, batch_id)
            if answer is None:
                return None
            requests = answer.requests
            reason = answer.outcome if answer.outcome in SPLIT_OUTCOMES else None
            if reason is None:
                ids = [card.local_id for card in batch]
                answer = dataclasses.replace(
                    answer, skipped=_apply(self.by_id, ids, answer.merges)[2]
                )
                checkpoint = BatchCheckpoint(self.level, batch_id, digest, answer)
                if batch_record_size(checkpoint) <= self.limits.record_budget:
                    if answer.skipped:
                        self._log("batch_merges_skipped", batch_id, count=answer.skipped)
                    return self._finish(batch_id, digest, answer)
                reason = _RECORD_BUDGET
        if len(batch) <= 2:
            self._log("batch_unsplittable", batch_id, reason=reason, cards=len(batch))
            return self._finish(batch_id, digest, BatchAnswer((), 0, UNSPLITTABLE, requests))
        halves = _halves(batch)
        names = [f"{batch_id}.1", f"{batch_id}.2"]
        self._log("batch_split", batch_id, reason=reason, halves=names, requests=requests)
        results = await _gather(
            [self._batch(half, name) for half, name in zip(halves, names, strict=True)]
        )
        if any(result is None for result in results):
            # A half not answered: its finished sibling is checkpointed and
            # adopted on resume, but nothing of this batch applies now.
            return None
        return [answer for result in results if result is not None for answer in result]

    def _log(self, phase: str, batch_id: str, **fields: Any) -> None:
        self.log({"phase": phase, "level": self.level, "batch": batch_id, **fields})


async def consolidate(
    groups: Sequence[ModelGroup],
    *,
    evidence: CardEvidence,
    ask: Ask,
    limits: ConsolidationLimits,
    adopted: Mapping[tuple[int, str], BatchCheckpoint],
    on_batch: Callable[[BatchCheckpoint], None],
    log: Callable[[Mapping[str, Any]], None],
    redact: Callable[[str], str] | None = None,
) -> Consolidated:
    """Merge groups formed in different portions, level by level (§7.2, §7.3).

    Level ``n`` is unshifted when ``n`` is even and shifted when odd. Its
    batches run concurrently, at most ``limits.concurrency`` requests at once
    (consolidation throttles; ``ask`` sends one request with its repairs).
    The merges apply in batch order, never completion order. Consolidation
    stops after a level whose plan was one batch holding every card, after
    two consecutive levels without a merge, or after ``limits.max_levels``.

    ``ask(batch, level, batch_id)`` returns the batch's `BatchAnswer`; an
    outcome in `SPLIT_OUTCOMES`, or an answer whose checkpoint would exceed
    ``limits.record_budget``, splits the batch into ``<id>.1`` and ``<id>.2``.
    It raises `MaxRequestsReached` when ``max_requests`` was reached: no
    further batch is asked, the finished batches of the current level apply
    and consolidation stops. Any other exception ends consolidation after the
    running batches are cancelled; every batch finished before it was already
    reported. ``on_batch`` sees each newly finished leaf once, as it
    finishes; an adopted one never. ``adopted`` maps ``(level, batch_id)``
    to a checkpoint, replayed when its input digest equals the batch's. A
    batch with an adopted descendant (``b3.1``) was split before and is split
    again without a request.
    """

    by_id: dict[str, ModelGroup] = {
        f"g{i + 1}": dataclasses.replace(group, local_id=f"g{i + 1}")
        for i, group in enumerate(groups)
    }
    semaphore = asyncio.Semaphore(limits.concurrency)
    answered = admitted = skipped_total = 0
    budget_reached = False
    level = quiet = 0

    while level < limits.max_levels and len(by_id) > 1:
        cards = [
            build_card(
                group,
                evidence=evidence,
                examples=limits.card_examples,
                chars=limits.card_chars,
                redact=redact,
            )
            for group in by_id.values()
        ]
        batches = plan_batches(
            cards,
            capacity=limits.capacity,
            input_budget=limits.input_budget,
            shifted=level % 2 == 1,
        )
        runner = _Level(
            level,
            by_id,
            ask=ask,
            limits=limits,
            adopted=adopted,
            on_batch=on_batch,
            log=log,
            semaphore=semaphore,
        )
        results = await runner.run(batches)
        merged = 0
        next_groups: dict[str, ModelGroup] = {}
        # Batch order, then leaf order: never the order the answers arrived in.
        for batch, leaves in zip(batches, results, strict=True):
            changed: dict[str, ModelGroup] = {}
            consumed: set[str] = set()
            for answer in leaves:
                answered += 1
                if answer.outcome is not None:
                    continue
                admitted += 1
                ids = sorted({local for ids, _ in answer.merges for local in ids}, key=_id_order)
                after, count, skipped = _apply(by_id, ids, answer.merges)
                merged += count
                skipped_total += skipped
                consumed.update(ids)
                changed.update({group.local_id: group for group in after})
            for card in batch:
                if card.local_id in changed:
                    next_groups[card.local_id] = changed[card.local_id]
                elif card.local_id not in consumed:
                    next_groups[card.local_id] = by_id[card.local_id]
        by_id = next_groups
        level += 1
        if runner.stopped:
            budget_reached = True
            log({"phase": "max_requests_reached", "stage": "consolidation", "level": level - 1})
            break
        if len(batches) == 1:
            break
        quiet = 0 if merged else quiet + 1
        if quiet >= 2:
            break

    if admitted:
        state = "done"
    elif answered:
        state = "invalid"
    elif budget_reached:
        state = "skipped"
    else:
        state = "none"
    ordered = sorted(by_id.values(), key=lambda group: _id_order(group.local_id))
    return Consolidated(tuple(ordered), level, state, skipped_total)


def _id_order(local: str) -> tuple[int, str]:
    return len(local), local


def _ancestors(batch_id: str) -> list[str]:
    parents = []
    while "." in batch_id:
        batch_id = batch_id.rsplit(".", 1)[0]
        parents.append(batch_id)
    return parents


async def _gather(coroutines: Sequence[Awaitable[Any]]) -> list[Any]:
    """Run coroutines concurrently; on the first failure cancel the rest and re-raise it."""

    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        failure = None if task.cancelled() else task.exception()
        if failure is not None:
            raise failure
    return [task.result() for task in tasks]
