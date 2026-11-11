"""Golden request bytes of `/15` chat roles after a broken tool call.

b18 changes how `/16` chat role loops treat a tool call whose arguments are not
one JSON object: the call is retained in the history as `{}`. `/15` must stay as
it is. These scenarios, recorded on the plan commit before any production edit,
pin what `/15` does today: every request body the provider received, every
private `AttemptLog` record and the result, for the chat Reflector and the chat
Editor, one fixture per kind of broken call. The kinds are `dsml`,
`truncated`, `empty`, `array`, `string`, `null`, `nan` (on a tool whose other
arguments are valid), `skipped_broken` (a valid first call and a broken second
one in the same turn) and `unknown_tool_broken` (an unknown name with broken
arguments).

Today `/15` retains the broken text verbatim in the next request's history. A
strict provider (`tests/strict_history.py`) therefore refuses that request; the
class and code each role ends with are pinned below as literals so later tests
can import them.

Set `EVOLUTION_REGEN_CHAT_LOOP_GOLDENS=1` to write the fixtures (only the new
ones under `tests/fixtures/chat_loop/broken_calls/`).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree.roles.editor_contracts import outcome_class
from tests.strict_history import StrictScriptedClient, broken_history_calls
from tests.tree.roles.editor.test_chat_editor import (
    _editor,
    _log_lines,
    _plan,
    _request,
    _ws,
)
from tests.tree.roles.editor.test_chat_editor import _response as _editor_response
from tests.tree.roles.reflector.test_chat_reflector import (
    _response,
    reflect_request,  # noqa: F401 - re-used fixture
)
from tests.tree.roles.test_chat_loop_reflector_golden import (
    _CapturingClient,
    _deltas,
    _normalize,
    _reflector,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "chat_loop" / "broken_calls"
REGENERATE = os.environ.get("EVOLUTION_REGEN_CHAT_LOOP_GOLDENS") == "1"

# What each role ends with under `/15` when a strict provider refuses the history
# that retains a broken call (`test_v15_strict_provider_refuses_retained_broken_call`).
V15_STRICT_REFUSAL_CLASS_REFLECTOR = "provider_error"
V15_STRICT_REFUSAL_CODE_REFLECTOR = "provider_error"
V15_STRICT_REFUSAL_CLASS_EDITOR = "provider_error"
V15_STRICT_REFUSAL_CODE_EDITOR = "provider_error"

# kind -> (tool name, raw `arguments`) of the broken call.
BROKEN: dict[str, tuple[str, str]] = {
    "dsml": (
        "list_files",
        '<｜DSML｜function_calls><｜DSML｜invoke name="list_files">'
        "</｜DSML｜invoke></｜DSML｜function_calls>",
    ),
    "truncated": ("read_file", '{"path": "a'),
    "empty": ("list_files", ""),
    "array": ("list_files", "[]"),
    "string": ("list_files", '"x"'),
    "null": ("list_files", "null"),
    "nan": ("read_file", '{"path": "skills/SKILL.md", "start_line": NaN}'),
    "unknown_tool_broken": ("no_such_tool", '{"x"'),
}
KINDS = [*BROKEN, "skipped_broken"]


def _raw(*calls: tuple[str, str]) -> Any:
    tool_calls = [
        SimpleNamespace(
            id="call" if len(calls) == 1 else f"call-{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (name, arguments) in enumerate(calls)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))]
    )


def _broken_turn(kind: str, first: str) -> Any:
    """The scripted turn holding the broken call; `first` is a valid tool of the role."""

    if kind == "skipped_broken":
        return _raw((first, "{}"), ("read_file", '{"path": "a'))
    return _raw(BROKEN[kind])


def _reflector_script(kind: str) -> list[Any]:
    return [
        _response("list_files"),
        _broken_turn(kind, "list_evidence"),
        _response("finish_reflection", {"groups": [], "declined": True}),
    ]


def _editor_script(kind: str) -> list[Any]:
    return [
        _editor_response("list_files"),
        _broken_turn(kind, "list_files"),
        _plan("skills/SKILL.md"),
        _editor_response(
            "write_file", json.dumps({"path": "skills/SKILL.md", "content": "second\n"})
        ),
        _editor_response("finish_edit", '{"summary":"done"}'),
    ]


def _check(name: str, transcript: dict[str, Any]) -> None:
    fixture = FIXTURES / f"{name}.json"
    text = json.dumps(transcript, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if REGENERATE:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(text, encoding="utf-8")
    assert json.loads(fixture.read_text(encoding="utf-8")) == transcript


def _outcome(result: Any) -> dict[str, Any]:
    return json.loads(json.dumps(dataclasses.asdict(result), default=repr))


async def _run_reflector(
    tmp_path: Path, request: Any, kind: str, client: Any, inner: _CapturingClient
) -> tuple[Any, dict[str, Any]]:
    reflector = _reflector(tmp_path, client)
    result = await reflector.reflect(request)
    path = Path(reflector.log_root) / f"{request.reflection_id}.jsonl"
    log = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    transcript = _normalize(
        {"requests": _deltas(inner.bodies), "log": log, "result": _outcome(result)}, tmp_path
    )
    return result, transcript


async def _run_editor(
    tmp_path: Path, client: Any, inner: _CapturingClient
) -> tuple[Any, dict[str, Any]]:
    workspace = _ws(tmp_path)
    editor, _ = _editor(tmp_path, client=client)
    result = await editor.edit(_request(workspace))
    transcript = _normalize(
        {
            "requests": _deltas(inner.bodies),
            "log": _log_lines(editor),
            "result": _outcome(result),
            "published": (workspace / "skills" / "SKILL.md").read_text(encoding="utf-8"),
        },
        tmp_path,
    )
    return result, transcript


@pytest.mark.parametrize("kind", KINDS)
async def test_reflector_v15_broken_call(
    tmp_path: Path,
    kind: str,
    reflect_request: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    inner = _CapturingClient(_reflector_script(kind))
    _result, transcript = await _run_reflector(tmp_path, reflect_request, kind, inner, inner)
    assert not inner.script, "every scripted item must be consumed"
    _check(f"reflector_{kind}", transcript)


@pytest.mark.parametrize("kind", KINDS)
async def test_editor_v15_broken_call(tmp_path: Path, kind: str) -> None:
    inner = _CapturingClient(_editor_script(kind))
    _result, transcript = await _run_editor(tmp_path, inner, inner)
    assert not inner.script, "every scripted item must be consumed"
    _check(f"editor_{kind}", transcript)


# --------------------------------------------------------------------------
# A strict provider refuses what `/15` retains
# --------------------------------------------------------------------------

STRICT_KINDS = ["truncated", "empty", "array", "nan"]


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", instant)


@pytest.mark.parametrize("kind", STRICT_KINDS)
@pytest.mark.parametrize("role", ["reflector", "editor"])
async def test_v15_strict_provider_refuses_retained_broken_call(
    tmp_path: Path,
    role: str,
    kind: str,
    no_sleep: None,
    reflect_request: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    """The request after a broken call carries it verbatim; a strict provider refuses it."""

    if role == "reflector":
        inner = _CapturingClient(_reflector_script(kind))
        strict = StrictScriptedClient(inner, "http_400")
        result, _ = await _run_reflector(tmp_path, reflect_request, kind, strict, inner)
        ended = (outcome_class(result), result.code)
        pinned = (V15_STRICT_REFUSAL_CLASS_REFLECTOR, V15_STRICT_REFUSAL_CODE_REFLECTOR)
        assert result.status == "failed"
    else:
        inner = _CapturingClient(_editor_script(kind))
        strict = StrictScriptedClient(inner, "http_400")
        result, _ = await _run_editor(tmp_path, strict, inner)
        ended = (outcome_class(result), result.code)
        pinned = (V15_STRICT_REFUSAL_CLASS_EDITOR, V15_STRICT_REFUSAL_CODE_EDITOR)
    assert strict.refusals, "the history after a broken call must reach the provider"
    assert ended == pinned


def test_the_unscripted_kinds_are_broken_for_the_strict_parser() -> None:
    for kind, (_name, arguments) in BROKEN.items():
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "c", "function": {"name": "t", "arguments": arguments}}],
                }
            ]
        }
        # unknown_tool_broken and every other kind carry arguments that are not one object
        assert broken_history_calls(body) == ["c"], kind
