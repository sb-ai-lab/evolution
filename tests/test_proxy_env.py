"""Proxy env helper tests independent of any LLM SDK."""

from __future__ import annotations

import os

from evolution.llm.proxy_env import without_proxy_env


def test_without_proxy_env_removes_all_proxy_vars_and_restores(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy")
    monkeypatch.setenv("http_proxy", "http://lower-proxy")
    monkeypatch.setenv("https_proxy", "http://lower-secure-proxy")
    monkeypatch.setenv("all_proxy", "socks5://lower-proxy")

    with without_proxy_env():
        assert os.environ.get("HTTP_PROXY") is None
        assert os.environ.get("HTTPS_PROXY") is None
        assert os.environ.get("ALL_PROXY") is None
        assert os.environ.get("http_proxy") is None
        assert os.environ.get("https_proxy") is None
        assert os.environ.get("all_proxy") is None

    assert os.environ["HTTP_PROXY"] == "http://proxy"
    assert os.environ["HTTPS_PROXY"] == "http://secure-proxy"
    assert os.environ["ALL_PROXY"] == "socks5://proxy"
    assert os.environ["http_proxy"] == "http://lower-proxy"
    assert os.environ["https_proxy"] == "http://lower-secure-proxy"
    assert os.environ["all_proxy"] == "socks5://lower-proxy"
