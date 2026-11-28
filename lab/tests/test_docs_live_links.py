"""Lab's live documents link to files that exist; frozen records keep their historical links (ADR-0062)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

LAB = Path(__file__).resolve().parents[1]
FROZEN = ("docs/adr/", "docs/pdr/", "docs/codex/", "docs/superpowers/", "docs/notes/", "tests/")
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def _live_documents() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=LAB, capture_output=True, check=False)
    if listed.returncode != 0:
        pytest.skip("Lab's documents are not in a git checkout")
    names = [name for name in listed.stdout.decode().split("\0") if name and not name.startswith(FROZEN)]
    return [LAB / name for name in names]


def test_live_documents_link_to_existing_files():
    broken = []
    for path in _live_documents():
        fenced = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for target in LINK.findall(line):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                if not (path.parent / unquote(target.split("#", 1)[0])).exists():
                    broken.append(f"{path.relative_to(LAB)}:{number}: {target}")
    assert broken == []
