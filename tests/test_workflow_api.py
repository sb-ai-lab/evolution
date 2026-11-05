"""Framework-level lifecycle tests for non-paper, custom tasks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from evolution.core.models import SkillTrace
from evolution.core.store import SkillStore
from evolution.evolve.rewrite_outcome import PROMOTED_ELIGIBLE
from evolution.lineage.traces import TraceStore
from evolution.llm.mock_backend import MockBackend
from evolution.workflow import commit_skill, list_traces, load_trace, reflect, rewrite, solve


def _make_custom_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    tasks_dir = tmp_path / "custom-corpus"
    task_dir = tasks_dir / "write-message"
    tests_dir = task_dir / "tests"
    inputs_dir = task_dir / "inputs"
    tests_dir.mkdir(parents=True)
    inputs_dir.mkdir(parents=True)

    SkillStore(project_dir=project).create(
        name="generic-writer",
        description="Write simple requested output files",
    )

    (task_dir / "task.toml").write_text(
        'version = "1.0"\n'
        "[metadata]\n"
        'name = "write-message"\n'
        'difficulty = "easy"\n'
        'category = "custom-demo"\n'
        'origin = "custom"\n'
        "[skills]\n"
        'required = ["generic-writer"]\n'
        "[evaluation]\n"
        'timeout_sec = "30"\n'
        'test_file = "tests/test_outputs.py"\n',
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text(
        "Read `source.txt` and write `output/message.txt` containing exactly `expected`.",
        encoding="utf-8",
    )
    (inputs_dir / "source.txt").write_text("expected\n", encoding="utf-8")
    (tests_dir / "test_outputs.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "WORKSPACE = Path(os.environ['TASK_WORKSPACE'])\n\n"
        "def test_message_file():\n"
        "    assert (WORKSPACE / 'output' / 'message.txt').read_text().strip() == 'expected'\n",
        encoding="utf-8",
    )
    return project, tasks_dir, tmp_path / "workspace"


def test_custom_task_lifecycle_uses_framework_api(tmp_path: Path, monkeypatch) -> None:
    project, tasks_dir, workspace = _make_custom_project(tmp_path)
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "1")

    bad_solver = MockBackend(
        responses=[
            "```python\n"
            "from pathlib import Path\n"
            "Path('output').mkdir(exist_ok=True)\n"
            "Path('output/message.txt').write_text('wrong\\n')\n"
            "```"
        ]
    )
    result = solve(
        "write-message",
        model="mock",
        backend=bad_solver,
        project_dir=project,
        tasks_dir=tasks_dir,
        workspace=workspace,
    )

    assert result.success is False
    traces = list_traces("generic-writer", project_dir=project, task="write-message")
    assert len(traces) == 1
    assert traces[0]["task"] == "write-message"

    reflector = MockBackend(
        responses=[
            json.dumps(
                {
                    "root_cause": "the output file contains the wrong value",
                    "skill_gap": "the skill does not remind the solver to copy the input value",
                    "suggested_fix": "read the provided source file and verify the final output",
                    "severity": "major",
                    "pattern": "logic_error",
                    "evidence_sufficiency": "sufficient",
                    "rewrite_recommended": True,
                }
            )
        ]
    )
    reflections = reflect(
        "generic-writer",
        backend=reflector,
        model="mock",
        project_dir=project,
        tasks_dir=tasks_dir,
        task="write-message",
    )

    assert len(reflections) == 1
    assert reflections[0]["rewrite_recommended"] is True

    rewriter = MockBackend(
        responses=[
            "## When to use this skill\n"
            "Use when a task asks for a small deterministic output file.\n\n"
            "## Steps\n"
            "- Read the task inputs before writing outputs.\n"
            "- Verify the final output file content before stopping.\n"
        ]
    )
    outcome = rewrite(
        "generic-writer",
        backend=rewriter,
        model="mock",
        project_dir=project,
        tasks_dir=tasks_dir,
        task="write-message",
        apply=True,
    )

    assert outcome.candidate_status == PROMOTED_ELIGIBLE
    assert "Verify the final output" in outcome.body
    new_version = commit_skill("generic-writer", project_dir=project)
    assert new_version == "v2"


def test_load_trace_rejects_path_traversal_and_symlink_reads(tmp_path: Path) -> None:
    project = tmp_path / "project"
    skills = SkillStore(project_dir=project)
    skills.create(name="safe-reader", description="Test trace boundaries")
    skill = skills.get("safe-reader")
    trace = SkillTrace(
        id="safe-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="mock",
        task="task",
        timestamp=datetime.now(),
    )
    trace_dir = TraceStore(skill).save(trace, "prompt", "response", "code")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid trace id"):
        load_trace("safe-reader", "../../secret.txt", project_dir=project)

    (trace_dir / "prompt.md").unlink()
    (trace_dir / "prompt.md").symlink_to(secret)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        load_trace("safe-reader", "safe-trace", project_dir=project, sections=("prompt",))
