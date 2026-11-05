"""`evo task list` must render every task without crashing.

Regression: an unexpected ``difficulty`` value (e.g. ``"middle"``) made the
renderer emit ``[]value[/]`` → ``rich.markup.MarkupError``; task-provided
strings containing ``[``/``]`` were also unescaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.eval.task import TaskResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_task(tasks_dir: Path, name: str, difficulty: str, category: str) -> None:
    d = tasks_dir / name
    d.mkdir(parents=True)
    (d / "task.toml").write_text(
        'version = "1.0"\n'
        "[metadata]\n"
        f'name = "{name}"\n'
        f'difficulty = "{difficulty}"\n'
        f'category = "{category}"\n'
        "[skills]\n"
        'required = ["demo"]\n',
        encoding="utf-8",
    )
    (d / "instruction.md").write_text("Do the thing.", encoding="utf-8")


def test_tasks_renders_unknown_difficulty_and_bracketed_fields(
    runner: CliRunner, tmp_path: Path
) -> None:
    tasks_dir = tmp_path / "tasks"
    _make_task(tasks_dir, "bad-diff", "middle", "ml[x]cat")
    _make_task(tasks_dir, "good-diff", "medium", "docs")

    result = runner.invoke(app, ["task", "list", "--dir", str(tasks_dir)])

    assert result.exit_code == 0, result.output
    assert "bad-diff" in result.output
    assert "middle" in result.output
    assert "good-diff" in result.output
    assert "medium" in result.output


def test_task_list_uses_env_root_but_explicit_dir_wins(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    env_root = tmp_path / "from-env"
    explicit_root = tmp_path / "explicit"
    _make_task(env_root, "env-task", "easy", "test")
    _make_task(explicit_root, "explicit-task", "easy", "test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVO_TASKS_ROOT", str(env_root))

    env_result = runner.invoke(app, ["task", "list"])
    explicit_result = runner.invoke(app, ["task", "list", "--dir", str(explicit_root)])

    assert env_result.exit_code == 0, env_result.output
    assert "env-task" in env_result.output
    assert "explicit-task" not in env_result.output
    assert explicit_result.exit_code == 0, explicit_result.output
    assert "explicit-task" in explicit_result.output
    assert "env-task" not in explicit_result.output


@pytest.mark.parametrize(
    "command",
    [
        ["task", "eval", "demo"],
        ["task", "eval-all"],
    ],
)
def test_task_eval_commands_pass_repeatable_env(
    runner: CliRunner, tmp_path: Path, monkeypatch, command: list[str]
) -> None:
    tasks_root = tmp_path / "tasks"
    _make_task(tasks_root, "demo", "easy", "test")
    calls: list[tuple[str, ...]] = []

    def fake_run_task(task, workspace, *, timeout=None, pass_env=()):
        calls.append(tuple(pass_env))
        return TaskResult(task_name=task.name, total_tests=1, passed=1)

    monkeypatch.setattr("evolution.eval.runner.run_task", fake_run_task)
    result = runner.invoke(
        app,
        [
            *command,
            "--workspace",
            str(tmp_path / "workspace"),
            "--dir",
            str(tasks_root),
            "--pass-env",
            "FIRST",
            "--pass-env",
            "SECOND",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("FIRST", "SECOND")]
