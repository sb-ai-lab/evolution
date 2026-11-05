"""Resolve the Python interpreter used to run a task's solution + tests.

sb-bench tasks declare a role (de / ds / genai / infra / pm / swe). Each role
has its own conda env holding the dependency surface that role's tasks need
(duckdb, pandera, openpyxl, pdf/docx/pptx, …). The legacy harness
(``test_bench_scripts/qwen35_9B_run/bench.py``) selected the role's env python
and patched solve.sh / pytest to use it. Running every solution under the evo
CLI's own interpreter (``sys.executable``) instead makes lib-heavy roles fail
with ``ModuleNotFoundError`` regardless of skill quality — a silent confounder
in evolution results. This module restores the role→env mapping.

Resolution order for a role:

1. ``EVO_ROLE_PYTHON_<ROLE>`` env var (explicit per-role override).
2. :data:`DEFAULT_ROLE_PYTHON_MAP` (mirrors bench.py).

Falls back to ``sys.executable`` when the role is unknown/None or the mapped
interpreter does not exist on this machine — but logs a warning in the latter
case, because a silent fallback would reintroduce the very confounder this
module exists to remove.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors test_bench_scripts/qwen35_9B_run/bench.py:ROLE_PYTHON_MAP. Override a
# single role without editing this file via EVO_ROLE_PYTHON_<ROLE> (e.g.
# EVO_ROLE_PYTHON_DE=/path/to/python).
_mlspace = Path.home() / ".mlspace" / "envs"
DEFAULT_ROLE_PYTHON_MAP: dict[str, str] = {
    "de": str(_mlspace / "de-env/bin/python"),
    "ds": str(_mlspace / "ds-env/bin/python"),
    "genai": str(_mlspace / "genai-env/bin/python"),
    "infra": str(_mlspace / "infra-env/bin/python"),
    "pm": str(_mlspace / "pm-env/bin/python"),
    "swe": str(_mlspace / "swe-env/bin/python"),
}

# Roles whose missing interpreter we have already warned about, so a long run
# does not emit one warning per solve attempt.
_warned_roles: set[str] = set()


def resolve_python_for_role(role: str | None) -> str:
    """Return the interpreter path for ``role``, or ``sys.executable``.

    See the module docstring for the resolution order and fallback rationale.
    """
    if not role:
        return sys.executable

    role_key = role.strip().lower()
    override = os.environ.get(f"EVO_ROLE_PYTHON_{role_key.upper()}")
    candidate = override or DEFAULT_ROLE_PYTHON_MAP.get(role_key)

    if candidate and os.path.exists(candidate):
        return candidate

    if candidate and role_key not in _warned_roles:
        _warned_roles.add(role_key)
        logger.warning(
            "Role %r python %r not found; falling back to %s. Tasks needing "
            "role-specific libraries may fail with ModuleNotFoundError. Set "
            "EVO_ROLE_PYTHON_%s to override.",
            role_key,
            candidate,
            sys.executable,
            role_key.upper(),
        )
    return sys.executable
