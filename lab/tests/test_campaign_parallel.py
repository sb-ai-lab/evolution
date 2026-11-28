"""Lab's native launch must retain parallel policy and real A3 isolation."""
import asyncio
import json
import os
from pathlib import Path
import runpy
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_campaign_example_runs_isolated_instances_and_releases(tmp_path, monkeypatch, concurrency):
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    if not wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL to the installed producing wheel")
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/campaign/offline.py"))
    session, instance = example["Session"], example["Instance"]
    original_turn, original_init, original_close = session.turn, instance.__init__, instance.close
    barrier, lock = Barrier(concurrency), Lock()
    roots, closed, active = [], [], set()
    peaks, turns = [], []

    def init(self, tree):
        original_init(self, tree)
        self.test_root = tree.root
        roots.append(tree.root)

    def close(self):
        closed.append(self.test_root)
        original_close(self)

    def turn(self, message):
        with lock:
            assert self.instance_id not in active
            active.add(self.instance_id)
            peaks.append(len(active))
            turns.append(self.instance_id)
            # Family 14 (/15): core's connection probe sends one lone turn before the campaign; it has no peer.
            probe = len(turns) == 1
        try:
            if not probe:
                barrier.wait(timeout=5)
            return original_turn(self, message)
        finally:
            with lock:
                active.remove(self.instance_id)

    monkeypatch.setattr(instance, "__init__", init)
    monkeypatch.setattr(instance, "close", close)
    monkeypatch.setattr(session, "turn", turn)
    original_report = example["read_campaign_report"]

    def report_after_core_cleanup(*args, **kwargs):
        # Check before the example's final target.close can mask a core leak.
        assert roots and set(closed) == set(roots)
        assert all(not path.exists() for path in roots)
        return original_report(*args, **kwargs)

    monkeypatch.setitem(example["main"].__globals__, "read_campaign_report", report_after_core_cleanup)
    root = tmp_path / "example"
    await asyncio.wait_for(example["main"](SimpleNamespace(
        output=str(root), wheel=[wheel], max_concurrency=concurrency,
        max_scorer_concurrency=1)), timeout=30)
    raw = json.loads((root / "recipe/manifest.json").read_text())
    assert raw["campaign"]["max_concurrency"] == concurrency
    assert raw["campaign"]["max_scorer_concurrency"] == 1
    assert max(peaks) == concurrency
    assert len(roots) >= concurrency and set(closed) == set(roots)
    assert all(not path.exists() for path in roots)
    from evolution_lab.report import read_campaign_report
    report = read_campaign_report(root / "run")
    assert report.data["core"]["complete"]
    assert len(report.data["core"]["rounds"]) == 2
    assert all(row["coverage"]["state"] == "complete" for row in report.data["measurements"])
    with pytest.raises(FileExistsError):
        await example["main"](SimpleNamespace(output=str(root), wheel=[wheel],
            max_concurrency=concurrency, max_scorer_concurrency=1))
