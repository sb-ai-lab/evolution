"""Offline command-level regression for the complete optimizer pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.core.store import SkillStore
from evolution.evolve import optimizer
from evolution.lineage.traces import TraceStore
from evolution.lineage.tracker import LineageTracker
from evolution.llm.backend import LLMResponse

pytestmark = pytest.mark.slow


def _task(root: Path, name: str, skill_name: str) -> None:
    task = root / name
    (task / "inputs").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text(
        'version = "1.0"\n'
        "[metadata]\n"
        f'name = "{name}"\n'
        'difficulty = "easy"\n'
        'category = "test"\n'
        "[skills]\n"
        f'required = ["{skill_name}"]\n'
        "[evaluation]\n"
        'test_file = "tests/test_outputs.py"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Write good to output.txt.", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_output():\n"
        "    output = Path(os.environ['TASK_WORKSPACE']) / 'output.txt'\n"
        "    assert output.read_text(encoding='utf-8') == 'good'\n",
        encoding="utf-8",
    )


class _ScriptedOptimizerBackend:
    model = "scripted-offline"
    seed_mode = "forwarded"

    async def complete_with_skills(self, messages, skills, system=None, seed=None):
        del messages, system, seed
        value = "good" if any("Verification marker" in skill.body for skill in skills) else "bad"
        return LLMResponse(
            content=(
                "```python\n"
                "from pathlib import Path\n"
                f"Path('output.txt').write_text({value!r}, encoding='utf-8')\n"
                "```"
            )
        )

    async def complete_with_skill(self, messages, skill, system=None, seed=None):
        return await self.complete_with_skills(messages, [skill], system=system, seed=seed)

    async def complete(self, messages, system=None, seed=None):
        del system, seed
        prompt = messages[-1]["content"]
        if "# Diagnose Failing Traces" in prompt:
            return LLMResponse(
                content=json.dumps(
                    {
                        "root_cause": "The solver stopped without checking its output.",
                        "skill_gap": "The skill lacks a final verification instruction.",
                        "suggested_fix": "Require output verification before completion.",
                        "severity": "major",
                        "pattern": "missing_step",
                        "evidence_sufficiency": "sufficient",
                        "rewrite_recommended": True,
                    }
                )
            )
        if "# Propose Skill Edits" in prompt:
            return LLMResponse(
                content=json.dumps(
                    {
                        "edits": [
                            {
                                "op": "append",
                                "target": "",
                                "content": (
                                    "Before finishing, verify the produced output and correct it "
                                    "when checks fail. Verification marker."
                                ),
                                "rationale": "Adds the missing verification step.",
                                "evidence_refs": ["train-task"],
                            }
                        ]
                    }
                )
            )
        raise AssertionError(f"Unexpected editor prompt: {prompt[:80]}")


def test_cli_runs_full_optimizer_and_promotes(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_name = "cli-e2e-skill"
    SkillStore(project_dir=project).create(skill_name, "Offline end-to-end optimizer test")
    _task(project / "tasks", "train-task", skill_name)
    _task(project / "tasks", "validation-task", skill_name)
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        optimizer,
        "get_backend",
        lambda _model, **_kwargs: _ScriptedOptimizerBackend(),
    )

    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            skill_name,
            "--solver-model",
            "scripted",
            "--editor-model",
            "scripted",
            "--train",
            "train-task",
            "--validate",
            "validation-task",
            "--trials",
            "4",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    data = payload["data"]
    assert data["status"] == "completed"
    assert data["final_version"] == "v2"
    assert data["rounds"][0]["outcome"] == "promoted"

    skill = SkillStore(project_dir=project).get(skill_name)
    assert "Verification marker" in skill.body
    assert LineageTracker(skill).current_version == "v2"
    traces = TraceStore(skill).list(task="train-task")
    assert len(traces) == 4
    assert {trace["trace_context"]["phase"] for trace in traces} == {"collect"}
    assert not TraceStore(skill).list(task="validation-task")
