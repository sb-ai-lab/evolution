"""Tests for the mechanical edit-op pipeline (parse / apply / select)."""

from __future__ import annotations

import pytest

from evolution.evolve.edit_ops import (
    EditOp,
    _inside_fence,
    apply_edits,
    edit_budget,
    edit_dedup_body_threshold,
    edit_dedup_topic_enabled,
    edit_repair_insert_enabled,
    parse_edits,
    select_edits,
)

# ---------------------------------------------------------------------------
# EditOp validation
# ---------------------------------------------------------------------------


def test_append_needs_no_target() -> None:
    EditOp(op="append", content="new text")


def test_replace_requires_target() -> None:
    try:
        EditOp(op="replace", content="x")
    except Exception as exc:
        assert "target" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_insert_after_requires_target_and_content() -> None:
    try:
        EditOp(op="insert_after", target="anchor")
    except Exception as exc:
        assert "content" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_delete_requires_only_target() -> None:
    EditOp(op="delete", target="anchor")


def test_defaults() -> None:
    op = EditOp(op="append", content="x")
    assert op.target == ""
    assert op.rationale == ""
    assert op.evidence_refs == []


# ---------------------------------------------------------------------------
# apply_edits — full op x found/missing matrix
# ---------------------------------------------------------------------------


def test_apply_append() -> None:
    body, report = apply_edits("original", [EditOp(op="append", content="added")])
    assert body == "original\n\nadded"
    assert report == [
        {"op": "append", "target_head": "", "status": "applied_append", "rationale": ""}
    ]


def test_apply_append_to_empty_body() -> None:
    body, report = apply_edits("", [EditOp(op="append", content="added")])
    assert body == "added"
    assert report[0]["status"] == "applied_append"


def test_apply_insert_after_found() -> None:
    body, report = apply_edits(
        "line one\nline two", [EditOp(op="insert_after", target="line one", content="\nnew line")]
    )
    assert body == "line one\nnew line\nline two"
    assert report == [
        {
            "op": "insert_after",
            "target_head": "line one",
            "status": "applied_insert_after",
            "rationale": "",
        }
    ]


def test_apply_insert_after_missing_falls_back_to_append() -> None:
    body, report = apply_edits(
        "original", [EditOp(op="insert_after", target="missing", content="added")]
    )
    assert body == "original\n\nadded"
    assert report[0]["status"] == "fallback_append"


def test_apply_insert_after_missing_skips_when_insert_fallback_disabled() -> None:
    body, report = apply_edits(
        "original",
        [EditOp(op="insert_after", target="missing", content="added")],
        insert_fallback=False,
    )
    assert body == "original"
    assert report[0]["status"] == "skipped_target_not_found"


def test_apply_insert_after_found_unaffected_by_insert_fallback_disabled() -> None:
    body, report = apply_edits(
        "line one\nline two",
        [EditOp(op="insert_after", target="line one", content="\nnew line")],
        insert_fallback=False,
    )
    assert body == "line one\nnew line\nline two"
    assert report[0]["status"] == "applied_insert_after"


def test_edit_repair_insert_enabled_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("EVO_EDIT_REPAIR_INSERT", raising=False)
    assert edit_repair_insert_enabled() is False
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "1")
    assert edit_repair_insert_enabled() is True
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "0")
    assert edit_repair_insert_enabled() is False


def test_apply_replace_found() -> None:
    body, report = apply_edits(
        "hello world", [EditOp(op="replace", target="world", content="there")]
    )
    assert body == "hello there"
    assert report == [
        {"op": "replace", "target_head": "world", "status": "applied_replace", "rationale": ""}
    ]


def test_apply_replace_missing_is_skipped() -> None:
    body, report = apply_edits("hello world", [EditOp(op="replace", target="missing", content="x")])
    assert body == "hello world"
    assert report[0]["status"] == "skipped_target_not_found"


def test_apply_delete_found() -> None:
    body, report = apply_edits("hello world", [EditOp(op="delete", target=" world")])
    assert body == "hello"
    assert report == [
        {"op": "delete", "target_head": " world", "status": "applied_delete", "rationale": ""}
    ]


def test_apply_delete_missing_is_skipped() -> None:
    body, report = apply_edits("hello world", [EditOp(op="delete", target="missing")])
    assert body == "hello world"
    assert report[0]["status"] == "skipped_target_not_found"


def test_apply_empty_edits_is_noop() -> None:
    body, report = apply_edits("unchanged", [])
    assert body == "unchanged"
    assert report == []


def test_apply_target_head_clips_to_60_chars() -> None:
    long_target = "x" * 100
    body, report = apply_edits(long_target, [EditOp(op="delete", target=long_target)])
    assert body == ""
    assert report[0]["target_head"] == "x" * 60


def test_apply_report_includes_rationale_capped_to_120_chars() -> None:
    long_rationale = "x" * 200
    body, report = apply_edits(
        "original", [EditOp(op="append", content="added", rationale=long_rationale)]
    )
    assert body == "original\n\nadded"
    assert report[0]["rationale"] == "x" * 120


def test_apply_sequential_state_second_edit_targets_first_edit_output() -> None:
    edits = [
        EditOp(op="append", content="## New Section\ncontent here"),
        EditOp(op="insert_after", target="## New Section", content=" (revised)"),
    ]
    body, report = apply_edits("base body", edits)
    assert body == "base body\n\n## New Section (revised)\ncontent here"
    assert report[0]["status"] == "applied_append"
    assert report[1]["status"] == "applied_insert_after"


def test_apply_edits_use_first_match_only() -> None:
    body, report = apply_edits("a-a-a", [EditOp(op="replace", target="a", content="b")])
    assert body == "b-a-a"
    assert report[0]["status"] == "applied_replace"


def test_apply_insert_after_relocates_out_of_fence() -> None:
    body = "# Title\n\n```python\ndef foo():\n    return 1\n```\n\nMore text"
    edits = [EditOp(op="insert_after", target="return 1", content="**Pitfall:** off-by-one")]
    new_body, report = apply_edits(body, edits)
    assert report[0]["status"] == "applied_insert_after_relocated_fence"
    assert new_body.count("```") == 2
    fence_close = new_body.index("```", new_body.index("```python") + 3) + 3
    assert new_body.index("**Pitfall:**") >= fence_close


def test_apply_insert_after_relocated_content_does_not_glue_to_next_line() -> None:
    body = "```python\nreturn 1\n```\nMore text"
    edits = [EditOp(op="insert_after", target="return 1", content="**Pitfall:** off-by-one")]
    new_body, report = apply_edits(body, edits)
    assert report[0]["status"] == "applied_insert_after_relocated_fence"
    assert new_body == "```python\nreturn 1\n```\n**Pitfall:** off-by-one\nMore text"


def test_apply_insert_after_adds_separating_newline_before_block_marker() -> None:
    body = "## Heading\nBody text"
    edits = [EditOp(op="insert_after", target="## Heading", content="**Pitfall:** watch out")]
    new_body, report = apply_edits(body, edits)
    assert new_body == "## Heading\n**Pitfall:** watch out\nBody text"
    assert "## Heading**Pitfall:**" not in new_body
    assert report[0]["status"] == "applied_insert_after"


def test_apply_insert_after_relocated_fence_always_line_separates() -> None:
    body = "Head\n```py\ncode\n```"
    edits = [EditOp(op="insert_after", target="code", content="plain content")]
    new_body, report = apply_edits(body, edits)
    assert report[0]["status"] == "applied_insert_after_relocated_fence"
    assert new_body.count("```") == 2
    assert new_body == "Head\n```py\ncode\n```\nplain content"


def test_inside_fence_boundary_positions() -> None:
    text = "before\n```python\ncode\n```\nafter"
    fence_open = text.index("```python")
    fence_close = text.index("```", fence_open + 3)
    assert _inside_fence(text, 0) is False
    assert _inside_fence(text, fence_open) is False
    assert _inside_fence(text, fence_open + len("```python\n")) is True
    assert _inside_fence(text, fence_close) is True
    assert _inside_fence(text, fence_close + len("```\n")) is False


# ---------------------------------------------------------------------------
# parse_edits — tolerant extraction
# ---------------------------------------------------------------------------


def test_parse_fenced_json_block() -> None:
    text = '```json\n{"edits": [{"op": "append", "content": "hello"}]}\n```'
    ops, errors = parse_edits(text)
    assert errors == []
    assert len(ops) == 1
    assert ops[0].op == "append"
    assert ops[0].content == "hello"


def test_parse_prose_around_json() -> None:
    text = (
        "Sure, here are the edits:\n"
        '{"edits": [{"op": "replace", "target": "foo", "content": "bar"}]}\n'
        "Let me know if you need anything else."
    )
    ops, errors = parse_edits(text)
    assert errors == []
    assert len(ops) == 1
    assert ops[0].op == "replace"
    assert ops[0].target == "foo"


def test_parse_bare_top_level_list() -> None:
    text = '[{"op": "append", "content": "first"}, {"op": "append", "content": "second"}]'
    ops, errors = parse_edits(text)
    assert errors == []
    assert [op.content for op in ops] == ["first", "second"]


def test_parse_single_edit_object() -> None:
    text = '{"op": "delete", "target": "obsolete line"}'
    ops, errors = parse_edits(text)
    assert errors == []
    assert len(ops) == 1
    assert ops[0].op == "delete"
    assert ops[0].target == "obsolete line"


def test_parse_invalid_op_name_yields_error_record() -> None:
    text = '{"edits": [{"op": "frobnicate", "target": "x", "content": "y"}]}'
    ops, errors = parse_edits(text)
    assert ops == []
    assert len(errors) == 1
    assert "raw" in errors[0]
    assert "reason" in errors[0]


def test_parse_missing_target_on_replace_yields_error_record() -> None:
    text = '{"edits": [{"op": "replace", "content": "y"}]}'
    ops, errors = parse_edits(text)
    assert ops == []
    assert len(errors) == 1
    assert "target" in errors[0]["reason"]


def test_parse_mixed_valid_and_invalid_entries() -> None:
    text = (
        '{"edits": ['
        '{"op": "append", "content": "good"}, '
        '{"op": "replace", "content": "no target"}'
        "]}"
    )
    ops, errors = parse_edits(text)
    assert len(ops) == 1
    assert ops[0].content == "good"
    assert len(errors) == 1


def test_parse_unparseable_text_never_raises() -> None:
    ops, errors = parse_edits("this is not json at all, just prose")
    assert ops == []
    assert len(errors) == 1
    assert "reason" in errors[0]


def test_parse_empty_string_never_raises() -> None:
    ops, errors = parse_edits("")
    assert ops == []
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# select_edits — mechanical dedupe + budget clip
# ---------------------------------------------------------------------------


def test_select_dedupes_exact_duplicates_keeping_first() -> None:
    a = EditOp(op="append", content="x")
    b = EditOp(op="append", content="x")
    selected, report = select_edits([a, b], budget=4)
    assert selected == [a]
    assert report == [
        {"op": "append", "target_head": "", "status": "skipped_duplicate", "rationale": ""}
    ]


def test_select_keeps_distinct_ops() -> None:
    a = EditOp(op="append", content="x")
    b = EditOp(op="append", content="y")
    selected, report = select_edits([a, b], budget=4)
    assert selected == [a, b]
    assert report == []


def test_select_clips_to_budget_preserving_order() -> None:
    ops = [EditOp(op="append", content=str(i)) for i in range(6)]
    selected, report = select_edits(ops, budget=3)
    assert selected == ops[:3]
    assert [r["status"] for r in report] == ["skipped_over_budget"] * 3


def test_select_empty_proposals_is_noop() -> None:
    selected, report = select_edits([], budget=4)
    assert selected == []
    assert report == []


def test_select_dedupe_runs_before_budget_clip() -> None:
    a = EditOp(op="append", content="x")
    dup = EditOp(op="append", content="x")
    b = EditOp(op="append", content="y")
    selected, report = select_edits([a, dup, b], budget=2)
    assert selected == [a, b]
    statuses = sorted(r["status"] for r in report)
    assert statuses == ["skipped_duplicate"]


def test_edit_budget_default_is_four(monkeypatch) -> None:
    monkeypatch.delenv("EVO_EDIT_BUDGET", raising=False)
    assert edit_budget() == 4


def test_edit_budget_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_BUDGET", "7")
    assert edit_budget() == 7


def test_edit_budget_bad_value_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_BUDGET", "not-a-number")
    with pytest.raises(ValueError, match="not an integer"):
        edit_budget()


def test_train_optimization_defaults_are_more_aggressive(monkeypatch) -> None:
    monkeypatch.setenv("EVO_OPTIMIZATION_TARGET", "train")
    monkeypatch.delenv("EVO_EDIT_BUDGET", raising=False)
    monkeypatch.delenv("EVO_EDIT_REPAIR_INSERT", raising=False)
    monkeypatch.delenv("EVO_EDIT_DEDUP_BODY", raising=False)
    monkeypatch.delenv("EVO_EDIT_DEDUP_TOPIC", raising=False)

    assert edit_budget() == 6
    assert edit_repair_insert_enabled() is True
    assert edit_dedup_body_threshold() == 0.90
    assert edit_dedup_topic_enabled() is True


# ---------------------------------------------------------------------------
# edit_dedup_body_threshold + select_edits body-aware duplicate suppression
# ---------------------------------------------------------------------------


def test_edit_dedup_body_threshold_default_is_none(monkeypatch) -> None:
    monkeypatch.delenv("EVO_EDIT_DEDUP_BODY", raising=False)
    assert edit_dedup_body_threshold() is None


def test_edit_dedup_body_threshold_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    assert edit_dedup_body_threshold() == 0.9


def test_edit_dedup_body_threshold_garbage_or_zero_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "not-a-number")
    assert edit_dedup_body_threshold() is None
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0")
    assert edit_dedup_body_threshold() is None


_DEDUP_BODY = (
    "# Guide\n\n"
    "## Lessons\n"
    "Always validate input types.\n"
    "Check for None before use.\n"
    "Log the validation failure.\n"
)


def test_select_edits_drops_near_duplicate_content_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    dup = EditOp(
        op="insert_after",
        target="## Lessons",
        content=(
            "Always validate input types.\nCheck for None before use.\nLog the validation failure."
        ),
    )
    selected, report = select_edits([dup], budget=4, body=_DEDUP_BODY)
    assert selected == []
    assert report == [
        {
            "op": "insert_after",
            "target_head": "## Lessons",
            "status": "skipped_duplicate_content",
            "rationale": "",
        }
    ]


def test_select_edits_keeps_novel_content_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    novel = EditOp(
        op="insert_after",
        target="## Lessons",
        content=(
            "Use retries with exponential backoff.\nCap max attempts at 5.\nLog each retry attempt."
        ),
    )
    selected, report = select_edits([novel], budget=4, body=_DEDUP_BODY)
    assert selected == [novel]
    assert report == []


def test_select_edits_duplicate_content_ignored_when_guard_disabled(monkeypatch) -> None:
    monkeypatch.delenv("EVO_EDIT_DEDUP_BODY", raising=False)
    dup = EditOp(
        op="insert_after",
        target="## Lessons",
        content=(
            "Always validate input types.\nCheck for None before use.\nLog the validation failure."
        ),
    )
    selected, report = select_edits([dup], budget=4, body=_DEDUP_BODY)
    assert selected == [dup]
    assert report == []


def test_select_edits_replace_never_suppressed_for_duplicate_content(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    dup_replace = EditOp(
        op="replace",
        target="Always validate input types.",
        content=(
            "Always validate input types.\nCheck for None before use.\nLog the validation failure."
        ),
    )
    selected, report = select_edits([dup_replace], budget=4, body=_DEDUP_BODY)
    assert selected == [dup_replace]
    assert report == []


def test_select_edits_drops_duplicate_topic_already_in_body(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_TOPIC", "1")
    body = "# Guide\n\n## Common Patterns\n\n#### Safe Placeholder Replacement\nUse this once.\n"
    dup = EditOp(
        op="insert_after",
        target="## Common Patterns",
        content="#### Safe Placeholder Replacement\nUse the placeholder geometry.",
    )

    selected, report = select_edits([dup], budget=4, body=body)

    assert selected == []
    assert report == [
        {
            "op": "insert_after",
            "target_head": "## Common Patterns",
            "status": "skipped_duplicate_topic",
            "rationale": "",
        }
    ]


def test_select_edits_drops_duplicate_topic_within_proposal_set(monkeypatch) -> None:
    monkeypatch.setenv("EVO_EDIT_DEDUP_TOPIC", "1")
    first = EditOp(
        op="insert_after",
        target="## Common Patterns",
        content="#### Safe Placeholder Replacement\nFirst recipe.",
    )
    second = EditOp(
        op="insert_after",
        target="# Guide",
        content="#### Safe Placeholder Replacement\nSecond recipe.",
    )

    selected, report = select_edits([first, second], budget=4, body="# Guide\n")

    assert selected == [first]
    assert report[0]["status"] == "skipped_duplicate_topic"
