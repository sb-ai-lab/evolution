"""Shared readiness contract for Table 3 framework adapters.

The pre-full-run gate is intentionally offline: it verifies that every
framework can be addressed through the same fixed-TraceSpec contract before
any API-backed run. Actual produce plans must then pass
``validate_produce_plan`` before they can be evaluated or reported.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_FRAMEWORKS: tuple[str, ...] = ("evolution", "memp", "hermes", "memento")
REQUIRED_PRODUCE_FIELDS: tuple[str, ...] = (
    "schema",
    "status",
    "valid_for_eval",
    "trace_spec_hash",
    "n_traces_used",
    "trace_budget",
    "source_model_ids",
    "used_openrouter",
    "base_seed",
    "records",
    "health",
)
REQUIRED_PRODUCE_SCHEMA = "table3-framework-produce-v2"


@dataclass(frozen=True)
class FrameworkContract:
    framework: str
    module: str
    produce_symbol: str
    plan_class: str
    fixed_trace_driver: str
    required_paths: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    isolation_roots: tuple[str, ...] = ()


@dataclass
class FrameworkReadiness:
    framework: str
    ok: bool = False
    module: str = ""
    produce_symbol: str = ""
    plan_class: str = ""
    fixed_trace_driver: str = ""
    issues: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


def framework_contracts(workspace_root: Path, artifacts: Path) -> dict[str, FrameworkContract]:
    """Return the explicit framework-to-adapter routing."""
    return {
        "evolution": FrameworkContract(
            framework="evolution",
            module="evolution.adapters.evolution_adapter",
            produce_symbol="produce_with_trace_spec",
            plan_class="ProducePlan",
            fixed_trace_driver="native_evolution_produce_with_trace_spec",
            isolation_roots=(str(artifacts / "adapter_readiness_isolation" / "evolution"),),
        ),
        "memp": FrameworkContract(
            framework="memp",
            module="evolution.adapters.memp_adapter",
            produce_symbol="produce",
            plan_class="MempProducePlan",
            fixed_trace_driver="table3_trace_driver_calls_memp_run_episode_per_trace",
            required_paths=(str(workspace_root / "MemP" / "ProcedureMem"),),
            isolation_roots=(str(artifacts / "adapter_readiness_isolation" / "memp_home"),),
        ),
        "hermes": FrameworkContract(
            framework="hermes",
            module="evolution.adapters.hermes_adapter",
            produce_symbol="produce",  # Table-3 produce-once; iterative loop uses edit_skill_from_traces
            plan_class="HermesProducePlan",
            fixed_trace_driver="table3_trace_driver_calls_hermes_agent_per_trace",
            required_paths=(str(workspace_root / "hermes-agent" / "run_agent.py"),),
            isolation_roots=(str(artifacts / "adapter_readiness_isolation" / "hermes_home"),),
        ),
        "memento": FrameworkContract(
            framework="memento",
            module="evolution.adapters.memento_adapter",
            produce_symbol="produce",
            plan_class="MementoProducePlan",
            fixed_trace_driver="table3_trace_driver_calls_memento_agent_per_trace",
            required_paths=(str(workspace_root / "Memento-Skills" / "pyproject.toml"),),
            required_binaries=("memento",),
            isolation_roots=(str(artifacts / "adapter_readiness_isolation" / "memento_runs"),),
        ),
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def trace_spec_source_model_ids(trace_payload: dict[str, Any]) -> list[str]:
    ids = {
        str(item.get("source_model_id"))
        for item in trace_payload.get("trace_spec", [])
        if item.get("source_model_id")
    }
    return sorted(ids)


def trace_spec_task_ids(trace_payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("task_id"))
        for item in trace_payload.get("trace_spec", [])
        if item.get("task_id")
    ]


def validate_trace_spec_payload(trace_payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    budget = trace_payload.get("trace_budget")
    specs = trace_payload.get("trace_spec")
    if not isinstance(budget, int) or budget <= 0:
        issues.append(f"trace_budget invalid: {budget!r}")
    if not isinstance(specs, list):
        issues.append("trace_spec must be a list")
        return issues
    if isinstance(budget, int) and len(specs) != budget:
        issues.append(f"trace_spec length {len(specs)} != trace_budget {budget}")
    for idx, item in enumerate(specs):
        if not isinstance(item, dict):
            issues.append(f"trace_spec[{idx}] is not an object")
            continue
        for key in ("task_id", "source_model_id", "attempt_seed"):
            if key not in item:
                issues.append(f"trace_spec[{idx}] missing {key}")
    return issues


def validate_eval_plan_for_genr(eval_units: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for idx, unit in enumerate(eval_units):
        if unit.get("metric") != "genr":
            continue
        if unit.get("source_artifact_cell_id") != unit.get("source_cell_id"):
            issues.append(
                f"eval_plan[{idx}] GenR artifact cell {unit.get('source_artifact_cell_id')!r} "
                f"!= source cell {unit.get('source_cell_id')!r}"
            )
        if unit.get("target_cell_id") == unit.get("source_artifact_cell_id"):
            issues.append(f"eval_plan[{idx}] GenR target equals artifact/source cell")
    return issues


def heldout_in_sources(source_model_ids: list[str], heldout_model_id: str) -> bool:
    h = heldout_model_id.lower()
    return any(h in m.lower() for m in source_model_ids)


def validate_produce_plan(
    plan: dict[str, Any],
    *,
    trace_spec_hash: str,
    trace_budget: int,
    heldout_model_id: str,
) -> list[str]:
    """Validate one real produce plan before eval/reporting.

    This intentionally rejects partial, stale, skipped, and no-op artifacts.
    A framework may honestly produce no useful memory, but that outcome must
    be represented as a completed plan with full trace coverage and explicit
    health counters, not as a missing or skipped artifact.
    """
    issues: list[str] = []
    for key in REQUIRED_PRODUCE_FIELDS:
        if key not in plan:
            issues.append(f"missing required field: {key}")
    if issues:
        return issues

    if plan.get("schema") != REQUIRED_PRODUCE_SCHEMA:
        issues.append(f"schema must be {REQUIRED_PRODUCE_SCHEMA!r}, got {plan.get('schema')!r}")
    if plan.get("status") != "completed":
        issues.append(f"status must be completed, got {plan.get('status')!r}")
    if plan.get("valid_for_eval") is not True:
        issues.append("valid_for_eval must be true")
    if plan.get("trace_spec_hash") != trace_spec_hash:
        issues.append("trace_spec_hash does not match frozen trace spec")
    if plan.get("trace_budget") != trace_budget:
        issues.append(f"trace_budget {plan.get('trace_budget')!r} != expected {trace_budget}")
    if plan.get("n_traces_used") != trace_budget:
        issues.append(f"n_traces_used {plan.get('n_traces_used')!r} != trace_budget {trace_budget}")
    if plan.get("used_openrouter") not in (True, False):
        issues.append("used_openrouter must be boolean")

    source_ids = plan.get("source_model_ids")
    if not isinstance(source_ids, list) or not source_ids:
        issues.append("source_model_ids must be a non-empty list")
    elif heldout_in_sources([str(x) for x in source_ids], heldout_model_id):
        issues.append("held-out GenM model appears in source_model_ids")

    base_seed = plan.get("base_seed")
    if not isinstance(base_seed, dict):
        issues.append("base_seed must be an object")
    else:
        if base_seed.get("policy") != "source_trace_task_skill_union_v1":
            issues.append(f"base_seed.policy invalid: {base_seed.get('policy')!r}")
        source_task_ids = base_seed.get("seed_source_task_ids")
        if not isinstance(source_task_ids, list) or not source_task_ids:
            issues.append("base_seed.seed_source_task_ids must be a non-empty list")
        names = base_seed.get("base_skill_names")
        hashes = base_seed.get("base_skill_hashes")
        if not isinstance(names, list) or not names:
            issues.append("base_seed.base_skill_names must be a non-empty list")
            names = []
        if not isinstance(hashes, dict) or not hashes:
            issues.append("base_seed.base_skill_hashes must be a non-empty object")
            hashes = {}
        if isinstance(names, list) and isinstance(hashes, dict):
            name_set = {str(n) for n in names}
            hash_set = {str(k) for k in hashes}
            if name_set != hash_set:
                issues.append("base_seed names and hash keys differ")
            for skill, digest in hashes.items():
                if not isinstance(digest, str) or len(digest) != 64:
                    issues.append(f"base_seed hash for {skill!r} is not a sha256 hex digest")

    records = plan.get("records")
    if not isinstance(records, list):
        issues.append("records must be a list")
    elif len(records) != trace_budget:
        issues.append(f"records length {len(records)} != trace_budget {trace_budget}")
    else:
        n_completed = sum(
            1
            for rec in records
            if isinstance(rec, dict)
            and rec.get("solve_status") == "completed"
            and rec.get("pytest_status") == "completed"
        )
        for idx, rec in enumerate(records):
            if not isinstance(rec, dict):
                issues.append(f"records[{idx}] is not an object")
                continue
            for key in ("task_id", "source_model_id", "solve_status", "pytest_status"):
                if key not in rec:
                    issues.append(f"records[{idx}] missing {key}")
            if rec.get("solve_status") in ("skipped", "missing"):
                issues.append(
                    f"records[{idx}] has invalid solve_status={rec.get('solve_status')!r}"
                )
            elif rec.get("solve_status") == "hard_fail" and n_completed == 0:
                issues.append(
                    f"records[{idx}] has invalid solve_status='hard_fail' and no other trace completed"
                )
            if rec.get("pytest_status") == "missing":
                issues.append(f"records[{idx}] has invalid pytest_status='missing'")
            elif rec.get("pytest_status") == "not_run" and (
                rec.get("solve_status") != "hard_fail" or n_completed == 0
            ):
                issues.append(f"records[{idx}] has invalid pytest_status='not_run'")

    health = plan.get("health")
    if not isinstance(health, dict):
        issues.append("health must be an object")
    else:
        if health.get("persistence_ok") is not True:
            issues.append("health.persistence_ok must be true")
        if health.get("base_seed_ok") is not True:
            issues.append("health.base_seed_ok must be true")
        if health.get("eval_writes_blocked") is not True:
            issues.append("health.eval_writes_blocked must be true")
        if health.get("skipped_or_noop_labelled") is not True:
            issues.append("health.skipped_or_noop_labelled must be true")
        if health.get("mixed_feedback_policy") is True:
            issues.append("health.mixed_feedback_policy must not be true")

    if not isinstance(plan.get("feedback_policy"), str) or not plan.get("feedback_policy"):
        issues.append("feedback_policy must be a non-empty string")
    if plan.get("oracle_visible") not in (True, False):
        issues.append("oracle_visible must be boolean")
    if not isinstance(plan.get("report_group_key"), str) or not plan.get("report_group_key"):
        issues.append("report_group_key must be a non-empty string")

    if plan.get("framework") == "evolution":
        evo_plan = (plan.get("state") or {}).get("evolution_plan") or {}
        summary = evo_plan.get("evolve_summary") or {}
        if int(summary.get("error", 0) or 0) > 0:
            issues.append(f"evolution evolve_summary has errors: {summary}")
        train = evo_plan.get("train_solve_summary") or []
        verifier_failures = [
            item
            for item in train
            if item.get("success") is False and int(item.get("total_tests", 0) or 0) > 0
        ]
        # A leak-rejected / no-change round (clean reflect→rewrite→commit
        # but the version did not bump) is an honest no-op: the skill
        # stays at a valid prior version and the seed chain continues
        # from it. That is NOT a "produced no evolved skill version"
        # violation. ``no_failure_signal`` is deliberately NOT treated as
        # benign here: if the adapter claims no failure signal while the
        # verifier shows real failures, that contradiction must still be
        # flagged (research integrity — no silent no-op past failures).
        benign_no_op = int(summary.get("no_op", 0) or 0) > 0
        # δ-gated outcomes (paper §4.2) are honest mechanism results that keep
        # the seed chain valid: PROMOTE produced a new active version;
        # ROLLBACK / ROLLBACK_NO_VALIDATION ran the mechanism and kept the
        # parent (same validity class as no_op). ``no_failure_signal`` stays
        # NON-benign on purpose (claiming no failure while the verifier shows
        # real failures is a contradiction that must still be flagged).
        gate_progress = (
            int(summary.get("promoted", 0) or 0) > 0
            or int(summary.get("rolled_back", 0) or 0) > 0
            or int(summary.get("rolled_back_no_validation", 0) or 0) > 0
        )
        if (
            verifier_failures
            and int(summary.get("evolved", 0) or 0) <= 0
            and not benign_no_op
            and not gate_progress
        ):
            issues.append(
                "evolution has verifier-covered failed source traces but produced no evolved skill version"
            )

    return issues


def check_framework(contract: FrameworkContract) -> FrameworkReadiness:
    result = FrameworkReadiness(
        framework=contract.framework,
        module=contract.module,
        produce_symbol=contract.produce_symbol,
        plan_class=contract.plan_class,
        fixed_trace_driver=contract.fixed_trace_driver,
    )

    try:
        module = importlib.import_module(contract.module)
    except Exception as exc:
        result.issues.append(f"import failed: {type(exc).__name__}: {exc}")
        return result
    result.checks["module_imported"] = True

    produce = getattr(module, contract.produce_symbol, None)
    if not callable(produce):
        result.issues.append(f"missing callable {contract.produce_symbol}")
    else:
        result.checks["produce_callable"] = True

    plan_cls = getattr(module, contract.plan_class, None)
    if plan_cls is None:
        result.issues.append(f"missing plan class {contract.plan_class}")
    else:
        try:
            plan_obj = plan_cls()
            if hasattr(plan_obj, "to_jsonable"):
                plan_json = plan_obj.to_jsonable()
            elif hasattr(plan_obj, "__dict__"):
                plan_json = dict(plan_obj.__dict__)
            else:
                plan_json = {}
            result.checks["plan_class_instantiates"] = True
            result.checks["plan_framework"] = plan_json.get("framework", contract.framework)
        except Exception as exc:
            result.issues.append(f"plan class instantiate failed: {type(exc).__name__}: {exc}")

    missing_paths = [p for p in contract.required_paths if not Path(p).exists()]
    if missing_paths:
        result.issues.append(f"missing required paths: {missing_paths}")
    result.checks["required_paths"] = {p: Path(p).exists() for p in contract.required_paths}

    missing_bins = [b for b in contract.required_binaries if shutil.which(b) is None]
    if missing_bins:
        result.issues.append(f"missing required binaries on PATH: {missing_bins}")
    result.checks["required_binaries"] = {b: shutil.which(b) for b in contract.required_binaries}

    for root in contract.isolation_roots:
        p = Path(root)
        if str(p).startswith(str(Path.home())) and "adapter_readiness_isolation" not in str(p):
            result.issues.append(f"isolation root is too broad: {p}")
    result.checks["isolation_roots"] = list(contract.isolation_roots)

    result.ok = not result.issues
    return result


__all__ = [
    "FrameworkContract",
    "FrameworkReadiness",
    "REQUIRED_FRAMEWORKS",
    "REQUIRED_PRODUCE_FIELDS",
    "REQUIRED_PRODUCE_SCHEMA",
    "check_framework",
    "framework_contracts",
    "heldout_in_sources",
    "load_json",
    "sha256_file",
    "trace_spec_source_model_ids",
    "trace_spec_task_ids",
    "validate_eval_plan_for_genr",
    "validate_produce_plan",
    "validate_trace_spec_payload",
]
