"""Native Darwin evidence; Linux cannot establish these platform premises."""
import fcntl
import os
from pathlib import Path
import platform
import select
import sqlite3
import subprocess
import sys
import tempfile
import time

import psutil
import pytest

from evolution_lab._ownership import GROUP_TREE_MARKER, SEAM_NAME, select_mode
from platforms import darwin_only
from test_owner_scopes_ownership import (
    test_probe_matrix_group_tree_marker as _exercise_matrix,
    test_group_member_forked_after_last_poll_is_killed as _exercise_post_poll_fork,
)

pytestmark = darwin_only('the real macOS ownership check')


@pytest.fixture(autouse=True)
def unforced_ownership(monkeypatch):
    monkeypatch.delenv(SEAM_NAME, raising=False)
    assert sys.platform == 'darwin'
    assert select_mode() == GROUP_TREE_MARKER


async def test_six_child_matrix_live_helper(tmp_path):
    # Includes the protected /bin/sleep, fresh sessions, cleared environments
    # and a double fork; the same live helper harness runs on Linux.
    await _exercise_matrix(tmp_path)


def test_exit_observed_while_leader_stays_zombie():
    print(f'Darwin ownership: macOS={platform.mac_ver()[0]} machine={platform.machine()} '
          f'Python={platform.python_version()} psutil={psutil.__version__} '
          f'waitid={hasattr(os, "waitid")} kqueue={hasattr(select, "kqueue")}')
    leader = subprocess.Popen([sys.executable, '-I', '-c', 'raise SystemExit(0)'], process_group=0)
    try:
        process = psutil.Process(leader.pid)
        deadline = time.monotonic() + 5
        while process.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(.01)
        assert process.status() == psutil.STATUS_ZOMBIE
        assert leader.returncode is None
        pid, status = os.waitpid(leader.pid, os.WNOHANG)
        assert pid == leader.pid, 'status observation must leave the child available for final reap'
        leader.returncode = os.waitstatus_to_exitcode(status)
        assert leader.returncode == 0
    finally:
        if leader.returncode is None:
            leader.kill()
            leader.wait(timeout=5)


async def test_group_member_forked_after_last_poll_darwin(tmp_path):
    await _exercise_post_poll_fork(tmp_path)


def test_store_uses_f_getpath(tmp_path, monkeypatch):
    from evolution_lab.owner.store import OwnerStateStore
    connected = []
    original = sqlite3.connect
    def connect(path, **kwargs):
        connected.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with OwnerStateStore.open(tmp_path / 'state') as store:
        directory = os.fsdecode(fcntl.fcntl(store._root_fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0'))
        expected = os.path.join(directory, 'owner.sqlite3')
        assert connected == [expected]
        connected_info = os.stat(expected)
        admitted_info = os.stat('owner.sqlite3', dir_fd=store._root_fd, follow_symlinks=False)
        assert (connected_info.st_dev, connected_info.st_ino) == (admitted_info.st_dev, admitted_info.st_ino)
        assert store.generation


async def test_state_under_tmp():
    from evolution_lab.owner import Owner
    with tempfile.TemporaryDirectory(prefix='lab-darwin-', dir='/tmp') as directory:
        state = Path(directory) / 'state'
        async with Owner.open(state) as owner:
            assert str(owner.state_directory).startswith('/private/tmp/')
            assert owner.state_directory == Path(os.path.realpath(state))
            assert owner.ownership == GROUP_TREE_MARKER
