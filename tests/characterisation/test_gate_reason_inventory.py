"""Package-wide inventory of every ``"reason"`` string emitted anywhere in ``evolve/``.

Gate reasons are writer input, not logs: they reach ``rejected_memory.jsonl`` and
``format_for_writer`` and land in the next writer prompt. A reason added, renamed
or reworded in any module changes what the optimizer is told about its last
failure, so the vocabulary is frozen at the package level.

This is the wide, cheap half of the pair. ``test_gate_reasons.py`` is the deep,
narrow half: it drives every ``promotion_gate`` literal through a real fixture and
locks the resulting telemetry bytes. Locking vocabulary here costs no fixtures, so
it can cover modules that have no cheap deterministic driver.

Two shapes carry a reason and both are scanned. A scanner that walks only
``ast.Dict`` is blind to subscript assignment, which is exactly where
``paired_improvement``, ``underpowered`` and ``validation_saturated`` live::

    return False, {**telemetry, "reason": "task_regression"}   # dict literal
    round_state["reason"] = "validation_saturated"             # subscript

``meta_skill._REJECT_REASON`` is deliberately out of scope; see the scope note in
``test_gate_reasons.py``.

To refresh after an INTENTIONAL change, run this file and copy the printed actual
inventory into ``GOLDEN_EVOLVE_REASONS`` in the same commit as the source change.
"""

from __future__ import annotations

import ast
from pathlib import Path

EVOLVE = Path(__file__).resolve().parents[2] / "src" / "evolution" / "evolve"

GOLDEN_EVOLVE_REASONS = (
    "edit entry is not a JSON object",
    "inconclusive_missing_arm",
    "insufficient_pairs",
    "no 'edits' list or edit object found in parsed JSON",
    "no_observed_gain",
    "no_significant_gain",
    "no_validation_tasks",
    "paired_improvement",
    "propose response truncated (finish_reason=length); raise EVO_LLM_MAX_TOKENS",
    "task_regression",
    "underpowered",
    "unpaired_or_empty",
    "validation_saturated",
)


def _result_literals(node: ast.AST) -> set[str]:
    """String literals an expression can evaluate to, ignoring its operands."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return _result_literals(node.body) | _result_literals(node.orelse)
    is_get_call = (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 2
    )
    if is_get_call:
        assert isinstance(node, ast.Call)
        return _result_literals(node.args[1])
    return set()


def _reason_literals_in(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                is_reason_key = isinstance(key, ast.Constant) and key.value == "reason"
                if is_reason_key:
                    found |= _result_literals(value)
        elif isinstance(node, ast.Assign):
            targets_reason = any(
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "reason"
                for target in node.targets
            )
            if targets_reason:
                found |= _result_literals(node.value)
    return found


def evolve_reason_inventory() -> tuple[str, ...]:
    """Every reason literal across ``src/evolution/evolve/**/*.py``."""
    found: set[str] = set()
    for module in sorted(EVOLVE.rglob("*.py")):
        found |= _reason_literals_in(ast.parse(module.read_text(encoding="utf-8")))
    return tuple(sorted(found))


def test_evolve_reason_vocabulary_is_locked() -> None:
    actual = evolve_reason_inventory()
    assert actual == GOLDEN_EVOLVE_REASONS, (
        f"evolve/ reason vocabulary drifted; actual inventory: {actual!r}"
    )


def test_scanner_sees_subscript_assignment_not_only_dict_literals() -> None:
    """Arm the scanner: a dict-literal-only walk would miss these three.

    If this ever passes trivially, the scanner has been narrowed back to
    ``ast.Dict`` and the inventory above is quietly no longer package-wide.
    """
    subscript_only = {"paired_improvement", "underpowered", "validation_saturated"}

    assert subscript_only <= set(evolve_reason_inventory())
