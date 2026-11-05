"""Core edit-ops rewrite mode: engine + outcome classification."""

from __future__ import annotations

import asyncio
import json

import pytest

from evolution.core.parser import load_skill
from evolution.evolve.rewrite_edit_ops import (
    _rewrite_skill_edit_ops_impl,
    edit_proposals,
    evidence_policy_text,
    propose_and_apply_edits,
    propose_prompt_name,
)
from evolution.evolve.rewrite_outcome import EDIT_OPS_PARSE_ERROR, PROMOTED_ELIGIBLE
from evolution.llm.backend import LLMResponse
from evolution.llm.mock_backend import MockBackend

BODY = "# Guide\n\nUse the API carefully.\n\n## Steps\nRead inputs first.\n"


class FakeBackend(MockBackend):
    def __init__(self, replies: list[str]) -> None:
        super().__init__(responses=replies)
        self.prompts: list[tuple[str, str]] = []
        self.seeds: list[int | None] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        seed: int | None = None,
    ) -> LLMResponse:
        self.prompts.append((messages[0]["content"], system or ""))
        self.seeds.append(seed)
        response = await super().complete(messages, system, seed)
        response.tokens_in = 10
        response.tokens_out = 5
        return response


def _edits_json(edits):
    return json.dumps({"edits": edits})


def test_propose_and_apply_happy_path():
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "replace",
                        "target": "Use the API carefully.",
                        "content": "Use the documented API entrypoint.",
                        "rationale": "observed wrong entrypoint",
                        "evidence_refs": ["t1"],
                    }
                ]
            )
        ]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="trace evidence", blind=True)
    )
    assert "documented API entrypoint" in new_body
    assert "Use the API carefully." not in new_body
    assert tel["n_applied"] == 1
    assert tel["tokens_in"] == 10
    prompt, system = backend.prompts[0]
    assert "trace evidence" in prompt
    assert "BLIND mode" in prompt
    assert "BLIND mode" in system


def test_propose_unparseable_is_honest_noop():
    backend = FakeBackend(["I refuse to answer with JSON."])
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert new_body == BODY
    assert tel["error_noop"] is True
    assert tel["n_proposed"] == 0


def test_repair_round_fixes_missing_target():
    backend = FakeBackend(
        [
            _edits_json([{"op": "replace", "target": "NOT IN BODY", "content": "x"}]),
            _edits_json(
                [{"op": "replace", "target": "Read inputs first.", "content": "Read inputs twice."}]
            ),
        ]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert "Read inputs twice." in new_body
    assert tel["edit_repair_used"] is True
    assert "NOT IN BODY" in backend.prompts[1][0]
    assert tel["final_source"] == "repair"
    assert tel["n_proposed"] == 2


def test_repair_keeps_successes_and_uses_remaining_budget():
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "replace",
                        "target": "Use the API carefully.",
                        "content": "Use the API safely.",
                    },
                    {"op": "replace", "target": "MISSING", "content": "x"},
                ]
            ),
            _edits_json(
                [
                    {
                        "op": "replace",
                        "target": "Read inputs first.",
                        "content": "Read and validate inputs.",
                    },
                    {"op": "append", "target": "", "content": "OVER BUDGET"},
                ]
            ),
        ]
    )
    new_body, telemetry = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True, budget=2)
    )
    assert "Use the API safely." in backend.prompts[1][0]
    assert "Use the API safely." in new_body
    assert "Read and validate inputs." in new_body
    assert "OVER BUDGET" not in new_body
    assert telemetry["n_applied"] == 2


def test_record_call_captures_propose_call():
    reply = _edits_json([{"op": "replace", "target": "Use the API carefully.", "content": "x"}])
    backend = FakeBackend([reply])
    calls = []
    asyncio.run(
        propose_and_apply_edits(
            backend,
            body=BODY,
            evidence="e",
            blind=True,
            record_call=lambda *args: calls.append(args),
        )
    )
    assert len(calls) == 1
    call_type, prompt, response = calls[0]
    assert call_type == "propose"
    assert "e" in prompt
    assert response == reply


def test_record_call_captures_repair_call():
    backend = FakeBackend(
        [
            _edits_json([{"op": "replace", "target": "NOT IN BODY", "content": "x"}]),
            _edits_json(
                [{"op": "replace", "target": "Read inputs first.", "content": "Read inputs twice."}]
            ),
        ]
    )
    calls = []
    asyncio.run(
        propose_and_apply_edits(
            backend,
            body=BODY,
            evidence="e",
            blind=True,
            record_call=lambda *args: calls.append(args),
        )
    )
    assert [c[0] for c in calls] == ["propose", "repair"]
    assert "NOT IN BODY" in calls[1][1]


@pytest.fixture()
def skill(tmp_path):
    d = tmp_path / "guide"
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: guide\ndescription: test skill\n---\n{BODY}", encoding="utf-8"
    )
    return load_skill(d)


REFLECTIONS = [
    {
        "root_cause": "wrong entrypoint",
        "skill_gap": "missing API guidance",
        "suggested_fix": "name the entrypoint",
        "severity": "major",
        "pattern": "wrong_api",
    }
]


def test_impl_promoted_eligible(skill, monkeypatch):
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "0.50")
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "append",
                        "target": "",
                        "content": "## Pitfalls\nAlways check the entrypoint name.",
                    }
                ]
            )
        ]
    )
    result = asyncio.run(
        _rewrite_skill_edit_ops_impl(backend, skill, REFLECTIONS, "task", None, None)
    )
    assert result.candidate_status == PROMOTED_ELIGIBLE
    assert "Always check the entrypoint name." in result.body
    assert result.telemetry["n_applied"] == 1


def test_impl_parse_error_status(skill):
    backend = FakeBackend(["not json at all"])
    result = asyncio.run(
        _rewrite_skill_edit_ops_impl(backend, skill, REFLECTIONS, "task", None, None)
    )
    assert result.candidate_status == EDIT_OPS_PARSE_ERROR
    assert result.body == skill.body


def test_edit_proposals_default_and_garbage(monkeypatch):
    monkeypatch.delenv("EVO_EDIT_PROPOSALS", raising=False)
    assert edit_proposals() == 1
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "3")
    assert edit_proposals() == 3
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "not-a-number")
    with pytest.raises(ValueError, match="not an integer"):
        edit_proposals()
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "0")
    with pytest.raises(ValueError, match="must be positive"):
        edit_proposals()
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "-3")
    with pytest.raises(ValueError, match="must be positive"):
        edit_proposals()


def test_single_proposal_payload_is_byte_identical_and_skips_merge(monkeypatch):
    monkeypatch.delenv("EVO_EDIT_PROPOSALS", raising=False)
    backend = FakeBackend(
        [_edits_json([{"op": "replace", "target": "Use the API carefully.", "content": "x"}])]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert len(backend.prompts) == 1
    assert "Proposal lens" not in backend.prompts[0][0]
    assert backend.seeds == [None]
    assert tel["n_proposal_calls"] == 1
    assert tel["merge_used"] is False
    assert tel["final_source"] == "propose"
    assert "x" in new_body


def test_multi_proposal_merge_fires_over_budget(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "3")
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "append",
                        "target": "",
                        "content": "Propose-1-idiom",
                        "evidence_refs": ["t1"],
                    }
                ]
            ),
            _edits_json(
                [
                    {
                        "op": "append",
                        "target": "",
                        "content": "Propose-2-idiom",
                        "evidence_refs": ["t2"],
                    }
                ]
            ),
            _edits_json(
                [
                    {
                        "op": "append",
                        "target": "",
                        "content": "Propose-3-idiom",
                        "evidence_refs": ["t3"],
                    }
                ]
            ),
            _edits_json(
                [
                    {"op": "append", "target": "", "content": "Merged-A"},
                    {"op": "append", "target": "", "content": "Merged-B"},
                ]
            ),
        ]
    )
    calls = []
    new_body, tel = asyncio.run(
        propose_and_apply_edits(
            backend,
            body=BODY,
            evidence="evidence for t1",
            blind=True,
            task_context="task context",
            runtime_contract="runtime contract",
            budget=2,
            record_call=lambda *args: calls.append(args[0]),
        )
    )
    assert calls == ["propose", "propose", "propose", "merge"]
    assert backend.seeds[:3] == [None, 1001, 1002]
    assert "Propose-1-idiom" in new_body and "Propose-2-idiom" in new_body
    assert "Merged-A" not in new_body
    assert "Propose-3-idiom" not in new_body
    merge_prompt = backend.prompts[3][0]
    assert "Propose-1-idiom" in merge_prompt
    assert "Propose-3-idiom" in merge_prompt
    assert "## Current Skill Body" in merge_prompt
    assert BODY in merge_prompt
    assert "task context" in merge_prompt
    assert "runtime contract" in merge_prompt
    assert "BLIND mode" in merge_prompt
    assert "t1" in merge_prompt
    assert tel["merge_used"] is False
    assert tel["n_merge_rejected"] == 2
    assert tel["n_applied"] == 2
    assert tel["final_source"] == "propose"


def test_multi_proposal_no_merge_when_unique_fits_budget(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "3")
    same_edit = _edits_json([{"op": "append", "target": "", "content": "Only-idiom"}])
    backend = FakeBackend([same_edit, same_edit, same_edit])
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True, budget=2)
    )
    assert len(backend.prompts) == 3
    assert tel["merge_used"] is False
    assert "merge_parse_error" not in tel
    assert new_body.count("Only-idiom") == 1
    assert tel["n_applied"] == 1


def test_propose_and_apply_reports_duplicate_content_skip_when_configured(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "insert_after",
                        "target": "Use the API carefully.",
                        "content": "## Steps\nRead inputs first.",
                    }
                ]
            )
        ]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert tel["n_skipped_duplicate_content"] == 1
    assert new_body == BODY


def test_propose_and_apply_reports_zero_duplicate_content_skips_when_configured_and_novel(
    monkeypatch,
):
    monkeypatch.setenv("EVO_EDIT_DEDUP_BODY", "0.9")
    backend = FakeBackend(
        [_edits_json([{"op": "replace", "target": "Use the API carefully.", "content": "x"}])]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert tel["n_skipped_duplicate_content"] == 0
    assert "x" in new_body


def test_propose_and_apply_omits_duplicate_content_key_when_guard_unset(monkeypatch):
    monkeypatch.delenv("EVO_EDIT_DEDUP_BODY", raising=False)
    backend = FakeBackend(
        [_edits_json([{"op": "replace", "target": "Use the API carefully.", "content": "x"}])]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert "n_skipped_duplicate_content" not in tel


def test_repair_insert_flag_on_missed_insert_enters_repair(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "1")
    backend = FakeBackend(
        [
            _edits_json([{"op": "insert_after", "target": "NOT IN BODY", "content": "added"}]),
            _edits_json(
                [{"op": "insert_after", "target": "## Steps", "content": "\nCheck outputs."}]
            ),
        ]
    )
    calls = []
    new_body, tel = asyncio.run(
        propose_and_apply_edits(
            backend,
            body=BODY,
            evidence="e",
            blind=True,
            record_call=lambda *args: calls.append(args[0]),
        )
    )
    assert calls == ["propose", "repair"]
    assert tel["edit_repair_used"] is True
    assert "NOT IN BODY" in backend.prompts[1][0]
    assert "Check outputs." in new_body
    assert "added" not in new_body


def test_repair_insert_flag_on_terminal_miss_is_noop(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "1")
    backend = FakeBackend(
        [
            _edits_json([{"op": "insert_after", "target": "NOT IN BODY", "content": "added"}]),
            _edits_json(
                [{"op": "insert_after", "target": "STILL NOT IN BODY", "content": "added"}]
            ),
        ]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert tel["edit_repair_used"] is True
    assert [e["status"] for e in tel["edit_apply_report"]] == [
        "skipped_target_not_found",
        "skipped_target_not_found",
    ]
    assert new_body == BODY


def test_repair_insert_flag_off_missed_insert_falls_back_without_repair(monkeypatch):
    monkeypatch.delenv("EVO_EDIT_REPAIR_INSERT", raising=False)
    backend = FakeBackend(
        [_edits_json([{"op": "insert_after", "target": "NOT IN BODY", "content": "added"}])]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )
    assert len(backend.prompts) == 1
    assert "edit_repair_used" not in tel
    assert tel["edit_apply_report"][0]["status"] == "fallback_append"
    assert new_body == BODY + "\n\nadded"


def test_repair_insert_flag_on_with_repair_disabled_keeps_fallback(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "1")
    backend = FakeBackend(
        [_edits_json([{"op": "insert_after", "target": "NOT IN BODY", "content": "added"}])]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True, repair=False)
    )
    assert len(backend.prompts) == 1
    assert tel["edit_apply_report"][0]["status"] == "fallback_append"
    assert new_body == BODY + "\n\nadded"


def test_train_target_selects_train_prompt_variant(monkeypatch):
    monkeypatch.setenv("EVO_OPTIMIZATION_TARGET", "train")

    assert propose_prompt_name() == "propose_edits_train"


def test_train_oracle_policy_allows_train_tests_but_names_held_out_boundary():
    policy = evidence_policy_text(blind=False)

    assert "TRAIN_EVIDENCE" in policy
    assert "training traces" in policy
    assert "held-out" in policy
    assert "leak scanner discards" not in policy
    assert "none of those literals may reach the skill itself" not in policy


def test_train_variant_placeholder_set_identical():
    from evolution import prompts

    base_tokens = set(prompts._TOKEN_RE.findall(prompts.load_prompt("propose_edits")))
    train_tokens = set(prompts._TOKEN_RE.findall(prompts.load_prompt("propose_edits_train")))
    assert train_tokens == base_tokens


def test_multi_proposal_merge_garbage_falls_back_to_mechanical(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "3")
    backend = FakeBackend(
        [
            _edits_json([{"op": "append", "target": "", "content": "Propose-1-idiom"}]),
            _edits_json([{"op": "append", "target": "", "content": "Propose-2-idiom"}]),
            _edits_json([{"op": "append", "target": "", "content": "Propose-3-idiom"}]),
            "not json at all",
        ]
    )
    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True, budget=2)
    )
    assert tel["merge_used"] is False
    assert tel["merge_parse_error"] is True
    assert "Propose-1-idiom" in new_body
    assert "Propose-2-idiom" in new_body
    assert "Propose-3-idiom" not in new_body
    assert tel["n_applied"] == 2
    assert tel["edit_parse_errors"] == 1
    assert tel["final_source"] == "propose"


def test_duplicate_topic_skip_enters_repair(monkeypatch):
    monkeypatch.setenv("EVO_EDIT_DEDUP_TOPIC", "1")
    body = BODY + "\n#### Existing Recipe\nDo the known thing.\n"
    backend = FakeBackend(
        [
            _edits_json(
                [
                    {
                        "op": "insert_after",
                        "target": "## Steps",
                        "content": "#### Existing Recipe\nDuplicate guidance.",
                    }
                ]
            ),
            _edits_json(
                [
                    {
                        "op": "insert_after",
                        "target": "## Steps",
                        "content": "#### New Recipe\nCheck the generated artifact.",
                    }
                ]
            ),
        ]
    )

    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=body, evidence="e", blind=True)
    )

    assert tel["edit_repair_used"] is True
    assert tel["final_source"] == "repair"
    assert "#### New Recipe" in new_body
    assert "Duplicate guidance" not in new_body


def test_train_target_terminal_repaired_insert_miss_is_skipped(monkeypatch):
    monkeypatch.setenv("EVO_OPTIMIZATION_TARGET", "train")
    monkeypatch.setenv("EVO_EDIT_REPAIR_INSERT", "1")
    monkeypatch.setenv("EVO_EDIT_PROPOSALS", "1")
    backend = FakeBackend(
        [
            _edits_json([{"op": "insert_after", "target": "NOT IN BODY", "content": "bad"}]),
            _edits_json([{"op": "insert_after", "target": "STILL NOT IN BODY", "content": "bad"}]),
        ]
    )

    new_body, tel = asyncio.run(
        propose_and_apply_edits(backend, body=BODY, evidence="e", blind=True)
    )

    assert new_body == BODY
    assert tel["edit_repair_used"] is True
    assert tel["n_applied"] == 0
    assert tel["edit_apply_report"][0]["status"] == "skipped_target_not_found"
