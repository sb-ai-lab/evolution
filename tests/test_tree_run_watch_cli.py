"""Read-only watch following over genuine journal bytes (Task 6 of #197)."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from evolution.tree import run_watch
from evolution.tree.campaign import CampaignResult
from evolution.tree.cli import tree_app
from evolution.tree.evolve import exit_code_for
from tests import run_observability_fakes as fakes

pytestmark = pytest.mark.slow
runner = CliRunner()


class Script:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.now = 0.0
        self.sleeps = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        try:
            action = next(self.actions)
        except StopIteration:
            raise KeyboardInterrupt from None
        action()


@pytest.fixture(scope="module")
async def campaign(tmp_path_factory):
    return await fakes.genuine_campaign_run(tmp_path_factory.mktemp("watch-campaign"))


@pytest.fixture(scope="module")
async def campaign12(tmp_path_factory):
    return await fakes.genuine_repeated_campaign_run(tmp_path_factory.mktemp("watch-campaign12"))


@pytest.fixture(scope="module")
async def evaluation(tmp_path_factory):
    return await fakes.genuine_repair_evaluation_run(tmp_path_factory.mktemp("watch-evaluation"))


@pytest.fixture(scope="module")
async def suspended_campaign(tmp_path_factory):
    from tests.campaign_drivers import _ParentLies
    from tests.campaign_drivers import two_case_run as _two_case_run

    work = tmp_path_factory.mktemp("watch-suspension")
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
        work, adapter=adapter, used=fakes._edited_used, as_family_13=dict(fakes.CAMPAIGN_SETTINGS)
    )
    return run


def replay(source, target, journal, count=-1):
    replayer = fakes.JournalReplayer(source, target)
    lines = replayer.lines(journal)
    replayer.append_records(journal, lines[:count])
    return replayer, lines


def follow(path, script, lines, **kwargs):
    return run_watch.follow(
        path,
        emit=lines.append,
        sleep=script.sleep,
        clock=script.clock,
        wall=lambda: "12:34:56",
        **kwargs,
    )


def test_cli_once_is_registered_and_prints_a_finished_snapshot(campaign):
    result = runner.invoke(tree_app, ["watch", "--run-dir", str(campaign), "--once"])
    assert result.exit_code == 0, result.output
    # ADR-0032: a /15 fixture is born as family 14.
    assert "campaign created · family 14" in result.stdout
    assert "finished: completed" in result.stdout
    assert result.stderr == ""


def test_start_replays_silently_then_appended_half_line_ends(campaign, tmp_path):
    replayer, records = replay(campaign, tmp_path / "run", "campaign.jsonl")
    script = Script(
        lambda: replayer.append_bytes("campaign.jsonl", records[-1][:-1]),
        lambda: replayer.append_bytes("campaign.jsonl", b"\n"),
    )
    lines = []
    assert follow(replayer.target, script, lines) == 0
    assert script.sleeps == 2
    assert sum("campaign created" in line for line in lines) == 1
    assert sum("campaign finished" in line for line in lines) == 1
    assert not any("attempt opened" in line for line in lines)


@pytest.mark.parametrize(
    "outcome,code",
    [("completed", 0), ("stopped", 3)],
)
def test_campaign_ending_map_matches_evolve(campaign, tmp_path, outcome, code):
    replayer, records = replay(campaign, tmp_path / "run", "campaign.jsonl")
    document = json.loads(records[-1])
    document["payload"].update(outcome=outcome)
    # All four are legal campaign endings; their reason is a bounded code.
    document["payload"]["reason"] = {
        "completed": "rounds_exhausted",
        "stopped": "cancelled",
        "budget_exhausted": "wall_time_budget",
        "infrastructure_failed": "scorer_failed",
    }[outcome]
    document["payload"]["continuation"]["reason"] = document["payload"]["reason"]
    ending = (json.dumps(document) + "\n").encode()
    lines = []
    result = CampaignResult("finished", True, document["payload"], None, None, None, None)
    assert (
        follow(
            replayer.target, Script(lambda: replayer.append_bytes("campaign.jsonl", ending)), lines
        )
        == exit_code_for(result)
        == code
    )
    assert any(f"campaign finished · {outcome}" in line for line in lines)


@pytest.mark.parametrize("once", [False, True])
def test_finished_at_start_never_sleeps(campaign, once):
    script, lines = Script(), []
    assert follow(campaign, script, lines, once=once) == 0
    assert script.sleeps == 0
    assert "finished: completed" in lines[-1]


def test_once_on_live_cut_exits_zero(campaign, tmp_path):
    replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl", 2)
    script, lines = Script(), []
    assert follow(replayer.target, script, lines, once=True) == 0
    assert script.sleeps == 0
    assert lines[-1].endswith("running")


def test_waits_once_until_journal_appears(campaign, tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    replayer = fakes.JournalReplayer(campaign, target)
    script = Script(
        lambda: None,
        lambda: replayer.append_records("campaign.jsonl", replayer.lines("campaign.jsonl")),
    )
    lines = []
    assert follow(target, script, lines) == 0
    assert lines.count("12:34:56  waiting for the campaign journal") == 1


def test_once_empty_directory_does_not_wait(tmp_path):
    script, lines = Script(), []
    assert follow(tmp_path, script, lines, once=True) == 0
    assert script.sleeps == 0


def test_ctrl_c_returns_130(campaign, tmp_path):
    replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl", 2)
    assert follow(replayer.target, Script(), []) == 130


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
def test_new_suspension_ends_but_historical_suspension_waits(
    suspended_campaign, evaluation, tmp_path, kind
):
    source = suspended_campaign if kind == "campaign" else evaluation
    journal, event = f"{kind}.jsonl", f"{kind}_suspended"
    original = fakes.JournalReplayer(source, tmp_path / "run")
    records = original.lines(journal)
    cut = next(i for i, raw in enumerate(records) if json.loads(raw)["event"] == event)
    original.append_records(journal, records[:cut])
    new_lines = []
    assert (
        follow(
            original.target, Script(lambda: original.append_bytes(journal, records[cut])), new_lines
        )
        == 1
    )
    lines = []
    assert (
        follow(
            original.target,
            Script(lambda: original.append_records(journal, records[cut + 1 :])),
            lines,
        )
        == 0
    )
    assert any("waiting for a resume" in line for line in lines)
    assert any("completed" in line for line in lines)


async def test_family15_suspension_is_new_once_and_historical_idle_keeps_following(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _ParentLies
    from tests.test_tree_campaign_family15_delivery import public_run

    source, result = await public_run(
        tmp_path / "source", monkeypatch, gate={"fresh_parent": True}, adapter=_ParentLies()
    )
    assert result.terminal == "suspended"
    replayer = fakes.JournalReplayer(source, tmp_path / "replay")
    records = replayer.lines("campaign.jsonl")
    assert json.loads(records[-1])["event"] == "campaign_suspended"
    replayer.append_records("campaign.jsonl", records[:-1])
    session = run_watch.WatchSession(replayer.target)
    try:
        replayer.append_records("campaign.jsonl", records[-1:])
        assert session.poll()[1] == 1
        assert session.poll() == ([], 1)
    finally:
        session.close()
    session = run_watch.WatchSession(source)
    try:
        assert session.initial_ending is None
        assert session.poll()[1] is None
        assert session.poll()[1] is None
    finally:
        session.close()
    script, lines = Script(lambda: None), []
    assert follow(source, script, lines) == 130
    assert script.sleeps == 2


def test_one_poll_stops_its_registry_at_the_first_new_suspension(
    suspended_campaign,
    tmp_path,
):
    from evolution.tree.run_reader import measurement_infos, open_run

    replayer = fakes.JournalReplayer(suspended_campaign, tmp_path / "run")
    journal = "campaign.jsonl"
    records = replayer.lines(journal)
    cut = next(
        i for i, raw in enumerate(records) if json.loads(raw)["event"] == "campaign_suspended"
    )
    replayer.append_records(journal, records[:cut])
    before_ids = {info.id for info in measurement_infos(open_run(replayer.target).snapshot)}
    resumed_ids = {
        info.id for info in measurement_infos(open_run(suspended_campaign).snapshot)
    } - before_ids
    assert resumed_ids, "genuine resume must bind a new measurement"
    for mid in resumed_ids:
        measurement = f"measurements/{mid}/events.jsonl"
        replayer.append_records(measurement, replayer.lines(measurement))
    session = run_watch.WatchSession(replayer.target)
    try:
        # Suspension, resume, new binding and finish arrive between two polls.
        replayer.append_records(journal, records[cut:])
        lines, code = session.poll()
        assert code == 1
        assert not any(mid in line for mid in resumed_ids for line in lines), lines
        assert not any("campaign finished" in line for line in lines)
        assert session.records[-1].event == "campaign_suspended"
        assert len(session.records) == cut + 1
        assert session.poll() == ([], 1)
    finally:
        session.close()


def test_evaluation_completion_new_and_finished(evaluation, tmp_path):
    replayer, records = replay(evaluation, tmp_path / "run", "evaluation.jsonl")
    lines = []
    assert (
        follow(
            replayer.target,
            Script(lambda: replayer.append_bytes("evaluation.jsonl", records[-1])),
            lines,
        )
        == 0
    )
    assert any("evaluation completed" in line for line in lines)
    assert follow(evaluation, Script(), []) == 0


@pytest.mark.parametrize("shape", ["missing", "file", "symlink", "ambiguous", "foreign"])
def test_cli_refusals_are_path_free(campaign, tmp_path, monkeypatch, shape):
    target = tmp_path / "PRIVATE-PATH"
    if shape == "file":
        target.touch()
    elif shape in ("symlink", "ambiguous", "foreign"):
        target.mkdir()
        if shape == "symlink":
            (target / "campaign.jsonl").symlink_to(campaign / "campaign.jsonl")
        elif shape == "ambiguous":
            (target / "campaign.jsonl").touch()
            (target / "evaluation.jsonl").touch()
        else:
            monkeypatch.setattr("os.getuid", lambda: -1)
    result = runner.invoke(tree_app, ["watch", "--run-dir", str(target)])
    assert result.exit_code == 2, result.output
    assert "refused:" in result.stderr
    assert "PRIVATE-PATH" not in result.output


@pytest.mark.parametrize("fault", ["corrupt", "truncate", "replace"])
def test_journal_fault_mid_follow_is_path_free(campaign, tmp_path, monkeypatch, fault):
    replayer, _ = replay(campaign, tmp_path / "PRIVATE-PATH", "campaign.jsonl", 2)
    path = replayer.target / "campaign.jsonl"

    def damage():
        if fault == "corrupt":
            replayer.append_bytes("campaign.jsonl", b"PRIVATE-TASK-DATA\n")
        elif fault == "truncate":
            path.write_bytes(b"")
        else:
            replacement = replayer.target / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)

    original = run_watch.follow
    script = Script(damage)
    monkeypatch.setattr(
        run_watch, "follow", lambda path, **kwargs: original(path, sleep=script.sleep, **kwargs)
    )
    result = runner.invoke(tree_app, ["watch", "--run-dir", str(replayer.target)])
    assert result.exit_code == 1, result.output
    assert "journal unavailable:" in result.stderr
    assert "PRIVATE-" not in result.output
    assert ("corrupt_suffix" if fault == "corrupt" else "journal_rewritten") in result.stderr


def test_idle_wall_prefix_and_no_history_progress_replay(campaign, tmp_path):
    replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl", 2)
    lines = []
    script = Script(*(lambda: None for _ in range(360)))
    assert follow(replayer.target, script, lines) == 130
    idle = [line for line in lines if "no new records" in line]
    assert len(idle) == 2
    assert idle[0].startswith("12:34:56  no new records for 1m")


def test_follow_flushes_pending_measurement_progress_on_idle_poll(campaign12, tmp_path):
    campaign = campaign12
    original = fakes.JournalReplayer(campaign, tmp_path / "run")
    count = (
        next(
            i
            for i, raw in enumerate(original.lines("campaign.jsonl"))
            if json.loads(raw)["event"] == "measurement_bound"
        )
        + 1
    )
    replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl", count)
    from evolution.tree.run_reader import measurement_infos, open_run

    mid = measurement_infos(open_run(campaign).snapshot)[0].id
    journal = f"measurements/{mid}/events.jsonl"
    records = replayer.lines(journal)
    scored = [i for i, raw in enumerate(records) if json.loads(raw)["event"] == "scoring_finished"]
    # Two of six scores arrive inside the rate limit; no completion flush applies.
    replayer.append_records(journal, records[: scored[0]])
    actions = [lambda: replayer.append_records(journal, records[scored[0] : scored[1] + 1])]
    script = Script(*actions, *(lambda: None for _ in range(10)))
    lines = []
    assert follow(replayer.target, script, lines) == 130
    progress = [
        line for line in lines if "pairs scored" in line and re.match(r"\d\d:\d\d:\d\d", line)
    ]
    assert any("2/" in line for line in progress), progress


def test_silent_history_never_flushes_pending_old_progress(campaign12, tmp_path):
    campaign = campaign12
    original = fakes.JournalReplayer(campaign, tmp_path / "run")
    count = (
        next(
            i
            for i, raw in enumerate(original.lines("campaign.jsonl"))
            if json.loads(raw)["event"] == "measurement_bound"
        )
        + 1
    )
    replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl", count)
    from evolution.tree.run_reader import measurement_infos, open_run

    mid = measurement_infos(open_run(campaign).snapshot)[0].id
    journal = f"measurements/{mid}/events.jsonl"
    records = replayer.lines(journal)
    scored = [i for i, raw in enumerate(records) if json.loads(raw)["event"] == "scoring_finished"]
    replayer.append_records(journal, records[: scored[1] + 1])
    lines = []
    assert follow(replayer.target, Script(*(lambda: None for _ in range(11))), lines) == 130
    assert sum("pairs scored" in line for line in lines) <= 1  # snapshot only


def test_start_snapshot_measurement_counts_use_the_admitted_head(campaign12, tmp_path, monkeypatch):
    replayer = fakes.JournalReplayer(campaign12, tmp_path / "run")
    primary = replayer.lines("campaign.jsonl")
    count = (
        next(i for i, raw in enumerate(primary) if json.loads(raw)["event"] == "measurement_bound")
        + 1
    )
    replayer.append_records("campaign.jsonl", primary[:count])
    from evolution.tree.run_reader import measurement_infos, open_run

    mid = measurement_infos(open_run(campaign12).snapshot)[0].id
    journal = f"measurements/{mid}/events.jsonl"
    records = replayer.lines(journal)
    scored = [i for i, raw in enumerate(records) if json.loads(raw)["event"] == "scoring_finished"]
    replayer.append_records(journal, records[: scored[0] + 1])
    original = run_watch.JournalTail.poll
    advanced = False

    def poll(tail):
        nonlocal advanced
        result = original(tail)
        if tail._file_name == "events.jsonl" and not advanced:
            advanced = True
            replayer.append_records(journal, records[scored[0] + 1 : scored[1] + 1])
        return result

    monkeypatch.setattr(run_watch.JournalTail, "poll", poll)
    session = run_watch.WatchSession(replayer.target)
    try:
        assert advanced
        assert any("1/6 pairs scored" in line for line in session.snapshot.incomplete), (
            session.snapshot.incomplete
        )
    finally:
        session.close()


def test_progress_session_can_wait_before_run_directory_exists(campaign, tmp_path):
    target = tmp_path / "unborn"
    script = Script()
    session = run_watch.WatchSession(target, clock=script.clock, allow_unborn=True)
    try:
        assert session.heads == {}
        assert session.records == []
        assert session.snapshot.snapshot_line() is None
        assert session.poll() == ([], None)
        replayer = fakes.JournalReplayer(campaign, target)
        replayer.append_records("campaign.jsonl", replayer.lines("campaign.jsonl")[:2])
        lines, code = session.poll()
        assert code is None
        assert any("campaign created" in line for line in lines)
        assert any("baseline" in line for line in lines)
    finally:
        session.close()


def test_explicit_start_heads_replays_only_prefix_then_emits_append(campaign, tmp_path):
    replayer, records = replay(campaign, tmp_path / "run", "campaign.jsonl", 2)
    heads = {"campaign.jsonl": sum(map(len, records[:2]))}
    replayer.append_records("campaign.jsonl", records[2:])
    lines = []
    assert follow(replayer.target, Script(), lines, start_heads=heads) == 0
    assert any("attempt opened" in line for line in lines)
    assert sum("campaign finished" in line for line in lines) == 1
    assert any(line.endswith("running") for line in lines)


def test_waits_for_partial_campaign_creation_before_selecting_family(campaign, tmp_path):
    replayer = fakes.JournalReplayer(campaign, tmp_path / "run")
    records = replayer.lines("campaign.jsonl")
    replayer.append_bytes("campaign.jsonl", records[0][:20])
    script = Script(
        lambda: replayer.append_bytes("campaign.jsonl", records[0][20:]),
        lambda: replayer.append_records("campaign.jsonl", records[1:]),
    )
    lines = []
    assert follow(replayer.target, script, lines) == 0
    # ADR-0032: a /15 fixture is born as family 14.
    assert any("campaign created · family 14" in line for line in lines)


def test_start_heads_are_raw_byte_offsets(campaign, tmp_path):
    replayer = fakes.JournalReplayer(campaign, tmp_path / "run")
    records = replayer.lines("campaign.jsonl")
    padded = records[0][:-1] + b" " * 3000 + b"\n"
    replayer.append_records("campaign.jsonl", [padded, *records[1:]])
    session = run_watch.WatchSession(
        replayer.target, start_heads={"campaign.jsonl": len(padded) + len(records[1])}
    )
    try:
        assert session.snapshot.stage == "baseline"
        lines, code = session.poll()
        assert code == 0
        assert any("attempt opened" in line for line in lines)
    finally:
        session.close()


@pytest.mark.parametrize("live", [False, True])
def test_no_writes_locks_or_task_data(campaign, monkeypatch, tmp_path, live):
    import fcntl
    import os

    needles = fakes.task_data_needles(campaign)
    if live:
        replayer, _ = replay(campaign, tmp_path / "run", "campaign.jsonl")
        campaign = replayer.target
    before = {
        path.relative_to(campaign): path.read_bytes()
        for path in campaign.rglob("*")
        if path.is_file()
    }
    original = os.open

    def checked_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        return original(path, flags, *args, **kwargs)

    def forbidden(*args):
        pytest.fail("watch must never take or test a lock")

    monkeypatch.setattr(os, "open", checked_open)
    monkeypatch.setattr(fcntl, "flock", forbidden)
    monkeypatch.setattr(fcntl, "lockf", forbidden)
    lines = []
    assert follow(campaign, Script(), lines) == (130 if live else 0)
    output = "\n".join(lines)
    for needle in needles:
        if len(needle) < 3:
            assert not re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", output)
        else:
            assert needle not in output
    assert before == {
        path.relative_to(campaign): path.read_bytes()
        for path in campaign.rglob("*")
        if path.is_file()
    }


def test_no_task_data_in_newly_followed_records(campaign, tmp_path):
    replayer, records = replay(campaign, tmp_path / "run", "campaign.jsonl", 1)
    lines = []
    assert (
        follow(
            replayer.target,
            Script(lambda: replayer.append_records("campaign.jsonl", records[1:])),
            lines,
        )
        == 0
    )
    assert any("attempt opened" in line for line in lines)
    output = "\n".join(lines)
    for needle in fakes.task_data_needles(campaign):
        if len(needle) < 3:
            assert not re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", output)
        else:
            assert needle not in output
