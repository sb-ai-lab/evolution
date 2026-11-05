"""The read-only run reader (Task 2 of #197): kind, family, registry, tries, tails.

`run_reader` is the layer the `trace`, `watch` and `--progress` commands read
through. These tests pin its contract against genuine runs (one per family,
plus retired frozen fixtures and standalone evaluations) and against a
`JournalReplayer` copy of a genuine journal for the incremental tail.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.tree.campaign import CampaignFamilyUnsupported
from evolution.tree.campaign_schema import CampaignSnapshot
from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
from evolution.tree.run_reader import (
    JournalRewritten,
    JournalTail,
    JournalUnreadable,
    MeasurementInfo,
    RunKind,
    RunRefused,
    TryState,
    measurement_infos,
    open_run,
    tries,
)
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalFault
from tests import run_observability_fakes as fakes

pytestmark = pytest.mark.slow

CHAMPION = "m-r00-champion"


@pytest.fixture(scope="module")
async def campaign_run(tmp_path_factory):
    return await fakes.genuine_campaign_run(tmp_path_factory.mktemp("campaign"))


@pytest.fixture(scope="module")
async def family14_run(tmp_path_factory):
    return await fakes.genuine_family14_run(tmp_path_factory.mktemp("family14"))


@pytest.fixture(scope="module")
async def family14_refresh_run(tmp_path_factory):
    return await fakes.genuine_family14_refresh_run(tmp_path_factory.mktemp("family14-refresh"))


@pytest.fixture(scope="module")
async def repeated_campaign_run(tmp_path_factory):
    return await fakes.genuine_repeated_campaign_run(tmp_path_factory.mktemp("repeated_campaign"))


@pytest.fixture(scope="module")
async def continued_run(tmp_path_factory):
    return await fakes.genuine_continued_run(tmp_path_factory.mktemp("continued"))


@pytest.fixture(scope="module")
async def rebaselined_run(tmp_path_factory):
    return await fakes.genuine_rebaselined_run(tmp_path_factory.mktemp("rebaselined"))


@pytest.fixture(scope="module")
async def evaluation_run(tmp_path_factory):
    return await fakes.genuine_evaluation_run(tmp_path_factory.mktemp("evaluation"))


@pytest.fixture(scope="module")
async def retried_evaluation_run(tmp_path_factory):
    return await fakes.genuine_retried_evaluation_run(tmp_path_factory.mktemp("retried"))


@pytest.fixture(scope="module")
async def repair_evaluation_run(tmp_path_factory):
    return await fakes.genuine_repair_evaluation_run(tmp_path_factory.mktemp("repair"))


@pytest.fixture(scope="module")
async def call_failure_evaluation_run(tmp_path_factory):
    return await fakes.genuine_call_failure_evaluation_run(tmp_path_factory.mktemp("call-failure"))


@pytest.fixture(scope="module")
async def crashed_resumed_run(tmp_path_factory):
    return await fakes.genuine_crashed_resumed_run(tmp_path_factory.mktemp("crashed"))


def _journal_records(run: Path, measurement_id: str) -> tuple[Envelope, ...]:
    from evolution.tree.execution.journal import inspect_journal

    return inspect_journal(fakes.measurement_journal(run, measurement_id).parent).records


def _tries_of(run: Path, measurement_id: str) -> list[TryState]:
    records = _journal_records(run, measurement_id)
    return tries(records, records[0].payload["identity"])


def _infos_by_id(run: Path) -> dict[str, MeasurementInfo]:
    handle = open_run(run)
    return {info.id: info for info in measurement_infos(handle.snapshot)}


# -- kind and family (decisions 1 and 2) ---------------------------------------


def test_an_evaluation_run_dir_opens_with_no_family(evaluation_run):
    handle = open_run(evaluation_run)
    assert handle.kind is RunKind.EVALUATION
    assert handle.family is None
    assert not isinstance(handle.snapshot, CampaignSnapshot)


def test_a_directory_holding_both_journals_is_ambiguous(
    repeated_campaign_run, evaluation_run, tmp_path
):
    both = tmp_path / "both"
    both.mkdir()
    shutil.copyfile(repeated_campaign_run / "campaign.jsonl", both / "campaign.jsonl")
    shutil.copyfile(evaluation_run / "evaluation.jsonl", both / "evaluation.jsonl")
    with pytest.raises(RunRefused, match="run_dir_ambiguous") as caught:
        open_run(both)
    assert caught.value.code == "run_dir_ambiguous"


def test_a_directory_holding_neither_journal_is_not_a_run_directory(tmp_path):
    (tmp_path / "stray.txt").write_text("nothing here")
    with pytest.raises(RunRefused, match="not_a_run_directory") as caught:
        open_run(tmp_path)
    assert caught.value.code == "not_a_run_directory"


def test_an_absent_path_and_a_file_path_are_not_run_directories(tmp_path):
    with pytest.raises(RunRefused, match="not_a_run_directory"):
        open_run(tmp_path / "absent")
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(RunRefused, match="not_a_run_directory"):
        open_run(file_path)


def test_an_unknown_family_integer_is_a_refusal(repeated_campaign_run, tmp_path):
    forged = tmp_path / "forged"
    forged.mkdir()
    (created,) = (repeated_campaign_run / "campaign.jsonl").read_text().splitlines()[:1]
    record = json.loads(created)
    record["payload"]["family_version"] = 99
    (forged / "campaign.jsonl").write_text(json.dumps(record) + "\n")
    with pytest.raises(CampaignFamilyUnsupported) as caught:
        open_run(forged)
    assert caught.value.journaled == 99


# -- the measurement registry (decision 5) -------------------------------------


def test_family_14_registry_roles_and_final_member_roles(family14_run):
    infos = _infos_by_id(family14_run)
    assert infos[CHAMPION].role == "baseline"
    assert infos["m-r01-a01"].role == "candidate"
    champion = infos["m-r01-final-01-champion"]
    finalist = infos["m-r01-final-01-a01"]
    assert (champion.role, champion.cohort_role) == ("final", "champion")
    assert (finalist.role, finalist.cohort_role) == ("final", "finalist")


def test_a_family_14_refresh_measurement_reports_the_refresh_role(family14_refresh_run):
    infos = _infos_by_id(family14_refresh_run)
    assert infos["m-r02-refresh"].role == "refresh"
    assert infos["m-r03-refresh"].role == "refresh"
    assert infos["m-r02-refresh"].complete is True
    # Everyone else keeps the registry role.
    assert infos[CHAMPION].role == "baseline"
    assert infos["m-r02-a01"].role == "candidate"


def test_a_continue_re_bind_reports_its_epoch_and_source(continued_run):
    infos = _infos_by_id(continued_run)
    rebound = {
        measurement_id: info
        for measurement_id, info in infos.items()
        if info.rebound_from is not None
    }
    assert rebound
    for measurement_id, info in rebound.items():
        assert measurement_id.endswith("@e1")
        assert info.epoch == 1
        assert measurement_id == f"{info.rebound_from}@e1"
        assert infos[info.rebound_from].epoch == 0


def test_a_plain_id_measurement_bound_after_a_rebaseline_reports_epoch_one(rebaselined_run):
    """The epoch is the binding history's fact, never parsed from the `@e<k>`
    suffix: round 2's plain-id attempt measurement carries `epoch` 1, and the
    test renames nothing — it only asserts the registry projection."""
    infos = _infos_by_id(rebaselined_run)
    plain = [info for info in infos.values() if "@e" not in info.id and info.epoch == 1]
    assert plain, "round 2 must bind plain-id measurements under epoch 1"
    assert all(info.round == 2 for info in plain)
    assert infos["m-r02-champion@e1"].epoch == 1
    assert all(info.epoch == 0 for info in infos.values() if info.round == 1)


def test_an_evaluation_run_has_one_evaluation_child(evaluation_run):
    handle = open_run(evaluation_run)
    (info,) = measurement_infos(handle.snapshot)
    assert info.id == "evaluation"
    assert info.role == "evaluation"
    assert info.round is None
    assert info.attempt_id is None
    assert info.epoch == 0
    assert info.rebound_from is None
    assert info.cohort_role is None
    assert info.complete is True
    bound = next(
        record for record in handle.snapshot.records if record.event == "measurement_bound"
    )
    assert info.candidate_digest == bound.payload["identity"]["candidate_digest"]


# -- tries (decision 6) ---------------------------------------------------------


def test_one_try_per_pair_in_journal_order_on_a_campaign(campaign_run):
    handle = open_run(campaign_run)
    for info in measurement_infos(handle.snapshot):
        records = _journal_records(campaign_run, info.id)
        states = tries(records, records[0].payload["identity"])
        started = [record for record in records if record.event == "rollout_started"]
        assert len(states) == len(started)
        assert [(s.case_id, s.sample_id) for s in states] == [
            (p["case_id"], p["sample_id"]) for p in (record.payload for record in started)
        ]
        schedule = {
            (entry["case_id"], entry["sample_id"]): entry["repeat"]
            for entry in records[0].payload["identity"]["schedule"]
        }
        sends = [record for record in records if record.event == "target_send_started"]
        for state in states:
            assert state.retry_ordinal == 0
            assert state.repair is False
            assert state.imported is None
            assert state.repeat == schedule[(state.case_id, state.sample_id)]
            assert state.rollout.outcome == "completed"
            assert (state.rollout.kind, state.rollout.owner) == ("score", None)
            assert state.rollout.error_code is None
            assert state.rollout.target_calls == sum(
                1
                for record in sends
                if (record.payload["case_id"], record.payload["sample_id"])
                == (state.case_id, state.sample_id)
            )
            assert state.rollout.started == state.start.to_payload()["timestamp"]
            assert state.rollout.finished == state.terminal.to_payload()["timestamp"]
            assert state.scoring.status == "scored"
            assert state.scoring.started is not None
            assert state.scoring.terminal is not None
            assert state.scoring.terminal.event == "scoring_finished"


def test_retries_are_their_own_tries_in_ascending_ordinal_order(retried_evaluation_run):
    records = _journal_records(retried_evaluation_run, "evaluation")
    states = tries(records, records[0].payload["identity"])
    ordinals = [state.retry_ordinal for state in states]
    assert len(states) >= 3
    assert ordinals == sorted(ordinals)
    assert ordinals[0] == 0
    assert len(set(ordinals)) == len(ordinals)
    failed = [state for state in states if state.rollout.outcome == "target_failed"]
    assert failed
    assert all(state.rollout.kind == "indeterminate" for state in failed)
    assert all(state.rollout.owner == "target" for state in failed)
    assert all(state.scoring.status == "not_started" for state in failed)
    last = states[-1]
    assert last.rollout.outcome == "completed"
    assert last.scoring.status == "scored"


def test_a_repair_try_carries_the_repair_flag(repair_evaluation_run):
    records = _journal_records(repair_evaluation_run, "evaluation")
    states = tries(records, records[0].payload["identity"])
    repairs = [state for state in states if state.repair]
    assert len(repairs) == 1
    (repair,) = repairs
    assert repair.rollout.outcome == "completed"
    assert repair.scoring.status == "scored"
    plain = [state for state in states if not state.repair]
    assert plain and all(state.scoring.status != "scored" for state in plain)


def test_a_failed_target_call_reports_its_error_code_and_call_count(
    call_failure_evaluation_run,
):
    records = _journal_records(call_failure_evaluation_run, "evaluation")
    states = tries(records, records[0].payload["identity"])
    assert states
    for state in states:
        assert state.rollout.outcome == "completed"
        assert state.rollout.error_code == "internal"
        assert state.rollout.target_calls == 2  # both call attempts counted
    failed = [record for record in records if record.event == "target_send_failed"]
    assert failed and all(record.payload["error_code"] == "internal" for record in failed)


def test_a_crash_and_resume_writes_no_duplicate_tries(crashed_resumed_run):
    handle = open_run(crashed_resumed_run)
    for info in measurement_infos(handle.snapshot):
        records = _journal_records(crashed_resumed_run, info.id)
        states = tries(records, records[0].payload["identity"])
        started = [
            record for record in records if record.event in ("rollout_started", "sample_imported")
        ]
        assert len(states) == len(started)
        keys = [(state.case_id, state.sample_id, state.retry_ordinal) for state in states]
        assert len(set(keys)) == len(keys)
        assert all(state.terminal is not None or state.imported is not None for state in states)


# -- unknown entries change nothing and are never opened (decision 4) -----------


def test_unknown_entries_change_nothing_and_are_never_opened(campaign_run, tmp_path, monkeypatch):
    polluted = tmp_path / "run"
    shutil.copytree(campaign_run, polluted)
    (polluted / "connection" / "op1").mkdir(parents=True)
    (polluted / "connection" / "op1" / "events.jsonl").write_text("garbage\n")
    (polluted / "connection-logs").mkdir(exist_ok=True)
    (polluted / "connection-logs" / "out.txt").write_text("noise")
    (polluted / "logs").mkdir()
    (polluted / "logs" / "evolve.log").write_text("noise")
    (polluted / "stray.bin").write_bytes(b"\x00")

    clean_infos = _infos_by_id(campaign_run)
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    handle = open_run(polluted)
    infos = measurement_infos(handle.snapshot)
    monkeypatch.undo()

    assert {info.id for info in infos} == set(clean_infos)
    for info in infos:
        assert _tries_of(polluted, info.id) == _tries_of(campaign_run, info.id)
    for path in opened:
        assert "connection" not in path
        assert "stray" not in path
        assert "/logs/" not in path


# -- the incremental tail (decision 3) ------------------------------------------

JOURNAL = f"measurements/{CHAMPION}/events.jsonl"


def _open_tail(replay: Path) -> JournalTail:
    return JournalTail(
        replay / "measurements" / CHAMPION,
        "events.jsonl",
        family=S1_FAMILY,
        state=Lifecycle(),
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
    )


def _replayer(campaign_run, tmp_path) -> fakes.JournalReplayer:
    return fakes.JournalReplayer(campaign_run, tmp_path / "replay")


def test_a_tail_returns_complete_records_once_in_order(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:3])
    tail = _open_tail(replayer.target)
    try:
        first = tail.poll()
        assert [record.to_payload()["sequence"] for record in first] == [1, 2, 3]
        assert tail.poll() == ()
        replayer.append_records(JOURNAL, lines[3:8])
        second = tail.poll()
        assert [record.to_payload()["sequence"] for record in second] == list(range(4, 9))
        assert tail.poll() == ()
    finally:
        tail.close()


def test_a_half_written_last_line_is_returned_whole_once_completed(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:5])
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == 5
        partial = lines[5][:-1]  # drop the newline: a half-written record
        cut = len(partial) // 2
        replayer.append_bytes(JOURNAL, partial[:cut])
        assert tail.poll() == ()
        assert tail.position < sum(len(line) for line in lines[:6])
        replayer.append_bytes(JOURNAL, partial[cut:] + b"\n")
        (whole,) = tail.poll()
        assert whole == Envelope.from_bytes(lines[5], family=S1_FAMILY)
    finally:
        tail.close()


def test_a_record_split_across_three_polls_arrives_once(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:4])
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == 4
        (line,) = lines[4:5]
        cut = len(line) // 3
        replayer.append_bytes(JOURNAL, line[:cut])
        assert tail.poll() == ()
        replayer.append_bytes(JOURNAL, line[cut : 2 * cut])
        assert tail.poll() == ()
        replayer.append_bytes(JOURNAL, line[2 * cut :])
        (record,) = tail.poll()
        assert record == Envelope.from_bytes(line, family=S1_FAMILY)
    finally:
        tail.close()


def test_a_corrupt_complete_line_is_unreadable(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:5])
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == 5
        replayer.append_bytes(JOURNAL, b'{"schema_version": 1, "broken"\n')
        with pytest.raises(JournalUnreadable, match="corrupt_suffix"):
            tail.poll()
    finally:
        tail.close()


def test_a_truncation_below_the_offset_is_a_rewritten_journal(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines)
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == len(lines)
        journal = replayer.target / JOURNAL
        journal.write_bytes(journal.read_bytes()[: len(journal.read_bytes()) // 2])
        with pytest.raises(JournalRewritten):
            tail.poll()
    finally:
        tail.close()


def test_a_replaced_file_is_a_rewritten_journal(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines)
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == len(lines)
        journal = replayer.target / JOURNAL
        replacement = journal.with_name("events.jsonl.replacement")
        replacement.write_bytes(journal.read_bytes())
        os.replace(replacement, journal)
        with pytest.raises(JournalRewritten):
            tail.poll()
    finally:
        tail.close()


def test_a_deleted_journal_mid_tail_is_a_rewritten_journal(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:5])
    tail = _open_tail(replayer.target)
    try:
        assert len(tail.poll()) == 5
        (replayer.target / JOURNAL).unlink()
        with pytest.raises(JournalRewritten):
            tail.poll()
    finally:
        tail.close()


def test_a_permission_change_mid_tail_is_a_journal_fault(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:5])
    tail = _open_tail(replayer.target)
    directory = replayer.target / "measurements" / CHAMPION
    try:
        assert len(tail.poll()) == 5
        directory.chmod(0o000)
        with pytest.raises(JournalFault):
            tail.poll()
    finally:
        directory.chmod(0o700)
        tail.close()


def test_a_symlinked_journal_is_refused(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    replayer.append_records(JOURNAL, replayer.lines(JOURNAL)[:2])
    journal = replayer.target / JOURNAL
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target_file = elsewhere / "events.jsonl"
    target_file.write_bytes(journal.read_bytes())
    journal.unlink()
    journal.symlink_to(target_file)
    with pytest.raises(JournalFault):
        _open_tail(replayer.target)


def test_a_foreign_owned_directory_is_refused(campaign_run, tmp_path, monkeypatch):
    replayer = _replayer(campaign_run, tmp_path)
    replayer.append_records(JOURNAL, replayer.lines(JOURNAL)[:2])
    real_fstat = os.fstat

    def foreign(fd):
        info = real_fstat(fd)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=info.st_uid + 1,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_ino=info.st_ino,
        )

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(JournalFault):
        _open_tail(replayer.target)


def test_a_hard_linked_journal_is_refused(campaign_run, tmp_path):
    replayer = _replayer(campaign_run, tmp_path)
    replayer.append_records(JOURNAL, replayer.lines(JOURNAL)[:2])
    journal = replayer.target / JOURNAL
    os.link(journal, journal.with_name("events-linked.jsonl"))
    with pytest.raises(JournalFault):
        _open_tail(replayer.target)


def test_the_tail_opens_read_only_and_never_locks(campaign_run, tmp_path, monkeypatch):
    replayer = _replayer(campaign_run, tmp_path)
    lines = replayer.lines(JOURNAL)
    replayer.append_records(JOURNAL, lines[:5])

    opened: list[tuple[str, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append((str(path), flags))
        return real_open(path, flags, mode, *args, **kwargs)

    def no_lock(*args, **kwargs):
        raise AssertionError("readers never take a lock")

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(fcntl, "flock", no_lock)
    monkeypatch.setattr(fcntl, "lockf", no_lock)
    tail = _open_tail(replayer.target)
    try:
        tail.poll()
    finally:
        tail.close()
    journal_opens = [flags for path, flags in opened if Path(path).name == "events.jsonl"]
    assert journal_opens
    for flags in journal_opens:
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT)
