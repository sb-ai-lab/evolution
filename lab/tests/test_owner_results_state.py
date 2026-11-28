"""S4 codecs, fixed ownership and cancellation-safe filesystem lifetime."""
import asyncio
import json
import threading
from dataclasses import replace

import pytest

from evolution_lab.owner import Owner, OwnerError, ResourceRef, Failure, OperationResult, ProcessFacts
from evolution_lab.owner import models
from evolution_lab.owner.store import OwnerStateStore
from test_owner_state import _ready_target, _executor, TWO_DIGEST, FIRST_OPERATION_ID
from test_owner_core_raw import finished_campaign

pytestmark = pytest.mark.slow


def accepted(store, kind='campaign'):
    return store.accept_operation(prepared_digest=TWO_DIGEST, target_ref=_ready_target(store),
        executor=replace(_executor(), kind=f'lab-{kind}-worker/1'),
        kind=kind, operation_id=FIRST_OPERATION_ID)


def released(store):
    value = accepted(store)
    store.settle_pre_dispatch_failure(value.status.id, Failure.make('dispatch_refused', 'before_start'))
    return value


def test_retained_summary_is_closed_and_deeply_immutable(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        status = released(store).status
    assert status.retained_summary is None
    assert status.to_payload()['retained_summary'] is None
    summary = {'campaign_outcome': {'outcome': 'stopped', 'reason': 'requested',
        'continuation': {'kind': 'none', 'reason': None}}, 'dispositions': [{
        'scope_id': 'scope1', 'disposition': 'manual', 'reason': 'operator assertion'}]}
    projected = replace(status, retained_summary=summary)
    expected = json.loads(json.dumps(summary))
    summary['campaign_outcome']['continuation']['kind'] = 'modified'
    summary['dispositions'][0]['reason'] = 'modified'
    assert projected.to_payload()['retained_summary'] == expected
    with pytest.raises(TypeError):
        projected.retained_summary['dispositions'][0]['reason'] = 'modified'
    payload = projected.to_payload()
    payload['retained_summary']['campaign_outcome']['reason'] = 'modified'
    assert projected.to_payload()['retained_summary'] == expected
    invalid = [
        {**expected, 'private': 'secret'},
        {**expected, 'campaign_outcome': {**expected['campaign_outcome'], 'private': 'secret'}},
        {**expected, 'campaign_outcome': {**expected['campaign_outcome'], 'reason': 1}},
        {**expected, 'campaign_outcome': {**expected['campaign_outcome'],
            'continuation': {'kind': 'none', 'reason': None, 'private': 'secret'}}},
        {**expected, 'dispositions': [{'scope_id': 'scope1', 'disposition': 'manual',
            'reason': 'asserted', 'path': 'private'}]},
        {**expected, 'dispositions': [{'scope_id': '../escape', 'disposition': 'manual', 'reason': 'asserted'}]},
        {**expected, 'dispositions': [{'scope_id': 'scope1', 'disposition': 'invented', 'reason': None}]},
        {**expected, 'dispositions': [{'scope_id': 'scope1', 'disposition': 'manual', 'reason': 3}]},
    ]
    for value in invalid:
        with pytest.raises(OwnerError):
            replace(status, retained_summary=value)


def test_closed_result_codecs_and_task_kinds():
    diff = models.DiffResult(FIRST_OPERATION_ID, '0' * 64, '1' * 64, 'changed', '',
        ({'path': 'tool.bin', 'kind': 'binary'},), source_ref='initial', selected_ref='champion:v1')
    for value in (diff, models.ExportResult(FIRST_OPERATION_ID, 'no_change', None, None, None),
                  models.DeletionResult(FIRST_OPERATION_ID, 'deleted', None)):
        assert type(value).from_payload(value.to_payload()) == value
        with pytest.raises(OwnerError):
            type(value).from_payload({**value.to_payload(), 'extra': True})
    with pytest.raises(OwnerError):
        models.DiffResult(FIRST_OPERATION_ID, '0' * 64, '1' * 64, 'changed', '',
            ({'path': '../escape', 'kind': 'binary'},), source_ref='initial', selected_ref='champion:v1')
    with pytest.raises(OwnerError):
        models.ExportResult(FIRST_OPERATION_ID, 'no_change', '0' * 64, '0' * 64, {})
    with pytest.raises(OwnerError):
        models.DeletionResult(FIRST_OPERATION_ID, 'failed', None)
    with pytest.raises(OwnerError):
        models.TaskResult('delete', {'operation_id': FIRST_OPERATION_ID, 'state': 'deleted', 'failure': None, 'private': 1})


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_acceptance_inventory_owns_only_fixed_results(tmp_path, kind):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        value = accepted(store, kind)
        names = {'stdout.log', 'stderr.log', 'diagnostic.json'}
        if kind == 'evaluation':
            names |= {'evaluation-inputs.json', 'evaluation-outcome.json'}
        else:
            names.add('core-entry.json')
        assert store.owned_inventory(value.status.id) == ({'path': f'artifacts/{value.status.id}', 'kind': 'tree'},) + tuple(
            {'path': f'scopes/{value.scope_id}/{name}', 'kind': 'file'} for name in sorted(names))
        assert not (store.state_directory / 'artifacts' / value.status.id).exists()


def test_no_change_survives_reopen_and_delete_intent_retains_only_summary(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        operation = released(store)
        ref = ResourceRef('operation', operation.status.id)
        store.update_operation(ref.id, result=OperationResult(TWO_DIGEST, ProcessFacts(),
            core={'private_report': 'DO NOT RETAIN', 'campaign_outcome': {'outcome': 'stopped', 'reason': 'cancelled', 'continuation': {'kind': 'none', 'reason': None}}},
            stopped_reason='requested'), results_state='present')
        export = store.accept_task(action='export', ref=ref).task_ref
        payload = models.TaskResult('export', models.ExportResult(ref.id, 'no_change', None, None, None))
        store.finish_task(export.id, result=payload)
        deletion = store.accept_deletion(ref.id)
        assert store.operation_status(ref.id).results_state == 'deleting'
        assert store.operation_status(ref.id).result.core is None
        assert 'DO NOT RETAIN' not in json.dumps(store.deletion_record(ref.id))
        assert store.deletion_record(ref.id)['summary']['campaign_outcome']['outcome'] == 'stopped'
        assert store.deletion_record(ref.id)['inventory'] == list(store.owned_inventory(ref.id))
        store.finish_deletion(ref.id, deletion.task_ref.id)
        assert store.task_status(export.id).result == payload
    with OwnerStateStore.open(state) as store:
        assert store.task_status(export.id).result == payload
        assert store.operation_status(ref.id).results_state == 'deleted'
        assert store.deletion_record(ref.id)['state'] == 'deleted'
        assert store.task_status(deletion.task_ref.id).result.kind == 'delete'


def test_ready_export_stores_reference_and_delete_strips_it(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        value = released(store)
        ref = ResourceRef('operation', value.status.id)
        task = store.accept_task(action='export', ref=ref).task_ref
        archive = 'a' * 64
        record = store.publish_export(ref.id, archive)
        assert record['path'] == f'artifacts/{ref.id}/exports/{archive}.zip'
        store.finish_task(task.id, result=models.TaskResult('export', models.ExportResult(
            ref.id, 'ready', archive, archive, {'private': 'archive only'})))
        assert store.task_status(task.id).result is None
        assert store.task_export(task.id) == record
        row = store._database.execute('SELECT result_json FROM tasks WHERE id=?', (task.id,)).fetchone()
        assert row[0] is None
        deletion = store.accept_deletion(ref.id)
        assert store.task_export(task.id) is None
        store.finish_deletion(ref.id, deletion.task_ref.id, failure=Failure.make('io_error', 'delete'))
        assert store.operation_status(ref.id).results_state == 'delete_failed'
        assert store.task_status(deletion.task_ref.id).failure.code == 'io_error'
        retry = store.accept_deletion(ref.id)
        store.finish_deletion(ref.id, retry.task_ref.id)
        assert store.operation_status(ref.id).results_state == 'deleted'
        assert store.task_export(task.id) is None
        assert store.published_export(ref.id, archive) is None
        mark = store._database.execute('SELECT deleted FROM operation_exports WHERE operation_id=?',
                                       (ref.id,)).fetchone()
        assert mark[0] == 1


def test_deletion_admission_requires_operation_release(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        value = accepted(store)
        with pytest.raises(OwnerError) as error:
            store.accept_deletion(value.status.id)
        assert error.value.code == 'resource_busy'
        assert store.deletion_record(value.status.id) is None


def test_guards_allow_readers_and_export_but_fail_fast_on_conflicts():
    from evolution_lab.owner.result_io import OperationResultIO
    guards = OperationResultIO()
    first = guards.acquire_read('op', 'read')
    second = guards.acquire_export('op', 'export')
    with pytest.raises(OwnerError):
        guards.acquire_write('op', 'delete')
    with pytest.raises(OwnerError):
        guards.acquire_export('op', 'export')
    other = guards.acquire_write('other', 'delete')
    first.release()
    second.release()
    writer = guards.acquire_write('op', 'delete')
    assert guards.writing('op')
    with pytest.raises(OwnerError):
        guards.acquire_read('op', 'read')
    writer.release()
    writer.release()
    other.release()
    assert not guards.writing('op')


def test_cancelled_read_keeps_guard_and_close_keeps_flock_until_io_finishes(tmp_path):
    started, finish = threading.Event(), threading.Event()
    def io():
        started.set()
        assert finish.wait(8)
        return b'finished'
    async def scenario():
        state = tmp_path / 'state'
        async with Owner.open(state) as owner:
            async def work():
                return await owner._result_thread(io)
            caller = asyncio.create_task(owner._result_work(work,
                owner._result_io.acquire_read('op', 'read')))
            assert await asyncio.to_thread(started.wait, 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            with pytest.raises(OwnerError):
                owner._result_io.acquire_write('op', 'delete')
            close = asyncio.create_task(owner.close(wait_seconds=.01))
            await asyncio.sleep(.05)
            assert not close.done()
            with pytest.raises(OwnerError):
                await asyncio.to_thread(OwnerStateStore.open, state)
            finish.set()
            await close
            assert not owner._result_owned
        with OwnerStateStore.open(state):
            pass
    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_deletion_commit_failure_keeps_original_report_and_retry_inventory(tmp_path):
    import sqlite3
    with OwnerStateStore.open(tmp_path / 'state') as store:
        operation = released(store)
        result = OperationResult(TWO_DIGEST, ProcessFacts(), core={'private': 'retained until intent'})
        store.update_operation(operation.status.id, result=result, results_state='present')
        store._database.set_authorizer(lambda action, arg1, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT' else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(OwnerError) as error:
                store.accept_deletion(operation.status.id)
            assert error.value.code == 'io_error'
        finally:
            store._database.set_authorizer(None)
        assert store.deletion_record(operation.status.id) is None
        assert store.operation_status(operation.status.id).result == result
        task = store.accept_deletion(operation.status.id)
        inventory = store.deletion_record(operation.status.id)['inventory']
        store.finish_deletion(operation.status.id, task.task_ref.id,
            failure=Failure.make('io_error', 'delete'), remaining=inventory[:1])
    with OwnerStateStore.open(tmp_path / 'state') as store:
        assert store.operation_status(operation.status.id).results_state == 'delete_failed'
        retry = store.accept_deletion(operation.status.id)
        assert store.deletion_record(operation.status.id)['inventory'] == inventory
        store.finish_deletion(operation.status.id, retry.task_ref.id)
        # A late receipt-derived update cannot restore deleted report payload.
        store.update_operation(operation.status.id, result=result, results_state='present')
        assert store.operation_status(operation.status.id).results_state == 'deleted'
        assert store.operation_status(operation.status.id).result.core is None


def test_export_task_wait_does_not_resurrect_deleted_cached_manifest(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            # Use only synchronous temporary-state fixture construction on its store thread.
            target = await owner._loop.run_in_executor(owner._executor, _ready_target, owner._store)
            operation = await owner._db('accept_operation', prepared_digest=TWO_DIGEST,
                target_ref=target, executor=_executor(), operation_id=FIRST_OPERATION_ID)
            await owner._db('settle_pre_dispatch_failure', operation.status.id,
                Failure.make('dispatch_refused', 'before_start'))
            ref = ResourceRef('operation', operation.status.id)
            accepted_task = await owner._db('accept_task', action='export', ref=ref)
            entry = owner._register_task(accepted_task.task_ref, 'export', ref)
            from hashlib import sha256
            from io import BytesIO
            from zipfile import ZipFile
            from evolution_lab.owner.results import publish_archive
            manifest = {'schema': 'lab-change-set/1', 'operation_id': ref.id,
                'private': 'must disappear'}
            buffer = BytesIO()
            with ZipFile(buffer, 'w') as archive:
                archive.writestr('manifest.json', json.dumps(manifest))
            data = buffer.getvalue()
            digest = sha256(data).hexdigest()
            result = models.ExportResult(ref.id, 'ready', digest, digest, manifest)
            (owner.state_directory / 'artifacts' / ref.id).mkdir(parents=True)
            await owner._result_thread(publish_archive, owner.state_directory, result, data)
            await owner._db('publish_export', ref.id, digest)
            await owner._finish_task(entry, result=models.TaskResult('export', result))
            assert (await owner.wait(entry.ref)).result.payload['manifest']['private'] == 'must disappear'
            # A published archive and terminal task cache must still admit the
            # current journal; an actual old creation prefix cannot bypass it.
            import shutil
            from pathlib import Path
            run = owner.state_directory / 'artifacts' / ref.id / ref.id
            run.mkdir()
            shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                            run / 'campaign.jsonl')
            for read in (lambda: owner.export(ref), lambda: owner.read_export(ref, digest),
                         lambda: owner.task(entry.ref), lambda: owner.wait(entry.ref)):
                with pytest.raises(OwnerError) as refused:
                    await read()
                assert refused.value.code == 'export_unavailable'
            deletion = await owner._db('accept_deletion', ref.id)
            await owner._db('finish_deletion', ref.id, deletion.task_ref.id)
            assert (await owner.task(entry.ref)).result is None
            assert (await owner.wait(entry.ref)).result is None
    asyncio.run(scenario())


def test_cancellation_before_first_await_cannot_abandon_transferred_lease(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            completed = asyncio.Event()
            async def work():
                completed.set()
            wait = owner._result_work(work, owner._result_io.acquire_read('op', 'read'))
            caller = asyncio.create_task(wait)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert owner._result_owned or completed.is_set()
            await owner._drain_result_work()
            assert completed.is_set()
            owner._result_io.acquire_write('op', 'delete').release()
    asyncio.run(scenario())


def test_same_generation_retry_after_deletion_commit_failure(tmp_path):
    import sqlite3

    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            target = await owner._loop.run_in_executor(owner._executor, _ready_target, owner._store)
            operation = await owner._db('accept_operation', prepared_digest=TWO_DIGEST,
                target_ref=target, executor=_executor(), operation_id=FIRST_OPERATION_ID)
            ref = ResourceRef('operation', operation.status.id)
            await owner._db('settle_pre_dispatch_failure', ref.id,
                Failure.make('dispatch_refused', 'before_start'))
            completed = owner._loop.create_future()
            finish_worker = asyncio.Event()

            def reject_commit():
                owner._store._database.set_authorizer(lambda action, arg1, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT'
                    else sqlite3.SQLITE_OK)

            def allow_commit():
                owner._store._database.set_authorizer(None)

            async def first_attempt():
                acceptance = await owner._db('accept_deletion', ref.id)
                entry = owner._register_task(acceptance.task_ref, 'delete', ref)
                await owner._db('task_running', entry.ref.id)
                await owner._loop.run_in_executor(owner._executor, reject_commit)
                try:
                    with pytest.raises(OwnerError) as failed:
                        await owner._db('finish_deletion', ref.id, entry.ref.id)
                    assert failed.value.code == 'io_error'
                finally:
                    await owner._loop.run_in_executor(owner._executor, allow_commit)
                durable = await owner._db('task_status', entry.ref.id)
                live = replace(durable, state='failed', failure=failed.value.failure,
                    persistence=models.Persistence('failed', failed.value.failure),
                    result=models.TaskResult('delete', models.DeletionResult(
                        ref.id, 'failed', failed.value.failure)))
                owner._task_cache[entry.ref.id] = live
                entry.done.set_result(live)
                completed.set_result(entry)
                await finish_worker.wait()
                return entry

            runner = owner._hold_result(first_attempt, owner._result_io.acquire_write(ref.id))
            try:
                entry = await asyncio.shield(completed)
                # A terminal result Future does not prove its finalizer has ended.
                with pytest.raises(OwnerError) as active:
                    owner._completed_deletion(ref.id, entry.ref.id)
                assert active.value.code == 'resource_busy'
                with pytest.raises(OwnerError):
                    owner._result_io.acquire_write(ref.id)
            finally:
                finish_worker.set()
                await asyncio.shield(runner)

            durable = await owner._db('task_status', entry.ref.id)
            assert durable.state == 'running'
            before = await owner._db('deletion_record', ref.id)
            assert before['state'] == 'deleting'
            with pytest.raises(OwnerError) as missing_lease:
                owner._completed_deletion(ref.id, entry.ref.id)
            assert missing_lease.value.code == 'invalid_argument'
            with pytest.raises(OwnerError) as no_evidence:
                await owner._db('accept_deletion', ref.id)
            assert no_evidence.value.code == 'resource_busy'

            lease = owner._result_io.acquire_write(ref.id)
            try:
                live = owner._completed_deletion(ref.id, entry.ref.id)
                assert live.persistence.state == 'failed'
                mismatched = (
                    replace(live, id='different-task'),
                    replace(live, owner_generation='different-generation'),
                    replace(live, action='export'),
                    replace(live, ref=ResourceRef('operation', 'different-operation')),
                    replace(live, state='running'),
                )
                for evidence in mismatched:
                    with pytest.raises(OwnerError) as invalid:
                        await owner._db('accept_deletion', ref.id, completed_deletion=evidence)
                    assert invalid.value.code == 'invalid_argument'
                    assert await owner._db('deletion_record', ref.id) == before
                retry = await owner._db('accept_deletion', ref.id, completed_deletion=live)
                after = await owner._db('deletion_record', ref.id)
                assert after['task_id'] != before['task_id']
                assert after['inventory'] == before['inventory']
                assert after['summary'] == before['summary']
                assert (await owner._db('task_status', entry.ref.id)).state == 'running'
                result = await owner._db('finish_deletion', ref.id, retry.task_ref.id)
                assert result.state == 'succeeded'
                assert (await owner._db('operation_status', ref.id)).results_state == 'deleted'
            finally:
                lease.release()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['io', 'corrupt', 'family', 'pin', 'missing_journal', 'missing_run'])
def test_v7_restart_fallback_keeps_family14_but_never_foreign_payload(
        tmp_path, monkeypatch, damage, finished_campaign):
    from pathlib import Path
    import shutil
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.readers import project_operation
    state = tmp_path / 'state'
    source_state, source_op, source_status = finished_campaign
    source_run = source_state / 'artifacts' / source_op / source_op
    with OwnerStateStore.open(state) as store:
        value = released(store)
        op = value.status.id
        run = state / 'artifacts' / op / op
        shutil.copytree(source_run, run)
        status = replace(store.operation_status(op), process=source_status.process)
        admitted = project_operation(state, status)
        assert admitted.results_state == 'present'
        assert core_raw.inspect_family14_view(run).snapshot.family_version == 14
        store.update_operation(op, process=admitted.process, result=admitted.result,
                               results_state=admitted.results_state)
        # A subsequent start may refuse before launch; its process facts must
        # neither replace the producing start nor make a known result not_created.
        store.update_operation(op, process=ProcessFacts(False, None, True, False))
    with OwnerStateStore.open(state) as store:
        durable = store.operation_status(op)
        if damage == 'io':
            def fail(*a, **k):
                raise OSError('temporary')
            monkeypatch.setattr('evolution.tree.campaign_projections.inspect_view', fail)
        elif damage == 'family':
            shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                            run / 'campaign.jsonl')
        elif damage == 'pin':
            monkeypatch.setattr(core_raw, '_installed_core', lambda: 'wrong')
        elif damage == 'missing_journal':
            (run / 'campaign.jsonl').unlink()
        elif damage == 'missing_run':
            run.rename(run.with_name('lost-run'))
        else:
            (run / 'campaign.jsonl').write_text('broken\n')
        projected = project_operation(state, durable)
        assert projected.results_state == 'unavailable'
        assert projected.result.process == durable.result.process
        assert projected.result.core == (None if damage == 'family' else durable.result.core)
        store.update_operation(op, result=projected.result, results_state=projected.results_state)
    with OwnerStateStore.open(state) as store:
        assert store.operation_status(op).result.core == projected.result.core
