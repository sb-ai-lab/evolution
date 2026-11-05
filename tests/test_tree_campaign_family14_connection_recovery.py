"""H10 Task 8: connection-preflight recovery and condition changes (F0 R62, C§4.1, C§8).

Every run is a genuine `/15` campaign (`family14_run`). A crash is either a
campaign hook raising `Crash` (`_cut_hook`), or a power cut (`power_cut`):
once the chosen S1 record is durable the process is dead, so no later journal
record, trace or Target release happens and the instance the cut left live
stays live in the one `FakeAdapter` both invocations share. The resume must
settle it.

For every cut: each preflight start has exactly one terminal, no Target
instance is left live, no operation id is reused, the audit is clean, and the
structural facts (campaign event names without the connection records, ledger
events, row identities) equal an uncut control's.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.authorizations import Authorization
from evolution.tree.execution.interaction import RolloutInfrastructureError
from tests.campaign_drivers import (
    EDITED,
    STATISTICAL,
    _cut_after_cohort_stage,
    _cut_hook,
    _measurement_events,
    _records,
    _Resettable,
    _ResettableTagged,
)
from tests.campaign_fakes import CONNECTION_PROBING, Crash
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_connection_run import (
    CONNECTION_EVENTS,
    ConnectedAdapter,
    campaign,
    connected_run,
    expected_binding,
    measurement_starts,
    names,
    payloads,
)
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run

pytestmark = pytest.mark.slow

NOW = "2026-10-12T00:00:00Z"


class PowerCut(BaseException):
    """The process dies: no `except Exception` holds it, and nothing runs after it."""


class ResettableConnected(_Resettable, ConnectedAdapter):
    """`ConnectedAdapter` whose Target settings the operator changes between invocations."""


def power_cut(
    patch: pytest.MonkeyPatch, adapter: Any, cut: Callable[[str, dict[str, Any]], bool]
) -> dict[str, bool]:
    """Die right after the first S1 record `cut(event, payload)` accepts is durable.

    From then on every S1 append, campaign append, trace write and Target
    release raises `PowerCut` without effect, as if the process were gone.
    """

    from evolution.tree.campaign import _Campaign
    from evolution.tree.execution.journal import Journal
    from evolution.tree.target import connection_probe as probe_module

    state = {"dead": False}
    journal_append = Journal.append

    def append(self: Any, event: str, payload: dict[str, Any], operation_id: Any = None) -> int:
        if state["dead"]:
            raise PowerCut("the process is gone")
        sequence = journal_append(self, event, payload, operation_id)
        if cut(event, payload):
            state["dead"] = True
            raise PowerCut("power cut")
        return sequence

    campaign_append = _Campaign.append

    def append_campaign(self: Any, *args: Any, **kwargs: Any) -> Any:
        if state["dead"]:
            raise PowerCut("the process is gone")
        return campaign_append(self, *args, **kwargs)

    release = adapter.release

    async def released(instance_id: str) -> None:
        if state["dead"]:
            raise PowerCut("the process is gone")
        await release(instance_id)

    write_trace = probe_module._write_trace

    def write(*args: Any, **kwargs: Any) -> Any:
        if state["dead"]:
            raise PowerCut("the process is gone")
        return write_trace(*args, **kwargs)

    patch.setattr(Journal, "append", append)
    patch.setattr(_Campaign, "append", append_campaign)
    patch.setattr(adapter, "release", released)
    patch.setattr(probe_module, "_write_trace", write)
    return state


def first(event_name: str) -> Callable[[str, dict[str, Any]], bool]:
    """The first S1 record named `event_name` (the probe's: it precedes every dispatch)."""

    seen: list[str] = []

    def cut(event: str, payload: dict[str, Any]) -> bool:
        if event == event_name and not seen:
            seen.append(event)
            return True
        return False

    return cut


def admitted_start(event: str, payload: dict[str, Any]) -> bool:
    """The binding's boundary: the first measurement start carrying an admission."""

    return event == "rollout_started" and payload.get("connection_admission") is not None


# --- invariants ------------------------------------------------------------------------


def starts(run: Path) -> list[dict[str, Any]]:
    return [r for r in campaign(run) if r["event"] == "connection_preflight_started"]


def terminals_of(run: Path, operation_id: str) -> list[dict[str, Any]]:
    return [
        r
        for r in campaign(run)
        if r["event"] in CONNECTION_EVENTS[1:] and r["operation_id"] == operation_id
    ]


def assert_one_terminal_per_start(run: Path) -> list[str]:
    operation_ids = [r["operation_id"] for r in starts(run)]
    assert len(set(operation_ids)) == len(operation_ids), "an operation id was reused"
    for operation_id in operation_ids:
        assert len(terminals_of(run, operation_id)) == 1, operation_id
    every = [r["operation_id"] for r in campaign(run) if r.get("operation_id")]
    others = [op for op in every if op not in operation_ids]
    assert not set(operation_ids) & set(others), "a probe operation id was reused"
    # Every probe journal on disk is declared by a start.
    root = run / "connection"
    if root.exists():
        assert {child.name for child in root.iterdir()} <= set(operation_ids)
    return operation_ids


def structure(run: Path, ignore: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Event names without the connection records, ledger events and row identities.

    `ignore` names records a cut legitimately adds (a rollout the crash left
    open is observed `indeterminate` on resume).
    """

    def rows(found: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return sorted({(row["case_id"], row["sample_id"]) for row in found})

    return {
        "events": [
            name
            for name in names(run)
            if name not in CONNECTION_EVENTS and name != "campaign_suspended" and name not in ignore
        ],
        "ledger": [record["event"] for record in _records(run / "artifacts.jsonl")],
        "rows": {mid: rows(found) for mid, found in measurement_starts(run).items()},
    }


async def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    run, snapshot, adapter = await connected_run(tmp_path / "control", monkeypatch)
    assert snapshot.finished["outcome"] == "completed" and adapter.live == {}
    return structure(run)


def assert_recovered(
    run: Path,
    snapshot: Any,
    adapter: Any,
    uncut: dict[str, Any],
    ignore: frozenset[str] = frozenset(),
) -> list[str]:
    operation_ids = assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert_clean_run(run, snapshot)
    assert structure(run, ignore) == uncut
    # The last preflight admitted the binding's one boundary.
    admissions = [
        row["connection_admission"]
        for rows in measurement_starts(run).values()
        for row in rows
        if row["connection_admission"] is not None
    ]
    assert len(admissions) == 1
    (finished,) = [r for r in campaign(run) if r["event"] == "connection_preflight_finished"][-1:]
    assert admissions[0]["operation_id"] == finished["operation_id"]
    return operation_ids


def appended_since(run: Path, before: int) -> list[str]:
    return names(run)[before:]


# --- crash cuts inside the probe: an open start settles `interrupted` -------------------


PROBE_CUTS = {
    # (hook or S1 record, the interrupted record's cleanup, whether a probe journal was born)
    "started_unborn": ("after_connection_preflight_started", "not_needed", False),
    "journal_born": ("after_connection_probe_journal_born", "not_needed", True),
    "materialized": ("target_materialize_finished", "released", True),
    "rollout_started": ("rollout_started", "released", True),
    "scoring_started": ("scoring_started", "released", True),
    "released": ("target_release_finished", "released", True),
}


@pytest.mark.parametrize("cut", list(PROBE_CUTS))
async def test_a_crash_inside_the_probe_settles_its_start_and_probes_again(
    tmp_path, monkeypatch, cut
):
    """R62: an open start's leftover is released, its start closes
    `interrupted{reason: interrupted}`, and the resume probes again under a new
    operation id before its first dispatch."""

    seam, cleanup, born = PROBE_CUTS[cut]
    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        if seam.startswith("after_"):
            _cut_hook(patch, seam)
            with pytest.raises(Crash):
                await connected_run(tmp_path, patch, adapter)
        else:
            power_cut(patch, adapter, first(seam))
            with pytest.raises(PowerCut):
                await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (start,) = starts(run)
    crashed = start["operation_id"]
    assert names(run)[-1] == "connection_preflight_started"
    assert not (run / "measurements").exists() or not any(measurement_starts(run).values())
    if cut in ("materialized", "rollout_started", "scoring_started"):
        assert set(adapter.live) == set(adapter.probes)  # the cut left the probe's instance
    before = len(names(run))

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    appended = appended_since(run, before)
    assert appended[:3] == [
        "connection_preflight_interrupted",
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert interrupted["reason"] == "interrupted"
    assert interrupted["cleanup"] == cleanup
    assert interrupted["trace_digest"] is None  # the process died before the trace
    assert len(interrupted["evidence"]) == (1 if born else 0)
    assert terminals_of(run, crashed)[0]["event"] == "connection_preflight_interrupted"
    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert operation_ids[0] == crashed and len(operation_ids) == 2


async def test_an_unborn_probe_journal_directory_is_replaced(tmp_path, monkeypatch):
    """A crash inside `Journal.create` leaves `connection/<op>/` with no record:
    it holds nothing durable, so recovery removes it and the start closes with
    no evidence (ruling 22)."""

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_started")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (start,) = starts(run)
    unborn = run / "connection" / start["operation_id"]
    unborn.mkdir(parents=True, mode=0o700)
    (unborn / "events.jsonl").write_bytes(b"")

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert interrupted == {
        "reason": "interrupted",
        "trace_digest": None,
        "cleanup": "not_needed",
        "evidence": [],
    }
    assert not unborn.exists()
    assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert_clean_run(run, snapshot)


# --- crash cuts after the probe's terminal --------------------------------------------


async def test_a_crash_after_a_compatible_probe_before_dispatch_probes_again(tmp_path, monkeypatch):
    """C§4.1: the boundary is the first local start, not the compatible record.
    The resume probes again under a new operation id; the old compatible
    record stays history and admits nothing."""

    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_terminal")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    assert names(run)[-1] == "connection_preflight_finished"
    (old,) = starts(run)

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert operation_ids[0] == old["operation_id"] and len(operation_ids) == 2
    old_finished, new_finished = [
        r for r in campaign(run) if r["event"] == "connection_preflight_finished"
    ]
    assert old_finished["operation_id"] == old["operation_id"]
    assert new_finished["operation_id"] == operation_ids[1]
    assert old["payload"]["invocation_id"] != starts(run)[1]["payload"]["invocation_id"]
    assert len(adapter.probes) == 2


async def test_a_crash_after_the_boundary_resumes_without_a_probe(tmp_path, monkeypatch):
    """C§4.1: once the binding's first local start is durable the resume does
    not probe, and it dispatches only the owed work, every start naming `D`
    with a null admission."""

    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, admitted_start)
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    ((mid, (boundary,)),) = [(m, rows) for m, rows in measurement_starts(run).items() if rows]
    assert adapter.live  # the measurement's instance the cut left
    before = len(names(run))

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    assert not [n for n in appended_since(run, before) if n in CONNECTION_EVENTS]
    assert len(adapter.probes) == 1
    resumed = measurement_starts(run)[mid][1:]
    assert resumed and all(row["connection_admission"] is None for row in resumed)
    assert {row["connection_binding_digest"] for row in resumed} == {expected_binding(snapshot)}
    assert boundary["connection_admission"]["operation_id"] == starts(run)[0]["operation_id"]
    # The row the cut left open is observed indeterminate, as for any crash.
    assert names(run).count("indeterminate_observed") == 1
    assert_recovered(run, snapshot, adapter, uncut, frozenset({"indeterminate_observed"}))


async def test_a_crash_between_the_failed_terminal_and_its_suspension(tmp_path, monkeypatch):
    """R61, choice 17: the resume writes the owed suspension first, before any
    other record, and ends suspended; the next run probes afresh."""

    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()  # connection_event_missing
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_terminal")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    assert names(run)[-1] == "connection_preflight_failed"
    (failed,) = [r for r in campaign(run) if r["event"] == "connection_preflight_failed"]
    before = len(names(run))

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    appended = appended_since(run, before)
    assert appended[0] == "campaign_suspended"
    assert not [n for n in appended if n in CONNECTION_EVENTS or n == "stage_opened"]
    assert payloads(run, "campaign_suspended") == [
        {
            "reason": "connection_incompatible",
            "owner": "connection",
            "evidence": {
                "evidence_kind": "stage_record",
                "seq": failed["sequence"],
                "operation_id": failed["operation_id"],
            },
        }
    ]
    assert snapshot.finished is None and snapshot.connection.owes_suspension is None
    assert adapter.live == {}

    adapter.probe_events = ({"type": "assistant_message", "content": "probe-reply-h10"},)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert operation_ids[0] == failed["operation_id"] and len(operation_ids) == 2


async def test_a_crash_after_the_suspension_resumes_with_a_fresh_probe(tmp_path, monkeypatch):
    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_suspended")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    assert names(run)[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    before = len(names(run))

    adapter.probe_events = ({"type": "assistant_message", "content": "probe-reply-h10"},)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    appended = appended_since(run, before)
    assert appended[:2] == ["connection_preflight_started", "connection_preflight_finished"]
    assert names(run).count("campaign_suspended") == 1
    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert len(operation_ids) == 2


async def test_an_unsettled_leftover_fails_unavailable_and_suspends(tmp_path, monkeypatch):
    """C§4.4: when recovery cannot release the probe's instance, the start
    closes `failed / unavailable / cleanup_failed` and the suspension follows
    at once. The repaired Target's next resume releases the leftover and
    probes afresh."""

    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, first("rollout_started"))
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (start,) = starts(run)
    leftover = set(adapter.live)
    assert leftover == set(adapter.probes)
    before = len(names(run))

    adapter.probe_refuse_release = True
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    assert appended_since(run, before)[:2] == ["connection_preflight_failed", "campaign_suspended"]
    (failed,) = payloads(run, "connection_preflight_failed")
    assert (failed["status"], failed["cause_code"], failed["cleanup"]) == (
        "connection_preflight_unavailable",
        "cleanup_failed",
        "failed",
    )
    assert (failed["detail_code"], failed["manifest_path"], failed["observed_path"]) == (
        None,
        None,
        None,
    )
    assert failed["trace_digest"] is None and len(failed["evidence"]) == 1
    (suspension,) = payloads(run, "campaign_suspended")
    assert suspension["reason"] == "connection_preflight_unavailable"
    assert suspension["evidence"]["operation_id"] == start["operation_id"]
    assert snapshot.finished is None
    assert set(adapter.live) == leftover  # recorded as a failed cleanup, never silent

    adapter.probe_refuse_release = False
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert len(operation_ids) == 2


# --- resume after a failure (F0 §8) ---------------------------------------------------


async def test_a_cleared_transient_cause_probes_afresh_and_fans_out(tmp_path, monkeypatch):
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    uncut = await control(tmp_path, monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_materialize = RolloutInfrastructureError("down", "target_unavailable")
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert names(run)[-2:] == ["connection_preflight_failed", "campaign_suspended"]

    adapter.probe_materialize = None
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    operation_ids = assert_recovered(run, snapshot, adapter, uncut)
    assert len(operation_ids) == 2
    assert [r["event"] for r in terminals_of(run, operation_ids[1])] == [
        "connection_preflight_finished"
    ]


async def test_the_same_incompatibility_fails_again_under_a_new_operation(tmp_path, monkeypatch):
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()
    run, _, _ = await connected_run(tmp_path, monkeypatch, adapter)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)

    operation_ids = assert_one_terminal_per_start(run)
    assert len(operation_ids) == 2
    failed = payloads(run, "connection_preflight_failed")
    assert [p["detail_code"] for p in failed] == ["connection_event_missing"] * 2
    suspensions = payloads(run, "campaign_suspended")
    assert [s["evidence"]["operation_id"] for s in suspensions] == operation_ids
    assert names(run)[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    assert "stage_opened" not in names(run)
    assert snapshot.finished is None and adapter.live == {}


# --- sibling admission and creation ---------------------------------------------------


async def test_an_undeclared_probe_journal_directory_refuses_the_resume(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_terminal")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (run / "connection" / ("e" * 32)).mkdir(mode=0o700)
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as refused:
        await connected_run(tmp_path, monkeypatch, adapter)
    assert refused.value.code == "undeclared_sibling"
    assert (run / "campaign.jsonl").read_bytes() == before


async def test_creation_refuses_an_existing_connection_directory(tmp_path, monkeypatch):
    """`evolve` refuses a foreign entry first (`run_dir_not_campaign`); C1's own
    creation check refuses `connection/` too, for a caller that skips it."""

    from evolution.tree import evolve as evolve_module
    from evolution.tree.evolve import EvolveRefused

    (tmp_path / "run" / "connection").mkdir(parents=True, mode=0o700)
    with pytest.raises(EvolveRefused) as refused:
        await connected_run(tmp_path, monkeypatch)
    assert refused.value.code == "run_dir_not_campaign"
    with monkeypatch.context() as patch:
        patch.setattr(evolve_module, "run_dir_state", lambda *args, **kwargs: "new")
        with pytest.raises(EvolveRefused) as refused:
            await connected_run(tmp_path, patch)
    assert refused.value.code == "run_root_not_empty"
    assert not (tmp_path / "run" / "campaign.jsonl").exists()


# --- close and cancellation settle an open preflight first ----------------------------


def _decision(refused: Any, effect: str | None) -> Authorization:
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "accept the changed condition",
        now=NOW,
        effect=effect,
    )


async def test_a_close_settles_an_open_preflight_before_its_terminal(tmp_path, monkeypatch):
    """ADR-0029, ruling 19: an accepted close finds the probe the crash left
    open; its leftover is released and its start closes `interrupted` before
    the close's two records."""

    from evolution.tree.evolve import EvolveRefused

    adapter = ResettableConnected(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, first("target_materialize_finished"))
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    assert set(adapter.live) == set(adapter.probes)
    before = len(names(run))
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await connected_run(tmp_path, monkeypatch, adapter)
    assert "close" in refused.value.effects

    run, snapshot, _ = await connected_run(
        tmp_path, monkeypatch, adapter, authorizations=[_decision(refused.value, "close")]
    )

    assert appended_since(run, before) == [
        "connection_preflight_interrupted",
        "authorization_applied",
        "campaign_finished",
    ]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("interrupted", "released")
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == (
        "stopped",
        "condition_changed",
    )
    assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert audit(run)["mismatches"] == []


async def test_a_cancellation_terminal_settles_an_open_preflight_first(tmp_path, monkeypatch):
    """A durable `cancelled` role terminal's owed `campaign_finished` waits for
    the open preflight: it closes `interrupted{reason: cancelled}` after its
    leftover is released.

    Not reachable from a genuine run today: a probe never runs beside an
    editor or a Reflector (the gate precedes every stage; family 14 refuses
    `linked_from`). The prefix here is genuine (a crash with the probe open);
    only `adopt`'s reduction is told a cancelled role terminal is durable, so
    the real `_finish_owed_cancellation` path runs over it.
    """

    from evolution.tree import campaign as campaign_module

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, first("rollout_started"))
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    before = len(names(run))
    real_reduce = campaign_module.reduce

    def reduce(*args: Any, **kwargs: Any) -> Any:
        reduced = real_reduce(*args, **kwargs)
        return dataclasses.replace(reduced, cancelled_role_seq=len(args[0]))

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module, "reduce", reduce)
        run, snapshot, _ = await connected_run(tmp_path, patch, adapter)

    assert appended_since(run, before) == ["connection_preflight_interrupted", "campaign_finished"]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("cancelled", "released")
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert_one_terminal_per_start(run)
    assert adapter.live == {}


# --- condition changes (ADR-0020) -----------------------------------------------------


def _epoch_starts(run: Path, epoch: int) -> list[dict[str, Any]]:
    return [r for r in starts(run) if r["payload"]["epoch"] == epoch]


def _admitted(run: Path, digest: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (mid, row["connection_admission"])
        for mid, rows in measurement_starts(run).items()
        for row in rows
        if row["connection_binding_digest"] == digest and row["connection_admission"] is not None
    ]


async def _changed(tmp_path, monkeypatch, effect, *, cut, rounds=2, **extra):
    """Cut a genuine run, move the Target's settings, accept `effect`, finish."""

    from evolution.tree.evolve import EvolveRefused

    adapter = _ResettableTagged(used=EDITED["used"])
    options = {"adapter": adapter, "campaign": {"rounds": rounds}, **extra}
    with monkeypatch.context() as patch:
        cut(patch)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    assert effect in refused.value.effects
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        STATISTICAL,
        authorizations=[_decision(refused.value, effect)],
        **options,
    )
    assert snapshot.finished is not None
    # No probe instance is left live. (Epoch-0 cohort instances the cut left
    # owned stay live across the change: pre-existing, outside H10.)
    assert not set(adapter.live) & set(adapter.probe_instances)
    assert adapter.probe_released == adapter.probe_instances
    return run, snapshot


def _assert_new_binding_probed(run: Path, snapshot: Any) -> tuple[dict[str, Any], str]:
    old, new = expected_binding(snapshot, 0), expected_binding(snapshot, 1)
    assert old != new
    (probe,) = _epoch_starts(run, 1)
    assert probe["payload"]["binding_digest"] == new
    assert [r["event"] for r in terminals_of(run, probe["operation_id"])] == [
        "connection_preflight_finished"
    ]
    # The probe precedes the first new-condition rollout: that start, and only
    # it, carries the new binding's admission, naming this probe.
    ((_mid, admission),) = _admitted(run, new)
    assert admission["operation_id"] == probe["operation_id"]
    assert admission["controller_invocation_id"] == probe["payload"]["invocation_id"]
    for mid, rows in measurement_starts(run).items():
        epoch = snapshot.measurements[mid].get("epoch", 0)
        for row in rows:
            if epoch == 1:
                assert row["connection_binding_digest"] == new, mid
    assert audit(run)["mismatches"] == []
    return probe, new


async def test_a_continue_probes_before_the_first_new_condition_rollout(tmp_path, monkeypatch):
    """ADR-0020: the change opens epoch 1 and so a new binding; the rebound
    measurements carry their scored rows, which grant no readiness, and the
    first new-condition start is admitted by a fresh probe."""

    run, snapshot = await _changed(
        tmp_path, monkeypatch, "continue", cut=lambda patch: _cut_after_cohort_stage(patch, 1)
    )
    rebound = payloads(run, "measurement_rebound")
    assert rebound and all(p["imported"] > 0 for p in rebound)
    probe, _ = _assert_new_binding_probed(run, snapshot)
    events = names(run)
    assert events.index("identity_rebased") < campaign(run).index(probe)


async def test_a_fully_carried_baseline_grants_no_readiness(tmp_path, monkeypatch):
    """The closest reachable form of 'a baseline made only of imported rows
    grants no readiness' (family 14 refuses `linked_from`, so no inherited
    baseline exists). A cut after the Champion member completed, then a
    `continue`: the finalist is re-bound with every row carried and becomes
    round 2's baseline without one local rollout. Nothing local runs under the
    new binding until round 3's baseline refresh, and that first local start
    is admitted by its own fresh probe."""

    run, snapshot = await _changed(
        tmp_path,
        monkeypatch,
        "continue",
        cut=lambda patch: _cut_hook(patch, "after_cohort_measurement_completed"),
        rounds=3,
        family14_settings={"refresh": 1},
    )
    (rebound,) = payloads(run, "measurement_rebound")
    carried = rebound["measurement_id"]
    rows = [r["event"] for r in _records(_measurement_events(run, carried))]
    assert "sample_imported" in rows and "rollout_started" not in rows
    assert carried in [p["measurement_id"] for p in payloads(run, "baseline_bound")]
    probe, new = _assert_new_binding_probed(run, snapshot)
    ((admitted_mid, _),) = _admitted(run, new)
    (refresh,) = payloads(run, "baseline_refresh_bound")
    assert admitted_mid != carried and admitted_mid == refresh["measurement_id"]
    records = campaign(run)
    assert records.index(probe) > max(
        i for i, r in enumerate(records) if r["event"] == "measurement_rebound"
    )


async def test_a_rebaseline_probes_before_the_fresh_baseline(tmp_path, monkeypatch):
    run, snapshot = await _changed(
        tmp_path, monkeypatch, "rebaseline", cut=lambda patch: _cut_after_cohort_stage(patch, 1)
    )
    probe, new = _assert_new_binding_probed(run, snapshot)
    records = campaign(run)
    fresh = [
        r
        for r in records[records.index(probe) :]
        if r["event"] == "baseline_bound" or r["event"] == "stage_opened"
    ]
    baselines = payloads(run, "baseline_bound")
    fresh_baseline = baselines[-1]["measurement_id"]
    ((admitted_mid, _),) = _admitted(run, new)
    assert admitted_mid == fresh_baseline
    assert any(
        r["event"] == "stage_opened" and r["payload"]["measurement_id"] == fresh_baseline
        for r in fresh
    )


class _FailingProbe(_ResettableTagged):
    """`_ResettableTagged` whose probe instance (only) can be made unavailable."""

    fail_probe = False

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
        if self.fail_probe and CONNECTION_PROBING.get():
            raise RolloutInfrastructureError("down", "target_unavailable")
        return await super().materialize(candidate, strategy, instance_id)


#: Where the first new-condition dispatch after a `continue` gates: the cut
#: before the change, extra driver settings, and the campaign record the
#: failed preflight's start follows.
FAILED_AFTER_CONTINUE = {
    "cohort-stage": (lambda patch: _cut_after_cohort_stage(patch, 1), {}, "stage_opened"),
    "first-baseline": (
        lambda patch: _cut_hook(patch, "after_measurement_bound"),
        {},
        "measurement_rebound",
    ),
    "joint-stage-rebound": (
        lambda patch: _cut_hook(patch, "after_parent_measurement_bound"),
        {},
        "comparison_bound",
    ),
    "joint-stage-fresh": (
        lambda patch: _cut_hook(patch, "after_editor_finished"),
        {},
        "comparison_bound",
    ),
    "refresh": (
        lambda patch: _cut_hook(patch, "after_cohort_measurement_completed"),
        {"family14_settings": {"refresh": 1}},
        "measurement_bound",
    ),
}


@pytest.mark.parametrize("site", list(FAILED_AFTER_CONTINUE))
async def test_a_failed_probe_after_a_continue_suspends_and_resumes(tmp_path, monkeypatch, site):
    """ADR-0020, R61: the first new-condition probe after a `continue` fails at
    each gate site. Its terminal and suspension are adjacent and end the
    invocation, nothing is admitted under the new binding, and the audit is
    clean; the repaired resume probes the new binding afresh and finishes."""

    from evolution.tree.evolve import EvolveRefused

    cut, extra, preceding = FAILED_AFTER_CONTINUE[site]
    adapter = _FailingProbe(used=EDITED["used"])
    options = {"adapter": adapter, "campaign": {"rounds": 3}, **extra}
    with monkeypatch.context() as patch:
        cut(patch)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    adapter.fail_probe = True
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        STATISTICAL,
        authorizations=[_decision(refused.value, "continue")],
        **options,
    )
    assert snapshot.finished is None
    events = names(run)
    failed_at = events.index("connection_preflight_failed", events.index("authorization_applied"))
    assert events[failed_at + 1 :] == ["campaign_suspended"]
    (failed,) = _epoch_starts(run, 1)
    assert events[failed_at - 1] == "connection_preflight_started"
    assert events[failed_at - 2] == preceding
    new = expected_binding(snapshot, 1)
    assert failed["payload"]["binding_digest"] == new
    assert payloads(run, "connection_preflight_failed")[-1]["status"] == (
        "connection_preflight_unavailable"
    )
    assert _admitted(run, new) == []
    assert audit(run)["mismatches"] == []

    adapter.fail_probe = False
    run, snapshot = await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    assert snapshot.finished is not None
    assert_one_terminal_per_start(run)
    failed_again, probe = _epoch_starts(run, 1)
    assert failed_again == failed
    assert [r["event"] for r in terminals_of(run, probe["operation_id"])] == [
        "connection_preflight_finished"
    ]
    ((_mid, admission),) = _admitted(run, new)
    assert admission["operation_id"] == probe["operation_id"]
    assert not set(adapter.live) & set(adapter.probe_instances)
    assert audit(run)["mismatches"] == []


def _version(patch: pytest.MonkeyPatch, version: str) -> None:
    from evolution.tree import campaign as campaign_module

    patch.setattr(campaign_module, "resolve_package_version", lambda: version)


@pytest.mark.parametrize("boundary", [False, True], ids=["before-boundary", "after-boundary"])
async def test_a_producing_version_rebase_probes_again_only_without_a_boundary(
    tmp_path, monkeypatch, boundary
):
    """ADR-0020: a non-comparable rebase opens no epoch, so `D` is unchanged;
    the resume probes again only when no local start under `D` is durable."""

    from evolution.tree.evolve import EvolveRefused

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        if boundary:
            power_cut(patch, adapter, admitted_start)
            with pytest.raises(PowerCut):
                await connected_run(tmp_path, patch, adapter)
        else:
            _cut_hook(patch, "after_connection_preflight_terminal")
            with pytest.raises(Crash):
                await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (first_start,) = starts(run)
    with monkeypatch.context() as patch:
        _version(patch, "0.3.0a99")
        with pytest.raises(EvolveRefused) as refused:
            await connected_run(tmp_path, patch, adapter)
        assert refused.value.effects == ("rebase",)
        run, snapshot, _ = await connected_run(
            tmp_path, patch, adapter, authorizations=[_decision(refused.value, None)]
        )
    assert snapshot.finished["outcome"] == "completed"
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == ["producing_version"]
    assert len(snapshot.epochs) == 1
    digest = expected_binding(snapshot)
    found = starts(run)
    assert {s["payload"]["binding_digest"] for s in found} == {digest}
    assert first_start["payload"]["binding_digest"] == digest
    assert len(found) == (1 if boundary else 2)
    assert len(adapter.probes) == len(found)
    assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert audit(run)["mismatches"] == []


def test_the_unborn_directory_removal_is_scoped_to_the_probe(tmp_path):
    """The recovery helper removes only `connection/<op>`, never its parent."""

    from evolution.tree.campaign import _remove_unborn_probe

    root = tmp_path / "run"
    keep = root / "connection" / ("a" * 32)
    gone = root / "connection" / ("b" * 32)
    keep.mkdir(parents=True)
    (keep / "events.jsonl").write_bytes(b"{}\n")
    gone.mkdir(parents=True)
    _remove_unborn_probe(root, "b" * 32)
    assert keep.exists() and not gone.exists() and (root / "connection").is_dir()
    _remove_unborn_probe(root, "c" * 32)  # absent: nothing to do
    shutil.rmtree(root)


async def test_a_probe_journal_that_cannot_be_reopened_writes_nothing(tmp_path, monkeypatch):
    """Ruling 16: an `IdentityError` reopening the open probe's journal (a
    producing version the run never admitted) propagates with nothing written
    to the campaign journal; once the operator admits it, the resume settles."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.execution.journal_identity import IdentityError

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, first("target_materialize_finished"))
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()

    async def unadmitted(*_args: Any, **_kwargs: Any) -> Any:
        raise IdentityError("package_version_mismatch")

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module, "release_probe_leftover", unadmitted)
        with pytest.raises(IdentityError) as raised:
            await connected_run(tmp_path, patch, adapter)
    assert str(raised.value) == "package_version_mismatch"
    assert (run / "campaign.jsonl").read_bytes() == before
    assert set(adapter.live) == set(adapter.probes)

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert_clean_run(run, snapshot)


async def test_a_cancellation_records_a_refused_release_as_a_failed_cleanup(tmp_path, monkeypatch):
    """Ruling 14: under a durable cancellation a leftover the Target refuses
    closes the start `interrupted{cancelled, cleanup: failed}` -- never a
    failed terminal or a suspension -- and the cancellation's terminal follows.
    (The same constructed trigger as the cancellation test above.)"""

    from evolution.tree import campaign as campaign_module

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        power_cut(patch, adapter, first("target_materialize_finished"))
        with pytest.raises(PowerCut):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    before = len(names(run))
    adapter.probe_refuse_release = True
    real_reduce = campaign_module.reduce

    def reduce(*args: Any, **kwargs: Any) -> Any:
        reduced = real_reduce(*args, **kwargs)
        return dataclasses.replace(reduced, cancelled_role_seq=len(args[0]))

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module, "reduce", reduce)
        run, snapshot, _ = await connected_run(tmp_path, patch, adapter)

    assert appended_since(run, before) == ["connection_preflight_interrupted", "campaign_finished"]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("cancelled", "failed")
    assert set(adapter.live) == set(adapter.probes)  # recorded, never silent


async def test_a_lost_boundary_journal_faults_before_any_probe(tmp_path, monkeypatch):
    """Ruling 24 (ADR-0008, C§4.1): the baseline journal holding the binding's
    boundary is lost after the campaign cited it. The resume refuses to probe
    over the loss: it faults on the measurement journal with no new start and
    no new admission, and once the journal is restored it finishes without
    probing again."""

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_affected_cases_selected")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch, adapter)
    run = tmp_path / "run"
    (start,) = starts(run)
    (baseline,) = [
        mid
        for mid, rows in measurement_starts(run).items()
        if rows and any(row["connection_admission"] for row in rows)
    ]
    source = _measurement_events(run, baseline)
    original = source.read_bytes()
    history = (run / "campaign.jsonl").read_bytes()
    source.unlink()

    _, fault, _ = await connected_run(
        tmp_path, monkeypatch, adapter, expected_fault=("persistence_failed", "measurement_journal")
    )

    assert fault.measurement == baseline
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    assert starts(run) == [start]
    assert len(adapter.probes) == 1
    assert not [
        row
        for mid, rows in measurement_starts(run).items()
        for row in rows
        if row["connection_admission"] is not None
    ]
    assert adapter.live == {}

    source.write_bytes(original)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert starts(run) == [start] and len(adapter.probes) == 1
    assert_one_terminal_per_start(run)
    assert adapter.live == {}
    assert_clean_run(run, snapshot)


# -- the merge with H8: a Grouper's owed suspension comes first -----------------


def _spy_gate_at(patch: pytest.MonkeyPatch, seam: str) -> list[tuple[bool, bool]]:
    """`(pending is None, _connection_may_probe())` at each firing of `seam`."""

    from evolution.tree.campaign import _Campaign

    original, seen = _Campaign.hook, []

    def hook(self, name):
        if name == seam:
            seen.append((self.pending is None, self._connection_may_probe()))
        original(self, name)

    patch.setattr(_Campaign, "hook", hook)
    return seen


def _assert_grouper_suspension_next(run: Path) -> list[dict[str, Any]]:
    """ADR-0034, F0 R50: the record after `grouping_failed(role_failed)` is its
    Grouper suspension, and no connection record follows the failure."""

    records = _records(run / "campaign.jsonl")
    (index,) = [
        at
        for at, record in enumerate(records)
        if record["event"] == "grouping_failed" and record["payload"]["code"] == "role_failed"
    ]
    failed, follower = records[index], records[index + 1]
    assert follower["event"] == "campaign_suspended"
    assert follower["payload"]["owner"] == "grouper"
    assert follower["payload"]["evidence"]["seq"] == failed["sequence"]
    assert not [r for r in records[index:] if r["event"] in CONNECTION_EVENTS]
    # The probe ran before the grouping: the gate was live in this run.
    assert "connection_preflight_finished" in [r["event"] for r in records[:index]]
    return records


async def test_no_preflight_starts_while_a_grouper_suspension_is_owed(tmp_path, monkeypatch):
    """At `after_grouping_failed` the role failure is durable and the live
    `stop` has not run yet (`pending` is still `None`): the gate already
    refuses a probe, from the owed Grouper cause alone."""

    from tests.test_tree_campaign_family14_grouper_run import _chain_client, _chain_run

    _chain_client(monkeypatch, "outage")
    seen = _spy_gate_at(monkeypatch, "after_grouping_failed")

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 1)

    assert seen == [(True, False)]
    _assert_grouper_suspension_next(run)
    assert snapshot.finished is None
    assert audit(run)["mismatches"] == []


async def test_a_resume_owing_a_grouper_suspension_starts_no_preflight(tmp_path, monkeypatch):
    """A cut between `grouping_failed(role_failed)` and its suspension: the
    resume writes exactly that suspension and runs no probe."""

    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family14_grouper_run import _chain_client, _chain_run

    _chain_client(monkeypatch, "outage")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_failed")
        with pytest.raises(Crash):
            await _chain_run(tmp_path, patch, 1)
    run = tmp_path / "run"
    before = _records(run / "campaign.jsonl")
    assert before[-1]["event"] == "grouping_failed"
    assert before[-1]["payload"]["code"] == "role_failed"
    probes: list[str] = []
    original = _Campaign._run_connection_probe

    async def probe(self, digest, fields):
        probes.append(digest)
        return await original(self, digest, fields)

    monkeypatch.setattr(_Campaign, "_run_connection_probe", probe)

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 1)

    after = _assert_grouper_suspension_next(run)
    assert after[: len(before)] == before
    assert [r["event"] for r in after[len(before) :]] == ["campaign_suspended"]
    assert probes == []
    assert snapshot.finished is None
    assert audit(run)["mismatches"] == []
