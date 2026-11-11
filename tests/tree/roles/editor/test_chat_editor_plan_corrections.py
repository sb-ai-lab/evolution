"""Family-15 chat plan normalization and corrections, with /15 compatibility pins."""

import json

import pytest

from evolution.prompts import TREE_EDITOR_SYSTEM
from evolution.tree.roles.editor.chat_editor import ChatCredentials, ChatEditor
from evolution.tree.roles.seals import seal_prompt
from tests.tree.roles.editor.test_chat_editor import (
    SECRET,
    _budget,
    _CapturingClient,
    _editor,
    _log_lines,
    _plan,
    _prose_response,
    _request,
    _response,
    _section,
    _write,
    _ws,
)

pytestmark = pytest.mark.slow


def _editor16(tmp_path, responses, **kwargs):
    client = _CapturingClient([_response("list_files"), *responses])
    kwargs.setdefault("max_turns", 24)
    editor = ChatEditor(
        section=_section(**kwargs),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: client,
        plan_correction=True,
    )
    return editor, client


def _plan_actions(*pairs):
    return _response(
        "record_plan",
        json.dumps(
            {
                "summary": "fix",
                "changes": [
                    {"path": path, "action": action, "purpose": "fix"} for path, action in pairs
                ],
            }
        ),
    )


def _l19():
    return [
        _plan("skills/SKILL.md"),
        _write("skills/SKILL.md", "second\n"),
        _write("skills/NEW.md", "new\n"),
        _plan_actions(("skills/SKILL.md", "update"), ("skills/OTHER.md", "update")),
        _write("skills/OTHER.md", "other\n"),
        _response("finish_edit", '{"summary":"done"}'),
    ]


async def test_the_observed_l19_sequence_finishes_with_the_edit_kept(tmp_path):
    workspace = _ws(tmp_path)
    editor, _ = _editor16(tmp_path, _l19())
    result = await editor.edit(_request(workspace))
    assert result.status == "edited"
    assert {path.name for path in (workspace / "skills").iterdir()} == {
        "SKILL.md",
        "NEW.md",
        "OTHER.md",
    }
    normalized = [entry for entry in _log_lines(editor) if entry["phase"] == "plan_normalized"]
    assert {(e["path"], e["declared"], e["recorded"]) for e in normalized} >= {
        ("skills/NEW.md", None, "create"),
        ("skills/OTHER.md", "update", "create"),
    }
    assert not [
        entry for entry in _log_lines(editor) if entry["phase"] == "protocol_repair_requested"
    ]


async def test_an_unplanned_addition_beyond_the_budget_gets_the_budget_tool_error(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _write("skills/NEW.md", "n\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
        budgets=_budget(1),
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    errors = [entry for entry in _log_lines(editor) if entry["phase"] == "tool_error"]
    assert errors[0]["error"].startswith(
        "write_file refused: this Attempt may change at most 1 file"
    )
    assert not any(
        e["phase"] == "plan_normalized" and e["path"] == "skills/NEW.md" for e in _log_lines(editor)
    )


async def test_a_path_outside_the_writable_set_is_never_added(tmp_path):
    workspace = _ws(tmp_path)
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            _write("outside/X.md", "x\n"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(workspace))
    assert result.status == "edited" and not (workspace / "outside" / "X.md").exists()
    assert not any(
        e["phase"] == "plan_normalized" and e["path"] == "outside/X.md" for e in _log_lines(editor)
    )


async def test_re_planning_from_a_to_b_and_changing_only_b_finishes(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            _plan("skills/OTHER.md", action="create"),
            _write("skills/OTHER.md", "other\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    assert not [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]


async def test_a_withdrawn_path_that_still_differs_is_added_back(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _plan("skills/OTHER.md", action="create"),
            _write("skills/OTHER.md", "other\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    assert any(
        e["phase"] == "plan_normalized"
        and (e["path"], e["declared"], e["recorded"]) == ("skills/SKILL.md", None, "update")
        for e in _log_lines(editor)
    )


async def test_v15_keeps_plan_mismatch_and_its_repair_budget(tmp_path):
    editor, _ = _editor(tmp_path, [_response("list_files"), *_l19()], max_turns=24)
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "failed" and result.code == "protocol"
    assert len([e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]) == 2


async def test_create_of_an_existing_file_is_normalized(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md", action="create"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    assert any(
        e["phase"] == "plan_normalized" and (e["declared"], e["recorded"]) == ("create", "update")
        for e in _log_lines(editor)
    )


async def test_a_planned_create_becomes_update_at_the_second_mutation(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/NEW.md", action="create"),
            _write("skills/NEW.md", "x\n"),
            _write("skills/NEW.md", "y\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    assert any(
        e["phase"] == "plan_normalized"
        and (e["path"], e["declared"], e["recorded"]) == ("skills/NEW.md", "create", "update")
        for e in _log_lines(editor)
    )


async def test_re_planning_an_executed_delete_is_told_to_leave_the_path_out(tmp_path):
    workspace = _ws(tmp_path)
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md", action="delete"),
            _response("delete_file", '{"path":"skills/SKILL.md"}'),
            _plan_actions(("skills/SKILL.md", "delete"), ("skills/NEW.md", "create")),
            _plan_actions(("skills/NEW.md", "create")),
            _write("skills/NEW.md", "new\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(workspace))
    assert result.status == "edited"
    assert {path.name for path in (workspace / "skills").iterdir()} == {"NEW.md"}
    corrections = [e for e in _log_lines(editor) if e["phase"] == "plan_correction_requested"]
    assert [e["category"] for e in corrections] == ["plan_delete_missing"]
    assert "leave it out" in corrections[0]["error"]


async def test_six_plan_corrections_in_a_row_neither_block_record_plan_nor_spend_repairs(tmp_path):
    bad = _plan_actions(("skills/SKILL.md", "update"), ("skills/SKILL.md", "update"))
    editor, client = _editor16(
        tmp_path,
        [bad] * 6
        + [
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
        max_turns=12,
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    entries = _log_lines(editor)
    assert [e["category"] for e in entries if e["phase"] == "plan_correction_requested"] == [
        "plan_repeated_path"
    ] * 6
    assert not [e for e in entries if e["phase"] == "protocol_repair_requested"]
    assert all(
        "record_plan" in {t["function"]["name"] for t in request["tools"]}
        for request in client.calls[1:]
    )


@pytest.mark.parametrize(
    "script,category",
    [
        ([_plan("outside/X.md")], "plan_path_not_writable"),
        ([_plan("../escape.md")], "plan_path_unsafe"),
        ([_plan("skills/GONE.md", action="delete")], "plan_delete_missing"),
        ([_write("skills/SKILL.md", "x\n")], "mutation_before_plan"),
        ([_response("finish_edit", '{"summary":"d"}')], "finish_before_plan"),
        (
            [
                _plan("skills/SKILL.md", "skills/OTHER.md"),
                _write("skills/SKILL.md", "x\n"),
                _response("finish_edit", '{"summary":"d"}'),
            ],
            "plan_mismatch",
        ),
        ([_plan("skills/SKILL.md"), _write("outside/X.md", "x\n")], "plan_path_not_writable"),
        ([_plan("skills/SKILL.md"), _write("../escape.md", "x\n")], "plan_path_unsafe"),
    ],
)
async def test_each_plan_problem_is_a_correction_with_its_category(tmp_path, script, category):
    editor, _ = _editor16(
        tmp_path,
        [
            *script,
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "fixed\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    corrections = [e for e in _log_lines(editor) if e["phase"] == "plan_correction_requested"]
    assert corrections[0]["category"] == category
    assert not [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]


async def test_malformed_calls_still_spend_protocol_repairs(tmp_path):
    editor, _ = _editor16(tmp_path, [_prose_response()] * 3)
    result = await editor.edit(_request(_ws(tmp_path)))
    assert (result.status, result.code) == ("failed", "protocol")
    assert [
        e["category"] for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"
    ] == ["no_tool_call", "no_tool_call"]


@pytest.mark.parametrize(
    "bad,category",
    [
        (_prose_response(), "no_tool_call"),
        (_response("nope"), "unknown_tool"),
        (_response("record_plan", "[]"), "invalid_arguments"),
        (_response("record_plan", '{"summary":1,"changes":[]}'), "invalid_arguments"),
        (_response("write_file", "{}"), "invalid_arguments"),
        (_response("write_file", '{"path":7,"content":"x"}'), "invalid_arguments"),
        (_response("finish_edit", '{"summary":7}'), "invalid_arguments"),
        (_response("finish_edit", "{}"), "invalid_arguments"),
    ],
)
async def test_broken_call_repairs_carry_their_category(tmp_path, bad, category):
    editor, _ = _editor16(
        tmp_path,
        [
            bad,
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    repairs = [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]
    assert repairs[0]["category"] == category
    assert not [e for e in _log_lines(editor) if e["phase"] == "plan_correction_requested"]


async def test_max_turns_ends_a_model_that_never_complies(tmp_path):
    editor, _ = _editor16(tmp_path, [_plan("outside/X.md")] * 10, max_turns=4)
    result = await editor.edit(_request(_ws(tmp_path)))
    assert (result.status, result.code) == ("failed", "turns_exhausted")


async def test_repeated_finish_mismatches_cost_turns_until_model_complies(tmp_path):
    editor, _ = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            *[_response("finish_edit", '{"summary":"d"}')] * 6,
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
        max_turns=14,
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    assert [
        e["category"] for e in _log_lines(editor) if e["phase"] == "plan_correction_requested"
    ] == ["plan_mismatch"] * 6
    assert not [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]


async def test_v15_log_entries_have_no_category(tmp_path):
    editor, _ = _editor(
        tmp_path,
        [
            _response("list_files"),
            _prose_response(),
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ],
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    repairs = [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]
    assert len(repairs) == 1 and "category" not in repairs[0]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": "skills/MISSING.md"}),
        ("inspect_file", {"path": "skills/MISSING.md"}),
        ("replace_text", {"path": "skills/MISSING.md", "old_text": "old", "new_text": "new"}),
        ("edit_yaml", {"path": "skills/MISSING.md", "key_path": "key", "new_text": "new"}),
        ("delete_file", {"path": "skills/MISSING.md"}),
        (
            "apply_patch",
            {
                "patch": "--- a/skills/MISSING.md\n+++ b/skills/MISSING.md\n@@ -1 +1 @@\n-old\n+new\n"
            },
        ),
    ],
)
async def test_v16_missing_path_is_tool_error_before_plan_normalization(tmp_path, tool, args):
    from dataclasses import replace

    from tests.test_tree_chat_role_retry import RETRY

    editor, client = _editor16(
        tmp_path,
        [
            _plan("skills/SKILL.md"),
            _response(tool, json.dumps(args)),
            _write("skills/SKILL.md", "second\n"),
            _response("finish_edit", '{"summary":"done"}'),
        ],
        retry=RETRY,
    )
    editor = replace(editor, family15=True, role_resilience=True)
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited", result.note
    errors = [r for r in _log_lines(editor) if r.get("phase") == "tool_error"]
    assert len(errors) == 1
    assert errors[0]["error"].startswith(
        "path does not exist: skills/MISSING.md; nearest existing paths: "
    )
    assert "skills/SKILL.md" in errors[0]["error"]
    assert not [
        r
        for r in _log_lines(editor)
        if r.get("phase") in {"plan_normalized", "protocol_repair_requested"}
    ]


async def test_v16_plan_delete_missing_lists_nearest(tmp_path):
    from dataclasses import replace

    from tests.test_tree_chat_role_retry import RETRY

    editor, client = _editor16(
        tmp_path,
        [
            _plan("skills/MISSING.md", action="delete"),
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "second\n"),
            _response("finish_edit", '{"summary":"done"}'),
        ],
        retry=RETRY,
    )
    editor = replace(editor, family15=True, role_resilience=True)
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited", result.note
    errors = [r for r in _log_lines(editor) if r.get("phase") == "plan_correction_requested"]
    assert len(errors) == 1 and errors[0]["category"] == "plan_delete_missing"
    assert "nearest existing paths: skills/SKILL.md" in errors[0]["error"]


@pytest.mark.parametrize(
    "bad",
    ['{"path":7,"content":"x"}', '{ "content" : "x" }'],
)
async def test_v16_editor_object_arguments_repaired_for_schema_keep_their_string(tmp_path, bad):
    """ADR-0061: object arguments repaired by `_expect` are never rewritten."""

    from dataclasses import replace

    from tests.test_tree_chat_role_retry import _resilient_editor

    client = _CapturingClient(
        [
            _response("list_files"),
            _response("write_file", bad),
            _plan("skills/SKILL.md"),
            _write("skills/SKILL.md", "x\n"),
            _response("finish_edit", '{"summary":"d"}'),
        ]
    )
    editor = replace(_resilient_editor(tmp_path, client), family15=True, plan_correction=True)
    assert (await editor.edit(_request(_ws(tmp_path)))).status == "edited"
    (repair,) = [e for e in _log_lines(editor) if e["phase"] == "protocol_repair_requested"]
    assert (repair["category"], repair["error"]) == ("invalid_arguments", "invalid tool arguments")
    assert "replaced_calls" not in repair
    history = client.calls[-1]["messages"]
    retained = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    assert retained["tool_calls"][0]["function"]["arguments"] == bad
    correction = next(
        m["content"]
        for m in history
        if m["role"] == "user" and "Correction details: " in str(m["content"])
    )
    assert "were removed from the conversation history" not in correction
    details = json.loads(correction.split("Correction details: ", 1)[1])
    assert details["received_calls"][0]["arguments"]["preview"] == bad
