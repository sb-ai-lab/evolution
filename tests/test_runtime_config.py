from __future__ import annotations

from pathlib import Path

import pytest

from evolution.config import llm_max_tokens
from evolution.eval.paths import resolve_tasks_root


def test_task_root_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVO_TASKS_ROOT", str(tmp_path / "from-env"))

    assert resolve_tasks_root(project_dir=tmp_path) == (tmp_path / "from-env").resolve()
    assert (
        resolve_tasks_root(explicit=tmp_path / "explicit", project_dir=tmp_path)
        == (tmp_path / "explicit").resolve()
    )


def test_task_root_defaults_to_project_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVO_TASKS_ROOT", raising=False)

    assert resolve_tasks_root(project_dir=tmp_path) == (tmp_path / "tasks").resolve()


def test_max_tokens_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVO_LLM_MAX_TOKENS", "8192")

    assert llm_max_tokens() == 8192
    assert llm_max_tokens(4096) == 4096
    monkeypatch.delenv("EVO_LLM_MAX_TOKENS")
    assert llm_max_tokens() == 16384


@pytest.mark.parametrize("raw", ["0", "-1", "many"])
def test_max_tokens_rejects_invalid_environment(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVO_LLM_MAX_TOKENS", raw)

    with pytest.raises(ValueError, match="EVO_LLM_MAX_TOKENS"):
        llm_max_tokens()
