from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Task:
    id: str
    cell_id: str
    skills: tuple[str, ...] = ("anchor",)


def test_produce_with_trace_spec_evolves_once_per_row_and_filters_aux(monkeypatch, tmp_path):
    import evolution.core.store as store_mod
    import evolution.eval.solver as solver_mod
    import evolution.eval.task as task_mod
    import evolution.eval.workspace as workspace_mod
    from evolution.adapters import evolution_adapter as adapter

    pilot = SimpleNamespace(
        train=[
            _Task(id="t1", cell_id="cell-a", skills=("anchor", "aux")),
            _Task(id="t2", cell_id="cell-b", skills=("anchor", "aux")),
        ],
        test=[],
    )
    plan = adapter.ProducePlan(
        per_task_skills={"t1": ["anchor", "aux"], "t2": ["anchor", "aux"]},
        preloaded_skill_names=["anchor", "aux"],
    )
    monkeypatch.setattr(adapter, "produce_baseline", lambda _pilot: plan)
    monkeypatch.setattr(adapter, "_current_skill_version", lambda _skill: "v1")
    monkeypatch.setattr(adapter, "_build_litellm_backend", lambda **_kwargs: SimpleNamespace())

    class _Store:
        def __init__(self, *, project_dir):
            pass

        def get(self, name):
            return SimpleNamespace(name=name)

    monkeypatch.setattr(store_mod, "SkillStore", _Store)

    def fake_load_task(task_dir: Path):
        task_id = Path(task_dir).name
        return SimpleNamespace(id=task_id, name=task_id)

    def fake_setup_workspace(_task, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)

    def fake_solve_task(
        *,
        task,
        backend,
        workspace,
        skills,
        timeout,
        evaluate,
        model_name,
        manifest,
        manifest_scope,
        **_kwargs,
    ):
        assert manifest_scope == "skill_evolution"
        assert task.name in manifest.train_ids()
        return SimpleNamespace(
            eval_result=SimpleNamespace(
                success=False,
                errors=["failed"],
                pass_rate=0.0,
                total_tests=1,
            ),
            errors=[],
            code_extraction_failed=False,
            code_extraction_retried=False,
        )

    monkeypatch.setattr(task_mod, "load_task", fake_load_task)
    monkeypatch.setattr(workspace_mod, "setup_workspace", fake_setup_workspace)
    monkeypatch.setattr(solver_mod, "solve_task", fake_solve_task)

    tasks_dir = tmp_path / "tasks"
    for tid, task_name in {"t1": "Task One", "t2": "Task Two"}.items():
        task_dir = tasks_dir / tid
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(f'name = "{task_name}"\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "unit-produce-split",
                "tasks": [
                    {"id": "t1", "split": "train"},
                    {"id": "t2", "split": "train"},
                ],
            }
        ),
        encoding="utf-8",
    )

    calls: list[dict] = []

    def fake_evolve(**kwargs):
        calls.append(kwargs)
        return {
            "anchor": {
                "evolve_rounds": 1,
                "before_version": "v1",
                "after_version": "v2",
                "failing_task": "Task One",
                "failing_tasks": kwargs["skill_to_failing_tasks"]["anchor"],
                "trace_ids": kwargs["skill_to_failing_tasks"]["anchor"],
                "reflect_rc": 0,
                "rewrite_rc": 0,
                "commit_rc": 0,
                "notes": [],
            }
        }

    monkeypatch.setattr(adapter, "evolve_skills_one_round", fake_evolve)

    result = adapter.produce_with_trace_spec(
        pilot=pilot,
        trace_spec=[
            SimpleNamespace(task_id="t1", source_model_id="solver-a", attempt_seed=1),
            SimpleNamespace(task_id="t2", source_model_id="solver-b", attempt_seed=2),
        ],
        trace_budget=2,
        seed=0,
        task_diversity="T3",
        model_dispatch={
            "solver-a": {"api_base": "https://example.test/v1", "api_key": "k"},
            "solver-b": {"api_base": "https://example.test/v1", "api_key": "k"},
        },
        reflector_model_id="codex/gpt-5.5",
        reflector_api_base="https://example.test/v1",
        reflector_api_key="k",
        manifest_path=manifest_path,
        evolution_tasks_dir=tasks_dir,
        work_root=tmp_path / "work",
        do_evolve=True,
        allowed_skill_names=["anchor"],
    )

    assert len(calls) == 1
    assert calls[0]["skill_to_failing_tasks"] == {"anchor": ["Task One", "Task Two"]}
    assert calls[0]["trace_model_id"] is None
    assert calls[0]["evolution_tasks_dir"] == tasks_dir
    assert result["per_task_skills"] == {"t1": ["anchor"], "t2": ["anchor"]}
    assert result["preloaded_skill_names"] == ["anchor"]
    assert list(result["evolve_records"]) == ["aggregate"]
    record = result["evolve_records"]["aggregate"]["anchor"]
    assert record["evolve_status"] == "evolved"
    assert record["selected_source_models"] == ["solver-a", "solver-b"]
    assert record["source_model_filter"] == ""
    assert record["contributing_cells"] == ["cell-a", "cell-b"]


def _load_table34_eval_module():
    path = (
        REPO_ROOT
        / "experiments"
        / "table3-4-minimal-sbbench-v2"
        / "runner"
        / "framework_eval_valid.py"
    )
    sys.path.insert(0, str(REPO_ROOT / "src"))
    sys.path.insert(0, str(REPO_ROOT / "experiments"))
    spec = importlib.util.spec_from_file_location("table34_framework_eval_valid_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["table34_framework_eval_valid_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_evolution_eval_anchor_only_uses_persisted_anchor_not_task_aux(tmp_path):
    mod = _load_table34_eval_module()
    skills_dir = tmp_path / "skills"
    (skills_dir / "anchor").mkdir(parents=True)
    (skills_dir / "aux").mkdir(parents=True)
    (skills_dir / "anchor" / "SKILL.md").write_text("ANCHOR", encoding="utf-8")
    (skills_dir / "aux" / "SKILL.md").write_text("AUX", encoding="utf-8")

    bodies = mod._evolution_skill_bodies(
        {
            "skills_dir": str(skills_dir),
            "skill_names": ["anchor"],
            "skill_selection_policy": "anchor_only",
        },
        task_skill_names=["aux"],
    )

    assert bodies == ["ANCHOR"]


def test_evolution_eval_anchor_only_fails_if_persisted_anchor_missing(tmp_path):
    mod = _load_table34_eval_module()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="missing persisted skills"):
        mod._evolution_skill_bodies(
            {
                "skills_dir": str(skills_dir),
                "skill_names": ["anchor"],
                "skill_selection_policy": "anchor_only",
            },
            task_skill_names=[],
        )
