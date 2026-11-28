"""Materials revision of a project (spec §3.4)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .clone import scrubbed_environment
from .inventory import Entry, HashCache, collect, inventory_digest
from .models import Project

REVISION_SCHEMA = "lab-materials-revision/1"


@dataclass(frozen=True, slots=True)
class Revision:
    digest: str
    places: tuple[dict, ...]  # {"role", "id"?, "path", "digest"}
    files: int
    git: dict | None
    entries: tuple[Entry, ...]
    # Per-place provenance is internal only; ``entries`` and the wire digest
    # remain the same union used by existing consumers.
    place_entries: tuple[tuple[str, str | None, tuple[Entry, ...]], ...] = ()

    def entries_for_place(self, role: str, implementation: str | None = None) -> tuple[Entry, ...] | None:
        return next((entries for item_role, item_id, entries in self.place_entries
                     if item_role == role and item_id == implementation), None)

    def place_digest(self, role: str, implementation: str | None = None) -> str | None:
        for place in self.places:
            if place["role"] == role and place.get("id") == implementation:
                return place["digest"]
        return None

    def to_payload(self) -> dict[str, Any]:
        return {"schema": REVISION_SCHEMA, "digest": self.digest, "places": [dict(p) for p in self.places],
                "files": self.files, "git": self.git}


def git_state(path: Path) -> dict | None:
    # --no-optional-locks: a poll must never refresh (write) the team's .git/index behind
    # their back or race their own git for index.lock (final review I7).
    # Scrubbed: an inherited GIT_DIR/GIT_WORK_TREE would describe another repository (codex r1 of the dev35 branch).
    env = scrubbed_environment()

    def run(*argv: str) -> str | None:
        try:
            result = subprocess.run(["git", "--no-optional-locks", "-C", str(path), *argv], env=env,
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None
    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=normal")
    return {"commit": None if head is None else head.strip(), "clean": None if status is None else status.strip() == ""}


def _prefix(place: str) -> str:
    return "" if place in (".", "") else place


def _nested_cut(place_path: Path, place_prefix: str, materials_path: Path) -> str | None:
    """The entry path of a materials root lying strictly inside an implementation place, or None (W84, spec
    2026-10-02 §3.1). Paths are compared lexically after normpath: the inventory never follows symlinks."""
    place = PurePosixPath(os.path.normpath(place_path))
    materials = PurePosixPath(os.path.normpath(materials_path))
    if materials == place or place not in materials.parents:
        return None
    relative = materials.relative_to(place).as_posix()
    return f"{place_prefix}/{relative}" if place_prefix else relative


def materials_revision(project: Project, *, cache: HashCache | None = None) -> Revision:
    working_copy = Path(project.working_copy.path)
    materials_path = working_copy / project.materials_root
    places: list[dict] = []
    union: dict[str, Entry] = {}
    place_entries: list[tuple[str, str | None, tuple[Entry, ...]]] = []
    specs = [("materials_root", None, materials_path, _prefix(project.materials_root))]
    for item in project.active_implementations():
        absolute = PurePosixPath(item.place).is_absolute()
        specs.append(("implementation", item.id, Path(item.place) if absolute else working_copy / item.place,
                      f"@{item.id}" if absolute else _prefix(item.place)))
    for role, item_id, path, prefix in specs:
        entries = collect(path, prefix=prefix, exclude=project.exclude, cache=cache)
        union.update({entry.path: entry for entry in entries})
        own = entries
        cut = _nested_cut(path, prefix, materials_path) if role == "implementation" else None
        if cut is not None:  # the agent's bytes, not the task's: ТЗ and package writes leave the place alone (W84)
            own = tuple(entry for entry in entries if entry.path != cut and not entry.path.startswith(cut + "/"))
        place_entries.append((role, item_id, own))
        place = {"role": role, "path": prefix or ".", "digest": inventory_digest(own)}
        if item_id is not None:
            place["id"] = item_id
        places.append(place)
    entries = tuple(sorted(union.values(), key=lambda entry: entry.path))
    digest = inventory_digest(entries, extra={"places": [{k: v for k, v in p.items() if k != "digest"} for p in places]})
    return Revision(digest, tuple(places), sum(1 for e in entries if e.type == "file"),
                    git_state(working_copy) if project.working_copy.git else None, entries, tuple(place_entries))
