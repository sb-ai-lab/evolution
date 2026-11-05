"""Audit real H3 journals directly, including concrete durable forgeries."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.execution.corpus import load_reflection_corpus
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import (
    OBSERVE,
    TWO_ATTEMPTS,
    _cut_hook,
    _measurement_events,
    _records,
    _statistical,
    observe_barrier,  # noqa: F401
)
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_gate_audit import _rewrite_campaign

pytestmark = pytest.mark.slow


def audit(run):
    return verify_evidence(run, load_reflection_corpus(run.parent / "corpus.jsonl").cases)


def forge(run, edit):
    _rewrite_campaign(run, edit, family=14)


def finding(run, event, detail, *, code="precondition_failed"):
    found = audit(run)["mismatches"]
    assert any(
        row["event"] == event and row["code"] == code and detail in row["detail"] for row in found
    ), found


async def cut_run(
    tmp_path, monkeypatch, hook="before_selection_decided", config=OBSERVE, *, call=1, **kwargs
):
    _cut_hook(monkeypatch, hook, call=call)
    with pytest.raises(Crash):
        await family14_run(tmp_path, monkeypatch, config, **kwargs)
    return tmp_path / "run"


@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
async def test_real_final_journals_audit_clean(tmp_path, monkeypatch, mode):
    config = OBSERVE if mode == "observe" else _statistical(mode)
    run, _ = await family14_run(tmp_path, monkeypatch, config, campaign={"rounds": 2})
    result = audit(run)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("point", 0.123),
        ("lower", 0.0123),
        ("exact", True),
        ("pairing", "unpaired"),
    ],
)
async def test_global_fields_are_rederived_from_cited_rows(tmp_path, monkeypatch, field, value):
    run = await cut_run(tmp_path, monkeypatch, config=_statistical("bound"))
    assert payloads(run, "global_comparison_completed")[0][field] != value

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload[field] = value
            if field == "exact":
                payload["lower"] = payload["point"]

    forge(run, edit)
    finding(run, "global_comparison_completed", field)


@pytest.mark.parametrize("field,value", [("point", 0.321), ("critical", True), ("complete", False)])
async def test_member_completion_is_rederived_from_cited_rows(tmp_path, monkeypatch, field, value):
    run = await cut_run(tmp_path, monkeypatch)

    def edit(event, payload):
        if event == "final_measurement_completed" and payload["measurement_id"].endswith("a01"):
            payload[field] = value
            if field == "complete":
                payload.update(point=None, missing=1)

    forge(run, edit)
    finding(run, "final_measurement_completed", "completion is not its member's rows")


@pytest.mark.parametrize("event", ["final_measurement_completed", "global_comparison_completed"])
async def test_missing_reference_file_is_a_named_integrity_finding(tmp_path, monkeypatch, event):
    run = await cut_run(tmp_path, monkeypatch)

    def edit(name, payload):
        if name == event:
            payload["evidence"][0]["locator"] = "absent.jsonl#bytes=0:10"

    forge(run, edit)
    result = audit(run)["mismatches"]
    assert any(row["event"] == event and row["code"] == "evidence_missing" for row in result), (
        result
    )


@pytest.mark.parametrize("event", ["global_comparison_completed", "selection_decided"])
async def test_completion_and_comparison_references_keep_their_order(tmp_path, monkeypatch, event):
    run, _ = await family14_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)

    def edit(name, payload):
        if name == event:
            if name == "selection_decided":
                payload["evidence"][-2:] = reversed(payload["evidence"][-2:])
            else:
                payload["evidence"].reverse()

    forge(run, edit)
    if event == "selection_decided":
        # Family 14 authenticates aggregate citation order during replay too.
        finding(run, None, "every completion and every comparison in order", code="suffix_refused")
    else:
        finding(run, event, "completion")


@pytest.mark.parametrize(
    "event,field,value,detail",
    [
        (
            "final_measurement_bound",
            "measurement_id",
            "m-r01-final-02-champion",
            "not a final member",
        ),
        ("final_measurement_bound", "round_stop", "first_accepted", "policy in force"),
        ("finalists_bound", "seed_mode", "none", "policy in force"),
        (
            "global_comparison_completed",
            "finalist_measurement_id",
            "m-r01-final-01-champion",
            "members",
        ),
        (
            "global_comparison_completed",
            "reason",
            "global_point_improved",
            "eligibility does not follow",
        ),
        ("global_comparison_completed", "level", 0.5, "level is its finalist's own"),
    ],
)
async def test_binding_and_comparison_lifecycle_refuses_forged_contract(
    tmp_path, monkeypatch, event, field, value, detail
):
    run = await cut_run(tmp_path, monkeypatch, config=_statistical("bound"))

    def edit(name, payload):
        if name == event:
            payload[field] = value

    forge(run, edit)
    finding(run, None, detail, code="suffix_refused")


async def test_wrong_accepted_flag_is_refused(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch, config=_statistical("bound"))

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload.update(accepted=False, reason="global_bound_not_positive")

    forge(run, edit)
    finding(run, None, "eligibility does not follow", code="suffix_refused")


async def test_earlier_completion_prefix_is_refused_at_citation_floor(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    first = _measurement_events(run, member).read_bytes().splitlines(keepends=True)[0]

    def edit(event, payload):
        if event == "final_measurement_completed" and payload["measurement_id"] == member:
            payload["prefix"] = f"measurements/{member}/events.jsonl#bytes=0:{len(first)}"

    forge(run, edit)
    finding(run, None, "ends before an earlier citation", code="suffix_refused")


async def test_swapped_comparison_records_are_refused(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)
    path = run / "campaign.jsonl"
    records = _records(path)
    indexes = [i for i, row in enumerate(records) if row["event"] == "global_comparison_completed"]
    a, b = indexes
    records[a]["payload"], records[b]["payload"] = records[b]["payload"], records[a]["payload"]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records)
    )
    finding(run, None, "compared in order", code="suffix_refused")


async def test_selection_ranking_is_rederived_in_attempt_order(tmp_path, monkeypatch):
    run, _ = await family14_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)

    def edit(event, payload):
        if event == "selection_decided":
            payload["ranking"].reverse()

    forge(run, edit)
    finding(run, None, "another ranking", code="suffix_refused")


async def test_wrong_retained_baseline_is_refused(tmp_path, monkeypatch):
    from tests.campaign_drivers import _cohort_scorer, _flag_on_finalists_bound

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**_statistical("bound"), "scorer": _cohort_scorer(flag, flip=True)},
        campaign={"rounds": 2},
    )
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event == "baseline_bound" and payload["round"] == 2:
            payload["measurement_id"] = "m-r00-champion"

    forge(run, edit)
    finding(run, None, "baseline", code="suffix_refused")


async def test_primary_outside_range_cannot_hide_inside_in_range_mean(tmp_path, monkeypatch):
    from tests.campaign_drivers import (
        EDITED,
        TWO_CASE_SCORER,
        _arm_on_finalists_bound,
        _blind_to_out_of_range,
        _CohortTagged,
    )

    _blind_to_out_of_range(monkeypatch)
    scorer = TWO_CASE_SCORER.replace(
        '"metrics": {"score": 1.0 if passed else 0.0}',
        '"metrics": {"score": (1.5 if payload["case_id"] == "c1" else 0.0) '
        'if passed and "#cohort" in view else (1.0 if passed else 0.0)}',
    )
    adapter = _CohortTagged(faults={}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    run = await cut_run(
        tmp_path,
        monkeypatch,
        "after_cohort_measurement_completed",
        config={**OBSERVE, "scorer": scorer},
        adapter=adapter,
        call=2,
    )
    assert payloads(run, "final_measurement_completed")[1]["point"] == 0.75
    finding(run, "final_measurement_completed", "c1/01", code="primary_out_of_range")


@pytest.mark.parametrize(
    "intervening", ["", "malformed complete line\n", "malformed\rcomplete line\n"]
)
@pytest.mark.parametrize("row_event", ["rollout_started", "rollout_finished", "scoring_finished"])
async def test_completed_member_cannot_gain_rows_after_cited_prefix(
    tmp_path, monkeypatch, intervening, row_event
):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    path = _measurement_events(run, member)
    records = _records(path)
    extra = next(row for row in records if row["event"] == row_event)
    extra["sequence"] = len(records) + 1
    with path.open("a") as stream:
        stream.write(intervening)
        stream.write(json.dumps(extra, sort_keys=True, separators=(",", ":")) + "\n")
    finding(
        run, "final_measurement_completed", "after its completion", code="completed_member_appended"
    )
    # Later bytes cannot contaminate the comparison's authoritative numerical read.
    assert not [m for m in audit(run)["mismatches"] if m["event"] == "global_comparison_completed"]


@pytest.mark.parametrize("tail", [b'{"torn":', b"malformed but outside the cited prefix\n"])
async def test_uncited_malformed_tail_cannot_change_final_facts(tmp_path, monkeypatch, tail):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    with _measurement_events(run, member).open("ab") as stream:
        stream.write(tail)
    assert audit(run)["mismatches"] == []


async def test_changed_scored_bytes_inside_prefix_are_detected(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    path = _measurement_events(run, member)
    original = path.read_bytes()
    assert b'"score":1.0' in original
    attestation = next(
        row for row in payloads(run, "instance_attested") if row["measurement_id"] == member
    )
    # H6 checkpoints final attestations before dispatch. Their hashes cover
    # materialization, while the completion's locator covers the scored rows.
    attested_end = int(attestation["evidence"][0]["locator"].rsplit(":", 1)[1])
    assert attested_end <= original.index(b'"score":1.0')
    path.write_bytes(original.replace(b'"score":1.0', b'"score":0.0', 1))
    finding(run, "final_measurement_completed", "completion is not its member's rows")
    finding(run, "global_comparison_completed", "point is 0.0, the record says 0.5")


async def test_changed_bytes_inside_attested_prefix_fail_reference_integrity(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    attestation = next(
        row for row in payloads(run, "instance_attested") if row["measurement_id"] == member
    )
    reference = attestation["evidence"][0]
    path = _measurement_events(run, member)
    lines = path.read_bytes().splitlines(keepends=True)
    first = json.loads(lines[0])
    first["timestamp"] = "2000" + first["timestamp"][4:]
    changed = canonical_json(first) + b"\n"
    assert changed != lines[0] and len(changed) == len(lines[0])
    assert len(changed) <= int(reference["locator"].rsplit(":", 1)[1])
    path.write_bytes(changed + b"".join(lines[1:]))
    assert any(
        row["event"] == "instance_attested"
        and row["code"] == "evidence_mismatch"
        and row["locator"] == reference["locator"]
        for row in audit(run)["mismatches"]
    )


@pytest.mark.parametrize("substitution", ["another_member", "duplicate"])
async def test_completion_must_cite_exactly_its_chain_attestations(
    tmp_path, monkeypatch, substitution
):
    run = await cut_run(tmp_path, monkeypatch, config=_statistical("bound"))
    champion, finalist = payloads(run, "final_measurement_completed")
    assert finalist["evidence"]

    def edit(event, payload):
        if event == "final_measurement_completed" and payload["measurement_id"].endswith("a01"):
            payload["evidence"] = (
                champion["evidence"]
                if substitution == "another_member"
                else [*payload["evidence"], payload["evidence"][0]]
            )

    forge(run, edit)
    finding(run, "final_measurement_completed", "does not cite its member's attestations")


async def test_born_final_member_refuses_candidate_gate_imports(tmp_path, monkeypatch):
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from evolution.tree.execution.journal_schema import S1_FAMILY
    from evolution.tree.storage.envelope import Envelope
    from tests.tree.execution.test_journal_durability import imported_payload

    run = await cut_run(tmp_path, monkeypatch, "after_cohort_measurement_completed", call=2)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    path = _measurement_events(run, member)
    created = _records(path)[0]
    identity = RunIdentityV1.from_payload(created["payload"]["identity"])
    imported = {
        **created,
        "sequence": 2,
        "event": "sample_imported",
        "operation_id": None,
        "payload": imported_payload(identity, identity.to_payload()["schedule"][0]),
    }
    # A real S1-admitted import at birth, declaring the Attempt's gate as source.
    imported["payload"]["source"]["measurement_id"] = "m-r01-a01"
    path.write_bytes(
        Envelope.from_payload(created, family=S1_FAMILY).encode()
        + Envelope.from_payload(imported, family=S1_FAMILY).encode()
    )
    finding(run, "final_measurement_bound", "imports no rows", code="baseline_import_mismatch")


@pytest.mark.parametrize("critical", [False, True])
async def test_rollout_owned_incomplete_final_audits_clean(tmp_path, monkeypatch, critical):
    from tests.campaign_drivers import (
        EDITED,
        LENIENT,
        VARYING_SCORER,
        _arm_on_finalists_bound,
        _cohort_scorer,
        _CohortFaults,
        _flag_on_finalists_bound,
    )

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    adapter = _CohortFaults(faults={("candidate", "c1", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _cohort_scorer(flag, critical=True) if critical else VARYING_SCORER},
        adapter=adapter,
        campaign={"indeterminate": LENIENT},
    )
    assert payloads(run, "global_comparison_completed")[0]["reason"] == "final_incomplete"
    assert audit(run)["mismatches"] == []


async def test_unreadable_cited_file_remains_a_persistence_failure(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch)
    member = payloads(run, "finalists_bound")[0]["members"][1]
    path = _measurement_events(run, member)
    real_read = type(path).read_bytes

    def unreadable(self):
        if self == path:
            raise PermissionError("cited measurement cannot be read")
        return real_read(self)

    monkeypatch.setattr(type(path), "read_bytes", unreadable)
    with pytest.raises(PermissionError, match="cited measurement cannot be read"):
        audit(run)


def test_optional_stored_binding_reference_is_integrity_checked(tmp_path, observe_barrier):  # noqa: F811
    """Stable shape/admission only: no stored-side measurement is executed."""
    from tests.test_tree_campaign_family14 import _before_look, _life
    from tests.test_tree_campaign_family14_final import _finalists, _member

    records = copy.deepcopy(_before_look(observe_barrier))
    digest = hashlib.sha256(b"x" * 100).hexdigest()
    baseline = next(row["payload"] for row in records if row["event"] == "baseline_complete")
    baseline["evidence"][0].update(
        content_digest=digest,
        locator=f"measurements/{baseline['measurement_id']}/events.jsonl#bytes=0:100",
    )
    life = _life(records, stability="stable")
    life.accept("comparison_look_evaluated", **life.look_payload(life.sealed, 1))
    life.accept(
        "attempt_closed",
        attempt_id=life.sealed,
        outcome="finalist",
        reason="observe_finalist",
        look=1,
        code=None,
    )
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    bound = _member(life, 0)
    life.accept("final_measurement_bound", **bound)
    (tmp_path / "campaign.jsonl").write_bytes(b"".join(row.encode() for row in life.envelopes))
    source = _measurement_events(tmp_path, bound["stored_champion"]["measurement_id"])
    source.parent.mkdir(parents=True)
    source.write_bytes(b"y" * 100)  # Same extent, different actual cited bytes.
    result = verify_evidence(tmp_path)["mismatches"]
    assert any(
        row["event"] == "final_measurement_bound" and row["code"] == "evidence_mismatch"
        for row in result
    ), result
