"""Contracts for Evolution v2 sample dispositions and public reason identifiers."""

from __future__ import annotations

from typing import Any

import pytest

from evolution.tree.execution.sample_outcomes import (
    SAMPLE_REASON_REGISTRY_V1,
    SAMPLE_REASON_SCHEMA_VERSION,
    SampleDisposition,
    classify_sample_outcome,
    disposition_for,
    sample_reason_registry_payload,
)

EXPECTED_DISPOSITIONS = {
    "completed": ("score", None, None),
    "turn_budget": ("score", "candidate", "turn_budget_exhausted"),
    "time_budget": (
        "indeterminate",
        "rollout",
        "rollout_time_budget_exhausted",
    ),
    "target_failed": ("indeterminate", "target", "target_unstable"),
    "candidate_failed": ("score", "candidate", "candidate_turn_failed"),
    "environment_failed": (
        "indeterminate",
        "environment",
        "environment_unstable",
    ),
    "scorer_failed": ("indeterminate", "scorer", "scorer_unstable"),
    "interrupted": ("indeterminate", "rollout", "rollout_interrupted"),
    "cancelled": ("exclude", "rollout", "rollout_cancelled"),
    "infrastructure_failed": (
        "exclude",
        "infrastructure",
        "infrastructure_failed",
    ),
}

EXPECTED_REASON_PAYLOAD = {
    "schema_version": 2,
    "reasons": [
        {
            "id": "candidate_turn_failed",
            "outcome": "candidate_failed",
            "disposition": "score",
            "owner": "candidate",
        },
        {
            "id": "environment_unstable",
            "outcome": "environment_failed",
            "disposition": "indeterminate",
            "owner": "environment",
        },
        {
            "id": "infrastructure_failed",
            "outcome": "infrastructure_failed",
            "disposition": "exclude",
            "owner": "infrastructure",
        },
        {
            "id": "rollout_cancelled",
            "outcome": "cancelled",
            "disposition": "exclude",
            "owner": "rollout",
        },
        {
            "id": "rollout_interrupted",
            "outcome": "interrupted",
            "disposition": "indeterminate",
            "owner": "rollout",
        },
        {
            "id": "rollout_time_budget_exhausted",
            "outcome": "time_budget",
            "disposition": "indeterminate",
            "owner": "rollout",
        },
        {
            "id": "scorer_unstable",
            "outcome": "scorer_failed",
            "disposition": "indeterminate",
            "owner": "scorer",
        },
        {
            "id": "target_unstable",
            "outcome": "target_failed",
            "disposition": "indeterminate",
            "owner": "target",
        },
        {
            "id": "turn_budget_exhausted",
            "outcome": "turn_budget",
            "disposition": "score",
            "owner": "candidate",
        },
    ],
}


@pytest.mark.parametrize(("outcome", "expected"), EXPECTED_DISPOSITIONS.items())
def test_disposition_table_is_exact(outcome: Any, expected: tuple[object, ...]) -> None:
    """A wrong kind, owner, or reason must break the finite v2 outcome contract."""

    item = disposition_for(outcome)

    assert (item.kind, item.owner, item.reason) == expected


def test_disposition_rejects_an_unregistered_tuple() -> None:
    """Callers cannot construct a valid-looking but unregistered state tuple."""

    with pytest.raises(ValueError, match="registered outcome tuple"):
        SampleDisposition(
            "time_budget",
            "score",
            "candidate",
            "turn_budget_exhausted",
        )


def test_disposition_rejects_an_unknown_outcome() -> None:
    """Unknown outcomes fail closed instead of inheriting a default disposition."""

    with pytest.raises(ValueError, match="sample outcome is invalid"):
        disposition_for("unknown")


def test_reason_registry_is_immutable_and_complete() -> None:
    """S1-min receives every stable non-null reason from an immutable registry."""

    assert SAMPLE_REASON_SCHEMA_VERSION == 2
    assert tuple(SAMPLE_REASON_REGISTRY_V1) == tuple(
        entry["id"] for entry in EXPECTED_REASON_PAYLOAD["reasons"]
    )
    with pytest.raises(TypeError):
        SAMPLE_REASON_REGISTRY_V1["unknown"] = disposition_for("completed")


def test_reason_registry_payload_is_exact_and_detached() -> None:
    """Published payloads are deterministic copies without mutable shared state."""

    payload = sample_reason_registry_payload()

    assert payload == EXPECTED_REASON_PAYLOAD
    payload["reasons"].clear()
    assert sample_reason_registry_payload() == EXPECTED_REASON_PAYLOAD


def test_candidate_failed_scores_against_the_candidate():
    disposition = disposition_for("candidate_failed")
    assert disposition.kind == "score"
    assert disposition.owner == "candidate"
    assert disposition.reason == "candidate_turn_failed"
    assert classify_sample_outcome("candidate_failed") == "score"


def test_candidate_failed_matches_turn_budget_shape():
    # The registry's promise: the new terminal is shaped exactly like the one
    # scoreable Candidate-owned terminal that already existed (ADR-0001).
    new = disposition_for("candidate_failed")
    old = disposition_for("turn_budget")
    assert (new.kind, new.owner) == (old.kind, old.owner)


def test_reason_registry_version_is_two_and_carries_the_new_reason():
    payload = sample_reason_registry_payload()
    assert payload["schema_version"] == 2
    assert SAMPLE_REASON_SCHEMA_VERSION == 2
    ids = [row["id"] for row in payload["reasons"]]
    assert "candidate_turn_failed" in ids
    assert ids == sorted(ids)
    row = next(r for r in payload["reasons"] if r["id"] == "candidate_turn_failed")
    assert row == {
        "id": "candidate_turn_failed",
        "outcome": "candidate_failed",
        "disposition": "score",
        "owner": "candidate",
    }
