"""Isolated family-15 contracts; reader/writer enablement is a later task."""

from __future__ import annotations

import copy
import hashlib
import importlib
import sys
from fractions import Fraction
from functools import partial

import pytest

from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
from evolution.tree.storage.envelope import Envelope, SchemaError
from tests.family15_fakes import (
    family15_created,
    measurement15,
    publication15,
    reference15,
    v16_document,
)
from tests.tree.manifest.test_manifest_v15 import load


def schema():
    assert importlib.util.find_spec("evolution.tree.family15_schema") is not None, (
        "family15 closed contracts are missing"
    )
    return importlib.import_module("evolution.tree.family15_schema")


@pytest.mark.parametrize("count", [None, 1, 0, True, "1"])
def test_candidate_inspected_withheld_family15(count):
    payload = dict(round=1, attempt="r01-a01", inspection=1, components={})
    if count is not None:
        payload["components_withheld"] = count
    if count is None or type(count) is int and count > 0:
        schema().validate_family15_payload("candidate_inspected", payload)
    else:
        with pytest.raises(SchemaError):
            schema().validate_family15_payload("candidate_inspected", payload)


def q(value=0):
    value = Fraction(value)
    return dict(
        format="rational-hex/1",
        numerator=format(value.numerator, "x"),
        denominator=format(value.denominator, "x"),
    )


def binding(mode="point", *, global_scope=False):
    scopes = [
        dict(
            name="global" if global_scope else "focus",
            cases=[
                dict(
                    case_id="c",
                    group="g",
                    weight=q(1),
                    repeats=[1],
                    reference_values=[q(Fraction(1, 2))],
                )
            ],
        )
    ]
    if not global_scope:
        scopes.append(dict(name="causal", cases=[]))
    for scope in scopes:
        scope.update(
            rule="bound" if mode == "confidence" else "off" if mode == "unfiltered" else "point",
            declared_cases=[c["case_id"] for c in scope["cases"]],
        )
        if mode != "unfiltered":
            scope.update(threshold=q(), strict=scope["name"] != "causal")
        if mode == "confidence":
            scope.update(
                confidence=q(Fraction(19, 20)),
                units=[dict(unit_id="c", cases=["c"], weight=q(1))] if scope["cases"] else [],
            )
    result = dict(
        format="comparison-data/1",
        kind="binding",
        epoch=0,
        policy_digest="d" * 64,
        mode=mode,
        execution_noise={"kind": "negligible"},
        candidate=measurement15(),
        reference=measurement15("reference", settled=True),
        reference_kind="stored",
        scopes=scopes,
        excluded_reference=[],
    )
    if mode == "confidence":
        result["statistics"] = dict(
            method="bounded-weighted-mean/1", unit="case", profile="bounded-weighted-mean/1"
        )
    return result


def source_summary(measurement):
    return dict(
        measurement_id=measurement["measurement_id"],
        identity_digest=measurement["identity_digest"],
        candidate_digest=measurement["identity"]["candidate_digest"],
        prefix=measurement["prefix"],
    )


def gate_policy(tmp_path, *, mode="confidence", fallback=True, tolerance=0, fresh=False):
    doc = v16_document()
    doc["campaign"]["evaluation"]["repeats_per_question"] = 2
    gate = doc["campaign"]["gate"]
    gate.update(mode=mode, fresh_parent=fresh)
    gate["causal"]["tolerance"] = tolerance
    if mode in {"confidence", "statistical"}:
        gate["focus"]["confidence"] = gate["causal"]["confidence"] = 0.95
    if mode == "confidence":
        gate["statistics"] = {"method": "bounded-weighted-mean/1", "unit": "case"}
    if fallback:
        gate["bound_fallback"] = "point"
    return load(tmp_path, doc).workflow.campaign


def calibration(source="stored", *, allowance=Fraction(1, 8), degenerate=False):
    return schema().calibration_payload(
        source=source,
        measurement_id="reference",
        prefix=reference15("journal_prefix", "reference"),
        retained=1,
        seed_input="e" * 64,
        allowance=allowance,
        degenerate=degenerate,
    )


def measured_noise_binding(policy):
    """Shape-only two-scope profile, with no claim to authenticated observation bytes."""
    value = binding("confidence") | {"policy_digest": policy.digest}
    value["reference_kind"] = "fresh" if policy.fresh_parent else "stored"
    value["reference"] = measurement15("reference", settled=not policy.fresh_parent)
    for scope, case_id in zip(value["scopes"], ("c", "d"), strict=True):
        case = dict(case_id=case_id, group="g", weight=q(1), repeats=[1, 2])
        if not policy.fresh_parent:
            case["reference_values"] = [q(Fraction(1, 2))] * 2
        scope.update(
            declared_cases=[case_id],
            cases=[case],
            units=[dict(unit_id=case_id, cases=[case_id], weight=q(1))],
        )
    for side in ("candidate", "reference"):
        measurement = value[side]
        measurement["identity"]["schedule"] = [
            dict(case_id=case_id, sample_id=f"s{repeat}", repeat=repeat, requested_seed=42)
            for case_id in ("c", "d")
            for repeat in (1, 2)
        ]
        measurement["identity_digest"] = RunIdentityV1.from_payload(measurement["identity"]).digest
    return value


def point_fallback(scope):
    scope.update(
        rule="point",
        fallback={
            "reason": ("focus_unattainable" if scope["name"] == "focus" else "causal_unconfirmable")
        },
    )
    scope.pop("confidence")
    scope.pop("units")


def result_for_binding(bound):
    value = result_data("confidence")
    value["binding_digest"] = hashlib.sha256(canonical_json(bound)).hexdigest()
    template = value["scopes"][0]
    value["scopes"] = []
    for scope in bound["scopes"]:
        result = copy.deepcopy(template) | {"name": scope["name"], "rule": scope["rule"]}
        if "fallback" in scope:
            result["fallback"] = copy.deepcopy(scope["fallback"])
            result.pop("confidence")
            result.pop("enclosure")
        value["scopes"].append(result)
    for side in ("candidate", "reference"):
        measurement = copy.deepcopy(bound[side])
        measurement["prefix"] = reference15("journal_prefix", measurement["measurement_id"])
        value[side] = source_summary(measurement)
        value["coverage"][side] = dict(required=4, settled=4, admissible=4)
    value["retained_pairs"] = [
        dict(candidate=c, reference=r)
        for c, r in zip(
            bound["candidate"]["identity"]["schedule"],
            bound["reference"]["identity"]["schedule"],
            strict=True,
        )
    ]
    value.update(variation="not_requested", repeat_diagnostics=[])
    return value


def focus_fallback_calibration_failure(policy):
    bound = measured_noise_binding(policy)
    point_fallback(bound["scopes"][0])
    value = result_for_binding(bound)
    value.update(
        outcome="comparison_unavailable", reason="insufficient_calibration", deciding_scope="causal"
    )
    value["scopes"][1].update(
        status="unavailable", unavailable="insufficient_calibration", point=None, enclosure=None
    )
    return bound, value


def global_binding(candidate, champion):
    result = binding(global_scope=True)
    result.update(candidate=copy.deepcopy(candidate), reference=copy.deepcopy(champion))
    return result


def result_data(mode="point"):
    s = dict(
        name="focus",
        rule="bound" if mode == "confidence" else "off" if mode == "unfiltered" else "point",
        status="available",
        declared_count=1,
        point=q(Fraction(1, 4)),
        unit_count=1,
        mass=q(1),
        excluded_mass=q(),
        unavailable=None,
    )
    if mode == "confidence":
        s.update(
            confidence=q(Fraction(19, 20)),
            enclosure=dict(lower=q(-1), upper=q(Fraction(-1) + Fraction(1, 2**64)), steps=64),
        )
    return dict(
        format="comparison-data/1",
        kind="result",
        binding_digest="a" * 64,
        candidate=source_summary(measurement15(settled=True)),
        reference=source_summary(measurement15("reference", settled=True)),
        mode=mode,
        scopes=[s],
        coverage={
            "candidate": {"required": 1, "settled": 1, "admissible": 1},
            "reference": {"required": 1, "settled": 1, "admissible": 1},
        },
        retained_pairs=[
            {
                "candidate": measurement15()["identity"]["schedule"][0],
                "reference": measurement15("reference")["identity"]["schedule"][0],
            }
        ],
        excluded_reference=[],
        repeat_diagnostics=[dict(case_id="c", variation=None, unavailable="insufficient_repeats")],
        variation="unavailable",
        deciding_scope="focus",
        outcome="comparison_not_passed",
        reason="threshold_not_passed",
    )


def event_payloads():
    ref, pub = reference15(), publication15()
    common = dict(round=1, attempt_id="attempt")
    source = dict(
        measurement_id="candidate",
        identity_digest="a" * 64,
        candidate_digest="b" * 64,
        prefix=reference15("journal_prefix", "candidate"),
    )
    return {
        "auxiliary_payload_declared": {
            key: value for key, value in pub["declaration"].items() if key != "declaration_ref"
        },
        "attempt_opened": dict(
            round=1,
            attempt_index=1,
            attempt_id="attempt",
            parent_version="v1",
            parent_digest="c" * 64,
            path="early_global",
        ),
        "attempt_closed": dict(
            attempt_id="attempt",
            outcome="rejected",
            reason="structurally_unattainable",
            path="early_global",
            evidence=ref,
        ),
        "comparison_preflight_failed": dict(
            **common,
            epoch=0,
            phase="early_global",
            mode="point",
            data=pub,
            reason="structurally_unattainable",
        ),
        "comparison_bound": dict(
            **common,
            epoch=0,
            data=pub,
            candidate=source,
            reference=source_summary(measurement15("reference", settled=True)),
            reference_kind="stored",
        ),
        "comparison_completed": dict(
            **common,
            binding=ref,
            data=pub,
            outcome="comparison_not_passed",
            reason="threshold_not_passed",
            deciding_scope="focus",
            candidate=source,
            reference=source_summary(measurement15("reference", settled=True)),
        ),
        "candidate_gate_sealed": dict(**common, data=pub, source=source, slots_digest="d" * 64),
        "candidate_completion_bound": dict(
            **common,
            data=pub,
            finalist_closure=ref,
            gate_seal=ref,
            measurement_id="completion",
            identity_digest="d" * 64,
            pairs_digest="e" * 64,
            missing_count=0,
            required=1,
            imported=1,
        ),
        "gate_measurement_completed": dict(
            round=1,
            attempt_id="a",
            binding=reference15(),
            side="candidate",
            source=source,
            required=1,
            admissible=1,
        ),
        "candidate_completion_completed": dict(
            **common, binding=ref, source=source, required=1, admissible=1
        ),
        "final_measurement_bound": dict(
            **common,
            data=pub,
            verification="reuse",
            candidate_source="completion",
            champion_side="stored",
            finalist_closure=ref,
            source_ref=ref,
            candidate=source,
            champion=source_summary(measurement15("reference", settled=True)),
        ),
        "global_comparison_completed": dict(
            **common,
            binding=ref,
            data=pub,
            outcome="passed",
            reason="passed",
            deciding_scope="global",
            accepted=True,
            candidate=source,
            reference=source_summary(measurement15("reference", settled=True)),
        ),
        "selection_decided": dict(
            round=1,
            data=pub,
            reason="automatic",
            selected_candidate="b" * 64,
            selected_evidence=source,
            next_champion=source,
        ),
        "diagnostic_import_declared": dict(
            measurement_id="baseline",
            data=pub,
            bundle=publication15("baseline_diagnostics"),
            rows_digest="a" * 64,
            scheduled=1,
            included=1,
            sufficiency="complete",
            authentication={
                "score": "journal_bound",
                "request": "authenticated",
                "stdout": "captured_at_import",
            },
        ),
    }


def test_exact_rational_round_trip_exceeds_decimal_guard_without_mutating_it():
    api = schema()
    original = sys.get_int_max_str_digits()
    huge = Fraction(2**129606 + 1, 2**129541)
    encoded = api.encode_rational(huge)
    assert (
        api.decode_rational(importlib.import_module("json").loads(canonical_json(encoded))) == huge
    )
    assert sys.get_int_max_str_digits() == original
    assert api.encode_rational(Fraction(0)) == q(0)


@pytest.mark.parametrize(
    "bad",
    [
        dict(numerator="00"),
        dict(numerator="-0"),
        dict(numerator="+1"),
        dict(numerator="A"),
        dict(numerator="0x1"),
        dict(denominator="0"),
        dict(denominator="-1"),
        dict(numerator="2", denominator="4"),
        dict(numerator=1),
        dict(extra=True),
        dict(format="rational/1"),
    ],
)
def test_noncanonical_rationals_refused(bad):
    with pytest.raises(SchemaError):
        schema().decode_rational(q() | bad)


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered"])
def test_binding_closed_mode_shapes_and_unique_schedules(mode):
    api, value = schema(), binding(mode)
    api.validate_comparison_data(value)
    for mutate in [
        lambda p: p.update(extra=1),
        lambda p: p["candidate"]["identity"]["schedule"].append(
            p["candidate"]["identity"]["schedule"][0]
        ),
        lambda p: p["scopes"][0]["cases"][0].update(weight=q(2)),
        lambda p: p.update(statistics=None) if mode != "confidence" else p.pop("statistics"),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises((SchemaError, ValueError)):
            api.validate_comparison_data(bad)


def bootstrap_binding(*, global_scope=False):
    value = binding("confidence", global_scope=global_scope)
    value.update(mode="statistical", statistics={"method": "f13-bootstrap/1"})
    for scope in value["scopes"]:
        scope.update(rule="bootstrap")
        scope.pop("units")
    return value


def bootstrap_result():
    from evolution.tree.confidence_statistics import derive_seed

    value = result_data()
    value.update(mode="statistical", caveats=["exact_contrast", "intervals_empirical"])
    value["scopes"][0].update(
        rule="bootstrap",
        confidence=q(Fraction(19, 20)),
        lower=q(Fraction(1, 4)),
        exact=True,
        pairing="exact",
    )
    value["bootstrap"] = dict(
        method="f13-bootstrap/1",
        campaign_seed=42,
        binding=value["binding_digest"],
        scope="joint",
        prefixes=[value[side]["prefix"]["locator"] for side in ("candidate", "reference")],
        look=2,
        purpose="gate",
    )
    value["bootstrap"]["seed"] = derive_seed(
        **{k: v for k, v in value["bootstrap"].items() if k != "method"}
    )
    return value


def test_bootstrap_shapes_keep_exact_lower_and_the_kernel_method():
    api = schema()
    api.validate_comparison_data(bootstrap_binding())
    api.validate_comparison_data(bootstrap_result())
    for mutate in (
        lambda p: p["scopes"][0].update(lower=q(Fraction(1, 8))),
        lambda p: p["scopes"][0].update(pairing="paired"),
        lambda p: p["scopes"][0].update(exact=1),
        lambda p: p.update(caveats=["exact_contrast"]),
        lambda p: p.update(caveats=["intervals_empirical", "intervals_empirical"]),
        lambda p: p.update(caveats=["intervals_empirical", "unknown"]),
    ):
        value = bootstrap_result()
        mutate(value)
        with pytest.raises(SchemaError):
            api.validate_comparison_data(value)
    value = bootstrap_binding()
    value["statistics"]["method"] = "bounded-weighted-mean/1"
    with pytest.raises(SchemaError):
        api.validate_comparison_data(value)
    with pytest.raises(SchemaError):
        api.validate_comparison_data(bootstrap_binding(global_scope=True))


def test_statistical_results_require_bootstrap_metadata():
    value = bootstrap_result()
    schema().validate_comparison_data(value)
    value.pop("bootstrap")
    with pytest.raises(SchemaError, match="bootstrap metadata"):
        schema().validate_comparison_data(value)


def test_statistical_preflight_is_early_global_point_only():
    value = preflight() | {"mode": "statistical", "statistics": {"method": "f13-bootstrap/1"}}
    schema().validate_comparison_data(value)
    event = event_payloads()["comparison_preflight_failed"] | {"mode": "statistical"}
    schema().validate_family15_payload("comparison_preflight_failed", event)
    for payload in (value, event):
        bad = dict(payload, phase="attempt")
        with pytest.raises(SchemaError):
            if "format" in bad:
                schema().validate_comparison_data(bad)
            else:
                schema().validate_family15_payload("comparison_preflight_failed", bad)


@pytest.mark.parametrize("reason", ["insufficient_repeats", "insufficient_calibration"])
def test_new_unavailable_reasons_close_only_evidenced_family15_attempts(reason):
    value = result_data()
    value.update(outcome="comparison_unavailable", reason=reason)
    value["scopes"][0].update(status="unavailable", unavailable=reason, point=None)
    schema().validate_comparison_data(value)
    event = event_payloads()["comparison_completed"]
    event.update(outcome="comparison_unavailable", reason=reason)
    schema().validate_family15_payload("comparison_completed", event)
    closed = event_payloads()["attempt_closed"] | {"path": "candidate", "reason": reason}
    schema().validate_family15_payload("attempt_closed", closed)
    with pytest.raises(SchemaError):
        schema().validate_family15_payload("attempt_closed", closed | {"path": "early_global"})
    with pytest.raises(SchemaError):
        schema().validate_family15_payload(
            "attempt_closed",
            dict(
                attempt_id="attempt",
                outcome="insufficient_evidence",
                reason=reason,
                look=None,
                code=None,
            ),
        )


def test_fallback_requires_the_declared_policy_and_scope_specific_reason(tmp_path):
    policy = gate_policy(tmp_path)
    value = binding("confidence") | {"policy_digest": policy.digest}
    scope = value["scopes"][0]
    scope.update(rule="point", fallback={"reason": "focus_unattainable"})
    scope.pop("confidence")
    scope.pop("units")
    schema().validate_comparison_data(value, policy=policy)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)
    undeclared = gate_policy(tmp_path, fallback=False)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(
            value | {"policy_digest": undeclared.digest}, policy=undeclared
        )
    for mutate in (
        lambda p: p["scopes"][0]["fallback"].update(reason="causal_unconfirmable"),
        lambda p: p["scopes"][0]["fallback"].update(extra=True),
        lambda p: p["scopes"][0].pop("fallback"),
        lambda p: p["scopes"][1].update(fallback={"reason": "causal_unconfirmable"}),
    ):
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(bad, policy=policy)
    result = result_data()
    result.update(
        mode="confidence", binding_digest=hashlib.sha256(canonical_json(value)).hexdigest()
    )
    result["scopes"][0]["fallback"] = {"reason": "focus_unattainable"}
    # A normally bound substitution is unchanged in the result (not R8).
    result["scopes"].append(
        dict(
            name="causal",
            rule="bound",
            status="skipped",
            declared_count=0,
            point=None,
            unit_count=0,
            mass=q(),
            excluded_mass=q(),
            unavailable=None,
            confidence=q(Fraction(19, 20)),
            enclosure=None,
        )
    )
    schema().validate_comparison_data(result, policy=policy, binding=value)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(result, policy=policy)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(result, binding=value)
    for mode in ("point", "unfiltered", "statistical"):
        bad = copy.deepcopy(value)
        bad["mode"] = mode
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(bad, policy=policy)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("tolerance", [0, "measured_noise"])
def test_result_side_fallback_substitution_is_only_r8(tmp_path, fresh, tolerance):
    policy = gate_policy(tmp_path, fresh=fresh, tolerance=tolerance)
    bound = binding("confidence") | {"policy_digest": policy.digest}
    # Move the retained case to causal; focus remains a valid empty-support scope.
    bound["scopes"][0]["cases"], bound["scopes"][1]["cases"] = [], bound["scopes"][0]["cases"]
    bound["scopes"][0]["declared_cases"], bound["scopes"][1]["declared_cases"] = [], ["c"]
    bound["scopes"][0]["units"], bound["scopes"][1]["units"] = [], bound["scopes"][0]["units"]
    if fresh:
        bound.update(reference_kind="fresh", reference=measurement15("reference"))
        bound["scopes"][1]["cases"][0].pop("reference_values")
    value = result_data("confidence")
    value.update(
        binding_digest=hashlib.sha256(canonical_json(bound)).hexdigest(),
        outcome="comparison_unavailable",
        reason="empty_support",
    )
    focus = value["scopes"][0]
    causal = copy.deepcopy(focus)
    focus.update(
        status="unavailable",
        point=None,
        unit_count=0,
        declared_count=0,
        mass=q(),
        unavailable="empty_support",
        enclosure=None,
    )
    causal.update(name="causal", rule="point", fallback={"reason": "causal_unconfirmable"})
    causal.pop("confidence")
    causal.pop("enclosure")
    value["scopes"].append(causal)
    if fresh and tolerance == "measured_noise":
        value["calibration"] = calibration("fresh")
        schema().validate_comparison_data(value, policy=policy, binding=bound)
        for mutate in (
            lambda p: p.pop("calibration"),
            lambda p: p.update(binding_digest="f" * 64),
            lambda p: p["scopes"][1]["fallback"].update(reason="focus_unattainable"),
        ):
            bad = copy.deepcopy(value)
            mutate(bad)
            with pytest.raises(SchemaError):
                schema().validate_comparison_data(bad, policy=policy, binding=bound)
        # Unknown q must never authorize a causal fallback.
        value["reason"] = "insufficient_calibration"
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(value, policy=policy, binding=bound)
    else:
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(value, policy=policy, binding=bound)


@pytest.mark.parametrize("fresh", [False, True])
def test_focus_fallback_survives_causal_insufficient_calibration(tmp_path, fresh):
    policy = gate_policy(tmp_path, fresh=fresh, tolerance="measured_noise")
    bound, value = focus_fallback_calibration_failure(policy)
    # Focus was frozen from its ceilings independently of q; C remains bound.
    schema().validate_comparison_data(bound, policy=policy)
    schema().validate_comparison_data(value, policy=policy, binding=bound)
    assert value["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert value["scopes"][1]["rule"] == "bound" and "calibration" not in value


@pytest.mark.parametrize("fresh", [False, True])
def test_measured_noise_causal_binding_fallback_requires_stored_calibration(tmp_path, fresh):
    policy = gate_policy(tmp_path, fresh=fresh, tolerance="measured_noise")
    bound = measured_noise_binding(policy)
    point_fallback(bound["scopes"][1])
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(bound, policy=policy)


def test_stored_measured_noise_causal_binding_fallback_with_calibration_is_valid(tmp_path):
    policy = gate_policy(tmp_path, tolerance="measured_noise")
    bound = measured_noise_binding(policy)
    point_fallback(bound["scopes"][1])
    bound["calibration"] = calibration()
    bound["scopes"][1]["threshold"] = q(-Fraction(1, 8))
    schema().validate_comparison_data(bound, policy=policy)
    schema().validate_comparison_data(result_for_binding(bound), policy=policy, binding=bound)


def test_fresh_measured_noise_causal_binding_cannot_bypass_r8_with_matching_result(tmp_path):
    policy = gate_policy(tmp_path, fresh=True, tolerance="measured_noise")
    bound = measured_noise_binding(policy)
    point_fallback(bound["scopes"][1])
    value = result_for_binding(bound) | {"calibration": calibration("fresh")}
    # Even when the result is calibrated, the causal binding must have stayed bound.
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value, policy=policy, binding=bound)


@pytest.mark.parametrize("source", ["stored", "fresh"])
def test_calibration_payload_is_exact_closed_and_self_digest_bound(source):
    value = calibration(source)
    assert value["method"] == "f13-bootstrap/1"
    assert value["level"] == q(Fraction(995, 1000))
    assert value["allowance"] == q(Fraction(1, 8))
    assert (
        value["result_digest"]
        == hashlib.sha256(
            canonical_json({k: v for k, v in value.items() if k != "result_digest"})
        ).hexdigest()
    )
    data = binding() if source == "stored" else result_data()
    # Calibration requires a nonempty causal scope, not the focus-only fixture.
    data["scopes"][0]["name"] = "causal"
    if source == "stored":
        data["scopes"] = [
            dict(
                name="focus", rule="point", declared_cases=[], cases=[], threshold=q(), strict=True
            ),
            data["scopes"][0],
        ]
        data["scopes"][1]["strict"] = False
    else:
        data["deciding_scope"] = "causal"
    data["calibration"] = value
    schema().validate_comparison_data(data)
    for mutation in (
        {"retained": True},
        {"retained": -1},
        {"method": "other"},
        {"level": q(Fraction(99, 100))},
        {"seed_input": "not-a-digest"},
        {"allowance": q(-1)},
        {"degenerate": 1},
        {"degenerate": True},
        {"result_digest": "f" * 64},
        {"extra": None},
        {"source": "candidate"},
        {"prefix": reference15("journal_prefix", "other")},
    ):
        bad = copy.deepcopy(data)
        bad["calibration"].update(mutation)
        if "result_digest" not in mutation:
            bad["calibration"]["result_digest"] = hashlib.sha256(
                canonical_json(
                    {k: v for k, v in bad["calibration"].items() if k != "result_digest"}
                )
            ).hexdigest()
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(bad)
    bad = copy.deepcopy(data)
    bad["calibration"] = calibration("fresh" if source == "stored" else "stored")
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(bad)


def test_caveats_admit_the_four_declared_values_without_changing_old_payloads():
    value = result_data()
    before = canonical_json(value)
    schema().validate_comparison_data(value)
    assert canonical_json(value) == before
    for caveat in (
        "exact_contrast",
        "intervals_empirical",
        "noise_not_observed",
        "noise_allowance_empirical",
    ):
        schema().validate_comparison_data(value | {"caveats": [caveat]})
    for bad in (None, "intervals_empirical", ["unknown"], ["noise_not_observed"] * 2):
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(value | {"caveats": bad})


def test_zero_calibration_and_calibration_context_require_the_actual_policy(tmp_path):
    policy = gate_policy(tmp_path, mode="point", fallback=False, tolerance="measured_noise")
    value = binding() | {"policy_digest": policy.digest}
    value["scopes"][0]["name"] = "causal"
    value["scopes"][0]["strict"] = False
    value["scopes"] = [
        dict(name="focus", rule="point", declared_cases=[], cases=[], threshold=q(), strict=True),
        value["scopes"][0],
    ]
    value["calibration"] = calibration(allowance=Fraction(), degenerate=True)
    schema().validate_comparison_data(value, policy=policy)
    no_calibration = gate_policy(tmp_path, mode="point", fallback=False)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(
            value | {"policy_digest": no_calibration.digest}, policy=no_calibration
        )
    for mutate in (
        lambda p: p.update(calibration=None),
        lambda p: p.update(mode="unfiltered"),
        lambda p: p["calibration"].update(measurement_id="candidate"),
    ):
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(bad)


def test_results_distinguish_coverage_exclusions_and_unavailable_bounds():
    api = schema()
    value = result_data("confidence")
    api.validate_comparison_data(value)
    value["scopes"][0].update(
        status="unavailable",
        enclosure=None,
        unavailable="empty_support",
        point=None,
        unit_count=0,
        mass=q(),
        excluded_mass=q(1),
    )
    value.update(outcome="comparison_unavailable", reason="empty_support", retained_pairs=[])
    api.validate_comparison_data(value)
    value["outcome"] = "passed"
    with pytest.raises(SchemaError):
        api.validate_comparison_data(value)
    value = result_data()
    value.update(outcome="passed", reason="passed")
    value["coverage"]["candidate"]["admissible"] = 0
    value["excluded_reference"] = [measurement15()["identity"]["schedule"][0]]
    with pytest.raises(SchemaError):
        api.validate_comparison_data(value)


@pytest.mark.parametrize("event", list(event_payloads()))
def test_new_event_contracts_are_closed_and_bounded(event):
    api, value = schema(), event_payloads()[event]
    api.validate_family15_payload(event, value)
    with pytest.raises(SchemaError):
        api.validate_family15_payload(event, value | {"extra": None})
    with pytest.raises(SchemaError):
        api.validate_family15_payload(event, {})


def test_early_global_and_mode_specific_refusals():
    api = schema()
    value = event_payloads()["comparison_preflight_failed"]
    for extra in ({"candidate_digest": "a" * 64}, {"focus": []}, {"causal": []}):
        with pytest.raises(SchemaError):
            api.validate_family15_payload("comparison_preflight_failed", value | extra)
    with pytest.raises(SchemaError):
        api.validate_family15_payload("comparison_preflight_failed", value | {"mode": "unfiltered"})
    with pytest.raises(SchemaError):
        api.validate_family15_payload("campaign_suspended", {"reason": "comparison_unavailable"})


def test_creation_exact_policy_and_real_envelope_secret_guard(tmp_path):
    api = schema()
    doc = v16_document()
    doc["campaign"]["gate"]["execution_noise"] = dict(
        kind="bounded", allowance=0.1, source="PRIVATE-CANARY"
    )
    value = family15_created(load(tmp_path, doc).workflow.campaign)
    raw = dict(
        schema_version=1,
        run_id="run",
        sequence=1,
        timestamp="2026-10-18T00:00:00Z",
        event="campaign_created",
        operation_id=None,
        payload=value,
    )
    assert Envelope.from_payload(raw, family=api.FAMILY15_CONTRACT).payload == value
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=api.FAMILY15_CONTRACT, secrets=["PRIVATE-CANARY"])
    from evolution.tree.campaign_schema import campaign_family_for

    old = copy.deepcopy(raw)
    old["payload"].pop("diagnostic_import_format")
    old["payload"]["family_version"] = 14
    with pytest.raises(SchemaError):
        Envelope.from_payload(old, family=campaign_family_for(14))
    assert campaign_family_for(15) is api.FAMILY15_CONTRACT


def preflight(mode="point", *, threshold=Fraction(3, 4)):
    value = binding(mode, global_scope=True)
    value.pop("candidate")
    value.update(kind="preflight", phase="early_global")
    value["scopes"][0]["threshold"] = q(threshold)
    result = dict(
        name="global",
        status="calculated",
        ceilings=[dict(case_id="c", value=q(Fraction(1, 2)))],
        passes=False,
    )
    if mode == "point":
        result["delta_max"] = q(Fraction(1, 2))
    else:
        result.update(e_max={"kind": "finite", "value": q(2)}, c_max=q(Fraction(1, 2)))
    value["results"] = [result]
    return value


def test_preflight_empty_causal_and_support_boundaries_have_distinct_shapes():
    api = schema()
    point = preflight()
    api.validate_comparison_data(point)
    point["results"][0]["ceilings"][0]["value"] = q(1)
    with pytest.raises(SchemaError):
        api.validate_comparison_data(point)
    confidence = preflight("confidence")
    api.validate_comparison_data(confidence)
    confidence["results"][0]["e_max"] = {"kind": "positive_infinity"}
    with pytest.raises(SchemaError):
        api.validate_comparison_data(confidence)
    confidence["scopes"][0]["threshold"] = q(-1)
    confidence["results"][0]["c_max"] = None
    confidence["results"][0]["passes"] = True
    api.validate_comparison_data(confidence)
    confidence["scopes"][0]["threshold"] = q(1)
    confidence["results"][0]["e_max"] = None
    confidence["results"][0]["passes"] = False
    api.validate_comparison_data(confidence)
    local = binding("confidence")
    local.pop("candidate")
    local.update(
        kind="preflight",
        phase="attempt",
        results=[
            dict(confidence["results"][0], name="focus"),
            dict(name="causal", status="skipped"),
        ],
    )
    local["scopes"][0]["threshold"] = q(1)
    api.validate_comparison_data(local)


def test_confidence_can_use_global_point_without_invented_endpoints():
    value = preflight("confidence")
    value["scopes"][0].update(rule="point")
    value["scopes"][0].pop("confidence")
    value["scopes"][0].pop("units")
    value["results"][0].pop("e_max")
    value["results"][0].pop("c_max")
    value["results"][0]["delta_max"] = q(Fraction(1, 2))
    schema().validate_comparison_data(value)


def test_skipped_causal_never_becomes_an_unavailable_comparison():
    value = result_data("confidence")
    value["scopes"].append(
        dict(
            name="causal",
            rule="bound",
            status="skipped",
            declared_count=0,
            point=None,
            unit_count=0,
            mass=q(),
            excluded_mass=q(),
            unavailable=None,
            confidence=q(Fraction(99, 100)),
            enclosure=None,
        )
    )
    schema().validate_comparison_data(value)
    value["scopes"][1]["name"] = "focus"
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def completion_data():
    source, destination = measurement15(settled=True), measurement15()
    destination["measurement_id"] = "completion"
    slot = destination["identity"]["schedule"][0]
    row = dict(
        case_id=slot["case_id"],
        sample_id=slot["sample_id"],
        candidate_digest="b" * 64,
        outcome="completed",
        metrics={"score": 0.5},
        critical_pass=True,
        effective_seed=42,
        dependency_digest="dep",
        source=dict(
            run_id="run",
            measurement_id="candidate",
            identity_digest=source["identity_digest"],
            candidate_digest="b" * 64,
            retry_ordinal=0,
            rollout_digest="a" * 64,
            scoring_digest="a" * 64,
            condition_fields=[],
        ),
    )
    rows = [row]
    declaration = dict(
        mode="inherit",
        source_measurement_id="candidate",
        source_prefix_digest="a" * 64,
        pairs_digest=hashlib.sha256(canonical_json(rows)).hexdigest(),
        imported=1,
        scheduled=1,
        fields=[],
        origin_runs=1,
    )
    return dict(
        format="comparison-data/1",
        kind="completion",
        finalist_closure=reference15(),
        gate_seal=reference15(),
        destination=destination,
        source=source,
        imports=declaration,
        rows=rows,
        missing_slots=[],
    )


def test_completion_seal_and_final_reuse_bind_canonical_measurement_slots():
    api = schema()
    value = completion_data()
    api.validate_comparison_data(value)
    for mutate in [
        lambda p: p["rows"][0]["source"].update(candidate_digest="c" * 64),
        lambda p: p["missing_slots"].append(p["destination"]["identity"]["schedule"][0]),
        lambda p: p["imports"].update(pairs_digest="f" * 64),
        lambda p: p["rows"][0].update(extra=1),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(SchemaError):
            api.validate_comparison_data(bad)
    seal = dict(
        format="comparison-data/1",
        kind="gate_seal",
        measurement=value["source"],
        slots_digest=hashlib.sha256(
            canonical_json(value["source"]["identity"]["schedule"])
        ).hexdigest(),
    )
    api.validate_comparison_data(seal)
    final = dict(
        format="comparison-data/1",
        kind="final",
        verification="reuse",
        candidate_source="completion",
        candidate=value["destination"],
        champion_side="stored",
        champion=measurement15("reference", settled=True),
        finalist_closure=reference15(),
        source_ref=reference15(),
        comparison=global_binding(value["destination"], measurement15("reference", settled=True)),
        candidate_work=value["destination"]["identity"]["schedule"],
        schedule_order=[
            dict(measurement_id="completion", slot=value["destination"]["identity"]["schedule"][0])
        ],
    )
    api.validate_comparison_data(final)
    final["verification"] = "fresh"
    with pytest.raises(SchemaError):
        api.validate_comparison_data(final)


def test_typed_records_have_immutable_exact_authority():
    api = schema()
    raw = dict(
        schema_version=1,
        run_id="run",
        sequence=1,
        timestamp="2026-10-18T00:00:00Z",
        event="selection_decided",
        operation_id=None,
        payload=event_payloads()["selection_decided"],
    )
    envelope = Envelope.from_payload(raw, family=api.FAMILY15_CONTRACT)
    from evolution.tree.artifacts.artifact_payloads import AuxiliarySourceReference

    reference = AuxiliarySourceReference(
        1,
        "journal_record",
        "run",
        f"campaign.jsonl#bytes=0:{len(envelope.encode())}",
        hashlib.sha256(envelope.encode()).hexdigest(),
    )
    record = api.Family15Record(envelope, reference)
    state = api.Family15RoundState(selection=record)
    assert state.selection == record and state.finalists is None
    owned = record.payload
    owned["selected_candidate"] = "e" * 64
    assert record.payload != owned
    with pytest.raises(SchemaError):
        api.Family15RoundState(finalists=record)
    with pytest.raises(SchemaError):
        api.Family15AttemptState(gate_seal=record)


def test_exact_large_evidence_only_external_not_in_journal_records():
    api = schema()
    value = preflight("confidence")
    huge = Fraction(2**129606 + 1, 2**129541)
    value["results"][0].update(
        e_max=dict(kind="finite", value=q(huge)), c_max=q(1 - 1 / huge), passes=True
    )
    api.validate_comparison_data(value)
    assert len(canonical_json(value)) > api.MAX_RECORD_BYTES
    event = event_payloads()["comparison_preflight_failed"]
    api.validate_family15_payload("comparison_preflight_failed", event)
    assert len(canonical_json(event)) < 2048
    with pytest.raises(SchemaError):
        api.validate_family15_payload("comparison_preflight_failed", event | {"e_max": q(huge)})


def test_preflight_decision_matches_recorded_exact_evidence_and_threshold():
    value = preflight()
    value["results"][0]["passes"] = True
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)
    value = preflight("confidence", threshold=Fraction(-1))
    value["results"][0].update(e_max={"kind": "positive_infinity"}, c_max=None, passes=True)
    schema().validate_comparison_data(value)
    value["results"][0]["passes"] = False
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def test_ranking_ties_use_attempt_index_and_reference_full_evidence():
    api = schema()
    source = event_payloads()["candidate_gate_sealed"]["source"]
    entries = [
        dict(
            attempt_id=f"a{index}",
            attempt_index=index,
            candidate_digest="b" * 64,
            result=reference15(),
            evidence=source,
            accepted=True,
            point=q(Fraction(1, 2)),
        )
        for index in (2, 1)
    ]
    value = dict(
        format="comparison-data/1",
        kind="ranking",
        round_stop="all_attempts",
        entries=entries,
        proposed_attempt="a1",
    )
    api.validate_comparison_data(value)
    for mutate in [
        lambda p: p.update(proposed_attempt="a2"),
        lambda p: p["entries"][0].update(point=None),
        lambda p: p["entries"][0].update(candidate_digest="c" * 64),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(SchemaError):
            api.validate_comparison_data(bad)


def test_diagnostic_declaration_preserves_canonical_imports_and_authentication_labels():
    from evolution.tree.baseline_diagnostics import GAP_REASONS

    api, completion = schema(), completion_data()
    source = event_payloads()["candidate_gate_sealed"]["source"]
    source["identity_digest"] = completion["source"]["identity_digest"]
    value = dict(
        format="comparison-data/1",
        kind="diagnostic_import",
        destination=completion["destination"],
        source_prefixes=[source],
        rows_digest=completion["imports"]["pairs_digest"],
        slot_index=completion["rows"],
        gaps_digest="f" * 64,
        counts=dict.fromkeys(GAP_REASONS, 0),
        authentication=event_payloads()["diagnostic_import_declared"]["authentication"],
    )
    api.validate_comparison_data(value)
    for mutate in [
        lambda p: p["authentication"].update(stdout="historically_authenticated"),
        lambda p: p["slot_index"].append(p["slot_index"][0]),
        lambda p: p.update(source_prefixes=[]),
        lambda p: p["counts"].update(unknown=1),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(SchemaError):
            api.validate_comparison_data(bad)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_prefix",
        "invalid_publication_namespace",
        "unknown_kind",
        "malformed_scope",
        "bool_count",
    ],
)
def test_malformed_contracts_fail_with_schema_errors(mutation):
    api = schema()
    if mutation == "wrong_prefix":
        value = binding()
        value["reference"]["prefix"]["locator"] = "measurements/other/events.jsonl#bytes=0:100"
        validate = partial(api.validate_comparison_data, value)
    elif mutation == "unknown_kind":
        value = binding() | {"kind": "anything"}
        validate = partial(api.validate_comparison_data, value)
    elif mutation == "malformed_scope":
        value = result_data() | {"scopes": ["not-an-object"]}
        validate = partial(api.validate_comparison_data, value)
    elif mutation == "invalid_publication_namespace":
        value = event_payloads()["comparison_bound"]
        value["data"]["publication_ref"]["run_id"] = 123
        validate = partial(api.validate_family15_payload, "comparison_bound", value)
    else:
        value = event_payloads()["candidate_completion_completed"] | {"required": True}
        validate = partial(api.validate_family15_payload, "candidate_completion_completed", value)
    with pytest.raises(SchemaError):
        validate()


def test_parent_exclusions_allow_partial_reference_but_never_partial_candidate():
    api = schema()
    value = result_data()
    value.update(outcome="passed", reason="passed")
    value["coverage"]["reference"].update(required=2, settled=2, admissible=1)
    value["excluded_reference"] = [
        dict(case_id="excluded", sample_id="s1", repeat=1, requested_seed=42)
    ]
    api.validate_comparison_data(value)
    value["coverage"]["candidate"].update(required=2)
    with pytest.raises(SchemaError):
        api.validate_comparison_data(value)


def test_family15_created_rejects_family14_policy_and_bad_identity_digest(tmp_path):
    from tests.campaign_drivers import native14_created

    value = native14_created(round_stop="all_attempts")
    value.update(family_version=15, diagnostic_import_format="baseline-diagnostic-bundle/1")
    with pytest.raises(SchemaError):
        schema().validate_family15_payload("campaign_created", value)
    value = family15_created(load(tmp_path, v16_document()).workflow.campaign)
    value["identity_digest"] = "f" * 64
    with pytest.raises(SchemaError):
        schema().validate_family15_payload("campaign_created", value)


def test_selection_is_automatic_or_retains_without_an_eligible_candidate():
    api = schema()
    value = event_payloads()["selection_decided"]
    api.validate_family15_payload("selection_decided", value)
    for reason in ("approved", "rejected", "no_eligible_candidate"):
        with pytest.raises(SchemaError):
            api.validate_family15_payload("selection_decided", value | {"reason": reason})
    for key, extra in (("promotion", "automatic"), ("decision", None)):
        with pytest.raises(SchemaError):
            api.validate_family15_payload("selection_decided", value | {key: extra})
    value.update(reason="no_eligible_candidate", selected_candidate=None, selected_evidence=None)
    api.validate_family15_payload("selection_decided", value)
    value["reason"] = "automatic"
    with pytest.raises(SchemaError):
        api.validate_family15_payload("selection_decided", value)


def test_family15_delta_events_are_exactly_the_automatic_contract():
    from evolution.tree.family15_audit import EVENTS

    api = schema()
    expected = {
        "check_shadowed",
        "attempt_closed",
        "attempt_opened",
        "auxiliary_payload_declared",
        "candidate_completion_bound",
        "candidate_completion_completed",
        "candidate_gate_sealed",
        "champion_final_bound",
        "comparison_bound",
        "comparison_completed",
        "focus_decided",
        "comparison_preflight_failed",
        "diagnostic_import_declared",
        "final_measurement_bound",
        "final_measurement_completed",
        "finalists_bound",
        "gate_measurement_completed",
        "global_comparison_completed",
        "selection_decided",
        "grouping_portion_finished",
        "grouping_batch_finished",
        # b17 (ADR-0057): one whole description per shortened or dropped map
        # entry, bound by the terminal's `full_descriptions_digest`.
        "grouping_full_description",
    }
    assert set(api._EVENT_FIELDS) == expected
    assert set(EVENTS) == expected | {"grouping_finished"}
    assert not hasattr(api, "PromotionDecision")
    with pytest.raises(SchemaError, match="family-15 contract"):
        api.validate_family15_payload("operator_decision", {"round": 1})


def test_final_reuse_schedule_contains_only_declared_missing_candidate_work():
    completion = completion_data()
    value = dict(
        format="comparison-data/1",
        kind="final",
        verification="reuse",
        candidate_source="completion",
        candidate=completion["destination"],
        champion_side="stored",
        champion=measurement15("reference", settled=True),
        finalist_closure=reference15(),
        source_ref=reference15(),
        comparison=global_binding(
            completion["destination"], measurement15("reference", settled=True)
        ),
        candidate_work=[],
        schedule_order=[],
    )
    schema().validate_comparison_data(value)
    value["schedule_order"] = [
        dict(measurement_id="completion", slot=completion["destination"]["identity"]["schedule"][0])
    ]
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)
    value.update(schedule_order=[], verification="fresh", candidate_source="fresh")
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def test_result_needs_settled_prefixes_and_final_needs_matching_global_profile():
    value = result_data()
    schema().validate_comparison_data(value)
    value["candidate"]["prefix"] = None
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)
    value = completion_data()
    final = dict(
        format="comparison-data/1",
        kind="final",
        verification="reuse",
        candidate_source="completion",
        candidate=value["destination"],
        champion_side="stored",
        champion=measurement15("reference", settled=True),
        finalist_closure=reference15(),
        source_ref=reference15(),
        comparison=global_binding(value["destination"], measurement15("reference", settled=True)),
        candidate_work=[],
        schedule_order=[],
    )
    schema().validate_comparison_data(final)
    final["comparison"]["candidate"]["measurement_id"] = "wrong"
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(final)


def test_result_records_excluded_mass_separately_from_normalized_weights():
    value = result_data()
    value["scopes"][0].update(mass=q(Fraction(3, 4)), excluded_mass=q(Fraction(1, 4)))
    schema().validate_comparison_data(value)
    value["scopes"][0]["excluded_mass"] = q(Fraction(1, 2))
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def completion_with_two_gate_cases():
    """A complete two-case gate, using unchanged measurement/import identities."""
    from evolution.tree.execution.journal_identity import RunIdentityV1

    value = completion_data()
    for side in ("source", "destination"):
        measurement = value[side]
        measurement["identity"]["schedule"].append(
            dict(measurement["identity"]["schedule"][0], case_id="c2")
        )
        measurement["identity_digest"] = RunIdentityV1(measurement["identity"]).digest
    value["rows"].append(copy.deepcopy(value["rows"][0]))
    value["rows"][1]["case_id"] = "c2"
    for row in value["rows"]:
        row["source"]["identity_digest"] = value["source"]["identity_digest"]
    value["imports"].update(
        imported=2,
        scheduled=2,
        pairs_digest=hashlib.sha256(canonical_json(value["rows"])).hexdigest(),
    )
    return value


def test_completion_preserves_canonical_union_of_prior_epoch_condition_fields():
    value = completion_with_two_gate_cases()
    value["rows"][0]["source"]["condition_fields"] = ["identity.target"]
    value["rows"][1]["source"]["condition_fields"] = ["identity.scorer", "identity.target"]
    fields = ["identity.scorer", "identity.target"]
    value["imports"].update(
        fields=fields, pairs_digest=hashlib.sha256(canonical_json(value["rows"])).hexdigest()
    )
    schema().validate_comparison_data(value)
    for inconsistent in ([], fields[:1], fields[::-1], fields + fields, fields + ["other"]):
        bad = copy.deepcopy(value)
        bad["imports"]["fields"] = inconsistent
        with pytest.raises(SchemaError):
            schema().validate_comparison_data(bad)


def test_completion_refuses_a_declaration_that_erases_carried_condition_history():
    value = completion_data()
    value["rows"][0]["source"]["condition_fields"] = ["identity.target"]
    value["imports"]["pairs_digest"] = hashlib.sha256(canonical_json(value["rows"])).hexdigest()
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


@pytest.mark.parametrize("retained_rows", [0, 1])
def test_completion_partition_uses_actual_destination_admission(retained_rows):
    value = completion_with_two_gate_cases()
    value["rows"] = value["rows"][:retained_rows]
    value["imports"].update(
        imported=retained_rows,
        origin_runs=int(retained_rows > 0),
        pairs_digest=hashlib.sha256(canonical_json(value["rows"])).hexdigest(),
    )
    value["missing_slots"] = value["destination"]["identity"]["schedule"][retained_rows:]
    # Shape proves a complete partition. E separately authenticates whether
    # conditions actually authorize these omissions from the sealed source.
    schema().validate_comparison_data(value)
    value["missing_slots"] = []
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def test_completion_destination_must_cover_every_source_gate_slot():
    from evolution.tree.execution.journal_identity import RunIdentityV1

    value = completion_data()
    source = value["source"]
    source["identity"]["schedule"].append(dict(source["identity"]["schedule"][0], case_id="c2"))
    source["identity_digest"] = RunIdentityV1(source["identity"]).digest
    value["rows"][0]["source"]["identity_digest"] = source["identity_digest"]
    value["imports"]["pairs_digest"] = hashlib.sha256(canonical_json(value["rows"])).hexdigest()
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


def test_completion_missing_slots_are_only_the_destination_complement():
    from evolution.tree.execution.journal_identity import RunIdentityV1

    value = completion_with_two_gate_cases()
    destination = value["destination"]
    schedule = destination["identity"]["schedule"]
    for slot, row in zip(schedule, value["rows"], strict=True):
        slot["sample_id"] = row["sample_id"] = "destination-sample"
    schedule.append(dict(schedule[0], case_id="c3"))
    destination["identity_digest"] = RunIdentityV1(destination["identity"]).digest
    value["imports"].update(
        scheduled=3, pairs_digest=hashlib.sha256(canonical_json(value["rows"])).hexdigest()
    )
    value["missing_slots"] = [schedule[-1]]
    schema().validate_comparison_data(value)
    # A source and destination may spell samples differently, but the frozen
    # requested seed must survive for the same case/repeat.
    for slot in schedule:
        slot["requested_seed"] = 43
    destination["identity_digest"] = RunIdentityV1(destination["identity"]).digest
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


@pytest.mark.parametrize("mode", ["unfiltered", "point", "confidence"])
def test_disabled_deciding_rule_cannot_report_numerical_rejection(mode):
    value = result_data(mode)
    scope = value["scopes"][0]
    if mode != "unfiltered":
        value["deciding_scope"] = scope["name"] = "global"
        scope["rule"] = "off"
        scope.pop("confidence", None)
        scope.pop("enclosure", None)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)
    for reason in ("critical_failed", "provenance_failed"):
        value["reason"] = reason
        schema().validate_comparison_data(value)
    value.update(outcome="passed", reason="passed")
    schema().validate_comparison_data(value)


@pytest.mark.parametrize("kind", ["binding", "preflight"])
@pytest.mark.parametrize("non_object", [None, 0, "scope", []])
def test_binding_scope_arrays_refuse_every_non_object_as_schema_error(kind, non_object):
    value = binding() if kind == "binding" else preflight()
    value["scopes"].append(non_object)
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value)


@pytest.mark.parametrize("change", ["dependency", "seed_mode"])
def test_completion_shape_allows_changed_condition_zero_carry(change):
    from evolution.tree.execution.journal_identity import RunIdentityV1

    value = completion_data()
    destination = value["destination"]
    target = destination["identity"]["target"]
    if change == "dependency":
        target["dependency_digest"] = "new-deps"
    else:
        target["seed_mode"] = "unpaired"
        for slot in destination["identity"]["schedule"]:
            slot["sample_id"] = "candidate-01"
            slot["requested_seed"] = None
    destination["identity_digest"] = RunIdentityV1(destination["identity"]).digest
    value["rows"] = []
    value["imports"].update(
        imported=0,
        origin_runs=0,
        fields=[],
        pairs_digest=hashlib.sha256(canonical_json([])).hexdigest(),
    )
    value["missing_slots"] = destination["identity"]["schedule"]
    schema().validate_comparison_data(value)
    payload = event_payloads()["candidate_completion_bound"]
    payload.update(imported=0, missing_count=payload["required"])
    schema().validate_family15_payload("candidate_completion_bound", payload)


def leak_records():
    from tests.test_tree_campaign_schema import _projection_changed_failure

    reads = {"entries": [], "omitted_reads": 0, "omitted_errors": 0}
    return [
        (
            "reflection_failed",
            _projection_changed_failure(error_class="oracle_leak", reason="oracle_leak"),
        ),
        (
            "grouping_failed",
            {"round": 1, "grouping_id": "g1", "code": "oracle_leak", "reads": reads},
        ),
        (
            "attempt_closed",
            {
                "attempt_id": "r01-a01",
                "outcome": "abandoned",
                "reason": "seal_refused",
                "look": None,
                "code": "oracle_leak",
            },
        ),
    ]


@pytest.mark.parametrize("event,payload", leak_records())
def test_leak_is_a_family_15_optional_field_on_three_records(event, payload):
    from evolution.tree.campaign_schema import validate_payload

    validate = schema().validate_family15_payload
    validate(event, {**payload, "leak": {"case_id": "c3", "oracle_pointer": "/reference"}})
    validate(event, {**payload, "leak": {"case_id": "c3"}})
    validate(event, {**payload, "leak": {"case_id": "c3", "oracle_pointer": "/" + "k" * 1023}})
    for bad in (
        {"case_id": "c3", "oracle_pointer": "/" + "k" * 1024},
        {"case_id": "c3", "extra": 1},
        {"oracle_pointer": "/a"},
        {"case_id": 1},
        {"case_id": "c3", "oracle_pointer": "/" + "я" * 512},
        {"case_id": "c3", "oracle_pointer": "a"},
        {"case_id": "c3", "oracle_pointer": "/~2"},
    ):
        with pytest.raises(SchemaError):
            validate(event, {**payload, "leak": bad})
    with pytest.raises(SchemaError):
        validate_payload(event, {**payload, "leak": {"case_id": "c3"}}, final=True)
    wrong = (
        {**payload, "error_class": "invalid_result", "reason": "invalid_result"}
        if event == "reflection_failed"
        else {**payload, "code": "secret_detected"}
    )
    with pytest.raises(SchemaError):
        validate(event, {**wrong, "leak": {"case_id": "c3"}})


def test_detail_shape_is_closed():
    from evolution.tree.campaign_schema import validate_payload
    from tests.test_tree_campaign_schema import _projection_changed_failure

    base = _projection_changed_failure(error_class="invalid_result", reason="invalid_result")
    validate = schema().validate_family15_payload
    for detail in (
        {"category": "bounds"},
        {"category": "unknown_key", "path": "groups[0]"},
        {"category": "unknown_probe", "path": "groups[2].probe_refs[0]"},
    ):
        validate("reflection_failed", {**base, "detail": detail})
        with pytest.raises(SchemaError):
            validate_payload("reflection_failed", {**base, "detail": detail}, final=True)
    for bad in (
        {"category": "free text"},
        {"category": "unknown_key", "path": "groups[0].x"},
        {"category": "bounds", "value": "v"},
        {"category": "unknown_key", "path": "groups[10000]"},
        {"category": "bounds", "path": None},
    ):
        with pytest.raises(SchemaError):
            validate("reflection_failed", {**base, "detail": bad})
    with pytest.raises(SchemaError):
        validate("reflection_failed", {**leak_records()[0][1], "detail": {"category": "bounds"}})


@pytest.mark.parametrize("event,payload", leak_records())
def test_family15_admits_shadowed_on_carrier_records(event, payload):
    from evolution.tree.campaign_schema import validate_payload

    hit = {
        "check": "reflection_oracle_leak",
        "code": "oracle_leak",
        "count": 2,
        "leak": {"case_id": "c1", "oracle_pointer": "/answer"},
    }
    value = {**payload, "shadowed": [hit]}
    schema().validate_family15_payload(event, value)
    assert value["shadowed"] == [hit]
    with pytest.raises(SchemaError):
        validate_payload(event, value, final=True)


@pytest.mark.parametrize(
    "bad",
    [
        {"check": "made_up", "code": "oracle_leak", "count": 1},
        {"check": "reflection_oracle_leak", "code": "run_secret", "count": 1},
        {"check": "reflection_oracle_leak", "code": "oracle_leak", "count": True},
        {"check": "reflection_oracle_leak", "code": "oracle_leak", "count": 0},
        {"check": "reflection_oracle_leak", "code": "oracle_leak", "count": 1, "value": "secret"},
    ],
)
def test_shadow_entries_refuse_unknown_fields_names_codes_and_invalid_counts(bad):
    event, payload = leak_records()[0]
    with pytest.raises(SchemaError):
        schema().validate_family15_payload(event, {**payload, "shadowed": [bad]})


def test_check_shadowed_is_family15_only_and_has_nullable_context():
    from evolution.tree.campaign_schema import validate_payload

    payload = {
        "check": "verbatim_answer_unmatched",
        "code": "verbatim_answer_unmatched",
        "count": 1,
        "round": None,
        "operation_id": None,
    }
    schema().validate_family15_payload("check_shadowed", payload)
    schema().validate_family15_payload(
        "check_shadowed", {**payload, "round": 1, "operation_id": "probe1"}
    )
    with pytest.raises(SchemaError):
        validate_payload("check_shadowed", payload, final=True)
    for invalid in ({"round": 0}, {"operation_id": 2}, {"extra": "private"}):
        with pytest.raises(SchemaError):
            schema().validate_family15_payload("check_shadowed", {**payload, **invalid})


def test_family15_admits_shadowed_on_editor_failure():
    from evolution.tree.campaign_schema import validate_payload

    payload = {
        "attempt_id": "r01-a01",
        "operation_id": "op-1",
        "error_class": "reported",
        "note_digest": None,
        "reason": "changed_file_budget",
        "shadowed": [{"check": "input_run_secret_redaction", "code": "redacted", "count": 1}],
    }
    schema().validate_family15_payload("editor_failed", payload)
    with pytest.raises(SchemaError):
        validate_payload("editor_failed", payload, final=True)


def test_focus_decided_record_round_trip():
    payload = dict(
        round=1,
        attempt_id="a",
        binding=reference15(),
        outcome="comparison_not_passed",
        reason="threshold_not_passed",
        data=publication15(),
    )
    envelope = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=1,
            timestamp="2026-10-28T00:00:00Z",
            event="focus_decided",
            operation_id=None,
            payload=payload,
        ),
        family=schema().FAMILY15_CONTRACT,
    )
    assert envelope.to_payload()["payload"] == payload


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered"])
def test_not_measured_is_causal_only_and_requires_nonpassing_focus(mode):
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from tests.test_tree_campaign_policy_v12 import payload

    document = payload(mode)
    document["gate"]["stages"] = "focus_first"
    policy = CampaignPolicyV12.from_payload(document)
    value = result_data(mode)
    if mode == "unfiltered":
        value["reason"] = "critical_failed"
    causal = copy.deepcopy(value["scopes"][0])
    causal.update(name="causal", status="not_measured", point=None, unavailable=None)
    if mode == "confidence":
        causal["enclosure"] = None
    value["scopes"].append(causal)
    schema().validate_comparison_data(value, policy=policy)
    assert schema().NOT_MEASURED == "not_measured"
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(
            value, policy=CampaignPolicyV12.from_payload(payload(mode))
        )
    wrong = copy.deepcopy(value)
    wrong["scopes"][0]["status"] = "not_measured"
    wrong["scopes"][0]["point"] = None
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(wrong, policy=policy)
    wrong = copy.deepcopy(value)
    wrong.update(outcome="passed", reason="passed")
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(wrong, policy=policy)


def test_focus_record_seed_binds_scope_and_inputs():
    from evolution.tree.confidence_statistics import derive_seed

    seed = bootstrap_result()["bootstrap"]
    seed["scope"] = "focus"
    seed["seed"] = derive_seed(**{k: v for k, v in seed.items() if k not in {"method", "seed"}})
    payload = dict(
        round=1,
        attempt_id="a",
        binding=reference15(),
        outcome="passed",
        reason="passed",
        data=publication15(),
        seed=seed,
    )
    schema().validate_family15_payload("focus_decided", payload)
    with pytest.raises(SchemaError):
        schema().validate_family15_payload(
            "focus_decided", payload | {"seed": seed | {"scope": "joint"}}
        )
    with pytest.raises(SchemaError):
        schema().validate_family15_payload(
            "focus_decided", payload | {"seed": seed | {"seed": seed["seed"] + 1}}
        )


def test_focus_bootstrap_metadata_uses_the_stage_scope():
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.confidence_statistics import derive_seed
    from tests.test_tree_campaign_policy_v12 import payload

    document = payload("statistical")
    document["evaluation"]["repeats_per_question"] = 2
    document["gate"]["stages"] = "focus_first"
    policy = CampaignPolicyV12.from_payload(document)
    value = bootstrap_result()
    seed = value["bootstrap"]
    seed.update(campaign_seed=policy.random_seed)
    seed["seed"] = derive_seed(**{k: v for k, v in seed.items() if k not in {"method", "seed"}})
    with pytest.raises(SchemaError):
        schema().validate_comparison_data(value, policy=policy)
    seed["scope"] = "focus"
    seed["seed"] = derive_seed(**{k: v for k, v in seed.items() if k not in {"method", "seed"}})
    schema().validate_comparison_data(value, policy=policy)
