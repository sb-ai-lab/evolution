"""R1: every LiteLLM call is hard-bounded by EVO_LLM_CALL_TIMEOUT_S.

Regression guard for the spec_006 pre-verifier hang: a code-extraction-retry
LLM call blocked forever because litellm.acompletion had no wall-clock bound.
The timeout must be per-call and NON-retried (not ×6 via retry_async).
"""

from __future__ import annotations

import asyncio
import time

import pytest


def test_litellm_call_hard_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    litellm = pytest.importorskip("litellm")
    import evolution.llm.litellm_backend as lb

    async def _hang(*args, **kwargs):  # never returns
        await asyncio.sleep(10_000)

    monkeypatch.setattr(litellm, "acompletion", _hang)
    monkeypatch.setenv("EVO_LLM_CALL_TIMEOUT_S", "2")

    be = lb.LiteLLMBackend(model="openai/x", api_base="http://localhost:1/v1", api_key="EMPTY")

    t0 = time.time()
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        asyncio.run(be.complete([{"role": "user", "content": "hi"}]))
    dt = time.time() - t0
    # ~one EVO_LLM_CALL_TIMEOUT_S, NOT retried ×6 (retry_async backoff would be minutes)
    assert dt < 30, f"timeout not enforced per-call / was retried: {dt:.1f}s"


def test_litellm_timeout_kwarg_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    litellm = pytest.importorskip("litellm")
    import evolution.llm.litellm_backend as lb

    seen: dict = {}

    class _Msg:
        content = "OK"
        reasoning_content = None

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def _fake(*args, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)
    monkeypatch.setenv("EVO_LLM_CALL_TIMEOUT_S", "123")
    be = lb.LiteLLMBackend(model="openai/x", api_base="https://openrouter.ai/api/v1", api_key="k")
    asyncio.run(be.complete([{"role": "user", "content": "hi"}]))
    assert seen.get("timeout") == 123.0, "litellm.acompletion did not receive the hard timeout"
