"""A lost artifact ledger is refused before any write (ADR-0008).

The campaign journal declaring a measurement proves the artifact store was born.
A missing or empty `artifacts.jsonl` is then lost evidence, not an unborn store,
and `artifacts/` must never be removed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from evolution.tree.campaign_view import classify_store
from evolution.tree.evolve import EvolveRefused, evolve
from tests.campaign_drivers import _cut_hook, _records, _Resettable
from tests.campaign_fakes import Crash
from tests.family15_fakes import family15_admitted, independent_run
from tests.test_tree_campaign_family15_condition_change import _interrupted
from tests.test_tree_evolve import fake_adapters
from tests.test_tree_family14_public_links import _cli, _fingerprint, _finished, _workspace15


@pytest.mark.parametrize("ledger", ["missing", "empty"])
@pytest.mark.parametrize("writable", [False, True])
def test_a_finished_run_without_its_ledger_is_refused_through_the_cli(
    tmp_path, monkeypatch, writable, ledger
):
    manifest = _workspace15(tmp_path / "ws")
    run = asyncio.run(_finished(manifest, tmp_path / "ws" / "run"))
    assert (run / "artifacts" / "champions" / "v1").is_dir()  # the test must bite
    assert any((run / "artifacts" / "candidates").iterdir())
    if ledger == "missing":
        (run / "artifacts.jsonl").unlink()
    else:
        (run / "artifacts.jsonl").write_bytes(b"")
        # Truncating keeps the product's private mode; recreated with `touch`
        # it would be 0644. Both are refused before any mode check.
        (run / "artifacts.jsonl").chmod(0o644)
    if writable:
        subprocess.run(["chmod", "-R", "u+w", str(run / "artifacts")], check=True)
    before = _fingerprint(run / "artifacts")
    journal = (run / "campaign.jsonl").read_bytes()
    result = _cli(manifest, run, monkeypatch)
    assert result.exit_code == 2, result.output
    assert "refused: artifact_ledger_missing" in result.stderr
    assert "Traceback" not in result.output
    assert _fingerprint(run / "artifacts") == before
    assert (run / "campaign.jsonl").read_bytes() == journal
    assert (ledger == "missing") == (not (run / "artifacts.jsonl").exists())


@pytest.mark.parametrize("writable", [False, True])
async def test_an_unfinished_family15_run_without_its_ledger_is_refused(
    tmp_path, monkeypatch, writable
):
    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_round_closed")
    assert (run / "artifacts" / "champions" / "v1").is_dir()
    (run / "artifacts.jsonl").unlink()
    if writable:
        subprocess.run(["chmod", "-R", "u+w", str(run / "artifacts")], check=True)
    before = _fingerprint(run / "artifacts")
    journal = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as refused:
        await evolve(tmp_path / "manifest.json", run, adapter_factory=lambda *a: adapter)
    assert refused.value.code == "artifact_ledger_missing"
    assert _fingerprint(run / "artifacts") == before
    assert (run / "campaign.jsonl").read_bytes() == journal


def test_a_store_never_born_is_still_recreated_on_family_14(tmp_path, monkeypatch):
    manifest = _workspace15(tmp_path / "ws")
    run = tmp_path / "ws" / "run"
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            asyncio.run(evolve(manifest, run, adapter_factory=fake_adapters()))
    assert [r["event"] for r in _records(run / "campaign.jsonl")] == ["campaign_created"]
    assert classify_store(run) == "absent"
    result = asyncio.run(evolve(manifest, run, adapter_factory=fake_adapters()))
    assert result.terminal == "finished"
    assert classify_store(run) == "born"


def test_a_store_never_born_with_an_empty_ledger_is_recreated_on_family_14(tmp_path, monkeypatch):
    manifest = _workspace15(tmp_path / "ws")
    run = tmp_path / "ws" / "run"
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            asyncio.run(evolve(manifest, run, adapter_factory=fake_adapters()))
    assert [r["event"] for r in _records(run / "campaign.jsonl")] == ["campaign_created"]
    (run / "artifacts.jsonl").write_bytes(b"")
    (run / "artifacts.jsonl").chmod(0o644)
    assert classify_store(run) == "unborn"
    result = asyncio.run(evolve(manifest, run, adapter_factory=fake_adapters()))
    assert result.terminal == "finished"
    assert classify_store(run) == "born"


def test_removing_a_born_store_raises_even_under_python_O(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from evolution.tree.campaign import CampaignInvariantViolated, _Campaign

        root = Path(sys.argv[1])
        artifacts = root / "artifacts"
        artifacts.mkdir()
        evidence = artifacts / "preserved.txt"
        evidence.write_bytes(b"born store evidence")
        snapshot = SimpleNamespace(measurements={"baseline": {}})
        campaign = SimpleNamespace(
            snapshot=lambda: snapshot,
            run_root=root,
            store_resumed=False,
            owed_store_rebases=[],
        )
        try:
            asyncio.run(_Campaign.adopt_siblings(campaign, None))
        except CampaignInvariantViolated as exc:
            if "a born store is never removed" not in str(exc):
                raise SystemExit(f"wrong invariant: {exc}")
        except Exception as exc:
            if not evidence.exists():
                raise SystemExit("born-store evidence was deleted before the invariant guard")
            raise SystemExit(f"born-store removal was not refused: {exc}")
        else:
            raise SystemExit("born-store removal did not raise its invariant")
        if evidence.read_bytes() != b"born store evidence":
            raise SystemExit("born-store evidence changed")
        """
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(result.stdout + result.stderr)


@pytest.mark.parametrize("site", ["deferred", "held"])
def test_a_missing_store_identity_in_the_drive_loop_is_an_invariant_violation(tmp_path, site):
    # Both drive-loop rebase completions name the store identity
    # `adopt` recorded. Its absence is an executor bug that must stop the
    # campaign under Python `-O` too, before any store rebase is attempted.
    script = textwrap.dedent(
        """
        import asyncio
        import sys
        from types import SimpleNamespace
        from evolution.tree.campaign import CampaignInvariantViolated, _Campaign

        site = sys.argv[1]
        reached = []
        campaign = SimpleNamespace(
            error=None,
            fault=None,
            pending=None,
            stopping=False,
            cancelled=lambda: False,
            family_version=15,
            _owed_closure=lambda: False,
            deferred_consequences=[lambda: reached.append("consequence")],
            held_change=(None, None, None, None, None, "continue"),
            held_close=None,
            pending_change=None,
            store_identity=None,
            _write_condition_change=lambda: reached.append("condition_change"),
            _repair_environment_docs=lambda: reached.append("docs"),
            _complete_owed_store_rebases=lambda identity: reached.append(("rebase", identity)),
        )
        try:
            if site == "deferred":
                asyncio.run(_Campaign.drive(campaign))
            else:
                _Campaign._release_held_change(campaign)
        except CampaignInvariantViolated as exc:
            if "store identity" not in str(exc):
                raise SystemExit(f"wrong invariant: {exc}")
        except Exception as exc:
            raise SystemExit(f"a missing store identity was not refused: {exc!r} after {reached}")
        else:
            raise SystemExit(f"a missing store identity did not raise its invariant: {reached}")
        if any(isinstance(step, tuple) for step in reached):
            raise SystemExit(f"a store rebase was attempted without its identity: {reached}")
        if site == "held" and (reached or campaign.held_change is None):
            raise SystemExit(f"the held decision was consumed: {reached}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, site],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(result.stdout + result.stderr)


async def test_a_store_never_born_is_still_recreated_on_family_15(tmp_path, monkeypatch):
    adapter = _Resettable()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            await independent_run(tmp_path, patch, adapter=adapter)
    family15_admitted(monkeypatch)
    run = tmp_path / "run"
    assert [r["event"] for r in _records(run / "campaign.jsonl")] == ["campaign_created"]
    # Unborn, not just absent. 0644, not the product's private 0600: an
    # unborn store is removed before its ledger is ever read.
    (run / "artifacts.jsonl").write_bytes(b"")
    (run / "artifacts.jsonl").chmod(0o644)
    assert classify_store(run) == "unborn"
    result = await evolve(tmp_path / "manifest.json", run, adapter_factory=lambda *a: adapter)
    assert result.terminal == "finished"
    assert classify_store(run) == "born"
