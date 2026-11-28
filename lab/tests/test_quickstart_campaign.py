"""A project campaign uses the existing preparation and Owner paths offline."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import jsonschema
import pytest
from evolution_lab.cli.parser import main
from evolution_lab.quickstart import QuickstartError, new_project
from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from test_owner_core_raw import (
    finished_campaign,  # noqa: F401 — real Owner result for verdict fixtures
)

pytestmark = pytest.mark.slow


def _campaign(project, **kwargs):
    from evolution_lab.quickstart._campaign import campaign
    kwargs.setdefault("environ", {})
    return asyncio.run(campaign(project, wheels=(_installed_lab_wheel(), CORE_WHEEL), **kwargs))


@pytest.mark.parametrize('parent_relative', [False, True])
def test_campaign_runs_the_untouched_skeleton_offline(tmp_path, monkeypatch, parent_relative):
    from evolution_lab.quickstart._campaign import summary_schema
    project = new_project(tmp_path / "project")
    original = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    argument = project
    if parent_relative:
        work = tmp_path / 'work'
        work.mkdir()
        monkeypatch.chdir(work)
        argument = Path('../project')
    result = _campaign(argument)
    assert result.exit_code in (0, 1), result.summary
    assert result.summary["campaign"]["outcome"] == "completed", result.summary
    assert result.summary["campaign"]["final_champion"] is not None
    assert result.summary["blocker_count"] == 0
    assert result.run_dir == project / ".lab/runs/001"
    assert (project / ".lab/latest").resolve() == result.run_dir
    for name in ("summary.json", "resolved.json", "report.md", "changes.diff", "campaign.log"):
        assert (result.run_dir / name).is_file(), name
    jsonschema.Draft202012Validator(summary_schema()).validate(result.summary)
    for path, data in original.items():
        assert (project / path).read_bytes() == data
    broken = {**result.summary, "unexpected": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(summary_schema()).validate(broken)


@pytest.mark.parametrize("outcome,promoted,refused,blockers,exit_code", [
    ("completed", True, False, 0, 0),
    ("completed", False, False, 0, 1),
    (None, False, False, 1, 3),
    ("suspended", False, False, 0, 4),
    (None, False, True, 0, 5),
])
def test_campaign_exit_codes(outcome, promoted, refused, blockers, exit_code):
    from evolution_lab.quickstart._campaign import _campaign_verdict
    summary = {"blocker_count": blockers, "final_target": {"state": "stopped", "complete": True},
               "campaign": {"outcome": None if outcome == "suspended" else outcome,
                            "suspended": outcome == "suspended", "promoted": promoted, "refusal": {} if refused else None,
                            "failure": "core_refused" if refused else None, "persistence_state": "saved",
                            "process_exit": 2 if refused else 0, "cleanup_complete": True}}
    assert _campaign_verdict(summary)[0] == exit_code


def test_campaign_missing_secret_refuses_before_state(tmp_path):
    project = new_project(tmp_path / "project")
    path = project / "manifest.json"
    document = json.loads(path.read_text())
    document["editor"]["allowed_env"] = ["EDITOR_KEY"]
    path.write_text(json.dumps(document))
    with pytest.raises(QuickstartError) as caught:
        _campaign(project)
    assert caught.value.code == "secret_missing"
    assert caught.value.role == "editor"
    assert not (project / ".lab").exists()



def test_campaign_chat_editor_key_uses_core_inventory(tmp_path):
    project = new_project(tmp_path / "project")
    path = project / "manifest.json"
    document = json.loads(path.read_text())
    document["editor"] = {"kind": "chat", "model": "mock", "base_url": "http://127.0.0.1:1",
                          "api_key_env": "EDITOR_KEY", "context_compact_chars": 60000,
                          "context_message_max_chars": 12000, "retry": {
                              "version": "role_call_retry/1", "max_attempts": 1,
                              "total_budget_seconds": 30,
                              "backoff": {"initial_seconds": 0, "max_seconds": 0,
                                          "multiplier": 1, "jitter": "full"}}}
    path.write_text(json.dumps(document))
    with pytest.raises(QuickstartError) as caught:
        _campaign(project)
    assert caught.value.code == "secret_missing"
    assert caught.value.role == "editor"
    assert not (project / ".lab").exists()

def test_lab_campaign_operation_id_still_reads_the_campaign_view(tmp_path):
    from test_service_cli import FakeClient
    payload = {"schema": "lab-campaign-view/15", "operation_id": "operation"}
    outputs = []
    for name in ("campaign", "view"):
        client = FakeClient([payload])
        out, err = io.StringIO(), io.StringIO()
        assert main(["--socket", str(tmp_path / "service.sock"), "--json", name, "operation"],
                    stdout=out, stderr=err, client_factory=lambda client=client, **_: client) == 0
        assert client.calls == [("GET", "/v1/operations/operation/campaign", None, False, "lab-campaign-view/15")]
        outputs.append(out.getvalue())
        assert ("lab view" in err.getvalue()) == (name == "campaign")
    assert outputs[0] == outputs[1]


def test_campaign_directory_never_connects_to_service(tmp_path):
    project = new_project(tmp_path / "project")
    out, err = io.StringIO(), io.StringIO()
    def forbidden(**kwargs):
        pytest.fail("project campaign must not open a service client")
    assert main(["campaign", str(project)], stdout=out, stderr=err, client_factory=forbidden) == 2
    assert "--wheel" in err.getvalue()


def test_just_campaign_recipe_exists():
    text = (Path(__file__).parents[1] / "justfile").read_text()
    assert "campaign dir *args:" in text
    section = text.split("campaign dir *args:", 1)[1].split("\n\n", 1)[0]
    assert 'campaign "{{dir}}"' in section
    assert '--wheel "{{lab_wheel}}" --wheel "{{core_wheel}}"' in section



def test_campaign_cli_runs_offline_and_prints_closed_json(tmp_path):
    project = new_project(tmp_path / "project")
    out, err = io.StringIO(), io.StringIO()
    code = main(["campaign", str(project), "--json", "--wheel", str(_installed_lab_wheel()),
                 "--wheel", str(CORE_WHEEL)], stdout=out, stderr=err,
                client_factory=lambda **_: pytest.fail("offline command opened a service client"))
    assert code in (0, 1), err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["schema"] == "lab-campaign-summary/1"
    assert payload["exit_code"] == code
    assert payload["campaign"]["outcome"] == "completed"
    assert not err.getvalue()


def test_cancelling_campaign_stops_its_started_target(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module("evolution_lab.quickstart._campaign")
    from evolution_lab.owner import Owner
    project = new_project(tmp_path / "project")
    scratch_paths = []
    original_scratch = module.TemporaryDirectory
    def scratch(**kwargs):
        result = original_scratch(**kwargs)
        scratch_paths.append(Path(result.name))
        return result
    monkeypatch.setattr(module, "TemporaryDirectory", scratch)
    async def scenario():
        ready = asyncio.Event()
        async def hold(owner, bundle, prepared, launch, target_secrets, role_secrets,
                       summary, targets, operation, log, *, action):
            assert scratch_paths and all(not (path / "source").exists() for path in scratch_paths)
            connection = await owner.bind(bundle.digest)
            await module._start_target(owner, connection, target_secrets, targets)
            ready.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(module, "_run_campaign", hold)
        task = asyncio.create_task(module.campaign(project, wheels=(_installed_lab_wheel(), CORE_WHEEL), environ={}))
        try:
            await asyncio.wait_for(ready.wait(), 20)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        from evolution_lab.quickstart._evaluate import installed_versions, pair_label
        state = project / ".lab" / ("state-" + pair_label(*installed_versions()))
        async with Owner.open(state) as owner:
            assert not (await owner.health()).blockers
            assert all(target.state == "stopped" for target in await owner.list_targets())
    asyncio.run(scenario())


def test_real_owner_suspension_is_exit_four_with_its_reason(tmp_path):
    from evolution_lab.owner import Owner
    from evolution_lab.quickstart._campaign import _campaign_facts, _campaign_verdict
    from evolution_lab.quickstart._readout import render_campaign_readout
    from test_owner_resume import _suspended_operation, _suspending

    state, bundle, prepared, _ = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, status = await _suspended_operation(owner, bundle, prepared)
            try:
                assert status.result.core["campaign_outcome"] is None
                row = _campaign_facts(status)
                summary = {"campaign": row, "blocker_count": 0,
                           "final_target": {"state": "stopped", "complete": True},
                           "next_command": "lab campaign project", "exit_code": 4}
                assert _campaign_verdict(summary)[0] == 4, row
                assert row["suspended"] is True
                assert row["suspension_reason"] == "target_unstable"
                text = render_campaign_readout(summary, tmp_path)
                assert "Campaign: suspended (target_unstable)" in text
                assert "not started" not in text
                row["cleanup_complete"] = False
                assert _campaign_verdict(summary)[0] == 3
                row["cleanup_complete"] = True
                row["persistence_state"] = "failed"
                assert _campaign_verdict(summary)[0] == 3
            finally:
                await owner.wait(await owner.stop_target(target))
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,process_exit", [("completed", 0), ("stopped", 3), ("budget_exhausted", 4)])
@pytest.mark.parametrize("promoted", [True, False])
def test_terminal_core_payload_uses_core_exit_codes(finished_campaign, outcome, process_exit, promoted):  # noqa: F811
    from dataclasses import replace

    from evolution_lab.quickstart._campaign import _campaign_facts, _campaign_verdict
    # Adapt a real Owner projection at the outcome/process boundary. Status
    # projections expose promotion in rounds, not CampaignResult.initial_champion.
    status = finished_campaign[2]
    core = status.result.to_payload()["core"]
    assert "initial_champion" not in core
    core["campaign_outcome"] = {"outcome": outcome, "reason": "budget" if outcome == "budget_exhausted" else "done"}
    for index, round_ in enumerate(core["rounds"]):
        round_["outcome"] = "promoted" if promoted and index == 0 else "clean"
    result = replace(status.result, core=core, process=replace(status.result.process, exit_code=process_exit))
    row = _campaign_facts(replace(status, result=result))
    summary = {"campaign": row, "blocker_count": 0, "final_target": {"state": "stopped", "complete": True}}
    assert _campaign_verdict(summary)[0] == (0 if promoted else 1), row


def test_campaign_project_error_is_exit_two(tmp_path):
    with pytest.raises(QuickstartError) as caught:
        _campaign(tmp_path / "absent")
    assert caught.value.exit_code == 2


def test_project_campaign_reports_a_real_promotion(tmp_path):
    from evolution_lab.projects.demo import campaign_project
    project = campaign_project(tmp_path / "project")
    result = _campaign(project)
    assert result.exit_code == 0, result.summary
    assert result.summary["campaign"]["promoted"] is True
    assert result.summary["campaign"]["final_champion"] == "v1"
    assert result.summary["campaign"]["diff_state"] == "changed"
