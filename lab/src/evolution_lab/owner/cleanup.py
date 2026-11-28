"""Positive campaign settlement and exact, explicitly requested scope cleanup."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from .settlement import (positive_settlement, read_campaign_settlement,
    read_evaluation_settlement)
from .models import (Approval, CleanupFacts, Failure, OwnerError, Persistence, ProcessFacts,
    ResourceRef, TargetRef, TaskRef, TaskResult)
from .diagnostic import read_campaign_core_terminal, read_campaign_refusal, read_campaign_findings, read_worker_failure
from .receipts import (decode_json, publish_cleanup_request, publish_json,
    read_cleanup_receipt)
from .target import matched_health, runtime_config
from .tasks import failure_for, positive_seconds


async def sweep_namespace(binding, root, target_ref, boot_id, run_id, *, scratch=None):
    from evolution.tree.target.http_adapter import HttpTransport
    from evolution.tree.execution.wire_v2 import is_instance_id, parse_health, parse_release, parse_error
    config = runtime_config(root, binding, target_ref, scratch=scratch)
    try:
        first = parse_health(await matched_health(binding, config, boot_id))
        owned = tuple(item for item in first.instances if is_instance_id(item, run_id))
        transport = HttpTransport(binding.manifest.target)
        for instance_id in owned:
            status, raw, media = await transport.request('DELETE', '/evolution/v2/instances/' + instance_id, None)
            if media.split(';')[0].strip().lower() != 'application/json':
                raise ValueError()
            payload = decode_json(raw, max_bytes=binding.manifest.target.max_response_bytes)
            if status == 200:
                if parse_release(payload).instance_id != instance_id:
                    raise ValueError()
            elif status != 404 or parse_error(status, payload).code != 'unknown_instance':
                raise ValueError()
        last = parse_health(await matched_health(binding, config, boot_id))
        if any(is_instance_id(item, run_id) for item in last.instances):
            raise ValueError()
        return dict(boot_id=boot_id, run_id=run_id, released_instance_ids=list(owned), namespace_empty=True)
    except Exception:
        raise OwnerError('cleanup_unconfirmed', 'cleanup') from None


class CleanupSupport:
    async def _finalize_operation(self, runtime):
        from .readers import project_operation
        ref = ResourceRef('operation', runtime.id)
        value = await self._db('operation_status', runtime.id)
        # A forced receipt commits control_state cleanup_required (confirm_scope_cleanup): readers keep the
        # state they had until the result is written with its persistence outcome.
        self._operation_cache.setdefault(runtime.id, value)
        process = runtime.process_facts(value.process)
        receipt = None
        try:
            receipt = await asyncio.to_thread(read_cleanup_receipt, runtime.paths.scope_directory,
                runtime.scope_id, self.owner_generation)
            process = ProcessFacts(receipt['child_launched'], receipt['child_exit_code'], True,
                process.forced is True or receipt['forced'])
            await self._db('confirm_scope_cleanup', runtime.scope_id, self.owner_generation, forced=process.forced)
        except (OSError, ValueError):
            pass
        value = replace(value, process=process,
            target_cleanup=CleanupFacts('unconfirmed', None))
        refusal = None
        if runtime.prepared.kind == 'campaign':
            refusal = await asyncio.to_thread(read_campaign_refusal, runtime.paths.diagnostic_path,
                operation_id=runtime.id, scope_id=runtime.scope_id)
            core_terminal = await asyncio.to_thread(read_campaign_core_terminal, runtime.paths.diagnostic_path,
                operation_id=runtime.id, scope_id=runtime.scope_id)
            if core_terminal is not None:
                runtime.core_terminal = core_terminal
                value = replace(value, core_terminal=core_terminal)
        if refusal is not None:
            findings = await asyncio.to_thread(read_campaign_findings, runtime.paths.diagnostic_path,
                operation_id=runtime.id, scope_id=runtime.scope_id)
            if findings:
                await self._db('record_refusal', runtime.id, start=value.starts[-1].start, findings=findings)
            runtime.refusal = refusal
            value = replace(value, failure=Failure.make('core_refused', 'running'), refusal=refusal)
        elif value.failure is None and value.refusal is None and value.core_terminal is None:
            failure = await asyncio.to_thread(read_worker_failure, runtime.paths.diagnostic_path,
                operation_id=runtime.id, scope_id=runtime.scope_id, kind=runtime.prepared.kind)
            if failure is not None:
                value = replace(value, failure=failure)
        value = await asyncio.to_thread(
            project_operation,
            self.state_directory,
            value,
            scope_directory=runtime.paths.scope_directory,
        )
        # A probe receipt can already have committed cleanup_required while the
        # worker was running. Keep the pre-terminal cache until persistence settles,
        # just as for a forced worker receipt read in this finalizer.
        if value.control_state not in {'finished', 'cleanup_required', 'released', 'orphaned'}:
            self._operation_cache[runtime.id] = value
        positive = False
        if receipt is not None:
            if receipt['child_launched'] is False and process.forced is False:
                positive = True
            elif process.forced is False:
                try:
                    from evolution.tree.evolve import derive_run_id

                    run_id = derive_run_id(runtime.paths.run_directory)
                    read_settlement = (
                        read_evaluation_settlement
                        if runtime.prepared.kind == "evaluation"
                        else read_campaign_settlement
                    )
                    settled = await asyncio.to_thread(
                        read_settlement,
                        runtime.paths.settlement_path,
                        operation_id=runtime.id,
                        scope_id=runtime.scope_id,
                        owner_generation=self.owner_generation,
                        run_id=run_id,
                    )
                    if not positive_settlement(settled, run_id=run_id,
                            kind=runtime.prepared.kind):
                        raise ValueError()
                    target = await self._db('target_status', runtime.target.ref.id)
                    if target.state != 'ready' or runtime.target.stop.is_set():
                        raise ValueError()
                    facts = await sweep_namespace(runtime.prepared.bundle, self.state_directory,
                        runtime.target.ref, target.boot_id, run_id, scratch=self.scratch_directory)
                    await asyncio.to_thread(publish_json, runtime.paths.scope_directory / 'reuse.json',
                        dict(schema=f'lab-{runtime.prepared.kind}-reuse/1', operation_id=runtime.id,
                            scope_id=runtime.scope_id, owner_generation=self.owner_generation, **facts))
                    positive = True
                except (OwnerError, OSError, ValueError):
                    pass
        scopes = await self._db('resource_scopes', ref)
        positive = positive and all(scope['cleanup_complete'] == 1 and scope['forced'] != 1 for scope in scopes)
        if positive:
            value = replace(value, target_cleanup=CleanupFacts('confirmed', True))
        else:
            await self._db('mark_operation_cleanup_required', runtime.id)
            value = replace(value, control_state='cleanup_required')
        persisted = value
        if value.result is not None:
            persisted = replace(
                value,
                result=replace(value.result, persistence=Persistence('saved')),
            )
        await self._db('update_operation', runtime.id, process=persisted.process,
            target_cleanup=persisted.target_cleanup, result=persisted.result,
            failure=persisted.failure, refusal=persisted.refusal, core_terminal=persisted.core_terminal,
            results_state=persisted.results_state)
        self._operation_cache[runtime.id] = persisted
        if positive:
            await self._db('release_evidenced_reservations', ref, scope_ids=tuple(scope['id'] for scope in scopes), disposition='normal')
            await self._discard_released_snapshots()
        self._operation_cache.pop(runtime.id, None)

    async def _cleanup_ref(self, ref, *, observed_start=None, stage='cleanup'):
        if isinstance(ref, TargetRef):
            await self.target(ref)
            return ResourceRef('target', ref.id)
        if isinstance(ref, TaskRef):
            await self.task(ref)
            return ResourceRef('task', ref.id)
        if not isinstance(ref, ResourceRef):
            raise OwnerError('invalid_argument', 'cleanup')
        if ref.kind == 'operation':
            status = await self._db('operation_status', ref.id)
            if observed_start is not None and status.starts[-1].start != observed_start:
                raise OwnerError('state_changed', stage)
        elif ref.kind == 'target':
            await self._db('target_status', ref.id)
        else:
            await self._db('task_status', ref.id)
        return ref

    async def _cleanup_scope_ref(self, ref):
        if ref.kind == 'task':
            task = await self._db('task_status', ref.id)
            if task.action == 'target_start' and task.ref is not None:
                return task.ref
        return ref

    def _completed_conformance_for_cleanup(self, ref):
        if ref.kind != 'task':
            return None
        runtime = self._conformance.get(ref.id)
        if runtime is None:
            return None
        # Stop/receipts alone do not end the Owner-owned finalizer. Its live
        # completion remains authoritative if the terminal DB write failed.
        if not runtime.done.done():
            raise OwnerError('resource_busy', 'before_start')
        return runtime.entry.done.result()

    async def cleanup(self, ref, *, wait_seconds=None, approval: Approval | None = None,
                      request_id: str | None = None, observed_start: int | None = None):
        return await self._cleanup(ref, wait_seconds=wait_seconds, require_probe_free=False,
                                   approval=approval, request_id=request_id, observed_start=observed_start)

    async def _cleanup_auto(self, ref):
        return await self._cleanup(ref, wait_seconds=None, require_probe_free=True)

    async def _admit_cleanup_action(self, factory, *, stage, approval):
        try:
            return await self._admit(factory)
        except OwnerError as error:
            # The shared admission runner maps failures to before_start.
            if approval is not None and error.code == 'state_changed':
                raise OwnerError(error.code, stage) from None
            raise

    async def _cleanup(self, ref, *, wait_seconds, require_probe_free, approval: Approval | None = None,
                       request_id: str | None = None, observed_start: int | None = None):
        self._assert_mutation()
        ref = await self._cleanup_ref(ref, observed_start=observed_start)
        scoped = await self._cleanup_scope_ref(ref)
        runtime = self._operations.get(scoped.id) if scoped.kind == 'operation' else None
        if (runtime is not None and not runtime.done.done() and not runtime.stop.is_set()
            and (runtime.helper is None or runtime.helper.process.poll() is None)):
            raise OwnerError('busy', 'cleanup')
        if scoped.kind == 'target' and await self._db('target_has_operation', scoped.id):
            raise OwnerError('busy', 'cleanup')
        if wait_seconds is None:
            seconds = 30
            if scoped.kind == 'operation':
                from .binding import open_prepared
                operation = await self._db('operation_status', scoped.id)
                prepared = await asyncio.to_thread(open_prepared, self._storage, operation.prepared_digest)
                seconds = prepared.runtime_policy.cleanup_wait_seconds
        else:
            seconds = positive_seconds(wait_seconds)
        approval_kwargs = {} if approval is None else dict(
            approval=approval, request_id=request_id, observed_start=observed_start)

        async def run_cleanup(accepted):
            completed = self._completed_conformance_for_cleanup(scoped)
            reservation = await self._db('accept_task', action='cleanup', ref=ref,
                completed_conformance=completed, require_probe_free=require_probe_free, **approval_kwargs)
            entry = self._register_task(reservation.task_ref, 'cleanup', ref)
            accepted.set_result(entry.ref)
            try:
                await self._db('task_running', entry.ref.id)
                async with asyncio.timeout(seconds):
                    if runtime is not None:
                        await runtime.request_stop()
                        await asyncio.shield(runtime.done)
                    await self._clean_scopes(scoped)
                await self._finish_task(entry, result=TaskResult('cleanup', CleanupFacts('confirmed', True).to_payload()))
            except BaseException as error:
                failure = Failure.make('cleanup_unconfirmed', 'cleanup') if isinstance(error, TimeoutError) else failure_for(error, 'cleanup')
                await self._finish_task(entry, result=TaskResult('cleanup', CleanupFacts('unconfirmed', None).to_payload()), failure=failure)

        async def admitted(accepted):
            self._assert_mutation()
            # Resume checks result leases. Keep the operation's start current
            # until the Owner-held cleanup has read, cleaned and settled its scopes.
            lease = (self._result_io.acquire_read(scoped.id, 'cleanup')
                     if scoped.kind == 'operation' else None)
            try:
                await run_cleanup(accepted)
            finally:
                if lease is not None:
                    lease.release()

        return await self._admit_cleanup_action(admitted, stage='cleanup', approval=approval)

    async def _clean_scopes(self, ref):
        scopes = await self._db(
            'resource_scopes', ref, include_target=ref.kind in {'operation', 'task'}
        )
        if not scopes:
            return
        # A completed normal operation no longer owns any target resources.
        if ref.kind == 'operation':
            operation_scope = next(scope for scope in scopes if scope['resource_kind'] == 'operation')
            if all(scope['state'] == 'released' for scope in scopes if scope['operation_id'] == ref.id):
                await self._repair_released_cleanup_fact(ref, operation_scope)
                return
        for scope in scopes:
            if scope['state'] == 'released':
                continue
            if scope['target_id'] is not None:
                runtime = self._targets.get(scope['target_id'])
                if runtime is not None:
                    runtime.startup.stop.set()
                    await runtime.request_stop()
            if scope['helper_possible']:
                await asyncio.to_thread(publish_cleanup_request, self.state_directory / scope['root_relative'],
                    scope['id'], scope['owner_generation'])
            else:
                await self._db('settle_scope_no_helper', scope['id'])
        while True:
            complete = True
            for scope in scopes:
                if scope['cleanup_complete'] == 1 or not scope['helper_possible']:
                    continue
                try:
                    receipt = await asyncio.to_thread(read_cleanup_receipt, self.state_directory / scope['root_relative'],
                        scope['id'], scope['owner_generation'])
                    await self._db('confirm_scope_cleanup', scope['id'], scope['owner_generation'],
                        forced=receipt['forced'] or scope['forced'] == 1, child_exit_code=receipt['child_exit_code'])
                except (OSError, ValueError):
                    complete = False
            if complete:
                break
            await asyncio.sleep(.05)
        await self._db('release_evidenced_reservations', ref,
            scope_ids=tuple(scope['id'] for scope in scopes), disposition='confirmed_cleanup')
        await self._discard_released_snapshots()  # the release may have released probe rows (spec §3.5)
        if ref.kind == 'operation':
            await self._db('update_operation', ref.id, target_cleanup=CleanupFacts('confirmed', True))
            self._operation_cache.pop(ref.id, None)

    async def _repair_released_cleanup_fact(self, ref, scope):
        # The confirmed-cleanup release and the operation's target_cleanup fact
        # are two commits; an Owner that dies between them leaves a released
        # operation whose fact still says pending/unconfirmed. The scope's
        # confirmed evidence is durable, so a later cleanup rewrites the fact.
        # A manual release (scope cleanup_state 'manual') is never repaired.
        if scope['cleanup_state'] not in {'confirmed', 'not_needed'} or scope['cleanup_complete'] != 1:
            return
        current = await self._db('operation_status', ref.id)
        if current.control_state != 'released':
            return
        if current.target_cleanup.state in {'confirmed', 'not_needed', 'manual'}:
            return
        await self._db('update_operation', ref.id, target_cleanup=CleanupFacts('confirmed', True))
        self._operation_cache.pop(ref.id, None)

    async def release_after_manual_cleanup(self, ref, *, reason, approval: Approval | None = None,
                                           request_id: str | None = None, observed_start: int | None = None):
        self._assert_mutation()
        ref = await self._cleanup_ref(ref, observed_start=observed_start, stage='release')
        scoped = await self._cleanup_scope_ref(ref)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise OwnerError('invalid_argument', 'release')
        runtime = self._operations.get(scoped.id) if scoped.kind == 'operation' else None
        if runtime is not None and not runtime.done.done() and not runtime.stop.is_set():
            raise OwnerError('busy', 'release')
        if scoped.kind == 'target' and await self._db('target_has_operation', scoped.id):
            raise OwnerError('busy', 'release')
        approval_kwargs = {} if approval is None else dict(
            approval=approval, request_id=request_id, observed_start=observed_start, approval_ref=ref)

        async def admitted(accepted):
            self._assert_mutation()
            completed = self._completed_conformance_for_cleanup(scoped)
            receipt = await self._db('release_after_manual_cleanup', scoped, reason=reason,
                include_target=scoped.kind == 'operation', completed_conformance=completed, **approval_kwargs)
            await self._discard_released_snapshots()
            self._operation_cache.pop(scoped.id, None)
            accepted.set_result(replace(receipt, ref=ref))
        return await self._admit_cleanup_action(admitted, stage='release', approval=approval)
