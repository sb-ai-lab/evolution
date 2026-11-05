"""Exact skill-selection and provenance invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.bindings import SkillBinding, resolve_skill_bindings
from evolution.core.containers import ContainerStore
from evolution.core.models import ContainerSkillRef
from evolution.core.store import SkillStore
from evolution.eval.task import Task
from evolution.lineage.version import VersionStore
from evolution.workflow import resolve_task_bindings


def _create_skill(store: SkillStore, name: str):
    skill, _ = store.create(name=name, description=f"Test {name}")
    return skill


def test_active_resolution_ignores_dirty_working_tree(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")
    dirty_script = owner.path / "scripts" / "dirty.py"
    dirty_script.write_text("print('dirty')\n", encoding="utf-8")
    owner.skill_md_path.write_text(
        owner.skill_md_path.read_text(encoding="utf-8") + "\nDIRTY WORKING MARKER\n",
        encoding="utf-8",
    )

    [active] = resolve_skill_bindings(["alpha"], store=store, project_dir=tmp_path)
    [working] = resolve_skill_bindings(["alpha"], store=store, project_dir=tmp_path, working=True)

    assert active.version == "v1"
    assert active.source == "active"
    assert "DIRTY WORKING MARKER" not in active.skill.body
    assert not (active.skill.path / "scripts" / "dirty.py").exists()
    assert working.version.startswith("working@")
    assert "DIRTY WORKING MARKER" in working.skill.body
    assert (working.skill.path / "scripts" / "dirty.py").exists()


def test_identity_covers_managed_tree_only(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")
    baseline = SkillBinding.working(owner).sha256

    for excluded in (".evolution/note", ".traces/note", "evals/note"):
        path = owner.path / excluded
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored\n", encoding="utf-8")
    assert SkillBinding.working(owner).sha256 == baseline

    managed_hashes: set[str] = set()
    for managed in ("scripts/run.py", "references/guide.md", "assets/icon.bin"):
        path = owner.path / managed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"managed")
        managed_hashes.add(SkillBinding.working(owner).sha256)
    assert len(managed_hashes) == 3

    script = owner.path / "scripts" / "run.py"
    before_content = SkillBinding.working(owner).sha256
    script.write_bytes(b"changed")
    after_content = SkillBinding.working(owner).sha256
    script.chmod(0o755)
    after_mode = SkillBinding.working(owner).sha256
    script.rename(script.with_name("renamed.py"))
    after_path = SkillBinding.working(owner).sha256
    assert len({before_content, after_content, after_mode, after_path}) == 4


def test_pinned_version_verifies_optional_hash(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")
    digest = VersionStore(owner).digest("v1")

    [binding] = resolve_skill_bindings(
        [ContainerSkillRef(name="alpha", version="v1", sha256=digest)],
        store=store,
        project_dir=tmp_path,
    )
    assert binding.sha256 == digest

    with pytest.raises(ValueError, match="hash mismatch"):
        resolve_skill_bindings(
            [ContainerSkillRef(name="alpha", version="v1", sha256="0" * 64)],
            store=store,
            project_dir=tmp_path,
        )


def test_binding_provenance_uses_full_tree_sha256(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")

    binding = SkillBinding.pinned(owner, "v1")

    assert binding.provenance() == {
        "name": "alpha",
        "version": "v1",
        "sha256": VersionStore(owner).digest("v1"),
        "source": "version",
    }


def test_from_skill_keeps_snapshot_owner_canonical(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")

    binding = SkillBinding.from_skill(VersionStore(owner).load("v1"))

    assert binding.owner.path == owner.path
    assert binding.skill.path != owner.path
    assert binding.version == "v1"
    assert binding.sha256 == VersionStore(owner).digest("v1")


def test_resolution_preserves_requirement_order_and_deduplicates_owner(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    _create_skill(store, "alpha")
    _create_skill(store, "beta")
    containers = ContainerStore(project_dir=tmp_path)
    containers.create("pack", "Test pack", ["alpha", "beta"])

    bindings = resolve_skill_bindings(["alpha", "pack", "beta"], store=store, project_dir=tmp_path)

    assert [binding.name for binding in bindings] == ["alpha", "beta"]


def test_resolution_rejects_conflicting_pins_for_same_owner(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")
    owner.skill_md_path.write_text(
        owner.skill_md_path.read_text(encoding="utf-8") + "\nVERSION TWO\n",
        encoding="utf-8",
    )
    VersionStore(owner).save("v2")

    with pytest.raises(ValueError, match="Conflicting pins"):
        resolve_skill_bindings(
            [
                ContainerSkillRef(name="alpha", version="v1"),
                ContainerSkillRef(name="alpha", version="v2"),
            ],
            store=store,
            project_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("requirements", "selectors"),
    [
        (["alpha"], {"working": True, "skill_version": "v1"}),
        (["pack"], {"working": True, "snapshot": "s1"}),
        (["pack"], {"skill_version": "v1", "snapshot": "s1"}),
        ([ContainerSkillRef(name="alpha", version="v1")], {"working": True}),
        (
            [ContainerSkillRef(name="alpha", version="v1")],
            {"skill_version": "v1"},
        ),
        ([ContainerSkillRef(name="pack", version="v1")], {"snapshot": "s1"}),
    ],
)
def test_resolution_rejects_ambiguous_selectors(
    tmp_path: Path,
    requirements: list[str | ContainerSkillRef],
    selectors: dict[str, str | bool],
) -> None:
    store = SkillStore(project_dir=tmp_path)
    _create_skill(store, "alpha")
    ContainerStore(project_dir=tmp_path).create("pack", "Test pack", ["alpha"])

    with pytest.raises(ValueError, match="selectors cannot be combined"):
        resolve_skill_bindings(
            requirements,
            store=store,
            project_dir=tmp_path,
            **selectors,
        )


def test_container_snapshot_binding_carries_frozen_provenance(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    owner = _create_skill(store, "alpha")
    _create_skill(store, "beta")
    containers = ContainerStore(project_dir=tmp_path)
    containers.create("pack", "Test pack", ["alpha"])
    containers.snapshot("pack", "frozen", skill_store=store)
    containers.update("pack", skills=["beta"])

    [binding] = resolve_skill_bindings(["pack"], store=store, project_dir=tmp_path, snapshot="s1")

    assert binding.owner.path == owner.path
    assert binding.provenance() == {
        "name": "alpha",
        "version": "v1",
        "sha256": VersionStore(owner).digest("v1"),
        "source": "container-snapshot",
        "container": "pack",
        "snapshot": "s1",
    }


def test_task_binding_override_keeps_supporting_requirements(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    _create_skill(store, "primary")
    _create_skill(store, "supporting")
    _create_skill(store, "replacement")
    task = Task(
        name="task",
        instruction="Do it",
        difficulty="easy",
        category="test",
        skills_required=["primary", "supporting"],
        path=tmp_path,
    )

    bindings = resolve_task_bindings(
        task,
        store=store,
        project_dir=tmp_path,
        skill_name="replacement",
    )

    assert [binding.name for binding in bindings] == ["replacement", "supporting"]
    assert all(binding.source == "active" for binding in bindings)
