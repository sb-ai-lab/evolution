"""Stored final contrasts from durable sample rows (H5, R28/R33 and M2)."""

from dataclasses import replace

import pytest

from evolution.tree.campaign_statistics import MissingMetric, SampleRow
from evolution.tree.confidence_cohort import global_comparison_record

GROUPS = {"a": ("g1", "s"), "b": ("g1", "s"), "c": ("g2", "s"), "d": ("g3", "s")}
FINALIST = "r01-final-a01"
STORED = {"measurement_id": "baseline", "prefix_digest": "a" * 64}


def rows(scores, *, paired=True, dependency="d" * 64):
    return tuple(
        SampleRow(
            case_id=case,
            sample_id=f"sample-{repeat}",
            repeat=repeat,
            requested_seed=repeat if paired else None,
            effective_seed=repeat if paired else None,
            boot_id="",
            dependency_digest=dependency,
            outcome="completed",
            disposition="score",
            metrics={"score": score},
            critical_pass=True,
        )
        for case, values in scores.items()
        for repeat, score in enumerate(values, 1)
    )


def compare(
    finalist,
    champion,
    *,
    excluded=frozenset(),
    mode="statistical",
    global_mode="bound",
    repeats=3,
    complete=True,
    critical=False,
    groups=GROUPS,
    stored=STORED,
    removal_evidence=(),
):
    cohort = {
        "round": 1,
        "champion": {"version": "v0", "digest": "c" * 64},
        "finalists": [
            {
                "attempt_id": "r01-a01",
                "candidate_digest": "f" * 64,
                "level": None if mode == "observe" else 0.9,
            }
        ],
        "members": [FINALIST],
        "global": global_mode,
        "mode": mode,
        "repeats": repeats,
        "seed_mode": "exact",
        "digest": "b" * 64,
    }
    return global_comparison_record(
        cohort,
        {FINALIST: {"complete": complete, "critical": critical, "prefix": "fresh-prefix"}},
        members=[FINALIST],
        position=0,
        finalist_rows=finalist,
        champion_rows=champion,
        groups=groups,
        corpus_ids=list(groups),
        primary="score",
        campaign_seed=7,
        draws=400,
        family_version=14,
        stability="stable",
        stored_champion=stored,
        excluded=excluded,
        removal_evidence=removal_evidence,
    )


def test_bilateral_removals_cut_at_r_and_separately_renormalized_weights():
    # Fixed weights a=b=1/6, c=d=1/3. Remove b and leave c point-only:
    # point weights a,c,d = 1/5,2/5,2/5; bound a,d = 1/3,2/3.
    finalist = rows(
        {
            "a": [0.75, 0.5, 1.0, 0.0],
            "b": [1.0] * 4,
            "c": [1.0, 0.0, 0.0, 0.0],
            "d": [0.5] * 3 + [0.0],
        }
    )
    champion = rows(
        {
            "a": [0.5, 0.25, 0.75, 1.0],
            "b": [0.0] * 4,
            "c": [0.5, 1.0, 1.0, 1.0],
            "d": [0.5] * 3 + [1.0],
        }
    )
    result = compare(
        finalist,
        champion,
        excluded=frozenset({("b", 3), ("b", 1), ("b", 2), ("c", 2), ("c", 3), ("a", 4)}),
    )
    assert result["point"] == 0.25
    assert result["lower"] == pytest.approx(1 / 12)
    assert result["exact"] is False
    assert result["pairing"] == "exact"
    assert result["champion_side"] == "stored"
    assert result["champion_measurement_id"] == "baseline"
    assert result["removed_pairs"] == [["b", 1], ["b", 2], ["b", 3], ["c", 2], ["c", 3]]
    assert result["removed_cases"] == ["b"]
    assert result["accepted"] is True


def test_empty_stored_case_is_removed_without_full_coverage_requirement():
    result = compare(
        rows({case: [1.0] * 3 for case in GROUPS}),
        rows({"a": [0.5] * 3, "c": [0.5] * 3, "d": [0.5] * 3}),
    )
    assert result["point"] == result["lower"] == 0.5
    assert result["removed_cases"] == ["b"]
    assert result["exact"] is True


def test_constant_paired_difference_is_exact_even_when_each_side_varies():
    result = compare(
        rows({case: [0.5, 0.75, 1.0] for case in GROUPS}),
        rows({case: [0.25, 0.5, 0.75] for case in GROUPS}),
    )
    assert result["point"] == result["lower"] == 0.25
    assert result["exact"] is True
    assert result["reason"] == "global_bound_positive"


def test_equal_unpaired_varying_multisets_are_not_exact():
    side = rows({case: [0.0, 1.0, 1.0] for case in GROUPS}, paired=False)
    result = compare(side, side)
    assert result["point"] == 0.0
    assert result["lower"] < 0.0
    assert result["exact"] is False
    assert result["pairing"] == "unpaired"
    assert result["accepted"] is False


def test_point_only_provenance_mismatch_prevents_pairing_over_bound_subset():
    finalist = rows({case: [0.5, 0.75, 1.0] for case in GROUPS})
    champion = rows({case: [0.25, 0.5, 0.75] for case in GROUPS})
    champion = tuple(
        replace(row, dependency_digest="e" * 64) if row.case_id == "c" else row for row in champion
    )
    result = compare(finalist, champion, excluded=frozenset({("c", 2), ("c", 3)}))
    assert result["point"] == 0.25
    assert result["pairing"] == "unpaired"
    assert result["lower"] < 0.25
    assert result["exact"] is False


@pytest.mark.parametrize(
    "mode,global_mode",
    [
        ("statistical", "off"),
        ("statistical", "point"),
        ("statistical", "bound"),
        ("observe", "off"),
    ],
)
def test_empty_point_is_not_accepted_and_preserves_complete_estimate(mode, global_mode):
    side = rows({case: [0.5] * 3 for case in GROUPS})
    result = compare(
        side,
        side,
        mode=mode,
        global_mode=global_mode,
        critical=True,
        excluded=frozenset((case, repeat) for case in GROUPS for repeat in (1, 2, 3)),
    )
    assert result["point"] is result["lower"] is None
    assert result["level"] == (None if mode == "observe" else 0.9)
    assert result["exact"] == (None if mode == "observe" else False)
    assert result["reason"] == result["unavailable"] == "final_incomplete"
    assert result["accepted"] is False
    assert result["removed_cases"] == list(GROUPS)


@pytest.mark.parametrize(
    "mode,global_mode,critical,reason",
    [
        ("statistical", "off", False, "global_off"),
        ("statistical", "point", False, "global_point_improved"),
        ("statistical", "bound", False, "final_incomplete"),
        ("statistical", "bound", True, "final_incomplete"),
        ("statistical", "point", True, "critical_failure"),
        ("observe", "off", False, "global_off"),
    ],
)
def test_empty_bound_retains_point_and_decides_by_mode(mode, global_mode, critical, reason):
    result = compare(
        rows({case: [0.75] * 3 for case in GROUPS}),
        rows({case: [0.5] * 3 for case in GROUPS}),
        mode=mode,
        global_mode=global_mode,
        critical=critical,
        excluded=frozenset((case, repeat) for case in GROUPS for repeat in (2, 3)),
    )
    assert result["point"] == 0.25 and result["lower"] is None
    assert result["level"] == (None if mode == "observe" else 0.9)
    assert result["exact"] == (None if mode == "observe" else False)
    assert result["unavailable"] == (
        "bound_not_requested_in_observe_mode" if mode == "observe" else "insufficient_repeats"
    )
    assert result["reason"] == reason
    assert result["accepted"] == reason.startswith("global_")


def test_observe_r1_preserves_point_without_bound():
    result = compare(
        rows({case: [0.75, 0.0] for case in GROUPS}),
        rows({case: [0.5, 1.0] for case in GROUPS}),
        repeats=1,
        mode="observe",
        global_mode="off",
    )
    assert result["point"] == 0.25
    assert result["lower"] is result["exact"] is None
    assert result["accepted"] is True


def test_incomplete_finalist_reads_no_numerical_rows_and_has_fixed_state():
    result = compare((), (), complete=False, critical=True)
    assert result["point"] is result["lower"] is result["level"] is result["exact"] is None
    assert result["reason"] == result["unavailable"] == "final_incomplete"
    assert result["champion_side"] == "stored"
    assert result["removed_pairs"] == result["removed_cases"] == []


@pytest.mark.parametrize("global_mode", ["point", "bound"])
@pytest.mark.parametrize("score,accepted", [(0.25, False), (0.5, False), (0.75, True)])
def test_global_threshold_is_strictly_positive(global_mode, score, accepted):
    result = compare(
        rows({case: [score] * 3 for case in GROUPS}),
        rows({case: [0.5] * 3 for case in GROUPS}),
        global_mode=global_mode,
    )
    assert result["accepted"] is accepted


@pytest.mark.parametrize("side", ["finalist", "champion"])
def test_missing_metric_in_excluded_row_is_still_a_fault(side):
    good = rows({case: [0.5] * 3 for case in GROUPS})
    bad = (replace(good[0], metrics={"other": 0.5}), *good[1:])
    result = compare(
        bad if side == "finalist" else good,
        bad if side == "champion" else good,
        excluded=frozenset({("a", 1)}),
    )
    assert result == MissingMetric("a", "sample-1")


@pytest.mark.parametrize("bad_score", [-0.1, 1.1])
def test_out_of_range_metric_is_not_a_removed_case_or_zero(bad_score):
    side = rows({case: [0.5] * 3 for case in GROUPS})
    bad = (replace(side[0], metrics={"score": bad_score}), *side[1:])
    with pytest.raises(ValueError):
        compare(side, bad, excluded=frozenset({("a", 1)}))


@pytest.mark.parametrize("repeat", [0, -1, True, 1.5])
def test_invalid_repeat_is_refused(repeat):
    side = rows({case: [0.5] * 3 for case in GROUPS})
    with pytest.raises(ValueError):
        compare(side, (replace(side[0], repeat=repeat), *side[1:]))


@pytest.mark.parametrize("later_bytes", [False, True])
async def test_campaign_stored_inputs_use_citation_instead_of_live_measurement(
    tmp_path, monkeypatch, later_bytes
):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run

    original = _Campaign.hook
    observed = []

    def inspect_stored(self, name):
        if name == "after_finalists_bound":
            state = self.snapshot().rounds[1]
            mid = state.baseline_measurement_id
            if later_bytes:
                self.measurements[mid]._require_journal().append(
                    "journal_dispatch_stopped", {"code": "journal_limit_exceeded"}
                )
                assert self.prefix(mid) != self.prefix(mid, round_number=1)

            # A live-head regression must fail, even if later bytes carry no scores.
            def refuse_live(*args, **kwargs):
                raise AssertionError("stored input read a live measurement")

            with monkeypatch.context() as patch:
                patch.setattr(self.measurements[mid], "sample_rows", refuse_live)
                patch.setattr(self.measurements[mid], "exhausted_rows", refuse_live)
                stored_rows = self.cited_baseline_rows(1, mid)
                assert len(stored_rows) == 2
                assert all(row.disposition == "score" for row in stored_rows)
                assert self.excluded_pairs(mid, 1, rows=stored_rows) == frozenset()
                observed.extend(stored_rows)
            raise Crash("checked cited stored inputs")
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", inspect_stored)
    with pytest.raises(Crash):
        await family14_run(tmp_path, monkeypatch, family14_settings={"stability": "stable"})
    assert len(observed) == 2


def test_cited_exclusions_validate_all_rows_then_cut_at_r():
    from types import SimpleNamespace

    from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
    from evolution.tree.campaign_schema import pairs_digest

    # The call only needs these campaign facts; no live measurement is present.
    side = rows({"a": [0.5, 0.5, 0.5, 0.5]})
    side = tuple(
        replace(row, disposition="exclude", exhausted=True, metrics=None)
        if row.repeat in (2, 4)
        else row
        for row in side
    )
    summary = {"baseline": {1: (2, pairs_digest([("a", "sample-2"), ("a", "sample-4")]))}}
    campaign = object.__new__(_Campaign)
    campaign._family_version = 14
    campaign.snapshot = lambda: SimpleNamespace(excluded=summary)
    assert campaign.excluded_pairs("baseline", 3, rows=side) == frozenset({("a", 2)})
    with pytest.raises(CampaignInvariantViolated, match="pairs_excluded"):
        campaign.excluded_pairs("baseline", 3, rows=side[:3])


@pytest.mark.parametrize("digest,lower", [("a" * 64, -11 / 96), ("c" * 64, -7 / 96)])
def test_stored_content_digest_is_the_second_seed_prefix(digest, lower):
    # Independent six-draw-per-side Random.random bootstrap, 400 replicates;
    # linear 0.1 quantile at ranks 39/40 gives these exact dyadic-score ratios.
    finalist = rows({"a": [1 / 16, 3 / 16, 9 / 16, 10 / 16, 14 / 16, 1.0]}, paired=False)
    champion = rows({"a": [0.0, 2 / 16, 5 / 16, 7 / 16, 11 / 16, 15 / 16]}, paired=False)
    result = compare(
        finalist,
        champion,
        repeats=6,
        groups={"a": ("g", "s")},
        stored={"measurement_id": "baseline", "prefix_digest": digest},
    )
    assert result["point"] == pytest.approx(13 / 96)
    assert result["lower"] == pytest.approx(lower)
    assert result["pairing"] == "unpaired" and result["exact"] is False


def test_cited_reader_preserves_imported_scores_and_source_provenance():
    from evolution.tree.campaign import _Campaign
    from tests.test_tree_baseline_import import source_measurement

    source = source_measurement(imported_from="grandparent", metrics={"score": 0.75})
    campaign = object.__new__(_Campaign)
    campaign.cited_baseline_records = lambda round_number, measurement_id: source.records
    (row,) = campaign.cited_baseline_rows(1, "baseline")
    assert row.imported is True
    assert row.metrics == {"score": 0.75}
    imported = next(
        record.payload for record in source.records if record.event == "sample_imported"
    )
    assert row.effective_seed == imported["effective_seed"]
    assert row.dependency_digest == imported["dependency_digest"]
    # Cited imported evidence participates in the real numerical producer.
    finalist = rows({"c": [1.0]}, dependency="different")
    result = compare(
        finalist, (row,), repeats=1, groups={"c": ("g", "s")}, mode="observe", global_mode="off"
    )
    assert result["point"] == 0.25
    assert result["pairing"] == "unpaired"


@pytest.mark.parametrize(
    "excluded",
    [
        frozenset({("a", 0)}),
        frozenset({("a", True)}),
        frozenset({("a", 1.5)}),
        frozenset({("unknown", 1)}),
    ],
)
def test_invalid_stored_exclusions_are_not_silently_removed(excluded):
    side = rows({case: [0.5] * 3 for case in GROUPS})
    with pytest.raises(ValueError):
        compare(side, side, excluded=excluded)


def _independent_summary(values):
    import hashlib
    import json

    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"count": len(values), "digest": hashlib.sha256(encoded.encode()).hexdigest()}


@pytest.mark.parametrize("count", [0, 1, 64, 65])
def test_removal_encoder_keeps_small_lists_and_hashes_complete_large_sets(count):
    from evolution.tree import confidence_cohort

    # Corpus order deliberately differs from identifier order and input order.
    cases = [f"z{i:04d}" for i in reversed(range(count))]
    pairs = [[case, repeat] for case in cases for repeat in (1, 2)]
    payload = dict(removed_pairs=list(reversed(pairs)), removed_cases=list(reversed(cases)))
    encoded = confidence_cohort.encode_final_removals(payload, corpus_ids=cases)
    assert encoded["removed_pairs"] == (pairs if len(pairs) <= 64 else _independent_summary(pairs))
    assert encoded["removed_cases"] == (cases if count <= 64 else _independent_summary(cases))
    assert payload["removed_cases"] == list(reversed(cases))  # caller inputs stay intact


@pytest.mark.parametrize("count", [0, 1, 64, 65, 10000])
def test_producer_encodes_every_exclusion_instead_of_truncating(count):
    from evolution.tree.execution.journal_identity import canonical_json

    # Leave two observations so the actual producer can calculate an exact bound.
    finalist = rows({"z": [0.75] * (count + 2)})
    champion = rows({"z": [0.5] * (count + 2)})
    result = compare(
        finalist,
        champion,
        groups={"z": ("g", "s")},
        repeats=count + 2,
        excluded=frozenset(("z", repeat) for repeat in range(1, count + 1)),
    )
    pairs = [["z", repeat] for repeat in range(1, count + 1)]
    assert result["removed_pairs"] == (pairs if count <= 64 else _independent_summary(pairs))
    assert result["point"] == result["lower"] == 0.25
    assert result["removed_cases"] == []
    assert len(canonical_json(result)) <= 16 * 1024


def test_two_legal_small_lists_that_overflow_payload_are_both_summarized():
    from evolution.tree import confidence_cohort
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.storage.envelope import IDENTIFIER

    cases = ["𝒛" * 254 + f"{index:02d}" for index in range(9)]
    assert all(IDENTIFIER.fullmatch(case) for case in cases)
    pairs = [[case, 1] for case in cases]
    payload = dict(removed_pairs=pairs, removed_cases=cases)
    assert len(canonical_json(payload)) > 16 * 1024
    encoded = confidence_cohort.encode_final_removals(payload, corpus_ids=cases)
    assert encoded == dict(
        removed_pairs=_independent_summary(pairs), removed_cases=_independent_summary(cases)
    )
    assert len(canonical_json(encoded)) <= 16 * 1024


def test_producer_budget_includes_exact_evidence_and_long_legal_case_ids():
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.storage.envelope import IDENTIFIER
    from tests.campaign_fakes import prefix_reference

    cases = ["𝒛" * 254 + f"{index:02d}" for index in range(14)]
    assert all(IDENTIFIER.fullmatch(case) for case in cases)
    groups = {case: ("g", "s") for case in cases}
    side = rows({case: [0.5] * 3 for case in cases})
    excluded = frozenset((case, 1) for case in cases)
    # Two valid-sized durable reference objects make the legal pair list overflow.
    evidence = [
        prefix_reference("𝒓" * 256, "campaign.jsonl#bytes=0:100"),
        prefix_reference("𝒓" * 256, "measurements/m-r00-champion/events.jsonl#bytes=0:100"),
    ]
    evidence[0]["kind"] = "journal_record"
    without = compare(side, side, groups=groups, excluded=excluded)
    assert isinstance(without["removed_pairs"], list)
    assert len(canonical_json({**without, "evidence": evidence})) > 16 * 1024
    result = compare(side, side, groups=groups, excluded=excluded, removal_evidence=evidence)
    assert result["removed_pairs"] == _independent_summary([[case, 1] for case in cases])
    assert result["removed_cases"] == []
    assert "evidence" not in result
    assert len(canonical_json({**result, "evidence": evidence})) <= 16 * 1024
    campaign_family_for(14).validate_payload(
        "global_comparison_completed",
        {
            **result,
            "finalist_measurement_id": "m-r01-final-01-a01",
            "champion_measurement_id": "m-r00-champion",
            "evidence": evidence,
        },
    )
