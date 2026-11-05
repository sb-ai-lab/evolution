"""H7 audit of the family-14 next-baseline derivation (baseline refresh slice).

Task 1 reproduces a latent H5/H6 audit false positive: `_check_cohort_selection`
requires round r+1's `baseline_bound` to name round r's selected measurement.
An accepted `rebaseline` applied between rounds legitimately binds a fresh
`m-rNN-champion@e<k>` instead (PDR-0016, ADR-0020); the engine and the
lifecycle replay accept that journal, the audit reports it. H7 rewrites the
same next-baseline derivation for the refresh, so Task 6 fixes it (the
next round's baseline is the selection's, the refresh's or the fresh
`rebaseline` id, read from the reduced prefix) and the strict xfail is gone.

Task 6 also audits the refresh itself: genuine refresh journals audit clean,
forged ones fail at the check that owns the forged fact -- the lifecycle's
re-derivation during the audit's replay (`suffix_refused`), or the audit's own
precondition (`precondition_failed`) for what the lifecycle leaves to it. A
last group calls the audit's own checks with the lifecycle bypassed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from evolution.tree.campaign import _Campaign
from evolution.tree.campaign_epochs import fresh_baseline_id
from evolution.tree.campaign_projections import (
    _Evidence,
    _family14_next_baseline_finding,
    _precondition_finding,
    _refresh_findings,
)
from evolution.tree.campaign_schema import campaign_family_for, journal_prefix_measurement
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.authorizations import Authorization
from evolution.tree.storage.envelope import Envelope
from tests.campaign_drivers import (
    COUNTING_SCORER,
    EDITED,
    OBSERVE,
    _cut_hook,
    _events,
    _measurement_events,
    _Resettable,
)
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_audit import audit, forge
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS, _refresh_run
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_stored_run import _two_round_scorer

pytestmark = pytest.mark.slow

#: Round 1's selected measurement: stable retention keeps the stored baseline,
#: unstable retention adopts the fresh final Champion, Promotion the finalist.
SELECTED = {
    ("stable", True): "m-r00-champion",
    ("unstable", True): "m-r01-final-01-champion",
    ("stable", False): "m-r01-final-01-a01",
    ("unstable", False): "m-r01-final-01-a01",
}


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "promoted"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_rebaseline_between_rounds_after_a_selection_audits_clean(
    tmp_path, monkeypatch, stability, retained
):
    """Round 1 closes `retained` or `promoted`; a cut after its `round_closed`;
    the Target's settings change; the accepted `rebaseline` makes round 2 bind
    a fresh `m-r02-champion@e1`. The run completes, replays and audits clean."""

    flag = tmp_path / "veto.flag"
    config = {**OBSERVE, "scorer": _two_round_scorer(monkeypatch, flag, retained)}
    adapter = _Resettable(used=EDITED["used"])
    options = {
        "family14_settings": {"stability": stability},
        "adapter": adapter,
        "campaign": {"rounds": 2},
    }
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    # The cut fired before the scorer fixture's own `after_round_closed` cleanup.
    flag.unlink(missing_ok=True)
    run = tmp_path / "run"
    (closed,) = payloads(run, "round_closed")
    assert (closed["round"], closed["outcome"]) == (1, "retained" if retained else "promoted")
    (selection,) = payloads(run, "selection_decided")
    assert selection["measurement_id"] == SELECTED[stability, retained]
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, config, **options)
    assert "rebaseline" in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-12T00:00:00Z",
        effect="rebaseline",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[decision], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "rebaseline"
    baselines = {row["round"]: row["measurement_id"] for row in payloads(run, "baseline_bound")}
    assert baselines[2] == "m-r02-champion@e1"
    assert audit(run)["mismatches"] == []


# -- Task 6: the audit of the refresh count, both records and the next baseline --


def _lines(run):
    return (run / "campaign.jsonl").read_bytes().splitlines(keepends=True)


def _rows(run):
    return [json.loads(line) for line in _lines(run)]


def _at(rows, event, number):
    """The index of round `number`'s first `event`."""
    return next(
        i
        for i, row in enumerate(rows)
        if row["event"] == event and row["payload"].get("round") == number
    )


def _campaign_reference(run, index):
    """A `journal_record` reference to campaign record `index`, by its bytes."""

    lines = _lines(run)
    start = sum(len(line) for line in lines[:index])
    data = lines[index]
    return {
        "kind": "journal_record",
        "locator": f"campaign.jsonl#bytes={start}:{start + len(data)}",
        "content_digest": hashlib.sha256(data).hexdigest(),
        "run_id": _rows(run)[index]["run_id"],
        "version": 1,
    }


def _shorter_head(run, measurement_id):
    """A coherent `journal_prefix` of `measurement_id`'s journal one record short."""

    lines = _measurement_events(run, measurement_id).read_bytes().splitlines(keepends=True)
    data = b"".join(lines[:-1])
    return {
        "kind": "journal_prefix",
        "locator": f"measurements/{measurement_id}/events.jsonl#bytes=0:{len(data)}",
        "content_digest": hashlib.sha256(data).hexdigest(),
        "run_id": measurement_id,
        "version": 1,
    }


def _splice(run, transform):
    """Rewrite the campaign log from an edited record list (reorder, insert, drop).

    Only for forgeries the lifecycle refuses at the first changed record: every
    record before it keeps its bytes, so their references stay exact, and the
    refused record ends the admitted prefix. Sequences are renumbered.
    """

    rows = transform(_rows(run))
    data = b""
    for sequence, row in enumerate(rows, start=1):
        row["sequence"] = sequence
        data += Envelope.from_payload(row, family=campaign_family_for(14)).encode()
    (run / "campaign.jsonl").write_bytes(data)


def _forge_record(run, event, number, edit):
    """`forge` (H3's re-sealing rewrite) round `number`'s first `event` payload."""

    done = []

    def rewrite(name, payload):
        if name == event and payload.get("round") == number and not done:
            done.append(name)
            return edit(payload)
        return None

    forge(run, rewrite)
    assert done


def _refused(run, detail):
    """The lifecycle's own refusal of the forged record, reported by the audit."""
    found = audit(run)["mismatches"]
    assert [(row["event"], row["code"]) for row in found] == [(None, "suffix_refused")], found
    assert detail in found[0]["detail"], found


def _precondition(run, event, detail):
    """Exactly one finding: the audit's own precondition check of `event`."""
    found = audit(run)["mismatches"]
    assert [(row["event"], row["code"]) for row in found] == [(event, "precondition_failed")], found
    assert detail in found[0]["detail"], found


# Genuine clean journals ----------------------------------------------------


@pytest.mark.parametrize("refresh", [1, 2])
async def test_genuine_stalled_refresh_runs_audit_clean(tmp_path, monkeypatch, refresh):
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, refresh)
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 3
    assert payloads(run, "baseline_refresh_bound")
    result = audit(run)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []


async def _stable_retained_run(tmp_path, monkeypatch, refresh):
    """Two `stable` rounds; round 1 closes `retained` on the stored `m-r00-champion`."""
    flag = tmp_path / "veto.flag"
    config = {**OBSERVE, "scorer": _two_round_scorer(monkeypatch, flag, True)}
    return await family14_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": "stable", "refresh": refresh},
        campaign={"rounds": 2},
    )


async def test_genuine_stable_retained_refresh_audits_clean(tmp_path, monkeypatch):
    """Round 1 closes `retained` under `stable`, so its selection keeps the
    stored `m-r00-champion`; with N = 1 round 2 refreshes it. The next round's
    `baseline_bound` names `m-r02-refresh`, not the selection's measurement,
    and `_check_cohort_selection` accepts it because the refresh replaces
    exactly that measurement. Round 2's stable final cites the refresh as its
    stored Champion."""

    run, snapshot = await _stable_retained_run(tmp_path, monkeypatch, 1)
    assert snapshot.finished["outcome"] == "completed"
    closed = payloads(run, "round_closed")
    assert closed[0]["outcome"] == "retained"
    assert payloads(run, "selection_decided")[0]["measurement_id"] == CHAMPION0
    (bound,) = payloads(run, "baseline_refresh_bound")
    assert (bound["round"], bound["replaced"]["measurement_id"]) == (2, CHAMPION0)
    baselines = {row["round"]: row["measurement_id"] for row in payloads(run, "baseline_bound")}
    assert baselines == {1: CHAMPION0, 2: "m-r02-refresh"}
    finals = [row for row in payloads(run, "final_measurement_bound") if row["round"] == 2]
    assert finals and all(
        row["stored_champion"]["measurement_id"] == "m-r02-refresh" for row in finals
    )
    result = audit(run)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []


async def test_genuine_continue_freezing_a_complete_refresh_before_its_completion_audits_clean(
    tmp_path, monkeypatch
):
    """Genuine: a cut right after round 2's refresh `baseline_complete` (a
    test-only append wrapper; there is no production hook there), then an
    accepted `continue`. The complete `m-r02-refresh` is frozen by the change
    and keeps its id; `baseline_refresh_completed` names it after the change,
    which `_FROZEN_REUSE` admits like a later `baseline_complete`."""

    adapter = _Resettable(used=EDITED["used"])
    options = {
        "family14_settings": {"refresh": 1},
        "adapter": adapter,
        "campaign": {"rounds": 2},
    }
    original = _Campaign.append

    def append(self, event, payload, operation_id=None):
        written = original(self, event, payload, operation_id)
        if event == "baseline_complete" and payload["measurement_id"] == "m-r02-refresh":
            raise Crash("power cut")
        return written

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "append", append)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    run = tmp_path / "run"
    assert [row["event"] for row in _rows(run)][-1] == "baseline_complete"
    assert not payloads(run, "baseline_refresh_completed")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    assert "continue" in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-12T00:00:00Z",
        effect="continue",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, NO_FINALISTS, authorizations=[decision], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "continue"
    events = [row["event"] for row in _rows(run)]
    assert events.index("authorization_applied") < events.index("baseline_refresh_completed")
    (completed,) = payloads(run, "baseline_refresh_completed")
    assert completed["measurement_id"] == "m-r02-refresh"
    assert audit(run)["mismatches"] == []


# Forged `baseline_refresh_bound` ------------------------------------------
# Each forgery is a genuine N = 1 (or N = 2) journal rewritten with coherent
# outer references. The lifecycle re-derives every value it can and refuses
# the record (the audit reports that refusal as `suffix_refused` with the
# lifecycle's rule); the cited closure is the audit's own fact.


async def test_forged_counted_rounds_are_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    _forge_record(
        run,
        "baseline_refresh_bound",
        3,
        lambda payload: payload.update(counted_rounds={"first": 1, "last": 2}),
    )
    _refused(run, "the refresh's counted rounds are not the derived count")


async def test_forged_after_rounds_is_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    _forge_record(
        run,
        "baseline_refresh_bound",
        3,
        lambda payload: payload.update(baseline_refresh_after_rounds=2),
    )
    _refused(run, "the refresh names the policy's baseline_refresh_after_rounds")


async def test_forged_replaced_measurement_is_refused(tmp_path, monkeypatch):
    """Round 3 claims to replace round 1's baseline, citing that baseline's
    real complete prefix (a coherent reference of another measurement)."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    (first,) = [row for row in payloads(run, "baseline_complete") if row["round"] == 1]
    other = first["evidence"][0]

    def edit(payload):
        payload["replaced"] = {
            "measurement_id": CHAMPION0,
            "prefix_digest": other["content_digest"],
        }
        payload["evidence"][0] = other

    _forge_record(run, "baseline_refresh_bound", 3, edit)
    _refused(run, "the refresh replaces the round's derived baseline")


async def test_forged_replaced_digest_is_refused(tmp_path, monkeypatch):
    """The replaced baseline's own journal, cited one record short: an intact
    reference whose digest is not the complete baseline's."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    head = _shorter_head(run, "m-r02-refresh")

    def edit(payload):
        payload["replaced"]["prefix_digest"] = head["content_digest"]
        payload["evidence"][0] = head

    _forge_record(run, "baseline_refresh_bound", 3, edit)
    _refused(run, "the refresh names the replaced baseline's prefix digest")


@pytest.mark.parametrize("refresh,cited", [(1, 1), (2, 1)], ids=["another-round", "first-not-last"])
async def test_closure_evidence_naming_another_rounds_close_fails_its_check(
    tmp_path, monkeypatch, refresh, cited
):
    """The lifecycle leaves `evidence[1]` to the audit: round 3's refresh must
    cite the `round_closed` of its last counted round (2), not round 1's --
    whether N = 1 (another round) or N = 2 (the first counted round)."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, refresh)
    rows = _rows(run)
    reference = _campaign_reference(run, _at(rows, "round_closed", cited))
    _forge_record(
        run,
        "baseline_refresh_bound",
        3,
        lambda payload: payload["evidence"].__setitem__(1, reference),
    )
    _precondition(
        run, "baseline_refresh_bound", "does not cite the round_closed of its last counted round"
    )


async def test_closure_evidence_naming_another_record_fails_its_check(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    rows = _rows(run)
    reference = _campaign_reference(run, _at(rows, "grouping_started", 2))
    _forge_record(
        run,
        "baseline_refresh_bound",
        3,
        lambda payload: payload["evidence"].__setitem__(1, reference),
    )
    _precondition(
        run, "baseline_refresh_bound", "does not cite the round_closed of its last counted round"
    )


async def test_a_refresh_one_round_early_is_refused(tmp_path, monkeypatch):
    """N = 2: a refresh inserted at round 2, where the count is 1."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 2)

    def transform(rows):
        due = rows[_at(rows, "baseline_refresh_bound", 3)]
        early = {**due, "payload": {**due["payload"]}}
        early["payload"].update(
            round=2,
            measurement_id="m-r02-refresh",
            counted_rounds={"first": 1, "last": 1},
        )
        early["payload"]["evidence"] = [
            rows[_at(rows, "baseline_complete", 1)]["payload"]["evidence"][0],
            _campaign_reference(run, _at(rows, "round_closed", 1)),
        ]
        at = _at(rows, "baseline_bound", 2)
        return [*rows[:at], early, *rows[at:]]

    _splice(run, transform)
    _refused(run, "no refresh is due for the round")


async def test_a_refresh_one_round_late_is_refused(tmp_path, monkeypatch):
    """N = 1: round 2's refresh written after round 2's `baseline_bound`."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)

    def transform(rows):
        at = _at(rows, "baseline_refresh_bound", 2)
        refresh = rows.pop(at)
        bound = _at(rows, "baseline_bound", 2)
        return [*rows[: bound + 1], refresh, *rows[bound + 1 :]]

    _splice(run, transform)
    _refused(run, "the refresh measurement needs its baseline_refresh_bound")


async def test_a_missing_refresh_where_due_is_refused(tmp_path, monkeypatch):
    """N = 1: round 2's refresh dropped, its baseline names the replaced one."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)

    def transform(rows):
        rows.pop(_at(rows, "baseline_refresh_bound", 2))
        rows[_at(rows, "baseline_bound", 2)]["payload"]["measurement_id"] = CHAMPION0
        return rows

    _splice(run, transform)
    _refused(run, "a due refresh is bound before the round's baseline")


async def test_a_refresh_under_n0_is_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 0)
    _splice(run, _n0_refresh(run))
    _refused(run, "the policy refreshes no baseline")


# Forged `baseline_refresh_completed` ---------------------------------------


async def test_a_completion_citing_another_prefix_is_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    head = _shorter_head(run, "m-r03-refresh")
    _forge_record(
        run,
        "baseline_refresh_completed",
        3,
        lambda payload: payload["evidence"].__setitem__(0, head),
    )
    _refused(run, "the refresh completion cites the round's baseline reference")


async def test_a_completion_before_baseline_complete_is_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)

    def transform(rows):
        completed = rows.pop(_at(rows, "baseline_refresh_completed", 3))
        at = _at(rows, "baseline_complete", 3)
        return [*rows[:at], completed, *rows[at:]]

    _splice(run, transform)
    _refused(run, "the refresh's baseline is not complete")


async def test_a_completion_after_grouping_is_refused(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)

    def transform(rows):
        completed = rows.pop(_at(rows, "baseline_refresh_completed", 3))
        at = _at(rows, "grouping_started", 3)
        return [*rows[: at + 1], completed, *rows[at + 1 :]]

    _splice(run, transform)
    _refused(run, "the round's refresh is not completed")


async def test_an_uncompleted_refresh_whose_round_grouped_is_refused(tmp_path, monkeypatch):

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    forge(
        run,
        lambda event, payload: (
            False if event == "baseline_refresh_completed" and payload["round"] == 3 else None
        ),
    )
    _refused(run, "the round's refresh is not completed")


# The audit's own checks, the lifecycle bypassed ------------------------------
# Unit level: each forged record is substituted into an `_Evidence` built from
# a genuine journal, so the lifecycle never sees it, and the audit's own
# re-derivation is called directly. This proves the audit does not merely
# repeat a lifecycle refusal: each check reaches its own finding.


def _bypassed(run, transform):

    rows = transform(_rows(run))
    records = []
    for sequence, row in enumerate(rows, start=1):
        row["sequence"] = sequence
        records.append(Envelope.from_payload(row, family=campaign_family_for(14)))
    return _Evidence(run, tuple(records), {}, family_version=14)


def _record_finding(context, event, number):

    (record,) = [
        record
        for record in context.records
        if record.event == event and record.payload["round"] == number
    ]
    return _precondition_finding(context, record)


def _edited(event, number, edit):
    def transform(rows):
        edit(rows[_at(rows, event, number)]["payload"])
        return rows

    return transform


@pytest.mark.parametrize(
    "field,detail",
    [
        ("counted", "counted rounds are not the count re-derived at its prefix"),
        ("after-rounds", "names another baseline_refresh_after_rounds than the policy in force"),
        ("replaced-id", "replaces another measurement than the derived baseline"),
        ("replaced-digest", "the stored reference is not the round's complete baseline"),
    ],
)
async def test_the_bound_check_rederives_the_due_refresh(tmp_path, monkeypatch, field, detail):
    """Each forged value is coherent with the record grammar (`evidence[0]`
    matches `replaced`), so only the audit's re-derivation can refuse it."""

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    clean = _bypassed(run, lambda rows: rows)
    assert _record_finding(clean, "baseline_refresh_bound", 3) is None
    (first,) = [row for row in payloads(run, "baseline_complete") if row["round"] == 1]
    other, head = first["evidence"][0], _shorter_head(run, "m-r02-refresh")

    def edit(payload):
        if field == "counted":
            payload["counted_rounds"] = {"first": 1, "last": 2}
        elif field == "after-rounds":
            payload["baseline_refresh_after_rounds"] = 2
        else:
            cited = other if field == "replaced-id" else head
            payload["replaced"] = {
                "measurement_id": journal_prefix_measurement(cited["locator"]),
                "prefix_digest": cited["content_digest"],
            }
            payload["evidence"][0] = cited

    context = _bypassed(run, _edited("baseline_refresh_bound", 3, edit))
    code, found = _record_finding(context, "baseline_refresh_bound", 3)
    assert code == "precondition_failed" and detail in found, found


async def test_the_bound_check_refuses_a_refresh_that_is_not_due(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 2)

    def transform(rows):
        due = rows[_at(rows, "baseline_refresh_bound", 3)]
        early = {**due, "payload": {**due["payload"]}}
        early["payload"].update(round=2, measurement_id="m-r02-refresh")
        at = _at(rows, "baseline_bound", 2)
        return [*rows[:at], early, rows[at]]

    context = _bypassed(run, transform)
    code, found = _record_finding(context, "baseline_refresh_bound", 2)
    assert (code, found) == ("precondition_failed", "no refresh is due for the round at its prefix")


async def test_the_bound_check_refuses_a_refresh_under_n0(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 0)
    context = _bypassed(run, _n0_refresh(run))
    code, found = _record_finding(context, "baseline_refresh_bound", 2)
    assert (code, found) == ("precondition_failed", "the policy in force refreshes no baseline")


def _n0_refresh(run):
    """Insert a well-shaped round-2 refresh into an N = 0 journal, end at its baseline."""

    def transform(rows):
        complete = rows[_at(rows, "baseline_complete", 1)]["payload"]["evidence"][0]
        closure = _campaign_reference(run, _at(rows, "round_closed", 1))
        at = _at(rows, "baseline_bound", 2)
        bound = rows[at]
        refresh = {
            **bound,
            "event": "baseline_refresh_bound",
            "payload": {
                "round": 2,
                "measurement_id": "m-r02-refresh",
                "baseline_refresh_after_rounds": 1,
                "counted_rounds": {"first": 1, "last": 1},
                "replaced": {
                    "measurement_id": CHAMPION0,
                    "prefix_digest": complete["content_digest"],
                },
                "evidence": [complete, closure],
            },
        }
        return [*rows[:at], refresh, bound]

    return transform


async def test_the_completion_check_resolves_its_baseline_complete(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    clean = _bypassed(run, lambda rows: rows)
    assert _record_finding(clean, "baseline_refresh_completed", 3) is None
    head = _shorter_head(run, "m-r03-refresh")
    context = _bypassed(
        run,
        _edited("baseline_refresh_completed", 3, lambda p: p["evidence"].__setitem__(0, head)),
    )
    code, found = _record_finding(context, "baseline_refresh_completed", 3)
    assert code == "precondition_failed"
    assert "does not cite its round's baseline_complete prefix" in found

    def early(rows):
        completed = rows.pop(_at(rows, "baseline_refresh_completed", 3))
        at = _at(rows, "baseline_complete", 3)
        return [*rows[:at], completed]

    code, found = _record_finding(_bypassed(run, early), "baseline_refresh_completed", 3)
    assert code == "precondition_failed"
    assert "precedes its round's baseline_complete" in found


def _whole(context):

    return [(row["event"], row["code"], row["detail"]) for row in _refresh_findings(context)]


async def test_refresh_findings_are_empty_on_genuine_journals(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)
    assert _whole(_bypassed(run, lambda rows: rows)) == []


async def test_refresh_findings_rederive_the_count_after_every_round(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 1)

    def missing(rows):
        rows.pop(_at(rows, "baseline_refresh_bound", 3))
        rows[_at(rows, "baseline_bound", 3)]["payload"]["measurement_id"] = "m-r02-refresh"
        return rows

    assert _whole(_bypassed(run, missing)) == [
        ("baseline_bound", "precondition_failed", "round 3 owes a refresh before its baseline")
    ]

    def late(rows):
        refresh = rows.pop(_at(rows, "baseline_refresh_bound", 3))
        bound = _at(rows, "baseline_bound", 3)
        return [*rows[: bound + 1], refresh, *rows[bound + 1 :]]

    assert _whole(_bypassed(run, late)) == [
        ("baseline_bound", "precondition_failed", "round 3 owes a refresh before its baseline")
    ]

    def after_grouping(rows):
        completed = rows.pop(_at(rows, "baseline_refresh_completed", 3))
        at = _at(rows, "grouping_started", 3)
        return [*rows[: at + 1], completed, *rows[at + 1 :]]

    assert _whole(_bypassed(run, after_grouping)) == [
        (
            "baseline_refresh_completed",
            "precondition_failed",
            "round 3's refresh completes after its round grouped or selected its portion",
        )
    ]

    def uncompleted(rows):
        rows.pop(_at(rows, "baseline_refresh_completed", 3))
        return rows

    assert _whole(_bypassed(run, uncompleted)) == [
        (
            "baseline_refresh_bound",
            "precondition_failed",
            "round 3's refresh is left uncompleted while its round grouped or selected its portion",
        )
    ]


async def test_refresh_findings_refuse_a_refresh_that_is_not_due(tmp_path, monkeypatch):
    run, _ = await _refresh_run(tmp_path, monkeypatch, 2)

    def early(rows):
        due = rows[_at(rows, "baseline_refresh_bound", 3)]
        refresh = {**due, "payload": {**due["payload"], "round": 2}}
        refresh["payload"]["measurement_id"] = "m-r02-refresh"
        at = _at(rows, "baseline_bound", 2)
        return [*rows[:at], refresh, rows[at]]

    assert _whole(_bypassed(run, early)) == [
        ("baseline_refresh_bound", "precondition_failed", "round 2's refresh is not due")
    ]
    run0 = tmp_path / "n0"
    run0.mkdir()
    run0, _ = await _refresh_run(run0, monkeypatch, 0)
    assert _whole(_bypassed(run0, _n0_refresh(run0))) == [
        (
            "baseline_refresh_bound",
            "precondition_failed",
            "the policy refreshes no baseline, yet round 2 journals a refresh",
        )
    ]


async def test_genuine_rebaseline_superseding_a_bound_refresh_audits_clean(tmp_path, monkeypatch):
    """Genuine: a cut at `after_baseline_refresh_bound` (round 2, N = 1), then
    an accepted `rebaseline`. Round 2 becomes the `rebaseline` round and binds
    the fresh `m-r02-champion@e1` (plan decision 7); its refresh stays
    uncompleted as superseded. `_refresh_findings` derives that from the
    reduced prefix and reports nothing."""

    adapter = _Resettable(used=EDITED["used"])
    options = {
        "family14_settings": {"refresh": 1},
        "adapter": adapter,
        "campaign": {"rounds": 2},
    }
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_baseline_refresh_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    run = tmp_path / "run"
    assert [row["event"] for row in _rows(run)][-1] == "baseline_refresh_bound"
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    assert "rebaseline" in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-12T00:00:00Z",
        effect="rebaseline",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, NO_FINALISTS, authorizations=[decision], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    baselines = {row["round"]: row["measurement_id"] for row in payloads(run, "baseline_bound")}
    assert baselines == {1: CHAMPION0, 2: "m-r02-champion@e1"}
    assert [row["round"] for row in payloads(run, "baseline_refresh_bound")] == [2]
    assert not payloads(run, "baseline_refresh_completed")
    assert audit(run)["mismatches"] == []


# Audit behavior ---------------------------------------------------------------

#: Every case passes, so no baseline has a failure: each round selects a
#: `reason="none"` focus portion without grouping and closes `clean`.
CLEAN = {**OBSERVE, "scorer": COUNTING_SCORER.replace('"passed": passed', '"passed": True')}


async def _clean_refresh_run(tmp_path, monkeypatch):
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, CLEAN, family14_settings={"refresh": 1}, campaign={"rounds": 2}
    )
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["clean", "clean"]
    assert not payloads(run, "grouping_started")
    assert [p["reason"] for p in payloads(run, "focus_portion_selected")] == ["none", "none"]
    assert [p["round"] for p in payloads(run, "baseline_refresh_completed")] == [2]
    return run, snapshot


async def test_genuine_clean_refresh_run_audits_clean(tmp_path, monkeypatch):
    """Genuine: a `clean` close keeps the baseline and counts, so N = 1
    refreshes round 2; with no failure the refresh completes before the
    round's `reason="none"` focus portion, and nothing groups."""

    run, snapshot = await _clean_refresh_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] == "completed"
    events = [row["event"] for row in _rows(run)]
    assert events.index("baseline_refresh_completed") < max(
        i for i, event in enumerate(events) if event == "focus_portion_selected"
    )
    assert audit(run)["mismatches"] == []


async def test_refresh_findings_treat_a_none_portion_as_the_grouped_point(tmp_path, monkeypatch):
    """Lifecycle bypassed. The no-failing path selects its portion without a
    `grouping_started`; the lifecycle gates that portion on the completion
    (`_check_refresh_completed`), so the audit uses it as the grouped point."""

    run, _ = await _clean_refresh_run(tmp_path, monkeypatch)

    def completed_after_portion(rows):
        completed = rows.pop(_at(rows, "baseline_refresh_completed", 2))
        at = _at(rows, "focus_portion_selected", 2)
        return [*rows[: at + 1], completed, *rows[at + 1 :]]

    assert _whole(_bypassed(run, completed_after_portion)) == [
        (
            "baseline_refresh_completed",
            "precondition_failed",
            "round 2's refresh completes after its round grouped or selected its portion",
        )
    ]

    def uncompleted_then_ends(rows):
        rows.pop(_at(rows, "baseline_refresh_completed", 2))
        return rows[: _at(rows, "focus_portion_selected", 2) + 1]

    assert _whole(_bypassed(run, uncompleted_then_ends)) == [
        (
            "baseline_refresh_bound",
            "precondition_failed",
            "round 2's refresh is left uncompleted while its round grouped or selected its portion",
        )
    ]


_NEXT = "the next round binds another baseline measurement"
_REPLACED = "the next round's refresh replaces another measurement than the selection's"


def _rebound(name):
    """Round 2's `baseline_bound` renamed to `name`; the journal ends there."""

    def transform(rows):
        at = _at(rows, "baseline_bound", 2)
        rows[at]["payload"]["measurement_id"] = name
        return rows[: at + 1]

    return transform


async def test_the_next_baseline_check_refuses_a_fresh_id_without_a_rebaseline(
    tmp_path, monkeypatch
):
    """Lifecycle bypassed. N = 0: round 2 binds `m-r02-champion@e1`, the fresh
    `rebaseline` id, although round 2 is no `rebaseline` round in the reduced
    prefix. The finding is reported on round 1's `selection_decided`."""

    run, _ = await _stable_retained_run(tmp_path, monkeypatch, 0)
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == CHAMPION0
    assert _whole(_bypassed(run, _rebound(CHAMPION0))) == []
    found = _refresh_findings(_bypassed(run, _rebound("m-r02-champion@e1")))
    assert [(row["event"], row["code"], row["detail"]) for row in found] == [
        ("selection_decided", "precondition_failed", _NEXT)
    ]
    (selection,) = [
        row for row in _events(run, "selection_decided") if row["payload"]["round"] == 1
    ]
    assert found[0]["locator"] == selection["payload"]["evidence"][0]["locator"]
    assert found[0]["sequence"] == selection["sequence"]


async def test_the_next_baseline_check_refuses_the_selection_id_after_a_refresh(
    tmp_path, monkeypatch
):
    """Lifecycle bypassed. N = 1: a refresh for round 2 precedes its
    `baseline_bound`, which names the selection's `m-r00-champion` anyway."""

    run, _ = await _stable_retained_run(tmp_path, monkeypatch, 1)
    assert _whole(_bypassed(run, _rebound("m-r02-refresh"))) == []
    assert _whole(_bypassed(run, _rebound(CHAMPION0))) == [
        ("selection_decided", "precondition_failed", _NEXT)
    ]


async def test_the_next_baseline_rule_reads_the_reduced_prefix(tmp_path, monkeypatch):
    """Pure level, on the genuine reduced prefix before round 2's
    `baseline_bound` (stable retained, N = 1). A refresh whose `replaced` is
    not the selection's measurement is refused -- the lifecycle cannot admit
    one, so the entry is substituted in the snapshot -- and the fresh id is
    accepted only when the prefix makes round 2 the `rebaseline` round."""

    run, _ = await _stable_retained_run(tmp_path, monkeypatch, 1)
    context = _bypassed(run, lambda rows: rows)
    before = context.snapshot_before(_at(_rows(run), "baseline_bound", 2))
    (entry,) = before.refreshes
    assert _family14_next_baseline_finding(before, 2, CHAMPION0, "m-r02-refresh") is None
    other = {**dict(entry), "replaced": {**dict(entry["replaced"]), "measurement_id": "m-x"}}
    forged = replace(before, refreshes=(other,))
    assert _family14_next_baseline_finding(forged, 2, CHAMPION0, "m-r02-refresh") == _REPLACED
    fresh = fresh_baseline_id(2, len(before.epochs) - 1)
    assert _family14_next_baseline_finding(before, 2, CHAMPION0, fresh) == _NEXT
    rebaseline = replace(before, rebaseline_from_round=2)
    assert _family14_next_baseline_finding(rebaseline, 2, CHAMPION0, fresh) is None
    assert _family14_next_baseline_finding(rebaseline, 2, CHAMPION0, "m-r02-refresh") == _NEXT
