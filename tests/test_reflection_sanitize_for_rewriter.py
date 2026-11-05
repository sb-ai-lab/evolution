"""Reflection free-text is passed through under the train-oracle policy.

Train traces may quote train verifier details; held-out protection happens at
the split/provenance gate before reflection, not by scrubbing train evidence
from the rewriter's in-memory input.
"""

from __future__ import annotations

from evolution.evolve.reflect import _sanitize_reflections_for_rewriter


def _reflection() -> dict:
    return {
        "root_cause": "model emitted the wrong status",
        "skill_gap": "skill should output 'DriversLicense' and a total of 123456789",
        "suggested_fix": "describe the field generically",
        "pattern": "logic_error",
    }


def test_train_oracle_mode_preserves_train_literals(monkeypatch):
    monkeypatch.setenv("EVO_OPTIMIZATION_TARGET", "train")
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    out = _sanitize_reflections_for_rewriter([_reflection()])
    assert "DriversLicense" in out[0]["skill_gap"]
    assert "123456789" in out[0]["skill_gap"]


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
    # The disk-backed reflection stays verbatim; the in-memory reference is not scrubbed.
    assert src == snapshot
