"""V-S1-4: public durable preparation without target or campaign execution."""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from scaffold_15 import scaffold_15


def _request(tmp_path: Path, monkeypatch, *, reserved: bool = False, scaffold=scaffold_15):
    from evolution_lab.preparation import (
        CampaignSource,
        EnvironmentInput,
        SourceRoot,
    )
    from evolution_lab.preparation.environment import CapturedEnvironment
    from evolution_lab.preparation.models import EnvironmentDescriptor
    import evolution_lab.preparation.bundle as bundle_module

    root = tmp_path / "source"
    scaffold(root)
    descriptor = EnvironmentDescriptor.from_payload({
        "schema": "lab-environment/1",
        "executable": sys.executable,
        "executable_sha256": "a" * 64,
        "python_version": "3.12.11",
        "distributions": [
            {
                "name": "evolution-lab",
                "version": "0.0.3.dev4",
                "wheel_sha256": "b" * 64,
            },
            {
                "name": "evo-skills",
                "version": "0.3.0a5",
                "wheel_sha256": "c" * 64,
            },
        ],
        "runtime_files": [],
        "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1",
        "limitations": ["synthetic capture boundary"],
    })

    def capture(value, *, execution):
        assert value.executable == Path(sys.executable)
        assert execution is True
        return CapturedEnvironment(descriptor, '{"archive_wheels":[]}')

    monkeypatch.setattr(bundle_module, "capture_environment", capture)
    manifest = root / "manifest.json"
    raw = json.loads(manifest.read_text())
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    manifest.write_text(json.dumps(raw))
    if reserved:
        corpus = root / "corpus.jsonl"
        rows = [json.loads(row) for row in corpus.read_text().splitlines() if row]
        rows[-1]["public"]["conformance"] = True
        corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))

    launcher = root / "launcher.py"
    launcher.write_text(
        "from pathlib import Path\n"
        "Path('LAUNCHER_EXECUTED').write_text('unexpected')\n"
    )
    launcher.chmod(0o755)
    launch = root / "launch.json"
    launch.write_text(json.dumps({
        "schema": "lab-target-launch/1",
        "command": {
            "executable": sys.executable,
            "args": [{"bundle_path": "launcher.py"}],
        },
        "environment_ref": descriptor.digest,
        "program_paths": ["launcher.py"],
        "env": {"MODE": "offline"},
        "secret_names": ["TARGET_TOKEN"],
        "endpoint": {"base_url": raw["target"]["base_url"]},
        "identity": {
            "target_id": "neutral-target",
            "contract_digest": raw["target"].get("contract_digest"),
            "settings_hash": raw["target"].get("settings_hash"),
            "runtime_ref_digest": None,
            "seed_mode": raw["target"]["seed_mode"],
        },
        "policy": "manifest.json",
        "limits": {
            "max_instances": 4,
            "max_concurrent_sessions": 2,
            "max_input_bytes": 1 << 20,
            "max_output_bytes": 1 << 20,
        },
        "startup_timeout_seconds": 10,
        "stop_grace_seconds": 10,
        "conformance": None,
    }))
    return {
        "source": CampaignSource(manifest, launch),
        "environments": (
            EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",)),
        ),
        "env": {"MODE": "offline", "LANG": "C.UTF-8"},
        "secret_names": ("EXECUTION_TOKEN", "TARGET_TOKEN"),
        "roots": (SourceRoot("project", root),),
    }


def _assert_failure(error, code: str, stage: str) -> None:
    assert error.value.failure.to_payload() == {
        "code": code,
        "stage": stage,
        "message": error.value.failure.message,
    }
    assert str(error.value) == error.value.failure.message
    assert str(error.value).startswith("The preparation") or str(error.value).startswith(
        "The requested"
    ) or str(error.value).startswith("The saved") or str(error.value).startswith(
        "The campaign"
    ) or str(error.value).startswith("A declared")


def test_describe_environment_infers_lexical_role_and_returns_only_descriptor(
    tmp_path, monkeypatch
):
    import evolution_lab.preparation.api as api_module
    import evolution_lab.preparation.bundle as bundle_module
    from evolution_lab.preparation import (
        EnvironmentDescriptor,
        EnvironmentInput,
        describe_environment,
    )
    from evolution_lab.preparation.environment import CapturedEnvironment

    request = _request(tmp_path, monkeypatch)
    execution_input = request["environments"][0]
    execution_capture = bundle_module.capture_environment(execution_input, execution=True)
    target_input = EnvironmentInput(
        Path("/opt/neutral-target/bin/python"),
        (tmp_path / "target.whl",),
    )
    target_payload = execution_capture.descriptor.to_payload()
    target_payload["executable"] = str(target_input.executable)
    target_payload["executable_sha256"] = "d" * 64
    target_descriptor = EnvironmentDescriptor.from_payload(target_payload)

    def capture(value, *, execution):
        if value is execution_input:
            assert execution is True
            return execution_capture
        assert value is target_input
        assert execution is False
        return CapturedEnvironment(target_descriptor, '{"private":"receipt"}')

    monkeypatch.setattr(api_module, "capture_environment", capture)

    described_execution = describe_environment(execution_input)
    described_target = describe_environment(target_input)

    assert isinstance(described_execution, EnvironmentDescriptor)
    assert described_execution is execution_capture.descriptor
    assert described_target is target_descriptor
    assert described_execution.python_version is not None
    assert "digest" not in described_execution.to_payload()
    assert not hasattr(described_execution, "source_receipt")
    with pytest.raises(FrozenInstanceError):
        described_execution.python_version = "changed"


def test_describe_environment_rejects_an_invalid_input_before_capture(monkeypatch):
    import evolution_lab.preparation.api as api_module
    from evolution_lab.preparation import PreparationError, describe_environment

    monkeypatch.setattr(
        api_module,
        "capture_environment",
        lambda *args, **kwargs: pytest.fail("capture must not receive an invalid input"),
    )
    with pytest.raises(PreparationError) as caught:
        describe_environment(object())
    _assert_failure(caught, "invalid_request", "bundle")


def test_create_bundle_recaptures_and_rejects_a_stale_described_digest(tmp_path, monkeypatch):
    import evolution_lab.preparation.api as api_module
    import evolution_lab.preparation.bundle as bundle_module
    from evolution_lab.preparation import (
        EnvironmentDescriptor,
        PreparationError,
        PreparationStore,
        describe_environment,
    )
    from evolution_lab.preparation.environment import CapturedEnvironment

    request = _request(tmp_path, monkeypatch)
    environment_input = request["environments"][0]
    first = bundle_module.capture_environment(environment_input, execution=True)
    monkeypatch.setattr(api_module, "capture_environment", lambda value, *, execution: first)
    described = describe_environment(environment_input)
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["environment_ref"] = described.digest
    launch_path.write_text(json.dumps(launch))

    changed_payload = described.to_payload()
    changed_payload["executable_sha256"] = "e" * 64
    changed = EnvironmentDescriptor.from_payload(changed_payload)
    monkeypatch.setattr(
        bundle_module,
        "capture_environment",
        lambda value, *, execution: CapturedEnvironment(changed, '{"archive_wheels":[]}'),
    )

    with pytest.raises(PreparationError) as caught:
        PreparationStore(tmp_path / "state").create_bundle(**request)
    _assert_failure(caught, "environment_invalid", "bundle")
    assert list((tmp_path / "state" / "bundles").iterdir()) == []


def test_public_store_creates_prepares_and_reopens_immutable_summaries(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _request(tmp_path, monkeypatch)
    state = tmp_path / "state"
    store = PreparationStore(state)

    bundle = store.create_bundle(**request)
    prepared = store.prepare_campaign(bundle.digest)
    reopened = PreparationStore(state)

    assert reopened.get_bundle(bundle.digest) == bundle
    assert reopened.get_prepared(prepared.digest) == prepared
    assert prepared.to_payload() == {
        "schema": "lab-prepared-summary/1",
        "digest": prepared.digest,
        "bundle_digest": bundle.digest,
        "kind": "campaign",
        "target_binding": {
            "launch_digest": bundle.launch_digest,
            "environment_digest": bundle.environment_digests[0],
            "base_url": "http://127.0.0.1:8765",
            "policy_digest": "384604dcc63fb6815da879606f2a66f7d310a285cafcb79e6e68feb711985937",
            "target_id": "neutral-target",
            "contract_digest": None,
            "settings_hash": None,
            "runtime_ref_digest": None,
            "seed_mode": "exact",
        },
        "execution_environment": bundle.environment_digests[0],
        "secret_names": ["EXECUTION_TOKEN", "TARGET_TOKEN"],
        "runtime_policy": {
            "stop_grace_seconds": 315.0,
            "cleanup_wait_seconds": 30.0,
        },
        "evaluation": None,
    }
    assert not hasattr(store, "start")
    assert not hasattr(store, "start_target")
    assert not (tmp_path / "LAUNCHER_EXECUTED").exists()
    assert not any(path.name.startswith("run") for path in state.rglob("*"))
    with pytest.raises(FrozenInstanceError):
        prepared.kind = "evaluation"
    payload = prepared.to_payload()
    payload["secret_names"].append("MUTATED")
    assert prepared.secret_names == ("EXECUTION_TOKEN", "TARGET_TOKEN")


def test_prepare_uses_saved_a8_grouping_docs_and_reflection_metadata_after_source_removal(
    tmp_path, monkeypatch
):
    from evolution_lab.preparation import PreparationStore

    request = _request(tmp_path, monkeypatch)
    root = request["source"].manifest_path.parent
    grouping = root / "grouping"
    grouping.mkdir()
    (grouping / "group.py").write_text(
        "from pathlib import Path\nPath('GROUPING_EXECUTED').write_text('unexpected')\n"
    )
    (grouping / "dependency.txt").write_text("dependency\n")
    docs = root / "docs/nested"
    docs.mkdir(parents=True)
    (root / "docs/guide.md").write_text("guide\n")
    (docs / "details.md").write_text("details\n")
    manifest_path = request["source"].manifest_path
    manifest = json.loads(manifest_path.read_text())
    manifest["reflector"]["environment_docs"] = "docs"
    manifest["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "group.py"],
        "cwd": "grouping",
        "dependency_paths": ["grouping/dependency.txt"],
        "allowed_env": [],
        "timeout_seconds": 17,
        "config": {"opaque_locator": str(grouping / "dependency.txt")},
    }
    manifest_path.write_text(json.dumps(manifest))
    corpus = root / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    rows[0]["reflection_metadata"] = {"failure_family": "format", "rank": 2}
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["conformance"] = {"kind": "core-a5-session-public/1"}
    launch_path.write_text(json.dumps(launch))
    state = tmp_path / "state"
    store = PreparationStore(state)

    bundle = store.create_bundle(**request)
    shutil.rmtree(root)
    prepared = store.prepare_campaign(bundle.digest)

    assert prepared.runtime_policy.stop_grace_seconds == 332.0
    assert (state / "bundles" / bundle.digest / "inputs/project/docs/nested/details.md").read_text() == "details\n"
    assert not (tmp_path / "GROUPING_EXECUTED").exists()
    assert PreparationStore(state).get_prepared(prepared.digest) == prepared


def test_explicit_runtime_policy_is_validated_and_changes_identity(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**_request(tmp_path, monkeypatch))
    explicit = store.prepare_campaign(
        bundle.digest,
        stop_grace_seconds=45,
        cleanup_wait_seconds=12.5,
    )
    defaulted = store.prepare_campaign(bundle.digest)

    assert explicit.runtime_policy.to_payload() == {
        "stop_grace_seconds": 45,
        "cleanup_wait_seconds": 12.5,
    }
    assert explicit.digest != defaulted.digest
    for value in (True, 0, float("inf")):
        with pytest.raises(PreparationError) as caught:
            store.prepare_campaign(bundle.digest, stop_grace_seconds=value)
        _assert_failure(caught, "invalid_request", "prepare")


def test_prepare_and_read_remap_missing_or_corrupt_references_safely(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    store = PreparationStore(tmp_path / "state")
    with pytest.raises(PreparationError) as missing_prepare:
        store.prepare_campaign("d" * 64)
    _assert_failure(missing_prepare, "not_found", "prepare")
    with pytest.raises(PreparationError) as missing_read:
        store.get_bundle("d" * 64)
    _assert_failure(missing_read, "not_found", "read")

    bundle = store.create_bundle(**_request(tmp_path, monkeypatch))
    launch = tmp_path / "state" / "bundles" / bundle.digest / "inputs/project/launch.json"
    launch.chmod(0o600)
    launch.write_text("corrupt")
    with pytest.raises(PreparationError) as corrupt_prepare:
        store.prepare_campaign(bundle.digest)
    _assert_failure(corrupt_prepare, "artifacts_invalid", "prepare")


def test_prepare_rejects_reserved_public_input_before_publication(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**_request(tmp_path, monkeypatch, reserved=True))

    with pytest.raises(PreparationError) as caught:
        store.prepare_campaign(bundle.digest)
    _assert_failure(caught, "reserved_public_input", "prepare")
    assert list((tmp_path / "state" / "prepared").iterdir()) == []


def test_launch_may_strengthen_manifest_optional_identity_pins(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _request(tmp_path, monkeypatch)
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["identity"]["contract_digest"] = "d" * 64
    launch["identity"]["settings_hash"] = "e" * 64
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / "state")

    prepared = store.prepare_campaign(store.create_bundle(**request).digest)

    assert prepared.target_binding.contract_digest == "d" * 64
    assert prepared.target_binding.settings_hash == "e" * 64


@pytest.mark.parametrize(
    "mutation",
    ["endpoint", "contract", "settings", "seed_mode", "policy", "input_limit", "output_limit"],
)
def test_prepare_requires_launch_semantics_to_agree_with_native_manifest(
    tmp_path, monkeypatch, mutation
):
    from evolution_lab.preparation import PreparationError, PreparationStore

    request = _request(tmp_path, monkeypatch)
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    manifest_path = request["source"].manifest_path
    manifest = json.loads(manifest_path.read_text())
    if mutation == "endpoint":
        launch["endpoint"]["base_url"] = "http://127.0.0.1:8766"
    elif mutation == "contract":
        manifest["target"]["contract_digest"] = "f" * 64
        launch["identity"]["contract_digest"] = "d" * 64
    elif mutation == "settings":
        manifest["target"]["settings_hash"] = "f" * 64
        launch["identity"]["settings_hash"] = "e" * 64
    elif mutation == "seed_mode":
        launch["identity"]["seed_mode"] = "none"
    elif mutation == "policy":
        launch["policy"] = "corpus.jsonl"
    elif mutation == "input_limit":
        launch["limits"]["max_input_bytes"] = (1 << 20) - 1
    else:
        launch["limits"]["max_output_bytes"] = (1 << 26) + 1
    manifest_path.write_text(json.dumps(manifest))
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)

    with pytest.raises(PreparationError) as caught:
        store.prepare_campaign(bundle.digest)
    _assert_failure(caught, "artifacts_invalid", "prepare")
    assert list((tmp_path / "state" / "prepared").iterdir()) == []


def test_preparation_module_exports_current_durable_surface():
    import evolution_lab.preparation as preparation

    assert set(preparation.__all__) == {
        "BundleSummary",
        "CampaignSource",
        "EnvironmentDescriptor",
        "EnvironmentInput",
        "EvaluationSource",
        "Failure",
        "PathBinding",
        "PreparationError",
        "PreparationStore",
        "PreparedSummary",
        "ProbeSource",
        "SourceRoot",
        "describe_environment",
    }
    assert not hasattr(preparation, "StoredBundle")
    assert not hasattr(preparation, "StoredPrepared")


def _prepare_with_limits(tmp_path, monkeypatch, scaffold, instances, sessions):
    from evolution_lab.preparation import PreparationStore

    request = _request(tmp_path, monkeypatch, scaffold=scaffold)
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["limits"].update(max_instances=instances, max_concurrent_sessions=sessions)
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / "state")
    return store.prepare_campaign(store.create_bundle(**request).digest)


def test_prepare_refuses_a_target_too_small_for_the_15_campaign(tmp_path, monkeypatch):
    from scaffold_15 import scaffold_15
    from evolution_lab.preparation import PreparationError

    with pytest.raises(PreparationError) as error:
        _prepare_with_limits(tmp_path, monkeypatch, scaffold_15, 2, 1)
    assert error.value.failure.code == "artifacts_invalid" and error.value.failure.stage == "prepare"
    assert "target capacity: the campaign needs 4 instances, the target declares 2" in str(error.value)


def test_prepare_accepts_a_target_sized_for_the_15_campaign(tmp_path, monkeypatch):
    from scaffold_15 import scaffold_15

    assert _prepare_with_limits(tmp_path, monkeypatch, scaffold_15, 4, 1)


def test_prepare_says_more_than_1024_when_core_cannot_count_the_demand(tmp_path, monkeypatch):
    from scaffold_15 import scaffold_15
    from evolution_lab.preparation import PreparationError, api

    def too_many(policy, sessions):
        raise ValueError("instance demand exceeds 1024")

    monkeypatch.setattr(api.core_raw, "instance_demand", too_many)
    with pytest.raises(PreparationError) as error:
        _prepare_with_limits(tmp_path, monkeypatch, scaffold_15, 2, 1)
    assert error.value.failure.code == "artifacts_invalid" and error.value.failure.stage == "prepare"
    assert "the campaign needs more than 1024 instances, the target declares 2" in str(error.value)
