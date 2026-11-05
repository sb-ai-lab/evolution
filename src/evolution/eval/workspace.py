"""Workspace setup — prepare input files for task evaluation."""

from __future__ import annotations

from pathlib import Path

from evolution.eval.task import Task


def setup_workspace(task: Task, workspace: Path) -> Path:
    """Stage a task's input files into a workspace directory.

    Delegates to the task's layout: the evo native layout copies
    ``inputs/*`` into the workspace root; the sb-bench native layout
    mirrors the task tree (``environment/``, ``solve.sh``, …). Returns the
    workspace path.
    """
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    task.resolve_layout().stage_workspace(task.path, workspace)
    return workspace
