"""H3 real family-14 campaigns, stopped before H12 public report assembly.

Only `_Campaign.result` is intercepted, by raising after drive/barrier/terminal.
Creation, lifecycle admission, adapters, scoring, journals and Promotion are real.
Public report validation is deliberately outside this driver's contract.
"""

from __future__ import annotations

import json

import pytest

from tests.campaign_drivers import (
    OBSERVE,
    STATISTICAL,
    WORSE_SCORER,
    _events,
    _measurement_events,
    _records,
    _run_kwargs,
    two_case_run,
)
from tests.campaign_fakes import family14_admitted, family14_document

pytestmark = pytest.mark.slow


class _BeforePublicReport(Exception):
    def __init__(self, snapshot, fault=None):
        self.snapshot = snapshot
        self.fault = fault


async def family14_run(
    tmp_path, monkeypatch, config=OBSERVE, *, family14_settings=None, expected_fault=None, **kwargs
):
    """Return `(run_root, snapshot)` after a genuine family-14 engine invocation.

    Reusing the same path resumes it. `config` is the family-13 fixture's
    gate/repeats/scorer/adapter settings; `family14_settings` may set `margin`,
    `stability`, `round_stop`, `refresh` (`baseline_refresh_after_rounds`,
    default 0) and `grouper` (the `/15` `grouper` section; by default the
    fixture's `reflector.grouping` moves there). Other keywords go to
    `_two_case_run`.
    `expected_fault=(terminal, owner)` returns the engine's actual fault at
    the same pre-public seam, for persistence
    recovery tests; it does not validate public fault result assembly.
    """
    from evolution.tree.campaign import _Campaign
    from tests import campaign_drivers as fixtures

    family14_admitted(monkeypatch)
    real_evolve = fixtures.evolve

    async def evolve(path, *args, **options):
        document = json.loads(path.read_text())
        path.write_text(json.dumps(family14_document(document, **family_options)))
        return await real_evolve(path, *args, **options)

    def before_public_report(self):
        raise _BeforePublicReport(self.snapshot(), self.fault)

    family_options, rest = _run_kwargs(config)
    if family14_settings is not None:
        if set(family14_settings) - {"margin", "stability", "round_stop", "refresh", "grouper"}:
            raise ValueError(
                "driver settings support only margin, stability, round_stop, refresh and grouper"
            )
        family_options.update(family14_settings)
    if "adapter" in kwargs:
        rest.pop("used", None)
    # Scope the seams so later calls can resume without wrapping wrappers.
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", evolve)
        patch.setattr(_Campaign, "result", before_public_report)
        with pytest.raises(_BeforePublicReport) as stopped:
            await two_case_run(tmp_path, terminal=None, **{**rest, **kwargs})
    if expected_fault is not None:
        fault = stopped.value.fault
        assert fault is not None
        assert (fault.terminal, fault.owner) == expected_fault
        return tmp_path / "run", fault
    return tmp_path / "run", stopped.value.snapshot


def payloads(run, event):
    return [record["payload"] for record in _events(run, event)]


async def test_family14_promotes_real_final_member_and_reuses_it(tmp_path, monkeypatch):
    run, snapshot = await family14_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "attempt_closed")[0]["outcome"] == "finalist"
    (binding,) = payloads(run, "finalists_bound")
    assert binding["members"] == ["m-r01-final-01-champion", "m-r01-final-01-a01"]
    bound = payloads(run, "final_measurement_bound")
    assert [(p["stability"], p["round_stop"], p["champion_side"]) for p in bound] == [
        ("unstable", "all_attempts", "fresh"),
        ("unstable", "all_attempts", "fresh"),
    ]
    champion, finalist = payloads(run, "final_measurement_completed")
    (comparison,) = payloads(run, "global_comparison_completed")
    assert comparison["point"] == pytest.approx(finalist["point"] - champion["point"])
    assert (comparison["accepted"], comparison["reason"], comparison["exact"]) == (
        True,
        "global_off",
        None,
    )
    assert comparison["removed_pairs"] == comparison["removed_cases"] == []
    selection = payloads(run, "selection_decided")[0]
    assert selection["measurement_id"] == "m-r01-final-01-a01"
    assert selection["ranking"][0]["accepted"] is True
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["promoted", "clean"]
    assert [p["attempts_opened"] for p in payloads(run, "round_closed")] == [1, 0]
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    for member in binding["members"]:
        events = _records(_measurement_events(run, member))
        assert sum(r["event"] == "scoring_finished" for r in events) == 2
        assert sum(p["measurement_id"] == member for p in payloads(run, "stage_opened")) == 1
        assert not any(r["event"] == "sample_imported" for r in events)
    assert not payloads(run, "cohort_measurement_bound")


async def test_unicode_case_ids_reach_reflector_editor_feedback_and_audit(tmp_path, monkeypatch):
    from evolution.tree.roles.editor.editor_brief import _observation_evidence_id
    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    names = {"c1": "пример-1", "c2": "пример-2"}
    original_manifest = fixtures.two_case_manifest

    def renamed_manifest(*args, rows, **kwargs):
        # Keep public.case_id for FakeInstance's scripted responses; the corpus
        # identity, schedule, journal and role evidence use the renamed IDs.
        renamed = [{**row, "case_id": names[row["case_id"]]} for row in rows]
        return original_manifest(*args, rows=renamed, **kwargs)

    monkeypatch.setattr(fixtures, "two_case_manifest", renamed_manifest)
    diagnosis = {
        "status": "reflected",
        "note": "n",
        "groups": [
            {
                "root_cause": "r",
                "suggested_fix": "f",
                "evidence_ids": [_observation_evidence_id(names["c1"], 1)],
                "affected_cases": [names["c1"]],
                "target_paths": ["skills/SKILL.md"],
                "counterevidence": "c",
                "regression_risks": "r",
                "why_this_layer": "w",
            }
        ],
    }
    reflector = f"print({json.dumps(diagnosis, ensure_ascii=False)!r})\n"
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 1, "attempts_per_round": 2, "attempt_feedback": "sequential"},
        family14_settings={"round_stop": "all_attempts"},
        reflector=reflector,
    )

    assert payloads(run, "reflection_finished")
    assert len(payloads(run, "editor_started")) == 2
    feedback = payloads(run, "attempt_input_frozen")[1]["feedback"]["attempts"][0]
    assert [row["case_id"] for row in feedback["cases"]] == [names["c1"], names["c2"]]
    assert_clean_run(run, snapshot)


async def test_family14_no_finalists_has_null_selection(tmp_path, monkeypatch):
    run, _ = await family14_run(tmp_path, monkeypatch, {**STATISTICAL, "scorer": WORSE_SCORER})
    assert not payloads(run, "finalists_bound")
    (selection,) = payloads(run, "selection_decided")
    assert (selection["reason"], selection["measurement_id"], selection["ranking"]) == (
        "no_finalists",
        None,
        [],
    )
    assert payloads(run, "round_closed")[0]["attempts_opened"] == 1


@pytest.mark.parametrize(
    "mode,reason",
    [("off", "global_off"), ("point", "global_point_improved"), ("bound", "global_bound_positive")],
)
async def test_family14_statistical_global_modes(tmp_path, monkeypatch, mode, reason):
    from tests.campaign_drivers import _statistical

    run, _ = await family14_run(tmp_path, monkeypatch, _statistical(mode))
    (comparison,) = payloads(run, "global_comparison_completed")
    assert (comparison["accepted"], comparison["reason"], comparison["exact"]) == (
        True,
        reason,
        False,
    )
    assert (
        comparison["lower"] is not None
        and comparison["level"] == payloads(run, "finalists_bound")[0]["finalists"][0]["level"]
    )
    assert comparison["champion_side"] == "fresh"
    assert payloads(run, "round_closed")[0]["outcome"] == "promoted"


async def test_family14_equal_exact_final_retains_and_reuses_fresh_champion(tmp_path, monkeypatch):
    from tests.campaign_drivers import _cohort_scorer, _flag_on_finalists_bound, _statistical

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**_statistical("bound"), "scorer": _cohort_scorer(flag, flip=True)},
        campaign={"rounds": 2},
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert (comparison["point"], comparison["lower"], comparison["exact"]) == (0, 0, True)
    assert (comparison["accepted"], comparison["reason"]) == (False, "global_bound_not_positive")
    selection = payloads(run, "selection_decided")[0]
    assert (selection["measurement_id"], selection["reason"]) == (
        "m-r01-final-01-champion",
        "no_accepted_finalist",
    )
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    assert not [r for r in _records(run / "artifacts.jsonl") if r["event"].startswith("promotion_")]


async def test_family14_incomplete_first_precedes_critical_and_retains(tmp_path, monkeypatch):
    from tests.campaign_drivers import (
        EDITED,
        LENIENT,
        _arm_on_finalists_bound,
        _cohort_scorer,
        _CohortFaults,
        _flag_on_finalists_bound,
    )

    flag = tmp_path / "final.flag"
    adapter = _CohortFaults(faults={("candidate", "c1", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    _flag_on_finalists_bound(monkeypatch, flag)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _cohort_scorer(flag, critical=True)},
        adapter=adapter,
        campaign={"indeterminate": LENIENT},
    )
    champion, finalist = payloads(run, "final_measurement_completed")
    assert champion["complete"] is True
    assert (finalist["complete"], finalist["critical"]) == (False, True)
    (comparison,) = payloads(run, "global_comparison_completed")
    assert (
        comparison["accepted"],
        comparison["reason"],
        comparison["unavailable"],
        comparison["exact"],
        comparison["point"],
        comparison["lower"],
        comparison["level"],
    ) == (False, "final_incomplete", "final_incomplete", None, None, None, None)
    assert payloads(run, "selection_decided")[0]["ranking"][0]["score"] is None
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"


async def test_family14_critical_final_vetoes_global_off(tmp_path, monkeypatch):
    from tests.campaign_drivers import _cohort_scorer, _flag_on_finalists_bound, _statistical

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**_statistical("off"), "scorer": _cohort_scorer(flag, critical=True)},
    )
    (comparison,) = payloads(run, "global_comparison_completed")
    assert comparison["point"] is not None
    assert (comparison["accepted"], comparison["reason"]) == (False, "critical_failure")
    assert payloads(run, "selection_decided")[0]["reason"] == "no_accepted_finalist"


async def test_family14_all_attempts_close_before_joint_final_and_ties_use_index(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import TWO_ATTEMPTS, _dispatch_order

    seen = _dispatch_order(monkeypatch)
    run, _ = await family14_run(
        tmp_path, monkeypatch, campaign={**TWO_ATTEMPTS, "attempt_feedback": "sequential"}
    )
    binding = _events(run, "finalists_bound")[0]
    assert binding["sequence"] > max(r["sequence"] for r in _events(run, "attempt_closed"))
    members = binding["payload"]["members"]
    assert members == ["m-r01-final-01-champion", "m-r01-final-01-a01", "m-r01-final-01-a02"]
    assert seen[-1] == [(member, case, 1) for case in ("c1", "c2") for member in members]
    (selection,) = payloads(run, "selection_decided")
    assert [r["attempt_id"] for r in selection["ranking"]] == ["r01-a01", "r01-a02"]
    assert selection["attempt_id"] == "r01-a01"
    assert payloads(run, "round_closed")[0]["attempts_opened"] == 2
    completions = _events(run, "final_measurement_completed")
    comparisons = _events(run, "global_comparison_completed")
    assert max(r["sequence"] for r in completions) < min(r["sequence"] for r in comparisons)
    assert all(p["evidence"] for p in payloads(run, "final_measurement_completed"))
    frozen = payloads(run, "attempt_input_frozen")[1]
    assert frozen["feedback"]["attempts"][0]["outcome"] == "finalist"
    assert frozen["feedback"]["attempts"][0]["reason"] == "observe_finalist"


async def test_family14_unplanned_round_records_zero_attempts(tmp_path, monkeypatch):
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        reflector='print(\'{"status":"declined","note":"nothing to suggest","groups":[]}\')\n',
    )
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == ("unplanned", 0)
    assert not payloads(run, "attempt_opened") and not payloads(run, "finalists_bound")
