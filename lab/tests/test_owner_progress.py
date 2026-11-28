import pytest

from evolution_lab.owner import core_raw
from evolution_lab.owner.models import Coverage
from evolution_lab.owner.progress import (
    PHASES, STAGE_OF_ACTION, STAGES, CampaignPlan, campaign_progress, evaluation_progress,
    validate_progress,
)

PLAN = CampaignPlan("campaign.json", rounds=5, attempts_per_round=4, metric_primary="score")
AT = "2026-09-29T11:52:40.000001Z"


def _act(kind, **fields):
    return {"kind": kind, "fields": fields}


def _core(actions=(), rounds=(), measurements=(), **top):
    core = {"complete": False, "suspended": False, "actions": list(actions),
            "rounds": list(rounds), "measurements": list(measurements)}
    core.update(top)
    return core


def _sample(state):
    return {"case_id": "c", "sample_id": "s", "scoring_state": state, "metrics": {}}


def test_table_covers_every_action_of_the_pin():
    """Spec 2026-10-13 §6.6: the stage table is exactly the pin's status action names of family14."""
    assert set(STAGE_OF_ACTION) == core_raw.status_action_names()
    assert set(STAGE_OF_ACTION.values()) == set(STAGES)


@pytest.mark.parametrize("kind", sorted(STAGE_OF_ACTION))
def test_each_action_maps_to_its_stage(kind):
    value = campaign_progress(_core([_act(kind)]), PLAN, AT)
    assert value["stages"] == [STAGE_OF_ACTION[kind]]
    assert value["phase"] == STAGE_OF_ACTION[kind]
    validate_progress(value, "campaign")


def test_phase_is_the_latest_stage_whatever_the_action_order():
    actions = [_act("CloseAttempt"), _act("RunEditor"), _act("BindBaseline")]
    value = campaign_progress(_core(actions), PLAN, AT)
    assert value["stages"] == ["baseline", "generation", "evaluation"]
    assert value["phase"] == "evaluation"
    assert campaign_progress(_core(list(reversed(actions))), PLAN, AT) == value


def test_unknown_action_is_its_own_stage():
    only = campaign_progress(_core([_act("NewCoreThing")]), PLAN, AT)
    assert only["stages"] == ["unknown"] and only["phase"] == "unknown"
    mixed = campaign_progress(_core([_act("NewCoreThing"), _act("RunEditor")]), PLAN, AT)
    assert mixed["stages"] == ["generation", "unknown"] and mixed["phase"] == "generation"


def test_suspended_finished_and_waiting():
    assert campaign_progress(_core([_act("RunEditor")], suspended=True), PLAN, AT)["phase"] == "suspended"
    assert campaign_progress(_core(complete=True), PLAN, AT)["phase"] == "finished"
    assert campaign_progress(_core(), PLAN, AT)["phase"] == "waiting"


def test_rounds_attempts_and_coverage():
    rounds = [
        {"round": 1, "outcome": "promoted", "attempts": [{"outcome": "finalist"}, {"outcome": "rejected"}]},
        {"round": 2, "outcome": None, "attempts": [{"outcome": "rejected"}, {"outcome": None}]},
    ]
    measurements = [
        {"measurement_id": "m1", "status": {"complete": True, "samples": [_sample("finished")] * 3}},
        {"measurement_id": "m2", "status": {"complete": False,
                                            "samples": [_sample("finished"), _sample("not_started")]}},
        {"measurement_id": "m3", "status": None},
    ]
    value = campaign_progress(_core([_act("EvaluateLook")], rounds, measurements), PLAN, AT)
    assert value["rounds"] == {"completed": 1, "current": 2, "planned": 5}
    assert value["attempts"] == {"current_round": {"closed": 1, "running": 1, "planned": 4}, "total": 4}
    assert value["coverage"] == {"expected": 2, "scored": 1}
    assert value["policy_state"] == "present" and value["observed_at"] == AT


def test_no_policy_is_not_an_error():
    value = campaign_progress(_core(), CampaignPlan(None, None, None, None), AT)
    assert value["rounds"]["planned"] is None
    assert value["attempts"]["current_round"]["planned"] is None
    assert value["policy_state"] == "unavailable"
    validate_progress(value, "campaign")


def test_finished_campaign_has_no_current_round():
    rounds = [{"round": r, "outcome": "unmeasurable", "attempts": []} for r in (1, 2)]
    value = campaign_progress(_core(rounds=rounds, complete=True), PLAN, AT)
    assert value["rounds"] == {"completed": 2, "current": None, "planned": 5}
    assert value["attempts"]["current_round"] == {"closed": 0, "running": 0, "planned": 4}


def test_no_core_means_no_progress():
    assert campaign_progress(None, PLAN, AT) is None


def test_evaluation_progress():
    value = evaluation_progress(Coverage(60, 40), AT)
    assert value == {"kind": "evaluation", "coverage": {"expected": 60, "scored": 40}, "observed_at": AT}
    validate_progress(value, "evaluation")
    assert evaluation_progress(None, None)["coverage"] is None


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(extra=1),
    lambda v: v.update(phase="thinking"),
    lambda v: v.update(stages=["generation", "nope"]),
    lambda v: v["rounds"].update(planned=-1),
    lambda v: v.update(policy_state="maybe"),
])
def test_validate_progress_is_closed(mutate):
    value = campaign_progress(_core([_act("RunEditor")]), PLAN, AT)
    mutate(value)
    with pytest.raises(ValueError):
        validate_progress(value, "campaign")


def test_phases_constant():
    assert PHASES == frozenset(STAGES) | {"suspended", "finished", "waiting", "unknown"}


@pytest.mark.parametrize("kind,stage", [
    ("BindComparison", "evaluation"), ("CalibrateNoise", "evaluation"), ("BindFinalists", "selection"),
    ("BindFinalMember", "selection"), ("RunFinalStage", "selection"), ("CompleteFinalMember", "selection")])
def test_family_14_actions_join_existing_stages(kind, stage):
    assert STAGE_OF_ACTION[kind] == stage


def test_suspended_final_is_the_selection_stage():
    core = _core([_act("RunFinalStage")], suspended=True)
    value = campaign_progress(core, PLAN, AT)
    assert value["stages"] == ["selection"] and value["phase"] == "suspended"


def test_family_15_actions_have_stages():
    assert STAGE_OF_ACTION["FailPreflight"] == "evaluation"
    assert STAGE_OF_ACTION["AdvanceCandidateCompletion"] == "selection"


@pytest.mark.parametrize("kind", ["BindCompletion", "CompleteWinner", "CompleteWinnerStage"])
def test_removed_actions_are_not_in_the_pin_table(kind):
    assert kind not in STAGE_OF_ACTION
