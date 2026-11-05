"""The shared agent call keeps role policy outside its backend boundary."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.tree.roles.agent_role import (
    agent_log_secrets,
    call_agent,
    verify_agent_seals,
)
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.seals import seal_program, seal_prompt
from evolution.tree.roles.workspace import open_attempt_log


def _seals(tmp_path: Path):
    program = tmp_path / "bin" / "codex"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    return seal_program(program), seal_prompt(None, packaged="system")


async def test_neutral_call_uses_fresh_backend_and_preserves_log_shape(tmp_path, monkeypatch):
    import time

    program, prompt = _seals(tmp_path)
    section = AgentCliEditorSection(model="codex", permission="unsafe", reasoning_effort="high")
    instances = []

    class Backend:
        last_usage = {"tool_turns": 2}
        last_codex_diagnostics = {"last_message": "done"}

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            instances.append(self)

        async def complete_file_edit(self, root, opening, system, *, timeout_s):
            assert (root, opening, system, timeout_s) == (tmp_path, "opening", "system", 25.0)
            return SimpleNamespace(content="answer")

    monkeypatch.setattr(time, "monotonic", lambda: 105.0)
    with open_attempt_log(
        tmp_path / "logs", "call", secrets=agent_log_secrets(("secret",), program)
    ) as log:
        for call in (1, 2):
            assert (
                await call_agent(
                    section=section,
                    program=program,
                    prompt=prompt,
                    child_env={"TOKEN": "secret"},
                    root=tmp_path,
                    opening="opening",
                    deadline_seconds=30,
                    started=100,
                    log=log,
                    call=call,
                    backend_factory=Backend,
                )
                == 2
            )
    assert len(instances) == 2 and instances[0] is not instances[1]
    for backend in instances:
        assert backend.kwargs == {
            "model": "codex",
            "agent_permission": "unsafe",
            "reasoning_effort": "high",
            "timeout_s": 25.0,
            "sandbox": None,
            "agent_bin": str(program.path),
            "env": {"TOKEN": "secret"},
            "explicit": True,
        }
    lines = [
        json.loads(line) for line in (tmp_path / "logs" / "call.jsonl").read_text().splitlines()
    ]
    assert [line["phase"] for line in lines] == ["request", "response", "request", "response"]
    assert lines[0] == {
        "phase": "request",
        "call": 1,
        "model": "codex",
        "system_sha256": prompt.sha256,
        "opening": "opening",
        "timeout_s": 25.0,
    }
    assert lines[1] == {
        "phase": "response",
        "call": 1,
        "content": "answer",
        "usage": {"tool_turns": 2},
        "diagnostics": {"last_message": "done"},
        "tool_turns": 2,
    }


@pytest.mark.parametrize("error", [RuntimeError("reader broke"), asyncio.CancelledError()])
async def test_neutral_call_propagates_faults_without_legacy_translation(tmp_path, error):
    program, prompt = _seals(tmp_path)

    class Backend:
        async def complete_file_edit(self, *args, **kwargs):
            raise error

    with open_attempt_log(tmp_path / "logs", "call", secrets=()) as log:
        with pytest.raises(type(error)):
            await call_agent(
                section=AgentCliEditorSection(model="codex"),
                program=program,
                prompt=prompt,
                child_env={},
                root=tmp_path,
                opening="opening",
                deadline_seconds=30,
                started=time.monotonic(),
                log=log,
                call=1,
                backend_factory=lambda **kwargs: Backend(),
            )
    lines = [
        json.loads(line) for line in (tmp_path / "logs" / "call.jsonl").read_text().splitlines()
    ]
    assert [line["phase"] for line in lines] == ["request"]


def test_seals_and_log_secrets_keep_prompt_first_and_program_path(tmp_path):
    program, prompt = _seals(tmp_path)
    assert verify_agent_seals(prompt, program) is None
    assert agent_log_secrets(("secret",), program) == ("secret", str(program.path))
    program.path.write_text("changed")
    assert verify_agent_seals(prompt, program) == "command_changed"
