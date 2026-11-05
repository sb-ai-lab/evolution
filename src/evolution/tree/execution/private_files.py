"""Private regular-file admission before writing."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path

_NOT_A_REGULAR_FILE_ERRNOS = frozenset({errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK})
"""Opening a private sink failed *because* it is not a plain file to write to."""


class WorkspaceError(RuntimeError):
    """The Candidate workspace cannot be read, admitted or replaced."""


def open_private_file(path: Path, flags: int, *, refusal: str) -> int:
    """Open `path` and prove the descriptor before anything is written through it.

    The one open-and-verify used by every private sink an attempt writes -- the
    attempt transcript here and the subprocess editor's stderr file -- because
    both pathnames are predictable and both roots may already hold entries on
    resume. `O_NOFOLLOW` alone rejects only a symlink: a FIFO at the name would
    block the open, synchronously, before the event loop can fire the editor's
    deadline, and a hard link to a file outside the run directory would receive
    the whole stream and the `fchmod`. So the open adds `O_NONBLOCK`, which
    turns the FIFO into an immediate refusal, and the `fstat` refuses anything
    that is not an owner-owned, single-link regular file. `O_NONBLOCK` is
    cleared again before the descriptor is returned, so the writes themselves
    remain ordinary blocking writes.

    `O_TRUNC` is honoured, but *after* the check rather than by the open, as an
    `ftruncate`: an open that truncates would empty a hard-linked file outside
    the run directory before the `fstat` could refuse it, which is the very
    write this exists to prevent.

    `refusal` is the message of the `WorkspaceError` a refusal raises: it names
    the entry's basename and never an absolute path.
    """

    truncate = bool(flags & os.O_TRUNC)
    try:
        descriptor = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        # A FIFO with no reader, or a device that would have blocked: the
        # entry is not the private regular file this is allowed to write to.
        # Every other errno -- `ELOOP` for the symlink above all -- stays the
        # `OSError` the callers already classify.
        if exc.errno in _NOT_A_REGULAR_FILE_ERRNOS:
            raise WorkspaceError(refusal) from exc
        raise
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
        ):
            raise WorkspaceError(refusal)
        os.fchmod(descriptor, 0o600)
        if truncate:
            os.ftruncate(descriptor, 0)
        blocking = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, blocking & ~os.O_NONBLOCK)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
