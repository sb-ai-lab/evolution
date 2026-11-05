"""Contracts for the record redactor, `evolution.tree.storage.redaction.safe_record`.

Credential values and hidden model reasoning must be removed, including inside
a serialized JSON string. These contracts exercise the shared function called
by the append log, Candidate editors and `evo tree evolve`.
"""

from __future__ import annotations

import json

import pytest

from evolution.tree.storage.redaction import safe_record


def test_safe_record_redacts_credentials_and_hidden_reasoning() -> None:
    record = safe_record(
        {
            "Authorization": "credential-value",
            "observation": "visible <think>hidden</think> credential-value",
        },
        ("credential-value",),
    )

    encoded = json.dumps(record)
    assert "credential-value" not in encoded
    assert "hidden" not in encoded
    assert "visible" in encoded


def test_safe_record_redacts_serialized_http_response_secrets_and_reasoning() -> None:
    record = safe_record(
        {
            "response": json.dumps(
                {
                    "Authorization": "Bearer response-secret",
                    "token": "response-token",
                    "visible": "ok <analysis>hidden</analysis> <reasoning>also hidden</reasoning>",
                }
            )
        }
    )

    encoded = json.dumps(record)
    assert "response-secret" not in encoded
    assert "response-token" not in encoded
    assert "hidden" not in encoded
    assert "ok" in encoded


@pytest.mark.parametrize(
    "content",
    ('{ "visible": 1 }', '{"visible":1,"visible":2}'),
)
def test_safe_record_preserves_serialized_content_that_needs_no_redaction(content: str) -> None:
    assert safe_record(content) == content


def test_safe_record_removes_a_secret_from_an_overwritten_json_member() -> None:
    content = '{"note":"secret-value","note":"public"}'

    assert "secret-value" not in safe_record(content, ("secret-value",))


def test_safe_record_leaves_a_short_redaction_out_of_structured_values() -> None:
    """A redaction is a substring rule, so it must not eat a value it merely matches."""

    record = safe_record({"champion": {"digest": "yes", "path": "yes"}}, ("yes",))

    assert record == {"champion": {"digest": "[REDACTED]", "path": "[REDACTED]"}}


def test_safe_record_redacts_a_long_value_inside_a_longer_string() -> None:
    record = safe_record({"root": "/private/credential-value/version"}, ("credential-value",))

    assert record == {"root": "/private/[REDACTED]/version"}


def test_safe_record_keeps_token_counts_and_redacts_token_credentials() -> None:
    """`token` in a key name means a credential, not a counter.

    A provider's `usage` block names its counts `*_tokens`; the key-name rule
    must let those through (and `reasoning_tokens`, which the hidden-reasoning
    rule would otherwise catch) while still redacting every credential-shaped
    key that carries `token` in the singular.
    """

    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
        "completion_tokens_details": {"reasoning_tokens": 5, "accepted_prediction_tokens": 0},
        "max_tokens": 4096,
        "cost": 0.01,
    }
    assert safe_record({"usage": usage}) == {"usage": usage}

    credentials = {
        "token": "t",
        "access_token": "t",
        "api_token": "t",
        "id_token": "t",
        "refresh_token": "t",
        "Authorization": "t",
        "reasoning": "hidden",
    }
    assert safe_record(credentials) == dict.fromkeys(credentials, "[REDACTED]")
