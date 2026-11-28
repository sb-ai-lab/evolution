"""Native dispatch is guarded and never automatically resumes an existing run."""
from pathlib import Path
import json
import os
import sys
import threading
import asyncio
import socket

import pytest
from ownership_modes import ownership_mode  # noqa: F401
from scaffold_15 import scaffold_15

pytestmark = pytest.mark.slow


def prepared(tmp_path):
    from evolution_lab.recipe import prepare_inputs
    scaffold_15(tmp_path / "sample")
    return prepare_inputs(tmp_path / "sample/manifest.json")


async def test_guard_refusal_happens_before_process_or_output_creation(tmp_path):
    from evolution_lab.budget import BudgetExhausted
    from evolution_lab.runner import run_native
    inputs = prepared(tmp_path)
    def refused(): raise BudgetExhausted("not authorized")
    with pytest.raises(BudgetExhausted):
        await run_native(inputs, tmp_path / "run", dispatch_guard=refused)
    assert not (tmp_path / "run").exists() and not (tmp_path / "run-lab-logs").exists()


async def test_existing_run_is_never_implicitly_resumed(tmp_path):
    from evolution_lab.runner import run_native
    inputs = prepared(tmp_path)
    run = tmp_path / "run"; run.mkdir(); (run / "marker").write_text("preserve")
    with pytest.raises(FileExistsError):
        await run_native(inputs, run, dispatch_guard=lambda: None)
    assert (run / "marker").read_text() == "preserve"


async def test_run_requires_an_exact_verified_core_wheel(tmp_path):
    from evolution_lab.runner import run_native
    inputs = prepared(tmp_path)
    with pytest.raises(ValueError, match="core wheel"):
        await run_native(inputs, tmp_path / "run", dispatch_guard=lambda: None)
    assert not (tmp_path / "run").exists()


@pytest.fixture
def native_sample(tmp_path):
    from evolution.tree import load_manifest_v2, serve_reference_target
    from evolution.tree.reference_target_cli import bind_reference_target
    from evolution_lab.recipe import prepare_inputs
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    if not wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL to the installed producing wheel")
    sample = scaffold_15(tmp_path / "sample")
    target = bind_reference_target(load_manifest_v2(sample.manifest), allowed_root=tmp_path)
    server = serve_reference_target(target, port=0)
    raw = json.loads(sample.manifest.read_text())
    raw["target"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    sample.manifest.write_text(json.dumps(raw))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield prepare_inputs(sample.manifest, wheel_paths=(Path(wheel),))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.usefixtures("ownership_mode")
async def test_native_multiround_campaign_and_report(native_sample, tmp_path):
    from evolution_lab.runner import run_native
    from evolution_lab.report import read_campaign_report
    result = await run_native(native_sample, tmp_path / "run", dispatch_guard=lambda: None)
    assert result.exit_code == 0, result.stderr.read_text()
    assert not result.stop_requested
    pins = json.loads((result.stdout.parent / "prepared-inputs.json").read_text())
    assert pins["distributions"][0]["sha256"] == native_sample.wheels[0].sha256
    report = read_campaign_report(tmp_path / "run", distributions=native_sample.wheels)
    core = report.data["core"]
    assert core["complete"] and len(core["rounds"]) == 2
    assert report.data["producing_version"] == next(
        pin.version for pin in native_sample.wheels if pin.name == "evo-skills")
    assert report.data["measurements"]
    assert all(row["coverage"]["state"] == "complete" for row in report.data["measurements"])
    assert report.data["unavailable_measurements"] == []
    assert report.data["lab_notices"] == [] or report.data["lab_notices"][0]["code"] == "no_candidate_measured"
    # Final review I1: the round details are read on a real run and carry codes only, never the Reflector's texts.
    assert report.data["lab_rounds"] is not None and len(report.data["lab_rounds"]) == len(core["rounds"])
    assert all(row["source"] == "core_raw" for row in report.data["lab_rounds"])
    assert (report.data["lab_rounds_state"], report.data["lab_rounds_reason"]) == ("present", None)
    assert any(row["reflection"]["state"] == "present" for row in report.data["lab_rounds"])
    assert all(set(row["reflection"]) == {"state", "error_class", "reason"} for row in report.data["lab_rounds"])
    report.write(tmp_path / "report")
    from evolution_lab.owner import core_raw
    def unreadable(root):
        raise core_raw.CoreRawUnavailable("unreadable")
    def unsupported(root):
        raise core_raw.CoreRawUnavailable("core_raw_unsupported")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core_raw, "campaign_details", unreadable)
        degraded = read_campaign_report(tmp_path / "run").data
        assert degraded["lab_rounds"] is None
        assert (degraded["lab_rounds_state"], degraded["lab_rounds_reason"]) == ("unavailable", "unreadable")
        assert all("reflection_errors" not in n for n in degraded["lab_notices"])
        patch.setattr(core_raw, "campaign_details", unsupported)
        with pytest.raises(core_raw.CoreRawUnavailable):
            read_campaign_report(tmp_path / "run")
    with (tmp_path / "run/campaign.jsonl").open("ab") as stream:
        stream.write(b'{"torn":')
    partial = read_campaign_report(tmp_path / "run")
    assert partial.data["campaign_journal_integrity"] == "incomplete_tail"
    assert "incomplete_tail" in partial.markdown()


@pytest.mark.usefixtures("ownership_mode")
async def test_budget_refusal_stops_owned_campaign_without_restart(native_sample, tmp_path):
    from evolution_lab.budget import BudgetExhausted
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import run_native
    # A real child waits inside the scorer; its marker makes the stop deterministic.
    scorer = native_sample.manifest.path.parent / "scorer/score.py"
    marker = tmp_path / "scorer-started"
    scorer.write_text(scorer.read_text().replace(
        "import json\n", "from pathlib import Path\nimport time\n"
        f"import os\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\nimport json\n"))
    prepared_again = prepare_inputs(native_sample.manifest.path,
                                    wheel_paths=tuple(pin.path for pin in native_sample.wheels))
    def guard():
        if marker.exists():
            raise BudgetExhausted("offline limit reached")
    result = await asyncio.wait_for(
        run_native(prepared_again, tmp_path / "run", dispatch_guard=guard, grace_seconds=1), timeout=10)
    assert marker.exists() and result.stop_requested
    assert result.forced_stop
    with pytest.raises(ProcessLookupError): os.kill(int(marker.read_text()), 0)
    saved = json.loads((result.stdout.parent / "process-result.json").read_text())
    assert saved["forced_stop"] and saved["cleanup_complete"]
    assert saved["reason"] == "budget_exhausted"
    assert result.stdout.is_file() and result.stderr.is_file()
    with pytest.raises(FileExistsError):
        await run_native(prepared_again, tmp_path / "run", dispatch_guard=lambda: None)


@pytest.mark.usefixtures("ownership_mode")
async def test_drift_is_refused_before_starting_native_core(native_sample, tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.runner import run_native
    native_sample.manifest.path.write_text("{}")
    with pytest.raises(InputChanged):
        await run_native(native_sample, tmp_path / "run", dispatch_guard=lambda: None)
    assert not (tmp_path / "run-lab-logs").exists()


@pytest.mark.parametrize("exhaust", [False, True])
@pytest.mark.usefixtures("ownership_mode")
async def test_actual_core_chat_editor_uses_budget_endpoint(native_sample, tmp_path, monkeypatch, exhaust):
    import httpx
    import uvicorn
    from evolution_lab.budget import BudgetExhausted, BudgetLedger, Gateway, Price, Route, create_app
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import run_native
    from evolution_lab.report import read_campaign_report
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=1100000 if exhaust else 2000000, identity="chat-offline")
    calls = []
    def upstream(request):
        calls.append(json.loads(request.content))
        assert ledger.summary()["unknown_requests"] == 1
        index = (len(calls) - 1) % 3
        tool, args = [("list_files", {}), ("record_plan", {"summary": "keep", "changes": []}),
                      ("finish_edit", {"summary": "keep"})][index]
        payload = {"id": f"reply-{len(calls)}", "object": "chat.completion", "created": 0,
                   "model": "editor-model", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                       "role": "assistant", "content": None, "tool_calls": [{"id": f"call-{len(calls)}",
                       "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}}]}
        if not exhaust:
            payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return httpx.Response(200, json=payload)
    route = Route("https://offline.invalid/v1", "synthetic-key", "editor-model", Price(1, 1, 1000), 100, client_token="client-key")
    gateway = Gateway(ledger, {"editor": route}, transport=httpx.MockTransport(upstream))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(gateway), log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        raw = json.loads(native_sample.manifest.path.read_text())
        raw["editor"] = {"kind": "chat", "model": "editor-model", "api_key_env": "LAB_OFFLINE_EDITOR_KEY",
                         "base_url": f"http://127.0.0.1:{sock.getsockname()[1]}/editor/v1",
                         "max_output_tokens": 100, "max_turns": 8,
                         "context_compact_chars": 128_000, "context_message_max_chars": 12_000,
                         # /15 requires the author's own retry on a chat role; Lab never supplies it (PDR-0015).
                         "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                                   "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8,
                                               "jitter": "full"}}}
        native_sample.manifest.path.write_text(json.dumps(raw))
        monkeypatch.setenv("LAB_OFFLINE_EDITOR_KEY", "client-key")
        inputs = prepare_inputs(native_sample.manifest.path, wheel_paths=tuple(pin.path for pin in native_sample.wheels))
        def guard():
            if gateway.exhausted():
                raise BudgetExhausted("offline cap")
        result = await asyncio.wait_for(run_native(inputs, tmp_path / "run", dispatch_guard=guard), timeout=15)
        if exhaust:
            assert result.stop_requested
            assert len(calls) == 1 and ledger.summary()["unknown_requests"] == 1
        else:
            assert result.exit_code == 0, result.stderr.read_text()
            report = read_campaign_report(tmp_path / "run")
            # With role resilience (the /11 `retry`) core a21's chat Editor makes one continuation turn
            # after an empty `finish_edit` ("The brief contains a diagnosis and your own plan..."), so the
            # scripted upstream is called 12 times over the two rounds (6 on a17); every call is metered.
            # See docs/notes/core-a21-pin.md.
            assert len(calls) == 12
            assert all((attempt["outcome"], attempt["reason"]) == ("abandoned", "editor_declined")
                       for row in report.data["core"]["rounds"] for attempt in row["attempts"])
            assert ledger.summary()["requests"] == 12 and ledger.summary()["unknown_requests"] == 0
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()


@pytest.mark.usefixtures("ownership_mode")
async def test_default_grace_allows_core_terminal_stop(native_sample, tmp_path):
    from evolution_lab.budget import BudgetExhausted
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import run_native
    from evolution_lab.report import read_campaign_report
    scorer = native_sample.manifest.path.parent / "scorer/score.py"
    marker = tmp_path / "scoring"
    scorer.write_text(scorer.read_text().replace("import json\n", "from pathlib import Path\nimport time\n"
        f"Path({str(marker)!r}).touch()\ntime.sleep(.25)\nimport json\n"))
    pinned = prepare_inputs(native_sample.manifest.path, wheel_paths=tuple(p.path for p in native_sample.wheels))
    def guard():
        if marker.exists(): raise BudgetExhausted("test")
    result = await asyncio.wait_for(run_native(pinned, tmp_path / "run", dispatch_guard=guard), 15)
    assert result.stop_requested and not result.forced_stop
    report = read_campaign_report(tmp_path / "run")
    assert report.data["core"]["campaign_outcome"]["outcome"] == "stopped"
    assert report.data["lab_result"]["reason"] == "budget_exhausted"


@pytest.mark.usefixtures("ownership_mode")
async def test_guard_exception_is_persisted_and_owned_processes_cleaned(native_sample, tmp_path):
    from evolution_lab.runner import run_native
    checks = 0
    def guard():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("PRIVATE-GUARD-ERROR")
    with pytest.raises(RuntimeError):
        await run_native(native_sample, tmp_path / "run", dispatch_guard=guard, grace_seconds=.1)
    saved = (tmp_path / "run-lab-logs/process-result.json").read_text()
    assert json.loads(saved)["reason"] == "exception"
    assert "PRIVATE-GUARD-ERROR" not in saved


@pytest.mark.usefixtures("ownership_mode")
async def test_task_cancellation_reaps_scorer_and_persists_result(native_sample, tmp_path):
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.runner import run_native
    scorer = native_sample.manifest.path.parent / 'scorer/score.py'
    marker = tmp_path / 'scorer-pid'
    scorer.write_text(scorer.read_text().replace('import json\n',
        f'import os,time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\nimport json\n'))
    inputs = prepare_inputs(native_sample.manifest.path, wheel_paths=tuple(p.path for p in native_sample.wheels))
    task = asyncio.create_task(run_native(inputs, tmp_path / 'run', dispatch_guard=lambda: None, grace_seconds=.1))
    try:
        for _ in range(500):
            if marker.exists() and marker.read_text(): break
            await asyncio.sleep(.01)
        assert marker.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        with pytest.raises(ProcessLookupError): os.kill(int(marker.read_text()), 0)
        saved = json.loads((tmp_path / 'run-lab-logs/process-result.json').read_text())
        assert saved['reason'] == 'cancelled' and saved['cleanup_complete'] and saved['forced_stop']
    finally:
        if not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


@pytest.mark.usefixtures("ownership_mode")
async def test_run_native_holds_supervisor_lock_and_reports_caller(native_sample, tmp_path):
    import fcntl
    from evolution_lab.runner import run_native
    result = await run_native(native_sample, tmp_path / "run", dispatch_guard=lambda: None,
                              kill_on_caller_exit=True)
    assert result.exit_code == 0, result.stderr.read_text()
    logs = tmp_path / "run-lab-logs"
    lock = logs / "supervisor.lock"
    assert lock.is_file() and (lock.stat().st_mode & 0o777) == 0o600
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released after the supervisor exited
    assert json.loads((logs / "supervisor-result.json").read_text())["caller_lost"] is False
    saved = json.loads((logs / "process-result.json").read_text())
    assert saved["version"] == "lab-process-result/1" and "caller_lost" not in saved


@pytest.mark.parametrize('platform', ['linux', 'linux2', 'darwin', 'win32'])
def test_admit_platforms(tmp_path, monkeypatch, platform):
    from evolution_lab.runner import _admit
    inputs = prepared(tmp_path)
    monkeypatch.setattr(sys, 'platform', platform)
    if platform == 'win32':
        with pytest.raises(ValueError, match='native process ownership'):
            _admit(inputs, 1.0)
    else:
        assert _admit(inputs, 1.0) == 1.0
