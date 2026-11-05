"""Regression smoke for task-layout detection, staging, and evaluation.

The fixtures are generated in a temporary directory so this package test does
not depend on the ignored task corpus or the external SB-Bench checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.eval.layouts import detect_layout
from evolution.eval.runner import run_task
from evolution.eval.task import load_task
from evolution.eval.workspace import setup_workspace
from evolution.eval.workspace_materializer import WorkspaceMaterializationError

_TEST_SOURCE = """from __future__ import annotations

import os
from pathlib import Path


def test_answer() -> None:
    workspace = Path(
        os.environ.get("TASK_WORKSPACE")
        or os.environ.get("WORKSPACE_DIR")
        or "."
    )
    assert (workspace / "answer.txt").read_text(encoding="utf-8") == "ok"
"""

_SB_TEST_SOURCE = """from __future__ import annotations

from pathlib import Path


def test_answer() -> None:
    root = Path(__file__).resolve().parents[1]
    expected = Path(__file__).with_name("expected.txt").read_text(encoding="utf-8")
    assert (root / "output" / "answer.txt").read_text(encoding="utf-8") == expected
"""


def _write_evo_task(root: Path) -> Path:
    task = root / "synthetic-evo"
    (task / "inputs").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text(
        'version = "1.0"\n'
        "[metadata]\n"
        'name = "synthetic-evo"\n'
        'difficulty = "easy"\n'
        'category = "test"\n'
        "[skills]\n"
        "required = []\n"
        "[evaluation]\n"
        'test_file = "tests/test_outputs.py"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Write answer.txt.", encoding="utf-8")
    (task / "inputs" / "seed.txt").write_text("evo", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(_TEST_SOURCE, encoding="utf-8")
    return task


def _write_sb_bench_task(root: Path) -> Path:
    task = root / "synthetic-sb"
    (task / "environment" / "data").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "source_artifacts").mkdir()
    (task / "task.toml").write_text(
        "[task]\n"
        'id = "synthetic-sb"\n'
        'name = "Synthetic SB task"\n'
        'difficulty = "easy"\n'
        'skills = ["model_training"]\n'
        "[verification]\n"
        'verifier_entrypoint = "tests/test_outputs.py"\n'
        "timeout_seconds = 30\n",
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Write answer.txt.", encoding="utf-8")
    (task / "environment" / "data" / "seed.txt").write_text("sb", encoding="utf-8")
    (task / "solve.sh").write_text("python3 solution.py\n", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(_SB_TEST_SOURCE, encoding="utf-8")
    (task / "tests" / "expected.txt").write_text("ok", encoding="utf-8")
    (task / "tests" / ".ground_truth.json").write_text("oracle", encoding="utf-8")
    (task / "source_artifacts" / "raw.json").write_text("oracle", encoding="utf-8")
    (task / "data_generator.py").write_text("# setup only\n", encoding="utf-8")
    (task / "solution.py").write_text("# oracle solution\n", encoding="utf-8")
    return task


@pytest.fixture
def layout_tasks(tmp_path: Path) -> dict[str, Path]:
    return {
        "evo": _write_evo_task(tmp_path),
        "sb_bench": _write_sb_bench_task(tmp_path),
    }


def test_detects_layouts_from_synthetic_formats(layout_tasks: dict[str, Path]) -> None:
    for expected, task_dir in layout_tasks.items():
        assert detect_layout(task_dir).name == expected
        assert load_task(task_dir).resolve_layout().name == expected


def test_sb_layout_normalizes_legacy_skill_ids(layout_tasks: dict[str, Path]) -> None:
    assert load_task(layout_tasks["sb_bench"]).skills_required == ["model-training"]


@pytest.mark.parametrize("layout_name", ["evo", "sb_bench"])
def test_workspace_staging_preserves_layout_contract(
    layout_name: str,
    layout_tasks: dict[str, Path],
    tmp_path: Path,
) -> None:
    task = load_task(layout_tasks[layout_name])
    workspace = tmp_path / f"workspace-{layout_name}"

    setup_workspace(task, workspace)

    if layout_name == "evo":
        assert (workspace / "seed.txt").read_text(encoding="utf-8") == "evo"
        assert not (workspace / "environment").exists()
    else:
        assert (workspace / "environment" / "data" / "seed.txt").read_text(encoding="utf-8") == "sb"
        assert (workspace / "solve.sh").is_file()
        for hidden in ("tests", "source_artifacts", "data_generator.py", "solution.py"):
            assert not (workspace / hidden).exists()


@pytest.mark.parametrize("verifier_entrypoint", ["checks/verify.py", "verify.py"])
def test_sb_workspace_hides_configured_verifier(
    layout_tasks: dict[str, Path], tmp_path: Path, verifier_entrypoint: str
) -> None:
    task_dir = layout_tasks["sb_bench"]
    task_toml = task_dir / "task.toml"
    task_toml.write_text(
        task_toml.read_text(encoding="utf-8").replace("tests/test_outputs.py", verifier_entrypoint),
        encoding="utf-8",
    )
    verifier = task_dir / verifier_entrypoint
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text(_SB_TEST_SOURCE, encoding="utf-8")
    if verifier.parent != task_dir:
        (verifier.parent / "oracle_helper.py").write_text("SECRET = 42\n", encoding="utf-8")

    workspace = tmp_path / "configured-verifier"
    setup_workspace(load_task(task_dir), workspace)

    assert not (workspace / verifier_entrypoint).exists()
    if verifier.parent != task_dir:
        assert not (workspace / verifier.parent.relative_to(task_dir)).exists()


def test_sb_workspace_rejects_symlinked_verifier_tree(
    layout_tasks: dict[str, Path], tmp_path: Path
) -> None:
    task_dir = layout_tasks["sb_bench"]
    external = tmp_path / "external-tests"
    external.mkdir()
    (external / "test_outputs.py").write_text(_SB_TEST_SOURCE, encoding="utf-8")
    tests_dir = task_dir / "tests"
    for child in tests_dir.iterdir():
        child.unlink()
    tests_dir.rmdir()
    tests_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(WorkspaceMaterializationError, match="symlink"):
        setup_workspace(load_task(task_dir), tmp_path / "symlinked-verifier")


@pytest.mark.parametrize("layout_name", ["evo", "sb_bench"])
def test_layout_evaluation_is_reproducible(
    layout_name: str,
    layout_tasks: dict[str, Path],
    tmp_path: Path,
) -> None:
    task = load_task(layout_tasks[layout_name])
    workspace = tmp_path / f"evaluation-{layout_name}"
    setup_workspace(task, workspace)
    if layout_name == "sb_bench":
        (workspace / "output").mkdir(exist_ok=True)
        (workspace / "output" / "answer.txt").write_text("ok", encoding="utf-8")
        (workspace / "tests").mkdir()
        (workspace / "tests" / "test_outputs.py").write_text(
            "def test_shadow(): assert False\n", encoding="utf-8"
        )
    else:
        (workspace / "answer.txt").write_text("ok", encoding="utf-8")

    first = run_task(task, workspace)
    second = run_task(task, workspace)

    assert first.success is True
    assert second.success is True
    assert (
        first.total_tests,
        first.passed,
        first.failed,
        first.skipped,
    ) == (
        second.total_tests,
        second.passed,
        second.failed,
        second.skipped,
    )


def test_sb_verifier_replaces_shadow_test_symlink_without_following_it(
    layout_tasks: dict[str, Path], tmp_path: Path
) -> None:
    task = load_task(layout_tasks["sb_bench"])
    workspace = tmp_path / "evaluation-symlink"
    setup_workspace(task, workspace)
    (workspace / "output" / "answer.txt").write_text("ok", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (workspace / "tests").symlink_to(victim, target_is_directory=True)

    result = run_task(task, workspace)

    assert result.success is True
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (workspace / "tests").is_symlink()
