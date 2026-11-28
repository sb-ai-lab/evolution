"""``lab-operation-summary/1`` (spec 2026-09-29 §2.2): a list row without ``result.core``."""
from __future__ import annotations

from collections.abc import Mapping

from .models import OperationResult, OwnerError, _thaw_json

SUMMARY_KEYS = frozenset({"id", "kind", "entrypoint", "control_state", "results_state", "resume",
                          "progress", "progress_state", "times", "times_state", "times_source",
                          "times_reason", "outcome"})


def short_outcome(status) -> dict | None:
    return _short(status.kind, status.result)


def short_outcome_of_result(kind, result_payload) -> dict | None:
    """The short outcome of a stored ``lab-operation-result/1`` payload (a link's ``last_seen``
    snapshot of another pair); an absent or unreadable snapshot has none."""
    if kind != "campaign" or result_payload is None:
        return None
    try:
        result = OperationResult.from_payload(result_payload)
    except (OwnerError, ValueError, TypeError):
        return None
    return _short(kind, result)


def _short(kind, result) -> dict | None:
    if kind != "campaign" or result is None:
        return None
    core = _thaw_json(result.core)
    outcome = core.get("campaign_outcome") if isinstance(core, Mapping) else None
    if not isinstance(outcome, Mapping):
        return None
    champion = core.get("final_champion")
    return {"outcome": outcome.get("outcome"), "reason": outcome.get("reason"),
            "champion": champion.get("version") if isinstance(champion, Mapping) else None}


def operation_summary(status) -> dict:
    return {"id": status.id, "kind": status.kind, "entrypoint": status.entrypoint,
            "control_state": status.control_state, "results_state": status.results_state,
            "resume": None if status.resume is None else status.resume.to_payload(),
            "progress": _thaw_json(status.progress), "progress_state": status.progress_state,
            "times": _thaw_json(status.times), "times_state": status.times_state,
            "times_source": status.times_source, "times_reason": status.times_reason,
            "outcome": short_outcome(status)}
