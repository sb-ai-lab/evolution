"""Transport-neutral attestation verifier used by every promotion-capable adapter.

ADR-0005: the target attests by computing digests over the bytes it loaded, and
core compares; the verifier here is the single place that comparison happens, so
the same rules apply whether the adapter is `HttpSessionAdapter`, an in-process
fake used by conformance, or any future custom adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from evolution.tree.artifacts.artifacts import ArtifactRef, ExpectedAttestation
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    AttestationError,
    SemanticIdentity,
    TargetAdapter,
    TargetIdentity,
)
from evolution.tree.execution.wire_v2 import REQUIRED_ATTESTS

_REQUIRED = frozenset(REQUIRED_ATTESTS)


class ComponentsChanged(AttestationError):
    """An instance declared a different component map from its
    reference inside the reference epoch (an instance that declares none is
    not compared). An attestation refusal in every respect but the suspension
    reason it decides (`target_components_changed`)."""

    def __init__(self, measurement_id: str, reference: str, declared: str) -> None:
        super().__init__(
            f"{measurement_id}: component map {declared} is not the reference {reference}",
            "attestation_mismatch",
        )
        self.measurement_id = measurement_id
        self.reference = reference
        self.declared = declared


def verify_attestation(identity: TargetIdentity, expected: ExpectedAttestation) -> None:
    """Require the required attested set and both S2-owned digests to match.

    `identity.attested` must equal exactly `frozenset(REQUIRED_ATTESTS)` (an extra
    or a missing field is `attestation_missing`); then `identity.instance.
    loaded_candidate_digest` must equal `expected.candidate_digest` and
    `identity.semantic.dependency_digest` must equal `expected.dependency_digest`
    (either mismatch is `attestation_mismatch`).
    """
    if identity.attested != _REQUIRED:
        raise AttestationError(
            "target did not attest exactly the required identity fields",
            "attestation_missing",
        )
    if (
        identity.instance.loaded_candidate_digest != expected.candidate_digest
        or identity.semantic.dependency_digest != expected.dependency_digest
    ):
        raise AttestationError(
            "attested digests do not match the expected artifacts",
            "attestation_mismatch",
        )


@dataclass(frozen=True)
class TargetPreflight:
    """The declared identity and capabilities a promotion-capable adapter froze
    before its first materialize call; `epoch` is the target's `boot_id`."""

    semantic: SemanticIdentity
    capabilities: AdapterCapabilities
    epoch: str

    def __post_init__(self) -> None:
        if not isinstance(self.epoch, str) or not self.epoch.strip():
            raise ValueError("epoch must not be blank")


class PromotionCapableAdapter(TargetAdapter[ArtifactRef], Protocol):
    """A `TargetAdapter` over `ArtifactRef` that also attests before materializing.

    An adapter without `preflight` is not promotion-capable: the complete
    declared `SemanticIdentity` must exist before the first journal write, so the
    generic conformance suite fails such an adapter at its first check.
    """

    async def preflight(self, expected: ExpectedAttestation) -> TargetPreflight: ...
