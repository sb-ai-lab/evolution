"""Installed S2 process owner; one scope, no core or SQLite imports.

Linux follows H1 _supervisor: reap waitpid(-1), then address only /proc's
current direct children. The other mode uses the shared GroupTreeMarker.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import secrets
import stat
import subprocess
import sys
import time

from .._ownership import (GROUP_TREE_MARKER, SUBREAPER, SWEEP_WAIT_SECONDS,
    GroupTreeMarker, child_environment)
from .protocol import (FrameReader, MAX_ACTION_BYTES, ProtocolError,
    encode_frame, make_event, validate_command)
from .receipts import (ReceiptError, publish_json_at, read_json_at,
    validate_cleanup_receipt, validate_cleanup_request, validate_id)


class ScopeHelper:
    def __init__(self, command_fd, event_fd, directory_fd, ready_fd, scope_id, generation, mode):
        if mode not in {SUBREAPER, GROUP_TREE_MARKER}:
            raise ValueError('unknown ownership mode')
        self.mode = mode
        self.mechanism = None
        self._ps = None
        self._leader_exit_seen = False
        self._token = secrets.token_hex(16) if mode == GROUP_TREE_MARKER else None
        self.command_fd, self.event_fd = command_fd, event_fd
        self.event_pipe_buf = os.fpathconf(event_fd, 'PC_PIPE_BUF')
        self.directory_fd, self.ready_fd = directory_fd, ready_fd
        self.scope_id, self.generation = validate_id(scope_id), validate_id(generation)
        self.config = None
        self.child = None
        self.reader = FrameReader()
        self.go_received = False
        self.spawn_closed = False
        self.forced = False
        self.stop_deadline = None
        self.config_writer = None
        self.config_bytes = memoryview(b'')
        self.children_path = (Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children')
                              if mode == SUBREAPER else None)
        self.signal_stop = False
        self.signal_force = False
        self.command_open = True
        self.last_request_poll = -1.0

    def event(self, kind, data=None):
        if self.event_fd is None:
            return
        frame = encode_frame(make_event(self.scope_id, self.generation, kind, data))
        try:
            # An oversized frame or full pipe loses the channel, never delaying
            # TERM, escalation, reaping, or durable publication.
            if len(frame) > self.event_pipe_buf:
                raise OSError('event exceeds PIPE_BUF')
            if os.write(self.event_fd, frame) != len(frame):
                raise OSError('partial event')
        except OSError:
            os.close(self.event_fd)
            self.event_fd = None

    def stop(self):
        self.spawn_closed = True
        if self.stop_deadline is not None:
            return
        grace = self.config['stop_grace_seconds'] if self.config else 1.0
        self.stop_deadline = time.monotonic() + grace
        effect = 'prevented_start'
        if self.child is not None:
            effect = 'too_late'
            running = self.child.returncode is None
            if self.mode == GROUP_TREE_MARKER:
                self.mechanism.sweep()
                running = not self.mechanism.leader_exited()
            if running:
                try:
                    # Unreaped direct-child PID remains ours, including zombie.
                    os.kill(self.child.pid, signal.SIGTERM)
                    effect = 'delivered'
                except ProcessLookupError:
                    pass
        self.event('stop_effect', {'value': effect})

    def force(self):
        self.spawn_closed = True
        if not self.forced:
            self.forced = True  # sticky before attempting any SIGKILL/event
            self.event('force_started')

    def kill_current_children(self):
        if self.mode == GROUP_TREE_MARKER:
            if self.mechanism is not None and self.mechanism.kill_union():
                self.force()
            return
        for value in self.children_path.read_text().split():
            self.force()
            try:
                os.kill(int(value), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def reap(self):
        if self.mode == GROUP_TREE_MARKER:
            if self.child is None or self.child.returncode is not None:
                return True
            if not self.mechanism.scope_empty():
                return False
            code = self.mechanism.reap_leader()
            if code is None:
                return False
            self.child.returncode = code
            self.event('child_exited', {'exit_code': code})
            return True
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid == 0:
                return False
            if self.child is not None and pid == self.child.pid:
                self.child.returncode = os.waitstatus_to_exitcode(status)
                self.event('child_exited', {'exit_code': self.child.returncode})

    def poll_request(self):
        now = time.monotonic()
        if now - self.last_request_poll < .1:
            return
        self.last_request_poll = now
        try:
            value = read_json_at(self.directory_fd, 'cleanup.request')
            validate_cleanup_request(value, self.scope_id, self.generation)
        except (OSError, ReceiptError):
            return
        self.stop()

    def commands(self):
        if not self.command_open:
            return
        try:
            messages, eof = self.reader.read_available(self.command_fd)
            for message in messages:
                validate_command(message, self.scope_id, self.generation)
                kind = message['kind']
                if kind == 'CONFIG':
                    if self.config is not None or self.go_received or self.spawn_closed:
                        raise ProtocolError('duplicate or late CONFIG')
                    self.config = message['data']
                    self.event('ready')
                elif kind == 'GO':
                    if self.config is None or self.go_received or self.spawn_closed:
                        raise ProtocolError('duplicate or inadmissible GO')
                    self.go_received = True
                elif kind == 'FORCE':
                    self.stop()
                    self.force()
                else:
                    self.stop()
            if self.config is not None:
                self.reader.max_bytes = MAX_ACTION_BYTES
                if self.reader.incomplete:
                    raise ProtocolError('partial atomic action')
            if eof:
                self.command_open = False
                self.stop()
        except (OSError, ProtocolError, ReceiptError):
            self.command_open = False
            self.stop()

    def close_config_writer(self):
        if self.config_writer is not None:
            os.close(self.config_writer)
            self.config_writer = None
        self.config_bytes = memoryview(b'')

    def close_ready(self):
        if self.ready_fd is not None:
            os.close(self.ready_fd)
            self.ready_fd = None

    def spawn(self):
        self.spawn_closed = True  # exactly one child is ever admissible
        opened = []
        try:
            stdio = []
            for name in ('stdout.log', 'stderr.log'):
                descriptor = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=self.directory_fd)
                os.fchmod(descriptor, 0o600)
                opened.append(descriptor)
                stdio.append(descriptor)
            environment = child_environment(self.config['env'], self.mode, self._token)
            inherited = []
            role = self.config['role']
            if role != 'process':
                config_read, self.config_writer = os.pipe()
                opened.append(config_read)
                os.set_blocking(self.config_writer, False)
                self.config_bytes = memoryview(encode_frame(self.config['child_config']))
                inherited.append(config_read)
                config_name = {
                    'target': 'LAB_TARGET_CONFIG_FD',
                    'campaign': 'LAB_CAMPAIGN_CONFIG_FD',
                    'evaluation': 'LAB_EVALUATION_CONFIG_FD',
                    'conformance': 'LAB_CONFORMANCE_CONFIG_FD',
                    'gateway': 'LAB_GATEWAY_CONFIG_FD',
                }[role]
                environment[config_name] = str(config_read)
                if role == 'target':
                    inherited.append(self.ready_fd)
                    environment['LAB_TARGET_READY_FD'] = str(self.ready_fd)
            self.child = subprocess.Popen(self.config['argv'], env=environment,
                cwd=self.config['cwd'], stdin=subprocess.DEVNULL, stdout=stdio[0], stderr=stdio[1],
                close_fds=True, pass_fds=tuple(inherited),
                **({'process_group': 0} if self.mode == GROUP_TREE_MARKER else {}))
            if self.mode == GROUP_TREE_MARKER:
                self.mechanism = GroupTreeMarker(self.child.pid, self._token, ps=self._ps)
            self.event('child_started')
        except (OSError, ValueError, ProtocolError):
            self.event('spawn_failed')
            self.stop()
        finally:
            for descriptor in opened:
                os.close(descriptor)
            self.close_ready()
            if self.child is None:
                self.close_config_writer()

    def write_child_config(self):
        if self.config_writer is None:
            return
        try:
            count = os.write(self.config_writer, self.config_bytes)
            self.config_bytes = self.config_bytes[count:]
            if not self.config_bytes:
                self.close_config_writer()
        except BlockingIOError:
            pass
        except OSError:
            self.close_config_writer()
            self.stop()

    def group_tree_marker_step(self):
        if self.child is None:
            return True, False
        if self.child.returncode is not None:
            return True, True
        exited = self.mechanism.leader_exited()
        if exited:
            if not self._leader_exit_seen:
                self.mechanism.sweep()
                self._leader_exit_seen = True
        else:
            self.mechanism.poll()
        if (exited or self.forced
                or (self.stop_deadline is not None and time.monotonic() >= self.stop_deadline)):
            self.kill_current_children()
        return self.reap(), exited

    def run(self):
        for descriptor in (self.command_fd, self.event_fd, self.directory_fd, self.ready_fd):
            os.set_inheritable(descriptor, False)
        os.set_blocking(self.command_fd, False)
        os.set_blocking(self.event_fd, False)
        if not stat.S_ISDIR(os.fstat(self.directory_fd).st_mode):
            return 1
        if self.mode == SUBREAPER:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                return 1
            self.children_path.read_text()  # ownership prerequisite, before ready
        else:
            import psutil
            self._ps = psutil  # establish the dependency before CONFIG can emit ready
        def stop_signal(*_):
            self.signal_stop = True
        def force_signal(*_):
            self.signal_force = True
        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGINT, stop_signal)
        signal.signal(signal.SIGUSR1, force_signal)
        try:
            while True:
                self.commands()
                self.poll_request()
                if self.signal_stop:
                    self.stop()
                if self.signal_force:
                    self.stop()
                    self.force()
                if self.go_received and not self.spawn_closed:
                    self.spawn()
                self.write_child_config()
                if self.mode == GROUP_TREE_MARKER:
                    empty, exited = self.group_tree_marker_step()
                    if self.spawn_closed and empty:
                        break
                    time.sleep(SWEEP_WAIT_SECONDS if exited else .01)
                    continue
                empty = self.reap()
                if self.spawn_closed and empty:
                    break
                if self.child is not None and self.child.returncode is not None:
                    # The direct child has ended; surviving owned descendants
                    # have no useful work and are reaped after forced cleanup.
                    self.kill_current_children()
                elif self.forced or (self.stop_deadline is not None and time.monotonic() >= self.stop_deadline):
                    self.kill_current_children()
                time.sleep(.01)
        except BaseException:
            # Even an unexpected protocol/IO failure cannot abandon descendants.
            # Reuse the one pinned deadline and sticky forced fact in fallback.
            self.stop()
            if self.mode == GROUP_TREE_MARKER:
                while True:
                    empty, exited = self.group_tree_marker_step()
                    if empty:
                        break
                    time.sleep(SWEEP_WAIT_SECONDS if exited else .01)
            else:
                while not self.reap():
                    if self.forced or time.monotonic() >= self.stop_deadline:
                        self.kill_current_children()
                    time.sleep(.01)
        finally:
            self.close_config_writer()
            self.close_ready()
        self.spawn_closed = True
        if self.child is None:
            self.event('spawn_skipped')
        self.event('tree_empty')
        receipt = dict(schema='lab-scope-cleanup/2', ownership=self.mode, scope_id=self.scope_id,
            owner_generation=self.generation, child_launched=self.child is not None,
            child_exit_code=None if self.child is None else self.child.returncode,
            process_tree_empty=True, forced=self.forced, spawn_closed=True)
        validate_cleanup_receipt(receipt, self.scope_id, self.generation)
        try:
            publish_json_at(self.directory_fd, 'cleanup.receipt', receipt)
        except (OSError, ReceiptError):
            return 1
        return 0


def main():
    if len(sys.argv) != 8 or sys.argv[-1] not in {SUBREAPER, GROUP_TREE_MARKER}:
        return 2
    try:
        helper = ScopeHelper(*(int(v) for v in sys.argv[1:5]), *sys.argv[5:])
        return helper.run()
    except (OSError, ValueError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
