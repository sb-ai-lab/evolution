"""Real A1 HTTP path, with a synthetic domain backend and no model provider."""
import os
from pathlib import Path
from threading import Thread

import pytest
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.target.config import TargetBinding
from evolution.tree.execution.interaction import CallRetries, EvalCase, RolloutBudget, ScoreResult, ScorerIdentity, ScriptedEnvironment, TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.target.target_server import BackendReply, TargetServer, serve_target


@pytest.fixture
def connected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "instructions.txt").write_text("original")
    policy = ArtifactPolicyV1((), (), ("instructions.txt",))
    seen, closed, controls, loaded = [], [], {}, []

    class Session:
        effective_seed = None
        def __init__(self, instance_id): self.instance_id = instance_id
        def turn(self, message):
            seen.append(message.content)
            if "turn" in controls:
                return controls["turn"](self.instance_id, message)
            return BackendReply(TurnResult(events=({"type": "assistant_message", "content": "answer"},),
                                            usage=Usage(), latency_ms=0, outcome="ok"))
        def close(self): closed.append("session")

    class Instance:
        def __init__(self, instance_id): self.instance_id = instance_id
        def open(self, public, seed):
            assert dict(public) == {"history": []} and seed is None
            return Session(self.instance_id)
        def close(self): closed.append("instance")

    class Backend:
        def load(self, tree):
            loaded.append(tree.root)
            assert tree.root != source
            assert (tree.root / "instructions.txt").read_text() == "original"
            if "load" in controls: controls["load"](tree)
            return Instance(tree.instance_id)

    target = TargetServer(Backend(), allowed_artifact_root=tmp_path, policy=policy,
                          seed_mode="none", target_id="synthetic", contract_digest="contract",
                          settings_hash="settings", runtime_ref_digest="runtime")
    http = serve_target(target, port=0)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield dict(source=source, policy=policy, seen=seen, closed=closed, controls=controls, loaded=loaded,
                   target=TargetBinding(f"http://127.0.0.1:{http.server_port}", "none", "contract", "settings"))
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        target.close()


def arguments(tmp_path, connected):
    from evolution_lab.provenance import WheelPin
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    if not wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL to the installed producing wheel")

    class Scorer:
        identity = ScorerIdentity("synthetic", "scorer-1")
        async def score(self, case, view, candidate):
            assert view.history.turns[0].events[0]["content"] == "answer"
            assert case.oracle["expected"] == "PRIVATE-ORACLE"
            return ScoreResult({"quality": .75})

    return dict(source_root=connected["source"], policy=connected["policy"], target=connected["target"],
                run_directory=tmp_path / "run", run_id="standalone", evaluation_set_id="synthetic",
                cases=(EvalCase("case", "group", "train", (), {"history": []},
                                {"script": [{"content": "question", "fields": {}}]},
                                {"expected": "PRIVATE-ORACLE"}),),
                environment=ScriptedEnvironment(), scorer=Scorer(), repeat_seeds=(None, None),
                budget=RolloutBudget(max_turns=1, timeout_seconds=10),
                metric_plan={"primary": "quality"}, wheels=(WheelPin.capture(Path(wheel)),),
                dispatch_guard=lambda: None)


async def test_http_standalone_copies_measures_reports_and_releases(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    from evolution_lab.report import read_evaluation_report
    args = arguments(tmp_path, connected)
    result = await evaluate_target(**args)
    assert result["coverage"] == {"expected": 2, "scored": 2}
    assert connected["seen"] == ["question", "question"]
    assert connected["closed"] == ["session", "session", "instance"]
    report = read_evaluation_report(tmp_path / "run/measurement")
    assert report.data["core"]["campaign_outcome"] is None
    assert "PRIVATE-ORACLE" not in str(result)
    assert (connected["source"] / "instructions.txt").read_text() == "original"
    with pytest.raises(FileExistsError): await evaluate_target(**args)
    assert len(connected["seen"]) == 2


async def test_refused_dispatch_creates_no_output_and_runs_no_target(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    from evolution_lab.budget import BudgetExhausted
    args = arguments(tmp_path, connected)
    def refuse(): raise BudgetExhausted("no allowance")
    args["dispatch_guard"] = refuse
    with pytest.raises(BudgetExhausted): await evaluate_target(**args)
    assert not (tmp_path / "run").exists() and connected["seen"] == []


async def test_failed_scorer_still_releases_instance(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    from evolution.tree.execution.interaction import ScoringError
    args = arguments(tmp_path, connected)
    async def fail(*args): raise ScoringError("synthetic")
    args["scorer"].score = fail
    result = await evaluate_target(**args)
    assert result["stopped_reason"] is None
    assert result["coverage"] == {"expected": 2, "scored": 0}
    assert result["unmeasured"] == [
        {"case_id": "case", "sample_id": "case-1-repeat-1", "outcome": "scorer_failed", "tries": 1},
        {"case_id": "case", "sample_id": "case-1-repeat-2", "outcome": "scorer_failed", "tries": 1},
    ]
    assert connected["closed"] == ["session", "session", "instance"]


async def test_guard_blocks_next_sample_and_preserves_partial_measurement(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    from evolution_lab.budget import BudgetExhausted
    args = arguments(tmp_path, connected)
    original_score = args["scorer"].score
    scored = []
    async def score(*values):
        result = await original_score(*values)
        scored.append(True)
        return result
    args["scorer"].score = score
    def guard():
        if scored: raise BudgetExhausted("limit reached")
    args["dispatch_guard"] = guard
    result = await evaluate_target(**args)
    assert result["coverage"] == {"expected": 2, "scored": 1}
    assert result["stopped_reason"] == "budget_exhausted"
    assert connected["seen"] == ["question"]
    assert connected["closed"] == ["session", "instance"]


async def test_cancellation_is_not_lost_when_constructing_identity(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    args = arguments(tmp_path, connected)
    args["budget"] = RolloutBudget(max_turns=1, timeout_seconds=10, cancelled=lambda: True)
    result = await evaluate_target(**args)
    assert result["coverage"]["scored"] == 0
    assert result["stopped_reason"] is not None
    assert connected["seen"] == []
    assert connected["closed"][-1] == "instance"


async def test_seed_mismatch_is_refused_before_any_output(tmp_path, connected):
    from evolution_lab.standalone import evaluate_target
    args = arguments(tmp_path, connected)
    args["repeat_seeds"] = (42,)
    with pytest.raises(ValueError, match="seed"): await evaluate_target(**args)
    assert not (tmp_path / "run").exists()
    assert connected["seen"] == []


async def test_input_alias_mutation_does_not_change_declared_plan(tmp_path, connected):
    import json
    from evolution_lab.standalone import evaluate_target
    args = arguments(tmp_path, connected)
    seeds, metric = [None, None], {"primary": "quality"}
    args.update(repeat_seeds=seeds, metric_plan=metric)
    def mutate_aliases():
        seeds[:] = [None]
        metric["primary"] = "unapproved"
    args["dispatch_guard"] = mutate_aliases
    result = await evaluate_target(**args)
    assert result["coverage"] == {"expected": 2, "scored": 2}
    saved = json.loads((tmp_path / "run/evaluation-inputs.json").read_text())
    assert saved["metric_plan"] == {"primary": "quality"}
    assert saved["identity"]["row_provenance"] == 1


async def test_release_failure_is_visible_in_lab_result(tmp_path, connected, monkeypatch):
    import json
    from evolution_lab.standalone import evaluate_target
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution_lab.report import read_evaluation_report
    real_release = HttpSessionAdapter.release
    async def release(self, instance_id):
        await real_release(self, instance_id)
        raise RuntimeError("PRIVATE-CLEANUP-ERROR")
    monkeypatch.setattr(HttpSessionAdapter, "release", release)
    args = arguments(tmp_path, connected)
    with pytest.raises(RuntimeError):
        await evaluate_target(**args)
    saved = (args["run_directory"] / "evaluation-result.json").read_text()
    assert json.loads(saved)["stopped_reason"] == "exception"
    assert "PRIVATE-CLEANUP-ERROR" not in saved
    report = read_evaluation_report(args["run_directory"] / "measurement")
    assert report.data["lab_result"]["stopped_reason"] == "exception"


# The caller's RolloutBudget beyond core identity reaches the measurement. A
# backend exception is answered `internal` (HTTP 500), which the adapter types
# as a retryable transient; without `call_retries` it stops the evaluation as
# `infrastructure_failed` after one send. Exhausted call retries leave each
# sample unmeasured, so both samples run without reaching the three-sample stop.
@pytest.mark.parametrize("call_retries, stopped, sends", [
    (None, "infrastructure_failed", 1),
    (CallRetries(max_attempts=3, turn_max_attempts=3), None, 6),
])
async def test_standalone_applies_the_callers_call_retries(tmp_path, connected, call_retries, stopped, sends):
    from evolution_lab.standalone import evaluate_target
    from evolution.tree.target.target_server import BackendError
    def fail(instance_id, message): raise BackendError("PRIVATE-BACKEND-ERROR")
    connected["controls"]["turn"] = fail
    args = arguments(tmp_path, connected)
    args["budget"] = RolloutBudget(max_turns=1, timeout_seconds=10, call_retries=call_retries)
    result = await evaluate_target(**args)
    assert result["stopped_reason"] == stopped
    assert result["coverage"] == {"expected": 2, "scored": 0}
    assert len(connected["seen"]) == sends
    if call_retries is not None:
        assert result["unmeasured"] == [
            {"case_id": "case", "sample_id": "case-1-repeat-1", "outcome": "target_failed", "tries": 1},
            {"case_id": "case", "sample_id": "case-1-repeat-2", "outcome": "target_failed", "tries": 1},
        ]
        assert connected["closed"] == ["session", "session", "instance"]
    assert "PRIVATE-BACKEND-ERROR" not in str(result)


# One scripted message per rollout: threshold 1 ends the sample on the failed
# turn, threshold 3 tolerates it and the rollout completes.
@pytest.mark.parametrize("threshold, outcome", [(1, "candidate_failed"), (3, "completed")])
async def test_standalone_applies_the_callers_failed_turn_threshold(tmp_path, connected, threshold, outcome):
    from evolution_lab.standalone import evaluate_target
    connected["controls"]["turn"] = lambda instance_id, message: BackendReply(TurnResult(
        events=({"type": "assistant_message", "content": "answer"},), usage=Usage(), latency_ms=0,
        outcome="candidate_failed"))
    args = arguments(tmp_path, connected)
    args["budget"] = RolloutBudget(max_turns=3, timeout_seconds=10, max_failed_turns=threshold)
    result = await evaluate_target(**args)
    assert result["coverage"] == {"expected": 2, "scored": 2}
    assert [s["execution"]["outcome"] for s in result["core"]["samples"]] == [outcome, outcome]


# PDR-0014: the applied budget is part of the evaluation's durable record,
# written before execution and repeated in the Lab result.
@pytest.mark.parametrize("budget, recorded", [
    (RolloutBudget(max_turns=1, timeout_seconds=10, max_failed_turns=1),
     {"max_failed_turns": 1, "call_retries": None}),
    (RolloutBudget(max_turns=1, timeout_seconds=10, max_failed_turns=3,
                   call_retries=CallRetries(max_attempts=2, turn_max_attempts=4)),
     {"max_failed_turns": 3, "call_retries": {"max_attempts": 2, "turn_max_attempts": 4}}),
])
async def test_standalone_records_the_applied_budget(tmp_path, connected, budget, recorded):
    import json
    from evolution_lab.standalone import evaluate_target
    from evolution_lab.report import read_evaluation_report
    args = arguments(tmp_path, connected)
    args["budget"] = budget
    result = await evaluate_target(**args)
    assert result["evaluation_budget"] == recorded
    inputs = json.loads((tmp_path / "run/evaluation-inputs.json").read_text())
    assert inputs["evaluation_budget"] == recorded
    outcome = json.loads((tmp_path / "run/evaluation-result.json").read_text())
    assert outcome["evaluation_budget"] == recorded
    report = read_evaluation_report(tmp_path / "run/measurement")
    assert report.data["lab_result"]["evaluation_budget"] == recorded


async def test_standalone_returns_partial_unanswered_without_changing_saved_receipts(tmp_path, connected, monkeypatch):
    from evolution_lab.standalone import evaluate_target
    from test_unanswered_count import attach_public_api_traces
    import json
    args = arguments(tmp_path, connected)
    attach_public_api_traces(monkeypatch, args["scorer"], args["run_directory"] / "measurement", [False, None])
    result = await evaluate_target(**args)
    assert {key: result.get(key) for key in ("unanswered", "unanswered_state", "answered_reported")} == {
        "unanswered": 1, "unanswered_state": "partial", "answered_reported": 1}
    saved = json.loads((args["run_directory"] / "evaluation-result.json").read_text())
    assert saved == {"version": "lab-evaluation-result/2", "run_id": "standalone", "coverage": None,
                     "unmeasured": None, "stopped_reason": None, "min_coverage": 1.0,
                     "evaluation_budget": {"max_failed_turns": 1, "call_retries": None}}
    measurement_saved = json.loads((args["run_directory"] / "measurement/evaluation-result.json").read_text())
    assert measurement_saved == {"version": "lab-evaluation-result/2", "run_id": "standalone",
                                 "coverage": {"expected": 2, "scored": 2}, "unmeasured": [],
                                 "stopped_reason": None, "min_coverage": 1.0}
