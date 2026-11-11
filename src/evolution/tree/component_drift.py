"""Component drift in campaign family 12 (G2 spec §4.6; ADR-0024 as amended 2026-10-02).

An Attempt selected in component mode relied on two maps: the Candidate's,
from its `candidate_inspected`, and the Parent's, the one the baseline rows
name. A measurement instance that loads either tree must render the same
map. Inside that map's reference epoch a different map is target
instability and suspends (`target_components_changed`); after an accepted
condition change it is the report-only `components_changed_by_condition`.
The executor, the audit and the report all decide through `drift_suspends`.

Family 13 (F7 plan choice 10, design §9) checks the final cohort's members
too: a finalist member against its Attempt's Candidate reference, the
Champion member against the round's own reference (`champion_reference`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from evolution.tree.affected_cases import (
    SampleProvenance,
    baseline_provenance,
    declaring_measurements,
    parent_mode,
)
from evolution.tree.execution.journal_schema import declaration_digest

if TYPE_CHECKING:
    from evolution.tree.campaign_schema import AttemptState, CampaignSnapshot


@dataclass(frozen=True)
class DriftReference:
    side: Literal["candidate", "parent", "champion"]
    tree: str  # the tree's candidate_digest
    digest: str  # the reference map's components_digest
    epoch: int  # its reference epoch (G2 spec §4.6, as narrowed by G2b3b)


@dataclass(frozen=True)
class ComponentsCheck:
    reference: str  # DriftReference.digest
    suspends: bool  # reference epoch == the checked measurement's bind epoch


def checked_selection(attempt: AttemptState) -> bool:
    """A component-mode selection that did not fall back to every case."""

    selection = attempt.selection
    return (
        selection is not None
        and selection["mode"] == "component"
        and selection["fallback_reason"] is None
    )


def candidate_reference(attempt: AttemptState) -> DriftReference | None:
    if not checked_selection(attempt) or attempt.inspected_components is None:
        return None
    assert attempt.candidate_digest is not None and attempt.inspected_epoch is not None
    return DriftReference(
        "candidate",
        attempt.candidate_digest,
        declaration_digest(attempt.inspected_components, attempt.inspected_withheld),
        attempt.inspected_epoch,
    )


def parent_reference(
    snapshot: CampaignSnapshot,
    attempt: AttemptState,
    baseline: Mapping[str, Sequence[SampleProvenance]],
    declaring: Mapping[str, Sequence[str]],
) -> DriftReference | None:
    if not checked_selection(attempt):
        return None
    _mode, digest, _fallback = parent_mode(baseline, {d: ({}, 0) for d in declaring})
    assert digest is not None  # a checked selection had no fallback
    epochs = [int(snapshot.measurements[mid]["epoch"]) for mid in declaring[digest]]
    return DriftReference("parent", attempt.parent_digest, digest, max(epochs))


def baseline_parent_reference(
    snapshot: CampaignSnapshot,
    attempt: AttemptState,
    baseline_id: str,
    baseline_records: Sequence[Any],
    source_records: Callable[[str], Sequence[Any] | None],
) -> DriftReference | None:
    """`parent_reference` over the round's cited baseline and its same-run sources.

    The one reading the executor and the audit share, each with its own
    record access (`baseline_inputs`' convention).
    """

    if not checked_selection(attempt):
        return None
    baseline = baseline_provenance(baseline_records)
    declaring = declaring_measurements(
        baseline_id, baseline_records, baseline, snapshot.run_id, source_records
    )
    return parent_reference(snapshot, attempt, baseline, declaring)


def champion_reference(
    snapshot: CampaignSnapshot,
    round_number: int,
    baseline_id: str,
    baseline_records: Sequence[Any],
    source_records: Callable[[str], Sequence[Any] | None],
) -> DriftReference | None:
    """Family 13 (F7 plan choice 10): the round's reference for its Champion member.

    `parent_mode` over the round's cited baseline and its same-run sources,
    as `baseline_parent_reference` reads them, but with no Attempt: it does
    not depend on any selection. `None` when the baseline rows do not share
    one map digest, when they declare none (`path`), and when no measurement
    of this run declares the shared map (`components_unavailable`: there is
    no reference epoch). The tree is the Champion the round's cohort binds.
    """

    baseline = baseline_provenance(baseline_records)
    declaring = declaring_measurements(
        baseline_id, baseline_records, baseline, snapshot.run_id, source_records
    )
    _mode, digest, fallback = parent_mode(baseline, {d: ({}, 0) for d in declaring})
    if digest is None or fallback is not None:
        return None
    if snapshot.family_version == 15:
        champion_digest = snapshot.measurements[baseline_id]["candidate_digest"]
    else:
        cohort = snapshot.rounds[round_number].cohort
        assert cohort is not None
        champion_digest = cohort["champion"]["digest"]
    epochs = [int(snapshot.measurements[mid]["epoch"]) for mid in declaring[digest]]
    return DriftReference("champion", str(champion_digest), digest, max(epochs))


def drift_suspends(reference: DriftReference, epoch: int, declared: Collection[str]) -> bool:
    """Same reference epoch, or a map this measurement already rendered in its
    own epoch: the Target changed its rendering with no accepted change since."""

    return reference.epoch == epoch or reference.digest in declared
