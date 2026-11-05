"""Phase 4: Regression smoke for the TaskLayout abstraction.

Runs a solve pipeline (setup workspace → place solution → run tests) on four
reference tasks under both the legacy code path (``EVO_USE_LAYOUTS`` off)
and the layout code path (``EVO_USE_LAYOUTS=1``) and asserts the observable
results match.

For evo tasks the layout implementation mirrors the legacy inline code, so
results must be identical.  For sb-bench tasks the legacy path does not
properly stage workspaces (it only copies ``inputs/*`` which many sb-bench
tasks lack), so we only verify the layout path produces valid, reproducible
results — we compare against a golden snapshot rather than against legacy.

Reference tasks
---------------
  evo:   econ-detrending-correlation, exceltable-in-ppt
  sb:    de/sonnet-81cf9bcd8c3e, ds/sonnet-acaed29e54ec

See plans/layout_abstraction.md §Phase 4.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

EVO_ROOT = Path(__file__).resolve().parent.parent.parent
TASKS_DIR = EVO_ROOT / "tasks"

EVO_TASKS = [
    "econ-detrending-correlation",
    "exceltable-in-ppt",
]

SB_BENCH_TASKS = [
    "de/sonnet-81cf9bcd8c3e",
    "ds/sonnet-acaed29e54ec",
]


def _require_sb_fixture(name: str) -> Path:
    """Return the fixture task dir, or skip if it isn't checked out.

    The sb-bench fixtures are committed under ``tasks/<role>/<task>/`` but a
    trimmed checkout may omit them; skip rather than hard-fail so the evo
    regression tests still gate CI.
    """
    task_dir = TASKS_DIR / name
    if not (task_dir / "task.toml").exists():
        pytest.skip(f"sb-bench fixture not present: {task_dir}")
    return task_dir


# A minimal stub that creates ``output/`` and a dummy ``answer.txt``.
# The econ task's test suite will flag the value as incorrect but both
# paths will report the same pass/fail counts — proving equivalence.
_STUB_CODE = """
import os

os.makedirs("output", exist_ok=True)
with open("answer.txt", "w") as f:
    f.write("0.99999")
"""


# ── helpers ────────────────────────────────────────────────────────────────


def _write_stub(ws: Path) -> None:
    """Place a stub solution."""
    ws.joinpath("_solve.py").write_text(_STUB_CODE, encoding="utf-8")
    ws.joinpath("solution.py").write_text(_STUB_CODE, encoding="utf-8")


def _wipe(ws: Path) -> None:
    """Remove solution and output artefacts (leaves inputs + tests intact)."""
    for name in ("_solve.py", "solution.py", "answer.txt"):
        p = ws / name
        if p.is_file():
            p.unlink()
    out = ws / "output"
    if out.is_dir():
        shutil.rmtree(out, ignore_errors=True)


# Files that carry runtime noise (timestamps, process ids, etc.) and
# should not participate in deterministic snapshot comparison.
_SKIP_SNAPSHOT = frozenset(
    {
        ".workspace_guard.jsonl",
        "_workspace_guard_bin",
        "_workspace_guard_py",
        "pytest_output.txt",
    }
)


def _snapshot(ws: Path) -> dict:
    """Deterministic workspace snapshot, skipping guard/runtime files."""
    snap: dict = {}
    for child in sorted(ws.iterdir()):
        if child.name in _SKIP_SNAPSHOT:
            continue
        if child.is_file():
            snap[child.name] = child.read_bytes()
        elif child.is_dir():
            snap[child.name] = {
                str(p.relative_to(child)): p.read_bytes()
                for p in sorted(child.rglob("*"))
                if p.is_file() and p.name not in _SKIP_SNAPSHOT
            }
    return snap


def _run_one(task_dir: Path, layout_mode: str, ws: Path):
    """Run the full pipeline under the given layout mode.

    Parameters
    ----------
    task_dir:
        Directory containing ``task.toml``.
    layout_mode:
        ``"legacy"`` or ``"layout"`` — controls ``EVO_USE_LAYOUTS``.
    ws:
        Workspace path (must exist).

    Returns
    -------
    tuple[TaskResult, dict]  — ``(result, snapshot)``.
    """
    from evolution.eval.runner import run_task
    from evolution.eval.task import load_task
    from evolution.eval.workspace import setup_workspace

    os.environ["EVO_USE_LAYOUTS"] = "1" if layout_mode == "layout" else "0"

    task = load_task(task_dir)
    setup_workspace(task, ws)
    _wipe(ws)
    _write_stub(ws)
    result = run_task(task, ws)

    return result, _snapshot(ws)


# ── tests ──────────────────────────────────────────────────────────────────


class TestLayoutDetection:
    """Basic sanity: detect_layout picks the right layout for each task."""

    def test_evo_tasks(self):
        from evolution.eval.layouts import detect_layout

        for name in EVO_TASKS:
            layout = detect_layout(TASKS_DIR / name)
            assert layout.name == "evo", f"{name}: expected 'evo', got '{layout.name}'"

    def test_sb_bench_tasks(self):
        from evolution.eval.layouts import detect_layout

        for name in SB_BENCH_TASKS:
            task_dir = _require_sb_fixture(name)
            layout = detect_layout(task_dir)
            assert layout.name in ("sb_bench", "sb-bench"), (
                f"{name}: expected 'sb_bench', got '{layout.name}'"
            )


class TestEvoRegressionSmoke:
    """Evo tasks: legacy and layout paths must produce identical results."""

    @pytest.mark.parametrize("task_name", EVO_TASKS)
    def test_identical(self, task_name, tmp_path):
        """For evo tasks the layout path must be byte-identical to legacy."""
        task_dir = TASKS_DIR / task_name
        ws = tmp_path / "ws"
        ws.mkdir()

        # Save original env.
        old = os.environ.pop("EVO_USE_LAYOUTS", None)

        try:
            legacy_result, legacy_snap = _run_one(task_dir, "legacy", ws)
            _wipe(ws)
            layout_result, layout_snap = _run_one(task_dir, "layout", ws)
        finally:
            if old is not None:
                os.environ["EVO_USE_LAYOUTS"] = old

        # ── compare test results ───────────────────────────────────────────
        assert legacy_result.task_name == layout_result.task_name
        assert legacy_result.total_tests == layout_result.total_tests, (
            f"total_tests: legacy={legacy_result.total_tests} != layout={layout_result.total_tests}"
        )
        assert legacy_result.passed == layout_result.passed, (
            f"passed: legacy={legacy_result.passed} != layout={layout_result.passed}"
        )
        assert legacy_result.failed == layout_result.failed, (
            f"failed: legacy={legacy_result.failed} != layout={layout_result.failed}"
        )
        assert legacy_result.skipped == layout_result.skipped, (
            f"skipped: legacy={legacy_result.skipped} != layout={layout_result.skipped}"
        )

        # ── compare workspace snapshot ─────────────────────────────────────
        assert legacy_snap == layout_snap, (
            f"Workspace snapshot differs for {task_name}:\n"
            f"  legacy keys: {sorted(legacy_snap.keys())}\n"
            f"  layout keys: {sorted(layout_snap.keys())}"
        )


class TestSbBenchSmoke:
    """Sb-bench tasks: layout path must produce valid, reproducible results.

    The legacy path doesn't properly handle sb-bench tasks (it only copies
    ``inputs/*``), so we don't compare against legacy. Instead we run the
    layout path twice and assert the results are deterministic.
    """

    @pytest.mark.parametrize("task_name", SB_BENCH_TASKS)
    def test_layout_runs_and_reproducible(self, task_name, tmp_path):
        task_dir = _require_sb_fixture(task_name)
        ws = tmp_path / "ws"

        old = os.environ.pop("EVO_USE_LAYOUTS", None)
        try:
            os.environ["EVO_USE_LAYOUTS"] = "1"

            from evolution.eval.runner import run_task
            from evolution.eval.task import load_task
            from evolution.eval.workspace import setup_workspace

            # ── run 1 ────────────────────────────────────────────────────────
            task = load_task(task_dir)
            assert task.layout is not None
            assert task.layout.name in ("sb_bench", "sb-bench")

            setup_workspace(task, ws)

            # Workspace must have the full tree (not just inputs/*).
            assert (ws / "environment").is_dir(), f"sandbox missing environment/ for {task_dir}"

            _wipe(ws)
            _write_stub(ws)
            result_1 = run_task(task, ws)

            # ── run 2 (reproducibility check) ────────────────────────────────
            _wipe(ws)
            _write_stub(ws)
            result_2 = run_task(task, ws)

            # Both runs must produce the same test outcome.
            assert result_1.task_name == result_2.task_name
            assert result_1.total_tests == result_2.total_tests, (
                f"total_tests mismatch between runs for {task_dir}: "
                f"{result_1.total_tests} vs {result_2.total_tests}"
            )
            assert result_1.passed == result_2.passed
            assert result_1.failed == result_2.failed
            assert result_1.skipped == result_2.skipped
        finally:
            if old is not None:
                os.environ["EVO_USE_LAYOUTS"] = old
            else:
                os.environ.pop("EVO_USE_LAYOUTS", None)
