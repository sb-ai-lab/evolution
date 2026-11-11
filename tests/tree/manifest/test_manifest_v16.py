"""/16 is admitted independently while /15 retains its exact identity."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from evolution.tree import campaign, campaign_policy, evolve
from evolution.tree.cli import tree_app
from evolution.tree.manifest import manifest_v2
from evolution.tree.manifest.manifest_inventory import manifest_dependencies
from tests.family15_fakes import v16_document
from tests.test_tree_campaign_policy_v12 import change, payload
from tests.tree.manifest.test_manifest_v2 import write_v5_document
from tests.tree.manifest.test_manifest_v15 import (
    COMMAND_GROUPER,
    INTERNAL_HTTP_URLS,
    chat_roles_document,
    load,
    refused,
    role_section,
    v15_document,
)


def test_v16_loads_connection_grouper_and_policy_and_dependency_inventory(tmp_path):
    manifest = load(tmp_path, v16_document())
    assert hasattr(manifest_v2, "SCHEMA_V16")
    assert manifest.schema == manifest_v2.SCHEMA_V16
    assert isinstance(manifest.workflow.campaign, campaign_policy.CampaignPolicyV12)
    assert manifest.workflow.connection is not None
    inventory = manifest_dependencies(manifest)
    assert inventory.schema == manifest.schema
    assert "declared_spellings_unavailable" not in inventory.boundary
    assert manifest.workflow.grouper is not None


@pytest.mark.parametrize("minimum", [1, 3])
def test_minimum_claim_setting_is_bound_in_adaptation(tmp_path, minimum):
    document = chat_roles_document(v16_document())
    baseline = _role_adaptation(load(tmp_path, document), tmp_path)
    document["reflector"]["selection"]["min_claim_cases"] = minimum
    changed = _role_adaptation(load(tmp_path, document), tmp_path)
    assert changed.min_claim_cases == minimum
    assert changed.digest != baseline.digest


@pytest.mark.parametrize(
    ("minimum", "code"), [(0, "bad_value"), (True, "bad_type"), ("2", "bad_type")]
)
def test_minimum_claim_refuses_invalid_values(tmp_path, minimum, code):
    document = v16_document()
    document["reflector"]["selection"]["min_claim_cases"] = minimum
    assert refused(tmp_path, document)[:2] == (code, "reflector.selection.min_claim_cases")


def test_v15_refuses_minimum_claim(tmp_path):
    document = v15_document()
    document["reflector"]["selection"]["min_claim_cases"] = 1
    assert refused(tmp_path, document)[:2] == ("unknown_key", "reflector.selection.min_claim_cases")


def test_read_only_policy_dispatch_selects_family15_before_parent_classes(tmp_path):
    policy = load(tmp_path, v16_document()).workflow.campaign
    assert campaign._family_for_policy_class(type(policy), "campaign_adaptation/7") == 15
    assert campaign_policy.policy_class_for_payload(policy.to_payload()) is type(policy)


def test_real_evolve_and_library_admit_v16_without_creating_a_run(tmp_path):
    path = write_v5_document(tmp_path, v16_document())
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert evolve.admit_manifest(path).schema == manifest_v2.SCHEMA_V16
    campaign._refuse_unadmitted_family(15)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_v15_policy_keeps_its_exact_payload_and_digest(tmp_path):
    document = v15_document()
    policy = load(tmp_path, document).workflow.campaign
    assert type(policy) is campaign_policy.CampaignPolicyV11
    expected = document["campaign"]
    expected["sample_retries"] = 1
    for threshold in expected["indeterminate"].values():
        threshold["max_rate"] = "0.500000000000"
    assert policy.to_payload() == expected
    # Captured from the unchanged /15 parser at the implementation baseline.
    assert policy.digest == "917d531a4d50c241d9182c48d7d20345e15f4682ce9837139aa75b4ae580df9f"


def _v12_identity(policy):
    from evolution.tree.campaign_identity import CampaignIdentityV1
    from tests.campaign_drivers import native14_created

    created = native14_created(round_stop="all_attempts")
    identity = created["identity"]
    identity.update(policy=policy.to_payload(), policy_digest=policy.digest)
    return CampaignIdentityV1.from_payload(identity)


def test_identity_round_trip_binds_every_new_policy_field(tmp_path):
    from evolution.tree.campaign_identity import CampaignIdentityV1

    document = v16_document()
    policy = load(tmp_path, document).workflow.campaign
    identity = _v12_identity(policy)
    assert identity.to_payload()["policy"] == policy.to_payload()
    assert CampaignIdentityV1.from_payload(identity.to_payload()).digest == identity.digest
    document["campaign"]["gate"]["verification"] = "fresh"
    other = load(tmp_path, document).workflow.campaign
    assert _v12_identity(other).digest != identity.digest


def test_existing_envelope_secret_screen_refuses_noise_source_in_bound_identity(tmp_path):
    from evolution.tree.campaign import secret_location
    from evolution.tree.campaign_identity import CampaignIdentityV1
    from evolution.tree.storage.envelope import Envelope, EnvelopeFamily, SchemaError

    document = v16_document()
    document["campaign"]["gate"]["execution_noise"] = {
        "kind": "bounded",
        "allowance": 0.1,
        "source": "Calibration PRIVATE-CANARY",
    }
    identity = _v12_identity(load(tmp_path, document).workflow.campaign)

    # Exercise the existing
    # envelope's secret validation with a real, validated CampaignIdentityV1.
    def validate_identity(event, value):
        assert event == "identity_checked"
        CampaignIdentityV1.from_payload(value["identity"])

    family = EnvelopeFamily(
        validate_identity, frozenset({"identity_checked"}), lambda _event: False
    )
    record = {
        "schema_version": 1,
        "run_id": "run",
        "sequence": 1,
        "timestamp": "2026-10-18T00:00:00Z",
        "event": "identity_checked",
        "operation_id": None,
        "payload": {"identity": identity.to_payload()},
    }
    assert Envelope.from_payload(record, family=family).payload["identity"] == identity.to_payload()
    with pytest.raises(SchemaError, match="^invalid journal record$"):
        Envelope.from_payload(record, family=family, secrets=["PRIVATE-CANARY"])
    assert (
        secret_location(record, ["PRIVATE-CANARY"]) == "identity.policy.gate.execution_noise.source"
    )


def test_family15_policy_cannot_be_misfiled_as_family14():
    with pytest.raises(campaign.PolicyNotAdmitted, match="policy_v12_not_admitted"):
        campaign._refuse_misfiled(15, 14)
    with pytest.raises(campaign.PolicyNotAdmitted, match="policy_v12_not_admitted"):
        campaign._refuse_misfiled(14, 15)


@pytest.mark.parametrize(
    "path",
    (
        "gate.mode",
        "gate.causal.tolerance",
        "gate.verification",
        "gate.execution_noise.kind",
    ),
)
def test_loader_preserves_campaign_prefix_and_recommendation_in_refusals(tmp_path, path):
    document = v16_document()
    section = document["campaign"]
    parts = path.split(".")
    for key in parts[:-1]:
        section = section[key]
    del section[parts[-1]]
    with pytest.raises(manifest_v2.ManifestError) as caught:
        load(tmp_path, document)
    assert (caught.value.code, caught.value.detail) == ("missing_key", f"campaign.{path}")
    assert caught.value.expected


@pytest.mark.parametrize("value", ["automatic", "manual"])
def test_campaign_promotion_is_refused_as_an_unknown_key(tmp_path, value):
    document = v16_document()
    document["campaign"]["promotion"] = value
    with pytest.raises(manifest_v2.ManifestError) as caught:
        load(tmp_path, document)
    assert (caught.value.code, caught.value.detail) == ("unknown_key", "campaign.promotion")


def test_campaign_promotion_is_refused_before_evolve_creates_a_run(tmp_path):
    document = v16_document()
    document["campaign"]["promotion"] = "automatic"
    path = write_v5_document(tmp_path, document)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(evolve.EvolveRefused) as caught:
        evolve.admit_manifest(path)
    assert (caught.value.code, caught.value.path) == ("unknown_key", "campaign.promotion")
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_both_real_writer_gates_are_open_together():
    assert evolve.CAMPAIGN_SCHEMAS == (manifest_v2.SCHEMA_V15, manifest_v2.SCHEMA_V16)
    assert campaign._ADMITTED_CAMPAIGN_FAMILIES == frozenset({14, 15})


@pytest.mark.parametrize("role", ["editor", "reflector"])
def test_v16_chat_role_loads_without_api_key_env(tmp_path, role):
    from tests.tree.manifest.test_manifest_v15 import chat_document

    document = v16_document()
    chat = chat_document(context_compact_chars=800000, context_message_max_chars=64000)
    document[role] = chat[role]
    document[role].pop("api_key_env")
    workflow = load(tmp_path, document).workflow
    section = workflow.editor if role == "editor" else workflow.reflector.role
    assert section.api_key_env is None


@pytest.mark.parametrize("base_url", INTERNAL_HTTP_URLS)
@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_v16_chat_role_accepts_plain_http_to_any_host(tmp_path, role, base_url):
    """`/16` admits the same internal `http` endpoints as `/15`."""

    document = chat_roles_document(v16_document())
    document[role]["base_url"] = base_url
    manifest = load(tmp_path, document)
    assert manifest.schema == manifest_v2.SCHEMA_V16
    assert role_section(manifest, role).base_url == base_url


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_v16_chat_role_still_refuses_another_scheme(tmp_path, role):
    document = chat_roles_document(v16_document())
    document[role]["base_url"] = "ftp://llm.internal/v1"
    assert refused(tmp_path, document)[:2] == ("bad_value", f"{role}.base_url")


@pytest.mark.parametrize(
    "mode,path,value,code,help_word",
    [
        ("statistical", "evaluation.repeats_per_question", 1, "bad_value", "statistical"),
        ("point", "gate.causal.tolerance", "measured_noise", "bad_value", "measured_noise"),
        ("confidence", "gate.causal.tolerance", "measured_noise", "bad_value", "measured_noise"),
        ("unfiltered", "gate.causal.tolerance", "measured_noise", "bad_value", "measured_noise"),
        ("point", "gate.bound_fallback", "point", "bad_value", "confidence"),
        ("statistical", "gate.bound_fallback", "point", "bad_value", "confidence"),
        ("unfiltered", "gate.bound_fallback", "point", "bad_value", "confidence"),
        ("statistical", "gate.statistics", {}, "unknown_key", "statistics"),
    ],
)
def test_new_gate_refusals_have_help_before_any_run_directory(
    tmp_path, mode, path, value, code, help_word
):
    document = v16_document()
    document["campaign"] = payload(mode)
    if mode == "unfiltered":
        document["campaign"]["evaluation"]["repeats_per_question"] = 2
    change(document["campaign"], path, value)
    manifest_path = write_v5_document(tmp_path, document)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(evolve.EvolveRefused) as caught:
        evolve.admit_manifest(manifest_path)
    assert (caught.value.code, caught.value.path) == (code, f"campaign.{path}")
    assert help_word in caught.value.expected
    result = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest_path), "--run-dir", str(tmp_path / "run")],
    )
    assert result.exit_code == 2
    assert f"{code} at campaign.{path}" in result.output
    assert f"evo tree evolve: {caught.value.expected}\n" in result.output
    assert help_word in result.output
    assert not (tmp_path / "run").exists()
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "mode,tolerance,fallback",
    [
        ("statistical", 0, None),
        ("statistical", "measured_noise", None),
        ("point", "measured_noise", None),
        ("confidence", "measured_noise", None),
        ("confidence", 0, "point"),
        ("confidence", "measured_noise", "point"),
    ],
)
def test_manifest_admits_explicit_gate_options(tmp_path, mode, tolerance, fallback):
    document = v16_document()
    document["campaign"] = payload(mode)
    document["campaign"]["evaluation"]["repeats_per_question"] = 10
    gate = document["campaign"]["gate"]
    gate["causal"]["tolerance"] = tolerance
    if fallback is not None:
        gate["bound_fallback"] = fallback
    policy = load(tmp_path, document).workflow.campaign
    assert policy.to_payload() == document["campaign"]
    assert policy.bound_fallback == fallback


# --: `reflector.group_text_chars` in /16 (ADR-0047) -----------------------------------


@pytest.mark.parametrize("value", [1, 4096, None])
def test_a_v16_reflector_declares_its_group_text_limit(tmp_path, value):
    document = v16_document()
    document["reflector"]["group_text_chars"] = value
    reflector = load(tmp_path, document).workflow.reflector
    assert (reflector.group_text_chars, reflector.group_text_chars_declared) == (value, True)
    assert manifest_v2.effective_group_text_chars(reflector, manifest_v2.SCHEMA_V16) == value


def test_an_absent_v16_group_text_limit_is_4096(tmp_path):
    reflector = load(tmp_path, v16_document()).workflow.reflector
    assert reflector.group_text_chars_declared is False
    assert manifest_v2.effective_group_text_chars(reflector, manifest_v2.SCHEMA_V16) == 4096


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (0, "bad_value"),
        (-1, "bad_value"),
        (1.5, "bad_type"),
        ("512", "bad_type"),
        (True, "bad_type"),
    ],
)
def test_a_bad_v16_group_text_limit_is_told_its_domain(tmp_path, value, code):
    from evolution.tree.manifest.manifest_v15_help import REFLECTOR_GROUP_TEXT_CHARS

    document = v16_document()
    document["reflector"]["group_text_chars"] = value
    with pytest.raises(manifest_v2.ManifestError) as caught:
        load(tmp_path, document)
    error = caught.value
    assert (error.code, error.detail, error.expected) == (
        code,
        "reflector.group_text_chars",
        REFLECTOR_GROUP_TEXT_CHARS,
    )


DECLARED_PATHS = {"version": "secret_oracle_paths/1", "pointers": ["/reference"]}


def test_v16_admits_declared_secret_oracle_paths(tmp_path):
    workflow = load(tmp_path, {**v16_document(), "secret_oracle_paths": DECLARED_PATHS}).workflow
    assert workflow.secret_oracle_paths.pointers == ("/reference",)
    assert workflow.connection.secret_oracle_paths == workflow.secret_oracle_paths


@pytest.mark.parametrize("pointers", [[], [""], ["/" + "a" * 1023, *[f"/k{i}" for i in range(63)]]])
def test_v16_admits_empty_whole_and_inclusive_secret_oracle_paths(tmp_path, pointers):
    workflow = load(
        tmp_path,
        {**v16_document(), "secret_oracle_paths": {**DECLARED_PATHS, "pointers": pointers}},
    ).workflow
    assert workflow.secret_oracle_paths.pointers == tuple(pointers)


def test_without_the_key_no_secret_oracle_paths_are_declared(tmp_path):
    workflow = load(tmp_path, v16_document()).workflow
    assert workflow.secret_oracle_paths is None and workflow.connection.secret_oracle_paths is None


@pytest.mark.parametrize(
    "value,code,detail",
    [
        (["/reference"], "bad_type", "secret_oracle_paths"),
        ({"pointers": []}, "missing_key", "secret_oracle_paths.version"),
        ({"version": "secret_oracle_paths/1"}, "missing_key", "secret_oracle_paths.pointers"),
        ({**DECLARED_PATHS, "strings": []}, "unknown_key", "secret_oracle_paths.strings"),
        ({**DECLARED_PATHS, "version": 1}, "bad_type", "secret_oracle_paths.version"),
        (
            {**DECLARED_PATHS, "version": "secret_oracle_paths/2"},
            "bad_value",
            "secret_oracle_paths.version",
        ),
        ({**DECLARED_PATHS, "pointers": "/reference"}, "bad_type", "secret_oracle_paths.pointers"),
        (
            {**DECLARED_PATHS, "pointers": [f"/k{i}" for i in range(65)]},
            "bad_value",
            "secret_oracle_paths.pointers",
        ),
        ({**DECLARED_PATHS, "pointers": ["/a", 7]}, "bad_type", "secret_oracle_paths.pointers[1]"),
        (
            {**DECLARED_PATHS, "pointers": ["reference"]},
            "bad_value",
            "secret_oracle_paths.pointers[0]",
        ),
        ({**DECLARED_PATHS, "pointers": ["/a~2"]}, "bad_value", "secret_oracle_paths.pointers[0]"),
        (
            {**DECLARED_PATHS, "pointers": ["/" + "a" * 1024]},
            "bad_value",
            "secret_oracle_paths.pointers[0]",
        ),
        (
            {**DECLARED_PATHS, "pointers": ["/" + "я" * 512]},
            "bad_value",
            "secret_oracle_paths.pointers[0]",
        ),
        (
            {**DECLARED_PATHS, "pointers": ["/a", "/a"]},
            "bad_value",
            "secret_oracle_paths.pointers[1]",
        ),
        (
            {**DECLARED_PATHS, "pointers": ["/\ud800"]},
            "bad_value",
            "secret_oracle_paths.pointers[0]",
        ),
    ],
)
def test_secret_oracle_path_shape_refusals_name_the_path_never_the_value(
    tmp_path, value, code, detail
):
    from evolution.tree.manifest.manifest_v15_help import SECRET_ORACLE_PATHS_EXPECTED

    got_code, got_detail, expected = refused(
        tmp_path, {**v16_document(), "secret_oracle_paths": value}
    )
    assert (got_code, got_detail) == (code, detail)
    assert expected == SECRET_ORACLE_PATHS_EXPECTED
    assert "reference" not in got_detail


def test_v16_admits_evidence(tmp_path):
    workflow = load(
        tmp_path,
        {
            **v16_document(),
            "evidence": {
                "version": "evidence/1",
                "view": "allowlist",
            },
        },
    ).workflow
    assert workflow.evidence.view == "allowlist"


def test_v16_refuses_a_bad_evidence_member_with_its_path(tmp_path):
    error = refused(
        tmp_path,
        {
            **v16_document(),
            "evidence": {
                "version": "evidence/1",
                "turns": 0,
            },
        },
    )
    assert error[:2] == ("bad_value", "evidence.turns")
    assert not (tmp_path / "run").exists()


def test_without_evidence_the_workflow_keeps_it_undeclared(tmp_path):
    assert load(tmp_path, v16_document()).workflow.evidence is None


def test_v16_refuses_a_bad_mode_before_writes(tmp_path):
    error = refused(
        tmp_path,
        {
            **v16_document(),
            "checks": {
                "version": "checks/1",
                "modes": {"seal_oracle_leak": "warn"},
            },
        },
    )
    assert error[:2] == ("bad_value", "checks.modes.seal_oracle_leak")
    assert not (tmp_path / "run").exists()


ROLE_LOOP_KEYS = (
    ("editor", "max_protocol_repairs", 0),
    ("editor", "max_consecutive_tool_failures", 1),
    ("reflector", "max_protocol_repairs", 0),
    ("reflector", "max_consecutive_tool_failures", 1),
    ("reflector", "max_consecutive_skips", 1),
    ("grouper", "max_repairs", 0),
    ("grouper", "max_consecutive_full_fallbacks", 1),
)


def test_v16_admits_every_role_loop_key(tmp_path):
    document = chat_roles_document(v16_document())
    for section, key, minimum in ROLE_LOOP_KEYS:
        document[section][key] = minimum
    workflow = load(tmp_path, document).workflow
    for section, key, minimum in ROLE_LOOP_KEYS:
        value = getattr(workflow, section)
        if section == "reflector" and key != "max_consecutive_skips":
            value = value.role
        assert getattr(value, key) == minimum


@pytest.mark.parametrize("section,key,minimum", ROLE_LOOP_KEYS)
@pytest.mark.parametrize(
    "value,code",
    [(True, "bad_type"), (None, "bad_type"), (1.5, "bad_type"), ("below", "bad_value")],
)
def test_a_role_loop_key_below_its_minimum_is_bad_value(
    tmp_path, section, key, minimum, value, code
):
    document = chat_roles_document(v16_document())
    document[section][key] = minimum - 1 if value == "below" else value
    assert refused(tmp_path, document)[:2] == (code, f"{section}.{key}")


@pytest.mark.parametrize("section,key,minimum", ROLE_LOOP_KEYS)
def test_role_loop_counts_have_no_upper_bound(tmp_path, section, key, minimum):
    document = chat_roles_document(v16_document())
    document[section][key] = 10**30
    workflow = load(tmp_path, document).workflow
    value = getattr(workflow, section)
    if section == "reflector" and key != "max_consecutive_skips":
        value = value.role
    assert getattr(value, key) == 10**30


def test_role_loop_effective_defaults_and_explicit_zero(tmp_path):
    from evolution.tree.manifest.manifest_v2 import effective_full_fallbacks, effective_skips
    from evolution.tree.roles.config import (
        effective_grouper_repairs,
        effective_protocol_repairs,
        effective_tool_failures,
    )

    document = chat_roles_document(v16_document())
    workflow = load(tmp_path, document).workflow
    assert effective_protocol_repairs(workflow.editor) == 2
    assert effective_tool_failures(workflow.editor) == 5
    assert effective_grouper_repairs(workflow.grouper) == 2
    assert effective_full_fallbacks(workflow.grouper) == 2
    assert effective_skips(workflow.reflector) == 2
    document["editor"]["max_protocol_repairs"] = 0
    document["grouper"]["max_repairs"] = 0
    workflow = load(tmp_path, document).workflow
    assert effective_protocol_repairs(workflow.editor) == 0
    assert effective_grouper_repairs(workflow.grouper) == 0


@pytest.mark.parametrize(
    "grouper",
    [
        COMMAND_GROUPER,
        {
            "kind": "fields",
            "features": [
                {"name": "kind", "source": "case", "path": "/kind", "description": "kind"}
            ],
        },
        {"kind": "agent_cli", "model": "codex", "timeout_seconds": 600},
    ],
)
def test_v16_every_grouper_kind_accepts_the_full_fallback_limit(tmp_path, grouper):
    document = v16_document()
    document["grouper"] = {**grouper, "max_consecutive_full_fallbacks": 3}
    assert load(tmp_path, document).workflow.grouper.max_consecutive_full_fallbacks == 3


def test_agent_grouper_refuses_chat_repairs(tmp_path):
    document = v16_document()
    document["grouper"] = {
        "kind": "agent_cli",
        "model": "codex",
        "timeout_seconds": 600,
        "max_repairs": 3,
    }
    assert refused(tmp_path, document)[:2] == ("unknown_key", "grouper.max_repairs")


def _role_adaptation(manifest, root):
    from evolution.tree.evolve import _adaptive_config

    partition = {
        "profile": "corpus_partition/1",
        "mode": "implicit_train",
        "assignments": [["c1", "train"]],
        **{
            key: "a" * 64
            for key in ("corpus_digest", "metadata_digest", "train_digest", "test_digest")
        },
    }
    workflow = manifest.workflow
    return _adaptive_config(
        workflow,
        corpus=[],
        reflection_metadata={},
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
        seed_root=manifest.seed_root,
        run_dir=root / "run",
        environ={workflow.grouper.api_key_env: "offline-test-value"},
        final_adaptation={"corpus_partition": partition},
        schema=manifest.schema,
    ).identity


@pytest.mark.parametrize(
    "schema,editor_digest,reflector_digest,reflection_digest",
    [
        (
            "15",
            "2a0aa67e937030043387e413dd9f9f05e7b1bf26f935394b2e04425fa8a5a32c",
            "f5522275bdd1e5f763b0fe64847ece2af19f8ba74a97b05ca60850ecded65eb2",
            "8b7996b036ac094915c6434f812a30ef97f7b0703b863dce767a0954aed32d8e",
        ),
        (
            "16",
            # b17 bound role_contracts/1 in /16 chat identities.
            # b18: role_contracts/2 (ADR-0061)
            "f9f23a1f62890025c0f39e7d1c3f2da4f7aea4700e4049f6108a31a601b9aa68",
            # b18: role_contracts/2 (ADR-0061)
            "ac4fb162fcaf8d91346c01fe2dd5ccc393b8dcfc7baaf80a2f7100ac944d7ff4",
            "86246b9b3ffa57dcf6b755364ee67092d74ee96fb7c12941cdbb665c4008c531",
        ),
    ],
)
def test_absent_role_loop_keys_keep_role_and_adaptation_digests(
    tmp_path, schema, editor_digest, reflector_digest, reflection_digest
):
    import hashlib
    from pathlib import Path

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.seals import FileSeal, chat_identity, chat_reflector_identity

    manifest = load(
        tmp_path, chat_roles_document(v15_document() if schema == "15" else v16_document())
    )
    workflow = manifest.workflow
    prompt = FileSeal(Path("unused"), "a" * 64)
    assert (
        chat_identity(
            workflow.editor, prompt, plan_correction=schema == "16", family15=schema == "16"
        ).digest
        == editor_digest
    )
    assert (
        chat_reflector_identity(
            workflow.reflector.role, prompt, result_repair=schema == "16", family15=schema == "16"
        ).digest
        == reflector_digest
    )
    adaptation = _role_adaptation(manifest, tmp_path).to_payload()
    # /16 records `model_grouping/2` and the packaged `_v3` prompt (b17,
    # ADR-0057), so only /15 keeps the earlier Grouper digest.
    assert hashlib.sha256(canonical_json(adaptation["grouping"])).hexdigest() == (
        "fd0979b1a99fb81c518a37e2c72d0190bd86454fcc0411702bfc00dac409a4aa"
        if schema == "15"
        else V16_GROUPING_DIGEST
    )
    assert hashlib.sha256(canonical_json(adaptation["reflection"])).hexdigest() == reflection_digest


#: The undeclared `/16` chat Grouper's adaptation payload under
#: `model_grouping/2` with the packaged `_v3` prompt (b17, ADR-0057).
V16_GROUPING_DIGEST = "ab826a3e6e8a1b41b2573803f3fdf915c50189bf070af767547aad011dfd391d"


def test_declared_role_loop_keys_bind_role_and_adaptation_payloads(tmp_path):
    from evolution.tree.roles.seals import section_payload

    document = chat_roles_document(v16_document())
    for section, key, minimum in ROLE_LOOP_KEYS:
        document[section][key] = minimum + 3
    manifest = load(tmp_path, document)
    adaptation = _role_adaptation(manifest, tmp_path).to_payload()
    assert section_payload(manifest.workflow.editor)["max_protocol_repairs"] == 3
    assert section_payload(manifest.workflow.reflector.role)["max_consecutive_tool_failures"] == 4
    assert adaptation["grouping"]["max_repairs"] == 3
    assert adaptation["grouping"]["max_consecutive_full_fallbacks"] == 4
    assert adaptation["reflection"]["max_consecutive_skips"] == 4


@pytest.mark.parametrize("value", [0, 9, 10**30])
def test_v16_admits_preflight_sample_retries(tmp_path, value):
    document = v16_document()
    document["target"]["preflight_sample_retries"] = value
    assert load(tmp_path, document).target.preflight_sample_retries == value


@pytest.mark.parametrize(
    "value,code", [(-1, "bad_value"), (True, "bad_type"), (None, "bad_type"), (0.5, "bad_type")]
)
def test_v16_refuses_bad_preflight_sample_retries(tmp_path, value, code):
    document = v16_document()
    document["target"]["preflight_sample_retries"] = value
    assert refused(tmp_path, document)[:2] == (code, "target.preflight_sample_retries")


#: b16 spec §10: the `/16` chat Grouper's portion keys and their minimums.
GROUPING_V2_KEYS = (
    ("group_output_tokens", 1),
    ("glimpse_chars", 1),
    ("max_set_members", 2),
    ("read_chars", 1),
    ("max_turns", 1),
    ("card_examples", 0),
    ("max_reduce_levels", 1),
    ("max_requests", 1),
)


@pytest.mark.parametrize("key,minimum", GROUPING_V2_KEYS)
def test_v16_grouping_keys_have_no_upper_bound(tmp_path, key, minimum):
    document = chat_roles_document(v16_document())
    for value in (minimum, 10**9):
        document["grouper"][key] = value
        assert getattr(load(tmp_path, document).workflow.grouper, key) == value
    for value, code in ((minimum - 1, "bad_value"), (True, "bad_type"), (2.0, "bad_type")):
        document["grouper"][key] = value
        assert refused(tmp_path, document)[:2] == (code, f"grouper.{key}")


def test_v16_grouping_zero_is_refused_except_card_examples(tmp_path):
    for key, _ in GROUPING_V2_KEYS:
        document = chat_roles_document(v16_document())
        document["grouper"][key] = 0
        if key == "card_examples":
            assert load(tmp_path, document).workflow.grouper.card_examples == 0
        else:
            assert refused(tmp_path, document)[:2] == ("bad_value", f"grouper.{key}")
    document = chat_roles_document(v16_document())
    document["grouper"]["max_set_members"] = 1
    assert refused(tmp_path, document)[:2] == ("bad_value", "grouper.max_set_members")


def test_v16_grouping_effective_defaults_and_declared_values(tmp_path):
    from evolution.tree.roles.config import GroupingV2Limits, effective_grouping_v2

    document = chat_roles_document(v16_document())
    assert effective_grouping_v2(load(tmp_path, document).workflow.grouper) == GroupingV2Limits(
        group_output_tokens=256,
        glimpse_chars=800,
        max_set_members=64,
        read_chars=16000,
        max_turns=8,
        card_examples=2,
        max_reduce_levels=4,
        max_requests=None,
    )
    for position, (key, minimum) in enumerate(GROUPING_V2_KEYS):
        document["grouper"][key] = minimum + position
    limits = effective_grouping_v2(load(tmp_path, document).workflow.grouper)
    assert [getattr(limits, key) for key, _ in GROUPING_V2_KEYS] == [
        minimum + position for position, (_, minimum) in enumerate(GROUPING_V2_KEYS)
    ]


def test_v16_identity_records_protocol_v2_and_declared_keys(tmp_path):
    import hashlib

    from evolution.prompts import TREE_GROUPING_MODEL_SYSTEM, TREE_GROUPING_MODEL_V3_SYSTEM

    def grouping(document):
        return _role_adaptation(load(tmp_path, document), tmp_path).to_payload()["grouping"]

    # b17 seals the packaged `_v3` prompt for a `/16` chat Grouper (ADR-0057);
    # `/15` keeps `_v1`.
    v3 = hashlib.sha256(TREE_GROUPING_MODEL_V3_SYSTEM.encode()).hexdigest()
    v1 = hashlib.sha256(TREE_GROUPING_MODEL_SYSTEM.encode()).hexdigest()
    assert v1 != v3
    undeclared = grouping(chat_roles_document(v16_document()))
    assert (undeclared["protocol"], undeclared["prompt_sha256"]) == ("model_grouping/2", v3)
    assert not {key for key, _ in GROUPING_V2_KEYS} & undeclared.keys()
    old = grouping(chat_roles_document(v15_document()))
    assert (old["protocol"], old["prompt_sha256"]) == ("model_grouping/1", v1)

    document = chat_roles_document(v16_document())
    document["grouper"]["glimpse_chars"] = 1200
    one = grouping(document)
    assert one["glimpse_chars"] == 1200
    assert not ({key for key, _ in GROUPING_V2_KEYS} - {"glimpse_chars"}) & one.keys()
    for key, minimum in GROUPING_V2_KEYS:
        document["grouper"][key] = minimum
    every = grouping(document)
    assert {key: every[key] for key, _ in GROUPING_V2_KEYS} == dict(GROUPING_V2_KEYS)


def test_b18_keeps_chat_grouper_identity_and_grouping_policy_digest(tmp_path):
    import hashlib

    from evolution.tree.evolve import _chat_grouper_identity
    from evolution.tree.execution.journal_identity import canonical_json
    from tests import test_tree_b17_manifest_identity as b17

    # The b17 literals live in the parametrization of the pinning test; read them
    # from there instead of copying the hex strings.
    (mark,) = [
        m
        for m in b17.test_absent_b17_role_keys_keep_identity_bytes.pytestmark
        if m.name == "parametrize"
    ]
    b17_grouper_digest = dict(mark.args[1])["grouper"]
    section = b17.parse_role(tmp_path, "grouper", manifest_v2.SCHEMA_V16)
    identity = _chat_grouper_identity(section, family15=True)
    assert hashlib.sha256(canonical_json(identity)).hexdigest() == b17_grouper_digest
    # V16_GROUPING_DIGEST is the policy digest asserted in
    # test_absent_role_loop_keys_keep_role_and_adaptation_digests for "16".
    assert V16_GROUPING_DIGEST == "ab826a3e6e8a1b41b2573803f3fdf915c50189bf070af767547aad011dfd391d"
