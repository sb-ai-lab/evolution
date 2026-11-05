"""Focused immutable snapshot and promotion-gate tests."""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path
from statistics import fmean

import pytest

from evolution.tree.artifacts.snapshot import SnapshotStore, snapshot_digest
from evolution.tree.execution.candidate_integrity import SnapshotError
from evolution.tree.statistics import (
    GatePlan,
    ScoreSample,
    gate_at_look,
    group_weighted_score,
    repeat_scores,
)


def _sample(case_id: str, sample_id: str, score: float, group_id: str = "group") -> ScoreSample:
    seed = int(sample_id) if sample_id.isdecimal() else None
    return ScoreSample(
        case_id,
        sample_id,
        score,
        seed,
        seed,
        "boot",
        "deps",
        group_id,
        "stratum",
    )


def test_python_source_is_unchanged_by_candidate_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    artifact = source / "skill.py"
    artifact.write_bytes(b"print('source')\n")
    os.chmod(artifact, 0o755)
    before = snapshot_digest(source)
    store = SnapshotStore(tmp_path / "run")
    parent = store.initialize(source)
    candidate = store.candidate(parent, "attempt-1")
    assert parent.root.stat().st_mode & 0o222 == 0
    assert candidate.root.stat().st_mode & 0o200
    (candidate.root / "skill.py").write_bytes(b"print('candidate')\n")
    candidate = store.seal(candidate)
    promoted = store.promote(candidate, "v1")

    assert snapshot_digest(source) == before
    assert (promoted.root / "skill.py").read_bytes() == b"print('candidate')\n"
    assert promoted.root.stat().st_mode & 0o222 == 0
    assert not (tmp_path / "run" / "current_version.json").exists()


def test_snapshot_rejects_a_run_directory_inside_its_source(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "skill.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must not overlap"):
        SnapshotStore(source / "run").initialize(source)


def test_initial_snapshot_cannot_resume_with_a_different_seed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "skill.py").write_text("first\n", encoding="utf-8")
    (second / "skill.py").write_text("second\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "run")

    store.initialize(first)

    with pytest.raises(SnapshotError, match="differs from seed"):
        store.initialize(second)


@pytest.mark.parametrize("snapshot_id", ["../escape", "/escape", ".hidden", ""])
def test_snapshot_store_rejects_unsafe_destination_ids(tmp_path: Path, snapshot_id: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "skill.py").write_text("source\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "run")
    parent = store.initialize(source)

    with pytest.raises(SnapshotError, match="invalid snapshot ID"):
        store.candidate(parent, snapshot_id)


def test_snapshot_store_rejects_a_symlinked_destination_parent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "skill.py").write_text("source\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "versions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SnapshotError, match="destination parent is unsafe"):
        SnapshotStore(run).initialize(source)

    assert not list(outside.iterdir())


def test_exact_gate_pairs_shuffled_samples_and_rejects_missing_identity() -> None:
    parent = [_sample("case", str(index), 0.0) for index in range(10)]
    candidate = [_sample("case", str(index), 1.0) for index in reversed(range(10))]
    plan = GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200)

    assert gate_at_look(parent, candidate, plan, 10).decision == "promote"
    with pytest.raises(ValueError, match="matching sample identities"):
        gate_at_look(parent, candidate[:-1] + [_sample("case", "missing", 1.0)], plan, 10)


def test_exact_gate_requires_seed_and_service_proof() -> None:
    parent = [
        ScoreSample("case", str(index), 0.0, group_id="group", stratum="stratum")
        for index in range(10)
    ]
    candidate = [
        ScoreSample("case", str(index), 1.0, group_id="group", stratum="stratum")
        for index in range(10)
    ]

    with pytest.raises(ValueError, match="matching effective seeds"):
        gate_at_look(parent, candidate, GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200), 10)


def test_unpaired_gate_accepts_independent_sample_identities() -> None:
    parent = [_sample("case", f"parent-{index}", 0.0) for index in range(10)]
    candidate = [_sample("case", f"candidate-{index}", 1.0) for index in range(10)]

    assert (
        gate_at_look(
            parent, candidate, GatePlan("unpaired", 3, {10: Fraction("0.05")}, draws=200), 10
        ).decision
        == "promote"
    )


def test_none_seed_mode_takes_the_group_aware_unpaired_path() -> None:
    parent = [_sample("case", f"parent-{index}", 0.0) for index in range(10)]
    candidate = [_sample("case", f"candidate-{index}", 1.0) for index in range(10)]

    assert (
        gate_at_look(
            parent, candidate, GatePlan("none", 3, {10: Fraction("0.05")}, draws=200), 10
        ).decision
        == "promote"
    )


def _paired(**overrides: object) -> tuple[list[ScoreSample], list[ScoreSample]]:
    base: dict[str, object] = {
        "boot_id": "boot-left",
        "dependency_digest": "deps",
    }
    base.update(overrides)
    parent = [
        ScoreSample(
            "case",
            str(index),
            0.0,
            index,
            index,
            str(base["boot_id"]),
            str(base["dependency_digest"]),
            "group",
            "stratum",
        )
        for index in range(10)
    ]
    candidate = [
        ScoreSample(
            "case",
            str(index),
            1.0,
            index,
            index,
            "boot-right",
            str(base["dependency_digest"]),
            "group",
            "stratum",
        )
        for index in range(10)
    ]
    return parent, candidate


def test_exact_gate_accepts_paired_samples_with_different_boot_ids() -> None:
    parent, candidate = _paired()

    result = gate_at_look(
        parent, candidate, GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200), 10
    )

    assert result.decision == "promote"


def test_exact_gate_refuses_mismatched_dependency_digest() -> None:
    parent, candidate = _paired()
    candidate = [
        ScoreSample(
            sample.case_id,
            sample.sample_id,
            sample.score,
            sample.requested_seed,
            sample.effective_seed,
            sample.boot_id,
            "other-deps",
            sample.group_id,
            sample.stratum,
        )
        for sample in candidate
    ]

    with pytest.raises(ValueError, match="matching dependency digest"):
        gate_at_look(parent, candidate, GatePlan("exact", 3, {10: Fraction("0.05")}, draws=200), 10)


def test_gate_does_not_treat_repeats_as_independent_cases() -> None:
    parent = [
        _sample(case_id, f"{repeat:02d}", float(case_id == "loss"), case_id)
        for case_id in ("gain-a", "gain-b", "loss")
        for repeat in range(10)
    ]
    candidate = [
        _sample(case_id, f"{repeat:02d}", float(case_id != "loss"), case_id)
        for case_id in ("gain-a", "gain-b", "loss")
        for repeat in range(10)
    ]

    result = gate_at_look(
        parent,
        candidate,
        GatePlan("exact", 3, {10: Fraction("0.05")}, draws=2_000),
        10,
    )

    assert result.decision == "continue"


def test_repeat_scores_keep_the_spread_the_group_score_averages_away() -> None:
    samples = [_sample("a", str(repeat), score) for repeat, score in enumerate((0.2, 0.4, 0.9))] + [
        _sample("b", str(repeat), score) for repeat, score in enumerate((0.0, 1.0, 0.5))
    ]
    per_repeat = repeat_scores(samples, 3)

    assert per_repeat == [0.1, 0.7, 0.7]
    assert fmean(per_repeat) == pytest.approx(group_weighted_score(samples, 3))


def test_group_weighted_score_does_not_let_a_large_group_dominate() -> None:
    samples = [
        _sample(case_id, str(repeat), 0.0, "large")
        for case_id in ("large-a", "large-b", "large-c")
        for repeat in range(10)
    ] + [_sample("small", str(repeat), 1.0, "small") for repeat in range(10)]

    assert group_weighted_score(samples, 10) == 0.5
