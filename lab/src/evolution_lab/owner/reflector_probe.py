"""One campaign start's reflector process, owned exclusively by S2 helper scopes.

This is separate from Owner's interactive target probe sessions. Core remains the
only authority on what a mid-campaign probe outage means for the campaign.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat
from urllib.parse import urlsplit

from .models import OwnerError
from .protocol import ChannelClosed, HelperHandle
from .receipts import open_directory_nofollow, read_cleanup_receipt


async def healthy(probe):
    """One bounded direct loopback GET, completed headers, without reading its body."""
    import httpx

    try:
        async with asyncio.timeout(probe.request_timeout_seconds):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=probe.request_timeout_seconds) as client:
                async with client.stream('GET', probe.base_url + probe.health_path) as response:
                    return 200 <= response.status_code < 300
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        return False


def check_port(probe):
    endpoint = urlsplit(probe.base_url)
    family = socket.AF_INET6 if ':' in endpoint.hostname else socket.AF_INET
    try:
        with socket.socket(family) as sock:
            # A reaped server may leave accepted TCP connections in TIME_WAIT.
            # Reuse ignores those connections, while bind+listen still rejects a live listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((endpoint.hostname, endpoint.port))
            sock.listen(1)
    except OSError:
        raise OwnerError('reflector_probe_port_busy', 'before_start') from None


def append_notes(directory, lines, *, create=False):
    directory_fd = open_directory_nofollow(directory)
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open('stderr.log', flags, 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError('worker log is not a regular file')
            for line in lines:
                raw = ('evo tree evolve: note: ' + line + '\n').encode('utf-8')
                while raw:
                    raw = raw[os.write(descriptor, raw):]
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


class ReflectorProbe:
    def __init__(self, owner, runtime, binding):
        from .core_raw import reflector_probe_policy

        self.owner, self.runtime, self.binding = owner, runtime, binding
        self.probe = binding.manifest.workflow.reflector.probe
        self.continuing = reflector_probe_policy(binding.manifest) == 'continue'
        self.launch = binding.launch.get('reflector_probe')
        self.helper = None
        self.scope_id = None
        self.marked = False
        self.restarts = 0
        self.last_exit = None
        self.state = 'stopped'
        self.start_number = None
        self.notes = []
        self.watcher = None
        self.ending = False
        self.forced_cleanup = False

    def interrupted(self):
        return (self.ending or self.forced_cleanup or self.runtime.stop.is_set() or self.owner._stopping
                or (self.runtime.helper is not None and self.runtime.helper.process.poll() is not None))

    async def persist(self):
        await self.owner._db('update_reflector_probe', self.runtime.id, start=self.start_number,
                            state=self.state, restarts=self.restarts, last_exit=self.last_exit)

    async def note(self, text):
        self.notes.append(text)
        await self.flush_notes()

    async def flush_notes(self):
        worker = self.runtime.helper
        if (not self.notes or worker is None or self.runtime.paths is None
                or not any(e['kind'] == 'child_started' for e in worker.events)):
            return
        try:
            await asyncio.to_thread(append_notes, self.runtime.paths.scope_directory, tuple(self.notes))
        except (OSError, ValueError):
            pass  # Operator log IO cannot replace an acquired Core/probe outcome.
        self.notes.clear()

    async def finish_notes_without_worker(self):
        """Called only by admission's final block: no worker helper can now be spawned."""
        if self.runtime.helper is not None or not self.notes:
            return
        directory = self.owner.state_directory / 'scopes' / self.runtime.scope_id
        try:
            await asyncio.to_thread(directory.mkdir, mode=0o700, exist_ok=True)
            await asyncio.to_thread(append_notes, directory, tuple(self.notes), create=True)
        except (OSError, ValueError):
            pass
        self.notes.clear()

    async def prepare(self):
        value = await self.owner._db('operation_status', self.runtime.id)
        self.start_number = value.starts[-1].start
        await self.owner._db('begin_reflector_probe', self.runtime.id,
                            launched_by='operator' if self.launch is None else 'lab')
        async def startup():
            try:
                if self.launch is None:
                    if not await healthy(self.probe):
                        raise OwnerError('reflector_probe_unavailable', 'before_start')
                    self.state = 'running'
                    await self.persist()
                else:
                    from .binding import revalidate_environment
                    await asyncio.to_thread(revalidate_environment, self.binding,
                                            self.launch['environment_ref'], stage='before_start')
                    check_port(self.probe)
                    if not await self.launch_scope():
                        raise OwnerError('dispatch_refused', 'before_start')
            except OwnerError as error:
                if error.code != 'reflector_probe_unavailable' or not self.continuing:
                    raise
                # Keep a launched child owned and watched: it can become healthy later.
                # A helper that did not launch a child must be fully settled before dispatch.
                if self.helper is not None:
                    if any(e['kind'] == 'child_started' for e in self.helper.events):
                        self.state = 'running'
                        await self.persist()
                    else:
                        await self.request_stop()
                        await self.settle_scope()
                else:
                    # The operator's participation window is active even if health is unavailable.
                    self.state = 'running'
                    await self.persist()
                await self.note(f'reflector probe at {self.probe.base_url} is unavailable at start; '
                                'reflections continue without it')
        task = asyncio.create_task(startup())
        stop = asyncio.create_task(self.runtime.stop.wait())
        try:
            done, _ = await asyncio.wait((task, stop), return_when=asyncio.FIRST_COMPLETED)
            if stop in done or self.interrupted():
                raise OwnerError('dispatch_refused', 'before_start')
            await task
        finally:
            for pending in (task, stop):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stop, return_exceptions=True)

    async def open_scope(self, *, restart=False):
        if self.interrupted():
            return False
        self.scope_id = await self.owner._db('accept_reflector_probe_scope', self.runtime.id, restart=restart)
        if self.scope_id is None:
            return False
        self.marked = False
        if restart:
            self.restarts += 1  # The store committed this count with the accepted scope.
        directory = self.owner.state_directory / 'scopes' / self.scope_id
        await asyncio.to_thread(directory.mkdir, mode=0o700)
        await asyncio.to_thread((directory / 'work').mkdir, mode=0o700)
        await self.owner._db('mark_helper_possible', self.scope_id)
        self.marked = True
        self.helper = HelperHandle.launch(self.binding.execution_environment.executable,
                                         directory, self.scope_id, self.owner.owner_generation,
                                         ownership=self.owner.ownership)
        return True

    async def launch_scope(self, *, restart=False):
        # A committed spawn marker must retain its handle even when a stop cancels
        # startup. This short ownership handoff is shielded; CONFIG/health remain cancellable.
        opening = asyncio.create_task(self.open_scope(restart=restart))
        try:
            if not await asyncio.shield(opening):
                return False
        except asyncio.CancelledError:
            await opening
            raise
        directory = self.owner.state_directory / 'scopes' / self.scope_id
        argv = [self.launch['executable']]
        for arg in self.launch['args']:
            argv.append(arg['literal'] if 'literal' in arg else
                        str(self.binding.bundle_path / arg['bundle_path']) if 'bundle_path' in arg else
                        str(urlsplit(self.probe.base_url).port))
        try:
            async with asyncio.timeout(self.launch['startup_timeout_seconds']):
                await self.helper.configure(argv=argv, env=dict(self.runtime.target.secrets.environment),
                    stop_grace_seconds=self.launch['stop_grace_seconds'], cwd=str(directory / 'work'))
                await self.helper.wait_event('ready')
                if self.interrupted():
                    return False
                await self.helper.go()
                await self.helper.wait_event('child_started')
                await self.note(f'reflector probe started at {self.probe.base_url}')
                while not await healthy(self.probe):
                    if self.helper.process.poll() is not None:
                        raise OwnerError('reflector_probe_unavailable', 'before_start')
                    await asyncio.sleep(.02)
                self.state = 'running'
                await self.persist()
                return True
        except (TimeoutError, ChannelClosed):
            raise OwnerError('reflector_probe_unavailable', 'before_start') from None

    async def request_stop(self):
        if self.helper is not None:
            try:
                await self.helper.stop()
            except (ChannelClosed, OSError):
                self.helper.close_commands()

    def close_channels(self):
        if self.helper is not None:
            self.helper.close_commands()
            self.helper.close_events()
            self.helper.close_target_ready()

    async def settle_scope(self):
        if self.scope_id is None:
            return
        if self.helper is None:
            if self.marked:
                raise OwnerError('cleanup_unconfirmed', 'finalizing')
            await self.owner._db('settle_scope_no_helper', self.scope_id)
        else:
            seconds = self.runtime.prepared.runtime_policy.cleanup_wait_seconds
            if await self.helper.wait(timeout=seconds) is None:
                raise OwnerError('cleanup_unconfirmed', 'finalizing')
            receipt = await asyncio.to_thread(read_cleanup_receipt,
                self.owner.state_directory / 'scopes' / self.scope_id, self.scope_id, self.owner.owner_generation)
            if receipt['forced']:
                durable = await self.owner._db('operation_status', self.runtime.id)
                self.owner._operation_cache.setdefault(self.runtime.id, durable)
            await self.owner._db('confirm_scope_cleanup', self.scope_id, self.owner.owner_generation,
                                 forced=receipt['forced'], child_exit_code=receipt['child_exit_code'])
            if receipt['forced']:
                # This run's own receipt now requires cleanup. Stop replacements
                # while leaving Core's outcome to the ordinary worker finalizer.
                self.forced_cleanup = True
                await self.note('reflector probe cleanup was forced; no further restarts for this run')
            self.last_exit = receipt['child_exit_code']
            await self.note(f'reflector probe exited with code {self.last_exit}; restart {self.restarts}')
            self.close_channels()
        self.helper = None
        self.scope_id = None
        self.state = 'stopped'
        await self.persist()

    async def watch(self):
        while not self.interrupted():
            await self.flush_notes()
            if self.helper is not None and any(e['kind'] == 'child_exited' for e in self.helper.events):
                # STOP lets the helper reap descendants of an exited child before any replacement.
                await self.request_stop()
                await self.settle_scope()
                while not self.interrupted():
                    if self.restarts >= self.launch['max_restarts']:
                        self.state = 'exhausted'
                        await self.persist()
                        await self.note(f'reflector probe exhausted max_restarts={self.launch["max_restarts"]}')
                        return
                    try:
                        if not await self.launch_scope(restart=True):
                            # The campaign ended during the restart handoff. Any accepted
                            # scope still belongs to us and needs its ordinary receipt.
                            await self.request_stop()
                            await self.settle_scope()
                            return
                        break
                    except OwnerError as error:
                        if error.code != 'reflector_probe_unavailable':
                            raise
                        await self.request_stop()
                        await self.settle_scope()
            await asyncio.sleep(.02)

    async def finish(self):
        self.ending = True
        if self.watcher is not None:
            self.watcher.cancel()
            outcome, = await asyncio.gather(self.watcher, return_exceptions=True)
        else:
            outcome = None
        await self.request_stop()
        await self.settle_scope()
        if self.state != 'exhausted':
            self.state = 'stopped'
            if self.start_number is not None:
                await self.persist()
        await self.flush_notes()
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            raise outcome
