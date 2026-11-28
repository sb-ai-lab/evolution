"""Approved neutral S4 deletion boundaries and Owner state fixtures."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from evolution_lab.owner import Owner, OwnerError, ResourceRef, Failure, OperationResult, ProcessFacts
from evolution_lab.owner import models
from evolution_lab.owner.store import OwnerStateStore
from test_owner_results_state import accepted, released
from test_owner_state import _ready_target, _executor, TWO_DIGEST, FIRST_OPERATION_ID

REF = ResourceRef('operation', FIRST_OPERATION_ID)


async def register(owner, *, kind='campaign', finish=True):
    target = await owner._loop.run_in_executor(owner._executor, _ready_target, owner._store)
    operation = await owner._db('accept_operation', prepared_digest=TWO_DIGEST,
        target_ref=target, executor=replace(_executor(), kind=f'lab-{kind}-worker/1'),
        kind=kind, operation_id=REF.id)
    if finish:
        await owner._db('settle_pre_dispatch_failure', REF.id,
            Failure.make('dispatch_refused', 'before_start'))
    return operation


def files(state, inventory):
    for row in inventory:
        path = state / row['path']
        if row['kind'] == 'tree':
            path = path / REF.id / 'private'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'private result')


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_delete_owned_results_retains_control_preparation_and_summary(tmp_path, monkeypatch, kind):
    async def scenario():
        state = tmp_path / 'state'
        async with Owner.open(state) as owner:
            operation = await register(owner, kind=kind)
            inventory = await owner._db('owned_inventory', REF.id)
            files(state, inventory)
            preserved = [state / 'scopes' / operation.scope_id / name
                for name in ('receipt.json', 'settlement.json', 'stop.mailbox')]
            preserved += [state / 'bundles' / 'immutable', state / 'prepared' / 'immutable',
                state / 'targets' / 'snapshot', tmp_path / 'external-copy']
            for path in preserved:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'keep')
            known = OperationResult(TWO_DIGEST, ProcessFacts(), kind=kind,
                coverage=models.Coverage(4, 2), stopped_reason='requested',
                core={'private': 'do not retain', 'campaign_outcome': {
                    'outcome': 'stopped', 'reason': 'requested',
                    'continuation': {'kind': 'none', 'reason': None}}})
            await owner._db('update_operation', REF.id, result=known, results_state='present')
            owner._operation_cache[REF.id] = await owner._db('operation_status', REF.id)
            assert (await owner.status(REF)).retained_summary is None
            # Readonly owned trees and symlink leaves must not affect destinations.
            root = state / 'artifacts' / REF.id
            (root / 'outside-link').symlink_to(tmp_path / 'external-copy')
            (root / REF.id).chmod(0o500)
            root.chmod(0o500)
            before = await owner._db('reservations')
            task = await owner.delete_results(REF)
            terminal = await owner.wait(task)
            assert terminal.state == 'succeeded' and terminal.persistence.state == 'saved'
            assert terminal.result.payload['state'] == 'deleted'
            assert all(not (state / row['path']).exists() for row in inventory)
            assert all(path.read_bytes() == b'keep' for path in preserved)
            assert await owner._db('reservations') == before  # target lifetime stays independent
            assert (await owner._db('target_status', operation.status.target_ref.id)).state == 'ready'
            status = await owner.status(REF)
            assert status.results_state == 'deleted' and status.result.core is None
            assert status.result.coverage == models.Coverage(4, 2)
            assert status.result.stopped_reason == 'requested'
            summary = status.to_payload()['retained_summary']
            assert set(summary) == {'campaign_outcome', 'dispositions'}
            assert summary['campaign_outcome']['outcome'] == 'stopped'
            assert status.result.campaign_execution is None
            assert 'do not retain' not in json.dumps(status.to_payload())
            assert (await owner.list_operations())[0] == status
            assert (await owner.report(REF)).reason == 'results_deleted'
            assert (await owner.report_markdown(REF)).reason == 'results_deleted'
            for call in (lambda: owner.diff(REF), lambda: owner.export(REF),
                         lambda: owner.read_export(REF, 'a' * 64)):
                with pytest.raises(OwnerError) as error:
                    await call()
                assert error.value.code == 'results_deleted'
            import evolution_lab.owner.results as result_module
            monkeypatch.setattr(result_module, 'delete_inventory',
                lambda *a: pytest.fail('deleted repeat traversed files'))
            assert (await owner.wait(await owner.delete_results(REF))).result.payload['state'] == 'deleted'
            assert (await owner.status(REF)).to_payload()['retained_summary'] == summary
        async with Owner.open(state) as owner:
            status = await owner.status(REF)
            assert status.results_state == 'deleted'
            assert status.to_payload()['retained_summary'] == summary
            assert (await owner.list_operations())[0] == status
            assert (await owner.wait(task)).result.payload['state'] == 'deleted'
    asyncio.run(scenario())


def test_delete_refuses_active_and_live_finalizer(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner, finish=False)
            with pytest.raises(OwnerError) as error:
                await owner.delete_results(REF)
            assert error.value.code == 'resource_busy'
            assert await owner._db('deletion_record', REF.id) is None
            await owner._db('settle_pre_dispatch_failure', REF.id,
                Failure.make('dispatch_refused', 'before_start'))
            done = owner._loop.create_future()
            owner._operations[REF.id] = SimpleNamespace(done=done)
            try:
                with pytest.raises(OwnerError) as error:
                    await owner.delete_results(REF)
                assert error.value.code == 'resource_busy'
            finally:
                done.set_result(None)
                del owner._operations[REF.id]
    asyncio.run(scenario())


def test_orphan_needs_manual_release_and_preserves_unknown_cleanup(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        operation = accepted(store)
    async def scenario():
        async with Owner.open(state) as owner:
            with pytest.raises(OwnerError) as error:
                await owner.delete_results(REF)
            assert error.value.code == 'cleanup_required'
            await owner._db('release_after_manual_cleanup', REF, reason='external cleanup asserted')
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
            status = await owner.status(REF)
            assert status.target_cleanup.state == 'manual'
            assert status.target_cleanup.complete is None
            summary = status.to_payload()['retained_summary']
            assert summary == {'campaign_outcome': None, 'dispositions': [{
                'scope_id': operation.scope_id, 'disposition': 'manual',
                'reason': 'external cleanup asserted'}]}
        async with Owner.open(state) as owner:
            assert (await owner.status(REF)).to_payload()['retained_summary'] == summary
            assert (await owner.list_operations())[0].to_payload()['retained_summary'] == summary
    asyncio.run(scenario())


def test_parent_symlink_failure_retry_and_restart_never_auto_deletes(tmp_path):
    async def scenario():
        state = tmp_path / 'state'
        outside = tmp_path / 'outside'
        outside.mkdir()
        async with Owner.open(state) as owner:
            operation = await register(owner)
            inventory = await owner._db('owned_inventory', REF.id)
            files(state, inventory)
            scope = state / 'scopes' / operation.scope_id
            scope.rename(outside / 'scope')
            scope.symlink_to(outside / 'scope', target_is_directory=True)
            task = await owner.delete_results(REF)
            failed = await owner.wait(task)
            assert failed.state == 'failed' and failed.failure.code == 'io_error'
            assert failed.result.payload['state'] == 'failed'
            assert failed.persistence.state == 'saved'
            assert not (state / 'artifacts' / REF.id).exists()
            assert (outside / 'scope' / 'stdout.log').read_bytes() == b'private result'
            record = await owner._db('deletion_record', REF.id)
            assert record['state'] == 'delete_failed'
            assert record['remaining'] == [row for row in inventory if row['kind'] == 'file']
            summary = (await owner.status(REF)).to_payload()['retained_summary']
            assert summary == {'campaign_outcome': None, 'dispositions': []}
            assert (await owner.report(REF)).reason == 'io_error'
            assert (await owner.report_markdown(REF)).reason == 'io_error'
            for call in (lambda: owner.diff(REF), lambda: owner.export(REF),
                         lambda: owner.read_export(REF, 'a' * 64)):
                with pytest.raises(OwnerError) as error:
                    await call()
                assert error.value.code == 'export_unavailable'
        async with Owner.open(state) as owner:
            assert (await owner.status(REF)).results_state == 'delete_failed'
            assert (await owner.list_operations())[0].to_payload()['retained_summary'] == summary
            assert (outside / 'scope' / 'stdout.log').exists()
            scope.unlink()
            (outside / 'scope').rename(scope)
            retry = await owner.wait(await owner.delete_results(REF))
            assert retry.state == 'succeeded'
            after = await owner._db('deletion_record', REF.id)
            assert after['inventory'] == record['inventory']
            assert after['summary'] == record['summary']
            assert (await owner.status(REF)).to_payload()['retained_summary'] == summary
    asyncio.run(scenario())


def test_failed_final_commit_is_not_deleted_and_same_owner_retry_finishes(tmp_path, monkeypatch):
    import sqlite3
    async def scenario():
        state = tmp_path / 'state'
        async with Owner.open(state) as owner:
            await register(owner)
            inventory = await owner._db('owned_inventory', REF.id)
            files(state, inventory)
            real_finish = owner._store.finish_deletion
            def failing_finish(*args, **kwargs):
                owner._store._database.set_authorizer(lambda action, arg1, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT'
                    else sqlite3.SQLITE_OK)
                try:
                    return real_finish(*args, **kwargs)
                finally:
                    owner._store._database.set_authorizer(None)
            monkeypatch.setattr(owner._store, 'finish_deletion', failing_finish)
            task = await owner.delete_results(REF)
            failed = await owner.wait(task)
            assert failed.state == 'failed' and failed.persistence.state == 'failed'
            assert failed.failure.code == 'io_error'
            assert failed.result.payload['state'] == 'failed'
            assert (await owner._db('task_status', task.id)).state == 'running'
            before = await owner._db('deletion_record', REF.id)
            assert before['state'] == 'deleting'
            assert not (state / 'artifacts' / REF.id).exists()
            assert (await owner.report(REF)).reason == 'io_error'
            assert (await owner.report_markdown(REF)).reason == 'io_error'
            assert owner._tasks[task.id].runner.done()
            monkeypatch.setattr(owner._store, 'finish_deletion', real_finish)
            retry = await owner.wait(await owner.delete_results(REF))
            assert retry.state == 'succeeded'
            after = await owner._db('deletion_record', REF.id)
            assert before['inventory'] == after['inventory']
            assert before['summary'] == after['summary']
            assert (await owner.task(task)).persistence.state == 'failed'
        async with Owner.open(state) as owner:
            assert (await owner.task(task)).state == 'unknown'
            assert (await owner.status(REF)).results_state == 'deleted'
    asyncio.run(scenario())


def test_interrupted_intent_requires_explicit_repeat(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        operation = released(store)
        outcome = {'outcome': 'stopped', 'reason': 'requested',
            'continuation': {'kind': 'none', 'reason': None}}
        store.update_operation(REF.id, result=OperationResult(TWO_DIGEST,
            ProcessFacts(), core={'campaign_outcome': outcome, 'private': 'secret'}))
        inventory = store.owned_inventory(REF.id)
        files(state, inventory)
        task = store.accept_deletion(REF.id).task_ref
        store.task_running(task.id)
    async def scenario():
        async with Owner.open(state) as owner:
            assert (await owner.task(task)).state == 'unknown'
            assert (await owner.status(REF)).results_state == 'deleting'
            summary = {'campaign_outcome': outcome, 'dispositions': []}
            assert (await owner.status(REF)).to_payload()['retained_summary'] == summary
            assert (await owner.list_operations())[0].to_payload()['retained_summary'] == summary
            assert (await owner.report(REF)).reason == 'io_error'
            assert (await owner.report_markdown(REF)).reason == 'io_error'
            assert (state / 'artifacts' / REF.id / REF.id / 'private').exists()
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
            assert (await owner.status(REF)).to_payload()['retained_summary'] == summary
    asyncio.run(scenario())


def test_active_delete_has_durable_status_busy_reads_and_no_cached_archive(tmp_path, monkeypatch):
    from evolution_lab.owner.results import publish_archive
    from hashlib import sha256
    from io import BytesIO
    from zipfile import ZipFile
    import evolution_lab.owner.results as results
    started, finish = threading.Event(), threading.Event()
    real_delete = results.delete_inventory
    def blocked(state, inventory):
        started.set()
        assert finish.wait(8)
        return real_delete(state, inventory)
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            inventory = await owner._db('owned_inventory', REF.id)
            files(owner.state_directory, inventory)
            known = OperationResult(TWO_DIGEST, ProcessFacts(), core={'private': 'secret'})
            await owner._db('update_operation', REF.id, result=known, results_state='present')
            owner._operation_cache[REF.id] = await owner._db('operation_status', REF.id)
            # Ready task cache, archive and no_change historical fact coexist.
            buffer = BytesIO()
            manifest = {'schema': 'lab-change-set/1', 'operation_id': REF.id, 'private': 'secret'}
            with ZipFile(buffer, 'w') as archive:
                archive.writestr('manifest.json', json.dumps(manifest))
            data = buffer.getvalue()
            digest = sha256(data).hexdigest()
            ready = models.ExportResult(REF.id, 'ready', digest, digest, manifest)
            await owner._result_thread(publish_archive, owner.state_directory, ready, data)
            await owner._db('publish_export', REF.id, digest)
            ready_ref = (await owner._db('accept_task', action='export', ref=REF)).task_ref
            entry = owner._register_task(ready_ref, 'export', REF)
            await owner._finish_task(entry, result=models.TaskResult('export', ready))
            no_change_ref = (await owner._db('accept_task', action='export', ref=REF)).task_ref
            no_change = models.TaskResult('export', models.ExportResult(REF.id, 'no_change', None, None, None))
            await owner._db('finish_task', no_change_ref.id, result=no_change)
            assert (await owner.task(ready_ref)).result is not None
            monkeypatch.setattr(results, 'delete_inventory', blocked)
            task = await owner.delete_results(REF)
            assert await asyncio.to_thread(started.wait, 5)
            try:
                status = await owner.status(REF)
                assert status.results_state == 'deleting' and status.result.core is None
                assert status.to_payload()['retained_summary'] == {
                    'campaign_outcome': None, 'dispositions': []}
                assert (await owner.list_operations())[0] == status
                assert (await owner.task(ready_ref)).result is None
                assert (await owner.wait(no_change_ref)).result == no_change
                assert owner._task_cache[ready_ref.id].result is None
                for call in (lambda: owner.report(REF), lambda: owner.report_markdown(REF), lambda: owner.diff(REF),
                             lambda: owner.export(REF), lambda: owner.read_export(REF, digest),
                             lambda: owner.delete_results(REF)):
                    with pytest.raises(OwnerError) as error:
                        await call()
                    assert error.value.code == 'resource_busy'
                caller = asyncio.create_task(owner.wait(task))
                await asyncio.sleep(0)
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                assert owner._result_io.writing(REF.id)
            finally:
                finish.set()
            assert (await owner.wait(task)).state == 'succeeded'
            assert (await owner.wait(ready_ref)).result is None
            assert (await owner.wait(no_change_ref)).result == no_change
    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_cancelled_status_keeps_reader_until_actual_io_ends(tmp_path, monkeypatch):
    import evolution_lab.owner.readers as readers
    started, finish = threading.Event(), threading.Event()
    real_project = readers.project_operation
    def blocked(*args, **kwargs):
        started.set()
        assert finish.wait(8)
        return real_project(*args, **kwargs)
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            monkeypatch.setattr(readers, 'project_operation', blocked)
            caller = asyncio.create_task(owner.status(REF))
            assert await asyncio.to_thread(started.wait, 5)
            try:
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                with pytest.raises(OwnerError) as error:
                    await owner.delete_results(REF)
                assert error.value.code == 'resource_busy'
                assert await owner._db('deletion_record', REF.id) is None
            finally:
                finish.set()
            await owner._drain_result_work()
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
    try:
        asyncio.run(scenario())
    finally:
        finish.set()


@pytest.mark.parametrize('replacement', ['link', 'directory'])
def test_inventory_leaf_links_unlink_but_file_directory_is_not_recursed(tmp_path, replacement):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            operation = await register(owner)
            log = owner.state_directory / 'scopes' / operation.scope_id / 'stdout.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            outside = tmp_path / 'outside'
            outside.mkdir()
            sentinel = outside / 'sentinel'
            sentinel.write_bytes(b'keep')
            if replacement == 'link':
                log.symlink_to(outside, target_is_directory=True)
                root = owner.state_directory / 'artifacts' / REF.id
                root.parent.mkdir(parents=True, exist_ok=True)
                root.symlink_to(outside, target_is_directory=True)
            else:
                log.mkdir()
                (log / 'unexpected').write_bytes(b'keep')
            terminal = await owner.wait(await owner.delete_results(REF))
            assert sentinel.read_bytes() == b'keep'
            assert terminal.state == ('succeeded' if replacement == 'link' else 'failed')
            if replacement == 'directory':
                assert (log / 'unexpected').read_bytes() == b'keep'
            else:
                assert not log.is_symlink() and not root.is_symlink()
    asyncio.run(scenario())


def test_late_receipt_updates_old_cleanup_without_releasing_new_operation(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            old = await register(owner)
            known = OperationResult(TWO_DIGEST, ProcessFacts(), core={'private': 'old'})
            await owner._db('update_operation', REF.id, result=known, results_state='present')
            await owner.wait(await owner.delete_results(REF))
            new = await owner._db('accept_operation', prepared_digest=TWO_DIGEST,
                target_ref=old.status.target_ref, executor=_executor(), operation_id='new-operation')
            before = await owner._db('reservations')
            await owner._db('confirm_scope_cleanup', old.scope_id, owner.owner_generation, forced=False)
            await owner._db('release_evidenced_reservations', REF,
                scope_ids=(old.scope_id,), disposition='confirmed_cleanup')
            await owner._db('update_operation', REF.id, result=known,
                target_cleanup=models.CleanupFacts('confirmed', True), results_state='present')
            assert await owner._db('reservations') == before
            status = await owner.status(REF)
            assert status.results_state == 'deleted' and status.result.core is None
            assert status.target_cleanup.complete is True
            await owner._db('settle_pre_dispatch_failure', new.status.id,
                Failure.make('dispatch_refused', 'before_start'))
    asyncio.run(scenario())


def test_delete_waits_for_exact_live_cleanup_runner_even_after_terminal_future(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            ready = owner._loop.create_future()
            finish = asyncio.Event()
            async def cleanup_finalizer():
                task = await owner._db('accept_task', action='cleanup', ref=REF)
                entry = owner._register_task(task.task_ref, 'cleanup', REF)
                await owner._finish_task(entry, result=models.TaskResult('cleanup',
                    models.CleanupFacts('confirmed', True).to_payload()))
                ready.set_result(entry)
                await finish.wait()
            runner = owner._hold(cleanup_finalizer())
            entry = await ready
            try:
                assert entry.done.done() and not entry.runner.done()
                with pytest.raises(OwnerError) as error:
                    await owner.delete_results(REF)
                assert error.value.code == 'resource_busy'
                assert await owner._db('deletion_record', REF.id) is None
            finally:
                finish.set()
                await runner
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
    asyncio.run(scenario())


def test_intent_commit_failure_performs_no_unlink(tmp_path, monkeypatch):
    import sqlite3
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            inventory = await owner._db('owned_inventory', REF.id)
            files(owner.state_directory, inventory)
            original = owner._store.accept_deletion
            def refuse_commit(*args, **kwargs):
                owner._store._database.set_authorizer(lambda action, arg1, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT'
                    else sqlite3.SQLITE_OK)
                try:
                    return original(*args, **kwargs)
                finally:
                    owner._store._database.set_authorizer(None)
            monkeypatch.setattr(owner._store, 'accept_deletion', refuse_commit)
            with pytest.raises(OwnerError) as error:
                await owner.delete_results(REF)
            assert error.value.code == 'io_error' and error.value.stage == 'delete'
            assert await owner._db('deletion_record', REF.id) is None
            assert (owner.state_directory / 'artifacts' / REF.id / REF.id / 'private').read_bytes() == b'private result'
    asyncio.run(scenario())


def test_finished_operation_deletes_while_target_runs_new_operation(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            old = await register(owner, finish=False)
            await owner._db('settle_scope_no_helper', old.scope_id)
            await owner._db('release_evidenced_reservations', REF,
                scope_ids=(old.scope_id,), disposition='normal')
            assert (await owner._db('operation_status', REF.id)).control_state == 'finished'
            new = await owner._db('accept_operation', prepared_digest=TWO_DIGEST,
                target_ref=old.status.target_ref, executor=_executor(), operation_id='next-operation')
            inventory = await owner._db('owned_inventory', new.status.id)
            for row in inventory:
                path = owner.state_directory / row['path']
                if row['kind'] == 'tree':
                    path = path / 'keep'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'new result')
            before = await owner._db('reservations')
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
            assert await owner._db('reservations') == before
            assert (owner.state_directory / 'artifacts' / new.status.id / 'keep').read_bytes() == b'new result'
            assert (await owner._db('operation_status', new.status.id)).control_state == 'reserved'
            assert (await owner._db('target_status', old.status.target_ref.id)).state == 'ready'
            await owner._db('settle_pre_dispatch_failure', new.status.id,
                Failure.make('dispatch_refused', 'before_start'))
    asyncio.run(scenario())


def test_family6_outcome_survives_deletion_as_the_lab_triple(tmp_path):
    state = tmp_path / 'state'
    triple = {'outcome': 'stopped', 'reason': 'condition_changed',
        'continuation': {'kind': 'linked_run', 'reason': 'condition_changed'}}
    with OwnerStateStore.open(state) as store:
        released(store)
        store.update_operation(REF.id, result=OperationResult(TWO_DIGEST, ProcessFacts(),
            core={'campaign_outcome': {**triple, 'champion_path': 'artifacts/champions/v1'}}))
        files(state, store.owned_inventory(REF.id))
    async def scenario():
        async with Owner.open(state) as owner:
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
            summary = (await owner.status(REF)).to_payload()['retained_summary']
            assert summary == {'campaign_outcome': triple, 'dispositions': []}
    asyncio.run(scenario())
