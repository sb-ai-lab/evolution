"""``evolution.config`` accessors must read ``os.environ`` on each call.

Many ``EVO_*`` flags are toggled mid-run (paper pipelines, tests). If an
accessor cached at import time, a ``monkeypatch.setenv`` would silently not take
effect. These tests pin the read-on-access contract.
"""

from __future__ import annotations

from evolution import config


def test_reflect_blind_reads_env_each_call(monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    assert config.reflect_blind() is False
    monkeypatch.setenv("EVO_REFLECT_BLIND", "1")
    assert config.reflect_blind() is True
    monkeypatch.setenv("EVO_REFLECT_BLIND", "0")
    assert config.reflect_blind() is False


def test_llm_reasoning_effort_reads_env_each_call(monkeypatch):
    monkeypatch.delenv("EVO_LLM_REASONING_EFFORT", raising=False)
    assert config.llm_reasoning_effort() is None
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")
    assert config.llm_reasoning_effort() == "high"
