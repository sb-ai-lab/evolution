"""`ChatEditor` at its v2 seam: one attempt, one projection, one published tree."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.prompts import TREE_EDITOR_SYSTEM
from evolution.tree.roles.chat_history import assistant_message, tool_exchange
from evolution.tree.roles.config import (
    DEFAULT_CONTEXT_COMPACT_CHARS,
    DEFAULT_CONTEXT_MESSAGE_MAX_CHARS,
    ChatEditorSection,
)
from evolution.tree.roles.editor.chat_editor import (
    EVIDENCE_PAGE_BYTES,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    ChatCredentials,
    ChatEditor,
    EditFailure,
    _apply_hunks,
    _compact_messages,
    _evidence_page,
    _inspect_file,
    _parse_patch,
    _read_file_range,
    _response_audit,
    request_options,
    response_telemetry,
)
from evolution.tree.roles.editor.editor_brief import EvidenceRecord, render_catalog
from evolution.tree.roles.editor_contracts import (
    Diagnosis,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    EditRequest,
)
from evolution.tree.roles.seals import chat_identity, seal_prompt
from evolution.tree.roles.workspace import Budgets, ControlCollision

pytestmark = pytest.mark.slow

# A neutral diagnosis for tests not themselves about the diagnosis field.
_DEFAULT_DIAGNOSIS = Diagnosis("absent", None, None, (), None)

# --------------------------------------------------------------------------
# Fakes and response builders, moved verbatim from `tests/test_tree_file_editor.py`
# --------------------------------------------------------------------------


def _response(name: str, arguments: str = "{}") -> Any:
    call = SimpleNamespace(
        id="call", type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])


def _legacy_response(name: str, arguments: str = "{}") -> Any:
    call = SimpleNamespace(name=name, arguments=arguments)
    message = SimpleNamespace(tool_calls=None, function_call=call, content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _plan(*paths: str, legacy: bool = False, action: str = "update") -> Any:
    arguments = json.dumps(
        {
            "summary": "smallest useful change",
            "changes": [
                {"path": path, "action": action, "purpose": "repair behavior"} for path in paths
            ],
        }
    )
    response = _legacy_response if legacy else _response
    return response("record_plan", arguments)


def _prose_response(content: str = "no tool") -> Any:
    message = SimpleNamespace(tool_calls=None, function_call=None, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _parallel_response(*calls: tuple[str, str]) -> Any:
    tool_calls = [
        SimpleNamespace(
            id=f"call-{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (name, arguments) in enumerate(calls)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))]
    )


class _Client:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = next(self._responses)
        response.model = kwargs["model"]
        provider = (kwargs.get("extra_body") or {}).get("provider", {}).get("only", [])
        if provider:
            response.provider = "Z.AI" if provider[0] == "z-ai" else provider[0]
        return response


class _ModernRejectingClient(_Client):
    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if "tools" in kwargs:
            raise RuntimeError("400 invalid JSON syntax")
        return next(self._responses)


class _LegacyContinuationRejectingClient(_Client):
    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        messages = kwargs["messages"]
        if any(message["role"] == "function" for message in messages):
            raise RuntimeError("400 invalid JSON syntax")
        return next(self._responses)


class _RetryingClient(_Client):
    def __init__(self, responses: list[Any], failures: int) -> None:
        super().__init__(responses)
        self._failures = failures

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._failures:
            self._failures -= 1
            raise RuntimeError("credential-value <think>hidden</think>")
        return next(self._responses)


class _CapturingClient(_Client):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.messages: list[str] = []

    async def create(self, **kwargs: Any) -> Any:
        self.messages.extend(str(message.get("content", "")) for message in kwargs["messages"])
        return await super().create(**kwargs)


# --------------------------------------------------------------------------
# v2 fixtures
# --------------------------------------------------------------------------

SECRET = "sekret-key-value"


def _section(**kw: Any) -> ChatEditorSection:
    kw.setdefault("base_url", "https://gw.example/v1")
    return ChatEditorSection(model="m", api_key_env="K", **kw)


def _editor(
    tmp_path: Path,
    responses: list[Any] | None = None,
    *,
    client: _Client | None = None,
    log_root: Path | None = None,
    **kw: Any,
) -> tuple[ChatEditor, _Client]:
    fake = client if client is not None else _Client(list(responses or []))
    editor = ChatEditor(
        section=_section(**kw),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=(log_root if log_root is not None else tmp_path / "run" / "editor-logs"),
        secrets=(SECRET,),
        client_factory=lambda section, credentials: fake,
    )
    return editor, fake


def _brief(cases: int = 1) -> EditBrief:
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=tuple(
            EditBriefCase(
                case_id=f"c{index}",
                group_id="g1",
                stratum="s1",
                tags=("t",),
                public={"text": "hello"},
                repeats=(EditBriefRepeat(1, f"c{index}-r1", "rollout_finished", 0.5, True),),
            )
            for index in range(1, cases + 1)
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )


def _ws(tmp_path: Path, name: str = "ws") -> Path:
    workspace = tmp_path / name
    (workspace / "skills").mkdir(parents=True)
    (workspace / "skills" / "SKILL.md").write_text("first\n", encoding="utf-8")
    return workspace


def _request(
    workspace: Path,
    *,
    attempt_id: str = "a1",
    writable: tuple[str, ...] = ("skills/**",),
    brief: EditBrief | None = None,
    deadline: float = 30.0,
) -> EditRequest:
    return EditRequest(attempt_id, 1, workspace, writable, brief or _brief(), deadline)


def _log_lines(editor: ChatEditor, attempt_id: str = "a1") -> list[dict[str, Any]]:
    path = editor.log_root / f"{attempt_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _unique(values: list[str]) -> list[str]:
    """Message contents in first-seen order: a captured client sees each many times."""

    return list(dict.fromkeys(values))


def _edit_flow(*, path: str = "skills/SKILL.md", content: str = "second\n") -> list[Any]:
    return [
        _response("list_files"),
        _plan(path),
        _response("write_file", json.dumps({"path": path, "content": content})),
        _response("finish_edit", '{"summary":"done"}'),
    ]


# --------------------------------------------------------------------------
# Identity and configuration
# --------------------------------------------------------------------------


def test_identity_is_declared_configuration(tmp_path: Path) -> None:
    editor, _client = _editor(tmp_path)

    assert editor.identity == chat_identity(editor.section, editor.prompt)
    assert editor.identity.kind == "chat"


def test_request_options_carry_provider_pin_quantization_and_reasoning() -> None:
    section = _section(provider_only=("z-ai",), quantizations=("fp8",), reasoning_effort="high")

    options = request_options(section)

    assert options == {
        "extra_body": {
            "provider": {
                "only": ["z-ai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "quantizations": ["fp8"],
            },
            "reasoning": {"effort": "high"},
        }
    }
    assert request_options(_section()) == {}


def test_qwen_models_disable_thinking_in_the_extra_body() -> None:
    options = request_options(
        ChatEditorSection(model="Qwen/Qwen3", base_url="https://gw.example/v1", api_key_env="K")
    )

    assert options["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


# --------------------------------------------------------------------------
# Outcomes: edited / declined
# --------------------------------------------------------------------------


async def test_edited_publishes_admitted_change_and_keeps_candidate_modes(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    skill = workspace / "skills" / "SKILL.md"
    os.chmod(skill, 0o644)
    editor, client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("edited", "done")
    assert skill.read_text(encoding="utf-8") == "second\n"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o644
    assert [call["model"] for call in client.calls] == ["m"] * len(client.calls)


async def test_a_fifo_at_the_attempt_log_path_fails_the_attempt_without_blocking(
    tmp_path: Path,
) -> None:
    """The attempt log is opened before anything else, so its refusal is the attempt's.

    A FIFO at the predicted pathname used to block the open on the event-loop
    thread; now it is `failed/workspace_changed`, no provider call is made and
    the Candidate is never touched.
    """

    workspace = _ws(tmp_path)
    log_root = tmp_path / "run" / "editor-logs"
    log_root.mkdir(parents=True)
    log_root.chmod(0o700)
    os.mkfifo(log_root / "a1.jsonl")
    editor, client = _editor(tmp_path, _edit_flow())

    started = time.monotonic()
    result = await editor.edit(_request(workspace))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the open must not wait for a reader"
    assert result.status == "failed"
    assert result.note.startswith("workspace_changed ")
    assert client.calls == [], "no provider call is made"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_a_reverted_edit_is_declined_and_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write, revert, nudge, finish: the admitted diff is empty, so nothing ships.

    `record_plan` promises an update, the model writes it and then restores the
    original bytes. The first `finish_edit` sees a plan that no longer matches
    the (now empty) diff and earns the one nudge; the second is `_covers`-clean
    because the plan is satisfied vacuously. The status is decided by that final
    diff, not by the fact that a mutating tool once succeeded, so the result is
    `declined` and `publish` -- which would replace the Candidate root for no
    change at all -- is never reached.
    """

    workspace = _ws(tmp_path)
    skill = workspace / "skills" / "SKILL.md"
    before = skill.stat()
    published: list[Any] = []
    monkeypatch.setattr(
        "evolution.tree.roles.editor.chat_editor.publish",
        lambda changes, projection: published.append(changes),
    )
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("write_file", '{"path":"skills/SKILL.md","content":"first\\n"}'),
            _response("finish_edit", '{"summary":"nothing after all"}'),
            _response("finish_edit", '{"summary":"nothing after all"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("declined", "nothing after all")
    assert published == [], "an empty change set is never published"
    assert skill.read_text(encoding="utf-8") == "first\n"
    after = skill.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


async def test_declined_when_no_mutating_tool_succeeded(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("list_files"),
            _plan(),
            _response("finish_edit", '{"summary":"nothing to do"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("declined", "nothing to do")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_declined_with_an_empty_summary_reports_no_change_proposed(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [_response("list_files"), _plan(), _response("finish_edit", '{"summary":""}')],
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("declined", "no change proposed")


# --------------------------------------------------------------------------
# Writable set
# --------------------------------------------------------------------------


async def test_write_outside_writable_set_is_refused_by_the_tool_and_by_admission(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    (workspace / "README.md").write_text("readme\n", encoding="utf-8")
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(),
            _response("write_file", '{"path":"README.md","content":"hijacked\\n"}'),
            _response("finish_edit", '{"summary":"gave up"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "declined"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "readme\n"
    refusal = json.dumps(client.calls[-1]["messages"])
    assert "not writable" in refusal


async def test_a_plan_outside_the_writable_set_is_refused(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    (workspace / "README.md").write_text("readme\n", encoding="utf-8")
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("README.md"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "readme\n"


async def test_dot_prefixed_candidate_paths_are_listed_read_and_written(tmp_path: Path) -> None:
    """`.github/**` is a legitimate writable set (controller R5), so its files are reachable.

    The tools validate a Candidate path with the Candidate-path grammar, not
    with the policy-rule grammar, which vetoes every dot-prefixed segment: a
    file the artifact policy admitted and `writable_paths` marks writable must
    be visible to `list_files` and open to `read_file` and `write_file`.
    """

    workspace = _ws(tmp_path)
    ci = workspace / ".github" / "workflows" / "ci.yml"
    ci.parent.mkdir(parents=True)
    ci.write_text("on: push\n", encoding="utf-8")
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(".github/workflows/ci.yml"),
            _response("read_file", '{"path":".github/workflows/ci.yml"}'),
            _response(
                "write_file",
                '{"path":".github/workflows/ci.yml","content":"on: pull_request\\n"}',
            ),
            _response("finish_edit", '{"summary":"ci updated"}'),
        ],
    )

    result = await editor.edit(_request(workspace, writable=(".github/**",)))

    assert (result.status, result.note) == ("edited", "ci updated")
    assert ci.read_text(encoding="utf-8") == "on: pull_request\n"
    # The listing carries the path, and the read carries the file's bytes.
    assert ".github/workflows/ci.yml" in json.dumps(client.calls[1]["messages"])
    assert "on: push" in json.dumps(client.calls[3]["messages"])


async def test_admission_refuses_the_changed_file_budget_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.editor import chat_editor

    for name in ("_check_plan_budget", "_check_edit_budget"):
        monkeypatch.setattr(chat_editor._Session, name, lambda self, *args: None, raising=False)
    workspace = _ws(tmp_path)
    (workspace / "skills" / "OTHER.md").write_text("other\n", encoding="utf-8")
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md", "skills/OTHER.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("write_file", '{"path":"skills/OTHER.md","content":"changed\\n"}'),
            _response("finish_edit", '{"summary":"two files"}'),
        ],
        budgets=Budgets(
            max_changed_files=1, max_written_bytes=16 * 1024 * 1024, max_payload_bytes=1024 * 1024
        ),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note == "actor_violation changed-file budget exceeded"
    assert result.code == "changed_file_budget"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"
    assert (workspace / "skills" / "OTHER.md").read_text(encoding="utf-8") == "other\n"


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------


def _bad_yaml_flow(second: str) -> list[Any]:
    return [
        _response("list_files"),
        _plan("skills/config.yaml"),
        _response("write_file", '{"path":"skills/config.yaml","content":"a: [1\\n"}'),
        _response("finish_edit", '{"summary":"first try"}'),
        _response("write_file", json.dumps({"path": "skills/config.yaml", "content": second})),
        _response("finish_edit", '{"summary":"second try"}'),
    ]


async def test_self_check_repair_then_failure(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    config = workspace / "skills" / "config.yaml"
    config.write_text("a: 1\n", encoding="utf-8")
    editor, client = _editor(tmp_path, _bad_yaml_flow("b: [2\n"))

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("self_check_failed: skills/config.yaml:")
    assert result.code == "self_check_failed"
    assert config.read_text(encoding="utf-8") == "a: 1\n"
    repair = [
        message
        for call in client.calls
        for message in call["messages"]
        if str(message.get("content", "")).startswith("self_check:")
    ]
    assert repair


async def test_self_check_repair_that_succeeds_publishes(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    config = workspace / "skills" / "config.yaml"
    config.write_text("a: 1\n", encoding="utf-8")
    editor, _client = _editor(tmp_path, _bad_yaml_flow("b: 2\n"))

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("edited", "second try")
    assert config.read_text(encoding="utf-8") == "b: 2\n"


# --------------------------------------------------------------------------
# Failure classes
# --------------------------------------------------------------------------


async def test_turns_exhausted_discards_the_projection(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
        ],
        max_turns=2,
    )

    result = await editor.edit(_request(workspace))

    assert result.note.startswith("turns_exhausted")
    assert result.status == "failed"
    assert result.code == "turns_exhausted"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_protocol_repairs_exhausted_is_a_protocol_failure(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path, [_response("list_files"), *[_prose_response() for _ in range(3)]]
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("protocol ")
    assert result.code == "protocol"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_provider_error_after_three_failed_attempts(tmp_path: Path, monkeypatch) -> None:
    workspace = _ws(tmp_path)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", no_sleep)
    client = _RetryingClient([], failures=99)
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("provider_error ")
    # The failure happens during preflight -- the first request the session
    # ever makes -- so it is re-coded `preflight_failed` (design step 5); the
    # note itself is untouched.
    assert result.code == "preflight_failed"
    assert "RuntimeError" in result.note
    assert "credential-value" not in result.note


async def test_an_expired_deadline_ends_the_attempt_as_a_provider_error(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(workspace, deadline=0.0))

    assert result.status == "failed"
    assert result.note.startswith("provider_error ")
    # The expired deadline fires on the session's first (preflight) request.
    assert result.code == "preflight_failed"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"
    # The transcript records the failure's own class, not a stand-in: an
    # expired deadline is a `provider_error`, never a `TimeoutError`.
    failed = [
        entry
        for entry in _log_lines(editor)
        if entry.get("phase") == "provider_attempt" and entry["outcome"] == "failed"
    ]
    assert failed, "the expired deadline recorded no failed provider attempt"
    assert [entry["error_type"] for entry in failed] == ["provider_error"] * len(failed)
    assert all("deadline" in (entry["note"] or "") for entry in failed)
    assert all("TimeoutError" not in json.dumps(entry) for entry in failed)


async def test_the_cumulative_written_byte_budget_is_an_actor_violation(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        _edit_flow(),
        budgets=Budgets(max_changed_files=None, max_written_bytes=3, max_payload_bytes=1024 * 1024),
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == (
        "failed",
        "actor_violation written-byte budget exceeded",
    )
    assert result.code == "written_byte_budget"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_provider_changed_when_pinned_provider_differs(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)

    class _WrongProviderClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            response = await super().create(**kwargs)
            response.provider = "B"
            return response

    client = _WrongProviderClient([_response("list_files")])
    editor, _client = _editor(tmp_path, client=client, provider_only=("A",))

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note.split(" ")[0]) == ("failed", "provider_changed")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_prompt_changed_before_the_attempt(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("declared\n", encoding="utf-8")
    editor = ChatEditor(
        section=_section(),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: _Client(_edit_flow()),
    )
    prompt.write_text("rewritten\n", encoding="utf-8")

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "prompt_changed")
    assert not (editor.log_root / "a1.jsonl").exists()


async def test_prompt_changed_mid_session_leaves_the_candidate_untouched(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("declared\n", encoding="utf-8")

    class _RewritingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            prompt.write_text("rewritten mid-session\n", encoding="utf-8")
            return await super().create(**kwargs)

    editor = ChatEditor(
        section=_section(),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: _RewritingClient(_edit_flow()),
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "prompt_changed")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


def _sealed_prompt_editor(
    tmp_path: Path, responses: list[Any], *, rewritten: bool, **kw: Any
) -> tuple[ChatEditor, Path]:
    """An editor whose prompt is a real file, optionally rewritten under the run."""

    prompt = tmp_path / "prompt.md"
    prompt.write_text("declared\n", encoding="utf-8")

    class _Rewriting(_Client):
        async def create(self, **kwargs: Any) -> Any:
            if rewritten:
                prompt.write_text("rewritten mid-run\n", encoding="utf-8")
            return await super().create(**kwargs)

    editor = ChatEditor(
        section=_section(**kw),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: _Rewriting(list(responses)),
    )
    return editor, prompt


def _declining_flow() -> list[Any]:
    return [_response("list_files"), _plan(), _response("finish_edit", '{"summary":"nothing"}')]


async def test_prompt_changed_under_a_declined_run_is_still_refused(tmp_path: Path) -> None:
    """PDR-0005: the seal is verified after *every* outcome, not only before a publication.

    A run that publishes nothing used to return its outcome without a
    post-run check, so a prompt rewritten under it was invisible.
    """

    workspace = _ws(tmp_path)
    editor, _prompt = _sealed_prompt_editor(tmp_path, _declining_flow(), rewritten=True)

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "prompt_changed (after declined)")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"
    assert any(line.get("error_class") == "prompt_changed" for line in _log_lines(editor))


async def test_prompt_changed_under_a_failed_run_keeps_the_class_it_replaced(
    tmp_path: Path,
) -> None:
    """The tampering wins the result, and the outcome it overrode stays in the note."""

    workspace = _ws(tmp_path)
    editor, _prompt = _sealed_prompt_editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
        ],
        rewritten=True,
        max_turns=2,
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "prompt_changed (after turns_exhausted)")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_an_untouched_prompt_leaves_a_declined_run_declined(tmp_path: Path) -> None:
    """The control: the post-run check is a check, not a new failure mode."""

    workspace = _ws(tmp_path)
    editor, prompt = _sealed_prompt_editor(tmp_path, _declining_flow(), rewritten=False)

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("declined", "nothing")
    assert prompt.read_text(encoding="utf-8") == "declared\n"


async def test_workspace_changed_when_the_candidate_is_missing(tmp_path: Path) -> None:
    editor, _client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(tmp_path / "absent"))

    assert result.status == "failed"
    assert result.note.startswith("workspace_changed ")


async def test_anchor_loss_before_publication(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)

    class _RenamingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            response = await super().create(**kwargs)
            if len(self.calls) == 4:
                workspace.rename(tmp_path / "moved")
            return response

    client = _RenamingClient(_edit_flow())
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("workspace_changed ")
    assert (tmp_path / "moved" / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_control_collision_is_reported_as_its_own_class(tmp_path: Path, monkeypatch) -> None:
    workspace = _ws(tmp_path)

    def collide(*args: Any, **kwargs: Any) -> Any:
        raise ControlCollision("Candidate already holds the control prefix: .evolution")

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.project", collide)
    editor, _client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("control_collision ")


async def test_an_unexpected_exception_is_classified_by_its_type(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _ws(tmp_path)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise ZeroDivisionError("boom")

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.self_check", explode)
    editor, _client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "exception:ZeroDivisionError")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"
    assert any(entry.get("phase") == "exception" for entry in _log_lines(editor))


async def test_attempt_id_must_be_one_path_segment(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(tmp_path, _edit_flow())

    with pytest.raises(ValueError, match="one path segment"):
        await editor.edit(_request(workspace, attempt_id="../escape"))


# --------------------------------------------------------------------------
# Cancellation and concurrency
# --------------------------------------------------------------------------


def _editor_scratch_dirs() -> set[str]:
    """The editor scratch directories present in the system temp now."""

    return {
        name for name in os.listdir(tempfile.gettempdir()) if name.startswith("evo-tree-editor-")
    }


async def test_cancellation_mid_provider_call_leaves_candidate_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _ws(tmp_path)
    entered = asyncio.Event()

    class _HangingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    editor, _client = _editor(tmp_path, client=_HangingClient([]))
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    # The editor's scratch directories are made in the system temp directory.
    # The shared one holds residue of interrupted runs (observed 2026-09-20)
    # and, under pytest-xdist, the live scratch of other workers' editors, so
    # this test gets a system temp of its own.
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    task = asyncio.ensure_future(editor.edit(_request(workspace)))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before
    assert _editor_scratch_dirs() == set()


async def test_two_concurrent_attempts_do_not_share_state(tmp_path: Path) -> None:
    first_ws, second_ws = _ws(tmp_path, "one"), _ws(tmp_path, "two")
    first, _c1 = _editor(tmp_path, _edit_flow(content="one\n"))
    second, _c2 = _editor(tmp_path, _edit_flow(content="two\n"))

    results = await asyncio.gather(
        first.edit(_request(first_ws, attempt_id="a1")),
        second.edit(_request(second_ws, attempt_id="a2")),
    )

    assert [result.status for result in results] == ["edited", "edited"]
    assert (first_ws / "skills" / "SKILL.md").read_text(encoding="utf-8") == "one\n"
    assert (second_ws / "skills" / "SKILL.md").read_text(encoding="utf-8") == "two\n"
    assert _log_lines(first, "a1") and _log_lines(second, "a2")


# --------------------------------------------------------------------------
# Transcript
# --------------------------------------------------------------------------


async def test_transcript_is_private_and_redacted(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response(
                "write_file",
                json.dumps({"path": "skills/SKILL.md", "content": f"holds {SECRET}\n"}),
            ),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    log = editor.log_root / "a1.jsonl"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text(encoding="utf-8")
    # The record is the operator's transcript: what the model was sent, what it
    # asked for, and what each tool did -- with the run's secrets blanked.
    assert SECRET not in text
    assert "holds [REDACTED]" in text
    assert "smallest useful change" in text
    entries = _log_lines(editor)
    requests = [entry for entry in entries if entry.get("phase") == "model_request"]
    assert requests[0]["tool_names"] == ["list_files"]  # the preflight offers one tool
    # The first request record carries the whole conversation; every later one
    # carries only what changed since the previous record (W2: transcript growth).
    assert requests[0]["messages_after"] == 0
    # The preflight probe and the main conversation are different lists; a
    # record that shares nothing with its predecessor carries the whole list.
    conversation: list[dict[str, Any]] = []
    for request in requests:
        conversation = conversation[: request["messages_after"]] + request["messages"]
        assert len(conversation) == request["message_count"]
    assert conversation[0] == {"role": "system", "content": TREE_EDITOR_SYSTEM}
    assert any(request["messages_after"] > 0 for request in requests[1:])
    assert "write_file" in requests[-1]["tool_names"]
    write = next(
        entry
        for entry in entries
        if entry.get("phase") == "tool_call" and entry.get("tool") == "write_file"
    )
    assert write["arguments"]["path"] == "skills/SKILL.md"
    assert write["arguments"]["content"] == "holds [REDACTED]\n"
    assert write["result"] == {"written": "skills/SKILL.md"}
    assert [entry.get("tool") for entry in entries if entry.get("phase") == "tool_call"] == [
        "record_plan",
        "write_file",
        "finish_edit",
    ]


# --------------------------------------------------------------------------
# Dialect negotiation and preflight
# --------------------------------------------------------------------------


async def test_legacy_function_calls_are_supported_end_to_end(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    client = _LegacyContinuationRejectingClient(
        [
            _legacy_response("list_files"),
            _plan("skills/SKILL.md", legacy=True),
            _legacy_response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"
    assert len(client.calls[0]["tools"]) == 1
    assert "functions" in client.calls[1]
    assert client.calls[2]["messages"][-1]["role"] == "user"
    assert "write_file" in client.calls[2]["messages"][-1]["content"]


async def test_preflight_falls_back_to_the_legacy_request_format(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    client = _Client(
        [
            _prose_response("I'll call list_files."),
            _legacy_response("list_files"),
            _plan("skills/SKILL.md", legacy=True),
            _legacy_response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert "tools" in client.calls[0]
    assert client.calls[1]["tool_choice"] == "auto"
    assert "parallel_tool_calls" not in client.calls[1]
    assert all("functions" in call and "tools" not in call for call in client.calls[2:])


async def test_the_preflight_probe_tool_and_message_are_parameters(tmp_path: Path) -> None:
    """Another role (the family-15 Grouper) probes with its own read tool: the
    one offered tool, the forced legacy call, the accepted name and the
    preflight record all follow `probe_tool`, the user message `probe_message`."""

    from evolution.tree.roles.editor.chat_editor import _tool, negotiate_style
    from evolution.tree.roles.workspace import open_attempt_log

    tools = [_tool("list_case", {"case": {"type": "string"}}, ["case"]), _tool("finish", {})]
    client = _Client(
        [
            _prose_response("I will call list_case."),
            _prose_response("I will call list_case."),
            _legacy_response("list_case", '{"case":"c1"}'),
        ]
    )
    turns = iter(range(10))
    with open_attempt_log(tmp_path / "logs", "probe", secrets=()) as log:
        style = await negotiate_style(
            client=client,
            section=_section(),
            log=log,
            tools=tools,
            next_turn=lambda: next(turns),
            remaining=lambda: 30.0,
            record=lambda *_: None,
            role="grouper",
            probe_tool="list_case",
            probe_message="Call list_case now.",
        )
    lines = [
        json.loads(line)
        for line in (tmp_path / "logs" / "probe.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert style == "legacy"
    assert client.calls[0]["messages"][0] == {"role": "user", "content": "Call list_case now."}
    assert [tool["function"]["name"] for tool in client.calls[0]["tools"]] == ["list_case"]
    assert client.calls[2]["function_call"] == {"name": "list_case"}
    assert "preflight requires list_case" in client.calls[2]["messages"][-1]["content"]
    assert lines[-1] == {"phase": "preflight", "tool": "list_case", "request_style": "legacy"}


async def test_preflight_falls_back_when_the_modern_request_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _ws(tmp_path)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", no_sleep)
    client = _ModernRejectingClient(
        [
            _legacy_response("list_files"),
            _plan("skills/SKILL.md", legacy=True),
            _legacy_response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert all("tools" in call for call in client.calls[:6])
    assert all("functions" in call for call in client.calls[6:])


async def test_preflight_falls_back_to_modern_auto_tool_choice(tmp_path: Path, monkeypatch) -> None:
    workspace = _ws(tmp_path)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", no_sleep)

    class _RequiredRejectingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if kwargs.get("tool_choice") == "required":
                raise RuntimeError("unsupported tool_choice")
            return next(self._responses)

    client = _RequiredRejectingClient(_edit_flow())
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert all(call.get("tool_choice") == "required" for call in client.calls[:3])
    assert all(call.get("tool_choice") == "auto" for call in client.calls[3:])
    assert all("parallel_tool_calls" not in call for call in client.calls[3:])


async def test_a_zai_pin_starts_with_the_supported_auto_tool_choice(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, client = _editor(tmp_path, _edit_flow(), provider_only=("z-ai",))

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert all(call.get("tool_choice") == "auto" for call in client.calls)


async def test_openrouter_required_tools_omit_the_parallel_tool_parameter(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, client = _editor(tmp_path, _edit_flow(), base_url="https://openrouter.ai/api/v1")

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert all("parallel_tool_calls" not in call for call in client.calls)


async def test_preflight_that_never_calls_list_files_is_a_protocol_failure(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(tmp_path, [_prose_response() for _ in range(6)])

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("protocol ")
    assert result.code == "preflight_failed"


async def test_a_retried_provider_call_records_every_attempt(tmp_path: Path, monkeypatch) -> None:
    workspace = _ws(tmp_path)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", no_sleep)
    client = _RetryingClient(_edit_flow(), failures=2)
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    attempts = [
        (entry["attempt"], entry["outcome"])
        for entry in _log_lines(editor)
        if entry.get("phase") == "provider_attempt" and entry["outcome"] != "started"
    ]
    assert attempts[:3] == [(1, "failed"), (2, "failed"), (3, "succeeded")]
    assert all("credential-value" not in json.dumps(entry) for entry in _log_lines(editor))


# --------------------------------------------------------------------------
# Plan discipline
# --------------------------------------------------------------------------


async def test_a_mutation_before_record_plan_is_refused(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"early\\n"}'),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"
    assert "record_plan is required before a file change" in json.dumps(client.calls[-1])


async def test_a_change_outside_the_recorded_plan_is_refused(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    (workspace / "skills" / "OTHER.md").write_text("other\n", encoding="utf-8")
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/OTHER.md","content":"hijack\\n"}'),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert (workspace / "skills" / "OTHER.md").read_text(encoding="utf-8") == "other\n"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"


async def test_an_editor_that_drops_a_planned_file_still_ships_the_edits_it_made(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    (workspace / "skills" / "OTHER.md").write_text("other\n", encoding="utf-8")
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md", "skills/OTHER.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"partial"}'),
            _response("finish_edit", '{"summary":"partial"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("edited", "partial")
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"
    assert (workspace / "skills" / "OTHER.md").read_text(encoding="utf-8") == "other\n"


async def test_a_plan_that_contradicts_the_diff_after_the_nudge_is_plan_mismatch(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _plan("skills/SKILL.md", action="delete"),
            _response("finish_edit", '{"summary":"done"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("plan_mismatch ")
    assert result.code == "plan_mismatch"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


# --------------------------------------------------------------------------
# Tool semantics
# --------------------------------------------------------------------------


def test_read_file_supports_line_ranges(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    content, start, end, total, digest = _read_file_range(path, {"start_line": 2, "end_line": 3})

    assert (content, start, end, total) == ("two\nthree\n", 2, 3, 3)
    assert len(digest) == 64


def test_inspect_file_reports_size_preview_and_sections(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("# Title\n\nbody\n\n## Sub\n\nmore\n", encoding="utf-8")

    info = _inspect_file(path, tmp_path)

    assert info["path"] == "SKILL.md"
    assert info["line_count"] == 7
    assert [section["title"] for section in info["sections"]] == ["Title", "Sub"]


def test_apply_patch_inserts_a_zero_hunk_at_file_start() -> None:
    patch = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -0,0 +1 @@\n+added\n"

    changes = _parse_patch(patch)

    assert list(changes) == ["SKILL.md"]
    assert _apply_hunks("body\n", changes["SKILL.md"]) == "added\nbody\n"


async def test_replace_text_refuses_an_old_text_that_is_not_unique(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    skill = workspace / "skills" / "SKILL.md"
    skill.write_text("repeat\nrepeat\n", encoding="utf-8")
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            _response(
                "replace_text",
                json.dumps({"path": "skills/SKILL.md", "old_text": "repeat", "new_text": "once"}),
            ),
            _response(
                "replace_text",
                json.dumps(
                    {
                        "path": "skills/SKILL.md",
                        "old_text": "repeat\nrepeat\n",
                        "new_text": "once\n",
                    }
                ),
            ),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert skill.read_text(encoding="utf-8") == "once\n"
    errors = [entry for entry in _log_lines(editor) if entry.get("phase") == "tool_error"]
    assert [entry["tool"] for entry in errors] == ["replace_text"]


async def test_a_tool_that_keeps_failing_stops_being_offered(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    bad = _response(
        "apply_patch",
        json.dumps(
            {
                "patch": "--- a/skills/SKILL.md\n+++ b/skills/SKILL.md\n"
                "@@ -1,1 +1,1 @@\n-absent\n+present\n"
            }
        ),
    )
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan("skills/SKILL.md"),
            *[bad for _ in range(MAX_CONSECUTIVE_TOOL_FAILURES)],
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    offered = [
        {tool["function"]["name"] for tool in call["tools"]}
        for call in client.calls
        if "tools" in call
    ]
    assert "apply_patch" in offered[1]
    assert "apply_patch" not in offered[-1]


async def test_edit_yaml_is_not_offered_without_a_writable_yaml_file(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, client = _editor(tmp_path, _edit_flow())

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    offered = [
        {tool["function"]["name"] for tool in call["tools"]}
        for call in client.calls
        if "tools" in call and len(call["tools"]) > 1
    ]
    assert offered and all("edit_yaml" not in names for names in offered)


async def test_the_first_of_several_parallel_calls_runs_and_the_rest_are_dropped(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    client = _Client(
        [
            _parallel_response(("list_files", "{}"), ("read_file", '{"path":"skills/SKILL.md"}')),
            _plan("skills/SKILL.md"),
            _parallel_response(
                ("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
                ("write_file", '{"path":"skills/SKILL.md","content":"loser\\n"}'),
            ),
            _response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"
    dropped = [
        entry for entry in _log_lines(editor) if entry.get("phase") == "parallel_calls_dropped"
    ]
    assert [(entry["tool"], [call["name"] for call in entry["calls"]]) for entry in dropped] == [
        ("write_file", ["write_file"])
    ]


async def test_a_failing_tool_call_does_not_spend_the_protocol_repair_budget(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("read_file", '{"path":"MISSING_ONE.md"}'),
            _response("read_file", '{"path":"MISSING_TWO.md"}'),
            _response("read_file", '{"path":"MISSING_THREE.md"}'),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"


async def test_an_unknown_tool_name_is_a_protocol_repair(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("edit_skill", "{}"),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert "unknown tool: edit_skill" in json.dumps(client.calls[-1])


# --------------------------------------------------------------------------
# Evidence pagination over the rendered catalog
# --------------------------------------------------------------------------


def test_catalog_page_stays_within_its_byte_budget() -> None:
    catalog = tuple(
        EvidenceRecord(f"observation-{index:03d}-" + "x" * 100, "failure_observation")
        for index in range(100)
    )

    records = _evidence_page(catalog, 0)

    response = {"records": records, "next_offset": len(records), "total": len(catalog)}
    assert len(json.dumps(response, separators=(",", ":")).encode()) <= EVIDENCE_PAGE_BYTES
    assert 0 < len(records) < 100


async def test_evidence_pagination_over_the_rendered_catalog(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    brief = _brief(cases=70)
    catalog = render_catalog(brief)
    assert catalog[0].evidence_id == "summary"
    client = _CapturingClient(
        [
            _response("list_files"),
            _response("list_evidence"),
            _response("list_evidence", '{"offset":64}'),
            _response("read_evidence", '{"evidence_id":"summary"}'),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace, brief=brief))

    assert result.status == "edited"
    pages = [
        json.loads(message)["records"]
        for message in _unique(client.messages)
        if message.startswith('{"records":')
    ]
    assert [len(page) for page in pages] == [64, 8]
    assert pages[0][0]["id"] == "summary"
    usage = next(entry for entry in _log_lines(editor) if entry.get("phase") == "evidence_usage")
    assert usage["catalog_listed"] is True
    assert (usage["read_ids"], len(usage["unread_cases"]), usage["severity"]) == (
        ["summary"],
        70,
        "warning",
    )


def _brief_v2() -> EditBrief:
    from evolution.tree.roles.editor_contracts import (
        AttemptChanges,
        AttemptResult,
        BriefHistory,
        HistoryAttempt,
        HistoryRound,
        Observation,
        ObservationInput,
        ObservationTurn,
    )

    observation = Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "ok",
                ObservationInput("present", "hello", False),
                ({"type": "assistant_message", "content": "TARGET-SAID-THIS", "truncated": False},),
                0,
                False,
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        "a" * 64,
        "edited",
        None,
        "rejected",
        "upper_bound_nonpositive",
        None,
        3,
        "c" * 64,
        AttemptChanges("present", ("skills/SKILL.md",), False),
        AttemptResult("absent"),
    )
    history = BriefHistory(1, (1,), 0, (HistoryRound(1, "stalled", None, (attempt,), False),))
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=(
            EditBriefCase(
                case_id="c1",
                group_id="g1",
                stratum="s1",
                tags=("t",),
                public={"text": "hello"},
                repeats=(EditBriefRepeat(1, "c1-r1", "rollout_finished", 0.5, True, observation),),
            ),
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
        history=history,
    )


async def test_v2_evidence_catalog_offers_observation_and_attempt_and_coverage_still_names_only_cases(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    brief = _brief_v2()
    client = _CapturingClient(
        [
            _response("list_files"),
            _response("list_evidence"),
            *_edit_flow()[1:],
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace, brief=brief))

    assert result.status == "edited"
    pages = [
        json.loads(message)["records"]
        for message in _unique(client.messages)
        if message.startswith('{"records":')
    ]
    kinds = {record["kind"] for page in pages for record in page}
    assert {"observation", "attempt"} <= kinds

    usage = next(entry for entry in _log_lines(editor) if entry.get("phase") == "evidence_usage")
    assert usage["catalog_listed"] is True
    assert usage["unread_cases"] == ["case:c1"]


async def test_read_evidence_walks_a_long_record_by_offset(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    brief = _brief(cases=700)
    summary = render_catalog(brief)[0]
    assert len(summary.content) > 64_000
    client = _CapturingClient(
        [
            _response("list_files"),
            _response("read_evidence", '{"evidence_id":"summary"}'),
            _response("read_evidence", '{"evidence_id":"summary"}'),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace, brief=brief))

    assert result.status == "edited"
    chunks = [
        json.loads(message)
        for message in _unique(client.messages)
        if message.startswith('{"id":"summary"')
    ]
    assert len(chunks) == 2
    assert chunks[0]["next_offset"] == len(chunks[0]["content"])
    assert chunks[1]["next_offset"] is None
    assert "".join(chunk["content"] for chunk in chunks) == summary.content


async def test_reading_every_case_record_clears_the_unread_accounting(
    tmp_path: Path,
) -> None:
    workspace = _ws(tmp_path)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("read_evidence", '{"evidence_id":"case:c1"}'),
            _plan("skills/SKILL.md"),
            _response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    usage = next(entry for entry in _log_lines(editor) if entry.get("phase") == "evidence_usage")
    assert (usage["unread_cases"], usage["severity"]) == ([], "info")
    assert usage["catalog_listed"] is False


async def test_unread_catalog_content_never_reaches_the_model(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    brief = EditBrief(
        champion_digest="a" * 64,
        operator_context=None,
        cases=(
            EditBriefCase(
                case_id="c1",
                group_id="g1",
                stratum="s1",
                tags=(),
                public={"text": "unmistakable-observation"},
                repeats=(EditBriefRepeat(1, "c1-r1", "rollout_finished", 0.1, False),),
            ),
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )
    client = _CapturingClient(_edit_flow())
    editor, _client = _editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace, brief=brief))

    assert result.status == "edited"
    assert not any("unmistakable-observation" in message for message in client.messages)


# --------------------------------------------------------------------------
# Compaction
# --------------------------------------------------------------------------


def test_compaction_keeps_complete_tool_exchanges_before_a_correction() -> None:
    first = assistant_message(_response("read_file").choices[0].message)
    second = assistant_message(_response("read_file").choices[0].message)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "brief"},
        *tool_exchange(first, {"content": "a" * 150_000}),
        *tool_exchange(second, {"content": "b" * 150_000}),
        {"role": "user", "content": "correct the last call"},
    ]

    _compact_messages(
        messages,
        trigger_chars=DEFAULT_CONTEXT_COMPACT_CHARS,
        message_max_chars=DEFAULT_CONTEXT_MESSAGE_MAX_CHARS,
    )

    assert all(
        message.get("role") != "tool"
        or (
            index > 0
            and messages[index - 1].get("role") == "assistant"
            and messages[index - 1]["tool_calls"][0]["id"] == message["tool_call_id"]
        )
        for index, message in enumerate(messages)
    )


def test_compaction_keeps_the_system_prompt_and_opening_intact() -> None:
    call = assistant_message(_response("read_file").choices[0].message)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "opening"},
        *[message for _ in range(6) for message in tool_exchange(call, {"content": "c" * 40_000})],
    ]

    _compact_messages(
        messages,
        trigger_chars=DEFAULT_CONTEXT_COMPACT_CHARS,
        message_max_chars=DEFAULT_CONTEXT_MESSAGE_MAX_CHARS,
    )

    assert messages[0] == {"role": "system", "content": "system prompt"}
    assert messages[1] == {"role": "user", "content": "opening"}
    assert messages[2]["content"].startswith("Earlier editor context was compacted.")


def test_compaction_waits_for_the_declared_trigger() -> None:
    call = assistant_message(_response("read_file").choices[0].message)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "opening"},
        *[message for _ in range(6) for message in tool_exchange(call, {"content": "c" * 40_000})],
    ]
    original = [dict(message) for message in messages]

    _compact_messages(messages, trigger_chars=400_000, message_max_chars=12_000)
    assert messages == original

    _compact_messages(messages, trigger_chars=200_000, message_max_chars=12_000)
    assert messages[2]["content"].startswith("Earlier editor context was compacted.")


def test_compaction_truncates_tail_messages_at_the_declared_size() -> None:
    call = assistant_message(_response("read_file").choices[0].message)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "opening"},
        *tool_exchange(call, {"content": "a" * 150_000}),
        *tool_exchange(call, {"content": "b" * 30_000}),
        {"role": "user", "content": "continue"},
    ]

    _compact_messages(messages, trigger_chars=160_000, message_max_chars=40_000)

    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert tool_messages
    assert all(len(message["content"]) <= 40_000 + 60 for message in tool_messages)
    assert any(len(message["content"]) > 12_000 for message in tool_messages)


async def test_the_editor_compacts_at_its_section_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.editor import chat_editor

    seen: list[tuple[int, int]] = []

    def spy(messages: list[dict[str, Any]], *, trigger_chars: int, message_max_chars: int) -> None:
        seen.append((trigger_chars, message_max_chars))

    monkeypatch.setattr(chat_editor, "_compact_messages", spy)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("list_files"),
            _plan(),
            _response("finish_edit", '{"summary":"nothing to do"}'),
        ],
        context_compact_chars=400_000,
        context_message_max_chars=60_000,
    )

    result = await editor.edit(_request(_ws(tmp_path)))

    assert result.status == "declined"
    assert seen and set(seen) == {(400_000, 60_000)}


# --------------------------------------------------------------------------
# Attempt setup: everything the attempt owns lives inside its own handlers
# --------------------------------------------------------------------------


def _bare_editor(tmp_path: Path, **kw: Any) -> ChatEditor:
    """An editor with no `client_factory`, so `_new_client` is the seam."""

    return ChatEditor(
        section=_section(**kw),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
    )


def _watch_discard(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every discarded projection, still discarding it for real."""

    import evolution.tree.roles.editor.chat_editor as module

    real = module.discard
    seen: list[Any] = []

    def recording(projection: Any) -> None:
        seen.append(projection)
        real(projection)

    monkeypatch.setattr(module, "discard", recording)
    return seen


async def test_a_client_that_cannot_be_built_is_the_attempts_own_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_new_client`'s packaging guard raises `EditFailure`; built outside the
    attempt's handlers it would escape as an unhandled exception instead.
    """

    workspace = _ws(tmp_path)
    editor = _bare_editor(tmp_path)

    def refuse(section: Any, credentials: Any) -> Any:
        raise EditFailure("provider_error", "install evo-skills[tree]")

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor._new_client", refuse)
    discarded = _watch_discard(monkeypatch)

    result = await editor.edit(_request(workspace))

    assert result.status == "failed"
    assert result.note.startswith("provider_error")
    assert "install evo-skills[tree]" in result.note
    assert len(discarded) == 1
    assert not discarded[0].root.exists()
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_the_client_is_closed_once_when_the_session_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _ws(tmp_path)

    class _ClosingClient(_Client):
        def __init__(self) -> None:
            super().__init__([])
            self.closes = 0

        async def close(self) -> None:
            self.closes += 1

    client = _ClosingClient()
    editor, _client = _editor(tmp_path, client=client)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("session refused by the test")

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor._Session.__init__", refuse)
    discarded = _watch_discard(monkeypatch)

    result = await editor.edit(_request(workspace))

    assert (result.status, result.note) == ("failed", "exception:RuntimeError")
    assert client.closes == 1
    assert len(discarded) == 1
    assert (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"


async def test_a_long_non_ascii_case_id_reaches_an_outcome(tmp_path: Path) -> None:
    """A `case_id` whose escaped evidence id would overflow the 128-character
    cap must not turn every attempt of the round into `exception:ValueError`.
    """

    workspace = _ws(tmp_path)
    case_id = "ссылка" * 6 + "ссыл"
    assert len(case_id) == 40
    brief = EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=(
            EditBriefCase(
                case_id=case_id,
                group_id="g1",
                stratum="s1",
                tags=("t",),
                public={"text": "hello"},
                repeats=(EditBriefRepeat(1, "r1", "rollout_finished", 0.5, True),),
            ),
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )
    editor, _client = _editor(
        tmp_path,
        [_response("list_files"), _plan(), _response("finish_edit", '{"summary":"nothing to do"}')],
    )

    result = await editor.edit(_request(workspace, brief=brief))

    assert result.status in {"declined", "edited"}
    assert "ValueError" not in result.note


def test_request_records_are_deltas_that_survive_compaction() -> None:
    """The shared prefix shrinks when compaction rewrites the middle of the list."""

    from evolution.tree.roles.editor.chat_editor import _message_key, _request_audit, _shared_prefix

    system = {"role": "system", "content": "s"}
    a, b, c, d = ({"role": "user", "content": x} for x in "abcd")
    first = [system, a, b]
    second = [system, a, b, c]
    compacted = [system, {"role": "user", "content": "summary"}, c, d]
    keys = [[_message_key(m) for m in conv] for conv in (first, second, compacted)]
    assert _shared_prefix([], keys[0]) == 0
    assert _shared_prefix(keys[0], keys[1]) == 3
    assert _shared_prefix(keys[1], keys[2]) == 1
    record = _request_audit(second, "modern", 2, [], after=3)
    assert (record["messages_after"], record["messages"], record["message_count"]) == (3, [c], 4)
    rewritten = _request_audit(compacted, "modern", 3, [], after=1)
    assert rewritten["messages"] == compacted[1:]


# --------------------------------------------------------------------------
# ADR-0037 D2/D3: the file budget refuses a plan or an edit as a tool error
# --------------------------------------------------------------------------


def _budget(limit: int) -> Budgets:
    return Budgets(
        max_changed_files=limit, max_written_bytes=16 * 1024 * 1024, max_payload_bytes=1024 * 1024
    )


def _many(workspace: Path, count: int) -> list[str]:
    paths = []
    for index in range(1, count + 1):
        relative = f"skills/f{index:02d}.md"
        (workspace / relative).write_text(f"file {index}\n", encoding="utf-8")
        paths.append(relative)
    return paths


def _write(path: str, content: str) -> Any:
    return _response("write_file", json.dumps({"path": path, "content": content}))


def _errors(editor: ChatEditor) -> list[dict[str, Any]]:
    return [line for line in _log_lines(editor) if line["phase"] == "tool_error"]


async def test_an_over_budget_plan_is_refused_before_any_edit(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    paths = _many(workspace, 12)
    small = paths[:3]
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(*paths),
            _plan(*small),
            *[_write(path, "changed\n") for path in small],
            _response("finish_edit", '{"summary":"three files"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert [p for p in paths if (workspace / p).read_text(encoding="utf-8") == "changed\n"] == small
    (error,) = _errors(editor)
    assert error["tool"] == "record_plan" and error["turn"] <= 2
    assert "12 files" in error["error"] and "at most 3 files" in error["error"]
    assert "highest-mass hypothesis" in error["error"]
    assert error["error"].endswith(
        "a planned update counts toward this plan even if the file later returns to "
        "its original bytes"
    )
    lines = _log_lines(editor)
    assert not [line for line in lines if line["phase"] == "protocol_repair_requested"]
    first_write = next(i for i, line in enumerate(lines) if line.get("tool") == "write_file")
    assert lines.index(error) < first_write


async def test_a_restored_file_frees_its_slot_at_plan_time(tmp_path: Path) -> None:
    """D2 counts the live changed set: a restore makes room for a new plan.

    The cumulative plan still names the restored path, so the first
    `finish_edit` earns today's one nudge and the second is accepted -- a
    planned path that ends byte-identical is never a `plan_mismatch` failure."""

    workspace = _ws(tmp_path)
    a, b, c, d = _many(workspace, 4)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(a, b, c),
            _write(a, "changed\n"),
            _write(b, "changed\n"),
            _write(c, "changed\n"),
            _write(a, "file 1\n"),  # the original bytes
            _plan(d),
            _write(d, "changed\n"),
            _response("finish_edit", '{"summary":"b c d"}'),
            _response("finish_edit", '{"summary":"b c d"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    assert _errors(editor) == []
    assert (workspace / a).read_text(encoding="utf-8") == "file 1\n"
    assert all((workspace / p).read_text(encoding="utf-8") == "changed\n" for p in (b, c, d))
    repairs = [line for line in _log_lines(editor) if line["phase"] == "protocol_repair_requested"]
    assert len(repairs) == 1 and "does not exactly match" in repairs[0]["error"]


async def test_the_edit_guard_lists_the_changed_files_and_a_restore_frees_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 holds even when a plan got past D2 (patched out here)."""

    from evolution.tree.roles.editor import chat_editor

    monkeypatch.setattr(chat_editor._Session, "_check_plan_budget", lambda self, plan: None)
    workspace = _ws(tmp_path)
    a, b, c, d = _many(workspace, 4)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(a, b, c, d),
            _write(a, "changed\n"),
            _write(b, "changed\n"),
            _write(c, "changed\n"),
            _write(d, "changed\n"),  # refused: a, b, c already differ
            _write(a, "file 1\n"),  # restore: frees a slot
            _write(d, "changed\n"),
            _response("finish_edit", '{"summary":"b c d"}'),
            _response("finish_edit", '{"summary":"b c d"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    (error,) = _errors(editor)
    assert error["tool"] == "write_file"
    assert "at most 3 files" in error["error"]
    assert f"{a}, {b}, {c}" in error["error"]
    assert error["error"].endswith(
        "Change only these files, or write an updated file's original bytes back to free "
        "its slot; a deleted file cannot be restored this way"
    )
    assert not [
        line
        for line in _log_lines(editor)
        if line["phase"] == "protocol_repair_requested" and "budget" in line["error"]
    ]


def _patch(*edits: tuple[str, str, str]) -> Any:
    """One `apply_patch` call replacing each path's single line `old` with `new`."""

    patch = "".join(
        f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{old}+{new}" for path, old, new in edits
    )
    return _response("apply_patch", json.dumps({"patch": patch}))


async def test_the_edit_guard_counts_every_path_of_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 on `apply_patch`: the patch's paths join the live changed set."""

    from evolution.tree.roles.editor import chat_editor

    monkeypatch.setattr(chat_editor._Session, "_check_plan_budget", lambda self, plan: None)
    workspace = _ws(tmp_path)
    a, b, c, d = _many(workspace, 4)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(a, b, c, d),
            _write(a, "changed\n"),
            _write(b, "changed\n"),
            _patch((c, "file 3\n", "patched\n"), (d, "file 4\n", "patched\n")),  # refused
            _patch((c, "file 3\n", "patched\n")),
            _response("finish_edit", '{"summary":"a b c"}'),
            _response("finish_edit", '{"summary":"a b c"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    (error,) = _errors(editor)
    assert error["tool"] == "apply_patch"
    assert error["error"].startswith(
        f"apply_patch refused: this Attempt may change at most 3 files, and 2 files already "
        f"differ from the Candidate it started from: {a}, {b}."
    )
    assert (workspace / c).read_text(encoding="utf-8") == "patched\n"
    assert (workspace / d).read_text(encoding="utf-8") == "file 4\n"
    assert not [
        line
        for line in _log_lines(editor)
        if line["phase"] == "protocol_repair_requested" and "budget" in line["error"]
    ]


async def test_the_edit_guard_refuses_a_delete_over_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 on `delete_file`: a delete is one more changed file; a write-back frees a slot."""

    from evolution.tree.roles.editor import chat_editor

    monkeypatch.setattr(chat_editor._Session, "_check_plan_budget", lambda self, plan: None)
    workspace = _ws(tmp_path)
    a, b, c, d = _many(workspace, 4)
    plan = _response(
        "record_plan",
        json.dumps(
            {
                "summary": "three updates and a delete",
                "changes": [
                    *[
                        {"path": path, "action": "update", "purpose": "repair behavior"}
                        for path in (a, b, c)
                    ],
                    {"path": d, "action": "delete", "purpose": "remove a stale file"},
                ],
            }
        ),
    )
    delete = _response("delete_file", json.dumps({"path": d}))
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            plan,
            _write(a, "changed\n"),
            _write(b, "changed\n"),
            _write(c, "changed\n"),
            delete,  # refused: a, b, c already differ
            _write(a, "file 1\n"),  # the original bytes: frees a slot
            delete,
            _response("finish_edit", '{"summary":"b c, delete d"}'),
            _response("finish_edit", '{"summary":"b c, delete d"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert result.status == "edited"
    (error,) = _errors(editor)
    assert error["tool"] == "delete_file"
    assert f"{a}, {b}, {c}" in error["error"]
    assert error["error"].endswith("a deleted file cannot be restored this way")
    assert not (workspace / d).exists()
    assert (workspace / a).read_text(encoding="utf-8") == "file 1\n"


async def test_a_persistent_over_budget_plan_ends_in_protocol_exhaustion(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    paths = _many(workspace, 12)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            *[_plan(*paths) for _ in range(5)],  # five consecutive refusals block record_plan
            *[_write(paths[0], "changed\n") for _ in range(3)],
        ],
        budgets=_budget(3),
        max_turns=20,
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.code) == ("failed", "protocol")
    assert all((workspace / p).read_text(encoding="utf-8").startswith("file ") for p in paths)
    assert len(_errors(editor)) == 5


async def test_without_a_file_budget_nothing_is_refused(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    paths = _many(workspace, 12)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(*paths),
            *[_write(path, "changed\n") for path in paths],
            _response("finish_edit", '{"summary":"all"}'),
        ],
        max_turns=20,
    )
    result = await editor.edit(_request(workspace))
    assert result.status == "edited" and _errors(editor) == []


async def test_an_unreadable_projection_in_a_budget_check_fails_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `WorkspaceError` from the budget's live count is neither a pass nor a tool error.

    It propagates like the loop's other live reads of the projection (the
    `finish_edit` diff): the attempt ends `failed` with the generic
    `exception` code, and no file changes."""

    from evolution.tree.execution.private_files import WorkspaceError
    from evolution.tree.roles.editor import chat_editor

    def unreadable(projection: Any) -> tuple[str, ...]:
        raise WorkspaceError("Candidate workspace holds an unsupported entry: skills/x")

    monkeypatch.setattr(chat_editor, "changed_file_paths", unreadable)
    workspace = _ws(tmp_path)
    paths = _many(workspace, 2)
    editor, _client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(*paths),
            *[_write(path, "changed\n") for path in paths],
            _response("finish_edit", '{"summary":"two"}'),
        ],
        budgets=_budget(3),
    )

    result = await editor.edit(_request(workspace))

    assert (result.status, result.code) == ("failed", "exception")
    assert _errors(editor) == []
    assert all((workspace / p).read_text(encoding="utf-8").startswith("file ") for p in paths)


async def test_the_chat_opening_states_the_file_budget(tmp_path: Path) -> None:
    for name, budgets, stated in (
        ("budget", Budgets(3, 16 * 1024 * 1024, 1024 * 1024), True),
        ("none", None, False),
    ):
        workspace = _ws(tmp_path / name)
        client = _CapturingClient(_edit_flow())
        kwargs = {} if budgets is None else {"budgets": budgets}
        editor, _ = _editor(tmp_path / name, client=client, **kwargs)
        result = await editor.edit(_request(workspace))
        assert result.status == "edited"
        opening = next(text for text in _unique(client.messages) if text.startswith("context"))
        assert ("Budget: change at most 3 files in this Attempt" in opening) is stated
        assert "VALIDATION.json" not in opening  # the agent-CLI sentence only


async def test_the_chat_editor_reads_a_trace_page_in_a_pinned_round(tmp_path):
    from dataclasses import replace

    from tests.test_tree_baseline_traces import reader
    from tests.tree.roles.editor.test_editor_evidence import _Trace

    trace = _Trace(tmp_path / "baseline-trace", turns=1)
    try:
        traces = reader(trace)
        flow = _edit_flow()
        flow[1:1] = [_response("read_trace", json.dumps({"evidence_id": "obs:c:1"}))]
        editor, client = _editor(tmp_path, flow)
        request = replace(_request(_ws(tmp_path)), traces=traces)
        result = await editor.edit(request)
        assert result.status == "edited", result.note
        offered = [tool["function"]["name"] for tool in client.calls[-1]["tools"]]
        assert offered[offered.index("read_evidence") + 1] == "read_trace"
        assert any(
            json.loads(message["content"]).get("content") == traces.page("obs:c:1")["content"]
            for message in client.calls[-1]["messages"]
            if message.get("role") == "tool"
        )
    finally:
        trace.close()


def test_chat_tools_1_bytes_are_unchanged():
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.editor.chat_editor import TOOLS

    assert (
        hashlib.sha256(canonical_json(TOOLS)).hexdigest()
        == "d36a5f626240e165967a8f463d64e9dc3340c7ca48d7918d3f5d7ee18b713ca8"
    )


@pytest.mark.parametrize("limit", [0, 5])
async def test_declared_editor_protocol_repair_limit(tmp_path, limit):
    workspace = _ws(tmp_path)
    editor, client = _editor(
        tmp_path,
        [_response("list_files"), *[_prose_response() for _ in range(4)], *_edit_flow()[1:]],
        max_protocol_repairs=limit,
        max_turns=16,
    )
    result = await editor.edit(_request(workspace))
    assert result.status == ("failed" if limit == 0 else "edited")
    if limit == 0:
        assert result.code == "protocol"
        assert len(client.calls) == 2


@pytest.mark.parametrize("limit", [1, 7])
async def test_declared_editor_tool_failure_limit(tmp_path, limit):
    workspace = _ws(tmp_path)
    bad = _response("read_file", '{"path":"missing.txt"}')
    editor, client = _editor(
        tmp_path,
        [_response("list_files"), *[bad for _ in range(limit)], *_edit_flow()[1:]],
        max_consecutive_tool_failures=limit,
        max_turns=20,
    )
    result = await editor.edit(_request(workspace))
    assert result.status == "edited"
    offered = [{t["function"]["name"] for t in call["tools"]} for call in client.calls]
    assert "read_file" in offered[limit]
    assert "read_file" not in offered[limit + 1]


@pytest.mark.parametrize("limit", [0, 5])
async def test_declared_protocol_limit_controls_style_preflight_repairs(tmp_path, limit):
    workspace = _ws(tmp_path)
    editor, client = _editor(
        tmp_path,
        [
            *[_prose_response() for _ in range(6)],
            _legacy_response("list_files"),
            _plan("skills/SKILL.md", legacy=True),
            _legacy_response("write_file", '{"path":"skills/SKILL.md","content":"second\\n"}'),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ],
        max_protocol_repairs=limit,
        max_turns=20,
    )
    result = await editor.edit(_request(workspace))
    assert result.status == ("failed" if limit == 0 else "edited")
    if limit == 0:
        # Two modern dialect probes, then the first legacy call; no repair.
        assert len(client.calls) == 3
        assert result.code == "preflight_failed"


class _Usage:
    def model_dump(self):
        return {"prompt_tokens": 7, "completion_tokens": 2}


def _audit_response(usage):
    message = SimpleNamespace(content="hello", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="length")
    return SimpleNamespace(id="r1", model="m", choices=[choice], usage=usage)


def test_response_audit_unchanged():
    section = SimpleNamespace(
        model="m", provider_only=None, provider_order=None, provider_allow_fallbacks=None
    )
    record = _response_audit(_audit_response(_Usage()), section, "modern", 3)
    assert record["finish_reason"] == "length"
    assert record["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}
    assert record["content"] == "hello"
    assert record["turn"] == 3
    assert response_telemetry(_audit_response(_Usage())) == (
        "length",
        {"prompt_tokens": 7, "completion_tokens": 2},
    )
    assert response_telemetry(_audit_response(None)) == ("length", None)
    assert response_telemetry(_audit_response("not a dict")) == ("length", None)
    assert response_telemetry(SimpleNamespace(choices=[SimpleNamespace()])) == (None, None)


@pytest.mark.parametrize("family15", [False, True])
@pytest.mark.parametrize("mode", ["normal", "error", "repair", "correction"])
async def test_editor_dropped_calls_named_in_first_result_only_in_v16(tmp_path, family15, mode):
    from dataclasses import replace

    from tests.test_tree_chat_role_retry import _resilient_editor, _ScriptedClient

    class RecordingClient(_ScriptedClient):
        async def create(self, **kwargs):
            # Keep the sent request before the loop appends later turns.
            kwargs["messages"] = json.loads(json.dumps(kwargs["messages"]))
            return await super().create(**kwargs)

    first = (
        "unknown" if mode == "repair" else "finish_edit" if mode == "correction" else "read_file"
    )
    args = (
        '{"summary":"done"}'
        if mode == "correction"
        else '{"path":"MISSING.md"}'
        if mode == "error"
        else '{"path":"skills/SKILL.md"}'
    )
    client = RecordingClient(
        [
            _response("list_files"),
            _parallel_response(
                (first, args), ("list_files", "{}"), ("read_file", '{"path":"skills/SKILL.md"}')
            ),
            *_edit_flow()[1:],
        ]
    )
    editor = replace(
        _resilient_editor(tmp_path, client), family15=family15, plan_correction=mode == "correction"
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "edited"
    outputs = [json.loads(m["content"]) for m in client.calls[2]["messages"] if m["role"] == "tool"]
    assert len(outputs) == 3
    assert outputs[1:] == [
        {"error": "not_executed", "reason": "one_tool_per_turn"},
        {"error": "not_executed", "reason": "one_tool_per_turn"},
    ]
    assert ("parallel_calls_not_run" in outputs[0]) is family15
    if family15:
        assert outputs[0]["parallel_calls_not_run"]["tools"] == ["list_files", "read_file"]


def test_nearest_paths_deterministic_ranking():
    from evolution.tree.roles.editor.chat_editor import nearest_paths

    listed = ["docs/unit_tests.md", "docs/units.md", "src/tree.py", "unit.md"]
    want = ["docs/unit_tests.md", "unit.md", "docs/units.md"]
    assert nearest_paths("unit_tree.md", listed, 3) == want
    assert nearest_paths("unit_tree.md", list(reversed(listed)), 3) == want
    assert nearest_paths("unit_tree.md", listed, 0) == []


def test_nearest_paths_only_listed_and_zero_disables_hints(tmp_path):
    from evolution.tree.roles.editor.chat_editor import missing_path_text

    root = tmp_path / "visible"
    root.mkdir()
    (root / "unit.md").write_text("readable")
    (root / "binary.md").write_bytes(b"\xff")
    (root / "linked.md").symlink_to(root / "unit.md")
    (tmp_path / "hidden.md").write_text("outside the role projection")
    assert (
        missing_path_text("unit_tree.md", root, 5)
        == "path does not exist: unit_tree.md; nearest existing paths: unit.md"
    )
    assert missing_path_text("unit_tree.md", root, 0) == "path does not exist: unit_tree.md"


@pytest.mark.parametrize("family15", [False, True])
def test_read_file_missing_text_is_family_gated(tmp_path, family15):
    from evolution.tree.roles.editor.chat_editor import ToolError, read_file_tool

    (tmp_path / "unit.md").write_text("readable")
    with pytest.raises(ToolError) as caught:
        read_file_tool(
            tmp_path,
            {"path": "unit_tree.md"},
            max_payload_bytes=10000,
            nearest_paths=5 if family15 else None,
        )
    assert str(caught.value) == (
        "path does not exist: unit_tree.md; nearest existing paths: unit.md"
        if family15
        else "cannot read file: unit_tree.md"
    )


@pytest.mark.parametrize("family15", [False, True])
async def test_editor_opening_states_own_limits_only_in_v16(tmp_path, family15):
    from dataclasses import replace

    from tests.test_tree_chat_role_retry import _resilient_editor, _ScriptedClient

    client = _ScriptedClient(_edit_flow())
    editor = replace(
        _resilient_editor(tmp_path, client, max_turns=12, max_protocol_repairs=4), family15=family15
    )
    assert (await editor.edit(_request(_ws(tmp_path)))).status == "edited"
    opening = client.calls[1]["messages"][1]["content"]
    sentence = "Session limits: at most 12 model turns in this session, the preflight included, and 4 protocol repairs."
    assert (sentence in opening) is family15
    if family15:
        assert opening.endswith(sentence)
