"""ADR-0040 D7/D8 for a standalone evaluation: its inode floors and its trace faults."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import NoReturn

import pytest

from evolution.tree import evaluation as evaluation_module
from evolution.tree.evaluation import evaluate
from evolution.tree.execution import scorer_traces
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_train_test import evaluation_manifest

pytestmark = pytest.mark.slow


def _entries(root: Path) -> int:
    return sum(1 for _ in root.rglob("*"))


def _few_inodes(free: int):  # type: ignore[no-untyped-def]
    real = os.statvfs

    def statvfs(path):  # type: ignore[no-untyped-def]
        stats = real(path)
        return os.statvfs_result((*stats[:5], max(stats.f_files, 10**6), free, free, *stats[8:]))

    return statvfs


async def _evaluate(path: Path, run: Path):  # type: ignore[no-untyped-def]
    return await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())


@pytest.mark.parametrize("stop", ["ledger_born", "v0_published", "measurement_born"])
async def test_a_resumed_evaluation_writes_no_more_than_its_resume_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    floors: list[int] = []
    real_need = evaluation_module.evaluation_resume_need

    def recording(*args, **kwargs):  # type: ignore[no-untyped-def]
        floors.append(real_need(*args, **kwargs))
        return floors[-1]

    from evolution.tree.artifacts.artifacts import ArtifactStore
    from evolution.tree.campaign_measurement import Measurement

    def crash(*args, **kwargs) -> NoReturn:  # type: ignore[no-untyped-def]
        raise RuntimeError("stopped")

    owner, hook = {
        "ledger_born": (ArtifactStore, "_make_layout"),
        "v0_published": (Measurement, "create"),
        "measurement_born": (Measurement, "materialize"),
    }[stop]
    monkeypatch.setattr(owner, hook, crash)
    with pytest.raises(RuntimeError, match="stopped"):
        await _evaluate(path, run)
    monkeypatch.undo()
    monkeypatch.setattr(evaluation_module, "evaluation_resume_need", recording)
    before = _entries(run)

    result = await _evaluate(path, run)

    assert result["state"] == "completed"
    (floor,) = floors
    assert _entries(run) - before <= floor


async def test_an_evaluation_below_its_floor_warns_once_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    said = []

    def note(message):
        if "inodes" in message:
            assert not (run / "artifacts/artifacts.jsonl").exists()
            said.append(message)

    monkeypatch.setattr(os, "statvfs", _few_inodes(5))
    result = await evaluate(path, run, notes=note, adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    assert len(said) == 1 and "5" in said[0] and "not block" in said[0]

    def no_estimate(*args, **kwargs):
        pytest.fail("completed evaluation must not estimate")

    monkeypatch.setattr(evaluation_module, "_snapshot", no_estimate)
    before = {str(p.relative_to(run)): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    again = await _evaluate(path, run)
    assert again == result
    assert before == {
        str(p.relative_to(run)): p.read_bytes() for p in run.rglob("*") if p.is_file()
    }


def _enospc(*_args: object) -> NoReturn:
    raise OSError(errno.ENOSPC, "No space left on device")


async def test_a_trace_fault_stops_the_evaluation_and_the_same_invocation_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    with monkeypatch.context() as patch:
        patch.setattr(scorer_traces, "_raw_write", _enospc)
        with pytest.raises(scorer_traces.ScorerTraceStorageFault) as fault:
            await _evaluate(path, run)
    assert fault.value.code == "storage_capacity_exhausted"
    assert "scorer-traces/evaluation.pack" in fault.value.detail

    result = await _evaluate(path, run)

    assert result["state"] == "completed"
