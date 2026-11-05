"""H7 Task 8: recovery and authorization at every family-14 refresh boundary.

Every run is a genuine `family14_run` engine invocation, stopped before H12's
public report assembly. A crash cut raises `Crash` (or the uncatchable
`_PowerCut`) at a refresh boundary, and the same command resumes the same run
directory. Each resumed run replays (`journal_consistent`), audits clean and
is compared with an uncut control of its scenario by event/naming order,
ledger transactions and row identities (H3's `_facts`), never by
independently seeded numerical payloads.

Cuts at production hooks use `_cut_hook`. The three boundaries with no
production hook -- after `baseline_bound` before `measurement_bound`, after
`pairs_excluded` and after `baseline_complete` -- use the test-only
`_cut_after_append`, which raises right after a real append and decides
nothing (plan decision 9).
"""

from __future__ import annotations

import sys
from collections import Counter

import pytest

from evolution.tree.campaign import _Campaign
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    MANIFEST_SEED,
    SERIAL,
    _cut_hook,
    _facts,
    _measurement_events,
    _PowerCut,
    _records,
    _ResettableTagged,
    _starts,
)
from tests.campaign_fakes import Crash, crash_after_the_stage_once
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS, _event_order
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

REFRESH = "m-r02-refresh"


def _cut_after_append(monkeypatch, event, *, call=1):
    """Raise `Crash` right after the `call`-th real append of campaign `event`.

    Test-only (plan decision 9): the record is durable, nothing after it is.
    """

    original, seen = _Campaign.append, []

    def append(self, name, payload, operation_id=None):
        written = original(self, name, payload, operation_id)
        if name == event:
            seen.append(name)
            if len(seen) == call:
                raise Crash("power cut")
        return written

    monkeypatch.setattr(_Campaign, "append", append)
    return seen


def _refresh_open(run):
    """A refresh is bound and not yet completed, read from the durable journal."""

    data = (run / "campaign.jsonl").read_bytes()
    return data.count(b'"event":"baseline_refresh_bound"') > data.count(
        b'"event":"baseline_refresh_completed"'
    )


class _RefreshTarget(_ResettableTagged):
    """`_ResettableTagged` whose Champion sessions, while a refresh is open in
    the run's own journal, play `faults[(case_id, repeat)]`; `cut = (case_id,
    repeat)` cuts the power once at that session's first send (after its
    `rollout_started` is durable). Clearing `faults` repairs the Target."""

    def __init__(self, run, *, faults=None, **kwargs):
        super().__init__(**kwargs)
        self.run = run
        self.faults = dict(faults or {})
        self.cut = None

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind != "champion":
            return instance
        lookup, send = instance._lookup, instance.send

        def key(session):
            return session.case_id, session.requested_seed - MANIFEST_SEED

        def scripted(session):
            if _refresh_open(self.run) and key(session) in self.faults:
                return self.faults[key(session)]
            return lookup(session)

        async def cutting(session, message, request_id):
            if self.cut is not None and _refresh_open(self.run) and key(session) == self.cut:
                self.cut = None
                raise _PowerCut("power cut")
            return await send(session, message, request_id)

        instance._lookup = scripted
        instance.send = cutting
        return instance


#: The shared crash scenario: `NO_FINALISTS` (every round `stalled`), N = 1,
#: two rounds, one try at a time. Round 2 refreshes `m-r00-champion`; its
#: Champion instance exhausts (c2, 1) below the lenient thresholds, so the
#: refresh writes `pairs_excluded` before `baseline_complete` (ADR-0016).
EXHAUSTED = {("c2", 1): "target_failed"}


def _target(root, faults=EXHAUSTED):
    return _RefreshTarget(root / "run", faults=faults, used=EDITED["used"])


async def _scenario(root, monkeypatch, adapter, *, rounds=2, refresh=1, campaign=None, **kwargs):
    return await family14_run(
        root,
        monkeypatch,
        NO_FINALISTS,
        adapter=adapter,
        family14_settings={"refresh": refresh},
        campaign={"rounds": rounds, "indeterminate": LENIENT, **SERIAL, **(campaign or {})},
        **kwargs,
    )


def _cut(monkeypatch, adapter, cut):
    """Arm one named cut of the shared scenario's round-2 refresh."""

    kind, name, call = CUTS[cut]
    if kind == "hook":
        _cut_hook(monkeypatch, name, call=call)
    elif kind == "append":
        _cut_after_append(monkeypatch, name, call=call)
    elif kind == "stage":
        crash_after_the_stage_once(monkeypatch, measurement_prefix=REFRESH)
    else:
        adapter.cut = name


#: (a)-(h): every boundary of the refresh, as `(kind, name, call)`. The first
#: `baseline_bound`, `after_baseline_bound` and `baseline_complete` are round
#: 1's, so round 2's are the second. `stage` crashes after the refresh's first
#: stage, before its barrier; `send` cuts the power inside that stage, at the
#: first send of (c1, 2).
CUTS = {
    "a-after-last-counted-close": ("hook", "after_round_closed", 1),
    "b-refresh-bound": ("hook", "after_baseline_refresh_bound", 1),
    "c-baseline-bound-unborn": ("append", "baseline_bound", 2),
    "d-baseline-bound-born": ("hook", "after_baseline_bound", 2),
    "e-after-stage-before-barrier": ("stage", None, None),
    "e-mid-stage-power-cut": ("send", ("c1", 2), None),
    "f-pairs-excluded": ("append", "pairs_excluded", 1),
    "g-baseline-complete": ("append", "baseline_complete", 2),
    "h-refresh-completed": ("hook", "after_baseline_refresh_completed", 1),
}


#: The campaign journal's last durable record at each cut with a fixed one.
LAST = {
    "a-after-last-counted-close": "round_closed@1",
    "b-refresh-bound": f"baseline_refresh_bound@2:{REFRESH}",
    "c-baseline-bound-unborn": f"baseline_bound@2:{REFRESH}",
    "d-baseline-bound-born": f"measurement_bound@2:{REFRESH}",
    "f-pairs-excluded": f"pairs_excluded@2:{REFRESH}",
    "g-baseline-complete": f"baseline_complete@2:{REFRESH}",
    "h-refresh-completed": f"baseline_refresh_completed@2:{REFRESH}",
}


def _bytes(run):
    return {
        path.parent.name: path.read_bytes()
        for path in (run / "measurements").glob("*/events.jsonl")
    }


#: Extra `instance_attested` records of the refresh a resume writes after each
#: (e) cut. The crash after stage 1 loses the instance before stage 2, so stage
#: 2 attests a new one; the power cut inside stage 1 re-runs the lost try on
#: the instance stage 1 already attested, so it writes none.
EXTRA_ATTESTATIONS = {"e-after-stage-before-barrier": 1, "e-mid-stage-power-cut": 0}


def _attestations(facts):
    """The `instance_attested` records of a `_facts` journal, by measurement."""

    return Counter(
        keys["measurement_id"] for event, keys in facts[0] if event == "instance_attested"
    )


#: Fact keys that hold a campaign journal sequence, not a name.
SEQUENCE_KEYS = ("comparison", "supersedes")


def _without_attestations(facts):
    """`facts` without `instance_attested` records and sequence-valued keys.

    A resume may attest a new Target instance: after the crash between stages
    1 and 2 it does, while the power cut inside stage 1 re-runs the lost try on
    the instance stage 1 already attested (see `EXTRA_ATTESTATIONS`, which the
    caller pins before dropping them). An attestation is fresh metadata (H4).
    Any extra record, such as the power cut's `rollout_interrupted`
    observation, shifts the journal sequences later records cite, so those
    keys are dropped too. Scheduled and scored work stand."""

    campaign, ledger, rows = facts
    kept = [
        (event, {k: v for k, v in keys.items() if k not in SEQUENCE_KEYS})
        for event, keys in campaign
        if event != "instance_attested"
    ]
    return kept, ledger, rows


def _scored(run, measurement_id):
    return {
        (row["payload"]["case_id"], row["payload"]["sample_id"])
        for row in _records(_measurement_events(run, measurement_id))
        if row["event"] == "scoring_finished"
    }


def _tries(run, measurement_id):
    """How many tries each `(case_id, sample_id)` of a measurement started."""

    return Counter(
        (row["payload"]["case_id"], row["payload"]["sample_id"])
        for row in _records(_measurement_events(run, measurement_id))
        if row["event"] == "rollout_started"
    )


def _assert_round_2_binds_the_refresh(run):
    """Round 2 binds `m-r02-refresh` exactly once and no round-2 record names
    the replaced `m-r00-champion` as its measurement; its grouping reads the
    refresh's prefix after the completion."""

    assert [(p["round"], p["measurement_id"]) for p in payloads(run, "baseline_bound")] == [
        (1, CHAMPION0),
        (2, REFRESH),
    ]
    (bound,) = payloads(run, "baseline_refresh_bound")
    assert (bound["round"], bound["replaced"]["measurement_id"]) == (2, CHAMPION0)
    (completed,) = payloads(run, "baseline_refresh_completed")
    assert completed["measurement_id"] == REFRESH
    assert not [
        row
        for row in _records(run / "campaign.jsonl")
        if row["payload"].get("round") == 2 and row["payload"].get("measurement_id") == CHAMPION0
    ]
    (complete,) = [p for p in payloads(run, "baseline_complete") if p["round"] == 2]
    (grouping,) = [p for p in payloads(run, "grouping_started") if p["round"] == 2]
    assert grouping["baseline_binding"] == complete["evidence"][0]["content_digest"]
    events = [row["event"] for row in _records(run / "campaign.jsonl")]
    assert events.index("baseline_refresh_completed") < max(
        i for i, event in enumerate(events) if event == "grouping_started"
    )


@pytest.mark.parametrize("cut", list(CUTS))
async def test_a_crash_at_every_refresh_boundary_resumes_the_owed_action(
    tmp_path, monkeypatch, cut
):
    """Each cut, resumed by the same command in the same run directory: the
    durable prefix stands byte for byte (campaign journal and every measurement
    journal), only the missing work runs, and the run matches its uncut
    control's records, ledger and rows."""

    adapter = _target(tmp_path)
    with monkeypatch.context() as patch:
        _cut(patch, adapter, cut)
        with pytest.raises((Crash, _PowerCut)):
            await _scenario(tmp_path, patch, adapter)
    run = tmp_path / "run"
    history = (run / "campaign.jsonl").read_bytes()
    journals = _bytes(run)
    assert not payloads(run, "campaign_finished")
    if cut in LAST:
        assert _event_order(run)[-1] == LAST[cut]
    else:
        # Inside the refresh's first stage: some rows scored, none complete.
        assert _scored(run, REFRESH)
        assert len([p for p in payloads(run, "baseline_complete") if p["round"] == 2]) == 0
    # (c) leaves the refresh named but unborn (#106); from (d) on it is born.
    unborn = ("a-after-last-counted-close", "b-refresh-bound", "c-baseline-bound-unborn")
    assert (REFRESH in journals) == (cut not in unborn)
    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    result = audit(run)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    for name, content in journals.items():
        assert _measurement_events(run, name).read_bytes().startswith(content)
    _assert_round_2_binds_the_refresh(run)
    (excluded,) = payloads(run, "pairs_excluded")
    assert (excluded["measurement_id"], excluded["count"]) == (REFRESH, 1)

    control_root = tmp_path / "uncut"
    control_root.mkdir()
    control, plain = await _scenario(control_root, monkeypatch, _target(control_root))
    assert plain.finished["outcome"] == "completed"
    resumed, uncut = _facts(run), _facts(control)
    tries, plain_tries = _tries(run, REFRESH), _tries(control, REFRESH)
    if cut.startswith("e-"):
        # Exactly the expected extra attestations, and only of the refresh.
        extra = Counter({REFRESH: EXTRA_ATTESTATIONS[cut]})
        assert _attestations(resumed) == _attestations(uncut) + extra
        resumed, uncut = _without_attestations(resumed), _without_attestations(uncut)
    if cut == "e-mid-stage-power-cut":
        # The try the power cut is durable as started: the resume records it
        # interrupted (rollout-owned, below the thresholds) and runs it once
        # more. Every other sample of the refresh keeps its single try.
        interrupted = (
            "indeterminate_observed",
            {"measurement_id": REFRESH, "reason": "rollout_interrupted"},
        )
        assert resumed[0].count(interrupted) == uncut[0].count(interrupted) + 1
        resumed[0].remove(interrupted)
        plain_tries[("c1", "02")] += 1
    assert tries == plain_tries
    assert resumed == uncut


# Infrastructure ---------------------------------------------------------------


def _suspension_barriers(run, monkeypatch):
    """`(round, barrier)` of each threshold barrier the lifecycle derives and
    checks while replaying the journal (`_check_barrier_window`), observed,
    never changed."""
    from evolution.tree.campaign_schema import CampaignLifecycle

    seen = []
    original = CampaignLifecycle._check_barrier_window

    def spy(self, state, barrier):
        seen.append((state.round, barrier))
        return original(self, state, barrier)

    with monkeypatch.context() as patch:
        patch.setattr(CampaignLifecycle, "_check_barrier_window", spy)
        journal_consistent(run)
    return seen


async def test_a_threshold_crossing_in_the_refresh_suspends_it_open_and_the_repair_completes_it(
    tmp_path, monkeypatch
):
    """PDR-0015, ADR-0019: the refresh's Champion fails (c2, 1) once with no
    retry and a target threshold of 0. The stage barrier suspends the campaign
    in the `baseline_stage` window with the refresh open: no
    `baseline_complete`, no completion, no grouping, no close. After the
    repair the same command measures only the missing pair and stage 2, then
    completes the refresh. (Below the thresholds the exhausted pair is
    excluded instead: the shared crash scenario above.)"""

    adapter = _target(tmp_path, faults={("c2", 1): "target_failed"})
    campaign = {
        "sample_retries": 0,
        "indeterminate": {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}},
    }
    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, campaign=campaign)
    assert snapshot.finished is None
    assert snapshot.suspensions[-1]["evidence_kind"] == "threshold"
    (suspended,) = payloads(run, "campaign_suspended")
    assert (suspended["reason"], suspended["owner"]) == ("target_unstable", "target")
    assert snapshot.rounds[2].attempts == {} and snapshot.rounds[2].closed is None
    assert [entry["completed"] for entry in snapshot.refreshes] == [None]
    assert snapshot.rounds[2].baseline_measurement_id == REFRESH
    assert not snapshot.rounds[2].baseline_complete
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1]
    assert [
        p["stage"] for p in payloads(run, "stage_opened") if p["measurement_id"] == REFRESH
    ] == [1]
    # Family 14 journals no `threshold_reached` (I1 spec §5.1: the crossing
    # rides in the suspension), so the barrier is read where the lifecycle
    # derives it while admitting the suspension.
    assert not payloads(run, "threshold_reached")
    assert _suspension_barriers(run, monkeypatch) == [(2, "baseline_stage")]
    assert audit(run)["mismatches"] == []
    stage_one = _tries(run, REFRESH)
    assert stage_one[("c2", "01")] == 1 and set(stage_one.values()) == {1}

    adapter.faults.clear()
    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, campaign=campaign)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []
    assert len(payloads(run, "campaign_suspended")) == 1
    assert sum(p["count"] for p in payloads(run, "repairs_dispatched")) == 1
    assert _starts(run, REFRESH, "c2") == [(0, False), (1, True)]
    tries = _tries(run, REFRESH)
    # Stage 1's settled pairs never run again; stage 2 runs once.
    assert {key: tries[key] for key in stage_one} == {
        **stage_one,
        ("c2", "01"): 2,
    }
    assert set(tries) - set(stage_one) and all(
        tries[key] == 1 for key in set(tries) - set(stage_one)
    )
    assert not payloads(run, "pairs_excluded")
    _assert_round_2_binds_the_refresh(run)


# Cancellation -----------------------------------------------------------------


def _journal_has(run, needle, count):
    def cancelled():
        path = run / "campaign.jsonl"
        return path.is_file() and path.read_bytes().count(needle) >= count

    return cancelled


async def test_a_cancellation_during_the_refresh_stage_stops_without_grouping(
    tmp_path, monkeypatch
):
    """The caller cancels once the refresh scored its first row: the run stops
    `cancelled` with the refresh open, before its `baseline_complete`, and
    writes no grouping and no close -- the engine never needs a close of a
    round whose refresh is unfinished. The finished run verifies on the same
    invocation and cannot resume work."""
    from tests.campaign_drivers import _cancel_when

    adapter = _target(tmp_path, faults={})
    run = tmp_path / "run"
    stop = _cancel_when(run, REFRESH, b'"scoring_finished"')
    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, cancelled=stop)
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert [entry["completed"] for entry in snapshot.refreshes] == [None]
    assert not snapshot.rounds[2].baseline_complete
    assert [p["round"] for p in payloads(run, "round_closed")] == [1]
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1]
    assert audit(run)["mismatches"] == []
    history, journals = (run / "campaign.jsonl").read_bytes(), _bytes(run)
    run, again = await _scenario(tmp_path, monkeypatch, adapter)
    assert again.finished == snapshot.finished
    assert (run / "campaign.jsonl").read_bytes() == history
    assert _bytes(run) == journals


async def test_a_cancellation_right_after_the_refresh_stage_stops_before_its_completion(
    tmp_path, monkeypatch
):
    """The token flips once the refresh's `baseline_complete` is durable: the
    run stops `cancelled` between `baseline_complete` and
    `baseline_refresh_completed` -- the window where the lifecycle refuses any
    close but `condition_changed` -- and writes no close there."""

    adapter = _target(tmp_path, faults={})
    run = tmp_path / "run"
    stop = _journal_has(run, b'"event":"baseline_complete"', 2)
    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, cancelled=stop)
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert snapshot.rounds[2].baseline_complete
    assert [entry["completed"] for entry in snapshot.refreshes] == [None]
    assert [p["round"] for p in payloads(run, "round_closed")] == [1]
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1]
    assert _records(run / "campaign.jsonl")[-2]["event"] == "baseline_complete"
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []


def _seed_baseline_open(run):
    """The seed baseline is still open: no `baseline_complete` is durable yet."""

    data = (run / "campaign.jsonl").read_bytes()
    return data.count(b'"event":"baseline_complete"') == 0


def _try_starts(run, measurement_id, case_id, sample_id):
    """The `rollout_started` payloads of one pair of a measurement, in order."""

    return [
        row["payload"]
        for row in _records(_measurement_events(run, measurement_id))
        if row["event"] == "rollout_started"
        and (row["payload"]["case_id"], row["payload"]["sample_id"]) == (case_id, sample_id)
    ]


@pytest.mark.parametrize(
    "baseline,refresh,rounds",
    [(REFRESH, 1, 2), (CHAMPION0, 0, 1)],
    ids=["refresh", "seed-baseline-control"],
)
async def test_a_forced_stop_withholding_a_retry_cut_before_its_terminal_resumes_the_baseline(
    tmp_path, monkeypatch, baseline, refresh, rounds
):
    """#194 (spec §5, rewritten from H7's strict xfails): the baseline's
    Champion fails (c2, 1); the caller cancels on that failure, so the stopping
    run withholds its retry and its remaining tries of stage 1 finish
    `cancelled` (observable: one try of (c2, 01), cancelled rows). The power
    goes after the stage returned, before its barrier and the `stopped`
    terminal. The first resume, not cancelled, runs stage 2, then suspends
    `champion_incomplete` (owner `target`) listing the declined (c2, 01) at
    ordinal 0. The second resume gives (c2, 01) its restored D1 retry, which
    fails again because the scripted faults persist, so the pair is exhausted
    and excluded, and the run completes. The same on the refresh and on the
    seed baseline (the control parameter, N = 0). Nobody clears the faults."""
    from tests.campaign_drivers import _cancel_when

    if baseline == CHAMPION0:
        # Arm the faults for the seed baseline's Champion only: they play while
        # the seed baseline is open, never in a later Champion session.
        monkeypatch.setattr(sys.modules[__name__], "_refresh_open", _seed_baseline_open)
    adapter = _target(tmp_path)
    run = tmp_path / "run"
    stop = _cancel_when(run, baseline, b'"outcome":"target_failed"')
    options = {"refresh": refresh, "rounds": rounds}
    with monkeypatch.context() as patch:
        crash_after_the_stage_once(patch, measurement_prefix=baseline)
        with pytest.raises(Crash):
            await _scenario(tmp_path, patch, adapter, cancelled=stop, **options)
    assert not payloads(run, "campaign_finished")
    finished = {
        (row["payload"]["case_id"], row["payload"]["sample_id"]): row["payload"]["outcome"]
        for row in _records(_measurement_events(run, baseline))
        if row["event"] == "rollout_finished"
    }
    # The token withheld (c2, 01)'s retry and cancelled the stage's last tries.
    assert _tries(run, baseline)[("c2", "01")] == 1
    assert finished[("c2", "01")] == "target_failed"
    assert "cancelled" in finished.values()
    history = (run / "campaign.jsonl").read_bytes()

    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, **options)
    assert snapshot.finished is None
    listing = payloads(run, "campaign_suspended")[-1]
    assert (listing["reason"], listing["owner"]) == ("champion_incomplete", "target")
    evidence = listing["evidence"]
    assert (evidence["evidence_kind"], evidence["measurement_id"]) == ("champion_rows", baseline)
    listed = {(r["case_id"], r["sample_id"]): r["retry_ordinal"] for r in evidence["rows"]}
    assert listed[("c2", "01")] == 0
    assert [
        p["stage"] for p in payloads(run, "stage_opened") if p["measurement_id"] == baseline
    ] == [1, 2]
    assert journal_consistent(run).finished is None
    assert audit(run)["mismatches"] == []

    run, snapshot = await _scenario(tmp_path, monkeypatch, adapter, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    first, second = _try_starts(run, baseline, "c2", "01")
    assert int(first.get("retry_ordinal", 0)) == 0
    assert int(second["retry_ordinal"]) == 1 and "repair" not in second
    (excluded,) = [p for p in payloads(run, "pairs_excluded") if p["measurement_id"] == baseline]
    assert ("c2", "01") in {(p["case_id"], p["sample_id"]) for p in excluded["pairs"]}
    assert audit(run)["mismatches"] == []
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    if baseline == REFRESH:
        _assert_round_2_binds_the_refresh(run)


# Authorizations -------------------------------------------------------------


def _decision(refused, effect):
    from evolution.tree.execution.authorizations import Authorization

    assert effect in refused.effects
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "accept changed target",
        now="2026-10-12T00:00:00Z",
        effect=effect,
    )


async def _authorized(tmp_path, monkeypatch, cut, effect, *, rounds=3, refresh=1):
    """The shared scenario without faults, cut at `cut`; the Target's settings
    change (`settings-v2`) and the comparable change is accepted with
    `effect`. Returns `(run, snapshot, durable journals at the cut)`."""
    from evolution.tree.evolve import EvolveRefused

    adapter = _target(tmp_path, faults={})
    options = {"rounds": rounds, "refresh": refresh}
    with monkeypatch.context() as patch:
        _cut(patch, adapter, cut)
        with pytest.raises((Crash, _PowerCut)):
            await _scenario(tmp_path, patch, adapter, **options)
    run = tmp_path / "run"
    journals = _bytes(run)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await _scenario(tmp_path, monkeypatch, adapter, **options)
    decision = _decision(refused.value, effect)
    run, snapshot = await _scenario(
        tmp_path, monkeypatch, adapter, authorizations=[decision], **options
    )
    assert snapshot.finished is not None
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == effect
    for name, content in journals.items():
        assert _measurement_events(run, name).read_bytes().startswith(content)
    return run, snapshot, journals


def _baselines(run):
    return {row["round"]: row["measurement_id"] for row in payloads(run, "baseline_bound")}


def _refreshes(run):
    return [
        (row["round"], row["counted_rounds"], row["replaced"]["measurement_id"])
        for row in payloads(run, "baseline_refresh_bound")
    ]


def _completions(run):
    return {
        row["round"]: row["measurement_id"] for row in payloads(run, "baseline_refresh_completed")
    }


async def test_continue_at_the_bound_refresh_binds_it_plain_in_the_new_epoch(tmp_path, monkeypatch):
    """`continue` after `baseline_refresh_bound`: the refresh is still due (the
    count is kept), and its measurement, first bound in epoch 1, keeps the plain
    id `m-r02-refresh` (ADR-0020); nothing is rebound."""

    run, snapshot, _ = await _authorized(tmp_path, monkeypatch, "b-refresh-bound", "continue")
    assert snapshot.finished["outcome"] == "completed"
    assert len(snapshot.epochs) == 2
    assert not payloads(run, "measurement_rebound")
    assert _baselines(run) == {1: CHAMPION0, 2: REFRESH, 3: "m-r03-refresh"}
    assert _refreshes(run) == [
        (2, {"first": 1, "last": 1}, CHAMPION0),
        (3, {"first": 2, "last": 2}, REFRESH),
    ]
    assert _completions(run) == {2: REFRESH, 3: "m-r03-refresh"}
    events = _event_order(run)
    assert events.index("authorization_applied") < events.index(f"baseline_bound@2:{REFRESH}")


async def test_continue_inside_the_refresh_stage_rebinds_it_and_names_the_rebind(
    tmp_path, monkeypatch
):
    """`continue` after the refresh's first stage (before its barrier): the
    unfinished refresh is rebound `m-r02-refresh@e1`, carrying its scored
    rows, and `baseline_refresh_completed` names the rebind. The original
    journal stays frozen byte for byte, and round 3 refreshes the rebind."""

    rebind = f"{REFRESH}@e1"
    run, snapshot, journals = await _authorized(
        tmp_path, monkeypatch, "e-after-stage-before-barrier", "continue"
    )
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, REFRESH).read_bytes() == journals[REFRESH]
    (rebound,) = payloads(run, "measurement_rebound")
    assert (rebound["from_measurement_id"], rebound["measurement_id"]) == (REFRESH, rebind)
    assert rebound["imported"] > 0
    assert snapshot.rounds[2].baseline_measurement_id == rebind
    assert _completions(run) == {2: rebind, 3: "m-r03-refresh"}
    assert _refreshes(run) == [
        (2, {"first": 1, "last": 1}, CHAMPION0),
        (3, {"first": 2, "last": 2}, rebind),
    ]
    (complete,) = [p for p in payloads(run, "baseline_complete") if p["round"] == 2]
    assert complete["measurement_id"] == rebind


async def test_rebaseline_at_the_bound_refresh_supersedes_it_and_restarts_the_count(
    tmp_path, monkeypatch
):
    """`rebaseline` after `baseline_refresh_bound`: round 2 becomes the
    `rebaseline` round and binds `m-r02-champion@e1` (plan decision 7); the
    refresh stays uncompleted as superseded. The count restarts on the fresh
    id: round 3 refreshes it over round 2 alone."""

    fresh = "m-r02-champion@e1"
    run, snapshot, _ = await _authorized(tmp_path, monkeypatch, "b-refresh-bound", "rebaseline")
    assert snapshot.finished["outcome"] == "completed"
    assert _baselines(run) == {1: CHAMPION0, 2: fresh, 3: "m-r03-refresh"}
    assert _refreshes(run) == [
        (2, {"first": 1, "last": 1}, CHAMPION0),
        (3, {"first": 2, "last": 2}, fresh),
    ]
    assert _completions(run) == {3: "m-r03-refresh"}
    assert not (run / "measurements" / REFRESH).exists()


async def test_rebaseline_inside_the_refresh_stage_closes_the_round_and_rebinds_nothing(
    tmp_path, monkeypatch
):
    """`rebaseline` after the refresh's first stage: round 2 closes
    `condition_changed` with the refresh uncompleted and its journal frozen;
    round 3 is the `rebaseline` round and binds a fresh `m-r03-champion@e1`,
    with no refresh (the count restarts)."""

    run, snapshot, journals = await _authorized(
        tmp_path, monkeypatch, "e-after-stage-before-barrier", "rebaseline"
    )
    assert snapshot.finished["outcome"] == "completed"
    assert snapshot.rounds[2].closed["outcome"] == "condition_changed"
    assert not snapshot.rounds[2].baseline_complete
    assert not payloads(run, "measurement_rebound")
    assert _measurement_events(run, REFRESH).read_bytes() == journals[REFRESH]
    assert _baselines(run) == {1: CHAMPION0, 2: REFRESH, 3: "m-r03-champion@e1"}
    assert [row[0] for row in _refreshes(run)] == [2]
    assert _completions(run) == {}
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1, 3]


async def test_close_inside_the_refresh_stage_finishes_the_run(tmp_path, monkeypatch):
    """`close` after the refresh's first stage: the run finishes
    `condition_changed` in the same invocation, the refresh uncompleted and
    its journal frozen. (A crash between the applied close and its terminal
    is H12's, H6-F3.)"""

    run, snapshot, journals = await _authorized(
        tmp_path, monkeypatch, "e-after-stage-before-barrier", "close"
    )
    assert snapshot.finished["reason"] == "condition_changed"
    assert not payloads(run, "measurement_rebound")
    assert _measurement_events(run, REFRESH).read_bytes() == journals[REFRESH]
    assert _completions(run) == {}
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1]


async def test_after_a_rebaseline_the_count_restarts(tmp_path, monkeypatch):
    """N = 2. The uncut control (same Target, N and rounds) refreshes
    `m-r00-champion` at round 3, over rounds 1-2. Here a `rebaseline` after
    round 1's close makes round 2 bind `m-r02-champion@e1`; round 3 has
    counted only round 2 on it and does not refresh, and round 4 refreshes it
    over rounds 2-3."""

    fresh = "m-r02-champion@e1"
    run, snapshot, _ = await _authorized(
        tmp_path, monkeypatch, "a-after-last-counted-close", "rebaseline", rounds=4, refresh=2
    )
    assert snapshot.finished["outcome"] == "completed"
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 4
    assert _baselines(run) == {1: CHAMPION0, 2: fresh, 3: fresh, 4: "m-r04-refresh"}
    assert _refreshes(run) == [(4, {"first": 2, "last": 3}, fresh)]
    control_root = tmp_path / "uncut"
    control_root.mkdir()
    control, plain = await _scenario(
        control_root, monkeypatch, _target(control_root, faults={}), rounds=4, refresh=2
    )
    assert plain.finished["outcome"] == "completed"
    assert _refreshes(control) == [(3, {"first": 1, "last": 2}, CHAMPION0)]


@pytest.mark.parametrize("changed", [2, 0], ids=["N1-to-N2", "N1-to-N0"])
async def test_changing_n_at_the_bound_refresh_admits_no_continue(tmp_path, monkeypatch, changed):
    """Task 5 review: could a refresh stop being due between its
    `baseline_refresh_bound` and `baseline_bound`, stalling the planner (which
    binds the refresh id) against the lifecycle ("the round's refresh is not
    due")? Only N can change it, and N is part of the policy, whose digest is
    a comparable identity field that is not agent-side: a changed N admits
    `rebaseline` or `close`, never `continue`. The accepted `rebaseline` makes
    the round the `rebaseline` round, which binds its fresh id and supersedes
    the refresh, so nothing stalls."""
    from evolution.tree.evolve import EvolveRefused

    adapter = _target(tmp_path, faults={})
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_baseline_refresh_bound")
        with pytest.raises(Crash):
            await _scenario(tmp_path, patch, adapter)
    with pytest.raises(EvolveRefused) as refused:
        await _scenario(tmp_path, monkeypatch, adapter, refresh=changed)
    assert refused.value.effects == ("rebaseline", "close")
    decision = _decision(refused.value, "rebaseline")
    run, snapshot = await _scenario(
        tmp_path, monkeypatch, adapter, refresh=changed, authorizations=[decision]
    )
    assert snapshot.finished["outcome"] == "completed"
    assert snapshot.identity["policy"]["baseline_refresh_after_rounds"] == changed
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []
    assert _baselines(run) == {1: CHAMPION0, 2: "m-r02-champion@e1"}
    assert [row[0] for row in _refreshes(run)] == [2]
    assert _completions(run) == {}


# Persistence ----------------------------------------------------------------


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_a_damaged_refresh_journal_read_by_a_later_stable_final_restores_the_same_run(
    tmp_path, monkeypatch, damage
):
    """`stable` observe, N = 1: round 1 is `retained`, round 2 refreshes, and
    round 2's stable final cites the completed `m-r02-refresh` as its stored
    Champion. A cut after that final's binding; the refresh journal is then
    removed or corrupted. The resume is a persistence fault of the measurement
    journal -- never a fresh substitute: no second refresh, no new Champion
    measurement, no selection. Restoring the bytes resumes the same run to
    completion on the original refresh (H6's
    `test_later_stored_baseline_read_failure_restores_same_run`)."""
    from tests.campaign_drivers import OBSERVE
    from tests.test_tree_campaign_family14_refresh_run import _phase_hooks, _veto_scorer

    flag = tmp_path / "veto.flag"
    _phase_hooks(monkeypatch, veto=flag, veto_rounds={1})
    options = {
        "family14_settings": {"stability": "stable", "refresh": 1},
        "campaign": {"rounds": 2},
    }
    config = {**OBSERVE, "scorer": _veto_scorer(flag)}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, config, **options)
    run = tmp_path / "run"
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["retained"]
    (completed,) = payloads(run, "baseline_refresh_completed")
    (final,) = [p for p in payloads(run, "final_measurement_bound") if p["round"] == 2]
    stored = final["stored_champion"]
    assert stored == {
        "measurement_id": REFRESH,
        "prefix_digest": completed["evidence"][0]["content_digest"],
    }
    source = _measurement_events(run, REFRESH)
    original = source.read_bytes()
    history = (run / "campaign.jsonl").read_bytes()
    born = sorted(path.name for path in (run / "measurements").iterdir())
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(original.replace(b'"timestamp":"2', b'"timestamp":"1', 1))
        assert source.read_bytes() != original
    _, fault = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        expected_fault=("persistence_failed", "measurement_journal"),
        **options,
    )
    assert fault.terminal == "persistence_failed"
    assert not [p for p in payloads(run, "global_comparison_completed") if p["round"] == 2]
    assert not [p for p in payloads(run, "selection_decided") if p["round"] == 2]
    assert len(payloads(run, "baseline_refresh_bound")) == 1
    # At most the final member declared before the cut is born; nothing on
    # the Champion side replaces the unreadable refresh.
    after = {path.name for path in (run / "measurements").iterdir()}
    assert set(born) <= after <= {*born, final["measurement_id"]}
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    source.write_bytes(original)
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    assert source.read_bytes() == original
    assert len(payloads(run, "baseline_refresh_bound")) == 1
    assert _baselines(run) == {1: CHAMPION0, 2: REFRESH}
    assert all(
        p["stored_champion"] == stored
        for p in payloads(run, "final_measurement_bound")
        if p["round"] == 2
    )
    (comparison,) = [p for p in payloads(run, "global_comparison_completed") if p["round"] == 2]
    assert comparison["champion_measurement_id"] == REFRESH
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []
