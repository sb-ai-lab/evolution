"""rev J1: reflection free-text fed to the rewriter is sanitized in oracle
mode (the reflector may have quoted an expected value), passed through in blind
mode, and the on-disk/source reflection dict is never mutated."""

from __future__ import annotations

from evolution.evolution.reflect import _sanitize_reflections_for_rewriter


def _reflection() -> dict:
    return {
        "root_cause": "model emitted the wrong status",
        "skill_gap": "skill should output 'DriversLicense' and a total of 123456789",
        "suggested_fix": "describe the field generically",
        "pattern": "logic_error",
    }


def test_oracle_mode_redacts_literals(monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    out = _sanitize_reflections_for_rewriter([_reflection()])
    assert "DriversLicense" not in out[0]["skill_gap"]
    assert "123456789" not in out[0]["skill_gap"]


def test_blind_mode_passes_through(monkeypatch):
    monkeypatch.setenv("EVO_REFLECT_BLIND", "1")
    src = _reflection()
    out = _sanitize_reflections_for_rewriter([src])
    assert out[0]["skill_gap"] == src["skill_gap"]


def test_source_dict_is_not_mutated(monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    src = _reflection()
    snapshot = dict(src)
    _sanitize_reflections_for_rewriter([src])
    # The disk-backed reflection stays verbatim; only the in-memory copy is cleaned.
    assert src == snapshot
