"""The CLI must be runnable as a module, not only via the `evo` console script.

Regression: `python -m evolution.cli` previously failed (no `__main__`), and
`python -m evolution.cli.main` produced zero output, so the no-install path
documented for offline use did not work.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args, "--help"],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(SRC), "PATH": ""},
        capture_output=True,
        text=True,
    )


def test_python_m_evolution_cli_runs() -> None:
    result = _run(["evolution.cli"])
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout


def test_python_m_evolution_cli_main_runs() -> None:
    result = _run(["evolution.cli.main"])
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
