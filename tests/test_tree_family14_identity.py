"""Family 14 in the campaign identity, the adaptation identity and the family
seams (family-14 implementation design, slice H1: R13-R18, M1, M8)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from evolution.prompts import TREE_AGENT_GROUPER_SYSTEM, TREE_GROUPING_MODEL_SYSTEM
from evolution.tree import campaign as campaign_module
from evolution.tree.artifacts.attestation import TargetPreflight
from evolution.tree.campaign_epochs import WINDOW_OWNER_OF_FIELD, restarted_owners
from evolution.tree.campaign_identity import (
    FINAL_ADAPTATION_PROFILE,
    AdaptationIdentityV1,
    CampaignIdentityV1,
    CapabilitiesInsufficient,
    admit_capabilities,
    build_adaptation_identity,
    build_identity,
    reflection_group_text_chars,
)
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_schema import (
    FINAL_CAMPAIGN_FAMILY,
    FINAL_CAMPAIGN_FAMILY_VERSION,
    CampaignLifecycle,
    SchemaError,
    campaign_family_for,
    validate_payload,
)
from evolution.tree.execution.authorizations import (
    COMPARABLE_FIELDS,
    comparable_changed,
    comparable_field,
    identity_differences,
)
from evolution.tree.execution.interaction import AdapterCapabilities, EvalCase
from evolution.tree.execution.journal_identity import IdentityError, canonical_json
from evolution.tree.roles.grouping.grouping_runtime import AdaptiveCampaignRuntimeConfig
from evolution.tree.roles.grouping.reflect_grouping import FieldFeature
from evolution.tree.target.connection_contract import (
    ConnectionContract,
    MetricContract,
    RoleVisibility,
)
from tests.campaign_fakes import (
    family14_admitted,
    v11_policy,
)
from tests.test_tree_campaign_identity import (
    EDITOR,
    ENV,
    REFLECTOR,
    SCORER,
    STORE_IDENTITY,
    _semantic,
)

CORPUS = [EvalCase("c1", "g1", "s1", (), {}, {}, {}), EvalCase("c2", "g1", "s1", (), {}, {}, {})]
CONNECTION = {"metric_contract": "c" * 64, "role_visibility": "d" * 64}


def preflight(max_instances=16, sessions=2):
    capabilities = AdapterCapabilities(
        "exact", sessions, True, frozenset({"artifact_root_injection"}), max_instances
    )
    return TargetPreflight(_semantic("exact"), capabilities, "boot-1")


def identity(policy, connection=None):
    return build_identity(
        policy,
        preflight(),
        ENV,
        SCORER,
        EDITOR,
        CORPUS,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
        connection=connection,
    )


# -- Identity bytes of families up to 13 are unchanged ------------------------------


# -- R17, M8: the two optional fields ------------------------------------------------


def test_a_family_14_identity_carries_both_connection_digests():
    built = identity(v11_policy(), CONNECTION)
    payload = built.to_payload()
    assert (payload["metric_contract"], payload["role_visibility"]) == ("c" * 64, "d" * 64)
    assert CampaignIdentityV1.from_payload(payload).digest == built.digest


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda p: p.pop("role_visibility"), "invalid_identity_fields"),
        (lambda p: p.pop("metric_contract"), "invalid_identity_fields"),
        (lambda p: p.__setitem__("metric_contract", "zz"), "invalid_identity_digest"),
        (lambda p: p.__setitem__("role_visibility", None), "invalid_identity_digest"),
    ],
)
def test_the_connection_fields_come_together_as_digests(edit, code):
    payload = identity(v11_policy(), CONNECTION).to_payload()
    edit(payload)
    with pytest.raises(IdentityError, match=f"^{code}$"):
        CampaignIdentityV1.from_payload(payload)


def test_metric_contract_is_comparable_and_scorer_owned_role_visibility_is_not():
    assert "metric_contract" in COMPARABLE_FIELDS
    assert "metric_contract" in campaign_module._COMPARABLE
    assert "role_visibility" not in COMPARABLE_FIELDS
    assert set(COMPARABLE_FIELDS) == set(campaign_module._COMPARABLE) | {"artifact_store"}
    assert comparable_field("identity.metric_contract")
    assert not comparable_field("identity.role_visibility")
    assert WINDOW_OWNER_OF_FIELD["metric_contract"] == "scorer"
    assert restarted_owners(["identity.metric_contract"]) == frozenset({"scorer"})
    assert restarted_owners(["identity.role_visibility"]) == frozenset()


def test_changing_one_declaration_differs_in_its_field_only():
    base = {"identity": identity(v11_policy(), CONNECTION).to_payload()}
    contract = {
        "identity": identity(v11_policy(), {**CONNECTION, "metric_contract": "e" * 64}).to_payload()
    }
    visibility = {
        "identity": identity(v11_policy(), {**CONNECTION, "role_visibility": "e" * 64}).to_payload()
    }
    changed = identity_differences(base, contract)
    assert [difference.field for difference in changed] == ["identity.metric_contract"]
    assert comparable_changed(changed)
    changed = identity_differences(base, visibility)
    assert [difference.field for difference in changed] == ["identity.role_visibility"]
    assert not comparable_changed(changed)


# -- R18: adaptation profile /7 holds the Grouper identity ----------------------------

FIELDS_IDENTITY = {
    "kind": "fields",
    "features": [
        {
            "name": "terminal",
            "source": "observation",
            "path": "/terminal",
            "description": "Terminal.",
        }
    ],
}
SELECTION = {"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32}
PARTITION = {
    "profile": "corpus_partition/1",
    "mode": "implicit_train",
    "assignments": [["c1", "train"]],
    "corpus_digest": "a" * 64,
    "metadata_digest": "b" * 64,
    "train_digest": "a" * 64,
    "test_digest": "b" * 64,
}


def adaptation(grouping=None, *, profile=FINAL_ADAPTATION_PROFILE, labels=None, reflection=None):
    return build_adaptation_identity(
        grouping=FIELDS_IDENTITY if grouping is None else grouping,
        selection=SELECTION,
        families={"kind": "none"},
        attempt_feedback="sequential",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
        family_membership={},
        profile=profile,
        reflection={"max_consecutive_failures": 2} if reflection is None else reflection,
        corpus_partition=PARTITION if profile == FINAL_ADAPTATION_PROFILE else None,
        provenance_labels=labels,
    )


def chat_identity():
    from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER

    payload = {
        key: value for key, value in CHAT_GROUPER.items() if key not in ("retry", "fallback")
    }
    return {
        **payload,
        "kind": "chat",
        "protocol": "model_grouping/1",
        "proxy_env": None,
        "verify_ssl_certs": True,
        "reasoning_effort": None,
        "provider_only": None,
        "representatives_per_case": 3,
        "retry": CHAT_GROUPER["retry"],
        "prompt_sha256": hashlib.sha256(TREE_GROUPING_MODEL_SYSTEM.encode()).hexdigest(),
        "fallback": None,
    }


def agent_identity():
    return {
        "kind": "agent_cli",
        "protocol": "agent_grouping_request/1",
        "model": "codex",
        "permission": "safe",
        "allowed_env": [],
        "reasoning_effort": None,
        "max_turns": None,
        "max_payload_bytes": 1024 * 1024,
        "timeout_seconds": 600,
        "prompt_sha256": "1" * 64,
        "program_sha256": "2" * 64,
        "fallback": None,
    }


def test_profile_7_binds_the_partition_reflection_and_labels():
    built = adaptation()
    payload = built.to_payload()
    assert built.profile == FINAL_ADAPTATION_PROFILE
    assert payload["corpus_partition"] == PARTITION
    assert payload["reflection"] == {"max_consecutive_failures": 2}
    labelled = adaptation(labels=[["c1", ["skills/**"]]])
    assert labelled.provenance_labels == {"c1": ("skills/**",)}
    assert AdaptationIdentityV1.from_payload(labelled.to_payload()).digest == labelled.digest


# -- #282: `reflection.group_text_chars` (design §4.4, R2) ----------------------------------


@pytest.mark.parametrize("limit", [1, 4096, None])
def test_profile_7_binds_an_optional_group_text_limit(limit):
    built = adaptation(reflection={"max_consecutive_failures": 2, "group_text_chars": limit})
    assert built.to_payload()["reflection"] == {
        "max_consecutive_failures": 2,
        "group_text_chars": limit,
    }
    assert AdaptationIdentityV1.from_payload(built.to_payload()).digest == built.digest
    assert built.digest != adaptation().digest
    assert reflection_group_text_chars(built.to_payload()["reflection"]) == limit


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "512"])
def test_profile_7_refuses_a_bad_group_text_limit(limit):
    with pytest.raises(IdentityError, match="^invalid_identity_integer$"):
        adaptation(reflection={"max_consecutive_failures": 2, "group_text_chars": limit})


def test_profile_7_reflection_stays_closed():
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation(reflection={"max_consecutive_failures": 2, "zz": 1})
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation(reflection={"group_text_chars": 4096})


def test_profile_4_does_not_admit_the_group_text_limit():
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation(
            profile="campaign_adaptation/4",
            reflection={"max_consecutive_failures": 2, "group_text_chars": 4096},
        )


def test_a_journal_without_the_limit_reads_as_512():
    """R2: a `/15` run, and a `/16` run created by 2.0.0b9, journaled no key and ran at 512."""

    import json

    journal = (
        Path(__file__).resolve().parent / "fixtures" / "family14_b2" / "run" / "campaign.jsonl"
    )
    created = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["payload"]
    payload = created["adaptation_payload"]
    assert payload["reflection"] == {"max_consecutive_failures": 2}
    assert AdaptationIdentityV1.from_payload(payload).digest == created["adaptation"]["digest"]
    assert reflection_group_text_chars(payload["reflection"]) == 512


@pytest.mark.parametrize("grouping", [chat_identity(), agent_identity(), FIELDS_IDENTITY])
def test_profile_7_admits_every_grouper_kind(grouping):
    assert adaptation(grouping).to_payload()["grouping"] == grouping


def test_profile_7_refuses_the_model_kind():
    model = {**chat_identity(), "kind": "model"}
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation(model)


@pytest.mark.parametrize(
    "edit",
    [
        lambda g: g.pop("program_sha256"),
        lambda g: g.__setitem__("zz", 1),
        lambda g: g.__setitem__("prompt_sha256", "zz"),
        lambda g: g.__setitem__("timeout_seconds", 9),
        lambda g: g.__setitem__("permission", "root"),
        lambda g: g.__setitem__("allowed_env", ["B", "A"]),
        lambda g: g.__setitem__("protocol", "agent_grouping_request/2"),
        lambda g: g.__setitem__("reasoning_effort", []),
        lambda g: g.__setitem__("reasoning_effort", {}),
    ],
)
def test_the_agent_grouper_slot_is_closed(edit):
    grouping = agent_identity()
    edit(grouping)
    with pytest.raises(IdentityError):
        adaptation(grouping)


def test_profile_7_requires_its_partition():
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        build_adaptation_identity(
            grouping=FIELDS_IDENTITY,
            selection=SELECTION,
            families={"kind": "none"},
            attempt_feedback="sequential",
            reflection_metadata_digest="a" * 64,
            environment_docs_digest=None,
            family_membership={},
            profile=FINAL_ADAPTATION_PROFILE,
            reflection={"max_consecutive_failures": 2},
        )


def test_the_grouping_policy_digest_is_the_grouper_digest():
    config = AdaptiveCampaignRuntimeConfig(
        identity=adaptation(),
        grouping_features=(FieldFeature("terminal", "observation", "/terminal", "Terminal."),),
    )
    assert (
        config.policy_digest
        == hashlib.sha256(
            canonical_json({"version": "reflect_grouping/1", "grouping": FIELDS_IDENTITY})
        ).hexdigest()
    )
    other = {
        **FIELDS_IDENTITY,
        "features": [{**FIELDS_IDENTITY["features"][0], "description": "Other."}],
    }
    changed = AdaptiveCampaignRuntimeConfig(
        identity=adaptation(other),
        grouping_features=(FieldFeature("terminal", "observation", "/terminal", "Other."),),
    )
    assert changed.policy_digest != config.policy_digest


def test_an_agent_grouping_source_is_exactly_one(tmp_path):
    from evolution.tree.roles.config import AgentCliGrouperSection
    from tests.tree.manifest.test_manifest_v15 import AGENT_GROUPER, load, v15_document

    manifest = load(tmp_path, v15_document(grouper=AGENT_GROUPER))
    assert manifest.workflow is not None
    section = manifest.workflow.grouper
    assert isinstance(section, AgentCliGrouperSection)
    config = AdaptiveCampaignRuntimeConfig(
        identity=adaptation(agent_identity()), agent_grouping=section
    )
    assert config.agent_grouping is section and not config.command_grouping
    with pytest.raises(ValueError, match="^agent_grouping_has_features$"):
        AdaptiveCampaignRuntimeConfig(
            identity=adaptation(agent_identity()),
            agent_grouping=section,
            grouping_features=(FieldFeature("terminal", "observation", "/terminal", "T."),),
        )


# -- R18: the Grouper identity builders in evolve ----------------------------------------


def test_a_chat_grouper_identity_is_the_model_payload_under_kind_chat(tmp_path):
    from evolution.tree.evolve import _chat_grouper_identity, _model_grouping_identity
    from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, load, v15_document

    manifest = load(tmp_path, v15_document(grouper=CHAT_GROUPER))
    assert manifest.workflow is not None
    section = manifest.workflow.grouper
    built = _chat_grouper_identity(section)
    assert built["kind"] == "chat"
    assert {**built, "kind": "model"} == _model_grouping_identity(section)
    assert built["prompt_sha256"] == hashlib.sha256(TREE_GROUPING_MODEL_SYSTEM.encode()).hexdigest()
    assert adaptation(built).to_payload()["grouping"] == built


def test_a_vanished_grouper_prompt_is_a_grouper_refusal(tmp_path):
    from evolution.tree.bindings import BindingError
    from evolution.tree.evolve import _chat_grouper_identity
    from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, load, v15_document

    (tmp_path / "group.md").write_text("Group.\n")
    manifest = load(tmp_path, v15_document(grouper={**CHAT_GROUPER, "system_prompt": "group.md"}))
    (tmp_path / "group.md").unlink()
    assert manifest.workflow is not None
    with pytest.raises(BindingError) as caught:
        _chat_grouper_identity(manifest.workflow.grouper)
    assert (caught.value.code, caught.value.path) == ("grouper_invalid", "grouper.system_prompt")


def test_an_agent_grouper_identity_seals_prompt_and_program(tmp_path):
    from evolution.tree.evolve import _agent_cli_grouper_identity
    from tests.tree.manifest.test_manifest_v15 import AGENT_GROUPER, load, v15_document

    (tmp_path / "bin").mkdir()
    program = tmp_path / "bin" / "codex"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    grouper = {**AGENT_GROUPER, "agent_bin": "bin/codex", "allowed_env": ["B_TOKEN", "A_TOKEN"]}
    manifest = load(tmp_path, v15_document(grouper=grouper))
    assert manifest.workflow is not None
    built = _agent_cli_grouper_identity(
        manifest.workflow.grouper, {"A_TOKEN": "a" * 16, "B_TOKEN": "b" * 16}
    )
    assert built["program_sha256"] == hashlib.sha256(program.read_bytes()).hexdigest()
    assert built["prompt_sha256"] == hashlib.sha256(TREE_AGENT_GROUPER_SYSTEM.encode()).hexdigest()
    assert built["allowed_env"] == ["A_TOKEN", "B_TOKEN"]
    assert (built["timeout_seconds"], built["fallback"]) == (600, None)
    assert adaptation(built).to_payload()["grouping"] == built


def test_the_model_grouping_identity_keeps_its_kind_and_vocabulary(tmp_path):
    from evolution.tree.evolve import _model_grouping_identity
    from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, load, v15_document

    # The native chat Grouper shares this identity builder before its kind is
    # changed to chat by _chat_grouper_identity; no retired manifest is loaded.
    manifest = load(tmp_path, v15_document(grouper=CHAT_GROUPER))
    assert manifest.workflow is not None
    assert _model_grouping_identity(manifest.workflow.grouper)["kind"] == "model"


# -- R13: the constant and the reader family ---------------------------------------


def test_family_14_is_a_known_reader_family():
    assert FINAL_CAMPAIGN_FAMILY_VERSION == 14
    assert campaign_family_for(14) is FINAL_CAMPAIGN_FAMILY
    CampaignLifecycle(selector=None, adaptive=True, family_version=14)
    assert campaign_family_for(15) is not FINAL_CAMPAIGN_FAMILY
    with pytest.raises(SchemaError, match="^campaign family is not supported$"):
        campaign_family_for(16)


def _created14(**policy):
    from tests.campaign_drivers import native14_created

    return native14_created(**policy)


def test_a_family_14_creation_validates():
    created = _created14()
    validate_payload("campaign_created", created, final=True)
    campaign_family_for(14).validate_payload("campaign_created", created)
    validate_payload(
        "campaign_created", _created14(round_stop="all_attempts", stability="unstable"), final=True
    )


def _redigest(payload):
    payload["identity_digest"] = hashlib.sha256(canonical_json(payload["identity"])).hexdigest()
    return payload


def test_family_14_binds_both_connection_digests():

    created = _created14()
    for name in ("metric_contract", "role_visibility"):
        del created["identity"][name]
    with pytest.raises(
        SchemaError, match="^family 14 identity binds metric_contract and role_visibility$"
    ):
        validate_payload("campaign_created", _redigest(created), final=True)


def test_family_14_creates_adaptation_profile_7():
    created = _created14()
    created["adaptation"]["version"] = "campaign_adaptation/6"
    with pytest.raises(SchemaError, match="^adaptation slot version$"):
        validate_payload("campaign_created", created, final=True)


def test_the_lifecycle_accepts_a_family_14_creation():
    from evolution.tree.storage.envelope import Envelope

    state = CampaignLifecycle(selector=None, adaptive=True, family_version=14)
    state.accept(
        Envelope.from_payload(
            {
                "schema_version": 1,
                "run_id": "run",
                "sequence": 1,
                "timestamp": "2026-10-09T00:00:00Z",
                "event": "campaign_created",
                "operation_id": None,
                "payload": _created14(),
            },
            family=campaign_family_for(14),
        )
    )
    assert state.snapshot.family_version == 14
    assert state.policy.round_stop == "first_accepted"


# -- R14: the class/profile pairing and the boundary checks ------------------------------


def test_v11_with_profile_7_selects_family_14():
    from evolution.tree.campaign import _family_for_policy_class

    assert _family_for_policy_class(CampaignPolicyV11, "campaign_adaptation/7") == 14


@pytest.mark.parametrize(
    ("selected", "journaled", "code"),
    [
        (14, 13, "policy_v11_not_admitted"),
        (13, 14, "policy_v11_not_admitted"),
        (14, 12, "policy_v10_not_admitted"),
        (12, 14, "policy_v10_not_admitted"),
    ],
)
def test_a_misfiled_family_14_policy_is_malformed_evidence(selected, journaled, code):
    from evolution.tree.campaign import PolicyNotAdmitted, _refuse_misfiled

    with pytest.raises(PolicyNotAdmitted, match=f"^{code}$"):
        _refuse_misfiled(selected, journaled)


def test_matching_families_pass_the_misfiled_check():
    from evolution.tree.campaign import _refuse_misfiled

    _refuse_misfiled(14, 14)
    _refuse_misfiled(14, None)


def test_a_v11_policy_drives_family_14_only():
    from evolution.tree.campaign import _admit_runtime_policy

    _admit_runtime_policy(v11_policy(), 14)
    for policy, family, code in (
        (v11_policy(), 13, "adaptive_runtime_policy_v11_family_mismatch"),
    ):
        with pytest.raises(ValueError, match=f"^{code}$"):
            _admit_runtime_policy(policy, family)


# -- M1: /15 does not run without the test-only helper -------------------------------------


CONNECTION_CONTRACT = ConnectionContract(MetricContract("pointwise", ()), RoleVisibility(()))


def test_the_verbatim_answers_field_is_admitted_beside_the_connection_fields_only():
    from tests.family14_b2_scenario import journal_records

    fixture = Path(__file__).resolve().parent / "fixtures" / "family14_b2" / "run"
    payload = journal_records(fixture)[0]["payload"]["identity"]
    with_key = {**payload, "verbatim_answers": "a" * 64}
    assert CampaignIdentityV1(with_key).to_payload()["verbatim_answers"] == "a" * 64
    legacy = {k: v for k, v in payload.items() if k not in ("metric_contract", "role_visibility")}
    with pytest.raises(IdentityError, match="invalid_identity_fields"):
        CampaignIdentityV1({**legacy, "verbatim_answers": "a" * 64})
    with pytest.raises(IdentityError):
        CampaignIdentityV1({**payload, "verbatim_answers": "not-a-digest"})
    assert "verbatim_answers" not in CampaignIdentityV1(payload).to_payload()
    assert CampaignIdentityV1(payload).digest == hashlib.sha256(canonical_json(payload)).hexdigest()


def adaptive7(grouping=None):
    return AdaptiveCampaignRuntimeConfig(
        identity=adaptation(grouping),
        grouping_features=(FieldFeature("terminal", "observation", "/terminal", "Terminal."),),
    )


def test_the_runtime_cap_admits_a_v11_policy_and_a_lower_cap_refuses_it(tmp_path, monkeypatch):
    from evolution.tree.campaign import _admit_active_runtime, run_campaign

    assert campaign_module._ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION >= 14
    _admit_active_runtime(v11_policy(), adaptive7(), CONNECTION_CONTRACT)
    assert campaign_module._runtime_family_version(v11_policy(), adaptive7()) == 14
    monkeypatch.setattr(campaign_module, "_ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION", 13)
    with pytest.raises(ValueError, match="^adaptive_runtime_policy_v11_family_mismatch$"):
        _admit_active_runtime(v11_policy(), adaptive7(), CONNECTION_CONTRACT)
    root = tmp_path / "run"
    with pytest.raises(ValueError, match="^adaptive_runtime_policy_v11_family_mismatch$"):
        asyncio.run(
            run_campaign(
                v11_policy(),
                run_root=root,
                seed_root=tmp_path,
                artifact_policy=None,  # type: ignore[arg-type]
                adapter=None,  # type: ignore[arg-type]
                environment=None,  # type: ignore[arg-type]
                scorer=None,  # type: ignore[arg-type]
                editor=None,  # type: ignore[arg-type]
                reflector=None,  # type: ignore[arg-type]
                corpus=(),
                adaptive=adaptive7(),
                connection=CONNECTION_CONTRACT,
            )
        )
    assert not root.exists()


def test_family14_admitted_marks_the_probe_and_asserts_the_gates(monkeypatch):
    from evolution.tree import evolve as evolve_module
    from evolution.tree.campaign import _admit_active_runtime
    from evolution.tree.manifest.manifest_v2 import SCHEMA_V15

    family14_admitted(monkeypatch)
    first = campaign_module.run_probe
    family14_admitted(monkeypatch)
    assert campaign_module.run_probe is first
    assert evolve_module.CAMPAIGN_SCHEMAS.count(SCHEMA_V15) == 1
    assert getattr(campaign_module.run_probe, "marks_probing", False)
    _admit_active_runtime(v11_policy(), adaptive7(), CONNECTION_CONTRACT)


def test_family_14_requires_its_connection(monkeypatch):
    from evolution.tree.campaign import _admit_active_runtime

    family14_admitted(monkeypatch)
    with pytest.raises(ValueError, match="^connection_contract_required$"):
        _admit_active_runtime(v11_policy(), adaptive7(), None)


def test_an_agent_cli_grouper_is_admitted_at_cap_14_and_refused_below(tmp_path, monkeypatch):
    from evolution.tree.campaign import _admit_active_runtime
    from evolution.tree.roles.config import AgentCliGrouperSection
    from tests.tree.manifest.test_manifest_v15 import AGENT_GROUPER, load, v15_document

    manifest = load(tmp_path, v15_document(grouper=AGENT_GROUPER))
    assert manifest.workflow is not None
    section = manifest.workflow.grouper
    assert isinstance(section, AgentCliGrouperSection)
    config = AdaptiveCampaignRuntimeConfig(
        identity=adaptation(agent_identity()), agent_grouping=section
    )
    # This fixture exercises the historical cap-14 boundary explicitly.
    monkeypatch.setattr(campaign_module, "_ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION", 14)
    _admit_active_runtime(v11_policy(), config, CONNECTION_CONTRACT)
    assert campaign_module._runtime_family_version(v11_policy(), config) == 14
    monkeypatch.setattr(campaign_module, "_ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION", 13)
    with pytest.raises(ValueError, match="^adaptive_runtime_policy_v11_family_mismatch$"):
        _admit_active_runtime(v11_policy(), config, CONNECTION_CONTRACT)


# -- R16: capacity ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stability", "round_stop", "max_instances", "admitted"),
    [
        ("stable", "first_accepted", 1, True),  # #160: one instance
        ("stable", "all_attempts", 2, False),  # K = 3 finalists at once
        ("stable", "all_attempts", 3, True),
        ("unstable", "first_accepted", 1, False),  # Champion + 1 finalist
        ("unstable", "first_accepted", 2, True),
        ("unstable", "all_attempts", 3, False),  # Champion + K
        ("unstable", "all_attempts", 4, True),
    ],
)
def test_a_v11_policy_needs_its_final_measurement_capacity(
    stability, round_stop, max_instances, admitted
):
    policy = v11_policy(
        stability=stability,
        round_stop=round_stop,
        attempts_per_round=3,
        parallel_attempts=1,
        max_concurrency=2,
    )
    capabilities = AdapterCapabilities(
        "exact", 2, True, frozenset({"artifact_root_injection"}), max_instances=max_instances
    )
    if admitted:
        admit_capabilities(policy, capabilities)
    else:
        with pytest.raises(CapabilitiesInsufficient, match="capabilities_insufficient"):
            admit_capabilities(policy, capabilities)


def test_a_fresh_parent_keeps_the_candidate_gates_need():
    policy = v11_policy(
        fresh_parent=True, attempts_per_round=3, parallel_attempts=1, max_concurrency=2
    )
    capabilities = AdapterCapabilities(
        "exact", 2, True, frozenset({"artifact_root_injection"}), max_instances=1
    )
    with pytest.raises(CapabilitiesInsufficient):
        admit_capabilities(policy, capabilities)


# -- H10 (F0 R59, choice 14): the measurement identity's connection marker ----------

#: `derived_identity(identity(policy), "a" * 64, side).digest` for the V9 (family 12),
#: V10 (family 13) and V11 (family 14) policies, both sides, computed at the H10 base
#: 162395be1 from the pristine export (`.superpowers/h10-t4/base_digests.py`).
BASE_MEASUREMENT_DIGEST = "21e5e9b0fcefdd1218dfb4de7fe52e900160dc8c03b76034798a8902ed8d49ad"


@pytest.mark.parametrize("side", ["champion", "candidate"])
def test_a_family_14_measurement_identity_carries_the_connection_marker(side):
    from evolution.tree.campaign_identity import derived_identity
    from evolution.tree.execution.journal_identity import RunIdentityV1

    derived = derived_identity(identity(v11_policy(), CONNECTION), "a" * 64, side)
    assert derived.connection_binding is True and derived.row_provenance is True
    assert derived.digest != BASE_MEASUREMENT_DIGEST
    # The marker is the whole change: without it the base bytes come back.
    payload = derived.to_payload()
    assert payload.pop("connection_binding") == 1
    assert RunIdentityV1.from_payload(payload).digest == BASE_MEASUREMENT_DIGEST


# -- #290 §4.2, C5: a Grouper without `api_key_env` leaves the name out ------------

#: Computed at `d5daf1d5d`, before #290 Task 4: the `CHAT_GROUPER` section that
#: declares `GROUPER_API_KEY` keeps its Grouper payload and adaptation digest.
DECLARED_GROUPER_DIGESTS = {
    "grouper": "fd0979b1a99fb81c518a37e2c72d0190bd86454fcc0411702bfc00dac409a4aa",
    "model": "2d63dc885b7ad9fd4456f59ed971baabc1a623d42fb8f744447094ca1a04e408",
    "adaptation": "b7aeb4127b80f2cca822461d3e6ef800d9ae510e5c270754b3452e0997c163b8",
}


def _grouper_section(tmp_path, **overrides):
    from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, load, v15_document

    grouper = {**CHAT_GROUPER, **overrides}
    grouper = {key: value for key, value in grouper.items() if value is not None}
    manifest = load(tmp_path, v15_document(grouper=grouper))
    assert manifest.workflow is not None
    return manifest.workflow.grouper


def _sha(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def test_a_declared_grouper_key_keeps_its_identity_digests(tmp_path):
    from evolution.tree.evolve import _chat_grouper_identity, _model_grouping_identity

    section = _grouper_section(tmp_path)
    built = _chat_grouper_identity(section)
    assert built["api_key_env"] == "GROUPER_API_KEY"
    assert {
        "grouper": _sha(built),
        "model": _sha(_model_grouping_identity(section)),
        "adaptation": adaptation(built).digest,
    } == DECLARED_GROUPER_DIGESTS


def test_an_undeclared_grouper_key_is_left_out_and_admitted(tmp_path):
    from evolution.tree.evolve import _chat_grouper_identity, _model_grouping_identity

    declared = _chat_grouper_identity(_grouper_section(tmp_path / "declared"))
    section = _grouper_section(tmp_path / "bare", api_key_env=None)
    assert section.api_key_env is None
    built = _chat_grouper_identity(section)

    assert "api_key_env" not in built
    assert "api_key_env" not in _model_grouping_identity(section)
    assert built == {key: value for key, value in declared.items() if key != "api_key_env"}
    built_identity = adaptation(built)
    assert built_identity.to_payload()["grouping"] == built
    assert AdaptationIdentityV1.from_payload(built_identity.to_payload()).digest == (
        built_identity.digest
    )
    assert built_identity.digest != adaptation(declared).digest


@pytest.mark.parametrize("value", [None, "", "  ", 7, "A=B"], ids=repr)
def test_a_present_grouper_key_must_still_be_a_name(value):
    grouping = {**chat_identity(), "api_key_env": value}
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation(grouping)


def test_the_omission_is_the_only_new_shape_and_the_slot_stays_closed():
    bare = {key: value for key, value in chat_identity().items() if key != "api_key_env"}
    assert adaptation(bare).to_payload()["grouping"] == bare
    for missing in ("proxy_env", "model", "base_url"):
        with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
            adaptation({key: value for key, value in bare.items() if key != missing})
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        adaptation({**bare, "zz": 1})
