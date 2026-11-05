"""Skill body compaction before Table A body-injection."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

_SKILL_BODY_CHAR_LIMIT = int(os.environ.get("SKILL_BODY_CHAR_LIMIT", "262144"))


def skill_body_char_limit() -> int:
    return _SKILL_BODY_CHAR_LIMIT


def body_needs_compaction(body: str) -> bool:
    return len(body) > _SKILL_BODY_CHAR_LIMIT


async def compact_skill_body_async(
    *,
    body: str,
    skill_id: str,
    backend: Any,
    reflection_summary: str = "",
) -> str:
    from evolution.core.models import Skill, SkillFrontmatter
    from evolution.evolution.reflect import (
        _compact_oversized_rewrite,
        rewrite_size_limits,
    )

    fm = SkillFrontmatter.model_validate({"name": skill_id, "description": "iterative"})
    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(body, encoding="utf-8")
        skill = Skill(frontmatter=fm, body=body, path=skill_dir, skill_md_path=skill_md)
        limits = rewrite_size_limits(skill, body)
        reflections = [
            {
                "pattern": "other",
                "skill_gap": reflection_summary or "oversized skill body",
                "suggested_fix": "compact while preserving procedure",
            }
        ]
        return await _compact_oversized_rewrite(backend, skill, body, reflections, limits)


def maybe_compact_skill_body(
    *,
    body: str,
    skill_id: str,
    solver_cfg: dict[str, Any],
    api_key: str,
    reflection_summary: str = "",
) -> tuple[str, bool]:
    """Return (body_for_solve, was_compacted)."""
    if not body_needs_compaction(body):
        return body, False
    from evolution.llm.litellm_backend import LiteLLMBackend

    model = (
        solver_cfg.get("litellm_id") or solver_cfg.get("id") or solver_cfg.get("served_id_hint", "")
    )
    kwargs: dict[str, Any] = {
        "api_base": solver_cfg.get("api_base", ""),
        "api_key": api_key or "EMPTY",
    }
    if isinstance(solver_cfg.get("extra_body"), dict):
        kwargs["extra_body"] = dict(solver_cfg["extra_body"])
    if solver_cfg.get("max_output_tokens"):
        kwargs["max_tokens"] = int(solver_cfg["max_output_tokens"])
    if solver_cfg.get("temperature") is not None:
        kwargs["temperature"] = float(solver_cfg["temperature"])
    backend = LiteLLMBackend(model=model, **kwargs)
    try:
        compacted = asyncio.run(
            compact_skill_body_async(
                body=body,
                skill_id=skill_id,
                backend=backend,
                reflection_summary=reflection_summary,
            )
        )
    except Exception:
        return body, False
    return compacted, True


__all__ = [
    "body_needs_compaction",
    "maybe_compact_skill_body",
    "skill_body_char_limit",
]
