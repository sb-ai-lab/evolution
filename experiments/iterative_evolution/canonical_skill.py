"""Canonical skill path helpers for the iterative evolution loop."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "experiments"))

from _shared.table3_base_seed import (
    BaseSkillSeed,
    copy_base_skill_dirs,
    resolve_base_skill_seed,
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_skill_path(run_dir: Path, skill_id: str) -> Path:
    return run_dir / "skills" / skill_id / "SKILL.md"


def versions_dir(run_dir: Path, skill_id: str) -> Path:
    return run_dir / "skills" / skill_id / "versions"


def _remove_tree_retry(path: Path) -> None:
    for attempt, delay_s in enumerate((0.05, 0.1, 0.2, 0.5, 1.0), start=1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 5:
                raise
            time.sleep(delay_s)


def snapshot_version(run_dir: Path, skill_id: str, version: str) -> Path:
    """Write immutable ``versions/<version>/SKILL.md``."""
    dst = versions_dir(run_dir, skill_id) / version / "SKILL.md"
    src = canonical_skill_path(run_dir, skill_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def read_canonical(run_dir: Path, skill_id: str) -> str:
    p = canonical_skill_path(run_dir, skill_id)
    if not p.is_file():
        raise FileNotFoundError(f"canonical skill missing: {p}")
    return p.read_text(encoding="utf-8", errors="replace")


def write_canonical(run_dir: Path, skill_id: str, body: str) -> Path:
    p = canonical_skill_path(run_dir, skill_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def seed_v0(
    *,
    run_dir: Path,
    corpus_root: Path,
    skill_id: str,
    source_task_ids: list[str],
) -> dict[str, Any]:
    """Seed v0 from SKILL_HANDCRAFT.md (fallback SKILL.md) via table3_base_seed."""
    specs = [{"task_id": tid} for tid in source_task_ids]
    cells = {
        "corpus_root": str(corpus_root),
        "tasks": {tid: {"skills": [skill_id]} for tid in source_task_ids},
    }
    seed = resolve_base_skill_seed(cells=cells, specs=specs)
    if skill_id not in seed.skill_names:
        raise ValueError(f"skill {skill_id!r} not in resolved seed: {seed.skill_names}")
    staging = run_dir / f"_seed_staging.{os.getpid()}.{time.time_ns()}"
    if staging.exists():
        _remove_tree_retry(staging)
    staging.mkdir(parents=True)
    check = copy_base_skill_dirs(seed, staging)
    src = staging / skill_id / "SKILL.md"
    if not src.is_file():
        raise FileNotFoundError(f"seed SKILL.md missing after copy: {src}")
    body = src.read_text(encoding="utf-8", errors="replace")
    write_canonical(run_dir, skill_id, body)
    snapshot_version(run_dir, skill_id, "v0")
    meta = {
        "skill_id": skill_id,
        "version": "v0",
        "sha256": sha256_text(body),
        "seed_check": check,
        "seed": seed.to_jsonable(),
    }
    (run_dir / "seed_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if staging.exists():
        _remove_tree_retry(staging)
    return meta


def list_version_snapshots(run_dir: Path, skill_id: str) -> list[str]:
    vdir = versions_dir(run_dir, skill_id)
    if not vdir.is_dir():
        return []
    out: list[str] = []
    for child in sorted(vdir.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            out.append(child.name)
    return out


def read_version_body(run_dir: Path, skill_id: str, version: str) -> str:
    p = versions_dir(run_dir, skill_id) / version / "SKILL.md"
    return p.read_text(encoding="utf-8", errors="replace")


__all__ = [
    "BaseSkillSeed",
    "canonical_skill_path",
    "list_version_snapshots",
    "read_canonical",
    "read_version_body",
    "seed_v0",
    "sha256_file",
    "sha256_text",
    "snapshot_version",
    "versions_dir",
    "write_canonical",
]
