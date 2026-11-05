"""Tests for the task evaluation runner."""

from __future__ import annotations

from pathlib import Path

from evolution.eval.runner import run_task
from evolution.eval.task import Task


def _make_task_with_test(tmp_path: Path, test_code: str) -> Task:
    """Create a task with a custom test file."""
    task_dir = tmp_path / "my-task"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_outputs.py").write_text(test_code)
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'name = "my-task"\n'
        'difficulty = "easy"\n'
        'category = "test"\n\n'
        "[skills]\n"
        'required = ["test-skill"]\n\n'
        "[evaluation]\n"
        'timeout_sec = "30"\n'
        'test_file = "tests/test_outputs.py"\n'
    )
    (task_dir / "instruction.md").write_text("Do the thing.")

    return Task(
        name="my-task",
        instruction="Do the thing.",
        difficulty="easy",
        category="test",
        skills_required=["test-skill"],
        timeout_sec=30.0,
        test_file="tests/test_outputs.py",
        path=task_dir,
    )


class TestRunTask:
    def test_all_pass(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        task = _make_task_with_test(
            tmp_path,
            """
import os
from pathlib import Path

def test_one():
    assert True

def test_two():
    assert 1 + 1 == 2
""",
        )
        result = run_task(task, workspace)
        assert result.passed >= 2
        assert result.failed == 0
        assert result.success is True
        assert result.pass_rate == 1.0

    def test_some_fail(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        task = _make_task_with_test(
            tmp_path,
            """
def test_pass():
    assert True

def test_fail():
    assert False, "intentional failure"
""",
        )
        result = run_task(task, workspace)
        assert result.passed == 1
        assert result.failed == 1
        assert result.success is False

    def test_workspace_env_var(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "output.txt").write_text("hello")

        task = _make_task_with_test(
            tmp_path,
            """
import os
from pathlib import Path

def test_workspace_set():
    ws = os.environ.get("TASK_WORKSPACE")
    assert ws is not None

def test_output_exists():
    ws = Path(os.environ["TASK_WORKSPACE"])
    assert (ws / "output.txt").exists()

def test_output_content():
    ws = Path(os.environ["TASK_WORKSPACE"])
    assert (ws / "output.txt").read_text() == "hello"
""",
        )
        result = run_task(task, workspace)
        assert result.passed == 3
        assert result.failed == 0
        assert result.success is True

    def test_missing_workspace(self, tmp_path: Path):
        task = _make_task_with_test(tmp_path, "def test_x(): pass")
        result = run_task(task, tmp_path / "nonexistent")
        assert result.success is False
        assert len(result.errors) > 0

    def test_timeout(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        task = _make_task_with_test(
            tmp_path,
            """
import time
def test_slow():
    time.sleep(60)
""",
        )
        task.timeout_sec = 2.0
        result = run_task(task, workspace, timeout=2.0)
        assert result.success is False
        assert any("Timeout" in e for e in result.errors)


class TestRunTaskRawOutput:
    """Phase-0 plumbing: raw pytest output + node ids reach TaskResult."""

    def test_raw_stdout_and_nodeids(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        task = _make_task_with_test(
            tmp_path,
            """
def test_pass():
    assert True

def test_fail():
    assert False, "intentional failure"
""",
        )
        result = run_task(task, workspace)
        assert result.raw_stdout
        assert any(n.endswith("::test_pass") for n in result.nodeids)
        assert any(n.endswith("::test_fail") for n in result.nodeids)

    def test_pytest_output_file_written(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        task = _make_task_with_test(tmp_path, "def test_x():\n    assert True\n")
        run_task(task, workspace)
        out = workspace / "pytest_output.txt"
        assert out.is_file()
        assert "test_x" in out.read_text()

    def test_pytest_output_file_written_on_timeout(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        task = _make_task_with_test(
            tmp_path,
            "import time\ndef test_slow():\n    time.sleep(60)\n",
        )
        task.timeout_sec = 2.0
        run_task(task, workspace, timeout=2.0)
        assert (workspace / "pytest_output.txt").is_file()
