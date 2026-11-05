"""The optimizer's promote/reject sequence must survive a refactor byte-for-byte."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.product import optimizer_decision_replay as replay


@pytest.fixture(scope="module")
def reference() -> dict[str, Any]:
    """One capture, reused: each capture already runs every scenario twice."""
    return replay.capture()


def _gate_reason(round_record: dict[str, Any]) -> str | None:
    """The gate's own reason, absent for a round the saturation preflight stopped."""
    validation = round_record.get("validation") or {}
    return (validation.get("telemetry") or {}).get("reason")


def _decision_path(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["status"],
        record["reason"],
        record["final_version"],
        tuple((entry["outcome"], _gate_reason(entry)) for entry in record["rounds"]),
    )


def test_capture_records_every_declared_scenario(reference: dict[str, Any]) -> None:
    assert set(reference["scenarios"]) == set(replay.SCENARIOS)


def test_scenarios_reach_distinct_decision_paths(reference: dict[str, Any]) -> None:
    """Six scenarios that all reach one branch would replay nothing worth replaying.

    The reason strings are part of the path on purpose. Without them ``no_gain``
    and ``regressed`` are the same tuple -- both keep v1 and both read
    ``rejected`` -- and the arm would pass while covering one branch less than it
    claims.
    """
    paths = {name: _decision_path(record) for name, record in reference["scenarios"].items()}
    assert len(set(paths.values())) == len(paths), f"scenarios collapsed onto one path: {paths}"


def test_the_declared_gate_reasons_are_actually_reached(reference: dict[str, Any]) -> None:
    reached = {
        _gate_reason(entry)
        for record in reference["scenarios"].values()
        for entry in record["rounds"]
    }
    assert {"paired_improvement", "task_regression", "no_observed_gain", "underpowered"} <= reached


def test_capture_is_reproducible_in_a_second_capture(reference: dict[str, Any]) -> None:
    assert replay.compare(reference, replay.capture()) == []


def test_a_changed_gate_reason_is_reported(reference: dict[str, Any]) -> None:
    mutated = copy.deepcopy(reference)
    mutated["scenarios"]["promoted"]["rounds"][0]["validation"]["telemetry"]["reason"] = "drifted"

    divergences = replay.compare(reference, mutated)

    assert divergences == [
        "changed: promoted.rounds[0].validation.telemetry.reason: 'paired_improvement' -> 'drifted'"
    ]


def test_a_changed_promoted_version_is_reported(reference: dict[str, Any]) -> None:
    mutated = copy.deepcopy(reference)
    mutated["scenarios"]["promoted"]["final_version"] = "v9"

    assert replay.compare(reference, mutated) == ["changed: promoted.final_version: 'v2' -> 'v9'"]


def test_a_dropped_round_is_reported(reference: dict[str, Any]) -> None:
    mutated = copy.deepcopy(reference)
    mutated["scenarios"]["two_rounds"]["rounds"].pop()

    divergences = replay.compare(reference, mutated)

    assert divergences
    assert all(line.startswith("removed: two_rounds.rounds[1]") for line in divergences)


def test_a_schema_change_is_reported_alone(reference: dict[str, Any]) -> None:
    """A schema bump must not be reported as thousands of field diffs."""
    mutated = copy.deepcopy(reference)
    mutated["schema"] = "something-else-v2"

    assert replay.compare(reference, mutated) == [
        f"schema: {replay.SCHEMA!r} -> 'something-else-v2'"
    ]


def test_the_volatile_round_id_is_redacted(reference: dict[str, Any]) -> None:
    ids = {
        round_record["round_id"]
        for record in reference["scenarios"].values()
        for round_record in record["rounds"]
    }
    assert ids == {"<redacted>"}


def test_the_execution_fakes_do_not_leak(reference: dict[str, Any]) -> None:
    """A leaked fake turns an unrelated later failure into a mystery."""
    from evolution.evolve import optimizer

    for name in ("_collect_one", "_reflect_round", "_rewrite_round", "_validate_one"):
        assert getattr(optimizer, name).__module__ == "evolution.evolve.optimizer"
