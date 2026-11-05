"""Shared measurement configuration contracts."""

import json
import subprocess
import sys
from dataclasses import MISSING, FrozenInstanceError, fields
from typing import get_type_hints

import pytest

from evolution.tree.execution.config import EvaluationPolicy
from evolution.tree.execution.interaction import RolloutBudget


def test_evaluation_policy_import_boundary_in_a_fresh_process() -> None:
    script = """
import importlib
import json
from typing import get_type_hints

from evolution.tree.execution.config import EvaluationPolicy

module_names = (
    "evolution.tree.evaluation_manifest",
    "evolution.tree.campaign_identity",
    "evolution.tree.campaign_measurement",
)
modules = [importlib.import_module(name) for name in module_names]
print(json.dumps({
    "owner": EvaluationPolicy.__module__,
    "old_exports": [module.__name__ for module in modules if hasattr(module, "EvaluationPolicy")],
    "annotation_is_canonical": get_type_hints(modules[0].EvaluationManifest)["policy"] is EvaluationPolicy,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "owner": "evolution.tree.execution.config",
        "old_exports": [],
        "annotation_is_canonical": True,
    }


def test_evaluation_policy_fields_remain_ordered_and_required() -> None:
    assert [
        (field.name, field.default, field.default_factory) for field in fields(EvaluationPolicy)
    ] == [
        ("repeats", MISSING, MISSING),
        ("random_seed", MISSING, MISSING),
        ("max_concurrency", MISSING, MISSING),
        ("max_scorer_concurrency", MISSING, MISSING),
        ("metric_primary", MISSING, MISSING),
        ("budget", MISSING, MISSING),
        ("sample_retries", MISSING, MISSING),
    ]


def test_evaluation_policy_annotations_resolve_at_runtime() -> None:
    assert get_type_hints(EvaluationPolicy) == {
        "repeats": int,
        "random_seed": int,
        "max_concurrency": int,
        "max_scorer_concurrency": int,
        "metric_primary": str,
        "budget": RolloutBudget,
        "sample_retries": int,
    }


def test_evaluation_policy_is_frozen() -> None:
    policy = EvaluationPolicy(3, 1, 1, 1, "score", RolloutBudget(2, 20), 0)
    with pytest.raises(FrozenInstanceError):
        policy.repeats = 4
