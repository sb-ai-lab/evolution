"""Unit tests for the coding-agent CLI backend that avoid live agent calls."""

from __future__ import annotations

from evolution.llm.agent_backend import (
    AgentBackend,
    ClaudeCli,
    CodexCli,
    HermesCli,
    _format_agent_prompt,
    _normalise_model,
    _parse_claude_result,
    _parse_claude_usage,
    _parse_tokens_used,
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


def test_agent_backend_model_labels():
    assert AgentBackend(model="claude-code").model_label == "claude-code/claude-sonnet-4-6"
    assert AgentBackend(model="hermes").model_label == "hermes/default"
    assert AgentBackend(model="codex").model_label == "codex/gpt-5.5"


def test_codex_text_invocation_shape(tmp_path):
    cli = CodexCli()
    inv = cli.text_invocation(
        agent_bin="/usr/bin/codex",
        model="gpt-5.5",
        reasoning_effort="high",
        sandbox="read-only",
        prompt="hello",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )
    assert inv.argv[:2] == ["/usr/bin/codex", "exec"]
    assert "--json" in inv.argv
    assert "-o" in inv.argv and str(tmp_path / "out.txt") in inv.argv
    assert "-C" in inv.argv and str(tmp_path) in inv.argv
    assert inv.stdin == "hello"
    assert inv.cwd is None  # codex uses -C, not subprocess cwd


def test_claude_text_invocation_shape(tmp_path):
    cli = ClaudeCli()
    inv = cli.text_invocation(
        agent_bin="/usr/bin/claude",
        model="claude-sonnet-4-6",
        reasoning_effort="high",
        sandbox="read-only",
        prompt="hello",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )
    assert inv.argv[:2] == ["/usr/bin/claude", "-p"]
    assert "--output-format" in inv.argv and "json" in inv.argv
    # text path disables edit tools (empty tools value follows --tools)
    assert "--tools" in inv.argv
    assert inv.argv[inv.argv.index("--tools") + 1] == ""
    assert inv.stdin == "hello"
    assert inv.cwd == tmp_path  # claude is cwd-scoped
    assert inv.out_file is None


def test_claude_file_edit_invocation_enables_tools(tmp_path):
    cli = ClaudeCli()
    inv = cli.file_edit_invocation(
        agent_bin="/usr/bin/claude",
        model="claude-sonnet-4-6",
        reasoning_effort="high",
        prompt="edit it",
        work_dir=tmp_path,
        out_file=tmp_path / "meta.txt",
    )
    assert inv.argv[inv.argv.index("--tools") + 1] == "Read,Edit,Write,Glob"
    assert inv.cwd == tmp_path
    assert inv.diagnostics_seed["sandbox"] == "claude-skip-permissions"


def test_hermes_invocation_prompt_in_argv(tmp_path):
    cli = HermesCli()
    inv = cli.text_invocation(
        agent_bin="/usr/bin/hermes",
        model="my-model",
        reasoning_effort="high",
        sandbox="read-only",
        prompt="hi there",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )
    assert inv.argv[:3] == ["/usr/bin/hermes", "-z", "hi there"]
    assert "-m" in inv.argv and "my-model" in inv.argv
    assert inv.stdin is None  # hermes carries the prompt in argv
    assert inv.cwd == tmp_path


def test_hermes_omits_model_when_empty(tmp_path):
    cli = HermesCli()
    inv = cli.text_invocation(
        agent_bin="/usr/bin/hermes",
        model="",
        reasoning_effort="high",
        sandbox="read-only",
        prompt="hi",
        work=tmp_path,
        out_file=tmp_path / "out.txt",
    )
    assert inv.argv == ["/usr/bin/hermes", "-z", "hi"]


def test_hermes_rejects_oversized_prompt(tmp_path):
    import pytest

    from evolution.llm.agent_backend import _HERMES_ARGV_PROMPT_LIMIT, AgentBackendError

    cli = HermesCli()
    big = "x" * (_HERMES_ARGV_PROMPT_LIMIT + 1)
    with pytest.raises(AgentBackendError, match="too large for argv"):
        cli.text_invocation(
            agent_bin="/usr/bin/hermes",
            model="m",
            reasoning_effort="high",
            sandbox="read-only",
            prompt=big,
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
