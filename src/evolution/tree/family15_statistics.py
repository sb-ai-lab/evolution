"""Exact family-15 adapters over the unchanged family-14 repeat bootstrap.

The frozen profile owns weights and retained repeats. Cases stay in corpus
order, and all local projections share one kernel stream and pairing proof.
No published binary64 endpoint is read back into a family-15 decision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from evolution.tree.confidence_statistics import (
    METHOD_VERSION,
    CaseEvidence,
    NoiseCase,
    Pairing,
    derive_seed,
    joint_replicates,
    noise_calibration,
    one_sided_lower,
    pairing_of,
    point_contrast,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.family15_schema import calibration_payload, decode_rational, encode_rational
from evolution.tree.final_statistics import exact_scope
from evolution.tree.statistics import ScoreSample

if TYPE_CHECKING:
    from evolution.tree.case_comparison import ComparisonProfile


@dataclass(frozen=True)
class Calibration:
    """Unknown q has neither a numeric allowance nor a fictitious calibration record."""

    allowance: Fraction | None
    degenerate: bool
    unavailable: str | None
    payload: Mapping[str, Any] | None


def calibration_seed_binding(
    policy_digest: str,
    reference_measurement_id: str,
    parent_prefix: Mapping[str, Any],
    causal: Sequence[tuple[str, Fraction]],
) -> str:
    """R7's non-circular stored-Parent subdigest; weights use rational-hex/1.

    Case order is the frozen causal profile's order, also used for the kernel.
    The complete prefix (locator and content digest) binds the Parent evidence.
    """
    return hashlib.sha256(
        canonical_json(
            dict(
                policy=policy_digest,
                reference=reference_measurement_id,
                prefix=dict(parent_prefix),
                causal=[[case, encode_rational(weight)] for case, weight in causal],
            )
        )
    ).hexdigest()


def calibrate(
    parent: Mapping[str, Sequence[ScoreSample]],
    causal: Sequence[tuple[str, Fraction]],
    *,
    campaign_seed: int,
    seed_binding: str,
    prefix: Mapping[str, Any],
    source: str,
    measurement_id: str,
) -> Calibration:
    """Two virtual Parent measurements at the retained sizes, never Candidate rows.

    The caller filters to the frozen retained repeats. No calibration is requested
    for empty C; missing or one-repeat Parent support has no publishable output.
    """
    if not causal:
        raise ValueError("empty causal scope has no calibration")
    weights = dict(causal)
    if len(weights) != len(causal) or sum(weights.values()) != 1:
        raise ValueError("calibration requires unique cases with normalized weights")
    seed = derive_seed(
        campaign_seed=campaign_seed,
        binding=seed_binding,
        scope="noise",
        prefixes=[prefix["locator"]],
        look=None,
        purpose="noise",
    )
    if any(len(parent.get(case, ())) < 2 for case in weights):
        return Calibration(None, False, "insufficient_calibration", None)
    result = noise_calibration(
        [
            NoiseCase(case, tuple(parent[case]), len(parent[case]), len(parent[case]))
            for case, _weight in causal
        ],
        weights,
        seed=seed,
    )
    assert result.allowance is not None and result.unavailable is None
    payload = calibration_payload(
        source=source,
        measurement_id=measurement_id,
        prefix=prefix,
        retained=len(causal),
        seed_input=seed_binding,
        allowance=result.allowance,
        degenerate=result.degenerate,
    )
    return Calibration(result.allowance, result.degenerate, None, MappingProxyType(payload))


@dataclass(frozen=True)
class BootstrapScope:
    lower: Fraction | None
    point: Fraction | None
    exact: bool | None
    pairing: Pairing | None
    unavailable: str | None


@dataclass(frozen=True)
class BootstrapResult:
    scopes: Mapping[str, BootstrapScope]
    seed: int
    campaign_seed: int
    binding_digest: str
    prefixes: tuple[str, ...]
    look: int
    caveats: tuple[str, ...]
    scope_name: str = "joint"

    def metadata(self) -> dict[str, Any]:
        """Every input to derive_seed, plus the derived 128-bit integer."""
        return dict(
            method=METHOD_VERSION,
            campaign_seed=self.campaign_seed,
            binding=self.binding_digest,
            scope=self.scope_name,
            prefixes=list(self.prefixes),
            look=self.look,
            purpose="gate",
            seed=self.seed,
        )


def bootstrap_scopes(
    profile: ComparisonProfile,
    candidate: Mapping[str, Sequence[ScoreSample]],
    parent: Mapping[str, Sequence[ScoreSample]],
    *,
    campaign_seed: int,
    binding_digest: str,
    prefixes: Sequence[str],
    scope_name: str | None = None,
) -> BootstrapResult:
    """One fixed R look; insufficiency is local, exactness follows family 14.

    Empty/excluded scopes have no estimate here: their owner distinguishes an
    empty declared causal scope (skipped) from empty retained support. The full
    bound schedule, not the surviving repeats, fixes R in the seed.
    """
    body = profile.to_payload()
    if body["mode"] != "statistical" or binding_digest != profile.digest:
        raise ValueError("bootstrap requires its statistical comparison binding")
    if scope_name not in {None, "focus", "causal"}:
        raise ValueError("unknown bootstrap stage scope")
    look = max(s["repeat"] for s in body["reference"]["identity"]["schedule"])
    seed = derive_seed(
        campaign_seed=campaign_seed,
        binding=binding_digest,
        scope=scope_name or "joint",
        prefixes=prefixes,
        look=look,
        purpose="gate",
    )
    scopes = [
        s
        for s in body["scopes"]
        if s["rule"] == "bootstrap"
        and s["cases"]
        and (scope_name is None or s["name"] == scope_name)
    ]
    if not scopes:
        # Global point/off and wholly excluded local scopes have no bootstrap
        # quantity. Keep recorded seed inputs without calling a local kernel.
        return BootstrapResult(
            MappingProxyType({}),
            seed,
            campaign_seed,
            binding_digest,
            tuple(prefixes),
            look,
            ("intervals_empirical",),
            scope_name or "joint",
        )
    retained = {c["case_id"]: set(map(str, c["repeats"])) for s in scopes for c in s["cases"]}
    evidence = []
    for case, _group in profile.groups:
        if case not in retained:
            continue
        sides = [
            tuple(s for s in source.get(case, ()) if s.sample_id in retained[case])
            for source in (candidate, parent)
        ]
        if all(sides):
            evidence.append(CaseEvidence(case, sides[0], sides[1]))
    pairing = pairing_of(evidence)
    by_id = {c.case_id: c for c in evidence}
    built = {}
    available = {}
    for scope in scopes:
        name = scope["name"]
        weights = {c["case_id"]: decode_rational(c["weight"]) for c in scope["cases"]}
        point = point_contrast(evidence, weights) if weights.keys() <= by_id.keys() else None
        if any(
            case not in by_id or min(len(by_id[case].candidate), len(by_id[case].parent)) < 2
            for case in weights
        ):
            built[name] = BootstrapScope(None, point, None, pairing, "insufficient_repeats")
            continue
        available[name] = weights
    if available:
        replicates = joint_replicates(evidence, available, pairing=pairing, seed=seed)
        for scope in scopes:
            name = scope["name"]
            if name not in available:
                continue
            weights = available[name]
            point = point_contrast(evidence, weights)
            exact = exact_scope(evidence, weights, pairing)
            lower = (
                point
                if exact
                else one_sided_lower(replicates.draws(name), decode_rational(scope["confidence"]))
            )
            built[name] = BootstrapScope(lower, point, exact, pairing, None)
    caveats = {"intervals_empirical"}
    if any(scope.exact for scope in built.values()):
        caveats.add("exact_contrast")
    return BootstrapResult(
        MappingProxyType(built),
        seed,
        campaign_seed,
        binding_digest,
        tuple(prefixes),
        look,
        tuple(sorted(caveats)),
        scope_name or "joint",
    )
