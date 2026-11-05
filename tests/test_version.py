"""Tests for version storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.store import SkillStore
from evolution.lineage.version import VersionStore


@pytest.fixture
def skill_with_versions(tmp_path: Path):
    store = SkillStore(project_dir=tmp_path)
    store.create(name="versioned", description="A skill to test versioning.")
    return store, "versioned"


class TestVersionStore:
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

    def test_load_missing_raises(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)
        with pytest.raises(KeyError):
            vs.load("v99")

    def test_delete_version(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)

        vs.save("temp")
        assert vs.exists("temp")
        vs.delete("temp")
        assert not vs.exists("temp")

    def test_list_candidates(self, skill_with_versions):
        store, name = skill_with_versions
        skill = store.get(name)
        vs = VersionStore(skill)

        vs.save("v1-candidate-1")
        vs.save("v1-candidate-2")
        candidates = vs.list_candidates()
        assert "v1-candidate-1" in candidates
        assert "v1-candidate-2" in candidates
        assert "v1" not in candidates
