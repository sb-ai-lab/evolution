"""Tests for skill store."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from evolution.core.store import SkillStore
from evolution.lineage.state_io import LockTimeoutError


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

    def test_rejects_linked_skill_nodes_and_unsafe_resources(self, tmp_path: Path):
        skills_root = tmp_path / ".agents" / "skills"
        external = tmp_path / "external"
        external.mkdir()
        (external / "SKILL.md").write_text(
            "---\nname: linked\ndescription: external\n---\n\n# Linked\n",
            encoding="utf-8",
        )
        skills_root.mkdir(parents=True)
        (skills_root / "linked").symlink_to(external, target_is_directory=True)

        unsafe = skills_root / "unsafe"
        unsafe.mkdir()
        (unsafe / "SKILL.md").write_text(
            "---\nname: unsafe\ndescription: unsafe\n---\n\n# Unsafe\n",
            encoding="utf-8",
        )
        references = unsafe / "references"
        references.mkdir()
        (references / "outside").symlink_to(external / "SKILL.md")

        store = SkillStore(project_dir=tmp_path)

        assert not store.has("linked")
        assert not store.has("unsafe")

    def test_rejects_symlinked_skill_root(self, tmp_path: Path):
        external = tmp_path / "external"
        external.mkdir()
        (tmp_path / ".agents").symlink_to(external, target_is_directory=True)
        store = SkillStore(project_dir=tmp_path)

        assert store.list() == []
        with pytest.raises(ValueError, match="root must not contain symlinks"):
            store.create(name="escaped", description="Must stay local")
        assert not (external / "skills" / "escaped").exists()


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
        skill, _ = store.create(
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

    def test_create_from_file_preserves_v0_and_marks_import(self, tmp_path: Path):
        source = tmp_path / "seed.md"
        source.write_text(
            "---\nname: seeded\ndescription: Original description.\n---\n\n# Exact v0\n",
            encoding="utf-8",
        )
        store = SkillStore(project_dir=tmp_path)

        store.create(
            name="seeded",
            description="Launcher description must not rewrite v0.",
            from_file=source,
        )

        skill_dir = tmp_path / ".agents" / "skills" / "seeded"
        assert (skill_dir / "SKILL.md").read_bytes() == source.read_bytes()
        lineage = json.loads((skill_dir / ".evolution" / "lineage.json").read_text())
        assert lineage["lineage"][0]["origin"] == "import"

    def test_create_replace_validates_seed_before_moving_existing_skill(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="seeded", description="Existing skill")
        external = tmp_path / "external.md"
        external.write_text("secret", encoding="utf-8")
        linked_seed = tmp_path / "seed.md"
        linked_seed.symlink_to(external)

        with pytest.raises(ValueError, match="symlink"):
            store.create(
                name="seeded",
                description="Replacement",
                from_file=linked_seed,
                replace=True,
            )

        assert store.get("seeded").description == "Existing skill"
        assert not list((tmp_path / ".agents" / "skills").glob("seeded-deprecated-on-*"))

    def test_create_replace_rejects_malformed_regular_seed_before_moving_existing_skill(
        self, tmp_path: Path
    ):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="seeded", description="Existing skill")
        malformed = tmp_path / "bad.md"
        malformed.write_text("not a valid skill", encoding="utf-8")

        with pytest.raises(ValueError):
            store.create(
                name="seeded",
                description="Replacement",
                from_file=malformed,
                replace=True,
            )

        assert store.get("seeded").description == "Existing skill"
        assert not list((tmp_path / ".agents" / "skills").glob("seeded-deprecated-on-*"))

    def test_create_replace_respects_parent_scoped_mutation_lock(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="locked", description="Existing skill")
        skills_root = tmp_path / ".agents" / "skills"
        lock_path = skills_root / ".locked.mutation.lock"
        holder = os.open(lock_path, os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(LockTimeoutError):
                store.create(name="locked", description="Replacement", replace=True)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        assert store.get("locked").description == "Existing skill"


class TestStoreCatalog:
    def test_population_status(self, tmp_skills: Path):
        store = SkillStore(project_dir=tmp_skills)
        pop = store.population_status()
        assert pop.total >= 2
        assert pop.active >= 1
