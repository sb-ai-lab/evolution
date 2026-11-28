"""dev37 Task 6: Owner probe instances — admission, wire, settlement, blocking (spec 2026-10-02 §3.2–§3.5)."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
import psutil

from ownership_modes import ownership_mode  # noqa: F401

from test_owner_probe_bundle import make_probe_bundle, ready_probe_target
from test_owner_store_starts import DIGEST, _executor, _finish_normally, _ready_target
from test_owner_target import make_bundle, ready_target

pytestmark = pytest.mark.slow

_CONFORMANCE = {"kind": "core-session-public/1"}


def new_id() -> str:
    return uuid.uuid4().hex


def run(coroutine):
    return asyncio.run(coroutine)


def lose_materialize_response(monkeypatch):
    """The instance is created on the target, but its answer is lost: the core ledger records the create call
    ``uncertain`` (as the transport does for a reply that never arrived) and the adapter raises transport_error.
    Returns ``restore()``, which puts the real ``materialize`` back for later opens."""
    from evolution.tree import TargetCallLedger
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    original, io_end = HttpSessionAdapter.materialize, TargetCallLedger._io_end

    async def lossy(self, *args, **kwargs):
        await original(self, *args, **kwargs)  # the instance exists on the target ...
        raise RolloutInfrastructureError("response lost", "transport_error")  # ... the answer is lost

    def lost(self, call, outcome, reason):
        if call.operation == "create_instance" and outcome == "response":
            outcome, reason = "uncertain", "transport_error"
        return io_end(self, call, outcome, reason)

    monkeypatch.setattr(HttpSessionAdapter, "materialize", lossy)
    monkeypatch.setattr(TargetCallLedger, "_io_end", lost)

    def restore():
        monkeypatch.setattr(HttpSessionAdapter, "materialize", original)
        monkeypatch.setattr(TargetCallLedger, "_io_end", io_end)
    return restore


async def _stop(owner, target):
    assert (await owner.wait(await owner.stop_target(target))).state == "succeeded"


# --- store: one admission transaction and the blocking rule -------------------------------


def test_store_admission_blocks_target_use_and_follows_max_instances(tmp_path):
    from evolution_lab.owner import OwnerError, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first, second = new_id(), new_id()
        store.admit_probe(session_id=first, target_id=target.id, max_instances=1)
        assert store.probe_rows() == [(first, store.generation, "admitted")]
        with pytest.raises(OwnerError) as caught:
            store.admit_probe(session_id=second, target_id=target.id, max_instances=1)
        assert (caught.value.code, caught.value.stage) == ("resource_busy", "probe")
        # Every action that takes target_use refuses while the row is not released ...
        refusals = {
            "check": lambda: store.accept_task(action="check", ref=ResourceRef("target", target.id),
                                               reserve_target_use=True),
            "conformance": lambda: store.accept_task(action="conformance", ref=ResourceRef("target", target.id),
                                                     reserve_target_use=True, reserve_execution=True),
            "operation": lambda: store.accept_operation(prepared_digest=DIGEST, target_ref=target,
                                                        executor=_executor()),
        }
        for state in ("admitted", "open", "releasing"):
            if state != "admitted":
                store.set_probe_state(first, state, instance_id="i-1")
            for name, call in refusals.items():
                with pytest.raises(OwnerError) as caught:
                    call()
                assert (caught.value.code, caught.value.stage) == ("probe_sessions_open", "before_start"), name
        # ... and nothing was reserved by the refusals.
        assert set(store.reservations()) == {"target_lifetime"}
        # An uncertain row blocks with the existing cleanup_required, but not the target stop.
        store.set_probe_state(first, "uncertain")
        for name, call in refusals.items():
            with pytest.raises(OwnerError) as caught:
                call()
            assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start"), name
        # After an unsettled release only a stop: a new probe is refused even with room left.
        for limit in (1, 5):
            with pytest.raises(OwnerError) as caught:
                store.admit_probe(session_id=second, target_id=target.id, max_instances=limit)
            assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
        stop = store.accept_task(action="target_stop", ref=ResourceRef("target", target.id),
                                 reserve_target_use=True)
        assert stop.scope_id is not None
        # A settled row never leaves its terminal state through set_probe_state.
        store.set_probe_state(first, "releasing")
        assert dict((s, st) for s, _, st in store.probe_rows())[first] == "uncertain"
        store.release_probes_of_target(target.id)
        assert store.probe_rows() == [(first, store.generation, "released")]
        store.set_probe_state(first, "uncertain")
        assert store.probe_rows() == [(first, store.generation, "released")]


def test_store_target_quarantine_is_one_check_for_admissions_and_sends(tmp_path):
    """Codex r2 R2-1 (ADR-0021): an uncertain probe row or a cleanup_required target quarantines the target for
    every probe send, not only for new admissions; only a target stop lifts it."""
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first, second = new_id(), new_id()
        for session in (first, second):
            store.admit_probe(session_id=session, target_id=target.id, max_instances=3)
        store.set_probe_state(second, "open", instance_id="i-2")
        store.check_probe_target(target.id)  # no uncertain row: the open session may send
        store.set_probe_state(first, "uncertain")
        with pytest.raises(OwnerError) as caught:
            store.check_probe_target(target.id)
        assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
        store.release_probes_of_target(target.id)  # what a confirmed target stop does
        store.check_probe_target(target.id)
        store.mark_target_cleanup_required(target.id)
        with pytest.raises(OwnerError) as caught:
            store.check_probe_target(target.id)
        assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")


def test_store_resume_refuses_while_a_probe_row_is_open(tmp_path):
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        session = new_id()
        store.admit_probe(session_id=session, target_id=target.id, max_instances=2)
        with pytest.raises(OwnerError) as caught:
            store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert (caught.value.code, caught.value.stage) == ("probe_sessions_open", "before_start")
        store.set_probe_state(session, "releasing")
        store.set_probe_state(session, "released")
        assert store.accept_resume(first.status.id, target_ref=target, executor=_executor()).status.id \
            == first.status.id


def test_store_admission_needs_a_ready_target_of_this_generation(tmp_path):
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        connection = store.bind_connection(bundle_digest="0" * 64, launch_digest="1" * 64,
                                           source_kind="campaign")
        starting = store.accept_target_start(connection_id=connection.id, launch_digest="1" * 64,
                                             bundle_digest="0" * 64, base_url="http://127.0.0.1:8123")
        for target_id in (starting.target_ref.id, "f" * 32):
            with pytest.raises(OwnerError) as caught:
                store.admit_probe(session_id=new_id(), target_id=target_id, max_instances=1)
            assert (caught.value.code, caught.value.stage) == ("target_not_running", "probe")
        assert store.probe_rows() == []


def test_store_new_generation_marks_unreleased_rows_uncertain(tmp_path):
    from evolution_lab.owner.store import OwnerStateStore

    state = tmp_path / "state"
    sessions = [new_id() for _ in range(3)]
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        for session in sessions:
            store.admit_probe(session_id=session, target_id=target.id, max_instances=3)
        store.set_probe_state(sessions[1], "open", instance_id="i-1")
        store.set_probe_state(sessions[2], "releasing")
        store.set_probe_state(sessions[2], "released")
        old = store.generation
    with OwnerStateStore.open(state) as store:
        rows = {session: (generation, value) for session, generation, value in store.probe_rows()}
        assert rows == {sessions[0]: (old, "uncertain"), sessions[1]: (old, "uncertain"),
                        sessions[2]: (old, "released")}


# --- Owner over a real target ----------------------------------------------------------------


@pytest.mark.usefixtures('ownership_mode')
def test_probe_open_seed_follows_target_mode(tmp_path):
    from evolution_lab.owner import Owner, OwnerError

    exact_root, none_root = tmp_path / "exact", tmp_path / "none"
    exact_root.mkdir()
    none_root.mkdir()
    state, bundle, _ = make_bundle(exact_root)

    async def exact():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert opened.seed_mode == "exact"
            assert type(opened.requested_seed) is int and opened.requested_seed == opened.effective_seed
            assert opened.target_id == target.id and opened.owner_generation == owner.owner_generation
            status = await owner.target(target)
            assert (opened.boot_id, opened.bundle_digest, opened.launch_digest) == (
                status.boot_id, status.bundle_digest, status.launch_digest)
            assert opened.source_kind == "campaign"
            assert opened.instance_id.startswith(f"{opened.session_id}-")
            assert (await owner.probe_states())[opened.session_id] == "open"
            assert await owner.probe_close(opened.session_id) == "released"
            chosen = await owner.probe_open(target, session_id=new_id(), seed=7)
            assert (chosen.requested_seed, chosen.effective_seed) == (7, 7)
            assert await owner.probe_close(chosen.session_id) == "released"
            await _stop(owner, target)

    run(exact())
    state, digest = make_probe_bundle(none_root)

    async def none():
        async with Owner.open(state) as owner:
            _, target = await ready_probe_target(owner, digest)
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=new_id(), seed=5)
            assert (caught.value.code, caught.value.stage) == ("invalid_argument", "probe")
            assert await owner.probe_states() == {}
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert opened.seed_mode == "none" and opened.source_kind == "probe"
            assert (opened.requested_seed, opened.effective_seed) == (None, None)
            assert await owner.probe_close(opened.session_id) == "released"
            await _stop(owner, target)

    run(none())


@pytest.mark.usefixtures('ownership_mode')
def test_probe_turn_returns_core_turn_result(tmp_path):
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            for text in ("привет", "ещё раз"):
                out = await owner.probe_turn(opened.session_id, text, timeout=30)
                assert out.failure is None and out.closed is None
                assert out.result.outcome == "ok"
                assert out.result.events[-1]["type"] == "assistant_message"
            assert await owner.probe_close(opened.session_id) == "released"
            # Idempotent close; a turn on a closed session is refused.
            assert await owner.probe_close(opened.session_id) == "released"
            with pytest.raises(OwnerError) as caught:
                await owner.probe_turn(opened.session_id, "x", timeout=30)
            assert (caught.value.code, caught.value.stage) == ("not_found", "probe")
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_admitted_row_blocks_target_use_before_open_returns(tmp_path, monkeypatch):
    from evolution.tree.target.http_adapter import HttpSessionAdapter

    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path, conformance=_CONFORMANCE)
    original = HttpSessionAdapter.materialize

    async def scenario():
        gate, reached = asyncio.Event(), asyncio.Event()

        async def gated(self, *args, **kwargs):
            reached.set()
            await gate.wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(HttpSessionAdapter, "materialize", gated)
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            session = new_id()
            opening = asyncio.create_task(owner.probe_open(target, session_id=session, seed=None))
            await asyncio.wait_for(reached.wait(), 10)
            assert (await owner.probe_states())[session] == "admitted"
            for name, call in (("check", owner.check(connection, target_ref=target)),
                               ("conformance", owner.conformance(connection, target_ref=target))):
                with pytest.raises(OwnerError) as caught:
                    await call
                assert (caught.value.code, caught.value.stage) == ("probe_sessions_open", "before_start"), name
            gate.set()
            opened = await asyncio.wait_for(opening, 10)
            assert (await owner.probe_states())[session] == "open"
            with pytest.raises(OwnerError) as caught:
                await owner.check(connection, target_ref=target)
            assert caught.value.code == "probe_sessions_open"
            assert await owner.probe_close(opened.session_id) == "released"
            checked = await owner.wait(await owner.check(connection, target_ref=target))
            assert checked.state == "succeeded", checked.to_payload()
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_close_releases_on_positive_settlement_and_unblocks(tmp_path):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert (await owner.probe_turn(opened.session_id, "hi", timeout=30)).failure is None
            state_after = await owner.probe_close(opened.session_id)
            assert state_after == "released"
            # A released session's snapshot is discarded.
            assert not (state / "artifacts" / f"probe-{opened.session_id}").exists()
            assert (await owner.probe_states())[opened.session_id] == "released"
            checked = await owner.wait(await owner.check(connection, target_ref=target))
            assert checked.state == "succeeded", checked.to_payload()
            await _stop(owner, target)
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_lost_materialize_response_is_uncertain_and_blocks_with_cleanup_required(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            restore = lose_materialize_response(monkeypatch)
            session = new_id()
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=session, seed=None)
            assert (caught.value.code, caught.value.stage) == ("target_unavailable", "probe")
            restore()
            assert (await owner.probe_states())[session] == "uncertain"
            # An uncertain session keeps its snapshot; no new probe until the target stops.
            assert (state / "artifacts" / f"probe-{session}").is_dir()
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=new_id(), seed=None)
            assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
            assert await owner.probe_close(session) == "uncertain"
            with pytest.raises(OwnerError) as caught:
                await owner.check(connection, target_ref=target)
            assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
            await _stop(owner, target)
            assert (await owner.probe_states())[session] == "released"
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_stop_target_closes_probes_before_admission(tmp_path):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, max_instances=2)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.probe_open(target, session_id=new_id(), seed=None)
            second = await owner.probe_open(target, session_id=new_id(), seed=None)
            await _stop(owner, target)
            states = await owner.probe_states()
            assert states[first.session_id] == states[second.session_id] == "released"
            assert await owner.probe_close(first.session_id) == "released"
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_owner_close_closes_probes_before_stopping_targets(tmp_path):
    from evolution_lab.owner.store import OwnerStateStore

    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
        assert (await owner.close()).cleanup.complete is True
        return opened

    opened = run(scenario())
    with OwnerStateStore.open(state) as store:
        assert [(s, v) for s, _, v in store.probe_rows()] == [(opened.session_id, "released")]


@pytest.mark.usefixtures('ownership_mode')
def test_owner_close_bounds_probe_cleanup_by_its_deadline(tmp_path, monkeypatch):
    """Codex r1 A3: a probe close slower than the shutdown budget does not hold the Owner close; the row stays
    uncertain (never released) and the target stop is still requested."""
    import time

    from evolution_lab.owner.store import OwnerStateStore
    from evolution_lab.owner.target import TargetRuntime

    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="close_sleep:20",
                                   stop_grace_seconds=15)
    stops = []
    request_stop = TargetRuntime.request_stop

    async def recorded(self):
        stops.append(self.ref.id)
        return await request_stop(self)
    monkeypatch.setattr(TargetRuntime, "request_stop", recorded)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            started = time.monotonic()
            await owner.close(wait_seconds=1)
            elapsed = time.monotonic() - started
        return target, opened, elapsed

    target, opened, elapsed = run(scenario())
    assert elapsed < 6, elapsed
    assert target.id in stops
    with OwnerStateStore.open(state) as store:
        assert [(s, v) for s, _, v in store.probe_rows()] == [(opened.session_id, "uncertain")]


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("cancel_send", [False, True])
def test_turn_timeout_returns_within_timeout_and_aborts_target(tmp_path, monkeypatch, cancel_send):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="sleep:4")

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            runtime, probe = owner._targets[target.id], owner._probes[opened.session_id]
            pids = [process.pid for process in psutil.Process(runtime.helper.process.pid).children()]
            assert pids
            stop_gate = asyncio.Event()
            stop = owner._stop_target_runtime
            settle = probe.ledger.settle

            async def gated_stop(*args, **kwargs):
                await stop_gate.wait()
                return await stop(*args, **kwargs)

            async def forbidden(*args, **kwargs):
                pytest.fail("an interrupted turn must not close/release")

            async def background_settle(*args, **kwargs):
                assert asyncio.current_task() is not probe.settled, "the reply must not await ledger settlement"
                return await settle(*args, **kwargs)

            monkeypatch.setattr(owner, "_stop_target_runtime", gated_stop)
            monkeypatch.setattr(probe.instance, "close", forbidden)
            monkeypatch.setattr(probe.adapter, "release", forbidden)
            monkeypatch.setattr(probe.ledger, "settle", background_settle)
            started = time.monotonic()
            try:
                turn = asyncio.create_task(owner.probe_turn(opened.session_id, "x", timeout=0.2))
                if cancel_send:
                    await asyncio.sleep(.05)
                    assert probe.turn is not None
                    probe.turn.cancel()
                out = await asyncio.wait_for(turn, 1.2)
                assert time.monotonic() - started < 1.2
                assert out.result is None and out.failure == ("target_unreachable" if cancel_send else "turn_timeout")
                assert out.closed == "uncertain"
                assert (await owner.probe_states())[opened.session_id] == "uncertain"
                assert probe.ledger.report().admission_closed
                assert (state / "artifacts" / f"probe-{opened.session_id}").is_dir()
            finally:
                stop_gate.set()
            await asyncio.wait_for(asyncio.shield(runtime.ended), 2)
            status = await owner.target(target)
            assert status.state == "stopped" and status.cleanup.complete is True
            # A late admitted stop must not undo the watcher's confirmed stopped state.
            assert await owner._stop_target_runtime(target, runtime, wait_seconds=.2) == status
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert (await owner.probe_states())[opened.session_id] == "released"
            async with asyncio.timeout(5):
                while (state / "artifacts" / f"probe-{opened.session_id}").exists():
                    await asyncio.sleep(.01)
            assert [entry.action for entry in owner._tasks.values()].count("target_stop") == 1

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("materialized,watcher_first", [(False, False), (True, False), (True, True)])
def test_abort_does_not_wait_for_another_opening_session(tmp_path, monkeypatch, materialized, watcher_first):
    from evolution.tree.target.http_adapter import HttpSessionAdapter

    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="sleep:4", max_instances=2)
    materialize = HttpSessionAdapter.materialize

    async def scenario():
        gate, reached, stopping, stop_gate = [asyncio.Event() for _ in range(4)]

        async def blocked(self, *args, **kwargs):
            instance = await materialize(self, *args, **kwargs) if materialized else None
            reached.set()
            await gate.wait()
            return instance if materialized else await materialize(self, *args, **kwargs)

        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.probe_open(target, session_id=new_id(), seed=None)
            turning, watching = asyncio.Event(), asyncio.Event()
            close = owner._close_probes_for

            async def recorded_close(target_id, *, deadline=None):
                if deadline is None:
                    watching.set()
                return await close(target_id, deadline=deadline)

            async def blocked_send(*args, **kwargs):
                turning.set()
                await asyncio.Event().wait()

            if watcher_first:
                monkeypatch.setattr(owner, "_close_probes_for", recorded_close)
                monkeypatch.setattr(owner._probes[first.session_id].instance, "send", blocked_send)
            monkeypatch.setattr(HttpSessionAdapter, "materialize", blocked)
            second = new_id()
            opening = asyncio.create_task(owner.probe_open(target, session_id=second, seed=None))
            await asyncio.wait_for(reached.wait(), 5)
            stop = owner._stop_target_runtime

            async def gated_stop(*args, **kwargs):
                stopping.set()
                await stop_gate.wait()
                return await stop(*args, **kwargs)

            monkeypatch.setattr(owner, "_stop_target_runtime", gated_stop)
            try:
                turn = asyncio.create_task(owner.probe_turn(first.session_id, "x", timeout=5 if watcher_first else .2))
                if watcher_first:
                    await asyncio.wait_for(turning.wait(), 1)
                    await owner._targets[target.id].request_stop()
                    await asyncio.wait_for(watching.wait(), 2)
                out = await asyncio.wait_for(turn, 1.2)
                assert (out.failure, out.closed) == ("target_unreachable" if watcher_first else "turn_timeout", "uncertain")
                if not watcher_first:
                    await asyncio.wait_for(stopping.wait(), 1)
                    assert (await owner.probe_states())[second] == "uncertain"
                    assert (state / "artifacts" / f"probe-{second}").is_dir()
                with pytest.raises(OwnerError) as caught:
                    await asyncio.wait_for(asyncio.shield(opening), 1.2)
                assert caught.value.code == "target_not_running"
                stop_gate.set()
                await asyncio.wait_for(asyncio.shield(owner._targets[target.id].ended), 2)
                assert (await owner.target(target)).state == "stopped"
                assert opening.done()  # outstanding opening work was withdrawn, not left to reconnect
            finally:
                gate.set()
                stop_gate.set()
                with pytest.raises(OwnerError) as caught:
                    await asyncio.wait_for(opening, 5)
                assert caught.value.code == "target_not_running"
            assert (await owner.probe_states())[second] == "released"
            async with asyncio.timeout(5):
                while second in owner._probes or (state / "artifacts" / f"probe-{second}").exists():
                    await asyncio.sleep(.01)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_unconfirmed_abort_leaves_target_cleanup_required(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="sleep:4")

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            runtime = owner._targets[target.id]
            stop, request_stop = owner._stop_target_runtime, runtime.request_stop

            async def ignored():
                pass  # the fake process does not observe the stop request during this test's stop wait

            async def bounded(ref, target_runtime, *, wait_seconds):
                return await stop(ref, target_runtime, wait_seconds=.2)

            monkeypatch.setattr(runtime, "request_stop", ignored)
            monkeypatch.setattr(owner, "_stop_target_runtime", bounded)
            try:
                started = time.monotonic()
                out = await asyncio.wait_for(owner.probe_turn(opened.session_id, "x", timeout=.2), 1.2)
                assert time.monotonic() - started < 1.2
                assert (out.failure, out.closed) == ("turn_timeout", "uncertain")
                async with asyncio.timeout(2):
                    while (await owner.target(target)).state != "cleanup_required":
                        await asyncio.sleep(.01)
                assert runtime.helper.process.poll() is None
                assert (await owner.target(target)).cleanup.complete is not True
                assert (await owner.probe_states())[opened.session_id] == "uncertain"
                assert (state / "artifacts" / f"probe-{opened.session_id}").is_dir()
                stops = [entry for entry in owner._tasks.values() if entry.action == "target_stop"]
                assert len(stops) == 1 and (await owner.wait(stops[0].ref)).state == "failed"
            finally:
                monkeypatch.setattr(runtime, "request_stop", request_stop)
                monkeypatch.setattr(owner, "_stop_target_runtime", stop)
                # The ordinary stop entry can retry this still-live, quarantined probe target.
                await _stop(owner, target)
                await asyncio.wait_for(asyncio.shield(runtime.ended), 5)
            assert (await owner.probe_states())[opened.session_id] == "released"

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_normal_turn_does_not_stop_target(tmp_path):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            out = await owner.probe_turn(opened.session_id, "hi", timeout=.2)
            assert out.failure is None and out.closed is None and out.result.outcome == "ok"
            assert (await owner.target(target)).state == "ready"
            assert all(entry.action != "target_stop" for entry in owner._tasks.values())
            assert await owner.probe_close(opened.session_id) == "released"
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_cancelled_child_before_shut_probe_stays_forced(tmp_path, monkeypatch):
    """F1: a done/cancelled child is still an outstanding interrupted turn, not a normal close."""
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            probe = owner._probes[opened.session_id]
            stop_gate, closing = asyncio.Event(), asyncio.Event()
            calls = []
            stop = owner._stop_target_runtime

            async def gated_stop(*args, **kwargs):
                await stop_gate.wait()
                return await stop(*args, **kwargs)

            async def forbidden(*args, **kwargs):
                calls.append("wire cleanup")
                raise AssertionError("interrupted turns must not run normal cleanup")

            async def shut():
                assert probe.turn.done() and probe.turn.cancelled()
                closing.set()
                await owner._shut_probe(probe)

            async def cancelled(*args, **kwargs):
                owner._hold(shut())  # queued before the child's completion wakes _probe_turn
                raise asyncio.CancelledError

            monkeypatch.setattr(probe.instance, "send", cancelled)
            monkeypatch.setattr(probe.instance, "close", forbidden)
            monkeypatch.setattr(probe.adapter, "release", forbidden)
            monkeypatch.setattr(owner, "_stop_target_runtime", gated_stop)
            try:
                out = await asyncio.wait_for(owner.probe_turn(opened.session_id, "x", timeout=.2), 1.2)
                await asyncio.wait_for(closing.wait(), 1)
                assert (out.failure, out.closed) == ("target_unreachable", "uncertain")
                assert calls == []
                assert (await owner.probe_states())[opened.session_id] == "uncertain"
            finally:
                stop_gate.set()

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_forced_reply_and_process_stop_do_not_wait_for_default_executor(tmp_path, monkeypatch):
    """F2: an already-started Core request occupies the only default worker past cancellation."""
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, port = make_bundle(tmp_path, launch_schema=2, mode="sleep:4")
    connect = http.client.HTTPConnection.connect

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            probe, runtime = owner._probes[opened.session_id], owner._targets[target.id]
            entered, release = threading.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            previous = loop._default_executor
            pool = ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(pool)

            def blocked(self):
                if self.port == port:
                    entered.set()
                    assert release.wait(10), "test did not release the network worker"
                return connect(self)

            monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked)
            try:
                started = time.monotonic()
                out = await asyncio.wait_for(owner.probe_turn(opened.session_id, "x", timeout=.2), 1.2)
                assert time.monotonic() - started < 1.2
                assert entered.is_set() and not release.is_set()
                assert probe.ledger.report().in_flight == 1
                assert (out.failure, out.closed) == ("turn_timeout", "uncertain")
                assert (await owner.probe_states())[opened.session_id] == "uncertain"
                async with asyncio.timeout(2):
                    while runtime.helper.process.poll() is None:
                        await asyncio.sleep(.01)
                with pytest.raises(OwnerError) as caught:
                    await owner.start_target(connection, secrets={"TARGET_TOKEN": "private-neutral-token"})
                assert caught.value.code == "cleanup_required"
                assert (state / "artifacts" / f"probe-{opened.session_id}").is_dir()
            finally:
                release.set()
                loop.set_default_executor(previous)
                pool.shutdown(wait=False)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_aborted_materialize_io_must_drain_before_endpoint_reuse(tmp_path, monkeypatch):
    """F3: a materialize worker already started but delayed before connect cannot reach a replacement."""
    from evolution.tree.target.http_adapter import HttpSessionAdapter, HttpTransport
    from evolution.tree.execution.wire_v2 import parse_health

    from evolution_lab.owner import Owner, OwnerError, TargetRef

    state, bundle, port = make_bundle(tmp_path, launch_schema=2, mode="sleep:4", max_instances=2)
    connect, preflight = http.client.HTTPConnection.connect, HttpSessionAdapter.preflight

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        claimed = threading.Lock()

        def blocked(self):
            if self.port == port and claimed.acquire(blocking=False):
                entered.set()
                assert release.wait(10), "test did not release the materialize worker"
            return connect(self)

        async def arm_after_preflight(self, *args, **kwargs):
            result = await preflight(self, *args, **kwargs)
            monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked)
            return result

        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            first = await owner.probe_open(target, session_id=new_id(), seed=None)
            monkeypatch.setattr(HttpSessionAdapter, "preflight", arm_after_preflight)
            second = new_id()
            opening = asyncio.create_task(owner.probe_open(target, session_id=second, seed=None))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(.01)
                probe, runtime = owner._probes[second], owner._targets[target.id]
                assert probe.ledger.report().in_flight == 1
                out = await asyncio.wait_for(owner.probe_turn(first.session_id, "x", timeout=.2), 1.2)
                assert (out.failure, out.closed) == ("turn_timeout", "uncertain")
                with pytest.raises(OwnerError) as caught:
                    await asyncio.wait_for(asyncio.shield(opening), 1.2)
                assert caught.value.code == "target_not_running"
                async with asyncio.timeout(2):
                    while runtime.helper.process.poll() is None:
                        await asyncio.sleep(.01)
                assert probe.ledger.report().in_flight == 1  # cancellation did not stop this thread
                assert (await owner.probe_states())[second] == "uncertain"
                with pytest.raises(OwnerError) as caught:
                    await owner.start_target(connection, secrets={"TARGET_TOKEN": "private-neutral-token"})
                assert caught.value.code == "cleanup_required"
            finally:
                release.set()
                await asyncio.gather(opening, return_exceptions=True)
                monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
                monkeypatch.setattr(HttpSessionAdapter, "preflight", preflight)
            async with asyncio.timeout(5):
                while (await owner.target(target)).state != "stopped":
                    await asyncio.sleep(.01)
            for entry in list(owner._tasks.values()):
                if entry.action == "target_stop":
                    await owner.wait(entry.ref)
            assert probe.ledger.report().in_flight == 0
            restarted = await owner.wait(await owner.start_target(
                connection, secrets={"TARGET_TOKEN": "private-neutral-token"}))
            assert restarted.state == "succeeded", restarted.to_payload()
            replacement = TargetRef(restarted.ref.id, owner.owner_generation)
            status, raw, _ = await HttpTransport(runtime.binding.manifest.target).request("GET", "/evolution/v2/health")
            import json
            assert status == 200 and parse_health(json.loads(raw)).instances == ()
            await _stop(owner, replacement)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_turn_longer_than_the_transport_read_timeout_runs_to_the_session_timeout(tmp_path, monkeypatch):
    """Codex r1 A2: the session's turn timeout reaches the transport; every other call and the settlement keep
    the declared transport bound (here connect 1 s + read 1 s, settle 3*1 + 1 + 5 s)."""
    from evolution.tree import TargetCallLedger

    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="sleep:3",
                                   target_timeouts={"connect_seconds": 1, "read_seconds": 1})
    settle, waits = TargetCallLedger.settle, []

    async def recorded(self, timeout):
        waits.append(timeout)
        return await settle(self, timeout)
    monkeypatch.setattr(TargetCallLedger, "settle", recorded)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            for bad in (0, -1, float("inf"), True, "10"):
                with pytest.raises(OwnerError) as caught:
                    await owner.probe_open(target, session_id=new_id(), seed=None, turn_timeout_seconds=bad)
                assert (caught.value.code, caught.value.stage) == ("invalid_argument", "probe")
            opened = await owner.probe_open(target, session_id=new_id(), seed=None, turn_timeout_seconds=10)
            out = await owner.probe_turn(opened.session_id, "x", timeout=10)
            assert out.failure is None and out.result.outcome == "ok", out
            assert await owner.probe_close(opened.session_id) == "released"
            assert waits == [9.0]
            # Without the session's allowance the declared read timeout cuts the same turn off.
            short = await owner.probe_open(target, session_id=new_id(), seed=None)
            out = await owner.probe_turn(short.session_id, "x", timeout=10)
            assert (out.failure, out.closed) == ("target_unreachable", "uncertain")
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_stop_during_a_turn_interrupts_it_and_closes_the_session(tmp_path):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2, mode="sleep:2")

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            turn = asyncio.create_task(owner.probe_turn(opened.session_id, "x", timeout=60))
            await asyncio.sleep(.3)
            await asyncio.wait_for(_stop(owner, target), 30)
            out = await asyncio.wait_for(turn, 5)
            assert out.result is None and out.failure == "target_unreachable"
            assert out.closed == "uncertain"  # a timed-out or cancelled send is never a normal return
            assert (await owner.probe_states())[opened.session_id] == "released"
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
@pytest.mark.parametrize("failing", ["releasing", "final"])
def test_a_failing_state_write_still_settles_and_answers_uncertain(tmp_path, monkeypatch, failing):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, _ = make_bundle(tmp_path)
    original = OwnerStateStore.set_probe_state

    def flaky(self, session_id, value, **kwargs):
        if (value == "releasing") if failing == "releasing" else (value in {"released", "uncertain"}):
            raise OwnerError("io_error", "probe")
        return original(self, session_id, value, **kwargs)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            runtime = owner._probes[opened.session_id]
            closed = []
            store_close = runtime.store.close
            runtime.store.close = lambda: (closed.append(True), store_close())
            monkeypatch.setattr(OwnerStateStore, "set_probe_state", flaky)
            assert await owner.probe_close(opened.session_id) == "uncertain"
            monkeypatch.setattr(OwnerStateStore, "set_probe_state", original)
            assert opened.session_id not in owner._probes
            assert closed == [True] and runtime.ledger.report().admission_closed
            # Answered again from the store, still never "releasing".
            assert await owner.probe_close(opened.session_id) == "uncertain"
            expected = "uncertain" if failing == "releasing" else "releasing"
            rows = {s: v for s, _, v in await owner._db("probe_rows")}
            assert rows[opened.session_id] == expected
            assert (state / "artifacts" / f"probe-{opened.session_id}").is_dir()
            await _stop(owner, target)
            assert (await owner.probe_states())[opened.session_id] == "released"
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_probe_limit_follows_max_instances(tmp_path):
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path, max_instances=1)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.probe_open(target, session_id=new_id(), seed=None)
            refused = new_id()
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=refused, seed=None)
            assert (caught.value.code, caught.value.stage) == ("resource_busy", "probe")
            assert refused not in await owner.probe_states()
            assert await owner.probe_close(first.session_id) == "released"
            again = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert await owner.probe_close(again.session_id) == "released"
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_open_creates_no_instance_on_a_target_quarantined_after_its_admission(tmp_path, monkeypatch):
    """Codex r2 R2-1 (ADR-0021): a session admitted before another one became uncertain does not go on to create
    its instance on the now quarantined target."""
    from evolution.tree.target.http_adapter import HttpSessionAdapter

    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path, max_instances=2)
    preflight, materialize = HttpSessionAdapter.preflight, HttpSessionAdapter.materialize
    created, quarantine = [], {}

    async def preflight_then_quarantine(self, *args, **kwargs):
        answer = await preflight(self, *args, **kwargs)
        if quarantine:  # the other session's release went unsettled meanwhile
            await quarantine["owner"]._db("set_probe_state", quarantine["session"], "uncertain")
        return answer

    async def counted(self, *args, **kwargs):
        created.append(args)
        return await materialize(self, *args, **kwargs)

    monkeypatch.setattr(HttpSessionAdapter, "preflight", preflight_then_quarantine)
    monkeypatch.setattr(HttpSessionAdapter, "materialize", counted)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert len(created) == 1
            quarantine.update(owner=owner, session=first.session_id)
            second = new_id()
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=second, seed=None)
            assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
            assert len(created) == 1  # no instance was created for the second session
            assert (await owner.probe_states())[second] == "released"
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_probe_open_refuses_bad_arguments_and_a_stopped_target(tmp_path):
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            for session, seed in (("not-hex", None), (new_id(), True), (new_id(), "1")):
                with pytest.raises(OwnerError) as caught:
                    await owner.probe_open(target, session_id=session, seed=seed)
                assert (caught.value.code, caught.value.stage) == ("invalid_argument", "probe")
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=opened.session_id, seed=None)
            assert caught.value.code == "invalid_argument"
            assert await owner.probe_close(opened.session_id) == "released"
            await _stop(owner, target)
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=new_id(), seed=None)
            assert caught.value.code == "target_not_running"
            with pytest.raises(OwnerError) as caught:
                await owner.probe_close(new_id())
            assert (caught.value.code, caught.value.stage) == ("not_found", "probe")

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_new_generation_marks_open_probe_rows_uncertain(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.api import Owner as OwnerClass
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, _ = make_bundle(tmp_path)

    async def crash():
        # Simulate an Owner that dies with an open probe: neither shutdown nor the target
        # settlement closes or releases its row.
        async def no_close(self, probe):
            return None

        monkeypatch.setattr(OwnerClass, "_shut_probe", no_close)
        monkeypatch.setattr(OwnerStateStore, "release_probes_of_target", lambda self, target_id: None)
        monkeypatch.setattr(OwnerStateStore, "_release_probe_rows", staticmethod(lambda database, target_id: None))
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
        monkeypatch.undo()
        return opened

    opened = run(crash())

    async def reopen():
        async with Owner.open(state) as owner:
            assert opened.owner_generation != owner.owner_generation
            assert (await owner.probe_states())[opened.session_id] == "uncertain"
            assert await owner.probe_close(opened.session_id) == "uncertain"

    run(reopen())


@pytest.mark.parametrize("route", ["confirmed", "manual"])
def test_store_cleanup_of_an_earlier_target_releases_its_probe_rows(tmp_path, route):
    from evolution_lab.owner import ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state = tmp_path / "state"
    session = new_id()
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        store.admit_probe(session_id=session, target_id=target.id, max_instances=1)
        store.set_probe_state(session, "open", instance_id="i-1")
        scope = store.target_scope(target.id)
        store.mark_helper_possible(scope["id"])
    with OwnerStateStore.open(state) as store:
        assert store.target_status(target.id).state == "cleanup_required"
        assert [v for _, _, v in store.probe_rows()] == ["uncertain"]
        if route == "confirmed":
            store.confirm_scope_cleanup(scope["id"], scope["owner_generation"], forced=False)
            store.release_evidenced_reservations(ResourceRef("target", target.id),
                                                 scope_ids=(scope["id"],), disposition="confirmed_cleanup")
        else:
            store.release_after_manual_cleanup(ResourceRef("target", target.id), reason="operator killed it")
        assert store.target_status(target.id).state == "stopped"
        assert [v for _, _, v in store.probe_rows()] == ["released"]


# --- final review F1, F5: definitive failures settle by the ledger; released snapshots go ------


@pytest.mark.usefixtures('ownership_mode')
def test_protocol_error_turn_settles_released_and_the_target_stays_usable(tmp_path):
    from evolution_lab.owner import Owner

    state, bundle, _ = make_bundle(tmp_path, launch_schema=2)

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            opened = await owner.probe_open(target, session_id=new_id(), seed=None)
            # Above the target's max_input_bytes (make_bundle: 2_000_000): the target answers 413.
            out = await owner.probe_turn(opened.session_id, "x" * 2_100_000, timeout=30)
            assert out.result is None and out.failure == "protocol_error"
            assert out.closed == "released"  # the target answered: the ledger settles positively
            assert (await owner.probe_states())[opened.session_id] == "released"
            assert not (state / "artifacts" / f"probe-{opened.session_id}").exists()
            checked = await owner.wait(await owner.check(connection, target_ref=target))
            assert checked.state == "succeeded", checked.to_payload()
            again = await owner.probe_open(target, session_id=new_id(), seed=None)
            assert (await owner.probe_turn(again.session_id, "hi", timeout=30)).failure is None
            assert await owner.probe_close(again.session_id) == "released"
            await _stop(owner, target)
            assert not (await owner.health()).blockers

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_refused_preflight_settles_released(tmp_path, monkeypatch):
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path)
    original = HttpSessionAdapter.preflight

    async def refused(self, expected):
        await original(self, expected)  # the target answered health ...
        raise RolloutInfrastructureError("pin differs", "pin_mismatch")  # ... and the adapter refuses it

    async def scenario():
        async with Owner.open(state) as owner:
            connection, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(HttpSessionAdapter, "preflight", refused)
            session = new_id()
            with pytest.raises(OwnerError) as caught:
                await owner.probe_open(target, session_id=session, seed=None)
            assert (caught.value.code, caught.value.stage) == ("target_mismatch", "probe")
            monkeypatch.setattr(HttpSessionAdapter, "preflight", original)
            assert (await owner.probe_states())[session] == "released"
            checked = await owner.wait(await owner.check(connection, target_ref=target))
            assert checked.state == "succeeded", checked.to_payload()
            await _stop(owner, target)

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_target_stop_deletes_snapshots_of_the_rows_it_releases(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError

    state, bundle, _ = make_bundle(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            restore = lose_materialize_response(monkeypatch)
            session = new_id()
            with pytest.raises(OwnerError):
                await owner.probe_open(target, session_id=session, seed=None)
            restore()
            assert (await owner.probe_states())[session] == "uncertain"
            snapshot = state / "artifacts" / f"probe-{session}"
            assert snapshot.is_dir()
            await _stop(owner, target)
            assert (await owner.probe_states())[session] == "released"
            assert not snapshot.exists()

    run(scenario())


@pytest.mark.usefixtures('ownership_mode')
def test_manual_cleanup_of_an_earlier_target_deletes_its_probe_snapshots(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state = tmp_path / "state"
    session, kept = new_id(), new_id()
    with OwnerStateStore.open(state) as store:  # an earlier generation died with an open probe row
        target = _ready_target(store)
        store.admit_probe(session_id=session, target_id=target.id, max_instances=1)
        store.set_probe_state(session, "open", instance_id="i-1")
        store.mark_helper_possible(store.target_scope(target.id)["id"])
    for name in (session, kept):  # `kept` has no released row: never touched
        (state / "artifacts" / f"probe-{name}").mkdir(parents=True)
        (state / "artifacts" / f"probe-{name}" / "file").write_text("x")

    async def scenario():
        async with Owner.open(state) as owner:
            assert (await owner.probe_states())[session] == "uncertain"
            await owner.release_after_manual_cleanup(ResourceRef("target", target.id), reason="operator stopped it")
            assert {s: v for s, _, v in await owner._db("probe_rows")}[session] == "released"

    run(scenario())
    assert not (state / "artifacts" / f"probe-{session}").exists()
    assert (state / "artifacts" / f"probe-{kept}" / "file").exists()
