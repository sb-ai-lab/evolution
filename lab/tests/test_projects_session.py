import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from scaffold_15 import scaffold_15

from evolution_lab.owner.models import ClientTask, ResourceRef
from evolution_lab.preparation import PreparationStore
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.session import ProjectSession, client_ref, parse_client_ref

PAIR = Pair("0.0.3.dev28", "0.3.0a17")


def _sha(data: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class FakeStatus:
    id: str
    prepared_digest: str = "p" * 64
    control_state: str = "running"
    kind: str = "evaluation"
    result: object = None
    resume: object = None
    results_state: str = "present"
    def to_payload(self):
        return {"id": self.id, "control_state": self.control_state, "kind": self.kind,
                "result": self.result, "resume": self.resume, "results_state": self.results_state}


class FakeOwner:
    def __init__(self):
        self.operations, self.targets, self.tasks, self.statuses = [], [], [], {}
        self.exports = {}
        self.start_orders = {}  # operation_id -> Owner order key of its latest start (default: start order)
        self.kind = "evaluation"
        self.summary_calls, self.times, self.outcomes = [], {}, {}
    async def start(self, prepared_digest, *, target_ref, secrets, client_ref=None):
        status = FakeStatus(f"op{len(self.operations) + 1}", prepared_digest, kind=self.kind)
        self.operations.append((status.id, client_ref))
        self.statuses[status.id] = status
        return status
    async def bind(self, bundle_digest):
        return type("C", (), {"id": "c1", "bundle_digest": bundle_digest})()
    async def start_target(self, connection, *, secrets, client_ref=None):
        target_id = f"t{len(self.targets) + 1}"
        self.targets.append((target_id, connection.bundle_digest, client_ref))
        # The fake task ref carries its own target so task() can answer like Owner's
        # lab-task-status/1 does (ref = the target this start created or reused).
        return type("TaskRef", (), {"id": "task-start", "target_id": target_id})()
    async def task(self, ref):
        return type("TaskStatus", (), {"ref": ResourceRef("target", ref.target_id)})()
    async def client_operations(self):
        return tuple((i, r) for i, r in self.operations if r is not None)
    async def client_targets(self):
        return tuple(self.targets)
    async def client_tasks(self):
        return tuple(self.tasks)
    async def status(self, ref):
        return self.statuses[ref.id]
    async def list_summaries(self, ids=None):
        self.summary_calls.append(None if ids is None else set(ids))
        return tuple({"id": i, "kind": s.kind, "entrypoint": None, "control_state": s.control_state,
                      "results_state": s.results_state, "resume": s.resume, "progress": None,
                      "progress_state": "unavailable", "times": self.times.get(i),
                      "times_state": "present" if i in self.times else "unavailable",
                      "times_source": "core_raw" if i in self.times else None,
                      "times_reason": None if i in self.times else "unreadable",
                      "outcome": self.outcomes.get(i)}
                     for i, s in self.statuses.items() if ids is None or i in ids)
    async def cycle_starts(self):
        """Owner's one-read form: operation -> its current start key, that start's export, the
        export's deletion mark and the operation's results_state."""
        return {i: {"start": self.start_orders.get(i, n + 1),
                    "export_id": (self.exports.get(i) or {}).get("export_id"),
                    "export_deleted": bool((self.exports.get(i) or {}).get("deleted")),
                    "results_state": self.statuses[i].results_state}
                for n, (i, _) in enumerate(self.operations)}


class SlowOwner(FakeOwner):
    """A FakeOwner whose ``start`` suspends until the test releases it."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, prepared_digest, *, target_ref, secrets, client_ref=None):
        self.started.set()
        await self.release.wait()
        return await super().start(prepared_digest, target_ref=target_ref, secrets=secrets, client_ref=client_ref)


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    from evolution_lab.preparation import bundle as bundle_module
    from evolution_lab.preparation.environment import CapturedEnvironment
    from evolution_lab.preparation.models import EnvironmentDescriptor

    work = tmp_path / "work"
    lab_dir = work / "lab"
    scaffold_15(lab_dir)
    agent_dir = work / "agent"
    agent_dir.mkdir()
    (agent_dir / "a.py").write_text("A = 1\n")

    descriptor = EnvironmentDescriptor.from_payload({
        "schema": "lab-environment/1", "executable": sys.executable,
        "executable_sha256": "a" * 64, "python_version": "3.12.11",
        "distributions": [
            {"name": "evolution-lab", "version": "0.0.3.dev28", "wheel_sha256": "b" * 64},
            {"name": "evo-skills", "version": "0.3.0a17", "wheel_sha256": "c" * 64},
        ],
        "runtime_files": [], "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1", "limitations": ["synthetic capture boundary"],
    })

    def capture(value, *, execution):
        assert value.executable == Path(sys.executable)
        assert execution is True
        return CapturedEnvironment(descriptor, '{"archive_wheels":[]}')
    monkeypatch.setattr(bundle_module, "capture_environment", capture)

    manifest = lab_dir / "manifest.json"
    raw = json.loads(manifest.read_text())
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    manifest.write_text(json.dumps(raw))

    launcher = lab_dir / "launcher.py"
    launcher.write_text("raise RuntimeError('must not execute')\n")
    launcher.chmod(0o751)

    launch = lab_dir / "launch.json"
    launch.write_text(json.dumps({
        "schema": "lab-target-launch/1",
        "command": {"executable": sys.executable, "args": [{"bundle_path": "launcher.py"}, {"literal": "--offline"}]},
        "environment_ref": descriptor.digest, "program_paths": ["launcher.py"],
        "env": {"MODE": "local"}, "secret_names": ["TOKEN"],
        "endpoint": {"base_url": raw["target"]["base_url"]},
        "identity": {"target_id": "neutral", "contract_digest": None,
                     "settings_hash": None, "runtime_ref_digest": None,
                     "seed_mode": raw["target"]["seed_mode"]},
        "policy": "manifest.json", "limits": {"max_instances": 4, "max_concurrent_sessions": 1,
                                                  "max_input_bytes": 1 << 20, "max_output_bytes": 1 << 20},
        "startup_timeout_seconds": 10, "stop_grace_seconds": 10, "conformance": None,
    }))

    entry = lab_dir / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": "manifest.json", "launch_path": "launch.json"},
        "environments": [{"executable": sys.executable, "wheel_paths": [], "runtime_files": []}],
        "env": {"MODE": "local"}, "secret_names": ["TOKEN"],
        "roots": [{"alias": "agent", "source_path": "../agent"}, {"alias": "lab", "source_path": "."}],
        "path_bindings": [],
    }))

    store = PreparationStore(tmp_path / "state")
    projects = Projects.open(tmp_path / "home")
    projects.create(id="acq", name="Acq", materials_root="lab",
                    implementations=[{"id": "agent", "name": "Agent", "place": "agent",
                                      "package_entry": "bundle-request.json"}],
                    path=str(work))
    owner = FakeOwner()
    session = ProjectSession(projects, preparation=store, owner=owner, pair=PAIR, state_root=tmp_path / "state")
    yield session, projects, owner
    projects.close()


def test_client_ref_round_trip():
    value = client_ref("acq", "agent")
    assert value == '{"implementation":"agent","project":"acq"}'
    assert parse_client_ref(value) == ("acq", "agent")
    assert parse_client_ref("opaque") is None


async def _prepared(session):
    summary, _ = await session.bundle("acq")
    prepared, _ = await session.prepare("acq", None, bundle_digest=summary.digest, kind="campaign", params={})
    return prepared.digest


async def test_prepare_records_its_kind_on_the_link(tmp_path, session_env):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    _, link = await session.prepare("acq", None, bundle_digest=summary.digest, kind="campaign", params={})
    assert link.operation_kind == "campaign"


async def _campaign(session, owner):
    owner.kind = "campaign"
    status, link = await session.start("acq", None, prepared_digest=await _prepared(session), target_ref=object(), secrets={})
    owner.statuses[status.id].control_state = "finished"
    await session.sync("acq")
    return status.id, projects_link(session, status.id)


def projects_link(session, operation_id):
    return session.projects.store.link_for("operation", operation_id, PAIR, session.state_root)


async def test_sync_records_the_export_of_a_campaign(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    assert projects.store.link_exports("acq") == {}
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    await session.sync("acq")
    assert projects.store.link_exports("acq") == {link.dedupe_key(): ("e" * 64, 1)}


async def test_sync_drops_the_export_of_a_previous_start(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    await session.sync("acq")
    del owner.exports[operation_id]  # resumed: Owner reports no archive for the new start
    owner.start_orders[operation_id] = 7
    await session.sync("acq")
    assert projects.store.link_exports("acq") == {}


async def test_sync_keeps_the_start_of_a_recorded_export_when_owner_cannot_answer(tmp_path, session_env, monkeypatch):
    from evolution_lab.owner.models import OwnerError
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    await session.sync("acq")

    async def refuse():
        raise OwnerError("service_stopping", "finalizing")
    monkeypatch.setattr(owner, "cycle_starts", refuse)
    await session.sync("acq")
    assert projects.store.link_exports("acq") == {link.dedupe_key(): ("e" * 64, 1)}


def _result_step(value):
    [cycle] = value["scenarios"][0]["cycles"]
    return {s["id"]: s for s in cycle["steps"]}["result"]


async def test_resume_with_a_failed_owner_read_does_not_make_result_done(tmp_path, session_env, monkeypatch):
    """codex r1 C1 (ADR-0015): start 1's export is recorded; the operation resumes into start 2
    and sync's Owner read fails. The old export must not become start 2's result."""
    from evolution_lab.owner.models import OwnerError
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    await session.sync("acq")
    assert _result_step(await session.status_payload("acq", scenario="evolve"))["state"] == "done"
    del owner.exports[operation_id]  # resumed into start 2, which has not exported yet
    owner.start_orders[operation_id] = 7
    owner.statuses[operation_id].control_state = "running"
    reads = {"n": 0}
    answer = owner.cycle_starts

    async def fail_first():
        reads["n"] += 1
        if reads["n"] == 1:  # the read inside sync
            raise OwnerError("service_stopping", "finalizing")
        return await answer()
    monkeypatch.setattr(owner, "cycle_starts", fail_first)
    step = _result_step(await session.status_payload("acq", scenario="evolve"))
    assert reads["n"] == 2
    assert step["state"] != "done" and step.get("sources") != [{"export": "e" * 64, "results_state": "present"}]


async def test_a_stored_export_of_an_older_start_does_not_count(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.start_orders[operation_id] = 7  # the current start; nothing exported by it
    projects.store.set_link_export(link.dedupe_key(), "e" * 64, 1)  # start 1's export
    step = _result_step(await session.status_payload("acq", scenario="evolve"))
    assert step["state"] != "done" and step.get("sources") != [{"export": "e" * 64, "results_state": "present"}]


async def test_sync_does_not_ask_for_exports_of_evaluations(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    await session.start("acq", None, prepared_digest=await _prepared(session), target_ref=object(), secrets={})

    async def unexpected():
        raise AssertionError("an evaluation has no change-set export")
    monkeypatch.setattr(owner, "cycle_starts", unexpected)
    await session.sync("acq")


async def test_start_links_operation_and_blocks_mutations(tmp_path, session_env):
    session, projects, owner = session_env
    status, link = await session.start("acq", None, prepared_digest=await _prepared(session), target_ref=object(), secrets={})
    assert owner.operations == [(status.id, client_ref("acq", "agent"))]
    assert link.kind == "operation" and link.object_id == status.id and link.pair == PAIR
    assert await session.active_operation("acq") == status.id
    with pytest.raises(ProjectError) as caught:
        await session.update("acq", {"op": "archive", "reason": None})
    assert caught.value.code == "active_operation"


async def test_start_needs_a_linked_prepared(tmp_path, session_env):
    session, projects, owner = session_env
    with pytest.raises(ProjectError) as caught:
        await session.start("acq", None, prepared_digest="f" * 64, target_ref=object(), secrets={})
    assert caught.value.code == "not_linked" and owner.operations == []


async def test_link_failure_after_start_is_repaired_by_sync(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    prepared = await _prepared(session)
    original = projects.store.insert_link
    monkeypatch.setattr(projects.store, "insert_link", lambda link: (_ for _ in ()).throw(OSError("disk")))
    status, link = await session.start("acq", None, prepared_digest=prepared, target_ref=object(), secrets={})
    assert link is None
    assert await session.active_operation("acq") == status.id  # read from Owner, not the registry
    with pytest.raises(ProjectError) as caught:  # the missing link does not open the project to archive
        await session.update("acq", {"op": "archive", "reason": None})
    assert caught.value.code == "active_operation"
    monkeypatch.setattr(projects.store, "insert_link", original)
    await session.sync("acq")
    assert [l.object_id for l in await session.operations("acq")] == [status.id]


async def test_bundle_requires_coverage_of_the_place(tmp_path, session_env):
    session, projects, owner = session_env
    projects.update("acq", {"op": "move_implementation", "implementation": "agent", "place": "elsewhere"})
    with pytest.raises(ProjectError) as caught:
        await session.bundle("acq")
    assert caught.value.code == "package_mismatch"


async def test_two_projects_one_bundle_do_not_share_targets(tmp_path, session_env):
    session, projects, owner = session_env
    summary, link = await session.bundle("acq")
    twin_root = projects.get("acq").working_copy.path
    projects.create(id="twin", name="Twin", materials_root="lab", implementations=[{"id": "agent", "place": "agent", "package_entry": "bundle-request.json"}], path=twin_root)
    twin_summary, _ = await session.bundle("twin")
    assert twin_summary.digest == summary.digest
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    await session.sync()
    assert [l.object_id for l in projects.store.links("acq", kind="target")] == ["t1"]
    assert projects.store.links("twin", kind="target") == ()


# --- Fix round 1 (controller ruling R12) ---

async def test_start_target_link_failure_returns_none_and_sync_repairs(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    original = projects.store.insert_link
    monkeypatch.setattr(projects.store, "insert_link", lambda link: (_ for _ in ()).throw(OSError("disk")))
    task, link = await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    assert link is None
    assert task.id == "task-start" and owner.targets  # Owner still accepted the target
    monkeypatch.setattr(projects.store, "insert_link", original)
    await session.sync("acq")
    assert [l.object_id for l in projects.store.links("acq", kind="target")] == ["t1"]


async def test_start_target_survives_a_failing_operation_status_during_sync(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    prepared, _ = await session.prepare("acq", None, bundle_digest=summary.digest, kind="campaign", params={})
    await session.start("acq", None, prepared_digest=prepared.digest, target_ref=object(), secrets={})

    async def failing_status(ref):
        raise RuntimeError("owner unavailable")
    monkeypatch.setattr(owner, "status", failing_status)
    task, link = await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    assert link is None and task.id == "task-start"

    monkeypatch.undo()
    await session.sync("acq")
    assert [l.object_id for l in projects.store.links("acq", kind="target")] == ["t1"]


async def test_repeated_bundle_refreshes_stored_binding_and_downstream_links(tmp_path, session_env):
    session, projects, owner = session_env
    summary, link = await session.bundle("acq")
    original_work = Path(projects.get("acq").working_copy.path)
    moved = tmp_path / "moved"
    shutil.copytree(original_work, moved)
    projects.update("acq", {"op": "set_working_copy", "path": str(moved)})

    summary2, link2 = await session.bundle("acq")
    assert summary2.digest == summary.digest
    assert link2.binding["working_copy"] == str(moved) != link.binding["working_copy"]

    stored = projects.store.links("acq", implementation="agent", kind="bundle")
    assert len(stored) == 1
    assert stored[0].binding["working_copy"] == str(moved)

    prepared, prepared_link = await session.prepare("acq", None, bundle_digest=summary2.digest, kind="campaign", params={})
    assert prepared_link.binding["working_copy"] == str(moved)


async def test_update_waits_for_in_flight_start_before_seeing_it_active(tmp_path, session_env):
    session, projects, owner = session_env
    prepared = await _prepared(session)
    slow = SlowOwner()
    session.owner = slow

    start_task = asyncio.ensure_future(
        session.start("acq", None, prepared_digest=prepared, target_ref=object(), secrets={}))
    await slow.started.wait()

    update_task = asyncio.ensure_future(session.update("acq", {"op": "archive", "reason": None}))
    await asyncio.sleep(0)
    assert not update_task.done()  # blocked behind start()'s lock, not yet reading active_operation

    slow.release.set()
    status, link = await start_task
    with pytest.raises(ProjectError) as caught:
        await update_task
    assert caught.value.code == "active_operation"


async def test_facts_follow_inputs_changes(tmp_path, session_env):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert check.holds
    agent = Path(projects.get("acq").working_copy.path) / "agent"
    next(agent.rglob("*.py")).write_text("CHANGED = 1\n")
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert not check.holds and check.stale_reason.codes == ("inputs_changed",)


# --- Fix round 1 (controller ruling R14) ---

async def test_check_orders_by_owner_task_position_not_link_order(tmp_path, session_env):
    """I1: T1's link is older than T2's, but its task is recorded by Owner *after* T2's —
    the latest check must be picked by Owner's own order, not by which target's link the
    registry happens to iterate first."""
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})  # T1 (older link) -> "t1"
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})  # T2 (newer link) -> "t2"
    owner.tasks.append(ClientTask("k1", "check", "failed", "t2", summary.digest, client_ref("acq", "agent")))
    owner.tasks.append(ClientTask("k2", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert check.holds


async def test_check_orders_by_owner_task_position_reverse(tmp_path, session_env):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})  # T1 -> "t1"
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})  # T2 -> "t2"
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    owner.tasks.append(ClientTask("k2", "check", "failed", "t2", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert not check.holds


async def test_frozen_evaluation_result_survives_a_later_unreadable_journal(tmp_path, session_env):
    """I3 (spec §3.6 / PDR-0015): once an evaluation's result is recorded terminal, a later
    sync() reporting an unreadable results journal must not overwrite the fixed result."""
    session, projects, owner = session_env
    prepared = await _prepared(session)
    status, _ = await session.start("acq", None, prepared_digest=prepared, target_ref=object(), secrets={})
    owner.statuses[status.id].control_state = "finished"
    owner.statuses[status.id].result = {"coverage": {"expected": 2, "scored": 2}, "stop_reason": None,
                                        "persistence": {"state": "saved", "failure": None}}
    _, facts = await session.facts("acq")
    baseline = next(f for f in facts if f.id == "baseline.measured")
    assert baseline.holds

    owner.statuses[status.id].result = {"coverage": None, "stop_reason": "unknown",
                                        "persistence": {"state": "saved", "failure": None}}
    owner.statuses[status.id].results_state = "unavailable"
    _, facts = await session.facts("acq")
    baseline = next(f for f in facts if f.id == "baseline.measured")
    assert baseline.holds


async def test_package_present_source_names_the_reason(tmp_path, session_env):
    session, projects, owner = session_env
    entry = Path(projects.get("acq").working_copy.path) / "lab" / "bundle-request.json"

    async def present():
        _, facts = await session.facts("acq")
        return next(f for f in facts if f.id == "package.present")

    assert (await present()).source == {"kind": "package_entry", "entry": "bundle-request.json", "reason": None}
    entry.unlink()
    assert (await present()).source["reason"] == "missing"
    entry.mkdir()
    assert (await present()).source["reason"] == "not_a_file"
    entry.rmdir()
    projects.update("acq", {"op": "set_package", "implementation": "agent", "package_entry": None})
    assert (await present()).source == {"kind": "package_entry", "entry": None, "reason": "not_configured"}


async def test_facts_implementation_available_reflects_the_filesystem(tmp_path, session_env):
    session, projects, owner = session_env
    project = projects.get("acq")
    shutil.rmtree(Path(project.working_copy.path) / "agent")
    _, facts = await session.facts("acq")
    available = next(f for f in facts if f.id == "implementation.available")
    assert available.value is False


# --- Final-review fix wave ---

async def test_twin_projects_on_one_bundle_do_not_share_check_facts(tmp_path, session_env):
    """M11(a): the check ran on project A's target, so project B (same Bundle) has no check."""
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    projects.create(id="twin", name="Twin", materials_root="lab", path=projects.get("acq").working_copy.path,
                    implementations=[{"id": "agent", "place": "agent", "package_entry": "bundle-request.json"}])
    twin_summary, _ = await session.bundle("twin")
    assert twin_summary.digest == summary.digest
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    assert next(f for f in facts if f.id == "check.passed").holds
    _, facts = await session.facts("twin")
    check = next(f for f in facts if f.id == "check.passed")
    assert check.value is False and check.source is None


async def test_bundle_of_a_service_environment_package_without_one_is_unconfigured(tmp_path, session_env):
    """M2: like the general bundle route, a ``{"kind": "service"}`` package on a session without
    a service execution environment is ``service_environment_unconfigured``."""
    from evolution_lab.service.errors import ServiceError
    session, projects, owner = session_env
    entry = Path(projects.get("acq").working_copy.path) / "lab" / "bundle-request.json"
    request = json.loads(entry.read_text())
    request["environments"] = [{"kind": "service"}]
    entry.write_text(json.dumps(request))
    assert session._service_environment is None
    with pytest.raises(ServiceError) as caught:
        await session.bundle("acq")
    assert caught.value.code == "service_environment_unconfigured"


# --- Codex branch review round 2 ---

def _edit_request(projects, change):
    entry = Path(projects.get("acq").working_copy.path) / "lab" / "bundle-request.json"
    request = json.loads(entry.read_text())
    change(request)
    entry.write_text(json.dumps(request))


def _check(facts):
    return next(f for f in facts if f.id == "check.passed")


async def test_an_in_copy_source_whose_name_starts_with_at_stays_in_the_working_copy(tmp_path, session_env):
    """ADR-0017 p. 7 (codex r2): external labels are always ``@`` + an absolute path, so the
    working-copy directory ``@agent`` (label ``@agent``) is re-read as ``<wc>/@agent`` —
    never as ``agent`` relative to the service process — and its fact stays fresh."""
    session, projects, owner = session_env
    work = Path(projects.get("acq").working_copy.path)
    place = (work / "agent").rename(work / "@agent")
    projects.update("acq", {"op": "move_implementation", "implementation": "agent", "place": "@agent"})
    _edit_request(projects, lambda request: request["roots"][0].update(source_path="../@agent"))
    summary, link = await session.bundle("acq")
    assert {"alias": "agent", "path": "@agent"} in link.binding["inputs"]["sources"]
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    assert _check(facts).holds
    (place / "a.py").write_text("CHANGED = 1\n")
    _, facts = await session.facts("acq")
    assert not _check(facts).holds and _check(facts).stale_reason.codes == ("inputs_changed",)


async def test_an_external_source_is_still_read_by_its_absolute_path(tmp_path, session_env):
    session, projects, owner = session_env
    external = tmp_path / "ext"
    external.mkdir()
    (external / "data.txt").write_text("1\n")
    _edit_request(projects, lambda request: request["roots"].append({"alias": "ext", "source_path": str(external)}))
    summary, link = await session.bundle("acq")
    assert {"alias": "ext", "path": f"@{external}"} in link.binding["inputs"]["sources"]
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    assert _check(facts).holds
    (external / "data.txt").write_text("2\n")
    _, facts = await session.facts("acq")
    assert not _check(facts).holds and _check(facts).stale_reason.codes == ("inputs_changed",)


class GatedSyncOwner(FakeOwner):
    """The first sync() after a start suspends in client_operations until the test releases it."""

    def __init__(self):
        super().__init__()
        self.parked, self.gate, self._first = asyncio.Event(), asyncio.Event(), True

    async def client_operations(self):
        if self._first:
            self._first = False
            self.parked.set()
            await self.gate.wait()
        return await super().client_operations()


async def test_concurrent_start_target_returns_the_link_of_its_own_target(tmp_path, session_env):
    """codex r2: A accepts T1 and yields in sync(); B starts T2 (T1 not ready, no reuse) and
    links both. A must still answer with T1's link — the one its Owner task names — not the
    newest link of the same Bundle."""
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    gated = GatedSyncOwner()
    session.owner = gated
    first = asyncio.ensure_future(session.start_target("acq", None, bundle_digest=summary.digest, secrets={}))
    await gated.parked.wait()  # A: T1 accepted, parked inside sync()
    _, second_link = await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    gated.gate.set()
    _, first_link = await first
    assert second_link.object_id == "t2"
    assert first_link.object_id == "t1"
    assert [l.object_id for l in projects.store.links("acq", kind="target")] == ["t1", "t2"]


async def test_start_target_link_is_none_when_the_task_status_cannot_be_read(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")

    async def failing_task(ref):
        raise RuntimeError("owner unavailable")
    monkeypatch.setattr(owner, "task", failing_task)
    task, link = await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    assert link is None and task.id == "task-start"
    assert [l.object_id for l in projects.store.links("acq", kind="target")] == ["t1"]  # sync still linked it


@pytest.mark.parametrize("label", [
    '{"implementation":"agent","project":[]}', '{"implementation":"agent","project":{"id":"acq"}}',
    '{"implementation":7,"project":"acq"}', '{"implementation":"Agent!","project":"acq"}',
])
def test_parse_client_ref_needs_two_valid_ids(label):
    assert parse_client_ref(label) is None


async def test_sync_skips_a_malformed_client_ref(tmp_path, session_env):
    """codex r2: Owner accepts any opaque label; one with a list for the project must not
    reach SQLite (ProgrammingError) and abort startup sync — it is simply not a Lab label."""
    session, projects, owner = session_env
    bad = '{"implementation":"agent","project":[]}'
    owner.operations.append(("op-x", bad))
    owner.statuses["op-x"] = FakeStatus("op-x")
    owner.targets.append(("t-x", "b" * 64, bad))
    await session.sync()
    assert await session.operation_ids("acq") == ()
    assert projects.store.links("acq", kind="operation") == () and projects.store.links("acq", kind="target") == ()


async def test_status_of_a_fresh_project(tmp_path, session_env):
    session, projects, owner = session_env
    value = await session.status_payload("acq")
    assert value["schema"] == "lab-project-status/6" and (value["project"], value["implementation"]) == ("acq", "agent")
    connect, create_skill, evolve, restructure = value["scenarios"]
    assert create_skill["state"] == "available" and create_skill["steps"][0]["state"] == "ready"
    got = {s["id"]: s for s in connect["steps"]}
    assert connect["state"] == "available" and got["spec"]["state"] == "ready"
    assert got["package"]["state"] == "done"  # the fixture's package entry is valid
    assert evolve["state"] == "unavailable" and evolve["missing"] == ["connected"]
    plan = {s["id"]: s for s in evolve["next_cycle"]["steps"]}["plan"]
    assert plan["state"] == "done"  # a campaign entry with a valid manifest
    assert restructure["state"] == "unavailable" and restructure["missing"] == ["implementation.ported"]  # the fixture's implementation is not a port


async def test_status_sees_the_spec_file(tmp_path, session_env):
    session, projects, owner = session_env
    root = projects.root_path(projects.get("acq"))
    (root / "SPEC.md").write_text("# Spec\n")
    from evolution_lab import guides
    from evolution_lab.guides import answers
    (root / "spec.answers.json").write_text(
        json.dumps(answers.complete_answers(guides.requirements(), implementation="agent")), encoding="utf-8")
    connect = (await session.status_payload("acq", scenario="connect"))["scenarios"][0]
    assert {s["id"]: s for s in connect["steps"]}["spec"]["state"] == "done"


async def test_next_cycle_prepare_follows_current_inputs(tmp_path, session_env):
    session, projects, owner = session_env
    prepared = await _prepared(session)

    def prepare_step(value):
        return {s["id"]: s for s in value["scenarios"][0]["next_cycle"]["steps"]}["prepare"]
    step = prepare_step(await session.status_payload("acq", scenario="evolve"))
    assert step["state"] == "done" and step["sources"] == [{"prepared": prepared}]
    (tmp_path / "work" / "agent" / "a.py").write_text("A = 2\n")  # the Bundle's inputs changed (R2)
    assert prepare_step(await session.status_payload("acq", scenario="evolve"))["state"] == "ready"


async def test_evaluation_prepared_is_not_a_campaign_cycle(tmp_path, session_env):
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    # PreparationStore.prepare_evaluation on a campaign-sourced Bundle requires the evaluation's
    # full parameter set (no defaults to fall back on, per api.py's campaign branch); this is the
    # brief's step-1 fallback body, adapted from test_service_installed_projects.py's prepare
    # request (selection replaced by {"all": True} since the fixture's corpus has no "q1" case).
    params = {"selection": {"all": True}, "repeat_seeds": [17], "evaluation_set_id": "p1-projects-baseline",
             "max_turns": 2, "timeout_seconds": 30, "metric_plan": {"primary": "score"}, "runtime_policy": None,
             "max_failed_turns": 1, "call_retries": "off"}
    await session.prepare("acq", None, bundle_digest=summary.digest, kind="evaluation", params=params)
    value = await session.status_payload("acq", scenario="evolve")
    assert {s["id"]: s for s in value["scenarios"][0]["next_cycle"]["steps"]}["prepare"]["state"] == "ready"


async def test_status_lists_campaign_cycles_with_their_export(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    value = await session.status_payload("acq", scenario="evolve")
    [only] = value["scenarios"][0]["cycles"]
    assert (only["operation_id"], only["detail"], only["control_state"]) == (operation_id, "available", "finished")
    assert {s["id"]: s for s in only["steps"]}["result"]["sources"] == [{"export": "e" * 64, "results_state": "present"}]
    assert only["outcome"] is None  # the fake Owner's summary has no outcome yet
    owner.outcomes[operation_id] = {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v1"}
    owner.times[operation_id] = {"first_event_at": "2026-09-29T10:00:00Z", "last_event_at": "2026-09-29T12:00:00Z"}
    [only] = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    got = {s["id"]: s for s in only["steps"]}
    assert only["outcome"]["reason"] == "rounds_exhausted" and got["run"]["done_at"] == "2026-09-29T12:00:00Z"
    assert got["prepare"]["done_at"] == projects.store.link_for("prepared", link.prepared_digest, PAIR,
                                                                session.state_root).created_at


async def test_sync_keeps_the_export_id_after_results_are_deleted(tmp_path, session_env):
    """Decision В: the registry keeps what Owner reports — the id survives as a deleted export."""
    session, projects, owner = session_env
    operation_id, link = await _campaign(session, owner)
    owner.exports[operation_id] = {"operation_id": operation_id, "export_id": "e" * 64, "sha256": "e" * 64, "path": "x"}
    await session.sync("acq")
    owner.exports[operation_id]["deleted"] = True
    owner.statuses[operation_id].results_state = "deleted"
    await session.sync("acq")
    assert projects.store.link_exports("acq") == {link.dedupe_key(): ("e" * 64, 1)}


async def test_status_after_delete_results(tmp_path, session_env):
    session, projects, owner = session_env
    exported, _ = await _campaign(session, owner)
    owner.exports[exported] = {"operation_id": exported, "export_id": "e" * 64, "sha256": "e" * 64,
                               "path": "x", "deleted": True}
    owner.statuses[exported].results_state = "deleted"
    bare, _ = await _campaign(session, owner)
    owner.statuses[bare].results_state = "deleted"
    value = await session.status_payload("acq", scenario="evolve")
    cycles = {c["operation_id"]: {s["id"]: s for s in c["steps"]} for c in value["scenarios"][0]["cycles"]}
    assert cycles[exported]["result"]["state"] == "done"
    assert cycles[exported]["result"]["sources"] == [{"export": "e" * 64, "results_state": "deleted"}]
    assert (cycles[bare]["result"]["state"], cycles[bare]["result"]["blocked_reason"]) == (
        "blocked", {"code": "results_deleted"})
    assert cycles[bare]["run"]["done_at"] is None  # the operation's times went with the results


async def test_a_cycle_of_another_pair_reads_deletion_from_its_snapshot(tmp_path, session_env):
    from dataclasses import replace
    session, projects, owner = session_env
    _, link = await _campaign(session, owner)
    old = Pair("0.0.3.dev27", "0.3.0a17")
    gone = replace(link, pair=old, object_id="op-old", last_seen={**link.last_seen, "results_state": "deleted"})
    projects.store.insert_link(gone)
    projects.store.set_link_export(gone.dedupe_key(), "f" * 64, 3)
    value = await session.status_payload("acq", scenario="evolve")
    other = next(c for c in value["scenarios"][0]["cycles"] if c["operation_id"] == "op-old")
    result = {s["id"]: s for s in other["steps"]}["result"]
    assert (other["detail"], result["state"]) == ("unavailable", "done")
    assert result["sources"] == [{"export": "f" * 64, "results_state": "deleted"}]
    assert all("op-old" not in ids for ids in owner.summary_calls)


async def test_facts_payload_is_version_four(tmp_path, session_env):
    session, projects, owner = session_env
    assert (await session.facts_payload("acq"))["schema"] == "lab-project-facts/5"


async def test_an_older_campaign_resumed_later_is_the_current_cycle(tmp_path, session_env):
    """F1: the current cycle is the latest by Owner start, not by link creation order."""
    session, projects, owner = session_env
    older, _ = await _campaign(session, owner)
    newer, _ = await _campaign(session, owner)
    for operation_id in (older, newer):
        owner.statuses[operation_id].resume = {"state": "ready", "reason": "r"}
    owner.start_orders[older] = 10  # resumed (and suspended again) after `newer` started
    value = await session.status_payload("acq", scenario="evolve")
    cycles = value["scenarios"][0]["cycles"]
    assert [(c["operation_id"], c["current"]) for c in cycles] == [(older, True), (newer, False)]


async def test_missing_project_wins_over_an_unknown_scenario(tmp_path, session_env):
    """F6: the project and implementation are looked up before the scenario is validated."""
    session, projects, owner = session_env
    with pytest.raises(ProjectError) as caught:
        await session.status_payload("nobody", scenario="nope")
    assert caught.value.code == "project_not_found"


async def test_unknown_scenario_is_an_invalid_request(tmp_path, session_env):
    session, projects, owner = session_env
    with pytest.raises(ProjectError) as caught:
        await session.status_payload("acq", scenario="nope")
    assert caught.value.code == "invalid_request"


def _campaign_facts(payload):
    [fact] = [f for f in payload["facts"] if f["id"] == "campaigns"]  # docs/api/projects.md: a fact, not a top-level key
    return fact["value"]


def test_last_seen_keeps_the_frozen_resume_form_and_registry_resume_merges():
    from evolution_lab.projects.session import registry_resume, seen_resume
    target = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
              "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g"}]}
    assert seen_resume({"state": "ready", "reason": None, "target": target}) == {"state": "ready", "reason": None}
    assert seen_resume(None) is None
    assert registry_resume({"state": "ready", "reason": None}, target) == {
        "state": "ready", "reason": None, "target": target}
    assert registry_resume({"state": "ready", "reason": None}, None) == {
        "state": "ready", "reason": None, "target": None}
    assert registry_resume(None, target) is None
    refused = {"state": "refused", "reason": "results_deleted"}
    assert registry_resume(refused, target) == {"state": "refused", "reason": "results_deleted", "target": None}
    assert registry_resume({"state": "cleanup_first", "reason": "cleanup_required"}, target)["target"] == target
    assert registry_resume({"state": "busy", "reason": "resource_busy"}, target)["target"] == target


async def test_sync_records_the_target_requirement_outside_last_seen(tmp_path, session_env):
    session, projects, owner = session_env
    target = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
              "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g"}]}
    operation_id, _ = await _campaign(session, owner)
    owner.statuses[operation_id].resume = {"state": "ready", "reason": None, "target": target}
    await session.sync("acq")
    link = projects_link(session, operation_id)
    assert link.last_seen["resume"] == {"state": "ready", "reason": None}  # record_json form frozen
    expected = {"state": "ready", "reason": None, "target": target}
    [cycle] = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    assert cycle["resume"] == expected
    [campaign] = _campaign_facts(await session.facts_payload("acq"))
    assert campaign["resume"] == expected
    owner.statuses[operation_id].resume = {"state": "ready", "reason": None, "target": None}
    await session.sync("acq")
    assert session.projects.store.link_resume_targets("acq") == {}


async def test_link_without_resume_target_row_reads_null(tmp_path, session_env):
    # A campaign link as dev28 left it: last_seen.resume = {state, reason}, no table row.
    session, projects, owner = session_env
    operation_id, _ = await _campaign(session, owner)
    owner.statuses[operation_id].resume = {"state": "ready", "reason": None}
    await session.sync("acq")
    assert session.projects.store.link_resume_targets("acq") == {}
    [cycle] = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    assert cycle["resume"] == {"state": "ready", "reason": None, "target": None}
    [campaign] = _campaign_facts(await session.facts_payload("acq"))
    assert campaign["resume"] == {"state": "ready", "reason": None, "target": None}


async def test_sync_writes_last_seen_and_resume_target_in_one_transaction(tmp_path, session_env, monkeypatch):
    # ADR-0017: the registry snapshot of a campaign is never torn - a failure of the table write
    # rolls the link's last_seen back with it.
    from evolution_lab.projects.store import RegistryStore
    session, projects, owner = session_env
    operation_id, _ = await _campaign(session, owner)
    owner.statuses[operation_id].resume = {"state": "ready", "reason": None}
    await session.sync("acq")
    before = projects_link(session, operation_id).last_seen
    owner.statuses[operation_id].resume = {"state": "busy", "reason": "resource_busy"}

    def boom(self, dedupe, target):
        raise RuntimeError("crash between the halves")
    monkeypatch.setattr(RegistryStore, "set_link_resume_target", boom)
    with pytest.raises(RuntimeError):
        await session.sync("acq")
    assert projects_link(session, operation_id).last_seen == before


async def test_project_operations_summary(tmp_path, session_env):
    session, projects, owner = session_env
    first, _ = await _campaign(session, owner)
    second, _ = await _campaign(session, owner)
    owner.outcomes[first] = {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v1"}
    value = await session.operations_summary("acq")
    assert (value["schema"], value["project"]) == ("lab-project-operation-list/2", "acq")
    rows = value["operations"]
    assert [row["label"] for row in rows] == ["Acq · evolve · цикл 1", "Acq · evolve · цикл 2"]
    for row, operation_id in zip(rows, (first, second)):
        assert set(row) == {"link", "label", "operation_state", "operation", "outcome"}
        assert row["operation_state"] == "present" and row["operation"]["id"] == row["link"]["object_id"] == operation_id
    assert rows[0]["outcome"]["reason"] == "rounds_exhausted" and rows[1]["outcome"] is None


async def test_other_pair_link_has_no_operation(tmp_path, session_env):
    from dataclasses import replace
    from evolution_lab.owner.models import OperationResult, ProcessFacts
    session, projects, owner = session_env
    _, link = await _campaign(session, owner)
    core = {"campaign_outcome": {"outcome": "completed", "reason": "rounds_exhausted"},
            "final_champion": {"version": "v2"}}
    result = OperationResult(link.prepared_digest, ProcessFacts(), core=core).to_payload()
    old = Pair("0.0.3.dev27", "0.3.0a17")
    projects.store.insert_link(replace(link, pair=old, object_id="op-old",
                                       last_seen={**link.last_seen, "result": result}))
    value = await session.operations_summary("acq")
    row = next(row for row in value["operations"] if row["link"]["object_id"] == "op-old")
    assert (row["operation_state"], row["operation"]) == ("other_pair", None)
    assert row["outcome"] == {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v2"}
    assert row["label"] == "Acq · evolve · цикл 2"
    assert all("op-old" not in ids for ids in owner.summary_calls)  # another pair's state is never opened


async def test_evidence_steps_done_at_come_from_owner_times_not_link_creation(tmp_path, session_env):
    """Review fix 1: a task-sourced fact has no moment; an operation of this pair gives its
    Owner ``times.last_event_at`` (spec §2.5), not the link's creation time."""
    session, projects, owner = session_env
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    status, _ = await session.start("acq", None, prepared_digest=await _prepared(session), target_ref=object(), secrets={})
    owner.statuses[status.id].control_state = "finished"
    owner.statuses[status.id].result = {"coverage": {"expected": 2, "scored": 2}, "stop_reason": None,
                                        "persistence": {"state": "saved", "failure": None}}
    owner.times[status.id] = {"first_event_at": "2026-09-29T10:00:00Z", "last_event_at": "2026-09-29T11:30:00Z"}
    connect = (await session.status_payload("acq", scenario="connect"))["scenarios"][0]
    got = {s["id"]: s for s in connect["steps"]}
    assert got["check"]["state"] == "done" and got["check"]["done_at"] is None
    assert got["baseline"]["state"] == "done" and got["baseline"]["done_at"] == "2026-09-29T11:30:00Z"


async def test_cycle_label_does_not_depend_on_the_implementation_filter(tmp_path, session_env):
    """Ruling R7: cycles are numbered project-wide, then the implementation filter applies."""
    from dataclasses import replace
    session, projects, owner = session_env
    _, first = await _campaign(session, owner)
    projects.store.insert_link(replace(first, implementation="other", object_id="op-other"))
    third, _ = await _campaign(session, owner)

    def labels(value):
        return {row["link"]["object_id"]: row["label"] for row in value["operations"]}
    every = labels(await session.operations_summary("acq"))
    only = labels(await session.operations_summary("acq", "agent"))
    assert every["op-other"] == "Acq · evolve · цикл 2" and "op-other" not in only
    assert only[third] == every[third] == "Acq · evolve · цикл 3"


VALID_MAP = {"schema": "restructure-behavior-map/1", "behaviors": [{"id": "refund", "source": "agent/a.py"}]}


def _restructure_catalogue(tmp_path, monkeypatch, schema=None):
    from scenario_fixtures import BEHAVIOR_MAP, RESTRUCTURE, SKILL, write_catalogue
    from evolution_lab import guides
    from evolution_lab.guides._scenarios import read_catalogue
    files = {"schemas/behavior-map-1.json": BEHAVIOR_MAP if schema is None else schema}
    base = tmp_path / ("catalogue" if schema is None else "catalogue-changed")
    catalogue = read_catalogue(write_catalogue(base, {"restructure": (RESTRUCTURE, files, SKILL)}))
    monkeypatch.setattr(guides, "scenarios", lambda: catalogue)
    return catalogue


def _add_port(projects, port_id):
    projects.update("acq", {"op": "add_implementation", "implementation": {
        "id": port_id, "place": "agent", "origin": {"kind": "ported_from", "implementation": "agent"}}})


async def test_status_checks_each_implementations_own_schema_artifact(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    _restructure_catalogue(tmp_path, monkeypatch)
    _add_port(projects, "port-a")
    _add_port(projects, "port-b")
    root = tmp_path / "work" / "lab" / "restructure"
    (root / "port-a").mkdir(parents=True)
    (root / "port-b").mkdir()
    (root / "port-a" / "behavior-map.json").write_text(json.dumps(VALID_MAP))
    (root / "port-b" / "behavior-map.json").write_text(json.dumps({"schema": "restructure-behavior-map/1",
                                                                    "behaviors": [{"id": ""}]}))
    a = {s["id"]: s for s in (await session.status_payload("acq", "port-a"))["scenarios"][0]["steps"]}
    b = {s["id"]: s for s in (await session.status_payload("acq", "port-b"))["scenarios"][0]["steps"]}
    assert a["map"]["state"] == "done" and a["map"]["sources"] == [
        {"artifact": "restructure/port-a/behavior-map.json", "digest": _sha(json.dumps(VALID_MAP).encode())}]
    assert (b["map"]["state"], b["map"]["blocked_reason"]) == ("blocked", {"code": "artifact_invalid"})
    assert [e["path"] for e in b["map"]["invalid"]] == ["/behaviors/0", "/behaviors/0/id"]
    source = (await session.status_payload("acq", "agent"))["scenarios"][0]
    assert (source["state"], source["missing"]) == ("unavailable", ["implementation.ported"])


async def test_a_non_json_artifact_is_invalid_and_a_failing_schema_is_unusable(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    catalogue = _restructure_catalogue(tmp_path, monkeypatch)
    _add_port(projects, "port-a")
    target = tmp_path / "work" / "lab" / "restructure" / "port-a" / "behavior-map.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff not json")
    step = {s["id"]: s for s in (await session.status_payload("acq", "port-a"))["scenarios"][0]["steps"]}["map"]
    assert step["blocked_reason"] == {"code": "artifact_invalid"}
    assert step["invalid"] == [{"artifact": "restructure/port-a/behavior-map.json", "digest": _sha(b"\xff not json"),
                                "path": "", "message": "the file is not UTF-8 JSON"}]

    class Broken:
        def iter_errors(self, document):
            raise RuntimeError("validator bug")
    catalogue.validators["restructure"]["behavior-map/1"] = Broken()
    target.write_text(json.dumps(VALID_MAP))
    value = await session.status_payload("acq", "port-a")
    step = {s["id"]: s for s in value["scenarios"][0]["steps"]}["map"]
    assert value["schema"] == "lab-project-status/6"
    assert step["blocked_reason"] == {"code": "schema_unusable", "schema": "behavior-map/1"}


async def test_a_hugely_nested_artifact_is_invalid_and_the_status_still_answers(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    _restructure_catalogue(tmp_path, monkeypatch)
    _add_port(projects, "port-a")
    target = tmp_path / "work" / "lab" / "restructure" / "port-a" / "behavior-map.json"
    target.parent.mkdir(parents=True)
    target.write_text("[" * 100000)
    value = await session.status_payload("acq", "port-a")
    step = {s["id"]: s for s in value["scenarios"][0]["steps"]}["map"]
    assert step["blocked_reason"] == {"code": "artifact_invalid"}
    assert step["invalid"] == [{"artifact": "restructure/port-a/behavior-map.json", "digest": _sha(b"[" * 100000),
                                "path": "", "message": "nested too deeply"}]


async def test_a_schema_change_makes_a_catalogue_linked_confirmation_stale(tmp_path, session_env, monkeypatch):
    import copy
    from scenario_fixtures import BEHAVIOR_MAP
    session, projects, owner = session_env
    _restructure_catalogue(tmp_path, monkeypatch)
    (tmp_path / "work" / "lab" / "SPEC.md").write_text("spec\n")
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "catalogue", "ref": "scenarios"}], channel="cli", author="t", note=None)
    changed = copy.deepcopy(BEHAVIOR_MAP)
    changed["properties"]["note"] = {"type": "string"}
    _restructure_catalogue(tmp_path, monkeypatch, schema=changed)
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert not fact.holds and fact.stale_reason.codes == ("catalogue_changed",)


def _terminal_result():
    from evolution_lab.owner.models import OperationResult, ProcessFacts
    return OperationResult("a" * 64, ProcessFacts(True, 0, True, False),
                           core={"campaign_outcome": {"outcome": "completed", "reason": "rounds_exhausted"},
                                 "final_champion": {"version": "v1"}}).to_payload()


async def test_a_suspended_cycle_blocks_run_and_is_not_complete(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, _ = await _campaign(session, owner)
    owner.statuses[operation_id].resume = {"state": "ready", "reason": None, "target": None}
    [only] = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    run = {s["id"]: s for s in only["steps"]}["run"]
    assert (run["state"], run["blocked_reason"], only["complete"]) == ("blocked", {"code": "campaign_suspended"}, False)


async def test_a_terminal_cycle_stays_done_after_its_results_are_deleted(tmp_path, session_env):
    session, projects, owner = session_env
    operation_id, _ = await _campaign(session, owner)
    owner.statuses[operation_id].result = _terminal_result()
    await session.sync("acq")
    original = _terminal_result()
    expected = {key: value for key, value in original.items()
                if key not in {"unanswered", "unanswered_state", "answered_reported"}}
    expected["schema"] = "lab-operation-result/2"
    assert projects_link(session, operation_id).last_seen["result"] == expected
    owner.statuses[operation_id].result = None
    owner.statuses[operation_id].results_state = "deleted"
    [only] = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    run = {s["id"]: s for s in only["steps"]}["run"]
    assert (run["state"], only["outcome"]["outcome"], only["complete"]) == ("done", "completed", True)
    assert projects_link(session, operation_id).last_seen["result"] == expected


async def test_cycle_order_counts_the_projects_campaign_links(tmp_path, session_env):
    session, projects, owner = session_env
    first, _ = await _campaign(session, owner)
    second, _ = await _campaign(session, owner)
    cycles = (await session.status_payload("acq", scenario="evolve"))["scenarios"][0]["cycles"]
    assert {c["operation_id"]: c["order"] for c in cycles} == {first: 1, second: 2}
    labels = [row["label"] for row in (await session.operations_summary("acq"))["operations"]]
    assert labels[-2:] == ["Acq · evolve · цикл 1", "Acq · evolve · цикл 2"]


async def test_session_registry_reads_are_project_2(tmp_path, session_env):
    session, projects, owner = session_env
    got = await session.get_project("acq")
    assert got["schema"] == "lab-project/2" and [i["port"] for i in got["implementations"]] == [None]
    listed = await session.list_projects(archived=False)
    assert listed == {"schema": "lab-project-list/2", "projects": [got]}
    renamed = await session.update("acq", {"op": "rename", "name": "Acq 2"})
    assert renamed["schema"] == "lab-project/2" and renamed["name"] == "Acq 2"


def _document_catalogue(tmp_path, monkeypatch, schemas):
    """One scenario whose steps each check ``restructure/{implementation}/document.json`` against one schema
    (``schemas``: ``{step id: schema document}``), so several schemas can share one path."""
    import copy
    from scenario_fixtures import RESTRUCTURE, write_catalogue
    from evolution_lab import guides
    from evolution_lab.guides._scenarios import read_catalogue
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["produces"] = []
    descriptor["schemas"] = {f"{step}/1": f"schemas/{step}-1.json" for step in schemas}
    descriptor["steps"] = [
        {"id": step, "title": step, "kind": "work", "human_only": False, "optional": False, "requires": [],
         "outputs": [{"artifact": {"path": "restructure/{implementation}/document.json", "check": "schema",
                                   "schema": f"{step}/1"}}], "skill": "SKILL.md"}
        for step in schemas]
    files = {f"schemas/{step}-1.json": {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
             for step, schema in schemas.items()}
    catalogue = read_catalogue(write_catalogue(tmp_path / "catalogue-document", {"restructure": (descriptor, files, "x")}))
    monkeypatch.setattr(guides, "scenarios", lambda: catalogue)
    target = tmp_path / "work" / "lab" / "restructure" / "port-a" / "document.json"
    target.parent.mkdir(parents=True)
    return target


async def _document_steps(session):
    return {s["id"]: s for s in (await session.status_payload("acq", "port-a"))["scenarios"][0]["steps"]}


async def test_each_schema_checks_a_shared_artifact_path_on_its_own(tmp_path, session_env, monkeypatch):
    """codex r1 of the branch (PDR-0018): the check is keyed by (scenario, path, schema), so a later output on
    the same path never overwrites an earlier output's verdict."""
    session, projects, owner = session_env
    _add_port(projects, "port-a")
    target = _document_catalogue(tmp_path, monkeypatch, {"whole": {"type": "integer"}, "text": {"type": "string"}})
    target.write_text(json.dumps("text"))
    steps = await _document_steps(session)
    assert (steps["whole"]["state"], steps["whole"]["blocked_reason"]) == ("blocked", {"code": "artifact_invalid"})
    assert steps["text"]["state"] == "done"


async def test_a_fifo_artifact_is_invalid_without_blocking_the_status(tmp_path, session_env, monkeypatch):
    """codex r1 of the branch: a special file at an artifact path is never opened for a blocking read."""
    import os
    session, projects, owner = session_env
    _add_port(projects, "port-a")
    target = _document_catalogue(tmp_path, monkeypatch, {"whole": {"type": "integer"}})
    os.mkfifo(target)
    try:
        steps = await asyncio.wait_for(_document_steps(session), timeout=10)
    except TimeoutError:  # release a reader stuck in open() so the worker thread ends with the test
        os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        raise
    assert steps["whole"]["blocked_reason"] == {"code": "artifact_invalid"}
    assert steps["whole"]["invalid"] == [{"artifact": "restructure/port-a/document.json", "digest": None,
                                          "path": "", "message": "not a regular file"}]


@pytest.mark.parametrize("content", ["NaN", "Infinity", "-Infinity", "[1, NaN]"])
async def test_non_json_constants_make_an_artifact_invalid(tmp_path, session_env, monkeypatch, content):
    """codex r1 of the branch: Python's json accepts NaN/Infinity, JSON does not; NaN would pass any bounds."""
    session, projects, owner = session_env
    _add_port(projects, "port-a")
    target = _document_catalogue(tmp_path, monkeypatch, {"share": {
        "anyOf": [{"type": "number", "minimum": 0, "maximum": 1},
                  {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}}]}})
    target.write_text(content)
    steps = await _document_steps(session)
    assert steps["share"]["blocked_reason"] == {"code": "artifact_invalid"}
    assert steps["share"]["invalid"] == [{"artifact": "restructure/port-a/document.json",
                                          "digest": _sha(content.encode()),
                                          "path": "", "message": "the file is not UTF-8 JSON"}]


async def test_a_confirmation_linked_to_the_place_survives_a_materials_write(tmp_path, session_env):
    """W84 (spec 2026-10-02 §3.2): materials `lab` inside place `.` — a ТЗ write keeps spec.confirmed linked to the
    implementation place, a materials_root link goes digest_changed."""
    session, projects, owner = session_env
    projects.update("acq", {"op": "move_implementation", "implementation": "agent", "place": "."})
    spec = tmp_path / "work" / "lab" / "SPEC.md"
    spec.write_text("spec\n")
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "place", "ref": "implementation:agent"}], channel="cli", author="t",
                          note=None)
    await session.confirm("acq", None, claim="task.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "place", "ref": "materials_root"}], channel="cli", author="t", note=None)
    (tmp_path / "work" / "lab" / "NOTES.md").write_text("answers\n")
    _, facts = await session.facts("acq")
    by_id = {f.id: f for f in facts}
    assert by_id["spec.confirmed"].holds
    assert not by_id["task.confirmed"].holds and by_id["task.confirmed"].stale_reason.codes == ("digest_changed",)


async def test_status_digests_match_the_confirmation_digest(tmp_path, session_env):
    session, projects, owner = session_env
    spec = tmp_path / "work" / "lab" / "SPEC.md"
    spec.write_text("spec\n")
    steps = {s["id"]: s for s in (await session.status_payload("acq", "agent", "connect"))["scenarios"][0]["steps"]}
    shown = steps["spec"]["sources"]
    assert shown == [{"artifact": "SPEC.md", "digest": _sha(b"spec\n")}]
    done = await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md", "digest": shown[0]["digest"]},
                                 linked=[], channel="cli", author="t", note=None)
    assert done.attests["digest"] == shown[0]["digest"]


async def test_a_replaced_artifact_refuses_a_confirmation_with_the_shown_digest(tmp_path, session_env):
    from evolution_lab.projects.errors import ProjectError
    session, projects, owner = session_env
    spec = tmp_path / "work" / "lab" / "SPEC.md"
    spec.write_text("spec\n")
    steps = {s["id"]: s for s in (await session.status_payload("acq", "agent", "connect"))["scenarios"][0]["steps"]}
    shown = steps["spec"]["sources"][0]["digest"]
    spec.write_text("spec, replaced\n")
    again = {s["id"]: s for s in (await session.status_payload("acq", "agent", "connect"))["scenarios"][0]["steps"]}
    assert again["spec"]["sources"][0]["digest"] == _sha(b"spec, replaced\n") != shown
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md", "digest": shown},
                              linked=[], channel="cli", author="t", note=None)
    assert caught.value.code == "digest_mismatch"


async def test_one_path_is_observed_once_per_status(tmp_path, session_env, monkeypatch):
    """codex r1 of the dev38 spec: an `exists` output and a schema output on one path share one observation."""
    import copy
    from evolution_lab.projects import session as session_module
    session, projects, owner = session_env
    _add_port(projects, "port-a")
    target = _document_catalogue(tmp_path, monkeypatch, {"whole": {"type": "object"}})
    from evolution_lab import guides
    catalogue = guides.scenarios()
    descriptor = catalogue.descriptor("restructure")
    descriptor["steps"].append({**copy.deepcopy(descriptor["steps"][0]), "id": "seen", "title": "seen",
                                "outputs": [{"artifact": {"path": "restructure/{implementation}/document.json",
                                                          "check": "exists"}}]})
    target.write_text("{}")
    calls = []
    real = session_module.observe_file
    monkeypatch.setattr(session_module, "observe_file", lambda file, **kw: calls.append(str(file)) or real(file, **kw))
    steps = await _document_steps(session)
    assert calls.count(str(target)) == 1
    assert steps["whole"]["sources"] == steps["seen"]["sources"] == [
        {"artifact": "restructure/port-a/document.json", "digest": _sha(b"{}")}]


async def test_two_invalid_files_in_one_step_are_attributed(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    catalogue = _restructure_catalogue(tmp_path, monkeypatch)
    descriptor = catalogue.descriptor("restructure")
    descriptor["steps"][0]["outputs"].append({"artifact": {
        "path": "restructure/{implementation}/second.json", "check": "schema", "schema": "behavior-map/1"}})
    _add_port(projects, "port-a")
    root = tmp_path / "work" / "lab" / "restructure" / "port-a"
    root.mkdir(parents=True)
    bad = json.dumps({"schema": "restructure-behavior-map/1", "behaviors": [{"id": ""}]}).encode()
    (root / "behavior-map.json").write_bytes(bad)
    (root / "second.json").write_bytes(b"\xff")
    step = {s["id"]: s for s in (await session.status_payload("acq", "port-a"))["scenarios"][0]["steps"]}["map"]
    assert [(r["artifact"], r["digest"]) for r in step["invalid"]] == [
        ("restructure/port-a/behavior-map.json", _sha(bad))] * 2 + [("restructure/port-a/second.json", _sha(b"\xff"))]


async def test_a_socket_at_an_exists_path_is_not_done(tmp_path, session_env):
    import socket
    session, projects, owner = session_env
    path = tmp_path / "work" / "lab" / "SPEC.md"
    server = socket.socket(socket.AF_UNIX)
    try:
        try:
            server.bind(str(path))
        except OSError:
            pytest.skip("socket path too long")
        steps = {s["id"]: s for s in (await session.status_payload("acq", "agent", "connect"))["scenarios"][0]["steps"]}
        assert steps["spec"]["state"] == "ready" and steps["spec"]["sources"] == []
    finally:
        server.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
async def test_an_unreadable_excluded_exists_artifact_is_done_without_a_digest(tmp_path, session_env):
    """spec 2026-10-02 §2.1: `unreadable` is reachable for a path the revision skips (`project.exclude`)."""
    session, projects, owner = session_env
    projects.update("acq", {"op": "set_exclude", "exclude": ["lab/SPEC.md"]})
    path = tmp_path / "work" / "lab" / "SPEC.md"
    path.write_text("spec\n")
    from evolution_lab import guides
    from evolution_lab.guides import answers
    (path.parent / "spec.answers.json").write_text(
        json.dumps(answers.complete_answers(guides.requirements(), implementation="agent")), encoding="utf-8")
    path.chmod(0)
    try:
        steps = {s["id"]: s for s in (await session.status_payload("acq", "agent", "connect"))["scenarios"][0]["steps"]}
        assert steps["spec"]["state"] == "done"
        assert steps["spec"]["sources"][0] == {"artifact": "SPEC.md", "digest": None}
        assert [x["artifact"] for x in steps["spec"]["sources"]] == ["SPEC.md", "spec.answers.json"]
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
async def test_an_unreadable_excluded_schema_artifact_is_invalid_without_a_digest(tmp_path, session_env, monkeypatch):
    session, projects, owner = session_env
    _add_port(projects, "port-a")
    target = _document_catalogue(tmp_path, monkeypatch, {"whole": {"type": "object"}})
    projects.update("acq", {"op": "set_exclude", "exclude": ["lab/restructure/**"]})
    target.write_text("{}")
    target.chmod(0)
    try:
        steps = await _document_steps(session)
        assert steps["whole"]["invalid"] == [{"artifact": "restructure/port-a/document.json", "digest": None,
                                              "path": "", "message": "the file cannot be read"}]
    finally:
        target.chmod(0o600)


async def test_spec_answers_are_checked_against_the_shipped_schema(tmp_path, session_env):
    session, projects, owner = session_env
    materials = projects.root_path(projects.get("acq"))
    (materials / "SPEC.md").write_text("# spec\n", encoding="utf-8")
    from evolution_lab import guides
    from evolution_lab.guides import answers
    document = answers.complete_answers(guides.requirements(), implementation="agent")
    del document["items"]["agent.config"]
    (materials / "spec.answers.json").write_text(json.dumps(document), encoding="utf-8")
    spec = {s["id"]: s for s in (await session.status_payload("acq", scenario="connect"))["scenarios"][0]["steps"]}["spec"]
    assert spec["state"] == "blocked" and spec["blocked_reason"] == {"code": "artifact_invalid"}
    assert [(r["artifact"], r["path"]) for r in spec["invalid"]] == [("spec.answers.json", "/items")]
    (materials / "spec.answers.json").write_text("[]", encoding="utf-8")
    spec = {s["id"]: s for s in (await session.status_payload("acq", scenario="connect"))["scenarios"][0]["steps"]}["spec"]
    assert spec["state"] == "blocked" and spec["invalid"][0]["path"] == ""
    empty = answers.complete_answers(guides.requirements(), implementation="agent")
    empty["items"] = {}
    (materials / "spec.answers.json").write_text(json.dumps(empty), encoding="utf-8")
    spec = {s["id"]: s for s in (await session.status_payload("acq", scenario="connect"))["scenarios"][0]["steps"]}["spec"]
    assert spec["state"] == "blocked" and len(spec["invalid"]) == 5
    assert {r["path"] for r in spec["invalid"]} == {"/items"}
    assert all("'" in r["message"] and "is a required property" in r["message"] for r in spec["invalid"])
    old_digest = answers.complete_answers(guides.requirements(), implementation="agent")
    old_digest["catalogue"]["digest"] = "0" * 64
    (materials / "spec.answers.json").write_text(json.dumps(old_digest), encoding="utf-8")
    spec = {s["id"]: s for s in (await session.status_payload("acq", scenario="connect"))["scenarios"][0]["steps"]}["spec"]
    assert spec["state"] == "blocked" and "/catalogue/digest" in [r["path"] for r in spec["invalid"]]
    (materials / "spec.answers.json").write_text(
        json.dumps(answers.complete_answers(guides.requirements(), implementation="agent")), encoding="utf-8")
    spec = {s["id"]: s for s in (await session.status_payload("acq", scenario="connect"))["scenarios"][0]["steps"]}["spec"]
    assert spec["state"] == "done"


async def test_project_prepare_3_records_prepared_link(session_env):
    from evolution_lab.service.project_codecs import decode_project_prepare

    session, projects, _owner = session_env
    bundle, _ = await session.bundle("acq")
    implementation, value = decode_project_prepare({
        "schema": "lab-service-project-prepare-request/3", "implementation": None,
        "kind": "campaign", "bundle_digest": bundle.digest,
        "stop_grace_seconds": None, "cleanup_wait_seconds": None,
    })
    prepared, link = await session.prepare("acq", implementation, bundle_digest=value.bundle_digest,
                                           kind=value.kind, params=value.kwargs)
    stored = projects.store.link_for("prepared", prepared.digest, PAIR, session.state_root)
    assert stored is not None
    assert stored.object_id == link.object_id == prepared.digest and stored.bundle_digest == bundle.digest
