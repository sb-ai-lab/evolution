"""Cold-start CLI helpers: scaffold loads, credential check, app imports."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.cli.onboarding import _model_credential, _scaffold_task
from evolution.eval.task import TaskResult


def test_scaffold_task_is_loadable(tmp_path: Path):
    from evolution.eval.task import load_task

    task_dir = _scaffold_task("demo", tmp_path, "my-skill", force=False)
    t = load_task(task_dir)
    assert t.name == "demo"
    assert t.skills_required == ["my-skill"]
    assert t.role is None  # evo-native: no conda role-env required


@pytest.mark.parametrize(
    ("task_id", "skill_id"),
    [("../outside", "my-skill"), ("demo", "../outside")],
)
def test_scaffold_task_rejects_unsafe_ids(tmp_path: Path, task_id: str, skill_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid"):
        _scaffold_task(task_id, tmp_path / "tasks", skill_id, force=False)
    assert not (tmp_path / "outside").exists()


def test_scaffold_task_rejects_nested_symlink_escape_before_writing(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = tasks_root / "demo"
    outside = tmp_path / "outside"
    task_dir.mkdir(parents=True)
    outside.mkdir()
    (task_dir / "tests").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        _scaffold_task("demo", tasks_root, "my-skill", force=True)

    assert list(outside.iterdir()) == []


def test_model_credential_branches(monkeypatch):
    assert _model_credential("mock", None)[0] is True
    # local endpoint needs no provider key
    assert _model_credential("openai/whatever", "http://localhost:30004/v1")[0] is True

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ok, env, fix = _model_credential("openrouter/anthropic/claude-sonnet-4", None)
    assert ok is False and env == "OPENROUTER_API_KEY" and "export" in fix
    assert (
        _model_credential("openrouter/anthropic/claude-sonnet-4", "https://remote.example/v1")[0]
        is False
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert _model_credential("openrouter/anthropic/claude-sonnet-4", None)[0] is True


def test_cli_app_imports():
    assert app is not None


def test_task_new_uses_env_tasks_root(tmp_path: Path, monkeypatch):
    tasks_root = tmp_path / "from-env"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVO_TASKS_ROOT", str(tasks_root))

    result = CliRunner().invoke(app, ["task", "new", "demo"])

    assert result.exit_code == 0, result.output
    assert (tasks_root / "demo" / "task.toml").is_file()


def test_quickstart_threads_root_permission_and_pass_env(tmp_path: Path, monkeypatch):
    tasks_root = tmp_path / "from-env"
    calls: dict[str, dict] = {}

    def solve(task, **kwargs):
        calls["solve"] = kwargs
        return SimpleNamespace(
            eval_result=TaskResult(task_name=task, total_tests=1, failed=1), errors=[]
        )

    def reflect(skill, **kwargs):
        calls["reflect"] = kwargs
        return [{"root_cause": "test failure"}]

    def rewrite(skill, **kwargs):
        calls["rewrite"] = kwargs
        return SimpleNamespace(candidate_status="rejected", reason="test")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVO_TASKS_ROOT", str(tasks_root))
    monkeypatch.setattr("evolution.workflow.solve", solve)
    monkeypatch.setattr("evolution.workflow.reflect", reflect)
    monkeypatch.setattr("evolution.workflow.rewrite", rewrite)

    result = CliRunner().invoke(
        app,
        [
            "quickstart",
            "--permission",
            "unsafe",
            "--pass-env",
            "FIRST",
            "--pass-env",
            "SECOND",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tasks_root / "hello-evolution" / "task.toml").is_file()
    assert calls["solve"]["tasks_dir"] == tasks_root.resolve()
    assert calls["solve"]["agent_permission"] == "unsafe"
    assert calls["solve"]["pass_env"] == ["FIRST", "SECOND"]
    assert calls["reflect"]["agent_permission"] == "unsafe"
    assert calls["rewrite"]["agent_permission"] == "unsafe"
