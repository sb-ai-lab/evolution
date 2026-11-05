"""ADR-0036: admission signals the CLI without installing process-global state."""

from __future__ import annotations

import faulthandler
import io
import json
import os
import shutil
import signal
import sys

import pytest

from evolution.tree import process_log
from evolution.tree.evaluation import evaluate
from evolution.tree.evolve import evolve
from evolution.tree.execution.authorizations import (
    Authorization,
    hygiene_instance_digest,
    write_decision,
)
from evolution.tree.execution.output_admission import EvolveRefused
from tests.campaign_drivers import _cut_hook, _records
from tests.campaign_fakes import Crash, FakeAdapter
from tests.test_tree_evolve import held_campaign_lock, listing
from tests.test_tree_family14_public_links import _workspace15
from tests.test_tree_train_test import evaluation_manifest

pytestmark = pytest.mark.slow


async def _unfinished(tmp_path, monkeypatch):
    path = _workspace15(tmp_path)
    run = tmp_path / "run"
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            await evolve(path, run, adapter_factory=lambda *a: FakeAdapter())
    return path, run


def _driver_callback(monkeypatch, callback):
    """Attach the public callback to an existing genuine-run driver's invocation."""
    from tests import campaign_drivers as fixtures

    real = fixtures.evolve

    async def invoke(*args, **kwargs):
        return await real(*args, **kwargs, process_log=callback)

    monkeypatch.setattr(fixtures, "evolve", invoke)


async def test_a_new_campaign_fires_once_after_its_creation_record(tmp_path):
    path = _workspace15(tmp_path)
    calls = []

    def record(root, admitted):
        calls.append((root, admitted, [r["event"] for r in _records(root / "campaign.jsonl")]))

    result = await evolve(
        path, tmp_path / "run", adapter_factory=lambda *a: FakeAdapter(), process_log=record
    )
    assert result.terminal == "finished"
    assert calls == [(tmp_path / "run", frozenset(), ["campaign_created"])]


async def test_a_resumed_campaign_fires_once(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_connection_run import (
        ConnectedAdapter,
        _script,
        connected_run,
    )

    adapter = ConnectedAdapter()
    with monkeypatch.context() as patch:
        run, state, _ = await connected_run(
            tmp_path, patch, adapter, **_script(adapter, "materialize")
        )
    assert state.finished is None
    calls = []
    adapter.probe_materialize = None
    with monkeypatch.context() as patch:
        _driver_callback(patch, lambda *args: calls.append(args))
        await connected_run(tmp_path, patch, adapter)
    assert calls == [(run.resolve(), frozenset())]


async def test_the_admitted_hygiene_reaches_the_callback(tmp_path, monkeypatch):
    path, run = await _unfinished(tmp_path, monkeypatch)
    logs = run / "logs"
    logs.mkdir(mode=0o750)
    logs.chmod(0o750)
    digest = hygiene_instance_digest("logs", "not owner-only", "0o750")
    decision = Authorization.new("run_dir_unsafe_entry", digest, "accept this directory")
    write_decision(run, decision)
    calls = []
    result = await evolve(
        path,
        run,
        adapter_factory=lambda *a: FakeAdapter(),
        decision_directory=True,
        process_log=lambda *args: calls.append(args),
    )
    assert result.terminal == "finished"
    assert calls == [(run.resolve(), frozenset({digest}))]


@pytest.mark.parametrize(
    "problem", ["run_identity_changed", "run_dir_not_campaign", "journal_busy", "hygiene", "secret"]
)
async def test_refusals_never_fire(tmp_path, monkeypatch, problem):
    path, run = await _unfinished(tmp_path, monkeypatch)
    options = {}
    if problem == "run_identity_changed":
        scorer = tmp_path / "scorer" / "score.py"
        scorer.write_text(scorer.read_text() + "\n# changed\n")
    elif problem == "run_dir_not_campaign":
        run = tmp_path / "foreign"
        run.mkdir()
        (run / "foreign.txt").write_text("not a campaign")
    elif problem == "hygiene":
        (run / "logs").mkdir(mode=0o750)
        (run / "logs").chmod(0o750)
    elif problem == "secret":
        options["authorizations"] = [
            Authorization.new("run_dir_unsafe_entry", "a" * 64, "private-secret-value")
        ]
        document = json.loads(path.read_text())
        document["editor"]["allowed_env"] = ["PROCESS_LOG_SECRET"]
        path.write_text(json.dumps(document))
        options["environ"] = {**os.environ, "PROCESS_LOG_SECRET": "private-secret-value"}
    if problem == "journal_busy":
        # The state-first owed-terminal path exposes journal_busy as a refusal;
        # ordinary resume currently returns a persistence fault instead.
        scorer = tmp_path / "scorer" / "score.py"
        scorer.write_text(scorer.read_text() + "\n# changed\n")
        with pytest.raises(EvolveRefused) as caught:
            await evolve(path, run, adapter_factory=lambda *a: FakeAdapter())
        decision = Authorization.new(
            "run_identity_changed", caught.value.instance_digest, "close", effect="close"
        )
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_close_applied")
            with pytest.raises(Crash):
                await evolve(
                    path, run, adapter_factory=lambda *a: FakeAdapter(), authorizations=[decision]
                )
    calls = []
    before = listing(run)

    async def invoke():
        with pytest.raises(EvolveRefused) as caught:
            await evolve(
                path,
                run,
                adapter_factory=lambda *a: FakeAdapter(),
                process_log=lambda *args: calls.append(args),
                **options,
            )
        expected = {"hygiene": "run_dir_unsafe_entry", "secret": "record_matches_secret"}.get(
            problem, problem
        )
        assert caught.value.code == expected

    if problem == "journal_busy":
        with held_campaign_lock(run):
            await invoke()
    else:
        await invoke()
    assert calls == []
    assert listing(run) == before


async def test_a_finished_campaign_reopened_does_not_fire(tmp_path):
    path = _workspace15(tmp_path)
    run = tmp_path / "run"
    await evolve(path, run, adapter_factory=lambda *a: FakeAdapter())
    calls = []
    before = listing(run)
    result = await evolve(
        path, run, adapter_factory=lambda *a: FakeAdapter(), process_log=lambda *a: calls.append(a)
    )
    assert result.terminal == "finished"
    assert calls == []
    assert listing(run) == before


async def test_a_finished_reopen_that_only_repairs_environment_docs_gets_no_log(tmp_path):
    """ADR-0036, 2026-10-14 amendment: a docs repair without a journal append fires nothing."""
    docs = {"api.md": "The target exposes /health.\n"}
    path = _workspace15(tmp_path, environment_docs=docs)
    run = tmp_path / "run"
    await evolve(path, run, adapter_factory=lambda *a: FakeAdapter())
    snapshot = run / "environment-docs"
    shutil.rmtree(snapshot)
    journal = (run / "campaign.jsonl").read_bytes()
    calls = []
    log = process_log.ProcessLog("evolve", prefix="evo tree evolve", stderr=io.StringIO())

    def materialize(root, admitted):
        calls.append(root)
        log.materialize(root, admitted)

    try:
        result = await evolve(
            path, run, adapter_factory=lambda *a: FakeAdapter(), process_log=materialize
        )
    finally:
        log.close()
    assert result.terminal == "finished"
    assert (snapshot / "api.md").read_text(encoding="utf-8") == docs["api.md"]
    assert (run / "campaign.jsonl").read_bytes() == journal
    assert calls == []
    assert not (run / "logs").exists()


@pytest.mark.parametrize("callback_raises", [False, True])
async def test_an_accepted_close_and_an_owed_terminal_fire_once(
    tmp_path, monkeypatch, callback_raises
):
    path, run = await _unfinished(tmp_path, monkeypatch)
    scorer = tmp_path / "scorer" / "score.py"
    scorer.write_text(scorer.read_text() + "\n# changed\n")
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *a: FakeAdapter())
    decision = Authorization.new(
        "run_identity_changed",
        caught.value.instance_digest,
        "close changed condition",
        effect="close",
    )
    calls = []

    def before_close(root, admitted):
        assert _records(root / "campaign.jsonl")[-1]["event"] != "authorization_applied"
        calls.append((root, admitted))
        if callback_raises:
            raise RuntimeError("log failed")

    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_close_applied")
        with pytest.raises(Crash):
            await evolve(
                path,
                run,
                adapter_factory=lambda *a: FakeAdapter(),
                authorizations=[decision],
                process_log=before_close,
            )
    assert len(calls) == 1
    assert _records(run / "campaign.jsonl")[-1]["event"] == "authorization_applied"
    calls.clear()

    def record(root, admitted):
        assert _records(root / "campaign.jsonl")[-1]["event"] == "authorization_applied"
        calls.append((root, admitted))
        if callback_raises:
            raise RuntimeError("log failed")

    result = await evolve(path, run, process_log=record)
    assert result.finished["reason"] == "condition_changed"
    assert calls == [(run.resolve(), frozenset())]


@pytest.mark.parametrize("unsafe_logs", [False, True])
async def test_an_owed_cancellation_fires_once(tmp_path, unsafe_logs):
    from tests.test_tree_evolve import drop_last_record

    path = _workspace15(tmp_path)
    marker = tmp_path / "reflector" / "started"
    (tmp_path / "reflector" / "reflect.py").write_text(
        "from pathlib import Path\nimport time\nPath('started').write_text('1')\ntime.sleep(120)\n"
    )
    run = tmp_path / "run"
    result = await evolve(
        path, run, adapter_factory=lambda *a: FakeAdapter(), cancelled=marker.exists
    )
    assert result.finished["reason"] == "cancelled"
    assert any(
        r["event"] == "reflection_failed" and r["payload"]["error_class"] == "cancelled"
        for r in _records(run / "campaign.jsonl")
    )
    drop_last_record(run)
    if unsafe_logs:
        (run / "logs").mkdir(mode=0o750)
        (run / "logs").chmod(0o750)
    calls = []

    def before_terminal(root, admitted):
        assert _records(root / "campaign.jsonl")[-1]["event"] == "reflection_failed"
        calls.append((root, admitted))

    result = await evolve(
        path, run, adapter_factory=lambda *a: FakeAdapter(), process_log=before_terminal
    )
    assert result.finished["reason"] == "cancelled"
    assert calls == [(run.resolve(), frozenset())]


async def test_a_raising_callback_changes_nothing(tmp_path):
    def broken(*args):
        raise RuntimeError("log failed")

    path = _workspace15(tmp_path)
    result = await evolve(
        path, tmp_path / "run", adapter_factory=lambda *a: FakeAdapter(), process_log=broken
    )
    assert (result.terminal, result.code) == ("finished", None)


@pytest.mark.parametrize("callback_raises", [False, True])
async def test_evaluate_fires_once_after_birth_and_never_for_a_completed_report(
    tmp_path, callback_raises
):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    calls = []

    def record(root, admitted):
        assert _records(root / "evaluation.jsonl")[0]["event"] == "evaluation_created"
        calls.append((root, admitted))
        if callback_raises:
            raise RuntimeError("log failed")

    options = {"adapter_factory": lambda *a: FakeAdapter(), "process_log": record}
    report = await evaluate(path, run, **options)
    assert report["state"] == "completed"
    assert calls == [(run.absolute(), frozenset())]
    calls.clear()
    await evaluate(path, run, **options)
    assert calls == []
    document = json.loads(path.read_text())
    document["random_seed"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused, match="run_identity_changed"):
        await evaluate(path, run, **options)
    assert calls == []


async def test_a_library_call_installs_nothing(tmp_path):
    def state():
        return sys.stdout, sys.stderr, faulthandler.is_enabled(), signal.getsignal(signal.SIGHUP)

    before = state()
    campaign_root = tmp_path / "campaign"
    path = _workspace15(campaign_root)
    await evolve(path, campaign_root / "run", adapter_factory=lambda *a: FakeAdapter())
    assert state() == before
    evaluation_root = tmp_path / "evaluation"
    path = evaluation_manifest(evaluation_root)
    await evaluate(path, evaluation_root / "run", adapter_factory=lambda *a: FakeAdapter())
    assert state() == before
    assert not list(tmp_path.rglob("logs"))


async def test_a_resumed_evaluation_fires_once(tmp_path):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, cancelled=lambda: True, adapter_factory=lambda *a: FakeAdapter())
    assert _records(run / "evaluation.jsonl")[-1]["event"] == "evaluation_suspended"
    calls = []
    report = await evaluate(
        path, run, adapter_factory=lambda *a: FakeAdapter(), process_log=lambda *a: calls.append(a)
    )
    assert report["state"] == "completed"
    assert calls == [(run.absolute(), frozenset())]


async def test_a_post_birth_evaluation_refusal_fires_once(tmp_path):
    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["max_concurrency"] = 2
    path.write_text(json.dumps(document))
    run = tmp_path / "run"
    calls = []
    with pytest.raises(EvolveRefused, match="capabilities_insufficient"):
        await evaluate(
            path,
            run,
            adapter_factory=lambda *a: FakeAdapter(max_instances=1, max_concurrent_sessions=1),
            process_log=lambda *a: calls.append(a),
        )
    assert calls == [(run.absolute(), frozenset())]
    assert _records(run / "evaluation.jsonl")[0]["event"] == "evaluation_created"
