"""Family 7: a legacy run suspends on an unstable target, the report shows it,
and resume_native continues the same run directory (PDR-0014)."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading

import pytest
from scaffold_15 import scaffold_15_with_legacy_failures

from suspension_faults import FailingTurns

pytestmark = pytest.mark.slow


@pytest.fixture
def unstable_sample(tmp_path):
    from evolution.tree import load_manifest_v2, serve_reference_target
    from evolution.tree.reference_target_cli import bind_reference_target
    from evolution_lab.recipe import prepare_inputs
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    lab_wheel = os.environ.get("EVOLUTION_LAB_WHEEL")
    if not wheel or not lab_wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL and EVOLUTION_LAB_WHEEL to the installed producing pair")
    sample = scaffold_15_with_legacy_failures(tmp_path / "sample")
    target = bind_reference_target(load_manifest_v2(sample.manifest), allowed_root=tmp_path)
    flag = tmp_path / "target-broken"
    flag.write_text("")
    FailingTurns(target, mode=f"fail_after:1:{flag}")
    server = serve_reference_target(target, port=0)
    raw = json.loads(sample.manifest.read_text())
    raw["target"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
    # One attempt per call: an exhausted call is immediate, so the test is fast.
    raw["campaign"]["call_retries"] = {"max_attempts": 1, "turn_max_attempts": 1}
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    sample.manifest.write_text(json.dumps(raw))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield prepare_inputs(sample.manifest, wheel_paths=(Path(lab_wheel), Path(wheel))), flag
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()


async def test_suspended_legacy_run_is_reported_and_resume_native_finishes_it(unstable_sample, tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.report import read_campaign_report
    from evolution_lab.runner import resume_native, run_native
    inputs, flag = unstable_sample
    run = tmp_path / "run"
    first = await asyncio.wait_for(run_native(inputs, run, dispatch_guard=lambda: None), 120)
    assert first.exit_code == 1, first.stderr.read_text()
    status = inspect_campaign(run).to_payload()
    assert status["suspended"] is True and status["complete"] is False
    assert status["suspensions"][-1]["reason"] == "target_unstable"
    events = [json.loads(line)["event"] for line in (run / "campaign.jsonl").read_text().splitlines()]
    assert events.index("connection_preflight_finished") < events.index("campaign_suspended")

    report = read_campaign_report(run)
    assert report.data["core"]["suspended"] is True
    text = report.markdown()
    assert "Кампания приостановлена: target_unstable (владелец target, evidence threshold)." in text
    assert "resume_native" in text
    assert any(row.get("call_failures_by_code") for row in report.data["measurements"])

    # The H1 client also creates a `-lab-logs` directory but never takes the
    # supervisor lock (`resume_native` refuses such a run); the legacy
    # continuation line must key off that lock, not off `lab_starts` alone.
    logs = run.with_name(run.name + "-lab-logs")
    lock = logs / "supervisor.lock"
    lock.rename(logs / "supervisor.lock.aside")
    try:
        non_legacy_text = read_campaign_report(run).markdown()
        assert "Продолжение средствами Lab в этой версии недоступно; папка прогона сохранена." in non_legacy_text
        assert "`resume_native`" not in non_legacy_text
    finally:
        (logs / "supervisor.lock.aside").rename(lock)  # resume_native needs it

    flag.unlink()
    await asyncio.wait_for(resume_native(inputs, run, dispatch_guard=lambda: None), 180)
    final = inspect_campaign(run, verify=True).to_payload()
    assert final["complete"] is True and final["suspended"] is False
    assert len(final["suspensions"]) >= 1
