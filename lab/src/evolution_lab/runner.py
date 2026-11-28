"""Own a native core CLI process; campaign decisions and journals stay in core.

The required dispatch guard is an admission hook, not a complete paid-run
preflight. Callers must supply budgeted bindings and authorize provider access.
"""
import asyncio
import fcntl
import json
import math
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution_lab.owner.core_raw import campaign_status, CoreRawUnavailable

from evolution.tree import JournalFault
from evolution.tree.campaign import CAMPAIGN_FILE, CAMPAIGN_LOCK, CampaignFamilyUnsupported
from evolution.tree.evolve import check_path_separation
from evolution.tree.roles.config import CommandGroupingSection, ModelGroupingSection

from evolution_lab.budget import BudgetExhausted
from evolution_lab.recipe import PreparedInputs
from evolution_lab.provenance import verify_isolated
from evolution_lab import _supervisor
from evolution_lab._ownership import select_mode
from evolution_lab._manifest_schema import LEGACY_CAMPAIGN_MANIFEST_SCHEMAS, grouping_section, is_legacy_campaign_manifest
from evolution_lab._outcomes import write_result


@dataclass(frozen=True)
class NativeRun:
    exit_code: int
    stop_requested: bool
    forced_stop: bool
    stdout: Path
    stderr: Path


async def _stop(process: asyncio.subprocess.Process, grace_seconds: float) -> bool:
    """Ask our supervisor to stop, then force owned-descendant cleanup."""
    if process.returncode is not None:
        return False
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return False
    except asyncio.TimeoutError:
        try:
            os.kill(process.pid, signal.SIGUSR1)
        except ProcessLookupError:
            pass
        await process.wait()
        return True


def _campaign_stop_grace(manifest) -> float:
    """Resolve the existing H1 campaign shutdown window from a native manifest."""
    workflow = manifest.workflow
    assert workflow is not None
    target = manifest.target.timeouts
    grouping = grouping_section(workflow)
    if isinstance(grouping, CommandGroupingSection):
        grouping_timeout = grouping.command.timeout_seconds
    elif isinstance(grouping, ModelGroupingSection):
        grouping_timeout = float(grouping.timeout_seconds)  # retries and repairs included (manifest /10+)
    else:
        grouping_timeout = 0.0
    assert grouping_timeout is not None
    return (workflow.campaign.editor_timeout_seconds + workflow.campaign.reflection_timeout_seconds
            + workflow.scorer.timeout_seconds
            + workflow.campaign.budget.timeout_seconds + grouping_timeout + target.connect_seconds
            + target.read_seconds + 10.0)


def _grace(prepared: PreparedInputs) -> float:
    return _campaign_stop_grace(prepared.manifest)


def _admit(prepared: PreparedInputs, grace_seconds: float | None) -> float:
    if not is_legacy_campaign_manifest(prepared.manifest):
        raise ValueError("a runnable native campaign manifest (" + " or ".join(sorted(LEGACY_CAMPAIGN_MANIFEST_SCHEMAS)) + ") is required")
    prepared.verify(env=os.environ)
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise ValueError("native process ownership requires Linux or macOS")
    if grace_seconds is None:
        grace_seconds = _grace(prepared)
    if isinstance(grace_seconds, bool) or not math.isfinite(grace_seconds) or grace_seconds <= 0:
        raise ValueError("grace_seconds must be positive and finite")
    return grace_seconds


def _take_lock(path: Path, *, create: bool = True) -> int | None:
    """Hold the Lab supervisor lock, or None when a live holder has it.

    ``create=False`` opens only an existing lock (FileNotFoundError otherwise).
    """
    flags = os.O_RDWR | os.O_CLOEXEC | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


_START = re.compile(r"start-([2-9]|[1-9][0-9]+)")
_PAIR = ("evolution-lab", "evo-skills")


class ResumeRefused(ValueError):
    """A continuation refused before any Lab start directory or process exists."""

    CODES = frozenset({
        "not_a_lab_campaign", "producing_pair_unknown", "producing_pair_changed",
        "previous_start_alive", "campaign_absent", "campaign_family_unsupported",
        "campaign_unreadable", "campaign_finished",
    })

    def __init__(self, code: str, campaign_outcome: Mapping[str, Any] | None = None) -> None:
        if code not in self.CODES:
            raise ValueError("unknown resume refusal")
        super().__init__(code)
        self.code = code
        self.campaign_outcome = campaign_outcome


def _normal(name: str) -> str:
    return name.lower().replace("_", "-")


def _pins(rows) -> set[tuple[str, str, str]] | None:
    """All distribution pins, or None unless exactly one Lab and one core pin exist."""
    if not isinstance(rows, list) or not all(
            isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ("name", "version", "sha256"))
            for row in rows):
        return None
    names = [_normal(row["name"]) for row in rows]
    if any(names.count(name) != 1 for name in _PAIR):
        return None
    return {(_normal(row["name"]), row["version"], row["sha256"]) for row in rows}


def _probe_core_lock(path: Path) -> bool:
    """True when a live writer holds core's campaign lease."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing releases a lease this probe took
    return False


def _campaign_state(run: Path) -> None:
    if not (run / CAMPAIGN_FILE).is_file():
        raise ResumeRefused("campaign_absent")
    try:
        payload = campaign_status(run).to_payload()
    except CampaignFamilyUnsupported:
        raise ResumeRefused("campaign_family_unsupported") from None
    except (JournalFault, ValueError, OSError, CoreRawUnavailable):
        raise ResumeRefused("campaign_unreadable") from None
    if payload["complete"] is True:
        raise ResumeRefused("campaign_finished", payload["campaign_outcome"])


async def _supervise(
    prepared: PreparedInputs,
    run: Path,
    start: Path,
    lock_fd: int,
    *,
    dispatch_guard: Callable[[], None],
    stop_event: asyncio.Event | None,
    grace_seconds: float,
    kill_on_caller_exit: bool,
    result: dict[str, Any],
) -> NativeRun:
    provenance = start / "prepared-inputs.json"
    try:
        ownership = select_mode()
        with provenance.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(prepared.public(), ensure_ascii=False, sort_keys=True) + "\n")
        provenance.chmod(0o600)
    except BaseException:
        os.close(lock_fd)  # no supervisor exists yet, and no process result is written
        raise
    stdout, stderr = start / "stdout.log", start / "stderr.log"
    # The current interpreter retains its venv. -I excludes checkout/cwd imports.
    argv = (sys.executable, "-I", "-m", "evolution.cli.main", "tree", "evolve",
            "--manifest", str(prepared.manifest.path), "--run-dir", str(run))
    caller = ("--caller-pid", str(os.getpid())) if kill_on_caller_exit else ()
    supervision = start / "supervisor-result.json"
    stopped = forced = False
    reason = None
    process = None
    lock_open = True
    try:
        with stdout.open("xb") as out, stderr.open("xb") as err:
            stdout.chmod(0o600)
            stderr.chmod(0o600)
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", _supervisor.__file__, "--ownership", ownership, *caller, str(supervision), *argv,
                cwd=prepared.manifest.path.parent, stdout=out, stderr=err,
                start_new_session=True, pass_fds=(lock_fd,),
            ))
            try:
                process = await asyncio.shield(spawn)
            except BaseException:
                # Cancellation during creation must not lose ownership of a
                # supervisor that may already have started its core child.
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        pass
                process = spawn.result()
                raise
            finally:
                os.close(lock_fd)  # the supervisor holds the same lock from here on
                lock_open = False
            while process.returncode is None:
                stopped = stop_event is not None and stop_event.is_set()
                if stopped:
                    reason = "stop_requested"
                try:
                    dispatch_guard()
                except BudgetExhausted:
                    stopped = True
                    reason = "budget_exhausted"
                if stopped:
                    forced = await _stop(process, grace_seconds)
                    break
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
        prepared.verify(env=os.environ)
    except BaseException as exc:
        stopped = True
        reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "exception"
        if process is not None:
            cleanup = asyncio.create_task(_stop(process, grace_seconds))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
            forced = cleanup.result()
        raise
    finally:
        if lock_open:
            os.close(lock_fd)
        supervisor = json.loads(supervision.read_text()) if supervision.is_file() else {}
        forced = forced or supervisor.get("forced_stop", False)
        exit_code = supervisor.get("core_exit_code", process.returncode if process else None)
        payload = {**result, "exit_code": exit_code,
                   "stop_requested": stopped, "forced_stop": forced, "reason": reason,
                   "cleanup_complete": supervisor.get("cleanup_complete", process is None),
                   "grace_seconds": grace_seconds}
        if result["version"] == "lab-process-result/2":
            payload["core_refused"] = None if exit_code is None else exit_code == 2
            payload["caller_lost"] = supervisor.get("caller_lost", False)
        write_result(start / "process-result.json", payload)
    assert exit_code is not None
    return NativeRun(exit_code, stopped, forced, stdout, stderr)


async def run_native(
    prepared: PreparedInputs,
    run_directory: Path,
    *,
    dispatch_guard: Callable[[], None],
    stop_event: asyncio.Event | None = None,
    grace_seconds: float | None = None,
    kill_on_caller_exit: bool = False,
) -> NativeRun:
    """Start a fresh native campaign; never resume or overwrite an existing run.

    The guard runs before writes/dispatch and every 100 ms while running. Budget
    refusal requests shutdown; hard request admission belongs in the gateway.
    A killed process need not have a terminal journal: inspect core's projection
    to determine its outcome, and continue it only through ``resume_native``.
    ``kill_on_caller_exit`` makes the supervisor SIGKILL the tree when this
    process dies, leaving an interrupted rather than stopped campaign.
    Logs can contain private diagnostics.
    """
    dispatch_guard()
    grace_seconds = _admit(prepared, grace_seconds)
    run = Path(run_directory).absolute()
    logs = run.with_name(run.name + "-lab-logs")
    for path in (run, logs):
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
    if not any(pin.name.lower().replace('_', '-') == "evo-skills" for pin in prepared.wheels):
        raise ValueError("an exact verified core wheel is required")
    await asyncio.to_thread(verify_isolated, prepared.wheels)
    programs = [command.cwd for command in prepared.commands]
    programs.extend(pin.path for pin in prepared.files)
    programs.extend(prepared.program_paths)
    for path in (run, logs):
        check_path_separation(path, prepared.manifest.seed_root, programs)
    if stop_event is not None and stop_event.is_set():
        raise ValueError("stop already requested")
    logs.mkdir(mode=0o700, parents=True, exist_ok=False)
    lock_fd = _take_lock(logs / "supervisor.lock")
    assert lock_fd is not None  # a directory this call just created has no other holder
    return await _supervise(prepared, run, logs, lock_fd, dispatch_guard=dispatch_guard,
                            stop_event=stop_event, grace_seconds=grace_seconds,
                            kill_on_caller_exit=kill_on_caller_exit,
                            result={"version": "lab-process-result/1"})


async def resume_native(
    prepared: PreparedInputs,
    run_directory: Path,
    *,
    dispatch_guard: Callable[[], None],
    stop_event: asyncio.Event | None = None,
    grace_seconds: float | None = None,
    kill_on_caller_exit: bool = False,
) -> NativeRun:
    """Continue an interrupted native campaign in its existing run directory.

    Refusals before any start directory or process: ``ResumeRefused`` with a
    closed ``code``. Whether this is the same campaign (corpus, seed, policy,
    scorer, roles, adaptation) is core's decision at adoption: its refusal is
    exit 2, recorded as ``core_refused``. A cooperative stop or budget refusal
    finishes the campaign for good; after a forced stop core's projection
    decides whether a later continuation is possible. Episodes cut off by the
    death of a process are rerun by core in campaign family 6 (each spends a
    sample retry); an exhausted sample is indeterminate.
    """
    dispatch_guard()
    grace_seconds = _admit(prepared, grace_seconds)
    current = _pins([pin.public() for pin in prepared.wheels])
    if current is None or len(current) != len(prepared.wheels):
        raise ValueError("exact verified Lab and core wheels are required")
    run = Path(run_directory).absolute()
    logs = run.with_name(run.name + "-lab-logs")
    first = logs / "prepared-inputs.json"
    if run.is_symlink() or not run.is_dir() or not first.is_file():
        raise ResumeRefused("not_a_lab_campaign")
    try:
        rows = json.loads(first.read_text())["distributions"]
        started = _pins(rows)
        if started is not None and len(started) != len(rows):
            started = None  # a duplicated pin row is not an exact pair
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        started = None
    if started is None:
        raise ResumeRefused("producing_pair_unknown")
    if started != current:
        raise ResumeRefused("producing_pair_changed")
    try:
        # Only run_native creates this lock; H1 client and Owner logs have none.
        lock_fd = _take_lock(logs / "supervisor.lock", create=False)
    except FileNotFoundError:
        raise ResumeRefused("not_a_lab_campaign") from None
    if lock_fd is None:
        raise ResumeRefused("previous_start_alive")
    try:
        if _probe_core_lock(run / CAMPAIGN_LOCK):
            raise ResumeRefused("previous_start_alive")
        _campaign_state(run)
        await asyncio.to_thread(verify_isolated, prepared.wheels)
        programs = [command.cwd for command in prepared.commands]
        programs.extend(pin.path for pin in prepared.files)
        programs.extend(prepared.program_paths)
        for path in (run, logs):
            check_path_separation(path, prepared.manifest.seed_root, programs)
        if stop_event is not None and stop_event.is_set():
            raise ValueError("stop already requested")
        taken = [int(match[1]) for entry in logs.iterdir()
                 if (match := _START.fullmatch(entry.name)) and entry.is_dir() and not entry.is_symlink()]
        number = max(taken, default=1) + 1
        start = logs / f"start-{number}"
        start.mkdir(mode=0o700, exist_ok=False)
    except BaseException:
        os.close(lock_fd)
        raise
    return await _supervise(prepared, run, start, lock_fd, dispatch_guard=dispatch_guard,
                            stop_event=stop_event, grace_seconds=grace_seconds,
                            kill_on_caller_exit=kill_on_caller_exit,
                            result={"version": "lab-process-result/2", "start": number, "resume": True})
