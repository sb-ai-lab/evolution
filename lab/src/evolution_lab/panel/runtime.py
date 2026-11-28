"""The panel's runtime file and single-panel lock in the Lab home (ADR-0018 p. 4).

Liveness is the lock, not the PID: a panel holds ``panel.lock`` (flock) for its whole life, so
a free lock means no panel, whatever ``panel.json`` says.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from evolution_lab._paths import canonical_root
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.home import _no_symlinks  # the same no-symlink rule as the service

from evolution_lab.durability import sync_file

RUNTIME_SCHEMA = "lab-panel-runtime/1"
STATUS_SCHEMA = "lab-panel-status/1"
RUNTIME_NAME = "panel.json"
LOCK_NAME = "panel.lock"
LOG_PATH = Path("logs") / "panel.log"
_KEYS = frozenset({"schema", "pid", "port", "url", "started_at", "home"})

MESSAGES = {
    "panel_already_running": "A panel is already running for this Lab home.",
    "panel_dependencies_missing": "The panel requires the optional panel dependencies (pip install 'evolution-lab[panel]').",
    "panel_port_in_use": "The requested panel port is already in use.",
    "panel_start_failed": "The background panel exited before it became ready.",
    "panel_start_timeout": "The background panel did not become ready in time.",
    "panel_identity_unconfirmed": "The process named in panel.json did not confirm it is this panel; nothing was stopped.",
    "panel_stop_timeout": "The panel did not stop in time.",
    "home_invalid": "The Lab home path is invalid.",
}


class PanelError(Exception):
    def __init__(self, code: str, **fields):
        super().__init__(code)
        self.code, self.message, self.fields = code, MESSAGES[code], fields

    def payload(self) -> dict:
        return {"error": {"code": self.code, "stage": "panel", "message": self.message}, **self.fields}


def prepare_panel_home(home: Path) -> Path:
    """Validate the Lab home for the panel and create only the home directory if it is missing.

    The panel owns just ``panel.json``, ``panel.lock`` and ``logs/panel.log`` in the home
    (ADR-0018 p. 1, amended): no general home preparation. Ancestors are canonicalized;
    the home itself and its control objects must not be symlinks.
    """
    path = Path(home)
    if not path.is_absolute() or ".." in path.parts:
        raise PanelError("home_invalid")
    try:
        path = canonical_root(path)
        _no_symlinks(path)
    except (ProjectError, ValueError):
        raise PanelError("home_invalid") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise PanelError("home_invalid")
    return path


def existing_panel_home(home: Path) -> Path | None:
    """Read-only home check for ``status``/``stop``: the same rule as start, nothing is created.

    Returns None when the home does not exist (no panel can run there); ``home_invalid`` for a
    relative or ``..`` path, a symlinked home root, or a non-directory.
    """
    path = Path(home)
    if not path.is_absolute() or ".." in path.parts:
        raise PanelError("home_invalid")
    try:
        path = canonical_root(path)
        _no_symlinks(path)
    except (ProjectError, ValueError):
        raise PanelError("home_invalid") from None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise PanelError("home_invalid")
    return path


def open_log(home: Path) -> int:
    """Open ``H/logs/panel.log`` for appending; ``logs`` is created 0700 only if missing.

    An existing ``logs`` must be a real directory owned by the user; neither it nor the log
    file is followed if it is a symlink, and nothing is chmod-ed through a path.
    """
    logs = Path(home) / LOG_PATH.parent
    created = True
    try:
        os.mkdir(logs, 0o700)
    except FileExistsError:
        created = False
    info = os.lstat(logs)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PanelError("home_invalid")
    try:
        directory = os.open(logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise PanelError("home_invalid") from None
        raise
    try:
        if created:
            os.fchmod(directory, 0o700)  # through the descriptor: independent of the umask
        try:
            fd = os.open(LOG_PATH.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise PanelError("home_invalid") from None
            raise
    finally:
        os.close(directory)
    os.fchmod(fd, 0o600)
    return fd


def runtime_record(*, pid: int, port: int, host: str, token: str, started_at: str, home: Path) -> dict:
    return {"schema": RUNTIME_SCHEMA, "pid": pid, "port": port,
            "url": f"http://{host}:{port}/?token={token}", "started_at": started_at, "home": str(home)}


def write_runtime(home: Path, record: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".panel-", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, separators=(",", ":"))
            stream.flush()
            sync_file(stream.fileno())
        os.replace(temporary, Path(home) / RUNTIME_NAME)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_runtime(home: Path) -> dict | None:
    try:
        # O_NOFOLLOW: a symlinked panel.json reads as absent (ELOOP is an OSError).
        fd = os.open(Path(home) / RUNTIME_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.loads(stream.read())
    except (ValueError, OSError):
        return None
    if not isinstance(value, dict) or set(value) != _KEYS or value.get("schema") != RUNTIME_SCHEMA:
        return None
    for key in ("pid", "port"):
        if type(value[key]) is not int or value[key] <= 0:
            return None
    if value["port"] > 65535:
        return None
    if not all(isinstance(value[key], str) for key in ("url", "started_at", "home")):
        return None
    return value


def remove_runtime(home: Path, pid: int) -> None:
    record = read_runtime(home)
    if record is not None and record["pid"] == pid:
        (Path(home) / RUNTIME_NAME).unlink(missing_ok=True)


def acquire_lock(home: Path) -> int:
    try:
        fd = os.open(Path(home) / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PanelError("home_invalid") from None
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        record = read_runtime(home)
        raise PanelError("panel_already_running", url=None if record is None else record["url"]) from None
    return fd


def release_lock(fd: int) -> None:
    os.close(fd)  # closing the descriptor releases the flock


def lock_is_held(home: Path) -> bool:
    try:
        fd = acquire_lock(home)
    except PanelError as error:
        if error.code != "panel_already_running":
            raise
        return True
    release_lock(fd)
    return False


def runtime_status(home: Path) -> dict:
    try:
        fd = acquire_lock(home)
    except PanelError as error:
        if error.code != "panel_already_running":
            raise
        return {"schema": STATUS_SCHEMA, "state": "running", "panel": read_runtime(home)}
    try:
        # Unlink while the probe lock is held: a panel starting now blocks on the lock
        # and writes its file only after we release, so a fresh file is never deleted.
        (Path(home) / RUNTIME_NAME).unlink(missing_ok=True)
    finally:
        release_lock(fd)
    return {"schema": STATUS_SCHEMA, "state": "not_running", "panel": None}
