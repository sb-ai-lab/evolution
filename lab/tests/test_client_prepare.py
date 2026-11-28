"""H1 public preparation/check surface without real target requests."""
import json
import os
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from platforms import linux_only
from scaffold_15 import scaffold_15
from evolution.tree.execution.interaction import RolloutInfrastructureError


def sample(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    scaffold_15(root)
    return root / "manifest.json"


def command_grouping(manifest: Path, *, timeout_seconds: float = 17) -> Path:
    grouping = manifest.parent / "grouping"
    grouping.mkdir()
    script = grouping / "group.py"
    script.write_text(
        "from pathlib import Path\nPath('DISPATCHED').write_text('unexpected')\n"
    )
    dependency = grouping / "dependency.txt"
    dependency.write_text("grouping dependency\n")
    raw = json.loads(manifest.read_text())
    raw["grouper"] = {
        "kind": "command",
        "argv": ["python3", "group.py"],
        "cwd": "grouping",
        "timeout_seconds": timeout_seconds,
        "dependency_paths": ["grouping/dependency.txt"],
        "allowed_env": ["GROUPING_SECRET"],
        "config": {"opaque_locator": "leave/this/literal"},
    }
    manifest.write_text(json.dumps(raw))
    return script


def test_public_models_have_closed_fresh_json_projections(tmp_path):
    from evolution_lab.client import (
        CheckResult,
        Failure,
        RunResult,
        StatusSnapshot,
        StopReceipt,
        connect,
    )

    connection = connect(sample(tmp_path))
    failure = Failure("lab_error", "running")
    checked = CheckResult("failed", failure)
    result = RunResult(
        operation_id="operation-1",
        state="failed",
        stage="running",
        launched=True,
        exit_code=1,
        stop_requested=False,
        stop_reason=None,
        stop_effect="not_requested",
        core_force_requested=False,
        cleanup_complete=True,
        failure=failure,
        secondary_failures=(Failure("io_error", "finalizing"),),
        persistence_state="failed",
        persistence_failure=Failure("io_error", "finalizing"),
    )
    status = StatusSnapshot(
        operation_id="operation-1",
        state="failed",
        stage="done",
        launched=True,
        exit_code=1,
        stop_requested=False,
        cleanup_complete=True,
        result=result,
        campaign_availability="available",
        campaign_projection={"state": "partial", "items": [1]},
        campaign_failure=None,
    )
    receipt = StopReceipt("operation-1", "already_terminal")

    assert set(connection.public()) == {"schema", "manifest_digest", "editor_kind"}
    assert connection.public()["editor_kind"] == "subprocess"
    assert checked.public() == {
        "schema": "lab-check/1",
        "state": "failed",
        "failure": {"code": "lab_error", "stage": "running"},
    }
    assert set(result.public()) == {
        "schema", "operation_id", "state", "stage", "launched", "exit_code",
        "stop_requested", "stop_reason", "stop_effect", "core_force_requested",
        "cleanup_complete", "failure", "secondary_failures", "persistence",
    }
    assert set(status.public()) == {
        "schema", "operation_id", "state", "stage", "launched", "exit_code",
        "stop_requested", "cleanup_complete", "result", "campaign",
    }
    assert receipt.public() == {
        "schema": "lab-stop/1", "operation_id": "operation-1",
        "disposition": "already_terminal",
    }
    first = status.public()
    first["campaign"]["projection"]["items"].append(2)
    assert status.public()["campaign"]["projection"]["items"] == [1]
    assert json.loads(json.dumps(status.public())) == status.public()


def test_connect_pins_manifest_and_prepare_refuses_manifest_drift(tmp_path):
    from evolution_lab.client import ClientError, connect, prepare

    manifest = sample(tmp_path)
    connection = connect(manifest)
    raw = json.loads(manifest.read_text())
    raw["campaign"]["rounds"] = 3
    manifest.write_text(json.dumps(raw))

    with pytest.raises(ClientError) as caught:
        prepare(connection, wheel_paths=(), extra_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "inputs_changed", "stage": "prepare"}


def test_connect_accepts_manifest_15_and_refuses_manifest_3(tmp_path):
    from evolution_lab.client import ClientError, connect

    manifest = sample(tmp_path)
    assert connect(manifest).public()["schema"] == "lab-connection/1"

    raw = json.loads(manifest.read_text())
    raw["schema"] = "evolution-v2-manifest/3"
    raw["campaign"].pop("attempt_feedback")
    for field in ("selection", "families"):
        raw["reflector"].pop(field)
    manifest.write_text(json.dumps(raw))

    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}


@pytest.mark.parametrize("schema", ["evolution-v2-manifest/12", "evolution-v2-manifest/13"])
def test_connect_refuses_manifest_12_and_13(tmp_path, schema):
    """Only /15 connects; /12 and /13 remain outside Lab admission."""
    from evolution_lab.client import ClientError, connect

    manifest = sample(tmp_path)
    raw = json.loads(manifest.read_text())
    raw["schema"] = schema
    manifest.write_text(json.dumps(raw))

    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}


def test_connect_accepts_core_scaffold_manifest_15(tmp_path):
    """The pinned core's own /15 scaffold (statistical gate, unconverted) connects: /15 is Lab's main schema."""
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2
    from evolution.tree.reference_target_cli import scaffold
    from evolution_lab.client import connect
    manifest = scaffold(tmp_path / "sample").manifest
    assert load_manifest_v2(manifest).schema == "evolution-v2-manifest/15"
    assert connect(manifest).public()["schema"] == "lab-connection/1"


def test_connect_refuses_15_without_gate_mode_with_core_diagnosis(tmp_path):
    from scaffold_15 import scaffold_15
    from evolution_lab.client import ClientError, connect
    manifest = scaffold_15(tmp_path / "sample").manifest
    raw = json.loads(manifest.read_text())
    del raw["campaign"]["gate"]["mode"]
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}
    assert caught.value.detail == "missing_key at campaign.gate.mode: expected exactly 'statistical' or 'observe'"
    assert str(caught.value) == "manifest_invalid: " + caught.value.detail


def test_connect_refuses_a_linked_run(tmp_path):
    from evolution_lab.client import ClientError, connect

    manifest = sample(tmp_path)
    raw = json.loads(manifest.read_text())
    raw["linked_from"] = {"run_dir": "../previous-run", "baseline": "fresh"}
    manifest.write_text(json.dumps(raw))

    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "linked_run_unsupported", "stage": "connect"}


def test_connect_refuses_an_agent_cli_grouper(tmp_path):
    """PDR-0015: core accepts a /15 ``agent_cli`` Grouper; H1 ``connect`` refuses it before any effect."""
    from evolution.tree import load_manifest_v2
    from evolution_lab.client import ClientError, connect
    from scaffold_15 import scaffold_15

    manifest = scaffold_15(tmp_path / "sample").manifest
    raw = json.loads(manifest.read_text())
    raw["grouper"] = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 60}
    manifest.write_text(json.dumps(raw))
    assert type(load_manifest_v2(manifest).workflow.grouper).__name__ == "AgentCliGrouperSection"
    before = sorted(tmp_path.rglob("*"))

    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "grouper_kind_unsupported", "stage": "connect"}
    assert sorted(tmp_path.rglob("*")) == before


def test_prepare_accepts_reflection_metadata_and_pins_its_bytes(tmp_path):
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    corpus = manifest.parent / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    rows[0]["reflection_metadata"] = {"failure_family": "format", "rank": 2}
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))

    inputs = prepare_inputs(manifest, env={"PATH": os.environ["PATH"]})

    assert [case.case_id for case in inputs.cases] == ["q1", "q2"]
    corpus_pin = next(pin for pin in inputs.files if pin.path == corpus.resolve())
    assert corpus_pin.sha256 == "c7d16c7a6cccc0e379cc333df9ef2a090715afc3e9cedd8ed1affd2cd1f84305"


def test_grouping_command_is_pinned_without_dispatch_and_uses_only_explicit_env(
    tmp_path, monkeypatch
):
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    script = command_grouping(manifest)
    monkeypatch.setenv("GROUPING_SECRET", "AMBIENT-CANARY")
    inputs = prepare_inputs(
        manifest,
        env={"PATH": os.environ["PATH"], "GROUPING_SECRET": "EXPLICIT-CANARY"},
    )

    grouping = inputs.commands[-1]
    assert grouping.cwd == script.parent.resolve()
    assert grouping.child_env["GROUPING_SECRET"] == "EXPLICIT-CANARY"
    assert {grouping.executable, *grouping.proof_paths} <= {
        pin.path for pin in inputs.files
    }
    assert script.parent.resolve() in inputs.program_paths
    assert not (script.parent / "DISPATCHED").exists()

    monkeypatch.setenv("GROUPING_SECRET", "ROTATED-AMBIENT")
    inputs.verify()


def test_grouping_command_file_drift_is_detected(tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    script = command_grouping(manifest)
    inputs = prepare_inputs(
        manifest,
        env={"PATH": os.environ["PATH"], "GROUPING_SECRET": "EXPLICIT-CANARY"},
    )
    script.write_text("raise SystemExit(3)\n")

    with pytest.raises(InputChanged):
        inputs.verify()


def test_docs_inventory_keeps_full_utf8_tree_beyond_legacy_budget(tmp_path):
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    docs = manifest.parent / "docs"
    (docs / "nested").mkdir(parents=True)
    (docs / "guide.md").write_text("x" * 60001)
    (docs / "nested" / "é.txt").write_text("hello\n")
    raw = json.loads(manifest.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    manifest.write_text(json.dumps(raw))

    inputs = prepare_inputs(manifest, env={"PATH": os.environ["PATH"]})

    assert inputs.environment_docs_digest == (
        "c244368d2cec4f4c74f1c27584ca8a598a2e6202de6453a69971c3633122b7d7"
    )
    assert {pin.path for pin in inputs.files} >= {
        (docs / "guide.md").resolve(),
        (docs / "nested" / "é.txt").resolve(),
    }


@pytest.mark.parametrize("mutation", ["change", "add", "remove"])
def test_docs_inventory_detects_complete_tree_drift(tmp_path, mutation):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    docs = manifest.parent / "docs"
    docs.mkdir()
    original = docs / "guide.md"
    original.write_text("original\n")
    raw = json.loads(manifest.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    manifest.write_text(json.dumps(raw))
    inputs = prepare_inputs(manifest, env={"PATH": os.environ["PATH"]})

    if mutation == "change":
        original.write_text("changed\n")
    elif mutation == "add":
        (docs / "added.md").write_text("added\n")
    else:
        original.unlink()

    with pytest.raises(InputChanged):
        inputs.verify()


@pytest.mark.parametrize("invalid", ["empty", "symlink", "non_utf8", pytest.param(
    "bad_name", marks=linux_only("APFS refuses non-UTF-8 file names"))])
def test_docs_inventory_refuses_invalid_tree_entries(tmp_path, invalid):
    from evolution_lab.recipe import PreparationArtifactsInvalid, prepare_inputs

    manifest = sample(tmp_path)
    docs = manifest.parent / "docs"
    docs.mkdir()
    if invalid == "symlink":
        (docs / "link").symlink_to(manifest.parent / "corpus.jsonl")
    elif invalid == "non_utf8":
        (docs / "bad.bin").write_bytes(b"\xff")
    elif invalid == "bad_name":
        descriptor = os.open(os.fsencode(docs) + b"/bad-\xff", os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
    raw = json.loads(manifest.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    manifest.write_text(json.dumps(raw))

    with pytest.raises(PreparationArtifactsInvalid):
        prepare_inputs(manifest, env={"PATH": os.environ["PATH"]})


def test_prepare_classifies_corpus_rejection_as_artifacts_invalid(tmp_path):
    from evolution_lab.client import ClientError, connect, prepare

    manifest = sample(tmp_path)
    connection = connect(manifest)
    (manifest.parent / "corpus.jsonl").write_text("not-json\n")

    with pytest.raises(ClientError) as caught:
        prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "artifacts_invalid", "stage": "prepare"}


def test_prepare_classifies_an_initially_unsafe_seed_as_artifacts_invalid(tmp_path):
    from evolution_lab.client import ClientError, connect, prepare

    manifest = sample(tmp_path)
    connection = connect(manifest)
    (manifest.parent / "seed" / "unsafe-link").symlink_to("uv.lock")

    with pytest.raises(ClientError) as caught:
        prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {
        "code": "artifacts_invalid", "stage": "prepare"
    }


def test_prepare_maps_presealed_input_drift_to_inputs_changed(tmp_path, monkeypatch):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))
    monkeypatch.setattr(
        client,
        "prepare_inputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(client.InputChanged()),
    )

    with pytest.raises(client.ClientError) as caught:
        client.prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "inputs_changed", "stage": "prepare"}


@pytest.mark.parametrize("mutation", ["delete", "symlink"])
def test_prepare_rechecks_connected_manifest_before_reporting_artifact_failure(
    tmp_path, monkeypatch, mutation
):
    import evolution_lab.client as client

    manifest = sample(tmp_path)
    connection = client.connect(manifest)

    def fail_after_manifest_drift(*args, **kwargs):
        manifest.unlink()
        if mutation == "symlink":
            manifest.symlink_to("corpus.jsonl")
        raise client.PreparationArtifactsInvalid()

    monkeypatch.setattr(client, "prepare_inputs", fail_after_manifest_drift)
    with pytest.raises(client.ClientError) as caught:
        client.prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "inputs_changed", "stage": "prepare"}


def test_prepare_reports_command_drift_between_binding_and_file_pin_as_inputs_changed(
    tmp_path, monkeypatch
):
    import evolution_lab.client as client
    import evolution_lab.recipe as recipe

    manifest = sample(tmp_path)
    connection = client.connect(manifest)
    scorer_script = manifest.parent / "scorer" / "score.py"
    capture = recipe.FilePin.capture

    def capture_after_injected_drift(cls, path):
        if Path(path) == scorer_script and scorer_script.exists():
            scorer_script.unlink()
        return capture(path)

    monkeypatch.setattr(recipe.FilePin, "capture", classmethod(capture_after_injected_drift))
    with pytest.raises(client.ClientError) as caught:
        client.prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "inputs_changed", "stage": "prepare"}


def test_command_binding_rejection_is_artifacts_invalid(tmp_path):
    from evolution_lab.client import ClientError, connect, prepare

    manifest = sample(tmp_path)
    raw = json.loads(manifest.read_text())
    raw["scorer"]["argv"][0] = "/definitely/missing/program"
    manifest.write_text(json.dumps(raw))
    connection = connect(manifest)

    with pytest.raises(ClientError) as caught:
        prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {"code": "artifacts_invalid", "stage": "prepare"}


def test_prepare_keeps_an_independent_env_copy_and_exports_no_values(tmp_path, monkeypatch):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))
    captured = []
    prepared_inputs = SimpleNamespace(
        manifest=connection._manifest,
        files=(SimpleNamespace(
            path=connection._manifest_pin.path,
            sha256=connection.manifest_digest,
        ),),
        seed=SimpleNamespace(candidate_digest="a" * 64, dependency_digest="b" * 64),
        cases=(object(), object()),
        wheels=(
            SimpleNamespace(name="evolution-lab", version="0.0.3.dev3", sha256="c" * 64),
            SimpleNamespace(name="evo-skills", version="0.3.0a5", sha256="d" * 64),
        ),
    )

    def fake_prepare_inputs(path, *, wheel_paths, extra_paths, env, schemas):
        assert schemas == client.LEGACY_CAMPAIGN_MANIFEST_SCHEMAS
        prepared_inputs._environment = MappingProxyType(dict(env))
        captured.append((env, prepared_inputs._environment))
        return prepared_inputs

    monkeypatch.setattr(client, "prepare_inputs", fake_prepare_inputs)
    monkeypatch.setattr(client, "verify_isolated", lambda pins, *, env=None: None)
    supplied = {"PATH": "/first/bin", "TOKEN": "PRIVATE-CANARY"}
    prepared = client.prepare(connection, wheel_paths=(Path("lab.whl"), Path("core.whl")), env=supplied)
    supplied["TOKEN"] = "rotated"

    assert dict(client._prepared_environment(prepared)) == {
        "PATH": "/first/bin", "TOKEN": "PRIVATE-CANARY"
    }
    assert captured[0][0] is supplied
    assert captured[0][1] is client._prepared_environment(prepared)
    assert "PRIVATE-CANARY" not in json.dumps(prepared.public())
    assert prepared.public() == {
        "schema": "lab-prepared/1",
        "manifest_digest": connection.public()["manifest_digest"],
        "seed": {"candidate_digest": "a" * 64, "dependency_digest": "b" * 64},
        "case_count": 2,
        "distributions": [
            {"name": "evolution-lab", "version": "0.0.3.dev3", "wheel_sha256": "c" * 64},
            {"name": "evo-skills", "version": "0.3.0a5", "wheel_sha256": "d" * 64},
        ],
    }


def test_prepare_requires_both_producing_distributions_and_supported_platform(tmp_path, monkeypatch):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))
    fake = SimpleNamespace(
        manifest=connection._manifest,
        files=(SimpleNamespace(
            path=connection._manifest_pin.path,
            sha256=connection.manifest_digest,
        ),),
        seed=SimpleNamespace(candidate_digest="a", dependency_digest="b"),
        cases=(),
        wheels=(SimpleNamespace(name="evo-skills", version="x", sha256="c"),),
        _environment=MappingProxyType({}),
    )
    monkeypatch.setattr(client, "prepare_inputs", lambda *a, **k: fake)
    monkeypatch.setattr(client, "verify_isolated", lambda *a, **k: None)

    with pytest.raises(client.ClientError) as missing:
        client.prepare(connection, wheel_paths=(Path("core.whl"),), env={})
    assert missing.value.failure.public() == {"code": "environment_invalid", "stage": "prepare"}

    monkeypatch.setattr(client.sys, "platform", "win32")
    with pytest.raises(client.ClientError) as platform:
        client.prepare(connection, wheel_paths=(Path("lab.whl"), Path("core.whl")), env={})
    assert platform.value.failure.public() == {"code": "environment_invalid", "stage": "prepare"}


def test_prepare_admits_darwin(tmp_path, monkeypatch):
    import evolution_lab.client as client
    connection = client.connect(sample(tmp_path))
    def reached(*args, **kwargs):
        raise RuntimeError('preparation reached')
    monkeypatch.setattr(client.sys, 'platform', 'darwin')
    monkeypatch.setattr(client, 'prepare_inputs', reached)
    with pytest.raises(RuntimeError, match='preparation reached'):
        client.prepare(connection, wheel_paths=(Path('core.whl'), Path('lab.whl')), env={})


@pytest.mark.parametrize(
    "failure",
    [OSError("cannot spawn"), subprocess.TimeoutExpired(["python"], 60)],
    ids=["spawn-oserror", "timeout"],
)
def test_prepare_classifies_isolated_interpreter_failures_as_environment_invalid(
    tmp_path, monkeypatch, failure
):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))
    monkeypatch.setattr(client, "_REQUIRED_DISTRIBUTIONS", frozenset())

    def fail_isolated_spawn(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail_isolated_spawn)
    with pytest.raises(client.ClientError) as caught:
        client.prepare(connection, wheel_paths=(), env={"PATH": os.environ["PATH"]})
    assert caught.value.failure.public() == {
        "code": "environment_invalid", "stage": "prepare"
    }


@pytest.mark.parametrize(
    ("core_code", "public_code"),
    [
        ("transport_error", "target_unavailable"),
        ("not_ready", "target_unavailable"),
        ("callee_timeout", "target_unavailable"),
        ("internal", "target_unavailable"),
        ("pin_mismatch", "target_mismatch"),
        ("policy_mismatch", "target_mismatch"),
        ("strategy_unsupported", "target_mismatch"),
        ("request_conflict", "target_mismatch"),
        ("future_new_code", "lab_error"),
    ],
)
async def test_check_maps_only_core_codes_not_exception_text_or_class(
    tmp_path, monkeypatch, core_code, public_code
):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))

    class FakeAdapter:
        run_ids = []

        def __init__(self, binding, policy, *, run_id):
            self.run_ids.append(run_id)

        async def preflight(self, expected):
            assert expected.candidate_digest and expected.dependency_digest
            raise RolloutInfrastructureError("transport_error", core_code)

    monkeypatch.setattr(client, "HttpSessionAdapter", FakeAdapter)
    first = await client.check(connection)
    second = await client.check(connection)

    assert first.public() == {
        "schema": "lab-check/1", "state": "failed",
        "failure": {"code": public_code, "stage": "check"},
    }
    assert second.public() == first.public()
    assert len(set(FakeAdapter.run_ids)) == 2


async def test_check_passes_without_becoming_an_implicit_prepare_gate(tmp_path, monkeypatch):
    import evolution_lab.client as client

    connection = client.connect(sample(tmp_path))

    class FakeAdapter:
        def __init__(self, binding, policy, *, run_id):
            pass

        async def preflight(self, expected):
            return object()

    monkeypatch.setattr(client, "HttpSessionAdapter", FakeAdapter)
    assert (await client.check(connection)).public() == {
        "schema": "lab-check/1", "state": "passed", "failure": None,
    }


async def test_check_classifies_an_unsafe_seed_as_artifacts_invalid(tmp_path):
    from evolution_lab.client import check, connect

    manifest = sample(tmp_path)
    connection = connect(manifest)
    (manifest.parent / "seed" / "unsafe-link").symlink_to("uv.lock")

    assert (await check(connection)).public() == {
        "schema": "lab-check/1",
        "state": "failed",
        "failure": {"code": "artifacts_invalid", "stage": "check"},
    }


def test_connect_unknown_key_with_control_character_is_a_clean_manifest_invalid(tmp_path):
    from scaffold_15 import scaffold_15
    from evolution_lab.client import ClientError, connect
    manifest = scaffold_15(tmp_path / "sample").manifest
    raw = json.loads(manifest.read_text())
    raw["a\nb"] = 1
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}
    assert "a?b" in caught.value.detail and str(caught.value).isprintable()


@pytest.mark.parametrize('schema', ['evolution-v2-manifest/15', 'evolution-v2-manifest/16'])
def test_client_prepare_refuses_reserved_marker_name(tmp_path, monkeypatch, schema):
    import evolution_lab.client as client
    if schema.endswith('/16'):
        from scaffold_16 import scaffold_16
        path = scaffold_16(tmp_path / 'sample').manifest
    else:
        path = sample(tmp_path)
    value = json.loads(path.read_text())
    value['scorer']['allowed_env'] = ['EVOLUTION_LAB_SCOPE']
    path.write_text(json.dumps(value))
    if schema.endswith('/16'):
        from evolution_lab._manifest_schema import CAMPAIGN_MANIFEST_SCHEMAS
        connection = client._connect(path, CAMPAIGN_MANIFEST_SCHEMAS)
        def prepare():
            return client._prepare(connection, CAMPAIGN_MANIFEST_SCHEMAS,
                wheel_paths=(Path('core.whl'), Path('lab.whl')), env={})
    else:
        connection = client.connect(path)
        def prepare():
            return client.prepare(connection, wheel_paths=(Path('core.whl'), Path('lab.whl')), env={})
    with pytest.raises(client.ClientError) as failure:
        prepare()
    assert failure.value.failure.public() == {'code': 'reserved_environment_name', 'stage': 'prepare'}
