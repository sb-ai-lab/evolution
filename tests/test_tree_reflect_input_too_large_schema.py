"""`reflection_failed {reason: input_too_large}` and the probe-less start (#228, merged D5, D7)."""

from __future__ import annotations

import pytest

from evolution.tree.campaign_schema import (
    ADAPTIVE_REFLECTION_ERRORS,
    FINAL_REFLECTION_ERRORS,
)
from evolution.tree.roles.role_failures import GROUPER_REASONS, reflection_class, reflection_reason


def test_the_reason_is_unknown_for_the_reflector_and_content_for_the_grouper() -> None:
    assert reflection_reason("input_too_large", None) == "input_too_large"
    assert reflection_class("input_too_large", "input_too_large") == "unknown"
    assert GROUPER_REASONS["input_too_large"] == "content"


def test_the_error_class_exists_in_family_14_only() -> None:
    assert "input_too_large" in FINAL_REFLECTION_ERRORS
    assert "input_too_large" not in ADAPTIVE_REFLECTION_ERRORS


def test_family14_admits_only_the_matching_reason() -> None:
    from evolution.tree.campaign_schema import SchemaError, validate_payload
    from tests.test_tree_campaign_schema import _projection_changed_failure

    payload = _projection_changed_failure(error_class="input_too_large", reason="input_too_large")
    validate_payload("reflection_failed", payload, final=True)
    with pytest.raises(SchemaError, match="carries a reason"):
        validate_payload(
            "reflection_failed", {k: v for k, v in payload.items() if k != "reason"}, final=True
        )
    with pytest.raises(SchemaError, match="reflection reason outside"):
        validate_payload("reflection_failed", {**payload, "reason": "invalid_result"}, final=True)
