"""Run explicitly with pytest at c405ce1e1 only; never regenerate on the branch.

Copy this generator and tests/family15_b14_scenario.py into the detached base
checkout, then invoke pytest on this file. All inputs come from offline fakes.
The seed freezes host-specific /15 setup facts; the test re-emits every /16
record through this release's actual envelope grammar and campaign lifecycle.
"""

import hashlib
import json
import subprocess
from pathlib import Path

from tests.campaign_drivers import observe_barrier  # noqa: F401
from tests.family15_b14_scenario import BASE, single_journal


def test_generate(observe_barrier):  # noqa: F811
    root = Path(__file__).resolve().parents[3]
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert revision == BASE, "STOP: journal pin must be generated on the immutable b14 base"
    raw = single_journal(observe_barrier)
    Path(__file__).with_name("seed.json").write_text(json.dumps(observe_barrier, indent=2) + "\n")
    print(
        f"base={revision} records={len(raw.splitlines())} bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"
    )
