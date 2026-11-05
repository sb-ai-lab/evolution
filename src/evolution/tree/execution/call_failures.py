"""Which target-call failures are retried, and what an unretried one means.

The class is decided by the failure's code alone. The wire's `retryable` flag
can only withhold a retry; it never moves a failure between classes (ADR-0019).
A code nobody here knows -- a custom adapter's own, or an exception that is
not a `RolloutInfrastructureError` at all -- is transient and not retried:
counted against the target's threshold,
never a suspension on its own.
"""

from __future__ import annotations

from typing import Literal

from evolution.tree.execution.interaction import RolloutInfrastructureError

FailureClass = Literal["transient", "contract", "structural"]

TRANSIENT_CODES = frozenset(
    {
        "transport_error",
        "callee_timeout",
        "upstream_unavailable",
        "internal",
        "not_ready",
        "instances_exhausted",
    }
)
CONTRACT_CODES = frozenset(
    {
        "invalid_request",
        "request_conflict",
        "request_expired",
        "unknown_instance",
        "unknown_session",
        "instance_conflict",
        "root_not_allowed",
        "invalid_response",
        "response_too_large",
        "incomplete_response",
    }
)
STRUCTURAL_CODES = frozenset(
    {
        "pin_mismatch",
        "policy_mismatch",
        "target_epoch_changed",
        "limits_incompatible",
        "identity_changed",
    }
)


def _typed(exc: BaseException) -> RolloutInfrastructureError | None:
    """Walk `__cause__` to the first typed failure, preferring an inner adapter
    code over the rollout's own default-coded wrapper (`"target send failed")
    from exc`, code `infrastructure`)."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, RolloutInfrastructureError) and seen.code != "infrastructure":
            return seen
        seen = seen.__cause__
    return exc if isinstance(exc, RolloutInfrastructureError) else None


def classify_failure(exc: BaseException) -> FailureClass:
    from evolution.tree.execution.interaction import AttestationError

    typed = _typed(exc)
    if typed is None:
        return "transient"
    if isinstance(typed, AttestationError) or typed.code in STRUCTURAL_CODES:
        return "structural"
    if typed.code in CONTRACT_CODES:
        return "contract"
    return "transient"


def may_retry(exc: BaseException) -> bool:
    typed = _typed(exc)
    return (
        typed is not None
        and typed.code in TRANSIENT_CODES
        and typed.retryable
        and classify_failure(exc) == "transient"
    )
