"""Thin wrapper around ``subprocess.run`` for experiment orchestration.

Logs the labeled command line (NOT the env), forwards stdout+stderr to
``log_to`` if given, and returns the exit code. Never echoes API keys
(memory ``feedback_secret_hygiene.md``); the caller must scrub ``cmd``
of secrets before passing it in.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def run(
    cmd: list[str],
    *,
    label: str,
    env: dict | None = None,
    cwd: Path | None = None,
    log_to: Path | None = None,
    timeout_s: float | None = None,
    check: bool = False,
) -> int:
    """Run ``cmd`` and return its exit code.

    Stdout+stderr are captured and (if ``log_to`` given) appended to the
    file. The label is logged at INFO level alongside the rc.
    """
    log.info("%s: running %s", label, cmd[0])
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s: timeout after %.1fs", label, timeout_s or 0.0)
        return -1
    if log_to is not None:
        log_to.parent.mkdir(parents=True, exist_ok=True)
        with log_to.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {label} (rc={proc.returncode}) ---\n")
            if proc.stdout:
                fh.write(proc.stdout)
            if proc.stderr:
                fh.write("\n--- stderr ---\n")
                fh.write(proc.stderr)
    log.info("%s: rc=%d", label, proc.returncode)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return proc.returncode


__all__ = ["run"]
