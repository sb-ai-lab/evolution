"""End-to-end test for the ``evo run bench run`` subcommand.

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
from evolution.eval.solver import SolveResult
from evolution.eval.task import TaskResult
from evolution.llm.mock_backend import MockBackend


def _make_task_dir(root: Path, name: str, skills: list[str] | None = None) -> None:
    """Build a tiny solvable task on disk that writes output.txt."""
    task_dir = root / name
    required = ", ".join(f'"{skill}"' for skill in skills or [])
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "inputs").mkdir()
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        f'name = "{name}"\n'
        'difficulty = "easy"\n'
        'category = "test"\n\n'
        "[skills]\n"
        f"required = [{required}]\n\n"
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


@pytest.fixture(autouse=True)
def _no_inode_floor(monkeypatch) -> None:
    """The inode preflight reads the host's free inodes, which other tests and
    parallel workers consume; it has its own tests in test_compact_storage."""
    monkeypatch.setenv("EVO_MIN_FREE_INODES", "0")


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


def _bench_csvs(repo: Path) -> list[Path]:
    return sorted((repo / "results" / "mock-model").glob("results-no-skill-*.csv"))


def test_bench_run_writes_csv_and_exits_zero(isolated_repo: Path, mock_get_backend):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
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

    csvs = _bench_csvs(isolated_repo)
    assert len(csvs) == 1
    csv_path = csvs[0]
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
            "run",
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
            "run",
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

    csvs = _bench_csvs(isolated_repo)
    assert len(csvs) == 2  # fresh per-invocation CSVs, not overwritten
    assert [len(list(csv.DictReader(path.open(encoding="utf-8")))) for path in csvs] == [1, 1]


def test_bench_run_unknown_task_exits_one(isolated_repo: Path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["run", "bench", "run", "-m", "mock-model", "--tasks", "no-such-task"],
    )
    assert result.exit_code == 1
    assert "Unknown task" in result.stdout


def test_solve_accepts_relative_workspace(isolated_repo: Path, mock_get_backend):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "solve",
            "hello-task",
            "-m",
            "mock-model",
            "--no-skill",
            "--workspace",
            "relative-workspace",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (isolated_repo / "relative-workspace" / "output.txt").read_text() == "hello\n"


def test_bench_run_threads_runtime_boundary_options(isolated_repo: Path, monkeypatch):
    from evolution.eval import solver

    captured: dict = {}
    real_solve_task = solver.solve_task

    def backend_factory(_name, **kwargs):
        captured["backend"] = kwargs
        return MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello\\n")\n'
                "```"
            ]
        )

    def solve_task(*args, **kwargs):
        captured["pass_env"] = kwargs["pass_env"]
        return real_solve_task(*args, **kwargs)

    monkeypatch.setenv("EVO_LLM_MAX_TOKENS", "4321")
    monkeypatch.setattr("evolution.llm.backend.get_backend", backend_factory)
    monkeypatch.setattr("evolution.eval.solver.solve_task", solve_task)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
            "--permission",
            "unsafe",
            "--pass-env",
            "FIRST",
            "--pass-env",
            "SECOND",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["backend"]["max_tokens"] == 4321
    assert captured["backend"]["agent_permission"] == "unsafe"
    assert captured["pass_env"] == ["FIRST", "SECOND"]


def test_bench_run_passes_all_resolved_bindings(tmp_path: Path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    _make_task_dir(tasks_root, "bound-task", ["primary", "supporting"])
    sentinel = [object(), object()]
    captured: dict = {}

    def resolve_task_bindings(task, **kwargs):
        captured["requirements"] = task.skills_required
        captured["resolver"] = kwargs
        return sentinel

    def solve_task(task, backend, workspace, **kwargs):
        captured["bindings"] = kwargs["bindings"]
        assert "skills" not in kwargs
        return SolveResult(
            task_name=task.name,
            script_exit_code=0,
            eval_result=TaskResult(task_name=task.name, total_tests=1, passed=1),
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("evolution.llm.backend.get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr("evolution.workflow.resolve_task_bindings", resolve_task_bindings)
    monkeypatch.setattr("evolution.eval.solver.solve_task", solve_task)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "bound-task",
            "--skill-version",
            "v2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["requirements"] == ["primary", "supporting"]
    assert captured["resolver"]["skill_version"] == "v2"
    assert captured["resolver"]["snapshot"] is None
    assert captured["bindings"] is sentinel


def test_bench_run_does_not_downgrade_missing_binding(tmp_path: Path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    _make_task_dir(tasks_root, "bound-task", ["missing"])
    solve_called = False

    def solve_task(*args, **kwargs):
        nonlocal solve_called
        solve_called = True
        raise AssertionError("solve_task must not run without required bindings")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("evolution.llm.backend.get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr("evolution.eval.solver.solve_task", solve_task)

    result = CliRunner().invoke(
        app,
        ["run", "bench", "run", "-m", "mock-model", "--tasks", "bound-task"],
    )

    assert result.exit_code == 1, result.output
    assert "Skill 'missing' not found" in result.output
    assert solve_called is False


def test_bench_run_exits_two_on_infrastructure_error(isolated_repo: Path, monkeypatch):
    def fail_solve(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr("evolution.cli.bench._solve_one", fail_solve)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "backend unavailable" in result.output


def test_bench_run_evaluated_failure_still_exits_zero(isolated_repo: Path, monkeypatch):
    def evaluated_failure(task, *args, **kwargs):
        return SolveResult(
            task_name=task.name,
            script_exit_code=1,
            eval_result=TaskResult(task_name=task.name, total_tests=1, failed=1),
        )

    monkeypatch.setattr("evolution.llm.backend.get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr("evolution.eval.solver.solve_task", evaluated_failure)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "bench",
            "run",
            "-m",
            "mock-model",
            "--tasks",
            "hello-task",
            "--no-skill",
        ],
    )

    assert result.exit_code == 0, result.output


def test_bench_matrix_threads_runtime_options(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("evolution.cli.bench.bench_run", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(
        app,
        [
            "run",
            "bench",
            "matrix",
            "--models",
            "mock-model",
            "--conditions",
            "no-skill",
            "--permission",
            "unsafe",
            "--pass-env",
            "FIRST",
            "--pass-env",
            "SECOND",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["max_tokens"] is None
    assert calls[0]["permission"] == "unsafe"
    assert calls[0]["pass_env"] == ["FIRST", "SECOND"]


@pytest.mark.parametrize("command", ["run", "matrix"])
def test_bench_help_has_runtime_options_but_no_api_key(command: str):
    result = CliRunner().invoke(app, ["run", "bench", command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--permission" in result.output
    assert "--pass-env" in result.output
    assert "--api-key" not in result.output
