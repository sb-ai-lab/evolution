"""V-S5-2: idle Owner, bounded public-call doubles, private UDS lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

from evolution_lab.owner import Connection, Owner, OwnerError, TaskRef
from evolution_lab.service.errors import ServiceError
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from evolution_lab.service.server import PrivateListener, serve


@pytest.mark.parametrize('kind', ['relative', 'inside_state', 'contains_state', 'symlink', 'public', 'clones', 'workspaces'])
def test_service_scratch_rejects_unsafe_paths(tmp_path, kind):
    cfg = config(tmp_path)
    scratch = tmp_path / 'scratch'
    if kind == 'relative':
        scratch = Path('relative')
    elif kind == 'inside_state':
        scratch = cfg.state_directory / 'scratch'
    elif kind == 'contains_state':
        scratch = tmp_path
    elif kind == 'symlink':
        actual = tmp_path / 'actual'
        actual.mkdir(mode=0o700)
        (tmp_path / 'alias').symlink_to(actual, target_is_directory=True)
        scratch = tmp_path / 'alias'
    elif kind == 'public':
        scratch.mkdir(mode=0o750)
    else:
        scratch = cfg.home_directory / kind / 'scratch'
    with pytest.raises(ServiceError) as caught:
        ServiceConfig(cfg.socket_path, cfg.state_directory, home_directory=cfg.home_directory,
            scratch_directory=scratch).validate_paths()
    assert caught.value.code == 'invalid_argument'


def test_serve_cli_scratch_reaches_service(monkeypatch, tmp_path):
    from evolution_lab.cli import main
    seen = []
    async def inspect_config(cfg, *, stderr, on_ready=None):
        seen.append(cfg)
        return 0
    monkeypatch.setattr('evolution_lab.service.server.serve', inspect_config)
    assert main(['serve', '--state', str(tmp_path / 'state'), '--home', str(tmp_path / 'home'),
        '--scratch', str(tmp_path / 'scratch')]) == 0
    assert seen[0].scratch_directory == tmp_path / 'scratch'


@pytest.mark.asyncio
@pytest.mark.parametrize('port_leftover', [False, True])
async def test_service_scratch_creates_private_pair_once_and_logs(tmp_path, caplog, port_leftover, monkeypatch):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    from dataclasses import replace
    from hashlib import sha256
    cfg = config(tmp_path)
    scratch = tmp_path / 'scratch'
    if port_leftover:
        from evolution_lab.projects.port import mark_leftover
        cfg.home_directory.mkdir(mode=0o700)
        (cfg.home_directory / 'tmp').mkdir(mode=0o700)
        scratch = cfg.home_directory / 'tmp' / 'port-scratch'
        scratch.mkdir(mode=0o700)
        mark_leftover(scratch)
        (scratch / 'preserved').write_text('existing scratch')
    cfg = replace(cfg, scratch_directory=scratch)
    pair = scratch / sha256(str(cfg.state_directory).encode()).hexdigest()[:16]
    runtime = ServiceRuntime(cfg)
    with caplog.at_level(logging.INFO, logger='evolution_lab.service.runtime'):
        try:
            assert await runtime.start()
            assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
            assert stat.S_IMODE(pair.stat().st_mode) == 0o700
            assert runtime.owner.scratch_directory == pair
            if port_leftover:
                assert (scratch / 'preserved').read_text() == 'existing scratch'
            assert json.loads((cfg.state_directory / 'scratch.json').read_text()) == {
                'schema': 'lab-owner-scratch/1', 'path': str(pair)}
        finally:
            await runtime.shutdown()
    messages = [r.getMessage() for r in caplog.records if r.name == 'evolution_lab.service.runtime']
    assert messages.count(f'durability=relaxed scratch={scratch}') == 1


@pytest.mark.asyncio
async def test_serve_scratch_rebind_refusal_names_old_root_in_stderr(tmp_path):
    from dataclasses import replace
    from evolution_lab.owner.store import OwnerStateStore
    cfg = config(tmp_path)
    old = tmp_path / 'old-pair'
    with OwnerStateStore.open(cfg.state_directory, scratch_directory=old):
        pass
    (old / 'targets' / 'owned').mkdir(parents=True)
    stderr = io.StringIO()
    assert await serve(replace(cfg, scratch_directory=tmp_path / 'new'), stderr=stderr) == 1
    rows = [json.loads(line) for line in stderr.getvalue().splitlines()]
    assert rows[0]['error']['code'] == 'invalid_argument'
    assert set(rows[0]['error']) == {'code', 'stage', 'message'}
    assert str(old) in rows[0]['error']['message']
    assert not cfg.socket_path.exists()


@pytest.mark.asyncio
async def test_serve_scratch_cleanup_warning_keeps_stopped_success_and_json_diagnostics(tmp_path):
    from dataclasses import replace
    from evolution_lab.owner import ResourceRef
    from evolution_lab.owner.scratch import scratch_pair_directory
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_state import _ready_target, _target_directories
    from evolution_lab.service.app import create_app

    cfg = config(tmp_path)
    scratch = tmp_path / 'scratch'
    pair = scratch_pair_directory(scratch, cfg.state_directory)
    with OwnerStateStore.open(cfg.state_directory, scratch_directory=pair) as store:
        ref = _ready_target(store)
        _target_directories(pair)
        scopes = store.resource_scopes(ResourceRef('target', ref.id))
        for scope in scopes:
            store.confirm_scope_cleanup(scope['id'], scope['owner_generation'], forced=False)
        (pair / 'targets').chmod(0o500)
        store.release_evidenced_reservations(ResourceRef('target', ref.id),
            scope_ids=tuple(scope['id'] for scope in scopes), disposition='confirmed_cleanup')
        status = store.target_status(ref.id)
        assert status.state == 'stopped' and status.cleanup.complete is True and status.failure is None

    def app_factory(runtime):
        runtime.request_shutdown()
        return create_app(runtime)

    stderr = io.StringIO()
    try:
        assert await serve(replace(cfg, scratch_directory=scratch), app_factory=app_factory, stderr=stderr) == 0
        rows = [json.loads(line) for line in stderr.getvalue().splitlines()]
        warnings = [row for row in rows if 'error' in row]
        assert len(warnings) == 1
        assert set(warnings[0]['error']) == {'code', 'stage', 'message'}
        assert 'Scratch' in warnings[0]['error']['message']
        assert rows[-1]['cleanup'] == {'state': 'confirmed', 'complete': True}
    finally:
        (pair / 'targets').chmod(0o700)


@pytest.mark.parametrize('durability', ['fast', [], 1])
def test_service_config_rejects_invalid_durability(tmp_path, durability):
    with pytest.raises(ServiceError) as caught:
        ServiceConfig(tmp_path / 'service.sock', tmp_path / 'state', durability=durability)
    assert caught.value.code == 'invalid_argument'


def test_serve_cli_durability_parses_and_rejects_unknown():
    from evolution_lab.cli import main
    from evolution_lab.cli.errors import UsageError
    from evolution_lab.cli.parser import build_parser
    parser = build_parser()
    assert parser.parse_args(['serve', '--state', '/tmp/state']).durability is None
    assert parser.parse_args(['serve', '--state', '/tmp/state', '--durability', 'relaxed']).durability == 'relaxed'
    with pytest.raises(UsageError, match='invalid choice'):
        parser.parse_args(['serve', '--state', '/tmp/state', '--durability', 'fast'])
    assert main(['serve', '--state', '/tmp/state', '--durability', 'fast']) == 2


def test_serve_cli_passes_relaxed_durability_to_service(monkeypatch):
    from evolution_lab.cli import main
    seen = []

    async def inspect_config(config, *, stderr, on_ready=None):
        seen.append(config)
        return 0

    monkeypatch.setattr('evolution_lab.service.server.serve', inspect_config)
    assert main(['serve', '--state', '/tmp/state', '--home', '/tmp/home', '--durability', 'relaxed']) == 0
    assert seen[0].durability == 'relaxed'


@pytest.mark.asyncio
@pytest.mark.parametrize('durability', [None, 'relaxed'])
async def test_service_durability_reaches_all_stores_and_logs_once(tmp_path, caplog, durability, monkeypatch):
    monkeypatch.delenv('EVOLUTION_DURABILITY', raising=False)
    from dataclasses import replace
    cfg = config(tmp_path)
    if durability is not None:
        cfg = replace(cfg, durability=durability)
    runtime = ServiceRuntime(cfg)
    with caplog.at_level(logging.INFO, logger='evolution_lab.service.runtime'):
        try:
            assert await runtime.start()
            journal, synchronous = ('wal', 1)
            assert runtime.projects.store._db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert runtime.projects.store._db.execute('PRAGMA synchronous').fetchone()[0] == synchronous
            def owner_pragmas():
                db = runtime.owner._store._database
                return (db.execute('PRAGMA journal_mode').fetchone()[0],
                        db.execute('PRAGMA synchronous').fetchone()[0])
            assert await asyncio.get_running_loop().run_in_executor(runtime.owner._executor, owner_pragmas) == (journal, synchronous)
            db = runtime.project_session._probes()._db
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == journal
            assert db.execute('PRAGMA synchronous').fetchone()[0] == synchronous
        finally:
            await runtime.shutdown()
    messages = [r.getMessage() for r in caplog.records if r.name == 'evolution_lab.service.runtime']
    assert messages.count(f'durability={durability or "relaxed"} scratch=none') == 1


@pytest.mark.asyncio
async def test_http_facade_maps_only_explicit_public_calls_and_returns_safe_outputs():
    """The V1 facade neither discovers resources nor decorates public projections."""
    import httpx
    from evolution_lab.service.app import create_app

    digest = 'a' * 64

    class Public:
        def __init__(self, kind):
            self.kind = kind

        def to_payload(self):
            return {'schema': 'test-public/1', 'kind': self.kind}

    class Runtime:
        service_environment = None

        def __init__(self):
            self.owner_calls = []
            self.preparation_calls = []

        async def call_owner(self, method, *args, **kwargs):
            self.owner_calls.append((method, args, kwargs))
            if method == 'read_export':
                return b'zip-bytes'
            if method == 'list_operations':
                return (Public('operation-a'), Public('operation-b'))
            return Public(method)

        async def call_preparation(self, method, *args, **kwargs):
            self.preparation_calls.append((method, args, kwargs))
            return Public(method)

        def mutation_key(self, headers):
            return None

        async def call_mutation(self, key, route, parts, target, method, /, *args, **kwargs):
            call = self.owner_calls if target == 'owner' else self.preparation_calls
            call.append((method, args, kwargs))
            return Public(method)

        def connection(self, identifier):
            assert identifier == 'connection'
            return Public('connection')

    runtime = Runtime()
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url='http://lab') as client:
        async def request(method, path, *, body=None, expected=200):
            headers = {} if body is None else {'content-type': 'application/json'}
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == expected
            assert response.headers['cache-control'] == 'no-store'
            return response

        bundle = {'schema': 'lab-service-bundle-request/1', 'source': {
            'kind': 'evaluation', 'input_path': '/tmp/input.json', 'launch_path': '/tmp/launch.json',
        }, 'environments': [], 'env': {}, 'secret_names': [], 'roots': [], 'path_bindings': []}
        prepare = {'schema': 'lab-service-prepare-request/2', 'kind': 'evaluation',
            'bundle_digest': digest, 'selection': {'all': True}, 'repeat_seeds': None,
            'evaluation_set_id': None, 'max_turns': None, 'timeout_seconds': None,
            'metric_plan': None, 'runtime_policy': None,
            'max_failed_turns': None, 'call_retries': None}
        target = {'schema': 'lab-target-ref/1', 'id': 'target', 'owner_generation': 'generation'}
        await request('POST', '/v1/bundles', body=bundle)
        await request('GET', f'/v1/bundles/{digest}')
        await request('POST', '/v1/prepared-executions', body=prepare)
        await request('POST', '/v1/prepared-executions',
                      body={**prepare, 'schema': 'lab-service-prepare-request/3', 'split': None})
        await request('GET', f'/v1/prepared-executions/{digest}')
        await request('POST', '/v1/connections/from-bundle', body={
            'schema': 'lab-service-bind-request/1', 'bundle_digest': digest,
        }, expected=201)
        await request('POST', '/v1/connections/connection/target', body={
            'schema': 'lab-service-target-start-request/1', 'secrets': {},
        }, expected=202)
        await request('GET', '/v1/targets/target?owner_generation=generation')
        await request('DELETE', '/v1/targets/target?owner_generation=generation', expected=202)
        await request('POST', '/v1/connections/connection/check', body={
            'schema': 'lab-service-check-request/1', 'target_ref': target,
        }, expected=202)
        await request('POST', '/v1/connections/connection/conformance', body={
            'schema': 'lab-service-conformance-request/1', 'target_ref': target, 'runtime_policy': None,
        }, expected=202)
        await request('GET', '/v1/tasks/task?owner_generation=generation')
        await request('POST', '/v1/tasks/task/stop', body={}, expected=200)
        await request('POST', '/v1/operations', body={
            'schema': 'lab-service-start-request/1', 'prepared_digest': digest,
            'target_ref': target, 'secrets': {},
        }, expected=202)
        listed = await request('GET', '/v1/operations')
        assert listed.json() == {'schema': 'lab-operation-list/8', 'operations': [
            {'schema': 'test-public/1', 'kind': 'operation-a'},
            {'schema': 'test-public/1', 'kind': 'operation-b'},
        ]}
        for suffix in ('', '/report', '/diff'):
            await request('GET', '/v1/operations/operation' + suffix)
        await request('POST', '/v1/operations/operation/stop', body={})
        await request('POST', '/v1/operations/operation/export', body={}, expected=202)
        archive = await request('GET', f'/v1/operations/operation/exports/{digest}/content')
        assert archive.content == b'zip-bytes'
        assert archive.headers['etag'] == f'"{digest}"'
        assert archive.headers['content-disposition'] == f'attachment; filename="{digest}.zip"'
        await request('DELETE', '/v1/operations/operation/results', expected=202)
        await request('POST', '/v1/cleanup', body={
            'schema': 'lab-service-cleanup-request/1', 'ref': {'kind': 'task', 'id': 'task'}, 'wait_seconds': None,
        }, expected=202)
        await request('POST', '/v1/releases', body={
            'schema': 'lab-service-release-request/1', 'ref': {'kind': 'target', 'id': 'target'}, 'reason': 'cleaned',
        })
        await request('GET', '/v1/health')
        invalid = await client.get('/v1/health?unexpected=true')
        assert invalid.status_code == 400
        assert invalid.json()['error']['code'] == 'invalid_request'
        media = await client.post('/v1/operations/operation/stop', content=b'{}')
        assert media.status_code == 415

    assert [name for name, _, _ in runtime.preparation_calls] == [
        'create_bundle', 'get_bundle', 'prepare_evaluation', 'prepare_evaluation', 'get_prepared',
    ]
    assert runtime.preparation_calls[2][2] == runtime.preparation_calls[3][2]
    assert runtime.preparation_calls[3][2]['split'] is None
    assert [name for name, _, _ in runtime.owner_calls] == [
        'bind', 'start_target', 'target', 'stop_target', 'check', 'conformance', 'task',
        'stop', 'start', 'list_operations', 'status', 'report', 'diff', 'stop', 'export',
        'read_export', 'delete_results', 'cleanup', 'release_after_manual_cleanup', 'health',
    ]
    assert runtime.owner_calls[1][1][0].kind == 'connection'
    assert runtime.owner_calls[1][2] == {'secrets': {}}
    assert runtime.owner_calls[2][1][0].to_payload() == target
    assert runtime.owner_calls[8][1][0] == digest
    assert runtime.owner_calls[8][2]['target_ref'].to_payload() == target
    assert runtime.owner_calls[15][1][1] == digest
    assert runtime.owner_calls[17][1][0].kind == 'task'
    assert runtime.owner_calls[18][2] == {'reason': 'cleaned'}


@pytest.mark.asyncio
async def test_measurement_route_accepts_epoch_id_only_in_the_measurement_position():
    import httpx
    from evolution_lab.service.app import create_app

    class Public:
        def to_payload(self):
            return {'schema': 'test-public/1'}

    class Runtime:
        def __init__(self):
            self.calls = []

        async def call_owner(self, method, *args, **kwargs):
            self.calls.append((method, args))
            return Public()

    runtime = Runtime()
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url='http://lab') as client:
        for path in ('/v1/operations/op1/measurements/m-r01-champion@e1',
                     '/v1/operations/op1/measurements/m-r01-champion%40e1'):
            assert (await client.get(path)).status_code == 200
        for path in ('/v1/operations/op1/measurements/m@e0', '/v1/operations/op1/measurements/m@e1@e2',
                     '/v1/operations/op1@e1/measurements/m1', '/v1/operations/op1%40e1/report'):
            response = await client.get(path)
            assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_request', path
    assert [(m, a[0].id, a[1]) for m, a in runtime.calls] == [('measurement', 'op1', 'm-r01-champion@e1')] * 2


@pytest.mark.asyncio
async def test_http_facade_rejects_reader_bodies_and_preserves_typed_safe_errors():
    import httpx
    from evolution_lab.service.app import create_app

    class Public:
        def to_payload(self):
            return {'schema': 'test-public/1'}

    class Runtime:
        def __init__(self):
            self.calls = []

        async def call_owner(self, method, *args, **kwargs):
            self.calls.append(method)
            if method == 'health':
                raise OwnerError('busy', 'before_start')
            return Public()

        async def call_preparation(self, method, *args, **kwargs):
            return Public()

        def connection(self, identifier):
            raise AssertionError(identifier)

    runtime = Runtime()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url='http://lab') as client:
        reader_body = await client.request('GET', '/v1/targets/target?owner_generation=generation',
            content=b'not-allowed')
        assert reader_body.status_code == 400
        assert reader_body.json()['error']['code'] == 'invalid_request'
        assert runtime.calls == []
        typed = await client.get('/v1/health')
        assert typed.status_code == 409
        assert typed.json()['error']['code'] == 'busy'
        assert typed.headers['cache-control'] == 'no-store'
        trailing = await client.post('/v1/operations/')
        assert trailing.status_code == 404
        assert trailing.headers['cache-control'] == 'no-store'


def config(tmp_path):
    tmp_path.chmod(0o700)
    # Always a Lab home under tmp_path: never the user's ~/.local/share/evolution-lab (R17).
    return ServiceConfig(tmp_path / 'service.sock', tmp_path / 'state', 0.2, home_directory=tmp_path / 'home')


@pytest.mark.asyncio
async def test_idle_owner_private_listener_and_second_owner_refusal(tmp_path):
    cfg = config(tmp_path)
    runtime = ServiceRuntime(cfg)
    assert await runtime.start()
    listener = PrivateListener.bind(cfg.socket_path)
    try:
        assert stat.S_IMODE(cfg.state_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(cfg.socket_path.stat().st_mode) == 0o600
        assert (await runtime.call_owner('health')).service_state == 'running'
        with pytest.raises(OwnerError) as caught:
            async with Owner.open(cfg.state_directory):
                pytest.fail('second Owner acquired service state')
        assert caught.value.code == 'busy'
    finally:
        result = await runtime.shutdown()
        listener.close()
    assert result.cleanup.complete is True
    assert not cfg.socket_path.exists()
    async with Owner.open(cfg.state_directory) as reopened:
        assert (await reopened.health()).owner_generation != result.owner_generation


@pytest.mark.asyncio
async def test_cancel_before_dispatch_and_after_bind_retains_connection(tmp_path, monkeypatch):
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = Connection('connection', 'evaluation', None, None, runtime.owner.owner_generation)

    async def bind(self, digest):
        entered.set()
        await release.wait()
        return accepted

    monkeypatch.setattr(Owner, 'bind', bind)
    before = asyncio.create_task(runtime.call_owner('bind', 'unused'))
    before.cancel()
    with pytest.raises(asyncio.CancelledError):
        await before
    assert not entered.is_set()
    after = asyncio.create_task(runtime.call_owner('bind', 'unused'))
    await entered.wait()
    after.cancel()
    with pytest.raises(asyncio.CancelledError):
        await after
    shutdown = runtime.request_shutdown()
    assert shutdown is runtime.request_shutdown()
    assert runtime.stopping.is_set()
    with pytest.raises(ServiceError) as caught:
        await runtime.call_owner('health')
    assert caught.value.code == 'service_stopping'
    assert not shutdown.done()
    release.set()
    assert (await asyncio.shield(shutdown)).cleanup.complete
    assert runtime.connection('connection') is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['start_target', 'check', 'conformance'])
async def test_cancelled_admission_ref_is_stopped_before_close(tmp_path, monkeypatch, action):
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    entered, release = asyncio.Event(), asyncio.Event()
    ref = TaskRef('accepted-task', runtime.owner.owner_generation)
    stops = []
    real_close = Owner.close

    async def accepted(self):
        entered.set()
        await release.wait()
        return ref

    async def stop(self, value):
        assert (await self.health()).service_state == 'running'
        stops.append(value)
        raise OwnerError('not_found', 'stop')

    async def close(self, **kwargs):
        assert stops == [ref]
        return await real_close(self, **kwargs)

    monkeypatch.setattr(Owner, action, accepted)
    monkeypatch.setattr(Owner, 'stop', stop)
    monkeypatch.setattr(Owner, 'close', close)
    caller = asyncio.create_task(runtime.call_owner(action))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    shutdown = runtime.request_shutdown()
    release.set()
    assert (await shutdown).cleanup.complete
    assert runtime.diagnostics[0]['error']['code'] == 'not_found'


@pytest.mark.asyncio
async def test_shutdown_drains_read_then_stops_then_preparation_before_unlock(tmp_path, monkeypatch):
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    loop = asyncio.get_running_loop()
    read_entered, read_release = asyncio.Event(), asyncio.Event()
    prep_entered, prep_release = asyncio.Event(), threading.Event()
    stop_entered = asyncio.Event()
    real_health = Owner.health
    real_close = Owner.close
    order = []

    async def read(self, *args):
        read_entered.set()
        await read_release.wait()
        order.append('read')
        return b'neutral saved bytes'

    async def health(self):
        from dataclasses import replace
        return replace(await real_health(self), active_operation_id='operation')

    async def stop(self, ref):
        assert ref.kind == 'operation' and ref.id == 'operation'
        order.append('stop')
        stop_entered.set()

    def preparation(self, digest):
        loop.call_soon_threadsafe(prep_entered.set)
        assert prep_release.wait(10)
        order.append('preparation')

    async def close(self, **kwargs):
        if kwargs:
            assert kwargs == {'wait_seconds': 0.2}
            order.append('close')
        return await real_close(self, **kwargs)

    from evolution_lab.preparation import PreparationStore
    monkeypatch.setattr(Owner, 'read_export', read)
    monkeypatch.setattr(Owner, 'health', health)
    monkeypatch.setattr(Owner, 'stop', stop)
    monkeypatch.setattr(Owner, 'close', close)
    monkeypatch.setattr(PreparationStore, 'get_bundle', preparation)
    reader = asyncio.create_task(runtime.call_owner('read_export', None, None))
    preparing = asyncio.create_task(runtime.call_preparation('get_bundle', 'unused'))
    await asyncio.gather(read_entered.wait(), prep_entered.wait())
    reader.cancel()
    preparing.cancel()
    await asyncio.gather(reader, preparing, return_exceptions=True)
    shutdown = runtime.request_shutdown()
    try:
        with pytest.raises(OwnerError):
            async with Owner.open(runtime.config.state_directory):
                pytest.fail('Owner unlocked during in-flight calls')
        read_release.set()
        await stop_entered.wait()
        assert not shutdown.done()
        with pytest.raises(OwnerError):
            async with Owner.open(runtime.config.state_directory):
                pytest.fail('Owner unlocked during preparation')
    finally:
        read_release.set()
        prep_release.set()
        await shutdown
    assert order == ['read', 'stop', 'preparation', 'close']


def test_private_path_refusals_and_own_inode_cleanup(tmp_path):
    cfg = config(tmp_path)
    cfg.validate_paths()
    cfg.socket_path.write_text('existing')
    with pytest.raises(ServiceError) as caught:
        PrivateListener.bind(cfg.socket_path)
    assert caught.value.code == 'socket_path_exists'
    assert cfg.socket_path.read_text() == 'existing'
    cfg.socket_path.unlink()
    listener = PrivateListener.bind(cfg.socket_path)
    cfg.socket_path.unlink()
    cfg.socket_path.write_text('replacement')
    listener.close()
    assert cfg.socket_path.read_text() == 'replacement'
    cfg.socket_path.unlink()
    cfg.state_directory.mkdir(mode=0o755)
    with pytest.raises(ServiceError):
        cfg.validate_paths()
    cfg.state_directory.rmdir()
    (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ServiceError):
        ServiceConfig(tmp_path / 'alias' / 'service.sock', cfg.state_directory).validate_paths()
    with pytest.raises(ServiceError):
        ServiceConfig(Path('relative'), cfg.state_directory).validate_paths()


@pytest.mark.asyncio
async def test_startup_failure_after_owner_emits_real_result_and_preserves_file(tmp_path):
    cfg = config(tmp_path)
    output = io.StringIO()

    def failing_app(runtime):
        raise RuntimeError('private startup detail')

    assert await serve(cfg, app_factory=failing_app, stderr=output) == 1
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert lines[0]['error']['code'] == 'lab_error'
    assert lines[1]['schema'] == 'lab-owner-shutdown/1'
    assert lines[1]['cleanup']['complete'] is True
    assert 'private startup detail' not in output.getvalue()
    assert not cfg.socket_path.exists()


@pytest.mark.asyncio
async def test_http_startup_failure_is_reported_once_after_unwind(tmp_path, monkeypatch):
    import evolution_lab.service.server as server_module
    config_class, _ = server_module._load_http()

    class FailedServer:
        should_exit = False

        def __init__(self, config):
            self.config = config

        async def serve(self, *, sockets):
            assert len(sockets) == 1
            raise OSError('private HTTP startup detail')

    monkeypatch.setattr(server_module, '_load_http', lambda: (config_class, FailedServer))
    cfg = config(tmp_path)
    output = io.StringIO()
    assert await serve(cfg, app_factory=lambda runtime: None, stderr=output) == 1
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0]['error']['code'] == 'io_error'
    assert lines[1]['schema'] == 'lab-owner-shutdown/1'
    assert lines[1]['cleanup']['complete'] is True
    assert 'private HTTP startup detail' not in output.getvalue()
    assert not cfg.socket_path.exists()


@pytest.mark.asyncio
async def test_existing_socket_refusal_unwinds_open_owner_and_preserves_file(tmp_path):
    cfg = config(tmp_path)
    cfg.socket_path.write_text('existing')
    output = io.StringIO()
    assert await serve(cfg, app_factory=lambda runtime: None, stderr=output) == 1
    lines = output.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])['error']['code'] == 'socket_path_exists'
    assert json.loads(lines[1])['schema'] == 'lab-owner-shutdown/1'
    assert json.loads(lines[1])['cleanup']['complete'] is True
    assert cfg.socket_path.read_text() == 'existing'
    assert cfg.state_directory.exists()


@pytest.mark.asyncio
async def test_missing_dependencies_and_busy_owner_do_not_fabricate_shutdown(tmp_path, monkeypatch):
    import evolution_lab.service.server as server_module
    cfg = config(tmp_path)
    output = io.StringIO()

    def missing():
        raise ImportError('private environment detail')

    with monkeypatch.context() as context:
        context.setattr(server_module, '_load_http', missing)
        assert await serve(cfg, stderr=output) == 1
    assert not cfg.state_directory.exists()
    assert not cfg.socket_path.exists()
    assert json.loads(output.getvalue())['error']['code'] == 'lab_error'
    assert 'dependencies' in output.getvalue()
    assert 'private environment detail' not in output.getvalue()
    cfg.state_directory.mkdir(mode=0o700)
    async with Owner.open(cfg.state_directory):
        output = io.StringIO()
        assert await serve(cfg, stderr=output) == 1
        assert json.loads(output.getvalue())['error']['code'] == 'busy'
        assert not cfg.socket_path.exists()


@pytest.mark.parametrize('phase', ['request', 'startup'])
def test_pid_signals_coalesce_and_restore_handlers(tmp_path, phase):
    # A neutral HTTP read is deliberately held across two signals. No target runs.
    cfg = config(tmp_path)
    child = r'''
import asyncio, signal, sys
from contextlib import asynccontextmanager
from evolution_lab.owner import Owner
from evolution_lab.service.runtime import ServiceConfig
from evolution_lab.service.server import serve

async def main():
    initial = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    release = asyncio.Event()
    entered = asyncio.Event()
    if sys.argv[3] == 'startup':
        real_open = Owner.open
        @classmethod
        @asynccontextmanager
        async def delayed_open(cls, state, **kwargs):
            async with real_open(state, **kwargs) as owner:
                print('OPEN', flush=True)
                await asyncio.sleep(0.3)
                yield owner
        Owner.open = delayed_open
    real_health = Owner.health
    async def delayed_health(self):
        if not entered.is_set():
            entered.set()
            print('READ', flush=True)
            await release.wait()
        return await real_health(self)
    Owner.health = delayed_health
    def factory(runtime):
        assert sys.argv[3] != 'startup', 'HTTP started after startup signal'
        async def unblock():
            await runtime.stopping.wait()
            print('STOPPING', flush=True)
            await asyncio.sleep(0.3)
            release.set()
        asyncio.create_task(unblock())
        async def app(scope, receive, send):
            await runtime.call_owner('health')
            await send({'type':'http.response.start', 'status':200, 'headers':[]})
            await send({'type':'http.response.body', 'body':b'ok'})
        return app
    if sys.argv[3] == 'startup':
        Owner.health = real_health
    code = await serve(ServiceConfig(sys.argv[1], sys.argv[2], 0.2, home_directory=sys.argv[4]), app_factory=factory)
    assert all(signal.getsignal(s) == h for s, h in initial.items())
    return code
raise SystemExit(asyncio.run(main()))
'''
    process = subprocess.Popen([sys.executable, '-c', child, str(cfg.socket_path), str(cfg.state_directory), phase,
        str(cfg.home_directory)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, 'PYTHONPATH': ''})
    import time
    deadline = time.monotonic() + 10
    client = socket.socket(socket.AF_UNIX)
    try:
        import select
        if phase == 'request':
            while True:
                try:
                    client.connect(str(cfg.socket_path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            client.sendall(b'GET / HTTP/1.1\r\nHost: local\r\n\r\n')
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == ('READ' if phase == 'request' else 'OPEN')
        os.kill(process.pid, signal.SIGTERM)
        if phase == 'request':
            assert select.select([process.stdout], [], [], 10)[0]
            assert process.stdout.readline().strip() == 'STOPPING'
        os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        client.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    payloads = [json.loads(line) for line in stderr.splitlines()]
    assert len(payloads) == 1
    assert payloads[0]['schema'] == 'lab-owner-shutdown/1'
    assert payloads[0]['cleanup']['complete'] is True
    assert not cfg.socket_path.exists()


def test_config_socket_too_long_code(tmp_path):
    with pytest.raises(ServiceError) as error:
        ServiceConfig(Path("/" + "s" * 110), tmp_path / "state", home_directory=tmp_path / "home")
    assert error.value.code == "socket_path_too_long"
    assert "--socket" in error.value.message


def test_run_service_defaults_socket_to_home(monkeypatch):
    # A short private directory, not tmp_path: the deep pytest tmp_path nesting under this
    # checkout can itself exceed the AF_UNIX sun_path limit, unrelated to what this test covers
    # (test_service_installed.py documents the same workaround for the installed-service tests).
    short = Path(tempfile.mkdtemp(prefix="w49-"))
    try:
        short.chmod(0o700)
        seen = {}

        async def fake_serve(config, *, stderr, on_ready=None):
            seen["config"] = config
            return 0
        monkeypatch.setattr("evolution_lab.service.server.serve", fake_serve)
        from evolution_lab.service.server import run_service
        home = short / "home"
        assert run_service(socket_path=None, state_directory=short / "state", home_directory=home) == 0
        assert seen["config"].socket_path == home / "service.sock"
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_serve_default_socket_on_fresh_home():
    """validate_paths must not stat a home that start() has not created yet."""
    # See test_run_service_defaults_socket_to_home: a short private directory, not tmp_path.
    short = Path(tempfile.mkdtemp(prefix="w49-"))
    try:
        short.chmod(0o700)
        home = short / "fresh-home"
        config = ServiceConfig(home / "service.sock", short / "state", home_directory=home)
        config.validate_paths()  # no FileNotFoundError, no invalid_argument
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_serve_prints_a_readiness_line_on_stdout():
    """W56: `lab serve` says where it listens once the socket is bound."""
    import select
    import time
    short = Path(tempfile.mkdtemp(prefix="w56-"))
    try:
        short.chmod(0o700)
        home = short / "h"
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; from evolution_lab.cli import main; raise SystemExit(main(sys.argv[1:]))",
             "serve", "--state", str(short / "s"), "--home", str(home)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        try:
            assert select.select([process.stdout], [], [], 10)[0]
            assert process.stdout.readline().strip() == f"lab serve: listening on {home}/service.sock"
        finally:
            process.terminate()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    finally:
        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.asyncio
async def test_content_routes_decode_once_and_refuse_invalid_requests():
    import httpx
    from evolution_lab.service.app import create_app
    class Runtime:
        def __init__(self):
            self.calls = []
        async def call_owner(self, method, *args, **kwargs):
            self.calls.append((method, args, kwargs))
            return {"schema": "test/1"}
    runtime = Runtime()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://lab") as client:
        for raw, decoded in [("%D0%BA%D0%B5%D0%B9%D1%81%2B1%40a", "кейс+1@a"), ("case-1", "case-1")]:
            response = await client.get(f"/v1/operations/op1/measurements/m-r01-a01/cases/{raw}/samples/00?disclose=private")
            assert response.status_code == 200
            method, args, kwargs = runtime.calls[-1]
            assert (method, args[0].id, *args[1:], kwargs) == ("sample", "op1", "m-r01-a01", decoded, "00", {"disclose": True})
        for path in ["/measurements/m/cases/%252540x/samples/00", "/measurements/m/cases/a%20b/samples/00",
                     "/measurements/m/cases/a%2Fb/samples/00", "/measurements/m/cases/c/samples/00?disclose=yes",
                     "/attempts/r1-a1", "/reflections/r01-a01"]:
            response = await client.get("/v1/operations/op1" + path)
            assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
        for path in ["/measurements/m/cases/c/samples/00", "/attempts/r01-a01", "/reflections/r01-x01"]:
            for suffix, data, headers in [("?bad=1", None, {}), ("?disclose=private&disclose=private", None, {}),
                                          ("", b"{}", {}), ("", None, {"lab-request-id": "bad"})]:
                response = await client.request("GET", "/v1/operations/op1" + path + suffix, content=data, headers=headers)
                assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


def test_service_roots_under_symlinked_ancestor_are_canonical(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    cfg = ServiceConfig(link / 'home/service.sock', link / 'state',
        home_directory=link / 'home', scratch_directory=link / 'scratch')
    assert cfg.state_directory == real.resolve() / 'state'
    assert cfg.home_directory == real.resolve() / 'home'
    assert cfg.socket_path == real.resolve() / 'home/service.sock'
    assert cfg.scratch_directory == real.resolve() / 'scratch'
    cfg.validate_paths()


@pytest.mark.parametrize('kind', ['state', 'home', 'scratch', 'socket_parent'])
def test_service_symlinked_root_is_refused(tmp_path, kind):
    cfg = config(tmp_path)
    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    values = dict(socket_path=cfg.socket_path, state_directory=cfg.state_directory,
        home_directory=cfg.home_directory)
    values[{'state':'state_directory','home':'home_directory','scratch':'scratch_directory',
        'socket_parent':'socket_path'}[kind]] = link / 'service.sock' if kind == 'socket_parent' else link
    with pytest.raises(ServiceError):
        ServiceConfig(**values).validate_paths()


def test_config_socket_canonical_address_too_long_is_refused(tmp_path):
    from evolution_lab.projects.endpoint import socket_fits
    real = tmp_path / ('r' * 100)
    real.mkdir(mode=0o700)
    (real / 'private').mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='lab-short-') as base:
        alias = Path(base) / 'l'
        alias.symlink_to(real, target_is_directory=True)
        requested = alias / 'private/s'
        assert socket_fits(requested)
        with pytest.raises(ServiceError) as failure:
            ServiceConfig(requested, tmp_path / 'state')
        assert failure.value.code == 'socket_path_too_long'


def test_service_error_survives_a_context_manager():
    # contextlib assigns __traceback__ to an exception thrown back out of its generator.
    @asynccontextmanager
    async def scope():
        yield

    async def scenario():
        async with scope():
            raise ServiceError('socket_path_too_long')

    with pytest.raises(ServiceError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == 'socket_path_too_long'
    with pytest.raises(AttributeError):
        caught.value.code = 'io_error'
