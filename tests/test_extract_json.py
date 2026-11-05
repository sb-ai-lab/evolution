"""Reflector JSON extraction: fenced/raw/balanced tiers + repair + loud failure."""

from __future__ import annotations

import logging

from evolution.evolve.reflection import _extract_json


def test_plain_double_quoted_json():
    assert _extract_json('{"root_cause": "x", "severity": "major"}')["severity"] == "major"


def test_fenced_json_after_reasoning():
    text = 'Let me think about this.\n\n```json\n{"root_cause": "wrong api"}\n```\nDone.'
    assert _extract_json(text)["root_cause"] == "wrong api"


def test_single_quoted_object_repaired():
    # No double-quotes present -> single->double repair tier recovers it.
    out = _extract_json("prefix {'root_cause': 'x', 'pattern': 'wrong_api'} suffix")
    assert out["root_cause"] == "x"
    assert out["pattern"] == "wrong_api"


def test_trailing_comma_repaired():
    assert _extract_json('{"a": 1, "b": 2,}')["b"] == 2


def test_total_failure_is_loud_and_marked(caplog):
    with caplog.at_level(logging.WARNING, logger="evolution.evolve.reflection"):
        out = _extract_json("there is absolutely no json object here")
    assert out["parse_error"] == "Could not extract JSON"
    assert any("could not extract JSON" in r.message for r in caplog.records)


if __name__ == "__main__":  # pragma: no cover
    test_plain_double_quoted_json()
    test_fenced_json_after_reasoning()
    test_single_quoted_object_repaired()
    test_trailing_comma_repaired()
    print("extract_json checks passed")
