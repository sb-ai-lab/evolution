"""Canonical home for every prompt the framework sends to an LLM.

One import surface so a newcomer (or coding agent) can discover and read all
task-solving, reflection, and rewrite prompts in one place::

    from evolution import prompts
    print(prompts.BASE_SOLVER_SYSTEM_PROMPT)
    print(prompts.REFLECT_SYSTEM_ORACLE)

Each constant is defined in the module that *uses* it (so the prompt sits next
to the code that renders and sends it) and re-exported here. The authoritative
definitions live in:

* task solving      — :mod:`evolution.eval.solver`
* reflection/diagnose — :mod:`evolution.evolve.reflection`,
  :mod:`evolution.evolve.reflection_evidence`
* rewrite / edit    — :mod:`evolution.evolve.reflect`

Prompt text is covered by golden tests (``tests/test_prompts_golden.py``): any
edit to a prompt must update its golden hash, which keeps changes deliberate and
reviewable (prompt wording is load-bearing for measured fitness).
"""

from __future__ import annotations

import re
from pathlib import Path

from evolution.eval.solver import (
    BASE_SOLVER_SYSTEM_PROMPT,
    CODE_RETRY_SYSTEM_REMINDER,
    OUTPUT_CWD_CONTRACT,
    ROOT_CWD_CONTRACT,
)
from evolution.evolve.reflect import (
    CODEX_FILE_EDIT_PROMPT,
    CODEX_FILE_EDIT_SYSTEM,
    REWRITE_PROMPT,
    REWRITE_SYSTEM,
    SESSION_PROMPT,
    SESSION_SYSTEM_BLIND,
    SESSION_SYSTEM_ORACLE,
)
from evolution.evolve.reflection import (
    REFLECT_PROMPT,
    REFLECT_SUCCESS_PROMPT,
    REFLECT_SYSTEM_BLIND,
    REFLECT_SYSTEM_ORACLE,
)
from evolution.evolve.reflection_evidence import ORACLE_LEAD_IN

__all__ = [
    # task solving
    "BASE_SOLVER_SYSTEM_PROMPT",
    "ROOT_CWD_CONTRACT",
    "OUTPUT_CWD_CONTRACT",
    "CODE_RETRY_SYSTEM_REMINDER",
    # reflection / diagnose
    "REFLECT_SYSTEM_BLIND",
    "REFLECT_SYSTEM_ORACLE",
    "REFLECT_PROMPT",
    "REFLECT_SUCCESS_PROMPT",
    "ORACLE_LEAD_IN",
    # rewrite / edit
    "REWRITE_SYSTEM",
    "REWRITE_PROMPT",
    "CODEX_FILE_EDIT_SYSTEM",
    "CODEX_FILE_EDIT_PROMPT",
    "SESSION_SYSTEM_BLIND",
    "SESSION_SYSTEM_ORACLE",
    "SESSION_PROMPT",
    "TREE_AGENT_EDITOR_SYSTEM",
    "TREE_AGENT_EDITOR_SYSTEM_V2",
    "TREE_EDITOR_SYSTEM",
    "TREE_EDITOR_SYSTEM_V2",
    "TREE_EDITOR_SYSTEM_V3",
    "TREE_AGENT_REFLECTOR_SYSTEM",
    "TREE_REFLECTOR_SYSTEM",
    "TREE_AGENT_REFLECTOR_SYSTEM_V3",
    "TREE_AGENT_REFLECTOR_SYSTEM_V4",
    "TREE_REFLECTOR_SYSTEM_V3",
    "TREE_REFLECTOR_SYSTEM_V4",
    "TREE_REFLECTOR_SYSTEM_V5",
    "TREE_REFLECTOR_SYSTEM_V6",
    "TREE_GROUPING_MODEL_SYSTEM",
    "TREE_GROUPING_MODEL_V2_SYSTEM",
    "TREE_GROUPING_MODEL_V3_SYSTEM",
    "TREE_AGENT_GROUPER_SYSTEM",
]

_PROMPTS_DIR = Path(__file__).resolve().parent
_INCLUDE_RE = re.compile(r"\{\{include:([\w./-]+)\}\}")
_INCLUDE_NAME_RE = re.compile(r"^_blocks/[A-Za-z0-9_-]+$")
_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")
_prompt_cache: dict[str, str] = {}


def _read_prompt_file(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path.read_text(encoding="utf-8")


def _validate_include_name(name: str) -> None:
    if not _INCLUDE_NAME_RE.fullmatch(name):
        raise ValueError(f"prompt includes must reference _blocks/<name>, got {name!r}")
    root = (_PROMPTS_DIR / "_blocks").resolve()
    path = (_PROMPTS_DIR / f"{name}.md").resolve()
    if root not in path.parents:
        raise ValueError(f"prompt include escapes _blocks/: {name!r}")


def load_prompt(name: str) -> str:
    if name in _prompt_cache:
        return _prompt_cache[name]
    text = _read_prompt_file(name)

    def _inline(match: re.Match[str]) -> str:
        block_name = match.group(1)
        _validate_include_name(block_name)
        block_text = _read_prompt_file(block_name)
        if _INCLUDE_RE.search(block_text):
            raise ValueError(f"nested include not allowed: {block_name}")
        return block_text

    _prompt_cache[name] = _INCLUDE_RE.sub(_inline, text)
    return _prompt_cache[name]


def render(name: str, /, **kwargs: str) -> str:
    text = load_prompt(name)
    tokens = set(_TOKEN_RE.findall(text))
    given = set(kwargs)
    if tokens != given:
        missing = sorted(tokens - given)
        extra = sorted(given - tokens)
        raise ValueError(f"placeholder mismatch: missing={missing} extra={extra}")
    return _TOKEN_RE.sub(lambda m: kwargs[m.group(1)], text)


def clear_cache() -> None:
    _prompt_cache.clear()


# Markdown files end with a newline. The editor's pre-existing system text did not.
TREE_AGENT_EDITOR_SYSTEM = load_prompt("tree_agent_editor").removesuffix("\n")
#: `/16` agent-CLI Editor default (b18, #399, ADR-0061): the V1 text plus one
#: paragraph that tells the Editor to reconcile a new rule with the files it
#: changes. `/15` keeps `TREE_AGENT_EDITOR_SYSTEM` (its digest is in the `/15`
#: identity).
TREE_AGENT_EDITOR_SYSTEM_V2 = load_prompt("tree_agent_editor_v2").removesuffix("\n")
TREE_EDITOR_SYSTEM = load_prompt("tree_editor").removesuffix("\n")
TREE_EDITOR_SYSTEM_V2 = load_prompt("tree_editor_v2").removesuffix("\n")
#: `/16` chat Editor default (b18, #399, ADR-0061): V2 plus one paragraph that
#: tells the Editor to reconcile a new rule with the files it changes. `/15`
#: keeps `TREE_EDITOR_SYSTEM`; V2 stays packaged unchanged.
TREE_EDITOR_SYSTEM_V3 = load_prompt("tree_editor_v3").removesuffix("\n")
TREE_AGENT_REFLECTOR_SYSTEM = load_prompt("tree_agent_reflector").removesuffix("\n")
TREE_REFLECTOR_SYSTEM = load_prompt("tree_reflector").removesuffix("\n")
#: Current campaign default (ADR-0022): frames a selected group's description
#: as the grouping stage's investigation question, not a causal verdict.
TREE_AGENT_REFLECTOR_SYSTEM_V3 = load_prompt("tree_agent_reflector_v3").removesuffix("\n")
TREE_AGENT_REFLECTOR_SYSTEM_V4 = load_prompt("tree_agent_reflector_v4").removesuffix("\n")
TREE_REFLECTOR_SYSTEM_V3 = load_prompt("tree_reflector_v3").removesuffix("\n")
#: `/16` chat only (#318, ADR-0048): V3 plus the corrected-result contract of
#: an in-role checked `finish_reflection` and the `case_id`/`case_handle`
#: distinction. `/15` keeps V3 byte for byte.
TREE_REFLECTOR_SYSTEM_V4 = load_prompt("tree_reflector_v4").removesuffix("\n")
TREE_REFLECTOR_SYSTEM_V5 = load_prompt("tree_reflector_v5").removesuffix("\n")
TREE_REFLECTOR_SYSTEM_V6 = load_prompt("tree_reflector_v6").removesuffix("\n")
#: The packaged default system prompt of a `/10` model grouping role and a
#: `/15` Chat Grouper using `model_grouping/1`; a declared
#: `system_prompt` file wins over it. Its digest is bound into the adaptation
#: identity (`prompt_sha256`).
TREE_GROUPING_MODEL_SYSTEM = load_prompt("tree_grouping_model").removesuffix("\n")
#: `/16` chat Grouper only (b16, ADR-0055): the packaged default of protocol
#: `model_grouping/2`, a portion tool session ending in `finish_portion` and a
#: batched reduce, split on its `## Reduce` line. `/15` keeps
#: `TREE_GROUPING_MODEL_SYSTEM` byte for byte. Groupings formed under 2.0.0b17
#: seal `_v3` instead (ADR-0057): `_v2` stays packaged for runs formed under
#: b16 and the first wave.
TREE_GROUPING_MODEL_V2_SYSTEM = load_prompt("tree_grouping_model_v2").removesuffix("\n")
#: `/16` chat Grouper only (b17, ADR-0057): `_v2` plus the two group-text
#: rules -- a group's texts stand alone (no `o<n>`/`c<n>`/`s<n>` references,
#: cards, variants or other groups; the expectation in full) and stay short
#: (prose targets per key; the bounds are ceilings). Same protocol
#: `model_grouping/2`, answer schema and tools; requests only, no code checks.
TREE_GROUPING_MODEL_V3_SYSTEM = load_prompt("tree_grouping_model_v3").removesuffix("\n")
#: Family 14 only (ADR-0034, A§4): the packaged default system prompt of a
#: `/15` `grouper.kind: agent_cli`; a declared `system_prompt` file wins over
#: it. Its digest is bound into the Grouper identity (`prompt_sha256`). Slice
#: H9 owns the wording (family-14 H1 plan, choice 12).
TREE_AGENT_GROUPER_SYSTEM = load_prompt("tree_agent_grouper").removesuffix("\n")
#: Family 10 only (PDR-0019, ADR-0023): the one continuation turn a chat or
#: agent-CLI Editor may take after an empty-diff decline, asked to make the
#: change or explain why not. Its SHA-256 enters the family-10 Editor
#: identity digest (`continuation_sha256`); it never enters the Reflector's.
TREE_EDITOR_CONTINUATION = load_prompt("tree_editor_continuation")
