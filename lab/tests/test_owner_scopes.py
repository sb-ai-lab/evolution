"""V-S2-2: installed helper ownership and immutable cleanup evidence."""
import asyncio
import json
import os
from pathlib import Path
import signal
import struct
import sys
import time
import uuid

import pytest
import psutil
from platforms import linux_only

from ownership_modes import ownership_mode  # noqa: F401

from evolution_lab.owner.protocol import HelperHandle, encode_frame, make_command
from evolution_lab.owner.receipts import (
    ReceiptError, publish_json, read_json, read_cleanup_receipt,
    publish_cleanup_request, validate_cleanup_receipt,
)


def scope(tmp_path):
    sid, generation = uuid.uuid4().hex, uuid.uuid4().hex
    directory = tmp_path / sid
    directory.mkdir(mode=0o700)
    return directory, sid, generation


async def finish(handle):
    handle.close_commands()
    assert await handle.wait(5) is not None
    handle.close_events()
    handle.close_target_ready()


@pytest.mark.usefixtures('ownership_mode')
def test_helper_writes_v2_receipt_with_ownership(tmp_path, ownership_mode):
    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            await handle.configure(argv=[sys.executable, '-I', '-c', 'raise SystemExit(7)'], env={}, stop_grace_seconds=.2)
            await handle.wait_event('ready', 5)
            await handle.go()
            assert await handle.wait(5) == 0
            receipt = read_cleanup_receipt(directory, sid, generation)
            assert receipt == dict(schema='lab-scope-cleanup/2', ownership=ownership_mode, scope_id=sid,
                owner_generation=generation, child_launched=True, child_exit_code=7,
                process_tree_empty=True, forced=False, spawn_closed=True)
            assert {'ready', 'child_started', 'child_exited', 'tree_empty'} <= {e['kind'] for e in handle.events}
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('intent', ['cancel', 'stop', 'eof', 'request', 'force', 'partial_config', 'partial_action'])
def test_pre_go_cleanup_closes_spawn_admission(tmp_path, intent):
    async def run():
        directory, sid, generation = scope(tmp_path)
        marker = directory / 'spawned'
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            if intent == 'partial_config':
                os.write(handle.command_fd, struct.pack('!I', 1000) + b'{')
                publish_cleanup_request(directory, sid, generation)
            elif intent == 'partial_action':
                await handle.configure(argv=[sys.executable, '-c', 'pass'], env={}, stop_grace_seconds=.2)
                await handle.wait_event('ready', 5)
                os.write(handle.command_fd, struct.pack('!I', 1000) + b'{')
            else:
                await handle.configure(argv=[sys.executable, '-c', f'open({str(marker)!r}, "w").close()'], env={}, stop_grace_seconds=.2)
                await handle.wait_event('ready', 5)
                if intent == 'eof':
                    handle.close_commands()
                elif intent == 'request':
                    first = publish_cleanup_request(directory, sid, generation)
                    assert publish_cleanup_request(directory, sid, generation) == first
                else:
                    await getattr(handle, intent)()
            assert await handle.wait(5) == 0
            receipt = read_cleanup_receipt(directory, sid, generation)
            assert receipt['child_launched'] is False
            assert receipt['spawn_closed'] is True
            assert receipt['forced'] is (intent == 'force')
            assert not marker.exists()
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
def test_eof_during_partial_configuration(tmp_path):
    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        os.write(handle.command_fd, struct.pack('!I', 900000) + b'{')
        await finish(handle)
        assert read_cleanup_receipt(directory, sid, generation)['child_launched'] is False
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('child_exits', [False, True])
def test_reaps_child_and_detached_grandchild_even_without_events(tmp_path, child_exits, ownership_mode):
    async def run():
        directory, sid, generation = scope(tmp_path)
        # A session-detached grandchild must remain owned after its parent exits.
        script = '''import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid = os.fork()
if pid == 0:
 os.setsid()
 open('grandchild.pid', 'w').write(str(os.getpid()))
 while True: time.sleep(.05)
while not os.path.exists('grandchild.pid'): time.sleep(.01)
open('child.ready', 'w').close()
'''+ ('raise SystemExit(0)\n' if child_exits else 'while True: time.sleep(.05)\n')
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            await handle.configure(argv=[sys.executable, '-I', '-c', script], env={}, cwd=str(directory), stop_grace_seconds=.25)
            await handle.wait_event('ready', 5)
            await handle.go()
            await handle.wait_event('child_started', 5)
            end = time.monotonic() + 3
            while not (directory / 'child.ready').exists() and time.monotonic() < end:
                await asyncio.sleep(.01)
            assert (directory / 'child.ready').exists()
            grandchild = int((directory / 'grandchild.pid').read_text())
            handle.close_events()
            if not child_exits:
                await handle.stop()
                assert await handle.wait(.03) is None  # wait does not kill helper
                await handle.stop()  # must not restart helper grace
            assert await handle.wait(5) == 0
            receipt = read_cleanup_receipt(directory, sid, generation)
            assert receipt['forced'] is True
            assert receipt['process_tree_empty'] is True
            assert receipt['child_exit_code'] == (0 if child_exits else -signal.SIGKILL)
            if ownership_mode == 'subreaper':
                assert not psutil.pid_exists(grandchild)
            else:
                try:
                    status = psutil.Process(grandchild).status()
                except psutil.NoSuchProcess:
                    pass
                else:
                    assert status == psutil.STATUS_ZOMBIE
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
@linux_only('lists child descriptors through the Linux kernel filesystem')
@pytest.mark.parametrize('role,fd_name', [('target', 'LAB_TARGET_CONFIG_FD'), ('campaign', 'LAB_CAMPAIGN_CONFIG_FD')])
def test_child_config_and_descriptor_environment_isolation(tmp_path, monkeypatch, role, fd_name, ownership_mode):
    monkeypatch.setenv('PRIVATE_AMBIENT_SECRET', 'must-not-leak')
    async def run():
        directory, sid, generation = scope(tmp_path)
        marker = directory / 'child.json'
        script = '''import json, os
from evolution_lab.owner.protocol import read_frame
name = %r
fd = int(os.environ[name])
value = read_frame(fd)
os.close(fd)
fds = []
for n in os.listdir('/proc/self/fd'):
 try: fds.append(os.readlink('/proc/self/fd/'+n))
 except FileNotFoundError: pass
json.dump({'env': dict(os.environ), 'value': value, 'fds': fds}, open(%r, 'w'))
if 'LAB_TARGET_READY_FD' in os.environ:
 os.write(int(os.environ['LAB_TARGET_READY_FD']), b'ready')
''' % (fd_name, str(marker))
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            command_link = os.readlink(f'/proc/self/fd/{handle.command_fd}')
            await handle.configure(argv=[sys.executable, '-I', '-c', script], env={'ALLOWED': 'yes'}, role=role,
                child_config={'schema': 'neutral/1', 'data': 'x' * 100000}, stop_grace_seconds=.2)
            await handle.wait_event('ready', 5)
            await handle.go()
            assert await handle.wait(5) == 0
            result = json.loads(marker.read_text())
            assert 'PRIVATE_AMBIENT_SECRET' not in result['env']
            assert result['env']['ALLOWED'] == 'yes'
            assert result['value']['data'] == 'x' * 100000
            assert command_link not in result['fds']
            if role == 'target':
                assert os.read(handle.target_ready_fd, 99) == b'ready'
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.parametrize('version', ['1', '2'])
def test_closed_bounded_receipts_and_no_overwrite_or_symlinks(tmp_path, version):
    directory, sid, generation = scope(tmp_path)
    receipt = dict(schema=f'lab-scope-cleanup/{version}', scope_id=sid, owner_generation=generation,
        child_launched=False, child_exit_code=None, process_tree_empty=True, forced=False, spawn_closed=True)
    if version == '2':
        receipt['ownership'] = 'group_tree_marker'
    path = directory / 'cleanup.receipt'
    publish_json(path, receipt)
    assert read_cleanup_receipt(directory, sid, generation) == receipt | {'ownership': receipt.get('ownership', 'subreaper')}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        publish_json(path, receipt)
    for delta in [{'unexpected': 1}, {'forced': 1}, {'child_exit_code': True}, {'process_tree_empty': False}, {'spawn_closed': False}]:
        with pytest.raises(ReceiptError):
            validate_cleanup_receipt(receipt | delta, sid, generation)
    with pytest.raises(ReceiptError):
        read_cleanup_receipt(directory, uuid.uuid4().hex, generation)
    for raw in [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'x' * 20000]:
        p = directory / uuid.uuid4().hex
        p.write_bytes(raw)
        with pytest.raises(ReceiptError):
            read_json(p, max_bytes=16384)
    alias = tmp_path / 'alias'
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        read_json(alias / 'cleanup.receipt')
    symlink = directory / 'symlink'
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        read_json(symlink)


@pytest.mark.usefixtures('ownership_mode')
def test_duplicate_config_and_go_cannot_spawn_twice(tmp_path):
    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            args = dict(argv=[sys.executable, '-c', 'pass'], env={}, stop_grace_seconds=.2)
            await handle.configure(**args)
            await handle.wait_event('ready', 5)
            await handle.configure(**args)
            assert await handle.wait(5) == 0
            assert read_cleanup_receipt(directory, sid, generation)['child_launched'] is False
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize('cancel_writer', [False, True])
def test_stalled_config_writer_is_woken_by_channel_close_or_cancellation(tmp_path, cancel_writer):
    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        # Freeze before CONFIG to fill the real command pipe without a consumer.
        handle.process.send_signal(signal.SIGSTOP)
        writing = asyncio.create_task(handle.configure(argv=[sys.executable, '-c', 'pass'],
            env={'DATA': 'x' * 500000}, stop_grace_seconds=.1))
        try:
            await asyncio.sleep(.05)
            assert not writing.done()
            if cancel_writer:
                writing.cancel()
            else:
                handle.close_commands()
            done, _ = await asyncio.wait({writing}, timeout=.5)
            assert writing in done, 'channel close must wake a blocked CONFIG writer'
            with pytest.raises((asyncio.CancelledError, ConnectionError, OSError)):
                await writing
            assert handle.command_fd is None
        finally:
            writing.cancel()
            handle.process.send_signal(signal.SIGCONT)
            await finish(handle)
        assert read_cleanup_receipt(directory, sid, generation)['child_launched'] is False
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
def test_stalled_child_config_still_observes_stop_and_reaps(tmp_path):
    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            await handle.configure(argv=[sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                env={}, role='campaign', child_config={'large': 'x' * 500000}, stop_grace_seconds=.1)
            await handle.wait_event('ready', 5)
            await handle.go()
            await handle.wait_event('child_started', 5)
            publish_cleanup_request(directory, sid, generation)
            assert await handle.wait(5) == 0
            assert read_cleanup_receipt(directory, sid, generation)['child_launched'] is True
        finally:
            await finish(handle)
    asyncio.run(run())


@pytest.mark.usefixtures('ownership_mode')
def test_repeated_stop_does_not_extend_grace_and_explicit_force_is_sticky(tmp_path, ownership_mode):
    async def run(force):
        directory, sid, generation = scope(tmp_path)
        marker = directory / 'ready'
        script = f'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(marker)!r}, "w").close(); time.sleep(60)'
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            await handle.configure(argv=[sys.executable, '-I', '-c', script], env={}, stop_grace_seconds=.2)
            await handle.wait_event('ready', 5)
            await handle.go()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            assert marker.exists()
            start = time.monotonic()
            if force:
                await handle.force()
            else:
                await handle.stop()
                while handle.process.poll() is None and time.monotonic() - start < .5:
                    await asyncio.sleep(.04)
                    try:
                        await handle.stop()
                    except (ConnectionError, OSError):
                        break
            # The new mode performs two full sweeps; retain the same total bound.
            assert await handle.wait(.5 if ownership_mode == 'group_tree_marker' else .1) == 0
            assert time.monotonic() - start < .5
            assert read_cleanup_receipt(directory, sid, generation)['forced'] is True
        finally:
            await finish(handle)
    asyncio.run(run(False))
    asyncio.run(run(True))


@pytest.mark.usefixtures('ownership_mode')
def test_late_real_helper_receipt_does_not_release_new_store_scope(tmp_path):
    from evolution_lab.owner import ResourceRef
    from evolution_lab.owner.store import OwnerStateStore
    async def run():
        state = tmp_path / 'state'
        with OwnerStateStore.open(state) as store:
            def accept():
                connection = store.bind_connection(bundle_digest='0' * 64, launch_digest='1' * 64, source_kind='campaign')
                return store.accept_target_start(connection_id=connection.id, launch_digest='1' * 64,
                    bundle_digest='0' * 64, base_url='http://127.0.0.1:8123')
            old = accept()
            directory = state / 'scopes' / old.scope_id
            directory.mkdir(mode=0o700)
            helper = HelperHandle.launch(sys.executable, directory, old.scope_id, store.generation)
            try:
                store.mark_target_cleanup_required(old.target_ref.id)
                store.release_after_manual_cleanup(ResourceRef('target', old.target_ref.id), reason='operator assertion')
                new = accept()
                before = store.reservations()
                helper.close_commands()
                assert await helper.wait(5) == 0
                receipt = read_cleanup_receipt(directory, old.scope_id, store.generation)
                store.confirm_scope_cleanup(old.scope_id, store.generation, forced=receipt['forced'])
                assert store.reservations() == before
                assert store.scope_state(new.scope_id) == 'active'
                assert store.target_status(new.target_ref.id).state == 'starting'
            finally:
                await finish(helper)
    asyncio.run(run())


def test_v1_receipt_reads_as_subreaper(tmp_path):
    directory, sid, generation = scope(tmp_path)
    value = dict(schema='lab-scope-cleanup/1', scope_id=sid, owner_generation=generation,
        child_launched=False, child_exit_code=None, process_tree_empty=True, forced=False, spawn_closed=True)
    publish_json(directory / 'cleanup.receipt', value)
    assert read_cleanup_receipt(directory, sid, generation) == value | {'ownership': 'subreaper'}
    assert 'ownership' not in value


@pytest.mark.parametrize('version,extra', [
    ('2', {'ownership': 'other'}), ('2', {}), ('1', {'ownership': 'subreaper'}),
    ('2', {'ownership': []}), ('2', {'ownership': None}),
])
def test_unknown_receipt_ownership_is_refused(tmp_path, version, extra):
    _, sid, generation = scope(tmp_path)
    value = dict(schema=f'lab-scope-cleanup/{version}', scope_id=sid, owner_generation=generation,
        child_launched=False, child_exit_code=None, process_tree_empty=True, forced=False, spawn_closed=True)
    with pytest.raises(ReceiptError):
        validate_cleanup_receipt(value | extra, sid, generation)


@pytest.mark.parametrize('mode', ['subreaper', 'group_tree_marker'])
@pytest.mark.parametrize('launched,code,forced', [(False, None, False), (True, 7, False), (True, -9, True)])
def test_receipt_consumers_keep_process_facts(tmp_path, mode, launched, code, forced):
    _, sid, generation = scope(tmp_path)
    value = dict(schema='lab-scope-cleanup/2', ownership=mode, scope_id=sid,
        owner_generation=generation, child_launched=launched, child_exit_code=code,
        process_tree_empty=True, forced=forced, spawn_closed=True)
    result = validate_cleanup_receipt(value, sid, generation)
    assert result == value
    assert result is not value


def test_helper_pipes_are_not_inheritable(tmp_path, monkeypatch, ownership_mode):
    def unavailable(*args, **kwargs):
        raise AttributeError('pipe2 is unavailable')
    monkeypatch.setattr(os, 'pipe2', unavailable, raising=False)

    async def run():
        directory, sid, generation = scope(tmp_path)
        handle = HelperHandle.launch(sys.executable, directory, sid, generation)
        try:
            assert all(not os.get_inheritable(fd) for fd in (
                handle.command_fd, handle.event_fd, handle.target_ready_fd))
        finally:
            await finish(handle)
    asyncio.run(run())


async def test_atomic_frames_use_pc_pipe_buf(tmp_path, monkeypatch):
    from evolution_lab.owner.protocol import ProtocolError, make_event
    directory, sid, generation = scope(tmp_path)
    read_fd, write_fd = os.pipe()
    handle = HelperHandle.__new__(HelperHandle)
    handle.command_fd = write_fd
    handle._write_lock = asyncio.Lock()
    try:
        for action in ('GO', 'CANCEL', 'STOP', 'FORCE'):
            message = make_command(sid, generation, action)
            await handle._send(message, atomic=True)
            assert os.read(read_fd, 4096) == encode_frame(message)
        original = os.fpathconf
        monkeypatch.setattr(os, 'fpathconf', lambda fd, name: 512 if name == 'PC_PIPE_BUF' else original(fd, name))
        message = make_command(sid, generation, 'STOP')
        await handle._send(message, atomic=True)
        assert os.read(read_fd, 512) == encode_frame(message)
        padded = message | {'padding': 'x' * 600}
        assert 512 < len(encode_frame(padded)) < 4096
        with pytest.raises(ProtocolError, match='action exceeds PIPE_BUF'):
            await handle._send(padded, atomic=True)
        event = make_event(sid, generation, 'child_exited', {'exit_code': -(2 ** 63)})
        assert len(encode_frame(event)) < 512
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_atomic_events_use_pc_pipe_buf(tmp_path, monkeypatch):
    from evolution_lab.owner import _helper
    _, sid, generation = scope(tmp_path)
    read_fd, write_fd = os.pipe()
    original = os.fpathconf
    monkeypatch.setattr(os, 'fpathconf', lambda fd, name: 512 if name == 'PC_PIPE_BUF' else original(fd, name))
    helper = _helper.ScopeHelper(0, write_fd, 0, 0, sid, generation, 'subreaper')
    monkeypatch.setattr(_helper, 'encode_frame', lambda message: b'x' * 600)
    try:
        helper.event('ready')
        assert helper.event_fd is None
        assert os.read(read_fd, 1024) == b''
    finally:
        os.close(read_fd)
        if helper.event_fd is not None:
            os.close(helper.event_fd)
