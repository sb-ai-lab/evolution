"""Family 13 kernel properties at the method's draw count (frozen design §10 items 3 to 5)."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from evolution.tree import confidence_statistics as cs
from evolution.tree.statistics import ScoreSample, _draw

pytestmark = pytest.mark.slow

#: Replay vector for `test_a_20000_draw_calibration_replays_bit_for_bit`, computed
#: once from an independent restatement of the plan's draw order. Never regenerate.
NOISE_SEED = 284961804244779531191751784669472600138
NOISE_DIGEST = "9c37f22f4a467377d7933356e342013a52ec4e7f1202d90c5997aa46f48686f9"
NOISE_ALLOWANCE = Fraction(3, 20)


def _sample(case_id: str, repeat: int, score: float) -> ScoreSample:
    return ScoreSample(case_id, str(repeat), score, None, None, "boot", "deps", f"g-{case_id}", "s")


def _case(case_id, candidate, parent) -> cs.CaseEvidence:
    return cs.CaseEvidence(
        case_id,
        tuple(_sample(case_id, r, s) for r, s in enumerate(candidate, start=1)),
        tuple(_sample(case_id, r, s) for r, s in enumerate(parent, start=1)),
    )


def _own_groups(ids) -> dict[str, tuple[str, str]]:
    return {case_id: (f"g-{case_id}", "s") for case_id in ids}


def _noise_fixture() -> tuple[tuple[cs.NoiseCase, ...], dict[str, Fraction]]:
    source = random.Random(2026)
    cases = tuple(
        cs.NoiseCase(
            f"c{i}",
            tuple(_sample(f"c{i}", r, _draw(source, 17) / 16) for r in range(1, 6)),
            5,
            5,
        )
        for i in range(8)
    )
    ids = [case.case_id for case in cases]
    return cases, cs.scope_weights(ids, _own_groups(ids))


FOCUS = ("f1", "f2", "f3", "f4")
CAUSAL = ("c1", "c2", "c3")
GROUPS = {case_id: (f"g{i % 3}", "s") for i, case_id in enumerate(FOCUS + CAUSAL)}
R = 10
EFFECTS = ((0.0, 0.0), (0.35, 0.0), (0.35, -0.25), (-0.35, 0.0))
PROPERTY_DRAWS = 1000  # the property does not depend on the draw count
BINDING = "ab" * 32


def _trajectory(k: int) -> dict[str, tuple[list[float], list[float]]]:
    stream = random.Random(1000 + k)
    focus_effect, causal_effect = EFFECTS[k % len(EFFECTS)]
    rows = {}
    for i, case_id in enumerate(FOCUS + CAUSAL):
        base = 0.25 + 0.1 * (i % 4)
        effect = focus_effect if case_id in FOCUS else causal_effect
        candidate = min(1.0, max(0.0, base + effect))
        rows[case_id] = (
            [float(stream.random() < candidate) for _ in range(R)],
            [float(stream.random() < base) for _ in range(R)],
        )
    return rows


def _cases(rows, ids, look) -> tuple[cs.CaseEvidence, ...]:
    return tuple(
        _case(case_id, rows[case_id][0][:look], rows[case_id][1][:look]) for case_id in ids
    )


def _seed(k: int, look: int, scope: str) -> int:
    return cs.derive_seed(
        campaign_seed=k,
        binding=BINDING,
        scope=scope,
        prefixes=(f"t{k}/candidate#{look}", f"t{k}/parent#{look}"),
        look=look,
        purpose="gate",
    )


def _final(rows, k) -> cs.Decision:
    estimates = cs.final_statistical(
        _cases(rows, FOCUS + CAUSAL, R),
        focus=cs.scope_weights(FOCUS, GROUPS),
        causal=cs.scope_weights(CAUSAL, GROUPS),
        overall=cs.corpus_weights(FOCUS + CAUSAL, GROUPS, FOCUS + CAUSAL),
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="unpaired",
        seed=_seed(k, R, "joint"),
        draws=PROPERTY_DRAWS,
    )
    return cs.decide_final_statistical(
        estimates.focus, estimates.causal, tolerance=0.2, critical=False
    )


def test_the_noise_allowance_tail_is_numerically_stable_across_seeds() -> None:
    cases, weights = _noise_fixture()
    allowances = sorted(
        cs.noise_calibration(cases, weights, seed=seed).allowance for seed in range(5)
    )
    assert allowances[0] is not None and allowances[0] > 0
    assert allowances[-1] - allowances[0] <= allowances[2] / 10


def test_a_20000_draw_calibration_replays_bit_for_bit() -> None:
    cases, weights = _noise_fixture()
    first = cs.noise_calibration(cases, weights, seed=NOISE_SEED)
    assert first == cs.noise_calibration(cases, weights, seed=NOISE_SEED)
    assert first.draws is not None and len(first.draws.numerators) == cs.DRAWS
    assert first.allowance == NOISE_ALLOWANCE
    assert cs.noise_result_digest(first.draws) == NOISE_DIGEST
