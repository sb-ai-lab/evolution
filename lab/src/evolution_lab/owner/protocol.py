"""Private S2 /1 framing and parent helper handle (stdlib only)."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import struct
import subprocess

from .._ownership import GROUP_TREE_MARKER, SUBREAPER, MARKER_NAME, select_mode
from .receipts import (ReceiptError, decode_json, encode_json,
    open_directory_nofollow, validate_id)

from evolution_lab.launch_env import with_launch_values


COMMAND_SCHEMA = 'lab-scope-command/1'
EVENT_SCHEMA = 'lab-scope-event/1'
MAX_CONFIG_BYTES = 1024 * 1024
MAX_ACTION_BYTES = 4096  # Read/admission bound; atomic writes use PC_PIPE_BUF.
_HEADER = struct.Struct('!I')
_ACTIONS = {'GO', 'CANCEL', 'STOP', 'FORCE'}
_EVENTS = {'ready': set(), 'child_started': set(), 'spawn_skipped': set(),
    'spawn_failed': set(), 'child_exited': {'exit_code'}, 'stop_effect': {'value'},
    'force_started': set(), 'tree_empty': set()}
_RESERVED_ENV = {
    MARKER_NAME,
    'LAB_TARGET_CONFIG_FD', 'LAB_TARGET_READY_FD', 'LAB_CAMPAIGN_CONFIG_FD',
    'LAB_EVALUATION_CONFIG_FD', 'LAB_CONFORMANCE_CONFIG_FD', 'LAB_GATEWAY_CONFIG_FD',
}


class ProtocolError(ValueError):
    """Invalid or incomplete private protocol."""


class ChannelClosed(ConnectionError):
    """The helper channel no longer accepts frames."""


def encode_frame(payload, *, max_bytes=MAX_CONFIG_BYTES):
    try:
        raw = encode_json(payload, max_bytes=max_bytes)
    except ReceiptError as exc:
        raise ProtocolError('invalid framed JSON') from exc
    return _HEADER.pack(len(raw)) + raw


def read_frame(fd, *, max_bytes=MAX_CONFIG_BYTES):
    """Read exactly one bounded frame; intended for a child's private config FD."""
    def exact(size):
        result = bytearray()
        while len(result) < size:
            chunk = os.read(fd, size - len(result))
            if not chunk:
                raise ProtocolError('incomplete frame')
            result.extend(chunk)
        return result
    size = _HEADER.unpack(exact(4))[0]
    if not 0 < size <= max_bytes:
        raise ProtocolError('invalid frame length')
    try:
        return decode_json(exact(size), max_bytes=max_bytes)
    except ReceiptError as exc:
        raise ProtocolError('invalid framed JSON') from exc


class FrameReader:
    def __init__(self, max_bytes=MAX_CONFIG_BYTES):
        self.buffer = bytearray()
        self.max_bytes = max_bytes

    @property
    def incomplete(self):
        return bool(self.buffer)

    def read_available(self, fd):
        messages = []
        eof = False
        for _ in range(16):
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                eof = True
                break
            self.buffer.extend(chunk)
            while len(self.buffer) >= 4:
                size = _HEADER.unpack(self.buffer[:4])[0]
                if not 0 < size <= self.max_bytes:
                    raise ProtocolError('invalid frame length')
                if len(self.buffer) < size + 4:
                    break
                raw = self.buffer[4:size + 4]
                del self.buffer[:size + 4]
                try:
                    messages.append(decode_json(raw, max_bytes=self.max_bytes))
                except ReceiptError as exc:
                    raise ProtocolError('invalid framed JSON') from exc
        if eof and self.incomplete:
            raise ProtocolError('incomplete frame at EOF')
        return messages, eof


def _message(schema, scope_id, owner_generation, kind, data):
    return dict(schema=schema, scope_id=validate_id(scope_id),
        owner_generation=validate_id(owner_generation), kind=kind, data=data)


def make_command(scope_id, owner_generation, kind):
    if kind not in _ACTIONS:
        raise ProtocolError('invalid action')
    return _message(COMMAND_SCHEMA, scope_id, owner_generation, kind, {})


def make_config(scope_id, owner_generation, *, argv, env, stop_grace_seconds,
        role='process', child_config=None, cwd=None):
    data = dict(argv=list(argv), env=dict(env), stop_grace_seconds=stop_grace_seconds,
        role=role, child_config=child_config, cwd=cwd)
    message = _message(COMMAND_SCHEMA, scope_id, owner_generation, 'CONFIG', data)
    validate_command(message, scope_id, owner_generation)
    return message


def _validate_message(message, schema, scope_id, owner_generation):
    if not isinstance(message, dict) or set(message) != {'schema', 'scope_id', 'owner_generation', 'kind', 'data'}:
        raise ProtocolError('invalid message shape')
    if (message['schema'] != schema or message['scope_id'] != scope_id
            or message['owner_generation'] != owner_generation):
        raise ProtocolError('message identity mismatch')
    if not isinstance(message['kind'], str) or not isinstance(message['data'], dict):
        raise ProtocolError('invalid message fields')


def validate_command(message, scope_id, owner_generation):
    _validate_message(message, COMMAND_SCHEMA, scope_id, owner_generation)
    kind, data = message['kind'], message['data']
    if kind in _ACTIONS:
        if data or len(encode_frame(message)) > MAX_ACTION_BYTES:
            raise ProtocolError('invalid atomic action')
        return message
    if kind != 'CONFIG' or set(data) != {'argv', 'env', 'stop_grace_seconds', 'role', 'child_config', 'cwd'}:
        raise ProtocolError('invalid configuration shape')
    argv, env, grace = data['argv'], data['env'], data['stop_grace_seconds']
    if (not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv)
            or not os.path.isabs(argv[0])):
        raise ProtocolError('invalid executable argv')
    if not isinstance(env, dict) or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
            or k in _RESERVED_ENV or not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ProtocolError('invalid child environment')
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or grace <= 0:
        raise ProtocolError('invalid stop grace')
    if data['role'] not in ('process', 'target', 'campaign', 'evaluation', 'conformance', 'gateway'):
        raise ProtocolError('invalid child role')
    if data['cwd'] is not None and (not isinstance(data['cwd'], str) or not os.path.isabs(data['cwd']) or '\0' in data['cwd']):
        raise ProtocolError('invalid working directory')
    if data['role'] == 'process':
        if data['child_config'] is not None:
            raise ProtocolError('process role has no child configuration')
    elif not isinstance(data['child_config'], dict):
        raise ProtocolError('missing child configuration')
    encode_frame(message)
    return message


def make_event(scope_id, owner_generation, kind, data=None):
    message = _message(EVENT_SCHEMA, scope_id, owner_generation, kind, {} if data is None else data)
    return validate_event(message, scope_id, owner_generation)


def validate_event(message, scope_id, owner_generation):
    _validate_message(message, EVENT_SCHEMA, scope_id, owner_generation)
    kind, data = message['kind'], message['data']
    if kind not in _EVENTS or set(data) != _EVENTS[kind]:
        raise ProtocolError('invalid event')
    if kind == 'child_exited' and type(data['exit_code']) is not int:
        raise ProtocolError('invalid child exit')
    if kind == 'stop_effect' and data['value'] not in ('delivered', 'too_late', 'prevented_start'):
        raise ProtocolError('invalid stop effect')
    return message


async def _fd_ready(fd, *, still_open):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def wake():
        if not future.done():
            future.set_result(None)
    loop.add_reader(fd, wake)
    try:
        await future
    finally:
        if still_open():
            loop.remove_reader(fd)


class HelperHandle:
    """Parent channel ownership. A timeout never signals or kills the helper.

    launch must run on the owning event loop. Event draining starts immediately.
    CONFIG cancellation closes the command FD; callers should still finalize the
    scope using durable cleanup.receipt, never just helper exit or missing events.
    """

    def __init__(self, process, command_fd, event_fd, target_ready_fd, scope_id, owner_generation):
        self.process = process
        self.command_fd = command_fd
        self.event_fd = event_fd
        self.target_ready_fd = target_ready_fd
        self.scope_id = scope_id
        self.owner_generation = owner_generation
        self.events = []
        self.events_available = True
        self._event_queue = asyncio.Queue()
        self._changed = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._drain_task = asyncio.create_task(self._drain())

    @classmethod
    def launch(cls, executable, scope_directory, scope_id, owner_generation, *, ownership=None):
        asyncio.get_running_loop()
        ownership = select_mode() if ownership is None else ownership
        if ownership not in {SUBREAPER, GROUP_TREE_MARKER}:
            raise ProtocolError('unknown ownership mode')
        validate_id(scope_id)
        validate_id(owner_generation)
        if not os.path.isabs(executable):
            raise ProtocolError('helper executable must be absolute')
        directory_fd = open_directory_nofollow(scope_directory)
        opened = [directory_fd]
        try:
            command_read, command_write = os.pipe()
            opened.extend((command_read, command_write))
            event_read, event_write = os.pipe()
            opened.extend((event_read, event_write))
            ready_read, ready_write = os.pipe()
            opened.extend((ready_read, ready_write))
            process = subprocess.Popen([executable, '-I', '-m', 'evolution_lab.owner._helper',
                str(command_read), str(event_write), str(directory_fd), str(ready_write),
                scope_id, owner_generation, ownership], env=with_launch_values({}), close_fds=True,
                pass_fds=(command_read, event_write, directory_fd, ready_write),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for descriptor in (command_write, event_read, ready_read):
                os.set_blocking(descriptor, False)
            handle = cls(process, command_write, event_read, ready_read, scope_id, owner_generation)
            for descriptor in (command_write, event_read, ready_read):
                opened.remove(descriptor)
            return handle
        finally:
            for descriptor in opened:
                os.close(descriptor)

    async def _send(self, message, *, atomic=False):
        frame = encode_frame(message)
        async with self._write_lock:
            try:
                if self.command_fd is None:
                    raise ChannelClosed('command channel closed')
                if atomic and len(frame) > os.fpathconf(self.command_fd, 'PC_PIPE_BUF'):
                    raise ProtocolError('action exceeds PIPE_BUF')
                remaining = memoryview(frame)
                while remaining:
                    if self.command_fd is None:
                        raise ChannelClosed('command channel closed')
                    try:
                        count = os.write(self.command_fd, remaining)
                    except BlockingIOError:
                        # Recheck channel ownership after a bounded async yield.
                        # close_commands must wake a stalled CONFIG writer even
                        # when closing an FD does not wake an epoll waiter.
                        await asyncio.sleep(.01)
                        continue
                    remaining = remaining[count:]
                    if atomic and remaining:
                        raise ChannelClosed('partial atomic command')
            except (OSError, asyncio.CancelledError, ChannelClosed):
                self.close_commands()
                raise

    async def configure(self, **kwargs):
        await self._send(make_config(self.scope_id, self.owner_generation, **kwargs))

    async def command(self, kind):
        await self._send(make_command(self.scope_id, self.owner_generation, kind), atomic=True)

    async def go(self):
        await self.command('GO')

    async def stop(self):
        await self.command('STOP')

    async def cancel(self):
        await self.command('CANCEL')

    async def force(self):
        await self.command('FORCE')

    async def _drain(self):
        reader = FrameReader(MAX_ACTION_BYTES)
        try:
            while self.event_fd is not None:
                messages, eof = reader.read_available(self.event_fd)
                for message in messages:
                    validate_event(message, self.scope_id, self.owner_generation)
                    self.events.append(message)
                    self._event_queue.put_nowait(message)
                    self._changed.set()
                if eof:
                    break
                descriptor = self.event_fd
                await _fd_ready(descriptor, still_open=lambda: self.event_fd == descriptor)
        except (OSError, ProtocolError, asyncio.CancelledError):
            pass
        finally:
            self.events_available = False
            self._changed.set()
            self._event_queue.put_nowait(None)

    async def next_event(self, timeout=None):
        async with asyncio.timeout(timeout):
            return await self._event_queue.get()

    async def wait_event(self, kind, timeout=None):
        async with asyncio.timeout(timeout):
            while True:
                self._changed.clear()
                for event in self.events:
                    if event['kind'] == kind:
                        return event
                if not self.events_available:
                    raise ChannelClosed('event unavailable')
                await self._changed.wait()

    async def wait(self, timeout=None):
        """Return helper exit or None by deadline, leaving cleanup running."""
        try:
            async with asyncio.timeout(timeout):
                while self.process.poll() is None:
                    await asyncio.sleep(.01)
                if not self._drain_task.done() and self.event_fd is not None:
                    await asyncio.shield(self._drain_task)
                return self.process.returncode
        except TimeoutError:
            return None

    def close_commands(self):
        if self.command_fd is not None:
            os.close(self.command_fd)
            self.command_fd = None

    def close_events(self):
        self._drain_task.cancel()
        if self.event_fd is not None:
            asyncio.get_running_loop().remove_reader(self.event_fd)
            os.close(self.event_fd)
            self.event_fd = None

    def close_target_ready(self):
        if self.target_ready_fd is not None:
            os.close(self.target_ready_fd)
            self.target_ready_fd = None
