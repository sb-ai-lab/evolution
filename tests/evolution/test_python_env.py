"""Unit tests for role -> interpreter resolution (eval/python_env.py).

Guards the confounder fix: sb-bench solutions + tests must run under the role's
conda env, resolved from the real account home (passwd), independent of $HOME.
A role with no resolvable interpreter hard-fails rather than silently falling
back to the evo interpreter.
"""

from __future__ import annotations

import sys

import pytest

from evolution.eval import python_env
from evolution.eval.python_env import (
    RolePythonUnavailable,
    expected_role_python,
    resolve_python_for_role,
)


def test_none_role_uses_current_interpreter():
    assert resolve_python_for_role(None) == sys.executable
    assert resolve_python_for_role("") == sys.executable


def test_known_role_maps_to_env_python_when_present(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: True)
    assert resolve_python_for_role("de") == expected_role_python("de")
    assert resolve_python_for_role("INFRA") == expected_role_python("infra")


def test_mlspace_root_uses_account_home_not_dollar_home(monkeypatch, tmp_path):
    monkeypatch.delenv("EVO_MLSPACE_ROOT", raising=False)
    monkeypatch.delenv("EVO_ROLE_PYTHON_DE", raising=False)
    monkeypatch.setattr(python_env, "account_home", lambda: tmp_path)
    monkeypatch.setenv("HOME", "/nonexistent/sandbox")
    assert expected_role_python("de") == str(
        tmp_path / ".mlspace" / "envs" / "de-env" / "bin" / "python"
    )


def test_mlspace_root_env_override(monkeypatch, tmp_path):
    root = tmp_path / "ml"
    monkeypatch.setenv("EVO_MLSPACE_ROOT", str(root))
    monkeypatch.delenv("EVO_ROLE_PYTHON_DS", raising=False)
    assert expected_role_python("ds") == str(root / "envs" / "ds-env" / "bin" / "python")


def test_missing_env_python_hard_fails(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: False)
    monkeypatch.delenv("EVO_DISABLE_ROLE_PYTHON", raising=False)
    with pytest.raises(RolePythonUnavailable):
        resolve_python_for_role("ds")


def test_disable_role_python_opt_out(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: False)
    monkeypatch.setenv("EVO_DISABLE_ROLE_PYTHON", "1")
    assert resolve_python_for_role("ds") == sys.executable


def test_env_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: True)
    monkeypatch.setenv("EVO_ROLE_PYTHON_DE", "/custom/python")
    assert resolve_python_for_role("de") == "/custom/python"


def test_unknown_role_hard_fails(monkeypatch):
    monkeypatch.delenv("EVO_DISABLE_ROLE_PYTHON", raising=False)
    with pytest.raises(RolePythonUnavailable):
        resolve_python_for_role("totally-not-a-role")
