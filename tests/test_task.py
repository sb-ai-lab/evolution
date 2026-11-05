"""Tests for the task model and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.eval.task import TaskResult, discover_tasks, load_task


def _make_task(tmp_path: Path, name: str = "test-task", difficulty: str = "easy") -> Path:
    """Create a minimal task directory."""
    task_dir = tmp_path / name
    task_dir.mkdir(parents=True)
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()

    (task_dir / "task.toml").write_text(
        f'version = "1.0"\n\n'
        f"[metadata]\n"
        f'name = "{name}"\n'
        f'difficulty = "{difficulty}"\n'
        f'category = "testing"\n'
        f'tags = ["python", "test"]\n\n'
        f"[source]\n"
        f'origin = "manual"\n'
        f'task_id = "{name}"\n\n'
        f"[skills]\n"
        f'required = ["xlsx"]\n\n'
        f"[evaluation]\n"
        f'timeout_sec = "300"\n'
        f'test_file = "tests/test_outputs.py"\n'
        f'grading = "pytest"\n'
    )
    (task_dir / "instruction.md").write_text(f"# {name}\n\nDo the thing.\n")
    (tests_dir / "test_outputs.py").write_text("def test_placeholder():\n    assert True\n")
    return task_dir


class TestLoadTask:
    def test_load_basic(self, tmp_path: Path):
        task_dir = _make_task(tmp_path)
        task = load_task(task_dir)

        assert task.name == "test-task"
        assert task.difficulty == "easy"
        assert task.category == "testing"
        assert task.skills_required == ["xlsx"]
        assert "python" in task.tags
        assert "test" in task.tags
        assert task.source == "manual"

    def test_load_instruction(self, tmp_path: Path):
        task_dir = _make_task(tmp_path)
        task = load_task(task_dir)

        assert "Do the thing" in task.instruction

    def test_load_path_set(self, tmp_path: Path):
        task_dir = _make_task(tmp_path)
        task = load_task(task_dir)

        assert task.path == task_dir.resolve()

    def test_load_test_path(self, tmp_path: Path):
        task_dir = _make_task(tmp_path)
        task = load_task(task_dir)

        assert task.test_path == task_dir.resolve() / "tests" / "test_outputs.py"
        assert task.test_path.exists()

    def test_missing_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_task(tmp_path / "nonexistent")

    def test_missing_toml_raises(self, tmp_path: Path):
        task_dir = tmp_path / "no-toml"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("hello")
        with pytest.raises(FileNotFoundError):
            load_task(task_dir)

    def test_missing_instruction_raises(self, tmp_path: Path):
        task_dir = tmp_path / "no-instruction"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text('version = "1.0"\n')
        with pytest.raises(FileNotFoundError):
            load_task(task_dir)

    def test_fallback_name_from_dirname(self, tmp_path: Path):
        """If name is not in toml, use directory name."""
        task_dir = tmp_path / "my-task"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(
            'version = "1.0"\n\n[metadata]\ndifficulty = "easy"\ncategory = "test"\n'
        )
        (task_dir / "instruction.md").write_text("hello")
        task = load_task(task_dir)
        assert task.name == "my-task"


class TestDiscoverTasks:
    def test_discover_multiple(self, tmp_path: Path):
        _make_task(tmp_path, "task-a", "easy")
        _make_task(tmp_path, "task-b", "medium")
        _make_task(tmp_path, "task-c", "hard")

        tasks = discover_tasks(tmp_path)
        names = [t.name for t in tasks]

        assert len(tasks) == 3
        assert "task-a" in names
        assert "task-b" in names
        assert "task-c" in names

    def test_discover_sorted(self, tmp_path: Path):
        _make_task(tmp_path, "zzz-task")
        _make_task(tmp_path, "aaa-task")

        tasks = discover_tasks(tmp_path)
        assert tasks[0].name == "aaa-task"
        assert tasks[1].name == "zzz-task"

    def test_discover_skips_non_tasks(self, tmp_path: Path):
        _make_task(tmp_path, "real-task")
        # Directory without task.toml
        (tmp_path / "not-a-task").mkdir()
        (tmp_path / "not-a-task" / "README.md").write_text("hi")

        tasks = discover_tasks(tmp_path)
        assert len(tasks) == 1
        assert tasks[0].name == "real-task"

    def test_discover_empty_dir(self, tmp_path: Path):
        tasks = discover_tasks(tmp_path)
        assert tasks == []

    def test_discover_nonexistent(self, tmp_path: Path):
        tasks = discover_tasks(tmp_path / "nope")
        assert tasks == []


class TestTaskResult:
    def test_pass_rate(self):
        r = TaskResult(task_name="test", total_tests=10, passed=7, failed=3)
        assert r.pass_rate == 0.7

    def test_pass_rate_zero(self):
        r = TaskResult(task_name="test", total_tests=0)
        assert r.pass_rate == 0.0

    def test_success(self):
        r = TaskResult(task_name="test", total_tests=5, passed=5, failed=0)
        assert r.success is True

    def test_not_success(self):
        r = TaskResult(task_name="test", total_tests=5, passed=4, failed=1)
        assert r.success is False

    def test_not_success_empty(self):
        r = TaskResult(task_name="test", total_tests=0)
        assert r.success is False


class TestDiscoverRealTasks:
    """Test discovery against the actual tasks/ directory."""

    def test_discovers_adopted_tasks(self):
        tasks_dir = Path(__file__).parent.parent / "tasks"
        if not tasks_dir.exists():
            pytest.skip("tasks/ directory not found")

        tasks = discover_tasks(tasks_dir)
        names = [t.name for t in tasks]

        assert len(tasks) >= 6
        assert "powerlifting-coef-calc" in names
        assert "offer-letter-generator" in names
        assert "court-form-filling" in names
        assert "xlsx-recover-data" in names
        assert "econ-detrending-correlation" in names
        assert "pptx-reference-formatting" in names

    def test_skills_mapping(self):
        tasks_dir = Path(__file__).parent.parent / "tasks"
        if not tasks_dir.exists():
            pytest.skip("tasks/ directory not found")

        tasks = discover_tasks(tasks_dir)
        by_name = {t.name: t for t in tasks}

        assert by_name["powerlifting-coef-calc"].skills_required == ["xlsx"]
        assert by_name["offer-letter-generator"].skills_required == ["docx"]
        assert by_name["court-form-filling"].skills_required == ["pdf"]
        assert by_name["econ-detrending-correlation"].skills_required == ["timeseries-detrending"]
        assert by_name["pptx-reference-formatting"].skills_required == ["pptx"]

    def test_difficulty_levels(self):
        tasks_dir = Path(__file__).parent.parent / "tasks"
        if not tasks_dir.exists():
            pytest.skip("tasks/ directory not found")

        tasks = discover_tasks(tasks_dir)
        by_name = {t.name: t for t in tasks}

        assert by_name["powerlifting-coef-calc"].difficulty == "easy"
        assert by_name["xlsx-recover-data"].difficulty == "medium"
