"""Exact complete-scope comparisons, independent of dispatch."""

from dataclasses import replace
from fractions import Fraction

import pytest

from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.case_comparison import ComparisonProfile, evaluate_comparison
from tests.test_tree_campaign_family15_schema import binding, q


def row(value, *, case="c", repeat=1, disposition="score", critical=True):
    return SampleRow(
        case,
        f"s{repeat}",
        repeat,
        42,
        42,
        "boot",
        "a" * 64,
        "completed",
        disposition,
        {"score": value} if value is not None else None,
        critical,
    )


def profile(*, mode="point", threshold=0, noise="negligible", repeats=1):
    body = binding(mode)
    from tests.family15_fakes import reference15

    body["candidate"]["prefix"] = reference15("journal_prefix", "candidate")
    body["scopes"][0]["threshold"] = q(threshold)
    body["execution_noise"] = {"kind": noise}
    for side in ("candidate", "reference"):
        identity = body[side]["identity"]
        identity["schedule"] = [
            dict(identity["schedule"][0], sample_id=f"s{i}", repeat=i)
            for i in range(1, repeats + 1)
        ]
        from evolution.tree.execution.journal_identity import RunIdentityV1

        body[side]["identity_digest"] = RunIdentityV1.from_payload(identity).digest
    body["scopes"][0]["cases"][0].update(
        repeats=list(range(1, repeats + 1)), reference_values=[q(Fraction(1, 2))] * repeats
    )
    return ComparisonProfile.from_payload(body, primary="score", groups={"c": "g"})


@pytest.mark.parametrize(
    "value,outcome",
    [(0.75, "passed"), (0.5, "comparison_not_passed"), (0.25, "comparison_not_passed")],
)
def test_strict_focus(value, outcome):
    result = evaluate_comparison(profile(), [row(value)], [row(0.5)])
    assert result.outcome == outcome
    assert result.to_payload()["scopes"][0]["point"] == q(Fraction(value) - Fraction(0.5))


def test_candidate_missing_repeat_cannot_hide_behind_parent_exclusion():
    p = profile(repeats=3)
    body = p.to_payload()
    body["excluded_reference"] = [body["reference"]["identity"]["schedule"][2]]
    body["scopes"][0]["cases"][0].update(repeats=[1, 2], reference_values=[q(0.5)] * 2)
    p = ComparisonProfile.from_payload(body, primary="score", groups={"c": "g"})
    result = evaluate_comparison(
        p,
        [row(1, repeat=i) for i in (1, 2)],
        [row(0.5, repeat=i) for i in (1, 2)]
        + [replace(row(None, repeat=3, disposition="exclude"), exhausted=True)],
    )
    assert result.reason == "candidate_incomplete"


def test_binary64_values_are_not_decimal_reinterpreted():
    result = evaluate_comparison(
        profile(threshold=Fraction(0.3) - Fraction(0.1)), [row(0.3)], [row(0.1)]
    )
    assert result.outcome == "comparison_not_passed"


@pytest.mark.parametrize("repeats,variation", [(1, "unavailable"), (3, "observed")])
def test_observed_repeat_diagnostics(repeats, variation):
    result = evaluate_comparison(
        profile(noise="observed", repeats=repeats),
        [row(i / 4, repeat=i) for i in range(1, repeats + 1)],
        [row(0.5, repeat=i) for i in range(1, repeats + 1)],
    )
    assert result.to_payload()["variation"] == variation
    diagnostic = result.to_payload()["repeat_diagnostics"][0]
    assert diagnostic["variation"] == (None if repeats == 1 else q(Fraction(1, 16)))


def test_critical_veto_precedes_threshold():
    assert (
        evaluate_comparison(profile(), [row(1, critical=False)], [row(0.5)]).reason
        == "critical_failed"
    )


def built(
    *,
    mode="point",
    scopes=None,
    groups=None,
    repeats=1,
    allowance=None,
    unit="case",
    global_mode="point",
    confidence=(0.1, 0.95),
    exclusions=(),
    fresh=False,
    margin=0,
    tolerance=0,
    parent_values=None,
    fallback=False,
    stages=None,
):
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.case_comparison import build_profile
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.family15_fakes import measurement15, v16_document

    groups = groups or {"c": "g"}
    scopes = scopes or {"focus": list(groups), "causal": []}
    document = v16_document()["campaign"]
    document["evaluation"]["repeats_per_question"] = repeats
    gate = document["gate"]
    if stages is not None:
        gate["stages"] = stages
    gate.update(mode=mode, **{"global": "off" if mode == "unfiltered" else global_mode})
    gate.update(fresh_parent=fresh)
    if fallback:
        gate["bound_fallback"] = "point"
    gate["focus"]["margin"] = margin
    gate["causal"]["tolerance"] = tolerance
    if allowance is not None:
        gate["execution_noise"] = dict(
            kind="bounded", allowance=allowance, source="declared reserve"
        )
    if mode in {"confidence", "statistical"}:
        gate["focus"]["confidence"], gate["causal"]["confidence"] = confidence
    if mode == "confidence":
        gate["statistics"] = dict(method="bounded-weighted-mean/1", unit=unit)
    policy = CampaignPolicyV12.from_payload(document)
    candidate, reference = measurement15(settled=True), measurement15("reference", settled=True)
    for m in (candidate, reference):
        m["identity"]["schedule"] = [
            dict(case_id=case, sample_id=f"s{i}", repeat=i, requested_seed=42)
            for case in groups
            for i in range(1, repeats + 1)
        ]
        m["identity_digest"] = RunIdentityV1.from_payload(m["identity"]).digest
    reference_rows = [
        replace(
            row(0 if parent_values is None else parent_values[case][i - 1], case=case, repeat=i),
            disposition="exclude" if (case, i) in exclusions else "score",
            exhausted=(case, i) in exclusions,
        )
        for case in groups
        for i in range(1, repeats + 1)
    ]
    profile = build_profile(
        policy=policy,
        corpus=[dict(case_id=c, group_id=g) for c, g in groups.items()],
        scopes=scopes,
        candidate=candidate,
        reference=reference,
        reference_rows=reference_rows,
        epoch=0,
        reference_kind="fresh" if fresh else "stored",
    )
    return profile, reference_rows


def test_small_focus_falls_back_at_reference_ceilings_not_candidate_outcomes():
    from evolution.tree.case_comparison import preflight

    p, refs = built(mode="confidence", confidence=(0.95, 0.95), fallback=True)
    scope = p.to_payload()["scopes"][0]
    assert scope["rule"] == "point"
    assert scope["fallback"] == {"reason": "focus_unattainable"}
    assert "units" not in scope and "confidence" not in scope
    assert not preflight(p, refs).impossible
    for value, outcome in [(1, "passed"), (0, "comparison_not_passed")]:
        result = evaluate_comparison(p, [row(value)], refs).to_payload()
        assert result["outcome"] == outcome
        assert result["scopes"][0]["fallback"] == scope["fallback"]


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("tolerance", [0, "measured_noise"])
def test_causal_fallback_preserves_focus_bound_and_is_candidate_invariant(fresh, tolerance):
    groups = {**{f"a{i}": "g" for i in range(6)}, "b": "g"}
    p, refs = built(
        mode="confidence",
        fallback=True,
        fresh=fresh,
        repeats=2,
        confidence=(0.9, 0.9),
        tolerance=tolerance,
        groups=groups,
        scopes={"focus": list(groups)[:-1], "causal": ["b"]},
    )
    body = p.to_payload()
    assert body["scopes"][0]["rule"] == "bound"
    deferred = fresh and tolerance == "measured_noise"
    assert body["scopes"][1]["rule"] == ("bound" if deferred else "point")
    assert ("fallback" in body["scopes"][1]) != deferred
    candidate = [row(1 if c != "b" else 0, case=c, repeat=i) for c in groups for i in (1, 2)]
    for rows in (candidate, [replace(r, metrics={"score": 0.0}) for r in candidate]):
        result = evaluate_comparison(p, rows, refs, campaign_seed=1).to_payload()
        causal = result["scopes"][1]
        assert causal["rule"] == "point"
        assert causal["fallback"] == {"reason": "causal_unconfirmable"}
        assert "enclosure" not in causal and "confidence" not in causal
    assert evaluate_comparison(p, candidate, refs, campaign_seed=1).outcome == "passed"


def test_large_scopes_keep_bound_and_fallback_uses_reference_aware_ceilings():
    from evolution.tree.case_comparison import fallback_reason

    p, _ = built(mode="confidence", confidence=(0.9, 0.9), groups={str(i): "g" for i in range(8)})
    scope = p.to_payload()["scopes"][0]
    # The helper must not need the policy declaration: it reports structural
    # feasibility; the caller owns the opt-in substitution.
    policy = p.policy
    assert fallback_reason(scope, policy, threshold=Fraction()) is None
    for case in scope["cases"]:
        case["reference_values"] = [q(1)]
    assert fallback_reason(scope, policy, threshold=Fraction()) == "focus_unattainable"
    causal = {**scope, "name": "causal", "strict": False}
    assert fallback_reason(causal, policy, threshold=Fraction(-1)) is None


@pytest.mark.parametrize("unit", ["case", "group"])
def test_opted_in_large_scopes_are_unchanged(unit):
    groups = {f"c{i}": f"g{i}" for i in range(72)}
    options = dict(
        mode="confidence",
        confidence=(0.9, 0.9),
        unit=unit,
        tolerance=0.05,
        groups=groups,
        scopes={"focus": list(groups)[:8], "causal": list(groups)[8:]},
    )
    original, _ = built(**options)
    fallback, _ = built(**options, fallback=True)
    assert original.to_payload()["scopes"] == fallback.to_payload()["scopes"]
    assert all(
        scope["rule"] == "bound" and "fallback" not in scope
        for scope in fallback.to_payload()["scopes"]
    )


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("guard", [None, "candidate_incomplete", "critical_failed"])
def test_unknown_q_keeps_independent_focus_fallback_only(fresh, guard):
    p, refs = built(
        mode="confidence",
        confidence=(0.9, 0.9),
        fallback=True,
        repeats=2,
        fresh=fresh,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        exclusions=() if fresh else (("b", 2),),
    )
    if fresh:
        body = p.to_payload()
        body["scopes"][1]["cases"][0]["repeats"] = [1]
        p = ComparisonProfile.from_payload(
            body, primary="score", groups=dict(p.groups), policy=p.policy
        )
    candidate = [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)]
    if guard == "candidate_incomplete":
        candidate.pop()
    elif guard == "critical_failed":
        candidate[0] = replace(candidate[0], critical_pass=False)
    result = evaluate_comparison(p, candidate, refs, campaign_seed=1).to_payload()
    assert result["reason"] == (guard or "insufficient_calibration")
    assert result["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert result["scopes"][1]["rule"] == "bound"
    assert "fallback" not in result["scopes"][1]
    assert result["scopes"][1]["threshold"] is None


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("allowance,causal_rule", [(None, "bound"), (0.125, "point")])
def test_causal_fallback_uses_known_q_and_execution_allowance_once(fresh, allowance, causal_rule):
    p, refs = built(
        mode="confidence",
        confidence=(0.9, 0.9),
        fallback=True,
        repeats=2,
        fresh=fresh,
        tolerance="measured_noise",
        allowance=allowance,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        parent_values={"a": [0, 0], "b": [0, 1]},
    )
    result = evaluate_comparison(
        p,
        [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)],
        refs,
        campaign_seed=1,
    ).to_payload()
    calibration = result.get("calibration", p.to_payload().get("calibration"))
    assert calibration["allowance"] == q(1)
    assert result["scopes"][1]["threshold"] == q(Fraction(allowance or 0) - 1)
    assert result["scopes"][1]["rule"] == causal_rule
    assert ("fallback" in result["scopes"][1]) == (causal_rule == "point")
    assert p.to_payload()["scopes"][1]["rule"] == ("bound" if fresh else causal_rule)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("tamper", ["threshold", "calibration_removed", "retained"])
def test_measured_result_structural_links_need_no_numerical_recomputation(fresh, tamper):
    import copy
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_schema import validate_comparison_data
    from evolution.tree.storage.envelope import SchemaError

    p, refs = built(
        repeats=2,
        fresh=fresh,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    result = evaluate_comparison(
        p,
        [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)],
        refs,
        campaign_seed=1,
    ).to_payload()
    validate_comparison_data(result, policy=p.policy, binding=p.to_payload())
    bad = copy.deepcopy(result)
    if tamper == "threshold":
        bad["scopes"][1]["threshold"] = q(Fraction(1, 8))
    elif tamper == "calibration_removed":
        if not fresh:
            bad["scopes"][1].pop("threshold")
        else:
            bad.pop("calibration")
    elif not fresh:
        # Stored calibration is fixed in the binding, never transported in a result.
        bad["calibration"] = copy.deepcopy(p.to_payload()["calibration"])
    else:
        bad["calibration"]["retained"] += 1
        c = bad["calibration"]
        c["result_digest"] = hashlib.sha256(
            canonical_json({k: v for k, v in c.items() if k != "result_digest"})
        ).hexdigest()
    with pytest.raises(SchemaError):
        validate_comparison_data(bad, policy=p.policy, binding=p.to_payload())


@pytest.mark.parametrize("fresh", [False, True])
def test_fallback_artifacts_fail_closed_without_declared_policy_and_exact_binding(fresh):
    from evolution.tree.family15_schema import validate_comparison_data
    from evolution.tree.storage.envelope import SchemaError

    p, refs = built(
        mode="confidence",
        confidence=(0.9, 0.9),
        fallback=True,
        repeats=2,
        fresh=fresh,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    result = evaluate_comparison(
        p, [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)], refs, campaign_seed=1
    ).to_payload()
    validate_comparison_data(result, policy=p.policy, binding=p.to_payload())
    for context in ({}, {"policy": p.policy}, {"binding": p.to_payload()}):
        with pytest.raises(SchemaError):
            validate_comparison_data(result, **context)


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
@pytest.mark.parametrize("fresh", [False, True])
def test_measured_causal_threshold_is_exact_e_minus_q_reserved_once(mode, fresh):
    from evolution.tree.family15_schema import decode_rational, validate_comparison_data
    from evolution.tree.family15_statistics import calibrate
    from tests.test_tree_family15_statistics import samples

    # q=0 gives an exact inclusive boundary. Repeats and confidence do not
    # discount the deterministic allowance e, and numeric binary64 stays exact.
    p, refs = built(
        mode=mode,
        fresh=fresh,
        repeats=10,
        allowance=0.125,
        margin=-0.9,
        confidence=(0.1, 0.1),
        tolerance="measured_noise",
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "g"},
        parent_values={"a": [0.25] * 10, "b": [0.25] * 10},
    )
    body = p.to_payload()
    causal = body["scopes"][1]
    assert causal["tolerance"] == "measured_noise"
    assert causal["threshold"] == (None if fresh else q(Fraction(1, 8)))
    candidates = [row(1, case="a", repeat=i) for i in range(1, 11)]
    candidates += [row(0.375, case="b", repeat=i) for i in range(1, 11)]
    result = evaluate_comparison(p, candidates, refs, campaign_seed=42).to_payload()
    calibration = result["calibration"] if fresh else body["calibration"]
    assert ("calibration" in body) != fresh
    assert ("calibration" in result) == fresh
    assert decode_rational(calibration["allowance"]) == 0
    assert result["scopes"][1]["threshold"] == q(Fraction(1, 8))
    assert result["scopes"][1]["status"] == "available"
    assert "noise_not_observed" in result["caveats"]
    # A nonpassing focus must not obscure the causal boundary check.
    if mode == "confidence":
        assert result["reason"] == "threshold_not_passed"
    else:
        assert result["outcome"] == "passed"
    validate_comparison_data(result)
    expected = calibrate(
        {"b": samples("b", [0.25] * 10)},
        [("b", Fraction(1))],
        campaign_seed=42 if fresh else 1,
        seed_binding=p.digest if fresh else calibration["seed_input"],
        prefix=body["reference"]["prefix"],
        source="fresh" if fresh else "stored",
        measurement_id=body["reference"]["measurement_id"],
    )
    assert calibration == expected.payload
    # Candidate outcomes can change the decision, never the Parent-only q.
    changed = evaluate_comparison(
        p, [replace(r, metrics={"score": 0.0}) for r in candidates], refs, campaign_seed=42
    ).to_payload()
    assert changed.get("calibration", body.get("calibration")) == calibration
    assert changed["scopes"][1]["threshold"] == result["scopes"][1]["threshold"]
    if mode != "confidence":
        below = [
            replace(r, metrics={"score": 0.375 - 2**-20}) if r.case_id == "b" else r
            for r in candidates
        ]
        rejected = evaluate_comparison(p, below, refs, campaign_seed=42).to_payload()
        assert (rejected["reason"], rejected["deciding_scope"]) == (
            "threshold_not_passed",
            "causal",
        )


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("guard", [None, "candidate_incomplete", "critical_failed"])
def test_unknown_measured_q_is_unavailable_never_a_zero_threshold(mode, fresh, guard):
    p, refs = built(
        mode=mode,
        repeats=2,
        tolerance="measured_noise",
        margin=-0.9,
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "g"},
        exclusions=() if fresh else (("b", 2),),
        fresh=fresh,
    )
    body = p.to_payload()
    if fresh:
        # Retained support is the kernel's input; fresh coverage is still over
        # all scheduled slots and remains a separate, higher-priority guard.
        body["scopes"][1]["cases"][0]["repeats"] = [1]
        p = ComparisonProfile.from_payload(body, primary="score", groups={"a": "g", "b": "g"})
    assert "calibration" not in body
    assert body.get("calibration_unavailable") == (None if fresh else "insufficient_calibration")
    assert body["scopes"][1]["threshold"] is None
    candidate = [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)]
    if guard == "candidate_incomplete":
        candidate.pop()
    if guard == "critical_failed":
        candidate[0] = replace(candidate[0], critical_pass=False)
    result = evaluate_comparison(p, candidate, refs, campaign_seed=1).to_payload()
    assert result["reason"] == (guard or "insufficient_calibration")
    assert result["scopes"][1]["threshold"] is None
    assert "calibration" not in result
    if mode != "statistical":
        assert "caveats" not in result
    else:
        assert "noise_not_observed" not in result["caveats"]


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
@pytest.mark.parametrize("fresh", [False, True])
def test_positive_measured_q_matches_kernel_and_candidate_outcomes_cannot_change_it(mode, fresh):
    from evolution.tree.confidence_statistics import NoiseCase, derive_seed, noise_calibration
    from evolution.tree.family15_schema import decode_rational as dq
    from tests.test_tree_family15_statistics import samples

    p, refs = built(
        mode=mode,
        fresh=fresh,
        repeats=4,
        allowance=0.1,
        tolerance="measured_noise",
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "g"},
        parent_values={"a": [0.25] * 4, "b": [0.25, 0.75] * 2},
    )
    body = p.to_payload()
    low = [row(0, case=c, repeat=i) for c in ("a", "b") for i in range(1, 5)]
    high = [replace(r, metrics={"score": 1.0}) for r in low]
    results = [
        evaluate_comparison(p, rows, refs, campaign_seed=1).to_payload() for rows in (low, high)
    ]
    calibrations = [r["calibration"] if fresh else body["calibration"] for r in results]
    assert calibrations[0] == calibrations[1]
    calibration = calibrations[0]
    seed = derive_seed(
        campaign_seed=1,
        binding=calibration["seed_input"],
        scope="noise",
        prefixes=[body["reference"]["prefix"]["locator"]],
        look=None,
        purpose="noise",
    )
    expected = noise_calibration(
        [NoiseCase("b", samples("b", [0.25, 0.75] * 2), 4, 4)], {"b": Fraction(1)}, seed=seed
    )
    assert dq(calibration["allowance"]) == expected.allowance > 0
    for result in results:
        assert dq(result["scopes"][1]["threshold"]) == Fraction(0.1) - expected.allowance
        assert "noise_allowance_empirical" in result["caveats"]
        assert "noise_not_observed" not in result["caveats"]


@pytest.mark.parametrize("fresh", [False, True])
def test_empty_causal_never_calibrates_or_invents_q(fresh, monkeypatch):
    import evolution.tree.family15_statistics as statistics

    def forbidden(*a, **kw):
        raise AssertionError("empty causal calibration")

    monkeypatch.setattr(statistics, "calibrate", forbidden)
    p, refs = built(repeats=2, tolerance="measured_noise", fresh=fresh)
    body = p.to_payload()
    assert "calibration" not in body and body["scopes"][1]["threshold"] is None
    result = evaluate_comparison(p, [row(1, repeat=i) for i in (1, 2)], refs).to_payload()
    assert result["scopes"][1]["status"] == "skipped"
    assert "calibration" not in result and "caveats" not in result


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
def test_fresh_measured_noise_never_masks_reference_coverage(mode):
    p, refs = built(
        mode=mode,
        fresh=True,
        repeats=2,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    candidate = [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)]
    result = evaluate_comparison(p, candidate, refs[:-1], campaign_seed=1).to_payload()
    assert (
        result["outcome"] == "comparison_unavailable" and result["reason"] == "reference_incomplete"
    )
    assert "calibration" not in result and result["scopes"][1]["threshold"] is None


@pytest.mark.parametrize("fresh", [False, True])
def test_unknown_calibration_grammar_refuses_a_fictitious_zero_threshold(fresh):
    import copy

    from evolution.tree.family15_schema import validate_comparison_data
    from evolution.tree.storage.envelope import SchemaError

    p, _ = built(
        fresh=fresh,
        repeats=2,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        exclusions=() if fresh else (("b", 2),),
    )
    body = p.to_payload()
    assert body["scopes"][1]["threshold"] is None
    bad = copy.deepcopy(body)
    bad["scopes"][1]["threshold"] = q(Fraction())
    with pytest.raises(SchemaError):
        validate_comparison_data(bad)
    bad = copy.deepcopy(body)
    bad["scopes"][1].pop("tolerance")
    with pytest.raises(SchemaError):
        validate_comparison_data(bad)


@pytest.mark.parametrize("fresh", [False, True])
def test_uncalibrated_preflight_has_no_numeric_causal_claim(fresh):
    from evolution.tree.case_comparison import preflight
    from evolution.tree.family15_report import explain_preflight, preflight_lines

    p, _ = built(
        fresh=fresh,
        repeats=2,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        exclusions=() if fresh else (("b", 2),),
    )
    result = preflight(p, None)
    assert result.to_payload()["results"][1] == {"name": "causal", "status": "uncalibrated"}
    lines = preflight_lines(explain_preflight(result), label="local")
    assert lines[-1] == (
        "causal · calibration will be computed from fresh Parent at decision"
        if fresh
        else "causal · Parent calibration unavailable"
    )


def test_inclusive_causal_equality_and_strict_focus_equality():
    p, refs = built(scopes={"focus": ["a"], "causal": ["b"]}, groups={"a": "g", "b": "g"})
    assert evaluate_comparison(p, [row(1, case="a"), row(0, case="b")], refs).outcome == "passed"
    assert (
        evaluate_comparison(p, [row(0, case="a"), row(0, case="b")], refs).reason
        == "threshold_not_passed"
    )


def test_allowance_is_reserved_once_and_does_not_shrink_with_repeats():
    for repeats in (1, 3):
        p, refs = built(allowance=0.25, repeats=repeats)
        assert p.to_payload()["scopes"][0]["threshold"] == q(Fraction(1, 4))
        assert (
            evaluate_comparison(
                p, [row(0.25, repeat=i) for i in range(1, repeats + 1)], refs
            ).outcome
            == "comparison_not_passed"
        )
        assert (
            evaluate_comparison(
                p, [row(0.5, repeat=i) for i in range(1, repeats + 1)], refs
            ).outcome
            == "passed"
        )


def test_global_confidence_uses_max_declared_level_even_without_causal():
    p, refs = built(mode="confidence", scopes={"global": ["c"]}, global_mode="bound")
    assert p.to_payload()["scopes"][0]["confidence"] == q(Fraction(0.95))
    assert evaluate_comparison(p, [row(1)], refs).outcome == "comparison_not_passed"


@pytest.mark.parametrize("unit", ["case", "group"])
def test_one_repeat_confidence_is_valid(unit):
    p, refs = built(mode="confidence", unit=unit, groups={f"c{i}": f"g{i}" for i in range(5)})
    result = evaluate_comparison(p, [row(1, case=f"c{i}") for i in range(5)], refs)
    assert result.outcome == "passed"
    assert result.to_payload()["scopes"][1]["status"] == "skipped"


def test_global_weights_restrict_full_corpus_and_renormalize_without_phantom_mass():
    groups = {"a": "heavy", "b": "light", "c": "light"}
    p, refs = built(scopes={"global": list(groups)}, groups=groups, exclusions=(("b", 1),))
    cases = p.to_payload()["scopes"][0]["cases"]
    assert [c["weight"] for c in cases] == [q(Fraction(2, 3)), q(Fraction(1, 3))]
    result = evaluate_comparison(
        p, [row(1, case="a"), row(0, case="b"), row(0, case="c")], refs
    ).to_payload()
    assert result["scopes"][0]["point"] == q(Fraction(2, 3))
    assert result["scopes"][0]["excluded_mass"] == q(Fraction(1, 4))


def test_nonempty_excluded_causal_is_unavailable_not_skipped():
    p, refs = built(
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "h"},
        exclusions=(("b", 1),),
    )
    result = evaluate_comparison(p, [row(1, case="a"), row(1, case="b")], refs)
    assert result.reason == "empty_support"
    assert result.to_payload()["deciding_scope"] == "causal"


def test_unfiltered_preserves_critical_and_complete_coverage_guards():
    p, refs = built(mode="unfiltered")
    assert evaluate_comparison(p, [row(0)], refs).outcome == "passed"
    assert evaluate_comparison(p, [row(0, critical=False)], refs).reason == "critical_failed"
    assert evaluate_comparison(p, [], refs).reason == "candidate_incomplete"


def test_duplicate_or_unscheduled_rows_are_never_extra_weight():
    p = profile()
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_comparison(p, [row(1), row(1)], [row(0.5)])


@pytest.mark.parametrize("rule", ["point", "off"])
def test_confidence_campaign_global_point_or_off_has_no_bound(rule):
    p, refs = built(mode="confidence", scopes={"global": ["c"]}, global_mode=rule)
    result = evaluate_comparison(p, [row(1)], refs).to_payload()
    assert result["outcome"] == "passed"
    assert "enclosure" not in result["scopes"][0]
    assert "confidence" not in result["scopes"][0]


def test_whole_excluded_group_has_no_phantom_unit_or_weight():
    p, refs = built(
        mode="confidence", unit="group", groups={"a": "x", "b": "y"}, exclusions=(("b", 1),)
    )
    scope = p.to_payload()["scopes"][0]
    assert scope["units"] == [dict(unit_id="x", cases=["a"], weight=q(Fraction(1)))]
    assert evaluate_comparison(p, [row(1, case="a"), row(1, case="b")], refs).to_payload()[
        "scopes"
    ][0]["excluded_mass"] == q(Fraction(1, 2))


def test_parent_exclusion_cannot_hide_candidate_critical_failure():
    p, refs = built(repeats=3, exclusions=(("c", 3),))
    candidate = [row(1, repeat=i, critical=i != 3) for i in (1, 2, 3)]
    assert evaluate_comparison(p, candidate, refs).reason == "critical_failed"


def test_negative_focus_margin_explicitly_permits_regression():
    p = profile(threshold=Fraction(-1, 2))
    assert evaluate_comparison(p, [row(0.25)], [row(0.5)]).outcome == "passed"


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("focus,expected", [(0.5, "comparison_not_passed"), (0.75, "passed")])
def test_statistical_exact_thresholds_reserve_allowance_once(fresh, focus, expected):
    p, refs = built(
        mode="statistical",
        repeats=10,
        fresh=fresh,
        allowance=0.25,
        margin=0.25,
        tolerance=0.25,
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "g"},
    )
    candidate = [
        row(focus if r.case_id == "a" else 0, case=r.case_id, repeat=r.repeat) for r in refs
    ]
    result = evaluate_comparison(p, candidate, refs, campaign_seed=123).to_payload()
    assert result["outcome"] == expected
    assert [s["lower"] for s in result["scopes"]] == [q(Fraction(focus)), q(0)]
    assert result["caveats"] == ["exact_contrast", "intervals_empirical"]


def test_statistical_local_preflight_is_disabled_but_global_point_is_not():
    from evolution.tree.case_comparison import preflight

    local, refs = built(mode="statistical", repeats=10, margin=2)
    assert preflight(local, refs).to_payload() is None
    assert not preflight(local, refs).impossible
    global_profile, refs = built(mode="statistical", repeats=10, scopes={"global": ["c"]})
    assert preflight(global_profile, refs).to_payload()["phase"] == "early_global"


@pytest.mark.parametrize("confidence", [0.9, 0.95])
def test_bootstrap_confidence_convention_does_not_change_bound_or_threshold_inputs(confidence):
    from evolution.tree.confidence_statistics import confidence_fraction
    from evolution.tree.family15_schema import decode_rational as dq

    for mode in ("statistical", "confidence"):
        p, _ = built(
            mode=mode,
            repeats=10,
            confidence=(confidence, confidence),
            margin=0.1,
            tolerance=0.1,
            allowance=0.1,
        )
        focus, causal = p.to_payload()["scopes"]
        expected = (
            confidence_fraction(confidence) if mode == "statistical" else Fraction(confidence)
        )
        assert dq(focus["confidence"]) == dq(causal["confidence"]) == expected
        assert dq(focus["threshold"]) == Fraction(0.1) + Fraction(0.1)
        assert dq(causal["threshold"]) == -Fraction(0.1) + Fraction(0.1)


def test_statistical_insufficient_repeats_is_per_scope_and_precedes_thresholds():
    p, refs = built(
        mode="statistical",
        repeats=2,
        groups={"a": "g", "b": "h"},
        scopes={"focus": ["a"], "causal": ["b"]},
        exclusions=(("b", 2),),
    )
    candidate = [row(0, case=r.case_id, repeat=r.repeat) for r in refs]
    result = evaluate_comparison(p, candidate, refs, campaign_seed=123).to_payload()
    assert (result["outcome"], result["reason"], result["deciding_scope"]) == (
        "comparison_unavailable",
        "insufficient_repeats",
        "causal",
    )
    assert result["scopes"][0]["exact"] is True
    assert result["scopes"][1]["lower"] is None


def test_statistical_empty_excluded_scope_and_incomplete_coverage_are_safe():
    p, refs = built(mode="statistical", repeats=2, exclusions=(("c", 1), ("c", 2)))
    result = evaluate_comparison(p, [row(1, repeat=i) for i in (1, 2)], refs, campaign_seed=123)
    assert result.reason == "empty_support"
    p, refs = built(mode="statistical", repeats=2)
    assert evaluate_comparison(p, [], refs, campaign_seed=123).reason == "candidate_incomplete"


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("margin,tolerance", [(0, 0.5), (0.5, 0.5), (0, 0)])
def test_statistical_varying_repeats_decide_from_exact_lower_bounds(fresh, margin, tolerance):
    from evolution.tree.family15_schema import decode_rational as dq

    p, refs = built(
        mode="statistical",
        repeats=10,
        fresh=fresh,
        margin=margin,
        tolerance=tolerance,
        allowance=0.125,
        confidence=(0.75, 0.75),
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    refs = [
        replace(r, metrics={"score": 0.25 if r.case_id == "a" and r.repeat % 2 else 0.5})
        for r in refs
    ]
    body = p.to_payload()
    if not fresh:
        for scope in body["scopes"]:
            for c in scope["cases"]:
                c["reference_values"] = [
                    q(Fraction(r.metrics["score"])) for r in refs if r.case_id == c["case_id"]
                ]
    p = ComparisonProfile.from_payload(body, primary="score", groups=dict(p.groups))
    candidate = [
        row(
            (1 if r.repeat % 2 else 0.5) if r.case_id == "a" else (0.5 if r.repeat % 2 else 0.25),
            case=r.case_id,
            repeat=r.repeat,
        )
        for r in refs
    ]
    result = evaluate_comparison(p, candidate, refs, campaign_seed=123).to_payload()
    f, c = result["scopes"]
    passed = dq(f["lower"]) - Fraction(0.125) > Fraction(margin) and dq(c["lower"]) - Fraction(
        0.125
    ) >= -Fraction(tolerance)
    assert (result["outcome"] == "passed") == passed == (margin == 0 and tolerance == 0.5)
    assert result["caveats"] == ["intervals_empirical"]


def test_statistical_retains_critical_veto():
    p, refs = built(mode="statistical", repeats=2)
    assert (
        evaluate_comparison(
            p, [row(1, repeat=i, critical=False) for i in (1, 2)], refs, campaign_seed=123
        ).reason
        == "critical_failed"
    )


def test_statistical_critical_failure_precedes_insufficient_retained_repeats():
    from evolution.tree.family15_schema import validate_comparison_data
    from evolution.tree.storage.envelope import SchemaError

    p, refs = built(mode="statistical", repeats=2, exclusions=(("c", 2),))
    result = evaluate_comparison(
        p, [row(1, repeat=i, critical=False) for i in (1, 2)], refs, campaign_seed=123
    ).to_payload()
    assert (result["outcome"], result["reason"]) == ("comparison_not_passed", "critical_failed")
    scope = result["scopes"][0]
    assert (scope["rule"], scope["status"], scope["unavailable"]) == (
        "bootstrap",
        "unavailable",
        "insufficient_repeats",
    )
    assert scope["lower"] is scope["exact"] is None
    validate_comparison_data(result)
    for unavailable in ("empty_support", "reference_incomplete", "insufficient_calibration"):
        scope["unavailable"] = unavailable
        with pytest.raises(SchemaError):
            validate_comparison_data(result)
    scope["unavailable"] = "insufficient_repeats"
    result["reason"] = "threshold_not_passed"
    with pytest.raises(SchemaError):
        validate_comparison_data(result)


@pytest.mark.parametrize("mode", ["point", "confidence"])
def test_critical_unavailable_exception_does_not_relax_existing_modes(mode):
    from evolution.tree.family15_schema import validate_comparison_data
    from evolution.tree.storage.envelope import SchemaError

    p, refs = built(mode=mode, repeats=2, exclusions=(("c", 2),))
    result = evaluate_comparison(
        p, [row(1, repeat=i, critical=False) for i in (1, 2)], refs
    ).to_payload()
    result["scopes"][0].update(status="unavailable", unavailable="insufficient_repeats")
    if mode == "confidence":
        result["scopes"][0]["enclosure"] = None
    with pytest.raises(SchemaError):
        validate_comparison_data(result)


@pytest.mark.parametrize("global_mode", ["point", "off"])
def test_statistical_global_only_uses_no_local_bootstrap_kernel(monkeypatch, global_mode):
    from evolution.tree import family15_statistics as statistics

    def forbidden(*args, **kwargs):
        raise AssertionError("local bootstrap kernel called for global-only comparison")

    for name in (
        "pairing_of",
        "joint_replicates",
        "one_sided_lower",
        "exact_scope",
        "point_contrast",
    ):
        monkeypatch.setattr(statistics, name, forbidden)
    p, refs = built(
        mode="statistical", repeats=10, scopes={"global": ["c"]}, global_mode=global_mode
    )
    result = evaluate_comparison(
        p, [row(1, repeat=i) for i in range(1, 11)], refs, campaign_seed=123
    ).to_payload()
    assert result["outcome"] == "passed"
    assert result["scopes"][0]["rule"] == global_mode
    assert result["caveats"] == ["intervals_empirical"]


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered"])
def test_focus_numbers_equal_single_point_confidence_unfiltered(mode):
    from evolution.tree.case_comparison import evaluate_causal_after_focus, evaluate_focus

    p, refs = built(
        stages="focus_first",
        mode=mode,
        groups={"b": "g", "a": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        tolerance=1,
        margin=-1,
    )
    rows = [row(0.5, case=c) for c in ("b", "a")]
    single = evaluate_comparison(p, rows, refs).to_payload()
    focus = evaluate_focus(p, rows, refs)
    assert focus.to_payload()["scopes"] == single["scopes"][:1]
    assert evaluate_causal_after_focus(p, focus, rows, refs).to_payload() == single


def test_reason_precedence_differs_only_when_focus_fails():
    from evolution.tree.case_comparison import evaluate_focus

    p, refs = built(
        stages="focus_first", groups={"a": "g", "b": "g"}, scopes={"focus": ["a"], "causal": ["b"]}
    )
    rows = [row(0, case="a"), row(1, case="b", critical=False)]
    assert evaluate_comparison(p, rows, refs).reason == "critical_failed"
    focus = evaluate_focus(p, rows, refs).to_payload()
    assert focus["reason"] == "threshold_not_passed"
    assert focus["deciding_scope"] == "focus"
    assert focus["coverage"]["candidate"]["required"] == 1


def test_statistical_per_stage_seed_reproducible():
    from evolution.tree.case_comparison import evaluate_causal_after_focus, evaluate_focus

    p, refs = built(
        stages="focus_first",
        mode="statistical",
        repeats=3,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    rows = [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2, 3)]
    focus = evaluate_focus(p, rows, refs, campaign_seed=p.policy.random_seed)
    assert focus == evaluate_focus(p, rows, refs, campaign_seed=p.policy.random_seed)
    joint = evaluate_comparison(p, rows, refs, campaign_seed=p.policy.random_seed).to_payload()[
        "bootstrap"
    ]
    seed = focus.to_payload()["bootstrap"]
    assert seed["scope"] == "focus" and seed["seed"] != joint["seed"]
    full = evaluate_causal_after_focus(
        p, focus, rows, refs, campaign_seed=p.policy.random_seed
    ).to_payload()
    assert full["bootstrap"]["scope"] == "causal"
    assert full["scopes"][0] == focus.to_payload()["scopes"][0]


@pytest.mark.parametrize("mode", ["point", "confidence", "statistical", "unfiltered"])
@pytest.mark.parametrize("empty", [False, True])
def test_focus_rejection_does_not_compute_causal_numbers(mode, empty):
    from evolution.tree.case_comparison import evaluate_focus, focus_rejection

    p, refs = built(
        stages="focus_first",
        mode=mode,
        repeats=2,
        fresh=True,
        groups={"a": "g"} if empty else {"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": [] if empty else ["b"]},
        tolerance="measured_noise" if mode != "unfiltered" else 0,
    )
    rows = [row(0, case="a", repeat=i, critical=mode != "unfiltered") for i in (1, 2)]
    focus = evaluate_focus(p, rows, refs, campaign_seed=p.policy.random_seed)
    result = focus_rejection(p, focus).to_payload()
    assert result["scopes"][0] == focus.to_payload()["scopes"][0]
    causal = result["scopes"][1]
    assert causal["status"] == ("skipped" if empty else "not_measured")
    assert causal["point"] is None
    assert "calibration" not in result
    assert result["coverage"]["candidate"]["required"] == 2


def test_causal_seed_is_gate_only_and_hashes_the_declared_inputs():
    import hashlib

    from evolution.tree.confidence_statistics import METHOD_VERSION, derive_seed
    from evolution.tree.execution.journal_identity import canonical_json

    inputs = dict(
        campaign_seed=123,
        binding="a" * 64,
        scope="causal",
        prefixes=["candidate", "parent"],
        look=3,
        purpose="gate",
    )
    assert derive_seed(**inputs) == int.from_bytes(
        hashlib.sha256(canonical_json(dict(method=METHOD_VERSION, **inputs))).digest()[:16], "big"
    )
    with pytest.raises(ValueError, match="scope and purpose"):
        derive_seed(**(inputs | {"purpose": "global"}))


@pytest.mark.parametrize("mode", ["point", "confidence", "statistical", "unfiltered"])
@pytest.mark.parametrize("empty", [False, True])
def test_scoped_results_validate_against_the_complete_binding(mode, empty):
    from evolution.tree.case_comparison import (
        evaluate_causal_after_focus,
        evaluate_focus,
        focus_rejection,
    )
    from evolution.tree.family15_schema import validate_comparison_data

    p, refs = built(
        stages="focus_first",
        mode=mode,
        repeats=2,
        fresh=True,
        groups={"a": "g"} if empty else {"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": [] if empty else ["b"]},
        tolerance="measured_noise" if mode != "unfiltered" else 0,
        fallback=mode == "confidence",
    )
    for passing in (False, True):
        rows = [
            row(1 if passing else 0, case=c, repeat=i, critical=passing or mode != "unfiltered")
            for c, _ in p.groups
            for i in (1, 2)
        ]
        focus = evaluate_focus(p, rows, refs, campaign_seed=p.policy.random_seed)
        validate_comparison_data(focus.to_payload(), binding=p.to_payload(), policy=p.policy)
        result = (
            evaluate_causal_after_focus(p, focus, rows, refs, campaign_seed=p.policy.random_seed)
            if focus.outcome == "passed"
            else focus_rejection(p, focus)
        )
        validate_comparison_data(result.to_payload(), binding=p.to_payload(), policy=p.policy)


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered", "statistical"])
def test_causal_critical_failure_keeps_single_deciding_scope(mode):
    from evolution.tree.case_comparison import evaluate_causal_after_focus, evaluate_focus

    p, refs = built(
        stages="focus_first",
        mode=mode,
        repeats=2,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        margin=-1,
    )
    rows = [row(1, case=c, repeat=i, critical=c == "a") for c in ("a", "b") for i in (1, 2)]
    single = evaluate_comparison(p, rows, refs, campaign_seed=p.policy.random_seed).to_payload()
    focus = evaluate_focus(p, rows, refs, campaign_seed=p.policy.random_seed)
    assert focus.outcome == "passed"
    staged = evaluate_causal_after_focus(
        p, focus, rows, refs, campaign_seed=p.policy.random_seed
    ).to_payload()
    assert staged["outcome"] == single["outcome"] == "comparison_not_passed"
    assert staged["reason"] == single["reason"] == "critical_failed"
    assert staged["scopes"] == single["scopes"]
    assert staged["deciding_scope"] == single["deciding_scope"] == "focus"


def test_seeded_focus_first_equivalence_over_400_trials():
    import random

    from evolution.tree.case_comparison import (
        evaluate_causal_after_focus,
        evaluate_focus,
        focus_rejection,
    )

    rng = random.Random(7)
    exercised = set()
    for trial in range(400):
        mode = rng.choice(["point", "confidence", "unfiltered"])
        fresh = rng.random() < 0.4
        focus_cases = [f"f{i}" for i in range(rng.randint(1, 4))]
        causal_cases = [f"k{i}" for i in range(rng.randint(0, 4))]
        groups = {case: rng.choice(["g1", "g2", "g3"]) for case in focus_cases + causal_cases}
        repeats = rng.choice([1, 2, 3])
        parent = {
            case: [rng.choice([0, 0.25, 0.5, 0.75, 1]) for _ in range(repeats)] for case in groups
        }
        options = dict(
            mode=mode,
            groups=groups,
            scopes={"focus": focus_cases, "causal": causal_cases},
            repeats=repeats,
            fresh=fresh,
            tolerance=rng.choice([0, 0.1, 0.25]),
            margin=rng.choice([0, -0.1, 0.1]),
            parent_values=parent,
            global_mode=rng.choice(["point", "off"]),
        )
        single_profile, single_refs = built(**options)
        scoped_profile, refs = built(stages="focus_first", **options)
        candidate = [
            row(
                rng.choice([0, 0.25, 0.5, 0.75, 1]),
                case=case,
                repeat=repeat,
                critical=rng.random() > 0.08,
            )
            for case in groups
            for repeat in range(1, repeats + 1)
        ]
        seed = single_profile.policy.random_seed
        single = evaluate_comparison(
            single_profile, candidate, single_refs, campaign_seed=seed
        ).to_payload()
        full = evaluate_comparison(scoped_profile, candidate, refs, campaign_seed=seed).to_payload()
        # The binding includes the selected policy; all numerical evidence is identical.
        single["binding_digest"] = full["binding_digest"]
        assert single == full, trial
        focus = evaluate_focus(scoped_profile, candidate, refs, campaign_seed=seed)
        if focus.outcome == "passed":
            joined = evaluate_causal_after_focus(
                scoped_profile, focus, candidate, refs, campaign_seed=seed
            ).to_payload()
            assert joined == single, trial
        else:
            rejected = focus_rejection(scoped_profile, focus).to_payload()
            assert rejected["outcome"] == single["outcome"] == "comparison_not_passed", trial
            assert rejected["scopes"][0] == single["scopes"][0], trial
            assert rejected["reason"] == single["reason"] or (
                single["reason"],
                rejected["reason"],
            ) == ("critical_failed", "threshold_not_passed"), trial
            causal = rejected["scopes"][1]
            assert causal["point"] is None
            assert causal["status"] == ("not_measured" if causal_cases else "skipped")
        exercised.add((mode, fresh, focus.outcome))
    assert exercised == {
        (mode, fresh, outcome)
        for mode in ("point", "confidence", "unfiltered")
        for fresh in (False, True)
        for outcome in ("passed", "comparison_not_passed")
    }
