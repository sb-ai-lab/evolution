"""Batched multi-level consolidation of grouping cards (b16 spec §7, task C5)."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from evolution.tree.roles.grouping import consolidation as cons
from evolution.tree.roles.grouping import grouping_portions as gp
from evolution.tree.roles.grouping.model_grouping import (
    ModelGroup,
    ObservationRow,
    build_case_profiles,
)
from evolution.tree.roles.grouping.portion_session import RequestBudget
from evolution.tree.roles.role_call import RoleCallExhausted
from tests.tree.roles.grouping.test_grouping_portions import rec, registry

RECORD_BUDGET = 64 * 1024 - gp.CHECKPOINT_ENVELOPE_BYTES


# --- Fixtures -----------------------------------------------------------------


def row(eid: str, criteria: Sequence[str] = ("c1",)) -> ObservationRow:
    return ObservationRow(eid, 0, "failing", "completed", False, list(criteria), 0.0, "s")


def evidence(
    eids: Sequence[str],
    *,
    criteria: Mapping[str, Sequence[str]] | None = None,
    used: Mapping[str, tuple[str, ...] | None] | None = None,
) -> cons.CardEvidence:
    criteria = criteria or {}
    return cons.CardEvidence(
        rows={eid: row(eid, criteria.get(eid, ("c1",))) for eid in eids},
        glimpses={eid: f"glimpse of {eid}" for eid in eids},
        order={eid: i for i, eid in enumerate(eids)},
        used=used or {},
    )


def group(
    local: str,
    eids: Sequence[str],
    *,
    title: str = "t",
    variant: str = "v",
    variants: Sequence[str] | None = None,
) -> ModelGroup:
    labels = list(variants) if variants is not None else [variant] * len(eids)
    cases = tuple(eid.split("#")[0] for eid in eids)
    return ModelGroup(
        local_id=local,
        title=title,
        failure_mode=f"fm {title}",
        expected_behaviour="eb",
        question="q?",
        members=tuple(zip(eids, labels, strict=True)),
        affected_cases=tuple(sorted(set(cases))),
        citations=("verdict:c1",),
        priority="p",
        metrics=(),
        member_cases=cases,
    )


def limits(**overrides: Any) -> cons.ConsolidationLimits:
    values: dict[str, Any] = {
        "capacity": 8,
        "input_budget": 200_000,
        "record_budget": RECORD_BUDGET,
        "concurrency": 4,
        "max_levels": 4,
        "card_examples": 2,
        "card_chars": 800,
    }
    values.update(overrides)
    return cons.ConsolidationLimits(**values)


def card_title(card: cons.Card) -> str:
    return json.loads(card.text)["title"]


def merge_equal_titles(batch: Sequence[cons.Card]) -> tuple[tuple[tuple[str, ...], None], ...]:
    """Merge every two or more cards of the batch whose titles are equal."""

    by_title: dict[str, list[str]] = {}
    for card in batch:
        by_title.setdefault(card_title(card), []).append(card.local_id)
    return tuple((tuple(ids), None) for ids in by_title.values() if len(ids) > 1)


class Asker:
    """A scripted `ask`: merges equal titles, records every call."""

    def __init__(self, *, decide=None, delays=None, budget: RequestBudget | None = None):
        self.calls: list[tuple[int, str, tuple[str, ...]]] = []
        self.decide = decide
        self.delays = delays or {}
        self.budget = budget

    async def __call__(self, batch, level, batch_id):
        if self.budget is not None:
            self.budget.take()
        self.calls.append((level, batch_id, tuple(card.local_id for card in batch)))
        delay = self.delays.get((level, batch_id))
        if delay:
            await asyncio.sleep(delay)
        if self.decide is not None:
            answer = self.decide(batch, level, batch_id)
            if answer is not None:
                return answer
        return cons.BatchAnswer(
            merges=merge_equal_titles(batch), skipped=0, outcome=None, requests=1
        )


class Sink:
    def __init__(self) -> None:
        self.checkpoints: list[cons.BatchCheckpoint] = []
        self.log: list[dict[str, Any]] = []

    def on_batch(self, checkpoint: cons.BatchCheckpoint) -> None:
        self.checkpoints.append(checkpoint)

    def record(self, value: Mapping[str, Any]) -> None:
        self.log.append(dict(value))


async def run(groups, ev, ask, *, adopted=None, sink=None, **overrides):
    sink = sink or Sink()
    result = await cons.consolidate(
        groups,
        evidence=ev,
        ask=ask,
        limits=limits(**overrides),
        adopted=adopted or {},
        on_batch=sink.on_batch,
        log=sink.record,
    )
    return result, sink


def members_of(result: cons.Consolidated) -> list[frozenset[str]]:
    return sorted(
        (frozenset(eid for eid, _ in g.members) for g in result.groups),
        key=lambda s: sorted(s),
    )


def singles(
    n: int, *, titles: Sequence[str] | None = None
) -> tuple[list[ModelGroup], cons.CardEvidence]:
    eids = [f"case{i:03d}#0" for i in range(n)]
    names = list(titles) if titles is not None else [f"t{i}" for i in range(n)]
    groups = [group(f"p{i + 1}-g1", [eid], title=names[i]) for i, eid in enumerate(eids)]
    return groups, evidence(eids)


# --- Cards ----------------------------------------------------------------------


def test_card_bounded_as_members_grow():
    def card_for(n: int) -> cons.Card:
        eids = [f"case#{i}" for i in range(n)]
        ev = evidence(
            eids,
            criteria={eid: (f"criterion-{i}",) for i, eid in enumerate(eids)},
            used={eid: (f"component-{i}",) for i, eid in enumerate(eids)},
        )
        return cons.build_card(group("g1", eids), evidence=ev, examples=2, chars=800)

    small, large = card_for(3), card_for(20_000)
    body = json.loads(large.text)
    counts = body["counts"]
    assert counts["members"] == 20_000
    assert counts["cases"] == 1
    assert len(gp._json_text(counts)) <= 800
    # Every criterion and component is counted: the rest is one remainder entry.
    listed = counts["failed_criterion_counts"]
    assert listed[-1]["more"] > 0
    assert sum(entry["count"] for entry in listed) == 20_000
    components = counts["provenance"]["components"]
    assert sum(entry["count"] for entry in components) == 20_000
    assert counts["provenance"]["unknown"] == 0
    # Examples are the lowest o-refs; the card does not grow with members.
    assert body["examples"] == ["glimpse of case#0", "glimpse of case#1"]
    assert len(large.text) <= len(small.text) + 800
    assert len(card_for(40_000).text) <= len(small.text) + 800


def test_card_examples_zero_and_no_provenance_line():
    ev = evidence(["a#0", "a#1"])
    card = cons.build_card(group("g1", ["a#0", "a#1"]), evidence=ev, examples=0, chars=800)
    body = json.loads(card.text)
    assert body["examples"] == []
    assert "provenance" not in body["counts"]
    assert set(body) == {
        "id",
        "title",
        "failure_mode",
        "expected_behaviour",
        "question",
        "counts",
        "examples",
    }


def test_card_provenance_redacted_and_unknown_counted():
    eids = ["a#0", "a#1", "a#2"]
    ev = evidence(eids, used={"a#0": ("secret-x",), "a#1": ("plain",), "a#2": None})
    card = cons.build_card(
        group("g1", eids),
        evidence=ev,
        examples=1,
        chars=800,
        redact=lambda text: "[redacted]" if "secret" in text else text,
    )
    provenance = json.loads(card.text)["counts"]["provenance"]
    assert provenance == {
        "components": [{"name": "[redacted]", "count": 1}, {"name": "plain", "count": 1}],
        "unknown": 1,
    }
    assert "secret" not in card.text


def test_card_criteria_key_is_most_common_failed_criteria():
    eids = ["a#0", "a#1", "a#2"]
    ev = evidence(eids, criteria={"a#0": ("z", "b"), "a#1": ("b", "z"), "a#2": ("a",)})
    card = cons.build_card(group("g1", eids), evidence=ev, examples=0, chars=800)
    assert card.criteria_key == ("b", "z")


def test_card_evidence_from_profiles():
    reg = registry([rec("a", 1, criteria=("x",)), rec("a", 2, criteria=("y",)), rec("b", 1)])
    profiles = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    table = gp.reference_table(reg)
    glimpses = {r.evidence_id: gp.glimpse(r, chars=200) for r in reg.records}
    ev = cons.card_evidence(profiles, table, glimpses=glimpses, used={"a#1": ("m",)})
    assert ev.order == {"a#1": 0, "a#2": 1, "b#1": 2}
    assert ev.rows["a#2"].failed_criteria == ["y"]
    assert ev.used == {"a#1": ("m",)}


# --- Batches ----------------------------------------------------------------------


def test_batches_respect_both_limits():
    groups, ev = singles(40)
    cards = [cons.build_card(g, evidence=ev, examples=1, chars=400) for g in groups]
    one = cons.batch_input_size((cards[0],))
    two = cons.batch_input_size(tuple(cards[:2]))
    assert one == gp.portion_input_size(cons.batch_input((cards[0],)))
    assert two == gp.portion_input_size(cons.batch_input(tuple(cards[:2])))
    # Capacity binds: at most 2 x capacity cards.
    batches = cons.plan_batches(cards, capacity=3, input_budget=10**9, shifted=False)
    assert [len(b) for b in batches] == [6] * 6 + [4]
    # The input budget binds: five cards fit, six do not.
    budget = cons.batch_input_size(tuple(sorted(cards, key=cons.card_order)[:5]))
    batches = cons.plan_batches(cards, capacity=100, input_budget=budget, shifted=False)
    assert all(cons.batch_input_size(b) <= budget for b in batches)
    assert len(batches[0]) == 5
    assert sum(len(b) for b in batches) == 40
    # Every card is in exactly one batch, in card order.
    flat = [card.local_id for batch in batches for card in batch]
    assert flat == [card.local_id for card in sorted(cards, key=cons.card_order)]
    # Deterministic, whatever the input order.
    shuffled = list(cards)
    random.Random(7).shuffle(shuffled)
    assert cons.plan_batches(shuffled, capacity=3, input_budget=10**9, shifted=True) == (
        cons.plan_batches(cards, capacity=3, input_budget=10**9, shifted=True)
    )


def test_shifted_batches_move_boundaries_by_half_a_batch():
    groups, ev = singles(12)
    cards = [cons.build_card(g, evidence=ev, examples=0, chars=400) for g in groups]
    plain = cons.plan_batches(cards, capacity=2, input_budget=10**9, shifted=False)
    shifted = cons.plan_batches(cards, capacity=2, input_budget=10**9, shifted=True)
    assert [len(b) for b in plain] == [4, 4, 4]
    assert [len(b) for b in shifted] == [2, 4, 4, 2]
    # All cards fit one batch: shifting keeps the one batch.
    whole = cons.plan_batches(cards, capacity=6, input_budget=10**9, shifted=True)
    assert [len(b) for b in whole] == [12]


def test_cards_ordered_by_failed_criteria_then_id():
    eids = ["a#0", "b#0", "c#0"]
    ev = evidence(eids, criteria={"a#0": ("z",), "b#0": ("a",), "c#0": ("m",)})
    cards = [
        cons.build_card(group(f"g{i + 1}", [eid]), evidence=ev, examples=0, chars=400)
        for i, eid in enumerate(eids)
    ]
    (batch,) = cons.plan_batches(cards, capacity=4, input_budget=10**9, shifted=False)
    assert [card.local_id for card in batch] == ["g2", "g3", "g1"]


# --- Consolidation --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_from_different_portions_merge_exactly():
    groups = [
        group("p1-g1", ["a#0", "a#1"], title="same"),
        group("p2-g1", ["a#2", "b#0"], title="same"),
        group("p3-g1", ["c#0"], title="other"),
        group("p4-g2", ["c#1", "d#0", "d#1"], title="same"),
    ]
    eids = [eid for g in groups for eid, _ in g.members]
    result, sink = await run(groups, evidence(eids), Asker())
    assert result.reduce_state == "done"
    assert members_of(result) == [
        frozenset({"a#0", "a#1", "a#2", "b#0", "c#1", "d#0", "d#1"}),
        frozenset({"c#0"}),
    ]
    merged = next(g for g in result.groups if len(g.members) == 7)
    assert len({eid for eid, _ in merged.members}) == 7
    assert merged.affected_cases == ("a", "b", "c", "d")
    assert sorted(eid for g in result.groups for eid, _ in g.members) == sorted(eids)
    # One batch held every card: one level, one checkpoint.
    assert result.levels == 1
    assert [(c.level, c.batch_id) for c in sink.checkpoints] == [(0, "b1")]


@pytest.mark.asyncio
async def test_merges_apply_in_batch_order_whatever_completion():
    groups, ev = singles(16, titles=[f"t{i // 2}" for i in range(16)])

    async def once(delays):
        ask = Asker(delays=delays)
        result, sink = await run(groups, ev, ask, capacity=2, max_levels=1)
        return result, sink

    fast_first, sink_a = await once({(0, f"b{i}"): 0.001 * i for i in range(1, 5)})
    slow_first, sink_b = await once({(0, f"b{i}"): 0.001 * (5 - i) for i in range(1, 5)})
    assert fast_first == slow_first
    assert [c.batch_id for c in sink_b.checkpoints] != [c.batch_id for c in sink_a.checkpoints]
    assert sorted(sink_a.checkpoints, key=lambda c: c.batch_id) == sorted(
        sink_b.checkpoints, key=lambda c: c.batch_id
    )
    assert len(fast_first.groups) == 8


@pytest.mark.asyncio
async def test_cut_batch_splits():
    groups, ev = singles(8, titles=["x", "x", "y", "y", "z", "z", "w", "w"])

    def decide(batch, level, batch_id):
        if len(batch) > 2:
            return cons.BatchAnswer(merges=(), skipped=0, outcome="cut", requests=1)
        return None

    ask = Asker(decide=decide)
    result, sink = await run(groups, ev, ask, capacity=4, max_levels=1)
    ids = [call[1] for call in ask.calls]
    assert ids[0] == "b1"
    assert set(ids) == {"b1", "b1.1", "b1.2", "b1.1.1", "b1.1.2", "b1.2.1", "b1.2.2"}
    assert sorted(c.batch_id for c in sink.checkpoints) == ["b1.1.1", "b1.1.2", "b1.2.1", "b1.2.2"]
    assert len(result.groups) == 4
    assert result.reduce_state == "done"
    splits = [entry for entry in sink.log if entry["phase"] == "batch_split"]
    assert sorted(entry["batch"] for entry in splits) == ["b1", "b1.1", "b1.2"]
    assert all(entry["reason"] == "cut" for entry in splits)


@pytest.mark.asyncio
async def test_one_card_half_is_never_asked():
    groups, ev = singles(3, titles=["x", "x", "y"])

    def decide(batch, level, batch_id):
        if len(batch) > 2:
            return cons.BatchAnswer(merges=(), skipped=0, outcome="cut", requests=1)
        if len(batch) == 2:
            return cons.BatchAnswer(merges=(), skipped=0, outcome="invalid", requests=1)
        return None  # a one-card answer would be admitted, if it were asked

    ask = Asker(decide=decide)
    result, sink = await run(groups, ev, ask, max_levels=1)
    assert [call[1] for call in ask.calls] == ["b1", "b1.1"]
    assert [c.batch_id for c in sink.checkpoints] == ["b1.1"]
    # Only the real answer counts: b1.1 was invalid, so the reduce is invalid.
    assert result.reduce_state == "invalid"
    assert len(result.groups) == 3
    assert not [e for e in sink.log if e["phase"] == "batch_unsplittable"]


@pytest.mark.asyncio
async def test_two_card_batch_still_cut_makes_no_merge_and_is_logged():
    groups, ev = singles(2, titles=["x", "x"])

    def decide(batch, level, batch_id):
        return cons.BatchAnswer(merges=(), skipped=0, outcome="session_timeout", requests=2)

    result, sink = await run(groups, ev, Asker(decide=decide))
    assert len(result.groups) == 2
    assert result.reduce_state == "invalid"
    (checkpoint,) = sink.checkpoints
    assert checkpoint.answer.outcome == "batch_unsplittable"
    assert checkpoint.answer.requests == 2
    (entry,) = [e for e in sink.log if e["phase"] == "batch_unsplittable"]
    assert entry == {
        "phase": "batch_unsplittable",
        "level": 0,
        "batch": "b1",
        "reason": "session_timeout",
        "cards": 2,
    }


@pytest.mark.asyncio
async def test_record_budget_splits_a_batch():
    groups, ev = singles(4, titles=["x", "x", "y", "y"])
    long_header = ModelGroup(
        local_id="r0",
        title="T" * 128,
        failure_mode="F" * 512,
        expected_behaviour="E" * 512,
        question="Q" * 512,
        members=(),
        affected_cases=(),
        citations=(),
        priority="P" * 256,
        metrics=(),
    )

    def decide(batch, level, batch_id):
        merges = tuple((ids, long_header) for ids, _ in merge_equal_titles(batch))
        return cons.BatchAnswer(merges=merges, skipped=0, outcome=None, requests=1)

    two = cons.BatchCheckpoint(
        0, "b1", "d" * 64, cons.BatchAnswer(((("g1", "g2"), long_header),), 0, None, 1)
    )
    budget = cons.batch_record_size(two) + 10
    result, sink = await run(groups, ev, Asker(decide=decide), record_budget=budget, max_levels=1)
    assert sorted(c.batch_id for c in sink.checkpoints) == ["b1.1", "b1.2"]
    assert all(cons.batch_record_size(c) <= budget for c in sink.checkpoints)
    assert len(result.groups) == 2
    assert [e["reason"] for e in sink.log if e["phase"] == "batch_split"] == ["record_budget"]


@pytest.mark.asyncio
async def test_stop_rules():
    # At most one group: no request, reduce state none.
    groups, ev = singles(1)
    ask = Asker()
    result, _ = await run(groups, ev, ask)
    assert (ask.calls, result.levels, result.reduce_state) == ([], 0, "none")

    # One batch held every card: stop after that level, even with merges left.
    groups, ev = singles(6, titles=["x"] * 6)
    ask = Asker()
    result, _ = await run(groups, ev, ask, capacity=8)
    assert result.levels == 1 and len(ask.calls) == 1 and len(result.groups) == 1

    # Two quiet levels (one of each arrangement) stop it before max_levels.
    groups, ev = singles(12)
    ask = Asker()
    result, _ = await run(groups, ev, ask, capacity=2, max_levels=10)
    assert result.levels == 2
    assert result.reduce_state == "done"
    assert {call[0] for call in ask.calls} == {0, 1}

    # max_levels bounds the levels while merges keep happening.
    titles = [f"t{i // 2}" for i in range(32)]
    groups, ev = singles(32, titles=titles)

    def chain(batch, level, batch_id):
        # Merge the first two cards of every batch, whatever their titles.
        if len(batch) < 2:
            return None
        ids = (batch[0].local_id, batch[1].local_id)
        return cons.BatchAnswer(merges=((ids, None),), skipped=0, outcome=None, requests=1)

    ask = Asker(decide=chain)
    result, _ = await run(groups, ev, ask, capacity=2, max_levels=3)
    assert result.levels == 3
    assert {call[0] for call in ask.calls} == {0, 1, 2}

    # A level whose batches all hold one card sends nothing; two such levels stop.
    groups, ev = singles(3)
    cards = [cons.build_card(g, evidence=ev, examples=2, chars=800) for g in groups]
    ask = Asker()
    result, _ = await run(
        groups, ev, ask, input_budget=cons.batch_input_size((cards[0],)), max_levels=9
    )
    assert (ask.calls, result.levels, result.reduce_state) == ([], 2, "none")


@pytest.mark.asyncio
async def test_boundary_neighbours_meet_in_shifted_level():
    # Eight cards of capacity 2: unshifted batches [1-4] [5-8]; cards 4 and 5
    # belong together but sit on either side of the boundary.
    titles = ["a", "b", "c", "x", "x", "d", "e", "f"]
    groups, ev = singles(8, titles=titles)
    ask = Asker()
    result, _ = await run(groups, ev, ask, capacity=2)
    level0 = [call for call in ask.calls if call[0] == 0]
    level1 = [call for call in ask.calls if call[0] == 1]
    assert [len(call[2]) for call in level0] == [4, 4]
    assert [len(call[2]) for call in level1] == [2, 4, 2]
    assert len(result.groups) == 7
    assert any(len(g.members) == 2 for g in result.groups)
    # Level 0 made no merge, level 1 merged, level 2 (unshifted) was quiet,
    # level 3 (shifted) was quiet: two quiet levels in a row stop it.
    assert result.levels == 4


@pytest.mark.asyncio
async def test_merge_over_variant_limit_skipped_and_counted():
    many = [f"v{i}" for i in range(10)]
    others = [f"w{i}" for i in range(10)]
    groups = [
        group("p1-g1", [f"a#{i}" for i in range(10)], title="x", variants=many),
        group("p2-g1", [f"b#{i}" for i in range(10)], title="x", variants=others),
        group("p3-g1", ["c#0"], title="y"),
        group("p4-g1", ["d#0"], title="y"),
    ]
    eids = [eid for g in groups for eid, _ in g.members]
    result, sink = await run(groups, evidence(eids), Asker())
    assert result.skipped == 1
    assert result.reduce_state == "done"
    assert len(result.groups) == 3
    (checkpoint,) = sink.checkpoints
    assert checkpoint.answer.skipped == 1
    assert len(checkpoint.answer.merges) == 2
    assert {"phase": "batch_merges_skipped", "level": 0, "batch": "b1", "count": 1} in sink.log


@pytest.mark.asyncio
async def test_invalid_answers_make_no_merge():
    groups, ev = singles(4, titles=["x"] * 4)

    def invalid(batch, level, batch_id):
        return cons.BatchAnswer(merges=(), skipped=0, outcome="invalid", requests=3)

    result, sink = await run(groups, ev, Asker(decide=invalid))
    assert len(result.groups) == 4
    assert result.reduce_state == "invalid"
    assert [c.answer.outcome for c in sink.checkpoints] == ["invalid"]


@pytest.mark.asyncio
async def test_adopted_batches_replay_without_ask():
    groups, ev = singles(16, titles=[f"t{i // 2}" for i in range(16)])
    first = Sink()
    result, _ = await run(groups, ev, Asker(), sink=first, capacity=2, max_levels=2)
    adopted = {(c.level, c.batch_id): c for c in first.checkpoints}

    ask = Asker()
    again, second = await run(groups, ev, ask, adopted=adopted, capacity=2, max_levels=2)
    assert ask.calls == []
    assert second.checkpoints == []
    assert again == result

    # A checkpoint for other input is not adopted: that batch is asked again.
    stale = dict(adopted)
    key = (0, "b2")
    stale[key] = cons.BatchCheckpoint(0, "b2", "0" * 64, adopted[key].answer)
    ask = Asker()
    again, _ = await run(groups, ev, ask, adopted=stale, capacity=2, max_levels=2)
    assert [(call[0], call[1]) for call in ask.calls] == [(0, "b2")]
    assert again == result


@pytest.mark.asyncio
async def test_adopted_half_replays_split_without_asking_parent():
    groups, ev = singles(4, titles=["x", "x", "y", "y"])

    def decide(batch, level, batch_id):
        if len(batch) > 2:
            return cons.BatchAnswer(merges=(), skipped=0, outcome="cut", requests=1)
        return None

    first = Sink()
    result, _ = await run(groups, ev, Asker(decide=decide), sink=first, max_levels=1)
    adopted = {(c.level, c.batch_id): c for c in first.checkpoints if c.batch_id == "b1.1"}
    ask = Asker(decide=decide)
    again, _ = await run(groups, ev, ask, adopted=adopted, max_levels=1)
    assert [call[1] for call in ask.calls] == ["b1.2"]
    assert again == result


@pytest.mark.asyncio
async def test_max_requests_stops_at_current_level():
    groups, ev = singles(16, titles=[f"t{i // 2}" for i in range(16)])
    budget = RequestBudget(cap=0)
    result, sink = await run(groups, ev, Asker(budget=budget), capacity=2)
    assert result.reduce_state == "skipped"
    assert len(result.groups) == 16
    assert sink.checkpoints == []
    assert any(e["phase"] == "max_requests_reached" for e in sink.log)

    budget = RequestBudget(cap=2)
    result, sink = await run(groups, ev, Asker(budget=budget), capacity=2, concurrency=1)
    assert result.reduce_state == "done"
    assert result.levels == 1
    assert len(sink.checkpoints) == 2
    # Only the finished batches' merges apply.
    assert len(result.groups) == 16 - 4


@pytest.mark.asyncio
async def test_provider_failure_propagates_after_finished_checkpoints():
    groups, ev = singles(8, titles=[f"t{i // 2}" for i in range(8)])

    class Boom(Exception):
        pass

    def decide(batch, level, batch_id):
        if batch_id == "b2":
            raise Boom
        return None

    sink = Sink()
    with pytest.raises(Boom):
        await run(groups, ev, Asker(decide=decide), sink=sink, capacity=2, concurrency=1)
    assert [c.batch_id for c in sink.checkpoints] == ["b1"]


@pytest.mark.asyncio
async def test_provider_failure_stops_queued_batches():
    """The first provider failure stops new batches.

    With one slot, b2 waits for b1. b1 yields and then fails; the slot it
    releases must not start b2 or b3 before the level is cancelled.
    """

    groups, ev = singles(8, titles=[f"t{i // 2}" for i in range(8)])

    def decide(batch, level, batch_id):
        if batch_id == "b1":
            raise RoleCallExhausted("provider_unavailable", 1, "http_503")
        return None

    ask = Asker(decide=decide, delays={(0, "b1"): 0.01})
    sink = Sink()
    with pytest.raises(RoleCallExhausted):
        await run(groups, ev, ask, sink=sink, capacity=1, concurrency=1)
    assert [call[1] for call in ask.calls] == ["b1"]
    assert sink.checkpoints == []


def test_batch_body_round_trip():
    header = ModelGroup("r0", "T", "F", "E", "Q", (), (), (), "P", ())
    checkpoint = cons.BatchCheckpoint(
        2,
        "b3.1",
        "d" * 64,
        cons.BatchAnswer(((("g1", "g4"), header), (("g2", "g3"), None)), 1, None, 2),
    )
    body = cons.batch_body(checkpoint)
    assert json.loads(json.dumps(body)) == body
    assert cons.batch_from_body(body) == checkpoint
    failed = cons.BatchCheckpoint(0, "b1", "e" * 64, cons.BatchAnswer((), 0, "invalid", 3))
    assert cons.batch_from_body(cons.batch_body(failed)) == failed
    assert cons.batch_record_size(checkpoint) == len(gp._json_text(body).encode("utf-8"))
