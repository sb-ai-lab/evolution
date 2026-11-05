"""Journal family boundaries survive authorized identity replacement."""

from __future__ import annotations

import copy
import hashlib

import pytest

from evolution.tree.campaign_identity import CampaignIdentityV1, LinkedFrom
from evolution.tree.campaign_schema import CampaignLifecycle, SchemaError, campaign_family_for
from evolution.tree.execution.authorizations import (
    Authorization,
    identity_differences,
    identity_instance_digest,
    identity_record,
)
from evolution.tree.execution.journal_identity import IdentityError, canonical_json
from evolution.tree.storage.envelope import Envelope
from tests.test_tree_campaign_identity import DECLARATION, _linked_from
from tests.test_tree_family14_identity import CONNECTION, _created14


class _NativeLog:
    """The reader-boundary tests need only envelope admission and native reduction."""

    def __init__(self):
        self.records = []
        self.state = CampaignLifecycle(selector=None, adaptive=True, family_version=14)

    def push(self, event, **payload):
        record = Envelope.from_payload(
            dict(
                schema_version=1,
                run_id="run",
                sequence=len(self.records) + 1,
                timestamp="2026-01-01T00:00:00Z",
                event=event,
                operation_id=None,
                payload=payload,
            ),
            family=campaign_family_for(14),
        )
        self.state.accept(record)
        self.records.append(record)
        return record


def _created(family):
    if family != 14:
        raise ValueError("reader-boundary fixtures require family 14")
    log = _NativeLog()
    log.push("campaign_created", **_created14())
    return log


def _change(log, edit, *, effect="rebaseline"):
    created = log.records[0].payload
    before = {
        key: copy.deepcopy(created[key])
        for key in ("identity", "identity_digest", "producing_version", "adaptation")
    }
    after = copy.deepcopy(before)
    edit(after["identity"])
    after["identity_digest"] = hashlib.sha256(canonical_json(after["identity"])).hexdigest()
    journaled = identity_record(before)
    observed = identity_record(after)
    triple_keys = ("identity_digest", "producing_version", "adaptation_digest")
    journaled = {key: journaled[key] for key in triple_keys}
    observed = {key: observed[key] for key in triple_keys}
    instance = identity_instance_digest(journaled, observed)
    decision = Authorization.new(
        "run_identity_changed",
        instance,
        "accepted connection repair",
        now="2026-10-10T00:00:00Z",
        effect=effect,
    )
    rebased = {
        **after,
        "previous_digest": before["identity_digest"],
        "instance_digest": instance,
        "changed_fields": sorted(d.field for d in identity_differences(before, after)),
        "authorization": decision.journal_payload(),
    }
    if "adaptation_payload" in created:
        rebased["adaptation_payload"] = copy.deepcopy(created["adaptation_payload"])
    applied = dict(
        effect=effect,
        code="run_identity_changed",
        instance_digest=instance,
        authorization=decision.decision_payload(),
        journaled=journaled,
        observed=observed,
    )
    return applied, rebased


@pytest.mark.parametrize("family", [14])
def test_rebaseline_cannot_cross_the_connection_field_family_boundary(family):
    log = _created(family)

    def edit(identity):
        if family < 14:
            identity.update(CONNECTION)
        else:
            for key in CONNECTION:
                del identity[key]

    applied, rebased = _change(log, edit)
    # The operator decision is valid and accepted. Shape validation must stop
    # the owed rebase, rather than relying on unrelated lifecycle checks.
    log.push("authorization_applied", **applied)
    assert log.state.snapshot.pending_change["phase"] == "rebase"
    message = (
        f"family {family} refuses metric_contract and role_visibility"
        if family < 14
        else "family 14 identity binds metric_contract and role_visibility"
    )
    with pytest.raises(SchemaError, match=f"^{message}$"):
        campaign_family_for(family).validate_payload("identity_rebased", rebased)
    # Envelope deliberately wraps payload refusals at the journal boundary.
    with pytest.raises(SchemaError, match="^invalid journal record$"):
        log.push("identity_rebased", **rebased)
    assert len(log.state.snapshot.epochs) == 1


@pytest.mark.parametrize("family", [14])
def test_valid_rebaseline_keeps_its_family_identity_and_opens_an_epoch(family):
    log = _created(family)

    def edit(identity):
        if family == 14:
            identity["metric_contract"] = "e" * 64
        else:
            identity["scorer"]["digest"] = "e" * 64

    applied, rebased = _change(log, edit)
    log.push("authorization_applied", **applied)
    log.push("identity_rebased", **rebased)
    snapshot = log.state.snapshot
    assert len(snapshot.epochs) == 2
    assert snapshot.pending_change is None
    assert snapshot.condition_changes[-1]["fields"] == (
        ["metric_contract"] if family == 14 else ["scorer"]
    )
    assert snapshot.identity_digest == rebased["identity_digest"]


@pytest.mark.parametrize("family", [14])
@pytest.mark.parametrize("event", ["campaign_created", "identity_rebased"])
@pytest.mark.parametrize("field", ["metric_contract", "role_visibility"])
def test_single_connection_field_keeps_the_malformed_identity_classification(family, event, field):
    log = _created(family)

    def edit(identity):
        if family == 14:
            del identity[field]
        else:
            identity[field] = "e" * 64

    if event == "campaign_created":
        payload = copy.deepcopy(log.records[0].payload)
        edit(payload["identity"])
        payload["identity_digest"] = hashlib.sha256(canonical_json(payload["identity"])).hexdigest()
    else:
        _, payload = _change(log, edit)
    with pytest.raises(SchemaError, match="^campaign identity is invalid$"):
        campaign_family_for(family).validate_payload(event, payload)


def test_context_free_link_preserves_legacy_bare_baseline_field_grammar():
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        _linked_from(baseline={**DECLARATION, "fields": ["metric_contract"]})


def test_standalone_identity_still_admits_both_optional_connection_shapes():
    payload = _created14()["identity"]
    assert CampaignIdentityV1.from_payload(payload).to_payload() == payload
    for field in CONNECTION:
        del payload[field]
    assert CampaignIdentityV1.from_payload(payload).to_payload() == payload


@pytest.mark.parametrize("field", ["target.Invalid", "target.a.b.c", "identity.Metric_contract"])
def test_context_free_link_keeps_the_legacy_dotted_grammar_closed(field):
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        _linked_from(baseline={**DECLARATION, "fields": [field]})


@pytest.mark.parametrize(
    "field",
    [
        "artifact_store",
        "corpus_cases",
        "corpus_digest",
        "environment",
        "evaluation_set_id",
        "materialization_strategy",
        "metric_plan_digest",
        "policy_digest",
        "scorer",
        "target",
        "identity.metric_contract",
        "metric_contract.pointer",
        "identity.target.settings_hash",
    ],
)
def test_context_free_link_keeps_all_legacy_bare_and_dotted_baseline_fields(field):
    linked = _linked_from(baseline={**DECLARATION, "fields": [field]})
    assert linked.to_payload()["baseline"]["fields"] == [field]


def test_family14_creation_accepts_a_dotted_metric_contract_baseline_field():
    created = _created14()
    created["identity"]["linked_from"] = _linked_from(
        baseline={**DECLARATION, "fields": ["identity.metric_contract"]}
    ).to_payload()
    created["identity_digest"] = hashlib.sha256(canonical_json(created["identity"])).hexdigest()
    campaign_family_for(14).validate_payload("campaign_created", created)


@pytest.mark.parametrize("family,expected", [(14, ("rebaseline", "close"))])
def test_admissible_effects_classifies_metric_contract_by_recorded_family(family, expected):
    from evolution.tree.campaign_epochs import admissible_effects
    from evolution.tree.execution.authorizations import RefusalDifference

    assert (
        admissible_effects(family, [RefusalDifference("identity.metric_contract", "a", "b")], 1)
        == expected
    )


@pytest.mark.parametrize("choice", ["version", "adaptation", "policy"])
def test_authorized_rebase_preserves_v11_policy_and_identity(choice):
    from evolution.tree.campaign_identity import AdaptationIdentityV1
    from evolution.tree.campaign_policy import CampaignPolicyV11

    log = _created(14)
    before = log.records[0].payload
    _, rebased = _change(log, lambda identity: None)
    if choice == "version":
        rebased["producing_version"] = "0.3.0a34"
    elif choice == "adaptation":
        rebased["adaptation_payload"]["reflection"]["max_consecutive_failures"] = 4
        rebased["adaptation"]["digest"] = AdaptationIdentityV1.from_payload(
            rebased["adaptation_payload"]
        ).digest
    else:
        policy = CampaignPolicyV11.from_payload({**rebased["identity"]["policy"], "rounds": 3})
        rebased["identity"]["policy"] = policy.to_payload()
        rebased["identity"]["policy_digest"] = policy.digest
    rebased["identity_digest"] = hashlib.sha256(canonical_json(rebased["identity"])).hexdigest()
    rebased["changed_fields"] = sorted(
        d.field for d in identity_differences(identity_record(before), identity_record(rebased))
    )
    instance = identity_instance_digest(identity_record(before), identity_record(rebased))
    decision = Authorization.new(
        "run_identity_changed",
        instance,
        "accept rebase",
        now="2026-10-10T00:00:00Z",
        effect="rebaseline" if choice == "policy" else None,
    )
    rebased["instance_digest"] = instance
    rebased["authorization"] = decision.journal_payload()
    if choice == "policy":
        log.push(
            "authorization_applied",
            effect="rebaseline",
            code="run_identity_changed",
            instance_digest=instance,
            authorization=decision.decision_payload(),
            journaled={
                k: identity_record(before)[k]
                for k in ("identity_digest", "producing_version", "adaptation_digest")
            },
            observed={
                k: identity_record(rebased)[k]
                for k in ("identity_digest", "producing_version", "adaptation_digest")
            },
        )
    log.push("identity_rebased", **rebased)
    assert type(log.state.policy) is CampaignPolicyV11
    policy = log.state.policy
    assert (
        policy.stability,
        policy.round_stop,
        policy.baseline_refresh_after_rounds,
        policy.focus_margin,
    ) == ("stable", "first_accepted", 3, 0.0)
    assert log.state.snapshot.identity_digest == rebased["identity_digest"]
    assert log.state.snapshot.identity["policy_digest"] == policy.digest


def test_family14_link_context_admits_a_bare_metric_contract_baseline_field():
    linked = _linked_from(
        baseline={**DECLARATION, "fields": ["identity.policy_digest", "metric_contract"]},
        successor_family=14,
    )
    assert linked.to_payload()["baseline"]["fields"] == [
        "identity.policy_digest",
        "metric_contract",
    ]


@pytest.mark.parametrize("family", [None])
def test_older_or_absent_link_context_still_refuses_a_bare_metric_contract(family):
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        _linked_from(
            baseline={**DECLARATION, "fields": ["metric_contract"]}, successor_family=family
        )


def test_link_context_is_never_serialized_or_compared():
    import dataclasses

    plain = _linked_from(baseline=DECLARATION)
    framed = _linked_from(baseline=DECLARATION, successor_family=14)
    assert framed.to_payload() == plain.to_payload()
    assert framed == plain
    assert "successor_family" not in repr(framed)
    assert (
        dataclasses.replace(framed, condition_changed=("identity.scorer.digest",)).successor_family
        == 14
    )


def test_from_payload_threads_the_link_context():
    payload = _linked_from(baseline=DECLARATION).to_payload()
    payload["baseline"]["fields"] = ["metric_contract"]
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        LinkedFrom.from_payload(payload)
    assert LinkedFrom.from_payload(payload, successor_family=14).successor_family == 14


def test_family14_creation_accepts_a_bare_metric_contract_baseline_field():
    created = _created14()
    linked = _linked_from(baseline=DECLARATION).to_payload()
    linked["baseline"]["fields"] = ["metric_contract"]
    created["identity"]["linked_from"] = linked
    created["identity_digest"] = hashlib.sha256(canonical_json(created["identity"])).hexdigest()
    campaign_family_for(14).validate_payload("campaign_created", created)
    assert CampaignIdentityV1.from_payload(created["identity"]).to_payload() == created["identity"]


def test_the_connection_family_constant_is_the_final_family():
    from evolution.tree.campaign_identity import CONNECTION_FAMILY_VERSION
    from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION

    assert CONNECTION_FAMILY_VERSION == FINAL_CAMPAIGN_FAMILY_VERSION


def test_link_context_helper_follows_the_connection_fields():
    from evolution.tree.campaign_identity import _link_context

    assert _link_context(_created14()["identity"]) == 14
