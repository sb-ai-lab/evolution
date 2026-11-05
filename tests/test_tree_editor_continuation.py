"""Task 8: one continuation turn on an empty diff (PDR-0019, ADR-0023, spec §6).

The chat-role doubles are `test_tree_chat_editor.py`'s own (`_response`,
`_plan`, `_ws`, `_request`, `_Client`), following Task 7's convention of
importing rather than copying (`tests/test_tree_chat_role_retry.py`'s own
docstring notes there is no `FakeChatServer` class with scripted tokens).
The agent-role doubles are `test_tree_agent_editor.py`'s own `factory`/
`FakeBackend`.
"""

from __future__ import annotations

import json
from pathlib import Path

from evolution.prompts import TREE_AGENT_EDITOR_SYSTEM, TREE_EDITOR_CONTINUATION, TREE_EDITOR_SYSTEM
from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection
from evolution.tree.roles.editor.agent_editor import AgentCliEditor
from evolution.tree.roles.editor.chat_editor import ChatCredentials, ChatEditor
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.seals import agent_identity, chat_identity, seal_program, seal_prompt
from tests.tree.roles.editor.test_agent_editor import CHILD_ENV, _candidate, _program
from tests.tree.roles.editor.test_agent_editor import SECRET as AGENT_SECRET
from tests.tree.roles.editor.test_agent_editor import _request as _agent_request
from tests.tree.roles.editor.test_agent_editor import factory as agent_backend_factory
from tests.tree.roles.editor.test_chat_editor import SECRET as CHAT_SECRET
from tests.tree.roles.editor.test_chat_editor import (
    _Client,
    _log_lines,
    _plan,
    _response,
    _section,
    _ws,
)
from tests.tree.roles.editor.test_chat_editor import (
    _request as _chat_request,
)

RETRY = RoleCallRetry(
    max_attempts=3,
    total_budget_seconds=30,
    initial_seconds=0,
    multiplier=1,
    max_seconds=0,
)


def _events(rig, name):
    return [r for r in rig.records() if r["event"] == name]


def _empty_plan() -> object:
    return _response("record_plan", json.dumps({"summary": "investigating", "changes": []}))


def _resilient_chat_editor(
    tmp_path: Path, responses: list[object], *, max_turns: int | None = None
) -> tuple[ChatEditor, _Client]:
    section_kw: dict[str, object] = {"retry": RETRY}
    if max_turns is not None:
        section_kw["max_turns"] = max_turns
    fake = _Client(list(responses))
    editor = ChatEditor(
        section=_section(**section_kw),
        credentials=ChatCredentials(CHAT_SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(CHAT_SECRET,),
        client_factory=lambda section, credentials: fake,
        role_resilience=True,
    )
    return editor, fake


# --------------------------------------------------------------------------
# Chat Editor
# --------------------------------------------------------------------------


async def test_chat_editor_continues_once_and_edits(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    responses = [
        _response("list_files"),
        _empty_plan(),
        _response("finish_edit", '{"summary":"nothing to do"}'),
        _plan("skills/SKILL.md"),
        _response("write_file", json.dumps({"path": "skills/SKILL.md", "content": "second\n"})),
        _response("finish_edit", '{"summary":"done"}'),
    ]
    editor, client = _resilient_chat_editor(tmp_path, responses)

    result = await editor.edit(_chat_request(workspace))

    assert (result.status, result.continued) == ("edited", True)
    phases = [line.get("phase") for line in _log_lines(editor)]
    assert phases.count("continuation") == 1


async def test_chat_editor_declines_after_one_continuation(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    responses = [
        _response("list_files"),
        _empty_plan(),
        _response("finish_edit", '{"summary":"first no change"}'),
        _response("finish_edit", '{"summary":"second no change"}'),
    ]
    editor, client = _resilient_chat_editor(tmp_path, responses)

    result = await editor.edit(_chat_request(workspace))

    assert (result.status, result.continued) == ("declined", True)
    assert len(client.calls) == 4


async def test_no_continuation_without_turn_budget(tmp_path: Path) -> None:
    # `max_turns=3` leaves exactly enough turns for the preflight probe
    # (`negotiate_style` consumes turn 0), `record_plan` (turn 1) and
    # `finish_edit` (turn 2); after that `self._turns == max_turns`, so there
    # is no room left for the one continuation turn.
    workspace = _ws(tmp_path)
    responses = [
        _response("list_files"),
        _empty_plan(),
        _response("finish_edit", '{"summary":"no change"}'),
    ]
    editor, client = _resilient_chat_editor(tmp_path, responses, max_turns=3)

    result = await editor.edit(_chat_request(workspace))

    assert (result.status, result.continued) == ("declined", False)
    assert len(client.calls) == 3


# --------------------------------------------------------------------------
# Agent-CLI Editor
# --------------------------------------------------------------------------


async def test_agent_editor_second_call_uses_continuation_text(tmp_path: Path) -> None:
    root = _candidate(tmp_path)

    def _write_skill(work_dir: Path) -> None:
        (work_dir / "skills" / "SKILL.md").write_text("second\n", encoding="utf-8")

    build, instances = agent_backend_factory(
        edits=[lambda work: None, _write_skill],
        usage=[{"tool_turns": 1}, {"tool_turns": 1}],
        content="ok",
    )
    editor = AgentCliEditor(
        section=AgentCliEditorSection(model="codex"),
        program=seal_program(_program(tmp_path)),
        prompt=seal_prompt(None, packaged=TREE_AGENT_EDITOR_SYSTEM),
        child_env=dict(CHILD_ENV),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(AGENT_SECRET,),
        backend_factory=build,
        role_resilience=True,
    )

    result = await editor.edit(_agent_request(root))

    assert (result.status, result.continued) == ("edited", True)
    assert len(instances) == 2
    assert instances[1].opening == TREE_EDITOR_CONTINUATION


def _resilient_agent_editor(tmp_path: Path, edits, *, max_turns: int | None = None):
    build, instances = agent_backend_factory(
        edits=edits,
        usage=[{"tool_turns": 1}] * len(edits),
        content="ok",
    )
    section_kw: dict[str, object] = {"model": "codex"}
    if max_turns is not None:
        section_kw["max_turns"] = max_turns
    editor = AgentCliEditor(
        section=AgentCliEditorSection(**section_kw),
        program=seal_program(_program(tmp_path)),
        prompt=seal_prompt(None, packaged=TREE_AGENT_EDITOR_SYSTEM),
        child_env=dict(CHILD_ENV),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(AGENT_SECRET,),
        backend_factory=build,
        role_resilience=True,
    )
    return editor, instances


def _broken_yaml(work_dir: Path) -> None:
    (work_dir / "skills" / "config.yaml").write_text("key: [unclosed\n", encoding="utf-8")


def _restore_yaml(work_dir: Path) -> None:
    (work_dir / "skills" / "config.yaml").write_text("key: value\n", encoding="utf-8")


def _write_skill_md(work_dir: Path) -> None:
    (work_dir / "skills" / "SKILL.md").write_text("continued\n", encoding="utf-8")


async def test_agent_editor_continues_after_a_repair_that_empties_the_diff(
    tmp_path: Path,
) -> None:
    # The self-check repair and
    # the continuation may both happen. A repair that reverts the invalid edit
    # leaves an empty diff; with budget left the Editor gets its one
    # continuation turn, which here edits.
    root = _candidate(tmp_path)
    editor, instances = _resilient_agent_editor(
        tmp_path, [_broken_yaml, _restore_yaml, _write_skill_md]
    )

    result = await editor.edit(_agent_request(root))

    assert (result.status, result.continued) == ("edited", True)
    assert len(instances) == 3
    assert instances[1].opening != TREE_EDITOR_CONTINUATION
    assert instances[2].opening == TREE_EDITOR_CONTINUATION


async def test_agent_editor_continues_once_after_a_repair_then_declines(tmp_path: Path) -> None:
    # At most one continuation per invocation: an empty continuation declines.
    root = _candidate(tmp_path)
    editor, instances = _resilient_agent_editor(
        tmp_path, [_broken_yaml, _restore_yaml, lambda work: None]
    )

    result = await editor.edit(_agent_request(root))

    assert (result.status, result.continued) == ("declined", True)
    assert len(instances) == 3


async def test_agent_editor_no_continuation_after_a_repair_without_turn_budget(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    editor, instances = _resilient_agent_editor(
        tmp_path, [_broken_yaml, _restore_yaml, _write_skill_md], max_turns=2
    )

    result = await editor.edit(_agent_request(root))

    assert (result.status, result.continued) == ("declined", False)
    assert len(instances) == 2


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_identity_includes_continuation_only_in_family_10(tmp_path: Path) -> None:
    section = ChatEditorSection(model="m", base_url="https://gw.example/v1", api_key_env="K")
    prompt = seal_prompt(None, packaged=TREE_EDITOR_SYSTEM)

    assert chat_identity(section, prompt) == chat_identity(
        section, prompt, continuation_sha256=None
    )
    assert chat_identity(section, prompt) != chat_identity(
        section, prompt, continuation_sha256="a" * 64
    )


def test_agent_identity_includes_continuation_only_in_family_10(tmp_path: Path) -> None:
    program = seal_program(_program(tmp_path))
    prompt = seal_prompt(None, packaged=TREE_AGENT_EDITOR_SYSTEM)
    section = AgentCliEditorSection(model="codex")

    assert agent_identity(section, prompt, program) == agent_identity(
        section, prompt, program, continuation_sha256=None
    )
    assert agent_identity(section, prompt, program) != agent_identity(
        section, prompt, program, continuation_sha256="a" * 64
    )


# --------------------------------------------------------------------------
# Campaign level
# --------------------------------------------------------------------------
