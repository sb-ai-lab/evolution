"""H9: genuine agent calls across refreshed baselines and fallback chains."""

from __future__ import annotations

import json

import pytest

from evolution.tree.campaign_caveats import grouping_caveat_entries
from tests.agent_grouping_fakes import scripted_grouping_cli
from tests.campaign_drivers import _records
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_grouper_run import NO_FINALISTS, _spy_refusals
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


async def agent_chain_run(tmp_path, monkeypatch, section, rounds, **kwargs):
    return await family14_run(
        tmp_path,
        monkeypatch,
        NO_FINALISTS,
        family14_settings={"grouper": section, "refresh": 1},
        campaign={"rounds": rounds},
        **kwargs,
    )


def assert_caveats(run, snapshot, expected):
    """Option A: derived entries, with genuine journal seqs; no H12 validator."""
    records = _records(run / "campaign.jsonl")
    finished = [r for r in records if r["event"] == "grouping_finished"]
    assert grouping_caveat_entries(snapshot) == [
        (
            next(records.index(r) + 1 for r in finished if r["payload"]["round"] == number),
            {
                "kind": "grouping_fallback",
                "round": number,
                "reason": reason,
                "fallback_kind": "singletons",
                "scope": scope,
            },
        )
        for number, reason, scope in expected
    ]
    for record in finished:
        payload = record["payload"]
        reason = (payload["fallback"] or {}).get("reason")
        assert (
            payload["calls"]
            == {
                "turns_exhausted": {"agent_calls": 1, "repairs": 0, "turns": 6},
                "invalid_output": {"agent_calls": 2, "repairs": 1, "turns": 2},
                None: {"agent_calls": 1, "repairs": 0, "turns": 1},
            }[reason]
        )


def assert_second_link(run, snapshot, number, reason):
    records = _records(run / "campaign.jsonl")
    started, failed, suspended = records[-3:]
    assert [r["event"] for r in records[-3:]] == [
        "grouping_started",
        "grouping_failed",
        "campaign_suspended",
    ]
    assert started["payload"]["round"] == number
    assert failed["operation_id"] == started["operation_id"]
    assert failed["payload"] == {
        "round": number,
        "grouping_id": started["payload"]["grouping_id"],
        "code": "role_failed",
        "reason": reason,
        "error_class": "content",
        "reads": failed["payload"]["reads"],
    }
    assert suspended["payload"] == {
        "reason": "grouper_failing",
        "owner": "grouper",
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": records.index(failed) + 1,
            "operation_id": failed["operation_id"],
        },
    }
    assert len(payloads(run, "campaign_suspended")) == 1
    assert [p["round"] for p in payloads(run, "round_closed")] == list(range(1, number))
    assert snapshot.finished is None and snapshot.grouper_chain == 0
    assert snapshot.rounds[number].grouping is None
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "first,last",
    [
        ("invalid_output", "invalid_output"),
        ("turns_exhausted", "turns_exhausted"),
        ("invalid_output", "turns_exhausted"),
        ("turns_exhausted", "invalid_output"),
    ],
)
@pytest.mark.parametrize("middle", [None, "partial", "admitted", "agent_error"])
async def test_agent_full_fallback_chain(tmp_path, monkeypatch, first, last, middle):
    """A partial preserves the chain; admission and suspension reset it."""
    refusals = _spy_refusals(monkeypatch)
    script = [first, *([] if middle is None else [middle]), last]
    section = scripted_grouping_cli(tmp_path, *script)
    rounds = 2 if middle in (None, "agent_error") else 3
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, rounds)
    if middle == "agent_error":
        assert snapshot.finished is None and snapshot.grouper_chain == 0
        (failed,) = payloads(run, "grouping_failed")
        assert (failed["round"], failed["reason"], failed["error_class"]) == (
            2,
            "agent_error",
            "unknown",
        )
        assert audit(run)["mismatches"] == []
        run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, rounds)
    assert refusals == []
    expected = [(1, first, "all")]
    if middle == "partial":
        expected.append((2, "invalid_output", "uncovered"))
    if middle in ("admitted", "agent_error"):
        expected.append((rounds, last, "all"))
        assert snapshot.finished["outcome"] == "completed"
        assert snapshot.grouper_chain == 1
        assert len(payloads(run, "campaign_suspended")) == int(middle == "agent_error")
        assert len(payloads(run, "round_closed")) == rounds
    else:
        assert_second_link(run, snapshot, rounds, last)
    assert_caveats(run, snapshot, expected)
    starts = payloads(run, "grouping_started")
    assert len(starts) == len(script)
    assert len({p["baseline_binding"] for p in starts}) == rounds
    calls = [
        json.loads(line)
        for line in (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
    ]
    assert len(calls) == sum(2 if mode in ("invalid_output", "partial") else 1 for mode in script)
    assert len({call["root"] for call in calls}) == len(script)
    assert list((run / ".grouping").iterdir()) == []
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("reason", ["invalid_output", "turns_exhausted"])
async def test_authorized_agent_change_does_not_reset_fallback_chain(tmp_path, monkeypatch, reason):
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash

    section = scripted_grouping_cli(tmp_path, reason, reason)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await agent_chain_run(tmp_path, monkeypatch, section, 2)
    changed = {**section, "timeout_seconds": 61}
    with pytest.raises(EvolveRefused) as refused:
        await agent_chain_run(tmp_path, monkeypatch, changed, 2)
    assert refused.value.code == "run_identity_changed"
    assert refused.value.effects == ("rebase",)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "adjust Grouper deadline",
        now="2026-10-12T00:00:00Z",
    )
    run, snapshot = await agent_chain_run(
        tmp_path, monkeypatch, changed, 2, authorizations=[decision]
    )
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == ["adaptation"]
    created = _records(run / "campaign.jsonl")[0]["payload"]["adaptation_payload"]
    assert rebased["adaptation_payload"]["grouping"] != created["grouping"]
    assert {**rebased["adaptation_payload"], "grouping": None} == {**created, "grouping": None}
    assert_caveats(run, snapshot, [(1, reason, "all")])
    assert_second_link(run, snapshot, 2, reason)


async def test_agent_exact_turn_limit_partial_answer_extends_full_fallback_chain(
    tmp_path, monkeypatch
):
    from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY, sealed_grouping_cli

    section = sealed_grouping_cli(
        tmp_path,
        max_turns=1,
        body=GROUP_ALL_AGENT_BODY + "\noutput = work / '.evolution/GROUPS.json'\n"
        "groups = json.loads(output.read_text())\n"
        "groups['groups'][0]['members'] = groups['groups'][0]['members'][:1]\n"
        "output.write_text(json.dumps(groups))\n",
    )
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 2)
    assert_second_link(run, snapshot, 2, "turns_exhausted")
    (finished,) = payloads(run, "grouping_finished")
    assert finished["calls"] == {"agent_calls": 1, "repairs": 0, "turns": 1}
    assert finished["fallback"] == {
        "kind": "singletons",
        "scope": "all",
        "reason": "turns_exhausted",
    }
    assert [caveat for _seq, caveat in grouping_caveat_entries(snapshot)] == [
        {
            "kind": "grouping_fallback",
            "round": 1,
            "reason": "turns_exhausted",
            "fallback_kind": "singletons",
            "scope": "all",
        }
    ]
    calls = [
        json.loads(line)
        for line in (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2
    assert all(not call["repair"] for call in calls)
    assert list((run / ".grouping").iterdir()) == []
