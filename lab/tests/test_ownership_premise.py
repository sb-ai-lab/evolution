"""Prove group identity while the exited leader remains an unreaped zombie."""
from contextlib import contextmanager
import json
import os
import select
import signal
import subprocess
import sys
import time
import uuid

import psutil
import pytest

from platforms import linux_only


pytestmark = linux_only("proves the premise with waitid WNOWAIT and /proc-free psutil on Linux")
_MARKER = "EVOLUTION_LAB_SCOPE"
_MEMBER = """
import json, os, signal, sys
command = int(sys.argv[1])
print(json.dumps({'member': os.getpid()}), flush=True)
if os.read(command, 1) == b'f':
    if os.fork() == 0:
        print(json.dumps({'grandchild': os.getpid()}), flush=True)
    else:
        os._exit(0)
while True:
    signal.pause()
"""
_LEADER = """
import os, sys
if os.fork() == 0:
    os.execve(sys.executable, [sys.executable, '-c', sys.argv[2], sys.argv[1]], {})
os._exit(23)
"""


def _until(predicate, message, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError(message)


def _dead(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _frame(leader):
    assert select.select([leader.stdout], [], [], 2)[0], "child did not report its pid"
    return json.loads(leader.stdout.readline())


def _reap(leader):
    pid, status = os.waitpid(leader.pid, 0)
    assert pid == leader.pid
    leader.returncode = os.waitstatus_to_exitcode(status)
    assert leader.returncode == 23


@contextmanager
def _scope():
    read_fd, write_fd = os.pipe()
    token = uuid.uuid4().hex
    leader = None
    try:
        leader = subprocess.Popen(
            [sys.executable, "-c", _LEADER, str(read_fd), _MEMBER],
            process_group=0, env={_MARKER: token}, pass_fds=(read_fd,),
            stdout=subprocess.PIPE, text=True,
        )
        os.close(read_fd)
        read_fd = None
        member = _frame(leader)["member"]
        _until(lambda: psutil.Process(leader.pid).status() == psutil.STATUS_ZOMBIE,
               "leader did not become a zombie")
        yield leader, member, write_fd, token
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(write_fd)
        if leader is not None:
            # Never call poll/wait or create a new Popen before the final reap.
            if leader.returncode is None:
                os.killpg(leader.pid, signal.SIGKILL)
                if 'member' in locals():
                    _until(lambda: _dead(member), "group member survived fixture cleanup")
                _reap(leader)
            leader.stdout.close()


def test_zombie_leader_keeps_pgid_valid():
    with _scope() as (leader, member, _command, _token):
        observed = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert observed is not None and observed.si_pid == leader.pid
        assert os.getpgid(member) == leader.pid
        os.killpg(leader.pid, 0)
        assert psutil.Process(leader.pid).status() == psutil.STATUS_ZOMBIE


def test_killpg_reaches_member_forked_after_last_poll():
    with _scope() as (leader, member, command, token):
        swept = {p.pid for p in psutil.Process(leader.pid).children(recursive=True)}
        for process in psutil.process_iter(["uids", "environ"]):
            try:
                uids, environ = process.info["uids"], process.info["environ"]
                if (uids is not None and uids.real == os.getuid()
                        and environ and environ.get(_MARKER) == token):
                    swept.add(process.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        os.write(command, b"f")
        grandchild = _frame(leader)["grandchild"]
        _until(lambda: _dead(member), "member did not exit after forking")
        escaped = psutil.Process(grandchild)
        assert escaped.ppid() != member
        assert escaped.pid not in swept
        assert _MARKER not in escaped.environ()
        assert os.getpgid(grandchild) == leader.pid
        os.killpg(leader.pid, signal.SIGKILL)
        _until(lambda: _dead(grandchild), "post-poll group member survived SIGKILL")
        _reap(leader)


def test_reaped_leader_releases_the_pgid_only_after_reap():
    with _scope() as (leader, member, _command, _token):
        created = psutil.Process(leader.pid).create_time()
        os.killpg(leader.pid, signal.SIGKILL)
        _until(lambda: _dead(member), "member survived before final reap")
        _reap(leader)
        try:
            replacement = psutil.Process(leader.pid)
        except psutil.NoSuchProcess:
            return
        assert replacement.create_time() != created
