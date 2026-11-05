"""Closed check modes and canonical declaration identity."""

import hashlib

import pytest

from evolution.tree.execution.check_modes import (
    CHECK_NAMES,
    CODES,
    CONSEQUENCES,
    ENFORCE_ALL,
    parse_checks,
)
from evolution.tree.execution.evidence_settings import SettingsError
from evolution.tree.execution.journal_identity import canonical_json


def test_unlisted_checks_enforce_and_non_enforce_keeps_name_order():
    modes = parse_checks(
        {
            "version": "checks/1",
            "modes": {
                "seal_oracle_leak": "off",
                "reflection_oracle_leak": "shadow",
            },
        }
    )
    assert modes.mode("grouping_run_secret") == "enforce"
    assert modes.non_enforce() == (
        ("reflection_oracle_leak", "shadow"),
        ("seal_oracle_leak", "off"),
    )
    assert set(CONSEQUENCES) == set(CHECK_NAMES) == set(CODES)
    assert modes.input_modes() == {
        "input_run_secret_redaction": "enforce",
        "attempt_feedback_screen": "enforce",
        "history_excerpt_screen": "enforce",
    }
    assert ENFORCE_ALL.non_enforce() == ()


@pytest.mark.parametrize(
    "modes,code,path",
    [
        ({"primary_metric_range": "off"}, "unknown_key", "modes.primary_metric_range"),
        ({"seal_oracle_leak": "warn"}, "bad_value", "modes.seal_oracle_leak"),
        ({"seal_oracle_leak": []}, "bad_value", "modes.seal_oracle_leak"),
        ([], "bad_type", "modes"),
    ],
)
def test_unknown_names_and_modes_are_refused_with_the_path(modes, code, path):
    with pytest.raises(SettingsError) as raised:
        parse_checks({"version": "checks/1", "modes": modes})
    assert (raised.value.code, raised.value.path) == (code, path)


@pytest.mark.parametrize(
    "value,code,path",
    [
        (None, "bad_type", ""),
        ({}, "missing_key", "version"),
        ({"version": "checks/1"}, "missing_key", "modes"),
        ({"version": "checks/2", "modes": {}}, "bad_value", "version"),
        ({"version": 1, "modes": {}}, "bad_type", "version"),
        ({"version": "checks/1", "modes": {}, "extra": "private"}, "unknown_key", "extra"),
    ],
)
def test_checks_requires_a_closed_versioned_object(value, code, path):
    with pytest.raises(SettingsError) as raised:
        parse_checks(value)
    assert (raised.value.code, raised.value.path) == (code, path)
    assert "private" not in str(raised.value)


def test_checks_payload_and_digest_are_canonical_and_owned():
    declaration = {
        "version": "checks/1",
        "modes": {
            "seal_oracle_leak": "off",
            "history_excerpt_screen": "shadow",
        },
    }
    modes = parse_checks(declaration)
    before = modes.digest
    expected = {
        "version": "checks/1",
        "modes": {
            "history_excerpt_screen": "shadow",
            "seal_oracle_leak": "off",
        },
    }
    assert modes.to_payload() == expected
    assert list(modes.to_payload()["modes"]) == ["history_excerpt_screen", "seal_oracle_leak"]
    assert before == hashlib.sha256(canonical_json(expected)).hexdigest()
    declaration["modes"]["seal_oracle_leak"] = "enforce"
    assert modes.digest == before
    assert modes.input_modes()["history_excerpt_screen"] == "shadow"


def test_hits_merge_by_check_code_and_leak_in_first_hit_order():
    from evolution.tree.execution.check_modes import HitSink

    hits = HitSink()
    log = []
    leak = {"case_id": "c1", "oracle_pointer": "/answer"}
    hits.hit("reflection_oracle_leak", "oracle_leak", leak=leak, log=log.append)
    hits.hit("input_run_secret_redaction", "redacted", log=log.append)
    hits.hit("reflection_oracle_leak", "oracle_leak", leak=leak, log=log.append)
    leak["case_id"] = "changed"
    assert hits.entries() == [
        {
            "check": "reflection_oracle_leak",
            "code": "oracle_leak",
            "count": 2,
            "leak": {"case_id": "c1", "oracle_pointer": "/answer"},
        },
        {"check": "input_run_secret_redaction", "code": "redacted", "count": 1},
    ]
    assert [entry["count"] for entry in log] == [1, 1, 1]
    assert all(entry["event"] == "check_shadowed" for entry in log)
    hits.entries()[0]["leak"]["case_id"] = "mutated"
    assert hits.entries()[0]["leak"]["case_id"] == "c1"


def test_hits_keep_distinct_leak_locations_separate():
    from evolution.tree.execution.check_modes import HitSink

    hits = HitSink()
    for leak in ({"case_id": "c1"}, {"case_id": "c2"}, {"case_id": "c1", "oracle_pointer": "/a"}):
        hits.hit("seal_oracle_leak", "oracle_leak", leak=leak)
    assert [entry["leak"] for entry in hits.entries()] == [
        {"case_id": "c1"},
        {"case_id": "c2"},
        {"case_id": "c1", "oracle_pointer": "/a"},
    ]


def test_journal_redaction_keeps_keys_that_collide_after_redaction():
    from evolution.tree.execution.check_modes import redact_for_journal

    assert redact_for_journal({"akey-one": 1, "akey-two": 2}, ("key-one", "key-two")) == {
        "a[REDACTED]": 1,
        "a[REDACTED]#2": 2,
    }
    assert redact_for_journal(
        {"akey-one": 1, "akey-two": 2, "a[REDACTED]#2": 3}, ("key-one", "key-two")
    ) == {"a[REDACTED]": 1, "a[REDACTED]#2": 2, "a[REDACTED]#2#2": 3}


def test_a_secret_free_value_is_unchanged():
    from evolution.tree.execution.check_modes import redact_for_journal

    value = {"nested": ["text", 3, True, None, {"a": "b"}]}
    assert redact_for_journal(value, ("key-one",)) == value
    assert redact_for_journal({"x": ["key-one", {"key-one": "key-one"}]}, ("key-one",)) == {
        "x": ["[REDACTED]", {"[REDACTED]": "[REDACTED]"}],
    }


def test_terminal_fields_omit_empty_hits_and_return_an_independent_payload():
    from evolution.tree.execution.check_modes import HitSink

    hits = HitSink()
    assert hits.terminal_fields() == {}
    hits.hit("reflection_run_secret", "run_secret")
    fields = hits.terminal_fields()
    assert fields == {
        "shadowed": [{"check": "reflection_run_secret", "code": "run_secret", "count": 1}]
    }
    fields["shadowed"][0]["count"] = 99
    assert hits.entries()[0]["count"] == 1


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_mode_predicate_skips_off_and_records_shadow_without_retaining_text(mode):
    from evolution.tree.execution.check_modes import CheckModes, HitSink, check_predicate

    calls = []
    logs = []

    def detector(value):
        calls.append(value)
        return value == "private-value"

    hits = HitSink(log=logs.append)
    predicate = check_predicate(
        "grouping_run_secret",
        "secret_detected",
        detector,
        modes=CheckModes({"grouping_run_secret": mode}),
        hits=hits,
    )
    assert predicate("private-value") is (mode == "enforce")
    assert predicate("plain") is False
    assert calls == ([] if mode == "off" else ["private-value", "plain"])
    assert hits.entries() == (
        [{"check": "grouping_run_secret", "code": "secret_detected", "count": 1}]
        if mode == "shadow"
        else []
    )
    assert "private-value" not in repr(logs)


@pytest.mark.parametrize("surface", ["role_input", "journal"])
def test_nested_secrets_are_redacted_whole_in_keys_and_values(surface):
    from evolution.tree.execution.check_modes import redact_for_journal
    from evolution.tree.execution.input_secrets import redact_run_secrets

    short, long = "sk-abc", "sk-abc-1234567890"
    redact = redact_run_secrets if surface == "role_input" else redact_for_journal
    value = {f"key-{long}": {"text": f"token {long} leaked", "items": [short, long]}}
    assert redact(value, ("", short, short, long, "")) == {
        "key-[REDACTED]": {
            "text": "token [REDACTED] leaked",
            "items": ["[REDACTED]", "[REDACTED]"],
        }
    }
