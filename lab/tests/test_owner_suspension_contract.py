"""Task6: real writer journals through the installed adapter and public Owner.

threshold and stage_record use the retained dev55/b8 capture in the private
checkout and its neutral replay (``details-reader``) in the public checkout. fault_rows
(``suspension-fault``) and the generated ``suspension_evidence`` journals are historical dev52/b6 inputs (user
decision 2026-10-19): read by the b9 adapter, labelled with their producing pair, never recaptured.

Fixtures are generated only by explicitly invoked capture commands. These tests
never launch campaigns or import the capture generator.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from evolution_lab.owner import Owner, core_raw
from panel_fixtures import load_snapshot
from test_owner_delete import register, REF

ROOT = Path(__file__).parent / "fixtures"
GENERATED = ROOT / "suspension_evidence"
DETAILS_READER = ROOT / "panel" / "details-reader"
B6_CORE_SHA256 = "acdbc710902ea448fe1f22308250ba35768df5ee1cf4669c5b6751cb46514ec0"
B8_CORE_SHA256 = "bce365df308b54cc73eb2ff724e3d5843263d666f5359da31b1197b419763d44"
B8_CASES = {"threshold", "stage_record"}
CASES = ("threshold", "stage_record", "fault_rows", "final-fault", "champion-gap", "component-drift")


def fixture(name):
    if name in B8_CASES:
        snap = load_snapshot("details")
        retained = snap["meta"]["retained_journals"][name]
        original = ROOT / "panel" / "details" / retained["path"]
        if original.is_file():
            return original, retained, snap
        reader = json.loads((DETAILS_READER / "meta.json").read_text())
        kept = reader["journals"][name]
        return DETAILS_READER / kept["path"], kept, snap
    if name == "fault_rows":
        snap = load_snapshot("suspension-fault")
        kept = snap["meta"]["retained_journals"][name]
        return ROOT / "panel" / "suspension-fault" / kept["path"], kept, snap
    meta = json.loads((GENERATED / "meta.json").read_text())
    item = meta["scenarios"][name]
    kept = next(row for row in item["files"] if row["path"] == "campaign.jsonl")
    return GENERATED / name / "campaign.jsonl", kept, meta


def writer_suspensions(journal):
    return [row for row in map(json.loads, journal.read_text().splitlines())
            if row["event"] == "campaign_suspended"]


@pytest.mark.parametrize("name", CASES)
def test_retained_writer_bytes_and_producing_pair(name):
    journal, kept, meta = fixture(name)
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == kept["sha256"]
    if name in B8_CASES:
        assert meta["meta"]["core_version"] == "2.0.0b8" != core_raw.SUPPORTED_CORE
        assert meta["meta"]["lab_version"] == "0.0.3.dev55"
        assert meta["meta"]["producing_wheels"]["core"]["sha256"] == B8_CORE_SHA256
    elif name == "fault_rows":
        assert meta["meta"]["core_version"] == "2.0.0b6" != core_raw.SUPPORTED_CORE
        assert meta["meta"]["lab_version"] == "0.0.3.dev52"
        assert meta["meta"]["producing_wheels"]["core"]["sha256"] == B6_CORE_SHA256
    else:
        assert meta["completed"] and meta["scenarios"][name]["completed"]
        assert meta["core_version"] == "2.0.0b6" != core_raw.SUPPORTED_CORE
        assert meta["core_wheel"]["sha256"] == B6_CORE_SHA256
        for kept_file in meta["scenarios"][name]["files"]:
            source = GENERATED / name / kept_file["path"]
            assert hashlib.sha256(source.read_bytes()).hexdigest() == kept_file["sha256"]


@pytest.mark.parametrize("name", CASES)
def test_installed_adapter_projects_actual_writer_fields(name):
    journal, _, meta = fixture(name)
    actual = core_raw.campaign_suspensions(journal.parent)
    raw = writer_suspensions(journal)
    assert actual and len(actual) == len(raw)
    for entry, record in zip(actual, raw):
        payload = record["payload"]
        evidence = payload["evidence"]
        assert entry == {
            "seq": record["sequence"], "reason": payload["reason"], "owner": payload["owner"],
            "evidence_kind": evidence["evidence_kind"],
            "evidence": {key: value for key, value in evidence.items() if key not in {"evidence_kind", "rows"}},
            "rows": evidence.get("rows"),
        }
    if name in {"threshold", "stage_record", "fault_rows"}:
        operation = meta["meta"]["retained_journals"][name]["operation_id"]
        answer = meta["routes"][f"/v1/operations/{operation}/suspensions"]
        assert answer["status"] == 200 and answer["body"]["suspensions"] == actual


def test_actual_matrix_has_every_kind_and_both_stage_shapes():
    rows = [row for name in CASES for row in core_raw.campaign_suspensions(fixture(name)[0].parent)]
    assert {row["evidence_kind"] for row in rows} == {
        "threshold", "stage_record", "fault_rows", "final_rows", "champion_rows"}
    assert {"prefix" in row["evidence"] for row in rows if row["evidence_kind"] == "stage_record"} == {False, True}
    for row in rows:
        if row["evidence_kind"] == "threshold":
            assert isinstance(row["evidence"]["crossing"]["max_rate"], str)
        if row["rows"] is not None:
            assert row["rows"] and row["evidence"]["count"] >= len(row["rows"])


@pytest.mark.parametrize("name", CASES)
def test_public_owner_reads_retained_journal_without_campaign_execution(tmp_path, name):
    journal, _, _ = fixture(name)
    async def read():
        async with Owner.open(tmp_path / "state") as owner:
            await register(owner)
            run = tmp_path / "state" / "artifacts" / REF.id / REF.id
            run.mkdir(parents=True)
            shutil.copyfile(journal, run / "campaign.jsonl")
            payload = (await owner.suspensions(REF)).to_payload()
            assert payload["schema"] == "lab-suspension-evidence/1"
            assert payload["source"] == "core_raw" and payload["state"] == "available"
            assert payload["reason"] is None
            assert payload["suspensions"] == core_raw.campaign_suspensions(journal.parent)
    asyncio.run(read())


def test_champion_continuation_preserves_listing_prefix_and_history():
    journal, _, meta = fixture("champion-gap")
    item = meta["scenarios"]["champion-gap"]
    prefix = item["listed_prefix"]
    assert hashlib.sha256(journal.read_bytes()[:prefix["bytes"]]).hexdigest() == prefix["sha256"]
    history = writer_suspensions(journal)
    assert [row["payload"]["evidence"]["evidence_kind"] for row in history] == ["champion_rows"]
    records = list(map(json.loads, journal.read_text().splitlines()))
    assert records[-1]["event"] == "campaign_finished" and records[-1]["payload"]["outcome"] == "completed"
