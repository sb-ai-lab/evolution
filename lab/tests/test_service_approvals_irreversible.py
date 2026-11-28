"""Approval and start binding for irreversible Lab Service mutations."""
import asyncio
import sqlite3

import pytest

from evolution_lab.owner import ResourceRef, TaskRef
from evolution_lab.owner.store import OwnerStateStore
from test_owner_store_starts import DIGEST, ONE_DIGEST, ZERO_DIGEST, _executor
from test_service_approvals_admission import (
    approval, approval_count, assert_approval, headers, offline_target, operation_approvals,
    resume_request, service, start_without_approval, store_call,
)


ACTIONS = ("delete_results", "release", "cleanup")
STORE_ACTIONS = {
    "delete_results": "accept_deletion",
    "release": "release_after_manual_cleanup",
    "cleanup": "accept_task",
}
STAGES = {"delete_results": "delete", "release": "release", "cleanup": "cleanup"}


async def _send(client, action, ref, *, key=None, approved=True, observed_start=1, via="form"):
    if action == "delete_results":
        method, path = "DELETE", f"/v1/operations/{ref.id}/results"
        body = {"schema": "lab-service-delete-results-request/1"} if approved else None
    elif action == "cleanup":
        method, path = "POST", "/v1/cleanup"
        body = {"schema": f"lab-service-cleanup-request/{2 if approved else 1}",
                "ref": ref.to_payload(), "wait_seconds": 2}
    else:
        method, path = "POST", "/v1/releases"
        body = {"schema": f"lab-service-release-request/{2 if approved else 1}",
                "ref": ref.to_payload(), "reason": "operator verified cleanup"}
    if approved:
        body.update(approval={**approval(action), "via": via}, observed_start=observed_start)
    return await client.request(method, path, headers=key, json=body)


async def _operation(action, service, target):
    owner, _ = service
    if action == "release":
        accepted = await owner._db("accept_operation", prepared_digest=DIGEST, target_ref=target,
                                   executor=_executor())
        operation = accepted.status.id
        await owner._db("mark_operation_cleanup_required", operation)
    else:
        operation = await start_without_approval(service, target)
    return ResourceRef("operation", operation)


async def _state(owner):
    def snapshot(store):
        return {
            table: [tuple(row) for row in store._database.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("operations", "operation_starts", "tasks", "deletions",
                          "scopes", "reservations", "dispositions", "targets")
        }
    return await store_call(owner, snapshot)


async def _assert_completed(owner, response, action, ref):
    if action == "release":
        assert response.status_code == 200, response.text
        assert response.json() == {
            "schema": "lab-owner-release/1", "ref": ref.to_payload(),
            "disposition": "manual", "reason": "operator verified cleanup",
            "cleanup": {"state": "manual", "complete": None}, "released": True,
        }
        if ref.kind == "operation":
            status = await owner._db("operation_status", ref.id)
            assert status.control_state == "released"
    else:
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert receipt["schema"] == "lab-task-ref/1"
        status = await owner.wait(TaskRef(receipt["id"], receipt["owner_generation"]))
        assert status.state == "succeeded", status.to_payload()
        if action == "delete_results":
            operation = await owner._db("operation_status", ref.id)
            assert operation.results_state == "deleted"


@pytest.mark.parametrize("action", ACTIONS)
async def test_approval_is_recorded_once_and_compared_on_replay(service, offline_target, action):
    owner, client = service
    ref = await _operation(action, service, offline_target)
    key = headers(owner, "approved")
    accepted = await _send(client, action, ref, key=key)
    await _assert_completed(owner, accepted, action, ref)
    replay = await _send(client, action, ref, key=key)
    assert replay.status_code == accepted.status_code
    assert replay.json() == accepted.json()
    conflict = await _send(client, action, ref, key=key, via="client_rules")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "request_conflict"
    record, = await operation_approvals(owner, ref.id)
    resource_kind = "task" if action == "cleanup" else "operation"
    resource_id = accepted.json()["id"] if action == "cleanup" else ref.id
    assert_approval(record, request_id="approved", tool=action, resource_kind=resource_kind,
                    resource_id=resource_id, start=1, outcome="applied" if action == "release" else "admitted")
    assert await approval_count(owner) == 1


@pytest.mark.parametrize("action", ACTIONS)
async def test_legacy_mutation_keeps_response_and_writes_no_approval(service, offline_target, action):
    owner, client = service
    ref = await _operation(action, service, offline_target)
    key = headers(owner, "legacy")
    accepted = await _send(client, action, ref, key=key, approved=False)
    await _assert_completed(owner, accepted, action, ref)
    replay = await _send(client, action, ref, key=key, approved=False)
    assert replay.status_code == accepted.status_code
    assert replay.json() == accepted.json()
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("action", ACTIONS)
async def test_stale_start_cannot_change_rows(service, offline_target, action):
    owner, client = service
    ref = await _operation(action, service, offline_target)
    before = await _state(owner)
    response = await _send(client, action, ref, key=headers(owner, "stale"), observed_start=2)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["stage"]) == ("state_changed", STAGES[action])
    assert await _state(owner) == before
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("action", ACTIONS)
async def test_admission_rechecks_start_inside_transaction(service, offline_target, monkeypatch, action):
    owner, client = service
    ref = await _operation("cleanup", service, offline_target)
    original_db = owner._db
    interleaved = None

    async def db(method, *args, **kwargs):
        nonlocal interleaved
        if method == STORE_ACTIONS[action]:
            # Commit a new start after the Owner's last read before admission.
            await original_db("accept_resume", ref.id, target_ref=offline_target, executor=_executor())
            if action == "release":
                await original_db("mark_operation_cleanup_required", ref.id)
            interleaved = await _state(owner)
        return await original_db(method, *args, **kwargs)

    monkeypatch.setattr(owner, "_db", db)
    response = await _send(client, action, ref, key=headers(owner, "advance-before-admission"))
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["stage"]) == ("state_changed", STAGES[action])
    assert interleaved is not None
    assert await _state(owner) == interleaved
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("action", ACTIONS)
async def test_approval_insert_failure_rolls_back_admission(service, offline_target, monkeypatch, action):
    owner, client = service
    ref = await _operation(action, service, offline_target)
    before = await _state(owner)
    original_insert = OwnerStateStore._insert_approval

    def fail_insert(*args, **kwargs):
        original_insert(*args, **kwargs)
        raise sqlite3.OperationalError("approval insert failed")

    monkeypatch.setattr(OwnerStateStore, "_insert_approval", fail_insert)
    response = await _send(client, action, ref, key=headers(owner, "rollback"))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "io_error"
    assert await _state(owner) == before
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("action", ACTIONS)
async def test_approved_mutation_requires_key_and_positive_integer_start(service, offline_target, action):
    owner, client = service
    ref = await _operation(action, service, offline_target)
    assert (await _send(client, action, ref)).status_code == 400
    for value in (None, True, 0, -1, "1"):
        refused = await _send(client, action, ref, key=headers(owner, "invalid"), observed_start=value)
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == "invalid_request"
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("action", ("cleanup", "release"))
async def test_non_operation_approval_records_receipt_without_start(service, action):
    owner, client = service

    def start_target(store):
        connection = store.bind_connection(bundle_digest=ZERO_DIGEST, launch_digest=ONE_DIGEST,
                                           source_kind="campaign")
        target = store.accept_target_start(connection_id=connection.id, launch_digest=ONE_DIGEST,
                                           bundle_digest=ZERO_DIGEST, base_url="http://127.0.0.1:8123")
        store.mark_target_cleanup_required(target.target_ref.id)
        return target

    target = await store_call(owner, start_target)
    # The task ref resolves to the target for cleanup, but remains the release receipt's ref.
    ref = ResourceRef("task", target.task_ref.id)
    accepted = await _send(client, action, ref, key=headers(owner, "target-cleanup"), observed_start=None)
    await _assert_completed(owner, accepted, action, ref)
    status = await owner._db("target_status", target.target_ref.id)
    assert status.state == "stopped"

    def recorded_resource(store):
        row, = store._database.execute("SELECT resource_kind,resource_id,start,outcome FROM approvals")
        return tuple(row)

    if action == "cleanup":
        expected = ("task", accepted.json()["id"], None, "admitted")
    else:
        expected = ("task", ref.id, None, "applied")
    assert await store_call(owner, recorded_resource) == expected


@pytest.mark.parametrize("approved", [False, True], ids=["cleanup-v1", "cleanup-v2"])
async def test_resume_cannot_advance_operation_during_cleanup_handoff(
    service, offline_target, monkeypatch, approved,
):
    owner, client = service
    ref = await _operation("cleanup", service, offline_target)
    original_db = owner._db
    admitted = asyncio.Event()
    continue_cleanup = asyncio.Event()

    async def db(method, *args, **kwargs):
        if method == "task_running":
            admitted.set()
            await continue_cleanup.wait()
        return await original_db(method, *args, **kwargs)

    monkeypatch.setattr(owner, "_db", db)
    accepted = await _send(client, "cleanup", ref, key=headers(owner, "cleanup-handoff"), approved=approved)
    assert accepted.status_code == 202, accepted.text
    resume_url = f"/v1/operations/{ref.id}/resume"
    try:
        await asyncio.wait_for(admitted.wait(), timeout=2)
        task = await owner._db("task_status", accepted.json()["id"])
        assert task.state == "pending"
        records = await operation_approvals(owner, ref.id)
        expected_resources = [{"kind": "task", "id": task.id}] if approved else []
        assert [record["resource"] for record in records] == expected_resources
        before = await _state(owner)
        blocked = await client.post(resume_url, headers=headers(owner, "resume-during-cleanup"),
                                    json=resume_request(offline_target, 1))
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "resource_busy"
        assert await _state(owner) == before
    finally:
        continue_cleanup.set()
    await _assert_completed(owner, accepted, "cleanup", ref)
    resumed = await client.post(resume_url, headers=headers(owner, "resume-after-cleanup"),
                                json=resume_request(offline_target, 1))
    assert resumed.status_code == 202, resumed.text
    assert [start["start"] for start in resumed.json()["starts"]] == [1, 2]
