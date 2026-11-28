"""A target's gateway, owned by an ordinary S2 scope and immutable receipt."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile

import httpx

from evolution_lab.durability import effective

from .models import OwnerError
from .protocol import ChannelClosed, HelperHandle, MAX_CONFIG_BYTES, encode_frame
from .receipts import read_cleanup_receipt
from evolution_lab.projects.endpoint import socket_fits


def gateway_available():
    if any(importlib.util.find_spec(name) is None for name in ('fastapi', 'uvicorn')):
        raise OwnerError('gateway_unavailable', 'target_start')


def check_gateway_port(policy):
    block = policy.gateway
    try:
        family = socket.AF_INET6 if ':' in block.host else socket.AF_INET
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((block.host, block.port))
            sock.listen(1)
    except OSError:
        raise OwnerError('gateway_port_busy', 'target_start') from None


class TargetGateway:
    def __init__(self, owner, runtime):
        self.owner, self.runtime = owner, runtime
        self.helper = None
        self.scope_id = None
        self.marked = False
        self.tokens = {}
        self.control_directory = None
        self._control_credential = secrets.token_urlsafe(32)
        self._control_lock = asyncio.Lock()
        self._uses_lock = asyncio.Lock()
        self.uses = frozenset()
        self._uses_unavailable = False

    async def _control(self, uses=None):
        from evolution_lab._gateway_helper import control_frame
        async with self._control_lock, asyncio.timeout(2):
            reader, writer = await asyncio.open_unix_connection(str(self.control_directory / 'c'), limit=MAX_CONFIG_BYTES)
            try:
                writer.write(encode_frame({'credential': self._control_credential,
                    'uses': None if uses is None else sorted(uses)}, max_bytes=MAX_CONFIG_BYTES))
                await writer.drain()
                response = await control_frame(reader)
                if response != {'ready': True}:
                    raise OwnerError('gateway_unavailable', 'target_start')
            finally:
                writer.close()
                await writer.wait_closed()

    async def set_uses(self, uses: frozenset[str]):
        if not isinstance(uses, frozenset) or any(not isinstance(use, str) or not use for use in uses):
            raise OwnerError('invalid_argument', 'before_start')
        try:
            await self._control(uses)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
            raise OwnerError('gateway_unavailable', 'before_start') from None

    async def open_use(self, use: str):
        async with self._uses_lock:
            if self._uses_unavailable:
                raise OwnerError('gateway_unavailable', 'before_start')
            updated = self.uses | {use}
            try:
                await self.set_uses(updated)
            except BaseException:
                # It may have applied before losing its ACK (including caller
                # cancellation). No later admission can trust that set.
                self._uses_unavailable = True
                await self.runtime.request_stop()
                raise
            self.uses = updated

    async def close_use(self, use: str):
        async with self._uses_lock:
            if use not in self.uses:
                return
            updated = self.uses - {use}
            try:
                await self.set_uses(updated)
            except BaseException as error:
                self._uses_unavailable = True
                # A stale use set cannot serve later calls. Stop the owned target
                # through its existing common cleanup path if the ACK is lost.
                await self.runtime.request_stop()
                if not isinstance(error, OwnerError):
                    raise
            finally:
                self.uses = updated

    def alive(self):
        return (self.helper is not None and self.helper.process.poll() is None
                and not any(e['kind'] in {'child_exited', 'spawn_failed'} for e in self.helper.events))

    async def _open(self):
        self.scope_id = await self.owner._db('accept_target_gateway_scope', self.runtime.ref.id)
        directory = self.owner.state_directory / 'scopes' / self.scope_id
        await asyncio.to_thread(directory.mkdir, mode=0o700)
        try:
            self.control_directory = Path(tempfile.mkdtemp(prefix='lab-gw-'))
            if not socket_fits(self.control_directory / 'c'):
                raise OwnerError('gateway_unavailable', 'target_start')
            await self.owner._db('mark_helper_possible', self.scope_id)
            self.marked = True
            self.helper = HelperHandle.launch(sys.executable, directory, self.scope_id, self.owner.owner_generation,
                                             ownership=self.owner.ownership)
        except BaseException:
            if self.helper is None:
                await self._remove_control_directory()
            raise

    async def _remove_control_directory(self):
        if self.control_directory is not None:
            try:
                await asyncio.to_thread(shutil.rmtree, self.control_directory)
            except FileNotFoundError:
                pass

    async def start(self, policy, upstream_keys) -> dict[str, str]:
        gateway_available()
        check_gateway_port(policy)
        self.tokens = {route: secrets.token_urlsafe(32) for route in policy.routes}
        opening = asyncio.create_task(self._open())
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            await opening
            raise
        try:
            async with asyncio.timeout(self.runtime.binding.launch['startup_timeout_seconds']):
                await self.helper.configure(argv=[sys.executable, '-I', '-m', 'evolution_lab._gateway_helper'],
                    env={}, stop_grace_seconds=self.runtime.binding.launch['stop_grace_seconds'], role='gateway',
                    child_config={'durability': effective(), 'policy': policy.to_payload(), 'upstream_keys': dict(upstream_keys),
                        'tokens': self.tokens, 'control_path': str(self.control_directory / 'c'),
                        'control_credential': self._control_credential})
                await self.helper.wait_event('ready')
                await self.helper.go()
                await self.helper.wait_event('child_started')
                while True:
                    if not self.alive():
                        raise OwnerError('gateway_unavailable', 'target_start')
                    try:
                        await self._control()
                        break
                    except (OSError, TimeoutError, asyncio.IncompleteReadError, OwnerError):
                        await asyncio.sleep(.02)
                host = f'[{policy.gateway.host}]' if ':' in policy.gateway.host else policy.gateway.host
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    response = await client.get(f'http://{host}:{policy.gateway.port}/health')
                    if response.status_code != 200 or not self.alive():
                        raise OwnerError('gateway_unavailable', 'target_start')
                return dict(self.tokens)
        except (TimeoutError, ChannelClosed, OSError, httpx.HTTPError):
            raise OwnerError('gateway_unavailable', 'target_start') from None

    async def request_stop(self):
        if self.helper is not None:
            try:
                await self.helper.stop()
            except (ChannelClosed, OSError):
                self.helper.close_commands()

    async def stop(self):
        await self.request_stop()
        if self.scope_id is None:
            return
        if self.helper is None:
            if self.marked:
                raise OwnerError('cleanup_unconfirmed', 'finalizing')
            await self.owner._db('settle_scope_no_helper', self.scope_id)
            await self._remove_control_directory()
            return
        if await self.helper.wait(timeout=30) is None:
            raise OwnerError('cleanup_unconfirmed', 'finalizing')
        try:
            receipt = await self.owner._loop.run_in_executor(self.owner._executor, read_cleanup_receipt,
                self.owner.state_directory / 'scopes' / self.scope_id, self.scope_id, self.owner.owner_generation)
            await self.owner._db('confirm_scope_cleanup', self.scope_id, self.owner.owner_generation,
                forced=receipt['forced'], child_exit_code=receipt['child_exit_code'])
        finally:
            await self._remove_control_directory()

    def close(self):
        if self.helper is not None:
            self.helper.close_commands()
            self.helper.close_events()
            self.helper.close_target_ready()
        self.tokens.clear()
        self._control_credential = ''
