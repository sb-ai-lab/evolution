"""Explicit family dialects and ordinal final-measurement identities."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from typing import Any

import pytest

from evolution.tree.campaign_identity import final_measurement_id_for
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.execution.journal_identity import IdentityError


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        (
            14,
            {
                "family_version": 14,
                "measurement_bound": "final_measurement_bound",
                "measurement_completed": "final_measurement_completed",
                "gate_pass_outcome": "finalist",
                "gate_pass_decision": "finalist",
                "contrast_key": "accepted",
                "ranking_key": "accepted",
                "incomplete_reason": "final_incomplete",
                "suspension_evidence_kind": "final_rows",
                "stage_barrier": "final_stage",
                "registry_role": "final",
                "selection_retained_reason": "no_accepted_finalist",
                "interim_focus_rejection_reason": "focus_below_margin",
                "final_focus_rejection_reason": "focus_below_margin",
                "report_key": "final",
            },
        )
    ],
)
def test_explicit_family_selects_its_exact_dialect(family: int, expected: dict[str, Any]) -> None:
    assert asdict(campaign_vocabulary_for(family)) == expected


@pytest.mark.parametrize("family", [0, 1, 12, 16, -1, True, False, 13.0, "14", None, "final"])
def test_unknown_family_is_rejected_without_guessing_from_spelling(family: Any) -> None:
    with pytest.raises(ValueError, match="unsupported_campaign_vocabulary_family"):
        campaign_vocabulary_for(family)


@pytest.mark.parametrize("family", [14])
def test_selected_vocabulary_cannot_be_mutated(family: int) -> None:
    vocabulary = campaign_vocabulary_for(family)
    with pytest.raises(FrozenInstanceError):
        vocabulary.contrast_key = "wrong"
    assert campaign_vocabulary_for(family) is vocabulary


def test_global_acceptance_key_is_separate_from_candidate_gate_pass() -> None:
    vocabulary = campaign_vocabulary_for(14)
    assert vocabulary.gate_pass_outcome == vocabulary.gate_pass_decision == "finalist"
    assert vocabulary.contrast_key == vocabulary.ranking_key == "accepted"


@pytest.mark.parametrize(
    ("round_number", "ordinal", "index", "expected"),
    [
        (1, 1, None, "m-r01-final-01-champion"),
        (1, 2, None, "m-r01-final-02-champion"),
        (1, 1, 1, "m-r01-final-01-a01"),
        (1, 1, 2, "m-r01-final-01-a02"),
        (1, 2, 1, "m-r01-final-02-a01"),
        (1, 2, 2, "m-r01-final-02-a02"),
        (12, 34, 56, "m-r12-final-34-a56"),
        (123, 123, 123, "m-r123-final-123-a123"),
    ],
)
def test_final_measurement_ids_bind_round_ordinal_and_member(
    round_number: int, ordinal: int, index: int | None, expected: str
) -> None:
    assert final_measurement_id_for(round_number, ordinal, index) == expected


@pytest.mark.parametrize("field", ["round_number", "ordinal", "index"])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.0, "1"])
def test_final_measurement_ids_refuse_invalid_indices(field: str, invalid: Any) -> None:
    arguments = {"round_number": 1, "ordinal": 1, "index": 1}
    arguments[field] = invalid
    with pytest.raises(IdentityError, match=f"invalid_final_measurement_{field}"):
        final_measurement_id_for(**arguments)


@pytest.mark.parametrize("field", ["round_number", "ordinal"])
def test_only_a_champion_member_index_may_be_null(field: str) -> None:
    arguments = {"round_number": 1, "ordinal": 1, "index": 1}
    arguments[field] = None
    with pytest.raises(IdentityError, match=f"invalid_final_measurement_{field}"):
        final_measurement_id_for(**arguments)


@pytest.mark.parametrize(
    "family,reason,kind",
    [(14, "final_incomplete", "final_rows")],
)
def test_gap_evidence_uses_family_dialect_and_preserves_256_row_bound(family, reason, kind):
    import hashlib
    import json

    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.confidence_cohort import cohort_owner, cohort_rows_evidence

    rows = [
        {
            "measurement_id": "m-final",
            "case_id": f"c{i:03d}",
            "sample_id": "01",
            "retry_ordinal": i % 2,
            "owner": "rollout" if i < 299 else "scorer",
        }
        for i in range(300)
    ]
    evidence = cohort_rows_evidence(rows, family_version=family)
    expected_digest = hashlib.sha256(
        json.dumps(
            sorted(
                [
                    [r["measurement_id"], r["case_id"], "01", r["retry_ordinal"], r["owner"]]
                    for r in rows
                ]
            ),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert (
        evidence["evidence_kind"],
        evidence["count"],
        evidence["truncated"],
        evidence["rows_digest"],
    ) == (kind, 300, True, expected_digest)
    assert evidence["rows"] == rows[:256]
    assert cohort_owner(rows) == "scorer"
    campaign_family_for(family).validate_payload(
        "campaign_suspended", {"reason": reason, "owner": "scorer", "evidence": evidence}
    )


@pytest.mark.parametrize(
    "family,kind,reason",
    [(14, "final_rows", "final_incomplete")],
)
def test_cli_suspension_detail_uses_selected_dialect(family, kind, reason):
    from evolution.tree.cli import _suspension_detail

    text = _suspension_detail(
        {
            "evidence_kind": kind,
            "count": 1,
            "rows": [
                {"measurement_id": "m-final", "case_id": "c1", "sample_id": "01", "owner": "target"}
            ],
        },
        reason=reason,
    )
    noun = "cohort" if family == 13 else "final"
    title = "cohort" if family == 13 else "measurement"
    assert text == (
        f"the final {title} has pairs left without a score: an infrastructure-owned one "
        "on a finalist or any on the Champion; repair the cause and run the same command to resume; "
        f"1 unfinished {noun} pair, first m-final c1/01 (target)"
    )
