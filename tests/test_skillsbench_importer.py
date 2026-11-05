"""Tests for SkillsBench importer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.core.store import SkillStore
from evolution.importers.skillsbench import (
    discover_skills,
    import_skills,
)


def _make_skillsbench_repo(tmp_path: Path) -> Path:
    """Create a minimal SkillsBench repo structure for testing."""
    repo = tmp_path / "skillsbench"
    tasks = repo / "tasks"

    # Task 1: easy, with one skill
    t1 = tasks / "easy-task"
    t1_skills = t1 / "environment" / "skills" / "simple-skill"
    t1_skills.mkdir(parents=True)
    (t1_skills / "SKILL.md").write_text(
        "---\nname: simple-skill\n"
        "description: A simple test skill.\n---\n\n"
        "# Simple Skill\n\nDo simple things.\n"
    )
    (t1 / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'difficulty = "easy"\n'
        'category = "testing"\n'
        'tags = ["python", "testing"]\n'
    )

    # Task 2: medium, with two skills (one shared name)
    t2 = tasks / "medium-task"
    t2_skills_a = t2 / "environment" / "skills" / "xlsx"
    t2_skills_a.mkdir(parents=True)
    (t2_skills_a / "SKILL.md").write_text(
        "---\nname: xlsx\ndescription: Excel file handling.\n---\n\n# XLSX\n\nUse openpyxl.\n"
    )
    t2_skills_b = t2 / "environment" / "skills" / "pdf"
    t2_skills_b.mkdir(parents=True)
    (t2_skills_b / "SKILL.md").write_text(
        "---\nname: pdf\n"
        "description: PDF processing with pdfplumber.\n---\n\n"
        "# PDF\n\nUse pdfplumber.\n"
    )
    (t2 / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'difficulty = "medium"\n'
        'category = "data-analysis"\n'
        'tags = ["excel", "pdf"]\n'
    )

    # Task 3: has the same "xlsx" skill (for dedup testing)
    t3 = tasks / "another-task"
    t3_skills = t3 / "environment" / "skills" / "xlsx"
    t3_skills.mkdir(parents=True)
    (t3_skills / "SKILL.md").write_text(
        "---\nname: xlsx\ndescription: Excel file handling (copy).\n---\n\n# XLSX\n\nDuplicate.\n"
    )
    (t3 / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'difficulty = "hard"\n'
        'category = "finance"\n'
        'tags = ["excel"]\n'
    )

    # Task 4: skill with scripts/
    t4 = tasks / "scripted-task"
    t4_skills = t4 / "environment" / "skills" / "with-scripts"
    t4_skills.mkdir(parents=True)
    (t4_skills / "SKILL.md").write_text(
        "---\nname: with-scripts\n"
        "description: A skill with helper scripts.\n---\n\n"
        "# With Scripts\n\nRun the helper.\n"
    )
    t4_scripts = t4_skills / "scripts"
    t4_scripts.mkdir()
    (t4_scripts / "run.sh").write_text("#!/bin/bash\necho hello\n")
    (t4 / "task.toml").write_text(
        'version = "1.0"\n\n'
        "[metadata]\n"
        'difficulty = "easy"\n'
        'category = "devops"\n'
        'tags = ["bash", "scripts"]\n'
    )

    return repo


class TestDiscoverSkills:
    def test_discovers_all(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        entries = discover_skills(repo)
        names = [e.name for e in entries]
        assert "simple-skill" in names
        assert "xlsx" in names
        assert "pdf" in names
        assert "with-scripts" in names

    def test_deduplicates_by_default(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        entries = discover_skills(repo)
        names = [e.name for e in entries]
        # "xlsx" appears in two tasks but should be deduplicated
        assert names.count("xlsx") == 1

    def test_no_dedup(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        entries = discover_skills(repo, deduplicate=False)
        names = [e.name for e in entries]
        assert names.count("xlsx") == 2

    def test_parses_task_metadata(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        entries = discover_skills(repo)
        simple = next(e for e in entries if e.name == "simple-skill")
        assert simple.difficulty == "easy"
        assert simple.category == "testing"
        assert "python" in simple.tags

    def test_missing_repo_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            discover_skills(tmp_path / "nonexistent")

    def test_discovers_count(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        entries = discover_skills(repo)
        # 4 unique skills (xlsx deduped)
        assert len(entries) == 4


class TestImportSkills:
    def test_import_all(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        imported = import_skills(store, repo)

        assert len(imported) == 4
        assert store.has("simple-skill")
        assert store.has("xlsx")
        assert store.has("pdf")
        assert store.has("with-scripts")

    def test_import_by_name(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        imported = import_skills(store, repo, names=["xlsx", "pdf"])

        assert len(imported) == 2
        assert store.has("xlsx")
        assert store.has("pdf")
        assert not store.has("simple-skill")

    def test_import_adds_metadata(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        import_skills(store, repo, names=["simple-skill"])

        skill = store.get("simple-skill")
        assert skill.metadata.get("source") == "skillsbench"
        assert skill.metadata.get("source-task") == "easy-task"
        assert skill.metadata.get("category") == "testing"
        assert skill.metadata.get("difficulty") == "easy"

    def test_import_with_theme(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        import_skills(
            store,
            repo,
            names=["xlsx"],
            theme_prefix="productivity/documents",
        )

        skill = store.get("xlsx")
        assert skill.metadata.get("theme") == "productivity/documents"

    def test_import_skips_existing(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")

        # Import once
        import_skills(store, repo, names=["xlsx"])
        # Import again — should skip
        imported = import_skills(store, repo, names=["xlsx"])
        assert imported == []

    def test_import_copies_scripts(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        import_skills(store, repo, names=["with-scripts"])

        skill = store.get("with-scripts")
        assert (skill.path / "scripts" / "run.sh").exists()

    def test_lineage_origin_is_import(self, tmp_path: Path):
        repo = _make_skillsbench_repo(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        import_skills(store, repo, names=["pdf"])

        skill = store.get("pdf")
        lineage = json.loads((skill.path / ".evolution" / "lineage.json").read_text())
        assert lineage["lineage"][0]["origin"] == "import"
