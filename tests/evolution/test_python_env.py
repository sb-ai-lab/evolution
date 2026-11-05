"""Unit tests for role -> interpreter resolution (eval/python_env.py).

Guards the confounder fix: sb-bench solutions + tests must run under the
role's conda env (which carries duckdb/pandera/openpyxl/pdf/docx/pptx), not
the evo CLI's own interpreter.
"""

from __future__ import annotations

import sys

from evolution.eval import python_env
from evolution.eval.python_env import resolve_python_for_role


def test_none_role_uses_current_interpreter():
    assert resolve_python_for_role(None) == sys.executable
    assert resolve_python_for_role("") == sys.executable


def test_known_role_maps_to_env_python_when_present(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: True)
    assert resolve_python_for_role("de") == python_env.DEFAULT_ROLE_PYTHON_MAP["de"]
    assert resolve_python_for_role("INFRA") == python_env.DEFAULT_ROLE_PYTHON_MAP["infra"]


def test_missing_env_python_falls_back_to_current_interpreter(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: False)
    monkeypatch.setattr(python_env, "_warned_roles", set())
    assert resolve_python_for_role("ds") == sys.executable


def test_env_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(python_env.os.path, "exists", lambda p: True)
    monkeypatch.setenv("EVO_ROLE_PYTHON_DE", "/custom/python")
    assert resolve_python_for_role("de") == "/custom/python"


def test_unknown_role_falls_back_silently(monkeypatch):
    monkeypatch.setattr(python_env, "_warned_roles", set())
    assert resolve_python_for_role("totally-not-a-role") == sys.executable
