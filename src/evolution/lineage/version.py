"""Store and retrieve skill version snapshots."""

from __future__ import annotations

import shutil
from pathlib import Path

from evolution.core.models import Skill
from evolution.core.parser import load_skill


class VersionStore:
    """Manages version snapshots in .evolution/versions/.

    Layout:
        .evolution/versions/
        ├── v1/
        │   ├── SKILL.md
        │   └── scripts/...
        ├── v2/
        │   └── SKILL.md
        └── v3-candidate-1/
            └── SKILL.md
    """

    def __init__(self, skill: Skill) -> None:
        self._skill = skill
        self._versions_dir = skill.path / ".evolution" / "versions"
        self._versions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def versions_dir(self) -> Path:
        return self._versions_dir

    def save(self, version: str, source: Skill | None = None) -> Path:
        """Snapshot a skill under the given version label.

        If *source* is None, snapshots self._skill (the current active skill).
        Copies SKILL.md + scripts/ + references/ + assets/.
        Returns the path to the version directory.
        """
        source = source or self._skill
        dest = self._versions_dir / version

        if dest.exists():
            shutil.rmtree(dest)

        dest.mkdir(parents=True)

        # Copy SKILL.md
        shutil.copy2(source.skill_md_path, dest / "SKILL.md")

        # Copy resource dirs if they exist and have content
        for subdir in ("scripts", "references", "assets"):
            src_dir = source.path / subdir
            if src_dir.is_dir() and any(src_dir.iterdir()):
                shutil.copytree(src_dir, dest / subdir)

        return dest

    def load(self, version: str) -> Skill:
        """Load a historical version as a Skill object.

        Raises KeyError if the version doesn't exist.
        """
        version_dir = self._versions_dir / version
        if not version_dir.exists():
            raise KeyError(f"Version {version!r} not found")
        return load_skill(version_dir)

    def list_versions(self) -> list[str]:
        """All stored version labels, sorted alphabetically."""
        if not self._versions_dir.exists():
            return []
        return sorted(
            d.name for d in self._versions_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
        )

    def list_candidates(self) -> list[str]:
        """Version labels containing 'candidate'."""
        return [v for v in self.list_versions() if "candidate" in v]

    def delete(self, version: str) -> None:
        """Remove a version snapshot."""
        version_dir = self._versions_dir / version
        if version_dir.exists():
            shutil.rmtree(version_dir)

    def exists(self, version: str) -> bool:
        return (self._versions_dir / version / "SKILL.md").exists()
