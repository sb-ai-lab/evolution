"""Canonical operator-root ancestors, preserving the final nofollow boundary."""
from __future__ import annotations

import os
from pathlib import Path


def canonical_root(value: Path | str) -> Path:
    path = Path(value)
    if '\0' in str(path):
        raise ValueError('invalid operator root')
    # Preserve parent traversal until realpath follows the existing ancestors.
    # abspath would collapse link/.. before following link, changing its meaning.
    absolute = path.absolute()
    if absolute.name == '..':
        canonical = Path(os.path.realpath(absolute))
    else:
        canonical = Path(os.path.realpath(absolute.parent)) / absolute.name
    if canonical == Path(canonical.anchor):
        raise ValueError('operator root must be a dedicated directory')
    return canonical
