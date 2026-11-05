"""Family-aware read-only audit coverage for the family-5 layered gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.campaign import CampaignFamilyUnsupported
from evolution.tree.campaign_projections import (
    CAMPAIGN_FILE,
    CampaignUnreadable,
    inspect_view,
    read_run_id,
    verify_evidence,
)
from evolution.tree.campaign_schema import (
    campaign_family_for,
)
from evolution.tree.campaign_statistics import SampleRow, score_samples_of
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.statistics import GatePlan, GateResult, gate_at_look
from evolution.tree.storage.envelope import Envelope
from tests.campaign_fakes import (
    ARTIFACT_POLICY,
    Rig,
    corpus,
)

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("reader", [inspect_view, read_run_id, verify_evidence])
def test_readers_do_not_guess_an_unknown_campaign_family(tmp_path: Path, reader) -> None:
    """Readers classify a legible unknown family without reinterpreting its meaning."""

    from tests.campaign_drivers import native14_created

    created = dict(
        schema_version=1,
        run_id="run",
        sequence=1,
        timestamp="2026-01-01T00:00:00Z",
        event="campaign_created",
        operation_id=None,
        payload=native14_created(),
    )
    created["payload"]["family_version"] = 16
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / CAMPAIGN_FILE).write_text(
        json.dumps(created, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    with pytest.raises(CampaignFamilyUnsupported) as refusal:
        reader(run_root)

    assert str(refusal.value) == "campaign_family_unsupported"
    assert (refusal.value.journaled, refusal.value.reader) == (16, 5)


def test_illegible_creation_keeps_the_existing_corrupt_prefix_classification(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / CAMPAIGN_FILE).write_bytes(b'{"not":"a campaign envelope"}\n')

    assert inspect_view(run_root).snapshot.run_id == ""
    report = verify_evidence(run_root)
    assert report["mismatches"][0]["code"] == "suffix_refused"
    with pytest.raises(CampaignUnreadable, match="campaign_unreadable"):
        read_run_id(run_root)


# -- family 6: the gate replay over excluded baseline pairs --------------


# -- task 9: the audit proves the copy; statistics are unchanged ----------


def baseline_rows(*, imported: bool) -> tuple[SampleRow, ...]:
    """Two scored `SampleRow`s, as either an executed try or a carried import."""

    return tuple(
        SampleRow(
            case_id=f"case-{index:02d}",
            sample_id="01",
            repeat=1,
            requested_seed=None,
            effective_seed=None,
            boot_id="",
            dependency_digest="dep",
            outcome="completed",
            disposition="score",
            metrics={"primary": 0.2 * index},
            critical_pass=True,
            retry_ordinal=0,
            exhausted=False,
            imported=imported,
        )
        for index in range(2)
    )


def candidate_rows() -> tuple[SampleRow, ...]:
    """The gate's other side: never imported, values that straddle the baseline's."""

    return tuple(
        SampleRow(
            case_id=f"case-{index:02d}",
            sample_id="01",
            repeat=1,
            requested_seed=None,
            effective_seed=None,
            boot_id="",
            dependency_digest="dep",
            outcome="completed",
            disposition="score",
            metrics={"primary": 0.6 + 0.2 * index},
            critical_pass=True,
            retry_ordinal=0,
            exhausted=False,
        )
        for index in range(2)
    )


def decide_look(
    baseline: tuple[SampleRow, ...], candidate: tuple[SampleRow, ...], *, look: int = 1
) -> GateResult:
    """The look `score_samples_of` + `gate_at_look` decide over two row sets.

    The minimal wiring `_check_look_evaluated` uses, without a durable log:
    `SampleRow.imported` never reaches `ScoreSample` or the gate kernel, so
    this is the composition whose result the pin below holds constant.
    """

    cases = {row.case_id: (row.case_id, "s") for row in (*baseline, *candidate)}
    parent = score_samples_of(baseline, cases, "primary", look)
    child = score_samples_of(candidate, cases, "primary", look)
    assert isinstance(parent, tuple) and isinstance(child, tuple)
    plan = GatePlan(seed_mode="none", random_seed=0, alpha_spend={look: Fraction(1, 20)})
    return gate_at_look(parent, child, plan, look)


def test_imported_rows_decide_the_gate_exactly_as_executed_rows() -> None:
    """§3.3: an imported row is scored like any other; the gate cannot tell
    the difference, so a carried baseline decides exactly as an executed one."""

    executed = baseline_rows(imported=False)
    carried = tuple(replace(row, imported=True) for row in executed)

    assert decide_look(carried, candidate_rows()) == decide_look(executed, candidate_rows())


# -- family 7: suspensions, windows and repair tries (I1 spec §5.2-§5.4) ----


def _rewrite_campaign(
    run_root: Path, edit: Callable[[str, dict[str, Any]], bool | None], *, family: int = 7
) -> None:
    """Rewrite a family-7 (or `family`) campaign log in place, rebasing its own locators.

    A forged payload changes a record's length, so every later reference into
    `campaign.jsonl` is re-pointed at the same records and re-digested; sibling
    references are untouched. An `edit` that returns `False` drops its record:
    the later records are renumbered (a forgery that cites a sequence fixes it
    itself).
    """

    original = (run_root / CAMPAIGN_FILE).read_bytes().splitlines(keepends=True)
    old = [0]
    for line in original:
        old.append(old[-1] + len(line))
    records: list[Envelope] = []

    def rebase(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                rebase(item)
            return
        if not isinstance(value, dict):
            return
        locator = value.get("locator")
        if isinstance(locator, str) and locator.startswith(f"{CAMPAIGN_FILE}#bytes="):
            start, end = (int(part) for part in locator.rsplit("=", 1)[1].split(":"))
            new = [0]
            for admitted in records:
                new.append(new[-1] + len(admitted.encode()))
            first, last = kept[old.index(start)], kept[old.index(end)]
            cited = b"".join(record.encode() for record in records[first:last])
            value["locator"] = f"{CAMPAIGN_FILE}#bytes={new[first]}:{new[last]}"
            value["content_digest"] = hashlib.sha256(cited).hexdigest()
        for item in value.values():
            rebase(item)

    # Per original record, how many records are kept before it.
    kept: list[int] = []
    for line in original:
        kept.append(len(records))
        payload = json.loads(line)
        rebase(payload["payload"])
        if edit(payload["event"], payload["payload"]) is False:
            continue
        payload["sequence"] = len(records) + 1
        records.append(Envelope.from_payload(payload, family=campaign_family_for(family)))
    kept.append(len(records))
    (run_root / CAMPAIGN_FILE).write_bytes(b"".join(record.encode() for record in records))


async def test_a_repair_try_no_suspension_named_is_reported(tmp_path: Path) -> None:
    """Carried finding 1: only a row the preceding `fault_rows` suspension
    named may receive a repair, and at most one per suspension."""

    from tests.test_tree_campaign_projections import unrepaired_fault_rig

    rig = await unrepaired_fault_rig(tmp_path, "unnamed-7")
    assert verify_evidence(rig.run_root)["mismatches"] == []
    lines = [line for line in (rig.run_root / CAMPAIGN_FILE).read_bytes().splitlines(True) if line]
    first = next(index for index, line in enumerate(lines) if b'"campaign_suspended"' in line)
    # One suspension stands, naming ordinal 0; the journal holds two repairs.
    (rig.run_root / CAMPAIGN_FILE).write_bytes(b"".join(lines[: first + 1]))

    report = verify_evidence(rig.run_root)

    found = [row for row in report["mismatches"] if row["code"] == "repair_try_mismatch"]
    assert len(found) == 1
    # The two details both say "repair": this one is the unnamed row, not the
    # ordinal bound.
    assert "named by no suspension" in found[0]["detail"]


async def test_a_truncated_fault_row_suspension_verifies_and_bounds_its_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path `_repair_detail`'s reconstruction exists for: a suspension that
    listed only its first row still names -- and still grants exactly one repair
    per -- every row it counted (I1 spec §5.2, §5.4)."""

    from evolution.tree import campaign_schema
    from tests.campaign_fakes import v8_policy
    from tests.test_tree_campaign import seed_key
    from tests.test_tree_campaign_suspension import LENIENT

    # The bound is patched for the whole test: the journal is written *and*
    # read back under it, since the record's own shape rule reads it too.
    monkeypatch.setattr(campaign_schema, "MAX_FAULT_ROWS", 1)
    monkeypatch.setattr(campaign_module, "MAX_FAULT_ROWS", 1)
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=1, attempts_per_round=1, sample_retries=0, indeterminate=LENIENT
        ),
        run_name="truncated-7",
    )
    faulted = [("case-01", "02"), ("case-02", "01")]
    for case_id, sample_id in faulted:
        rig.adapter.dispositions[seed_key(rig, case_id, sample_id)] = "invalid_request"
    await rig.run()
    for _ in range(2):
        assert (await rig.resume()).terminal == "suspended"
    evidence = [
        record["payload"]["evidence"]
        for record in rig.records()
        if record["event"] == "campaign_suspended"
    ]
    assert len(evidence) == 3
    assert all(
        (item["count"], len(item["rows"]), item["truncated"]) == (2, 1, True) for item in evidence
    )

    assert verify_evidence(rig.run_root)["mismatches"] == []

    # One suspension stands, naming ordinal 0 for both rows; the journal holds
    # two repairs of each, and the second is reconstructed as unnamed.
    lines = [line for line in (rig.run_root / CAMPAIGN_FILE).read_bytes().splitlines(True) if line]
    first = next(index for index, line in enumerate(lines) if b'"campaign_suspended"' in line)
    (rig.run_root / CAMPAIGN_FILE).write_bytes(b"".join(lines[: first + 1]))

    report = verify_evidence(rig.run_root)

    found = [row for row in report["mismatches"] if row["code"] == "repair_try_mismatch"]
    assert len(found) == 1
    assert "named by no suspension" in found[0]["detail"]


# -- family 8: the family-expanded gate scope (ADR-0021) ---------------------


# -- #78: threshold repairs (spec §3.4, §3.5, §3.7) -------------------------


def _threshold_rig(
    tmp_path: Path, name: str, indeterminate: dict[str, Any], *, schema: str = SCHEMA_V15
) -> Rig:
    from tests.campaign_fakes import v8_policy

    return Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=1, attempts_per_round=1, sample_retries=0, indeterminate=indeterminate
        ),
        cases=corpus(4),
        run_name=name,
        schema=schema,
    )


def _findings(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [row for row in report["mismatches"] if row["code"] == code]


async def test_an_exclusion_with_an_owed_threshold_repair_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#78 (spec §3.7): an exclusion over a reserved repair that never started.

    The lifecycle refuses an exclusion only while a named pair has no
    reservation after the suspension that named it; whether the reserved
    repair really started is in the measurement journal, so it is the
    audit's. The run crashes right after the reservation, and the resume is
    an executor that skips the repair -- the executor's and the planner's
    owed set patched empty -- so it excludes the pair it reserved. The audit
    reads the journal unpatched. The completion that follows is not reported
    as well: once `pairs_excluded` is written, spec §3.2's condition 1 owes
    that measurement nothing."""

    from evolution.tree import campaign_view
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign import seed_key
    from tests.test_tree_campaign_suspension import STRICT, _crash_after_reservation

    rig = _threshold_rig(tmp_path, "owed-7", STRICT)
    rig.adapter.dispositions[seed_key(rig, "case-01", "01")] = ["target_failed"]
    assert (await rig.run()).terminal == "suspended"
    with pytest.raises(Crash):
        await rig.resume(_hooks=_crash_after_reservation())
    monkeypatch.setattr(campaign_module, "threshold_owed", lambda *_: frozenset())
    monkeypatch.setattr(campaign_view, "threshold_owed", lambda *_: frozenset())
    assert (await rig.resume()).terminal == "finished"
    events = rig.events()
    assert events.index("repairs_dispatched") < events.index("pairs_excluded")
    monkeypatch.undo()

    report = verify_evidence(rig.run_root)

    assert not _findings(report, "suffix_refused")
    (found,) = _findings(report, "threshold_repair_owed")
    assert found["event"] == "pairs_excluded"
    assert found["detail"] == "1 threshold repair(s) owed"


# -- #91/#97 spec §5: held Attempts, gate closures and primary ranges -------


def _precondition_finding(context: Any, record: Envelope) -> tuple[str, str] | None:
    from evolution.tree.campaign_projections import _precondition_finding as finding

    return finding(context, record)


def _prefix_reference(rig: Rig, measurement_id: str) -> dict[str, Any]:
    name = f"measurements/{measurement_id}/events.jsonl"
    journal = (rig.run_root / name).read_bytes()
    return dict(
        version=1,
        kind="journal_prefix",
        run_id=measurement_id,
        locator=f"{name}#bytes=0:{len(journal)}",
        content_digest=hashlib.sha256(journal).hexdigest(),
    )


async def _held_and_suspended(tmp_path: Path, *, schema: str = SCHEMA_V15) -> Rig:
    """Task 7's crossing: Attempt 1's Candidate fails every pair, it is held,
    and the round barrier suspends with it open."""

    from tests.test_tree_campaign_suspension import STRICT, _fail_candidate, _held_rig

    rig = _held_rig(tmp_path, 1, STRICT, schema=schema)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")
    assert (await rig.run()).terminal == "suspended"
    assert "r01-a01" not in {
        r["payload"]["attempt_id"] for r in rig.records() if r["event"] == "attempt_closed"
    }
    assert verify_evidence(rig.run_root)["mismatches"] == []
    return rig


async def test_a_held_attempt_closed_before_its_threshold_repair_started_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#91 spec §3.2, §5: the resume reserves the held Attempt's repairs and
    crashes; the next resume is an executor that skips them -- the owed set
    patched empty -- and closes the Attempt at the round barrier. The
    lifecycle sees the reservation and admits it; whether the repairs started
    is in the measurement journal, so the audit reports the barred look. The
    closure after the bar is not checked again -- the bar is the decision,
    and nothing runs between the two (`latest_look_barred`)."""

    from evolution.tree import campaign_view
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_suspension import _crash_after_reservation

    rig = await _held_and_suspended(tmp_path)
    rig.adapter.dispositions.clear()
    with pytest.raises(Crash):
        await rig.resume(_hooks=_crash_after_reservation())
    monkeypatch.setattr(campaign_module, "threshold_owed", lambda *_: frozenset())
    monkeypatch.setattr(campaign_view, "threshold_owed", lambda *_: frozenset())
    assert (await rig.resume()).terminal == "finished"
    monkeypatch.undo()
    closed = {
        r["payload"]["attempt_id"]: r["payload"]
        for r in rig.records()
        if r["event"] == "attempt_closed"
    }
    assert closed["r01-a01"]["reason"] == "incomplete_sample"

    report = verify_evidence(rig.run_root)

    assert not _findings(report, "suffix_refused")
    found = _findings(report, "attempt_held_threshold")
    assert [row["event"] for row in found] == ["look_not_started"]
    assert all(row["detail"] == "6 threshold repair(s) owed" for row in found)
    assert {row["code"] for row in report["mismatches"]} == {"attempt_held_threshold"}


@pytest.mark.parametrize("version", ["current"])
async def test_a_baseline_completed_over_an_out_of_range_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """#97 spec §5: from 0.3.0a26 the audit reports a completion that rests on
    a scored primary outside [0, 1] -- here written by an executor whose range
    rule was patched off. The same baseline written by 0.3.0a25, when a `2.0`
    was an ordinary score, audits as it was written."""

    from evolution.tree import campaign_view
    from evolution.tree.campaign_statistics import NO_PRIMARY_RANGE
    from tests.test_tree_campaign_suspension import LENIENT, _SeedOutOfRangeScorer, v8

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.scorer = _SeedOutOfRangeScorer(scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest)
    with monkeypatch.context() as patch:
        if version == "current":
            unchecked = lambda *_: NO_PRIMARY_RANGE  # noqa: E731
            patch.setattr(campaign_module, "primary_range_for", unchecked)
            patch.setattr(campaign_view, "primary_range_for", unchecked)
        else:
            patch.setattr(campaign_module, "resolve_package_version", lambda: version)
        await rig.run(crash_after="grouping_started")
    assert "baseline_complete" in rig.events()

    report = verify_evidence(rig.run_root)

    if version != "current":
        assert report["mismatches"] == []
        return
    (found,) = report["mismatches"]
    assert (found["event"], found["code"]) == ("baseline_complete", "primary_out_of_range")
    assert "outside [0, 1]" in found["detail"]


async def test_a_truncated_out_of_range_suspension_verifies_and_bounds_its_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#97 spec §5: `scorer_metric_out_of_range` rows recompute like
    `scorer_metric_missing` ones -- a suspension that listed only its first
    row still names, and grants one repair to, every row it counted."""

    from evolution.tree import campaign_schema
    from tests.campaign_fakes import v8_policy
    from tests.test_tree_campaign_suspension import LENIENT

    monkeypatch.setattr(campaign_schema, "MAX_FAULT_ROWS", 1)
    monkeypatch.setattr(campaign_module, "MAX_FAULT_ROWS", 1)
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=1, attempts_per_round=1, sample_retries=0, indeterminate=LENIENT
        ),
        run_name="truncated-range-7",
    )
    seed = scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
    from tests.campaign_fakes import PromotingScorer

    rig.scorer = PromotingScorer(seed, out_of_range_for=frozenset({"case-01"}))
    await rig.run()
    for _ in range(2):
        assert (await rig.resume()).terminal == "suspended"
    suspended = [r["payload"] for r in rig.records() if r["event"] == "campaign_suspended"]
    assert len(suspended) == 3
    assert {p["reason"] for p in suspended} == {"scorer_metric_out_of_range"}
    assert all(
        (p["evidence"]["count"] > 1, len(p["evidence"]["rows"]), p["evidence"]["truncated"])
        == (True, 1, True)
        for p in suspended
    )

    assert verify_evidence(rig.run_root)["mismatches"] == []

    lines = [line for line in (rig.run_root / CAMPAIGN_FILE).read_bytes().splitlines(True) if line]
    first = next(index for index, line in enumerate(lines) if b'"campaign_suspended"' in line)
    (rig.run_root / CAMPAIGN_FILE).write_bytes(b"".join(lines[: first + 1]))

    report = verify_evidence(rig.run_root)

    found = _findings(report, "repair_try_mismatch")
    assert len(found) == 1
    assert "named by no suspension" in found[0]["detail"]
