"""`reflect_evidence`'s pure grouping pipeline: repeats ->
cohorts -> clusters -> focus -> portion. `project_repeats`'s ordinary path is
covered in `tests/tree/roles/editor/test_editor_evidence.py`, alongside
`project_observations`, so both share the same trace fixture; this module
also reuses that fixture for one `project_repeats` robustness test (a
pathologically nested `public_feedback` value) that belongs with the rest of
the grouping pipeline's failure-handling tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution.tree.campaign_schema import CampaignSnapshot, RoundState
from evolution.tree.roles.editor_contracts import ABSENT_OBSERVATION, UNAVAILABLE_OBSERVATION
from evolution.tree.roles.reflector.reflect_evidence import (
    RepeatEvidence,
    clusters_of,
    cohorts_of,
    policy_digest,
    portion_of,
    project_repeats,
    scale_buckets,
    select_focus,
)
from evolution.tree.roles.reflector_contracts import REFLECT_LIMITS_V1
from tests.campaign_fakes import PRIMARY
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import _nested_object, _reflect_row, _Trace

_SHA = "a" * 64


def rep(
    case,
    repeat,
    *,
    failed=(),
    outcome="completed",
    critical=None,
    events=4,
    trace="present",
    score=0.0,
    feedback=None,
    feedback_omitted=0,
    feedback_truncated=(),
):
    """One `RepeatEvidence`; `outcome` is an R2a sample terminal, `completed`
    being the healthy one (`sample_outcomes.Terminal`)."""

    verdicts = tuple({"criterion": c, "passed": False, "note": None} for c in failed)
    return RepeatEvidence(
        case,
        repeat,
        f"{case}-{repeat}",
        outcome,
        critical,
        score,
        trace,
        verdicts,
        feedback or {},
        feedback_omitted,
        tuple(feedback_truncated),
        UNAVAILABLE_OBSERVATION if trace == "unavailable" else ABSENT_OBSERVATION,
        events,
        None if trace == "unavailable" else tuple(sorted(failed)),
    )


def _snapshot(rounds: dict[int, RoundState]) -> CampaignSnapshot:
    """The bare fields `stalled_fingerprints`/`select_focus` read, built by
    direct dataclass construction (both `RoundState` and `CampaignSnapshot`
    have public fields, and this is the pattern `tests/tree/roles/editor/test_editor_evidence.py`
    already uses for the same two dataclasses) -- no executor involved."""

    return CampaignSnapshot(
        family_version=14,
        identity={},
        run_id="run",
        alpha_plan={},
        rounds=rounds,
        champion=("v0", _SHA),
        champion_dependency_digest="dep",
        measurements={},
        accounted=frozenset(),
        counts={},
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=len(rounds),
    )


@pytest.fixture
def empty_snapshot() -> CampaignSnapshot:
    return _snapshot({})


@pytest.fixture
def snapshot_with_stalled():
    """`snapshot_with_stalled(fingerprints)`: a round 1 that promoted (clearing
    any earlier stall), followed by one `stalled` round per fingerprint, each
    carrying that fingerprint as its focus -- exactly the shape
    `stalled_fingerprints` walks."""

    def build(fingerprints: tuple[str, ...]) -> CampaignSnapshot:
        rounds: dict[int, RoundState] = {
            1: RoundState(
                round=1, closed={"outcome": "promoted"}, focus={"focus_fingerprint": None}
            )
        }
        for offset, fingerprint in enumerate(fingerprints, start=2):
            rounds[offset] = RoundState(
                round=offset,
                closed={"outcome": "stalled"},
                focus={"focus_fingerprint": fingerprint},
            )
        return _snapshot(rounds)

    return build


def test_failing_rule_follows_the_pdr():
    assert rep("a", 1, failed=("x",)).failing
    assert rep("a", 1, critical=False).failing
    assert rep("a", 1, outcome="target_failed").failing
    assert rep("a", 1, outcome="target_failed", trace="unavailable").failing
    # `turn_budget` scores, but the Candidate ran out of turns: a failure mode.
    assert rep("a", 1, outcome="turn_budget").failing
    assert not rep("a", 1).failing
    assert not rep("a", 1, trace="unavailable").failing


def test_terciles_are_nearest_rank_over_present_repeats():
    """The contract's boundary sentence governs both boundaries: a count equal to
    the high boundary takes the lower bucket, so `[1..7]` is low {1,2,3}, mid
    {4,5}, high {6,7}."""

    reps = [rep("a", i, events=n) for i, n in enumerate([1, 2, 3, 4, 5, 6, 7], start=1)]
    reps.append(rep("b", 1, trace="unavailable"))
    buckets = scale_buckets(reps)
    assert [buckets[("a", i)] for i in range(1, 8)] == [
        "low",
        "low",
        "low",
        "mid",
        "mid",
        "high",
        "high",
    ]
    assert buckets[("b", 1)] == "none"


def test_a_count_on_the_high_boundary_takes_the_lower_bucket():
    """`[1,2,3,4,4,5]`: `third` is 2, the high boundary count is 4, and both
    fours are `mid` rather than straddling into `high`."""

    reps = [rep("a", i, events=n) for i, n in enumerate([1, 2, 3, 4, 4, 5], start=1)]
    buckets = scale_buckets(reps)
    assert [buckets[("a", i)] for i in range(1, 7)] == ["low", "low", "mid", "mid", "mid", "high"]


def test_cohorts_and_clusters_are_canonically_ordered():
    reps = [
        rep("b", 1, failed=("x",)),
        rep("a", 1, failed=("x",)),
        rep("a", 2, failed=("x",)),
        rep("a", 3, failed=("y",)),
        rep("c", 1, outcome="target_failed", trace="unavailable"),
    ]
    cohorts = cohorts_of(reps)
    assert [(c.cohort_id, c.case_id, c.failed_criteria, c.mass) for c in cohorts] == [
        ("c001", "a", ("x",), 2),
        ("c002", "a", ("y",), 1),
        ("c003", "b", ("x",), 1),
        ("c004", "c", None, 1),
    ]
    clusters = clusters_of(cohorts)
    assert [(cl.case_ids, cl.mass) for cl in clusters][0] == (("a", "b"), 3)
    assert clusters[0].fingerprint != clusters[1].fingerprint


def test_verdictless_failures_cluster_per_case_not_together():
    """The contract: "without verdicts a cluster is one case's cohorts". A present
    trace that recorded no verdict at all has `failed_criteria ==`, not
    `None`; keying that as a shared failure mode would merge every terminal
    failure in the corpus into one cluster."""

    reps = [
        rep("a", 1, outcome="target_failed"),
        rep("b", 1, outcome="target_failed"),
        rep("c", 1, outcome="target_failed", trace="unavailable"),
    ]
    cohorts = cohorts_of(reps)
    assert [(c.case_id, c.failed_criteria) for c in cohorts] == [
        ("a", ()),
        ("b", ()),
        ("c", None),
    ]
    clusters = clusters_of(cohorts)
    # Equal mass, so the canonical order is by fingerprint, not by case.
    assert sorted(cl.case_ids for cl in clusters) == [("a",), ("b",), ("c",)]
    assert len({cl.fingerprint for cl in clusters}) == 3


def test_focus_skips_stalled_and_reports_exhausted(snapshot_with_stalled):
    reps = [rep("a", 1, failed=("x",)), rep("a", 2, failed=("x",)), rep("b", 1, failed=("y",))]
    first = select_focus(reps, snapshot=snapshot_with_stalled(()))
    assert first.reason == "mass" and first.focus.case_ids == ("a",)
    second = select_focus(reps, snapshot=snapshot_with_stalled((first.focus.fingerprint,)))
    assert second.focus.case_ids == ("b",) and second.reason == "mass"
    both = select_focus(
        reps, snapshot=snapshot_with_stalled((first.focus.fingerprint, second.focus.fingerprint))
    )
    assert both.reason == "exhausted" and both.focus.fingerprint == first.focus.fingerprint


def test_no_failure_is_reason_none_with_unavailable_count(empty_snapshot):
    selection = select_focus(
        [rep("a", 1), rep("a", 2, trace="unavailable")], snapshot=empty_snapshot
    )
    assert (selection.reason, selection.focus, selection.unavailable) == ("none", None, 1)
    payload = selection.to_payload()
    assert payload["focus_fingerprint"] is None and payload["clusters"] == []


def test_portion_exemplar_contrast_and_others(empty_snapshot):
    # Nine present repeats of one case, so nearest-rank thirds put 4/5/6 in
    # `mid` and 1/1/1, 9/9/9 in `low`/`high`: the three failing repeats share
    # one bucket, so `cohorts_of` keeps them in one cohort even with `scale`
    # in its equivalence key.
    reps = [
        rep("a", 1, failed=("x",), events=4),
        rep("a", 2, failed=("x",), events=5),
        rep("a", 3, failed=("x",), events=6),
        rep("a", 4, events=1, score=1.0),
        rep("a", 5, events=1, score=1.0),
        rep("a", 6, events=1, score=1.0),
        rep("a", 7, events=9, score=1.0),
        rep("a", 8, events=9, score=1.0),
        rep("a", 9, events=9, score=1.0),
    ]
    focus = select_focus(reps, snapshot=empty_snapshot).focus
    (portion,) = portion_of(focus, reps)
    assert portion.exemplar.repeat == 2  # nearest the median event count 5
    assert portion.contrast.repeat == 4  # the passing repeat with the lowest index
    assert [r.repeat for r in portion.others] == [1, 3]


def test_portion_with_no_present_member_has_no_exemplar_or_contrast(empty_snapshot):
    reps = [
        rep("a", 1, outcome="target_failed", trace="unavailable"),
        rep("a", 2, outcome="target_failed", trace="unavailable"),
    ]
    focus = select_focus(reps, snapshot=empty_snapshot).focus
    (portion,) = portion_of(focus, reps)
    assert portion.exemplar is None
    assert portion.contrast is None
    assert [r.repeat for r in portion.others] == [1, 2]


def test_repeats_of_one_case_in_different_buckets_form_two_cohorts():
    """Two present repeats: `third` is 1, so the low boundary is the smaller
    count and the high boundary is the larger one -- and a count *on* the high
    boundary takes the lower bucket, so the split is low/mid rather
    than low/high. What this test pins is the split itself: one case's one
    failure mode in two scale buckets is two cohorts."""

    reps = [rep("a", 1, failed=("x",), events=1), rep("a", 2, failed=("x",), events=100)]
    cohorts = cohorts_of(reps)
    assert len(cohorts) == 2
    assert {cohort.scale for cohort in cohorts} == {"low", "mid"}
    assert all(cohort.case_id == "a" and cohort.failed_criteria == ("x",) for cohort in cohorts)
    assert all(cohort.mass == 1 for cohort in cohorts)


def test_portion_is_capped_at_sixteen_cohorts(empty_snapshot):
    reps = [rep(f"c{i:02d}", 1, failed=("x",)) for i in range(20)]
    focus = select_focus(reps, snapshot=empty_snapshot).focus
    assert len(portion_of(focus, reps)) == 16


def test_project_repeats_a_pathologically_nested_feedback_value_is_unavailable(
    tmp_path: Path,
) -> None:
    """A `RecursionError` anywhere between reading `stdout` and building the
    Reflector's feedback mapping must still leave the repeat `unavailable`,
    never raise out of `project_repeats`; the feedback projection runs inside
    the trace-read guard."""

    nested = _nested_object()
    trace = _Trace(tmp_path, turns=1)
    stdout = (
        '{"metrics":{"accuracy":1.0},"verdicts":[],'
        f'"public_feedback":{{"deep":{nested}}},"details":{{}}}}'
    )
    (trace.directory / "stdout").write_text(stdout, encoding="utf-8")
    row = _reflect_row("s1", 1)
    try:
        repeats = project_repeats(
            rows=(row,),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
    finally:
        trace.close()
    (repeat,) = repeats
    assert repeat.trace == "unavailable"
    assert repeat.verdicts == () and repeat.feedback == {} and repeat.failed_criteria is None
    assert repeat.event_count == 0
    assert repeat.feedback_omitted == 0 and repeat.feedback_truncated == ()


def _projected_feedback(tmp_path: Path, mapping: dict) -> RepeatEvidence:
    """One `RepeatEvidence` whose trace published `mapping` as `public_feedback`."""

    trace = _Trace(tmp_path, turns=1)
    stdout = {
        "metrics": {"accuracy": 1.0},
        "verdicts": [],
        "public_feedback": mapping,
        "details": {},
    }
    (trace.directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
    try:
        repeats = project_repeats(
            rows=(_reflect_row("s1", 1),),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
    finally:
        trace.close()
    (repeat,) = repeats
    assert repeat.trace == "present"
    return repeat


def test_feedback_values_keep_their_own_json_types(tmp_path: Path) -> None:
    """PDR-0007 as amended: every value in its own JSON type. A boolean stays a
    boolean, `null` stays null, and a nested object or array passes through
    unchanged -- none of them becomes the JSON *string* of itself, which is
    an incomplete feedback projection."""

    mapping = {
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "absent": None,
        "nested": {"a": [1, 2], "b": {"c": False}},
        "list": ["x", "y"],
        "note": "the agent read the wrong file",
    }
    repeat = _projected_feedback(tmp_path, mapping)
    assert repeat.feedback == mapping
    assert repeat.feedback["flag"] is True and repeat.feedback["absent"] is None
    assert repeat.feedback["nested"] == {"a": [1, 2], "b": {"c": False}}
    assert repeat.feedback_omitted == 0 and repeat.feedback_truncated == ()


def test_feedback_keys_past_the_published_count_are_dropped_and_counted(tmp_path: Path) -> None:
    """Seventeen keys under a published `feedback_keys` of sixteen: the first
    sixteen in sorted-key order survive and the drop is reported rather than
    silent."""

    keys = REFLECT_LIMITS_V1["feedback_keys"]
    mapping = {f"k{i:02d}": i for i in range(keys + 1)}
    repeat = _projected_feedback(tmp_path, mapping)
    assert len(repeat.feedback) == keys
    assert sorted(repeat.feedback) == sorted(mapping)[:keys]
    assert f"k{keys:02d}" not in repeat.feedback
    assert repeat.feedback_omitted == 1 and repeat.feedback_truncated == ()


def test_a_long_feedback_string_is_cut_and_its_key_flagged(tmp_path: Path) -> None:
    """Only a string value is cut, at `feedback_value_chars * 8`, and the key
    it belongs to is named in `feedback_truncated`; a short string beside it is
    untouched and unflagged."""

    limit = REFLECT_LIMITS_V1["feedback_value_chars"] * 8
    mapping = {"long": "x" * (limit + 40), "short": "ok"}
    repeat = _projected_feedback(tmp_path, mapping)
    assert repeat.feedback["long"] == "x" * limit
    assert repeat.feedback["short"] == "ok"
    assert repeat.feedback_truncated == ("long",) and repeat.feedback_omitted == 0


def test_policy_digest_is_stable():
    assert policy_digest() == policy_digest() and len(policy_digest()) == 64


def test_a_carried_repeat_projects_as_unavailable_with_its_score(tmp_path: Path) -> None:
    """Ruling R9, PDR-0013 known limitation: a carried (`sample_imported`) repeat
    has no scoring record in the successor -- its trace stays in the
    predecessor -- so the Reflector sees it `unavailable`, keeping its outcome
    and primary score, and the projection neither crashes nor drops it."""

    from dataclasses import replace

    trace = _Trace(tmp_path, turns=1)
    stdout = {"metrics": {"accuracy": 1.0}, "verdicts": [], "public_feedback": {}, "details": {}}
    (trace.directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
    carried = replace(_reflect_row("s2", 2), imported=True)
    try:
        repeats = project_repeats(
            rows=(_reflect_row("s1", 1), carried),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
    finally:
        trace.close()
    measured, imported = repeats
    assert measured.trace == "present"
    assert (imported.sample_id, imported.trace) == ("s2", "unavailable")
    assert imported.outcome == "completed" and imported.primary_score == 1.0
    assert imported.verdicts == () and imported.event_count == 0
