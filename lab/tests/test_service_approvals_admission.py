"""Approval records share the Owner's admission transactions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from evolution_lab.owner import Owner
from evolution_lab.owner.store import OwnerStateStore
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from test_owner_store_starts import DIGEST, ZERO_DIGEST, _executor, _finish_normally, _ready_target
from test_service_policy import request as policy_request


@pytest.fixture
async def service(tmp_path):
    async with Owner.open(tmp_path / "state") as owner:
        runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state"))
        runtime.owner = owner
        runtime._executor = ThreadPoolExecutor(max_workers=1)
        runtime._admission_open = True
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                         base_url="http://lab") as client:
                yield owner, client
        finally:
            runtime._executor.shutdown(wait=True)


async def store_call(owner, action, *args):
    return await asyncio.get_running_loop().run_in_executor(
        owner._executor, partial(action, owner._store, *args))


@pytest.fixture
async def offline_target(service, monkeypatch):
    from evolution_lab.owner import binding, execution, policy, secrets

    owner, _ = service
    target_ref = await store_call(owner, _ready_target)

    async def no_process_to_stop():
        pass

    owner._targets[target_ref.id] = SimpleNamespace(
        ref=target_ref, binding=SimpleNamespace(source_kind="campaign"), secrets=object(),
        policy_digest=None, execution_policy=None, gateway=None, helper=None,
        request_stop=no_process_to_stop,
    )
    prepared = SimpleNamespace(kind="campaign", prepared_digest=DIGEST,
                               bundle=SimpleNamespace(bundle_digest=ZERO_DIGEST))
    monkeypatch.setattr(binding, "open_prepared", lambda *_: prepared)
    monkeypatch.setattr(binding, "declared_executor_identity", lambda *a, **kw: _executor())
    monkeypatch.setattr(secrets, "bind_execution_secrets", lambda *a, **kw: object())
    monkeypatch.setattr(policy, "execution_tokens", lambda *a, **kw: {})
    monkeypatch.setattr(execution, "_fresh_core", lambda _: {"complete": False, "suspended": True})

    async def finish_without_worker(runtime, _declared):
        await store_call(owner, _finish_normally, runtime.id, runtime.scope_id)

    monkeypatch.setattr(owner, "_run_operation", finish_without_worker)
    return target_ref


def headers(owner, request_id):
    return {"Lab-Request-Id": request_id, "Lab-Owner-Generation": owner.owner_generation}


def approval(tool):
    return {"via": "form", "autonomy": "ask", "tool": tool}


def start_request(target_ref, *, with_approval=True):
    body = {"schema": "lab-service-start-request/3", "prepared_digest": DIGEST,
            "target_ref": target_ref.to_payload(), "secrets": {}, "linked_from": None,
            "baseline": "inherit", "seed": "champion", "baseline_from": None}
    if with_approval:
        body.update(schema="lab-service-start-request/4", approval=approval("operation_start"))
    return body


def resume_request(target_ref, observed_start):
    return {"schema": "lab-service-resume-request/3", "target_ref": target_ref.to_payload(),
            "secrets": {}, "accept": [], "reason": None, "prepared_digest": None,
            "approval": approval("operation_resume"), "observed_start": observed_start}


def confirm_request():
    return {**policy_request(), "schema": "lab-service-policy-confirm-request/2",
            "channel": "mcp", "approval": approval("policy_confirm")}


async def operation_approvals(owner, operation_id):
    records = await owner._db("approvals_for", operation_id=operation_id)
    return [record.to_payload() for record in records]


async def approval_count(owner):
    def count(store):
        return store._database.execute("SELECT count(*) FROM approvals").fetchone()[0]

    return await store_call(owner, count)


def assert_approval(record, *, request_id, tool, resource_kind, resource_id, start, outcome):
    assert record["schema"] == "lab-approval/1"
    assert record["request_id"] == request_id
    assert record["tool"] == tool
    assert record["via"] == "form"
    assert record["autonomy"] == "ask"
    assert record["resource"] == {"kind": resource_kind, "id": resource_id}
    assert record["start"] == start
    assert record["outcome"] == outcome


async def test_start_records_approval_once_across_replay(service, offline_target):
    owner, client = service
    body = start_request(offline_target)
    key = headers(owner, "start-with-approval")
    first = await client.post("/v1/operations", headers=key, json=body)
    assert first.status_code == 202, first.text
    operation = first.json()
    assert operation["schema"] == "lab-operation-status/8"
    assert operation["starts"][0]["initiated_by"] == "human"
    replay = await client.post("/v1/operations", headers=key, json=body)
    assert replay.status_code == 202 and replay.json() == operation
    changed = {**body, "approval": {**body["approval"], "via": "client_rules"}}
    conflict = await client.post("/v1/operations", headers=key, json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "request_conflict"
    record, = await operation_approvals(owner, operation["id"])
    assert_approval(record, request_id="start-with-approval", tool="operation_start",
                    resource_kind="operation", resource_id=operation["id"], start=1,
                    outcome="admitted")


async def start_without_approval(service, target_ref):
    owner, client = service
    response = await client.post("/v1/operations", json=start_request(target_ref, with_approval=False))
    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    await owner._operations[operation_id].done
    # The post-finish auto-resume projection also owns a result read lease.
    await asyncio.gather(*tuple(owner._owned))
    return operation_id


async def test_start_v3_is_unkeyed_and_records_no_approval(service, offline_target):
    owner, _ = service
    operation = await start_without_approval(service, offline_target)
    assert await operation_approvals(owner, operation) == []


async def test_start_admission_refusal_records_no_approval(service, offline_target):
    owner, client = service
    await owner._db("accept_operation", prepared_digest=DIGEST, target_ref=offline_target, executor=_executor())
    response = await client.post("/v1/operations", headers=headers(owner, "busy-start"),
                                 json=start_request(offline_target))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "busy"
    assert await approval_count(owner) == 0


async def test_start_rolls_back_if_approval_cannot_be_written(service, offline_target, monkeypatch):
    owner, client = service

    def fail_insert(*args, **kwargs):
        raise sqlite3.OperationalError("approval insert refused")

    monkeypatch.setattr(OwnerStateStore, "_insert_approval", fail_insert)
    response = await client.post("/v1/operations", headers=headers(owner, "rollback-start"),
                                 json=start_request(offline_target))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "io_error"
    assert not await owner.list_operations()
    assert await approval_count(owner) == 0


async def test_resume_records_approved_new_start(service, offline_target):
    owner, client = service
    operation = await start_without_approval(service, offline_target)
    url = f"/v1/operations/{operation}/resume"
    body = resume_request(offline_target, 1)
    key = headers(owner, "resume-with-approval")
    response = await client.post(url, headers=key, json=body)
    assert response.status_code == 202, response.text
    starts = response.json()["starts"]
    assert [(start["start"], start["initiated_by"]) for start in starts] == [(1, "human"), (2, "human")]
    replay = await client.post(url, headers=key, json=body)
    assert replay.status_code == 202 and replay.json() == response.json()
    record, = await operation_approvals(owner, operation)
    assert_approval(record, request_id="resume-with-approval", tool="operation_resume",
                    resource_kind="operation", resource_id=operation, start=2, outcome="admitted")


@pytest.mark.parametrize("advance", ["before_precheck", "before_transaction"])
async def test_stale_resume_refuses_even_when_the_new_start_is_busy(
    service, offline_target, monkeypatch, advance,
):
    owner, client = service
    operation = await start_without_approval(service, offline_target)

    async def accept_other_resume():
        return await owner._db("accept_resume", operation, target_ref=offline_target, executor=_executor())

    if advance == "before_precheck":
        other = await accept_other_resume()
    else:
        # Interleave a committed admission after Owner read the verdict.
        original_db = owner._db

        async def db(method, *args, **kwargs):
            nonlocal other
            if method == "accept_resume":
                other = await original_db("accept_resume", operation, target_ref=offline_target, executor=_executor())
            return await original_db(method, *args, **kwargs)

        other = None
        monkeypatch.setattr(owner, "_db", db)

    response = await client.post(f"/v1/operations/{operation}/resume", headers=headers(owner, "stale-resume"),
                                 json=resume_request(offline_target, 1))
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["stage"]) == ("state_changed", "before_start")
    assert other is not None
    assert await owner._db("operation_status", operation) == other.status
    assert await operation_approvals(owner, operation) == []


async def test_policy_confirmation_records_approval_only_for_first_confirmation(service):
    owner, client = service
    body = confirm_request()
    digest = body["expected_digest"]
    url = "/v1/execution-policies"
    response = await client.post(url, headers=headers(owner, "confirm-policy"), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["schema"] == "lab-execution-policy-record/1"
    assert response.json()["author"] == "operator"
    assert response.json()["channel"] == "mcp"
    duplicate = await client.post(url, headers=headers(owner, "confirm-policy-again"),
                                  json={**body, "author": "someone else"})
    assert duplicate.status_code == 200 and duplicate.json() == response.json()
    record, = await owner._db("approvals_for", policy_digest=digest)
    assert_approval(record.to_payload(), request_id="confirm-policy", tool="policy_confirm",
                    resource_kind="execution_policy", resource_id=digest, start=None, outcome="applied")


async def test_approval_bearing_routes_require_mutation_key(service, offline_target):
    owner, client = service
    operation = await start_without_approval(service, offline_target)
    for path, body in [
        ("/v1/operations", start_request(offline_target)),
        (f"/v1/operations/{operation}/resume", resume_request(offline_target, 1)),
        ("/v1/execution-policies", confirm_request()),
    ]:
        response = await client.post(path, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    assert not await owner.execution_policies()
    assert await approval_count(owner) == 0


@pytest.mark.parametrize("observed_start", [True, 0, -1, None, "1"])
async def test_resume_observation_requires_positive_integer(service, offline_target, observed_start):
    owner, client = service
    operation = await start_without_approval(service, offline_target)
    response = await client.post(f"/v1/operations/{operation}/resume", headers=headers(owner, "invalid-start"),
                                 json=resume_request(offline_target, observed_start))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert await approval_count(owner) == 0
