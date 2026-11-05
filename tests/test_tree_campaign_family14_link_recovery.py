"""H12a: family-14 link recovery (/15 §15.8) and the probe after an inherited baseline."""

from __future__ import annotations

import copy
import shutil

import pytest

from tests.campaign_drivers import (
    OBSERVE,
    _champion_copy,
    _cut_hook,
    _events,
    _measurement_events,
    _records,
)
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_link_run import (
    _c1_veto,
    _imported,
    assert_clean,
    edit_document,
    link_of,
    predecessor14,
    successor14,
)
from tests.test_tree_campaign_family14_refresh_run import CHAMPION0
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


def _veto():
    """`NO_FINALISTS` whose `c1` also fails by a critical veto until an edit.

    Carried rows bring no scorer traces (PDR-0013; ADR-0017, 2026-10-02), so a
    verdict-only failure is unavailable to focus and a wholly inherited round
    would select `none` and dispatch nothing. A critical veto still counts as
    failing without traces: the successor opens an Attempt, and its first
    locally executed rollout is a Candidate rollout."""

    from tests.campaign_drivers import WORSE_SCORER
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    return {**NO_FINALISTS, "scorer": _c1_veto(WORSE_SCORER)}


async def _predecessor(tmp_path, monkeypatch, config):
    """A finished native family-14 predecessor under `tmp_path / "pred"`."""

    return await predecessor14(tmp_path, monkeypatch, config)


def _seed(tmp_path, predecessor):
    """The predecessor's Champion copy, copied out as the operator's seed root,
    so the same successor command still names an existing seed once the
    predecessor directory is moved away."""

    return shutil.copytree(_champion_copy(predecessor), tmp_path / "seed", symlinks=True)


async def _seeded(tmp_path, monkeypatch, predecessor, seed, **options):
    """`successor14` (`OBSERVE`, inherit) seeded from `seed`; `(run, snapshot)`."""

    with monkeypatch.context() as patch:
        return await family14_run(
            tmp_path / "succ", patch, OBSERVE, linked_from=(predecessor, seed), **options
        )


def _journal(run):
    return (run / "campaign.jsonl").read_bytes()


def _sequence(run, event, **match):
    """The campaign-journal sequences of `event` whose payload has `match`."""

    return [
        r["sequence"]
        for r in _events(run, event)
        if all(r["payload"].get(k) == v for k, v in match.items())
    ]


def _born_records(run, measurement_id):
    from evolution.tree.campaign_measurement import Measurement

    if Measurement.classify(run, measurement_id) != "born":
        return []
    return _records(_measurement_events(run, measurement_id))


def _started(run):
    """`{measurement_id: [rollout_started payloads]}` over the bound measurements
    (`measurement_bound` and `parent_measurement_bound`), in campaign order."""

    bound = [
        p["measurement_id"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] in ("measurement_bound", "parent_measurement_bound")
        for p in [r["payload"]]
    ]
    return {
        mid: [r["payload"] for r in _born_records(run, mid) if r["event"] == "rollout_started"]
        for mid in bound
    }


def _admissions(run):
    """`(measurement_id, operation_id)` of every start carrying an admission."""

    return [
        (mid, start["connection_admission"]["operation_id"])
        for mid, starts in _started(run).items()
        for start in starts
        if start.get("connection_admission") is not None
    ]


# -- §15.8: a crash before the import birth ------------------------------------------


@pytest.mark.parametrize("seam", ["after_campaign_created", "after_measurement_bound"])
async def test_a_crash_before_the_import_birth_is_completed_from_the_predecessor(
    tmp_path, monkeypatch, seam
):
    """The family-14 counterpart of the retired `tests/test_tree_campaign.py`
    `test_a_crash_before_the_birth_is_completed_from_the_predecessor`: the
    declaration is durable, the rows are not; the same command re-reads the
    predecessor and births the whole declared set, with no decision."""

    from evolution.tree.campaign_measurement import Measurement

    predecessor = await _predecessor(tmp_path, monkeypatch, OBSERVE)
    control, control_snapshot = await successor14(tmp_path / "control", monkeypatch, predecessor)
    with monkeypatch.context() as patch:
        _cut_hook(patch, seam)
        with pytest.raises(Crash):
            await successor14(tmp_path, patch, predecessor)
    cut = tmp_path / "succ" / "run"
    declared = link_of(cut)
    assert declared["baseline"]["imported"] > 0
    assert _records(cut / "campaign.jsonl")[-1]["event"] == seam.removeprefix("after_")
    assert Measurement.classify(cut, CHAMPION0) == "absent"
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor)
    assert link_of(run) == declared
    assert len(_imported(run)) == declared["baseline"]["imported"]
    assert not payloads(run, "authorization_applied")
    assert snapshot.finished == control_snapshot.finished
    assert [p["outcome"] for p in payloads(run, "round_closed")] == [
        p["outcome"] for p in payloads(control, "round_closed")
    ]
    assert_clean(run, snapshot)
    assert_clean(control, control_snapshot)


# -- §15.8: a lost source is authorizable and measures fresh --------------------------


async def test_a_lost_source_is_authorizable_and_measures_fresh(tmp_path, monkeypatch):
    """§5.3: the owed copy's source is gone; the resume refuses before any write,
    and the accepted decision is journaled as `fresh_baseline`: the baseline is
    born empty and measured in full, behind the binding's probe."""

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import (
        Authorization,
        baseline_import_instance_digest,
    )

    predecessor = await predecessor14(tmp_path, monkeypatch)
    seed = _seed(tmp_path, predecessor)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_measurement_bound")
        with pytest.raises(Crash):
            await _seeded(tmp_path, patch, predecessor, seed)
    run = tmp_path / "succ" / "run"
    declared = link_of(run)["baseline"]
    assert declared["imported"] > 0
    (tmp_path / "pred").rename(tmp_path / "lost")
    before = _journal(run)
    with pytest.raises(EvolveRefused) as refused:
        await _seeded(tmp_path, monkeypatch, predecessor, seed)
    assert refused.value.code == "baseline_import_unavailable"
    (created,) = _events(run, "campaign_created")
    digest = baseline_import_instance_digest(created["run_id"], declared["pairs_digest"])
    assert refused.value.instance_digest == digest
    assert _journal(run) == before

    decision = Authorization.new("baseline_import_unavailable", digest, "gone")
    run, snapshot = await _seeded(
        tmp_path, monkeypatch, predecessor, seed, authorizations=[decision]
    )
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "fresh_baseline"
    assert applied["authorization"]["id"] == decision.id
    assert applied["pairs_digest"] == declared["pairs_digest"]
    assert not _imported(run)
    # Measured in full: every scheduled sample of the baseline ran here.
    champion = _records(_measurement_events(run, CHAMPION0))
    schedule = champion[0]["payload"]["identity"]["schedule"]
    started = _started(run)[CHAMPION0]
    assert {(s["case_id"], s["sample_id"]) for s in started} == {
        (e["case_id"], e["sample_id"]) for e in schedule
    }
    assert [p["measurement_id"] for p in payloads(run, "baseline_complete")][0] == CHAMPION0
    # The probe precedes the baseline's first rollout: that start, the only
    # admitted one, names the compatible preflight, which the campaign journal
    # holds before the baseline's stage opens.
    (start,) = payloads(run, "connection_preflight_started")
    (finish,) = payloads(run, "connection_preflight_finished")
    assert finish["status"] == "compatible"
    assert _admissions(run) == [(CHAMPION0, start["operation_id"])]
    assert started[0]["connection_admission"]["operation_id"] == start["operation_id"]
    (finished_at,) = _sequence(run, "connection_preflight_finished")
    assert finished_at < min(_sequence(run, "stage_opened", measurement_id=CHAMPION0))
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


# -- Review focus 5: a finished successor never reads its predecessor again -----------


async def test_a_finished_successor_never_reads_its_predecessor_again(tmp_path, monkeypatch):
    """§5.2, §7: once the successor finished, nothing is owed; the same command
    with the predecessor moved away neither refuses nor writes."""

    predecessor = await predecessor14(tmp_path, monkeypatch)
    seed = _seed(tmp_path, predecessor)
    run, snapshot = await _seeded(tmp_path, monkeypatch, predecessor, seed)
    assert snapshot.finished is not None
    assert len(_imported(run)) == link_of(run)["baseline"]["imported"] > 0
    before = _journal(run)
    (tmp_path / "pred").rename(tmp_path / "moved")
    again, again_snapshot = await _seeded(tmp_path, monkeypatch, predecessor, seed)
    assert again == run
    assert again_snapshot.finished == snapshot.finished
    assert again_snapshot == snapshot
    assert _journal(run) == before
    assert not predecessor.exists() and (tmp_path / "moved" / "run").is_dir()
    assert_clean(run, again_snapshot)


# -- ADR-0029 amendment 2026-10-09: the probe after a wholly inherited baseline -------


async def test_a_wholly_inherited_baseline_probes_before_the_first_candidate_rollout(
    tmp_path, monkeypatch
):
    """The probe runs before the first locally executed rollout of the binding;
    with the whole baseline inherited that is a Candidate rollout.

    `ensure_connection` is the first statement of `run_stage`, so the probe
    follows the Candidate's `measurement_bound` (and its Parent's and the
    comparison's binds) and precedes its first `stage_opened`: the ordering
    is pinned at `stage_opened`, and the Candidate's first start carries the
    probe's admission."""

    predecessor = await _predecessor(tmp_path, monkeypatch, _veto())
    assert [p["outcome"] for p in payloads(predecessor, "round_closed")] == ["stalled"]
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor, _veto())
    baseline = link_of(run)["baseline"]
    schedule = _records(_measurement_events(run, CHAMPION0))[0]["payload"]["identity"]["schedule"]
    assert baseline["imported"] == baseline["scheduled"] == len(schedule)
    assert len(_imported(run)) == baseline["imported"]
    started = _started(run)
    assert started[CHAMPION0] == []
    (start,) = _sequence(run, "connection_preflight_started")
    (finish,) = _sequence(run, "connection_preflight_finished")
    first = next(mid for mid, starts in started.items() if starts)
    assert first == "m-r02-a01"
    (bound,) = _sequence(run, "measurement_bound", measurement_id=first)
    opened = min(_sequence(run, "stage_opened", measurement_id=first))
    assert bound < start < finish < opened
    (operation,) = payloads(run, "connection_preflight_started")
    assert _admissions(run) == [(first, operation["operation_id"])]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


async def test_an_incompatible_contract_fails_the_probe_after_a_whole_import(tmp_path, monkeypatch):
    """The production contract (`assistant_message` `/content`, which
    `FakeInstance` does not emit): the whole baseline is imported without a
    check, and the probe at the first Candidate rollout fails and suspends."""

    from tests.tree.manifest.test_manifest_v15 import METRIC_CONTRACT

    predecessor = await predecessor14(tmp_path, monkeypatch, _veto())
    with monkeypatch.context() as patch:
        edit_document(
            patch, lambda d: d["scorer"].update(metric_contract=copy.deepcopy(METRIC_CONTRACT))
        )
        run, snapshot = await successor14(tmp_path, patch, predecessor, _veto())
    link = link_of(run)
    assert link["condition_changed"] == ["identity.metric_contract"]
    assert link["baseline"]["imported"] == link["baseline"]["scheduled"] == len(_imported(run))
    assert snapshot.finished is None
    (failed,) = payloads(run, "connection_preflight_failed")
    assert (failed["status"], failed["detail_code"], failed["manifest_path"]) == (
        "connection_incompatible",
        "connection_event_missing",
        "scorer.metric_contract.target_fields[0]",
    )
    (suspended,) = payloads(run, "campaign_suspended")
    (failed_at,) = _sequence(run, "connection_preflight_failed")
    assert suspended["owner"] == "connection"
    assert suspended["reason"] == "connection_incompatible"
    assert suspended["evidence"]["seq"] == failed_at
    # Nothing past it: the suspension is the last record, no Candidate stage
    # opened, and no measurement journal holds a rollout start.
    records = _records(run / "campaign.jsonl")
    assert [r["event"] for r in records[-2:]] == [
        "connection_preflight_failed",
        "campaign_suspended",
    ]
    assert {p["measurement_id"] for p in payloads(run, "stage_opened")} == {CHAMPION0}
    assert not any(_started(run).values())
    assert_clean(run, snapshot)


# -- H10 choice 19: no Candidate inspection beside the probe after a whole import ----


async def test_h10_choice_19_no_inspection_runs_beside_the_probe_after_a_whole_import(
    tmp_path, monkeypatch
):
    """Choice 19's wait (an inspection holds while a probe holds an instance) is
    unreachable in a wholly inherited link: every baseline row is a cross-run
    `sample_imported` naming the predecessor's component map, which no journal
    of this run declares, so the selection falls back `components_unavailable`
    before any inspection. The same two parallel Attempts inspect when the
    baseline is measured locally (the predecessor)."""

    from tests.campaign_drivers import MAP_FILE, TWO_ATTEMPTS

    campaign = {**TWO_ATTEMPTS, "parallel_attempts": 2, "attempt_feedback": "none"}
    options = {"components": MAP_FILE, "campaign": campaign}
    predecessor = await predecessor14(tmp_path, monkeypatch, _veto(), **options)
    assert len(payloads(predecessor, "candidate_inspected")) == 2
    assert {p["fallback_reason"] for p in payloads(predecessor, "affected_cases_selected")} == {
        None
    }
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor, _veto(), **options)
    baseline = link_of(run)["baseline"]
    assert baseline["imported"] == baseline["scheduled"] == len(_imported(run))
    assert {r["payload"]["source"]["run_id"] for r in _imported(run)} == {
        _events(predecessor, "campaign_created")[0]["run_id"]
    }
    assert all(r["payload"].get("components_digest") for r in _imported(run))
    selections = payloads(run, "affected_cases_selected")
    assert sorted((p["attempt"], p["mode"], p["fallback_reason"]) for p in selections) == [
        ("r02-a01", "component", "components_unavailable"),
        ("r02-a02", "component", "components_unavailable"),
    ]
    assert not payloads(run, "candidate_inspected")
    assert not (run / "inspections").exists()
    assert len(payloads(run, "connection_preflight_started")) == 1
    assert len(payloads(run, "connection_preflight_finished")) == 1
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


# -- B3: code drift under an open frozen Attempt (option A) ---------------------


async def test_b3_projection_drift_after_a_version_rebase_raises_and_the_old_release_recovers(
    tmp_path, monkeypatch
):
    """A release that changes the controller's view (here only the view seam
    moves; the manifest is unchanged) is a version-only identity change, which
    offers only `rebase`. Accepting it journals the rebase, then `run_editor`
    recomputes `D` for the Attempt frozen before the upgrade and raises the
    existing invariant before `editor_started`. Downgrading to the old release
    is another version-only change: accepting its `rebase` continues the same
    Attempt under the same frozen input, and the run completes clean."""

    from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
    from evolution.tree.roles.role_visibility import RoleVisibilityView
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run
    from tests.test_tree_campaign_family14_visibility_change import _decision, _refused, _version
    from tests.test_tree_campaign_family14_visibility_run import (
        READABLE,
        WRITABLE,
        _arrange,
        _HiddenAware,
        _listings,
        _visible_entries,
    )

    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    before = _journal(run)
    ledger = [r["event"] for r in _records(run / "artifacts.jsonl")]
    moved = RoleVisibilityView(readable_paths=(), writable_paths=WRITABLE)
    with monkeypatch.context() as patch:
        _version(patch, "0.3.0a99")
        patch.setattr(_Campaign, "role_view", property(lambda self: moved))
        upgrade = await _refused(tmp_path, monkeypatch, adapter=adapter)
        assert upgrade.code == "run_identity_changed"
        # A version-only change: `close` is not offered.
        assert upgrade.effects == ("rebase",)
        assert [d.field for d in upgrade.differences] == ["producing_version"]
        assert _journal(run) == before
        accepted = _decision(upgrade, None, reason="accept the new release")
        with pytest.raises(CampaignInvariantViolated, match="frozen editor common input changed"):
            await family14_run(tmp_path, monkeypatch, adapter=adapter, authorizations=[accepted])
        after = _journal(run)
        assert after.startswith(before)
        appended = [r["event"] for r in _records(run / "campaign.jsonl")][
            len(before.splitlines()) :
        ]
        # Only the rebase is journaled; the raise comes before `editor_started`.
        assert appended == ["identity_rebased"]
        (rebased,) = payloads(run, "identity_rebased")
        assert rebased["changed_fields"] == ["producing_version"]
        # The rebase moves the artifact store too, and `run_editor`
        # materializes the Attempt workspace before the check.
        assert [r["event"] for r in _records(run / "artifacts.jsonl")] == [
            *ledger,
            "store_rebased",
            "workspace_opened",
            "workspace_ready",
        ]
        # The accepted `rebase` is a trap: every resume on the new release raises.
        opened = (run / "artifacts.jsonl").read_bytes()
        with pytest.raises(CampaignInvariantViolated, match="frozen editor common input changed"):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
        assert _journal(run) == after
        assert (run / "artifacts.jsonl").read_bytes() == opened
    assert payloads(run, "attempt_input_frozen") == [frozen]
    assert not payloads(run, "editor_started")
    assert _listings(listing) == []

    # The operator downgrades: the original release and view.
    downgrade = await _refused(tmp_path, monkeypatch, adapter=adapter)
    assert downgrade.code == "run_identity_changed"
    assert downgrade.effects == ("rebase",)
    assert [d.field for d in downgrade.differences] == ["producing_version"]
    assert _journal(run) == after
    restored = _decision(downgrade, None, reason="finish the Attempt on the old release")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=adapter, authorizations=[restored]
    )
    assert snapshot.finished["outcome"] == "completed"
    assert [p["changed_fields"] for p in payloads(run, "identity_rebased")] == [
        ["producing_version"],
        ["producing_version"],
    ]
    # The same Attempt continues under the same frozen input.
    assert payloads(run, "attempt_input_frozen") == [frozen]
    (started,) = payloads(run, "editor_started")
    assert started["attempt_id"] == frozen["attempt_id"]
    assert _listings(listing) == [_visible_entries(READABLE)]
    assert_clean_run(run, snapshot)


# -- Review M1: the successor-family context survives a rebase of the successor ------


async def test_a_bare_metric_contract_declaration_survives_a_rebase_of_the_successor(
    tmp_path, monkeypatch
):
    """`linked_from` is never re-derived on resume: the identity check re-reads
    the latest identity's declaration under that identity's own family
    context. Here the latest is the successor's `identity_rebased`, whose
    linked-baseline declaration names the bare `metric_contract` (Task 5's
    genuine predecessor). The successor is cut after round 2 closed, its
    stability moves to `stable` (a comparable policy change, refused
    `run_identity_changed`), the accepted `rebaseline` is cut after round 3's
    baseline is bound, and the plain resume re-parses the rebased identity."""

    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_link_run import (
        MESSAGE_CONTRACT,
        _assert_predecessor_clean,
        _rebased14,
    )
    from tests.test_tree_campaign_family14_refresh_recovery import _decision, _journal_has

    def contract(document):
        document["scorer"]["metric_contract"] = copy.deepcopy(MESSAGE_CONTRACT)

    stop = _journal_has(tmp_path / "pred" / "run", b'"event":"identity_rebased"', 1)
    predecessor = await _rebased14(tmp_path, monkeypatch, None, edit=contract, cancelled=stop)
    rounds = {"campaign": {"rounds": 2}}
    stable = {"stability": "stable"}

    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await successor14(tmp_path, patch, predecessor, **rounds)
    run = tmp_path / "succ" / "run"
    declared = link_of(run)
    assert declared["baseline"]["fields"] == ["metric_contract"]
    assert [p["round"] for p in payloads(run, "round_closed")] == [2]
    assert not _events(run, "campaign_finished")

    before = _journal(run)
    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        with pytest.raises(EvolveRefused) as refused:
            await successor14(tmp_path, patch, predecessor, family14_settings=stable, **rounds)
    assert refused.value.code == "run_identity_changed"
    assert _journal(run) == before
    decision = _decision(refused.value, "rebaseline")

    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        _cut_hook(patch, "after_baseline_bound")
        with pytest.raises(Crash):
            await successor14(
                tmp_path,
                patch,
                predecessor,
                family14_settings=stable,
                authorizations=[decision],
                **rounds,
            )
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["identity"]["policy"]["gate"]["stability"] == "stable"
    assert rebased["identity"]["linked_from"] == declared
    assert rebased["identity"]["linked_from"]["baseline"]["fields"] == ["metric_contract"]
    assert [p["round"] for p in payloads(run, "baseline_bound")][-1] == 3
    assert not _events(run, "campaign_finished")

    # The plain resume: the latest identity is the rebase, re-parsed at the
    # identity check with its family context; no further decision is owed.
    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        resumed, snapshot = await successor14(
            tmp_path, patch, predecessor, family14_settings=stable, **rounds
        )
    assert resumed == run
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "rebaseline"
    assert payloads(run, "identity_rebased") == [rebased]
    assert link_of(run) == declared
    assert [p["round"] for p in payloads(run, "round_closed")] == [2, 3]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)
    _assert_predecessor_clean(predecessor)
