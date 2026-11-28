"""Explicit process ownership selection; optional process tooling loads on use."""
from __future__ import annotations

from collections.abc import Mapping
import os
import signal
import sys
import time


SUBREAPER = "subreaper"
GROUP_TREE_MARKER = "group_tree_marker"
MARKER_NAME = "EVOLUTION_LAB_SCOPE"
SEAM_NAME = "EVOLUTION_LAB_TEST_OWNERSHIP"
POLL_SECONDS = 0.5
SWEEP_WAIT_SECONDS = 0.25
OPERATOR_LINE = (
    "Process ownership (macOS): process group + process tree polled every 0.5 s + environment marker. "
    "A process that leaves the group, escapes the tree between polls and hides the marker can survive a stop."
)


def select_mode(platform=None, environ=None) -> str:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform.startswith("linux"):
        if SEAM_NAME in environ:
            if environ[SEAM_NAME] != GROUP_TREE_MARKER:
                raise ValueError(f"invalid {SEAM_NAME}")
            return GROUP_TREE_MARKER
        return SUBREAPER
    if platform == "darwin":
        return GROUP_TREE_MARKER
    raise ValueError(f"unsupported ownership platform: {platform}")


def operator_line(mode: str) -> str | None:
    if mode == GROUP_TREE_MARKER:
        return OPERATOR_LINE
    if mode == SUBREAPER:
        return None
    raise ValueError("unknown ownership mode")


def child_environment(base: Mapping[str, str], mode: str, token: str | None) -> dict[str, str]:
    if mode not in {SUBREAPER, GROUP_TREE_MARKER}:
        raise ValueError("unknown ownership mode")
    result = {key: value for key, value in base.items() if key not in {MARKER_NAME, SEAM_NAME}}
    if mode == GROUP_TREE_MARKER:
        if not isinstance(token, str) or not token:
            raise ValueError("ownership marker required")
        result[MARKER_NAME] = token
    return result


class GroupTreeMarker:
    """Own the union while the original group leader remains unreaped."""

    def __init__(self, leader_pid, token, *, ps=None, osmod=os, now=time.monotonic):
        if ps is None:
            import psutil
            ps = psutil
        self._ps, self._os, self._now = ps, osmod, now
        self._leader = ps.Process(leader_pid)
        self._token = token
        self._self_pid = osmod.getpid()
        self._owned = {}
        self._group_live = {}
        self._next_poll = 0.0
        self._empty_sweeps = 0
        self._empty_ready = False
        self._reaped = False

    def _active(self):
        if self._reaped:
            raise RuntimeError("ownership already reaped")
        if not self._leader.is_running():
            raise RuntimeError("leader identity lost before final reap")

    def _live(self, process):
        try:
            return process.is_running() and process.status() != self._ps.STATUS_ZOMBIE
        except (self._ps.NoSuchProcess, self._ps.ZombieProcess):
            return False
        except self._ps.AccessDenied:
            # Missing status is not evidence that an owned process has died.
            return True

    def _remember(self, process):
        if process.pid not in {self._leader.pid, self._self_pid} and self._live(process):
            self._owned[process.pid] = process

    def _walk(self):
        self._owned = {pid: process for pid, process in self._owned.items() if self._live(process)}
        for root in (self._leader, *tuple(self._owned.values())):
            if not self._live(root):
                continue
            try:
                children = root.children(recursive=True)
            except (self._ps.NoSuchProcess, self._ps.ZombieProcess, self._ps.AccessDenied):
                continue
            for process in children:
                self._remember(process)

    def poll(self):
        self._active()
        instant = self._now()
        if instant >= self._next_poll:
            self._walk()
            self._next_poll = instant + POLL_SECONDS

    def leader_exited(self) -> bool:
        self._active()
        return self._leader.status() == self._ps.STATUS_ZOMBIE

    def sweep(self):
        self._active()
        self._walk()
        for process in self._ps.process_iter(["uids", "environ"]):
            try:
                uids, environ = process.info["uids"], process.info["environ"]
                if (uids is not None and uids.real == self._os.getuid()
                        and environ and environ.get(MARKER_NAME) == self._token):
                    self._remember(process)
            except (self._ps.NoSuchProcess, self._ps.ZombieProcess, self._ps.AccessDenied):
                continue
        self._group_live = {}
        for pid in self._ps.pids():
            if pid in {self._leader.pid, self._self_pid}:
                continue
            try:
                if self._os.getpgid(pid) == self._leader.pid:
                    process = self._ps.Process(pid)
                    if self._live(process):
                        self._group_live[pid] = process
                        self._remember(process)
            except (ProcessLookupError, PermissionError, self._ps.NoSuchProcess,
                    self._ps.ZombieProcess, self._ps.AccessDenied):
                continue
        clean = self.leader_exited() and not self._group_live and not self._owned
        self._empty_sweeps = self._empty_sweeps + 1 if clean else 0
        self._empty_ready = self._empty_sweeps >= 2

    def kill_union(self) -> bool:
        self.sweep()
        signalled = False
        if self._group_live or not self.leader_exited():
            try:
                self._os.killpg(self._leader.pid, signal.SIGKILL)
                signalled = True
            except ProcessLookupError:
                pass
        for process in tuple(self._owned.values()):
            if not self._live(process):
                continue
            try:
                process.send_signal(signal.SIGKILL)
                signalled = True
            except (self._ps.NoSuchProcess, self._ps.ZombieProcess, self._ps.AccessDenied):
                pass
        return signalled

    def scope_empty(self) -> bool:
        if not self.leader_exited():
            self._empty_sweeps = 0
            self._empty_ready = False
            return False
        self.sweep()
        return self._empty_ready

    def reap_leader(self) -> int | None:
        """Reap last, or return None when the final sweep requires more cleanup."""
        self._active()
        if not self._empty_ready:
            raise RuntimeError("scope has not been observed empty")
        self.sweep()
        if not self._empty_ready:
            return None
        pid, status = self._os.waitpid(self._leader.pid, 0)
        self._reaped = True
        if pid != self._leader.pid:
            raise RuntimeError("unexpected leader reap")
        return self._os.waitstatus_to_exitcode(status)
