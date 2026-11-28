"""Launch policy stays outside Bundle and prepared identities."""
import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import runpy
from types import MappingProxyType, SimpleNamespace

import pytest

from test_client_operation import prepared, runtime  # fixtures
from platforms import linux_process_environ


def test_demo_refuses_invalid_mode_before_preparation(monkeypatch, tmp_path):
    from evolution_lab.durability import DurabilityConfigError
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'invalid')
    demo = Path(__file__).resolve().parents[1] / 'examples/owner/campaign.py'
    source, state = tmp_path / 'source', tmp_path / 'state'
    monkeypatch.setattr(sys, 'argv', [str(demo), '--python', sys.executable,
        '--lab-wheel', str(tmp_path / 'lab.whl'), '--core-wheel', str(tmp_path / 'core.whl'),
        '--source-directory', str(source), '--state-directory', str(state), '--port', '12345'])
    with pytest.raises(DurabilityConfigError, match='EVOLUTION_DURABILITY'):
        runpy.run_path(str(demo), run_name='__main__')
    assert not source.exists()
    assert not state.exists()


@pytest.mark.parametrize('schema', [1, 2, 3])
@pytest.mark.parametrize('mode', ['relaxed', 'strict'])
async def test_builtin_target_gets_launch_values_without_changing_bundle(monkeypatch, tmp_path, schema, mode):
    from evolution_lab.owner import Owner
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation.storage import PreparationStorage
    from test_owner_target import make_bundle, ready_target
    monkeypatch.setenv('EVOLUTION_DURABILITY', mode)
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    carried = {'EVOLUTION_DURABILITY': 'strict' if mode == 'relaxed' else 'relaxed'}
    if schema == 3:
        carried['TMPDIR'] = str(tmp_path / 'explicit')
        Path(carried['TMPDIR']).mkdir()
    state, bundle, _ = make_bundle(tmp_path, launch_schema=schema, target_env=carried)
    prep = PreparationStore(state)
    original = PreparationStorage(state).get_bundle(bundle.digest).descriptor.to_payload()
    digest = prep.prepare_campaign(bundle.digest).digest
    seen = []
    configure = HelperHandle.configure
    async def record(self, **kwargs):
        if kwargs.get('role') == 'target':
            seen.append(dict(kwargs['env']))
        return await configure(self, **kwargs)
    monkeypatch.setattr(HelperHandle, 'configure', record)
    async with Owner.open(state) as owner:
        await ready_target(owner, bundle)
    assert len(seen) == 1
    assert seen[0]['TARGET_TOKEN'] == 'private-neutral-token'
    if schema in (2, 3):
        assert seen[0]['EVOLUTION_DURABILITY'] == mode
        assert seen[0]['TMPDIR'] == carried.get('TMPDIR', str(tmp_path))
    else:
        assert seen[0]['EVOLUTION_DURABILITY'] == carried['EVOLUTION_DURABILITY']
        assert 'TMPDIR' not in seen[0]
    assert PreparationStorage(state).get_bundle(bundle.digest).descriptor.to_payload() == original
    assert prep.prepare_campaign(bundle.digest).digest == digest


@pytest.mark.parametrize('value', ['Strict', 'strict '])
def test_lab_cli_refuses_unknown(monkeypatch, tmp_path, value):
    from evolution_lab.cli import main
    from evolution_lab.service import server
    monkeypatch.setattr(server, 'run_service', lambda **kwargs: pytest.fail('service dispatched with invalid durability'))
    monkeypatch.setenv('EVOLUTION_DURABILITY', value)
    out, err = io.StringIO(), io.StringIO()
    state = tmp_path / 'state'
    assert main(['serve', '--state', str(state)], stdout=out, stderr=err) == 2
    assert f'EVOLUTION_DURABILITY must be "relaxed" or "strict" (got "{value}")' in err.getvalue()
    assert not state.exists()


def test_launch_values_copy_and_precedence(monkeypatch, tmp_path):
    from evolution_lab.launch_env import with_launch_values
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    original = {'EVOLUTION_DURABILITY': 'strict', 'UNCHANGED': 'yes'}
    assert with_launch_values(original) == {'EVOLUTION_DURABILITY': 'relaxed', 'TMPDIR': str(tmp_path), 'UNCHANGED': 'yes'}
    assert original == {'EVOLUTION_DURABILITY': 'strict', 'UNCHANGED': 'yes'}
    assert with_launch_values({'TMPDIR': '/explicit'})['TMPDIR'] == '/explicit'


@pytest.mark.parametrize('mode', ['relaxed', 'strict'])
async def test_helper_and_worker_get_value(monkeypatch, tmp_path, mode):
    from evolution_lab.launch_env import with_launch_values
    from evolution_lab.owner.protocol import HelperHandle
    from test_owner_scopes import scope, finish
    monkeypatch.setenv('EVOLUTION_DURABILITY', mode)
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    directory, sid, generation = scope(tmp_path)
    handle = HelperHandle.launch(sys.executable, directory, sid, generation)
    try:
        output = directory / 'env.json'
        code = 'import os,json; from pathlib import Path; Path(' + repr(str(output)) + ').write_text(json.dumps([os.environ.get("EVOLUTION_DURABILITY"), os.environ.get("TMPDIR")]))'
        await handle.configure(argv=[sys.executable, '-I', '-c', code], env=with_launch_values({'EVOLUTION_DURABILITY': 'conflicting'}), stop_grace_seconds=.2)
        await handle.wait_event('ready', 5)
        if sys.platform == 'linux':
            entries = linux_process_environ(handle.process.pid)
            assert f'EVOLUTION_DURABILITY={mode}'.encode() in entries
            assert f'TMPDIR={tmp_path}'.encode() in entries
        await handle.go()
        assert await handle.wait(5) == 0
        assert json.loads(output.read_text()) == [mode, str(tmp_path)]
    finally:
        await finish(handle)


async def test_h1_launch_gets_value(monkeypatch, tmp_path, prepared, runtime):
    from test_client_operation import new
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    environment = dict(prepared._environment, EVOLUTION_DURABILITY='strict')
    environment.pop('TMPDIR', None)
    prepared = replace(prepared, _environment=MappingProxyType(environment))
    before = prepared.public()
    output = tmp_path / 'h1-env.json'
    runtime('import os,json; from pathlib import Path; Path(' + repr(str(output)) + ').write_text(json.dumps([os.environ.get("EVOLUTION_DURABILITY"),os.environ.get("TMPDIR")]))')
    result = await asyncio.wait_for(new(prepared, tmp_path).run(), 10)
    assert result.exit_code == 0
    assert json.loads(output.read_text()) == ['relaxed', str(tmp_path)]
    assert prepared.public() == before
    assert prepared._environment['EVOLUTION_DURABILITY'] == 'strict'


async def test_bundle_env_value_replaced(monkeypatch, tmp_path):
    from evolution_lab.owner import Owner
    from evolution_lab.owner.protocol import HelperHandle
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation.storage import PreparationStorage
    from test_owner_target import make_bundle, ready_target
    create = PreparationStore.create_bundle
    def create_with_value(self, **kwargs):
        kwargs['env'] = dict(kwargs['env'], EVOLUTION_DURABILITY='strict')
        return create(self, **kwargs)
    monkeypatch.setattr(PreparationStore, 'create_bundle', create_with_value)
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    state, bundle, _ = make_bundle(tmp_path, conformance={"kind": "core-session-public/1"}, max_output_bytes=8192)
    preparation = PreparationStore(state)
    before = preparation.prepare_campaign(bundle.digest).digest
    original = PreparationStorage(state).get_bundle(bundle.digest).descriptor.to_payload()
    seen = []
    configure = HelperHandle.configure
    async def record(self, **kwargs):
        if kwargs.get('role') == 'conformance':
            seen.append(dict(kwargs['env']))
        return await configure(self, **kwargs)
    monkeypatch.setattr(HelperHandle, 'configure', record)
    async with Owner.open(state) as owner:
        connection, target, _ = await ready_target(owner, bundle)
        status = await asyncio.wait_for(owner.wait(await owner.conformance(connection, target_ref=target)), 20)
        assert status.state == 'succeeded', status.to_payload()
    assert seen and seen[0]['EVOLUTION_DURABILITY'] == 'relaxed'
    assert PreparationStorage(state).get_bundle(bundle.digest).descriptor.to_payload() == original
    assert preparation.prepare_campaign(bundle.digest).digest == before


@pytest.mark.parametrize('mode,journal,synchronous', [('relaxed', 'wal', 1), ('strict', 'delete', 2)])
async def test_gateway_ledger_follows_config(monkeypatch, tmp_path, mode, journal, synchronous):
    import uvicorn
    from evolution_lab import _gateway_helper, budget
    from test_execution_policy import policy_payload
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'strict' if mode == 'relaxed' else 'relaxed')
    policy = policy_payload()
    policy['gateway']['ledger_path'] = str(tmp_path / 'budget.sqlite3')
    observed = []
    factory = budget.gateway_from_policy
    def capture(*args, **kwargs):
        gateway = factory(*args, **kwargs)
        with gateway.ledger.connection() as db:
            observed.append((db.execute('PRAGMA journal_mode').fetchone()[0], db.execute('PRAGMA synchronous').fetchone()[0], gateway.ledger))
        return gateway
    monkeypatch.setattr(budget, 'gateway_from_policy', capture)
    async def nothing():
        pass
    monkeypatch.setattr(uvicorn, 'Server', lambda config: SimpleNamespace(serve=nothing))
    async def control_server(*args, **kwargs):
        return SimpleNamespace(close=lambda: None, wait_closed=nothing)
    monkeypatch.setattr(asyncio, 'start_unix_server', control_server)
    control = tmp_path / 'control'
    control.mkdir(mode=0o700)
    await _gateway_helper.serve({'durability': mode, 'policy': policy, 'upstream_keys': {'ROUTER_KEY': 'test'}, 'tokens': {name: 'client-test' for name in policy['routes']}, 'control_path': str(control / 'c')})
    assert observed[0][:2] == (journal, synchronous)
    assert observed[0][2]._anchor is None


def test_serve_flag_sets_process_value(monkeypatch, tmp_path):
    from evolution_lab.cli import main
    from evolution_lab.service import server
    from evolution_lab.service.runtime import ServiceRuntime
    from evolution_lab.owner.receipts import publish_json
    monkeypatch.delenv('EVOLUTION_DURABILITY', raising=False)
    synced = []
    monkeypatch.setattr(os, 'fsync', synced.append)
    async def inspect(config, **kwargs):
        runtime = ServiceRuntime(config)
        try:
            assert await runtime.start()
            assert os.environ['EVOLUTION_DURABILITY'] == 'strict'
            def pragma():
                return runtime.owner._store._database.execute('PRAGMA journal_mode').fetchone()[0]
            assert await runtime._loop.run_in_executor(runtime.owner._executor, pragma) == 'delete'
            synced.clear()
            publish_json(tmp_path / 'receipt.json', {'ok': True})
            assert synced
        finally:
            await runtime.shutdown()
        return 0
    monkeypatch.setattr(server, 'serve', inspect)
    assert main(['serve', '--state', str(tmp_path / 'state'), '--home', str(tmp_path / 'home'), '--durability', 'strict']) == 0
