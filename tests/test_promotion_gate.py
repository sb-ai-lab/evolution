from __future__ import annotations

import pytest

from evolution.evolve import promotion_gate as pg

pytestmark = pytest.mark.slow


def test_split_n5_is_3_diagnose_2_validate():
    diag, val = pg.split_train_ids(["a", "b", "c", "d", "e"])
    assert diag == ["a", "b", "c"] and val == ["d", "e"]
    assert pg.delta_gate_enabled(["a", "b", "c", "d", "e"]) is True


def test_pass_rate_gate_strict_gt():
    scores = {("p", "v"): 0.4, ("c", "v"): 0.9}
    promote, tel = pg.gate_candidate(
        parent_body="p",
        candidate_body="c",
        validate_ids=["v"],
        score_fn=lambda body, t: scores[(body, t)],
        delta=0.0,
    )
    assert promote is True and tel["gate"] == "delta_gate"


def test_gate_candidate_rejects_empty_validation():
    promote, telemetry = pg.gate_candidate(
        parent_body="parent",
        candidate_body="candidate",
        validate_ids=[],
        score_fn=lambda body, task: 1.0,
    )

    assert promote is False
    assert telemetry["reason"] == "no_validation_tasks"


def test_conservative_paired_gate_requires_four_measurements() -> None:
    promote, telemetry = pg.conservative_paired_gate(
        {"val": [0.0, 0.0, 0.0]},
        {"val": [1.0, 1.0, 1.0]},
    )

    assert promote is False
    assert telemetry["reason"] == "insufficient_pairs"
    assert telemetry["n_pairs"] == 3


def test_conservative_paired_gate_promotes_a_powered_uniform_gain() -> None:
    promote, telemetry = pg.conservative_paired_gate(
        {"val": [0.0, 0.0, 0.0, 0.0]},
        {"val": [1.0, 1.0, 1.0, 1.0]},
    )

    assert promote is True
    assert telemetry["p_value"] == pytest.approx(1 / 16)
    assert telemetry["reason"] == "paired_improvement"


def test_conservative_paired_gate_rejects_any_task_regression() -> None:
    promote, telemetry = pg.conservative_paired_gate(
        {"stable": [1.0, 1.0], "improvable": [0.0, 0.0]},
        {"stable": [0.5, 0.5], "improvable": [1.0, 1.0]},
    )

    assert promote is False
    assert telemetry["reason"] == "task_regression"
    assert telemetry["regressions"] == ["stable"]


def test_conservative_paired_gate_rejects_mismatched_trial_shapes() -> None:
    with pytest.raises(ValueError, match="same paired trial count"):
        pg.conservative_paired_gate(
            {"val": [0.0, 0.0, 0.0, 0.0]},
            {"val": [1.0, 1.0]},
        )


def test_gate_candidate_validates_alpha_before_scoring(monkeypatch):
    monkeypatch.setenv("EVO_GATE_METRIC", "perm_lex")
    monkeypatch.setenv("EVO_PROMOTE_ALPHA", "2")

    with pytest.raises(ValueError, match="EVO_PROMOTE_ALPHA"):
        pg.gate_candidate(
            parent_body="parent",
            candidate_body="candidate",
            validate_ids=["task"],
            score_fn=lambda body, task: pytest.fail("score function ran"),
        )


def test_plain_gate_rejects_trainval_metric(monkeypatch):
    monkeypatch.setenv("EVO_GATE_METRIC", "solve_count_trainval")
    with pytest.raises(ValueError, match="EVO_GATE_METRIC"):
        pg.validate_gate_environment()


def test_gate_records_observed_delta_alongside_margin_params():
    scores = {("p", "v"): 0.4, ("c", "v"): 0.9}
    _, tel = pg.gate_candidate(
        parent_body="p",
        candidate_body="c",
        validate_ids=["v"],
        score_fn=lambda body, t: scores[(body, t)],
        delta=0.05,
        tolerance=0.01,
    )
    assert tel["observed_delta"] == pytest.approx(0.5)
    assert tel["delta"] == 0.05 and tel["tolerance"] == 0.01


def test_perm_lex_worked_example_n1_full_solve_p_is_1_over_20():
    promote, extra = pg.perm_lex_decision([[0.9, 0.8, 0.7]], [[1.0, 1.0, 1.0]], alpha=0.10)
    assert extra["p_value"] == pytest.approx(0.05) and promote is True
    assert pg.perm_lex_decision([[0.9, 0.8, 0.7]], [[1.0, 1.0, 1.0]], alpha=0.01)[0] is False


def test_perm_lex_worked_example_n2_k2_p_is_1_over_36():
    promote, extra = pg.perm_lex_decision(
        [[0.5, 0.5], [0.25, 0.5]], [[1.0, 1.0], [1.0, 0.75]], alpha=0.10
    )
    assert extra["p_value"] == pytest.approx(1 / 36) and extra["perm_space"] == 36 and promote


def test_perm_lex_single_noisy_bump_does_not_promote():
    promote, extra = pg.perm_lex_decision([[0.5, 0.5, 0.5]], [[0.5, 0.5, 0.6]], alpha=0.10)
    assert promote is False and extra["p_value"] == pytest.approx(0.5)


def test_perm_lex_paired_full_dominance_p_is_1_over_2_pow_k():
    # N=1, K=4 paired, candidate strictly beats parent on every seed -> only the all-+
    # sign assignment is >=lex observed -> p = 1/2^4 = 0.0625.
    promote, extra = pg.perm_lex_paired_decision(
        [[0.0, 0.0, 0.0, 0.0]], [[1.0, 1.0, 1.0, 1.0]], alpha=0.10
    )
    assert extra["p_value"] == pytest.approx(1 / 16) and extra["perm_space"] == 16
    assert extra["paired"] is True and promote is True


def test_perm_lex_paired_beats_unpaired_under_between_seed_noise():
    # Candidate is consistently better per seed but seeds are noisy, so the pooled
    # (unpaired) values interleave and unpaired loses signal. Pairing removes the
    # between-seed variance -> promotes where unpaired cannot.
    parent = [[0.1, 0.5, 0.8, 0.4]]
    cand = [[0.3, 0.7, 0.95, 0.6]]
    p_unpaired = pg.perm_lex_decision(parent, cand, alpha=0.10)
    p_paired = pg.perm_lex_paired_decision(parent, cand, alpha=0.10)
    assert p_paired[1]["p_value"] < p_unpaired[1]["p_value"]
    assert p_paired[0] is True and p_unpaired[0] is False


def test_perm_lex_paired_rejects_unpaired_shaped_input():
    promote, extra = pg.perm_lex_paired_decision([[0.5, 0.5]], [[1.0]], alpha=0.10)
    assert promote is False and extra["reason"] == "unpaired_or_empty"


def test_gate_candidate_perm_lex_paired_via_env(monkeypatch):
    monkeypatch.setenv("EVO_GATE_METRIC", "perm_lex_paired")
    monkeypatch.setenv("EVO_PROMOTE_ALPHA", "0.10")
    rates = {("p", "v"): [0.0, 0.0, 0.0, 0.0], ("c", "v"): [1.0, 1.0, 1.0, 1.0]}
    promote, tel = pg.gate_candidate(
        parent_body="p",
        candidate_body="c",
        validate_ids=["v"],
        score_fn=lambda body, t: rates[(body, t)],
    )
    assert tel["gate"] == "perm_lex_paired" and tel["p_value"] == pytest.approx(1 / 16) and promote


def test_gate_candidate_unknown_metric_raises(monkeypatch):
    monkeypatch.setenv("EVO_GATE_METRIC", "nonsense")
    with pytest.raises(ValueError, match="EVO_GATE_METRIC"):
        pg.gate_candidate(
            parent_body="p",
            candidate_body="c",
            validate_ids=["v"],
            score_fn=lambda body, t: 0.5,
        )
    with pytest.raises(ValueError, match="unknown gate metric"):
        pg._aggregate_gate_metric([[0.5]], "mystery_mean")


def test_perm_lex_p_floor_marks_underpowered_below_alpha(monkeypatch):
    promote, extra = pg.perm_lex_decision([[0.9, 0.8, 0.7]], [[1.0, 1.0, 1.0]], alpha=0.01)
    assert promote is False
    assert extra["p_floor"] == pytest.approx(0.05)
    assert extra["status"] == "UNDERPOWERED"


def test_perm_lex_paired_p_floor_collapses_under_tied_pairs():
    # 3 of 4 pairs are exact ties: the naive floor 1/2^4 = 0.0625 would clear alpha,
    # but the realized support collapses to two states -> floor 0.5 -> UNDERPOWERED.
    promote, extra = pg.perm_lex_paired_decision(
        [[0.0, 0.5, 0.5, 0.5]], [[1.0, 0.5, 0.5, 0.5]], alpha=0.10
    )
    assert promote is False
    assert extra["p_value"] == pytest.approx(0.5)
    assert extra["p_floor"] == pytest.approx(0.5)
    assert extra["status"] == "UNDERPOWERED"


def test_perm_lex_paired_powered_case_has_no_underpowered_status():
    _, extra = pg.perm_lex_paired_decision(
        [[0.0, 0.0, 0.0, 0.0]], [[1.0, 1.0, 1.0, 1.0]], alpha=0.10
    )
    assert extra["p_floor"] == pytest.approx(1 / 16)
    assert "status" not in extra


def test_race_confirm_ships_confirmed_winner_on_clear_separation():
    out = pg.race_ship_decision(
        {"v0": [[0.0] * 5], "v1": [[1.0] * 5]},
        alpha=0.10,
        ship_rule="confirm",
    )
    assert out["label"] == "reeval_confirmed_winner"
    assert out["shipped_version"] == "v1" and out["best_version"] == "v1"
    assert out["p_value"] == pytest.approx(1 / 32)
    assert out["p_floor"] == pytest.approx(1 / 32)
    assert out["d_vs_baseline"]["v1"] == pytest.approx(1.0)
    assert out["exact"] is True
    assert out["confidence"] == "confirmed@0.05"


def test_race_confirm_ships_v0_on_nonseparation():
    out = pg.race_ship_decision(
        {
            "v0": [[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0]],
            "v1": [[1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]],
        },
        alpha=0.10,
        ship_rule="confirm",
    )
    assert out["label"] == "reeval_ship_v0_nonseparation"
    assert out["shipped_version"] == "v0" and out["best_version"] == "v1"
    assert out["p_value"] == pytest.approx(93 / 256)
    assert out["p_floor"] == pytest.approx(1 / 256)
    assert out["confidence"] == "unconfirmed"


def test_race_confirm_underpowered_ships_v0():
    out = pg.race_ship_decision(
        {"v0": [[0.0, 0.5, 0.5, 0.5]], "v1": [[1.0, 0.5, 0.5, 0.5]]},
        alpha=0.10,
        ship_rule="confirm",
    )
    assert out["label"] == "reeval_ship_v0_underpowered"
    assert out["shipped_version"] == "v0"
    assert out["p_floor"] == pytest.approx(0.5)
    assert out["confidence"] == "underpowered"


def test_race_argmax_default_ships_argmax_even_when_unconfirmed():
    out = pg.race_ship_decision(
        {
            "v0": [[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0]],
            "v1": [[1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]],
        },
        alpha=0.10,
    )
    assert out["ship_rule"] == "argmax"
    assert out["shipped_version"] == "v1"
    assert out["label"] == "reeval_argmax_unconfirmed"
    assert out["confidence"] == "unconfirmed"
    assert out["p_value"] == pytest.approx(93 / 256)


def test_race_argmax_confirmed_label_on_clear_separation():
    out = pg.race_ship_decision({"v0": [[0.0] * 5], "v1": [[1.0] * 5]}, alpha=0.10)
    assert out["shipped_version"] == "v1"
    assert out["label"] == "reeval_argmax_confirmed"
    assert out["confidence"] == "confirmed@0.05"


def test_race_argmax_underpowered_still_ships_argmax():
    out = pg.race_ship_decision(
        {"v0": [[0.0, 0.5, 0.5, 0.5]], "v1": [[1.0, 0.5, 0.5, 0.5]]},
        alpha=0.10,
    )
    assert out["shipped_version"] == "v1"
    assert out["label"] == "reeval_argmax_underpowered"
    assert out["confidence"] == "underpowered"


def test_race_argmax_full_tie_ships_v0():
    out = pg.race_ship_decision({"v0": [[0.5, 0.5]], "v1": [[0.5, 0.5]]}, alpha=0.10)
    assert out["best_version"] == "v0"
    assert out["shipped_version"] == "v0"
    assert out["label"] == "reeval_ship_v0"
    assert out["p_floor"] == pytest.approx(1.0)


def test_race_single_version_ships_v0():
    out = pg.race_ship_decision({"v0": [[0.2, 0.4], [0.6]]}, alpha=0.10)
    assert out["label"] == "reeval_single_version"
    assert out["shipped_version"] == "v0"
    assert out["p_value"] is None


def test_race_max_t_uses_same_flips_across_versions():
    # v2 dominates on every unit; v1 is pure noise. The max-T null must still
    # confirm v2 (shared flips), not dilute it into non-separation.
    out = pg.race_ship_decision(
        {
            "v0": [[0.0] * 6],
            "v1": [[1.0, 0.0, 1.0, 0.0, 1.0, 0.0]],
            "v2": [[1.0] * 6],
        },
        alpha=0.10,
        ship_rule="confirm",
    )
    assert out["best_version"] == "v2"
    assert out["shipped_version"] == "v2"
    assert out["label"] == "reeval_confirmed_winner"


def test_race_mismatched_shapes_or_rule_raise():
    with pytest.raises(ValueError, match="shape"):
        pg.race_ship_decision({"v0": [[0.0, 0.0]], "v1": [[1.0]]}, alpha=0.10)
    with pytest.raises(ValueError, match="vN"):
        pg.race_ship_decision({"seed": [[0.0]]}, alpha=0.10)
    with pytest.raises(ValueError, match="EVO_RACE_SHIP_RULE"):
        pg.race_ship_decision({"v0": [[0.0]]}, alpha=0.10, ship_rule="hopeful")


def test_race_monte_carlo_path_is_deterministic():
    rates = {
        "v0": [[float(i % 2) for i in range(20)]],
        "v1": [[float((i + 1) % 2) for i in range(20)]],
    }
    a = pg.race_ship_decision(rates, alpha=0.10)
    b = pg.race_ship_decision(rates, alpha=0.10)
    assert a["exact"] is False
    assert a["p_value"] > 0.0
    assert a["p_value"] == b["p_value"] and a["label"] == b["label"]


def test_race_exact_when_nonzero_units_small_despite_many_total_units():
    # 20 units but only 3 nonzero diffs (+1/20 each): the null max-stat is
    # (s1+s2+s3)/20 over 2^3 shared sign states and only +++ reaches the observed
    # d = 3/20, so p is exactly 1/8 — no MC smoothing ((1+hits)/20001 can never
    # equal 1/8).
    rates = {
        "v0": [[0.0, 0.0, 0.0] + [0.5] * 17],
        "v1": [[1.0, 1.0, 1.0] + [0.5] * 17],
    }
    out = pg.race_ship_decision(rates, alpha=0.10)
    assert out["n_units"] == 20
    assert out["exact"] is True
    assert out["p_value"] == 1 / 8
    assert out["p_floor"] == 1 / 8
    assert out["shipped_version"] == "v1"


def test_race_exact_enumeration_flips_union_of_nonzero_units_across_versions():
    # v1 differs on units {0,1}, v2 on {2,3}: the union of 4 nonzero units is
    # enumerated with SHARED flips (2^4 states). max((s0+s1)/20, (s2+s3)/20) >=
    # observed d = 2/20 in 4 + 4 - 1 = 7 states, so p is exactly 7/16.
    rates = {
        "v0": [[0.0] * 4 + [0.5] * 16],
        "v1": [[1.0, 1.0, 0.0, 0.0] + [0.5] * 16],
        "v2": [[0.0, 0.0, 1.0, 1.0] + [0.5] * 16],
    }
    out = pg.race_ship_decision(rates, alpha=0.10)
    assert out["exact"] is True
    assert out["p_value"] == 7 / 16
    assert out["best_version"] == "v1"
