"""Closed evidence settings and stable observation defaults."""

import pytest

from evolution.tree.execution.evidence_settings import (
    DEFAULT_EVIDENCE,
    EvidenceSettings,
    SettingsError,
    parse_evidence,
)
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2


def test_defaults_are_b12_values_plus_the_two_new_limits():
    assert DEFAULT_EVIDENCE.to_payload() == {
        "version": "evidence/1",
        "view": "full",
        "turns": 8,
        "events_per_turn": 32,
        "text_chars": 4096,
        "input_chars": 2048,
        "feedback_keys": 16,
        "feedback_value_chars": 64,
        "cited_chars": 65536,
        "trace_page_chars": 16000,
    }
    assert DEFAULT_EVIDENCE.limits() == {k: BRIEF_LIMITS_V2[k] for k in DEFAULT_EVIDENCE.limits()}
    assert EvidenceSettings.from_payload(DEFAULT_EVIDENCE.to_payload()) == DEFAULT_EVIDENCE


def test_a_partial_object_fills_defaults_and_has_no_upper_bound():
    settings = parse_evidence({"version": "evidence/1", "events_per_turn": 10**30})
    assert settings.events_per_turn == 10**30
    assert settings.turns == 8 and settings.view == "full"


@pytest.mark.parametrize(
    "member,value,code",
    [
        ("view", "raw", "bad_value"),
        ("view", [], "bad_type"),
        ("turns", 0, "bad_value"),
        ("events_per_turn", 1, "bad_value"),
        ("text_chars", 31, "bad_value"),
        ("input_chars", -1, "bad_value"),
        ("feedback_keys", -1, "bad_value"),
        ("feedback_value_chars", 0, "bad_value"),
        ("cited_chars", -1, "bad_value"),
        ("trace_page_chars", 255, "bad_value"),
        ("turns", True, "bad_type"),
        ("turns", 1.5, "bad_type"),
        ("extra", "PRIVATE-VALUE", "unknown_key"),
    ],
)
def test_bad_members_name_the_path_not_the_value(member, value, code):
    with pytest.raises(SettingsError) as raised:
        parse_evidence({"version": "evidence/1", member: value})
    assert (raised.value.code, raised.value.path) == (code, member)
    assert "PRIVATE-VALUE" not in str(raised.value)


@pytest.mark.parametrize(
    "value,code,path",
    [
        (None, "bad_type", ""),
        ([], "bad_type", ""),
        ({}, "missing_key", "version"),
        ({"version": 1}, "bad_type", "version"),
        ({"version": "evidence/2"}, "bad_value", "version"),
    ],
)
def test_evidence_requires_a_versioned_object(value, code, path):
    with pytest.raises(SettingsError) as raised:
        parse_evidence(value)
    assert (raised.value.code, raised.value.path) == (code, path)


def test_evidence_admits_the_minimums():
    settings = parse_evidence(
        {
            "version": "evidence/1",
            "view": "allowlist",
            "turns": 1,
            "events_per_turn": 2,
            "text_chars": 32,
            "input_chars": 0,
            "feedback_keys": 0,
            "feedback_value_chars": 1,
            "cited_chars": 0,
            "trace_page_chars": 256,
        }
    )
    assert settings.view == "allowlist" and settings.input_chars == 0
