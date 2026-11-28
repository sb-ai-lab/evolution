"""Auto-resume decisions and coordination use in-memory fake operations."""
from types import SimpleNamespace
import asyncio

import pytest

from evolution_lab.execution_policy import AutoResumeRules


def status(*, reason='reflector_failing', starts=(), control='finished', complete=False, failure=None):
    core = {'suspended': reason is not None, 'complete': complete,
            'campaign_outcome': {'outcome': 'completed'} if complete else None,
            'suspensions': [] if reason is None else [{'reason': reason}]}
    return SimpleNamespace(kind='campaign', starts=starts, control_state=control,
                           result=SimpleNamespace(core=core), failure=failure,
                           refusal=None, core_terminal=None)


def start(who='human', attempt=None):
    return SimpleNamespace(initiated_by=who, auto_resume_attempt=attempt)


def rules(*, reasons=frozenset({'reflector_failing'}), max_resumes=2, delay_seconds=1):
    return AutoResumeRules(reasons, max_resumes, delay_seconds, 1)


def test_auto_resume_off_without_block():
    from evolution_lab.owner.auto_resume import initial_state
    value = initial_state(status(starts=(start(),)), None, None, secrets_available=True)
    assert value['state'] == 'off' and value['blocked'] is None


def test_auto_resume_listed_reason_waits():
    from evolution_lab.owner.auto_resume import initial_state
    value = initial_state(status(starts=(start(),)), rules(), 'a' * 64, secrets_available=True)
    assert value['state'] == 'waiting' and value['next_at'] and value['used'] == 0


def test_next_time_is_based_on_start_completion():
    from datetime import datetime, timezone
    from evolution_lab.owner.auto_resume import initial_state
    ended = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    value = initial_state(status(starts=(start(),)), rules(delay_seconds=60), 'a' * 64,
                          secrets_available=True, finished_at=ended)
    assert value['next_at'] == '2026-01-01T00:01:00Z'


def test_auto_resume_reason_not_listed_and_exhausted():
    from evolution_lab.owner.auto_resume import initial_state
    value = initial_state(status(reason='editor_failing', starts=(start(),)), rules(), 'a' * 64,
                          secrets_available=True)
    assert value['blocked']['code'] == 'reason_not_listed'
    value = initial_state(status(starts=(start(), start('auto_resume', 1))), rules(max_resumes=1),
                          'a' * 64, secrets_available=True)
    assert value['state'] == 'exhausted' and value['used'] == 1


def test_auto_resume_closed_start_failure_and_secrets_unavailable():
    from evolution_lab.owner.auto_resume import initial_state
    assert initial_state(status(reason=None, complete=True, starts=(start(),)), rules(), 'a' * 64,
                         secrets_available=True)['state'] == 'closed'
    failed = status(starts=(start(), start('auto_resume', 1)), failure=SimpleNamespace(code='io_error'))
    assert initial_state(failed, rules(), 'a' * 64, secrets_available=True)['blocked']['code'] == 'start_failed'
    assert initial_state(status(starts=(start(),)), rules(), 'a' * 64,
                         secrets_available=False)['blocked']['code'] == 'secrets_unavailable'


@pytest.mark.parametrize('reason,used', [('editor_failing', 0), ('reflector_failing', 1)])
def test_paused_campaign_without_secrets_reports_unavailable_first(reason, used):
    from evolution_lab.owner.auto_resume import initial_state
    starts = (start(),) if not used else (start(), start('auto_resume', 1))
    value = initial_state(status(reason=reason, starts=starts), rules(max_resumes=1), 'a' * 64,
                          secrets_available=False)
    assert value['blocked']['code'] == 'secrets_unavailable'


def test_auto_resume_without_pause_or_outcome_is_start_failure():
    from evolution_lab.owner.auto_resume import initial_state
    value = initial_state(status(reason=None, complete=False, starts=(start(), start('auto_resume', 1))),
                          rules(), 'a' * 64, secrets_available=True)
    assert value['state'] == 'blocked' and value['blocked']['code'] == 'start_failed'


def test_no_core_outcome_never_reports_closed():
    from evolution_lab.owner.auto_resume import initial_state
    value = initial_state(status(reason=None, complete=False, starts=(start(),)), rules(), 'a' * 64,
                          secrets_available=True)
    assert value['state'] == 'blocked' and value['blocked']['code'] == 'start_failed'
    value = status(reason=None, complete=True, starts=(start(),))
    value.result.core['campaign_outcome'] = None
    assert initial_state(value, rules(), 'a' * 64, secrets_available=True)['state'] == 'blocked'


def test_new_pause_is_distinct_from_stale_pause_on_failed_auto_start():
    from evolution_lab.owner.auto_resume import initial_state
    value = status(starts=(start(), start('auto_resume', 1)), failure=SimpleNamespace(code='io_error'))
    value.result.core['suspensions'].append({'reason': 'reflector_failing'})
    fresh = initial_state(value, rules(), 'a' * 64, secrets_available=True, previous_suspensions=1)
    assert fresh['state'] == 'waiting' and fresh['used'] == 1
    stale = initial_state(value, rules(), 'a' * 64, secrets_available=True, previous_suspensions=2)
    assert stale['state'] == 'blocked' and stale['blocked']['code'] == 'start_failed'


@pytest.mark.asyncio
async def test_busy_waits_without_spending_and_retries():
    from evolution_lab.owner.auto_resume import AutoResumeTimer
    from evolution_lab.owner.models import OwnerError
    attempts = []
    finished = asyncio.Event()

    async def step():
        attempts.append('step')
        if len(attempts) == 1:
            raise OwnerError('busy', 'resume')
        finished.set()

    state = initial_waiting()
    timer = AutoResumeTimer(state, step, delay=0, busy_recheck=.05)
    timer.start()
    await asyncio.sleep(.01)
    assert state['state'] == 'waiting' and state['detail'] == {'busy': {'code': 'busy'}}
    assert state['used'] == 0 and len(attempts) == 1
    await asyncio.wait_for(finished.wait(), .5)
    await timer.task
    assert state['state'] == 'running' and state['detail'] is None


@pytest.mark.asyncio
async def test_stop_cancels_wait_and_close_has_no_task():
    from evolution_lab.owner.auto_resume import AutoResumeTimer
    attempts = []

    async def step():
        attempts.append(True)

    state = initial_waiting()
    timer = AutoResumeTimer(state, step, delay=1, busy_recheck=1)
    timer.start()
    await timer.cancel()
    assert state['state'] == 'cancelled' and timer.task.done()
    assert attempts == []


def initial_waiting():
    return {'policy_digest': 'a' * 64, 'state': 'waiting', 'next_at': '2026-01-01T00:00:00Z',
            'used': 0, 'max': 2, 'blocked': None, 'detail': None}


@pytest.mark.asyncio
async def test_timer_exposes_running_without_expired_deadline_while_step_executes():
    from evolution_lab.owner.auto_resume import AutoResumeTimer
    entered, release = asyncio.Event(), asyncio.Event()
    async def step():
        entered.set()
        await release.wait()
    state = initial_waiting()
    timer = AutoResumeTimer(state, step, delay=0, busy_recheck=1)
    timer.start()
    try:
        await entered.wait()
        assert state['state'] == 'running' and state['next_at'] is None
    finally:
        release.set()
        await timer.task


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['no_policy', 'no_rules', 'evaluation', 'closed', 'exhausted', 'blocked'])
async def test_manager_retains_credentials_only_while_auto_resume_can_use_them(mode):
    from evolution_lab.owner.auto_resume import AutoResumeManager
    owner = SimpleNamespace(_operations={})
    manager = AutoResumeManager(owner)
    policy = None if mode == 'no_policy' else SimpleNamespace(auto_resume=None if mode == 'no_rules' else rules(max_resumes=1))
    target = SimpleNamespace(execution_policy=policy, policy_digest='a' * 64,
                             requested_secrets={'TARGET': 'private-sentinel'})
    current = status(starts=(start('auto_resume', 1) if mode == 'exhausted' else start(),), control='running')
    current.id = 'b' * 32
    current.starts[0].scope_id = 'c' * 32
    if mode == 'evaluation':
        current.kind = 'evaluation'
    runtime = SimpleNamespace(id=current.id, scope_id='c' * 32)
    owner._operations[current.id] = runtime
    await manager.started(current, target, {'ROLE': 'private-sentinel'})
    entry = manager.entries[current.id]
    if mode in {'no_policy', 'no_rules', 'evaluation'}:
        assert entry.credentials is None
    else:
        assert entry.credentials is not None
        current.control_state = 'finished'
        if mode == 'closed':
            current.result.core['campaign_outcome'] = {'outcome': 'completed'}
        if mode == 'blocked':
            current.result.core['suspensions'] = [{'reason': 'editor_failing'}]
        await manager.finished(current, runtime)
        assert entry.state['state'] == mode
        assert entry.credentials is None
    await manager.close()


@pytest.mark.asyncio
async def test_step_cleanup_and_resume_on_current_target():
    from evolution_lab.owner.auto_resume import perform_step
    from evolution_lab.owner.models import ResourceRef, TargetRef
    target = TargetRef('a' * 32, 'b' * 32)
    ref = ResourceRef('operation', 'c' * 32)
    calls = []

    class FakeOwner:
        def __init__(self):
            self.owner_generation = target.owner_generation
            self._targets = {target.id: SimpleNamespace(policy_digest='d' * 64)}

        async def status(self, _):
            return SimpleNamespace(resume=SimpleNamespace(state='cleanup_first', target=None),
                target_ref=target, starts=(SimpleNamespace(target_ref=target),),
                prepared_digest='e' * 64)

        async def _cleanup_auto(self, ref):
            calls.append('cleanup')
            return 'cleanup-task'

        async def wait(self, task):
            assert task == 'cleanup-task'
            return SimpleNamespace(state='succeeded')

        async def _db(self, method, *args):
            assert method == 'operation_status'
            return SimpleNamespace(control_state='released', target_cleanup=SimpleNamespace(state='confirmed', complete=True))

        async def target(self, _):
            return SimpleNamespace(state='ready', bundle_digest='f' * 64)

        async def _resume_auto(self, got_ref, *, target_ref, secrets, cancellation=None):
            assert got_ref == ref and target_ref == target and secrets == {'TOKEN': 'sentinel'}
            calls.append('resume')

    await perform_step(FakeOwner(), ref, 'd' * 64,
                       {'execution': {'TOKEN': 'sentinel'}, 'target': {}})
    assert calls == ['cleanup', 'resume']


@pytest.mark.asyncio
async def test_unstable_target_stopped_and_other_policy_ignored():
    from evolution_lab.owner.auto_resume import perform_step
    from evolution_lab.owner.models import ResourceRef, TargetRef
    generation = 'b' * 32
    old = TargetRef('a' * 32, generation)
    other = TargetRef('d' * 32, generation)
    fresh = TargetRef('e' * 32, generation)
    ref = ResourceRef('operation', 'c' * 32)
    calls = []

    class FakeOwner:
        def __init__(self):
            self.owner_generation = generation
            self._targets = {old.id: SimpleNamespace(policy_digest='1' * 64),
                             other.id: SimpleNamespace(policy_digest='2' * 64)}

        async def status(self, _):
            return SimpleNamespace(resume=SimpleNamespace(state='ready', target=SimpleNamespace(excluded=(old,))),
                target_ref=old, starts=(SimpleNamespace(target_ref=old),), prepared_digest='f' * 64)

        async def target(self, _):
            return SimpleNamespace(state='ready', bundle_digest='f' * 64)

        async def list_targets(self, **_):
            return (SimpleNamespace(id=other.id, owner_generation=generation, state='ready',
                                    bundle_digest='f' * 64, execution_policy='2' * 64),)

        async def _admit_target_stop(self, target_ref, runtime, *, require_probe_free):
            assert target_ref == old and require_probe_free is True
            calls.append('stop')
            return 'stop-task'

        async def bind(self, bundle):
            assert bundle == 'f' * 64
            return 'connection'

        async def start_target(self, connection, *, secrets, execution_policy):
            assert connection == 'connection' and secrets == {'TARGET': 'secret'} and execution_policy == '1' * 64
            calls.append('start')
            return 'start-task'

        async def wait(self, task):
            return SimpleNamespace(state='succeeded', ref=ResourceRef('target', fresh.id) if task == 'start-task' else None)

        async def _resume_auto(self, got_ref, *, target_ref, secrets, cancellation=None):
            assert got_ref == ref and target_ref == fresh
            calls.append('resume')

    await perform_step(FakeOwner(), ref, '1' * 64,
                       {'execution': {}, 'target': {'TARGET': 'secret'}})
    assert calls == ['stop', 'start', 'resume']


@pytest.mark.asyncio
async def test_cleanup_failure_blocks_instead_of_resuming():
    from evolution_lab.owner.auto_resume import AutoResumeBlocked, perform_step
    from evolution_lab.owner.models import ResourceRef

    class FakeOwner:
        async def status(self, _):
            return SimpleNamespace(resume=SimpleNamespace(state='cleanup_first'))
        async def _cleanup_auto(self, _):
            return 'cleanup'
        async def wait(self, _):
            return SimpleNamespace(state='failed')
        async def _db(self, *_):
            return SimpleNamespace(control_state='cleanup_required',
                                   target_cleanup=SimpleNamespace(state='unconfirmed', complete=None))

    with pytest.raises(AutoResumeBlocked) as caught:
        await perform_step(FakeOwner(), ResourceRef('operation', 'a' * 32), 'b' * 64, {})
    assert caught.value.code == 'cleanup_failed'


@pytest.mark.asyncio
async def test_new_target_start_failure_is_blocked():
    from evolution_lab.owner.auto_resume import AutoResumeBlocked, perform_step
    from evolution_lab.owner.models import OwnerError, ResourceRef, TargetRef
    target = TargetRef('a' * 32, 'b' * 32)
    class FakeOwner:
        _targets = {}
        async def status(self, _):
            return SimpleNamespace(resume=SimpleNamespace(state='ready', target=None), target_ref=target,
                                   starts=(SimpleNamespace(target_ref=target),))
        async def target(self, _):
            return SimpleNamespace(state='stopped', bundle_digest='c' * 64)
        async def list_targets(self, **_):
            return ()
        async def bind(self, _):
            return 'connection'
        async def start_target(self, *_, **__):
            raise OwnerError('target_unavailable', 'target_start')
    with pytest.raises(AutoResumeBlocked) as caught:
        await perform_step(FakeOwner(), ResourceRef('operation', 'd' * 32), 'e' * 64,
                           {'execution': {}, 'target': {}})
    assert caught.value.code == 'target_start_failed'


@pytest.mark.asyncio
async def test_failed_admission_blocks_without_a_repeat():
    from evolution_lab.owner.auto_resume import AutoResumeTimer
    from evolution_lab.owner.models import OwnerError
    calls = []
    async def step():
        calls.append(1)
        raise OwnerError('binding_mismatch', 'before_start')
    state = initial_waiting()
    timer = AutoResumeTimer(state, step, delay=0, busy_recheck=.01)
    timer.start()
    await timer.task
    await asyncio.sleep(.03)
    assert len(calls) == 1
    assert state['blocked'] == {'code': 'resume_refused', 'detail': {'refusal': 'binding_mismatch'}}


@pytest.mark.asyncio
async def test_manual_start_with_higher_limit_reenables_after_exhaustion():
    from evolution_lab.owner.auto_resume import AutoResumeManager
    owner = SimpleNamespace(_operations={})
    manager = AutoResumeManager(owner)
    old = status(starts=(start(), start('auto_resume', 1)))
    old.id = 'a' * 32
    old.starts[-1].scope_id = 'b' * 32
    low = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules(max_resumes=1)),
                          policy_digest='c' * 64, requested_secrets={})
    runtime = SimpleNamespace(id=old.id, scope_id='b' * 32)
    owner._operations[old.id] = runtime
    await manager.started(old, low, {})
    await manager.finished(old, runtime)
    assert manager.entries[old.id].state['state'] == 'exhausted'
    high = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules(max_resumes=3, delay_seconds=60)),
                           policy_digest='d' * 64, requested_secrets={})
    new = status(starts=(*old.starts, start()), control='running')
    new.id = old.id
    new.starts[-1].scope_id = 'e' * 32
    await manager.started(new, high, {})
    runtime = SimpleNamespace(id=new.id, scope_id='e' * 32)
    owner._operations[new.id] = runtime
    new.control_state = 'finished'
    await manager.finished(new, runtime)
    assert manager.entries[new.id].state['state'] == 'waiting'
    assert manager.entries[new.id].state['used'] == 1 and manager.entries[new.id].state['max'] == 3
    await manager.close()


@pytest.mark.asyncio
async def test_delete_results_cancels_wait_and_clears_credentials():
    from evolution_lab.owner import Owner
    from evolution_lab.owner.auto_resume import AutoResumeManager
    from evolution_lab.owner.models import ResourceRef, TaskRef
    owner = Owner()
    owner._loop = asyncio.get_running_loop()
    owner._assert_loop = lambda: None
    owner._stopping = owner._closed = False
    owner._operations, owner._tasks = {}, {}
    owner._result_io = SimpleNamespace(acquire_write=lambda *_: None)
    owner._hold_result = lambda factory, _: asyncio.create_task(factory())
    owner._auto_resume = AutoResumeManager(owner)
    ref = ResourceRef('operation', 'a' * 32)
    row = status(starts=(start(),))
    row.id = ref.id
    row.starts[0].scope_id = 'b' * 32
    target = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules(delay_seconds=60)),
                             policy_digest='c' * 64, requested_secrets={'TARGET': 'private'})
    runtime = SimpleNamespace(id=ref.id, scope_id='b' * 32, done=asyncio.get_running_loop().create_future())
    runtime.done.set_result(None)
    owner._operations[ref.id] = runtime
    await owner._auto_resume.started(row, target, {'ROLE': 'private'})
    await owner._auto_resume.finished(row, runtime)
    entry = owner._auto_resume.entries[ref.id]
    assert entry.state['state'] == 'waiting'
    task_ref = TaskRef('d' * 32, 'e' * 32)
    async def db(method, *args, **kwargs):
        if method == 'operation_status':
            return SimpleNamespace(control_state='finished', results_state='present', starts=row.starts)
        if method == 'deletion_record':
            return None
        if method == 'owned_inventory':
            return ()
        if method == 'accept_deletion':
            return SimpleNamespace(task_ref=task_ref)
        return None
    async def summary(*_):
        return None
    async def result_thread(*_):
        return ()
    async def finish(*_, **__):
        return None
    owner._db, owner._delete_summary, owner._result_thread = db, summary, result_thread
    owner._finish_deletion_task = finish
    owner._register_task = lambda task, *_: SimpleNamespace(ref=task)
    owner._invalidate_deleted_payloads = lambda _: None
    assert await owner.delete_results(ref) == task_ref
    assert entry.credentials is None and entry.timer.task.done()
    await owner._auto_resume.close()


@pytest.mark.asyncio
async def test_manager_runs_one_timer_and_releases_secrets_on_close(monkeypatch):
    from evolution_lab.owner import auto_resume
    from evolution_lab.owner.models import ResourceRef
    called = asyncio.Event()
    ref = ResourceRef('operation', 'c' * 32)

    async def step(owner, got_ref, policy, credentials, cancellation):
        assert got_ref == ref and credentials['execution']['TOKEN'] == 'secret-sentinel'
        called.set()

    monkeypatch.setattr(auto_resume, 'perform_step', step)
    owner = SimpleNamespace(_operations={})
    manager = auto_resume.AutoResumeManager(owner)
    target = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules(delay_seconds=0)),
                             policy_digest='a' * 64, requested_secrets={'TARGET': 'secret-sentinel'})
    current = status(starts=(start(),), control='running')
    current.id = ref.id
    current.starts[0].scope_id = 'd' * 32
    runtime = SimpleNamespace(id=ref.id, scope_id='d' * 32)
    owner._operations[ref.id] = runtime
    await manager.started(current, target, {'TOKEN': 'secret-sentinel'})
    current.control_state = 'finished'
    await manager.finished(current, runtime)
    await asyncio.wait_for(called.wait(), .5)
    assert manager.entries[ref.id].state['state'] in {'waiting', 'running'}
    await manager.close()
    assert manager.entries == {}


@pytest.mark.asyncio
async def test_unreadable_finished_projection_blocks_without_leaking_error():
    from evolution_lab.owner import Owner
    from evolution_lab.owner.auto_resume import AutoResumeManager
    owner = Owner()
    owner._stopping = False
    owner._operations = {}
    owner._auto_resume = AutoResumeManager(owner)
    target = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules()),
                             policy_digest='a' * 64, requested_secrets={})
    current = status(starts=(start(),), control='running')
    current.id = 'b' * 32
    current.starts[0].scope_id = 'c' * 32
    runtime = SimpleNamespace(id=current.id, scope_id='c' * 32)
    owner._operations[current.id] = runtime
    await owner._auto_resume.started(current, target, {'ROLE': 'secret-sentinel'})
    async def bad_status(_):
        raise RuntimeError('secret-sentinel')
    owner.status = bad_status
    tasks = []
    def hold(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task
    owner._hold = hold
    owner._schedule_auto_resume(runtime)
    await asyncio.gather(*tasks, return_exceptions=True)
    state = owner._auto_resume.entries[current.id].state
    assert state['state'] == 'blocked' and state['blocked']['code'] == 'start_failed'
    assert 'secret-sentinel' not in repr(state)
    await owner._auto_resume.close()


@pytest.mark.asyncio
async def test_owner_waiting_stop_cancels_and_never_persists_secrets(tmp_path, monkeypatch, caplog):
    from evolution_lab.owner import Owner
    from evolution_lab.owner.auto_resume import AutoResumeManager
    from evolution_lab.owner.models import ResourceRef
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_store_starts import DIGEST, _executor, _ready_target

    sentinel = 'sentinel-auto-resume-private-78132'
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        target_ref = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target_ref, executor=_executor())
    ref = ResourceRef('operation', accepted.status.id)
    owner = Owner()
    owner._auto_resume = AutoResumeManager(owner)
    owner._assert_loop = lambda: None
    owner._assert_mutation = lambda: None
    owner._result_io = SimpleNamespace(writing=lambda _: False, acquire_read=lambda *_: None)
    async def work(callback, _lease):
        return await callback()
    async def projection(*_, **__):
        return accepted.status
    owner._result_work = work
    owner._status_projection = projection
    target = SimpleNamespace(execution_policy=SimpleNamespace(auto_resume=rules(delay_seconds=60)),
                             policy_digest='a' * 64, requested_secrets={'TARGET': sentinel})
    await owner._auto_resume.started(accepted.status, target, {'TOKEN': sentinel})
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    runtime = SimpleNamespace(id=ref.id, scope_id=accepted.scope_id, done=done)
    owner._operations = {ref.id: runtime}
    fake_finished = status(starts=accepted.status.starts)
    fake_finished.id = ref.id
    await owner._auto_resume.finished(fake_finished, runtime)
    assert (await owner.status(ref)).auto_resume['state'] == 'waiting'
    receipt = await owner.stop(ref)
    assert receipt.accepted
    assert (await owner.status(ref)).auto_resume['state'] == 'cancelled'
    assert sentinel not in repr((await owner.status(ref)).to_payload())
    await owner._auto_resume.close()
    assert sentinel not in caplog.text
    for path in state.rglob('*'):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path


@pytest.mark.asyncio
async def test_fake_end_to_end_auto_resume_and_secrets_after_close_restart(tmp_path, caplog):
    from evolution_lab.execution_policy import parse_policy, policy_digest
    from evolution_lab.owner.auto_resume import AutoResumeManager
    from evolution_lab.owner.models import ResourceRef
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_store_starts import DIGEST, ZERO_DIGEST, ONE_DIGEST, _executor, _finish_normally

    sentinel = 'sentinel-fake-auto-role-private-18812'
    state_path = tmp_path / 'state'
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['reflector_failing'], 'max_resumes': 2, 'delay_seconds': 0}}
    digest = policy_digest(document)
    policy = parse_policy(document)
    done = asyncio.Event()
    with OwnerStateStore.open(state_path) as store:
        store.confirm_execution_policy(document, expected_digest=digest, author='operator', channel='api')
        connection = store.bind_connection(bundle_digest=ZERO_DIGEST, launch_digest=ONE_DIGEST,
                                           source_kind='campaign')
        target_task = store.accept_target_start(connection_id=connection.id, launch_digest=ONE_DIGEST,
                    bundle_digest=ZERO_DIGEST, base_url='http://127.0.0.1:8123', execution_policy=digest)
        store.mark_target_ready(target_task.task_ref.id, boot_id='fake')
        target_ref = target_task.target_ref
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target_ref, executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        ref = ResourceRef('operation', first.status.id)
        target = SimpleNamespace(execution_policy=policy, policy_digest=digest,
                                 requested_secrets={'TARGET_TOKEN': sentinel})

        class FakeOwner:
            def __init__(self):
                self.owner_generation = store.generation
                self._targets = {target_ref.id: target}
                self._operations = {}
                self.manager = AutoResumeManager(self)
                self.core_complete = False

            async def status(self, _):
                current = store.operation_status(ref.id)
                value = status(reason=None if self.core_complete else 'reflector_failing',
                               complete=self.core_complete, starts=current.starts)
                value.id = ref.id
                value.resume = SimpleNamespace(state='ready', target=None)
                value.target_ref = target_ref
                value.prepared_digest = DIGEST
                return value

            async def target(self, _):
                return SimpleNamespace(state='ready', bundle_digest=ZERO_DIGEST)

            async def _resume_auto(self, _, *, target_ref, secrets, cancellation=None):
                assert secrets == {'ROLE_TOKEN': sentinel}
                next_start = store.accept_resume(ref.id, target_ref=target_ref, executor=_executor(),
                                                 initiated_by='auto_resume')
                runtime = SimpleNamespace(id=ref.id, scope_id=next_start.scope_id)
                self._operations[ref.id] = runtime
                await self.manager.started(next_start.status, target, secrets, automatic=True)
                _finish_normally(store, ref.id, next_start.scope_id)
                self.core_complete = True
                await self.manager.finished(await self.status(ref), runtime)
                done.set()

            async def _db(self, method, *args):
                return getattr(store, method)(*args)

        owner = FakeOwner()
        runtime = SimpleNamespace(id=ref.id, scope_id=first.scope_id)
        owner._operations[ref.id] = runtime
        await owner.manager.started(first.status, target, {'ROLE_TOKEN': sentinel})
        await owner.manager.finished(await owner.status(ref), runtime)
        await asyncio.wait_for(done.wait(), .5)
        assert owner.manager.entries[ref.id].state['state'] == 'closed'
        assert owner.manager.entries[ref.id].state['used'] == 1
        assert owner.manager.entries[ref.id].credentials is None
        await owner.manager.close()
    with OwnerStateStore.open(state_path) as reopened:
        persisted = reopened.operation_status(ref.id)
        assert [(s.initiated_by, s.auto_resume_attempt) for s in persisted.starts] == [
            ('human', None), ('auto_resume', 1)]
        class NewOwner:
            async def _db(self, method, *args):
                return getattr(reopened, method)(*args)
        paused = status(starts=persisted.starts)
        paused.id = ref.id
        projection = await AutoResumeManager(NewOwner()).project(paused)
        assert projection['blocked']['code'] == 'secrets_unavailable'
        assert projection['used'] == 1
        assert sentinel not in repr(projection)
    assert sentinel not in caplog.text
    for path in state_path.rglob('*'):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path


@pytest.mark.asyncio
@pytest.mark.parametrize('admission_race', ['none', 'before_commit', 'after_commit', 'raise_limit', 'manual_unlisted', 'manual_waiting', 'pre_dispatch_failure', 'manual_after_cancel', 'target_start_failed', 'manual_failure', 'manual_terminal', 'stop_running_pause', 'stop_running_outcome', 'stop_unlisted', 'stop_exhausted', 'stop_finish_pending', 'stop_db_refused'])
async def test_public_owner_fake_role_auto_resumes_and_close_drops_secrets(tmp_path, monkeypatch, caplog, admission_race):
    from dataclasses import dataclass
    from evolution_lab.execution_policy import parse_policy, policy_digest
    from evolution_lab.owner import Owner
    from evolution_lab.owner.auto_resume import AutoResumeManager
    from evolution_lab.owner.models import ResourceRef, ResumeVerdict
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_store_starts import DIGEST, ZERO_DIGEST, ONE_DIGEST, _executor, _finish_normally
    import evolution_lab.owner.binding as binding
    import evolution_lab.owner.execution as execution
    import evolution_lab.owner.secrets as secrets_module
    import evolution_lab.owner.policy as policy_module
    import evolution_lab.owner.tasks as task_module

    sentinel = 'sentinel-owner-fake-role-831704'
    root = tmp_path / 'state'
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing' if admission_race in {'manual_unlisted', 'stop_unlisted'} else 'reflector_failing'],
        'max_resumes': 1 if admission_race in {'raise_limit', 'stop_exhausted'} else 2,
        'delay_seconds': 60 if admission_race in {'manual_waiting', 'manual_failure', 'manual_terminal', 'stop_finish_pending', 'stop_db_refused'} else 0}}
    digest = policy_digest(document)
    policy = parse_policy(document)
    store = OwnerStateStore.open(root)
    store.confirm_execution_policy(document, expected_digest=digest, author='operator', channel='api')
    connection = store.bind_connection(bundle_digest=ZERO_DIGEST, launch_digest=ONE_DIGEST,
                                       source_kind='campaign')
    target_task = store.accept_target_start(connection_id=connection.id, launch_digest=ONE_DIGEST,
        bundle_digest=ZERO_DIGEST, base_url='http://127.0.0.1:8123', execution_policy=digest)
    store.mark_target_ready(target_task.task_ref.id, boot_id='fake')
    target_ref = target_task.target_ref
    loop = asyncio.get_running_loop()
    owner = Owner()
    owner._loop, owner._owned, owner._operations = loop, set(), {}
    owner.owner_generation = store.generation
    owner._stopping = owner._closed = False
    owner._close_task = owner._shutdown_result = None
    owner._conformance, owner._tasks, owner._target_cache = {}, {}, {}
    owner._operation_cache = {}
    owner._auto_resume = AutoResumeManager(owner)
    owner._assert_loop = owner._assert_mutation = lambda: None
    owner._result_io = SimpleNamespace(writing=lambda _: False, held=lambda _: False,
                                       acquire_read=lambda *_: None)
    owner.state_directory = root
    owner._storage = object()
    owner._executor = SimpleNamespace(shutdown=lambda **_: None)
    fake_target = SimpleNamespace(ref=target_ref, execution_policy=policy, policy_digest=digest,
        requested_secrets={'TARGET_TOKEN': sentinel}, secrets=SimpleNamespace(values={}), gateway=None, helper=None,
        binding=SimpleNamespace(source_kind='campaign'))
    async def request_stop():
        return None
    fake_target.request_stop = request_stop
    owner._targets = {target_ref.id: fake_target}
    target_start_attempts = []
    if admission_race == 'target_start_failed':
        from evolution_lab.owner.models import OwnerError
        async def fake_bind(_):
            return 'connection'
        async def failed_start(*_, **__):
            target_start_attempts.append(1)
            raise OwnerError('target_unavailable', 'target_start')
        owner.bind = fake_bind
        owner.start_target = failed_start
    prepared = SimpleNamespace(kind='campaign', prepared_digest=DIGEST,
                               bundle=SimpleNamespace(bundle_digest=ZERO_DIGEST))
    monkeypatch.setattr(binding, 'open_prepared', lambda *_: prepared)
    monkeypatch.setattr(binding, 'declared_executor_identity', lambda *_, **__: _executor())
    monkeypatch.setattr(secrets_module, 'bind_execution_secrets', lambda *_, **__: SimpleNamespace())
    monkeypatch.setattr(policy_module, 'execution_tokens', lambda *_, **__: {})
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', immediate)
    admission_entered, release_admission = asyncio.Event(), asyncio.Event()
    release_role = asyncio.Event()
    async def db(method, *args, **kwargs):
        if method == 'request_operation_stop' and admission_race == 'stop_db_refused':
            release_role.set()
            await owner._operations[args[0]].done
        if method == 'accept_resume' and admission_race == 'before_commit' and kwargs.get('initiated_by') == 'auto_resume':
            admission_entered.set()
            await release_admission.wait()
        result = getattr(store, method)(*args, **kwargs)
        if method == 'accept_resume' and admission_race == 'after_commit' and kwargs.get('initiated_by') == 'auto_resume':
            admission_entered.set()
            await release_admission.wait()
        return result
    owner._db = db
    async def result_work(callback, _lease):
        return await callback()
    owner._result_work = result_work
    owner._close_all_probes = lambda _: immediate(lambda: None)
    owner._drain_result_work = lambda: immediate(lambda: None)
    role_calls = []
    failures = []
    original_failure_for = task_module.failure_for
    def record_failure(error, stage):
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        failures.append((type(error).__name__, getattr(error, 'name', None), stage,
                         (frames[-1].name, frames[-1].lineno) if frames else None))
        return original_failure_for(error, stage)
    monkeypatch.setattr(task_module, 'failure_for', record_failure)
    finished = asyncio.Event()
    role_entered = asyncio.Event()

    @dataclass
    class Projected:
        id: str
        kind: str
        starts: tuple
        control_state: str
        target_ref: object
        prepared_digest: str
        result: object
        failure: object
        refusal: object
        core_terminal: object
        process: object
        resume: object
        auto_resume: object = None

    def core():
        if admission_race in {'pre_dispatch_failure', 'manual_failure', 'manual_terminal', 'stop_running_pause'} and role_calls:
            return {'suspended': True, 'complete': False, 'suspensions': [{'reason': 'reflector_failing'}]}
        if admission_race == 'stop_running_outcome' and role_calls:
            return {'suspended': False, 'complete': True, 'campaign_outcome': {'outcome': 'stopped'}, 'suspensions': []}
        completion_count = 4 if admission_race == 'raise_limit' else 3 if admission_race in {'manual_after_cancel', 'stop_exhausted'} else 2
        paused = len(role_calls) < completion_count
        return {'suspended': paused, 'complete': not paused,
                'campaign_outcome': None if paused else {'outcome': 'completed'},
                'suspensions': [{'reason': 'reflector_failing'} for _ in range(len(role_calls))] if paused else []}

    async def projection(ref, **_):
        value = store.operation_status(ref.id)
        return Projected(value.id, value.kind, value.starts, value.control_state, value.target_ref,
            value.prepared_digest, SimpleNamespace(core=core()), value.failure, value.refusal,
            value.core_terminal, value.process, ResumeVerdict('ready', None) if core()['suspended'] else None)
    owner._status_projection = projection
    monkeypatch.setattr(execution, '_fresh_core', lambda _: core())

    async def run_operation(runtime, _declared):
        timeout_count = 3 if admission_race == 'raise_limit' else 2 if admission_race == 'manual_after_cancel' else 1
        role_calls.append('fake-timeout' if len(role_calls) < timeout_count else 'fake-success')
        role_entered.set()
        if admission_race == 'stop_db_refused':
            await release_role.wait()
        if admission_race.startswith('stop_running_'):
            await asyncio.wait_for(runtime.stop.wait(), 1)
        if admission_race == 'after_commit' and len(role_calls) == 2:
            await asyncio.wait_for(runtime.stop.wait(), 1)
        if admission_race in {'pre_dispatch_failure', 'manual_failure'} and len(role_calls) == 2:
            from evolution_lab.owner.models import Failure
            store.settle_pre_dispatch_failure(runtime.id, Failure.make('io_error', 'before_start'))
        else:
            _finish_normally(store, runtime.id, runtime.scope_id)
        if admission_race == 'target_start_failed' and len(role_calls) == 1:
            from evolution_lab.owner.models import Failure
            store.settle_target_no_helper(target_ref.id, Failure.make('target_unavailable', 'target_start'))
        if len(role_calls) == (3 if admission_race == 'manual_after_cancel' else 2):
            finished.set()
    owner._run_operation = run_operation
    deferred = []
    schedule = owner._schedule_auto_resume
    if admission_race in {'stop_finish_pending', 'stop_db_refused'}:
        owner._schedule_auto_resume = deferred.append
    accepted = await owner.start(DIGEST, target_ref=target_ref, secrets={'ROLE_TOKEN': sentinel})
    ref = ResourceRef('operation', accepted.id)
    if admission_race in {'stop_unlisted', 'stop_exhausted', 'stop_finish_pending', 'stop_db_refused'}:
        if admission_race == 'stop_finish_pending':
            await asyncio.wait_for(owner._operations[ref.id].done, 1)
            assert deferred and (await owner.status(ref)).auto_resume['state'] == 'running'
        elif admission_race == 'stop_db_refused':
            await role_entered.wait()
        else:
            expected = 'exhausted' if admission_race == 'stop_exhausted' else 'blocked'
            for _ in range(100):
                if (await owner.status(ref)).auto_resume['state'] == expected:
                    break
                await asyncio.sleep(0)
            assert (await owner.status(ref)).auto_resume['state'] == expected
        before = dict((await owner.status(ref)).auto_resume)
        receipt = await owner.stop(ref)
        assert not receipt.accepted and receipt.effect == 'too_late'
        assert (await owner.status(ref)).auto_resume == before
        assert not owner._auto_resume.entries[ref.id].cancelled
        if admission_race in {'stop_finish_pending', 'stop_db_refused'}:
            schedule(deferred[0])
            for _ in range(100):
                if (await owner.status(ref)).auto_resume['state'] == 'waiting':
                    break
                await asyncio.sleep(0)
            assert (await owner.status(ref)).auto_resume['state'] == 'waiting'
        await owner.close()
        return
    if admission_race.startswith('stop_running_'):
        await asyncio.wait_for(role_entered.wait(), 1)
        assert (await owner.status(ref)).auto_resume['state'] == 'running'
        assert (await owner.stop(ref)).accepted
        await asyncio.wait_for(owner._operations[ref.id].done, 1)
        for _ in range(20):
            await asyncio.sleep(0)
        current = await owner.status(ref)
        assert current.auto_resume['state'] == ('closed' if admission_race.endswith('outcome') else 'cancelled')
        assert len(current.starts) == 1
        assert owner._auto_resume.entries[ref.id].credentials is None
        await owner.close()
        return
    if admission_race == 'target_start_failed':
        for _ in range(100):
            current = await owner.status(ref)
            if current.auto_resume['state'] == 'blocked':
                break
            await asyncio.sleep(0)
        assert current.auto_resume['blocked']['code'] == 'target_start_failed'
        assert current.auto_resume['used'] == 0 and target_start_attempts == [1]
        await asyncio.sleep(.03)
        assert target_start_attempts == [1] and len(current.starts) == 1
        await owner.close()
        assert sentinel not in caplog.text
        for path in root.rglob('*'):
            if path.is_file():
                assert sentinel.encode() not in path.read_bytes(), path
        return
    if admission_race in {'before_commit', 'after_commit'}:
        await asyncio.wait_for(admission_entered.wait(), 1)
        stop = asyncio.create_task(owner.stop(ref))
        await asyncio.sleep(0)
        release_admission.set()
        receipt = await asyncio.wait_for(stop, 1)
        assert receipt.accepted
        for _ in range(20):
            await asyncio.sleep(0)
        expected = ['human'] if admission_race == 'before_commit' else ['human', 'auto_resume']
        assert [s.initiated_by for s in store.operation_status(ref.id).starts] == expected
        if admission_race == 'after_commit':
            assert owner._operations[ref.id].stop.is_set()
            assert (await owner.status(ref)).auto_resume['state'] == 'closed'
        await owner.close()
        assert sentinel not in caplog.text
        for path in root.rglob('*'):
            if path.is_file():
                assert sentinel.encode() not in path.read_bytes(), path
        return
    if admission_race in {'manual_unlisted', 'manual_waiting', 'manual_after_cancel', 'manual_failure', 'manual_terminal'}:
        for _ in range(100):
            current = await owner.status(ref)
            if current.auto_resume['state'] in {'blocked', 'waiting'}:
                break
            await asyncio.sleep(0)
        if admission_race == 'manual_unlisted':
            assert current.auto_resume['blocked']['code'] == 'reason_not_listed'
        else:
            assert current.auto_resume['state'] == 'waiting'
        if admission_race == 'manual_after_cancel':
            assert (await owner.stop(ref)).accepted
            assert (await owner.status(ref)).auto_resume['state'] == 'cancelled'
            assert len(store.operation_status(ref.id).starts) == 1
        await owner.resume(ref, target_ref=target_ref, secrets={'ROLE_TOKEN': sentinel})
    try:
        await asyncio.wait_for(finished.wait(), 1)
    except TimeoutError:
        pytest.fail(f"fake campaign did not resume: calls={role_calls}, "
                    f"state={owner._auto_resume.entries[ref.id].state}, failures={failures}")
    if admission_race in {'pre_dispatch_failure', 'manual_failure', 'manual_terminal'}:
        for _ in range(100):
            current = await owner.status(ref)
            if current.auto_resume['state'] == 'blocked':
                break
            await asyncio.sleep(0)
        assert current.auto_resume['blocked']['code'] == 'start_failed'
        assert current.auto_resume['used'] == (1 if admission_race == 'pre_dispatch_failure' else 0)
        await asyncio.sleep(.03)
        assert len(role_calls) == 2
        await owner.close()
        assert sentinel not in caplog.text
        for path in root.rglob('*'):
            if path.is_file():
                assert sentinel.encode() not in path.read_bytes(), path
        return
    if admission_race == 'raise_limit':
        for _ in range(100):
            current = await owner.status(ref)
            if current.auto_resume['state'] == 'exhausted':
                break
            await asyncio.sleep(0)
        assert current.auto_resume['state'] == 'exhausted' and current.auto_resume['used'] == 1
        from evolution_lab.owner.models import Failure
        store.settle_target_no_helper(target_ref.id, Failure.make('target_unavailable', 'target_start'))
        higher = {'schema': 'lab-execution-policy/2', 'auto_resume': {
            'reasons': ['reflector_failing'], 'max_resumes': 3, 'delay_seconds': 0}}
        higher_digest = policy_digest(higher)
        store.confirm_execution_policy(higher, expected_digest=higher_digest, author='operator', channel='api')
        new_task = store.accept_target_start(connection_id=connection.id, launch_digest=ONE_DIGEST,
            bundle_digest=ZERO_DIGEST, base_url='http://127.0.0.1:8123', execution_policy=higher_digest)
        store.mark_target_ready(new_task.task_ref.id, boot_id='fake-new')
        new_ref = new_task.target_ref
        new_target = SimpleNamespace(ref=new_ref, execution_policy=parse_policy(higher), policy_digest=higher_digest,
            requested_secrets={'TARGET_TOKEN': sentinel}, secrets=SimpleNamespace(values={}), gateway=None,
            helper=None, binding=SimpleNamespace(source_kind='campaign'), request_stop=request_stop)
        owner._targets[new_ref.id] = new_target
        await owner.resume(ref, target_ref=new_ref, secrets={'ROLE_TOKEN': sentinel})
    for _ in range(100):
        current = await owner.status(ref)
        if current.auto_resume['state'] == 'closed':
            break
        await asyncio.sleep(0)
    expected_calls = (['fake-timeout', 'fake-timeout', 'fake-timeout', 'fake-success']
                      if admission_race == 'raise_limit' else
                      ['fake-timeout', 'fake-timeout', 'fake-success'] if admission_race == 'manual_after_cancel'
                      else ['fake-timeout', 'fake-success'])
    assert role_calls == expected_calls
    expected_used = 2 if admission_race == 'raise_limit' else 0 if admission_race in {'manual_unlisted', 'manual_waiting'} else 1
    assert current.auto_resume['state'] == 'closed' and current.auto_resume['used'] == expected_used
    expected_starts = [('human', None), ('human', None) if admission_race in {'manual_unlisted', 'manual_waiting'} else ('auto_resume', 1)]
    if admission_race == 'raise_limit':
        expected_starts += [('human', None), ('auto_resume', 2)]
    if admission_race == 'manual_after_cancel':
        expected_starts = [('human', None), ('human', None), ('auto_resume', 1)]
    assert [(s.initiated_by, s.auto_resume_attempt) for s in current.starts] == expected_starts
    assert sentinel not in repr(current)
    await owner.close()
    assert owner._auto_resume.entries == {}
    with OwnerStateStore.open(root) as reopened:
        persisted = reopened.operation_status(ref.id)
        class NewOwner:
            async def _db(self, method, *args):
                return getattr(reopened, method)(*args)
        paused = status(starts=persisted.starts)
        paused.id = ref.id
        recovered = await AutoResumeManager(NewOwner()).project(paused)
        assert recovered['blocked']['code'] == 'secrets_unavailable'
        assert recovered['used'] == expected_used and sentinel not in repr(recovered)
    assert sentinel not in caplog.text
    for path in root.rglob('*'):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path
