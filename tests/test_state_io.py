"""Process-safe persistence primitives for lineage state."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from pathlib import Path

import pytest

from evolution.lineage.state_io import (
    LockOrderError,
    LockTimeoutError,
    atomic_write_json,
    skill_mutation_lock,
    transaction_lock,
)


def test_transaction_lock_times_out_when_another_process_lock_is_held(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "state.lock"
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    started = time.monotonic()
    try:
        with pytest.raises(LockTimeoutError, match="state.lock"):
            with transaction_lock(lock_path, timeout=0.05):
                pytest.fail("contended lock must not be entered")
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    assert time.monotonic() - started < 1


def test_atomic_write_json_replaces_file_without_leaving_staging_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"generation": 1}\n', encoding="utf-8")

    atomic_write_json(target, {"generation": 2})

    assert target.read_text(encoding="utf-8") == '{\n  "generation": 2\n}\n'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_write_secures_new_files_and_preserves_existing_mode(tmp_path: Path) -> None:
    new_target = tmp_path / "new.json"
    old_umask = os.umask(0o277)
    try:
        atomic_write_json(new_target, {})
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(new_target.stat().st_mode) == 0o400

    new_target.chmod(0o640)
    atomic_write_json(new_target, {"updated": True})
    assert stat.S_IMODE(new_target.stat().st_mode) == 0o640


def test_nested_transaction_locks_reject_inverted_path_order(tmp_path: Path) -> None:
    with transaction_lock(tmp_path / "z.lock"):
        with pytest.raises(LockOrderError, match="canonical path order"):
            with transaction_lock(tmp_path / "a.lock"):
                pytest.fail("inverted lock order must not be entered")


def test_skill_mutation_lock_is_reentrant_and_exclusive(tmp_path: Path) -> None:
    skill_path = tmp_path / "skill"
    with skill_mutation_lock(skill_path):
        with skill_mutation_lock(skill_path):
            pass

    lock_path = skill_path.parent / f".{skill_path.name}.mutation.lock"
    holder_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockTimeoutError):
            with skill_mutation_lock(skill_path):
                pytest.fail("a separately held mutation lease must exclude writers")
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
