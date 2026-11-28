"""Task 9: probe session routes over a fake session (spec 2026-10-02 §5.1)."""
from __future__ import annotations

import httpx
import pytest

from evolution_lab.projects.errors import ProjectError
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

SID = "a" * 32
CEILING = {"turns": {"used": 0, "max": 3}, "tokens": None, "cost": None}
SESSION = {"schema": "lab-probe-session/1", "id": SID, "project": "acq", "implementation": "agent",
           "state": "open", "closed_reason": None, "reset_of": None, "created_at": "t", "closed_at": None,
           "target": {"id": "t1", "owner_generation": "gen-1", "boot_id": "b", "bundle_digest": "b" * 64,
                      "launch_digest": "l" * 64, "source": "entry", "seed_mode": "exact"},
           "seed": {"requested": None, "effective": None}, "ceiling": CEILING, "budget": None,
           "tokens_incomplete": False, "cost_unavailable": False, "turn_timeout_seconds": 120, "turns": []}
TURN = {"schema": "lab-probe-turn/1", "session_id": SID, "turn_id": SID + "/1", "started_at": "t", "finished_at": "t",
        "latency_ms": 5, "request": {"content": "hi"}, "answer": "yo", "steps": [], "outcome": "ok", "reason": None,
        "agent_outcome": "unspecified", "usage": None, "cost": None, "truncated": False, "failure": None,
        "ceiling": CEILING}
START = {"schema": "lab-service-project-probe-request/1", "implementation": "agent", "ceiling": {"max_turns": 3},
         "budget": None, "turn_timeout_seconds": None, "seed": None}
SAY = {"schema": "lab-service-probe-turn-request/1", "content": "hi"}


class FakeSession:
    def __init__(self):
        self.calls = []
        self.error = None

    def _go(self, *call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    async def probe_start(self, project_id, implementation_id, *, ceiling, budget, turn_timeout_seconds, seed):
        self._go("probe_start", project_id, implementation_id, ceiling, budget, turn_timeout_seconds, seed)
        return dict(SESSION)

    async def probe_turn(self, session_id, content):
        self._go("probe_turn", session_id, content)
        return dict(TURN)

    async def probe_session(self, session_id):
        self._go("probe_session", session_id)
        return dict(SESSION)

    async def probe_list(self, project_id, implementation_id=None):
        self._go("probe_list", project_id, implementation_id)
        return {"schema": "lab-probe-session-list/1", "sessions": []}

    async def probe_reset(self, session_id):
        self._go("probe_reset", session_id)
        return dict(SESSION)

    async def probe_close(self, session_id):
        self._go("probe_close", session_id)
        return dict(SESSION, state="closed")


@pytest.fixture
async def service(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2, home_directory=tmp_path / "home"))
    runtime.project_session = FakeSession()
    runtime._admission_open = True
    runtime.owner = type("O", (), {"owner_generation": "gen-1"})()
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        yield runtime, client


def key(request_id):
    return {"Lab-Request-Id": request_id, "Lab-Owner-Generation": "gen-1"}


async def test_start(service):
    runtime, client = service
    response = await client.post("/v1/projects/acq/probes", json=START)
    assert response.status_code == 201 and response.json() == SESSION
    assert runtime.project_session.calls == [("probe_start", "acq", "agent", {"max_turns": 3}, None, 120, None)]
    full = dict(START, ceiling={"max_turns": 2, "max_tokens": 9}, turn_timeout_seconds=30, seed=7)
    assert (await client.post("/v1/projects/acq/probes", json=full)).status_code == 201
    assert runtime.project_session.calls[-1] == ("probe_start", "acq", "agent", {"max_turns": 2, "max_tokens": 9},
                                                 None, 30, 7)


@pytest.mark.parametrize("body", [
    {"schema": "lab-service-project-probe-request/1", "implementation": "agent", "ceiling": {"max_turns": 1}},
    dict(START, extra=1), dict(START, ceiling=3), dict(START, budget="x"), dict(START, seed=True),
    dict(START, turn_timeout_seconds="5"), dict(START, implementation=None)])
async def test_start_unknown_or_bad_keys_are_invalid_request(service, body):
    runtime, client = service
    response = await client.post("/v1/projects/acq/probes", json=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert runtime.project_session.calls == []


async def test_start_schema_mismatch(service):
    _, client = service
    response = await client.post("/v1/projects/acq/probes", json=dict(START, schema="lab-service-project-probe-request/2"))
    assert response.json()["error"]["code"] == "unsupported_schema"


async def test_list_session_and_reads_refuse_a_mutation_key(service):
    runtime, client = service
    listed = await client.get("/v1/projects/acq/probes?implementation=agent")
    assert listed.status_code == 200 and listed.json()["schema"] == "lab-probe-session-list/1"
    assert (await client.get("/v1/projects/acq/probes")).status_code == 200
    got = await client.get(f"/v1/probes/{SID}")
    assert got.status_code == 200 and got.json() == SESSION
    assert runtime.project_session.calls == [("probe_list", "acq", "agent"), ("probe_list", "acq", None),
                                             ("probe_session", SID)]
    before = len(runtime.project_session.calls)
    for path in ("/v1/projects/acq/probes", f"/v1/probes/{SID}"):
        assert (await client.get(path, headers=key("r1"))).status_code == 400
    assert len(runtime.project_session.calls) == before
    assert (await client.get("/v1/projects/acq/probes?other=1")).status_code == 400
    assert (await client.get(f"/v1/probes/{SID}?x=1")).status_code == 400


async def test_turn_replays_by_key_without_a_second_turn(service):
    runtime, client = service
    first = await client.post(f"/v1/probes/{SID}/turns", json=SAY, headers=key("t1"))
    second = await client.post(f"/v1/probes/{SID}/turns", json=SAY, headers=key("t1"))
    assert first.status_code == second.status_code == 200 and first.json() == second.json() == TURN
    assert runtime.project_session.calls == [("probe_turn", SID, "hi")]
    changed = await client.post(f"/v1/probes/{SID}/turns", json=dict(SAY, content="other"), headers=key("t1"))
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "request_conflict"
    assert len(runtime.project_session.calls) == 1


@pytest.mark.parametrize("body", [
    dict(SAY, content=""), dict(SAY, content=5), dict(SAY, extra=1), {"schema": SAY["schema"]},
    dict(SAY, content="x" * ((1 << 20) + 1))])
async def test_turn_body_is_closed(service, body):
    runtime, client = service
    response = await client.post(f"/v1/probes/{SID}/turns", json=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert runtime.project_session.calls == []


async def test_turn_limit_is_in_bytes(service):
    runtime, client = service
    ok = await client.post(f"/v1/probes/{SID}/turns", json=dict(SAY, content="я" * (1 << 19)))  # exactly 1 MiB
    assert ok.status_code == 200
    over = await client.post(f"/v1/probes/{SID}/turns", json=dict(SAY, content="я" * ((1 << 19) + 1)))
    assert over.status_code == 400


async def test_turn_lone_surrogate_is_invalid_request(service):
    runtime, client = service
    raw = b'{"schema":"lab-service-probe-turn-request/1","content":"\\ud800"}'
    response = await client.post(f"/v1/probes/{SID}/turns", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400 and runtime.project_session.calls == []


async def test_turn_escaped_text_of_the_limit_passes_the_wire(service):
    runtime, client = service
    text = "я" * (1 << 19)  # 1 MiB of UTF-8, 3 MiB when every character is a \\uXXXX escape
    import json as _json
    raw = _json.dumps(dict(SAY, content=text)).encode()
    response = await client.post(f"/v1/probes/{SID}/turns", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 200 and runtime.project_session.calls == [("probe_turn", SID, text)]


async def test_reset_and_close(service):
    runtime, client = service
    assert (await client.post(f"/v1/probes/{SID}/reset", headers=key("r1"))).status_code == 201
    assert (await client.post(f"/v1/probes/{SID}/reset", json={})).status_code == 201
    assert (await client.post(f"/v1/probes/{SID}/reset", json={"a": 1})).status_code == 400
    closed = await client.delete(f"/v1/probes/{SID}", headers=key("c1"))
    assert closed.status_code == 200 and closed.json()["state"] == "closed"
    again = await client.delete(f"/v1/probes/{SID}", headers=key("c1"))
    assert again.json() == closed.json()
    assert [c[0] for c in runtime.project_session.calls] == ["probe_reset", "probe_reset", "probe_close"]


@pytest.mark.parametrize("code,status", [("probe_ceiling_reached", 409), ("probe_session_not_found", 404),
                                         ("probe_session_closed", 409), ("probe_limit_reached", 409),
                                         ("target_not_ready", 409), ("target_undeclared", 422)])
async def test_project_errors_map_to_statuses(service, code, status):
    runtime, client = service
    runtime.project_session.error = ProjectError(code)
    response = await client.post(f"/v1/probes/{SID}/turns", json=SAY)
    assert response.status_code == status and response.json()["error"]["code"] == code
    response = await client.get(f"/v1/probes/{SID}")
    assert response.status_code == status and response.json()["error"]["code"] == code


async def test_shutdown_closes_the_probe_store_before_the_home(tmp_path):
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2, home_directory=tmp_path / "home"))
    order = []

    class Session:
        def stop_probes(self):
            order.append("stop_probes")

        async def sync(self):
            order.append("sync")

        def abandon_probes(self):
            order.append("abandon_probes")

        def close(self):
            order.append("probes.close")

    class Projects:
        def close(self):
            order.append("projects.close")

    runtime.project_session, runtime.projects = Session(), Projects()
    await runtime._shutdown()
    assert order[0] == "stop_probes"  # queued probe calls are refused first (spec 2026-10-02 §5.1)
    assert order.index("probes.close") < order.index("projects.close")
    assert order.index("abandon_probes") < order.index("probes.close")  # codex r2 R2-2
