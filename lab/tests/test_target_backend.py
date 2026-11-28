"""Neutral backend against core's contract (spec W19 §3.2; checks §6.1, §6.6)."""
import asyncio
import gc
import json
import threading
import warnings
from threading import Thread

import pytest
from evolution.tree import (
    ArtifactPolicyV1,
    BackendError,
    LoadedTree,
    Message,
    TargetBinding,
    TargetServer,
    Usage,
    serve_target,
)
from evolution.tree.execution.interaction import ScoreResult, ScorerIdentity

from evolution.tree.execution.wire_v2 import PREFIX, STRATEGY, canonical_body, new_instance_id

from evolution_lab.target import AgentTurn, TargetConfigurationError, TurnReason, backend
from evolution_lab.target._backend import _contains_literal
from evolution_lab.target._fit import _WIDEST_LATENCY, _WIDEST_OUTCOME

REASON = TurnReason("execution", "model_behavior")


def tree(tmp_path, seed_mode="none", max_output_bytes=1 << 20):
    return LoadedTree(instance_id="i1", root=tmp_path, policy=ArtifactPolicyV1((), (), ()),
                      candidate_digest="c" * 64, dependency_digest="d" * 64, artifact=(), dependency=(),
                      max_output_bytes=max_output_bytes, seed_mode=seed_mode)


class Agent:
    def __init__(self, view, log, script=None, description=None,
                 describe_raises=False, close_session_raises=False):
        self.view, self.log, self.script = view, log, script or {}
        self.description = description or {"agent": "synthetic"}
        self.describe_raises, self.close_session_raises = describe_raises, close_session_raises
        self.loops = set()

    def describe(self):
        if self.describe_raises:
            raise RuntimeError("describe is broken")
        return self.description

    async def open(self, public, *, session_id, seed):
        self.loops.add(id(asyncio.get_running_loop()))
        self.log.append(("open", dict(public), seed))
        return {"turns": 0}

    async def turn(self, message, state, *, session_id, seed):
        self.loops.add(id(asyncio.get_running_loop()))
        self.log.append(("turn", message.content, state, seed))
        if message.content in self.script:
            return self.script[message.content](state)
        return AgentTurn("ok", f"echo:{message.content}", state={"turns": state["turns"] + 1})

    async def close_session(self, session_id):
        self.log.append(("close_session",))
        if self.close_session_raises:
            raise RuntimeError("close_session is broken")

    async def aclose(self):
        self.log.append(("aclose",))


def make(log, **kwargs):
    agents = []
    async def factory(view):
        agent = Agent(view, log, **kwargs)
        agents.append(agent)
        return agent
    return factory, agents


def test_turn_state_keep_and_close_order(tmp_path):
    log = []
    factory, agents = make(log, script={"fail": lambda state: AgentTurn("target_failed", "")})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({"history": []}, None)
    first = session.turn(Message("hi"))
    assert first.result.outcome == "ok"
    assert first.result.events[-1] == {"type": "assistant_message", "content": "echo:hi"}
    assert session.turn(Message("fail")).result.outcome == "target_failed"
    session.turn(Message("again"))
    assert log[-1] == ("turn", "again", {"turns": 1}, None)   # KEEP after the failed turn
    session.close()
    instance.close()
    assert log[-2:] == [("close_session",), ("aclose",)]
    assert len(agents[0].loops) == 1
    assert not any(t.name.startswith("lab-target-instance") for t in threading.enumerate())


def test_two_sessions_on_one_instance_advance_independently(tmp_path):
    """Spec §6.1: two sessions on one instance, interleaved turns; each
    session's state advances from its own history, unaffected by the other."""
    log = []
    factory, _ = make(log)
    instance = backend(factory).load(tree(tmp_path))
    session_a = instance.open({}, None)
    session_b = instance.open({}, None)
    session_a.turn(Message("a1"))
    session_b.turn(Message("b1"))
    session_a.turn(Message("a2"))
    session_b.turn(Message("b2"))
    turns = [entry for entry in log if entry[0] == "turn"]
    # Both sessions start from {"turns": 0}; each one's second turn sees the
    # state its own first turn produced, not the other session's.
    assert turns == [
        ("turn", "a1", {"turns": 0}, None),
        ("turn", "b1", {"turns": 0}, None),
        ("turn", "a2", {"turns": 1}, None),
        ("turn", "b2", {"turns": 1}, None),
    ]
    instance.close()


@pytest.mark.parametrize("mode, requested, expected", [("none", None, None), ("exact", 7, 7)])
def test_seed_modes(tmp_path, mode, requested, expected):
    log = []
    factory, _ = make(log)
    instance = backend(factory).load(tree(tmp_path, seed_mode=mode))
    session = instance.open({}, requested)
    assert session.effective_seed == expected
    session.turn(Message("x"))
    assert log[-1][-1] == expected
    instance.close()


def test_unpaired_seed_is_chosen_and_reported(tmp_path):
    factory, _ = make([])
    instance = backend(factory).load(tree(tmp_path, seed_mode="unpaired"))
    session = instance.open({}, None)
    assert type(session.effective_seed) is int
    instance.close()


def test_unpaired_seed_honours_a_requested_seed(tmp_path):
    # `seed_mode == "unpaired"` only randomizes when the caller asked for no
    # seed; a requested seed is reported back unchanged, same as "exact".
    factory, _ = make([])
    instance = backend(factory).load(tree(tmp_path, seed_mode="unpaired"))
    session = instance.open({}, 7)
    assert session.effective_seed == 7
    instance.close()


def test_agent_errors_become_backend_errors(tmp_path):
    def boom(state): raise RuntimeError("agent bug")
    def broken(state): raise TargetConfigurationError("no model")
    factory, _ = make([], script={"boom": boom, "broken": broken})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    for content in ("boom", "broken"):
        with pytest.raises(BackendError):
            session.turn(Message(content))
    instance.close()


def test_reply_is_fitted_and_flagged(tmp_path):
    big = lambda state: AgentTurn("ok", "y" * 5000, events=[{"type": "trace", "blob": "z" * 5000}])
    factory, _ = make([], script={"big": big})
    instance = backend(factory).load(tree(tmp_path, max_output_bytes=1024))
    reply = instance.open({}, None).turn(Message("big"))
    assert reply.truncated is True and [e["type"] for e in reply.result.events] == ["assistant_message"]
    instance.close()


def test_candidate_failed_carries_its_reason_as_the_turn_field(tmp_path):
    failed = lambda state: AgentTurn("candidate_failed", "bad format", reason=REASON)
    factory, _ = make([], script={"bad": failed})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    reply = session.turn(Message("bad"))
    assert reply.result.outcome == "candidate_failed"
    assert reply.result.reason == {"category": "execution", "code": "model_behavior"}
    assert all(event["type"] != "turn_reason" for event in reply.result.events)
    assert session.turn(Message("fine")).result.reason is None
    instance.close()


def minimum_size(reason):
    """Lab's measure of the mandatory minimum: an empty answer, plus the reason."""
    body = {"events": [{"type": "assistant_message", "content": ""}], "usage": None,
            "latency_ms": _WIDEST_LATENCY, "outcome": _WIDEST_OUTCOME, "truncated": False}
    if reason is not None:
        body["reason"] = {"category": reason.category, "code": reason.code}
    return len(canonical_body(body))


def test_reason_stays_when_only_the_minimum_fits(tmp_path):
    turn = lambda state: AgentTurn("candidate_failed", "long answer " * 50,
                                   events=[{"type": "tool_call", "name": "t"}], reason=REASON)
    factory, _ = make([], script={"x": turn})
    instance = backend(factory).load(tree(tmp_path, max_output_bytes=minimum_size(REASON)))
    reply = instance.open({}, None).turn(Message("x"))
    assert reply.truncated is True
    assert reply.result.events == ({"type": "assistant_message", "content": ""},)
    assert reply.result.reason == {"category": "execution", "code": "model_behavior"}
    instance.close()


def test_minimum_that_does_not_fit_is_a_backend_error(tmp_path):
    turn = lambda state: AgentTurn("candidate_failed", "", reason=REASON)
    factory, _ = make([], script={"x": turn})
    instance = backend(factory).load(tree(tmp_path, max_output_bytes=minimum_size(REASON) - 1))
    with pytest.raises(BackendError):
        instance.open({}, None).turn(Message("x"))
    instance.close()


def test_turn_that_is_not_an_agent_turn_is_a_backend_error(tmp_path):
    factory, _ = make([], script={"x": lambda state: {"outcome": "ok", "text": "duck"}})
    instance = backend(factory).load(tree(tmp_path))
    with pytest.raises(BackendError):
        instance.open({}, None).turn(Message("x"))
    instance.close()


def serve_one_turn(tmp_path, turn, limit):
    """One turn through a live core `TargetServer` whose `max_output_bytes` is `limit`."""
    root = (tmp_path / f"root-{limit}").resolve()
    (root / "candidate").mkdir(parents=True)
    policy = ArtifactPolicyV1((), (), ())
    factory, _ = make([], script={"x": turn})
    server = TargetServer(backend(factory), allowed_artifact_root=root, policy=policy, seed_mode="none",
                          target_id="synthetic", contract_digest="contract", settings_hash="settings",
                          runtime_ref_digest="runtime", max_output_bytes=limit)
    try:
        instance_id = new_instance_id("run")
        status, _ = server.dispatch("POST", f"{PREFIX}/instances", json.dumps({
            "request_id": "create", "instance_id": instance_id, "candidate_root": str(root / "candidate"),
            "policy": policy.to_payload(), "strategy": STRATEGY}).encode())
        assert status == 200
        status, payload = server.dispatch("POST", f"{PREFIX}/instances/{instance_id}/sessions",
                                          json.dumps({"request_id": "open", "public": {}, "seed": None}).encode())
        assert status == 200
        session_id = json.loads(payload)["session_id"]
        status, payload = server.dispatch(
            "POST", f"{PREFIX}/instances/{instance_id}/sessions/{session_id}/turns",
            json.dumps({"request_id": "turn", "message": {"content": "x", "fields": {}}}).encode())
        return status, json.loads(payload)
    finally:
        server.close()


def create_only(tmp_path, name, description):
    """Create a single instance through a live core `TargetServer` and return
    the create response, without opening a session."""
    root = (tmp_path / f"root-{name}").resolve()
    (root / "candidate").mkdir(parents=True)
    policy = ArtifactPolicyV1((), (), ())
    factory, _ = make([], description=description)
    server = TargetServer(backend(factory), allowed_artifact_root=root, policy=policy, seed_mode="none",
                          target_id="synthetic", contract_digest="contract", settings_hash="settings",
                          runtime_ref_digest="runtime")
    try:
        instance_id = new_instance_id("run")
        status, payload = server.dispatch("POST", f"{PREFIX}/instances", json.dumps({
            "request_id": "create", "instance_id": instance_id, "candidate_root": str(root / "candidate"),
            "policy": policy.to_payload(), "strategy": STRATEGY}).encode())
        return status, json.loads(payload)
    finally:
        server.close()


def test_agent_description_reaches_core_on_create(tmp_path):
    """CORE-W19-03: after create, the description the agent declared at `load`
    appears in the identity body core records for the instance (core writes it
    once into `instance_attested`; `_identity_body` is what `POST /instances`
    returns)."""
    status, body = create_only(tmp_path, "describe-ok", {"config_hash": "abc123", "hermetic": True})
    assert status == 200
    assert body["description"] == {"config_hash": "abc123", "hermetic": True}


def test_nested_description_fails_create_as_internal_through_a_live_server(tmp_path):
    status, body = create_only(tmp_path, "describe-nested", {"nested": {"inner": 1}})
    assert status == 500 and body["error"]["code"] == "internal"


def test_oversized_description_fails_create_as_internal_through_a_live_server(tmp_path):
    status, body = create_only(tmp_path, "describe-oversized", {f"key{i}": i for i in range(33)})
    assert status == 500 and body["error"]["code"] == "internal"


def test_literal_secret_description_still_fails_at_load_through_a_live_server(tmp_path):
    """The secret refusal happens in `_Backend.load` itself (Lab's `_backend`),
    before core ever sees a `load` result to publish — so it takes the same
    `500 internal` path a raising `load` always has (ADR-0009)."""
    root = (tmp_path / "root-describe-secret").resolve()
    (root / "candidate").mkdir(parents=True)
    policy = ArtifactPolicyV1((), (), ())
    factory, _ = make([], description={"key": "sk-SECRET"})
    server = TargetServer(backend(factory, secrets=("sk-SECRET",)), allowed_artifact_root=root, policy=policy,
                          seed_mode="none", target_id="synthetic", contract_digest="contract",
                          settings_hash="settings", runtime_ref_digest="runtime")
    try:
        instance_id = new_instance_id("run")
        status, payload = server.dispatch("POST", f"{PREFIX}/instances", json.dumps({
            "request_id": "create", "instance_id": instance_id, "candidate_root": str(root / "candidate"),
            "policy": policy.to_payload(), "strategy": STRATEGY}).encode())
        assert status == 500 and json.loads(payload)["error"]["code"] == "internal"
    finally:
        server.close()


def test_byte_boundary_through_a_live_target_server(tmp_path):
    """Spec §3.2: the minimum with its reason fits exactly -> 200 carrying the
    reason; one byte less room -> the backend refuses and the server answers 500."""
    turn = lambda state: AgentTurn("candidate_failed", "an answer that cannot fit", reason=REASON)
    limit = minimum_size(REASON)
    status, body = serve_one_turn(tmp_path, turn, limit)
    assert status == 200
    assert body["outcome"] == "candidate_failed" and body["truncated"] is True
    assert body["reason"] == {"category": "execution", "code": "model_behavior"}
    assert body["events"] == [{"type": "assistant_message", "content": ""}]
    assert len(canonical_body(body)) <= limit
    status, body = serve_one_turn(tmp_path, turn, limit - 1)
    assert status == 500 and body["error"]["code"] == "internal"


def test_full_reply_with_reason_at_its_exact_measure_is_served_untouched(tmp_path):
    text = "a complete answer"
    turn = lambda state: AgentTurn("ok", text, reason=REASON)
    measured = {"events": [{"type": "assistant_message", "content": text}], "usage": None,
                "latency_ms": _WIDEST_LATENCY, "outcome": _WIDEST_OUTCOME, "truncated": False,
                "reason": {"category": "execution", "code": "model_behavior"}}
    limit = len(canonical_body(measured))
    status, body = serve_one_turn(tmp_path, turn, limit)
    assert status == 200 and body["truncated"] is False and body["events"][-1]["content"] == text
    status, body = serve_one_turn(tmp_path, turn, limit - 1)
    assert status == 200 and body["truncated"] is True and body["events"][-1]["content"] == text[:-1]


def test_description_with_a_literal_secret_refuses_load(tmp_path):
    factory, _ = make([], description={"key": "sk-SECRET"})
    with pytest.raises(BackendError):
        backend(factory, secrets=("sk-SECRET",)).load(tree(tmp_path))
    factory, _ = make([], description={"key": "public"})
    instance = backend(factory, secrets=("sk-SECRET",)).load(tree(tmp_path))
    assert instance.description == {"key": "public"}
    instance.close()


def test_secret_scan_sees_through_json_escaping(tmp_path):
    # A `json.dumps` scan misses these: the quote/backslash/newline are escaped
    # in the dumped text, so the raw needle no longer appears in it.
    for secret in ('ab"cd', "a\\b", "line\nbreak"):
        factory, _ = make([], description={"key": secret})
        with pytest.raises(BackendError):
            backend(factory, secrets=(secret,)).load(tree(tmp_path))


def test_contains_literal_walks_nested_lists():
    # `_contains_literal` itself still walks arbitrary nesting: core's shape
    # validation (below) now refuses a nested description before this ever
    # runs in `_Backend.load`, but the scanner stays robust in isolation.
    value = {"nested": ["outer", {"inner": ["deep-SECRET"]}]}
    assert _contains_literal(value, ("deep-SECRET",)) is True
    assert _contains_literal(value, ("absent",)) is False


def test_contains_literal_tolerates_a_non_string_key():
    # A `json.dumps` scan raises a bare TypeError on a tuple key; this must not.
    # Core's shape validation would also refuse a non-identifier key, so this
    # is exercised on `_contains_literal` directly rather than through `.load`.
    assert _contains_literal({("tuple", "key"): "value"}, ("nope",)) is False


def test_short_secret_does_not_false_positive_on_json_punctuation(tmp_path):
    factory, _ = make([], description={"key": "public"})
    instance = backend(factory, secrets=(":",)).load(tree(tmp_path))
    instance.close()


def test_describe_returning_a_non_mapping_refuses_load_with_backend_error(tmp_path):
    factory, _ = make([], description="not-a-mapping")
    with pytest.raises(BackendError):
        backend(factory).load(tree(tmp_path))


def test_nested_description_refuses_load(tmp_path):
    # CORE-W19-03: core's shape is a flat object of scalars; `_backend` holds
    # its loaded instance to the same shape via `validate_instance_description`
    # rather than waiting for core to refuse it later.
    factory, _ = make([], description={"nested": {"inner": 1}})
    with pytest.raises(BackendError):
        backend(factory).load(tree(tmp_path))


def test_oversized_description_refuses_load(tmp_path):
    factory, _ = make([], description={f"key{i}": i for i in range(33)})  # > 32 keys
    with pytest.raises(BackendError):
        backend(factory).load(tree(tmp_path))


def test_instance_exposes_describe_for_core(tmp_path):
    # CORE-W19-03: the loaded instance hands its captured description back to
    # core through `describe()`, called once after `load`; the agent is never
    # called again for it.
    factory, _ = make([], description={"config_hash": "abc123", "hermetic": True})
    instance = backend(factory).load(tree(tmp_path))
    assert instance.describe() == {"config_hash": "abc123", "hermetic": True}
    instance.close()


def test_load_failure_after_the_factory_still_releases_the_agent(tmp_path):
    log = []
    factory, _ = make(log, describe_raises=True)
    with pytest.raises(BackendError):
        backend(factory).load(tree(tmp_path))
    assert log == [("aclose",)]
    assert not any(t.name.startswith("lab-target-instance") for t in threading.enumerate())


def test_instance_close_keeps_closing_after_a_session_close_fails(tmp_path):
    log = []
    factory, _ = make(log, close_session_raises=True)
    instance = backend(factory).load(tree(tmp_path))
    instance.open({}, None)
    instance.open({}, None)
    with pytest.raises(BackendError):
        instance.close()
    assert log.count(("close_session",)) == 2
    assert log[-1] == ("aclose",)
    assert not any(t.name.startswith("lab-target-instance") for t in threading.enumerate())


def test_instance_close_is_idempotent_and_does_not_mask_the_first_error(tmp_path):
    log = []
    factory, _ = make(log, close_session_raises=True)
    instance = backend(factory).load(tree(tmp_path))
    instance.open({}, None)
    with pytest.raises(BackendError):
        instance.close()
    instance.close()  # a second close is a no-op: no new error, the first is not replayed
    assert log.count(("close_session",)) == 1
    assert log[-1] == ("aclose",)


def test_open_after_close_refuses_without_creating_a_coroutine(tmp_path):
    log = []
    factory, _ = make(log)
    instance = backend(factory).load(tree(tmp_path))
    instance.close()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BackendError):
            instance.open({}, None)
        gc.collect()
    assert not any("coroutine" in str(w.message) for w in caught)
    assert log[-1] == ("aclose",)  # no "open" entry was appended


def test_turn_after_close_refuses_without_creating_a_coroutine(tmp_path):
    log = []
    factory, _ = make(log)
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    instance.close()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BackendError):
            session.turn(Message("x"))
        gc.collect()
    assert not any("coroutine" in str(w.message) for w in caught)
    assert log[-1] == ("aclose",)  # no "turn" entry was appended


def test_agent_systemexit_becomes_backend_error_and_does_not_hang(tmp_path):
    def blow_up(state):
        raise SystemExit(3)

    factory, _ = make([], script={"exit": blow_up})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)

    outcome = {}

    def run():
        try:
            session.turn(Message("exit"))
        except BaseException as exc:  # captured across the thread boundary
            outcome["error"] = exc

    runner = threading.Thread(target=run)
    runner.start()
    runner.join(timeout=5)
    assert not runner.is_alive(), "session.turn hung instead of raising"
    assert isinstance(outcome.get("error"), BackendError)

    # the loop thread must have survived the SystemExit and still serve turns
    reply = session.turn(Message("hi"))
    assert reply.result.outcome == "ok"
    instance.close()


async def test_http_standalone_through_the_neutral_backend(tmp_path):
    """Real A1 HTTP path over the neutral backend, mirroring tests/test_standalone.py's
    `connected` fixture and `arguments()` helper (spec W19 §3.2)."""
    from evolution_lab.standalone import evaluate_target
    from tests.test_standalone import arguments

    source = tmp_path / "source"
    source.mkdir()
    (source / "instructions.txt").write_text("original")
    policy = ArtifactPolicyV1((), (), ("instructions.txt",))
    log = []
    factory, _ = make(log)
    target = TargetServer(backend(factory), allowed_artifact_root=tmp_path, policy=policy,
                          seed_mode="none", target_id="synthetic", contract_digest="contract",
                          settings_hash="settings", runtime_ref_digest="runtime")
    http = serve_target(target, port=0)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        connected = dict(source=source, policy=policy,
                          target=TargetBinding(f"http://127.0.0.1:{http.server_port}", "none",
                                                "contract", "settings"))

        class Scorer:
            identity = ScorerIdentity("synthetic", "scorer-1")

            async def score(self, case, view, candidate):
                assert view.history.turns[0].events[-1] == {"type": "assistant_message",
                                                              "content": "echo:question"}
                return ScoreResult({"quality": .75})

        args = arguments(tmp_path, connected)
        args["scorer"] = Scorer()
        result = await evaluate_target(**args)
        assert result["coverage"] == {"expected": 2, "scored": 2}
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        target.close()


class _OpenReturns(Agent):
    def __init__(self, view, log, initial):
        super().__init__(view, log)
        self.initial = initial

    async def open(self, public, *, session_id, seed):
        self.log.append(("open", dict(public), seed))
        return self.initial


def load_with_open_state(tmp_path, initial, log):
    async def factory(view):
        return _OpenReturns(view, log, initial)
    return backend(factory).load(tree(tmp_path))


@pytest.mark.parametrize("initial", [object(), {"x": float("nan")}, {"f": open}])
def test_open_returning_non_json_state_is_a_backend_error(tmp_path, initial):
    """Spec §3.1: `open` returns `JsonValue | None`; anything else fails the open
    and the agent's per-session bookkeeping is released."""
    log = []
    instance = load_with_open_state(tmp_path, initial, log)
    with pytest.raises(BackendError, match="JSON"):
        instance.open({}, None)
    assert instance.sessions == {}
    assert ("close_session",) in log
    instance.close()


@pytest.mark.parametrize("bad", [object(), float("nan"), {"deep": [1, float("inf")]}])
def test_turn_returning_non_json_state_is_a_backend_error_and_keeps_state(tmp_path, bad):
    log = []
    factory, _ = make(log, script={"bad": lambda state: AgentTurn("ok", "x", state=bad)})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    session.turn(Message("hi"))
    with pytest.raises(BackendError, match="JSON"):
        session.turn(Message("bad"))
    session.turn(Message("again"))
    assert log[-1] == ("turn", "again", {"turns": 1}, None)
    instance.close()


def test_json_state_round_trips_unchanged(tmp_path):
    rich = {"s": "é\n\"", "i": 2**70, "f": 0.1, "b": True, "n": None, "l": [1, [2.5, {"k": []}]], "d": {}}
    log = []
    factory, _ = make(log, script={"set": lambda state: AgentTurn("ok", "", state=rich),
                                   "peek": lambda state: AgentTurn("ok", "")})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    session.turn(Message("set"))
    session.turn(Message("peek"))
    assert log[-1] == ("turn", "peek", rich, None)
    instance.close()


@pytest.mark.parametrize("initial", [None, {"turns": 0, "notes": ["a"]}])
def test_open_json_state_reaches_the_first_turn_unchanged(tmp_path, initial):
    log = []
    async def factory(view):
        agent = _OpenReturns(view, log, initial)
        agent.script = {"peek": lambda state: AgentTurn("ok", "")}
        return agent
    instance = backend(factory).load(tree(tmp_path))
    instance.open({}, None).turn(Message("peek"))
    assert log[-1] == ("turn", "peek", initial, None)
    instance.close()


def test_stored_state_is_a_copy_the_agent_cannot_mutate_later(tmp_path):
    log, kept = [], {"turns": 5, "items": [1]}
    factory, _ = make(log, script={"set": lambda state: AgentTurn("ok", "", state=kept),
                                   "peek": lambda state: AgentTurn("ok", "")})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    session.turn(Message("set"))
    kept["items"].append(2)
    session.turn(Message("peek"))
    assert log[-1] == ("turn", "peek", {"turns": 5, "items": [1]}, None)
    instance.close()


BLANK_EVENT = lambda state: AgentTurn("ok", "", events=({"type": " "},), state={"turns": 99})


def test_state_is_committed_only_after_the_reply_validates(tmp_path):
    """A reply core rejects (a blank event type passes Lab's check, not core's)
    is a `BackendError` and does not advance the session's state."""
    log = []
    factory, _ = make(log, script={"blank": BLANK_EVENT})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    session.turn(Message("hi"))
    with pytest.raises(BackendError):
        session.turn(Message("blank"))
    session.turn(Message("again"))
    assert log[-1] == ("turn", "again", {"turns": 1}, None)
    instance.close()


def test_reply_core_rejects_is_internal_through_a_live_server(tmp_path):
    status, body = serve_one_turn(tmp_path, BLANK_EVENT, 1 << 20)
    assert status == 500 and body["error"]["code"] == "internal"


# --- Codex W19 branch review round 2 -------------------------------------


@pytest.mark.parametrize("secrets, description", [
    (("123456",), {"credential": 123456}),       # an integer spelling the secret
    (("3456",), {"credential": 123456}),         # the secret inside the number's text
    (("-12",), {"credential": -12}),             # sign is part of the JSON text
    (("0.25",), {"ratio": 0.25}),                # a float's canonical JSON text
    (("1e+100",), {"big": 1e100}),               # ... in JSON's exponent spelling
    (("true",), {"flag": True}),                 # a boolean's JSON text, not Python's "True"
    (("null",), {"nothing": None}),              # null's JSON text, not Python's "None"
])
def test_numeric_boolean_and_null_values_are_checked_by_their_json_text(tmp_path, secrets, description):
    """ADR-0013: a scalar that is not a string is compared by its canonical
    JSON text -- what core writes into the instance description -- under the
    same rule as a string (the secret occurs in the value's text)."""
    factory, _ = make([], description=description)
    with pytest.raises(BackendError, match="launch secret"):
        backend(factory, secrets=secrets).load(tree(tmp_path))


@pytest.mark.parametrize("secrets", [("sk-SECRET",), ("123456",), ("True",), ("None",), (":",), ('"',)])
def test_description_with_unrelated_numbers_still_loads(tmp_path, secrets):
    description = {"version": 3, "ratio": 0.5, "enabled": True, "seed": None, "count": 12345}
    factory, _ = make([], description=description)
    instance = backend(factory, secrets=secrets).load(tree(tmp_path))
    assert instance.description == description
    instance.close()


@pytest.mark.parametrize("payload", [float("nan"), object(), "\ud800"])
def test_event_the_wire_cannot_encode_is_a_backend_error_and_keeps_state(tmp_path, payload):
    """Serialisation failures inside `fit` (core's `canonical_body` ValueError,
    a lone surrogate's UnicodeEncodeError) are `BackendError` and commit
    nothing."""
    log = []
    bad = lambda state: AgentTurn("ok", "", events=({"type": "trace", "value": payload},), state={"turns": 99})
    factory, _ = make(log, script={"bad": bad})
    instance = backend(factory).load(tree(tmp_path))
    session = instance.open({}, None)
    session.turn(Message("hi"))
    with pytest.raises(BackendError):
        session.turn(Message("bad"))
    session.turn(Message("again"))
    assert log[-1] == ("turn", "again", {"turns": 1}, None)
    instance.close()


@pytest.mark.parametrize("text,events", [
    ("", ()),
    (" \t\n\u2003", ()),
    ("", ({"type": "tool_result", "name": "lookup", "status": "ok"},)),
])
def test_empty_ok_answer_is_target_failed_with_events_usage_and_state(tmp_path, text, events):
    log = []
    usage = Usage(prompt=3)
    turn = AgentTurn("ok", text, events=events, usage=usage, state={"turns": 7}, truncated=True,
                     reason=TurnReason("agent_outcome", "gave_up"))
    factory, _ = make(log, script={"empty": lambda state: turn,
                                   "keep": lambda state: AgentTurn("ok", "")})
    instance = backend(factory).load(tree(tmp_path))
    try:
        session = instance.open({}, None)
        reply = session.turn(Message("empty"))
        assert reply.result.outcome == "target_failed"
        assert reply.result.reason == {"category": "lab", "code": "empty_answer"}
        assert reply.result.events == (*events, {"type": "assistant_message", "content": text})
        assert reply.result.usage == usage
        assert reply.truncated is True
        session.turn(Message("keep"))
        session.turn(Message("again"))
        assert log[-1] == ("turn", "again", {"turns": 7}, None)
        assert turn.outcome == "ok" and turn.reason == TurnReason("agent_outcome", "gave_up")
    finally:
        instance.close()


@pytest.mark.parametrize("policy,outcome,text", [
    ("ok", "ok", ""),
    ("ok", "ok", " \t"),
    ("target_failed", "ok", " answer "),
    ("target_failed", "candidate_failed", ""),
    ("target_failed", "target_failed", ""),
])
def test_empty_answer_policy_preserves_opt_out_nonempty_and_agent_failures(tmp_path, policy, outcome, text):
    turn = AgentTurn(outcome, text, reason=REASON)
    factory, _ = make([], script={"x": lambda state: turn})
    instance = backend(factory, empty_answer=policy).load(tree(tmp_path))
    try:
        reply = instance.open({}, None).turn(Message("x"))
        assert reply.result.outcome == outcome
        assert reply.result.reason == {"category": "execution", "code": "model_behavior"}
        assert reply.result.events == ({"type": "assistant_message", "content": text},)
    finally:
        instance.close()


def test_empty_answer_replacement_reason_is_measured_before_fitting(tmp_path):
    factory, _ = make([], script={"x": lambda state: AgentTurn("ok", "", reason=REASON)})
    reason = TurnReason("lab", "empty_answer")
    instance = backend(factory).load(tree(tmp_path, max_output_bytes=minimum_size(reason)))
    try:
        reply = instance.open({}, None).turn(Message("x"))
        assert reply.result.outcome == "target_failed"
        assert reply.result.reason == {"category": "lab", "code": "empty_answer"}
        assert reply.truncated is False
    finally:
        instance.close()


def test_empty_answer_reason_must_fit_before_state_is_committed(tmp_path):
    log = []
    factory, _ = make(log, script={"empty": lambda state: AgentTurn("ok", "", state={"turns": 99})})
    instance = backend(factory).load(tree(tmp_path, max_output_bytes=minimum_size(None)))
    try:
        session = instance.open({}, None)
        with pytest.raises(BackendError, match="minimum exceeds"):
            session.turn(Message("empty"))
        session.turn(Message("peek"))
        assert log[-1] == ("turn", "peek", {"turns": 0}, None)
    finally:
        instance.close()


@pytest.mark.parametrize("policy", [None, "candidate_failed", "", [], {}])
def test_backend_refuses_invalid_empty_answer_policy(policy):
    factory, _ = make([])
    with pytest.raises(ValueError, match="empty_answer"):
        backend(factory, empty_answer=policy)
