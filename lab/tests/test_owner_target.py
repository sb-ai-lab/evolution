"""V-S2-3: actual managed target lifecycle and cancellation ownership."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import subprocess
import uuid
import venv
from pathlib import Path

import pytest

from ownership_modes import ownership_mode  # noqa: F401
from scaffold_15 import scaffold_15
from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from ports import free_port

pytestmark = pytest.mark.slow


@pytest.mark.parametrize('scratch_enabled', [False, True])
def test_target_scratch_paths_keep_scopes_and_artifacts_durable(tmp_path, scratch_enabled):
    from types import SimpleNamespace
    from evolution_lab.owner.target import create_target_paths, runtime_config
    from evolution_lab.owner import TargetRef
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    state.mkdir(mode=0o700)
    pair.mkdir(mode=0o700)
    scratch = pair if scratch_enabled else None
    create_target_paths(state, 'target', 'scope', scratch=scratch)
    policy = SimpleNamespace(to_payload=lambda: {})
    binding = SimpleNamespace(launch={'schema': 'lab-target-launch/1', 'endpoint': {'base_url': 'http://localhost'},
        'identity': {}, 'limits': {}}, launch_digest='a' * 64, bundle_digest='b' * 64,
        manifest=SimpleNamespace(policy=policy))
    cfg = runtime_config(state, binding, TargetRef('1' * 32, '2' * 32), scratch=scratch)
    root = pair if scratch_enabled else state
    assert (root / 'targets' / 'target').is_dir()
    assert (root / 'snapshots' / 'target').is_dir()
    assert (state / 'scopes' / 'scope').is_dir()
    assert (state / 'artifacts').is_dir()
    assert cfg['allowed_artifact_root'] == str(state / 'artifacts')
    assert cfg['work_directory'] == str(root / 'targets' / ('1' * 32))
    assert cfg['snapshot_root'] == str(root / 'snapshots' / ('1' * 32))
    if scratch_enabled:
        assert not (state / 'targets').exists()
        assert not (state / 'snapshots').exists()
        assert not (pair / 'scopes').exists()
        assert not (pair / 'artifacts').exists()


def make_bundle(tmp_path, *, mode='normal', port=None, max_instances=4,
                max_output_bytes=4096, target_environment=None,
                launcher_source=None, conformance=None, rollout_timeout_seconds=None,
                launch_schema=1, model_grouping=None, bundle_secret_names=(), target_timeouts=None,
                stop_grace_seconds=.2, scaffold=scaffold_15, reflector_probe=None, target_env=None):
    from evolution_lab.preparation import CampaignSource, EnvironmentInput, PreparationStore, SourceRoot, describe_environment
    source = tmp_path / 'source'
    scaffold(source)
    if port is None:
        port = free_port()
    manifest_path = source / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['target']['base_url'] = f'http://127.0.0.1:{port}'
    if model_grouping is not None:
        manifest['grouper'] = dict(model_grouping)
    if rollout_timeout_seconds is not None:
        manifest['campaign']['budget']['timeout_seconds'] = rollout_timeout_seconds
    if target_timeouts is not None:
        manifest['target']['timeouts'] = dict(target_timeouts)
    for role in ('scorer', 'editor', 'reflector'):
        if 'argv' in manifest[role]:
            manifest[role]['argv'][0] = sys.executable
    manifest_path.write_text(json.dumps(manifest))
    environment = EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ())
    descriptor = describe_environment(target_environment or environment)
    common = dict(environment_ref=descriptor.digest,
        env={'TARGET_MODE': mode, **(target_env or {})}, secret_names=['TARGET_TOKEN'],
        endpoint={'base_url': manifest['target']['base_url']},
        identity=dict(target_id='neutral-target', contract_digest='a' * 64,
            settings_hash='b' * 64, runtime_ref_digest='c' * 64,
            seed_mode=manifest['target']['seed_mode']),
        policy='manifest.json', limits=dict(max_instances=max_instances, max_concurrent_sessions=1,
            max_input_bytes=2_000_000, max_output_bytes=max_output_bytes),
        # A ready target reports at once; 3 s was too short for a Python target on a loaded host (#475).
        startup_timeout_seconds=20, stop_grace_seconds=stop_grace_seconds, conformance=conformance)
    if launch_schema in {2, 3}:
        shutil.copytree(Path(__file__).parent / 'owner_fixtures' / 'echo_agent', source / 'agent')
        launch = dict(schema='lab-target-launch/2',
            agent=dict(module='echo_agent', attribute='build', import_root='agent'),
            program_paths=['agent'], **common)
    else:
        shutil.copyfile(Path(__file__).parent / 'owner_fixtures' / 'launcher.py', source / 'launcher.py')
        if launcher_source is not None:
            (source / 'launcher.py').write_text(launcher_source)
        launch = dict(schema='lab-target-launch/1',
            command=dict(executable=str(descriptor.executable), args=[{'bundle_path': 'launcher.py'}]),
            program_paths=['launcher.py'], **common)
    if launch_schema == 3:
        launch.update(schema='lab-target-launch/3', reflector_probe=reflector_probe,
                      turn={'empty_answer': 'target_failed'})
    launch_path = source / 'launch.json'
    launch_path.write_text(json.dumps(launch))
    state = tmp_path / 'state'
    prep = PreparationStore(state)
    bundle = prep.create_bundle(source=CampaignSource(manifest_path, launch_path),
        environments=(environment,) if target_environment is None else (environment, target_environment),
        env={'TARGET_MODE': mode, **(target_env or {})}, secret_names=['TARGET_TOKEN', *bundle_secret_names],
        roots=(SourceRoot('project', source),))
    return state, bundle, port


@pytest.mark.usefixtures('ownership_mode')
def test_distinct_target_environment_without_lab_uses_producing_helper(tmp_path):
    from evolution_lab.owner import Owner
    from evolution_lab.owner.receipts import read_cleanup_receipt
    from evolution_lab.preparation import EnvironmentInput
    target_root = tmp_path / 'target-environment'
    venv.EnvBuilder(with_pip=False).create(target_root)
    target_python = target_root / 'bin' / 'python'
    subprocess.run([sys.executable, '-m', 'pip', '--python', str(target_python),
        '--no-cache-dir', 'install', '--no-index', '--no-deps', str(CORE_WHEEL)],
        check=True, capture_output=True, text=True)
    # This independent neutral launcher implements only ready/health. It has no
    # dependency on Lab and must run in the target interpreter, not the helper's.
    launcher = '''import os
config_fd = int(os.environ.pop('LAB_TARGET_CONFIG_FD'))
ready_fd = int(os.environ.pop('LAB_TARGET_READY_FD'))
os.set_inheritable(config_fd, False)
os.set_inheritable(ready_fd, False)
import hashlib, importlib.util, json, signal, struct, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
def read_exact(size):
 data = b''
 while len(data) < size:
  part = os.read(config_fd, size - len(data))
  if not part: raise RuntimeError('incomplete config')
  data += part
 return data
size = struct.unpack('!I', read_exact(4))[0]
config = json.loads(read_exact(size))
os.close(config_fd)
boot = uuid.uuid4().hex
health = dict(ready=True, protocol_version='2', boot_id=boot,
 allowed_artifact_root=config['allowed_artifact_root'],
 policy_digest=hashlib.sha256(json.dumps(config['policy'],sort_keys=True,separators=(',',':')).encode()).hexdigest(),
 capabilities=dict(materialization=['artifact_root'],attests=[],instantiable=True,**config['limits']),
 instances=[], **config['identity'])
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def do_GET(self):
  payload=json.dumps(health).encode()
  self.send_response(200)
  self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(payload)))
  self.end_headers()
  self.wfile.write(payload)
 def log_message(self,*args): pass
endpoint=urlsplit(config['base_url'])
server=HTTPServer((endpoint.hostname,endpoint.port),Handler)
thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
thread.start()
stopped=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stopped.set())
signal.signal(signal.SIGINT,lambda *_:stopped.set())
print(json.dumps({'executable':sys.executable,'lab_present':importlib.util.find_spec('evolution_lab') is not None}),flush=True)
ready=json.dumps(dict(schema='lab-target-ready/1',launch_id=config['launch_id'],
 launch_digest=config['launch_digest'],base_url=config['base_url'],boot_id=boot)).encode()
os.write(ready_fd,struct.pack('!I',len(ready))+ready)
os.close(ready_fd)
stopped.wait()
server.shutdown()
server.server_close()
thread.join()
'''
    state, bundle, _ = make_bundle(tmp_path,
        target_environment=EnvironmentInput(target_python, (CORE_WHEEL,), ()),
        launcher_source=launcher)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            runtime = owner._targets[target.id]
            assert runtime.helper.process.args[0] == sys.executable
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
            scope = state / 'scopes' / runtime.scope_id
            child = json.loads((scope / 'stdout.log').read_text())
            assert child == {'executable': str(target_python), 'lab_present': False}
            receipt = read_cleanup_receipt(scope, runtime.scope_id, owner.owner_generation)
            assert receipt['schema'] == 'lab-scope-cleanup/2'
            assert receipt['child_launched'] is True and receipt['forced'] is False
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


async def ready_target(owner, bundle):
    connection = await owner.bind(bundle.digest)
    task = await owner.start_target(connection, secrets={'TARGET_TOKEN': 'private-neutral-token'})
    status = await asyncio.wait_for(owner.wait(task), 8)
    assert status.state == 'succeeded', json.dumps(status.to_payload())
    from evolution_lab.owner import TargetRef
    return connection, TargetRef(status.ref.id, owner.owner_generation), task


def test_owner_marker_token_is_absent_from_durable_state(tmp_path, ownership_mode):
    from evolution_lab.owner import Owner
    token_path = tmp_path / 'observed-token'
    launcher = (Path(__file__).parent / 'owner_fixtures/launcher.py').read_text()
    launcher = launcher.replace('import os\n',
        'import os\n' + f"open({str(token_path)!r},'w').write(os.environ.get('EVOLUTION_LAB_SCOPE',''))\n", 1)
    state, bundle, _ = make_bundle(tmp_path, launcher_source=launcher)
    async def run():
        async with Owner.open(state) as owner:
            assert owner.ownership == ownership_mode
            _, target, _ = await ready_target(owner, bundle)
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
    asyncio.run(run())
    token = token_path.read_bytes()
    if ownership_mode == 'subreaper':
        assert token == b''
    else:
        assert len(token) == 32
        for path in state.rglob('*'):
            if path.is_file():
                assert token not in path.read_bytes(), path.relative_to(state)


@pytest.mark.usefixtures('ownership_mode')
def test_owner_check_admits_a_manifest_16_project(tmp_path):
    """Owner check admits /16 (spec D2); public H1 ``connect`` stays /15-only (test_scaffold_manifest)."""
    from evolution_lab.owner import Owner
    from scaffold_16 import scaffold_16
    state, bundle, _ = make_bundle(tmp_path, scaffold=scaffold_16)
    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            checked = await owner.wait(await owner.check(connection, target_ref=target))
            assert checked.state == 'succeeded', checked.to_payload()
            assert checked.result.kind == 'check'
            await owner.wait(await owner.stop_target(target))
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('scratch_enabled', [False, True])
def test_real_target_readiness_reuse_explicit_check_and_shutdown(tmp_path, scratch_enabled):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, port = make_bundle(tmp_path)
    scratch = tmp_path / 'pair' if scratch_enabled else None
    async def scenario():
        async with Owner.open(state, scratch_directory=scratch) as owner:
            connection, target, startup = await ready_target(owner, bundle)
            value = await owner.target(target)
            assert value.state == 'ready' and value.boot_id
            assert value.health['allowed_artifact_root'] == str(state / 'artifacts')
            temporary = state if scratch is None else scratch
            assert (temporary / 'targets' / target.id).is_dir()
            assert (temporary / 'snapshots' / target.id).is_dir()
            if scratch is not None:
                assert not (state / 'targets' / target.id).exists()
                assert not (state / 'snapshots' / target.id).exists()
            assert value.health['boot_id'] == value.boot_id
            repeat = await owner.start_target(connection, secrets={'TARGET_TOKEN': 'private-neutral-token'})
            assert repeat != startup
            assert (await owner.wait(repeat)).ref.id == target.id
            with pytest.raises(OwnerError) as caught:
                await owner.start_target(connection, secrets={'TARGET_TOKEN': 'changed'})
            assert caught.value.code == 'binding_mismatch'
            check = await owner.check(connection, target_ref=target)
            checked = await owner.wait(check)
            assert checked.state == 'succeeded', checked.to_payload()
            assert checked.result.kind == 'check'
            assert (await owner.target(target)).state == 'ready'
            stop = await owner.stop_target(target)
            assert (await owner.wait(stop)).state == 'succeeded'
            assert (await owner.target(target)).cleanup.complete is True
            assert (temporary / 'targets' / target.id).exists() is (not scratch_enabled)
            assert (temporary / 'snapshots' / target.id).exists() is (not scratch_enabled)
            assert not (await owner.health()).blockers
            assert (await owner.close()).cleanup.complete is True
    asyncio.run(scenario())
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', port)) != 0


@pytest.mark.usefixtures('ownership_mode')
def test_ready_target_of_another_bundle_refuses_as_slot_held(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    from test_owner_probe_bundle import make_probe_bundle
    state, bundle, _ = make_bundle(tmp_path)
    _, probe_digest = make_probe_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            other = await owner.bind(probe_digest)
            with pytest.raises(OwnerError) as caught:
                await owner.start_target(other, secrets={"TARGET_TOKEN": "private-probe-token"})
            assert (caught.value.code, caught.value.stage) == ("target_slot_held", "before_start")
            with pytest.raises(OwnerError) as same_bundle:
                await owner.start_target(connection, secrets={"TARGET_TOKEN": "changed"})
            assert same_bundle.value.code == "binding_mismatch"
            stop = await owner.stop_target(target)
            assert (await owner.wait(stop)).state == "succeeded"
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_real_target_scratch_removal_failure_keeps_success_and_retries_on_open(tmp_path, caplog):
    from evolution_lab.owner import Owner
    state, bundle, _ = make_bundle(tmp_path)
    pair = tmp_path / 'pair'

    async def scenario():
        async with Owner.open(state, scratch_directory=pair) as owner:
            _, target, _ = await ready_target(owner, bundle)
            work = pair / 'targets' / target.id
            snapshots = pair / 'snapshots' / target.id
            (pair / 'targets').chmod(0o500)
            try:
                stopped = await owner.wait(await owner.stop_target(target))
                assert stopped.state == 'succeeded' and stopped.failure is None
                status = await owner.target(target)
                assert status.state == 'stopped' and status.cleanup.complete is True
                assert work.exists() and not snapshots.exists()
                assert len([record for record in caplog.records if record.name == 'evolution_lab.owner.scratch']) == 1
            finally:
                (pair / 'targets').chmod(0o700)
        async with Owner.open(state, scratch_directory=pair) as reopened:
            assert (await reopened.target(target)).state == 'stopped'
            assert not work.exists() and not snapshots.exists()

    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('mode', ['wrong_boot', 'extra_ready'])
def test_invalid_ready_cannot_promote_target_and_cleanup_releases_slot(tmp_path, mode):
    from evolution_lab.owner import Owner
    state, bundle, _ = make_bundle(tmp_path, mode=mode)
    async def scenario():
        async with Owner.open(state) as owner:
            task = await owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'x'})
            result = await owner.wait(task)
            assert result.state == 'failed'
            assert result.failure.code == 'target_mismatch'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_foreign_listening_port_is_never_adopted(tmp_path):
    from evolution_lab.owner import Owner
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', 0))
        foreign.listen()
        state, bundle, _ = make_bundle(tmp_path, port=foreign.getsockname()[1])
        async def scenario():
            async with Owner.open(state) as owner:
                task = await owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'x'})
                result = await owner.wait(task)
                assert result.state == 'failed'
                assert not (await owner.health()).blockers
                assert foreign.fileno() >= 0
        asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_explicit_stop_startup_and_wait_cancellation(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, _ = make_bundle(tmp_path, mode='stall')
    async def scenario():
        async with Owner.open(state) as owner:
            connection = await owner.bind(bundle.digest)
            task = await owner.start_target(connection, secrets={'TARGET_TOKEN': 'x'})
            with pytest.raises(OwnerError) as caught:
                await owner.start_target(connection, secrets={'TARGET_TOKEN': 'x'})
            assert caught.value.code == 'busy'
            waiter = asyncio.create_task(owner.wait(task))
            await asyncio.sleep(.05)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            receipt = await owner.stop(task)
            assert receipt.effect == 'requested'
            assert (await owner.stop(task)).effect in {'already_requested', 'too_late'}
            result = await owner.wait(task)
            assert result.state == 'failed'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_caller_cancellation_during_offloop_commit_retains_owned_admission(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, TargetRef
    from evolution_lab.owner.store import OwnerStateStore
    state, bundle, _ = make_bundle(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = OwnerStateStore.accept_target_start
    def delayed(self, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, **kwargs)
    monkeypatch.setattr(OwnerStateStore, 'accept_target_start', delayed)
    async def scenario():
        async with Owner.open(state) as owner:
            caller = asyncio.create_task(owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'x'}))
            assert await asyncio.to_thread(entered.wait, 3)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            for _ in range(400):
                health = await owner.health()
                if health.blockers:
                    ref = health.blockers[0]
                    target = TargetRef(ref.id, owner.owner_generation)
                    if (await owner.target(target)).state == 'ready':
                        break
                await asyncio.sleep(.01)
            else:
                pytest.fail('accepted target never became discoverable and ready')
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('scratch_enabled', [False, True])
def test_owner_process_death_cleans_target_but_new_owner_retains_orphan(tmp_path, scratch_enabled):
    from evolution_lab.owner import Owner, OwnerError, TargetRef
    from evolution_lab.owner.receipts import read_cleanup_receipt
    state, bundle, port = make_bundle(tmp_path)
    scratch = tmp_path / 'pair' if scratch_enabled else None
    async def scenario():
        script = '''import asyncio, json, sys
from pathlib import Path
from evolution_lab.owner import Owner
async def main():
    scratch = Path(sys.argv[3]) if sys.argv[3] else None
    async with Owner.open(sys.argv[1], scratch_directory=scratch) as owner:
        task = await owner.start_target(await owner.bind(sys.argv[2]), secrets={'TARGET_TOKEN':'x'})
        result = await owner.wait(task)
        print(json.dumps(result.to_payload()), flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', script,
            str(state), bundle.digest, str(scratch) if scratch else '', stdout=asyncio.subprocess.PIPE)
        try:
            result = json.loads(await asyncio.wait_for(process.stdout.readline(), 8))
            assert result['state'] == 'succeeded', result
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        scope = next((state / 'scopes').iterdir())
        for _ in range(400):
            if (scope / 'cleanup.receipt').exists():
                break
            await asyncio.sleep(.01)
        receipt = read_cleanup_receipt(scope, scope.name, result['owner_generation'])
        assert receipt['process_tree_empty'] is True
        target = TargetRef(result['ref']['id'], result['owner_generation'])
        temporary = state if scratch is None else scratch
        paths = tuple(temporary / name / target.id for name in ('targets', 'snapshots'))
        assert all(path.is_dir() for path in paths)
        async with Owner.open(state, scratch_directory=scratch) as owner:
            assert (await owner.health()).blockers
            assert (await owner.target(target)).state == 'cleanup_required'
            assert all(path.is_dir() for path in paths)
            with pytest.raises(OwnerError) as caught:
                await owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'x'})
            assert caught.value.code == 'cleanup_required'
            cleaned = await owner.wait(await owner.cleanup(target, wait_seconds=8))
            assert cleaned.state == 'succeeded'
            assert (await owner.target(target)).state == 'stopped'
            assert all(path.exists() is (not scratch_enabled) for path in paths)
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_check_holds_use_reservation_and_stop_only_cancels_observation(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    from evolution.tree.target.http_adapter import HttpTransport
    state, bundle, _ = make_bundle(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = HttpTransport._request
    # Signature-agnostic: core a10 threads the ledger call and the serialized
    # payload through this private worker, a9 took only (method, path, body).
    def held_get(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(HttpTransport, '_request', held_get)
            checking = await owner.check(connection, target_ref=target)
            assert await asyncio.to_thread(entered.wait, 3)
            with pytest.raises(OwnerError) as caught:
                await owner.stop_target(target)
            assert caught.value.code == 'busy'
            with pytest.raises(OwnerError) as caught:
                await owner.check(connection, target_ref=target)
            assert caught.value.code == 'busy'
            await owner.stop(checking)
            assert (await owner.wait(checking)).state == 'failed'
            assert (await owner.target(target)).state == 'ready'
            release.set()
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.usefixtures('ownership_mode')
def test_missing_receipt_keeps_lifetime_blocker_after_real_process_exit(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    import evolution_lab.owner.target as target_module
    state, bundle, _ = make_bundle(tmp_path)
    def absent(*args):
        raise FileNotFoundError('fixture receipt unavailable')
    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(target_module, 'read_cleanup_receipt', absent)
            stopped = await owner.wait(await owner.stop_target(target))
            assert stopped.state == 'failed'
            assert (await owner.target(target)).cleanup.complete is None
            assert (await owner.health()).blockers
            with pytest.raises(OwnerError):
                await owner.start_target(connection, secrets={'TARGET_TOKEN': 'private-neutral-token'})
    asyncio.run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_short_close_releases_lock_and_returns_unconfirmed_without_killing_helper(tmp_path):
    from evolution_lab.owner import Owner
    state, bundle, _ = make_bundle(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            await ready_target(owner, bundle)
            result = await owner.close(wait_seconds=.000001)
            assert result.cleanup.complete is None
            assert result.blockers
            assert await owner.close() == result
            async with Owner.open(state) as reopened:
                assert (await reopened.health()).blockers
            for _ in range(400):
                if list((state / 'scopes').glob('*/cleanup.receipt')):
                    break
                await asyncio.sleep(.01)
            assert list((state / 'scopes').glob('*/cleanup.receipt'))
    asyncio.run(scenario())


async def drive_one_turn(state, port, content):
    """§6.4: one real turn over the target's HTTP address, core's own client.

    Mirrors the materialize/open/send steps `evolution_lab.standalone.evaluate_target`
    takes over `HttpSessionAdapter` (the core client this file's other tests already
    monkeypatch through `HttpTransport`), trimmed to the one turn this test needs.
    """
    from evolution.tree import (ArtifactPolicyV1, ArtifactRef, ExpectedAttestation,
        HttpSessionAdapter, Message, TargetBinding, new_instance_id)
    from evolution.tree.artifacts.artifact_policy import scan_tree
    # The scaffolded manifest's own artifact policy (reference_target_cli.scaffold):
    # a `uv.lock` dependency and a `skills/**` writable path; the target refuses any
    # other policy digest.
    policy = ArtifactPolicyV1(('build/**',), ('uv.lock',), ('skills/**',))
    candidate_root = state / 'artifacts' / 'turn-candidate'
    candidate_root.mkdir(parents=True)
    (candidate_root / 'uv.lock').write_text('# candidate dependency\n')
    scan = scan_tree(candidate_root, policy)
    candidate = ArtifactRef('candidate', 'turn-candidate', candidate_root,
        scan.candidate_digest, scan.dependency_digest, policy)
    binding = TargetBinding(f'http://127.0.0.1:{port}', 'exact', 'a' * 64, 'b' * 64)
    adapter = HttpSessionAdapter(binding, policy, run_id='task8-turn')
    await adapter.preflight(ExpectedAttestation(scan.candidate_digest, scan.dependency_digest))
    instance_id = new_instance_id('task8-turn')
    instance = await adapter.materialize(candidate, 'artifact_root_injection', instance_id)
    try:
        session = await instance.open({}, 1, uuid.uuid4().hex)
        try:
            return await instance.send(session, Message(content), uuid.uuid4().hex)
        finally:
            await instance.close(session)
    finally:
        await adapter.release(instance_id)


@pytest.mark.usefixtures('ownership_mode')
def test_launch_v2_target_starts_matches_health_and_stops(tmp_path):
    from evolution_lab.owner import Owner
    state, bundle, port = make_bundle(tmp_path, launch_schema=2)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            value = await owner.target(target)
            assert value.state == 'ready' and value.boot_id
            result = await drive_one_turn(state, port, 'ping')
            assert result.events[-1] == {'type': 'assistant_message', 'content': 'echo:ping'}
            assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
            assert (await owner.target(target)).state == 'stopped'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


def test_runtime_config_v2_carries_agent_and_secret_names(tmp_path):
    from types import SimpleNamespace
    from evolution_lab.owner.target import runtime_config, target_argv
    launch = dict(schema='lab-target-launch/2', endpoint={'base_url': 'http://x'}, identity={}, limits={},
        agent={'module': 'echo_agent', 'attribute': 'build', 'import_root': 'inputs/project/agent'},
        secret_names=['TARGET_TOKEN'])
    binding = SimpleNamespace(launch=launch, launch_digest='d', bundle_digest='b',
        bundle_path=tmp_path / 'bundle', target_environment=SimpleNamespace(executable='/venv/bin/python'),
        manifest=SimpleNamespace(policy=SimpleNamespace(to_payload=lambda: {})))
    config = runtime_config(tmp_path, binding, SimpleNamespace(id='t1'))
    assert config['schema'] == 'lab-target-runtime/2'
    assert config['agent'] == {'module': 'echo_agent', 'attribute': 'build',
        'import_root': str(tmp_path / 'bundle' / 'inputs/project/agent')}
    assert config['secret_names'] == ['TARGET_TOKEN']
    assert target_argv(binding) == ['/venv/bin/python', '-I', '-m', 'evolution_lab.target.launch']
