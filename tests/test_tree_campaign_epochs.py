"""I2 Task 1: condition epochs in the lifecycle snapshot (ADR-0020, spec §5.1).

Phase A only: the creation record opens epoch 0 and every measurement binds
into it. `_identity_rebased` is written to open a further epoch on a
comparable change, but nothing admits a comparable `identity_rebased` yet
(ADR-0015), so a rebase today is always non-comparable and opens no epoch --
these fixtures exercise exactly that boundary, not the (later) comparable
case.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from evolution.tree.campaign import _Campaign, inspect_campaign
from evolution.tree.campaign_epochs import (
    admissible_effects,
    base_measurement_id,
    epoch_identity,
    epoch_of,
    measurement_id_for_epoch,
    visibility_forces_close,
)
from evolution.tree.campaign_identity import CampaignIdentityV1
from evolution.tree.campaign_projections import _Evidence, _PreconditionFailed
from evolution.tree.execution.authorizations import RefusalDifference
from tests.campaign_fakes import (  # noqa: F401
    all_offline_campaign_fixtures,
    campaign_with_version_rebase,
    finished_campaign,
)

pytestmark = pytest.mark.slow


def test_measurement_id_for_epoch_zero_is_the_base_id() -> None:
    assert measurement_id_for_epoch("m-r01-a01", 0) == "m-r01-a01"
    assert measurement_id_for_epoch("m-r01-a01", 2) == "m-r01-a01@e2"
    assert base_measurement_id("m-r01-a01@e2") == "m-r01-a01"
    assert base_measurement_id("m-r00-champion") == "m-r00-champion"


def test_a_run_without_comparable_rebase_has_one_epoch(finished_campaign) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    snapshot = finished_campaign.snapshot()
    assert len(snapshot.epochs) == 1
    assert snapshot.epochs[0] == finished_campaign.creation_identity_payload
    for measurement_id, entry in snapshot.measurements.items():
        assert entry["epoch"] == 0
        assert epoch_of(snapshot, measurement_id) == 0
        assert epoch_identity(snapshot, measurement_id).digest == snapshot.creation_identity_digest


def test_a_non_comparable_rebase_opens_no_epoch(campaign_with_version_rebase) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    snapshot = campaign_with_version_rebase.snapshot()
    assert len(snapshot.rebases) == 1
    assert len(snapshot.epochs) == 1


# -- Task 2: every measurement derives from its binding epoch -------------------


async def test_resume_derives_siblings_from_their_binding_epoch(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    finished_campaign,  # noqa: F811
) -> None:
    seen: list[tuple[str, str]] = []
    original = _Campaign.derive_with

    def spy(self, identity, measurement_id, candidate_digest, side, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((measurement_id, identity.digest))
        return original(self, identity, measurement_id, candidate_digest, side, **kwargs)

    monkeypatch.setattr(_Campaign, "derive_with", spy)
    await finished_campaign.reopen_read_only()
    snapshot = finished_campaign.snapshot()
    assert seen and all(digest == epoch_identity(snapshot, mid).digest for mid, digest in seen)
    # Both admission (`_admit_siblings_read_only`) and adoption
    # (`adopt_siblings`) derive every bound measurement.
    assert sorted({mid for mid, _ in seen}) == sorted(snapshot.measurements)


def test_audit_uses_the_binding_epoch(finished_campaign) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    report = inspect_campaign(finished_campaign.run_root, verify=True).to_payload()
    assert report["verification"]["mismatches"] == []


async def test_resume_reads_each_sibling_identity_off_its_epoch(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    finished_campaign,  # noqa: F811
) -> None:
    """Resume asks `epoch_identity` for every bound measurement, in admission and adoption."""

    from evolution.tree import campaign as campaign_module

    asked: list[str] = []
    original = campaign_module.epoch_identity

    def spy(snapshot, measurement_id):  # type: ignore[no-untyped-def]
        asked.append(measurement_id)
        return original(snapshot, measurement_id)

    monkeypatch.setattr(campaign_module, "epoch_identity", spy)
    await finished_campaign.reopen_read_only()
    measurements = sorted(finished_campaign.snapshot().measurements)
    assert sorted(asked) == sorted(measurements * 2)


def _record(event: str, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(event=event, payload=payload)


def test_evidence_rebuilds_the_binding_epoch_from_the_records(  # type: ignore[no-untyped-def]
    finished_campaign,  # noqa: F811
) -> None:
    """`_Evidence.identity_for` counts only comparable rebases, by record position.

    Nothing admits a comparable `identity_rebased` yet, so the prefix is
    synthetic: the audit's epoch rebuild is read off the records alone.
    """

    creation = finished_campaign.creation_identity_payload
    moved = copy.deepcopy(creation)
    moved["scorer"]["digest"] = "scorer-v2"
    records = (
        _record("campaign_created", identity=creation),
        _record("measurement_bound", measurement_id="m-r00-champion"),
        _record("identity_rebased", identity=creation, changed_fields=["producing_version"]),
        _record("measurement_bound", measurement_id="m-r01-a01"),
        _record("identity_rebased", identity=moved, changed_fields=["identity.scorer.digest"]),
        _record("measurement_bound", measurement_id="m-r01-a01@e1"),
    )
    context = _Evidence(finished_campaign.run_root, records, {}, family_version=14)  # type: ignore[arg-type]
    epoch0 = CampaignIdentityV1.from_payload(creation).digest
    epoch1 = CampaignIdentityV1.from_payload(moved).digest
    assert epoch0 != epoch1
    assert context.identity_for("m-r00-champion").digest == epoch0
    assert context.identity_for("m-r01-a01").digest == epoch0
    assert context.identity_for("m-r01-a01@e1").digest == epoch1
    assert context.identity().digest == epoch0
    with pytest.raises(_PreconditionFailed):
        context.identity_for("m-r09-a09")


def test_evidence_places_a_rebound_measurement_in_its_records_epoch(  # type: ignore[no-untyped-def]
    finished_campaign,  # noqa: F811
) -> None:
    """Task 5: a `measurement_rebound` creates its measurement where it stands."""

    creation = finished_campaign.creation_identity_payload
    moved = copy.deepcopy(creation)
    moved["target"]["settings_hash"] = "settings-2"
    records = (
        _record("campaign_created", identity=creation),
        _record("measurement_bound", measurement_id="m-r01-a01"),
        _record(
            "identity_rebased", identity=moved, changed_fields=["identity.target.settings_hash"]
        ),
        _record("measurement_rebound", measurement_id="m-r01-a01@e1"),
    )
    context = _Evidence(finished_campaign.run_root, records, {}, family_version=14)  # type: ignore[arg-type]
    assert (
        context.identity_for("m-r01-a01").digest == CampaignIdentityV1.from_payload(creation).digest
    )
    assert (
        context.identity_for("m-r01-a01@e1").digest == CampaignIdentityV1.from_payload(moved).digest
    )


# -- H11 Task 7: a family-14 `role_visibility` change offers only `close` ------


def _difference(field: str) -> RefusalDifference:
    return RefusalDifference(field, "a" * 64, "b" * 64)


VISIBILITY = _difference("identity.role_visibility")
TARGET = _difference("identity.target.settings_hash")
POLICY = _difference("identity.policy_digest")
SCORER = _difference("identity.scorer.digest")
EDITOR = _difference("identity.editor.digest")
VERSION = RefusalDifference("producing_version", "0.3.0a32", "0.3.0a33")


def test_visibility_forces_close_only_for_a_family_14_visibility_difference() -> None:
    """PDR-0023, ADR-0029: the identity field path is `identity.role_visibility`.

    The identity journals the `role_visibility/1` digest as a string, so
    `identity_differences` names the field itself; a dotted descendant is
    matched too, so a structured value could never slip past.
    """

    assert visibility_forces_close(14, (VISIBILITY,))
    assert visibility_forces_close(14, (TARGET, VISIBILITY, VERSION))
    assert visibility_forces_close(14, (_difference("identity.role_visibility.readable"),))
    assert not visibility_forces_close(13, (VISIBILITY,))
    assert not visibility_forces_close(14, ())
    assert not visibility_forces_close(14, (TARGET, POLICY, EDITOR, VERSION))
    # The metric contract is the other connection field: comparable, not close-only.
    assert not visibility_forces_close(14, (_difference("identity.metric_contract"),))
    assert not visibility_forces_close(14, (_difference("identity.role_visibility_extra"),))


@pytest.mark.parametrize("remaining", [0, 3])
@pytest.mark.parametrize(
    "differences",
    [
        (VISIBILITY,),
        (VISIBILITY, TARGET),
        (TARGET, VISIBILITY),
        (VISIBILITY, POLICY),
        (VISIBILITY, SCORER),
        (VISIBILITY, VERSION),
        (VISIBILITY, EDITOR),
        (VISIBILITY, TARGET, POLICY, VERSION),
    ],
)
def test_a_family_14_visibility_change_admits_only_close(
    differences: tuple[RefusalDifference, ...], remaining: int
) -> None:
    assert admissible_effects(14, differences, remaining) == ("close",)


def test_family_14_non_visibility_effects_are_unchanged() -> None:
    for family in (14,):
        assert admissible_effects(family, (TARGET,), 3) == ("continue", "rebaseline", "close")
        assert admissible_effects(family, (TARGET,), 0) == ("continue", "close")
        assert admissible_effects(family, (POLICY,), 3) == ("rebaseline", "close")
        assert admissible_effects(family, (TARGET, POLICY), 0) == ("close",)
        assert admissible_effects(family, (EDITOR, VERSION), 3) == ("close",)
