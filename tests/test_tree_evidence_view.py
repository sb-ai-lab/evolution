"""Full episode rendering and explicit middle cuts (ADR-0051)."""

from dataclasses import replace

from evolution.tree.evidence_view import (
    cut_items,
    cut_text,
    is_marker,
    render_event,
    render_turn,
    render_turns,
    text_of,
)
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE


def turn(index=0, events=(), **message):
    return {
        "index": index,
        "message": {"content": "q", "fields": {}, **message},
        "events": list(events),
        "outcome": "ok",
        "reason": None,
    }


def test_every_event_type_and_key_is_shown_and_non_strings_are_canonical_json():
    event = {
        "type": "tool_result",
        "name": "list_service_requests",
        "result": [],
        "ms": 12,
        "extra": {"b": 1, "a": None},
    }
    assert render_event(event, DEFAULT_EVIDENCE, ()) == {
        "type": "tool_result",
        "name": "list_service_requests",
        "result": "[]",
        "ms": "12",
        "extra": '{"a":null,"b":1}',
    }
    assert render_event({"type": "custom_thing", "x": "y"}, DEFAULT_EVIDENCE, ()) == {
        "type": "custom_thing",
        "x": "y",
    }


def test_a_60_event_turn_keeps_both_ends_and_both_assistant_messages():
    events = [{"type": "tool_call", "name": f"t{i}"} for i in range(60)]
    events[10] = {"type": "assistant_message", "content": "first"}
    events[40] = {"type": "assistant_message", "content": "last"}
    shown = render_turn(turn(events=events), replace(DEFAULT_EVIDENCE, events_per_turn=4), ())[
        "events"
    ]
    assert [e for e in shown if not is_marker(e)] == [events[i] for i in (0, 1, 10, 40, 58, 59)]
    assert [e for e in shown if is_marker(e)] == [
        {"cut": {"events": 8}},
        {"cut": {"events": 29}},
        {"cut": {"events": 17}},
    ]


def test_a_long_object_result_is_cut_in_its_canonical_text_head_and_tail():
    value = {"rows": [{"id": i} for i in range(2000)]}
    shown = render_event(
        {"type": "tool_result", "result": value}, replace(DEFAULT_EVIDENCE, text_chars=100), ()
    )["result"]
    text = text_of(value)
    assert shown == text[:50] + f"\n…[{len(text) - 100} characters cut]…\n" + text[-50:]


def test_a_long_conversation_keeps_first_and_last_turns():
    shown, omitted = render_turns(
        [turn(i) for i in range(20)], replace(DEFAULT_EVIDENCE, turns=4), ()
    )
    assert [t["index"] for t in shown if not is_marker(t)] == [0, 1, 18, 19]
    assert shown[2] == {"cut": {"turns": 16}}
    assert omitted == 16


def test_turns_one_keeps_the_last_turn():
    shown, omitted = render_turns(
        [turn(i) for i in range(3)], replace(DEFAULT_EVIDENCE, turns=1), ()
    )
    assert shown[0] == {"cut": {"turns": 2}}
    assert shown[1]["index"] == 2 and omitted == 2


def test_turn_input_shows_content_and_fields_cut_at_input_chars():
    settings = replace(DEFAULT_EVIDENCE, input_chars=4)
    shown = render_turn(turn(content="abcdef", fields={"x": 123}), settings, ())
    assert shown["input"] == {
        "content": "ab\n…[2 characters cut]…\nef",
        "fields": '{"\n…[5 characters cut]…\n3}',
    }
    assert render_turn(turn(), settings, ())["input"]["fields"] is None
    assert (
        render_turn(turn(content="abc"), replace(settings, input_chars=0), ())["input"]["content"]
        == "\n…[3 characters cut]…\n"
    )


def test_reason_is_shown_with_every_key():
    source = {**turn(), "reason": {"code": "bad", "details": {"n": 2}}}
    assert render_turn(source, DEFAULT_EVIDENCE, ())["reason"] == {
        "code": "bad",
        "details": '{"n":2}',
    }


def test_a_secret_straddling_a_cut_is_redacted_whole():
    secret = "sk-live-0123456789abcdef"
    text = "a" * 45 + secret + "b" * 100
    shown = render_event(
        {"type": "assistant_message", "content": text},
        replace(DEFAULT_EVIDENCE, text_chars=100),
        (secret,),
    )["content"]
    assert "sk-live" not in shown and "89abcdef" not in shown
    assert "[" in shown


def test_every_rendered_string_redacts_before_cut():
    shown = render_turn(
        {
            **turn(content="secret", fields={"key": "secret"}),
            "reason": {"code": "secret"},
            "outcome": "secret",
            "events": [{"type": "custom", "secret": "secret"}],
        },
        DEFAULT_EVIDENCE,
        ("secret",),
    )
    assert "secret" not in text_of(shown)
    assert shown["input"]["content"] == "[REDACTED]"


def test_a_target_event_named_cut_is_not_a_marker():
    assert not is_marker(render_event({"type": "cut", "cut": {"events": 3}}, DEFAULT_EVIDENCE, ()))
    assert is_marker({"cut": {"events": 3}})


def test_no_cut_and_odd_limits_and_empty_values():
    assert cut_text("abc", 3) == ("abc", 0)
    assert cut_text("abcdef", 3) == ("a\n…[3 characters cut]…\nef", 3)
    assert cut_items([1, 2], 2, unit="events") == [1, 2]
    assert cut_items([], 0, unit="events") == []
    assert cut_items([0, 1, 2, 3, 4], 3, unit="events") == [0, {"cut": {"events": 2}}, 3, 4]
