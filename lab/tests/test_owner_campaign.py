"""V-S2-4: installed public-core campaign worker and tracked adapter."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
from threading import Thread
import uuid

import pytest

from test_owner_target import make_bundle
from scaffold_15 import scaffold_15_with_legacy_failures

pytestmark = pytest.mark.slow


def _admission_config(tmp_path):
    """Real receipt identity and paths, even when Core admission is stubbed."""
    return {"manifest_path": "unused.json", "operation_id": "a" * 32,
            "scope_id": "b" * 32, "owner_generation": "c" * 32,
            "diagnostic_path": str(tmp_path / "diagnostic.json")}


@pytest.mark.parametrize("terminal,code", [
    ("storage_capacity_exhausted", "storage_inodes_insufficient"),
    ("persistence_failed", "reflector_budget_unavailable"),
])
def test_b5_storage_fault_result_keeps_terminal_owner_and_code(
    terminal, code, tmp_path, monkeypatch, capsys,
):
    """A typed result crosses the worker boundary without a campaign or storage fault."""
    from types import SimpleNamespace

    import evolution.tree.evolve as evolve_module
    from evolution.tree.campaign import CampaignResult
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution_lab.owner import _campaign_worker as worker, core_raw
    from evolution_lab.owner.campaign_views import ATTEMPT_GROUP, ATTEMPT_STAGE
    from evolution_lab.owner.settlement import positive_settlement, read_campaign_settlement

    # Reuse the existing neutral projection bytes; no campaign or journal is created.
    payload = json.loads((Path(__file__).parent / "fixtures/family14/connection-suspended-status.json").read_text())
    status = CampaignProjection.from_payload(payload)
    result = CampaignResult(
        terminal=terminal, quiescent=False, finished=None, owner="artifact_store", code=code,
        result=None, status=status, fault_detail="private storage detail",
    )
    admitted = SimpleNamespace(workflow=None, target=object())
    config = {"operation_id": "1" * 32, "scope_id": "2" * 32,
              "owner_generation": "3" * 32, "run_id": "c-neutral",
              "run_directory": str(tmp_path), "manifest_path": str(tmp_path / "manifest.json"),
              "settlement_path": str(tmp_path / "settlement.json"),
              "diagnostic_path": str(tmp_path / "diagnostic.json")}
    calls = []

    async def typed_result(manifest, root, **kwargs):
        assert manifest is admitted and root == tmp_path
        assert kwargs["environ"] == {} and kwargs["cancelled"]() is False
        calls.append(kwargs["ledger"])
        return result

    monkeypatch.setattr(evolve_module, "admit_manifest", lambda path: admitted)
    monkeypatch.setattr(evolve_module, "redactions_for", lambda *args: ())
    monkeypatch.setattr(evolve_module, "short_forwarded_names", lambda *args: ())
    monkeypatch.setattr(evolve_module, "evolve", typed_result)
    monkeypatch.setattr(worker, "settle_timeout", lambda target: .01)

    assert worker.run_worker(config, {}, lambda: False) == evolve_module.exit_code_for(result) == 1
    captured = capsys.readouterr()
    assert captured.err == "" and len(calls) == 1
    assert json.loads(captured.out) == {
        "terminal": terminal, "quiescent": False, "finished": None,
        "owner": "artifact_store", "code": code, "result": None, "status": status.to_payload(),
    }
    settlement = read_campaign_settlement(
        config["settlement_path"], operation_id=config["operation_id"], scope_id=config["scope_id"],
        owner_generation=config["owner_generation"], run_id=config["run_id"],
    )
    # Returning a storage terminal is a normal library return. The unused ledger is separately quiet.
    assert settlement["normal_return"] is True and settlement["ledger"]["quiescent"] is True
    assert settlement["ledger"]["admission_closed"] is True
    assert settlement["ledger"]["admitted"] == settlement["ledger"]["bound_transports"] == 0
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is False
    assert json.loads(Path(config["diagnostic_path"]).read_text()) == {
        "schema": "lab-campaign-diagnostic/5", "operation_id": config["operation_id"],
        "scope_id": config["scope_id"], "failure": None, "refusal": None,
        "core_terminal": {"owner": "artifact_store", "terminal": terminal, "code": code}, "findings": [], "exception": None,
    }
    assert not (tmp_path / "campaign.jsonl").exists()
    assert code not in ATTEMPT_GROUP and code not in ATTEMPT_STAGE
    assert code not in core_raw.suspension_reasons()


async def _wait_for(path: Path, *, seconds: float = 8) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"timed out waiting for {path.name}")
        await asyncio.sleep(.01)


@pytest.mark.parametrize("schema,checks,expected", [
    (15, None, ""),
    (16, None, ""),
    (16, {"connection_preflight": "off", "seal_oracle_leak": "shadow"},
     "evo tree evolve: note: check seal_oracle_leak is shadow: a Candidate that writes an oracle string into the skill is measured and can be promoted\n"
     "evo tree evolve: note: check connection_preflight is off: the campaign starts without a passed connection preflight\n"),
])
def test_worker_start_and_resume_check_notes_match_core_cli(
    tmp_path, monkeypatch, capsys, schema, checks, expected,
):
    """Missing, reordered or differently prefixed start/resume notes break Core parity."""
    import evolution.tree.evolve as evolve_module
    from evolution.cli.app import app
    from evolution.tree import load_manifest_v2
    import evolution.tree.cli  # register the Core command
    from typer.testing import CliRunner
    from scaffold_15 import scaffold_15
    from scaffold_16 import scaffold_16
    from evolution_lab.owner import _campaign_worker as worker

    manifest = (scaffold_15 if schema == 15 else scaffold_16)(tmp_path / "source").manifest
    document = json.loads(manifest.read_text())
    if checks is not None:
        document["checks"] = {"version": "checks/1", "modes": checks}
    manifest.write_text(json.dumps(document))
    admitted = load_manifest_v2(manifest)

    class BeforeExecution(Exception):
        pass

    async def stop_before_execution(*args, **kwargs):
        raise BeforeExecution

    monkeypatch.setattr(evolve_module, "admit_manifest", lambda _: admitted)
    monkeypatch.setattr(evolve_module, "redactions_for", lambda *args: ())
    monkeypatch.setattr(evolve_module, "short_forwarded_names", lambda *args: ())
    monkeypatch.setattr(evolve_module, "evolve", stop_before_execution)
    monkeypatch.setattr(worker, "settle_timeout", lambda _: .01)
    invocation = CliRunner().invoke(app, ["tree", "evolve", "--manifest", str(manifest),
                                         "--run-dir", str(tmp_path / "core-run")])
    assert isinstance(invocation.exception, BeforeExecution)
    assert invocation.stderr == expected
    for mode in ("new", "continue"):
        config = {"mode": mode, "run_id": "neutral-run", "run_directory": str(tmp_path),
                  "settlement_path": str(tmp_path / "settlement.json"),
                  "scope_id": "1" * 32, "owner_generation": "2" * 32, "operation_id": "3" * 32}
        with pytest.raises(BeforeExecution):
            asyncio.run(worker._evolve(config, {}, lambda: False, admitted, (), ()))
        assert capsys.readouterr().err == expected
    assert not (tmp_path / "campaign.jsonl").exists()


@pytest.mark.parametrize("mode", ["new", "continue"])
@pytest.mark.parametrize("terminal", ["suspended", "persistence_failed"])
def test_worker_prints_unencrypted_warning_and_provider_attempt_lines_after_report(
    tmp_path, monkeypatch, mode, terminal,
):
    """b10 public helpers/result only: no provider, target, environment lookup or journal."""
    from types import SimpleNamespace

    import evolution.tree.evolve as evolve_module
    from evolution.tree.campaign import CampaignResult
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution.tree.roles.config import ChatEditorSection
    from evolution_lab.owner import _campaign_worker as worker

    secret = "NEUTRAL-SECRET-123"
    url = f"http://provider.example/{secret}"
    workflow = SimpleNamespace(
        checks=None,
        editor=ChatEditorSection("neutral", url, "DECLARED_KEY"),
        reflector=SimpleNamespace(role=ChatEditorSection("neutral", url, None)),
        grouper=ChatEditorSection("neutral", "http://localhost:1234", None),
    )
    admitted = SimpleNamespace(workflow=workflow, target=object())
    payload = json.loads((Path(__file__).parent / "fixtures/family14/connection-suspended-status.json").read_text())
    result = CampaignResult(
        terminal=terminal, quiescent=True, finished=None,
        owner="campaign_journal" if terminal == "persistence_failed" else "editor",
        code="record_matches_secret" if terminal == "persistence_failed" else "editor_provider_failed",
        status=CampaignProjection.from_payload(payload), result=None,
        provider_attempt_lines=(f"editor provider attempt 1 {secret}", "editor provider attempt 2 failed"),
    )
    config = {"mode": mode, "run_id": "neutral-run", "run_directory": str(tmp_path),
              "settlement_path": str(tmp_path / "settlement.json"),
              "scope_id": "1" * 32, "owner_generation": "2" * 32, "operation_id": "3" * 32}
    writes = []
    monkeypatch.setattr(worker, "_write", lambda stream, value: writes.append((stream, value)))
    monkeypatch.setattr(worker, "settle_timeout", lambda _: .01)

    async def fake_evolve(manifest, root, **kwargs):
        assert manifest is admitted and root == tmp_path and kwargs["environ"] == {}
        # Before the evolve call, all declared endpoint lines have already arrived.
        assert len(writes) == 4
        kwargs["notes"](f"editor recovered {secret}")
        return result

    monkeypatch.setattr(evolve_module, "evolve", fake_evolve)
    assert asyncio.run(worker._evolve(config, {}, lambda: False, admitted, (secret,), ())) == 1
    lines = [text.rstrip("\n") for _, text in writes]
    assert secret not in "\n".join(lines)
    safe_url = url.replace(secret, "[REDACTED]")
    assert lines[:4] == [
        f"evo tree evolve: WARNING: editor sends its credential unencrypted to {safe_url}",
        f"evo tree evolve: WARNING: reflector talks to {safe_url} without encryption",
        f"evo tree evolve: reflector calls {safe_url} without credentials (no api_key_env declared)",
        "evo tree evolve: grouper calls http://localhost:1234 without credentials (no api_key_env declared)",
    ]
    assert lines[4] == "evo tree evolve: note: editor recovered [REDACTED]"
    assert writes[5][0] is sys.stdout and json.loads(lines[5])["terminal"] == terminal
    assert lines[-2:] == ["evo tree evolve: editor provider attempt 1 [REDACTED]",
                          "evo tree evolve: editor provider attempt 2 failed"]
    if terminal == "persistence_failed":
        assert "cannot continue this campaign in Lab" in lines[6]
        assert "describe()" in lines[6] and "new campaign" in lines[6]
    else:
        assert len(lines) == 8
    assert not (tmp_path / "campaign.jsonl").exists()


def test_worker_endpoint_and_attempt_output_oserror_does_not_change_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import evolution.tree.evolve as evolve_module
    from evolution.tree.campaign import CampaignResult
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution_lab.owner import _campaign_worker as worker

    payload = json.loads((Path(__file__).parent / "fixtures/family14/connection-suspended-status.json").read_text())
    result = CampaignResult(terminal="suspended", quiescent=True, finished=None,
                            owner="editor", code="editor_provider_failed", result=None,
                            status=CampaignProjection.from_payload(payload), provider_attempt_lines=("failed",))
    admitted = SimpleNamespace(workflow=SimpleNamespace(checks=None), target=object())
    monkeypatch.setattr(evolve_module, "unencrypted_model_roles", lambda _: (("editor", "http://example.test", True),))
    monkeypatch.setattr(evolve_module, "unauthenticated_roles", lambda _: (("reflector", "http://example.test"),))
    async def fake_evolve(*args, **kwargs):
        return result
    monkeypatch.setattr(evolve_module, "evolve", fake_evolve)
    monkeypatch.setattr(worker, "settle_timeout", lambda _: .01)
    def broken(*args):
        raise OSError("closed output")
    monkeypatch.setattr(worker, "_write", broken)
    config = {"run_id": "neutral-run", "run_directory": str(tmp_path),
              "settlement_path": str(tmp_path / "settlement.json"),
              "scope_id": "1" * 32, "owner_generation": "2" * 32, "operation_id": "3" * 32}
    assert asyncio.run(worker._evolve(config, {}, lambda: False, admitted, (), ())) == 1
    assert json.loads((tmp_path / "settlement.json").read_text())["normal_return"] is True


class _Target:
    def __init__(self, binding):
        from evolution.tree.target.reference_target import ReferenceBackend
        from evolution.tree.target.target_server import TargetServer, serve_target

        launch = binding.launch
        snapshots = binding.bundle_path.parent.parent / "campaign-snapshots"
        snapshots.mkdir(mode=0o700)
        limits = dict(launch["limits"])
        # Core admits the /15 observe policy with four instances at max_concurrency=2.
        limits["max_instances"] = max(4, limits["max_instances"])
        self.server = TargetServer(
            ReferenceBackend(),
            allowed_artifact_root=binding.bundle_path.parent.parent / "artifacts",
            snapshot_root=snapshots,
            policy=binding.manifest.policy,
            **dict(launch["identity"]),
            **limits,
        )
        endpoint = binding.manifest.target.base_url.rsplit(":", 1)
        self.http = serve_target(self.server, "127.0.0.1", int(endpoint[1]))
        self.thread = Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": .01}
        )
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)
        self.server.close()


def _campaign(tmp_path: Path, **bundle_options):
    from evolution_lab.owner.binding import open_bundle
    from evolution_lab.owner.execution import (
        campaign_worker_config,
        create_campaign_paths,
    )
    from evolution_lab.owner.store import OwnerStateStore
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation.storage import PreparationStorage

    state, bundle, _ = make_bundle(tmp_path, **bundle_options)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    store = OwnerStateStore.open(state)
    generation = store.generation
    store.close()
    binding = open_bundle(PreparationStorage(state), bundle.digest)
    operation_id, scope_id = uuid.uuid4().hex, uuid.uuid4().hex
    paths = create_campaign_paths(state, operation_id, scope_id)
    config = campaign_worker_config(
        operation_id=operation_id,
        scope_id=scope_id,
        owner_generation=generation,
        bundle_digest=bundle.digest,
        prepared_digest=prepared.digest,
        manifest_path=binding.manifest_path,
        run_directory=paths.run_directory,
        settlement_path=paths.settlement_path,
        diagnostic_path=paths.diagnostic_path,
    )
    scorer = binding.manifest.workflow.scorer.cwd
    assert scorer.is_relative_to(binding.bundle_path)
    assert not stat.S_IMODE(scorer.stat().st_mode) & 0o222
    return state, binding, paths, config


async def _helper_run(binding, paths, config, *, stop=False, env=None):
    from evolution_lab.owner.execution import campaign_worker_command
    from evolution_lab.owner.protocol import HelperHandle

    helper = HelperHandle.launch(
        sys.executable, paths.scope_directory, config["scope_id"],
        config["owner_generation"],
    )
    await helper.configure(
        argv=campaign_worker_command(sys.executable),
        env=dict(binding.base_environment if env is None else env),
        stop_grace_seconds=.5,
        role="campaign",
        child_config=config,
        cwd=str(paths.scope_directory),
    )
    await helper.wait_event("ready", timeout=3)
    await helper.go()
    await helper.wait_event("child_started", timeout=3)
    if stop:
        await _wait_for(paths.run_directory / "campaign.jsonl")
        await helper.stop()
    assert await helper.wait(timeout=20) == 0
    helper.close_commands()
    helper.close_target_ready()
    helper.close_events()
    return helper


def test_actual_campaign_worker_normal_and_fresh_namespace(tmp_path):
    from evolution_lab.owner.execution import create_campaign_paths
    from evolution_lab.owner.settlement import read_campaign_settlement
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.owner.receipts import read_cleanup_receipt

    state, binding, paths, config = _campaign(tmp_path)
    target = _Target(binding)
    try:
        helper = asyncio.run(_helper_run(binding, paths, config))
    finally:
        target.close()
    receipt = read_cleanup_receipt(
        paths.scope_directory, config["scope_id"], config["owner_generation"]
    )
    settlement = read_campaign_settlement(
        paths.settlement_path,
        operation_id=config["operation_id"], scope_id=config["scope_id"],
        owner_generation=config["owner_generation"], run_id=config["run_id"],
    )
    assert receipt["child_exit_code"] == 0 and receipt["forced"] is False
    assert settlement["schema"] == "lab-campaign-settlement/2"
    assert settlement["scope_id"] == config["scope_id"]
    assert settlement["owner_generation"] == config["owner_generation"]
    assert settlement["operation_id"] == config["operation_id"]
    assert settlement["run_id"] == config["run_id"]
    assert settlement["normal_return"] is True
    ledger = settlement["ledger"]
    assert ledger["namespace"] == config["run_id"]
    assert ledger["admission_closed"] is True
    assert ledger["quiescent"] is True
    assert ledger["uncertain"] == 0
    assert ledger["bound_transports"] >= 1
    assert ledger["admitted"] >= 1
    document = json.loads(paths.stdout_path.read_text())
    assert document["terminal"] == "finished"
    assert document["finished"]["outcome"] == "completed"
    assert json.loads(paths.diagnostic_path.read_text()) == {
        "schema": "lab-campaign-diagnostic/5", "operation_id": config["operation_id"],
        "scope_id": config["scope_id"], "failure": None, "refusal": None, "core_terminal": None, "findings": [], "exception": None,
    }
    with pytest.raises(OwnerError) as caught:
        create_campaign_paths(state, config["operation_id"], uuid.uuid4().hex)
    assert caught.value.code == "path_conflict"


def test_actual_campaign_worker_cooperative_stop_keeps_positive_settlement(tmp_path):
    from evolution_lab.owner.settlement import positive_settlement, read_campaign_settlement
    from evolution_lab.owner.receipts import read_cleanup_receipt

    _, binding, paths, config = _campaign(tmp_path)
    target = _Target(binding)
    try:
        asyncio.run(_helper_run(binding, paths, config, stop=True))
    finally:
        target.close()
    receipt = read_cleanup_receipt(
        paths.scope_directory, config["scope_id"], config["owner_generation"]
    )
    settlement = read_campaign_settlement(
        paths.settlement_path,
        operation_id=config["operation_id"], scope_id=config["scope_id"],
        owner_generation=config["owner_generation"], run_id=config["run_id"],
    )
    assert receipt["child_exit_code"] == 3 and receipt["forced"] is False
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is True
    assert json.loads(paths.stdout_path.read_text())["finished"]["outcome"] == "stopped"


def test_stdout_failure_does_not_replace_actual_core_exit(tmp_path):
    from evolution_lab.owner.execution import campaign_worker_command
    from evolution_lab.owner.settlement import read_campaign_settlement
    from evolution_lab.owner.protocol import encode_frame

    _, binding, paths, config = _campaign(tmp_path)
    target = _Target(binding)
    config_read, config_write = os.pipe()
    read_end, write_end = os.pipe()
    process = None
    try:
        environment = dict(binding.base_environment)
        environment["LAB_CAMPAIGN_CONFIG_FD"] = str(config_read)
        process = subprocess.Popen(
            campaign_worker_command(sys.executable), env=environment,
            cwd=paths.scope_directory, stdin=subprocess.DEVNULL,
            stdout=write_end, stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=(config_read,),
        )
        os.close(config_read)
        config_read = -1
        os.close(write_end)
        write_end = -1
        os.close(read_end)  # The worker's final stdout write receives EPIPE.
        read_end = -1
        os.write(config_write, encode_frame(config))
        os.close(config_write)
        config_write = -1
        assert process.wait(20) == 0
    finally:
        for descriptor in (config_read, config_write, read_end, write_end):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        target.close()
    assert read_campaign_settlement(
        paths.settlement_path,
        operation_id=config["operation_id"], scope_id=config["scope_id"],
        owner_generation=config["owner_generation"], run_id=config["run_id"],
    )["normal_return"] is True


def test_settle_timeout_uses_binding_timeouts():
    from types import SimpleNamespace

    from evolution_lab.owner.settlement import settle_timeout

    binding = SimpleNamespace(timeouts=SimpleNamespace(connect_seconds=2, read_seconds=10))
    assert settle_timeout(binding) == 21.0


def test_owner_campaign_normal_and_cooperative_stop_reuse_same_target(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            boot = (await owner.target(target)).boot_id
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            value = await finished(owner, ref)
            assert value.control_state == 'finished', value.to_payload()
            assert value.process.exit_code == 0
            assert value.process.cleanup_complete is True
            assert value.target_cleanup.complete is True
            assert value.result.kind == 'campaign' and value.result.coverage is None
            assert value.result.campaign_execution is None and value.result.core is not None
            report = await owner.report(ref)
            assert report.state == 'available'
            assert report.reflector_probe == {
                'launched_by': None, 'state': 'not_declared', 'restarts': 0, 'last_exit': None}
            assert (report.reflector_probe_state, report.reflector_probe_reason) == ('present', None)
            markdown = await owner.report_markdown(ref)
            assert (markdown.state, markdown.reason) == ('available', None)
            from evolution_lab.report import read_campaign_report
            run = state / 'artifacts' / ref.id / ref.id
            standalone = read_campaign_report(run, owner_run=True)
            assert (standalone.data['reflector_probe'], standalone.data['reflector_probe_state'],
                    standalone.data['reflector_probe_reason']) == (None, 'unavailable', 'unreadable')
            assert 'reflector probe: unavailable (unreadable)' in standalone.markdown()
            assert markdown.markdown == read_campaign_report(
                run, owner_run=True, owner_reflector_probe=report.reflector_probe).markdown()
            second = await owner.start(prepared.digest, target_ref=target, secrets={})
            second_ref = ResourceRef('operation', second.id)
            await _wait_for(state / 'artifacts' / second.id / second.id / 'campaign.jsonl')
            assert (await owner.stop(second_ref)).effect == 'requested'
            stopped = await finished(owner, second_ref)
            assert stopped.process.exit_code == 3, stopped.to_payload()
            assert stopped.control_state == 'finished' and stopped.target_cleanup.complete is True
            assert (await owner.target(target)).boot_id == boot
            assert (await owner.target(target)).state == 'ready'
            assert len(await owner.list_operations()) == 2
            assert (await owner.close()).cleanup.complete is True
    asyncio.run(scenario())


def test_owner_report_makes_one_core_acquisition(tmp_path, monkeypatch):
    """Spec 2026-10-19 D3: one authenticated Core acquisition per read. An Owner report request reads
    ``core_raw.campaign_read`` once, with the result, and its status metadata (resume target), usage and report all
    come from that read, so a measurement bound on a live campaign between two reads cannot reach the report
    without its usage row (codex branch review r1, family15-owner-report-double-acquisition)."""
    from evolution_lab.owner import Owner, ResourceRef, core_raw
    import evolution_lab.report as report_module
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    calls, reads = [], []
    original = core_raw.campaign_read

    def counted(run, **kwargs):
        calls.append(kwargs)
        reads.append(original(run, **kwargs))
        return reads[-1]

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            assert (await finished(owner, ref)).control_state == 'finished'
            monkeypatch.setattr(core_raw, "campaign_read", counted)
            monkeypatch.setattr(report_module, "campaign_read", counted)
            report = await owner.report(ref)
            monkeypatch.setattr(core_raw, "campaign_read", original)
            monkeypatch.setattr(report_module, "campaign_read", original)
            assert report.state == 'available', report.to_payload()
            assert calls == [{"families": core_raw.FAMILIES, "with_result": True, "with_bindings": True}]
            read, = reads
            core = read.result.to_payload()
            assert report.report["core"]["run_id"] == core["run_id"]
            assert sorted(row["measurement_id"] for row in report.usage) == sorted(
                entry["measurement_id"] for entry in read.status.to_payload()["measurements"])
            assert (await owner.close()).cleanup.complete is True
    asyncio.run(scenario())


def test_failed_finalization_of_a_campaign_without_a_result_still_shows_cleanup_required(
    tmp_path, monkeypatch
):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target

    state, bundle, prepared = prepared_target(tmp_path)

    async def refused_configure(self, **kwargs):
        raise OSError("helper refused its configuration")

    def fail_update(self, *args, **kwargs):
        raise OwnerError("io_error", "finalizing")

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            with monkeypatch.context() as local:
                # No child, no journal: the failure path's terminal value has no result, and its store
                # write fails too.
                local.setattr(HelperHandle, "configure", refused_configure)
                local.setattr(OwnerStateStore, "update_operation", fail_update)
                accepted = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef("operation", accepted.id)
                value = await finished(owner, ref)
            assert value.control_state == "cleanup_required", value.to_payload()
            assert value.result is None
            cleaned = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()

    asyncio.run(scenario())


def _published_campaign_settlement(paths, config):
    from evolution_lab.owner.settlement import read_campaign_settlement

    return read_campaign_settlement(
        paths.settlement_path,
        operation_id=config["operation_id"], scope_id=config["scope_id"],
        owner_generation=config["owner_generation"], run_id=config["run_id"],
    )


def _records(snapshot):
    return [
        (record.operation, record.state, record.abandoned, record.reason)
        for record in snapshot.records
    ]


def test_campaign_worker_turn_cancelled_after_its_send_started_publishes_uncertain(
    tmp_path, monkeypatch
):
    """The core rollout deadline cancels a `send` whose worker thread is already
    inside the target call; the reply arrives later, so the call is uncertain."""
    from threading import Thread

    from evolution_lab.owner import _campaign_worker as worker
    from evolution_lab.owner.settlement import positive_settlement
    from ledger_faults import HeldCall, LedgerProbe, abandoned_pending_turn, is_turn

    _, binding, paths, config = _campaign(tmp_path, rollout_timeout_seconds=1.5)
    probe = LedgerProbe(monkeypatch, worker)
    target = _Target(binding)
    held = HeldCall(target.server, is_turn)
    outcome = {}
    runner = Thread(target=lambda: outcome.setdefault(
        "code", worker.run_worker(config, dict(binding.base_environment), lambda: False)
    ))
    try:
        runner.start()
        assert held.entered.wait(10)
        assert probe.wait_for(abandoned_pending_turn, seconds=10)
        held.release.set()
        runner.join(40)
        assert not runner.is_alive()
    finally:
        held.release.set()
        runner.join(40)
        target.close()
    settlement = _published_campaign_settlement(paths, config)
    # evolve itself returned: the ledger's uncertainty alone forbids reuse.
    assert settlement["normal_return"] is True
    ledger = settlement["ledger"]
    assert ledger["namespace"] == config["run_id"]
    assert ledger["uncertain_any"] is True
    assert ledger["uncertain"] >= 1
    assert ledger["bound_transports"] >= 1
    [snapshot] = probe.settled
    assert _records(snapshot) == [("turn", "uncertain", True, "abandoned")]
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is False


def test_campaign_worker_transport_failure_on_turn_publishes_uncertain(tmp_path, monkeypatch):
    """The target reads a turn request and closes the connection without a reply."""
    from evolution_lab.owner import _campaign_worker as worker
    from evolution_lab.owner.settlement import positive_settlement
    from ledger_faults import LedgerProbe, drop_first_reply, is_turn

    _, binding, paths, config = _campaign(tmp_path)
    probe = LedgerProbe(monkeypatch, worker)
    dropped = drop_first_reply(monkeypatch, is_turn)
    target = _Target(binding)
    try:
        worker.run_worker(config, dict(binding.base_environment), lambda: False)
    finally:
        target.close()
    assert len(dropped) == 1
    settlement = _published_campaign_settlement(paths, config)
    assert settlement["normal_return"] is True
    ledger = settlement["ledger"]
    assert ledger["uncertain"] >= 1
    assert ledger["uncertain_any"] is True
    assert ledger["quiescent"] is True
    [snapshot] = probe.settled
    assert _records(snapshot) == [("turn", "uncertain", False, "transport_error")]
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is False


def _settle_timeout_settlement(tmp_path, monkeypatch):
    """Run the actual campaign worker while one turn stays inside the target past
    the rollout deadline and a shortened settle; return the published settlement
    and the worker config. The held call is released before returning."""
    from threading import Thread
    import time

    from evolution_lab.owner import _campaign_worker as worker
    from ledger_faults import HeldCall, is_turn

    _, binding, paths, config = _campaign(tmp_path, rollout_timeout_seconds=1.5)
    target = _Target(binding)
    held = HeldCall(target.server, is_turn, seconds=60)
    outcome = {}
    with monkeypatch.context() as local:
        local.setattr(worker, "settle_timeout", lambda binding: .3)
        runner = Thread(target=lambda: outcome.setdefault(
            "code", worker.run_worker(config, dict(binding.base_environment), lambda: False)
        ))
        try:
            runner.start()
            assert held.entered.wait(10)
            deadline = time.monotonic() + 40
            while not paths.settlement_path.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert paths.settlement_path.exists()
            # `asyncio.run` teardown waits for the held executor thread.
            assert runner.is_alive()
            settlement = _published_campaign_settlement(paths, config)
        finally:
            held.release.set()
            runner.join(40)
            target.close()
    assert not runner.is_alive()
    return settlement, config, outcome


def test_campaign_worker_settle_timeout_publishes_non_quiescent(tmp_path, monkeypatch):
    from evolution_lab.owner.settlement import positive_settlement

    settlement, config, _ = _settle_timeout_settlement(tmp_path, monkeypatch)
    assert settlement["normal_return"] is True
    ledger = settlement["ledger"]
    assert ledger["namespace"] == config["run_id"]
    assert ledger["admission_closed"] is True
    assert ledger["quiescent"] is False
    assert ledger["in_flight"] >= 1 and ledger["pending"] >= 1
    # Nothing is uncertain yet: non-quiescence alone forbids reuse.
    assert ledger["uncertain"] == 0 and ledger["uncertain_any"] is False
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is False


def test_owner_non_quiescent_worker_ledger_requires_cleanup(tmp_path, monkeypatch):
    """A settle timeout's published ledger, put in place of an otherwise positive
    campaign settlement, makes the Owner require cleanup instead of reusing."""
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner import cleanup
    from evolution_lab.owner.settlement import positive_settlement
    from test_owner_cleanup import finished, prepared_target
    from test_owner_target import ready_target

    (tmp_path / "worker").mkdir()
    (tmp_path / "owner").mkdir()
    stuck, _, _ = _settle_timeout_settlement(tmp_path / "worker", monkeypatch)
    assert stuck["ledger"]["quiescent"] is False
    original = cleanup.read_campaign_settlement
    substituted = []

    def non_quiescent(*args, **kwargs):
        value = original(*args, **kwargs)
        run_id = value["run_id"]
        assert positive_settlement(value, run_id=run_id, kind="campaign") is True
        value["ledger"] = {**stuck["ledger"], "namespace": run_id}
        substituted.append(value)
        return value

    monkeypatch.setattr(cleanup, "read_campaign_settlement", non_quiescent)
    state, bundle, prepared = prepared_target(tmp_path / "owner")

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            value = await finished(owner, ref)
            assert value.control_state == "cleanup_required", value.to_payload()
            assert value.process.cleanup_complete is True
            assert value.target_cleanup.complete is None
            cleaned = await owner.wait(await owner.cleanup(ref))
            assert cleaned.state == "succeeded", cleaned.to_payload()
            assert (await owner.target(target)).state == "stopped"

    asyncio.run(scenario())
    assert len(substituted) == 1


@pytest.mark.parametrize("raised", ["before_evolve", "after_evolve_returned"])
def test_campaign_worker_evolve_raising_publishes_abnormal_settlement(
    tmp_path, monkeypatch, raised
):
    import evolution.tree.evolve as evolve_module

    from evolution_lab.owner import _campaign_worker as worker
    from evolution_lab.owner.settlement import positive_settlement

    _, binding, paths, config = _campaign(tmp_path)
    real_evolve = evolve_module.evolve

    async def raising(*args, **kwargs):
        if raised == "after_evolve_returned":
            await real_evolve(*args, **kwargs)
        raise RuntimeError("private evolve failure")

    monkeypatch.setattr(evolve_module, "evolve", raising)
    target = _Target(binding)
    try:
        assert worker.run_worker(config, dict(binding.base_environment), lambda: False) == 1
    finally:
        target.close()
    assert paths.diagnostic_path.exists()
    settlement = _published_campaign_settlement(paths, config)
    assert settlement["schema"] == "lab-campaign-settlement/2"
    assert settlement["normal_return"] is False
    ledger = settlement["ledger"]
    assert ledger["admission_closed"] is True and ledger["quiescent"] is True
    assert ledger["uncertain"] == 0 and ledger["uncertain_any"] is False
    if raised == "before_evolve":
        assert ledger["namespace"] is None and ledger["bound_transports"] == 0
    else:
        # A clean ledger: the abnormal return alone forbids reuse.
        assert ledger["namespace"] == config["run_id"]
        assert ledger["bound_transports"] >= 1 and ledger["admitted"] >= 1
        assert positive_settlement(
            {**settlement, "normal_return": True}, run_id=config["run_id"], kind="campaign"
        ) is True
    assert positive_settlement(settlement, run_id=config["run_id"], kind="campaign") is False


def test_worker_refusal_prints_core_fields_per_finding_without_authorize_hint(monkeypatch, capsys, tmp_path):
    """PDR-0013: code, class, differences, instance digest and effect sentences, no hint."""
    import evolution.tree.evolve as evolve_module
    from evolution.tree import RefusalDifference
    from evolution.tree.execution.authorizations import EFFECT_SENTENCES
    from evolution.tree.execution.output_admission import EvolveRefused, RefusalFinding
    from evolution_lab.owner import _campaign_worker as worker

    refusal = EvolveRefused.from_findings((
        RefusalFinding("run_identity_changed", None, "PRIVATE-DETAIL",
                       (RefusalDifference("target.settings_hash", "a" * 64, "b" * 64),),
                       "c" * 64, "close"),
        RefusalFinding("run_dir_unsafe_entry", "scorer-traces", "PRIVATE-DETAIL",
                       (), "d" * 64, "hygiene"),
    ))

    def refuse(_path):
        raise refusal

    monkeypatch.setattr(evolve_module, "admit_manifest", refuse)
    assert worker.run_worker(_admission_config(tmp_path), {}, lambda: False) == 2
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "evo tree evolve: refused: run_identity_changed",
        f"evo tree evolve: differs: target.settings_hash  journaled {'a' * 64}  observed {'b' * 64}",
        "evo tree evolve: class: authorizable",
        "evo tree evolve: instance_digest: " + "c" * 64,
        "evo tree evolve: effects: close (default)",
        *("evo tree evolve: " + line for line in EFFECT_SENTENCES["close"]),
        "evo tree evolve: refused: run_dir_unsafe_entry at scorer-traces",
        "evo tree evolve: class: authorizable",
        "evo tree evolve: instance_digest: " + "d" * 64,
        "evo tree evolve: effects: hygiene (default)",
    ]
    assert "evo tree authorize" not in err and "PRIVATE-DETAIL" not in err


def test_worker_refusal_publishes_the_first_finding_as_diagnostic(monkeypatch, tmp_path):
    """ADR-0023: the first finding of a core refusal reaches Owner through diagnostic.json."""
    import evolution.tree.evolve as evolve_module
    from evolution.tree.execution.output_admission import EvolveRefused, RefusalFinding
    from evolution_lab.owner import _campaign_worker as worker

    refusal = EvolveRefused.from_findings((
        RefusalFinding("corpus_inside_role_workspace", None, None, (), None, None),
        RefusalFinding("run_dir_unsafe_entry", "x", None, (), "d" * 64, "hygiene"),
    ))
    monkeypatch.setattr(evolve_module, "admit_manifest", lambda _p: (_ for _ in ()).throw(refusal))
    config = {"manifest_path": "unused.json", "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "operation_id": "a" * 32, "scope_id": "b" * 32, "owner_generation": "c" * 32}
    assert worker.run_worker(config, {}, lambda: False) == 2
    assert json.loads((tmp_path / "diagnostic.json").read_text()) == {
        "schema": "lab-campaign-diagnostic/5", "operation_id": "a" * 32, "scope_id": "b" * 32,
        "failure": None, "core_terminal": None, "exception": None,
        "refusal": {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None},
        "findings": [
            {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None,
             "effects": [], "effect_texts": [], "differs": []},
            {"code": "run_dir_unsafe_entry", "path": "x", "class": "authorizable", "instance_digest": "d" * 64,
             "effects": ["hygiene"], "effect_texts": [""], "differs": []}]}


def test_refusal_path_is_redacted():
    from evolution.tree.execution.output_admission import EvolveRefused, RefusalFinding
    from evolution_lab.owner._campaign_worker import refusal_payload
    error = EvolveRefused.from_findings((RefusalFinding("x_missing", "SECRET-VALUE-123", None, (), None, None),))
    assert "SECRET-VALUE-123" not in json.dumps(refusal_payload(error, ("SECRET-VALUE-123",)))


def _refuse_with(monkeypatch, *findings):
    import evolution.tree.evolve as evolve_module
    from evolution.tree.execution.output_admission import EvolveRefused

    refusal = EvolveRefused.from_findings(findings)

    def refuse(_path):
        raise refusal

    monkeypatch.setattr(evolve_module, "admit_manifest", refuse)


def test_worker_refusal_prints_core_expected_after_refused(monkeypatch, capsys, tmp_path):
    """Spec 2026-10-09 §3: a /14 policy refusal's fixed domain statement, printed after `refused:` as core's CLI does."""
    from evolution.tree.execution.output_admission import RefusalFinding
    from evolution_lab.owner import _campaign_worker as worker

    _refuse_with(monkeypatch, RefusalFinding("missing_key", "campaign.gate.mode", "PRIVATE-DETAIL", (), None, None,
                                             expected="expected exactly 'statistical' or 'observe'"))
    assert worker.run_worker(_admission_config(tmp_path), {}, lambda: False) == 2
    assert capsys.readouterr().err.splitlines() == [
        "evo tree evolve: refused: missing_key at campaign.gate.mode",
        "evo tree evolve: expected exactly 'statistical' or 'observe'",
        "evo tree evolve: class: hard",
    ]


def test_worker_refusal_prints_every_effect_of_a_finding_default_first(monkeypatch, capsys, tmp_path):
    """a21 (I2 §4.3): a comparable run_identity_changed offers several effects, default first."""
    from evolution.tree import RefusalDifference, refusal_class
    from evolution.tree.execution.authorizations import EFFECT_SENTENCES
    from evolution.tree.execution.output_admission import RefusalFinding
    from evolution_lab.owner import _campaign_worker as worker

    finding = RefusalFinding(
        "run_identity_changed", None, "PRIVATE-DETAIL",
        (RefusalDifference("target.settings_hash", "a" * 64, "b" * 64),),
        "c" * 64, "continue", ("continue", "rebaseline", "close"))
    assert refusal_class(finding.code) == "authorizable" and finding.instance_digest is not None
    _refuse_with(monkeypatch, finding)

    assert worker.run_worker(_admission_config(tmp_path), {}, lambda: False) == 2
    err = capsys.readouterr().err
    assert "class: authorizable" in err
    assert "evo tree evolve: effects: continue (default), rebaseline, close" in err
    for effect in ("continue", "rebaseline", "close"):
        assert EFFECT_SENTENCES[effect]
        for sentence in EFFECT_SENTENCES[effect]:
            assert sentence in err
    assert "evo tree authorize" not in err and "PRIVATE-DETAIL" not in err


def test_worker_refusal_with_one_effect_prints_it_as_default(monkeypatch, capsys, tmp_path):
    from evolution.tree import refusal_class
    from evolution.tree.execution.authorizations import EFFECT_SENTENCES
    from evolution.tree.execution.output_admission import RefusalFinding
    from evolution_lab.owner import _campaign_worker as worker

    finding = RefusalFinding("run_identity_changed", None, "", (), "c" * 64, "close")
    assert refusal_class(finding.code) == "authorizable"
    _refuse_with(monkeypatch, finding)

    assert worker.run_worker(_admission_config(tmp_path), {}, lambda: False) == 2
    err = capsys.readouterr().err
    assert "evo tree evolve: effects: close (default)\n" in err
    for sentence in EFFECT_SENTENCES["close"]:
        assert sentence in err


def test_model_grouping_without_its_key_is_core_refusal_not_lab_error(tmp_path, capsys):
    """The key name is not declared, so the worker's environment lacks it: core refuses at bind."""
    from evolution_lab.owner import _campaign_worker as worker
    from test_owner_binding import MODEL_GROUPING

    _, binding, paths, config = _campaign(tmp_path, model_grouping=MODEL_GROUPING)
    assert "GROUPING_KEY" not in binding.base_environment

    code = worker.run_worker(config, dict(binding.base_environment), lambda: False)

    assert code == 2
    err = capsys.readouterr().err
    assert "evo tree evolve: refused: grouper_env_missing at grouper.api_key_env" in err
    published = json.loads(paths.diagnostic_path.read_text())
    assert published["failure"] is None  # a core refusal, not a lab error (ADR-0023)
    assert published["refusal"]["code"] == "grouper_env_missing"


def test_owner_campaign_suspended_by_an_unstable_target_is_shown(tmp_path):
    from evolution_lab._suspension import last_suspension
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished
    from test_owner_target import ready_target
    flag = tmp_path / "target-broken"
    flag.write_text("")
    state, bundle, _ = make_bundle(tmp_path, mode=f"fail_after:1:{flag}", max_instances=4, launch_schema=2,
                                   scaffold=scaffold_15_with_legacy_failures)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", first.id)
            value = await finished(owner, ref, seconds=60)
            assert value.process.exit_code == 1, value.to_payload()
            assert value.failure is None
            core = value.result.core
            assert core["suspended"] is True and core["complete"] is False
            assert core["campaign_outcome"] is None
            assert last_suspension(core) == {"seq": core["suspensions"][-1]["seq"],
                "reason": "target_unstable", "owner": "target", "evidence_kind": "threshold"}
            report = await owner.report(ref)
            assert report.state == "available" and report.report["core"]["suspended"] is True
            markdown = await owner.report_markdown(ref)
            assert markdown.state == "available" and markdown.markdown.startswith("# Кампания\n")
    asyncio.run(scenario())


def test_campaign_worker_config_two_carries_mode(tmp_path):
    from evolution_lab.owner.execution import campaign_worker_config, validate_campaign_worker_config
    op, scope = "a" * 32, "b" * 32
    run = tmp_path / "artifacts" / op / op
    scope_dir = tmp_path / "scopes" / scope
    kwargs = dict(operation_id=op, scope_id=scope, owner_generation="c" * 32,
        bundle_digest="d" * 64, prepared_digest="e" * 64,
        manifest_path=tmp_path / "manifest.json", run_directory=run,
        settlement_path=scope_dir / "settlement.json", diagnostic_path=scope_dir / "diagnostic.json")
    value = campaign_worker_config(**kwargs, mode="continue")
    assert value["schema"] == "lab-campaign-worker-config/2" and value["mode"] == "continue"
    assert campaign_worker_config(**kwargs)["mode"] == "new"
    for bad in ("resume", None, True):
        with pytest.raises(ValueError):
            validate_campaign_worker_config({**value, "mode": bad})
    with pytest.raises(ValueError):
        validate_campaign_worker_config({**value, "schema": "lab-campaign-worker-config/1"})


def test_continue_paths_create_only_the_scope(tmp_path):
    from evolution_lab.owner.execution import create_campaign_paths
    from evolution_lab.owner.models import OwnerError
    for name in ("scopes", "artifacts"):
        (tmp_path / name).mkdir(mode=0o700)
    op, first, second = "a" * 32, "b" * 32, "c" * 32
    paths = create_campaign_paths(tmp_path, op, first)
    (paths.run_directory / "campaign.jsonl").write_text("")
    again = create_campaign_paths(tmp_path, op, second, continue_run=True)
    assert again.run_directory == paths.run_directory
    assert again.scope_directory == tmp_path / "scopes" / second
    with pytest.raises(OwnerError) as caught:
        create_campaign_paths(tmp_path, op, second, continue_run=True)
    assert caught.value.code == "path_conflict"
    with pytest.raises(OwnerError) as caught:
        create_campaign_paths(tmp_path, "f" * 32, "e" * 32, continue_run=True)
    assert caught.value.code == "campaign_unreadable"


def test_worker_run_directory_check_follows_mode(tmp_path):
    from evolution_lab.owner._campaign_worker import check_run_directory
    run = tmp_path / "run"; run.mkdir()
    check_run_directory(run, "new")
    with pytest.raises(ValueError):
        check_run_directory(run, "continue")
    (run / "campaign.jsonl").write_text("")
    check_run_directory(run, "continue")
    with pytest.raises(ValueError):
        check_run_directory(run, "new")


class _GroupingUpstream:
    """A fake OpenAI-compatible endpoint the model grouping calls; records what core sent."""

    def __init__(self, reply, *, provider=None):
        import http.server
        import threading

        self.requests = []
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                upstream.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                          "body": json.loads(body)})
                payload = json.dumps({
                    **({'provider': provider} if provider is not None else {}),
                    "id": "reply", "object": "chat.completion", "created": 0, "model": "fake-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": reply(upstream.requests[-1])}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def test_model_grouping_key_reaches_core_and_no_lab_file(tmp_path):
    """Spec 2026-09-30 §2.3: the key value reaches core's process and appears in no file under the state.

    The scaffold's Grouper groups failing observations with the model (its baseline has
    failing samples), so no special corpus is needed. The fake endpoint answers `{"groups": []}`: core
    asks for protocol repair twice per round, then falls back to singletons; what matters here is the
    wire, not the grouping.
    """
    from test_owner_binding import MODEL_GROUPING
    key = "sk-grouping-e2e-0123456789abcdef"
    upstream = _GroupingUpstream(lambda request: json.dumps({"groups": []}))
    try:
        grouping = {**MODEL_GROUPING, "base_url": f"http://127.0.0.1:{upstream.port}/v1"}
        grouping.pop("proxy_env")
        state, binding, paths, config = _campaign(
            tmp_path, model_grouping=grouping, bundle_secret_names=("GROUPING_KEY",))
        target = _Target(binding)
        try:
            # The worker environment goes through the product binding, as Owner admission builds it:
            # if `secret_partition` did not put the grouping key into E, `bind_execution_secrets`
            # refuses the value and the key never reaches core.
            from evolution_lab.owner.secrets import (
                bind_execution_secrets, bind_target_secrets, secret_partition)
            partition = secret_partition(binding)
            assert partition.execution == ("GROUPING_KEY",) and partition.target == ("TARGET_TOKEN",)
            target_secrets = bind_target_secrets(binding, {"TARGET_TOKEN": "target-token-value"})
            bound = bind_execution_secrets(
                binding, {"GROUPING_KEY": key}, target_secrets=target_secrets)
            assert bound.environment["GROUPING_KEY"] == key
            asyncio.run(_helper_run(binding, paths, config, env=bound.environment))
        finally:
            target.close()
    finally:
        upstream.close()
    assert upstream.requests, "core never called the grouping model"
    assert {request["authorization"] for request in upstream.requests} == {f"Bearer {key}"}
    assert all(request["path"] == "/v1/chat/completions" for request in upstream.requests)
    assert json.loads(paths.stdout_path.read_text())["finished"]["outcome"] == "completed"
    scanned = [path for path in tmp_path.rglob("*") if path.is_file()]
    # The scan covers the run's own artefacts, not just the bundle: the campaign log and the worker's
    # stderr are among the files checked for the key value.
    assert paths.run_directory / "campaign.jsonl" in scanned
    assert paths.stderr_path in scanned
    assert (paths.run_directory / "campaign.jsonl").stat().st_size > 0
    leaked = [str(path.relative_to(tmp_path)) for path in scanned if key.encode() in path.read_bytes()]
    assert leaked == []


def test_campaign_start_admits_a_manifest_16_project(tmp_path):
    """The pre-dispatch step of ``Owner.start`` (``prepare_worker_inputs``) admits /16 (spec D2)."""
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.owner.execution import prepare_worker_inputs
    from evolution_lab.preparation import PreparationStore
    from scaffold_16 import scaffold_16
    state, bundle, _ = make_bundle(tmp_path, scaffold=scaffold_16)
    store = PreparationStore(state)
    prepared = store.prepare_campaign(bundle.digest)
    fresh = open_prepared(store._storage, prepared.digest, stage="before_start")
    assert fresh.kind == "campaign"
    from evolution_lab.client import PreparedCampaign
    assert isinstance(prepare_worker_inputs(fresh, SimpleSecrets()), PreparedCampaign)


class SimpleSecrets:
    environment = {"TARGET_TOKEN": "private-neutral-token"}


@pytest.mark.parametrize("phase", ["admission", "redactions", "log_redactions", "evolve", "short_secret", "empty_redactions", "overlap"])
def test_campaign_exception_log_is_redacted_or_class_only(tmp_path, monkeypatch, capsys, phase):
    """A failure before redaction is ready must never format its private message."""
    from types import SimpleNamespace
    import evolution.tree.evolve as core
    from evolution_lab.owner import _campaign_worker as worker
    secret = "s3cr" if phase == "short_secret" else "neutral-secret-credential"
    admitted = SimpleNamespace(workflow=None, target=SimpleNamespace(timeouts=SimpleNamespace(
        connect_seconds=.01, read_seconds=.01)))
    config = {"manifest_path": "unused", "run_directory": str(tmp_path),
              "operation_id": "a" * 32, "scope_id": "b" * 32,
              "owner_generation": "c" * 32, "run_id": "neutral-run",
              "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "settlement_path": str(tmp_path / "settlement.json")}
    def broken(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(core, "admit_manifest", broken if phase == "admission" else lambda _: admitted)
    monkeypatch.setattr(core, "redactions_for", broken if phase == "redactions" else lambda *args: ())
    if phase == "log_redactions":
        monkeypatch.setattr(worker, "exception_redactions", broken)
    monkeypatch.setattr(core, "short_forwarded_names", lambda *args: ())
    async def raising(*args, **kwargs):
        message = "left|abcd|right" if phase == "overlap" else secret
        raise RuntimeError("public failure" if phase == "empty_redactions" else message)
    monkeypatch.setattr(core, "evolve", raising)
    environ = {} if phase == "empty_redactions" else {"TARGET_TOKEN": secret}
    if phase == "overlap":
        environ = {"TARGET_TOKEN": "abc", "OTHER_TOKEN": "bcd"}
    assert worker.run_worker(config, environ, lambda: False) == 1
    captured = capsys.readouterr()
    if phase == "overlap":
        payload = captured.err.split("left|", 1)[1].split("|right", 1)[0]
        assert not set("abcd") & set(payload)
        assert all(value not in captured.err + Path(config["diagnostic_path"]).read_text()
                   for value in environ.values())
    assert secret not in captured.err + captured.out + Path(config["diagnostic_path"]).read_text()
    if phase in {"admission", "redactions", "log_redactions"}:
        assert captured.err == "RuntimeError\n"
    else:
        assert "Traceback (most recent call last):" in captured.err
        assert "RuntimeError" in captured.err
    assert json.loads(Path(config["diagnostic_path"]).read_text())["failure"] == {
        "code": "lab_error", "stage": "running", "message": "The owner operation failed."}


def test_owner_finalization_puts_the_published_core_refusal_into_status(tmp_path, monkeypatch):
    """Owner wiring of ADR-0023: finalization reads the worker's ``diagnostic.json`` into the status.

    Lab's own pre-dispatch checks (``prepare_worker_inputs``) refuse every layout core would refuse before
    the worker starts, so a real worker-side refusal cannot be provoked through ``Owner.start``. The worker's
    side is covered by ``test_model_grouping_without_its_key_is_core_refusal_not_lab_error``; here the file is
    published into the scope the way the worker does, and the Owner finalizes the run around it.
    """
    from evolution_lab.owner import Owner, ResourceRef, cleanup, execution
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished
    from test_owner_target import ready_target
    refusal = {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None}
    original = execution.create_campaign_paths

    def publishing(state, operation_id, scope_id, **kwargs):
        paths = original(state, operation_id, scope_id, **kwargs)
        paths.diagnostic_path.write_text(json.dumps({"schema": "lab-campaign-diagnostic/2",
            "operation_id": operation_id, "scope_id": scope_id, "failure": None, "refusal": refusal}))
        return paths

    monkeypatch.setattr(execution, "create_campaign_paths", publishing)
    # A refused worker leaves no positive settlement (ADR-0008): the operation stays cleanup_required.
    monkeypatch.setattr(cleanup, "positive_settlement", lambda *args, **kwargs: False)
    state, bundle, _ = make_bundle(tmp_path)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            value = await finished(owner, ref, seconds=60)
            assert value.failure.code == "core_refused", value.to_payload()
            assert value.refusal.code == "corpus_inside_role_workspace"
            assert value.control_state == "cleanup_required"
            assert value.target_cleanup.state == "unconfirmed"
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            after = await owner.status(ref)
            assert after.failure.code == "core_refused"
            assert after.refusal.code == "corpus_inside_role_workspace"
    asyncio.run(scenario())


def test_a_failed_finalization_write_keeps_the_core_refusal(tmp_path, monkeypatch):
    """ADR-0004: a persistence failure does not destroy the available in-memory outcome (codex r1, item 1).

    The finalization write that carries the refusal fails once; the failure path rebuilds the terminal value
    from the durable row, which has no refusal yet. The refusal read at finalization must survive in the
    published value and in the retried write; the persistence failure shows separately as ``failure``.
    """
    from evolution_lab.owner import Owner, OwnerError, ResourceRef, cleanup, execution
    from evolution_lab.owner.store import OwnerStateStore
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished
    from test_owner_target import ready_target
    refusal = {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None}
    original = execution.create_campaign_paths

    def publishing(state, operation_id, scope_id, **kwargs):
        paths = original(state, operation_id, scope_id, **kwargs)
        paths.diagnostic_path.write_text(json.dumps({"schema": "lab-campaign-diagnostic/2",
            "operation_id": operation_id, "scope_id": scope_id, "failure": None, "refusal": refusal}))
        return paths

    update = OwnerStateStore.update_operation
    failed = []

    def fail_the_refusal_write_once(self, operation_id, **kwargs):
        if kwargs.get("refusal") is not None and not failed:
            failed.append(operation_id)
            raise OwnerError("io_error", "finalizing")
        return update(self, operation_id, **kwargs)

    monkeypatch.setattr(execution, "create_campaign_paths", publishing)
    monkeypatch.setattr(cleanup, "positive_settlement", lambda *args, **kwargs: False)
    monkeypatch.setattr(OwnerStateStore, "update_operation", fail_the_refusal_write_once)
    state, bundle, _ = make_bundle(tmp_path)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            value = await finished(owner, ref, seconds=60)
            assert failed == [operation.id]
            assert value.refusal is not None, value.to_payload()
            assert value.refusal.code == "corpus_inside_role_workspace"
            assert value.failure.code == "io_error"  # the persistence failure, reported separately
            assert value.control_state == "cleanup_required"
            assert (await owner.wait(await owner.cleanup(ref))).state == "succeeded"
            after = await owner.status(ref)  # durable: the cache is dropped after cleanup
            assert after.refusal is not None and after.refusal.code == "corpus_inside_role_workspace"
            assert after.starts[-1].refusal == after.refusal
    asyncio.run(scenario())
