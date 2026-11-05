"""#194 family 14 (`/15`): a resume after a stranded stop lists the Champion
measurement's gaps (spec §5, Task 10).

Every run is a genuine `family14_run` engine invocation over `NO_FINALISTS`
(every round closes `stalled`), stopped before H12's public report assembly. The
scenario mirrors H7's: the Champion fails `(c2, 01)` with `target_failed` while the
cut measurement is open, the caller's token flips on that failure, the stopping run
declines that pair's retry and its remaining tries of stage 1 end `cancelled`, and the
power is cut after the stage, before its barrier and the `stopped` terminal.
The first plain resume runs stage 2 and suspends `champion_incomplete`; the
next one (the Target repaired) gives each listed pair one granted D1 retry
and the run completes.

The Champion measurements family 14 measures afresh, and so can cut: the
seed baseline, a round baseline that a `rebaseline` binds, and a refresh
(H7). G6: a resume that owes a connection record writes it first and lists
nothing; the listing comes on a later resume.
"""

from __future__ import annotations

import pytest

from evolution.tree.execution.interaction import RolloutInfrastructureError
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    MANIFEST_SEED,
    SERIAL,
    _cancel_when,
    _cut_hook,
    _measurement_events,
    _records,
    _ResettableTagged,
)
from tests.campaign_fakes import CONNECTION_PROBING, Crash, crash_after_the_stage_once
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

#: Round 2's refresh of the seed baseline (`refresh: 1`).
REFRESH = "m-r02-refresh"
SEED = "m-r00-champion"
#: Round 2's fresh baseline after a `rebaseline` in round 1 (`refresh: 0`).
ROUND_TWO = "m-r02-champion@e1"
#: The Champion's scripted fault while the cut measurement is open.
FAULTS = {("c2", 1): "target_failed"}
#: What the first resume lists after the cut stop, in schedule order: the
#: declined `(c2, 01)` (owner `target`) and the two `cancelled` pairs (owner
#: `rollout`), all at ordinal 0; `target` outranks `rollout`.
LISTED = [("c2", "01", 0, "target"), ("c2", "02", 0, "rollout"), ("c2", "03", 0, "rollout")]


def _forge_d1_after_cancel(run, measurement_id, case_id, sample_id):
    """Append an ungranted D1 start/cancelled terminal through the S1 writer."""
    from evolution.tree.execution.journal import (
        EVENTS_FILE,
        LEASE_FILE,
        MAX_JOURNAL_BYTES,
        MAX_RECORD_BYTES,
    )
    from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
    from evolution.tree.storage.append_log import AppendLog

    path = _measurement_events(run, measurement_id).parent
    records = _records(path / EVENTS_FILE)
    pair = [
        r
        for r in records
        if r["event"].startswith("rollout_")
        and (r["payload"].get("case_id"), r["payload"].get("sample_id")) == (case_id, sample_id)
    ]
    started = [r for r in pair if r["event"] == "rollout_started"][-1]
    finished = pair[-1]
    assert finished["event"] == "rollout_finished"
    assert finished["payload"]["outcome"] == "cancelled"
    ordinal = started["payload"].get("retry_ordinal", 0) + 1
    operation = f"{started['operation_id']}-forged-{ordinal}"
    log = AppendLog.open(
        path,
        file_name=EVENTS_FILE,
        lock_name=LEASE_FILE,
        create=False,
        state_factory=Lifecycle,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        secrets=(),
        envelope_family=S1_FAMILY,
        require_empty=False,
    )
    try:
        log.scan()
        run_id = records[0]["run_id"]
        start = {k: v for k, v in started["payload"].items() if k != "repair"}
        log.append(run_id, "rollout_started", {**start, "retry_ordinal": ordinal}, operation)
        log.append(
            run_id,
            "rollout_finished",
            {**finished["payload"], "retry_ordinal": ordinal, "outcome": "cancelled"},
            operation,
        )
    finally:
        log.close()
    return ordinal


class _Guarded:
    """State read by the Champion listing guard, without a campaign behind it."""

    from evolution.tree.campaign import _Campaign

    _champion_listing_admissible = _Campaign._champion_listing_admissible
    _list_champion_gaps = _Campaign._list_champion_gaps
    del _Campaign

    def __init__(self):
        from types import SimpleNamespace

        self.stopping = False
        self.pending = None
        self.pending_change = None
        self.held_change = None
        self.token = False
        self.owed = False
        self.stopped = []
        self.state = SimpleNamespace(
            finished=None,
            owed_terminal=None,
            threshold=None,
            cancelled_role_seq=None,
            pending_change=None,
            suspension_causes={4: ("probe_setup", None)},
            rounds={1: SimpleNamespace(probe_check={"status": "ready"})},
            measurements={SEED: {"epoch": 0}},
            epochs=[{"epoch": 0}],
        )

    def cancelled(self):
        return self.token

    def snapshot(self):
        return self.state

    def _connection_record_owed(self):
        return self.owed

    def stop(self, pending):
        self.stopped.append(pending)


def test_a_restored_cancelled_try_holds_no_stage_slot():
    """A restored grant supplies its own slot, never another measurement's."""
    from types import SimpleNamespace

    from evolution.tree.campaign import _unfilled_slots

    seed = "m-r00-champion"
    seen = {}

    class View:
        max_repeat = 4

        def unfilled_slots(self, horizon, reserved, declined=()):
            seen["declined"] = set(declined)
            return 0

    snapshot = SimpleNamespace(
        family_version=14,
        measurements={seed: {"stages": [{"look": 3}, {"look": 4}]}},
        accounted=frozenset({(seed, "c2", "01", 0)}),
        retries_declined=frozenset({(seed, "c2", "01", 0)}),
        listing_restored=frozenset({(seed, "c2", "02", 0), ("m-r01-final-01-a01", "c1", "01", 0)}),
    )
    assert _unfilled_slots(snapshot, seed, View(), (3, 4)) == 0
    assert seen["declined"] == {("c2", "01", 0), ("c2", "02", 0)}


def _open(run, measurement_id):
    """`measurement_id` is born and no `baseline_complete` names it yet."""

    if not _measurement_events(run, measurement_id).is_file():
        return False
    return not [
        p for p in payloads(run, "baseline_complete") if p["measurement_id"] == measurement_id
    ]


class _GapTarget(_ResettableTagged):
    """`_ResettableTagged` whose Champion sessions play `faults[(case_id,
    repeat)]` while `measurement_id` is open in the run's own journal, and
    whose connection probe instance alone can be made unavailable
    (`fail_probe`). Clearing `faults` repairs the Target."""

    def __init__(self, run, measurement_id, *, faults=FAULTS):
        super().__init__(used=EDITED["used"])
        self.run = run
        self.measurement_id = measurement_id
        self.faults = dict(faults)
        self.fail_probe = False

    async def materialize(self, candidate, strategy, instance_id):
        if self.fail_probe and CONNECTION_PROBING.get():
            raise RolloutInfrastructureError("down", "target_unavailable")
        return await super().materialize(candidate, strategy, instance_id)

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind != "champion":
            return instance
        lookup = instance._lookup

        def scripted(session):
            key = (session.case_id, session.requested_seed - MANIFEST_SEED)
            if key in self.faults and _open(self.run, self.measurement_id):
                return self.faults[key]
            return lookup(session)

        instance._lookup = scripted
        return instance


async def _run(
    root, monkeypatch, adapter, *, rounds=1, refresh=0, round_stop=None, grouper=None, **kwargs
):
    """One genuine invocation of the scenario; the same `root` resumes it.
    `grouper`, when given, is the `/15` `grouper` section (H8)."""

    settings = {"refresh": refresh}
    if round_stop is not None:
        settings["round_stop"] = round_stop
    if grouper is not None:
        settings["grouper"] = grouper
    return await family14_run(
        root,
        monkeypatch,
        NO_FINALISTS,
        adapter=adapter,
        family14_settings=settings,
        campaign={"rounds": rounds, "indeterminate": LENIENT, **SERIAL},
        **kwargs,
    )


async def _cut_stop(root, monkeypatch, adapter, **options):
    """The forced stop on `(c2, 01)`'s failure in `adapter.measurement_id`,
    cut after its stage returned, before the barrier and the terminal."""

    run = root / "run"
    measurement_id = adapter.measurement_id
    stop = _cancel_when(run, measurement_id, b'"outcome":"target_failed"')
    with monkeypatch.context() as patch:
        cut = crash_after_the_stage_once(patch, measurement_prefix=measurement_id)
        with pytest.raises(Crash):
            await _run(root, patch, adapter, cancelled=stop, **options)
    assert cut == [measurement_id]
    assert not payloads(run, "campaign_finished")
    assert _finished(run, measurement_id) == {
        ("c1", "01"): "completed",
        ("c1", "02"): "completed",
        ("c1", "03"): "completed",
        ("c2", "01"): "target_failed",
        ("c2", "02"): "cancelled",
        ("c2", "03"): "cancelled",
    }
    return run


def _finished(run, measurement_id):
    return {
        (r["payload"]["case_id"], r["payload"]["sample_id"]): r["payload"]["outcome"]
        for r in _records(_measurement_events(run, measurement_id))
        if r["event"] == "rollout_finished"
    }


def _starts(run, measurement_id):
    """`(case, sample, retry_ordinal, repair)` of every `rollout_started`, in order."""

    return [
        (p["case_id"], p["sample_id"], p.get("retry_ordinal", 0), p.get("repair"))
        for p in (
            r["payload"]
            for r in _records(_measurement_events(run, measurement_id))
            if r["event"] == "rollout_started"
        )
    ]


def _listings(run):
    return [p for p in payloads(run, "campaign_suspended") if p["reason"] == "champion_incomplete"]


def _assert_listed(run, snapshot, measurement_id):
    """The latest record is the listing of `measurement_id`'s three gaps, the
    stage-2 dispatch ran before it, and the journal replays and audits clean."""

    assert snapshot.finished is None
    (listing,) = _listings(run)
    assert _records(run / "campaign.jsonl")[-1]["payload"] == listing
    assert listing["owner"] == "target"
    evidence = listing["evidence"]
    assert (evidence["evidence_kind"], evidence["measurement_id"]) == (
        "champion_rows",
        measurement_id,
    )
    assert (evidence["count"], evidence["truncated"]) == (3, False)
    assert [
        (r["case_id"], r["sample_id"], r["retry_ordinal"], r["owner"]) for r in evidence["rows"]
    ] == LISTED
    assert {r["measurement_id"] for r in evidence["rows"]} == {measurement_id}
    assert [
        p["stage"] for p in payloads(run, "stage_opened") if p["measurement_id"] == measurement_id
    ] == [1, 2]
    assert journal_consistent(run).finished is None
    assert audit(run)["mismatches"] == []


async def _completes(root, monkeypatch, adapter, run, **options):
    """The Target repaired, the next plain resume starts each listed pair once
    as an ordinary D1 retry (ordinal 1, no `repair` key), excludes nothing
    and completes the campaign; the earlier journal is its prefix."""

    measurement_id = adapter.measurement_id
    history = (run / "campaign.jsonl").read_bytes()
    before = len(_starts(run, measurement_id))
    adapter.faults.clear()
    run, snapshot = await _run(root, monkeypatch, adapter, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    assert _starts(run, measurement_id)[before:] == [
        (case, sample, ordinal + 1, None) for case, sample, ordinal, _owner in LISTED
    ]
    (reserved,) = [
        p for p in payloads(run, "repairs_dispatched") if p["measurement_id"] == measurement_id
    ]
    assert reserved["count"] == len(LISTED)
    assert not [p for p in payloads(run, "pairs_excluded") if p["measurement_id"] == measurement_id]
    assert len(_listings(run)) == 1
    assert audit(run)["mismatches"] == []
    return run, snapshot


# -- the Champion measurements family 14 measures afresh -----------------------


async def test_a_cut_stop_in_the_seed_baseline_is_listed_then_completed(tmp_path, monkeypatch):
    """The seed baseline (N = 0, one round)."""

    adapter = _GapTarget(tmp_path / "run", CHAMPION0)
    run = await _cut_stop(tmp_path, monkeypatch, adapter)
    run, snapshot = await _run(tmp_path, monkeypatch, adapter)
    _assert_listed(run, snapshot, CHAMPION0)
    await _completes(tmp_path, monkeypatch, adapter, run)


async def test_a_cut_stop_in_a_rebaseline_round_baseline_is_listed_then_completed(
    tmp_path, monkeypatch
):
    """A round baseline (N = 0): round 1 is cut right after its Attempt is
    bound; the operator moves the Target's settings and accepts `rebaseline`,
    which closes round 1 `condition_changed` and binds `ROUND_TWO` afresh. The
    stop is cut there, and the next resume lists `ROUND_TWO`'s gaps."""

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization

    run = tmp_path / "run"
    adapter = _GapTarget(run, ROUND_TWO)
    options = {"rounds": 2}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_measurement_bound", call=2)
        with pytest.raises(Crash):
            await _run(tmp_path, patch, adapter, **options)
    assert [p["measurement_id"] for p in payloads(run, "measurement_bound")][0] == CHAMPION0
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await _run(tmp_path, monkeypatch, adapter, **options)
    assert "rebaseline" in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-13T00:00:00Z",
        effect="rebaseline",
    )
    await _cut_stop(tmp_path, monkeypatch, adapter, authorizations=[decision], **options)
    assert [(p["round"], p["outcome"]) for p in payloads(run, "round_closed")] == [
        (1, "condition_changed")
    ]
    assert [p["measurement_id"] for p in payloads(run, "baseline_bound")] == [CHAMPION0, ROUND_TWO]

    run, snapshot = await _run(tmp_path, monkeypatch, adapter, **options)
    _assert_listed(run, snapshot, ROUND_TWO)
    run, _ = await _completes(tmp_path, monkeypatch, adapter, run, **options)
    assert [p["measurement_id"] for p in payloads(run, "baseline_complete")] == [
        CHAMPION0,
        ROUND_TWO,
    ]


@pytest.mark.parametrize("round_stop", ["first_accepted", "all_attempts"])
async def test_a_cut_stop_in_a_refresh_is_listed_then_completed(tmp_path, monkeypatch, round_stop):
    """A refresh (`refresh: 1`, two rounds), under each `round_stop`: round 2
    refreshes the seed baseline; the stop is cut inside the refresh, the next
    resume lists the refresh's gaps with nothing grouped or closed in round 2,
    and the one after completes the refresh before round 2 groups."""

    options = {"rounds": 2, "refresh": 1, "round_stop": round_stop}
    adapter = _GapTarget(tmp_path / "run", REFRESH)
    run = await _cut_stop(tmp_path, monkeypatch, adapter, **options)
    run, snapshot = await _run(tmp_path, monkeypatch, adapter, **options)
    _assert_listed(run, snapshot, REFRESH)
    assert [entry["completed"] for entry in snapshot.refreshes] == [None]
    assert [p["round"] for p in payloads(run, "grouping_started")] == [1]
    assert [p["round"] for p in payloads(run, "round_closed")] == [1]

    run, _ = await _completes(tmp_path, monkeypatch, adapter, run, **options)
    (completed,) = payloads(run, "baseline_refresh_completed")
    assert completed["measurement_id"] == REFRESH
    events = [r["event"] for r in _records(run / "campaign.jsonl")]
    assert events.index("baseline_refresh_completed") < max(
        i for i, event in enumerate(events) if event == "grouping_started"
    )


async def test_a_forged_d1_retry_on_an_unlisted_champion_pair_is_a_finding(tmp_path, monkeypatch):
    """Rule (c) in family 14 (round 2, R10): an uncut stop on the seed
    baseline ends `stopped(cancelled)` and lists nothing, so a D1 retry of
    `(c2, 02)`'s cancelled try, which S1 admits under `row_provenance`, is
    named by no suspension; the audit reports it and only it."""

    run = tmp_path / "run"
    adapter = _GapTarget(run, CHAMPION0)
    stop = _cancel_when(run, CHAMPION0, b'"outcome":"target_failed"')
    run, snapshot = await _run(tmp_path, monkeypatch, adapter, cancelled=stop)
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert not _listings(run)
    assert _finished(run, CHAMPION0)[("c2", "02")] == "cancelled"
    assert audit(run)["mismatches"] == []
    ordinal = _forge_d1_after_cancel(run, CHAMPION0, "c2", "02")
    assert ordinal == 1
    detail = f"the repair try {ordinal} of c2/02 was named by no suspension"
    found = audit(run)["mismatches"]
    assert [(m["code"], m["detail"]) for m in found] == [("repair_try_mismatch", detail)], found


# -- G6: a record the resume owes is written first, the listing later ---------


def _events_after(run, before):
    return [r["event"] for r in _records(run / "campaign.jsonl")[before:]]


def _decision(refused, effect):
    from evolution.tree.execution.authorizations import Authorization

    assert effect in refused.effects
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "accept changed target",
        now="2026-10-13T00:00:00Z",
        effect=effect,
    )


async def test_an_owed_connection_suspension_is_written_before_the_listing(tmp_path, monkeypatch):
    """G6, an owed connection suspension (H10, `_connection_record_owed`).

    Not reachable from a genuine run today: a binding's probe precedes its
    first admitted start, and once that boundary is durable no probe runs
    again in the epoch, so a failed preflight never follows a stranded
    Champion stop under the same binding (a `continue` rebinds the stopped
    measurement, a `rebaseline` freezes it). The prefix here is genuine; only
    one resume is told that the boundary is not durable
    (`_connection_boundary`), so the real gate probes, the probe instance is
    unavailable, and the power is cut right after `connection_preflight_failed`
    and before its suspension. The audit reports exactly that preflight start
    (it follows its binding's admitted preflight) and nothing else.

    The next plain resume writes the owed connection suspension alone
    (`recover_connection`) and lists nothing. The one after that dispatches
    under the durable boundary with no new probe, runs stage 2 and lists the
    gaps; the next completes the campaign.
    """

    from evolution.tree.campaign import _Campaign

    run = tmp_path / "run"
    adapter = _GapTarget(run, CHAMPION0)
    await _cut_stop(tmp_path, monkeypatch, adapter)
    before = len(_records(run / "campaign.jsonl"))
    adapter.fail_probe = True
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "_connection_boundary", lambda self, digest: False)
        _cut_hook(patch, "after_connection_preflight_terminal")
        with pytest.raises(Crash):
            await _run(tmp_path, patch, adapter)
    assert _events_after(run, before) == [
        "connection_preflight_started",
        "connection_preflight_failed",
    ]
    (seam, failed) = _records(run / "campaign.jsonl")[before:]
    assert failed["payload"]["status"] == "connection_preflight_unavailable"
    adapter.fail_probe = False

    def seam_only():
        found = audit(run)["mismatches"]
        assert [(row["sequence"], row["event"]) for row in found] == [
            (seam["sequence"], "connection_preflight_started")
        ], found
        assert "follows its admitted preflight" in found[0]["detail"]

    seam_only()

    before = len(_records(run / "campaign.jsonl"))
    run, snapshot = await _run(tmp_path, monkeypatch, adapter)
    assert _events_after(run, before) == ["campaign_suspended"]
    assert payloads(run, "campaign_suspended") == [
        {
            "reason": "connection_preflight_unavailable",
            "owner": "connection",
            "evidence": {
                "evidence_kind": "stage_record",
                "seq": failed["sequence"],
                "operation_id": failed["operation_id"],
            },
        }
    ]
    assert snapshot.finished is None
    assert snapshot.connection.owes_suspension is None
    assert not _listings(run)
    assert journal_consistent(run).finished is None
    seam_only()

    before = len(_records(run / "campaign.jsonl"))
    run, snapshot = await _run(tmp_path, monkeypatch, adapter)
    appended = _events_after(run, before)
    assert not [name for name in appended if name.startswith("connection_preflight")]
    assert appended[0] == "stage_opened" and appended[-1] == "campaign_suspended"
    assert snapshot.finished is None
    (listing,) = _listings(run)
    assert listing["evidence"]["measurement_id"] == CHAMPION0
    assert [
        (r["case_id"], r["sample_id"], r["retry_ordinal"], r["owner"])
        for r in listing["evidence"]["rows"]
    ] == LISTED
    seam_only()

    adapter.faults.clear()
    run, snapshot = await _run(tmp_path, monkeypatch, adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    assert len(_listings(run)) == 1
    seam_only()


async def test_an_owed_grouper_suspension_is_written_before_a_later_listing(tmp_path, monkeypatch):
    """G6, an owed Grouper suspension (H8): the closest genuine order.

    A Grouper cause beside an unfinished Champion measurement is refused by
    the lifecycle itself: a grouping starts only once its round's baseline
    and refresh are complete (`_check_refresh_completed`), and the next
    Champion measurement opens only after a later round binds it. So the
    owed record comes first in time: the `chat` Grouper's provider fails
    round 1's grouping and the power is cut after `grouping_failed
    (role_failed)`, before its suspension; the operator moves the Target's
    settings and accepts `rebaseline`. That resume closes round 1
    `condition_changed` and writes the owed `grouper_provider_failed`
    suspension; it binds and measures nothing and lists nothing. The next
    resume binds `ROUND_TWO`, where the stop is cut; the one after lists its
    gaps, and the last completes the campaign with a clean audit.
    """

    from evolution.tree.evolve import EvolveRefused
    from tests.model_grouping_fakes import chat_grouper_section
    from tests.test_tree_campaign_family14_grouper_run import _chain_client

    client = _chain_client(monkeypatch, "outage", "admitted")
    run = tmp_path / "run"
    adapter = _GapTarget(run, ROUND_TWO)
    options = {"rounds": 2, "grouper": chat_grouper_section()}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_failed")
        with pytest.raises(Crash):
            await _run(tmp_path, patch, adapter, **options)
    failed = _records(run / "campaign.jsonl")[-1]
    assert failed["event"] == "grouping_failed"
    assert (failed["payload"]["round"], failed["payload"]["code"]) == (1, "role_failed")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await _run(tmp_path, monkeypatch, adapter, **options)
    decision = _decision(refused.value, "rebaseline")

    before = len(_records(run / "campaign.jsonl"))
    run, snapshot = await _run(tmp_path, monkeypatch, adapter, authorizations=[decision], **options)
    assert _events_after(run, before) == [
        "authorization_applied",
        "identity_rebased",
        "round_closed",
        "campaign_suspended",
    ]
    (suspended,) = payloads(run, "campaign_suspended")
    assert (suspended["reason"], suspended["owner"]) == ("grouper_provider_failed", "grouper")
    assert suspended["evidence"]["seq"] == failed["sequence"]
    assert snapshot.finished is None
    assert not _listings(run)
    assert audit(run)["mismatches"] == []

    await _cut_stop(tmp_path, monkeypatch, adapter, **options)
    run, snapshot = await _run(tmp_path, monkeypatch, adapter, **options)
    _assert_listed(run, snapshot, ROUND_TWO)
    assert [p["reason"] for p in payloads(run, "campaign_suspended")] == [
        "grouper_provider_failed",
        "champion_incomplete",
    ]
    run, _ = await _completes(tmp_path, monkeypatch, adapter, run, **options)
    assert client.script == []
    assert [p["round"] for p in payloads(run, "grouping_finished")] == [2]


# -- G6: a `probe_unavailable` that replaced the pending record wins -----------


def test_a_probe_unavailable_that_replaced_the_pending_record_wins():
    """`stop()` replaces whatever this invocation decided with
    `probe_unavailable` while the prefix records an unavailable probe check.
    The listing then waits for that decision: `_list_champion_gaps` returns
    without deciding anything, and the pending record stays
    `probe_unavailable`. (Without such a pending record the same durable
    check is an invariant breach: Task 9's guard tests.)

    Unit level, over the guard's state with the real `stop()`: no genuine
    family-14 run holds an unavailable probe check beside an unfinished
    Champion measurement, since the check is the Reflector's, after its
    round's baseline completed, and the connection gate refuses every probe
    while one stands (`_connection_may_probe`).
    """

    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign, _Pending

    evidence = ({"kind": "journal_record"},)

    class _Stopping(_Guarded):
        stop = _Campaign.stop

        def probe_failure_evidence(self):
            return evidence

        def _suspend_or_finish(self, pending):
            return pending

    guarded = _Stopping()
    guarded.state.rounds[1] = SimpleNamespace(probe_check={"status": "unavailable"})
    guarded.stop(_Pending("infrastructure_failed", "infrastructure_failed", "none"))
    decided = guarded.pending
    assert (decided.reason, decided.evidence) == ("probe_unavailable", evidence)
    assert guarded._champion_listing_admissible(SEED) is False
    assert guarded._list_champion_gaps(SEED) is None
    assert guarded.pending is decided
