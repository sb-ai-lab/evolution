"""Sample editor: rewrite the first line of the Candidate's skills/SKILL.md.

Reads the `edit_request/1` document on stdin (its `brief` field is the
`edit_brief/5` or `/7` brief), replaces the first line of
`<workspace>/skills/SKILL.md` with `IMPROVED: reference skill` and prints
`{"status": "edited", "note": "first line improved"}`. On a Candidate whose
first line already is that line the bytes are unchanged and the campaign
records the attempt as `editor_no_change`. A sample, not a recommended
editor. The sample is written for `edit_brief/5` and `edit_brief/7` and refuses any other
version rather than editing blind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

IMPROVED_LINE = "IMPROVED: reference skill"


def main() -> None:
    request = json.load(sys.stdin)
    brief = request.get("brief") if isinstance(request, dict) else None
    version = brief.get("version") if isinstance(brief, dict) else None
    if version not in {"edit_brief/5", "edit_brief/7"}:
        sys.stderr.write(f"edit.py: expected edit_brief/5 or edit_brief/7, got {version!r}\n")
        raise SystemExit(2)
    skill = Path(request["workspace"]) / "skills" / "SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    lines[0:1] = [IMPROVED_LINE]
    skill.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "edited", "note": "first line improved"}))


if __name__ == "__main__":
    main()
