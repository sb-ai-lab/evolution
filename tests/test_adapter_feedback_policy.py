from evolution.adapters.feedback_policy import (
    FeedbackPolicy,
    policy_metadata,
    render_pytest_observation,
    sanitize_public_feedback,
)

PYTEST_OUTPUT = """\
FAILED tests/test_outputs.py::test_report - AssertionError: expected "secret-value-12345"
E   assert "actual" == "secret-value-12345"
collected 2 items
1 passed, 1 failed
/tmp/hidden/tests/test_outputs.py
"""


def test_hidden_summary_does_not_emit_assertion_text() -> None:
    rendered = render_pytest_observation(
        policy=FeedbackPolicy.HIDDEN_SUMMARY,
        passed=1,
        total=2,
        pass_rate=0.5,
        solver_rc=0,
        pytest_output=PYTEST_OUTPUT,
    )

    assert "secret-value" not in rendered
    assert "AssertionError" not in rendered
    assert "tests/test_outputs.py" not in rendered
    assert "test_report" in rendered


def test_native_public_sanitizes_pytest_and_solver_tails() -> None:
    rendered = render_pytest_observation(
        policy=FeedbackPolicy.NATIVE_PUBLIC,
        passed=1,
        total=2,
        pass_rate=0.5,
        solver_rc=0,
        solver_tail='wrote "/tmp/hidden/source_artifacts/private.csv"',
        pytest_output=PYTEST_OUTPUT,
    )

    assert "secret-value" not in rendered
    assert "tests/test_outputs.py" not in rendered
    assert "/tmp/hidden" not in rendered
    assert "<redacted>" in rendered


def test_oracle_diagnostic_is_explicitly_oracle_visible() -> None:
    rendered = render_pytest_observation(
        policy=FeedbackPolicy.ORACLE_DIAGNOSTIC,
        passed=1,
        total=2,
        pass_rate=0.5,
        solver_rc=0,
        pytest_output=PYTEST_OUTPUT,
    )
    meta = policy_metadata(FeedbackPolicy.ORACLE_DIAGNOSTIC).to_dict()

    assert "secret-value" in rendered
    assert "oracle_diagnostic" in rendered
    assert meta["oracle_visible"] is True
    assert meta["tests_visible_to_agent"] is False


def test_public_sanitizer_drops_oracle_lines() -> None:
    sanitized = sanitize_public_feedback(PYTEST_OUTPUT)

    assert "FAILED" not in sanitized
    assert "E   assert" not in sanitized
    assert "secret-value" not in sanitized
    assert "collected" in sanitized
