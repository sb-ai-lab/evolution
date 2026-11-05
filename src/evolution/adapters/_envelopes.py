"""Harness-authored prompt blocks shared across framework adapters.

Framework-native prompts are never defined here — only the envelopes the
benchmark harness itself wraps around tasks and skill edits. Where a sentence
also appears on a byte-frozen runner surface (t34/5p2 consumers of
``native_framework_runners``), that phrasing is canonical: frozen surfaces
adopt the constant with zero byte change and only non-live surfaces are
harmonized to it.
"""

from __future__ import annotations

TASK_INSTRUCTION_BLOCK = "Task instruction:\n---\n{task_instruction}\n---\n"

TRACE_EVIDENCE_BLOCK = "Trace evidence from recent train solves:\n---\n{trace_digest}\n---\n"

WORKSPACE_CWD_RULE = (
    "Your CURRENT WORKING DIRECTORY is the task workspace — explore it with "
    "your tools to discover the input files."
)

# Byte-identical to loop.py's bundle runtime_contract write_rule.
WORKSPACE_ROOT_WRITE_RULE = "write requested deliverables under output/ from the workspace root"

WORKSPACE_LISTING_HEADER = "Workspace files (relative to the workspace root):"

NO_PACKAGE_INSTALL_RULE = (
    "The benchmark Python environment already contains the required task "
    "packages; do not create virtual environments and do not run pip, uv, "
    "ensurepip, apt, or package installation commands."
)

FILE_OUTPUT_EXAMPLE_FLAT = "If the task asks for `foo.pdf`, create `output/foo.pdf`."
FILE_OUTPUT_EXAMPLE_NESTED = (
    "If the task asks for `redacted/paper1.pdf`, create `output/redacted/paper1.pdf`."
)

ANCHOR_EDIT_RULE = (
    "You are improving reusable skill ``{skill_id}`` in place for similar "
    "future tasks. Update ONLY ``{skill_id}`` — do not create new skills and "
    "do not write to any other skill name. {edit_mechanism}"
)

GENERIC_PROCEDURE_RULE = (
    "Capture only generalizable procedure — never task-specific answers or values."
)

# For tool-call adapters (memento, hermes) with a reflection / skill-creator step.
PROC_MEMORY_SKILL_CREATOR = (
    "After solving, if the task required domain-specific techniques — "
    "file-format APIs, schema patterns, library-specific workflows, or "
    "non-obvious edge cases — call the built-in ``skill-creator`` during "
    "your reflection step to capture a reusable procedure. "
    "Skip the call for tasks that used only general Python idioms."
)

# For action-space adapters (memp) whose done-answer feeds trajectory reflect.
PROC_MEMORY_DONE_SUMMARY = (
    "When you call ``done``, include in the ``<answer>`` a brief summary of "
    "the domain-specific libraries, APIs, and patterns you applied — "
    "this builds procedural memory for future tasks."
)

__all__ = [
    "ANCHOR_EDIT_RULE",
    "FILE_OUTPUT_EXAMPLE_FLAT",
    "FILE_OUTPUT_EXAMPLE_NESTED",
    "GENERIC_PROCEDURE_RULE",
    "NO_PACKAGE_INSTALL_RULE",
    "PROC_MEMORY_DONE_SUMMARY",
    "PROC_MEMORY_SKILL_CREATOR",
    "TASK_INSTRUCTION_BLOCK",
    "TRACE_EVIDENCE_BLOCK",
    "WORKSPACE_CWD_RULE",
    "WORKSPACE_LISTING_HEADER",
    "WORKSPACE_ROOT_WRITE_RULE",
]
