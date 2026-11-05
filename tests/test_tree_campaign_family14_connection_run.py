"""H10 Task 7: the family-14 connection gate in genuine campaigns (F0 R57-R61).

Every run here is a real `/15` campaign (`family14_run`): real creation,
lifecycle admission, S1 journals, subprocess Scorer and the probe runner. The
probe tests declare a real metric contract (`assistant_message` `/content`) and
use `ConnectedAdapter`, whose sessions emit that event; its probe instance (the
one materialized while the campaign's probe runs, `CONNECTION_PROBING`) can be
scripted to fail on its own. The Scorer recognises the probe's reply by its
marker, so a Scorer failure hits the probe only.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.interaction import Message, RolloutInfrastructureError, TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.target.connection_probe import binding_digest, epoch_binding, probe_seed
from tests.campaign_drivers import EDITED, OBSERVE, TWO_CASE_SCORER, _records
from tests.campaign_fakes import (
    CONNECTION_PROBING,
    FakeAdapter,
    FakeInstance,
    ScriptedDeadlineExceeded,
)
from tests.tree.manifest.test_manifest_v15 import METRIC_CONTRACT

pytestmark = pytest.mark.slow

PROBE_MARK = "probe-reply-h10"
CONNECTION_EVENTS = (
    "connection_preflight_started",
    "connection_preflight_finished",
    "connection_preflight_failed",
    "connection_preflight_interrupted",
)


# --- the fake Target and Scorer -----------------------------------------------------


class ConnectedInstance(FakeInstance):
    """A `FakeInstance` whose replies also carry the contract's `assistant_message`."""

    adapter: ConnectedAdapter
    probe = False

    async def send(self, session: Any, message: Any, request_id: str) -> TurnResult:
        adapter = self.adapter
        if self.probe:
            script = adapter.probe_send
            if isinstance(script, BaseException):
                raise script
            if script == "time_budget":
                raise ScriptedDeadlineExceeded("scripted rollout deadline")
            if callable(script):
                script()
            return TurnResult(
                events=tuple(dict(event) for event in adapter.probe_events),
                usage=Usage(prompt=1, completion=1, cached=0, total=2),
                latency_ms=1,
                outcome=adapter.probe_outcome,
            )
        result = await super().send(session, message, request_id)
        reply = {"type": "assistant_message", "content": "measured reply"}
        return replace(result, events=(*result.events, reply))


class ConnectedAdapter(FakeAdapter):
    """Scripts the probe's own instance; every other instance behaves as `FakeAdapter`'s."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.probe_neutral = False
        self.probe_events: tuple[dict[str, Any], ...] = (
            {"type": "assistant_message", "content": PROBE_MARK},
        )
        self.probe_outcome = "ok"
        self.probe_send: Any = None
        self.probe_materialize: BaseException | None = None
        self.probe_foreign_digest = False
        self.probe_refuse_release = False
        self.probes: list[str] = []

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
        if CONNECTION_PROBING.get():
            if self.probe_materialize is not None:
                raise self.probe_materialize
            self.probes.append(instance_id)
        instance = await super().materialize(candidate, strategy, instance_id)
        if CONNECTION_PROBING.get():
            # Kept out of the measurement counters a probe-neutral adapter keeps
            # it out of, so `materialized` names the measurements' instances.
            self.materialized.remove(instance_id)
        return instance

    async def release(self, instance_id: str) -> None:
        if instance_id in self.probes and self.probe_refuse_release:
            raise RuntimeError("target refuses to release the probe instance")
        await super().release(instance_id)

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        probing = CONNECTION_PROBING.get()
        instance = ConnectedInstance(
            instance_id,
            boot_id,
            self._semantic_for(candidate.dependency_digest),
            "f" * 64 if probing and self.probe_foreign_digest else candidate.digest,
            {} if probing else self.dispositions,
            self.capabilities.max_concurrent_sessions,
            self._description_for(instance_id),
            self._components_for(candidate.digest),
            self.used,
        )
        instance.adapter = self
        instance.probe = probing
        return instance


def probe_scorer(mode: str | None = None) -> str:
    """`TWO_CASE_SCORER`, except that the probe's reply gets `mode`'s output."""

    prelude = f"""
import json as _json
import sys as _sys
import time as _time

_payload = _json.loads(_sys.stdin.read())
_mode = {mode!r}
if _mode is not None and {PROBE_MARK!r} in _json.dumps(_payload["view"]):
    if _mode == "exit":
        _sys.exit(3)
    if _mode == "timeout":
        _time.sleep(30)
    _outputs = {{
        "invalid": "not json",
        "primary_missing": '{{"metrics":{{"other":1.0}},"critical_pass":true,"verdicts":[]}}',
        "primary_type": '{{"metrics":{{"score":"high"}},"critical_pass":true,"verdicts":[]}}',
        "primary_nonfinite": '{{"metrics":{{"score":1e999}},"critical_pass":true,"verdicts":[]}}',
    }}
    _sys.stdout.write(_outputs[_mode])
    _sys.exit(0)
import io as _io
_sys.stdin = _io.StringIO(_json.dumps(_payload))
"""
    return prelude + TWO_CASE_SCORER


# --- the driver -------------------------------------------------------------------------


async def connected_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: FakeAdapter | None = None,
    *,
    contract: dict[str, Any] | None = None,
    scorer_mode: str | None = None,
    scorer_timeout: float | None = None,
    max_failed_turns: int | None = None,
    config: dict[str, Any] = OBSERVE,
    **kwargs: Any,
) -> tuple[Path, Any, FakeAdapter]:
    """`family14_run` under a real metric contract (default `METRIC_CONTRACT`)."""

    from tests import campaign_fakes
    from tests import test_tree_campaign_family14_run as driver

    real_document = campaign_fakes.family14_document

    def document(base: Any, **options: Any) -> dict[str, Any]:
        moved = real_document(
            base, **options, metric_contract=METRIC_CONTRACT if contract is None else contract
        )
        if scorer_timeout is not None:
            moved["scorer"]["timeout_seconds"] = scorer_timeout
        if max_failed_turns is not None:
            moved["campaign"]["budget"]["max_failed_turns"] = max_failed_turns
        return moved

    adapter = adapter if adapter is not None else ConnectedAdapter(used=EDITED["used"])
    monkeypatch.setattr(driver, "family14_document", document)
    run, snapshot = await driver.family14_run(
        tmp_path,
        monkeypatch,
        config,
        adapter=adapter,
        scorer=probe_scorer(scorer_mode),
        **kwargs,
    )
    return run, snapshot, adapter


def campaign(run: Path) -> list[dict[str, Any]]:
    return _records(run / "campaign.jsonl")


def names(run: Path) -> list[str]:
    return [record["event"] for record in campaign(run)]


def payloads(run: Path, event: str) -> list[dict[str, Any]]:
    return [record["payload"] for record in campaign(run) if record["event"] == event]


def measurement_starts(run: Path) -> dict[str, list[dict[str, Any]]]:
    root = run / "measurements"
    if not root.exists():
        return {}
    return {
        path.name: [
            record["payload"]
            for record in _records(path / "events.jsonl")
            if record["event"] == "rollout_started"
        ]
        for path in sorted(root.iterdir())
        if (path / "events.jsonl").exists()
    }


def expected_binding(snapshot: Any, epoch: int = 0) -> str:
    return binding_digest(**epoch_binding(epoch, snapshot.epochs[epoch]))


# --- a valid connection ---------------------------------------------------------------


async def test_a_compatible_probe_admits_the_first_baseline_start(tmp_path, monkeypatch):
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    events = names(run)
    assert snapshot.finished["outcome"] == "completed"
    # Exactly one probe, before the baseline's first stage, for the whole run:
    # every later stage -- the Attempt's, the finals', round 2's -- is under the
    # same binding in the same invocation.
    assert [e for e in events if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    started_at = events.index("connection_preflight_started")
    assert events[started_at + 1] == "connection_preflight_finished"
    assert started_at + 1 < events.index("stage_opened")
    assert events.count("stage_opened") > 1
    (start,) = payloads(run, "connection_preflight_started")
    (finished,) = payloads(run, "connection_preflight_finished")
    digest = expected_binding(snapshot)
    champion = payloads(run, "campaign_created")[0]["identity"]["artifact_store"]
    assert start["binding_digest"] == digest
    assert (start["epoch"], start["case_id"], start["case_index"]) == (0, "c1", 0)
    assert start["candidate_digest"] == champion["seed_candidate_digest"]
    assert start["probe_seed"] == probe_seed(digest, start["candidate_digest"])
    assert (finished["status"], finished["cleanup"]) == ("compatible", "released")
    operation = start["operation_id"]
    # The probe journal is its own S1 journal, never a measurement.
    probe = _records(run / "connection" / operation / "events.jsonl")
    (probe_start,) = [r["payload"] for r in probe if r["event"] == "rollout_started"]
    assert probe_start["sample_id"] == "probe" and "connection_admission" not in probe_start
    assert any(r["event"] == "scoring_finished" for r in probe)
    assert (run / "connection-logs" / operation / "summary.json").is_file()
    # The admission: one start of the whole run carries it -- the baseline's
    # first -- naming the compatible preflight and this invocation.
    starts = measurement_starts(run)
    assert all(row["sample_id"] != "probe" for rows in starts.values() for row in rows)
    assert all(
        row["connection_binding_digest"] == digest for rows in starts.values() for row in rows
    )
    admitted = [
        (mid, index, row["connection_admission"])
        for mid, rows in starts.items()
        for index, row in enumerate(rows)
        if row["connection_admission"] is not None
    ]
    assert admitted == [
        (
            "m-r00-champion",
            0,
            {"operation_id": operation, "controller_invocation_id": start["invocation_id"]},
        )
    ]
    # The Target saw one probe instance, released before the first measurement
    # materialized, and nothing is left owned.
    assert len(adapter.probes) == 1 and set(adapter.probes) <= set(adapter.released)
    assert adapter.live == {}
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    assert_clean_run(run, snapshot)


async def test_the_probe_adds_no_row_repeat_or_slot(tmp_path, monkeypatch):
    """Baseline rows equal a probe-neutral control's row identities."""

    from tests.test_tree_campaign_family14_run import family14_run

    control, _ = await family14_run(
        tmp_path / "control", monkeypatch, adapter=FakeAdapter(used=EDITED["used"])
    )
    run, _, _ = await connected_run(tmp_path / "probed", monkeypatch)

    def rows(root: Path) -> dict[str, list[tuple[str, str, Any]]]:
        return {
            mid: sorted((r["case_id"], r["sample_id"], r.get("retry_ordinal")) for r in found)
            for mid, found in measurement_starts(root).items()
        }

    assert rows(run) == rows(control)
    stages = [(p["measurement_id"], p["sample_count"]) for p in payloads(run, "stage_opened")]
    assert stages == [
        (p["measurement_id"], p["sample_count"]) for p in payloads(control, "stage_opened")
    ]
    # The control's default contract declares no Target field; it probes too.
    assert names(control).count("connection_preflight_finished") == 1


# --- failed probes: the terminal and its suspension, and nothing dispatched -------------


def _script(adapter: ConnectedAdapter, case: str) -> dict[str, Any]:
    """Arrange `case`'s failure; return the driver keywords it needs."""

    if case == "event_missing":
        adapter.probe_events = ({"type": "message", "text": PROBE_MARK},)
    elif case == "field_empty":
        adapter.probe_events = ({"type": "assistant_message", "content": " "},)
    elif case == "field_type":
        adapter.probe_events = ({"type": "assistant_message", "content": 5},)
    elif case in ("primary_missing", "primary_type", "primary_nonfinite", "invalid"):
        return {"scorer_mode": case}
    elif case == "scorer_exit":
        return {"scorer_mode": "exit"}
    elif case == "scorer_timeout":
        return {"scorer_mode": "timeout", "scorer_timeout": 2}
    elif case == "materialize":
        adapter.probe_materialize = RolloutInfrastructureError("down", "target_unavailable")
    elif case == "attestation":
        adapter.probe_foreign_digest = True
    elif case == "send_infrastructure":
        # Not retried under the run's `call_retries` (a transient code would
        # exhaust them and close `target_failed`, the case below).
        adapter.probe_send = RolloutInfrastructureError("bad request", "invalid_request")
    elif case == "refused_release":
        adapter.probe_refuse_release = True
    elif case == "time_budget":
        adapter.probe_send = "time_budget"
    elif case == "candidate_failed":
        adapter.probe_outcome = "candidate_failed"
        return {"max_failed_turns": 1}
    elif case == "target_failed":
        adapter.probe_outcome = "target_failed"
    elif case != "turn_budget":
        raise AssertionError(case)
    return {}


FAILURES = [
    ("event_missing", "connection_incompatible", "connection_event_missing", None),
    ("field_empty", "connection_incompatible", "connection_field_empty", None),
    ("field_type", "connection_incompatible", "connection_field_type", None),
    ("primary_missing", "connection_incompatible", "connection_primary_missing", None),
    ("primary_type", "connection_incompatible", "connection_primary_type", None),
    ("primary_nonfinite", "connection_incompatible", "connection_primary_nonfinite", None),
    ("invalid", "connection_incompatible", "connection_scorer_output_invalid", "output_invalid"),
    # A custom adapter's own code is recorded as `infrastructure` (H10 review r1 item 5).
    ("materialize", "connection_preflight_unavailable", None, "infrastructure"),
    ("attestation", "connection_preflight_unavailable", None, "attestation_mismatch"),
    ("send_infrastructure", "connection_preflight_unavailable", None, "infrastructure_failed"),
    ("target_failed", "connection_preflight_unavailable", None, "target_failed"),
    ("scorer_exit", "connection_preflight_unavailable", None, "process_failed"),
    ("scorer_timeout", "connection_preflight_unavailable", None, "timeout"),
    ("refused_release", "connection_preflight_unavailable", None, "cleanup_failed"),
    ("time_budget", "connection_preflight_inconclusive", None, "time_budget"),
    ("candidate_failed", "connection_preflight_inconclusive", None, "candidate_failed"),
    ("turn_budget", "connection_preflight_inconclusive", None, "turn_budget"),
]


@pytest.mark.parametrize(
    ("case", "status", "detail", "cause"), FAILURES, ids=[f[0] for f in FAILURES]
)
async def test_a_failed_probe_suspends_with_nothing_between_and_nothing_dispatched(
    tmp_path, monkeypatch, case, status, detail, cause
):
    adapter = ConnectedAdapter(used=EDITED["used"])
    options = _script(adapter, case)
    if case == "turn_budget":
        _probe_environment(monkeypatch)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter, **options)
    events = names(run)
    records = campaign(run)
    failed_at = events.index("connection_preflight_failed")
    # R61: the failed terminal and its suspension are adjacent, and the
    # suspension is this invocation's last record.
    assert events[failed_at + 1 :] == ["campaign_suspended"]
    failed = records[failed_at]["payload"]
    suspension = records[failed_at + 1]["payload"]
    assert (failed["status"], failed["detail_code"], failed["cause_code"]) == (
        status,
        detail,
        cause,
    )
    assert suspension == {
        "reason": status,
        "owner": "connection",
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": snapshot.connection.operations[0]["terminal"]["seq"],
            "operation_id": records[failed_at]["operation_id"],
        },
    }
    assert snapshot.finished is None and "campaign_finished" not in events
    # Nothing was dispatched: no stage, no measurement start, and the Target
    # materialized the probe's instance only.
    assert "stage_opened" not in events
    assert not any(measurement_starts(run).values())
    assert adapter.materialized == []
    assert len(adapter.probes) == (0 if case == "materialize" else 1)
    if case == "refused_release":
        assert failed["cleanup"] == "failed"
        assert set(adapter.live) == set(adapter.probes)
    else:
        assert adapter.live == {}


class _Endless:
    """The run's Environment identity, wanting one more turn forever."""

    def __init__(self, identity: Any) -> None:
        self.identity = identity

    async def next(self, case: Any, history: Any) -> Message:
        return Message("again")


def _probe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's Environment (only) wants turns past the run's turn budget."""

    from evolution.tree import campaign as campaign_module

    real = campaign_module.run_probe

    async def probe(**kwargs: Any) -> Any:
        return await real(**{**kwargs, "environment": _Endless(kwargs["environment"].identity)})

    monkeypatch.setattr(campaign_module, "run_probe", probe)


# --- R61: back to back under the writer mutex -------------------------------------------


def _on_hook(monkeypatch: pytest.MonkeyPatch, seam: str, action: Any) -> list[Any]:
    """Call `action(campaign)` at each firing of campaign hook `seam`."""

    from evolution.tree.campaign import _Campaign

    original, seen = _Campaign.hook, []

    def hook(self: Any, name: str) -> None:
        original(self, name)
        if name == seam:
            seen.append(action(self))

    monkeypatch.setattr(_Campaign, "hook", hook)
    return seen


async def test_a_concurrent_writer_lands_after_the_suspension(tmp_path, monkeypatch):
    """A thread appending through `_Campaign.append` while the gate holds the
    mutex between the failed terminal and its suspension blocks there and lands
    after the suspension, where the lifecycle admits it."""

    threads: list[threading.Thread] = []
    blocked: list[bool] = []

    def contend(campaign: Any) -> None:
        if campaign.snapshot().connection.owes_suspension is None:
            return
        record = dict(
            owner="campaign_journal", measurement_id=None, code="storage_capacity_exhausted"
        )
        thread = threading.Thread(target=campaign.append, args=("capacity_exhausted", record))
        thread.start()
        thread.join(0.3)
        blocked.append(thread.is_alive())
        threads.append(thread)

    def settle(campaign: Any) -> None:
        # Right after the gate released the mutex: the writer gets in now.
        for thread in threads:
            thread.join(5)

    _on_hook(monkeypatch, "after_connection_preflight_terminal", contend)
    _on_hook(monkeypatch, "after_connection_suspended", settle)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()
    run, _, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert blocked == [True] and not any(thread.is_alive() for thread in threads)
    assert names(run)[-3:] == [
        "connection_preflight_failed",
        "campaign_suspended",
        "capacity_exhausted",
    ]


async def test_write_terminal_writes_nothing_after_the_gate_suspended(tmp_path, monkeypatch):
    """A stop another task decides right after the failed terminal, and an
    unresponsive editor, leave no further terminal; the barrier still releases."""

    from evolution.tree.campaign import _Pending

    def stop(campaign: Any) -> None:
        campaign.stop(_Pending("stopped", "cancelled", "none"))
        campaign.unresponsive = True

    _on_hook(monkeypatch, "after_connection_suspended", stop)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()
    run, _, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert names(run)[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    assert adapter.live == {}


def test_the_gate_holds_the_writer_mutex_across_both_appends():
    """Choice 13 by inspection: no `await` between the two appends."""

    import ast
    import inspect
    import textwrap

    from evolution.tree.campaign import _Campaign

    tree = ast.parse(textwrap.dedent(inspect.getsource(_Campaign._suspend_connection)))
    (function,) = tree.body
    assert isinstance(function, ast.FunctionDef)  # a plain `def`: it cannot await
    (block,) = [node for node in ast.walk(function) if isinstance(node, ast.With)]
    assert ast.unparse(block.items[0].context_expr) == "self.log.mutex"
    appended = [
        node.args[0]
        for node in ast.walk(block)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "self.append"
    ]
    assert [ast.unparse(arg) for arg in appended] == [
        "CONNECTION_FAILED_EVENT",
        "'campaign_suspended'",
    ]


# --- cancellation and stops while the probe runs ---------------------------------------


async def test_a_caller_cancellation_while_the_probe_runs_is_interrupted(tmp_path, monkeypatch):
    token: list[bool] = []
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = lambda: token.append(True)
    run, snapshot, _ = await connected_run(
        tmp_path, monkeypatch, adapter, cancelled=lambda: bool(token)
    )
    events = names(run)
    assert [e for e in events if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_interrupted",
    ]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("cancelled", "released")
    assert interrupted["trace_digest"] is not None and len(interrupted["evidence"]) == 1
    assert "campaign_suspended" not in events and "stage_opened" not in events
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert events[-1] == "campaign_finished"
    assert adapter.live == {}


async def test_a_stop_another_task_decides_while_the_probe_runs_is_interrupted(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Campaign, _Pending

    sessions: list[Any] = []
    real_gate = _Campaign.ensure_connection

    async def gate(self: Any, dispatch: Any) -> Any:
        sessions.append(self)
        return await real_gate(self, dispatch)

    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = lambda: sessions[0].stop(_Pending("stopped", "cancelled", "none"))
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("interrupted", "released")
    events = names(run)
    assert "campaign_suspended" not in events and "stage_opened" not in events
    # That task's terminal is the one written.
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert adapter.live == {}


async def test_an_asyncio_cancellation_of_the_probe_closes_its_start(tmp_path, monkeypatch):
    """A cancelled task (not the cooperative token) releases the leftover first,
    writes `interrupted{reason: cancelled}` and re-raises the cancellation."""

    from evolution.tree.campaign import _Campaign

    adapter = ConnectedAdapter(used=EDITED["used"])

    def cancel() -> None:
        raise asyncio.CancelledError

    adapter.probe_send = cancel
    seen: list[str] = []
    real = _Campaign._run_connection_probe

    async def probe(self: Any, *args: Any) -> Any:
        try:
            return await real(self, *args)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    monkeypatch.setattr(_Campaign, "_run_connection_probe", probe)
    with pytest.raises(asyncio.CancelledError):
        await connected_run(tmp_path, monkeypatch, adapter)
    run = tmp_path / "run"
    assert seen == ["cancelled"]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == ("cancelled", "released")
    assert "campaign_suspended" not in names(run)
    assert adapter.live == {}


async def test_no_probe_starts_once_the_campaign_is_stopping(tmp_path, monkeypatch):
    """Ruling 18: a cancellation before the first dispatch starts no probe and
    dispatches nothing; the run ends `stopped(cancelled)`."""

    from evolution.tree.campaign import _Campaign

    token: list[bool] = []
    real = _Campaign.ensure_connection

    async def gate(self: Any, dispatch: Any) -> Any:
        token.append(True)
        return await real(self, dispatch)

    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    run, snapshot, adapter = await connected_run(
        tmp_path, monkeypatch, cancelled=lambda: bool(token)
    )
    events = names(run)
    assert not [e for e in events if e in CONNECTION_EVENTS]
    assert "stage_opened" not in events and not (run / "connection").exists()
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert adapter.probes == [] and adapter.materialized == []


# --- families up to 13 --------------------------------------------------------------

#: Each measurement's `rollout_started` payloads, without the random operation
#: id, of a genuine two-case run (`_two_case_run(used=EDITED["used"])`, family
#: 12; with `as_family_13={"gate": {"mode": "observe"}, "repeats": 1}`, family
#: 13), recorded on the immutable base `162395be1` from its pristine export
#: (`.superpowers/h10-t7/base_rollout_started.py`, controller ruling 3), as
#: `{measurement: (candidate digest, sorted (case, sample, retry ordinal))}`.
#: Every payload's key set is `BASE_START_KEYS`. Never regenerate it.
_SEED = "66df64e998f8602c4c129c2901192a3331322f55bf4ddc65ac758a38141ce7a0"
_EDIT = "77de7ef08042ebc0864d7c56f4a6e74d9a84d4cd260f85cd517b9a9f1112a35c"
_THREE = [(case, f"0{n}", 0) for case in ("c1", "c2") for n in (1, 2, 3)]
_ONE = [("c1", "01", 0), ("c2", "01", 0)]
BASE_START_KEYS = {"candidate_digest", "case_id", "retry_ordinal", "sample_id"}


# --- the admission token at the measurement ---------------------------------------------


async def _marked_measurement(tmp_path: Path) -> tuple[Any, Any]:
    from evolution.tree.campaign_measurement import Measurement
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.campaign_fakes import policy
    from tests.test_tree_campaign_measurement import CAMPAIGN_RUN_ID, MEASUREMENT_ID, build_rig

    rig = await build_rig(
        tmp_path,
        campaign_policy=policy(max_concurrency=4),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
    )
    payload = rig.measurement().identity.to_payload()
    measurement = Measurement.declare(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        measurement_id=MEASUREMENT_ID,
        identity=RunIdentityV1.from_payload({**payload, "connection_binding": 1}),
        corpus=rig.cases,
    )
    measurement.create()
    return rig, measurement


def _starts(rig: Any) -> list[dict[str, Any]]:
    path = rig.run_root / "measurements" / "m-r00-champion" / "events.jsonl"
    return [r["payload"] for r in _records(path) if r["event"] == "rollout_started"]


async def test_concurrent_tries_under_one_token_write_one_admission(tmp_path):
    """Ruling 13: the rows of one stage run their tries concurrently
    (`_run_row` -> `_run_try`); exactly one start claims the admission."""

    from evolution.tree.execution.journal_rollout import ConnectionAdmission

    rig, measurement = await _marked_measurement(tmp_path)
    token = ConnectionAdmission("d" * 64, "preflight-1", "invocation-1")
    try:
        plan = rig.plans()[0]
        await measurement.run_stage(plan, connection=token, **rig.stage_kwargs())
    finally:
        measurement.close()
    starts = _starts(rig)
    assert len(starts) == len(plan.rows) > 1
    assert all(row["connection_binding_digest"] == "d" * 64 for row in starts)
    admitted = [row["connection_admission"] for row in starts if row["connection_admission"]]
    assert admitted == [{"operation_id": "preflight-1", "controller_invocation_id": "invocation-1"}]
    assert starts[0]["connection_admission"] is not None
    assert rig.adapter.peak_live > 1  # the tries really ran side by side


async def test_a_spent_token_and_refused_rows_name_the_binding_without_admission(tmp_path):
    from evolution.tree.execution.journal_rollout import ConnectionAdmission

    rig, measurement = await _marked_measurement(tmp_path)
    fresh = ConnectionAdmission("e" * 64, "preflight-1", "invocation-1")
    try:
        plan = rig.plans()[0]
        refused, dispatched = replace(plan, rows=plan.rows[:2]), replace(plan, rows=plan.rows[2:])
        measurement.refuse_rows(refused, connection=fresh)
        await measurement.run_stage(
            dispatched, connection=ConnectionAdmission.spent("e" * 64), **rig.stage_kwargs()
        )
    finally:
        measurement.close()
    starts = _starts(rig)
    assert len(starts) == len(plan.rows)
    assert {row["connection_binding_digest"] for row in starts} == {"e" * 64}
    assert all(row["connection_admission"] is None for row in starts)
    # `refuse_rows` never claimed the fresh token's admission.
    assert fresh.claim() is not None


def test_the_admission_token_is_read_only_and_a_spent_one_never_admits():
    from evolution.tree.execution.journal_rollout import ConnectionAdmission

    token = ConnectionAdmission("a" * 64, "op-1", "inv-1")
    for name in ("binding_digest", "operation_id", "invocation_id"):
        with pytest.raises(AttributeError):
            setattr(token, name, "b" * 64)
    spent = ConnectionAdmission.spent("a" * 64)
    assert (spent.binding_digest, spent.operation_id, spent.invocation_id) == ("a" * 64, None, None)
    assert spent.claim() is None
    spent._restore()
    assert spent.claim() is None
    for bad in ("A" * 64, "a" * 63, 7, None):
        with pytest.raises(ValueError, match="invalid_connection_admission"):
            ConnectionAdmission.spent(bad)  # type: ignore[arg-type]


def test_the_admission_uses_the_journal_schema_digest_pattern():
    from evolution.tree.execution import journal_rollout, journal_schema

    assert journal_rollout.SHA256 is journal_schema.SHA256 is journal_schema._SHA256


# --- gate semantics ---------------------------------------------------------------------


async def test_the_binding_is_recomputed_at_every_dispatch_and_probed_once(tmp_path, monkeypatch):
    """`D` is derived again at every dispatching stage; a later stage under the
    same `D` in the same invocation does not probe."""

    from evolution.tree.campaign import _Campaign

    derived: list[str] = []
    gates: list[bool] = []
    real_binding, real_gate = _Campaign.connection_binding, _Campaign.ensure_connection

    def binding(self: Any) -> Any:
        found = real_binding(self)
        derived.append(found[0])
        return found

    async def gate(self: Any, dispatch: Any) -> Any:
        gates.append(dispatch())
        return await real_gate(self, dispatch)

    monkeypatch.setattr(_Campaign, "connection_binding", binding)
    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    assert len(derived) == sum(gates) >= 3
    assert set(derived) == {expected_binding(snapshot)}
    assert names(run).count("connection_preflight_started") == 1


async def test_concurrent_dispatches_share_one_probe(tmp_path, monkeypatch):
    """Single flight: two stages reaching the gate together run one probe and
    get the same admission token."""

    from evolution.tree.campaign import _Campaign

    real_gate = _Campaign.ensure_connection
    shared: list[tuple[Any, Any]] = []

    async def gate(self: Any, dispatch: Any) -> Any:
        if shared:
            return await real_gate(self, dispatch)
        first, second = await asyncio.gather(real_gate(self, dispatch), real_gate(self, dispatch))
        shared.append((first, second))
        return first

    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch)
    ((first, second),) = shared
    assert first is second and first.operation_id is not None
    assert names(run).count("connection_preflight_started") == 1
    assert len(adapter.probes) == 1
    assert snapshot.finished["outcome"] == "completed"


async def test_a_waiter_after_a_faulted_probe_starts_no_second_preflight(tmp_path, monkeypatch):
    """Single flight after a probe storage fault (review round 1, item 1).

    The first caller's probe faults writing its trace: a classified
    `persistence_failed` fault, its start left open for recovery (ADR-0008).
    The waiter takes the gate lock before any `reap` can mark the campaign
    stopping. It starts no second preflight (the lifecycle would refuse it as
    an invariant violation), dispatches nothing and stops the invocation, so
    the classified fault is the one the campaign surfaces. The resume settles
    the open start and probes again. `second is _REFUSED` is the assertion that
    discriminates the fix; `fault is first` also passes before it."""

    from evolution.tree.campaign import _REFUSED, _Campaign
    from evolution.tree.execution.journal import JournalFault
    from evolution.tree.target import connection_probe as probe_module

    real_gate = _Campaign.ensure_connection
    seen: list[tuple[Any, Any, bool]] = []

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise JournalFault("journal_unavailable")

    async def gate(self: Any, dispatch: Any) -> Any:
        if seen:
            return await real_gate(self, dispatch)
        first, second = await asyncio.gather(
            real_gate(self, dispatch), real_gate(self, dispatch), return_exceptions=True
        )
        # Read before this task ends: no `reap` has run yet.
        seen.append((first, second, self.stopping))
        for found in (first, second):
            if isinstance(found, BaseException):
                raise found
        return first

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "ensure_connection", gate)
        patch.setattr(probe_module, "_write_trace", broken)
        run, fault, _ = await connected_run(
            tmp_path,
            patch,
            adapter,
            expected_fault=("persistence_failed", "measurement_journal"),
        )
    ((first, second, stopping),) = seen
    assert (first.terminal, first.owner, first.code) == (
        "persistence_failed",
        "measurement_journal",
        "journal_unavailable",
    )
    assert fault is first
    assert second is _REFUSED and stopping
    events = names(run)
    assert [e for e in events if e in CONNECTION_EVENTS] == ["connection_preflight_started"]
    assert "stage_opened" not in events and "campaign_finished" not in events
    assert not any(measurement_starts(run).values())
    assert len(adapter.probes) == 1 and adapter.live == {}

    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, adapter)
    assert snapshot.finished["outcome"] == "completed"
    starts = [r for r in campaign(run) if r["event"] == "connection_preflight_started"]
    assert len(starts) == 2 and len(adapter.probes) == 2
    for start in starts:
        terminals = [
            r["event"]
            for r in campaign(run)
            if r["event"] in CONNECTION_EVENTS[1:] and r["operation_id"] == start["operation_id"]
        ]
        assert len(terminals) == 1, start["operation_id"]
    assert adapter.live == {}
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    assert_clean_run(run, snapshot)


async def test_a_resume_after_the_boundary_does_not_probe(tmp_path, monkeypatch):
    """C§4.1: the boundary is durable, so the resumed invocation dispatches under
    a binding-only token: its starts name `D` with a null admission."""

    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash

    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_reflection_finished")
        with pytest.raises(Crash):
            await connected_run(tmp_path, patch)
    before = {mid: len(rows) for mid, rows in measurement_starts(tmp_path / "run").items()}
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] == "completed"
    assert names(run).count("connection_preflight_started") == 1 and adapter.probes == []
    starts = measurement_starts(run)
    resumed = [row for mid, rows in starts.items() for row in rows[before.get(mid, 0) :]]
    assert resumed and all(row["connection_admission"] is None for row in resumed)
    assert {row["connection_binding_digest"] for row in resumed} == {expected_binding(snapshot)}
    admitted = [row for rows in starts.values() for row in rows if row["connection_admission"]]
    assert len(admitted) == 1
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    assert_clean_run(run, snapshot)


async def test_an_inspection_waits_while_a_probe_holds_the_gate(tmp_path, monkeypatch):
    """Choice 19: a component inspection boots a Target instance, so it waits on
    the gate's lock while a probe holds it. (A real overlap needs an Attempt
    reaching dispatch first, which only an inherited baseline gives; family 14
    refuses `linked_from` until H12, so the lock is held here directly.)"""

    import time

    from evolution.tree import campaign as campaign_module
    from tests.campaign_drivers import MAP_FILE

    released: list[float] = []
    inspected: list[float] = []

    def hold(campaign: Any) -> None:
        async def holder() -> None:
            async with campaign.connection_lock:
                await asyncio.sleep(1.0)
                released.append(time.monotonic())

        asyncio.get_running_loop().create_task(holder())

    real_inspect = campaign_module.inspect_candidate

    async def inspect(**kwargs: Any) -> Any:
        inspected.append(time.monotonic())
        return await real_inspect(**kwargs)

    monkeypatch.setattr(campaign_module, "inspect_candidate", inspect)
    _on_hook(monkeypatch, "after_editor_started", hold)
    run, _, _ = await connected_run(
        tmp_path, monkeypatch, ConnectedAdapter(components=MAP_FILE, used=EDITED["used"])
    )
    assert "candidate_inspected" in names(run)
    assert len(released) == len(inspected) == 1 and inspected[0] >= released[0]


# --- Stops in the probe's un-polled windows --------------------------------------------


def _stop_during_scoring(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    """Call `action()` when the probe starts scoring: after the rollout's last
    poll of its cooperative stop, so only the gate's post-probe check sees it."""

    from evolution.tree.target import connection_probe

    real = connection_probe.durable_score

    async def score(*args: Any, **kwargs: Any) -> Any:
        action()
        return await real(*args, **kwargs)

    monkeypatch.setattr(connection_probe, "durable_score", score)


def _capture_campaign(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from evolution.tree.campaign import _Campaign

    sessions: list[Any] = []
    real_gate = _Campaign.ensure_connection

    async def gate(self: Any, dispatch: Any) -> Any:
        sessions.append(self)
        return await real_gate(self, dispatch)

    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    return sessions


def _assert_stopped_without_suspension(run: Path, snapshot: Any, reason: str, cleanup: str) -> None:
    events = names(run)
    assert [e for e in events if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_interrupted",
    ]
    (interrupted,) = payloads(run, "connection_preflight_interrupted")
    assert (interrupted["reason"], interrupted["cleanup"]) == (reason, cleanup)
    assert "campaign_suspended" not in events and "stage_opened" not in events
    assert not any(measurement_starts(run).values())
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert events[-1] == "campaign_finished"


async def test_a_caller_cancel_during_a_failing_materialize_is_interrupted(tmp_path, monkeypatch):
    """The token set inside the probe's materialize, which then fails: no poll
    saw it, yet the operator's cancel is never a connection suspension."""

    token: list[bool] = []

    class Cancelling(ConnectedAdapter):
        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            if CONNECTION_PROBING.get():
                token.append(True)
            return await super().materialize(candidate, strategy, instance_id)

    adapter = Cancelling(used=EDITED["used"])
    adapter.probe_materialize = RolloutInfrastructureError("down", "target_unavailable")
    run, snapshot, _ = await connected_run(
        tmp_path, monkeypatch, adapter, cancelled=lambda: bool(token)
    )
    # A materialize was started (and failed), so the cleanup rule says
    # `released`; `not_needed` is only a probe that never started one.
    _assert_stopped_without_suspension(run, snapshot, "cancelled", "released")
    assert adapter.live == {}


async def test_a_caller_cancel_during_a_failing_score_is_interrupted(tmp_path, monkeypatch):
    token: list[bool] = []
    _stop_during_scoring(monkeypatch, lambda: token.append(True))
    run, snapshot, adapter = await connected_run(
        tmp_path, monkeypatch, scorer_mode="exit", cancelled=lambda: bool(token)
    )
    _assert_stopped_without_suspension(run, snapshot, "cancelled", "released")
    assert adapter.live == {}


async def test_another_tasks_stop_during_a_failing_score_keeps_its_terminal(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Pending

    sessions = _capture_campaign(monkeypatch)
    _stop_during_scoring(
        monkeypatch, lambda: sessions[0].stop(_Pending("stopped", "cancelled", "none"))
    )
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch, scorer_mode="exit")
    # That task's terminal is written; the connection outcome is `interrupted`.
    _assert_stopped_without_suspension(run, snapshot, "interrupted", "released")
    assert adapter.live == {}


async def test_a_compatible_probe_after_a_stop_finishes_but_dispatches_nothing(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Pending

    sessions = _capture_campaign(monkeypatch)
    _stop_during_scoring(
        monkeypatch, lambda: sessions[0].stop(_Pending("stopped", "cancelled", "none"))
    )
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch)
    events = names(run)
    assert [e for e in events if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    assert "stage_opened" not in events and not any(measurement_starts(run).values())
    assert adapter.materialized == [] and adapter.live == {}
    assert sessions[0].connection_tokens == {}
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")


async def test_a_cleanup_fault_after_a_task_cancellation_keeps_the_cancellation(
    tmp_path, monkeypatch
):
    """Ruling (minor 2): when closing the cancelled probe's start fails, the
    `CancelledError` still propagates, with the cleanup fault as its cause."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.storage.journal_errors import JournalFault

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise JournalFault()

    monkeypatch.setattr(campaign_module, "release_probe_leftover", refuse)
    adapter = ConnectedAdapter(used=EDITED["used"])

    def cancel() -> None:
        raise asyncio.CancelledError

    adapter.probe_send = cancel
    with pytest.raises(asyncio.CancelledError) as raised:
        await connected_run(tmp_path, monkeypatch, adapter)
    assert isinstance(raised.value.__cause__, campaign_module._Fault)
    # The start stays open for recovery (Task 8).
    events = names(tmp_path / "run")
    assert [e for e in events if e in CONNECTION_EVENTS] == ["connection_preflight_started"]


# --- What dispatches --------------------------------------------------------------------


def _stub(stages: int = 0, remaining: tuple[dict[str, Any], ...] = ()) -> Any:
    from types import SimpleNamespace

    from evolution.tree.campaign_measurement import StagePlan

    rows = (
        {"case_id": "c1", "sample_id": "01", "repeat": 1},
        {"case_id": "c2", "sample_id": "01", "repeat": 1},
    )
    carried = [
        SimpleNamespace(case_id=row["case_id"], sample_id=row["sample_id"], imported=True)
        for row in rows
    ]
    measurement = SimpleNamespace(sample_rows=lambda horizon: carried)
    plan = StagePlan(1, 1, rows)
    stub = SimpleNamespace(
        measurements={"m": measurement},
        snapshot=lambda: SimpleNamespace(measurements={"m": {"stages": [None] * stages}}),
        _stage_dispatch_rows=lambda mid, plan: (
            replace(plan, rows=remaining),
            frozenset(),
            frozenset(),
        ),
        family_version=14,
    )
    return stub, plan


def test_a_fully_carried_unopened_stage_dispatches_nothing_and_never_probes():
    from evolution.tree.campaign import _Campaign

    stub, plan = _stub()
    assert _Campaign._stage_will_dispatch(stub, "m", plan) is False
    # `ensure_connection` returns before deriving `D`: the stub has no
    # `connection_binding`, so a probe attempt would raise.
    assert asyncio.run(_Campaign.ensure_connection(stub, lambda: False)) is None
    # A repair owed from another stage still dispatches.
    owed, plan = _stub(remaining=({"case_id": "c1", "sample_id": "02", "repeat": 2},))
    assert _Campaign._stage_will_dispatch(owed, "m", plan) is True


def test_a_joint_stage_dispatching_on_one_side_gates_once():
    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign

    asked: list[str] = []
    stub = SimpleNamespace(
        plans_for=lambda mid: ("plan",),
        _stage_will_dispatch=lambda mid, plan: asked.append(mid) or mid == "parent",
    )
    assert _Campaign._joint_will_dispatch(stub, ("candidate", "parent"), 1) is True
    assert asked == ["candidate", "parent"]
    stub.plans_for = lambda mid: ()
    assert _Campaign._joint_will_dispatch(stub, ("candidate",), 1) is False


def test_a_storage_fault_reading_a_joint_side_propagates():
    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign, _Fault
    from evolution.tree.storage.journal_errors import JournalFault

    def fault(mid: str, plan: Any) -> bool:
        raise JournalFault()

    stub = SimpleNamespace(plans_for=lambda mid: ("plan",), _stage_will_dispatch=fault)
    with pytest.raises(_Fault):
        _Campaign._joint_will_dispatch(stub, ("candidate",), 1)


async def test_each_joint_stage_calls_the_gate_once(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign

    calls: list[str] = []
    real_joint, real_gate = _Campaign.run_joint_stage, _Campaign.ensure_connection
    inside: list[int] = []

    async def joint(self: Any, *args: Any, **kwargs: Any) -> Any:
        inside.append(len(calls))
        result = await real_joint(self, *args, **kwargs)
        inside.append(len(calls))
        return result

    async def gate(self: Any, dispatch: Any) -> Any:
        calls.append("gate")
        return await real_gate(self, dispatch)

    monkeypatch.setattr(_Campaign, "run_joint_stage", joint)
    monkeypatch.setattr(_Campaign, "ensure_connection", gate)
    await connected_run(tmp_path, monkeypatch)
    pairs = list(zip(inside[::2], inside[1::2], strict=True))
    assert pairs and all(after - before == 1 for before, after in pairs)


# --- #287: a run secret in the instance description -------------------------------------

#: A model id the Scorer forwards as its judge model -- a run secret by the
#: length rule -- that the backend's `describe()` also reports.
JUDGE = "deepseek/judge-model-x"
JUDGE_ENV = "B2E_LAB_JUDGE_MODEL"
JUDGE_PATH = f"scorer.allowed_env.{JUDGE_ENV}"
#: What a repaired `describe()` reports instead: a digest of the model id.
REPAIRED = {"model": "sha256:" + hashlib.sha256(JUDGE.encode()).hexdigest()}
DESCRIPTION_SENTENCE = (
    "evo tree evolve: suspended: connection_incompatible — the target's instance "
    f"description holds the value of {JUDGE_PATH}; remove it from describe() (for example "
    "report a hash) and run the same command again"
)


def forward_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `/15` and `/16` document the drivers write forwards `JUDGE_ENV`.

    Patched where both drivers resolve `family14_document` at call time, so
    call it before `_connected` or `connected_run` captures the function.
    """

    from tests import campaign_fakes

    real = campaign_fakes.family14_document

    def document(base: Any, **options: Any) -> dict[str, Any]:
        moved = real(base, **options)
        moved["scorer"]["allowed_env"] = sorted(
            {*moved["scorer"].get("allowed_env", ()), JUDGE_ENV}
        )
        return moved

    monkeypatch.setattr(campaign_fakes, "family14_document", document)
    monkeypatch.setenv(JUDGE_ENV, JUDGE)


def assert_nowhere(run: Path, value: str) -> None:
    for path in run.rglob("*"):
        if path.is_file():
            assert value.encode() not in path.read_bytes(), path


async def description_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: Any
) -> tuple[Path, Any, ConnectedAdapter]:
    """A public `/15` run (`family14_public_run`) whose instances describe
    themselves as `description`, under the judge-forwarding Scorer."""

    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_inspection import _connected

    adapter = ConnectedAdapter(used=EDITED["used"], description=description)
    with monkeypatch.context() as patch:
        forward_judge(patch)
        _connected(patch)
        run, result = await family14_public_run(
            tmp_path, patch, adapter=adapter, scorer=probe_scorer(None)
        )
    return run, result, adapter


def assert_description_suspension(run: Path, result: Any, adapter: ConnectedAdapter) -> None:
    """The probe refused the description: one failed preflight and its
    suspension, nothing dispatched, the instance released, the value nowhere."""

    assert (result.terminal, result.owner, result.code) == (
        "suspended",
        "connection",
        "connection_incompatible",
    )
    events = names(run)
    assert events[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    (failed,) = payloads(run, "connection_preflight_failed")
    assert (
        failed["status"],
        failed["detail_code"],
        failed["manifest_path"],
        failed["observed_path"],
        failed["cause_code"],
        failed["cleanup"],
    ) == (
        "connection_incompatible",
        "connection_description_secret",
        JUDGE_PATH,
        "target.description",
        None,
        "released",
    )
    assert payloads(run, "campaign_suspended")[-1]["reason"] == "connection_incompatible"
    # No baseline was paid for: no stage, no measurement start, no attestation.
    assert "stage_opened" not in events and "instance_attested" not in events
    assert not any(measurement_starts(run).values())
    assert adapter.materialized == [] and len(adapter.probes) == 1
    assert adapter.live == {}
    (operation,) = [p["operation_id"] for p in payloads(run, "connection_preflight_started")]
    probe = [r["event"] for r in _records(run / "connection" / operation / "events.jsonl")]
    assert "rollout_started" not in probe and "target_release_finished" in probe
    summary = _json_file(run / "connection-logs" / operation / "summary.json")
    assert summary["description_location"] == "description.model"
    connection = result.status.to_payload()["connection"]
    assert (connection["state"], connection["outcome"]) == ("suspended", "connection_incompatible")
    assert connection["detail"] == {
        "code": "connection_description_secret",
        "metric": "score",
        "manifest_path": JUDGE_PATH,
        "observed_path": "target.description",
    }
    assert_nowhere(run, JUDGE)


def _json_file(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def test_a_description_secret_suspends_the_campaign_before_the_baseline(tmp_path, monkeypatch):
    from tests.test_tree_cli_family14 import _invoke, _stderr

    # Synchronous: the CLI runner below starts its own event loop.
    run, result, adapter = asyncio.run(description_run(tmp_path, monkeypatch, {"model": JUDGE}))
    assert_description_suspension(run, result, adapter)
    invocation = _invoke(tmp_path / "cli", monkeypatch, result)
    assert invocation.exit_code == 1, invocation.output
    assert _stderr(invocation)[0] == DESCRIPTION_SENTENCE
    assert JUDGE not in invocation.stdout and JUDGE not in invocation.stderr


async def test_resume_after_repairing_describe_finishes(tmp_path, monkeypatch):
    run, result, adapter = await description_run(tmp_path, monkeypatch, {"model": JUDGE})
    assert_description_suspension(run, result, adapter)
    run, result, adapter = await description_run(tmp_path, monkeypatch, REPAIRED)
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert [e for e in names(run) if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_failed",
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    assert payloads(run, "instance_attested")
    assert all(p["description"] == REPAIRED for p in payloads(run, "instance_attested"))
    assert_nowhere(run, JUDGE)


def _normalized(run: Path) -> list[Any]:
    """Every record of the campaign and probe journals and the probe trace,
    with what differs between two identical runs replaced by its first-seen
    ordinal: times, ids, digests and the run directory. Byte ranges are
    dropped, since a time's or a latency's length moves every later offset."""

    import json
    import re

    volatile = re.compile(r"[0-9a-f]{32,64}|c-[0-9a-f]+|\d{4}-\d\d-\d\dT[0-9:.+Z-]+")
    seen: dict[str, str] = {}

    def text(path: Path) -> str:
        raw = path.read_text(encoding="utf-8").replace(str(run), "<run>")
        raw = re.sub(r"#bytes=\d+:\d+", "#bytes", raw)
        return volatile.sub(lambda m: seen.setdefault(m.group(0), f"<{len(seen)}>"), raw)

    documents: list[Any] = []
    for record in json.loads("[" + ",".join(text(run / "campaign.jsonl").splitlines()) + "]"):
        record.pop("timestamp", None)
        documents.append(record)
    for operation in sorted((run / "connection").iterdir()):
        for line in text(operation / "events.jsonl").splitlines():
            record = json.loads(line)
            record.pop("timestamp", None)
            record.get("payload", {}).pop("latency_ms", None)
            documents.append(record)
    for trace in sorted((run / "connection-logs").glob("*/summary.json")):
        documents.append(json.loads(text(trace)))
    return documents


async def test_a_campaign_without_description_secrets_keeps_its_bytes(tmp_path, monkeypatch):
    """A description that holds no run secret changes nothing: the same
    records, and the trace keeps its key set, as with the check switched off."""

    from evolution.tree.target import connection_probe

    run, result, _ = await description_run(tmp_path / "checked", monkeypatch, REPAIRED)
    assert result.terminal == "finished"
    with monkeypatch.context() as patch:
        patch.setattr(connection_probe, "description_secret", lambda *args: None)
        control, unchecked, _ = await description_run(tmp_path / "control", patch, REPAIRED)
    assert unchecked.terminal == "finished"
    events = names(run)
    assert "connection_preflight_finished" in events and "instance_attested" in events
    assert _normalized(run) == _normalized(control)
    for trace in (run / "connection-logs").glob("*/summary.json"):
        assert "description_location" not in _json_file(trace)
