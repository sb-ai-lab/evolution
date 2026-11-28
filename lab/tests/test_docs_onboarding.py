"""The onboarding path in the user guide: project skeleton, `lab new-project`, `lab evaluate` (PDR-0035)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"
pytestmark = pytest.mark.skipif(not GUIDE.is_dir(), reason="Lab's guides live in the Core repository's docs/guide")
SKELETON_README = ROOT / "src" / "evolution_lab" / "quickstart" / "skeleton" / "README.md"


def _read(name: str) -> str:
    return (GUIDE / name).read_text(encoding="utf-8")


def test_connect_agent_opens_with_the_three_commands():
    text = _read("connect-agent.md")
    head = text.split("\n### ", 1)[0]
    assert "lab new-project" in head and "lab evaluate" in head
    opening = text.split("\n## Before you start", 1)[0]
    assert "agent/my_agent.py" in opening and "agent/adapter.py" in opening


def test_first_evaluation_has_no_script_surgery():
    text = _read("first-evaluation.md")
    assert "Replace everything from" not in text
    assert "## Swap in your own agent" not in text
    assert "lab new-project" in text and "lab evaluate" in text
    assert "## What `lab evaluate` does inside" in text
    exit_codes = text.split("### Exit codes", 1)[1].split("\n### ", 1)[0]
    for code in ("`0`", "`1`", "`2`", "`3`"):
        assert f"| {code} |" in exit_codes, code


def test_demo_names_the_agent_and_the_adapter():
    text = _read("demo.md")
    assert "adapter.py" in text and "my_agent.py" in text
    assert "example_agent.py   the agent" not in text


def test_quick_start_and_entry_pages_point_at_the_new_first_step():
    assert "just new-project" in (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("cli.md", "projects.md", "examples.md"):
        assert "first-evaluation.md#measure-your-own-agent" in _read(name), name
    install = _read("install.md")
    assert "Exit 2" in install and "Exit 3" in install


def test_guide_anchors_used_by_the_onboarding_pages_exist():
    def anchors(text: str) -> set[str]:
        found = set()
        for line in text.splitlines():
            match = re.match(r"#{1,6} (.+)", line)
            if match:
                slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
                found.add(slug)
        return found | set(re.findall(r'<a (?:id|name)="([^"]+)"', text))

    link = re.compile(r"\]\(([a-z-]+\.md)?#([^)\s]+)\)")
    for path in sorted(GUIDE.glob("*.md")):
        for target, anchor in link.findall(path.read_text(encoding="utf-8")):
            page = GUIDE / target if target else path
            if not page.exists():
                continue
            assert anchor in anchors(page.read_text(encoding="utf-8")), f"{path.name} -> {target}#{anchor}"


def test_skeleton_is_never_called_a_template_and_its_readme_has_no_dead_link():
    for path in (ROOT / "README.md", *GUIDE.glob("*.md")):
        assert "project template" not in path.read_text(encoding="utf-8").lower(), path.name
    readme = SKELETON_README.read_text(encoding="utf-8")
    assert "](../" not in readme
    assert "connect-agent.md" in readme and "lab guides path" in readme


def test_first_steps_tell_the_short_path_and_the_corpus_fields():
    for text in (_read("connect-agent.md"), SKELETON_README.read_text(encoding="utf-8")):
        assert "answer(question) -> str" in text and "no change" in text
        assert "`lab` is not on your PATH" in text and "just new-project DIR" in text
        for field in ("`case_id`", "`group_id`", "`stratum`", "`public.text`", "`oracle.expected`", "`oracle.critical`"):
            assert field in text, field
    assert "evaluation-inputs.md" in _read("connect-agent.md") and "evaluation-inputs.md" in SKELETON_README.read_text()
    adapter = (SKELETON_README.parent / "agent" / "adapter.py").read_text(encoding="utf-8")
    assert "needs no change here" in adapter
