"""Loop-local fail-fast result guards and Owner-held filesystem lifetimes.

Acquire a lease synchronously, then transfer it immediately to _result_work or
_hold_result. A lease must never be owned by a cancellable public caller's finally.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .models import OwnerError, ResourceRef, TaskStatus


@dataclass
class _Access:
    readers: int = 0
    writer: bool = False
    exporting: bool = False


class ResultLease:
    def __init__(self, access, kind):
        self._access = access
        self._kind = kind
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        if self._kind == 'write':
            self._access.writer = False
        else:
            self._access.readers -= 1
            if self._kind == 'export':
                self._access.exporting = False


class OperationResultIO:
    """Only use on the Owner event loop; no waiting or filesystem access."""
    def __init__(self):
        self._operations = {}

    def _acquire(self, operation_id, stage, kind):
        access = self._operations.setdefault(operation_id, _Access())
        if (access.writer or (kind == 'write' and access.readers)
                or (kind == 'export' and access.exporting)):
            raise OwnerError('resource_busy', stage)
        if kind == 'write':
            access.writer = True
        else:
            access.readers += 1
            if kind == 'export':
                access.exporting = True
        return ResultLease(access, kind)

    def acquire_read(self, operation_id, stage='read'):
        return self._acquire(operation_id, stage, 'read')

    def acquire_write(self, operation_id, stage='delete'):
        return self._acquire(operation_id, stage, 'write')

    def acquire_export(self, operation_id, stage='export'):
        return self._acquire(operation_id, stage, 'export')

    def held(self, operation_id):
        """Any read, export or write lease is currently held for the operation."""
        access = self._operations.get(operation_id)
        return access is not None and (access.writer or access.readers > 0)

    def writing(self, operation_id):
        access = self._operations.get(operation_id)
        return access is not None and access.writer


class ResultIOSupport:
    def _completed_deletion(self, operation_id, task_id):
        """Read exact live completion for retry while holding the new write lease.

        A completed result Future is insufficient: its runner may still finalize
        filesystem work. Never synthesize completion from the durable task/cache.
        """
        self._assert_loop()
        ref = ResourceRef('operation', operation_id)
        if not self._result_io.writing(operation_id):
            raise OwnerError('invalid_argument', 'delete')
        entry = self._tasks.get(task_id)
        if entry is None:
            return None
        if (entry.ref.id != task_id or entry.ref.owner_generation != self.owner_generation
                or entry.action != 'delete' or entry.resource != ref):
            raise OwnerError('invalid_argument', 'delete')
        if (not entry.done.done() or entry.done.cancelled()
                or entry.runner is None or not entry.runner.done()):
            raise OwnerError('resource_busy', 'delete')
        completed = entry.done.result()
        if (not isinstance(completed, TaskStatus)
                or completed.id != task_id
                or completed.owner_generation != self.owner_generation
                or completed.action != 'delete' or completed.ref != ref
                or completed.state not in {'succeeded', 'failed', 'unknown'}):
            raise OwnerError('invalid_argument', 'delete')
        return completed

    def _hold_result(self, factory, lease=None):
        """Immediately take ownership of a lease and an async factory's full IO.

        Returns an Owner-held Task. Export/delete admissions should place their
        entire admission + finalization factory here, not merely their thread call.
        """
        try:
            self._assert_loop()
            if self._stopping or self._closed:
                raise OwnerError('service_stopping', 'read')
        except BaseException:
            if lease is not None:
                lease.release()
            raise
        async def run():
            try:
                return await factory()
            finally:
                if lease is not None:
                    lease.release()
        task = self._hold(run())
        self._result_owned.add(task)
        task.add_done_callback(self._result_owned.discard)
        return task

    def _result_work(self, factory, lease=None):
        """Transfer ownership before returning the caller's awaitable."""
        task = self._hold_result(factory, lease)
        async def wait():
            return await asyncio.shield(task)
        return wait()

    async def _result_thread(self, function, *args, **kwargs):
        """Do not unwind a lease owner until its worker thread has actually ended."""
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
                if worker.done():
                    # Retrieve exceptions before propagating cancellation.
                    worker.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _drain_result_work(self):
        while self._result_owned:
            await asyncio.gather(*tuple(self._result_owned), return_exceptions=True)
