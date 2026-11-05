"""End-to-end test for the ``evo bench run`` subcommand.

Patches ``get_backend`` to return a deterministic MockBackend so the test
runs offline and exercises the full pipeline: task discovery, workspace
setup, solve, eval, CSV append.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.llm.mock_backend import MockBackend


def _make_task_dir(root: Path, name: str) -> None:
    """Build a tiny solvable task on disk that writes output.txt."""
    task_dir = root / name
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "inputs").mkdir()
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        f'name = "{name}"\n'
        'difficulty = "easy"\n'
        'category = "test"\n\n'
        "[skills]\n"
        "required = []\n\n"
        "[evaluation]\n"
        'timeout_sec = "30"\n'
        'test_file = "tests/test_outputs.py"\n',
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("Write hello to output.txt")
    (task_dir / "tests" / "test_outputs.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        'WORKSPACE = Path(os.environ.get("TASK_WORKSPACE", "/root"))\n\n'
        "def test_output_exists():\n"
        '    assert (WORKSPACE / "output.txt").exists()\n',
        encoding="utf-8",
    )


@pytest.fixture
def isolated_repo(tmp_path: Path, monkeypatch) -> Path:
    """A tmp working directory with one solvable task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _make_task_dir(tasks_dir, "hello-task")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def mock_get_backend(monkeypatch):
    """Replace evolution.llm.backend.get_backend with a MockBackend factory."""

    def _factory(_name, **_kwargs):
        return MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello\\n")\n'
                "```"
            ]
        )

    monkeypatch.setattr("evolution.llm.backend.get_backend", _factory)
    return _factory


def test_bench_run_writes_csv_and_exits_zero(isolated_repo: Path, mock_get_backend):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
        ],
    )
    assert result.exit_code == 0, result.stdout

    csv_path = isolated_repo / "results" / "mock-model" / "results.csv"
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "mock-model"
    assert row["task"] == "hello-task"
    assert row["condition"] == "no-skill"
    assert row["trial"] == "1"
    assert int(row["passed"]) == 1
    assert int(row["total"]) == 1
    assert int(row["exit_code"]) == 0


def test_bench_run_appends_across_trials(isolated_repo: Path, mock_get_backend):
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
            "--trials",
            "1",
        ],
    )
    runner.invoke(
        app,
        [
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
            "--trials",
            "1",
        ],
    )

    csv_path = isolated_repo / "results" / "mock-model" / "results.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2  # appended, not overwritten


def test_bench_run_unknown_task_exits_one(isolated_repo: Path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bench", "run", "-m", "mock-model", "--tasks", "no-such-task"],
    )
    assert result.exit_code == 1
    assert "Unknown task" in result.stdout
