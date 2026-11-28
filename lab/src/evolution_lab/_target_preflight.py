"""Shared public-core target preflight below client and Owner admission."""
from __future__ import annotations

from uuid import uuid4

from evolution.tree import ArtifactUnsafeError
from evolution.tree.artifacts.artifacts import ExpectedAttestation
from evolution.tree.campaign_policy import PolicyError
from evolution.tree.execution.interaction import RolloutInfrastructureError

from evolution_lab.provenance import InputChanged


_UNAVAILABLE_CODES = frozenset(
    {"transport_error", "not_ready", "callee_timeout", "internal"}
)
_MISMATCH_CODES = frozenset({
    "pin_mismatch", "strategy_unsupported", "attestation_missing",
    "attestation_mismatch", "limits_incompatible", "policy_mismatch",
    "invalid_response", "response_too_large", "invalid_request",
    "request_conflict", "request_expired", "root_not_allowed",
    "unknown_instance", "unknown_session", "instance_conflict",
    "instances_exhausted", "target_epoch_changed", "not_preflighted",
})


def preflight_failure_code(error: BaseException) -> str:
    """Map a preflight exception to the closed Lab check failure code."""
    if isinstance(error, InputChanged):
        return "inputs_changed"
    if isinstance(error, OSError):
        return "io_error"
    if isinstance(error, (ArtifactUnsafeError, PolicyError)):
        return "artifacts_invalid"
    if isinstance(error, RolloutInfrastructureError):
        if error.code in _UNAVAILABLE_CODES:
            return "target_unavailable"
        if error.code in _MISMATCH_CODES:
            return "target_mismatch"
    return "lab_error"


async def run_target_preflight(manifest, *, scan, adapter_type) -> None:
    """Scan saved artifacts and run the public adapter preflight once."""
    seed = scan(manifest.seed_root, manifest.policy)
    adapter = adapter_type(manifest.target, manifest.policy, run_id=str(uuid4()))
    await adapter.preflight(
        ExpectedAttestation(seed.candidate_digest, seed.dependency_digest)
    )
