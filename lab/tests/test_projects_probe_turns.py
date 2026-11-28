import json
from types import MappingProxyType, SimpleNamespace

import pytest
from evolution.tree.execution.interaction import TurnResult
from evolution.tree.execution.usage import Usage

from evolution_lab.projects.probe_turns import agent_outcome, check_ceiling, turn_record


@pytest.mark.parametrize("outcome,reason,failure,expected", [
    ("ok", {"category": "agent_outcome", "code": "answered"}, None, "answered"),
    ("ok", {"category": "agent_outcome", "code": "handoff"}, None, "handoff"),
    ("ok", {"category": "agent_outcome", "code": "off_topic"}, None, "off_topic"),
    ("ok", {"category": "agent_outcome", "code": "gave_up"}, None, "gave_up"),
    ("ok", {"category": "agent_outcome", "code": "teleport"}, None, "unspecified"),
    ("ok", {"category": "other", "code": "handoff"}, None, "unspecified"),
    ("ok", None, None, "unspecified"),
    ("candidate_failed", {"category": "agent_outcome", "code": "answered"}, None, "error"),
    ("target_failed", None, None, "infrastructure"),
    ("target_failed", None, "turn_timeout", "infrastructure"),
])
def test_agent_outcome_table(outcome, reason, failure, expected):
    assert agent_outcome(outcome, reason, failure) == expected


def test_turn_record_answer_steps_and_usage():
    result = TurnResult(events=({"type": "tool_call", "name": "x"}, {"type": "assistant_message", "content": "hi"}),
                        usage=Usage(10, 5, 0, 15), latency_ms=12, outcome="ok",
                        reason={"category": "agent_outcome", "code": "answered"})
    rec = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q", result=result,
                      failure=None, cost=None)
    assert rec["answer"] == "hi" and rec["steps"][0] == {"index": 0, "type": "tool_call", "data": {"name": "x"}}
    assert rec["usage"] == {"prompt": 10, "completion": 5, "cached": 0, "total": 15}
    assert rec["turn_id"] == "a" * 32 + "/1" and rec["agent_outcome"] == "answered" and rec["failure"] is None
    assert rec["schema"] == "lab-probe-turn/1" and rec["latency_ms"] == 12 and rec["request"] == {"content": "q"}
    assert "ceiling" not in rec


def test_turn_record_without_assistant_message_has_null_answer():
    result = TurnResult(events=({"type": "tool_call", "name": "x"},), usage=None, latency_ms=1, outcome="ok")
    rec = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q", result=result,
                      failure=None, cost=None)
    assert rec["answer"] is None and rec["usage"] is None and rec["agent_outcome"] == "unspecified"


def test_nested_frozen_event_data_is_plain_json():
    arguments = MappingProxyType({"a": (MappingProxyType({"b": (1, 2)}),)})
    result = TurnResult(events=({"type": "tool_call", "name": "x", "arguments": arguments},),
                        usage=None, latency_ms=1, outcome="ok")
    rec = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q", result=result,
                      failure=None, cost=None)
    assert rec["steps"][0]["data"]["arguments"] == {"a": [{"b": [1, 2]}]}
    assert json.loads(json.dumps(rec, allow_nan=False)) == rec


@pytest.mark.parametrize("data", [{"x": float("nan")}, {1: "x"}], ids=["nan", "non_string_key"])
def test_unencodable_event_becomes_protocol_error(data):
    # Core rejects these at construction; simulate a malformed boundary result to test Lab's defensive path.
    result = SimpleNamespace(events=({"type": "tool_call", "arguments": data},
                                     {"type": "assistant_message", "content": "hi"}),
                             usage=Usage(10, 5, 0, 15), latency_ms=1, outcome="ok", reason=None)
    rec = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q", result=result,
                      failure=None, cost=None)
    assert rec["failure"] == {"code": "protocol_error"}
    assert rec["outcome"] == "target_failed" and rec["steps"] == [] and rec["answer"] is None
    assert rec["agent_outcome"] == "infrastructure" and rec["usage"] is None and rec["reason"] is None
    assert rec["latency_ms"] is None and rec["truncated"] is False
    assert json.loads(json.dumps(rec, allow_nan=False)) == rec


def test_failed_turn_record_shape():
    rec = turn_record(session_id="a" * 32, n=2, started_at="t0", finished_at="t1", content="q", result=None,
                      failure="turn_timeout", cost=None)
    assert rec["outcome"] == "target_failed" and rec["answer"] is None and rec["steps"] == []
    assert rec["failure"] == {"code": "turn_timeout"} and rec["agent_outcome"] == "infrastructure"


@pytest.mark.parametrize("ceiling,used,expected", [
    ({"max_turns": 2}, {"turns": 1}, None),
    ({"max_turns": 2}, {"turns": 2}, "probe_ceiling_reached"),
    ({"max_turns": 9, "max_tokens": 100}, {"turns": 1, "tokens": 100}, "probe_ceiling_reached"),
    ({"max_turns": 9, "max_tokens": 100}, {"turns": 1, "tokens": 0, "tokens_incomplete": True}, "probe_ceiling_unverifiable"),
    ({"max_turns": 9}, {"turns": 1, "tokens_incomplete": True, "cost_unavailable": True}, None),
    ({"max_turns": 9, "max_cost": "1.00"}, {"turns": 1, "cost_units": 10**9}, "probe_ceiling_reached"),
    ({"max_turns": 9, "max_cost": "1.00"}, {"turns": 1, "cost_units": 10**9 - 1}, None),
    ({"max_turns": 9, "max_cost": "1.00"}, {"turns": 1, "cost_unavailable": True}, "probe_ceiling_unverifiable"),
])
def test_check_ceiling(ceiling, used, expected):
    assert check_ceiling(ceiling, {"tokens": 0, "tokens_incomplete": False, "cost_units": 0,
                                   "cost_unavailable": False, **used}) == expected


def test_truncated_is_derived_from_the_output_truncated_event():
    events = ({"type": "assistant_message", "content": "hi"}, {"type": "output_truncated"})
    rec = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q",
                      result=TurnResult(events=events, usage=None, latency_ms=1, outcome="ok"), failure=None, cost=None)
    assert rec["truncated"] is True and rec["steps"][-1] == {"index": 1, "type": "output_truncated", "data": {}}
    plain = turn_record(session_id="a" * 32, n=1, started_at="t0", finished_at="t1", content="q",
                        result=TurnResult(events=events[:1], usage=None, latency_ms=1, outcome="ok"), failure=None, cost=None)
    assert plain["truncated"] is False
