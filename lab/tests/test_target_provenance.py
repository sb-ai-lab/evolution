"""Turn provenance crosses the real Core server without changing old agents."""
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from evolution.tree import ArtifactPolicyV1, BackendError, Message, TargetServer
from evolution.tree.execution.wire_v2 import PREFIX, STRATEGY, canonical_body
from evolution.tree.target import target_server

from evolution_lab.target import AgentTurn, KEEP, TurnReason, backend


class Agent:
    def __init__(self, turn):
        self.answer = turn

    def describe(self):
        return {"agent": "synthetic"}

    async def open(self, public, *, session_id, seed):
        return None

    async def turn(self, message, state, *, session_id, seed):
        return self.answer

    async def close_session(self, session_id):
        pass

    async def aclose(self):
        pass


def make_backend(turn, secrets=()):
    async def factory(view):
        return Agent(turn)
    return backend(factory, secrets=secrets)


def server_turn(tmp_path, turn, monkeypatch, limit=10000):
    monkeypatch.setattr(target_server, "time", SimpleNamespace(monotonic=lambda: 0))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    policy = ArtifactPolicyV1((), (), ())
    server = TargetServer(make_backend(turn), allowed_artifact_root=tmp_path, policy=policy,
                          seed_mode="none", target_id="synthetic", contract_digest="contract",
                          settings_hash="settings", runtime_ref_digest="runtime", max_output_bytes=limit)
    def post(path, body):
        return server.dispatch("POST", PREFIX + path, json.dumps(body).encode())
    try:
        status, body = post("/instances", {"request_id": "create", "instance_id": "run-0123456789abcdef0123456789abcdef", "candidate_root": str(candidate), "policy": policy.to_payload(), "strategy": STRATEGY})
        assert status == 200
        iid = json.loads(body)["instance_id"]
        status, body = post(f"/instances/{iid}/sessions", {"request_id": "open", "public": {}, "seed": None})
        assert status == 200
        sid = json.loads(body)["session_id"]
        status, body = post(f"/instances/{iid}/sessions/{sid}/turns", {"request_id": "turn", "message": {"content": "x", "fields": {}}})
        assert status == 200
        return body
    finally:
        server.close()


def test_provenance_validates_normalizes_and_is_frozen():
    from evolution_lab.target import TurnProvenance
    used = ["skills/A.md", "component"]
    provenance = TurnProvenance(used)
    used.append("skills/B.md")
    assert provenance.used == ("skills/A.md", "component")
    assert provenance.to_payload() == {"used": ["skills/A.md", "component"]}
    with pytest.raises(FrozenInstanceError):
        provenance.used = ()
    for invalid in ([1], [""], ["a"] * 1025, "skills/A.md"):
        with pytest.raises(ValueError):
            TurnProvenance(invalid)


def test_positional_agent_turn_and_bad_provenance():
    reason = TurnReason("agent", "answered")
    turn = AgentTurn("ok", "hello", (), KEEP, None, False, reason)
    assert turn.reason == reason and turn.provenance is None
    with pytest.raises(ValueError, match="provenance"):
        AgentTurn("ok", "hello", provenance={"used": []})


@pytest.mark.parametrize("outcome,text,expected", [("ok", "hello", "ok"), ("ok", "", "target_failed"), ("candidate_failed", "", "candidate_failed"), ("target_failed", "", "target_failed")])
def test_real_server_carries_provenance_for_every_outcome(tmp_path, monkeypatch, outcome, text, expected):
    from evolution_lab.target import TurnProvenance
    body = server_turn(tmp_path, AgentTurn(outcome, text, provenance=TurnProvenance(["skills/A.md"])), monkeypatch)
    payload = json.loads(body)
    assert payload["provenance"] == {"used": ["skills/A.md"]}
    assert payload["outcome"] == expected


def test_dev72_turn_bytes_unchanged(tmp_path, monkeypatch):
    body = server_turn(tmp_path, AgentTurn("ok", "hello"), monkeypatch)
    assert body == b'{"events":[{"content":"hello","type":"assistant_message"}],"latency_ms":0,"outcome":"ok","truncated":false,"usage":null}'


@pytest.mark.parametrize("used", [["skills/A.md"], ["skills/é.md", "component"],
                                  [f"skills/{i}.md" for i in range(50)]])
@pytest.mark.parametrize("offset", [-1, 0, 1, 50, 1000])
def test_server_fits_provenance_whole_at_mandatory_boundary(tmp_path, monkeypatch, used, offset):
    from evolution_lab.target import TurnProvenance

    provenance = {"used": used}
    # Independently construct Core's conservative turn body, including the
    # widest latency/outcome and the complete final assistant message.
    minimum = {"events": [{"type": "assistant_message", "content": "answer" * 100}], "usage": None,
               "latency_ms": 99999999999999999999, "outcome": "candidate_failed",
               "truncated": False, "provenance": provenance,
               "reason": {"category": "agent", "code": "answered"}}
    boundary = len(canonical_body(minimum))
    turn = AgentTurn("ok", "answer" * 100, events=({"type": "tool_call", "name": "x"},),
                     reason=TurnReason("agent", "answered"), provenance=TurnProvenance(used))
    payload = json.loads(server_turn(tmp_path, turn, monkeypatch, boundary + offset))
    assert payload.get("provenance") == (provenance if offset >= 0 else None)
    assert payload["reason"] == minimum["reason"]
    assert payload["events"][-1]["type"] == "assistant_message"
    if offset == 0:
        assert payload["events"] == [{"type": "assistant_message", "content": "answer" * 100}]
        assert payload["truncated"] is True


def test_launch_secret_in_used_is_refused_before_state_commit(tmp_path):
    from evolution_lab.target import TurnProvenance
    from test_target_backend import tree
    turn = AgentTurn("ok", "hello", state={"changed": True}, provenance=TurnProvenance(["skills/secret-token.md"]))
    instance = make_backend(turn, secrets=("secret-token",)).load(tree(tmp_path))
    try:
        session = instance.open({}, None)
        with pytest.raises(BackendError, match="turn provenance carries a launch secret"):
            session.turn(Message("x"))
        assert session._state is None
    finally:
        instance.close()


def test_secret_matching_list_index_does_not_reject_unrelated_used(tmp_path):
    from evolution_lab.target import TurnProvenance
    from test_target_backend import tree
    turn = AgentTurn("ok", "hello", provenance=TurnProvenance(["skills/A.md"]))
    instance = make_backend(turn, secrets=("0",)).load(tree(tmp_path))
    try:
        reply = instance.open({}, None).turn(Message("x"))
        assert reply.result.provenance == {"used": ["skills/A.md"]}
    finally:
        instance.close()


def test_provenance_does_not_shorten_reply_that_fits_without_it(tmp_path, monkeypatch):
    from evolution_lab.target import TurnProvenance
    text = "answer " * 1120
    turn = AgentTurn("ok", text, provenance=TurnProvenance([f"skills/{i}.md" for i in range(20)]))
    payload = json.loads(server_turn(tmp_path, turn, monkeypatch, 8192))
    assert payload["events"] == [{"type": "assistant_message", "content": text}]
    assert "provenance" not in payload
    assert payload["truncated"] is False
