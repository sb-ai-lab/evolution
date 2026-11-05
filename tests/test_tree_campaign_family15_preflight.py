"""Reference-aware exact pre-spend bounds; no observed Candidate evidence."""

from dataclasses import replace
from fractions import Fraction as F

import pytest

from evolution.tree.case_comparison import ComparisonProfile, preflight
from evolution.tree.family15_schema import decode_rational as dq
from evolution.tree.family15_schema import encode_rational as q
from tests.test_tree_case_comparison import built
from tests.test_tree_case_statistics import equal_e

pytestmark = pytest.mark.slow


def test_point_fallback_still_rejects_impossible_reference_ceiling():
    p, refs = built(
        mode="confidence",
        fallback=True,
        confidence=(0.9, 0.9),
        parent_values={"c": [1]},
    )
    result = preflight(p, refs)
    assert result.impossible
    body = result.to_payload()
    assert body["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert body["results"][0]["delta_max"] == q(F())


async def test_real_point_fallback_still_closes_structurally_unattainable(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests.campaign_drivers import EDITED, _records
    from tests.test_tree_campaign_family15_gate import gate_run

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="confidence",
        gate_options={
            "bound_fallback": "point",
            "global": "off",
            "focus": {"margin": 1, "confidence": 0.9},
        },
        **EDITED,
    )
    records = _records(run / "campaign.jsonl")
    failed = next(r for r in records if r["event"] == "comparison_preflight_failed")
    assert failed["payload"]["reason"] == "structurally_unattainable"
    assert not any(r["event"] == "comparison_completed" for r in records)
    projection = inspect_campaign(run, verify=True).to_payload()
    assert not projection["verification"]["mismatches"], projection["verification"]
    assert projection["independent"]["attempts"][0]["preflight"]["scopes"][0]["fallback"] == {
        "reason": "focus_unattainable"
    }


def stored_profile(values, **kwargs):
    profile, rows = built(**kwargs)
    rows = [replace(r, metrics={"score": values.get((r.case_id, r.repeat), 0)}) for r in rows]
    body = profile.to_payload()
    for scope in body["scopes"]:
        for case in scope["cases"]:
            case["reference_values"] = [
                q(F(values.get((case["case_id"], r), 0))) for r in case["repeats"]
            ]
    return ComparisonProfile.from_payload(body, primary="score", groups=dict(profile.groups)), rows


@pytest.mark.parametrize("failures,impossible", [(4, True), (5, False)])
def test_133_case_stored_failures_exact_evidence(failures, impossible):
    groups = {str(i): "g" for i in range(133)}
    profile, rows = stored_profile(
        {(str(i), 1): int(i >= failures) for i in range(133)},
        groups=groups,
        scopes={"global": list(groups)},
        mode="confidence",
        global_mode="bound",
    )
    result = preflight(profile, rows)
    assert result.impossible is impossible
    expected = equal_e([F(1)] * failures + [F(0)] * (133 - failures))
    assert dq(result.to_payload()["results"][0]["e_max"]["value"]) == expected
    assert float(expected) == pytest.approx(11.028194 if failures == 4 else 21.063, rel=1e-4)


def test_retained_repeats_unequal_groups_and_allowance_once():
    p, rows = stored_profile(
        {("a", 1): 0.25, ("a", 2): 0.75, ("b", 1): 0.5, ("b", 2): 1, ("c", 1): 0.25},
        groups={"a": "heavy", "b": "light", "c": "light"},
        repeats=2,
        scopes={"global": ["a", "b", "c"]},
        exclusions=(("c", 2),),
        allowance=0.1,
    )
    result = preflight(p, rows)
    r = result.to_payload()["results"][0]
    assert dq(r["delta_max"]) == F(1, 2) * F(1, 2) + F(1, 4) * F(1, 4) + F(1, 4) * F(3, 4)
    assert dq(result.to_payload()["scopes"][0]["threshold"]) == F(0.1)
    assert not result.impossible


def test_reference_value_change_invalidates_frozen_binding():
    p, rows = stored_profile({("c", 1): 0.5})
    with pytest.raises(ValueError, match="reference"):
        preflight(p, [replace(rows[0], metrics={"score": 0.75})])
    changed, changed_rows = stored_profile({("c", 1): 0.75})
    assert preflight(p, rows).profile_digest != preflight(changed, changed_rows).profile_digest


@pytest.mark.parametrize("threshold,passes", [(0.5, False), (1, False), (-1, True)])
def test_exact_point_and_confidence_support(threshold, passes):
    mode = "confidence" if threshold in (-1, 1) else "point"
    p, rows = stored_profile({("c", 1): 0.5}, mode=mode)
    body = p.to_payload()
    body["scopes"][0].update(threshold=q(F(threshold)))
    p = ComparisonProfile.from_payload(body, primary="score", groups=dict(p.groups))
    assert preflight(p, rows).to_payload()["results"][0]["passes"] is passes


def test_causal_point_equality_is_inclusive():
    p, rows = stored_profile(
        {("c", 1): 0.5, ("d", 1): 0.5},
        groups={"c": "g", "d": "g"},
        scopes={"focus": ["c"], "causal": ["d"]},
    )
    body = p.to_payload()
    body["scopes"][1]["threshold"] = q(F(0.5))
    p = ComparisonProfile.from_payload(body, primary="score", groups=dict(p.groups))
    assert preflight(p, rows).to_payload()["results"][1]["passes"] is True


def test_unfiltered_has_no_numerical_veto():
    p, rows = stored_profile({("c", 1): 1}, mode="unfiltered")
    result = preflight(p, rows)
    assert not result.impossible
    assert result.to_payload() is None


@pytest.mark.parametrize("unit", ["case", "group"])
@pytest.mark.parametrize("margin,allowance", [(-0.1, 0.02), (0.3, 0.1)])
def test_unequal_fractional_confidence_matches_independent_reference(unit, margin, allowance):
    from tests.test_tree_case_statistics_properties import reference_e

    p, rows = stored_profile(
        {("a", 1): 0.3, ("a", 2): 0.1, ("b", 1): 0.8, ("b", 2): 0.2, ("c", 1): 0.4, ("c", 2): 0.7},
        mode="confidence",
        groups={"a": "heavy", "b": "light", "c": "light"},
        repeats=2,
        allowance=allowance,
        unit=unit,
    )
    body = p.to_payload()
    body["scopes"][0]["threshold"] = q(F(margin) + F(allowance))
    p = ComparisonProfile.from_payload(body, primary="score", groups=dict(p.groups))
    scope = body["scopes"][0]
    case_values = {
        c["case_id"]: 1 - sum(map(dq, c["reference_values"])) / 2 for c in scope["cases"]
    }
    weights = {c["case_id"]: dq(c["weight"]) for c in scope["cases"]}
    masses = [dq(u["weight"]) for u in scope["units"]]
    values = [
        sum(weights[c] * case_values[c] for c in u["cases"]) / mass
        for u, mass in zip(scope["units"], masses, strict=True)
    ]
    expected = reference_e(values, masses, F(margin) + F(allowance))
    result = preflight(p, rows).to_payload()["results"][0]
    assert dq(result["e_max"]["value"]) == expected
    assert result["passes"] is (expected > 1 / (1 - dq(scope["confidence"])))


def test_conditional_local_guidance_is_not_an_admission_rule():
    from evolution.tree.family15_report import local_guidance

    guidance = local_guidance(0.95)
    assert guidance["minimum_units"] == 5
    assert guidance["status"] == "conditional_guidance"
    assert guidance["interpretation"] == "not_an_admission_or_power_claim"
