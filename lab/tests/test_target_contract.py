"""Neutral turn contract (spec W19 §3.1)."""
import pytest

from evolution_lab.target import KEEP, OUTCOMES, AgentTurn, LaunchContext, TurnReason


def test_outcomes_on_core_a16():
    assert OUTCOMES == frozenset({"ok", "candidate_failed", "target_failed"})


def test_candidate_failed_is_an_outcome():
    assert AgentTurn("candidate_failed", "", reason=TurnReason("execution", "model_behavior")).outcome == "candidate_failed"


@pytest.mark.parametrize("category, code", [("", "x"), ("ok", "bad code"), ("a" * 65, "x"), (1, "x"), ("x", None)])
def test_reason_is_identifiers(category, code):
    with pytest.raises(ValueError):
        TurnReason(category, code)


def test_reason_is_frozen_and_allowed_on_any_outcome():
    reason = TurnReason("a.b_c-1", "Z" * 64)
    for outcome in OUTCOMES:
        assert AgentTurn(outcome, "", reason=reason).reason is reason
    assert AgentTurn("ok", "").reason is None
    with pytest.raises(AttributeError):
        reason.code = "other"


def test_reason_keeps_its_legacy_positional_argument():
    # Provenance was appended in dev73; pre-dev73 positional callers retain
    # their reason and do not start claiming provenance.
    reason = TurnReason("agent", "answered")
    turn = AgentTurn("ok", "done", (), KEEP, None, False, reason)
    assert turn.reason is reason
    assert turn.provenance is None


def test_agent_turn_defaults_keep_state_and_copies_events():
    source = [{"type": "tool_call", "name": "lookup_order"}]
    turn = AgentTurn("ok", "done", events=source)
    source[0]["name"] = "changed"
    assert turn.state is KEEP
    assert turn.events == ({"type": "tool_call", "name": "lookup_order"},)
    assert turn.truncated is False and turn.usage is None


@pytest.mark.parametrize("kwargs", [
    dict(outcome="agent_failed", text=""),
    dict(outcome="ok", text=None),
    dict(outcome="ok", text="", events=[{"name": "no type"}]),
    dict(outcome="ok", text="", truncated=1),
    dict(outcome="ok", text="", usage={"prompt": 1}),
    dict(outcome="candidate_failed", text="", reason={"category": "a", "code": "b"}),
])
def test_agent_turn_rejects_malformed(kwargs):
    with pytest.raises(ValueError):
        AgentTurn(**kwargs)


def test_keep_is_a_singleton():
    assert type(KEEP)() is KEEP and repr(KEEP) == "KEEP"


def test_launch_context_is_read_only(tmp_path):
    context = LaunchContext(env={"A": "1"}, secrets={"TOKEN": "s"}, work_directory=tmp_path)
    with pytest.raises(TypeError):
        context.env["B"] = "2"


def test_import_does_not_pull_agent_frameworks():
    import subprocess
    import sys
    from pathlib import Path
    import evolution_lab

    # A fresh interpreter tests the import boundary without replacing SDK classes
    # already held by other tests (including AgentTurn and TurnProvenance).
    package_root = str(Path(evolution_lab.__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, "-I", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
import evolution.tree  # Core may import what it likes.
before = set(sys.modules)
import evolution_lab.target
added = {name.split('.')[0] for name in set(sys.modules) - before}
assert not added & {'sylph', 'pydantic_ai', 'openai', 'anthropic'}, added
""", package_root], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_json_value_is_the_public_state_type():
    """Spec §3.1: session state is `JsonValue | None` (plus `KEEP` on a turn)."""
    import typing

    from evolution_lab import target
    from evolution_lab.target import JsonValue, TurnAgent

    assert "JsonValue" in target.__all__
    def shape(hint):
        # Forward references resolve one level deep, so compare the outer
        # shape by name.
        return {(typing.get_origin(arg) or arg).__name__ for arg in typing.get_args(hint)}

    json_shape = {"NoneType", "bool", "int", "float", "str", "list", "dict"}
    assert shape(JsonValue) == json_shape
    assert shape(typing.get_type_hints(TurnAgent.open)["return"]) == json_shape
    assert shape(typing.get_type_hints(TurnAgent.turn)["state"]) == json_shape
    assert shape(typing.get_type_hints(AgentTurn)["state"]) == json_shape | {"_Keep"}


def test_agent_outcome_vocabulary_is_public_and_closed():
    import evolution_lab.target as target
    assert target.AGENT_OUTCOME_CATEGORY == "agent_outcome"
    assert target.AGENT_OUTCOMES == frozenset({"answered", "off_topic", "handoff", "gave_up"})
    assert "AGENT_OUTCOME_CATEGORY" in target.__all__ and "AGENT_OUTCOMES" in target.__all__
    # every declared code is a valid TurnReason code
    for code in target.AGENT_OUTCOMES:
        target.TurnReason(target.AGENT_OUTCOME_CATEGORY, code)
