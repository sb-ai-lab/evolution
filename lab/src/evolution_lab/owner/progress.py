"""Campaign progress (W50, spec 2026-09-29 §2.1): one pure function for status and SSE.

``phase`` is Lab's reading of core's public ``next_actions`` (``result.core.actions``), not a
claim of core. The form is closed (ADR-0017): a new key or stage value is a new schema version;
an action this table does not know maps to ``unknown``, which is itself in the closed list.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

STAGES = ("baseline", "reflection", "generation", "evaluation", "selection", "finishing")
STAGE_OF_ACTION: Mapping[str, str] = {
    "BindBaseline": "baseline", "MeasureBaselineStage": "baseline", "CompleteBaseline": "baseline",
    "SelectFocus": "reflection", "RunGrouping": "reflection", "SelectFocusPortion": "reflection",
    "RunReflection": "reflection",
    "OpenAttempt": "generation", "RunEditor": "generation", "SealAttempt": "generation",
    "SelectAffected": "evaluation",
    "BindParent": "evaluation",
    "BindMeasurement": "evaluation", "RunAttemptStage": "evaluation", "EvaluateLook": "evaluation",
    "CloseAttempt": "evaluation",
    # Core's comparison and noise calibration of an Attempt.
    "BindComparison": "evaluation", "CalibrateNoise": "evaluation",
    # Core b15 decides an Attempt's focus after measurement, before causal work or closure.
    "DecideFocus": "evaluation",
    "SelectGateScope": "selection", "DecideRound": "selection",
    # Core a34, family 14 (/15; core 2.0.0b1 evolve admits /15; Lab admits /15 since dev48): a round's baseline refresh.
    "BindBaselineRefresh": "baseline", "CompleteBaselineRefresh": "baseline",
    # Family 14 (/15, dev48 branch 2): core's status renames the cohort actions (spec 2026-10-13 §6.2).
    "BindFinalists": "selection", "BindFinalMember": "selection", "RunFinalStage": "selection", "CompleteFinalMember": "selection",
    "FinishCampaign": "finishing",
    # Core b8, family 15: a failed pre-spend comparison check closes the attempt before measurement; a gate passer's
    # completion and global check is selection work. No new phase (D5).
    "FailPreflight": "evaluation", "AdvanceCandidateCompletion": "selection",
}
_ORDER = {stage: index for index, stage in enumerate(STAGES)} | {"unknown": len(STAGES)}
PHASES = frozenset(STAGES) | {"suspended", "finished", "waiting", "unknown"}
_CAMPAIGN_KEYS = {"kind", "phase", "stages", "rounds", "attempts", "coverage", "policy_state", "observed_at"}


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    """What Lab reads once per PreparedExecution: its entry name and the campaign policy."""
    entrypoint: str | None
    rounds: int | None
    attempts_per_round: int | None
    metric_primary: str | None

    @property
    def policy_state(self) -> str:
        return "present" if self.rounds is not None and self.attempts_per_round is not None else "unavailable"


def _list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _stages(actions: list) -> list[str]:
    seen = {STAGE_OF_ACTION.get(row.get("kind"), "unknown") for row in actions if isinstance(row, Mapping)}
    return sorted(seen, key=_ORDER.__getitem__)


def _phase(core: Mapping, stages: list[str]) -> str:
    if core.get("suspended") is True:
        return "suspended"
    if core.get("complete") is True:
        return "finished"
    known = [stage for stage in stages if stage != "unknown"]
    if known:
        return known[-1]
    return "unknown" if stages else "waiting"


def _coverage(measurements: list) -> dict | None:
    expected = scored = 0
    open_rows = 0
    for row in measurements:
        status = row.get("status") if isinstance(row, Mapping) else None
        if not isinstance(status, Mapping) or status.get("complete") is True:
            continue
        samples = [s for s in _list(status.get("samples")) if isinstance(s, Mapping)]
        open_rows += 1
        expected += len(samples)
        scored += sum(s.get("scoring_state") == "finished" for s in samples)
    return {"expected": expected, "scored": scored} if open_rows else None


def campaign_progress(core: Mapping | None, plan: CampaignPlan, observed_at: str | None) -> dict | None:
    if not isinstance(core, Mapping):
        return None
    stages = _stages(_list(core.get("actions")))
    rounds = [row for row in _list(core.get("rounds")) if isinstance(row, Mapping)]
    completed = sum(row.get("outcome") is not None for row in rounds)
    finished = core.get("complete") is True
    exhausted = plan.rounds is not None and completed >= plan.rounds
    current = None if finished or exhausted else completed + 1
    row = next((r for r in rounds if r.get("round") == current), None)
    attempts = [a for a in _list(row.get("attempts")) if isinstance(a, Mapping)] if row else []
    total = sum(len(_list(r.get("attempts"))) for r in rounds)
    return {
        "kind": "campaign", "phase": _phase(core, stages), "stages": stages,
        "rounds": {"completed": completed, "current": current, "planned": plan.rounds},
        "attempts": {"current_round": {
            "closed": sum(a.get("outcome") is not None for a in attempts),
            "running": sum(a.get("outcome") is None for a in attempts),
            "planned": plan.attempts_per_round}, "total": total},
        "coverage": _coverage(_list(core.get("measurements"))),
        "policy_state": plan.policy_state, "observed_at": observed_at,
    }


def evaluation_progress(coverage, observed_at: str | None) -> dict:
    return {"kind": "evaluation", "coverage": None if coverage is None else coverage.to_payload(),
            "observed_at": observed_at}


def _count(value: Any, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid count")


def _closed(value: Any, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid progress")
    return value


def _pair(value: Any) -> None:
    if value is None:
        return
    row = _closed(value, {"expected", "scored"})
    _count(row["expected"]), _count(row["scored"])
    if row["scored"] > row["expected"]:
        raise ValueError("invalid coverage")


def _moment(value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not value.endswith("Z")):
        raise ValueError("invalid timestamp")


def validate_progress(value: Any, kind: str) -> None:
    if kind == "evaluation":
        row = _closed(value, {"kind", "coverage", "observed_at"})
        _pair(row["coverage"])
    elif kind == "campaign":
        row = _closed(value, _CAMPAIGN_KEYS)
        if row["phase"] not in PHASES:
            raise ValueError("invalid phase")
        stages = row["stages"]
        if not isinstance(stages, list) or any(s not in _ORDER for s in stages) \
                or stages != sorted(set(stages), key=_ORDER.__getitem__):
            raise ValueError("invalid stages")
        rounds = _closed(row["rounds"], {"completed", "current", "planned"})
        _count(rounds["completed"]), _count(rounds["current"], optional=True)
        _count(rounds["planned"], optional=True)
        attempts = _closed(row["attempts"], {"current_round", "total"})
        current = _closed(attempts["current_round"], {"closed", "running", "planned"})
        _count(current["closed"]), _count(current["running"]), _count(current["planned"], optional=True)
        _count(attempts["total"])
        _pair(row["coverage"])
        if row["policy_state"] not in {"present", "unavailable"}:
            raise ValueError("invalid policy_state")
    else:
        raise ValueError("invalid kind")
    if row["kind"] != kind:
        raise ValueError("invalid progress kind")
    _moment(row["observed_at"])
