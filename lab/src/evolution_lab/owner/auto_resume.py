"""Owner-owned policy decisions and scheduling for paused campaigns."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import threading

from evolution_lab._suspension import last_suspension
from .models import OwnerError, ResourceRef, TargetRef


class AdmissionFence:
    """Order cancellation against the complete start-acceptance transaction."""
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._committed = False
        self.admission_ready = asyncio.Event()

    def is_set(self):
        return self._cancelled.is_set()

    def cancel(self):
        with self._lock:
            return self._cancel_locked()

    def try_cancel(self):
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._cancel_locked()
        finally:
            self._lock.release()

    def _cancel_locked(self):
        if self._committed:
            return False
        self._cancelled.set()
        return True

    @contextmanager
    def guard(self):
        with self._lock:
            if self.is_set():
                raise OwnerError('dispatch_refused', 'before_start')
            yield
            self._committed = True


async def cancel_fence(fence):
    result = fence.try_cancel()
    return await asyncio.to_thread(fence.cancel) if result is None else result


def next_time(seconds, *, base=None):
    return ((base or datetime.now(timezone.utc)) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def initial_state(status, rules, policy_digest, *, secrets_available, previous_suspensions=None, finished_at=None):
    used = sum(s.initiated_by == 'auto_resume' for s in status.starts)
    value = {'policy_digest': policy_digest, 'state': 'off', 'next_at': None, 'used': used,
             'max': None if rules is None else rules.max_resumes, 'blocked': None, 'detail': None}
    if rules is None or status.kind != 'campaign':
        return value
    if status.control_state in {'reserved', 'running', 'stopping'}:
        value['state'] = 'running'
        return value
    core = None if status.result is None else status.result.core
    if core is not None and core.get('campaign_outcome') is not None:
        value['state'] = 'closed'
        return value
    latest = status.starts[-1] if status.starts else None
    failure = status.failure or status.refusal or status.core_terminal
    suspension = last_suspension(core)
    if suspension is not None and not secrets_available:
        value.update(state='blocked', blocked={'code': 'secrets_unavailable', 'detail': None})
        return value
    rows = core.get('suspensions', ()) if core is not None else ()
    new_pause = previous_suspensions is not None and len(rows or ()) > previous_suspensions
    failed_start = (latest is not None and not new_pause
                    and (failure is not None or previous_suspensions is not None))
    if failed_start:
        code = None if failure is None else failure.code
        detail = {'refusal': code} if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,128}', code) else None
        value.update(state='blocked', blocked={'code': 'start_failed',
                                              'detail': detail})
        return value
    if suspension is None:
        value.update(state='blocked', blocked={'code': 'start_failed', 'detail': None})
    elif suspension['reason'] not in rules.reasons:
        value.update(state='blocked', blocked={'code': 'reason_not_listed', 'detail': None})
    elif used >= rules.max_resumes:
        value['state'] = 'exhausted'
    elif not secrets_available:
        value.update(state='blocked', blocked={'code': 'secrets_unavailable', 'detail': None})
    else:
        value.update(state='waiting', next_at=next_time(rules.delay_seconds, base=finished_at))
    return value


class AutoResumeBlocked(Exception):
    def __init__(self, code, detail=None):
        self.code, self.detail = code, detail


async def perform_step(owner, ref, policy_digest, secrets, cancellation=None):
    """Take the ordinary cleanup and resume paths for one paused operation."""
    value = await owner.status(ref)
    if value.resume is not None and value.resume.state == 'cleanup_first':
        try:
            task = await owner._cleanup_auto(ref)
            cleanup = await owner.wait(task)
        except OwnerError as error:
            if error.code in {'busy', 'resource_busy', 'target_slot_held', 'probe_sessions_open'}:
                raise
            raise AutoResumeBlocked('cleanup_failed') from None
        current = await owner._db('operation_status', ref.id)
        if (cleanup.state != 'succeeded' or current.control_state != 'released'
                or current.target_cleanup.state not in {'confirmed', 'not_needed'}
                or current.target_cleanup.complete is not True):
            raise AutoResumeBlocked('cleanup_failed')
    target = await owner.target(value.target_ref)
    requirement = None if value.resume is None else value.resume.target
    excluded = () if requirement is None else requirement.excluded
    runtime = owner._targets.get(value.target_ref.id)
    if (target.state == 'ready' and value.target_ref not in excluded and runtime is not None
            and runtime.policy_digest == policy_digest):
        await owner._resume_auto(ref, target_ref=value.target_ref, secrets=secrets['execution'], cancellation=cancellation)
        return
    used = {start.target_ref for start in value.starts}
    for candidate in await owner.list_targets(bundle=target.bundle_digest, state='ready'):
        candidate_ref = TargetRef(candidate.id, candidate.owner_generation)
        live = owner._targets.get(candidate.id)
        if (candidate_ref not in used and candidate_ref not in excluded and live is not None
                and candidate.execution_policy == policy_digest and live.policy_digest == policy_digest):
            await owner._resume_auto(ref, target_ref=candidate_ref, secrets=secrets['execution'], cancellation=cancellation)
            return
    if target.state == 'ready' and value.target_ref in excluded and runtime is not None:
        stop = await owner._admit_target_stop(value.target_ref, runtime, require_probe_free=True)
        result = await owner.wait(stop)
        if result.state != 'succeeded':
            raise AutoResumeBlocked('target_start_failed')
    try:
        connection = await owner.bind(target.bundle_digest)
        task = await owner.start_target(connection, secrets=secrets['target'], execution_policy=policy_digest)
        result = await owner.wait(task)
    except OwnerError as error:
        if error.code in {'busy', 'resource_busy', 'target_slot_held', 'probe_sessions_open'}:
            raise
        raise AutoResumeBlocked('target_start_failed') from None
    if result.state != 'succeeded' or result.ref is None:
        raise AutoResumeBlocked('target_start_failed')
    new_target = TargetRef(result.ref.id, owner.owner_generation)
    await owner._resume_auto(ref, target_ref=new_target, secrets=secrets['execution'], cancellation=cancellation)


class AutoResumeTimer:
    """One cancellable wait; the step owns cleanup, selection and admission."""
    def __init__(self, state, step, *, delay, busy_recheck):
        self.state, self.step = state, step
        self.delay, self.busy_recheck = delay, busy_recheck
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def cancel(self, *, cancelled=True):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if cancelled:
            self.state.update(state='cancelled', next_at=None, blocked=None, detail=None)

    async def _run(self):
        delay = self.delay
        while True:
            await asyncio.sleep(delay)
            self.state.update(state='running', next_at=None, blocked=None, detail=None)
            try:
                await self.step()
            except OwnerError as error:
                if error.code in {'busy', 'resource_busy', 'target_slot_held', 'probe_sessions_open'}:
                    code = 'busy' if error.code == 'probe_sessions_open' else error.code
                    delay = self.busy_recheck
                    self.state.update(state='waiting', next_at=next_time(delay), blocked=None,
                                      detail={'busy': {'code': code}})
                    continue
                self.state.update(state='blocked', next_at=None, detail=None,
                                  blocked={'code': 'resume_refused', 'detail': {'refusal': error.code}})
            except AutoResumeBlocked as error:
                self.state.update(state='blocked', next_at=None, detail=None,
                                  blocked={'code': error.code, 'detail': error.detail})
            except Exception:
                self.state.update(state='blocked', next_at=None, detail=None,
                                  blocked={'code': 'start_failed', 'detail': None})
            else:
                self.state.update(state='running', next_at=None, blocked=None, detail=None)
            return


@dataclass
class _Entry:
    scope_id: str
    rules: object
    policy_digest: str | None
    credentials: dict | None
    state: dict
    timer: AutoResumeTimer | None = None
    cancelled: bool = False
    cancellation: object = None
    previous_suspensions: int | None = None


class AutoResumeManager:
    def __init__(self, owner):
        self.owner = owner
        self.entries = {}

    async def started(self, status, target, execution_secrets, *, automatic=False, previous_core=None):
        old = self.entries.get(status.id)
        if not automatic and old is not None and old.timer is not None and old.timer.task is not asyncio.current_task():
            await cancel_fence(old.cancellation)
            await old.timer.cancel(cancelled=False)
        policy = target.execution_policy
        rules = None if policy is None else policy.auto_resume
        credentials = ({'execution': dict(execution_secrets),
                        'target': dict(target.requested_secrets or {})}
                       if rules is not None and status.kind == 'campaign' else None)
        entry = _Entry(status.starts[-1].scope_id, rules, target.policy_digest, credentials,
                       initial_state(status, rules, target.policy_digest, secrets_available=True),
                       cancellation=AdmissionFence(),
                       previous_suspensions=(len(previous_core.get('suspensions') or ())
                                             if previous_core is not None else 0))
        if old is not None:
            old.credentials = None
        self.entries[status.id] = entry
        return entry.state

    async def finished(self, status, runtime, *, finished_at=None):
        entry = self.entries.get(status.id)
        if (entry is None or entry.scope_id != runtime.scope_id
                or self.owner._operations.get(status.id) is not runtime):
            return
        entry.state = initial_state(status, entry.rules, entry.policy_digest,
                                    secrets_available=entry.credentials is not None,
                                    previous_suspensions=entry.previous_suspensions, finished_at=finished_at)
        if entry.cancelled and entry.state['state'] not in {'off', 'closed'}:
            entry.state.update(state='cancelled', next_at=None, blocked=None, detail=None)
        if entry.state['state'] not in {'waiting', 'running'}:
            entry.credentials = None
        if entry.state['state'] != 'waiting':
            return
        ref = ResourceRef('operation', status.id)
        async def step():
            await perform_step(self.owner, ref, entry.policy_digest, entry.credentials, entry.cancellation)
        deadline = datetime.fromisoformat(entry.state['next_at'].replace('Z', '+00:00'))
        delay = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
        entry.timer = AutoResumeTimer(entry.state, step, delay=delay,
                                      busy_recheck=entry.rules.busy_recheck_seconds)
        entry.timer.start()
        def settled(_):
            if entry.state['state'] not in {'waiting', 'running'}:
                entry.credentials = None
        entry.timer.task.add_done_callback(settled)

    async def project(self, status):
        entry = self.entries.get(status.id)
        if entry is not None and status.starts and entry.scope_id == status.starts[-1].scope_id:
            core = None if status.result is None else status.result.core
            if entry.rules is not None and core is not None and core.get('campaign_outcome') is not None \
                    and status.control_state not in {'reserved', 'running', 'stopping'}:
                entry.state.update(state='closed', next_at=None, blocked=None, detail=None)
                entry.credentials = None
            return entry.state
        history = await self.owner._db('operation_policy_history', status.id)
        digest = history[-1]['policy_digest'] if history else None
        rules = None
        if digest is not None:
            from evolution_lab.execution_policy import parse_policy
            record = await self.owner._db('execution_policy', digest)
            rules = parse_policy(record.to_payload()['policy']).auto_resume
        return initial_state(status, rules, digest, secrets_available=False)

    def projection_failed(self, runtime):
        entry = self.entries.get(runtime.id)
        if entry is not None and entry.scope_id == runtime.scope_id and not entry.cancelled and entry.rules is not None:
            entry.state.update(state='blocked', next_at=None, detail=None,
                               blocked={'code': 'start_failed', 'detail': None})
            entry.credentials = None

    async def cancel(self, operation_id, *, accepted_scope=None):
        """Cancel a timer, or the start whose stop/delete was already accepted.

        A too-late stop must not change terminal reasons or suppress a finish
        callback that has not scheduled its waiting timer yet.
        """
        entry = self.entries.get(operation_id)
        if entry is None:
            return False
        waiting = entry.state['state'] == 'waiting' or (
            entry.timer is not None and not entry.timer.task.done())
        running = accepted_scope == entry.scope_id and entry.state['state'] == 'running'
        if not waiting and not running:
            return False
        cancelled_before_accept = await cancel_fence(entry.cancellation)
        if not cancelled_before_accept:
            await entry.cancellation.admission_ready.wait()
            # Admission won: Owner.stop must now accept a stop of the new
            # runtime before changing its auto-resume entry.
            return False
        entry.cancelled = True
        entry.credentials = None
        if entry.timer is not None:
            await entry.timer.cancel(cancelled=waiting)
        if entry.state['state'] not in {'off', 'closed'}:
            entry.state.update(state='cancelled', next_at=None, blocked=None, detail=None)
        return waiting

    async def close(self):
        for entry in tuple(self.entries.values()):
            cancelled_before_accept = await cancel_fence(entry.cancellation)
            if not cancelled_before_accept:
                await entry.cancellation.admission_ready.wait()
            if entry.timer is not None:
                await entry.timer.cancel(cancelled=False)
            entry.credentials = None
        self.entries.clear()
