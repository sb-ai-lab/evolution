"""Task 9: project routes over a fake session, project mutation keys, the operation filter,
and the Lab home inside the service runtime (single writer, startup/shutdown sync)."""
from __future__ import annotations

import io
import json

import httpx
import pytest

from evolution_lab.owner import Owner, TaskRef
from evolution_lab.projects.errors import ProjectError
from evolution_lab.service.app import create_app
from evolution_lab.service.errors import ServiceError, error_response, translate_exception
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

PROJECT = {"schema": "lab-project/2", "id": "acq", "name": "Acq", "state": "ready", "failure": None,
           "created_at": "t", "archived": None, "record_version": 1,
           "working_copy": {"path": "/w", "created_by_lab": False, "git": False, "source": None},
           "materials_root": "lab", "exclude": [], "implementations": []}
DIGEST = "a" * 64
TARGET = {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "gen-1"}


class Public:
    def __init__(self, schema, **fields):
        self.schema, self.fields = schema, fields

    @property
    def id(self):
        return self.fields.get("id")

    def to_payload(self):
        return {"schema": self.schema, **self.fields}


class FakeSession:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return dict(PROJECT)

    async def list_projects(self, *, archived):
        self.calls.append(("list_projects", archived))
        return {"schema": "lab-project-list/2", "projects": [dict(PROJECT)]}

    async def get_project(self, project_id):
        if project_id != "acq":
            raise ProjectError("project_not_found")
        return dict(PROJECT)

    async def update(self, project_id, mutation, *, expected_record_version=None):
        raise ProjectError("active_operation")

    async def bundle(self, project_id, implementation_id=None):
        self.calls.append(("bundle", project_id, implementation_id))
        return Public("lab-bundle-summary/3", digest=DIGEST), Public("lab-project-link/1", kind="bundle")

    async def prepare(self, project_id, implementation_id, *, bundle_digest, kind, params):
        self.calls.append(("prepare", project_id, implementation_id, bundle_digest, kind, dict(params)))
        return Public("lab-prepared-summary/1", digest=DIGEST), Public("lab-project-link/1", kind="prepared")

    async def start_target(self, project_id, implementation_id, *, bundle_digest, secrets):
        self.calls.append(("start_target", project_id, implementation_id, bundle_digest, dict(secrets)))
        return TaskRef("task-1", "gen-1"), None

    async def start(self, project_id, implementation_id, *, prepared_digest, target_ref, secrets):
        self.calls.append(("start", project_id, implementation_id, prepared_digest, target_ref.id, dict(secrets)))
        return Public("lab-operation-status/8", id="op-1"), Public("lab-project-link/1", kind="operation")

    async def operations(self, project_id, implementation_id=None):
        self.calls.append(("operations", project_id, implementation_id))
        return (Public("lab-project-link/1", kind="operation"),)

    async def operations_summary(self, project_id, implementation_id=None):
        self.calls.append(("operations_summary", project_id, implementation_id))
        return {"schema": "lab-project-operation-list/2", "project": project_id, "operations": []}

    async def operation_ids(self, project_id, implementation_id=None):
        self.calls.append(("operation_ids", project_id, implementation_id))
        return ("op-2",)

    async def facts_payload(self, project_id, implementation_id=None):
        self.calls.append(("facts_payload", project_id, implementation_id))
        return {"schema": "lab-project-facts/5", "project": project_id}

    async def status_payload(self, project_id, implementation_id=None, scenario=None):
        self.calls.append(("status_payload", project_id, implementation_id, scenario))
        return {"schema": "lab-project-status/6", "project": project_id}

    async def confirm(self, project_id, implementation_id, *, claim, attests, linked, channel, author, note):
        self.calls.append(("confirm", project_id, implementation_id, claim, attests, linked, channel, author, note))
        return Public("lab-project-confirmation/1", id="c-1")

    async def confirmations(self, project_id, *, revoked=False):
        self.calls.append(("confirmations", project_id, revoked))
        return (Public("lab-project-confirmation/1", id="c-1"),)

    async def revoke_confirmation(self, project_id, confirmation_id, reason):
        self.calls.append(("revoke_confirmation", project_id, confirmation_id, reason))
        return Public("lab-project-confirmation/1", id=confirmation_id)

    async def accept(self, project_id, implementation_id, *, only, across_pairs, reason, channel, author, dry_run, expected=None):
        self.calls.append(("accept", project_id, implementation_id, only, across_pairs, reason, channel, author, dry_run, expected))
        return Public("lab-project-acceptance/1", id=None)

    async def revoke_acceptance(self, project_id, acceptance_id, reason):
        self.calls.append(("revoke_acceptance", project_id, acceptance_id, reason))
        return Public("lab-project-acceptance/1", id=acceptance_id)

    async def open_workspace(self, project_id, *, autonomy="ask"):
        self.calls.append(("open_workspace", project_id, autonomy))
        return {"schema": "lab-project-workspace/1", "path": "/h/workspaces/" + project_id}

    async def demo(self, project_id):
        self.calls.append(("demo", project_id))
        return dict(PROJECT)

    async def port(self, project_id, *, source, template, ref=None, port_id=None, into=None):
        self.calls.append(("port", project_id, source, template, ref, port_id, into))
        if source == "gone":
            raise ProjectError("port_destination_exists", "/h/ports/acq/gone-port")
        return dict(PROJECT)

    async def sync_check(self, project_id, *, rev=None):
        self.calls.append(("sync_check", project_id, rev))
        return {"schema": "lab-project-sync-check/1", "project": project_id}

    async def sync_apply(self, project_id, *, upstream):
        self.calls.append(("sync_apply", project_id, upstream))
        if upstream == "c" * 40:
            raise ProjectError("merge_conflict", "a.py")
        if upstream == "d" * 40:
            raise ProjectError("sync_not_recorded")
        return {"schema": "lab-project-sync-result/1", "mode": "merge"}


class FakeOwner:
    owner_generation = "gen-1"

    async def list_operations(self):
        return (Public("lab-operation-status/8", id="op-1"), Public("lab-operation-status/8", id="op-2"))

    async def list_summaries(self, ids=None):
        return tuple({"id": name} for name in ("op-1", "op-2") if ids is None or name in ids)


@pytest.fixture
def tmp_path(tmp_path_factory):
    """Keep socket paths within UNIX limits under the dev56 worktree basetemp."""
    return tmp_path_factory.mktemp("p")


@pytest.fixture
async def service(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2, home_directory=tmp_path / "home"))
    runtime.project_session = FakeSession()
    runtime.owner = FakeOwner()
    runtime._admission_open = True
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        yield runtime, client


def key(request_id, generation="gen-1"):
    return {"Lab-Request-Id": request_id, "Lab-Owner-Generation": generation}


CREATE = {"schema": "lab-service-project-create/1", "id": "acq", "name": "Acq", "materials_root": "lab",
          "implementations": [], "source": {"kind": "path", "path": "/w"}, "exclude": []}


async def test_create_and_read(service):
    runtime, client = service
    created = await client.post("/v1/projects", json=CREATE)
    assert created.status_code == 200 and created.json()["schema"] == "lab-project/2"
    assert runtime.project_session.calls[0] == ("create", {
        "id": "acq", "name": "Acq", "materials_root": "lab", "implementations": [], "exclude": [],
        "path": "/w", "clone": None})
    listed = await client.get("/v1/projects")
    assert listed.json()["schema"] == "lab-project-list/2"
    assert listed.json()["projects"] == [PROJECT]
    missing = await client.get("/v1/projects/zzz")
    assert missing.status_code == 404 and missing.json()["error"]["stage"] == "project"
    assert missing.json()["error"]["code"] == "project_not_found"


async def test_update_refusal_maps_to_409(service):
    runtime, client = service
    body = {"schema": "lab-service-project-update/1", "mutation": {"op": "archive", "reason": None}, "expected_record_version": None}
    refused = await client.patch("/v1/projects/acq", json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "active_operation"


async def test_closed_request_shapes(service):
    runtime, client = service
    bad = await client.post("/v1/projects", json={"schema": "lab-service-project-create/1"})
    assert bad.status_code == 400
    clone = dict(CREATE, source={"kind": "clone", "repo": "/r", "rev": None, "extra": 1})
    assert (await client.post("/v1/projects", json=clone)).status_code == 400
    other = dict(CREATE, schema="lab-service-project-create/2")
    assert (await client.post("/v1/projects", json=other)).json()["error"]["code"] == "unsupported_schema"
    assert (await client.get("/v1/projects?archived=0")).status_code == 400
    assert (await client.get("/v1/projects/acq?archived=1")).status_code == 400
    assert (await client.request("GET", "/v1/projects", content=b"{}")).status_code == 400
    accept = {"schema": "lab-service-accept-request/1", "implementation": None, "only": None,
              "across_pairs": "no", "reason": "r", "channel": "cli", "author": "t", "dry_run": True}
    assert (await client.post("/v1/projects/acq/acceptances", json=accept)).status_code == 400
    assert runtime.project_session.calls == []


async def test_operations_query_parameters(service):
    runtime, client = service
    refused = await client.get("/v1/operations?implementation=x")  # implementation needs project
    assert refused.status_code == 400
    for query in ("project=acq&project=acq", "other=1", "project="):
        assert (await client.get(f"/v1/operations?{query}")).status_code == 400
    everything = await client.get("/v1/operations")
    assert [op["id"] for op in everything.json()["operations"]] == ["op-1", "op-2"]
    filtered = await client.get("/v1/operations?project=acq&implementation=agent")
    assert filtered.status_code == 200 and filtered.json()["schema"] == "lab-operation-list/8"
    assert [op["id"] for op in filtered.json()["operations"]] == ["op-2"]
    assert runtime.project_session.calls == [("operation_ids", "acq", "agent")]


async def test_summary_view_takes_the_project_filters(service):
    runtime, client = service
    everything = await client.get("/v1/operations?view=summary")
    assert everything.json()["schema"] == "lab-operation-summary-list/1"
    assert [op["id"] for op in everything.json()["operations"]] == ["op-1", "op-2"]
    filtered = await client.get("/v1/operations?view=summary&project=acq&implementation=agent")
    assert [op["id"] for op in filtered.json()["operations"]] == ["op-2"]
    assert runtime.project_session.calls == [("operation_ids", "acq", "agent")]
    for query in ("view=other", "view=", "view=summary&view=summary", "view=summary&implementation=x"):
        assert (await client.get(f"/v1/operations?{query}")).status_code == 400


async def test_keyed_create_replays_without_a_second_create(service):
    runtime, client = service
    first = await client.post("/v1/projects", json=CREATE, headers=key("p1"))
    second = await client.post("/v1/projects", json=CREATE, headers=key("p1"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert [call[0] for call in runtime.project_session.calls] == ["create"]
    changed = await client.post("/v1/projects", json=dict(CREATE, name="Other"), headers=key("p1"))
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "request_conflict"


async def test_demo_route_before_project_routes(service):
    runtime, client = service
    runtime.service_environment = object()
    body = {"schema": "lab-service-project-demo-request/1", "id": None}
    created = await client.post("/v1/projects/demo", json=body, headers=key("d1"))
    assert created.status_code == 200 and created.json()["schema"] == "lab-project/2"
    assert runtime.project_session.calls == [("demo", None)]
    named = await client.post("/v1/projects/demo", json=dict(body, id="acq"))
    assert named.status_code == 200 and runtime.project_session.calls[-1] == ("demo", "acq")
    for bad in ({"schema": "lab-service-project-demo-request/1"}, dict(body, extra=1), dict(body, id=5)):
        assert (await client.post("/v1/projects/demo", json=bad)).status_code == 400


async def test_demo_route_without_service_environment_is_refused(service):
    runtime, client = service
    assert runtime.service_environment is None
    refused = await client.post("/v1/projects/demo", json={"schema": "lab-service-project-demo-request/1", "id": None})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "service_environment_unconfigured"
    assert runtime.project_session.calls == []


async def test_open_workspace_route_is_a_keyed_mutation(service):
    runtime, client = service
    opened = await client.post("/v1/projects/acq/workspace", headers=key("w1"))
    assert opened.status_code == 200 and opened.json()["schema"] == "lab-project-workspace/1"
    again = await client.post("/v1/projects/acq/workspace", headers=key("w1"))
    assert again.json() == opened.json()
    assert runtime.project_session.calls == [("open_workspace", "acq", "ask")]
    assert (await client.post("/v1/projects/acq/workspace", content=b"{}")).status_code == 400


async def test_workspace_request_binds_autonomy_and_preserves_the_response(service):
    runtime, client = service
    path = "/v1/projects/acq/workspace"
    request = {"schema": "lab-service-project-workspace-request/1", "autonomy": "yolo"}
    answer = await client.post(path, headers=key("workspace"), json=request)
    assert answer.status_code == 200
    assert answer.json() == {"schema": "lab-project-workspace/1", "path": "/h/workspaces/acq"}
    replay = await client.post(path, headers=key("workspace"), json=request)
    assert replay.json() == answer.json()
    conflict = await client.post(path, headers=key("workspace"), json={**request, "autonomy": "ask"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "request_conflict"
    for invalid in ({}, {"autonomy": "yolo"}, {**request, "extra": True}, {**request, "autonomy": []},
                    {**request, "autonomy": "always"}, {**request, "schema": "lab-service-project-workspace-request/2"}):
        assert (await client.post(path, json=invalid)).status_code == 400
    query = await client.post(path, params={"autonomy": "ask"}, json=request)
    assert query.status_code == 400
    assert runtime.project_session.calls == [("open_workspace", "acq", "yolo")]



PROJECT_PREPARE = ("lab-service-project-prepare-request/1", "lab-service-project-prepare-request/2")


async def test_project_start_v5_contains_strictly_decodable_status_v8(service, tmp_path):
    from types import SimpleNamespace
    from evolution_lab.cli.responses import validate
    from test_owner_auto_resume_wire import operation_payload

    runtime, client = service
    payload = operation_payload(tmp_path)
    async def start(*_, **__):
        return SimpleNamespace(id=payload['id'], to_payload=lambda: payload), None
    runtime.project_session.start = start
    response = await client.post('/v1/projects/acq/operations', json={
        'schema': 'lab-service-project-start-request/1', 'implementation': None,
        'prepared_digest': DIGEST, 'target_ref': TARGET, 'secrets': {}})
    assert response.status_code == 202
    assert response.json()['schema'] == 'lab-project-start-result/5'
    assert validate(response.json(), 'lab-project-start-result/5') == response.json()


@pytest.mark.parametrize("prepare_schema", PROJECT_PREPARE)
async def test_project_routes_reach_the_session_with_decoded_arguments(service, prepare_schema):
    runtime, client = service
    calls = runtime.project_session.calls
    bundle = await client.post("/v1/projects/acq/bundles", headers=key("b1"), json={
        "schema": "lab-service-project-bundle-request/1", "implementation": None})
    assert bundle.json() == {"schema": "lab-project-bundle-result/1",
                             "bundle": {"schema": "lab-bundle-summary/3", "digest": DIGEST},
                             "link": {"schema": "lab-project-link/1", "kind": "bundle"}}
    prepared = await client.post("/v1/projects/acq/prepared-executions", json={
        "schema": prepare_schema, "implementation": "agent", "kind": "campaign",
        "bundle_digest": DIGEST, "stop_grace_seconds": None, "cleanup_wait_seconds": 5})
    assert prepared.json()["schema"] == "lab-project-prepared-result/1"
    target = await client.post("/v1/projects/acq/targets", json={
        "schema": "lab-service-project-target-request/1", "implementation": None, "bundle_digest": DIGEST,
        "secrets": {"TOKEN": "s"}})
    assert target.status_code == 202
    assert target.json() == {"schema": "lab-project-target-result/1",
                             "task": {"schema": "lab-task-ref/1", "id": "task-1", "owner_generation": "gen-1"},
                             "link": None}
    assert runtime._stoppable_refs["task-1"] == TaskRef("task-1", "gen-1")
    started = await client.post("/v1/projects/acq/operations", json={
        "schema": "lab-service-project-start-request/1", "implementation": None, "prepared_digest": DIGEST,
        "target_ref": TARGET, "secrets": {}})
    assert started.status_code == 202 and started.json()["schema"] == "lab-project-start-result/5"
    assert started.json()["operation"]["id"] == "op-1"
    links = await client.get("/v1/projects/acq/operations?implementation=agent")
    assert links.json()["schema"] == "lab-project-operation-list/1" and len(links.json()["links"]) == 1
    facts = await client.get("/v1/projects/acq/facts")
    assert facts.json() == {"schema": "lab-project-facts/5", "project": "acq"}
    confirmed = await client.post("/v1/projects/acq/confirmations", json={
        "schema": "lab-service-confirm-request/1", "implementation": None, "claim": "spec.confirmed",
        "attests": {"path": "SPEC.md"}, "linked": [], "channel": "cli", "author": "t", "note": None})
    assert confirmed.json()["schema"] == "lab-project-confirmation/1"
    listed = await client.get("/v1/projects/acq/confirmations?revoked=1")
    assert listed.json()["schema"] == "lab-project-confirmation-list/1"
    revoke = {"schema": "lab-service-revoke-request/1", "reason": "wrong"}
    assert (await client.post("/v1/projects/acq/confirmations/c-1/revoke", json=revoke)).json()["id"] == "c-1"
    accepted = await client.post("/v1/projects/acq/acceptances", json={
        "schema": "lab-service-accept-request/1", "implementation": "agent", "only": ["connected"],
        "across_pairs": False, "reason": "r", "channel": "cli", "author": "t", "dry_run": True})
    assert accepted.json()["schema"] == "lab-project-acceptance/1"
    assert (await client.post("/v1/projects/acq/acceptances/a-1/revoke", json=revoke)).json()["id"] == "a-1"
    archived = await client.get("/v1/projects?archived=1")
    assert archived.status_code == 200
    assert calls == [
        ("bundle", "acq", None),
        ("prepare", "acq", "agent", DIGEST, "campaign", {"stop_grace_seconds": None, "cleanup_wait_seconds": 5}),
        ("start_target", "acq", None, DIGEST, {"TOKEN": "s"}),
        ("start", "acq", None, DIGEST, "target", {}),
        ("operations", "acq", "agent"),
        ("facts_payload", "acq", None),
        ("confirm", "acq", None, "spec.confirmed", {"path": "SPEC.md"}, [], "cli", "t", None),
        ("confirmations", "acq", True),
        ("revoke_confirmation", "acq", "c-1", "wrong"),
        ("accept", "acq", "agent", ["connected"], False, "r", "cli", "t", True, None),
        ("revoke_acceptance", "acq", "a-1", "wrong"),
        ("list_projects", True),
    ]


async def test_project_revokes_share_a_class_but_not_a_fingerprint(service):
    runtime, client = service
    revoke = {"schema": "lab-service-revoke-request/1", "reason": "wrong"}
    first = await client.post("/v1/projects/acq/confirmations/c-1/revoke", json=revoke, headers=key("r1"))
    assert first.status_code == 200
    other = await client.post("/v1/projects/acq/acceptances/c-1/revoke", json=revoke, headers=key("r1"))
    assert other.status_code == 409 and other.json()["error"]["code"] == "request_conflict"
    assert [call[0] for call in runtime.project_session.calls] == ["revoke_confirmation"]


async def test_sync_routes_decode_and_call_the_session(service):
    runtime, client = service
    r = await client.post("/v1/projects/acq/sync/check", headers=key("s1"),
                          json={"schema": "lab-service-project-sync-check-request/1", "rev": "develop"})
    assert r.status_code == 200 and r.json()["schema"] == "lab-project-sync-check/1"
    r = await client.post("/v1/projects/acq/sync", headers=key("s2"),
                          json={"schema": "lab-service-project-sync-request/1", "upstream": "a" * 40})
    assert r.status_code == 200 and r.json()["mode"] == "merge"
    assert ("sync_check", "acq", "develop") in runtime.project_session.calls
    assert ("sync_apply", "acq", "a" * 40) in runtime.project_session.calls


async def test_sync_request_bodies_are_closed(service):
    _, client = service
    r = await client.post("/v1/projects/acq/sync", headers=key("s3"),
                          json={"schema": "lab-service-project-sync-request/1", "upstream": "a" * 40, "extra": 1})
    assert r.status_code == 400


async def test_sync_errors_keep_status_stage_and_path(service):
    _, client = service
    body = {"schema": "lab-service-project-sync-request/1", "upstream": "c" * 40}
    r = await client.post("/v1/projects/acq/sync", headers=key("s4"), json=body)
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "merge_conflict" and error["stage"] == "project" and "a.py" in error["message"]
    body = {"schema": "lab-service-project-sync-request/1", "upstream": "d" * 40}
    r = await client.post("/v1/projects/acq/sync", headers=key("s5"), json=body)
    assert r.status_code == 500 and r.json()["error"]["code"] == "sync_not_recorded"


def test_project_mutation_class_has_its_own_limit():
    from evolution_lab.service.mutations import ROUTE_CLASSES, MutationTable
    names = {"project_create", "project_update", "project_bundle", "project_prepare", "project_target",
             "project_start", "project_confirm", "project_revoke", "project_accept", "project_workspace",
             "project_demo", "project_port", "project_sync_check", "project_sync"}
    assert {name for name, kind in ROUTE_CLASSES.items() if kind == "project"} == names
    table = MutationTable(work_limit=1, lifecycle_limit=1, project_limit=1)
    table.ensure_capacity("project_create")
    table._counts["project"] = 1
    with pytest.raises(ServiceError) as caught:
        table.ensure_capacity("project_update")
    assert caught.value.code == "request_table_full"
    table.ensure_capacity("check")  # work is independent


def test_project_errors_keep_their_stage_and_status():
    error = ProjectError("home_busy")
    assert translate_exception(error) is error
    response = error_response(error)
    assert response["status"] == 409
    assert response["body"]["error"] == {"code": "home_busy", "stage": "project",
                                         "message": "Another Lab process writes this Lab home, or this handle cannot write it."}
    expected = {"project_not_found": 404, "implementation_not_found": 404, "invalid_id": 400,
                "working_copy_unavailable": 422, "home_invalid": 500, "registry_newer": 409,
                "not_linked": 409, "invalid_request": 400, "inputs_changed": 409}
    for code, status in expected.items():
        assert error_response(ProjectError(code))["status"] == status


# --- runtime: the Lab home is opened by the service (real Owner, tmp home) ---------------

def config(tmp_path):
    tmp_path.chmod(0o700)
    return ServiceConfig(tmp_path / "service.sock", tmp_path / "state", 0.2, home_directory=tmp_path / "home")


async def test_runtime_holds_the_home_and_syncs_at_start_and_before_close(tmp_path, monkeypatch):
    from importlib.metadata import version

    from evolution_lab.projects.registry import Projects
    from evolution_lab.projects.session import ProjectSession
    order = []
    real_sync, real_close = ProjectSession.sync, Owner.close

    async def sync(self, project_id=None):
        order.append("sync")
        return await real_sync(self, project_id)

    async def close(self, **kwargs):
        if kwargs:
            order.append("close")
        return await real_close(self, **kwargs)

    monkeypatch.setattr(ProjectSession, "sync", sync)
    monkeypatch.setattr(Owner, "close", close)
    cfg = config(tmp_path)
    runtime = ServiceRuntime(cfg)
    assert await runtime.start()
    try:
        assert isinstance(runtime.project_session, ProjectSession)
        assert runtime.project_session.pair.to_payload() == {"lab": version("evolution-lab"), "core": version("evo-skills")}
        assert order == ["sync"]
        with pytest.raises(ProjectError) as caught:
            Projects.open(cfg.home_directory)
        assert caught.value.code == "home_busy"
        assert await runtime.call_projects("list_projects", archived=False) == {"schema": "lab-project-list/2", "projects": []}
    finally:
        result = await runtime.shutdown()
    assert result.cleanup.complete is True
    assert order == ["sync", "sync", "close"]
    Projects.open(cfg.home_directory).close()  # released


async def test_home_busy_at_startup_exits_like_owner_busy(tmp_path):
    from evolution_lab.projects.registry import Projects
    from evolution_lab.service.server import serve
    cfg = config(tmp_path)
    held = Projects.open(cfg.home_directory)
    try:
        output = io.StringIO()
        assert await serve(cfg, stderr=output) == 1
    finally:
        held.close()
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert lines == [{"error": {"code": "home_busy", "stage": "project",
                                "message": "Another Lab process writes this Lab home, or this handle cannot write it."}}]
    assert not cfg.state_directory.exists()
    assert not cfg.socket_path.exists()


async def test_owner_busy_at_startup_releases_the_home(tmp_path):
    from evolution_lab.projects.registry import Projects
    from evolution_lab.service.server import serve
    cfg = config(tmp_path)
    cfg.state_directory.mkdir(mode=0o700)
    async with Owner.open(cfg.state_directory):
        output = io.StringIO()
        assert await serve(cfg, stderr=output) == 1
        assert json.loads(output.getvalue())["error"]["code"] == "busy"
    Projects.open(cfg.home_directory).close()


async def test_runtime_needs_an_explicit_home(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "service.sock", tmp_path / "state", 0.2))
    with pytest.raises(ServiceError) as caught:
        await runtime.start()
    assert caught.value.code == "invalid_argument"
    await runtime.shutdown()
    assert not (tmp_path / "state").exists()
    with pytest.raises(ServiceError):
        ServiceConfig(tmp_path / "service.sock", tmp_path / "state", home_directory="relative/home")


def test_run_service_defaults_the_home_from_the_environment(tmp_path, monkeypatch):
    from evolution_lab.service import server
    seen = []

    async def serve(config, *, stderr=None, on_ready=None):
        seen.append(config.home_directory)
        return 0

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setenv("LAB_HOME", str(tmp_path / "lab-home"))
    assert server.run_service(socket_path=tmp_path / "s.sock", state_directory=tmp_path / "state") == 0
    assert server.run_service(socket_path=tmp_path / "s.sock", state_directory=tmp_path / "state",
                              home_directory=tmp_path / "explicit") == 0
    assert seen == [tmp_path / "lab-home", tmp_path / "explicit"]


# --- Final-review fix wave: I9 (shutdown order for project calls and operations) -------------

async def test_a_slow_project_call_does_not_delay_stopping_the_operation(tmp_path, monkeypatch):
    """I9(a): only start/start_target drain before the stop requests; a slow project call
    (bundle, clone, facts, ...) drains after them, like general preparation calls."""
    import asyncio
    from dataclasses import replace

    from evolution_lab.owner import ResourceRef
    from evolution_lab.projects.session import ProjectSession
    entered, release, stops = asyncio.Event(), asyncio.Event(), []
    real_health = Owner.health

    async def bundle(self, project_id, implementation_id=None):
        entered.set()
        await release.wait()
        raise ProjectError("package_unavailable")

    async def health(self):
        return replace(await real_health(self), active_operation_id="operation")

    async def stop(self, ref):
        stops.append(ref)

    monkeypatch.setattr(ProjectSession, "bundle", bundle)
    monkeypatch.setattr(Owner, "health", health)
    monkeypatch.setattr(Owner, "stop", stop)
    runtime = ServiceRuntime(config(tmp_path))
    assert await runtime.start()
    call = asyncio.create_task(runtime.call_projects("bundle", "acq", None))
    await entered.wait()
    shutdown = runtime.request_shutdown()
    try:
        async with asyncio.timeout(5):
            while not stops:
                await asyncio.sleep(0.01)
        assert not call.done() and not shutdown.done()  # the stop went out while bundle still runs
    finally:
        release.set()
        await shutdown
    assert stops == [ResourceRef("operation", "operation")]
    with pytest.raises(ProjectError):
        await call


class _SettlingOwner:
    """One project operation that is ``stopping`` for a moment after Stop, then ``finished``."""
    owner_generation = "gen-1"

    def __init__(self, ref):
        from test_projects_session import FakeStatus
        self.ref, self.value, self.settled = ref, FakeStatus("op1", control_state="running"), None

    async def client_operations(self):
        return (("op1", self.ref),)

    async def client_targets(self):
        return ()

    async def client_tasks(self):
        return ()

    async def status(self, ref):
        return self.value

    async def health(self):
        from types import SimpleNamespace
        active = self.value.control_state in {"reserved", "running", "stopping"}
        return SimpleNamespace(active_operation_id="op1" if active else None)

    async def stop(self, ref):
        import asyncio
        self.value.control_state = "stopping"

        async def settle():
            await asyncio.sleep(0.05)
            self.value.control_state = "finished"
        self.settled = asyncio.get_running_loop().create_task(settle())

    async def close(self, *, wait_seconds):
        return "shutdown-result"


class _NoContext:
    async def __aexit__(self, *exc):
        return None


async def test_shutdown_records_the_final_state_of_the_operation_it_stops(tmp_path):
    """I9(b): the shutdown sync runs after the stopped operation settled, so its link keeps
    ``finished`` (not ``stopping``) — the last projection of this pair before a repin."""
    from evolution_lab.projects.models import Link, Pair
    from evolution_lab.projects.registry import Projects, utc_now
    from evolution_lab.projects.session import ProjectSession, client_ref
    cfg = config(tmp_path)
    (tmp_path / "wc").mkdir()
    pair, state_root = Pair("l", "c"), str(cfg.state_directory)
    projects = Projects.open(cfg.home_directory)
    projects.create(id="acq", name="Acq", materials_root=".", implementations=[{"id": "agent", "place": "."}],
                    path=str(tmp_path / "wc"))
    projects.store.insert_link(Link("operation", "acq", "agent", pair, state_root, utc_now(),
                                    {"inputs": {"digest": "d", "sources": []}}, "b" * 64, "p" * 64, "op1",
                                    "evaluation", {"at": utc_now(), "control_state": "running"}))
    owner = _SettlingOwner(client_ref("acq", "agent"))
    runtime = ServiceRuntime(cfg)
    runtime.projects, runtime.owner, runtime._owner_context = projects, owner, _NoContext()
    runtime.project_session = ProjectSession(projects, preparation=None, owner=owner, pair=pair,
                                             state_root=cfg.state_directory)
    runtime._admission_open = True
    assert await runtime.shutdown() == "shutdown-result"
    with Projects.open(cfg.home_directory, read_only=True) as reader:
        link = reader.store.link_for("operation", "op1", pair, state_root)
    assert link.last_seen["control_state"] == "finished"


async def test_a_failing_shutdown_sync_is_diagnosed_not_raised(tmp_path, monkeypatch):
    """I9(c): shutdown never fails on the projection refresh, but the failure is reported."""
    from evolution_lab.projects.session import ProjectSession
    real_sync, failing = ProjectSession.sync, {"on": False}

    async def sync(self, project_id=None):
        if failing["on"]:
            raise RuntimeError("private registry detail")
        return await real_sync(self, project_id)

    monkeypatch.setattr(ProjectSession, "sync", sync)
    runtime = ServiceRuntime(config(tmp_path))
    assert await runtime.start()
    failing["on"] = True
    result = await runtime.shutdown()
    assert result.cleanup.complete is True
    assert [item["error"]["code"] for item in runtime.diagnostics] == ["lab_error"]
    assert "private registry detail" not in json.dumps(runtime.diagnostics)


# --- Codex branch review round 1: spec §3.1 state root vs the home -----------------------

def _tree(root):
    return sorted((str(path.relative_to(root)), path.stat().st_mode) for path in root.rglob("*"))


@pytest.mark.parametrize("inside", ["clones/x/state", "workspaces/y", "clones", ""])
async def test_a_state_root_overlapping_home_clones_or_workspaces_is_refused(tmp_path, inside):
    """Spec §3.1: the pair state root may live in the home but must not be, sit inside, or
    contain ``H/clones`` or ``H/workspaces``; refused before any state is created."""
    from evolution_lab.projects.registry import Projects
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    Projects.open(home).close()
    (home / "clones" / "x").mkdir(mode=0o700)
    before = _tree(home)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "service.sock", home / inside, 0.2, home_directory=home))
    try:
        with pytest.raises(ServiceError) as caught:
            await runtime.start()
    finally:
        await runtime.shutdown()
    assert caught.value.code == "invalid_argument"
    assert _tree(home) == before
    assert not (tmp_path / "service.sock").exists()


async def test_a_state_root_elsewhere_in_the_home_is_accepted(tmp_path):
    """Spec §3.1: ``--state H/pairs/dev28`` is a valid place for the pair state root."""
    from evolution_lab.projects.registry import Projects
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    Projects.open(home).close()
    (home / "pairs").mkdir(mode=0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "service.sock", home / "pairs" / "dev28", 0.2,
                                           home_directory=home))
    try:
        assert await runtime.start()
        assert (home / "pairs" / "dev28").is_dir()
    finally:
        result = await runtime.shutdown()
    assert result.cleanup.complete is True


async def test_a_state_root_under_home_tmp_survives_the_port_leftover_sweep(tmp_path):
    """Codex r2 (PDR-0018): ``--state H/tmp/port-old`` is accepted by validate_paths; the service passes it to
    the sweep as protected, so even a (forged) port-leftover mark next to it never removes the Owner state."""
    from evolution_lab.projects import port
    from evolution_lab.projects.registry import Projects
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    Projects.open(home).close()
    (home / "tmp").mkdir(mode=0o700)
    state = home / "tmp" / "port-old"
    cfg = ServiceConfig(tmp_path / "service.sock", state, 0.2, home_directory=home)
    runtime = ServiceRuntime(cfg)
    try:
        assert await runtime.start()
    finally:
        assert (await runtime.shutdown()).cleanup.complete is True
    before = {path: path.stat().st_ino for path in state.rglob("*") if path.is_file()}
    assert before
    port.mark_leftover(state)
    runtime = ServiceRuntime(cfg)
    try:
        assert await runtime.start()
        assert {path: path.stat().st_ino for path in before if path.exists()} == before
    finally:
        assert (await runtime.shutdown()).cleanup.complete is True


# --- Codex fix wave follow-up: the service's stderr stays JSON lines ------------------------

_FAILING_CLONE_SERVICE = r'''
import asyncio, sys
from evolution_lab.service.runtime import ServiceConfig
from evolution_lab.service.server import serve

async def main():
    def factory(runtime):
        async def scenario():
            try:
                await runtime.call_projects("create", id="acq", name="Acq", materials_root="lab",
                                            implementations=[{"id": "agent", "place": "agent"}],
                                            clone={"repo": sys.argv[4], "rev": None})
            except Exception as error:
                print("RAISED", getattr(error, "code", type(error).__name__), flush=True)
            finally:
                runtime.request_shutdown()
        asyncio.get_running_loop().create_task(scenario())
        async def app(scope, receive, send):
            raise RuntimeError("no requests expected")
        return app
    config = ServiceConfig(sys.argv[1], sys.argv[2], 0.2, home_directory=sys.argv[3])
    return await serve(config, app_factory=factory)
raise SystemExit(asyncio.run(main()))
'''


def test_a_failing_clone_in_a_running_service_is_one_json_diagnostic_line(tmp_path):
    """Follow-up 1: ``lab serve`` writes JSON lines only. The clone's git stderr tail (redacted)
    reaches the service's stderr as one diagnostic in the usual ``{"error": …}`` shape, never as
    plain text from Python's default logging."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import evolution_lab
    tmp_path.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
    env["PYTHONPATH"] = str(Path(evolution_lab.__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, "-c", _FAILING_CLONE_SERVICE, str(tmp_path / "s.sock"),
                             str(tmp_path / "state"), str(tmp_path / "home"),
                             "https://lab:s3cret@127.0.0.1:1/team.git"],
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.stdout.splitlines() == ["RAISED clone_failed"], (result.stdout, result.stderr)
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stderr.splitlines()]  # every line is JSON
    diagnostics = [line["error"] for line in lines if "error" in line]
    assert len(diagnostics) == 1, lines
    assert (diagnostics[0]["code"], diagnostics[0]["stage"]) == ("clone_failed", "project")
    assert diagnostics[0]["message"].startswith("git clone exited 128; stderr tail:\n")
    assert "127.0.0.1" in diagnostics[0]["message"]
    assert lines[-1]["schema"] == "lab-owner-shutdown/1"
    assert "s3cret" not in result.stderr


async def test_status_route_passes_query_parameters(service):
    runtime, client = service
    response = await client.get("/v1/projects/acq/status?implementation=agent&scenario=evolve")
    assert response.status_code == 200 and response.json()["schema"] == "lab-project-status/6"
    assert runtime.project_session.calls[-1] == ("status_payload", "acq", "agent", "evolve")
    assert (await client.get("/v1/projects/acq/status?other=1")).status_code == 400
    assert (await client.get("/v1/projects/acq/status", headers=key("r-1"))).status_code == 400


async def test_scenario_routes_serve_the_catalogue(service):
    from evolution_lab import guides
    runtime, client = service
    listing = await client.get("/v1/scenarios")
    assert listing.status_code == 200 and listing.json() == guides.scenarios().list_payload()
    entry = await client.get("/v1/scenarios/connect")
    assert entry.status_code == 200 and entry.json() == guides.scenarios().entry_payload("connect")
    missing = await client.get("/v1/scenarios/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/v1/scenarios?x=1")).status_code == 400
    assert runtime.project_session.calls == []  # no project session involved


async def test_scenario_schema_route_serves_the_file(service, tmp_path, monkeypatch):
    from scenario_fixtures import write_catalogue
    from evolution_lab import guides
    from evolution_lab.guides._scenarios import read_catalogue
    runtime, client = service
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    monkeypatch.setattr(guides, "scenarios", lambda: catalogue)
    found = await client.get("/v1/scenarios/restructure/schemas/behavior-map/1")
    assert found.status_code == 200 and found.headers["content-type"].startswith("application/schema+json")
    assert found.content == (tmp_path / "c" / "restructure" / "schemas" / "behavior-map-1.json").read_bytes()
    for path in ("/v1/scenarios/restructure/schemas/behavior-map/2", "/v1/scenarios/nope/schemas/behavior-map/1"):
        missing = await client.get(path)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/v1/scenarios/restructure/schemas/behavior-map/1?x=1")).status_code == 400
    assert runtime.project_session.calls == []


async def test_scenario_routes_work_without_a_home(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2))
    runtime._admission_open = True
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        assert (await client.get("/v1/scenarios")).status_code == 200


async def test_project_operations_summary_view(service):
    runtime, client = service
    session = runtime.project_session
    value = (await client.get("/v1/projects/acq/operations?view=summary&implementation=agent")).json()
    assert value == {"schema": "lab-project-operation-list/2", "project": "acq", "operations": []}
    assert session.calls[-1] == ("operations_summary", "acq", "agent")
    plain = await client.get("/v1/projects/acq/operations")
    assert plain.json()["schema"] == "lab-project-operation-list/1" and session.calls[-1] == ("operations", "acq", None)
    refused = await client.get("/v1/projects/acq/operations?view=full")
    assert (refused.status_code, refused.json()["error"]["code"]) == (400, "invalid_request")


@pytest.mark.parametrize("prepare_schema", PROJECT_PREPARE)
async def test_project_prepare_names_the_missing_key(service, prepare_schema):
    """W52: the project route reaches decode_prepare_request and gets the same message."""
    _runtime, client = service
    response = await client.post("/v1/projects/acq/prepared-executions", json={
        "schema": prepare_schema, "implementation": "agent", "kind": "campaign",
        "bundle_digest": DIGEST, "cleanup_wait_seconds": None})
    body = response.json()["error"]
    assert body["code"] == "invalid_request" and "missing: stop_grace_seconds" in body["message"]


PROJECT_EVALUATION = {"implementation": "agent", "kind": "evaluation", "bundle_digest": DIGEST,
                      "selection": {"all": True}, "repeat_seeds": None, "evaluation_set_id": None, "max_turns": None,
                      "timeout_seconds": None, "metric_plan": None, "runtime_policy": None,
                      "max_failed_turns": None, "call_retries": None}


async def test_project_prepare_2_carries_the_split_and_1_means_split_null(service):
    """User decision 2026-10-09: project /1 is the fields of prepare /2 (split null), project /2 those of /3."""
    runtime, client = service
    route = "/v1/projects/acq/prepared-executions"
    old = await client.post(route, json={"schema": "lab-service-project-prepare-request/1", **PROJECT_EVALUATION})
    new = await client.post(route, json={"schema": "lab-service-project-prepare-request/2", **PROJECT_EVALUATION,
                                         "split": "test"})
    assert old.status_code == new.status_code == 200
    params = [call[5] for call in runtime.project_session.calls if call[0] == "prepare"]
    assert [value["split"] for value in params] == [None, "test"]
    for body in ({"schema": "lab-service-project-prepare-request/1", **PROJECT_EVALUATION, "split": "test"},
                 {"schema": "lab-service-project-prepare-request/2", **PROJECT_EVALUATION}):
        refused = await client.post(route, json=body)
        assert refused.json()["error"]["code"] == "invalid_request"
    for schema in ("lab-service-project-prepare-request/4", ["lab-service-project-prepare-request/2"]):
        unsupported = await client.post(route, json={"schema": schema, **PROJECT_EVALUATION, "split": None})
        assert unsupported.json()["error"]["code"] == "unsupported_schema"


PORT = {"schema": "lab-service-project-port-request/1", "from": "agent", "template": "/t/sylph", "ref": "v1",
        "id": None, "into": None}


async def test_port_route_decodes_the_body_and_is_idempotent(service):
    runtime, client = service
    first = await client.post("/v1/projects/acq/ports", json=PORT, headers=key("r-port"))
    again = await client.post("/v1/projects/acq/ports", json=PORT, headers=key("r-port"))
    assert first.status_code == again.status_code == 200 and first.json() == again.json() == PROJECT
    assert [call for call in runtime.project_session.calls if call[0] == "port"] == [
        ("port", "acq", "agent", "/t/sylph", "v1", None, None)]
    other = await client.post("/v1/projects/acq/ports", json={**PORT, "ref": "v2"}, headers=key("r-port"))
    assert other.status_code == 409 and other.json()["error"]["code"] == "request_conflict"


@pytest.mark.parametrize("body", [{**PORT, "extra": 1}, {k: v for k, v in PORT.items() if k != "into"},
                                  {**PORT, "from": ""}, {**PORT, "template": None}, {**PORT, "id": 3},
                                  {**PORT, "schema": "lab-service-project-port-request/2"}])
async def test_port_route_refuses_a_malformed_body(service, body):
    runtime, client = service
    response = await client.post("/v1/projects/acq/ports", json=body)
    assert response.status_code == 400
    assert not [call for call in runtime.project_session.calls if call[0] == "port"]


async def test_port_errors_keep_the_closed_envelope_and_name_the_destination(service):
    runtime, client = service
    response = await client.post("/v1/projects/acq/ports", json={**PORT, "from": "gone"})
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "port_destination_exists", "stage": "project",
                                         "message": "The port destination exists and is not empty. /h/ports/acq/gone-port"}}


@pytest.mark.parametrize("code, status", [
    ("implementation_archived", 409), ("git_unavailable", 422), ("port_destination_exists", 409),
    ("port_into_working_copy", 400), ("port_into_lab_home", 400), ("template_not_git", 422),
    ("template_ref_not_found", 422), ("template_fetch_failed", 422), ("port_failed", 500)])
def test_port_failures_map_to_http_statuses(code, status):
    assert error_response(ProjectError(code))["status"] == status


async def test_probe_class_has_its_own_limit():
    import asyncio
    from evolution_lab.service.errors import ServiceError
    from evolution_lab.service.mutations import ROUTE_CLASSES, MutationTable
    probe = {"project_probe_start", "probe_turn", "probe_reset", "probe_close"}
    assert {name for name, kind in ROUTE_CLASSES.items() if kind == "probe"} == probe
    table = MutationTable(project_limit=1, probe_limit=1)
    table.record("k1", "probe_turn", b"d", asyncio.get_running_loop().create_future())
    with pytest.raises(ServiceError) as caught:
        table.ensure_capacity("probe_close")
    assert caught.value.code == "request_table_full"
    table.ensure_capacity("project_create")  # a full PROBE table does not block a project mutation


async def test_probe_class_default_limit_is_one_thousand():
    import asyncio
    from evolution_lab.service.errors import ServiceError
    from evolution_lab.service.mutations import MutationTable
    table = MutationTable()
    loop = asyncio.get_running_loop()
    for number in range(1_000):
        table.record(f"k{number}", "probe_turn", b"d", loop.create_future())
    with pytest.raises(ServiceError) as caught:
        table.ensure_capacity("probe_turn")
    assert caught.value.code == "request_table_full"
    table.ensure_capacity("project_create")

ACCEPT_REQUEST = {"schema": "lab-service-accept-request/2", "implementation": "agent", "only": None,
                  "across_pairs": False, "reason": "typo", "channel": "cli", "author": "t", "dry_run": False,
                  "expected": [{"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"},
                                "to": {"attests": "sha256:" + "b" * 64}}]}


@pytest.mark.parametrize("expected", [None, ACCEPT_REQUEST["expected"], [{"id": [], "from": {}, "to": None}]])
async def test_accept_request_2_closed_form(service, expected):
    runtime, client = service
    payload = {**ACCEPT_REQUEST, "expected": expected}
    response = await client.post("/v1/projects/acq/acceptances", json=payload)
    assert response.status_code == 200, response.text
    assert runtime.project_session.calls[-1][-1] == expected
    for bad in ({k: v for k, v in payload.items() if k != "expected"}, {**payload, "extra": True}):
        response = await client.post("/v1/projects/acq/acceptances", json=bad)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("expected", [[], {}, "x", [None], [{"id": "x", "from": {}}],
                                    [{"id": "x", "from": {}, "to": {}, "extra": 1}]])
async def test_accept_request_2_empty_expected_invalid(service, expected):
    _, client = service
    response = await client.post("/v1/projects/acq/acceptances", json={**ACCEPT_REQUEST, "expected": expected})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


async def test_accept_request_1_still_accepted(service):
    runtime, client = service
    payload = {key: value for key, value in ACCEPT_REQUEST.items() if key != "expected"}
    payload["schema"] = "lab-service-accept-request/1"
    assert (await client.post("/v1/projects/acq/acceptances", json=payload)).status_code == 200
    assert runtime.project_session.calls[-1][-1] is None
    assert (await client.post("/v1/projects/acq/acceptances", json={**payload, "expected": None})).status_code == 400


def test_accept_request_unknown_version_is_unsupported_schema():
    from evolution_lab.service.project_codecs import decode_accept

    payload = {key: value for key, value in ACCEPT_REQUEST.items() if key != "expected"}
    payload["schema"] = "lab-service-accept-request/4"
    with pytest.raises(ServiceError) as caught:
        decode_accept(payload)
    assert caught.value.code == "unsupported_schema"


async def test_project_prepare_3_carries_all_settings_and_rejects_unknown_fields(service):
    runtime, client = service
    route = "/v1/projects/acq/prepared-executions"
    body = {"schema": "lab-service-project-prepare-request/3", **PROJECT_EVALUATION,
            "split": "test", "sample_retries": 2, "min_coverage": 0.9, "concurrency": 2,
            "scorer_concurrency": 3, "max_consecutive_unmeasured": 4}
    response = await client.post(route, json=body)
    assert response.status_code == 200
    assert response.json()["link"] == {"schema": "lab-project-link/1", "kind": "prepared"}
    call = runtime.project_session.calls[-1]
    assert call[:5] == ("prepare", "acq", "agent", DIGEST, "evaluation")
    assert {key: call[5][key] for key in ("split", "sample_retries", "min_coverage", "concurrency",
                                          "scorer_concurrency", "max_consecutive_unmeasured")} == {
        "split": "test", "sample_retries": 2, "min_coverage": 0.9, "concurrency": 2,
        "scorer_concurrency": 3, "max_consecutive_unmeasured": 4}
    refused = await client.post(route, json={**body, "unknown": 1})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_request"
