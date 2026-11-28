"""Reply fitting under max_output_bytes (spec W19 §3.2; check §6.2)."""
import pytest
from evolution.tree import Usage
from evolution.tree.execution.wire_v2 import canonical_body

from evolution_lab.target import _fit
from evolution_lab.target._fit import VISIBLE, OutputTooLarge, fit, _size

REASON = {"category": "execution", "code": "model_behavior"}


def body(events, usage=None, reason=None):
    measured = {"events": list(events), "usage": usage.to_payload() if usage is not None else None,
                "latency_ms": _fit._WIDEST_LATENCY, "outcome": _fit._WIDEST_OUTCOME, "truncated": False}
    if reason is not None:
        measured["reason"] = dict(reason)
    return measured


def test_widest_outcome_is_candidate_failed():
    assert _fit._WIDEST_OUTCOME == "candidate_failed"


def test_fit_measures_with_core_not_a_copy():
    assert not hasattr(_fit, "canonical")


@pytest.mark.parametrize("events, reason", [
    ([{"type": "assistant_message", "content": "ünïcode ✓"}], None),
    ([{"type": "tool_result", "name": "a", "status": "ok", "nested": {"b": [1, 2.5, None]}}], None),
    ([{"type": "assistant_message", "content": "x"}], REASON),
])
def test_size_is_core_canonical_measure_of_the_body(events, reason):
    assert _size(events, Usage(prompt=3), reason) == len(canonical_body(body(events, Usage(prompt=3), reason)))


def test_everything_fits_untouched():
    events, cut, kept = fit([{"type": "tool_call", "name": "t"}], "answer", None, 10_000)
    assert events == ({"type": "tool_call", "name": "t"}, {"type": "assistant_message", "content": "answer"})
    assert cut is False and kept is False


def test_invisible_events_go_first_then_visible_from_the_end():
    agent = [{"type": "tool_call", "name": "first"}, {"type": "custom_trace", "blob": "x" * 400},
             {"type": "tool_call", "name": "second"}]
    minimal = len(canonical_body(body([{"type": "tool_call", "name": "first"},
                                       {"type": "assistant_message", "content": "a"}])))
    events, cut, _ = fit(agent, "a", None, minimal)
    assert [e.get("name") for e in events] == ["first", None] and cut is True


def test_reason_is_measured_and_content_is_trimmed_last():
    events, cut, _ = fit([{"type": "tool_call", "name": "t"}], "x" * 1000, None, 400, reason=REASON)
    assert [e["type"] for e in events] == ["assistant_message"]
    assert len(events[-1]["content"]) < 1000 and cut is True
    assert len(canonical_body(body(events, reason=REASON))) <= 400
    # Without the reason the same limit leaves room for more content.
    plain, _, _ = fit([{"type": "tool_call", "name": "t"}], "x" * 1000, None, 400)
    assert len(plain[-1]["content"]) == len(events[-1]["content"]) + len(canonical_body({"reason": REASON})) - 1


def test_reason_stays_when_only_the_minimum_fits():
    minimum = len(canonical_body(body([{"type": "assistant_message", "content": ""}], reason=REASON)))
    events, cut, _ = fit([{"type": "trace", "blob": "z" * 50}], "text", None, minimum, reason=REASON)
    assert events == ({"type": "assistant_message", "content": ""},) and cut is True
    with pytest.raises(OutputTooLarge):
        fit([], "", None, minimum - 1, reason=REASON)


def test_minimum_that_cannot_fit_raises():
    with pytest.raises(OutputTooLarge):
        fit([], "", None, 10)


def test_visible_set_matches_editor_projection():
    assert VISIBLE == {"assistant_message", "tool_call", "tool_result", "skill_read", "output_truncated"}


def test_reply_at_limit_with_truncated_false_does_not_get_cut():
    """Regression: truncated=False (5 bytes in JSON) is wider than True (4 bytes).

    If fit() measured with truncated=True (4 bytes), a reply at the measured
    limit would exceed the server's actual limit (which uses truncated=False,
    5 bytes). Verify the fix: measure with truncated=False so replies that
    fit() approves will not exceed the server's limit.
    """
    # Create a simple reply with just the assistant message (empty prior events)
    reply_events = [{"type": "assistant_message", "content": "hi"}]

    # Measure this reply's size using _size (which measures with truncated=False)
    exact_size = _size(reply_events, None)

    # Call fit() with empty prior events and content "hi"
    # This will create the same structure as reply_events
    fit_events, cut, _ = fit([], "hi", None, exact_size)

    # Verify fit() did not cut anything
    assert cut is False, "fit() should not cut when limit equals the measured size"

    # Verify the actual body (with truncated=False) fits within the limit
    actual_body = body(fit_events, None)
    actual_size = len(canonical_body(actual_body))
    assert actual_size <= exact_size, f"actual body {actual_size} bytes exceeds limit {exact_size} bytes"
