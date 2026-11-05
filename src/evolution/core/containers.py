"""Container store — manages named groups of skills."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from evolution.core.models import ContainerSkillRef, Skill, SkillContainer


class ContainerStore:
    """Discover, manage, and resolve skill containers.

    Containers are YAML files in .agents/containers/ directories.
    Scan order matches SkillStore: project-level, then user-level.
    """

    def __init__(
        self,
        project_dir: str | Path | None = None,
        user_dir: str | Path | None = None,
    ) -> None:
        self._scan_roots: list[Path] = []

        if project_dir is not None:
            p = Path(project_dir).resolve() / ".agents" / "containers"
            self._scan_roots.append(p)

        if user_dir is not None:
            u = Path(user_dir).expanduser().resolve() / ".agents" / "containers"
            self._scan_roots.append(u)

        self._index: dict[str, SkillContainer] = {}
        self.discover()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[SkillContainer]:
        """Scan all configured directories for containers. Rebuild index."""
        self._index.clear()

        for root in self._scan_roots:
            if not root.is_dir():
                continue
            for f in sorted(root.iterdir()):
                if f.suffix in (".yaml", ".yml") and f.is_file():
                    self._index_container(f)

        return list(self._index.values())

    def _index_container(self, path: Path) -> None:
        """Parse a container YAML file and add to index."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            container = SkillContainer.model_validate(data)
        except Exception:
            return

        # Project-level wins on name collision
        if container.name not in self._index:
            self._index[container.name] = container

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list(self) -> list[SkillContainer]:
        """Return all containers sorted by name."""
        return sorted(self._index.values(), key=lambda c: c.name)

    def get(self, name: str) -> SkillContainer:
        """Get a container by name. Raises KeyError if not found."""
        if name not in self._index:
            raise KeyError(f"Container {name!r} not found")
        return self._index[name]

    def has(self, name: str) -> bool:
        return name in self._index

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        description: str,
        skills: list[str],
        metadata: dict[str, str] | None = None,
    ) -> SkillContainer:
        """Create a new container YAML file."""
        container = SkillContainer(
            name=name,
            description=description,
            skills=skills,
            metadata=metadata,
        )

        if self.has(name):
            raise FileExistsError(f"Container {name!r} already exists")

        root = self._write_root()
        root.mkdir(parents=True, exist_ok=True)
        self._write_yaml(root / f"{name}.yaml", container)
        self._index[name] = container
        return container

    def update(
        self,
        name: str,
        description: str | None = None,
        skills: list | None = None,
        metadata: dict[str, str] | None = None,
    ) -> SkillContainer:
        """Update an existing container."""
        container = self.get(name)
        updates: dict = {}
        if description is not None:
            updates["description"] = description
        if skills is not None:
            # Normalize: accept bare strings, dicts, or ContainerSkillRef
            normalized = []
            for item in skills:
                if isinstance(item, str):
                    normalized.append(ContainerSkillRef(name=item))
                elif isinstance(item, dict):
                    normalized.append(ContainerSkillRef(**item))
                else:
                    normalized.append(item)
            updates["skills"] = normalized
        if metadata is not None:
            existing = container.metadata or {}
            updates["metadata"] = {**existing, **metadata}

        updated = container.model_copy(update=updates)

        # Find and overwrite the file
        path = self._find_file(name)
        self._write_yaml(path, updated)
        self._index[name] = updated
        return updated

    def delete(self, name: str) -> None:
        """Delete a container YAML file."""
        path = self._find_file(name)
        path.unlink()
        del self._index[name]

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        name: str,
        skill_store,
        snapshot_label: str | None = None,
    ) -> list[Skill]:
        """Load all skills from a container.

        Args:
            name: Container name.
            skill_store: A SkillStore instance to load skills from.
            snapshot_label: If given, load skill versions from this snapshot
                instead of current active versions.

        Returns:
            Ordered list of Skill objects.

        Raises:
            KeyError: if container, snapshot, or any skill not found.
        """
        container = self.get(name)

        if snapshot_label:
            # Find the snapshot and load pinned versions
            versions_map = self._get_snapshot_skills(name, snapshot_label)
            from evolution.lineage.version import VersionStore

            result: list[Skill] = []
            for ref in container.skills:
                ver = versions_map.get(ref.name)
                if not ver:
                    raise KeyError(f"Skill {ref.name!r} not found in snapshot {snapshot_label!r}")
                skill = skill_store.get(ref.name)
                vs = VersionStore(skill)
                result.append(vs.load(ver))
            return result

        return [skill_store.get(ref.name) for ref in container.skills]

    def _get_snapshot_skills(self, name: str, label: str) -> dict[str, str]:
        """Get {skill_name: version} from a snapshot."""
        entries = self._load_lineage(name)
        for e in entries:
            if e.get("label") == label:
                return e.get("skills", {})
        raise KeyError(f"Snapshot {label!r} not found for container {name!r}")

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _lineage_path(self, name: str) -> Path:
        root = self._write_root()
        d = root / ".evolution"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.json"

    def _load_lineage(self, name: str) -> list[dict]:
        """Load snapshot history for a container. Returns [] if none."""
        p = self._lineage_path(name)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_lineage(self, name: str, entries: list[dict]) -> None:
        p = self._lineage_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")

    def snapshots(self, name: str) -> list[dict]:
        """Return snapshot history for a container."""
        if not self.has(name):
            raise KeyError(f"Container {name!r} not found")
        return self._load_lineage(name)

    def snapshot(self, name: str, message: str, skill_store=None) -> str:
        """Record a snapshot of current skill versions used by this container.

        Reads each skill's current active version from LineageTracker and
        appends a timestamped entry to the snapshot log. Returns the label.
        """
        container = self.get(name)
        entries = self._load_lineage(name)

        # Compute next label
        max_v = 0
        for e in entries:
            v = e.get("label", "")
            if v.startswith("s") and v[1:].isdigit():
                max_v = max(max_v, int(v[1:]))
        label = f"s{max_v + 1}"

        # Resolve current active version for each skill
        skills_map: dict[str, str] = {}
        for ref in container.skills:
            if skill_store and skill_store.has(ref.name):
                from evolution.lineage.tracker import LineageTracker

                skill = skill_store.get(ref.name)
                tracker = LineageTracker(skill)
                ver = tracker.current_version
                skills_map[ref.name] = ver if ver != "v0" else "v1"
            else:
                skills_map[ref.name] = "unknown"

        entry = {
            "label": label,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "skills": skills_map,
        }
        entries.append(entry)
        self._save_lineage(name, entries)

        return label

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_root(self) -> Path:
        """First scan root (project-level preferred)."""
        if not self._scan_roots:
            raise RuntimeError("No scan roots configured")
        return self._scan_roots[0]

    def _find_file(self, name: str) -> Path:
        """Find the YAML file for a container by name."""
        for root in self._scan_roots:
            for ext in (".yaml", ".yml"):
                path = root / f"{name}{ext}"
                if path.is_file():
                    return path
        raise KeyError(f"Container file not found for {name!r}")

    @staticmethod
    def _write_yaml(path: Path, container: SkillContainer) -> None:
        """Write container to YAML file (always bare skill names)."""
        data: dict = {
            "name": container.name,
            "description": container.description,
            "skills": container.skill_names,
        }
        if container.metadata:
            data["metadata"] = container.metadata
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
