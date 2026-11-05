"""Task 4: v2 statistics adaptation, evidence, looks, selector, thresholds."""

from __future__ import annotations

import math
import random
from fractions import Fraction
from typing import Any

import pytest

from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_schema import CampaignSnapshot, RoundState
from evolution.tree.campaign_statistics import (
    MissingMetric,
    SampleRow,
    arbitrate_owner,
    case_groups,
    critical_vetoed,
    evaluate_look,
    exclude_pairs,
    look_evaluable,
    score_samples_of,
    thresholds_reached,
)
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.statistics import GatePlan, ScoreSample, gate_at_look, group_weighted_score
from tests.test_tree_campaign_identity import policy_payload


def _policy(**overrides: object) -> CampaignPolicyV11:
    payload = policy_payload(editor_context=None)
    payload.update(overrides)
    return CampaignPolicyV11.from_payload(payload)


def _snapshot(
    *,
    rounds: dict[int, RoundState],
    counts: dict[str, int] | None = None,
    denominator: int = 0,
    window_base_counts: dict[str, int] | None = None,
    window_base_denominator: int = 0,
) -> CampaignSnapshot:
    return CampaignSnapshot(
        family_version=14,
        identity={},
        run_id="run-1",
        alpha_plan={},
        rounds=rounds,
        champion=("v0", "0" * 64),
        champion_dependency_digest="deps",
        measurements={},
        accounted=frozenset(),
        counts=counts or {},
        denominator=denominator,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=1,
        window_base_counts=window_base_counts or {},
        window_base_denominator=window_base_denominator,
    )


def _case(case_id: str, group_id: str = "group", stratum: str = "stratum") -> EvalCase:
    return EvalCase(case_id, group_id, stratum, (), {}, {}, {})


def _row(
    case_id: str,
    repeat: int,
    *,
    sample_id: str | None = None,
    disposition: str = "score",
    metrics: dict[str, float] | None = None,
    critical_pass: bool | None = None,
    requested_seed: int | None = None,
    effective_seed: int | None = None,
    dependency_digest: str = "deps",
    boot_id: str = "boot",
) -> SampleRow:
    return SampleRow(
        case_id=case_id,
        sample_id=sample_id if sample_id is not None else f"{case_id}-{repeat}",
        repeat=repeat,
        requested_seed=requested_seed,
        effective_seed=effective_seed,
        boot_id=boot_id,
        dependency_digest=dependency_digest,
        outcome="completed",
        disposition=disposition,
        metrics=metrics,
        critical_pass=critical_pass,
    )


# -- select ---------------------------------------------------------------


# -- thresholds_reached / arbitrate_owner ----------------------------------


def test_thresholds_reached_crosses_count_over_max_count() -> None:
    policy = _policy(
        indeterminate={
            o: {"max_count": 2, "max_rate": "0.99"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(rounds={}, counts={"target": 3}, denominator=100)

    crossed = thresholds_reached(snapshot, policy)

    assert crossed == (
        {
            "owner": "target",
            "kind": "count",
            "count": 3,
            "denominator": 100,
            "max_count": 2,
            "max_rate": "0.990000000000",
        },
    )


def test_thresholds_reached_crosses_rate_exactly() -> None:
    policy = _policy(
        indeterminate={
            o: {"max_count": 100, "max_rate": "0.07"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(rounds={}, counts={"scorer": 1}, denominator=13)
    assert Fraction(1, 13) > Fraction("0.07")

    crossed = thresholds_reached(snapshot, policy)

    assert len(crossed) == 1
    assert crossed[0]["owner"] == "scorer"
    assert crossed[0]["kind"] == "rate"


def test_thresholds_reached_reports_count_when_both_cross() -> None:
    policy = _policy(
        indeterminate={
            o: {"max_count": 1, "max_rate": "0.01"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(rounds={}, counts={"rollout": 5}, denominator=10)

    crossed = thresholds_reached(snapshot, policy)

    assert crossed == (
        {
            "owner": "rollout",
            "kind": "count",
            "count": 5,
            "denominator": 10,
            "max_count": 1,
            "max_rate": "0.010000000000",
        },
    )


def test_thresholds_reached_zero_denominator_never_crosses_rate() -> None:
    policy = _policy(
        indeterminate={
            o: {"max_count": 100, "max_rate": "0.00"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(rounds={}, counts={"target": 0}, denominator=0)

    assert thresholds_reached(snapshot, policy) == ()


def test_thresholds_reached_is_in_the_fixed_owner_order() -> None:
    policy = _policy(
        indeterminate={
            o: {"max_count": 0, "max_rate": "0.99"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(
        rounds={},
        counts={"rollout": 1, "target": 1, "scorer": 1, "environment": 1},
        denominator=100,
    )

    crossed = thresholds_reached(snapshot, policy)

    assert [row["owner"] for row in crossed] == ["target", "environment", "scorer", "rollout"]


def test_thresholds_reached_crosses_over_the_window_not_the_totals() -> None:
    """Spec §5.3: three target-owned indeterminate tries cross `max_count: 2`.

    After a suspension the same three tries are no longer counted -- the
    window base absorbs them -- and a fourth try (one new try in the new
    window) still does not cross.
    """

    policy = _policy(
        indeterminate={
            o: {"max_count": 2, "max_rate": "0.99"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )

    # Before any suspension (family <=6, or family 7 with an empty window):
    # three tries cross, same as the totals.
    before = _snapshot(rounds={}, counts={"target": 3}, denominator=20)
    assert [row["owner"] for row in thresholds_reached(before, policy)] == ["target"]

    # After a suspension whose window base absorbed those same three tries,
    # the totals still say 3 but the window says 0: no crossing.
    after_suspension = _snapshot(
        rounds={},
        counts={"target": 3},
        denominator=20,
        window_base_counts={"target": 3},
        window_base_denominator=20,
    )
    assert thresholds_reached(after_suspension, policy) == ()

    # A fourth try -- the first of the new window -- still does not cross:
    # the window count is 1, not 4.
    fourth_try = _snapshot(
        rounds={},
        counts={"target": 4},
        denominator=25,
        window_base_counts={"target": 3},
        window_base_denominator=20,
    )
    assert thresholds_reached(fourth_try, policy) == ()


def test_thresholds_reached_crosses_again_once_the_window_itself_reaches_it() -> None:
    """Three more tries in the new window cross again, over the window's own
    count and denominator, not the campaign's totals (which would be 6)."""

    policy = _policy(
        indeterminate={
            o: {"max_count": 2, "max_rate": "0.99"}
            for o in ("target", "environment", "scorer", "rollout")
        }
    )
    snapshot = _snapshot(
        rounds={},
        counts={"target": 6},
        denominator=6,
        window_base_counts={"target": 3},
        window_base_denominator=3,
    )

    crossed = thresholds_reached(snapshot, policy)

    assert crossed == (
        {
            "owner": "target",
            "kind": "count",
            "count": 3,
            "denominator": 3,
            "max_count": 2,
            "max_rate": "0.990000000000",
        },
    )


def test_arbitrate_owner_returns_the_first_in_fixed_order() -> None:
    assert arbitrate_owner(["rollout", "target", "scorer"]) == "target"
    assert arbitrate_owner(["rollout", "scorer"]) == "scorer"


def test_arbitrate_owner_refuses_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="crossed owner"):
        arbitrate_owner([])


# -- score_samples_of / look_evaluable / evaluate_look / critical_vetoed --


def test_case_groups_from_cases_and_from_corpus_cases() -> None:
    cases = [EvalCase("a", "g1", "s1", (), {}, {}, {}), EvalCase("b", "g2", "s2", (), {}, {}, {})]
    expected = {"a": ("g1", "s1"), "b": ("g2", "s2")}

    assert case_groups(cases) == expected
    assert (
        case_groups(
            [
                {"case_id": "a", "group_id": "g1", "stratum": "s1"},
                {"case_id": "b", "group_id": "g2", "stratum": "s2"},
            ]
        )
        == expected
    )


def test_score_samples_of_accepts_the_mapping() -> None:
    corpus = [_case("case-a", group_id="g", stratum="s")]
    rows = [_row("case-a", 1, metrics={"score": 0.5}, requested_seed=1, effective_seed=1)]

    assert score_samples_of(rows, case_groups(corpus), "score", 3) == score_samples_of(
        rows, corpus, "score", 3
    )


def test_score_samples_of_returns_missing_metric_when_primary_is_absent() -> None:
    corpus = [_case("case-a")]
    rows = [
        _row("case-a", 1, metrics={"score": 1.0}),
        _row("case-a", 2, metrics={"other": 1.0}, sample_id="case-a-missing"),
    ]

    result = score_samples_of(rows, corpus, "score", 2)

    assert result == MissingMetric("case-a", "case-a-missing")


def test_score_samples_of_returns_missing_metric_at_an_interior_repeat() -> None:
    corpus = [_case("case-a")]
    rows = [
        _row("case-a", 1, metrics={"score": 0.1}, sample_id="s1"),
        _row("case-a", 2, metrics={"other": 1.0}, sample_id="s2-missing"),
        _row("case-a", 3, metrics={"score": 0.3}, sample_id="s3"),
    ]

    result = score_samples_of(rows, corpus, "score", 3)

    assert result == MissingMetric("case-a", "s2-missing")


def test_score_samples_of_builds_score_samples_with_repeat_as_sample_id() -> None:
    corpus = [_case("case-a", group_id="g", stratum="s")]
    rows = [_row("case-a", 1, metrics={"score": 0.5}, requested_seed=1, effective_seed=1)]

    result = score_samples_of(rows, corpus, "score", 3)

    assert result == (
        ScoreSample(
            case_id="case-a",
            sample_id="1",
            score=0.5,
            requested_seed=1,
            effective_seed=1,
            boot_id="boot",
            dependency_digest="deps",
            group_id="g",
            stratum="s",
        ),
    )


def test_score_samples_of_ignores_non_score_dispositions_and_later_repeats() -> None:
    corpus = [_case("case-a")]
    rows = [
        _row("case-a", 1, metrics={"score": 0.1}),
        _row("case-a", 2, disposition="indeterminate", metrics=None),
        _row("case-a", 5, metrics={"score": 0.9}),
    ]

    result = score_samples_of(rows, corpus, "score", 1)

    assert result == (
        ScoreSample(
            case_id="case-a",
            sample_id="1",
            score=0.1,
            requested_seed=None,
            effective_seed=None,
            boot_id="boot",
            dependency_digest="deps",
            group_id="group",
            stratum="stratum",
        ),
    )


def test_look_evaluable_true_when_both_sides_score_every_repeat() -> None:
    baseline = [_row("case-a", 1), _row("case-a", 2)]
    attempt = [_row("case-a", 1), _row("case-a", 2)]

    ok, missing = look_evaluable(baseline, attempt, 2)

    assert ok is True
    assert missing == ()


def test_look_evaluable_names_missing_rows() -> None:
    baseline = [_row("case-a", 1)]
    attempt = [_row("case-a", 1, disposition="indeterminate", metrics=None, sample_id="att-1")]

    ok, missing = look_evaluable(baseline, attempt, 2)

    assert ok is False
    assert {"case_id": "case-a", "sample_id": "2", "state": "not_started"} in missing
    assert {"case_id": "case-a", "sample_id": "att-1", "state": "indeterminate"} in missing


def test_look_evaluable_ignores_excluded_pairs() -> None:
    base = [
        _row("c1", 1, sample_id="01"),
        _row("c2", 1, sample_id="01", disposition="indeterminate"),
    ]
    cand = [_row("c1", 1, sample_id="01"), _row("c2", 1, sample_id="01")]

    assert look_evaluable(base, cand, 1) == (
        False,
        ({"case_id": "c2", "sample_id": "01", "state": "indeterminate"},),
    )
    assert look_evaluable(base, cand, 1, excluded=frozenset({("c2", 1)})) == (True, ())


def test_look_evaluable_still_names_a_pair_that_is_not_excluded() -> None:
    base = [
        _row("c1", 1, sample_id="01", disposition="indeterminate"),
        _row("c2", 1, sample_id="01", disposition="indeterminate"),
    ]
    cand = [_row("c1", 1, sample_id="01"), _row("c2", 1, sample_id="01")]

    assert look_evaluable(base, cand, 1, excluded=frozenset({("c2", 1)})) == (
        False,
        ({"case_id": "c1", "sample_id": "01", "state": "indeterminate"},),
    )


def test_look_evaluable_skips_an_excluded_pair_absent_from_one_side() -> None:
    base = [_row("c1", 1, sample_id="01"), _row("c1", 2, sample_id="02")]
    cand = [_row("c1", 1, sample_id="01")]

    assert look_evaluable(base, cand, 2, excluded=frozenset({("c1", 2)})) == (True, ())


def test_exclude_pairs_drops_both_sides_rows() -> None:
    rows = [_row("c1", 1, sample_id="01"), _row("c2", 1, sample_id="01")]

    assert [r.case_id for r in exclude_pairs(rows, frozenset({("c2", 1)}))] == ["c1"]
    assert exclude_pairs(rows, frozenset()) == tuple(rows)


def test_exclusion_is_keyed_by_case_and_repeat_so_unpaired_sides_lose_the_same_pair() -> None:
    """Ruling R13: under `unpaired`/`none` the two sides' sample ids differ
    (`champion-NN` / `candidate-NN`), so an exclusion keyed by sample id would
    leave the Candidate's pair in; `(case_id, repeat)` removes it from both."""

    base = [
        _row("c1", 1, sample_id="champion-01"),
        _row("c1", 2, sample_id="champion-02", disposition="indeterminate"),
    ]
    cand = [
        _row("c1", 1, sample_id="candidate-01"),
        _row("c1", 2, sample_id="candidate-02"),
    ]
    excluded = frozenset({("c1", 2)})

    assert [r.sample_id for r in exclude_pairs(base, excluded)] == ["champion-01"]
    assert [r.sample_id for r in exclude_pairs(cand, excluded)] == ["candidate-01"]
    assert look_evaluable(base, cand, 2, excluded=excluded) == (True, ())


def test_latest_rows_of_an_empty_prefix_is_empty() -> None:
    from evolution.tree.campaign_statistics import (
        MeasurementTries,
        latest_rows,
        measurement_tries,
    )
    from evolution.tree.execution.journal_schema import Lifecycle

    assert latest_rows((), Lifecycle()) == ()
    assert measurement_tries(()) == MeasurementTries(tries=0, exhausted_repeats=())


def test_evaluate_look_refuses_an_undeclared_look() -> None:
    plan = GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200)
    with pytest.raises(ValueError, match="no caller-supplied alpha spend"):
        evaluate_look([], [], plan, 6)


def test_evaluate_look_matches_gate_at_look() -> None:
    parent = [
        ScoreSample("case", str(i), 0.0, i, i, "boot", "deps", "group", "stratum")
        for i in range(10)
    ]
    candidate = [
        ScoreSample("case", str(i), 1.0, i, i, "boot", "deps", "group", "stratum")
        for i in range(10)
    ]
    plan = GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200)

    assert evaluate_look(parent, candidate, plan, 10).decision == "promote"


def test_critical_vetoed_is_true_only_on_an_explicit_false() -> None:
    assert critical_vetoed([_row("case-a", 1, critical_pass=False)]) is True
    assert critical_vetoed([_row("case-a", 1, critical_pass=True)]) is False
    assert critical_vetoed([_row("case-a", 1, critical_pass=None)]) is False
    assert critical_vetoed([]) is False


# -- exact-rational gate arithmetic (Task 1) -------------------------------


def _samples(side: str, scores: dict[str, list[float]], group: dict[str, str]) -> list[ScoreSample]:
    out = []
    for case_id, values in scores.items():
        for repeat, score in enumerate(values, start=1):
            out.append(
                ScoreSample(
                    case_id=case_id,
                    sample_id=str(repeat),
                    score=score,
                    requested_seed=repeat,
                    effective_seed=repeat,
                    boot_id=f"boot-{side}",
                    dependency_digest="d" * 64,
                    group_id=group[case_id],
                    stratum="s",
                )
            )
    return out


GROUPS = {"a": "g1", "b": "g1", "c": "g2"}
PARENT = {"a": [0.1, 0.2, 0.3], "b": [0.3, 0.3, 0.1], "c": [0.7, 0.1, 0.2]}
CHILD = {"a": [0.2, 0.2, 0.3], "b": [0.3, 0.4, 0.1], "c": [0.7, 0.3, 0.2]}
PLAN = GatePlan(
    seed_mode="exact",
    random_seed=12345,
    alpha_spend={3: Fraction("0.01"), 6: Fraction("0.015"), 10: Fraction("0.025")},
    alpha_budget=Fraction("0.05"),
    draws=200,
)


def _draw(randomizer: random.Random, n: int) -> int:
    return (int(randomizer.random() * 9007199254740992.0) * n) >> 53


def _reference_exact(
    parent: dict, child: dict, group: dict, seed: int, draws: int, alpha: Fraction
):
    """An independent all-`Fraction` bootstrap: the spec §5 semantics, written plainly."""

    deltas: dict[str, dict[str, list[Fraction]]] = {}
    for case_id in parent:
        deltas.setdefault(group[case_id], {})[case_id] = [
            Fraction(c) - Fraction(p) for p, c in zip(parent[case_id], child[case_id], strict=True)
        ]
    randomizer = random.Random(seed)
    group_ids = tuple(deltas)
    samples = []
    for _ in range(draws):
        group_means = []
        for _ in group_ids:
            cases = deltas[group_ids[_draw(randomizer, len(group_ids))]]
            case_ids = tuple(cases)
            case_means = []
            for _ in case_ids:
                values = cases[case_ids[_draw(randomizer, len(case_ids))]]
                picked = [values[_draw(randomizer, len(values))] for _ in values]
                case_means.append(sum(picked, Fraction(0)) / len(values))
            group_means.append(sum(case_means, Fraction(0)) / len(case_means))
        samples.append(sum(group_means, Fraction(0)) / len(group_means))
    samples.sort()
    lower = samples[math.floor(alpha / 2 * draws)]
    upper = samples[math.ceil((1 - alpha / 2) * draws) - 1]
    return lower, upper


def test_gate_endpoints_are_the_exact_rationals_rounded_once() -> None:
    """Spec §5: no float summation anywhere; `lower`/`upper` are `float(endpoint)`."""

    parent = _samples("parent", PARENT, GROUPS)
    child = _samples("child", CHILD, GROUPS)
    result = gate_at_look(parent, child, PLAN, 3)
    lower, upper = _reference_exact(PARENT, CHILD, GROUPS, 12345 + 3, 200, Fraction("0.01"))
    assert (result.lower, result.upper) == (float(lower), float(upper))
    expected = "promote" if lower > 0 else "reject" if upper <= 0 else "continue"
    assert result.decision == expected


def test_gate_golden_values() -> None:
    """The exact-replay canary: pinned once, compared exactly, on every supported interpreter."""

    parent = _samples("parent", PARENT, GROUPS)
    child = _samples("child", CHILD, GROUPS)
    result = gate_at_look(parent, child, PLAN, 3)
    # Paste the three values the implementation prints the first time this
    # test runs (`print(result.decision, result.lower.hex(), result.upper.hex())`)
    # and never edit them again: a change here is a change of the contract.
    assert result.decision == "continue"
    assert result.lower.hex() == "0x0.0p+0"
    assert result.upper.hex() == "0x1.1111111111111p-3"


GROUPS_UNPAIRED = {"a": "g1", "b": "g1", "c": "g2"}
# Different dyadic families on each side (parent: eighths, child: tenths, whose
# nearest `float` has a much wider power-of-two denominator) so the golden
# test also stresses `_scaled`'s shared-exponent merge across `c:`/`p:`
# prefixed groups, not just a single-denominator case.
PARENT_UNPAIRED = {
    "a": [0.125, 0.375, 0.625],
    "b": [0.875, 0.125, 0.375],
    "c": [0.625, 0.875, 0.125],
}
CHILD_UNPAIRED = {"a": [0.2, 0.4, 0.6], "b": [0.8, 0.1, 0.3], "c": [0.7, 0.9, 0.1]}
PLAN_UNPAIRED = GatePlan(
    seed_mode="unpaired",
    random_seed=54321,
    alpha_spend={3: Fraction("0.02")},
    alpha_budget=Fraction("0.05"),
    draws=200,
)


def _reference_exact_unpaired(
    parent: dict, child: dict, group: dict, seed: int, draws: int, alpha: Fraction
):
    """An independent all-`Fraction` unpaired bootstrap: group -> case ->
    candidate's `len(values)` picks -> parent's `len(values)` picks, case mean
    = candidate mean - parent mean (spec §5, written plainly)."""

    candidate: dict[str, dict[str, list[Fraction]]] = {}
    parent_values: dict[str, dict[str, list[Fraction]]] = {}
    for case_id in child:
        group_id = group[case_id]
        candidate.setdefault(group_id, {})[case_id] = [Fraction(v) for v in child[case_id]]
        parent_values.setdefault(group_id, {})[case_id] = [Fraction(v) for v in parent[case_id]]
    randomizer = random.Random(seed)
    group_ids = tuple(candidate)
    samples = []
    for _ in range(draws):
        group_means = []
        for _ in group_ids:
            group_id = group_ids[_draw(randomizer, len(group_ids))]
            case_ids = tuple(candidate[group_id])
            case_means = []
            for _ in case_ids:
                case_id = case_ids[_draw(randomizer, len(case_ids))]
                candidate_values = candidate[group_id][case_id]
                candidate_picked = [
                    candidate_values[_draw(randomizer, len(candidate_values))]
                    for _ in candidate_values
                ]
                parent_case_values = parent_values[group_id][case_id]
                parent_picked = [
                    parent_case_values[_draw(randomizer, len(parent_case_values))]
                    for _ in parent_case_values
                ]
                candidate_mean = sum(candidate_picked, Fraction(0)) / len(candidate_values)
                parent_mean = sum(parent_picked, Fraction(0)) / len(parent_case_values)
                case_means.append(candidate_mean - parent_mean)
            group_means.append(sum(case_means, Fraction(0)) / len(case_means))
        samples.append(sum(group_means, Fraction(0)) / len(group_means))
    samples.sort()
    lower = samples[math.floor(alpha / 2 * draws)]
    upper = samples[math.ceil((1 - alpha / 2) * draws) - 1]
    return lower, upper


def test_unpaired_gate_endpoints_are_the_exact_rationals_rounded_once() -> None:
    """Unpaired analogue of the exact-mode test: stresses the `c:`/`p:`-prefixed
    shared-exponent merge in `_scaled` and the parent-subtraction draw order in
    `_bootstrap_groups`, which the exact-mode-only tests above never exercise."""

    parent = _samples("parent", PARENT_UNPAIRED, GROUPS_UNPAIRED)
    child = _samples("child", CHILD_UNPAIRED, GROUPS_UNPAIRED)
    result = gate_at_look(parent, child, PLAN_UNPAIRED, 3)
    lower, upper = _reference_exact_unpaired(
        PARENT_UNPAIRED, CHILD_UNPAIRED, GROUPS_UNPAIRED, 54321 + 3, 200, Fraction("0.02")
    )
    assert (result.lower, result.upper) == (float(lower), float(upper))
    expected = "promote" if lower > 0 else "reject" if upper <= 0 else "continue"
    assert result.decision == expected


def test_unpaired_gate_golden_values() -> None:
    """The unpaired exact-replay canary: pinned once, compared exactly."""

    parent = _samples("parent", PARENT_UNPAIRED, GROUPS_UNPAIRED)
    child = _samples("child", CHILD_UNPAIRED, GROUPS_UNPAIRED)
    result = gate_at_look(parent, child, PLAN_UNPAIRED, 3)
    # Paste the three values the implementation prints the first time this
    # test runs (`print(result.decision, result.lower.hex(), result.upper.hex())`)
    # and never edit them again: a change here is a change of the contract.
    assert result.decision == "continue"
    assert result.lower.hex() == "-0x1.5dddddddddddep-2"
    assert result.upper.hex() == "0x1.8000000000000p-2"


def test_gate_draws_consume_random_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §5: the draw is `(int(r * 2**53) * n) >> 53` over `random()`, nothing else."""

    scripted = iter([0.0, 0.5, 0.999, 0.25] * 1000)

    class Stub(random.Random):
        def random(self) -> float:  # type: ignore[override]
            return next(scripted)

        def choice(self, seq):  # type: ignore[override]
            raise AssertionError("choice must not be used")

        def choices(self, *args, **kwargs):  # type: ignore[override]
            raise AssertionError("choices must not be used")

        def getrandbits(self, k):  # type: ignore[override]
            raise AssertionError("getrandbits must not be used")

    import evolution.tree.statistics as statistics_module

    monkeypatch.setattr(statistics_module.random, "Random", lambda seed: Stub())

    parent = _samples("parent", PARENT, GROUPS)
    child = _samples("child", CHILD, GROUPS)
    gate_at_look(parent, child, PLAN, 3)


def test_gate_plan_refuses_float_alpha() -> None:
    with pytest.raises(ValueError, match="alpha must be exact"):
        GatePlan(
            seed_mode="exact", random_seed=0, alpha_spend={3: 0.01}, alpha_budget=Fraction("0.05")
        )
    with pytest.raises(ValueError, match="alpha must be exact"):
        GatePlan(
            seed_mode="exact", random_seed=0, alpha_spend={3: Fraction("0.01")}, alpha_budget=0.05
        )


def test_alpha_plan_gate_plan_is_exact() -> None:
    from evolution.tree.campaign_policy import AlphaPlan

    # Test the shared exact-arithmetic value directly, not a retired policy.
    alpha = AlphaPlan(
        budget="0.050000000000",
        spend={3: "0.010000000000", 6: "0.015000000000", 10: "0.025000000000"},
        budget_attempt="0.016666666666",
        share_attempt={3: "0.003333333333", 6: "0.005000000000", 10: "0.008333333333"},
    )
    plan = alpha.gate_plan("exact", 7)
    assert plan.alpha_spend == {
        look: Fraction(share) for look, share in alpha.share_attempt.items()
    }
    assert plan.alpha_budget == Fraction(alpha.budget_attempt)


def test_gate_plan_accepts_any_positive_look_and_refuses_non_positive() -> None:
    GatePlan(
        seed_mode="exact", random_seed=0, alpha_spend={1: Fraction("0.01"), 4: Fraction("0.01")}
    )
    with pytest.raises(ValueError, match="positive"):
        GatePlan(seed_mode="exact", random_seed=0, alpha_spend={0: Fraction("0.01")})


# -- the kernel with unequal repeats (continuable-terminals spec §4.5) -----


#: `a` lost its second repeat on both sides; `b` and `c` keep all three.
PARENT_PARTIAL = {"a": [0.125, 0.375], "b": [0.25, 0.5, 0.75], "c": [0.5, 0.25, 0.0]}
CHILD_PARTIAL = {"a": [0.625, 0.25], "b": [0.5, 0.5, 0.875], "c": [0.75, 0.25, 0.125]}


def test_a_paired_case_with_fewer_repeats_matches_the_fraction_reference() -> None:
    parent = _samples("parent", PARENT_PARTIAL, GROUPS)
    child = _samples("child", CHILD_PARTIAL, GROUPS)
    plan = GatePlan("exact", 11, {3: Fraction("0.05")}, draws=100)

    with pytest.raises(ValueError, match="lacks samples for look 3"):
        gate_at_look(parent, child, plan, 3)
    result = gate_at_look(parent, child, plan, 3, excluded_repeats=True)

    lower, upper = _reference_exact(
        PARENT_PARTIAL, CHILD_PARTIAL, GROUPS, 11 + 3, 100, Fraction("0.05")
    )
    assert (result.lower, result.upper) == (float(lower), float(upper))
    assert result.decision == ("promote" if lower > 0 else "reject" if upper <= 0 else "continue")
    # Pinned once: a change here is a change of the family-6 contract.
    assert (result.decision, result.lower.hex(), result.upper.hex()) == (
        "promote",
        "0x1.5555555555555p-8",
        "0x1.2000000000000p-2",
    )


def test_a_case_mean_is_over_its_own_repeats() -> None:
    """One case, two repeats against one with three: the case means weight equally."""

    samples = _samples("child", {"a": [1.0, 0.0], "b": [0.0, 0.0, 0.0]}, {"a": "g", "b": "g"})
    assert group_weighted_score(samples, 3, excluded_repeats=True) == 0.25


def test_an_unpaired_case_with_fewer_repeats_matches_the_fraction_reference() -> None:
    parent = _samples("parent", PARENT_PARTIAL, GROUPS_UNPAIRED)
    child = _samples("child", CHILD_PARTIAL, GROUPS_UNPAIRED)
    plan = GatePlan("unpaired", 13, {3: Fraction("0.05")}, draws=100)

    result = gate_at_look(parent, child, plan, 3, excluded_repeats=True)

    lower, upper = _reference_exact_unpaired(
        PARENT_PARTIAL, CHILD_PARTIAL, GROUPS_UNPAIRED, 13 + 3, 100, Fraction("0.05")
    )
    assert (result.lower, result.upper) == (float(lower), float(upper))
    assert (result.decision, result.lower.hex(), result.upper.hex()) == (
        "continue",
        "0x0.0p+0",
        "0x1.8000000000000p-2",
    )


def test_unequal_counts_across_sides_are_refused() -> None:
    parent = _samples("parent", PARENT_PARTIAL, GROUPS_UNPAIRED)
    child = _samples("child", {**CHILD_PARTIAL, "a": [0.625, 0.25, 0.5]}, GROUPS_UNPAIRED)
    plan = GatePlan("unpaired", 13, {3: Fraction("0.05")}, draws=100)

    with pytest.raises(ValueError, match="matching repeat counts per case"):
        gate_at_look(parent, child, plan, 3, excluded_repeats=True)


def _imported_envelope(
    entry: dict[str, Any],
    *,
    metrics: dict[str, float],
    dependency_digest: str,
    critical_pass: bool | None = None,
) -> Any:
    """A `sample_imported` envelope for one schedule entry (Task 1's payload)."""

    from evolution.tree.storage.envelope import Envelope
    from tests.tree.execution.test_journal_schema import IMPORT_SOURCE

    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run-1",
            sequence=2,
            timestamp="2026-09-18T12:00:00Z",
            event="sample_imported",
            operation_id=None,
            payload=dict(
                case_id=entry["case_id"],
                sample_id=entry["sample_id"],
                candidate_digest="c" * 64,
                outcome="completed",
                metrics=metrics,
                critical_pass=critical_pass,
                effective_seed=entry["requested_seed"],
                dependency_digest=dependency_digest,
                source=dict(IMPORT_SOURCE),
            ),
        ),
        family=S1_FAMILY,
    )


def test_an_imported_record_is_a_scored_row_with_its_own_provenance() -> None:
    from evolution.tree.campaign_statistics import sample_row

    entry: dict[str, Any] = dict(case_id="c", sample_id="01", repeat=1, requested_seed=7)
    record = _imported_envelope(entry, metrics={"score": 0.5}, dependency_digest="f" * 64)
    row = sample_row(
        entry,
        None,
        None,
        seed_mode="exact",
        dependency_digest="a" * 64,
        boot_id="",
        imported=record,
    )
    assert (row.disposition, dict(row.metrics or {}), row.imported) == (
        "score",
        {"score": 0.5},
        True,
    )
    assert row.dependency_digest == "f" * 64  # never the target's
    assert row.effective_seed == 7 and not row.exhausted
    assert (row.outcome, row.retry_ordinal, row.boot_id) == ("completed", 0, "")


def test_latest_rows_and_tries_read_an_imported_pair_as_scored_and_no_try() -> None:
    from evolution.tree.campaign_statistics import (
        MeasurementTries,
        latest_rows,
        measurement_tries,
    )
    from evolution.tree.execution.journal_schema import Lifecycle
    from tests.tree.execution.test_journal_schema import imported, retried_ledger

    state = retried_ledger()
    records = [state.created, imported(state)]
    state.accept(records[1])
    (row,) = latest_rows(records, state)
    assert (row.disposition, row.imported, row.exhausted) == ("score", True, False)
    assert measurement_tries(records) == MeasurementTries(tries=0, exhausted_repeats=(), imported=1)
    fresh = Lifecycle()
    for record in records:
        fresh.accept(record)
    assert latest_rows(records, fresh) == (row,)


def test_a_named_pair_owes_its_repair_until_a_try_past_it_started():
    from evolution.tree.campaign_statistics import owed_threshold_rows

    named = {("c1", "01"): (0, 9), ("c2", "01"): (1, 9)}
    latest = {("c1", "01"): 0, ("c2", "01"): 2}
    assert owed_threshold_rows(named, latest.get) == frozenset({("c1", "01")})


def _scored_row(value: float):
    from evolution.tree.campaign_statistics import SampleRow

    return SampleRow(
        case_id="c1",
        sample_id="s1",
        repeat=1,
        requested_seed=None,
        effective_seed=None,
        boot_id="",
        dependency_digest="",
        outcome="scored",
        disposition="score",
        metrics={"score": value},
        critical_pass=None,
    )


@pytest.mark.parametrize("value", [-0.01, 1.0001, 2.0])
def test_a_primary_outside_the_unit_interval_is_a_fault_when_checked(value):
    from evolution.tree.campaign_statistics import PRIMARY_OUT_OF_RANGE, PrimaryRange, row_fault

    checked = PrimaryRange(live=True)
    assert row_fault(_scored_row(value), "score", primary_range=checked) == PRIMARY_OUT_OF_RANGE
    assert row_fault(_scored_row(value), "score") is None  # families 3-6, pre-fix audits


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_a_primary_inside_the_unit_interval_is_not_a_fault(value):
    from evolution.tree.campaign_statistics import PrimaryRange, row_fault

    assert row_fault(_scored_row(value), "score", primary_range=PrimaryRange(live=True)) is None


# -- #91/#97: the one held-Attempt definition (rulings R13, R16) -------------


def _held_row(repeat: int, disposition: str, *, exhausted: bool = False, value: float = 0.5):
    from evolution.tree.campaign_statistics import SampleRow

    return SampleRow(
        case_id="c1",
        sample_id=f"s{repeat}",
        repeat=repeat,
        requested_seed=None,
        effective_seed=None,
        boot_id="",
        dependency_digest="",
        outcome="completed" if disposition == "score" else "target_failed",
        disposition=disposition,
        metrics={"score": value} if disposition == "score" else None,
        critical_pass=None,
        exhausted=exhausted,
    )


def test_only_the_latest_stage_s_exhausted_row_holds_an_attempt() -> None:
    """Ruling R16: an earlier look was decided over its own rows; an exhausted
    row there (a run resumed from an earlier producer) does not hold the
    Attempt at its next look. One in the latest stage does."""

    from evolution.tree.campaign_statistics import (
        NO_PRIMARY_RANGE,
        rows_held,
        stage_exhausted,
        stage_floor,
    )

    earlier = _held_row(1, "indeterminate", exhausted=True)
    scored = [_held_row(2, "score"), _held_row(3, "score")]
    assert stage_floor((1, 3), 3) == 1 and stage_floor((1, 3), 1) == 0

    assert not stage_exhausted([[earlier, *scored]], 3, stage_floor((1, 3), 3))
    assert not rows_held([([earlier, *scored], NO_PRIMARY_RANGE)], 3, 1, "score")
    assert stage_exhausted([[earlier]], 1, 0)
    latest = _held_row(3, "indeterminate", exhausted=True)
    assert rows_held([([earlier, scored[0], latest], NO_PRIMARY_RANGE)], 3, 1, "score")


def test_a_held_attempt_s_rows_are_settled_and_fault_free() -> None:
    """Ruling R13: a row still owed a D1 try, or not started, keeps the Attempt
    active; a fault row on either side keeps it out of the held set, the
    out-of-range primary only where the range rule is in force."""

    from evolution.tree.campaign_statistics import NO_PRIMARY_RANGE, PrimaryRange, rows_held

    unchecked, checked = NO_PRIMARY_RANGE, PrimaryRange(live=True, imported=True)
    exhausted = _held_row(1, "indeterminate", exhausted=True)
    owed = _held_row(1, "indeterminate")
    out_of_range = _held_row(1, "score", value=2.0)

    assert rows_held([([exhausted], unchecked)], 1, 0, "score")
    assert not rows_held([([exhausted, owed], unchecked)], 1, 0, "score")
    assert not rows_held([([exhausted, _held_row(1, "not_started")], unchecked)], 1, 0, "score")
    assert rows_held([([exhausted], unchecked), ([out_of_range], unchecked)], 1, 0, "score")
    assert not rows_held([([exhausted], unchecked), ([out_of_range], checked)], 1, 0, "score")


def test_a_settled_stage_lacking_pairs_holds_from_the_held_repair_version() -> None:
    """#120: under `lacking` (from `0.3.0a27`) a settled stage holds whenever a
    row of it did not score -- a forced stop's `cancelled` row, or a try whose
    retry the cancelled run declined -- not only through an exhausted row
    (R13, still the rule without `lacking`). A declined retry is settled only
    on its own side; a row still owed a D1 try, a row not started and a fault
    row keep the Attempt out; an earlier stage's unscored row does not hold
    it (R16)."""

    from dataclasses import replace

    from evolution.tree.campaign_statistics import (
        NO_PRIMARY_RANGE,
        declined_tries,
        rows_held,
        stage_lacking,
    )

    scored = _held_row(1, "score")
    cancelled = replace(_held_row(1, "exclude"), outcome="cancelled")
    failed = replace(_held_row(1, "exclude"), outcome="infrastructure_failed")
    owed = _held_row(1, "indeterminate")
    declined = [frozenset({(owed.case_id, owed.sample_id, owed.retry_ordinal)})]

    def held(*sides: list[Any], **kwargs: Any) -> bool:
        if kwargs.get("lacking"):
            kwargs.setdefault("declined", [frozenset()] * len(sides))
        return rows_held([(rows, NO_PRIMARY_RANGE) for rows in sides], 1, 0, "score", **kwargs)

    assert not held([scored, cancelled])
    assert held([scored, cancelled], lacking=True)
    assert stage_lacking([[scored, cancelled]], 1, 0)
    assert not held([scored, owed], lacking=True)
    assert held([scored, owed], lacking=True, declined=declined)
    assert not held([scored, owed], declined=declined)
    assert not held([scored], [owed], lacking=True, declined=[*declined, frozenset()])
    assert not held([scored, cancelled, _held_row(1, "not_started")], lacking=True)
    assert not held([scored, cancelled, failed], lacking=True)
    assert not held([scored], lacking=True)
    assert held([_held_row(1, "indeterminate", exhausted=True)], lacking=True)
    earlier = replace(cancelled, repeat=1)
    assert not rows_held(
        [([earlier, replace(scored, repeat=2)], NO_PRIMARY_RANGE)],
        2,
        1,
        "score",
        lacking=True,
        declined=[frozenset()],
    )
    with pytest.raises(ValueError, match="declined"):
        rows_held([([scored], NO_PRIMARY_RANGE)], 1, 0, "score", lacking=True)
    assert declined_tries(
        {("m-1", "c1", "s1", 0), ("m-2", "c1", "s1", 1), ("m-1", "c1", "s1")}, "m-1"
    ) == frozenset({("c1", "s1", 0)})


def test_a_carried_row_is_judged_by_the_version_its_carry_was_admitted_under() -> None:
    """ADR-0019 (amended 2026-10-03): a `sample_imported` row's range follows its
    carry's admission version as well as the version in force; a live row
    only the latter."""

    from dataclasses import replace

    from evolution.tree.campaign_statistics import (
        PRIMARY_OUT_OF_RANGE,
        primary_range_for,
        row_fault,
    )

    live = _scored_row(2.0)
    carried = replace(live, imported=True)
    pre_fix = primary_range_for(7, "0.3.0a26", "0.3.0a25")
    post_fix = primary_range_for(7, "0.3.0a26", "0.3.0a26")

    assert row_fault(live, "score", primary_range=pre_fix) == PRIMARY_OUT_OF_RANGE
    assert row_fault(carried, "score", primary_range=pre_fix) is None
    assert row_fault(carried, "score", primary_range=post_fix) == PRIMARY_OUT_OF_RANGE
    # No carry recorded: the version in force decides. Exempt in force: nothing.
    assert primary_range_for(7, "0.3.0a26", None).imported
    assert not primary_range_for(7, "0.3.0a25", "0.3.0a26").imported


def test_only_a_row_written_before_the_fix_is_historical() -> None:
    """#97 spec §4.4 as widened 2026-10-03: a measurement completed under an
    exempt version, or a row carried in under one."""

    from dataclasses import replace

    from evolution.tree.campaign_statistics import pre_fix_out_of_range

    out_of_range = _held_row(1, "score", value=2.0)
    carried = replace(out_of_range, imported=True)
    exhausted = _held_row(1, "indeterminate", exhausted=True)

    def historical(rows, *, completed=False, imported=False):
        return pre_fix_out_of_range(
            rows, "score", completed_before_fix=completed, imported_before_fix=imported
        )

    assert historical([_held_row(1, "score"), out_of_range], completed=True)
    assert not historical([_held_row(1, "score"), exhausted], completed=True)
    assert not historical([out_of_range], imported=True)  # a live row is not carried
    assert historical([carried], imported=True)
    assert not historical([carried])
