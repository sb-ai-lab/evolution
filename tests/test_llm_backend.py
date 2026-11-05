"""Tests for LLM backend abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.models import Skill, SkillFrontmatter
from evolution.llm.agent_backend import AgentBackend
from evolution.llm.backend import (
    LLMBackend,
    agent_model_label,
    get_backend,
    is_agent_model,
    model_seed_mode,
)
from evolution.llm.mock_backend import MockBackend


def _make_skill() -> Skill:
    return Skill(
        frontmatter=SkillFrontmatter(name="test", description="test skill"),
        body="# Test\n\nInstructions.",
        path=Path("/fake"),
        skill_md_path=Path("/fake/SKILL.md"),
    )


class TestMockBackend:
    @pytest.mark.asyncio
    async def test_complete(self):
        backend = MockBackend(responses=["hello"])
        resp = await backend.complete([{"role": "user", "content": "hi"}])
        assert resp.content == "hello"
        assert resp.total_tokens > 0

    @pytest.mark.asyncio
    async def test_complete_with_skill(self):
        backend = MockBackend(responses=["skilled response"])
        resp = await backend.complete_with_skill(
            [{"role": "user", "content": "do something"}],
            skill=_make_skill(),
        )
        assert resp.content == "skilled response"
        assert backend.calls[-1]["skills"] == ["test"]

    @pytest.mark.asyncio
    async def test_default_response(self):
        backend = MockBackend()
        resp = await backend.complete([{"role": "user", "content": "hi"}])
        assert resp.content == "mock response"

    @pytest.mark.asyncio
    async def test_sequential_responses(self):
        backend = MockBackend(responses=["first", "second"])
        r1 = await backend.complete([{"role": "user", "content": "1"}])
        r2 = await backend.complete([{"role": "user", "content": "2"}])
        r3 = await backend.complete([{"role": "user", "content": "3"}])
        assert r1.content == "first"
        assert r2.content == "second"
        assert r3.content == "mock response"

    @pytest.mark.asyncio
    async def test_calls_recorded(self):
        backend = MockBackend()
        await backend.complete(
            [{"role": "user", "content": "test"}],
            system="sys prompt",
        )
        assert len(backend.calls) == 1
        assert backend.calls[0]["system"] == "sys prompt"


class TestGetBackend:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("codex", True),
            ("codex/", True),
            ("claude-code/claude-sonnet-4-6", True),
            ("hermes/GigaChat-2-M", True),
            ("openai/gpt-4o", False),
        ],
    )
    def test_agent_model_detection(self, model: str, expected: bool):
        assert is_agent_model(model) is expected

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("codex", "codex/gpt-5.5"),
            ("codex/", "codex/gpt-5.5"),
            ("claude-code", "claude-code/claude-sonnet-4-6"),
            ("hermes", "hermes/default"),
            ("hermes/GigaChat-2-M", "hermes/GigaChat-2-M"),
        ],
    )
    def test_agent_model_label(self, model: str, expected: str):
        assert agent_model_label(model) == expected

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("mock", "forwarded"),
            ("openai/gpt-4o", "forwarded"),
            ("codex/gpt-5.5", "unsupported"),
            ("claude-code/claude-sonnet-4-6", "unsupported"),
            ("hermes/default", "unsupported"),
            ("gigachat/GigaChat-3-Ultra", "unsupported"),
        ],
    )
    def test_model_seed_mode_matches_builtin_backend(self, model: str, expected: str):
        assert model_seed_mode(model) == expected

    def test_mock_backend(self):
        backend = get_backend("mock")
        assert isinstance(backend, MockBackend)

    def test_mock_backend_is_protocol(self):
        backend = get_backend("mock")
        assert isinstance(backend, LLMBackend)

    def test_mock_backend_ignores_solve_kwargs(self):
        # Regression: `evo run solve -m mock` always passes max_tokens (plus
        # api_base/api_key) through get_backend; MockBackend must accept and
        # ignore them like the real backends, not raise TypeError.
        backend = get_backend("mock", max_tokens=100, api_base="x", api_key="y")
        assert isinstance(backend, MockBackend)

    def test_codex_backend(self):
        backend = get_backend("codex/gpt-5.5", api_base="codex-cli", api_key="EMPTY")
        assert isinstance(backend, AgentBackend)
        assert isinstance(backend, LLMBackend)
        assert backend.model_label == "codex/gpt-5.5"
        assert backend.agent_permission == "safe"

    def test_agent_backend_rejects_invalid_permission(self):
        with pytest.raises(ValueError, match="agent_permission must be 'safe' or 'unsafe'"):
            get_backend("codex", agent_permission="sometimes")

    def test_claude_code_backend(self):
        backend = get_backend("claude-code", api_base="x", api_key="EMPTY")
        assert isinstance(backend, AgentBackend)
        assert isinstance(backend, LLMBackend)
        assert backend.model_label == "claude-code/claude-sonnet-4-6"
        assert backend.agent_permission == "safe"

    def test_hermes_backend(self):
        backend = get_backend("hermes/my-model", api_base="x", api_key="EMPTY")
        assert isinstance(backend, AgentBackend)
        assert isinstance(backend, LLMBackend)
        assert backend.model_label == "hermes/my-model"
        assert backend.agent_permission == "safe"

    def test_hermes_empty_suffix_keeps_default_routing(self):
        backend = get_backend("hermes/")
        assert isinstance(backend, AgentBackend)
        assert backend.model_label == "hermes/default"

    def test_bare_claude_still_litellm(self):
        # Bare `claude` must remain a LiteLLM alias, NOT the claude-code CLI.
        pytest.importorskip("litellm")
        from evolution.llm.litellm_backend import LiteLLMBackend

        backend = get_backend("claude", api_base="x", api_key="EMPTY")
        assert isinstance(backend, LiteLLMBackend)
