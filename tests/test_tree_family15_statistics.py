"""Exact family-15 adapters reuse the immutable repeat-bootstrap kernel."""

from dataclasses import replace
from fractions import Fraction as F

import pytest

from evolution.tree.confidence_statistics import (
    CaseEvidence,
    confidence_fraction,
    derive_seed,
    joint_replicates,
    one_sided_lower,
    pairing_of,
)
from evolution.tree.family15_schema import decode_rational
from evolution.tree.family15_statistics import bootstrap_scopes
from evolution.tree.final_statistics import final_statistical14
from evolution.tree.statistics import ScoreSample
from tests.test_tree_case_comparison import built


@pytest.mark.parametrize("source", ["stored", "fresh"])
@pytest.mark.parametrize("varying", [False, True])
def test_calibration_keeps_exact_family14_allowance_and_seed(source, varying):
    from evolution.tree.confidence_statistics import NoiseCase, noise_calibration
    from evolution.tree.family15_schema import calibration_payload
    from evolution.tree.family15_statistics import calibrate
    from tests.family15_fakes import measurement15

    parent = {
        "a": samples("a", [0.25, 0.5] * 5 if varying else [0.25] * 10),
        "b": samples("b", [0.5, 0.75] * 3 if varying else [0.5] * 6),
    }
    causal = [("b", F(1, 3)), ("a", F(2, 3))]
    m = measurement15("reference", settled=True)
    result = calibrate(
        parent,
        causal,
        campaign_seed=123,
        seed_binding="a" * 64,
        prefix=m["prefix"],
        source=source,
        measurement_id=m["measurement_id"],
    )
    seed = derive_seed(
        campaign_seed=123,
        binding="a" * 64,
        scope="noise",
        prefixes=[m["prefix"]["locator"]],
        look=None,
        purpose="noise",
    )
    expected = noise_calibration(
        [NoiseCase(c, parent[c], len(parent[c]), len(parent[c])) for c, _ in causal],
        dict(causal),
        seed=seed,
    )
    assert type(result.allowance) is F
    assert result.allowance == expected.allowance
    assert result.degenerate == expected.degenerate == (not varying)
    assert result.unavailable is None
    assert result.payload == calibration_payload(
        source=source,
        measurement_id=m["measurement_id"],
        prefix=m["prefix"],
        retained=2,
        seed_input="a" * 64,
        allowance=expected.allowance,
        degenerate=expected.degenerate,
    )


def test_calibration_subdigest_binds_ordered_exact_weights_and_parent_only():
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_schema import encode_rational
    from evolution.tree.family15_statistics import calibration_seed_binding
    from tests.family15_fakes import measurement15

    prefix = measurement15("reference", settled=True)["prefix"]
    causal = [("b", F(1, 3)), ("a", F(2, 3))]
    expected = hashlib.sha256(
        canonical_json(
            dict(
                policy="a" * 64,
                reference="reference",
                prefix=prefix,
                causal=[[c, encode_rational(w)] for c, w in causal],
            )
        )
    ).hexdigest()
    assert calibration_seed_binding("a" * 64, "reference", prefix, causal) == expected
    assert calibration_seed_binding("a" * 64, "reference", prefix, causal[::-1]) != expected


@pytest.mark.parametrize("count", [0, 1])
def test_insufficient_calibration_has_no_allowance_or_payload(count):
    from evolution.tree.family15_statistics import calibrate
    from tests.family15_fakes import measurement15

    m = measurement15("reference", settled=True)
    result = calibrate(
        {"a": samples("a", [0.25] * count)},
        [("a", F(1))],
        campaign_seed=123,
        seed_binding="a" * 64,
        prefix=m["prefix"],
        source="stored",
        measurement_id=m["measurement_id"],
    )
    assert result.allowance is None and result.payload is None
    assert result.unavailable == "insufficient_calibration"
    assert result.degenerate is False


def samples(case, values, *, boot="boot"):
    return tuple(
        ScoreSample(case, str(i), value, i, i, boot, "a" * 64, "g", "s")
        for i, value in enumerate(values, 1)
    )


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("paired", [False, True])
def test_r10_variation_matches_family14_exact_kernel_and_is_deterministic(fresh, paired):
    profile, _ = built(
        mode="statistical",
        repeats=10,
        fresh=fresh,
        confidence=(0.75, 0.75),
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    candidate = {c: samples(c, [0.5, 0.75] * 5) for c in ("a", "b")}
    parent = {c: samples(c, [0.25, 0.5] * 5, boot="boot" if paired else "other") for c in candidate}
    if not paired:
        parent = {
            c: tuple(replace(s, effective_seed=999) for s in rows) for c, rows in parent.items()
        }
    # Paired differences vary too: this exercises the actual bootstrap, not just exactness.
    candidate["a"] = samples("a", [0.75, 0.5] * 5)
    kwargs = dict(
        campaign_seed=123, binding_digest=profile.digest, prefixes=["candidate", "parent"]
    )
    result = bootstrap_scopes(profile, candidate, parent, **kwargs)
    assert result == bootstrap_scopes(profile, candidate, parent, **kwargs)
    evidence = [CaseEvidence(c, candidate[c], parent[c]) for c in candidate]
    seed = derive_seed(
        campaign_seed=123,
        binding=profile.digest,
        scope="joint",
        prefixes=kwargs["prefixes"],
        look=10,
        purpose="gate",
    )
    expected = final_statistical14(
        evidence,
        focus={"a": F(1)},
        causal={"b": F(1)},
        overall={"a": F(1, 2), "b": F(1, 2)},
        focus_confidence=0.75,
        causal_confidence=0.75,
        pairing=pairing_of(evidence),
        seed=seed,
    )
    assert result.seed == seed
    for name in ("focus", "causal"):
        scope = result.scopes[name]
        assert type(scope.lower) is F and type(scope.point) is F
        assert float(scope.lower) == getattr(expected, name).lower
        assert scope.exact == getattr(expected, name).exact
        assert scope.pairing == ("exact" if paired else "unpaired")
    assert "intervals_empirical" in result.caveats


@pytest.mark.parametrize("confidence", [0.9, 0.95])
def test_nondyadic_confidence_matches_family14_at_a_real_quantile_gap(confidence):
    profile, _ = built(mode="statistical", repeats=10, confidence=(confidence, confidence))
    candidate = samples("c", [((i**5 * 1934931) % 2**24) / 2**24 for i in range(1, 11)])
    parent = tuple(
        replace(s, effective_seed=999)
        for s in samples("c", [((i**5 * 2654437) % 2**24) / 2**24 for i in range(1, 11)])
    )
    result = bootstrap_scopes(
        profile,
        {"c": candidate},
        {"c": parent},
        campaign_seed=123,
        binding_digest=profile.digest,
        prefixes=["c", "p"],
    )
    evidence = [CaseEvidence("c", candidate, parent)]
    pairing = pairing_of(evidence)
    draws = joint_replicates(
        evidence,
        {"focus": {"c": F(1)}},
        pairing=pairing,
        seed=result.seed,
    ).draws("focus")
    level = confidence_fraction(confidence)
    h = (len(draws.numerators) - 1) * (1 - level)
    index = h.numerator // h.denominator
    assert draws.numerators[index] < draws.numerators[index + 1]
    lower = one_sided_lower(draws, level)
    assert one_sided_lower(draws, F(confidence)) != lower
    assert result.scopes["focus"].lower == lower
    expected = final_statistical14(
        evidence,
        focus={"c": F(1)},
        causal=None,
        overall={"c": F(1)},
        focus_confidence=confidence,
        causal_confidence=confidence,
        pairing=pairing,
        seed=result.seed,
    )
    assert float(result.scopes["focus"].lower) == expected.focus.lower
    assert decode_rational(profile.to_payload()["scopes"][0]["confidence"]) == level


def test_exact_paired_contrast_with_varying_sides_is_decided_on_point():
    profile, _ = built(mode="statistical", repeats=10)
    parent = {"c": samples("c", [0.25, 0.5] * 5)}
    candidate = {"c": tuple(replace(s, score=s.score + 0.25) for s in parent["c"])}
    result = bootstrap_scopes(
        profile,
        candidate,
        parent,
        campaign_seed=1,
        binding_digest=profile.digest,
        prefixes=["c", "p"],
    )
    assert result.scopes["focus"].exact is True
    assert result.scopes["focus"].lower == result.scopes["focus"].point == F(1, 4)
    assert result.caveats == ("exact_contrast", "intervals_empirical")


def test_insufficient_case_does_not_disable_other_scope():
    profile, _ = built(
        mode="statistical",
        repeats=10,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    candidate = {"a": samples("a", [0.75] * 10), "b": samples("b", [0.5])}
    parent = {c: samples(c, [0.25] * 10) for c in candidate}
    result = bootstrap_scopes(
        profile,
        candidate,
        parent,
        campaign_seed=1,
        binding_digest=profile.digest,
        prefixes=["c", "p"],
    )
    assert result.scopes["focus"].lower == F(1, 2)
    assert result.scopes["causal"].unavailable == "insufficient_repeats"
    assert result.scopes["causal"].lower is None


@pytest.mark.parametrize("repeats", [2, 3, 10])
def test_seed_look_is_bound_r_not_last_retained_repeat(repeats):
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from tests.family15_fakes import v16_document

    document = v16_document()["campaign"]
    document["evaluation"]["repeats_per_question"] = repeats
    policy = CampaignPolicyV12.from_payload(document)
    profile, _ = built(mode="statistical", repeats=repeats, exclusions=(("c", repeats),))
    result = bootstrap_scopes(
        profile,
        {"c": samples("c", [0.75] * repeats)},
        {"c": samples("c", [0.25] * repeats)},
        campaign_seed=policy.random_seed,
        binding_digest=profile.digest,
        prefixes=["c", "p"],
    )
    assert max(profile.to_payload()["scopes"][0]["cases"][0]["repeats"]) == repeats - 1
    assert result.look == policy.repeats
    assert result.seed == derive_seed(
        campaign_seed=policy.random_seed,
        binding=profile.digest,
        scope="joint",
        prefixes=["c", "p"],
        look=policy.repeats,
        purpose="gate",
    )
