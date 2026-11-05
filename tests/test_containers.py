"""Tests for skill containers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.core.containers import ContainerLineageError, ContainerStore
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
    def test_public_frozen_skill_ref(self):
        ref = ContainerSkillRef(name="skill-a", version="v2", sha256="a" * 64)
        assert ref.model_dump() == {
            "name": "skill-a",
            "version": "v2",
            "sha256": "a" * 64,
        }
        assert str(ref) == "skill-a@v2"

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

    def test_structured_skill_refs_rejected(self):
        with pytest.raises(ValueError):
            SkillContainer(
                name="plain-names-only",
                description="x",
                skills=[{"name": "a", "version": "v1"}],
            )


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

    def test_stale_store_cannot_overwrite_concurrent_create(self, container_dir: Path):
        first = ContainerStore(project_dir=container_dir)
        stale = ContainerStore(project_dir=container_dir)
        first.create(name="shared", description="first", skills=["a"])

        with pytest.raises(FileExistsError):
            stale.create(name="shared", description="second", skills=["b"])


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
        assert c.skills == ["a", "b"]

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
        assert c.skills == ["a", "b", "c"]

    def test_update_rejects_structured_skill_refs(self, cstore: ContainerStore):
        cstore.create(name="plain", description="x", skills=["a"])

        with pytest.raises(ValueError):
            cstore.update("plain", skills=[{"name": "a", "version": "v1"}])

    def test_update_metadata_merges(self, cstore: ContainerStore):
        cstore.create(name="meta", description="x", skills=["a"], metadata={"k1": "v1"})
        c = cstore.update("meta", metadata={"k2": "v2"})
        assert c.metadata == {"k1": "v1", "k2": "v2"}

    def test_stale_store_rereads_container_inside_transaction(self, container_dir: Path):
        first = ContainerStore(project_dir=container_dir)
        first.create(name="shared", description="x", skills=["a"], metadata={"base": "1"})
        stale = ContainerStore(project_dir=container_dir)

        first.update("shared", metadata={"first": "2"})
        stale.update("shared", metadata={"second": "3"})

        persisted = ContainerStore(project_dir=container_dir).get("shared")
        assert persisted.metadata == {"base": "1", "first": "2", "second": "3"}


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

    def test_delete_rejects_absolute_name_without_touching_external_yaml(
        self, cstore: ContainerStore, tmp_path: Path
    ):
        external = tmp_path / "victim.yaml"
        external.write_text("name: victim\ndescription: keep\nskills: [a]\n")

        with pytest.raises(ValueError, match="Invalid container name"):
            cstore.delete(str(external.with_suffix("")))

        assert external.exists()


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
        assert c.skills == ["foo", "bar"]

    def test_skips_invalid_yaml(self, container_dir: Path):
        bad = container_dir / ".agents" / "containers" / "bad.yaml"
        bad.write_text("this is not valid yaml: [[[")
        cstore = ContainerStore(project_dir=container_dir)
        assert not cstore.has("bad")

    def test_skips_symlinked_yaml(self, container_dir: Path, tmp_path: Path):
        external = tmp_path / "external.yaml"
        external.write_text("name: linked\ndescription: outside\nskills: [foo]\n")
        linked = container_dir / ".agents" / "containers" / "linked.yaml"
        linked.symlink_to(external)

        assert not ContainerStore(project_dir=container_dir).has("linked")


def test_container_lineage_rejects_symlinked_state_directory(
    container_dir: Path, tmp_path: Path
) -> None:
    store = ContainerStore(project_dir=container_dir)
    store.create(name="safe", description="safe", skills=["a"])
    outside = tmp_path / "outside-lineage"
    outside.mkdir()
    marker = outside / "safe.json"
    marker.write_text("[]", encoding="utf-8")
    (container_dir / ".agents" / "containers" / ".evolution").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(ValueError, match="escapes"):
        store.snapshots("safe")

    assert marker.read_text(encoding="utf-8") == "[]"


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
    @pytest.mark.parametrize(
        "payload",
        ["{", "{}", '[{"label": 1, "skills": {}}]', '[{"label": "s1"}]'],
    )
    def test_malformed_lineage_json_raises_domain_error(
        self, versioned_project: Path, payload: str
    ):
        cstore = ContainerStore(project_dir=versioned_project)
        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        lineage_path = versioned_project / ".agents" / "containers" / ".evolution" / "my-pack.json"
        lineage_path.parent.mkdir()
        lineage_path.write_text(payload, encoding="utf-8")

        with pytest.raises(ContainerLineageError, match="my-pack"):
            cstore.snapshots("my-pack")

    def test_snapshot_records_skill_versions(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)

        cstore.create(name="my-pack", description="test", skills=["alpha-skill", "beta-skill"])
        label = cstore.snapshot("my-pack", "initial snapshot", skill_store=sstore)

        assert label == "s1"
        entries = cstore.snapshots("my-pack")
        assert len(entries) == 1
        assert [ref["name"] for ref in entries[0]["skill_refs"]] == [
            "alpha-skill",
            "beta-skill",
        ]
        assert all(ref["version"] == "v1" for ref in entries[0]["skill_refs"])
        assert all(len(ref["sha256"]) == 64 for ref in entries[0]["skill_refs"])
        assert "skills" not in entries[0]
        assert entries[0]["message"] == "initial snapshot"

    def test_stale_store_snapshots_current_persisted_membership(self, versioned_project: Path):
        writer = ContainerStore(project_dir=versioned_project)
        writer.create(name="my-pack", description="test", skills=["alpha-skill"])
        stale = ContainerStore(project_dir=versioned_project)
        writer.update("my-pack", skills=["beta-skill"])

        stale.snapshot(
            "my-pack",
            "current membership",
            skill_store=SkillStore(project_dir=versioned_project),
        )

        assert [ref["name"] for ref in stale.snapshots("my-pack")[0]["skill_refs"]] == [
            "beta-skill"
        ]

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
        assert entries[0]["skill_refs"][0]["name"] == "alpha-skill"
        assert entries[0]["skill_refs"][0]["version"] == "v1"

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

    def test_snapshot_freezes_ordered_membership_and_managed_tree_hash(
        self, versioned_project: Path
    ):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)
        version_dir = (
            versioned_project
            / ".agents"
            / "skills"
            / "alpha-skill"
            / ".evolution"
            / "versions"
            / "v1"
        )
        reference = version_dir / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("frozen guide\n", encoding="utf-8")
        cstore.create(
            name="my-pack",
            description="test",
            skills=["beta-skill", "alpha-skill"],
        )
        cstore.snapshot("my-pack", "frozen", skill_store=sstore)
        cstore.update("my-pack", skills=["alpha-skill"])

        resolved = cstore.resolve("my-pack", sstore, snapshot_label="s1")
        assert [skill.name for skill in resolved] == ["beta-skill", "alpha-skill"]

        reference.write_text("tampered guide\n", encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            cstore.resolve("my-pack", sstore, snapshot_label="s1")

    def test_reads_legacy_snapshot_skills_map(self, versioned_project: Path):
        cstore = ContainerStore(project_dir=versioned_project)
        sstore = SkillStore(project_dir=versioned_project)
        cstore.create(name="my-pack", description="test", skills=["alpha-skill"])
        lineage_path = versioned_project / ".agents" / "containers" / ".evolution" / "my-pack.json"
        lineage_path.parent.mkdir()
        lineage_path.write_text(
            json.dumps(
                [
                    {
                        "label": "s1",
                        "message": "legacy",
                        "skills": {"beta-skill": "v1", "alpha-skill": "v1"},
                    }
                ]
            ),
            encoding="utf-8",
        )

        resolved = cstore.resolve("my-pack", sstore, snapshot_label="s1")

        assert [skill.name for skill in resolved] == ["beta-skill", "alpha-skill"]
