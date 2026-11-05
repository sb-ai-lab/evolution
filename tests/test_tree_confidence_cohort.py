"""The cohort's pure producer (design §6.9, §6.13, D7; slice F5)."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

from evolution.tree.campaign_statistics import MissingMetric, SampleRow

GROUPS = {"c1": ("g1", "s"), "c2": ("g1", "s"), "c3": ("g2", "s")}
CORPUS = ("c1", "c2", "c3")


def _row(case_id, repeat, *, score=None, outcome="completed", exhausted=False, critical=True):
    scored = score is not None
    return SampleRow(
        case_id=case_id,
        sample_id=f"{repeat:02d}",
        repeat=repeat,
        requested_seed=None,
        effective_seed=None,
        boot_id="",
        dependency_digest="d" * 64,
        outcome=outcome,
        disposition="score" if scored else "indeterminate",
        metrics={"score": score} if scored else None,
        critical_pass=critical if scored else None,
        exhausted=exhausted,
    )


def _complete(scores):
    return tuple(
        _row(case_id, repeat, score=score)
        for case_id, values in scores.items()
        for repeat, score in enumerate(values, start=1)
    )


def test_infrastructure_owners_are_the_infrastructure_failed_terminals():
    from evolution.tree.campaign_schema import _THRESHOLD_TERMINAL
    from evolution.tree.confidence_cohort import INFRASTRUCTURE_OWNERS

    assert INFRASTRUCTURE_OWNERS == {
        owner
        for owner, (outcome, _) in _THRESHOLD_TERMINAL.items()
        if outcome == "infrastructure_failed"
    }


def test_a_complete_member_scores_the_group_weighted_mean():
    from evolution.tree.confidence_cohort import member_completion

    rows = _complete({"c1": [1.0, 0.5], "c2": [0.0, 0.0], "c3": [0.25, 0.75]})
    result = member_completion(rows, groups=GROUPS, corpus=CORPUS, primary="score", repeats=2)
    # g1: (0.75 + 0.0) / 2; g2: 0.5; groups equal.
    expected = (Fraction(3, 8) + Fraction(1, 2)) / 2
    assert (result.complete, result.missing, result.critical) == (True, 0, False)
    assert result.point == float(expected)


def test_the_member_point_difference_is_the_cohort_point_contrast():
    from evolution.tree.campaign_statistics import score_samples_of
    from evolution.tree.confidence_cohort import member_completion
    from evolution.tree.confidence_statistics import CaseEvidence, point_contrast, scope_weights

    finalist = _complete({"c1": [0.1, 0.7], "c2": [0.3, 0.9], "c3": [0.2, 0.2]})
    champion = _complete({"c1": [0.6, 0.6], "c2": [0.1, 0.4], "c3": [0.8, 0.0]})
    points = [
        Fraction(
            str(
                member_completion(
                    rows, groups=GROUPS, corpus=CORPUS, primary="score", repeats=2
                ).point
            )
        )
        for rows in (finalist, champion)
    ]
    samples = [score_samples_of(rows, GROUPS, "score", 2) for rows in (finalist, champion)]
    cases = [
        CaseEvidence(
            case_id,
            candidate=tuple(s for s in samples[0] if s.case_id == case_id),
            parent=tuple(s for s in samples[1] if s.case_id == case_id),
        )
        for case_id in CORPUS
    ]
    contrast = point_contrast(cases, scope_weights(list(CORPUS), GROUPS))
    assert abs((points[0] - points[1]) - contrast) < Fraction(1, 10**12)


def test_an_incomplete_finalist_has_no_point_and_counts_its_missing_pairs():
    from evolution.tree.confidence_cohort import member_completion

    rows = (
        *_complete({"c1": [1.0], "c2": [1.0]}),
        _row("c3", 1, outcome="time_budget", exhausted=True),
    )
    result = member_completion(rows, groups=GROUPS, corpus=CORPUS, primary="score", repeats=1)
    assert (result.complete, result.point, result.missing) == (False, None, 1)


def test_a_scored_critical_failure_is_reported():
    from evolution.tree.confidence_cohort import member_completion

    rows = (*_complete({"c1": [1.0], "c2": [1.0]}), _row("c3", 1, score=0.0, critical=False))
    result = member_completion(rows, groups=GROUPS, corpus=CORPUS, primary="score", repeats=1)
    assert result.critical is True and result.complete is True


def test_a_missing_primary_is_named():
    from evolution.tree.confidence_cohort import member_completion

    bad = replace(_row("c2", 1, score=1.0), metrics={"other": 1.0})
    rows = (*_complete({"c1": [1.0], "c3": [1.0]}), bad)
    result = member_completion(rows, groups=GROUPS, corpus=CORPUS, primary="score", repeats=1)
    assert result == MissingMetric("c2", "01")


def test_the_champion_names_every_exhaustion_and_a_finalist_only_infrastructure():
    from evolution.tree.confidence_cohort import Member, incomplete_rows

    champion = Member(
        "m-r01-cohort-champion",
        "champion",
        (
            _row("c2", 1, outcome="time_budget", exhausted=True),
            _row("c1", 1, score=1.0),
        ),
    )
    finalist = Member(
        "m-r01-cohort-a01",
        "finalist",
        (
            _row("c3", 1, outcome="time_budget", exhausted=True),
            _row("c1", 1, outcome="target_failed", exhausted=True),
            _row("c2", 1, outcome="target_failed", exhausted=False),
        ),
    )
    assert incomplete_rows([champion, finalist], CORPUS) == (
        {
            "measurement_id": "m-r01-cohort-champion",
            "case_id": "c2",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "rollout",
        },
        {
            "measurement_id": "m-r01-cohort-a01",
            "case_id": "c1",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "target",
        },
    )


def test_an_exhausted_unscored_row_is_owned_by_the_rollout():
    from evolution.tree.confidence_cohort import Member, incomplete_rows

    unscored = replace(_row("c1", 1, outcome="completed"), disposition="unscored", exhausted=True)
    champion = Member("m-r01-cohort-champion", "champion", (unscored,))
    finalist = Member("m-r01-cohort-a01", "finalist", (unscored,))
    assert incomplete_rows([champion, finalist], CORPUS) == (
        {
            "measurement_id": "m-r01-cohort-champion",
            "case_id": "c1",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "rollout",
        },
    )


# -- PDR-0022 / ADR-0027 (2026-10-05): a settled stage's gaps -----------------


def _cancelled(case_id, repeat):
    """A try the caller's token ended: `exclude`, owner `rollout`, never retried."""

    return replace(_row(case_id, repeat, outcome="cancelled"), disposition="exclude")


def _champion(*rows, declined=frozenset()):
    from evolution.tree.confidence_cohort import Member

    return Member("m-r01-cohort-champion", "champion", rows, frozenset(declined))


def _finalist(*rows, declined=frozenset()):
    from evolution.tree.confidence_cohort import Member

    return Member("m-r01-cohort-a01", "finalist", rows, frozenset(declined))


def test_a_cancelled_champion_row_is_listed_with_owner_rollout():
    from evolution.tree.confidence_cohort import incomplete_rows

    champion = _champion(_row("c1", 1, score=1.0), _cancelled("c2", 1))
    assert incomplete_rows([champion], CORPUS) == (
        {
            "measurement_id": "m-r01-cohort-champion",
            "case_id": "c2",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "rollout",
        },
    )


def test_incomplete_rows_carry_the_latest_try_ordinal():
    from evolution.tree.confidence_cohort import incomplete_rows

    # A Champion gap at ordinal 1 (its retry ran and was cancelled) and a
    # finalist target gap at ordinal 0 (declined).
    champion = _champion(replace(_cancelled("c1", 1), retry_ordinal=1))
    finalist = _finalist(_row("c2", 1, outcome="target_failed"), declined={("c2", "01", 0)})
    assert [r["retry_ordinal"] for r in incomplete_rows([champion, finalist], CORPUS)] == [1, 0]


def test_the_cohort_rows_digest_covers_the_ordinal():
    from evolution.tree.campaign_schema import cohort_rows_digest

    row = {
        "measurement_id": "m",
        "case_id": "c1",
        "sample_id": "01",
        "retry_ordinal": 0,
        "owner": "target",
    }
    assert cohort_rows_digest([row]) != cohort_rows_digest([{**row, "retry_ordinal": 1}])


def test_a_cancelled_finalist_row_is_not_listed():
    from evolution.tree.confidence_cohort import incomplete_rows

    finalist = _finalist(_row("c1", 1, score=1.0), _cancelled("c2", 1))
    assert incomplete_rows([finalist], CORPUS) == ()


def test_a_declined_infrastructure_retry_on_a_finalist_is_listed():
    from evolution.tree.confidence_cohort import incomplete_rows

    declined = _row("c2", 1, outcome="target_failed", exhausted=False)
    finalist = _finalist(_row("c1", 1, score=1.0), declined, declined={("c2", "01", 0)})
    assert incomplete_rows([finalist], CORPUS) == (
        {
            "measurement_id": "m-r01-cohort-a01",
            "case_id": "c2",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": "target",
        },
    )
    # The same try with its retry still owed is no gap: its stage is not settled.
    assert incomplete_rows([_finalist(declined)], CORPUS) == ()


def test_the_gap_predicate_reads_cancelled_exhausted_scored_and_owed_rows():
    from evolution.tree.confidence_cohort import cohort_gap, rollout_owned_gap

    cancelled = _cancelled("c1", 1)
    exhausted = _row("c1", 1, outcome="time_budget", exhausted=True)
    scored = _row("c1", 1, score=1.0)
    owed = _row("c1", 1, outcome="time_budget", exhausted=False)
    not_started = replace(_row("c1", 1, outcome=None), disposition="not_started")
    declined_target = _row("c1", 1, outcome="target_failed", exhausted=False)
    assert rollout_owned_gap(cancelled) and rollout_owned_gap(exhausted)
    assert not rollout_owned_gap(scored) and not rollout_owned_gap(owed)
    assert not rollout_owned_gap(not_started)
    # A declined try is a gap with its own owner: rollout-owned only for `rollout`.
    assert rollout_owned_gap(owed, {("c1", "01", 0)})
    assert cohort_gap(declined_target, {("c1", "01", 0)})
    assert not rollout_owned_gap(declined_target, {("c1", "01", 0)})
    # The declined key names one try: another ordinal's decline does not settle it.
    assert not cohort_gap(declined_target, {("c1", "01", 1)})
