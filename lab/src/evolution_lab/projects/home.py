"""The Lab home above pairs and its single-writer lock (ADR-0017 p. 1-2)."""
from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import Mapping

from evolution_lab._paths import canonical_root

from .errors import ProjectError

LOCK_NAME = "home.lock"
REGISTRY_NAME = "projects.sqlite3"


def default_home(environ: Mapping[str, str] = os.environ) -> Path:
    if environ.get("LAB_HOME"):
        return Path(environ["LAB_HOME"])
    base = environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "evolution-lab"


def _no_symlinks(path: Path) -> None:
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ProjectError("home_invalid")
    except FileNotFoundError:
        pass


def prepare_home(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ProjectError("home_invalid")
    try:
        path = canonical_root(path)
    except ValueError:
        raise ProjectError('home_invalid') from None
    _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProjectError("home_invalid")
    for name in ("clones", "workspaces"):
        (path / name).mkdir(mode=0o700, exist_ok=True)
    return path


class HomeLock:
    """Exclusive, non-blocking flock on ``home.lock`` for one writer per home."""

    def __init__(self, home: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(Path(home) / LOCK_NAME, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ProjectError("home_busy") from None
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
