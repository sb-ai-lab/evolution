"""LLM transport failure must be a loud invalid trace, never a scored stub.

Regression guard for the t34v7 proxy-leak incident: a total client-transport
failure was laundered into status=completed / valid_for_eval=True stub rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.eval.solver import solve_task
from evolution.llm.backend import LLMResponse
from tests.test_solver import _make_solvable_task


class _RaisingBackend:
    """Every LLM call raises (simulates litellm APIError: Access denied)."""

    seed_mode = "forwarded"

    async def complete(self, messages, system=None, seed=None) -> LLMResponse:
        raise RuntimeError("litellm.APIError: OpenAIException - Access denied")

    async def complete_with_skill(self, messages, skill, system=None, seed=None):
        return await self.complete(messages, system, seed)

    async def complete_with_skills(self, messages, skills, system=None, seed=None):
        return await self.complete(messages, system, seed)


class _EmptyZeroTokenBackend:
    """Returns an empty, zero-token response without raising (silent no-call)."""

    seed_mode = "forwarded"

    async def complete(self, messages, system=None, seed=None) -> LLMResponse:
        return LLMResponse(content="", tokens_in=0, tokens_out=0, time_ms=0)

    async def complete_with_skill(self, messages, skill, system=None, seed=None):
        return await self.complete(messages, system, seed)

    async def complete_with_skills(self, messages, skills, system=None, seed=None):
        return await self.complete(messages, system, seed)


def test_raising_backend_marks_transport_failed(tmp_path: Path) -> None:
    task, workspace = _make_solvable_task(tmp_path)
    result = solve_task(task, _RaisingBackend(), workspace)
    assert result.llm_transport_failed is True
    assert result.code_extraction_failed is True
    assert result.runtime_repair_attempted is False
    # stub still written so the harness does not crash
    assert (workspace / "_solve.py").exists()


def test_zero_token_response_is_transport_failed(tmp_path: Path) -> None:
    task, workspace = _make_solvable_task(tmp_path)
    result = solve_task(task, _EmptyZeroTokenBackend(), workspace)
    assert result.llm_transport_failed is True
    assert result.code_extraction_failed is True
    assert result.runtime_repair_attempted is False


def test_genuine_no_code_block_is_not_transport_failed(tmp_path: Path) -> None:
    """Model responded with prose (real, valid signal) — must stay
    code_extraction_failed, never reclassified as transport."""
    from evolution.llm.mock_backend import MockBackend

    task, workspace = _make_solvable_task(tmp_path)
    backend = MockBackend(responses=["I cannot solve this.", "Still no code here."])
    result = solve_task(task, backend, workspace)
    assert result.llm_transport_failed is False
    assert result.code_extraction_failed is True
    assert result.runtime_repair_attempted is False


def test_max_tokens_clamped_for_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    litellm = pytest.importorskip("litellm")
    import evolution.llm.litellm_backend as lb

    lb._MAX_MODEL_LEN.clear()
    lb._MAX_MODEL_LEN["http://localhost:30004/v1"] = 131072

    captured: dict = {}

    class _Msg:
        content = "OK"
        reasoning_content = None

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 5
        completion_tokens = 2

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def _fake_acompletion(model, messages, **kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setenv("EVO_PROMPT_RESERVE_TOKENS", "32768")

    import asyncio

    be = lb.LiteLLMBackend(
        model="openai/openai/gpt-oss-120b",
        api_base="http://localhost:30004/v1",
        api_key="EMPTY",
        max_tokens=131072,
    )
    asyncio.run(be.complete([{"role": "user", "content": "hi"}]))
    assert captured["max_tokens"] == 131072 - 32768


def test_max_tokens_untouched_for_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    litellm = pytest.importorskip("litellm")
    import evolution.llm.litellm_backend as lb

    captured: dict = {}

    class _Msg:
        content = "OK"
        reasoning_content = None

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 5
        completion_tokens = 2

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def _fake_acompletion(model, messages, **kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    import asyncio

    be = lb.LiteLLMBackend(
        model="anthropic/claude-sonnet-4-5",
        api_base="https://openrouter.ai/api/v1",
        api_key="x",
        max_tokens=131072,
    )
    asyncio.run(be.complete([{"role": "user", "content": "hi"}]))
    assert captured["max_tokens"] == 131072
