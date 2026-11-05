"""H10 Task 9: the audit of the family-14 connection records (F0 R60, C§6.1).

ADR-0029 (amended 2026-10-09) and F0 R60 fix what the audit proves.

Per record:

- a `connection_preflight_started`'s binding digest re-derives from its fields,
  and those fields are its condition epoch's (the first train case included);
- each terminal's cited probe-journal prefix exists and hashes to its digest
  (the generic reference check);
- the terminal's `cleanup`, `status` and `cause_code` agree with the probe
  journal's S1 records;
- a connection suspension names the failed terminal right before it, with
  that terminal's status as its reason.

Over the whole journal, for each connection binding `D`:

- exactly one locally executed `rollout_started` carries an admission;
- in its own measurement journal, no earlier start under `D` executed a
  session (the synthetic starts `refuse_rows` writes are exempt);
- the admission names a compatible preflight of `D` written by the same
  controller invocation;
- that preflight is the last preflight of `D` in campaign-journal order;
- every start names its measurement's epoch binding;
- a binding whose starts executed with no admission is a finding.

What the audit cannot prove (F0 R60, F0 §15): S1 journals of different
measurements share no order with each other or with the campaign journal. So
"the first start under `D`" is checked within the admitted start's own journal
only, and "no later failed or unresolved preflight" in campaign-journal order
only: a start in a measurement journal cannot be placed before or after a
campaign record written after the compatible terminal. The audit never
re-scores the probe. It reads the probe journal's record kinds, rollout outcome,
cleanup and the primary metric's name, never a reply, an event value or a score.

Earlier failed or interrupted preflights of `D` are admitted: a suspension
and a resume that probes again is ADR-0029's recovery path. A later
compatible-but-stopped `connection_preflight_finished` of `D` (controller
ruling 14/19/21) cannot follow an admission. Once a start carries the
admission, the boundary is durable. The gate returns at once for `D` in this
invocation and in every later one, so it never probes `D` again. A
compatible-but-stopped probe grants no admission, so it can only precede the
admitted preflight. A forged one after the admitted preflight is a finding.

The genuine runs come from Task 7's fixtures (`ConnectedAdapter`,
`probe_neutral = False`). Forgeries copy them and then edit bytes. A campaign
edit is re-sealed by `forge`. A measurement-journal edit also re-seals every
campaign reference into that journal (`forge_measurement`). So the only
findings are the connection audit's own, or the lifecycle's refusal where the
lifecycle already owns the fact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign_projections import (
    _connection_findings,
    _Evidence,
    _precondition_finding,
    _verify_reference,
)
from evolution.tree.campaign_schema import campaign_family_for
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.storage.envelope import Envelope
from evolution.tree.target.connection_probe import binding_digest, probe_seed
from tests import test_tree_campaign_family14_connection_run as t7
from tests.campaign_drivers import EDITED
from tests.test_tree_campaign_family14_audit import audit, forge
from tests.test_tree_campaign_family14_connection_run import (
    FAILURES,
    ConnectedAdapter,
    _script,
    campaign,
    connected_run,
    expected_binding,
    measurement_starts,
    names,
    payloads,
)
from tests.test_tree_campaign_family14_gate_run import assert_clean_run

pytestmark = pytest.mark.slow

FINDING = "precondition_failed"
UNREADABLE = "a measurement journal under the binding cannot be read"


# --- forging helpers ----------------------------------------------------------------


def _offsets(lines: list[bytes]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def forge_measurement(run: Path, measurement_id: str, edit: Any) -> None:
    """Edit one measurement journal's rows, then re-seal every campaign reference into it.

    `edit(rows)` mutates the parsed S1 rows in place. Each row is re-encoded,
    and every campaign `locator` into the journal is re-pointed at the same
    record boundaries and re-digested. `forge` then re-seals the campaign
    log's own locators. Only the forged fact differs from the genuine run.
    """

    name = f"measurements/{measurement_id}/events.jsonl"
    path = run / name
    lines = path.read_bytes().splitlines(keepends=True)
    old = _offsets(lines)
    rows = [json.loads(line) for line in lines]
    edit(rows)
    encoded = [Envelope.from_payload(row, family=S1_FAMILY).encode() for row in rows]
    new = _offsets(encoded)
    blob = b"".join(encoded)
    path.write_bytes(blob)

    def moved(locator: str) -> tuple[str, int, int]:
        start, end = (int(part) for part in locator.rsplit("=", 1)[1].split(":"))
        start, end = new[old.index(start)], new[old.index(end)]
        return f"{name}#bytes={start}:{end}", start, end

    def reseal(value: Any) -> None:
        # A reference (`locator` plus `content_digest`), or a bare locator
        # string such as `comparison_look_evaluated.prefixes`.
        items = list(enumerate(value)) if isinstance(value, list) else []
        if isinstance(value, dict):
            items = list(value.items())
            locator = value.get("locator")
            if isinstance(locator, str) and locator.startswith(f"{name}#bytes="):
                value["locator"], start, end = moved(locator)
                value["content_digest"] = hashlib.sha256(blob[start:end]).hexdigest()
                return
        for key, item in items:
            if isinstance(item, str) and item.startswith(f"{name}#bytes="):
                value[key] = moved(item)[0]
            else:
                reseal(item)

    forge(run, lambda event, payload: reseal(payload))


def insert_campaign(run: Path, position: int, extra: list[dict[str, Any]]) -> None:
    """Insert `extra` rows before original campaign record `position` (0-based).

    Records are renumbered, and every campaign self-locator is re-pointed at
    the same records and re-digested. References only point backwards, so
    each record is re-sealed against the records already rebuilt.
    """

    path = run / "campaign.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    old = _offsets(lines)
    rows: list[tuple[int | None, dict[str, Any]]] = [
        (index, json.loads(line)) for index, line in enumerate(lines)
    ]
    rows[position:position] = [(None, row) for row in extra]
    moved = {index: at for at, (index, _row) in enumerate(rows) if index is not None}
    records: list[Envelope] = []

    def rebase(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                rebase(item)
            return
        if not isinstance(value, dict):
            return
        locator = value.get("locator")
        if isinstance(locator, str) and locator.startswith("campaign.jsonl#bytes="):
            start, end = (int(part) for part in locator.rsplit("=", 1)[1].split(":"))
            first, last = moved[old.index(start)], moved[old.index(end) - 1] + 1
            built = _offsets([record.encode() for record in records])
            cited = b"".join(record.encode() for record in records[first:last])
            value["locator"] = f"campaign.jsonl#bytes={built[first]}:{built[last]}"
            value["content_digest"] = hashlib.sha256(cited).hexdigest()
        for item in value.values():
            rebase(item)

    for _index, row in rows:
        rebase(row["payload"])
        row["sequence"] = len(records) + 1
        records.append(Envelope.from_payload(row, family=campaign_family_for(14)))
    path.write_bytes(b"".join(record.encode() for record in records))


def bypassed(run: Path, transform: Any = lambda rows: rows) -> _Evidence:
    """An `_Evidence` over transformed campaign rows the lifecycle never admits."""

    rows = transform([dict(row) for row in campaign(run)])
    records = []
    for sequence, row in enumerate(rows, start=1):
        row["sequence"] = sequence
        records.append(Envelope.from_payload(row, family=campaign_family_for(14)))
    context = _Evidence(run, tuple(records), {}, family_version=14)
    # As `verify_evidence`'s reference pass: every cited range is read (and
    # proven) into the cache the precondition checks read from.
    for record in records:
        evidence = record.payload.get("evidence")
        if isinstance(evidence, list):
            for reference in evidence:
                assert _verify_reference(run, reference, context.cache) is None
    return context


def only(run: Path, event: str | None, detail: str, *, code: str = FINDING) -> dict[str, Any]:
    """The audit reports exactly one finding: `(event, code)` whose detail has `detail`."""

    found = audit(run)["mismatches"]
    seen = [(row["event"], row["code"], row["detail"]) for row in found]
    assert [(row["event"], row["code"]) for row in found] == [(event, code)], seen
    assert detail in (found[0]["detail"] or ""), seen
    return found[0]


def admitted(run: Path) -> tuple[str, int, dict[str, Any]]:
    """`(measurement, start index, admission)` of the run's one admitted start."""

    (found,) = [
        (mid, index, row["connection_admission"])
        for mid, rows in measurement_starts(run).items()
        for index, row in enumerate(rows)
        if row["connection_admission"] is not None
    ]
    return found


def edit_start(measurement_index: int, change: Any) -> Any:
    """An S1 edit applied to the measurement journal's `measurement_index`-th start."""

    def edit(rows: list[dict[str, Any]]) -> None:
        starts = [row for row in rows if row["event"] == "rollout_started"]
        change(starts[measurement_index]["payload"])

    return edit


def binding_record(run: Path, measurement_id: str) -> str:
    """The event of the campaign record that binds `measurement_id` (where S1 findings sit)."""

    return next(
        row["event"]
        for row in campaign(run)
        if row["payload"].get("measurement_id") == measurement_id
    )


async def compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    run, snapshot, _ = await connected_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    assert_clean_run(run, snapshot)
    return run, snapshot


async def failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> Path:
    adapter = ConnectedAdapter(used=EDITED["used"])
    options = _script(adapter, case)
    run, _, _ = await connected_run(tmp_path, monkeypatch, adapter, **options)
    assert names(run)[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    return run


async def failed_then_compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A failed probe, its suspension, and a resume whose fresh probe is compatible."""

    await failed_run(tmp_path, monkeypatch, "event_missing")
    run, snapshot, adapter = await connected_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] == "completed"
    assert len(adapter.probes) == 1
    return run


# --- genuine runs audit clean ------------------------------------------------------


async def test_a_compatible_connection_audits_clean(tmp_path, monkeypatch):
    run, snapshot = await compatible(tmp_path, monkeypatch)
    context = bypassed(run)
    assert _connection_findings(context) == []
    for record in context.records:
        if record.event.startswith("connection_preflight_"):
            assert _precondition_finding(context, record) is None, record.event
    assert admitted(run)[:2] == ("m-r00-champion", 0)
    assert (
        expected_binding(snapshot)
        == payloads(run, "connection_preflight_started")[0]["binding_digest"]
    )


@pytest.mark.parametrize(
    ("case", "status", "detail", "cause"), FAILURES, ids=[f[0] for f in FAILURES]
)
async def test_every_genuine_failed_probe_audits_clean(
    tmp_path, monkeypatch, case, status, detail, cause
):
    """Each failure class's terminal agrees with its probe journal, and its
    suspension names it right before."""

    await t7.test_a_failed_probe_suspends_with_nothing_between_and_nothing_dispatched(
        tmp_path, monkeypatch, case, status, detail, cause
    )
    run = tmp_path / "run"
    assert audit(run)["mismatches"] == []
    context = bypassed(run)
    (failed,) = [r for r in context.records if r.event == "connection_preflight_failed"]
    (suspension,) = [r for r in context.records if r.event == "campaign_suspended"]
    assert _precondition_finding(context, failed) is None
    assert _precondition_finding(context, suspension) is None
    assert _connection_findings(context) == []


STOPS = [
    "test_a_caller_cancellation_while_the_probe_runs_is_interrupted",
    "test_a_stop_another_task_decides_while_the_probe_runs_is_interrupted",
    "test_an_asyncio_cancellation_of_the_probe_closes_its_start",
    "test_a_caller_cancel_during_a_failing_materialize_is_interrupted",
    "test_a_caller_cancel_during_a_failing_score_is_interrupted",
    "test_another_tasks_stop_during_a_failing_score_keeps_its_terminal",
    "test_a_compatible_probe_after_a_stop_finishes_but_dispatches_nothing",
    "test_a_resume_after_the_boundary_does_not_probe",
]


@pytest.mark.parametrize("name", STOPS)
async def test_genuine_stopped_and_resumed_probes_audit_clean(tmp_path, monkeypatch, name):
    """Ruling 14/19/21: an `interrupted` terminal of a probe that computed a
    failure audits clean, whatever its trace records; so does a
    compatible-but-stopped `_finished` with no admission."""

    await getattr(t7, name)(tmp_path, monkeypatch)
    assert audit(tmp_path / "run")["mismatches"] == []


async def test_an_earlier_failed_preflight_of_the_binding_is_admitted(tmp_path, monkeypatch):
    """R60: suspension and a resume that probes again is the recovery path."""

    run = await failed_then_compatible(tmp_path, monkeypatch)
    operations = [
        row["event"] for row in campaign(run) if row["event"].startswith("connection_preflight_")
    ]
    assert operations == [
        "connection_preflight_started",
        "connection_preflight_failed",
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    first, second = payloads(run, "connection_preflight_started")
    assert first["binding_digest"] == second["binding_digest"]
    assert first["invocation_id"] != second["invocation_id"]
    assert admitted(run)[2] == {
        "operation_id": second["operation_id"],
        "controller_invocation_id": second["invocation_id"],
    }
    assert audit(run)["mismatches"] == []


async def test_the_measurement_reseal_alone_keeps_a_genuine_run_clean(tmp_path, monkeypatch):
    """The forging helpers change nothing by themselves."""

    run, _ = await compatible(tmp_path, monkeypatch)
    before = (run / "measurements" / "m-r00-champion" / "events.jsonl").read_bytes()
    forge_measurement(run, "m-r00-champion", lambda rows: None)
    assert (run / "measurements" / "m-r00-champion" / "events.jsonl").read_bytes() == before
    insert_campaign(run, 3, [])
    assert audit(run)["mismatches"] == []


# --- forged admissions -------------------------------------------------------------


async def test_an_admission_naming_another_invocation(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, _admission = admitted(run)

    def other(payload: dict[str, Any]) -> None:
        payload["connection_admission"]["controller_invocation_id"] = uuid.uuid4().hex

    forge_measurement(run, mid, edit_start(index, other))
    found = only(run, binding_record(run, mid), "another invocation than its preflight's")
    assert found["locator"].startswith(f"measurements/{mid}/events.jsonl#bytes=")


async def test_an_admission_naming_a_failed_preflight(tmp_path, monkeypatch):
    run = await failed_then_compatible(tmp_path, monkeypatch)
    failed = payloads(run, "connection_preflight_started")[0]
    mid, index, _admission = admitted(run)

    def named(payload: dict[str, Any]) -> None:
        payload["connection_admission"] = {
            "operation_id": failed["operation_id"],
            "controller_invocation_id": failed["invocation_id"],
        }

    forge_measurement(run, mid, edit_start(index, named))
    only(run, binding_record(run, mid), "a preflight that is not compatible")


async def test_an_admission_naming_no_preflight(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, _admission = admitted(run)

    def unknown(payload: dict[str, Any]) -> None:
        payload["connection_admission"]["operation_id"] = uuid.uuid4().hex

    forge_measurement(run, mid, edit_start(index, unknown))
    only(run, binding_record(run, mid), "names no connection preflight")


async def test_two_admissions_under_one_binding(tmp_path, monkeypatch):
    """A second journal's first start also claims the boundary."""

    run, _ = await compatible(tmp_path, monkeypatch)
    _mid, _index, admission = admitted(run)

    def claim(payload: dict[str, Any]) -> None:
        assert payload["connection_admission"] is None
        payload["connection_admission"] = dict(admission)

    forge_measurement(run, "m-r01-a01", edit_start(0, claim))
    found = only(run, binding_record(run, "m-r01-a01"), "a second start under the binding")
    assert found["locator"].startswith("measurements/m-r01-a01/events.jsonl#bytes=")


async def test_an_admission_after_an_executed_start_of_its_journal(tmp_path, monkeypatch):
    """The admission moves from the journal's first start to its second."""

    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, admission = admitted(run)

    def move(rows: list[dict[str, Any]]) -> None:
        starts = [row["payload"] for row in rows if row["event"] == "rollout_started"]
        starts[index]["connection_admission"] = None
        starts[index + 1]["connection_admission"] = dict(admission)

    forge_measurement(run, mid, move)
    only(run, binding_record(run, mid), "an earlier start under the binding executed")


async def test_executed_starts_without_an_admission(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, _admission = admitted(run)

    def drop(payload: dict[str, Any]) -> None:
        payload["connection_admission"] = None

    forge_measurement(run, mid, edit_start(index, drop))
    only(run, binding_record(run, mid), "executed starts carry no admission")


async def test_a_compatible_preflight_after_the_admitted_one(tmp_path, monkeypatch):
    """A second compatible preflight of the same binding, written just before
    `campaign_finished`, long after every measurement completed. In campaign
    order it follows the admitted preflight, so the admission no longer names
    the binding's last preflight. The copy is otherwise genuine: it probes
    the Champion in force there and has its own copy of the probe journal,
    so its own records audit clean. (Records cite earlier records by
    sequence, so a copy inserted earlier would also break those citations.)"""

    run, snapshot = await compatible(tmp_path, monkeypatch)
    rows = campaign(run)
    start = next(r for r in rows if r["event"] == "connection_preflight_started")
    finished = next(r for r in rows if r["event"] == "connection_preflight_finished")
    old, new = start["operation_id"], uuid.uuid4().hex
    shutil.copytree(run / "connection" / old, run / "connection" / new)
    copies = []
    for row in (start, finished):
        row = copy.deepcopy(row)
        row["operation_id"] = new
        if "operation_id" in row["payload"]:
            row["payload"]["operation_id"] = new
        for reference in row["payload"].get("evidence", []):
            reference["run_id"] = new
            reference["locator"] = reference["locator"].replace(old, new)
        copies.append(row)
    probed = copies[0]["payload"]
    probed["candidate_digest"] = snapshot.champion[1]
    probed["dependency_digest"] = snapshot.champion_dependency_digest
    probed["probe_seed"] = probe_seed(probed["binding_digest"], snapshot.champion[1])
    position = len(rows) - 1
    assert rows[position]["event"] == "campaign_finished"
    insert_campaign(run, position, copies)
    found = only(run, "connection_preflight_started", "a later preflight of the binding")
    assert found["sequence"] == position + 1


# --- forged digests ----------------------------------------------------------------


async def test_a_forged_preflight_binding_digest(tmp_path, monkeypatch):
    """The seed is re-derived from the forged digest, so the lifecycle admits it;
    only the audit's re-derivation from the epoch refuses it. The whole-journal
    pass keys preflights by their epoch's binding, so the admission still names
    its preflight and nothing else is reported."""

    run, _ = await compatible(tmp_path, monkeypatch)
    forged = hashlib.sha256(b"another binding").hexdigest()

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_started":
            payload["binding_digest"] = forged
            payload["probe_seed"] = probe_seed(forged, payload["candidate_digest"])

    forge(run, edit)
    only(run, "connection_preflight_started", "does not re-derive")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "0" * 64),
        ("scorer", "1" * 64),
        ("case_id", "c2"),
    ],
)
async def test_a_preflight_field_that_is_not_its_epochs(tmp_path, monkeypatch, field, value):
    """The digest is re-computed over the forged field, so only the comparison
    with the epoch identity refuses it (`case_id`: the first train case)."""

    run, snapshot = await compatible(tmp_path, monkeypatch)
    corpus = snapshot.epochs[0]["corpus_digest"]

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_started":
            payload[field] = value
            keys = (
                "epoch",
                "target",
                "environment",
                "scorer",
                "metric_contract",
                "primary_metric",
                "case_id",
                "case_index",
            )
            digest = binding_digest(**{k: payload[k] for k in keys}, corpus_digest=corpus)
            payload["binding_digest"] = digest
            payload["probe_seed"] = probe_seed(digest, payload["candidate_digest"])

    forge(run, edit)
    only(run, "connection_preflight_started", f"{field} is not its condition epoch's")


async def test_a_forged_rollout_binding_digest(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, _admission = admitted(run)

    def other(payload: dict[str, Any]) -> None:
        payload["connection_binding_digest"] = hashlib.sha256(b"elsewhere").hexdigest()

    forge_measurement(run, mid, edit_start(index + 1, other))
    only(run, binding_record(run, mid), "another connection binding than its measurement's epoch")


# --- the probe journal -------------------------------------------------------------


def _probe_events(run: Path) -> Path:
    (operation,) = [
        row["operation_id"]
        for row in campaign(run)
        if row["event"] == "connection_preflight_started"
    ]
    return run / "connection" / operation / "events.jsonl"


async def test_a_missing_probe_journal(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    _probe_events(run).unlink()
    only(run, "connection_preflight_finished", "", code="evidence_missing")


async def test_a_corrupted_probe_journal_byte(tmp_path, monkeypatch):
    run, _ = await compatible(tmp_path, monkeypatch)
    path = _probe_events(run)
    data = bytearray(path.read_bytes())
    at = data.index(b'"rollout_started"') + 1
    data[at] = ord("R")
    path.write_bytes(bytes(data))
    only(run, "connection_preflight_finished", "", code="evidence_mismatch")


async def test_a_terminal_status_that_is_not_the_probe_journals(tmp_path, monkeypatch):
    """A Target-field failure (the rollout completed, nothing was scored)
    rewritten as an inconclusive turn budget, its suspension's reason with it."""

    run = await failed_run(tmp_path, monkeypatch, "field_type")

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload.update(
                status="connection_preflight_inconclusive",
                detail_code=None,
                manifest_path=None,
                observed_path=None,
                cause_code="turn_budget",
            )
        elif event == "campaign_suspended":
            payload["reason"] = "connection_preflight_inconclusive"

    forge(run, edit)
    only(run, "connection_preflight_failed", "status is not the probe journal's")


async def test_the_audit_accepts_a_description_secret_terminal_without_a_rollout(
    tmp_path, monkeypatch
):
    """#287: the probe materialized, attested and released its instance and ran
    no rollout; the audit cannot see the description (it is in no journal), so
    it admits the code from what the probe journal shows."""

    run, result, _ = await t7.description_run(tmp_path, monkeypatch, {"model": t7.JUDGE})
    assert result.code == "connection_incompatible"
    assert audit(run)["mismatches"] == []
    context = bypassed(run)
    (failed,) = [r for r in context.records if r.event == "connection_preflight_failed"]
    assert failed.payload["detail_code"] == "connection_description_secret"
    assert _precondition_finding(context, failed) is None
    assert _connection_findings(context) == []
    # The repaired resume audits clean too: the failed preflight is the
    # admitted recovery path.
    run, result, _ = await t7.description_run(tmp_path, monkeypatch, t7.REPAIRED)
    assert result.terminal == "finished"
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("case", ["materialize", "event_missing"])
async def test_the_audit_refuses_that_code_when_materialize_did_not_finish(
    tmp_path, monkeypatch, case
):
    """The code needs a finished materialize and release and no rollout: over
    a probe whose materialize failed, or one that ran its rollout, it is a
    finding."""

    run = await failed_run(tmp_path, monkeypatch, case)

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload.update(
                status="connection_incompatible",
                detail_code="connection_description_secret",
                manifest_path="scorer.allowed_env.B2E_LAB_JUDGE_MODEL",
                observed_path="target.description",
                cause_code=None,
            )
        elif event == "campaign_suspended":
            payload["reason"] = "connection_incompatible"

    forge(run, edit)
    detail = "status is not" if case == "materialize" else "detail is not"
    only(run, "connection_preflight_failed", f"{detail} the probe journal's")


async def test_an_attestation_failure_after_materialize_stays_unavailable(tmp_path, monkeypatch):
    """An attestation refusal leaves the probe journal a description secret
    leaves (materialized, released, no rollout): it still audits clean as
    `unavailable`, and no other incompatibility is admitted over it."""

    run = await failed_run(tmp_path, monkeypatch, "attestation")
    assert audit(run)["mismatches"] == []

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload.update(
                status="connection_incompatible",
                detail_code="connection_event_missing",
                manifest_path="scorer.metric_contract.target_fields[0]",
                observed_path="events[event=assistant_message,last]/content",
                cause_code=None,
            )
        elif event == "campaign_suspended":
            payload["reason"] = "connection_incompatible"

    forge(run, edit)
    only(run, "connection_preflight_failed", "detail is not the probe journal's")


async def test_a_rollout_cause_that_is_not_the_probe_journals(tmp_path, monkeypatch):
    run = await failed_run(tmp_path, monkeypatch, "time_budget")

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload["cause_code"] = "turn_budget"

    forge(run, edit)
    only(run, "connection_preflight_failed", "cause is not the probe journal's")


@pytest.mark.parametrize(
    ("case", "cause"),
    [
        ("materialize", "made_up_code"),
        ("materialize", "turn_budget"),
        ("time_budget", "target_failed"),
    ],
)
async def test_a_cause_outside_the_closed_vocabulary(tmp_path, monkeypatch, case, cause):
    """The audit flags a cause outside its failure class's closed set, including where no
    probe-journal fact could contradict it (a probe that never reached a
    rollout). The record schema refuses such a cause too, so the forgery is
    written under a widened copy of the probe's vocabulary; the audit keeps
    its own reading of the closed set."""

    from evolution.tree.target import connection_probe

    run = await failed_run(tmp_path, monkeypatch, case)
    widened = {status: codes | {cause} for status, codes in connection_probe.CAUSE_CODES.items()}
    monkeypatch.setattr(connection_probe, "CAUSE_CODES", widened)

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload["cause_code"] = cause

    forge(run, edit)
    only(run, "connection_preflight_failed", "cause is not in the family-14 vocabulary")


async def test_a_field_detail_over_a_scored_probe(tmp_path, monkeypatch):
    """A Scorer output failure rewritten as a Target-field failure: the probe
    journal shows the Scorer ran, so no Target field failed."""

    run = await failed_run(tmp_path, monkeypatch, "primary_type")

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "connection_preflight_failed":
            payload.update(
                detail_code="connection_event_missing",
                manifest_path="scorer.metric_contract.target_fields[0]",
                observed_path="events[event=assistant_message,last]/content",
            )

    forge(run, edit)
    only(run, "connection_preflight_failed", "detail is not the probe journal's")


@pytest.mark.parametrize("terminal", ["failed", "interrupted"])
async def test_a_terminal_cleanup_that_is_not_the_probe_journals(tmp_path, monkeypatch, terminal):
    if terminal == "failed":
        run = await failed_run(tmp_path, monkeypatch, "field_type")
    else:
        await t7.test_a_caller_cancellation_while_the_probe_runs_is_interrupted(
            tmp_path, monkeypatch
        )
        run = tmp_path / "run"
    event = f"connection_preflight_{terminal}"

    def edit(name: str, payload: dict[str, Any]) -> None:
        if name == event:
            assert payload["cleanup"] == "released"
            payload["cleanup"] = "not_needed"

    forge(run, edit)
    only(run, event, "cleanup is not the probe journal's")


async def test_a_compatible_terminal_over_an_unscored_probe(tmp_path, monkeypatch):
    """Lifecycle bypassed: a `_finished` whose probe journal records a Scorer
    failure. The audit's own check refuses it."""

    run = await failed_run(tmp_path, monkeypatch, "scorer_exit")

    def compatible_terminal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        at = next(i for i, r in enumerate(rows) if r["event"] == "connection_preflight_failed")
        row = rows[at]
        row["event"] = "connection_preflight_finished"
        row["payload"] = {
            "status": "compatible",
            "trace_digest": row["payload"]["trace_digest"],
            "cleanup": "released",
            "evidence": row["payload"]["evidence"],
        }
        return rows[: at + 1]

    context = bypassed(run, compatible_terminal)
    finding = _precondition_finding(context, context.records[-1])
    assert finding is not None and finding[0] == FINDING, finding
    assert "a compatible terminal needs" in finding[1]


# --- the connection suspension ------------------------------------------------------


async def test_a_suspension_with_a_wrong_seq_is_refused(tmp_path, monkeypatch):
    """The lifecycle already refuses it, and the audit reports that refusal."""

    run = await failed_run(tmp_path, monkeypatch, "field_type")

    def edit(event: str, payload: dict[str, Any]) -> None:
        if event == "campaign_suspended":
            payload["evidence"]["seq"] -= 1

    forge(run, edit)
    only(run, None, "names the failed preflight", code="suffix_refused")


@pytest.mark.parametrize("forgery", ["names_the_start", "not_adjacent", "other_reason"])
async def test_the_audit_checks_the_connection_suspension_itself(tmp_path, monkeypatch, forgery):
    """Lifecycle bypassed: the audit's own `stage_record` rule for a connection
    reason (the failed terminal of that status, immediately before)."""

    run = await failed_run(tmp_path, monkeypatch, "field_type")

    def transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        suspension = rows[-1]
        if forgery == "names_the_start":
            # The start has the same operation id, so only the kind refuses it.
            start = next(
                i for i, r in enumerate(rows) if r["event"] == "connection_preflight_started"
            )
            suspension["payload"]["evidence"]["seq"] = start + 1
        elif forgery == "not_adjacent":
            rows.insert(-1, copy.deepcopy(rows[0]))
        else:
            suspension["payload"]["reason"] = "connection_preflight_unavailable"
        return rows

    context = bypassed(run, transform)
    assert context.records[-1].event == "campaign_suspended"
    finding = _precondition_finding(context, context.records[-1])
    assert finding == (FINDING, "the stage record is not a failing record of that kind")


# --- families up to 13 -------------------------------------------------------------


async def test_an_unreadable_journal_withholds_only_the_missing_admission(tmp_path, monkeypatch):
    """H10 limitation (b): a torn tail of the admitted journal hides the
    admission, so "carry no admission" is withheld and the unreadable journal
    is reported in its place."""

    run, _ = await compatible(tmp_path, monkeypatch)
    mid, _index, _admission = admitted(run)
    path = run / "measurements" / mid / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"torn')
    found = _connection_findings(bypassed(run))
    assert [(row["detail"], row["locator"]) for row in found] == [
        (UNREADABLE, f"measurements/{mid}/events.jsonl#bytes=0:{path.stat().st_size}")
    ]
    assert found[0]["event"] == binding_record(run, mid)
    reasons = [str(item) for item in audit(run)["mismatches"]]
    assert any("not a valid journal" in reason for reason in reasons), reasons
    assert any(UNREADABLE in reason for reason in reasons), reasons
    assert not any("carry no admission" in reason for reason in reasons), reasons


async def test_a_forged_admission_beside_a_torn_journal_of_its_binding(tmp_path, monkeypatch):
    """I1: a torn tail on a peripheral journal of the binding, outside every
    cited range, leaves the admission in a readable journal judged, so the
    forgery is reported. No conclusion is withheld, so the uncited torn tail
    itself stays ignored (H3)."""

    run, _ = await compatible(tmp_path, monkeypatch)
    mid, index, _admission = admitted(run)

    def other(payload: dict[str, Any]) -> None:
        payload["connection_admission"]["controller_invocation_id"] = uuid.uuid4().hex

    forge_measurement(run, mid, edit_start(index, other))
    path = run / "measurements" / "m-r01-final-01-champion" / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"torn')
    found = only(run, binding_record(run, mid), "another invocation than its preflight's")
    assert found["locator"].startswith(f"measurements/{mid}/events.jsonl#bytes=")
    details = [row["detail"] for row in _connection_findings(bypassed(run))]
    assert details == ["the admission names another invocation than its preflight's"]
