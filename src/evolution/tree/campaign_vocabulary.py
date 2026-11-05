"""Immutable durable spellings selected explicitly by campaign family.

Family 13 retains its confidence/cohort dialect; family 14 uses final
measurements and reserves global acceptance for the full-corpus comparison.
Earlier families keep their existing paths. This table selects names only:
it never infers a family from, or rewrites, a persisted record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class CampaignVocabulary:
    """The family-dependent names used by shared confidence/final handlers."""

    family_version: int
    measurement_bound: str
    measurement_completed: str
    gate_pass_outcome: str
    gate_pass_decision: str
    contrast_key: str
    ranking_key: str
    incomplete_reason: str
    suspension_evidence_kind: str
    stage_barrier: str
    registry_role: str
    #: No finalist passed the global guards; distinct from `no_finalists`.
    selection_retained_reason: str
    interim_focus_rejection_reason: str
    final_focus_rejection_reason: str
    report_key: str


_VOCABULARIES: Final[Mapping[int, CampaignVocabulary]] = MappingProxyType(
    {
        13: CampaignVocabulary(
            family_version=13,
            measurement_bound="cohort_measurement_bound",
            measurement_completed="cohort_measurement_completed",
            gate_pass_outcome="accepted",
            gate_pass_decision="accepted",
            contrast_key="eligible",
            ranking_key="eligible",
            incomplete_reason="cohort_incomplete",
            suspension_evidence_kind="cohort_rows",
            stage_barrier="cohort_stage",
            registry_role="cohort",
            selection_retained_reason="no_eligible_finalist",
            interim_focus_rejection_reason="focus_upper_nonpositive",
            final_focus_rejection_reason="focus_lower_not_positive",
            report_key="cohort",
        ),
        14: CampaignVocabulary(
            family_version=14,
            measurement_bound="final_measurement_bound",
            measurement_completed="final_measurement_completed",
            gate_pass_outcome="finalist",
            gate_pass_decision="finalist",
            contrast_key="accepted",
            ranking_key="accepted",
            incomplete_reason="final_incomplete",
            suspension_evidence_kind="final_rows",
            stage_barrier="final_stage",
            registry_role="final",
            selection_retained_reason="no_accepted_finalist",
            interim_focus_rejection_reason="focus_below_margin",
            final_focus_rejection_reason="focus_below_margin",
            report_key="final",
        ),
    }
)


def campaign_vocabulary_for(family_version: int) -> CampaignVocabulary:
    """Select explicit family 13/14/15 operational spellings without coercion."""

    if type(family_version) is int and family_version == 15:
        return replace(_VOCABULARIES[14], family_version=15)
    if type(family_version) is not int or family_version not in _VOCABULARIES:
        raise ValueError("unsupported_campaign_vocabulary_family")
    return _VOCABULARIES[family_version]
