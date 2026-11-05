"""`evo tasks` must render every task without crashing.

Regression: an unexpected ``difficulty`` value (e.g. ``"middle"``) made the
renderer emit ``[]value[/]`` → ``rich.markup.MarkupError``; task-provided
strings containing ``[``/``]`` were also unescaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app


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

    result = runner.invoke(app, ["tasks", "--dir", str(tasks_dir)])

    assert result.exit_code == 0, result.output
    assert "bad-diff" in result.output
    assert "middle" in result.output
    assert "good-diff" in result.output
    assert "medium" in result.output
