"""Tests for the task solver."""

from __future__ import annotations

import json
from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter
from evolution.eval.solver import _extract_python_code, solve_task
from evolution.eval.task import Task
from evolution.lineage.traces import TraceStore
from evolution.llm.mock_backend import MockBackend
from evolution.splits import SplitManifest


def _make_solvable_task(tmp_path: Path) -> tuple[Task, Path]:
    """Create a task that can be solved by writing a file."""
    task_dir = tmp_path / "write-file-task"
    tests_dir = task_dir / "tests"
    inputs_dir = task_dir / "inputs"
    tests_dir.mkdir(parents=True)
    inputs_dir.mkdir()

    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'name = "write-file-task"\n'
        'difficulty = "easy"\n'
        'category = "test"\n\n'
        "[skills]\n"
        'required = ["test-skill"]\n\n'
        "[evaluation]\n"
        'timeout_sec = "30"\n'
        'test_file = "tests/test_outputs.py"\n'
    )
    (task_dir / "instruction.md").write_text("Write 'hello world' to output.txt")
    (tests_dir / "test_outputs.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        'WORKSPACE = Path(os.environ.get("TASK_WORKSPACE", "/root"))\n\n'
        "def test_output_exists():\n"
        '    assert (WORKSPACE / "output.txt").exists()\n\n'
        "def test_output_content():\n"
        '    assert (WORKSPACE / "output.txt").read_text().strip() == "hello world"\n'
    )

    # Input file
    (inputs_dir / "input.txt").write_text("some input\n")

    task = Task(
        name="write-file-task",
        instruction="Write 'hello world' to output.txt",
        difficulty="easy",
        category="test",
        skills_required=["test-skill"],
        timeout_sec=30.0,
        test_file="tests/test_outputs.py",
        path=task_dir,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Copy inputs
    from evolution.eval.workspace import setup_workspace

    setup_workspace(task, workspace)

    return task, workspace


def _make_skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nUse Python carefully.\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="Use Python carefully.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _write_split_manifest(tmp_path: Path, *, task_id: str) -> SplitManifest:
    path = tmp_path / "split_manifest.json"
    path.write_text(
        json.dumps(
            {
                "name": "unit-solver-split",
                "tasks": [{"id": task_id, "split": "train"}],
            }
        ),
        encoding="utf-8",
    )
    return SplitManifest.load(path)


class TestExtractPythonCode:
    def test_python_block(self):
        text = 'Here is code:\n```python\nprint("hello")\n```\nDone.'
        assert _extract_python_code(text) == 'print("hello")'

    def test_py_block(self):
        text = '```py\nprint("hello")\n```'
        assert _extract_python_code(text) == 'print("hello")'

    def test_generic_block(self):
        text = '```\nprint("hello")\n```'
        assert _extract_python_code(text) == 'print("hello")'

    def test_bare_code(self):
        text = 'import os\nprint("hello")'
        assert _extract_python_code(text) == 'import os\nprint("hello")'

    def test_bare_from_import_code(self):
        text = 'from pathlib import Path\nPath("output.txt").write_text("ok")'
        assert _extract_python_code(text) == text

    def test_bare_code_with_module_docstring(self):
        text = '"""Create output."""\nfrom pathlib import Path\nPath("output.txt").write_text("ok")'
        assert _extract_python_code(text) == text

    def test_no_code(self):
        text = "This is just text with no code."
        assert _extract_python_code(text) == ""

    def test_generic_non_python_block(self):
        text = "```\nThis is a fenced explanation, not code.\n```"
        assert _extract_python_code(text) == ""

    def test_multiline_code(self):
        text = '```python\nimport os\nfrom pathlib import Path\n\nprint("hi")\n```'
        code = _extract_python_code(text)
        assert "import os" in code
        assert 'print("hi")' in code


class TestSolveTask:
    def test_successful_solve(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        # LLM returns code that writes the expected file
        backend = MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello world\\n")\n'
                'print("Done")\n'
                "```"
            ]
        )

        result = solve_task(task, backend, workspace)

        assert result.llm_response is not None
        assert result.script_exit_code == 0
        assert result.eval_result is not None
        assert result.eval_result.passed == 2
        assert result.success is True
        assert result.pass_rate == 1.0

    def test_wrong_output(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("wrong answer\\n")\n'
                "```"
            ]
        )

        result = solve_task(task, backend, workspace)

        assert result.script_exit_code == 0
        assert result.eval_result is not None
        assert result.eval_result.failed > 0
        assert result.success is False

    def test_script_error(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(responses=['```python\nraise ValueError("oops")\n```'])

        result = solve_task(task, backend, workspace)

        assert result.script_exit_code != 0
        assert "Script exited with code" in result.errors[0]

    def test_no_code_in_response(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(responses=["I cannot solve this task."])

        result = solve_task(task, backend, workspace)

        assert result.success is False
        assert any("No Python code" in e for e in result.errors)

    def test_no_evaluate(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(responses=['```python\nprint("no output file")\n```'])

        result = solve_task(task, backend, workspace, evaluate=False)

        assert result.eval_result is None
        assert result.script_exit_code == 0

    def test_input_files_available(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        # Verify input was copied
        assert (workspace / "input.txt").exists()

    def test_records_llm_call(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(responses=['```python\nprint("hi")\n```'])

        solve_task(task, backend, workspace, evaluate=False)

        assert len(backend.calls) == 1
        assert "hello world" in backend.calls[0]["messages"][0]["content"]

    def test_trace_evidence_artifacts_are_captured_before_pytest(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)
        skill = _make_skill(tmp_path)
        # The verifier writes a file whose name/content must not appear in the
        # trace evidence artifact manifest. Evidence is supposed to describe
        # the agent solve step, not pytest side effects.
        task.test_path.write_text(
            "import os\n"
            "from pathlib import Path\n\n"
            'WORKSPACE = Path(os.environ.get("TASK_WORKSPACE", "/root"))\n\n'
            "def test_output_exists_and_verifier_side_effect():\n"
            '    (WORKSPACE / "verifier_artifact_secret.txt").write_text("EXPECTED_SENTINEL")\n'
            '    assert (WORKSPACE / "output.txt").exists()\n',
            encoding="utf-8",
        )
        backend = MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello world\\n")\n'
                "```"
            ]
        )

        result = solve_task(task, backend, workspace, skill=skill, model_name="solver-model")

        assert result.eval_result is not None
        assert result.eval_result.passed == 1
        traces = TraceStore(skill).list()
        assert len(traces) == 1
        evidence = json.loads(
            (TraceStore(skill).traces_dir / traces[0]["id"] / "evidence.json").read_text(
                encoding="utf-8"
            )
        )
        artifact_paths = {
            entry.get("path")
            for entry in evidence["produced_artifacts"]
            if isinstance(entry, dict)
        }
        assert "output.txt" in artifact_paths
        assert "verifier_artifact_secret.txt" not in artifact_paths

    def test_trace_records_split_manifest_identity(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)
        skill = _make_skill(tmp_path)
        manifest = _write_split_manifest(tmp_path, task_id=task.name)
        backend = MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello world\\n")\n'
                "```"
            ]
        )

        solve_task(
            task,
            backend,
            workspace,
            skill=skill,
            model_name="solver-model",
            manifest=manifest,
            manifest_scope="skill_evolution",
        )

        traces = TraceStore(skill).list()
        assert len(traces) == 1
        assert traces[0]["manifest_name"] == manifest.name
        assert traces[0]["manifest_sha"] == manifest.sha256
        assert traces[0]["manifest_scope"] == "skill_evolution"

    def test_trace_evidence_saved_for_solve_timeout(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)
        skill = _make_skill(tmp_path)
        backend = MockBackend(
            responses=[
                "```python\n"
                "import time\n"
                "time.sleep(5)\n"
                "```"
            ]
        )

        result = solve_task(
            task,
            backend,
            workspace,
            skill=skill,
            model_name="solver-model",
            timeout=0.2,
        )

        assert result.eval_result is None
        assert any("timed out" in e for e in result.errors)
        traces = TraceStore(skill).list()
        assert len(traces) == 1
        evidence = json.loads(
            (TraceStore(skill).traces_dir / traces[0]["id"] / "evidence.json").read_text(
                encoding="utf-8"
            )
        )
        assert evidence["agent_solve"]["timeout"] is True
        assert evidence["agent_solve"]["diagnostic"] == "timeout"
