"""Family 13's pure statistics kernel (implementation design §§4, 6; ADR-0027)."""

from __future__ import annotations

import ast
import hashlib
import json
import random
from fractions import Fraction
from pathlib import Path

import pytest

from evolution.tree import confidence_statistics as cs
from evolution.tree.statistics import ScoreSample, _draw, group_weighted_score


def test_method_object_pins_the_versioned_constants() -> None:
    assert cs.method_object() == {
        "version": "f13-bootstrap/1",
        "draws": 20000,
        "quantile": "linear",
        "noise_level": 0.995,
    }
    assert cs.DRAWS == 20_000
    assert cs.NOISE_LEVEL == Fraction(995, 1000)


@pytest.mark.parametrize(
    ("mode", "repeats", "looks"),
    [
        ("statistical", 2, (2,)),
        ("statistical", 3, (3,)),
        ("statistical", 4, (3, 4)),
        ("statistical", 6, (3, 5, 6)),
        ("statistical", 10, (3, 5, 10)),
        ("statistical", 11, (3, 5, 10, 11)),
        ("statistical", 15, (3, 5, 10, 15)),
        ("observe", 1, (1,)),
        ("observe", 10, (10,)),
    ],
)
def test_looks_are_the_fixed_stages_below_r_then_r(mode, repeats, looks) -> None:
    assert cs.confidence_looks(mode, repeats) == looks


@pytest.mark.parametrize(
    ("mode", "repeats"),
    [
        ("statistical", 1),
        ("statistical", 0),
        ("observe", 0),
        ("statistical", True),
        ("observe", 2.0),
        ("smoke", 3),
    ],
)
def test_looks_refuse_a_count_the_mode_does_not_admit(mode, repeats) -> None:
    with pytest.raises(ValueError):
        cs.confidence_looks(mode, repeats)


def test_confidence_is_read_from_its_published_spelling() -> None:
    assert cs.confidence_fraction(0.9) == Fraction(9, 10)
    assert cs.confidence_fraction(0.995) == Fraction(995, 1000)


@pytest.mark.parametrize(
    "bad", [0, 1, 0.0, 1.0, 90, -0.1, True, float("nan"), float("inf"), "0.9", None]
)
def test_confidence_refuses_a_value_outside_the_open_unit_interval(bad) -> None:
    with pytest.raises(ValueError):
        cs.confidence_fraction(bad)


def test_publish_is_the_nearest_float_and_decisions_read_its_shortest_spelling() -> None:
    assert cs.publish(Fraction(1, 10)) == 0.1
    assert cs.published_fraction(0.1) == Fraction(1, 10)
    assert cs.published_fraction(0.1) != Fraction(0.1)
    assert cs.published_fraction(0) == 0
    for bad in (True, float("nan"), "0.1", None):
        with pytest.raises(ValueError):
            cs.published_fraction(bad)
    with pytest.raises(ValueError):
        cs.publish(0.1)  # only exact values are published


def test_linear_quantile_matches_the_specified_interpolation() -> None:
    draws = cs.Draws.of([Fraction(v) for v in (4, 0, 3, 1, 2)])
    assert draws.numerators == (0, 1, 2, 3, 4) and draws.denominator == 1
    assert cs.linear_quantile(draws, Fraction(0)) == 0
    assert cs.linear_quantile(draws, Fraction(1)) == 4
    assert cs.linear_quantile(draws, Fraction(1, 10)) == Fraction(2, 5)  # h = 0.4
    assert cs.linear_quantile(draws, Fraction(7, 8)) == Fraction(7, 2)  # h = 3.5


def test_draws_keep_exact_rationals_over_one_denominator() -> None:
    draws = cs.Draws.of([Fraction(1, 3), Fraction(-1, 6), Fraction(1, 2)])
    assert (draws.numerators, draws.denominator) == ((-1, 2, 3), 6)
    assert cs.linear_quantile(draws, Fraction(1, 4)) == Fraction(1, 12)  # h = 0.5


def test_one_sided_lower_uses_quantile_one_minus_c_and_two_sided_equal_tails() -> None:
    draws = cs.Draws.of([Fraction(v) for v in range(101)])  # x[i] = i, so h = 100 q
    assert cs.one_sided_lower(draws, Fraction(9, 10)) == 10
    assert cs.two_sided(draws, Fraction(9, 10)) == (5, 95)
    assert cs.one_sided_lower(draws, Fraction(995, 1000)) == Fraction(1, 2)


def test_degenerate_means_every_draw_equal_exactly() -> None:
    assert cs.Draws.of([Fraction(1, 3)] * 4).degenerate
    assert not cs.Draws.of([Fraction(1, 3), Fraction(1, 3) + Fraction(1, 10**30)]).degenerate


@pytest.mark.parametrize("q", [Fraction(-1, 10), Fraction(11, 10), 0.5])
def test_quantile_refuses_a_level_outside_zero_to_one_or_a_float(q) -> None:
    with pytest.raises(ValueError):
        cs.linear_quantile(cs.Draws.of([Fraction(0), Fraction(1)]), q)


@pytest.mark.parametrize(("numerators", "denominator"), [((), 1), ((2, 1), 1), ((1,), 0)])
def test_draws_refuse_empty_unsorted_or_a_bad_denominator(numerators, denominator) -> None:
    with pytest.raises(ValueError):
        cs.Draws(numerators, denominator)


_BINDING = "ab" * 32
#: Replay vectors (frozen design §9.2). Computed once from an independent
#: `json.dumps` + `hashlib` derivation; never regenerate them to make a test pass.
SEED_VECTOR = 284961804244779531191751784669472600138
DRAW_VECTOR = [6, 7, 5, 4, 5]


def _independent_seed(**payload) -> int:
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(body).digest()[:16], "big")


def _seed_args(**changes):
    args = dict(
        campaign_seed=17,
        binding=_BINDING,
        scope="focus",
        prefixes=(
            "measurements/m-r01-a01/events.jsonl#bytes=0:120",
            "measurements/m-r01-a01-parent/events.jsonl#bytes=0:96",
        ),
        look=3,
        purpose="gate",
    )
    return {**args, **changes}


def test_seed_is_the_first_128_bits_of_the_sha256_of_its_canonical_inputs() -> None:
    args = _seed_args()
    seed = cs.derive_seed(**args)
    assert seed == _independent_seed(
        method="f13-bootstrap/1",
        campaign_seed=17,
        binding=_BINDING,
        scope="focus",
        prefixes=list(args["prefixes"]),
        look=3,
        purpose="gate",
    )
    assert 0 <= seed < 2**128


def test_the_seed_and_its_stream_replay_bit_for_bit() -> None:
    seed = cs.derive_seed(
        campaign_seed=0,
        binding="0" * 64,
        scope="noise",
        prefixes=("measurements/m-r01-baseline/events.jsonl#bytes=0:1",),
        look=None,
        purpose="noise",
    )
    assert seed == SEED_VECTOR
    stream = random.Random(SEED_VECTOR)
    assert [_draw(stream, 10) for _ in range(5)] == DRAW_VECTOR


@pytest.mark.parametrize(
    "changes",
    [
        {"campaign_seed": 18},
        {"binding": "cd" * 32},
        {"prefixes": tuple(reversed(_seed_args()["prefixes"]))},
        {"look": 5},
        {"scope": "joint"},
    ],
)
def test_every_input_changes_the_seed(changes) -> None:
    assert cs.derive_seed(**_seed_args(**changes)) != cs.derive_seed(**_seed_args())


@pytest.mark.parametrize(
    ("scope", "purpose", "look"),
    [
        ("focus", "noise", 3),
        ("joint", "global", 3),
        ("noise", "noise", 3),
        ("global", "global", 1),
        ("focus", "gate", None),
        ("focus", "gate", 0),
        ("diagnostic", "gate", 3),
    ],
)
def test_scope_purpose_and_look_must_agree(scope, purpose, look) -> None:
    with pytest.raises(ValueError):
        cs.derive_seed(**_seed_args(scope=scope, purpose=purpose, look=look))


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": "xyz"},
        {"binding": "AB" * 32},
        {"prefixes": ()},
        {"prefixes": ("",)},
        {"prefixes": "measurements/m/events.jsonl#bytes=0:1"},
        {"campaign_seed": True},
    ],
)
def test_the_seed_refuses_a_malformed_input(changes) -> None:
    with pytest.raises(ValueError):
        cs.derive_seed(**_seed_args(**changes))


def test_binding_digest_is_over_the_payload_without_evidence() -> None:
    payload = {"round": 1, "attempt_id": "r01-a01", "evidence": {"x": 1}}
    assert (
        cs.binding_digest(payload)
        == hashlib.sha256(b'{"attempt_id":"r01-a01","round":1}').hexdigest()
    )
    assert cs.binding_digest(payload) == cs.binding_digest({**payload, "evidence": {"y": 2}})


def test_the_kernel_imports_no_campaign_or_family_module() -> None:
    """Plan R2: the kernel's only project imports are the gate kernel and the canonical encoder."""

    tree = ast.parse(Path(cs.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert {name for name in imported if name.startswith("evolution")} == {
        "evolution.tree.statistics",
        "evolution.tree.execution.journal_identity",
    }


def _sample(case_id, repeat, score, group=None, *, seed=None, deps="deps") -> ScoreSample:
    return ScoreSample(
        case_id=case_id,
        sample_id=str(repeat),
        score=score,
        requested_seed=seed,
        effective_seed=seed,
        boot_id="boot",
        dependency_digest=deps,
        group_id=group or f"g-{case_id}",
        stratum="s",
    )


def _case(case_id, candidate, parent, group=None, *, paired=False) -> cs.CaseEvidence:
    def side(scores):
        return tuple(
            _sample(case_id, repeat, score, group, seed=repeat if paired else None)
            for repeat, score in enumerate(scores, start=1)
        )

    return cs.CaseEvidence(case_id, side(candidate), side(parent))


def _groups(cases) -> dict[str, tuple[str, str]]:
    return {case.case_id: (case.candidate[0].group_id, "s") for case in cases}


def test_scope_weights_are_equal_groups_then_equal_cases() -> None:
    groups = {"a": ("g1", "s"), "b": ("g1", "s"), "c": ("g2", "s")}
    assert cs.scope_weights(("a", "b", "c"), groups) == {
        "a": Fraction(1, 4),
        "b": Fraction(1, 4),
        "c": Fraction(1, 2),
    }
    assert cs.scope_weights(("a", "c"), groups) == {"a": Fraction(1, 2), "c": Fraction(1, 2)}


def test_corpus_weights_divide_by_the_full_group_size() -> None:
    groups = {c: (g, "s") for c, g in zip("abcde", ("g1", "g1", "g2", "g2", "g3"), strict=True)}
    assert cs.corpus_weights(("a", "c"), groups, corpus=tuple("abcde")) == {
        "a": Fraction(1, 6),
        "c": Fraction(1, 6),
    }


@pytest.mark.parametrize("ids", [(), ("a", "a"), ("a", "z")])
def test_weights_refuse_an_empty_repeated_or_foreign_case_set(ids) -> None:
    groups = {"a": ("g1", "s"), "b": ("g1", "s")}
    with pytest.raises(ValueError):
        cs.scope_weights(ids, groups)
    with pytest.raises(ValueError):
        cs.corpus_weights(ids, groups, corpus=("a", "b"))


def test_point_contrast_equals_the_group_weighted_score_difference() -> None:
    cases = (
        _case("a", [1.0, 0.5, 0.25], [0.0, 0.5], "g1"),
        _case("b", [0.75], [0.25, 0.25, 1.0], "g1"),
        _case("c", [0.5, 0.5], [1.0, 0.0], "g2"),
    )
    weights = cs.scope_weights(("a", "b", "c"), _groups(cases))
    candidate = [s for case in cases for s in case.candidate]
    parent = [s for case in cases for s in case.parent]
    expected = group_weighted_score(candidate, 3, excluded_repeats=True) - group_weighted_score(
        parent, 3, excluded_repeats=True
    )
    assert float(cs.point_contrast(cases, weights)) == pytest.approx(expected, abs=1e-15)


def _replay(cases, weights, seed, draws, *, paired=False) -> tuple[Fraction, ...]:
    """The documented draw order (plan R4), restated independently of the kernel."""

    stream = random.Random(seed)
    values = []
    for _ in range(draws):
        total = Fraction(0)
        for case in cases:
            candidate = [Fraction(s.score) for s in case.candidate]
            parent = [Fraction(s.score) for s in case.parent]
            if paired:
                deltas = [c - p for c, p in zip(candidate, parent, strict=True)]
                mean = sum(deltas[_draw(stream, len(deltas))] for _ in deltas) / len(deltas)
            else:
                c = sum(candidate[_draw(stream, len(candidate))] for _ in candidate) / len(
                    candidate
                )
                p = sum(parent[_draw(stream, len(parent))] for _ in parent) / len(parent)
                mean = c - p
            total += weights[case.case_id] * mean
        values.append(total)
    return tuple(values)


def test_unpaired_replicates_follow_the_documented_draw_order() -> None:
    cases = (_case("a", [0.25, 0.75, 0.5], [0.0, 1.0]), _case("b", [1.0], [0.5, 0.25]))
    weights = {"a": Fraction(1, 2), "b": Fraction(1, 2)}
    replicates = cs.joint_replicates(cases, {"all": weights}, pairing="unpaired", seed=7, draws=4)
    assert replicates.values("all") == _replay(cases, weights, 7, 4)


def test_exact_replicates_resample_within_case_differences_in_repeat_order() -> None:
    cases = (
        _case("a", [0.25, 0.75, 0.5], [0.0, 1.0, 0.5], paired=True),
        _case("b", [1.0, 0.5], [0.5, 0.25], paired=True),
    )
    weights = {"a": Fraction(1, 2), "b": Fraction(1, 2)}
    shuffled = tuple(
        cs.CaseEvidence(c.case_id, tuple(reversed(c.candidate)), c.parent) for c in cases
    )
    expected = _replay(cases, weights, 9, 4, paired=True)
    for given in (cases, shuffled):
        replicates = cs.joint_replicates(given, {"all": weights}, pairing="exact", seed=9, draws=4)
        assert replicates.values("all") == expected


def test_exact_pairs_match_by_sample_id_when_sort_keys_tie() -> None:
    def obs(sample_id, score, seed):
        return ScoreSample(
            case_id="a",
            sample_id=sample_id,
            score=score,
            requested_seed=seed,
            effective_seed=seed,
            boot_id="boot",
            dependency_digest="deps",
            group_id="g-a",
            stratum="s",
        )

    parent = (obs("1", 0, 1), obs("01", 1, 2))
    candidate = (obs("01", 0, 2), obs("1", 1, 1))  # differences: "1" -> +1, "01" -> -1
    weights = {"all": {"a": Fraction(1)}}

    def run(cand, par):
        case = cs.CaseEvidence("a", cand, par)
        assert cs.pairing_of((case,)) == "exact"
        return cs.joint_replicates((case,), weights, pairing="exact", seed=3, draws=64).values(
            "all"
        )

    base = run(candidate, parent)
    assert set(base) == {Fraction(-1), Fraction(0), Fraction(1)}
    assert len(set(base)) > 1
    assert run(candidate[::-1], parent) == base
    assert run(candidate, parent[::-1]) == base
    assert run(candidate[::-1], parent[::-1]) == base


@pytest.mark.parametrize(
    "parent",
    [
        (_sample("a", 1, 0.5), _sample("a", 2, 0.5)),  # no requested seed
        (_sample("a", 1, 0.5, seed=1, deps="other"), _sample("a", 2, 0.5, seed=2, deps="other")),
        (_sample("a", 1, 0.5, seed=1), _sample("a", 3, 0.5, seed=3)),  # another repeat
    ],
)
def test_exact_pairing_refuses_without_the_provenance_proof(parent) -> None:
    candidate = (_sample("a", 1, 1.0, seed=1), _sample("a", 2, 1.0, seed=2))
    case = cs.CaseEvidence("a", candidate, parent)
    assert cs.pairing_of((case,)) == "unpaired"
    with pytest.raises(ValueError):
        cs.joint_replicates((case,), {"all": {"a": Fraction(1)}}, pairing="exact", seed=1, draws=2)


def test_pairing_of_is_exact_only_when_every_case_proves() -> None:
    proved = _case("a", [1.0, 0.5], [0.5, 0.5], paired=True)
    unproved = _case("b", [1.0, 0.5], [0.5, 0.5])
    assert cs.pairing_of((proved,)) == "exact"
    assert cs.pairing_of((proved, unproved)) == "unpaired"


def test_cases_and_groups_are_never_resampled() -> None:
    cases = (_case("a", [1.0, 1.0], [0.0, 0.0], "g1"), _case("b", [0.5, 0.5], [0.5, 0.5], "g2"))
    weights = cs.scope_weights(("a", "b"), _groups(cases))
    draws = cs.joint_replicates(
        cases, {"all": weights}, pairing="unpaired", seed=3, draws=50
    ).draws("all")
    assert draws.degenerate
    assert cs.linear_quantile(draws, Fraction(1, 2)) == Fraction(1, 2)


def test_projections_are_read_from_one_replicate() -> None:
    cases = (_case("f", [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]), _case("c", [0.5, 1.0], [0.0, 0.5, 1.0]))
    replicates = cs.joint_replicates(
        cases,
        {
            "focus": {"f": Fraction(1)},
            "causal": {"c": Fraction(1)},
            "overall": {"f": Fraction(1, 2), "c": Fraction(1, 2)},
        },
        pairing="unpaired",
        seed=11,
        draws=200,
    )
    triples = zip(
        replicates.values("focus"),
        replicates.values("causal"),
        replicates.values("overall"),
        strict=True,
    )
    assert all(overall == (focus + causal) / 2 for focus, causal, overall in triples)


def test_a_replicate_is_a_function_of_its_seed_alone() -> None:
    cases = (_case("a", [0.25, 0.75, 0.5], [0.0, 1.0]),)
    projection = {"all": {"a": Fraction(1)}}
    first = cs.joint_replicates(cases, projection, pairing="unpaired", seed=5, draws=30)
    assert first == cs.joint_replicates(cases, projection, pairing="unpaired", seed=5, draws=30)
    assert first != cs.joint_replicates(cases, projection, pairing="unpaired", seed=6, draws=30)


def test_joint_replicates_refuse_malformed_inputs() -> None:
    case = _case("a", [0.5, 1.0], [0.0, 0.5])
    good = {"all": {"a": Fraction(1)}}
    bad_calls = [
        ((), good, "unpaired", 1),
        ((case, case), good, "unpaired", 1),
        ((case,), {}, "unpaired", 1),
        ((case,), {"all": {}}, "unpaired", 1),
        ((case,), {"all": {"z": Fraction(1)}}, "unpaired", 1),
        ((case,), {"all": {"a": Fraction(0)}}, "unpaired", 1),
        ((case,), good, "none", 1),
        ((case,), good, "unpaired", 0),
    ]
    for cases, projections, pairing, draws in bad_calls:
        with pytest.raises(ValueError):
            cs.joint_replicates(cases, projections, pairing=pairing, seed=1, draws=draws)
    with pytest.raises(ValueError):
        cs.CaseEvidence("a", (), case.parent)
    with pytest.raises(ValueError):
        cs.CaseEvidence("a", (_sample("b", 1, 0.5),), case.parent)


def _noise_case(case_id, scores, first, second) -> cs.NoiseCase:
    return cs.NoiseCase(
        case_id,
        tuple(_sample(case_id, repeat, score) for repeat, score in enumerate(scores, start=1)),
        first,
        second,
    )


def _noise_replay(cases, weights, seed, draws) -> tuple[Fraction, ...]:
    stream = random.Random(seed)
    values = []
    for _ in range(draws):
        total = Fraction(0)
        for case in cases:
            pool = [Fraction(s.score) for s in case.parent]
            first = sum(pool[_draw(stream, len(pool))] for _ in range(case.first)) / case.first
            second = sum(pool[_draw(stream, len(pool))] for _ in range(case.second)) / case.second
            total += weights[case.case_id] * (first - second)
        values.append(total)
    return tuple(sorted(values))


def test_noise_follows_the_documented_draw_order_with_per_side_counts() -> None:
    cases = (_noise_case("a", [0.0, 0.5, 1.0], 3, 2), _noise_case("b", [0.25, 0.75], 2, 2))
    weights = {"a": Fraction(1, 2), "b": Fraction(1, 2)}
    result = cs.noise_calibration(cases, weights, seed=4, draws=6)
    assert result.draws is not None
    assert tuple(
        Fraction(n, result.draws.denominator) for n in result.draws.numerators
    ) == _noise_replay(cases, weights, 4, 6)


def test_observe_r1_draws_two_one_observation_measurements() -> None:
    cases = (_noise_case("a", [0.0, 1.0], 1, 1),)
    result = cs.noise_calibration(cases, {"a": Fraction(1)}, seed=8, draws=40)
    assert result.draws is not None
    assert set(result.draws.numerators) <= {-result.draws.denominator, 0, result.draws.denominator}
    assert result.unavailable is None


def test_noise_allowance_is_the_negated_half_percent_quantile_floored_at_zero() -> None:
    assert cs.noise_allowance(cs.Draws.of([Fraction(-2), Fraction(0), Fraction(1)])) == Fraction(
        99, 50
    )
    assert cs.noise_allowance(cs.Draws.of([Fraction(1), Fraction(2), Fraction(3)])) == 0
    cases = (_noise_case("a", [0.0, 0.5, 1.0], 3, 3), _noise_case("b", [0.25, 0.75, 1.0], 3, 3))
    result = cs.noise_calibration(
        cases, {"a": Fraction(1, 2), "b": Fraction(1, 2)}, seed=5, draws=400
    )
    assert result.draws is not None
    assert result.allowance == max(Fraction(0), -cs.linear_quantile(result.draws, Fraction(1, 200)))
    assert (result.degenerate, result.unavailable) == (False, None)


def test_a_constant_parent_is_degenerate() -> None:
    result = cs.noise_calibration(
        (_noise_case("a", [1.0, 1.0], 2, 2),), {"a": Fraction(1)}, seed=1, draws=50
    )
    assert (result.allowance, result.degenerate, result.unavailable) == (0, True, None)


def test_a_case_with_fewer_than_two_parent_observations_is_unavailable() -> None:
    cases = (_noise_case("a", [0.5, 1.0], 2, 2), _noise_case("b", [0.5], 2, 2))
    result = cs.noise_calibration(
        cases, {"a": Fraction(1, 2), "b": Fraction(1, 2)}, seed=1, draws=50
    )
    assert result == cs.NoiseResult(None, False, "insufficient_calibration_repeats", None)


def test_noise_result_digest_is_over_the_published_sorted_draws() -> None:
    draws = cs.Draws.of([Fraction(1, 3), Fraction(-1, 4)])
    assert (
        cs.noise_result_digest(draws) == hashlib.sha256(b"[-0.25,0.3333333333333333]").hexdigest()
    )


def test_noise_refuses_bad_sizes_or_weights() -> None:
    with pytest.raises(ValueError):
        _noise_case("a", [0.5, 1.0], 0, 2)
    with pytest.raises(ValueError):
        _noise_case("a", [0.5, 1.0], 2, True)
    case = _noise_case("a", [0.5, 1.0], 2, 2)
    for weights in ({}, {"a": Fraction(1), "b": Fraction(1)}, {"a": Fraction(-1)}):
        with pytest.raises(ValueError):
            cs.noise_calibration((case,), weights, seed=1, draws=2)


FOCUS_CASES = (
    _case("f1", [1.0, 0.5, 1.0, 0.75], [0.5, 0.25, 0.5, 0.0]),
    _case("f2", [0.75, 1.0, 0.5, 1.0], [0.5, 0.5, 0.75, 0.25]),
)
CAUSAL_CASES = (
    _case("c1", [0.5, 0.25, 0.5, 0.75], [0.5, 0.5, 0.25, 0.75]),
    _case("c2", [1.0, 0.75, 0.75, 1.0], [1.0, 1.0, 0.5, 0.75]),
)
ALL = FOCUS_CASES + CAUSAL_CASES
GROUPS = {
    "f1": ("g1", "s"),
    "f2": ("g2", "s"),
    "c1": ("g1", "s"),
    "c2": ("g3", "s"),
    "u1": ("g3", "s"),
}
CORPUS_IDS = ("f1", "f2", "c1", "c2", "u1")
FW = cs.scope_weights(("f1", "f2"), GROUPS)
CW = cs.scope_weights(("c1", "c2"), GROUPS)
OW = cs.corpus_weights(("f1", "f2", "c1", "c2"), GROUPS, CORPUS_IDS)


def test_an_interim_focus_interval_is_two_sided_at_the_focus_confidence() -> None:
    estimate = cs.interim_focus(
        FOCUS_CASES, FW, confidence=0.9, pairing="unpaired", seed=21, draws=400
    )
    draws = cs.joint_replicates(
        FOCUS_CASES, {"focus": FW}, pairing="unpaired", seed=21, draws=400
    ).draws("focus")
    lower, upper = cs.two_sided(draws, Fraction(9, 10))
    assert estimate == cs.Estimate(
        cs.publish(cs.point_contrast(FOCUS_CASES, FW)),
        cs.publish(lower),
        cs.publish(upper),
        0.9,
        None,
    )
    assert estimate.record() == {
        "point": estimate.point,
        "lower": estimate.lower,
        "upper": estimate.upper,
        "level": 0.9,
        "unavailable": None,
    }


def test_an_interim_case_with_one_observation_makes_the_interval_unavailable() -> None:
    cases = (FOCUS_CASES[0], _case("f2", [1.0], [0.5, 0.25]))
    estimate = cs.interim_focus(cases, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    assert (estimate.lower, estimate.upper, estimate.unavailable) == (
        None,
        None,
        "insufficient_repeats",
    )
    assert estimate.point == cs.publish(cs.point_contrast(cases, FW))


def test_a_zero_width_interim_interval_keeps_its_bounds_and_is_marked_degenerate() -> None:
    cases = (_case("f1", [1.0, 1.0], [0.5, 0.5]), _case("f2", [0.5, 0.5], [0.5, 0.5]))
    estimate = cs.interim_focus(cases, FW, confidence=0.9, pairing="unpaired", seed=1, draws=50)
    assert estimate.lower == estimate.upper == estimate.point == 0.25
    assert estimate.unavailable == "degenerate_interval"


def test_the_final_look_reads_focus_causal_and_overall_from_one_joint_stream() -> None:
    final = cs.final_statistical(
        ALL,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.8,
        pairing="unpaired",
        seed=33,
        draws=300,
    )
    replicates = cs.joint_replicates(
        ALL, {"focus": FW, "causal": CW, "overall": OW}, pairing="unpaired", seed=33, draws=300
    )
    assert final.focus.lower == cs.publish(
        cs.one_sided_lower(replicates.draws("focus"), Fraction(9, 10))
    )
    assert final.causal is not None
    assert final.causal.lower == cs.publish(
        cs.one_sided_lower(replicates.draws("causal"), Fraction(4, 5))
    )
    assert (final.focus.upper, final.causal.upper, final.overall.upper) == (None, None, None)
    assert (final.focus.level, final.causal.level, final.overall.level) == (0.9, 0.8, 0.9)
    assert final.overall.lower == cs.publish(
        cs.one_sided_lower(replicates.draws("overall"), Fraction(9, 10))
    )
    assert final.overall.point == cs.publish(cs.point_contrast(ALL, OW))


def test_the_overall_level_is_the_larger_confidence_or_the_focus_one_when_causal_is_empty() -> None:
    assert cs.global_level(0.8, 0.95, causal_empty=False) == 0.95
    assert cs.global_level(0.9, 0.8, causal_empty=False) == 0.9
    assert cs.global_level(0.9, 0.95, causal_empty=True) == 0.9
    final = cs.final_statistical(
        FOCUS_CASES,
        focus=FW,
        causal=None,
        overall=cs.corpus_weights(("f1", "f2"), GROUPS, CORPUS_IDS),
        focus_confidence=0.9,
        causal_confidence=0.95,
        pairing="unpaired",
        seed=3,
        draws=100,
    )
    assert final.causal is None and final.overall.level == 0.9


def test_a_short_causal_case_leaves_only_the_focus_bound_available() -> None:
    cases = FOCUS_CASES + (CAUSAL_CASES[0], _case("c2", [1.0], [1.0, 0.5]))
    final = cs.final_statistical(
        cases,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="unpaired",
        seed=3,
        draws=100,
    )
    assert final.causal is not None
    assert final.focus.lower is not None and final.focus.unavailable in (
        None,
        "degenerate_interval",
    )
    for estimate in (final.causal, final.overall):
        assert (estimate.lower, estimate.unavailable) == (None, "insufficient_repeats")


def test_the_focus_bound_reads_the_one_stream_over_every_case_not_only_the_available_ones() -> None:
    cases = FOCUS_CASES + (CAUSAL_CASES[0], _case("c2", [1.0], [1.0, 0.5]))
    kwargs = {"pairing": "unpaired", "seed": 2, "draws": 100}
    full = cs.joint_replicates(cases, {"focus": FW}, **kwargs).draws("focus")
    only_f = cs.joint_replicates(FOCUS_CASES, {"focus": FW}, **kwargs).draws("focus")
    expected = cs.publish(cs.one_sided_lower(full, cs.confidence_fraction(0.9)))
    restricted = cs.publish(cs.one_sided_lower(only_f, cs.confidence_fraction(0.9)))
    assert expected == 0.3125 and restricted == 0.28125  # the seed tells the streams apart
    final = cs.final_statistical(
        cases,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="unpaired",
        seed=2,
        draws=100,
    )
    assert final.focus.lower == expected


def test_no_draw_is_made_when_every_scope_is_short(monkeypatch) -> None:
    short = (_case("f1", [1.0], [0.5, 0.5]), _case("f2", [1.0], [0.5, 0.5]))
    short = (short[0], short[1], _case("c1", [1.0], [1.0, 0.5]), _case("c2", [1.0], [1.0, 0.5]))

    def refuse(*args, **kwargs):
        raise AssertionError("no draw is made when no scope is available")

    monkeypatch.setattr(cs, "joint_replicates", refuse)
    final = cs.final_statistical(
        short,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="unpaired",
        seed=2,
        draws=100,
    )
    for estimate in (final.focus, final.causal, final.overall):
        assert estimate is not None
        assert (estimate.lower, estimate.upper, estimate.unavailable) == (
            None,
            None,
            "insufficient_repeats",
        )


def test_a_short_focus_case_leaves_only_the_causal_bound_available() -> None:
    cases = (FOCUS_CASES[0], _case("f2", [0.75], [0.5, 0.5])) + CAUSAL_CASES
    final = cs.final_statistical(
        cases,
        focus=FW,
        causal=CW,
        overall=OW,
        focus_confidence=0.9,
        causal_confidence=0.9,
        pairing="unpaired",
        seed=3,
        draws=100,
    )
    assert final.causal is not None
    assert final.causal.lower is not None and final.causal.unavailable in (
        None,
        "degenerate_interval",
    )
    for estimate in (final.focus, final.overall):
        assert (estimate.lower, estimate.unavailable) == (None, "insufficient_repeats")


def test_observe_points_request_no_bound() -> None:
    cases = tuple(_case(c.case_id, [c.candidate[0].score], [c.parent[0].score]) for c in ALL)
    points = cs.observe_points(cases, focus=FW, causal=CW, overall=OW)
    assert points.causal is not None
    for estimate, weights in ((points.focus, FW), (points.causal, CW), (points.overall, OW)):
        assert estimate == cs.Estimate(
            cs.publish(cs.point_contrast(cases, weights)),
            None,
            None,
            None,
            "not_requested_in_observe_mode",
        )


def test_a_cohort_contrast_is_one_sided_at_the_finalists_level() -> None:
    weights = cs.scope_weights(tuple(c.case_id for c in ALL), GROUPS)
    estimate = cs.cohort_contrast(ALL, weights, level=0.95, pairing="unpaired", seed=44, draws=200)
    draws = cs.joint_replicates(
        ALL, {"global": weights}, pairing="unpaired", seed=44, draws=200
    ).draws("global")
    assert estimate == cs.Estimate(
        cs.publish(cs.point_contrast(ALL, weights)),
        cs.publish(cs.one_sided_lower(draws, Fraction(19, 20))),
        None,
        0.95,
        None,
    )


def test_a_degenerate_cohort_bound_is_published_as_computed() -> None:
    cases = (_case("f1", [1.0, 1.0], [0.5, 0.5]),)
    estimate = cs.cohort_contrast(
        cases, {"f1": Fraction(1)}, level=0.9, pairing="unpaired", seed=1, draws=20
    )
    assert (estimate.lower, estimate.unavailable) == (0.5, None)


def test_an_observe_cohort_contrast_requests_no_bound() -> None:
    cases = (_case("f1", [1.0], [0.5]),)
    estimate = cs.cohort_contrast(
        cases, {"f1": Fraction(1)}, level=None, pairing="unpaired", seed=1
    )
    assert estimate == cs.Estimate(0.5, None, None, None, "bound_not_requested_in_observe_mode")


def test_compositions_refuse_overlapping_or_incomplete_scopes() -> None:
    with pytest.raises(ValueError):  # focus and causal overlap
        cs.final_statistical(
            ALL,
            focus={**FW, "c1": Fraction(1)},
            causal=CW,
            overall=OW,
            focus_confidence=0.9,
            causal_confidence=0.9,
            pairing="unpaired",
            seed=1,
            draws=2,
        )
    with pytest.raises(ValueError):  # a case outside both scopes
        cs.final_statistical(
            ALL,
            focus=FW,
            causal=None,
            overall=OW,
            focus_confidence=0.9,
            causal_confidence=0.9,
            pairing="unpaired",
            seed=1,
            draws=2,
        )
    with pytest.raises(ValueError):  # an empty causal scope is None, not {}
        cs.final_statistical(
            FOCUS_CASES,
            focus=FW,
            causal={},
            overall=FW,
            focus_confidence=0.9,
            causal_confidence=0.9,
            pairing="unpaired",
            seed=1,
            draws=2,
        )
    with pytest.raises(ValueError):  # interim weighs exactly the focus cases
        cs.interim_focus(ALL, FW, confidence=0.9, pairing="unpaired", seed=1, draws=2)


@pytest.mark.parametrize(
    ("focus_retained", "causal_cases", "causal_retained", "expected"),
    [
        (0, 3, 3, cs.Decision("insufficient_evidence", "no_evaluable_focus")),
        (2, 3, 0, cs.Decision("insufficient_evidence", "no_evaluable_pairs")),
        (2, 0, 0, None),
        (2, 3, 1, None),
    ],
)
def test_a_binding_closes_on_a_wholly_excluded_scope(
    focus_retained, causal_cases, causal_retained, expected
) -> None:
    assert (
        cs.decide_binding(
            focus_retained=focus_retained,
            causal_cases=causal_cases,
            causal_retained=causal_retained,
        )
        == expected
    )


# -- F6: the family-13 selector (design §6.11) ------------------------------------


def _fv(attempt_id, score, eligible, reason):
    return cs.FinalistVerdict(attempt_id, score, eligible, reason)


def _row(attempt_id, score, eligible, reason):
    return {"attempt_id": attempt_id, "score": score, "eligible": eligible, "reason": reason}


def test_no_finalists_select_nothing() -> None:
    assert cs.decide_selection("off", []) == cs.SelectionVerdict(None, "no_finalists", ())


def test_the_highest_eligible_score_is_selected_and_ineligible_rows_follow() -> None:
    verdict = cs.decide_selection(
        "point",
        [
            _fv("r01-a01", 0.6, True, "global_point_improved"),
            _fv("r01-a02", 0.9, False, "critical_failure"),
            _fv("r01-a03", 0.7, True, "global_point_improved"),
            _fv("r01-a04", None, False, "cohort_incomplete"),
        ],
    )
    assert verdict.winner == 2 and verdict.reason == "finalist_selected"
    assert verdict.ranking == (
        _row("r01-a03", 0.7, True, "global_point_improved"),
        _row("r01-a01", 0.6, True, "global_point_improved"),
        _row("r01-a02", 0.9, False, "critical_failure"),
        _row("r01-a04", None, False, "cohort_incomplete"),
    )


def test_an_exact_score_tie_goes_to_the_smaller_index() -> None:
    verdict = cs.decide_selection(
        "off", [_fv("r01-a01", 0.5, True, "global_off"), _fv("r01-a02", 0.5, True, "global_off")]
    )
    assert verdict.winner == 0
    assert [row["attempt_id"] for row in verdict.ranking] == ["r01-a01", "r01-a02"]


def test_the_ranking_reads_published_values_exactly() -> None:
    # 0.1 + 0.2 is published as 0.30000000000000004, which is above 0.3.
    verdict = cs.decide_selection(
        "off",
        [_fv("r01-a01", 0.3, True, "global_off"), _fv("r01-a02", 0.1 + 0.2, True, "global_off")],
    )
    assert verdict.winner == 1


@pytest.mark.parametrize(
    ("mode", "finalists", "reason"),
    [
        ("off", [("critical_failure", 0.4)], "no_eligible_finalist"),
        ("point", [("critical_failure", 0.4), ("cohort_incomplete", None)], "no_eligible_finalist"),
        (
            "point",
            [("critical_failure", 0.4), ("global_point_not_improved", 0.2)],
            "global_point_not_improved",
        ),
        ("bound", [("global_bound_not_positive", 0.3)], "global_bound_not_positive"),
    ],
)
def test_a_retained_champion_names_why(mode, finalists, reason) -> None:
    verdict = cs.decide_selection(
        mode,
        [
            _fv(f"r01-a0{index}", score, False, why)
            for index, (why, score) in enumerate(finalists, start=1)
        ],
    )
    assert verdict.winner is None and verdict.reason == reason
    assert [row["attempt_id"] for row in verdict.ranking] == [
        f"r01-a0{index}" for index in range(1, len(finalists) + 1)
    ]


@pytest.mark.parametrize(
    ("mode", "finalist"),
    [
        ("loose", ("r01-a01", 0.5, True, "global_off")),  # unknown mode
        ("off", ("r01-a01", 0.5, True, "critical_failure")),  # reason against eligibility
        ("off", ("r01-a01", None, True, "global_off")),  # an eligible finalist has a score
        ("off", ("r01-a01", 0.5, False, "global_point_not_improved")),  # off fails no mode
    ],
)
def test_the_selector_refuses_inconsistent_verdicts(mode, finalist) -> None:
    with pytest.raises(ValueError):
        cs.decide_selection(mode, [_fv(*finalist)])


def test_the_selection_vocabulary_is_the_designs() -> None:
    assert cs.SELECTION_REASONS == {
        "finalist_selected",
        "no_eligible_finalist",
        "global_point_not_improved",
        "global_bound_not_positive",
        "no_finalists",
    }
    assert cs.GUARD_REASONS == {"critical_failure", "cohort_incomplete"}
    assert cs.GUARD_REASONS <= cs.GLOBAL_REASONS[False]
