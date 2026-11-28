import json

import pytest
from evolution_lab.context_report import Route
from evolution_lab.context_scenario import (
    ContextScenarioError,
    load_context_scenario,
    observed_window,
)


@pytest.fixture
def scenario_file(tmp_path):
    def write(**changes):
        path = tmp_path / "scenario-private-path.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "lab-context-scenario/1",
                    "roles": {
                        "reflector": {
                            "model": "model-owner/example-chat",
                            "upstream": "mock-provider/hosted",
                            "source": "public route settings",
                            "styles": ["modern", "legacy"],
                            "window_tokens": 131_072,
                            "reserve_tokens": 14_384,
                            **changes,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    return write


def test_scenario_identifies_each_declared_route_and_request_style(scenario_file):
    parsed = load_context_scenario(scenario_file(tokenizer="example-tokenizer", page_chars=16_000))
    scenario = parsed.roles["reflector"]
    route = scenario.report_route("legacy")

    assert scenario.styles == ("modern", "legacy")
    assert (route.model, route.upstream) == ("model-owner/example-chat", "mock-provider/hosted")
    assert route.request_style == "legacy"
    assert route.tokenizer == "example-tokenizer"
    assert route.source == "public route settings"
    assert (scenario.reserve_tokens, scenario.page_chars) == (14_384, 16_000)


def test_estimate_uses_serialized_request_bytes_and_rounds_bounds_outward(scenario_file):
    scenario = load_context_scenario(scenario_file(bytes_per_token=[2.5, 3.5])).roles["reflector"]

    estimate = scenario.estimate_input(42)

    assert (estimate.lower, estimate.upper) == (12, 17)
    assert (estimate.unit, estimate.provenance) == ("tokens", "estimate")
    assert "bytes_per_token" in estimate.source
    assert "public route settings" in estimate.source


def test_tokenizer_label_alone_cannot_give_an_input_token_count(scenario_file):
    scenario = load_context_scenario(scenario_file(tokenizer="example-tokenizer")).roles[
        "reflector"
    ]

    measurement = scenario.estimate_input(42)

    assert measurement.provenance == "unknown"
    assert (measurement.lower, measurement.upper) == (None, None)
    assert "bytes_per_token" in measurement.source


def test_matching_observed_upstream_and_model_uses_explicit_window(scenario_file):
    scenario = load_context_scenario(scenario_file()).roles["reflector"]

    window = observed_window(
        Route(model="model-owner/example-chat", upstream="mock-provider/hosted"), scenario
    )

    assert (window.lower, window.upper) == (131_072, 131_072)
    assert window.provenance == "configuration"
    assert "public route settings" in window.source


@pytest.mark.parametrize(
    ("model", "upstream"),
    [
        ("client-alias", "mock-provider/hosted"),
        ("model-owner/example-chat", "different-provider"),
        ("model-owner/example-chat", None),
        (None, "mock-provider/hosted"),
    ],
)
def test_window_stays_unknown_under_a_mismatched_or_incomplete_observed_route(
    scenario_file, model, upstream
):
    scenario = load_context_scenario(scenario_file()).roles["reflector"]

    window = observed_window(Route(model=model, upstream=upstream), scenario)

    assert window.provenance == "unknown"
    assert window.lower is None
    assert "upstream" in window.source or "provider" in window.source


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ""},
        {"upstream": "https://user:secret@example.test/v1"},
        {"source": "secret text\nsecond line"},
        {"styles": []},
        {"styles": ["modern", "modern"]},
        {"styles": ["secret unsupported style"]},
        {"window_tokens": True},
        {"window_tokens": 0},
        {"reserve_tokens": -1},
        {"reserve_tokens": 0},
        {"styles": [["modern"]]},
        {"page_chars": 0},
        {"page_chars": 1_000_001},
        {"bytes_per_token": [3, 2]},
        {"bytes_per_token": [0, 3]},
        {"bytes_per_token": [True, 3]},
        {"bytes_per_token": [2, float("inf")]},
        {"unknown_secret_field": "raw private prompt"},
    ],
)
def test_malformed_scenario_rejects_before_any_private_value_is_reported(scenario_file, changes):
    path = scenario_file(**changes)

    with pytest.raises(ContextScenarioError) as raised:
        load_context_scenario(path)

    message = str(raised.value)
    for private in (str(path), "secret", "raw private prompt", "example.test"):
        assert private not in message


def test_duplicate_role_or_route_key_cannot_silently_override_metadata(scenario_file):
    path = scenario_file()
    path.write_text(path.read_text().replace('"styles":', '"window_tokens": 100, "styles":'))

    with pytest.raises(ContextScenarioError, match="duplicate"):
        load_context_scenario(path)


def test_missing_scenario_file_has_a_path_free_error(tmp_path):
    missing = tmp_path / "secret-scenario-path"

    with pytest.raises(ContextScenarioError, match="read") as raised:
        load_context_scenario(missing)

    assert str(missing) not in str(raised.value)
