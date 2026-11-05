"""Unit tests for the coding-agent CLI backend that avoid live agent calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from evolution.llm.agent_backend import (
    AgentBackend,
    AgentInvocation,
    ClaudeCli,
    CodexCli,
    HermesCli,
    _agent_env,
    _format_agent_prompt,
    _normalise_model,
    _parse_claude_result,
    _parse_claude_usage,
    _parse_tokens_used,
    _safe_hermes_argv,
    _safe_hermes_env,
    _select_cli,
)


def test_normalise_codex_model_names():
    assert _normalise_model("codex") == "gpt-5.5"
    assert _normalise_model("codex/gpt-5.5") == "gpt-5.5"
    assert _normalise_model("gpt-5.5") == "gpt-5.5"


def test_format_agent_prompt_keeps_content_without_role_headers():
    prompt = _format_agent_prompt(
        [
            {"role": "user", "content": "Do the task."},
            {"role": "assistant", "content": "Previous answer."},
        ],
        system="Follow instructions.",
    )

    assert prompt == "Follow instructions.\n\nDo the task.\n\nPrevious answer."
    assert "# SYSTEM INSTRUCTIONS" not in prompt
    assert "# USER MESSAGE" not in prompt
    assert "# ASSISTANT MESSAGE" not in prompt


def test_parse_tokens_used_best_effort():
    assert _parse_tokens_used("tokens used: 12,345") == 12345
    assert _parse_tokens_used("total tokens = 987") == 987
    assert _parse_tokens_used("no usage here") == 0


def test_agent_backend_accepts_litellm_style_unused_kwargs(monkeypatch):
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")
    backend = AgentBackend(model="codex/gpt-5.5", api_base="codex-cli", api_key="EMPTY")
    assert backend.model_label == "codex/gpt-5.5"
    assert backend._reasoning_effort == "high"


def test_agent_backend_rejects_seed_before_cli_lookup(monkeypatch):
    backend = AgentBackend(model="codex/gpt-5.5")
    monkeypatch.setattr(
        "evolution.llm.agent_backend._resolve_agent_bin",
        lambda *_args: (_ for _ in ()).throw(AssertionError("CLI lookup started")),
    )

    with pytest.raises(ValueError, match="seed"):
        asyncio.run(backend.complete([{"role": "user", "content": "hi"}], seed=7))


def test_select_cli_routing():
    cli, model = _select_cli("codex")
    assert isinstance(cli, CodexCli) and model == "gpt-5.5"

    cli, model = _select_cli("codex/gpt-5.5")
    assert isinstance(cli, CodexCli) and model == "gpt-5.5"

    cli, model = _select_cli("gpt-5.5")  # bare name -> codex (back-compat)
    assert isinstance(cli, CodexCli) and model == "gpt-5.5"

    cli, model = _select_cli("claude-code")
    assert isinstance(cli, ClaudeCli) and model == "claude-sonnet-4-6"

    cli, model = _select_cli("claude-code/claude-opus-4-7")
    assert isinstance(cli, ClaudeCli) and model == "claude-opus-4-7"

    cli, model = _select_cli("hermes")
    assert isinstance(cli, HermesCli) and model == ""

    cli, model = _select_cli("hermes/my-model")
    assert isinstance(cli, HermesCli) and model == "my-model"


def _codex_argv(work, out_file, permission_args):
    return [
        "/usr/bin/codex",
        "exec",
        "-m",
        "gpt-5.5",
        "-c",
        "model_reasoning_effort=high",
        *permission_args,
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "-C",
        str(work),
        "-o",
        str(out_file),
        "-",
    ]


def _claude_argv(tools, unsafe, budget=None):
    argv = [
        "/usr/bin/claude",
        "-p",
        "--model",
        "claude-sonnet-4-6",
        "--effort",
        "high",
        "--no-session-persistence",
    ]
    if unsafe:
        argv.append("--dangerously-skip-permissions")
    argv.extend(["--output-format", "json", "--tools", tools])
    if budget is not None:
        argv.extend(["--max-budget-usd", str(budget)])
    return argv


def test_agent_backend_model_labels():
    assert AgentBackend(model="claude-code").model_label == "claude-code/claude-sonnet-4-6"
    assert AgentBackend(model="hermes").model_label == "hermes/default"
    assert AgentBackend(model="codex").model_label == "codex/gpt-5.5"


def test_agent_backend_threads_unsafe_permission_to_file_edit(tmp_path, monkeypatch):
    captured: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.pid = 1
            self.stdin = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return self.returncode

    async def fake_exec(*argv, **_kwargs):
        captured.extend(argv)
        return FakeProcess()

    monkeypatch.setattr("evolution.llm.agent_backend.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.delenv("EVO_CODEX_FILE_EDIT_SANDBOX", raising=False)
    backend = AgentBackend(model="codex/gpt-5.5", agent_permission="unsafe", agent_bin="/bin/true")

    asyncio.run(backend.complete_file_edit(tmp_path, "edit it"))

    assert "--dangerously-bypass-approvals-and-sandbox" in captured
    assert "-s" not in captured


@pytest.mark.parametrize(
    ("agent_permission", "requested", "expected"),
    [
        ("safe", "danger-full-access", "read-only"),
        ("unsafe", "danger-full-access", "danger-full-access"),
    ],
)
def test_codex_text_invocation_enforces_safe_sandbox(
    tmp_path, agent_permission, requested, expected
):
    inv = CodexCli().text_invocation(
        agent_bin="/usr/bin/codex",
        model="gpt-5.5",
        reasoning_effort="high",
        agent_permission=agent_permission,
        sandbox=requested,
        prompt="hello",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )

    assert inv.argv == _codex_argv(tmp_path, tmp_path / "out.txt", ["-s", expected])


def test_safe_backend_ignores_constructor_and_environment_sandbox(monkeypatch):
    monkeypatch.setenv("EVO_CODEX_SANDBOX", "danger-full-access")

    backend = AgentBackend(
        model="codex/gpt-5.5",
        agent_permission="safe",
        sandbox="danger-full-access",
    )

    assert backend._sandbox == "read-only"


def test_safe_agent_env_is_cli_specific_and_excludes_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "codex-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-key")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("UNRELATED_SENTINEL_SECRET", "must-not-leak")

    env = _agent_env("codex", "safe")

    assert env["OPENAI_API_KEY"] == "codex-key"
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert "PATH" in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "UNRELATED_SENTINEL_SECRET" not in env


def test_unsafe_agent_env_inherits_host_environment(monkeypatch):
    monkeypatch.setenv("UNRELATED_SENTINEL_SECRET", "allowed-by-unsafe")

    assert _agent_env("codex", "unsafe")["UNRELATED_SENTINEL_SECRET"] == "allowed-by-unsafe"


@pytest.mark.parametrize("mode", ["file_edit_invocation", "session_invocation"])
@pytest.mark.parametrize(
    ("agent_permission", "permission_args", "sandbox"),
    [
        ("safe", ["-s", "workspace-write"], "workspace-write"),
        ("unsafe", ["--dangerously-bypass-approvals-and-sandbox"], "bypass"),
    ],
)
def test_codex_edit_modes_respect_permission(
    tmp_path, monkeypatch, mode, agent_permission, permission_args, sandbox
):
    monkeypatch.delenv("EVO_CODEX_FILE_EDIT_SANDBOX", raising=False)
    kwargs = {
        "agent_bin": "/usr/bin/codex",
        "model": "gpt-5.5",
        "reasoning_effort": "high",
        "agent_permission": agent_permission,
        "prompt": "edit it",
        "work_dir": tmp_path,
        "out_file": tmp_path / "meta.txt",
    }
    if mode == "session_invocation":
        kwargs["budget_usd"] = 1.0

    inv = getattr(CodexCli(), mode)(**kwargs)

    assert inv.argv == _codex_argv(tmp_path, tmp_path / "meta.txt", permission_args)
    assert inv.diagnostics_seed == {
        "sandbox": sandbox,
        "bypass_approvals_and_sandbox": agent_permission == "unsafe",
    }


@pytest.mark.parametrize("agent_permission", ["safe", "unsafe"])
def test_claude_text_invocation_respects_permission(tmp_path, agent_permission):
    inv = ClaudeCli().text_invocation(
        agent_bin="/usr/bin/claude",
        model="claude-sonnet-4-6",
        reasoning_effort="high",
        agent_permission=agent_permission,
        sandbox="read-only",
        prompt="hello",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )

    assert inv.argv == _claude_argv("", agent_permission == "unsafe")


@pytest.mark.parametrize(
    ("mode", "tools", "budget"),
    [
        ("file_edit_invocation", "Read,Edit,Write,Glob", None),
        ("session_invocation", "Read,Edit,Write,Glob,Bash", 2.5),
    ],
)
@pytest.mark.parametrize("agent_permission", ["safe", "unsafe"])
def test_claude_edit_modes_respect_permission(tmp_path, mode, tools, budget, agent_permission):
    kwargs = {
        "agent_bin": "/usr/bin/claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "agent_permission": agent_permission,
        "prompt": "edit it",
        "work_dir": tmp_path,
        "out_file": tmp_path / "meta.txt",
    }
    if budget is not None:
        kwargs["budget_usd"] = budget

    inv = getattr(ClaudeCli(), mode)(**kwargs)

    assert inv.argv == _claude_argv(tools, agent_permission == "unsafe", budget)
    assert inv.diagnostics_seed["sandbox"] == (
        "claude-skip-permissions" if agent_permission == "unsafe" else "claude-permissions"
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("my-model", ["/usr/bin/hermes", "-z", "hi", "-m", "my-model"]),
        ("", ["/usr/bin/hermes", "-z", "hi"]),
    ],
)
def test_hermes_text_unsafe_invocation_is_exact(tmp_path, model, expected):
    inv = HermesCli().text_invocation(
        agent_bin="/usr/bin/hermes",
        model=model,
        reasoning_effort="high",
        agent_permission="unsafe",
        sandbox="read-only",
        prompt="hi",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )

    assert inv.argv == expected


def test_hermes_file_edit_pins_file_tools_in_unsafe_mode(tmp_path):
    kwargs = {
        "agent_bin": "/usr/bin/hermes",
        "model": "my-model",
        "reasoning_effort": "high",
        "agent_permission": "unsafe",
        "prompt": "edit it",
        "work_dir": tmp_path,
        "out_file": tmp_path / "out.txt",
    }

    inv = HermesCli().file_edit_invocation(**kwargs)

    assert inv.argv == [
        "/usr/bin/hermes",
        "-z",
        "edit it",
        "-m",
        "my-model",
        "-t",
        "file",
    ]


def test_hermes_unsafe_session_preserves_full_tool_access(tmp_path):
    invocation = HermesCli().session_invocation(
        agent_bin="/usr/bin/hermes",
        model="my-model",
        reasoning_effort="high",
        agent_permission="unsafe",
        prompt="work on it",
        work_dir=tmp_path,
        out_file=tmp_path / "out.txt",
        budget_usd=1.0,
    )

    assert invocation.argv == [
        "/usr/bin/hermes",
        "-z",
        "work on it",
        "-m",
        "my-model",
    ]


def test_safe_hermes_session_uses_only_file_tools(tmp_path):
    invocation = HermesCli().session_invocation(
        agent_bin="/usr/bin/hermes",
        model="my-model",
        reasoning_effort="high",
        agent_permission="safe",
        prompt="work on it",
        work_dir=tmp_path,
        out_file=tmp_path / "out.txt",
        budget_usd=1.0,
    )

    assert invocation.argv[-2:] == ["-t", "file"]


def test_safe_hermes_text_uses_file_tools(tmp_path):
    invocation = HermesCli().text_invocation(
        agent_bin="/usr/bin/hermes",
        model="my-model",
        reasoning_effort="high",
        agent_permission="safe",
        sandbox="read-only",
        prompt="hi",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )

    assert invocation.argv[-2:] == ["-t", "file"]


def test_safe_hermes_env_isolated_and_sanitized(tmp_path, monkeypatch):
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "custom_providers:\n"
        "- name: local\n"
        "  base_url: https://example.invalid/v1\n"
        "  authorization: Bearer embedded-secret\n"
        "  api_key: embedded-key\n"
        "  key_env: HERMES_TEST_KEY\n"
        "- name: hostile\n"
        "  base_url: https://example.invalid/v1\n"
        "  key_env: PYTHONUSERBASE\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("HERMES_TEST_KEY", "allowed-key")
    monkeypatch.setenv("UNRELATED_SENTINEL_SECRET", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "injected-python"))
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "injected-user-base"))
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path / "injected-libraries"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "injected-venv"))
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path / "injected-conda"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_state = tmp_path / "run-state"
    run_state.mkdir()

    env = _safe_hermes_env(run_state, workspace)
    config = (Path(env["HERMES_HOME"]) / "config.yaml").read_text(encoding="utf-8")

    assert env["HERMES_TEST_KEY"] == "allowed-key"
    assert "UNRELATED_SENTINEL_SECRET" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "PYTHONPATH" not in env
    assert "PYTHONUSERBASE" not in env
    assert "LD_LIBRARY_PATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert "CONDA_PREFIX" not in env
    assert Path(env["HOME"]).is_relative_to(run_state)
    assert "embedded-secret" not in config
    assert "embedded-key" not in config


def test_safe_hermes_config_keeps_provider_routing_without_inline_credentials(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "provider_routing:\n"
        "  default:\n"
        "    provider: local\n"
        "    api_key: embedded-routing-key\n"
        "    credentials: embedded-routing-credentials\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_state = tmp_path / "run-state"
    run_state.mkdir()

    env = _safe_hermes_env(run_state, workspace)
    config = (Path(env["HERMES_HOME"]) / "config.yaml").read_text(encoding="utf-8")

    assert "provider_routing:\n" in config
    assert "provider: local" in config
    assert "embedded-routing-key" not in config
    assert "embedded-routing-credentials" not in config


def test_safe_hermes_allows_editable_checkout_runtime(tmp_path):
    checkout = tmp_path / "hermes-agent"
    executable = checkout / ".venv" / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (checkout / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (checkout / "run_agent.py").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    argv = _safe_hermes_argv([str(executable), "-z", "hi"], workspace, state)
    read_roots = {Path(argv[index + 1]) for index, value in enumerate(argv) if value == "--read"}

    assert checkout in read_roots
    assert argv[1] == "-I"


def test_safe_hermes_confines_fake_executable_and_cleans_state(tmp_path, monkeypatch):
    checkout = tmp_path / "fake-hermes"
    executable = checkout / ".venv" / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    (checkout / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    executable.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$HERMES_HOME" > "$PWD/run_state"\n'
        'printf "%s" "${UNRELATED_SENTINEL_SECRET-}" > "$PWD/leaked_secret"\n'
        'printf "%s\\n" "$@" > "$PWD/argv"\n'
        f"if printf escaped > {str(outside)!r}; then result=escaped; else result=blocked; fi\n"
        'printf "%s" "$result" > "$PWD/path_guard"\n'
        'printf "done\\n"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing-config"))
    monkeypatch.setenv("UNRELATED_SENTINEL_SECRET", "must-not-leak")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = AgentBackend(model="hermes/test", agent_bin=str(executable))

    asyncio.run(backend.complete_file_edit(workspace, "edit it"))

    run_state = Path((workspace / "run_state").read_text(encoding="utf-8")).parent
    assert (workspace / "argv").read_text(encoding="utf-8").splitlines()[-2:] == ["-t", "file"]
    assert (workspace / "leaked_secret").read_text(encoding="utf-8") == ""
    assert (workspace / "path_guard").read_text(encoding="utf-8") == "blocked"
    assert not outside.exists()
    assert not run_state.exists()


def test_safe_hermes_fails_closed_when_confinement_cannot_start(tmp_path, monkeypatch, capsys):
    from evolution.llm import hermes_sandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(
        hermes_sandbox,
        "restrict",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        hermes_sandbox.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("agent started")),
    )

    returncode = hermes_sandbox.main(
        ["--workspace", str(workspace), "--state", str(state), "--", "/bin/true"]
    )

    assert returncode == 125
    assert "unavailable" in capsys.readouterr().err


def test_hermes_rejects_oversized_prompt(tmp_path):
    from evolution.llm.agent_backend import _HERMES_ARGV_PROMPT_LIMIT, AgentBackendError

    with pytest.raises(AgentBackendError, match="too large for argv"):
        HermesCli().text_invocation(
            agent_bin="/usr/bin/hermes",
            model="m",
            reasoning_effort="high",
            agent_permission="unsafe",
            sandbox="read-only",
            prompt="x" * (_HERMES_ARGV_PROMPT_LIMIT + 1),
            work=tmp_path,
            out_file=tmp_path / "out.txt",
        )


def test_parse_claude_result_reads_result_field():
    assert _parse_claude_result('{"is_error": false, "result": "the answer"}') == "the answer"
    assert _parse_claude_result('{"is_error": true, "result": "oops"}') == ""
    assert _parse_claude_result("not json") == ""
    assert _parse_claude_result("") == ""


def test_parse_claude_usage_reads_tokens():
    payload = '{"result": "x", "usage": {"input_tokens": 11, "output_tokens": 2}}'
    assert _parse_claude_usage(payload) == (11, 2)
    assert _parse_claude_usage('{"result": "x"}') == (None, None)
    assert _parse_claude_usage("not json") == (None, None)


def test_claude_session_records_required_write_even_on_nonzero_return(tmp_path, monkeypatch):
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("before", encoding="utf-8")
    backend = AgentBackend(model="claude-code", agent_bin="/bin/true")

    async def fake_run(invocation, timeout):
        skill_path.write_text("after", encoding="utf-8")
        return False, 7, '{"is_error":true,"result":""}', "failed after write", 12

    monkeypatch.setattr(backend, "_run", fake_run)

    asyncio.run(
        backend.complete_agent_session(
            tmp_path,
            "edit it",
            required_write=skill_path,
        )
    )

    diagnostics = backend.last_codex_diagnostics
    assert diagnostics["returncode"] == 7
    assert diagnostics["file_changes"] == [
        {
            "path": str(skill_path),
            "kind": "update",
            "source": "backend_before_after",
            "before_sha256": hashlib.sha256(b"before").hexdigest(),
            "after_sha256": hashlib.sha256(b"after").hexdigest(),
        }
    ]


def test_run_bounds_verbose_output_and_keeps_tails(monkeypatch):
    monkeypatch.setattr("evolution.llm.agent_backend._MAX_CAPTURE_BYTES", 512)
    backend = AgentBackend(model="codex", agent_bin=sys.executable)
    invocation = AgentInvocation(
        argv=[
            sys.executable,
            "-c",
            "import os; os.write(1, b'A' * 4096 + b'OUT_TAIL'); "
            "os.write(2, b'B' * 4096 + b'ERR_TAIL')",
        ],
        stdin=None,
        cwd=None,
        out_file=None,
    )

    timed_out, returncode, stdout, stderr, _ = asyncio.run(backend._run(invocation, 5))

    assert not timed_out
    assert returncode == 0
    assert len(stdout.encode()) <= 512
    assert len(stderr.encode()) <= 512
    assert "output truncated" in stdout
    assert stdout.endswith("OUT_TAIL")
    assert stderr.endswith("ERR_TAIL")


def test_run_preserves_normal_json_reply():
    payload = json.dumps({"is_error": False, "result": "the answer"})
    backend = AgentBackend(model="claude-code", agent_bin=sys.executable)
    invocation = AgentInvocation(
        argv=[sys.executable, "-c", f"print({payload!r})"],
        stdin=None,
        cwd=None,
        out_file=None,
    )

    timed_out, returncode, stdout, _, _ = asyncio.run(backend._run(invocation, 5))

    assert not timed_out
    assert returncode == 0
    assert _parse_claude_result(stdout) == "the answer"


def test_timeout_kills_and_reaps_term_resistant_process(tmp_path, monkeypatch):
    monkeypatch.setattr("evolution.llm.agent_backend._PROCESS_TERM_GRACE_SECONDS", 0.05)
    pid_file = tmp_path / "pid"
    backend = AgentBackend(model="codex", agent_bin=sys.executable)
    invocation = AgentInvocation(
        argv=[
            sys.executable,
            "-c",
            "import os, signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1], 'w').write(str(os.getpid())); "
            "time.sleep(60)",
            str(pid_file),
        ],
        stdin=None,
        cwd=None,
        out_file=None,
    )

    timed_out, returncode, _, _, _ = asyncio.run(backend._run(invocation, 0.2))

    assert timed_out
    assert returncode == -signal.SIGKILL
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancellation_kills_and_reaps_agent_process(tmp_path, monkeypatch):
    monkeypatch.setattr("evolution.llm.agent_backend._PROCESS_TERM_GRACE_SECONDS", 0.05)
    pid_file = tmp_path / "pid"
    backend = AgentBackend(model="codex", agent_bin=sys.executable)
    invocation = AgentInvocation(
        argv=[
            sys.executable,
            "-c",
            "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
            str(pid_file),
        ],
        stdin=None,
        cwd=None,
        out_file=None,
    )

    async def cancel_run() -> None:
        task = asyncio.create_task(backend._run(invocation, 60))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("agent process did not publish its PID")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_kills_term_resistant_grandchild_after_parent_exits(tmp_path, monkeypatch):
    monkeypatch.setattr("evolution.llm.agent_backend._PROCESS_TERM_GRACE_SECONDS", 0.05)
    pid_file = tmp_path / "grandchild-pid"
    child_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])"
    )
    backend = AgentBackend(model="codex", agent_bin=sys.executable)
    invocation = AgentInvocation(
        argv=[sys.executable, "-c", parent_code, child_code, str(pid_file)],
        stdin=None,
        cwd=None,
        out_file=None,
    )

    timed_out, _, _, _, _ = asyncio.run(backend._run(invocation, 0.3))

    assert timed_out
    grandchild_pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(grandchild_pid, 0)
            state = Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2]
            if state == "Z":
                break
        except (FileNotFoundError, ProcessLookupError):
            break
        time.sleep(0.01)
    else:
        pytest.fail("grandchild survived process-group timeout cleanup")


@pytest.mark.parametrize("timed_out,rc", [(True, -15), (False, 7)])
async def test_file_edit_error_discriminates_timeout_without_changing_text(
    tmp_path, monkeypatch, timed_out, rc
):
    from evolution.llm.agent_backend import AgentBackendError

    backend = AgentBackend(model="codex", agent_bin="/bin/true", reasoning_effort="high")

    async def run(invocation, timeout):
        assert timeout == 12
        return timed_out, rc, "", "", 1

    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(AgentBackendError) as caught:
        await backend.complete_file_edit(tmp_path, "edit", timeout_s=12)
    assert type(caught.value) is AgentBackendError
    assert caught.value.timed_out is timed_out
    if timed_out:
        assert (
            str(caught.value)
            == "codex file-edit timed out after 12s (model=codex/gpt-5.5, effort=high)"
        )
    else:
        assert (
            str(caught.value)
            == "codex file-edit exited rc=7 (model=codex/gpt-5.5, effort=high). tail:\n\n"
        )


def test_agent_backend_error_discriminator_is_optional():
    from evolution.llm.agent_backend import AgentBackendError

    error = AgentBackendError("old error")
    assert error.timed_out is False
    assert error.args == ("old error",)
    assert str(error) == "old error"
