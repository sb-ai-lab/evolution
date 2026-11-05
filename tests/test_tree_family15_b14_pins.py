"""Spec §9: omitted stages preserves deterministic b14 Single journal bytes.

Computed at c405ce1e1 in an isolated base checkout, never on this branch.
The frozen seed contains offline fake facts only. Do not regenerate a mismatch.
"""

import hashlib
import json
from pathlib import Path

from tests.family15_b14_scenario import single_journal

EXPECTED_SHA256 = "d799eb1f683247ce1a63e4246c7c654020e0f733ad92cecc9ee4bcab6048e354"


def test_single_journal_bytes_match_b14():
    fixture = Path(__file__).parent / "fixtures" / "family15_b14" / "seed.json"
    raw = single_journal(json.loads(fixture.read_text()))
    assert len(raw.splitlines()) == 42
    assert len(raw) == 34460
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256, (
        "STOP: b14 Single byte identity changed"
    )
