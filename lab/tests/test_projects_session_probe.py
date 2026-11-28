"""``ProjectSession`` probe API (spec 2026-10-02 §3, §4): forms, chain ceiling, gateway cost, reconcile."""
import asyncio
import sqlite3
from types import MappingProxyType, SimpleNamespace

import pytest
from evolution.tree import TurnResult, Usage

from evolution_lab.owner.models import OwnerError
from evolution_lab.owner.probe import ProbeOpened, ProbeTurnOutcome
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.probes import ProbeStore
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.session import ProjectSession, client_ref

PAIR = Pair("0.0.3.dev37", "0.3.0a28")
GEN = "a" * 32
SESSION_KEYS = {"schema", "id", "project", "implementation", "state", "closed_reason", "reset_of", "created_at",
                "closed_at", "target", "seed", "ceiling", "budget", "tokens_incomplete", "cost_unavailable",
                "turn_timeout_seconds", "turns"}
TURN_KEYS = {"schema", "session_id", "turn_id", "started_at", "finished_at", "latency_ms", "request", "answer",
             "steps", "outcome", "reason", "agent_outcome", "usage", "cost", "truncated", "failure", "ceiling"}


def ok_turn(usage=Usage(10, 5, 0, 15)):
    events = ({"type": "assistant_message", "content": "ответ"},)
    return ProbeTurnOutcome(TurnResult(events=events, usage=usage, latency_ms=7, outcome="ok"), None, None)


#: Turn outcomes the real Owner can answer (owner/probe.py): a timed-out or cancelled send never settles
#: ``released``; any other failure is settled by the ledger, ``released`` or ``uncertain``.
_POSSIBLE = {(None, None), ("turn_timeout", "uncertain"), ("target_unreachable", "released"),
             ("target_unreachable", "uncertain"), ("protocol_error", "released"), ("protocol_error", "uncertain")}


class FakeOwner:
    def __init__(self, ref, *, state="ready", source_kind="package", generation=GEN):
        self.owner_generation = generation
        self.targets = [SimpleNamespace(id="t1", owner_generation=generation, state=state)]
        self.refs = {"t1": ref}
        self.states = {}
        self.outcomes = []
        self.opened, self.turned, self.closed = [], [], []
        self.turn_timeouts = []  # turn_timeout_seconds of each probe_open
        self.open_error = None
        self.close_answer = "released"
        self.on_turn = None
        self.source_kind = source_kind
        self.seed_mode = "exact"
        self.states_error = None
        self.open_gate = None  # an Event the open waits on once admitted

    async def list_targets(self, *, bundle=None, state=None):
        return tuple(t for t in self.targets if state is None or t.state == state)

    async def client_targets(self):
        return tuple((t.id, "b" * 64, self.refs[t.id]) for t in self.targets)

    async def probe_open(self, target_ref, *, session_id, seed, turn_timeout_seconds):
        self.turn_timeouts.append(turn_timeout_seconds)
        # As the real Owner: the open is Owner-held, a cancelled caller does not stop it.
        return await asyncio.shield(self._probe_open(target_ref, session_id=session_id, seed=seed))

    async def _probe_open(self, target_ref, *, session_id, seed):
        if self.open_error is not None:
            raise self.open_error
        if seed is not None and self.seed_mode != "exact":
            raise OwnerError("invalid_argument", "probe")
        self.opened.append((target_ref, session_id, seed))
        self.states[session_id] = "admitted"
        if self.open_gate is not None:
            await self.open_gate.wait()
        self.states[session_id] = "open"
        return ProbeOpened(session_id, "inst", target_ref.id, target_ref.owner_generation, "boot", "b" * 64,
                           "l" * 64, self.source_kind, self.seed_mode, seed if seed is not None else 77,
                           seed if seed is not None else 77)

    async def probe_turn(self, session_id, content, *, timeout):
        self.turned.append((session_id, content, timeout))
        if self.on_turn is not None:
            if (waited := self.on_turn()) is not None:
                await waited
        outcome = self.outcomes.pop(0) if self.outcomes else ok_turn()
        assert (outcome.failure, outcome.closed) in _POSSIBLE, "the real Owner never answers this"
        if outcome.closed is not None:
            self.states[session_id] = outcome.closed
        return outcome

    async def probe_close(self, session_id):
        self.closed.append(session_id)
        self.states[session_id] = self.close_answer
        return self.close_answer

    async def probe_states(self):
        if self.states_error is not None:
            raise self.states_error
        return dict(self.states)

    def restarted(self, generation="c" * 32):
        """The next generation's Owner, as the real one: the store turns every unreleased row ``uncertain`` and
        ``probe_states`` keeps ``uncertain`` rows of any generation (not released ones)."""
        new = FakeOwner(self.refs["t1"], generation=generation, source_kind=self.source_kind)
        new.states = {session: "uncertain" for session, state in self.states.items() if state != "released"}
        return new


@pytest.fixture
def probe_env(tmp_path):
    work = tmp_path / "work"
    (work / "im").mkdir(parents=True)
    (work / "lab").mkdir()
    projects = Projects.open(tmp_path / "home")
    projects.create(id="pr", name="P", materials_root="lab", path=str(work),
                    implementations=[{"id": "im", "name": "I", "place": "im"}])
    owner = FakeOwner(client_ref("pr", "im"))
    session = ProjectSession(projects, preparation=None, owner=owner, pair=PAIR, state_root=tmp_path / "state")
    yield SimpleNamespace(session=session, owner=owner, projects=projects, home=tmp_path / "home")
    session.close()
    projects.close()


async def test_start_turn_history_close(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None, seed=123)
    assert set(s) == SESSION_KEYS and s["schema"] == "lab-probe-session/1"
    assert len(s["id"]) == 32 and int(s["id"], 16) >= 0
    assert (s["project"], s["implementation"], s["state"], s["closed_reason"], s["reset_of"]) == \
        ("pr", "im", "open", None, None)
    assert s["created_at"].endswith("Z") and s["closed_at"] is None
    assert s["target"] == {"id": "t1", "owner_generation": GEN, "boot_id": "boot", "bundle_digest": "b" * 64,
                           "launch_digest": "l" * 64, "source": "package", "seed_mode": "exact"}
    assert s["seed"] == {"requested": 123, "effective": 123}
    assert s["ceiling"] == {"turns": {"used": 0, "max": 3}, "tokens": None, "cost": None}
    assert (s["budget"], s["tokens_incomplete"], s["cost_unavailable"]) == (None, False, False)
    assert s["turn_timeout_seconds"] == 120 and s["turns"] == []
    assert owner.opened[0][0].id == "t1" and owner.opened[0][1] == s["id"]

    t = await session.probe_turn(s["id"], "привет")
    assert set(t) == TURN_KEYS
    assert t["schema"] == "lab-probe-turn/1" and t["turn_id"] == s["id"] + "/1"
    assert t["answer"] == "ответ" and t["agent_outcome"] == "unspecified" and t["cost"] is None
    assert t["ceiling"] == {"turns": {"used": 1, "max": 3}, "tokens": None, "cost": None}
    assert owner.turned == [(s["id"], "привет", 120)]
    h = await session.probe_session(s["id"])
    assert [x["turn_id"] for x in h["turns"]] == [t["turn_id"]] and h["turns"][0] == t
    stored = ProbeStore.open(probe_env.home)  # the stored record carries its ceiling, not only the answer
    try:
        assert stored.turns(s["id"]) == [t]
    finally:
        stored.close()
    assert h["ceiling"]["turns"] == {"used": 1, "max": 3}
    c = await session.probe_close(s["id"])
    assert c["state"] == "closed" and c["closed_reason"] == "closed" and c["closed_at"].endswith("Z")
    assert owner.closed == [s["id"]]
    again = await session.probe_close(s["id"])  # closing a closed session is a no-op
    assert again == c and owner.closed == [s["id"]]

    listed = await session.probe_list("pr")
    assert listed["schema"] == "lab-probe-session-list/1"
    assert [x["id"] for x in listed["sessions"]] == [s["id"]] and "turns" not in listed["sessions"][0]
    assert (await session.probe_list("pr", "im"))["sessions"] == listed["sessions"]


async def test_probe_bundle_target_reports_entry_source(probe_env):
    probe_env.owner.source_kind = "probe"
    s = await probe_env.session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert s["target"]["source"] == "entry" and probe_env.owner.opened[0][2] is None


@pytest.mark.parametrize("invalid", [None, "nan", "non_string_key"])
async def test_turn_with_nested_event_is_done_not_pending(probe_env, invalid):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    arguments = MappingProxyType({"a": (MappingProxyType({"b": (1, 2)}),)})
    if invalid == "nan":
        arguments = MappingProxyType({"a": (MappingProxyType({"b": float("nan")}),)})
    elif invalid == "non_string_key":
        arguments = MappingProxyType({"a": (MappingProxyType({1: "x"}),)})
    events = ({"type": "tool_call", "name": "x", "arguments": arguments},)
    if invalid is None:
        result = TurnResult(events=events, usage=None, latency_ms=1, outcome="ok")
    else:
        # A real TurnResult rejects these; the fake Owner supplies a malformed boundary result.
        result = SimpleNamespace(events=events, usage=None, latency_ms=1, outcome="ok", reason=None)
    owner.outcomes.append(ProbeTurnOutcome(result, None, None))
    turn = await session.probe_turn(s["id"], "q")
    if invalid is None:
        assert turn["steps"][0]["data"]["arguments"] == {"a": [{"b": [1, 2]}]}
        assert turn["outcome"] == "ok" and turn["failure"] is None
    else:
        assert turn["failure"] == {"code": "protocol_error"} and turn["outcome"] == "target_failed"
        assert turn["steps"] == [] and turn["answer"] is None
    stored = ProbeStore.open(probe_env.home)
    try:
        assert stored.pending_turns() == [] and stored.turns(s["id"]) == [turn]
        assert stored._db.execute("SELECT state FROM turns WHERE session_id = ?", (s["id"],)).fetchall() == [("done",)]
    finally:
        stored.close()
    assert (await session.probe_session(s["id"]))["state"] == "open" and owner.closed == []


async def test_ceiling_spans_the_reset_chain(probe_env):
    session = probe_env.session
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 2}, budget=None, turn_timeout_seconds=30)
    await session.probe_turn(s["id"], "1")
    r = await session.probe_reset(s["id"])
    assert r["reset_of"] == s["id"] and r["id"] != s["id"] and r["ceiling"]["turns"]["used"] == 1
    assert r["turn_timeout_seconds"] == 30 and r["turns"] == []
    old = await session.probe_session(s["id"])
    assert old["state"] == "closed" and old["closed_reason"] == "reset"
    assert probe_env.owner.opened[1][2] is None  # the reset picks its seed by the target's mode
    assert probe_env.owner.turn_timeouts == [30, 30]  # the transport lets each turn run the session's timeout
    t = await session.probe_turn(r["id"], "2")
    assert t["turn_id"] == r["id"] + "/1" and t["ceiling"] == {"turns": {"used": 2, "max": 2}, "tokens": None,
                                                               "cost": None}
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(r["id"], "3")
    assert caught.value.code == "probe_ceiling_reached"
    with pytest.raises(ProjectError) as caught:  # the chain stays linear: a closed session is not reset
        await session.probe_reset(s["id"])
    assert caught.value.code == "probe_session_closed"


async def test_turn_on_closed_session_is_refused(probe_env):
    session = probe_env.session
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    await session.probe_close(s["id"])
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_session_closed" and probe_env.owner.turned == []


async def test_unknown_or_malformed_session_id(probe_env):
    for call in (probe_env.session.probe_session, probe_env.session.probe_close, probe_env.session.probe_reset):
        with pytest.raises(ProjectError) as caught:
            await call("f" * 32)
        assert caught.value.code == "probe_session_not_found"
    with pytest.raises(ProjectError) as caught:
        await probe_env.session.probe_turn("f" * 32, "q")
    assert caught.value.code == "probe_session_not_found"
    with pytest.raises(ProjectError) as caught:
        await probe_env.session.probe_turn("../x", "q")
    assert caught.value.code == "invalid_request"


async def test_missing_usage_with_max_tokens_blocks_next_turn(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 5, "max_tokens": 100}, budget=None)
    owner.outcomes.append(ok_turn(usage=None))
    t = await session.probe_turn(s["id"], "q")
    assert t["usage"] is None and t["ceiling"]["tokens"] == {"used": 0, "max": 100}
    assert (await session.probe_session(s["id"]))["tokens_incomplete"] is True
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_ceiling_unverifiable"


#: Temporary triggers on the history connection that make one completion write fail as a full disk would: the
#: write that finishes the turn, or the one that leaves a finished turn with its ceiling.
_FAILING_WRITE = {
    "finish": "CREATE TEMP TRIGGER fail_write BEFORE UPDATE ON turns WHEN NEW.state = 'done' "
              "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END",
    "ceiling": "CREATE TEMP TRIGGER fail_write AFTER UPDATE ON turns "
               "WHEN NEW.state = 'done' AND json_extract(NEW.record, '$.ceiling') IS NOT NULL "
               "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END",
}


@pytest.mark.parametrize("failing", sorted(_FAILING_WRITE))
async def test_unrecorded_turn_spend_is_unknown_and_blocks_a_token_ceiling(probe_env, failing):
    """PDR-0019: a pending turn's spend is unknown until its record is written; a failed completion write leaves
    the turn pending (never half-done), and the next turn under a token ceiling is unverifiable."""
    session = probe_env.session
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 5, "max_tokens": 1000}, budget=None)
    db = session._probes()._db
    db.execute(_FAILING_WRITE[failing])
    with pytest.raises(sqlite3.Error):
        await session.probe_turn(s["id"], "q")
    db.execute("DROP TRIGGER fail_write")  # the disk has room again
    stored = ProbeStore.open(probe_env.home)
    try:
        assert stored.pending_turns() == [(s["id"], 1)] and stored.turns(s["id"]) == []
    finally:
        stored.close()
    h = await session.probe_session(s["id"])  # the Owner still holds the session open: the turn stays pending
    assert h["state"] == "open" and h["tokens_incomplete"] is True and h["ceiling"]["turns"]["used"] == 1
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_ceiling_unverifiable"
    assert len(probe_env.owner.turned) == 1


async def test_unrecorded_turn_without_a_token_ceiling_passes_by_turn_count(probe_env):
    session = probe_env.session
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 2}, budget=None)
    db = session._probes()._db
    db.execute(_FAILING_WRITE["finish"])
    with pytest.raises(sqlite3.Error):
        await session.probe_turn(s["id"], "q")
    db.execute("DROP TRIGGER fail_write")
    t = await session.probe_turn(s["id"], "q2")  # the pending turn counts as a turn
    assert t["turn_id"] == s["id"] + "/2" and t["ceiling"]["turns"] == {"used": 2, "max": 2}
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q3")
    assert caught.value.code == "probe_ceiling_reached"


async def test_token_ceiling_counts_usage_total(probe_env):
    session = probe_env.session
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 5, "max_tokens": 20}, budget=None)
    t = await session.probe_turn(s["id"], "q")
    assert t["ceiling"] == {"turns": {"used": 1, "max": 5}, "tokens": {"used": 15, "max": 20}, "cost": None}
    await session.probe_turn(s["id"], "q")  # one turn may step over the ceiling; it is visible
    h = await session.probe_session(s["id"])
    assert h["ceiling"]["tokens"] == {"used": 30, "max": 20}
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_ceiling_reached"


@pytest.mark.parametrize("ceiling, budget, extra", [
    (None, None, {}),
    ({}, None, {}),
    ({"max_turns": 0}, None, {}),
    ({"max_turns": 1001}, None, {}),
    ({"max_turns": True}, None, {}),
    ({"max_turns": 1, "max_tokens": 0}, None, {}),
    ({"max_turns": 1, "other": 1}, None, {}),
    ({"max_turns": 1, "max_cost": "1.0"}, None, {}),
    ({"max_turns": 1, "max_cost": "0"}, {"ledger_path": "/l.db", "roles": ["a"], "currency": "RUB"}, {}),
    ({"max_turns": 1, "max_cost": "NaN"}, {"ledger_path": "/l.db", "roles": ["a"], "currency": "RUB"}, {}),
    ({"max_turns": 1, "max_cost": 1}, {"ledger_path": "/l.db", "roles": ["a"], "currency": "RUB"}, {}),
    ({"max_turns": 1}, {"ledger_path": "l.db", "roles": ["a"], "currency": "RUB"}, {}),
    ({"max_turns": 1}, {"ledger_path": "/l.db", "roles": [], "currency": "RUB"}, {}),
    ({"max_turns": 1}, {"ledger_path": "/l.db", "roles": ["a", "a"], "currency": "RUB"}, {}),
    ({"max_turns": 1}, {"ledger_path": "/l.db", "roles": ["a"], "currency": "EUR"}, {}),
    ({"max_turns": 1}, {"ledger_path": "/l.db", "roles": ["a"]}, {}),
    ({"max_turns": 1}, None, {"turn_timeout_seconds": 0}),
    ({"max_turns": 1}, None, {"turn_timeout_seconds": 601}),
    ({"max_turns": 1}, None, {"seed": "1"}),
])
async def test_start_requires_ceiling_and_cost_needs_budget(probe_env, ceiling, budget, extra):
    with pytest.raises(ProjectError) as caught:
        await probe_env.session.probe_start("pr", "im", ceiling=ceiling, budget=budget, **extra)
    assert caught.value.code == "invalid_request" and probe_env.owner.opened == []


async def test_start_on_foreign_or_not_ready_target_is_target_not_ready(probe_env):
    session, owner = probe_env.session, probe_env.owner
    owner.refs["t1"] = client_ref("pr", "other")
    with pytest.raises(ProjectError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert caught.value.code == "target_not_ready"
    owner.refs["t1"] = client_ref("pr", "im")
    owner.targets[0].state = "starting"
    with pytest.raises(ProjectError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert caught.value.code == "target_not_ready"
    owner.targets[0].state = "ready"
    owner.open_error = OwnerError("target_not_running", "probe")  # stopped between the read and the open
    with pytest.raises(ProjectError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert caught.value.code == "target_not_ready"
    assert (await session.probe_list("pr"))["sessions"] == []


async def test_owner_resource_busy_maps_to_probe_limit_reached(probe_env):
    session, owner = probe_env.session, probe_env.owner
    owner.open_error = OwnerError("resource_busy", "probe")
    with pytest.raises(ProjectError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert caught.value.code == "probe_limit_reached"
    owner.open_error = OwnerError("probe_sessions_open", "before_start")  # other Owner refusals pass through
    with pytest.raises(OwnerError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert caught.value.code == "probe_sessions_open"


async def test_definitive_failed_turn_with_settled_release_is_turn_failed(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.outcomes.append(ProbeTurnOutcome(None, "protocol_error", "released"))
    t = await session.probe_turn(s["id"], "q")
    assert t["failure"] == {"code": "protocol_error"} and t["outcome"] == "target_failed"
    assert t["agent_outcome"] == "infrastructure" and t["answer"] is None and t["steps"] == []
    h = await session.probe_session(s["id"])
    assert h["state"] == "closed" and h["closed_reason"] == "turn_failed" and h["tokens_incomplete"] is True


@pytest.mark.parametrize("failure", ["turn_timeout", "target_unreachable", "protocol_error"])
async def test_failed_turn_with_uncertain_release_is_release_failed(probe_env, failure):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.outcomes.append(ProbeTurnOutcome(None, failure, "uncertain"))
    t = await session.probe_turn(s["id"], "q")
    assert t["failure"] == {"code": failure}
    assert (await session.probe_session(s["id"]))["closed_reason"] == "release_failed"


async def test_fake_owner_refuses_a_timeout_that_settles_released(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.outcomes.append(ProbeTurnOutcome(None, "turn_timeout", "released"))
    with pytest.raises(AssertionError):
        await session.probe_turn(s["id"], "q")


async def test_uncertain_close_is_release_failed(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.close_answer = "uncertain"
    c = await session.probe_close(s["id"])
    assert c["state"] == "closed" and c["closed_reason"] == "release_failed"


async def test_uncertain_reset_records_release_failed_and_opens_nothing(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.close_answer = "uncertain"
    with pytest.raises(OwnerError) as caught:
        await session.probe_reset(s["id"])
    assert (caught.value.code, caught.value.stage) == ("cleanup_required", "before_start")
    assert (await session.probe_session(s["id"]))["closed_reason"] == "release_failed"
    assert len(owner.opened) == 1 and len((await session.probe_list("pr"))["sessions"]) == 1


def _ledger(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE requests(role TEXT, reserved INTEGER, settled INTEGER)")
    db.execute("INSERT INTO requests VALUES ('agent', 9000000000, 9000000000)")  # before the window
    db.commit()
    db.close()


def _charge(path, role, reserved, settled):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO requests VALUES (?, ?, ?)", (role, reserved, settled))
    db.commit()
    db.close()


async def test_gateway_cost_window_and_upper_bound(probe_env, tmp_path):
    session, owner = probe_env.session, probe_env.owner
    ledger = tmp_path / "gateway.sqlite3"
    _ledger(ledger)
    budget = {"ledger_path": str(ledger), "roles": ["agent"], "currency": "RUB"}
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 5, "max_cost": "1.0"}, budget=budget)
    assert s["budget"] == budget
    assert s["ceiling"]["cost"] == {"used": "0.000000000", "max": "1.0", "currency": "RUB"}

    def charge():
        _charge(ledger, "agent", 600_000_000, None)
        _charge(ledger, "judge", 5_000_000_000, 5_000_000_000)  # another role is not this turn's cost
    owner.on_turn = charge
    t = await session.probe_turn(s["id"], "q")
    assert t["cost"] == {"amount": "0.600000000", "currency": "RUB", "attribution": "ledger_window",
                         "upper_bound": True}
    assert t["ceiling"] == {"turns": {"used": 1, "max": 5}, "tokens": None,
                            "cost": {"used": "0.600000000", "max": "1.0", "currency": "RUB"}}
    owner.on_turn = lambda: _charge(ledger, "agent", 700_000_000, 500_000_000)
    t = await session.probe_turn(s["id"], "q")
    assert t["cost"]["amount"] == "0.500000000" and t["cost"]["upper_bound"] is False
    h = await session.probe_session(s["id"])
    assert h["ceiling"]["cost"]["used"] == "1.100000000" and h["cost_unavailable"] is False
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_ceiling_reached"


async def test_unreadable_ledger_runs_the_turn_and_blocks_the_next(probe_env, tmp_path):
    session = probe_env.session
    budget = {"ledger_path": str(tmp_path / "missing.sqlite3"), "roles": ["agent"], "currency": "USD"}
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 5, "max_cost": "1"}, budget=budget)
    t = await session.probe_turn(s["id"], "q")
    assert t["cost"] is None and t["answer"] == "ответ"
    assert (await session.probe_session(s["id"]))["cost_unavailable"] is True
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_ceiling_unverifiable"


async def test_reconcile_after_restart_closes_and_marks_interrupted(probe_env):
    session, projects = probe_env.session, probe_env.projects
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    sid = s["id"]
    await session.probe_turn(sid, "1")
    other = ProbeStore.open(probe_env.home)  # a turn the previous service began and never finished
    other.begin_turn(sid, 2, "2", "2026-10-02T00:00:00Z")
    other.close()
    # A restart: the new Owner generation holds the session's row as uncertain.
    restarted = ProjectSession(projects, preparation=None, owner=probe_env.owner.restarted(), pair=PAIR,
                               state_root=probe_env.home / "state")
    try:
        await restarted.probe_reconcile()
        h = await restarted.probe_session(sid)
    finally:
        restarted.close()
    assert h["state"] == "closed" and h["closed_reason"] == "target_gone"
    assert [x["turn_id"] for x in h["turns"]] == [sid + "/1", sid + "/2"]
    assert h["turns"][-1]["failure"] == {"code": "interrupted"} and h["turns"][-1]["request"] == {"content": "2"}
    assert h["turns"][-1]["ceiling"] == {"turns": {"used": 2, "max": 3}, "tokens": None, "cost": None}
    assert h["tokens_incomplete"] is True
    assert set(h["turns"][-1]) == TURN_KEYS
    stored = ProbeStore.open(probe_env.home)
    try:
        assert stored.turns(sid)[-1]["ceiling"] == h["turns"][-1]["ceiling"]
    finally:
        stored.close()


async def test_reconcile_uncertain_row_is_release_failed(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.states[s["id"]] = "uncertain"  # e.g. a target stop whose settlement was not positive
    await session.probe_reconcile()
    assert (await session.probe_session(s["id"]))["closed_reason"] == "release_failed"
    owner.states.clear()
    t = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.states[t["id"]] = "released"  # a stopped target released it
    h = await session.probe_session(t["id"])  # every probe method reconciles first
    assert h["state"] == "closed" and h["closed_reason"] == "target_gone"


async def test_seed_on_a_target_without_exact_seeds_is_invalid_request(probe_env):
    session, owner = probe_env.session, probe_env.owner
    owner.seed_mode = "unpaired"
    with pytest.raises(ProjectError) as caught:
        await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None, seed=5)
    assert caught.value.code == "invalid_request"
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None)
    assert s["target"]["seed_mode"] == "unpaired"


async def test_cancelled_caller_does_not_release_the_turn_lock_or_leave_the_turn_pending(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    entered, release = asyncio.Event(), asyncio.Event()

    def block():
        entered.set()
        return release.wait()
    owner.on_turn = block
    caller = asyncio.create_task(session.probe_turn(s["id"], "q"))
    await entered.wait()
    caller.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not caller.done() and session._probe_lock.locked()  # the Owner turn still runs under the lock
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not session._probe_lock.locked()
    h = await session.probe_session(s["id"])
    assert h["state"] == "open" and [x["answer"] for x in h["turns"]] == ["ответ"]
    owner.on_turn = None
    t = await session.probe_turn(s["id"], "q2")
    assert t["turn_id"] == s["id"] + "/2" and t["ceiling"]["turns"]["used"] == 2


async def _cancel_while_opening(owner, call):
    """Run ``call`` until the Owner open is admitted, cancel the caller, then let the open finish."""
    owner.open_gate = asyncio.Event()
    caller = asyncio.create_task(call)
    while not owner.opened or owner.states.get(owner.opened[-1][1]) != "admitted":
        await asyncio.sleep(0)
    caller.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    owner.open_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    return owner.opened[-1][1]


async def test_cancelled_start_records_the_session_the_owner_opened(probe_env):
    """Codex r1 A1: the transition finishes before the cancellation is answered, so no Owner probe is left
    without history."""
    session, owner = probe_env.session, probe_env.owner
    opened = await _cancel_while_opening(
        owner, session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None))
    assert owner.states[opened] == "open"
    listed = (await session.probe_list("pr"))["sessions"]
    assert [(x["id"], x["state"]) for x in listed] == [(opened, "open")]
    assert (await session.probe_turn(opened, "q"))["turn_id"] == opened + "/1"


async def test_cancelled_reset_records_the_replacement_the_owner_opened(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    replacement = await _cancel_while_opening(owner, session.probe_reset(s["id"]))
    assert replacement != s["id"] and owner.states[replacement] == "open"
    listed = {x["id"]: x for x in (await session.probe_list("pr"))["sessions"]}
    assert (listed[s["id"]]["state"], listed[s["id"]]["closed_reason"]) == ("closed", "reset")
    assert (listed[replacement]["state"], listed[replacement]["reset_of"]) == ("open", s["id"])
    assert not session._probe_lock.locked()


async def test_cancelled_start_whose_history_write_fails_closes_the_owner_probe(probe_env, monkeypatch):
    session, owner = probe_env.session, probe_env.owner
    store = session._probes()

    def full(row):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(store, "create_session", full)
    opened = await _cancel_while_opening(
        owner, session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None))
    assert owner.closed == [opened] and owner.states[opened] == "released"
    assert (await session.probe_list("pr"))["sessions"] == []


@pytest.mark.parametrize("state", ["admitted", "releasing", "open"])
async def test_reconcile_leaves_transient_owner_states_alone(probe_env, state):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.states[s["id"]] = state
    await session.probe_reconcile()
    assert (await session.probe_session(s["id"]))["state"] == "open"
    owner.states.pop(s["id"])  # no row at all: a new generation
    await session.probe_reconcile()
    assert (await session.probe_session(s["id"]))["closed_reason"] == "target_gone"


async def test_failed_reconcile_after_an_owner_turn_error_keeps_the_owner_answer(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)

    async def gone(session_id, content, *, timeout):
        owner.states_error = RuntimeError("reconcile failed")
        raise OwnerError("not_found", "probe")
    owner.probe_turn = gone
    with pytest.raises(ProjectError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "probe_session_closed"

    async def stopping(session_id, content, *, timeout):
        owner.states_error = RuntimeError("reconcile failed")
        raise OwnerError("service_stopping", "probe")
    owner.states_error = None
    owner.probe_turn = stopping
    with pytest.raises(OwnerError) as caught:
        await session.probe_turn(s["id"], "q")
    assert caught.value.code == "service_stopping"


@pytest.mark.parametrize("seed", [-1, 2**31, 2**63])
async def test_seed_out_of_the_owner_range_is_invalid_request_before_any_owner_call(probe_env, seed):
    with pytest.raises(ProjectError) as caught:
        await probe_env.session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None, seed=seed)
    assert caught.value.code == "invalid_request" and probe_env.owner.opened == []


async def test_seed_at_the_range_edges_is_passed_through(probe_env):
    for seed in (0, 2**31 - 1):
        s = await probe_env.session.probe_start("pr", "im", ceiling={"max_turns": 1}, budget=None, seed=seed)
        assert s["seed"] == {"requested": seed, "effective": seed}


@pytest.mark.parametrize("prior", ["open", "uncertain"])
async def test_session_of_an_earlier_generation_is_target_gone_whatever_the_owner_holds(probe_env, prior):
    session, projects, owner = probe_env.session, probe_env.projects, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    owner.states[s["id"]] = prior  # "uncertain": e.g. a turn the Owner close interrupted
    new = owner.restarted()
    assert (await new.probe_states())[s["id"]] == "uncertain"
    restarted = ProjectSession(projects, preparation=None, owner=new, pair=PAIR, state_root=probe_env.home / "state")
    try:
        await restarted.probe_reconcile()
        h = await restarted.probe_session(s["id"])
    finally:
        restarted.close()
    assert h["state"] == "closed" and h["closed_reason"] == "target_gone"


async def test_shutdown_refuses_queued_probe_calls_and_leaves_an_interrupted_turn_pending(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    entered, release = asyncio.Event(), asyncio.Event()

    def block():
        entered.set()
        return release.wait()
    owner.on_turn = block
    running = asyncio.create_task(session.probe_turn(s["id"], "first"))
    await entered.wait()
    owner.on_turn = None
    queued = [asyncio.create_task(call) for call in (
        session.probe_turn(s["id"], "second"), session.probe_close(s["id"]), session.probe_reset(s["id"]))]
    await asyncio.sleep(0)
    session.stop_probes()  # shutdown begins
    session.interrupt_probes()  # the deadline passed: the Owner close interrupts the running turn
    owner.outcomes.append(ProbeTurnOutcome(None, "target_unreachable", "uncertain"))
    owner.states[s["id"]] = "uncertain"
    release.set()
    for call in (running, *queued):
        with pytest.raises(OwnerError) as caught:
            await call
        assert caught.value.code == "service_stopping"
    assert [content for _, content, _ in owner.turned] == ["first"] and owner.closed == []
    stored = ProbeStore.open(probe_env.home)
    try:  # nothing recorded: the turn is pending and the session open, for the next start to reconcile
        assert stored.pending_turns() == [(s["id"], 1)]
        assert stored.session(s["id"])["state"] == "open"
    finally:
        stored.close()
    restarted = ProjectSession(probe_env.projects, preparation=None, owner=owner.restarted(), pair=PAIR,
                               state_root=probe_env.home / "state")
    try:
        h = await restarted.probe_session(s["id"])
    finally:
        restarted.close()
    assert h["closed_reason"] == "target_gone" and h["turns"][-1]["failure"] == {"code": "interrupted"}


async def test_a_turn_answered_during_shutdown_is_recorded(probe_env):
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    entered, release = asyncio.Event(), asyncio.Event()

    def block():
        entered.set()
        return release.wait()
    owner.on_turn = block
    running = asyncio.create_task(session.probe_turn(s["id"], "q"))
    await entered.wait()
    session.interrupt_probes()  # only a failed turn is left pending: an answer is still an answer
    release.set()
    assert (await running)["answer"] == "ответ"


async def test_abandoned_probe_calls_stop_waiting_on_the_owner_and_leave_the_history_alone(probe_env):
    """Codex r2 R2-2 (§5.1): once the service lets go of the probe calls after the Owner close, a call still
    waiting on the Owner answers service_stopping at once (its turn stays pending for the next start), and no
    later probe call writes or reopens the history."""
    session, owner = probe_env.session, probe_env.owner
    s = await session.probe_start("pr", "im", ceiling={"max_turns": 3}, budget=None)
    never = asyncio.Event()
    owner.on_turn = never.wait  # a turn the Owner never answers
    turn = asyncio.create_task(session.probe_turn(s["id"], "q"))
    await asyncio.sleep(0.05)
    assert not turn.done()
    session.abandon_probes()
    with pytest.raises(OwnerError) as caught:
        await asyncio.wait_for(turn, 1)
    assert caught.value.code == "service_stopping"
    store = session._probe_store
    assert store.pending_turns() == [(s["id"], 1)] and store.session(s["id"])["state"] == "open"
    session.close()
    with pytest.raises(OwnerError) as caught:
        await session.probe_session(s["id"])
    assert caught.value.code == "service_stopping"
    assert session._probe_store is None  # not reopened
