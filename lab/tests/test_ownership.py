"""Platform selection and the isolated child environment for process ownership."""
import subprocess
import sys
import os
import signal
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("platform,expected", [
    ("linux", "subreaper"), ("linux2", "subreaper"), ("darwin", "group_tree_marker"),
])
def test_select_mode_by_platform(platform, expected):
    from evolution_lab._ownership import select_mode
    assert select_mode(platform=platform, environ={}) == expected


def test_unsupported_platform_is_refused():
    from evolution_lab._ownership import select_mode
    with pytest.raises(ValueError, match="win32"):
        select_mode(platform="win32", environ={})


def test_seam_forces_macos_mode_on_linux_only():
    from evolution_lab._ownership import SEAM_NAME, select_mode
    assert select_mode("linux", {SEAM_NAME: "group_tree_marker"}) == "group_tree_marker"
    for value in ("group_tree_marker", "subreaper", "", "x"):
        assert select_mode("darwin", {SEAM_NAME: value}) == "group_tree_marker"
    for value in ("subreaper", "", "x"):
        with pytest.raises(ValueError, match=SEAM_NAME):
            select_mode("linux", {SEAM_NAME: value})


def test_operator_line_text():
    from evolution_lab._ownership import operator_line
    assert operator_line("group_tree_marker") == (
        "Process ownership (macOS): process group + process tree polled every 0.5 s + environment marker. "
        "A process that leaves the group, escapes the tree between polls and hides the marker can survive a stop."
    )
    assert operator_line("subreaper") is None


@pytest.mark.parametrize("mode,token,expected", [
    ("subreaper", None, {"KEEP": "value"}),
    ("group_tree_marker", "new-token", {"KEEP": "value", "EVOLUTION_LAB_SCOPE": "new-token"}),
])
def test_child_environment_drops_inherited_marker(mode, token, expected):
    from evolution_lab._ownership import child_environment
    base = {"KEEP": "value", "EVOLUTION_LAB_SCOPE": "outer",
            "EVOLUTION_LAB_TEST_OWNERSHIP": "group_tree_marker"}
    original = dict(base)
    assert child_environment(base, mode, token) == expected
    assert base == original


def test_unknown_mode_and_missing_marker_cannot_silently_degrade():
    from evolution_lab._ownership import child_environment, operator_line
    with pytest.raises(ValueError):
        child_environment({}, "unknown", None)
    with pytest.raises(ValueError):
        operator_line("unknown")
    with pytest.raises(ValueError):
        child_environment({}, "group_tree_marker", None)


def test_ownership_module_is_stdlib_only_at_import():
    result = subprocess.run([sys.executable, "-I", "-c",
        "import sys, evolution_lab._ownership; print('psutil' in sys.modules)"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "False\n"


class _NoSuchProcess(Exception):
    pass


class _ZombieProcess(_NoSuchProcess):
    pass


class _AccessDenied(Exception):
    pass


class _Process:
    def __init__(self, ps, pid):
        if pid not in ps.table:
            raise _NoSuchProcess(pid)
        self.ps, self.pid = ps, pid
        self.created = ps.table[pid].created

    def is_running(self):
        return self.pid in self.ps.table and self.ps.table[self.pid].created == self.created

    def status(self):
        if self.pid not in self.ps.table:
            raise _NoSuchProcess(self.pid)
        return self.ps.table[self.pid].status

    def create_time(self):
        return self.created

    def children(self, recursive=False):
        if not self.is_running():
            raise _NoSuchProcess(self.pid)
        self.ps.walks.append(self.pid)
        result = [self.ps.Process(p.pid) for p in self.ps.table.values() if p.ppid == self.pid]
        if recursive:
            for child in tuple(result):
                result.extend(child.children(recursive=True))
        return result

    def send_signal(self, number):
        if not self.is_running():
            raise _NoSuchProcess(self.pid)
        self.ps.signals.append((self.pid, number))


class _PS:
    STATUS_ZOMBIE = "zombie"
    NoSuchProcess = _NoSuchProcess
    ZombieProcess = _ZombieProcess
    AccessDenied = _AccessDenied

    def __init__(self):
        self.table, self.signals, self.walks = {}, [], []
        self.marker_scans = 0

    def add(self, pid, *, ppid=1, pgid=777, uid=7, env=None, status="running", created=1):
        self.table[pid] = SimpleNamespace(pid=pid, ppid=ppid, pgid=pgid, uid=uid,
            env={} if env is None else env, status=status, created=created)

    def Process(self, pid):
        return _Process(self, pid)

    def pids(self):
        return list(self.table)

    def process_iter(self, attrs):
        self.marker_scans += 1
        for pid, row in tuple(self.table.items()):
            process = self.Process(pid)
            process.info = {"uids": SimpleNamespace(real=row.uid),
                            "environ": None if isinstance(row.env, Exception) else row.env}
            yield process


class _OS:
    waitstatus_to_exitcode = staticmethod(os.waitstatus_to_exitcode)

    def __init__(self, ps):
        self.ps, self.calls = ps, []

    def getpid(self):
        return 999

    def getuid(self):
        return 7

    def getpgid(self, pid):
        self.calls.append(("getpgid", pid))
        if pid not in self.ps.table:
            raise ProcessLookupError(pid)
        return self.ps.table[pid].pgid

    def killpg(self, pid, number):
        self.calls.append(("killpg", pid, number))

    def waitpid(self, pid, options):
        self.calls.append(("waitpid", pid, options))
        self.ps.table.pop(pid)
        return pid, 37 << 8


def _fake_scope():
    from evolution_lab._ownership import GroupTreeMarker
    ps = _PS()
    ps.add(100, pgid=100)
    ps.add(999, pgid=999)
    clock = [0.0]
    osmod = _OS(ps)
    return GroupTreeMarker(100, "scope-token", ps=ps, osmod=osmod, now=lambda: clock[0]), ps, osmod, clock


def test_reused_pid_is_not_signalled():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.add(101, ppid=100)
    mechanism.poll()
    ps.add(101, ppid=1, created=2)
    ps.table[100].status = "zombie"
    assert mechanism.kill_union() is False
    assert ps.signals == []
    assert not any(call[0] == "killpg" for call in osmod.calls)


def test_leader_reaped_last_after_empty_scope():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.table[100].status = "zombie"
    with pytest.raises(RuntimeError):
        mechanism.reap_leader()
    assert mechanism.scope_empty() is False
    assert not any(call[0] == "waitpid" for call in osmod.calls)
    assert mechanism.scope_empty() is True
    assert mechanism.reap_leader() == 37
    assert [call for call in osmod.calls if call[0] == "waitpid"] == [("waitpid", 100, 0)]
    assert osmod.calls[-1] == ("waitpid", 100, 0)


def test_pgid_never_used_after_final_reap():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.table[100].status = "zombie"
    assert not mechanism.scope_empty()
    assert mechanism.scope_empty()
    mechanism.reap_leader()
    before = list(osmod.calls)
    for operation in (mechanism.kill_union, mechanism.sweep, mechanism.scope_empty):
        with pytest.raises(RuntimeError):
            operation()
    assert osmod.calls == before


def test_zombies_count_as_dead():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.add(101, ppid=100, status="zombie")
    ps.add(102, pgid=100, status="zombie")
    ps.table[100].status = "zombie"
    mechanism.poll()
    assert mechanism.kill_union() is False
    assert mechanism.scope_empty() is True
    assert ps.signals == []
    assert not any(call[0] == "killpg" for call in osmod.calls)


def test_scope_empty_needs_two_clean_sweeps():
    mechanism, ps, _osmod, _ = _fake_scope()
    ps.table[100].status = "zombie"
    ps.add(101, env={"EVOLUTION_LAB_SCOPE": "scope-token"})
    assert not mechanism.scope_empty()
    ps.table[101].status = "zombie"
    assert mechanism.scope_empty() is False
    assert mechanism.scope_empty() is True


def test_union_kills_group_tree_and_marked():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.add(101, ppid=100, pgid=101)
    ps.add(102, pgid=100)
    ps.add(103, pgid=103, env={"EVOLUTION_LAB_SCOPE": "scope-token"})
    ps.table[999].env = {"EVOLUTION_LAB_SCOPE": "scope-token"}
    assert mechanism.kill_union() is True
    assert ("killpg", 100, signal.SIGKILL) in osmod.calls
    assert set(ps.signals) == {(101, signal.SIGKILL), (102, signal.SIGKILL), (103, signal.SIGKILL)}


def test_unknown_environment_neither_adds_nor_removes():
    mechanism, ps, _osmod, _ = _fake_scope()
    ps.add(101, ppid=100, env=_AccessDenied())
    ps.add(102, env={})
    ps.add(103, env=_AccessDenied())
    mechanism.poll()
    ps.table[101].ppid = 1
    assert mechanism.kill_union() is True
    assert ps.signals == [(101, signal.SIGKILL)]


def test_marker_scan_needs_exact_token_and_own_uid():
    mechanism, ps, _osmod, _ = _fake_scope()
    ps.add(101, env={"EVOLUTION_LAB_SCOPE": "scope-token"})
    ps.add(102, env={"EVOLUTION_LAB_SCOPE": "scope-token-extra"})
    ps.add(103, env={"EVOLUTION_LAB_SCOPE": "scope"})
    ps.add(104, uid=8, env={"EVOLUTION_LAB_SCOPE": "scope-token"})
    assert mechanism.kill_union() is True
    assert ps.signals == [(101, signal.SIGKILL)]


def test_kill_union_reports_forced_only_for_live_members():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.table[100].status = "zombie"
    assert mechanism.kill_union() is False
    assert not any(call[0] == "killpg" for call in osmod.calls)
    assert ps.signals == []


def test_kill_union_kills_a_live_leader_without_descendants():
    mechanism, ps, osmod, _ = _fake_scope()
    assert mechanism.kill_union() is True
    assert ("killpg", 100, signal.SIGKILL) in osmod.calls
    assert ps.signals == []  # the leader belongs to the group, never a saved-pid signal


def test_poll_walks_every_half_second():
    mechanism, ps, _osmod, clock = _fake_scope()
    mechanism.poll()
    clock[0] = .3
    mechanism.poll()
    assert ps.walks.count(100) == 1
    clock[0] = .5
    mechanism.poll()
    assert ps.walks.count(100) == 2


def test_live_leader_empty_check_does_not_trigger_cleanup_sweeps():
    mechanism, ps, osmod, clock = _fake_scope()
    for instant in (0, .1, .3, .5):
        clock[0] = instant
        mechanism.poll()
        assert mechanism.scope_empty() is False
    assert ps.walks.count(100) == 2
    assert ps.marker_scans == 0
    assert osmod.calls == []


def test_newcomer_before_final_reap_returns_to_cleanup():
    mechanism, ps, osmod, _ = _fake_scope()
    ps.table[100].status = "zombie"
    assert not mechanism.scope_empty()
    assert mechanism.scope_empty()
    ps.add(101, env={"EVOLUTION_LAB_SCOPE": "scope-token"})
    assert mechanism.reap_leader() is None
    assert not any(call[0] == "waitpid" for call in osmod.calls)
    assert mechanism.kill_union()
    assert ps.signals == [(101, signal.SIGKILL)]
    ps.table[101].status = "zombie"
    assert not mechanism.scope_empty()
    assert mechanism.scope_empty()
    assert mechanism.reap_leader() == 37
    assert osmod.calls[-1] == ("waitpid", 100, 0)
