"""Explicit platform skips for tests that exercise a kernel contract."""
import fcntl
import os
import sys

import pytest


def linux_only(reason: str):
    return pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only: " + reason)


def darwin_only(reason: str):
    return pytest.mark.skipif(sys.platform != "darwin", reason="macOS only: " + reason)


def fd_path(fd: int) -> str:
    """Path of an open file/directory for descriptor-based fault/audit checks."""
    if sys.platform.startswith('linux'):
        return os.readlink(f'/proc/self/fd/{fd}')
    if sys.platform == 'darwin':
        return os.fsdecode(fcntl.fcntl(fd, getattr(fcntl, 'F_GETPATH'), bytes(1024)).rstrip(b'\0'))
    raise ValueError('unsupported descriptor-path platform')


def linux_mounts() -> list[str]:
    """Lines of the Linux mount table; callers check the platform first."""
    with open('/proc/self/mounts', encoding='utf-8') as table:
        return table.read().splitlines()


def linux_process_environ(pid: int) -> list[bytes]:
    """NUL-separated environment entries of a Linux process."""
    with open(f'/proc/{pid}/environ', 'rb') as environ:
        return environ.read().split(b'\0')
