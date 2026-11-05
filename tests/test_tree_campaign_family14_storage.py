"""ADR-0040 D7 and D8 in a real family-14 campaign: trace storage faults and the inode floor."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import NoReturn

import pytest

from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.campaign import (
    CAMPAIGN_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    _Campaign,
    _open_round,
)
from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for, reduce
from evolution.tree.execution import scorer_traces
from evolution.tree.storage.append_log import inspect_log
from evolution.tree.storage.inode_floor import count_tree
from tests.family14_public import family14_public_run
from tests.test_tree_campaign_family14_run import family14_run

pytestmark = pytest.mark.slow


def _enospc(*_args: object) -> NoReturn:
    raise OSError(errno.ENOSPC, "No space left on device")


def _few_inodes(free: int):  # type: ignore[no-untyped-def]
    real = os.statvfs

    def statvfs(path):  # type: ignore[no-untyped-def]
        stats = real(path)
        return os.statvfs_result((*stats[:5], max(stats.f_files, 10**6), free, free, *stats[8:]))

    return statvfs


def _events(run: Path) -> list[str]:
    return [json.loads(line)["event"] for line in (run / "campaign.jsonl").read_text().splitlines()]


def _measurement_events(run: Path) -> list[str]:
    return [
        json.loads(line)["event"]
        for journal in sorted((run / "measurements").glob("*/events.jsonl"))
        for line in journal.read_text().splitlines()
    ]


async def test_resume_inode_floor_reads_open_round_before_writer_log_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable open-round Candidate is held even when resume has no writer log yet."""

    class _PowerCut(Exception):
        pass

    real_hook = _Campaign.hook

    def cut_after_close(self, name: str) -> None:
        if name == "after_attempt_closed":
            raise _PowerCut
        real_hook(self, name)

    monkeypatch.setattr(_Campaign, "hook", cut_after_close)
    with pytest.raises(_PowerCut):
        await family14_run(tmp_path, monkeypatch, campaign={"rounds": 1})

    run = tmp_path / "run"
    observed = inspect_log(
        run,
        file_name=CAMPAIGN_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=14),
        envelope_family=campaign_family_for(14),
    )
    snapshot = reduce(observed.records, selector=None, family_version=14)
    open_round = _open_round(snapshot)
    assert open_round is not None
    attempts = set(snapshot.rounds[open_round].attempts)

    lineage = ArtifactStore.inspect(run)
    champion = lineage.champion
    assert champion is not None
    trees = [count_tree(run / champion.reference.locator)]
    candidates = [
        candidate
        for candidate in lineage.candidates
        if candidate.state == "sealed"
        and candidate.attempt_id in attempts
        and candidate.reference is not None
    ]
    assert candidates
    trees.extend(count_tree(run / candidate.reference.locator) for candidate in candidates)

    # Resume calls inode_floor before open_log; this object models that exact
    # lifecycle point while retaining the real durable journal and artifacts.
    holder = object.__new__(_Campaign)
    holder.run_root = run
    holder._family_version = 14
    holder.log = None
    assert _Campaign._held_tree_inodes(holder) == max(trees)


async def test_a_trace_write_fault_resumes_despite_a_low_inode_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Every trace write fails with ENOSPC: a storage fault, never a scorer failure.
    with monkeypatch.context() as patch:
        patch.setattr(scorer_traces, "_raw_write", _enospc)
        run, faulted = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 1})
    assert faulted.terminal == "storage_capacity_exhausted"
    assert faulted.code == "storage_capacity_exhausted"
    assert faulted.fault_detail is not None
    assert "ENOSPC (write)" in faulted.fault_detail
    assert "scoring_failed" not in _measurement_events(run)

    # A forecast alone cannot recreate the real write-fault terminal.
    with monkeypatch.context() as patch:
        patch.setattr(os, "statvfs", _few_inodes(50))
        _run, done = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 1})
    assert done.terminal == "finished"
    assert "scoring_finished" in _measurement_events(run)


async def test_a_fresh_run_below_its_inode_floor_continues_measuring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", _few_inodes(50))
    run, result = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 1})
    assert result.terminal == "finished"
    assert "capacity_exhausted" not in _events(run)
    assert "scoring_finished" in _measurement_events(run)


@pytest.mark.parametrize(
    ("stability", "round_stop", "refresh"),
    [
        ("stable", "first_accepted", 0),
        ("stable", "all_attempts", 1),
        ("unstable", "first_accepted", 1),
        ("unstable", "all_attempts", 0),
    ],
)
async def test_a_round_binds_no_more_measurements_than_the_floor_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stability: str, round_stop: str, refresh: int
) -> None:
    from evolution.tree.storage.inode_floor import CampaignTerms, measurements_per_round
    from tests.test_tree_campaign_family14_run import family14_run

    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 2},
        family14_settings={"stability": stability, "round_stop": round_stop, "refresh": refresh},
    )
    assert snapshot.finished is not None
    identity = json.loads((run / "campaign.jsonl").read_text().splitlines()[0])["payload"]
    policy = identity["identity"]["policy"]
    terms = CampaignTerms(
        attempts=policy["attempts_per_round"],
        parallel=policy["parallel_attempts"],
        rounds=policy["rounds"],
        cases=2,
        repeats=policy["evaluation"]["repeats_per_question"],
        fresh_parent=policy["gate"]["fresh_parent"],
        refresh=policy["baseline_refresh_after_rounds"] > 0,
        stability=policy["gate"]["stability"],
        round_stop=policy["round_stop"],
        probe_budget=None,
        growth=0,
        shared_tmp=False,
        scorer_concurrency=policy["max_scorer_concurrency"],
    )
    bound = [name for name in os.listdir(run / "measurements") if not name.startswith("m-r00")]
    for round_id in (1, 2):
        in_round = [name for name in bound if name.startswith(f"m-r{round_id:02d}")]
        assert len(in_round) <= measurements_per_round(terms), in_round
