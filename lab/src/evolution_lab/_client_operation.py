"""One live H1 owner; campaign meaning remains in Evolution's public APIs."""
from __future__ import annotations

import asyncio
from dataclasses import fields
import json
import math
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

from evolution.tree.evolve import check_path_separation

from evolution_lab import _supervisor
from evolution_lab._client_models import (
    ClientError, ExecutionSnapshot, Failure, PreparedCampaign, RunResult, StopReceipt,
    prepared_environment, prepared_inputs,
)
from evolution_lab._client_protocol import (
    ChannelClosed, CommandChannel, ProtocolError, open_event_channel, supervisor_invocation,
)
from evolution_lab._ownership import select_mode
from evolution_lab.budget import BudgetExhausted
from evolution_lab.provenance import InputChanged, IsolatedEnvironmentError, verify_isolated
from evolution_lab.runner import _grace

from evolution_lab.launch_env import with_launch_values


def _core_argv(inputs, run: Path) -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", "evolution.cli.main", "tree", "evolve",
            "--manifest", str(inputs.manifest.path), "--run-dir", str(run))


def _publish_execution(logs: Path, snapshot: ExecutionSnapshot) -> None:
    """Late Task 4 seam: publication must not mutate the execution snapshot."""
    from evolution_lab._client_evidence import publish_execution
    publish_execution(logs, snapshot)


class RunOperation:
    """Inert until ``run`` enters; usable by one event loop and one invocation."""

    def __init__(self, prepared: PreparedCampaign, run_directory, *, dispatch_guard,
                 grace_seconds=None) -> None:
        invalid = ClientError(Failure("invalid_argument", "before_start"))
        if not isinstance(prepared, PreparedCampaign) or not callable(dispatch_guard):
            raise invalid
        try:
            run = Path(run_directory).absolute()
            grace = _grace(prepared_inputs(prepared)) if grace_seconds is None else grace_seconds
            if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or grace <= 0:
                raise invalid
            logs = run.with_name(run.name + "-lab-logs")
        except (TypeError, ValueError) as exc:
            raise invalid from exc
        self._operation_id = str(uuid4())
        self._run_directory, self._logs = run, logs
        self._inputs = prepared_inputs(prepared)
        self._environment = prepared_environment(prepared)
        self._guard = dispatch_guard
        self._grace = float(grace)
        self._invoked = False
        self._result = None
        self._stage = "before_start"
        self._launched = False
        self._exit_code = None
        self._cleanup = True
        self._core_force = False
        self._stop_reason = None
        self._stop_effect = "not_requested"
        self._failures = []
        self._callback = None
        self._emitted = set()
        self._sequence = 0
        self._fatal = None
        self._wake = None
        self._logs_owned = False
        self._verified = False
        self._process = None
        self._commands = None
        self._events = None
        self._reader = None
        self._config = None
        self._queue = None
        self._ready = False
        self._go_attempted = False
        self._stop_sent = False
        self._force_sent = False
        self._stop_time = None
        self._evidence_failed = False

    @property
    def operation_id(self):
        return self._operation_id

    @property
    def run_directory(self):
        return self._run_directory

    @property
    def result(self):
        return self._result

    def _status_fields(self) -> dict:
        result = self._result
        return {
            "operation_id": self.operation_id,
            "state": result.state if result else "active" if self._invoked else "created",
            "stage": result.stage if result else self._stage,
            "launched": result.launched if result else self._launched,
            "exit_code": result.exit_code if result else self._exit_code,
            "stop_requested": result.stop_requested if result else self._stop_reason is not None,
            "cleanup_complete": result.cleanup_complete if result else self._cleanup, "result": result,
        }

    def _request_stop(self, reason):
        if self._stop_reason is None:
            self._stop_reason = reason
            if self._launched is False and not self._go_attempted:
                self._stop_effect = "prevented_start"
            elif self._exit_code is not None:
                self._stop_effect = "too_late"
            else:
                self._stop_effect = "unknown"
        if self._wake is not None:
            self._wake.set()

    def stop(self) -> StopReceipt:
        if self._result is not None:
            disposition = "already_terminal"
        elif self._stop_reason is not None:
            disposition = "already_requested"
        else:
            disposition = "requested"
            self._request_stop("user")
        return StopReceipt(self.operation_id, disposition)

    def _fail(self, code, stage=None):
        self._failures.append(Failure(code, stage or self._stage))

    def _exception(self, exc):
        if isinstance(exc, asyncio.CancelledError):
            self._fatal = self._fatal or exc
            self._request_stop("cancelled")
            return
        if isinstance(exc, (ChannelClosed, ProtocolError)):
            self._lost_evidence()
            return
        if isinstance(exc, InputChanged):
            code = "inputs_changed"
        elif isinstance(exc, IsolatedEnvironmentError):
            code = "environment_invalid"
        elif isinstance(exc, FileExistsError):
            code = "path_conflict"
        elif isinstance(exc, OSError):
            code = "io_error"
        else:
            code = "lab_error"
        self._fail(code)
        if not isinstance(exc, Exception):
            self._fatal = self._fatal or exc
        if self._process is not None and self._exit_code is None:
            self._request_stop("cancelled" if isinstance(exc, asyncio.CancelledError) else "lab_error")

    def _emit(self, kind):
        if kind in self._emitted or self._callback is None:
            return
        self._emitted.add(kind)
        self._sequence += 1
        event = {"schema": "lab-lifecycle/1", "operation_id": self.operation_id,
                 "sequence": self._sequence, "kind": kind, "launched": self._launched,
                 "exit_code": self._exit_code, "stop_requested": self._stop_reason is not None,
                 "cleanup_complete": self._cleanup}
        try:
            self._callback(event)
        except BaseException as exc:
            self._callback = None
            if isinstance(exc, Exception):
                self._fail("callback_failed")
                reason = "callback_failed"
            else:
                self._fatal = self._fatal or exc
                reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "lab_error"
                if reason == "lab_error":
                    self._fail("lab_error")
            if self._exit_code is None and (self._go_attempted or self._launched is False):
                self._request_stop(reason)

    def _notify_stop(self):
        if self._stop_reason is not None:
            self._emit("stop_requested")

    def _check_guard(self):
        if self._stop_reason is not None:
            return
        try:
            self._guard()
        except BudgetExhausted:
            self._request_stop("budget_exhausted")
        except Exception:
            self._fail("dispatch_refused")
            self._request_stop("dispatch_refused")

    async def run(self, *, on_event=None) -> RunResult:
        if self._invoked or (on_event is not None and not callable(on_event)):
            raise ClientError(Failure("invalid_argument", "before_start"))
        self._invoked = True
        self._callback = on_event
        self._wake = asyncio.Event()
        owner = asyncio.create_task(self._execute())
        propagated = None
        while True:
            try:
                await asyncio.shield(owner)
                break
            except asyncio.CancelledError as exc:
                propagated = propagated or exc
                self._request_stop("cancelled")
        if propagated is not None:
            raise propagated
        if self._fatal is not None:
            raise self._fatal
        return self._result

    def _verify_before(self):
        self._inputs.verify()
        verify_isolated(self._inputs.wheels, env=self._environment)
        programs = [command.cwd for command in self._inputs.commands]
        programs.extend(pin.path for pin in self._inputs.files)
        programs.extend(self._inputs.program_paths)
        for path in (self.run_directory, self._logs):
            if path.exists() or path.is_symlink():
                raise FileExistsError()
            try:
                check_path_separation(path, self._inputs.manifest.seed_root, programs)
            except ValueError as exc:
                raise FileExistsError() from exc

    def _create_logs(self):
        self._logs.mkdir(mode=0o700, parents=True, exist_ok=False)
        self._logs_owned = True
        with (self._logs / "prepared-inputs.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(self._inputs.public(), sort_keys=True, ensure_ascii=False) + "\n")
        (self._logs / "prepared-inputs.json").chmod(0o600)

    async def _read_events(self):
        try:
            while True:
                event = await self._events.read()
                if event is None:
                    break
                self._queue.put_nowait(event)
                self._wake.set()
        except Exception:
            self._queue.put_nowait(None)
        finally:
            self._wake.set()

    def _lost_evidence(self):
        if not self._evidence_failed:
            self._evidence_failed = True
            self._fail("lifecycle_unavailable")
        if self._exit_code is None:
            self._request_stop("lab_error")

    def _pump(self):
        if self._queue is None:
            return
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if event is None:
                self._lost_evidence()
                self._events.close()
                continue
            kind, data = event["kind"], event["data"]
            if kind == "ready":
                self._ready = True
            elif kind == "core_started":
                self._launched = True
                if self._cleanup is not True and not self._go_attempted:
                    self._core_force = None
                if self._stop_effect == "prevented_start":
                    self._stop_effect = "unknown"
                self._stage = "stopping" if self._stop_reason else "running"
                self._emit("core_started")
            elif kind == "core_exited":
                self._launched = True
                if self._stop_effect == "prevented_start":
                    self._stop_effect = "unknown"
                self._exit_code = data["exit_code"]
                if (self._stop_reason is not None and self._stop_effect == "unknown"
                        and not self._stop_sent and not self._force_sent):
                    # An unsent intent overtaken by exit has no missing ack.
                    # Once shutdown was attempted, only supervisor evidence
                    # may resolve uncertain delivery/acknowledgement.
                    self._stop_effect = "too_late"
                self._emit("core_exited")
            elif kind in ("spawn_skipped", "spawn_failed"):
                if self._launched is not True:
                    self._launched = False
                if kind == "spawn_failed":
                    self._fail("spawn_failed", "spawning")
                else:
                    self._stop_effect = "prevented_start"
            elif kind == "stop_effect":
                self._stop_effect = data["value"]
            elif kind == "core_force_requested":
                self._core_force = True
            elif kind == "cleanup_complete":
                self._cleanup = True
                if self._core_force is None:
                    self._core_force = False
        self._notify_stop()

    async def _pause(self, timeout=.01):
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _spawn(self):
        ownership = select_mode()
        launch_environment = with_launch_values(self._environment)
        descriptors = []
        try:
            command_read, command_write = os.pipe()
            descriptors.extend((command_read, command_write))
            event_read, event_write = os.pipe()
            descriptors.extend((event_read, event_write))
            argv = supervisor_invocation(Path(_supervisor.__file__), command_read,
                                         event_write, self.operation_id,
                                         _core_argv(self._inputs, self.run_directory),
                                         ownership=ownership)
            with (self._logs / "stdout.log").open("xb") as stdout, (self._logs / "stderr.log").open("xb") as stderr:
                os.fchmod(stdout.fileno(), 0o600)
                os.fchmod(stderr.fileno(), 0o600)
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *argv, cwd=self._inputs.manifest.path.parent, stdout=stdout, stderr=stderr,
                        start_new_session=True, env=launch_environment,
                        pass_fds=(command_read, event_write))
                except OSError:
                    self._fail("spawn_failed", "spawning")
                    return
            self._cleanup = None
            for fd in (command_read, event_write):
                os.close(fd)
                descriptors.remove(fd)
            self._commands = CommandChannel(command_write)
            descriptors.remove(command_write)
            descriptors.remove(event_read)
            self._events = await open_event_channel(event_read, self.operation_id)
            self._queue = asyncio.Queue()
            self._reader = asyncio.create_task(self._read_events())
            self._config = asyncio.create_task(self._commands.send_config(
                self.operation_id, launch_environment, grace_seconds=self._grace))
            deadline = asyncio.get_running_loop().time() + self._grace
            while not self._ready and self._stop_reason is None:
                self._pump()
                if self._config.done():
                    self._config.result()
                if self._ready:
                    break
                if self._reader.done() or asyncio.get_running_loop().time() >= deadline:
                    self._lost_evidence()
                    break
                await self._pause()
            self._pump()
            if self._ready:
                self._check_guard()
            self._notify_stop()
            if self._stop_reason is None and self._ready:
                self._go_attempted = True
                if self._launched is not True:
                    self._launched = None
                    self._core_force = None
                self._commands.send_go(self.operation_id)
        finally:
            for fd in descriptors:
                os.close(fd)

    def _signal_supervisor(self, sig):
        # Only this still-owned asyncio subprocess is addressable, never a core PID.
        if self._process.returncode is None:
            try:
                self._process.send_signal(sig)
            except ProcessLookupError:
                pass

    def _send_shutdown(self, force=False):
        try:
            if self._commands is None:
                raise ChannelClosed()
            if force:
                self._commands.send_force(self.operation_id)
            else:
                self._commands.send_stop(self.operation_id)
        except (ChannelClosed, ProtocolError, OSError):
            self._lost_evidence()
            self._signal_supervisor(signal.SIGUSR1 if force else signal.SIGTERM)

    async def _finish_supervisor(self):
        if self._process is None:
            return
        if self._config is not None:
            if not self._config.done():
                self._config.cancel()
            try:
                await self._config
            except asyncio.CancelledError:
                pass
            except Exception:
                self._lost_evidence()
        if not self._go_attempted:
            if self._commands is not None:
                if self._ready:
                    try:
                        self._commands.send_cancel(self.operation_id)
                    except (ChannelClosed, ProtocolError, OSError):
                        pass
                self._commands.close()
            try:
                await asyncio.wait_for(self._process.wait(), self._grace)
            except asyncio.TimeoutError:
                self._signal_supervisor(signal.SIGKILL)
                await self._process.wait()
        else:
            next_guard = 0
            while self._process.returncode is None:
                self._pump()
                now = asyncio.get_running_loop().time()
                if self._reader is not None and self._reader.done() and self._cleanup is not True:
                    self._lost_evidence()
                if self._stop_reason is None and self._exit_code is None and now >= next_guard:
                    self._check_guard()
                    next_guard = now + .1
                self._notify_stop()
                if self._stop_reason is not None and self._exit_code is None:
                    self._stage = "stopping"
                    if not self._stop_sent:
                        self._stop_sent = True
                        self._stop_time = now
                        self._send_shutdown()
                    elif not self._force_sent and now - self._stop_time >= self._grace:
                        self._force_sent = True
                        self._send_shutdown(force=True)
                await self._pause()
            await self._process.wait()
        if self._reader is not None:
            await self._reader
            self._pump()
        # Reap proves pre-GO cleanup only while positive core facts do not
        # contradict the local no-dispatch inference.
        if not self._go_attempted and self._launched is False:
            self._cleanup = True

    async def _execute(self):
        try:
            self._notify_stop()
            self._check_guard()
            self._notify_stop()
            if self._stop_reason is None:
                await asyncio.to_thread(self._verify_before)
                self._verified = True
                if self._stop_reason is None:
                    self._create_logs()
                    self._stage = "spawning"
                    await self._spawn()
        except BaseException as exc:
            self._exception(exc)
        try:
            await self._finish_supervisor()
        except BaseException as exc:
            self._exception(exc)
            # Guard BaseException still requires the same owner's stop/reap path.
            try:
                await self._finish_supervisor()
            except BaseException as cleanup_exc:
                self._exception(cleanup_exc)
        finally:
            if self._commands is not None:
                self._commands.close()
            if self._events is not None:
                self._events.close()
        if self._verified and self._logs_owned:
            self._stage = "finalizing"
            try:
                await asyncio.to_thread(self._inputs.verify)
            except BaseException as exc:
                self._exception(exc)
        self._notify_stop()
        missing = (self._launched is None or self._cleanup is not True
                   or (self._launched is True and self._exit_code is None)
                   or (self._stop_reason is not None and self._stop_effect == "unknown"))
        if missing and not any(f.code == "lifecycle_unavailable" for f in self._failures):
            self._fail("lifecycle_unavailable")
        self._emit("completed")
        failure = self._failures[0] if self._failures else None
        if self._launched is None or self._cleanup is not True or (self._launched is True and self._exit_code is None):
            state = "unknown"
        elif failure:
            state = "failed"
        elif self._stop_effect in ("prevented_start", "delivered"):
            state = "stopped"
        else:
            state = "exited"
        snapshot = ExecutionSnapshot(
            self.operation_id, state,
            failure.stage if failure else "before_start" if not self._logs_owned and self._stop_reason else "done",
            self._launched, self._exit_code, self._stop_reason is not None, self._stop_reason,
            self._stop_effect, self._core_force, self._cleanup, failure, tuple(self._failures[1:]))
        persistence, persistence_failure = "not_attempted", None
        if self._logs_owned:
            try:
                await asyncio.to_thread(_publish_execution, self._logs, snapshot)
                persistence = "saved"
            except BaseException as exc:
                persistence, persistence_failure = "failed", Failure("io_error", "finalizing")
                if not isinstance(exc, Exception):
                    self._fatal = self._fatal or exc
        self._result = RunResult(**{f.name: getattr(snapshot, f.name) for f in fields(snapshot)},
                                 persistence_state=persistence, persistence_failure=persistence_failure)


def create_run(prepared, run_directory, *, dispatch_guard, grace_seconds=None):
    return RunOperation(prepared, run_directory, dispatch_guard=dispatch_guard,
                        grace_seconds=grace_seconds)


def stop(operation) -> StopReceipt:
    if not isinstance(operation, RunOperation):
        raise ClientError(Failure("invalid_argument", "stopping"))
    return operation.stop()
