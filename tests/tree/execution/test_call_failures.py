import pytest

from evolution.tree.execution.call_failures import classify_failure, may_retry
from evolution.tree.execution.interaction import AttestationError
from evolution.tree.execution.interaction import RolloutInfrastructureError as E


@pytest.mark.parametrize(
    ("code", "retryable", "klass", "retry"),
    [
        ("transport_error", True, "transient", True),
        ("callee_timeout", True, "transient", True),
        ("upstream_unavailable", True, "transient", True),
        ("internal", True, "transient", True),
        ("not_ready", True, "transient", True),
        ("instances_exhausted", True, "transient", True),
        # the flag only withholds a retry; the class follows the code
        ("internal", False, "transient", False),
        ("invalid_request", True, "contract", False),
        ("invalid_request", False, "contract", False),
        ("request_conflict", False, "contract", False),
        ("unknown_session", False, "contract", False),
        ("unknown_instance", False, "contract", False),
        ("invalid_response", False, "contract", False),
        ("response_too_large", False, "contract", False),
        ("pin_mismatch", False, "structural", False),
        ("policy_mismatch", False, "structural", False),
        ("target_epoch_changed", False, "structural", False),
        ("limits_incompatible", False, "structural", False),
        ("identity_changed", False, "structural", False),
        ("admission_closed", False, "transient", False),
        ("target_unavailable", False, "transient", False),  # a custom adapter's own code
    ],
)
def test_classification_table(code, retryable, klass, retry):
    exc = E("x", code, retryable=retryable)
    assert classify_failure(exc) == klass
    assert may_retry(exc) is retry


def test_attestation_is_structural():
    exc = AttestationError("x", "attestation_mismatch")
    assert classify_failure(exc) == "structural"


def test_an_unclassified_exception_is_transient_and_not_retried():
    assert classify_failure(ValueError("boom")) == "transient"
    assert may_retry(ValueError("boom")) is False


def test_a_wrapped_error_is_classified_by_its_cause():
    inner = E("x", "internal", retryable=True)
    outer = E("target send failed")
    outer.__cause__ = inner
    assert classify_failure(outer) == "transient"
    assert may_retry(outer) is True
