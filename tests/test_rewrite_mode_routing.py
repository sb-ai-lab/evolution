from __future__ import annotations

import pytest

from evolution.cli.commands.evolve import _resolve_cli_rewrite_mode
from evolution.evolve.reflect import normalize_rewrite_mode


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text-return", "text-return"),
        ("file_edit", "file-edit"),
        ("agentic_session", "agentic-session"),
        ("session", "agentic-session"),
        ("edit_ops", "edit-ops"),
    ],
)
def test_cli_rewrite_mode_normalizes_all_modes(raw, expected, monkeypatch):
    monkeypatch.delenv("EVO_REWRITE_MODE", raising=False)
    assert _resolve_cli_rewrite_mode(raw) == (expected, "option")


def test_cli_rewrite_option_wins_over_environment(monkeypatch):
    monkeypatch.setenv("EVO_REWRITE_MODE", "file-edit")
    assert _resolve_cli_rewrite_mode("edit_ops") == ("edit-ops", "option")
    assert _resolve_cli_rewrite_mode(None) == ("file-edit", "environment")


def test_cli_rewrite_mode_defaults_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("EVO_REWRITE_MODE", raising=False)
    assert _resolve_cli_rewrite_mode(None) == ("text-return", "default")
    monkeypatch.setenv("EVO_REWRITE_MODE", "unknown")
    with pytest.raises(ValueError, match="invalid rewrite mode"):
        _resolve_cli_rewrite_mode(None)


def test_low_level_mode_uses_the_same_normalized_value():
    assert normalize_rewrite_mode("agentic_session") == "agentic-session"
