"""Family-14 role feedback (ADR-0037): what each role is told, end to end."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.slow

DIAGNOSIS = {
    "status": "reflected",
    "note": "n",
    "groups": [
        {
            "root_cause": "r",
            "suggested_fix": "f",
            "evidence_ids": ["obs:c1:1"],
            "affected_cases": ["c1"],
            "target_paths": ["skills/SKILL.md"],
            "counterevidence": "c",
            "regression_risks": "r",
            "why_this_layer": "w",
        }
    ],
}


def dumping_reflector(out: Path, diagnosis: dict[str, Any] = DIAGNOSIS) -> str:
    """A subprocess Reflector that saves each request document, then reflects."""

    return (
        "import json, pathlib, sys\n"
        "request = json.load(sys.stdin)\n"
        f"out = pathlib.Path({str(out)!r})\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / f'{len(list(out.iterdir())):02d}.json').write_text(json.dumps(request))\n"
        f"print({json.dumps(diagnosis)!r})\n"
    )


async def test_the_reflector_reads_the_editor_budget_and_its_input_digest_is_unchanged(
    tmp_path, monkeypatch
):
    """The opening names the budget; the digested payload is the request's own evidence.

    Case handles are salted per run, so digests cannot be compared across two
    runs; instead each run's captured payload digest must equal the digest of the
    `evidence` the Reflector was handed, and the opening must end with that payload.
    """

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
    from tests.family14_b2_scenario import _sha, capture_role_inputs, i1_options
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    for name, budget in (("plain", None), ("stated", 3)):
        root = tmp_path / name
        root.mkdir()
        with monkeypatch.context() as patch:
            if budget is not None:
                patch.setattr(SubprocessEditor, "max_changed_files", budget, raising=False)
            options = i1_options(root, patch)
            captured = capture_role_inputs(patch)
            run, _ = await family14_run(
                root, patch, reflector=dumping_reflector(root / "requests"), **options
            )
        (first,) = [p for p in payloads(run, "reflection_started") if p["round"] == 1]
        assert len(first["input_digest"]) == 64
        request = json.loads((root / "requests" / "00.json").read_text())
        lines = request["opening"].splitlines()
        assert ("at most 3 files" in request["opening"]) is (budget is not None)
        assert lines[-1] == canonical_json(request["evidence"]).decode()
        assert _sha(request["evidence"]) == captured["reflector_payload_digests"][0]
        if budget is not None:
            assert "at most 3 files" in lines[-2]


class OverBudgetBackend:
    """The agent CLI at the backend seam: every call leaves four new files."""

    prompts: list[str] = []
    validations: list[Any] = []
    requests: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.last_usage = {"tool_turns": 1}
        self.last_codex_diagnostics: dict[str, Any] = {}

    async def complete_file_edit(self, work_dir, prompt, system=None, *, timeout_s=0.0):
        root = Path(work_dir)
        OverBudgetBackend.prompts.append(prompt)
        control = root / ".evolution"
        validation = control / "VALIDATION.json"
        OverBudgetBackend.validations.append(
            json.loads(validation.read_text()) if validation.exists() else None
        )
        OverBudgetBackend.requests.append(json.loads((control / "EDIT_REQUEST.json").read_text()))
        for index in range(4):
            (root / "skills" / f"b3-{index}.md").write_text("edited\n", encoding="utf-8")
        return SimpleNamespace(content="four files", tokens_in=1, tokens_out=1, time_ms=1)


async def test_ten_budget_failures_over_two_rounds_name_their_cause_everywhere(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_schema import SchemaError
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.editor import agent_editor as agent_editor_module
    from tests import campaign_drivers as fixtures
    from tests.campaign_drivers import EDITED
    from tests.campaign_fakes import FakeAdapter, Family14Life
    from tests.family14_b2_scenario import journal_records
    from tests.test_tree_campaign_family14_run import family14_run, payloads
    from tests.test_tree_evolve import agent_editor_section

    for name in ("prompts", "validations", "requests"):
        setattr(OverBudgetBackend, name, [])
    monkeypatch.setattr(agent_editor_module, "AgentBackend", OverBudgetBackend)
    workspace = fixtures.write_workspace

    def write_workspace(*args, **kwargs):
        root = Path(args[0])
        return workspace(
            *args, **{**kwargs, "editor": {**agent_editor_section(root), "max_changed_files": 3}}
        )

    monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 2, "attempts_per_round": 5, "attempt_feedback": "sequential"},
        reflector=dumping_reflector(tmp_path / "requests"),
        # Admission sizes for a final measurement of five finalists and a fresh
        # Champion (`all_attempts`, `unstable`); the fake's default admits four.
        adapter=FakeAdapter(max_instances=6, used=EDITED["used"]),
    )
    assert snapshot.finished is not None
    failures = payloads(run, "editor_failed")
    assert len(failures) == 10
    assert {(p["error_class"], p["reason"]) for p in failures} == {
        ("reported", "changed_file_budget")
    }
    assert {(p["outcome"], p["reason"], p["code"]) for p in payloads(run, "attempt_closed")} == {
        ("abandoned", "editor_failed", None)
    }
    frozen = {p["attempt_id"]: p for p in payloads(run, "attempt_input_frozen")}
    (row,) = frozen["r01-a02"]["feedback"]["attempts"]
    assert (row["outcome"], row["reason"], row["changes_status"]) == (
        "abandoned",
        "changed_file_budget",
        "unavailable",
    )
    # The next round's Reflector history and opening (D9, D10).
    second = json.loads(sorted((tmp_path / "requests").iterdir())[-1].read_text())
    reasons = {a["reason"] for r in second["evidence"]["history"]["rounds"] for a in r["attempts"]}
    assert reasons == {"changed_file_budget"}
    assert "Each Editor Attempt may change at most 3 files" in second["opening"]
    assert "`changed_file_budget`: the Editor changed more files" in second["opening"]
    # The Editor: the budget line, the repair report, the round-2 brief history.
    assert all("Budget: change at most 3 files" in p for p in OverBudgetBackend.prompts)
    reports = [v for v in OverBudgetBackend.validations if v is not None and "changed_paths" in v]
    assert len(reports) == 10
    assert reports[0]["issues"][0].startswith(
        "changed-file budget exceeded: 4 files changed, limit 3"
    )
    round_two = [r for r in OverBudgetBackend.requests if r["brief"]["history"]["rounds"]]
    assert round_two
    assert {a["reason"] for a in round_two[0]["brief"]["history"]["rounds"][0]["attempts"]} == {
        "changed_file_budget"
    }
    # No Target or scorer work for any Candidate (the round's fresh baseline is
    # journaled as the champion side, which is not Candidate work).
    assert not [p for p in payloads(run, "measurement_bound") if p["attempt_id"] != "champion"]
    assert not payloads(run, "attempt_sealed")
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    # A hand-edited row that hides the code is refused by the reducer (ADR-0037 I1 lockstep).
    records = journal_records(run)
    index = next(
        i
        for i, r in enumerate(records)
        if r["event"] == "attempt_input_frozen" and r["payload"]["attempt_id"] == "r01-a02"
    )
    life = Family14Life(records[:index])
    edited = copy.deepcopy(records[index]["payload"])
    edited["feedback"]["attempts"][0]["reason"] = "editor_failed"
    frozen_part = {
        k: edited[k] for k in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
    }
    edited["input_digest"] = hashlib.sha256(canonical_json(frozen_part)).hexdigest()
    with pytest.raises(SchemaError, match="feedback differs from the closed attempt"):
        life.accept("attempt_input_frozen", op=records[index]["operation_id"], **edited)


def chat_budget_section(base_url: str) -> dict[str, Any]:
    from tests import fake_chat_server
    from tests.tree.manifest.test_manifest_v2 import RETRY

    return {
        "kind": "chat",
        "model": fake_chat_server.MODEL,
        "base_url": base_url,
        "api_key_env": "E1_KEY",
        "max_turns": 24,
        "retry": RETRY,
        "context_compact_chars": 800_000,
        "context_message_max_chars": 64_000,
        "max_changed_files": 3,
    }


def _chat_plan(paths: list[str]) -> dict[str, Any]:
    from tests import fake_chat_server

    return fake_chat_server.tool_call(
        "record_plan",
        {
            "summary": "fix",
            "changes": [{"path": p, "action": "update", "purpose": "fix"} for p in paths],
        },
    )


def _chat_write(path: str, content: str) -> dict[str, Any]:
    from tests import fake_chat_server

    return fake_chat_server.tool_call("write_file", {"path": path, "content": content})


@pytest.mark.parametrize("compliant", [True, False])
async def test_a_twelve_file_chat_plan_is_corrected_inside_the_attempt(
    tmp_path, monkeypatch, compliant
):
    from tests import campaign_drivers as fixtures
    from tests import fake_chat_server
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    paths = ["skills/SKILL.md", *[f"skills/f{i:02d}.md" for i in range(1, 12)]]
    if compliant:
        script = [
            fake_chat_server.tool_call("list_files"),
            _chat_plan(paths),
            _chat_plan(paths[:3]),
            _chat_write("skills/SKILL.md", "IMPROVED: seed skill edited\n"),
            _chat_write(paths[1], "edited\n"),
            _chat_write(paths[2], "edited\n"),
            fake_chat_server.tool_call("finish_edit", {"summary": "three files"}),
        ]
    else:
        script = [
            fake_chat_server.tool_call("list_files"),
            *[_chat_plan(paths) for _ in range(5)],
            *[_chat_write(paths[1], "edited\n") for _ in range(3)],
        ]
    base_url, server = fake_chat_server.start(script)
    try:
        monkeypatch.setenv("E1_KEY", "sekret-key-value")
        workspace = fixtures.write_workspace

        def write_workspace(*args, **kwargs):
            path = workspace(*args, **{**kwargs, "editor": chat_budget_section(base_url)})
            for relative in paths[1:]:
                (Path(args[0]) / "seed" / relative).write_text(f"{relative}\n", encoding="utf-8")
            return path

        monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, campaign={"rounds": 1, "attempts_per_round": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
    log = [
        json.loads(line)
        for line in (run / "editor-logs" / "r01-a01.jsonl").read_text().splitlines()
        if line
    ]
    errors = [line for line in log if line["phase"] == "tool_error"]
    assert errors[0]["tool"] == "record_plan" and errors[0]["turn"] <= 2
    assert "12 files" in errors[0]["error"] and "at most 3 files" in errors[0]["error"]
    if compliant:
        assert [p["status"] for p in payloads(run, "editor_finished")] == ["edited"]
        # The sealed Candidate differs from the seed Champion in exactly the
        # three files of the corrected plan.
        (sealed,) = payloads(run, "attempt_sealed")
        candidate, seed = run / sealed["artifact_locator"], run / "artifacts" / "champions" / "v0"
        files = sorted(p.relative_to(seed) for p in seed.rglob("*") if p.is_file())
        assert files == sorted(
            p.relative_to(candidate) for p in candidate.rglob("*") if p.is_file()
        )
        changed = [str(p) for p in files if (seed / p).read_bytes() != (candidate / p).read_bytes()]
        assert changed == paths[:3]
        assert [p for p in payloads(run, "measurement_bound") if p.get("attempt_id") == "r01-a01"]
        assert not [line for line in log if line["phase"] == "protocol_repair_requested"]
    else:
        (failed,) = payloads(run, "editor_failed")
        assert (failed["error_class"], failed["reason"]) == ("reported", "protocol_exhausted")
        assert not payloads(run, "attempt_sealed")
        assert not [
            p for p in payloads(run, "measurement_bound") if p.get("attempt_id") == "r01-a01"
        ]


async def test_a_b2_shaped_open_round_resumes_after_the_version_rebase(tmp_path, monkeypatch):
    """Spec §8: a family-14 run cut after `attempt_input_frozen` is resumed after
    the producing-version rebase and re-runs the Attempt under the same frozen
    input, without `frozen editor common input changed`. The prefix is written
    by this branch's code under `producing_version` 2.0.0b1 (the I1 scenario has
    no budget failure); its frozen common digests are proven equal to the b2
    generator's `expected.json`, so it is b2-shaped in fact."""

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.family14_b2_scenario import i1_options
    from tests.test_tree_campaign_family14_run import family14_run, payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision, _version

    options = i1_options(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen", call=3)  # r01-a01, r01-a02, r02-a01
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    frozen = {p["attempt_id"]: p for p in payloads(run, "attempt_input_frozen")}
    assert sorted(frozen) == ["r01-a01", "r01-a02", "r02-a01"]
    assert not [p for p in payloads(run, "editor_started") if p["attempt_id"] == "r02-a01"]
    # The cut prefix is b2-shaped in fact, not only in kind: every frozen common
    # input equals what the b2 generator recorded (Task 1, `expected.json`).
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "family14_b2" / "expected.json").read_text()
    )
    assert {key: p["common_input_digest"] for key, p in frozen.items()} == {
        key: expected["common_input_digests"][key] for key in frozen
    }
    with monkeypatch.context() as patch:
        _version(patch, "0.3.0a99")
        with pytest.raises(EvolveRefused) as caught:
            await family14_run(tmp_path, monkeypatch, **options)
        refused = caught.value
        assert (refused.code, refused.effects) == ("run_identity_changed", ("rebase",))
        assert [d.field for d in refused.differences] == ["producing_version"]
        accepted = _decision(refused, None, reason="accept the producing-version change")
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, authorizations=[accepted], **options
        )
    # Reaching here proves `run_editor` recomputed the brief and `D` and found the
    # frozen `common_input_digest` (no `frozen editor common input changed`).
    # `editor_started.common_input_digest` is copied from the frozen record
    # (`campaign.py:6992-6997`), so comparing the two would prove nothing.
    assert snapshot.finished is not None
    assert [p["attempt_id"] for p in payloads(run, "editor_started")].count("r02-a01") == 1
    assert [p for p in payloads(run, "attempt_input_frozen") if p["attempt_id"] == "r02-a01"] == [
        frozen["r02-a01"]
    ]
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
