"""Tests for the task solver."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from evolution.core.bindings import SkillBinding
from evolution.core.models import Skill, SkillFrontmatter
from evolution.core.store import SkillStore
from evolution.eval.layouts.base import CollectResult
from evolution.eval.solver import _extract_python_code, solve_task
from evolution.eval.task import Task
from evolution.eval.workspace import setup_workspace
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
    setup_workspace(task, workspace)

    return task, workspace


class TestSetupWorkspace:
    def test_existing_workspace_is_reset(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)
        (workspace / "stale-output.txt").write_text("old")

        setup_workspace(task, workspace)

        assert not (workspace / "stale-output.txt").exists()
        assert (workspace / "input.txt").read_text() == "some input\n"

    def test_task_source_cannot_be_workspace(self, tmp_path: Path):
        task, _ = _make_solvable_task(tmp_path)

        with pytest.raises(ValueError, match="must not overlap task source"):
            setup_workspace(task, task.path)

    def test_project_root_cannot_be_workspace(self, tmp_path: Path):
        task, _ = _make_solvable_task(tmp_path)
        project = tmp_path / "project"
        project.mkdir()

        with pytest.raises(ValueError, match="unsafe workspace cleanup target"):
            setup_workspace(task, project, project_dir=project)

    def test_default_workspaces_are_unique(self, tmp_path: Path, monkeypatch):
        task, _ = _make_solvable_task(tmp_path)
        monkeypatch.setattr("evolution.eval.workspace.tempfile.tempdir", str(tmp_path))

        first = setup_workspace(task)
        second = setup_workspace(task)

        assert first != second


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
                "counts": {"train": 1, "val": 0, "test": 0},
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
    def test_seeded_unknown_backend_fails_before_prompt_or_backend_work(
        self, tmp_path: Path
    ) -> None:
        task, workspace = _make_solvable_task(tmp_path)

        class UnknownBackend:
            calls = 0

            async def complete(self, messages, system=None, seed=None):
                self.calls += 1
                raise AssertionError("backend work started")

        backend = UnknownBackend()

        with pytest.raises(ValueError, match="seed"):
            solve_task(task, backend, workspace, evaluate=False, attempt_seed=37)  # type: ignore[arg-type]

        assert backend.calls == 0
        assert not (workspace / "_prompt.md").exists()
        assert not (workspace / "prompt_audit.json").exists()

    def test_exact_binding_drives_injection_and_trace_provenance(self, tmp_path: Path) -> None:
        task, workspace = _make_solvable_task(tmp_path)
        store = SkillStore(project_dir=tmp_path)
        owner, _ = store.create(name="test-skill", description="A test skill")
        binding = SkillBinding.current(owner)
        owner.skill_md_path.write_text(
            owner.skill_md_path.read_text(encoding="utf-8") + "\nDIRTY WORKING MARKER\n",
            encoding="utf-8",
        )

        class CapturingBackend(MockBackend):
            async def complete_with_skills(self, messages, skills, system=None, seed=None):
                self.injected_bodies = [item.body for item in skills]
                return await super().complete_with_skills(messages, skills, system, seed)

        backend = CapturingBackend(
            responses=[
                "```python\nfrom pathlib import Path\n"
                'Path("output.txt").write_text("hello world\\n")\n```'
            ]
        )

        result = solve_task(
            task,
            backend,
            workspace,
            bindings=[binding],
            model_name="solver-model",
            trace_required=True,
        )

        assert all("DIRTY WORKING MARKER" not in body for body in backend.injected_bodies)
        assert len(result.trace_ids) == 1
        [trace] = TraceStore(owner).list()
        assert trace["skill_version"] == "v1"
        assert trace["trace_context"]["skill_binding"] == binding.provenance()

    def test_output_collection_failure_persists_invalid_trace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task, workspace = _make_solvable_task(tmp_path)
        skill, _ = SkillStore(project_dir=tmp_path).create(
            name="test-skill", description="A test skill"
        )
        binding = SkillBinding.current(skill)
        monkeypatch.setattr(
            "evolution.eval.layouts.evo.EvoTaskLayout.collect_outputs",
            lambda *_args: CollectResult(error="forced-copy-error"),
        )

        result = solve_task(
            task,
            MockBackend(
                responses=[
                    "```python\nfrom pathlib import Path\n"
                    'Path("output.txt").write_text("hello world\\n")\n```'
                ]
            ),
            workspace,
            bindings=[binding],
            model_name="solver-model",
            trace_required=True,
        )

        assert result.eval_result is None
        assert len(result.trace_ids) == 1
        [trace] = TraceStore(skill).list()
        assert trace["trace_context"]["scored_measurement_valid"] is False
        assert trace["errors"] == ["output collection failed: forced-copy-error"]

    def test_child_process_only_receives_explicit_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task, workspace = _make_solvable_task(tmp_path)
        skill = _make_skill(tmp_path)
        monkeypatch.setenv("CUSTOM_ALLOWED", "visible")
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
        backend = MockBackend(
            responses=[
                "```python\n"
                "import json, os\n"
                "from pathlib import Path\n"
                'Path("output.txt").write_text("hello world\\n")\n'
                'Path("env.json").write_text(json.dumps({\n'
                '    "allowed": os.environ.get("CUSTOM_ALLOWED"),\n'
                '    "provider_secret": os.environ.get("OPENAI_API_KEY"),\n'
                "}))\n"
                "```"
            ]
        )

        solve_task(
            task,
            backend,
            workspace,
            skill=skill,
            evaluate=False,
            pass_env=("CUSTOM_ALLOWED",),
        )

        assert json.loads((workspace / "env.json").read_text()) == {
            "allowed": "visible",
            "provider_secret": None,
        }

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

        backend = MockBackend(
            responses=[
                '```python\nraise ValueError("oops")\n```',
                '```python\nraise RuntimeError("still broken")\n```',
            ]
        )

        result = solve_task(task, backend, workspace)

        assert result.script_exit_code != 0
        assert result.runtime_repair_attempted is True
        assert result.runtime_repair_succeeded is False
        assert any("Script exited with code" in error for error in result.errors)

    def test_nonzero_exit_gets_one_clean_runtime_repair(self, tmp_path: Path) -> None:
        task, workspace = _make_solvable_task(tmp_path)
        backend = MockBackend(
            responses=[
                "```python\n"
                "from pathlib import Path\n"
                'Path("stale.txt").write_text("first attempt")\n'
                'raise ValueError("broken parser")\n'
                "```",
                "```python\n"
                "from pathlib import Path\n"
                'assert not Path("stale.txt").exists()\n'
                'Path("output.txt").write_text("hello world\\n")\n'
                "```",
            ]
        )

        result = solve_task(task, backend, workspace, attempt_seed=37)

        assert len(backend.calls) == 2
        assert [call["seed"] for call in backend.calls] == [37, 37]
        assert "broken parser" in backend.calls[1]["messages"][0]["content"]
        assert result.runtime_repair_attempted is True
        assert result.runtime_repair_succeeded is True
        assert result.initial_script_exit_code not in (None, 0)
        assert result.script_exit_code == 0
        assert result.success is True
        assert not (workspace / "stale.txt").exists()

    def test_no_code_in_response(self, tmp_path: Path):
        task, workspace = _make_solvable_task(tmp_path)

        backend = MockBackend(responses=["I cannot solve this task.", "Still no Python code here."])

        result = solve_task(task, backend, workspace)

        assert result.success is False
        assert len(backend.calls) == 2
        assert result.runtime_repair_attempted is False
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

    def test_prompt_describes_nested_public_data_but_not_internal_files(
        self, tmp_path: Path
    ) -> None:
        from openpyxl import Workbook

        task, workspace = _make_solvable_task(tmp_path)
        data_dir = workspace / "environment" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "feed.csv").write_text("partner,rank\nacme,7\n", encoding="utf-8")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "History"
        sheet.append(["partner", "previous_rank"])
        sheet.append(["acme", 9])
        workbook.save(data_dir / "historical.xlsx")

        for relative in (
            "tests/oracle.csv",
            "output/old.csv",
            "environment/data/_private.csv",
            "environment/data/ground_truth.csv",
        ):
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SHOULD_NOT_BE_VISIBLE", encoding="utf-8")

        backend = MockBackend(responses=['```python\nprint("ok")\n```'])

        solve_task(task, backend, workspace, evaluate=False)

        prompt = backend.calls[0]["messages"][0]["content"]
        assert "environment/data/feed.csv" in prompt
        assert "partner,rank" in prompt
        assert "environment/data/historical.xlsx" in prompt
        assert 'sheet "History"' in prompt
        assert "previous_rank" in prompt
        assert "SHOULD_NOT_BE_VISIBLE" not in prompt

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
        assert uuid.UUID(traces[0]["id"]).version == 4
        evidence = json.loads(
            (TraceStore(skill).traces_dir / traces[0]["id"] / "evidence.json").read_text(
                encoding="utf-8"
            )
        )
        artifact_paths = {
            entry.get("path") for entry in evidence["produced_artifacts"] if isinstance(entry, dict)
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
        backend = MockBackend(responses=["```python\nimport time\ntime.sleep(5)\n```"])

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
        assert len(backend.calls) == 1
        assert result.runtime_repair_attempted is False
        traces = TraceStore(skill).list()
        assert len(traces) == 1
        evidence = json.loads(
            (TraceStore(skill).traces_dir / traces[0]["id"] / "evidence.json").read_text(
                encoding="utf-8"
            )
        )
        assert evidence["agent_solve"]["timeout"] is True
        assert evidence["agent_solve"]["diagnostic"] == "timeout"
