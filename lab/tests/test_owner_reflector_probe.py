"""Reflector processes belong to each campaign start's helper scopes, not Core."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import re
import shutil
import socket
import sys

import pytest

from ownership_modes import ownership_mode  # noqa: F401
from scaffold_15 import scaffold_15, scaffold_15_with_legacy_failures
from test_owner_target import make_bundle, ready_target
from test_owner_cleanup import finished
from ports import free_port

pytestmark = pytest.mark.slow


def probe_bundle(tmp_path, *, owned=True, declared=True, port=None, delay=0,
                 startup=3, restarts=1, mode='normal', availability=None, scaffold=scaffold_15):
    from evolution_lab.preparation import EnvironmentInput, describe_environment, PreparationStore
    from test_owner_binding import _installed_lab_wheel, CORE_WHEEL
    port = free_port() if port is None else port
    def probe_scaffold(path):
        sample = scaffold(path)
        doc = json.loads(sample.manifest.read_text())
        if availability is not None:
            from scaffold_16 import as_manifest_16
            doc = as_manifest_16(doc, mode='point')
        doc['reflector']['probe'] = ({'base_url': f'http://127.0.0.1:{port}',
            'health_path': '/neutral-health', 'request_timeout_seconds': .2} if declared else None)
        if availability is not None and availability != 'default' and declared:
            doc['reflector']['probe']['on_unavailable'] = availability
        sample.manifest.write_text(json.dumps(doc))
        shutil.copyfile(Path(__file__).parent / 'owner_fixtures/reflector_probe.py', path / 'probe.py')
        return sample
    environment = describe_environment(EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ()))
    launch = dict(environment_ref=environment.digest, executable=str(environment.executable),
        args=[{'bundle_path': 'probe.py'}, {'literal': '--port'}, {'probe_port': None},
              {'literal': '--delay'}, {'literal': str(delay)}],
        startup_timeout_seconds=startup, stop_grace_seconds=.3, max_restarts=restarts) if owned else None
    state, bundle, _ = make_bundle(tmp_path, launch_schema=3, reflector_probe=launch,
                                  scaffold=probe_scaffold, mode=mode)
    return state, bundle, PreparationStore(state).prepare_campaign(bundle.digest), port


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('owned', [False, True])
def test_continue_dispatches_worker_after_unavailable_health_and_keeps_cleanup(tmp_path, owned):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, port = probe_bundle(tmp_path, owned=owned, availability='continue',
        delay=2, startup=.3, mode='sleep:1')
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', operation.id)
            async def dispatched():
                status = await owner.status(ref)
                return status if status.process.launched else None
            await wait_until(dispatched)
            active = await owner._db('reflector_probe_status', ref.id)
            assert active['state'] == 'running', active
            value = await finished(owner, ref)
            assert value.process.launched is True, value.to_payload()
            assert value.failure is None, value.to_payload()
            await owner._operations[ref.id].done
            rows = await scopes(owner, ref)
            assert len(rows) == int(owned)
            receipts(state, rows)
            log = state / 'scopes' / value.starts[-1].scope_id / 'stderr.log'
            assert (f'evo tree evolve: note: reflector probe at http://127.0.0.1:{port} '
                    'is unavailable at start; reflections continue without it') in log.read_text()
    asyncio.run(scenario())


async def scopes(owner, ref):
    return [s for s in await owner._db('resource_scopes', ref) if s['resource_kind'] == 'reflector_probe']


def receipts(state, rows):
    from evolution_lab.owner.receipts import read_cleanup_receipt
    values = [read_cleanup_receipt(state / s['root_relative'], s['id'], s['owner_generation']) for s in rows]
    assert all(v['schema'] == 'lab-scope-cleanup/2' and v['ownership'] in ('subreaper', 'group_tree_marker') for v in values)
    assert all(r['process_tree_empty'] is True for r in values)
    return values


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('availability', [None, 'default', 'suspend'])
@pytest.mark.parametrize('owned,delay,code', [(False, 0, 'reflector_probe_unavailable'),
    (True, 2, 'reflector_probe_unavailable')])
def test_unavailable_refuses_before_worker_and_releases_only_campaign(tmp_path, owned, delay, code, availability):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, _ = probe_bundle(tmp_path, owned=owned, delay=delay, startup=.3,
                                             availability=availability)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            value = await finished(owner, ref)
            assert value.failure is not None and value.failure.code == code, value.to_payload()
            assert value.control_state == 'released' and value.process.launched is False
            report = (await owner.report(ref)).to_payload()
            assert report['state'] == 'unavailable' and report['report'] is None
            assert report['reflector_probe_state'] == 'present'
            assert report['reflector_probe'] == await owner._db('reflector_probe_status', ref.id)
            assert report['reflector_probe']['launched_by'] == ('lab' if owned else 'operator')
            from evolution_lab.cli.responses import validate
            from evolution_lab.cli.output import _report_lines
            validate(report, 'lab-owner-report/15')
            assert any('launched_by=' in line for line in _report_lines(report))
            assert owner._operations[ref.id].helper is None
            assert not (state / 'artifacts' / ref.id / ref.id / 'campaign.jsonl').exists()
            assert (await owner.target(target)).state == 'ready'
            assert set((await owner._db('reservations'))) == {'target_lifetime'}
            rows = await scopes(owner, ref)
            assert len(rows) == int(owned)
            receipts(state, rows)
            await asyncio.shield(owner._operations[ref.id].done)
            if owned:
                worker_scope = state / 'scopes' / value.starts[-1].scope_id
                notes = (worker_scope / 'stderr.log').read_text()
                # The probe never gets healthy within its 0.3 s startup, which also covers the helper and
                # the child's spawn. On a loaded host the budget can end before the child starts: no
                # "started" note, and no exit code (#475). Once started, Lab stops it with SIGTERM: it
                # exits 0 with its handler installed and -15 before that. All are the ordinary stop; a
                # forced kill would be -9, with a "cleanup was forced" note.
                exited = re.search(r'evo tree evolve: note: reflector probe exited with code (0|-15|None); restart 0\n', notes)
                assert exited, notes
                started = 'evo tree evolve: note: reflector probe started at ' in notes
                assert started == (exited.group(1) != 'None'), notes
                assert 'cleanup was forced' not in notes
                assert not (worker_scope / 'cleanup.receipt').exists()
                assert not (worker_scope / 'stdout.log').exists()
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_probe_launch_and_finalization_use_real_helper_and_target_environment(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, port = probe_bundle(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            value = await finished(owner, ref)
            rows = await scopes(owner, ref)
            assert len(rows) == 1, value.to_payload()
            assert value.control_state == 'finished', value.to_payload()
            receipt, = receipts(state, rows)
            assert receipt['child_launched'] and receipt['child_exit_code'] == 0
            facts = json.loads((state / rows[0]['root_relative'] / 'stdout.log').read_text())
            assert facts['target_env'] and facts['target_secret'] and not facts['execution_secret']
            assert Path(facts['cwd']).is_relative_to(state)
            worker = state / 'scopes' / value.starts[-1].scope_id / 'stderr.log'
            assert f'evo tree evolve: note: reflector probe started at http://127.0.0.1:{port}' in worker.read_text()
            record = await owner._db('reflector_probe_status', ref.id)
            assert record == {'launched_by': 'lab', 'state': 'stopped', 'restarts': 0, 'last_exit': 0}
            view = await owner.campaign(ref)
            report = (await owner.report(ref)).to_payload()
            markdown = await owner.report_markdown(ref)
            assert view['reflector_probe'] == report['reflector_probe'] == record
            assert view['reflector_probe_state'] == report['reflector_probe_state'] == 'present'
            assert report['report']['reflector_probe'] == record
            assert markdown.state == 'available'
            assert 'reflector probe: launched_by=lab state=stopped restarts=0' in markdown.markdown
            work = Path(facts['cwd'])
            assert work.is_dir()
            deleted = await owner.wait(await owner.delete_results(ref))
            assert deleted.state == 'succeeded'
            assert not work.exists()
            assert not (state / rows[0]['root_relative'] / 'stdout.log').exists()
            receipts(state, rows)  # immutable control evidence survives result deletion
    asyncio.run(scenario())


async def wait_until(check, seconds=8):
    async with asyncio.timeout(seconds):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(.02)


async def get(port, path):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode())
        await writer.drain()
        return await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.usefixtures('ownership_mode')
def test_restarts_wait_for_receipts_and_exhaust_exact_budget(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, port = probe_bundle(tmp_path, mode='sleep:2', restarts=1)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            async def running(count):
                row = await owner._db('reflector_probe_status', ref.id)
                return row and row['state'] == 'running' and len(await scopes(owner, ref)) == count
            await wait_until(lambda: running(1))
            await get(port, '/exit')
            await wait_until(lambda: running(2))
            rows = await scopes(owner, ref)
            assert receipts(state, rows[:1])[0]['child_exit_code'] == 7
            await get(port, '/exit')
            async def exhausted():
                row = await owner._db('reflector_probe_status', ref.id)
                return row if row and row['state'] == 'exhausted' else None
            assert await wait_until(exhausted) == {
                'launched_by': 'lab', 'state': 'exhausted', 'restarts': 1, 'last_exit': 7}
            await asyncio.sleep(.15)
            assert len(await scopes(owner, ref)) == 2
            receipts(state, await scopes(owner, ref))
            await owner.stop(ref)
            await finished(owner, ref)
            worker = owner._operations[ref.id].paths.scope_directory / 'stderr.log'
            text = worker.read_text()
            assert text.count('reflector probe started at') == 2
            assert 'exited with code 7; restart 0' in text and 'exited with code 7; restart 1' in text
            assert 'exhausted max_restarts=1' in text
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('ending', ['stop', 'worker_exit'])
@pytest.mark.parametrize('boundary', ['before_scope', 'accepted', 'marked', 'ready'])
def test_campaign_end_during_probe_restart_finishes_normally(tmp_path, monkeypatch, ending, boundary):
    """Ending a run during restart must retain receipts and the real Core outcome."""
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.owner.reflector_probe import ReflectorProbe
    gate = tmp_path / 'turn-release'
    state, bundle, prepared, port = probe_bundle(tmp_path, mode=f'gate:{gate}',
                                               availability='continue', startup=30)
    original_db, original_open = Owner._db, ReflectorProbe.open_scope
    original_ready, original_finish = HelperHandle.wait_event, ReflectorProbe.finish

    async def scenario():
        entered, release, finish_entered, finish_release = (asyncio.Event() for _ in range(4))
        probe = None
        finishes = []

        async def hold():
            entered.set()
            await release.wait()

        async def held_db(self, method, *args, **kwargs):
            result = await original_db(self, method, *args, **kwargs)
            selected = {'accepted': 'accept_reflector_probe_scope', 'marked': 'mark_helper_possible'}
            if (probe is not None and not entered.is_set()
                    and method == selected.get(boundary)):
                await hold()
            return result

        async def held_open(self, *args, **kwargs):
            if self is probe and boundary == 'before_scope' and not entered.is_set():
                await hold()
            return await original_open(self, *args, **kwargs)

        async def held_ready(self, kind, *args, **kwargs):
            result = await original_ready(self, kind, *args, **kwargs)
            if (probe is not None and self is probe.helper and probe.restarts == 1
                    and kind == 'ready' and boundary == 'ready' and not entered.is_set()):
                await hold()
            return result

        async def held_finish(self):
            finishes.append(self)
            finish_entered.set()
            await finish_release.wait()  # Let the watcher observe the end before finish cancels it.
            return await original_finish(self)

        monkeypatch.setattr(Owner, '_db', held_db)
        monkeypatch.setattr(ReflectorProbe, 'open_scope', held_open)
        monkeypatch.setattr(HelperHandle, 'wait_event', held_ready)
        monkeypatch.setattr(ReflectorProbe, 'finish', held_finish)
        async with Owner.open(state) as owner:
            try:
                _, target, _ = await ready_target(owner, bundle)
                boot = (await owner.target(target)).boot_id
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                runtime = owner._operations[ref.id]
                async def turn_started():
                    return gate.with_suffix('.entered').exists()
                await wait_until(turn_started)
                probe = runtime.reflector_probe
                await get(port, '/exit')
                await asyncio.wait_for(entered.wait(), 8)
                prior = receipts(state, (await scopes(owner, ref))[:1])
                assert prior[0]['child_exit_code'] == 7
                if ending == 'stop':
                    assert (await owner.stop(ref)).effect == 'requested'
                gate.touch()  # Complete the real target exchange, letting Core exit cooperatively.
                await asyncio.wait_for(finish_entered.wait(), 12)
                assert runtime.helper.process.poll() is not None
                release.set()
                outcome, = await asyncio.wait_for(asyncio.gather(probe.watcher, return_exceptions=True), 8)
                finish_release.set()
                await asyncio.wait_for(asyncio.shield(runtime.done), 8)
                value = await owner.status(ref)
                assert value.failure is None, value.failure
                assert value.control_state == 'finished', value.to_payload()
                assert value.refusal is None
                assert value.process.exit_code == (3 if ending == 'stop' else 0)
                assert value.process.cleanup_complete is True and value.target_cleanup.complete is True
                assert value.result.core is not None and value.result.persistence.state == 'saved'
                assert outcome is None and len(finishes) == 1
                rows = await scopes(owner, ref)
                assert len(rows) == (1 if boundary == 'before_scope' else 2)
                assert all(row['state'] == 'released' and row['cleanup_complete'] == 1 for row in rows)
                evidence = receipts(state, rows)
                assert evidence[0] == prior[0]  # The old scope's immutable evidence survives restart.
                if len(evidence) == 2:
                    assert evidence[1]['child_launched'] is False and evidence[1]['forced'] is False
                record = await owner._db('reflector_probe_status', ref.id)
                assert record['state'] == 'stopped'
                assert record['restarts'] == len(rows) - 1
                assert set(await owner._db('reservations')) == {'target_lifetime'}
                assert (await owner.target(target)).state == 'ready'
                assert (await owner.target(target)).boot_id == boot
            finally:
                gate.touch()
                release.set()
                finish_release.set()
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_committed_stop_before_probe_restart_admission_finishes_normally(tmp_path, monkeypatch):
    """DB stop commits before its await returns and sets the runtime's stop event."""
    from evolution_lab.owner import Owner, ResourceRef
    gate = tmp_path / 'turn-release'
    state, bundle, prepared, port = probe_bundle(tmp_path, mode=f'gate:{gate}', availability='continue')
    original_db = Owner._db

    async def scenario():
        accepting, accept_release, stopped, stop_release = (asyncio.Event() for _ in range(4))
        probe = None

        async def held_db(self, method, *args, **kwargs):
            if method == 'accept_reflector_probe_scope' and probe is not None:
                accepting.set()  # open_scope has already checked the in-memory flags.
                await accept_release.wait()
            result = await original_db(self, method, *args, **kwargs)
            if method == 'request_operation_stop':
                stopped.set()  # The real store transaction committed; runtime.stop is still false.
                await stop_release.wait()
            return result

        monkeypatch.setattr(Owner, '_db', held_db)
        async with Owner.open(state) as owner:
            stop = None
            try:
                _, target, _ = await ready_target(owner, bundle)
                boot = (await owner.target(target)).boot_id
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                runtime = owner._operations[ref.id]
                async def turn_started():
                    return gate.with_suffix('.entered').exists()
                await wait_until(turn_started)
                probe = runtime.reflector_probe
                await get(port, '/exit')
                await asyncio.wait_for(accepting.wait(), 8)
                stop = asyncio.create_task(owner.stop(ref))
                await asyncio.wait_for(stopped.wait(), 8)
                assert (await owner._db('operation_status', ref.id)).control_state == 'stopping'
                assert not runtime.stop.is_set() and not probe.interrupted()
                prior = receipts(state, await scopes(owner, ref))
                assert len(prior) == 1 and prior[0]['child_exit_code'] == 7
                accept_release.set()
                outcome, = await asyncio.wait_for(asyncio.gather(probe.watcher, return_exceptions=True), 8)
                assert not runtime.stop.is_set()
                stop_release.set()
                assert (await stop).effect == 'requested'
                gate.touch()
                await asyncio.wait_for(asyncio.shield(runtime.done), 20)
                value = await owner.status(ref)
                assert value.failure is None, value.failure
                assert outcome is None and value.control_state == 'finished'
                assert value.process.exit_code == 3 and value.process.cleanup_complete is True
                assert value.target_cleanup.complete is True and value.result.core is not None
                assert value.result.persistence.state == 'saved'
                rows = await scopes(owner, ref)
                assert len(rows) == 1 and rows[0]['state'] == 'released'
                assert receipts(state, rows) == prior
                assert (await owner._db('reflector_probe_status', ref.id)) == {
                    'launched_by': 'lab', 'state': 'stopped', 'restarts': 0, 'last_exit': 7}
                assert set(await owner._db('reservations')) == {'target_lifetime'}
                assert (await owner.target(target)).state == 'ready'
                assert (await owner.target(target)).boot_id == boot
            finally:
                accept_release.set()
                stop_release.set()
                gate.touch()
                if stop is not None:
                    await stop
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('failure', ['receipt', 'database'])
def test_probe_finish_failure_is_not_retried_and_requires_cleanup(tmp_path, monkeypatch, failure):
    from evolution_lab.owner import Owner, ResourceRef, reflector_probe
    state, bundle, prepared, _ = probe_bundle(tmp_path)
    original_db = Owner._db
    original_receipt = reflector_probe.read_cleanup_receipt
    original_finish = reflector_probe.ReflectorProbe.finish
    probe, finishes, failures = None, [], []

    def failed_receipt(*args, **kwargs):
        receipt = original_receipt(*args, **kwargs)
        if failure == 'receipt' and probe is not None and probe.ending and not failures:
            failures.append(failure)
            raise OSError('injected probe receipt read failure')
        return receipt

    async def failed_db(self, method, *args, **kwargs):
        if (failure == 'database' and probe is not None and probe.ending and not failures
                and method == 'confirm_scope_cleanup' and args[0] == probe.scope_id):
            failures.append(failure)
            raise OSError('injected probe cleanup confirmation failure')
        return await original_db(self, method, *args, **kwargs)

    async def tracked_finish(self):
        nonlocal probe
        probe = self
        finishes.append(self)
        return await original_finish(self)

    monkeypatch.setattr(reflector_probe, 'read_cleanup_receipt', failed_receipt)
    monkeypatch.setattr(Owner, '_db', failed_db)
    monkeypatch.setattr(reflector_probe.ReflectorProbe, 'finish', tracked_finish)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            await asyncio.wait_for(asyncio.shield(owner._operations[ref.id].done), 20)
            value = await owner.status(ref)
            assert value.control_state == 'cleanup_required' and value.failure.code == 'io_error'
            assert value.target_cleanup.complete is not True
            assert 'execution' in await owner._db('reservations')
            assert failures == [failure]
            assert len(finishes) == 1
            rows = await scopes(owner, ref)
            assert len(rows) == 1 and rows[0]['cleanup_complete'] is None
            evidence = receipts(state, rows)  # The real receipt survives the failed read/write.
            assert evidence[0]['process_tree_empty'] is True
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
            assert receipts(state, rows) == evidence
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_operator_probe_gets_one_health_and_is_never_stopped(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from evolution_lab.owner import Owner, ResourceRef
    paths = []
    from evolution_lab.owner import reflector_probe
    original = reflector_probe.healthy
    owner_requests = []
    async def tracked(probe):
        owner_requests.append(probe.health_path)
        return await original(probe)
    monkeypatch.setattr(reflector_probe, 'healthy', tracked)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(204)
            self.end_headers()
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        state, bundle, prepared, port = probe_bundle(tmp_path, owned=False, port=server.server_port)
        async def scenario():
            async with Owner.open(state) as owner:
                _, target, _ = await ready_target(owner, bundle)
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                value = await finished(owner, ref)
                assert value.control_state == 'finished'
                assert owner_requests == ['/neutral-health']
                assert all(path == '/neutral-health' for path in paths)  # Core checks independently
                assert not await scopes(owner, ref)
                await owner.close()
                assert (await get(port, '/still-here')).startswith(b'HTTP/1.0 204')
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('reuse_address', [False, True])
@pytest.mark.parametrize('availability', [None, 'continue'])
def test_busy_port_refuses_without_touching_foreign_listener(tmp_path, reuse_address, availability):
    from evolution_lab.owner import Owner, ResourceRef
    with socket.socket() as listener:
        if reuse_address:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        state, bundle, prepared, _ = probe_bundle(tmp_path, port=listener.getsockname()[1], availability=availability)
        async def scenario():
            async with Owner.open(state) as owner:
                _, target, _ = await ready_target(owner, bundle)
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                value = await finished(owner, ref)
                assert value.failure.code == 'reflector_probe_port_busy'
                assert value.control_state == 'released' and value.process.launched is False
                assert not await scopes(owner, ref)
                with socket.create_connection(listener.getsockname(), timeout=1):
                    pass
                assert (await owner.target(target)).state == 'ready'
        asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_no_declaration_performs_no_probe_get(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef, reflector_probe
    state, bundle, prepared, _ = probe_bundle(tmp_path, owned=False, declared=False)
    async def forbidden(*args):
        pytest.fail('probe-less campaigns must not make a health request')
    monkeypatch.setattr(reflector_probe, 'healthy', forbidden)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            assert (await finished(owner, ref)).control_state == 'finished'
            assert not await scopes(owner, ref)
            assert await owner._db('reflector_probe_status', ref.id) == {
                'launched_by': None, 'state': 'not_declared', 'restarts': 0, 'last_exit': None}
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('ending', ['stop', 'close'])
def test_stop_during_slow_probe_start_does_not_wait_startup_deadline(tmp_path, ending):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, _ = probe_bundle(tmp_path, delay=20, startup=20)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            async def spawned():
                runtime = owner._operations[ref.id]
                probe = runtime.reflector_probe
                return probe and probe.helper and any(e['kind'] == 'child_started' for e in probe.helper.events)
            await wait_until(spawned)
            rows = await scopes(owner, ref)
            async with asyncio.timeout(3):
                if ending == 'close':
                    await owner.close()
                else:
                    await owner.stop(ref)
                    await asyncio.shield(owner._operations[ref.id].done)
            assert owner._operations[ref.id].helper is None
            receipts(state, rows)
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_owner_death_recovers_probe_through_scopes(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, _ = probe_bundle(tmp_path, mode='sleep:2')
    script = '''import asyncio,json,sys
from evolution_lab.owner import Owner,TargetRef
async def main():
 async with Owner.open(sys.argv[1]) as owner:
  task=await owner.start_target(await owner.bind(sys.argv[2]),secrets={'TARGET_TOKEN':'private-neutral-token'})
  target=TargetRef((await owner.wait(task)).ref.id,owner.owner_generation)
  op=await owner.start(sys.argv[3],target_ref=target,secrets={})
  runtime=owner._operations[op.id]
  while runtime.helper is None: await asyncio.sleep(.01)
  await runtime.helper.wait_event('child_started')
  print(json.dumps({'id':op.id}),flush=True)
  await asyncio.Event().wait()
asyncio.run(main())
'''
    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', script,
            str(state), bundle.digest, prepared.digest, stdout=asyncio.subprocess.PIPE)
        try:
            row = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
        finally:
            if process.returncode is None:
                process.kill()  # only the test-owned Owner process; helpers must clean their own trees
            await process.wait()
        async with Owner.open(state) as owner:
            ref = ResourceRef('operation', row['id'])
            assert (await owner.status(ref)).control_state == 'orphaned'
            assert await owner._db('reflector_probe_status', ref.id) is None
            value = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert value.state == 'succeeded', value.to_payload()
            rows = await scopes(owner, ref)
            assert len(rows) == 1 and all(s['state'] == 'released' for s in rows)
            receipts(state, rows)
            assert (await owner._db('reflector_probe_status', ref.id))['state'] == 'stopped'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_worker_log_keeps_owner_notes_between_child_writes(tmp_path):
    from test_owner_scopes import scope, finish
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.owner.reflector_probe import append_notes
    from evolution_lab.owner.receipts import read_cleanup_receipt
    async def scenario():
        directory, sid, generation = scope(tmp_path)
        child = "import sys,time,pathlib; print('first', file=sys.stderr,flush=True); "
        child += "\nwhile not pathlib.Path('continue').exists(): time.sleep(.01)\n"
        child += "print('last',file=sys.stderr,flush=True)"
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            await handle.configure(argv=[sys.executable, '-I', '-c', child], env={},
                                   stop_grace_seconds=.2, cwd=str(directory))
            await handle.wait_event('ready', 5)
            await handle.go()
            async def first():
                log = directory / 'stderr.log'
                return log.exists() and log.read_text() == 'first\n'
            await wait_until(first)
            append_notes(directory, ['reflector probe started at http://127.0.0.1:1234'])
            (directory / 'continue').touch()
            assert await handle.wait(5) == 0
            assert (directory / 'stderr.log').read_text() == (
                'first\nevo tree evolve: note: reflector probe started at http://127.0.0.1:1234\nlast\n')
            assert read_cleanup_receipt(directory, sid, generation)['process_tree_empty']
        finally:
            await finish(handle)
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_forced_probe_receipt_preserves_cleanup_uncertainty(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, _ = probe_bundle(tmp_path, restarts=0)
    original = Owner._db
    observed = []
    async def inspect_before_persistence(self, method, *args, **kwargs):
        if method == 'update_operation':
            observed.append(await self.status(ResourceRef('operation', args[0])))
        return await original(self, method, *args, **kwargs)
    monkeypatch.setattr(Owner, '_db', inspect_before_persistence)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            async def worker_started():
                runtime = owner._operations[ref.id]
                return runtime.helper and any(e['kind'] == 'child_started' for e in runtime.helper.events)
            await wait_until(worker_started)
            probe = owner._operations[ref.id].reflector_probe
            await probe.helper.force()
            await asyncio.shield(owner._operations[ref.id].done)
            value = await owner.status(ref)
            assert value.control_state == 'cleanup_required', value.to_payload()
            assert value.target_cleanup.complete is not True
            assert 'execution' in await owner._db('reservations')
            assert receipts(state, await scopes(owner, ref))[0]['forced'] is True
            assert observed
            assert all(v.control_state not in {'finished', 'cleanup_required', 'released'}
                       or v.result is None or v.result.persistence.state != 'not_attempted' for v in observed)
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('availability', ['continue', 'suspend'])
def test_forced_probe_cleanup_stops_restarts_and_preserves_core_finalization(tmp_path, monkeypatch, availability):
    """An own forced receipt must not try admission and skip the worker's finalizer."""
    from evolution_lab.owner import Owner, ResourceRef, cleanup
    gate = tmp_path / 'turn-release'
    state, bundle, prepared, _ = probe_bundle(tmp_path, mode=f'gate:{gate}',
        restarts=3, availability=availability)
    reads = []
    original_refusal, original_terminal = cleanup.read_campaign_refusal, cleanup.read_campaign_core_terminal

    def read_refusal(path, **kwargs):
        value = original_refusal(path, **kwargs)
        reads.append(('refusal', kwargs, value))
        return value

    def read_terminal(path, **kwargs):
        value = original_terminal(path, **kwargs)
        reads.append(('terminal', kwargs, value))
        return value

    monkeypatch.setattr(cleanup, 'read_campaign_refusal', read_refusal)
    monkeypatch.setattr(cleanup, 'read_campaign_core_terminal', read_terminal)

    async def scenario():
        runtime = None
        confirmations = []
        original_db = Owner._db

        async def observed_db(self, method, *args, **kwargs):
            result = await original_db(self, method, *args, **kwargs)
            if method == 'confirm_scope_cleanup' and runtime is not None and args[0] == runtime.scope_id:
                rows = await original_db(self, 'resource_scopes', ResourceRef('operation', runtime.id))
                confirmations.append((result, next(row for row in rows if row['id'] == runtime.scope_id)))
            return result

        monkeypatch.setattr(Owner, '_db', observed_db)
        async with Owner.open(state) as owner:
            try:
                _, target, _ = await ready_target(owner, bundle)
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                runtime = owner._operations[ref.id]
                async def turn_started():
                    return gate.with_suffix('.entered').exists()
                await wait_until(turn_started)
                probe = runtime.reflector_probe
                await probe.helper.force()  # Real helper command, child exit and immutable forced receipt.
                outcome, = await asyncio.wait_for(asyncio.gather(probe.watcher, return_exceptions=True), 8)
                assert runtime.helper.process.poll() is None and not runtime.stop.is_set()
                prior_rows = await scopes(owner, ref)
                assert len(prior_rows) == 1 and prior_rows[0]['cleanup_complete'] == 1
                evidence = receipts(state, prior_rows)
                assert evidence[0]['forced'] is True
                assert (await owner._db('operation_status', ref.id)).control_state == 'cleanup_required'
                gate.touch()
                await asyncio.wait_for(asyncio.shield(runtime.done), 20)
                value = await owner.status(ref)
                assert value.failure is None, value.failure
                assert outcome is None
                assert value.control_state == 'cleanup_required' and value.target_cleanup.complete is not True
                assert value.result.core is not None and value.result.persistence.state == 'saved'
                worker = json.loads((runtime.paths.scope_directory / 'stdout.log').read_text())
                assert worker['terminal'] == ('suspended' if availability == 'suspend' else 'finished')
                assert value.result.core['suspended'] is (availability == 'suspend')
                if availability == 'suspend':
                    assert worker['owner'] == 'campaign' and worker['code'] == 'probe_unavailable'
                identity = {'operation_id': ref.id, 'scope_id': runtime.scope_id}
                assert reads == [('refusal', identity, None), ('terminal', identity, None)]
                assert value.refusal is None and value.core_terminal is None
                rows = await owner._db('resource_scopes', ref)
                worker_row, = [row for row in rows if row['id'] == runtime.scope_id]
                confirmed, = confirmations
                assert confirmed[0] is True and confirmed[1]['cleanup_complete'] == 1
                assert confirmed[1]['cleanup_state'] == 'confirmed' and confirmed[1]['forced'] == 0
                # Aggregate cleanup demotes the current scope after confirming its
                # receipt, because the probe's forced cleanup still needs attention.
                assert worker_row['state'] == 'cleanup_required' and worker_row['cleanup_state'] == 'pending'
                assert worker_row['cleanup_complete'] is None and worker_row['forced'] == 0
                assert value.process.cleanup_complete is True
                assert receipts(state, [worker_row])[0]['forced'] is False
                assert await scopes(owner, ref) == prior_rows
                assert await owner._db('reflector_probe_status', ref.id) == {
                    'launched_by': 'lab', 'state': 'stopped', 'restarts': 0,
                    'last_exit': evidence[0]['child_exit_code']}
                notes = (runtime.paths.scope_directory / 'stderr.log').read_text()
                assert 'reflector probe cleanup was forced; no further restarts for this run' in notes
                assert 'exhausted max_restarts=' not in notes
                assert 'execution' in await owner._db('reservations')
                assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
                assert receipts(state, prior_rows) == evidence
                assert not (await owner.health()).blockers
            finally:
                gate.touch()
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_worker_spawn_failure_stops_probe_and_keeps_conservative_reservation(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle
    state, bundle, prepared, _ = probe_bundle(tmp_path)
    original = HelperHandle.configure
    async def fail_worker(self, **kwargs):
        if kwargs.get('role') == 'campaign':
            raise OSError('injected campaign configuration failure')
        return await original(self, **kwargs)
    monkeypatch.setattr(HelperHandle, 'configure', fail_worker)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            await asyncio.shield(owner._operations[ref.id].done)
            value = await owner.status(ref)
            assert value.control_state == 'cleanup_required'
            assert len(await scopes(owner, ref)) == 1
            receipts(state, await scopes(owner, ref))
            assert 'execution' in await owner._db('reservations')
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_resume_launches_new_probe_with_fresh_restart_budget(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    from test_owner_resume import _replace_target
    flag = tmp_path / 'broken'
    flag.touch()
    state, bundle, prepared, port = probe_bundle(tmp_path, mode=f'fail_after:1:{flag}', restarts=1,
                                                scaffold=scaffold_15_with_legacy_failures)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            async def running():
                value = await owner._db('reflector_probe_status', ref.id)
                return value and value['state'] == 'running'
            await wait_until(running)
            await get(port, '/exit')
            one = await finished(owner, ref)
            assert one.result.core['suspended'] is True
            prior_scopes = await scopes(owner, ref)
            assert len(prior_scopes) == 2
            assert (await owner._db('reflector_probe_status', ref.id))['restarts'] == 1
            receipts(state, prior_scopes)
            target = await _replace_target(owner, bundle, target)
            flag.unlink()
            await owner.resume(ref, target_ref=target, secrets={})
            two = await finished(owner, ref)
            assert two.control_state == 'finished' and len(two.starts) == 2, two.to_payload()
            current_scopes = await scopes(owner, ref)
            assert len(current_scopes) == 1
            assert not {s['id'] for s in prior_scopes} & {s['id'] for s in current_scopes}
            assert (await owner._db('reflector_probe_status', ref.id))['restarts'] == 0
            assert (await owner._db('reflector_probe_status', ref.id, start=1))['restarts'] == 1
            receipts(state, current_scopes)
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_resume_refuses_missing_operator_probe_without_worker_or_new_reservations(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from evolution_lab.owner import Owner, ResourceRef
    from test_owner_resume import _replace_target
    flag = tmp_path / 'broken'
    flag.touch()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        state, bundle, prepared, _ = probe_bundle(tmp_path, owned=False, port=server.server_port,
                                                 mode=f'fail_after:1:{flag}', scaffold=scaffold_15_with_legacy_failures)
        async def scenario():
            async with Owner.open(state) as owner:
                _, target, _ = await ready_target(owner, bundle)
                op = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', op.id)
                assert (await finished(owner, ref)).result.core['suspended'] is True
                target = await _replace_target(owner, bundle, target)
                server.shutdown()
                server.server_close()
                thread.join()
                flag.unlink()
                await owner.resume(ref, target_ref=target, secrets={})
                value = await finished(owner, ref)
                assert value.failure.code == 'reflector_probe_unavailable'
                assert value.control_state == 'released' and value.process.launched is False
                assert owner._operations[ref.id].helper is None
                assert set(await owner._db('reservations')) == {'target_lifetime'}
                assert (await owner.target(target)).state == 'ready'
                assert not await scopes(owner, ref)
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_health_reset_is_unavailable_and_non_2xx_is_not_ready():
    from types import SimpleNamespace
    from evolution_lab.owner.reflector_probe import healthy
    async def scenario():
        async def reset(reader, writer):
            await reader.read(1)
            writer.transport.abort()
        async def refused(reader, writer):
            await reader.read(8192)
            writer.write(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async def incomplete(reader, writer):
            await reader.read(8192)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        for callback in (reset, refused, incomplete):
            server = await asyncio.start_server(callback, '127.0.0.1', 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                probe = SimpleNamespace(base_url=f'http://127.0.0.1:{port}',
                                        health_path='/neutral-health', request_timeout_seconds=.2)
                assert await healthy(probe) is False
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('boundary', ['accept_reflector_probe_scope', 'mark_helper_possible'])
def test_stop_during_probe_scope_commit_keeps_finalizer_ownership(tmp_path, monkeypatch, boundary):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared, _ = probe_bundle(tmp_path)
    original = Owner._db
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(self, method, *args, **kwargs):
            result = await original(self, method, *args, **kwargs)
            probe_runtime = next((r.reflector_probe for r in self._operations.values()
                                  if r.reflector_probe is not None), None)
            if method == boundary and probe_runtime is not None and not entered.is_set():
                entered.set()
                await release.wait()
            return result
        monkeypatch.setattr(Owner, '_db', held)
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            await asyncio.wait_for(entered.wait(), 8)
            await owner.stop(ref)
            await asyncio.sleep(.02)
            release.set()
            await asyncio.wait_for(asyncio.shield(owner._operations[ref.id].done), 8)
            value = await owner.status(ref)
            assert value.control_state == 'released', value.to_payload()
            rows = await scopes(owner, ref)
            assert len(rows) == 1 and rows[0]['state'] == 'released'
            assert rows[0]['cleanup_complete'] == 1
            receipts(state, [s for s in rows if s['helper_possible']])
            assert set(await owner._db('reservations')) == {'target_lifetime'}
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('available', [False, True])
def test_note_io_failure_never_changes_probe_refusal_or_core_outcome(tmp_path, monkeypatch, available):
    from evolution_lab.owner import Owner, ResourceRef, reflector_probe
    state, bundle, prepared, _ = probe_bundle(tmp_path, delay=0 if available else 2,
                                             startup=3 if available else .3)
    writes = []
    def failed_log(*args, **kwargs):
        writes.append(None)
        raise OSError('injected operator log failure')
    monkeypatch.setattr(reflector_probe, 'append_notes', failed_log)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            op = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', op.id)
            await asyncio.shield(owner._operations[ref.id].done)
            value = await owner.status(ref)
            assert writes
            if available:
                assert value.control_state == 'finished' and value.process.exit_code == 0
                assert value.failure is None and value.result.core is not None
            else:
                assert value.control_state == 'released'
                assert value.failure.code == 'reflector_probe_unavailable'
                assert value.process.launched is False
            receipts(state, await scopes(owner, ref))
    asyncio.run(scenario())
