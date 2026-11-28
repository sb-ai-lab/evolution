"""Own one Lab CLI through a Linux subreaper or a group/tree/marker union.

This file also owns the private H1 wire format and runs by absolute path under
Python ``-I``. Only the macOS ownership path loads psutil, through _ownership.
"""

import ctypes
import json
import math
import os
from pathlib import Path
import signal
import secrets
import struct
import subprocess
import sys
import time

from evolution_lab._ownership import (
    GROUP_TREE_MARKER, SUBREAPER, SWEEP_WAIT_SECONDS,
    GroupTreeMarker, child_environment,
)
from evolution_lab.durability import sync_file


COMMAND_SCHEMA = "lab-supervisor-command/1"
EVENT_SCHEMA = "lab-supervisor-event/1"
H1_MODE = "--h1"
_HEADER = struct.Struct("!I")
_COMMAND_KINDS = frozenset({"config", "GO", "CANCEL", "STOP", "FORCE"})
_EVENT_DATA = {
    "ready": frozenset(),
    "spawn_skipped": frozenset({"stop_effect"}),
    "spawn_failed": frozenset(),
    "core_started": frozenset(),
    "stop_effect": frozenset({"value"}),
    "core_force_requested": frozenset({"value"}),
    "core_exited": frozenset({"exit_code"}),
    "cleanup_complete": frozenset({"value"}),
}


class ProtocolError(Exception):
    """The private local channel did not contain a complete valid record."""


class ChannelClosed(Exception):
    """A framed write could not be delivered in full."""


def _message(schema, operation_id, kind, data=None):
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    return {
        "schema": schema,
        "operation_id": operation_id,
        "kind": kind,
        "data": {} if data is None else data,
    }


def make_config(operation_id, environment, grace_seconds):
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("environment must be a string mapping")
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, (int, float))
        or not math.isfinite(grace_seconds)
        or grace_seconds <= 0
    ):
        raise ValueError("grace_seconds must be positive and finite")
    return _message(
        COMMAND_SCHEMA,
        operation_id,
        "config",
        {"env": dict(environment), "grace_seconds": float(grace_seconds)},
    )


def make_command(operation_id, kind):
    if kind not in _COMMAND_KINDS or kind == "config":
        raise ValueError("unknown supervisor command")
    return _message(COMMAND_SCHEMA, operation_id, kind)


def make_event(operation_id, kind, data=None):
    message = _message(EVENT_SCHEMA, operation_id, kind, data)
    validate_event(message)
    return message


def encode_frame(message):
    try:
        payload = json.dumps(
            message, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("record is not JSON encodable") from exc
    if not payload or len(payload) > 0xFFFFFFFF:
        raise ProtocolError("invalid record length")
    return _HEADER.pack(len(payload)) + payload


def decode_payload(payload):
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON record") from exc
    if not isinstance(message, dict):
        raise ProtocolError("record must be an object")
    return message


def validate_event(message):
    if set(message) != {"schema", "operation_id", "kind", "data"}:
        raise ProtocolError("invalid event shape")
    if message["schema"] != EVENT_SCHEMA:
        raise ProtocolError("invalid event schema")
    if not isinstance(message["operation_id"], str) or not message["operation_id"]:
        raise ProtocolError("invalid event operation_id")
    kind = message["kind"]
    data = message["data"]
    if (
        not isinstance(kind, str)
        or kind not in _EVENT_DATA
        or not isinstance(data, dict)
    ):
        raise ProtocolError("invalid event kind")
    if set(data) != _EVENT_DATA[kind]:
        raise ProtocolError("invalid event data")
    if kind == "spawn_skipped" and data["stop_effect"] != "prevented_start":
        raise ProtocolError("invalid spawn_skipped data")
    if kind == "stop_effect":
        value = data["value"]
        if not isinstance(value, str) or value not in {"delivered", "too_late"}:
            raise ProtocolError("invalid stop effect")
    if kind in {"core_force_requested", "cleanup_complete"} and data["value"] is not True:
        raise ProtocolError("invalid boolean fact")
    if kind == "core_exited" and (
        isinstance(data["exit_code"], bool) or not isinstance(data["exit_code"], int)
    ):
        raise ProtocolError("invalid core exit code")
    return message


def _read_exact(fd, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ProtocolError("incomplete framed record")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(fd):
    header = _read_exact(fd, _HEADER.size)
    if header is None:
        return None
    length = _HEADER.unpack(header)[0]
    if length == 0:
        raise ProtocolError("empty framed record")
    payload = _read_exact(fd, length)
    if payload is None:
        raise ProtocolError("incomplete framed record")
    return decode_payload(payload)


class IncrementalFrameReader:
    """Drain complete frames from a nonblocking post-GO command pipe."""

    _MAX_READS_PER_TURN = 16
    _MAX_BYTES_PER_TURN = 65536

    def __init__(self, max_payload):
        self._buffer = bytearray()
        self._expected = None
        self._max_payload = max_payload

    @property
    def incomplete(self):
        return bool(self._buffer) or self._expected is not None

    def _parse_available(self, messages):
        while True:
            if self._expected is None:
                if len(self._buffer) < _HEADER.size:
                    return
                self._expected = _HEADER.unpack(self._buffer[:_HEADER.size])[0]
                del self._buffer[:_HEADER.size]
                if self._expected == 0 or self._expected > self._max_payload:
                    raise ProtocolError("invalid atomic command length")
            if len(self._buffer) < self._expected:
                return
            payload = bytes(self._buffer[:self._expected])
            del self._buffer[:self._expected]
            self._expected = None
            messages.append(decode_payload(payload))

    def read_available(self, fd):
        messages = []
        eof = False
        remaining = self._MAX_BYTES_PER_TURN
        self._parse_available(messages)
        for _ in range(self._MAX_READS_PER_TURN):
            try:
                chunk = os.read(fd, min(65536, remaining))
            except BlockingIOError:
                break
            if not chunk:
                eof = True
                break
            self._buffer.extend(chunk)
            remaining -= len(chunk)
            # Validate each newly available header before asking the pipe for
            # more bytes; a perpetually-readable writer cannot defer parsing.
            self._parse_available(messages)
            if remaining <= 0:
                break
        if eof and self.incomplete:
            raise ProtocolError("incomplete command frame")
        return messages, eof


def validate_command(message, operation_id, *, expect_config=False):
    if set(message) != {"schema", "operation_id", "kind", "data"}:
        raise ProtocolError("invalid command shape")
    if message["schema"] != COMMAND_SCHEMA or message["operation_id"] != operation_id:
        raise ProtocolError("command identity mismatch")
    kind = message["kind"]
    data = message["data"]
    if (
        not isinstance(kind, str)
        or kind not in _COMMAND_KINDS
        or not isinstance(data, dict)
    ):
        raise ProtocolError("invalid command kind")
    if expect_config:
        if kind != "config" or set(data) != {"env", "grace_seconds"}:
            raise ProtocolError("configuration must be first")
        environment = data["env"]
        grace = data["grace_seconds"]
        if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ProtocolError("invalid configuration environment")
        if (
            isinstance(grace, bool)
            or not isinstance(grace, (int, float))
            or not math.isfinite(grace)
            or grace <= 0
        ):
            raise ProtocolError("invalid configuration grace")
    elif kind == "config" or data:
        raise ProtocolError("invalid command data")
    return message


def write_atomic_frame(fd, message):
    frame = encode_frame(message)
    if len(frame) > os.fpathconf(fd, "PC_PIPE_BUF"):
        raise ProtocolError("atomic record exceeds PIPE_BUF")
    try:
        written = os.write(fd, frame)
    except OSError as exc:
        raise ChannelClosed("channel write failed") from exc
    if written != len(frame):
        raise ChannelClosed("partial atomic channel write")


def _set_handlers(state):
    def request_stop(*_):
        state["stop"] = True

    def request_force(*_):
        state["stop"] = True
        state["force"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGUSR1, request_force)


def _establish_child_ownership():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish child ownership")
    children_file = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    children_file.read_text()
    return children_file


def _kill_children(children_file):
    killed = False
    for child in children_file.read_text().split():
        try:
            os.kill(int(child), signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            pass
    return killed


def _reap(core_pid):
    core_exit = None
    no_children = False
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            no_children = True
            break
        if pid == 0:
            break
        if pid == core_pid:
            core_exit = os.waitstatus_to_exitcode(status)
    return core_exit, no_children


def _scope_token(ownership):
    if ownership == GROUP_TREE_MARKER:
        import psutil  # noqa: F401 — fail before ready/spawn if unavailable
        return secrets.token_hex(32)
    return None


def _group_step(core, mechanism, force=False):
    """Observe without reaping until the entire union is empty."""
    if core.returncode is not None:
        return core.returncode, True, False
    exited = mechanism.leader_exited()
    if not exited:
        mechanism.poll()
    killed = mechanism.kill_union() if force or exited else False
    if mechanism.scope_empty():
        code = mechanism.reap_leader()
        if code is not None:
            core.returncode = code
            return code, True, killed
    return None, False, killed


def _cleanup_without_events(core, children_file, grace_seconds, *, ownership, mechanism):
    if ownership == GROUP_TREE_MARKER:
        # A known returncode means final reap and cleanup already happened.
        # Never reconstruct a capability from that PID or touch its old pgid.
        if core.returncode is not None:
            return core.returncode
        mechanism.sweep()
        if not mechanism.leader_exited():
            try:
                os.kill(core.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_seconds
        while True:
            observed, empty, _ = _group_step(
                core, mechanism, force=time.monotonic() >= deadline
            )
            if empty:
                return observed
            time.sleep(SWEEP_WAIT_SECONDS if mechanism.leader_exited() else .01)

    # A reaped PID is no longer an ownership capability and may already have
    # been reused. Once returncode is known, address descendants only through
    # the subreaper's current direct-child set.
    if core.returncode is None:
        try:
            os.kill(core.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    core_exit = None
    while time.monotonic() < deadline:
        observed, no_children = _reap(core.pid)
        if observed is not None:
            core_exit = observed
            core.returncode = observed
        if no_children:
            return core_exit
        time.sleep(0.01)
    while True:
        _kill_children(children_file)
        observed, no_children = _reap(core.pid)
        if observed is not None:
            core_exit = observed
            core.returncode = observed
        if no_children:
            return core_exit
        time.sleep(0.01)


def _emit_skipped(event_fd, operation_id):
    write_atomic_frame(
        event_fd,
        make_event(operation_id, "spawn_skipped", {"stop_effect": "prevented_start"}),
    )
    write_atomic_frame(
        event_fd, make_event(operation_id, "cleanup_complete", {"value": True})
    )


def _h1_main(argv, ownership):
    if len(argv) < 5 or argv[3] != "--":
        return 2
    try:
        command_fd = int(argv[0])
        event_fd = int(argv[1])
    except ValueError:
        return 2
    operation_id = argv[2]
    core_argv = argv[4:]
    if command_fd <= 2 or event_fd <= 2 or command_fd == event_fd or not operation_id:
        return 2
    state = {"stop": False, "force": False}
    core = None
    children_file = None
    mechanism = None
    grace_seconds = 1.0
    try:
        _set_handlers(state)
        children_file = _establish_child_ownership() if ownership == SUBREAPER else None
        token = _scope_token(ownership)
        config = read_frame(command_fd)
        if config is None:
            return 2
        validate_command(config, operation_id, expect_config=True)
        environment = child_environment(config["data"]["env"], ownership, token)
        grace_seconds = float(config["data"]["grace_seconds"])
        write_atomic_frame(event_fd, make_event(operation_id, "ready"))

        command = read_frame(command_fd)
        if command is None:
            _emit_skipped(event_fd, operation_id)
            return 0
        validate_command(command, operation_id)
        if command["kind"] != "GO":
            if command["kind"] not in {"CANCEL", "STOP", "FORCE"}:
                raise ProtocolError("GO or cancellation required")
            _emit_skipped(event_fd, operation_id)
            return 0

        os.set_blocking(command_fd, False)
        command_reader = IncrementalFrameReader(
            os.fpathconf(command_fd, "PC_PIPE_BUF") - _HEADER.size
        )
        pending_commands, command_eof = command_reader.read_available(command_fd)
        if command_reader.incomplete:
            raise ProtocolError("partial atomic command before spawn")
        if command_eof:
            _emit_skipped(event_fd, operation_id)
            return 0
        for pending in pending_commands:
            validate_command(pending, operation_id)
            if pending["kind"] in {"CANCEL", "STOP", "FORCE"}:
                _emit_skipped(event_fd, operation_id)
                return 0
            raise ProtocolError("duplicate or invalid pre-spawn command")
        if state["stop"]:
            _emit_skipped(event_fd, operation_id)
            return 0

        try:
            core = subprocess.Popen(
                core_argv,
                start_new_session=True,
                env=environment,
                close_fds=True,
            )
        except (OSError, ValueError):
            write_atomic_frame(event_fd, make_event(operation_id, "spawn_failed"))
            write_atomic_frame(
                event_fd, make_event(operation_id, "cleanup_complete", {"value": True})
            )
            return 1
        if ownership == GROUP_TREE_MARKER:
            mechanism = GroupTreeMarker(core.pid, token)
        write_atomic_frame(event_fd, make_event(operation_id, "core_started"))

        core_exit = None
        stop_effect_sent = False
        force_fact_sent = False
        protocol_failure = False
        while True:
            incoming_commands = []
            command_eof = False
            if command_reader is not None:
                try:
                    incoming_commands, command_eof = command_reader.read_available(
                        command_fd
                    )
                except ProtocolError:
                    command_reader = None
                    state["stop"] = state["force"] = True
                    protocol_failure = True
            if command_eof:
                state["stop"] = True
            for incoming in incoming_commands:
                try:
                    validate_command(incoming, operation_id)
                except ProtocolError:
                    command_reader = None
                    state["stop"] = state["force"] = True
                    protocol_failure = True
                else:
                    kind = incoming["kind"]
                    if kind in {"STOP", "CANCEL"}:
                        state["stop"] = True
                    elif kind == "FORCE":
                        state["stop"] = state["force"] = True
                    else:
                        command_reader = None
                        state["stop"] = state["force"] = True
                        protocol_failure = True

            if (state["force"] and not force_fact_sent and core_exit is None
                    and (mechanism is None or not mechanism.leader_exited())):
                write_atomic_frame(
                    event_fd,
                    make_event(operation_id, "core_force_requested", {"value": True}),
                )
                force_fact_sent = True
            if state["stop"] and not stop_effect_sent:
                if mechanism is not None:
                    mechanism.sweep()
                if core_exit is not None or (mechanism is not None and mechanism.leader_exited()):
                    effect = "too_late"
                else:
                    try:
                        os.kill(core.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        effect = "too_late"
                    else:
                        effect = "delivered"
                write_atomic_frame(
                    event_fd,
                    make_event(operation_id, "stop_effect", {"value": effect}),
                )
                stop_effect_sent = True
            if ownership == GROUP_TREE_MARKER:
                observed, no_children, _ = _group_step(core, mechanism, state["force"])
            else:
                if state["force"] or core_exit is not None:
                    _kill_children(children_file)
                observed, no_children = _reap(core.pid)
            if observed is not None and core_exit is None:
                core_exit = observed
                core.returncode = observed
                if state["stop"] and not stop_effect_sent:
                    write_atomic_frame(
                        event_fd,
                        make_event(operation_id, "stop_effect", {"value": "too_late"}),
                    )
                    stop_effect_sent = True
                write_atomic_frame(
                    event_fd,
                    make_event(operation_id, "core_exited", {"exit_code": core_exit}),
                )
            if no_children:
                if command_reader is not None and command_reader.incomplete:
                    protocol_failure = True
                write_atomic_frame(
                    event_fd, make_event(operation_id, "cleanup_complete", {"value": True})
                )
                status = core_exit if core_exit is not None and 0 <= core_exit <= 255 else 1
                return 2 if protocol_failure else status
            time.sleep(SWEEP_WAIT_SECONDS if mechanism is not None and mechanism.leader_exited() else .01)
    except (ProtocolError, ChannelClosed, OSError, ImportError):
        if core is not None and (children_file is not None or mechanism is not None):
            _cleanup_without_events(core, children_file, grace_seconds,
                                    ownership=ownership, mechanism=mechanism)
        return 2
    except BaseException:
        # Match the group helper's failure cleanup without broadening Linux's
        # existing exception admission. A mechanism failure cannot abandon Core.
        if ownership != GROUP_TREE_MARKER:
            raise
        if core is not None and mechanism is not None:
            _cleanup_without_events(core, children_file, grace_seconds,
                                    ownership=ownership, mechanism=mechanism)
        return 2
    finally:
        for fd in (command_fd, event_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _legacy_main(argv, ownership, caller_pid=None):
    status_path = Path(argv[0])
    stop = forced = False

    def request_stop(*_):
        nonlocal stop
        stop = True

    def request_force(*_):
        nonlocal stop, forced
        stop = forced = True

    def caller_lost():
        return caller_pid is not None and os.getppid() != caller_pid

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGUSR1, request_force)
    children_file = _establish_child_ownership() if ownership == SUBREAPER else None
    token = _scope_token(ownership)
    lost = caller_lost()
    core = None if lost else subprocess.Popen(
        argv[1:], start_new_session=True,
        env=child_environment(os.environ, ownership, token),
    )
    mechanism = GroupTreeMarker(core.pid, token) if core is not None and ownership == GROUP_TREE_MARKER else None
    exit_code = None
    sent_stop = False
    cleanup_forced = False
    try:
        # A caller lost after the check above is caught right here, before the
        # first wait; the contract does not exclude work inside that window.
        while core is not None:
            lost = lost or caller_lost()
            if stop and not sent_stop and exit_code is None and not lost:
                if mechanism is not None:
                    mechanism.sweep()
                if mechanism is None or not mechanism.leader_exited():
                    try:
                        os.kill(core.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                sent_stop = True
            if mechanism is not None:
                observed, no_children, killed = _group_step(core, mechanism, lost or forced)
                cleanup_forced = killed or cleanup_forced
            else:
                if lost or forced or exit_code is not None:
                    cleanup_forced = _kill_children(children_file) or cleanup_forced
                observed, no_children = _reap(core.pid)
            if observed is not None:
                exit_code = observed
                core.returncode = observed
            if no_children:
                break
            time.sleep(SWEEP_WAIT_SECONDS if mechanism is not None and mechanism.leader_exited() else .01)
    finally:
        while core is not None:
            if mechanism is not None:
                observed, no_children, killed = _group_step(core, mechanism, True)
                cleanup_forced = killed or cleanup_forced
            else:
                cleanup_forced = _kill_children(children_file) or cleanup_forced
                observed, no_children = _reap(core.pid)
            if observed is not None:
                exit_code = observed
                core.returncode = observed
            if no_children:
                break
            time.sleep(SWEEP_WAIT_SECONDS if mechanism is not None and mechanism.leader_exited() else .01)
        # Readers see either no status or a complete one: write a private
        # sibling, fsync it, then rename. An existing status is never replaced.
        partial = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(
                    {
                        "core_exit_code": exit_code,
                        "stop_requested": stop,
                        "forced_stop": forced or cleanup_forced,
                        "cleanup_complete": True,
                        "caller_lost": lost,
                    },
                    stream,
                )
                stream.flush()
                sync_file(stream.fileno())
            if os.path.lexists(status_path):
                raise FileExistsError(status_path)
            os.replace(partial, status_path)
        except BaseException:
            try:
                os.unlink(partial)
            except OSError:
                pass
            raise
    return exit_code if exit_code is not None and 0 <= exit_code <= 255 else 1


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--ownership" or args[1] not in {SUBREAPER, GROUP_TREE_MARKER}:
        return 2
    ownership, args = args[1], args[2:]
    if args[0] == H1_MODE:
        return _h1_main(args[1:], ownership)
    caller_pid = None
    if args[:1] == ["--caller-pid"]:
        try:
            caller_pid = int(args[1])
        except (IndexError, ValueError):
            return 2
        if caller_pid <= 0:
            return 2
        args = args[2:]
    if len(args) < 2:
        return 2
    return _legacy_main(args, ownership, caller_pid)


if __name__ == "__main__":
    sys.exit(main())
