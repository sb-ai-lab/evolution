"""Approved S4 neutral export publication/state fixtures; no campaign execution."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_STORED

import pytest

from evolution_lab.owner import Owner, OwnerError, ResourceRef, Failure
from evolution_lab.owner.changes import SnapshotFile
from test_owner_changes import OP, campaign_identity, case, changes
from test_owner_state import _ready_target, _executor
from test_owner_results_state import accepted

REF = ResourceRef('operation', OP)


@pytest.mark.parametrize('saved_state', ['not_created', 'unavailable'])
@pytest.mark.parametrize('missing', [False, True])
def test_released_orphan_resolves_artifacts_despite_stale_results_state(case, saved_state, missing):
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(case.state) as store:
        store.accept_operation(prepared_digest=case.status.prepared_digest,
            target_ref=_ready_target(store), executor=_executor(), operation_id=OP)
        if saved_state == 'unavailable':
            store.update_operation(OP, results_state=saved_state)
    if missing:
        selected_file = case.selected / 'editable.txt'
        selected_file.parent.chmod(0o700)
        selected_file.unlink()

    async def scenario():
        async with Owner.open(case.state) as owner:
            assert (await owner.status(REF)).control_state == 'orphaned'
            for call in (owner.diff, owner.export):
                with pytest.raises(OwnerError) as error:
                    await call(REF)
                assert error.value.code == 'cleanup_required'
            await owner.release_after_manual_cleanup(REF, reason='external cleanup asserted')
            status = await owner.status(REF)
            assert status.control_state == 'released'
            assert status.target_cleanup.complete is None
            if missing:
                with pytest.raises(OwnerError) as error:
                    await owner.diff(REF)
                assert (error.value.code, error.value.stage) == ('export_unavailable', 'diff')
            else:
                assert (await owner.diff(REF)).state == 'changed'
            exported = await owner.wait(await owner.export(REF))
            if missing:
                assert exported.state == 'failed'
                assert exported.failure.code == 'export_unavailable'
            else:
                assert exported.state == 'succeeded'
                assert await owner.read_export(REF, exported.result.payload['export_id'])
    asyncio.run(scenario())


async def register(owner, case):
    target = await owner._loop.run_in_executor(owner._executor, _ready_target, owner._store)
    await owner._db('accept_operation', prepared_digest=case.status.prepared_digest,
        target_ref=target, executor=_executor(), kind='campaign', operation_id=OP)
    await owner._db('settle_pre_dispatch_failure', OP,
        Failure.make('dispatch_refused', 'before_start'))
    await owner._db('update_operation', OP, results_state='present')


def test_export_public_methods_exist():
    assert callable(getattr(Owner, 'export', None))
    assert callable(getattr(Owner, 'read_export', None))


def test_owner_scratch_diff_and_export_use_pair_but_publish_durably(case, tmp_path, monkeypatch):
    import stat
    from evolution_lab.owner.results import archive_snapshot
    with case.open() as snapshot:
        expected = archive_snapshot(snapshot)[1]
    pair = tmp_path / 'pair'
    observed = []
    from evolution_lab.owner import changes as module
    original_scan = module.scan_tree
    def record_scan(path, *args, **kwargs):
        if path.parent.parent.name == 'staging':
            observed.append(path)
        return original_scan(path, *args, **kwargs)
    monkeypatch.setattr(module, 'scan_tree', record_scan)
    async def scenario():
        async with Owner.open(case.state, scratch_directory=pair) as owner:
            await register(owner, case)
            assert (await owner.diff(REF)).state == 'changed'
            status = await owner.wait(await owner.export(REF))
            assert status.state == 'succeeded', status.to_payload()
            assert await owner.read_export(REF, status.result.payload['export_id']) == expected
            record = await owner._db('published_export', OP)
            assert (case.state / record['path']).is_file()
            assert not (pair / 'artifacts').exists()
        assert len(observed) == 4  # both sides, for the diff and for the export
        assert all(path.parent.parent == pair / 'staging' for path in observed)
        assert not list((pair / 'staging').iterdir())
        assert stat.S_IMODE(pair.stat().st_mode) == 0o700
    asyncio.run(scenario())


def test_export_saved_snapshot_repeat_restart_and_registry(case, monkeypatch):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            task = await owner.export(REF)
            status = await owner.wait(task)
            assert status.state == 'succeeded' and status.failure is None
            result = status.result.payload
            data = await owner.read_export(REF, result['export_id'])
            assert sha256(data).hexdigest() == result['sha256'] == result['export_id']
            with ZipFile(BytesIO(data)) as archive:
                assert archive.namelist() == ['manifest.json', 'payload/00000000']
                manifest = json.loads(archive.read('manifest.json'))
                assert manifest['schema'] == 'lab-change-set/1'
                assert manifest['operation_id'] == OP
                assert manifest['prepared_digest'] == '1' * 64
                assert manifest['bundle_digest'] == '2' * 64
                assert manifest['campaign_outcome'] is None
                assert manifest['campaign_complete'] is False
                assert manifest['measurement_complete'] is False
                assert archive.read('payload/00000000') == b'after\n'
                assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
                assert b'dependency\n' not in data and b'fixed\n' not in data
            record = await owner._db('task_export', task.id)
            path = case.state / record['path']
            inode = path.stat().st_ino
            # Registry discovery does not depend on reopening the seed.
            from evolution_lab.owner import changes
            monkeypatch.setattr(changes, 'snapshot_changes',
                lambda *a, **k: pytest.fail('repeat rebuilt snapshot'))
            repeated = await owner.wait(await owner.export(REF))
            assert repeated.result == status.result
            assert path.stat().st_ino == inode
            assert (await owner._db('task_status', task.id)).result is None
        async with Owner.open(case.state) as owner:
            assert (await owner.task(task)).result == status.result
            assert await owner.read_export(REF, result['export_id']) == data
            assert (await owner.wait(await owner.export(REF))).result == status.result
    asyncio.run(scenario())


def test_archive_fidelity_and_determinism_from_all_change_kinds():
    from evolution_lab.owner.results import archive_snapshot
    before = {'both': SnapshotFile(b'before', 0), 'gone': SnapshotFile(b'gone', 1),
        'mode': SnapshotFile(b'unchanged', 0)}
    after = {'both': SnapshotFile(b'\x00after\xff', 1), 'new/empty': SnapshotFile(b'', 0),
        'mode': SnapshotFile(b'unchanged', 1)}
    pins = ({'name': 'evo-skills', 'version': '0.3.0a8', 'wheel_sha256': '8' * 64},)
    snap = SimpleNamespace(operation_id=OP, prepared_digest='1' * 64,
        bundle_digest='2' * 64, distributions=pins, source_digest='3' * 64,
        selected_digest='4' * 64, selected_version='v7', campaign_outcome=None,
        campaign_complete=False, measurement_complete=False, source_files=before,
        selected_files=after, changed_paths=tuple(sorted(before.keys() | after.keys())),
        no_change=False)
    result, data = archive_snapshot(snap)
    assert archive_snapshot(snap) == (result, data)
    with ZipFile(BytesIO(data)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['distributions'] == list(pins)
        assert [row['kind'] for row in manifest['changes']] == ['modify', 'delete', 'mode', 'add']
        assert archive.namelist() == ['manifest.json', 'payload/00000000', 'payload/00000003']
        assert archive.read('payload/00000000') == b'\x00after\xff'
        assert archive.read('payload/00000003') == b''
        assert manifest['changes'][0]['before_executable'] == 0
        assert manifest['changes'][0]['after_executable'] == 1
        assert manifest['changes'][0]['after_sha256'] == sha256(b'\x00after\xff').hexdigest()
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert archive.read('manifest.json') == json.dumps(manifest,
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def test_change_set_manifest_keeps_the_lab_outcome_triple():
    from evolution_lab.owner.results import archive_snapshot
    triple = {'outcome': 'completed', 'reason': 'rounds_exhausted',
        'continuation': {'kind': 'linked_run', 'reason': 'rounds_exhausted'}}
    snap = SimpleNamespace(operation_id=OP, prepared_digest='1' * 64,
        bundle_digest='2' * 64, distributions=(), source_digest='3' * 64,
        selected_digest='4' * 64, selected_version='v1',
        campaign_outcome={**triple, 'champion_path': 'artifacts/champions/v1'},
        campaign_complete=True, measurement_complete=True,
        source_files={'a': SnapshotFile(b'before', 0)}, selected_files={'a': SnapshotFile(b'after', 0)},
        changed_paths=('a',), no_change=False)
    _, data = archive_snapshot(snap)
    with ZipFile(BytesIO(data)) as archive:
        assert json.loads(archive.read('manifest.json'))['campaign_outcome'] == triple


def test_no_change_survives_restart_and_deleted_state_without_zip(case):
    initial = case.view.snapshot.identity['artifact_store']['seed_candidate_digest']
    case.view = replace(case.view, snapshot=replace(case.view.snapshot, champion=('v0', initial)))
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            task = await owner.export(REF)
            status = await owner.wait(task)
            assert status.state == 'succeeded'
            assert status.result.payload == {'operation_id': OP, 'state': 'no_change',
                'export_id': None, 'sha256': None, 'manifest': None}
            assert await owner._db('published_export', OP) is None
            assert not (case.run.parent / 'exports').exists()
        async with Owner.open(case.state) as owner:
            assert (await owner.task(task)).result == status.result
            intent = await owner._db('accept_deletion', OP)
            await owner._db('finish_deletion', OP, intent.task_ref.id)
            assert (await owner.wait(task)).result == status.result
            for call in (owner.export(REF), owner.read_export(REF, '0' * 64)):
                with pytest.raises(OwnerError) as error:
                    await call
                assert error.value.code == 'results_deleted'
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'symlink', 'parent_symlink'])
def test_corrupted_registry_archive_never_uses_cached_manifest(case, damage, tmp_path):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            task = await owner.export(REF)
            result = (await owner.wait(task)).result.payload
            archive = case.run.parent / 'exports' / f"{result['export_id']}.zip"
            if damage == 'corrupt':
                archive.write_bytes(b'corrupted archive')
            elif damage == 'missing':
                archive.unlink()
            elif damage == 'symlink':
                outside = tmp_path / 'outside.zip'
                archive.rename(outside)
                archive.symlink_to(outside)
            else:
                moved = tmp_path / 'outside-exports'
                archive.parent.rename(moved)
                archive.parent.symlink_to(moved, target_is_directory=True)
            for call in (owner.read_export(REF, result['export_id']), owner.task(task)):
                with pytest.raises(OwnerError) as error:
                    await call
                assert (error.value.code, error.value.stage) == ('export_unavailable', 'read')
            repeated = await owner.wait(await owner.export(REF))
            assert repeated.state == 'failed' and repeated.result is None
            assert repeated.failure.code == 'export_unavailable'
    asyncio.run(scenario())


@pytest.mark.parametrize('failure_point', ['publish_bytes', 'publish_record', 'finalize'])
def test_publication_failure_and_explicit_retry(case, monkeypatch, failure_point):
    from evolution_lab.owner import results
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            real_db = owner._db
            real_publish = results.publish_archive
            if failure_point == 'publish_bytes':
                def broken(*args):
                    raise OSError('private IO detail must not leak')
                monkeypatch.setattr(results, 'publish_archive', broken)
            else:
                async def broken_db(method, *args, **kwargs):
                    if method == ('publish_export' if failure_point == 'publish_record' else 'finish_task'):
                        raise OSError('private commit detail must not leak')
                    return await real_db(method, *args, **kwargs)
                monkeypatch.setattr(owner, '_db', broken_db)
            task = await owner.export(REF)
            failed = await owner.wait(task)
            if failure_point == 'finalize':
                assert failed.state == 'succeeded'
                assert failed.persistence.state == 'failed'
                assert failed.result.payload['state'] == 'ready'
            else:
                assert failed.state == 'failed' and failed.failure.code == 'io_error'
                assert failed.failure.stage == 'export'
            monkeypatch.setattr(owner, '_db', real_db)
            monkeypatch.setattr(results, 'publish_archive', real_publish)
            archives = list((case.run.parent / 'exports').glob('*.zip'))
            if failure_point != 'publish_bytes':
                assert len(archives) == 1
                first = archives[0].read_bytes()
                inode = archives[0].stat().st_ino
            repeated = await owner.wait(await owner.export(REF))
            assert repeated.state == 'succeeded' and repeated.persistence.state == 'saved'
            if failure_point != 'publish_bytes':
                assert await owner.read_export(REF, repeated.result.payload['export_id']) == first
                assert archives[0].stat().st_ino == inode
            assert len(list((case.run.parent / 'exports').glob('*.zip'))) == 1
    asyncio.run(scenario())


def test_crash_link_leftover_is_reused_only_on_full_byte_match(case, monkeypatch):
    import os
    from evolution_lab.owner import results
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            real_db = owner._db
            async def fail_record(method, *args, **kwargs):
                if method == 'publish_export':
                    raise OSError('commit failed')
                return await real_db(method, *args, **kwargs)
            monkeypatch.setattr(owner, '_db', fail_record)
            assert (await owner.wait(await owner.export(REF))).state == 'failed'
            monkeypatch.setattr(owner, '_db', real_db)
            archive = next((case.run.parent / 'exports').glob('*.zip'))
            orphan = archive.parent / '.earlier-crash.tmp'
            os.link(archive, orphan)
            inode = archive.stat().st_ino
            good = archive.read_bytes()
            archive.write_bytes(b'wrong bytes at expected destination')
            failed = await owner.wait(await owner.export(REF))
            assert failed.state == 'failed' and failed.failure.code == 'export_unavailable'
            assert archive.read_bytes() == b'wrong bytes at expected destination'
            archive.write_bytes(good)
            repeated = await owner.wait(await owner.export(REF))
            assert repeated.state == 'succeeded'
            assert archive.stat().st_ino == inode
            assert orphan.exists()  # No unrelated-temp discovery or GC.
            assert await owner.read_export(REF, archive.stem) == good
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['admission', 'lookup', 'finalization'])
def test_pending_export_mutex_lost_reply_and_shared_readers(case, monkeypatch, phase):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            entered = asyncio.Event()
            release = asyncio.Event()
            real_db = owner._db
            blocked = {'admission': 'accept_task', 'lookup': 'published_export',
                'finalization': 'finish_task'}[phase]
            async def pause(method, *args, **kwargs):
                if method == blocked:
                    entered.set()
                    await release.wait()
                return await real_db(method, *args, **kwargs)
            monkeypatch.setattr(owner, '_db', pause)
            caller = asyncio.create_task(owner.export(REF))
            await entered.wait()
            try:
                with pytest.raises(OwnerError) as error:
                    await owner.export(REF)
                assert (error.value.code, error.value.stage) == ('resource_busy', 'export')
                with pytest.raises(OwnerError):
                    owner._result_io.acquire_write(OP)
                assert (await owner.status(REF)).id == OP
                await owner.report(REF)
                assert (await owner.diff(REF)).state == 'changed'
                if phase == 'admission':
                    caller.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await caller
            finally:
                release.set()
            if phase == 'admission':
                await owner._drain_result_work()
                task = next(iter(owner._tasks.values())).ref
            else:
                task = await caller
            status = await owner.wait(task)
            assert status.state == 'succeeded'
            assert status.result.payload['state'] == 'ready'
            lease = owner._result_io.acquire_write(OP)
            lease.release()
    asyncio.run(scenario())


def test_read_requires_scoped_registry_and_suppresses_deleted_ready_cache(case):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            task = await owner.export(REF)
            status = await owner.wait(task)
            export_id = status.result.payload['export_id']
            for bad, expected in [('0' * 64, 'not_found'), ('../file', 'invalid_argument'),
                                  (None, 'invalid_argument')]:
                with pytest.raises(OwnerError) as error:
                    await owner.read_export(REF, bad)
                assert (error.value.code, error.value.stage) == (expected, 'read')
            unknown = ResourceRef('operation', '22222222-2222-4222-8222-222222222222')
            with pytest.raises(OwnerError) as error:
                await owner.read_export(unknown, export_id)
            assert error.value.code == 'not_found'
            intent = await owner._db('accept_deletion', OP)
            assert (await owner.task(task)).result is None
            await owner._db('finish_deletion', OP, intent.task_ref.id)
            assert (await owner.wait(task)).result is None
            assert (await owner.task(task)).state == status.state
    asyncio.run(scenario())


@pytest.mark.parametrize('point', ['file_fsync', 'link', 'directory_fsync'])
def test_publication_syscall_failure_does_not_register(case, monkeypatch, point):
    import os
    import stat
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            real_fsync = os.fsync
            real_link = os.link
            def fail_fsync(fd):
                info = os.fstat(fd)
                selected = stat.S_ISREG(info.st_mode) if point == 'file_fsync' else stat.S_ISDIR(info.st_mode)
                if selected:
                    raise OSError('fsync denied')
                real_fsync(fd)
            def fail_link(*args, **kwargs):
                raise OSError('link denied')
            if point == 'link':
                monkeypatch.setattr(os, 'link', fail_link)
            else:
                monkeypatch.setattr(os, 'fsync', fail_fsync)
            try:
                status = await owner.wait(await owner.export(REF))
                assert status.state == 'failed' and status.failure.code == 'io_error'
                assert await owner._db('published_export', OP) is None
            finally:
                monkeypatch.setattr(os, 'fsync', real_fsync)
                monkeypatch.setattr(os, 'link', real_link)
            assert (await owner.wait(await owner.export(REF))).state == 'succeeded'
    asyncio.run(scenario())


def test_cancelled_wait_keeps_export_thread_and_staging_owned(case, monkeypatch):
    import threading
    from evolution_lab.owner import results
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            entered = asyncio.Event()
            release = threading.Event()
            real_publish = results.publish_archive
            def paused_publish(*args):
                owner._loop.call_soon_threadsafe(entered.set)
                release.wait()
                return real_publish(*args)
            monkeypatch.setattr(results, 'publish_archive', paused_publish)
            task = await owner.export(REF)
            await entered.wait()
            try:
                waiter = asyncio.create_task(owner.wait(task))
                await asyncio.sleep(0)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert len(list((case.run.parent / 'staging').iterdir())) == 1
                with pytest.raises(OwnerError) as error:
                    owner._result_io.acquire_write(OP)
                assert error.value.code == 'resource_busy'
            finally:
                release.set()
            assert (await owner.wait(task)).state == 'succeeded'
            assert not list((case.run.parent / 'staging').iterdir())
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_export_admission_refuses_unreleased_or_evaluation(tmp_path, kind):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            operation = await owner._loop.run_in_executor(owner._executor,
                accepted, owner._store, kind)
            with pytest.raises(OwnerError) as error:
                await owner.export(ResourceRef('operation', operation.status.id))
            assert (error.value.code, error.value.stage) == (
                'resource_busy' if kind == 'campaign' else 'export_unavailable', 'export')
            assert not owner._tasks
            with pytest.raises(OwnerError) as error:
                await owner.export(ResourceRef('target', OP))
            assert (error.value.code, error.value.stage) == ('invalid_argument', 'export')
    asyncio.run(scenario())


@pytest.mark.parametrize('state, expected', [('orphaned', 'cleanup_required'),
                                           ('cleanup_required', 'cleanup_required')])
def test_export_unreleased_orphan_refused_before_acceptance(case, monkeypatch, state, expected):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            real_db = owner._db
            async def projected_state(method, *args, **kwargs):
                value = await real_db(method, *args, **kwargs)
                return replace(value, control_state=state) if method == 'operation_status' else value
            monkeypatch.setattr(owner, '_db', projected_state)
            with pytest.raises(OwnerError) as error:
                await owner.export(REF)
            assert (error.value.code, error.value.stage) == (expected, 'export')
            assert not owner._tasks
    asyncio.run(scenario())


def test_manifest_preserves_saved_producing_pins_and_known_outcome(case):
    from evolution_lab.owner.results import archive_snapshot
    from evolution_lab.preparation.models import DistributionPin
    pin = DistributionPin('evo-skills', '0.3.0a8',
        '8cbb21ae68b38fb4e2199d2b968d7919d2a951b9b1d98de4fab77f4bf2bf5f52')
    case.binding.bundle.execution_environment.distributions = (pin,)
    with case.open(stage='export') as snapshot:
        outcome = {'outcome': 'stopped', 'reason': 'cancelled',
            'continuation': {'kind': 'unknown', 'reason': 'fixture'}}
        snapshot = replace(snapshot, campaign_outcome=outcome,
            campaign_complete=True, measurement_complete=True)
        result, data = archive_snapshot(snapshot)
        manifest = json.loads(ZipFile(BytesIO(data)).read('manifest.json'))
        assert manifest['distributions'] == [pin.to_payload()]
        assert manifest['campaign_outcome'] == outcome
        assert manifest['campaign_complete'] is True
        assert manifest['measurement_complete'] is True
        assert result.to_payload()['manifest'] == manifest


def test_published_export_reads_the_current_start_archive(case):
    async def scenario():
        async with Owner.open(case.state) as owner:
            await register(owner, case)
            assert await owner.published_export(REF) is None
            status = await owner.wait(await owner.export(REF))
            published = await owner.published_export(REF)
            assert published["export_id"] == status.result.payload["export_id"]
            assert published["operation_id"] == OP
            with pytest.raises(OwnerError):
                await owner.published_export(ResourceRef('target', 'x'))
    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("strict_durability")
