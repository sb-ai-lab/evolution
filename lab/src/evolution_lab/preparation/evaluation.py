"""Closed parsing and public core binding for fixed-evaluation preparation."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from evolution.tree import RunIdentityV1
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.bindings import BindingError, build_environment, build_scorer
from evolution.tree.execution.journal_identity import corpus_digest, scorer_binding
from evolution.tree.execution.config import CommandSection, EnvironmentSection
from evolution.tree.manifest.manifest_v2 import ManifestV2
from evolution.tree.execution.interaction import EvalCase

from evolution_lab._manifest_schema import EVALUATION_MANIFEST_SCHEMA, is_campaign_manifest

from .models import (EvaluationPlan, PreparationError, parse_call_retries, parse_max_consecutive_unmeasured,
                     parse_min_coverage, parse_sample_retries)
from .storage import canonical_json_bytes


_INPUT_KEYS = {
    "schema", "seed_root", "policy", "target", "corpus", "environment", "scorer",
    "evaluation_set_id", "metric_plan", "defaults",
}
_SCORER_KEYS = {
    "kind", "argv", "cwd", "dependency_paths", "allowed_env", "timeout_seconds",
}
_INPUT_SCHEMAS = {"lab-evaluation-input/1", "lab-evaluation-input/2", "lab-evaluation-input/3"}
_DEFAULT_KEYS = {"repeat_seeds", "max_turns", "timeout_seconds"}
_BUDGET_KEYS = {"max_failed_turns", "call_retries"}
_EVALUATION_KEYS = {"sample_retries", "min_coverage", "concurrency", "scorer_concurrency"}


def _invalid(stage: str, *, schema: bool = False) -> None:
    if stage == "bundle":
        raise PreparationError("unsupported_schema" if schema else "invalid_request", stage)
    raise PreparationError("artifacts_invalid", stage)


def _closed(value: Any, keys: set[str], stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _invalid(stage)
    return value


def _positive(value: Any, stage: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        _invalid(stage)
    return value


def _positive_int(value: Any, stage: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _invalid(stage)
    return value


def _string(value: Any, stage: str) -> str:
    if not isinstance(value, str) or not value:
        _invalid(stage)
    return value


def _strings(
    value: Any, stage: str, *, nonempty: bool = False, unique: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid(stage)
    result = tuple(_string(item, stage) for item in value)
    if (nonempty and not result) or (unique and len(set(result)) != len(result)):
        _invalid(stage)
    return result


def finite_json_object(value: Any, stage: str, *, request: bool = False) -> dict[str, Any]:
    def refuse() -> None:
        if request:
            raise PreparationError("invalid_request", stage)
        _invalid(stage)

    if not isinstance(value, Mapping) or not value or any(not isinstance(key, str) for key in value):
        refuse()
    try:
        return json.loads(canonical_json_bytes(dict(value)))
    except PreparationError:
        refuse()


def _decode(path: Path, stage: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes(), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        _invalid(stage)
    return _closed(value, _INPUT_KEYS, stage)


def load_evaluation_input(path: Path, stage: str) -> dict[str, Any]:
    """Load one closed Lab input and validate its non-file semantics."""
    value = _decode(path, stage)
    if value.get("schema") not in _INPUT_SCHEMAS:
        _invalid(stage, schema=True)
    _string(value["seed_root"], stage)
    policy = _closed(
        value["policy"], {"version", "excludes", "dependency_paths", "writable_paths"}, stage,
    )
    try:
        ArtifactPolicyV1.from_payload(policy)
    except (TypeError, ValueError):
        _invalid(stage)
    target = value["target"]
    if not isinstance(target, dict):
        _invalid(stage)
    corpus = _closed(value["corpus"], {"path"}, stage)
    _string(corpus["path"], stage)
    environment = _closed(value["environment"], {"kind", "input_text_key"}, stage)
    kind = environment["kind"]
    if kind == "scripted":
        if environment["input_text_key"] is not None:
            _invalid(stage)
    elif kind == "static":
        _string(environment["input_text_key"], stage)
    else:
        _invalid(stage)
    scorer = _closed(value["scorer"], _SCORER_KEYS, stage)
    if scorer["kind"] != "subprocess":
        _invalid(stage)
    _strings(scorer["argv"], stage, nonempty=True, unique=False)
    _string(scorer["cwd"], stage)
    _strings(scorer["dependency_paths"], stage)
    _strings(scorer["allowed_env"], stage)
    _positive(scorer["timeout_seconds"], stage)
    _string(value["evaluation_set_id"], stage)
    value["metric_plan"] = finite_json_object(value["metric_plan"], stage)
    defaults = value["defaults"]
    allowed = _DEFAULT_KEYS | _EVALUATION_KEYS | (_BUDGET_KEYS if value["schema"] != "lab-evaluation-input/1" else set())
    if value["schema"] == "lab-evaluation-input/3":
        allowed |= {"max_consecutive_unmeasured"}
    if not isinstance(defaults, dict) or not _DEFAULT_KEYS <= set(defaults) <= allowed:
        _invalid(stage)
    seeds = defaults["repeat_seeds"]
    if not isinstance(seeds, list) or not seeds or any(
        seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)) for seed in seeds
    ):
        _invalid(stage)
    seed_mode = target.get("seed_mode")
    if seed_mode == "exact":
        if any(seed is None for seed in seeds):
            _invalid(stage)
    elif seed_mode in {"none", "unpaired"}:
        if any(seed is not None for seed in seeds):
            _invalid(stage)
    else:
        _invalid(stage)
    _positive_int(defaults["max_turns"], stage)
    _positive(defaults["timeout_seconds"], stage)
    parse_max_consecutive_unmeasured(defaults.get("max_consecutive_unmeasured"), stage)
    # A null budget key means "not given"; the default applies (api.py).
    if defaults.get("max_failed_turns") is not None:
        _positive_int(defaults["max_failed_turns"], stage)
    if defaults.get("call_retries") not in (None, "off"):
        try:
            parse_call_retries(defaults["call_retries"], stage)
        except PreparationError:
            _invalid(stage)
    try:
        parse_sample_retries(defaults.get("sample_retries"), stage)
        parse_min_coverage(defaults.get("min_coverage"), stage)
    except PreparationError:
        _invalid(stage)
    for name in ("concurrency", "scorer_concurrency"):
        if defaults.get(name) is not None:
            _positive_int(defaults[name], stage)
    return value


def conformance_payload(value: Mapping[str, Any], *, seed_root: str) -> dict[str, Any]:
    policy = value["policy"]
    return {
        "schema": EVALUATION_MANIFEST_SCHEMA,
        "seed_root": seed_root,
        "target": value["target"],
        "artifacts": {
            "excludes": policy["excludes"],
            "dependency_paths": policy["dependency_paths"],
            "writable_paths": policy["writable_paths"],
        },
    }


def bind_evaluation_input(
    value: Mapping[str, Any], *, document: Path, manifest: ManifestV2,
    env: Mapping[str, str], secret_names: Sequence[str], stage: str,
    run_directory: Path | None = None,
):
    """Use public core builders to validate environment and scorer bindings."""
    section = value["environment"]
    environment = EnvironmentSection(
        kind=section["kind"],
        input_text_key="text" if section["input_text_key"] is None else section["input_text_key"],
    )
    scorer = value["scorer"]
    declared = set(env) | set(secret_names)
    if not set(scorer["allowed_env"]) <= declared:
        _invalid(stage)
    base = document.parent
    command = CommandSection(
        argv=tuple(scorer["argv"]),
        cwd=base / scorer["cwd"],
        dependency_paths=tuple(base / path for path in scorer["dependency_paths"]),
        allowed_env=frozenset(scorer["allowed_env"]),
        timeout_seconds=float(scorer["timeout_seconds"]),
    )
    environ = dict(env)
    environ.update({name: "" for name in secret_names})
    try:
        built_environment = build_environment(environment)
        built_scorer = build_scorer(
            command, seed_root=manifest.seed_root,
            run_dir=(
                document.parent / ".lab-binding"
                if run_directory is None else Path(run_directory)
            ),
            environ=environ,
        )
    except (BindingError, TypeError, ValueError):
        _invalid(stage)
    return built_environment, built_scorer, command


def bind_campaign_evaluation(
    manifest: ManifestV2, *, bundle_root: Path, env: Mapping[str, str],
    secret_names: Sequence[str], stage: str, run_directory: Path | None = None,
):
    """Build the campaign environment and scorer used by fixed evaluation."""
    try:
        if not is_campaign_manifest(manifest):
            _invalid(stage)
        workflow = manifest.workflow
        environ = dict(env)
        environ.update({name: "" for name in secret_names})
        environment = build_environment(workflow.environment)
        scorer = build_scorer(
            workflow.scorer,
            seed_root=manifest.seed_root,
            run_dir=(
                bundle_root / ".lab-binding"
                if run_directory is None else Path(run_directory)
            ),
            environ=environ,
        )
        return environment, scorer, workflow.scorer
    except PreparationError:
        raise
    except (AttributeError, BindingError, TypeError, ValueError):
        _invalid(stage)


def confirm_schedule(
    *, plan: EvaluationPlan, cases: Sequence[EvalCase], manifest: ManifestV2,
    environment: Any, scorer: Any,
) -> None:
    """Have core's public identity codec validate the selected seed schedule."""
    metric_bytes = canonical_json_bytes(plan.to_payload()["metric_plan"])
    target = manifest.target
    placeholder = "preparation-only"
    payload = {
        "profile": "rollout_measurement",
        "row_provenance": 1,
        "candidate_digest": placeholder,
        "target": {
            "contract_digest": target.contract_digest or placeholder,
            "dependency_digest": placeholder,
            "settings_hash": target.settings_hash or placeholder,
            "seed_mode": target.seed_mode,
            "protocol_version": placeholder,
            "runtime_ref_digest": placeholder,
        },
        "environment": asdict(environment.identity),
        "scorer": scorer_binding(scorer),
        "evaluation_set_id": plan.evaluation_set_id,
        "corpus_digest": corpus_digest(cases),
        "metric_plan_digest": hashlib.sha256(metric_bytes).hexdigest(),
        "schedule": [
            {
                "case_id": case.case_id,
                "sample_id": f"case-{case_index}-repeat-{repeat}",
                "repeat": repeat,
                "requested_seed": seed,
            }
            for case_index, case in enumerate(cases, 1)
            for repeat, seed in enumerate(plan.repeat_seeds, 1)
        ],
        "budget": {"max_turns": plan.max_turns, "timeout_seconds": plan.timeout_seconds},
        "max_concurrency": plan.concurrency,
        "max_scorer_concurrency": plan.scorer_concurrency,
        "materialization_strategy": "artifact_root_injection",
    }
    if plan.sample_retries is not None:
        payload["sample_retries"] = plan.sample_retries
    try:
        identity = RunIdentityV1(payload)
        identity.validate_corpus(cases)
    except (TypeError, ValueError):
        raise PreparationError("invalid_request", "prepare") from None
