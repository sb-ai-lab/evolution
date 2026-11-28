"""Standalone orchestration is independent of agent integrations and campaign editors."""
import dataclasses
from types import SimpleNamespace

import pytest

from evolution.tree import JournalBoundaryError, RunIdentityV1
from evolution.tree.execution.journal_identity import corpus_digest
from evolution.tree.execution.interaction import CandidateIntegrityError, CandidateView, EvalCase, InstanceProvenance, RolloutInfrastructureError, ScoreResult, ScorerIdentity, SemanticIdentity, StaticInputEnvironment, TargetIdentity, TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.candidate_integrity import artifact_digest


def bindings(tmp_path, *, fail=False, repeats=1, outcomes=None, max_turns=1, transient=False):
    root = tmp_path / "agent"; root.mkdir(); (root / "instructions.txt").write_text("fixed")
    candidate = CandidateView(root, artifact_digest(root, ("instructions.txt",)), ("instructions.txt",))
    cases = [EvalCase("c", "g", "synthetic", (), {"text": "question"}, {}, {"answer": "ORACLE-CANARY"})]
    environment = StaticInputEnvironment()
    semantic = SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime")
    target = TargetIdentity(semantic, InstanceProvenance("boot", candidate.digest), frozenset())

    class Instance:
        instance_id = "synthetic-instance"
        sends = 0
        closed = 0
        async def identity(self): return target
        async def open(self, public, seed, request_id):
            assert dict(public) == {"text": "question"}
            return SimpleNamespace(session_id="s", requested_seed=seed, effective_seed=seed)
        async def send(self, session, message, request_id):
            self.sends += 1
            if fail: raise ConnectionError("PRIVATE-ERROR")
            if transient:
                raise RolloutInfrastructureError("PRIVATE-ERROR", "transport_error", retryable=True)
            outcome = "ok" if outcomes is None else outcomes[min(self.sends, len(outcomes)) - 1]
            return TurnResult(events=({"type": "message", "text": "answer"},), usage=Usage(),
                              latency_ms=0, outcome=outcome)
        async def close(self, session): self.closed += 1

    class Scorer:
        identity = ScorerIdentity("synthetic", "fixed")
        calls = 0
        async def score(self, case, view, candidate):
            self.calls += 1
            assert case.oracle["answer"] == "ORACLE-CANARY"
            assert view.history.turns[0].events[0]["text"] == "answer"
            return ScoreResult({"quality": 0.75})

    identity = RunIdentityV1({
        "profile": "rollout_measurement", "candidate_digest": candidate.digest,
        "target": dataclasses.asdict(semantic), "environment": dataclasses.asdict(environment.identity),
        "scorer": {"kind": "synthetic", "digest": "fixed", "recovery": "non_replayable"},
        "evaluation_set_id": "synthetic", "corpus_digest": corpus_digest(cases), "metric_plan_digest": "a" * 64,
        "schedule": [{"case_id": "c", "sample_id": f"sample-{i}", "repeat": i, "requested_seed": i} for i in range(1, repeats + 1)],
        "budget": {"max_turns": max_turns, "timeout_seconds": 10},
        "max_concurrency": 1, "max_scorer_concurrency": 1, "materialization_strategy": "artifact_root_injection",
    })
    return dict(run_directory=tmp_path / "evaluation", run_id="evaluation", identity=identity,
                candidate=candidate, cases=cases, instance=Instance(), environment=environment, scorer=Scorer())


async def test_fixed_agent_evaluation_runs_declared_schedule_without_editor(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    kwargs = bindings(tmp_path, repeats=2)
    result = await evaluate_fixed(**kwargs)
    assert result["coverage"] == {"expected": 2, "scored": 2}
    assert result["stopped_reason"] is None
    assert result["core"]["claim_scope"] == "rollout_measurement"
    assert result["core"]["campaign_outcome"] is None and result["core"]["selection"] is None
    assert [s["metrics"] for s in result["core"]["samples"]] == [{"quality": 0.75}, {"quality": 0.75}]
    assert kwargs["instance"].closed == 2
    kwargs["candidate"].verify()
    assert "ORACLE-CANARY" not in str(result)


async def test_target_failure_stops_with_missing_coverage_not_zero_quality(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    kwargs = bindings(tmp_path, fail=True, repeats=2)
    result = await evaluate_fixed(**kwargs)
    assert result["coverage"] == {"expected": 2, "scored": 0}
    assert result["stopped_reason"] is not None
    assert kwargs["scorer"].calls == 0 and kwargs["instance"].sends == 1
    assert "PRIVATE-ERROR" not in str(result)


async def test_drift_refused_before_journal_creation_or_model_call(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    kwargs = bindings(tmp_path)
    (kwargs["candidate"].root / "instructions.txt").write_text("changed")
    with pytest.raises(CandidateIntegrityError): await evaluate_fixed(**kwargs)
    assert not kwargs["run_directory"].exists() and kwargs["instance"].sends == 0


async def test_existing_evaluation_is_not_silently_replayed(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    kwargs = bindings(tmp_path)
    await evaluate_fixed(**kwargs)
    with pytest.raises(JournalBoundaryError): await evaluate_fixed(**kwargs)
    assert kwargs["instance"].sends == 1


# StaticInputEnvironment offers one message per rollout, so each sample has
# exactly one turn and `max_turns` never binds. Core scores a sample that
# ends on the `candidate_failed` terminal (owner `candidate`), so coverage is
# the same for both thresholds; what the threshold changes is the sample's
# execution outcome: threshold 1 ends the rollout on the failed turn,
# threshold 3 tolerates it and the rollout completes.
@pytest.mark.parametrize("threshold, execution", [
    (1, {"kind": "score", "outcome": "candidate_failed", "owner": "candidate",
         "reason": "candidate_turn_failed"}),
    (3, {"kind": "score", "outcome": "completed", "owner": None, "reason": None}),
])
async def test_fixed_evaluation_applies_the_failed_turn_threshold(tmp_path, threshold, execution):
    from evolution_lab._fixed_execution import measure_fixed
    kwargs = bindings(tmp_path, outcomes=["candidate_failed", "candidate_failed", "ok"], max_turns=3)
    result = await measure_fixed(**kwargs, evaluation_budget={"max_failed_turns": threshold, "call_retries": None})
    assert result["coverage"] == {"expected": 1, "scored": 1}
    assert result["stopped_reason"] is None
    [sample] = result["core"]["samples"]
    assert sample["execution"] == execution
    assert kwargs["instance"].sends == 1


# Core resends a turn only for a typed, retryable transient code (here
# `transport_error`) and only when `turn_max_attempts > 1`; an untyped
# `ConnectionError` is classified transient but never resent. Either way the
# exhausted call is an unmeasured `target_failed` sample once `call_retries`
# is set; PDR-0028 continues the evaluation.
@pytest.mark.parametrize("failure, sends", [({"transient": True}, 3), ({"fail": True}, 1)])
async def test_fixed_evaluation_retries_a_transient_turn_then_continues(tmp_path, failure, sends):
    from evolution_lab._fixed_execution import measure_fixed
    kwargs = bindings(tmp_path, **failure)
    result = await measure_fixed(**kwargs, evaluation_budget={
        "max_failed_turns": 1, "call_retries": {"max_attempts": 3, "turn_max_attempts": 3}})
    assert result["stopped_reason"] is None
    assert result["coverage"] == {"expected": 1, "scored": 0}
    assert kwargs["instance"].sends == sends
    assert "PRIVATE-ERROR" not in str(result)


@pytest.mark.parametrize("failure", [{"transient": True}, {"fail": True}])
async def test_fixed_evaluation_without_budget_keeps_the_previous_behaviour(tmp_path, failure):
    from evolution_lab._fixed_execution import measure_fixed
    kwargs = bindings(tmp_path, **failure)
    result = await measure_fixed(**kwargs)
    assert result["stopped_reason"] == "infrastructure_failed" and kwargs["instance"].sends == 1


# The public `evaluate_fixed` forwards the budget beyond core identity to the
# same preparation `measure_fixed` uses.
async def test_evaluate_fixed_forwards_the_evaluation_budget(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    kwargs = bindings(tmp_path, transient=True)
    result = await evaluate_fixed(**kwargs, evaluation_budget={
        "max_failed_turns": 1, "call_retries": {"max_attempts": 3, "turn_max_attempts": 3}})
    assert result["stopped_reason"] is None and kwargs["instance"].sends == 3
