import json

import pytest
from evolution_lab.context_report import (
    ContextReport,
    Measurement,
    Metric,
    ReportRow,
    Route,
    calculate_token_budget,
    render_table,
)


def route_window():
    return Measurement.exact(
        131_072, unit="tokens", provenance="configuration", source="route.context_window"
    )


def response_reserve(value):
    return Measurement.exact(
        value, unit="tokens", provenance="configuration", source="reflector.max_output_tokens"
    )


@pytest.mark.parametrize(
    ("reserve", "remaining", "status"),
    [(16_384, -1_990, "over_window"), (14_384, 10, "within_window"), (14_394, 0, "zero_headroom")],
)
def test_observed_provider_usage_gives_signed_token_headroom(reserve, remaining, status):
    input_tokens = Measurement.exact(
        116_678, unit="tokens", provenance="provider_usage", source="usage.prompt_tokens"
    )

    budget = calculate_token_budget(route_window(), input_tokens, response_reserve(reserve))

    assert budget.status == status
    assert (budget.headroom.lower, budget.headroom.upper) == (remaining, remaining)
    assert budget.headroom.provenance == "derived"
    assert budget.input_tokens.provenance == "provider_usage"


@pytest.mark.parametrize(
    ("bounds", "headroom", "status"),
    [
        ((116_660, 116_670), (18, 28), "within_window"),
        ((116_680, 116_690), (-2, 8), "uncertain"),
        ((116_680, 116_688), (0, 8), "uncertain"),
        ((116_688, 116_690), (-2, 0), "uncertain"),
        ((116_690, 116_700), (-12, -2), "over_window"),
    ],
)
def test_estimates_conserve_headroom_bounds(bounds, headroom, status):
    estimate = Measurement("tokens", "estimate", "route.tokenizer_estimate", *bounds)

    budget = calculate_token_budget(route_window(), estimate, response_reserve(14_384))

    assert budget.status == status
    assert (budget.headroom.lower, budget.headroom.upper) == headroom
    assert budget.input_tokens.source == "route.tokenizer_estimate"
    assert budget.input_tokens.provenance == "estimate"


@pytest.mark.parametrize("missing", ["window", "input", "reserve"])
def test_token_headroom_is_unknown_when_any_operand_is_missing(missing):
    operands = {
        "window": route_window(),
        "input": Measurement.exact(
            116_678, unit="tokens", provenance="provider_usage", source="usage.prompt_tokens"
        ),
        "reserve": response_reserve(14_384),
    }
    operands[missing] = Measurement.unknown(unit="tokens", source=f"missing_{missing}")

    budget = calculate_token_budget(operands["window"], operands["input"], operands["reserve"])

    assert budget.status == "unknown"
    assert budget.headroom.provenance == "unknown"
    assert budget.headroom.lower is None
    assert budget.headroom.upper is None
    assert f"missing_{missing}" in budget.headroom.source


def test_character_compaction_threshold_cannot_stand_in_for_input_tokens():
    threshold = Measurement.exact(
        200_000, unit="chars", provenance="configuration", source="editor.context_compact_chars"
    )

    with pytest.raises(ValueError, match="tokens"):
        calculate_token_budget(route_window(), threshold, response_reserve(14_384))


@pytest.mark.parametrize(
    ("provenance", "lower", "upper"),
    [
        ("unknown", 0, 0),
        ("estimate", None, None),
        ("estimate", 1, None),
        ("estimate", 2, 1),
        ("configuration", True, True),
    ],
)
def test_invalid_measurement_cannot_masquerade_as_a_numeric_range(provenance, lower, upper):
    with pytest.raises(ValueError):
        Measurement("tokens", provenance, "example", lower, upper)


def test_both_output_formats_show_the_same_stages_and_request_provenance():
    report = ContextReport(
        (
            ReportRow(
                "evidence",
                "observed",
                metrics=(
                    Metric(
                        "available",
                        Measurement.exact(
                            20_000, unit="chars", provenance="measured", source="episode.events"
                        ),
                    ),
                    Metric(
                        "selected",
                        Measurement.exact(
                            16_000, unit="chars", provenance="measured", source="evidence.page"
                        ),
                    ),
                    Metric(
                        "truncated",
                        Measurement.exact(
                            4_000, unit="chars", provenance="derived", source="available - selected"
                        ),
                    ),
                ),
            ),
            ReportRow(
                "request",
                "hypothetical",
                role="editor",
                turn=2,
                session="attempt-3",
                scenario="after_compaction",
                route=Route(
                    model="example/model",
                    upstream="example",
                    request_style="responses",
                    policy_version="test-policy",
                    policy_digest="aa55",
                    tokenizer="example-tokenizer",
                    tokenizer_source="route.tokenizer",
                ),
                budget=calculate_token_budget(
                    route_window(),
                    Measurement("tokens", "estimate", "route.tokenizer_estimate", 116_680, 116_690),
                    response_reserve(14_384),
                ),
            ),
            ReportRow("grouping", "skipped", role="grouper", reason="disabled by configuration"),
        ),
        prepared_digest="prepared-1",
        bundle_digest="bundle-1",
        core_version="2.0.0b20",
    )

    payload = report.as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema"] == "lab-context-report/1"
    assert payload["prepared_digest"] == "prepared-1"
    assert payload["bundle_digest"] == "bundle-1"
    assert payload["core_version"] == "2.0.0b20"
    evidence, editor, grouper = payload["rows"]
    assert [m["measurement"]["lower"] for m in evidence["metrics"]] == [20_000, 16_000, 4_000]
    assert (editor["basis"], editor["role"], editor["turn"], editor["scenario"]) == (
        "hypothetical",
        "editor",
        2,
        "after_compaction",
    )
    assert editor["budget"]["status"] == "uncertain"
    assert editor["budget"]["headroom"]["lower"] == -2
    assert editor["budget"]["headroom"]["upper"] == 8
    assert editor["route"]["request_style"] == "responses"
    assert editor["route"]["policy_digest"] == "aa55"
    assert editor["session"] == "attempt-3"
    assert grouper["reason"] == "disabled by configuration"
    assert set(editor) == {
        "stage",
        "basis",
        "role",
        "turn",
        "session",
        "scenario",
        "reason",
        "metrics",
        "budget",
        "route",
    }

    table = render_table(report)
    overview = table[: table.index("Stage")]
    for field in (
        "attempt-3",
        "turn 2",
        "after_compaction",
        "responses",
        "uncertain",
        "-2..+8 tokens",
    ):
        assert field in overview
    for field in (
        "evidence",
        "observed",
        "20000 chars",
        "16000 chars",
        "4000 chars",
        "evidence.page",
        "hypothetical",
        "editor",
        "after_compaction",
        "116680..116690 tokens",
        "14384 tokens",
        "131072 tokens",
        "-2..8 tokens",
        "uncertain",
        "estimate",
        "responses",
        "test-policy",
        "aa55",
        "attempt-3",
        "route.tokenizer_estimate",
        "route.tokenizer",
        "skipped",
        "disabled by configuration",
        "prepared-1",
        "bundle-1",
        "2.0.0b20",
    ):
        assert field in table
