"""Evidence settings bind once per round, including the b12 upgrade boundary."""

from dataclasses import replace

import pytest

from evolution.tree import evolve as evolve_module
from evolution.tree.campaign_identity import AdaptationIdentityV1, IdentityError
from evolution.tree.campaign_projections import inspect_view
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE
from tests.campaign_drivers import audit_mismatches
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run
from tests.tree.manifest.test_manifest_v16 import (
    _role_adaptation,
    chat_roles_document,
    load,
    v15_document,
    v16_document,
)

pytestmark = pytest.mark.slow


def test_v16_always_binds_evidence_with_every_member_filled(tmp_path):
    identity = _role_adaptation(load(tmp_path, chat_roles_document(v16_document())), tmp_path)
    assert identity.to_payload()["evidence"] == DEFAULT_EVIDENCE.to_payload()


def test_v15_adaptation_payload_has_no_evidence(tmp_path):
    identity = _role_adaptation(load(tmp_path, chat_roles_document(v15_document())), tmp_path)
    assert "evidence" not in identity.to_payload()
    # Captured from 02c2873f3 with the same fixture and _adaptive_config arguments.
    assert identity.digest == "52fa05a9d1c6245dfb2009e1db10587d7b198b4c1703ed9dce09494324f6ac80"


@pytest.mark.parametrize(
    "evidence",
    [
        {"version": "evidence/2"},
        {"version": "evidence/1", "turns": 0},
        {"version": "evidence/1", "turns": True},
    ],
)
def test_adaptation_refuses_invalid_evidence(tmp_path, evidence):
    identity = _role_adaptation(load(tmp_path, chat_roles_document(v16_document())), tmp_path)
    with pytest.raises(IdentityError):
        AdaptationIdentityV1({**identity.to_payload(), "evidence": evidence})


def test_evidence_pin_owns_its_modes_and_returns_a_detached_payload():
    from evolution.tree.execution.evidence_settings import EvidencePin

    modes = {"history_excerpt_screen": "shadow"}
    pin = EvidencePin(DEFAULT_EVIDENCE, modes)
    modes["history_excerpt_screen"] = "off"
    assert pin.input_modes["history_excerpt_screen"] == "shadow"
    with pytest.raises(TypeError):
        pin.input_modes["history_excerpt_screen"] = "off"
    payload = pin.payload()
    payload["input_checks"]["history_excerpt_screen"] = "off"
    assert pin.payload() == {
        "evidence": DEFAULT_EVIDENCE.to_payload(),
        "input_checks": {"history_excerpt_screen": "shadow"},
    }


@pytest.mark.parametrize("old_writer", [False, True], ids=["changed-pin", "b12-upgrade"])
async def test_a_rebase_keeps_the_open_round_pin_and_changes_the_next(
    tmp_path, monkeypatch, old_writer
):
    real = evolve_module._adaptive_config

    def b12_config(*args, **kwargs):
        config = real(*args, **kwargs)
        payload = config.identity.to_payload()
        payload.pop("evidence", None)
        return replace(config, identity=AdaptationIdentityV1(payload))

    with monkeypatch.context() as patch:
        if old_writer:
            patch.setattr(evolve_module, "_adaptive_config", b12_config)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, patch, campaign={"rounds": 2}, cut="after_attempt_input_frozen"
            )
    run = tmp_path / "run"
    before = inspect_view(run).snapshot.round_evidence(1)
    if old_writer:
        assert before is None
    else:
        assert before.settings == DEFAULT_EVIDENCE
        assert set(before.input_modes.values()) == {"enforce"}

    def configure(document):
        if not old_writer:
            document["evidence"] = {"version": "evidence/1", "turns": 20}
            document["checks"] = {
                "version": "checks/1",
                "modes": {"history_excerpt_screen": "shadow", "connection_preflight": "off"},
            }

    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, campaign={"rounds": 2}, configure=configure)
    refusal = caught.value
    assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
    assert sorted(d.field for d in refusal.differences) == (
        ["adaptation"] if old_writer else ["adaptation", "identity.checks"]
    )
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 2},
        configure=configure,
        authorizations=[_decision(refusal, None, "accept evidence settings")],
    )
    assert result.terminal == "finished"
    assert len(payloads(run, "identity_rebased")) == 1
    snapshot = inspect_view(run).snapshot
    assert snapshot.round_evidence(1) == before
    after = snapshot.round_evidence(2)
    assert after.settings.turns == (8 if old_writer else 20)
    assert after.input_modes == {
        "input_run_secret_redaction": "enforce",
        "attempt_feedback_screen": "enforce",
        "history_excerpt_screen": "enforce" if old_writer else "shadow",
    }
    assert audit_mismatches(run) == []


@pytest.mark.parametrize(
    "section,key,value",
    [
        (None, "evidence", DEFAULT_EVIDENCE.to_payload()),
        (None, "checks", {"version": "checks/1", "modes": {}}),
        ("grouping", "max_consecutive_full_fallbacks", 2),
        ("grouping", "max_repairs", 0),
        ("reflection", "max_consecutive_skips", 2),
    ],
)
def test_family14_refuses_new_adaptation_controls(tmp_path, section, key, value):
    from evolution.tree.campaign_schema import SchemaError, validate_payload
    from tests.campaign_drivers import native14_created

    created = native14_created()
    payload = created["adaptation_payload"]
    if key == "max_repairs":
        payload["grouping"] = _role_adaptation(
            load(tmp_path, chat_roles_document(v15_document())), tmp_path
        ).to_payload()["grouping"]
    target = payload if section is None else payload[section]
    target[key] = value
    identity = AdaptationIdentityV1(payload)
    created["adaptation_payload"] = identity.to_payload()
    created["adaptation"]["digest"] = identity.digest
    with pytest.raises(SchemaError):
        validate_payload("campaign_created", created, final=True)


def _v16_grouping(tmp_path, **keys):
    document = chat_roles_document(v16_document())
    document["grouper"].update(keys)
    return _role_adaptation(load(tmp_path, document), tmp_path).to_payload()["grouping"]


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"group_output_tokens": 128},
        {"glimpse_chars": 400},
        {"max_set_members": 2},
        {"read_chars": 8000},
        {"max_turns": 4},
        {"card_examples": 0},
        {"max_reduce_levels": 2},
        {"max_requests": 50},
    ],
)
def test_family14_validator_refuses_model_grouping_v2_and_keys(tmp_path, keys):
    """b16 spec §11: `model_grouping/2` and its keys are family 15's only."""
    from evolution.tree.campaign_schema import SchemaError, validate_payload
    from tests.campaign_drivers import native14_created

    created = native14_created()
    payload = created["adaptation_payload"]
    payload["grouping"] = _v16_grouping(tmp_path, **keys)
    assert payload["grouping"]["protocol"] == "model_grouping/2"
    identity = AdaptationIdentityV1(payload)
    created["adaptation_payload"] = identity.to_payload()
    created["adaptation"]["digest"] = identity.digest
    with pytest.raises(SchemaError, match="family 14 adaptation"):
        validate_payload("campaign_created", created, final=True)


def test_family14_still_reads_its_model_grouping_v1_payload(tmp_path):
    from evolution.tree.campaign_schema import validate_payload
    from tests.campaign_drivers import native14_created

    created = native14_created()
    payload = created["adaptation_payload"]
    payload["grouping"] = _role_adaptation(
        load(tmp_path, chat_roles_document(v15_document())), tmp_path
    ).to_payload()["grouping"]
    identity = AdaptationIdentityV1(payload)
    created["adaptation_payload"] = identity.to_payload()
    created["adaptation"]["digest"] = identity.digest
    validate_payload("campaign_created", created, final=True)


@pytest.mark.parametrize("protocol", ["model_grouping/1", "model_grouping/2"])
def test_family15_reads_stored_model_grouping_v1_payload(tmp_path, protocol):
    """A family-15 payload journaled before b16 (`model_grouping/1`) stays readable."""
    from evolution.tree.campaign_schema import _validate_adaptation_payload

    payload = _role_adaptation(
        load(tmp_path, chat_roles_document(v16_document())), tmp_path
    ).to_payload()
    if protocol == "model_grouping/1":
        payload["grouping"] = _role_adaptation(
            load(tmp_path, chat_roles_document(v15_document())), tmp_path
        ).to_payload()["grouping"]
    else:
        payload["grouping"] = _v16_grouping(tmp_path, max_turns=3, card_examples=0)
    identity = AdaptationIdentityV1(payload)
    assert identity.to_payload()["grouping"]["protocol"] == protocol
    slot = {"digest": identity.digest, "version": identity.profile}
    _validate_adaptation_payload(identity.to_payload(), slot, family_version=15)


def test_evidence_is_not_admitted_by_an_older_adaptation_profile(tmp_path):
    payload = _role_adaptation(
        load(tmp_path, chat_roles_document(v15_document())), tmp_path
    ).to_payload()
    payload["profile"] = "campaign_adaptation/6"
    payload["grouping"]["kind"] = "model"
    AdaptationIdentityV1(payload)
    with pytest.raises(IdentityError):
        AdaptationIdentityV1({**payload, "evidence": DEFAULT_EVIDENCE.to_payload()})


def test_checks_without_evidence_do_not_create_a_round_pin():
    from evolution.tree.campaign_schema import pin_from_adaptation

    assert pin_from_adaptation({"checks": {"version": "checks/1", "modes": {}}}) is None
