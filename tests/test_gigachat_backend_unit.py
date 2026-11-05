"""Unit tests for GigaChatBackend that do not need a live API.

Covers the retry classifier and kwargs sanitization. Live API tests live
in ``test_gigachat_backend.py`` and are gated by the ``live`` marker.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("gigachat")

from evolution.llm.gigachat_backend import GigaChatBackend, _is_retryable


class _FakeHttpxTimeout(Exception):
    """Stand-in for httpx.ConnectTimeout — same module-name prefix matters."""


_FakeHttpxTimeout.__module__ = "httpx._exceptions"


def test_classifier_retries_scope_flap():
    # The "scope from db" auth flap is transient even though the message
    # looks like an auth error.
    exc = Exception("scope from db not fully includes consumed scope")
    assert _is_retryable(exc) is True


def test_classifier_treats_401_as_fatal():
    assert _is_retryable(Exception("HTTP 401 unauthorized")) is False
    assert _is_retryable(Exception("Invalid credentials")) is False


def test_classifier_retries_unknown_errors():
    # GigaChat default policy is "retry unless explicitly fatal".
    assert _is_retryable(Exception("connection reset")) is True
    assert _is_retryable(Exception("503 service unavailable")) is True


def test_classifier_retries_httpx_timeouts_with_empty_message():
    exc = _FakeHttpxTimeout("")
    assert _is_retryable(exc) is True


def test_kwargs_sanitization_drops_empty_api_base():
    b = GigaChatBackend(credentials="fake-key", api_base="")
    assert "api_base" not in b._client_kwargs
    # And api_base is dropped even when populated, since GigaChat ignores it.
    b2 = GigaChatBackend(credentials="fake-key", api_base="https://x")
    assert "api_base" not in b2._client_kwargs


def test_timeout_default_is_180s(monkeypatch):
    monkeypatch.delenv("GIGACHAT_TIMEOUT_SEC", raising=False)
    b = GigaChatBackend(credentials="fake-key")
    assert b._client_kwargs["timeout"] == 180.0


def test_timeout_overridable_via_env(monkeypatch):
    monkeypatch.setenv("GIGACHAT_TIMEOUT_SEC", "30")
    b = GigaChatBackend(credentials="fake-key")
    assert b._client_kwargs["timeout"] == 30.0


def test_scope_default_pers(monkeypatch):
    monkeypatch.delenv("GIGACHAT_SCOPE", raising=False)
    b = GigaChatBackend(credentials="fake-key")
    assert b._client_kwargs["scope"] == "GIGACHAT_API_PERS"


def test_scope_env_override(monkeypatch):
    monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_CORP")
    b = GigaChatBackend(credentials="fake-key")
    assert b._client_kwargs["scope"] == "GIGACHAT_API_CORP"


def test_api_key_alias_for_credentials():
    b = GigaChatBackend(api_key="alias-key")
    assert b._client_kwargs["credentials"] == "alias-key"


@pytest.mark.asyncio
async def test_complete_temporarily_removes_proxy_env(monkeypatch):
    import gigachat

    captured: dict[str, str | None] = {}

    class _Message:
        content = "OK"

    class _Choice:
        message = _Message()

    class _Usage:
        prompt_tokens = 3
        completion_tokens = 1

    class _Response:
        choices = [_Choice()]
        usage = _Usage()

    class _FakeGigaChat:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def achat(self, chat_request):
            captured["HTTP_PROXY"] = os.environ.get("HTTP_PROXY")
            captured["HTTPS_PROXY"] = os.environ.get("HTTPS_PROXY")
            captured["ALL_PROXY"] = os.environ.get("ALL_PROXY")
            captured["http_proxy"] = os.environ.get("http_proxy")
            captured["https_proxy"] = os.environ.get("https_proxy")
            captured["all_proxy"] = os.environ.get("all_proxy")
            return _Response()

    monkeypatch.setattr(gigachat, "GigaChat", _FakeGigaChat)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy")
    monkeypatch.setenv("http_proxy", "http://lower-proxy")
    monkeypatch.setenv("https_proxy", "http://lower-secure-proxy")
    monkeypatch.setenv("all_proxy", "socks5://lower-proxy")

    backend = GigaChatBackend(credentials="fake-key")
    response = await backend.complete([{"role": "user", "content": "ping"}])

    assert response.content == "OK"
    assert captured == {
        "HTTP_PROXY": None,
        "HTTPS_PROXY": None,
        "ALL_PROXY": None,
        "http_proxy": None,
        "https_proxy": None,
        "all_proxy": None,
    }
    assert os.environ["HTTP_PROXY"] == "http://proxy"
    assert os.environ["HTTPS_PROXY"] == "http://secure-proxy"
    assert os.environ["ALL_PROXY"] == "socks5://proxy"
    assert os.environ["http_proxy"] == "http://lower-proxy"
    assert os.environ["https_proxy"] == "http://lower-secure-proxy"
    assert os.environ["all_proxy"] == "socks5://lower-proxy"
