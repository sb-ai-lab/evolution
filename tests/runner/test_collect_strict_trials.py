"""Task 8: strict trial enforcement in the table3-4-minimal collector.

n_trials is a paper invariant (produce-once / eval-N). A (row, metric, task)
cell that has fewer than the declared ``n_trials`` trial_* artifacts must
HARD-fail collection (an under-trialed cell may not silently pass with only a
warning).

These tests construct a minimal ``_collect_flat`` input where one cell has
4 of 5 trial_* artifacts and assert collection raises an *issue* (not a mere
warning); a sibling test with a fully-trialed (5/5) cell asserts no such
issue is raised. The 4/5 test FAILS against the pre-Task-8 code (which only
appended a warning) and PASSES after the warnings->issues change.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.paper

REPO_ROOT = Path(__file__).resolve().parents[2]
EXP = REPO_ROOT / "experiments" / "table3-4-minimal-sbbench-v2"
COLLECT = EXP / "runner" / "collect_results.py"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_module("table3_minimal_collect_strict", COLLECT)

DECLARED_N_TRIALS = 5


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_run(tmp_path: Path, *, n_present: int) -> tuple[Path, dict]:
    """Construct a minimal collect input with ONE row_id=2 spec cell.

    row_id < 3 is used so ``_validate_produce_artifacts`` is skipped (it only
    inspects evolved rows >= 3), keeping the fixture minimal: no trace_spec or
    framework_produce artifacts are required.
    """
    run_root = tmp_path / "runs" / "strict_trials_test"
    skill_dir = run_root / "skills" / "pdf"

    unit = {
        "row_id": 2,
        "metric": "spec",
        "source_cell_id": "pm.pdf",
        "source_artifact_cell_id": "pm.pdf",
        "target_cell_id": "pm.pdf",
        "task_id": "court-form-filling",
        "split_tier": "s3_strict_test_heldout",
    }
    _write_json(skill_dir / "eval_plan.json", [unit])

    base = (
        skill_dir
        / "framework_eval"
        / "evolution"
        / f"row{unit['row_id']}"
        / unit["source_cell_id"]
        / unit["metric"]
        / unit["task_id"]
    )
    for trial_index in range(n_present):
        _write_json(
            base / f"trial_{trial_index}" / "result.json",
            {
                "schema": "table3-framework-eval-result-v1",
                "framework": "evolution",
                "row_id": unit["row_id"],
                "row_label": "No skill",
                "metric": unit["metric"],
                "source_cell_id": unit["source_cell_id"],
                "target_cell_id": unit["target_cell_id"],
                "task_id": unit["task_id"],
                "outcome": "ok",
                "pass_rate": 0.5,
                "total_tests": 2,
            },
        )

    _write_json(
        run_root / "RUN_MANIFEST.json",
        {
            "valid_for_api_run": True,
            "skills": [{"skill": "pdf", "artifact_dir": str(skill_dir)}],
        },
    )
    manifest = {
        "n_trials": DECLARED_N_TRIALS,
        "table4_skills": [],
        # _validate_produce_artifacts reads this even though it skips rows < 3.
        "models": {"heldout_genm_model": {"id": "openai/openai/gpt-oss-20b"}},
    }
    return run_root, manifest


def test_under_trialed_cell_hard_fails_collection(tmp_path):
    """4 of 5 trial_* artifacts -> a hard *issue*, not a warning.

    This is the fail-pre/pass-post assertion: before Task 8 the shortfall went
    to ``warnings`` and ``issues`` stayed empty (this assertion would FAIL).
    """
    run_root, manifest = _build_run(tmp_path, n_present=4)

    _flat, issues, warnings = collector._collect_flat(run_root, manifest)

    assert any("under-trialed cell" in i for i in issues), (
        f"under-trialed (4/5) cell must raise an issue, got issues={issues} warnings={warnings}"
    )
    offending = next(i for i in issues if "under-trialed cell" in i)
    assert "found 4 trials" in offending
    assert f"expected {DECLARED_N_TRIALS}" in offending
    assert "court-form-filling" in offending


def test_fully_trialed_cell_raises_no_trial_shortfall_issue(tmp_path):
    """5 of 5 trial_* artifacts -> no under-trialed issue (sibling control)."""
    run_root, manifest = _build_run(tmp_path, n_present=DECLARED_N_TRIALS)

    _flat, issues, _warnings = collector._collect_flat(run_root, manifest)

    assert not [i for i in issues if "under-trialed cell" in i], (
        f"fully-trialed (5/5) cell must not raise a trial-shortfall issue: {issues}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
