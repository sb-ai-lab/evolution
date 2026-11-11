"""b18 (#399, ADR-0061): the `/16` chat Editor default prompt `tree_editor_v3.md`.

V3 is V2 plus one paragraph that tells the Editor to reconcile a new rule with the
skill it edits. The anchors pinned here are the structural contract only; the
paragraph's coverage of spec section 6.2 requirements 1-8 is a review check.
"""

from __future__ import annotations

import re

from evolution import prompts

#: Requirement 8's anchor: the Editor never claims that code ran.
NO_EXECUTION_CLAIM = "never claim that code was executed or tested"


def _v3_section() -> str:
    prefix = prompts.TREE_EDITOR_SYSTEM_V2 + "\n\n"
    assert prompts.TREE_EDITOR_SYSTEM_V3.startswith(prefix)
    return prompts.TREE_EDITOR_SYSTEM_V3.removeprefix(prefix)


def test_editor_v3_is_v2_plus_one_section():
    section = _v3_section()
    assert section.strip() == section
    assert section
    assert "\n" not in section


def test_editor_v3_file_bytes_are_v2_bytes_plus_one_paragraph():
    v2 = (prompts._PROMPTS_DIR / "tree_editor_v2.md").read_bytes()
    v3 = (prompts._PROMPTS_DIR / "tree_editor_v3.md").read_bytes()
    prefix = v2.removesuffix(b"\n") + b"\n\n"
    assert v3.startswith(prefix)
    paragraph = v3.removeprefix(prefix)
    assert paragraph.endswith(b"\n")
    assert b"\n" not in paragraph.removesuffix(b"\n")


def test_editor_v3_section_names_the_contract():
    section = _v3_section()
    assert "record_plan" in section
    assert "finish_edit" in section
    # Requirement 4's explicit transition, within one sentence.
    assert re.search(r"\bif\b[^.]*\botherwise\b", section)
    assert NO_EXECUTION_CLAIM in section


def _agent_v2_section() -> str:
    prefix = prompts.TREE_AGENT_EDITOR_SYSTEM + "\n\n"
    assert prompts.TREE_AGENT_EDITOR_SYSTEM_V2.startswith(prefix)
    return prompts.TREE_AGENT_EDITOR_SYSTEM_V2.removeprefix(prefix)


def test_agent_editor_v2_is_v1_plus_one_section():
    section = _agent_v2_section()
    assert section.strip() == section
    assert section
    assert "\n" not in section


def test_agent_editor_v2_file_bytes_are_v1_bytes_plus_one_paragraph():
    v1 = (prompts._PROMPTS_DIR / "tree_agent_editor.md").read_bytes()
    v2 = (prompts._PROMPTS_DIR / "tree_agent_editor_v2.md").read_bytes()
    prefix = v1.removesuffix(b"\n") + b"\n\n"
    assert v2.startswith(prefix)
    paragraph = v2.removeprefix(prefix)
    assert paragraph.endswith(b"\n")
    assert b"\n" not in paragraph.removesuffix(b"\n")


def test_agent_editor_v2_section_names_the_contract():
    section = _agent_v2_section()
    assert "declared editable paths" in section
    assert "final summary" in section
    assert "record_plan" not in section
    assert "finish_edit" not in section
    assert re.search(r"\bif\b[^.]*\botherwise\b", section)
    assert NO_EXECUTION_CLAIM in section
