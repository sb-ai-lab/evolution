"""V-S2-5: conservative reuse, full associated cleanup and manual disposition."""
import asyncio
import json

import pytest

from test_owner_target import make_bundle, ready_target

pytestmark = pytest.mark.slow


def prepared_target(tmp_path):
    from evolution_lab.preparation import PreparationStore
    state, bundle, _ = make_bundle(tmp_path, max_instances=4)
    return state, bundle, PreparationStore(state).prepare_campaign(bundle.digest)


async def finished(owner, ref, seconds=20):
    """The status once the operation is settled: durably out of its busy states, and its runtime's
    own finalization (policy use, probe notes) done. Until then resume refuses with resource_busy
    (#475). The runtime's future is shielded: the timeout must not cancel the Owner's own future."""
    async with asyncio.timeout(seconds):
        while True:
            value = await owner.status(ref)
            if value.control_state not in {'reserved', 'running', 'stopping'}:
                break
            await asyncio.sleep(.02)
        runtime = owner._operations.get(ref.id)
        if runtime is None or runtime.done.done():
            return value
        await asyncio.shield(runtime.done)
        return await owner.status(ref)


def test_uncertain_settlement_requires_associated_target_cleanup(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef, OwnerError
    from evolution_lab.owner import cleanup
    original = cleanup.read_campaign_settlement
    def uncertain(*args, **kwargs):
        result = original(*args, **kwargs)
        result["ledger"]["uncertain_any"] = True
        return result
    monkeypatch.setattr(cleanup, 'read_campaign_settlement', uncertain)
    state, bundle, prepared = prepared_target(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', operation.id)
            value = await finished(owner, ref)
            assert value.control_state == 'cleanup_required'
            assert value.process.cleanup_complete is True
            assert value.target_cleanup.complete is None
            with pytest.raises(OwnerError):
                await owner.start(prepared.digest, target_ref=target, secrets={})
            task = await owner.cleanup(ref)
            with pytest.raises(OwnerError) as error:
                await owner.stop(task)
            assert error.value.code == 'invalid_argument'
            assert (await owner.wait(task)).state == 'succeeded'
            assert (await owner.target(target)).state == 'stopped'
            assert not (await owner.health()).blockers
            assert (await owner.status(ref)).result.core is not None
    asyncio.run(scenario())


def test_force_retains_blocker_until_full_cleanup(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    state, bundle, prepared = prepared_target(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            runtime = owner._operations[operation.id]
            async with asyncio.timeout(8):
                while runtime.helper is None:
                    await asyncio.sleep(.01)
            await runtime.helper.wait_event('child_started', timeout=5)
            await runtime.helper.force()
            ref = ResourceRef('operation', operation.id)
            value = await finished(owner, ref)
            assert value.control_state == 'cleanup_required'
            assert value.process.forced is True
            cleanup_task = await owner.cleanup(ref)
            assert (await owner.wait(cleanup_task)).state == 'succeeded'
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


def test_forced_cleanup_does_not_show_a_terminal_state_before_persistence(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.api import Owner as OwnerClass
    from test_owner_campaign import _wait_for
    state, bundle, prepared = prepared_target(tmp_path)
    original = OwnerClass._db
    watched = {}
    seen = []

    async def snapshot_after_receipt(self, method, *args, **kwargs):
        result = await original(self, method, *args, **kwargs)
        # A forced receipt commits control_state cleanup_required before the result is written.
        if method == 'confirm_scope_cleanup' and 'ref' in watched:
            seen.append(await self.status(watched['ref']))
        return result

    monkeypatch.setattr(OwnerClass, '_db', snapshot_after_receipt)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = watched['ref'] = ResourceRef('operation', operation.id)
            runtime = owner._operations[operation.id]
            await _wait_for(state / 'artifacts' / operation.id / operation.id / 'campaign.jsonl')
            await runtime.helper.force()
            value = await finished(owner, ref)
            assert value.process.forced is True and value.control_state == 'cleanup_required'
            assert value.result.persistence.state == 'saved'
            assert seen and seen[0].result is not None
            assert [(v.control_state, v.result.persistence.state) for v in seen
                    if v.control_state not in {'reserved', 'running', 'stopping'}
                    and v.result.persistence.state == 'not_attempted'] == []
            cleanup_task = await owner.cleanup(ref)
            assert (await owner.wait(cleanup_task)).state == 'succeeded'
    asyncio.run(scenario())


def test_failed_delete_cannot_be_repaired_by_empty_health(tmp_path, monkeypatch):
    from evolution_lab.owner.cleanup import sweep_namespace
    from evolution_lab.owner.binding import open_bundle
    from evolution_lab.preparation.storage import PreparationStorage
    from evolution_lab.owner import Owner, OwnerError
    from evolution.tree.target.http_adapter import HttpTransport
    from evolution.tree.execution.wire_v2 import is_instance_id
    state, bundle, _ = prepared_target(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            status = await owner.target(target)
            binding = open_bundle(PreparationStorage(state), bundle.digest)
            run_id = 'c-' + 'a' * 32
            own = run_id + '-' + 'b' * 32
            assert is_instance_id(own, run_id)
            foreign = 'c-' + 'c' * 32 + '-' + 'd' * 32
            health = dict(status.to_payload()['health'])
            calls = []
            async def request(self, method, path, payload=None):
                calls.append((method, path))
                if method == 'DELETE':
                    return 500, b'{}', 'application/json'
                health['instances'] = [own, foreign] if len(calls) == 1 else []
                return 200, json.dumps(health).encode(), 'application/json'
            with monkeypatch.context() as local:
                local.setattr(HttpTransport, 'request', request)
                with pytest.raises(OwnerError):
                    await sweep_namespace(binding, owner.state_directory, target, status.boot_id, run_id)
            assert ('DELETE', '/evolution/v2/instances/' + own) in calls
            assert all(foreign not in path for _, path in calls)
    asyncio.run(scenario())


def test_manual_release_preserves_unknown_and_late_receipt_cannot_free_new_target(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner import cleanup, target as target_module
    state, bundle, prepared = prepared_target(tmp_path)
    def unavailable(*args, **kwargs):
        raise OSError()
    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            with monkeypatch.context() as local:
                local.setattr(cleanup, 'read_cleanup_receipt', unavailable)
                local.setattr(target_module, 'read_cleanup_receipt', unavailable)
                operation = await owner.start(prepared.digest, target_ref=target, secrets={})
                ref = ResourceRef('operation', operation.id)
                value = await finished(owner, ref)
                assert value.control_state == 'cleanup_required'
                await owner._targets[target.id].request_stop()
                await owner._targets[target.id].ended
            old_scopes = await owner._db('resource_scopes', ref, include_target=True)
            receipt = await owner.release_after_manual_cleanup(ref, reason='External remnants and fixed port cleared')
            assert receipt.cleanup.complete is None and len(receipt.released_scopes) == 2
            previous = await owner.status(ref)
            assert previous.process.cleanup_complete is None and previous.target_cleanup.complete is None
            new_task = await owner.start_target(connection, secrets={'TARGET_TOKEN':'private-neutral-token'})
            assert (await owner.wait(new_task)).state == 'succeeded'
            before = await owner._db('reservations')
            for scope in old_scopes:
                await owner._db('confirm_scope_cleanup', scope['id'], scope['owner_generation'], forced=False)
            assert await owner._db('reservations') == before
            assert before['target_lifetime'][0] not in receipt.released_scopes
            assert (await owner.status(ref)).result.core == previous.result.core
    asyncio.run(scenario())


def test_close_deadline_and_cancelled_wait_preserve_live_campaign_blockers(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef, OwnerError
    state, bundle, prepared = prepared_target(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            runtime = owner._operations[operation.id]
            async with asyncio.timeout(8):
                while runtime.helper is None:
                    await asyncio.sleep(.01)
            await runtime.helper.wait_event('child_started', timeout=5)
            caller = asyncio.create_task(owner.close(wait_seconds=.000001))
            await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            result = await owner.close()
            assert result.cleanup.complete is None and result.blockers
            with pytest.raises(OwnerError) as error:
                await owner.start(prepared.digest, target_ref=target, secrets={})
            assert error.value.code == 'service_stopping'
        async with Owner.open(state) as reopened:
            ref = ResourceRef('operation', operation.id)
            assert (await reopened.status(ref)).control_state == 'orphaned'
            cleaned = await reopened.wait(await reopened.cleanup(ref, wait_seconds=8))
            assert cleaned.state == 'succeeded', cleaned.to_payload()
            assert not (await reopened.health()).blockers
    asyncio.run(scenario())
