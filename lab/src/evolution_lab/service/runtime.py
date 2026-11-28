"""One service loop owns public API calls, preparation IO and ordered shutdown.

Route code must fully decode inputs before calling either dispatch method. These
methods are internal facade seams: method names come from explicit route mappings,
never from a request. Neither HTTP cancellation nor an SSE disconnect owns work.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
import logging
import math
import os
from pathlib import Path
import signal
import sqlite3
import stat

from evolution_lab._paths import canonical_root
from evolution_lab.durability import DURABILITY_ENV, Durability, effective
from evolution_lab.owner import Connection, Owner, ResourceRef, TaskRef
from evolution_lab.owner.scratch import prepare_scratch_directory, scratch_pair_directory
from evolution_lab.preparation import PreparationStore
from evolution_lab.projects.endpoint import socket_fits
from evolution_lab.projects.probes import ProbeHistoryUnavailable

from .environment import capture_service_environment
from .errors import ServiceError, error_response
from .mutations import MutationKey, MutationTable, decode_mutation_key, fingerprint


_OWNER_METHODS = frozenset({
    'trace', 'refusal', 'failure', 'inputs',
    'confirm_execution_policy', 'execution_policies', 'execution_policy', 'approvals',
    'bind', 'start_target', 'target', 'stop_target', 'check', 'conformance',
    'task', 'start', 'resume', 'list_operations', 'status', 'stop', 'report', 'report_markdown', 'operation_routing', 'diff',
    'bundle_measurements', 'measurements', 'measurement', 'sample', 'attempt', 'reflection', 'campaign', 'suspensions', 'list_summaries',
    'export', 'read_export', 'delete_results', 'cleanup',
    'release_after_manual_cleanup', 'health',
    'client_operations', 'client_targets', 'list_targets', 'client_tasks', 'cycle_starts',
})
_PREPARATION_METHODS = frozenset({
    'create_bundle', 'get_bundle', 'prepare_campaign', 'prepare_evaluation', 'get_prepared',
})
_PROJECT_METHODS = frozenset({
    'create', 'list_projects', 'get_project', 'update', 'bundle', 'prepare', 'start_target', 'start',
    'operations', 'operations_summary', 'operation_ids', 'facts_payload', 'status_payload', 'tree', 'confirm', 'confirmations',
    'revoke_confirmation', 'accept', 'revoke_acceptance', 'open_workspace', 'demo', 'port',
    'probe_start', 'probe_turn', 'probe_session', 'probe_list', 'probe_reset', 'probe_close',
    'sync_check', 'sync_apply', 'project_approvals',
})
_STOPPABLE_CALLS = frozenset({'start_target', 'check', 'conformance'})
# Project calls that start Owner work drain with Owner calls, before the stop requests; every
# other project call (clone, Bundle, facts, ...) may be slow IO and drains after the stops,
# like preparation calls, so it never delays stopping a paid operation (final review I9).
_PROJECT_OWNER_CALLS = frozenset({'start_target', 'start'})
# Probe calls drive the Owner too, but a turn may run for minutes: shutdown waits for them at most
# shutdown_wait_seconds, then the Owner close interrupts the running turn (spec 2026-10-02 §5.1).
_PROBE_CALLS = frozenset({'probe_start', 'probe_turn', 'probe_reset', 'probe_close'})
_ACTIVE_CONTROL_STATES = frozenset({'reserved', 'running', 'stopping'})


def _absolute_path(value):
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise ServiceError('invalid_argument') from None
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise ServiceError('invalid_argument')
    return path


def _operator_root(value):
    try:
        return canonical_root(_absolute_path(value))
    except ValueError:
        raise ServiceError('invalid_argument') from None


def _without_symlinks(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ServiceError('invalid_argument')


def _private_directory(path):
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ServiceError('invalid_argument')


class _ProjectLogDiagnostics(logging.Handler):
    """The service's stderr is JSON lines. A project or scratch cleanup warning
    becomes one diagnostic in the usual
    ``{"error": {code, stage, message}}`` shape instead of plain text from Python's default
    handler; the message carries the tail."""

    def __init__(self, diagnostics):
        super().__init__(logging.WARNING)
        self._diagnostics = diagnostics

    def emit(self, record):
        try:
            from evolution_lab.projects.errors import CODES, ProjectError
            code = getattr(record, 'lab_code', None)
            body = error_response(ProjectError(code) if code in CODES else ServiceError('lab_error'))['body']
            body['error']['message'] = record.getMessage()
            self._diagnostics.append(body)
        except Exception:
            self.handleError(record)


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    socket_path: Path | str
    state_directory: Path | str
    shutdown_wait_seconds: float = 30
    execution_wheels: tuple[Path, ...] = ()
    execution_runtime_files: tuple[Path, ...] = ()
    # The Lab home (ADR-0017). None here means "not given": run_service resolves
    # default_home(); the runtime itself never falls back to the user's home.
    home_directory: Path | str | None = None
    durability: Durability | None = None
    scratch_directory: Path | None = None

    def __post_init__(self):
        if self.durability is not None and self.durability not in ('strict', 'relaxed'):
            raise ServiceError('invalid_argument')
        socket = _absolute_path(self.socket_path)
        # Preserve the original length admission, then check the bound address
        # again after canonicalization can expand its prefix.
        if not socket_fits(socket):
            raise ServiceError('socket_path_too_long')
        object.__setattr__(self, 'socket_path', _operator_root(socket.parent) / socket.name)
        if not socket_fits(self.socket_path):
            raise ServiceError('socket_path_too_long')
        object.__setattr__(self, 'state_directory', _operator_root(self.state_directory))
        value = self.shutdown_wait_seconds
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and value > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ServiceError('invalid_argument')
        object.__setattr__(self, 'execution_wheels',
            tuple(_absolute_path(p) for p in self.execution_wheels))
        object.__setattr__(self, 'execution_runtime_files',
            tuple(_absolute_path(p) for p in self.execution_runtime_files))
        if self.execution_runtime_files and not self.execution_wheels:
            raise ServiceError('invalid_argument')
        if self.home_directory is not None:
            object.__setattr__(self, 'home_directory', _operator_root(self.home_directory))
        if self.scratch_directory is not None:
            object.__setattr__(self, 'scratch_directory', _operator_root(self.scratch_directory))

    def validate_paths(self):
        """Read-only validation; creation follows under runtime ownership. A socket directly
        in the home (the W49 default) is checked by prepare_home, which creates the home."""
        _without_symlinks(self.socket_path.parent)
        _without_symlinks(self.state_directory)
        if self.socket_path.parent != self.home_directory:
            _private_directory(self.socket_path.parent)
        if self.state_directory.exists():
            _private_directory(self.state_directory)
        if self.scratch_directory is not None:
            _without_symlinks(self.scratch_directory)
            if self.scratch_directory.exists():
                _private_directory(self.scratch_directory)
            if (self.scratch_directory.is_relative_to(self.state_directory)
                    or self.state_directory.is_relative_to(self.scratch_directory)):
                raise ServiceError('invalid_argument')
        if self.home_directory is not None:
            _without_symlinks(self.home_directory)
            # Spec §3.1: the pair state root may live in the home (H/pairs/dev28) but must
            # not be, sit inside, or contain the project trees Lab owns there.
            state = self.state_directory.resolve(strict=False)
            for name in ('clones', 'workspaces'):
                tree = (self.home_directory / name).resolve(strict=False)
                if state.is_relative_to(tree) or tree.is_relative_to(state):
                    raise ServiceError('invalid_argument')
                if self.scratch_directory is not None:
                    scratch = self.scratch_directory.resolve(strict=False)
                    if scratch.is_relative_to(tree) or tree.is_relative_to(scratch):
                        raise ServiceError('invalid_argument')


class ServiceRuntime:
    """Lifecycle authority for one Owner, one preparation executor and one Lab home."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.owner = None
        self.preparation = None
        self.projects = None
        self.project_session = None
        self.service_environment = None
        self.stopping = asyncio.Event()
        self.shutdown_result = None
        self.diagnostics = []
        self.http_failure = None
        self._loop = asyncio.get_running_loop()
        self._admission_open = False
        self._connections: dict[str, Connection] = {}
        self._stoppable_refs: dict[str, TaskRef] = {}
        self._owner_calls = set()
        self._probe_calls = set()
        self._preparation_calls = set()
        self._mutations = MutationTable()
        self._executor = None
        self._owner_context = None
        self._startup_done = asyncio.Event()
        self._start_entered = False
        self._shutdown_task = None
        self._http_server = None
        self._http_task = None
        self._listener = None
        self._project_log = None

    def _assert_loop(self):
        if asyncio.get_running_loop() is not self._loop:
            raise ServiceError('invalid_argument')

    async def start(self):
        """Open the Lab home, the public Owner context, then preparation and the
        project session; False means signal unwind."""
        self._assert_loop()
        if self._start_entered:
            raise ServiceError('invalid_argument')
        self._start_entered = True
        if self.config.durability is not None:
            os.environ[DURABILITY_ENV] = self.config.durability
        effective()  # Refuse before any store or scratch directory opens.
        try:
            if self.stopping.is_set():
                return False
            if self.config.home_directory is None:
                raise ServiceError('invalid_argument')
            self.config.validate_paths()
            # Before the home opens: reconciling an interrupted clone may already log.
            self._project_log = _ProjectLogDiagnostics(self.diagnostics)
            logging.getLogger('evolution_lab.projects').addHandler(self._project_log)
            logging.getLogger('evolution_lab.owner.scratch').addHandler(self._project_log)
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lab-service-preparation')
            if self.config.execution_wheels:
                # Before state, Owner and socket: a refused capture leaves nothing behind.
                future = self._loop.run_in_executor(self._executor, partial(
                    capture_service_environment, self.config.execution_wheels,
                    self.config.execution_runtime_files))
                self._track(future, self._preparation_calls)
                self.service_environment = await asyncio.shield(future)
                if self.stopping.is_set():
                    return False
            # One writer per home (D16): before state, so home_busy leaves no state
            # behind and precedes an Owner busy on the same pair.
            from evolution_lab.projects.registry import Projects
            # The state root may live in the home (spec §3.1): the port-leftover sweep never touches it (codex r2).
            future = self._loop.run_in_executor(self._executor, partial(
                Projects.open, self.config.home_directory,
                protected=tuple(path for path in (self.config.state_directory,
                    self.config.scratch_directory) if path is not None),
                durability=self.config.durability))
            self._track(future, self._preparation_calls)
            self.projects = await asyncio.shield(future)
            if self.stopping.is_set():
                return False
            try:
                self.config.state_directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _without_symlinks(self.config.state_directory)
            _private_directory(self.config.state_directory)
            scratch = None
            if self.config.scratch_directory is not None:
                prepare_scratch_directory(self.config.scratch_directory)
                scratch = scratch_pair_directory(self.config.scratch_directory, self.config.state_directory)
            context = Owner.open(self.config.state_directory, durability=self.config.durability,
                scratch_directory=scratch)
            self.owner = await context.__aenter__()
            self._owner_context = context
            if self.stopping.is_set():
                return False
            future = self._loop.run_in_executor(self._executor, PreparationStore, self.config.state_directory)
            self._track(future, self._preparation_calls)
            self.preparation = await asyncio.shield(future)
            if self.stopping.is_set():
                return False
            from importlib.metadata import version
            from evolution_lab.projects.models import Pair
            from evolution_lab.projects.session import ProjectSession
            self.project_session = ProjectSession(
                self.projects, preparation=self.preparation, owner=self.owner,
                pair=Pair(version('evolution-lab'), version('evo-skills')),
                state_root=self.config.state_directory, run_io=self._run_io,
                service_environment=self.service_environment, socket_path=self.config.socket_path,
                durability=self.config.durability)
            # Admission is still closed: the session is called directly, not via call_*.
            await self.project_session.sync()
            # Sessions the previous run left open are closed (target_gone) before the first request (spec §3.5).
            try:
                await self.project_session.probe_reconcile()
            except (ProbeHistoryUnavailable, sqlite3.DatabaseError):
                # The rest of Lab starts; probe calls answer history_unavailable until the file is fixed (§5.1).
                self._diagnose(ServiceError('history_unavailable'))
            if self.stopping.is_set():
                return False
            self._admission_open = True
            logging.getLogger(__name__).info('durability=%s scratch=%s',
                self.config.durability or effective(), self.config.scratch_directory or 'none')
            return True
        finally:
            self._startup_done.set()

    def connection(self, identifier: str) -> Connection:
        """Resolve a bind result from this runtime; callers validate the ID first."""
        self._assert_loop()
        try:
            return self._connections[identifier]
        except KeyError:
            raise ServiceError('not_found') from None

    def _admit(self, method, allowed):
        self._assert_loop()
        if not self._admission_open:
            raise ServiceError('service_stopping')
        if method not in allowed:
            raise ServiceError('invalid_argument')

    @staticmethod
    def _track(future, collection):
        collection.add(future)

        def finished(done):
            collection.discard(done)
            # A disconnected observer may never retrieve this exception. Its
            # contents are never logged, and live observers still receive it.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        return future

    def _submit_owner(self, method: str, *args, **kwargs):
        """Admit and create the service-owned task synchronously (no await)."""
        self._admit(method, _OWNER_METHODS)

        async def invoke():
            result = await getattr(self.owner, method)(*args, **kwargs)
            if method == 'bind':
                if not isinstance(result, Connection):
                    raise ServiceError('lab_error')
                self._connections[result.id] = result
            elif method in _STOPPABLE_CALLS:
                if not isinstance(result, TaskRef):
                    raise ServiceError('lab_error')
                self._stoppable_refs[result.id] = result
            return result

        return self._track(self._loop.create_task(invoke()), self._owner_calls)

    async def call_owner(self, method: str, *args, **kwargs):
        """Admit a decoded public call and retain both execution and its refs."""
        return await asyncio.shield(self._submit_owner(method, *args, **kwargs))

    def _submit_preparation(self, method: str, *args, **kwargs):
        """Admit and schedule the executor job synchronously (no await)."""
        self._admit(method, _PREPARATION_METHODS)
        function = partial(getattr(self.preparation, method), *args, **kwargs)
        return self._track(self._loop.run_in_executor(self._executor, function), self._preparation_calls)

    async def call_preparation(self, method: str, *args, **kwargs):
        """Keep submitted synchronous work alive until its actual IO finishes."""
        return await asyncio.shield(self._submit_preparation(method, *args, **kwargs))

    def _run_io(self, function):
        """The project session's blocking IO shares the preparation executor."""
        return self._loop.run_in_executor(self._executor, function)

    def _submit_projects(self, method: str, /, *args, **kwargs):
        """Admit and create the service-owned project task synchronously (no await).

        ``start``/``start_target`` drive Owner, so they drain with Owner calls before the
        stop requests; a target task a project route started is stopped before close like any
        other. Probe mutations drain alongside, bounded by the shutdown budget. The rest drain
        after the stops, with preparation calls."""
        self._admit(method, _PROJECT_METHODS)

        async def invoke():
            try:
                result = await getattr(self.project_session, method)(*args, **kwargs)
            except ProbeHistoryUnavailable:
                raise ServiceError('history_unavailable') from None
            except sqlite3.DatabaseError:
                if method.startswith('probe_'):  # a probes.sqlite3 damaged past its header
                    raise ServiceError('history_unavailable') from None
                raise
            if method == 'start_target':
                task = result[0]
                if not isinstance(task, TaskRef):
                    raise ServiceError('lab_error')
                self._stoppable_refs[task.id] = task
            return result

        calls = (self._owner_calls if method in _PROJECT_OWNER_CALLS
                 else self._probe_calls if method in _PROBE_CALLS else self._preparation_calls)
        return self._track(self._loop.create_task(invoke()), calls)

    async def call_projects(self, method: str, /, *args, **kwargs):
        """Admit a decoded project call; like call_owner, cancellation does not own it."""
        return await asyncio.shield(self._submit_projects(method, *args, **kwargs))

    def mutation_key(self, headers) -> MutationKey | None:
        """Step 1: syntax and generation, before any request state is read."""
        self._assert_loop()
        key = decode_mutation_key(headers)
        if key is not None:
            if self.owner is None:
                raise ServiceError('service_stopping')
            if key.owner_generation != self.owner.owner_generation:
                raise ServiceError('request_unknown_generation')
        return key

    async def call_mutation(self, key, route, parts, target, method, /, *args, **kwargs):
        """Steps 3-4. Everything before the final await runs without yielding, so
        two concurrent requests with one key cannot create two actions.

        `parts` holds the `fingerprint` keywords (path/query/body); they are
        fingerprinted only for a keyed request, so unkeyed calls stay S5."""
        self._assert_loop()
        submit = {'owner': self._submit_owner, 'preparation': self._submit_preparation,
                  'projects': self._submit_projects}[target]
        if key is None:
            return await asyncio.shield(submit(method, *args, **kwargs))
        digest = fingerprint(route, **parts)
        existing = self._mutations.lookup(key.request_id, route, digest)
        if existing is not None:
            return await asyncio.shield(existing)
        if not self._admission_open:
            raise ServiceError('service_stopping')
        self._mutations.ensure_capacity(route)
        future = submit(method, *args, **kwargs)
        self._mutations.record(key.request_id, route, digest, future)
        return await asyncio.shield(future)

    def attach_http(self, server, task, listener):
        """Transfer HTTP/listener lifetime to runtime; Owner is outside lifespan."""
        self._assert_loop()
        self._http_server, self._http_task, self._listener = server, task, listener
        if server is not None and self.stopping.is_set():
            server.should_exit = True

    def request_shutdown(self):
        """Atomically close admission/observation and coalesce shutdown requests."""
        self._assert_loop()
        if self._shutdown_task is None:
            self._admission_open = False
            self.stopping.set()
            if self._http_server is not None:
                self._http_server.should_exit = True
            self._shutdown_task = self._loop.create_task(self._shutdown())
        return self._shutdown_task

    async def shutdown(self):
        return await asyncio.shield(self.request_shutdown())

    def _diagnose(self, error):
        self.diagnostics.append(error_response(error)['body'])

    async def _settle(self, operation_ids):
        """Wait, within the shutdown wait budget, until the operations Stop was requested for
        leave reserved/running/stopping. An unreadable status ends the wait for that one."""
        deadline = self._loop.time() + self.config.shutdown_wait_seconds
        pending = list(operation_ids)
        while pending:
            for operation_id in tuple(pending):
                try:
                    status = await self.owner.status(ResourceRef('operation', operation_id))
                except Exception:
                    pending.remove(operation_id)
                    continue
                if status.control_state not in _ACTIVE_CONTROL_STATES:
                    pending.remove(operation_id)
            remaining = deadline - self._loop.time()
            if not pending or remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))

    @staticmethod
    async def _drain(calls, timeout=None):
        if calls:
            await asyncio.wait(tuple(calls), timeout=timeout)

    async def _shutdown(self):
        if self._start_entered:
            await self._startup_done.wait()
        if self.project_session is not None:
            # Spec 2026-10-02 §5.1: a probe call still queued on the probe lock is refused service_stopping;
            # the running one gets at most the shutdown budget, then the Owner close interrupts it.
            self.project_session.stop_probes()
        await asyncio.gather(self._drain(self._owner_calls),
                             self._drain(self._probe_calls, self.config.shutdown_wait_seconds))
        stopped = []
        if self.owner is not None:
            try:
                health = await self.owner.health()
                if health.active_operation_id is not None:
                    await self.owner.stop(ResourceRef('operation', health.active_operation_id))
                    stopped.append(health.active_operation_id)
            except Exception as error:
                self._diagnose(error)
            for ref in self._stoppable_refs.values():
                try:
                    await self.owner.stop(ref)
                except Exception as error:
                    self._diagnose(error)
        if self._preparation_calls:
            await asyncio.gather(*tuple(self._preparation_calls), return_exceptions=True)
        if self.project_session is not None:
            # The last projection of this pair before a repin; Owner still reads. It runs once
            # the stopped operation settled, so its link keeps the final state, not "stopping".
            await self._settle(stopped)
            try:
                await self.project_session.sync()
            except Exception as error:
                self._diagnose(error)  # shutdown never fails on a projection refresh
        try:
            if self.owner is not None:
                if self.project_session is not None:
                    # A turn the close interrupts stays pending for the next start to record (spec §5.1).
                    self.project_session.interrupt_probes()
                try:
                    self.shutdown_result = await self.owner.close(wait_seconds=self.config.shutdown_wait_seconds)
                except Exception as error:
                    self._diagnose(error)
                finally:
                    try:
                        await self._owner_context.__aexit__(None, None, None)
                    except Exception as error:
                        self._diagnose(error)
        finally:
            if self.project_session is not None:
                # The Owner is closed, but a settlement it gave up on may still run (spec §5.1): a probe call
                # waiting on it answers service_stopping now. The calls only unwind here, so they return before
                # their history closes; the wait is still bounded, and a call later than that writes nothing.
                self.project_session.abandon_probes()
            await self._drain(self._probe_calls, self.config.shutdown_wait_seconds)
            if self.project_session is not None:
                # Before the home lock is released: the probe history handle belongs to the session.
                try:
                    self.project_session.close()
                except Exception as error:
                    self._diagnose(error)
            if self.projects is not None:
                # After the Owner context: the home lock outlives every pair write.
                try:
                    self.projects.close()
                except Exception as error:
                    self._diagnose(error)
            if self._executor is not None:
                # All its submitted futures have finished, so this cannot block IO.
                self._executor.shutdown(wait=True)
            if self._project_log is not None:
                logging.getLogger('evolution_lab.projects').removeHandler(self._project_log)
                logging.getLogger('evolution_lab.owner.scratch').removeHandler(self._project_log)
            if self._http_task is not None:
                try:
                    await self._http_task
                except asyncio.CancelledError:
                    self.http_failure = error_response(ServiceError('lab_error'))['body']
                except Exception as error:
                    # serve() also observes this task. Retain its failure apart
                    # from independent Stop/close/socket diagnostics so the entry
                    # point emits this same HTTP failure exactly once.
                    self.http_failure = error_response(error)['body']
            if self._listener is not None:
                try:
                    self._listener.close()
                except Exception as error:
                    self._diagnose(error)
        return self.shutdown_result

    @contextmanager
    def signal_handlers(self):
        """Own service-PID signals until cleanup; callbacks run on this loop."""
        self._assert_loop()
        previous = {}
        try:
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.getsignal(number)
                self._loop.add_signal_handler(number, self.request_shutdown)
            yield
        finally:
            for number, handler in previous.items():
                self._loop.remove_signal_handler(number)
                signal.signal(number, handler)
