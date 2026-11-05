from __future__ import annotations

from evolution.evolve.optimizer_validation import ValidationMeasurement, paired_schedule


def test_paired_schedule_shares_seeds_and_alternates_arm_order() -> None:
    schedule = paired_schedule(["val"], trials=4, base_seed=7, round_index=0)

    assert [pair.arms for pair in schedule] == [
        ("parent", "candidate"),
        ("candidate", "parent"),
        ("parent", "candidate"),
        ("candidate", "parent"),
    ]
    assert len({pair.seed for pair in schedule}) == 4


def test_validation_measurement_persists_scalars_only() -> None:
    measurement = ValidationMeasurement(seed=3, passed=2, total=4, pass_rate=0.5)

    assert measurement.to_dict() == {
        "seed": 3,
        "passed": 2,
        "total": 4,
        "pass_rate": 0.5,
    }
