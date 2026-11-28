"""Restate focus-first stage records, without deciding a gate or counting slots."""
from __future__ import annotations

from collections.abc import Mapping

STATES = ("waiting", "open", "passed", "rejected", "unavailable", "not_run", "incomplete", "unknown")
_DECISIONS = {"passed": "passed", "comparison_not_passed": "rejected", "comparison_unavailable": "unavailable"}


def _focus(attempt: Mapping, facts: Mapping) -> str:
    decision = facts.get("focus_decided")
    if decision in _DECISIONS:
        return _DECISIONS[decision]
    if decision is not None:
        return "unknown"
    if attempt.get("outcome") == "insufficient_evidence":
        return "incomplete"
    if facts.get("focus_opened") is True:
        return "open"
    if facts.get("focus_opened") is False:
        return "waiting"
    return "unknown"


def _causal(attempt: Mapping, facts: Mapping, focus: str) -> str:
    comparison = attempt.get("gate_comparison") or {}
    scope = next((s for s in comparison.get("scopes", ()) if s.get("name") == "causal"), {})
    status, outcome = scope.get("status"), comparison.get("outcome")
    if status == "skipped":
        return "not_run"
    if focus in ("rejected", "unavailable", "incomplete"):
        return "not_run"
    # A completed gate after the focus decision records the causal result. The
    # deciding layer alone is insufficient: a passing finalist may say focus.
    if status == "available" and outcome in ("passed", "comparison_not_passed"):
        return _DECISIONS[outcome]
    # A critical failure can reject C while its estimate remains unavailable.
    if status == "unavailable" and outcome == "comparison_not_passed":
        return "rejected"
    if status == "unavailable" and outcome == "comparison_unavailable":
        return "unavailable"
    if facts.get("causal_opened") is True:
        return "incomplete" if attempt.get("outcome") == "insufficient_evidence" else "open"
    if facts.get("causal_opened") is False and attempt.get("outcome") is None:
        return "waiting"
    return "unknown"


def attempt_stages(attempt: Mapping, facts: Mapping | None) -> list[dict]:
    """Apply dev64 §3's ordered scope rules to Core's projection and raw facts.

    Missing supplementary facts leave both cells unknown, while the containing
    projection remains readable. Attempt closure never erases a scope decision.
    """
    focus = "unknown" if facts is None else _focus(attempt, facts)
    causal = "unknown" if facts is None else _causal(attempt, facts, focus)
    return [{"scope": "focus", "state": focus}, {"scope": "causal", "state": causal}]
