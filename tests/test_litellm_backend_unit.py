"""Unit tests for LiteLLMBackend that do not need a live LLM endpoint.

The retry classifier and Qwen3 thinking-off behavior can be exercised
without ever calling ``litellm.acompletion``.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("litellm")

from litellm.exceptions import RateLimitError

from evolution.llm.litellm_backend import (
    LiteLLMBackend,
    _is_retryable,
    env_without_proxy_for_local_model,
    is_local_api_base,
)


def test_qwen3_disables_thinking_by_default(monkeypatch):
    monkeypatch.delenv("QWEN3_THINKING_ON", raising=False)
    b = LiteLLMBackend(model="openai/Qwen/Qwen3-30B")
    assert b._kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_qwen3_thinking_on_env_opts_in(monkeypatch):
    monkeypatch.setenv("QWEN3_THINKING_ON", "1")
    b = LiteLLMBackend(model="openai/Qwen/Qwen3-30B")
    assert "extra_body" not in b._kwargs


def test_qwen3_caller_supplied_extra_body_wins(monkeypatch):
    monkeypatch.delenv("QWEN3_THINKING_ON", raising=False)
    b = LiteLLMBackend(
        model="openai/Qwen/Qwen3-30B",
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    assert b._kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_non_qwen3_model_is_unchanged():
    b = LiteLLMBackend(model="openai/gpt-4o-mini")
    assert "extra_body" not in b._kwargs


# ── local reasoning-model generalization ────────────────────────────────────


def test_gpt_oss_sets_reasoning_effort_low(monkeypatch):
    monkeypatch.delenv("EVO_LLM_THINKING_ON", raising=False)
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)
    b = LiteLLMBackend(model="openai/gpt-oss-120b")
    assert b._kwargs["extra_body"] == {"reasoning_effort": "low"}


def test_reasoning_effort_env_overrides_auto_default(monkeypatch):
    # The paper pipeline sets EVO_LLM_REASONING_EFFORT; it must win over the
    # gpt-oss auto-default of "low".
    monkeypatch.delenv("EVO_LLM_THINKING_ON", raising=False)
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")
    b = LiteLLMBackend(model="openai/gpt-oss-120b")
    assert b._kwargs["extra_body"]["reasoning_effort"] == "high"


def test_thinking_on_env_opts_out_all_families(monkeypatch):
    monkeypatch.setenv("EVO_LLM_THINKING_ON", "1")
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)
    assert "extra_body" not in LiteLLMBackend(model="openai/gpt-oss-120b")._kwargs
    assert "extra_body" not in LiteLLMBackend(model="openai/Qwen/Qwen3-30B")._kwargs


def test_caller_extra_body_wins_for_gpt_oss(monkeypatch):
    monkeypatch.delenv("EVO_LLM_THINKING_ON", raising=False)
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)
    b = LiteLLMBackend(
        model="openai/gpt-oss-120b",
        extra_body={"reasoning_effort": "medium"},
    )
    assert b._kwargs["extra_body"] == {"reasoning_effort": "medium"}


def test_explicit_qwen_off_beats_legacy_environment(monkeypatch):
    monkeypatch.setenv("EVO_LLM_TEMPERATURE", "2")
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")

    backend = LiteLLMBackend(
        model="openai/Qwen/Qwen3.5-35B-A3B",
        temperature=0,
        reasoning_effort="off",
    )

    assert backend._kwargs == {
        "temperature": 0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }


def test_explicit_gpt_oss_effort_overrides_auto_low_and_legacy_environment(monkeypatch):
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")

    backend = LiteLLMBackend(
        model="openai/openai/gpt-oss-120b",
        reasoning_effort="medium",
    )

    assert backend._kwargs == {"extra_body": {"reasoning_effort": "medium"}}


def test_cloud_reasoning_effort_uses_litellm_provider_translation(monkeypatch):
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)

    backend = LiteLLMBackend(
        model="anthropic/claude-sonnet-4",
        reasoning_effort="high",
    )

    assert backend._kwargs["reasoning_effort"] == "high"
    assert "extra_body" not in backend._kwargs


def test_off_uses_each_model_familys_supported_control(monkeypatch):
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)

    gpt_oss = LiteLLMBackend(model="openai/gpt-oss-120b", reasoning_effort="off")
    generic = LiteLLMBackend(model="openai/gpt-4o-mini", reasoning_effort="off")

    assert gpt_oss._kwargs == {"extra_body": {"reasoning_effort": "low"}}
    assert "extra_body" not in generic._kwargs


def test_local_api_base_added_to_no_proxy(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    backend = LiteLLMBackend(model="openai/gpt-4o-mini", api_base="http://127.0.0.1:10115/v1")
    assert backend._kwargs["api_key"] == "not-needed"
    assert "127.0.0.1" in os.environ.get("NO_PROXY", "")
    assert "127.0.0.1" in os.environ.get("no_proxy", "")


def test_local_no_proxy_is_non_destructive(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.delenv("no_proxy", raising=False)
    LiteLLMBackend(model="openai/gpt-4o-mini", api_base="http://localhost:8000/v1")
    entries = set(os.environ["NO_PROXY"].split(","))
    assert "example.com" in entries and "localhost" in entries


def test_remote_api_base_does_not_touch_no_proxy(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    backend = LiteLLMBackend(
        model="anthropic/claude-sonnet-4", api_base="https://api.anthropic.com"
    )
    assert "api_key" not in backend._kwargs
    assert os.environ.get("NO_PROXY", "") == ""


def test_complete_falls_back_to_reasoning_content(monkeypatch):
    """When ``content`` is empty, ``complete`` returns ``reasoning_content``."""
    import asyncio

    import litellm

    class _Msg:
        content = ""
        reasoning_content = "```python\nprint('hi')\n```"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Usage:
        prompt_tokens = 5
        completion_tokens = 7

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def fake_acompletion(**kwargs):
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    b = LiteLLMBackend(model="openai/gpt-4o-mini")
    resp = asyncio.run(b.complete([{"role": "user", "content": "hi"}]))
    assert "print('hi')" in resp.content


def test_complete_forwards_exact_seed(monkeypatch):
    import litellm

    captured: dict[str, object] = {}

    class _Msg:
        content = "OK"
        reasoning_content = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    asyncio.run(
        LiteLLMBackend(model="openai/gpt-4o-mini").complete(
            [{"role": "user", "content": "hi"}], seed=0
        )
    )

    assert captured["seed"] == 0


def test_classifier_treats_rate_limit_as_retryable():
    # litellm's RateLimitError requires (message, model, llm_provider).
    exc = RateLimitError("rate limited", "gpt-4", "openai")
    assert _is_retryable(exc) is True


def test_classifier_treats_timeout_string_as_retryable():
    assert _is_retryable(Exception("Read timeout occurred")) is True
    assert _is_retryable(Exception("Connection reset by peer")) is True
    assert _is_retryable(Exception("upstream returned 503")) is True


def test_classifier_treats_auth_error_as_fatal():
    assert _is_retryable(Exception("401 unauthorized")) is False
    assert _is_retryable(Exception("invalid api key")) is False


def test_local_api_base_detection():
    assert is_local_api_base("http://localhost:30004/v1") is True
    assert is_local_api_base("http://127.0.0.1:30004/v1") is True
    assert is_local_api_base("http://[::1]:30004/v1") is True
    assert is_local_api_base("https://openrouter.ai/api/v1") is False
    assert is_local_api_base(None) is False


def test_env_without_proxy_only_changes_local_endpoints():
    env = {
        "HTTP_PROXY": "http://proxy",
        "HTTPS_PROXY": "http://secure-proxy",
        "ALL_PROXY": "socks5://proxy",
        "NO_PROXY": "metadata.google.internal",
        "KEEP": "1",
    }

    local = env_without_proxy_for_local_model("http://localhost:30004/v1", env)
    assert "HTTP_PROXY" not in local
    assert "HTTPS_PROXY" not in local
    assert "ALL_PROXY" not in local
    assert local["KEEP"] == "1"
    assert "localhost" in local["NO_PROXY"]
    assert "127.0.0.1" in local["NO_PROXY"]

    remote = env_without_proxy_for_local_model("https://openrouter.ai/api/v1", env)
    assert remote == env


def test_litellm_local_call_temporarily_removes_proxy_env(monkeypatch):
    litellm = pytest.importorskip("litellm")
    captured: dict[str, str | None] = {}

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
        captured["HTTP_PROXY"] = os.environ.get("HTTP_PROXY")
        captured["HTTPS_PROXY"] = os.environ.get("HTTPS_PROXY")
        captured["ALL_PROXY"] = os.environ.get("ALL_PROXY")
        captured["NO_PROXY"] = os.environ.get("NO_PROXY")
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy")
    monkeypatch.setenv("NO_PROXY", "existing.local")

    be = LiteLLMBackend(
        model="openai/openai/gpt-oss-120b",
        api_base="http://localhost:30004/v1",
        api_key="EMPTY",
    )
    asyncio.run(be.complete([{"role": "user", "content": "hi"}]))

    assert captured["HTTP_PROXY"] is None
    assert captured["HTTPS_PROXY"] is None
    assert captured["ALL_PROXY"] is None
    assert "localhost" in str(captured["NO_PROXY"])
    assert os.environ["HTTP_PROXY"] == "http://proxy"
    assert os.environ["HTTPS_PROXY"] == "http://secure-proxy"
    assert os.environ["ALL_PROXY"] == "socks5://proxy"
    no_proxy_entries = set(os.environ["NO_PROXY"].split(","))
    assert "existing.local" in no_proxy_entries
    assert "localhost" in no_proxy_entries


def test_litellm_remote_call_keeps_proxy_env(monkeypatch):
    litellm = pytest.importorskip("litellm")
    captured: dict[str, str | None] = {}

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
        captured["HTTP_PROXY"] = os.environ.get("HTTP_PROXY")
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")

    be = LiteLLMBackend(
        model="anthropic/claude-sonnet-4-5",
        api_base="https://openrouter.ai/api/v1",
        api_key="x",
    )
    asyncio.run(be.complete([{"role": "user", "content": "hi"}]))

    assert captured["HTTP_PROXY"] == "http://proxy"


def test_non_positive_call_timeout_never_starts_the_request(monkeypatch):
    import litellm

    started: list[bool] = []

    async def fake_acompletion(**kwargs):
        started.append(True)
        raise AssertionError("request must not start")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setenv("EVO_LLM_CALL_TIMEOUT_S", "-1")
    with pytest.raises(TimeoutError):
        asyncio.run(
            LiteLLMBackend(model="openai/gpt-4o-mini").complete([{"role": "user", "content": "hi"}])
        )
    assert started == []
