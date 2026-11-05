"""Strict envelope grammar and secret-match locations."""

import json

import pytest

from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    SchemaError,
    SecretMatch,
    secret_match,
)
from tests.tree.execution.test_journal_schema import record


def test_family_is_required():
    valid_record = record()
    with pytest.raises(TypeError):
        Envelope.from_payload(valid_record)


def test_wire_roundtrip_detaches_payload():
    raw = record()
    parsed = Envelope.from_payload(raw, family=S1_FAMILY)
    raw["payload"]["sample_id"] = "changed"
    assert parsed.to_payload()["payload"]["sample_id"] == "sample-1"
    assert (
        Envelope.from_bytes(parsed.encode(), family=S1_FAMILY).to_payload() == parsed.to_payload()
    )


def test_event_and_operation_id_are_read_without_decoding(monkeypatch):
    # #95: an audit filters journals by `event` on every record it walks, so
    # reading it must not decode the whole envelope again.
    parsed = Envelope.from_payload(record(), family=S1_FAMILY)

    def decode(_record):
        pytest.fail("decoded the envelope to read its event")

    monkeypatch.setattr(Envelope, "to_payload", decode)
    assert parsed.event == "rollout_started"
    assert parsed.operation_id == "op-1"
    again = Envelope.from_bytes(parsed.encode(), family=S1_FAMILY)
    assert again == parsed and hash(again) == hash(parsed)
    assert repr(again) == repr(parsed)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("sequence", True),
        ("sequence", 0),
        ("event", "promotion_pending"),
        ("run_id", "../secret"),
        ("operation_id", None),
        ("timestamp", "2026-09-06T12:00:00+03:00"),
    ],
)
def test_invalid_envelope_is_rejected(key, value):
    raw = record()
    raw[key] = value
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=S1_FAMILY)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[]",
        b"\xff",
    ],
)
def test_ambiguous_or_nonfinite_json_rejected(raw):
    with pytest.raises(SchemaError):
        Envelope.from_bytes(raw, family=S1_FAMILY)


def test_secret_match_reports_the_lowest_matching_index():
    value = {"payload": {"a": "xx zzzzzzzz yy", "b": ["abcdefgh"]}}
    found = secret_match(value, ("abcdefgh", "zzzzzzzz"))
    # The walk reaches "a" first (zzzzzzzz), but the lowest index wins.
    assert found == SecretMatch(location="a", index=0)
    assert secret_match(value, ("", "zzzzzzzz")) == SecretMatch(location="a", index=1)
    assert secret_match(value, ("nothing", "")) is None
    assert secret_match({"payload": {"k": "v"}}, ()) is None


def test_secret_match_locations_use_the_public_grammar():
    assert secret_match({"payload": {"abcdefgh": 1}}, ("abcdefgh",)).location == "record-key"
    assert secret_match({"run_id": "abcdefgh"}, ("abcdefgh",)).location == "envelope.run_id"
    nested = {"payload": {"x": [{"y": "abcdefgh"}]}}
    assert secret_match(nested, ("abcdefgh",)).location == "x[0].y"


def _pre_branch_refuses(item, secrets):
    """The journal's secret check before #287, kept as the fixed reference."""

    if isinstance(item, str):
        return any(secret and secret in item for secret in secrets)
    if isinstance(item, dict):
        return any(
            _pre_branch_refuses(key, secrets) or _pre_branch_refuses(child, secrets)
            for key, child in item.items()
        )
    if isinstance(item, list):
        return any(_pre_branch_refuses(child, secrets) for child in item)
    return False


def _with_envelope(**fields):
    raw = record()
    raw.update(fields)
    return raw


def _with_payload(payload):
    raw = record()
    raw["payload"] = payload
    return raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(record(), None, id="harmless"),
        pytest.param(_with_envelope(run_id="secret-two"), ("envelope.run_id", 1), id="envelope"),
        pytest.param(
            _with_envelope(operation_id="secret-one"), ("envelope.operation_id", 0), id="op"
        ),
        pytest.param(
            _with_payload({"case_id": "pre-secret-one-post"}), ("case_id", 0), id="substring"
        ),
        # #287: the payload-root key `""` has the empty path, and a
        # later non-matching sibling must not drop the match.
        pytest.param(_with_payload({"": "secret-one", "z": "x"}), ("", 0), id="empty-key"),
        pytest.param(
            _with_payload({"a": "x", "b": {"c": "secret-two"}, "d": "y"}),
            ("b.c", 1),
            id="nested-value",
        ),
        pytest.param(
            _with_payload({"x": [{"a": "y"}, {"pre-secret-two": "v"}], "z": "w"}),
            ("record-key", 1),
            id="key-in-list-of-dicts",
        ),
        # The first match walked is the location; the index is the lowest secret
        # anywhere, here a different one (a caller asks again with it alone).
        pytest.param(
            _with_payload({"a": "secret-two", "secret-one": "v"}),
            ("a", 0),
            id="key-beside-earlier-value",
        ),
        pytest.param(
            _with_payload({"secret-two": "secret-one"}), ("record-key", 0), id="key-before-value"
        ),
    ],
)
def test_secret_match_and_the_journal_refuse_the_same_payloads(raw, expected):
    secrets = ("secret-one", "secret-two")
    found = secret_match(json.loads(json.dumps(raw)), secrets)
    if expected is None:
        assert found is None
    else:
        assert found == SecretMatch(location=expected[0], index=expected[1])
    # The journal asks the same predicate, and refuses what it refused before #287;
    # a permissive family lets the payload shapes above reach the secret check.
    family = EnvelopeFamily(lambda event, payload: None, frozenset(), lambda event: False)
    Envelope.from_payload(raw, family=family)
    try:
        Envelope.from_payload(raw, secrets=secrets, family=family)
        refused = False
    except SchemaError:
        refused = True
    assert refused is (expected is not None)
    assert refused is _pre_branch_refuses(raw, secrets)
