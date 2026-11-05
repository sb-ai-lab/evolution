"""Task -> required-skills discovery for the evolution adapter."""

from __future__ import annotations

import logging

import tomllib

from .common import TASKS_ROOT

logger = logging.getLogger(__name__)


def required_skills(task_id: str) -> list[str]:
    """Skills declared in the task's ``task.toml`` (may be empty)."""
    tpath = TASKS_ROOT / task_id / "task.toml"
    if not tpath.exists():
        return []
    try:
        data = tomllib.loads(tpath.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - toml parse error
        logger.warning("tomllib failed on %s: %s", tpath, exc)
        return []
    # Evolution stores the list at [skills].required.
    skills = data.get("skills", {})
    if isinstance(skills, dict):
        required = skills.get("required")
        if isinstance(required, list):
            return [str(s) for s in required]
    # Fallbacks for tasks not produced by Evolution's importer.
    for container_key in ("metadata", "task"):
        c = data.get(container_key)
        if isinstance(c, dict) and isinstance(c.get("skills"), list):
            return [str(s) for s in c["skills"]]
    if isinstance(data.get("skills"), list):
        return [str(s) for s in data["skills"]]
    return []


__all__ = ["required_skills"]
