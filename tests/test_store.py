"""Tests for skill store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.core.store import SkillStore


class TestStoreDiscovery:
    def test_discovers_fixtures(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        entries = store.list()
        names = {e.name for e in entries}
        assert "valid-skill" in names
        assert "minimal-skill" in names

    def test_get_loads_full_skill(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        skill = store.get("valid-skill")
        assert skill.name == "valid-skill"
        assert "# Valid Skill" in skill.body
        assert skill.fitness == pytest.approx(0.83)

    def test_get_missing_raises(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        with pytest.raises(KeyError):
            store.get("nonexistent")

    def test_has(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        assert store.has("valid-skill")
        assert not store.has("nonexistent")

    def test_list_filters_by_status(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        active = store.list(status="active")
        assert all(e.status == "active" for e in active)

    def test_empty_store(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        assert store.list() == []


class TestStoreSearch:
    def test_keyword_match(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        results = store.search("parser")
        names = [r.name for r in results]
        assert "valid-skill" in names

    def test_no_match(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        results = store.search("xyzzyplugh")
        assert results == []


class TestStoreCreate:
    def test_create_scaffolds_correctly(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        skill = store.create(
            name="new-skill",
            description="A brand new skill for testing.",
        )

        assert skill.name == "new-skill"
        skill_dir = tmp_path / ".agents" / "skills" / "new-skill"
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "evals" / "evals.json").exists()
        assert (skill_dir / ".evolution" / "lineage.json").exists()
        assert (skill_dir / ".evolution" / "versions" / "v1" / "SKILL.md").exists()

        # Lineage has v1
        lineage = json.loads((skill_dir / ".evolution" / "lineage.json").read_text())
        assert lineage["current_version"] == "v1"
        assert lineage["lineage"][0]["version"] == "v1"
        assert lineage["lineage"][0]["status"] == "active"

    def test_create_duplicate_raises(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="dup", description="First.")
        with pytest.raises(FileExistsError):
            store.create(name="dup", description="Second.")

    def test_create_invalid_name_raises(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        with pytest.raises(Exception):
            store.create(name="Bad-Name", description="Uppercase.")

    def test_create_is_discoverable(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="findme", description="Should be in the index.")
        assert store.has("findme")
        entries = store.list()
        names = [e.name for e in entries]
        assert "findme" in names


class TestStoreCatalog:
    def test_to_catalog_xml(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        xml = store.to_catalog_xml()
        assert "<available_skills>" in xml
        assert "<name>valid-skill</name>" in xml

    def test_population_status(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        pop = store.population_status()
        assert pop.total >= 2
        assert pop.active >= 1
