"""Global concurrency gate for any local sglang endpoint.

sglang serves a single GPU; flooding it from many threads — or from many
SEPARATE OS PROCESSES (parallel anchor-skill workers) — collapses
throughput. The gate bounds aggregate concurrency on a given endpoint
both within a process (a ``threading.BoundedSemaphore`` fast path) and
ACROSS processes (an ``fcntl.flock``-backed slot pool under a shared
lock directory). Remote provider calls are not routed through here; they
use the provider's own rate limiting.

Use:

    from _shared.local_endpoint_gate import gate_for

    with gate_for(api_base).permit():
        # local-sglang chat completion call
        ...

The gate is keyed on the endpoint base URL so a second sglang endpoint
gets its own permits. Default base is read from
``LOCAL_SGLANG_API_BASE`` (fallback to legacy ``LOCAL_QWEN_API_BASE``,
fallback to ``http://localhost:30004/v1``). Permit count is read from
``LOCAL_SGLANG_GATE_PERMITS`` (legacy ``LOCAL_QWEN_GATE_PERMITS``,
default 3) and is fixed at first creation per ``api_base``.

Cross-process semantics: ``permits`` slot files live under
``<tmp>/evo_endpoint_gate/<sanitized-base>/slot_<i>.lock``. ``permit()``
acquires one free slot via non-blocking ``flock(LOCK_EX|LOCK_NB)``,
scanning slots and retrying with a small sleep until one is obtained;
the slot is released (``flock(LOCK_UN)`` + close) on context exit,
including on exception. flock is auto-released by the OS when the
holding process dies or the fd closes, so a crashed holder never leaks
a slot or deadlocks the run — no manual unlock file is used.

Renamed from ``local_qwen_gate.py`` on 2026-05-12. The implementation
never was Qwen-specific — it gates any localhost api_base. The old
module path remains as a re-export shim for unrelated experiment
scripts.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_BASE = (
    os.environ.get("LOCAL_SGLANG_API_BASE")
    or os.environ.get("LOCAL_QWEN_API_BASE")  # legacy
    or "http://localhost:30004/v1"
)
_DEFAULT_PERMITS = int(
    os.environ.get("LOCAL_SGLANG_GATE_PERMITS")
    or os.environ.get("LOCAL_QWEN_GATE_PERMITS")  # legacy
    or "3"
)

# Root for cross-process slot files. Shared by every process on the host.
# `EVO_ENDPOINT_GATE_LOCK_ROOT` overrides the default purely as a testability
# seam (hermetic tests point it under pytest tmp_path); default unchanged.
_LOCK_ROOT = Path(
    os.environ.get("EVO_ENDPOINT_GATE_LOCK_ROOT")
    or (Path(tempfile.gettempdir()) / "evo_endpoint_gate")
)

# Poll interval while waiting for a free slot (seconds).
_ACQUIRE_POLL_S = 0.05


def _sanitize_base(base: str) -> str:
    """Stable, filesystem-safe directory name for an api_base.

    Keeps a readable prefix plus a hash so distinct bases never collide
    even after sanitisation.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")[:64]
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{safe}_{digest}" if safe else digest


class _Gate:
    """Per-endpoint concurrency gate.

    ``permit()`` acquires the process-local semaphore first (cheap
    in-process coordination) and then a cross-process flock slot, so the
    aggregate number of permits in flight on this ``base`` across ALL
    processes is bounded by ``permits``.
    """

    def __init__(self, base: str, permits: int) -> None:
        self.base = base
        self.permits = max(1, int(permits))
        self._sem = threading.BoundedSemaphore(value=self.permits)
        self._slot_dir = _LOCK_ROOT / _sanitize_base(base)
        self._slot_dir.mkdir(parents=True, exist_ok=True)
        self._slot_paths = [self._slot_dir / f"slot_{i}.lock" for i in range(self.permits)]
        for sp in self._slot_paths:
            # Ensure the slot file exists; flock needs an fd to an
            # existing inode. Empty file, never written to.
            sp.touch(exist_ok=True)

    def _acquire_slot(self) -> int:
        """Block until a cross-process slot is obtained; return its fd.

        Scans the slot files with non-blocking exclusive flock. On a
        crashed holder the OS has already released that slot's flock, so
        a free fd will be found on a subsequent scan — no leak, no
        deadlock.
        """
        while True:
            for sp in self._slot_paths:
                fd = os.open(str(sp), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    # Only EWOULDBLOCK/EAGAIN means "slot busy, try next".
                    # Any other OSError (EBADF, ENOLCK, ...) is pathological:
                    # re-raise so it crashes loudly instead of infinite-
                    # spinning an unattended run. Close fd first (no leak).
                    os.close(fd)
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    continue
                return fd
            # Unbounded 50ms poll is intentional: GPU calls are multi-minute
            # and contention is low, so we WANT to block until a slot frees;
            # a timeout/backoff would just relocate the failure, not fix it.
            time.sleep(_ACQUIRE_POLL_S)

    @contextmanager
    def permit(self) -> Iterator[None]:
        # Process-local fast path first (cheap thread coordination),
        # then the cross-process flock slot. Aggregate concurrency on
        # this base across all processes is thereby <= permits.
        self._sem.acquire()
        fd: int | None = None
        try:
            fd = self._acquire_slot()
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)  # closing the fd also drops the flock
            self._sem.release()


_GATES: dict[str, _Gate] = {}
_LOCK = threading.Lock()


def gate_for(api_base: str | None = None, permits: int | None = None) -> _Gate:
    """Return the gate for the given local sglang endpoint.

    ``permits`` is honoured only at first creation per ``api_base``; later
    callers get the existing gate even if they ask for a different count.
    Reasoning: the permit count is a property of the GPU, not the caller.
    """
    base = (api_base or _DEFAULT_BASE).rstrip("/")
    p = int(permits if permits is not None else _DEFAULT_PERMITS)
    with _LOCK:
        if base not in _GATES:
            _GATES[base] = _Gate(base, p)
        return _GATES[base]


def is_local_endpoint(api_base: str | None) -> bool:
    """Heuristic: True if ``api_base`` points at a localhost endpoint."""
    if not api_base:
        return False
    b = api_base.lower()
    return ("localhost" in b) or ("127.0.0.1" in b) or ("0.0.0.0" in b)


# Convenience: the default gate for local-sglang source/eval calls.
local_endpoint_gate = gate_for()

# Back-compat aliases (deprecated). Kept so unrelated experiment scripts
# that import the old names keep working without a sweep. These are
# documented re-exports, not silent fallbacks (per `feedback_no_silent_fallbacks`).
is_local_qwen = is_local_endpoint
qwen_gate = local_endpoint_gate


__all__ = [
    "gate_for",
    "is_local_endpoint",
    "local_endpoint_gate",
    # Back-compat:
    "is_local_qwen",
    "qwen_gate",
]
