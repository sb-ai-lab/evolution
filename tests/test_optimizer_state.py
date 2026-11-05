from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from evolution.evolve.optimizer_state import checkpoint, optimizer_lease, read_state
from evolution.lineage.state_io import LockTimeoutError


def test_optimizer_lease_is_global_per_skill(tmp_path: Path) -> None:
    lock = tmp_path.parent / f".{tmp_path.name}.mutation.lock"
    holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockTimeoutError):
            with optimizer_lease(tmp_path, timeout=0.01):
                pytest.fail("contended optimizer lease must not be entered")
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_checkpoint_atomically_updates_phase(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    state = {"status": "running"}

    checkpoint(path, state, "validation")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["phase"] == "validation"
    assert stored["updated_at"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("payload", ["{", "{}", "[]"])
def test_read_state_rejects_corrupt_or_incomplete_json(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "run.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Optimizer state"):
        read_state(path)
