"""Project approval contracts through the service, registry and Owner stores."""
from __future__ import annotations

from hashlib import sha256
import sqlite3

import httpx
import pytest

from evolution_lab.owner.models import Approval
from evolution_lab.projects import sync
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.inventory import inventory_digest
from evolution_lab.projects.models import Link
from evolution_lab.projects.package import inventory_sources
from evolution_lab.projects.store import RegistryStore
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from test_projects_sync_session import git
from test_service_approvals_admission import (
    DIGEST, ZERO_DIGEST, approval, assert_approval, headers, offline_target, operation_approvals,
)


PROJECT = "/v1/projects/acq"
CONFIRM = {"schema": "lab-service-confirm-request/2", "implementation": None, "claim": "spec.confirmed",
           "attests": {"path": "SPEC.md"}, "linked": [], "channel": "mcp", "author": "Ada", "note": None,
           "approval": approval("project_confirm")}
ACCEPT = {"schema": "lab-service-accept-request/3", "implementation": None, "only": None, "across_pairs": False,
          "reason": "amend the spec", "channel": "mcp", "author": "Ada", "dry_run": False, "expected": None,
          "approval": approval("project_accept_commit")}


@pytest.fixture
async def runtime(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", home_directory=tmp_path / "home"))
    try:
        assert await runtime.start()
        yield runtime
    finally:
        await runtime.shutdown()


@pytest.fixture
async def service(runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                 base_url="http://lab") as client:
        yield runtime.owner, client


def create_project(projects, **source):
    return projects.create(id="acq", name="Acq", materials_root="lab",
                           implementations=[{"id": "agent", "place": "agent"}], **source)


@pytest.fixture
def work(runtime, tmp_path):
    work = tmp_path / "work"
    (work / "agent").mkdir(parents=True)
    (work / "lab").mkdir()
    (work / "lab" / "SPEC.md").write_text("v1\n")
    create_project(runtime.projects, path=str(work))
    return work


@pytest.fixture
def clone(runtime, tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = T\n\temail = t@t\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    team = tmp_path / "team"
    (team / "agent").mkdir(parents=True)
    (team / "agent" / "a.py").write_text("A = 1\n")
    git(team, "init", "-q", "-b", "main")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "initial")
    project = create_project(runtime.projects, clone={"repo": str(team)})
    (team / "agent" / "a.py").write_text("A = 2\n")
    git(team, "commit", "-qam", "incoming")
    return team, runtime.projects.home / "clones" / project.id, project.working_copy.source["base_revision"]


async def post(service, path, body, request_id=None):
    owner, client = service
    return await client.post(PROJECT + path, headers=headers(owner, request_id) if request_id else None, json=body)


def expected_facts(preview):
    return [{key: fact[key] for key in ("id", "from", "to")} for fact in preview["facts"]]


async def stale_confirmation(service, work):
    # Use the previous version to establish the stale fact without an approval.
    legacy = {**CONFIRM, "schema": "lab-service-confirm-request/1", "channel": "cli"}
    del legacy["approval"]
    response = await post(service, "/confirmations", legacy)
    assert response.status_code == 200, response.text
    (work / "lab" / "SPEC.md").write_text("v2\n")
    return response.json()


async def preview_acceptance(service):
    body = {**ACCEPT, "dry_run": True}
    response = await post(service, "/acceptances", body, "preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["schema"] == "lab-project-acceptance/1" and preview["id"] is None
    return expected_facts(preview)


def reject_approval_write(store, *, operation="INSERT"):
    store._execute(f"""CREATE TRIGGER reject_approval BEFORE {operation} ON approvals
                       BEGIN SELECT RAISE(ABORT, 'approval write refused'); END""")


async def test_confirm_records_author_and_approval_once_across_replay(service, runtime, work):
    first = await post(service, "/confirmations", CONFIRM, "confirm")
    assert first.status_code == 200, first.text
    assert (await post(service, "/confirmations", CONFIRM, "confirm")).json() == first.json()
    changed = {**CONFIRM, "approval": {**CONFIRM["approval"], "via": "client_rules"}}
    conflict = await post(service, "/confirmations", changed, "confirm")
    assert conflict.json()["error"]["code"] == "request_conflict"
    value = first.json()
    assert value["schema"] == "lab-project-confirmation/1"
    assert value["author"] == "Ada" and value["channel"] == "mcp" and "approval" not in value
    store = runtime.projects.store
    assert [item.to_payload() for item in store.confirmations("acq")] == [value]
    record, = store.project_approvals("acq")
    assert_approval(record, request_id="confirm", tool="project_confirm", resource_kind="confirmation",
                    resource_id=value["id"], start=None, outcome="applied")
    assert store.project_approvals("other") == []


async def test_approved_confirmation_retains_digest_mismatch(service, runtime, work):
    wrong = "sha256:" + sha256(b"old").hexdigest()
    body = {**CONFIRM, "attests": {"path": "SPEC.md", "digest": wrong}}
    response = await post(service, "/confirmations", body, "mismatched")
    assert response.status_code == 409 and response.json()["error"]["code"] == "digest_mismatch"
    assert runtime.projects.store.confirmations("acq") == ()
    assert runtime.projects.store.project_approvals("acq") == []


async def test_accept_dry_run_writes_no_approval_and_commit_binds_expected_facts(service, runtime, work):
    await stale_confirmation(service, work)
    expected = await preview_acceptance(service)
    store = runtime.projects.store
    assert store.acceptances("acq") == ()
    assert store.project_approvals("acq") == []
    unbound = await post(service, "/acceptances", ACCEPT, "unbound")
    assert unbound.status_code == 400 and unbound.json()["error"]["code"] == "invalid_request"
    result = await post(service, "/acceptances", {**ACCEPT, "expected": expected}, "accept")
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["schema"] == "lab-project-acceptance/1" and expected_facts(value) == expected
    assert value["author"] == "Ada" and value["channel"] == "mcp" and "approval" not in value
    assert [item.to_payload() for item in store.acceptances("acq")] == [value]
    record, = store.project_approvals("acq")
    assert_approval(record, request_id="accept", tool="project_accept_commit", resource_kind="acceptance",
                    resource_id=value["id"], start=None, outcome="applied")


async def test_direct_approved_acceptance_requires_expected_facts(service, runtime, work):
    await stale_confirmation(service, work)
    with pytest.raises(ProjectError) as result:
        await runtime.project_session.accept(
            "acq", None, only=None, across_pairs=False, reason="amend the spec", channel="mcp", author="Ada",
            dry_run=False, approval=Approval.decode(ACCEPT["approval"]), request_id="direct-unbound",
        )
    assert result.value.code == "invalid_request"
    store = runtime.projects.store
    assert store.acceptances("acq") == () and store.project_approvals("acq") == []


async def test_approved_acceptance_retains_digest_mismatch(service, runtime, work):
    await stale_confirmation(service, work)
    expected = await preview_acceptance(service)
    (work / "lab" / "SPEC.md").write_text("v3\n")
    response = await post(service, "/acceptances", {**ACCEPT, "expected": expected}, "mismatched")
    assert response.status_code == 409 and response.json()["error"]["code"] == "digest_mismatch"
    assert runtime.projects.store.acceptances("acq") == ()
    assert runtime.projects.store.project_approvals("acq") == []


async def test_approved_acceptance_retains_inputs_changed_under_mutation_lock(service, runtime, work, monkeypatch):
    # Feed the facts reader a persisted check result and its original inventory.
    session = runtime.project_session
    store = runtime.projects.store
    source = work / "agent" / "a.py"
    source.write_text("A = 1\n")
    sources = [{"path": "agent", "alias": "implementation"}]
    entries = inventory_sources(sources, working_copy=work)
    digest = inventory_digest(entries)
    store.put_inventory(digest, entries)
    store.insert_link(Link("target", "acq", "agent", session.pair, session.state_root, "t",
                           {"working_copy": str(work), "materials_root": "lab", "place": "agent",
                            "inputs": {"digest": digest, "sources": sources}},
                           ZERO_DIGEST, None, "t1", None,
                           {"tasks": [{"task_id": "k1", "action": "check", "state": "succeeded"}]}))
    source.write_text("A = 2\n")
    expected = await preview_acceptance(service)
    assert [fact["id"] for fact in expected] == ["check.passed"]
    facts = session.facts

    async def move_inputs_after_facts(*args, **kwargs):
        assert session._mutation_lock.locked()
        result = await facts(*args, **kwargs)
        source.write_text("A = 3\n")
        return result

    monkeypatch.setattr(session, "facts", move_inputs_after_facts)
    response = await post(service, "/acceptances", {**ACCEPT, "expected": expected}, "changed-inputs")
    assert response.status_code == 409 and response.json()["error"]["code"] == "inputs_changed"
    assert store.acceptances("acq") == () and store.project_approvals("acq") == []


@pytest.mark.parametrize("kind", ["confirmation", "acceptance"])
async def test_record_and_approval_rollback_together(service, runtime, work, kind):
    store = runtime.projects.store
    if kind == "confirmation":
        suffix, body, records = "/confirmations", CONFIRM, store.confirmations
    else:
        await stale_confirmation(service, work)
        body = {**ACCEPT, "expected": await preview_acceptance(service)}
        suffix, records = "/acceptances", store.acceptances
    reject_approval_write(store)
    result = await post(service, suffix, body, "rollback")
    assert result.status_code == 500, result.text
    assert records("acq") == () and store.project_approvals("acq") == []


@pytest.mark.parametrize("kind", ["confirmation", "acceptance"])
async def test_revocation_records_only_the_first_revoke(service, runtime, work, kind):
    value = await stale_confirmation(service, work)
    store = runtime.projects.store
    if kind == "acceptance":
        legacy = {**ACCEPT, "schema": "lab-service-accept-request/2", "channel": "cli"}
        del legacy["approval"]
        result = await post(service, "/acceptances", legacy)
        assert result.status_code == 200, result.text
        value = result.json()
    path = f"/{kind}s/{value['id']}/revoke"
    tool = f"project_revoke_{kind}"
    body = {"schema": "lab-service-revoke-request/2", "reason": "first", "approval": approval(tool)}
    reject_approval_write(store)
    rejected = await post(service, path, body, "rollback-revoke")
    assert rejected.status_code == 500, rejected.text
    assert getattr(store, f"{kind}s")("acq")[0].revoked is None
    store._execute("DROP TRIGGER reject_approval")
    first = await post(service, path, body, tool)
    assert first.status_code == 200, first.text
    second = await post(service, path, {**body, "reason": "second"}, f"{tool}-again")
    assert second.json() == first.json()
    assert first.json()["revoked"]["reason"] == "first"
    assert set(first.json()) == set(value)
    record, = store.project_approvals("acq")
    assert_approval(record, request_id=tool, tool=tool, resource_kind=kind,
                    resource_id=value["id"], start=None, outcome="applied")


async def prepare_sync(service):
    result = await post(service, "/sync/check", {"schema": "lab-service-project-sync-check-request/1", "rev": None})
    assert result.status_code == 200, result.text
    return {"schema": "lab-service-project-sync-request/2", "upstream": result.json()["upstream"]["sha"],
            "approval": approval("project_sync")}


async def test_sync_persists_admission_before_git_then_marks_applied(service, runtime, clone, monkeypatch):
    body = await prepare_sync(service)
    store = runtime.projects.store
    integrate = sync.integrate

    def integrate_with_admission(path, **kwargs):
        with sqlite3.connect(runtime.projects.home / "projects.sqlite3") as reader:
            assert reader.execute("SELECT outcome FROM approvals").fetchall() == [("admitted",)]
        assert git(path, "rev-parse", "HEAD") == clone[2]
        return integrate(path, **kwargs)

    monkeypatch.setattr(sync, "integrate", integrate_with_admission)
    result = await post(service, "/sync", body, "sync")
    assert result.status_code == 200, result.text
    assert result.json()["schema"] == "lab-project-sync-result/1"
    assert set(result.json()) == {"schema", "mode", "merged", "head", "project"}
    assert result.json()["merged"] == git(clone[1], "rev-parse", "HEAD") == body["upstream"]
    record, = store.project_approvals("acq")
    assert_approval(record, request_id="sync", tool="project_sync", resource_kind="sync",
                    resource_id=body["upstream"], start=None, outcome="applied")


@pytest.mark.parametrize("code", ["upstream_changed", "working_copy_dirty"])
async def test_refused_sync_retains_exact_sha_refusal_and_marks_failed(service, runtime, clone, code):
    body = await prepare_sync(service)
    if code == "upstream_changed":
        body["upstream"] = "f" * 40
    else:
        (clone[1] / "agent" / "a.py").write_text("dirty\n")
    result = await post(service, "/sync", body, "refused-sync")
    assert result.status_code == 409 and result.json()["error"]["code"] == code
    assert git(clone[1], "rev-parse", "HEAD") == clone[2]
    assert runtime.projects.get("acq").working_copy.source["base_revision"] == clone[2]
    record, = runtime.projects.store.project_approvals("acq")
    assert_approval(record, request_id="refused-sync", tool="project_sync", resource_kind="sync",
                    resource_id=body["upstream"], start=None, outcome="failed")


async def test_refused_sync_preserves_git_refusal_if_approval_update_fails(service, runtime, clone):
    body = await prepare_sync(service)
    body["upstream"] = "f" * 40
    store = runtime.projects.store
    reject_approval_write(store, operation="UPDATE")
    result = await post(service, "/sync", body, "refused-sync")
    assert result.status_code == 409 and result.json()["error"]["code"] == "upstream_changed"
    assert git(clone[1], "rev-parse", "HEAD") == clone[2]
    assert runtime.projects.get("acq").working_copy.source["base_revision"] == clone[2]
    record, = store.project_approvals("acq")
    assert record["outcome"] == "admitted"


@pytest.mark.parametrize("failure", ["registry_write", "approval_update", "post_merge_git"])
async def test_successful_git_with_unrecorded_sync_leaves_admitted(service, runtime, clone, monkeypatch, failure):
    body = await prepare_sync(service)
    store = runtime.projects.store
    if failure == "registry_write":
        def fail_record_sync(*args, **kwargs):
            raise OSError("registry write refused")
        monkeypatch.setattr(runtime.projects, "record_sync", fail_record_sync)
    elif failure == "approval_update":
        reject_approval_write(store, operation="UPDATE")
    else:
        run = sync._run

        def fail_after_merge(argv, path, code, **kwargs):
            if argv == ["rev-parse", "HEAD"] and git(path, "rev-parse", "HEAD") == body["upstream"]:
                raise RuntimeError("merged head could not be recorded")
            return run(argv, path, code, **kwargs)

        monkeypatch.setattr(sync, "_run", fail_after_merge)
    result = await post(service, "/sync", body, f"unrecorded-{failure}")
    assert result.status_code == 500 and result.json()["error"]["code"] == "sync_not_recorded"
    assert git(clone[1], "rev-parse", "HEAD") == body["upstream"]
    assert runtime.projects.get("acq").working_copy.source["base_revision"] == clone[2]
    record, = store.project_approvals("acq")
    assert record["outcome"] == "admitted"


def start_request(target_ref):
    return {"schema": "lab-service-project-start-request/2", "implementation": "agent", "prepared_digest": DIGEST,
            "target_ref": target_ref.to_payload(), "secrets": {}, "approval": approval("project_start")}


async def test_approved_project_start_records_owner_approval(service, runtime, work, offline_target):
    owner, _ = service
    session = runtime.project_session
    runtime.projects.store.insert_link(Link("prepared", "acq", "agent", session.pair, session.state_root, "t",
                                            {"inputs": {"digest": ZERO_DIGEST}}, ZERO_DIGEST, DIGEST,
                                            DIGEST, None, None))
    result = await post(service, "/operations", start_request(offline_target), "project-start")
    assert result.status_code == 202, result.text
    value = result.json()
    assert value["schema"] == "lab-project-start-result/5"
    assert set(value) == {"schema", "operation", "link"}
    assert value["link"]["project"] == "acq"
    operation_id = value["operation"]["id"]
    assert value["operation"]["starts"][0]["initiated_by"] == "human"
    record, = await operation_approvals(owner, operation_id)
    assert_approval(record, request_id="project-start", tool="project_start", resource_kind="operation",
                    resource_id=operation_id, start=1, outcome="admitted")
    assert runtime.projects.store.project_approvals("acq") == []


@pytest.mark.parametrize("path,body", [
    ("/confirmations", CONFIRM),
    ("/acceptances", {**ACCEPT, "dry_run": True}),
    ("/confirmations/c-1/revoke", {"schema": "lab-service-revoke-request/2", "reason": "x",
                                  "approval": approval("project_revoke_confirmation")}),
    ("/acceptances/a-1/revoke", {"schema": "lab-service-revoke-request/2", "reason": "x",
                                "approval": approval("project_revoke_acceptance")}),
    ("/sync", {"schema": "lab-service-project-sync-request/2", "upstream": "a" * 40,
               "approval": approval("project_sync")}),
    ("/operations", {"schema": "lab-service-project-start-request/2", "implementation": "agent", "secrets": {},
                     "prepared_digest": DIGEST, "target_ref": {"schema": "lab-target-ref/1", "id": "t",
                                                               "owner_generation": "old"},
                     "approval": approval("project_start")}),
])
async def test_approval_requests_require_key_and_closed_approval(service, path, body):
    result = await post(service, path, body)
    assert result.status_code == 400 and result.json()["error"]["code"] == "invalid_request"
    missing = {key: value for key, value in body.items() if key != "approval"}
    extra = {**body, "approval": {**body["approval"], "reason": "extra"}}
    for invalid in (missing, extra):
        result = await post(service, path, invalid, "invalid")
        assert result.status_code == 400 and result.json()["error"]["code"] == "invalid_request"


def test_migration_adds_approval_storage_without_rewriting_project(tmp_path):
    database = tmp_path / "projects.sqlite3"
    with RegistryStore.open(database) as registry:
        registry._execute("INSERT INTO projects VALUES (?, ?, ?)", ("acq", 0, '{"example": true}'))
        before = registry._execute("SELECT record_json FROM projects")
        registry._execute("DROP TABLE approvals")
        registry._execute("PRAGMA user_version=1004")
    with RegistryStore.open(database, read_only=True) as reader:
        assert reader.version == 1004
        assert reader.project_approvals("acq") == []
    with RegistryStore.open(database) as registry:
        assert registry.version == 1005
        assert registry._execute("SELECT record_json FROM projects") == before
        assert registry.project_approvals("acq") == []
    with sqlite3.connect(database.with_name("projects.sqlite3.bak-1004")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1004
        assert backup.execute("SELECT record_json FROM projects").fetchall() == before
