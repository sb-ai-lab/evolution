"""Evaluate a fixed, already admitted target through core measurement APIs.

This low-level assembly accepts the same bound core objects as a campaign.
The caller owns target materialization/release and budgeted provider bindings;
core owns sessions, execution/scoring journals and scorer trace persistence.
No editor, campaign emulation, recovery or hidden retries are introduced here.
"""
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evolution.tree import RunIdentityV1
from evolution.tree.execution.interaction import (
    CandidateView, Environment, EvalCase, Scorer, TargetInstance,
)
from evolution_lab._outcomes import evaluation_outcome, validate_coverage_threshold
from evolution_lab._fixed_execution import prepare_fixed, run_fixed


async def evaluate_fixed(
    *,
    run_directory: Path,
    run_id: str,
    identity: RunIdentityV1,
    candidate: CandidateView,
    cases: Sequence[EvalCase],
    instance: TargetInstance,
    environment: Environment,
    scorer: Scorer,
    dispatch_guard: Callable[[], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    evaluation_budget: Mapping[str, Any] | None = None,
    min_coverage: float = 1.0,
) -> dict[str, Any]:
    """Execute the pinned sequential schedule once; report partial coverage.

    An existing directory is refused by core, never resumed automatically.
    Persistence or candidate-integrity failures propagate instead of producing
    a report that could conceal invalid evidence. The primitive is currently
    sequential, so concurrent identities are refused rather than misreported.
    ``evaluation_budget`` (``max_failed_turns``, ``call_retries``) is the
    budget beyond core identity; ``None`` keeps core's defaults.
    """
    min_coverage = validate_coverage_threshold(min_coverage)
    measurement = prepare_fixed(
        run_directory=run_directory,
        run_id=run_id,
        identity=identity,
        candidate=candidate,
        cases=cases,
        instance=instance,
        environment=environment,
        scorer=scorer,
        dispatch_guard=dispatch_guard,
        cancelled=cancelled,
        evaluation_budget=evaluation_budget,
    )
    with evaluation_outcome(run_directory, min_coverage=min_coverage) as lab_outcome, measurement.journal:
        result = await run_fixed(measurement)
        result["min_coverage"] = min_coverage
        lab_outcome.update(run_id=run_id, **{key: result[key] for key in (
            "coverage", "unmeasured", "stopped_reason", "min_coverage")})
        from evolution_lab.owner.measurements import unanswered_count
        root = Path(run_directory)
        unanswered = unanswered_count(result["core"].get("samples") or [], root,
                                         root / "events.jsonl", result["core"]["run_id"])
        result.update(unanswered)
        return result
