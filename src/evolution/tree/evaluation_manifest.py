"""Strict manifest for measuring a frozen tree without improvement roles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.execution import config as _config
from evolution.tree.execution.config import (
    CommandSection,
    EnvironmentSection,
    ManifestError,
)
from evolution.tree.execution.interaction import CallRetries, RolloutBudget
from evolution.tree.manifest.manifest_v2 import (
    _parse_artifacts,
    _parse_command,
    _parse_corpus,
    _parse_environment,
    _parse_target,
    _validate_structure,
)
from evolution.tree.target.config import TargetBinding

EVALUATION_SCHEMA = "evolution-v2-evaluation/1"


@dataclass(frozen=True)
class EvaluationManifest:
    path: Path
    snapshot: dict[str, str]
    corpus: Path
    split: str
    target: TargetBinding
    artifacts: ArtifactPolicyV1
    environment: EnvironmentSection
    scorer: CommandSection
    policy: _config.EvaluationPolicy
    declaration: dict[str, Any]


def _integer(value: Any, field: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ManifestError("bad_value", field)
    return value


def load_evaluation_manifest(path: Path) -> EvaluationManifest:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ManifestError("bad_path", "manifest") from exc
    fields = {
        "schema",
        "snapshot",
        "corpus",
        "split",
        "target",
        "artifacts",
        "environment",
        "scorer",
        "repeats",
        "random_seed",
        "max_concurrency",
        "max_scorer_concurrency",
        "metric",
        "budget",
        "sample_retries",
        "call_retries",
    }
    if not isinstance(document, dict) or document.get("schema") != EVALUATION_SCHEMA:
        raise ManifestError("bad_schema", "schema")
    _validate_structure(document, frozenset(fields), frozenset())
    base = path.resolve().parent
    snapshot = document["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) not in ({"root"}, {"run_dir", "version"}):
        raise ManifestError("bad_value", "snapshot")
    if any(not isinstance(value, str) or not value.strip() for value in snapshot.values()):
        raise ManifestError("bad_value", "snapshot")
    source = "root" if "root" in snapshot else "run_dir"
    snapshot = {**snapshot, source: str((base / snapshot[source]).resolve())}
    if document["split"] not in ("train", "test"):
        raise ManifestError("bad_value", "split")
    metric = document["metric"]
    if (
        not isinstance(metric, dict)
        or set(metric) != {"primary"}
        or not isinstance(metric["primary"], str)
        or not metric["primary"].strip()
    ):
        raise ManifestError("bad_value", "metric")
    retries = document["call_retries"]
    budget = document["budget"]
    if not isinstance(retries, dict) or set(retries) != {"max_attempts", "turn_max_attempts"}:
        raise ManifestError("bad_value", "call_retries")
    if not isinstance(budget, dict) or set(budget) != {
        "max_turns",
        "timeout_seconds",
        "max_failed_turns",
    }:
        raise ManifestError("bad_value", "budget")
    try:
        rollout = RolloutBudget(**budget, call_retries=CallRetries(**retries))
    except (TypeError, ValueError) as exc:
        raise ManifestError("bad_value", "budget") from exc
    sample_retries = _integer(document["sample_retries"], "sample_retries", 0)
    if sample_retries > 8:
        raise ManifestError("bad_value", "sample_retries")
    policy = _config.EvaluationPolicy(
        _integer(document["repeats"], "repeats"),
        _integer(document["random_seed"], "random_seed", 0),
        _integer(document["max_concurrency"], "max_concurrency"),
        _integer(document["max_scorer_concurrency"], "max_scorer_concurrency"),
        metric["primary"],
        rollout,
        sample_retries,
    )
    seed = Path(snapshot[source])
    return EvaluationManifest(
        path.resolve(),
        snapshot,
        _parse_corpus(document["corpus"], "corpus", base),
        document["split"],
        _parse_target(document["target"], "target", schema="evolution-v2-manifest/15"),
        _parse_artifacts(document["artifacts"], "artifacts"),
        _parse_environment(document["environment"], "environment"),
        _parse_command(
            document["scorer"], "scorer", base, seed, with_timeout=True, require_kind=False
        ),
        policy,
        document,
    )
