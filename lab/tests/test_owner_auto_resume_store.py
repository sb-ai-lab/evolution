"""Durable initiator facts belong to each accepted start."""
import sqlite3

import pytest

from evolution_lab.owner.models import OwnerError, ResourceRef
from evolution_lab.owner.store import OwnerStateStore
from test_owner_store_starts import DIGEST, _executor, _finish_normally, _ready_target


def test_v11_fresh_and_v10_rejected_without_migration(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        assert store._database.execute('PRAGMA user_version').fetchone()[0] == 14
    with sqlite3.connect(state / 'owner.sqlite3') as db:
        db.execute('PRAGMA user_version=10')
    before = (state / 'owner.sqlite3').read_bytes()
    with pytest.raises(OwnerError, match='schema') as caught:
        OwnerStateStore.open(state)
    assert caught.value.code == 'unsupported_schema'
    assert (state / 'owner.sqlite3').read_bytes() == before


def test_initiators_and_attempts_survive_store_restart(tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        second = store.accept_resume(operation, target_ref=target, executor=_executor(), initiated_by='auto_resume')
        _finish_normally(store, operation, second.scope_id)
        third = store.accept_resume(operation, target_ref=target, executor=_executor())
        _finish_normally(store, operation, third.scope_id)
        fourth = store.accept_resume(operation, target_ref=target, executor=_executor(), initiated_by='auto_resume')
        _finish_normally(store, operation, fourth.scope_id)
    with OwnerStateStore.open(state) as store:
        starts = store.operation_status(operation).starts
        assert [(s.initiated_by, s.auto_resume_attempt) for s in starts] == [
            ('human', None), ('auto_resume', 1), ('human', None), ('auto_resume', 2)]
        assert sum(s.initiated_by == 'auto_resume' for s in starts) == 2
        assert store._database.execute('PRAGMA foreign_key_check').fetchall() == []


def test_resume_metadata_failure_rolls_back_acceptance(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        store._database.execute("CREATE TRIGGER fail_metadata BEFORE INSERT ON operation_start_initiators "
                                "BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        with pytest.raises(OwnerError) as caught:
            store.accept_resume(first.status.id, target_ref=target, executor=_executor(), initiated_by='auto_resume')
        assert caught.value.code == 'io_error'
        assert [s.start for s in store.operation_status(first.status.id).starts] == [1]
        assert store._database.execute('SELECT count(*) FROM operation_start_policies').fetchone()[0] == 1
        assert store._database.execute('SELECT count(*) FROM operation_start_initiators').fetchone()[0] == 1


def test_initial_metadata_failure_rolls_back_acceptance(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        store._database.execute("CREATE TRIGGER fail_metadata BEFORE INSERT ON operation_start_initiators "
                                "BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        with pytest.raises(OwnerError) as caught:
            store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        assert caught.value.code == 'io_error'
        assert store._database.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert store._database.execute('SELECT count(*) FROM operation_start_policies').fetchone()[0] == 0


def test_auto_target_stop_never_admits_with_open_probe(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        session = 'a' * 32
        store.admit_probe(session_id=session, target_id=target.id, max_instances=2)
        store.set_probe_state(session, 'open', instance_id='fake')
        with pytest.raises(OwnerError) as caught:
            store.accept_task(action='target_stop', ref=ResourceRef('target', target.id),
                              reserve_target_use=True, require_probe_free=True)
        assert caught.value.code == 'probe_sessions_open'
        assert store._database.execute('SELECT state FROM probe_instances WHERE session_id=?', (session,)).fetchone()[0] == 'open'


def test_auto_cleanup_never_admits_with_open_probe(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        session = 'a' * 32
        store.admit_probe(session_id=session, target_id=target.id, max_instances=2)
        with pytest.raises(OwnerError) as caught:
            store.accept_task(action='cleanup', ref=ResourceRef('operation', first.status.id),
                              require_probe_free=True)
        assert caught.value.code == 'probe_sessions_open'
        assert store._database.execute('SELECT state FROM probe_instances WHERE session_id=?', (session,)).fetchone()[0] == 'admitted'


def test_auto_cleanup_requires_operation_slot_while_scope_is_live(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        store._database.execute("DELETE FROM reservations WHERE slot='target_use'")
        with pytest.raises(OwnerError) as caught:
            store.accept_task(action='cleanup', ref=ResourceRef('operation', first.status.id),
                              require_probe_free=True)
        assert caught.value.code == 'resource_busy'


def test_cancellation_is_ordered_with_transaction_commit():
    import threading
    from evolution_lab.owner.auto_resume import AdmissionFence
    fence = AdmissionFence()
    entered, done = threading.Event(), threading.Event()
    result = []
    def stop():
        entered.set()
        result.append(fence.cancel())
        done.set()
    with fence.guard():
        thread = threading.Thread(target=stop)
        thread.start()
        assert entered.wait(1)
        assert not done.wait(.02)
    thread.join(1)
    assert result == [False]  # commit won the ordering; stop must stop that start
    other = AdmissionFence()
    assert other.cancel() is True
    with pytest.raises(OwnerError) as caught:
        with other.guard():
            pytest.fail('cancelled admission must not run')
    assert caught.value.code == 'dispatch_refused'
