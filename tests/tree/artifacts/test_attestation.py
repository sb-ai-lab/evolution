"""Behavioral contracts for the transport-neutral attestation verifier."""

from __future__ import annotations

import pytest

from evolution.tree.artifacts.artifacts import ExpectedAttestation
from evolution.tree.artifacts.attestation import TargetPreflight, verify_attestation
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    AttestationError,
    InstanceProvenance,
    SemanticIdentity,
    TargetIdentity,
)

SHA_A, SHA_D = "a" * 64, "d" * 64


def identity(loaded=SHA_A, dep=SHA_D, attested=("loaded_candidate_digest", "dependency_digest")):
    return TargetIdentity(
        SemanticIdentity("c", dep, "s", "exact", "2", "r"),
        InstanceProvenance("boot", loaded),
        frozenset(attested),
    )


def test_verify_attestation_passes_and_fails_by_code():
    expected = ExpectedAttestation(SHA_A, SHA_D)
    verify_attestation(identity(), expected)
    with pytest.raises(AttestationError) as info:
        verify_attestation(identity(attested=("loaded_candidate_digest",)), expected)
    assert info.value.code == "attestation_missing"
    with pytest.raises(AttestationError) as info:
        verify_attestation(identity(loaded="b" * 64), expected)
    assert info.value.code == "attestation_mismatch"
    with pytest.raises(AttestationError):
        verify_attestation(identity(dep="e" * 64), expected)


def test_capabilities_gain_max_instances_additively():
    caps = AdapterCapabilities("exact", 1, True, frozenset({"artifact_root_injection"}))
    assert caps.max_instances == 1
    with pytest.raises(ValueError):
        AdapterCapabilities("exact", 1, True, frozenset({"artifact_root_injection"}), 0)
    assert TargetPreflight(identity().semantic, caps, "boot").epoch == "boot"
