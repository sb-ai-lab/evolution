"""Codex agent framework editor for the iterative evolution loop (hermes-role).

A standalone framework row: the ``codex`` CLI agent edits the anchor SKILL.md
from trace evidence via the shared file-edit adapter. Distinct from using codex
as an editor *backend* inside another framework.
"""

from __future__ import annotations

import os
from typing import Any

from evolution.adapters.agent_cli_adapter import edit_skill_from_traces as _agent_edit

DEFAULT_MODEL = "codex/gpt-5.5"


def edit_skill_from_traces(**kwargs: Any) -> tuple[str, dict[str, Any]]:
    agent_model = os.environ.get("EVO_CODEX_MODEL") or DEFAULT_MODEL
    return _agent_edit(framework="codex", agent_model=agent_model, **kwargs)


__all__ = ["DEFAULT_MODEL", "edit_skill_from_traces"]
