"""Tests for skill containers."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.containers import ContainerStore
from evolution.core.models import ContainerSkillRef, SkillContainer
from evolution.core.store import SkillStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def container_dir(tmp_path: Path) -> Path:
    """Project dir with .agents/containers/ ready."""
    (tmp_path / ".agents" / "containers").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cstore(container_dir: Path) -> ContainerStore:
    return ContainerStore(project_dir=container_dir)


@pytest.fixture
def skill_project(tmp_path: Path) -> Path:
    """Project dir with two skills and containers dir."""
    skills_dir = tmp_path / ".agents" / "skills"
    containers_dir = tmp_path / ".agents" / "containers"
    skills_dir.mkdir(parents=True)
    containers_dir.mkdir(parents=True)

    # Create two minimal skills
    for name in ("alpha-skill", "beta-skill", "gamma-skill"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill {name}\n---\n\n# {name}\n"
        )

    return tmp_path


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestSkillContainerModel:
    def test_valid(self):
        c = SkillContainer(
            name="my-container",
            description="A test container",
            skills=["skill-a", "skill-b"],
        )
        assert c.name == "my-container"
        assert len(c.skills) == 2

    def test_empty_skills_rejected(self):
        with pytest.raises(ValueError, match="at least one skill"):
            SkillContainer(name="bad", description="x", skills=[])

    def test_bad_name_rejected(self):
        with pytest.raises(ValueError):
            SkillContainer(name="Bad Name!", description="x", skills=["a"])

    def test_metadata_optional(self):
        c = SkillContainer(name="minimal", description="x", skills=["a"])
        assert c.metadata is None


# ---------------------------------------------------------------------------
# ContainerStore — CRUD
# ---------------------------------------------------------------------------


class TestContainerStoreCreate:
    def test_create(self, cstore: ContainerStore, container_dir: Path):
        c = cstore.create(
            name="test-container",
            description="A test",
            skills=["skill-a", "skill-b"],
        )
        assert c.name == "test-container"
        assert cstore.has("test-container")
        # File exists
        assert (container_dir / ".agents" / "containers" / "test-container.yaml").exists()

    def test_create_duplicate_raises(self, cstore: ContainerStore):
        cstore.create(name="dup", description="x", skills=["a"])
        with pytest.raises(FileExistsError):
            cstore.create(name="dup", description="y", skills=["b"])

    def test_create_with_metadata(self, cstore: ContainerStore):
        c = cstore.create(
            name="tagged",
            description="x",
            skills=["a"],
            metadata={"source": "sb-bench"},
        )
        assert c.metadata["source"] == "sb-bench"


class TestContainerStoreRead:
    def test_list_empty(self, cstore: ContainerStore):
        assert cstore.list() == []

    def test_list_sorted(self, cstore: ContainerStore):
        cstore.create(name="beta", description="b", skills=["x"])
        cstore.create(name="alpha", description="a", skills=["x"])
        names = [c.name for c in cstore.list()]
        assert names == ["alpha", "beta"]

    def test_get(self, cstore: ContainerStore):
        cstore.create(name="my-cont", description="desc", skills=["a", "b"])
        c = cstore.get("my-cont")
        assert c.skill_names == ["a", "b"]

    def test_get_missing_raises(self, cstore: ContainerStore):
        with pytest.raises(KeyError):
            cstore.get("nonexistent")

    def test_has(self, cstore: ContainerStore):
        assert not cstore.has("nope")
        cstore.create(name="exists", description="x", skills=["a"])
        assert cstore.has("exists")


class TestContainerStoreUpdate:
    def test_update_description(self, cstore: ContainerStore):
        cstore.create(name="upd", description="old", skills=["a"])
        c = cstore.update("upd", description="new")
        assert c.description == "new"
        # Persisted
        cstore2 = ContainerStore(project_dir=cstore._scan_roots[0].parent.parent)
        assert cstore2.get("upd").description == "new"

    def test_update_skills(self, cstore: ContainerStore):
        cstore.create(name="upd2", description="x", skills=["a"])
        c = cstore.update("upd2", skills=["a", "b", "c"])
        assert c.skill_names == ["a", "b", "c"]

    def test_update_metadata_merges(self, cstore: ContainerStore):
        cstore.create(name="meta", description="x", skills=["a"], metadata={"k1": "v1"})
        c = cstore.update("meta", metadata={"k2": "v2"})
        assert c.metadata == {"k1": "v1", "k2": "v2"}


class TestContainerStoreDelete:
    def test_delete(self, cstore: ContainerStore, container_dir: Path):
        cstore.create(name="todel", description="x", skills=["a"])
        assert cstore.has("todel")
        cstore.delete("todel")
        assert not cstore.has("todel")
        assert not (container_dir / ".agents" / "containers" / "todel.yaml").exists()

    def test_delete_missing_raises(self, cstore: ContainerStore):
        with pytest.raises(KeyError):
            cstore.delete("nonexistent")


# ---------------------------------------------------------------------------
# ContainerStore — Discovery
# ---------------------------------------------------------------------------


class TestContainerStoreDiscovery:
    def test_discovers_yaml_files(self, container_dir: Path):
        # Write a YAML file manually
        yaml_path = container_dir / ".agents" / "containers" / "manual.yaml"
        yaml_path.write_text("name: manual\ndescription: hand-written\nskills:\n  - foo\n  - bar\n")
        cstore = ContainerStore(project_dir=container_dir)
        assert cstore.has("manual")
        c = cstore.get("manual")
        assert c.skill_names == ["foo", "bar"]

    def test_skips_invalid_yaml(self, container_dir: Path):
        bad = container_dir / ".agents" / "containers" / "bad.yaml"
        bad.write_text("this is not valid yaml: [[[")
        cstore = ContainerStore(project_dir=container_dir)
        assert not cstore.has("bad")


# ---------------------------------------------------------------------------
# ContainerStore — Resolution
# ---------------------------------------------------------------------------


class TestContainerResolve:
    def test_resolve_loads_skills(self, skill_project: Path):
        cstore = ContainerStore(project_dir=skill_project)
        sstore = SkillStore(project_dir=skill_project)

        cstore.create(
            name="my-pack",
            description="test",
            skills=["alpha-skill", "beta-skill"],
        )

        skills = cstore.resolve("my-pack", sstore)
        assert len(skills) == 2
        assert skills[0].name == "alpha-skill"
        assert skills[1].name == "beta-skill"

    def test_resolve_missing_skill_raises(self, skill_project: Path):
        cstore = ContainerStore(project_dir=skill_project)
        sstore = SkillStore(project_dir=skill_project)

        cstore.create(
            name="bad-pack",
            description="test",
            skills=["alpha-skill", "nonexistent"],
        )

        with pytest.raises(KeyError, match="nonexistent"):
            cstore.resolve("bad-pack", sstore)

    def test_resolve_preserves_order(self, skill_project: Path):
        cstore = ContainerStore(project_dir=skill_project)
        sstore = SkillStore(project_dir=skill_project)

        cstore.create(
            name="ordered",
            description="test",
            skills=["gamma-skill", "alpha-skill", "beta-skill"],
        )

        skills = cstore.resolve("ordered", sstore)
        assert [s.name for s in skills] == ["gamma-skill", "alpha-skill", "beta-skill"]


# ---------------------------------------------------------------------------
# Multi-skill LLM injection
# ---------------------------------------------------------------------------


class TestMultiSkillBackend:
    @pytest.mark.asyncio
    async def test_mock_backend_with_skills(self, skill_project: Path):
        from evolution.llm.mock_backend import MockBackend

        sstore = SkillStore(project_dir=skill_project)
        skills = [sstore.get("alpha-skill"), sstore.get("beta-skill")]

        backend = MockBackend(responses=["test response"])
        resp = await backend.complete_with_skills(
            [{"role": "user", "content": "hello"}],
            skills,
            system="test system",
        )
        assert resp.content == "test response"
        assert backend.calls[0]["skills"] == ["alpha-skill", "beta-skill"]

    @pytest.mark.asyncio
    async def test_single_skill_delegates(self, skill_project: Path):
        from evolution.llm.mock_backend import MockBackend

        sstore = SkillStore(project_dir=skill_project)
        skill = sstore.get("alpha-skill")

        backend = MockBackend(responses=["resp"])
        resp = await backend.complete_with_skill(
            [{"role": "user", "content": "hi"}],
            skill,
        )
        assert resp.content == "resp"
        # Should have delegated to complete_with_skills
        assert backend.calls[0]["skills"] == ["alpha-skill"]


# ---------------------------------------------------------------------------
# ContainerSkillRef model
# ---------------------------------------------------------------------------


class TestContainerSkillRef:
    def test_bare_name(self):
        ref = ContainerSkillRef(name="my-skill")
        assert ref.name == "my-skill"
        assert ref.version is None
        assert str(ref) == "my-skill"

    def test_with_version(self):
        ref = ContainerSkillRef(name="my-skill", version="v2")
        assert ref.version == "v2"
        assert str(ref) == "my-skill@v2"

    def test_container_accepts_bare_strings(self):
        c = SkillContainer(
            name="test",
            description="d",
            skills=["a", "b"],
        )
        assert len(c.skills) == 2
        assert isinstance(c.skills[0], ContainerSkillRef)
        assert c.skill_names == ["a", "b"]

    def test_container_accepts_dicts(self):
        c = SkillContainer(
            name="test",
            description="d",
            skills=[{"name": "a", "version": "v1"}, {"name": "b"}],
        )
        assert c.skills[0].version == "v1"
        assert c.skills[1].version is None
        assert c.skill_names == ["a", "b"]

    def test_container_accepts_mixed(self):
        c = SkillContainer(
            name="test",
            description="d",
            skills=["a", {"name": "b", "version": "v3"}],
        )
        assert c.skills[0].version is None
        assert c.skills[1].version == "v3"

    def test_skills_snapshot(self):
        c = SkillContainer(
            name="test",
            description="d",
            skills=[
                {"name": "a", "version": "v1"},
                {"name": "b", "version": "v2"},
            ],
        )
        assert c.skills_snapshot() == {"a": "v1", "b": "v2"}


# ---------------------------------------------------------------------------
# Container Versioning — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def versioned_project(tmp_path: Path) -> Path:
    """Project with skills that have lineage (v1 committed)."""
    skills_dir = tmp_path / ".agents" / "skills"
    containers_dir = tmp_path / ".agents" / "containers"
    skills_dir.mkdir(parents=True)
    containers_dir.mkdir(parents=True)

    for name in ("alpha-skill", "beta-skill"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill {name}\n---\n\n# {name}\n"
        )
        # Create v1 in .evolution
        evo_dir = d / ".evolution" / "versions" / "v1"
        evo_dir.mkdir(parents=True)
        (evo_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill {name}\n---\n\n# {name}\n"
        )
        # Write lineage.json
        import json

        lineage = {
            "skill_id": name,
            "current_version": "v1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "lineage": [
                {
                    "version": "v1",
                    "parent": None,
                    "status": "active",
                    "fitness": None,
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "origin": "manual",
                }
            ],
        }
        lineage_path = d / ".evolution" / "lineage.json"
        lineage_path.write_text(json.dumps(lineage, indent=2))

    return tmp_path


# ---------------------------------------------------------------------------
# Container Snapshots
# ---------------------------------------------------------------------------


class TestContainerSnapshots:
    def test_snapshot_records_skill_versions(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill", "beta-skill"])
        label = cstore.snapshot("my-pack", "initial snapshot", skill_store=sstore)

        assert label == "s1"
        entries = cstore.snapshots("my-pack")
        assert len(entries) == 1
        assert entries[0]["skills"] == {"alpha-skill": "v1", "beta-skill": "v1"}
        assert entries[0]["message"] == "initial snapshot"

    def test_snapshot_increments_label(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        cstore.snapshot("my-pack", "first", skill_store=sstore)
        label = cstore.snapshot("my-pack", "second", skill_store=sstore)
        assert label == "s2"

    def test_snapshots_returns_history(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        cstore.snapshot("my-pack", "first", skill_store=sstore)
        cstore.snapshot("my-pack", "second", skill_store=sstore)

        entries = cstore.snapshots("my-pack")
        assert len(entries) == 2
        assert entries[0]["label"] == "s1"
        assert entries[1]["label"] == "s2"
        assert "timestamp" in entries[0]

    def test_snapshots_empty_before_snapshot(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        assert cstore.snapshots("my-pack") == []

    def test_snapshot_persisted_to_disk(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        cstore.snapshot("my-pack", "test", skill_store=sstore)

        # Re-read from new store instance
        cstore2 = ContainerStore(project_dir=versioned_project)
        entries = cstore2.snapshots("my-pack")
        assert len(entries) == 1
        assert entries[0]["skills"]["alpha-skill"] == "v1"

    def test_container_yaml_unchanged_after_snapshot(self, versioned_project: Path):
        """Snapshot should not modify the container YAML file."""
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        yaml_path = versioned_project / ".agents" / "containers" / "my-pack.yaml"
        content_before = yaml_path.read_text()

        cstore.snapshot("my-pack", "test", skill_store=sstore)

        content_after = yaml_path.read_text()
        assert content_before == content_after

    def test_resolve_with_snapshot(self, versioned_project: Path):
        """Resolve should load skill versions from a snapshot."""
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        cstore.snapshot("my-pack", "s1 snapshot", skill_store=sstore)

        skills = cstore.resolve("my-pack", sstore, snapshot_label="s1")
        assert len(skills) == 1
        assert skills[0].name == "alpha-skill"

    def test_resolve_with_bad_snapshot_raises(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        with pytest.raises(KeyError, match="s99"):
            cstore.resolve("my-pack", sstore, snapshot_label="s99")
