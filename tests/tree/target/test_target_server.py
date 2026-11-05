"""The `/evolution/v2` server library: backend protocol, snapshot ownership,
lifecycle and the F0 page ("A1")."""

from __future__ import annotations

import ast
import dataclasses
import gc
import http.client
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.execution.interaction import TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import (
    PREFIX,
    canonical_body,
    parse_health,
    parse_instance,
    parse_session,
    parse_turn,
)
from evolution.tree.target import target_server
from evolution.tree.target.target_server import (
    BackendError,
    BackendReply,
    LoadedTree,
    TargetServer,
)


def test_backend_reply_wraps_a_turn_result_and_defaults_to_not_truncated():
    result = TurnResult(({"type": "assistant_message", "content": "a"},), None, 0, "ok")
    reply = BackendReply(result)
    assert reply.result is result and reply.truncated is False
    with pytest.raises(ValueError):
        BackendReply("not a result")  # type: ignore[arg-type]


def test_loaded_tree_is_frozen_and_carries_the_declared_bound_and_mode(tmp_path):
    from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1

    tree = LoadedTree(
        instance_id="run-" + "0" * 32,
        root=tmp_path,
        policy=ArtifactPolicyV1((), (), ("skills/**",)),
        candidate_digest="a" * 64,
        dependency_digest="b" * 64,
        artifact=(),
        dependency=(),
        max_output_bytes=1 << 20,
        seed_mode="exact",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        tree.root = tmp_path / "x"  # type: ignore[misc]
    assert issubclass(BackendError, Exception)


# -- scripted backend behaviours ----------------------------------------------

POLICY = ArtifactPolicyV1(("build/**",), ("uv.lock",), ("skills/**",))
RUN = "run"
IID = RUN + "-" + "0" * 32
IID2 = RUN + "-" + "1" * 32


def write_tree(root, first_line="# hello"):
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "SKILL.md").write_text(f"{first_line}\nbody\n")
    (root / "uv.lock").write_text("lock\n")


class ScriptedSession:
    def __init__(self, backend, instance, seed):
        self.backend, self.instance, self.effective_seed = backend, instance, seed
        self.turns: list[str] = []
        self.closed = False

    def turn(self, message):
        self.backend.calls.append(("turn", self.instance.tree.instance_id, message.content))
        if self.backend.turn_raises:
            raise BackendError("turn failed")
        self.turns.append(message.content)
        answer = f"{self.instance.skill_line} | {message.content} | {len(self.turns)}"
        result = TurnResult(
            events=(
                {"type": "assistant_message", "content": answer},
                {"type": "tool_call", "name": "search", "arguments": {"q": message.content}},
                {"type": "private_note", "text": "never shown"},
            ),
            usage=Usage(prompt=1, completion=2),
            latency_ms=12345,
            outcome="ok",
        )
        return BackendReply(result, truncated=self.backend.truncate)

    def close(self):
        self.backend.calls.append(("session.close", self.instance.tree.instance_id))
        self.closed = True


class ScriptedInstance:
    def __init__(self, backend, tree):
        self.backend, self.tree = backend, tree
        self.skill_line = (tree.root / "skills" / "SKILL.md").read_text().splitlines()[0]
        self.sessions: list[ScriptedSession] = []
        self.closed = False

    def open(self, public, seed):
        self.backend.calls.append(("open", self.tree.instance_id, dict(public), seed))
        if self.backend.open_raises:
            raise BackendError("open failed")
        reported = self.backend.seed_override(seed) if self.backend.seed_override else seed
        session = ScriptedSession(self.backend, self, reported)
        self.sessions.append(session)
        return session

    def close(self):
        self.backend.calls.append(("instance.close", self.tree.instance_id))
        self.closed = True


class ScriptedBackend:
    def __init__(self):
        self.calls: list = []
        self.instances: list[ScriptedInstance] = []
        self.load_raises = False
        self.open_raises = False
        self.turn_raises = False
        self.truncate = False
        self.seed_override = None
        self.load_gate: threading.Event | None = None

    def load(self, tree):
        self.calls.append(("load", tree.instance_id, tree.candidate_digest))
        # Gated by identity, not by call order: `load_gate` represents one
        # deliberately slow candidate (always `IID` in these tests), not
        # every load this backend ever serves — a concurrent instance's own
        # load (a different id) must proceed immediately so a test can prove
        # it is never stalled by the slow one. Gating "whichever call
        # arrives first" would be a race: nothing orders `IID`'s own load
        # ahead of a concurrently created instance's load reaching this line.
        if self.load_gate is not None and tree.instance_id == IID:
            self.load_gate.wait()
        if self.load_raises:
            raise BackendError("load failed")
        instance = ScriptedInstance(self, tree)
        self.instances.append(instance)
        return instance


@pytest.fixture
def allowed(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "allowed"
    root.mkdir()
    return root


def make_server(backend, allowed, cls=TargetServer, **overrides):
    kwargs = dict(
        allowed_artifact_root=allowed,
        policy=POLICY,
        seed_mode="exact",
        target_id="scripted",
        contract_digest="scripted-contract-1",
        settings_hash="scripted-settings-1",
        runtime_ref_digest="scripted-runtime-1",
    )
    kwargs.update(overrides)
    return cls(backend, **kwargs)


def create(server, iid, root, request_id="c1"):
    body = canonical_body(
        {
            "request_id": request_id,
            "instance_id": iid,
            "candidate_root": str(root),
            "policy": POLICY.to_payload(),
            "strategy": "artifact_root_injection",
        }
    )
    return server.dispatch("POST", f"{PREFIX}/instances", body)


def open_session(server, iid, seed=7, request_id="s1", public=None):
    body = canonical_body({"request_id": request_id, "public": public or {"q": "x"}, "seed": seed})
    return server.dispatch("POST", f"{PREFIX}/instances/{iid}/sessions", body)


def turn(server, iid, sid, content="hi", request_id="t1"):
    body = canonical_body({"request_id": request_id, "message": {"content": content, "fields": {}}})
    return server.dispatch("POST", f"{PREFIX}/instances/{iid}/sessions/{sid}/turns", body)


def test_callbacks_run_in_order_and_the_backend_reads_the_snapshot(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t1", "# one")
    status, body = create(server, IID, allowed / "t1")
    assert status == 200
    identity = parse_instance(json.loads(body))
    expected = scan_tree(allowed / "t1", POLICY)
    assert identity.loaded_candidate_digest == expected.candidate_digest
    tree_root = Path(backend.instances[0].tree.root)
    assert tree_root != (allowed / "t1") and tree_root.is_relative_to(server.snapshot_root)
    status, body = open_session(server, IID)
    sid = parse_session(json.loads(body)).session_id
    status, body = turn(server, IID, sid, "hello")
    assert status == 200
    reply = parse_turn(json.loads(body))
    assert reply.events[0]["content"] == "# one | hello | 1"
    assert reply.latency_ms != 12345  # the server's measurement, not the backend's
    assert [c[0] for c in backend.calls] == ["load", "open", "turn"]
    server.close()
    assert [c[0] for c in backend.calls][-2:] == ["session.close", "instance.close"]
    assert not server.snapshot_root.exists() or not any(server.snapshot_root.iterdir())


def test_two_trees_are_two_instances_answering_from_their_own_bytes(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "a", "# A")
    write_tree(allowed / "b", "# B")
    assert create(server, IID, allowed / "a", "c1")[0] == 200
    assert create(server, IID2, allowed / "b", "c2")[0] == 200
    ida = parse_instance(json.loads(server.dispatch("GET", f"{PREFIX}/instances/{IID}")[1]))
    idb = parse_instance(json.loads(server.dispatch("GET", f"{PREFIX}/instances/{IID2}")[1]))
    assert ida.loaded_candidate_digest != idb.loaded_candidate_digest
    sa = parse_session(json.loads(open_session(server, IID, request_id="sa")[1])).session_id
    sb = parse_session(json.loads(open_session(server, IID2, request_id="sb")[1])).session_id
    assert "# A |" in parse_turn(json.loads(turn(server, IID, sa)[1])).events[0]["content"]
    assert "# B |" in parse_turn(json.loads(turn(server, IID2, sb)[1])).events[0]["content"]
    server.close()


def test_source_modified_after_load_changes_neither_identity_nor_answers(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t", "# before")
    _, created = create(server, IID, allowed / "t")
    (allowed / "t" / "skills" / "SKILL.md").write_text("# after\n")
    (allowed / "t" / "skills" / "extra.md").write_text("new\n")
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[1] == created
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    assert "# before |" in parse_turn(json.loads(turn(server, IID, sid)[1])).events[0]["content"]
    server.close()


def test_a_raising_load_leaves_no_instance_no_record_and_no_snapshot(allowed):
    backend = ScriptedBackend()
    backend.load_raises = True
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    status, body = create(server, IID, allowed / "t")
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    assert not any(server.snapshot_root.iterdir())
    backend.load_raises = False
    assert create(server, IID, allowed / "t")[0] == 200  # the same request_id was never recorded
    server.close()


def test_a_load_returning_none_leaves_no_instance_no_record_and_no_snapshot(allowed):
    """ADR-0009 treats a non-conforming callback exactly like a raising one.
    Without the shape check, `load` returning `None` published a live instance
    whose every later session request answered `unknown_instance`."""

    class NoneBackend(ScriptedBackend):
        returns_none = True

        def load(self, tree):
            if self.returns_none:
                self.calls.append(("load", tree.instance_id, tree.candidate_digest))
                return None
            return super().load(tree)

    backend = NoneBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")

    status, body = create(server, IID, allowed / "t")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    assert not any(server.snapshot_root.iterdir())
    backend.returns_none = False
    assert create(server, IID, allowed / "t")[0] == 200  # the request_id was never recorded
    server.close()


def test_a_load_returning_an_object_without_open_is_closed_and_refused(allowed):
    """The structural check covers `close` too, and what the backend handed
    back is closed before the instance is forgotten: the server will never
    reach that object again."""

    closed: list[str] = []

    class Halfling:
        def close(self):
            closed.append("closed")

    class HalflingBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            return Halfling()

    backend = HalflingBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")

    status, body = create(server, IID, allowed / "t")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert closed == ["closed"]
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    assert not any(server.snapshot_root.iterdir())
    loads = len([call for call in backend.calls if call[0] == "load"])
    assert create(server, IID, allowed / "t")[0] == 500  # nothing was recorded:
    assert len([call for call in backend.calls if call[0] == "load"]) == loads + 1  # re-executed
    server.close()


def test_an_open_returning_an_object_without_turn_registers_no_session(allowed):
    """The session shape is judged before the seed: an object whose
    `effective_seed` the mode would accept is still refused, closed, and never
    registered when it cannot answer a turn."""

    closed: list[str] = []

    class Halfling:
        effective_seed = 7

        def close(self):
            closed.append("closed")

    class HalflingInstance(ScriptedInstance):
        def open(self, public, seed):
            self.backend.calls.append(("open", self.tree.instance_id, dict(public), seed))
            return Halfling()

    class HalflingBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            instance = HalflingInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = HalflingBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")

    status, body = open_session(server, IID)

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert closed == ["closed"]
    assert not server._instances[IID].sessions
    server.close()


def test_a_load_raising_file_exists_removes_the_snapshot_and_lets_a_retry_succeed(allowed):
    """`except FileExistsError` is scoped to the `snapshot_tree` call: a
    backend that raises it is a backend failure like any other, so the snapshot
    this copy created is removed and the same `instance_id` stays creatable —
    read as a destination collision, the abandoned snapshot would answer
    `internal` for every later creation of that id until the server closed."""

    class FileExistsBackend(ScriptedBackend):
        load_file_exists = True

        def load(self, tree):
            if self.load_file_exists:
                self.calls.append(("load", tree.instance_id, tree.candidate_digest))
                raise FileExistsError(str(tree.root))
            return super().load(tree)

    backend = FileExistsBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")

    status, body = create(server, IID, allowed / "t")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert not any(server.snapshot_root.iterdir())
    backend.load_file_exists = False
    assert create(server, IID, allowed / "t", request_id="c2")[0] == 200
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    assert turn(server, IID, sid)[0] == 200
    server.close()


@pytest.mark.parametrize(
    "mode, requested, reported, ok",
    [
        ("exact", 7, 7, True),
        ("exact", 7, 8, False),
        ("exact", 7, None, False),
        ("unpaired", None, 3, True),
        ("unpaired", None, None, False),
        ("none", None, None, True),
        ("none", None, 1, False),
    ],
)
def test_the_seed_table_is_validated_and_never_repaired(allowed, mode, requested, reported, ok):
    backend = ScriptedBackend()
    backend.seed_override = lambda seed: reported
    server = make_server(backend, allowed, seed_mode=mode)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    status, body = open_session(server, IID, seed=requested)
    if ok:
        assert status == 200
        created = parse_session(json.loads(body))
        assert created.requested_seed == requested and created.effective_seed == reported
    else:
        assert status == 500 and json.loads(body)["error"]["code"] == "internal"
        assert backend.instances[0].sessions[-1].closed
        assert not [c for c in backend.calls if c[0] == "turn"]
    server.close()


def test_open_and_turn_failures_leave_the_documented_state(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    backend.open_raises = True
    status, body = open_session(server, IID)
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    backend.open_raises = False
    sid = parse_session(json.loads(open_session(server, IID, request_id="s2")[1])).session_id
    backend.turn_raises = True
    status, body = turn(server, IID, sid)
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    backend.turn_raises = False
    assert turn(server, IID, sid, request_id="t2")[0] == 200  # the session stayed open
    server.close()


def test_target_failed_is_a_200_the_adapter_classifies(allowed):
    """The wire's own failure statement for one turn: a `200` the wire parses,
    carrying at least one event, whose `outcome` the adapter classifies."""

    class FailingSession(ScriptedSession):
        def turn(self, message):
            return BackendReply(
                TurnResult(
                    ({"type": "assistant_message", "content": ""},), None, 0, "target_failed"
                )
            )

    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    backend.instances[0].sessions[0].__class__ = FailingSession
    status, body = turn(server, IID, sid)
    assert status == 200
    assert parse_turn(json.loads(body)).outcome == "target_failed"
    server.close()


def test_a_reply_the_wire_would_reject_is_internal_and_never_recorded(allowed):
    """`TurnResult` admits an empty event tuple and `wire_v2.TurnResponse` does
    not, so the body is parsed with the wire's own parser before it is
    recorded: `500 internal`, no record, and the session stays open — the
    packaged surface cannot be made to serve, or replay, a 200 the wire
    rejects (ADR-0009)."""

    class EmptySession(ScriptedSession):
        def turn(self, message):
            self.backend.calls.append(("turn", self.instance.tree.instance_id, message.content))
            return BackendReply(TurnResult((), None, 0, "ok"))

    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    backend.instances[0].sessions[0].__class__ = EmptySession

    status, body = turn(server, IID, sid, "hi", "t1")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    # Nothing was recorded: the same request_id executes fresh rather than
    # replaying the refusal, and the session it was refused on is still open.
    backend.instances[0].sessions[0].__class__ = ScriptedSession
    status, body = turn(server, IID, sid, "hi", "t1")
    assert status == 200 and parse_turn(json.loads(body)).outcome == "ok"
    server.close()


def test_replay_returns_the_recorded_bytes_without_a_second_callback(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    first = turn(server, IID, sid, "hi", "t1")
    calls = len(backend.calls)
    assert turn(server, IID, sid, "hi", "t1") == first
    assert len(backend.calls) == calls
    server.close()


def test_truncated_and_the_output_bound(allowed):
    backend = ScriptedBackend()
    backend.truncate = True
    server = make_server(backend, allowed, max_output_bytes=400)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    status, body = turn(server, IID, sid, "x")
    assert status == 200 and json.loads(body)["truncated"] is True
    assert backend.instances[0].tree.max_output_bytes == 400
    status, body = turn(server, IID, sid, "y" * 600, "t2")
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    # Nothing was recorded for the failed attempt: resending the very same
    # request_id with a small message executes fresh rather than replaying
    # the 500.
    status, body = turn(server, IID, sid, "small", "t2")
    assert status == 200 and json.loads(body)["truncated"] is True
    assert turn(server, IID, sid, "z", "t3")[0] == 200  # nothing recorded, session open
    server.close()


def test_session_isolation_on_one_instance(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    s1 = parse_session(json.loads(open_session(server, IID, request_id="s1")[1])).session_id
    s2 = parse_session(json.loads(open_session(server, IID, request_id="s2")[1])).session_id
    turn(server, IID, s1, "a", "t1")
    turn(server, IID, s1, "b", "t2")
    reply = parse_turn(json.loads(turn(server, IID, s2, "c", "t3")[1]))
    assert reply.events[0]["content"].endswith("| c | 1")
    server.close()


# -- Failure paths -----------------------------------------


def test_open_closes_a_session_whose_effective_seed_property_raises(allowed):
    """A backend's `open` may hand back an object whose `effective_seed`
    itself raises on read (not just an invalid value). The server never put
    that object into `instance.sessions`, so `_teardown` will never
    reach it — the server must close it itself in the failure handler, not
    only on the invalid-seed branch."""

    class SeedRaisingSession:
        def __init__(self):
            self.closed = False

        @property
        def effective_seed(self):
            raise RuntimeError("boom")

        def turn(self, message):
            raise AssertionError("must not be called")

        def close(self):
            self.closed = True

    sessions: list[SeedRaisingSession] = []

    class SeedRaisingInstance(ScriptedInstance):
        def open(self, public, seed):
            self.backend.calls.append(("open", self.tree.instance_id, dict(public), seed))
            session = SeedRaisingSession()
            sessions.append(session)
            return session

    class SeedRaisingBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            instance = SeedRaisingInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = SeedRaisingBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")

    status, body = open_session(server, IID)

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert sessions[0].closed
    assert not server._instances[IID].sessions
    server.close()


def test_release_tombstones_even_when_remove_tree_raises(allowed, monkeypatch):
    """`remove_tree` failing to clear the instance's snapshot must not skip the
    bookkeeping around it: the instance still leaves the table, `health`, and
    a later `GET`, and its id is still tombstoned so a replayed creation is
    `request_expired`, not a second live instance."""
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")

    calls = {"n": 0}
    original_remove_tree = target_server.remove_tree

    def flaky_remove_tree(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        return original_remove_tree(path)

    monkeypatch.setattr(target_server, "remove_tree", flaky_remove_tree)

    status, body = server.dispatch("DELETE", f"{PREFIX}/instances/{IID}")

    assert status == 200 and json.loads(body) == {"instance_id": IID, "released": True}
    health = parse_health(json.loads(server.dispatch("GET", f"{PREFIX}/health")[1]))
    assert IID not in health.instances
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    status, body = create(server, IID, allowed / "t", request_id="after-release")
    assert status == 410 and json.loads(body)["error"]["code"] == "request_expired"
    server.close()


# -- three-phase creation, snapshot placement, and the close boundary (ADR-0009) --


def wait_for_load(backend, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not any(c[0] == "load" for c in backend.calls):
        assert time.monotonic() < deadline, "the backend's load was never called"
        time.sleep(0.005)


def test_a_blocked_load_leaves_health_and_other_instances_answering(allowed):
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    server = make_server(backend, allowed)
    write_tree(allowed / "slow")
    write_tree(allowed / "fast", "# fast")
    results = {}

    def slow():
        results["slow"] = create(server, IID, allowed / "slow", "c-slow")

    t = threading.Thread(target=slow, daemon=True)
    t.start()
    wait_for_load(backend)
    status, body = server.dispatch("GET", f"{PREFIX}/health")
    assert status == 200 and IID not in json.loads(body)["instances"]
    assert create(server, IID2, allowed / "fast", "c-fast")[0] == 200
    sid = parse_session(json.loads(open_session(server, IID2, request_id="sf")[1])).session_id
    assert turn(server, IID2, sid)[0] == 200
    waiters = {}

    def waiter(name, fn):
        waiters[name] = fn()

    threads = [
        threading.Thread(
            target=waiter,
            args=("replay", lambda: create(server, IID, allowed / "slow", "c-slow")),
            daemon=True,
        ),
        threading.Thread(
            target=waiter,
            args=("get", lambda: server.dispatch("GET", f"{PREFIX}/instances/{IID}")),
            daemon=True,
        ),
    ]
    for th in threads:
        th.start()
    time.sleep(0.2)  # give both threads real time to reach the lock and block
    assert not waiters  # they wait on the loading instance
    assert all(th.is_alive() for th in threads)  # waiting, not finished early
    backend.load_gate.set()
    t.join(5)
    for th in threads:
        th.join(5)
    assert results["slow"][0] == 200
    assert waiters["replay"] == results["slow"]
    assert waiters["get"][0] == 200
    server.close()


def test_a_failing_load_lets_waiters_observe_the_absent_instance(allowed):
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    backend.load_raises = True
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    t = threading.Thread(target=lambda: create(server, IID, allowed / "t"), daemon=True)
    t.start()
    wait_for_load(backend)
    got = {}
    g = threading.Thread(
        target=lambda: got.setdefault("r", server.dispatch("GET", f"{PREFIX}/instances/{IID}")),
        daemon=True,
    )
    g.start()
    backend.load_gate.set()
    t.join(5)
    g.join(5)
    assert got["r"][0] == 404
    server.close()


def test_snapshot_root_placement_rules(allowed, tmp_path):
    backend = ScriptedBackend()
    with pytest.raises(ValueError):
        make_server(backend, allowed, snapshot_root=allowed)
    with pytest.raises(ValueError):
        make_server(backend, allowed, snapshot_root=allowed.parent)
    under = allowed / ".snapshots"
    under.mkdir()
    server = make_server(backend, allowed, snapshot_root=under)
    write_tree(allowed / "t")
    assert create(server, IID, allowed / "t")[0] == 200
    status, body = create(server, IID2, allowed, "c2")  # contains the snapshot root
    assert status == 403 and json.loads(body)["error"]["code"] == "root_not_allowed"
    status, body = create(server, IID2, under, "c3")  # equals the snapshot root
    assert status == 403
    owned = server.snapshot_root
    assert owned.parent == under and owned.name == f"snapshot-{server.boot_id}"
    server.close()
    assert under.exists() and not any(under.iterdir())  # named root kept, owned subdir removed


def test_reference_target_with_a_broad_allowed_root_still_constructs(tmp_path):
    import tempfile

    from evolution.tree.target.reference_target import ReferenceTarget

    broad = Path(os.path.realpath(tempfile.gettempdir()))
    target = ReferenceTarget(allowed_artifact_root=broad, policy=POLICY)
    assert target.snapshot_root.is_dir()
    target.close()


def test_reference_target_with_a_broad_allowed_root_serves(tmp_path):
    """An instance from a tree under that broad root
    loads, a session opens and a turn answers."""
    from evolution.tree.target.reference_target import ReferenceTarget

    real = Path(os.path.realpath(tmp_path))
    target = ReferenceTarget(allowed_artifact_root=real.parent, policy=POLICY)
    try:
        write_tree(real / "t", "# broad")
        assert create(target, IID, real / "t")[0] == 200
        status, body = open_session(target, IID)
        assert status == 200
        sid = parse_session(json.loads(body)).session_id
        status, body = turn(target, IID, sid, "hello")
        assert status == 200
        assert parse_turn(json.loads(body)).events[0]["content"] == "# broad | hello"
    finally:
        target.close()
    assert not target.snapshot_root.exists()


def test_close_is_the_one_boundary(allowed):
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    server.close()
    server.close()  # idempotent
    for method, path in (
        ("GET", f"{PREFIX}/health"),
        ("GET", f"{PREFIX}/instances/{IID}"),
        ("POST", f"{PREFIX}/instances/{IID}/sessions/{sid}/turns"),
    ):
        status, body = server.dispatch(method, path, b"{}")
        assert status == 503 and json.loads(body)["error"]["code"] == "not_ready"
    assert backend.instances[0].closed and all(s.closed for s in backend.instances[0].sessions)
    assert not server.snapshot_root.exists()


def test_a_creation_admitted_before_close_and_published_after_it_is_refused(allowed):
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("r", create(server, IID, allowed / "t")), daemon=True
    )
    t.start()
    wait_for_load(backend)
    closer = threading.Thread(target=server.close, daemon=True)
    closer.start()
    backend.load_gate.set()
    t.join(5)
    closer.join(5)
    assert result["r"][0] == 503 and json.loads(result["r"][1])["error"]["code"] == "not_ready"
    assert backend.instances[0].closed
    assert not server.snapshot_root.exists()


def test_instance_id_that_aliases_the_snapshot_root_is_refused(allowed, tmp_path):
    """The identifier grammar admits `.` and `..`. `snapshot_root / "."` is
    `snapshot_root` itself. `snapshot_root / ".."` is worse: `Path` does not
    normalise a trailing `..`, so the path names the snapshot root's *parent*
    — and a load failure removes `instance.snapshot` with `shutil.rmtree`,
    which would delete everything in that parent wholesale. Both must be
    refused at admission, before any instance or directory is touched. Uses a
    caller-named `snapshot_root` (rather than the default owned temp
    directory) so its parent — and a sentinel file placed there — are under
    this test's control; the server's own root is the `snapshot-<boot_id>`
    subdirectory it creates inside the named one."""
    backend = ScriptedBackend()
    parent = tmp_path / "snapshots-parent"
    parent.mkdir()
    sentinel = parent / "sentinel.txt"
    sentinel.write_text("keep me")
    snapshot_root = parent / "snapshots"
    snapshot_root.mkdir()
    server = make_server(backend, allowed, snapshot_root=snapshot_root)
    write_tree(allowed / "t")
    for bad_id in (".", ".."):
        status, body = create(server, bad_id, allowed / "t")
        assert status == 400 and json.loads(body)["error"]["code"] == "invalid_request"
        assert server.dispatch("GET", f"{PREFIX}/instances/{bad_id}")[0] == 404
        assert server.snapshot_root.is_dir() and not any(server.snapshot_root.iterdir())
        assert sentinel.exists() and sentinel.read_text() == "keep me"
    server.close()


def test_two_servers_share_one_caller_named_snapshot_root(allowed, tmp_path):
    """A caller-named `snapshot_root` is a *container*: each server creates and
    owns one `snapshot-<boot_id>` subdirectory under it, so two servers may
    hold instances with the same `instance_id` without either one's snapshot
    colliding with — or being removed by — the other's. Nothing the caller put
    in that directory is touched by either server's `close()`."""
    shared = tmp_path / "shared-snapshots"
    shared.mkdir()
    bystander = shared / "not-ours.txt"
    bystander.write_text("keep me")
    first, second = ScriptedBackend(), ScriptedBackend()
    a = make_server(first, allowed, snapshot_root=shared)
    b = make_server(second, allowed, snapshot_root=shared)
    assert a.snapshot_root != b.snapshot_root
    assert a.snapshot_root.parent == shared and b.snapshot_root.parent == shared
    write_tree(allowed / "t")

    assert create(a, IID, allowed / "t")[0] == 200
    assert create(b, IID, allowed / "t")[0] == 200

    snap_a = Path(first.instances[0].tree.root)
    snap_b = Path(second.instances[0].tree.root)
    assert snap_a != snap_b
    assert (snap_a / "skills" / "SKILL.md").is_file() and (snap_b / "skills" / "SKILL.md").is_file()

    a.close()
    assert not snap_a.exists()
    assert (snap_b / "skills" / "SKILL.md").is_file()  # the other server's live snapshot
    assert bystander.read_text() == "keep me"
    assert turn(b, IID, parse_session(json.loads(open_session(b, IID)[1])).session_id)[0] == 200

    b.close()
    assert not snap_b.exists()
    assert shared.is_dir() and [entry.name for entry in shared.iterdir()] == ["not-ours.txt"]
    assert bystander.read_text() == "keep me"


def test_an_existing_snapshot_destination_is_internal_and_is_never_removed(allowed, monkeypatch):
    """`snapshot_tree` refuses a destination that already exists, before its
    own cleanup can run — so those bytes are someone else's. The server maps
    that to `500 internal` and leaves the destination exactly as it found it,
    rather than removing a directory its own copy never created."""
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    occupied = server.snapshot_root / IID
    occupied.mkdir()
    (occupied / "not-ours.txt").write_text("keep me")

    def refuse(source, policy, destination):
        raise FileExistsError(f"{destination} exists")

    monkeypatch.setattr(target_server, "snapshot_tree", refuse)

    status, body = create(server, IID, allowed / "t")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert (occupied / "not-ours.txt").read_text() == "keep me"
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    monkeypatch.undo()
    server.close()


def test_close_does_not_raise_over_a_plain_file_beside_the_owned_root(allowed, tmp_path):
    """A caller-named root may hold anything; `close()` removes only the
    server's own subdirectory and a plain file next to it is neither swept nor
    a reason to raise."""
    shared = tmp_path / "shared-snapshots"
    shared.mkdir()
    (shared / "plain.txt").write_text("keep me")
    backend = ScriptedBackend()
    server = make_server(backend, allowed, snapshot_root=shared)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")

    server.close()
    server.close()  # idempotent over the same caller-owned directory

    assert not server.snapshot_root.exists()
    assert (shared / "plain.txt").read_text() == "keep me"


def test_close_racing_a_failing_creation_removes_the_root_once(allowed):
    """`close()` must not remove the snapshot root while the failing creation
    is still removing a subtree inside it: the failing thread removes its own
    snapshot and drops the table entry afterwards, so `close()` either waits
    on that instance's lock or finds the entry already gone."""
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    backend.load_raises = True
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    created: dict = {}
    creator = threading.Thread(
        target=lambda: created.setdefault("r", create(server, IID, allowed / "t")), daemon=True
    )
    creator.start()
    wait_for_load(backend)
    failures: list[BaseException] = []

    def close_it():
        try:
            server.close()
        except BaseException as exc:  # noqa: BLE001 — the assertion is "it does not raise"
            failures.append(exc)

    closer = threading.Thread(target=close_it, daemon=True)
    closer.start()
    time.sleep(0.1)  # let `close()` reach the loading instance's lock
    backend.load_gate.set()
    creator.join(5)
    closer.join(5)

    assert not failures
    assert created["r"][0] == 500 and json.loads(created["r"][1])["error"]["code"] == "internal"
    assert not server.snapshot_root.exists()


def test_close_waits_for_a_turn_in_flight(allowed):
    """`close` waits for a turn already inside the backend — the
    instance's own lock is the wait — and the turn still answers `200`."""
    entered, gate = threading.Event(), threading.Event()

    class BlockingSession(ScriptedSession):
        def turn(self, message):
            entered.set()
            gate.wait(10)
            return super().turn(message)

    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    backend.instances[0].sessions[0].__class__ = BlockingSession
    answered: dict = {}
    turner = threading.Thread(
        target=lambda: answered.setdefault("r", turn(server, IID, sid)), daemon=True
    )
    turner.start()
    assert entered.wait(5)

    closer = threading.Thread(target=server.close, daemon=True)
    closer.start()
    time.sleep(0.2)
    assert closer.is_alive() and not answered  # close() waits on the turn's lock

    gate.set()
    turner.join(5)
    closer.join(5)

    assert not closer.is_alive()
    assert answered["r"][0] == 200
    assert backend.instances[0].closed and backend.instances[0].sessions[0].closed
    assert not server.snapshot_root.exists()


def test_an_owned_snapshot_root_is_discarded_when_the_server_is_dropped(allowed):
    """`close()` is the intended release, but a server dropped without it must
    not leak its snapshot root for the life of the process: a `weakref.finalize`
    that captures only the path removes it at collection."""
    server = make_server(ScriptedBackend(), allowed)
    root = server.snapshot_root
    assert root.is_dir()

    del server
    gc.collect()

    assert not root.exists()


def test_turn_returning_something_other_than_a_backend_reply_is_internal(allowed):
    """A backend `turn` may hand back anything — a bare string, a plain
    `TurnResult` instead of the `BackendReply` wrapper the protocol requires
    — and the server must fold that into `internal` without ever recording
    it or closing the session out from under the caller."""

    class WrongTypeSession(ScriptedSession):
        def turn(self, message):
            self.backend.calls.append(("turn", self.instance.tree.instance_id, message.content))
            return "not a BackendReply"

    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    create(server, IID, allowed / "t")
    sid = parse_session(json.loads(open_session(server, IID)[1])).session_id
    backend.instances[0].sessions[0].__class__ = WrongTypeSession
    status, body = turn(server, IID, sid, "hi", "t1")
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    # Nothing was recorded for the failed attempt: resending the same
    # request_id executes fresh rather than replaying the 500.
    backend.instances[0].sessions[0].__class__ = ScriptedSession
    status, body = turn(server, IID, sid, "hi", "t1")
    assert status == 200  # the session stayed open
    server.close()


# -- Cleanup, admission I/O and the release race -------


class ClosePropertyTrap:
    """A returned object whose `close` is a descriptor that raises.

    The shape check reads `close` to decide whether the object conforms, and
    the server's own best-effort cleanup reads it again: an attribute lookup
    outside the suppression would let this shape escape the cleanup path
    entirely (ADR-0009).
    """

    def open(self, public, seed):
        raise AssertionError("open must never be called on a rejected instance")

    def turn(self, message):
        raise AssertionError("turn must never be called on a rejected session")

    effective_seed = 7

    @property
    def close(self):
        raise RuntimeError("the close descriptor raises")


def test_a_load_whose_close_descriptor_raises_still_forgets_the_instance(allowed):
    """ADR-0009: a non-conforming callback leaves no instance behind — even
    when closing what it handed back fails in the attribute lookup. Without
    the guaranteed cleanup the table keeps a non-live entry whose lock is
    free, and the next creation of that `instance_id` loops on it forever."""

    class TrapBackend(ScriptedBackend):
        def __init__(self):
            super().__init__()
            self.trap = True

        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            if self.trap:
                return ClosePropertyTrap()
            instance = ScriptedInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = TrapBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")

    status, body = create(server, IID, allowed / "t")

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert server.dispatch("GET", f"{PREFIX}/instances/{IID}")[0] == 404
    assert IID not in server._instances  # no stranded entry to spin on
    assert not any(server.snapshot_root.iterdir())

    # A fresh creation of the same id against a healthy backend: bounded by a
    # join, because the failure this proves is an infinite loop, not a wrong
    # answer. The `request_id` is the first attempt's, which was never
    # recorded, so this executes rather than replays.
    backend.trap = False
    again: dict = {}
    t = threading.Thread(
        target=lambda: again.setdefault("r", create(server, IID, allowed / "t")),
        daemon=True,
    )
    t.start()
    t.join(10)
    assert not t.is_alive(), "the same-id creation spun on a stranded pending entry"
    assert again["r"][0] == 200
    server.close()


def test_an_open_whose_close_descriptor_raises_registers_no_session(allowed):
    """The same guarantee on the session path: the object `open` handed back
    is closed quietly and nothing is registered, whatever reading its `close`
    does — and the instance keeps serving."""

    class TrapInstance(ScriptedInstance):
        def __init__(self, backend, tree):
            super().__init__(backend, tree)
            self.trap = True

        def open(self, public, seed):
            self.backend.calls.append(("open", self.tree.instance_id, dict(public), seed))
            if self.trap:
                return ClosePropertyTrap()
            return super().open(public, seed)

    class TrapBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            instance = TrapInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = TrapBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    assert create(server, IID, allowed / "t")[0] == 200

    status, body = open_session(server, IID)

    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    assert not server._instances[IID].sessions
    assert not server._instances[IID].records  # nothing recorded for the refusal
    backend.instances[0].trap = False
    status, body = open_session(server, IID, request_id="s2")
    assert status == 200
    sid = parse_session(json.loads(body)).session_id
    assert turn(server, IID, sid)[0] == 200
    server.close()


def test_a_blocked_containment_walk_stalls_only_its_own_admission(allowed, monkeypatch):
    """ADR-0009: the global lock is never held for filesystem I/O. The
    containment walk is an `lstat` per path component, so a wedged
    automount under the allowed root can block one admission for as long as
    the kernel makes it wait — and must block nothing else: not `health`, not
    another root's creation, not a release."""
    backend = ScriptedBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "slow")
    write_tree(allowed / "fast", "# fast")
    real = TargetServer._contained
    entered, gate = threading.Event(), threading.Event()

    def blocking(self, candidate_root):
        if candidate_root.endswith("slow"):
            entered.set()
            gate.wait(10)
        return real(self, candidate_root)

    monkeypatch.setattr(TargetServer, "_contained", blocking)
    results: dict = {}
    t = threading.Thread(
        target=lambda: results.setdefault("slow", create(server, IID, allowed / "slow", "c-slow")),
        daemon=True,
    )
    t.start()
    assert entered.wait(5), "the blocking containment walk was never reached"

    status, body = server.dispatch("GET", f"{PREFIX}/health")
    assert status == 200 and json.loads(body)["instances"] == []
    assert create(server, IID2, allowed / "fast", "c-fast")[0] == 200
    sid = parse_session(json.loads(open_session(server, IID2, request_id="sf")[1])).session_id
    assert turn(server, IID2, sid, request_id="tf")[0] == 200
    status, body = server.dispatch("DELETE", f"{PREFIX}/instances/{IID2}")
    assert status == 200 and json.loads(body)["released"] is True
    assert t.is_alive()  # still inside its own validation, holding no global lock

    gate.set()
    t.join(10)
    assert results["slow"][0] == 200
    server.close()


def test_a_same_id_creation_during_a_release_never_reuses_the_closing_instance(allowed):
    """A release un-publishes under the global lock before it tears down, so a
    same-id creation cannot be handed the 200 identity of an instance that is
    already closing. It waits on the releasing instance instead and then finds
    the tombstone: 410 `request_expired`.

    Both halves of that race run here, because un-publishing is also where
    the instance's creation records go (ADR-0007: replay is byte-identical
    *while the owning instance is live*). A creation with a *different*
    `request_id` is the reuse path — it must not be handed the closing
    instance's identity. A resend of the *original* `request_id` is the replay
    path — while the records outlived the un-publication it returned the old
    byte-identical 200 out of phase A, before any instance state was
    consulted, for an instance the next request answered `unknown_instance`.
    Neither may be answered while the release is in flight, and both end at
    the tombstone.
    """
    gate = threading.Event()

    class BlockingInstance(ScriptedInstance):
        def close(self):
            self.backend.calls.append(("instance.close", self.tree.instance_id))
            gate.wait(10)
            self.closed = True

    class BlockingBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            instance = BlockingInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = BlockingBackend()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    status, first = create(server, IID, allowed / "t", "c1")
    assert status == 200
    released: dict = {}
    r = threading.Thread(
        target=lambda: released.setdefault(
            "r", server.dispatch("DELETE", f"{PREFIX}/instances/{IID}")
        ),
        daemon=True,
    )
    r.start()
    deadline = time.monotonic() + 5
    while not any(call[0] == "instance.close" for call in backend.calls):
        assert time.monotonic() < deadline, "the release never reached the teardown"
        time.sleep(0.005)

    created: dict = {}
    # `c2` is the reuse path (a fresh `request_id` for the same instance id);
    # `c1` is the original creation's own id, resent with the identical body.
    other = threading.Thread(
        target=lambda: created.setdefault("c2", create(server, IID, allowed / "t", "c2")),
        daemon=True,
    )
    same = threading.Thread(
        target=lambda: created.setdefault("c1", create(server, IID, allowed / "t", "c1")),
        daemon=True,
    )
    other.start()
    same.start()
    time.sleep(0.2)  # real time to reach the table and decide
    assert not created, "a creation was answered while the release was in flight"
    assert other.is_alive() and same.is_alive()

    gate.set()
    r.join(10)
    other.join(10)
    same.join(10)
    assert json.loads(released["r"][1])["released"] is True
    for request_id in ("c2", "c1"):
        status, body = created[request_id]
        assert status == 410, request_id
        assert json.loads(body)["error"]["code"] == "request_expired", request_id
        assert body != first, request_id
    server.close()


class PhaseBGatedServer(TargetServer):
    """A server one creation can be held inside, between phase A and phase C.

    Phase C's `request_id` reservation is only observable by a contender that
    has *already* passed phase A: one that arrives later waits in phase A
    instead, which is a different branch. `_contained` is phase B's last step,
    so blocking there for one named `candidate_root` parks exactly one request
    between the two locked phases — deterministically, and without a sleep
    standing in for the ordering.
    """

    def __init__(self, *args, gate_root, gate, entered, **kwargs):
        super().__init__(*args, **kwargs)
        self._gate_root, self._gate, self._entered = gate_root, gate, entered

    def _contained(self, candidate_root):
        contained = super()._contained(candidate_root)
        if candidate_root == self._gate_root:
            self._entered.set()
            self._gate.wait(10)
        return contained


def live_instances(server):
    return list(parse_health(json.loads(server.dispatch("GET", f"{PREFIX}/health")[1])).instances)


def test_two_concurrent_creations_sharing_a_request_id_do_not_both_publish(allowed):
    """ADR-0007: a `request_id` is idempotent with byte-identical replay, so
    two creations that share one may not both become an instance.

    The record alone cannot enforce that: it is written at *publication*, and
    both requests pass phase C while the other is still loading. Phase C
    therefore reserves the in-flight `request_id` in the same locked block
    that inserts its loading table entry, and a contender that finds the
    reservation waits on that instance's lock outside the global lock and
    loops. Here the contender is parked in phase B until the winner has
    reserved, so it meets the reservation in phase C — the exact interleaving
    in which both used to publish and the later publication overwrote the
    first request's record, leaving its own resend answered `request_conflict`
    instead of its bytes.
    """
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    entered, gate = threading.Event(), threading.Event()
    write_tree(allowed / "a", "# A")
    write_tree(allowed / "b", "# B")
    server = make_server(
        backend,
        allowed,
        cls=PhaseBGatedServer,
        gate_root=str(allowed / "b"),
        gate=gate,
        entered=entered,
    )
    results: dict = {}
    contender = threading.Thread(
        target=lambda: results.setdefault("loser", create(server, IID2, allowed / "b", "c1")),
        daemon=True,
    )
    contender.start()
    assert entered.wait(5), "the contender never reached phase B"
    winner = threading.Thread(
        target=lambda: results.setdefault("winner", create(server, IID, allowed / "a", "c1")),
        daemon=True,
    )
    winner.start()
    wait_for_load(backend)  # the winner has reserved `c1` and is loading
    gate.set()  # the contender leaves phase B and re-decides under the lock
    time.sleep(0.2)  # real time to reach the reservation and block on it
    assert "loser" not in results and contender.is_alive()
    # It waits on the winner's instance lock, never with the global lock held.
    assert server.dispatch("GET", f"{PREFIX}/health")[0] == 200
    backend.load_gate.set()
    winner.join(10)
    contender.join(10)
    status, body = results["winner"]
    assert status == 200
    assert results["loser"][0] == 409
    assert json.loads(results["loser"][1])["error"]["code"] == "request_conflict"
    assert [c[1] for c in backend.calls if c[0] == "load"] == [IID]
    assert live_instances(server) == [IID]
    assert create(server, IID, allowed / "a", "c1") == (200, body)  # byte-identical replay
    server.close()


def test_one_creation_sent_twice_concurrently_answers_the_identical_bytes(allowed):
    """The same `request_id` with the *same* body, concurrently: the second
    request waits on the reservation the first entered — phase A reads it
    before the instance table — and replays the published record verbatim
    once the load finishes. One request, one instance, one load, identical
    bytes (ADR-0007)."""
    backend = ScriptedBackend()
    backend.load_gate = threading.Event()
    server = make_server(backend, allowed)
    write_tree(allowed / "t")
    results: dict = {}
    first = threading.Thread(
        target=lambda: results.setdefault("first", create(server, IID, allowed / "t", "c1")),
        daemon=True,
    )
    first.start()
    wait_for_load(backend)
    second = threading.Thread(
        target=lambda: results.setdefault("second", create(server, IID, allowed / "t", "c1")),
        daemon=True,
    )
    second.start()
    time.sleep(0.2)  # real time to reach the reservation and block on it
    assert "second" not in results and second.is_alive()
    backend.load_gate.set()
    first.join(10)
    second.join(10)
    assert results["first"][0] == 200
    assert results["second"] == results["first"]
    assert [c[1] for c in backend.calls if c[0] == "load"] == [IID]
    assert live_instances(server) == [IID]
    server.close()


def test_a_reservation_dropped_by_a_failing_load_frees_the_request_id(allowed):
    """A reservation is not a tombstone: a load that publishes nothing drops
    it with its table entry (`_unlink_failed`), so the contender that waited
    on it wakes to a table with neither and creates its own instance. Without
    the drop that contender would wait forever on a `request_id` no record
    will ever answer for."""

    class FailingFirstBackend(ScriptedBackend):
        def load(self, tree):
            self.calls.append(("load", tree.instance_id, tree.candidate_digest))
            if tree.instance_id == IID:
                assert self.load_gate is not None
                self.load_gate.wait(10)
                raise BackendError("load failed")
            instance = ScriptedInstance(self, tree)
            self.instances.append(instance)
            return instance

    backend = FailingFirstBackend()
    backend.load_gate = threading.Event()
    server = make_server(backend, allowed)
    write_tree(allowed / "a", "# A")
    write_tree(allowed / "b", "# B")
    results: dict = {}
    failing = threading.Thread(
        target=lambda: results.setdefault("failed", create(server, IID, allowed / "a", "c1")),
        daemon=True,
    )
    failing.start()
    wait_for_load(backend)
    contender = threading.Thread(
        target=lambda: results.setdefault("created", create(server, IID2, allowed / "b", "c1")),
        daemon=True,
    )
    contender.start()
    time.sleep(0.2)  # real time to reach the reservation and block on it
    assert "created" not in results and contender.is_alive()
    backend.load_gate.set()
    failing.join(10)
    contender.join(10)
    assert results["failed"][0] == 500
    assert json.loads(results["failed"][1])["error"]["code"] == "internal"
    assert results["created"][0] == 200
    assert live_instances(server) == [IID2]
    server.close()


ROOT = Path(__file__).resolve().parents[3]


def test_f0_example_runs_verbatim_from_the_page(allowed):
    page = (ROOT / "docs" / "target-server.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", page, re.S)
    example = next(b for b in blocks if "class SkillLineBackend" in b)
    namespace: dict = {}
    exec(compile(example, "docs/target-server.md", "exec"), namespace)  # noqa: S102 — the page is the contract
    write_tree(allowed / "t", "# from the page")
    server, httpd = namespace["run"](allowed)
    try:
        # Not named `http`: that would shadow the `http.client` module below.
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            f"{PREFIX}/instances",
            body=json.dumps(
                {
                    "request_id": "c1",
                    "instance_id": IID,
                    "candidate_root": str(allowed / "t"),
                    "policy": ArtifactPolicyV1((), (), ("skills/**",)).to_payload(),
                    "strategy": "artifact_root_injection",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        created = conn.getresponse()
        assert created.status == 200
        # `http.client` refuses `getresponse()` on a reused connection until the
        # prior response body is drained (`ResponseNotReady`), so the body is
        # read here even though only the status is asserted — the same idiom
        # every other raw-HTTP test in this suite already follows.
        created.read()
        conn.request(
            "POST",
            f"{PREFIX}/instances/{IID}/sessions",
            body=json.dumps({"request_id": "s1", "public": {}, "seed": 1}),
            headers={"Content-Type": "application/json"},
        )
        sid = json.loads(conn.getresponse().read())["session_id"]
        conn.request(
            "POST",
            f"{PREFIX}/instances/{IID}/sessions/{sid}/turns",
            body=json.dumps({"request_id": "t1", "message": {"content": "ping", "fields": {}}}),
            headers={"Content-Type": "application/json"},
        )
        reply = json.loads(conn.getresponse().read())
        assert reply["events"][0]["content"] == "# from the page | ping"

        # The documented backend must also handle conformance's neutral case,
        # including a turn exactly at the advertised input limit. Echoing it
        # makes the larger response exceed max_output_bytes and return internal.
        conn.request(
            "POST",
            f"{PREFIX}/instances/{IID}/sessions",
            body=json.dumps({"request_id": "s2", "public": {"conformance": True}, "seed": 1}),
            headers={"Content-Type": "application/json"},
        )
        probe_sid = json.loads(conn.getresponse().read())["session_id"]
        probe = {"request_id": "t2", "message": {"content": "", "fields": {}}}
        overhead = len(json.dumps(probe, separators=(",", ":")).encode())
        probe["message"]["content"] = "c" * ((1 << 20) - overhead)
        conn.request(
            "POST",
            f"{PREFIX}/instances/{IID}/sessions/{probe_sid}/turns",
            body=json.dumps(probe, separators=(",", ":")),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["outcome"] == "ok"
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.close()


def test_target_server_imports_only_evolution_tree_and_the_standard_library():
    source = (ROOT / "src" / "evolution" / "tree" / "target" / "target_server.py").read_text()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            assert (
                top in sys.stdlib_module_names
                or name == "evolution.tree"
                or name.startswith("evolution.tree.")
            ), name


def test_the_library_names_are_exported():
    """The eight server names plus the three rollout types the backend protocol
    speaks in: ADR-0009 counts all eleven as the library's public surface.

    This pins the *count*, not merely membership: a ninth `target_server` name
    re-exported from `evolution.tree` without a matching change here (e.g. the
    `DescribingInstance` protocol, deliberately not part of the surface -- see
    its own docstring) must fail this test, not pass it silently.
    """
    import evolution.tree as tree

    server_names = frozenset(
        {
            "TargetServer",
            "TargetBackend",
            "BackendInstance",
            "BackendSession",
            "BackendReply",
            "LoadedTree",
            "BackendError",
            "serve_target",
        }
    )
    rollout_names = frozenset({"Message", "TurnResult", "Usage"})

    for name in server_names | rollout_names:
        assert name in tree.__all__ and hasattr(tree, name)

    exported_from_target_server = {
        name
        for name in tree.__all__
        if getattr(getattr(tree, name, None), "__module__", None)
        == "evolution.tree.target.target_server"
    }
    assert exported_from_target_server == server_names


# -- the optional `describe()` callback (ADR-0009, amended 2026-09-21) -------


class _DescribingInstance(ScriptedInstance):
    # `ScriptedInstance` plus the optional sixth callback.
    def __init__(self, backend, tree, description, raises=False):
        super().__init__(backend, tree)
        self._description = description
        self._raises = raises

    def describe(self):
        if self._raises:
            raise RuntimeError("no")
        return self._description


class _DescribingBackend(ScriptedBackend):
    def __init__(self, description, raises=False):
        super().__init__()
        self._description = description
        self._raises = raises

    def load(self, tree):
        return _DescribingInstance(self, tree, self._description, self._raises)


def _standing_root(tmp_path):
    # `parents=True`: a test that stands several servers up to compare their
    # bodies passes a fresh subdirectory of `tmp_path` for each.
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    root = Path(os.path.realpath(tmp_path)) / "allowed"
    root.mkdir()
    write_tree(root / "t")
    return root


def _create_instance_body(tmp_path, backend):
    """Stand a server up over `backend`, create one instance and hand back its
    raw creation body with the per-server `boot_id` masked, so two bodies from
    two servers can be compared byte for byte. Asserts the create succeeded."""
    root = _standing_root(tmp_path)
    server = make_server(backend, root)
    status, body = create(server, IID, root / "t")
    assert status == 200
    server.close()
    return body.replace(server.boot_id.encode("ascii"), b"BOOT")


def _create_instance(tmp_path, backend):
    """`_create_instance_body`, parsed."""
    return json.loads(_create_instance_body(tmp_path, backend))


def _create_instance_status(tmp_path, backend):
    """Stand a server up over `backend`, attempt one instance creation and hand
    back its status and the parsed `/health` page — for the refusal cases,
    where the create's own body is not the thing under test."""
    root = _standing_root(tmp_path)
    server = make_server(backend, root)
    status, _ = create(server, IID, root / "t")
    _, health_body = server.dispatch("GET", f"{PREFIX}/health")
    server.close()
    return status, json.loads(health_body)


def test_a_backend_without_describe_is_served_as_before(tmp_path):
    # `ScriptedBackend` has no `describe`. The claim the design rests on is
    # byte-identity of the creation body, not merely the absence of the key:
    # a describing backend's body must differ in that one key and nothing
    # else, and a non-describing one must be indistinguishable from `/6`.
    plain = _create_instance_body(tmp_path / "a", ScriptedBackend())
    assert b'"description"' not in plain
    baseline = _create_instance_body(tmp_path / "b", ScriptedBackend())
    assert plain == baseline
    described = _create_instance_body(tmp_path / "c", _DescribingBackend({"config_hash": "abc"}))
    assert described != plain
    assert described.replace(b',"description":{"config_hash":"abc"}', b"") == plain


def test_a_describing_backend_publishes_its_description(tmp_path):
    backend = _DescribingBackend({"config_hash": "abc", "hermetic": True})
    body = _create_instance(tmp_path, backend)
    assert body["description"] == {"config_hash": "abc", "hermetic": True}


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: _DescribingBackend(None, raises=True),
        lambda: _DescribingBackend({"nested": {"a": 1}}),
        lambda: _DescribingBackend({"blob": "x" * 5000}),
    ],
)
def test_a_bad_describe_refuses_the_instance_and_leaves_nothing(tmp_path, backend_factory):
    status, health = _create_instance_status(tmp_path, backend_factory())
    assert status == 500
    assert health["instances"] == []


def test_a_describe_returning_none_reports_nothing_and_creates_the_instance(tmp_path):
    """ADR-0009: `describe` returning `None` means
    "not reported". The instance is created, and its creation body and
    `GET /instances/{id}` omit `description` exactly as for a backend without
    the method; only a raise or a refused non-`None` value fails creation."""

    silent = _create_instance_body(tmp_path / "a", _DescribingBackend(None))
    plain = _create_instance_body(tmp_path / "b", ScriptedBackend())
    assert silent == plain
    assert b'"description"' not in silent
    root = _standing_root(tmp_path / "c")
    server = make_server(_DescribingBackend(None), root)
    status, created = create(server, IID, root / "t")
    assert status == 200
    fetched_status, fetched = server.dispatch("GET", f"{PREFIX}/instances/{IID}")
    assert (fetched_status, fetched) == (200, created)
    assert parse_instance(json.loads(created)).description is None
    _, health = server.dispatch("GET", f"{PREFIX}/health")
    assert len(json.loads(health)["instances"]) == 1
    server.close()


class _MutatingDescribingInstance(ScriptedInstance):
    """A backend instance that hands `describe()` the very dict it keeps —
    and goes on mutating it after the server has validated it."""

    def __init__(self, backend, tree, description):
        super().__init__(backend, tree)
        self.description = description

    def describe(self):
        return self.description


class _MutatingDescribingBackend(ScriptedBackend):
    def __init__(self, description):
        super().__init__()
        self.description = description

    def load(self, tree):
        return _MutatingDescribingInstance(self, tree, self.description)


def test_a_description_is_captured_not_borrowed_from_the_backend(tmp_path):
    """PDR-0014: the description is captured once from the creation body.

    A validator that handed back the caller's own mapping would capture
    nothing: `GET /instances/{id}` rebuilds the body from the held mapping
    (`_identity_body`) and would serve whatever the backend had since put in
    it — re-validated by no one, so a scalar could drift silently and a
    nested value could make a conforming client's `parse_instance` refuse a
    body the server itself served.
    """
    description = {"config_hash": "abc"}
    backend = _MutatingDescribingBackend(description)
    root = _standing_root(tmp_path)
    server = make_server(backend, root)
    try:
        status, created = create(server, IID, root / "t")
        assert status == 200
        # The backend mutates the object it returned from `describe()`: one
        # scalar changed, and one value the grammar would have refused.
        description["config_hash"] = "zzz"
        description["nested"] = {"a": 1}

        status, fetched = server.dispatch("GET", f"{PREFIX}/instances/{IID}")
        assert status == 200
        assert fetched == created
        for body in (created, fetched):
            assert parse_instance(json.loads(body)).description == {"config_hash": "abc"}
    finally:
        server.close()


# -- the optional turn `reason` (PDR-0014, W19) ------------------------------


class _ReasoningSession(ScriptedSession):
    """A scripted session whose reply carries whatever reason the test set."""

    def turn(self, message):
        self.turns.append(message.content)
        result = TurnResult(
            events=({"type": "assistant_message", "content": "{"},),
            usage=Usage(prompt=1, completion=1),
            latency_ms=7,
            outcome=self.backend.turn_outcome,
            reason=self.backend.turn_reason,
        )
        return BackendReply(result)


class _ReasoningInstance(ScriptedInstance):
    def open(self, public, seed):
        session = _ReasoningSession(self.backend, self, seed)
        self.sessions.append(session)
        return session


class _ReasoningBackend(ScriptedBackend):
    def __init__(self, reason, outcome="candidate_failed"):
        super().__init__()
        self.turn_reason = reason
        self.turn_outcome = outcome

    def load(self, tree):
        return _ReasoningInstance(self, tree)


def _served_turn_body(tmp_path, backend):
    """Stand a server up over `backend`, run one turn and hand back its raw body."""
    root = _standing_root(tmp_path)
    server = make_server(backend, root)
    try:
        assert create(server, IID, root / "t")[0] == 200
        status, opened = open_session(server, IID)
        assert status == 200
        session_id = json.loads(opened)["session_id"]
        status, body = turn(server, IID, session_id)
        assert status == 200
        return body
    finally:
        server.close()


def test_a_served_turn_carries_the_backends_reason(tmp_path):
    reason = {"category": "output_format", "code": "unparsable_json"}
    body = _served_turn_body(tmp_path, _ReasoningBackend(reason))
    parsed = json.loads(body)
    assert parsed["outcome"] == "candidate_failed"
    assert parsed["reason"] == reason
    # The server re-parses its own body before recording it; the reason must
    # survive that check rather than be dropped to pass it.
    assert parse_turn(parsed).reason == reason


def test_a_served_turn_without_a_reason_omits_the_key(tmp_path):
    # Omit-not-null, exactly like the instance body's `description`: an older
    # client's closed key set still parses this body.
    parsed = json.loads(_served_turn_body(tmp_path, _ReasoningBackend(None, outcome="ok")))
    assert "reason" not in parsed
    assert parse_turn(parsed).reason is None


def test_a_describing_backend_may_return_any_mapping(tmp_path):
    """ADR-0009 binds the callback as `describe() -> Mapping`, so a backend
    that returns a read-only `Mapping` — the natural way to hand out a
    description without handing out the dict behind it — is conforming and
    must be served, not answered `500 internal`."""

    backend = _DescribingBackend(MappingProxyType({"config_hash": "abc"}))
    body = _create_instance(tmp_path, backend)
    assert body["description"] == {"config_hash": "abc"}


# -- turn `provenance` and the optional `components()` (ADR-0024, G1) ---------


class _ProvenanceSession(ScriptedSession):
    """A scripted session whose reply carries the provenance the backend holds
    (or a large one for the content "big")."""

    def turn(self, message):
        self.turns.append(message.content)
        provenance = self.backend.turn_provenance
        if message.content == "big":
            provenance = {"used": ["a" * 200] * 64}
        result = TurnResult(
            events=({"type": "assistant_message", "content": "{"},),
            usage=Usage(prompt=1, completion=1),
            latency_ms=7,
            outcome=self.backend.turn_outcome,
            provenance=provenance,
        )
        return BackendReply(result)


class _ProvenanceInstance(ScriptedInstance):
    def open(self, public, seed):
        session = _ProvenanceSession(self.backend, self, seed)
        self.sessions.append(session)
        return session


class _ProvenanceBackend(ScriptedBackend):
    def __init__(self, provenance, outcome="ok"):
        super().__init__()
        self.turn_provenance = provenance
        self.turn_outcome = outcome

    def load(self, tree):
        instance = _ProvenanceInstance(self, tree)
        self.instances.append(instance)
        return instance


def test_turn_body_carries_provenance_only_when_set(tmp_path):
    used = {"used": ["skills/SKILL.md"]}
    body = json.loads(_served_turn_body(tmp_path / "a", _ProvenanceBackend(used)))
    assert body["provenance"] == used
    assert parse_turn(body).provenance == used
    bare = json.loads(_served_turn_body(tmp_path / "b", _ProvenanceBackend(None)))
    assert "provenance" not in bare
    assert parse_turn(bare).provenance is None


@pytest.mark.parametrize("outcome", ["target_failed", "candidate_failed"])
def test_provenance_on_a_failed_turn_is_served(tmp_path, outcome):
    used = {"used": ["skills/SKILL.md"]}
    body = json.loads(_served_turn_body(tmp_path, _ProvenanceBackend(used, outcome=outcome)))
    assert body["outcome"] == outcome
    assert body["provenance"] == used


def test_provenance_over_max_output_bytes_is_internal(tmp_path):
    root = _standing_root(tmp_path)
    server = make_server(_ProvenanceBackend(None), root, max_output_bytes=512)
    try:
        assert create(server, IID, root / "t")[0] == 200
        status, opened = open_session(server, IID)
        assert status == 200
        sid = json.loads(opened)["session_id"]
        status, body = turn(server, IID, sid, content="big", request_id="t1")
        assert status == 500 and json.loads(body)["error"]["code"] == "internal"
        # The session stays open: a following small turn on it succeeds.
        status, body = turn(server, IID, sid, content="small", request_id="t2")
        assert status == 200
        assert "provenance" not in json.loads(body)
        # Nothing was recorded for the failed turn: its request_id is fresh.
        status, body = turn(server, IID, sid, content="small", request_id="t1")
        assert status == 200
    finally:
        server.close()
    # Control: the same turn is served under the default cap.
    root = _standing_root(tmp_path / "control")
    server = make_server(_ProvenanceBackend(None), root)
    try:
        assert create(server, IID, root / "t")[0] == 200
        sid = json.loads(open_session(server, IID)[1])["session_id"]
        assert turn(server, IID, sid, content="big", request_id="t1")[0] == 200
    finally:
        server.close()


_COMPONENTS = {"skill": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}


class _ComponentsInstance(ScriptedInstance):
    def __init__(self, backend, tree, components, raises=False):
        super().__init__(backend, tree)
        self.components_value = components
        self._raises = raises

    def components(self):
        if self._raises:
            raise RuntimeError("no")
        return self.components_value


class _ComponentsBackend(ScriptedBackend):
    def __init__(self, components, raises=False):
        super().__init__()
        self.components_value = components
        self._raises = raises

    def load(self, tree):
        instance = _ComponentsInstance(self, tree, self.components_value, self._raises)
        self.instances.append(instance)
        return instance


def test_components_are_published_on_create_replay_and_get(tmp_path):
    root = _standing_root(tmp_path / "a")
    server = make_server(_ComponentsBackend(_COMPONENTS), root)
    try:
        status, created = create(server, IID, root / "t")
        assert status == 200
        assert json.loads(created)["components"] == _COMPONENTS
        replay_status, replayed = create(server, IID, root / "t")
        assert replay_status == 200 and replayed == created
        status, fetched = server.dispatch("GET", f"{PREFIX}/instances/{IID}")
        assert status == 200 and fetched == created
        assert parse_instance(json.loads(fetched)).components == _COMPONENTS
    finally:
        server.close()
    plain = _create_instance(tmp_path / "b", ScriptedBackend())
    assert "components" not in plain


def test_components_are_captured_once_and_detached(tmp_path):
    components = {"skill": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    root = _standing_root(tmp_path)
    server = make_server(_ComponentsBackend(components), root)
    try:
        status, created = create(server, IID, root / "t")
        assert status == 200
        components["skill"]["sources"].append("uv.lock")
        components["other"] = {"sha256": "zz", "sources": []}
        status, fetched = server.dispatch("GET", f"{PREFIX}/instances/{IID}")
        assert status == 200 and fetched == created
        assert parse_instance(json.loads(fetched)).components == _COMPONENTS
    finally:
        server.close()


def test_components_accept_a_mapping_proxy(tmp_path):
    body = _create_instance(tmp_path, _ComponentsBackend(MappingProxyType(_COMPONENTS)))
    assert body["components"] == _COMPONENTS


@pytest.mark.parametrize("bad", ["raises", "returns_none", "oversized", "malformed"])
def test_failing_components_fail_creation_cleanly(tmp_path, bad):
    if bad == "raises":
        backend = _ComponentsBackend(None, raises=True)
    elif bad == "returns_none":
        backend = _ComponentsBackend(None)
    elif bad == "oversized":
        backend = _ComponentsBackend(
            {f"c{i}": {"sha256": "a" * 64, "sources": []} for i in range(4097)}
        )
    else:
        backend = _ComponentsBackend({"k": {"sha256": "x"}})
    root = _standing_root(tmp_path)
    server = make_server(backend, root)
    try:
        status, body = create(server, IID, root / "t", request_id="c1")
        assert status == 500 and json.loads(body)["error"]["code"] == "internal"
        assert backend.instances[0].closed
        assert not any(server.snapshot_root.iterdir())
        _, health = server.dispatch("GET", f"{PREFIX}/health")
        assert json.loads(health)["instances"] == []
        # A new creation against a well-behaved backend then succeeds.
        server.backend = ScriptedBackend()
        assert create(server, IID, root / "t", request_id="c2")[0] == 200
    finally:
        server.close()


def test_an_oversized_creation_body_fails_creation_cleanly(tmp_path):
    backend = _ComponentsBackend({f"c{i}": {"sha256": "a" * 64, "sources": []} for i in range(60)})
    root = _standing_root(tmp_path)
    server = make_server(backend, root, max_output_bytes=2048)
    try:
        status, body = create(server, IID, root / "t", request_id="c1")
        assert status == 500 and json.loads(body)["error"]["code"] == "internal"
        assert backend.instances[0].closed
        assert not any(server.snapshot_root.iterdir())
        _, health = server.dispatch("GET", f"{PREFIX}/health")
        assert json.loads(health)["instances"] == []
        # The same server still creates an instance without components.
        server.backend = ScriptedBackend()
        assert create(server, IID, root / "t", request_id="c2")[0] == 200
    finally:
        server.close()


def test_public_surface_is_unchanged():
    import evolution.tree as tree

    assert not hasattr(tree, "ComponentsInstance")
    assert "ComponentsInstance" not in tree.__all__
