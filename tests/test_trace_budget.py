"""Round 0a: budget-invariant tests for §5.2 produce path.

Per the §5.2 plan's load-bearing premortem fix: H1 (task diversity →
cross-task transfer) cannot be tested with varying N. The selector and
the produce-path MUST refuse to run when len(trace_spec) != trace_budget.

These are unit tests; they don't require API keys, sglang, or any of the
heavy fixtures the full pilot needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from _shared.training_subset import (  # noqa: E402
    TraceSpec,
    select_training_subset,
)

# ---------------------------------------------------------------------------
# Selector invariants
# ---------------------------------------------------------------------------


def test_t1_single_returns_n_specs():
    specs = select_training_subset(
        task_diversity="T1",
        source_pool=["anthropic/claude-sonnet-4.6"],
        n_traces=24,
        seed=0,
        t1_task_id="task-A",
    )
    assert len(specs) == 24
    # T1 = same task; only one unique task_id.
    assert len({s.task_id for s in specs}) == 1
    # Single = one source model.
    assert len({s.source_model_id for s in specs}) == 1
    # Distinct attempt seeds so 24 calls aren't dedupe-able.
    assert len({s.attempt_seed for s in specs}) == 24


def test_t1_cross_returns_n_specs_with_three_models():
    specs = select_training_subset(
        task_diversity="T1",
        source_pool=[
            "anthropic/claude-sonnet-4.6",
            "openai/Qwen/Qwen3.6-35B-A3B",
            "meta-llama/llama-3.1-8b-instruct",
        ],
        n_traces=24,
        seed=0,
        t1_task_id="task-A",
    )
    assert len(specs) == 24
    assert len({s.task_id for s in specs}) == 1
    assert len({s.source_model_id for s in specs}) == 3
    # Even split: 8 per model.
    counts: dict[str, int] = {}
    for s in specs:
        counts[s.source_model_id] = counts.get(s.source_model_id, 0) + 1
    assert sorted(counts.values()) == [8, 8, 8]


def test_t2_single_n_specs_within_role():
    role_train = ["task-pm-1", "task-pm-2"]
    specs = select_training_subset(
        task_diversity="T2",
        source_pool=["anthropic/claude-sonnet-4.6"],
        n_traces=24,
        seed=0,
        role_train_tasks=role_train,
    )
    assert len(specs) == 24
    assert len({s.source_model_id for s in specs}) == 1
    # T2 spans multiple tasks.
    assert len({s.task_id for s in specs}) == 2


def test_t3_cross_anchor_pair():
    anchor_pair_tasks = ["pm-1", "pm-2", "ds-1", "ds-2"]
    specs = select_training_subset(
        task_diversity="T3",
        source_pool=[
            "anthropic/claude-sonnet-4.6",
            "openai/Qwen/Qwen3.6-35B-A3B",
            "meta-llama/llama-3.1-8b-instruct",
        ],
        n_traces=24,
        seed=0,
        anchor_pair_train_tasks=anchor_pair_tasks,
    )
    assert len(specs) == 24
    assert {s.task_id for s in specs} == set(anchor_pair_tasks)
    assert len({s.source_model_id for s in specs}) == 3


def test_held_out_invariant_rejects_gpt4o_mini():
    with pytest.raises(ValueError, match="held-out"):
        select_training_subset(
            task_diversity="T1",
            source_pool=["openai/gpt-4o-mini"],
            n_traces=24,
            seed=0,
            t1_task_id="task-A",
        )


def test_deterministic_reseed():
    """Same seed ⇒ same selector output (replayability)."""
    args = dict(
        task_diversity="T1",
        source_pool=[
            "anthropic/claude-sonnet-4.6",
            "openai/Qwen/Qwen3.6-35B-A3B",
            "meta-llama/llama-3.1-8b-instruct",
        ],
        n_traces=24,
        seed=42,
        t1_task_id="task-A",
    )
    a = [s.to_jsonable() for s in select_training_subset(**args)]
    b = [s.to_jsonable() for s in select_training_subset(**args)]
    assert a == b


# ---------------------------------------------------------------------------
# Produce-path invariant: trace_budget mismatch must refuse to run.
# We don't actually call solve_task here; the function refuses up front.
# ---------------------------------------------------------------------------


def test_produce_with_trace_spec_refuses_budget_mismatch():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from evolution.adapters.evolution_adapter import produce_with_trace_spec

    bad_spec = [TraceSpec("task-A", "anthropic/claude-sonnet-4.6", 0)]  # length 1, budget 24
    with pytest.raises(AssertionError, match="trace_budget invariant"):
        produce_with_trace_spec(
            pilot=None,
            trace_spec=bad_spec,
            trace_budget=24,
            seed=0,
            task_diversity="T1",
            model_dispatch={
                "anthropic/claude-sonnet-4.6": {
                    "api_base": "https://openrouter.ai/api/v1",
                    "api_key": "EMPTY",
                }
            },
            reflector_model_id="google/gemini-3-flash-preview",
            reflector_api_base="https://openrouter.ai/api/v1",
            reflector_api_key="EMPTY",
            manifest_path=Path("/tmp/manifest.json"),
            evolution_tasks_dir=Path("/tmp/tasks"),
            work_root=Path("/tmp/work"),
        )


# ---------------------------------------------------------------------------
# §5.2-local manifest validation (Fix 2)
# ---------------------------------------------------------------------------


@pytest.mark.paper
def test_build_manifest_5p2_passes_assert_no_leakage(tmp_path):
    """Per the rerun plan: every §5.2 train id must pass
    `SplitManifest.load(<built>).assert_no_leakage(<train_ids>,
    scope=SKILL_EVOLUTION)`.
    """
    sys.path.insert(0, str(REPO_ROOT / "experiments" / "5p2-table3-pilot"))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import json as _json

    from build_manifest import build_manifest, write_and_validate  # type: ignore

    from evolution.splits.manifest import SKILL_EVOLUTION

    cells_path = (
        REPO_ROOT / "experiments" / "4way-sbbench-pilot" / "artifacts" / "cells_resolved.json"
    )
    cells = _json.loads(cells_path.read_text(encoding="utf-8"))
    manifest = build_manifest(cells)

    out = tmp_path / "manifest_5p2.json"
    sm = write_and_validate(manifest, out)
    train_ids: list[str] = []
    for cell in cells["cells"]:
        for stream in ("train_a", "train_b"):
            train_ids.extend(cell["streams"].get(stream, []))
    # Should NOT raise.
    sm.assert_no_leakage(train_ids, scope=SKILL_EVOLUTION, context="test")
    # Sanity: counts.
    assert sm.counts.get("train") == len(train_ids)
