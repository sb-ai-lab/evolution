"""Approval records are durable Owner admission facts."""
import sqlite3
from contextlib import closing
from uuid import UUID

import pytest

from evolution_lab.owner import models
from evolution_lab.owner.store import OwnerStateStore, read_saved_operation_state
from test_owner_store_starts import DIGEST, ONE_DIGEST, ZERO_DIGEST


APPROVAL = {"via": "form", "autonomy": "ask", "tool": "operation_start"}


def _snapshot(database):
    tables = database.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {name: database.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for (name,) in tables}


def test_v13_migration_keeps_existing_rows(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with monkeypatch.context() as patch:
        # Create the database using the migration chain through v13.
        patch.setattr(OwnerStateStore, "_migrate_v13", staticmethod(lambda database: None), raising=False)
        with OwnerStateStore.open(state) as store:
            store.bind_connection(bundle_digest=ZERO_DIGEST, launch_digest=ONE_DIGEST,
                                  source_kind="campaign")

    assert read_saved_operation_state(state, "missing-operation") is None
    with closing(sqlite3.connect(state / "owner.sqlite3")) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 13
        before = _snapshot(database)

    with OwnerStateStore.open(state) as store:
        assert store._database.execute("PRAGMA user_version").fetchone()[0] == 14
        assert read_saved_operation_state(state, "missing-operation") is None
        assert store.approvals_for(operation_id="missing-operation") == []
    with closing(sqlite3.connect(state / "owner.sqlite3")) as database:
        after = _snapshot(database)
        assert after.pop("approvals") == []
        # An Owner open adds its own generation independently of migration.
        assert len(after["generations"]) == len(before["generations"]) + 1
        after["generations"].pop()
        assert after == before


@pytest.mark.parametrize("payload", [
    None,
    {"via": "form", "autonomy": "ask"},
    {**APPROVAL, "extra": "value"},
    {**APPROVAL, "via": "auto"},
    {**APPROVAL, "via": []},
    {**APPROVAL, "autonomy": "ask-paid"},
    {**APPROVAL, "autonomy": {}},
    {**APPROVAL, "tool": ""},
    {**APPROVAL, "tool": "unknown_tool"},
    {**APPROVAL, "tool": None},
])
def test_approval_decode_refuses_invalid_input(payload):
    with pytest.raises(ValueError):
        models.Approval.decode(payload)


def test_approval_decode_accepts_closed_payload():
    assert models.Approval.decode(APPROVAL) == models.Approval("form", "ask", "operation_start")
    assert models.Approval.decode({"via": "client_rules", "autonomy": "ask", "tool": "project_start"}) == (
        models.Approval("client_rules", "ask", "project_start")
    )
    assert models.Approval.decode({"via": "none", "autonomy": "yolo", "tool": "cleanup"}) == (
        models.Approval("none", "yolo", "cleanup")
    )


def test_admission_refusal_rolls_back_approval(tmp_path):
    approval = models.Approval.decode(APPROVAL)
    with OwnerStateStore.open(tmp_path / "state") as store:
        with pytest.raises(models.OwnerError, match="The state shown for approval has changed"):
            with store._transaction("before_start") as database:
                store._insert_approval(database, approval=approval,
                    request_id="start-1", resource_kind="operation", resource_id="operation-a", start=1)
                raise models.OwnerError("state_changed", "before_start")

        assert store.approvals_for(operation_id="operation-a") == []


def test_approvals_filter_and_order_across_generations(tmp_path):
    state = tmp_path / "state"
    operation_approval = models.Approval.decode(APPROVAL)
    policy_approval = models.Approval("client_rules", "ask", "policy_confirm")
    with OwnerStateStore.open(state) as store:
        original_generation = store.generation
        with store._transaction("before_start") as database:
            first = store._insert_approval(database, approval=operation_approval,
                request_id="start-1", resource_kind="operation", resource_id="operation-a", start=1)
            second = store._insert_approval(database, approval=operation_approval,
                request_id="start-2", resource_kind="operation", resource_id="operation-a", start=2)
            third = store._insert_approval(database, approval=operation_approval,
                request_id="start-3", resource_kind="operation", resource_id="operation-a", start=3)
            store._insert_approval(database, approval=operation_approval,
                request_id="start-b", resource_kind="operation", resource_id="operation-b", start=1)
            policy = store._insert_approval(database, approval=policy_approval,
                request_id="policy-1", resource_kind="execution_policy", resource_id=DIGEST, start=None,
                outcome="applied")
            assert UUID(first).hex == first
            row = database.execute("SELECT owner_generation FROM approvals WHERE id=?", (first,)).fetchone()
            assert row["owner_generation"] == original_generation
            database.execute("UPDATE approvals SET created_at=? WHERE id IN (?,?)",
                             ("2026-11-23T10:00:00.000000Z", first, second))
            database.execute("UPDATE approvals SET created_at=? WHERE id=?",
                             ("2026-11-23T09:00:00.000000Z", third))

    with OwnerStateStore.open(state) as store:
        assert store.generation != original_generation
        records = store.approvals_for(operation_id="operation-a")
        assert [record.id for record in records] == [third, *sorted((first, second))]
        assert records[0].to_payload() == {
            "schema": "lab-approval/1", "id": third, "request_id": "start-3",
            "tool": "operation_start", "via": "form", "autonomy": "ask",
            "resource": {"kind": "operation", "id": "operation-a"},
            "start": 3, "outcome": "admitted", "created_at": "2026-11-23T09:00:00.000000Z",
        }
        [record] = store.approvals_for(policy_digest=DIGEST)
        assert record.id == policy
        assert record.to_payload()["resource"] == {"kind": "execution_policy", "id": DIGEST}
        assert record.to_payload()["start"] is None
        assert record.to_payload()["outcome"] == "applied"
