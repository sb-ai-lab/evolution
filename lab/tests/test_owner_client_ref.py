"""client_ref on Owner operations and targets (ADR-0017 p. 4, ADR-0004 amendment)."""
import sqlite3

import pytest

from evolution_lab.owner.models import OwnerError
from evolution_lab.owner.store import OwnerStateStore
from test_owner_store_starts import DIGEST, _executor, _finish_normally

pytestmark = pytest.mark.slow

REF = '{"implementation":"agent","project":"acq"}'


def _resource(kind, identifier):
    from evolution_lab.owner.models import ResourceRef
    return ResourceRef(kind, identifier)


def _target(store, client_ref=None):
    connection = store.bind_connection(source_kind="campaign", bundle_digest="b" * 64, launch_digest="c" * 64)
    reservation = store.accept_target_start(connection_id=connection.id, launch_digest="c" * 64,
                                            bundle_digest="b" * 64, base_url="http://127.0.0.1:9", client_ref=client_ref)
    store.mark_target_ready(reservation.task_ref.id, boot_id="boot")
    return reservation.target_ref


def test_schema_ten_with_client_ref_columns(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        pass
    with sqlite3.connect(tmp_path / "state" / "owner.sqlite3") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 14
        for table, suffix in (("operations", ["client_ref"]),
                              ("targets", ["client_ref", "execution_policy"])):
            columns = [row[1] for row in database.execute(f"PRAGMA table_info({table})")]
            assert columns[-len(suffix):] == suffix


def test_operation_and_target_keep_client_ref(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _target(store, client_ref=REF)
        tagged = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor(), client_ref=REF)
        _finish_normally(store, tagged.status.id, tagged.scope_id)
        plain = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        assert store.client_operations() == ((tagged.status.id, REF),)
        assert store.client_targets() == ((target.id, "b" * 64, REF),)
        assert plain.status.id not in dict(store.client_operations())


def test_client_tasks_are_check_and_conformance_on_tagged_targets(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _target(store, client_ref=REF)
        check = store.accept_task(action="check", ref=_resource("target", target.id), reserve_target_use=True)
        tasks = store.client_tasks()
        assert [(t.task_id, t.action, t.target_id, t.client_ref) for t in tasks] == [(check.task_ref.id, "check", target.id, REF)]


def test_client_ref_is_bounded(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        with pytest.raises(OwnerError):
            _target(store, client_ref="x" * 1025)


def test_old_schema_two_state_is_refused(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        pass
    with sqlite3.connect(tmp_path / "state" / "owner.sqlite3") as database:
        database.execute("PRAGMA user_version = 2")
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(tmp_path / "state")
    assert caught.value.code == "unsupported_schema"


def test_old_schema_three_state_is_refused(tmp_path):
    """A dev31 state (user_version 3) is not opened by dev32 (decision А, ADR-0004)."""
    with OwnerStateStore.open(tmp_path / "state") as store:
        pass
    with sqlite3.connect(tmp_path / "state" / "owner.sqlite3") as database:
        database.execute("PRAGMA user_version = 3")
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(tmp_path / "state")
    assert caught.value.code == "unsupported_schema"


async def _ready_target_with_ref(owner, bundle, client_ref):
    import asyncio
    from evolution_lab.owner import TargetRef
    connection = await owner.bind(bundle.digest)
    task = await owner.start_target(connection, secrets={'TARGET_TOKEN': 'private-neutral-token'}, client_ref=client_ref)
    status = await asyncio.wait_for(owner.wait(task), 8)
    assert status.state == 'succeeded', status.to_payload()
    return connection, TargetRef(status.ref.id, owner.owner_generation), task


async def test_owner_start_and_target_carry_client_ref(tmp_path):
    from test_owner_cleanup import finished, prepared_target
    from evolution_lab.owner import Owner
    state, bundle, prepared = prepared_target(tmp_path)
    async with Owner.open(state) as owner:
        connection, target_ref, task = await _ready_target_with_ref(owner, bundle, REF)
        assert (target_ref.id, bundle.digest, REF) in await owner.client_targets()
        status = await owner.start(prepared.digest, target_ref=target_ref, secrets={}, client_ref=REF)
        await finished(owner, _resource("operation", status.id))
        assert (status.id, REF) in await owner.client_operations()


async def test_reusing_a_ready_target_under_another_client_ref_is_slot_held(tmp_path):
    """M11(d): a ready target is reused only by the same client_ref — another project's (or
    implementation's) start_target on the same Bundle and secrets is refused as target_slot_held."""
    from test_owner_cleanup import prepared_target
    from evolution_lab.owner import Owner
    other = '{"implementation":"agent","project":"twin"}'
    secrets = {'TARGET_TOKEN': 'private-neutral-token'}
    state, bundle, _ = prepared_target(tmp_path)
    async with Owner.open(state) as owner:
        connection, target_ref, _ = await _ready_target_with_ref(owner, bundle, REF)
        with pytest.raises(OwnerError) as caught:
            await owner.start_target(connection, secrets=secrets, client_ref=other)
        assert caught.value.code == 'target_slot_held'
        reused = await owner.start_target(connection, secrets=secrets, client_ref=REF)  # same ref: reuse
        import asyncio
        status = await asyncio.wait_for(owner.wait(reused), 8)
        assert status.state == 'succeeded' and status.ref.id == target_ref.id
        assert [row for row in await owner.client_targets() if row[2] == other] == []


async def test_other_client_ref_and_secrets_is_slot_held(tmp_path):
    """Another project's start with other secrets: the slot is held by a different project, not a secrets mismatch."""
    from test_owner_cleanup import prepared_target
    from evolution_lab.owner import Owner
    state, bundle, _ = prepared_target(tmp_path)
    async with Owner.open(state) as owner:
        connection, _, _ = await _ready_target_with_ref(owner, bundle, REF)
        with pytest.raises(OwnerError) as caught:
            await owner.start_target(connection, secrets={'TARGET_TOKEN': 'other'},
                                     client_ref='{"implementation":"agent","project":"twin"}')
        assert caught.value.code == 'target_slot_held'
        assert caught.value.stage == 'before_start'
