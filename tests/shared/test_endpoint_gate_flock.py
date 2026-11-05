"""Cross-process bound test for the local endpoint gate.

The gate must bound aggregate concurrency on a given ``api_base`` across
SEPARATE OS PROCESSES (parallel anchor workers run as distinct
``framework_produce_valid.py`` / eval processes). A process-local
``threading.BoundedSemaphore`` does NOT do this — each process gets its
own full permit pool — so this test FAILS on the old implementation and
PASSES once the gate is backed by ``fcntl.flock``.

Cross-process accounting is proved with a SHARED COUNTER FILE protected by
its OWN ``fcntl.flock`` (independent of the gate under test): each worker,
while holding a gate permit, atomically bumps a "currently inside" counter
and records the running maximum. If the gate truly bounds concurrency at
``permits`` across processes, the recorded max never exceeds ``permits``.
The dummy ``api_base`` is local-looking but never dialed — pure lock-file
accounting, no GPU.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "experiments"


def _worker(api_base: str, permits: int, counter_path: str, hold_s: float, lock_root: str) -> None:
    """Run in a fresh process: acquire a gate slot, bump shared counter."""
    import fcntl
    import os

    # Spawn child re-imports local_endpoint_gate fresh; set the lock-root
    # env BEFORE that import so the gate's slot files land under the
    # test's tmp_path, never the real shared /tmp/evo_endpoint_gate.
    os.environ["EVO_ENDPOINT_GATE_LOCK_ROOT"] = lock_root

    sys.path.insert(0, str(EXPERIMENTS))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from _shared.local_endpoint_gate import gate_for

    gate = gate_for(api_base, permits=permits)
    with gate.permit():
        # Atomically update the shared counter file under its own flock.
        with open(counter_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                cur, mx = (int(x) for x in fh.read().split())
                cur += 1
                mx = max(mx, cur)
                fh.seek(0)
                fh.truncate()
                fh.write(f"{cur} {mx}")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        time.sleep(hold_s)

        with open(counter_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                cur, mx = (int(x) for x in fh.read().split())
                cur -= 1
                fh.seek(0)
                fh.truncate()
                fh.write(f"{cur} {mx}")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@pytest.mark.parametrize("permits", [1])
def test_gate_bounds_concurrency_across_processes(tmp_path: Path, permits: int) -> None:
    """Two SEPARATE processes sharing one api_base must not exceed permits.

    Uses spawn so each child is a fresh interpreter (no inherited
    in-process semaphore state) — proving the bound is enforced at the
    OS level, not via shared Python objects.
    """
    # Local-looking but never dialed; unique per run to isolate slot files.
    api_base = f"http://localhost:39999/v1#{tmp_path.name}"
    counter = tmp_path / "counter.txt"
    counter.write_text("0 0", encoding="utf-8")

    # Hermetic: relocate the gate's cross-process lock root under tmp_path
    # so the test never touches the real shared /tmp/evo_endpoint_gate.
    lock_root = tmp_path / "gate_lock_root"
    real_default_root = Path(tempfile.gettempdir()) / "evo_endpoint_gate"
    real_root_existed_before = real_default_root.exists()

    ctx = mp.get_context("spawn")
    hold_s = 0.6
    procs = [
        ctx.Process(
            target=_worker,
            args=(api_base, permits, str(counter), hold_s, str(lock_root)),
        )
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, f"worker crashed (exitcode={p.exitcode})"

    # The relocated root must have been used (proves the env seam took
    # effect in the spawn child), and the real shared root must be
    # untouched by this test.
    assert lock_root.exists(), "gate did not use the relocated lock root"
    assert any(lock_root.iterdir()), "no slot files under the relocated root"
    if not real_root_existed_before:
        assert not real_default_root.exists(), (
            f"test created the real shared lock root {real_default_root} (hermeticity broken)"
        )

    cur, mx = (int(x) for x in counter.read_text(encoding="utf-8").split())
    assert cur == 0, f"counter not balanced (leak/imbalance): cur={cur}"
    # The crux: process-local BoundedSemaphore lets all 4 in at once
    # (mx == 4) and FAILS here; the flock-backed gate caps mx at `permits`.
    assert mx <= permits, (
        f"observed {mx} concurrent holders across processes, "
        f"expected <= {permits} (gate not bounding cross-process)"
    )


def test_public_api_and_aliases_intact() -> None:
    """Public surface + legacy aliases + old-path shim must keep working."""
    sys.path.insert(0, str(EXPERIMENTS))
    from _shared import local_endpoint_gate as new_mod
    from _shared import local_qwen_gate as old_mod

    for name in (
        "gate_for",
        "is_local_endpoint",
        "local_endpoint_gate",
        "is_local_qwen",
        "qwen_gate",
    ):
        assert name in new_mod.__all__, f"{name} missing from __all__"
        assert hasattr(new_mod, name)
        assert hasattr(old_mod, name), f"old-path shim missing {name}"

    assert new_mod.is_local_qwen is new_mod.is_local_endpoint
    assert old_mod.gate_for is new_mod.gate_for
    assert new_mod.is_local_endpoint("http://localhost:30004/v1") is True
    assert new_mod.is_local_endpoint("https://openrouter.ai/api/v1") is False

    g = new_mod.gate_for("http://localhost:30004/v1")
    with g.permit():
        pass  # contextmanager protocol intact


def test_acquire_slot_reraises_unexpected_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-EWOULDBLOCK flock OSError must propagate, not infinite-spin.

    EWOULDBLOCK/EAGAIN == "slot busy" -> keep scanning. Any other errno
    (here ENOLCK) is pathological and must crash loudly rather than be
    swallowed into the unbounded poll loop (Task 12 is unattended).
    """
    import errno
    import importlib

    monkeypatch.setenv("EVO_ENDPOINT_GATE_LOCK_ROOT", str(tmp_path / "lr"))
    sys.path.insert(0, str(EXPERIMENTS))
    from _shared import local_endpoint_gate as mod

    # Reload so _LOCK_ROOT picks up the relocated (tmp_path) root. Reload
    # again in finally with the env unset so the cached module is restored
    # to the real default root for any later test in this process.
    mod = importlib.reload(mod)
    try:
        gate = mod.gate_for("http://localhost:39998/v1#reraise", permits=1)

        calls = {"n": 0}

        def _boom(fd: int, op: int) -> None:
            calls["n"] += 1
            raise OSError(errno.ENOLCK, "no locks available")

        monkeypatch.setattr(mod.fcntl, "flock", _boom)
        with pytest.raises(OSError) as ei:
            gate._acquire_slot()
        assert ei.value.errno == errno.ENOLCK
        # Re-raised on the FIRST flock call: no infinite-spin / swallow.
        assert calls["n"] == 1
    finally:
        monkeypatch.delenv("EVO_ENDPOINT_GATE_LOCK_ROOT", raising=False)
        importlib.reload(mod)
