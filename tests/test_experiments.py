"""Tests for experiments — isolated skill evolution tracks."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evolution.core.experiments import (
    create_experiment,
    delete_experiment,
    get_experiment_store,
    list_experiments,
)


@pytest.fixture
def project_with_skills(tmp_path: Path) -> Path:
    """Project with a skill that has v1 committed."""
    skills_dir = tmp_path / ".agents" / "skills"
    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill\n---\n\n# My Skill v1\nOriginal content.\n"
    )

    # Create .evolution with v1
    evo_dir = skill_dir / ".evolution"
    v1_dir = evo_dir / "versions" / "v1"
    v1_dir.mkdir(parents=True)
    shutil.copy2(skill_dir / "SKILL.md", v1_dir / "SKILL.md")

    lineage = {
        "skill_id": "my-skill",
        "current_version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lineage": [
            {
                "version": "v1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "origin": "manual",
                "parent": None,
                "fitness": None,
                "status": "active",
            }
        ],
    }
    (evo_dir / "lineage.json").write_text(json.dumps(lineage, indent=2))

    return tmp_path


class TestCreateExperiment:
    def test_creates_experiment(self, project_with_skills: Path):
        exp_dir = create_experiment(
            project_with_skills,
            "ablation-1",
            [{"name": "my-skill", "version": "v1"}],
            description="Test ablation",
        )
        assert exp_dir.exists()
        assert (exp_dir / "skills" / "my-skill" / "SKILL.md").exists()
        assert (exp_dir / "experiment.json").exists()

    def test_experiment_has_own_lineage(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "ablation-1",
            [{"name": "my-skill", "version": "v1"}],
        )
        lineage_path = (
            project_with_skills
            / ".agents"
            / "experiments"
            / "ablation-1"
            / "skills"
            / "my-skill"
            / ".evolution"
            / "lineage.json"
        )
        assert lineage_path.exists()
        data = json.loads(lineage_path.read_text())
        assert data["current_version"] == "v1"
        assert data["lineage"][0]["origin"] == "fork"

    def test_duplicate_raises(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "dup",
            [{"name": "my-skill", "version": "v1"}],
        )
        with pytest.raises(FileExistsError):
            create_experiment(
                project_with_skills,
                "dup",
                [{"name": "my-skill", "version": "v1"}],
            )

    def test_missing_skill_raises(self, project_with_skills: Path):
        with pytest.raises(KeyError, match="nonexistent"):
            create_experiment(
                project_with_skills,
                "bad",
                [{"name": "nonexistent", "version": "v1"}],
            )

    def test_missing_version_raises(self, project_with_skills: Path):
        with pytest.raises(KeyError, match="v99"):
            create_experiment(
                project_with_skills,
                "bad",
                [{"name": "my-skill", "version": "v99"}],
            )

    def test_metadata_saved(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "ablation-1",
            [{"name": "my-skill", "version": "v1"}],
            description="testing",
        )
        meta = json.loads(
            (
                project_with_skills / ".agents" / "experiments" / "ablation-1" / "experiment.json"
            ).read_text()
        )
        assert meta["name"] == "ablation-1"
        assert meta["description"] == "testing"
        assert meta["forked_from"] == {"my-skill": "v1"}


class TestListExperiments:
    def test_empty(self, tmp_path: Path):
        assert list_experiments(tmp_path) == []

    def test_lists_experiments(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "exp-a",
            [{"name": "my-skill", "version": "v1"}],
        )
        create_experiment(
            project_with_skills,
            "exp-b",
            [{"name": "my-skill", "version": "v1"}],
        )
        exps = list_experiments(project_with_skills)
        names = [e["name"] for e in exps]
        assert "exp-a" in names
        assert "exp-b" in names


class TestGetExperimentStore:
    def test_returns_store_with_experiment_skills(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "exp-1",
            [{"name": "my-skill", "version": "v1"}],
        )
        store = get_experiment_store(project_with_skills, "exp-1")
        assert store.has("my-skill")
        skill = store.get("my-skill")
        assert skill.name == "my-skill"

    def test_experiment_skill_takes_priority(self, project_with_skills: Path):
        # Modify main skill
        main_skill_md = project_with_skills / ".agents" / "skills" / "my-skill" / "SKILL.md"
        main_skill_md.write_text(
            "---\nname: my-skill\ndescription: MODIFIED main\n---\n\n# Modified\n"
        )

        create_experiment(
            project_with_skills,
            "exp-1",
            [{"name": "my-skill", "version": "v1"}],
        )
        store = get_experiment_store(project_with_skills, "exp-1")
        skill = store.get("my-skill")
        # Should get experiment's version (forked from v1), not main's modified version
        assert "Original content" in skill.body

    def test_nonexistent_experiment_raises(self, project_with_skills: Path):
        with pytest.raises(KeyError, match="nope"):
            get_experiment_store(project_with_skills, "nope")


class TestDeleteExperiment:
    def test_deletes(self, project_with_skills: Path):
        create_experiment(
            project_with_skills,
            "to-delete",
            [{"name": "my-skill", "version": "v1"}],
        )
        exp_dir = project_with_skills / ".agents" / "experiments" / "to-delete"
        assert exp_dir.exists()
        delete_experiment(project_with_skills, "to-delete")
        assert not exp_dir.exists()

    def test_missing_raises(self, project_with_skills: Path):
        with pytest.raises(KeyError):
            delete_experiment(project_with_skills, "nonexistent")
