"""Pinned Editor inputs and current seal checks through real offline campaigns."""

import importlib
import json

import pytest

from evolution.tree.campaign import inspect_campaign
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


NEEDLE = "oracle answer for seal mode regression"
SECRET = "editor-input-secret-584396"


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_seal_oracle_modes(tmp_path, monkeypatch, mode):
    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_editor

    async def edit(editor, request, **kwargs):
        outcome = await original(editor, request, **kwargs)
        path = request.workspace / "skills/SKILL.md"
        path.write_text(path.read_text() + "\n" + NEEDLE + "\n")
        return outcome

    monkeypatch.setattr(campaign, "run_editor", edit)

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"seal_oracle_leak": mode}}

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        extra_cases={"c3": {"critical": True, "pass": True, "answer": NEEDLE}},
    )
    terminal = payloads(run, "attempt_closed")[0]
    if mode == "enforce":
        assert terminal["reason"] == "seal_refused" and terminal["code"] == "oracle_leak"
        assert not payloads(run, "attempt_sealed")
    else:
        assert result.terminal == "finished"
        assert payloads(run, "attempt_sealed")
        assert terminal.get("shadowed", []) == (
            [
                {
                    "check": "seal_oracle_leak",
                    "code": "oracle_leak",
                    "count": 1,
                    "leak": {"case_id": "c3", "oracle_pointer": "/answer"},
                }
            ]
            if mode == "shadow"
            else []
        )
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    private = [
        json.loads(line)
        for path in (run / "editor-logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert not any(row.get("check") == "seal_oracle_leak" for row in private)


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_editor_feedback_input_redaction_modes(mode):
    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from evolution.tree.roles.editor.editor_evidence import feedback_of
    from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2

    hits = HitSink()
    pin = EvidencePin(
        DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": mode}).input_modes()
    )
    shown, omitted = feedback_of(
        {"credential": SECRET}, BRIEF_LIMITS_V2, (SECRET,), pin=pin, hits=hits
    )
    assert shown == ({} if mode == "enforce" else {"credential": SECRET})
    assert omitted == (1 if mode == "enforce" else 0)
    assert hits.entries() == (
        [{"check": "input_run_secret_redaction", "code": "redacted", "count": 1}]
        if mode == "shadow"
        else []
    )


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_editor_brief_and_trace_use_the_pin_with_separate_hit_carriers(
    tmp_path, monkeypatch, mode
):
    import json

    from evolution.tree.roles.editor_contracts import brief_payload
    from tests.campaign_drivers import TWO_CASE_SCORER

    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_editor
    seen = []
    monkeypatch.setenv("EDITOR_INPUT_TEST_KEY", SECRET)

    async def capture(editor, request, **kwargs):
        seen.append((json.dumps(brief_payload(request.brief)), request.traces.whole("obs:c1:1")))
        return await original(editor, request, **kwargs)

    monkeypatch.setattr(campaign, "run_editor", capture)

    def configure(document):
        document["scorer"]["allowed_env"] = ["EDITOR_INPUT_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"input_run_secret_redaction": mode}}

    scorer = TWO_CASE_SCORER.replace(
        '"critical_pass": True,',
        '"critical_pass": True, "public_feedback": {"credential": os.environ["EDITOR_INPUT_TEST_KEY"]},',
    )
    run, result = await public_run(
        tmp_path, monkeypatch, configure=configure, scorer=scorer, script=SECRET
    )
    assert result.terminal == "finished"
    assert seen and all(
        (SECRET in brief) is (mode != "enforce") and (SECRET in trace) is (mode != "enforce")
        for brief, trace in seen
    )
    frozen = payloads(run, "attempt_input_frozen")[0]
    terminal = payloads(run, "editor_finished")[0]
    assert bool(frozen.get("shadowed")) is (mode == "shadow")
    assert bool(terminal.get("shadowed")) is (mode == "shadow")
    private = [
        json.loads(line)
        for path in (run / "editor-logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    logged = sum(row.get("count", 0) for row in private if row.get("event") == "check_shadowed")
    assert logged == sum(hit["count"] for hit in terminal.get("shadowed", []))
    # The fixture's command Reflector and fields Grouper perform no reads:
    # their input-build hits belong only to the durable terminal carrier.
    role_logs = [
        json.loads(line)
        for path in (run / "reflector-logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert not any(row.get("check") == "input_run_secret_redaction" for row in role_logs)
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize("text", [NEEDLE, SECRET], ids=["oracle", "run-secret"])
async def test_attempt_feedback_screen_modes(tmp_path, monkeypatch, mode, text):
    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_editor
    monkeypatch.setenv("EDITOR_INPUT_TEST_KEY", SECRET)

    async def edit(editor, request, **kwargs):
        outcome = await original(editor, request, **kwargs)
        path = request.workspace / "skills/SKILL.md"
        path.write_text(path.read_text() + "\n" + text + "\n")
        return outcome

    monkeypatch.setattr(campaign, "run_editor", edit)

    def configure(document):
        document["scorer"]["allowed_env"] = ["EDITOR_INPUT_TEST_KEY"]
        document["checks"] = {
            "version": "checks/1",
            "modes": {"seal_oracle_leak": "off", "attempt_feedback_screen": mode},
        }

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        campaign={
            "attempts_per_round": 2,
            "parallel_attempts": 1,
            "round_stop": "all_attempts",
            "attempt_feedback": "sequential",
        },
        extra_cases={"c3": {"critical": True, "pass": True, "answer": NEEDLE}},
    )
    assert result.terminal == "finished"
    frozen = payloads(run, "attempt_input_frozen")[1]
    feedback = frozen["feedback"]["attempts"][0]
    assert feedback["changes_status"] == ("withheld" if mode == "enforce" else "available")
    expected = "[REDACTED]" if text == SECRET else text
    assert (expected in feedback["diff_excerpt"]) is (mode != "enforce")
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert frozen.get("shadowed", []) == (
        [{"check": "attempt_feedback_screen", "code": "withheld", "count": 1}]
        if mode == "shadow"
        else []
    )
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_seal_shadow_hit_is_recomputed_after_published_candidate_recovery(
    tmp_path, monkeypatch
):
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from tests.test_tree_campaign_family15_delivery import PowerCut

    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_editor
    calls = []

    async def edit(editor, request, **kwargs):
        calls.append(request.attempt_id)
        outcome = await original(editor, request, **kwargs)
        path = request.workspace / "skills/SKILL.md"
        path.write_text(path.read_text() + "\n" + NEEDLE + "\n")
        return outcome

    monkeypatch.setattr(campaign, "run_editor", edit)

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"seal_oracle_leak": "shadow"}}

    original_hook = ArtifactStore._hook

    def cut(self, name):
        original_hook(self, name)
        if name == "after_seal_finished":
            raise PowerCut()

    extra = {"c3": {"critical": True, "pass": True, "answer": NEEDLE}}
    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "_hook", cut)
        with pytest.raises(PowerCut):
            await public_run(tmp_path, patch, configure=configure, extra_cases=extra)
    run = tmp_path / "run"
    assert not payloads(run, "attempt_closed")
    _, result = await public_run(tmp_path, monkeypatch, configure=configure, extra_cases=extra)
    assert result.terminal == "finished" and len(calls) == 1
    assert payloads(run, "attempt_closed")[0]["shadowed"] == [
        {
            "check": "seal_oracle_leak",
            "code": "oracle_leak",
            "count": 1,
            "leak": {"case_id": "c3", "oracle_pointer": "/answer"},
        }
    ]
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_rebase_preserves_frozen_editor_digest_and_applies_input_mode_next_round(
    tmp_path, monkeypatch
):
    import json

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.roles.editor_contracts import brief_payload
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut

    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_editor
    monkeypatch.setenv("EDITOR_INPUT_TEST_KEY", SECRET)
    seen = []

    async def capture(editor, request, **kwargs):
        seen.append(
            (
                request.round,
                json.dumps(brief_payload(request.brief)),
                request.traces.whole("obs:c1:1"),
            )
        )
        return await original(editor, request, **kwargs)

    monkeypatch.setattr(campaign, "run_editor", capture)
    scorer = TWO_CASE_SCORER.replace(
        'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
        'passed = bool(payload["oracle"].get("pass"))',
    )

    def configure(document):
        document["scorer"]["allowed_env"] = ["EDITOR_INPUT_TEST_KEY"]

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=configure,
            campaign={"rounds": 2},
            scorer=scorer,
            script=SECRET,
            cut="after_attempt_input_frozen",
        )
    run = tmp_path / "run"
    before = payloads(run, "attempt_input_frozen")[0]["common_input_digest"]

    def changed(document):
        configure(document)
        document["checks"] = {"version": "checks/1", "modes": {"input_run_secret_redaction": "off"}}

    with pytest.raises(EvolveRefused) as refusal:
        await public_run(
            tmp_path,
            monkeypatch,
            configure=changed,
            campaign={"rounds": 2},
            scorer=scorer,
            script=SECRET,
        )
    _, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=changed,
        campaign={"rounds": 2},
        scorer=scorer,
        script=SECRET,
        authorizations=[_decision(refusal.value, None, "accept input mode")],
    )
    assert result.terminal == "finished"
    assert {number for number, _, _ in seen} == {1, 2}
    assert all(
        (SECRET in brief) is (number == 2) and (SECRET in trace) is (number == 2)
        for number, brief, trace in seen
    )
    assert payloads(run, "attempt_input_frozen")[0]["common_input_digest"] == before
    assert payloads(run, "editor_started")[0]["common_input_digest"] == before
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def test_feedback_projection_applies_existing_bounds_after_journal_redaction():
    from evolution.tree.attempt_feedback import build_feedback
    from evolution.tree.execution.check_modes import redact_for_journal
    from tests.test_tree_attempt_feedback import source

    secret = "secret88"
    feedback = build_feedback(
        [source(changes_status="available", excerpt="x" * 2040 + secret)],
        current_index=2,
        exam_cases=(),
        redact=lambda value: redact_for_journal(value, (secret,)),
    )
    row = feedback["attempts"][0]
    assert len(row["diff_excerpt"]) == 2048
    assert row["diff_excerpt_truncated"] is True
    assert secret not in row["diff_excerpt"]


def test_unrepresentable_redacted_path_omits_the_whole_feedback_row_with_a_count():
    from evolution.tree.attempt_feedback import build_feedback
    from evolution.tree.execution.check_modes import redact_for_journal
    from tests.test_tree_attempt_feedback import source

    secret = "secret88"
    path = "skills/" + "/".join([secret * 25] * 17)
    assert len(path) < 4096 < len(path.replace(secret, "[REDACTED]"))
    feedback = build_feedback(
        [source(changes_status="available", changed_paths=(path,), excerpt="ordinary")],
        current_index=2,
        exam_cases=(),
        redact=lambda value: redact_for_journal(value, (secret,)),
    )
    assert feedback == {"attempts": [], "omitted_attempts": 1}
