"""Wave B probe (spec §3): core a17 continues a suspended campaign on a new
target instance that holds none of the first start's instances.

Owner sweeps the run_id namespace and a resumed start runs on a different
target (a new boot). This drives the same situation through the legacy runner
so the core contract is proven before Lab builds on it.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading

import pytest
from scaffold_15 import scaffold_15_with_legacy_failures

from suspension_faults import FailingTurns
from ports import free_port

pytestmark = pytest.mark.slow


def _serve(manifest, root, port):
    from evolution.tree import load_manifest_v2, serve_reference_target
    from evolution.tree.reference_target_cli import bind_reference_target
    target = bind_reference_target(load_manifest_v2(manifest), allowed_root=root)
    flag = root / "target-broken"
    flag.touch()
    faults = FailingTurns(target, mode=f"fail_after:1:{flag}")
    server = serve_reference_target(target, port=port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    return server, thread, faults


def _close(server, thread):
    server.shutdown(); thread.join(timeout=5); server.server_close()


async def test_core_continues_a_suspended_campaign_on_a_fresh_target_instance(tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import resume_native, run_native
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    lab_wheel = os.environ.get("EVOLUTION_LAB_WHEEL")
    if not wheel or not lab_wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL and EVOLUTION_LAB_WHEEL to the installed producing pair")
    port = free_port()
    sample = scaffold_15_with_legacy_failures(tmp_path / "sample")
    raw = json.loads(sample.manifest.read_text())
    raw["target"]["base_url"] = f"http://127.0.0.1:{port}"
    raw["campaign"]["call_retries"] = {"max_attempts": 1, "turn_max_attempts": 1}
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    sample.manifest.write_text(json.dumps(raw))
    inputs = prepare_inputs(sample.manifest, wheel_paths=(Path(lab_wheel), Path(wheel)))
    run = tmp_path / "run"

    server, thread, _faults = _serve(sample.manifest, tmp_path, port)
    try:
        first = await asyncio.wait_for(run_native(inputs, run, dispatch_guard=lambda: None), 120)
    finally:
        _close(server, thread)
    assert first.exit_code == 1, first.stderr.read_text()
    status = inspect_campaign(run).to_payload()
    assert status["suspended"] is True and status["complete"] is False
    assert status["suspensions"][-1]["reason"] == "target_unstable"
    events = [json.loads(line)["event"] for line in (run / "campaign.jsonl").read_text().splitlines()]
    assert events.index("connection_preflight_finished") < events.index("campaign_suspended")

    # A new bound reference target: a fresh instance store and a new boot,
    # healthy from the start. Nothing of the first start's namespace survives.
    server, thread, faults = _serve(sample.manifest, tmp_path, port)
    faults.failing.clear()
    try:
        await asyncio.wait_for(resume_native(inputs, run, dispatch_guard=lambda: None), 180)
    finally:
        _close(server, thread)
    final = inspect_campaign(run, verify=True).to_payload()
    assert final["complete"] is True and final["suspended"] is False
    assert len(final["suspensions"]) >= 1
