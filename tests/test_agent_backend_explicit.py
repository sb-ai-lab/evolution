"""Explicit-knob / explicit-env contract for :class:`AgentBackend` (Task 6).

Task 7 (the agent editor) must be able to construct ``AgentBackend`` with
every knob explicit and no fallback to the environment, and pass the exact
child environment the campaign bound. These tests pin that contract without
touching the existing (environment-fallback) solver path, which stays
covered by ``tests/test_agent_backend_unit.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from evolution.llm.agent_backend import (
    _FILE_EDIT_BYPASS_FLAG,
    CURATED_ENV_NAMES,
    AgentBackend,
    CliAgent,
    HermesCli,
    agent_tool_for,
    reports_tool_turns,
)


def test_explicit_backend_ignores_environment_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("EVO_CODEX_REASONING_EFFORT", "low")
    monkeypatch.setenv("EVO_CODEX_TIMEOUT_S", "1")
    monkeypatch.setenv("EVO_LLM_TIMEOUT_S", "2")
    monkeypatch.setenv("EVO_CODEX_SANDBOX", "danger-full-access")
    monkeypatch.setenv("CODEX_BIN", "/nonexistent")
    monkeypatch.setattr(
        "evolution.llm.agent_backend._resolve_agent_bin",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("_resolve_agent_bin must not be consulted when explicit=True")
        ),
    )

    backend = AgentBackend(
        "codex",
        explicit=True,
        agent_permission="unsafe",
        reasoning_effort=None,
        timeout_s=None,
        sandbox=None,
        agent_bin="/usr/bin/true",
        env={},
    )

    # None means the constructor's own constant default, never os.environ.
    assert backend._reasoning_effort == "xhigh"
    assert backend._timeout_s == 1800.0
    assert backend._sandbox == "read-only"
    assert backend._agent_bin == "/usr/bin/true"

    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    # _resolve_agent_bin would have raised (patched above) had any call path
    # consulted it; the given absolute path is used verbatim instead.
    assert captured["argv"][0] == "/usr/bin/true"


def test_default_backend_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setenv("EVO_CODEX_REASONING_EFFORT", "medium")

    backend = AgentBackend("codex/gpt-5.5")

    assert backend._reasoning_effort == "medium"
    assert backend._env is None
    assert backend._explicit is False


@pytest.mark.parametrize("model", ["codex/gpt-5.5", "claude-code"])
async def test_explicit_env_is_spawned_verbatim(monkeypatch, tmp_path, model):
    recorded: dict[str, dict[str, str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        recorded["env"] = env
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend(
        model,
        explicit=True,
        agent_bin="/usr/bin/true",
        env={"A": "1"},
    )

    await backend.complete_file_edit(tmp_path, "edit it")

    assert recorded["env"] == {"A": "1"}


async def test_explicit_env_is_spawned_verbatim_for_safe_hermes(monkeypatch, tmp_path):
    recorded: dict[str, dict[str, str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        recorded["env"] = env
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend(
        "hermes",
        explicit=True,
        agent_permission="safe",
        agent_bin="/usr/bin/true",
        env={"A": "1"},
    )

    await backend.complete_file_edit(tmp_path, "edit it")

    env = recorded["env"]
    assert env["A"] == "1"
    assert set(env) == {"A", "HERMES_HOME"}
    assert env["HERMES_HOME"]


def test_explicit_codex_file_edit_sandbox_never_comes_from_environment(monkeypatch, tmp_path):
    """Reviewer's round-1 probe (Finding 1, Critical).

    ``EVO_CODEX_FILE_EDIT_SANDBOX=danger-full-access`` must not leak into an
    explicit unsafe Codex file-edit/session run when ``sandbox=None`` — the
    module constant ``_FILE_EDIT_BYPASS`` wins instead.
    """
    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "danger-full-access")
    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend(
        "codex",
        explicit=True,
        agent_permission="unsafe",
        sandbox=None,
        agent_bin="/usr/bin/true",
        env={},
    )

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    argv = captured["argv"]
    assert _FILE_EDIT_BYPASS_FLAG in argv
    assert "danger-full-access" not in argv


def test_explicit_codex_file_edit_sandbox_uses_given_value(monkeypatch, tmp_path):
    """The explicitly passed ``sandbox`` (not the env) drives the file-edit mode."""
    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "danger-full-access")
    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend(
        "codex",
        explicit=True,
        agent_permission="unsafe",
        sandbox="workspace-write",
        agent_bin="/usr/bin/true",
        env={},
    )

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    argv = captured["argv"]
    assert ["-s", "workspace-write"] == argv[argv.index("-s") : argv.index("-s") + 2]
    assert _FILE_EDIT_BYPASS_FLAG not in argv


def test_default_codex_file_edit_sandbox_still_reads_environment(monkeypatch, tmp_path):
    """Mirror of the two tests above: without ``explicit``, the env still wins."""
    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "danger-full-access")
    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend("codex", agent_permission="unsafe", agent_bin="/usr/bin/true")

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    argv = captured["argv"]
    assert ["-s", "danger-full-access"] == argv[argv.index("-s") : argv.index("-s") + 2]


def test_explicit_hermes_never_reads_provider_from_environment(monkeypatch, tmp_path):
    """Reviewer's round-1 probe (Finding 2, Important)."""
    monkeypatch.setenv("HERMES_PROVIDER", "sneaky")
    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend(
        "hermes/my-model",
        explicit=True,
        agent_permission="unsafe",
        agent_bin="/usr/bin/true",
        env={},
    )

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    assert "--provider" not in captured["argv"]


def test_default_hermes_still_reads_provider_from_environment(monkeypatch, tmp_path):
    """Mirror: without ``explicit``, ``HERMES_PROVIDER`` still applies."""
    monkeypatch.setenv("HERMES_PROVIDER", "sneaky")
    captured: dict[str, list[str]] = {}

    async def fake_run_process(self, inv, argv, env, timeout):
        captured["argv"] = argv
        return False, 0, "", "", 1

    monkeypatch.setattr(AgentBackend, "_run_process", fake_run_process)

    backend = AgentBackend("hermes/my-model", agent_permission="unsafe", agent_bin="/usr/bin/true")

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    argv = captured["argv"]
    assert "--provider" in argv
    assert argv[argv.index("--provider") + 1] == "sneaky"


def test_curated_env_names_and_turn_telemetry():
    assert "OPENAI_API_KEY" in CURATED_ENV_NAMES("codex")
    assert "ANTHROPIC_API_KEY" in CURATED_ENV_NAMES("claude")
    assert "HOME" in CURATED_ENV_NAMES("codex")
    assert "OPENAI_API_KEY" not in CURATED_ENV_NAMES("unknown-tool")

    assert agent_tool_for("codex/gpt-5.5") == "codex"
    assert agent_tool_for("claude-code") == "claude"
    assert agent_tool_for("hermes") == "hermes"
    assert agent_tool_for("some-bare-model") == "codex"

    assert reports_tool_turns("codex") is True
    assert reports_tool_turns("claude-code") is True
    assert reports_tool_turns("hermes") is False
    assert type(HermesCli()).tool_turns is CliAgent.tool_turns
