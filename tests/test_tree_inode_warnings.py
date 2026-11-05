"""Advisory capacity output cannot veto work or conceal actual write failures."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.tree import campaign, evaluation
from evolution.tree.artifacts import artifacts
from evolution.tree.artifacts.artifact_errors import ArtifactAdmissionRefused
from evolution.tree.artifacts.artifact_policy import scan_tree, tree_inodes
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.cli import tree_app
from evolution.tree.storage.inode_floor import InodeNeed
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_evaluation_inodes import _few_inodes
from tests.test_tree_family14_public_commands import scaffold15 as scaffold15
from tests.test_tree_train_test import evaluation_manifest
from tests.tree.artifacts.test_artifact_policy import seed
from tests.tree.artifacts.test_artifact_store import POLICY


@pytest.mark.parametrize("command", ["evolve", "evaluate"])
@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
@pytest.mark.parametrize("progress", [False, True])
def test_cli_warns_before_failed_v0_copy_and_logs_it(
    tmp_path, monkeypatch, scaffold15, command, error, progress
):
    import typer

    manifest = scaffold15 if command == "evolve" else evaluation_manifest(tmp_path / "evaluation")
    root = manifest.parent / "run"
    emitted = []
    real_echo = typer.echo

    def echo(message=None, *args, **kwargs):
        emitted.append(str(message))
        return real_echo(message, *args, **kwargs)

    def failed_copy(*args, **kwargs):
        warnings = [line for line in emitted if "free inodes" in line]
        assert len(warnings) == 1, emitted
        assert "not block" in warnings[0] and "external programs" in warnings[0]
        raise OSError(error, "injected v0 write fault")

    monkeypatch.setattr(typer, "echo", echo)
    monkeypatch.setattr(os, "statvfs", _few_inodes(0))
    monkeypatch.setattr(artifacts, "stage_copy", failed_copy)
    invocation = CliRunner().invoke(
        tree_app,
        [
            command,
            "--manifest",
            str(manifest),
            "--run-dir",
            str(root),
            *(["--progress"] if progress else []),
        ],
    )
    assert invocation.exit_code != 0, invocation.output
    assert invocation.stderr.count("free inodes") == 1
    ledger = root / ("artifacts.jsonl" if command == "evolve" else "artifacts/artifacts.jsonl")
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert records[-1]["event"] == "artifact_copy_failed"
    expected = "io_failure" if error == errno.EIO else "storage_capacity_exhausted"
    assert records[-1]["payload"]["code"] == expected
    logs = list((root / "logs").glob(f"{command}-*.log"))
    assert len(logs) == 1 and "free inodes" in logs[0].read_text()


@pytest.mark.parametrize("sink", ["missing", "raising"])
async def test_evaluation_warning_sink_cannot_veto_work(tmp_path, monkeypatch, sink):
    path = evaluation_manifest(tmp_path)

    def raising(message):
        if "inodes" in message:
            raise RuntimeError("broken inode output")

    monkeypatch.setattr(os, "statvfs", _few_inodes(0))
    result = await evaluation.evaluate(
        path,
        tmp_path / "run",
        notes=None if sink == "missing" else raising,
        adapter_factory=lambda *args: FakeAdapter(),
    )
    assert result["state"] == "completed"


def test_identity_mismatch_precedes_estimate_observer(tmp_path):
    source = seed(tmp_path)
    scan = scan_tree(source, POLICY)
    identity = ArtifactStoreIdentityV1(POLICY, "0" * 64, scan.dependency_digest)
    seen = []
    with pytest.raises(ArtifactAdmissionRefused, match="run_identity_changed"):
        ArtifactStore.create(
            tmp_path / "run",
            source,
            POLICY,
            parallel_attempts=1,
            expected=identity,
            _estimate_observer=seen.append,
        )
    assert seen == [] and not (tmp_path / "run").exists()


@pytest.mark.parametrize("free_delta", [-1, 0, 1])
@pytest.mark.parametrize("resume", [False, True])
async def test_evaluation_boundary_notes_do_not_change_dispatch(
    tmp_path, monkeypatch, free_delta, resume
):
    path = evaluation_manifest(tmp_path)
    root = tmp_path / "run"
    if resume:
        real_layout = ArtifactStore._make_layout

        def cut(self):
            raise RuntimeError("cut before layout")

        monkeypatch.setattr(ArtifactStore, "_make_layout", cut)
        with pytest.raises(RuntimeError, match="cut before layout"):
            await evaluation.evaluate(path, root, adapter_factory=lambda *args: FakeAdapter())
        monkeypatch.setattr(ArtifactStore, "_make_layout", real_layout)
    observed = []
    real_need = evaluation.evaluation_resume_need if resume else evaluation.evaluation_need

    def need(*args, **kwargs):
        floor = real_need(*args, **kwargs)
        observed.append(floor)
        return floor

    monkeypatch.setattr(evaluation, "evaluation_resume_need" if resume else "evaluation_need", need)
    # The floor is computed after statvfs at creation, so calculate it from real inputs first.
    manifest = evaluation.load_evaluation_manifest(path)
    source = Path(manifest.snapshot["root"])
    tree = tree_inodes(scan_tree(source, manifest.artifacts))
    if resume:
        floor = real_need(
            root,
            tree=tree,
            unscored=1,
            scorer_concurrency=manifest.policy.max_scorer_concurrency,
            shared_tmp=True,
        )
    else:
        floor = real_need(
            tree=tree,
            samples=1,
            scorer_concurrency=manifest.policy.max_scorer_concurrency,
            shared_tmp=True,
        )
    monkeypatch.setattr(evaluation, "shares_tmp", lambda path: True)
    monkeypatch.setattr(os, "statvfs", _few_inodes(floor + free_delta))
    said = []
    result = await evaluation.evaluate(
        path, root, notes=said.append, adapter_factory=lambda *args: FakeAdapter()
    )
    assert result["state"] == "completed"
    assert observed == [floor]
    warnings = [message for message in said if "free inodes" in message]
    assert len(warnings) == int(free_delta < 0)


@pytest.mark.parametrize("progress", [False, True])
def test_evaluation_cli_notes_are_immediate_and_redacted(tmp_path, monkeypatch, progress):
    import typer

    secret = "capacity-secret-value"
    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["scorer"]["allowed_env"] = ["CAPACITY_TEST_SECRET"]
    path.write_text(json.dumps(document))
    monkeypatch.setenv("CAPACITY_TEST_SECRET", secret)
    emitted = []
    real_echo = typer.echo

    def echo(message=None, *args, **kwargs):
        emitted.append(str(message))
        return real_echo(message, *args, **kwargs)

    async def engine(*args, **kwargs):
        kwargs["notes"]("early note " + secret)
        assert emitted == ["early note [REDACTED]"]
        return {"state": "completed"}

    monkeypatch.setattr(typer, "echo", echo)
    monkeypatch.setattr(evaluation, "evaluate", engine)
    invocation = CliRunner().invoke(
        tree_app,
        [
            "evaluate",
            "--manifest",
            str(path),
            "--run-dir",
            str(tmp_path / "run"),
            *(["--progress"] if progress else []),
        ],
    )
    assert invocation.exit_code == 0, invocation.exception
    assert secret not in invocation.stderr


@pytest.mark.parametrize("free_delta", [-1, 0, 1])
@pytest.mark.parametrize("resume", [False, True])
async def test_campaign_boundary_notes_continue_and_finished_reopen_claims_no_work(
    tmp_path, monkeypatch, free_delta, resume
):
    from tests import campaign_drivers as fixtures
    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_storage import _enospc, _measurement_events

    if resume:
        with monkeypatch.context() as patch:
            patch.setattr("evolution.tree.execution.scorer_traces._raw_write", _enospc)
            _, faulted = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 2})
        assert faulted.terminal == "storage_capacity_exhausted"
    said = []
    real_evolve = fixtures.evolve

    async def with_notes(*args, **kwargs):
        return await real_evolve(*args, **kwargs, notes=said.append)

    real_floor = campaign._Campaign.inode_floor

    def floor(self, *, fresh):
        need = real_floor(self, fresh=fresh)
        assert need is not None
        actual = need(tree_inodes(scan_tree(self.seed_root, self.artifact_policy)))
        monkeypatch.setattr(os, "statvfs", _few_inodes(actual.floor + free_delta))
        return need

    monkeypatch.setattr(fixtures, "evolve", with_notes)
    monkeypatch.setattr(campaign._Campaign, "inode_floor", floor)
    root, result = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    assert result.terminal == "finished"
    assert "scoring_finished" in _measurement_events(root)
    warnings = [line for line in said if "free inodes" in line]
    assert len(warnings) == 1  # the whole-run note when at/above the next-work floor
    assert ("not block" in warnings[0]) == (free_delta < 0)
    before = (root / "campaign.jsonl").read_bytes()
    said.clear()
    _, reopened = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    assert reopened.terminal == "finished"
    assert (root / "campaign.jsonl").read_bytes() == before
    warning = [line for line in said if "free inodes" in line]
    assert len(warning) == 1
    if free_delta < 0:
        assert "requested next work" in warning[0]
        assert "will" not in warning[0]


@pytest.mark.parametrize("interrupt", [False, True])
def test_campaign_inode_sink_contains_only_ordinary_exceptions(interrupt):
    from types import SimpleNamespace

    from evolution.tree.artifacts.artifacts import CapacityEstimate

    called = []

    def sink(note):
        called.append(note)
        if interrupt:
            raise KeyboardInterrupt
        raise RuntimeError("broken notes")

    holder = SimpleNamespace(notes=sink)
    estimate = CapacityEstimate(1, 1, 1, 0, InodeNeed(100))
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            campaign._Campaign.note_inodes(holder, estimate)
    else:
        campaign._Campaign.note_inodes(holder, estimate)
        campaign._Campaign.note_inodes(holder, estimate)
    assert len(called) == 1


def test_round_allowance_covers_budget_parent_on_older_resume_and_rewrites(tmp_path):
    from evolution.tree.roles.reflector.reflect_budget import write_budget_record
    from evolution.tree.storage.inode_floor import campaign_need, other_entries_per_round
    from tests.tree.storage.test_inode_floor import _terms

    root = tmp_path / "old-run"
    (root / "reflector-logs").mkdir(parents=True)
    terms = _terms()
    before = set(root.rglob("*"))
    write_budget_record(root, "r1", {"budget": 1})
    assert len(set(root.rglob("*")) - before) == 2
    write_budget_record(root, "r1", {"budget": 2})
    assert len(set(root.rglob("*")) - before) == 2
    write_budget_record(root, "r2", {"budget": 1})
    assert len(set(root.rglob("*")) - before) == 3
    # The same L reserves those two entries on fresh and old-run admission;
    # there is no second fixed-entry charge at creation.
    assert other_entries_per_round(terms) == 45
    fresh = campaign_need(terms, 3, fresh=True)
    resumed = campaign_need(terms, 3, fresh=False)
    assert fresh.floor - resumed.floor == 23
    assert fresh.whole_run - fresh.start == 2 * resumed.floor


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_preflight_observer_is_optional_output_and_interruptible(tmp_path, error):
    source = seed(tmp_path)
    seen = []

    def observer(estimate):
        seen.append(estimate)
        raise error("observer")

    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            ArtifactStore.preflight(
                tmp_path / "run", source, POLICY, 1, _estimate_observer=observer
            )
    else:
        estimate = ArtifactStore.preflight(
            tmp_path / "run", source, POLICY, 1, _estimate_observer=observer
        )
        assert seen == [estimate]
    assert len(seen) == 1 and not (tmp_path / "run").exists()


def test_byte_refusal_still_precedes_observer_and_writes(tmp_path, monkeypatch):
    from evolution.tree.artifacts.artifact_errors import ArtifactCapacityError

    source = seed(tmp_path)
    seen = []
    monkeypatch.setattr(
        os, "statvfs", lambda path: os.statvfs_result((4096, 4096, 100, 0, 0, 100, 0, 0, 0, 255))
    )
    with pytest.raises(ArtifactCapacityError, match="storage_capacity_insufficient"):
        ArtifactStore.create(
            tmp_path / "run",
            source,
            POLICY,
            parallel_attempts=1,
            inodes=lambda tree: InodeNeed(100),
            _estimate_observer=seen.append,
        )
    assert seen == [] and not (tmp_path / "run").exists()


async def test_evaluation_scored_resume_needs_no_new_pack(tmp_path, monkeypatch):
    from evolution.tree.storage.append_log import AppendLog

    path = evaluation_manifest(tmp_path)
    root = tmp_path / "run"
    real_append = AppendLog.append

    def cut(self, run_id, event, *args, **kwargs):
        if event == "evaluation_completed":
            raise RuntimeError("cut after durable scoring")
        return real_append(self, run_id, event, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AppendLog, "append", cut)
        with pytest.raises(RuntimeError, match="cut after durable scoring"):
            await evaluation.evaluate(path, root, adapter_factory=lambda *args: FakeAdapter())
    assert evaluation._unscored(root, 3) == 0
    pack = root / "scorer-traces/evaluation.pack"
    pack.unlink()
    from evolution.tree.storage.inode_floor import evaluation_resume_need

    # With all scores durable, the absent pack is not a capacity charge.
    assert (
        evaluation_resume_need(
            root,
            tree=None,
            unscored=evaluation._unscored(root, 3),
            scorer_concurrency=1,
            shared_tmp=True,
        )
        == 5
    )
    result = await evaluation.evaluate(path, root, adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    assert not pack.exists()


@pytest.mark.parametrize("progress", [False, True])
def test_evolve_short_value_note_preserves_secret_redaction(scaffold15, monkeypatch, progress):
    document = json.loads(scaffold15.read_text())
    document["editor"]["allowed_env"] = ["SHORT_VALUE", "LONG_VALUE"]
    scaffold15.write_text(json.dumps(document))
    monkeypatch.setenv("SHORT_VALUE", "flag")
    monkeypatch.setenv("LONG_VALUE", "SHORT_VALUE")

    async def cut(*args, **kwargs):
        raise RuntimeError("stop after early notes")

    monkeypatch.setattr("evolution.tree.evolve.evolve", cut)
    result = CliRunner().invoke(
        tree_app,
        [
            "evolve",
            "--manifest",
            str(scaffold15),
            "--run-dir",
            str(scaffold15.parent / "run"),
            *(["--progress"] if progress else []),
        ],
    )
    assert isinstance(result.exception, RuntimeError)
    assert "forwarded value [REDACTED] is shorter" in result.stderr
    assert "SHORT_VALUE" not in result.stderr
