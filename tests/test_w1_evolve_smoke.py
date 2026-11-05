"""The W1 end-to-end smoke, run the way an operator runs it.

`scripts/w1_evolve_smoke.py` starts the packaged reference target and three
`evo tree evolve` processes of its own, so this test's whole job is to run the
script as a shell would and hold it to its printed contract: every check on its
own line, `ok` last, exit 0. A failure inside the script prints the offending
check on stderr, which is why both streams are attached to the assertion.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "w1_evolve_smoke.py"
SRC = ROOT / "src"


def test_w1_evolve_smoke_runs_end_to_end() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=180,
    )
    report = (
        f"exit {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert completed.returncode == 0, report
    lines = completed.stdout.splitlines()
    assert lines[-1] == "ok", report
    # The eleven checks the script's docstring promises, each on its own line.
    assert [line.split(".", 1)[0] for line in lines[:-1]] == [str(n) for n in range(1, 12)], report
