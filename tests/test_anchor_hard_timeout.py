"""R2: run_publication._run hard wall-clock kill of a wedged per-anchor subprocess.

Regression guard for the xlsx incident: the per-anchor produce subprocess had
no wall-clock bound, so an in-thread wedge froze the anchor forever. _run with
hard_timeout_s must SIGKILL the whole process group and raise within bound,
while leaving normal/non-zero behavior unchanged when the bound is unset.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(
    0, os.path.join(os.getcwd(), "experiments", "table3-4-minimal-sbbench-v2", "runner")
)
import run_publication as rp  # noqa: E402

ENV = dict(os.environ)


def _run(cmd, **kw):
    return rp._run(cmd, env=ENV, label="test", use_budget_guard=False, **kw)


def test_hard_timeout_kills_wedged_group_within_bound() -> None:
    t0 = time.time()
    with pytest.raises(RuntimeError, match="hard wall-clock timeout"):
        # backgrounded grandchild + foreground sleep: the t34v7 wedge shape
        _run(
            ["bash", "-c", 'sleep 600 & python3 -c "import time;time.sleep(600)"'], hard_timeout_s=3
        )
    assert time.time() - t0 < 60, "hard timeout not enforced"


def test_no_timeout_when_unset_success() -> None:
    assert _run(["bash", "-c", "exit 0"], hard_timeout_s=0) is None


def test_nonzero_exit_still_raises_with_bound() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _run(["bash", "-c", "exit 7"], hard_timeout_s=30)


def test_fast_command_under_bound_ok() -> None:
    assert _run(["bash", "-c", "echo hi; exit 0"], hard_timeout_s=30) is None
