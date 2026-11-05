from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_source_pools():
    path = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "5p3-adapter-specialization"
        / "runner"
        / "source_pools_v3.py"
    )
    spec = importlib.util.spec_from_file_location("source_pools_v3_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_context_source_rows_prefers_family_then_role_and_excludes_eval() -> None:
    sp = _load_source_pools()
    rows = [
        {
            "trace_id": "eval",
            "trace_dir": "/tmp/eval",
            "phase": "adaptation_source_external",
            "skill": "sql",
            "model_id": "openai/Qwen/Qwen3.5-9B",
            "role": "swe",
            "task_family": "sql-schema-and-joins",
            "task_id": "heldout",
            "attempt_idx": 1,
        },
        {
            "trace_id": "family",
            "trace_dir": "/tmp/family",
            "phase": "adaptation_source_external",
            "skill": "sql",
            "model_id": "openai/Qwen/Qwen3.5-9B",
            "role": "de",
            "task_family": "sql-schema-and-joins",
            "task_id": "source-family",
            "attempt_idx": 1,
        },
        {
            "trace_id": "role",
            "trace_dir": "/tmp/role",
            "phase": "adaptation_source_external",
            "skill": "sql",
            "model_id": "openai/Qwen/Qwen3.5-9B",
            "role": "swe",
            "task_family": "sql-query-tune",
            "task_id": "source-role",
            "attempt_idx": 1,
        },
        {
            "trace_id": "fallback",
            "trace_dir": "/tmp/fallback",
            "phase": "adaptation_source_external",
            "skill": "sql",
            "model_id": "openai/Qwen/Qwen3.5-9B",
            "role": "de",
            "task_family": "sql-query-tune",
            "task_id": "source-fallback",
            "attempt_idx": 1,
        },
        {
            "trace_id": "wrong-phase",
            "trace_dir": "/tmp/wrong",
            "phase": "evaluation_external",
            "skill": "sql",
            "model_id": "openai/Qwen/Qwen3.5-9B",
            "role": "swe",
            "task_family": "sql-schema-and-joins",
            "task_id": "wrong",
            "attempt_idx": 1,
        },
    ]

    chosen = sp.pick_context_source_rows(
        rows,
        skill="sql",
        model_id="openai/Qwen/Qwen3.5-9B",
        role="swe",
        task_family="sql-schema-and-joins",
        budget=3,
        exclude_task_ids={"heldout"},
    )

    assert [r["trace_id"] for r in chosen] == ["family", "role", "fallback"]


def test_pick_context_source_rows_raises_when_budget_cannot_be_met() -> None:
    sp = _load_source_pools()
    rows = [
        {
            "trace_id": "one",
            "trace_dir": "/tmp/one",
            "phase": "adaptation_source_external",
            "skill": "pdf",
            "model_id": "openai/openai/gpt-oss-20b",
            "role": "pm",
            "task_family": "pdf-form-fill",
            "task_id": "source",
        }
    ]

    try:
        sp.pick_context_source_rows(
            rows,
            skill="pdf",
            model_id="openai/openai/gpt-oss-20b",
            role="pm",
            task_family="pdf-form-fill",
            budget=2,
        )
    except ValueError as exc:
        assert "insufficient source traces" in str(exc)
    else:
        raise AssertionError("expected insufficient-source ValueError")


def test_full_evo_glob_is_context_keyed() -> None:
    sp = _load_source_pools()
    glob = sp.full_evo_glob(
        "pdf", "openai/google/gemma-4-E4B-it", "pm", "pdf-form-fill", 8
    )
    assert glob == "pdf__gemma-4-E4B-it__pm__pdf-form-fill__N8__*.md"
