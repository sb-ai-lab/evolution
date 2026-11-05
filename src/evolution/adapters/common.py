"""Shared utilities for framework adapters.

This module intentionally depends only on the standard library plus the
Evolution package's own modules so adapters can be imported from any
subprocess without dragging in every framework's heavy deps.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from evolution.splits.manifest import (
    REPORTING,
    SKILL_EVOLUTION,
    SplitManifest,
)

if TYPE_CHECKING:
    from evolution.core.store import SkillStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repository locations
# ---------------------------------------------------------------------------

#: Evolution **code checkout** root - resolved from this file's location, not
#: from CWD, so adapters invoked by pytest or subprocesses still see the right
#: paths. This MUST stay the source checkout: imports, ``REPO_ROOT/"experiments"``,
#: ``REPO_ROOT/"tasks"`` and other read-only asset reads depend on it.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Isolated **mutable project root**. Concurrent anchor workers each mutate
#: ``.agents/skills/<skill>/``; a shared root races. Each worker overrides
#: ``EVO_PROJECT_ROOT`` so its mutable skill state lives in its own tree.
#: Defaults to ``REPO_ROOT`` (single-process behaviour is unchanged).
PROJECT_ROOT = Path(os.environ.get("EVO_PROJECT_ROOT") or REPO_ROOT).resolve()

#: Mutable skill store. Derived from ``PROJECT_ROOT`` (not ``REPO_ROOT``) so
#: per-worker isolation also covers the on-disk skill state. May be pointed
#: elsewhere directly via ``EVO_SKILLS_ROOT``.
SKILLS_ROOT = Path(
    os.environ.get("EVO_SKILLS_ROOT") or (PROJECT_ROOT / ".agents" / "skills")
).resolve()

#: Read-only task assets — anchored at the code checkout, never the
#: per-worker mutable root.
TASKS_ROOT = REPO_ROOT / "tasks"
DEFAULT_PILOT_MANIFEST = REPO_ROOT / "experiments" / "smoke-4way" / "pilot_manifest.json"
DEFAULT_SPLIT_MANIFEST = (
    REPO_ROOT / "splits" / "skillsbench" / "manifests" / "domain-stratified-v1.json"
)


# ---------------------------------------------------------------------------
# Pilot manifest loader (thin wrapper over the split manifest)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotTask:
    id: str
    split: str  # "train" | "test"
    difficulty: str
    domain_norm: str
    skills: tuple[str, ...]
    # Procedural-memory cell identifier (e.g., "de.evidence_finding_triage").
    # Populated for sb-bench tasks; may be empty for evhmm/skillsbench.
    # Adapters use this to map test → train skills within the same cell.
    cell_id: str = ""


@dataclass
class PilotManifest:
    name: str
    raw: dict
    train: list[PilotTask] = field(default_factory=list)
    test: list[PilotTask] = field(default_factory=list)
    upstream: SplitManifest | None = None

    @classmethod
    def load(
        cls,
        pilot_path: str | Path = DEFAULT_PILOT_MANIFEST,
        upstream_path: str | Path | None = None,
    ) -> PilotManifest:
        pilot_path = Path(pilot_path).resolve()
        data = json.loads(pilot_path.read_text(encoding="utf-8"))
        upstream_rel = data.get("upstream_manifest", "")
        if upstream_path is None and upstream_rel:
            upstream_path = (REPO_ROOT.parent / upstream_rel).resolve()
            if not upstream_path.exists():
                # repo root-relative form
                upstream_path = (REPO_ROOT / upstream_rel).resolve()
        upstream = (
            SplitManifest.load(upstream_path)
            if upstream_path and Path(upstream_path).exists()
            else None
        )

        def _mk(split: str, entry: dict) -> PilotTask:
            return PilotTask(
                id=entry["id"],
                split=split,
                difficulty=entry.get("difficulty", ""),
                domain_norm=entry.get("domain_norm", ""),
                skills=tuple(entry.get("skills", []) or []),
                cell_id=entry.get("cell_id", "") or entry.get("group", "") or "",
            )

        train = [_mk("train", e) for e in data["tasks"]["train"]]
        test = [_mk("test", e) for e in data["tasks"]["test"]]
        return cls(
            name=data.get("name", pilot_path.stem),
            raw=data,
            train=train,
            test=test,
            upstream=upstream,
        )

    # ------------------------------------------------------------------
    def train_ids(self) -> list[str]:
        return [t.id for t in self.train]

    def test_ids(self) -> list[str]:
        return [t.id for t in self.test]

    def all_ids(self) -> list[str]:
        return [t.id for t in self.train + self.test]

    def task(self, task_id: str) -> PilotTask:
        for t in self.train + self.test:
            if t.id == task_id:
                return t
        raise KeyError(f"Unknown pilot task: {task_id}")

    # ------------------------------------------------------------------
    def assert_leakage_free(self) -> None:
        """Verify pilot's train-set has no overlap with upstream test-set
        AND no overlap between pilot-train and pilot-test internally."""
        # Internal partition — guards against malformed runtime manifests
        # listing the same task in both lanes.
        pilot_train = set(self.train_ids())
        pilot_test = set(self.test_ids())
        internal = pilot_train & pilot_test
        if internal:
            raise RuntimeError(
                f"Pilot internal leakage: tasks in both train and test: {sorted(internal)}"
            )
        if self.upstream is None:
            return  # no upstream, nothing further to assert against
        up_test = self.upstream.ids(REPORTING)
        violators = pilot_train & up_test
        if violators:
            raise RuntimeError(
                f"Pilot leakage: these pilot-train tasks are upstream-test: {sorted(violators)}"
            )
        # Conversely, pilot-test tasks should live in upstream-test.
        up_test_set = set(up_test)
        stray = pilot_test - up_test_set
        if stray:
            raise RuntimeError(f"Pilot-test tasks not in upstream test split: {sorted(stray)}")

    def model_config(self, which: str = "primary") -> dict:
        return dict(self.raw.get("models", {}).get(which, {}))


# ---------------------------------------------------------------------------
# SKILL.md authoring
# ---------------------------------------------------------------------------

_FRONTMATTER_SAFE_CHARS = re.compile(r"[^0-9a-zA-Z \-_.,/:;()']+")


def _sanitize_for_frontmatter(value: str, *, max_len: int = 400) -> str:
    text = _FRONTMATTER_SAFE_CHARS.sub(" ", value).strip()
    # collapse whitespace and cap length
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "..."
    return text


def _sanitize_skill_name(name: str) -> str:
    """Produce a filesystem-safe skill directory name."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", name.strip().lower()).strip("-")
    return slug or "unnamed-skill"


def write_skill_md(
    *,
    name: str,
    description: str,
    body: str,
    source: str,
    extra_metadata: dict | None = None,
    skills_root: Path = SKILLS_ROOT,
    overwrite: bool = True,
) -> Path:
    """Write a SKILL.md file conforming to Evolution's parser expectations.

    Returns the path to the written skill directory.
    """
    skill_dir = skills_root / _sanitize_skill_name(name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / "SKILL.md"
    if md_path.exists() and not overwrite:
        return skill_dir

    desc = _sanitize_for_frontmatter(description or name, max_len=400)
    fm_lines = [
        "---",
        f"name: {_sanitize_skill_name(name)}",
        f"description: {desc}",
        "license: MIT",
        "metadata:",
        f"  source: {source}",
    ]
    for k, v in (extra_metadata or {}).items():
        fm_lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
    fm_lines.append("---")
    md_text = "\n".join(fm_lines) + "\n\n" + (body or "").strip() + "\n"
    md_path.write_text(md_text, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# Task-path helpers
# ---------------------------------------------------------------------------


def evolution_task_dir(task_id: str) -> Path:
    p = TASKS_ROOT / task_id
    if not p.exists():
        raise FileNotFoundError(f"Task not adapted in Evolution tree: {task_id} (expected at {p})")
    return p


def task_instruction(task_id: str) -> str:
    return (evolution_task_dir(task_id) / "instruction.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Environment sanity
# ---------------------------------------------------------------------------


def getenv_or(raise_msg: str, *keys: str) -> str:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    raise RuntimeError(raise_msg + f" (tried env vars: {list(keys)})")


def ordered_unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def slug(s: str) -> str:
    """Filesystem-safe slug used by memento/evoskill adapters when
    minting skill directory names.

    50-char ceiling. Inputs ≤50 chars pass through unchanged (preserves
    byte-identical names for short cases). Longer inputs get a sha1
    hash-suffix to avoid collisions when multiple long names share a
    common prefix (otherwise two distinct tasks could land at the same
    slug, the second overwriting the first in the SkillStore).
    """
    s = re.sub(r"[^0-9a-zA-Z]+", "-", s.lower()).strip("-")
    if not s:
        return "skill"
    if len(s) <= 50:
        return s
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    return f"{s[:41]}-{h}"


# ---------------------------------------------------------------------------
# Baseline skill composition
# ---------------------------------------------------------------------------


class BaselineMode(str, enum.Enum):
    """How generated framework skills are blended with curated task skills."""

    NONE = "none"
    CURATED_ONLY = "curated_only"
    CURATED_PLUS_GENERATED = "curated_plus_generated"
    GENERATED_PLUS_CURATED = "generated_plus_curated"


def resolve_baseline_mode(value: str | None = None) -> BaselineMode:
    """Resolve ``SKILL_BASELINE_MODE`` into a stable enum.

    The default is produced-only. Publication runners should make any curated
    baseline blending explicit in their manifests rather than depending on a
    hidden environment default.
    """

    raw = (value or os.environ.get("SKILL_BASELINE_MODE") or BaselineMode.NONE.value).strip()
    normalised = raw.lower().replace("-", "_")
    aliases = {
        "": BaselineMode.NONE,
        "0": BaselineMode.NONE,
        "false": BaselineMode.NONE,
        "none": BaselineMode.NONE,
        "generated": BaselineMode.NONE,
        "produced": BaselineMode.NONE,
        "curated": BaselineMode.CURATED_ONLY,
        "curated_only": BaselineMode.CURATED_ONLY,
        "base": BaselineMode.CURATED_ONLY,
        "baseline": BaselineMode.CURATED_ONLY,
        "curated_plus_generated": BaselineMode.CURATED_PLUS_GENERATED,
        "baseline_plus_generated": BaselineMode.CURATED_PLUS_GENERATED,
        "base_plus_generated": BaselineMode.CURATED_PLUS_GENERATED,
        "generated_plus_curated": BaselineMode.GENERATED_PLUS_CURATED,
        "generated_plus_baseline": BaselineMode.GENERATED_PLUS_CURATED,
    }
    try:
        return aliases[normalised]
    except KeyError as exc:
        allowed = ", ".join(sorted(aliases))
        raise ValueError(
            f"Unknown SKILL_BASELINE_MODE={raw!r}; allowed aliases: {allowed}"
        ) from exc


def _curated_for_task(task: PilotTask, skill_store: SkillStore | None = None) -> list[str]:
    names = [s for s in task.skills if s]
    if skill_store is None:
        return names
    return [name for name in names if skill_store.has(name)]


def _compose_names(
    generated: Sequence[str],
    curated: Sequence[str],
    *,
    mode: BaselineMode,
) -> list[str]:
    if mode == BaselineMode.NONE:
        return ordered_unique(generated)
    if mode == BaselineMode.CURATED_ONLY:
        return ordered_unique(curated)
    if mode == BaselineMode.CURATED_PLUS_GENERATED:
        return ordered_unique([*curated, *generated])
    if mode == BaselineMode.GENERATED_PLUS_CURATED:
        return ordered_unique([*generated, *curated])
    raise AssertionError(f"unhandled BaselineMode: {mode}")


def compose_per_task_skills(
    generated_per_task: dict[str, list[str]],
    *,
    tasks: Sequence[PilotTask],
    mode: BaselineMode | None = None,
    skill_store: SkillStore | None = None,
) -> dict[str, list[str]]:
    """Compose train-task skill maps for framework-produced skills."""

    resolved = mode or resolve_baseline_mode()
    out: dict[str, list[str]] = {}
    for task in tasks:
        generated = generated_per_task.get(task.id, [])
        curated = _curated_for_task(task, skill_store)
        out[task.id] = _compose_names(generated, curated, mode=resolved)
    return out


def compose_test_time_skills(
    generated: Sequence[str],
    *,
    tasks: Sequence[PilotTask],
    mode: BaselineMode | None = None,
    skill_store: SkillStore | None = None,
) -> dict[str, list[str]]:
    """Compose test-task skill maps for generated global skills."""

    resolved = mode or resolve_baseline_mode()
    out: dict[str, list[str]] = {}
    for task in tasks:
        curated = _curated_for_task(task, skill_store)
        out[task.id] = _compose_names(generated, curated, mode=resolved)
    return out


def minimal_frontmatter_compat(text: str, dest_name: str) -> tuple[str, list[str]]:
    """Minimal frontmatter-compatibility layer for Evolution's SkillStore.

    Only enforces what the SkillStore parser strictly requires:
      * a frontmatter block delimited by ``---``;
      * a non-empty ``name:`` field that matches ``dest_name``;
      * a non-empty ``description:`` field.

    Returns ``(modified_text, normalised_fields)`` where ``normalised_fields``
    lists the fields that were changed (e.g. ``["name", "description",
    "added_block"]``). Empty list ⇒ no change.
    """
    normalised: list[str] = []
    if not text.lstrip().startswith("---"):
        first_heading = next(
            (
                ln.strip(" #")
                for ln in text.splitlines()
                if ln.startswith("# ") or ln.startswith("## ")
            ),
            dest_name,
        )
        description = (first_heading or dest_name)[:300] or dest_name
        block = f"---\nname: {dest_name}\ndescription: {description}\n---\n\n"
        normalised.append("added_block")
        return block + text.lstrip(), normalised

    fm_match = re.match(r"^---\n(.*?)\n---\n?", text, flags=re.DOTALL)
    if not fm_match:
        return text, normalised
    fm_body = fm_match.group(1)
    rest = text[fm_match.end() :]

    name_match = re.search(r"^name:\s*(.*)$", fm_body, flags=re.MULTILINE)
    if name_match:
        current = name_match.group(1).strip().strip("\"'")
        if current != dest_name:
            fm_body = re.sub(
                r"^(name:\s*).+$", f"\\1{dest_name}", fm_body, count=1, flags=re.MULTILINE
            )
            normalised.append("name")
    else:
        fm_body = f"name: {dest_name}\n" + fm_body
        normalised.append("name")

    desc_match = re.search(r"^description:\s*(.*)$", fm_body, flags=re.MULTILINE)
    if not desc_match or not desc_match.group(1).strip().strip("\"'"):
        first_line = next(
            (ln.strip(" #") for ln in rest.splitlines() if ln.strip()),
            dest_name,
        )
        if desc_match:
            fm_body = re.sub(
                r"^(description:\s*).*$",
                f"\\1{first_line[:300]}",
                fm_body,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            fm_body += f"\ndescription: {first_line[:300]}"
        normalised.append("description")

    if not normalised:
        return text, normalised
    return f"---\n{fm_body}\n---\n{rest}", normalised


__all__ = [
    "REPO_ROOT",
    "PROJECT_ROOT",
    "SKILLS_ROOT",
    "TASKS_ROOT",
    "DEFAULT_PILOT_MANIFEST",
    "DEFAULT_SPLIT_MANIFEST",
    "PilotTask",
    "PilotManifest",
    "write_skill_md",
    "evolution_task_dir",
    "task_instruction",
    "getenv_or",
    "ordered_unique",
    "slug",
    "SKILL_EVOLUTION",
    "REPORTING",
    "BaselineMode",
    "compose_per_task_skills",
    "compose_test_time_skills",
    "resolve_baseline_mode",
    "minimal_frontmatter_compat",
]
