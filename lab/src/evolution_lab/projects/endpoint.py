"""Where the service listens: ``<Lab home>/service.sock`` unless ``--socket`` is given (W49).

One writer per home (ADR-0017 p. 2) makes the path unambiguous; the home is 0700, the
private parent ADR-0007 requires. A path that does not fit ``sun_path`` is refused with a
hint; another path is never chosen silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

from .home import default_home

SOCKET_NAME = "service.sock"
def sun_path_max(platform=None) -> int:
    platform = sys.platform if platform is None else platform
    return 104 if platform == 'darwin' else 108


def too_long_hint(platform=None) -> str:
    return (f"the service socket path is longer than the Unix socket limit ({sun_path_max(platform) - 1} bytes); "
            "pass --socket with a shorter path")


def default_socket(home: Path) -> Path:
    return Path(home) / SOCKET_NAME


def socket_fits(path: Path, platform=None) -> bool:
    return len(os.fsencode(str(path))) < sun_path_max(platform)


def resolve_socket(socket: str | None, home: str | None, environ: Mapping[str, str] = os.environ) -> Path:
    """Explicit ``--socket`` wins; else ``--home``; else the default home. ValueError when the
    home-derived path does not fit (an explicit ``--socket`` is checked by its consumer)."""
    if socket is not None:
        return Path(socket)
    path = default_socket(Path(home) if home is not None else default_home(environ))
    if not socket_fits(path):
        raise ValueError(too_long_hint())
    return path
