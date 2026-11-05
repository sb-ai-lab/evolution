"""Tests for GigaChat backend."""

from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

from evolution.llm.backend import get_backend


class TestFactory:
    """Test get_backend() resolves gigachat names correctly."""

    def test_gigachat_default(self):
        backend = get_backend("gigachat", credentials="test")
        from evolution.llm.gigachat_backend import GigaChatBackend

        assert isinstance(backend, GigaChatBackend)
        assert backend._model == "GigaChat-2-Pro"

    def test_gigachat_pro(self):
        backend = get_backend("gigachat-pro", credentials="test")
        from evolution.llm.gigachat_backend import GigaChatBackend

        assert isinstance(backend, GigaChatBackend)
        assert backend._model == "GigaChat-2-Pro"

    def test_gigachat_max(self):
        backend = get_backend("gigachat-max", credentials="test")
        from evolution.llm.gigachat_backend import GigaChatBackend

        assert isinstance(backend, GigaChatBackend)
        assert backend._model == "GigaChat-2-Max"

    def test_gigachat_lite(self):
        backend = get_backend("gigachat-lite", credentials="test")
        from evolution.llm.gigachat_backend import GigaChatBackend

        assert isinstance(backend, GigaChatBackend)
        assert backend._model == "GigaChat-2-Pro-Lite"

    def test_api_key_maps_to_credentials(self):
        backend = get_backend("gigachat", api_key="my-key")
        from evolution.llm.gigachat_backend import GigaChatBackend

        assert isinstance(backend, GigaChatBackend)
        assert backend._client_kwargs["credentials"] == "my-key"

    def test_credentials_preferred_over_api_key(self):
        backend = get_backend("gigachat", credentials="cred", api_key="key")
        assert backend._client_kwargs["credentials"] == "cred"

    def test_default_scope(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_SCOPE", raising=False)
        backend = get_backend("gigachat", credentials="test")
        assert backend._client_kwargs["scope"] == "GIGACHAT_API_PERS"

    def test_scope_from_env(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_CORP")
        backend = get_backend("gigachat", credentials="test")
        assert backend._client_kwargs["scope"] == "GIGACHAT_API_CORP"

    def test_custom_scope(self):
        backend = get_backend("gigachat", credentials="test", scope="GIGACHAT_API_CORP")
        assert backend._client_kwargs["scope"] == "GIGACHAT_API_CORP"

    def test_max_tokens(self):
        backend = get_backend("gigachat", credentials="test", max_tokens=4096)
        assert backend._max_tokens == 4096

    def test_reasoning_effort_is_rejected_instead_of_forwarded(self):
        with pytest.raises(ValueError, match="reasoning_effort"):
            get_backend("gigachat", credentials="test", reasoning_effort="medium")

    def test_non_gigachat_still_uses_litellm(self):
        """Ensure other model names don't accidentally match gigachat."""
        pytest.importorskip("litellm")
        backend = get_backend("openrouter/anthropic/claude-sonnet-4")
        from evolution.llm.litellm_backend import LiteLLMBackend

        assert isinstance(backend, LiteLLMBackend)


class TestMessageConversion:
    """Test that messages are correctly converted to GigaChat format."""

    def test_import(self):
        from evolution.llm.gigachat_backend import GigaChatBackend

        backend = GigaChatBackend(credentials="test")
        assert backend._model == "GigaChat"

    def test_seed_is_rejected_before_sdk_import(self):
        from evolution.llm.gigachat_backend import GigaChatBackend

        backend = GigaChatBackend(credentials="test")
        with pytest.raises(ValueError, match="seed"):
            asyncio.run(backend.complete([{"role": "user", "content": "hi"}], seed=7))


@pytest.mark.live
@pytest.mark.skipif(
    importlib.util.find_spec("gigachat") is None or not os.environ.get("GIGACHAT_CREDENTIALS"),
    reason="requires the GigaChat SDK and GIGACHAT_CREDENTIALS",
)
class TestLive:
    """Live tests — require GIGACHAT_CREDENTIALS env var."""

    @pytest.mark.asyncio
    async def test_simple_completion(self):
        backend = get_backend("gigachat")
        response = await backend.complete(
            [{"role": "user", "content": "Reply with exactly: hello"}],
        )
        assert response.content
        assert response.tokens_in > 0
        assert response.tokens_out > 0
        assert response.time_ms > 0

    @pytest.mark.asyncio
    async def test_with_system_prompt(self):
        backend = get_backend("gigachat")
        response = await backend.complete(
            [{"role": "user", "content": "What is 2+2?"}],
            system="You are a helpful assistant. Reply concisely.",
        )
        assert "4" in response.content
