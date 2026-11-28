"""Policy target scopes use installed S2 helpers and never persist credentials."""
import asyncio
import json
import os
import secrets
import signal
import socket
from pathlib import Path

import pytest
import psutil

from ownership_modes import ownership_mode  # noqa: F401
from platforms import linux_only

from test_execution_policy import policy_payload
from test_owner_target import make_bundle
from ports import free_port

pytestmark = pytest.mark.slow


def policy_document(tmp_path, port=None):
    document = policy_payload()
    document['routes'] = {'target': document['routes']['target']}
    document['routes']['target']['api_key_secret'] = 'UPSTREAM_KEY'
    document['scorer_token_names'] = {}
    document['role_groups'] = {}
    if port is None:
        port = free_port()
    document['gateway'].update(port=port, ledger_path=str(tmp_path / 'ledger.sqlite'))
    return document


def test_target_request_v2_and_legacy_are_closed():
    from evolution_lab.service.codecs import decode_target_start_request
    from evolution_lab.service.errors import ServiceError
    old = decode_target_start_request({'schema': 'lab-service-target-start-request/1', 'secrets': {}})
    assert old.execution_policy is None
    new = decode_target_start_request({'schema': 'lab-service-target-start-request/2', 'secrets': {}, 'execution_policy': 'a' * 64})
    assert new.execution_policy == 'a' * 64
    with pytest.raises(ServiceError):
        decode_target_start_request({'schema': 'lab-service-target-start-request/1', 'secrets': {}, 'execution_policy': None})


def test_conflict_failure_is_narrow_and_roundtrips():
    from evolution_lab.owner.models import Failure, OwnerError
    from evolution_lab.owner.tasks import failure_for
    error = OwnerError('token_name_conflict', 'target_start', token_conflict=('LLM_TOKEN', ('reflector', 'editor')))
    assert error.failure.message == 'Token name LLM_TOKEN conflicts between consumers: editor, reflector.'
    assert Failure(**error.failure.to_payload()) == failure_for(error, 'target_start')
    for message in ('private value', 'Token name LLM_TOKEN conflicts between consumers: private.',
                    'Token name LLM_TOKEN conflicts between consumers: reflector, editor.'):
        with pytest.raises(ValueError):
            Failure('token_name_conflict', 'target_start', message)
    with pytest.raises(ValueError):
        OwnerError('invalid_argument', 'target_start', token_conflict=('LLM_TOKEN', ('target',)))


async def confirmed(owner, document):
    from evolution_lab.execution_policy import policy_digest
    record = await owner.confirm_execution_policy(document, expected_digest=policy_digest(document), author='tester', channel='test')
    return record.digest


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('policy', [False, True])
def test_real_target_gateway_start_stop_and_private_environment(tmp_path, policy):
    from evolution_lab.owner import Owner, TargetRef, ResourceRef
    from evolution_lab.owner.receipts import read_cleanup_receipt
    launcher = (Path(__file__).parent / 'owner_fixtures/launcher.py').read_text()
    # Record booleans only. No key/token values enter artifacts or assertions.
    launcher = launcher.replace("mode = os.environ.get", "print(__import__('json').dumps({'token': bool(os.environ.get('TARGET_TOKEN')), 'upstream': 'UPSTREAM_KEY' in os.environ}), flush=True)\nmode = os.environ.get")
    state, bundle, _ = make_bundle(tmp_path, launcher_source=launcher)
    key = secrets.token_urlsafe(32)
    async def scenario():
        async with Owner.open(state) as owner:
            connection = await owner.bind(bundle.digest)
            digest = await confirmed(owner, policy_document(tmp_path)) if policy else None
            task = await owner.start_target(connection, secrets={'UPSTREAM_KEY': key} if policy else {'TARGET_TOKEN': key}, execution_policy=digest)
            result = await owner.wait(task)
            assert result.state == 'succeeded', result.to_payload()
            target = TargetRef(result.result.payload['id'], owner.owner_generation)
            status = await owner.target(target)
            assert status.schema == 'lab-owner-target/2'
            assert ('execution_policy' in status.to_payload()) is policy
            runtime = owner._targets[target.id]
            assert (runtime.gateway is not None) is policy
            if policy:
                assert status.execution_policy == digest
                assert runtime.gateway.helper.process.args[0] == __import__('sys').executable
                await runtime.gateway.set_uses(frozenset({'probe:neutral'}))
                from evolution_lab.owner.protocol import encode_frame
                from evolution_lab._gateway_helper import control_frame
                reader, writer = await asyncio.open_unix_connection(str(runtime.gateway.control_directory / 'c'))
                try:
                    writer.write(encode_frame({'credential': 'not-authorized', 'uses': ['forged']}))
                    await writer.drain()
                    with pytest.raises(asyncio.IncompleteReadError):
                        await control_frame(reader)
                finally:
                    writer.close()
                    await writer.wait_closed()
            facts = json.loads((state / 'scopes' / runtime.scope_id / 'stdout.log').read_text())
            assert facts == {'token': True, 'upstream': False}
            reused = await owner.wait(await owner.start_target(connection, secrets={'UPSTREAM_KEY': key} if policy else {'TARGET_TOKEN': key}, execution_policy=digest))
            assert reused.result.payload['id'] == target.id
            if policy:
                changed = policy_document(tmp_path)
                changed['gateway'] = (await owner.execution_policy(digest)).to_payload()['policy']['gateway']
                changed['routes']['target']['model'] = 'another-model'
                other = await confirmed(owner, changed)
                from evolution_lab.owner import OwnerError
                with pytest.raises(OwnerError) as refusal:
                    await owner.start_target(connection, secrets={'UPSTREAM_KEY': key}, execution_policy=other)
                assert refusal.value.code == 'binding_mismatch'
            stopped = await owner.wait(await owner.stop_target(target))
            assert stopped.state == 'succeeded', stopped.to_payload()
            scopes = await owner._db('resource_scopes', ResourceRef('target', target.id))
            assert len(scopes) == (2 if policy else 1)
            for row in scopes:
                receipt = read_cleanup_receipt(state / row['root_relative'], row['id'], row['owner_generation'])
                assert receipt['schema'] == 'lab-scope-cleanup/2' and receipt['process_tree_empty']
            assert not await owner._db('reservations')
            for file in state.rglob('*'):
                if file.is_file():
                    assert key.encode() not in file.read_bytes(), str(file)
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('reason', ['unconfirmed', 'busy', 'secret_conflict', 'missing_extra'])
def test_refusals_precede_target_dispatch(tmp_path, reason, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, _ = make_bundle(tmp_path)
    if reason == 'missing_extra':
        import evolution_lab.owner.target_gateway as gateway_module
        original = gateway_module.importlib.util.find_spec
        monkeypatch.setattr(gateway_module.importlib.util, 'find_spec', lambda name: None if name == 'uvicorn' else original(name))
    async def scenario():
        async with Owner.open(state) as owner:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); sock.listen()
                document = policy_document(tmp_path, sock.getsockname()[1] if reason == 'busy' else None)
                digest = 'f' * 64 if reason == 'unconfirmed' else await confirmed(owner, document)
                supplied = {'UPSTREAM_KEY': secrets.token_urlsafe(32)}
                if reason == 'secret_conflict':
                    supplied['TARGET_TOKEN'] = secrets.token_urlsafe(32)
                code = {'unconfirmed': 'policy_unconfirmed', 'busy': 'gateway_port_busy', 'secret_conflict': 'secret_conflict', 'missing_extra': 'gateway_unavailable'}[reason]
                with pytest.raises(OwnerError) as caught:
                    await owner.start_target(await owner.bind(bundle.digest), secrets=supplied, execution_policy=digest)
                assert caught.value.code == code
                assert not owner._targets
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_gateway_child_death_stops_target_without_restart(tmp_path):
    from evolution_lab.owner import Owner, TargetRef, ResourceRef
    state, bundle, _ = make_bundle(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            digest = await confirmed(owner, policy_document(tmp_path))
            result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest), secrets={'UPSTREAM_KEY': secrets.token_urlsafe(32)}, execution_policy=digest))
            assert result.state == 'succeeded', result.to_payload()
            ref = TargetRef(result.result.payload['id'], owner.owner_generation)
            runtime = owner._targets[ref.id]
            # Test-only inspection of the live helper's child; production never stores PIDs.
            child, = (p.pid for p in psutil.Process(runtime.gateway.helper.process.pid).children())
            os.kill(child, signal.SIGKILL)
            async with asyncio.timeout(10):
                await asyncio.shield(runtime.ended)
            status = await owner.target(ref)
            assert status.state == 'stopped' and status.failure.code == 'gateway_unavailable'
            assert len(await owner._db('resource_scopes', ResourceRef('target', ref.id))) == 2
            assert not await owner._db('reservations')
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_gateway_forwards_only_upstream_key_and_records_acknowledged_uses(tmp_path):
    import httpx
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from evolution_lab.owner import Owner, TargetRef
    from evolution_lab.budget import BudgetLedger
    key = secrets.token_urlsafe(32)
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['content-length'])))
            received.append((self.headers.get('Authorization') == 'Bearer ' + key, body['model']))
            raw = json.dumps({'model': body['model'], 'choices': [{'message': {'content': 'ok'}}],
                              'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    state, bundle, _ = make_bundle(tmp_path)
    document = policy_document(tmp_path)
    document['routes']['target']['endpoint'] = f'http://127.0.0.1:{server.server_port}/v1'
    async def scenario():
        async with Owner.open(state) as owner:
            digest = await confirmed(owner, document)
            result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest), secrets={'UPSTREAM_KEY': key}, execution_policy=digest))
            assert result.state == 'succeeded', result.to_payload()
            ref = TargetRef(result.result.payload['id'], owner.owner_generation)
            gateway = owner._targets[ref.id].gateway
            await gateway.set_uses(frozenset(f'probe:overlap-{i:05d}' for i in range(400)))
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(f'http://127.0.0.1:{document["gateway"]["port"]}/target/v1/chat/completions',
                    headers={'Authorization': 'Bearer ' + gateway.tokens['target']},
                    json={'model': 'target-client', 'messages': [{'role': 'user', 'content': 'neutral'}], 'max_tokens': 10})
            assert response.status_code == 200, response.text
            assert received == [(True, 'local-model')]
            await owner.wait(await owner.stop_target(ref))
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown(); server.server_close(); thread.join()
    import sqlite3
    with sqlite3.connect(document['gateway']['ledger_path']) as db:
        row = db.execute('SELECT uses_json FROM routing_observations').fetchone()
    assert json.loads(row[0]) == [f'probe:overlap-{i:05d}' for i in range(400)]


@pytest.mark.usefixtures('ownership_mode')
def test_owner_death_recovers_both_target_scopes(tmp_path):
    import sys
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.receipts import read_cleanup_receipt
    state, bundle, _ = make_bundle(tmp_path)
    document = policy_document(tmp_path)
    script = '''import asyncio, json, secrets, sys
from evolution_lab.owner import Owner
from evolution_lab.execution_policy import policy_digest
async def main():
    async with Owner.open(sys.argv[1]) as owner:
        document = json.loads(sys.argv[3])
        record = await owner.confirm_execution_policy(document, expected_digest=policy_digest(document), author='tester', channel='test')
        task = await owner.start_target(await owner.bind(sys.argv[2]), secrets={'UPSTREAM_KEY': secrets.token_urlsafe(32)}, execution_policy=record.digest)
        print(json.dumps((await owner.wait(task)).to_payload()), flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
'''
    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', script,
            str(state), bundle.digest, json.dumps(document), stdout=asyncio.subprocess.PIPE)
        try:
            result = json.loads(await asyncio.wait_for(process.stdout.readline(), 12))
            assert result['state'] == 'succeeded', result
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        scopes = list((state / 'scopes').iterdir())
        assert len(scopes) == 2
        async with asyncio.timeout(10):
            while not all((scope / 'cleanup.receipt').exists() for scope in scopes):
                await asyncio.sleep(.02)
        for scope in scopes:
            assert read_cleanup_receipt(scope, scope.name, result['owner_generation'])['process_tree_empty']
        async with Owner.open(state) as owner:
            assert (await owner.health()).blockers
            ref = ResourceRef('target', result['result']['payload']['id'])
            rows = await owner._db('resource_scopes', ref)
            assert {row['resource_kind'] for row in rows} == {'target', 'target_gateway'}
            cleaned = await owner.wait(await owner.cleanup(ref))
            assert cleaned.state == 'succeeded', cleaned.to_payload()
            assert not await owner._db('reservations')
            assert all(row['state'] == 'released' for row in await owner._db('resource_scopes', ref))
    asyncio.run(scenario())


def test_no_policy_target_schema_is_only_reference_change():
    from evolution_lab.cli.responses import validate
    from evolution_lab.cli.output import write_value
    from io import StringIO
    reference = Path(__file__).parent / 'data/dev65-phase-a-reference/target.json'
    payload = json.loads(reference.read_text())
    payload['schema'] = 'lab-owner-target/2'
    validate(payload, 'lab-owner-target/2')
    assert 'execution_policy' not in payload
    output = StringIO()
    write_value(output, payload, machine=False)
    assert output.getvalue() == reference.with_suffix('.txt').read_text().replace('lab-owner-target/1', 'lab-owner-target/2')


def test_wrapped_validated_conflict_keeps_diagnostic_but_not_arbitrary_messages():
    from types import SimpleNamespace
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.owner.tasks import failure_for
    failure = OwnerError('token_name_conflict', 'target_start', token_conflict=('TOKEN', ('editor', 'target'))).failure
    wrapper = RuntimeError()
    wrapper.failure = failure
    projected = failure_for(wrapper, 'before_start')
    assert projected.stage == 'before_start' and projected.message == failure.message
    wrapper.failure = SimpleNamespace(code='token_name_conflict', message='private injected value')
    assert 'private' not in failure_for(wrapper, 'before_start').message


def token_binding_fixture():
    from types import SimpleNamespace as NS
    return NS(launch={'secret_names': ['TARGET_TOKEN'], 'env': {}}, base_environment={},
              source_kind='campaign', manifest=NS(workflow=NS(
                  scorer=NS(allowed_env=['TARGET_TOKEN']), editor=NS(),
                  reflector=NS(role=NS()), grouper=NS())))


def test_ordinary_target_secret_cannot_implicitly_bind_to_other_consumer(tmp_path):
    from evolution_lab.execution_policy import parse_policy
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.owner.secrets import policy_token_bindings
    document = policy_document(tmp_path, 12345)
    del document['target_token_name']
    document['scorer_token_names'] = {'TARGET_TOKEN': 'target'}
    with pytest.raises(OwnerError) as caught:
        policy_token_bindings(token_binding_fixture(), parse_policy(document))
    assert caught.value.code == 'token_name_conflict'
    assert 'TARGET_TOKEN' in caught.value.failure.message and 'scorer, target' in caught.value.failure.message


def test_requested_upstream_keys_are_excluded_from_runtime_repr():
    from evolution_lab.owner.target import TargetRuntime
    runtime = TargetRuntime(None, 'scope', None, None, None, None, requested_secrets={'UPSTREAM_KEY': 'private-marker'})
    assert 'private-marker' not in repr(runtime)


def test_gateway_health_transport_failure_has_public_gateway_code(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from evolution_lab.execution_policy import parse_policy
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.target_gateway import TargetGateway
    runtime = SimpleNamespace(binding=SimpleNamespace(launch={'startup_timeout_seconds': 1, 'stop_grace_seconds': .1}))
    gateway = TargetGateway(None, runtime)
    gateway.control_directory = tmp_path / 'control'
    gateway.control_directory.mkdir(mode=0o700)
    gateway.helper = SimpleNamespace(configure=AsyncMock(), wait_event=AsyncMock(), go=AsyncMock(),
                                     process=SimpleNamespace(poll=lambda: None), events=[])
    monkeypatch.setattr('evolution_lab.owner.target_gateway.check_gateway_port', lambda policy: None)
    monkeypatch.setattr(gateway, '_open', AsyncMock())
    monkeypatch.setattr(gateway, '_control', AsyncMock())
    monkeypatch.setattr(httpx.AsyncClient, 'get', AsyncMock(side_effect=httpx.ConnectError('unavailable')))
    async def scenario():
        with pytest.raises(OwnerError) as caught:
            await gateway.start(parse_policy(policy_document(tmp_path, 12345)), {'UPSTREAM_KEY': secrets.token_urlsafe(32)})
        assert caught.value.code == 'gateway_unavailable'
        assert gateway.helper.configure.call_args.kwargs['child_config']['control_path'] == str(gateway.control_directory / 'c')
    asyncio.run(scenario())


def test_target_response_cannot_explicitly_null_policy():
    from evolution_lab.cli.responses import _target
    payload = json.loads((Path(__file__).parent / 'data/dev65-phase-a-reference/target.json').read_text())
    payload.update(schema='lab-owner-target/2', execution_policy=None)
    with pytest.raises(ValueError):
        _target(payload)


@pytest.mark.parametrize('shared_route', [True, False])
def test_core_consumer_name_bindings_agree_only_on_same_route(tmp_path, shared_route):
    from types import SimpleNamespace as NS
    from evolution_lab.execution_policy import parse_policy
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.secrets import policy_token_bindings
    document = policy_document(tmp_path, 12345)
    document['routes']['editor'] = dict(document['routes']['target'])
    document['scorer_token_names'] = {'TARGET_TOKEN': 'target'}
    binding = token_binding_fixture()
    binding.manifest.workflow.editor = NS(api_key_env='SHARED', base_url='http://127.0.0.1:12345/editor/v1')
    binding.manifest.workflow.reflector.role = NS(api_key_env='SHARED',
        base_url='http://127.0.0.1:12345/' + ('editor' if shared_route else 'target') + '/v1')
    if shared_route:
        assert policy_token_bindings(binding, parse_policy(document))['SHARED'] == 'editor'
    else:
        with pytest.raises(OwnerError) as caught:
            policy_token_bindings(binding, parse_policy(document))
        assert caught.value.failure.message == 'Token name SHARED conflicts between consumers: editor, reflector.'


@pytest.mark.usefixtures('ownership_mode')
def test_target_lifetime_waits_for_gateway_receipt_confirmation(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, TargetRef, ResourceRef
    from evolution_lab.owner.target_gateway import TargetGateway
    state, bundle, _ = make_bundle(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = TargetGateway.stop
    async def delayed(self):
        await self.request_stop()
        entered.set()
        await release.wait()
        await original(self)
    monkeypatch.setattr(TargetGateway, 'stop', delayed)
    async def scenario():
        async with Owner.open(state) as owner:
            digest = await confirmed(owner, policy_document(tmp_path))
            result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest),
                secrets={'UPSTREAM_KEY': secrets.token_urlsafe(32)}, execution_policy=digest))
            assert result.state == 'succeeded', result.to_payload()
            target = TargetRef(result.result.payload['id'], owner.owner_generation)
            task = await owner.stop_target(target)
            try:
                await asyncio.wait_for(entered.wait(), 5)
                runtime = owner._targets[target.id]
                assert (state / 'scopes' / runtime.scope_id / 'cleanup.receipt').exists()
                assert 'target_lifetime' in await owner._db('reservations')
                rows = await owner._db('resource_scopes', ResourceRef('target', target.id))
                assert all(row['state'] != 'released' for row in rows)
                from evolution_lab.owner import OwnerError
                with pytest.raises(OwnerError) as caught:
                    await owner._db('release_evidenced_reservations', ResourceRef('target', target.id),
                        scope_ids=(runtime.scope_id,), disposition='confirmed_cleanup')
                assert caught.value.code == 'cleanup_required'
            finally:
                release.set()
            assert (await owner.wait(task)).state == 'succeeded'
            assert not await owner._db('reservations')
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_gateway_death_during_campaign_stops_target_and_retains_common_cleanup(tmp_path):
    import httpx
    from evolution_lab.owner import Owner, TargetRef, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from test_owner_cleanup import finished
    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode='sleep:1')
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            digest = await confirmed(owner, policy_document(tmp_path))
            result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest),
                secrets={'UPSTREAM_KEY': secrets.token_urlsafe(32)}, execution_policy=digest))
            assert result.state == 'succeeded', result.to_payload()
            target = TargetRef(result.result.payload['id'], owner.owner_generation)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            worker = owner._operations[operation.id]
            async with asyncio.timeout(10):
                while worker.helper is None:
                    await asyncio.sleep(.02)
                await worker.helper.wait_event('child_started')
                async with httpx.AsyncClient(trust_env=False) as client:
                    while True:
                        health = await client.get((await owner.target(target)).base_url + '/evolution/v2/health')
                        if health.json()['instances']:
                            break
                        await asyncio.sleep(.02)
            assert worker.helper.process.poll() is None
            runtime = owner._targets[target.id]
            helper_pid = runtime.gateway.helper.process.pid
            child, = (p.pid for p in psutil.Process(helper_pid).children())
            os.kill(child, signal.SIGKILL)
            async with asyncio.timeout(10):
                await asyncio.shield(runtime.ended)
            assert (await owner.target(target)).failure.code == 'gateway_unavailable'
            ref = ResourceRef('operation', operation.id)
            status = await finished(owner, ref, seconds=30)
            assert status.control_state == 'cleanup_required', status.to_payload()
            assert status.failure is None, status.to_payload()
            assert status.result is not None and status.result.core is not None, status.to_payload()
            core = status.result.core
            assert core['suspended'] is True and core['complete'] is False, core
            from evolution_lab._suspension import last_suspension
            suspension = last_suspension(core)
            assert (suspension['reason'], suspension['owner']) in {
                ('connection_preflight_unavailable', 'connection'), ('target_unstable', 'target')}, suspension
            print('gateway-death Core outcome: suspended=True complete=False '
                  f"reason={suspension['reason']} owner={suspension['owner']}")
            scopes = await owner._db('resource_scopes', ref, include_target=True)
            assert {'operation', 'target', 'target_gateway'} <= {s['resource_kind'] for s in scopes}
            assert 'target_lifetime' in await owner._db('reservations')
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
            assert not await owner._db('reservations')
    asyncio.run(scenario())


def test_cli_policy_target_start_uses_v2_request():
    from test_service_cli import FakeClient, invoke, TASK_REF
    client = FakeClient([TASK_REF])
    code, _, stderr = invoke(['--no-request-id', 'target', 'start', 'connection', '--execution-policy', 'a' * 64], client)
    assert code == 0, stderr
    assert client.calls[0][2] == {'schema': 'lab-service-target-start-request/2', 'secrets': {}, 'execution_policy': 'a' * 64}


@pytest.mark.usefixtures('ownership_mode')
def test_lab_filled_target_names_are_excluded_but_never_overridden(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.secrets import bind_target_secrets
    state, bundle, _ = make_bundle(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            binding = owner._connection_binding(await owner.bind(bundle.digest))
            bound = bind_target_secrets(binding, {}, lab_filled=frozenset({'TARGET_TOKEN'}))
            assert bound.names == ('TARGET_TOKEN',) and not bound.values
            with pytest.raises(OwnerError) as caught:
                bind_target_secrets(binding, {'TARGET_TOKEN': secrets.token_urlsafe(32)}, lab_filled=frozenset({'TARGET_TOKEN'}))
            assert caught.value.code == 'secret_conflict'
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_policy_without_gateway_starts_real_target_without_gateway(tmp_path):
    from evolution_lab.owner import Owner, TargetRef
    state, bundle, _ = make_bundle(tmp_path)
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 1, 'delay_seconds': 0}}
    async def scenario():
        async with Owner.open(state) as owner:
            digest = await confirmed(owner, document)
            connection = await owner.bind(bundle.digest)
            result = await owner.wait(await owner.start_target(connection, secrets={'TARGET_TOKEN': 'ordinary'}, execution_policy=digest))
            assert result.state == 'succeeded', result.to_payload()
            target = TargetRef(result.result.payload['id'], owner.owner_generation)
            assert owner._targets[target.id].gateway is None
            assert (await owner.target(target)).execution_policy == digest
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
    asyncio.run(scenario())


async def _control_gateway(tmp_path, owner, bundle):
    from evolution_lab.owner import TargetRef
    connection = await owner.bind(bundle.digest)
    digest = await confirmed(owner, policy_document(tmp_path))
    result = await owner.wait(await owner.start_target(connection, secrets={'UPSTREAM_KEY': 'offline-key'}, execution_policy=digest))
    assert result.state == 'succeeded', result.to_payload()
    target = TargetRef(result.result.payload['id'], owner.owner_generation)
    return target, owner._targets[target.id].gateway


async def test_gateway_control_socket_is_a_private_path(tmp_path, ownership_mode):
    import stat
    from evolution_lab.owner import Owner
    state, bundle, _ = make_bundle(tmp_path)
    async with Owner.open(state) as owner:
        target, gateway = await _control_gateway(tmp_path, owner, bundle)
        directory = gateway.control_directory
        info = directory.stat()
        assert directory.is_absolute() and stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()
        assert stat.S_ISSOCK((directory / 'c').stat().st_mode)
        assert stat.S_IMODE((directory / 'c').stat().st_mode) == 0o700
        await gateway.set_uses(frozenset({'probe:private-path'}))
        assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
        assert not directory.exists()


def _unopened_control_gateway(tmp_path, ownership_mode):
    from types import SimpleNamespace
    from evolution_lab.owner.target_gateway import TargetGateway
    state = tmp_path / 'state'
    (state / 'scopes').mkdir(parents=True)

    async def database(method, *args, **kwargs):
        return 'a' * 32 if method == 'accept_target_gateway_scope' else None

    owner = SimpleNamespace(state_directory=state, owner_generation='b' * 32,
        ownership=ownership_mode, _db=database)
    runtime = SimpleNamespace(ref=SimpleNamespace(id='c' * 32), binding=SimpleNamespace(launch={
        'startup_timeout_seconds': 1, 'stop_grace_seconds': .1}))
    return TargetGateway(owner, runtime)


async def test_gateway_control_directory_removed_after_failed_open(tmp_path, monkeypatch, ownership_mode):
    from evolution_lab.owner.protocol import HelperHandle
    gateway = _unopened_control_gateway(tmp_path, ownership_mode)
    def failed(*args, **kwargs):
        raise OSError('launch failed')
    monkeypatch.setattr(HelperHandle, 'launch', failed)
    with pytest.raises(OSError):
        await gateway._open()
    assert gateway.control_directory is not None
    assert not gateway.control_directory.exists()


async def test_gateway_control_path_too_long_is_refused(tmp_path, monkeypatch, ownership_mode):
    import tempfile
    from evolution_lab.execution_policy import parse_policy
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.protocol import HelperHandle
    parent = tmp_path / ('long-' + 'x' * 100)
    parent.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(parent))
    monkeypatch.setattr(HelperHandle, 'launch', lambda *a, **kw: pytest.fail('helper launched for an oversized path'))
    gateway = _unopened_control_gateway(tmp_path, ownership_mode)
    with pytest.raises(OwnerError) as failure:
        await gateway.start(parse_policy(policy_document(tmp_path)), {'UPSTREAM_KEY': 'offline-key'})
    assert failure.value.code == 'gateway_unavailable'
    assert not list(parent.glob('lab-gw-*'))


@linux_only('inspect only this gateway child sockets through /proc')
async def test_gateway_control_socket_avoids_abstract_namespace(tmp_path, ownership_mode):
    import psutil
    from evolution_lab.owner import Owner
    state, bundle, _ = make_bundle(tmp_path)
    async with Owner.open(state) as owner:
        target, gateway = await _control_gateway(tmp_path, owner, bundle)
        directory = gateway.control_directory
        inodes = set()
        for child in psutil.Process(gateway.helper.process.pid).children():
            for fd in Path(f'/proc/{child.pid}/fd').iterdir():
                try:
                    link = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if link.startswith('socket:['):
                    inodes.add(link[8:-1])
        paths = [f[7] for line in Path('/proc/net/unix').read_text().splitlines()[1:]
                 if len(f := line.split()) > 7 and f[6] in inodes]
        assert str(directory / 'c') in paths
        assert not any(path.startswith('@lab-gateway-') for path in paths)
        assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
