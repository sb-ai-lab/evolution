"""Deterministic no-network scorer used by the Evolution v2 offline fixture."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if any(name.endswith("API_KEY") or name.endswith("CREDENTIALS") for name in os.environ):
        return 9
    payload = json.load(sys.stdin)
    candidate_root = Path(os.environ["EVO_CANDIDATE_ROOT"])
    if (candidate_root / "skill.py").read_text(encoding="utf-8") != "VALUE = 1\n":
        return 8
    turns = len(payload["view"]["history"]["turns"])
    output = {
        "metrics": {"quality": 1.0},
        "critical_pass": True if payload["oracle"].get("critical") is True else None,
        "verdicts": [],
        "public_feedback": {"summary": "offline fixture passed"},
        "details": {"turns": turns},
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
