"""V-S3-5: explicit public conformance and mandatory full target cleanup."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from ownership_modes import ownership_mode  # noqa: F401

from test_owner_target import make_bundle, ready_target

pytestmark = pytest.mark.slow


DECLARATION = {"kind": "core-session-public/1"}


def _nonconforming_launcher() -> str:
    source = (Path(__file__).parent / "owner_fixtures" / "launcher.py").read_text()
    marker = "server = TargetServer(\n    ReferenceBackend(),"
    replacement = """class _RefusingBackend:
    def load(self, tree):
        raise RuntimeError('neutral conformance refusal')

server = TargetServer(
    _RefusingBackend(),"""
    assert marker in source
    return source.replace(marker, replacement)


def _bundle(tmp_path: Path, *, declaration=DECLARATION, conforming=True):
    return make_bundle(
        tmp_path,
        conformance=declaration,
        max_instances=2,
        max_output_bytes=8192,
        launcher_source=None if conforming else _nonconforming_launcher(),
    )


def _payload(status):
    assert status.result is not None and status.result.kind == "conformance"
    return status.result.payload


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("expected_ok", [True, False])
def test_explicit_public_conformance_preserves_report_and_stops_target(
    tmp_path: Path, expected_ok: bool
) -> None:
    from evolution_lab.owner import Owner

    state, bundle, _ = _bundle(tmp_path, conforming=expected_ok)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            payload = _payload(status)
            assert status.action == "conformance"
            assert status.state == "succeeded", status.to_payload()
            assert payload["core_report"]["version"] == "conformance/v2"
            assert payload["core_report"]["ok"] is expected_ok
            assert payload["process"]["launched"] is True
            assert payload["process"]["cleanup_complete"] is True
            assert payload["cleanup"] == {"state": "confirmed", "complete": True}
            assert payload["target_cleanup"] == {"state": "confirmed", "complete": True}
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers
            reports = tuple((state / "scopes").glob("*/conformance-report.json"))
            assert len(reports) == 1
            assert json.loads(reports[0].read_text())["ok"] is expected_ok
            assert not any((state / "artifacts").glob(".conformance-*"))

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_launch_v2_target_passes_core_conformance(tmp_path: Path) -> None:
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, conformance=DECLARATION)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            payload = _payload(status)
            assert status.state == "succeeded", status.to_payload()
            assert payload["core_report"]["version"] == "conformance/v2"
            assert payload["core_report"]["ok"] is True
            checks = payload["core_report"]["checks"]
            suites = {check["suite"] for check in checks}
            assert suites == {"G", "W"}
            assert all(check["ok"] for check in checks), checks
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("schema", [15, 16])
@pytest.mark.parametrize("name", ["conformance.json", "probe-policy.json"])
def test_campaign_named_like_internal_entry_passes_conformance(tmp_path, schema, name):
    """User filenames under inputs/project/.lab are campaigns, not root Bundle internals."""
    from scaffold_15 import scaffold_15
    from scaffold_16 import scaffold_16
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    from evolution_lab.owner import Owner
    from evolution_lab.preparation import CampaignSource, EnvironmentInput, PreparationStore, SourceRoot

    state, _, _ = make_bundle(
        tmp_path, scaffold=scaffold_15 if schema == 15 else scaffold_16,
        conformance=DECLARATION, max_output_bytes=8192,
    )
    project = tmp_path / "project"
    project.mkdir()
    nested = (tmp_path / "source").rename(project / ".lab")
    manifest_path = (nested / "manifest.json").rename(nested / name)
    launch_path = nested / "launch.json"
    launch = json.loads(launch_path.read_text())
    launch["policy"] = name
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(state)
    bundle = store.create_bundle(
        source=CampaignSource(manifest_path, launch_path),
        environments=(EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL)),),
        env={"TARGET_MODE": "normal"}, secret_names=("TARGET_TOKEN",),
        roots=(SourceRoot("project", project),),
    )
    stored = store._storage.get_bundle(bundle.digest)
    assert stored.descriptor.source_kind == "campaign"
    assert stored.descriptor.conformance_entrypoint == f"inputs/project/.lab/{name}"
    before = {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()}

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            status = await asyncio.wait_for(owner.wait(await owner.conformance(connection, target_ref=target)), 20)
            assert status.state == "succeeded", status.to_payload()
            assert _payload(status)["core_report"]["ok"] is True
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())
    assert store.get_bundle(bundle.digest).digest == bundle.digest
    assert {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()} == before


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("declaration", [None, {"kind": "core-a5-session-public/1"}])
def test_conformance_requires_new_offline_declaration_before_go(
    tmp_path: Path, declaration
) -> None:
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = _bundle(tmp_path, declaration=declaration)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            with pytest.raises(OwnerError) as caught:
                await owner.conformance(connection, target_ref=target)
            assert caught.value.code == "conformance_unavailable"
            assert (await owner.target(target)).state == "ready"
            assert (await owner.wait(await owner.stop_target(target))).state == "succeeded"

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_stop_after_possible_go_owns_one_finalizer_and_refuses_public_target_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.preparation.models import RuntimePolicy

    state, bundle, _ = _bundle(tmp_path)
    reached_go = asyncio.Event()
    release_go = asyncio.Event()
    original_go = HelperHandle.go

    async def held_go(self):
        reached_go.set()
        await release_go.wait()
        return await original_go(self)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(HelperHandle, "go", held_go)
            task = await owner.conformance(
                connection,
                target_ref=target,
                runtime_policy=RuntimePolicy(.1, 8),
            )
            await asyncio.wait_for(reached_go.wait(), 8)
            with pytest.raises(OwnerError) as stopped:
                await owner.stop_target(target)
            assert stopped.value.code == "resource_busy"
            with pytest.raises(OwnerError) as cleaned:
                await owner.cleanup(target)
            assert cleaned.value.code == "resource_busy"
            receipt = await owner.stop(task)
            assert receipt.accepted is True and receipt.effect == "requested"
            release_go.set()
            status = await asyncio.wait_for(owner.wait(task), 12)
            payload = _payload(status)
            assert status.state == "failed", status.to_payload()
            assert payload["core_report"] is None
            assert payload["cleanup"]["complete"] is True
            assert payload["target_cleanup"]["complete"] is True
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_public_target_stop_stays_resource_busy_while_conformance_stops_target(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.target import TargetRuntime

    state, bundle, _ = _bundle(tmp_path)
    target_stop_started = asyncio.Event()
    release_target_stop = asyncio.Event()
    real_request_stop = TargetRuntime.request_stop

    async def held_request_stop(self):
        target_stop_started.set()
        await release_target_stop.wait()
        return await real_request_stop(self)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(TargetRuntime, "request_stop", held_request_stop)
            task = await owner.conformance(connection, target_ref=target)
            await asyncio.wait_for(target_stop_started.wait(), 20)
            try:
                assert (await owner.target(target)).state == "stopping"
                with pytest.raises(OwnerError) as stopped:
                    await owner.stop_target(target)
                assert stopped.value.code == "resource_busy"
            finally:
                release_target_stop.set()
            status = await asyncio.wait_for(owner.wait(task), 12)
            assert status.state == "succeeded", status.to_payload()

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_missing_target_receipt_retains_report_and_blockers_until_explicit_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner
    from evolution_lab.owner import target as target_module

    state, bundle, _ = _bundle(tmp_path)
    real_read = target_module.read_cleanup_receipt

    def absent(*args, **kwargs):
        raise FileNotFoundError("fixture hides target receipt")

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(target_module, "read_cleanup_receipt", absent)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            payload = _payload(status)
            assert status.state == "unknown", status.to_payload()
            assert payload["core_report"] is not None
            assert payload["cleanup"]["complete"] is True
            assert payload["target_cleanup"]["complete"] is None
            assert (await owner.target(target)).state == "cleanup_required"
            assert (await owner.health()).blockers
            monkeypatch.setattr(target_module, "read_cleanup_receipt", real_read)
            cleaned = await owner.wait(await owner.cleanup(task, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_conformance_persistence_failure_keeps_live_report(tmp_path: Path, monkeypatch) -> None:
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, _ = _bundle(tmp_path)
    real_finish = OwnerStateStore.finish_task

    def fail_conformance(self, task_id, **kwargs):
        current = self.task_status(task_id)
        if current.action == "conformance":
            raise OwnerError("io_error", "finalizing")
        return real_finish(self, task_id, **kwargs)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, "finish_task", fail_conformance)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            payload = _payload(status)
            assert payload["core_report"] is not None
            assert status.persistence.state == "failed"
            assert status.persistence.failure.code == "io_error"
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize(
    ("method_name", "expected_cleanup", "expected_target_cleanup", "expected_target_state"),
    [
        ("confirm_scope_cleanup", None, True, "stopped"),
        ("release_evidenced_reservations", True, True, "stopped"),
    ],
)
def test_cleanup_persistence_failure_settles_live_conformance_outcome(
    tmp_path: Path,
    monkeypatch,
    method_name: str,
    expected_cleanup: bool | None,
    expected_target_cleanup: bool | None,
    expected_target_state: str,
) -> None:
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, _ = _bundle(tmp_path)
    real_method = getattr(OwnerStateStore, method_name)
    attempts = 0

    def fail_persistence(self, *args, **kwargs):
        nonlocal attempts
        if method_name == "confirm_scope_cleanup":
            scope = self._database.execute(
                "SELECT task_id FROM scopes WHERE id=?", (args[0],)
            ).fetchone()
            if scope is not None and scope["task_id"] is None:
                return real_method(self, *args, **kwargs)
        if method_name == "release_evidenced_reservations" and args[0].kind != "task":
            return real_method(self, *args, **kwargs)
        attempts += 1
        raise OwnerError("io_error", "finalizing")

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, method_name, fail_persistence)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            payload = _payload(status)
            assert status.state == "unknown", status.to_payload()
            assert payload["core_report"] is not None
            assert payload["cleanup"]["complete"] is expected_cleanup
            assert payload["target_cleanup"]["complete"] is expected_target_cleanup
            assert status.persistence.state == "failed"
            assert status.persistence.failure.code == "io_error"
            assert (await owner.target(target)).state == expected_target_state
            assert (await owner.health()).blockers
            assert attempts >= 1

            monkeypatch.setattr(OwnerStateStore, method_name, real_method)
            cleaned = await owner.wait(await owner.cleanup(task, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_owner_death_leaves_conformance_orphan_without_restart(tmp_path: Path) -> None:
    from evolution_lab.owner import Owner, TaskRef
    from evolution_lab.owner.receipts import read_cleanup_receipt

    state, bundle, _ = _bundle(tmp_path)
    script = """import asyncio,json,sys
from evolution_lab.owner import Owner,TargetRef
async def main():
 async with Owner.open(sys.argv[1]) as owner:
  connection=await owner.bind(sys.argv[2])
  started=await owner.start_target(connection,secrets={'TARGET_TOKEN':'x'})
  target=TargetRef((await owner.wait(started)).ref.id,owner.owner_generation)
  task=await owner.conformance(connection,target_ref=target)
  print(json.dumps(task.to_payload()),flush=True)
  await asyncio.Event().wait()
asyncio.run(main())
"""

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            script,
            str(state),
            bundle.digest,
            stdout=asyncio.subprocess.PIPE,
        )
        raw = await asyncio.wait_for(process.stdout.readline(), 12)
        task = TaskRef.from_payload(json.loads(raw))
        process.send_signal(signal.SIGKILL)
        await process.wait()
        async with asyncio.timeout(12):
            while True:
                scopes = tuple((state / "scopes").iterdir())
                if any((scope / "cleanup.receipt").exists() for scope in scopes):
                    break
                await asyncio.sleep(.02)
        receipt_scope = next(scope for scope in scopes if (scope / "cleanup.receipt").exists())
        receipt = read_cleanup_receipt(receipt_scope, receipt_scope.name, task.owner_generation)
        assert receipt['schema'] == 'lab-scope-cleanup/2'
        async with Owner.open(state) as owner:
            status = await owner.task(task)
            assert status.state == "unknown"
            assert status.result is None
            assert (await owner.health()).blockers
            cleaned = await owner.wait(await owner.cleanup(task, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("window", ["pending", "running", "finalizing"])
@pytest.mark.parametrize("action", ["cleanup", "manual_release"])
@pytest.mark.parametrize("resource_ref", [False, True])
def test_active_conformance_task_cleanup_refuses_before_scope_effects(
    tmp_path: Path, monkeypatch, window: str, action: str, resource_ref: bool
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.owner.target import TargetRuntime

    state, bundle, _ = _bundle(tmp_path)
    reached = asyncio.Event()
    proceed = asyncio.Event()
    real_db = Owner._db
    real_go = HelperHandle.go
    real_stop = TargetRuntime.request_stop

    async def held_db(self, method, *args, **kwargs):
        if method == "task_running" and args[0] in self._conformance:
            reached.set()
            await proceed.wait()
        return await real_db(self, method, *args, **kwargs)

    async def held_go(self):
        reached.set()
        await proceed.wait()
        return await real_go(self)

    async def held_stop(self):
        reached.set()
        await proceed.wait()
        return await real_stop(self)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            if window == "pending":
                monkeypatch.setattr(Owner, "_db", held_db)
            elif window == "running":
                monkeypatch.setattr(HelperHandle, "go", held_go)
            else:
                monkeypatch.setattr(TargetRuntime, "request_stop", held_stop)
            task = await owner.conformance(connection, target_ref=target)
            await asyncio.wait_for(reached.wait(), 20)
            try:
                ref = ResourceRef("task", task.id) if resource_ref else task
                before = await owner.health()
                target_before = await owner.target(target)
                with pytest.raises(OwnerError) as refused:
                    if action == "cleanup":
                        await owner.cleanup(ref, wait_seconds=8)
                    else:
                        await owner.release_after_manual_cleanup(ref, reason="operator assertion")
                assert refused.value.code == "resource_busy"
                assert (await owner.health()).blockers == before.blockers
                assert await owner.target(target) == target_before
                if window == "finalizing":
                    with pytest.raises(OwnerError) as busy:
                        await owner.start_target(
                            connection, secrets={"TARGET_TOKEN": "private-neutral-token"}
                        )
                    assert busy.value.code == "busy"
            finally:
                proceed.set()
            status = await asyncio.wait_for(owner.wait(task), 20)
            assert status.state == "succeeded", status.to_payload()
            assert (await owner.target(target)).state == "stopped"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("action", ["cleanup", "manual_release"])
@pytest.mark.parametrize("persisted_state", ["pending", "running"])
def test_live_terminal_conformance_cleanup_survives_failed_task_persistence(
    tmp_path: Path, monkeypatch, action: str, persisted_state: str
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, _ = _bundle(tmp_path)
    real_finish = OwnerStateStore.finish_task
    real_release = OwnerStateStore.release_evidenced_reservations
    real_running = OwnerStateStore.task_running

    def fail_finish(self, task_id, **kwargs):
        if self.task_status(task_id).action == "conformance":
            raise OwnerError("io_error", "finalizing")
        return real_finish(self, task_id, **kwargs)

    def fail_release(self, ref, **kwargs):
        if ref.kind == "task" and self.task_status(ref.id).action == "conformance":
            raise OwnerError("io_error", "finalizing")
        return real_release(self, ref, **kwargs)

    def keep_pending(self, task_id):
        if self.task_status(task_id).action != "conformance":
            return real_running(self, task_id)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, "finish_task", fail_finish)
            monkeypatch.setattr(OwnerStateStore, "release_evidenced_reservations", fail_release)
            if persisted_state == "pending":
                monkeypatch.setattr(OwnerStateStore, "task_running", keep_pending)
            task = await owner.conformance(connection, target_ref=target)
            status = await asyncio.wait_for(owner.wait(task), 20)
            assert status.state == "unknown", status.to_payload()
            assert status.persistence.state == "failed"
            assert _payload(status)["core_report"] is not None
            assert (await owner._db("task_status", task.id)).state == persisted_state
            assert (await owner.health()).blockers
            monkeypatch.setattr(OwnerStateStore, "release_evidenced_reservations", real_release)
            ref = ResourceRef("task", task.id)
            if action == "cleanup":
                cleaned = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
                assert cleaned.state == "succeeded", cleaned.to_payload()
            else:
                receipt = await owner.release_after_manual_cleanup(ref, reason="operator cleanup")
                assert receipt.released_scopes
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_active_conformance_target_aliases_refuse_cleanup_and_manual_release(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle

    state, bundle, _ = _bundle(tmp_path)
    reached = asyncio.Event()
    proceed = asyncio.Event()
    real_go = HelperHandle.go

    async def held_go(self):
        reached.set()
        await proceed.wait()
        return await real_go(self)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, started = await ready_target(owner, bundle)
            monkeypatch.setattr(HelperHandle, "go", held_go)
            task = await owner.conformance(connection, target_ref=target)
            await asyncio.wait_for(reached.wait(), 8)
            try:
                for ref in (target, ResourceRef("target", target.id),
                            started, ResourceRef("task", started.id)):
                    with pytest.raises(OwnerError) as cleaned:
                        await owner.cleanup(ref, wait_seconds=8)
                    assert cleaned.value.code == "resource_busy"
                    with pytest.raises(OwnerError) as released:
                        await owner.release_after_manual_cleanup(ref, reason="operator assertion")
                    assert released.value.code == "resource_busy"
                assert (await owner.target(target)).state == "ready"
            finally:
                proceed.set()
            assert (await asyncio.wait_for(owner.wait(task), 20)).state == "succeeded"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


@pytest.mark.parametrize("schema", [None, "evolution-v2-manifest/1"])
@pytest.mark.parametrize("name", ["conformance.json", "probe-policy.json"])
def test_conformance_worker_reads_internal_entry(tmp_path, monkeypatch, schema, name):
    import evolution.tree as tree
    from evolution_lab.owner._conformance_worker import _run
    from evolution_lab.preparation.evaluation import conformance_payload
    from test_preparation_evaluation import _evaluation_request

    request = _evaluation_request(tmp_path, monkeypatch)
    value = json.loads(request["source"].input_path.read_text())
    state = tmp_path / "state"
    path = state / "bundles" / ("a" * 64) / ".lab" / name
    path.parent.mkdir(parents=True)
    document = conformance_payload(value, seed_root=str(request["source"].input_path.parent / "seed"))
    if schema is None:
        document.pop("schema")
    path.write_text(json.dumps(document))
    before = path.read_bytes()

    async def run(manifest, *, workdir):
        assert manifest.workflow is None
        assert manifest.target.base_url == value["target"]["base_url"]
        assert manifest.seed_root == request["source"].input_path.parent / "seed"
        from types import SimpleNamespace
        return SimpleNamespace(ok=True, to_payload=lambda: {"ok": True})

    monkeypatch.setattr(tree, "run_conformance", run)
    report = tmp_path / "report.json"
    assert asyncio.run(_run({"manifest_path": str(path), "allowed_artifact_root": str(state / "artifacts"),
                             "workdir": str(tmp_path), "report_path": str(report)})) == 0
    assert json.loads(report.read_bytes()) == {"ok": True}
    assert path.read_bytes() == before


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("kind", ["evaluation", "probe"])
def test_owner_binds_internal_entry_without_changing_bundle(tmp_path, monkeypatch, kind):
    from evolution_lab.owner import Owner
    from evolution_lab.preparation import PreparationStore
    if kind == "evaluation":
        from test_owner_evaluation import _prepared
        state, summary, _, _ = _prepared(tmp_path, monkeypatch)
        digest = summary.digest
    else:
        from test_owner_probe_bundle import make_probe_bundle
        state, digest = make_probe_bundle(tmp_path)
    stored = PreparationStore(state)._storage.get_bundle(digest)
    before = {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()}

    async def scenario():
        async with Owner.open(state) as owner:
            connection = await owner.bind(digest)
            assert connection.source_kind == kind
            assert connection.bundle_digest == digest

    asyncio.run(scenario())
    assert PreparationStore(state).get_bundle(digest).digest == digest
    assert {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()} == before


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("legacy,code", [("manifest", "manifest_schema"), ("run", "campaign_family_unsupported")])
def test_old_core_input_refusal_reaches_owner_client_without_measurement(tmp_path, monkeypatch, legacy, code):
    """Inject the old input after Lab's pre-dispatch validation; Core refuses before any experiment.

    The subprocess uses the real worker's diagnostic publication and Owner finalization, not a
    fabricated refusal. The old run prefix is the existing neutral family-13 fixture.
    """
    from evolution_lab.owner import Owner, ResourceRef, execution
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished

    state, bundle, _ = make_bundle(tmp_path, max_instances=4)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    old_manifest = tmp_path / "old-manifest.json"
    old_manifest.write_text(json.dumps({"schema": "evolution-v2-manifest/14"}))
    prefix = Path(__file__).parent / "fixtures/campaign-family-refusal/campaign.jsonl"
    script = """import os,sys,shutil
from pathlib import Path
from evolution_lab.owner.protocol import read_frame, MAX_CONFIG_BYTES
from evolution_lab.owner._campaign_worker import run_worker
fd=int(os.environ.pop('LAB_CAMPAIGN_CONFIG_FD'))
config=read_frame(fd,max_bytes=MAX_CONFIG_BYTES)
os.close(fd)
if sys.argv[1]=='manifest': config['manifest_path']=sys.argv[2]
else: shutil.copyfile(sys.argv[3],Path(config['run_directory'])/'campaign.jsonl')
sys.exit(run_worker(config,dict(os.environ),lambda:False))
"""
    monkeypatch.setattr(execution, "campaign_worker_command", lambda executable: [
        str(executable), "-I", "-c", script, legacy, str(old_manifest), str(prefix),
    ])

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            status = await finished(owner, ref, seconds=20)
            payload = status.to_payload()
            assert status.process.exit_code == 2, payload
            assert payload["failure"] == {"code": "core_refused", "stage": "running",
                                         "message": "The core refused the campaign."}
            assert payload["refusal"]["code"] == code
            assert payload["starts"][-1]["refusal"]["code"] == code
            run = state / "artifacts" / operation.id / operation.id
            assert not (run / "measurements").exists()
            if legacy == "run":
                assert (run / "campaign.jsonl").read_bytes() == prefix.read_bytes()
            else:
                assert not (run / "campaign.jsonl").exists()
            assert (await owner.status(ref)).refusal.code == code
            assert (await owner.wait(await owner.cleanup(ref))).state == "succeeded"
            assert (await owner.status(ref)).refusal.code == code

    asyncio.run(scenario())
