"""Process durability policy for Lab; independent of Core."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

Durability = Literal["strict", "relaxed"]

DURABILITY_ENV = "EVOLUTION_DURABILITY"


class DurabilityConfigError(ValueError):
    """Raised for an unsupported process durability setting."""


def effective(environ: Mapping[str, str] | None = None) -> Literal["relaxed", "strict"]:
    value = (os.environ if environ is None else environ).get(DURABILITY_ENV, "")
    if value in ("", "relaxed"):
        return "relaxed"
    if value == "strict":
        return "strict"
    raise DurabilityConfigError(f'{DURABILITY_ENV} must be "relaxed" or "strict" (got "{value}")')


def check() -> None:
    effective()


def sync_file(fd: int) -> None:
    """Sync a file only when the process requests strict durability."""
    if effective() == "strict":
        os.fsync(fd)


def sync_directory(path: Path) -> None:
    """Sync directory entries under strict durability, without opening in relaxed."""
    if effective() == "relaxed":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
