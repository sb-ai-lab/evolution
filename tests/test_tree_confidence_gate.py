"""`confidence_gate`: the family-13 gate payloads from rows (slice F4)."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.confidence_gate import GateInvariant, gate_inputs
from evolution.tree.confidence_statistics import (
    NoiseCase,
    corpus_weights,
    derive_seed,
    final_statistical,
    interim_focus,
    method_object,
    noise_calibration,
    noise_result_digest,
    pairing_of,
    publish,
    scope_weights,
)
from tests.campaign_drivers import sealed_prefix  # noqa: F401  (fixture)

GROUPS = {"c1": ("g1", "s"), "c2": ("g2", "s"), "c3": ("g2", "s")}
CORPUS_IDS = ["c1", "c2", "c3"]


def _row(case_id, repeat, score, *, disposition="score", critical=None):
    """`tests/test_tree_affected_gate.py`'s row, plus a critical flag."""

    scored = disposition == "score"
    seed = 100 + repeat
    return SampleRow(
        case_id=case_id,
        sample_id=f"{repeat:02d}",
        repeat=repeat,
        requested_seed=seed,
        effective_seed=seed,
        boot_id="boot",
        dependency_digest="deps",
        outcome="completed" if scored else "target_failed",
        disposition=disposition,
        metrics={"score": score} if scored else None,
        critical_pass=critical,
    )


def _rows(table):
    """`{case_id: [score per repeat]}` -> rows, repeats from 1."""

    return tuple(_row(c, r, v) for c, scores in table.items() for r, v in enumerate(scores, 1))


def _comparison(
    *,
    mode="statistical",
    focus=(0,),
    causal=(1,),
    retained=None,
    repeats=4,
    looks=(3, 4),
    tolerance=None,
    parent_source="fresh_parent",
):
    return {
        "seq": 7,
        "digest": "a" * 64,
        "round": 1,
        "attempt_id": "r01-a01",
        "supersedes": None,
        "mode": mode,
        "epoch": 0,
        "candidate_measurement_id": "m-r01-a01",
        "parent_measurement_id": "m-r01-a01-parent" if parent_source == "fresh_parent" else None,
        "baseline_measurement_id": "m-r01-champion",
        "parent_source": parent_source,
        "focus": list(focus),
        "causal": list(causal),
        "retained": retained or {"focus": list(focus), "causal": list(causal)},
        "exclusions": None,
        "repeats": repeats,
        "looks": list(looks),
        "confidence": {"focus": 0.9, "causal": 0.9} if mode == "statistical" else None,
        "tolerance": tolerance or {"kind": "numeric", "value": 0.05},
        "method": method_object(),
    }


CALIBRATION = {
    "seq": 8,
    "digest": "b" * 64,
    "round": 1,
    "attempt_id": "r01-a01",
    "comparison": 7,
    "supersedes": None,
    "source_measurement_id": "m-r01-a01-parent",
    "source": "fresh_parent",
    "prefix": None,
}
NOISE_PREFIX = "measurements/m-r01-a01-parent/events.jsonl#bytes=0:200"


def _fail(*_args, **_kwargs):
    raise AssertionError("no draw expected")


def _score_samples(rows):
    from evolution.tree.campaign_statistics import score_samples_of

    return score_samples_of(rows, GROUPS, "score", 10)


def _calibrate(comparison, rows, *, excluded=frozenset(), horizon=None, draws=500):
    from evolution.tree.confidence_gate import calibration_record

    return calibration_record(
        comparison,
        CALIBRATION,
        parent_rows=rows,
        excluded=excluded,
        groups=GROUPS,
        corpus_ids=CORPUS_IDS,
        primary="score",
        horizon=horizon if horizon is not None else comparison["repeats"],
        prefix=NOISE_PREFIX,
        campaign_seed=17,
        draws=draws,
    )


# -- The noise calibration (design §6.4, plan choice 11; Task 5) --------------


def test_a_calibration_reads_every_usable_parent_observation_of_the_retained_c_cases():
    comparison = _comparison(focus=(0,), causal=(1, 2), repeats=3, looks=(3,))
    rows = _rows({"c2": [0.2, 0.5, 0.9], "c3": [0.1, 0.4, 0.8]})
    record = _calibrate(comparison, rows, excluded={("c3", 1)})
    seed = derive_seed(
        campaign_seed=17,
        binding="b" * 64,
        scope="noise",
        prefixes=[NOISE_PREFIX],
        look=None,
        purpose="noise",
    )
    kept = tuple(r for r in rows if (r.case_id, r.repeat) != ("c3", 1))
    samples = {c: tuple(x for x in _score_samples(kept) if x.case_id == c) for c in ("c2", "c3")}
    expected = noise_calibration(
        [
            NoiseCase("c2", samples["c2"], first=3, second=3),
            NoiseCase("c3", samples["c3"], first=2, second=2),
        ],
        {"c2": Fraction(1, 2), "c3": Fraction(1, 2)},
        seed=seed,
        draws=500,
    )
    assert expected.draws is not None and not expected.degenerate
    assert record == {
        "round": 1,
        "attempt_id": "r01-a01",
        "binding": 8,
        "prefix": NOISE_PREFIX,
        "allowance": publish(expected.allowance),
        "degenerate": expected.degenerate,
        "unavailable": None,
        "result_digest": noise_result_digest(expected.draws),
    }


def test_a_case_with_one_usable_parent_observation_is_unavailable_without_a_draw(monkeypatch):
    from evolution.tree import confidence_statistics

    monkeypatch.setattr(confidence_statistics, "_resample", _fail)
    comparison = _comparison(mode="observe", repeats=1, looks=(1,))
    record = _calibrate(comparison, _rows({"c2": [0.4]}))
    assert (
        record["allowance"],
        record["degenerate"],
        record["unavailable"],
        record["result_digest"],
    ) == (None, False, "insufficient_calibration_repeats", None)


def test_the_observe_r1_pool_holds_both_parent_observations_and_draws_one_versus_one():
    comparison = _comparison(mode="observe", repeats=1, looks=(1,))
    rows = _rows({"c2": [0.0, 1.0]})
    record = _calibrate(comparison, rows, horizon=2)
    # D0 over one-versus-one draws from {0, 1} is -1, 0 or 1; its 0.5% quantile is -1.
    assert (record["unavailable"], record["degenerate"], record["allowance"]) == (None, False, 1.0)
    # Two-versus-two draws give the same allowance, so the digest pins the sizes
    # (plan choice 11: `first = second = k`, the unexcluded repeats up to R, not the pool).
    seed = derive_seed(
        campaign_seed=17,
        binding="b" * 64,
        scope="noise",
        prefixes=[NOISE_PREFIX],
        look=None,
        purpose="noise",
    )
    expected = noise_calibration(
        [NoiseCase("c2", _score_samples(rows), first=1, second=1)],
        {"c2": Fraction(1)},
        seed=seed,
        draws=500,
    )
    assert expected.draws is not None
    assert record["result_digest"] == noise_result_digest(expected.draws)


def test_a_retained_case_with_no_usable_parent_observation_is_a_gate_invariant():
    """F15 and plan F10: a correct caller never calibrates such a case, so it
    is the module's own invariant -- raised, not asserted away under `-O`,
    for the executor to map and the audit to report."""

    from evolution.tree.confidence_gate import GateInvariant

    comparison = _comparison(focus=(0,), causal=(1, 2), repeats=3, looks=(3,))
    rows = _rows({"c2": [0.2, 0.5, 0.9]})  # c3 is retained but has no row
    with pytest.raises(GateInvariant, match="^retained case c3 has no usable Parent observation$"):
        _calibrate(comparison, rows)


def test_a_deterministic_parent_gives_a_degenerate_calibration():
    comparison = _comparison(focus=(0,), causal=(1,), repeats=3, looks=(3,))
    record = _calibrate(comparison, _rows({"c2": [1.0, 1.0, 1.0]}))
    assert (record["degenerate"], record["allowance"], record["unavailable"]) == (True, 0.0, None)


# -- Interim and final looks (design §6.5, §6.6, G4, G5, G9; Task 6) ----------

CANDIDATE = {"c1": [0.6, 0.9, 0.8, 1.0], "c2": [0.80, 0.90, 0.85, 0.90]}
PARENT = {"c1": [0.1, 0.2, 0.2, 0.2], "c2": [0.82, 0.88, 0.85, 0.89]}
PREFIXES = {
    "candidate": "measurements/m-r01-a01/events.jsonl#bytes=0:300",
    "parent": "measurements/m-r01-a01-parent/events.jsonl#bytes=0:300",
}


def _look(
    comparison,
    *,
    look,
    candidate=CANDIDATE,
    parent=PARENT,
    result=None,
    excluded=frozenset(),
    draws=1000,
):
    from evolution.tree.confidence_gate import look_record

    return look_record(
        comparison,
        result,
        look=look,
        candidate_rows=candidate if isinstance(candidate, tuple) else _rows(candidate),
        parent_rows=parent if isinstance(parent, tuple) else _rows(parent),
        excluded=excluded,
        groups=GROUPS,
        corpus_ids=CORPUS_IDS,
        primary="score",
        prefixes=PREFIXES,
        campaign_seed=17,
        draws=draws,
    )


def _evidence(cases, look, *, candidate=CANDIDATE, parent=PARENT, excluded=frozenset()):
    from evolution.tree.confidence_gate import case_evidence

    evidence, unobserved = case_evidence(
        _rows(candidate),
        _rows(parent),
        look,
        cases=cases,
        groups=GROUPS,
        primary="score",
        excluded=excluded,
    )
    assert unobserved is None
    return evidence


def _gate_seed(scope, look):
    return derive_seed(
        campaign_seed=17,
        binding="a" * 64,
        scope=scope,
        prefixes=[PREFIXES["candidate"], PREFIXES["parent"]],
        look=look,
        purpose="gate",
    )


def test_an_interim_look_draws_the_focus_scope_over_its_retained_cases():
    record = _look(_comparison(), look=3)  # R = 4, looks (3, 4); F = [c1], C = [c2]
    evidence = _evidence(["c1"], 3)
    expected = interim_focus(
        evidence,
        scope_weights(["c1"], GROUPS),
        confidence=0.9,
        pairing=pairing_of(evidence),
        seed=_gate_seed("focus", 3),
        draws=1000,
    )
    assert record["focus"] == expected.record()
    assert (record["final"], record["causal"], record["overall"]) == (False, None, None)
    assert (record["decision"], record["reason"]) == ("continue", None)
    assert record["focus"]["upper"] > 0
    assert record["caveats"] == ["intervals_empirical"]
    assert (record["round"], record["attempt_id"], record["comparison"], record["look"]) == (
        1,
        "r01-a01",
        7,
        3,
    )
    assert record["prefixes"] == PREFIXES and "evidence" not in record
    assert record["would_pass_point"] is None


def test_a_final_statistical_look_draws_one_joint_stream_over_f_and_c():
    record = _look(_comparison(), look=4)
    evidence = _evidence(["c1", "c2"], 4)
    expected = final_statistical(
        evidence,
        focus=scope_weights(["c1"], GROUPS),
        causal=scope_weights(["c2"], GROUPS),
        overall=corpus_weights(["c1", "c2"], GROUPS, CORPUS_IDS),
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing=pairing_of(evidence),
        seed=_gate_seed("joint", 4),
        draws=1000,
    )
    assert (record["focus"], record["causal"], record["overall"]) == (
        expected.focus.record(),
        expected.causal.record(),
        expected.overall.record(),
    )
    assert record["final"] is True and record["focus"]["upper"] is None
    assert (record["decision"], record["reason"]) == ("accepted", "gate_passed")
    assert record["tolerance"] == {"source": "numeric", "value": 0.05}


def test_the_critical_veto_rejects_first_at_any_look():
    vetoing = tuple(
        _row(
            r.case_id,
            r.repeat,
            r.metrics["score"],
            critical=False if (r.case_id, r.repeat) == ("c1", 1) else None,
        )
        for r in _rows(CANDIDATE)
    )
    record = _look(_comparison(), look=3, candidate=vetoing)
    assert (record["decision"], record["reason"]) == ("rejected", "critical_failure")
    assert record["focus"]["upper"] is not None  # the estimates are still published


def test_the_critical_veto_reads_the_candidate_rows_unexcluded_and_up_to_the_look():
    def vetoing(case_id, repeat):
        return tuple(
            _row(
                r.case_id,
                r.repeat,
                r.metrics["score"],
                critical=False if (r.case_id, r.repeat) == (case_id, repeat) else None,
            )
            for r in _rows(CANDIDATE)
        )

    # c2 is outside the interim focus and (c1, 1) is excluded: the veto still reads both.
    record = _look(_comparison(), look=3, candidate=vetoing("c2", 2))
    assert (record["decision"], record["reason"]) == ("rejected", "critical_failure")
    record = _look(_comparison(), look=3, candidate=vetoing("c1", 1), excluded={("c1", 1)})
    assert (record["decision"], record["reason"]) == ("rejected", "critical_failure")
    # A failure past the look is not yet read.
    record = _look(_comparison(), look=3, candidate=vetoing("c1", 4))
    assert (record["decision"], record["reason"]) == ("continue", None)


def test_an_observe_look_publishes_points_and_would_pass_point_without_a_draw(monkeypatch):
    from evolution.tree import confidence_statistics

    monkeypatch.setattr(confidence_statistics, "_resample", _fail)
    comparison = _comparison(
        mode="observe", repeats=1, looks=(1,), tolerance={"kind": "measured_noise"}
    )
    result = {"seq": 9, "allowance": 0.05, "degenerate": False, "unavailable": None}
    record = _look(comparison, look=1, result=result)
    # Binary floats: the published points are the exact contrasts of 0.6 - 0.1 and 0.80 - 0.82.
    assert record["focus"]["point"] == pytest.approx(0.5)
    assert record["causal"]["point"] == pytest.approx(-0.02)
    assert record["tolerance"] == {"source": "measured_noise", "value": 0.05}
    assert record["would_pass_point"] is True  # 0.5 > 0 and -0.02 >= -0.05
    assert (record["decision"], record["reason"]) == ("accepted", "observe_finalist")
    assert record["caveats"] == [
        "noise_allowance_empirical",
        "observe_gate_not_enforced",
        "uncalibrated_point_estimates",
    ]


def test_a_missing_metric_on_a_read_case_is_returned_not_raised():
    from evolution.tree.campaign_statistics import MissingMetric

    broken = tuple(
        _row(r.case_id, r.repeat, r.metrics["score"])
        if r.case_id != "c1" or r.repeat != 2
        else dataclasses.replace(r, metrics={"other": 1.0})
        for r in _rows(CANDIDATE)
    )
    assert _look(_comparison(), look=3, candidate=broken) == MissingMetric("c1", "02")


def test_an_unscored_owed_pair_raises_coverage_missing():
    from evolution.tree.campaign_statistics import CoverageMissing

    short = tuple(r for r in _rows(CANDIDATE) if (r.case_id, r.repeat) != ("c1", 2))
    with pytest.raises(CoverageMissing) as caught:
        _look(_comparison(), look=3, candidate=short)
    assert caught.value.missing == (("candidate", "c1", 2),)


def _trajectory(stream, repeats):
    """Seeded synthetic Candidate and Parent rows on F = c1 and C = c2."""

    effect = stream.choice([-0.3, 0.0, 0.3, 0.5])
    sides = {}
    for side, shift in (("candidate", effect), ("parent", 0.0)):
        sides[side] = {
            case: [
                min(
                    1.0,
                    max(
                        0.0,
                        round(
                            0.4 + (shift if case == "c1" else 0.0) + stream.uniform(-0.2, 0.2), 2
                        ),
                    ),
                )
                for _ in range(repeats)
            ]
            for case in ("c1", "c2")
        }
    return sides["candidate"], sides["parent"]


def test_sequential_acceptance_is_fixed_final_acceptance_among_the_survivors():
    """Design §12 item 4: over seeded synthetic trajectories, an Attempt the
    executor's schedule accepts is exactly one the final look alone accepts and
    no interim look rejected (F1 note: the identity is F4's to test)."""

    import random

    comparison = _comparison(
        repeats=10, looks=(3, 5, 10), tolerance={"kind": "numeric", "value": 0.1}
    )
    stream = random.Random(4)
    outcomes = []
    for _ in range(24):
        candidate, parent = _trajectory(stream, repeats=10)
        decisions = [
            _look(comparison, look=look, candidate=candidate, parent=parent, draws=500)["decision"]
            for look in (3, 5, 10)
        ]
        sequential = next((d for d in decisions if d != "continue"), "continue")
        fixed = decisions[-1]
        survived = all(d == "continue" for d in decisions[:-1])
        assert (sequential == "accepted") == (fixed == "accepted" and survived)
        outcomes.append(sequential)
    # The seeded stream exercises both branches; if a seed change loses one, pick another seed.
    assert "accepted" in outcomes and "rejected" in outcomes


# -- E1, the maintainer's ruling (a), 2026-10-04 --------------------------------

EARLY_C3 = frozenset({("c3", 1), ("c3", 2), ("c3", 3)})
WITH_C3 = {**CANDIDATE, "c3": [0.7] * 10}, {**PARENT, "c3": [0.3] * 10}


def test_an_unobserved_retained_case_renormalizes_the_interim_point_over_the_observed_ones():
    """E1 (a), 2026-10-04: F = [c1, c3] retained at R = 10; c3's baseline repeats
    1-3 are excluded, so at look 3 c3 is unobserved. The focus point is c1's
    contrast with c1's weight renormalized to 1; no bound; insufficient_repeats."""

    from evolution.tree.confidence_statistics import point_contrast

    comparison = _comparison(focus=(0, 2), causal=(1,), repeats=10, looks=(3, 5, 10))
    candidate, parent = WITH_C3
    record = _look(comparison, look=3, candidate=candidate, parent=parent, excluded=EARLY_C3)
    focus = record["focus"]
    assert (focus["lower"], focus["upper"], focus["unavailable"]) == (
        None,
        None,
        "insufficient_repeats",
    )
    c1 = _evidence(["c1"], 3)
    assert focus["point"] == publish(point_contrast(c1, {"c1": Fraction(1)}))
    assert (record["decision"], record["reason"]) == ("continue", "interval_unavailable")


def test_a_scope_with_no_observed_retained_case_publishes_a_null_point(monkeypatch):
    from evolution.tree import confidence_statistics

    monkeypatch.setattr(confidence_statistics, "_resample", _fail)  # no draw at all
    comparison = _comparison(focus=(2,), causal=(1,), repeats=10, looks=(3, 5, 10))
    candidate, parent = WITH_C3
    record = _look(comparison, look=3, candidate=candidate, parent=parent, excluded=EARLY_C3)
    assert record["focus"] == {
        "point": None,
        "lower": None,
        "upper": None,
        "level": 0.9,
        "unavailable": "insufficient_repeats",
    }
    assert (record["decision"], record["reason"]) == ("continue", "interval_unavailable")


def test_a_final_look_with_an_unobserved_retained_case_is_a_gate_invariant():
    from evolution.tree.confidence_gate import GateInvariant

    comparison = _comparison(focus=(2,), causal=(1,), repeats=3, looks=(3,))
    candidate, parent = WITH_C3
    with pytest.raises(GateInvariant, match="a final look has an unobserved retained case"):
        _look(comparison, look=3, candidate=candidate, parent=parent, excluded=EARLY_C3)


def test_case_evidence_names_the_unobserved_cases_in_corpus_order():
    from evolution.tree.confidence_gate import Unobserved, case_evidence

    candidate, parent = WITH_C3
    evidence, unobserved = case_evidence(
        _rows(candidate),
        _rows(parent),
        3,
        cases=["c1", "c2", "c3"],
        groups=GROUPS,
        primary="score",
        excluded=EARLY_C3 | {("c2", 1), ("c2", 2), ("c2", 3)},
    )
    assert [case.case_id for case in evidence] == ["c1"]
    assert unobserved == Unobserved(("c2", "c3"))


def test_the_renormalized_weights_keep_the_group_rule():
    from evolution.tree.confidence_gate import renormalized

    # Three retained focus cases in two groups (g1: c1, c2; g2: c3); c2 unobserved.
    # scope_weights gives c1 = 1/4, c3 = 1/2; renormalized over {c1, c3}: 1/3 and 2/3.
    assert renormalized({"c1": Fraction(1, 4), "c3": Fraction(1, 2)}) == {
        "c1": Fraction(1, 3),
        "c3": Fraction(2, 3),
    }


# -- look_missing: the rows a family-13 look lacks (Task 7) --------------------


def test_look_missing_names_each_sides_unscored_rows_in_scope_to_the_horizon():
    """Candidate first, then the fresh Parent, each in row order; a row past
    the horizon, outside F ∪ C, or on an excluded pair is not named."""

    from evolution.tree.confidence_gate import look_missing

    comparison = _comparison(focus=(0,), causal=(1,), repeats=1, looks=(1,))
    candidate = (
        _row("c1", 1, None, disposition="indeterminate"),
        _row("c2", 1, 0.5),
        _row("c3", 1, None, disposition="indeterminate"),  # outside F ∪ C
    )
    parent = (
        _row("c1", 1, None, disposition="not_started"),
        _row("c2", 1, None, disposition="indeterminate"),  # excluded below
        _row("c2", 2, None, disposition="indeterminate"),  # the `case_repeats` row
        _row("c2", 3, None, disposition="indeterminate"),  # past the horizon
    )
    sides = {"m-r01-a01": candidate, "m-r01-a01-parent": parent}
    missing = look_missing(
        comparison, sides, corpus_ids=CORPUS_IDS, excluded=frozenset({("c2", 1)}), horizon=2
    )
    assert missing == [
        {
            "measurement_id": "m-r01-a01",
            "case_id": "c1",
            "sample_id": "01",
            "state": "indeterminate",
        },
        {
            "measurement_id": "m-r01-a01-parent",
            "case_id": "c1",
            "sample_id": "01",
            "state": "not_started",
        },
        {
            "measurement_id": "m-r01-a01-parent",
            "case_id": "c2",
            "sample_id": "02",
            "state": "indeterminate",
        },
    ]
    assert look_missing(comparison, sides, corpus_ids=CORPUS_IDS, excluded=frozenset(), horizon=1)[
        -1
    ] == {
        "measurement_id": "m-r01-a01-parent",
        "case_id": "c2",
        "sample_id": "01",
        "state": "indeterminate",
    }


def test_look_missing_is_empty_when_every_row_in_scope_scored():
    from evolution.tree.confidence_gate import look_missing

    comparison = _comparison()
    sides = {"m-r01-a01": _rows(CANDIDATE), "m-r01-a01-parent": _rows(PARENT)}
    assert (
        look_missing(comparison, sides, corpus_ids=CORPUS_IDS, excluded=frozenset(), horizon=4)
        == []
    )


def test_gate_inputs_reads_the_bound_comparison():
    comparison = {"looks": [3, 5], "retained": {"focus": [0], "causal": [1]}, "repeats": 5}
    excluded = {("c1", 2), ("c1", 4)}
    interim = gate_inputs(comparison, max_repeat=5, look=3, excluded=excluded)
    assert (interim.looks, interim.final, interim.horizon) == ((3, 5), False, 3)
    assert interim.mask == frozenset({("c1", 2)})
    final = gate_inputs(
        comparison, max_repeat=6, look=5, excluded=excluded
    )  # a case_repeats Parent
    assert (final.final, final.horizon) == (True, 6)
    assert final.retained == {"focus": (0,), "causal": (1,)}
    assert final.mask == frozenset(excluded)


def test_gate_inputs_refuses_a_look_the_comparison_does_not_bind():
    with pytest.raises(GateInvariant):
        gate_inputs(
            {"looks": [3, 5], "retained": {"focus": [], "causal": []}, "repeats": 5},
            max_repeat=5,
            look=4,
            excluded=(),
        )
