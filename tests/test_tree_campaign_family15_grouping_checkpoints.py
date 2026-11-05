"""b16 §9: family-15 chat Grouper checkpoints and their adoption on resume.

Each scenario is a genuine `/16` campaign (`public_run`) whose `chat` Grouper
is the scripted `model_grouping/2` fake. Four failing cases (`c1` and the
three in `FAILING`), one repeat each and a capacity of one unit per portion
(``group_output_tokens`` equal to ``max_output_tokens``) give four portions,
so a grouping is the preflight, four portion sessions of one request each and
three consolidation batches (`MergingGrouper` merges the first two cards of
every batch: level 0 asks ``b1`` and ``b2``, level 1 the one batch left).

A cut is a `PowerCut` (a `BaseException`) raised from the campaign hook that
follows the k-th checkpoint of a kind, so it leaves the engine as a process
death would, with the `grouping_started` open; the resume with the same
command settles it `grouping_failed(interrupted)` and groups again.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.campaign_drivers import _records, audit_mismatches
from tests.model_grouping_fakes import PortionGrouper, chat_grouper_section, use_chat_grouper
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow

FAILING = {case: {"critical": True} for case in ("c3", "c4", "c5")}
#: The preflight, then one request per portion.
PORTIONS = 4
BATCHES = 3
GROUPING = 1 + PORTIONS + BATCHES
PORTION_EVENT = "grouping_portion_finished"
BATCH_EVENT = "grouping_batch_finished"


class MergingGrouper(PortionGrouper):
    """`PortionGrouper` whose consolidation merges the first two cards of a batch."""

    def _answer(self, kw: Any) -> Any:
        from evolution.tree.roles.grouping.consolidation import BATCH_LEAD

        if kw.get("tools") or kw.get("functions"):
            return super()._answer(kw)
        user = next(m["content"] for m in kw["messages"] if m["role"] == "user")
        cards = json.loads(user[len(BATCH_LEAD) :])["cards"]
        return json.dumps({"merges": [{"groups": [cards[0]["id"], cards[1]["id"]]}]}), []


def _configure(concurrency: int = 1, **overrides: Any):
    def configure(document):
        document["grouper"] = chat_grouper_section(
            concurrency=concurrency, group_output_tokens=8192, **overrides
        )

    return configure


def _cut_after(monkeypatch, name: str, count: int) -> None:
    """Raise `PowerCut` from the hook after the `count`-th `name` checkpoint."""

    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook
    seen = {"n": 0}

    def hook(self, hook_name):
        original(self, hook_name)
        if hook_name == name:
            seen["n"] += 1
            if seen["n"] == count:
                raise PowerCut()

    monkeypatch.setattr(_Campaign, "hook", hook)


async def _run(root, monkeypatch, configure, cases=FAILING):
    return await public_run(root, monkeypatch, configure=configure, extra_cases=cases)


async def _control(root, monkeypatch, *, concurrency: int = 1, cases=FAILING):
    client = use_chat_grouper(monkeypatch, MergingGrouper())
    run, result = await _run(root, monkeypatch, _configure(concurrency), cases)
    assert result.terminal == "finished"
    return run, len(client.requests)


async def _cut(root, monkeypatch, name, count, *, concurrency: int = 1, cases=FAILING):
    """A run cut after the `count`-th `name` hook; the client and its requests so far."""

    client = use_chat_grouper(monkeypatch, MergingGrouper())
    with monkeypatch.context() as patch:
        _cut_after(patch, name, count)
        with pytest.raises(PowerCut):
            await _run(root, monkeypatch, _configure(concurrency), cases)
    return client, len(client.requests)


def _finished(run) -> dict[str, Any]:
    (finished,) = payloads(run, "grouping_finished")
    return finished


def _checkpoints(run, event, grouping_id=None) -> list[dict[str, Any]]:
    return [
        p for p in payloads(run, event) if grouping_id is None or p["grouping_id"] == grouping_id
    ]


def _groupings(run) -> list[str]:
    return [p["grouping_id"] for p in payloads(run, "grouping_started")]


def _assert_same_result(run, control) -> None:
    """The admitted result of both runs; the registry digest binds each run's own evidence."""

    resumed, uncut = _finished(run), _finished(control)
    assert {**resumed["result"], "registry_digest": None} == {
        **uncut["result"],
        "registry_digest": None,
    }
    assert resumed["calls"]["reduce"] == uncut["calls"]["reduce"] == "done"
    assert resumed["fallback"] == uncut["fallback"]


# -- the uninterrupted run ---------------------------------------------------------


async def test_an_uncut_grouping_checkpoints_every_portion_and_batch(tmp_path, monkeypatch):
    run, requests = await _control(tmp_path, monkeypatch)
    assert requests == GROUPING
    (grouping_id,) = _groupings(run)
    portions = _checkpoints(run, PORTION_EVENT)
    batches = _checkpoints(run, BATCH_EVENT)
    assert {p["grouping_id"] for p in portions + batches} == {grouping_id}
    assert sorted(p["portion_id"] for p in portions) == ["p1", "p2", "p3", "p4"]
    assert [(b["level"], b["index"]) for b in batches] == [(0, "b1"), (0, "b2"), (1, "b1")]
    assert all(p["requests"] == 1 and len(p["groups"]) == 1 for p in portions)
    assert len({p["plan_digest"] for p in portions}) == 1
    assert all(len(b["merges"]) == 1 and b["skipped"] == 0 for b in batches)
    # Every checkpoint precedes the grouping's terminal.
    events = [r["event"] for r in _records(run / "campaign.jsonl")]
    last = max(i for i, e in enumerate(events) if e in (PORTION_EVENT, BATCH_EVENT))
    assert last < events.index("grouping_finished")
    assert audit_mismatches(run) == []


# -- interruption and adoption ------------------------------------------------------


async def test_interrupt_after_portions_resumes_and_adopts(tmp_path, monkeypatch):
    client, before = await _cut(tmp_path, monkeypatch, "after_grouping_portion_finished", 2)
    # One session at a time: the cut comes before `p3` is asked.
    assert before == 1 + 2
    run, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    # Only the rest is asked: the preflight, two portions and every batch.
    assert len(client.requests) - before == 1 + 2 + BATCHES
    first, second = _groupings(run)
    (failed,) = payloads(run, "grouping_failed")
    assert (failed["grouping_id"], failed["code"]) == (first, "interrupted")
    assert sorted(p["portion_id"] for p in _checkpoints(run, PORTION_EVENT, first)) == [
        "p1",
        "p2",
    ]
    # The resumed grouping records only what it asked itself.
    assert sorted(p["portion_id"] for p in _checkpoints(run, PORTION_EVENT, second)) == [
        "p3",
        "p4",
    ]
    assert len(_checkpoints(run, BATCH_EVENT, second)) == BATCHES
    assert _finished(run)["calls"]["map_requests"] == 2
    assert audit_mismatches(run) == []


async def test_interrupt_after_batches_resumes_and_adopts(tmp_path, monkeypatch):
    client, before = await _cut(tmp_path, monkeypatch, "after_grouping_batch_finished", 1)
    run, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    first, second = _groupings(run)
    # Level 0's `b2` may finish, and be recorded, before the cut reaches it.
    recorded = [(b["level"], b["index"]) for b in _checkpoints(run, BATCH_EVENT, first)]
    assert recorded in ([(0, "b1")], [(0, "b1"), (0, "b2")])
    # Every portion is adopted, so no preflight and no session: the rest of
    # the batches only.
    assert len(client.requests) - before == BATCHES - len(recorded)
    asked = [(b["level"], b["index"]) for b in _checkpoints(run, BATCH_EVENT, second)]
    assert recorded + asked == [(0, "b1"), (0, "b2"), (1, "b1")]
    assert _checkpoints(run, PORTION_EVENT, second) == []
    assert _finished(run)["calls"]["map_requests"] == 0
    assert audit_mismatches(run) == []


@pytest.mark.parametrize(
    "name,count",
    [
        ("after_grouping_portion_finished", 1),
        ("after_grouping_portion_finished", PORTIONS),
        ("after_grouping_batch_finished", 2),
    ],
)
async def test_resume_result_equals_uninterrupted(tmp_path, monkeypatch, name, count):
    control, _ = await _control(tmp_path / "control", monkeypatch)
    await _cut(tmp_path / "cut", monkeypatch, name, count)
    run, result = await _run(tmp_path / "cut", monkeypatch, _configure())
    assert result.terminal == "finished"
    _assert_same_result(run, control)


async def test_a_second_interruption_keeps_the_first_groupings_checkpoints(tmp_path, monkeypatch):
    control, _ = await _control(tmp_path / "control", monkeypatch)
    client, first_cut = await _cut(
        tmp_path / "cut", monkeypatch, "after_grouping_portion_finished", 1
    )
    # The resume adopts p1 and is cut after its own first checkpoint (p2).
    with monkeypatch.context() as patch:
        _cut_after(patch, "after_grouping_portion_finished", 1)
        with pytest.raises(PowerCut):
            await _run(tmp_path / "cut", monkeypatch, _configure())
    second_cut = len(client.requests)
    assert second_cut - first_cut == 1 + 1
    run, result = await _run(tmp_path / "cut", monkeypatch, _configure())
    assert result.terminal == "finished"
    # p1 from the first grouping, p2 from the second: two portions are left.
    assert len(client.requests) - second_cut == 1 + 2 + BATCHES
    assert len(_groupings(run)) == 3
    _assert_same_result(run, control)
    assert audit_mismatches(run) == []


async def test_crash_between_checkpoints_concurrency_4(tmp_path, monkeypatch):
    cases = {f"c{n}": {"critical": True} for n in range(3, 9)}  # seven failing with c1
    control, total = await _control(tmp_path / "control", monkeypatch, concurrency=4, cases=cases)
    client, before = await _cut(
        tmp_path / "cut",
        monkeypatch,
        "after_grouping_portion_finished",
        2,
        concurrency=4,
        cases=cases,
    )
    run, result = await _run(tmp_path / "cut", monkeypatch, _configure(4), cases)
    assert result.terminal == "finished"
    first = _groupings(run)[0]
    written = {p["portion_id"] for p in _checkpoints(run, PORTION_EVENT, first)}
    assert len(written) >= 2
    # Exactly the written portions are adopted: the resume asks every other one.
    assert len(client.requests) - before == total - len(written)
    second = _groupings(run)[1]
    asked = {p["portion_id"] for p in _checkpoints(run, PORTION_EVENT, second)}
    assert asked.isdisjoint(written) and len(asked | written) == 7
    _assert_same_result(run, control)
    assert audit_mismatches(run) == []


async def test_provider_suspension_keeps_checkpoints(tmp_path, monkeypatch):
    control, _ = await _control(tmp_path / "control", monkeypatch)
    # The preflight and two portions are answered; the third portion's
    # request and its retries fail, so the Grouper suspends as a provider.
    client = use_chat_grouper(monkeypatch, MergingGrouper(budget=1 + 2))
    run, result = await _run(tmp_path / "cut", monkeypatch, _configure())
    assert (result.terminal, result.code) == ("suspended", "grouper_provider_failed")
    (failed,) = payloads(run, "grouping_failed")
    assert (failed["code"], failed["error_class"]) == ("role_failed", "provider")
    assert sorted(p["portion_id"] for p in _checkpoints(run, PORTION_EVENT)) == ["p1", "p2"]
    before = len(client.requests)
    client.budget = None
    run, result = await _run(tmp_path / "cut", monkeypatch, _configure())
    assert result.terminal == "finished"
    assert len(client.requests) - before == 1 + 2 + BATCHES
    _assert_same_result(run, control)
    assert audit_mismatches(run) == []


async def test_changed_identity_adopts_nothing(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_visibility_change import _decision

    client, before = await _cut(tmp_path, monkeypatch, "after_grouping_portion_finished", 2)
    changed = _configure(glimpse_chars=700)
    with pytest.raises(EvolveRefused) as refused:
        await _run(tmp_path, monkeypatch, changed)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=changed,
        extra_cases=FAILING,
        authorizations=[_decision(refused.value, None, "accept the Grouper change")],
    )
    assert result.terminal == "finished"
    assert len(client.requests) - before == GROUPING
    second = _groupings(run)[-1]
    assert len(_checkpoints(run, PORTION_EVENT, second)) == PORTIONS
    assert audit_mismatches(run) == []


async def test_changed_plan_adopts_nothing(tmp_path, monkeypatch):
    from dataclasses import replace

    from evolution.tree.roles.grouping import model_grouping_v2

    client, before = await _cut(tmp_path, monkeypatch, "after_grouping_portion_finished", 2)
    planned = model_grouping_v2.plan_portions

    def other_plan(*args, **kwargs):
        plan = planned(*args, **kwargs)
        return replace(plan, digest="f" * 64)

    monkeypatch.setattr(model_grouping_v2, "plan_portions", other_plan)
    run, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    # The campaign offered both checkpoints; the plan digest refused them.
    assert len(client.requests) - before == GROUPING
    second = _groupings(run)[-1]
    assert len(_checkpoints(run, PORTION_EVENT, second)) == PORTIONS


async def test_changed_record_budget_adopts_no_batch(tmp_path, monkeypatch):
    """Spec §9, ADR-0055: nothing is adopted across a different plan, batches
    included. A record budget of 50,000 bytes instead of 57,344 changes the
    plan digest; the cards are the same, yet every batch is asked again."""

    from evolution.tree.family15_schema import MAX_RECORD_BYTES
    from evolution.tree.roles.grouping import grouping_portions

    client, before = await _cut(tmp_path, monkeypatch, "after_grouping_batch_finished", 1)
    monkeypatch.setattr(grouping_portions, "CHECKPOINT_ENVELOPE_BYTES", MAX_RECORD_BYTES - 50_000)
    run, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    first, second = _groupings(run)
    assert _checkpoints(run, BATCH_EVENT, first)
    plans = {p["plan_digest"] for p in _checkpoints(run, PORTION_EVENT, first)}
    (plan,) = {p["plan_digest"] for p in _checkpoints(run, PORTION_EVENT, second)}
    assert plan not in plans
    assert len(client.requests) - before == GROUPING
    asked = [(b["level"], b["index"]) for b in _checkpoints(run, BATCH_EVENT, second)]
    assert asked == [(0, "b1"), (0, "b2"), (1, "b1")]
    # Each batch names the plan of the grouping that asked it.
    assert {b["plan_digest"] for b in _checkpoints(run, BATCH_EVENT, second)} == {plan}
    assert {b["plan_digest"] for b in _checkpoints(run, BATCH_EVENT, first)} == plans
    assert audit_mismatches(run) == []


async def test_a_refused_checkpoint_propagates_and_keeps_the_grouping_open(tmp_path, monkeypatch):
    from evolution.tree.campaign import CampaignInvariantViolated, _GroupingCheckpoints

    client = use_chat_grouper(monkeypatch, MergingGrouper())
    original = _GroupingCheckpoints._write
    calls = {"n": 0}

    def broken(self, event, body, hook):
        calls["n"] += 1
        if calls["n"] == 2:
            body = {**body, "requests": -1}  # an executor bug: the schema refuses it
        return original(self, event, body, hook)

    with monkeypatch.context() as patch:
        patch.setattr(_GroupingCheckpoints, "_write", broken)
        with pytest.raises(CampaignInvariantViolated):
            await _run(tmp_path, monkeypatch, _configure())
    run = tmp_path / "run"
    # Not the model's failure: no terminal, the grouping stays open.
    assert payloads(run, "grouping_failed") == []
    assert [p["portion_id"] for p in _checkpoints(run, PORTION_EVENT)] == ["p1"]
    before = len(client.requests)
    run, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    (failed,) = payloads(run, "grouping_failed")
    assert failed["code"] == "interrupted"
    # p1 is adopted: the preflight, three portions and every batch.
    assert len(client.requests) - before == 1 + 3 + BATCHES
    assert audit_mismatches(run) == []


async def test_adoption_needs_the_same_baseline_and_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_schema import reduce

    await _cut(tmp_path, monkeypatch, "after_grouping_portion_finished", 2)
    run, _ = await _run(tmp_path, monkeypatch, _configure())
    records = _prefix(run)
    events = [record.event for record in records]
    # The journal as the resume read it: the first grouping settled interrupted.
    settled = records[: events.index("grouping_failed") + 1]
    snapshot = reduce(settled, selector=None, adaptive=True, family_version=15)
    campaign = SimpleNamespace(snapshot=lambda: snapshot)
    started = payloads(run, "grouping_started")[0]
    binding, policy = started["baseline_binding"], started["policy_digest"]

    def adopted(baseline_binding, policy_digest):
        return _Campaign._grouping_adoption(campaign, baseline_binding, policy_digest)

    assert sorted(adopted(binding, policy).portions) == ["p1", "p2"]
    for other in (adopted("0" * 64, policy), adopted(binding, "0" * 64)):
        assert not other.portions and not other.batches


# -- persisted form (PDR-0044) ---------------------------------------------------------

RUN_SECRET = "secret88"


class SecretGrouper(MergingGrouper):
    """`MergingGrouper` whose group titles and merged headers carry a run secret.

    With ``per_unit`` a portion's answer is one group per unit, each with
    every text field filled with the secret up to its bound, so the redacted
    record (``[REDACTED]`` is two characters longer) is a quarter larger.
    """

    def __init__(self, *, per_unit: bool = False) -> None:
        super().__init__(title=RUN_SECRET)
        self.per_unit = per_unit

    def _answer(self, kw: Any) -> Any:
        from evolution.tree.roles.grouping.consolidation import BATCH_LEAD
        from evolution.tree.roles.grouping.portion_session import PORTION_LEAD

        tools = [tool["function"]["name"] for tool in kw.get("tools") or ()]
        user = next(m["content"] for m in kw["messages"] if m["role"] == "user")
        if not tools and not kw.get("functions"):
            cards = json.loads(user[len(BATCH_LEAD) :])["cards"]
            header = {
                "title": f"{RUN_SECRET} merged",
                "failure_mode": "fm",
                "expected_behaviour": "eb",
                "question": "q?",
                "priority": "p",
            }
            merge = {"groups": [cards[0]["id"], cards[1]["id"]], **header}
            return json.dumps({"merges": [merge]}), []
        if not self.per_unit or tools == ["list_case"]:
            return super()._answer(kw)
        portion = json.loads(user[len(PORTION_LEAD) :])
        refs = [unit["ref"] for case in portion["cases"] for unit in case["units"]]
        s = RUN_SECRET
        groups = [
            {
                "id": f"g{n}",
                "title": s * 16,
                "failure_mode": s * 64,
                "expected_behaviour": s * 64,
                "question": s * 64,
                "members": [{"ref": ref, "variant": "v"}],
                "citations": [s * 32] * 16,
                "priority": s * 32,
            }
            for n, ref in enumerate(refs, start=1)
        ]
        return None, [("finish_portion", {"groups": groups})]


def _secret_configure(group_output_tokens: int = 8192):
    def configure(document):
        document["grouper"] = chat_grouper_section(group_output_tokens=group_output_tokens)
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"grouping_run_secret": "off"}}

    return configure


def _batch_requests(requests) -> list[str]:
    return [
        next(m["content"] for m in kw["messages"] if m["role"] == "user")
        for kw in requests
        if not kw.get("tools") and not kw.get("functions")
    ]


async def test_redacted_decisions_reach_consolidation_as_on_resume(tmp_path, monkeypatch):
    """PDR-0044: a role reads later the redacted text the journal keeps.

    With ``grouping_run_secret`` off a portion title and a merged header carry
    a run secret. Consolidation sees each as ``[REDACTED]``, in an
    uninterrupted run exactly as in one resumed after the first batch.
    """

    monkeypatch.setenv("GROUPING_TEST_KEY", RUN_SECRET)
    uncut = SecretGrouper()
    use_chat_grouper(monkeypatch, uncut)
    _, result = await _run(tmp_path / "uncut", monkeypatch, _secret_configure())
    assert result.terminal == "finished"
    control = _batch_requests(uncut.requests)
    assert len(control) == BATCHES
    assert all(RUN_SECRET not in text for text in control)
    assert '"title":"[REDACTED]"' in control[0]
    assert '"title":"[REDACTED] merged"' in control[-1]

    root = tmp_path / "cut"
    cut = SecretGrouper()
    use_chat_grouper(monkeypatch, cut)
    with monkeypatch.context() as patch:
        _cut_after(patch, "after_grouping_batch_finished", 1)
        with pytest.raises(PowerCut):
            await _run(root, monkeypatch, _secret_configure())
    before = len(cut.requests)
    run, result = await _run(root, monkeypatch, _secret_configure())
    assert result.terminal == "finished"
    resumed = _batch_requests(cut.requests[before:])
    # The level-1 batch reads b1's merged header: from the journal here.
    assert resumed[-1] == control[-1]
    assert RUN_SECRET not in (run / "campaign.jsonl").read_text()
    assert audit_mismatches(run) == []


async def test_checkpoint_budget_measures_the_redacted_record(tmp_path, monkeypatch):
    """PDR-0044 and spec §9: the budget measures what is written.

    Nine groups of secret-filled texts take about 56,400 bytes, within the
    57,344-byte budget, but about 70,000 once redacted, over the 64 KiB
    record ceiling. The portion splits on the budget; the schema refuses
    nothing.
    """

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_schema import MAX_RECORD_BYTES

    monkeypatch.setenv("GROUPING_TEST_KEY", RUN_SECRET)
    use_chat_grouper(monkeypatch, SecretGrouper(per_unit=True))
    cases = {f"c{n}": {"critical": True} for n in range(3, 11)}
    run, result = await _run(
        tmp_path, monkeypatch, _secret_configure(group_output_tokens=512), cases
    )
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    portions = _checkpoints(run, PORTION_EVENT)
    assert sorted(p["portion_id"] for p in portions) == ["p1.1", "p1.2"]
    assert sum(len(p["groups"]) for p in portions) == 9
    for record in _records(run / "campaign.jsonl"):
        if record["event"] in (PORTION_EVENT, BATCH_EVENT):
            assert len(canonical_json(record["payload"])) <= MAX_RECORD_BYTES
    logs = "".join(p.read_text() for p in (run / "reflector-logs").glob("grouping-*.jsonl"))
    assert '"reason": "record_budget"' in logs
    assert audit_mismatches(run) == []


# -- readers -------------------------------------------------------------------------


async def test_readers_accept_checkpoint_events(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.cli import tree_app

    await _cut(tmp_path, monkeypatch, "after_grouping_batch_finished", 1)
    run = tmp_path / "run"
    runner = CliRunner()

    def read(label: str) -> None:
        for command in (
            ["watch", "--run-dir", str(run), "--once"],
            ["trace", "--run-dir", str(run), "--out", str(tmp_path / f"{label}.jsonl")],
        ):
            outcome = runner.invoke(tree_app, command)
            assert outcome.exit_code == 0, outcome.output
        assert (tmp_path / f"{label}.jsonl").stat().st_size > 0
        projection = inspect_campaign(run, verify=True).to_payload()
        assert projection["verification"]["mismatches"] == []
        # The family-15 report is built over the same snapshot.
        assert projection["family_version"] == 15 and projection["independent"]

    # An open grouping with checkpoints, then the finished resumed run.
    assert _checkpoints(run, PORTION_EVENT) and _checkpoints(run, BATCH_EVENT)
    read("open")
    _, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    read("finished")


# -- the lifecycle -------------------------------------------------------------------


def _envelope(records, event, payload, *, family_version=15):
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.storage.envelope import Envelope

    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id=records[0].to_payload()["run_id"],
            sequence=len(records) + 1,
            timestamp="2026-10-30T00:00:00Z",
            event=event,
            operation_id=None,
            payload=payload,
        ),
        family=campaign_family_for(family_version),
    )


def _prefix(run):
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.storage.envelope import Envelope

    raw = (run / "campaign.jsonl").read_bytes().splitlines()
    return [Envelope.from_bytes(line, family=campaign_family_for(15)) for line in raw]


def _accepts(records) -> None:
    from evolution.tree.campaign_schema import reduce

    reduce(records, selector=None, adaptive=True, family_version=15)


async def test_checkpoint_naming_other_grouping_refused(tmp_path, monkeypatch):
    from evolution.tree.storage.envelope import SchemaError

    await _cut(tmp_path, monkeypatch, "after_grouping_portion_finished", 2)
    run = tmp_path / "run"
    records = _prefix(run)
    _accepts(records)
    (grouping_id,) = _groupings(run)
    first = _checkpoints(run, PORTION_EVENT)[0]
    p3 = {**first, "portion_id": "p3"}
    _accepts([*records, _envelope(records, PORTION_EVENT, p3)])
    refusals = [
        {**p3, "grouping_id": "r01-g-" + "0" * 32},  # names no started grouping
        first,  # a portion recorded twice
        {**first, "portion_id": first["portion_id"] + ".1"},  # a half of a recorded one
        {**p3, "plan_digest": "e" * 64},  # another plan in one grouping
    ]
    for payload in refusals:
        with pytest.raises(SchemaError):
            _accepts([*records, _envelope(records, PORTION_EVENT, payload)])
    # A batch names the plan of its grouping's portions too (spec §9).
    batch = {
        "grouping_id": grouping_id,
        "level": 0,
        "index": "b1",
        "plan_digest": first["plan_digest"],
        "input_digest": "b" * 64,
        "merges": [{"groups": ["g1", "g2"]}],
        "skipped": 0,
        "requests": 1,
    }
    _accepts([*records, _envelope(records, BATCH_EVENT, batch)])
    with pytest.raises(SchemaError):
        _accepts([*records, _envelope(records, BATCH_EVENT, {**batch, "plan_digest": "e" * 64})])
    # After the grouping's terminal, nothing may name it any more.
    from evolution.tree.campaign_schema import reduce

    snapshot = reduce(records, selector=None, adaptive=True, family_version=15)
    assert snapshot.family15 is not None
    held = snapshot.family15.groupings[grouping_id]
    assert sorted(held.portions) == sorted(
        p["portion_id"] for p in _checkpoints(run, PORTION_EVENT)
    )
    _, result = await _run(tmp_path, monkeypatch, _configure())
    assert result.terminal == "finished"
    finished = _prefix(run)
    with pytest.raises(SchemaError):
        _accepts([*finished, _envelope(finished, PORTION_EVENT, p3)])


@pytest.mark.parametrize(
    "event,payload",
    [
        (
            PORTION_EVENT,
            {
                "grouping_id": "r01-g-" + "0" * 32,
                "portion_id": "p1",
                "plan_digest": "a" * 64,
                "input_digest": "b" * 64,
                "outcome": "invalid_output",
                "requests": 3,
            },
        ),
        (
            PORTION_EVENT,
            {
                "grouping_id": "r01-g-" + "0" * 32,
                "portion_id": "p2.1",
                "plan_digest": "a" * 64,
                "input_digest": "b" * 64,
                # Admission scrubs a text naming an oracle value to empty.
                "groups": [
                    {
                        "id": "p2.1-g1",
                        "title": "",
                        "failure_mode": "",
                        "expected_behaviour": "",
                        "question": "",
                        "priority": "",
                        "citations": [],
                        "metrics": {},
                        "members": [{"evidence_id": "obs:c1:1", "variant": "v"}],
                        "affected_cases": ["c1"],
                    }
                ],
                "requests": 2,
            },
        ),
        (
            BATCH_EVENT,
            {
                "grouping_id": "r01-g-" + "0" * 32,
                "level": 0,
                "index": "b1",
                "plan_digest": "a" * 64,
                "input_digest": "b" * 64,
                "merges": [{"groups": ["g1", "g2"]}],
                "skipped": 0,
                "requests": 1,
            },
        ),
    ],
)
def test_checkpoint_shapes_are_family15_only(event, payload):
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.family15_schema import validate_family15_payload
    from evolution.tree.storage.envelope import Envelope, SchemaError

    validate_family15_payload(event, payload)
    for broken in (
        {k: v for k, v in payload.items() if k != "requests"},
        {**payload, "extra": 1},
        {**payload, "input_digest": "short"},
        {**payload, "requests": -1},
    ):
        with pytest.raises(SchemaError):
            validate_family15_payload(event, broken)
    record = dict(
        schema_version=1,
        run_id="run",
        sequence=2,
        timestamp="2026-10-30T00:00:00Z",
        event=event,
        operation_id=None,
        payload=payload,
    )
    Envelope.from_payload(record, family=campaign_family_for(15))
    # Family 14 refuses both events.
    with pytest.raises(SchemaError):
        Envelope.from_payload(record, family=campaign_family_for(14))
    # Each is operationless.
    with pytest.raises(SchemaError):
        Envelope.from_payload({**record, "operation_id": "op"}, family=campaign_family_for(15))
