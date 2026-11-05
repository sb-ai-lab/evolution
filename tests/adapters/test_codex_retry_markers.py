"""Task 10: Codex-aware retry predicate.

`_should_retry_evo_failure` must retry the union of the pre-existing
transient markers and the Codex-transient markers, but NEVER retry the
two hard-abort conditions (missing codex binary / bad credential), since
retrying those just burns a long unattended run's time budget.
"""

from __future__ import annotations

import subprocess

from evolution.adapters.evolution_adapter import _should_retry_evo_failure


def _proc(stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["evo"], returncode=1, stdout=stdout, stderr=stderr)


def test_retries_existing_transient_markers() -> None:
    for marker in (
        "Cannot connect to host",
        "connection reset",
        "rate limit exceeded",
        "litellm.APIError: boom",
        "request timeout",
    ):
        assert _should_retry_evo_failure(_proc(stderr=marker)) is True, marker


def test_retries_codex_transient_markers() -> None:
    for marker in (
        "HTTP 403 Forbidden",
        "HTTP 429 Too Many Requests",
        "CodexBackendError: stream closed",
        "AgentBackendError: stream closed",
        "codex exec produced no output",
        "empty completion from model",
    ):
        assert _should_retry_evo_failure(_proc(stderr=marker)) is True, marker


def test_hard_abort_codex_executable_not_found() -> None:
    # Even though "produced no output" would otherwise retry, the hard-abort
    # marker must win so the run fails loudly instead of looping.
    p = _proc(stderr="codex executable not found; produced no output")
    assert _should_retry_evo_failure(p) is False


def test_hard_abort_invalid_grant() -> None:
    p = _proc(stderr="oauth error: invalid_grant (rate limit?)")
    assert _should_retry_evo_failure(p) is False


def test_non_transient_not_retried() -> None:
    assert _should_retry_evo_failure(_proc(stderr="AssertionError: bad skill")) is False
