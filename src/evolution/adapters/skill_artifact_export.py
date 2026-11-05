"""Serialize a framework's evolved native skill state into a body-distinct SKILL.md.

EvoSkill and Memento evolve by producing new *sibling* skill directories beside the
anchor (EvoSkill's program search; Memento's agent skill-creator) rather than rewriting
the anchor SKILL.md. The iterative-evolution loop versions a skill only when the exported
anchor body changes, so those evolved siblings were invisible and discarded. These helpers
fold the real on-disk sibling skills into the anchor body.

Integrity: the composed body is built only from files the framework actually wrote to
disk. With nothing new on disk the anchor text is returned unchanged (idempotent), so a
non-evolving round stays an honest no-op -- no skill content is fabricated.
"""

from __future__ import annotations

import re
from pathlib import Path

# Opening ``---`` must be at the very start; mirrors evolution.core.parser._FRONTMATTER_RE.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)
_DEFAULT_EXCLUDE = ("skill-creator",)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_block_with_delimiters, body)``.

    The frontmatter block is preserved verbatim (no YAML round-trip) so anchor metadata
    is not reordered or re-validated. Returns ``("", text)`` when there is no frontmatter.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end() :]


def list_evolved_skill_dirs(
    skills_dir: Path | str,
    anchor_id: str,
    *,
    since_ts: float | None = None,
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE,
) -> list[Path]:
    """Sibling skill dirs (each containing ``SKILL.md``) produced beside the anchor.

    Excludes the anchor itself and ``exclude`` names (e.g. the built-in skill-creator).
    When ``since_ts`` is given, keep only dirs whose ``SKILL.md`` mtime is newer -- used
    for Memento's persistent skills dir to ignore earlier rounds. EvoSkill's
    ``produced_state`` is round-scoped, so pass ``since_ts=None``. Name-sorted for
    deterministic folding.
    """
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    excluded = set(exclude) | {anchor_id}
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in excluded:
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        if since_ts is not None:
            try:
                if skill_md.stat().st_mtime <= since_ts:
                    continue
            except OSError:
                continue
        out.append(child)
    return out


def collect_session_skill_dirs(
    session_ws: Path | str | None,
    *,
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE,
) -> list[Path]:
    """Skill dirs (containing ``SKILL.md``) anywhere under an agent session workspace.

    Memento's agent writes skill-creator output into its session workspace rather than the
    anchor skills dir; harvest those genuine outputs too. Name-sorted, deduped by dir name.
    """
    if not session_ws:
        return []
    root = Path(session_ws)
    if not root.is_dir():
        return []
    excluded = set(exclude)
    seen: dict[str, Path] = {}
    for skill_md in sorted(root.rglob("SKILL.md")):
        parent = skill_md.parent
        if parent.name in excluded:
            continue
        seen.setdefault(parent.name, parent)
    return [seen[name] for name in sorted(seen)]


def fold_evolved_skills(anchor_full_text: str, evolved_dirs: list[Path]) -> str:
    """Fold sibling skills into the anchor SKILL.md as additive ``##`` sections.

    Anchor frontmatter and body are preserved; each sibling becomes a
    ``## Evolved sub-skill: <name>`` section carrying that skill's body (its own
    frontmatter stripped). Empty siblings are skipped. With no usable siblings the anchor
    text is returned unchanged (idempotent -> downstream no-op, not a fabrication).
    """
    sections: list[str] = []
    seen: set[str] = set()
    for d in evolved_dirs:
        name = d.name
        if name in seen:
            continue
        skill_md = Path(d) / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, body = split_frontmatter(text)
        body = body.strip()
        if not body:
            continue
        seen.add(name)
        sections.append(f"## Evolved sub-skill: {name}\n\n{body}")
    if not sections:
        return anchor_full_text
    fm_block, anchor_body = split_frontmatter(anchor_full_text)
    appendix = "\n\n".join(sections)
    anchor_body = anchor_body.strip()
    composed_body = f"{anchor_body}\n\n{appendix}" if anchor_body else appendix
    if fm_block:
        return f"{fm_block}\n{composed_body}\n"
    return f"{composed_body}\n"


__all__ = [
    "collect_session_skill_dirs",
    "fold_evolved_skills",
    "list_evolved_skill_dirs",
    "split_frontmatter",
]
