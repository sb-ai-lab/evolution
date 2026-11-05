"""Local workspace defaults for paper experiment dependencies.

These are intentionally concrete for the development environment used to run
the paper experiments. Env vars still override each path so external clones can
reuse the code without editing scripts.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_ROOT = Path(
    os.environ.get("SKILL_EVOLUTIONS_ROOT", str(Path.home() / "skill_evolutions"))
).expanduser()

DEFAULT_AFTER_DATA_ROOT = WORKSPACE_ROOT / "datasets" / "sb-bench"
DEFAULT_HERMES_REPO = WORKSPACE_ROOT / "hermes-agent"
DEFAULT_MEMENTO_REPO = WORKSPACE_ROOT / "Memento-Skills"

DEFAULT_CRAFT_DATA_ROOT = DEFAULT_AFTER_DATA_ROOT  # backward compat alias


def env_path(name: str, default: Path) -> Path:
    """Return an env-overridable path resolved against the local filesystem."""
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser().resolve()
    return default.expanduser().resolve()


def after_data_root(env_name: str = "AFTER_DATA_ROOT") -> Path:
    return env_path(env_name, DEFAULT_AFTER_DATA_ROOT)


craft_data_root = after_data_root  # backward compat alias


def hermes_repo() -> Path:
    return env_path("HERMES_REPO", DEFAULT_HERMES_REPO)


def memento_repo() -> Path:
    return env_path("MEMENTO_REPO", DEFAULT_MEMENTO_REPO)
