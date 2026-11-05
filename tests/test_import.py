"""Tests for skill import functionality."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.core.store import SkillStore


def _make_source_skill(tmp_path: Path, name: str = "test-import") -> Path:
    """Create a minimal source skill directory for import tests."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\n"
        f"description: A skill for import testing.\n---\n\n"
        f"# {name}\n\nInstructions here.\n"
    )
    return src


def _make_source_with_scripts(tmp_path: Path) -> Path:
    """Create a source skill with scripts/ and references/."""
    src = tmp_path / "source" / "with-resources"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: with-resources\n"
        "description: Skill with bundled resources.\n---\n\n"
        "# With Resources\n\nUse the scripts.\n"
    )
    scripts = src / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("print('hello')\n")
    refs = src / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\n\nReference material.\n")
    return src


class TestImportSkill:
    def test_basic_import(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src)

        assert skill.name == "test-import"
        assert store.has("test-import")
        assert "Instructions here" in skill.body

    def test_import_creates_evolution(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src)

        evo_dir = skill.path / ".evolution"
        assert evo_dir.exists()
        assert (evo_dir / "lineage.json").exists()
        assert (evo_dir / "versions" / "v1" / "SKILL.md").exists()

        lineage = json.loads((evo_dir / "lineage.json").read_text())
        assert lineage["current_version"] == "v1"
        assert lineage["lineage"][0]["origin"] == "import"
        assert lineage["lineage"][0]["status"] == "active"

    def test_import_copies_resources(self, tmp_path: Path):
        src = _make_source_with_scripts(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src)

        assert (skill.path / "scripts" / "helper.py").exists()
        assert (skill.path / "references" / "guide.md").exists()
        # Also in v1 snapshot
        v1 = skill.path / ".evolution" / "versions" / "v1"
        assert (v1 / "scripts" / "helper.py").exists()
        assert (v1 / "references" / "guide.md").exists()

    def test_import_with_name_override(self, tmp_path: Path):
        src = _make_source_skill(tmp_path, name="original")
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src, name="renamed")

        assert skill.name == "renamed"
        assert store.has("renamed")
        assert not store.has("original")

    def test_import_with_metadata(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(
            src,
            metadata={"source": "skillsbench", "theme": "testing"},
        )

        assert skill.metadata.get("source") == "skillsbench"
        assert skill.metadata.get("theme") == "testing"

    def test_import_duplicate_raises(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        store.import_skill(src)

        with pytest.raises(FileExistsError):
            store.import_skill(src)

    def test_import_missing_source_raises(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path / "project")
        with pytest.raises(FileNotFoundError):
            store.import_skill(tmp_path / "nonexistent")

    def test_import_creates_evals_scaffold(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src)

        evals_json = skill.path / "evals" / "evals.json"
        assert evals_json.exists()
        data = json.loads(evals_json.read_text())
        assert data["skill_name"] == "test-import"

    def test_import_is_discoverable(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        store.import_skill(src)

        entries = store.list()
        names = [e.name for e in entries]
        assert "test-import" in names


class TestImportFromFile:
    def test_import_from_skill_md_path(self, tmp_path: Path):
        src = _make_source_skill(tmp_path)
        store = SkillStore(project_dir=tmp_path / "project")
        skill = store.import_skill(src / "SKILL.md")

        assert skill.name == "test-import"
        assert store.has("test-import")
