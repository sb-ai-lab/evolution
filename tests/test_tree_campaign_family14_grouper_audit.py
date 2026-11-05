"""H8 Task 8: the audit of the family-14 Grouper records and the fallback chain.

Every journal here is a genuine `family14_run` engine journal (see the
`_grouper_run` module's docstring), forged afterwards with the family-14
`forge` helper, which rebases every later campaign-log locator, or by
replacing the journal's last record. Each forgery names its outcome:

- *refused on replay*: the campaign lifecycle refuses the forged record, so
  the audit reports only `suffix_refused` with the lifecycle's own message.
  Where that happens, the audit's independent check (A§7: "the audit
  re-derives the fallback chain from the grouping records") is reached by
  disabling exactly that one lifecycle rule (`_lenient`, the family-10
  precedent of monkeypatching the reducer); the audit must then report the
  forgery from the record bytes alone;
- *reported by the audit*: the lifecycle admits the journal and the audit
  reports the forgery as `precondition_failed`;
- *refused on read*: the record grammar itself refuses the forged record (a
  `role_failed` whose `error_class` is not its reason's class), so the
  journal ends before it and the audit reports the refused suffix.

Genuine journals the order rule must admit are audited clean as well: a
journal ending right after its `role_failed` (R50's crash prefix), and a
resume whose decision's consequence records come between a `role_failed` and
its owed suspension (`rebase`) or close the campaign instead (`close`).

The chain (ADR-0034; plan decisions 6, 7, 13): a scope-`all`
`grouping_finished` adds one, a fallback-free one sets it to zero, an
`uncovered` one and any `grouping_failed` leave it, and every
`campaign_suspended` sets it to zero.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from evolution.tree.campaign_schema import campaign_family_for
from evolution.tree.execution import durable_reference
from evolution.tree.storage.envelope import Envelope, SchemaError
from tests.campaign_drivers import OBSERVE, WORSE_SCORER, _cut_hook, _records
from tests.campaign_fakes import Crash
from tests.model_grouping_fakes import ScriptedClient, chat_grouper_section, use_chat_grouper
from tests.test_tree_campaign_family14_audit import audit, forge
from tests.test_tree_campaign_family14_grouper_run import (
    FULL_INVALID,
    PROVIDER_TEXT,
    _chain_client,
    _chain_run,
    _grouping_script,
    _spy_refusals,
)
from tests.test_tree_campaign_family14_run import family14_run

pytestmark = pytest.mark.slow

#: The audit's per-record finding on a stage record of the wrong kind.
WRONG_KIND = "the stage record is not a failing record of that kind"
#: The chain pass's findings (plan decision 13).
NOT_SECOND_LINK = "a content role_failed is not the second full fallback"
SECOND_LINK_FINISHED = "the second full fallback is a grouping_finished, not a role_failed"
NOT_FOLLOWED = "the role_failed is not followed by its Grouper suspension"


# -- helpers --------------------------------------------------------------------


def _journal(run) -> list[dict[str, Any]]:
    return _records(run / "campaign.jsonl")


def _rows(run) -> list[tuple[Any, str, str]]:
    return [(row["event"], row["code"], row["detail"]) for row in audit(run)["mismatches"]]


def _refused(run, message: str) -> None:
    """The lifecycle refuses the forged record on replay: the audit's only
    finding is that refusal, with the lifecycle's own message."""

    assert _rows(run) == [(None, "suffix_refused", message)]


def _lenient(monkeypatch, rule: str) -> None:
    """Disable exactly one lifecycle rule (`_refuse` with message `rule`), so the
    forged record is admitted and the audit's own check is reached."""

    original = durable_reference._refuse

    def refuse(ok: object, message: str) -> None:
        if message != rule:
            original(ok, message)

    monkeypatch.setattr(durable_reference, "_refuse", refuse)


def _failure_items(name: str) -> list[Any]:
    """One grouping's provider answers; `exception` is the Grouper's own
    unexpected exception in its role call (`role_failed / exception`)."""

    return [RuntimeError(PROVIDER_TEXT)] if name == "exception" else _grouping_script(name)


async def _suspended_run(tmp_path, monkeypatch, name: str):
    """A genuine one-round run whose `chat` grouping fails `name` and suspends."""

    use_chat_grouper(monkeypatch, ScriptedClient(_failure_items(name)))
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )
    assert snapshot.finished is None
    assert _journal(run)[-1]["event"] == "campaign_suspended"
    return run


def _forge_last(run, edit) -> None:
    """Forge the journal's last record (no later locator cites it)."""

    def forged(event: str, payload: dict[str, Any]) -> None:
        if event == "campaign_suspended":
            edit(payload)

    forge(run, forged)


# -- clean genuine journals the order rule must admit -----------------------------

#: name: (the grouping scripts, rounds).
OWED = {
    "provider": (("outage",), 1),
    "unknown": (("exception",), 1),
    "second_link": (("garbage", "garbage"), 2),
}


@pytest.mark.parametrize("name", sorted(OWED))
async def test_a_journal_ending_at_its_role_failed_audits_clean(tmp_path, monkeypatch, name):
    """R50's crash prefix: the process died after `grouping_failed(role_failed)`
    and before its suspension. The journal is admitted and owes the
    suspension to the next invocation; the audit reports nothing."""

    groupings, rounds = OWED[name]
    use_chat_grouper(
        monkeypatch,
        ScriptedClient([item for g in groupings for item in _failure_items(g)]),
    )
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_failed")
        with pytest.raises(Crash):
            await _chain_run(tmp_path, patch, rounds)
    run = tmp_path / "run"
    last = _journal(run)[-1]
    assert (last["event"], last["payload"]["code"]) == ("grouping_failed", "role_failed")
    assert audit(run)["mismatches"] == []


#: decision: (the changed run settings, the effect, the journal's tail).
RESUME_DECISIONS = {
    # A Grouper change is not comparable: `rebase` writes `identity_rebased`,
    # then the owed suspension.
    "rebase": (
        {"grouper": {"max_output_tokens": 4096}},
        ["grouping_failed", "identity_rebased", "campaign_suspended"],
    ),
    # A scorer change is comparable and offers only `close`: the accepted
    # close and the terminal it owes end the campaign instead.
    "close": (
        {"config": {**OBSERVE, "scorer": WORSE_SCORER}},
        ["grouping_failed", "authorization_applied", "campaign_finished"],
    ),
}


@pytest.mark.parametrize("effect", sorted(RESUME_DECISIONS))
async def test_a_resume_decision_after_a_role_failed_audits_clean(tmp_path, monkeypatch, effect):
    """A resume writes its decisions' consequence records first
    (`_write_consequences` precedes `resumed_resilience_cause`). Cut after
    `grouping_failed(role_failed)`; the next invocation changes a bound input
    and the operator authorizes the one effect offered. The genuine journal
    puts the consequence records between the `role_failed` and its owed
    suspension (`rebase`), or ends the campaign with the close (`close`); both
    audit clean."""

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization

    refusals = _spy_refusals(monkeypatch)
    client = use_chat_grouper(monkeypatch, ScriptedClient(_failure_items("outage")))
    changed, tail = RESUME_DECISIONS[effect]

    async def run_it(patch, change: dict[str, Any], **options: Any):
        settings = {"grouper": chat_grouper_section(**change.get("grouper", {}))}
        config = change.get("config", OBSERVE)
        return await family14_run(tmp_path, patch, config, family14_settings=settings, **options)

    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_failed")
        with pytest.raises(Crash):
            await run_it(patch, {})
    with pytest.raises(EvolveRefused) as refused:
        await run_it(monkeypatch, changed)
    assert refused.value.code == "run_identity_changed"
    assert refused.value.effects == (effect,)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "an operator decision after a Grouper failure",
        now="2026-10-12T00:00:00Z",
        effect=None if effect == "rebase" else effect,
    )

    run, _ = await run_it(monkeypatch, changed, authorizations=[decision])

    assert refusals == [] and client.script == []
    assert [r["event"] for r in _journal(run)][-3:] == tail
    assert audit(run)["mismatches"] == []


# -- a suspension that names a record of the wrong class --------------------------

#: name: (the genuine failure, the forged suspension `(reason, owner)`).
WRONG_CLASS = {
    "provider_names_unknown": ("exception", ("grouper_provider_failed", "grouper")),
    "failing_names_provider": ("outage", ("grouper_failing", "grouper")),
    "grouping_failed_names_provider": ("outage", ("grouping_failed", "campaign")),
    "grouping_failed_names_unknown": ("exception", ("grouping_failed", "campaign")),
}


@pytest.mark.parametrize("name", sorted(WRONG_CLASS))
async def test_a_suspension_naming_a_role_failed_of_another_class(tmp_path, monkeypatch, name):
    """Refused on replay ("stage_record names a record of another kind"); with
    that rule disabled the audit re-derives the class from the named record's
    own `reason` and `error_class` and reports the suspension."""

    failure, (reason, owner) = WRONG_CLASS[name]
    run = await _suspended_run(tmp_path, monkeypatch, failure)
    assert audit(run)["mismatches"] == []
    _forge_last(run, lambda payload: payload.update(reason=reason, owner=owner))

    _refused(run, "stage_record names a record of another kind")

    _lenient(monkeypatch, "stage_record names a record of another kind")
    assert _rows(run) == [("campaign_suspended", "precondition_failed", WRONG_KIND)]


async def test_a_role_failed_whose_class_is_not_its_reasons(tmp_path, monkeypatch):
    """`provider_exhausted` journaled as class `unknown`, suspended
    `grouper_failing`: the record grammar itself refuses the pair
    (`_validate_grouper_failure`), so the journal is read only up to the record
    before it and the audit reports the refused suffix."""

    run = await _suspended_run(tmp_path, monkeypatch, "outage")
    rows = _journal(run)
    rows[-2]["payload"]["error_class"] = "unknown"
    rows[-1]["payload"]["reason"] = "grouper_failing"
    with pytest.raises(SchemaError, match="^grouper error_class is not the reason's class$"):
        campaign_family_for(14).validate_payload("grouping_failed", rows[-2]["payload"])
    (run / "campaign.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    )

    assert _rows(run) == [(None, "suffix_refused", "invalid journal record")]


# -- the chain ---------------------------------------------------------------------


def _forge_round(run, event: str, number: int, **fields: Any) -> None:
    def edit(name: str, payload: dict[str, Any]) -> None:
        if name == event and payload["round"] == number:
            payload.update(fields)

    forge(run, edit)


async def test_a_content_role_failed_at_the_first_link(tmp_path, monkeypatch):
    """full, full: forge round 1's full fallback into an admitted grouping, so
    round 2's content `role_failed` is the first link. Refused on replay ("a
    content role_failed is only the second full fallback"); with that rule
    disabled the audit's own chain reports the record and its
    `grouper_failing` suspension."""

    _chain_client(monkeypatch, "garbage", "garbage")
    run, _ = await _chain_run(tmp_path, monkeypatch, 2)
    assert audit(run)["mismatches"] == []
    _forge_round(run, "grouping_finished", 1, fallback=None)

    _refused(run, "a content role_failed is only the second full fallback")

    _lenient(monkeypatch, "a content role_failed is only the second full fallback")
    # The per-record check reports first, the whole-journal chain pass after.
    assert _rows(run) == [
        ("campaign_suspended", "precondition_failed", WRONG_KIND),
        ("grouping_failed", "precondition_failed", NOT_SECOND_LINK),
    ]


async def test_a_second_link_written_as_grouping_finished(tmp_path, monkeypatch):
    """The converse: full, admitted; forge round 2's admitted grouping into a
    full fallback, so the second link is a `grouping_finished`. Refused on
    replay ("the second full fallback is written as role_failed"); with that
    rule disabled the audit's own chain reports it."""

    _chain_client(monkeypatch, "garbage", "admitted")
    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    assert audit(run)["mismatches"] == []
    _forge_round(run, "grouping_finished", 2, fallback=dict(FULL_INVALID))

    _refused(run, "the second full fallback is written as role_failed")

    _lenient(monkeypatch, "the second full fallback is written as role_failed")
    assert _rows(run) == [("grouping_finished", "precondition_failed", SECOND_LINK_FINISHED)]


# -- the order: a role_failed is followed by its suspension -------------------------


async def test_a_role_failed_followed_by_another_record(tmp_path, monkeypatch):
    """Replace the Grouper suspension with an operator stop
    (`campaign_finished{stopped, cancelled}`): the lifecycle admits it, and the
    audit reports the `role_failed` that its suspension does not follow."""

    run = await _suspended_run(tmp_path, monkeypatch, "outage")
    rows = _journal(run)
    rows[-1] = {
        **rows[-1],
        "event": "campaign_finished",
        "operation_id": uuid.uuid4().hex,
        "payload": {
            "outcome": "stopped",
            "reason": "cancelled",
            "continuation": {"kind": "linked_run", "reason": "cancelled"},
            "evidence": None,
        },
    }
    family = campaign_family_for(14)
    (run / "campaign.jsonl").write_bytes(
        b"".join(Envelope.from_payload(row, family=family).encode() for row in rows)
    )

    assert _rows(run) == [("grouping_failed", "precondition_failed", NOT_FOLLOWED)]
