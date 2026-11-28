"""Store v2 (ADR-0015): one operation, several starts, one current scope."""
import pytest

from evolution_lab.owner.models import (
    CleanupFacts, ExecutorIdentity, OwnerError, ProcessFacts, Refusal, TargetRef,
)
from evolution_lab.owner.store import OwnerStateStore


DIGEST = "d" * 64
ZERO_DIGEST = "0" * 64
ONE_DIGEST = "1" * 64
TWO_DIGEST = "2" * 64


def _ready_target(store) -> TargetRef:
    connection = store.bind_connection(
        bundle_digest=ZERO_DIGEST, launch_digest=ONE_DIGEST, source_kind="campaign",
    )
    accepted = store.accept_target_start(
        connection_id=connection.id, launch_digest=ONE_DIGEST, bundle_digest=ZERO_DIGEST,
        base_url="http://127.0.0.1:8123",
    )
    store.mark_target_ready(accepted.task_ref.id, boot_id="boot-1")
    return accepted.target_ref


def _executor(kind: str = "lab-campaign-worker/1") -> ExecutorIdentity:
    module = ("evolution_lab.owner._worker" if kind == "lab-campaign-worker/1"
              else "evolution_lab.owner._evaluation_worker")
    return ExecutorIdentity(
        environment_digest=ZERO_DIGEST,
        lab_wheel_sha256=ONE_DIGEST,
        core_wheel_sha256=TWO_DIGEST,
        module_hashes={module: TWO_DIGEST},
        kind=kind,
    )


def _finish_normally(store, operation_id, scope_id):
    from evolution_lab.owner.models import ResourceRef
    store.mark_helper_possible(scope_id)
    store.mark_dispatch_possible(operation_id)
    store.mark_operation_running(operation_id)
    store.confirm_scope_cleanup(scope_id, store.generation, forced=False)
    store.update_operation(operation_id, process=ProcessFacts(True, 1, True, False),
        target_cleanup=CleanupFacts("confirmed", True), results_state="present")
    store.release_evidenced_reservations(ResourceRef("operation", operation_id),
        scope_ids=(scope_id,), disposition="normal")


def test_schema_version_thirteen(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        assert store._database.execute("PRAGMA user_version").fetchone()[0] == 14
        columns = [row[1] for row in store._database.execute("PRAGMA table_info(operation_exports)")]
        assert columns[-1] == "deleted"
        for table in ("operations", "operation_starts"):
            columns = {row[1]: row for row in store._database.execute(f"PRAGMA table_info({table})")}
            assert columns["core_terminal_json"][2:4] == ("TEXT", 0)


def test_resume_freezes_the_first_start_and_opens_a_second_scope(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        second = store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert second.status.id == first.status.id
        assert second.run_directory == first.run_directory
        assert second.scope_id != first.scope_id
        status = store.operation_status(first.status.id)
        assert status.control_state == "reserved"
        assert [s.start for s in status.starts] == [1, 2]
        one, two = status.starts
        assert one.scope_id == first.scope_id
        assert one.process == ProcessFacts(True, 1, True, False)
        assert one.target_cleanup == CleanupFacts("confirmed", True)
        assert one.settlement == "positive"
        assert two.scope_id == second.scope_id and two.settlement == "unknown"
        assert status.process == ProcessFacts()
        assert status.to_payload()["schema"] == "lab-operation-status/8"
        scopes = store._database.execute(
            "SELECT id,state FROM scopes WHERE operation_id=? ORDER BY start",
            (first.status.id,)).fetchall()
        assert [(r["id"], r["state"]) for r in scopes] == [
            (first.scope_id, "released"), (second.scope_id, "active")]


def test_cleanup_marks_only_the_current_scope(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        second = store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        store.mark_helper_possible(second.scope_id)
        store.mark_dispatch_possible(first.status.id)
        store.mark_operation_running(first.status.id)
        store.mark_operation_cleanup_required(first.status.id)
        rows = dict(store._database.execute(
            "SELECT id,state FROM scopes WHERE operation_id=?", (first.status.id,)).fetchall())
        assert rows[first.scope_id] == "released"
        assert rows[second.scope_id] == "cleanup_required"
        from evolution_lab.owner.models import ResourceRef
        assert [s["id"] for s in store.resource_scopes(
            ResourceRef("operation", first.status.id))] == [second.scope_id]


def test_published_export_belongs_to_the_start_that_published_it(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        old, new = "a" * 64, "b" * 64
        assert store.publish_export(operation, old)["export_id"] == old
        assert store.published_export(operation)["export_id"] == old
        store.accept_resume(operation, target_ref=target, executor=_executor())
        # The first start's archive no longer answers a new export ...
        assert store.published_export(operation) is None
        # ... but its exact ID still reads, until results are deleted.
        assert store.published_export(operation, old) == {
            "operation_id": operation, "export_id": old, "sha256": old,
            "path": f"artifacts/{operation}/exports/{old}.zip"}
        assert store.publish_export(operation, new)["export_id"] == new
        assert store.published_export(operation)["export_id"] == new
        rows = store._database.execute(
            "SELECT export_id,start FROM operation_exports WHERE operation_id=? ORDER BY start",
            (operation,)).fetchall()
        assert [(r["export_id"], r["start"]) for r in rows] == [(old, 1), (new, 2)]


def test_identical_archive_republished_by_a_later_start_is_current_again(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        same = "c" * 64
        store.publish_export(operation, same)
        store.accept_resume(operation, target_ref=target, executor=_executor())
        assert store.published_export(operation) is None
        store.publish_export(operation, same)
        assert store.published_export(operation)["export_id"] == same


@pytest.mark.parametrize("state,code", [
    ("running", "resource_busy"), ("cleanup_required", "cleanup_required"),
])
def test_resume_refuses_an_unfinished_operation(tmp_path, state, code):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        store.mark_helper_possible(first.scope_id)
        store.mark_dispatch_possible(first.status.id)
        store.mark_operation_running(first.status.id)
        if state == "cleanup_required":
            store.mark_operation_cleanup_required(first.status.id)
        with pytest.raises(OwnerError) as caught:
            store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert caught.value.code == code


def test_resume_refuses_after_manual_release_and_accepts_not_needed(tmp_path):
    from evolution_lab.owner.models import Failure, ResourceRef
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        # Pre-dispatch failure: released + not_needed — resumable.
        store.settle_pre_dispatch_failure(first.status.id, Failure.make("lab_error", "before_start"))
        store.update_operation(first.status.id, results_state="present")
        second = store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        store.mark_helper_possible(second.scope_id)
        store.mark_dispatch_possible(first.status.id)
        store.mark_operation_running(first.status.id)
        store.mark_operation_cleanup_required(first.status.id)
        store.release_after_manual_cleanup(ResourceRef("operation", first.status.id),
            reason="operator checked the target by hand")
        with pytest.raises(OwnerError) as caught:
            store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert caught.value.code == "cleanup_unconfirmed"
        rows = dict(store._database.execute(
            "SELECT id,cleanup_state FROM scopes WHERE operation_id=?", (first.status.id,)).fetchall())
        assert rows[first.scope_id] == "not_needed"  # the first start's scope is untouched


def test_resume_refuses_deleted_results_and_evaluation(tmp_path):
    # results_state 'deleted' -> results_deleted; kind 'evaluation' -> invalid_argument.
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        deleted = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        _finish_normally(store, deleted.status.id, deleted.scope_id)
        store.update_operation(deleted.status.id, results_state="deleted")
        evaluation = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor("lab-evaluation-worker/1"), kind="evaluation")
        _finish_normally(store, evaluation.status.id, evaluation.scope_id)
        for operation_id, code in (
            (deleted.status.id, "results_deleted"),
            (evaluation.status.id, "invalid_argument"),
        ):
            with pytest.raises(OwnerError) as precheck:
                store.resume_precheck(operation_id)
            assert precheck.value.code == code
            with pytest.raises(OwnerError) as caught:
                store.accept_resume(operation_id, target_ref=target, executor=_executor())
            assert caught.value.code == code
            assert [s.start for s in store.operation_status(operation_id).starts] == [1]


def test_resume_refuses_released_with_unconfirmed_target_cleanup(tmp_path):
    # A crash between releasing the operation and recording confirmed cleanup
    # (owner/cleanup.py) leaves released + unconfirmed: not admissible.
    from evolution_lab.owner.models import ResourceRef
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        store.mark_helper_possible(first.scope_id)
        store.mark_dispatch_possible(first.status.id)
        store.mark_operation_running(first.status.id)
        store.mark_operation_cleanup_required(first.status.id)
        store.confirm_scope_cleanup(first.scope_id, store.generation, forced=False)
        store.release_evidenced_reservations(ResourceRef("operation", first.status.id),
            scope_ids=(first.scope_id,), disposition="confirmed_cleanup")
        store.update_operation(first.status.id, target_cleanup=CleanupFacts("unconfirmed", None),
            results_state="present")
        assert store.operation_status(first.status.id).control_state == "released"
        for call in (lambda: store.resume_precheck(first.status.id),
                     lambda: store.accept_resume(first.status.id, target_ref=target,
                                                 executor=_executor())):
            with pytest.raises(OwnerError) as caught:
                call()
            assert caught.value.code == "cleanup_unconfirmed"


def test_latest_start_mirrors_a_live_overlay_of_top_level_fields(tmp_path):
    from dataclasses import replace
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        status = store.operation_status(first.status.id)
    live = ProcessFacts(True, None, False, False)
    overlaid = replace(status, process=live)
    assert overlaid.starts[-1].process == live
    assert overlaid.starts[-1].scope_id == status.starts[-1].scope_id
    assert overlaid.starts[-1].settlement == status.starts[-1].settlement
    assert overlaid.starts[0] == status.starts[0]
    assert overlaid.starts[0].process == ProcessFacts(True, 1, True, False)


@pytest.mark.parametrize("setup,expected", [
    ("finished", ("ready", None)),
    ("running", ("busy", "resource_busy")),
    ("cleanup_required", ("cleanup_first", "cleanup_required")),
    ("released_manual", ("refused", "cleanup_unconfirmed")),
    ("released_unconfirmed", ("cleanup_first", "cleanup_unconfirmed")),
    ("not_created", ("refused", "campaign_unreadable")),
    ("deleted", ("refused", "results_deleted")),
])
def test_status_field_reason_is_the_admission_code(tmp_path, setup, expected):
    from evolution_lab.owner.models import ResourceRef, ResumeVerdict
    from evolution_lab.owner.resume_rules import status_field
    suspended = {"suspended": True, "complete": False, "suspensions": []}
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target,
            executor=_executor())
        op = first.status.id
        if setup == "running":
            store.mark_helper_possible(first.scope_id)
            store.mark_dispatch_possible(op)
            store.mark_operation_running(op)
        elif setup == "cleanup_required":
            store.mark_helper_possible(first.scope_id)
            store.mark_dispatch_possible(op)
            store.mark_operation_running(op)
            store.mark_operation_cleanup_required(op)
        elif setup in {"released_manual", "released_unconfirmed"}:
            store.mark_helper_possible(first.scope_id)
            store.mark_dispatch_possible(op)
            store.mark_operation_running(op)
            store.mark_operation_cleanup_required(op)
            if setup == "released_manual":
                store.release_after_manual_cleanup(ResourceRef("operation", op), reason="checked by hand")
                store.update_operation(op, results_state="present")
            else:
                store.confirm_scope_cleanup(first.scope_id, store.generation, forced=False)
                store.release_evidenced_reservations(ResourceRef("operation", op),
                    scope_ids=(first.scope_id,), disposition="confirmed_cleanup")
                store.update_operation(op, target_cleanup=CleanupFacts("unconfirmed", None),
                    results_state="present")
        else:
            _finish_normally(store, op, first.scope_id)
            if setup == "not_created":
                store.update_operation(op, results_state="not_created")
            if setup == "deleted":
                store.update_operation(op, results_state="deleted")
        durable = store.operation_status(op)
        assert status_field(durable, suspended) == ResumeVerdict(*expected)
        if expected[0] == "ready":
            store.resume_precheck(op)
        else:
            with pytest.raises(OwnerError) as caught:
                store.resume_precheck(op)
            assert caught.value.code == expected[1]


def _seed_resumed_later(store):
    """A starts, B starts, then A resumes: A's latest start is the newest one."""
    target = _ready_target(store)
    a = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
    _finish_normally(store, a.status.id, a.scope_id)
    b = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
    _finish_normally(store, b.status.id, b.scope_id)
    before = store.cycle_starts()
    store.accept_resume(a.status.id, target_ref=target, executor=_executor())
    return a.status.id, b.status.id, before


def test_cycle_starts_follow_the_latest_start_of_each_operation(tmp_path):
    """spec §3.10 / ADR-0015: a resume is a new start, so an older operation resumed later
    orders after a newer operation that started once."""
    with OwnerStateStore.open(tmp_path / "state") as store:
        a, b, before = _seed_resumed_later(store)
        after = store.cycle_starts()
        assert before[a]["start"] < before[b]["start"]
        assert after[b]["start"] == before[b]["start"]
        assert after[a]["start"] > after[b]["start"]


def test_cycle_starts_report_only_the_export_of_the_current_start(tmp_path):
    """codex r1 C1: the start key and its export come from one read, so start 1's archive
    is never reported next to start 2's key."""
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        assert store.cycle_starts()[operation]["export_id"] is None
        old, new = "a" * 64, "b" * 64
        store.publish_export(operation, old)
        one = store.cycle_starts()[operation]
        assert one["export_id"] == old
        store.accept_resume(operation, target_ref=target, executor=_executor())
        two = store.cycle_starts()[operation]
        assert two == {"start": two["start"], "export_id": None, "export_deleted": False,
                       "results_state": "present"} and two["start"] > one["start"]
        store.publish_export(operation, new)
        assert store.cycle_starts()[operation] == {"start": two["start"], "export_id": new,
                                                   "export_deleted": False, "results_state": "present"}


def test_owner_cycle_starts_is_a_read_that_survives_a_restart(tmp_path):
    import asyncio
    from evolution_lab.owner import Owner
    with OwnerStateStore.open(tmp_path / "state") as store:
        a, b, _ = _seed_resumed_later(store)
        store.publish_export(b, "e" * 64)
        stored = store.cycle_starts()

    async def scenario():
        async with Owner.open(tmp_path / "state") as owner:
            return await owner.cycle_starts()
    assert asyncio.run(scenario()) == stored
    assert stored[b]["export_id"] == "e" * 64 and stored[a]["export_id"] is None


def test_deleted_results_keep_a_tombstone_of_the_current_start_export(tmp_path):
    """W55 (1), decision А/Б: the export row survives a successful deletion as a mark, in the
    same transaction as results_state 'deleted'; no reader sees it (ADR-0006)."""
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        archive = "a" * 64
        store.publish_export(operation, archive)
        key = store.cycle_starts()[operation]["start"]
        assert store.cycle_starts()[operation] == {
            "start": key, "export_id": archive, "export_deleted": False, "results_state": "present"}
        deletion = store.accept_deletion(operation)
        # Decision Г: while deleting, the row is not marked yet.
        assert store.cycle_starts()[operation] == {
            "start": key, "export_id": archive, "export_deleted": False, "results_state": "deleting"}
        store.finish_deletion(operation, deletion.task_ref.id)
        assert store.cycle_starts()[operation] == {
            "start": key, "export_id": archive, "export_deleted": True, "results_state": "deleted"}
        assert store.published_export(operation) is None
        assert store.published_export(operation, archive) is None
        with pytest.raises(OwnerError) as refused:
            store.publish_export(operation, archive)
        assert refused.value.code == "results_deleted"
        rows = store._database.execute(
            "SELECT export_id, deleted FROM operation_exports WHERE operation_id=?", (operation,)).fetchall()
        assert [(r["export_id"], r["deleted"]) for r in rows] == [(archive, 1)]


def test_deleted_results_without_an_export_report_the_state_only(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        deletion = store.accept_deletion(operation)
        store.finish_deletion(operation, deletion.task_ref.id)
        got = store.cycle_starts()[operation]
        assert got == {"start": got["start"], "export_id": None, "export_deleted": False,
                       "results_state": "deleted"}


def test_a_failed_deletion_leaves_the_export_unmarked(tmp_path):
    from evolution_lab.owner.models import Failure
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = first.status.id
        _finish_normally(store, operation, first.scope_id)
        store.publish_export(operation, "a" * 64)
        deletion = store.accept_deletion(operation)
        store.finish_deletion(operation, deletion.task_ref.id, failure=Failure.make("io_error", "delete"))
        got = store.cycle_starts()[operation]
        assert (got["export_id"], got["export_deleted"], got["results_state"]) == ("a" * 64, False, "delete_failed")


def test_refusal_is_stored_with_its_start_and_reset_by_resume(tmp_path):
    from evolution_lab.owner.models import Failure
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        op = first.status.id
        _finish_normally(store, op, first.scope_id)
        refusal = Refusal("journal_busy", None, "hard", None)
        store.update_operation(op, failure=Failure.make("core_refused", "running"), refusal=refusal)
        assert store.operation_status(op).refusal == refusal
        store.accept_resume(op, target_ref=target, executor=_executor())
        status = store.operation_status(op)
        assert status.refusal is None and status.failure is None
        assert status.starts[0].refusal == refusal and status.starts[-1].refusal is None


def test_refusal_survives_reopening_the_store(tmp_path):
    from evolution_lab.owner.models import Failure
    refusal = Refusal("journal_busy", "a/b", "hard", "f" * 64)
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        op = first.status.id
        store.update_operation(op, failure=Failure.make("core_refused", "running"), refusal=refusal)
    with OwnerStateStore.open(tmp_path / "state") as store:
        status = store.operation_status(op)
        assert status.refusal == refusal and status.starts[-1].refusal == refusal


@pytest.mark.parametrize("version", [6, 7])
def test_earlier_schema_is_refused_without_migration(tmp_path, version):
    import sqlite3
    state = tmp_path / "state"
    with OwnerStateStore.open(state):
        pass
    db = state / "owner.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = db.read_bytes()
    with pytest.raises(OwnerError) as raised:
        OwnerStateStore.open(state)
    assert (raised.value.code, raised.value.stage) == ("unsupported_schema", "connect")
    assert db.read_bytes() == before


def test_resume_freezes_core_terminal_with_start(tmp_path):
    from evolution_lab.owner.models import CoreTerminal
    terminal = CoreTerminal("campaign_journal", "persistence_failed", "record_matches_secret", {"ru": "подсказка", "en": "hint"})
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        op = first.status.id
        _finish_normally(store, op, first.scope_id)
        store.update_operation(op, core_terminal=terminal)
        store.accept_resume(op, target_ref=target, executor=_executor())
        status = store.operation_status(op)
        assert status.core_terminal is None
        assert status.starts[0].core_terminal == terminal and status.starts[-1].core_terminal is None
        _finish_normally(store, op, status.starts[-1].scope_id)
        deletion = store.accept_deletion(op)
        store.finish_deletion(op, deletion.task_ref.id)
        assert store.operation_status(op).starts[0].core_terminal == terminal
    with OwnerStateStore.open(state) as store:
        status = store.operation_status(op)
        assert status.starts[0].core_terminal == terminal and status.core_terminal is None


def test_reflector_scopes_block_predispatch_release_until_all_receipts(tmp_path):
    from evolution_lab.owner.models import Failure, ResourceRef
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = accepted.status.id
        store.begin_reflector_probe(operation, launched_by='lab')
        scope = store.accept_reflector_probe_scope(operation)
        store.mark_helper_possible(scope)
        ref = ResourceRef('operation', operation)
        assert [s['id'] for s in store.resource_scopes(ref)] == [accepted.scope_id, scope]
        failed = store.settle_pre_dispatch_failure(operation, Failure.make('io_error', 'before_start'))
        assert failed.control_state == 'cleanup_required'
        assert 'execution' in store.reservations()
        store.confirm_scope_cleanup(scope, store.generation, forced=False)
        store.settle_scope_no_helper(accepted.scope_id)
        store.release_evidenced_reservations(ref, scope_ids=(accepted.scope_id, scope), disposition='confirmed_cleanup')
        assert store.operation_status(operation).control_state == 'released'
        assert set(store.reservations()) == {'target_lifetime'}


def test_reflector_scope_history_stays_attached_to_its_start(tmp_path):
    from evolution_lab.owner.models import ResourceRef
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        one = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        ref = ResourceRef('operation', one.status.id)
        store.begin_reflector_probe(ref.id, launched_by='lab')
        probe = store.accept_reflector_probe_scope(ref.id)
        store.settle_scope_no_helper(probe)
        store.confirm_scope_cleanup(one.scope_id, store.generation, forced=False)
        store.release_evidenced_reservations(ref, scope_ids=(one.scope_id, probe), disposition='normal')
        store.update_operation(ref.id, results_state='present', target_cleanup=CleanupFacts('confirmed', True))
        two = store.accept_resume(ref.id, target_ref=target, executor=_executor())
        store.begin_reflector_probe(ref.id, launched_by='lab')
        fresh = store.accept_reflector_probe_scope(ref.id)
        assert [s['id'] for s in store.resource_scopes(ref)] == [two.scope_id, fresh]
        assert store.reflector_probe_status(ref.id) == {
            'launched_by': 'lab', 'state': 'stopped', 'restarts': 0, 'last_exit': None}
        assert store.reflector_probe_status(ref.id, start=1)['restarts'] == 0
        store.mark_operation_cleanup_required(ref.id)
        released = store.release_after_manual_cleanup(ref, reason='operator verified', include_target=True)
        assert set(released.released_scopes) == {two.scope_id, fresh, store.target_scope(target.id)['id']}
        assert store.scope_state(probe) == 'released'


@pytest.mark.parametrize('state', ['stopping', 'finished', 'released'])
def test_reflector_scope_admission_returns_no_scope_for_current_run_end(tmp_path, state):
    from evolution_lab.owner.models import Failure, ResourceRef
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = accepted.status.id
        store.begin_reflector_probe(operation, launched_by='lab')
        if state == 'released':
            store.settle_pre_dispatch_failure(operation, Failure.make('dispatch_refused', 'before_start'))
        elif state == 'finished':
            _finish_normally(store, operation, accepted.scope_id)
        else:
            store.mark_helper_possible(accepted.scope_id)
            store.mark_dispatch_possible(operation)
            store.mark_operation_running(operation)
            store.request_operation_stop(operation)
        assert store.operation_status(operation).control_state == state
        before = store.resource_scopes(ResourceRef('operation', operation))
        assert store.accept_reflector_probe_scope(operation) is None
        assert store.resource_scopes(ResourceRef('operation', operation)) == before
        assert store.reflector_probe_status(operation)['restarts'] == 0


@pytest.mark.parametrize('refusal', ['missing', 'foreign_generation', 'cleanup_required', 'orphaned'])
def test_reflector_scope_admission_keeps_ownership_and_cleanup_refusals_strict(tmp_path, refusal):
    from evolution_lab.owner.models import ResourceRef
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = accepted.status.id
        store.begin_reflector_probe(operation, launched_by='lab')
        if refusal == 'foreign_generation':
            store._database.execute('INSERT INTO generations VALUES(?,0,NULL)', ('f' * 32,))
            store._database.execute("UPDATE operations SET owner_generation=?,control_state='stopping' WHERE id=?",
                                    ('f' * 32, operation))
        elif refusal in {'cleanup_required', 'orphaned'}:
            store._database.execute('UPDATE operations SET control_state=? WHERE id=?', (refusal, operation))
        before = store.resource_scopes(ResourceRef('operation', operation))
        with pytest.raises(OwnerError) as caught:
            store.accept_reflector_probe_scope('e' * 32 if refusal == 'missing' else operation)
        assert caught.value.code == 'dispatch_refused'
        assert store.resource_scopes(ResourceRef('operation', operation)) == before
        assert store.reflector_probe_status(operation)['restarts'] == 0


def test_reflector_restart_scope_and_count_commit_or_roll_back_together(tmp_path):
    from evolution_lab.owner.models import ResourceRef
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        operation = accepted.status.id
        store.begin_reflector_probe(operation, launched_by='lab')
        initial = store.accept_reflector_probe_scope(operation)
        assert store.reflector_probe_status(operation)['restarts'] == 0
        restarted = store.accept_reflector_probe_scope(operation, restart=True)
        before = store.resource_scopes(ResourceRef('operation', operation))
        assert [s['id'] for s in before] == [accepted.scope_id, initial, restarted]
        assert store.reflector_probe_status(operation)['restarts'] == 1
        # A real SQLite failure after accepting the scope/count must roll both back.
        store._database.execute("CREATE TRIGGER fail_probe_inventory BEFORE INSERT ON owned_inventory "
                                "BEGIN SELECT RAISE(ABORT, 'injected inventory failure'); END")
        with pytest.raises(OwnerError) as caught:
            store.accept_reflector_probe_scope(operation, restart=True)
        assert caught.value.code == 'io_error'
        assert store.resource_scopes(ResourceRef('operation', operation)) == before
        assert store.reflector_probe_status(operation)['restarts'] == 1
        assert 'execution' in store.reservations()
        store._database.execute('DROP TRIGGER fail_probe_inventory')
        store.mark_helper_possible(accepted.scope_id)
        store.mark_dispatch_possible(operation)
        store.mark_operation_running(operation)
        store.request_operation_stop(operation)
        before_stop_admission = store.resource_scopes(ResourceRef('operation', operation))
        assert store.accept_reflector_probe_scope(operation, restart=True) is None
        assert store.resource_scopes(ResourceRef('operation', operation)) == before_stop_admission
        assert store.reflector_probe_status(operation)['restarts'] == 1


def test_reflector_status_distinguishes_absence_from_missing_evidence(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        assert store.reflector_probe_status(accepted.status.id) is None
        store.begin_reflector_probe(accepted.status.id, launched_by=None)
        assert store.reflector_probe_status(accepted.status.id) == {
            'launched_by': None, 'state': 'not_declared', 'restarts': 0, 'last_exit': None}


def test_schema_nine_refuses_existing_schema_eight_without_migration(tmp_path):
    import sqlite3
    root = tmp_path / 'state'
    with OwnerStateStore.open(root):
        pass
    with sqlite3.connect(root / 'owner.sqlite3') as database:
        database.execute('PRAGMA user_version=8')
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(root)
    assert caught.value.code == 'unsupported_schema'
