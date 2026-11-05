"""Evaluation-lane policy for iterative skill evolution.

The loop always exports a canonical ``SKILL.md`` for auditability. Some
frameworks also have a richer native runtime state that must be evaluated
directly, otherwise their real gains are hidden by the lossy export step.
"""

from __future__ import annotations

CANONICAL_EXPORT = "canonical_export"
NATIVE_RUNTIME = "native_runtime"

CANONICAL_PRIMARY_FRAMEWORKS = frozenset({"hermes", "codex", "claude", "evolution"})
NATIVE_PRIMARY_FRAMEWORKS = frozenset({"evoskill", "memento", "memp"})
SUPPORTED_FRAMEWORKS = CANONICAL_PRIMARY_FRAMEWORKS | NATIVE_PRIMARY_FRAMEWORKS


def normalize_framework(framework: str) -> str:
    return framework.strip().lower()


def primary_eval_lane(framework: str) -> str:
    fw = normalize_framework(framework)
    if fw in NATIVE_PRIMARY_FRAMEWORKS:
        return NATIVE_RUNTIME
    if fw in CANONICAL_PRIMARY_FRAMEWORKS:
        return CANONICAL_EXPORT
    raise ValueError(f"unsupported framework: {framework}")


def supports_native_runtime(framework: str) -> bool:
    return normalize_framework(framework) in NATIVE_PRIMARY_FRAMEWORKS


def supported_eval_lanes(framework: str) -> tuple[str, ...]:
    return (
        (CANONICAL_EXPORT, NATIVE_RUNTIME)
        if supports_native_runtime(framework)
        else (CANONICAL_EXPORT,)
    )


__all__ = [
    "CANONICAL_EXPORT",
    "CANONICAL_PRIMARY_FRAMEWORKS",
    "NATIVE_PRIMARY_FRAMEWORKS",
    "NATIVE_RUNTIME",
    "SUPPORTED_FRAMEWORKS",
    "normalize_framework",
    "primary_eval_lane",
    "supported_eval_lanes",
    "supports_native_runtime",
]
