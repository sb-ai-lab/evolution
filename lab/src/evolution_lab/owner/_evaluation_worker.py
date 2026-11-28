"""Installed S3 fixed-evaluation worker over public core measurement APIs."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import threading

from evolution.tree import TargetCallLedger, load_manifest_v2
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactStore, ExpectedAttestation
from evolution.tree.execution.corpus import load_eval_corpus
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.execution.interaction import RolloutBudget
from evolution.tree.execution.wire_v2 import new_instance_id

from evolution_lab._fixed_execution import build_fixed_identity, measure_fixed
from evolution_lab._outcomes import MAX_EVALUATION_RESULT_BYTES
from evolution_lab._manifest_schema import campaign_cases
from evolution_lab.preparation.evaluation import (
    bind_campaign_evaluation,
    bind_evaluation_input,
)
from evolution_lab.preparation.storage import PreparationStorage

from .binding import _thaw, open_prepared
from .execution import EVALUATION_OUTCOME_SCHEMA, validate_evaluation_worker_config
from .models import Failure
from .protocol import MAX_CONFIG_BYTES, read_frame
from .receipts import publish_json
from .settlement import EVALUATION_SETTLEMENT_SCHEMA, ledger_payload, settle_timeout
from ._worker_stderr import exception_record, exception_redactions, write_exception


def _bindings(config, environ):
    prepared = open_prepared(
        PreparationStorage(config["state_directory"]), config["prepared_digest"]
    )
    if (
        prepared.kind != "evaluation"
        or prepared.evaluation is None
        or prepared.bundle.bundle_digest != config["bundle_digest"]
    ):
        raise ValueError("evaluation preparation identity mismatch")
    binding = prepared.bundle
    manifest = binding.manifest
    measurement_directory = Path(config["run_directory"]) / "measurement"
    if binding.source_kind == "evaluation":
        value = _thaw(binding.evaluation_input)
        cases = load_eval_corpus(
            binding.entrypoint_path.parent / value["corpus"]["path"]
        )
        environment, scorer, _ = bind_evaluation_input(
            value,
            document=binding.entrypoint_path,
            manifest=manifest,
            env=environ,
            secret_names=(),
            stage="read",
            run_directory=measurement_directory,
        )
    elif binding.source_kind == "campaign":
        if manifest.workflow is None:
            raise ValueError("campaign evaluation has no workflow")
        cases = campaign_cases(manifest)
        environment, scorer, _ = bind_campaign_evaluation(
            manifest,
            bundle_root=binding.bundle_path,
            env=environ,
            secret_names=(),
            stage="read",
            run_directory=measurement_directory,
        )
    else:
        raise ValueError("unsupported evaluation source")
    by_id = {case.case_id: case for case in cases}
    selected = tuple(by_id[case_id] for case_id in prepared.evaluation.case_ids)
    if len(selected) != len(prepared.evaluation.case_ids):
        raise ValueError("evaluation case identity mismatch")
    return prepared, manifest, selected, environment, scorer


def _publish_outcome(config, result, *, budget, measurement_returned: bool = True) -> None:
    try:
        publish_json(config["outcome_path"], {
            "schema": EVALUATION_OUTCOME_SCHEMA,
            "operation_id": config["operation_id"],
            "scope_id": config["scope_id"],
            "run_id": config["run_id"],
            "measurement_returned": measurement_returned,
            "coverage": result["coverage"],
            "stopped_reason": result["stopped_reason"],
            "unmeasured": result["unmeasured"],
            "min_coverage": result["min_coverage"],
            "evaluation_budget": budget,
        }, max_bytes=MAX_EVALUATION_RESULT_BYTES)
    except (OSError, ValueError):
        pass


def _publish_diagnostic(config, stage="running", *, exception=None) -> None:
    try:
        publish_json(config["diagnostic_path"], {
            "schema": "lab-evaluation-diagnostic/2",
            "operation_id": config["operation_id"],
            "scope_id": config["scope_id"],
            "failure": Failure.make("lab_error", stage).to_payload(),
            "exception": exception,
        })
    except (OSError, ValueError):
        pass


async def _evaluate(config, environ, cancelled, ledger, mutation_facts, bound):
    prepared, manifest, cases, environment, scorer = _bindings(config, environ)
    bound["target"] = manifest.target
    bound["redactions"] = exception_redactions(environ)
    plan = prepared.evaluation
    applied = {"max_failed_turns": plan.max_failed_turns,
               "call_retries": None if plan.call_retries is None else _thaw(plan.call_retries)}
    run_directory = Path(config["run_directory"])
    source_root = Path(manifest.seed_root).resolve(strict=True)
    resolved_run = run_directory.resolve()
    if resolved_run.is_relative_to(source_root) or source_root.is_relative_to(resolved_run):
        raise ValueError("source and evaluation output must be separate")
    scan = scan_tree(source_root, manifest.policy)
    expected = ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)
    adapter = HttpSessionAdapter(
        manifest.target, manifest.policy, run_id=config["run_id"], ledger=ledger
    )
    preflight = await adapter.preflight(expected)
    if not resolved_run.is_relative_to(adapter.allowed_artifact_root.resolve()):
        raise ValueError("evaluation output must be inside target allowed artifact root")
    budget = RolloutBudget(
        max_turns=plan.max_turns,
        timeout_seconds=plan.timeout_seconds,
        cancelled=cancelled,
    )
    identity, metric_plan = build_fixed_identity(
        candidate_digest=scan.candidate_digest,
        target=preflight.semantic,
        environment=environment,
        scorer=scorer,
        evaluation_set_id=plan.evaluation_set_id,
        cases=cases,
        repeat_seeds=plan.repeat_seeds,
        budget=budget,
        metric_plan=_thaw(plan.metric_plan),
        concurrency=plan.concurrency,
        scorer_concurrency=plan.scorer_concurrency,
        sample_retries=plan.sample_retries,
    )
    distributions = [
        {
            "name": pin.name,
            "version": pin.version,
            "sha256": pin.wheel_sha256,
        }
        for pin in prepared.bundle.execution_environment.distributions
    ]
    try:
        publish_json(config["inputs_path"], {
            "schema": "lab-evaluation-inputs/1",
            "operation_id": config["operation_id"],
            "scope_id": config["scope_id"],
            "identity": identity.to_payload(),
            "metric_plan": metric_plan,
            "distributions": distributions,
        })
    except (OSError, ValueError):
        pass
    with ArtifactStore.create(
        run_directory / "artifacts",
        source_root,
        manifest.policy,
        parallel_attempts=1,
        expected=ArtifactStoreIdentityV1(
            manifest.policy, scan.candidate_digest, scan.dependency_digest
        ),
    ) as store:
        artifact = store.champion()
        instance_id = new_instance_id(config["run_id"])
        if cancelled():
            result = {"coverage": None, "stopped_reason": "cancelled",
                      "unmeasured": None, "min_coverage": plan.min_coverage}
            _publish_outcome(config, result, budget=applied, measurement_returned=False)
            return result
        mutation_facts["materialize_started"] = True
        try:
            instance = await adapter.materialize(
                artifact, "artifact_root_injection", instance_id
            )
            result = await measure_fixed(
                run_directory=run_directory / "measurement",
                run_id=config["run_id"],
                identity=identity,
                candidate=artifact.candidate_view(),
                cases=cases,
                instance=instance,
                environment=environment,
                scorer=scorer,
                cancelled=cancelled,
                evaluation_budget=applied,
                min_coverage=plan.min_coverage,
                max_consecutive_unmeasured=plan.max_consecutive_unmeasured,
            )
            _publish_outcome(config, result, budget=applied)
            return result
        finally:
            mutation_facts["release_started"] = True
            await adapter.release(instance_id)


async def _evaluate_settled(config, environ, cancelled, mutation_facts, bound=None):
    """Own the operation's ledger and publish settlement before the loop ends.

    Publication happens here, inside the coroutine, because `asyncio.run`
    teardown waits for default-executor threads an abandoned target call may
    still occupy (spec S6 §2.3).
    """
    ledger = TargetCallLedger()
    if bound is None:
        bound = {}
    normal_return = False
    try:
        result = await _evaluate(
            config, environ, cancelled, ledger, mutation_facts, bound
        )
        normal_return = True
        return result
    finally:
        ledger.close()
        target = bound.get("target")
        settled = await ledger.settle(
            settle_timeout(target) if target is not None else 5.0
        )
        try:
            publish_json(config["settlement_path"], {
                "schema": EVALUATION_SETTLEMENT_SCHEMA,
                "scope_id": config["scope_id"],
                "owner_generation": config["owner_generation"],
                "operation_id": config["operation_id"],
                "run_id": config["run_id"],
                "normal_return": normal_return,
                **mutation_facts,
                "ledger": ledger_payload(settled),
            })
        except (OSError, ValueError):
            pass


def run_worker(config, environ, cancelled) -> int:
    """Run one fixed evaluation; the coroutine publishes its own settlement."""
    mutation_facts = {"materialize_started": False, "release_started": False}
    result = None
    bound = {}
    try:
        result = asyncio.run(
            _evaluate_settled(config, environ, cancelled, mutation_facts, bound)
        )
        return 0
    except BaseException as error:
        write_exception(error, bound.get("redactions"))
        _publish_diagnostic(config, exception=exception_record(error))
        return 1
    finally:
        if result is not None:
            try:
                sys.stdout.write(json.dumps({
                    "coverage": result["coverage"],
                    "stopped_reason": result["stopped_reason"],
                    "unmeasured": result["unmeasured"],
                    "min_coverage": result["min_coverage"],
                }) + "\n")
                sys.stdout.flush()
            except OSError:
                pass


def main() -> None:
    chosen = 1
    config = None
    stopping = threading.Event()

    def request_stop(_number, _frame):
        stopping.set()

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, request_stop)
    try:
        raw_fd = os.environ.pop("LAB_EVALUATION_CONFIG_FD")
        config_fd = int(raw_fd)
        os.set_inheritable(config_fd, False)
        environ = dict(os.environ)
        try:
            config = validate_evaluation_worker_config(
                read_frame(config_fd, max_bytes=MAX_CONFIG_BYTES)
            )
        finally:
            os.close(config_fd)
        run = Path(config["run_directory"])
        if run.is_symlink() or not run.is_dir() or any(run.iterdir()):
            raise ValueError("evaluation run directory is not fresh")
        chosen = run_worker(config, environ, stopping.is_set)
    except BaseException as error:
        write_exception(error, None)
        if config is not None:
            _publish_diagnostic(config, "before_start", exception=exception_record(error))
        chosen = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except BaseException:
            pass
    os._exit(chosen)


if __name__ == "__main__":
    main()
