"""Tests for evolution.adapters._paths environment-variable resolution."""

from __future__ import annotations

import sys

import pytest

from evolution.adapters import _paths


def test_evo_bin_falls_back_to_path(monkeypatch):
    monkeypatch.delenv("EVO_BIN", raising=False)
    # Returns either an absolute resolution or the literal "evo" sentinel.
    result = _paths.evo_bin()
    assert result == "evo" or result.endswith("/evo")


def test_evo_bin_env_override(monkeypatch):
    monkeypatch.setenv("EVO_BIN", "/custom/path/evo")
    assert _paths.evo_bin() == "/custom/path/evo"


def test_python_bin_default_is_sys_executable(monkeypatch):
    monkeypatch.delenv("EVO_PYTHON", raising=False)
    assert _paths.python_bin() == sys.executable


def test_python_bin_env_override(monkeypatch):
    monkeypatch.setenv("EVO_PYTHON", "/opt/python3.10/bin/python")
    assert _paths.python_bin() == "/opt/python3.10/bin/python"


def test_tasks_root_default_is_repo_relative(monkeypatch):
    monkeypatch.delenv("EVO_TASKS_ROOT", raising=False)
    assert _paths.tasks_root().endswith("/tasks")


def test_tasks_root_env_override(monkeypatch):
    monkeypatch.setenv("EVO_TASKS_ROOT", "/custom/tasks")
    assert _paths.tasks_root() == "/custom/tasks"


@pytest.mark.parametrize(
    "func,var",
    [
        (_paths.evoskill_bin, "EVOSKILL_BIN"),
        (_paths.memento_bin, "MEMENTO_BIN"),
        (_paths.evoskill_root, "EVOSKILL_ROOT"),
    ],
)
def test_required_paths_raise_runtime_error_when_unset(monkeypatch, func, var):
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match=var):
        func()
