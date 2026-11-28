"""Package-defined trace observations travel through the existing /16 audiences."""

from __future__ import annotations

import json
import threading
from contextlib import closing, contextmanager

import pytest

from evolution.tree.campaign_statistics import latest_rows
from evolution.tree.evolve import evolve
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.execution.scorer_traces import ScorerTraceReader
from evolution.tree.target.target_server import TargetServer, serve_target
from tests import fake_chat_server
from tests.campaign_drivers import (
    _measurement_events,
    _records,
    audit_mismatches,
    two_case_manifest,
)
from tests.campaign_fakes import family14_document
from tests.family15_fakes import (
    DelegationAgent,
    FeedbackTarget,
    ResultCheckingAgent,
    feedback_scorer,
    v16_document,
)
from tests.test_tree_campaign_family14_reflector_history import chat_reflector
from tests.test_tree_campaign_family14_role_feedback import chat_budget_section
from tests.test_tree_campaign_family14_run import payloads
from tests.tree_evolve_support import POLICY

pytestmark = pytest.mark.slow

CASES = {"complete": 0.25, "diagnostic-gap": 0.5, "candidate-defect": 0.0}
SKILL = "skills/SKILL.md"
ORACLE = "RAW-ORACLE-REFERENCE"
HELD_OUT = "HELD-OUT-PUBLIC-INPUT"


def _row(case_id, score=0.0, *, split="train", text=None):
    return {
        "case_id": case_id,
        "group_id": case_id,
        "stratum": "fixture",
        "split": split,
        "public": {"text": text or case_id},
        "oracle": {"partial_credit": score, "secret": ORACLE},
    }


@contextmanager
def _target(root, agent):
    target = TargetServer(
        FeedbackTarget(agent),
        allowed_artifact_root=root,
        policy=POLICY,
        seed_mode="exact",
        target_id="package-feedback",
        contract_digest="package-feedback-contract",
        settings_hash="package-feedback-settings",
        runtime_ref_digest="package-feedback-runtime",
    )
    with closing(target), serve_target(target) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join(timeout=10)


def _manifest(root, url, agent, rows):
    path = two_case_manifest(root, rows=rows)
    document = family14_document(json.loads(path.read_text()), repeats=1, stability="stable")
    document["schema"] = "evolution-v2-manifest/16"
    document["target"]["base_url"] = url
    document["campaign"].update(rounds=1, attempts_per_round=1, sample_retries=0)
    document["campaign"]["budget"]["max_failed_turns"] = 1
    document["campaign"]["metric"]["primary"] = "task_quality"
    document["campaign"]["gate"] = v16_document()["campaign"]["gate"]
    document["grouper"] = {
        "kind": "fields",
        "features": [
            {
                "name": agent.observation,
                "source": "diagnostics",
                "path": "/" + agent.observation,
                "description": "Observed behavior from the package trace.",
            }
        ],
    }
    # The scorer reads the uncut history. The role view retains the trace's ends.
    document["evidence"] = {"version": "evidence/1", "events_per_turn": 3}
    (root / "scorer/score.py").write_text(feedback_scorer(agent))
    return path, document


def _opening(body):
    return next(m["content"] for m in body["messages"] if m["role"] == "user")


def _opening_json(body):
    (line,) = [line for line in _opening(body).splitlines() if line.startswith("{")]
    return json.loads(line)


def _tool_pages(body, tool):
    return [
        json.loads(message["content"])
        for message in body["messages"]
        if message["role"] == "tool"
        and (
            message["tool_call_id"] == f"call-{tool}"
            or message["tool_call_id"].startswith(f"call-{tool}-")
        )
    ]


class _RoleProvider:
    """Script only the model answers. All reads and writes use the native tools."""

    def __init__(self, agent):
        self.agent = agent
        self.reads = {}
        self.editor_requests = []

    def __call__(self, body):
        tools = [tool["function"]["name"] for tool in body["tools"]]
        if tools == ["list_files"]:
            return fake_chat_server.tool_call("list_files")
        if "finish_reflection" in tools:
            return self.reflect(body)
        return self.edit(body)

    def reflect(self, body):
        opening = _opening_json(body)
        ids = {entry["case_id"]: entry["exemplars"][0] for entry in opening["portion"]}
        # The gap is in another neighborhood, but the baseline catalog can locate it.
        gap_pages = _tool_pages(body, "list_evidence")
        if not gap_pages:
            gap_handle = next(
                entry["case_handle"]
                for entry in opening["evidence_catalog"]["entries"]
                if entry["case_id"] == "diagnostic-gap"
            )
            return fake_chat_server.tool_call("list_evidence", {"case_id": gap_handle})
        (gap,) = gap_pages[0]["entries"]
        citations = {**ids, "diagnostic-gap": gap["evidence_id"]}
        for page in _tool_pages(body, "read_evidence"):
            assert page["next_offset"] is None
            record = json.loads(page["content"])
            self.reads[record["identity"]["case_id"]] = record
        for case_id, evidence_id in citations.items():
            if case_id not in self.reads:
                response = fake_chat_server.tool_call("read_evidence", {"evidence_id": evidence_id})
                response["choices"][0]["message"]["tool_calls"][0]["id"] += f"-{case_id}"
                return response
        observed = self.reads["complete"]["diagnostics"][self.agent.observation]
        gap_observed = self.reads["diagnostic-gap"]["diagnostics"][self.agent.observation]
        return fake_chat_server.tool_call(
            "finish_reflection",
            {
                "groups": [
                    {
                        "root_cause": f"The trace reports {observed} before an unchecked reply.",
                        "suggested_fix": "Check the result before replying.",
                        "evidence_ids": list(citations.values()),
                        "affected_cases": list(ids),
                        "target_paths": [SKILL],
                        "counterevidence": f"The partial trace leaves the behavior {gap_observed}.",
                        "regression_risks": "Preserve replies when trace capture is partial.",
                        "why_this_layer": "The skill controls when the agent checks its result.",
                    }
                ]
            },
        )

    def edit(self, body):
        self.editor_requests.append(body)
        calls = {
            call["function"]["name"]
            for message in body["messages"]
            for call in message.get("tool_calls") or ()
        }
        if "read_evidence" not in calls:
            return fake_chat_server.tool_call("read_evidence", {"evidence_id": "diagnosis"})
        if "read_file" not in calls:
            return fake_chat_server.tool_call(
                "read_file", {"path": SKILL, "start_line": 1, "end_line": 1}
            )
        if "record_plan" not in calls:
            (diagnosis,) = _tool_pages(body, "read_evidence")
            complete = next(
                entry["evidence_id"]
                for entry in _opening_json(body)["cited"]
                if entry["observation"]["feedback"][self.agent.observation] == self.agent.signal
                and entry["observation"]["turns"][0]["input"]["content"] == "complete"
            )
            assert complete in diagnosis["content"]
            return fake_chat_server.tool_call(
                "record_plan",
                {
                    "summary": "Check results before replying",
                    "changes": [
                        {
                            "path": SKILL,
                            "action": "update",
                            "purpose": f"From {complete}, check the tool result before replying.",
                        }
                    ],
                },
            )
        if "write_file" not in calls:
            return fake_chat_server.tool_call(
                "write_file", {"path": SKILL, "content": "check results\n"}
            )
        return fake_chat_server.tool_call(
            "finish_edit", {"summary": "Check results before replying"}
        )


@pytest.mark.parametrize(
    "agent", [DelegationAgent(), ResultCheckingAgent()], ids=["delegation", "tool-check"]
)
async def test_package_feedback_reaches_its_roles_and_keeps_the_primary_metric(
    tmp_path, monkeypatch, agent
):
    rows = [_row(case_id, score) for case_id, score in CASES.items()]
    rows.append(_row("held-out", split="test", text=HELD_OUT))
    provider = _RoleProvider(agent)
    base_url, server = fake_chat_server.start([provider])
    monkeypatch.setenv("E1_KEY", "offline-feedback-test-key")
    try:
        with _target(tmp_path, agent) as target_url:
            path, document = _manifest(tmp_path, target_url, agent, rows)
            document["editor"] = chat_budget_section(base_url)
            document["reflector"] = {
                **{
                    key: document["reflector"][key]
                    for key in (
                        "environment_docs",
                        "probe",
                        "selection",
                        "families",
                        "max_consecutive_failures",
                    )
                },
                **chat_reflector(base_url),
            }
            path.write_text(json.dumps(document))
            run = tmp_path / "run"
            result = await evolve(path, run)
    finally:
        server.shutdown()
        server.server_close()

    assert result.terminal == "finished", (result.owner, result.code, result.suspended)
    (baseline,) = payloads(run, "baseline_bound")
    measurement = baseline["measurement_id"]
    events = _records(_measurement_events(run, measurement))
    scores = {
        record["payload"]["case_id"]: record["payload"]
        for record in events
        if record["event"] == "scoring_finished"
    }
    assert {case_id: score["metrics"]["task_quality"] for case_id, score in scores.items()} == CASES
    terminals = {
        record["payload"]["case_id"]: record["payload"]["outcome"]
        for record in events
        if record["event"] == "rollout_finished"
    }
    assert terminals == {
        "complete": "completed",
        "diagnostic-gap": "completed",
        "candidate-defect": "candidate_failed",
    }

    (grouping,) = payloads(run, "grouping_finished")
    labels = {entry["label"] for entry in grouping["result"]["descriptions"].values()}
    assert labels == {f'{agent.observation}="{value}"' for value in (agent.signal, "unknown")}
    reads = provider.reads
    assert set(reads) == set(CASES)
    with ScorerTraceReader(run) as traces:
        for case_id, score in scores.items():
            trace = traces.read(score["score_request_id"], measurement_id=measurement)
            scorer_input = json.loads(trace.request)["input"]
            assert scorer_input["case_id"] == case_id
            assert scorer_input["oracle"]["secret"] == ORACLE
            events = scorer_input["view"]["history"]["turns"][0]["events"]
            available = "partial" if case_id == "diagnostic-gap" else "complete"
            assert events[0] == {"type": "trace_availability", "state": available}
            expected = "unknown" if available == "partial" else agent.signal
            assert [event for event in events if event["type"] == agent.signal_type] == (
                [{"type": agent.signal_type, "status": agent.signal}]
                if available == "complete"
                else []
            )
            assert [event["name"] for event in events if event["type"] == "tool_call"] == (
                [agent.tool] if available == "complete" else []
            )
            scored = json.loads(trace.stdout)
            assert scored["public_feedback"] == {agent.observation: expected}
            assert scored["diagnostics"][agent.observation] == expected
            read = reads[case_id]
            assert read["identity"]["sample_id"] == score["sample_id"]
            assert read["observation"]["primary_score"] == CASES[case_id]
            assert read["observation"]["terminal"] == terminals[case_id]
            assert read["diagnostics"] == scored["diagnostics"]
            assert read["feedback"] == scored["public_feedback"]
            assert scored["details"]["private_material"] not in json.dumps(read)
            assert ORACLE not in json.dumps(read)

    first, *_, last = provider.editor_requests
    opening = _opening_json(first)
    citations = {entry["evidence_id"]: entry["observation"] for entry in opening["cited"]}
    assert opening["cited_omitted"] == 0
    assert set(citations) == {record["identity"]["evidence_id"] for record in reads.values()}
    for record in reads.values():
        cited = citations[record["identity"]["evidence_id"]]
        assert cited["feedback"] == record["feedback"]
        assert not any(
            event.get("type") == agent.signal_type
            for turn in cited["turns"]
            for event in turn["events"]
        )
    (diagnosis,) = _tool_pages(last, "read_evidence")
    assert f"The trace reports {agent.signal} before an unchecked reply." in diagnosis["content"]
    assert "The partial trace leaves the behavior unknown." in diagnosis["content"]
    for citation in citations:
        assert citation in diagnosis["content"]
    (reflection,) = payloads(run, "reflection_finished")
    assert set(reflection["groups"][0]["evidence_ids"]) == set(citations)
    assert set(reflection["groups"][0]["affected_cases"]) == {"complete", "candidate-defect"}
    assert {
        entry["evidence_id"]
        for entry in reflection["reads"]["entries"]
        if entry["operation"] == "read_evidence"
    } == set(citations)
    (frozen,) = payloads(run, "attempt_input_frozen")
    (editor,) = payloads(run, "editor_started")
    assert editor["input_digest"] == frozen["input_digest"]
    editor_input = json.dumps(provider.editor_requests)
    for canary in ("PRIVATE-DIAGNOSTIC-", "PRIVATE-SCORER-DETAILS", ORACLE, HELD_OUT, "held-out"):
        assert canary not in editor_input

    (gate,) = payloads(run, "comparison_completed")
    assert gate["outcome"] == "passed"
    scored_candidate = _records(_measurement_events(run, gate["candidate"]["measurement_id"]))
    assert {
        entry["payload"]["case_id"]: entry["payload"]["metrics"]
        for entry in scored_candidate
        if entry["event"] == "scoring_finished"
    } == dict.fromkeys(CASES, {"decoy": 0.0, "task_quality": 1.0})
    (completion,) = payloads(run, "candidate_completion_completed")
    assert completion["required"] == completion["admissible"] == len(CASES)
    (final,) = payloads(run, "final_measurement_bound")
    assert final["verification"] == "reuse"
    (comparison,) = payloads(run, "global_comparison_completed")
    assert comparison["accepted"] and comparison["outcome"] == "passed"
    (selection,) = payloads(run, "selection_decided")
    assert selection["reason"] == "automatic" and selection["selected_candidate"] is not None
    (closed,) = payloads(run, "round_closed")
    assert closed["outcome"] == "promoted"
    assert (run / "artifacts/champions/v1" / SKILL).read_text() == "check results\n"
    assert audit_mismatches(run) == []


@pytest.mark.parametrize(
    "owner,case_id,code",
    [
        ("target", "missing-at-target", "target_failed"),
        ("scorer", "missing-at-scorer", "scorer_failed"),
    ],
)
async def test_missing_required_evidence_is_an_indeterminate_sample(tmp_path, owner, case_id, code):
    with _target(tmp_path, DelegationAgent()) as target_url:
        path, document = _manifest(tmp_path, target_url, DelegationAgent(), [_row(case_id)])
        # Exercise the measurement owner, after the connection preflight boundary.
        document["checks"] = {"version": "checks/1", "modes": {"connection_preflight": "off"}}
        document["campaign"]["indeterminate"][owner] = {"max_count": 0, "max_rate": "1"}
        path.write_text(json.dumps(document))
        run = tmp_path / "run"
        result = await evolve(path, run)

    assert (result.terminal, result.owner) == ("suspended", owner)
    (observation,) = payloads(run, "indeterminate_observed")
    assert (observation["case_id"], observation["owner"], observation["reason"]) == (
        case_id,
        owner,
        f"{owner}_unstable",
    )
    journal_path = _measurement_events(run, observation["measurement_id"])
    journal = inspect_journal(journal_path.parent)
    state = Lifecycle()
    for record in journal.records:
        state.accept(record)
    (row,) = latest_rows(journal.records, state)
    assert (row.outcome, row.disposition, row.metrics, row.exhausted) == (
        code,
        "indeterminate",
        None,
        True,
    )
    events = _records(journal_path)
    assert not any(event["event"] == "scoring_finished" for event in events)
    rollout = next(event["payload"] for event in events if event["event"] == "rollout_finished")
    assert rollout["outcome"] == ("target_failed" if owner == "target" else "completed")
    assert any(event["event"] == "scoring_started" for event in events) is (owner == "scorer")
    assert any(event["event"] == "scoring_failed" for event in events) is (owner == "scorer")
    assert not payloads(run, "attempt_opened")
    assert audit_mismatches(run) == []
