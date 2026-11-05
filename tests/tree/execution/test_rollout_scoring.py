import json

import pytest

from evolution.tree.execution.rollout_scoring import (
    MAX_DIAGNOSTICS_BYTES,
    MAX_VERDICTS,
    ScoringError,
    _parse_score_result,
    parse_diagnostics,
    parse_verdicts,
)


def _output(**overrides):
    base = {"metrics": {"score": 1.0}, "critical_pass": None, "verdicts": []}
    base.update(overrides)
    return base


def test_verdicts_parse_to_closed_mappings():
    result = _parse_score_result(
        _output(verdicts=[{"criterion": "answers", "passed": False, "note": "no route"}])
    )
    assert result.verdicts == ({"criterion": "answers", "passed": False, "note": "no route"},)


def test_verdict_note_defaults_to_none():
    assert parse_verdicts([{"criterion": "a", "passed": True}]) == (
        {"criterion": "a", "passed": True, "note": None},
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        [{"criterion": "a"}],
        [{"criterion": "a", "passed": "yes"}],
        [{"criterion": "a", "passed": True, "extra": 1}],
        [{"criterion": "bad id!", "passed": True}],
        [{"criterion": "a", "passed": True}, {"criterion": "a", "passed": False}],
        [{"criterion": "a", "passed": True, "note": "x" * 257}],
        [{"criterion": "a", "passed": True, "note": "tab\tinside"}],
        [{"criterion": f"c{i}", "passed": True} for i in range(MAX_VERDICTS + 1)],
    ],
)
def test_bad_verdicts_are_scoring_errors(bad):
    with pytest.raises(ScoringError):
        parse_verdicts(bad)


def test_empty_verdicts_still_accepted():
    assert _parse_score_result(_output()).verdicts == ()


BASE = {"metrics": {"quality": 1.0}}


def test_diagnostics_is_refused_unless_the_family_admits_it():
    with pytest.raises(ScoringError, match="unknown fields"):
        _parse_score_result({**BASE, "diagnostics": {"route": "handoff"}})


def test_admitted_diagnostics_reach_the_result_frozen():
    result = _parse_score_result(
        {**BASE, "diagnostics": {"route": "handoff", "votes": [{"why": "x"}]}},
        diagnostics=True,
    )
    assert result.diagnostics["route"] == "handoff"
    with pytest.raises(TypeError):
        result.diagnostics["route"] = "answer"  # type: ignore[index]


def test_absent_diagnostics_is_none_even_when_admitted():
    assert _parse_score_result(dict(BASE), diagnostics=True).diagnostics is None


@pytest.mark.parametrize(
    "value",
    [
        [],
        "text",
        {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}},  # depth 9
        {"x": "y" * MAX_DIAGNOSTICS_BYTES},
        {"n": float("nan")},
        {"n": float("inf")},
    ],
)
def test_bad_diagnostics_are_scoring_errors(value):
    with pytest.raises(ScoringError):
        parse_diagnostics(value)


def _nested(levels: int, leaf: object = 1) -> object:
    value: object = leaf
    for _ in range(levels):
        value = {"k": value}
    return value


@pytest.mark.parametrize("levels", [500, 5000])
def test_pathologically_nested_diagnostics_are_scoring_errors(levels):
    """A 500-level object encodes in about 3 KB, and the depth check
    must refuse it as a `ScoringError`, never leak a `RecursionError`."""

    with pytest.raises(ScoringError, match="depth 8|finite JSON"):
        parse_diagnostics(_nested(levels))


def test_nested_lists_count_toward_the_depth():
    assert parse_diagnostics({"a": [[[[[[[1]]]]]]]})  # depth 8
    with pytest.raises(ScoringError, match="depth 8"):
        parse_diagnostics({"a": [[[[[[[[1]]]]]]]]})  # depth 9


def test_diagnostics_at_the_depth_and_size_limits_are_accepted():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}  # depth 8
    assert parse_diagnostics(deep)["a"]["b"]["c"]["d"]["e"]["f"]["g"]["h"] == 1
    overhead = len(json.dumps({"x": ""}, separators=(",", ":")).encode())
    exact = {"x": "y" * (MAX_DIAGNOSTICS_BYTES - overhead)}
    assert len(parse_diagnostics(exact)["x"]) == MAX_DIAGNOSTICS_BYTES - overhead


# -- Typed Scorer errors --------------------------------------------


def test_scoring_error_defaults_are_untyped_and_message_is_unchanged():
    error = ScoringError("boom")
    assert (error.code, error.metric, str(error)) == (None, None, "boom")
    typed = ScoringError("boom", code="timeout")
    assert (typed.code, typed.metric) == ("timeout", None)
    with pytest.raises(TypeError):
        ScoringError("boom", "timeout")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("metrics", "code", "metric"),
    [
        ({"quality": True}, "metric_type", "quality"),
        ({"quality": "x"}, "metric_type", "quality"),
        ({"quality": None}, "metric_type", "quality"),
        ({"quality": float("inf")}, "metric_nonfinite", "quality"),
        ({"quality": 1, "other": float("-inf")}, "metric_nonfinite", "other"),
        ({"quality": 10**400}, "metric_nonfinite", "quality"),
        ({"quality": 1, "other": -(10**400)}, "metric_nonfinite", "other"),
        ({"quality": 1, "other": False}, "metric_type", "other"),
    ],
)
def test_metric_refusals_carry_code_and_metric_name(metrics, code, metric):
    with pytest.raises(ScoringError, match="invalid ScoreResult") as exc:
        _parse_score_result({"metrics": metrics})
    assert (exc.value.code, exc.value.metric) == (code, metric)


@pytest.mark.parametrize(
    "output",
    [
        {"metrics": {"quality": 1}, "unknown": 1},
        {"metrics": []},
        {},
        {"metrics": {"quality": 1}, "public_feedback": []},
        {"metrics": {"quality": 1}, "details": 3},
        {"metrics": {"quality": 1}, "verdicts": "x"},
        {"metrics": {"quality": 1}, "critical_pass": "yes"},
        {"metrics": {"": 1}},
        {"metrics": {}},
    ],
)
def test_other_shape_refusals_are_output_invalid(output):
    with pytest.raises(ScoringError) as exc:
        _parse_score_result(output)
    assert (exc.value.code, exc.value.metric) == ("output_invalid", None)


def test_json_object_refusals_are_output_invalid():
    from evolution.tree.execution.rollout_scoring import parse_scorer_stdout

    for raw in ("not-json", "NaN", '{"metrics":NaN}', "[1]", '{"a":1,"a":2}', "[" * 100000):
        with pytest.raises(ScoringError) as exc:
            parse_scorer_stdout(raw)
        assert (exc.value.code, exc.value.metric) == ("output_invalid", None)


def test_score_result_refusals_keep_every_old_exception_type():
    from evolution.tree.execution.interaction import ScoreResult, ScoreResultError

    for metrics, code in (
        ({"q": True}, "metric_type"),
        ({"q": float("nan")}, "metric_nonfinite"),
        # #202: an integer too large for a float is non-finite, never an OverflowError.
        ({"q": 10**400}, "metric_nonfinite"),
    ):
        with pytest.raises(ScoreResultError) as exc:
            ScoreResult(metrics)
        error = exc.value
        assert isinstance(error, ValueError)
        assert isinstance(error, ScoringError)
        assert (error.code, error.metric, str(error)) == (
            code,
            "q",
            "metric values must be finite numbers",
        )
    # The other refusals are untouched plain ValueErrors.
    for kwargs in (
        {"metrics": {}},
        {"metrics": {" ": 1}},
        {"metrics": {"q": 1}, "critical_pass": 1},
    ):
        with pytest.raises(ValueError) as plain:
            ScoreResult(**kwargs)
        assert not isinstance(plain.value, ScoringError)
