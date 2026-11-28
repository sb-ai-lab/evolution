"""Final review F2, F3: probe calls at service shutdown and a broken probe history (spec 2026-10-02 §5.1)."""
from __future__ import annotations

import asyncio
import gc
import sqlite3
import time
import uuid

import httpx
import pytest

from test_owner_probe_bundle import _TOKEN, make_probe_bundle

from evolution_lab.owner import Owner, OwnerError
from evolution_lab.owner.probe import ProbeTurnOutcome
from evolution_lab.projects.probes import ProbeStore
from evolution_lab.projects.session import ProjectSession, client_ref
from evolution_lab.service.app import create_app
from evolution_lab.service.errors import ServiceError
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

pytestmark = pytest.mark.slow

WAIT = 0.2


def config(tmp_path):
    tmp_path.chmod(0o700)
    return ServiceConfig(tmp_path / 'service.sock', tmp_path / 'state', WAIT, home_directory=tmp_path / 'home')


def _open_session(runtime) -> str:
    """One open session in the history, on a target of this Owner generation."""
    sid = uuid.uuid4().hex
    target = {'id': 't1', 'owner_generation': runtime.owner.owner_generation, 'boot_id': 'boot',
              'bundle_digest': 'b' * 64, 'launch_digest': 'l' * 64, 'source': 'entry', 'seed_mode': 'none'}
    runtime.project_session._probes().create_session({
        'id': sid, 'project': 'pr', 'implementation': 'im', 'reset_of': None, 'created_at': '2026-10-02T00:00:00Z',
        'record': {'target': target, 'seed': {'requested': None, 'effective': None}, 'ceiling': {'max_turns': 5},
                   'budget': None, 'turn_timeout_seconds': 600}})
    return sid


@pytest.mark.asyncio
async def test_shutdown_bounds_the_probe_drain_refuses_queued_turns_and_closes_history_last(tmp_path, monkeypatch):
    runtime = ServiceRuntime(config(tmp_path))
    assert await runtime.start()
    sid = _open_session(runtime)
    entered, interrupted = asyncio.Event(), asyncio.Event()
    real_close, real_history_close, real_probe_turn = Owner.close, ProjectSession.close, ProjectSession.probe_turn
    order, turns = [], []

    async def states(self):
        return {sid: 'uncertain' if interrupted.is_set() else 'open'}

    async def turn(self, session_id, content, *, timeout):
        turns.append(content)
        entered.set()
        await interrupted.wait()  # a slow agent: only the Owner close ends this turn
        return ProbeTurnOutcome(None, 'target_unreachable', 'uncertain')

    async def close(self, **kwargs):
        if kwargs:
            order.append('owner_close')
            interrupted.set()
        return await real_close(self, **kwargs)

    def history_close(self):
        order.append('history_close')
        real_history_close(self)

    async def probe_turn(self, session_id, content):
        try:
            return await real_probe_turn(self, session_id, content)
        finally:
            order.append(f'returned:{content}')

    monkeypatch.setattr(Owner, 'probe_states', states)
    monkeypatch.setattr(Owner, 'probe_turn', turn)
    monkeypatch.setattr(Owner, 'close', close)
    monkeypatch.setattr(ProjectSession, 'close', history_close)
    monkeypatch.setattr(ProjectSession, 'probe_turn', probe_turn)
    running = asyncio.create_task(runtime.call_projects('probe_turn', sid, 'first'))
    await entered.wait()
    queued = asyncio.create_task(runtime.call_projects('probe_turn', sid, 'second'))
    await asyncio.sleep(0.05)  # queued on the probe lock
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(runtime.shutdown(), 10)
    finally:
        interrupted.set()
    assert loop.time() - started < 5  # the budget is WAIT; the turn alone would never end
    for call in (running, queued):
        with pytest.raises(OwnerError) as caught:
            await call
        assert caught.value.code == 'service_stopping'
    assert turns == ['first']  # the queued turn never reached the Owner
    # Every probe call returned before the history closed; the queued one was refused once the lock was free.
    assert order == ['owner_close', 'returned:first', 'returned:second', 'history_close']
    store = ProbeStore.open(tmp_path / 'home')
    try:  # the interrupted turn stays pending: the next start records it interrupted (target_gone)
        assert store.pending_turns() == [(sid, 1)] and store.session(sid)['state'] == 'open'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_shutdown_bounds_a_probe_settlement_the_owner_close_leaves_running(tmp_path):
    """Codex r2 R2-2 (spec §5.1): a probe close whose agent hangs in close_session outlives the Owner close (its
    target stop waits out a 15 s grace); shutdown does not wait for it. The Owner row stays uncertain, the call
    answers service_stopping, nothing reaches the loop's exception log, and the next start reconciles the session
    target_gone."""
    budget = 1
    state, digest = make_probe_bundle(tmp_path, mode="close_sleep:20", stop_grace_seconds=15)
    tmp_path.chmod(0o700)
    cfg = ServiceConfig(tmp_path / 'service.sock', state, budget, home_directory=tmp_path / 'home')
    work = tmp_path / 'work'
    (work / 'im').mkdir(parents=True)
    (work / 'lab').mkdir()
    loop = asyncio.get_running_loop()
    noise, handler = [], loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: noise.append(context))
    try:
        runtime = ServiceRuntime(cfg)
        assert await runtime.start()
        try:
            runtime.projects.create(id='pr', name='P', materials_root='lab', path=str(work),
                                    implementations=[{'id': 'im', 'name': 'I', 'place': 'im'}])
            connection = await runtime.owner.bind(digest)
            task = await runtime.owner.start_target(connection, secrets=_TOKEN, client_ref=client_ref('pr', 'im'))
            assert (await asyncio.wait_for(runtime.owner.wait(task), 12)).state == 'succeeded'
            sid = (await runtime.call_projects('probe_start', 'pr', 'im', ceiling={'max_turns': 3},
                                               budget=None))['id']
            closing = asyncio.create_task(runtime.call_projects('probe_close', sid))
            await asyncio.sleep(0.3)  # the close is on the target, the agent sleeping in close_session
            assert not closing.done()
        finally:
            started = time.monotonic()
            await asyncio.wait_for(runtime.shutdown(), 60)
            elapsed = time.monotonic() - started
        # The probe calls get the budget, then the Owner close gets it; nothing waits for the late settlement.
        assert elapsed < 2 * budget + 3, elapsed
        with pytest.raises(OwnerError) as caught:
            await closing
        assert caught.value.code == 'service_stopping'
        # The settlement the Owner close gave up on ends once the target's grace is over; it changes nothing.
        late = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert late
        _, pending = await asyncio.wait(late, timeout=30)
        assert not pending, pending
        from evolution_lab.owner.store import OwnerStateStore
        with OwnerStateStore.open(state) as store:
            assert [(s, v) for s, _, v in store.probe_rows()] == [(sid, 'uncertain')]
        history = ProbeStore.open(tmp_path / 'home')
        try:
            assert history.session(sid)['state'] == 'open'  # not recorded closed: the release never settled
        finally:
            history.close()
        again = ServiceRuntime(cfg)
        assert await again.start()  # the start reconciles the session of the earlier generation
        await again.shutdown()
        history = ProbeStore.open(tmp_path / 'home')
        try:
            row = history.session(sid)
            assert (row['state'], row['closed_reason']) == ('closed', 'target_gone')
        finally:
            history.close()
        gc.collect()
        await asyncio.sleep(0)
        assert noise == []
    finally:
        loop.set_exception_handler(handler)


@pytest.mark.asyncio
async def test_unreadable_probe_history_does_not_prevent_start(tmp_path):
    cfg = config(tmp_path)
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    db = sqlite3.connect(home / 'probes.sqlite3')
    db.execute('PRAGMA user_version = 2')  # a history version this Lab does not know
    db.close()
    runtime = ServiceRuntime(cfg)
    assert await runtime.start()
    try:
        assert [d['error']['code'] for d in runtime.diagnostics] == ['history_unavailable']
        transport = httpx.ASGITransport(app=create_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url='http://lab') as client:
            assert (await client.get('/v1/scenarios')).status_code == 200
            sid = 'f' * 32
            refused = await client.get(f'/v1/probes/{sid}')
            assert refused.status_code == 409 and refused.json()['error']['code'] == 'history_unavailable'
            closed = await client.delete(f'/v1/probes/{sid}')
            assert closed.status_code == 409 and closed.json()['error']['code'] == 'history_unavailable'
            (home / 'probes.sqlite3').unlink()  # the file is fixed: probes work again
            missing = await client.get(f'/v1/probes/{sid}')
            assert missing.status_code == 404 and missing.json()['error']['code'] == 'probe_session_not_found'
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_damaged_probe_history_does_not_prevent_start(tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    (home / 'probes.sqlite3').write_bytes(b'not a database' * 512)
    runtime = ServiceRuntime(config(tmp_path))
    assert await runtime.start()
    try:
        assert [d['error']['code'] for d in runtime.diagnostics] == ['history_unavailable']
        with pytest.raises(ServiceError) as caught:
            await runtime.call_projects('probe_session', 'f' * 32)
        assert caught.value.code == 'history_unavailable'
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_probe_history_damaged_past_its_header_answers_history_unavailable(tmp_path):
    runtime = ServiceRuntime(config(tmp_path))
    assert await runtime.start()
    try:
        path = tmp_path / 'home' / 'probes.sqlite3'
        runtime.project_session.close()  # drop the open handle, then damage every page after the header
        data = bytearray(path.read_bytes())
        data[100:] = b'\xff' * (len(data) - 100)
        path.write_bytes(bytes(data))
        with pytest.raises(ServiceError) as caught:
            await runtime.call_projects('probe_session', 'f' * 32)
        assert caught.value.code == 'history_unavailable'
    finally:
        await runtime.shutdown()
