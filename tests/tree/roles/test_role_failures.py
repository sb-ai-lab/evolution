import pytest

from evolution.tree.roles import role_failures as rf


@pytest.mark.parametrize(
    ("error_class", "code", "reason", "klass"),
    [
        ("reported", "provider_exhausted", "provider_exhausted", "provider"),
        ("reported", "provider_rejected", "provider_rejected", "provider"),
        ("reported", "preflight_failed", "preflight_failed", "unknown"),
        ("reported", "protocol", "protocol_exhausted", "content"),
        ("reported", "turns_exhausted", "turns_exhausted", "content"),
        ("reported", "budget", "turns_exhausted", "content"),
        ("reported", "self_check_failed", "self_check_failed", "content"),
        ("reported", "invalid_result", "invalid_result", "content"),
        ("invalid_result", None, "invalid_result", "content"),
        ("oracle_leak", None, "oracle_leak", "content"),
        ("reported", "agent_error", "agent_error", "unknown"),
        ("reported", "prompt_changed", "unclassified", "unknown"),
        ("reported", None, "unclassified", "unknown"),
        ("timeout", None, "timeout", "unknown"),
        ("control_collision", None, "control_collision", "unknown"),
        ("reader_integrity", None, "reader_integrity", "unknown"),
        ("exception:KeyError", None, "exception", "unknown"),
    ],
)
def test_reflection_table(error_class, code, reason, klass):
    assert rf.reflection_reason(error_class, code) == reason
    assert rf.reflection_class(error_class, reason) == klass


@pytest.mark.parametrize(
    ("error_class", "code", "reason", "klass"),
    [
        ("reported", "provider_exhausted", "provider_exhausted", "provider"),
        ("reported", "provider_rejected", "provider_rejected", "provider"),
        ("reported", "preflight_failed", "preflight_failed", "unknown"),
        ("reported", "protocol", "protocol_exhausted", "content"),
        ("reported", "turns_exhausted", "turns_exhausted", "content"),
        ("reported", "budget", "turns_exhausted", "content"),
        ("reported", "self_check_failed", "self_check_failed", "content"),
        ("reported", "actor_violation", "actor_violation", "content"),
        ("reported", "changed_file_budget", "changed_file_budget", "content"),
        ("reported", "written_byte_budget", "written_byte_budget", "content"),
        ("reported", "plan_mismatch", "plan_mismatch", "content"),
        ("reported", "agent_error", "agent_error", "unknown"),
        ("reported", "exit_status", "program_failed", "unknown"),
        ("reported", "bad_result", "program_failed", "unknown"),
        ("reported", "program_failed", "program_failed", "unknown"),
        ("reported", "workspace_changed", "environment_changed", "unknown"),
        ("reported", "command_changed", "environment_changed", "unknown"),
        ("reported", "provider_changed", "unclassified", "unknown"),
        ("timeout", None, "timeout", "unknown"),
        ("exception:OSError", None, "exception", "unknown"),
    ],
)
def test_editor_table(error_class, code, reason, klass):
    assert rf.editor_reason(error_class, code) == reason
    assert rf.editor_class(error_class, reason) == klass


@pytest.mark.parametrize("error_class", ["interrupted", "cancelled", "unresponsive"])
def test_lifecycle_has_no_reason(error_class):
    assert rf.reflection_reason(error_class, "anything") is None
    assert rf.editor_reason(error_class, "anything") is None


def test_pair_outside_table_raises():
    with pytest.raises(KeyError):
        rf.editor_class("timeout", "provider_exhausted")


def test_projection_changed_is_a_content_reflection_failure():
    """H11 (PDR-0019, ADR-0023): a Reflector that changed its own projection
    produced no admissible diagnosis; the class is `content`, never journaled."""

    assert rf.reflection_reason("projection_changed", None) == "projection_changed"
    assert rf.reflection_class("projection_changed", "projection_changed") == "content"
    with pytest.raises(KeyError):
        rf.editor_class("projection_changed", "projection_changed")


def test_grouper_reasons_are_the_closed_adr_0034_table():
    """ADR-0034: the eight Grouper reasons and their classes."""

    assert rf.GROUPER_REASONS == {
        "provider_exhausted": "provider",
        "provider_rejected": "provider",
        "invalid_output": "content",
        "input_too_large": "content",
        "turns_exhausted": "content",
        "agent_error": "unknown",
        "timeout": "unknown",
        "exception": "unknown",
    }
    for reason, klass in rf.GROUPER_REASONS.items():
        assert rf.grouper_class(reason) == klass


@pytest.mark.parametrize(
    "reason", ["provider_unavailable", "deadline_exhausted", "unclassified", "role_failed", ""]
)
def test_grouper_class_raises_outside_the_table(reason):
    with pytest.raises(KeyError):
        rf.grouper_class(reason)


def test_grouper_role_failure_carries_a_valid_pair():
    failure = rf.GrouperRoleFailure("provider_rejected", "provider")
    assert isinstance(failure, Exception)
    assert (failure.reason, failure.error_class) == ("provider_rejected", "provider")
    assert rf.GrouperRoleFailure("turns_exhausted", "content").error_class == "content"
    assert rf.GrouperRoleFailure("exception", "unknown").reason == "exception"


@pytest.mark.parametrize(
    ("reason", "error_class"),
    [
        ("provider_rejected", "unknown"),
        ("invalid_output", "provider"),
        ("timeout", "content"),
        ("provider_unavailable", "provider"),
        ("exception", "exception"),
    ],
)
def test_grouper_role_failure_refuses_a_mismatched_pair(reason, error_class):
    with pytest.raises(ValueError, match="Grouper"):
        rf.GrouperRoleFailure(reason, error_class)


@pytest.mark.parametrize(
    ("error_class", "suspension"),
    [
        ("provider", "grouper_provider_failed"),
        ("unknown", "grouper_failing"),
        ("content", "grouper_failing"),
    ],
)
def test_grouper_suspension_for_each_class(error_class, suspension):
    assert rf.grouper_suspension_for(error_class) == suspension


def test_grouper_suspension_for_refuses_a_value_outside_the_classes():
    with pytest.raises(KeyError):
        rf.grouper_suspension_for("exception")


@pytest.mark.parametrize("code", ["changed_file_budget", "written_byte_budget"])
def test_the_budget_codes_are_content_in_family_14_and_actor_violation_before(code):
    assert rf.editor_reason("reported", code) == code
    assert rf.editor_class("reported", code) == "content"
    assert rf.journaled_editor_reason("reported", code, family_version=14) == code
    for family in (10, 11, 12, 13):
        assert (
            rf.journaled_editor_reason("reported", code, family_version=family) == "actor_violation"
        )
    assert (
        rf.journaled_editor_reason("reported", "actor_violation", family_version=14)
        == "actor_violation"
    )
    assert rf.journaled_editor_reason("interrupted", None, family_version=14) is None


def test_the_budget_reason_family_is_the_final_campaign_family():
    from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION

    assert rf.EDITOR_BUDGET_REASON_FAMILY == FINAL_CAMPAIGN_FAMILY_VERSION


PROGRAM_CODES = ["exit_status", "result_too_large", "bad_result", "no_result", "program_failed"]


@pytest.mark.parametrize("code", PROGRAM_CODES)
def test_reflection_program_codes_are_named_from_family_15(code):
    assert rf.reflection_reason("reported", code, family_version=15) == "program_failed"
    assert rf.reflection_reason("reported", code, family_version=14) == "unclassified"
    assert rf.reflection_reason("reported", code) == "unclassified"
    assert rf.reflection_class("reported", "program_failed") == "unknown"
