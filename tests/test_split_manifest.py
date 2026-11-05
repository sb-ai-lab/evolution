"""Tests for evolution.splits.SplitManifest."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evolution.splits import LeakageError, SplitManifest, assert_no_leakage

FIXTURE = Path(__file__).parent / "fixtures" / "manifests" / "tiny-v1.json"


def _load() -> SplitManifest:
    return SplitManifest.load(FIXTURE)


def test_load_basic_metadata():
    m = _load()
    assert m.name == "tiny-v1"
    assert m.source == "test-fixture"
    assert m.seed == 42
    assert m.counts == {
        "train": 2,
        "val": 1,
        "test": 1,
        "eligible": 4,
        "all_repo": 4,
    }


def test_load_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        SplitManifest.load("/no/such/path/manifest.json")


def test_sha256_is_stable():
    m1 = _load()
    m2 = _load()
    assert m1.sha256 == m2.sha256
    expected = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert m1.sha256 == expected


def test_scope_skill_evolution_is_train_only():
    m = _load()
    assert m.tasks(scope="skill_evolution") == ["task-train-A", "task-train-B"]


def test_scope_dev_is_train_plus_val():
    m = _load()
    assert m.tasks(scope="dev") == [
        "task-train-A",
        "task-train-B",
        "task-val-C",
    ]


def test_scope_reporting_is_test_only():
    m = _load()
    assert m.tasks(scope="reporting") == ["task-test-D"]


def test_scope_all_returns_union():
    m = _load()
    assert m.tasks(scope="all") == [
        "task-test-D",
        "task-train-A",
        "task-train-B",
        "task-val-C",
    ]


def test_unknown_scope_raises_value_error():
    m = _load()
    with pytest.raises(ValueError):
        m.tasks(scope="bogus")


def test_assert_no_leakage_silent_for_matching_scope():
    m = _load()
    m.assert_no_leakage(["task-train-A"], scope="skill_evolution")
    m.assert_no_leakage(["task-test-D"], scope="reporting")
    m.assert_no_leakage(["task-train-A", "task-val-C"], scope="dev")


def test_assert_no_leakage_raises_when_test_in_evolution_scope():
    m = _load()
    with pytest.raises(LeakageError) as exc:
        m.assert_no_leakage(
            ["task-train-A", "task-test-D"],
            scope="skill_evolution",
            context="evolution loop",
        )
    msg = str(exc.value)
    assert "task-test-D" in msg
    assert "evolution loop" in msg
    assert "test=" in msg


def test_assert_no_leakage_classifies_unknown_ids():
    m = _load()
    with pytest.raises(LeakageError) as exc:
        m.assert_no_leakage(
            ["task-train-A", "not-in-manifest"],
            scope="skill_evolution",
        )
    assert "unknown=" in str(exc.value)


def test_free_function_form_works():
    m = _load()
    with pytest.raises(LeakageError):
        assert_no_leakage(m, ["task-test-D"], scope="skill_evolution")


def test_record_lookup():
    m = _load()
    rec = m.record("task-train-A")
    assert rec.split == "train"
    assert rec.skills == ("docx",)
    assert rec.modality == ("docx",)


def test_record_unknown_raises_keyerror():
    m = _load()
    with pytest.raises(KeyError):
        m.record("nope")


def test_summary_dict_shape():
    m = _load()
    s = m.summary()
    assert s["name"] == "tiny-v1"
    assert s["sha256"] == m.sha256
    assert s["counts"]["train"] == 2


def test_path_load_via_string(tmp_path: Path):
    # Round-trip via tmp_path to confirm str input works.
    target = tmp_path / "copy.json"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    m = SplitManifest.load(str(target))
    assert m.name == "tiny-v1"
    assert m.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()


def test_records_filtered_by_scope():
    m = _load()
    train_records = m.records(scope="skill_evolution")
    assert {r.id for r in train_records} == {"task-train-A", "task-train-B"}
    assert all(r.split == "train" for r in train_records)
