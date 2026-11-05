"""Centralized path resolution for the comparison-framework adapters.

Each adapter shells out to a third-party tool (``evo``, ``evoskill``,
``memento``) or imports a third-party package from a checkout that is not
part of the OSS distribution. To keep the
adapters portable, every external location is resolved here from
environment variables.

Environment variables
---------------------

============================  ===================================================
EVO_BIN                       Path to the ``evo`` CLI for self-driving
                              Evolution adapter (defaults to ``evo`` on PATH).
EVOSKILL_BIN                  Path to the ``evoskill`` CLI.
MEMENTO_BIN                   Path to the ``memento`` CLI.
EVO_PYTHON                    Python interpreter to embed inside generated
                              shell scripts (defaults to ``sys.executable``).
EVO_TASKS_ROOT                Tasks directory (defaults to ``<repo>/tasks``).
EVOSKILL_ROOT                 Filesystem path to the EvoSkill checkout
                              (added to ``PYTHONPATH`` when invoking
                              ``evoskill run``).
============================  ===================================================

Calling ``require(name)`` raises a clear ``RuntimeError`` if the
environment variable is unset, instead of failing later with a confusing
``FileNotFoundError`` when the binary is invoked.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    return default


def require(env_var: str, hint: str = "") -> str:
    """Return ``os.environ[env_var]`` or raise a clear RuntimeError.

    ``hint`` should explain how to populate the variable (a path, a
    package install, etc.).
    """
    val = os.environ.get(env_var)
    if not val:
        msg = f"Adapter requires environment variable {env_var}."
        if hint:
            msg += " " + hint
        raise RuntimeError(msg)
    return val


def evo_bin() -> str:
    sib = Path(sys.executable).parent / "evo"
    return (
        _env("EVO_BIN")
        or shutil.which("evo")
        or (str(sib) if sib.is_file() and os.access(sib, os.X_OK) else "")
        or "evo"
    )


def evoskill_bin() -> str:
    return require(
        "EVOSKILL_BIN",
        "Set it to the EvoSkill CLI, e.g. /opt/evoskill/bin/evoskill.",
    )


def memento_bin() -> str:
    sib = Path(sys.executable).parent / "memento"
    return (
        _env("MEMENTO_BIN")
        or shutil.which("memento")
        or (str(sib) if sib.is_file() and os.access(sib, os.X_OK) else "")
        or require(
            "MEMENTO_BIN",
            "Set it to the Memento CLI, e.g. /opt/memento/bin/memento.",
        )
    )


def python_bin() -> str:
    return _env("EVO_PYTHON") or sys.executable


def tasks_root() -> str:
    return _env("EVO_TASKS_ROOT") or str(REPO_ROOT / "tasks")


def evoskill_root() -> str:
    return require(
        "EVOSKILL_ROOT",
        "Set it to the EvoSkill repo checkout (used as PYTHONPATH for the harness).",
    )
