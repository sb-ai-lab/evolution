"""H1 owner behavior using neutral subprocesses and the real private supervisor."""
import asyncio
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import sys
import threading
from types import MappingProxyType

import pytest
from ownership_modes import ownership_mode  # noqa: F401
from scaffold_15 import scaffold_15


@pytest.fixture
def prepared(tmp_path):
    from evolution_lab._client_models import make_prepared_campaign
    from evolution_lab.recipe import prepare_inputs
    scaffold_15(tmp_path / "sample")
    environment = dict(os.environ, H1_TEST_SECRET="PRIVATE-ENV")
    inputs = prepare_inputs(tmp_path / "sample/manifest.json", env=environment)
    return make_prepared_campaign("fixture", inputs, MappingProxyType(environment))


@pytest.fixture
def runtime(monkeypatch):
    import evolution_lab._client_operation as operation
    monkeypatch.setattr(operation, "verify_isolated", lambda *a, **kw: None)
    monkeypatch.setattr(operation, "_publish_execution", lambda logs, snapshot: None)

    def command(source):
        monkeypatch.setattr(operation, "_core_argv", lambda inputs, run: (
            sys.executable, "-I", "-c", source))
    command("raise SystemExit(7)")
    return command


def new(prepared, tmp_path, **kwargs):
    from evolution_lab.client import create_run
    return create_run(prepared, tmp_path / "run", dispatch_guard=kwargs.pop("dispatch_guard", lambda: None),
                      grace_seconds=kwargs.pop("grace_seconds", .3), **kwargs)


def grouping_prepared(tmp_path):
    from evolution_lab._client_models import make_prepared_campaign
    from evolution_lab.recipe import prepare_inputs

    root = tmp_path / "grouping-sample"
    scaffold_15(root)
    grouping = root / "grouping"
    grouping.mkdir()
    (grouping / "group.py").write_text("raise SystemExit(0)\n")
    raw = json.loads((root / "manifest.json").read_text())
    raw["grouper"] = {
        "kind": "command",
        "argv": ["python3", "group.py"],
        "cwd": "grouping",
        "timeout_seconds": 17,
        "dependency_paths": [],
        "allowed_env": ["GROUPING_SECRET"],
        "config": {},
    }
    (root / "manifest.json").write_text(json.dumps(raw))
    environment = MappingProxyType(
        dict(os.environ, GROUPING_SECRET="GROUPING-SECRET")
    )
    inputs = prepare_inputs(root / "manifest.json", env=environment)
    return make_prepared_campaign("grouping-fixture", inputs, environment)


async def test_initial_stop_is_inert_and_single_use(prepared, tmp_path):
    from evolution_lab.client import ClientError, stop
    def forbidden():
        pytest.fail("initial stop must bypass guard")
    operation = new(prepared, tmp_path, dispatch_guard=forbidden)
    assert operation.result is None and not (tmp_path / "run-lab-logs").exists()
    assert stop(operation).disposition == "requested"
    assert stop(operation).disposition == "already_requested"
    events = []
    result = await asyncio.wait_for(operation.run(on_event=events.append), 3)
    assert (result.state, result.stage, result.launched, result.cleanup_complete,
            result.stop_effect, result.core_force_requested) == (
                "stopped", "before_start", False, True, "prevented_start", False)
    assert result.persistence_state == "not_attempted"
    assert [e["kind"] for e in events] == ["stop_requested", "completed"]
    assert stop(operation).disposition == "already_terminal"
    assert not (tmp_path / "run-lab-logs").exists()
    with pytest.raises(ClientError) as error:
        await operation.run()
    assert error.value.failure.code == "invalid_argument"


async def test_cancellation_before_entry_does_not_consume_run(prepared, tmp_path):
    operation = new(prepared, tmp_path)
    task = asyncio.create_task(operation.run())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operation.result is None and operation._status_fields()["state"] == "created"
    operation.stop()
    assert (await operation.run()).state == "stopped"


@pytest.mark.parametrize("budget, code, state", [(True, None, "stopped"), (False, "dispatch_refused", "failed")])
async def test_guard_refusal_prevents_writes(prepared, tmp_path, budget, code, state):
    from evolution_lab.budget import BudgetExhausted
    def refused():
        raise BudgetExhausted("private") if budget else RuntimeError("private")
    result = await new(prepared, tmp_path, dispatch_guard=refused).run()
    assert result.state == state and result.launched is False
    assert (result.failure.code if result.failure else None) == code
    assert result.stop_requested and result.stop_effect == "prevented_start"
    assert not (tmp_path / "run-lab-logs").exists()


@pytest.mark.usefixtures("ownership_mode")
async def test_fast_exit_and_immutable_public_result(prepared, tmp_path, runtime):
    events = []
    operation = new(prepared, tmp_path)
    result = await asyncio.wait_for(operation.run(on_event=events.append), 3)
    assert (result.state, result.exit_code, result.launched, result.cleanup_complete,
            result.core_force_requested) == ("exited", 7, True, True, False)
    assert [event["kind"] for event in events] == ["core_started", "core_exited", "completed"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert operation.result is result and result.persistence_state == "saved"
    assert not (tmp_path / "run-lab-logs/process-result.json").exists()
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 0
    public = result.public()
    public["secondary_failures"].append({})
    assert result.public()["secondary_failures"] == []
    assert "PRIVATE" not in json.dumps(public)


@pytest.mark.usefixtures("ownership_mode")
async def test_exact_prepared_env_reaches_child(prepared, tmp_path, runtime, monkeypatch):
    runtime("import os; print(os.environ['H1_TEST_SECRET']); print(os.environ.get('AMBIENT_ONLY', 'absent'))")
    monkeypatch.setenv("H1_TEST_SECRET", "wrong")
    monkeypatch.setenv("AMBIENT_ONLY", "wrong")
    result = await new(prepared, tmp_path).run()
    assert result.state == "exited"
    assert (tmp_path / "run-lab-logs/stdout.log").read_text().splitlines() == ["PRIVATE-ENV", "absent"]


@pytest.mark.parametrize("kind", ["core_started", "core_exited", "completed"])
@pytest.mark.usefixtures("ownership_mode")
async def test_callback_failure_preserves_facts_and_disables_callbacks(prepared, tmp_path, runtime, kind):
    events = []
    def callback(event):
        events.append(event["kind"])
        if event["kind"] == kind:
            raise RuntimeError("PRIVATE-CALLBACK")
    result = await new(prepared, tmp_path).run(on_event=callback)
    assert result.state == "failed" and result.failure.code == "callback_failed"
    assert result.launched is True and result.exit_code is not None and result.cleanup_complete is True
    assert events[-1] == kind and events.count(kind) == 1


@pytest.mark.usefixtures("ownership_mode")
async def test_user_stop_force_and_cancellation_cleanup(prepared, tmp_path, runtime):
    runtime("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)")
    operation = new(prepared, tmp_path, grace_seconds=.08)
    started = asyncio.Event()
    task = asyncio.create_task(operation.run(on_event=lambda e: started.set() if e["kind"] == "core_started" else None))
    await started.wait()
    while not (tmp_path / "run-lab-logs/stdout.log").read_text():
        await asyncio.sleep(.005)
    operation.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = operation.result
    assert (result.state, result.stop_reason, result.stop_effect, result.exit_code,
            result.core_force_requested, result.cleanup_complete) == (
                "stopped", "user", "delivered", -9, True, True)


@pytest.mark.usefixtures("ownership_mode")
async def test_post_exit_drift_and_completed_failure_order(prepared, tmp_path, runtime):
    def callback(event):
        if event["kind"] == "core_exited":
            prepared._inputs.manifest.path.write_text("{}")
        if event["kind"] == "completed":
            raise RuntimeError("private")
    result = await new(prepared, tmp_path).run(on_event=callback)
    assert (result.state, result.stage, result.exit_code) == ("failed", "finalizing", 7)
    assert result.failure.public() == {"code": "inputs_changed", "stage": "finalizing"}
    assert [f.code for f in result.secondary_failures] == ["callback_failed"]


@pytest.mark.usefixtures("ownership_mode")
async def test_late_stop_does_not_turn_natural_exit_into_stopped(prepared, tmp_path, runtime):
    operation = new(prepared, tmp_path)
    def callback(event):
        if event["kind"] == "core_exited":
            operation.stop()
    result = await operation.run(on_event=callback)
    assert (result.state, result.stop_effect, result.exit_code) == ("exited", "too_late", 7)


@pytest.mark.usefixtures("ownership_mode")
async def test_persistence_receipt_cannot_change_execution(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    snapshots = []
    def publish(logs, snapshot):
        snapshots.append(snapshot)
        raise OSError("PRIVATE-PATH")
    monkeypatch.setattr(module, "_publish_execution", publish)
    result = await new(prepared, tmp_path).run()
    assert result.state == "exited" and result.failure is None
    assert result.persistence_state == "failed" and result.persistence_failure.code == "io_error"
    assert snapshots[0].public()["schema"] == "lab-execution-result/1"
    assert "persistence" not in snapshots[0].public()


@pytest.mark.usefixtures("ownership_mode")
async def test_drift_and_path_separation_refuse_before_logs(prepared, tmp_path, runtime):
    prepared._inputs.manifest.path.write_text("{}")
    result = await new(prepared, tmp_path).run()
    assert result.failure.code == "inputs_changed" and result.launched is False
    assert not (tmp_path / "run-lab-logs").exists()


@pytest.mark.usefixtures("ownership_mode")
async def test_stop_interrupts_config_without_go(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    entered = asyncio.Event()
    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(module.CommandChannel, "send_config", stalled)
    operation = new(prepared, tmp_path)
    task = asyncio.create_task(operation.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operation.result.state == "stopped" and operation.result.launched is False
    assert operation.result.cleanup_complete is True


@pytest.mark.usefixtures("ownership_mode")
async def test_ready_timeout_has_lifecycle_failure_and_proven_no_start(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    async def stalled(*args, **kwargs):
        await asyncio.Future()
    monkeypatch.setattr(module.CommandChannel, "send_config", stalled)
    result = await new(prepared, tmp_path, grace_seconds=.04).run()
    assert result.state == "failed" and result.launched is False and result.cleanup_complete is True
    assert result.failure.public() == {"code": "lifecycle_unavailable", "stage": "spawning"}


def test_reflection_is_included_in_shared_grace(prepared):
    from evolution_lab.runner import _grace
    inputs = prepared._inputs
    campaign = inputs.manifest.workflow.campaign
    scorer = inputs.manifest.workflow.scorer
    target = inputs.manifest.target.timeouts
    assert _grace(inputs) == (campaign.editor_timeout_seconds + campaign.reflection_timeout_seconds
        + scorer.timeout_seconds + campaign.budget.timeout_seconds + target.connect_seconds + target.read_seconds + 10)


def test_stop_grace_counts_model_grouping_timeout(tmp_path):
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import _campaign_stop_grace
    from test_owner_binding import MODEL_GROUPING

    root = tmp_path / "model-grouping-sample"
    scaffold_15(root)
    raw = json.loads((root / "manifest.json").read_text())
    base = _campaign_stop_grace(prepare_inputs(root / "manifest.json", env=dict(os.environ)).manifest)
    raw["grouper"] = dict(MODEL_GROUPING)
    (root / "manifest.json").write_text(json.dumps(raw))
    environment = dict(os.environ, GROUPING_KEY="GROUPING-KEY-VALUE", GROUPING_PROXY="http://127.0.0.1:9/")
    model = _campaign_stop_grace(prepare_inputs(root / "manifest.json", env=environment).manifest)
    # The one `timeout_seconds` already spans retries and repairs (manifest /10+): counted once.
    assert model - base == MODEL_GROUPING["timeout_seconds"]


def test_grouping_timeout_is_in_default_grace_and_explicit_override_wins(tmp_path):
    from evolution_lab.client import create_run
    from evolution_lab.runner import _grace

    prepared = grouping_prepared(tmp_path)
    inputs = prepared._inputs
    campaign = inputs.manifest.workflow.campaign
    scorer = inputs.manifest.workflow.scorer
    target = inputs.manifest.target.timeouts
    assert _grace(inputs) == (
        campaign.editor_timeout_seconds
        + campaign.reflection_timeout_seconds
        + scorer.timeout_seconds
        + campaign.budget.timeout_seconds
        + target.connect_seconds
        + target.read_seconds
        + 17
        + 10
    )
    operation = create_run(
        prepared,
        tmp_path / "explicit-run",
        dispatch_guard=lambda: None,
        grace_seconds=0.25,
    )
    assert operation._grace == 0.25


@pytest.mark.usefixtures("ownership_mode")
async def test_destination_cannot_overlap_grouping_program(tmp_path, runtime):
    from evolution_lab.client import create_run

    prepared = grouping_prepared(tmp_path)
    grouping_cwd = prepared._inputs.manifest.workflow.grouper.command.cwd
    operation = create_run(
        prepared, grouping_cwd, dispatch_guard=lambda: None, grace_seconds=0.1
    )
    result = await operation.run()

    assert result.failure.code == "path_conflict" and result.launched is False
    assert not grouping_cwd.with_name(grouping_cwd.name + "-lab-logs").exists()


async def test_legacy_explicit_snapshot_mismatch_precedes_isolated_and_writes(prepared, tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.runner import run_native
    with pytest.raises(InputChanged):
        await run_native(prepared._inputs, tmp_path / "run", dispatch_guard=lambda: None)
    assert not (tmp_path / "run-lab-logs").exists()


@pytest.mark.parametrize("grace", [True, 0, -1, float('nan'), float('inf'), "1"])
def test_create_run_rejects_invalid_grace_without_writes(prepared, tmp_path, grace):
    from evolution_lab.client import ClientError
    with pytest.raises(ClientError) as error:
        new(prepared, tmp_path, grace_seconds=grace)
    assert error.value.failure.code == "invalid_argument"
    assert not (tmp_path / "run-lab-logs").exists()


async def test_stop_never_calls_callback_synchronously(prepared, tmp_path):
    operation = new(prepared, tmp_path)
    operation.stop()
    events = []
    def callback(event):
        events.append(event['kind'])
        assert operation.stop().disposition == "already_requested"
    result = await operation.run(on_event=callback)
    assert events == ['stop_requested', 'completed'] and result.state == 'stopped'


@pytest.mark.usefixtures("ownership_mode")
async def test_cancellation_while_verifying_waits_without_writes(prepared, tmp_path, runtime, monkeypatch):
    from evolution_lab.recipe import PreparedInputs
    operation = new(prepared, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = PreparedInputs.verify
    loop_thread = threading.get_ident()
    def verify(self, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return original(self, **kwargs)
    monkeypatch.setattr(PreparedInputs, 'verify', verify)
    task = asyncio.create_task(operation.run())
    while not entered.is_set():
        await asyncio.sleep(.005)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and operation.result is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operation.result.state == 'stopped'
    assert not (tmp_path / 'run-lab-logs').exists()


@pytest.mark.usefixtures("ownership_mode")
async def test_supervisor_creation_cancellation_retains_and_reaps_owner(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []
    async def spawn(*argv, **kwargs):
        process = await original(*argv, **kwargs)
        processes.append(process)
        entered.set()
        await release.wait()
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    operation = new(prepared, tmp_path)
    task = asyncio.create_task(operation.run())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operation.result.launched is False and operation.result.cleanup_complete is True
    assert processes[0].returncode is not None


@pytest.mark.parametrize('after_write', [False, True])
@pytest.mark.usefixtures("ownership_mode")
async def test_go_write_uncertainty_cannot_infer_no_launch(prepared, tmp_path, runtime, monkeypatch, after_write):
    import evolution_lab._client_operation as module
    original = module.CommandChannel.send_go
    def uncertain(self, operation_id):
        if after_write:
            original(self, operation_id)
        raise module.ChannelClosed('private')
    monkeypatch.setattr(module.CommandChannel, 'send_go', uncertain)
    result = await new(prepared, tmp_path).run()
    # Before write: supervisor may still acknowledge a no-spawn shutdown. After
    # write: positive core facts must win over the local write exception.
    if after_write and result.launched:
        assert result.exit_code is not None
    assert result.failure.code == 'lifecycle_unavailable'
    if result.launched is False:
        assert result.stop_effect == 'prevented_start'


@pytest.mark.usefixtures("ownership_mode")
async def test_missing_cleanup_preserves_exit_as_unknown(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    original = module.open_event_channel
    async def open_channel(*args):
        channel = await original(*args)
        read = channel.read
        async def read_without_cleanup():
            event = await read()
            if event and event['kind'] == 'cleanup_complete':
                return await read()
            return event
        channel.read = read_without_cleanup
        return channel
    monkeypatch.setattr(module, 'open_event_channel', open_channel)
    result = await new(prepared, tmp_path).run()
    assert (result.state, result.launched, result.exit_code, result.cleanup_complete) == ('unknown', True, 7, None)
    assert any(f.code == 'lifecycle_unavailable' for f in (result.failure, *result.secondary_failures))


@pytest.mark.usefixtures("ownership_mode")
async def test_guard_baseexception_is_propagated_after_cleanup(prepared, tmp_path, runtime):
    class Abort(BaseException):
        pass
    operation = None
    fatal = Abort()
    runtime('import time; time.sleep(60)')
    def guard():
        if operation._status_fields()['launched'] is True:
            raise fatal
    operation = new(prepared, tmp_path, dispatch_guard=guard)
    with pytest.raises(Abort) as caught:
        await operation.run()
    assert caught.value is fatal
    assert operation.result.launched is True and operation.result.cleanup_complete is True


@pytest.mark.usefixtures("ownership_mode")
async def test_completed_baseexception_still_publishes_frozen_result(prepared, tmp_path, runtime):
    class Abort(BaseException):
        pass
    operation = new(prepared, tmp_path)
    def callback(event):
        if event['kind'] == 'completed':
            raise Abort()
    with pytest.raises(Abort):
        await operation.run(on_event=callback)
    assert operation.result.exit_code == 7 and operation.result.persistence_state == 'saved'


@pytest.mark.usefixtures("ownership_mode")
async def test_environment_verifier_refusal_is_not_input_drift(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    from evolution_lab.provenance import IsolatedEnvironmentError
    def invalid(*args, **kwargs):
        raise IsolatedEnvironmentError('private')
    monkeypatch.setattr(module, 'verify_isolated', invalid)
    result = await new(prepared, tmp_path).run()
    assert result.failure.code == 'environment_invalid' and result.launched is False
    assert not (tmp_path / 'run-lab-logs').exists()


@pytest.mark.usefixtures("ownership_mode")
async def test_destination_cannot_overlap_reflector_program(prepared, tmp_path, runtime):
    from evolution_lab.client import create_run
    reflector = prepared._inputs.manifest.workflow.reflector.role.cwd
    operation = create_run(prepared, reflector, dispatch_guard=lambda: None)
    result = await operation.run()
    assert result.failure.code == 'path_conflict' and result.launched is False


@pytest.mark.usefixtures("ownership_mode")
async def test_post_go_lost_supervisor_does_not_invent_exit_or_cleanup(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    operation = new(prepared, tmp_path)
    def uncertain(self, operation_id):
        operation._process.kill()  # supervisor is still pre-core; no orphan fixture
        raise module.ChannelClosed()
    monkeypatch.setattr(module.CommandChannel, 'send_go', uncertain)
    result = await operation.run()
    assert (result.state, result.launched, result.exit_code, result.cleanup_complete) == ('unknown', None, None, None)
    assert result.failure.code == 'lifecycle_unavailable'
    assert operation.stop().disposition == 'already_terminal'


@pytest.mark.usefixtures("ownership_mode")
async def test_known_core_spawn_refusal_is_not_supervisor_exit(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    monkeypatch.setattr(module, '_core_argv', lambda inputs, run: (str(tmp_path / 'missing-executable'),))
    result = await new(prepared, tmp_path).run()
    assert (result.state, result.launched, result.exit_code, result.cleanup_complete) == ('failed', False, None, True)
    assert result.failure.public() == {'code': 'spawn_failed', 'stage': 'spawning'}


@pytest.mark.usefixtures("ownership_mode")
async def test_supervisor_spawn_refusal_closes_all_private_fds(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    original = os.pipe
    descriptors = []
    def pipe():
        pair = original()
        descriptors.extend(pair)
        return pair
    async def refused(*args, **kwargs):
        raise OSError('private')
    monkeypatch.setattr(module.os, 'pipe', pipe)
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', refused)
    result = await new(prepared, tmp_path).run()
    assert result.failure.code == 'spawn_failed' and result.cleanup_complete is True
    assert len(descriptors) == 4
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.usefixtures("ownership_mode")
async def test_cancellation_during_publication_keeps_terminal_status_consistent(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    entered, release = threading.Event(), threading.Event()
    snapshots, events = [], []
    def publish(logs, snapshot):
        snapshots.append(snapshot)
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(module, '_publish_execution', publish)
    operation = new(prepared, tmp_path)
    task = asyncio.create_task(operation.run(on_event=lambda e: events.append(e['kind'])))
    while not entered.is_set():
        await asyncio.sleep(.005)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ['core_started', 'core_exited', 'completed']
    assert operation.result.stop_requested == snapshots[0].stop_requested
    assert operation._status_fields()['stop_requested'] == operation.result.stop_requested


@pytest.mark.usefixtures("ownership_mode")
async def test_unexpected_internal_cancellation_is_finalized_without_operational_failure(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    async def cancelled(self):
        raise asyncio.CancelledError()
    monkeypatch.setattr(module.RunOperation, '_spawn', cancelled)
    operation = new(prepared, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await operation.run()
    assert operation.result.state == 'stopped' and operation.result.failure is None
    assert operation.result.stop_reason == 'cancelled'


@pytest.mark.usefixtures("ownership_mode")
async def test_positive_facts_override_no_go_inference_without_inventing_cleanup(prepared, tmp_path, runtime, monkeypatch):
    import evolution_lab._client_operation as module
    operation = new(prepared, tmp_path)
    original = module.open_event_channel
    async def open_channel(*args):
        channel = await original(*args)
        read = channel.read
        async def contradicting_events():
            event = await read()
            if event is None:
                return None
            kind = event['kind']
            if kind == 'ready':
                operation.stop()
            elif kind == 'spawn_skipped':
                event = dict(event, kind='core_started', data={})
            elif kind == 'cleanup_complete':
                event = dict(event, kind='core_exited', data={'exit_code': 7})
            return event
        channel.read = contradicting_events
        return channel
    monkeypatch.setattr(module, 'open_event_channel', open_channel)
    result = await operation.run()
    assert result.launched is True and result.exit_code == 7
    assert result.state == 'unknown' and result.cleanup_complete is None


@pytest.mark.parametrize('origin', ['started_callback', 'external_stop', 'cancellation'])
@pytest.mark.usefixtures("ownership_mode")
async def test_unsent_stop_overtaken_by_buffered_exit_is_too_late(
    prepared, tmp_path, runtime, monkeypatch, origin
):
    import evolution_lab._client_operation as module
    operation = new(prepared, tmp_path)
    original = module.RunOperation._pump
    events = []

    def pump(self):
        if self._go_attempted:
            # Batch the real supervisor's start/exit/cleanup, so this ordering
            # does not depend on child speed or scheduler timing.
            if not self._reader.done():
                return
            if origin == 'external_stop':
                self.stop()
        original(self)

    def callback(event):
        events.append(event['kind'])
        if origin == 'started_callback' and event['kind'] == 'core_started':
            operation.stop()
        elif origin == 'cancellation' and event['kind'] == 'core_started':
            raise asyncio.CancelledError()

    monkeypatch.setattr(module.RunOperation, '_pump', pump)
    task = asyncio.create_task(operation.run(on_event=callback))
    if origin == 'cancellation':
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    result = operation.result
    assert (result.state, result.exit_code, result.cleanup_complete,
            result.stop_effect, result.failure) == ('exited', 7, True, 'too_late', None)
    assert result.stop_reason == ('cancelled' if origin == 'cancellation' else 'user')
    assert not operation._stop_sent
    if origin == 'cancellation':
        assert events == ['core_started']  # callback is disabled after its exception
    else:
        assert events.count('stop_requested') == 1 and events[-1] == 'completed'


@pytest.mark.usefixtures("ownership_mode")
async def test_attempted_stop_with_missing_ack_remains_unknown(
    prepared, tmp_path, runtime, monkeypatch
):
    import evolution_lab._client_operation as module
    runtime('import time; time.sleep(60)')
    operation = new(prepared, tmp_path)
    original = module.open_event_channel

    async def open_channel(*args):
        channel = await original(*args)
        read = channel.read
        async def missing_ack():
            while True:
                event = await read()
                if event is None or event['kind'] != 'stop_effect':
                    return event
        channel.read = missing_ack
        return channel

    monkeypatch.setattr(module, 'open_event_channel', open_channel)
    def callback(event):
        if event['kind'] == 'core_started':
            operation.stop()
    result = await operation.run(on_event=callback)
    assert operation._stop_sent and result.exit_code is not None
    assert (result.state, result.cleanup_complete, result.stop_effect) == ('failed', True, 'unknown')
    assert result.failure.code == 'lifecycle_unavailable'


@pytest.mark.usefixtures("ownership_mode")
async def test_go_attempt_preserves_already_confirmed_start(
    prepared, tmp_path, runtime, monkeypatch
):
    import evolution_lab._client_operation as module
    operation = new(prepared, tmp_path)
    original = module.RunOperation._pump
    injected = False
    at_write = []

    def pump(self):
        nonlocal injected
        if self._queue is not None and not self._queue.empty() and not injected:
            # The first queued fact is ready. This defensive contradictory
            # positive fact must survive the subsequent local GO transition.
            self._queue.put_nowait({'kind': 'core_started', 'data': {}})
            injected = True
        original(self)

    def uncertain_go(self, operation_id):
        at_write.append((operation._go_attempted, operation._launched))
        operation._process.kill()  # real supervisor is still waiting before GO
        raise module.ChannelClosed()

    monkeypatch.setattr(module.RunOperation, '_pump', pump)
    monkeypatch.setattr(module.CommandChannel, 'send_go', uncertain_go)
    result = await operation.run()
    assert at_write == [(True, True)]
    assert result.state == 'unknown' and result.launched is True
    assert result.exit_code is None and result.cleanup_complete is None
    assert result.failure.code == 'lifecycle_unavailable'
