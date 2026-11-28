"""The repository's entry pages lead through Lab (PDR-0054): every command they show exists."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from evolution_lab.cli.parser import build_parser

LAB = Path(__file__).resolve().parents[1]
REPOSITORY = LAB.parent
ENTRY_PAGES = sorted((*REPOSITORY.glob("README*.md"), *REPOSITORY.glob("QUICKSTART*.md")))
FENCED = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)

pytestmark = pytest.mark.skipif(
    not (REPOSITORY / "QUICKSTART.md").is_file(), reason="the repository's entry pages live next to lab/"
)


def _command_lines() -> list[tuple[str, str]]:
    lines = []
    for page in ENTRY_PAGES:
        for block in FENCED.findall(page.read_text(encoding="utf-8")):
            for line in block.splitlines():
                command = line.split("#", 1)[0].strip()
                if command:
                    lines.append((page.name, command))
    return lines


def _lab_subcommands() -> set[str]:
    parser = build_parser()
    actions = [action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"]
    return set(actions[0].choices)


def _just_recipes() -> set[str]:
    summary = subprocess.run(["just", "--summary"], cwd=LAB, check=True, capture_output=True, text=True)
    return set(summary.stdout.split())


def test_root_quickstart_commands_exist():
    lab_commands = []
    for page, line in _command_lines():
        words = line.removeprefix("just ").split()
        if len(words) > 1 and words[0] == "lab":
            lab_commands.append((page, words[1]))
    assert lab_commands, "the entry pages show no lab command"
    known = _lab_subcommands()
    assert [item for item in lab_commands if item[1] not in known] == []


def test_root_quickstart_just_recipes_exist():
    recipes = [(page, line.split()[1]) for page, line in _command_lines() if line.startswith("just ")
               and len(line.split()) > 1 and "=" not in line.split()[1]]
    assert recipes, "the entry pages show no just recipe"
    known = _just_recipes()
    assert [item for item in recipes if item[1] not in known] == []
