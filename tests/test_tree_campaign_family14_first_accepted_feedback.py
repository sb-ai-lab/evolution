"""H6 frozen safe feedback over real finalist comparisons and exact citations."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from evolution.tree.campaign import _Campaign
from evolution.tree.campaign_schema import (
    SchemaError,
    effective_attempt_feedback,
    feedback_outcomes,
    validate_payload,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_evidence import project_attempt_feedback, project_history
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    _arm_on_finalists_bound,
    _cohort_scorer,
    _CohortFaults,
    _cut_hook,
    _events,
    _records,
    _statistical,
)
from tests.campaign_fakes import Crash, Family14Life
from tests.test_tree_campaign_family14_first_accepted_run import assert_unopened_absent, scenario
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_editor_attempt_feedback import _sources

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("feedback", ["none", "sequential"])
async def test_mixed_decline_gate_loss_global_loss_then_winner(
    tmp_path, monkeypatch, stability, feedback
):
    from tests import campaign_drivers as fixtures
    from tests.tree_evolve_support import EDITOR_SCRIPT

    counter = tmp_path / "editor.calls"
    editor = EDITOR_SCRIPT.replace(
        'skill = pathlib.Path(request["workspace"])',
        f"counter = pathlib.Path({str(counter)!r})\n"
        "calls = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(calls))\n"
        "if calls == 1:\n"
        '    print(json.dumps({"status": "declined", "note": "skip"}))\n'
        "    sys.exit(0)\n"
        'skill = pathlib.Path(request["workspace"])',
    )
    workspace = fixtures.write_workspace

    def write_workspace(*args, **kwargs):
        return workspace(*args, **{**kwargs, "editor_script": editor})

    monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=0)
    options["campaign"]["attempt_feedback"] = feedback
    original = _Campaign.hook
    flag = tmp_path / "critical.flag"

    def hook(self, name):
        original(self, name)
        if name in ("after_attempt_input_frozen", "before_finalists_bound"):
            index = len(self.snapshot().rounds[1].attempts)
            critical = index == (2 if name == "after_attempt_input_frozen" else 3)
            if critical:
                flag.touch()
            else:
                flag.unlink(missing_ok=True)

    monkeypatch.setattr(_Campaign, "hook", hook)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    closes = payloads(run, "attempt_closed")
    assert [(row["outcome"], row["reason"]) for row in closes] == [
        ("abandoned", "editor_declined"),
        ("rejected", "critical_failure"),
        ("finalist", "observe_finalist"),
        ("finalist", "observe_finalist"),
    ]
    assert [row["accepted"] for row in payloads(run, "global_comparison_completed")] == [
        False,
        True,
    ]
    assert counter.read_text() == "4"
    assert_unopened_absent(run, opened=4, k=5)
    inputs = payloads(run, "attempt_input_frozen")
    assert len(inputs) == len(payloads(run, "editor_started")) == 4
    assert len({row["common_input_digest"] for row in inputs}) == 1
    assert len({row["parent_digest"] for row in payloads(run, "attempt_opened")}) == 1
    final = inputs[-1]
    if feedback == "none":
        assert all(row["feedback"] == {"attempts": [], "omitted_attempts": 0} for row in inputs)
        assert all(row["source_refs"] == [] for row in inputs)
    else:
        assert [row["reason"] for row in final["feedback"]["attempts"]] == [
            "editor_declined",
            "critical_failure",
            "critical_failure",
        ]
        assert len(final["source_refs"]) == 4
    assert_clean_run(run, snapshot)


def feedback_options(tmp_path, monkeypatch, *, stability="stable", reason="critical_failure", k=2):
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=k)
    options["campaign"].update(attempts_per_round=k, attempt_feedback="sequential")
    if reason.startswith("global_"):
        options.pop("adapter")
        mode = "point" if reason == "global_point_not_improved" else "bound"
        options["config"] = {
            **_statistical(mode),
            "scorer": _cohort_scorer(tmp_path / "critical.flag", flip=True),
        }
    elif reason == "final_incomplete":
        adapter = _CohortFaults(faults={("candidate", "c2", 1): "time_budget"}, used=EDITED["used"])
        _arm_on_finalists_bound(monkeypatch, adapter)
        original = _Campaign.hook

        def hook(self, name):
            if name == "after_attempt_input_frozen":
                adapter.armed = False
            original(self, name)

        monkeypatch.setattr(_Campaign, "hook", hook)
        options["adapter"] = adapter
        options["campaign"]["indeterminate"] = LENIENT
    return options


def assert_sources(run, frozen):
    records = _records(run / "campaign.jsonl")
    life = Family14Life(records[: _events(run, "attempt_input_frozen")[-1]["sequence"] - 1])
    expected = []
    for row in frozen["feedback"]["attempts"]:
        for event in ("attempt_closed", "global_comparison_completed"):
            expected.append(life.reference(life.sequence_of(event, attempt_id=row["attempt_id"])))
    assert frozen["source_refs"] == expected
    assert (
        frozen["input_digest"]
        == hashlib.sha256(
            canonical_json(
                {
                    key: frozen[key]
                    for key in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
                }
            )
        ).hexdigest()
    )


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "reason",
    [
        "global_point_not_improved",
        "global_bound_not_positive",
        "critical_failure",
        "final_incomplete",
    ],
)
async def test_next_input_has_safe_global_loss_and_exact_close_then_global_sources(
    tmp_path, monkeypatch, stability, reason
):
    options = feedback_options(tmp_path, monkeypatch, stability=stability, reason=reason)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    inputs = payloads(run, "attempt_input_frozen")
    assert len(inputs) == 2
    row = inputs[1]["feedback"]["attempts"][0]
    assert (row["outcome"], row["reason"]) == ("finalist", reason)
    assert_sources(run, inputs[1])
    closes = payloads(run, "attempt_closed")
    assert {close["outcome"] for close in closes} == {"finalist"}
    assert all(close["reason"] in {"gate_passed", "observe_finalist"} for close in closes)
    assert len({entry["common_input_digest"] for entry in inputs}) == 1
    assert len({entry["parent_digest"] for entry in payloads(run, "attempt_opened")}) == 1
    assert snapshot.rounds[1].attempts["r01-a01"].reason == closes[0]["reason"]


@pytest.mark.parametrize(
    "reason",
    [
        "global_point_not_improved",
        "global_bound_not_positive",
        "critical_failure",
        "final_incomplete",
    ],
)
def test_pure_projection_can_show_all_safe_settled_losses_without_global_payload(tmp_path, reason):
    """Projection-only payload isolation; genuine reason cases are tested above."""
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    attempt = replace(attempt, outcome="finalist", reason="observe_finalist", looks={1: {}})
    state = replace(
        state,
        attempts={attempt.attempt_id: attempt},
        global_comparisons={
            attempt.attempt_id: {
                "accepted": False,
                "reason": reason,
                "point": 0.918273645,
                "lower": -0.817263544,
                "private": "PRIVATE_CANARY",
                "oracle": "ORACLE_CANARY",
            }
        },
    )
    kwargs.update(outcomes=feedback_outcomes(14), family_version=14)
    section = project_attempt_feedback(state, **kwargs)
    assert section["attempts"][0]["reason"] == reason
    assert section["attempts"][0]["cases"][0]["delta"] == -1.0
    assert not any(
        value in str(section)
        for value in ("0.918273645", "0.817263544", "PRIVATE_CANARY", "ORACLE_CANARY")
    )


@pytest.mark.parametrize("family,accepted", [(14, True)])
def test_pure_projection_retains_gate_reason_after_acceptance_or_in_earlier_family(
    tmp_path, family, accepted
):
    """An input after an accepted finalist cannot occur in a real first-accepted round."""
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    outcome = "finalist" if family == 14 else "accepted"
    attempt = replace(attempt, outcome=outcome, reason="observe_finalist", looks={1: {}})
    state = replace(
        state,
        attempts={attempt.attempt_id: attempt},
        global_comparisons={
            attempt.attempt_id: {
                "accepted": accepted,
                "reason": "global_off" if accepted else "global_point_not_improved",
            }
        },
    )
    kwargs.update(outcomes=feedback_outcomes(family), family_version=family)
    assert project_attempt_feedback(state, **kwargs)["attempts"][0]["reason"] == "observe_finalist"


async def test_eight_retained_losers_have_sixteen_refs_and_omit_older_attempts(
    tmp_path, monkeypatch
):
    options = feedback_options(tmp_path, monkeypatch, k=10)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen", call=10)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    inputs = payloads(run, "attempt_input_frozen")
    assert len(inputs[8]["feedback"]["attempts"]) == 8
    assert len(inputs[8]["source_refs"]) == 16
    validate_payload("attempt_input_frozen", inputs[8], final=True)
    oversized = copy.deepcopy(inputs[8])
    oversized["source_refs"].append(copy.deepcopy(oversized["source_refs"][-1]))
    with pytest.raises(SchemaError, match="source_refs limit"):
        validate_payload("attempt_input_frozen", oversized, final=True)
    frozen = inputs[9]
    assert [row["index"] for row in frozen["feedback"]["attempts"]] == list(range(2, 10))
    assert frozen["feedback"]["omitted_attempts"] == 1
    assert_sources(run, frozen)
    assert len(canonical_json(_events(run, "attempt_input_frozen")[-1])) < 1024 * 1024


@pytest.mark.parametrize(
    "forgery",
    [
        "absent",
        "duplicate",
        "reordered",
        "substituted",
        "locator",
        "close_locator",
        "digest",
        "run_id",
    ],
)
async def test_lifecycle_refuses_changed_comparison_sources_even_with_renewed_input_digest(
    tmp_path, monkeypatch, forgery
):
    options = feedback_options(tmp_path, monkeypatch)
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    record = _events(run, "attempt_input_frozen")[1]
    life = Family14Life(_records(run / "campaign.jsonl")[: record["sequence"] - 1])
    frozen = copy.deepcopy(record["payload"])
    refs = frozen["source_refs"]
    if forgery == "absent":
        refs.pop()
    elif forgery == "duplicate":
        refs.append(copy.deepcopy(refs[-1]))
    elif forgery == "reordered":
        refs.reverse()
    elif forgery == "substituted":
        refs[-1] = life.reference(life.sequence_of("final_measurement_completed"))
    elif forgery == "locator":
        refs[-1]["locator"] = "campaign.jsonl#bytes=0:1"
    elif forgery == "close_locator":
        refs[0]["locator"] = "campaign.jsonl#bytes=0:1"
    elif forgery == "digest":
        refs[-1]["content_digest"] = "f" * 64
    else:
        refs[-1]["run_id"] = "other-run"
    frozen["input_digest"] = hashlib.sha256(
        canonical_json(
            {
                key: frozen[key]
                for key in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
            }
        )
    ).hexdigest()
    with pytest.raises(SchemaError, match="frozen feedback source"):
        life.accept("attempt_input_frozen", **frozen)


@pytest.mark.parametrize(
    "stop,feedback", [("all_attempts", "sequential"), ("first_accepted", "none")]
)
async def test_all_attempts_uses_gate_reason_and_none_stays_empty(
    tmp_path, monkeypatch, stop, feedback
):
    options = feedback_options(tmp_path, monkeypatch)
    options["family14_settings"]["round_stop"] = stop
    options["campaign"]["attempt_feedback"] = feedback
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    frozen = payloads(run, "attempt_input_frozen")[1]
    if feedback == "none":
        assert frozen["feedback"] == {"attempts": [], "omitted_attempts": 0}
        assert frozen["source_refs"] == []
    else:
        assert frozen["feedback"]["attempts"][0]["reason"] == "observe_finalist"
        assert len(frozen["source_refs"]) == 1
    assert payloads(run, "global_comparison_completed")[0]["reason"] == "critical_failure"


async def test_frozen_bytes_survive_resume_and_editor_and_history_only_see_safe_reason(
    tmp_path, monkeypatch
):
    import evolution.tree.campaign as campaign_module
    from evolution.tree.roles.editor_contracts import brief_payload, history_payload
    from tests import campaign_drivers as fixtures

    real_workspace = fixtures.write_workspace

    def workspace(*args, **kwargs):
        rows = copy.deepcopy(kwargs["rows"])
        for row in rows:
            row["private"] = {"secret": "PRIVATE_CANARY"}
            row["oracle"]["expected"] = "ORACLE_CANARY"
        return real_workspace(*args, **{**kwargs, "rows": rows})

    monkeypatch.setattr(fixtures, "write_workspace", workspace)

    options = feedback_options(tmp_path, monkeypatch, k=3)
    options["config"]["scorer"] = options["config"]["scorer"].replace(
        '"score": score',
        f'"score": 0.918273645 if pathlib.Path({str(tmp_path / "critical.flag")!r}).exists() '
        "and edited else score",
    )
    requests = {}
    histories = []
    real_editor = campaign_module.run_editor
    original_hook = _Campaign.hook

    async def record_editor(backend, request, **kwargs):
        requests[request.attempt_id] = brief_payload(request.brief)
        return await real_editor(backend, request, **kwargs)

    def hook(self, name):
        if name == "after_round_closed":
            histories.append(
                history_payload(
                    project_history(
                        self.snapshot(),
                        measurements=self.measurements,
                        store=self.store,
                        artifact_policy=self.artifact_policy,
                        corpus=self.corpus,
                        policy=self.policy,
                    )
                )
            )
        original_hook(self, name)

    monkeypatch.setattr(campaign_module, "run_editor", record_editor)
    monkeypatch.setattr(_Campaign, "hook", hook)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    before = next(
        line
        for line in (run / "campaign.jsonl").read_bytes().splitlines()
        if json.loads(line)["event"] == "attempt_input_frozen"
        and json.loads(line)["payload"]["attempt_id"] == "r01-a02"
    )
    frozen = payloads(run, "attempt_input_frozen")[1]
    assert "r01-a02" not in requests
    real_projection = campaign_module.project_attempt_feedback

    def projection(*args, **kwargs):
        assert kwargs["current_index"] != 2, "resume recomputed an already-frozen input"
        return real_projection(*args, **kwargs)

    monkeypatch.setattr(campaign_module, "project_attempt_feedback", projection)
    # The crash intercepted the hook before scenario() could clear its final
    # scoring flag. Restore the fixture's gate phase before replaying Editor 2.
    (tmp_path / "critical.flag").unlink(missing_ok=True)
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    after = [
        line
        for line in (run / "campaign.jsonl").read_bytes().splitlines()
        if json.loads(line)["event"] == "attempt_input_frozen"
        and json.loads(line)["payload"]["attempt_id"] == "r01-a02"
    ]
    assert after == [before]
    assert requests["r01-a02"]["attempt_feedback"] == frozen["feedback"]
    assert {row["reason"] for row in histories[0]["rounds"][0]["attempts"]} == {"critical_failure"}
    assert "0.918273645" in str(payloads(run, "final_measurement_completed"))
    assert "0.918273645" not in str(requests)
    assert "0.918273645" not in str(histories)
    assert not any(
        value in str(requests) or value in str(histories)
        for value in ("PRIVATE_CANARY", "ORACLE_CANARY")
    )
    common = {key: value for key, value in requests["r01-a01"].items() if key != "attempt_feedback"}
    assert all(
        {key: value for key, value in request.items() if key != "attempt_feedback"} == common
        for request in requests.values()
    )


async def test_byte_limited_feedback_cites_only_retained_whole_rows(tmp_path, monkeypatch):
    from evolution.tree import attempt_feedback

    # Exercise the same byte-bound path with a small budget; the production
    # 128 KiB cap and its full-size long-path coverage remain unchanged.
    monkeypatch.setattr(attempt_feedback, "_SECTION_LIMIT", 1800)
    options = feedback_options(tmp_path, monkeypatch, k=6)
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    frozen = payloads(run, "attempt_input_frozen")[-1]
    rows = frozen["feedback"]["attempts"]
    assert 0 < len(rows) < 5
    assert frozen["feedback"]["omitted_attempts"] == 5 - len(rows)
    assert len(canonical_json(frozen["feedback"])) <= 1800
    assert len(frozen["source_refs"]) == 2 * len(rows)
    assert_sources(run, frozen)


@pytest.mark.parametrize(
    "editor_reason,family,expected",
    [
        ("changed_file_budget", 14, "changed_file_budget"),
        ("written_byte_budget", 14, "written_byte_budget"),
        ("actor_violation", 14, "editor_failed"),
        ("protocol_exhausted", 14, "editor_failed"),
    ],
)
def test_a_budget_failure_reports_its_code_in_family_14_only(
    tmp_path, editor_reason, family, expected
):
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    attempt = replace(
        attempt,
        outcome="abandoned",
        reason="editor_failed",
        editor="failed",
        editor_error="reported",
        editor_reason=editor_reason,
        candidate_digest=None,
        measurement_id=None,
        looks={},
    )
    state = replace(state, attempts={attempt.attempt_id: attempt})
    assert effective_attempt_feedback(state, attempt, family_version=family) == (expected, None)
    if family == 14:
        kwargs.update(outcomes=feedback_outcomes(14), family_version=14)
        row = project_attempt_feedback(state, **kwargs)["attempts"][0]
        assert (row["outcome"], row["reason"]) == ("abandoned", expected)
        assert (row["changes_status"], row["changed_paths"], row["diff_excerpt"]) == (
            "unavailable",
            [],
            "",
        )
        assert "rejection_layer" not in row
