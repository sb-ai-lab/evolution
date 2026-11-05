"""H8 Task 7: family-14 Grouper recovery at every row of A§5.4.

Every run is a genuine `family14_run` engine invocation (see the `_run`
module's docstring), cut and then resumed with the same command in the same
run directory. A resumed run audits clean and, where the scenario has one, is
compared with an uncut control of the same scenario by event order and naming
keys (A§10.4), minus the records the interruption itself owes.

How each cut is produced. A `Crash` (`RuntimeError`) raised inside a
family-14 `chat` role call is the Grouper's own `role_failed / exception`
(F0 R47), not a process death, so every cut here fires outside the role-call
wrap: `_cut_hook` raises `Crash` from a campaign hook (`after_grouping_started`,
`after_grouping_failed`, `after_grouping_finished`), which runs after the
record it follows is durable and outside `_chat_role_call`. The one mid-call
cut raises `_PowerCut`, a `BaseException`, from the scripted provider: no
`except Exception` holds it, so it leaves the engine as a process death would,
with the `grouping_started` open.

Rows (A§5.4; ADR-0019, ADR-0034; plan decisions 6, 10, 11, 16):

- during a grouping operation: the resume settles it `grouping_failed
  (interrupted)` and groups again under a new id on the same baseline;
- after `grouping_failed(role_failed)`, before `campaign_suspended`: the resume
  writes exactly the owed suspension, naming that record, and nothing else --
  through `resumed_resilience_cause` and, with that path removed, through the
  planner's grouping branch;
- after a Grouper suspension: the same command groups again on the same
  baseline; a repeat of the failure suspends again, a recovery completes;
- after `grouping_finished` with a fallback: the result stands, nothing groups
  again for that baseline, and the chain it began is still derived;
- after the second-link suspension: the chain starts empty, so one full
  fallback is a caveat again.
"""

from __future__ import annotations

from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.campaign_schema import GROUPER_SUSPENSIONS
from evolution.tree.campaign_view import FinishCampaign
from tests.campaign_drivers import OBSERVE, _cut_hook, _PowerCut, _records
from tests.campaign_fakes import Crash
from tests.model_grouping_fakes import (
    ScriptedClient,
    chat_grouper_section,
    group_everything,
    use_chat_grouper,
)
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_grouper_run import (
    FULL_INVALID,
    PROVIDER_TEXT,
    _assert_second_link,
    _caveat_rounds,
    _chain_client,
    _chain_run,
    _events,
    _fallbacks,
    _grouping_script,
    _spy_refusals,
)
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_visibility_recovery import _assert_same_structure

pytestmark = pytest.mark.slow


# -- helpers --------------------------------------------------------------------


def _journal(run) -> list[dict[str, Any]]:
    return _records(run / "campaign.jsonl")


def _of(run, event: str) -> list[dict[str, Any]]:
    return [record for record in _journal(run) if record["event"] == event]


def _client(monkeypatch, *items: Any) -> ScriptedClient:
    return use_chat_grouper(monkeypatch, ScriptedClient(list(items)))


async def _chat_run(tmp_path, monkeypatch, config=OBSERVE, **options: Any):
    """A genuine one-round family-14 run with a `chat` Grouper."""

    grouper = chat_grouper_section(**options.pop("grouper", {}))
    return await family14_run(
        tmp_path, monkeypatch, config, family14_settings={"grouper": grouper}, **options
    )


async def _cut(run_coroutine, monkeypatch, hook: str, *, call: int = 1):
    """Run `run_coroutine(patch)` until `_cut_hook` cuts it at `hook`."""

    with monkeypatch.context() as patch:
        seen = _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await run_coroutine(patch)
    assert len(seen) == call


async def _uncut(tmp_path, monkeypatch, run, *groupings: str, **options: Any):
    """An uncut run of the same scenario under `tmp_path/uncut`, its provider
    scripted with `groupings` (names of `_grouping_script`)."""

    control = tmp_path / "uncut"
    control.mkdir()
    client = _scripted(monkeypatch, *groupings)
    plain, snapshot = await run(control, monkeypatch, **options)
    assert client.script == []
    return plain, snapshot


def _failure_items(name: str) -> list[Any]:
    """`_grouping_script(name)`, plus `exception`: the Grouper's own unexpected
    exception in its role call (F0 R47), journaled `role_failed / exception`."""

    if name == "exception":
        return [RuntimeError(PROVIDER_TEXT)]
    return _grouping_script(name)


def _scripted(monkeypatch, *groupings: str) -> ScriptedClient:
    return _client(monkeypatch, *[item for name in groupings for item in _failure_items(name)])


def _assert_clean(run, snapshot) -> None:
    """The journal replays alone to `snapshot` and audits clean.

    The replay (`journal_consistent`) reduces the campaign journal with a
    fresh lifecycle and must agree with the engine's snapshot on the
    terminal and the derived Grouper chain; the audit (Task 8) re-derives the
    chain and each Grouper suspension's class from the record bytes and
    reports nothing.
    """

    replayed = journal_consistent(run)
    assert replayed.finished == snapshot.finished
    assert replayed.grouper_chain == snapshot.grouper_chain
    assert audit(run)["mismatches"] == []


# -- Row 1: interrupted during a grouping operation ----------------------------


@pytest.mark.parametrize("cut", ["after_started", "mid_call"])
async def test_a_grouping_cut_mid_operation_is_interrupted_and_runs_again(
    tmp_path, monkeypatch, cut
):
    """`after_started`: `_cut_hook` at `after_grouping_started`, before the
    role call. `mid_call`: the provider raises `_PowerCut` during the map
    request, so the role call itself never returns. Either way the resume
    writes `grouping_failed(interrupted)` for the open grouping, then a new
    `grouping_started` with a new id on the same `baseline_binding`."""

    refusals = _spy_refusals(monkeypatch)
    if cut == "after_started":
        client = _client(monkeypatch, group_everything())
        await _cut(lambda patch: _chat_run(tmp_path, patch), monkeypatch, "after_grouping_started")
        assert client.requests == []
    else:

        def power_cut(_request):
            raise _PowerCut("power cut")

        client = _client(monkeypatch, power_cut, group_everything())
        with pytest.raises(_PowerCut):
            await _chat_run(tmp_path, monkeypatch)
        assert len(client.requests) == 1
    run = tmp_path / "run"
    (opened,) = _of(run, "grouping_started")
    assert _journal(run)[-1] == opened

    run, snapshot = await _chat_run(tmp_path, monkeypatch)

    assert refusals == [] and client.script == []
    first, second = _of(run, "grouping_started")
    assert first == opened
    (interrupted,) = _of(run, "grouping_failed")
    assert interrupted["operation_id"] == first["operation_id"]
    assert interrupted["payload"] == {
        "round": 1,
        "grouping_id": first["payload"]["grouping_id"],
        "code": "interrupted",
        "reads": interrupted["payload"]["reads"],
    }
    records = _journal(run)
    assert records.index(interrupted) < records.index(second)
    assert second["payload"]["grouping_id"] != first["payload"]["grouping_id"]
    assert second["operation_id"] != first["operation_id"]
    assert {k: v for k, v in second["payload"].items() if k != "grouping_id"} == {
        k: v for k, v in first["payload"].items() if k != "grouping_id"
    }
    (finished,) = payloads(run, "grouping_finished")
    assert finished["grouping_id"] == second["payload"]["grouping_id"]
    assert finished["fallback"] is None
    assert "campaign_suspended" not in _events(run)
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    _assert_clean(run, snapshot)

    plain, _ = await _uncut(tmp_path, monkeypatch, _chat_run, "admitted")
    _assert_same_structure(run, plain, dropped_operations={first["operation_id"]})


# -- Row 2: after `grouping_failed(role_failed)`, before the suspension ----------

#: name: (the grouping scripts of the run, rounds, the owed suspension,
#: the failed record's `(reason, error_class)`).
OWED = {
    "provider": (("outage",), 1, "grouper_provider_failed", ("provider_exhausted", "provider")),
    "unknown": (("exception",), 1, "grouper_failing", ("exception", "unknown")),
    "second_link": (("garbage", "garbage"), 2, "grouper_failing", ("invalid_output", "content")),
}


def _owed_run(rounds: int):
    async def run(path, monkeypatch, **options):
        return await _chain_run(path, monkeypatch, rounds, **options)

    return run


def _only_the_planner(monkeypatch) -> None:
    """Remove `resumed_resilience_cause`'s Grouper half, so only the planner's
    grouping branch (F0 R46) can write the owed suspension."""

    original = campaign_module._Campaign.resumed_resilience_cause

    def cause(self):
        found = original(self)
        return None if found in GROUPER_SUSPENSIONS else found

    monkeypatch.setattr(campaign_module._Campaign, "resumed_resilience_cause", cause)


def _spy_planner(monkeypatch) -> list[tuple[Any, ...]]:
    """Every action list `drive` gets from `next_actions` (the planner)."""

    planned: list[tuple[Any, ...]] = []
    original = campaign_module.next_actions

    def next_actions(view, policy):
        actions = tuple(original(view, policy))
        planned.append(actions)
        return actions

    monkeypatch.setattr(campaign_module, "next_actions", next_actions)
    return planned


def _spy_resumed_causes(monkeypatch) -> list[str | None]:
    seen: list[str | None] = []
    original = campaign_module._Campaign.resumed_resilience_cause

    def cause(self):
        found = original(self)
        seen.append(found)
        return found

    monkeypatch.setattr(campaign_module._Campaign, "resumed_resilience_cause", cause)
    return seen


@pytest.mark.parametrize("owed_by", ["resumed_cause", "planner"])
@pytest.mark.parametrize("name", sorted(OWED))
async def test_a_cut_before_the_owed_suspension_writes_exactly_that_suspension(
    tmp_path, monkeypatch, name, owed_by
):
    """`_cut_hook` at `after_grouping_failed`: the `grouping_failed
    (role_failed)` record is durable and the live `self.stop(...)` never ran.
    The resume writes the one suspension of that record's class, naming it
    (`stage_record`, its seq and operation), and plans nothing else: no
    provider request, no grouping, no round record."""

    refusals = _spy_refusals(monkeypatch)
    groupings, rounds, reason, (failed_reason, error_class) = OWED[name]
    client = _scripted(monkeypatch, *groupings)
    run_it = _owed_run(rounds)
    await _cut(lambda patch: run_it(tmp_path, patch), monkeypatch, "after_grouping_failed")
    run = tmp_path / "run"
    before = _journal(run)
    failed = before[-1]
    assert failed["event"] == "grouping_failed"
    assert (failed["payload"]["code"], failed["payload"]["reason"]) == (
        "role_failed",
        failed_reason,
    )
    assert failed["payload"]["error_class"] == error_class
    requests = len(client.requests)
    if owed_by == "planner":
        _only_the_planner(monkeypatch)
    else:
        causes = _spy_resumed_causes(monkeypatch)
    planned = _spy_planner(monkeypatch)

    run, snapshot = await run_it(tmp_path, monkeypatch)

    assert refusals == [] and client.script == [] and len(client.requests) == requests
    after = _journal(run)
    assert after[: len(before)] == before
    (suspension,) = after[len(before) :]
    assert suspension["event"] == "campaign_suspended"
    assert suspension["payload"] == {
        "reason": reason,
        "owner": "grouper",
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": failed["sequence"],
            "operation_id": failed["operation_id"],
        },
    }
    assert len(_of(run, "campaign_suspended")) == 1
    if owed_by == "resumed_cause":
        # The resume path set the pending suspension before the drive loop,
        # which therefore never asked the planner: the planner's own
        # `role_failed` branch cannot have written this suspension.
        assert causes == [reason]
        assert planned == []
    else:
        # Only the planner's grouping branch proposed it, as its first action.
        assert planned[0] == (FinishCampaign("infrastructure_failed", reason, "none"),)
    assert snapshot.finished is None
    assert snapshot.grouper_chain == 0
    _assert_clean(run, snapshot)

    monkeypatch.undo()
    plain, _ = await _uncut(tmp_path, monkeypatch, run_it, *groupings)
    _assert_same_structure(run, plain)


# -- Row 3: after a Grouper suspension --------------------------------------------

SUSPENDING = {"outage": "grouper_provider_failed", "exception": "grouper_failing"}


@pytest.mark.parametrize("name", sorted(SUSPENDING))
async def test_after_a_grouper_suspension_the_same_command_groups_again(
    tmp_path, monkeypatch, name
):
    """Suspend; the same command repeats the failure and suspends again under
    a new grouping id on the same baseline; the same command, the provider
    repaired, groups a third time on that baseline and completes the round."""

    refusals = _spy_refusals(monkeypatch)
    client = _client(monkeypatch, *_failure_items(name))

    run, snapshot = await _chat_run(tmp_path, monkeypatch)
    assert snapshot.finished is None
    _assert_clean(run, snapshot)
    client.script.extend(_failure_items(name))

    run, snapshot = await _chat_run(tmp_path, monkeypatch)

    assert snapshot.finished is None and client.script == []
    starts = _of(run, "grouping_started")
    failures = _of(run, "grouping_failed")
    suspensions = _of(run, "campaign_suspended")
    assert len(starts) == len(failures) == len(suspensions) == 2
    records = _journal(run)
    for started, failed, suspended in zip(starts, failures, suspensions, strict=True):
        at = records.index(started)
        assert records[at : at + 3] == [started, failed, suspended]
        assert failed["payload"]["code"] == "role_failed"
        assert failed["payload"]["grouping_id"] == started["payload"]["grouping_id"]
        assert suspended["payload"]["reason"] == SUSPENDING[name]
        assert suspended["payload"]["evidence"]["seq"] == failed["sequence"]
    assert starts[1]["payload"]["grouping_id"] != starts[0]["payload"]["grouping_id"]
    assert starts[1]["payload"]["baseline_binding"] == starts[0]["payload"]["baseline_binding"]
    assert records[-1] == suspensions[1]
    _assert_clean(run, snapshot)
    client.script.extend(_grouping_script("admitted"))

    run, snapshot = await _chat_run(tmp_path, monkeypatch)

    assert refusals == [] and client.script == []
    starts = _of(run, "grouping_started")
    assert len(starts) == 3
    assert len({s["payload"]["grouping_id"] for s in starts}) == 3
    assert len({s["payload"]["baseline_binding"] for s in starts}) == 1
    (finished,) = payloads(run, "grouping_finished")
    assert finished["grouping_id"] == starts[2]["payload"]["grouping_id"]
    assert finished["fallback"] is None
    assert len(_of(run, "campaign_suspended")) == 2
    assert [p["round"] for p in payloads(run, "round_closed")] == [1]
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    _assert_clean(run, snapshot)

    plain, _ = await _uncut(tmp_path, monkeypatch, _chat_run, "admitted")
    _assert_same_structure(
        run,
        plain,
        dropped_operations={s["operation_id"] for s in starts[:2]},
        dropped_events={"campaign_suspended"},
    )


async def test_a_grouper_change_after_a_grouper_suspension_refuses_the_resume(
    tmp_path, monkeypatch
):
    """Existing H1b behavior, asserted once for the suspended state (A§10.6):
    the `grouper` section is part of the adaptation identity."""

    from evolution.tree.evolve import EvolveRefused

    client = _client(monkeypatch, *_grouping_script("outage"))
    run, snapshot = await _chat_run(tmp_path, monkeypatch)
    assert snapshot.finished is None
    assert payloads(run, "campaign_suspended")[-1]["reason"] == "grouper_provider_failed"
    before = (run / "campaign.jsonl").read_bytes()

    with pytest.raises(EvolveRefused) as refused:
        await _chat_run(tmp_path, monkeypatch, grouper={"max_output_tokens": 4096})

    assert refused.value.code == "run_identity_changed"
    assert [difference.field for difference in refused.value.differences] == ["adaptation"]
    assert (run / "campaign.jsonl").read_bytes() == before
    assert client.script == []


# -- Row 4: after `grouping_finished` with a fallback ------------------------------

#: name: (the grouping script, run config, the recorded fallback).
FALLBACKS = {
    "full": ("garbage", OBSERVE, FULL_INVALID),
    "partial": (
        "partial",
        {**OBSERVE, "repeats": 2},
        {"kind": "singletons", "scope": "uncovered", "reason": "invalid_output"},
    ),
}


@pytest.mark.parametrize("name", sorted(FALLBACKS))
async def test_a_fallback_result_stands_across_a_cut(tmp_path, monkeypatch, name):
    """`_cut_hook` at `after_grouping_finished`: the fallback result is durable.
    The resume reuses it: no provider request, no second `grouping_started`
    for that baseline, the same caveat, and the round completes."""

    refusals = _spy_refusals(monkeypatch)
    grouping, config, fallback = FALLBACKS[name]
    client = _chain_client(monkeypatch, grouping)
    await _cut(
        lambda patch: _chat_run(tmp_path, patch, config), monkeypatch, "after_grouping_finished"
    )
    run = tmp_path / "run"
    (stood,) = _of(run, "grouping_finished")
    assert _journal(run)[-1] == stood
    assert stood["payload"]["fallback"] == fallback
    requests = len(client.requests)

    run, snapshot = await _chat_run(tmp_path, monkeypatch, config)

    assert refusals == [] and client.script == [] and len(client.requests) == requests
    assert len(_of(run, "grouping_started")) == 1
    assert _of(run, "grouping_finished") == [stood]
    assert _caveat_rounds(snapshot) == [1]
    assert "grouping_failed" not in _events(run) and "campaign_suspended" not in _events(run)
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    _assert_clean(run, snapshot)

    plain, _ = await _uncut(tmp_path, monkeypatch, _chat_run, grouping, config=config)
    _assert_same_structure(run, plain)


async def test_a_cut_after_the_first_link_keeps_the_chain(tmp_path, monkeypatch):
    """The chain is derived from the journal, not held by the process: cut
    after round 1's full fallback, the resumed round 2's full fallback is still
    the second link and suspends `grouper_failing`."""

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "garbage")
    await _cut(lambda patch: _chain_run(tmp_path, patch, 2), monkeypatch, "after_grouping_finished")
    run = tmp_path / "run"
    assert _fallbacks(run) == [(1, FULL_INVALID)]
    assert _journal(run)[-1]["event"] == "grouping_finished"

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)

    assert refusals == [] and client.script == []
    assert len(_of(run, "grouping_started")) == 2
    _assert_second_link(run, snapshot, number=2, reason="invalid_output")
    _assert_clean(run, snapshot)

    plain, _ = await _uncut(tmp_path, monkeypatch, _owed_run(2), "garbage", "garbage")
    _assert_same_structure(run, plain)


# -- Row 5: after the second-link suspension -------------------------------------


async def test_after_the_second_link_suspension_the_chain_starts_empty(tmp_path, monkeypatch):
    """full, full suspends `grouper_failing`; the resume regroups round 2 on
    the same baseline with an empty chain, so its full fallback is a caveat
    and the campaign completes."""

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "garbage")
    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)
    _assert_second_link(run, snapshot, number=2, reason="invalid_output")
    _assert_clean(run, snapshot)
    (failed_start,) = [s for s in _of(run, "grouping_started") if s["payload"]["round"] == 2]
    client.script.extend(_grouping_script("garbage"))

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)

    assert refusals == [] and client.script == []
    regrouped = [s for s in _of(run, "grouping_started") if s["payload"]["round"] == 2]
    assert regrouped[0] == failed_start and len(regrouped) == 2
    assert regrouped[1]["payload"]["grouping_id"] != failed_start["payload"]["grouping_id"]
    assert (
        regrouped[1]["payload"]["baseline_binding"] == failed_start["payload"]["baseline_binding"]
    )
    assert _fallbacks(run) == [(1, FULL_INVALID), (2, FULL_INVALID)]
    assert (
        payloads(run, "grouping_finished")[1]["grouping_id"]
        == (regrouped[1]["payload"]["grouping_id"])
    )
    assert _caveat_rounds(snapshot) == [1, 2]
    assert snapshot.grouper_chain == 1
    assert len(_of(run, "campaign_suspended")) == 1
    assert [p["round"] for p in payloads(run, "round_closed")] == [1, 2]
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    _assert_clean(run, snapshot)


# -- An authorized condition change while a Grouper suspension is owed ---------

#: name: (the failing grouping's round, the authorization's effect). The
#: change is agent-side, so with rounds remaining the refusal offers all three
#: effects (`in_place_effects`); round 2 runs on a refresh baseline (plan
#: decision 15).
AUTHORIZED = {
    "r1-continue": (1, "continue"),
    "r1-rebaseline": (1, "rebaseline"),
    "r2-continue": (2, "continue"),
    "r2-rebaseline": (2, "rebaseline"),
}


@pytest.mark.parametrize("name", sorted(AUTHORIZED))
async def test_an_authorized_change_after_role_failed_still_writes_the_owed_suspension(
    tmp_path, monkeypatch, name
):
    """`_cut_hook` at `after_grouping_failed` leaves a `grouping_failed
    (role_failed)` whose suspension is owed; the operator then changes the
    Target's settings. The refused resume offers its effects; the authorized
    one applies the change, writes the owed `grouper_provider_failed`
    suspension naming that record (after `round_closed{condition_changed}`
    under `rebaseline`), and asks the provider nothing. The same command then
    groups again and completes the campaign."""

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import EDITED, _ResettableTagged

    refusals = _spy_refusals(monkeypatch)
    failing_round, effect = AUTHORIZED[name]
    rounds = 3
    regroupings = rounds - failing_round + (1 if effect == "continue" else 0)
    client = _scripted(
        monkeypatch,
        *["admitted"] * (failing_round - 1),
        "outage",
        *["admitted"] * regroupings,
    )
    adapter = _ResettableTagged(used=EDITED["used"])

    async def run_it(path, patch, **options):
        return await _chain_run(path, patch, rounds, adapter=adapter, **options)

    await _cut(lambda patch: run_it(tmp_path, patch), monkeypatch, "after_grouping_failed")
    run = tmp_path / "run"
    before = _journal(run)
    failed = before[-1]
    assert failed["event"] == "grouping_failed"
    assert failed["payload"]["round"] == failing_round
    assert (
        failed["payload"]["code"],
        failed["payload"]["reason"],
        failed["payload"]["error_class"],
    ) == ("role_failed", "provider_exhausted", "provider")
    (failed_start,) = [
        s for s in _of(run, "grouping_started") if s["operation_id"] == failed["operation_id"]
    ]
    requests = len(client.requests)

    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await run_it(tmp_path, monkeypatch)
    assert refused.value.code == "run_identity_changed"
    assert refused.value.effects == ("continue", "rebaseline", "close")
    assert _journal(run) == before
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept a changed Target after a Grouper failure",
        now="2026-10-12T00:00:00Z",
        effect=effect,
    )

    run, snapshot = await run_it(tmp_path, monkeypatch, authorizations=[decision])

    assert refusals == [] and len(client.requests) == requests
    after = _journal(run)
    assert after[: len(before)] == before
    added = after[len(before) :]
    closes = ["round_closed"] if effect == "rebaseline" else []
    assert [record["event"] for record in added] == [
        "authorization_applied",
        "identity_rebased",
        *closes,
        "campaign_suspended",
    ]
    if effect == "rebaseline":
        closed = added[2]["payload"]
        assert (closed["round"], closed["outcome"], closed["reason"]) == (
            failing_round,
            "condition_changed",
            None,
        )
        assert closed["attempts_opened"] == 0
    assert added[-1]["payload"] == {
        "reason": "grouper_provider_failed",
        "owner": "grouper",
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": failed["sequence"],
            "operation_id": failed["operation_id"],
        },
    }
    assert snapshot.finished is None
    assert snapshot.grouper_chain == 0
    _assert_clean(run, snapshot)

    run, snapshot = await run_it(tmp_path, monkeypatch)

    assert refusals == [] and client.script == []
    assert _journal(run)[: len(after)] == after
    regrouped = [s for s in _of(run, "grouping_started") if s["sequence"] > failed["sequence"]]
    assert len(regrouped) == regroupings
    if effect == "continue":
        # The changed condition is accepted in place: the failed round groups
        # again on the same baseline.
        assert regrouped[0]["payload"]["round"] == failing_round
        assert (
            regrouped[0]["payload"]["baseline_binding"]
            == failed_start["payload"]["baseline_binding"]
        )
    else:
        # The failed round closed `condition_changed`; the next round groups
        # on the new baseline.
        assert regrouped[0]["payload"]["round"] == failing_round + 1
        assert (
            regrouped[0]["payload"]["baseline_binding"]
            != failed_start["payload"]["baseline_binding"]
        )
    assert all(p["fallback"] is None for p in payloads(run, "grouping_finished"))
    assert len(_of(run, "campaign_suspended")) == 1
    assert len(_of(run, "grouping_failed")) == 1
    assert [p["round"] for p in payloads(run, "round_closed")] == list(range(1, rounds + 1))
    assert snapshot.finished is not None
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == (
        "completed",
        "rounds_exhausted",
    )
    _assert_clean(run, snapshot)
