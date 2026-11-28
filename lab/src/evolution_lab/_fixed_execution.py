"""One diagnostics-free fixed measurement engine shared by Lab entrypoints."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from evolution.tree import (
    Journal,
    RunIdentityV1,
    declare_instance_components,
    durable_rollout,
    durable_score,
    project_result,
)
from evolution.tree.execution.interaction import (
    CallRetries,
    CandidateIntegrityError,
    CandidateView,
    Environment,
    EvalCase,
    RolloutBudget,
    RolloutSample,
    Scorer,
    SemanticIdentity,
    TargetInstance,
)
from evolution.tree.execution.journal_schema import try_is_retryable
from evolution.tree.execution.sample_outcomes import disposition_for
from evolution.tree.execution.journal_identity import corpus_digest, scorer_binding

from evolution_lab.budget import BudgetExhausted
from evolution_lab.preparation.models import parse_max_consecutive_unmeasured


def normalize_metric_plan(metric_plan: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    metric_bytes = json.dumps(
        metric_plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return json.loads(metric_bytes), metric_bytes


def build_fixed_identity(
    *, candidate_digest: str, target: SemanticIdentity, environment: Environment,
    scorer: Scorer, evaluation_set_id: str, cases: Sequence[EvalCase],
    repeat_seeds: Sequence[int | None], budget: RolloutBudget,
    metric_plan: dict[str, Any], declared_corpus_digest: str | None = None,
    concurrency: int = 1, scorer_concurrency: int = 1,
    sample_retries: int | None = None,
) -> tuple[RunIdentityV1, dict[str, Any]]:
    """Build the fixed identity shape used by both execution entrypoints."""
    normalized_metric, metric_bytes = normalize_metric_plan(metric_plan)
    payload = {
        "profile": "rollout_measurement",
        "row_provenance": 1,
        "candidate_digest": candidate_digest,
        "target": asdict(target),
        "environment": asdict(environment.identity),
        "scorer": scorer_binding(scorer),
        "evaluation_set_id": evaluation_set_id,
        "corpus_digest": (
            corpus_digest(cases)
            if declared_corpus_digest is None else declared_corpus_digest
        ),
        "metric_plan_digest": hashlib.sha256(metric_bytes).hexdigest(),
        "schedule": [
            {
                "case_id": case.case_id,
                "sample_id": f"case-{case_index}-repeat-{repeat}",
                "repeat": repeat,
                "requested_seed": seed,
            }
            for case_index, case in enumerate(cases, 1)
            for repeat, seed in enumerate(repeat_seeds, 1)
        ],
        "budget": {
            "max_turns": budget.max_turns,
            "timeout_seconds": budget.timeout_seconds,
        },
        "max_concurrency": concurrency,
        "max_scorer_concurrency": scorer_concurrency,
        "materialization_strategy": "artifact_root_injection",
    }
    if sample_retries is not None:
        payload["sample_retries"] = sample_retries
    identity = RunIdentityV1(payload)
    identity.validate_corpus(cases)
    return identity, normalized_metric


@dataclass(frozen=True, slots=True)
class FixedMeasurement:
    journal: Journal
    identity: RunIdentityV1
    candidate: CandidateView
    cases: Sequence[EvalCase]
    instance: TargetInstance
    environment: Environment
    scorer: Scorer
    budget: RolloutBudget
    target: SemanticIdentity
    components_digest: str | None
    schedule: tuple[tuple[EvalCase, RolloutSample], ...]
    dispatch_guard: Callable[[], None] | None
    cancelled: Callable[[], bool]
    max_consecutive_unmeasured: int | str


def prepare_fixed(
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
    max_consecutive_unmeasured: int | str | None = None,
) -> FixedMeasurement:
    """Validate all bindings and create the core-owned measurement journal."""
    max_consecutive_unmeasured = parse_max_consecutive_unmeasured(max_consecutive_unmeasured, "prepare")
    candidate.verify()
    payload = identity.to_payload()
    by_id = identity.validate_corpus(cases)
    if payload["candidate_digest"] != candidate.digest:
        raise ValueError("fixed candidate differs from evaluation identity")
    target = SemanticIdentity(**payload["target"])
    # ADR-0014: the budget beyond core identity is Lab's; this is the one place
    # its JSON becomes core's typed CallRetries.
    extra: dict[str, Any] = {}
    if evaluation_budget is not None:
        retries = evaluation_budget["call_retries"]
        extra = {"max_failed_turns": evaluation_budget["max_failed_turns"],
                 "call_retries": None if retries is None else CallRetries(**retries)}
    budget = RolloutBudget(**payload["budget"], **extra, cancelled=cancelled)
    schedule = []
    for entry in payload["schedule"]:
        case = by_id[entry["case_id"]]
        sample = RolloutSample(
            entry["sample_id"], candidate.digest, entry["requested_seed"]
        )
        identity.validate_call(
            case=case,
            sample=sample,
            target=target,
            environment=environment.identity,
            scorer=scorer,
            budget=budget,
            corpus=cases,
        )
        schedule.append((case, sample))
    journal = Journal.create(run_directory, run_id, identity, cases)
    try:
        components_digest = declare_instance_components(journal, instance)
    except BaseException:
        journal.close()
        raise
    return FixedMeasurement(
        journal, identity, candidate, cases, instance, environment, scorer, budget,
        target, components_digest, tuple(schedule), dispatch_guard, cancelled,
        max_consecutive_unmeasured,
    )


async def run_fixed(measurement: FixedMeasurement) -> dict[str, Any]:
    """Run one prepared journal; the caller owns its surrounding diagnostics."""
    stopped_reason = None
    unmeasured = []
    consecutive_unmeasured = 0
    payload = measurement.identity.to_payload()
    sample_retries = payload.get("sample_retries")
    rollout_slots = asyncio.Semaphore(payload["max_concurrency"])
    scorer_slots = asyncio.Semaphore(payload["max_scorer_concurrency"])

    def record_unmeasured(case: EvalCase, sample: RolloutSample, outcome: str) -> None:
        nonlocal stopped_reason, consecutive_unmeasured
        unmeasured.append({
            "case_id": case.case_id, "sample_id": sample.sample_id,
            "outcome": outcome,
            "tries": len(measurement.journal.sample_tries(case.case_id, sample.sample_id)),
        })
        consecutive_unmeasured += 1
        limit = measurement.max_consecutive_unmeasured
        if limit != "off" and consecutive_unmeasured >= limit:
            stopped_reason = stopped_reason or "consecutive_unmeasured"

    async def run_sample(case: EvalCase, sample: RolloutSample) -> None:
        nonlocal stopped_reason, consecutive_unmeasured
        pending_outcome = None
        for attempt in range((sample_retries or 0) + 1):
            retry_ordinal = None if sample_retries is None else attempt
            candidate = measurement.candidate
            failed = False
            dispatched = False
            try:
                async with rollout_slots:
                    # Waiters (including retries) must recheck the shared stop
                    # after acquiring a slot, before starting another rollout.
                    if stopped_reason is not None:
                        if pending_outcome is not None:
                            record_unmeasured(case, sample, pending_outcome)
                        return
                    candidate.verify()
                    if measurement.dispatch_guard is not None:
                        measurement.dispatch_guard()
                    if measurement.cancelled():
                        stopped_reason = "cancelled"
                        if pending_outcome is not None:
                            record_unmeasured(case, sample, pending_outcome)
                        return
                    dispatched = True
                    trajectory = await durable_rollout(
                        journal=measurement.journal,
                        identity=measurement.identity,
                        corpus=measurement.cases,
                        case=case,
                        sample=sample,
                        instance=measurement.instance,
                        environment=measurement.environment,
                        scorer=measurement.scorer,
                        budget=measurement.budget,
                        expected_target=measurement.target,
                        components_digest=measurement.components_digest,
                        retry_ordinal=retry_ordinal,
                    )
                if trajectory.disposition.kind == "score":
                    # Scoring does not occupy a target session. Already-started
                    # samples finish even when a different sample stops dispatch.
                    async with scorer_slots:
                        if measurement.dispatch_guard is not None:
                            measurement.dispatch_guard()
                        if measurement.cancelled():
                            stopped_reason = "cancelled"
                        else:
                            await durable_score(
                                measurement.journal, measurement.scorer, case, trajectory,
                                candidate, retry_ordinal=retry_ordinal,
                            )
            except CandidateIntegrityError:
                raise
            except BudgetExhausted:
                stopped_reason = "budget_exhausted"
            except asyncio.CancelledError:
                stopped_reason = "cancelled"
                raise
            except Exception:
                failed = True

            if not dispatched and pending_outcome is not None:
                # A global stop while a retry was queued leaves its preceding
                # closed try unmeasured; it does not manufacture another try.
                stopped_reason = stopped_reason or "core_failed"
                record_unmeasured(case, sample, pending_outcome)
                return

            # A poisoned journal also refuses this public read: propagate that
            # global failure rather than inventing a partial Core projection.
            execution, scoring = measurement.journal.sample_records(case.case_id, sample.sample_id)
            # A refused retry must not reuse the preceding try's closed outcome.
            current = execution is not None and execution.payload.get("retry_ordinal") == retry_ordinal
            closed = current and execution.event != "rollout_started"
            if not closed:
                stopped_reason = stopped_reason or "core_failed"
                break
            outcome = execution.payload["outcome"]
            kind = disposition_for(outcome).kind
            scoring_failed = scoring is not None and scoring.event in {
                "scoring_failed", "scoring_interrupted",
            }
            if kind == "exclude":
                stopped_reason = stopped_reason or outcome
            elif failed and kind != "indeterminate" and not scoring_failed:
                stopped_reason = stopped_reason or "core_failed"
            if scoring is not None and scoring.event == "scoring_finished":
                consecutive_unmeasured = 0
                break
            pending_outcome = "scorer_failed" if scoring_failed else outcome
            if (stopped_reason is None and attempt < (sample_retries or 0)
                    and try_is_retryable(execution, scoring)):
                continue
            record_unmeasured(case, sample, pending_outcome)
            break

    async def dispatch(case: EvalCase, sample: RolloutSample) -> None:
        nonlocal stopped_reason
        try:
            await run_sample(case, sample)
        except BaseException:
            # Candidate integrity and poisoned-journal failures must prevent
            # queued samples from starting, then propagate after active work ends.
            stopped_reason = stopped_reason or "core_failed"
            raise

    if payload["max_concurrency"] == payload["max_scorer_concurrency"] == 1:
        # Preserve the complete dev59 journal event order at legacy defaults.
        for case, sample in measurement.schedule:
            await dispatch(case, sample)
            if stopped_reason is not None:
                break
    else:
        # Tasks enter the FIFO semaphore wait queue in schedule order. Completion
        # updates above have no await, so the counter follows completion order.
        tasks = [asyncio.create_task(dispatch(case, sample))
                 for case, sample in measurement.schedule]
        cancelled_error = None
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError as error:
            stopped_reason = "cancelled"
            cancelled_error = error
            # gather forwards cancellation to active rollouts/scorers; wait for
            # their Core-owned closure and cleanup before propagating cancellation.
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, asyncio.CancelledError):
                stopped_reason = "cancelled"
                cancelled_error = cancelled_error or outcome
            elif isinstance(outcome, BaseException):
                raise outcome
        if cancelled_error is not None:
            raise cancelled_error
    measurement.candidate.verify()
    projection = project_result(measurement.journal.snapshot()).to_payload()
    return {
        "core": projection,
        "coverage": {
            "expected": len(measurement.schedule),
            "scored": sum(
                sample["scoring_state"] == "finished"
                for sample in projection["samples"]
            ),
        },
        "unmeasured": unmeasured,
        "stopped_reason": stopped_reason,
    }


async def measure_fixed(*, min_coverage=1.0, **kwargs) -> dict[str, Any]:
    """Prepare and run without adding Lab diagnostics around core evidence."""
    from evolution_lab._outcomes import validate_coverage_threshold
    min_coverage = validate_coverage_threshold(min_coverage)
    measurement = prepare_fixed(**kwargs)
    with measurement.journal:
        result = await run_fixed(measurement)
        result["min_coverage"] = min_coverage
        return result
