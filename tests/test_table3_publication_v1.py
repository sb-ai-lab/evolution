"""Offline invariants for the Table 3/4 publication runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.paper

REPO_ROOT = Path(__file__).resolve().parents[1]
EXP = REPO_ROOT / "experiments" / "table3-publication-v1"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_module("table3_publication_preflight", EXP / "preflight_offline.py")
table4_static = _load_module("table3_publication_table4_static", EXP / "table4_static_eval.py")
collector = _load_module("table3_publication_collect", EXP / "collect_results.py")


def _manifest() -> dict:
    return json.loads((EXP / "table3_manifest.json").read_text(encoding="utf-8"))


def _subset() -> dict:
    return json.loads((EXP / "frozen_subset.json").read_text(encoding="utf-8"))


def test_publication_manifest_uses_declared_model_substitution():
    manifest = _manifest()
    models = manifest["models"]

    assert [m["paper_label"] for m in models["source_models_single"]] == ["Claude Sonnet 4"]
    assert [m["paper_label"] for m in models["source_models_cross"]] == [
        "Claude Sonnet 4",
        "Qwen3.6-35B-A3B local substitution",
        "Llama 3.1 8B",
    ]
    qwen = models["source_models_cross"][1]
    assert qwen["id"] == "openai/Qwen/Qwen3.6-35B-A3B"
    assert qwen["served_id_hint"] == "Qwen/Qwen3.6-35B-A3B"
    assert qwen["api_base"] == "http://localhost:30004/v1"
    assert models["heldout_genm_model"]["paper_label"] == "GPT-4o"
    assert models["heldout_genm_model"]["id"] == "openai/gpt-4o"
    assert manifest["policy"]["qwen_substitution_declared"] is True
    assert manifest["concurrency"] == {"local_qwen": 3, "openrouter": 16}

    serialized = json.dumps(models).lower()
    for forbidden in ("claude-sonnet-4.6", "gemini", "gpt-4o-mini"):
        assert forbidden not in serialized
    assert preflight._validate_models(manifest) == []


def test_local_qwen_endpoint_check_rejects_wrong_advertised_model(monkeypatch):
    manifest = _manifest()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data": [{"id": "Qwen/Qwen3.6-35B-A3B"}]}'

    monkeypatch.setattr(
        preflight.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse()
    )
    assert preflight._check_local_qwen(manifest) == []

    class BadResponse(FakeResponse):
        def read(self):
            return b'{"data": [{"id": "Qwen/Qwen3.5-27B"}]}'

    monkeypatch.setattr(
        preflight.urllib.request, "urlopen", lambda *_args, **_kwargs: BadResponse()
    )
    issues = preflight._check_local_qwen(manifest)
    assert any("not advertised" in issue for issue in issues)


def test_publication_preflight_freezes_expected_counts_and_spec_local(tmp_path):
    manifest = _manifest()
    subset = _subset()
    run_root = tmp_path / "runs" / "publication_test"
    summaries = []
    issues = []
    for skill_name in manifest["report_source_skills"]:
        summary = preflight._build_skill_artifacts(
            manifest=manifest,
            subset=subset,
            skill_name=skill_name,
            run_root=run_root,
            seed=0,
        )
        summaries.append(summary)
        issues.extend(preflight._invariant_issues(summary, manifest))

    assert issues == []
    by_skill = {s["skill"]: s for s in summaries}
    assert {k: v["n_sanitized_tasks"] for k, v in by_skill.items()} == {
        "docx": 11,
        "pdf": 15,
        "xlsx": 15,
    }

    for summary in summaries:
        skill_dir = Path(summary["artifact_dir"])
        cells = json.loads((skill_dir / "cells_resolved.json").read_text(encoding="utf-8"))
        eval_plan = json.loads((skill_dir / "eval_plan.json").read_text(encoding="utf-8"))
        source = next(c for c in cells["cells"] if c["cell_id"] == cells["source_cell"])
        local = set(source["streams"]["train_a"]) | set(source["streams"]["train_b"])
        diagnostic_val = set(source["streams"]["val"])
        spec_units = [u for u in eval_plan if u["metric"] == "spec"]
        assert spec_units
        assert {u["task_id"] for u in spec_units} == local
        assert {u["task_id"] for u in spec_units}.isdisjoint(diagnostic_val)


def test_publication_trace_and_genr_invariants(tmp_path):
    manifest = _manifest()
    subset = _subset()
    run_root = tmp_path / "runs" / "publication_test"
    heldout = manifest["models"]["heldout_genm_model"]["id"]
    for skill_name in manifest["report_source_skills"]:
        summary = preflight._build_skill_artifacts(
            manifest=manifest,
            subset=subset,
            skill_name=skill_name,
            run_root=run_root,
            seed=0,
        )
        skill_dir = Path(summary["artifact_dir"])
        eval_plan = json.loads((skill_dir / "eval_plan.json").read_text(encoding="utf-8"))
        for unit in eval_plan:
            if unit["metric"] == "genr":
                assert unit["source_artifact_cell_id"] == unit["source_cell_id"]
                assert unit["target_cell_id"] != unit["source_cell_id"]
        for entry in summary["trace_specs"]:
            payload = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
            assert payload["trace_budget"] == 24
            assert len(payload["trace_spec"]) == 24
            assert all(
                heldout.lower() not in s["source_model_id"].lower() for s in payload["trace_spec"]
            )


def test_table4_static_overlay_uses_skill_handcraft(tmp_path):
    manifest = _manifest()
    subset = _subset()
    run_root = tmp_path / "runs" / "publication_test"
    summary = preflight._build_skill_artifacts(
        manifest=manifest,
        subset=subset,
        skill_name="pdf",
        run_root=run_root,
        seed=0,
    )
    skill_dir = Path(summary["artifact_dir"])
    cells = json.loads((skill_dir / "cells_resolved.json").read_text(encoding="utf-8"))
    eval_plan = json.loads((skill_dir / "eval_plan.json").read_text(encoding="utf-8"))
    unit = next(u for u in eval_plan if u["metric"] == "table4" and u["row_id"] == 1)

    overlay, copied = table4_static.build_handcraft_overlay(
        manifest=manifest,
        cells=cells,
        unit=unit,
        task_id=unit["task_id"],
        cache_dir=tmp_path / "cache",
    )

    assert copied
    assert all(item["source"].endswith("SKILL_HANDCRAFT.md") for item in copied)
    for item in copied:
        assert Path(item["overlay"]).is_file()
        assert Path(item["overlay"]).read_text(encoding="utf-8") == Path(item["source"]).read_text(
            encoding="utf-8"
        )
    assert (overlay / "environment" / "skills" / "pdf" / "SKILL.md").is_file()


def test_publication_collector_rejects_zero_test_and_non_handcraft(tmp_path):
    path = tmp_path / "result.json"
    unit = {
        "row_id": 1,
        "metric": "table4",
        "source_cell_id": "pm.pdf",
        "target_cell_id": "pm.pdf",
        "task_id": "financial-modeling-qa",
    }
    path.write_text(
        json.dumps(
            {
                "schema": "table3-framework-eval-result-v1",
                "framework": "evolution",
                "row_id": 1,
                "metric": "table4",
                "source_cell_id": "pm.pdf",
                "target_cell_id": "pm.pdf",
                "task_id": "financial-modeling-qa",
                "outcome": "ok",
                "total_tests": 0,
                "static_skill_sources": [{"source": "/tmp/SKILL.md"}],
            }
        ),
        encoding="utf-8",
    )

    issues: list[str] = []
    data = collector._validate_result_artifact(path=path, unit=unit, issues=issues)
    assert data is not None
    collector._validate_static_table4(data, path, issues)

    assert any("zero-test" in issue for issue in issues)
    assert any("SKILL_HANDCRAFT.md" in issue for issue in issues)


def test_publication_collector_rejects_table3_row1_without_handcraft(tmp_path):
    """Table 3 row 1 Base Skill must load SKILL_HANDCRAFT.md (matching Table 4)."""
    path = tmp_path / "result.json"
    unit = {
        "row_id": 1,
        "metric": "spec",
        "source_cell_id": "pm.pdf",
        "target_cell_id": "pm.pdf",
        "task_id": "court-form-filling",
    }
    path.write_text(
        json.dumps(
            {
                "schema": "table3-framework-eval-result-v1",
                "framework": "evolution",
                "row_id": 1,
                "row_label": "Base Skill",
                "metric": "spec",
                "source_cell_id": "pm.pdf",
                "target_cell_id": "pm.pdf",
                "task_id": "court-form-filling",
                "outcome": "ok",
                "pass_rate": 0.5,
                "total_tests": 2,
                "baseline_condition": "default_skill",
                "static_skill_sources": [{"source": "/tmp/SKILL.md"}],
            }
        ),
        encoding="utf-8",
    )

    issues: list[str] = []
    data = collector._validate_result_artifact(path=path, unit=unit, issues=issues)
    assert data is not None
    collector._validate_static_baseline(data, path, issues)

    assert any("SKILL_HANDCRAFT.md" in issue for issue in issues)
    assert any("Table 3 row 1 Base Skill" in issue for issue in issues)


def test_publication_collector_accepts_table3_row1_handcraft(tmp_path):
    """Happy path: row 1 Base Skill with handcraft provenance passes validation."""
    path = tmp_path / "result.json"
    unit = {
        "row_id": 1,
        "metric": "gent",
        "source_cell_id": "pm.pdf",
        "target_cell_id": "pm.pdf",
        "task_id": "edit-pdf",
    }
    path.write_text(
        json.dumps(
            {
                "schema": "table3-framework-eval-result-v1",
                "framework": "evolution",
                "row_id": 1,
                "row_label": "Base Skill",
                "metric": "gent",
                "source_cell_id": "pm.pdf",
                "target_cell_id": "pm.pdf",
                "task_id": "edit-pdf",
                "outcome": "ok",
                "pass_rate": 0.75,
                "total_tests": 4,
                "baseline_condition": "default_skill",
                "static_skill_sources": [
                    {
                        "skill": "pdf",
                        "source": "/corpus/skills/pdf/SKILL_HANDCRAFT.md",
                        "overlay": "/tmp/overlay/pdf/SKILL.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    issues: list[str] = []
    data = collector._validate_result_artifact(path=path, unit=unit, issues=issues)
    assert data is not None
    collector._validate_static_baseline(data, path, issues)

    assert not [i for i in issues if "Base Skill" in i]


def test_training_selector_rejects_publication_heldout_gpt4o():
    from _shared.training_subset import select_training_subset

    with pytest.raises(ValueError, match="held-out"):
        select_training_subset(
            task_diversity="T1",
            source_pool=["openai/gpt-4o"],
            n_traces=24,
            seed=0,
            t1_task_id="task-A",
        )


def test_evolution_failure_selector_accepts_zero_passrate_pytest_failures():
    from evolution.adapters import evolution_adapter

    selected = evolution_adapter._select_one_failing_trace(
        [
            SimpleNamespace(
                task_id="pre-verifier-hard-fail",
                source_model_id="openai/Qwen/Qwen3.6-35B-A3B",
                success=False,
                error="no code block",
                pass_rate=0.0,
                total_tests=0,
                skills_used=["pdf"],
            ),
            SimpleNamespace(
                task_id="verifier-covered-fail",
                source_model_id="meta-llama/llama-3.1-8b-instruct",
                success=False,
                error="assertion failed",
                pass_rate=0.0,
                total_tests=4,
                skills_used=["pdf"],
            ),
        ],
        "pdf",
    )

    assert selected == ("verifier-covered-fail", "meta-llama/llama-3.1-8b-instruct")


def test_produce_contract_rejects_evolution_noop_after_verifier_failures():
    from _shared.table3_adapter_contract import validate_produce_plan

    plan = {
        "schema": "table3-framework-produce-v2",
        "framework": "evolution",
        "row_id": 3,
        "status": "completed",
        "valid_for_eval": True,
        "trace_spec_hash": "a" * 64,
        "trace_budget": 1,
        "n_traces_used": 1,
        "source_model_ids": ["anthropic/claude-sonnet-4"],
        "used_openrouter": True,
        "base_seed": {
            "policy": "source_trace_task_skill_union_v1",
            "seed_source_task_ids": ["task-A"],
            "base_skill_names": ["pdf"],
            "base_skill_hashes": {"pdf": "b" * 64},
        },
        "records": [
            {
                "task_id": "task-A",
                "source_model_id": "anthropic/claude-sonnet-4",
                "solve_status": "completed",
                "pytest_status": "completed",
            }
        ],
        "health": {
            "persistence_ok": True,
            "base_seed_ok": True,
            "eval_writes_blocked": True,
            "skipped_or_noop_labelled": True,
        },
        "state": {
            "evolution_plan": {
                "evolve_summary": {"evolved": 0, "no_failure_signal": 1, "error": 0},
                "train_solve_summary": [
                    {
                        "task_id": "task-A",
                        "success": False,
                        "pass_rate": 0.0,
                        "total_tests": 4,
                    }
                ],
            }
        },
    }

    issues = validate_produce_plan(
        plan,
        trace_spec_hash="a" * 64,
        trace_budget=1,
        heldout_model_id="openai/gpt-4o",
    )

    assert any("produced no evolved skill version" in issue for issue in issues)


def test_produce_contract_accepts_benign_no_op_keep_parent():
    """Regression for the t34v4_n3 whole-run abort: a leak-rejected /
    no-change round (clean reflect→rewrite→commit, version not bumped,
    classified ``no_op``) after verifier failures is an HONEST no-op —
    the skill stays at a valid prior version and the seed chain must
    continue. It must NOT raise the "produced no evolved skill version"
    or "evolve_summary has errors" contract violation (which previously
    forced valid_for_eval=False → SystemExit seed-chain whole-run kill).
    Contrast test_produce_contract_rejects_evolution_noop_after_verifier_failures:
    no_failure_signal is still flagged; only no_op is benign.
    """
    from _shared.table3_adapter_contract import validate_produce_plan

    plan = {
        "schema": "table3-framework-produce-v2",
        "framework": "evolution",
        "row_id": 3,
        "status": "completed",
        "valid_for_eval": True,
        "trace_spec_hash": "a" * 64,
        "trace_budget": 1,
        "n_traces_used": 1,
        "source_model_ids": ["anthropic/claude-sonnet-4"],
        "used_openrouter": True,
        "base_seed": {
            "policy": "source_trace_task_skill_union_v1",
            "seed_source_task_ids": ["task-A"],
            "base_skill_names": ["pdf"],
            "base_skill_hashes": {"pdf": "b" * 64},
        },
        "records": [
            {
                "task_id": "task-A",
                "source_model_id": "anthropic/claude-sonnet-4",
                "solve_status": "completed",
                "pytest_status": "completed",
            }
        ],
        "health": {
            "persistence_ok": True,
            "base_seed_ok": True,
            "eval_writes_blocked": True,
            "skipped_or_noop_labelled": True,
        },
        "state": {
            "evolution_plan": {
                "evolve_summary": {
                    "evolved": 0,
                    "no_failure_signal": 0,
                    "no_op": 1,
                    "error": 0,
                },
                "train_solve_summary": [
                    {
                        "task_id": "task-A",
                        "success": False,
                        "pass_rate": 0.0,
                        "total_tests": 4,
                    }
                ],
            }
        },
    }

    issues = validate_produce_plan(
        plan,
        trace_spec_hash="a" * 64,
        trace_budget=1,
        heldout_model_id="openai/gpt-4o",
    )

    assert not any("produced no evolved skill version" in issue for issue in issues), issues
    assert not any("evolve_summary has errors" in issue for issue in issues), issues
