"""Experimental Python entry point for evaluation through an existing v2 target.

Domain integration supplies a target binding, cases, environment and scorer.
Lab assembles copied artifacts, identity, schedule and release through public
core APIs. This is a one-shot operation, not a serialized recipe or permission
token; the caller owns target/gateway processes and provider authorization.
"""
from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactStore, ExpectedAttestation
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.execution.journal_identity import corpus_digest
from evolution.tree.target.config import TargetBinding
from evolution.tree.execution.interaction import Environment, EvalCase, RolloutBudget, Scorer
from evolution.tree.execution.wire_v2 import new_instance_id

from evolution_lab.evaluation import evaluate_fixed
from evolution_lab._fixed_execution import build_fixed_identity, normalize_metric_plan
from evolution_lab._outcomes import evaluation_outcome
from evolution_lab.provenance import WheelPin


async def evaluate_target(
    *, source_root: Path, policy: ArtifactPolicyV1, target: TargetBinding,
    run_directory: Path, run_id: str, evaluation_set_id: str,
    cases: Sequence[EvalCase], environment: Environment, scorer: Scorer,
    repeat_seeds: Sequence[int | None], budget: RolloutBudget,
    metric_plan: Mapping[str, Any], wheels: Sequence[WheelPin],
    dispatch_guard: Callable[[], None],
    sample_retries: int | None = None,
    concurrency: int = 1, scorer_concurrency: int = 1,
) -> dict[str, Any]:
    """Evaluate every case for each explicit repeat seed, sequentially.

    ``None`` is required for seed_mode none/unpaired; exact uses explicit ints.
    The guard must enforce the caller's conditions; it is not itself spending
    authorization. The provider gateway must guard each actual model request.
    No editor settings are accepted. Existing output roots are never resumed.
    Release failures propagate: a success response never hides failed cleanup.
    """
    source_root = Path(source_root).resolve(strict=True)
    instance_id = new_instance_id(run_id)
    repeat_seeds = tuple(repeat_seeds)
    run_directory = Path(run_directory).absolute()
    if run_directory.exists() or run_directory.is_symlink():
        raise FileExistsError(run_directory)
    resolved = run_directory.resolve()
    if resolved.is_relative_to(source_root) or source_root.is_relative_to(resolved):
        raise ValueError("source and evaluation output must be separate")
    if not repeat_seeds or any(seed is not None and type(seed) is not int for seed in repeat_seeds):
        raise ValueError("declare at least one repeat seed")
    if any((seed is None) == (target.seed_mode == "exact") for seed in repeat_seeds):
        raise ValueError("repeat seeds must match target seed mode")
    wheels = tuple(wheels)
    if not any(pin.name.lower().replace("_", "-") == "evo-skills" for pin in wheels):
        raise ValueError("exact core wheel is required")
    for pin in wheels:
        pin.verify()
    cases = tuple(cases)
    case_digest = corpus_digest(cases)
    if not metric_plan:
        raise ValueError("metric plan must be declared")
    metric_plan, _ = normalize_metric_plan(dict(metric_plan))
    scan = scan_tree(source_root, policy)
    expected = ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)
    adapter = HttpSessionAdapter(target, policy, run_id=run_id)
    dispatch_guard()
    preflight = await adapter.preflight(expected)
    if not resolved.is_relative_to(adapter.allowed_artifact_root.resolve()):
        raise ValueError("evaluation output must be inside target allowed artifact root")
    identity, metric_plan = build_fixed_identity(
        candidate_digest=scan.candidate_digest,
        target=preflight.semantic,
        environment=environment,
        scorer=scorer,
        evaluation_set_id=evaluation_set_id,
        cases=cases,
        repeat_seeds=repeat_seeds,
        budget=budget,
        metric_plan=metric_plan,
        declared_corpus_digest=case_digest,
        sample_retries=sample_retries,
        concurrency=concurrency,
        scorer_concurrency=scorer_concurrency,
    )
    applied = _evaluation_budget(budget)
    dispatch_guard()
    run_directory.mkdir(mode=0o700)
    with evaluation_outcome(run_directory) as lab_outcome:
        # PDR-0014: the applied budget is recorded before execution starts.
        provenance = {"distributions": [pin.public() for pin in wheels],
                      "identity": identity.to_payload(), "metric_plan": dict(metric_plan),
                      "evaluation_budget": applied}
        metadata = run_directory / "evaluation-inputs.json"
        metadata.touch(mode=0o600, exist_ok=False)
        metadata.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")
        with ArtifactStore.create(
            run_directory / "artifacts", source_root, policy, parallel_attempts=1,
            expected=ArtifactStoreIdentityV1(policy, scan.candidate_digest, scan.dependency_digest),
        ) as store:
            artifact = store.champion()
            # Even a lost materialization response may have created this instance.
            try:
                dispatch_guard()
                instance = await adapter.materialize(artifact, "artifact_root_injection", instance_id)
                result = await evaluate_fixed(
                    run_directory=run_directory / "measurement", run_id=run_id, identity=identity,
                    candidate=artifact.candidate_view(), cases=cases, instance=instance,
                    environment=environment, scorer=scorer,
                    dispatch_guard=dispatch_guard, cancelled=budget.cancelled,
                    evaluation_budget=applied,
                )
            finally:
                await adapter.release(instance_id)
            for pin in wheels:
                pin.verify()
        lab_outcome.update(run_id=run_id, stopped_reason=result["stopped_reason"],
                           evaluation_budget=applied)
        return {**result, "evaluation_budget": applied}


def _evaluation_budget(budget: RolloutBudget) -> dict[str, Any]:
    """The caller's budget beyond core identity, in prepare_fixed's JSON shape."""
    retries = budget.call_retries
    return {"max_failed_turns": budget.max_failed_turns,
            "call_retries": None if retries is None else {
                "max_attempts": retries.max_attempts,
                "turn_max_attempts": retries.turn_max_attempts}}
