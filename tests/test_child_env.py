"""Minimal environment contract for generated solutions and verifiers."""

from __future__ import annotations

from evolution.eval.child_env import minimal_child_env


def test_minimal_child_env_keeps_runtime_basics_and_requires_explicit_inheritance() -> None:
    base = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": "provider-secret",
        "ANTHROPIC_API_KEY": "provider-secret",
        "EXPERIMENT_LABEL": "anchor-a",
    }

    minimal = minimal_child_env(base)
    allowed = minimal_child_env(base, allow={"EXPERIMENT_LABEL", "OPENAI_API_KEY"})

    assert minimal == {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    assert allowed["EXPERIMENT_LABEL"] == "anchor-a"
    assert allowed["OPENAI_API_KEY"] == "provider-secret"
    assert "ANTHROPIC_API_KEY" not in allowed
