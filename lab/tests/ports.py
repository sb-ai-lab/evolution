"""Ports for test targets, gateways and panels that bind later, in another process (#475).

Binding port 0, closing the socket and writing the number into a manifest leaves a gap before the
real server binds it. Port 0 draws from the ephemeral range, which the host's outgoing connections
and the other xdist workers share, so the port could be taken in that gap
(``OSError: [Errno 98] Address already in use``). These ports come from below the ephemeral range
on Linux (32768) and macOS (49152), from a block that belongs to one xdist worker. A port that
fails a bind probe is skipped.

Tests that need a port held open (a foreign listener, a server handed the socket) keep binding
port 0 themselves.
"""

from __future__ import annotations

import os
import random
import re
import socket

BASE = 20000
BLOCK = 500
EPHEMERAL_FLOOR = 32768


class PortAllocator:
    """Hands out the ports of one worker's block in turn, starting at ``offset``."""

    def __init__(self, worker: str, *, offset: int) -> None:
        match = re.fullmatch(r"gw(\d+)", worker)
        index = int(match.group(1)) if match else 0
        self.start = BASE + index * BLOCK
        if self.start + BLOCK > EPHEMERAL_FLOOR:
            raise ValueError(f"xdist worker {worker} has no port block below {EPHEMERAL_FLOOR}")
        self.offset = offset % BLOCK

    def next(self) -> int:
        for _ in range(BLOCK):
            port = self.start + self.offset
            self.offset = (self.offset + 1) % BLOCK
            if _bindable(port):
                return port
        raise RuntimeError(f"no free port in {self.start}-{self.start + BLOCK - 1}")


def _bindable(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


_allocator: PortAllocator | None = None


def free_port() -> int:
    """A port on 127.0.0.1 that nothing listens on, for a server this test starts later.

    The first port is random within the block, so two concurrent test runs rarely start on the same one."""
    global _allocator
    if _allocator is None:
        _allocator = PortAllocator(os.environ.get("PYTEST_XDIST_WORKER", "master"), offset=random.randrange(BLOCK))
    return _allocator.next()
