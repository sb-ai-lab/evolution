"""Final review F9: ``ProjectSession`` probes over the REAL Owner and a real entry target (spec 2026-10-02 §3.3, §3.5).

The projects-level tests use a FakeOwner; this module pins the outcomes the real Owner gives for a turn timeout,
a definitive protocol error and a restart after an Owner that died with an open session."""
from __future__ import annotations

import asyncio

import pytest

from test_owner_probe_bundle import _TOKEN, make_probe_bundle

from evolution_lab.owner import Owner, OwnerError, TargetRef
from evolution_lab.projects.entry import DEFAULT_LIMITS
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.session import ProjectSession, client_ref

pytestmark = pytest.mark.slow

PAIR = Pair("0.0.3.dev37", "0.3.0a28")


def _projects(tmp_path) -> Projects:
    work = tmp_path / "work"
    (work / "im").mkdir(parents=True)
    (work / "lab").mkdir()
    projects = Projects.open(tmp_path / "home")
    projects.create(id="pr", name="P", materials_root="lab", path=str(work),
                    implementations=[{"id": "im", "name": "I", "place": "im"}])
    return projects


async def _target(owner, digest):
    """The pair's ready target, labelled for (pr, im) as the project route starts it."""
    connection = await owner.bind(digest)
    task = await owner.start_target(connection, secrets=_TOKEN, client_ref=client_ref("pr", "im"))
    status = await asyncio.wait_for(owner.wait(task), 12)
    assert status.state == "succeeded", status.to_payload()
    return connection, TargetRef(status.ref.id, owner.owner_generation)


async def _stop(owner, target):
    assert (await owner.wait(await owner.stop_target(target))).state == "succeeded"


def test_turn_timeout_is_release_failed_and_owner_stops_target(tmp_path, monkeypatch):
    state, digest = make_probe_bundle(tmp_path, mode="sleep:3")
    projects = _projects(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target = await _target(owner, digest)
            stop_gate = asyncio.Event()
            stop = owner._stop_target_runtime

            async def gated_stop(*args, **kwargs):
                await stop_gate.wait()
                return await stop(*args, **kwargs)

            monkeypatch.setattr(owner, "_stop_target_runtime", gated_stop)
            session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=state)
            try:
                s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None,
                                              turn_timeout_seconds=0.5)
                t = await session.probe_turn(s["id"], "slow")
                assert t["failure"] == {"code": "turn_timeout"} and t["outcome"] == "target_failed"
                h = await session.probe_session(s["id"])
                assert h["state"] == "closed" and h["closed_reason"] == "release_failed"
                for call in (owner.check(connection, target_ref=target),
                             session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)):
                    with pytest.raises(OwnerError) as caught:
                        await call
                    assert caught.value.code == "cleanup_required"
                stop_gate.set()
                await asyncio.wait_for(asyncio.shield(owner._targets[target.id].ended), 3)
                assert (await owner.target(target)).state == "stopped"
                stops = [entry for entry in owner._tasks.values() if entry.action == "target_stop"]
                assert len(stops) == 1
                assert (await asyncio.wait_for(owner.wait(stops[0].ref), 3)).state == "succeeded"
                assert not (await owner.health()).blockers
                assert (await session.probe_session(s["id"]))["closed_reason"] == "release_failed"
                _, restarted = await _target(owner, digest)
                assert restarted.id != target.id
                again = await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
                assert again["target"]["id"] == restarted.id
                await session.probe_close(again["id"])
                await _stop(owner, restarted)
            finally:
                stop_gate.set()
                session.close()

    try:
        asyncio.run(scenario())
    finally:
        projects.close()


def test_an_uncertain_session_quarantines_the_other_open_sessions_on_its_target(tmp_path, monkeypatch):
    """ADR-0021/dev58: A's timed-out turn aborts the target; B sends nothing more, becomes uncertain before
    stop confirmation, and its history closes release_failed when reconciled during that interval."""
    state, digest = make_probe_bundle(tmp_path, mode="sleep:3", max_instances=2)
    projects = _projects(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target = await _target(owner, digest)
            stop_gate, stopping = asyncio.Event(), asyncio.Event()
            stop = owner._stop_target_runtime

            async def gated_stop(*args, **kwargs):
                stopping.set()
                await stop_gate.wait()
                return await stop(*args, **kwargs)

            monkeypatch.setattr(owner, "_stop_target_runtime", gated_stop)
            session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=state)
            try:
                a, b = [await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None,
                                                  turn_timeout_seconds=0.5) for _ in range(2)]
                instance = owner._probes[b["id"]].instance
                sends, send = [], instance.send

                async def counted(*args, **kwargs):
                    sends.append(args)
                    return await send(*args, **kwargs)
                instance.send = counted
                assert (await session.probe_turn(a["id"], "slow"))["failure"] == {"code": "turn_timeout"}
                await asyncio.wait_for(stopping.wait(), 1)
                assert (await session.probe_session(a["id"]))["closed_reason"] == "release_failed"
                with pytest.raises(ProjectError) as caught:
                    await session.probe_turn(b["id"], "after")
                assert caught.value.code == "probe_session_closed"
                assert sends == []  # nothing reached the quarantined target
                h = await session.probe_session(b["id"])
                assert h["state"] == "closed" and h["closed_reason"] == "release_failed"
                assert h["turns"] == [] and h["ceiling"]["turns"]["used"] == 0
                assert session._probes().pending_turns() == []  # the refused turn is not a turn
                assert (await owner.probe_states())[b["id"]] == "uncertain"
                stop_gate.set()
                await asyncio.wait_for(asyncio.shield(owner._targets[target.id].ended), 3)
                assert (await owner.probe_states())[b["id"]] == "released"
                h = await session.probe_session(b["id"])
                assert h["state"] == "closed" and h["closed_reason"] == "release_failed"
            finally:
                stop_gate.set()
                session.close()

    try:
        asyncio.run(scenario())
    finally:
        projects.close()


def test_protocol_error_is_turn_failed_and_the_target_stays_usable(tmp_path):
    state, digest = make_probe_bundle(tmp_path)
    projects = _projects(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target = await _target(owner, digest)
            session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=state)
            try:
                s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
                # Above the entry target's max_input_bytes: the target refuses the request (413).
                t = await session.probe_turn(s["id"], "x" * (DEFAULT_LIMITS["max_input_bytes"] + 1024))
                assert t["failure"] == {"code": "protocol_error"} and t["answer"] is None
                h = await session.probe_session(s["id"])
                assert h["state"] == "closed" and h["closed_reason"] == "turn_failed"
                checked = await owner.wait(await owner.check(connection, target_ref=target))
                assert checked.state == "succeeded", checked.to_payload()
                again = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
                assert again["reset_of"] is None
                assert (await session.probe_turn(again["id"], "hi"))["answer"] == "echo:hi"
                assert (await session.probe_close(again["id"]))["closed_reason"] == "closed"
                await _stop(owner, target)
                assert not (await owner.health()).blockers
            finally:
                session.close()

    try:
        asyncio.run(scenario())
    finally:
        projects.close()


def test_restart_after_an_owner_died_with_an_open_session_is_target_gone(tmp_path, monkeypatch):
    from evolution_lab.owner.api import Owner as OwnerClass
    from evolution_lab.owner.store import OwnerStateStore

    state, digest = make_probe_bundle(tmp_path)
    projects = _projects(tmp_path)

    async def crash():
        # An Owner that dies with an open probe: neither its close nor the target stop releases the row.
        async def no_close(self, probe):
            return None

        monkeypatch.setattr(OwnerClass, "_shut_probe", no_close)
        monkeypatch.setattr(OwnerStateStore, "release_probes_of_target", lambda self, target_id: None)
        monkeypatch.setattr(OwnerStateStore, "_release_probe_rows", staticmethod(lambda database, target_id: None))
        async with Owner.open(state) as owner:
            await _target(owner, digest)
            session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=state)
            try:
                s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
                assert (await session.probe_turn(s["id"], "hi"))["answer"] == "echo:hi"
            finally:
                session.close()
        monkeypatch.undo()
        return s

    async def restart(s):
        async with Owner.open(state) as owner:
            assert owner.owner_generation != s["target"]["owner_generation"]
            assert (await owner.probe_states())[s["id"]] == "uncertain"  # an earlier generation's row
            session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=state)
            try:
                await session.probe_reconcile()  # what the service does at start
                h = await session.probe_session(s["id"])
            finally:
                session.close()
        assert h["state"] == "closed" and h["closed_reason"] == "target_gone"
        assert [turn["answer"] for turn in h["turns"]] == ["echo:hi"]  # the history stays readable

    try:
        asyncio.run(restart(asyncio.run(crash())))
    finally:
        projects.close()
