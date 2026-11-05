"""Shared sealed CLI call for tree roles; role policy stays with each caller."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evolution.llm.agent_backend import AgentBackend
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.seals import FileSeal, SealChanged
from evolution.tree.roles.workspace import AttemptLog


class AgentPromptChanged(SealChanged):
    """The prompt changed during the call's verify/read boundary."""


def verify_agent_seals(prompt: FileSeal, program: FileSeal) -> str | None:
    """Return the first changed seal's existing role refusal code."""

    try:
        prompt.verify()
    except SealChanged:
        return "prompt_changed"
    try:
        program.verify()
    except SealChanged:
        return "command_changed"
    return None


def agent_log_secrets(secrets: Sequence[str], program: FileSeal) -> tuple[str, ...]:
    """Redact credentials and the sealed CLI path from private transcripts."""

    return (*secrets, str(program.path))


def build_agent_backend(
    section: AgentCliEditorSection,
    program: FileSeal,
    child_env: Mapping[str, str],
    remaining: float,
    *,
    backend_factory: Callable[..., Any] | None = None,
) -> Any:
    """Create a backend per call so its usage and diagnostics belong to that call."""

    build = backend_factory if backend_factory is not None else AgentBackend
    return build(
        model=section.model,
        agent_permission=section.permission,
        reasoning_effort=section.reasoning_effort,
        timeout_s=remaining,
        sandbox=None,
        agent_bin=str(program.path),
        env=dict(child_env),
        explicit=True,
    )


async def call_agent(
    *,
    section: AgentCliEditorSection,
    program: FileSeal,
    prompt: FileSeal,
    child_env: Mapping[str, str],
    root: Path,
    opening: str,
    deadline_seconds: float,
    started: float,
    log: AttemptLog,
    call: int,
    backend_factory: Callable[..., Any] | None = None,
    on_backend_error: Callable[[Exception, Any, AttemptLog], None] | None = None,
) -> int:
    """Call one CLI, retaining request/response telemetry and propagating faults.

    A legacy role may supply an error callback to translate completion faults.
    Backend construction and prompt/seal reads stay outside that callback.
    """

    remaining = max(0.0, deadline_seconds - (time.monotonic() - started))
    try:
        prompt.verify()
        system = prompt.text()
    except SealChanged as exc:
        raise AgentPromptChanged(str(exc)) from exc
    log.record(
        {
            "phase": "request",
            "call": call,
            "model": section.model,
            "system_sha256": prompt.sha256,
            "opening": opening,
            "timeout_s": remaining,
        }
    )
    backend = build_agent_backend(
        section, program, child_env, remaining, backend_factory=backend_factory
    )
    try:
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(remaining):
            response = await backend.complete_file_edit(root, opening, system, timeout_s=remaining)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if on_backend_error is not None:
            on_backend_error(exc, backend, log)
        raise

    usage = dict(getattr(backend, "last_usage", None) or {})
    diagnostics = dict(getattr(backend, "last_codex_diagnostics", None) or {})
    reported = usage.get("tool_turns")
    turns = reported if isinstance(reported, int) and reported > 0 else 1
    log.record(
        {
            "phase": "response",
            "call": call,
            "content": str(getattr(response, "content", "") or ""),
            "usage": usage,
            "diagnostics": diagnostics,
            "tool_turns": turns,
        }
    )
    return turns
