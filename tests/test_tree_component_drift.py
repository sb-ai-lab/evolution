"""The component-drift rule (G2b3b): references and when drift suspends."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evolution.tree.affected_cases import SampleProvenance
from evolution.tree.component_drift import (
    DriftReference,
    candidate_reference,
    checked_selection,
    drift_suspends,
    parent_reference,
)
from evolution.tree.execution.journal_schema import components_digest

MAP = {"catalog": {"sha256": "a" * 64, "sources": []}}
DIGEST = components_digest(MAP)
OTHER = "f" * 64


def _attempt(selection, *, inspected=MAP, epoch=1):
    return SimpleNamespace(
        selection=selection,
        candidate_digest="cand",
        parent_digest="par",
        inspected_components=inspected,
        inspected_epoch=epoch,
    )


def _selection(mode="component", fallback=None):
    return {"mode": mode, "fallback_reason": fallback}


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        (_selection(), True),
        (_selection("path"), False),
        (_selection(fallback="components_unstable"), False),
        (_selection(fallback="unaccounted_path"), False),
        (None, False),
    ],
)
def test_checked_selection(selection, expected):
    assert checked_selection(_attempt(selection)) is expected


def test_candidate_reference_is_the_inspected_map_in_its_epoch():
    assert candidate_reference(_attempt(_selection())) == DriftReference(
        "candidate", "cand", DIGEST, 1
    )


def test_candidate_reference_is_none_unless_checked():
    assert candidate_reference(_attempt(_selection("path"))) is None
    assert candidate_reference(_attempt(None)) is None


def _baseline(digest):
    return {"c1": (SampleProvenance(("catalog",), digest, True),)}


def _snapshot(**epochs):
    return SimpleNamespace(measurements={mid: {"epoch": e} for mid, e in epochs.items()})


def test_a_parent_map_declared_in_epochs_0_and_1_is_referenced_by_epoch_1():
    snapshot = _snapshot(m0=0, m1=1)
    declaring = {DIGEST: ("m0", "m1")}
    assert parent_reference(
        snapshot, _attempt(_selection()), _baseline(DIGEST), declaring
    ) == DriftReference("parent", "par", DIGEST, 1)


def test_parent_reference_with_a_single_declaring_measurement():
    snapshot = _snapshot(m0=0)
    reference = parent_reference(
        snapshot, _attempt(_selection()), _baseline(DIGEST), {DIGEST: ("m0",)}
    )
    assert reference is not None and reference.epoch == 0


def test_parent_reference_is_none_unless_checked():
    snapshot = _snapshot(m0=0)
    declaring = {DIGEST: ("m0",)}
    assert parent_reference(snapshot, _attempt(None), _baseline(DIGEST), declaring) is None
    assert (
        parent_reference(snapshot, _attempt(_selection("path")), _baseline(None), declaring) is None
    )


def test_drift_suspends_in_the_reference_epoch_or_on_a_map_already_rendered():
    reference = DriftReference("candidate", "cand", DIGEST, 1)
    assert drift_suspends(reference, 1, ())
    assert drift_suspends(reference, 2, {DIGEST})
    assert not drift_suspends(reference, 2, {OTHER})
    assert not drift_suspends(reference, 2, ())


# -- the Champion cohort member's round-level reference (F7 plan choice 10) -------


def _declared(digest):
    return SimpleNamespace(
        event="components_declared", payload={"components_digest": digest, "components": {}}
    )


def _round_snapshot(**epochs):
    cohort = {"champion": {"version": 0, "digest": "champ"}}
    return SimpleNamespace(
        run_id="run-1",
        family_version=14,
        rounds={2: SimpleNamespace(cohort=cohort)},
        measurements={mid: {"epoch": e} for mid, e in epochs.items()},
    )


def _champion_reference(monkeypatch, baseline, records, sources=None, **epochs):
    """`champion_reference` over round 2's baseline `m-base`, whose rows are
    `baseline` (`baseline_provenance` replays real records; the rule under
    test is what it does with the rows) and whose journal is `records`."""

    import evolution.tree.component_drift as module

    monkeypatch.setattr(module, "baseline_provenance", lambda _records: baseline)
    return module.champion_reference(
        _round_snapshot(**epochs), 2, "m-base", records, (sources or {}).get
    )


def test_the_champion_reference_is_the_shared_baseline_map_in_its_latest_epoch(monkeypatch):
    from evolution.tree.affected_cases import SampleProvenance

    baseline = {
        "c1": (SampleProvenance(("catalog",), DIGEST, True),),
        "c2": (SampleProvenance(("catalog",), DIGEST, True, ("run-1", "m-src")),),
    }
    reference = _champion_reference(
        monkeypatch,
        baseline,
        [_declared(DIGEST)],
        {"m-src": [_declared(DIGEST)]},
        **{"m-base": 0, "m-src": 1},
    )
    assert reference == DriftReference("champion", "champ", DIGEST, 1)


def test_the_champion_reference_is_none_when_the_baseline_names_two_maps(monkeypatch):
    baseline = {"c1": _baseline(DIGEST)["c1"], "c2": _baseline(OTHER)["c1"]}
    records = [_declared(DIGEST), _declared(OTHER)]
    assert _champion_reference(monkeypatch, baseline, records, **{"m-base": 0}) is None


def test_the_champion_reference_is_none_when_the_baseline_declares_no_map(monkeypatch):
    assert _champion_reference(monkeypatch, _baseline(None), [], **{"m-base": 0}) is None
    assert _champion_reference(monkeypatch, {}, [], **{"m-base": 0}) is None


def test_the_champion_reference_is_none_when_no_measurement_declares_the_map(monkeypatch):
    """`components_unavailable`: no reference epoch (choice 10)."""

    assert _champion_reference(monkeypatch, _baseline(DIGEST), [], **{"m-base": 0}) is None
