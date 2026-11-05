"""The pure connection-probe core.

Every case here is built by hand: a `History` of frozen turns, or a raw event
stream where a value the frozen turn refuses (a non-finite number) has to be
reached, and a `ScoreResult` or a typed Scorer failure. No I/O, no journal.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import FrozenInstanceError, fields

import pytest

from evolution.tree.execution.interaction import (
    EvalCase,
    History,
    Message,
    ScoreResult,
    ScoreResultError,
    ScoringError,
    Turn,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.json_pointer import MISSING
from evolution.tree.target.connection_contract import MetricContract, TargetField
from evolution.tree.target.connection_probe import (
    CAUSE_CODES,
    CLEANUP_STATES,
    CONNECTION_BINDING_VERSION,
    DETAIL_CODES,
    INFRASTRUCTURE_CAUSES,
    MAX_PATH_BYTES,
    OUTCOME_STATUSES,
    PROBE_PROFILE,
    Diagnostic,
    FieldFailure,
    ProbeOutcome,
    ScorerFailure,
    binding_digest,
    binding_payload,
    cap_path,
    check_event_fields,
    check_primary,
    check_target_fields,
    classify,
    description_secret,
    expected_type,
    history_events,
    journalable_path,
    observed_type,
    probe_seed,
    representative_case,
    secret_path,
)

SECRET = "sk-live-0123456789abcdef-SECRET"
PRIMARY = "quality"


def turn(index, *events):
    return Turn(index, Message(""), tuple(events), None, 0, "ok")


def history(*turn_events):
    return History(tuple(turn(index, *events) for index, events in enumerate(turn_events)))


def contract(*specs):
    return MetricContract("pointwise", tuple(TargetField(*spec) for spec in specs))


ASSISTANT = ("assistant_message", "/content", "nonempty_string")


def field_check(events, *specs):
    """The first field failure over a single-turn raw event stream."""

    return check_event_fields(list(events), contract(*specs))


def diag(failure):
    assert isinstance(failure, FieldFailure)
    return failure.diagnostic


def manifest(index):
    return f"scorer.metric_contract.target_fields[{index}]"


def observed(event, pointer):
    return f"events[event={event},last]{pointer}"


def all_text(value):
    """Everything a diagnostic could leak: its repr and every string field."""

    return repr(value) + json.dumps(
        {f.name: getattr(value, f.name) for f in fields(value)}, default=repr
    )


# --- Target-field cases over a History ------------------------------------------


def test_a_missing_event_is_connection_event_missing():
    failure = check_target_fields(
        history([{"type": "tool_call", "name": "x"}]), contract(ASSISTANT)
    )
    assert diag(failure) == Diagnostic(
        "connection_event_missing", manifest(0), observed("assistant_message", "/content")
    )


def test_a_missing_pointer_is_connection_field_missing():
    failure = check_target_fields(
        history([{"type": "assistant_message", "text": "hi"}]), contract(ASSISTANT)
    )
    assert diag(failure) == Diagnostic(
        "connection_field_missing", manifest(0), observed("assistant_message", "/content")
    )


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("", "nonempty_string"),
        ("", "string"),
        ("", "finite_json"),
        ({}, "object"),
        ({}, "finite_json"),
        ([], "array"),
        ([], "finite_json"),
        ("   \t\n", "nonempty_string"),
    ],
)
def test_an_empty_value_is_connection_field_empty(value, rule):
    failure = check_target_fields(
        history([{"type": "assistant_message", "content": value}]),
        contract(("assistant_message", "/content", rule)),
    )
    assert diag(failure).detail_code == "connection_field_empty"
    assert diag(failure).manifest_path == manifest(0)
    assert diag(failure).observed_path == observed("assistant_message", "/content")


def test_a_whitespace_string_satisfies_the_plain_string_rule():
    assert (
        check_target_fields(
            history([{"type": "assistant_message", "content": "  "}]),
            contract(("assistant_message", "/content", "string")),
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (7, "nonempty_string"),
        (["x"], "string"),
        ("7", "finite_number"),
        (True, "finite_number"),
        (False, "finite_number"),
        (1, "boolean"),
        ("true", "boolean"),
        (["a"], "object"),
        ("{}", "object"),
        ({"a": 1}, "array"),
        ("x", "array"),
        (None, "nonempty_string"),
        (None, "string"),
        (None, "finite_number"),
        (None, "boolean"),
        (None, "object"),
        (None, "array"),
        (None, "finite_json"),
        ("", "object"),
        ([], "string"),
    ],
)
def test_a_wrong_type_or_null_is_connection_field_type(value, rule):
    failure = check_target_fields(
        history([{"type": "assistant_message", "content": value}]),
        contract(("assistant_message", "/content", rule)),
    )
    assert diag(failure) == Diagnostic(
        "connection_field_type", manifest(0), observed("assistant_message", "/content")
    )


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("hello", "nonempty_string"),
        ("hello", "string"),
        (0, "finite_number"),
        (-2.5, "finite_number"),
        (False, "boolean"),
        ({"a": None}, "object"),
        ([None], "array"),
        ({"a": [1, {"b": None, "c": "  "}], "d": False}, "finite_json"),
        (0, "finite_json"),
        (10**400, "finite_number"),
        ({"big": [10**400]}, "finite_json"),
        (False, "finite_json"),
        ("  ", "finite_json"),
    ],
)
def test_a_satisfied_rule_passes(value, rule):
    assert (
        check_target_fields(
            history([{"type": "assistant_message", "content": value}]),
            contract(("assistant_message", "/content", rule)),
        )
        is None
    )


def test_a_frozen_turn_refuses_a_non_finite_event_value():
    """Why the non-finite cases below use a raw event stream: a `History` cannot
    hold one, because the turn boundary already refuses it."""

    with pytest.raises(ValueError, match="finite"):
        turn(0, {"type": "assistant_message", "content": math.inf})


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (math.inf, "finite_number"),
        (-math.inf, "finite_number"),
        (math.nan, "finite_number"),
        (math.nan, "finite_json"),
        ({"a": [1, {"b": math.inf}]}, "finite_json"),
        ([0, [math.nan]], "finite_json"),
    ],
)
def test_a_non_finite_value_is_connection_field_nonfinite(value, rule):
    failure = field_check(
        [{"type": "assistant_message", "content": value}], ("assistant_message", "/content", rule)
    )
    assert diag(failure) == Diagnostic(
        "connection_field_nonfinite", manifest(0), observed("assistant_message", "/content")
    )


@pytest.mark.parametrize(
    "value",
    [{"a": {1, 2}}, [b"bytes"], {1: "non-string key"}, {"a": object()}],
)
def test_a_nested_non_json_value_under_finite_json_is_connection_field_type(value):
    failure = field_check(
        [{"type": "assistant_message", "content": value}],
        ("assistant_message", "/content", "finite_json"),
    )
    assert diag(failure).detail_code == "connection_field_type"


def test_a_deep_finite_json_value_does_not_recurse():
    value: object = 1.0
    for _ in range(5000):
        value = [value]
    assert (
        field_check(
            [{"type": "assistant_message", "content": value}],
            ("assistant_message", "/content", "finite_json"),
        )
        is None
    )


def test_a_valid_contract_is_compatible():
    events = history(
        [{"type": "assistant_message", "content": "answer", "score": 0.5}],
        [{"type": "tool_call", "args": {"q": "x"}}],
    )
    assert (
        check_target_fields(
            events,
            contract(
                ASSISTANT,
                ("assistant_message", "/score", "finite_number"),
                ("tool_call", "/args", "object"),
            ),
        )
        is None
    )


def test_an_empty_target_field_list_is_compatible():
    assert check_target_fields(history(), contract()) is None
    assert check_target_fields(history([{"type": "x"}]), contract()) is None


# --- selection, pointers, order and bounds --------------------------------------------


def test_the_selected_event_is_the_last_of_its_type_across_all_turns():
    events = history(
        [{"type": "assistant_message", "content": "first"}],
        [{"type": "assistant_message", "content": ""}, {"type": "tool_call"}],
        [{"type": "tool_call"}],
    )
    assert diag(check_target_fields(events, contract(ASSISTANT))).detail_code == (
        "connection_field_empty"
    )
    events = history(
        [{"type": "assistant_message", "content": ""}],
        [{"type": "assistant_message", "content": "later"}],
    )
    assert check_target_fields(events, contract(ASSISTANT)) is None


def test_an_earlier_turn_supplies_the_event_when_later_turns_have_none():
    events = history(
        [{"type": "assistant_message", "content": "only"}],
        [{"type": "tool_call"}],
    )
    assert check_target_fields(events, contract(ASSISTANT)) is None


def test_a_trailing_output_truncated_event_is_skipped():
    events = history(
        [{"type": "assistant_message", "content": "kept"}, {"type": "output_truncated"}],
    )
    assert check_target_fields(events, contract(ASSISTANT)) is None


def test_history_events_flatten_in_emission_order():
    events = history([{"type": "a"}, {"type": "b"}], [], [{"type": "c"}])
    assert [event["type"] for event in history_events(events)] == ["a", "b", "c"]


def test_an_empty_pointer_selects_the_whole_event():
    events = history([{"type": "assistant_message", "content": "x"}])
    assert check_target_fields(events, contract(("assistant_message", "", "object"))) is None
    failure = check_target_fields(events, contract(("assistant_message", "", "string")))
    assert diag(failure) == Diagnostic(
        "connection_field_type", manifest(0), observed("assistant_message", "")
    )


@pytest.mark.parametrize(
    ("pointer", "code"),
    [
        ("/items/0", None),
        ("/items/1", None),
        ("/items/2", "connection_field_missing"),
        ("/items/01", "connection_field_missing"),
        ("/items/-", "connection_field_missing"),
        ("/items/+1", "connection_field_missing"),
        ("/items/１", "connection_field_missing"),
        ("/nested/a~1b", None),
        ("/nested/m~0n", None),
        ("/content/0", "connection_field_missing"),
    ],
)
def test_array_indices_and_escapes_follow_pointer_value(pointer, code):
    events = history(
        [
            {
                "type": "assistant_message",
                "content": "text",
                "items": ["a", "b"],
                "nested": {"a/b": "slash", "m~n": "tilde"},
            }
        ]
    )
    failure = check_target_fields(events, contract(("assistant_message", pointer, "string")))
    assert (None if failure is None else diag(failure).detail_code) == code


def test_the_first_failure_in_manifest_order_is_reported():
    events = history([{"type": "assistant_message", "content": 7}])
    failure = check_target_fields(
        events,
        contract(
            ("assistant_message", "/content", "finite_number"),
            ("missing_event", "/x", "string"),
            ("assistant_message", "/content", "string"),
            ("assistant_message", "/absent", "string"),
        ),
    )
    assert diag(failure) == Diagnostic(
        "connection_event_missing", manifest(1), observed("missing_event", "/x")
    )


def test_cap_path_bounds_utf8_bytes_without_splitting_a_character():
    assert cap_path("abc") == "abc"
    long = "é" * 2000  # 4,000 bytes
    capped = cap_path(long)
    assert len(capped.encode("utf-8")) <= MAX_PATH_BYTES
    assert capped == "é" * (MAX_PATH_BYTES // 2)
    odd = "a" + "é" * 2000
    assert len(cap_path(odd).encode("utf-8")) == MAX_PATH_BYTES - 1
    assert cap_path("\ud800") == "?"


def test_paths_are_capped_at_2048_bytes():
    pointer = "/" + "k" * 3000
    event = "e" * 300
    failure = field_check([{"type": event}], (event, pointer, "string"))
    assert len(diag(failure).observed_path.encode("utf-8")) == MAX_PATH_BYTES
    assert diag(failure).observed_path.startswith(f"events[event={event},last]/kkk")
    long_metric = "m" * 4000
    primary = check_primary({"other": 1.0}, long_metric)
    assert primary is not None
    assert len(primary.observed_path.encode("utf-8")) == MAX_PATH_BYTES


def test_a_diagnostic_never_carries_the_observed_value():
    for value, rule in [
        (SECRET, "finite_number"),
        ({"token": SECRET}, "array"),
        ([SECRET], "object"),
        ({"nested": [SECRET, math.inf]}, "finite_json"),
        ({SECRET: {1, 2}}, "finite_json"),
    ]:
        failure = field_check(
            [{"type": "assistant_message", "content": value, "other": SECRET}],
            ("assistant_message", "/content", rule),
        )
        assert isinstance(failure, FieldFailure)
        assert SECRET not in all_text(failure.diagnostic)
        assert SECRET not in all_text(failure)
        outcome = classify(
            primary_metric=PRIMARY, cleanup="released", terminal="completed", field_failure=failure
        )
        assert SECRET not in all_text(outcome)


def test_a_secret_scorer_failure_yields_only_codes_and_paths():
    error = ScoringError(f"output {SECRET}", code="output_invalid")
    outcome = classify(
        primary_metric=PRIMARY,
        cleanup="released",
        terminal="completed",
        scorer_failure=ScorerFailure.from_error(error),
    )
    assert SECRET not in all_text(outcome)
    error = ScoreResultError(f"bad {SECRET}", code="metric_type", metric=SECRET)
    outcome = classify(
        primary_metric=PRIMARY,
        cleanup="released",
        terminal="completed",
        scorer_failure=ScorerFailure.from_error(error),
    )
    assert outcome.detail_code == "connection_scorer_output_invalid"
    assert SECRET not in all_text(outcome)


# --- bounded types for the owner-only trace (Q2) --------------------------------------


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("x", "string"),
        (1, "number"),
        (1.5, "number"),
        (math.inf, "number"),
        (True, "boolean"),
        (None, "null"),
        ({}, "object"),
        ([], "array"),
        ((), "array"),
        (MISSING, "missing"),
        ({1, 2}, None),
        (b"x", None),
    ],
)
def test_observed_type_is_bounded(value, kind):
    assert observed_type(value) == kind


@pytest.mark.parametrize(
    ("rule", "kind"),
    [
        ("nonempty_string", "string"),
        ("string", "string"),
        ("finite_number", "number"),
        ("boolean", "boolean"),
        ("object", "object"),
        ("array", "array"),
        ("finite_json", "json"),
    ],
)
def test_expected_type_per_rule(rule, kind):
    assert expected_type(rule) == kind


def test_a_field_failure_carries_the_trace_types_and_no_value():
    failure = check_target_fields(
        history([{"type": "assistant_message", "content": SECRET}]),
        contract(("assistant_message", "/content", "finite_number")),
    )
    assert failure == FieldFailure(
        Diagnostic("connection_field_type", manifest(0), observed("assistant_message", "/content")),
        expected_type="number",
        observed_type="string",
    )
    missing = check_target_fields(history(), contract(ASSISTANT))
    assert (missing.expected_type, missing.observed_type) == ("string", "missing")


# --- the Scorer result check -----------------------------------------------------------


def test_a_present_finite_primary_passes():
    assert check_primary(ScoreResult({PRIMARY: 0.25, "other": 1}).metrics, PRIMARY) is None


def test_check_primary_on_a_raw_mapping_does_not_convert_a_huge_integer():
    # `ScoreResult` is the overflow gate; production passes
    # `ScoreResult.metrics` (floats only).
    assert check_primary({PRIMARY: 10**400}, PRIMARY) is None


def test_a_missing_primary_is_connection_primary_missing():
    assert check_primary(ScoreResult({"other": 1.0}).metrics, PRIMARY) == Diagnostic(
        "connection_primary_missing", "campaign.metric.primary", "scorer.metrics.quality"
    )


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "connection_primary_type"),
        ("0.5", "connection_primary_type"),
        (None, "connection_primary_type"),
        (math.inf, "connection_primary_nonfinite"),
        (math.nan, "connection_primary_nonfinite"),
    ],
)
def test_a_raw_bad_primary_is_typed(value, code):
    """`ScoreResult` refuses these at construction (they then arrive as a typed
    `ScoringError`, below); the raw-mapping check covers them anyway."""

    assert check_primary({PRIMARY: value}, PRIMARY) == Diagnostic(
        code, "campaign.metric.primary", "scorer.metrics.quality"
    )


def test_score_result_refuses_bad_metrics_with_the_scorer_codes():
    for metrics, code in [
        ({PRIMARY: True}, "metric_type"),
        ({PRIMARY: "0.5"}, "metric_type"),
        ({PRIMARY: math.inf}, "metric_nonfinite"),
        ({PRIMARY: 10**400}, "metric_nonfinite"),
    ]:
        with pytest.raises(ScoreResultError) as caught:
            ScoreResult(metrics)
        failure = ScorerFailure.from_error(caught.value)
        assert failure == ScorerFailure(code, PRIMARY)


# --- classify(): the full mapping table (choice 8, R55, R56, Ruling 8) -----------------

U = "connection_preflight_unavailable"
I = "connection_preflight_inconclusive"  # noqa: E741
X = "connection_incompatible"
GOOD = ScoreResult({PRIMARY: 0.5})
FIELD_FAIL = FieldFailure(
    Diagnostic("connection_field_empty", manifest(2), observed("assistant_message", "/content")),
    expected_type="string",
    observed_type="string",
)
PRIMARY_PATHS = ("campaign.metric.primary", "scorer.metrics.quality")


def sf(code, metric=None):
    return ScorerFailure(code, metric)


MAPPING = [
    # (id, inputs, (status, detail_code, manifest_path, observed_path, cause_code))
    (
        "compatible",
        {"terminal": "completed", "score": GOOD},
        ("compatible", None, None, None, None),
    ),
    # materialize / identity / attestation infrastructure
    ("infra", {"infrastructure_code": "infrastructure"}, (U, None, None, None, "infrastructure")),
    (
        "attestation",
        {"infrastructure_code": "attestation_mismatch"},
        (U, None, None, None, "attestation_mismatch"),
    ),
    (
        "invalid_response",
        {"infrastructure_code": "invalid_response"},
        (U, None, None, None, "invalid_response"),
    ),
    (
        "unbounded infra code",
        {"infrastructure_code": "bad code with spaces"},
        (U, None, None, None, "infrastructure"),
    ),
    ("empty infra code", {"infrastructure_code": ""}, (U, None, None, None, "infrastructure")),
    # rollout terminals
    ("target_failed", {"terminal": "target_failed"}, (U, None, None, None, "target_failed")),
    (
        "environment_failed",
        {"terminal": "environment_failed"},
        (U, None, None, None, "environment_failed"),
    ),
    (
        "infrastructure_failed",
        {"terminal": "infrastructure_failed"},
        (U, None, None, None, "infrastructure_failed"),
    ),
    ("turn_budget", {"terminal": "turn_budget"}, (I, None, None, None, "turn_budget")),
    ("time_budget", {"terminal": "time_budget"}, (I, None, None, None, "time_budget")),
    (
        "candidate_failed",
        {"terminal": "candidate_failed"},
        (I, None, None, None, "candidate_failed"),
    ),
    # Target fields
    (
        "field",
        {"terminal": "completed", "field_failure": FIELD_FAIL},
        (X, "connection_field_empty", manifest(2), observed("assistant_message", "/content"), None),
    ),
    # Scorer codes
    (
        "process_failed",
        {"terminal": "completed", "scorer_failure": sf("process_failed")},
        (U, None, None, None, "process_failed"),
    ),
    (
        "timeout",
        {"terminal": "completed", "scorer_failure": sf("timeout")},
        (U, None, None, None, "timeout"),
    ),
    (
        "uncoded scorer failure",
        {"terminal": "completed", "scorer_failure": sf(None)},
        (U, None, None, None, "scorer_failed"),
    ),
    (
        "unknown scorer code",
        {"terminal": "completed", "scorer_failure": sf("something_else")},
        (U, None, None, None, "scorer_failed"),
    ),
    (
        "output_invalid",
        {"terminal": "completed", "scorer_failure": sf("output_invalid")},
        (X, "connection_scorer_output_invalid", "scorer", "scorer.output", "output_invalid"),
    ),
    (
        "primary metric_type",
        {"terminal": "completed", "scorer_failure": sf("metric_type", PRIMARY)},
        (X, "connection_primary_type", *PRIMARY_PATHS, None),
    ),
    (
        "primary metric_nonfinite",
        {"terminal": "completed", "scorer_failure": sf("metric_nonfinite", PRIMARY)},
        (X, "connection_primary_nonfinite", *PRIMARY_PATHS, None),
    ),
    (
        "other metric_type",
        {"terminal": "completed", "scorer_failure": sf("metric_type", "aux")},
        (X, "connection_scorer_output_invalid", "scorer", "scorer.metrics", "metric_type"),
    ),
    (
        "other metric_nonfinite",
        {"terminal": "completed", "scorer_failure": sf("metric_nonfinite", "aux")},
        (X, "connection_scorer_output_invalid", "scorer", "scorer.metrics", "metric_nonfinite"),
    ),
    (
        "metric code without a metric",
        {"terminal": "completed", "scorer_failure": sf("metric_type")},
        (X, "connection_scorer_output_invalid", "scorer", "scorer.metrics", "metric_type"),
    ),
    (
        "primary missing",
        {"terminal": "completed", "score": ScoreResult({"aux": 1.0})},
        (X, "connection_primary_missing", *PRIMARY_PATHS, None),
    ),
    # cleanup: a refused release overrides every outcome
    (
        "cleanup over compatible",
        {"terminal": "completed", "score": GOOD, "cleanup": "failed"},
        (U, None, None, None, "cleanup_failed"),
    ),
    (
        "cleanup over incompatible",
        {"terminal": "completed", "field_failure": FIELD_FAIL, "cleanup": "failed"},
        (U, None, None, None, "cleanup_failed"),
    ),
    (
        "cleanup over inconclusive",
        {"terminal": "turn_budget", "cleanup": "failed"},
        (U, None, None, None, "cleanup_failed"),
    ),
    (
        "cleanup over infra",
        {"infrastructure_code": "infrastructure", "cleanup": "failed"},
        (U, None, None, None, "cleanup_failed"),
    ),
    # a failure without an instance has nothing to release
    (
        "infra not_needed",
        {"infrastructure_code": "infrastructure", "cleanup": "not_needed"},
        (U, None, None, None, "infrastructure"),
    ),
    # precedence follows protocol order
    (
        "infra before terminal",
        {"infrastructure_code": "infrastructure", "terminal": "turn_budget"},
        (U, None, None, None, "infrastructure"),
    ),
]


@pytest.mark.parametrize(
    ("inputs", "expected"), [m[1:] for m in MAPPING], ids=[m[0] for m in MAPPING]
)
def test_classify_mapping_table(inputs, expected):
    arguments = {"primary_metric": PRIMARY, "cleanup": "released", **inputs}
    outcome = classify(**arguments)
    assert outcome == ProbeOutcome(*expected)
    assert (
        outcome.status,
        outcome.detail_code,
        outcome.manifest_path,
        outcome.observed_path,
        outcome.cause_code,
    ) == expected


def test_classify_pins_the_vocabularies():
    assert DETAIL_CODES == (
        "connection_event_missing",
        "connection_field_missing",
        "connection_field_empty",
        "connection_field_type",
        "connection_field_nonfinite",
        "connection_primary_missing",
        "connection_primary_type",
        "connection_primary_nonfinite",
        "connection_scorer_output_invalid",
        "connection_description_secret",
    )
    assert OUTCOME_STATUSES == (
        "compatible",
        "connection_incompatible",
        "connection_preflight_unavailable",
        "connection_preflight_inconclusive",
    )
    assert CLEANUP_STATES == ("released", "not_needed", "failed")
    assert [f.name for f in fields(ProbeOutcome)] == [
        "status",
        "detail_code",
        "manifest_path",
        "observed_path",
        "cause_code",
    ]


@pytest.mark.parametrize(
    "inputs",
    [
        {"terminal": "cancelled"},
        {"terminal": "interrupted"},
        {"terminal": "scorer_failed"},
        {"terminal": "nonsense"},
        {},
        {"terminal": "completed"},
        {"terminal": "completed", "score": GOOD, "scorer_failure": sf("timeout")},
        {"terminal": "completed", "score": GOOD, "cleanup": "not_needed"},
        {"terminal": "completed", "score": GOOD, "cleanup": "leaked"},
    ],
)
def test_classify_refuses_what_is_not_a_probe_outcome(inputs):
    arguments = {"primary_metric": PRIMARY, "cleanup": "released", **inputs}
    with pytest.raises(ValueError):
        classify(**arguments)


def test_an_empty_contract_still_applies_the_scorer_check():
    assert check_target_fields(history([{"type": "x"}]), contract()) is None
    outcome = classify(
        primary_metric=PRIMARY,
        cleanup="released",
        terminal="completed",
        score=ScoreResult({"aux": 1.0}),
    )
    assert outcome.detail_code == "connection_primary_missing"


def test_classify_a_field_failure_before_any_scorer_result():
    outcome = classify(
        primary_metric=PRIMARY,
        cleanup="released",
        terminal="completed",
        field_failure=FIELD_FAIL,
        score=ScoreResult({"aux": 1.0}),
    )
    assert outcome.detail_code == "connection_field_empty"


@pytest.mark.parametrize(
    "arguments",
    [
        ("compatible", "connection_field_empty", None, None, None),
        ("compatible", None, None, None, "x"),
        (X, None, None, None, None),
        (X, "connection_field_empty", None, "p", None),
        (X, "not_a_code", "m", "p", None),
        (U, None, None, None, None),
        (U, "connection_field_empty", "m", "p", "x"),
        (U, None, "m", None, "x"),
        (I, None, None, None, None),
        ("closed", None, None, None, None),
        (X, "connection_field_empty", "m" * (MAX_PATH_BYTES + 1), "p", None),
        (U, None, None, None, "x" * 65),
        (X, "connection_field_empty", "m", "p", "output_invalid"),
        (X, "connection_scorer_output_invalid", "m", "p", None),
        (X, "connection_scorer_output_invalid", "m", "p", "timeout"),
    ],
)
def test_probe_outcome_refuses_an_incoherent_combination(arguments):
    with pytest.raises(ValueError):
        ProbeOutcome(*arguments)


# --- The closed cause vocabulary -------------------------------------------------------


#: Every code the product's own target path raises at materialize or identity:
#: `materialize_target`, the HTTP adapter and its call ledger, the attestation
#: check, and the Target Service's wire error codes.
PRODUCT_INFRASTRUCTURE_CODES = frozenset(
    {
        "infrastructure",
        "invalid_response",
        "invalid_request",
        "root_not_allowed",
        "not_preflighted",
        "response_too_large",
        "callee_timeout",
        "transport_error",
        "admission_closed",
        "upstream_unavailable",
        "target_epoch_changed",
        "attestation_missing",
        "attestation_mismatch",
        "strategy_unsupported",
        "request_conflict",
        "request_expired",
        "unknown_instance",
        "unknown_session",
        "instance_conflict",
        "instances_exhausted",
        "policy_mismatch",
        "not_ready",
        "internal",
    }
)


def test_the_cause_vocabulary_is_closed_per_status():
    assert INFRASTRUCTURE_CAUSES == PRODUCT_INFRASTRUCTURE_CODES
    assert dict(CAUSE_CODES) == {
        "connection_incompatible": frozenset({"output_invalid", "metric_type", "metric_nonfinite"}),
        "connection_preflight_unavailable": frozenset(
            {
                "process_failed",
                "timeout",
                "scorer_failed",
                "cleanup_failed",
                "target_failed",
                "environment_failed",
                "infrastructure_failed",
            }
        )
        | PRODUCT_INFRASTRUCTURE_CODES,
        "connection_preflight_inconclusive": frozenset(
            {"turn_budget", "time_budget", "candidate_failed"}
        ),
    }


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        (U, "made_up_code"),
        (U, "target_unavailable"),
        (U, "turn_budget"),
        (U, "output_invalid"),
        (I, "target_failed"),
        (I, "infrastructure"),
        (I, "made_up_code"),
    ],
)
def test_probe_outcome_refuses_a_cause_outside_its_status(status, cause):
    with pytest.raises(ValueError):
        ProbeOutcome(status, None, None, None, cause)


@pytest.mark.parametrize("code", ["target_unavailable", "custom_adapter_code", "target_unstable"])
def test_classify_folds_an_unknown_adapter_code_into_infrastructure(code):
    outcome = classify(primary_metric=PRIMARY, cleanup="not_needed", infrastructure_code=code)
    assert outcome == ProbeOutcome(U, None, None, None, "infrastructure")


@pytest.mark.parametrize("code", sorted(PRODUCT_INFRASTRUCTURE_CODES))
def test_classify_keeps_every_product_infrastructure_code(code):
    outcome = classify(primary_metric=PRIMARY, cleanup="not_needed", infrastructure_code=code)
    assert outcome == ProbeOutcome(U, None, None, None, code)


def test_probe_outcome_is_frozen():
    outcome = ProbeOutcome("compatible", None, None, None, None)
    with pytest.raises(FrozenInstanceError):
        outcome.status = "connection_incompatible"  # type: ignore[misc]


def test_scorer_failure_from_error_copies_only_code_and_metric():
    error = ScoringError("free text", code="timeout")
    assert ScorerFailure.from_error(error) == ScorerFailure("timeout", None)
    assert [f.name for f in fields(ScorerFailure)] == ["code", "metric"]


# --- binding identity, seed and representative case (choices 4 and 6) -----------------

BINDING = {
    "epoch": 0,
    "target": "a" * 64,
    "environment": "b" * 64,
    "scorer": "c" * 64,
    "metric_contract": "d" * 64,
    "primary_metric": PRIMARY,
    "corpus_digest": "e" * 64,
    "case_id": "case-1",
    "case_index": 0,
}
CHAMPION = "f" * 64


def test_binding_payload_is_the_c41_field_list():
    assert PROBE_PROFILE == "connection_preflight/1"
    assert CONNECTION_BINDING_VERSION == "connection_binding/1"
    assert binding_payload(**BINDING) == {"version": "connection_binding/1", **BINDING}


def test_binding_digest_is_the_canonical_sha256_and_deterministic():
    expected = hashlib.sha256(
        canonical_json({"version": "connection_binding/1", **BINDING})
    ).hexdigest()
    assert binding_digest(**BINDING) == expected
    assert binding_digest(**dict(reversed(list(BINDING.items())))) == expected


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("epoch", 1),
        ("target", "1" * 64),
        ("environment", "2" * 64),
        ("scorer", "3" * 64),
        ("metric_contract", "4" * 64),
        ("primary_metric", "accuracy"),
        ("corpus_digest", "5" * 64),
        ("case_id", "case-2"),
        ("case_index", 1),
    ],
)
def test_each_binding_field_changes_the_digest_and_the_seed(name, changed):
    base = binding_digest(**BINDING)
    other = binding_digest(**{**BINDING, name: changed})
    assert other != base
    assert probe_seed(other, CHAMPION) != probe_seed(base, CHAMPION)


def test_a_champion_change_keeps_the_binding_and_changes_the_seed():
    digest = binding_digest(**BINDING)
    assert binding_digest(**BINDING) == digest
    assert probe_seed(digest, CHAMPION) != probe_seed(digest, "0" * 64)


def test_probe_seed_is_choice_6_and_fits_31_bits():
    digest = binding_digest(**BINDING)
    expected = (
        int.from_bytes(
            hashlib.sha256(
                canonical_json(
                    {
                        "version": "connection_preflight/1",
                        "binding": digest,
                        "candidate_digest": CHAMPION,
                    }
                )
            ).digest()[:4],
            "big",
        )
        & 0x7FFFFFFF
    )
    assert probe_seed(digest, CHAMPION) == expected
    assert probe_seed(digest, CHAMPION) == probe_seed(digest, CHAMPION)
    assert 0 <= expected <= 0x7FFFFFFF


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("epoch", -1),
        ("epoch", True),
        ("epoch", "0"),
        ("case_index", -1),
        ("case_index", False),
        ("target", ""),
        ("target", 3),
        ("primary_metric", "  "),
        ("case_id", ""),
    ],
)
def test_binding_payload_refuses_malformed_fields(name, bad):
    with pytest.raises(ValueError):
        binding_payload(**{**BINDING, name: bad})


@pytest.mark.parametrize(("binding", "champion"), [("", CHAMPION), ("x", ""), (1, CHAMPION)])
def test_probe_seed_refuses_blank_inputs(binding, champion):
    with pytest.raises(ValueError):
        probe_seed(binding, champion)


def case(case_id):
    return EvalCase(case_id, "g", "s", (), {}, {}, {})


def test_representative_case_is_the_first_case_at_index_0():
    corpus = (case("c-first"), case("c-second"))
    assert representative_case(corpus) == (corpus[0], 0)
    with pytest.raises(ValueError):
        representative_case(())


# --- A run secret in the instance description ------------------------------------

DESCRIPTION_SECRET = Diagnostic(
    "connection_description_secret", "scorer.allowed_env.JUDGE", "target.description"
)


def test_classify_a_description_secret_before_any_rollout():
    outcome = classify(
        primary_metric=PRIMARY, cleanup="released", description_secret=DESCRIPTION_SECRET
    )
    assert outcome == ProbeOutcome.incompatible(DESCRIPTION_SECRET)
    assert outcome.cause_code is None


def test_classify_a_cleanup_failure_and_infrastructure_win_over_a_description_secret():
    failed = classify(
        primary_metric=PRIMARY, cleanup="failed", description_secret=DESCRIPTION_SECRET
    )
    assert failed.status == "connection_preflight_unavailable"
    assert failed.cause_code == "cleanup_failed"
    down = classify(
        primary_metric=PRIMARY,
        cleanup="released",
        infrastructure_code="attestation_mismatch",
        description_secret=DESCRIPTION_SECRET,
    )
    assert down.cause_code == "attestation_mismatch"


def test_a_description_secret_carries_no_cause():
    with pytest.raises(ValueError):
        Diagnostic(
            "connection_description_secret", "scorer", "target.description", "output_invalid"
        )


def test_secret_path_names_a_secret_by_its_manifest_path_or_its_index():
    assert secret_path(0, ("scorer.allowed_env.A",)) == "scorer.allowed_env.A"
    assert secret_path(1, ("scorer.allowed_env.A",)) == "secrets[1]"
    assert secret_path(0, ()) == "secrets[0]"


@pytest.mark.parametrize(
    ("path", "secrets", "expected"),
    [
        ("scorer.allowed_env.JUDGE_MODEL", ("judge-model-x",), "scorer.allowed_env.JUDGE_MODEL"),
        ("scorer.allowed_env.JUDGE_MODEL", ("JUDGE_MODEL",), "scorer.allowed_env"),
        ("grouper.allowed_env.GROUP_TOKEN", ("GROUP_TOKEN",), "grouper.allowed_env"),
        ("secrets[3]", ("secrets[3]",), "secrets"),
        ("editor.api_key_env", ("api_key_env",), "editor"),
    ],
)
def test_journalable_path_falls_back_to_the_role_when_its_path_matches(path, secrets, expected):
    assert journalable_path(path, secrets) == expected


def test_description_secret_uses_the_journal_predicate_and_the_role_order():
    secrets = ("judge-model-x", "editor-key-0123")
    paths = ("scorer.allowed_env.JUDGE", "editor.api_key_env")
    assert description_secret(None, secrets, paths) is None
    assert description_secret({"model": "a-hash"}, secrets, paths) is None
    found = description_secret({"key": "editor-key-0123", "model": "judge-model-x"}, secrets, paths)
    assert found == (
        Diagnostic(
            "connection_description_secret", "scorer.allowed_env.JUDGE", "target.description"
        ),
        # The key path of the named secret, not of the first match walked.
        "description.model",
    )
    # A substring at any depth matches, as the journal's own refusal does.
    found = description_secret({"a": [{"b": "x editor-key-0123 y"}]}, secrets, paths)
    assert found is not None and found[1] == "description.a[0].b"
    assert found[0].manifest_path == "editor.api_key_env"


def test_description_secret_names_the_key_of_the_secret_it_names():
    """Two secrets, the later one walked first. The
    diagnostic names the first secret in role order, so the key path is that
    secret's, never the other's."""

    secrets = ("aaaa-judge-value", "bbbb-editor-value")
    paths = ("scorer.allowed_env.JUDGE", "editor.api_key_env")
    found = description_secret({"alpha": secrets[1], "beta": secrets[0]}, secrets, paths)
    assert found == (
        Diagnostic(
            "connection_description_secret", "scorer.allowed_env.JUDGE", "target.description"
        ),
        "description.beta",
    )
    # A key collision of the named secret stays opaque.
    found = description_secret({"alpha": secrets[1], secrets[0]: "x"}, secrets, paths)
    assert found is not None and found[1] == "record-key"
    assert found[0].manifest_path == "scorer.allowed_env.JUDGE"


#: A `chat` role's key and proxy, the key's name sorting
#: after the proxy's (ADR-0029 amendment 2026-10-21: names sorted within a role).
SORTED_SECRETS = ("sk-z-auth-value-01", "https://a-proxy.example.test:8080")
SORTED_NAMES = ("Z_AUTH", "A_PROXY")


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_description_secret_names_the_first_name_sorted_within_a_role(role):
    paths = (f"{role}.api_key_env", f"{role}.proxy_env")
    description = {"key": SORTED_SECRETS[0], "proxy": SORTED_SECRETS[1]}
    found = description_secret(description, SORTED_SECRETS, paths, SORTED_NAMES)
    assert found == (
        Diagnostic("connection_description_secret", f"{role}.proxy_env", "target.description"),
        # The location is the chosen secret's alone.
        "description.proxy",
    )
    # Only the key matches: it is named.
    found = description_secret({"key": SORTED_SECRETS[0]}, SORTED_SECRETS, paths, SORTED_NAMES)
    assert found is not None and found[0].manifest_path == f"{role}.api_key_env"


def test_description_secret_orders_by_role_then_name_then_unnamed_then_index():
    secrets = (
        "grouper-value-0001",
        "editor-bare-value-1",
        "editor-named-value",
        "reflector-value-01",
        "library-value-0001",
    )
    paths = ("grouper.allowed_env.A", "editor", "editor.allowed_env.Z", "reflector.api_key_env")
    names = ("A", None, "Z", "B")
    every = {f"k{i}": secret for i, secret in enumerate(secrets)}

    def named(description):
        found = description_secret(description, secrets, paths, names)
        assert found is not None
        return found[0].manifest_path, found[1]

    # The Editor before the Reflector and the Grouper, whatever their index;
    # within the Editor a named secret before the bare role's value.
    assert named(every) == ("editor.allowed_env.Z", "description.k2")
    del every["k2"]
    assert named(every) == ("editor", "description.k1")
    del every["k1"]
    assert named(every) == ("reflector.api_key_env", "description.k3")
    del every["k3"]
    assert named(every) == ("grouper.allowed_env.A", "description.k0")
    # A library secret with no manifest path sorts after every role.
    del every["k0"]
    assert named(every) == ("secrets[4]", "description.k4")
