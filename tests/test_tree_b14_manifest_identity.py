"""b14 declarations bind only /16 and leave absent-key identities intact."""

import hashlib
from dataclasses import replace

import pytest

from evolution.tree.campaign_identity import _probe_identity, probe_identity_payload
from evolution.tree.evolve import _chat_grouper_identity
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.manifest import manifest_v2 as manifest
from evolution.tree.roles.seals import section_payload
from tests.tree.manifest.test_manifest_v2 import RETRY


@pytest.mark.parametrize("identity", [{}, {"reflector": None}])
@pytest.mark.parametrize("family", [14, 13])
def test_probe_continues_checks_family_before_identity(identity, family):
    from evolution.tree.campaign_identity import probe_continues

    assert probe_continues(identity, family) is False


def chat_document():
    return dict(
        kind="chat",
        model="test-model",
        base_url="https://example.test/v1",
        context_compact_chars=128000,
        context_message_max_chars=12000,
        retry=RETRY,
    )


def grouper_document():
    return dict(
        kind="chat",
        backend="openai_chat",
        model="test-model",
        base_url="https://example.test/v1",
        timeout_seconds=60,
        concurrency=1,
        max_input_chars=8000,
        max_output_tokens=256,
        retry=RETRY,
    )


def parse_role(tmp_path, role, schema, **keys):
    if role == "grouper":
        return manifest._parse_grouper_section(
            {**grouper_document(), **keys}, role, tmp_path, tmp_path / "seed", schema=schema
        )
    return manifest._parse_chat_editor(
        {**chat_document(), **keys}, role, tmp_path, tmp_path / "seed", schema=schema
    )


def parse_probe(schema, **keys):
    return manifest._parse_probe_config(
        {"base_url": "http://localhost:8099", **keys},
        "reflector.probe",
        target_base_url="http://localhost:8765",
        schema=schema,
    )


@pytest.mark.parametrize(
    "role,key,value",
    [
        ("probe", "on_unavailable", "continue"),
        *[
            (role, key, value)
            for role in ("editor", "reflector", "grouper")
            for key, value in (("provider_order", ["fireworks"]), ("allow_fallbacks", True))
        ],
    ],
)
def test_v15_refuses_new_keys(tmp_path, role, key, value):
    with pytest.raises(manifest.ManifestError) as caught:
        if role == "probe":
            parse_probe(manifest.SCHEMA_V15, **{key: value})
        else:
            parse_role(tmp_path, role, manifest.SCHEMA_V15, **{key: value})
    prefix = "reflector.probe" if role == "probe" else role
    assert (caught.value.code, caught.value.detail) == ("unknown_key", f"{prefix}.{key}")


@pytest.mark.parametrize("mode", [None, "suspend", "continue"])
def test_v16_accepts_on_unavailable(mode):
    probe = parse_probe(manifest.SCHEMA_V16, **({} if mode is None else {"on_unavailable": mode}))
    assert probe.on_unavailable == mode
    payload = probe_identity_payload(probe)
    assert ("on_unavailable" in payload) is (mode is not None)
    assert _probe_identity(payload) == payload


def test_v16_refuses_fail():
    with pytest.raises(manifest.ManifestError) as caught:
        parse_probe(manifest.SCHEMA_V16, on_unavailable="fail")
    assert caught.value.code == "bad_value"


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize(
    "keys",
    [
        {"provider_order": ["x"], "provider_only": ["x"]},
        {"allow_fallbacks": True},
        {"provider_order": ["x", "x"]},
        {"provider_order": ["x"], "allow_fallbacks": "yes"},
    ],
)
def test_v16_routing_refusals(tmp_path, role, keys):
    with pytest.raises(manifest.ManifestError) as caught:
        parse_role(tmp_path, role, manifest.SCHEMA_V16, **keys)
    assert caught.value.code == "bad_value"


# Captured from 952c9533a before production edits; source files equal origin/main.
CHAT_BYTES = b'{"base_url":"https://example.test/v1","budgets":{"max_changed_files":null,"max_payload_bytes":1048576,"max_written_bytes":16777216},"context_compact_chars":128000,"context_message_max_chars":12000,"max_output_tokens":8192,"max_turns":64,"model":"test-model","provider_only":null,"proxy_env":null,"quantizations":null,"reasoning_effort":null,"retry":{"backoff":{"initial_seconds":1,"jitter":"full","max_seconds":20,"multiplier":2},"max_attempts":4,"total_budget_seconds":120,"version":"role_call_retry/1"},"verify_ssl_certs":true}'
GROUPER_BYTES = b'{"backend":"openai_chat","base_url":"https://example.test/v1","concurrency":1,"fallback":null,"kind":"chat","max_input_chars":8000,"max_output_tokens":256,"model":"test-model","prompt_sha256":"63b929ea0b46fbdb2127c251a65aa82c15c45ce9e9ec1f55d088563dad7ccb76","protocol":"model_grouping/1","provider_only":null,"proxy_env":null,"reasoning_effort":null,"representatives_per_case":3,"retry":{"backoff":{"initial_seconds":1,"jitter":"full","max_seconds":20,"multiplier":2},"max_attempts":4,"total_budget_seconds":120,"version":"role_call_retry/1"},"timeout_seconds":60,"verify_ssl_certs":true}'

#: A `/16` Grouper's identity as `evolve` builds it (b16 spec §11): `/15`'s keys
#: with `model_grouping/2` and the packaged `_v2` prompt's digest, and no
#: undeclared portion key.
GROUPER_V16_BYTES = GROUPER_BYTES.replace(b'"model_grouping/1"', b'"model_grouping/2"').replace(
    b"63b929ea0b46fbdb2127c251a65aa82c15c45ce9e9ec1f55d088563dad7ccb76",
    b"ec5ef3d2c7fe85b87c84388a1e32c323fa402f6f7942f2778e2853336d11d0ee",
)
IDENTITY_DIGESTS = {
    "grouper": "10ef231774444bbc5d167f66f9f6fc5e7248b985faf4edf5cc44e767775f299e",
    "grouper/16": "926c57741fc0fb7d0fe2e560a1fc21260cfdf6fff785d1ca202af20f686460be",
    "chat": "599a334d15e925c3eefa6768a57cb4cd628b2b7d8db1bc20bac3b4142c216aee",
}


def _grouper(section, schema):
    return _chat_grouper_identity(section, family15=schema == manifest.SCHEMA_V16)


@pytest.mark.parametrize("schema", [manifest.SCHEMA_V15, manifest.SCHEMA_V16])
@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_undeclared_keys_keep_identity_bytes(tmp_path, schema, role):
    section = parse_role(tmp_path, role, schema)
    v16 = schema == manifest.SCHEMA_V16
    payload = _grouper(section, schema) if role == "grouper" else section_payload(section)
    if role == "grouper":
        expected, digest = (GROUPER_V16_BYTES, "grouper/16") if v16 else (GROUPER_BYTES, "grouper")
    else:
        expected, digest = CHAT_BYTES, "chat"
    assert canonical_json(payload) == expected
    assert hashlib.sha256(canonical_json(payload)).hexdigest() == IDENTITY_DIGESTS[digest]
    probe = probe_identity_payload(parse_probe(schema))
    assert (
        canonical_json(probe)
        == b'{"base_url":"http://localhost:8099","budget":32,"description_sha256":null,"health_path":"/health","methods":["GET","POST"],"request_timeout_seconds":60.0}'
    )
    assert (
        hashlib.sha256(canonical_json(probe)).hexdigest()
        == "eb16055e7e7fcdcd49660aa840faa87ae52af90e7f1be723db0c246ee6796c85"
    )


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_declared_keys_change_identity(tmp_path, role):
    section = parse_role(tmp_path, role, manifest.SCHEMA_V16, provider_order=["fireworks"])

    def payload(section):
        if role == "grouper":
            return _grouper(section, manifest.SCHEMA_V16)
        return section_payload(section)

    ordered = payload(section)
    assert ordered["provider_order"] == ["fireworks"]
    assert "allow_fallbacks" not in ordered
    fallback = payload(replace(section, allow_fallbacks=False))
    assert fallback["allow_fallbacks"] is False
    assert canonical_json(fallback) != canonical_json(ordered)
    assert canonical_json(ordered) != (GROUPER_V16_BYTES if role == "grouper" else CHAT_BYTES)


async def test_declaring_on_unavailable_needs_rebase(tmp_path, monkeypatch):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_run import payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(campaign_module, "check_probe_health", ready)

    def configure(document, mode=None):
        document["reflector"]["probe"] = {"base_url": "http://localhost:8099"}
        if mode is not None:
            document["reflector"]["probe"]["on_unavailable"] = mode

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path, monkeypatch, configure=configure, cut="after_attempt_input_frozen"
        )
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()

    def declared(document):
        configure(document, "suspend")

    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=declared)
    refused = caught.value
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    assert (run / "campaign.jsonl").read_bytes() == before
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=declared,
        authorizations=[_decision(refused, None, reason="declare probe policy")],
    )
    assert result.terminal == "finished"
    assert payloads(run, "identity_rebased")
