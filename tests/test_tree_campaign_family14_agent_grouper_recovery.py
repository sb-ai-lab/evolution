"""H9 A§5.4 cuts over genuine agent CLI campaigns, through the H12-free seam.

Cuts after starts are deliberately inside the role operation, after a real CLI
has returned but before admission. BaseException escapes the role's exception
classification. Other cuts follow genuine durable campaign records; no journal
is constructed or rewritten here. Every settled prefix and recovery is audited.
"""

from __future__ import annotations

import json

import pytest

from evolution.llm.agent_backend import AgentBackend
from evolution.tree import campaign as campaign_module
from evolution.tree.campaign_view import FinishCampaign
from tests.agent_grouping_fakes import scripted_grouping_cli
from tests.campaign_drivers import _PowerCut
from tests.test_tree_campaign_family14_agent_grouper_chain import (
    agent_chain_run,
    assert_caveats,
    assert_second_link,
)
from tests.test_tree_campaign_family14_grouper_recovery import (
    _assert_clean,
    _cut,
    _journal,
    _of,
    _only_the_planner,
    _spy_planner,
    _spy_resumed_causes,
)
from tests.test_tree_campaign_family14_visibility_recovery import _assert_same_structure

pytestmark = pytest.mark.slow

UNKNOWN = ("agent_error", "timeout", "exception")
CONTENT = ("invalid_output", "turns_exhausted")


def calls(root):
    path = root / "grouper-invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def scenario(root, monkeypatch, *modes):
    """The exception seam runs the real CLI before raising a backend defect.

    Timeout uses the actual deadline over a sleeping process. All steps are
    sealed once, so resume never changes the declared program or its identity.
    """
    if "exception" in modes:
        original = AgentBackend.complete_file_edit
        seen = 0

        async def complete(self, *args, **kwargs):
            nonlocal seen
            result = await original(self, *args, **kwargs)
            if not args[0].is_relative_to(root / "run"):
                return result
            index = seen
            seen += 1
            if modes[index] == "exception":
                raise RuntimeError("backend failed after its real CLI returned")
            return result

        monkeypatch.setattr(AgentBackend, "complete_file_edit", complete)
    return scripted_grouping_cli(
        root,
        *({"exception": "admitted", "timeout": "hang"}.get(mode, mode) for mode in modes),
        timeout_seconds=10,
    )


def assert_terminals(run):
    """Each durable start owns exactly one terminal, with the same grouping id."""
    starts = _of(run, "grouping_started")
    assert len({row["payload"]["grouping_id"] for row in starts}) == len(starts)
    for start in starts:
        terminals = [
            row
            for row in _journal(run)
            if row["operation_id"] == start["operation_id"]
            and row["event"] in ("grouping_failed", "grouping_finished")
        ]
        assert len(terminals) == 1
        assert terminals[0]["payload"]["grouping_id"] == start["payload"]["grouping_id"]


def assert_same_agent_structure(run, plain, *, dropped_operations=frozenset(), **kwargs):
    """Compare H8 ordering plus agent calls and grouping/baseline identity links.

    Independent runs generate different UUIDs and binding digests. First-seen
    ordinals preserve their equality relationships, including repeated binding
    across regrouping and fresh bindings on refresh; numerical bytes differ.
    """
    _assert_same_structure(run, plain, dropped_operations=dropped_operations, **kwargs)

    def groupings(path, dropped):
        identities = {}
        baselines = {}
        rows = []
        for row in _journal(path):
            if row["operation_id"] in dropped or row["event"] not in (
                "grouping_started",
                "grouping_finished",
                "grouping_failed",
            ):
                continue
            payload = row["payload"]
            identity = payload["grouping_id"]
            number = identities.setdefault(identity, len(identities))
            binding = payload.get("baseline_binding")
            baseline = None if binding is None else baselines.setdefault(binding, len(baselines))
            rows.append(
                (
                    row["event"],
                    number,
                    baseline,
                    {
                        key: payload[key]
                        for key in (
                            "round",
                            "grouper_kind",
                            "code",
                            "reason",
                            "error_class",
                            "calls",
                            "fallback",
                        )
                        if key in payload
                    },
                )
            )
        return rows

    assert groupings(run, dropped_operations) == groupings(plain, frozenset())


def assert_regrouped(first, second):
    assert first["operation_id"] != second["operation_id"]
    assert first["payload"]["grouping_id"] != second["payload"]["grouping_id"]
    assert {**first["payload"], "grouping_id": None} == {
        **second["payload"],
        "grouping_id": None,
    }


async def control(root, monkeypatch, modes, rounds, *, resume=False):
    path = root / "uncut"
    path.mkdir()
    with monkeypatch.context() as patch:
        section = scenario(path, patch, *modes)
        run, snapshot = await agent_chain_run(path, patch, section, rounds)
        if resume:
            run, snapshot = await agent_chain_run(path, patch, section, rounds)
        _assert_clean(run, snapshot)
        assert_terminals(run)
    return run


async def test_cut_in_agent_operation_after_real_cli_runs_again(tmp_path, monkeypatch):
    section = scripted_grouping_cli(tmp_path, "admitted", "admitted")
    original = AgentBackend.complete_file_edit

    async def power_cut(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        assert len(calls(tmp_path)) == 1
        raise _PowerCut("CLI returned; grouping has no terminal yet")

    with monkeypatch.context() as patch:
        patch.setattr(AgentBackend, "complete_file_edit", power_cut)
        with pytest.raises(_PowerCut):
            await agent_chain_run(tmp_path, patch, section, 1)
    run = tmp_path / "run"
    (opened,) = _of(run, "grouping_started")
    assert _journal(run)[-1] == opened
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 1)
    first, second = _of(run, "grouping_started")
    assert_regrouped(first, second)
    (failed,) = _of(run, "grouping_failed")
    assert failed["operation_id"] == opened["operation_id"]
    assert failed["payload"]["code"] == "interrupted"
    assert _journal(run).index(failed) < _journal(run).index(second)
    assert len(calls(tmp_path)) == 2
    assert len({call["root"] for call in calls(tmp_path)}) == 2
    assert snapshot.finished["outcome"] == "completed"
    assert not _of(run, "campaign_suspended")
    assert_terminals(run)
    _assert_clean(run, snapshot)
    plain = await control(tmp_path, monkeypatch, ("admitted",), 1)
    assert_same_agent_structure(run, plain, dropped_operations={opened["operation_id"]})


@pytest.mark.parametrize("owed_by", ["resumed_cause", "planner"])
@pytest.mark.parametrize("reason", [*UNKNOWN, *CONTENT])
async def test_owed_suspension_makes_no_cli_call_then_next_resume_regroups(
    tmp_path, monkeypatch, reason, owed_by
):
    rounds = 2 if reason in CONTENT else 1
    modes = (reason, reason) if reason in CONTENT else (reason,)
    with monkeypatch.context() as setup:
        section = scenario(tmp_path, setup, *modes, "admitted")
        await _cut(
            lambda patch: agent_chain_run(tmp_path, patch, section, rounds),
            setup,
            "after_grouping_failed",
        )
        run = tmp_path / "run"
        before = _journal(run)
        failed = before[-1]
        assert failed["event"] == "grouping_failed"
        assert failed["payload"]["code"] == "role_failed"
        assert failed["payload"]["reason"] == reason
        assert failed["payload"]["error_class"] == ("content" if reason in CONTENT else "unknown")
        # A crash prefix owing suspension is itself valid evidence.
        from tests.test_tree_campaign_family14_audit import audit

        assert audit(run)["mismatches"] == []
        invoked = calls(tmp_path)
        with setup.context() as patch:
            if owed_by == "planner":
                _only_the_planner(patch)
            else:
                causes = _spy_resumed_causes(patch)
            planned = _spy_planner(patch)
            run, snapshot = await agent_chain_run(tmp_path, patch, section, rounds)
        after = _journal(run)
        assert after[: len(before)] == before
        (suspension,) = after[len(before) :]
        assert suspension["event"] == "campaign_suspended"
        assert suspension["payload"] == {
            "reason": "grouper_failing",
            "owner": "grouper",
            "evidence": {
                "evidence_kind": "stage_record",
                "seq": failed["sequence"],
                "operation_id": failed["operation_id"],
            },
        }
        assert calls(tmp_path) == invoked
        if owed_by == "planner":
            assert planned[0] == (
                FinishCampaign("infrastructure_failed", "grouper_failing", "none"),
            )
        else:
            assert causes == ["grouper_failing"] and planned == []
        assert snapshot.finished is None and snapshot.grouper_chain == 0
        assert_terminals(run)
        _assert_clean(run, snapshot)
        plain = await control(tmp_path, setup, modes, rounds)
        assert_same_agent_structure(run, plain)
        previous = _of(run, "grouping_started")[-1]
        run, snapshot = await agent_chain_run(tmp_path, setup, section, rounds)
        assert_regrouped(previous, _of(run, "grouping_started")[-1])
        assert len(calls(tmp_path)) == len(invoked) + 1
        assert snapshot.finished["outcome"] == "completed"
        assert_terminals(run)
        _assert_clean(run, snapshot)


def cut_after_suspension(monkeypatch):
    original = campaign_module._Campaign.append

    def append(self, event, *args, **kwargs):
        result = original(self, event, *args, **kwargs)
        if event == "campaign_suspended":
            raise _PowerCut("suspension is durable")
        return result

    monkeypatch.setattr(campaign_module._Campaign, "append", append)


@pytest.mark.parametrize("reason", UNKNOWN)
async def test_cut_after_suspension_repeated_failure_then_repair(tmp_path, monkeypatch, reason):
    with monkeypatch.context() as setup:
        section = scenario(tmp_path, setup, reason, reason, "admitted")
        with setup.context() as patch:
            cut_after_suspension(patch)
            with pytest.raises(_PowerCut):
                await agent_chain_run(tmp_path, patch, section, 1)
        run = tmp_path / "run"
        assert _journal(run)[-1]["event"] == "campaign_suspended"
        run, snapshot = await agent_chain_run(tmp_path, setup, section, 1)
        assert snapshot.finished is None
        starts = _of(run, "grouping_started")
        assert_regrouped(*starts)
        assert len(calls(tmp_path)) == 2
        for start, failed, suspended in zip(
            starts, _of(run, "grouping_failed"), _of(run, "campaign_suspended"), strict=True
        ):
            rows = _journal(run)
            index = rows.index(start)
            assert rows[index : index + 3] == [start, failed, suspended]
            assert failed["payload"]["reason"] == reason
            assert suspended["payload"]["evidence"]["seq"] == failed["sequence"]
        _assert_clean(run, snapshot)
        run, snapshot = await agent_chain_run(tmp_path, setup, section, 1)
        assert_regrouped(starts[-1], _of(run, "grouping_started")[-1])
        assert len(calls(tmp_path)) == 3
        assert snapshot.finished["outcome"] == "completed"
        assert_terminals(run)
        _assert_clean(run, snapshot)
    plain = await control(tmp_path, monkeypatch, ("admitted",), 1)
    assert_same_agent_structure(
        run,
        plain,
        dropped_operations={start["operation_id"] for start in starts},
        dropped_events={"campaign_suspended"},
    )


@pytest.mark.parametrize("mode", [*CONTENT, "partial"])
async def test_cut_after_fallback_reuses_durable_result_without_cli(tmp_path, monkeypatch, mode):
    section = scripted_grouping_cli(tmp_path, mode)
    await _cut(
        lambda patch: agent_chain_run(tmp_path, patch, section, 1),
        monkeypatch,
        "after_grouping_finished",
    )
    run = tmp_path / "run"
    (finished,) = _of(run, "grouping_finished")
    assert _journal(run)[-1] == finished
    invoked = calls(tmp_path)
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 1)
    assert calls(tmp_path) == invoked
    assert _of(run, "grouping_finished") == [finished]
    assert len(_of(run, "grouping_started")) == 1
    assert snapshot.finished["outcome"] == "completed"
    assert_caveats(
        run,
        snapshot,
        [
            (
                1,
                "invalid_output" if mode == "partial" else mode,
                "uncovered" if mode == "partial" else "all",
            )
        ],
    )
    assert_terminals(run)
    _assert_clean(run, snapshot)
    plain = await control(tmp_path, monkeypatch, (mode,), 1)
    assert_same_agent_structure(run, plain)


@pytest.mark.parametrize("reason", CONTENT)
async def test_cut_after_first_fallback_preserves_chain(tmp_path, monkeypatch, reason):
    section = scripted_grouping_cli(tmp_path, reason, reason)
    await _cut(
        lambda patch: agent_chain_run(tmp_path, patch, section, 2),
        monkeypatch,
        "after_grouping_finished",
    )
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 2)
    assert_second_link(run, snapshot, 2, reason)
    assert len({call["root"] for call in calls(tmp_path)}) == 2
    assert_caveats(run, snapshot, [(1, reason, "all")])
    assert_terminals(run)
    _assert_clean(run, snapshot)
    plain = await control(tmp_path, monkeypatch, (reason, reason), 2)
    assert_same_agent_structure(run, plain)


@pytest.mark.parametrize("reason", CONTENT)
async def test_cut_after_second_link_suspension_resets_chain(tmp_path, monkeypatch, reason):
    section = scripted_grouping_cli(tmp_path, reason, reason, reason)
    with monkeypatch.context() as patch:
        cut_after_suspension(patch)
        with pytest.raises(_PowerCut):
            await agent_chain_run(tmp_path, patch, section, 2)
    run = tmp_path / "run"
    failed_start = _of(run, "grouping_started")[-1]
    assert _journal(run)[-1]["event"] == "campaign_suspended"
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 2)
    assert_regrouped(failed_start, _of(run, "grouping_started")[-1])
    assert snapshot.finished["outcome"] == "completed" and snapshot.grouper_chain == 1
    assert_caveats(run, snapshot, [(1, reason, "all"), (2, reason, "all")])
    assert len({call["root"] for call in calls(tmp_path)}) == 3
    assert_terminals(run)
    _assert_clean(run, snapshot)
    plain = await control(tmp_path, monkeypatch, (reason, reason, reason), 2, resume=True)
    assert_same_agent_structure(run, plain)
