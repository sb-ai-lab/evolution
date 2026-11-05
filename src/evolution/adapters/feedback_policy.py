"""Shared native/verifier feedback policy for framework adapters.

The adapters use this module to keep two concerns separate:

* native traces: observations that a framework may learn from
* verifier traces: hidden pytest/audit output used for scoring

The default policy is hidden-oracle safe. More permissive policies are
explicitly labelled so reports cannot silently aggregate them with fair
baseline rows.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_COUNT_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "errors": re.compile(r"(\d+)\s+errors?"),
}
_DROP_ORACLE_LINE = re.compile(
    r"(?:^|\s)(?:FAILED|ERROR)\s|::test_|\btests?/[\w/]*test[\w/]*\.py\b"
    r"|^E\s+(?:assert|where|and|\+|-|\?)|^\s*[+-]\s|^\s*\?\s"
)
_REDACT = (
    re.compile(r"""(['"])(?:[^'"\n]{6,})\1"""),
    re.compile(r"\b\d{4,}\b"),
    re.compile(r"(?:/[\w.\-]+){2,}"),
)


class FeedbackPolicy(StrEnum):
    """Allowed feedback contracts for adapter-visible learning traces."""

    HIDDEN_SUMMARY = "hidden_summary"
    NATIVE_PUBLIC = "native_public"
    ORACLE_DIAGNOSTIC = "oracle_diagnostic"


@dataclass(frozen=True)
class FeedbackPolicyMetadata:
    feedback_policy: str
    oracle_visible: bool
    tests_visible_to_agent: bool
    native_trace_path: str = ""
    verifier_trace_path: str = ""
    guard_profile: str = "workspace_guarded"
    native_services_profile: str = "benchmark_minimal"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_feedback_policy(value: str | None = None) -> FeedbackPolicy:
    raw = (value or os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY") or "").strip().lower()
    if not raw:
        return FeedbackPolicy.HIDDEN_SUMMARY
    aliases = {
        "hidden": FeedbackPolicy.HIDDEN_SUMMARY,
        "hidden_summary": FeedbackPolicy.HIDDEN_SUMMARY,
        "summary": FeedbackPolicy.HIDDEN_SUMMARY,
        "native": FeedbackPolicy.NATIVE_PUBLIC,
        "native_public": FeedbackPolicy.NATIVE_PUBLIC,
        "public": FeedbackPolicy.NATIVE_PUBLIC,
        "oracle": FeedbackPolicy.ORACLE_DIAGNOSTIC,
        "oracle_diagnostic": FeedbackPolicy.ORACLE_DIAGNOSTIC,
        "leaky_train": FeedbackPolicy.ORACLE_DIAGNOSTIC,
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        allowed = ", ".join(sorted(aliases))
        raise ValueError(
            f"Unknown EVO_ADAPTER_FEEDBACK_POLICY={raw!r}; allowed: {allowed}"
        ) from exc


def policy_metadata(
    policy: FeedbackPolicy | str | None = None,
    *,
    native_trace_path: str | Path | None = None,
    verifier_trace_path: str | Path | None = None,
    tests_visible_to_agent: bool = False,
    guard_profile: str = "workspace_guarded",
    native_services_profile: str = "benchmark_minimal",
) -> FeedbackPolicyMetadata:
    resolved = resolve_feedback_policy(str(policy) if policy else None)
    return FeedbackPolicyMetadata(
        feedback_policy=resolved.value,
        oracle_visible=resolved is FeedbackPolicy.ORACLE_DIAGNOSTIC,
        tests_visible_to_agent=bool(tests_visible_to_agent),
        native_trace_path=str(native_trace_path or ""),
        verifier_trace_path=str(verifier_trace_path or ""),
        guard_profile=guard_profile,
        native_services_profile=native_services_profile,
    )


def parse_pytest_counts(pytest_output: str) -> dict[str, int | float]:
    text = pytest_output or ""
    counts: dict[str, int | float] = {}
    for key, pat in _COUNT_PATTERNS.items():
        match = pat.search(text)
        counts[key] = int(match.group(1)) if match else 0
    total = int(counts["passed"]) + int(counts["failed"]) + int(counts["errors"])
    counts["total_tests"] = total
    counts["pass_rate"] = (int(counts["passed"]) / total) if total else 0.0
    return counts


def failed_test_names(pytest_output: str, *, limit: int = 8) -> list[str]:
    names: list[str] = []
    for raw in (pytest_output or "").splitlines():
        match = _FAILED_LINE.match(raw.strip())
        if not match:
            continue
        nodeid = match.group(1)
        names.append(nodeid.rsplit("::", 1)[-1])
        if len(names) >= limit:
            break
    return names


def sanitize_public_feedback(text: str | None, *, limit: int = 4000) -> str:
    if not text:
        return ""
    kept: list[str] = []
    for raw in str(text).splitlines():
        if _DROP_ORACLE_LINE.search(raw):
            continue
        line = raw
        for pat in _REDACT:
            line = pat.sub("<redacted>", line)
        kept.append(line)
    out = "\n".join(kept).strip()
    return out[-limit:] if len(out) > limit else out


def render_pytest_observation(
    *,
    policy: FeedbackPolicy | str | None,
    passed: int,
    total: int,
    pass_rate: float,
    solver_rc: int | None,
    solver_tail: str = "",
    pytest_output: str = "",
    collect_note: str = "",
) -> str:
    resolved = resolve_feedback_policy(str(policy) if policy else None)
    prefix = (
        f"run_tests: {passed}/{total} pytest tests passed "
        f"(rate={pass_rate:.2f}, solver rc={solver_rc})."
    )
    failed = ", ".join(failed_test_names(pytest_output)) or "(none)"
    if resolved is FeedbackPolicy.HIDDEN_SUMMARY:
        return f"{prefix} Failing test names: {failed}.{collect_note}"
    if resolved is FeedbackPolicy.NATIVE_PUBLIC:
        solver = sanitize_public_feedback(solver_tail, limit=2000)
        pytest_tail = sanitize_public_feedback(pytest_output, limit=4000)
        parts = [prefix, f"Failing test names: {failed}."]
        if solver:
            parts.append(f"Solver output tail (public-sanitized):\n{solver}")
        if pytest_tail:
            parts.append(f"Pytest output tail (public-sanitized):\n{pytest_tail}")
        if collect_note:
            parts.append(collect_note.strip())
        return "\n".join(parts)
    tail = (pytest_output or "")[-12000:]
    parts = [prefix, f"Failing test names: {failed}."]
    if solver_tail:
        parts.append(f"Solver output tail:\n{solver_tail[-4000:]}")
    if tail:
        parts.append(f"Pytest output tail (oracle_diagnostic):\n{tail}")
    if collect_note:
        parts.append(collect_note.strip())
    return "\n".join(parts)


__all__ = [
    "FeedbackPolicy",
    "FeedbackPolicyMetadata",
    "resolve_feedback_policy",
    "policy_metadata",
    "parse_pytest_counts",
    "failed_test_names",
    "sanitize_public_feedback",
    "render_pytest_observation",
]
