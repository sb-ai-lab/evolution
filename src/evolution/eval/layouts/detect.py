"""Layout detection: pick the right ``TaskLayout`` for a given task dir.

Order of decision:
    1. If ``task.toml`` has ``[layout].kind = "evo"`` (or ``"sb-bench"``),
       honor that.
    2. Otherwise, look at the filesystem: presence of ``solve.sh`` AND
       ``environment/`` ⇒ sb-bench layout. Anything else ⇒ evo layout.

Returns an *instance* (not a class) so callers can stash it on ``Task``
without repeated construction.
"""

from __future__ import annotations

import re
from pathlib import Path

from evolution.eval.layouts.base import TaskLayout
from evolution.eval.layouts.evo import EvoTaskLayout
from evolution.eval.layouts.sb_bench import SbBenchTaskLayout

_LAYOUT_KIND_RE = re.compile(r"^\s*kind\s*=\s*\"([A-Za-z0-9_\-]+)\"", re.MULTILINE)
_LAYOUT_SECTION_RE = re.compile(r"^\[layout\][^\[]*", re.MULTILINE | re.DOTALL)

# Singletons — layouts are stateless.
_EVO = EvoTaskLayout()
_SB_BENCH = SbBenchTaskLayout()

# Public mapping in case callers want to look it up by name.
LAYOUTS: dict[str, TaskLayout] = {
    "evo": _EVO,
    "sb-bench": _SB_BENCH,
    "sb_bench": _SB_BENCH,
}


def detect_layout(task_dir: Path) -> TaskLayout:
    """Return the appropriate layout for ``task_dir``.

    See module docstring for the decision order. Falls back to evo on
    any ambiguity (current default behavior).
    """
    toml_path = task_dir / "task.toml"
    if toml_path.is_file():
        try:
            text = toml_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        section_match = _LAYOUT_SECTION_RE.search(text)
        if section_match:
            kind_match = _LAYOUT_KIND_RE.search(section_match.group(0))
            if kind_match:
                kind = kind_match.group(1).lower()
                if kind in LAYOUTS:
                    return LAYOUTS[kind]

    if (task_dir / "solve.sh").is_file():
        return _SB_BENCH
    return _EVO
