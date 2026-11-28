"""V-S2-6: saved Bundle binding and post-acceptance environment attestation."""
from __future__ import annotations

import json
import os
import shutil
import sys
import asyncio
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse
from zipfile import ZipFile

import pytest
from scaffold_15 import scaffold_15


CORE_WHEEL = Path(os.environ["LAB_TEST_CORE_WHEEL"])

# A top-level /15 chat Grouper: key and proxy are environment names; core reads their values.
MODEL_GROUPING = {
    "kind": "chat", "backend": "openai_chat", "model": "fake-model",
    "base_url": "http://127.0.0.1:9/v1", "api_key_env": "GROUPING_KEY", "proxy_env": "GROUPING_PROXY",
    "timeout_seconds": 30, "concurrency": 1, "max_input_chars": 8000, "max_output_tokens": 256,
    "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 20,
              "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 4, "jitter": "full"}},
}


def _installed_lab_wheel() -> Path:
    direct = json.loads(metadata.distribution("evolution-lab").read_text("direct_url.json"))
    parsed = urlparse(direct["url"])
    assert parsed.scheme == "file"
    wheel = Path(unquote(parsed.path))
    assert wheel.is_file()
    return wheel


def make_prepared(
    tmp_path: Path,
    *,
    secret_roles: bool = False,
    service_location: str | None = None,
    model_grouping: dict | None = None,
    grouping_prompt: bytes | None = None,
):
    from evolution_lab.preparation import (
        CampaignSource,
        EnvironmentInput,
        PreparationStore,
        SourceRoot,
    )

    source = tmp_path / "source"
    scaffold_15(source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for role in ("scorer", "editor", "reflector"):
        if "argv" in manifest[role]:
            manifest[role]["argv"][0] = sys.executable
    secret_names = ["TARGET_TOKEN"]
    target_names = ["TARGET_TOKEN"]
    if secret_roles:
        secret_names = [
            "CHAT_KEY",
            "CLI_TOKEN",
            "GROUPING_SHORT",
            "GROUPING_TOKEN",
            "OPENAI_API_KEY",
            "PROXY_URL",
            "SCORE_TOKEN",
            "SHARED_TOKEN",
            "TARGET_TOKEN",
            "UNUSED_TOKEN",
        ]
        target_names = ["SHARED_TOKEN", "TARGET_TOKEN"]
        manifest["scorer"]["allowed_env"] = [
            "OPTIONAL_NONSECRET",
            "SCORE_TOKEN",
            "SHARED_TOKEN",
        ]
        manifest["editor"] = {
            "kind": "chat",
            "model": "neutral-chat-model",
            "base_url": "http://127.0.0.1:9000/v1",
            "api_key_env": "CHAT_KEY",
            # Explicit /15 compaction, preserving the earlier schema defaults.
            "context_compact_chars": 128_000,
            "context_message_max_chars": 12_000,
            "proxy_env": "PROXY_URL",
            # /15 requires the author's own retry on a chat role; Lab never supplies it (PDR-0015).
            "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                      "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8,
                                  "jitter": "full"}},
        }
        adaptive = manifest["reflector"]
        manifest["reflector"] = {
            "kind": "agent_cli",
            "model": "codex/gpt-5",
            "permission": "safe",
            "agent_bin": sys.executable,
            "allowed_env": ["CLI_TOKEN"],
            "environment_docs": None,
            "probe": None,
            "max_consecutive_failures": 2,  # /15 key, written by the author (PDR-0015)
            "selection": adaptive["selection"],
            "families": adaptive["families"],
        }
        manifest["grouper"] = {
            "kind": "command",
            "argv": [sys.executable, "reflect.py"],
            "cwd": "reflector",
            "dependency_paths": [],
            "allowed_env": [
                "GROUPING_SHORT",
                "GROUPING_TOKEN",
                "OPTIONAL_GROUPING_NONSECRET",
            ],
            "timeout_seconds": 7,
            "config": {},
        }
    if model_grouping is not None:
        # The Reflector keeps its role; only the top-level Grouper is swapped for chat.
        manifest["grouper"] = dict(model_grouping)
        if grouping_prompt is not None:
            (source / "grouping.md").write_bytes(grouping_prompt)
            manifest["grouper"]["system_prompt"] = "grouping.md"
        secret_names += [model_grouping["api_key_env"], model_grouping["proxy_env"]]
    if service_location == "declared_secret":
        secret_names.append("LAB_TARGET_CONFIG_FD")
    elif service_location == "target_secret":
        secret_names.append("LAB_TARGET_CONFIG_FD")
        target_names.append("LAB_TARGET_CONFIG_FD")
    manifest_path.write_text(json.dumps(manifest))

    runtime = tmp_path / "runtime.marker"
    runtime.write_text("pinned runtime\n")
    lab_wheel = tmp_path / "retained-lab.whl"
    shutil.copyfile(_installed_lab_wheel(), lab_wheel)
    launch_path = source / "launch.json"
    base_env = {"BASE_MODE": "saved", "TARGET_MODE": "local"}
    target_env = {"TARGET_MODE": "local"}
    if service_location == "base_env":
        base_env["LAB_CAMPAIGN_CONFIG_FD"] = "9"
    elif service_location == "target_env":
        base_env["LAB_TARGET_READY_FD"] = "9"
        target_env["LAB_TARGET_READY_FD"] = "9"
    launch_path.write_text(json.dumps({
        "schema": "lab-target-launch/1",
        "command": {
            "executable": sys.executable,
            "args": [{"bundle_path": "launcher.py"}],
        },
        "environment_ref": "pending",
        "program_paths": ["launcher.py"],
        "env": target_env,
        "secret_names": target_names,
        "endpoint": {"base_url": manifest["target"]["base_url"]},
        "identity": {
            "target_id": "neutral-target",
            "contract_digest": None,
            "settings_hash": None,
            "runtime_ref_digest": None,
            "seed_mode": manifest["target"]["seed_mode"],
        },
        "policy": "manifest.json",
        "limits": {
            "max_instances": 4,
            "max_concurrent_sessions": 1,
            "max_input_bytes": 2_000_000,
            "max_output_bytes": 4096,
        },
        "startup_timeout_seconds": 10,
        "stop_grace_seconds": 10,
        "conformance": None,
    }))
    launcher = source / "launcher.py"
    launcher.write_text("raise RuntimeError('must not execute during binding')\n")
    launcher.chmod(0o700)

    environment = EnvironmentInput(
        Path(sys.executable),
        (lab_wheel, CORE_WHEEL),
        (runtime,),
    )
    from evolution_lab.preparation import describe_environment

    environment_digest = describe_environment(environment).digest
    launch = json.loads(launch_path.read_text())
    launch["environment_ref"] = environment_digest
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / "preparation")
    bundle = store.create_bundle(
        source=CampaignSource(manifest_path, launch_path),
        environments=(environment,),
        env=base_env,
        secret_names=secret_names,
        roots=(SourceRoot("project", source),),
    )
    prepared = store.prepare_campaign(bundle.digest)
    return {
        "state": tmp_path / "preparation",
        "source": source,
        "runtime": runtime,
        "lab_wheel": lab_wheel,
        "bundle": bundle,
        "prepared": prepared,
    }


def make_evaluation_prepared(tmp_path: Path, monkeypatch, *, shared_secret: bool = False):
    """Create a real captured evaluation Bundle from the Task 1 neutral fixture."""
    import evolution_lab.preparation.bundle as bundle_module
    from evolution_lab.preparation import EnvironmentInput, PreparationStore, describe_environment
    from test_preparation_evaluation import _evaluation_request

    capture_environment = bundle_module.capture_environment
    request = _evaluation_request(tmp_path, monkeypatch)
    monkeypatch.setattr(bundle_module, "capture_environment", capture_environment)
    root = request["source"].input_path.parent
    if shared_secret:
        value = json.loads(request["source"].input_path.read_text())
        value["scorer"]["allowed_env"].append("TARGET_TOKEN")
        request["source"].input_path.write_text(json.dumps(value))
    runtime = tmp_path / "evaluation-runtime.marker"
    runtime.write_text("pinned evaluation runtime\n")
    lab_wheel = tmp_path / "retained-evaluation-lab.whl"
    shutil.copyfile(_installed_lab_wheel(), lab_wheel)
    environment = EnvironmentInput(Path(sys.executable), (lab_wheel, CORE_WHEEL), (runtime,))
    environment_digest = describe_environment(environment).digest
    launch = json.loads(request["source"].launch_path.read_text())
    launch["environment_ref"] = environment_digest
    request["source"].launch_path.write_text(json.dumps(launch))
    request["environments"] = (environment,)
    store = PreparationStore(tmp_path / "evaluation-state")
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(bundle.digest, selection={"count": 1})
    return {
        "state": tmp_path / "evaluation-state",
        "source": root,
        "runtime": runtime,
        "lab_wheel": lab_wheel,
        "bundle": bundle,
        "prepared": prepared,
    }


def _drop_worker_member(wheel: Path) -> None:
    replacement = wheel.with_suffix(".replacement.whl")
    with ZipFile(wheel) as source, ZipFile(replacement, "w") as target:
        for info in source.infolist():
            if info.filename != "evolution_lab/owner/_campaign_worker.py":
                target.writestr(info, source.read(info.filename))
    os.replace(replacement, wheel)


def test_binding_reads_only_saved_bundle_after_source_is_deleted(tmp_path: Path) -> None:
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path)
    shutil.rmtree(saved["source"])

    bound = open_prepared(PreparationStorage(saved["state"]), saved["prepared"].digest)

    assert bound.bundle.bundle_path.is_dir()
    assert bound.bundle.launch_digest == saved["bundle"].launch_digest
    assert bound.bundle.manifest_path.is_relative_to(bound.bundle.bundle_path)
    assert bound.bundle.launch_path.is_relative_to(bound.bundle.bundle_path)
    assert bound.bundle.manifest.path == bound.bundle.manifest_path
    assert bound.bundle.manifest.schema == "evolution-v2-manifest/15"
    assert bound.bundle.execution_environment.digest == saved["bundle"].environment_digests[0]
    assert bound.bundle.target_environment.digest == saved["bundle"].environment_digests[0]
    assert bound.runtime_policy == saved["prepared"].runtime_policy


@pytest.mark.parametrize("change,code", [("drift", "environment_invalid"), ("missing", "environment_unavailable")])
def test_post_acceptance_revalidation_rejects_runtime_pin_change(
    tmp_path: Path, change: str, code: str
) -> None:
    from evolution_lab.owner.binding import open_bundle, revalidate_environment
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path)
    bound = open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)
    assert revalidate_environment(
        bound, bound.execution_environment.digest, stage="before_start"
    ) == bound.execution_environment
    if change == "drift":
        saved["runtime"].write_text("changed bytes\n")
    else:
        saved["runtime"].unlink()

    with pytest.raises(OwnerError) as caught:
        revalidate_environment(bound, bound.execution_environment.digest, stage="before_start")

    assert caught.value.code == code
    assert caught.value.stage == "before_start"


def test_declared_identity_refuses_a_retained_lab_wheel_without_s2_worker(
    tmp_path: Path,
) -> None:
    from evolution_lab.owner.binding import declared_executor_identity, open_bundle
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path)
    _drop_worker_member(saved["lab_wheel"])
    bound = open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)

    with pytest.raises(OwnerError) as caught:
        declared_executor_identity(bound)

    assert caught.value.code == "environment_invalid"
    assert caught.value.stage == "before_start"


@pytest.mark.parametrize("kind,worker_members", [
    ("campaign", {"evolution_lab.owner._campaign_worker", "evolution_lab.owner.core_entry",
                  "evolution_lab.owner.settlement"}),
    ("conformance", {"evolution_lab.owner._conformance_worker"}),
])
def test_real_installed_worker_helper_and_settlement_identity_is_revalidated(
    tmp_path: Path, kind, worker_members,
) -> None:
    from evolution_lab.owner.binding import (
        declared_executor_identity,
        open_bundle,
        revalidate_executor_identity,
    )
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path)
    bound = open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)
    declared = declared_executor_identity(bound, kind=kind)

    assert revalidate_executor_identity(bound, declared) == declared
    assert set(declared.module_hashes) == {
        "evolution_lab.owner._helper", "evolution_lab._ownership",
    } | worker_members


def test_evaluation_binding_reopens_saved_input_and_pins_real_worker(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner.binding import declared_executor_identity, open_prepared
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_evaluation_prepared(tmp_path, monkeypatch)
    shutil.rmtree(saved["source"])
    bound = open_prepared(PreparationStorage(saved["state"]), saved["prepared"].digest)

    assert bound.kind == "evaluation"
    assert bound.bundle.source_kind == "evaluation"
    assert bound.bundle.manifest.schema == "evolution-v2-manifest/1"
    assert bound.bundle.manifest.workflow is None
    assert bound.bundle.evaluation_input["scorer"]["allowed_env"] == (
        "SCORER_TOKEN", "MODE",
    )
    declared = declared_executor_identity(bound.bundle, kind="evaluation")
    assert declared.kind == "lab-evaluation-worker/1"
    assert set(declared.module_hashes) == {
        "evolution_lab.owner._helper",
        "evolution_lab._ownership",
        "evolution_lab.owner._evaluation_worker",
        "evolution_lab.owner.settlement",
    }


def test_owner_bind_tags_evaluation_connection(tmp_path: Path, monkeypatch) -> None:
    from evolution_lab.owner import Connection, Owner
    import evolution_lab.owner.api as api_module
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_evaluation_prepared(tmp_path, monkeypatch)

    async def immediate(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(api_module.asyncio, "to_thread", immediate)

    async def scenario():
        owner = Owner()
        owner._storage = PreparationStorage(saved["state"])
        owner._connections = {}
        owner._assert_mutation = lambda: None

        async def db(method, **values):
            assert method == "bind_connection"
            return Connection(
                "connection-1", values["source_kind"], values["bundle_digest"],
                values["launch_digest"], "generation-1",
            )

        async def admit(factory):
            accepted = asyncio.get_running_loop().create_future()
            await factory(accepted)
            return accepted.result()

        owner._db = db
        owner._admit = admit
        return await owner.bind(saved["bundle"].digest)

    assert asyncio.run(scenario()).source_kind == "evaluation"


def test_evaluation_check_uses_public_scan_and_preflight_only(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution.tree.artifacts.artifact_policy import scan_tree
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution_lab.owner.binding import open_bundle
    from evolution_lab.owner.target import evaluation_preflight
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_evaluation_prepared(tmp_path, monkeypatch)
    binding = open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)
    seen = []

    async def preflight(self, expected):
        seen.append(expected)
        return None

    monkeypatch.setattr(HttpSessionAdapter, "preflight", preflight)

    result = asyncio.run(evaluation_preflight(binding))
    assert result.public() == {
        "schema": "lab-check/1", "state": "passed", "failure": None,
    }
    expected = scan_tree(binding.manifest.seed_root, binding.manifest.policy)
    assert [(value.candidate_digest, value.dependency_digest) for value in seen] == [
        (expected.candidate_digest, expected.dependency_digest)
    ]


def _identity_with_receipt_package(tmp_path: Path, monkeypatch, package_dir) -> None:
    """Revalidate a real bundle whose probe receipt names the Lab package at ``package_dir``."""
    from types import SimpleNamespace

    from evolution_lab.owner import binding
    from evolution_lab.owner.binding import (
        declared_executor_identity,
        open_bundle,
        revalidate_executor_identity,
    )
    from evolution_lab.preparation.storage import PreparationStorage

    saved = make_prepared(tmp_path)
    bound = open_bundle(PreparationStorage(saved["state"]), saved["bundle"].digest)
    declared = declared_executor_identity(bound)
    original = binding._recapture

    def recapture(*args, **kwargs):
        receipt = json.loads(json.dumps(dict(original(*args, **kwargs).source_receipt)))
        for item in receipt["archive_wheels"]:
            if item["name"] == "evolution-lab":
                item["module_paths"]["evolution_lab"] = str(package_dir(Path) / "__init__.py")
        return SimpleNamespace(source_receipt=receipt)

    monkeypatch.setattr(binding, "_recapture", recapture)
    return revalidate_executor_identity(bound, declared), declared


def test_identity_accepts_the_package_reached_through_a_directory_alias(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution_lab

    real = Path(evolution_lab.__file__).resolve().parent
    alias = tmp_path / "site-alias"
    alias.symlink_to(real.parent, target_is_directory=True)

    result, declared = _identity_with_receipt_package(
        tmp_path, monkeypatch, lambda _: alias / real.name
    )

    assert result == declared


def test_identity_refuses_modules_outside_the_attested_package_copy(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution_lab
    from evolution_lab.owner import OwnerError

    real = Path(evolution_lab.__file__).resolve().parent
    copy = tmp_path / "copy" / real.name
    shutil.copytree(real, copy)

    with pytest.raises(OwnerError) as caught:
        _identity_with_receipt_package(tmp_path, monkeypatch, lambda _: copy)
    assert caught.value.code == "environment_invalid"
