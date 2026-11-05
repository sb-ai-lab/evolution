"""Canonical base-skill seeding helpers for Table 3 produce runs.

Rows 3-8 compare update mechanisms, so the base procedural state must be
derived once and materialized consistently for every framework.

Seeds prefer the human-authored ``SKILL_HANDCRAFT.md`` body so that Table 3
evolution starts from the same baseline as Table 4 Static-SkillsBench. If a
skill has no handcraft artifact, the canonical ``SKILL.md`` is used as the
fallback source and recorded in provenance.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import re
from evolution.core.models import SkillFrontmatter

BASE_SEED_POLICY = "source_trace_task_skill_union_v1"
HANDCRAFT_FILENAME = "SKILL_HANDCRAFT.md"
FALLBACK_FILENAME = "SKILL.md"
PREFERRED_SKILL_FILENAMES = (HANDCRAFT_FILENAME, FALLBACK_FILENAME)


@dataclass(frozen=True)
class BaseSkillSeed:
    """Canonical SB-Bench skill seed for one Table 3 produce artifact."""

    policy: str
    corpus_root: Path
    source_task_ids: list[str]
    skill_names: list[str]
    skill_dirs: dict[str, Path]
    skill_files: dict[str, str]
    skill_hashes: dict[str, str]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "corpus_root": str(self.corpus_root),
            "seed_source_task_ids": list(self.source_task_ids),
            "base_skill_names": list(self.skill_names),
            "base_skill_source_files": dict(self.skill_files),
            "base_skill_hashes": dict(self.skill_hashes),
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_skill_source(skill_dir: Path) -> tuple[str, Path] | None:
    for filename in PREFERRED_SKILL_FILENAMES:
        path = skill_dir / filename
        if path.is_file():
            return filename, path
    return None


def repair_seed_skill_name(skill: str, text: str) -> str:
    """Rewrite frontmatter ``name:`` to the folder skill id.

    SB-Bench skill identity is the directory name (for example
    ``skills/pipelines``), while hand-authored frontmatter can carry a broader
    display/category name. Runtime stores and native adapters must still index
    the staged seed by the folder id.
    """

    match = re.search(r"(?m)^name:[ \t]*(.+?)[ \t]*$", text)
    if match is None:
        return text
    current = match.group(1).strip().strip("'\"")
    if current == skill:
        return text
    try:
        SkillFrontmatter.model_validate({"name": skill, "description": "seed"})
    except Exception:
        return text
    return re.sub(r"(?m)^name:.*$", f"name: {skill}", text, count=1)


def resolve_base_skill_seed(
    *,
    cells: dict[str, Any],
    specs: list[dict[str, Any]],
) -> BaseSkillSeed:
    """Resolve canonical base skills from source trace task ids.

    The seed intentionally follows the source traces, not the eval target,
    so GenR does not leak the partner role's target skill into the source
    artifact.
    """

    corpus_root = Path(cells["corpus_root"]).resolve()
    source_task_ids: list[str] = []
    skill_names: list[str] = []
    for spec in specs:
        task_id = str(spec["task_id"])
        if task_id not in cells.get("tasks", {}):
            raise KeyError(f"trace task {task_id!r} missing from resolved cells")
        if task_id not in source_task_ids:
            source_task_ids.append(task_id)
        for skill in cells["tasks"][task_id].get("skills", []) or []:
            skill = str(skill)
            if skill not in skill_names:
                skill_names.append(skill)

    if not source_task_ids:
        raise ValueError("cannot seed Table 3 produce run: trace spec has no task ids")
    if not skill_names:
        raise ValueError(
            f"cannot seed Table 3 produce run: source tasks have no skills: {source_task_ids}"
        )

    skill_dirs: dict[str, Path] = {}
    skill_files: dict[str, str] = {}
    skill_hashes: dict[str, str] = {}
    missing: list[str] = []
    for skill in skill_names:
        skill_dir = corpus_root / "skills" / skill
        resolved = _resolve_skill_source(skill_dir)
        if resolved is None:
            missing.append(
                f"{skill}: {skill_dir / HANDCRAFT_FILENAME} or {skill_dir / FALLBACK_FILENAME}"
            )
            continue
        filename, source = resolved
        skill_dirs[skill] = skill_dir
        skill_files[skill] = filename
        body = source.read_text(encoding="utf-8")
        normalized = repair_seed_skill_name(skill, body)
        skill_hashes[skill] = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if normalized != body
            else _sha256(source)
        )
    if missing:
        raise FileNotFoundError("missing canonical SB-Bench skill source(s): " + "; ".join(missing))

    return BaseSkillSeed(
        policy=BASE_SEED_POLICY,
        corpus_root=corpus_root,
        source_task_ids=source_task_ids,
        skill_names=skill_names,
        skill_dirs=skill_dirs,
        skill_files=skill_files,
        skill_hashes=skill_hashes,
    )


def base_skill_bodies(seed: BaseSkillSeed) -> list[tuple[str, str]]:
    return [
        (
            skill,
            (seed.skill_dirs[skill] / seed.skill_files[skill]).read_text(
                encoding="utf-8", errors="replace"
            ),
        )
        for skill in seed.skill_names
    ]


def copy_base_skill_dirs(seed: BaseSkillSeed, dest_root: Path) -> dict[str, Any]:
    """Copy canonical seed skill directories into ``dest_root``.

    Whole-directory copy preserves auxiliary assets (``references/``,
    ``assets/``) that live alongside ``SKILL_HANDCRAFT.md``. The destination
    ``SKILL.md`` is then overwritten with the resolved preferred source body
    (handcraft when present, otherwise the canonical ``SKILL.md`` fallback).

    Existing destination skill directories are replaced so a stale produced
    state cannot silently override the canonical seed.
    """

    dest_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for skill in seed.skill_names:
        src = seed.skill_dirs[skill]
        dst = dest_root / skill
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=False)
        src_md = src / seed.skill_files[skill]
        body = src_md.read_text(encoding="utf-8")
        normalized = repair_seed_skill_name(skill, body)
        if normalized != body:
            (dst / "SKILL.md").write_text(normalized, encoding="utf-8")
        else:
            shutil.copy2(src_md, dst / "SKILL.md")
        copied.append(skill)
    check = verify_base_skill_dirs(seed, dest_root)
    check["copied"] = copied
    return check


def verify_base_skill_dirs(seed: BaseSkillSeed, dest_root: Path) -> dict[str, Any]:
    missing: list[str] = []
    hash_mismatch: list[str] = []
    for skill in seed.skill_names:
        p = dest_root / skill / "SKILL.md"
        if not p.is_file():
            missing.append(skill)
            continue
        if _sha256(p) != seed.skill_hashes[skill]:
            hash_mismatch.append(skill)
    return {
        "base_seed_ok": not missing and not hash_mismatch,
        "base_seed_missing": missing,
        "base_seed_hash_mismatch": hash_mismatch,
        "base_seed_dest": str(dest_root),
    }


__all__ = [
    "BASE_SEED_POLICY",
    "BaseSkillSeed",
    "FALLBACK_FILENAME",
    "HANDCRAFT_FILENAME",
    "PREFERRED_SKILL_FILENAMES",
    "base_skill_bodies",
    "copy_base_skill_dirs",
    "resolve_base_skill_seed",
    "verify_base_skill_dirs",
]
