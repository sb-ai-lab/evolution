"""lib/diff.js against Lab's own diff dialect (spec 2026-09-30 §2.1): patches come from
owner/results.py::_unified, expected counts from difflib itself, never from hand-written text."""
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import evolution_lab.panel as panel_pkg
from evolution_lab.owner.results import FileChange, _lf_lines, _unified
from panel_fixtures import FIXTURES

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "panel_js" / "diff-golden.mjs"

CASES = [  # (path, before bytes | None, after bytes | None)
    ("skills/SKILL.md", b"one\ntwo\nthree\n", b"ONE\ntwo\nthree\nfour\n"),
    ("new.md", None, b"hello\nworld\n"),
    ("old.md", b"bye\n", None),
    ("empty.md", None, b""),
    ("tail.md", b"a\nb", b"a\nc"),
    ("header-like.md", b"-- looks like a header\nkeep\n", b"keep\n++ also looks like one\n"),
    ("blank.md", b"\n\nx\n", b"\nx\n\n"),
]


def _change(path, before, after):
    kind = "add" if before is None else "delete" if after is None else "modify"
    return FileChange(path, kind, None, None, None, None, None, before, after)


def _expected(path, before, after):
    left = [] if before is None else _lf_lines(before.decode())
    right = [] if after is None else _lf_lines(after.decode())
    lines = list(difflib.unified_diff(left, right, lineterm="\n"))[2:]
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    status = "add" if before is None else "delete" if after is None else "modify"
    return {"path": path, "status": status, "added": added, "removed": removed}


@pytest.mark.skipif(NODE is None, reason="node is not installed; it is an optional test runner (ADR-0018 p. 5)")
def test_diff_js_matches_lab_dialect():
    texts = [_unified(_change(p, b, a), "" if b is None else b.decode(), "" if a is None else a.decode())
             for p, b, a in CASES]
    texts.append("".join(texts))  # several files in one patch, as Lab serves them
    env = {**os.environ, "LAB_PANEL_STATIC": str(Path(panel_pkg.__file__).parent / "static"),
           "LAB_PANEL_FIXTURES": str(FIXTURES)}
    result = subprocess.run([NODE, str(SCRIPT)], input=json.dumps(texts), capture_output=True, text=True,
                            timeout=60, env=env, check=True)
    parsed = json.loads(result.stdout)
    expected = [[_expected(*case)] for case in CASES]
    expected.append([row for rows in expected for row in rows])
    assert parsed == expected
