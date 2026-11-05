"""H6 row audit of genuine multi-final rounds and coherent journal forgeries."""

from __future__ import annotations

import copy
import hashlib

import pytest

from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import _cut_hook, _events, _records
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_audit import audit, finding, forge
from tests.test_tree_campaign_family14_first_accepted_run import scenario
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("mode", ["observe", "bound"])
async def test_all_final_bindings_and_next_baseline_audit_clean(
    tmp_path, monkeypatch, stability, retained, mode
):
    from tests.campaign_drivers import _statistical

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=3 if retained else 2)
    if mode == "bound":
        options["config"] = {**_statistical(mode), "scorer": options["config"]["scorer"]}
    options["campaign"].update(attempts_per_round=3, rounds=2, attempt_feedback="sequential")
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    journal_consistent(run)
    assert len(payloads(run, "finalists_bound")) >= 3
    assert len(payloads(run, "selection_decided")[0]["ranking"]) == 3
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("recompute", [False, True])
async def test_frozen_feedback_sources_are_independently_audited(tmp_path, monkeypatch, recompute):
    from evolution.tree.campaign_projections import verify_evidence

    options = scenario(tmp_path, monkeypatch, stability="stable")
    options["campaign"]["attempt_feedback"] = "sequential"
    _cut_hook(monkeypatch, "after_attempt_input_frozen", call=2)
    with pytest.raises(Crash):
        await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    frozen = payloads(run, "attempt_input_frozen")[-1]
    assert len(frozen["source_refs"]) == 2
    check = audit if recompute else lambda path: verify_evidence(path, [])
    after = check(run)
    path = run / "campaign.jsonl"
    raw = path.read_bytes()
    path.write_bytes(b"".join(raw.splitlines(keepends=True)[:-1]))
    before = check(run)
    path.write_bytes(raw)
    assert after["checked"] - before["checked"] == len(frozen["source_refs"])
    assert after["preconditions_checked"] - before["preconditions_checked"] == 1


def coherent_forge(run, edit):
    """Rebase outer refs and the frozen-input digest after every edited prefix."""

    frozen = {}

    def rewrite(event, payload):
        keep = edit(event, payload)
        if event == "attempt_input_frozen":
            payload["input_digest"] = hashlib.sha256(
                canonical_json(
                    {
                        key: payload[key]
                        for key in (
                            "common_input_digest",
                            "feedback",
                            "feedback_mode",
                            "source_refs",
                        )
                    }
                )
            ).hexdigest()
            frozen[payload["attempt_id"]] = payload["input_digest"]
        elif event == "editor_started":
            payload["input_digest"] = frozen[payload["attempt_id"]]
        return keep

    forge(run, rewrite)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("position", [1, 2, 3])
async def test_each_historical_global_is_recomputed_with_coherent_downstream_refs(
    tmp_path, monkeypatch, stability, position
):
    options = scenario(tmp_path, monkeypatch, stability=stability)
    options["campaign"]["attempt_feedback"] = "sequential"
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if (
            event == "global_comparison_completed"
            and payload["attempt_id"] == f"r01-a{position:02d}"
        ):
            payload["point"] = 0.123

    coherent_forge(run, edit)
    journal_consistent(run)
    found = audit(run)["mismatches"]
    sequence = _events(run, "global_comparison_completed")[position - 1]["sequence"]
    assert any(
        row["sequence"] == sequence
        and row["code"] == "precondition_failed"
        and "point" in row["detail"]
        for row in found
    ), found
    assert not any(row["code"] in {"suffix_refused", "evidence_mismatch"} for row in found), found


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "corruption",
    [
        "drop_loser",
        "missing",
        "duplicate",
        "historical_member",
        "attempt_count",
        "next_baseline",
        "feedback_reason",
    ],
)
async def test_multifinal_semantic_corruption_reaches_its_own_boundary(
    tmp_path, monkeypatch, stability, corruption
):
    options = scenario(tmp_path, monkeypatch, stability=stability)
    options["campaign"].update(attempt_feedback="sequential", rounds=2)
    run, _ = await family14_run(tmp_path, monkeypatch, **options)

    def edit(event, payload):
        if event == "selection_decided" and payload["round"] == 1:
            if corruption == "drop_loser":
                payload["ranking"] = [
                    row for row in payload["ranking"] if row["attempt_id"] != "r01-a01"
                ]
            elif corruption == "missing":
                payload["evidence"].pop(0)
            elif corruption == "duplicate":
                payload["evidence"][1] = copy.deepcopy(payload["evidence"][0])
        if (
            event == "global_comparison_completed"
            and payload["attempt_id"] == "r01-a02"
            and corruption == "historical_member"
        ):
            payload["finalist_measurement_id"] = "m-r01-final-01-a01"
        if event == "round_closed" and payload["round"] == 1 and corruption == "attempt_count":
            payload["attempts_opened"] -= 1
        if event == "baseline_bound" and payload["round"] == 2 and corruption == "next_baseline":
            payload["measurement_id"] = "m-r01-final-01-a01"
        if (
            event == "attempt_input_frozen"
            and payload["attempt_id"] == "r01-a02"
            and corruption == "feedback_reason"
        ):
            payload["feedback"]["attempts"][0]["reason"] = "observe_finalist"

    coherent_forge(run, edit)
    detail = {
        "drop_loser": "another ranking",
        "missing": "every completion",
        "duplicate": "every completion",
        "historical_member": "members",
        "attempt_count": "attempts_opened",
        "next_baseline": "baseline",
        "feedback_reason": "feedback differs",
    }[corruption]
    finding(run, None, detail, code="suffix_refused")


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("terminal", ["selection_decided", "round_closed"])
async def test_early_close_without_winner_or_k_is_refused(
    tmp_path, monkeypatch, stability, terminal
):
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=3)
    options["campaign"]["attempts_per_round"] = 3
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    first_compared = False
    baseline = payloads(run, "baseline_complete")[0]["measurement_id"]

    def edit(event, payload):
        nonlocal first_compared
        if event == terminal == "round_closed":
            payload["attempts_opened"] = 1
            payload["coverage"]["closure_refs"] = payload["coverage"]["closure_refs"][:1]
            return None
        if event == terminal == "selection_decided":
            count = 1 if stability == "stable" else 2
            payload["ranking"] = [
                row for row in payload["ranking"] if row["attempt_id"] == "r01-a01"
            ]
            payload["evidence"] = (
                payload["evidence"][:count] + payload["evidence"][3 * count : 3 * count + 1]
            )
            payload["measurement_id"] = (
                baseline if stability == "stable" else "m-r01-final-01-champion"
            )
            return None
        if first_compared:
            return False
        if event == "global_comparison_completed":
            first_compared = True
        return None

    coherent_forge(run, edit)
    detail = "winner or K Attempts" if terminal == "selection_decided" else "round has no selection"
    finding(run, None, detail, code="suffix_refused")


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_extra_attempt_after_acceptance_is_refused(tmp_path, monkeypatch, stability):
    options = scenario(tmp_path, monkeypatch, stability=stability)
    _cut_hook(monkeypatch, "after_global_comparison_completed", call=3)
    with pytest.raises(Crash):
        await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    rows = _records(run / "campaign.jsonl")
    extra = copy.deepcopy(_events(run, "attempt_opened")[-1])
    extra["sequence"] = len(rows) + 1
    extra["operation_id"] = "forged-extra-attempt"
    extra["payload"].update(attempt_id="r01-a04", attempt_index=4)
    with (run / "campaign.jsonl").open("ab") as stream:
        stream.write(canonical_json(extra) + b"\n")
    finding(run, None, "accepted finalist stops", code="suffix_refused")


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_early_rebound_and_later_finals_audit_their_own_epochs(
    tmp_path, monkeypatch, stability
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable, _statistical

    options = scenario(tmp_path, monkeypatch, stability=stability)
    options["config"] = {**_statistical("bound"), "scorer": options["config"]["scorer"]}
    options["adapter"] = adapter = _Resettable(used=options["adapter"].used)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound", call=1 if stability == "stable" else 2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    run, _ = await family14_run(
        tmp_path, monkeypatch, authorizations=[_continue(refusal.value)], **options
    )
    comparisons = payloads(run, "global_comparison_completed")
    assert comparisons[0]["finalist_measurement_id"].endswith("@e1")
    assert not comparisons[-1]["finalist_measurement_id"].endswith("@e1")
    journal_consistent(run)
    assert audit(run)["mismatches"] == []


async def test_early_comparison_cannot_substitute_latest_fresh_champion_value(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Campaign

    options = scenario(tmp_path, monkeypatch, stability="unstable")
    ordinal = tmp_path / "ordinal"
    hook = _Campaign.hook

    def final_phase(self, name):
        hook(self, name)
        if name == "before_finalists_bound":
            ordinal.write_text(str(len(self.snapshot().rounds[1].finals) + 1))
        elif name == "after_attempt_input_frozen":
            ordinal.unlink(missing_ok=True)

    monkeypatch.setattr(_Campaign, "hook", final_phase)
    options["config"]["scorer"] = options["config"]["scorer"].replace(
        '"score": score',
        f'"score": float(pathlib.Path({str(ordinal)!r}).read_text()) / 10 '
        f"if not edited and pathlib.Path({str(ordinal)!r}).exists() else score",
    )
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    comparisons = payloads(run, "global_comparison_completed")
    assert len({row["point"] for row in comparisons}) == 3
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event == "global_comparison_completed" and payload["attempt_id"] == "r01-a01":
            payload["point"] = comparisons[-1]["point"]

    coherent_forge(run, edit)
    journal_consistent(run)
    finding(run, "global_comparison_completed", "point")
