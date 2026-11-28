"""One file-set rule for Bundle copies and project inventories (ADR-0017 p. 6)."""
from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

# ``.venv``: a test environment left in a place (symlinks to the system python) would refuse the Bundle of a
# probe target; the target's own environment is declared outside the place. ``venv/`` is not excluded: it may be code.
EXCLUDED_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"})
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def excluded_name(name: str, *, directory: bool) -> bool:
    """True when a directory entry never enters a Bundle or an inventory."""
    if name in EXCLUDED_NAMES:
        return True
    return not directory and name.endswith(EXCLUDED_SUFFIXES)


def excluded_source(path: Path, *, directory: bool) -> bool:
    """True when a top-level source (Bundle root, inventory source) must not enter at all.

    The walks filter only children, so the boundary is checked here, one rule for both:
    any ancestor of the absolute path is an excluded name (``<repo>/.git/hooks``), or the
    leaf itself is excluded by :func:`excluded_name` with its directory flag — a directory
    named ``d.pyc`` stays, as it does as a child.
    """
    if any(part in EXCLUDED_NAMES for part in Path(path).absolute().parts[:-1]):
        return True
    return excluded_name(Path(path).name, directory=directory)


def excluded_path(relative: str | PurePosixPath) -> bool:
    """True when a POSIX path lies inside, or is, an excluded entry."""
    parts = PurePosixPath(relative).parts
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    return bool(parts) and parts[-1].endswith(EXCLUDED_SUFFIXES)


def entry_fields(relative: str, info: os.stat_result, *, sha256: str | None) -> dict:
    """The inventory record shared by Bundle entries and project inventories."""
    directory = stat.S_ISDIR(info.st_mode)
    return {
        "path": relative,
        "type": "directory" if directory else "file",
        "executable": stat.S_IMODE(info.st_mode) & 0o111,
        "size": None if directory else info.st_size,
        "sha256": None if directory else sha256,
    }
