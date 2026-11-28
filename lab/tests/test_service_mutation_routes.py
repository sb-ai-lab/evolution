"""S7 W28: keyed mutation order on the real runtime and routes, doubles for Owner."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from evolution_lab.owner import OwnerError, TaskRef

TARGET = {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "gen-1"}
DIGEST = "a" * 64


class Public:
    def __init__(self, value):
        self.value = value

    def to_payload(self):
        return {"schema": "test-public/1", "value": self.value}


class FakeOwner:
    owner_generation = "gen-1"

    def __init__(self):
        self.calls = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.failure = None

    async def _task(self, name, *details):
        self.calls.append((name, *details))
        await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return TaskRef(f"task-{len(self.calls)}", self.owner_generation)

    async def start_target(self, connection, *, secrets):
        return await self._task("start_target", dict(secrets))

    async def check(self, connection, *, target_ref):
        return await self._task("check", target_ref.id)

    async def conformance(self, connection, *, target_ref, runtime_policy):
        return await self._task("conformance", target_ref.id)

    async def start(self, prepared_digest, *, target_ref, secrets):
        await self._task("start", prepared_digest)
        return Public(f"operation-{len(self.calls)}")

    async def stop(self, ref):
        self.calls.append(("stop", ref.kind, ref.id))
        return Public("stopped")

    async def stop_target(self, ref):
        return await self._task("stop_target", ref.id, ref.owner_generation)

    async def delete_results(self, ref):
        return await self._task("delete_results", ref.kind, ref.id)

    async def cleanup(self, ref, *, wait_seconds):
        return await self._task("cleanup", ref.kind, ref.id, wait_seconds)

    async def release_after_manual_cleanup(self, ref, *, reason):
        self.calls.append(("release", ref.kind, ref.id, reason))
        return Public(f"released-{len(self.calls)}")


class FakePreparation:
    def __init__(self):
        self.calls = []

    def create_bundle(self, **kwargs):
        self.calls.append(kwargs)
        return Public(f"bundle-{len(self.calls)}")


@pytest.fixture
async def service(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state"))
    runtime.owner, runtime.preparation = FakeOwner(), FakePreparation()
    runtime._executor = ThreadPoolExecutor(max_workers=1)
    runtime._connections["connection"] = SimpleNamespace(id="connection")
    runtime._admission_open = True
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        yield runtime, client
    runtime._executor.shutdown(wait=True)


def key(request_id, generation="gen-1"):
    return {"Lab-Request-Id": request_id, "Lab-Owner-Generation": generation}


async def check(client, headers, target=TARGET, connection="connection"):
    return await client.post(f"/v1/connections/{connection}/check", headers=headers,
        json={"schema": "lab-service-check-request/1", "target_ref": target})


async def target_start(client, headers, secrets):
    return await client.post("/v1/connections/connection/target", headers=headers,
        json={"schema": "lab-service-target-start-request/1", "secrets": secrets})


async def start(client, headers, prepared=DIGEST):
    return await client.post("/v1/operations", headers=headers, json={
        "schema": "lab-service-start-request/1", "prepared_digest": prepared,
        "target_ref": TARGET, "secrets": {}})


async def test_replay_after_lost_response_returns_same_task_ref(service):  # W28-1
    runtime, client = service
    first = await target_start(client, key("k1"), {"TOKEN": "s"})
    second = await target_start(client, key("k1"), {"TOKEN": "s"})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(runtime.owner.calls) == 1


async def test_concurrent_same_key_creates_one_task(service):  # W28-2
    runtime, client = service
    runtime.owner.gate.clear()
    pending = [asyncio.create_task(check(client, key("k2"))) for _ in range(2)]

    async def one_recorded_action():
        while len(runtime._mutations) != 1 or len(runtime.owner.calls) != 1:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(one_recorded_action(), timeout=5)
    await asyncio.sleep(0.05)
    assert not pending[0].done() and not pending[1].done()
    runtime.owner.gate.set()
    first, second = await asyncio.gather(*pending)
    assert first.json() == second.json() and first.status_code == 202
    assert len(runtime.owner.calls) == 1


async def test_operation_start_same_key_one_operation(service):  # W28-3
    runtime, client = service
    assert (await start(client, key("k3"))).json() == (await start(client, key("k3"))).json()
    assert [call[0] for call in runtime.owner.calls] == ["start"]


async def test_same_key_other_parameters_or_route_conflicts_without_action(service):  # W28-4
    runtime, client = service
    await target_start(client, key("k4"), {"TOKEN": "s"})
    changed = await target_start(client, key("k4"), {"TOKEN": "other"})
    other_route = await check(client, key("k4"))
    for response in (changed, other_route):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "request_conflict"
    assert len(runtime.owner.calls) == 1


async def test_old_generation_refused_before_connection_lookup(service):  # W28-5
    runtime, client = service
    response = await check(client, key("k5", "gen-0"), connection="missing")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "request_unknown_generation"
    assert runtime.owner.calls == [] and len(runtime._mutations) == 0


@pytest.mark.parametrize("headers", [
    {"Lab-Request-Id": "only"}, {"Lab-Owner-Generation": "gen-1"},
    {"Lab-Request-Id": "bad id", "Lab-Owner-Generation": "gen-1"},
])
async def test_malformed_key_headers_are_invalid_request(service, headers):
    runtime, client = service
    response = await check(client, headers)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert runtime.owner.calls == []


@pytest.mark.parametrize("path", ["/v1/health", "/v1/operations", f"/v1/bundles/{DIGEST}",
                                  "/v1/tasks/task?owner_generation=gen-1"])
async def test_key_headers_on_reads_are_invalid_request(service, path):
    _, client = service
    response = await client.get(path, headers=key("k6"))
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


async def test_parse_and_connection_failures_are_not_remembered(service):
    runtime, client = service
    bad = await client.post("/v1/connections/connection/check", headers=key("k7"),
        json={"schema": "lab-service-check-request/1"})
    missing = await check(client, key("k7"), connection="absent")
    assert (bad.status_code, missing.status_code) == (400, 404)
    assert (await check(client, key("k7"))).status_code == 202
    assert len(runtime.owner.calls) == 1


async def test_not_found_after_dispatch_is_retried_other_failure_is_remembered(service):
    runtime, client = service
    runtime.owner.failure = OwnerError("not_found", "read")
    assert (await start(client, key("k8"))).status_code == 404
    runtime.owner.failure = None
    assert (await start(client, key("k8"))).status_code == 202
    runtime.owner.failure = OwnerError("busy", "before_start")
    assert (await check(client, key("k9"))).json()["error"]["code"] == "busy"
    runtime.owner.failure = None
    assert (await check(client, key("k9"))).json()["error"]["code"] == "busy"
    assert [call[0] for call in runtime.owner.calls] == ["start", "start", "check"]


async def test_stopping_admission_is_not_remembered(service):
    runtime, client = service
    runtime._admission_open = False
    assert (await check(client, key("k10"))).json()["error"]["code"] == "service_stopping"
    runtime._admission_open = True
    assert (await check(client, key("k10"))).status_code == 202


def test_resume_route_class_is_work():
    from evolution_lab.service.mutations import ROUTE_CLASSES
    assert ROUTE_CLASSES["resume"] == "work"


async def test_work_limit_does_not_block_lifecycle_or_unkeyed(service):
    from evolution_lab.service.mutations import MutationTable
    runtime, client = service
    runtime._mutations = MutationTable(work_limit=1, lifecycle_limit=1)
    assert (await check(client, key("w1"))).status_code == 202
    full = await check(client, key("w2"))
    assert full.status_code == 503 and full.json()["error"]["code"] == "request_table_full"
    stop = await client.post("/v1/tasks/task-1/stop", headers=key("l1"), json={})
    assert stop.status_code == 200
    assert (await check(client, {})).status_code == 202


async def test_unkeyed_mutations_keep_s5_behavior(service):
    runtime, client = service
    first, second = await check(client, {}), await check(client, {})
    assert first.json() != second.json() and len(runtime.owner.calls) == 2
    assert len(runtime._mutations) == 0


async def test_keyed_preparation_route_runs_once(service):
    runtime, client = service
    body = {"schema": "lab-service-bundle-request/1", "source": {"kind": "evaluation",
        "input_path": "/abs/i.json", "launch_path": "/abs/l.json"}, "environments": [],
        "env": {}, "secret_names": [], "roots": [], "path_bindings": []}
    one = await client.post("/v1/bundles", headers=key("b1"), json=body)
    two = await client.post("/v1/bundles", headers=key("b1"), json=body)
    assert one.json() == two.json() and len(runtime.preparation.calls) == 1


def raw_json(text, headers=None):
    return {"content": text.encode("ascii"), "headers": {"content-type": "application/json", **(headers or {})}}


LONE_SURROGATE = '{"schema": "lab-service-target-start-request/1", "secrets": {"TOKEN": "\\ud800"}}'
OTHER_SURROGATE = '{"schema": "lab-service-target-start-request/1", "secrets": {"TOKEN": "\\udc00"}}'


async def test_unkeyed_lone_surrogate_body_keeps_s5_behavior(service, monkeypatch):
    import evolution_lab.service.runtime as runtime_module
    runtime, client = service

    def no_fingerprint(*args, **kwargs):
        raise AssertionError("unkeyed request must not be fingerprinted")

    monkeypatch.setattr(runtime_module, "fingerprint", no_fingerprint)
    response = await client.post("/v1/connections/connection/target", **raw_json(LONE_SURROGATE))
    assert response.status_code == 202, response.text
    assert runtime.owner.calls == [("start_target", {"TOKEN": "\ud800"})]
    assert len(runtime._mutations) == 0


async def test_keyed_lone_surrogate_body_is_fingerprinted_deterministically(service):
    runtime, client = service
    path = "/v1/connections/connection/target"
    first = await client.post(path, **raw_json(LONE_SURROGATE, key("s1")))
    second = await client.post(path, **raw_json(LONE_SURROGATE, key("s1")))
    other = await client.post(path, **raw_json(OTHER_SURROGATE, key("s1")))
    assert first.status_code == second.status_code == 202, first.text
    assert first.json() == second.json()
    assert other.status_code == 409 and other.json()["error"]["code"] == "request_conflict"
    assert len(runtime.owner.calls) == 1


LIFECYCLE_ROUTES = {
    "stop_target": ("DELETE", "/v1/targets/target?owner_generation=gen-1", None, 202),
    "delete_results": ("DELETE", "/v1/operations/operation/results", None, 202),
    "cleanup": ("POST", "/v1/cleanup", {"schema": "lab-service-cleanup-request/1",
                "ref": {"kind": "operation", "id": "operation"}, "wait_seconds": None}, 202),
    "release": ("POST", "/v1/releases", {"schema": "lab-service-release-request/1",
                "ref": {"kind": "operation", "id": "operation"}, "reason": "manual"}, 200),
}


async def send(client, route, headers, *, url=None, body=None):
    method, default_url, default_body, _ = LIFECYCLE_ROUTES[route]
    body = default_body if body is None else body
    extra = {} if body is None else {"json": body}
    return await client.request(method, url or default_url, headers=headers, **extra)


@pytest.mark.parametrize("route", sorted(LIFECYCLE_ROUTES))
async def test_keyed_lifecycle_route_replay_runs_once(service, route):
    runtime, client = service
    one = await send(client, route, key(f"r-{route}"))
    two = await send(client, route, key(f"r-{route}"))
    assert one.status_code == two.status_code == LIFECYCLE_ROUTES[route][3], one.text
    assert one.json() == two.json()
    assert len(runtime.owner.calls) == 1


@pytest.mark.parametrize(("route", "changed"), [
    ("stop_target", {"url": "/v1/targets/target?owner_generation=gen-2"}),
    ("delete_results", {"url": "/v1/operations/other/results"}),
    ("cleanup", {"body": {**LIFECYCLE_ROUTES["cleanup"][2], "wait_seconds": 5}}),
    ("release", {"body": {**LIFECYCLE_ROUTES["release"][2], "reason": "other"}}),
])
async def test_keyed_lifecycle_route_changed_parameters_conflict(service, route, changed):
    runtime, client = service
    assert (await send(client, route, key(f"c-{route}"))).status_code == LIFECYCLE_ROUTES[route][3]
    response = await send(client, route, key(f"c-{route}"), **changed)
    assert response.status_code == 409 and response.json()["error"]["code"] == "request_conflict"
    assert len(runtime.owner.calls) == 1
