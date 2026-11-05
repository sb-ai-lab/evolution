"""Tests for version storage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evolution.core.store import SkillStore
from evolution.lineage.version import VersionStore, hash_tree


@pytest.fixture
def skill_with_versions(tmp_path: Path):
    store = SkillStore(project_dir=tmp_path)
    store.create(name="versioned", description="A skill to test versioning.")
    return store, "versioned"


class TestVersionStore:
    def test_hash_tree_is_deterministic_and_covers_paths_and_contents(self, tmp_path: Path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "scripts").mkdir(parents=True)
        (right / "scripts").mkdir(parents=True)
        (left / "SKILL.md").write_text("skill", encoding="utf-8")
        (left / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
        (right / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
        (right / "SKILL.md").write_text("skill", encoding="utf-8")

        assert hash_tree(left) == hash_tree(right)

        (right / "scripts" / "run.py").write_text("print(2)", encoding="utf-8")
        assert hash_tree(left) != hash_tree(right)

    def test_save_and_load(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)

        # v1 was already saved by create()
        assert vs.exists("v1")

        loaded = vs.load("v1")
        assert loaded.name == "versioned"

    def test_list_versions(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)
        assert "v1" in vs.list_versions()

    def test_save_new_version(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)

        vs.save("v2")
        assert vs.exists("v2")
        assert "v2" in vs.list_versions()

    def test_restore_replaces_the_complete_managed_skill_tree(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)
        skill.skill_md_path.write_text("version two", encoding="utf-8")
        for relative, content in (
            ("scripts/run.py", "print('v2')"),
            ("references/guide.md", "guide v2"),
            ("assets/icon.txt", "icon v2"),
        ):
            path = skill.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        vs.save("v2")

        skill.skill_md_path.write_text("working copy", encoding="utf-8")
        (skill.path / "scripts" / "run.py").write_text("changed", encoding="utf-8")
        (skill.path / "scripts" / "stale.py").write_text("stale", encoding="utf-8")
        shutil.rmtree(skill.path / "references")
        (skill.path / "assets" / "icon.txt").write_text("changed", encoding="utf-8")

        vs.restore("v2")

        assert skill.skill_md_path.read_text(encoding="utf-8") == "version two"
        assert (skill.path / "scripts" / "run.py").read_text() == "print('v2')"
        assert not (skill.path / "scripts" / "stale.py").exists()
        assert (skill.path / "references" / "guide.md").read_text() == "guide v2"
        assert (skill.path / "assets" / "icon.txt").read_text() == "icon v2"
        assert (skill.path / ".evolution").is_dir()

    def test_save_does_not_overwrite_existing_version(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        original = VersionStore(skill).load("v1").body
        skill.skill_md_path.write_text(
            skill.skill_md_path.read_text().replace(skill.body, "changed")
        )

        with pytest.raises(FileExistsError):
            VersionStore(skill).save("v1")

        assert VersionStore(skill).load("v1").body == original

    def test_load_missing_raises(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)
        with pytest.raises(KeyError):
            vs.load("v99")

    def test_rejects_unsafe_version_label(self, skill_with_versions):
        store, name = skill_with_versions

        with pytest.raises(ValueError, match="Invalid version label"):
            VersionStore(store.get(name)).save("../outside")

    def test_delete_version(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)

        vs.save("temp")
        assert vs.exists("temp")
        vs.delete("temp")
        assert not vs.exists("temp")
