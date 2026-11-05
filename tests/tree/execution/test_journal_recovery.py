"""Recovery classifies complete evidence without replaying unknown effects."""

import importlib
import importlib.util

import pytest

from evolution.tree.execution.journal_schema import S1_FAMILY
from tests.tree.execution.test_journal_schema import event, ledger


def recovery():
    assert importlib.util.find_spec("evolution.tree.execution.journal_recovery") is not None, (
        "native recovery missing"
    )
    return importlib.import_module("evolution.tree.execution.journal_recovery")


def prefix(outcome=None, scoring=None):
    state = ledger()
    records = [state.created]

    def add(kind, op="op-1", **payload):
        e = event(state, kind, op=op, **payload)
        state.accept(e)
        records.append(e)

    add("rollout_started")
    if outcome:
        kind = (
            "rollout_failed"
            if outcome in ("environment_failed", "infrastructure_failed")
            else "rollout_finished"
        )
        add(kind, outcome=outcome)
    if scoring:
        add(
            "scoring_started",
            op="score-op",
            score_request_id="score-1",
            input_schema_version=1,
            input_digest="a" * 64,
            scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
        )
        if scoring != "started":
            values = (
                {"metrics": {"accuracy": 1}, "critical_pass": None}
                if scoring == "finished"
                else {"failure": "scorer_error"}
                if scoring == "failed"
                else {}
            )
            add("scoring_" + scoring, op="score-op", score_request_id="score-1", **values)
    return records


@pytest.mark.parametrize(
    "outcome,scoring,want",
    [
        (None, None, None),
        ("completed", None, None),
        ("turn_budget", "started", None),
        ("completed", "finished", "completed"),
        ("turn_budget", "finished", "turn_budget"),
        ("completed", "failed", "scorer_failed"),
        ("completed", "interrupted", "interrupted"),
        ("time_budget", None, "time_budget"),
        ("target_failed", None, "target_failed"),
        ("cancelled", None, "cancelled"),
        ("interrupted", None, "interrupted"),
        ("environment_failed", None, "environment_failed"),
        ("infrastructure_failed", None, "infrastructure_failed"),
    ],
)
def test_total_final_disposition(outcome, scoring, want):
    m = recovery()
    plan = m.recovery_plan(prefix(outcome, scoring))
    decision = plan.executions[0]
    assert decision.outcome == outcome
    assert (decision.evaluation.outcome if decision.evaluation else None) == want


def test_unknown_execution_has_one_durable_closure_action_not_score():
    m = recovery()
    records = prefix()
    plan = m.recovery_plan(records)
    assert [(a.event, a.operation_id, a.payload["outcome"]) for a in plan.actions] == [
        ("rollout_interrupted", "op-1", "interrupted")
    ]
    from evolution.tree.storage.envelope import Envelope

    action = plan.actions[0]
    raw = records[-1].to_payload()
    raw.update(sequence=3, event=action.event, payload=action.payload)
    records.append(Envelope.from_payload(raw, family=S1_FAMILY))
    recovered = m.recovery_plan(records)
    assert not recovered.actions
    assert recovered.executions[0].evaluation.reason == "rollout_interrupted"


def test_unknown_scoring_has_core_owned_interruption():
    m = recovery()
    plan = m.recovery_plan(prefix("completed", "started"))
    assert [a.event for a in plan.actions] == ["scoring_interrupted"]
    assert plan.executions[0].outcome == "completed"
    assert plan.executions[0].evaluation is None


def test_legacy_unversioned_events_are_not_accepted():
    m = recovery()
    with pytest.raises(ValueError):
        m.recovery_plan([{"event": "rollout_finished", "terminal": "completed"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deterministic", "idempotent", "non_replayable"])
async def test_explicit_recovery_closes_each_scorer_mode_without_call(tmp_path, mode):
    m = recovery()
    assert hasattr(m, "recover_journal"), "owner recovery executor missing"
    from evolution import __version__
    from evolution.tree.execution.journal import Journal
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.tree.execution.test_journal_identity import cases, identity_payload

    p = identity_payload()
    p["scorer"]["recovery"] = mode
    identity = RunIdentityV1(p)
    kwargs = dict(version_resolver=lambda _: __version__)
    journal = Journal.create(tmp_path / "run", "run", identity, cases(), **kwargs)
    base = dict(case_id="c", sample_id="s1", candidate_digest="candidate")
    journal.append("rollout_started", base, "r")
    journal.append("rollout_finished", base | {"outcome": "completed"}, "r")
    journal.append(
        "scoring_started",
        base
        | dict(
            score_request_id="score",
            scorer=p["scorer"],
            input_digest="a" * 64,
            input_schema_version=1,
        ),
        "s",
    )
    journal.close()
    journal = Journal.resume(tmp_path / "run", "run", identity, cases(), **kwargs)
    try:
        plan = await m.recover_journal(journal)
        assert not journal.recovery_required and not plan.actions
        assert plan.executions[0].outcome == "completed"
        assert plan.executions[0].evaluation.owner == "rollout"
        count = journal.snapshot().sequence
        await m.recover_journal(journal)
        assert journal.snapshot().sequence == count
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_recovery_releases_owned_instance_after_durable_start(tmp_path):
    m = recovery()
    assert hasattr(m, "recover_journal"), "owner recovery executor missing"
    from evolution import __version__
    from evolution.tree.execution.journal import Journal, inspect_journal
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.tree.execution.test_journal_identity import cases, identity_payload

    identity = RunIdentityV1(identity_payload())
    kwargs = dict(version_resolver=lambda _: __version__)
    journal = Journal.create(tmp_path / "run", "run", identity, cases(), **kwargs)
    journal.append(
        "target_materialize_started",
        dict(
            instance_id="owned",
            candidate_digest="candidate",
            strategy="filesystem_overlay",
            retry_ordinal=0,
        ),
        "m",
    )
    journal.close()
    journal = Journal.resume(tmp_path / "run", "run", identity, cases(), **kwargs)
    calls = []

    class Adapter:
        async def release(self, instance):
            assert inspect_journal(tmp_path / "run").records[-1].event == "target_release_started"
            calls.append(instance)

    try:
        plan = await m.recover_journal(journal, Adapter())
        assert calls == ["owned"] and not plan.cleanup and not plan.actions
        await m.recover_journal(journal, Adapter())
        assert calls == ["owned"]
    finally:
        journal.close()


def prefix_retried(first_outcome="target_failed", second_pending=True, scoring=None):
    """A journal whose identity admits one retry: a closed try 0, then try 1."""
    from tests.tree.execution.test_journal_schema import SCORING_START, accept, retried_ledger

    state = retried_ledger()
    records = [state.created]

    def add(kind, op, **payload):
        records.append(accept(state, kind, op, retry_ordinal=records_ordinal[0], **payload))

    records_ordinal = [0]
    add("rollout_started", "r0")
    add("rollout_finished", "r0", outcome=first_outcome)
    records_ordinal[0] = 1
    add("rollout_started", "r1")
    if not second_pending:
        add("rollout_finished", "r1", outcome="completed")
        if scoring:
            add("scoring_started", "s1", **SCORING_START)
    return records


def test_recovery_interrupts_the_pending_try_with_its_ordinal():
    m = recovery()
    plan = m.recovery_plan(prefix_retried(first_outcome="target_failed", second_pending=True))
    (action,) = [a for a in plan.actions if a.event == "rollout_interrupted"]
    assert action.payload["retry_ordinal"] == 1
    assert plan.executions[0].outcome is None


def test_recovery_interrupts_the_pending_scoring_with_its_ordinal_and_reads_the_latest_try():
    m = recovery()
    records = prefix_retried(second_pending=False, scoring="started")
    plan = m.recovery_plan(records)
    (action,) = plan.actions
    assert action.event == "scoring_interrupted" and action.payload["retry_ordinal"] == 1
    assert plan.executions[0].outcome == "completed"


def test_recovery_without_sample_retries_writes_no_ordinal():
    m = recovery()
    for action in m.recovery_plan(prefix("completed", "started")).actions:
        assert "retry_ordinal" not in action.payload
    for action in m.recovery_plan(prefix()).actions:
        assert "retry_ordinal" not in action.payload
