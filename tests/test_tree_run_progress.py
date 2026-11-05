"""Progress is read-only presentation over genuine engine journals (#197)."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from evolution.tree import run_watch
from evolution.tree.cli import tree_app
from evolution.tree.execution.output_admission import EvolveRefused
from tests import run_observability_fakes as fakes
from tests.test_tree_surface_contract import CASES

pytestmark = pytest.mark.slow
runner = CliRunner()
EVOLVE_IDENTITY_REFUSAL = [
    "evo tree evolve: refused: run_identity_changed",
    "evo tree evolve: class: hard",
    "evo tree evolve: the campaign is finished and there is nothing left to continue; "
    "start a linked run that declares the new condition",
]


@pytest.fixture(scope="module")
async def campaign(tmp_path_factory):
    return await fakes.genuine_campaign_run(tmp_path_factory.mktemp("progress-campaign"))


@pytest.fixture(scope="module")
async def evaluation(tmp_path_factory):
    return await fakes.genuine_evaluation_run(tmp_path_factory.mktemp("progress-evaluation"))


def copy_journals(source, target, *, primary=None, records=None):
    replay = fakes.JournalReplayer(source, target)
    for path in source.glob("measurements/*/events.jsonl"):
        name = str(path.relative_to(source))
        replay.append_records(name, replay.lines(name))
    if primary is None:
        primary = "campaign.jsonl" if (source / "campaign.jsonl").exists() else "evaluation.jsonl"
    replay.append_records(primary, replay.lines(primary) if records is None else records)
    return replay


def invoke(kind, source, target, *extra):
    manifest = source.parent / ("manifest.json" if kind == "evolve" else "evaluation.json")
    return runner.invoke(
        tree_app, [kind, "--manifest", str(manifest), "--run-dir", str(target), *extra]
    )


def fake_engine(monkeypatch, kind, source, target, *, result=None, action=None, notes=()):
    async def engine(_manifest, run_dir, **kwargs):
        assert run_dir == target
        for message in notes:
            kwargs["notes"](message)
        if action is None:
            copy_journals(source, run_dir)
        else:
            action()
        return (
            result
            if result is not None
            else (CASES["completed"][0] if kind == "evolve" else {"state": "completed"})
        )

    monkeypatch.setattr(
        "evolution.tree.evolve.evolve"
        if kind == "evolve"
        else "evolution.tree.evaluation.evaluate",
        engine,
    )


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_default_off_keeps_original_bytes(kind, campaign, evaluation, tmp_path, monkeypatch):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    fake_engine(monkeypatch, kind, source, target)
    result = invoke(kind, source, target, *(["--json-indent", "0"] if kind == "evolve" else []))
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if kind == "evolve":
        assert result.stdout == (
            '{\n"terminal": "finished",\n"quiescent": true,\n'
            '"finished": {\n"outcome": "completed",\n"reason": "test"\n},\n'
            '"suspended": null,\n"owner": null,\n"code": null,\n'
            '"result": {\n"run_id": "c-run"\n},\n'
            '"status": {\n"run_id": "c-run",\n"state": "finished"\n}\n}\n'
        )
    else:
        assert result.stdout == '{"state": "completed"}\n'


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_new_run_drains_before_document_and_thread_stops(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    fake_engine(monkeypatch, kind, source, target)
    before = set(threading.enumerate())
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 0, result.output
    created = "campaign created" if kind == "evolve" else "evaluation created"
    ended = "campaign finished" if kind == "evolve" else "evaluation completed"
    assert created in result.stderr.splitlines()[0]
    assert ended in result.stderr.splitlines()[-1]
    assert result.output.index(ended) < result.output.index(
        "{\n" if kind == "evolve" else '{"state"'
    )
    assert set(threading.enumerate()) <= before
    assert all(line.startswith(f"evo tree {kind}: ") for line in result.stderr.splitlines())
    # Corpus, scorer feedback, request messages and role text never reach progress.
    for needle in fakes.task_data_needles(source):
        if len(needle) < 3:
            assert not re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", result.stderr)
        else:
            assert needle not in result.stderr
    for path in [
        source / ("campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"),
        *source.glob("measurements/*/events.jsonl"),
    ]:
        assert (target / path.relative_to(source)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_resumed_run_prints_one_start_snapshot_then_only_appends(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    primary = "campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"
    records = fakes.JournalReplayer(source, tmp_path / "unused").lines(primary)
    target = tmp_path / "run"
    replay = copy_journals(source, target, primary=primary, records=records[:-1])
    fake_engine(
        monkeypatch,
        kind,
        source,
        target,
        action=lambda: replay.append_records(primary, records[-1:]),
    )
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert len(lines) == 2
    assert "running" in lines[0]
    assert ("campaign finished" if kind == "evolve" else "evaluation completed") in lines[1]
    assert "created" not in result.stderr


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_already_finished_run_prints_snapshot_without_append(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    copy_journals(source, target)
    fake_engine(monkeypatch, kind, source, target, action=lambda: None)
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 0, result.output
    assert len(result.stderr.splitlines()) == 1
    assert "finished completed" in result.stderr


def test_quiet_keeps_progress_and_small_stdout(campaign, tmp_path, monkeypatch):
    target = tmp_path / "run"
    fake_engine(monkeypatch, "evolve", campaign, target)
    result = invoke("evolve", campaign, target, "--quiet", "--progress")
    assert result.exit_code == 0, result.output
    assert "campaign created" in result.stderr
    assert "campaign finished" in result.stderr
    assert json.loads(result.stdout) == {
        "terminal": "finished",
        "finished": {"outcome": "completed", "reason": "test"},
        "code": None,
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_progress_never_overrides_engine_exit(case, campaign, tmp_path, monkeypatch):
    target = tmp_path / "run"
    fake_engine(monkeypatch, "evolve", campaign, target, result=CASES[case][0])
    result = invoke("evolve", campaign, target, "--progress")
    assert result.exit_code == CASES[case][1], result.output
    assert "campaign finished" in result.stderr
    assert json.loads(result.stdout)["terminal"] == CASES[case][0].terminal


def test_evaluation_birth_alone_stays_silent_before_refusal(evaluation, tmp_path, monkeypatch):
    target = tmp_path / "run"
    replay = fakes.JournalReplayer(evaluation, target)
    polled = threading.Event()
    original = run_watch.WatchSession.poll

    def poll(session):
        result = original(session)
        if session.records:
            polled.set()
        return result

    monkeypatch.setattr(run_watch.WatchSession, "poll", poll)

    def refuse():
        replay.append_records("evaluation.jsonl", replay.lines("evaluation.jsonl")[:1])
        assert polled.wait(3), "background reader did not see the birth"
        raise EvolveRefused("capabilities_insufficient")

    fake_engine(monkeypatch, "evaluate", evaluation, target, action=refuse)
    before = set(threading.enumerate())
    result = invoke("evaluate", evaluation, target, "--progress")
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr == "evo tree evaluate: refused: capabilities_insufficient\n"
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_progress_lines_use_kind_secret_set(kind, campaign, evaluation, tmp_path, monkeypatch):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    manifest = source.parent / ("manifest.json" if kind == "evolve" else "evaluation.json")
    # Restore the shared fixture's manifest afterwards; no secret is journaled.
    original_document = manifest.read_text()
    document = json.loads(original_document)
    document["scorer"]["allowed_env"] = ["PROGRESS_FORWARDED"]
    secret = "eight-or-more-secret" if kind == "evolve" else "four"
    monkeypatch.setenv("PROGRESS_FORWARDED", secret)
    original_feed = run_watch.Renderer.feed

    def feed(renderer, record, **kwargs):
        lines = original_feed(renderer, record, **kwargs)
        return [*lines, f"forwarded {secret}"]

    monkeypatch.setattr(run_watch.Renderer, "feed", feed)
    fake_engine(monkeypatch, kind, source, target)
    try:
        manifest.write_text(json.dumps(document))
        result = invoke(kind, source, target, "--progress")
    finally:
        manifest.write_text(original_document)
    assert result.exit_code == 0, result.output
    assert "forwarded [REDACTED]" in result.stderr
    assert secret not in result.stderr


def test_help_exposes_progress():
    for kind in ("evolve", "evaluate"):
        result = runner.invoke(tree_app, [kind, "--help"])
        assert result.exit_code == 0
        assert "--progress" in result.stdout


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_background_prints_during_engine_only_after_append(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    primary = "campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"
    replay = fakes.JournalReplayer(source, target)
    polled = threading.Event()
    emitted = threading.Event()
    original_poll = run_watch.WatchSession.poll
    original_say = typer.echo
    threads = []

    def poll(session):
        threads.append(threading.current_thread())
        result = original_poll(session)
        polled.set()
        return result

    def echo(message=None, **kwargs):
        result = original_say(message, **kwargs)
        if "created" in str(message):
            emitted.set()
        return result

    monkeypatch.setattr(run_watch.WatchSession, "poll", poll)
    monkeypatch.setattr("typer.echo", echo)

    def write():
        assert polled.wait(3)
        assert not emitted.is_set(), "progress escaped before admission"
        # Two primary records include an evaluation binding after its birth.
        replay.append_records(primary, replay.lines(primary)[:2])
        assert emitted.wait(3), "progress waited until engine return"
        copy_journals(source, target, primary=primary, records=replay.lines(primary)[2:])

    fake_engine(monkeypatch, kind, source, target, action=write)
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 0, result.output
    background = [thread for thread in threads if thread is not threading.current_thread()]
    assert background and all(thread.daemon for thread in background)
    assert all(not thread.is_alive() for thread in background)
    assert "created" in result.stderr
    assert ("campaign finished" if kind == "evolve" else "evaluation completed") in result.stderr


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_same_deterministic_journals_with_or_without_progress(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    outputs = []
    for enabled in (False, True):
        target = tmp_path / str(enabled)
        fake_engine(monkeypatch, kind, source, target)
        result = invoke(kind, source, target, *(["--progress"] if enabled else []))
        assert result.exit_code == 0, result.output
        outputs.append(
            {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
        )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_controller_opens_read_only_and_never_locks(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    import fcntl
    import os

    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    primary = "campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"
    replay = fakes.JournalReplayer(source, target)
    records = replay.lines(primary)
    copy_journals(source, target, primary=primary, records=records[:-1])
    opened = []
    real_open = os.open

    def open_readonly(path, flags, *args, **kwargs):
        opened.append(flags)
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert not flags & (os.O_CREAT | os.O_APPEND | os.O_TRUNC)
        return real_open(path, flags, *args, **kwargs)

    def no_lock(*args, **kwargs):
        raise AssertionError("progress took or tested a lock")

    monkeypatch.setattr(os, "open", open_readonly)
    monkeypatch.setattr(fcntl, "flock", no_lock)
    monkeypatch.setattr(fcntl, "lockf", no_lock)
    lines = []
    controller = run_watch.ProgressController(target, say=lines.append)
    replay.append_records(primary, records[-1:])
    controller.finish()
    assert opened
    assert ("campaign finished" if kind == "evolve" else "evaluation completed") in lines[-1]
    assert {path.relative_to(target) for path in target.rglob("*") if path.is_file()} == {
        Path(primary),
        *[path.relative_to(source) for path in source.glob("measurements/*/events.jsonl")],
    }


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_engine_exception_stops_thread(kind, campaign, evaluation, tmp_path, monkeypatch):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"

    def fail():
        raise RuntimeError("engine failure")

    fake_engine(monkeypatch, kind, source, target, action=fail)
    before = set(threading.enumerate())
    result = invoke(kind, source, target, "--progress")
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "engine failure"
    assert result.stdout == result.stderr == ""
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
@pytest.mark.parametrize("fault", ["start", "poll", "render", "close", "output"])
def test_observer_failure_cannot_change_engine_result(
    kind, fault, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"

    def fail(*args, **kwargs):
        raise OSError("private observer failure detail")

    if fault == "start":
        monkeypatch.setattr(run_watch, "WatchSession", fail)
    elif fault == "poll":
        monkeypatch.setattr(run_watch.WatchSession, "poll", fail)
    elif fault == "render":
        monkeypatch.setattr(run_watch.Renderer, "feed", fail)
    elif fault == "close":
        original_close = run_watch.WatchSession.close

        def close(session):
            original_close(session)
            fail()

        monkeypatch.setattr(run_watch.WatchSession, "close", close)
    else:
        original_echo = typer.echo

        def echo(message=None, **kwargs):
            if "created" in str(message):
                fail()
            return original_echo(message, **kwargs)

        monkeypatch.setattr(typer, "echo", echo)
    fake_engine(monkeypatch, kind, source, target)
    before = set(threading.enumerate())
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 0, result.output
    assert "private observer failure" not in result.output
    assert json.loads(result.stdout)
    assert set(threading.enumerate()) <= before


def test_real_campaign_cli_finishes_then_refuses_changed_manifest(campaign, tmp_path, monkeypatch):
    from evolution.tree.evolve import evolve
    from tests.campaign_fakes import FakeAdapter

    target = tmp_path / "run"

    async def engine(manifest, run_dir, **kwargs):
        return await evolve(
            manifest,
            run_dir,
            adapter_factory=lambda *args: FakeAdapter(used=fakes._edited_used),
            **kwargs,
        )

    monkeypatch.setattr("evolution.tree.evolve.evolve", engine)
    result = invoke("evolve", campaign, target, "--progress")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["terminal"] == "finished"
    assert "campaign finished" in result.stderr
    assert result.output.index("campaign finished") < result.output.index("{\n")
    assert result.output.index("continue: set linked_from") > result.output.index("{\n")
    before = {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }
    manifest = campaign.parent / "manifest.json"
    original = manifest.read_text()
    changed = json.loads(original)
    changed["campaign"]["rounds"] += 1
    try:
        manifest.write_text(json.dumps(changed))
        refused = invoke("evolve", campaign, target, "--progress")
    finally:
        manifest.write_text(original)
    assert refused.exit_code == 2, refused.output
    assert refused.stdout == ""
    lines = refused.stderr.splitlines()
    assert lines[:9] == [
        "evo tree evolve: note: Corpus split: train (2 cases); held-out test (0 cases); mode implicit_train.",
        "evo tree evolve: note: Corpus partition_mode: implicit_train.",
        "evo tree evolve: note: Corpus split train: 2 cases, 1 groups.",
        'evo tree evolve: note: Corpus split train, stratum "s1": 2 cases, 1 groups.',
        "evo tree evolve: note: Corpus split test: 0 cases, 0 groups.",
        'evo tree evolve: note: Corpus split test, stratum "s1": 0 cases, 0 groups.',
        'evo tree evolve: note: Corpus split coverage: stratum "s1" is absent from test.',
        EVOLVE_IDENTITY_REFUSAL[0],
        "evo tree evolve: differs: identity.policy.rounds  journaled 1  observed 2",
    ]
    assert re.fullmatch(
        r"evo tree evolve: differs: identity\.policy_digest  "
        r"journaled [0-9a-f]{64}  observed [0-9a-f]{64}",
        lines[9],
    )
    assert lines[10:] == EVOLVE_IDENTITY_REFUSAL[1:]
    assert "campaign created" not in refused.stderr
    assert "finished completed" not in refused.stderr
    assert before == {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }


def test_real_evaluation_capabilities_refusal_after_birth(tmp_path, monkeypatch):
    import asyncio

    from evolution.tree.evaluation import evaluate
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_train_test import evaluation_manifest

    work = tmp_path / "workspace"
    manifest = evaluation_manifest(work)
    document = json.loads(manifest.read_text())
    document["max_concurrency"] = 2
    manifest.write_text(json.dumps(document))
    target = tmp_path / "run"
    polled = threading.Event()
    original_poll = run_watch.WatchSession.poll

    def poll(session):
        result = original_poll(session)
        if session.records:
            polled.set()
        return result

    class Insufficient(FakeAdapter):
        async def preflight(self, expected):
            result = await super().preflight(expected)
            assert await asyncio.to_thread(polled.wait, 3)
            return result

    async def engine(path, run_dir, **kwargs):
        return await evaluate(
            path,
            run_dir,
            adapter_factory=lambda *args: Insufficient(max_instances=1, max_concurrent_sessions=1),
            **kwargs,
        )

    monkeypatch.setattr(run_watch.WatchSession, "poll", poll)
    monkeypatch.setattr("evolution.tree.evaluation.evaluate", engine)
    before = set(threading.enumerate())
    result = runner.invoke(
        tree_app, ["evaluate", "--manifest", str(manifest), "--run-dir", str(target), "--progress"]
    )
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "Corpus partition_mode: explicit.",
        "Corpus split train: 1 cases, 1 groups.",
        'Corpus split train, stratum "common": 1 cases, 1 groups.',
        "Corpus split test: 1 cases, 1 groups.",
        'Corpus split test, stratum "common": 1 cases, 1 groups.',
        "evo tree evaluate: refused: capabilities_insufficient",
    ]
    assert [
        json.loads(line)["event"] for line in (target / "evaluation.jsonl").read_text().splitlines()
    ] == ["evaluation_created"]
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_bound_measurement_append_alone_opens_gate(kind, campaign, evaluation, tmp_path):
    source = campaign if kind == "evolve" else evaluation
    primary = "campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"
    target = tmp_path / "run"
    replay = fakes.JournalReplayer(source, target)
    records = replay.lines(primary)
    cut = next(
        i for i, raw in enumerate(records) if json.loads(raw)["event"] == "measurement_bound"
    )
    replay.append_records(primary, records[: cut + 1])
    measurement = (
        "measurements/m-r00-champion/events.jsonl"
        if kind == "evolve"
        else "measurements/evaluation/events.jsonl"
    )
    polled = threading.Event()
    lines = []

    def say(line):
        lines.append(line)
        if "pairs scored" in line:
            polled.set()

    controller = run_watch.ProgressController(target, say=say)
    try:
        assert lines == []
        replay.append_records(measurement, replay.lines(measurement))
        assert polled.wait(3), "measurement append did not admit live progress"
    finally:
        controller.finish()
    assert "running" in lines[0]
    assert "created" not in "\n".join(lines)
    assert (target / primary).read_bytes() == b"".join(records[: cut + 1])


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_no_write_refusal_discards_start_snapshot(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    copy_journals(source, target)

    def refuse():
        raise EvolveRefused("run_identity_changed")

    fake_engine(monkeypatch, kind, source, target, action=refuse)
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines()[0] == f"evo tree {kind}: refused: run_identity_changed"
    assert "finished completed" not in result.stderr
    assert "created" not in result.stderr


@pytest.fixture(scope="module")
async def resumed_suspension(tmp_path_factory):
    from tests.campaign_drivers import _ParentLies
    from tests.campaign_drivers import two_case_run as _two_case_run

    work = tmp_path_factory.mktemp("progress-suspension")
    adapter = _ParentLies(used=fakes._edited_used)
    run = await _two_case_run(
        work,
        adapter=adapter,
        used=fakes._edited_used,
        as_family_13=dict(fakes.CAMPAIGN_SETTINGS),
        terminal="suspended",
    )
    adapter.honest = 100
    await _two_case_run(
        work,
        adapter=adapter,
        used=fakes._edited_used,
        as_family_13=dict(fakes.CAMPAIGN_SETTINGS),
    )
    return run


def test_resumed_suspension_is_one_snapshot_then_new_records(
    resumed_suspension, tmp_path, monkeypatch
):
    source, target = resumed_suspension, tmp_path / "run"
    replay = fakes.JournalReplayer(source, target)
    records = replay.lines("campaign.jsonl")
    cut = next(
        i for i, raw in enumerate(records) if json.loads(raw)["event"] == "campaign_suspended"
    )
    copy_journals(source, target, primary="campaign.jsonl", records=records[: cut + 1])
    fake_engine(
        monkeypatch,
        "evolve",
        source,
        target,
        action=lambda: replay.append_records("campaign.jsonl", records[cut + 1 :]),
    )
    result = invoke("evolve", source, target, "--progress")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert "waiting for a resume" in lines[0]
    assert "suspended 1×" in lines[0]
    assert sum("waiting for a resume" in line for line in lines) == 1
    assert "campaign created" not in result.stderr
    assert "campaign finished" in lines[-1]


def test_evaluation_progress_preserves_suspended_exit(evaluation, tmp_path, monkeypatch):
    target = tmp_path / "run"
    fake_engine(monkeypatch, "evaluate", evaluation, target, result={"state": "suspended"})
    result = invoke("evaluate", evaluation, target, "--progress")
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {"state": "suspended"}
    assert "evaluation completed" in result.stderr


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
@pytest.mark.parametrize("refused", [False, True])
def test_engine_notes_are_immediate_before_journal_writes_or_refusal(
    kind, refused, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"

    prefix = "evo tree evolve: note: " if kind == "evolve" else ""
    diagnostics = [prefix + "first diagnostic", prefix + "second diagnostic"]
    emitted = []
    real_echo = typer.echo

    def echo(message=None, *args, **kwargs):
        if kwargs.get("err"):
            emitted.append(str(message))
        return real_echo(message, *args, **kwargs)

    def after_notes():
        # No journal exists yet, so the watcher cannot interleave progress here.
        # Check inside the engine: flushing only at its return is too late.
        assert not target.exists()
        assert emitted == diagnostics
        if refused:
            raise EvolveRefused("run_identity_changed")
        copy_journals(source, target)

    monkeypatch.setattr(typer, "echo", echo)

    fake_engine(
        monkeypatch,
        kind,
        source,
        target,
        action=after_notes,
        notes=("first diagnostic", "second diagnostic"),
    )
    result = invoke(kind, source, target, "--progress")
    assert result.exit_code == (2 if refused else 0), result.output
    lines = result.stderr.splitlines()
    assert lines[:2] == diagnostics
    assert all(lines.count(line) == 1 for line in diagnostics)
    if refused:
        refusal = (
            EVOLVE_IDENTITY_REFUSAL
            if kind == "evolve"
            else ["evo tree evaluate: refused: run_identity_changed"]
        )
        assert lines == [*diagnostics, *refusal]
        assert result.stdout == ""
        assert not target.exists()
    else:
        created = "campaign created" if kind == "evolve" else "evaluation created"
        ended = "campaign finished" if kind == "evolve" else "evaluation completed"
        assert created in lines[2]
        assert ended in lines[-1]
        assert result.output.index(ended) < result.output.index(
            "{\n" if kind == "evolve" else '{"state"'
        )
        if kind == "evolve":
            assert json.loads(result.stdout)["terminal"] == CASES["completed"][0].terminal
        else:
            assert json.loads(result.stdout) == {"state": "completed"}
        for path in [
            source / ("campaign.jsonl" if kind == "evolve" else "evaluation.jsonl"),
            *source.glob("measurements/*/events.jsonl"),
        ]:
            assert (target / path.relative_to(source)).read_bytes() == path.read_bytes()


def test_short_forwarded_note_precedes_engine_note_and_refusal(campaign, tmp_path, monkeypatch):
    target = tmp_path / "run"
    manifest = campaign.parent / "manifest.json"
    original = manifest.read_text()
    document = json.loads(original)
    document["scorer"]["allowed_env"] = ["SHORT_PROGRESS_FLAG"]
    monkeypatch.setenv("SHORT_PROGRESS_FLAG", "yes")

    def refuse():
        raise EvolveRefused("run_identity_changed")

    fake_engine(
        monkeypatch, "evolve", campaign, target, action=refuse, notes=("engine diagnostic",)
    )
    try:
        manifest.write_text(json.dumps(document))
        result = invoke("evolve", campaign, target, "--progress")
    finally:
        manifest.write_text(original)
    assert result.exit_code == 2, result.output
    lines = result.stderr.splitlines()
    assert result.stdout == ""
    assert lines == [
        "evo tree evolve: note: forwarded value SHORT_PROGRESS_FLAG is shorter than 8 characters and is not redacted",
        "evo tree evolve: note: engine diagnostic",
        *EVOLVE_IDENTITY_REFUSAL,
    ]
    assert not target.exists()


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
def test_default_off_preserves_notes_before_refusal(
    kind, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"

    def refuse():
        raise EvolveRefused("run_identity_changed")

    fake_engine(
        monkeypatch,
        kind,
        source,
        target,
        action=refuse,
        notes=("first diagnostic", "second diagnostic"),
    )
    result = invoke(kind, source, target)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    prefix = "evo tree evolve: note: " if kind == "evolve" else ""
    assert result.stderr.splitlines()[:3] == [
        prefix + "first diagnostic",
        prefix + "second diagnostic",
        f"evo tree {kind}: refused: run_identity_changed",
    ]


@pytest.mark.parametrize("kind", ["evolve", "evaluate"])
@pytest.mark.parametrize("old_partial", [False, True])
def test_first_new_byte_opens_snapshot_but_record_waits_for_newline(
    kind, old_partial, campaign, evaluation, tmp_path, monkeypatch
):
    source = campaign if kind == "evolve" else evaluation
    target = tmp_path / "run"
    replay = fakes.JournalReplayer(source, target)
    if kind == "evolve":
        journal = "campaign.jsonl"
        records = replay.lines(journal)
        copy_journals(source, target, primary=journal, records=records[:-1])
        following = records[-1]
        new_line = "campaign finished"
    else:
        replay.append_records("evaluation.jsonl", replay.lines("evaluation.jsonl")[:2])
        journal = "measurements/evaluation/events.jsonl"
        records = replay.lines(journal)
        cut = max(
            i for i, raw in enumerate(records) if json.loads(raw)["event"] == "scoring_finished"
        )
        replay.append_records(journal, records[:cut])
        following = records[cut]
        new_line = "3/3 pairs scored"
    half = len(following) // 2
    if old_partial:
        replay.append_bytes(journal, following[:half])
    polled = threading.Event()
    original_poll = run_watch.ProgressController._poll

    def poll(controller):
        result = original_poll(controller)
        polled.set()
        return result

    monkeypatch.setattr(run_watch.ProgressController, "_poll", poll)
    lines = []
    controller = run_watch.ProgressController(target, say=lines.append)
    try:
        assert polled.wait(3)
        assert lines == [], "pre-existing partial bytes opened the invocation gate"
        polled.clear()
        if old_partial:
            # One new byte extends the old half-line without completing it.
            offset = half
        else:
            offset = 0
        replay.append_bytes(journal, following[offset : offset + 1])
        assert polled.wait(3)
        assert len(lines) == 1, "first new bytes must print only the resumed snapshot"
        assert "running" in lines[0]
        assert new_line not in lines[0]
        replay.append_bytes(journal, following[offset + 1 :])
    finally:
        controller.finish()
    assert sum(new_line in line for line in lines) == 1
    assert len(lines) == 2


def test_physical_heads_keep_consumed_prefix_unchanged(campaign, tmp_path):
    replay = fakes.JournalReplayer(campaign, tmp_path / "run")
    records = replay.lines("campaign.jsonl")
    replay.append_records("campaign.jsonl", records[:1])
    session = run_watch.WatchSession(replay.target)
    try:
        consumed = session.heads
        observed = session.observed_byte_heads
        assert observed == consumed
        replay.append_bytes("campaign.jsonl", records[1][:1])
        assert session.poll() == ([], None)
        assert session.heads == consumed
        assert session.observed_byte_heads == {"campaign.jsonl": observed["campaign.jsonl"] + 1}
    finally:
        session.close()


async def test_family15_continuation_emits_live_before_finish(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import evolve
    from tests.campaign_drivers import _ParentLies
    from tests.test_tree_campaign_family15_delivery import public_run

    adapter = _ParentLies()
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        gate={"fresh_parent": True},
        campaign={"rounds": 2},
        adapter=adapter,
    )
    assert result.terminal == "suspended"
    adapter.honest = 100

    lines = []
    emitted = threading.Event()
    polled = threading.Event()
    original_poll = run_watch.ProgressController._poll

    def poll(controller):
        try:
            return original_poll(controller)
        finally:
            polled.set()

    def say(line):
        lines.append(line)
        if "round 1 closed" in line:
            emitted.set()

    original_hook = _Campaign.hook
    barriers = []

    def hook(self, name):
        original_hook(self, name)
        if name == "after_round_closed":
            barriers.append(name)
            assert emitted.wait(5), "progress was silent while the invocation was still running"

    monkeypatch.setattr(run_watch.ProgressController, "_poll", poll)
    monkeypatch.setattr(_Campaign, "hook", hook)
    controller = run_watch.ProgressController(run, say=say)
    try:
        assert polled.wait(3)
        continued = await evolve(
            tmp_path / "manifest.json", run, adapter_factory=lambda *args: adapter
        )
        assert barriers and emitted.is_set()
        assert continued.terminal == "finished"
    finally:
        controller.finish()
