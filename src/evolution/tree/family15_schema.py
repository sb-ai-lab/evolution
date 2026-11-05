"""Closed family-15 contracts (ADR-0041/43/44).

The internal lifecycle registers this grammar; public writer/reader gates remain
separate. It validates shapes and intrinsic relationships, not sibling bytes.
The lifecycle owns pure order/reference existence; the evidence audit authenticates
publications and recomputes evidence. A declaration alone never establishes consumer readiness.

Large lists and exact arithmetic belong in comparison-data/1, published through
AuxiliaryPublication. Journal records contain only bounded references/counts.
Rational hex avoids Python's decimal-integer conversion limit without changing it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from evolution.tree.artifacts.artifact_payloads import (
    AuxiliaryDeclaration,
    AuxiliaryFile,
    AuxiliarySourceReference,
)
from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
from evolution.tree.campaign_schema import (
    _FINAL_KEYS,
    FINAL_CAMPAIGN_FAMILY,
    SUSPENDABLE_NO_OPERATION_EVENTS,
    _validate_created,
    journal_prefix_measurement,
    validate_payload,
)
from evolution.tree.case_statistics import METHOD
from evolution.tree.confidence_statistics import METHOD_VERSION as BOOTSTRAP_METHOD
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
from evolution.tree.execution.journal_schema import (
    _validate_payload as validate_measurement_payload,
)
from evolution.tree.roles.role_failures import REFLECTION_DETAIL_CATEGORIES, REFLECTION_DETAIL_PATH
from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    SchemaError,
    _identifier,
    _integer,
    _keys,
)

FORMAT: Final = "comparison-data/1"
RATIONAL_FORMAT: Final = "rational-hex/1"
DIAGNOSTIC_FORMAT: Final = "baseline-diagnostic-bundle/1"
MAX_RECORD_BYTES: Final = 64 * 1024
_HEX = re.compile(r"(?:0|-?[1-9a-f][0-9a-f]*)\Z")
_POSITIVE_HEX = re.compile(r"[1-9a-f][0-9a-f]*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
if TYPE_CHECKING:
    from evolution.tree.campaign_policy import CampaignPolicyV12

NOT_MEASURED: Final = "not_measured"

MODES: Final = frozenset({"point", "confidence", "unfiltered", "statistical"})
OUTCOMES: Final = frozenset({"passed", "comparison_not_passed", "comparison_unavailable"})
UNAVAILABLE: Final = frozenset(
    {
        "empty_support",
        "candidate_incomplete",
        "reference_incomplete",
        "provenance_unavailable",
        "insufficient_repeats",
        "insufficient_calibration",
    }
)
REASONS: Final = UNAVAILABLE | {
    "passed",
    "threshold_not_passed",
    "critical_failed",
    "provenance_failed",
}
CAVEATS: Final = frozenset(
    {
        "exact_contrast",
        "intervals_empirical",
        "noise_not_observed",
        "noise_allowance_empirical",
    }
)
FALLBACK_REASONS: Final = {"focus": "focus_unattainable", "causal": "causal_unconfirmable"}


def _need(ok: object, reason: str = "invalid family-15 contract") -> None:
    if not ok:
        raise SchemaError(reason)


def _object(value: Any, fields: str) -> dict[str, Any]:
    return _keys(value, set(fields.split()))


def _sha(value: Any) -> None:
    _need(isinstance(value, str) and _SHA.fullmatch(value) is not None, "invalid digest")


def _choice(value: Any, choices: Any) -> None:
    _need(isinstance(value, str) and value in choices, "invalid tagged value")


def _list(value: Any) -> list[Any]:
    _need(isinstance(value, list), "expected array")
    return value


def _bool(value: Any) -> None:
    _need(type(value) is bool, "expected boolean")


def encode_rational(value: Fraction) -> dict[str, str]:
    """Canonical exact, reduced pair; no decimal conversion or size admission cap."""
    _need(type(value) is Fraction, "expected exact Fraction")
    return dict(
        format=RATIONAL_FORMAT,
        numerator=format(value.numerator, "x"),
        denominator=format(value.denominator, "x"),
    )


def decode_rational(value: Any) -> Fraction:
    p = _object(value, "format numerator denominator")
    _need(p["format"] == RATIONAL_FORMAT)
    numerator, denominator = p["numerator"], p["denominator"]
    _need(isinstance(numerator, str) and _HEX.fullmatch(numerator) is not None)
    _need(isinstance(denominator, str) and _POSITIVE_HEX.fullmatch(denominator) is not None)
    result = Fraction(int(numerator, 16), int(denominator, 16))
    _need(encode_rational(result) == p, "rational pair is not reduced")
    return result


def _rational(value: Any, low: int | None = None, high: int | None = None) -> Fraction:
    result = decode_rational(value)
    _need(low is None or result >= low)
    _need(high is None or result <= high)
    return result


def _campaign_ref(value: Any) -> AuxiliarySourceReference:
    ref = AuxiliarySourceReference.from_payload(value)
    _need(ref.kind == "journal_record", "expected campaign record")
    return ref


def _prefix(value: Any, measurement_id: str) -> _durable_reference.DurableReference:
    ref = _durable_reference.DurableReference.from_payload(value)
    _need(
        ref.kind == "journal_prefix" and journal_prefix_measurement(ref.locator) == measurement_id,
        "prefix must name this measurement",
    )
    # The older reference parser tolerates decimal leading zeros; the new wire does not.
    start, end = ref.locator.split("#bytes=")[1].split(":")
    _need(start == "0" and end == str(int(end)) and int(end) > 0)
    return ref


def _source(value: Any) -> dict[str, Any]:
    p = _object(value, "measurement_id identity_digest candidate_digest prefix")
    _identifier(p["measurement_id"])
    _sha(p["identity_digest"])
    _sha(p["candidate_digest"])
    _prefix(p["prefix"], p["measurement_id"])
    return p


def _measurement(value: Any, *, settled: bool = False) -> dict[str, Any]:
    p = _object(value, "measurement_id identity identity_digest prefix")
    _identifier(p["measurement_id"])
    identity = RunIdentityV1.from_payload(p["identity"])
    _need(identity.to_payload()["profile"] == "rollout_measurement")
    _need(identity.digest == p["identity_digest"], "measurement identity digest")
    _sha(p["identity"]["candidate_digest"])
    if p["prefix"] is not None:
        _prefix(p["prefix"], p["measurement_id"])
    else:
        _need(not settled, "settled source requires prefix")
    return p


def _slot(value: Any) -> tuple[str, int]:
    p = _object(value, "case_id sample_id repeat requested_seed")
    _identifier(p["case_id"])
    _identifier(p["sample_id"])
    _integer(p["repeat"], 1)
    _need(p["requested_seed"] is None or type(p["requested_seed"]) is int)
    return p["case_id"], p["repeat"]


def _slots(value: Any) -> dict[tuple[str, int], dict[str, Any]]:
    rows = _list(value)
    result = {_slot(row): row for row in rows}
    _need(len(result) == len(rows), "duplicate scheduled slot")
    return result


def _publication(value: Any, purpose: str = "comparison") -> AuxiliaryPublication:
    publication = AuxiliaryPublication.from_payload(value)
    _need(publication.declaration.purpose == purpose)
    # The campaign and S2 ledger own separate run IDs (ADR-0006). E proves
    # same-directory membership against each journal's actual identity.
    return publication


def _noise(value: Any) -> None:
    _need(isinstance(value, dict))
    kind = value.get("kind")
    _choice(kind, {"negligible", "bounded", "observed"})
    if kind == "bounded":
        p = _object(value, "kind allowance source")
        _rational(p["allowance"], 0)
        _need(
            isinstance(p["source"], str)
            and 0 < len(p["source"].strip())
            and len(p["source"]) <= 1024
        )
    else:
        _object(value, "kind")


def _fallback(value: Any, name: str) -> None:
    p = _object(value, "reason")
    _need(name in FALLBACK_REASONS and p["reason"] == FALLBACK_REASONS[name])


def _caveats(value: Any) -> None:
    rows = _list(value)
    for row in rows:
        _choice(row, CAVEATS)
    _need(len(rows) == len(set(rows)), "duplicate caveat")


def calibration_payload(
    *,
    source: str,
    measurement_id: str,
    prefix: Mapping[str, Any],
    retained: int,
    seed_input: str,
    allowance: Fraction,
    degenerate: bool,
) -> dict[str, Any]:
    """Canonical Parent-only calibration; digest covers every input and exact output."""
    p = dict(
        source=source,
        measurement_id=measurement_id,
        prefix=dict(prefix),
        retained=retained,
        method=BOOTSTRAP_METHOD,
        level=encode_rational(Fraction(995, 1000)),
        seed_input=seed_input,
        allowance=encode_rational(allowance),
        degenerate=degenerate,
    )
    p["result_digest"] = _digest(p)
    _calibration(p)
    return p


def _calibration(value: Any) -> dict[str, Any]:
    p = _object(
        value,
        "source measurement_id prefix retained method level seed_input allowance degenerate result_digest",
    )
    _choice(p["source"], {"stored", "fresh"})
    _identifier(p["measurement_id"])
    _prefix(p["prefix"], p["measurement_id"])
    _integer(p["retained"], 1)
    _need(p["method"] == BOOTSTRAP_METHOD)
    _need(_rational(p["level"]) == Fraction(995, 1000))
    _sha(p["seed_input"])
    allowance = _rational(p["allowance"], 0)
    _bool(p["degenerate"])
    _need(not p["degenerate"] or allowance == 0)
    _sha(p["result_digest"])
    _need(
        p["result_digest"] == _digest({k: v for k, v in p.items() if k != "result_digest"}),
        "calibration result digest differs",
    )
    return p


def _comparison_calibration(p: dict[str, Any], policy: CampaignPolicyV12 | None) -> None:
    if "calibration" not in p:
        return
    c = _calibration(p["calibration"])
    _need(p["mode"] != "unfiltered", "unfiltered has no causal calibration")
    source = p["reference_kind"] if p["kind"] != "result" else "fresh"
    _need(
        source == c["source"] == ("fresh" if p["kind"] == "result" else "stored"),
        "calibration belongs in the binding for stored Parent, result for fresh Parent",
    )
    _need(
        all(c[k] == p["reference"][k] for k in ("measurement_id", "prefix")),
        "calibration must name its exact Parent prefix",
    )
    causal = next((s for s in p["scopes"] if s["name"] == "causal"), None)
    _need(
        causal is not None and bool(causal.get("cases", causal.get("declared_count"))),
        "empty causal scope has no calibration",
    )
    if policy is not None:
        _need(policy.causal_tolerance == "measured_noise" and policy.gate_mode != "unfiltered")
        _need(policy.fresh_parent == (source == "fresh"))


def _scope_rule(
    name: Any,
    rule: Any,
    mode: str,
    *,
    policy: CampaignPolicyV12 | None = None,
    fallback: Any = None,
) -> None:
    _choice(rule, {"point", "bound", "off", "bootstrap"})
    expected = {
        "confidence": "bound",
        "unfiltered": "off",
        "statistical": "bootstrap",
        "point": "point",
    }[mode]
    if fallback is not None:
        _fallback(fallback, name)
        _need(
            mode == "confidence"
            and rule == "point"
            and policy is not None
            and policy.gate_mode == "confidence"
            and policy.bound_fallback == "point",
            "fallback requires declared confidence bound_fallback",
        )
        return
    if name != "global":
        _need(rule == expected, "local rule must follow campaign mode")
    elif mode == "unfiltered":
        _need(rule == "off")
    elif mode in {"point", "statistical"}:
        _need(rule in {"point", "off"})
    else:
        _need(rule in {"point", "bound", "off"})


def _scopes(
    value: Any,
    mode: str,
    reference_kind: str,
    statistics: Any,
    policy: CampaignPolicyV12 | None = None,
) -> None:
    scopes = _list(value)
    _need(all(isinstance(scope, dict) for scope in scopes), "scope must be an object")
    _need([scope.get("name") for scope in scopes] in (["focus", "causal"], ["global"]))
    all_cases: set[str] = set()
    for scope in scopes:
        rule = scope.get("rule")
        _scope_rule(scope.get("name"), rule, mode, policy=policy, fallback=scope.get("fallback"))
        fields = "name rule declared_cases cases"
        if rule != "off":
            fields += " threshold strict"
        if rule == "bound":
            fields += " confidence units"
        if rule == "bootstrap":
            fields += " confidence"
        if "tolerance" in scope:
            fields += " tolerance"
            _need(
                scope.get("name") == "causal"
                and scope["tolerance"] == "measured_noise"
                and mode != "unfiltered",
                "only causal measured tolerance may defer its threshold",
            )
            if policy is not None:
                _need(policy.causal_tolerance == "measured_noise")
        if "fallback" in scope:
            fields += " fallback"
            _fallback(scope["fallback"], scope["name"])
            _need(bool(scope["cases"]), "empty scope cannot fall back")
        p = _object(scope, fields)
        if rule != "off":
            if p["threshold"] is None:
                _need(p.get("tolerance") == "measured_noise" and "fallback" not in p)
            else:
                _rational(p["threshold"])
            _bool(p["strict"])
            _need(p["strict"] == (p["name"] != "causal"))
        if rule in {"bound", "bootstrap"}:
            _need(0 < _rational(p["confidence"]) < 1)
        declared = _list(p["declared_cases"])
        for case_id in declared:
            _identifier(case_id)
            _need(case_id not in all_cases, "declared scopes must be disjoint")
            all_cases.add(case_id)
        weights: dict[str, Fraction] = {}
        groups: dict[str, str] = {}
        for case in _list(p["cases"]):
            c = _object(
                case,
                "case_id group weight repeats"
                + (" reference_values" if reference_kind == "stored" else ""),
            )
            _identifier(c["case_id"])
            _identifier(c["group"])
            _need(c["case_id"] in declared and c["case_id"] not in weights)
            weight = _rational(c["weight"])
            _need(weight > 0)
            weights[c["case_id"]] = weight
            groups[c["case_id"]] = c["group"]
            repeats = _list(c["repeats"])
            for repeat in repeats:
                _integer(repeat, 1)
            _need(bool(repeats) and repeats == sorted(set(repeats)))
            if reference_kind == "stored":
                values = _list(c["reference_values"])
                _need(len(values) == len(repeats))
                for v in values:
                    _rational(v, 0, 1)
        _need(not weights or sum(weights.values()) == 1, "weights must sum exactly to one")
        if rule == "bound":
            unit_cases: set[str] = set()
            unit_ids: set[str] = set()
            for unit in _list(p["units"]):
                u = _object(unit, "unit_id cases weight")
                _identifier(u["unit_id"])
                _need(u["unit_id"] not in unit_ids)
                unit_ids.add(u["unit_id"])
                cases = _list(u["cases"])
                _need(
                    bool(cases)
                    and all(
                        isinstance(c, str) and c in weights and c not in unit_cases for c in cases
                    )
                )
                _need(len(set(cases)) == len(cases))
                unit_cases.update(cases)
                _need(_rational(u["weight"]) == sum(weights[c] for c in cases))
                if statistics["unit"] == "case":
                    _need(cases == [u["unit_id"]])
                else:
                    _need(all(groups[c] == u["unit_id"] for c in cases))
            _need(unit_cases == set(weights), "unit profile must cover retained cases")


def _binding(
    p: dict[str, Any], *, preflight: bool = False, policy: CampaignPolicyV12 | None = None
) -> None:
    mode = p.get("mode")
    _choice(mode, MODES)
    assert isinstance(mode, str)
    fields = "format kind epoch policy_digest mode execution_noise reference reference_kind scopes excluded_reference"
    fields += " phase results" if preflight else " candidate"
    if mode in {"confidence", "statistical"}:
        fields += " statistics"
    if "calibration" in p:
        fields += " calibration"
    if "calibration_unavailable" in p:
        fields += " calibration_unavailable"
    _object(p, fields)
    _integer(p["epoch"])
    _sha(p["policy_digest"])
    if policy is not None:
        _need(
            p["policy_digest"] == policy.digest and mode == policy.gate_mode,
            "comparison policy differs",
        )
    _noise(p["execution_noise"])
    _choice(p["reference_kind"], {"stored", "fresh"})
    reference = _measurement(p["reference"], settled=p["reference_kind"] == "stored")
    statistics = p.get("statistics")
    if mode == "confidence":
        statistics = _object(statistics, "method unit profile")
        _need(statistics["method"] == METHOD and statistics["profile"] == METHOD)
        _choice(statistics["unit"], {"case", "group"})
    elif mode == "statistical":
        statistics = _object(statistics, "method")
        _need(statistics["method"] == BOOTSTRAP_METHOD)
    _scopes(p["scopes"], mode, p["reference_kind"], statistics, policy)
    _comparison_calibration(p, policy)
    measured = next((s for s in p["scopes"] if s.get("tolerance") == "measured_noise"), None)
    if "calibration_unavailable" in p:
        _need(
            measured is not None
            and bool(measured["cases"])
            and p["reference_kind"] == "stored"
            and "calibration" not in p
            and p["calibration_unavailable"] == "insufficient_calibration",
            "unavailable calibration has no numeric output",
        )
    if measured is not None:
        if p["reference_kind"] == "stored" and measured["cases"]:
            _need(("calibration" in p) != ("calibration_unavailable" in p))
            if "calibration" in p:
                calibration = p["calibration"]
                _need(calibration["retained"] == len(measured["cases"]))
                noise = p["execution_noise"]
                allowance = (
                    decode_rational(noise["allowance"])
                    if noise["kind"] == "bounded"
                    else Fraction()
                )
                _need(
                    measured["threshold"] is not None
                    and decode_rational(measured["threshold"])
                    == allowance - decode_rational(calibration["allowance"]),
                    "measured causal threshold must reserve execution allowance exactly once",
                )
            else:
                _need(measured["threshold"] is None)
        else:
            _need("calibration" not in p and "calibration_unavailable" not in p)
            _need(measured["threshold"] is None, "unmeasured/empty Parent has no known q")
    if policy is not None and policy.causal_tolerance == "measured_noise":
        for scope in p["scopes"]:
            if scope["name"] == "causal" and "fallback" in scope:
                _need(
                    not policy.fresh_parent
                    and p["reference_kind"] == "stored"
                    and "calibration" in p,
                    "measured causal binding fallback requires stored Parent calibration",
                )
    reference_slots = _slots(reference["identity"]["schedule"])
    excluded = _slots(p["excluded_reference"])
    _need(all(reference_slots.get(key) == row for key, row in excluded.items()))
    if p["reference_kind"] == "fresh":
        _need(not excluded, "fresh reference cannot borrow stored exclusions")
    retained = {(c["case_id"], r) for s in p["scopes"] for c in s["cases"] for r in c["repeats"]}
    _need(retained <= reference_slots.keys() and not retained & excluded.keys())
    if not preflight:
        candidate = _measurement(p["candidate"])
        _need(candidate["measurement_id"] != reference["measurement_id"])
        candidate_slots = _slots(candidate["identity"]["schedule"])
        _need(retained <= candidate_slots.keys())
    else:
        _choice(p["phase"], {"early_global", "attempt"})
        _need(mode != "unfiltered", "unfiltered has no numerical preflight")
        _need(
            mode != "statistical" or p["phase"] == "early_global",
            "statistical has no local preflight",
        )
        if p["phase"] == "early_global":
            _need([s["name"] for s in p["scopes"]] == ["global"])
        _preflight_results(p)


def _evidence_number(value: Any) -> Fraction | None:
    _need(isinstance(value, dict))
    if value.get("kind") == "positive_infinity":
        _object(value, "kind")
        return None
    p = _object(value, "kind value")
    _need(p["kind"] == "finite")
    return _rational(p["value"], 0)


def _preflight_results(p: dict[str, Any]) -> None:
    results = _list(p["results"])
    _need(len(results) == len(p["scopes"]))
    for result, scope in zip(results, p["scopes"], strict=True):
        if scope["name"] == "causal" and not scope["declared_cases"]:
            _object(result, "name status")
            _need(result == {"name": "causal", "status": "skipped"})
            continue
        if scope.get("tolerance") == "measured_noise" and scope["threshold"] is None:
            _need(result == {"name": "causal", "status": "uncalibrated"})
            continue
        _need(scope["rule"] != "off")
        fields = "name status ceilings passes"
        fields += " e_max c_max" if scope["rule"] == "bound" else " delta_max"
        r = _object(result, fields)
        _need(r["name"] == scope["name"] and r["status"] == "calculated")
        _bool(r["passes"])
        ceilings = _list(r["ceilings"])
        _need(len(ceilings) == len(scope["cases"]))
        for ceiling, case in zip(ceilings, scope["cases"], strict=True):
            _object(ceiling, "case_id value")
            _need(ceiling["case_id"] == case["case_id"])
            expected = (
                1 - sum(map(decode_rational, case["reference_values"])) / len(case["repeats"])
                if p["reference_kind"] == "stored"
                else Fraction(1)
            )
            _need(_rational(ceiling["value"], 0, 1) == expected, "reference-aware ceiling")
        threshold = decode_rational(scope["threshold"])
        strict = scope["strict"]
        if scope["rule"] == "point":
            _need(
                _rational(r["delta_max"], 0, 1)
                == sum(
                    decode_rational(c["weight"]) * decode_rational(v["value"])
                    for c, v in zip(scope["cases"], ceilings, strict=True)
                )
            )
            delta = decode_rational(r["delta_max"])
            expected_pass = delta > threshold if strict else delta >= threshold
        else:
            confidence_cutoff = 1 / (1 - decode_rational(scope["confidence"]))
            if -1 < threshold < 1:
                e = _evidence_number(r["e_max"])
                _need(e is not None, "interior exact evidence is finite")
                assert e is not None
                expected_pass = e > confidence_cutoff if strict else e >= confidence_cutoff
                expected_confidence = 1 - 1 / e if e > 1 else None
                _need((r["c_max"] is None) == (expected_confidence is None))
                if expected_confidence is not None:
                    _need(decode_rational(r["c_max"]) == expected_confidence)
            elif threshold == -1:
                e = _evidence_number(r["e_max"])
                expected_pass = not strict or e is None or e > confidence_cutoff
                _need(r["c_max"] is None, "support decision has no interior c_max")
            else:
                _need(r["e_max"] is None and r["c_max"] is None, "exterior support uses no E")
                expected_pass = threshold < -1
        _need(
            r["passes"] == expected_pass, "preflight decision contradicts exact recorded evidence"
        )


def _coverage(value: Any) -> bool:
    p = _object(value, "required settled admissible")
    for n in p.values():
        _integer(n)
    _need(p["admissible"] <= p["settled"] <= p["required"])
    return p["admissible"] == p["required"]


def _result(
    p: dict[str, Any],
    *,
    policy: CampaignPolicyV12 | None = None,
    binding: Mapping[str, Any] | None = None,
) -> None:
    _object(
        p,
        "format kind binding_digest candidate reference mode scopes coverage retained_pairs excluded_reference repeat_diagnostics variation deciding_scope outcome reason"
        + (" calibration" if "calibration" in p else "")
        + (" caveats" if "caveats" in p else "")
        + (" bootstrap" if "bootstrap" in p else ""),
    )
    _sha(p["binding_digest"])
    candidate, reference = _source(p["candidate"]), _source(p["reference"])
    _need(candidate["measurement_id"] != reference["measurement_id"])
    _choice(p["mode"], MODES)
    if policy is not None:
        _need(p["mode"] == policy.gate_mode, "result policy mode differs")
    if "caveats" in p:
        _caveats(p["caveats"])
    _need(
        ("bootstrap" in p) == (p["mode"] == "statistical"),
        "bootstrap metadata is required exactly for statistical results",
    )
    if p["mode"] == "statistical":
        _need("intervals_empirical" in p.get("caveats", []), "bootstrap intervals are empirical")
    if "bootstrap" in p:
        from evolution.tree.confidence_statistics import derive_seed

        _need(p["mode"] == "statistical", "bootstrap metadata is statistical only")
        b = _object(p["bootstrap"], "method campaign_seed binding scope prefixes look purpose seed")
        _need(b["method"] == BOOTSTRAP_METHOD and b["binding"] == p["binding_digest"])
        _need(type(b["campaign_seed"]) is int)
        _need(b["scope"] in {"joint", "focus", "causal"} and b["purpose"] == "gate")
        if b["scope"] != "joint" and policy is not None:
            _need(policy.focus_first, "stage seed requires focus_first")
        _integer(b["look"], 2)
        _need(b["prefixes"] == [s["prefix"]["locator"] for s in (candidate, reference)])
        _need(
            type(b["seed"]) is int
            and b["seed"]
            == derive_seed(
                campaign_seed=b["campaign_seed"],
                binding=b["binding"],
                scope=b["scope"],
                prefixes=b["prefixes"],
                look=b["look"],
                purpose=b["purpose"],
            )
        )
        if policy is not None:
            _need(b["campaign_seed"] == policy.random_seed and b["look"] == policy.repeats)
            if policy.focus_first and [scope["name"] for scope in p["scopes"]] != ["global"]:
                expected_scope = (
                    "focus"
                    if len(p["scopes"]) == 1
                    or any(scope["status"] in {NOT_MEASURED, "skipped"} for scope in p["scopes"])
                    else "causal"
                )
                _need(b["scope"] == expected_scope, "focus-first bootstrap uses its stage scope")
    _outcome(p)
    coverage = _object(p["coverage"], "candidate reference")
    candidate_complete = _coverage(coverage["candidate"])
    _coverage(coverage["reference"])
    if not candidate_complete:
        _need(
            p["outcome"] == "comparison_unavailable" and p["reason"] == "candidate_incomplete",
            "Parent exclusions never excuse missing Candidate slots",
        )
    candidate_keys, reference_keys = set(), set()
    for pair in _list(p["retained_pairs"]):
        _object(pair, "candidate reference")
        c, r = _slot(pair["candidate"]), _slot(pair["reference"])
        _need(c == r and c not in candidate_keys and r not in reference_keys)
        candidate_keys.add(c)
        reference_keys.add(r)
    excluded = _slots(p["excluded_reference"])
    _need(not reference_keys & excluded.keys())
    scopes = _list(p["scopes"])
    _need(
        binding is not None or not any(isinstance(s, dict) and "fallback" in s for s in scopes),
        "result fallback requires its bound profile",
    )
    names: set[str] = set()
    for scope in scopes:
        _need(isinstance(scope, dict))
        _scope_rule(
            scope.get("name"),
            scope.get("rule"),
            p["mode"],
            policy=policy,
            fallback=scope.get("fallback"),
        )
        fields = "name rule status declared_count point unit_count mass excluded_mass unavailable"
        if scope.get("rule") == "bound":
            fields += " confidence enclosure"
        if scope.get("rule") == "bootstrap":
            fields += " confidence lower exact pairing"
        if "threshold" in scope:
            fields += " threshold"
            _need(scope.get("name") == "causal" and p["mode"] != "unfiltered")
            if policy is not None:
                _need(policy.causal_tolerance == "measured_noise")
            if scope["threshold"] is not None:
                _rational(scope["threshold"])
            else:
                _need(scope["status"] in {"skipped", "unavailable", NOT_MEASURED})
        if "fallback" in scope:
            fields += " fallback"
            _fallback(scope["fallback"], scope["name"])
        s = _object(scope, fields)
        _choice(s["name"], {"focus", "causal", "global"})
        _need(s["name"] not in names)
        names.add(s["name"])
        _integer(s["declared_count"])
        if "fallback" in s:
            _need(
                s["declared_count"] > 0
                and (s["name"] != "causal" or p["reason"] != "insufficient_calibration"),
                "fallback needs a nonempty scope; causal fallback needs known calibration",
            )
        _choice(s["status"], {"available", "unavailable", "skipped", NOT_MEASURED})
        _integer(s["unit_count"])
        mass = _rational(s["mass"], 0, 1)
        excluded_mass = _rational(s["excluded_mass"], 0, 1)
        _need(mass + excluded_mass == (1 if s["declared_count"] else 0))
        _need((s["unit_count"] == 0) == (mass == 0))
        if s["point"] is not None:
            _rational(s["point"], -1, 1)
        if s["status"] == NOT_MEASURED:
            _need(
                s["name"] == "causal"
                and s["declared_count"] > 0
                and s["point"] is None
                and s["unavailable"] is None
                and p["outcome"] != "passed"
                and p["deciding_scope"] == "focus"
                and (policy is None or policy.focus_first),
                "only a focus-first rejection leaves causal unmeasured",
            )
        elif s["status"] == "skipped":
            _need(
                s["name"] == "causal"
                and s["declared_count"] == 0
                and s["unit_count"] == 0
                and s["point"] is None
                and s["unavailable"] is None
            )
        elif s["unavailable"] is not None:
            _need(s["status"] == "unavailable")
            _choice(s["unavailable"], UNAVAILABLE)
            _need(
                p["outcome"] == "comparison_unavailable"
                or (
                    p["mode"] == "statistical"
                    and p["outcome"] == "comparison_not_passed"
                    and p["reason"] == "critical_failed"
                    and s["rule"] == "bootstrap"
                    and s["unavailable"] == "insufficient_repeats"
                )
                or (
                    p["outcome"] == "comparison_not_passed"
                    and p["reason"] == "critical_failed"
                    and s["name"] == "causal"
                    and "threshold" in s
                    and s["unavailable"] == "insufficient_calibration"
                )
            )
        else:
            _need(
                s["status"] == "available"
                and s["point"] is not None
                and s["unit_count"] > 0
                and s["declared_count"] > 0
            )
        if s["rule"] == "bound":
            _need(0 < _rational(s["confidence"]) < 1)
            if s["status"] != "available":
                _need(s["enclosure"] is None)
            else:
                enclosure = _object(s["enclosure"], "lower upper steps")
                lower, upper = (
                    _rational(enclosure["lower"], -1, 1),
                    _rational(enclosure["upper"], -1, 1),
                )
                _integer(enclosure["steps"], 64)
                _need(lower <= upper)
                width = upper - lower
                _need(width.numerator <= (width.denominator >> (enclosure["steps"] - 1)))
        elif s["rule"] == "bootstrap":
            _need(0 < _rational(s["confidence"]) < 1)
            if s["status"] != "available":
                _need(s["lower"] is None and s["exact"] is None)
                _need(s["pairing"] is None or s["pairing"] in {"exact", "unpaired"})
            else:
                lower = _rational(s["lower"], -1, 1)
                _bool(s["exact"])
                _choice(s["pairing"], {"exact", "unpaired"})
                if s["exact"]:
                    _need(lower == decode_rational(s["point"]) and "exact_contrast" in p["caveats"])
    _need(bool(names) and p["deciding_scope"] in names)
    if p["reason"] == "threshold_not_passed":
        deciding = next(scope for scope in scopes if scope["name"] == p["deciding_scope"])
        _need(deciding["rule"] != "off", "disabled rule cannot reject a numerical comparison")
    _choice(p["variation"], {"observed", "unavailable", "not_requested"})
    seen: set[str] = set()
    for diagnostic in _list(p["repeat_diagnostics"]):
        d = _object(diagnostic, "case_id variation unavailable")
        _identifier(d["case_id"])
        _need(d["case_id"] not in seen)
        seen.add(d["case_id"])
        if d["unavailable"] is None:
            _rational(d["variation"], 0)
        else:
            _choice(d["unavailable"], {"insufficient_repeats", "incomplete", "not_requested"})
            _need(d["variation"] is None)
    _comparison_calibration(p, policy)
    if binding is not None:
        _binding(dict(binding), policy=policy)
        _need(
            p["binding_digest"] == _digest(binding) and p["mode"] == binding["mode"],
            "result names another binding",
        )
        bound_scopes = binding["scopes"]
        if policy is not None and policy.focus_first and names == {"focus"}:
            bound_scopes = [scope for scope in bound_scopes if scope["name"] == "focus"]
        _need([s["name"] for s in scopes] == [s["name"] for s in bound_scopes])
        for result, bound in zip(scopes, bound_scopes, strict=True):
            if bound.get("tolerance") == "measured_noise":
                _need("threshold" in result, "measured result must carry its causal threshold")
                if binding["reference_kind"] == "stored":
                    _need(
                        result["threshold"] == bound["threshold"], "stored causal threshold changed"
                    )
                elif "calibration" in p:
                    calibration = p["calibration"]
                    noise = binding["execution_noise"]
                    allowance = (
                        decode_rational(noise["allowance"])
                        if noise["kind"] == "bounded"
                        else Fraction()
                    )
                    _need(
                        calibration["retained"] == len(bound["cases"]),
                        "calibration retained count differs",
                    )
                    _need(
                        result["threshold"] is not None
                        and decode_rational(result["threshold"])
                        == allowance - decode_rational(calibration["allowance"]),
                        "fresh causal threshold must reserve execution allowance exactly once",
                    )
                else:
                    _need(
                        result["threshold"] is None,
                        "unknown fresh calibration has no numeric threshold",
                    )
            if result["rule"] == bound["rule"]:
                _need(result.get("fallback") == bound.get("fallback"), "result changed fallback")
            else:
                _need(
                    policy is not None
                    and policy.gate_mode == "confidence"
                    and policy.bound_fallback == "point"
                    and policy.causal_tolerance == "measured_noise"
                    and policy.fresh_parent
                    and binding["reference_kind"] == "fresh"
                    and result["name"] == "causal"
                    and bound["rule"] == "bound"
                    and result["rule"] == "point"
                    and result.get("fallback") == {"reason": "causal_unconfirmable"}
                    and "calibration" in p
                    and p["reason"] != "insufficient_calibration",
                    "result-side substitution is only allowed for fresh measured causal fallback",
                )


def _outcome(p: dict[str, Any]) -> None:
    _choice(p["outcome"], OUTCOMES)
    _choice(p["reason"], REASONS)
    _choice(p["deciding_scope"], {"focus", "causal", "global"})
    _need((p["outcome"] == "comparison_unavailable") == (p["reason"] in UNAVAILABLE))
    _need((p["outcome"] == "passed") == (p["reason"] == "passed"))


def validate_comparison_data(
    value: Any,
    *,
    policy: CampaignPolicyV12 | None = None,
    binding: Mapping[str, Any] | None = None,
) -> None:
    """Validate closed data; optional authenticated context adds cross-document checks.

    No context is needed for legacy data. Fallbacks fail closed without a policy
    declaration. Artifact owners must supply that policy; fallback results also
    require their bound profile to enforce R8. This function authenticates neither.
    """
    try:
        _need(isinstance(value, dict) and value.get("format") == FORMAT)
        kind = value.get("kind")
        if kind in ("binding", "preflight"):
            _need(binding is None, "binding context is only for results")
            _binding(value, preflight=kind == "preflight", policy=policy)
        elif kind == "result":
            _result(value, policy=policy, binding=binding)
        else:
            _transfer_data(value)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError("invalid family-15 comparison data") from exc


def _transfer_data(p: dict[str, Any]) -> None:
    kind = p.get("kind")
    if kind == "gate_seal":
        _object(p, "format kind measurement slots_digest")
        m = _measurement(p["measurement"], settled=True)
        _need(p["slots_digest"] == _digest(m["identity"]["schedule"]))
    elif kind == "completion":
        _completion(p)
    elif kind == "final":
        _final(p)
    elif kind == "ranking":
        _ranking(p)
    elif kind == "diagnostic_import":
        _diagnostic_import(p)
    else:
        raise SchemaError("unknown comparison-data kind")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _completion(p: dict[str, Any]) -> None:
    _object(
        p, "format kind finalist_closure gate_seal destination source imports rows missing_slots"
    )
    _campaign_ref(p["finalist_closure"])
    _campaign_ref(p["gate_seal"])
    dest, source = _measurement(p["destination"]), _measurement(p["source"], settled=True)
    _need(dest["prefix"] is None and dest["measurement_id"] != source["measurement_id"])
    _need(
        dest["identity"]["candidate_digest"] == source["identity"]["candidate_digest"],
        "completion imports its own Candidate only",
    )
    declaration = _object(
        p["imports"],
        "mode source_measurement_id source_prefix_digest pairs_digest imported scheduled fields origin_runs",
    )
    _need(declaration["mode"] == "inherit")
    _need(declaration["source_measurement_id"] == source["measurement_id"])
    _need(declaration["source_prefix_digest"] == source["prefix"]["content_digest"])
    rows = _list(p["rows"])
    _need(declaration["pairs_digest"] == _digest(rows))
    dest_slots, source_slots = (
        _slots(dest["identity"]["schedule"]),
        _slots(source["identity"]["schedule"]),
    )
    # E authenticates complete original gate coverage and recomputes admission
    # under destination conditions. Changed conditions can leave gate slots owed.
    _need(source_slots.keys() <= dest_slots.keys(), "completion covers the sealed gate schedule")
    dest_by_sample = {(slot["case_id"], slot["sample_id"]): slot for slot in dest_slots.values()}
    imported: set[tuple[str, int]] = set()
    origins: set[str] = set()
    for row in rows:
        validate_measurement_payload("sample_imported", row)
        slot = dest_by_sample.get((row["case_id"], row["sample_id"]))
        _need(slot is not None, "import must name a destination scheduled sample")
        assert slot is not None
        key = _slot(slot)
        _need(key not in imported and dest_slots.get(key) == slot and key in source_slots)
        _need(
            row["candidate_digest"]
            == row["source"]["candidate_digest"]
            == source["identity"]["candidate_digest"]
        )
        _need(slot["requested_seed"] == source_slots[key]["requested_seed"])
        imported.add(key)
        origins.add(row["source"]["run_id"])
    fields = sorted({field for row in rows for field in row["source"]["condition_fields"]})
    _need(declaration["fields"] == fields, "completion preserves carried condition history")
    for name, expected in (
        ("imported", len(rows)),
        ("scheduled", len(dest_slots)),
        ("origin_runs", len(origins)),
    ):
        _integer(declaration[name])
        _need(declaration[name] == expected)
    missing = _slots(p["missing_slots"])
    _need(all(dest_slots.get(key) == row for key, row in missing.items()))
    _need(
        missing.keys() == dest_slots.keys() - imported,
        "completion must declare every missing slot",
    )


def _final(p: dict[str, Any]) -> None:
    _object(
        p,
        "format kind verification candidate_source candidate champion_side champion finalist_closure source_ref comparison candidate_work schedule_order",
    )
    _verification(p)
    candidate = _measurement(p["candidate"])
    champion = _measurement(p["champion"], settled=p["champion_side"] == "stored")
    _need(candidate["measurement_id"] != champion["measurement_id"])
    _campaign_ref(p["finalist_closure"])
    _campaign_ref(p["source_ref"])
    validate_comparison_data(p["comparison"])
    comparison = p["comparison"]
    _need(
        comparison["kind"] == "binding" and [s["name"] for s in comparison["scopes"]] == ["global"]
    )
    _need(comparison["candidate"] == candidate and comparison["reference"] == champion)
    _need(comparison["reference_kind"] == p["champion_side"])
    candidate_slots = _slots(candidate["identity"]["schedule"])
    work = _slots(p["candidate_work"])
    _need(all(candidate_slots.get(key) == slot for key, slot in work.items()))
    if p["verification"] == "fresh":
        _need(work == candidate_slots, "fresh verification dispatches the entire Candidate")
    required = {(candidate["measurement_id"], *key): slot for key, slot in work.items()}
    if p["champion_side"] == "fresh":
        required.update(
            {
                (champion["measurement_id"], *key): slot
                for key, slot in _slots(champion["identity"]["schedule"]).items()
            }
        )
    seen = set()
    for item in _list(p["schedule_order"]):
        row = _object(item, "measurement_id slot")
        _identifier(row["measurement_id"])
        key = (row["measurement_id"], *_slot(row["slot"]))
        _need(key not in seen and required.get(key) == row["slot"])
        seen.add(key)
    _need(seen == required.keys())


def _verification(p: dict[str, Any]) -> None:
    _choice(p["verification"], {"reuse", "fresh"})
    _choice(p["candidate_source"], {"completion", "fresh"})
    _need((p["verification"] == "reuse") == (p["candidate_source"] == "completion"))
    _choice(p["champion_side"], {"stored", "fresh"})


def _ranking(p: dict[str, Any]) -> None:
    _object(p, "format kind round_stop entries proposed_attempt")
    _choice(p["round_stop"], {"first_accepted", "all_attempts"})
    seen: set[str] = set()
    indexes: set[int] = set()
    accepted: list[tuple[Fraction, int, str]] = []
    for entry in _list(p["entries"]):
        e = _object(
            entry, "attempt_id attempt_index candidate_digest result evidence accepted point"
        )
        _identifier(e["attempt_id"])
        _integer(e["attempt_index"], 1)
        _need(e["attempt_id"] not in seen and e["attempt_index"] not in indexes)
        seen.add(e["attempt_id"])
        indexes.add(e["attempt_index"])
        _sha(e["candidate_digest"])
        _campaign_ref(e["result"])
        source = _source(e["evidence"])
        _need(source["candidate_digest"] == e["candidate_digest"])
        _bool(e["accepted"])
        if e["point"] is not None:
            point = _rational(e["point"], 0, 1)
            if e["accepted"]:
                accepted.append((point, e["attempt_index"], e["attempt_id"]))
        else:
            _need(not e["accepted"])
    if accepted:
        winner = min(
            accepted,
            key=(lambda item: item[1])
            if p["round_stop"] == "first_accepted"
            else (lambda item: (-item[0], item[1])),
        )
        _need(p["proposed_attempt"] == winner[2])
    else:
        _need(p["proposed_attempt"] is None)


def _diagnostic_import(p: dict[str, Any]) -> None:
    _object(
        p,
        "format kind destination source_prefixes rows_digest slot_index gaps_digest counts authentication",
    )
    destination = _measurement(p["destination"])
    dest_by_sample = {
        (s["case_id"], s["sample_id"]): s for s in destination["identity"]["schedule"]
    }
    prefixes = _list(p["source_prefixes"])
    seen: set[tuple[str, str]] = set()
    for source in prefixes:
        _source(source)
        key = (source["prefix"]["run_id"], source["measurement_id"])
        _need(key not in seen)
        seen.add(key)
    _sha(p["rows_digest"])
    _sha(p["gaps_digest"])
    slots = _list(p["slot_index"])
    keys = set()
    for row in slots:
        # This is the canonical independently admitted scalar row, including
        # its original source, not a competing row-reference representation.
        validate_measurement_payload("sample_imported", row)
        slot = dest_by_sample.get((row["case_id"], row["sample_id"]))
        _need(slot is not None)
        assert slot is not None
        key = _slot(slot)
        _need(key not in keys)
        keys.add(key)
        # Provenance owns the original campaign ID; a prefix owns its S1 UUID.
        # These namespaces are independent. E authenticates original campaign
        # provenance, including transitive origins, against admitted rows/bundles.
        _need(bool(seen))  # The authenticated carrier can itself hold transitive imports.
    _need(p["rows_digest"] == _digest(slots))
    from evolution.tree.baseline_diagnostics import GAP_REASONS

    counts = _keys(p["counts"], set(GAP_REASONS))
    for count in counts.values():
        _integer(count)
    _authentication(p["authentication"])


def _authentication(value: Any) -> None:
    p = _object(value, "score request stdout")
    _need(p["score"] == "journal_bound" and p["stdout"] == "captured_at_import")
    _choice(p["request"], {"authenticated", "partial", "unavailable"})


SHADOW_CARRIERS: Final = frozenset(
    {
        "grouping_finished",
        "grouping_failed",
        "reflection_finished",
        "reflection_failed",
        "attempt_input_frozen",
        "editor_finished",
        "editor_failed",
        "attempt_closed",
    }
)

_EVENT_FIELDS: Final = {
    "check_shadowed": "check code count round operation_id",
    "auxiliary_payload_declared": "family_version purpose format content_digest files",
    "attempt_opened": "round attempt_index attempt_id parent_version parent_digest path",
    "attempt_closed": "attempt_id outcome reason path evidence",
    "comparison_preflight_failed": "round attempt_id epoch phase mode data reason",
    "comparison_bound": "round attempt_id epoch data candidate reference reference_kind",
    "comparison_completed": "round attempt_id binding data outcome reason deciding_scope candidate reference",
    "focus_decided": "round attempt_id binding outcome reason data",
    "gate_measurement_completed": "round attempt_id binding side source required admissible",
    "candidate_gate_sealed": "round attempt_id data source slots_digest",
    "candidate_completion_bound": "round attempt_id data finalist_closure gate_seal measurement_id identity_digest pairs_digest missing_count required imported",
    "candidate_completion_completed": "round attempt_id binding source required admissible",
    "final_measurement_bound": "round attempt_id data verification candidate_source champion_side finalist_closure source_ref candidate champion",
    "finalists_bound": "round count attempts_digest",
    "champion_final_bound": "round attempt_id finalists measurement_id identity_digest candidate_digest",
    "final_measurement_completed": "round attempt_id binding side source required admissible",
    "global_comparison_completed": "round attempt_id binding data outcome reason deciding_scope accepted candidate reference",
    "selection_decided": "round data reason selected_candidate selected_evidence next_champion",
    "diagnostic_import_declared": "measurement_id data bundle rows_digest scheduled included sufficiency authentication",
    # b16 §9 (ADR-0055): the chat Grouper's checkpoints, one per finished
    # portion and one per finished leaf batch, written before anything uses
    # the decision. Both name the plan they were asked under. A portion adds
    # `groups` or `outcome`, a batch `outcome` when its answer was not
    # admitted.
    "grouping_portion_finished": "grouping_id portion_id plan_digest input_digest requests",
    "grouping_batch_finished": "grouping_id level index plan_digest input_digest merges skipped requests",
}
#: The checkpoint events of a family-15 grouping (b16 §9).
GROUPING_CHECKPOINTS: Final = frozenset({"grouping_portion_finished", "grouping_batch_finished"})
_PORTION_ID = re.compile(r"p[1-9][0-9]*(?:\.[12])*\Z")
_BATCH_ID = re.compile(r"b[1-9][0-9]*(?:\.[12])*\Z")
_GROUP_KEYS: Final = frozenset(
    {
        "id",
        "title",
        "failure_mode",
        "expected_behaviour",
        "question",
        "priority",
        "citations",
        "metrics",
        "members",
        "affected_cases",
    }
)
_MERGE_HEADER: Final = frozenset(
    {"title", "failure_mode", "expected_behaviour", "question", "priority"}
)


def _validate_leak(value: Any) -> None:
    from evolution.tree.execution.json_pointer import _pointer_tokens
    from evolution.tree.target.connection_contract import MAX_POINTER_BYTES

    payload = _keys(value, {"case_id"}, {"oracle_pointer"})
    _identifier(payload["case_id"])
    if "oracle_pointer" in payload:
        pointer = payload["oracle_pointer"]
        _need(isinstance(pointer, str))
        _need(len(pointer.encode("utf-8")) <= MAX_POINTER_BYTES)
        _pointer_tokens(pointer)


def _validate_detail(value: Any) -> None:
    detail = _keys(value, {"category"}, {"path"})
    _need(detail["category"] in REFLECTION_DETAIL_CATEGORIES)
    if "path" in detail:
        _need(isinstance(detail["path"], str))
        _need(REFLECTION_DETAIL_PATH.fullmatch(detail["path"]) is not None)


def _validate_shadow_entry(value: Any) -> None:
    from evolution.tree.execution.check_modes import CODES

    entry = _keys(value, {"check", "code", "count"}, {"leak"})
    _need(isinstance(entry["check"], str) and entry["check"] in CODES)
    _need(isinstance(entry["code"], str) and entry["code"] in CODES[entry["check"]])
    _integer(entry["count"], 1)
    if "leak" in entry:
        _validate_leak(entry["leak"])


def validate_family15_payload(event: str, value: Any) -> None:
    """Validate only this explicit delta; old semantic handlers are never called."""
    try:
        if (
            event in {"reflection_finished", "reflection_failed"}
            and isinstance(value, dict)
            and "probe_availability" in value
        ):
            value = dict(value)
            availability = _keys(
                value.pop("probe_availability"),
                {"health", "mediator", "unreachable", "transport_failures"},
            )
            _choice(availability["health"], {"ready", "unavailable"})
            _choice(availability["mediator"], {"started", "failed"})
            _integer(availability["unreachable"])
            _integer(availability["transport_failures"])
        if (
            event == "probe_completed"
            and isinstance(value, dict)
            and value.get("error") == "unreachable"
        ):
            # Shape is inherited; only the bound lifecycle can authorize this error.
            value = {**value, "error": "transport"}
        if isinstance(value, dict) and "shadowed" in value:
            _need(event in SHADOW_CARRIERS)
            value = dict(value)
            entries = _list(value.pop("shadowed"))
            _need(bool(entries))
            for entry in entries:
                _validate_shadow_entry(entry)
        if event == "check_shadowed":
            p = _keys(value, {"check", "code", "count", "round", "operation_id"}, {"leak"})
            _validate_shadow_entry(
                {k: v for k, v in p.items() if k not in {"round", "operation_id"}}
            )
            if p["round"] is not None:
                _integer(p["round"], 1)
            if p["operation_id"] is not None:
                _identifier(p["operation_id"])
            return
        if (
            event in {"reflection_failed", "grouping_failed", "attempt_closed"}
            and isinstance(value, dict)
            and "leak" in value
        ):
            value = dict(value)
            leak = value.pop("leak")
            _need(
                value.get("error_class") == "oracle_leak"
                if event == "reflection_failed"
                else value.get("code") == "oracle_leak"
                and (event != "attempt_closed" or value.get("reason") == "seal_refused")
            )
            _validate_leak(leak)
        if event == "reflection_failed" and isinstance(value, dict) and "detail" in value:
            value = dict(value)
            detail = value.pop("detail")
            _need(value.get("error_class") != "oracle_leak")
            _validate_detail(detail)
        if event == "campaign_created":
            p = _keys(value, _FINAL_KEYS[event] | {"diagnostic_import_format"})
            _need(p["diagnostic_import_format"] == DIAGNOSTIC_FORMAT)
            _validate_created(p, family_version=15)
            return
        if event == "stage_opened" and "scope" in value:
            scoped = dict(value)
            _choice(scoped.pop("scope"), {"focus", "causal"})
            validate_payload(event, scoped, final=True, independent=True)
            return
        if event in INHERITED_EVENTS or (event == "attempt_closed" and "path" not in value):
            if event == "attempt_closed":
                _need(value.get("outcome") in {"abandoned", "insufficient_evidence"})
                _need(value.get("look") is None)
                _need(
                    value.get("reason")
                    not in REASONS | {"insufficient_calibration", "insufficient_repeats"}
                )
            validate_payload(event, value, final=True, independent=True)
            return
        _need(event in _EVENT_FIELDS, "event is not in family-15 contract")
        fields = _EVENT_FIELDS[event]
        if event == "comparison_bound" and "focus" in value:
            fields += " focus"
        if event == "gate_measurement_completed" and "scopes" in value:
            fields += " scopes"
        if event == "focus_decided" and "seed" in value:
            fields += " seed"
        if event == "grouping_portion_finished":
            fields += " groups" if isinstance(value, dict) and "groups" in value else " outcome"
        if event == "grouping_batch_finished" and "outcome" in value:
            fields += " outcome"
        p = _object(value, fields)
        _need(len(canonical_json(p)) <= MAX_RECORD_BYTES, "family-15 record exceeds bound")
        for name in ("round", "attempt_index"):
            if name in p:
                _integer(p[name], 1)
        for name in (
            "epoch",
            "required",
            "admissible",
            "missing_count",
            "scheduled",
            "included",
            "count",
            "imported",
        ):
            if name in p:
                _integer(p[name])
        for name in ("attempt_id", "measurement_id", "parent_version", "run_id"):
            if name in p:
                if p[name] is not None or event not in {
                    "champion_final_bound",
                    "final_measurement_completed",
                }:
                    _identifier(p[name])
        for name in p:
            if name.endswith("_digest"):
                _sha(p[name])
        for name in (
            "binding",
            "finalist_closure",
            "gate_seal",
            "evidence",
            "source_ref",
            "finalists",
        ):
            if name in p:
                if name != "finalists" or p[name] is not None:
                    _campaign_ref(p[name])
        if "data" in p:
            _publication(p["data"])
        _event_details(event, p)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError("invalid family-15 record") from exc


def _summary(value: Any, *, settled: bool = False) -> None:
    p = _object(value, "measurement_id identity_digest candidate_digest prefix")
    _identifier(p["measurement_id"])
    _sha(p["identity_digest"])
    _sha(p["candidate_digest"])
    if p["prefix"] is None:
        _need(not settled, "settled measurement needs prefix")
    else:
        _prefix(p["prefix"], p["measurement_id"])


def _text(value: Any, *, empty: bool = False) -> None:
    _need(isinstance(value, str) and (empty or bool(value)), "expected text")


def _checkpoint(event: str, p: dict[str, Any]) -> None:
    """A grouping checkpoint's shape (b16 §9); the lifecycle proves its grouping."""

    from evolution.tree.roles.grouping.consolidation import INVALID, UNSPLITTABLE
    from evolution.tree.roles.grouping.portion_session import OUTCOMES as PORTION_OUTCOMES

    _identifier(p["grouping_id"])
    _integer(p["requests"])
    if event == "grouping_portion_finished":
        _need(isinstance(p["portion_id"], str) and _PORTION_ID.fullmatch(p["portion_id"]))
        if "outcome" in p:
            _choice(p["outcome"], PORTION_OUTCOMES)
            return
        groups = _list(p["groups"])
        _need(bool(groups), "a portion checkpoint without groups names its outcome")
        for raw in groups:
            group = _keys(raw, set(_GROUP_KEYS), {"provenance"})
            _text(group["id"])
            # Admission scrubs a text that names an oracle value to empty.
            for name in ("title", "failure_mode", "expected_behaviour", "question", "priority"):
                _text(group[name], empty=True)
            if "provenance" in group:
                _text(group["provenance"], empty=True)
            for citation in _list(group["citations"]):
                _text(citation, empty=True)
            _need(isinstance(group["metrics"], dict), "expected metrics object")
            members = _list(group["members"])
            _need(bool(members), "a group has members")
            for member in members:
                m = _keys(member, {"evidence_id", "variant"})
                _text(m["evidence_id"])
                _text(m["variant"], empty=True)
            for case in _list(group["affected_cases"]):
                _text(case)
        return
    _integer(p["level"])
    _need(isinstance(p["index"], str) and _BATCH_ID.fullmatch(p["index"]))
    _integer(p["skipped"])
    if "outcome" in p:
        _choice(p["outcome"], {INVALID, UNSPLITTABLE})
        _need(not p["merges"] and p["skipped"] == 0, "an unadmitted batch merges nothing")
    for raw in _list(p["merges"]):
        _need(isinstance(raw, dict), "expected merge object")
        header = set(raw) - {"groups"}
        _need(header in (set(), set(_MERGE_HEADER)), "a merge header is complete or absent")
        merge = _keys(raw, {"groups"} | header)
        ids = _list(merge["groups"])
        _need(len(ids) >= 2 and len(set(ids)) == len(ids), "a merge joins distinct groups")
        for local in ids:
            _text(local)
        for name in header:
            _text(merge[name], empty=True)


def _event_details(event: str, p: dict[str, Any]) -> None:
    if event in GROUPING_CHECKPOINTS:
        _checkpoint(event, p)
        return
    if event in {
        "comparison_bound",
        "comparison_completed",
        "global_comparison_completed",
        "final_measurement_bound",
    }:
        names = (
            ("candidate", "champion")
            if event == "final_measurement_bound"
            else ("candidate", "reference")
        )
        for name in names:
            _summary(
                p[name],
                settled=event in {"comparison_completed", "global_comparison_completed"},
            )
    if event == "comparison_bound":
        if "focus" in p:
            focus = _object(p["focus"], "seq digest outcome")
            _integer(focus["seq"], 1)
            _sha(focus["digest"])
            _need(focus["outcome"] == "passed", "only a passing focus can be carried")
        _choice(p["reference_kind"], {"stored", "fresh"})
        _summary(p["reference"], settled=p["reference_kind"] == "stored")
    if event == "candidate_completion_bound":
        _need(
            p["required"] > 0
            and p["imported"] >= 0
            and p["required"] == p["imported"] + p["missing_count"]
        )
    if event == "gate_measurement_completed":
        if "scopes" in p:
            _need(p["scopes"] in (["focus"], ["focus", "causal"]))
        _choice(p["side"], {"candidate", "parent"})
        _source(p["source"])
        _need(p["required"] > 0 and p["required"] == p["admissible"])
    if event == "final_measurement_completed":
        _choice(p["side"], {"candidate", "champion"})
        _need(p["side"] != "candidate" or p["attempt_id"] is not None)
        _source(p["source"])
        _need(p["required"] > 0 and p["required"] == p["admissible"])
    if event == "auxiliary_payload_declared":
        # Reuse the reviewed declaration contract, without inventing a future
        # campaign byte reference. The real reference is bound after append.
        fields = tuple(AuxiliaryFile.from_payload(f) for f in _list(p["files"]))
        AuxiliaryDeclaration(
            p["family_version"],
            p["purpose"],
            p["format"],
            p["content_digest"],
            fields,
            AuxiliarySourceReference(
                1, "journal_record", "shape-validation", "campaign.jsonl#bytes=0:1", "0" * 64
            ),
        )
    elif event in ("attempt_opened", "attempt_closed"):
        _choice(p["path"], {"early_global", "candidate"})
        if event == "attempt_closed":
            _choice(p["outcome"], {"rejected", "finalist"})
            _choice(p["reason"], REASONS | {"structurally_unattainable"})
            if p["path"] == "early_global":
                _need(p["outcome"] == "rejected" and p["reason"] == "structurally_unattainable")
            else:
                _need((p["outcome"] == "finalist") == (p["reason"] == "passed"))
    elif event == "comparison_preflight_failed":
        _choice(p["mode"], {"point", "confidence", "statistical"})
        _choice(p["phase"], {"early_global", "attempt"})
        _need(p["mode"] != "statistical" or p["phase"] == "early_global")
        _need(p["reason"] == "structurally_unattainable")
    elif event == "focus_decided":
        _outcome({**p, "deciding_scope": "focus"})
        if "seed" in p:
            from evolution.tree.confidence_statistics import derive_seed

            seed = _object(
                p["seed"], "method campaign_seed binding scope prefixes look purpose seed"
            )
            _need(seed["method"] == BOOTSTRAP_METHOD and seed["scope"] == "focus")
            _need(seed["purpose"] == "gate" and type(seed["campaign_seed"]) is int)
            _sha(seed["binding"])
            _integer(seed["look"], 2)
            _need(all(isinstance(prefix, str) for prefix in _list(seed["prefixes"])))
            _need(
                type(seed["seed"]) is int
                and seed["seed"]
                == derive_seed(**{k: v for k, v in seed.items() if k not in {"method", "seed"}})
            )
    elif event in ("comparison_completed", "global_comparison_completed"):
        _outcome(p)
        if event == "global_comparison_completed":
            _bool(p["accepted"])
            _need(p["accepted"] == (p["outcome"] == "passed"))
    elif event == "candidate_gate_sealed":
        _source(p["source"])
    elif event == "candidate_completion_completed":
        _source(p["source"])
        _need(
            p["required"] > 0 and p["admissible"] == p["required"],
            "completion requires full Candidate coverage",
        )
    elif event == "final_measurement_bound":
        _verification(p)
    elif event == "selection_decided":
        _choice(p["reason"], {"automatic", "no_eligible_candidate"})
        _need((p["selected_candidate"] is None) == (p["reason"] == "no_eligible_candidate"))
        next_champion = _source(p["next_champion"])
        if p["selected_candidate"] is None:
            _need(p["selected_evidence"] is None)
        else:
            _sha(p["selected_candidate"])
            source = _source(p["selected_evidence"])
            _need(source["candidate_digest"] == p["selected_candidate"] and source == next_champion)
    elif event == "diagnostic_import_declared":
        _publication(p["bundle"], "baseline_diagnostics")
        _need(p["included"] <= p["scheduled"])
        expected = (
            "complete"
            if p["included"] == p["scheduled"]
            else "partial"
            if p["included"]
            else "unavailable"
        )
        _need(p["sufficiency"] == expected)
        _authentication(p["authentication"])


# Only these unchanged operational contracts are inherited. Statistical and
# selection events are explicit deltas above; old looks/alpha/calibration and
# post-Promotion completion are deliberately absent.
INHERITED_EVENTS: Final = frozenset(
    {
        "baseline_bound",
        "baseline_complete",
        "measurement_bound",
        "parent_measurement_bound",
        "baseline_refresh_bound",
        "baseline_refresh_completed",
        "connection_preflight_started",
        "connection_preflight_finished",
        "connection_preflight_failed",
        "connection_preflight_interrupted",
        "editor_started",
        "editor_finished",
        "editor_failed",
        "attempt_sealed",
        "candidate_inspected",
        "affected_cases_selected",
        "stage_opened",
        "instance_attested",
        "indeterminate_observed",
        "pairs_excluded",
        "repairs_dispatched",
        "stage_redispatched",
        "threshold_reached",
        "capacity_exhausted",
        "round_closed",
        "campaign_finished",
        "focus_selected",
        "grouping_started",
        "grouping_finished",
        "grouping_failed",
        "focus_portion_selected",
        "attempt_input_frozen",
        "probe_checked",
        "reflection_started",
        "probe_completed",
        "reflection_finished",
        "reflection_failed",
        "gate_scope_selected",
        "identity_rebased",
        "authorization_applied",
        "measurement_rebound",
        "campaign_suspended",
    }
)
FAMILY15_CONTRACT: Final = EnvelopeFamily(
    validate_family15_payload,
    SUSPENDABLE_NO_OPERATION_EVENTS | frozenset({"campaign_created", *_EVENT_FIELDS}),
    FINAL_CAMPAIGN_FAMILY.is_start,
)


@dataclass(frozen=True)
class Family15Record:
    """Immutable validated event plus its exact local campaign byte authority.

    Constructor proves intrinsic reference identity, not existence in a log.
    The lifecycle supplies that membership/order proof before storing it.
    """

    envelope: Envelope
    reference: AuxiliarySourceReference

    def __post_init__(self) -> None:
        raw = self.envelope.to_payload()
        Envelope.from_payload(raw, family=FAMILY15_CONTRACT)
        _need(self.reference.kind == "journal_record")
        _need(raw["run_id"] == self.reference.run_id)
        encoded = self.envelope.encode()
        _need(hashlib.sha256(encoded).hexdigest() == self.reference.content_digest)
        start, end = self.reference.locator.split("#bytes=")[1].split(":")
        _need(int(end) - int(start) == len(encoded))

    @property
    def payload(self) -> dict[str, Any]:
        return self.envelope.payload


@dataclass(frozen=True)
class Family15AttemptState:
    """Family-specific adjunct to AttemptState, populated by CampaignLifecycle."""

    path: str = "candidate"
    preflight: Family15Record | None = None
    comparison_binding: Family15Record | None = None
    comparison: Family15Record | None = None
    focus_decision: Family15Record | None = None
    gate_candidate: Family15Record | None = None
    gate_parent: Family15Record | None = None
    gate_seal: Family15Record | None = None
    completion_binding: Family15Record | None = None
    completion: Family15Record | None = None
    final_binding: Family15Record | None = None
    global_result: Family15Record | None = None
    final_completion: Family15Record | None = None

    def __post_init__(self) -> None:
        _choice(self.path, {"candidate", "early_global"})
        for name, event in (
            ("preflight", "comparison_preflight_failed"),
            ("comparison_binding", "comparison_bound"),
            ("comparison", "comparison_completed"),
            ("focus_decision", "focus_decided"),
            ("gate_candidate", "gate_measurement_completed"),
            ("gate_parent", "gate_measurement_completed"),
            ("gate_seal", "candidate_gate_sealed"),
            ("completion_binding", "candidate_completion_bound"),
            ("completion", "candidate_completion_completed"),
            ("final_binding", "final_measurement_bound"),
            ("global_result", "global_comparison_completed"),
            ("final_completion", "final_measurement_completed"),
        ):
            record = getattr(self, name)
            _need(
                record is None
                or isinstance(record, Family15Record)
                and record.envelope.event == event
            )

        for name, side in (("gate_candidate", "candidate"), ("gate_parent", "parent")):
            record = getattr(self, name)
            _need(record is None or record.payload["side"] == side)


@dataclass(frozen=True)
class Family15RoundState:
    """None means no durable authority, never owed inference."""

    finalists: Family15Record | None = None
    champions: tuple[Family15Record, ...] = ()
    champion_completions: tuple[Family15Record, ...] = ()
    selection: Family15Record | None = None

    def __post_init__(self) -> None:
        for records, event in (
            (self.champions, "champion_final_bound"),
            (self.champion_completions, "final_measurement_completed"),
        ):
            _need(isinstance(records, tuple))
            for record in records:
                _need(isinstance(record, Family15Record) and record.envelope.event == event)
        for name, event in (
            ("finalists", "finalists_bound"),
            ("selection", "selection_decided"),
        ):
            record = getattr(self, name)
            _need(
                record is None
                or isinstance(record, Family15Record)
                and record.envelope.event == event
            )


@dataclass(frozen=True)
class Family15Grouping:
    """One grouping of the run with its checkpoints (b16 §9), kept for adoption.

    ``status`` is ``started``, ``finished`` or ``failed`` with the failure's
    ``code`` and ``error_class``. ``portions`` maps a portion id, ``batches``
    a ``(level, index)``, to its checkpoint record; ``plan_digest`` is the one
    plan every portion and batch checkpoint of the grouping names.
    """

    grouping_id: str
    round: int
    baseline_binding: str
    policy_digest: str
    status: str = "started"
    code: str | None = None
    error_class: str | None = None
    plan_digest: str | None = None
    portions: Mapping[str, Family15Record] = field(default_factory=dict)
    batches: Mapping[tuple[int, str], Family15Record] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _need(self.status in {"started", "finished", "failed"})
        for name, event in (
            ("portions", "grouping_portion_finished"),
            ("batches", "grouping_batch_finished"),
        ):
            records = MappingProxyType(dict(getattr(self, name)))
            for record in records.values():
                _need(isinstance(record, Family15Record) and record.envelope.event == event)
            object.__setattr__(self, name, records)


@dataclass(frozen=True)
class Family15SnapshotState:
    """Immutable family-specific recovery indexes, separate from old cohorts."""

    current_measurements: Mapping[str, str] = field(default_factory=dict)
    declarations: tuple[Family15Record, ...] = ()
    diagnostic_imports: tuple[Family15Record, ...] = ()
    check_modes: Mapping[str, str] = field(default_factory=dict)
    shadow_hits: Mapping[str, int] = field(default_factory=dict)
    #: Every grouping of the run in journal order, with its checkpoints (b16 §9).
    groupings: Mapping[str, Family15Grouping] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "current_measurements", MappingProxyType(dict(self.current_measurements))
        )
        for name in ("check_modes", "shadow_hits", "groupings"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for records, event in (
            (self.declarations, "auxiliary_payload_declared"),
            (self.diagnostic_imports, "diagnostic_import_declared"),
        ):
            _need(isinstance(records, tuple))
            for record in records:
                _need(isinstance(record, Family15Record) and record.envelope.event == event)
