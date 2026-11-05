"""Family 14 H3 envelope boundaries and explicit real-prefix fixtures."""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import pytest

from evolution.tree.campaign_schema import SchemaError, campaign_family_for
from tests.campaign_drivers import observe_barrier  # noqa: F401


def _life(records, **settings):
    """Lifecycle-only fixture; its fresh synthetic calibration is not row evidence."""
    from tests.campaign_fakes import Family14Life

    life = Family14Life(records, transcode=True, **settings)
    attempt = life.attempt(life.sealed)
    if attempt.calibration is not None and attempt.calibration_result is None:
        life.accept(
            "noise_calibration_completed", **life.calibration_completed_payload(life.sealed)
        )
    return life


def _before_look(records):
    stop = next(
        i
        for i, row in enumerate(records)
        if row["event"] in {"noise_calibration_completed", "comparison_look_evaluated"}
    )
    return records[:stop]


def _closed_life(records):
    """Re-use only the real pre-look prefix; every family-14 look/close is newly admitted."""
    life = _life(_before_look(records))
    attempt = life.sealed
    life.accept("comparison_look_evaluated", **life.look_payload(attempt, 1))
    life.accept(
        "attempt_closed",
        attempt_id=attempt,
        outcome="finalist",
        reason="observe_finalist",
        look=1,
        code=None,
    )
    return life


def test_explicit_fixture_admits_real_prefix_and_family14_pass(observe_barrier):
    life = _closed_life(observe_barrier)
    assert life.snapshot.family_version == 14
    assert life.state.policy.stability == "unstable"
    assert life.state.policy.round_stop == "all_attempts"
    assert life.attempt(life.sealed).outcome == "finalist"
    assert life.attempt(life.sealed).looks[1]["decision"] == "finalist"
    # Maintainer-approved native prefix: ADR-0032 creation is already family 14.
    assert observe_barrier[0]["payload"]["family_version"] == 14
    assert life.envelopes[0].payload["identity"]["policy_digest"] == life.state.policy.digest


def test_fixture_accepts_the_native_family_without_a_bridge(observe_barrier):
    from tests import campaign_fakes

    assert campaign_fakes.Family14Life(_before_look(observe_barrier)).snapshot.family_version == 14


def test_family14_document_builds_explicit_h3_settings(tmp_path, monkeypatch):
    from evolution.tree.evolve import admit_manifest
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2
    from tests import campaign_fakes
    from tests.campaign_drivers import v13_document
    from tests.tree.manifest.test_manifest_v2 import write_v5_document

    document = campaign_fakes.family14_document(v13_document(), gate={"mode": "observe"}, repeats=1)
    assert document["schema"] == "evolution-v2-manifest/15"
    assert document["campaign"]["gate"]["stability"] == "unstable"
    assert document["campaign"]["round_stop"] == "all_attempts"
    assert document["campaign"]["baseline_refresh_after_rounds"] == 0
    assert document["campaign"]["gate"]["focus"] == {"margin": 0}
    assert document["grouper"]["kind"] == "fields"
    assert "metric_contract" in document["scorer"] and "role_visibility" in document
    campaign_fakes.family14_admitted(monkeypatch)
    admit_manifest(load_manifest_v2(write_v5_document(tmp_path, document)))


@pytest.mark.parametrize(
    "event",
    [
        "cohort_measurement_bound",
        "cohort_measurement_completed",
        "connection_started",
        "grouping_suspended",
    ],
)
def test_family14_refuses_old_final_names_and_future_operations(event):
    with pytest.raises(SchemaError, match="event is not a campaign record"):
        campaign_family_for(14).validate_payload(event, {})


#: H10 (F0 §11, choice 7): the four connection-preflight records family 14 admits.
CONNECTION_RECORDS = (
    "connection_preflight_started",
    "connection_preflight_finished",
    "connection_preflight_failed",
    "connection_preflight_interrupted",
)


def test_family14_admits_the_connection_preflight_records():
    from evolution.tree.campaign_schema import _FINAL_KEYS

    for event in CONNECTION_RECORDS:
        assert event in _FINAL_KEYS
    # The H3 future-operation name stays unknown: the real records are the four.
    assert "connection_started" not in _FINAL_KEYS
    assert list(_FINAL_KEYS)[-len(CONNECTION_RECORDS) :] == list(CONNECTION_RECORDS)


def test_gate_pass_names_are_closed_by_family(observe_barrier):
    life = _life(_before_look(observe_barrier))
    payload = life.look_payload(life.sealed, 1)
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)
    payload["decision"] = "accepted"
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)


def test_family14_gate_estimates_require_exact_and_margin_is_bound(observe_barrier):
    life = _life(_before_look(observe_barrier))
    payload = life.look_payload(life.sealed, 1)
    assert payload["focus"]["exact"] is None
    assert payload["causal"]["exact"] is None
    assert "exact" not in payload["overall"]
    campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)
    del payload["focus"]["exact"]
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("comparison_look_evaluated", payload)
    payload = life.comparison_payload(life.sealed)
    payload["margin"] = 0
    campaign_family_for(14).validate_payload("comparison_bound", payload)


def test_fixture_bridge_refuses_completed_old_look(observe_barrier):
    from tests.campaign_fakes import Family14Life

    with pytest.raises(
        ValueError, match="family-14 fixture conversion stops before completed gate evidence"
    ):
        Family14Life(observe_barrier, transcode=True)
