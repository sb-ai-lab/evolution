"""Family 13's focus visitation across a `retained` round (#148; pre-F8 design
§3.1-§3.3, §3.5; ADR-0012 amendment of 2026-10-06).

Pure unit tests of the chain walk `adaptive_portion` delegates to. Rounds and
snapshots are `SimpleNamespace` stand-ins for `RoundState`/`CampaignSnapshot`
with only the fields the walk reads.
"""

from __future__ import annotations

from types import SimpleNamespace

from evolution.tree.campaign import (
    carried_cohort_ids,
    visitation_chain_start,
    visitation_in_cycle,
)
from evolution.tree.roles.grouping.reflect_grouping import Cohort, build_registry, select_portion
from tests.tree.roles.grouping.test_reflect_grouping import record


def _round(binding, cycle, cohorts=(), cases=(), outcome="stalled", policy="p"):
    return SimpleNamespace(
        focus_portion={
            "baseline_binding": binding,
            "policy_digest": policy,
            "cycle": cycle,
            "cohort_ids": list(cohorts),
            "case_ids": list(cases),
        },
        closed={"outcome": outcome, "reason": "x"},
    )


def test_retained_carries_its_cases_to_the_new_binding():
    rounds = {1: _round("B1", 1, ["k1"], ["c1"], outcome="retained")}
    assert visitation_in_cycle(rounds, 2, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset(),
        frozenset({"c1"}),
        1,
    )


def test_a_same_binding_round_contributes_cohorts_and_an_older_one_cases():
    rounds = {
        1: _round("B1", 1, ["k1"], ["c1"], outcome="retained"),
        2: _round("B2", 1, ["k2"], ["c2"]),
    }
    assert visitation_in_cycle(rounds, 3, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset({"k2"}),
        frozenset({"c1"}),
        1,
    )


def test_a_stalled_round_after_a_retained_one_keeps_its_own_cohorts():
    """Spec §4 Part B: round 3 stalled on B2 after round 2's `retained` refresh.
    Its own cohorts are visited on B2; rounds 1-2 (on B1) carry only their cases."""

    rounds = {
        1: _round("B1", 1, ["k1"], ["c1"]),
        2: _round("B1", 1, ["k2"], ["c2"], outcome="retained"),
        3: _round("B2", 1, ["k3"], ["c3"]),
    }
    assert visitation_in_cycle(rounds, 4, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset({"k3"}),
        frozenset({"c1", "c2"}),
        1,
    )


def test_a_higher_cycle_clears_both_sets():
    rounds = {
        1: _round("B1", 1, ["k1"], ["c1"], outcome="retained"),
        2: _round("B2", 2, ["k2"], ["c2"], outcome="retained"),
    }
    assert visitation_in_cycle(rounds, 3, binding="B3", policy_digest="p", chain_start=1) == (
        frozenset(),
        frozenset({"c2"}),
        2,
    )


def test_a_placeholder_portion_never_raises_or_adds():
    rounds = {
        1: _round("B1", 2, ["k1"], ["c1"], outcome="retained"),
        2: _round("B2", 1, [], [], outcome="stalled"),
    }
    assert visitation_in_cycle(rounds, 3, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset(),
        frozenset({"c1"}),
        2,
    )


def test_the_chain_starts_after_a_promotion():
    rounds = {
        1: _round("B1", 1, ["k1"], ["c1"], outcome="retained"),
        2: _round("B2", 1, ["k2"], ["c2"], outcome="promoted"),
    }
    assert visitation_in_cycle(rounds, 3, binding="B3", policy_digest="p", chain_start=3) == (
        frozenset(),
        frozenset(),
        1,
    )


def test_a_round_that_does_not_advance_contributes_nothing():
    rounds = {1: _round("B1", 1, ["k1"], ["c1"], outcome="condition_changed")}
    assert visitation_in_cycle(rounds, 2, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset(),
        frozenset(),
        1,
    )


def test_another_policy_digest_contributes_nothing():
    rounds = {1: _round("B1", 1, ["k1"], ["c1"], outcome="retained", policy="old")}
    assert visitation_in_cycle(rounds, 2, binding="B2", policy_digest="p", chain_start=1) == (
        frozenset(),
        frozenset(),
        1,
    )


def _snapshot(*, first_round=1, rebaseline_from_round=None, closed=None):
    return SimpleNamespace(
        first_round=first_round,
        rebaseline_from_round=rebaseline_from_round,
        rounds={
            number: SimpleNamespace(
                closed=None if outcome is None else {"outcome": outcome, "reason": "x"}
            )
            for number, outcome in (closed or {}).items()
        },
    )


def test_visitation_chain_start():
    retained = {1: "retained", 2: "promoted", 3: "retained", 4: "condition_changed"}
    # A promotion at round 2 starts the chain at round 3.
    assert visitation_chain_start(_snapshot(closed={1: "retained", 2: "promoted"}), 4) == 3
    # A promotion at round 3 starts the chain at round 4: the round after a
    # promotion begins at cycle 1 (scan #25).
    assert visitation_chain_start(_snapshot(closed={1: "retained", 3: "promoted"}), 4) == 4
    # A promotion at or after the round itself is not before it.
    assert visitation_chain_start(_snapshot(closed={1: "retained", 3: "promoted"}), 3) == 1
    # A rebaseline from round 4 starts the chain at round 4.
    assert visitation_chain_start(_snapshot(rebaseline_from_round=4, closed={1: "stalled"}), 5) == 4
    # Both: the later one.
    assert visitation_chain_start(_snapshot(rebaseline_from_round=4, closed=retained), 5) == 4
    assert visitation_chain_start(_snapshot(rebaseline_from_round=2, closed=retained), 5) == 3
    # Neither: the campaign's first round (a linked successor's, too).
    assert visitation_chain_start(_snapshot(closed={1: "retained", 2: "stalled"}), 3) == 1
    assert visitation_chain_start(_snapshot(first_round=4, closed={4: "retained"}), 5) == 4
    # A rebaseline above the round is ignored.
    assert visitation_chain_start(_snapshot(rebaseline_from_round=6, closed={1: "stalled"}), 5) == 1
    # An open round has no close and is not a promotion.
    assert visitation_chain_start(_snapshot(closed={1: "stalled", 2: None}), 3) == 1


def _cohort(cohort_id, group_id, case_id, *indices):
    return Cohort(
        cohort_id=cohort_id,
        group_id=group_id,
        case_id=case_id,
        member_indices=tuple(indices),
        member_ids=tuple(f"obs:{case_id}:{index}" for index in indices),
        group_key=group_id,
    )


def test_carried_cohort_ids_marks_every_group_of_a_carried_case():
    """Spec §4 Part B: a carried case regrouped into another group counts as
    visited, in every group it lands in (scan #13)."""

    cohorts = (
        _cohort("k-a-g1", "g1", "a", 0),
        _cohort("k-a-g2", "g2", "a", 1),
        _cohort("k-b-g1", "g1", "b", 2),
        _cohort("k-c-g2", "g2", "c", 3),
    )
    # Both groups of the carried case `a`; `b` and `c` are not carried.
    assert carried_cohort_ids(cohorts, {"a"}) == frozenset({"k-a-g1", "k-a-g2"})
    # A carried case with no cohort on this baseline marks nothing.
    assert carried_cohort_ids(cohorts, {"z"}) == frozenset()
    assert carried_cohort_ids(cohorts, {"a", "z"}) == frozenset({"k-a-g1", "k-a-g2"})
    assert carried_cohort_ids(cohorts, ()) == frozenset()


def test_a_carried_case_without_a_cohort_never_blocks_a_reset():
    """Review Focus 5: `c1` is carried but has no cohort on this baseline, so it
    marks nothing, and `select_portion` neither resets nor skips c2/c3."""

    registry = build_registry(
        "base",
        [
            record("c2-f", "c2", 1, failing=True, observation={"terminal": "failed"}),
            record("c3-f", "c3", 1, failing=True, observation={"terminal": "failed"}),
        ],
    )
    cohorts = (_cohort("k-c2", "g1", "c2", 0), _cohort("k-c3", "g2", "c3", 1))
    visited = carried_cohort_ids(cohorts, {"c1"})
    assert visited == frozenset()
    selection = select_portion(cohorts, registry=registry, visited_in_cycle=visited, focus_cases=1)
    assert selection.cycle_reset is False
    assert [cohort.case_id for cohort in selection.cohorts] in (["c2"], ["c3"])
