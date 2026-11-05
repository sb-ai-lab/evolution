from evolution.tree.campaign_caveats import (
    AFFECTED_CAVEAT_KINDS,
    CAVEAT_KINDS,
    LINEAGE_CAVEAT_KINDS,
    LineageCaveat,
    aggregate_lineage,
    own_caveats,
)

DECLARATION = dict(
    mode="inherit",
    source_measurement_id="m-r00-champion",
    source_prefix_digest="a" * 64,
    pairs_digest="b" * 64,
    imported=118,
    scheduled=120,
    fields=["identity.target.runtime_ref_digest"],
    origin_runs=1,
)


def linked(run_id="run-1", **extra):
    return {"run_id": run_id, **extra}


def test_no_link_and_an_unchanged_link_have_no_caveat():
    assert own_caveats(None) == []
    assert own_caveats(linked()) == []


def test_condition_changed_and_unfinished_caveats():
    payload = linked(
        condition_changed=["identity.scorer.digest"],
        predecessor_finished=False,
        authorization={"id": "a" * 64},
    )
    assert own_caveats(payload) == [
        {"kind": "condition_changed", "from_run": "run-1", "fields": ["identity.scorer.digest"]},
        {"kind": "predecessor_unfinished", "from_run": "run-1", "authorization_id": "a" * 64},
    ]


def test_baseline_inherited_is_the_third_caveat_and_reported_without_changed_fields():
    payload = {"run_id": "pred", "baseline": {**DECLARATION, "fields": []}}
    assert own_caveats(payload) == [
        {
            "kind": "baseline_inherited",
            "from_run": "pred",
            "source_measurement_id": "m-r00-champion",
            "imported": 118,
            "scheduled": 120,
            "fields": [],
            "origin_runs": 1,
        }
    ]


def test_no_caveat_for_an_empty_or_abandoned_import():
    assert own_caveats({"run_id": "p", "baseline": {**DECLARATION, "imported": 0}}) == []
    assert own_caveats({"run_id": "p", "baseline": DECLARATION}, baseline_abandoned=True) == []


def test_the_lineage_aggregate_is_bounded_by_kinds_not_by_length():
    # Twenty links, each with a changed target: fold the aggregate forward.
    carried: tuple[LineageCaveat, ...] = ()
    for index in range(20):
        predecessor = linked(
            run_id=f"run-{index}",
            condition_changed=["identity.target.runtime_ref_digest"],
            lineage_caveats=[c.to_payload() for c in carried],
        )
        carried = aggregate_lineage(predecessor, f"run-{index}")
    assert carried == (
        LineageCaveat("condition_changed", 20, ("identity.target.runtime_ref_digest",), "run-19"),
    )


def test_nearest_run_comes_from_the_lineage_when_the_predecessor_is_clean():
    inherited = [
        LineageCaveat("condition_changed", 1, ("identity.scorer.digest",), "run-b").to_payload()
    ]
    carried = aggregate_lineage(linked(run_id="run-c", lineage_caveats=inherited), "run-c")
    assert carried == (LineageCaveat("condition_changed", 1, ("identity.scorer.digest",), "run-b"),)


def test_fields_are_the_sorted_union():
    inherited = [
        LineageCaveat("condition_changed", 1, ("identity.scorer.digest",), "run-a").to_payload()
    ]
    carried = aggregate_lineage(
        linked(
            run_id="run-b", condition_changed=["identity.corpus_digest"], lineage_caveats=inherited
        ),
        "run-b",
    )
    assert carried[0].fields == ("identity.corpus_digest", "identity.scorer.digest")
    assert (carried[0].runs, carried[0].nearest_run) == (2, "run-b")


def test_an_abandoned_predecessor_import_is_not_carried_into_the_lineage():
    """Ruling R5: a predecessor that measured its baseline fresh under a
    `fresh_baseline` decision carried nothing, so its successor's lineage
    aggregate has no `baseline_inherited` from it."""

    predecessor = linked(run_id="run-a", baseline=DECLARATION)
    carried = aggregate_lineage(predecessor, "run-a")
    assert [c.kind for c in carried] == ["baseline_inherited"]
    assert aggregate_lineage(predecessor, "run-a", predecessor_baseline_abandoned=True) == ()


def test_family14_look_registry_is_separate_from_legacy_public_kinds():
    from evolution.tree.campaign_caveats import LOOK_CAVEAT_KINDS14
    from evolution.tree.campaign_schema import LOOK_CAVEATS, LOOK_CAVEATS14

    assert LOOK_CAVEAT_KINDS14 == (
        "intervals_empirical",
        "noise_allowance_empirical",
        "causal_parent_not_contemporaneous",
        "observe_gate_not_enforced",
        "uncalibrated_point_estimates",
        "noise_allowance_unavailable",
        "exact_contrast",
        "noise_not_observed",
    )
    assert set(LOOK_CAVEAT_KINDS14) == LOOK_CAVEATS14
    assert "observe_calibration_degenerate" not in LOOK_CAVEATS14
    assert "exact_contrast" not in LOOK_CAVEATS
    assert "noise_not_observed" not in CAVEAT_KINDS


def test_family14_caveat_registries_are_separate_tuples():
    """H12b plan choice 12: family 14's affected and document registries are
    their own tuples, so family 13's sort keys and validators are unchanged
    (the pins above pass unedited); `completion_regression` and
    `observe_calibration_degenerate` never appear in family 14 (D8, R25)."""

    from evolution.tree.campaign_caveats import (
        AFFECTED_CAVEAT_KINDS14,
        CAVEAT_KINDS14,
        CHAMPION_NOT_CONTEMPORANEOUS_KIND,
        FINAL_CAMPAIGN_KINDS,
        LOOK_CAVEAT_KINDS14,
        OBSERVE_CAMPAIGN_KINDS,
        STORED_PAIRS_EXCLUDED_KIND,
    )

    assert CHAMPION_NOT_CONTEMPORANEOUS_KIND == "champion_not_contemporaneous"
    assert STORED_PAIRS_EXCLUDED_KIND == "stored_pairs_excluded"
    assert AFFECTED_CAVEAT_KINDS14 == (
        "focus_selection_bias",
        "provenance_drift",
        "components_changed_by_condition",
        *LOOK_CAVEAT_KINDS14,
        "champion_not_contemporaneous",
        "stored_pairs_excluded",
    )
    assert CAVEAT_KINDS14 == (
        "condition_changed",
        "predecessor_unfinished",
        "baseline_inherited",
        "condition_changed_in_place",
        "grouping_fallback",
        *AFFECTED_CAVEAT_KINDS14,
    )
    assert len(set(CAVEAT_KINDS14)) == len(CAVEAT_KINDS14)
    assert "completion_regression" not in CAVEAT_KINDS14
    assert "observe_calibration_degenerate" not in CAVEAT_KINDS14
    # Choice 13: the campaign-level items lead in this order.
    assert FINAL_CAMPAIGN_KINDS == (*OBSERVE_CAMPAIGN_KINDS, "champion_not_contemporaneous")
    assert [AFFECTED_CAVEAT_KINDS14.index(kind) for kind in FINAL_CAMPAIGN_KINDS] == sorted(
        AFFECTED_CAVEAT_KINDS14.index(kind) for kind in FINAL_CAMPAIGN_KINDS
    )
    # Family 13's registries are untouched, and no family-14 kind is a lineage kind.
    assert len(AFFECTED_CAVEAT_KINDS) == 11
    assert CAVEAT_KINDS == CAVEAT_KINDS14[:5] + AFFECTED_CAVEAT_KINDS
    assert LINEAGE_CAVEAT_KINDS == (
        "condition_changed",
        "predecessor_unfinished",
        "baseline_inherited",
        "condition_changed_in_place",
    )
    assert not set(AFFECTED_CAVEAT_KINDS14) & set(LINEAGE_CAVEAT_KINDS)
    for kind in ("champion_not_contemporaneous", "stored_pairs_excluded", "exact_contrast"):
        assert kind not in CAVEAT_KINDS
