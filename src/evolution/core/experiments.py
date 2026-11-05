"""Experiments — isolated skill evolution tracks."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from evolution.core.store import SkillStore


def experiments_root(project_dir: Path) -> Path:
    """Return .agents/experiments/ directory."""
    return project_dir / ".agents" / "experiments"


def list_experiments(project_dir: Path) -> list[dict]:
    """List all experiments with their metadata."""
    root = experiments_root(project_dir)
    if not root.is_dir():
        return []

    results = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "experiment.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            meta = {"name": d.name}
        # Count skills
        skills_dir = d / "skills"
        skill_count = 0
        if skills_dir.is_dir():
            skill_count = sum(
                1 for s in skills_dir.iterdir() if s.is_dir() and (s / "SKILL.md").exists()
            )
        meta["skill_count"] = skill_count
        meta["path"] = str(d)
        results.append(meta)

    return results


def create_experiment(
    project_dir: Path,
    name: str,
    skills: list[dict],
    description: str = "",
) -> Path:
    """Create an experiment with forked skills.

    Args:
        project_dir: Project root.
        name: Experiment name.
        skills: List of {"name": str, "version": str} dicts.
        description: Optional description.

    Returns:
        Path to experiment directory.
    """
    root = experiments_root(project_dir)
    exp_dir = root / name

    if exp_dir.exists():
        raise FileExistsError(f"Experiment {name!r} already exists at {exp_dir}")

    skills_dir = exp_dir / "skills"
    skills_dir.mkdir(parents=True)

    # Load from main store
    main_store = SkillStore(project_dir=project_dir, user_dir=Path.home())

    for spec in skills:
        skill_name = spec["name"]
        version = spec.get("version", "v1")

        if not main_store.has(skill_name):
            raise KeyError(f"Skill {skill_name!r} not found in main store")

        skill = main_store.get(skill_name)

        # Load specific version
        from evolution.lineage.version import VersionStore

        vs = VersionStore(skill)
        if not vs.exists(version):
            raise KeyError(f"Version {version!r} not found for skill {skill_name!r}")

        # Copy skill at that version into experiment
        dest = skills_dir / skill_name
        dest.mkdir(parents=True)

        # Copy SKILL.md from the version snapshot
        version_dir = vs.versions_dir / version
        shutil.copy2(version_dir / "SKILL.md", dest / "SKILL.md")

        # Copy resource dirs if they exist in the version
        for subdir in ("scripts", "references", "assets"):
            src_sub = version_dir / subdir
            if src_sub.is_dir() and any(src_sub.iterdir()):
                shutil.copytree(src_sub, dest / subdir)

        # Initialize .evolution/ with v1 snapshot
        _init_experiment_skill(dest, skill_name, version)

    # Write experiment metadata
    meta = {
        "name": name,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "forked_from": {s["name"]: s.get("version", "v1") for s in skills},
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )

    return exp_dir


def get_experiment_store(project_dir: Path, name: str) -> SkillStore:
    """Get a SkillStore scoped to an experiment.

    Falls back to main store for skills not in the experiment.
    """
    exp_dir = experiments_root(project_dir) / name
    if not exp_dir.is_dir():
        raise KeyError(f"Experiment {name!r} not found")

    # Experiment skills take priority, main store as fallback
    return SkillStore(
        project_dir=None,
        user_dir=None,
        extra_dirs=[exp_dir / "skills", project_dir / ".agents" / "skills"],
    )


def delete_experiment(project_dir: Path, name: str) -> None:
    """Delete an experiment directory."""
    exp_dir = experiments_root(project_dir) / name
    if not exp_dir.is_dir():
        raise KeyError(f"Experiment {name!r} not found")
    shutil.rmtree(exp_dir)


def _init_experiment_skill(
    skill_dir: Path,
    skill_name: str,
    source_version: str,
) -> None:
    """Initialize .evolution/ for a forked skill in an experiment."""
    evo_dir = skill_dir / ".evolution"
    evo_dir.mkdir(exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    lineage_data = {
        "skill_id": skill_name,
        "current_version": "v1",
        "created_at": now,
        "lineage": [
            {
                "version": "v1",
                "timestamp": now,
                "origin": "fork",
                "parent": None,
                "mutation_type": None,
                "fitness": None,
                "status": "active",
                "message": f"Forked from {source_version}",
            }
        ],
    }
    (evo_dir / "lineage.json").write_text(
        json.dumps(lineage_data, indent=2),
        encoding="utf-8",
    )

    # Snapshot v1
    v1_dir = evo_dir / "versions" / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_dir / "SKILL.md", v1_dir / "SKILL.md")
    for subdir in ("scripts", "references", "assets"):
        src_sub = skill_dir / subdir
        if src_sub.is_dir() and any(src_sub.iterdir()):
            shutil.copytree(src_sub, v1_dir / subdir)
