"""Explicit continuation of an interrupted native campaign (spec 2026-09-17)."""
import asyncio
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from scaffold_15 import apply_legacy_failure_profile, scaffold_15

pytestmark = pytest.mark.slow

SRC = Path(__file__).resolve().parents[1] / "src"


@dataclass
class Sample:
    manifest: Path
    wheels: tuple[Path, ...]
    hold: Path     # while present, the scorer sleeps float(hold.read_text()) seconds
    marker: Path   # the sleeping scorer's pid

    def prepare(self, wheels=None):
        from evolution_lab.recipe import prepare_inputs
        return prepare_inputs(self.manifest, wheel_paths=self.wheels if wheels is None else wheels)


@pytest.fixture
def pair_sample(tmp_path):
    from evolution.tree import load_manifest_v2, serve_reference_target
    from evolution.tree.reference_target_cli import bind_reference_target
    core, lab = os.environ.get("EVOLUTION_CORE_WHEEL"), os.environ.get("EVOLUTION_LAB_WHEEL")
    if not core or not lab:
        pytest.skip("set EVOLUTION_CORE_WHEEL and EVOLUTION_LAB_WHEEL to the installed producing pair")
    sample = scaffold_15(tmp_path / "sample")
    target = bind_reference_target(load_manifest_v2(sample.manifest), allowed_root=tmp_path)
    server = serve_reference_target(target, port=0)
    raw = json.loads(sample.manifest.read_text())
    raw["target"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    sample.manifest.write_text(json.dumps(raw))
    hold, marker = tmp_path / "hold", tmp_path / "scorer-pid"
    scorer = sample.manifest.parent / "scorer/score.py"
    # Fixed bytes: deleting or rewriting `hold` never changes the campaign identity.
    scorer.write_text(scorer.read_text().replace("import json\n",
        f"import os, time\nfrom pathlib import Path\n_hold = Path({str(hold)!r})\n"
        f"if _hold.exists():\n    Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        f"    time.sleep(float(_hold.read_text() or '60'))\nimport json\n", 1))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield Sample(sample.manifest, (Path(lab), Path(core)), hold, marker)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def records(run):
    path = run / "campaign.jsonl"
    rows = []
    if path.exists():
        for line in path.read_bytes().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def wait_for(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def wait_event(run, event, timeout=60.0):
    wait_for(lambda: any(row.get("event") == event for row in records(run)), timeout)


def spawn_caller(sample, run, mode):
    code = (
        "import asyncio, sys\nfrom pathlib import Path\n"
        "from evolution_lab.recipe import prepare_inputs\n"
        "from evolution_lab.runner import run_native, resume_native\n"
        f"prepared = prepare_inputs(Path({str(sample.manifest)!r}), wheel_paths={tuple(map(str, sample.wheels))!r})\n"
        f"call = {mode}\n"
        f"asyncio.run(call(prepared, Path({str(run)!r}), dispatch_guard=lambda: None, kill_on_caller_exit=True))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.Popen([sys.executable, "-c", code], env=env)


def kill_caller(caller, status):
    """Kill `caller` and return `status` once it exists *and* parses as a
    complete JSON object. The supervisor renames a complete status into place;
    the parse retry within the same deadline stays as a guard against reading
    a status written by an older supervisor that created it before dumping."""
    caller.kill()
    caller.wait()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            parsed = json.loads(status.read_text())
        except (OSError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def killed_after_baseline(sample, run):
    """Start 1 dies right after the baseline; the held scorer keeps the campaign open.

    Timing assumption: the kill lands right after `baseline_complete`, before the
    first candidate measurement opens (the candidate must be edited first).
    This relies on the held scorer: the hold is written before the kill, so even
    if a slow kill let a candidate measurement open, its scorer would block
    rather than score it."""
    caller = spawn_caller(sample, run, "run_native")
    try:
        wait_event(run, "baseline_complete")
        sample.hold.write_text("60")
        status = kill_caller(caller, run.with_name(run.name + "-lab-logs") / "supervisor-result.json")
    finally:
        if caller.poll() is None:
            caller.kill()
    assert status["caller_lost"] is True
    return status


async def test_refusals_before_any_start(pair_sample, tmp_path):
    from evolution_lab.runner import ResumeRefused, resume_native
    prepared = pair_sample.prepare()
    run = tmp_path / "run"
    with pytest.raises(ResumeRefused) as missing:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert missing.value.code == "not_a_lab_campaign"
    run.mkdir()
    with pytest.raises(ResumeRefused) as no_logs:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert no_logs.value.code == "not_a_lab_campaign"
    logs = tmp_path / "run-lab-logs"
    logs.mkdir()
    (logs / "prepared-inputs.json").write_text(json.dumps(prepared.public()))
    (logs / "supervisor.lock").touch()  # marks a run_native start, so the refusal reaches the journal check
    with pytest.raises(ResumeRefused) as absent:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert absent.value.code == "campaign_absent"
    with pytest.raises(ValueError, match="Lab and core wheels"):
        await resume_native(pair_sample.prepare(wheels=pair_sample.wheels[1:]), run, dispatch_guard=lambda: None)
    assert not list(logs.glob("start-*"))


async def test_h1_style_logs_without_supervisor_lock_are_not_a_lab_campaign(pair_sample, tmp_path):
    """H1 client and Owner write the same root prepared-inputs.json but never supervisor.lock."""
    from evolution_lab.runner import ResumeRefused, resume_native
    prepared = pair_sample.prepare()
    run, logs = tmp_path / "run", tmp_path / "run-lab-logs"
    run.mkdir()
    logs.mkdir()
    (logs / "prepared-inputs.json").write_text(json.dumps(prepared.public()))
    with pytest.raises(ResumeRefused) as refused:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert refused.value.code == "not_a_lab_campaign"
    assert not (logs / "supervisor.lock").exists()
    assert not list(logs.glob("start-*"))


async def test_malformed_stored_pin_row_is_producing_pair_unknown(pair_sample, tmp_path):
    from evolution_lab.runner import ResumeRefused, resume_native
    prepared = pair_sample.prepare()
    run, logs = tmp_path / "run", tmp_path / "run-lab-logs"
    run.mkdir()
    logs.mkdir()
    (logs / "supervisor.lock").touch()
    stored = json.loads(json.dumps(prepared.public()))
    stored["distributions"][0]["name"] = 1
    (logs / "prepared-inputs.json").write_text(json.dumps(stored))
    with pytest.raises(ResumeRefused) as refused:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert refused.value.code == "producing_pair_unknown"
    assert not list(logs.glob("start-*"))


async def test_resume_result_without_exit_code_has_unknown_core_refusal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from evolution_lab import runner
    async def no_spawn(*args, **kwargs):
        raise OSError("spawn failed")
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", no_spawn)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    prepared = SimpleNamespace(public=lambda: {}, manifest=SimpleNamespace(path=manifest))
    start = tmp_path / "start-2"
    start.mkdir()
    lock_fd = os.open(tmp_path / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with pytest.raises(OSError, match="spawn failed"):
        await runner._supervise(prepared, tmp_path / "run", start, lock_fd, dispatch_guard=lambda: None,
                                stop_event=None, grace_seconds=1.0, kill_on_caller_exit=False,
                                result={"version": "lab-process-result/2", "start": 2, "resume": True})
    saved = json.loads((start / "process-result.json").read_text())
    assert saved["exit_code"] is None and saved["core_refused"] is None


async def test_finished_campaign_and_pair_refusals(pair_sample, tmp_path):
    from evolution_lab.runner import ResumeRefused, resume_native, run_native
    prepared, run = pair_sample.prepare(), tmp_path / "run"
    first = await run_native(prepared, run, dispatch_guard=lambda: None)
    assert first.exit_code == 0, first.stderr.read_text()
    with pytest.raises(ResumeRefused) as finished:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert finished.value.code == "campaign_finished"
    assert finished.value.campaign_outcome["outcome"] == "completed"
    inputs = tmp_path / "run-lab-logs/prepared-inputs.json"
    original = json.loads(inputs.read_text())
    changed = json.loads(json.dumps(original))
    changed["distributions"][0]["sha256"] = "0" * 64
    inputs.write_text(json.dumps(changed))
    with pytest.raises(ResumeRefused) as pair_changed:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert pair_changed.value.code == "producing_pair_changed"
    unknown = json.loads(json.dumps(original))
    unknown["distributions"] = [row for row in unknown["distributions"]
                                if row["name"].lower().replace("_", "-") != "evolution-lab"]
    inputs.write_text(json.dumps(unknown))
    with pytest.raises(ResumeRefused) as pair_unknown:
        await resume_native(prepared, run, dispatch_guard=lambda: None)
    assert pair_unknown.value.code == "producing_pair_unknown"
    assert not list((tmp_path / "run-lab-logs").glob("start-*"))


async def test_live_previous_start_is_refused(pair_sample, tmp_path):
    from evolution_lab.runner import ResumeRefused, resume_native, run_native
    prepared, run = pair_sample.prepare(), tmp_path / "run"
    pair_sample.hold.write_text("60")
    first = asyncio.create_task(run_native(prepared, run, dispatch_guard=lambda: None, grace_seconds=.5))
    try:
        await asyncio.to_thread(wait_for, pair_sample.marker.exists)
        with pytest.raises(ResumeRefused) as alive:
            await resume_native(prepared, run, dispatch_guard=lambda: None)
        assert alive.value.code == "previous_start_alive"
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not list((tmp_path / "run-lab-logs").glob("start-*"))


async def test_orphaned_core_lock_is_refused(pair_sample, tmp_path):
    from evolution.tree.campaign import CAMPAIGN_LOCK
    from evolution_lab.runner import ResumeRefused, resume_native
    run = tmp_path / "run"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    with (run / CAMPAIGN_LOCK).open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ResumeRefused) as alive:
            await resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None)
    assert alive.value.code == "previous_start_alive"
    assert not list((tmp_path / "run-lab-logs").glob("start-*"))


def champion_bindings(run):
    return [row for row in records(run)
            if row.get("event") == "measurement_bound"
            and row.get("payload", {}).get("measurement_id") == "m-r00-champion"]


def champion_rollouts(journal):
    return sum(1 for line in journal.read_bytes().splitlines()
               if json.loads(line).get("event") == "rollout_started")


async def test_kill_after_baseline_then_resume_finishes_without_remeasuring_champion(pair_sample, tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.runner import resume_native
    run = tmp_path / "run"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    if pair_sample.marker.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pair_sample.marker.read_text()), 0)
    assert inspect_campaign(run).to_payload()["complete"] is False
    champion_journal = run / "measurements/m-r00-champion/events.jsonl"
    champion_before = champion_journal.read_bytes()
    rollouts_before = champion_rollouts(champion_journal)
    campaign_before = len(records(run))
    pair_sample.hold.unlink()
    result = await asyncio.wait_for(
        resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    assert result.exit_code == 0, result.stderr.read_text()
    assert inspect_campaign(run).to_payload()["complete"] is True
    assert len(champion_bindings(run)) == 1
    # Core refuses a second champion binding, so the count alone cannot see a re-measurement.
    assert rollouts_before > 0 and champion_rollouts(champion_journal) == rollouts_before
    assert not [row for row in records(run)[campaign_before:]
                if row.get("payload", {}).get("measurement_id") == "m-r00-champion"]
    # baseline_complete cites the whole champion journal (release included), so resume appends nothing there.
    assert champion_journal.read_bytes() == champion_before
    saved = json.loads((tmp_path / "run-lab-logs/start-2/process-result.json").read_text())
    assert saved["version"] == "lab-process-result/2" and saved["start"] == 2 and saved["resume"] is True
    assert saved["core_refused"] is False and saved["caller_lost"] is False
    assert not (tmp_path / "run-lab-logs/process-result.json").exists()
    from evolution_lab.report import read_campaign_report
    report = read_campaign_report(run).data
    assert report["lab_result"]["start"] == 2 and report["lab_result"]["exit_code"] == 0
    assert [row["start"] for row in report["lab_starts"]] == [1, 2]
    assert report["lab_starts"][0]["process_result"] is False and report["lab_starts"][0]["caller_lost"] is True


def killed_during_candidate_measurement(sample, run):
    """Start 1 dies while the held scorer measures the first candidate."""
    caller = spawn_caller(sample, run, "run_native")
    try:
        wait_event(run, "baseline_complete")
        sample.hold.write_text("60")
        wait_for(sample.marker.exists)
        status = kill_caller(caller, run.with_name(run.name + "-lab-logs") / "supervisor-result.json")
    finally:
        if caller.poll() is None:
            caller.kill()
    assert status["caller_lost"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(int(sample.marker.read_text()), 0)
    return status


async def test_kill_during_candidate_measurement_kills_the_tree(pair_sample, tmp_path):
    await asyncio.to_thread(killed_during_candidate_measurement, pair_sample, tmp_path / "run")


async def test_kill_during_candidate_measurement_suspends_then_resume_completes(pair_sample, tmp_path):
    """SIGKILL inside an open candidate measurement (core #123, intended since 0.3.0a26/a27).

    The interrupted tries are rollout-owned indeterminate (``rollout_interrupted``): with the scaffold's
    concurrency 2 they cross ``max_count`` 2, so the first resume finishes the measurement and suspends
    with ``rollout_time_budget_exhausted``; a plain second resume completes the campaign without an
    authorization. No Attempt is sealed twice.
    """
    from evolution.tree import inspect_campaign
    from evolution_lab.runner import resume_native
    raw = json.loads(pair_sample.manifest.read_text())
    apply_legacy_failure_profile(raw)
    pair_sample.manifest.write_text(json.dumps(raw))
    run = tmp_path / "run"
    await asyncio.to_thread(killed_during_candidate_measurement, pair_sample, run)
    pair_sample.hold.unlink()
    await asyncio.wait_for(resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    first = inspect_campaign(run).to_payload()
    assert first["complete"] is False and first["suspended"] is True, first
    assert first["suspensions"][-1]["reason"] == "rollout_time_budget_exhausted"
    await asyncio.wait_for(resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    assert inspect_campaign(run).to_payload()["complete"] is True
    inspect_campaign(run, verify=True).to_payload()
    seals = [row["payload"]["attempt_id"] for row in records(run) if row.get("event") == "attempt_sealed"]
    assert len(seals) == len(set(seals))


async def test_changed_identity_is_core_refusal_without_new_records(pair_sample, tmp_path):
    from evolution_lab.runner import resume_native
    run = tmp_path / "run"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    pair_sample.hold.unlink()
    before = (run / "campaign.jsonl").read_bytes()
    seed_root = pair_sample.prepare().manifest.seed_root
    seed_file = next(path for path in sorted(seed_root.rglob("*")) if path.is_file())
    seed_file.write_text(seed_file.read_text() + "\n")
    result = await asyncio.wait_for(
        resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    assert result.exit_code == 2
    saved = json.loads((tmp_path / "run-lab-logs/start-2/process-result.json").read_text())
    assert saved["core_refused"] is True
    assert (run / "campaign.jsonl").read_bytes() == before


async def test_interrupted_resume_gets_its_own_start_and_the_next_one_finishes(pair_sample, tmp_path):
    """Start 2 is killed mid-run; start 3 gets its own directory and result.

    Start 2 dies inside the held candidate measurement, so start 3 may suspend on the rollout threshold
    (tries interrupted by the executor's death are rollout-owned indeterminate; core #123, intended) instead
    of finishing the campaign: only the log structure is asserted.
    """
    from evolution_lab.runner import resume_native
    run, logs = tmp_path / "run", tmp_path / "run-lab-logs"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    root_supervisor = (logs / "supervisor-result.json").read_bytes()
    pair_sample.marker.unlink(missing_ok=True)
    caller = spawn_caller(pair_sample, run, "resume_native")
    try:
        await asyncio.to_thread(wait_for, pair_sample.marker.exists)
        status = await asyncio.to_thread(kill_caller, caller, logs / "start-2/supervisor-result.json")
    finally:
        if caller.poll() is None:
            caller.kill()
    assert status["caller_lost"] is True
    assert not (logs / "start-2/process-result.json").exists()
    pair_sample.hold.unlink()
    result = await asyncio.wait_for(
        resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    # Exit code is not fixed: start 3 may suspend on the rollout threshold (core #123).
    assert (logs / "supervisor-result.json").read_bytes() == root_supervisor
    saved = json.loads((logs / "start-3/process-result.json").read_text())
    assert saved["version"] == "lab-process-result/2" and saved["start"] == 3 and saved["resume"] is True
    assert saved["exit_code"] == result.exit_code
    assert result.stdout == logs / "start-3/stdout.log"
    from evolution_lab.report import read_campaign_report
    report = read_campaign_report(run).data
    assert [row["start"] for row in report["lab_starts"]] == [1, 2, 3]
    assert report["lab_result"]["start"] == 3
    assert report["lab_starts"][1]["caller_lost"] is True and report["lab_starts"][1]["process_result"] is False


async def test_cooperative_stop_during_resume_is_final(pair_sample, tmp_path):
    from evolution_lab.runner import ResumeRefused, resume_native
    run = tmp_path / "run"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    pair_sample.marker.unlink(missing_ok=True)
    pair_sample.hold.write_text("0.25")
    stop = asyncio.Event()
    async def trip():
        await asyncio.to_thread(wait_for, pair_sample.marker.exists)
        stop.set()
    tripper = asyncio.create_task(trip())
    result = await asyncio.wait_for(
        resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None, stop_event=stop), 120)
    await tripper
    assert result.stop_requested and not result.forced_stop
    with pytest.raises(ResumeRefused) as finished:
        await resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None)
    assert finished.value.code == "campaign_finished"
    assert finished.value.campaign_outcome["outcome"] == "stopped"


# Core #120 (fixed in 0.3.0a27): a forced stop that cancels rollouts used to leave the campaign neither complete nor suspended on 0.3.0a26.
async def test_forced_stop_during_resume_defers_to_core_projection(pair_sample, tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.runner import ResumeRefused, resume_native
    run = tmp_path / "run"
    await asyncio.to_thread(killed_after_baseline, pair_sample, run)
    pair_sample.marker.unlink(missing_ok=True)
    stop = asyncio.Event()
    async def trip():
        await asyncio.to_thread(wait_for, pair_sample.marker.exists)
        stop.set()
    tripper = asyncio.create_task(trip())
    result = await asyncio.wait_for(resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None,
                                                  stop_event=stop, grace_seconds=1), 120)
    await tripper
    assert result.stop_requested and result.forced_stop
    pair_sample.hold.unlink()
    try:
        await asyncio.wait_for(resume_native(pair_sample.prepare(), run, dispatch_guard=lambda: None), 120)
    except ResumeRefused as refused:
        assert refused.code == "campaign_finished"
    else:
        # Any exit code: core ends the campaign itself (family 6 barrier) or, on
        # manifest /8 (family 7), suspends it: interrupted tries of the forced stop
        # are rollout-owned indeterminate and the scaffold's threshold is tight.
        payload = inspect_campaign(run).to_payload()
        assert payload["complete"] is True or payload["suspended"] is True, payload


@pytest.mark.parametrize('content,code', [('family', 'campaign_family_unsupported'),
                                         ('empty', 'campaign_unreadable'),
                                         ('corrupt', 'campaign_unreadable')])
async def test_legacy_resume_reader_refuses_before_dispatch(pair_sample, tmp_path, monkeypatch, content, code):
    import shutil
    from evolution_lab import runner
    prepared = pair_sample.prepare()
    run = tmp_path / 'run'
    run.mkdir()
    logs = run.with_name(run.name + '-lab-logs')
    logs.mkdir()
    (logs / 'prepared-inputs.json').write_text(json.dumps(prepared.public()))
    (logs / 'supervisor.lock').touch()
    if content == 'family':
        shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                        run / 'campaign.jsonl')
    else:
        (run / 'campaign.jsonl').write_text('' if content == 'empty' else 'broken\n')
    before = (run / 'campaign.jsonl').read_bytes()
    async def forbidden(*a, **k):
        pytest.fail('legacy resume dispatched after reader refusal')
    monkeypatch.setattr(runner, '_supervise', forbidden)
    with pytest.raises(runner.ResumeRefused) as raised:
        await runner.resume_native(prepared, run, dispatch_guard=lambda: None)
    assert raised.value.code == code
    assert not list(logs.glob('start-*'))
    assert (run / 'campaign.jsonl').read_bytes() == before
