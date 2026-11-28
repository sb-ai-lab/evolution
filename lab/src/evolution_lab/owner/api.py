"""Async Python Owner. Campaign and full cleanup facades extend this boundary."""
from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Mapping

from evolution_lab.durability import Durability
from evolution_lab.preparation.storage import PreparationStorage
from evolution_lab._ownership import select_mode

from .binding import open_bundle
from .models import (Approval, ApprovalRecord, CleanupFacts, Connection, Health, OwnerError, ProcessFacts,
    PolicyRecord, ResourceRef, ShutdownResult, StopReceipt, TargetRef, TaskRef, TaskResult)
from .models import _digest
from .store import OwnerStateStore
from .target import TargetSupport
from .tasks import TaskSupport, failure_for, positive_seconds
from .execution import ExecutionSupport
from .readers import ReaderSupport
from .cleanup import CleanupSupport
from .conformance import ConformanceSupport
from .probe import ProbeSupport
from .result_io import OperationResultIO, ResultIOSupport
from .results import ResultsSupport
from .traces import TraceSupport
from .measurements import MeasurementCache


class Owner(
    TaskSupport, TargetSupport, ExecutionSupport, ConformanceSupport,
    ReaderSupport, CleanupSupport, ResultIOSupport, ResultsSupport, ProbeSupport, TraceSupport,
):
    """One loop/thread and one durable state; all SQLite work uses one thread."""

    @classmethod
    @asynccontextmanager
    async def open(cls, state_directory, *, durability: Durability | None = None,
                   scratch_directory: Path | None = None):
        """Open one Owner; scratch_directory is the precomputed pair directory."""
        owner = cls()
        try:
            owner.ownership = select_mode()
        except ValueError:
            raise OwnerError('environment_invalid', 'connect') from None
        owner._loop = asyncio.get_running_loop()
        owner._thread = threading.get_ident()
        owner._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lab-owner-store')
        owner._owned = set()
        owner._result_owned = set()
        owner._result_io = OperationResultIO()
        owner._tasks = {}
        owner._targets = {}
        owner._connections = {}
        owner._task_cache = {}
        owner._target_cache = {}
        owner._operations = {}
        from .auto_resume import AutoResumeManager
        owner._auto_resume = AutoResumeManager(owner)
        owner._operation_cache = {}
        owner._plan_cache = {}
        owner._summary_cache = {}
        owner._projection_cache = {}
        owner._measurements = MeasurementCache()
        from .core_raw import BaselineProvenanceCache
        owner._baseline_provenance = BaselineProvenanceCache()
        owner._conformance = {}
        owner._probes = {}
        owner._probe_turn_lock = asyncio.Lock()
        owner._stopping = False
        owner._closed = False
        owner._close_task = None
        owner._shutdown_result = None
        opened = owner._loop.run_in_executor(owner._executor, functools.partial(
            OwnerStateStore.open, state_directory, durability=durability,
            scratch_directory=scratch_directory))
        try:
            try:
                owner._store = await asyncio.shield(opened)
            except asyncio.CancelledError:
                # An off-loop open can already own the lock even without a caller.
                store = await asyncio.shield(opened)
                await owner._loop.run_in_executor(owner._executor, store.close)
                raise
            owner.state_directory = owner._store.state_directory
            owner.scratch_directory = owner._store.scratch_directory
            owner.owner_generation = owner._store.generation
            owner._storage = await asyncio.to_thread(PreparationStorage, owner.state_directory)
        except BaseException:
            if hasattr(owner, '_store'):
                await owner._loop.run_in_executor(owner._executor, owner._store.close)
            owner._executor.shutdown(wait=False)
            raise
        try:
            yield owner
        finally:
            await owner.close()

    def _assert_loop(self):
        if threading.get_ident() != self._thread or asyncio.get_running_loop() is not self._loop:
            raise OwnerError('invalid_argument', 'before_start')

    def _assert_mutation(self):
        self._assert_loop()
        if self._stopping or self._closed:
            raise OwnerError('service_stopping', 'before_start')

    async def _db(self, method, *args, **kwargs):
        self._assert_loop()
        if self._closed:
            raise OwnerError('service_stopping', 'finalizing')
        call = functools.partial(getattr(self._store, method), *args, **kwargs)
        return await asyncio.shield(self._loop.run_in_executor(self._executor, call))

    def _connection_binding(self, connection):
        if not isinstance(connection, Connection) or connection.owner_generation != self.owner_generation:
            raise OwnerError('invalid_argument', 'connect')
        known = self._connections.get(connection.id)
        if known is None or known[0] != connection:
            raise OwnerError('not_found', 'connect')
        return known[1]

    async def confirm_execution_policy(self, document: Mapping, *, expected_digest: str,
                                       author: str, channel: str, note: str | None = None,
                                       approval: Approval | None = None, request_id: str | None = None) -> PolicyRecord:
        self._assert_mutation()
        approval_kwargs = {} if approval is None else dict(approval=approval, request_id=request_id)
        return await self._db("confirm_execution_policy", document, expected_digest=expected_digest,
                              author=author, channel=channel, note=note, **approval_kwargs)

    async def execution_policies(self) -> tuple[PolicyRecord, ...]:
        return await self._db("execution_policies")

    async def execution_policy(self, digest: str) -> PolicyRecord:
        return await self._db("execution_policy", digest)

    async def approvals(self, *, operation_id: str | None = None,
                        policy_digest: str | None = None) -> list[ApprovalRecord]:
        return await self._db("approvals_for", operation_id=operation_id, policy_digest=policy_digest)

    async def bind(self, bundle_digest):
        self._assert_mutation()
        async def admitted(accepted):
            bound = await asyncio.to_thread(open_bundle, self._storage, bundle_digest)
            self._assert_mutation()
            connection = await self._db('bind_connection', source_kind=bound.source_kind,
                bundle_digest=bound.bundle_digest, launch_digest=bound.launch_digest)
            self._connections[connection.id] = (connection, bound)
            accepted.set_result(connection)
        return await self._admit(admitted)

    async def check(self, connection, *, target_ref):
        self._assert_mutation()
        binding = self._connection_binding(connection)
        value = await self.target(target_ref)
        if target_ref.owner_generation != self.owner_generation or value.state != 'ready':
            raise OwnerError('target_not_running', 'check')
        if value.bundle_digest != binding.bundle_digest:
            raise OwnerError('binding_mismatch', 'check')
        def observe():
            if binding.source_kind in {'evaluation', 'probe'}:
                from .target import evaluation_preflight
                return asyncio.run(evaluation_preflight(binding))
            from evolution_lab._manifest_schema import CAMPAIGN_MANIFEST_SCHEMAS
            from evolution_lab.client import _connect, check
            return asyncio.run(check(_connect(binding.manifest_path, CAMPAIGN_MANIFEST_SCHEMAS)))
        async def admitted(accepted):
            self._assert_mutation()
            reservation = await self._db('accept_task', action='check', ref=ResourceRef('target', target_ref.id), reserve_target_use=True)
            entry = self._register_task(reservation.task_ref, 'check', ResourceRef('target', target_ref.id))
            accepted.set_result(entry.ref)
            try:
                await self._db('task_running', entry.ref.id)
                if self._stopping or entry.stop.is_set():
                    raise OwnerError('dispatch_refused', 'check')
                entry.observation = asyncio.create_task(asyncio.to_thread(observe))
                result = await entry.observation
                failure = None if result.failure is None else failure_for(result.failure, 'check')
                if result.failure is not None:
                    from .models import Failure
                    failure = Failure.make(result.failure.code, 'check')
                await self._finish_task(entry, result=TaskResult('check', result.public()), failure=failure)
            except BaseException as error:
                failure = (OwnerError('dispatch_refused', 'check').failure if isinstance(error, asyncio.CancelledError)
                    else failure_for(error, 'check'))
                await self._finish_task(entry, failure=failure)
        return await self._admit(admitted)

    async def stop(self, ref):
        self._assert_mutation()
        if isinstance(ref, ResourceRef) and ref.kind == 'operation':
            value = await self.status(ref)
            if await self._auto_resume.cancel(ref.id):
                return StopReceipt(ref, True, 'requested', value.process)
            runtime = self._operations.get(ref.id)
            if runtime is None or runtime.done.done():
                return StopReceipt(ref, False, 'too_late', value.process)
            async def request():
                current = await self._db('request_operation_stop', ref.id)
                if current.control_state not in {'reserved', 'running', 'stopping'}:
                    return StopReceipt(ref, False, 'too_late', current.process)
                await self._auto_resume.cancel(ref.id, accepted_scope=runtime.scope_id)
                already = runtime.stop.is_set()
                await runtime.request_stop()
                return StopReceipt(ref, True, 'already_requested' if already else 'requested', runtime.process_facts(current.process))
            return await asyncio.shield(self._hold(request()))
        if isinstance(ref, TaskRef):
            task_ref = ref
        elif isinstance(ref, ResourceRef) and ref.kind == 'task':
            current = await self._db('task_status', ref.id)
            task_ref = TaskRef(ref.id, current.owner_generation)
        else:
            raise OwnerError('invalid_argument', 'stop')
        value = await self.task(task_ref)
        if value.action not in {'target_start', 'check', 'conformance'}:
            raise OwnerError('invalid_argument', 'stop')
        entry = self._tasks.get(task_ref.id)
        resource = ResourceRef('task', task_ref.id)
        if entry is None or entry.done.done():
            return StopReceipt(resource, False, 'too_late', ProcessFacts())
        already = entry.stop.is_set()
        entry.stop.set()
        if entry.action == 'conformance':
            runtime = self._conformance.get(task_ref.id)
            if runtime is None:
                return StopReceipt(resource, False, 'too_late', ProcessFacts())
            await asyncio.shield(self._hold(runtime.request_stop()))
            return StopReceipt(
                resource, True, 'already_requested' if already else 'requested',
                runtime.process_facts(ProcessFacts()),
            )
        if entry.observation is not None:
            entry.observation.cancel()  # Read-only H1 observer; thread may finish its GET.
        runtime = self._targets.get(entry.resource.id)
        if entry.action == 'target_start' and runtime is not None:
            await asyncio.shield(self._hold(runtime.request_stop()))
        process = ProcessFacts()
        if entry.action == 'target_start' and runtime is not None and runtime.helper is not None:
            events = runtime.helper.events
            launched = True if any(e['kind'] == 'child_started' for e in events) else None
            exit_code = next((e['data']['exit_code'] for e in reversed(events) if e['kind'] == 'child_exited'), None)
            forced = True if any(e['kind'] == 'force_started' for e in events) else None
            process = ProcessFacts(launched=launched, exit_code=exit_code, forced=forced)
        return StopReceipt(resource, True, 'already_requested' if already else 'requested', process)

    async def health(self):
        self._assert_loop()
        blockers = await self._db('blockers')
        operations = await self._db('list_operations')
        active = next((op.id for op in operations if op.control_state in {'reserved', 'running', 'stopping'}), None)
        lab, core = await asyncio.to_thread(lambda: (version('evolution-lab'), version('evo-skills')))
        return Health(lab, core, (), self.owner_generation, active, blockers,
            'stopping' if self._stopping else 'running')

    async def client_operations(self):
        self._assert_loop()
        return await self._db('client_operations')

    async def cycle_starts(self) -> dict[str, dict]:
        """Read-only: operation id -> ``{"start", "export_id", "export_deleted",
        "results_state"}``, one snapshot.

        ``start`` orders the operation's current start (larger = started later; a resume is
        a new start, ADR-0015). ``export_id`` is the archive published by that same start,
        kept after ``delete_results`` as a mark with ``export_deleted: true`` (its bytes are
        gone, no read returns it). ``results_state`` is the operation's. All from one
        statement, so they never mix starts. Keys are only comparable within this state
        directory."""
        self._assert_loop()
        return await self._db('cycle_starts')

    async def client_targets(self):
        self._assert_loop()
        return await self._db('client_targets')

    async def list_targets(self, *, bundle=None, state=None):
        """Read-only: every durable target, optionally of one Bundle and/or only ``ready``.

        A live target's in-memory status wins over its durable row, as in ``Owner.target``."""
        self._assert_loop()
        if bundle is not None:
            _digest(bundle)
        if state not in {None, 'ready'}:
            raise OwnerError('invalid_argument', 'read')
        # The state filter runs after the merge: a live target may be ready before its
        # durable row says so.
        rows = await self._db('list_targets', bundle, None)
        merged = tuple(self._target_cache.get(row.id, row) for row in rows)
        if state is not None:
            merged = tuple(row for row in merged if row.state == state)
        return merged

    async def client_tasks(self):
        self._assert_loop()
        return await self._db('client_tasks')

    async def published_export(self, ref: ResourceRef):
        """The change-set archive published by the operation's current start, or ``None``.

        Read-only. An earlier start's archive is stale after resume (ADR-0015), so it is
        not reported here even though ``read_export`` can still serve it by id."""
        self._assert_loop()
        if not isinstance(ref, ResourceRef) or ref.kind != 'operation':
            raise OwnerError('invalid_argument', 'read')
        return await self._db('published_export', ref.id)

    async def diff(self, ref: ResourceRef, *, source='initial', selected=None):
        """Read a saved campaign's change between two trees directly, with Owner-held IO.

        ``source``/``selected`` are tree refs (``initial | champion:vN | attempt:<id>``);
        ``selected=None`` is the final champion."""
        self._assert_loop()
        if not isinstance(ref, ResourceRef) or ref.kind != 'operation':
            raise OwnerError('invalid_argument', 'diff')
        from .changes import parse_tree_ref
        parse_tree_ref(source)
        if selected is not None:
            parse_tree_ref(selected)
        if self._stopping or self._closed:
            raise OwnerError('service_stopping', 'diff')
        lease = self._result_io.acquire_read(ref.id, stage='diff')

        async def work():
            try:
                status = await self._export_status(ref, 'diff', producing=True)
                inventory = await self._db('owned_inventory', ref.id)

                def serialize():
                    from .changes import snapshot_changes
                    from .results import diff_snapshot
                    with snapshot_changes(self.state_directory, self._storage,
                            status, inventory, stage='diff',
                            source=source, selected=selected,
                            scratch_directory=self.scratch_directory) as snapshot:
                        return diff_snapshot(snapshot)

                return await self._result_thread(serialize)
            except OwnerError as error:
                if error.stage == 'diff':
                    raise
                raise OwnerError(error.code, 'diff') from None
            except OSError:
                raise OwnerError('io_error', 'diff') from None
            except Exception:
                raise OwnerError('lab_error', 'diff') from None

        return await self._result_work(work, lease)

    async def close(self, *, wait_seconds=30):
        self._assert_loop()
        seconds = positive_seconds(wait_seconds)
        if self._close_task is None:
            self._stopping = True
            self._close_task = asyncio.create_task(self._close(seconds))
        return await asyncio.shield(self._close_task)

    async def _close(self, seconds):
        deadline = self._loop.time() + seconds
        await self._auto_resume.close()
        # Probe sessions close before any target stops (spec 2026-10-02 §3.4), within the same deadline.
        await self._close_all_probes(deadline)
        for runtime in self._operations.values():
            if not runtime.done.done():
                await runtime.request_stop()
        for runtime in self._conformance.values():
            if not runtime.done.done():
                runtime.entry.stop.set()
                await runtime.request_stop()
        for entry in self._tasks.values():
            if not entry.done.done():
                entry.stop.set()
                if entry.observation:
                    entry.observation.cancel()
        while any(not runtime.done.done() for runtime in self._operations.values()) and self._loop.time() < deadline:
            await asyncio.sleep(min(.02, max(0, deadline - self._loop.time())))
        for runtime in self._targets.values():
            await runtime.request_stop()
        while self._owned and self._loop.time() < deadline:
            await asyncio.wait(tuple(self._owned), timeout=min(.05, max(0, deadline - self._loop.time())))
            # Admissions queued during close may become visible only after commit.
            for entry in self._tasks.values():
                if not entry.done.done():
                    entry.stop.set()
        for runtime in self._targets.values():
            if runtime.helper:
                runtime.helper.close_commands()
                runtime.helper.close_target_ready()
                runtime.helper.close_events()
        for runtime in self._operations.values():
            if runtime.reflector_probe is not None:
                runtime.reflector_probe.close_channels()
            if runtime.helper:
                runtime.helper.close_commands()
                runtime.helper.close_target_ready()
                runtime.helper.close_events()
        for runtime in self._conformance.values():
            if runtime.helper:
                runtime.helper.close_commands()
                runtime.helper.close_target_ready()
                runtime.helper.close_events()
        await self._drain_result_work()
        blockers = await self._db('blockers')
        self._shutdown_result = ShutdownResult(self.owner_generation,
            CleanupFacts('unconfirmed', None) if blockers else CleanupFacts('confirmed', True), blockers)
        await self._db('close')
        self._closed = True
        self._executor.shutdown(wait=False)
        return self._shutdown_result
