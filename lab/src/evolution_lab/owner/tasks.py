"""Owner-held admissions and task results; caller waits never own execution."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace

from .models import (
    Failure, OwnerError, Persistence, ResourceRef, TaskRef, TaskResult, TaskStatus,
)


def failure_for(error: BaseException, stage: str) -> Failure:
    if isinstance(error, OwnerError):
        return error.failure
    if isinstance(error, OSError):
        return Failure.make('io_error', stage)
    failure = getattr(error, 'failure', None)
    if isinstance(failure, Failure):
        return replace(failure, stage=stage)
    if failure is not None:
        try:
            return Failure.make(failure.code, stage)
        except OwnerError:
            pass
    return Failure.make('lab_error', stage)


def positive_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise OwnerError('invalid_argument', 'stop')
    return float(value)


@dataclass
class OwnedTask:
    ref: TaskRef
    action: str
    resource: object
    done: asyncio.Future
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task | None = None
    observation: asyncio.Task | None = None


class TaskSupport:
    def _hold(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._owned.add(task)
        def completed(value):
            self._owned.discard(value)
            if not value.cancelled():
                value.exception()  # Exception belongs to admission/result, not loop logging.
        task.add_done_callback(completed)
        return task

    async def _admit(self, factory):
        self._assert_mutation()
        accepted = self._loop.create_future()
        # Retrieve failure even when the only caller cancels its wait.
        accepted.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        async def run():
            try:
                await factory(accepted)
            except BaseException as error:
                if not accepted.done():
                    accepted.set_exception(OwnerError(failure_for(error, 'before_start').code, 'before_start'))
        self._hold(run())
        return await asyncio.shield(accepted)

    def _register_task(self, ref, action, resource):
        entry = OwnedTask(ref, action, resource, self._loop.create_future())
        entry.runner = asyncio.current_task()
        self._tasks[ref.id] = entry
        return entry

    async def _finish_task(self, entry, *, result=None, persisted_result=None,
                           failure=None, state=None, persistence_failure=None):
        try:
            status = await self._db(
                'finish_task', entry.ref.id,
                result=result if persisted_result is None else persisted_result,
                failure=failure, state=state,
                persistence_failure=persistence_failure,
            )
            if result is not None and result != status.result:
                status = replace(status, result=result)
        except BaseException as error:
            persisted = failure_for(error, 'finalizing')
            status = TaskStatus(entry.ref.id, self.owner_generation, entry.action, entry.resource,
                state or ('failed' if failure else 'succeeded'), result, failure,
                Persistence('failed', persisted))
        self._task_cache[entry.ref.id] = status
        if not entry.done.done():
            entry.done.set_result(status)
        return status

    async def task(self, task_ref):
        self._assert_loop()
        if not isinstance(task_ref, TaskRef):
            raise OwnerError('invalid_argument', 'read')
        status = self._task_cache.get(task_ref.id)
        if status is None:
            status = await self._db('task_status', task_ref.id)
        if status.owner_generation != task_ref.owner_generation:
            raise OwnerError('not_found', 'read')
        if status.action == 'export' and status.ref is not None:
            async def read_result():
                operation = await self._db('operation_status', status.ref.id)
                no_change = (status.result is not None
                    and status.result.payload.get('state') == 'no_change')
                if operation.results_state in {'deleting', 'delete_failed', 'deleted'}:
                    return status if no_change else replace(status, result=None)
                projector = getattr(self, '_project_export_task', None)
                return status if projector is None else await projector(status)
            if self._result_io.writing(status.ref.id):
                # A historical no_change fact has no private archive payload.
                no_change = (status.result is not None
                    and status.result.payload.get('state') == 'no_change')
                return status if no_change else replace(status, result=None)
            return await self._result_work(read_result,
                self._result_io.acquire_read(status.ref.id, 'read'))
        if status.action == 'conformance' and status.result is not None:
            from .conformance import project_conformance_task
            scopes = await self._db('resource_scopes', ResourceRef('task', task_ref.id))
            scope_directory = (
                self.state_directory / scopes[0]['root_relative'] if scopes else None
            )
            status = await asyncio.to_thread(
                project_conformance_task, status, scope_directory=scope_directory
            )
        return status

    async def wait(self, task_ref):
        await self.task(task_ref)
        entry = self._tasks.get(task_ref.id)
        if entry is not None:
            await asyncio.shield(entry.done)
        return await self.task(task_ref)
