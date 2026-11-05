"""G2 spec §3.4: the gate comparison on an explicit, named case set."""

from __future__ import annotations

from fractions import Fraction

import pytest

from evolution.tree.campaign_statistics import (
    CaseSetComparison,
    CoverageMissing,
    MissingMetric,
    compare_on_cases,
    exclude_pairs,
    score_samples_of,
)
from evolution.tree.statistics import GatePlan, gate_at_look
from tests.campaign_fakes import PRIMARY, score_rows

GROUPS = {"a": ("g1", "s1"), "b": ("g1", "s1"), "c": ("g2", "s2")}


def _plan(seed_mode: str = "exact") -> GatePlan:
    return GatePlan(
        seed_mode=seed_mode,  # type: ignore[arg-type]
        random_seed=17,
        alpha_spend={2: Fraction("0.05")},
        alpha_budget=Fraction("0.05"),
        draws=200,
    )


def _kernel(parent, candidate, *, seed_mode="exact", excluded_repeats=False):
    return gate_at_look(
        score_samples_of(parent, GROUPS, PRIMARY, 2),
        score_samples_of(candidate, GROUPS, PRIMARY, 2),
        _plan(seed_mode),
        2,
        excluded_repeats=excluded_repeats,
    )


def _only(rows, cases):
    return tuple(row for row in rows if row.case_id in cases)


@pytest.mark.parametrize("seed_mode", ["exact", "unpaired", "none"])
def test_the_whole_case_set_is_the_existing_kernel_bit_for_bit(seed_mode):
    parent = score_rows({"a": (0.0, 0.5), "b": (0.5, 0.5), "c": (1.0, 0.5)}, seed_mode=seed_mode)
    candidate = score_rows({"a": (1.0, 1.0), "b": (0.5, 1.0), "c": (1.0, 0.5)}, seed_mode=seed_mode)
    compared = compare_on_cases(
        parent,
        candidate,
        _plan(seed_mode),
        2,
        cases=("a", "b", "c"),
        groups=GROUPS,
        primary=PRIMARY,
    )
    assert compared == CaseSetComparison(
        _kernel(parent, candidate, seed_mode=seed_mode), ("a", "b", "c"), ()
    )
    # An independent local stream: a second call draws the same interval.
    assert compared == compare_on_cases(
        parent,
        candidate,
        _plan(seed_mode),
        2,
        cases=("a", "b", "c"),
        groups=GROUPS,
        primary=PRIMARY,
    )


def test_only_the_named_cases_enter_and_nothing_else_needs_coverage():
    # `b` is unmeasured on the Candidate side and `a` has a repeat beyond the look.
    parent = score_rows({"a": (0.0, 0.5, 1.0), "b": (0.5, None), "c": (1.0, 0.5)})
    candidate = score_rows({"a": (1.0, 1.0), "c": (1.0, 0.5)})
    compared = compare_on_cases(
        parent, candidate, _plan(), 2, cases=("c", "a"), groups=GROUPS, primary=PRIMARY
    )
    assert compared.cases == ("c", "a") and compared.dropped == ()
    assert compared.result == _kernel(_only(parent, {"a", "c"}), _only(candidate, {"a", "c"}))


def test_a_named_pair_without_a_scored_row_on_either_side_is_refused():
    parent = score_rows({"a": (0.0, 0.5), "c": (1.0, None)})
    candidate = score_rows({"a": (1.0,), "c": (1.0, 0.5)})
    with pytest.raises(CoverageMissing) as refused:
        compare_on_cases(
            parent, candidate, _plan(), 2, cases=("a", "c"), groups=GROUPS, primary=PRIMARY
        )
    assert not isinstance(refused.value, ValueError)
    assert refused.value.missing == (("candidate", "a", 2), ("parent", "c", 2))


def test_excluded_pairs_are_not_owed_and_a_wholly_excluded_case_drops_out():
    parent = score_rows({"a": (0.0, None), "b": (None, None), "c": (1.0, 0.5)})
    candidate = score_rows({"a": (1.0, 1.0), "b": (0.5, 0.5), "c": (1.0, 0.5)})
    excluded = frozenset({("a", 2), ("b", 1), ("b", 2)})
    compared = compare_on_cases(
        parent,
        candidate,
        _plan(),
        2,
        cases=("a", "b", "c"),
        groups=GROUPS,
        primary=PRIMARY,
        excluded=excluded,
    )
    assert compared.cases == ("a", "c") and compared.dropped == ("b",)
    kept_parent = _only(exclude_pairs(parent, excluded), {"a", "c"})
    kept_candidate = _only(exclude_pairs(candidate, excluded), {"a", "c"})
    assert compared.result == _kernel(kept_parent, kept_candidate, excluded_repeats=True)


def test_a_case_set_whose_every_pair_is_excluded_has_no_result():
    parent = score_rows({"b": (None, None)})
    candidate = score_rows({"b": (0.5, 0.5)})
    compared = compare_on_cases(
        parent,
        candidate,
        _plan(),
        2,
        cases=("b",),
        groups=GROUPS,
        primary=PRIMARY,
        excluded=frozenset({("b", 1), ("b", 2)}),
    )
    assert compared == CaseSetComparison(None, (), ("b",))


@pytest.mark.parametrize("cases", [(), ("a", "a"), ("a", "zzz")])
def test_a_case_set_names_distinct_corpus_cases(cases):
    rows = score_rows({"a": (0.5, 0.5)})
    with pytest.raises(ValueError, match="case set") as refused:
        compare_on_cases(rows, rows, _plan(), 2, cases=cases, groups=GROUPS, primary=PRIMARY)
    assert not isinstance(refused.value, CoverageMissing)


def test_an_undeclared_look_is_refused_even_when_every_case_drops_out():
    rows = score_rows({"b": (None, None)})
    with pytest.raises(ValueError, match="look"):
        compare_on_cases(
            rows,
            rows,
            _plan(),
            1,
            cases=("b",),
            groups=GROUPS,
            primary=PRIMARY,
            excluded=frozenset({("b", 1)}),
        )


def test_a_scored_row_without_the_primary_metric_is_named():
    parent = score_rows({"a": (0.0, 0.5)})
    candidate = score_rows({"a": (1.0, 1.0)}, primary="other")
    assert compare_on_cases(
        parent, candidate, _plan(), 2, cases=("a",), groups=GROUPS, primary=PRIMARY
    ) == MissingMetric("a", "01")


def test_the_exact_pairing_proof_still_applies():
    parent = score_rows({"a": (0.0, 0.5)})
    candidate = score_rows({"a": (1.0, 1.0)}, dependency_digest="other-deps")
    with pytest.raises(ValueError) as refused:
        compare_on_cases(
            parent, candidate, _plan(), 2, cases=("a",), groups=GROUPS, primary=PRIMARY
        )
    assert not isinstance(refused.value, CoverageMissing)
