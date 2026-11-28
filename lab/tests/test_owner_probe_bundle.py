"""dev37 Task 4: the Owner accepts a ``probe`` connection; operations refuse it (spec 2026-10-02 §2.4, §3.5)."""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from ports import free_port

_TOKEN = {"TARGET_TOKEN": "private-probe-token"}


def make_probe_bundle(tmp_path: Path, *, mode: str = "normal", max_instances: int = 1,
                      stop_grace_seconds: float = 0.2) -> tuple[Path, str]:
    """A ``probe`` Bundle from a place with an entry file and an echo agent (``TARGET_MODE`` = ``mode``);
    returns (state, digest). Its target has the entry's default limits but ``max_instances`` (1 by default)."""
    from evolution_lab.preparation import EnvironmentInput, PreparationStore, ProbeSource, SourceRoot
    from evolution_lab.projects.entry import ENTRY_FILE, probe_launch, read_target_entry

    place = tmp_path / "impl"
    place.mkdir()
    shutil.copytree(Path(__file__).parent / "owner_fixtures" / "echo_agent", place / "agent")
    (place / ENTRY_FILE).write_text(json.dumps({
        "schema": "lab-target-entry/1",
        "agent": {"module": "echo_agent", "attribute": "build", "import_root": "agent"},
        "program_paths": ["agent"], "environment": {"kind": "service"},
        "env": {"TARGET_MODE": mode}, "secret_names": ["TARGET_TOKEN"],
        "limits": {"max_instances": max_instances},
        "startup_timeout_seconds": 8, "stop_grace_seconds": stop_grace_seconds,
    }), encoding="utf-8")
    port = free_port()
    entry = read_target_entry(place)
    launch = probe_launch(entry, target_id="lab-probe-p-i", base_url=f"http://127.0.0.1:{port}")
    state = tmp_path / "state"
    summary = PreparationStore(state).create_bundle(
        source=ProbeSource(place / ENTRY_FILE, launch),
        environments=[EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ())],
        env=entry.env,
        secret_names=list(entry.secret_names),
        roots=[SourceRoot("implementation", place)],
    )
    assert summary.source_kind == "probe"
    return state, summary.digest


async def ready_probe_target(owner, digest):
    from evolution_lab.owner import TargetRef

    connection = await owner.bind(digest)
    task = await owner.start_target(connection, secrets=_TOKEN)
    status = await asyncio.wait_for(owner.wait(task), 12)
    assert status.state == "succeeded", status.to_payload()
    return connection, TargetRef(status.ref.id, owner.owner_generation)


def test_probe_bundle_binds_and_its_target_becomes_ready(tmp_path):
    from evolution_lab.owner import Owner

    state, digest = make_probe_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target = await ready_probe_target(owner, digest)
            assert connection.source_kind == "probe"
            value = await owner.target(target)
            assert value.state == "ready" and value.bundle_digest == digest
            check = await owner.check(connection, target_ref=target)
            checked = await owner.wait(check)
            assert checked.state == "succeeded", checked.to_payload()
            assert checked.result.kind == "check"
            assert (await owner.wait(await owner.stop_target(target))).state == "succeeded"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


def test_operations_refuse_probe_connection(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.preparation import PreparationError, PreparationStore

    state, digest = make_probe_bundle(tmp_path)
    store = PreparationStore(state)
    with pytest.raises(PreparationError):
        store.prepare_campaign(digest)
    with pytest.raises(PreparationError):
        store.prepare_evaluation(digest, selection={"count": 1})

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target = await ready_probe_target(owner, digest)
            refusals = {
                "conformance": owner.conformance(connection, target_ref=target),
                "start": owner.start("0" * 64, target_ref=target, secrets={}),
                "resume": owner.resume(ResourceRef("operation", "op-none"), target_ref=target, secrets={}),
            }
            for name, call in refusals.items():
                with pytest.raises(OwnerError) as caught:
                    await call
                assert (caught.value.code, caught.value.stage) == ("bundle_probe_only", "before_start"), name
                assert caught.value.failure.message == "a probe Bundle only raises a target for probe sessions"
            # No task or operation was reserved by the refusals.
            assert (await owner.target(target)).state == "ready"
            assert (await owner.wait(await owner.stop_target(target))).state == "succeeded"
            assert not (await owner.health()).blockers

    asyncio.run(scenario())


def test_state_version_is_10_and_older_state_is_refused(tmp_path):
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "fresh") as store:
        assert store._database.execute("PRAGMA user_version").fetchone()[0] == 14
        columns = [(row[1], row[2], row[3])
                   for row in store._database.execute("PRAGMA table_info(probe_instances)")]
        assert columns == [
            ("session_id", "TEXT", 0), ("owner_generation", "TEXT", 1), ("target_id", "TEXT", 1),
            ("instance_id", "TEXT", 0), ("state", "TEXT", 1),
            ("created_at", "REAL", 1), ("updated_at", "REAL", 1),
        ]
        references = sorted((row[3], row[2], row[4])
                            for row in store._database.execute("PRAGMA foreign_key_list(probe_instances)"))
        assert references == [("owner_generation", "generations", "id"), ("target_id", "targets", "id")]
    old = tmp_path / "dev36"
    old.mkdir(mode=0o700)
    with sqlite3.connect(old / "owner.sqlite3") as database:
        database.execute("CREATE TABLE generations(id TEXT PRIMARY KEY)")
        database.execute("PRAGMA user_version = 4")
    (old / "owner.sqlite3").chmod(0o600)
    with pytest.raises(OwnerError) as caught:
        OwnerStateStore.open(old)
    assert caught.value.code == "unsupported_schema"


def test_probe_error_codes_have_fixed_messages_and_http_status():
    from evolution_lab.owner.models import Failure
    from evolution_lab.service.errors import _STATUS

    assert Failure.make("probe_sessions_open", "probe").message == (
        "probe sessions are open on the target; close or reset them, or stop the target")
    assert Failure.make("bundle_probe_only", "before_start").message == (
        "a probe Bundle only raises a target for probe sessions")
    assert _STATUS["probe_sessions_open"] == 409 and _STATUS["bundle_probe_only"] == 409
