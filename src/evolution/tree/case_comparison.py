"""Family-15 frozen exact comparisons over authenticated, settled evidence.

Provenance/selection/drift/attestation remain their existing owners' admission
checks. This pure layer does not dispatch or reinterpret unpaired seeds as a
provenance failure. All scheduled Candidate slots precede reference exclusions.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal, Protocol

from evolution.tree.campaign_policy import CampaignPolicyV11, CampaignPolicyV12
from evolution.tree.campaign_statistics import MissingMetric, SampleRow
from evolution.tree.case_statistics import METHOD, exact_e, lower_enclosure, passes_bound
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.family15_schema import (
    decode_rational,
    encode_rational,
    validate_comparison_data,
)
from evolution.tree.statistics import ScoreSample

q = encode_rational


def source_summary(measurement: Mapping[str, Any]) -> dict[str, Any]:
    return dict(
        measurement_id=measurement["measurement_id"],
        identity_digest=measurement["identity_digest"],
        candidate_digest=measurement["identity"]["candidate_digest"],
        prefix=json.loads(canonical_json(measurement["prefix"])),
    )


def count_weights(ids: Sequence[str], groups: Mapping[str, str]) -> dict[str, Fraction]:
    sizes = Counter(groups[case] for case in ids)
    return {case: Fraction(1, len(sizes) * sizes[groups[case]]) for case in ids}


@dataclass(frozen=True)
class ComparisonProfile:
    """Immutable canonical binding, primary metric and original corpus groups."""

    encoded: bytes
    primary: str
    groups: tuple[tuple[str, str], ...]
    policy: CampaignPolicyV12 | None = None
    focus_binding_digest: str | None = None

    @classmethod
    def from_payload(
        cls,
        body: Mapping[str, Any],
        *,
        primary: str,
        groups: Mapping[str, str],
        policy: CampaignPolicyV11 | None = None,
        focus_binding_digest: str | None = None,
    ) -> ComparisonProfile:
        encoded = canonical_json(body)
        if policy is not None and not isinstance(policy, CampaignPolicyV12):
            raise ValueError("family-15 comparison requires its family-15 policy")
        validate_comparison_data(json.loads(encoded), policy=policy)
        return cls(encoded, primary, tuple(groups.items()), policy, focus_binding_digest)

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self.encoded)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.encoded).hexdigest()


@dataclass(frozen=True)
class ComparisonResult:
    """Canonical result; exact values never become journal floats.

    Observed ``variation`` is unbiased Candidate primary-score repeat variance
    (score-squared units), not contrast uncertainty or reference/provider noise.
    """

    encoded: bytes

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self.encoded)

    @property
    def outcome(self) -> str:
        return str(self.to_payload()["outcome"])

    @property
    def reason(self) -> str:
        return str(self.to_payload()["reason"])


def fallback_reason(
    scope: Mapping[str, Any], policy: CampaignPolicyV12, *, threshold: Fraction
) -> Literal["focus_unattainable", "causal_unconfirmable"] | None:
    """Test frozen bound support, never observed Candidate contrasts.

    Known reference values give focus ceilings; their absence is the fresh
    score-range envelope. Causal always tests the unchanged all-zero outcome.
    The threshold already reserves execution allowance exactly once.
    """
    name = scope["name"]
    if name not in {"focus", "causal"} or scope["rule"] != "bound" or not scope["cases"]:
        return None
    weights = {c["case_id"]: decode_rational(c["weight"]) for c in scope["cases"]}
    ceilings = {
        c["case_id"]: (
            1 - sum(map(decode_rational, c["reference_values"]), Fraction()) / len(c["repeats"])
            if "reference_values" in c
            else Fraction(1)
        )
        if name == "focus"
        else Fraction()
        for c in scope["cases"]
    }
    masses = [decode_rational(u["weight"]) for u in scope["units"]]
    values = [
        sum((weights[c] * ceilings[c] for c in u["cases"]), Fraction()) / mass
        for u, mass in zip(scope["units"], masses, strict=True)
    ]
    level = policy.focus_confidence if name == "focus" else policy.causal_confidence
    if level is None:
        raise ValueError("fallback needs the bound scope confidence")
    if passes_bound(values, masses, threshold, Fraction(level), strict=scope["strict"]):
        return None
    return "focus_unattainable" if name == "focus" else "causal_unconfirmable"


def _point_fallback(scope: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        **{k: v for k, v in scope.items() if k not in {"confidence", "units"}},
        "rule": "point",
        "fallback": {"reason": reason},
    }


def build_profile(
    *,
    policy: Any,
    corpus: Sequence[Mapping[str, Any]],
    scopes: Mapping[str, Sequence[str]],
    candidate: Mapping[str, Any] | None,
    reference: Mapping[str, Any],
    reference_rows: Sequence[SampleRow],
    epoch: int,
    reference_kind: str,
) -> ComparisonProfile:
    """Freeze exact counts, policy, retained slots and known reference values."""
    groups = {str(c["case_id"]): str(c["group_id"]) for c in corpus}
    rows = {(r.case_id, r.repeat): r for r in reference_rows}
    excluded = [
        s
        for s in reference["identity"]["schedule"]
        if reference_kind == "stored"
        and (s["case_id"], s["repeat"]) in rows
        and rows[s["case_id"], s["repeat"]].exhausted
        and rows[s["case_id"], s["repeat"]].disposition != "score"
    ]
    mask = {(s["case_id"], s["repeat"]) for s in excluded}
    noise = dict(policy.execution_noise)
    allowance = Fraction(noise["allowance"]) if noise["kind"] == "bounded" else Fraction(0)
    if noise["kind"] == "bounded":
        noise["allowance"] = q(allowance)
    bound_scopes = []
    calibration = None
    for name, declared in scopes.items():
        slots = [
            s
            for s in reference["identity"]["schedule"]
            if s["case_id"] in declared and (s["case_id"], s["repeat"]) not in mask
        ]
        ids = [case for case in declared if any(s["case_id"] == case for s in slots)]
        weights = count_weights(ids, groups)
        if name == "global" and ids:
            full = count_weights(list(groups), groups)
            total = sum((full[case] for case in ids), Fraction())
            weights = {case: full[case] / total for case in ids}
        cases = []
        for case in ids:
            repeats = sorted(s["repeat"] for s in slots if s["case_id"] == case)
            item: dict[str, Any] = dict(
                case_id=case, group=groups[case], weight=q(weights[case]), repeats=repeats
            )
            if reference_kind == "stored":
                values = []
                for repeat in repeats:
                    row = rows[case, repeat]
                    if not admissible(row, policy.metric_primary):
                        raise ValueError("stored reference is not settled admissible evidence")
                    assert row.metrics is not None
                    values.append(q(Fraction(row.metrics[policy.metric_primary])))
                item["reference_values"] = values
            cases.append(item)
        rule = (
            policy.global_mode
            if name == "global"
            else {
                "point": "point",
                "confidence": "bound",
                "unfiltered": "off",
                "statistical": "bootstrap",
            }[policy.gate_mode]
        )
        scope = dict(name=name, rule=rule, declared_cases=list(declared), cases=cases)
        if name == "causal" and policy.causal_tolerance == "measured_noise":
            # A null threshold is explicitly unknown/skipped, never a known q=0.
            scope.update(tolerance="measured_noise", threshold=None, strict=False)
            if cases and reference_kind == "stored":
                from evolution.tree.family15_statistics import calibrate, calibration_seed_binding

                causal = [(c["case_id"], weights[c["case_id"]]) for c in cases]
                calibration = calibrate(
                    _score_samples(reference_rows, groups, policy.metric_primary, slots),
                    causal,
                    campaign_seed=policy.random_seed,
                    seed_binding=calibration_seed_binding(
                        policy.digest, reference["measurement_id"], reference["prefix"], causal
                    ),
                    prefix=reference["prefix"],
                    source="stored",
                    measurement_id=reference["measurement_id"],
                )
                if calibration.allowance is not None:
                    scope["threshold"] = q(allowance - calibration.allowance)
        elif rule != "off":
            threshold = (
                Fraction(policy.focus_margin)
                if name == "focus"
                else -Fraction(policy.causal_tolerance)
                if name == "causal"
                else Fraction()
            )
            scope.update(threshold=q(threshold + allowance), strict=name != "causal")
        if rule in {"bound", "bootstrap"}:
            confidence = (
                policy.focus_confidence
                if name == "focus"
                else policy.causal_confidence
                if name == "causal"
                else max(policy.focus_confidence, policy.causal_confidence)
            )
            if rule == "bootstrap":
                from evolution.tree.confidence_statistics import confidence_fraction

                # Preserve the repeat kernel's published-confidence convention;
                # bound-method confidence and score/threshold inputs stay binary-exact.
                scope["confidence"] = q(confidence_fraction(confidence))
            else:
                scope["confidence"] = q(Fraction(confidence))
        if rule == "bound":
            assert policy.statistics is not None
            unit = policy.statistics["unit"]
            members: dict[str, list[str]] = {}
            for case in ids:
                members.setdefault(case if unit == "case" else groups[case], []).append(case)
            scope.update(
                confidence=q(Fraction(confidence)),
                units=[
                    dict(
                        unit_id=key,
                        cases=members[key],
                        weight=q(sum((weights[c] for c in members[key]), Fraction())),
                    )
                    for key in members
                ],
            )
        if policy.bound_fallback == "point" and scope.get("threshold") is not None:
            reason = fallback_reason(scope, policy, threshold=decode_rational(scope["threshold"]))
            if reason is not None:
                scope = _point_fallback(scope, reason)
        bound_scopes.append(scope)
    body = dict(
        format="comparison-data/1",
        kind="binding",
        epoch=epoch,
        policy_digest=policy.digest,
        mode=policy.gate_mode,
        execution_noise=noise,
        reference=dict(reference),
        reference_kind=reference_kind,
        scopes=bound_scopes,
        excluded_reference=excluded,
    )
    if calibration is not None:
        if calibration.payload is not None:
            body["calibration"] = dict(calibration.payload)
        else:
            body["calibration_unavailable"] = calibration.unavailable
    if policy.gate_mode == "confidence":
        body["statistics"] = dict(method=METHOD, profile=METHOD, unit=policy.statistics["unit"])
    elif policy.gate_mode == "statistical":
        from evolution.tree.confidence_statistics import METHOD_VERSION

        body["statistics"] = dict(method=METHOD_VERSION)
    if candidate is None:
        # Preflight-only input: validate its closed form once results are attached.
        return ComparisonProfile(
            canonical_json(body), policy.metric_primary, tuple(groups.items()), policy
        )
    body["candidate"] = dict(candidate)
    return ComparisonProfile.from_payload(
        body, primary=policy.metric_primary, groups=groups, policy=policy
    )


class _ScoredRow(Protocol):
    @property
    def disposition(self) -> str: ...

    @property
    def metrics(self) -> Mapping[str, float] | None: ...


def admissible(row: _ScoredRow, primary: str) -> bool:
    return (
        row.disposition == "score"
        and row.metrics is not None
        and primary in row.metrics
        and 0 <= row.metrics[primary] <= 1
    )


def _index(
    rows: Sequence[SampleRow], schedule: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, int], SampleRow]:
    slots = {(s["case_id"], s["repeat"]): s for s in schedule}
    result = {}
    for row in rows:
        key = row.case_id, row.repeat
        if key in result or key not in slots:
            raise ValueError("duplicate or unscheduled comparison row")
        slot = slots[key]
        if row.sample_id != slot["sample_id"] or row.requested_seed != slot["requested_seed"]:
            raise ValueError("comparison row differs from frozen slot")
        result[key] = row
    return result


def _score_samples(
    rows: Sequence[SampleRow],
    groups: Mapping[str, str],
    primary: str,
    slots: Sequence[Mapping[str, Any]],
) -> dict[str, list[ScoreSample]]:
    """Convert only admissible frozen slots, in schedule order, for the repeat kernel."""
    from evolution.tree.campaign_statistics import score_samples_of

    keys = {(s["case_id"], s["repeat"]) for s in slots}
    indexed = _index([r for r in rows if (r.case_id, r.repeat) in keys], slots)
    converted = score_samples_of(
        [
            indexed[s["case_id"], s["repeat"]]
            for s in slots
            if (s["case_id"], s["repeat"]) in indexed
            and admissible(indexed[s["case_id"], s["repeat"]], primary)
        ],
        {case: (group, "bound_corpus") for case, group in groups.items()},
        primary,
        max(s["repeat"] for s in slots),
    )
    if isinstance(converted, MissingMetric):
        raise ValueError("admissible comparison row is missing its metric")
    return {case: [s for s in converted if s.case_id == case] for case in groups}


def evaluate_comparison(
    profile: ComparisonProfile,
    candidate: Sequence[SampleRow],
    reference: Sequence[SampleRow],
    *,
    candidate_source: Mapping[str, Any] | None = None,
    reference_source: Mapping[str, Any] | None = None,
    schedules: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    campaign_seed: int | None = None,
) -> ComparisonResult:
    return _evaluate_comparison(
        profile,
        candidate,
        reference,
        candidate_source=candidate_source,
        reference_source=reference_source,
        schedules=schedules,
        campaign_seed=campaign_seed,
    )


def evaluate_focus(
    profile: ComparisonProfile,
    candidate: Sequence[SampleRow],
    reference: Sequence[SampleRow],
    *,
    candidate_source: Mapping[str, Any] | None = None,
    reference_source: Mapping[str, Any] | None = None,
    campaign_seed: int | None = None,
) -> ComparisonResult:
    """Decide only F, using the full immutable binding and its focus seed."""
    result = _evaluate_comparison(
        profile,
        candidate,
        reference,
        candidate_source=candidate_source,
        reference_source=reference_source,
        campaign_seed=campaign_seed,
        scope_name="focus",
    )

    if profile.policy is not None and profile.policy.focus_first:
        validate_comparison_data(
            result.to_payload(), binding=profile.to_payload(), policy=profile.policy
        )
    return result


def focus_rejection(profile: ComparisonProfile, focus_result: ComparisonResult) -> ComparisonResult:
    """Complete a nonpassing F decision without inspecting or evaluating C rows."""
    payload = focus_result.to_payload()
    if payload["outcome"] == "passed" or payload["binding_digest"] != profile.digest:
        raise ValueError("focus rejection requires this binding's nonpassing focus")
    scope = profile.to_payload()["scopes"][1]
    weights = count_weights(scope["declared_cases"], dict(profile.groups))
    mass = sum((weights[c["case_id"]] for c in scope["cases"]), Fraction())
    causal = dict(
        name="causal",
        rule=scope["rule"],
        status="not_measured" if scope["declared_cases"] else "skipped",
        declared_count=len(scope["declared_cases"]),
        point=None,
        unit_count=len(scope.get("units", scope["cases"])),
        mass=q(mass),
        excluded_mass=q((1 if scope["declared_cases"] else 0) - mass),
        unavailable=None,
    )
    if scope["rule"] == "bound":
        causal.update(confidence=scope["confidence"], enclosure=None)
    elif scope["rule"] == "bootstrap":
        causal.update(confidence=scope["confidence"], lower=None, exact=None, pairing=None)
    if "fallback" in scope:
        causal["fallback"] = scope["fallback"]
    if scope.get("tolerance") == "measured_noise":
        causal["threshold"] = scope["threshold"]
    payload["scopes"].append(causal)
    if profile.policy is not None and profile.policy.focus_first:
        validate_comparison_data(payload, binding=profile.to_payload(), policy=profile.policy)
    return ComparisonResult(canonical_json(payload))


def evaluate_causal_after_focus(
    profile: ComparisonProfile,
    focus_result: ComparisonResult,
    candidate: Sequence[SampleRow],
    reference: Sequence[SampleRow],
    *,
    candidate_source: Mapping[str, Any] | None = None,
    reference_source: Mapping[str, Any] | None = None,
    campaign_seed: int | None = None,
) -> ComparisonResult:
    """Decide C once and copy the already durable F result without remeasurement."""
    focus = focus_result.to_payload()
    if focus["outcome"] != "passed" or focus["binding_digest"] != (
        profile.focus_binding_digest or profile.digest
    ):
        raise ValueError("causal evaluation requires this binding's passing focus")
    causal = _evaluate_comparison(
        profile,
        candidate,
        reference,
        candidate_source=candidate_source,
        reference_source=reference_source,
        campaign_seed=campaign_seed,
        scope_name="causal",
    ).to_payload()
    body = profile.to_payload()
    payload = dict(causal)
    payload["scopes"] = [focus["scopes"][0], causal["scopes"][0]]
    payload["retained_pairs"] = focus["retained_pairs"] + causal["retained_pairs"]
    diagnostics = {
        d["case_id"]: d for d in focus["repeat_diagnostics"] + causal["repeat_diagnostics"]
    }
    payload["repeat_diagnostics"] = [
        diagnostics[case]
        for case in dict.fromkeys(s["case_id"] for s in body["candidate"]["identity"]["schedule"])
    ]
    if body["execution_noise"]["kind"] == "observed":
        payload["variation"] = (
            "unavailable" if any(d["unavailable"] for d in diagnostics.values()) else "observed"
        )
    for side, rows in (("candidate", candidate), ("reference", reference)):
        schedule = body[side]["identity"]["schedule"]
        indexed = _index(rows, schedule)
        payload["coverage"][side] = dict(
            required=len(schedule),
            settled=sum(r.disposition == "score" or r.exhausted for r in indexed.values()),
            admissible=sum(admissible(r, profile.primary) for r in indexed.values()),
        )
    if causal["outcome"] == "passed" or causal["reason"] == "critical_failed":
        payload["deciding_scope"] = "focus"
    if not body["scopes"][1]["declared_cases"] and "bootstrap" in focus:
        payload["bootstrap"] = focus["bootstrap"]
    caveats = sorted(set(focus.get("caveats", [])) | set(causal.get("caveats", [])))
    if caveats:
        payload["caveats"] = caveats
    validate_comparison_data(
        payload,
        binding=body,
        policy=profile.policy
        if profile.policy is not None
        and (profile.policy.bound_fallback is not None or profile.policy.focus_first)
        else None,
    )
    return ComparisonResult(canonical_json(payload))


def _evaluate_comparison(
    profile: ComparisonProfile,
    candidate: Sequence[SampleRow],
    reference: Sequence[SampleRow],
    *,
    candidate_source: Mapping[str, Any] | None = None,
    reference_source: Mapping[str, Any] | None = None,
    schedules: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    campaign_seed: int | None = None,
    scope_name: str | None = None,
) -> ComparisonResult:
    """One deciding look. Coverage, critical veto, then exact ordered scope rules."""
    body = profile.to_payload()
    groups = dict(profile.groups)
    schedules = schedules or {
        side: body[side]["identity"]["schedule"] for side in ("candidate", "reference")
    }
    if scope_name is not None:
        body["scopes"] = [scope for scope in body["scopes"] if scope["name"] == scope_name]
        cases = set(body["scopes"][0]["declared_cases"])
        schedules = {
            side: [slot for slot in slots if slot["case_id"] in cases]
            for side, slots in schedules.items()
        }
        candidate = [row for row in candidate if row.case_id in cases]
        reference = [row for row in reference if row.case_id in cases]
        if scope_name == "focus":
            body.pop("calibration", None)
            body.pop("calibration_unavailable", None)
    indexed = {
        "candidate": _index(candidate, schedules["candidate"]),
        "reference": _index(reference, schedules["reference"]),
    }
    coverage = {
        side: dict(
            required=len(schedules[side]),
            settled=sum(r.disposition == "score" or r.exhausted for r in indexed[side].values()),
            admissible=sum(admissible(r, profile.primary) for r in indexed[side].values()),
        )
        for side in schedules
    }
    incomplete = (
        "candidate_incomplete"
        if coverage["candidate"]["admissible"] != coverage["candidate"]["required"]
        else "reference_incomplete"
        if body["reference_kind"] == "fresh"
        and coverage["reference"]["admissible"] != coverage["reference"]["required"]
        else None
    )
    outcome, reason = "passed", "passed"
    deciding = body["scopes"][0]["name"]
    if incomplete:
        outcome, reason = "comparison_unavailable", incomplete
    elif any(row.critical_pass is False for row in candidate):
        outcome, reason = "comparison_not_passed", "critical_failed"
    causal = next((s for s in body["scopes"] if s.get("tolerance") == "measured_noise"), None)
    calibration = body.get("calibration")
    calibration_unavailable = body.get("calibration_unavailable")
    causal_threshold = None if causal is None else causal["threshold"]
    if causal is not None and causal["cases"] and body["reference_kind"] == "fresh":
        from evolution.tree.family15_statistics import calibrate

        if campaign_seed is None:
            raise ValueError("fresh calibration requires its bound campaign seed")
        calibration_slots = {(c["case_id"], r) for c in causal["cases"] for r in c["repeats"]}
        calibrated = calibrate(
            _score_samples(
                reference,
                groups,
                profile.primary,
                [
                    s
                    for s in schedules["reference"]
                    if (s["case_id"], s["repeat"]) in calibration_slots
                ],
            ),
            [(c["case_id"], decode_rational(c["weight"])) for c in causal["cases"]],
            campaign_seed=campaign_seed,
            seed_binding=profile.digest,
            prefix=(reference_source or body["reference"])["prefix"],
            source="fresh",
            measurement_id=(reference_source or body["reference"])["measurement_id"],
        )
        calibration = None if calibrated.payload is None else dict(calibrated.payload)
        calibration_unavailable = calibrated.unavailable
        if calibrated.allowance is not None:
            noise = body["execution_noise"]
            allowance = (
                decode_rational(noise["allowance"]) if noise["kind"] == "bounded" else Fraction()
            )
            causal_threshold = q(allowance - calibrated.allowance)
    if calibration_unavailable and reason == "passed":
        outcome, reason, deciding = "comparison_unavailable", calibration_unavailable, "causal"
    bootstrap = None
    if body["mode"] == "statistical":
        from evolution.tree.family15_statistics import bootstrap_scopes

        if campaign_seed is None:
            raise ValueError("statistical comparison requires its bound campaign seed")
        sources = {
            "candidate": dict(candidate_source or source_summary(body["candidate"])),
            "reference": dict(reference_source or source_summary(body["reference"])),
        }
        samples = {
            side: _score_samples(
                list(indexed[side].values()), groups, profile.primary, schedules[side]
            )
            if schedules[side]
            else {}
            for side in ("candidate", "reference")
        }
        bootstrap = bootstrap_scopes(
            profile,
            samples["candidate"],
            samples["reference"],
            campaign_seed=campaign_seed,
            binding_digest=profile.digest,
            prefixes=[sources[side]["prefix"]["locator"] for side in ("candidate", "reference")],
            scope_name=scope_name,
        )
        # Unavailable evidence takes precedence over numerical threshold failures.
        if reason == "passed":
            for name, scope_estimate in bootstrap.scopes.items():
                if scope_estimate.unavailable:
                    outcome, reason, deciding = (
                        "comparison_unavailable",
                        scope_estimate.unavailable,
                        name,
                    )
                    break
    results = []
    for scope in body["scopes"]:
        measured = scope.get("tolerance") == "measured_noise"
        if measured:
            scope = {**scope, "threshold": causal_threshold}
            if (
                body["reference_kind"] == "fresh"
                and causal_threshold is not None
                and profile.policy is not None
                and profile.policy.bound_fallback == "point"
            ):
                fallback = fallback_reason(
                    scope, profile.policy, threshold=decode_rational(causal_threshold)
                )
                if fallback is not None:
                    scope = _point_fallback(scope, fallback)
        declared = scope["declared_cases"]
        cases = scope["cases"]
        original = count_weights(declared, groups)
        mass = sum((original[c["case_id"]] for c in cases), Fraction())
        diffs = {}
        for case in cases:
            values = []
            for repeat in case["repeats"]:
                key = case["case_id"], repeat
                c, r = indexed["candidate"].get(key), indexed["reference"].get(key)
                if (
                    c is None
                    or r is None
                    or not admissible(c, profile.primary)
                    or not admissible(r, profile.primary)
                ):
                    break
                assert c.metrics is not None and r.metrics is not None
                values.append(
                    Fraction(c.metrics[profile.primary]) - Fraction(r.metrics[profile.primary])
                )
            if len(values) == len(case["repeats"]):
                diffs[case["case_id"]] = sum(values, Fraction()) / len(values)
        weights = {c["case_id"]: decode_rational(c["weight"]) for c in cases}
        available = bool(cases) and len(diffs) == len(cases) and incomplete is None
        skipped = scope["name"] == "causal" and not declared
        point = sum((weights[c] * diffs[c] for c in diffs), Fraction()) if available else None
        units = scope.get("units", [dict(cases=[c], weight=q(weights[c])) for c in weights])
        result = dict(
            name=scope["name"],
            rule=scope["rule"],
            status="skipped" if skipped else "available" if available else "unavailable",
            declared_count=len(declared),
            point=None if point is None else q(point),
            unit_count=len(units),
            mass=q(mass),
            excluded_mass=q((1 if declared else 0) - mass),
            unavailable=None if available or skipped else incomplete or "empty_support",
        )
        if "fallback" in scope:
            result["fallback"] = scope["fallback"]
        if measured:
            result["threshold"] = causal_threshold
            if calibration_unavailable and not skipped:
                available = False
                result.update(
                    status="unavailable", unavailable=incomplete or calibration_unavailable
                )
        passed = True
        if scope["rule"] == "bound":
            result.update(confidence=scope["confidence"], enclosure=None)
            if available:
                masses = [decode_rational(u["weight"]) for u in units]
                z = [
                    sum((weights[c] * diffs[c] for c in u["cases"]), Fraction()) / a
                    for u, a in zip(units, masses, strict=True)
                ]
                confidence = decode_rational(scope["confidence"])
                lower = lower_enclosure(z, masses, confidence)
                result["enclosure"] = dict(
                    lower=q(lower), upper=q(min(Fraction(1), lower + Fraction(1, 2**63))), steps=64
                )
                passed = passes_bound(
                    z,
                    masses,
                    decode_rational(scope["threshold"]),
                    confidence,
                    strict=scope["strict"],
                )
        elif scope["rule"] == "bootstrap":
            assert bootstrap is not None
            estimate = bootstrap.scopes.get(scope["name"])
            result.update(confidence=scope["confidence"], lower=None, exact=None, pairing=None)
            if available and estimate is not None:
                result["pairing"] = estimate.pairing
                if estimate.unavailable:
                    available = False
                    result.update(status="unavailable", unavailable=estimate.unavailable)
                else:
                    assert estimate.lower is not None and estimate.point == point
                    result.update(lower=q(estimate.lower), exact=estimate.exact)
                    threshold = decode_rational(scope["threshold"])
                    passed = (
                        estimate.lower > threshold
                        if scope["strict"]
                        else estimate.lower >= threshold
                    )
        elif scope["rule"] == "point" and available:
            threshold = decode_rational(scope["threshold"])
            assert point is not None
            passed = point > threshold if scope["strict"] else point >= threshold
        if reason == "passed" and not skipped:
            if not available:
                outcome, reason, deciding = (
                    "comparison_unavailable",
                    result["unavailable"],
                    scope["name"],
                )
            elif not passed:
                outcome, reason, deciding = (
                    "comparison_not_passed",
                    "threshold_not_passed",
                    scope["name"],
                )
        results.append(result)
    slots = {
        side: {(s["case_id"], s["repeat"]): s for s in schedule}
        for side, schedule in schedules.items()
    }
    retained = [
        (c["case_id"], repeat)
        for scope in body["scopes"]
        for c in scope["cases"]
        for repeat in c["repeats"]
    ]
    diagnostics = []
    observed = body["execution_noise"]["kind"] == "observed"
    for case in dict.fromkeys(s["case_id"] for s in schedules["candidate"]):
        rows = [r for r in candidate if r.case_id == case]
        missing = len(rows) != sum(s["case_id"] == case for s in schedules["candidate"]) or any(
            not admissible(r, profile.primary) for r in rows
        )
        why = (
            "not_requested"
            if not observed
            else "incomplete"
            if missing
            else "insufficient_repeats"
            if len(rows) < 2
            else None
        )
        variation = None
        if why is None:
            values = [Fraction(r.metrics[profile.primary]) for r in rows if r.metrics is not None]
            mean = sum(values, Fraction()) / len(values)
            variation = q(sum(((v - mean) ** 2 for v in values), Fraction()) / (len(values) - 1))
        diagnostics.append(dict(case_id=case, variation=variation, unavailable=why))
    payload = dict(
        format="comparison-data/1",
        kind="result",
        binding_digest=profile.digest,
        candidate=dict(candidate_source or source_summary(body["candidate"])),
        reference=dict(reference_source or source_summary(body["reference"])),
        mode=body["mode"],
        scopes=results,
        coverage=coverage,
        retained_pairs=[{side: slots[side][key] for side in slots} for key in retained],
        excluded_reference=body["excluded_reference"],
        repeat_diagnostics=diagnostics,
        variation="not_requested"
        if not observed
        else "unavailable"
        if any(d["unavailable"] for d in diagnostics)
        else "observed",
        deciding_scope=deciding,
        outcome=outcome,
        reason=reason,
    )
    caveats = set()
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap.metadata()
        caveats.update(
            {"intervals_empirical", *["exact_contrast" for s in results if s.get("exact") is True]}
        )
    if calibration is not None:
        caveats.add(
            "noise_allowance_empirical"
            if decode_rational(calibration["allowance"]) > 0
            else "noise_not_observed"
        )
        if body["reference_kind"] == "fresh":
            payload["calibration"] = calibration
    if caveats:
        payload["caveats"] = sorted(caveats)
    if scope_name is None:
        validate_comparison_data(
            payload,
            binding=body,
            policy=profile.policy
            if profile.policy is not None and profile.policy.bound_fallback is not None
            else None,
        )
    return ComparisonResult(canonical_json(payload))


@dataclass(frozen=True)
class PreflightResult:
    """Frozen ceilings, never observed Candidate evidence.

    Missing payload means disabled/provisional work and cannot authorize a skip.
    The profile digest includes reference values as well as weights.
    """

    profile_digest: str
    reference_digest: str
    basis: str
    encoded: bytes | None
    impossible: bool

    def to_payload(self) -> dict[str, Any] | None:
        return None if self.encoded is None else json.loads(self.encoded)


def preflight(profile: ComparisonProfile, reference: Sequence[SampleRow] | None) -> PreflightResult:
    """Prove only score-range impossibility against the exact frozen reference."""
    body = profile.to_payload()
    basis = body["reference_kind"]
    reference_digest = hashlib.sha256(
        canonical_json(
            {
                "reference": body["reference"],
                "kind": basis,
                "scopes": body["scopes"],
                "excluded": body["excluded_reference"],
            }
        )
    ).hexdigest()
    if body["mode"] == "statistical" and any(s["name"] != "global" for s in body["scopes"]):
        return PreflightResult(profile.digest, reference_digest, basis, None, False)
    if basis == "stored" and reference is not None:
        indexed = _index(reference, body["reference"]["identity"]["schedule"])
        actual_excluded = {
            key for key, row in indexed.items() if row.exhausted and row.disposition != "score"
        }
        if actual_excluded != {(s["case_id"], s["repeat"]) for s in body["excluded_reference"]}:
            raise ValueError("reference exclusions differ from frozen profile")
        for scope in body["scopes"]:
            for case in scope["cases"]:
                actual = []
                for repeat in case["repeats"]:
                    row = indexed.get((case["case_id"], repeat))
                    if row is None or not admissible(row, profile.primary):
                        raise ValueError("reference row is missing or inadmissible")
                    assert row.metrics is not None
                    actual.append(q(Fraction(row.metrics[profile.primary])))
                if actual != case["reference_values"]:
                    raise ValueError("reference values differ from frozen profile")
    scopes = [s for s in body["scopes"] if s["rule"] != "off"]
    if not scopes or any(not s["cases"] and s["name"] != "causal" for s in scopes):
        return PreflightResult(profile.digest, reference_digest, basis, None, False)
    results = []
    for scope in scopes:
        if scope["name"] == "causal" and not scope["declared_cases"]:
            results.append(dict(name="causal", status="skipped"))
            continue
        if not scope["cases"]:
            return PreflightResult(profile.digest, reference_digest, basis, None, False)
        if scope.get("tolerance") == "measured_noise" and scope["threshold"] is None:
            results.append(dict(name="causal", status="uncalibrated"))
            continue
        weights = {c["case_id"]: decode_rational(c["weight"]) for c in scope["cases"]}
        ceilings = {
            c["case_id"]: 1
            - sum(map(decode_rational, c["reference_values"]), Fraction()) / len(c["repeats"])
            if basis == "stored"
            else Fraction(1)
            for c in scope["cases"]
        }
        threshold = decode_rational(scope["threshold"])
        result = dict(
            name=scope["name"],
            status="calculated",
            ceilings=[dict(case_id=case, value=q(value)) for case, value in ceilings.items()],
        )
        if scope["rule"] == "point":
            maximum = sum((weights[c] * ceilings[c] for c in weights), Fraction())
            result.update(
                delta_max=q(maximum),
                passes=maximum > threshold if scope["strict"] else maximum >= threshold,
            )
        else:
            masses = [decode_rational(u["weight"]) for u in scope["units"]]
            values = [
                sum((weights[c] * ceilings[c] for c in u["cases"]), Fraction()) / mass
                for u, mass in zip(scope["units"], masses, strict=True)
            ]
            maximum_e = exact_e(values, masses, threshold) if -1 < threshold < 1 else None
            # Nonnegative ceilings make the eta=1 lower-support limit infinite.
            evidence = (
                dict(kind="positive_infinity")
                if threshold == -1
                else (dict(kind="finite", value=q(maximum_e)) if maximum_e is not None else None)
            )
            result.update(
                e_max=evidence,
                c_max=q(1 - 1 / maximum_e) if maximum_e is not None and maximum_e > 1 else None,
                passes=passes_bound(
                    values,
                    masses,
                    threshold,
                    decode_rational(scope["confidence"]),
                    strict=scope["strict"],
                ),
            )
        results.append(result)
    body.pop("candidate", None)
    body.update(
        kind="preflight",
        phase="early_global" if [s["name"] for s in scopes] == ["global"] else "attempt",
        scopes=scopes,
        results=results,
    )
    validate_comparison_data(body, policy=profile.policy)
    return PreflightResult(
        profile.digest,
        reference_digest,
        basis,
        canonical_json(body),
        any(r.get("passes") is False for r in results),
    )


def full_primary_score(
    rows: Sequence[SampleRow],
    schedule: Sequence[Mapping[str, Any]],
    *,
    corpus: Sequence[Mapping[str, Any]],
    primary: str,
) -> dict[str, str] | None:
    """Exact full train score, independent of stored-reference exclusions."""
    indexed = _index(rows, schedule)
    if len(indexed) != len(schedule) or not all(admissible(r, primary) for r in rows):
        return None
    groups = {str(c["case_id"]): str(c["group_id"]) for c in corpus}
    weights = count_weights(list(groups), groups)
    point = Fraction()
    for case, weight in weights.items():
        values = [
            Fraction(r.metrics[primary])
            for r in rows
            if r.case_id == case and r.metrics is not None
        ]
        if not values:
            return None
        point += weight * sum(values, Fraction()) / len(values)
    return q(point)


def ranking_payload(entries: Sequence[Mapping[str, Any]], round_stop: str) -> dict[str, Any]:
    eligible = [e for e in entries if e["accepted"] and e["point"] is not None]
    winner = min(
        eligible,
        key=(lambda e: (e["attempt_index"],))
        if round_stop == "first_accepted"
        else (lambda e: (-decode_rational(e["point"]), e["attempt_index"])),
        default=None,
    )
    body = dict(
        format="comparison-data/1",
        kind="ranking",
        round_stop=round_stop,
        entries=list(entries),
        proposed_attempt=None if winner is None else winner["attempt_id"],
    )
    validate_comparison_data(body)
    return body
