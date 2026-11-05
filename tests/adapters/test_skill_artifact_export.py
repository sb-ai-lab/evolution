"""Unit tests for the evoskill/memento native-artifact exporter."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from evolution.adapters.skill_artifact_export import (
    collect_session_skill_dirs,
    fold_evolved_skills,
    list_evolved_skill_dirs,
    split_frontmatter,
)
from evolution.core.parser import parse_skill_md

ANCHOR = (
    "---\n"
    "name: sql\n"
    'description: "Anchor skill"\n'
    "---\n\n"
    "# SQL\n\n"
    "## Rule\nUse parameterised queries.\n"
)


def _write_skill(root: Path, name: str, body: str, *, frontmatter: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f'---\nname: {name}\ndescription: "sib"\n---\n\n' if frontmatter else ""
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return d


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_fold_with_sibling_is_distinct_and_valid(tmp_path: Path) -> None:
    sib = _write_skill(tmp_path, "sql-helper", "## Helper\nLook up the scorer artifact.\n")
    out = fold_evolved_skills(ANCHOR, [sib])

    assert _sha(out) != _sha(ANCHOR)
    assert "## Evolved sub-skill: sql-helper" in out
    assert "Look up the scorer artifact." in out
    # anchor content survives so finalize won't restore-and-revert
    assert "Use parameterised queries." in out
    # still a parseable SKILL.md with the anchor frontmatter intact
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "SKILL.md").write_text(out, encoding="utf-8")
    fm, body = parse_skill_md(tmp_path / "out" / "SKILL.md")
    assert fm["name"] == "sql"
    assert body.strip()


def test_fold_idempotent_no_siblings() -> None:
    assert fold_evolved_skills(ANCHOR, []) == ANCHOR


def test_fold_skips_empty_sibling_body(tmp_path: Path) -> None:
    sib = _write_skill(tmp_path, "empty", "   \n")
    assert fold_evolved_skills(ANCHOR, [sib]) == ANCHOR


def test_fold_strips_sibling_frontmatter(tmp_path: Path) -> None:
    sib = _write_skill(tmp_path, "sql-helper", "## Helper\nbody text\n")
    _, anchor_body_only = split_frontmatter(ANCHOR)
    out = fold_evolved_skills(ANCHOR, [sib])
    _fm, out_body = split_frontmatter(out)
    # the sibling's own frontmatter must not be inlined into the anchor body
    assert "name: sql-helper" not in out_body
    assert 'description: "sib"' not in out_body
    # exactly one (anchor) frontmatter block at the top
    assert out.count("---\nname:") == 1


def test_list_evolved_skill_dirs_excludes_anchor_and_skill_creator(tmp_path: Path) -> None:
    _write_skill(tmp_path, "sql", "anchor\n")
    _write_skill(tmp_path, "skill-creator", "builtin\n")
    _write_skill(tmp_path, "sib-b", "b\n")
    _write_skill(tmp_path, "sib-a", "a\n")
    (tmp_path / "not-a-skill").mkdir()  # no SKILL.md -> ignored

    dirs = list_evolved_skill_dirs(tmp_path, "sql")
    assert [d.name for d in dirs] == ["sib-a", "sib-b"]


def test_list_evolved_skill_dirs_since_ts(tmp_path: Path) -> None:
    old = _write_skill(tmp_path, "old-sib", "old\n")
    os.utime(old / "SKILL.md", (1000.0, 1000.0))
    new = _write_skill(tmp_path, "new-sib", "new\n")
    os.utime(new / "SKILL.md", (5000.0, 5000.0))

    dirs = list_evolved_skill_dirs(tmp_path, "sql", since_ts=2000.0)
    assert [d.name for d in dirs] == ["new-sib"]


def test_collect_session_skill_dirs_recurses_and_dedups(tmp_path: Path) -> None:
    nested = tmp_path / "2026-06-15" / "abcd1234"
    _write_skill(nested, "agent-skill", "## A\nfrom session\n")
    _write_skill(nested, "skill-creator", "builtin\n")

    dirs = collect_session_skill_dirs(tmp_path)
    assert [d.name for d in dirs] == ["agent-skill"]


def test_collect_session_skill_dirs_handles_missing() -> None:
    assert collect_session_skill_dirs(None) == []
    assert collect_session_skill_dirs("/nonexistent/path/xyz") == []
