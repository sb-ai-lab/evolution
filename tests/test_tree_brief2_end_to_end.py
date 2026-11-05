"""`edit_brief/5` end to end: real campaigns, real traces, the canaries of §4.6.

Two harnesses from `tests/test_tree_evolve.py`: the in-memory `FakeAdapter`
(whose target answers with an event type outside the allowlist) and a served
target over the real HTTP adapter — the echoing `ReferenceTarget`, and a
non-echoing backend built on the target server library as a consumer would.
The editor is a subprocess that records every brief it is handed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import BackendReply, LoadedTree, TargetServer, TurnResult, Usage, serve_target
from evolution.tree.evolve import evolve
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V3
from evolution.tree.target.reference_target import (
    ReferenceBackend,
    ReferenceTarget,
    _ReferenceInstance,
    serve,
)
from tests.test_tree_evolve import fake_adapters
from tests.tree_evolve_support import CAMPAIGN_PAYLOAD, POLICY, write_workspace

pytestmark = pytest.mark.slow

RECORDING_EDITOR = r"""
import json
import pathlib
import sys

request = json.loads(sys.stdin.read())
pathlib.Path(f"brief-{request['attempt_id']}.json").write_text(json.dumps(request["brief"]), encoding="utf-8")
skill = pathlib.Path(request["workspace"]) / "skills" / "SKILL.md"
skill.write_text(skill.read_text(encoding="utf-8").rstrip("\n") + " edited\n", encoding="utf-8")
print(json.dumps({"status": "edited", "note": "recorded"}))
"""

FEEDBACK_SCORER = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
view = json.dumps(payload["view"], sort_keys=True)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = pathlib.Path(root, "skills", "SKILL.md").read_text(encoding="utf-8") if root else ""
edited = "edited" in view or "edited" in skill
json.dump(
    {
        "metrics": {"score": 1.0 if edited else 0.0, "hidden_metric": 42.0},
        "critical_pass": True,
        # `answered` passes once the edit lands; `concise` never does, so every
        # round's baseline keeps a failure mode to focus on and every round
        # opens its attempts (Reflector X1 §3.3).
        "verdicts": [
            {"criterion": "answered", "passed": bool(edited)},
            {"criterion": "concise", "passed": False},
        ],
        "public_feedback": {"format": "ok", "judge": "this answer is fine and PRIVATE-CANARY"},
        "details": {"note": "DETAILS-CANARY"},
    },
    sys.stdout,
)
sys.stderr.write("STDERR-CANARY\n")
"""

ROWS = [
    {
        "case_id": "c1",
        "group_id": "g1",
        "stratum": "s1",
        "public": {"text": "hello PUBLIC-CANARY"},
        "private": {
            "secret": "PRIVATE-CANARY",
            "script": [{"content": "SCRIPT-CANARY", "fields": {"k": "FIELDS-CANARY"}}],
        },
        "oracle": {"expected": "ORACLE-CANARY", "critical": True},
    }
]

CANARIES = (
    "PRIVATE-CANARY",
    "ORACLE-CANARY",
    "FIELDS-CANARY",
    "DETAILS-CANARY",
    "STDERR-CANARY",
    "hidden_metric",
    "SCRIPT-CANARY",
)


def _campaign(rounds: int = 2, attempts: int = 1, parallel: int = 1) -> dict[str, Any]:
    return {
        **CAMPAIGN_PAYLOAD,
        "rounds": rounds,
        "attempts_per_round": attempts,
        "parallel_attempts": parallel,
        "round_stop": "all_attempts",
    }


def _briefs(ws: Path) -> dict[str, dict[str, Any]]:
    return {
        path.stem.removeprefix("brief-"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ws / "editor").glob("brief-*.json"))
    }


def _observations(brief: dict[str, Any]) -> list[dict[str, Any]]:
    return [repeat["observation"] for case in brief["cases"] for repeat in case["repeats"]]


# -- the in-memory target: structure, history, canaries ----------------------


async def test_two_rounds_over_fake_adapters_carry_observations_and_history(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    manifest = write_workspace(
        ws,
        scorer_script=FEEDBACK_SCORER,
        editor_script=RECORDING_EDITOR,
        campaign=_campaign(),
        rows=ROWS,
    )
    result = await evolve(manifest, ws / "run", adapter_factory=fake_adapters())
    assert result.terminal == "finished"

    briefs = _briefs(ws)
    assert set(briefs) == {"r01-a01", "r02-a01"}
    first, second = briefs["r01-a01"], briefs["r02-a01"]
    assert first["version"] == second["version"] == "edit_brief/5"
    assert first["limits"] == dict(BRIEF_LIMITS_V3)
    assert "diagnosis" in first and "diagnosis" in second
    observations = _observations(first)
    # One per repeat of the one corpus case: `CAMPAIGN_PAYLOAD`'s alpha
    # schedule spends at look 3, so a case carries three repeats.
    assert len(observations) == 3
    for observation in observations:
        assert observation["status"] == "present"
        assert observation["turns"][0]["input"] == {
            "status": "present",
            "content": "hello PUBLIC-CANARY",
            "truncated": False,
        }
        # The fake target emits `{"type": "message"}`, outside the allowlist.
        assert observation["turns"][0]["events"] == []
        assert observation["turns"][0]["events_omitted"] == 1
        assert observation["feedback"] == {"format": "ok"}
        assert observation["feedback_omitted"] == 1
        assert observation["feedback_status"] == "present"
    assert first["history"]["rounds_closed"] == 0
    history = second["history"]
    assert (history["rounds_closed"], history["rounds_shown"], history["predecessor_rounds"]) == (
        1,
        [1],
        0,
    )
    [round_one] = history["rounds"]
    assert round_one["outcome"] == "promoted" and round_one["selected_attempt"] == "r01-a01"
    [attempt] = round_one["attempts"]
    # ADR-0032: a gate-passing Candidate is a finalist before final selection.
    assert attempt["editor"] == "edited" and attempt["outcome"] == "finalist"
    assert attempt["changes"] == {
        "status": "present",
        "paths": ["skills/SKILL.md"],
        "truncated": False,
    }
    assert attempt["result"]["status"] == "present"
    assert attempt["result"]["cases"][0]["repeats"][0]["primary_score"] == 1.0
    for brief in (first, second):
        encoded = json.dumps(brief)
        for canary in CANARIES:
            assert canary not in encoded, canary
        assert "PUBLIC-CANARY" in encoded
        # Spec §7: the Reflector's probe record is not the editor's to read,
        # neither the per-round log nor a group's `probe_refs`.
        assert "probe_refs" not in encoded and "probes" not in encoded


async def test_parallel_attempts_of_one_round_get_one_brief(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    manifest = write_workspace(
        ws,
        scorer_script=FEEDBACK_SCORER,
        editor_script=RECORDING_EDITOR,
        campaign={**_campaign(rounds=2, attempts=2, parallel=2), "attempt_feedback": "none"},
        rows=ROWS,
    )
    result = await evolve(manifest, ws / "run", adapter_factory=fake_adapters())
    assert result.terminal == "finished"
    briefs = _briefs(ws)
    assert briefs["r02-a01"] == briefs["r02-a02"]
    assert len(briefs["r02-a01"]["history"]["rounds"][0]["attempts"]) == 2


# -- served targets: the behaviour canary, the source canary, provenance ------


class _SilentSession:
    def __init__(self, line: str, seed: int | None) -> None:
        self.line = line
        self.effective_seed = seed

    def turn(self, message: Any) -> BackendReply:
        result = TurnResult(
            events=(
                {"type": "assistant_message", "content": self.line},
                {"type": "tool_call", "name": "lookup"},
            ),
            usage=Usage(),
            latency_ms=0,
            outcome="ok",
        )
        return BackendReply(result)

    def close(self) -> None:
        pass


class _SilentInstance:
    """Answers the skill line only, never the message (the non-echoing backend of §4.6)."""

    def __init__(self, tree: LoadedTree) -> None:
        skill = tree.root / "skills" / "SKILL.md"
        self.line = skill.read_text(encoding="utf-8").splitlines()[0] if skill.is_file() else ""

    def open(self, public: Any, seed: int | None) -> _SilentSession:
        return _SilentSession(self.line, seed)

    def close(self) -> None:
        pass


class _SilentBackend:
    def load(self, tree: LoadedTree) -> _SilentInstance:
        return _SilentInstance(tree)


def _serve(http: Any) -> threading.Thread:
    thread = threading.Thread(
        target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    return thread


@pytest.fixture
def echoing_target(tmp_path: Path) -> Any:
    target = ReferenceTarget(
        allowed_artifact_root=tmp_path.resolve(), policy=POLICY, seed_mode="exact"
    )
    http = serve(target, port=0)
    thread = _serve(http)
    try:
        yield http.server_address[1]
    finally:
        http.shutdown()
        http.server_close()
        target.close()
        thread.join(timeout=10)
        assert not thread.is_alive()


@pytest.fixture
def silent_target(tmp_path: Path) -> Any:
    server = TargetServer(
        _SilentBackend(),
        allowed_artifact_root=tmp_path.resolve(),
        policy=POLICY,
        seed_mode="exact",
        target_id="silent",
        contract_digest="silent-contract-1",
        settings_hash="silent-settings-1",
        runtime_ref_digest="silent-runtime-1",
    )
    http = serve_target(server, port=0)
    thread = _serve(http)
    try:
        yield http.server_address[1]
    finally:
        http.shutdown()
        http.server_close()
        server.close()
        thread.join(timeout=10)
        assert not thread.is_alive()


class _CanarySession:
    def __init__(self, inner: Any, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls
        self.effective_seed = inner.effective_seed

    def turn(self, message: Any) -> BackendReply:
        reply = self._inner.turn(message)
        r = reply.result
        result = TurnResult(
            r.events,
            r.usage,
            r.latency_ms,
            r.outcome,
            r.reason,
            {"used": ["canary#PROVENANCE-CANARY"]},
        )
        self._calls.append("turn")
        return BackendReply(result, reply.truncated)

    def close(self) -> None:
        self._inner.close()


class _CanaryInstance(_ReferenceInstance):
    calls: list[str] = []

    def components(self) -> Mapping[str, Any]:
        self.calls.append("components")
        return {"canary#COMPONENTS-CANARY": {"sha256": "c" * 64, "sources": ["skills/SKILL.md"]}}

    def open(self, public: Mapping[str, Any], seed: int | None) -> Any:
        return _CanarySession(super().open(public, seed), self.calls)


class _CanaryBackend(ReferenceBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def load(self, tree: LoadedTree) -> _CanaryInstance:
        instance = _CanaryInstance(self, tree)
        instance.calls = self.calls
        return instance


def _scripted_manifest(ws: Path, port: int, **overrides: Any) -> Path:
    manifest = write_workspace(
        ws,
        port=port,
        scorer_script=FEEDBACK_SCORER,
        editor_script=RECORDING_EDITOR,
        campaign=_campaign(rounds=1),
        rows=ROWS,
        **overrides,
    )
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["environment"] = {"kind": "scripted"}
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return manifest


async def test_behaviour_canary_the_echoing_target_republishes_its_input_in_its_reply_only(
    tmp_path: Path, echoing_target: int
) -> None:
    ws = tmp_path / "ws"
    manifest = _scripted_manifest(ws, echoing_target)
    result = await evolve(manifest, ws / "run")
    assert result.terminal == "finished"
    observations = _observations(_briefs(ws)["r01-a01"])
    assert len(observations) == 3
    for observation in observations:
        assert observation["status"] == "present"
        turn = observation["turns"][0]
        assert turn["input"] == {"status": "withheld", "content": None, "truncated": False}
        [event] = turn["events"]
        assert event["type"] == "assistant_message" and "SCRIPT-CANARY" in event["content"]
    encoded = json.dumps(_briefs(ws)["r01-a01"])
    # The scripted message reaches the brief through the target's reply and
    # nowhere else: once per observation, never as the turn's own input.
    assert encoded.count("SCRIPT-CANARY") == len(observations)
    for canary in (
        "PRIVATE-CANARY",
        "ORACLE-CANARY",
        "FIELDS-CANARY",
        "DETAILS-CANARY",
        "STDERR-CANARY",
        "hidden_metric",
    ):
        assert canary not in encoded, canary


async def test_source_canary_a_silent_target_leaves_no_trace_of_the_scripted_input(
    tmp_path: Path, silent_target: int
) -> None:
    ws = tmp_path / "ws"
    manifest = _scripted_manifest(ws, silent_target)
    result = await evolve(manifest, ws / "run")
    assert result.terminal == "finished"
    brief = _briefs(ws)["r01-a01"]
    observations = _observations(brief)
    assert len(observations) == 3
    for observation in observations:
        assert observation["status"] == "present"
        assert observation["turns"][0]["events"] == [
            {"type": "assistant_message", "content": "seed skill", "truncated": False},
            {"type": "tool_call", "name": "lookup"},
        ]
    encoded = json.dumps(brief)
    for canary in CANARIES:
        assert canary not in encoded, canary


async def test_provenance_canary_the_observation_follows_the_tree_not_the_oracle(
    tmp_path: Path, silent_target: int
) -> None:
    same_tree_a = await _run_async(
        tmp_path / "a", silent_target, oracle="ORACLE-A", skill="seed skill\n"
    )
    same_tree_b = await _run_async(
        tmp_path / "b", silent_target, oracle="ORACLE-B", skill="seed skill\n"
    )
    other_tree = await _run_async(
        tmp_path / "c", silent_target, oracle="ORACLE-A", skill="other skill\n"
    )
    assert len(same_tree_a) == len(same_tree_b) == len(other_tree) == 3
    assert same_tree_a == same_tree_b
    assert other_tree != same_tree_a
    assert other_tree[0]["turns"][0]["events"][0]["content"] == "other skill"


async def _run_async(ws: Path, port: int, *, oracle: str, skill: str) -> list[dict[str, Any]]:
    rows = [{**ROWS[0], "oracle": {"expected": oracle, "critical": True}}]
    manifest = write_workspace(
        ws,
        port=port,
        scorer_script=FEEDBACK_SCORER,
        editor_script=RECORDING_EDITOR,
        campaign=_campaign(rounds=1),
        rows=rows,
    )
    (ws / "seed" / "skills" / "SKILL.md").write_text(skill, encoding="utf-8")
    result = await evolve(manifest, ws / "run")
    assert result.terminal == "finished"
    return _observations(_briefs(ws)["r01-a01"])


async def test_a_nested_public_history_reaches_the_editor_and_the_attempt_is_edited(
    tmp_path: Path,
) -> None:
    """The acquiring readiness report (2026-09-11): a corpus row whose `public`
    carries a history of objects must reach the editor verbatim instead of
    failing the attempt before the editor runs."""

    history = [{"speaker": "CUSTOMER", "text": "earlier PUBLIC-HISTORY-CANARY", "turn": 1}]
    rows = [{**ROWS[0], "public": {"text": "hello PUBLIC-CANARY", "history": history}}]
    ws = tmp_path / "ws"
    manifest = write_workspace(
        ws,
        scorer_script=FEEDBACK_SCORER,
        editor_script=RECORDING_EDITOR,
        campaign=_campaign(rounds=1),
        rows=rows,
    )

    result = await evolve(manifest, ws / "run", adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    brief = _briefs(ws)["r01-a01"]
    assert brief["cases"][0]["public"]["history"] == history
    records = [
        json.loads(line)
        for line in (ws / "run" / "campaign.jsonl").read_text().splitlines()
        if line
    ]
    assert [r["payload"]["status"] for r in records if r["event"] == "editor_finished"] == [
        "edited"
    ]
    assert [r["payload"]["outcome"] for r in records if r["event"] == "attempt_closed"] != [
        "abandoned"
    ]
