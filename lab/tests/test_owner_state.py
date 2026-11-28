from __future__ import annotations

import json
import shutil
import sqlite3
import asyncio
import sys
import threading
from pathlib import Path

import pytest
from platforms import linux_only

from evolution_lab.owner import (
    CleanupFacts,
    ExecutorIdentity,
    Failure,
    OwnerError,
    ProcessFacts,
    ResourceRef,
    TargetRef,
)
from evolution_lab.owner.store import OwnerStateStore


def _target_directories(root, target_id='1' * 32):
    paths = tuple(root / container / target_id for container in ('targets', 'snapshots'))
    for path in paths:
        (path / 'nested').mkdir(parents=True)
        (path / 'nested' / 'data').write_text('private temporary data')
    return paths


@pytest.mark.parametrize('scratch_enabled', [True, False])
def test_confirmed_stop_removes_only_opted_in_scratch(tmp_path, scratch_enabled):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    scratch = pair if scratch_enabled else None
    with OwnerStateStore.open(state, scratch_directory=scratch) as store:
        ref = _ready_target(store)
        paths = _target_directories(pair if scratch_enabled else state)
        status = store.set_target_state(ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True))
        assert status.state == 'stopped' and status.cleanup == CleanupFacts('confirmed', True)
        assert all(path.exists() is (not scratch_enabled) for path in paths)


def test_scratch_failed_removal_is_soft_and_retried_on_open(tmp_path, caplog):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair) as store:
        ref = _ready_target(store)
        work, snapshots = _target_directories(pair)
        (pair / 'targets').chmod(0o500)
        try:
            status = store.set_target_state(ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True))
            assert status.state == 'stopped' and status.failure is None
            # The watcher may persist failure details after its release seam already
            # recorded the same confirmed stop. Only reopening retries that failure.
            store.set_target_state(ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True))
            assert work.exists() and not snapshots.exists()
            records = [record for record in caplog.records if record.name == 'evolution_lab.owner.scratch']
            assert len(records) == 1
            assert records[0].levelname == 'WARNING'
        finally:
            (pair / 'targets').chmod(0o700)
    with OwnerStateStore.open(state, scratch_directory=pair) as reopened:
        assert reopened.target_status(ref.id).state == 'stopped'
        assert not work.exists() and not snapshots.exists()


def test_scratch_open_preserves_orphan_until_explicit_confirmed_cleanup(tmp_path):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair) as first:
        ref = _ready_target(first)
        paths = _target_directories(pair)
    with OwnerStateStore.open(state, scratch_directory=pair) as reopened:
        assert reopened.target_status(ref.id).state == 'cleanup_required'
        assert all(path.is_dir() for path in paths)
        scopes = reopened.resource_scopes(ResourceRef('target', ref.id))
        for scope in scopes:
            reopened.confirm_scope_cleanup(scope['id'], scope['owner_generation'], forced=False)
        reopened.release_evidenced_reservations(ResourceRef('target', ref.id),
            scope_ids=tuple(scope['id'] for scope in scopes), disposition='confirmed_cleanup')
        assert reopened.target_status(ref.id).state == 'stopped'
        assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('state,cleanup', [
    ('cleanup_required', CleanupFacts('confirmed', True)),
    ('stopped', CleanupFacts('unconfirmed', None)),
    ('stopped', CleanupFacts('not_needed', True)),
    ('stopped', CleanupFacts('manual', None)),
])
def test_scratch_nonconfirmed_stops_are_preserved_on_transition_and_open(tmp_path, state, cleanup):
    root, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(root, scratch_directory=pair) as store:
        ref = _ready_target(store)
        paths = _target_directories(pair)
        store.set_target_state(ref.id, state=state, cleanup=cleanup)
        assert all(path.is_dir() for path in paths)
    with OwnerStateStore.open(root, scratch_directory=pair):
        assert all(path.is_dir() for path in paths)


def test_scratch_manual_orphan_release_keeps_directories(tmp_path):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair) as first:
        ref = _ready_target(first)
        paths = _target_directories(pair)
    with OwnerStateStore.open(state, scratch_directory=pair) as reopened:
        receipt = reopened.release_after_manual_cleanup(ResourceRef('target', ref.id), reason='operator attested stop')
        assert receipt.cleanup == CleanupFacts('manual', None)
        assert all(path.is_dir() for path in paths)
    with OwnerStateStore.open(state, scratch_directory=pair):
        assert all(path.is_dir() for path in paths)


def test_scratch_substituted_target_symlink_is_refused_without_following(tmp_path, caplog):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('outside data')
    with OwnerStateStore.open(state, scratch_directory=pair) as store:
        ref = _ready_target(store)
        work, snapshots = _target_directories(pair)
        shutil.rmtree(work)
        work.symlink_to(outside, target_is_directory=True)
        status = store.set_target_state(ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True))
        assert status.state == 'stopped' and status.failure is None
        assert (outside / 'keep').read_text() == 'outside data'
        assert work.is_symlink() and not snapshots.exists()
        assert len([record for record in caplog.records if record.name == 'evolution_lab.owner.scratch']) == 1


def test_scratch_open_sweeps_stale_staging_only_in_scratch(tmp_path):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair):
        pass
    for root in (state, pair):
        staging = root / 'staging' / ('4' * 32)
        (staging / 'nested').mkdir(parents=True)
        (staging / 'nested' / 'data').write_text('interrupted call')
    with OwnerStateStore.open(state, scratch_directory=pair):
        assert not tuple((pair / 'staging').iterdir())
        assert (state / 'staging' / ('4' * 32) / 'nested' / 'data').read_text() == 'interrupted call'


@pytest.mark.parametrize('transition', ['set_target_state', 'release_evidenced_reservations'])
def test_scratch_deletion_waits_for_successful_stop_commit(tmp_path, transition):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair) as store:
        ref = _ready_target(store)
        paths = _target_directories(pair)
        scopes = store.resource_scopes(ResourceRef('target', ref.id))
        for scope in scopes:
            store.confirm_scope_cleanup(scope['id'], scope['owner_generation'], forced=False)
        store._database.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and arg == 'COMMIT' else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(OwnerError) as caught:
                if transition == 'set_target_state':
                    store.set_target_state(ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True))
                else:
                    store.release_evidenced_reservations(ResourceRef('target', ref.id),
                        scope_ids=tuple(scope['id'] for scope in scopes), disposition='confirmed_cleanup')
            assert caught.value.code == 'io_error'
            assert store.target_status(ref.id).state == 'ready'
            assert all(path.is_dir() for path in paths)
        finally:
            store._database.set_authorizer(None)


def test_scratch_marker_same_empty_missing_and_removed(tmp_path):
    import shutil
    state, first, second = (tmp_path / name for name in ('state', 'first', 'second'))
    marker = state / 'scratch.json'
    with OwnerStateStore.open(state, scratch_directory=first):
        assert json.loads(marker.read_text()) == {'schema': 'lab-owner-scratch/1', 'path': str(first)}
    with OwnerStateStore.open(state, scratch_directory=first):
        pass
    with OwnerStateStore.open(state, scratch_directory=second):
        assert json.loads(marker.read_text())['path'] == str(second)
    shutil.rmtree(second)
    with OwnerStateStore.open(state, scratch_directory=first):
        assert json.loads(marker.read_text())['path'] == str(first)
    with OwnerStateStore.open(state):
        assert not marker.exists()
    with OwnerStateStore.open(state):
        assert not marker.exists()


@pytest.mark.parametrize('container', ['targets', 'snapshots'])
@pytest.mark.parametrize('new_scratch', [True, False])
def test_scratch_marker_refuses_rebind_with_old_target(tmp_path, container, new_scratch):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair):
        pass
    (pair / container / 'owned-target').mkdir(parents=True)
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(state, scratch_directory=tmp_path / 'new' if new_scratch else None)
    assert caught.value.code == 'invalid_argument'
    assert str(pair) in str(caught.value)
    from evolution_lab.service.errors import error_response
    assert error_response(caught.value)['body']['error']['message'] == OwnerError('invalid_argument', 'connect').failure.message
    assert json.loads((state / 'scratch.json').read_text())['path'] == str(pair)


@pytest.mark.parametrize('raw', [b'null', b'garbled', b'{}', b'{"schema":"lab-owner-scratch/1","path":"relative"}',
    b'{"schema":"lab-owner-scratch/1","path":"/tmp/../bad"}',
    b'{"schema":"lab-owner-scratch/1","path":123}',
    b'{"schema":"lab-owner-scratch/1","path":"/tmp/ok","extra":1}',
    b'{"schema":"lab-owner-scratch/1","path":"/tmp/ok","path":"/tmp/other"}'])
def test_scratch_marker_refuses_garbled_instead_of_rebinding(tmp_path, raw):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state):
        pass
    marker = state / 'scratch.json'
    marker.write_bytes(raw)
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(state, scratch_directory=tmp_path / 'pair')
    assert caught.value.code == 'invalid_argument'
    assert marker.read_bytes() == raw


@pytest.mark.parametrize('substitution', ['marker_symlink', 'root_symlink', 'targets_symlink'])
def test_scratch_marker_refuses_unsafe_recorded_paths(tmp_path, substitution):
    state, pair = tmp_path / 'state', tmp_path / 'pair'
    with OwnerStateStore.open(state, scratch_directory=pair):
        pass
    if substitution == 'marker_symlink':
        marker = state / 'scratch.json'
        marker.rename(state / 'original.json')
        marker.symlink_to(state / 'original.json')
    elif substitution == 'root_symlink':
        pair.rmdir()
        pair.symlink_to(tmp_path, target_is_directory=True)
    else:
        (pair / 'targets').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(state, scratch_directory=tmp_path / 'new')
    assert caught.value.code == 'invalid_argument'


def test_scratch_marker_is_not_rewritten_without_owner_flock(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state, scratch_directory=tmp_path / 'pair'):
        before = (state / 'scratch.json').read_bytes()
        with pytest.raises(OwnerError) as caught:
            OwnerStateStore.open(state, scratch_directory=tmp_path / 'new')
        assert caught.value.code == 'busy'
        assert (state / 'scratch.json').read_bytes() == before


@pytest.mark.parametrize('kind', ['unreadable', 'fifo'])
def test_scratch_marker_refuses_unreadable_and_fifo(tmp_path, kind):
    import os
    state = tmp_path / 'state'
    with OwnerStateStore.open(state):
        pass
    marker = state / 'scratch.json'
    if kind == 'unreadable':
        marker.write_text('{"schema":"lab-owner-scratch/1","path":"/tmp/pair"}')
        marker.chmod(0)
    else:
        os.mkfifo(marker, 0o600)
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(state, scratch_directory=tmp_path / 'pair')
    assert caught.value.code == 'invalid_argument'


@pytest.mark.parametrize("durability,journal,synchronous", [
    (None, "wal", 1), ("strict", "delete", 2), ("relaxed", "wal", 1),
])
def test_owner_durability(tmp_path, durability, journal, synchronous, monkeypatch):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    kwargs = {} if durability is None else {"durability": durability}
    with OwnerStateStore.open(tmp_path / "state", **kwargs) as store:
        assert store._database.execute("PRAGMA journal_mode").fetchone()[0] == journal
        assert store._database.execute("PRAGMA synchronous").fetchone()[0] == synchronous


@linux_only('WAL and SHM identity through the Linux descriptor path')
def test_owner_relaxed_proc_fd_wal_and_strict_reopen(tmp_path):
    state = tmp_path / "state"
    with OwnerStateStore.open(state, durability="relaxed") as store:
        generation = store.generation  # opening writes this generation through the real /proc/self/fd path
        proc_root = Path(f"/proc/self/fd/{store._root_fd}")
        assert (proc_root / "owner.sqlite3-wal").samefile(state / "owner.sqlite3-wal")
        assert (proc_root / "owner.sqlite3-shm").samefile(state / "owner.sqlite3-shm")
    with OwnerStateStore.open(state, durability="strict") as store:
        assert store._database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert store._database.execute("SELECT id FROM generations WHERE id=?", (generation,)).fetchone()[0] == generation
        assert not (state / "owner.sqlite3-wal").exists()
        assert not (state / "owner.sqlite3-shm").exists()


@pytest.mark.parametrize('phase', ['before_go', 'after_go'])
def test_actual_owner_death_around_go_retains_orphan_until_full_cleanup(tmp_path, phase):
    from evolution_lab.owner import Owner
    from test_owner_cleanup import prepared_target
    state, bundle, prepared = prepared_target(tmp_path)
    script = '''import asyncio,json,sys,threading
from evolution_lab.owner import Owner,TargetRef
from evolution_lab.owner.store import OwnerStateStore
async def main():
 async with Owner.open(sys.argv[1]) as owner:
  task=await owner.start_target(await owner.bind(sys.argv[2]),secrets={'TARGET_TOKEN':'x'})
  target=TargetRef((await owner.wait(task)).ref.id,owner.owner_generation)
  if sys.argv[4]=='before_go':
   original=OwnerStateStore.mark_dispatch_possible
   def held(self,operation_id):
    value=original(self,operation_id)
    print(json.dumps({'id':operation_id}),flush=True)
    threading.Event().wait()
   OwnerStateStore.mark_dispatch_possible=held
  operation=await owner.start(sys.argv[3],target_ref=target,secrets={})
  if sys.argv[4]=='after_go':
   runtime=owner._operations[operation.id]
   while runtime.helper is None: await asyncio.sleep(.01)
   await runtime.helper.wait_event('child_started')
   print(json.dumps({'id':operation.id}),flush=True)
  await asyncio.Event().wait()
asyncio.run(main())
'''
    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', script,
            str(state), bundle.digest, prepared.digest, phase, stdout=asyncio.subprocess.PIPE)
        try:
            row = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        async with Owner.open(state) as owner:
            ref = ResourceRef('operation', row['id'])
            value = await owner.status(ref)
            assert value.control_state == 'orphaned'
            assert value.dispatch_possible is True
            with pytest.raises(OwnerError):
                await owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'x'})
            result = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert result.state == 'succeeded', result.to_payload()
            assert not (await owner.health()).blockers
            scopes = await owner._db('resource_scopes', ref, include_target=True)
            assert len(scopes) == 2 and all(scope['state'] == 'released' for scope in scopes)
    asyncio.run(scenario())


def test_commit_denial_prevents_real_campaign_helper_and_caller_cancel_keeps_admission(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    original = OwnerStateStore.accept_operation
    def denied(self, **kwargs):
        self._database.set_authorizer(lambda action, arg1, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT' else sqlite3.SQLITE_OK)
        try:
            return original(self, **kwargs)
        finally:
            self._database.set_authorizer(None)
    entered, release = threading.Event(), threading.Event()
    def delayed(self, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(self, **kwargs)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, 'accept_operation', denied)
            with pytest.raises(OwnerError) as error:
                await owner.start(prepared.digest, target_ref=target, secrets={})
            assert error.value.code == 'io_error'
            assert not await owner.list_operations()
            assert len(list((state / 'scopes').iterdir())) == 1
            monkeypatch.setattr(OwnerStateStore, 'accept_operation', delayed)
            caller = asyncio.create_task(owner.start(prepared.digest, target_ref=target, secrets={}))
            assert await asyncio.to_thread(entered.wait, 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            async with asyncio.timeout(8):
                while not (operations := await owner.list_operations()):
                    await asyncio.sleep(.01)
            value = await finished(owner, ResourceRef('operation', operations[0].id))
            assert value.control_state == 'finished', value.to_payload()
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_failure_after_committed_acceptance_settles_without_helper(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner
    from test_owner_cleanup import prepared_target
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    original = OwnerStateStore.accept_operation
    def failed_return(self, **kwargs):
        original(self, **kwargs)
        raise OSError('private exception must not be persisted')
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, 'accept_operation', failed_return)
            with pytest.raises(OwnerError):
                await owner.start(prepared.digest, target_ref=target, secrets={})
            value, = await owner.list_operations()
            assert value.control_state == 'released' and not value.dispatch_possible
            assert value.process.launched is False and value.process.cleanup_complete is True
            assert len(list((state / 'scopes').iterdir())) == 1
            assert 'private exception' not in json.dumps(value.to_payload())
    asyncio.run(scenario())


def test_dispatch_commit_denial_with_live_helper_never_launches_campaign(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner
    from evolution_lab.owner.receipts import read_cleanup_receipt
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    original = OwnerStateStore.mark_dispatch_possible
    def denied(self, operation_id):
        self._database.set_authorizer(lambda action, arg1, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT' else sqlite3.SQLITE_OK)
        try:
            return original(self, operation_id)
        finally:
            self._database.set_authorizer(None)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(OwnerStateStore, 'mark_dispatch_possible', denied)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', operation.id)
            value = await finished(owner, ref)
            assert not value.dispatch_possible and value.control_state == 'cleanup_required'
            assert value.failure.code == 'io_error'
            runtime = owner._operations[operation.id]
            assert runtime.helper is not None
            result = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert result.state == 'succeeded', result.to_payload()
            receipt = read_cleanup_receipt(runtime.paths.scope_directory, runtime.scope_id, owner.owner_generation)
            assert receipt['schema'] == 'lab-scope-cleanup/2'
            assert receipt['child_launched'] is False and receipt['spawn_closed'] is True
            assert not (runtime.paths.run_directory / 'campaign.jsonl').exists()
            assert not (await owner.health()).blockers
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', ['finished', 'cleanup_required'])
def test_stop_queued_across_finalization_cannot_regress_durable_terminal_state(tmp_path, monkeypatch, terminal):
    from evolution_lab.owner import Owner
    from evolution_lab.owner import cleanup
    from test_owner_cleanup import prepared_target
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    original_finalize = cleanup.CleanupSupport._finalize_operation
    original_settlement = cleanup.read_campaign_settlement
    async def scenario():
        begin, finalize, persisted, returned = (asyncio.Event() for _ in range(4))
        stop_read, stop_continue = asyncio.Event(), asyncio.Event()
        async def held_finalize(self, runtime):
            begin.set()
            await finalize.wait()
            await original_finalize(self, runtime)
            persisted.set()
            await returned.wait()
        def settlement(*args, **kwargs):
            value = original_settlement(*args, **kwargs)
            value['ledger']['uncertain_any'] = terminal == 'cleanup_required'
            return value
        monkeypatch.setattr(cleanup.CleanupSupport, '_finalize_operation', held_finalize)
        monkeypatch.setattr(cleanup, 'read_campaign_settlement', settlement)
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', operation.id)
            runtime = owner._operations[operation.id]
            try:
                await asyncio.wait_for(begin.wait(), 15)
                original_status = owner.status
                async def stale_status(value):
                    status = await original_status(value)
                    stop_read.set()
                    await stop_continue.wait()
                    return status
                monkeypatch.setattr(owner, 'status', stale_status)
                stopping = asyncio.create_task(owner.stop(ref))
                await asyncio.wait_for(stop_read.wait(), 5)
                finalize.set()
                await asyncio.wait_for(persisted.wait(), 8)
                before = await owner._db('operation_status', ref.id)
                assert before.control_state == terminal
                assert not runtime.done.done()
                stop_continue.set()
                receipt = await asyncio.wait_for(stopping, 5)
                after = await owner._db('operation_status', ref.id)
                assert after.control_state == terminal
                assert after.result == before.result and after.failure == before.failure
                assert (await owner.health()).active_operation_id is None
                assert receipt.effect == 'too_late' and receipt.accepted is False
            finally:
                finalize.set()
                stop_continue.set()
                returned.set()
                await asyncio.wait_for(asyncio.shield(runtime.done), 8)
            if terminal == 'cleanup_required':
                await owner._targets[target.id].request_stop()
                await owner._targets[target.id].ended
                released = await owner.release_after_manual_cleanup(ref,
                    reason='External target and fixed port cleared after uncertain campaign')
                assert released.released is True
    asyncio.run(scenario())


ZERO_DIGEST = "0" * 64
ONE_DIGEST = "1" * 64
TWO_DIGEST = "2" * 64
TARGET_ID = "1" * 32
FIRST_OPERATION_ID = "2" * 32
SECOND_OPERATION_ID = "3" * 32


def _executor() -> ExecutorIdentity:
    source = {"evolution_lab.owner._worker": TWO_DIGEST}
    identity = ExecutorIdentity(
        environment_digest=ZERO_DIGEST,
        lab_wheel_sha256=ONE_DIGEST,
        core_wheel_sha256=TWO_DIGEST,
        module_hashes=source,
    )
    source["evolution_lab.owner._worker"] = ZERO_DIGEST
    assert identity.to_payload()["module_hashes"] == {
        "evolution_lab.owner._worker": TWO_DIGEST
    }
    return identity


def _ready_target(store: OwnerStateStore, *, target_id: str = TARGET_ID) -> TargetRef:
    connection = store.bind_connection(
        bundle_digest=ZERO_DIGEST,
        launch_digest=ONE_DIGEST,
        source_kind="campaign",
    )
    accepted = store.accept_target_start(
        connection_id=connection.id,
        launch_digest=ONE_DIGEST,
        bundle_digest=ZERO_DIGEST,
        base_url="http://127.0.0.1:8123",
        target_id=target_id,
    )
    assert set(store.reservations()) == {"target_lifetime", "target_use"}
    store.mark_target_ready(accepted.task_ref.id, boot_id="boot-1")
    assert set(store.reservations()) == {"target_lifetime"}
    return accepted.target_ref


def test_closed_owner_models_validate_refs_and_never_expose_paths() -> None:
    ref = ResourceRef.from_payload({"kind": "operation", "id": FIRST_OPERATION_ID})
    assert ref.to_payload() == {"kind": "operation", "id": FIRST_OPERATION_ID}
    assert TargetRef(TARGET_ID, "generation-1").to_payload() == {
        "schema": "lab-target-ref/1",
        "id": TARGET_ID,
        "owner_generation": "generation-1",
    }
    assert ProcessFacts().to_payload() == {
        "launched": None,
        "exit_code": None,
        "cleanup_complete": None,
        "forced": None,
    }
    assert CleanupFacts.pending().to_payload() == {"state": "pending", "complete": None}
    failure = Failure.make("busy", "before_start").to_payload()
    assert (failure["code"], failure["stage"]) == ("busy", "before_start")

    with pytest.raises(OwnerError) as unknown:
        ResourceRef.from_payload(
            {"kind": "operation", "id": FIRST_OPERATION_ID, "path": "/tmp/escape"}
        )
    assert unknown.value.failure.code == "invalid_request"
    with pytest.raises(OwnerError):
        ResourceRef("operation", "../escape")
    with pytest.raises(OwnerError):
        ExecutorIdentity(
            environment_digest=ZERO_DIGEST,
            lab_wheel_sha256=ONE_DIGEST,
            core_wheel_sha256=TWO_DIGEST,
            module_hashes={"worker": "not-a-digest"},
        )


def test_store_uses_secure_wal_sqlite_and_exclusive_stable_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        assert (state.stat().st_mode & 0o777) == 0o700
        assert ((state / "owner.lock").stat().st_mode & 0o777) == 0o600
        assert ((state / "owner.sqlite3").stat().st_mode & 0o777) == 0o600
        settings = {
            "journal_mode": store._database.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": store._database.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": store._database.execute("PRAGMA foreign_keys").fetchone()[0],
            "user_version": store._database.execute("PRAGMA user_version").fetchone()[0],
        }
        assert settings == {
            "journal_mode": "wal",
            "synchronous": 1,
            "foreign_keys": 1,
            "user_version": 14,
        }
        table_names = {
            row[0]
            for row in store._database.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE ?",
                ("table", "sqlite_%"),
            )
        }
        assert table_names == {
            "approvals",
            "connections",
            "deletions",
            "export_tasks",
            "operation_exports",
            "operation_starts",
            "operation_refusals",
            "execution_policies",
            "operation_start_policies",
            "operation_start_initiators",
            "probe_session_policies",
            "owned_inventory",
            "dispositions",
            "generations",
            "operations",
            "probe_instances",
            "reflector_probe_runs",
            "reservations",
            "scopes",
            "targets",
            "tasks",
        }
        lock_inode = (state / "owner.lock").stat().st_ino
        with pytest.raises(OwnerError) as busy:
            OwnerStateStore.open(state)
        assert busy.value.failure.code == "busy"
        assert (state / "owner.lock").stat().st_ino == lock_inode

    with OwnerStateStore.open(state):
        assert (state / "owner.lock").stat().st_ino == lock_inode


@pytest.mark.parametrize("control_name", ["owner.lock", "owner.sqlite3"])
def test_store_refuses_symlinked_roots_and_control_files(
    tmp_path: Path, control_name: str
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    if control_name == "owner.lock":
        state = tmp_path / "state-link"
        state.symlink_to(actual, target_is_directory=True)
    else:
        state = tmp_path / "state"
        state.mkdir()
        (state / control_name).symlink_to(tmp_path / "outside.sqlite3")

    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(state)
    assert caught.value.failure.code == "io_error"
    assert str(tmp_path) not in str(caught.value)
    assert not (tmp_path / "outside.sqlite3").exists()


def test_acceptance_is_durable_before_dispatch_and_commit_failure_keeps_reservations(
    tmp_path: Path,
) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target_ref = _ready_target(store)
        accepted = store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=target_ref,
            executor=_executor(),
            operation_id=FIRST_OPERATION_ID,
        )
        assert accepted.run_id == f"c-{FIRST_OPERATION_ID}"
        assert accepted.run_directory.name == FIRST_OPERATION_ID
        assert accepted.run_directory.parent == accepted.artifact_root
        assert accepted.status.control_state == "reserved"
        assert accepted.status.dispatch_possible is False
        assert set(store.reservations()) == {
            "execution",
            "target_lifetime",
            "target_use",
        }
        store.mark_helper_possible(accepted.scope_id)
        store._database.set_authorizer(
            lambda action, arg1, _arg2, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(OwnerError) as failed_commit:
            store.mark_dispatch_possible(FIRST_OPERATION_ID)
        assert failed_commit.value.failure.code == "io_error"
        store._database.set_authorizer(None)

        status = store.operation_status(FIRST_OPERATION_ID)
        assert status.dispatch_possible is False
        assert set(store.reservations()) == {
            "execution",
            "target_lifetime",
            "target_use",
        }
        ambiguous = store.settle_pre_dispatch_failure(
            FIRST_OPERATION_ID, Failure.make("spawn_failed", "spawning")
        )
        assert ambiguous.control_state == "cleanup_required"
        store.confirm_scope_cleanup(
            accepted.scope_id, accepted.status.owner_generation, forced=False
        )
        assert store.scope_state(accepted.scope_id) == "cleanup_required"
        assert set(store.reservations()) == {
            "execution",
            "target_lifetime",
            "target_use",
        }
        released = store.release_evidenced_reservations(
            ResourceRef("operation", FIRST_OPERATION_ID),
            scope_ids=(accepted.scope_id,),
            disposition="confirmed_cleanup",
        )
        assert released == (accepted.scope_id,)
        assert set(store.reservations()) == {"target_lifetime"}


def test_old_generation_is_orphaned_without_dispatch_and_blocks_new_acceptance(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = OwnerStateStore.open(state)
    target_ref = _ready_target(first)
    accepted = first.accept_operation(
        prepared_digest=TWO_DIGEST,
        target_ref=target_ref,
        executor=_executor(),
        operation_id=FIRST_OPERATION_ID,
    )
    first.close()

    with OwnerStateStore.open(state) as second:
        status = second.operation_status(FIRST_OPERATION_ID)
        assert status.control_state == "orphaned"
        assert status.dispatch_possible is False
        assert second.scope_state(accepted.scope_id) == "orphaned"
        with pytest.raises(OwnerError) as blocked:
            second.accept_operation(
                prepared_digest=TWO_DIGEST,
                target_ref=target_ref,
                executor=_executor(),
                operation_id=SECOND_OPERATION_ID,
            )
        assert blocked.value.failure.code == "cleanup_required"
        assert second.operation_status(FIRST_OPERATION_ID).dispatch_possible is False


def test_manual_release_is_separate_from_outcome_and_late_receipt_is_scope_local(
    tmp_path: Path,
) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        old_target = _ready_target(store)
        old = store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=old_target,
            executor=_executor(),
            operation_id=FIRST_OPERATION_ID,
        )
        store.mark_operation_cleanup_required(FIRST_OPERATION_ID)
        release = store.release_after_manual_cleanup(
            ResourceRef("operation", FIRST_OPERATION_ID), reason="operator verified process tree"
        )
        assert release.released is True
        assert release.cleanup.to_payload() == {"state": "manual", "complete": None}
        old_status = store.operation_status(FIRST_OPERATION_ID)
        assert old_status.control_state == "released"
        assert old_status.result is None
        assert old_status.failure is None
        assert set(store.reservations()) == {"target_lifetime"}

        store.mark_target_cleanup_required(TARGET_ID)
        store.release_after_manual_cleanup(
            ResourceRef("target", TARGET_ID), reason="operator verified target tree"
        )
        new_target = _ready_target(store, target_id="4" * 32)
        new = store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=new_target,
            executor=_executor(),
            operation_id=SECOND_OPERATION_ID,
        )
        assert set(store.reservations()) == {
            "execution",
            "target_lifetime",
            "target_use",
        }

        store.confirm_scope_cleanup(old.scope_id, old.status.owner_generation, forced=False)
        assert store.operation_status(FIRST_OPERATION_ID).control_state == "released"
        assert store.operation_status(SECOND_OPERATION_ID).control_state == "reserved"
        assert store.scope_state(new.scope_id) == "active"
        assert set(store.reservations()) == {
            "execution",
            "target_lifetime",
            "target_use",
        }

def test_run_namespace_is_never_reused_even_after_manual_release(tmp_path: Path) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=target,
            executor=_executor(),
            operation_id=FIRST_OPERATION_ID,
        )
        store.mark_operation_cleanup_required(FIRST_OPERATION_ID)
        store.release_after_manual_cleanup(
            ResourceRef("operation", FIRST_OPERATION_ID), reason="manual cleanup complete"
        )
        with pytest.raises(OwnerError) as duplicate:
            store.accept_operation(
                prepared_digest=TWO_DIGEST,
                target_ref=target,
                executor=_executor(),
                operation_id=FIRST_OPERATION_ID,
            )
        assert duplicate.value.failure.code == "path_conflict"

        persisted = json.loads(store.debug_operation_row(FIRST_OPERATION_ID)["executor_json"])
        assert persisted["kind"] == "lab-campaign-worker/1"
        assert "secrets" not in persisted


def test_known_no_helper_failure_releases_only_its_operation_slots(tmp_path: Path) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        accepted = store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=target,
            executor=_executor(),
            operation_id=FIRST_OPERATION_ID,
        )
        status = store.settle_pre_dispatch_failure(
            FIRST_OPERATION_ID, Failure.make("binding_mismatch", "before_start")
        )
        assert status.control_state == "released"
        assert status.dispatch_possible is False
        assert store.scope_state(accepted.scope_id) == "released"
        assert set(store.reservations()) == {"target_lifetime"}


def test_invalid_acceptance_input_and_live_manual_release_make_no_state_change(
    tmp_path: Path,
) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        with pytest.raises(OwnerError) as invalid:
            store.accept_operation(
                prepared_digest="not-a-digest",
                target_ref=target,
                executor=_executor(),
                operation_id=FIRST_OPERATION_ID,
            )
        assert invalid.value.failure.code == "invalid_argument"
        assert store.list_operations() == ()
        assert set(store.reservations()) == {"target_lifetime"}

        with pytest.raises(OwnerError) as live_release:
            store.release_after_manual_cleanup(
                ResourceRef("target", TARGET_ID), reason="x" * 257
            )
        assert live_release.value.failure.code == "invalid_argument"
        assert set(store.reservations()) == {"target_lifetime"}


def test_evaluation_operation_tagged_result_and_identity_survive_reopen(tmp_path: Path) -> None:
    from evolution_lab.owner import Coverage, OperationResult

    state = tmp_path / "state"
    identity = ExecutorIdentity(
        environment_digest=ZERO_DIGEST,
        lab_wheel_sha256=ONE_DIGEST,
        core_wheel_sha256=TWO_DIGEST,
        module_hashes={"evolution_lab.owner._evaluation_worker": TWO_DIGEST},
        kind="lab-evaluation-worker/1",
    )
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        accepted = store.accept_operation(
            prepared_digest=TWO_DIGEST,
            target_ref=target,
            executor=identity,
            kind="evaluation",
            operation_id=FIRST_OPERATION_ID,
        )
        result = OperationResult(
            TWO_DIGEST,
            ProcessFacts(launched=True, exit_code=0, cleanup_complete=True, forced=False),
            core={"schema": "evolution-project-result/1", "samples": []},
            coverage=Coverage(2, 1),
            stopped_reason="requested",
            campaign_execution=None,
            kind="evaluation",
        )
        store.update_operation(FIRST_OPERATION_ID, result=result, results_state="present")
        assert accepted.status.kind == "evaluation"
        assert set(store.reservations()) == {"execution", "target_lifetime", "target_use"}

    with OwnerStateStore.open(state) as reopened:
        status = reopened.operation_status(FIRST_OPERATION_ID)
        assert status.kind == "evaluation"
        assert status.executor.kind == "lab-evaluation-worker/1"
        assert status.result == result
        assert status.to_payload()["result"]["campaign_execution"] is None


def test_conformance_task_and_operation_share_one_execution_reservation(tmp_path: Path) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        accepted = store.accept_task(
            action="conformance",
            ref=ResourceRef("target", target.id),
            reserve_target_use=True,
            reserve_execution=True,
        )
        assert set(store.reservations()) == {"execution", "target_lifetime", "target_use"}
        assert store.task_status(accepted.task_ref.id).action == "conformance"
        with pytest.raises(OwnerError) as busy:
            store.accept_operation(
                prepared_digest=TWO_DIGEST,
                target_ref=target,
                executor=_executor(),
                operation_id=SECOND_OPERATION_ID,
            )
        assert busy.value.code == "busy"
        store.mark_helper_possible(accepted.scope_id)
        marked = store.mark_task_dispatch_possible(accepted.task_ref.id)
        assert marked.dispatch_possible is True


def test_conformance_refusal_before_helper_releases_both_use_slots(tmp_path: Path) -> None:
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        accepted = store.accept_task(
            action="conformance",
            ref=ResourceRef("target", target.id),
            reserve_target_use=True,
            reserve_execution=True,
        )
        finished = store.finish_task(
            accepted.task_ref.id,
            failure=Failure.make("conformance_unavailable", "conformance"),
        )
        assert finished.state == "failed"
        assert finished.dispatch_possible is False
        assert set(store.reservations()) == {"target_lifetime"}


@linux_only('the Linux SQLite descriptor connect path')
def test_store_database_path_linux_unchanged(tmp_path, monkeypatch):
    import os
    connected = []
    original = sqlite3.connect

    def connect(path, **kwargs):
        connected.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with OwnerStateStore.open(tmp_path / 'state') as store:
        expected = f'/proc/self/fd/{store._root_fd}/owner.sqlite3'
        assert connected == [expected]
        assert os.stat(expected).st_ino == (tmp_path / 'state/owner.sqlite3').stat().st_ino


@pytest.mark.parametrize('different_directory', [False, True])
def test_store_database_path_darwin_checks_identity(tmp_path, monkeypatch, different_directory):
    import fcntl
    import os
    state, other = tmp_path / 'state', tmp_path / 'other'
    other.mkdir()
    other_db = other / 'owner.sqlite3'
    with sqlite3.connect(other_db) as connection:
        connection.execute('CREATE TABLE foreign_data (value TEXT)')
        connection.execute("INSERT INTO foreign_data VALUES ('untouched')")
    before = other_db.read_bytes(), other_db.stat().st_mtime_ns
    original = sqlite3.connect
    connected = []

    def connect(path, **kwargs):
        connected.append(path)
        return original(path, **kwargs)

    def get_path(fd, command, buffer):
        assert command == fcntl.F_GETPATH and buffer == bytes(1024)
        opened, expected = os.fstat(fd), state.stat()
        assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
        return os.fsencode(other if different_directory else state).ljust(1024, b'\0')

    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(fcntl, 'F_GETPATH', 50, raising=False)
    monkeypatch.setattr(fcntl, 'fcntl', get_path)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    if different_directory:
        with pytest.raises(OwnerError) as failure:
            OwnerStateStore.open(state)
        assert (failure.value.code, failure.value.stage) == ('io_error', 'connect')
    else:
        with OwnerStateStore.open(state) as store:
            assert store.state_directory == state
    assert connected == [str((other if different_directory else state) / 'owner.sqlite3')]
    assert (other_db.read_bytes(), other_db.stat().st_mtime_ns) == before


def test_state_under_symlinked_ancestor_is_accepted(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    with OwnerStateStore.open(link / 'state') as store:
        assert store.state_directory == real.resolve() / 'state'
        assert (store.state_directory / 'owner.sqlite3').is_file()


def test_component_swapped_below_root_is_refused(tmp_path):
    import os
    from evolution_lab.owner.receipts import open_directory_nofollow
    outside = tmp_path / 'outside'
    (outside / 'scope').mkdir(parents=True)
    with OwnerStateStore.open(tmp_path / 'state') as store:
        scopes = store.state_directory / 'scopes'
        scopes.rmdir()
        scopes.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            fd = open_directory_nofollow(scopes / 'scope')
            os.close(fd)


def test_scratch_under_symlinked_ancestor_is_accepted(tmp_path):
    from evolution_lab.owner.scratch import prepare_scratch_directory
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    expected = real.resolve() / 'scratch'
    assert prepare_scratch_directory(link / 'scratch') == expected
    state = tmp_path / 'state'
    with OwnerStateStore.open(state, scratch_directory=link / 'scratch') as store:
        assert store.scratch_directory == expected
        assert json.loads((state / 'scratch.json').read_text())['path'] == str(expected)
    with OwnerStateStore.open(state, scratch_directory=expected) as store:
        assert store.scratch_directory == expected


def test_state_accepts_parent_relative_path(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    with OwnerStateStore.open('../state') as store:
        assert store.state_directory == tmp_path.resolve() / 'state'
        assert (store.state_directory / 'owner.sqlite3').is_file()
