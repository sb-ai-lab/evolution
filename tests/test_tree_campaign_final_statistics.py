"""Family 14's pure statistics kernel (family-14 design R20 to R28, M2, M6; ADR-0032)."""

from __future__ import annotations

import ast
import random
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest

from evolution.tree import confidence_statistics as cs
from evolution.tree import final_statistics as fs
from evolution.tree.confidence_gate import renormalized
from evolution.tree.statistics import ScoreSample


def _sample(case_id, repeat, score, group=None, *, seed=None) -> ScoreSample:
    return ScoreSample(
        case_id=case_id,
        sample_id=str(repeat),
        score=score,
        requested_seed=seed,
        effective_seed=seed,
        boot_id="boot",
        dependency_digest="deps",
        group_id=group or f"g-{case_id}",
        stratum="s",
    )


def _side(case_id, scores, group=None, *, paired=False):
    return tuple(
        _sample(case_id, repeat, score, group, seed=repeat if paired else None)
        for repeat, score in enumerate(scores, start=1)
    )


def _case(case_id, candidate, parent, group=None, *, paired=False) -> cs.CaseEvidence:
    return cs.CaseEvidence(
        case_id,
        _side(case_id, candidate, group, paired=paired),
        _side(case_id, parent, group, paired=paired),
    )


def _e14(point=0.1, lower=None, upper=None, level=0.9, unavailable=None, exact=False):
    return fs.Estimate14(point, lower, upper, level, unavailable, exact)


# Task 2: the module, `Estimate14` and the import boundary.


def test_the_family14_kernel_imports_only_the_family13_kernel_and_the_gate_kernel() -> None:
    tree = ast.parse(Path(fs.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    project = {name for name in imported if name.startswith("evolution")}
    assert "evolution.tree.confidence_statistics" in project
    assert project <= {"evolution.tree.confidence_statistics", "evolution.tree.statistics"}


def test_the_method_is_family_13s() -> None:
    assert cs.METHOD_VERSION == "f13-bootstrap/1"
    assert not hasattr(fs, "METHOD_VERSION")


def test_an_estimate14_record_is_family_13s_five_keys_plus_exact() -> None:
    estimate = fs.Estimate14(0.25, 0.25, 0.25, 0.9, None, True)
    assert estimate.record() == {
        "point": 0.25,
        "lower": 0.25,
        "upper": 0.25,
        "level": 0.9,
        "unavailable": None,
        "exact": True,
    }
    assert set(cs.Estimate(0.25, 0.25, 0.25, 0.9, None).record()) == {
        "point",
        "lower",
        "upper",
        "level",
        "unavailable",
    }


@pytest.mark.parametrize(
    "fields",
    [
        (0.25, 0.25, None, 0.9, None, True),  # one-sided exact
        (0.25, 0.25, 0.25, 0.9, None, True),  # two-sided exact
        (0.25, 0.1, None, 0.9, None, False),
        (0.25, None, None, 0.9, "insufficient_repeats", False),
        (0.25, None, None, None, "not_requested_in_observe_mode", None),
        (None, None, None, 0.9, "final_incomplete", False),
        (None, None, None, None, "final_incomplete", None),
    ],
)
def test_admitted_estimate_shapes(fields) -> None:
    estimate = fs.Estimate14(*fields)
    assert fs.Estimate14.from_record(estimate.record()) == estimate


@pytest.mark.parametrize(
    "fields",
    [
        (0.25, 0.2, None, 0.9, None, True),  # exact, lower != point
        (0.25, 0.25, 0.3, 0.9, None, True),  # exact, upper != point
        (0.25, None, None, 0.9, None, True),  # exact without a bound
        (0.25, 0.25, 0.25, 0.9, "insufficient_repeats", True),  # exact and unavailable
        (0.25, 0.25, 0.25, 0.9, "degenerate_interval", False),  # never in family 14
        (0.25, None, None, None, "not_requested_in_observe_mode", False),  # observe claims
        (0.25, 0.25, None, 0.9, None, 1),  # exact is a boolean
        (0.25, 0.1, None, 0.9, None, None),  # a requested bound claims exactness
    ],
)
def test_refused_estimate_shapes(fields) -> None:
    with pytest.raises(ValueError):
        fs.Estimate14(*fields)


def test_from_record_refuses_a_family13_record_or_an_extra_key() -> None:
    family13 = cs.Estimate(0.25, 0.1, None, 0.9, None).record()
    with pytest.raises(ValueError):
        fs.Estimate14.from_record(family13)
    with pytest.raises(ValueError):
        fs.Estimate14.from_record({**family13, "exact": False, "extra": 1})


def test_the_family14_unavailable_vocabulary_has_no_degenerate_interval() -> None:
    assert fs.UNAVAILABLE14 == {
        "insufficient_repeats",
        "not_requested_in_observe_mode",
        "bound_not_requested_in_observe_mode",
        "final_incomplete",
    }


# Task 3: exactness from rows (R21).


def test_a_paired_constant_difference_with_varying_sides_is_exact() -> None:
    case = _case("f1", [0.5, 0.75, 1.0], [0.25, 0.5, 0.75], paired=True)
    assert cs.pairing_of((case,)) == "exact"
    assert fs.exact_scope((case,), {"f1"}, "exact") is True
    assert fs.exact_scope((case,), {"f1"}, "unpaired") is False


def test_unpaired_equal_multisets_are_not_exact() -> None:
    case = _case("f1", [0.0, 1.0, 1.0], [0.0, 1.0, 1.0])
    assert cs.pairing_of((case,)) == "unpaired"
    assert fs.exact_scope((case,), {"f1"}, "unpaired") is False


def test_paired_equal_scores_are_an_exact_zero_difference() -> None:
    case = _case("f1", [0.0, 1.0, 1.0], [0.0, 1.0, 1.0], paired=True)
    assert fs.exact_scope((case,), {"f1"}, "exact") is True


def test_constant_sides_are_exact_under_either_pairing() -> None:
    unpaired = _case("f1", [1.0, 1.0], [0.5, 0.5, 0.5])
    paired = _case("f1", [1.0, 1.0], [0.5, 0.5], paired=True)
    assert fs.exact_scope((unpaired,), {"f1"}, "unpaired") is True
    assert fs.exact_scope((paired,), {"f1"}, "exact") is True


def test_exactness_reads_only_the_scope() -> None:
    cases = (_case("f1", [1.0, 1.0], [0.5, 0.5]), _case("c1", [1.0, 0.0], [0.5, 0.5]))
    assert fs.exact_scope(cases, {"f1"}, "unpaired") is True
    assert fs.exact_scope(cases, {"c1"}, "unpaired") is False
    assert fs.exact_scope(cases, {"f1", "c1"}, "unpaired") is False


def test_exact_scope_compares_exact_fractions() -> None:
    near = _case("f1", [0.1, 0.1 + 2**-55], [0.0, 0.0])
    assert near.candidate[0].score != near.candidate[1].score
    assert fs.exact_scope((near,), {"f1"}, "unpaired") is False


@pytest.mark.parametrize(
    ("scope", "pairing"),
    [(set(), "unpaired"), ({"zz"}, "unpaired"), ({"f1"}, "none"), ({"f1"}, "exact")],
)
def test_exact_scope_refuses_a_bad_scope_pairing_or_a_missing_proof(scope, pairing) -> None:
    case = _case("f1", [1.0, 0.5], [0.5, 0.5])  # no seeds: the proof fails
    with pytest.raises(ValueError):
        fs.exact_scope((case,), scope, pairing)


def test_exact_scope_validates_every_proof_before_returning_nonexact() -> None:
    varying = _case("f1", [1.0, 0.5], [0.5, 0.5], paired=True)
    unproven = _case("c1", [1.0, 1.0], [0.5, 0.5])
    with pytest.raises(ValueError):
        fs.exact_scope((varying, unproven), ("f1", "c1"), "exact")


# Task 4: the family-14 look estimates (R20, R22).


GROUPS = {
    "f1": ("g1", "s"),
    "f2": ("g2", "s"),
    "c1": ("g1", "s"),
    "c2": ("g3", "s"),
    "u1": ("g3", "s"),
}
CORPUS = ("f1", "f2", "c1", "c2", "u1")
FW = cs.scope_weights(("f1", "f2"), GROUPS)
CW = cs.scope_weights(("c1", "c2"), GROUPS)
OW = cs.corpus_weights(("f1", "f2", "c1", "c2"), GROUPS, CORPUS)
VARIED_F = (
    _case("f1", [1.0, 0.5, 1.0, 0.75], [0.5, 0.25, 0.5, 0.0]),
    _case("f2", [0.75, 1.0, 0.5, 1.0], [0.5, 0.5, 0.75, 0.25]),
)
VARIED_C = (
    _case("c1", [0.5, 0.25, 0.5, 0.75], [0.5, 0.5, 0.25, 0.75]),
    _case("c2", [1.0, 0.75, 0.75, 1.0], [1.0, 1.0, 0.5, 0.75]),
)
EXACT_F = (_case("f1", [1.0] * 4, [0.5] * 4), _case("f2", [0.75] * 4, [0.75] * 4))
EXACT_C = (_case("c1", [0.5] * 4, [0.5] * 4), _case("c2", [0.75] * 4, [1.0] * 4))


def _final14(cases, pairing="unpaired"):
    return fs.final_statistical14(
        cases,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.8,
        pairing=pairing,
        seed=33,
        draws=300,
    )


def test_an_interim_estimate_draws_as_family_13_and_adds_exact() -> None:
    family13 = cs.interim_focus(
        VARIED_F, FW, confidence=0.9, pairing="unpaired", seed=21, draws=400
    )
    family14 = fs.interim_focus14(
        VARIED_F, FW, confidence=0.9, pairing="unpaired", seed=21, draws=400
    )
    assert family13.unavailable is None
    assert family14.record() == {**family13.record(), "exact": False}


def test_an_exact_interim_estimate_has_equal_bounds_and_no_degenerate_marker() -> None:
    family13 = cs.interim_focus(EXACT_F, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    family14 = fs.interim_focus14(EXACT_F, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    assert family13.unavailable == "degenerate_interval"
    assert family14 == fs.Estimate14(0.25, 0.25, 0.25, 0.9, None, True)


def test_an_interim_paired_constant_difference_is_exact() -> None:
    cases = (
        _case("f1", [0.5, 0.75, 1.0], [0.25, 0.5, 0.75], paired=True),
        _case("f2", [0.0, 1.0, 0.5], [0.0, 1.0, 0.5], paired=True),
    )
    estimate = fs.interim_focus14(cases, FW, confidence=0.9, pairing="exact", seed=3, draws=50)
    assert estimate == fs.Estimate14(0.125, 0.125, 0.125, 0.9, None, True)


def test_an_interim_estimate_with_one_observation_is_insufficient_and_not_exact() -> None:
    cases = (EXACT_F[0], _case("f2", [1.0], [0.5, 0.5]))
    estimate = fs.interim_focus14(cases, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    assert estimate.record() == {
        "point": cs.publish(cs.point_contrast(cases, FW)),
        "lower": None,
        "upper": None,
        "level": 0.9,
        "unavailable": "insufficient_repeats",
        "exact": False,
    }


def test_an_exact_focus_with_a_bootstrapped_causal() -> None:
    final = _final14(EXACT_F + VARIED_C)
    assert final.causal is not None
    assert (final.focus.exact, final.causal.exact) == (True, False)
    assert final.focus.lower == final.focus.point == 0.25
    assert final.causal.lower != final.causal.point
    assert final.focus.upper is None and final.causal.upper is None


def test_a_bootstrapped_focus_with_an_exact_causal() -> None:
    final = _final14(VARIED_F + EXACT_C)
    assert final.causal is not None
    assert (final.focus.exact, final.causal.exact) == (False, True)
    assert final.causal.lower == final.causal.point == -0.125


def test_both_comparisons_exact_and_the_overall_diagnostic_keeps_five_keys() -> None:
    final = _final14(EXACT_F + EXACT_C)
    family13 = cs.final_statistical(
        EXACT_F + EXACT_C,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.8,
        pairing="unpaired",
        seed=33,
        draws=300,
    )
    assert family13.overall.unavailable == "degenerate_interval"
    assert final.causal is not None
    assert (final.focus.exact, final.causal.exact) == (True, True)
    assert final.overall.record() == {**family13.overall.record(), "unavailable": None}


def test_final_values_are_family_13s_on_a_drawn_comparison() -> None:
    family13 = cs.final_statistical(
        VARIED_F + VARIED_C,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.8,
        pairing="unpaired",
        seed=33,
        draws=300,
    )
    final = _final14(VARIED_F + VARIED_C)
    assert final.causal is not None and family13.causal is not None
    assert final.focus.record() == {**family13.focus.record(), "exact": False}
    assert final.causal.record() == {**family13.causal.record(), "exact": False}
    assert final.overall == family13.overall


def test_a_short_causal_case_is_insufficient_and_not_exact() -> None:
    cases = EXACT_F + (VARIED_C[0], _case("c2", [1.0], [1.0, 0.5]))
    final = _final14(cases)
    assert final.causal is not None
    assert (final.causal.unavailable, final.causal.exact, final.causal.lower) == (
        "insufficient_repeats",
        False,
        None,
    )
    assert final.focus.exact is True


def test_observe_points_carry_a_null_exact() -> None:
    final = fs.observe_points14(EXACT_F + EXACT_C, focus=FW, causal=CW, overall=OW)
    assert final.causal is not None
    assert final.focus.record() == {
        "point": 0.25,
        "lower": None,
        "upper": None,
        "level": None,
        "unavailable": "not_requested_in_observe_mode",
        "exact": None,
    }
    assert final.causal.exact is None
    assert "exact" not in final.overall.record()


# Task 5: decisions with the margin (M6, R24).


def test_the_family14_vocabulary_is_the_designs_and_family_13s_is_unchanged() -> None:
    assert dict(fs.ATTEMPT_DECISIONS14) == {
        "continue": frozenset({None, "interval_unavailable"}),
        "rejected": frozenset({"focus_below_margin", "causal_below_tolerance", "critical_failure"}),
        "finalist": frozenset({"gate_passed", "observe_finalist"}),
        "insufficient_evidence": frozenset(
            {
                "insufficient_repeats",
                "insufficient_calibration",
                "no_evaluable_focus",
                "no_evaluable_pairs",
            }
        ),
    }
    assert "focus_below_margin" not in set().union(*cs.ATTEMPT_DECISIONS.values())
    assert "finalist" not in cs.ATTEMPT_DECISIONS


@pytest.mark.parametrize(
    ("focus", "margin", "expected"),
    [
        # interval, m < 0: U_F = -0.05 is above m = -0.1
        (_e14(point=-0.2, lower=-0.3, upper=-0.05), -0.1, ("continue", None)),
        (_e14(point=-0.2, lower=-0.3, upper=-0.1), -0.1, ("rejected", "focus_below_margin")),
        # interval, m = 0
        (_e14(point=0.0, lower=-0.2, upper=0.0), 0, ("rejected", "focus_below_margin")),
        (_e14(point=0.0, lower=-0.2, upper=5e-324), 0, ("continue", None)),
        # interval, m > 0
        (_e14(point=0.1, lower=0.05, upper=0.2), 0.2, ("rejected", "focus_below_margin")),
        (_e14(point=0.1, lower=0.05, upper=0.25), 0.2, ("continue", None)),
        # exact, m < 0, = 0, > 0: d_F decides
        (_e14(point=-0.05, lower=-0.05, upper=-0.05, exact=True), -0.1, ("continue", None)),
        (
            _e14(point=-0.1, lower=-0.1, upper=-0.1, exact=True),
            -0.1,
            ("rejected", "focus_below_margin"),
        ),
        (_e14(point=0.0, lower=0.0, upper=0.0, exact=True), 0, ("rejected", "focus_below_margin")),
        (_e14(point=0.25, lower=0.25, upper=0.25, exact=True), 0, ("continue", None)),
        (
            _e14(point=0.2, lower=0.2, upper=0.2, exact=True),
            0.2,
            ("rejected", "focus_below_margin"),
        ),
        (_e14(point=0.25, lower=0.25, upper=0.25, exact=True), 0.2, ("continue", None)),
        # no early acceptance, an unavailable interval continues
        (_e14(point=0.9, lower=0.8, upper=0.95), -0.5, ("continue", None)),
        (_e14(unavailable="insufficient_repeats"), 0, ("continue", "interval_unavailable")),
    ],
)
def test_an_interim_look_rejects_when_the_focus_upper_or_exact_point_is_at_most_m(
    focus, margin, expected
) -> None:
    assert fs.decide_interim14(focus, margin=margin, critical=False) == cs.Decision(*expected)


@pytest.mark.parametrize(
    ("focus", "causal", "margin", "tolerance", "expected"),
    [
        # interval focus at m < 0, = 0, > 0
        (_e14(point=0.0, lower=-0.05), None, -0.1, None, ("finalist", "gate_passed")),
        (_e14(point=0.0, lower=-0.1), None, -0.1, None, ("rejected", "focus_below_margin")),
        (_e14(point=0.1, lower=0.0), None, 0, None, ("rejected", "focus_below_margin")),
        (_e14(point=0.1, lower=5e-324), None, 0, None, ("finalist", "gate_passed")),
        (_e14(point=0.3, lower=0.2), None, 0.2, None, ("rejected", "focus_below_margin")),
        (_e14(point=0.3, lower=0.25), None, 0.2, None, ("finalist", "gate_passed")),
        # exact focus at m < 0, = 0, > 0
        (_e14(point=-0.05, lower=-0.05, exact=True), None, -0.1, None, ("finalist", "gate_passed")),
        (_e14(point=0.0, lower=0.0, exact=True), None, 0, None, ("rejected", "focus_below_margin")),
        (_e14(point=0.25, lower=0.25, exact=True), None, 0, None, ("finalist", "gate_passed")),
        (
            _e14(point=0.2, lower=0.2, exact=True),
            None,
            0.2,
            None,
            ("rejected", "focus_below_margin"),
        ),
        # causal, interval and exact, against the tolerance
        (_e14(lower=0.1), _e14(lower=-0.3), 0, 0.3, ("finalist", "gate_passed")),
        (
            _e14(lower=0.1),
            _e14(lower=-0.30000000000000004),
            0,
            0.3,
            ("rejected", "causal_below_tolerance"),
        ),
        (
            _e14(lower=0.1),
            _e14(point=-0.3, lower=-0.3, exact=True),
            0,
            0.3,
            ("finalist", "gate_passed"),
        ),
        (
            _e14(lower=0.1),
            _e14(point=-0.5, lower=-0.5, exact=True),
            0,
            0.3,
            ("rejected", "causal_below_tolerance"),
        ),
        # focus first, then causal (G7)
        (_e14(lower=-0.1), _e14(lower=-0.5), 0, 0.1, ("rejected", "focus_below_margin")),
        # insufficient repeats precede the bounds; there is no degenerate branch
        (
            _e14(unavailable="insufficient_repeats"),
            _e14(point=0.0, lower=0.0, exact=True),
            0,
            0.1,
            ("insufficient_evidence", "insufficient_repeats"),
        ),
    ],
)
def test_the_final_statistical_look_applies_the_margin_and_the_tolerance(
    focus, causal, margin, tolerance, expected
) -> None:
    decision = fs.decide_final_statistical14(
        focus, causal, margin=margin, tolerance=tolerance, critical=False
    )
    assert decision == cs.Decision(*expected)
    assert decision.reason in fs.ATTEMPT_DECISIONS14[decision.decision]


def test_a_critical_failure_is_decided_first_at_both_looks() -> None:
    insufficient = _e14(unavailable="insufficient_repeats")
    assert fs.decide_interim14(insufficient, margin=0, critical=True) == cs.Decision(
        "rejected", "critical_failure"
    )
    assert fs.decide_final_statistical14(
        insufficient, None, margin=0, tolerance=None, critical=True
    ) == cs.Decision("rejected", "critical_failure")


@pytest.mark.parametrize("margin", [float("nan"), float("inf"), True, "0", None])
def test_a_malformed_margin_is_refused_even_under_a_critical_failure(margin) -> None:
    with pytest.raises(ValueError):
        fs.decide_interim14(_e14(), margin=margin, critical=True)
    with pytest.raises(ValueError):
        fs.decide_final_statistical14(_e14(), None, margin=margin, tolerance=None, critical=True)
    with pytest.raises(ValueError):
        fs.would_pass_point14(0.1, None, None, margin=margin)


def test_a_negative_zero_margin_is_zero() -> None:
    exact_zero = _e14(point=0.0, lower=0.0, upper=0.0, exact=True)
    assert (
        fs.decide_interim14(exact_zero, margin=-0.0, critical=False).reason == "focus_below_margin"
    )
    assert fs.would_pass_point14(0.0, None, None, margin=-0.0) is False


def test_the_final_look_refuses_a_missing_or_negative_tolerance_and_a_foreign_unavailable() -> None:
    with pytest.raises(ValueError):
        fs.decide_final_statistical14(
            _e14(lower=0.1), _e14(lower=0.0), margin=0, tolerance=None, critical=False
        )
    with pytest.raises(ValueError):
        fs.decide_final_statistical14(
            _e14(lower=0.1), _e14(lower=0.0), margin=0, tolerance=-0.1, critical=False
        )
    observe = fs.Estimate14(0.1, None, None, None, "not_requested_in_observe_mode", None)
    with pytest.raises(ValueError):
        fs.decide_final_statistical14(observe, None, margin=0, tolerance=None, critical=False)


def test_an_available_interim_estimate_needs_both_bounds() -> None:
    with pytest.raises(ValueError):
        fs.decide_interim14(_e14(lower=0.1, upper=None), margin=0, critical=False)


@pytest.mark.parametrize(
    ("focus", "causal", "tolerance", "margin", "expected"),
    [
        (0.05, None, None, -0.1, True),
        (-0.1, None, None, -0.1, False),
        (0.0, None, None, 0, False),
        (0.1, None, None, 0, True),
        (0.2, None, None, 0.2, False),
        (0.25, None, None, 0.2, True),
        (0.1, -0.1, None, 0, None),
        (0.1, -0.1, 0.1, 0, True),
        (0.1, -0.2, 0.1, 0, False),
        (0.1, 0.0, 0.0, 0.1, False),
    ],
)
def test_would_pass_point_reads_the_margin(focus, causal, tolerance, margin, expected) -> None:
    assert fs.would_pass_point14(focus, causal, tolerance, margin=margin) is expected


def test_observe_names_a_spared_attempt_finalist() -> None:
    assert fs.decide_final_observe14(critical=False) == cs.Decision("finalist", "observe_finalist")
    assert fs.decide_final_observe14(critical=True) == cs.Decision("rejected", "critical_failure")


def test_an_exact_look_decides_end_to_end_on_its_point() -> None:
    interim = fs.interim_focus14(EXACT_F, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    assert fs.decide_interim14(interim, margin=0.25, critical=False).reason == "focus_below_margin"
    assert fs.decide_interim14(interim, margin=0.2, critical=False) == cs.Decision("continue", None)
    final = _final14(EXACT_F + EXACT_C)
    assert fs.decide_final_statistical14(
        final.focus, final.causal, margin=0, tolerance=0.125, critical=False
    ) == cs.Decision("finalist", "gate_passed")
    assert fs.decide_final_statistical14(
        final.focus, final.causal, margin=0, tolerance=0.1, critical=False
    ) == cs.Decision("rejected", "causal_below_tolerance")


# Review focus (Task 5): inputs the spec implies.


def test_review_a_record_read_back_with_an_integer_or_negative_zero_point() -> None:
    record = {
        "point": 0,
        "lower": -0.0,
        "upper": 0.0,
        "level": 0.9,
        "unavailable": None,
        "exact": True,
    }
    estimate = fs.Estimate14.from_record(record)
    assert fs.decide_interim14(estimate, margin=0, critical=False) == cs.Decision(
        "rejected", "focus_below_margin"
    )
    assert fs.decide_interim14(estimate, margin=-0.5, critical=False) == cs.Decision(
        "continue", None
    )


@pytest.mark.parametrize(
    ("estimate", "margin", "expected"),
    [
        (_e14(point=1.0, lower=1.0, exact=True), 1.0, "focus_below_margin"),
        (_e14(point=0.9, lower=0.8), 1.0, "focus_below_margin"),
        (_e14(point=-0.9, lower=-0.95), -1.0, "gate_passed"),
        (_e14(point=-1.0, lower=-1.0, exact=True), -1.0, "focus_below_margin"),
    ],
)
def test_review_a_margin_at_the_edge_of_the_score_range(estimate, margin, expected) -> None:
    decision = fs.decide_final_statistical14(
        estimate, None, margin=margin, tolerance=None, critical=False
    )
    assert decision.reason == expected


def test_review_an_empty_causal_scope_keeps_focus_exactness() -> None:
    final = fs.final_statistical14(
        EXACT_F,
        focus=FW,
        causal=None,
        overall=cs.corpus_weights(("f1", "f2"), GROUPS, CORPUS),
        focus_confidence=0.9,
        causal_confidence=0.8,
        pairing="unpaired",
        seed=5,
        draws=50,
    )
    assert final.causal is None
    assert final.focus == fs.Estimate14(0.25, 0.25, None, 0.9, None, True)
    assert final.overall.level == 0.9 and final.overall.unavailable is None
    assert fs.decide_final_statistical14(
        final.focus, None, margin=0, tolerance=None, critical=False
    ) == cs.Decision("finalist", "gate_passed")


# Task 6: calibration -- zero tolerance and the unavailable close (R25, R26).


@pytest.mark.parametrize(
    ("mode", "degenerate", "unavailable", "close", "caveat"),
    [
        ("statistical", False, None, None, None),
        ("statistical", True, None, None, "noise_not_observed"),
        (
            "statistical",
            False,
            "insufficient_calibration_repeats",
            ("insufficient_evidence", "insufficient_calibration"),
            None,
        ),
        ("observe", False, None, None, None),
        ("observe", True, None, None, "noise_not_observed"),
        ("observe", False, "insufficient_calibration_repeats", None, "noise_allowance_unavailable"),
    ],
)
def test_a_family14_calibration_verdict(mode, degenerate, unavailable, close, caveat) -> None:
    verdict = fs.decide_calibration14(mode, degenerate=degenerate, unavailable=unavailable)
    assert verdict == cs.CalibrationVerdict(None if close is None else cs.Decision(*close), caveat)


def test_a_calibration_verdict_refuses_an_unknown_mode_or_reason() -> None:
    with pytest.raises(ValueError):
        fs.decide_calibration14("smoke", degenerate=False, unavailable=None)
    with pytest.raises(ValueError):
        fs.decide_calibration14("statistical", degenerate=False, unavailable="other")


def _noise(scores):
    cases = tuple(
        cs.NoiseCase(case_id, _side(case_id, values), len(values), len(values))
        for case_id, values in scores.items()
    )
    weights = {case_id: Fraction(1, len(scores)) for case_id in scores}
    return cs.noise_calibration(cases, weights, seed=5, draws=200)


def test_a_constant_parent_gives_a_tolerance_of_exactly_zero() -> None:
    result = _noise({"c1": [0.5, 0.5, 0.5], "c2": [1.0, 1.0]})
    assert (result.degenerate, result.allowance, result.unavailable) == (True, Fraction(0), None)
    tolerance = fs.measured_tolerance14(
        cs.publish(result.allowance), degenerate=True, unavailable=None
    )
    assert tolerance == 0.0
    assert fs.decide_calibration14("statistical", degenerate=True, unavailable=None).close is None


def test_a_degenerate_calibration_is_zero_whatever_its_published_allowance() -> None:
    assert fs.measured_tolerance14(0.01, degenerate=True, unavailable=None) == 0.0
    assert fs.measured_tolerance14(0.03, degenerate=False, unavailable=None) == 0.03
    assert (
        fs.measured_tolerance14(
            None, degenerate=False, unavailable="insufficient_calibration_repeats"
        )
        is None
    )


@pytest.mark.parametrize(
    ("allowance", "unavailable"),
    [(None, None), (0.1, "insufficient_calibration_repeats"), (-0.1, None)],
)
def test_measured_tolerance_refuses_an_inconsistent_calibration(allowance, unavailable) -> None:
    with pytest.raises(ValueError):
        fs.measured_tolerance14(allowance, degenerate=False, unavailable=unavailable)


def test_a_varied_candidate_against_zero_tolerance() -> None:
    parent = [0.5] * 4
    below = (
        _case("c1", [0.5, 0.75, 0.25, 0.5], parent),
        _case("c2", [0.5, 0.5, 0.25, 0.75], parent),
    )
    above = (_case("c1", [0.5, 0.75, 0.5, 1.0], parent), _case("c2", [0.5, 1.0, 0.75, 0.5], parent))
    for cases, expected in ((below, "causal_below_tolerance"), (above, "gate_passed")):
        final = _final14(EXACT_F + cases)
        assert final.causal is not None and final.causal.exact is False
        decision = fs.decide_final_statistical14(
            final.focus, final.causal, margin=0, tolerance=0.0, critical=False
        )
        assert decision.reason == expected


def test_an_unavailable_calibration_still_closes_a_statistical_attempt() -> None:
    result = _noise({"c1": [0.5], "c2": [1.0, 0.5]})
    assert result.unavailable == "insufficient_calibration_repeats"
    verdict = fs.decide_calibration14(
        "statistical", degenerate=result.degenerate, unavailable=result.unavailable
    )
    assert verdict.close == cs.Decision("insufficient_evidence", "insufficient_calibration")


# Task 7: the final contrast with the stored Champion side (R28, M2).


FC_GROUPS = {"a": ("g1", "s"), "b": ("g1", "s"), "c": ("g2", "s"), "d": ("g3", "s")}
FC_CORPUS = ("a", "b", "c", "d")
FC_WEIGHTS = cs.scope_weights(FC_CORPUS, FC_GROUPS)


def _final_case(case_id, finalist, champion, *, paired=False) -> fs.FinalCase:
    group = FC_GROUPS[case_id][0]
    return fs.FinalCase(
        case_id,
        _side(case_id, finalist, group, paired=paired),
        _side(case_id, champion, group, paired=paired),
    )


STORED = (
    _final_case("a", [1.0, 0.5, 1.0], [0.5, 0.25, 0.5]),
    _final_case("b", [0.75, 1.0, 0.5], [0.5, 0.75, 0.25]),
    _final_case("c", [0.5, 0.25, 0.75], [0.25, 0.5, 0.5]),
    _final_case("d", [1.0, 0.75, 1.0], [0.75, 1.0, 0.5]),
)


def _evidence(final_cases, keep=None):
    return tuple(
        cs.CaseEvidence(case.case_id, case.finalist, case.champion)
        for case in final_cases
        if keep is None or case.case_id in keep
    )


def _contrast(cases, *, excluded=frozenset(), level=0.95, pairing="unpaired"):
    return fs.final_contrast(
        cases, FC_WEIGHTS, excluded=excluded, level=level, pairing=pairing, seed=44, draws=200
    )


def test_with_nothing_excluded_the_final_contrast_is_family_13s_cohort_contrast() -> None:
    result = _contrast(STORED)
    family13 = cs.cohort_contrast(
        _evidence(STORED), FC_WEIGHTS, level=0.95, pairing="unpaired", seed=44, draws=200
    )
    assert result.estimate.record() == {**family13.record(), "exact": False}
    assert result.point_cases == result.bound_cases == FC_CORPUS
    assert (result.removed_cases, result.removed_pairs) == ((), ())


def test_the_renormalized_weights_are_family_13s_e1_rescaling() -> None:
    kept = ("a", "c", "d")
    assert fs.renormalized_weights(FC_WEIGHTS, kept) == renormalized(
        {case_id: FC_WEIGHTS[case_id] for case_id in kept}
    )
    assert sum(fs.renormalized_weights(FC_WEIGHTS, kept).values()) == 1
    with pytest.raises(ValueError):
        fs.renormalized_weights(FC_WEIGHTS, ())
    with pytest.raises(ValueError):
        fs.renormalized_weights(FC_WEIGHTS, ("zz",))


def test_excluded_pairs_leave_both_sides_and_are_reported_in_corpus_order() -> None:
    excluded = frozenset({("c", 2), ("a", 3)})
    result = _contrast(STORED, excluded=excluded)
    assert result.removed_pairs == (("a", 3), ("c", 2))
    masked = (
        _final_case("a", [1.0, 0.5], [0.5, 0.25]),
        STORED[1],
        fs.FinalCase(
            "c",
            (_sample("c", 1, 0.5, "g2"), _sample("c", 3, 0.75, "g2")),
            (_sample("c", 1, 0.25, "g2"), _sample("c", 3, 0.5, "g2")),
        ),
        STORED[3],
    )
    expected = _contrast(masked)
    assert result.estimate == expected.estimate
    assert result.point_cases == result.bound_cases == FC_CORPUS


def test_a_case_with_one_observation_counts_in_the_point_but_not_the_bound() -> None:
    excluded = frozenset({("b", 1), ("b", 2)})
    result = _contrast(STORED, excluded=excluded)
    assert result.point_cases == FC_CORPUS
    assert result.bound_cases == ("a", "c", "d")
    point_evidence = _evidence((STORED[0], _final_case("b", [0.5], [0.25]), STORED[2], STORED[3]))
    assert result.estimate.point == cs.publish(
        cs.point_contrast(point_evidence, fs.renormalized_weights(FC_WEIGHTS, FC_CORPUS))
    )
    bound = cs.cohort_contrast(
        _evidence(STORED, {"a", "c", "d"}),
        fs.renormalized_weights(FC_WEIGHTS, ("a", "c", "d")),
        level=0.95,
        pairing="unpaired",
        seed=44,
        draws=200,
    )
    assert result.estimate.lower == bound.lower
    assert result.estimate.exact is False


def test_a_one_observation_case_keeps_an_exact_bound_set_from_being_exact() -> None:
    constant = (
        _final_case("a", [1.0, 1.0], [0.5, 0.5]),
        _final_case("b", [0.75], [0.75]),
        _final_case("c", [0.5, 0.5], [0.5, 0.5]),
        _final_case("d", [1.0, 1.0], [0.75, 0.75]),
    )
    result = _contrast(constant)
    assert result.bound_cases == ("a", "c", "d")
    assert result.estimate.exact is False
    assert result.estimate.lower != result.estimate.point


def test_an_exact_global_contrast_has_its_bound_at_the_point() -> None:
    constant = (
        _final_case("a", [1.0, 1.0], [0.5, 0.5]),
        _final_case("b", [0.75, 0.75], [0.75, 0.75]),
        _final_case("c", [0.5, 0.5], [0.5, 0.5]),
        _final_case("d", [1.0, 1.0], [0.75, 0.75]),
    )
    result = _contrast(constant)
    assert result.estimate.exact is True
    assert result.estimate.lower == result.estimate.point
    assert result.estimate.upper is None


def test_a_stored_paired_constant_difference_is_exact() -> None:
    paired = tuple(
        _final_case(case_id, [0.75, 0.5, 1.0], [0.5, 0.25, 0.75], paired=True)
        for case_id in FC_CORPUS
    )
    pairing = fs.final_pairing(paired, excluded=frozenset())
    result = _contrast(paired, pairing=pairing)
    assert pairing == "exact"
    assert result.estimate == fs.Estimate14(0.25, 0.25, None, 0.95, None, True)


def test_observe_at_r1_keeps_its_full_point_contrast() -> None:
    single = tuple(
        _final_case(case.case_id, [case.finalist[0].score], [case.champion[0].score])
        for case in STORED
    )
    result = _contrast(single, level=None)
    assert result.point_cases == FC_CORPUS and result.bound_cases == ()
    assert result.estimate.record() == {
        "point": cs.publish(cs.point_contrast(_evidence(single), FC_WEIGHTS)),
        "lower": None,
        "upper": None,
        "level": None,
        "unavailable": "bound_not_requested_in_observe_mode",
        "exact": None,
    }


def test_an_empty_point_set_is_final_incomplete() -> None:
    excluded = frozenset((case_id, repeat) for case_id in FC_CORPUS for repeat in (1, 2, 3))
    for level, exact in ((0.95, False), (None, None)):
        result = _contrast(STORED, excluded=excluded, level=level)
        assert result.estimate == fs.Estimate14(None, None, None, level, "final_incomplete", exact)
        assert result.point_cases == () and result.removed_cases == FC_CORPUS
        assert len(result.removed_pairs) == 12


def test_an_empty_bound_set_publishes_a_null_lower_bound() -> None:
    single = tuple(
        _final_case(case.case_id, [case.finalist[0].score], [case.champion[0].score])
        for case in STORED
    )
    result = _contrast(single)
    assert result.estimate.record() == {
        "point": cs.publish(cs.point_contrast(_evidence(single), FC_WEIGHTS)),
        "lower": None,
        "upper": None,
        "level": 0.95,
        "unavailable": "insufficient_repeats",
        "exact": False,
    }


def test_an_unobserved_case_is_removed_not_an_invariant_failure() -> None:
    cases = (STORED[0], _final_case("b", [0.75, 1.0], []), STORED[2], STORED[3])
    result = _contrast(cases)
    assert result.removed_cases == ("b",)
    assert result.point_cases == result.bound_cases == ("a", "c", "d")
    assert result.estimate.point == cs.publish(
        cs.point_contrast(
            _evidence(STORED, {"a", "c", "d"}),
            fs.renormalized_weights(FC_WEIGHTS, ("a", "c", "d")),
        )
    )


@pytest.mark.parametrize(
    ("cases", "weights", "excluded"),
    [
        (STORED, FC_WEIGHTS, frozenset({("zz", 1)})),
        (STORED, FC_WEIGHTS, frozenset({("a", 0)})),
        (STORED, {k: v for k, v in FC_WEIGHTS.items() if k != "d"}, frozenset()),
        (STORED + STORED[:1], FC_WEIGHTS, frozenset()),
        ((), FC_WEIGHTS, frozenset()),
    ],
)
def test_the_final_contrast_refuses_malformed_inputs(cases, weights, excluded) -> None:
    with pytest.raises(ValueError):
        fs.final_contrast(
            cases, weights, excluded=excluded, level=0.95, pairing="unpaired", seed=1, draws=10
        )


def test_a_final_case_refuses_a_sample_id_that_is_not_a_repeat() -> None:
    odd = ScoreSample("a", "r1", 0.5, None, None, "boot", "deps", "g1", "s")
    with pytest.raises(ValueError):
        fs.FinalCase("a", (odd,), ())


def test_the_pairing_is_exact_only_when_every_pair_left_proves() -> None:
    paired = tuple(
        _final_case(case_id, [0.75, 0.5, 1.0], [0.5, 0.25, 0.75], paired=True)
        for case_id in FC_CORPUS
    )
    assert fs.final_pairing(paired, excluded=frozenset()) == "exact"
    assert fs.final_pairing(STORED, excluded=frozenset()) == "unpaired"
    unproved = fs.FinalCase(
        "d",
        _side("d", [1.0, 0.5], "g3", paired=True),
        _side("d", [0.5, 0.25], "g3"),  # the Champion side has no seeds
    )
    mixed = (*paired[:3], unproved)
    assert fs.final_pairing(mixed, excluded=frozenset()) == "unpaired"
    assert fs.final_pairing(mixed, excluded=frozenset({("d", 1), ("d", 2)})) == "exact"
    everything = frozenset((case_id, r) for case_id in FC_CORPUS for r in (1, 2, 3))
    assert fs.final_pairing(paired, excluded=everything) == "unpaired"
    with pytest.raises(ValueError):
        fs.final_pairing(paired, excluded=frozenset({("zz", 1)}))


# Review focus (Task 7): inputs the spec implies.


def test_review_exact_pairing_with_a_failing_proof_is_refused_not_drawn_unpaired() -> None:
    with pytest.raises(ValueError):
        _contrast(STORED, pairing="exact")  # STORED carries no seeds


def test_review_unequal_repeat_counts_on_the_two_sides_of_a_stored_contrast() -> None:
    cases = (
        _final_case("a", [1.0, 0.5, 1.0], [0.5, 0.25, 0.5, 0.75]),
        STORED[1],
        STORED[2],
        STORED[3],
    )
    result = _contrast(cases)
    assert result.point_cases == result.bound_cases == FC_CORPUS
    assert result.estimate.point == cs.publish(cs.point_contrast(_evidence(cases), FC_WEIGHTS))
    paired = tuple(
        _final_case(case_id, [0.5, 0.75, 1.0], [0.25, 0.5, 0.75, 1.0], paired=True)
        for case_id in FC_CORPUS
    )
    with pytest.raises(ValueError):
        _contrast(paired, pairing="exact")


# Task 8: the global verdict (M2, D10).


def test_the_global_vocabulary_names_accepted_and_final_incomplete() -> None:
    assert dict(fs.GLOBAL_REASONS14) == {
        True: frozenset({"global_off", "global_point_improved", "global_bound_positive"}),
        False: frozenset(
            {
                "critical_failure",
                "final_incomplete",
                "global_point_not_improved",
                "global_bound_not_positive",
            }
        ),
    }
    assert "final_incomplete" not in cs.GLOBAL_REASONS[False]


@pytest.mark.parametrize(
    ("mode", "complete", "critical", "point", "lower", "expected"),
    [
        ("off", False, False, None, None, (False, "final_incomplete")),
        ("point", True, False, None, None, (False, "final_incomplete")),  # empty point set
        ("off", True, False, None, None, (False, "final_incomplete")),
        ("bound", True, True, 0.1, None, (False, "final_incomplete")),  # empty bound set first
        ("bound", True, False, 0.1, None, (False, "final_incomplete")),
        ("point", True, False, 0.1, None, (True, "global_point_improved")),  # point decides
        ("off", True, False, 0.1, None, (True, "global_off")),
        ("point", True, True, 0.1, None, (False, "critical_failure")),
        ("point", True, False, 0.0, None, (False, "global_point_not_improved")),
        ("bound", True, False, 0.1, 0.0, (False, "global_bound_not_positive")),
        ("bound", True, False, 0.1, 5e-324, (True, "global_bound_positive")),
    ],
)
def test_the_global_verdict(mode, complete, critical, point, lower, expected) -> None:
    verdict = fs.decide_global14(
        mode, complete=complete, critical=critical, point=point, lower=lower
    )
    assert verdict == fs.GlobalVerdict14(*expected)
    assert verdict.reason in fs.GLOBAL_REASONS14[verdict.accepted]


def test_the_global_verdict_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        fs.decide_global14("strict", complete=True, critical=False, point=0.1, lower=0.1)


def test_a_stored_side_contrast_decides_through_the_global_verdict() -> None:
    single = tuple(
        _final_case(case.case_id, [case.finalist[0].score], [case.champion[0].score])
        for case in STORED
    )
    estimate = _contrast(single).estimate
    verdicts = {
        mode: fs.decide_global14(
            mode, complete=True, critical=False, point=estimate.point, lower=estimate.lower
        )
        for mode in ("off", "point", "bound")
    }
    assert verdicts["bound"] == fs.GlobalVerdict14(False, "final_incomplete")
    assert verdicts["point"].reason in {"global_point_improved", "global_point_not_improved"}
    assert verdicts["off"] == fs.GlobalVerdict14(True, "global_off")


# Task 9: family 14 at m = 0 is family 13 away from degeneracy.


_RENAMED = {
    ("rejected", "focus_upper_nonpositive"): ("rejected", "focus_below_margin"),
    ("rejected", "focus_lower_not_positive"): ("rejected", "focus_below_margin"),
    ("accepted", "gate_passed"): ("finalist", "gate_passed"),
}


def _trajectory_cases(k, look):
    """Synthetic 0/1 rows: a better, a worse and an equal Candidate in turn."""

    stream = random.Random(4000 + k)
    rate = (0.7, 0.1, 0.4)[k % 3]
    cases = []
    for case_id in ("f1", "f2", "c1", "c2"):
        candidate = [float(stream.random() < rate) for _ in range(look)]
        parent = [float(stream.random() < 0.4) for _ in range(look)]
        cases.append(_case(case_id, candidate, parent))
    return tuple(cases)


def _renamed(decision):
    return _RENAMED.get((decision.decision, decision.reason), (decision.decision, decision.reason))


def test_family14_at_zero_margin_decides_as_family_13_on_drawn_comparisons() -> None:
    seen = Counter()
    for k in range(24):
        for look in (3, 5):
            focus = _trajectory_cases(k, look)[:2]
            f13 = cs.interim_focus(focus, FW, confidence=0.9, pairing="unpaired", seed=k, draws=200)
            f14 = fs.interim_focus14(
                focus, FW, confidence=0.9, pairing="unpaired", seed=k, draws=200
            )
            if f13.unavailable is not None:
                continue
            d14 = fs.decide_interim14(f14, margin=0, critical=False)
            assert _renamed(cs.decide_interim(f13, critical=False)) == (d14.decision, d14.reason)
            seen[("interim", d14.decision)] += 1
        cases = _trajectory_cases(k, 6)
        kwargs = dict(
            focus=FW,
            causal=CW,
            overall=OW,
            focus_confidence=0.9,
            causal_confidence=0.9,
            pairing="unpaired",
            seed=k,
            draws=200,
        )
        f13 = cs.final_statistical(cases, **kwargs)
        f14 = fs.final_statistical14(cases, **kwargs)
        assert f13.causal is not None
        if "degenerate_interval" in (f13.focus.unavailable, f13.causal.unavailable):
            continue
        d13 = cs.decide_final_statistical(f13.focus, f13.causal, tolerance=0.05, critical=False)
        d14 = fs.decide_final_statistical14(
            f14.focus, f14.causal, margin=0, tolerance=0.05, critical=False
        )
        assert _renamed(d13) == (d14.decision, d14.reason)
        seen[("final", d14.decision)] += 1
    # both directions are exercised at both looks
    assert seen[("interim", "rejected")] and seen[("interim", "continue")]
    assert seen[("final", "rejected")] and seen[("final", "finalist")]


def _exact_cases(k, look, *, paired):
    """Rows the bootstrap cannot vary: constant paired differences, or constant sides."""

    stream = random.Random(7000 + k)
    cases = []
    for case_id in ("f1", "f2", "c1", "c2"):
        shift = stream.choice((-0.25, 0.0, 0.25, 0.5))
        if paired:
            parent = [stream.choice((0.25, 0.5)) for _ in range(look)]
        else:
            parent = [stream.choice((0.25, 0.5))] * look
        candidate = [score + shift for score in parent]
        cases.append(_case(case_id, candidate, parent, paired=paired))
    return tuple(cases)


@pytest.mark.parametrize("paired", [True, False])
def test_rows_without_variation_are_exactly_family_13s_degenerate_draws(paired) -> None:
    pairing = "exact" if paired else "unpaired"
    for k in range(12):
        cases = _exact_cases(k, 4, paired=paired)
        assert fs.exact_scope(cases, {"f1", "f2", "c1", "c2"}, pairing) is True
        f13 = cs.interim_focus(cases[:2], FW, confidence=0.9, pairing=pairing, seed=k, draws=100)
        f14 = fs.interim_focus14(cases[:2], FW, confidence=0.9, pairing=pairing, seed=k, draws=100)
        assert f13.unavailable == "degenerate_interval"
        assert f14.exact is True and f14.lower == f14.upper == f14.point == f13.point


def test_at_the_methods_draw_count_an_exact_comparison_publishes_its_point() -> None:
    cases = _exact_cases(1, 5, paired=True)
    interim = fs.interim_focus14(cases[:2], FW, confidence=0.9, pairing="exact", seed=11)
    final = fs.final_statistical14(
        cases,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="exact",
        seed=12,
    )
    assert final.causal is not None
    assert interim.exact and interim.lower == interim.upper == interim.point
    for estimate in (final.focus, final.causal):
        assert estimate.exact and estimate.lower == estimate.point and estimate.upper is None
    assert final.overall.unavailable is None
