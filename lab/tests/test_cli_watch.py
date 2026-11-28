"""Test `lab watch` with a scripted service and a fake clock."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json

import pytest

from evolution_lab.cli.client import ApiError, ClientError, RequestTimeout
from test_service_cli import FAMILY14, _event_frame, _family14_connection_suspended, _view, invoke

OP = "op14"
STATUS, CAMPAIGN, EVENTS = f"/v1/operations/{OP}", f"/v1/operations/{OP}/campaign", f"/v1/operations/{OP}/events"
WATCH = ["--socket", "/tmp/lab.sock", "watch", OP]
HEADER = f"watch {OP} campaign start 1  2026-10-14 09:00:00"
CLEANUP_HINT = f"hint: run lab cleanup operation {OP}, then read lab status {OP}"
FAILURE = {"code": "spawn_failed", "stage": "spawning", "message": "m"}
SCORED = {"kind": "evaluation", "coverage": {"expected": 4, "scored": 4}, "observed_at": "2026-10-14T09:00:00Z"}


class FakeClock:
    def __init__(self, start=datetime(2026, 10, 14, 9, 0, 0)):
        self.t, self.base, self.slept = 0.0, start, []
        self.readers = []

    def monotonic(self):
        return self.t

    def now(self):
        return self.base + timedelta(seconds=self.t)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class FakeEventReader:
    """One outstanding next, delivered at its scripted time or after a virtual deadline.

    Capture the next event and its timestamp, then wait virtually without waiting for wall time.
    Production's event-reader thread has the same one-outstanding-read boundary.
    """
    def __init__(self, events, clock):
        self.events, self.clock, self.pending, self.closed = iter(events), clock, None, False
        clock.readers.append(self)

    def next(self, timeout=None):
        if self.pending is None:
            began = self.clock.t
            try:
                result = (True, next(self.events))
            except BaseException as error:
                result = (False, error)
            self.pending = self.clock.t, result
            self.clock.t = began
        at, (ok, value) = self.pending
        if timeout is not None and at > self.clock.t + timeout:
            self.clock.t += timeout
            return None
        self.clock.t = max(self.clock.t, at)
        self.pending = None
        if not ok:
            raise value
        return value

    def close(self):
        self.closed = True
        close = getattr(self.events, "close", None)
        if close is not None:
            close()


@pytest.fixture
def clock(monkeypatch):
    from evolution_lab.cli import watch
    fake = FakeClock()
    monkeypatch.setattr(watch, "CLOCK", watch.Clock(fake.monotonic, fake.now, fake.sleep))
    monkeypatch.setattr(watch, "EventReader", lambda events: FakeEventReader(events, fake), raising=False)
    return fake


class WatchClient:
    """GET answers by path (a queue whose last answer repeats; an exception is raised), one scripted event stream per
    ``stream_events`` call (frames, exceptions, or ``("tick", seconds)`` to move the clock); every path recorded. An
    unscripted path raises KeyError, so an unexpected read fails the test."""

    def __init__(self, clock, gets, streams=()):
        self.clock, self.calls, self.schemas = clock, [], []
        self.read_times = []
        self.gets = {path: list(answers) for path, answers in gets.items()}
        self.streams = [list(stream) for stream in streams]

    def request_json(self, method, path, *, payload=None, mutation=False, expected_schema=None, mutation_key=None):
        self.calls.append(path)
        self.read_times.append((path, self.clock.t if self.clock is not None else None))
        self.schemas.append((path, expected_schema))
        answers = self.gets[path]
        value = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def stream_events(self, path):
        self.calls.append(path)
        for item in (self.streams.pop(0) if self.streams else []):
            if isinstance(item, tuple):
                self.clock.t += item[1]
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item


def _progress(phase="evaluation", current=1, closed=0, running=1):
    return {"kind": "campaign", "phase": phase, "stages": [phase],
            "rounds": {"completed": 2 if current is None else current - 1, "current": current, "planned": 2},
            "attempts": {"current_round": {"closed": closed, "running": running, "planned": 2}, "total": 4},
            "coverage": None, "policy_state": "present", "observed_at": "2026-10-14T09:00:00Z"}


DONE = _progress("finished", None, running=0)


def _line(progress):
    from evolution_lab.cli.output import _progress_line
    return _progress_line({"progress": progress, "progress_state": "present"})


def _status(control_state="running", *, start=1, progress=None, core=None, resume=None, kind="campaign",
            failure=None, cleanup=None, persistence=None, refusal=None):
    return {"schema": "lab-operation-status/8", "id": OP, "kind": kind, "control_state": control_state,
            "target_ref": {"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g1"},
            "refusal": refusal, "target_cleanup": cleanup or {"state": "not_needed", "complete": True}, "failure": failure,
            "results_state": "present",
            "result": {"kind": kind, "core": core, "persistence": persistence or {"state": "saved", "failure": None}},
            "progress": progress, "progress_state": "present" if progress else "unavailable",
            "starts": [{"start": n, "refusal": refusal if n == start else None} for n in range(1, start + 1)], "resume": resume}


def _frame(state="running", **payload):
    frame = _event_frame(state)
    frame["payload"].update(payload)
    return {"event": "status", "data": frame}


def _view14():
    return _view(json.loads((FAMILY14 / "observe-result.json").read_text()))


def _texts(view):
    from evolution_lab.cli.output import campaign_keyed_lines
    return [text for _, text in campaign_keyed_lines(view)]


def _run(client, *extra):
    return invoke([*WATCH, *extra], client)


def _refusal(code):
    return ApiError({"error": {"code": code, "stage": "transport", "message": "m"}})


# -- Task 7: the command and the rules of the summary


def test_watch_json_is_a_usage_error():
    client = WatchClient(None, {})
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "watch", OP], client)
    assert (code, stdout, client.calls) == (2, "", [])
    assert stderr == "usage: lab watch is text-only; use lab events for JSON\n"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "x"])
def test_watch_interval_must_be_an_integer_of_at_least_one(value):
    client = WatchClient(None, {})
    code, _, stderr = _run(client, "--interval", value)
    assert code == 2 and client.calls == [] and stderr.startswith("usage:")


@pytest.mark.parametrize(("changes", "expected"), [
    ({}, "finished"),
    ({"progress": _progress("suspended")}, "suspended"),
    ({"progress": None, "progress_state": "unavailable"}, None),
    ({"control_state": "orphaned", "target_cleanup": {"state": "unconfirmed", "complete": False}}, "abnormal"),
    ({"control_state": "cleanup_required", "target_cleanup": {"state": "pending", "complete": None}}, "abnormal"),
    ({"control_state": "released", "target_cleanup": {"state": "manual", "complete": False}}, "abnormal"),
    ({"control_state": "released", "target_cleanup": {"state": "confirmed", "complete": True}}, "finished"),
    ({"failure": FAILURE}, "abnormal"),
    ({"persistence": {"state": "failed", "failure": None}}, "abnormal"),
    ({"progress": _progress("suspended"), "persistence": {"state": "failed", "failure": None}}, "abnormal"),
    ({"progress": None, "failure": FAILURE}, "abnormal"),
])
def test_the_class_follows_the_terminal_frame_abnormal_then_suspended_then_finished(changes, expected):
    from evolution_lab.cli.watch import outcome_class
    assert outcome_class(_frame("finished", **changes)["data"]["payload"]) == expected


@pytest.mark.parametrize(("changes", "lines"), [
    ({"control_state": "orphaned", "target_cleanup": {"state": "unconfirmed", "complete": False}},
     ["abnormal: control_state=orphaned target_cleanup=unconfirmed failure=- persistence=saved", CLEANUP_HINT]),
    ({"control_state": "released", "target_cleanup": {"state": "manual", "complete": False}, "failure": FAILURE},
     ["abnormal: control_state=released target_cleanup=manual failure=spawn_failed persistence=saved", CLEANUP_HINT]),
    ({"failure": FAILURE, "persistence": {"state": "failed", "failure": None}},
     ["abnormal: control_state=finished target_cleanup=not_needed failure=spawn_failed persistence=failed",
      f"hint: the operation failed (spawn_failed); read lab status {OP} and lab report {OP}"]),
    ({"persistence": {"state": "failed", "failure": None}},
     ["abnormal: control_state=finished target_cleanup=not_needed failure=- persistence=failed",
      f"hint: the result was not saved (persistence failed); read lab status {OP}"]),
])
def test_the_abnormal_line_names_every_cause_and_hints_the_first(changes, lines):
    from evolution_lab.cli.watch import abnormal_lines
    payload = _frame("finished", **changes)["data"]["payload"]
    assert abnormal_lines(payload, OP) == lines


def test_a_status_reads_like_a_frame():
    from evolution_lab.cli.watch import facts_of_status
    status = _status("finished", start=3, progress=DONE, persistence={"state": "failed", "failure": None})
    facts = facts_of_status(status)
    assert (facts["start"], facts["control_state"], facts["progress"], facts["persistence"]) == (
        3, "finished", DONE, {"state": "failed", "failure": None})
    assert facts_of_status({**status, "starts": [], "result": None}) == {**facts, "start": 1, "persistence": None}


def test_transient_failures_and_permanent_refusals():
    from evolution_lab.cli.client import RequestInterrupted
    from evolution_lab.cli.watch import transient_code
    assert transient_code(ClientError("transport error")) == "client_error"
    assert transient_code(_refusal("service_stopping")) == "service_stopping"
    assert transient_code(_refusal("not_found")) is None
    assert transient_code(RequestTimeout()) is None and transient_code(RequestInterrupted()) is None


# -- Task 8: snapshot, feed, view re-reads and the summary


def test_a_campaign_finished_before_connecting_prints_the_snapshot_and_the_summary(clock):
    from evolution_lab.cli.output import outcome_line
    view = _view14()
    client = WatchClient(clock, {STATUS: [_status("finished", progress=DONE, core={"suspended": False})],
                                 CAMPAIGN: [view]})
    code, stdout, stderr = _run(client)
    assert (code, stderr) == (0, "")
    assert stdout.splitlines() == [HEADER, *_texts(view), f"09:00:00 {_line(DONE)}",
                                   f"09:00:00 {outcome_line(view['outcome'])}", "watch ended: finished start 1 exit 0"]
    assert client.calls == [STATUS, CAMPAIGN]


def test_an_evaluation_shows_progress_only_and_never_reads_a_campaign_view(clock):
    one, two = ({**SCORED, "coverage": {"expected": 4, "scored": n}} for n in (1, 2))
    client = WatchClient(clock, {STATUS: [_status(kind="evaluation", progress=one),
                                          _status("finished", kind="evaluation", progress=SCORED)]},
                         [[_frame(kind="evaluation", progress=two), _frame("finished", kind="evaluation", progress=SCORED)]])
    code, stdout, _ = _run(client)
    assert code == 0
    assert stdout.splitlines() == [f"watch {OP} evaluation start 1  2026-10-14 09:00:00", "09:00:00 progress: scored 1/4",
                                   "09:00:00 progress: scored 2/4", "09:00:00 progress: scored 4/4",
                                   "watch ended: finished start 1 exit 0"]
    assert client.calls == [STATUS, EVENTS, STATUS]


def test_a_paused_campaign_exits_3_with_the_pause_lines(clock):
    from evolution_lab._suspension import suspension_hint
    from evolution_lab.cli.output import outcome_line
    core = _family14_connection_suspended()
    view = _view(core, {"phase": "suspended"})
    status = _status("finished", progress=_progress("suspended", running=0), core=core,
                     resume={"state": "ready", "reason": None, "target": None})
    client = WatchClient(clock, {STATUS: [status], CAMPAIGN: [view]})
    code, stdout, _ = _run(client)
    assert code == 3
    assert stdout.splitlines()[-5:] == [
        "09:00:00 suspended: connection_incompatible (owner=connection, evidence=stage_record)",
        f"09:00:00 hint: {suspension_hint('connection_incompatible', 'en')}",
        f"09:00:00 continuation: lab resume {OP} --target <ready target of the same bundle> --generation <owner generation>",
        f"09:00:00 {outcome_line(view['outcome'])}",
        "watch ended: suspended start 1 exit 3"]


@pytest.mark.parametrize(("changes", "abnormal", "hint"), [
    ({"control_state": "orphaned", "cleanup": {"state": "unconfirmed", "complete": False}},
     "abnormal: control_state=orphaned target_cleanup=unconfirmed failure=- persistence=saved", CLEANUP_HINT),
    ({"control_state": "cleanup_required", "cleanup": {"state": "pending", "complete": None}},
     "abnormal: control_state=cleanup_required target_cleanup=pending failure=- persistence=saved", CLEANUP_HINT),
    ({"control_state": "released", "cleanup": {"state": "manual", "complete": False}},
     "abnormal: control_state=released target_cleanup=manual failure=- persistence=saved", CLEANUP_HINT),
    ({"control_state": "finished", "failure": FAILURE},
     "abnormal: control_state=finished target_cleanup=not_needed failure=spawn_failed persistence=saved",
     f"hint: the operation failed (spawn_failed); read lab status {OP} and lab report {OP}"),
    ({"control_state": "finished", "persistence": {"state": "failed", "failure": None}},
     "abnormal: control_state=finished target_cleanup=not_needed failure=- persistence=failed",
     f"hint: the result was not saved (persistence failed); read lab status {OP}"),
])
def test_an_abnormal_end_exits_4_naming_its_cause(clock, changes, abnormal, hint):
    client = WatchClient(clock, {STATUS: [_status(kind="evaluation", progress=SCORED, **changes)]})
    code, stdout, _ = _run(client)
    assert code == 4
    assert stdout.splitlines()[-3:] == [f"09:00:00 {abnormal}", f"09:00:00 {hint}", "watch ended: abnormal start 1 exit 4"]


def test_released_with_confirmed_cleanup_is_finished(clock):
    client = WatchClient(clock, {STATUS: [_status("released", kind="evaluation", progress=SCORED,
                                                  cleanup={"state": "confirmed", "complete": True})]})
    assert _run(client)[0] == 0


def test_the_feed_prints_changed_progress_lines_only(clock):
    from evolution_lab.cli.output import outcome_line
    view = _view14()
    one, two = _progress(closed=0), _progress(closed=1, running=0)
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", progress=DONE)], CAMPAIGN: [view]},
                         [[_frame(progress=one), _frame(progress=two), _frame(progress=two), _frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    assert code == 0
    assert stdout.splitlines() == [HEADER, *_texts(view), f"09:00:00 {_line(one)}", f"09:00:00 {_line(two)}",
                                   f"09:00:00 {_line(DONE)}", f"09:00:00 {outcome_line(view['outcome'])}",
                                   "watch ended: finished start 1 exit 0"]
    assert client.calls == [STATUS, CAMPAIGN, EVENTS, STATUS, CAMPAIGN]


@pytest.mark.parametrize(("extra", "times"), [([], [0, 10, 11]), (["--interval", "2"], [0, 2, 4, 11, 11])])
def test_the_view_is_reread_at_most_once_per_interval(clock, extra, times):
    steps = [_progress(closed=n, running=2 - n) for n in range(3)]
    client = WatchClient(clock, {STATUS: [_status(progress=steps[0]), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame(progress=steps[0]), ("tick", 3), _frame(progress=steps[1]), ("tick", 8),
                           _frame(progress=steps[2]), _frame("finished", progress=DONE)]])
    assert _run(client, *extra)[0] == 0
    # Deferred reads run at expiry; the final read bypasses the interval.
    assert [at for path, at in client.read_times if path == CAMPAIGN] == times


def test_a_changed_attempt_and_a_new_attempt_print_one_line_each(clock):
    from evolution_lab.cli.output import campaign_keyed_lines
    before = _view14()
    after = copy.deepcopy(before)
    round_ = next(item for item in after["rounds"] if item["attempts"])
    round_["attempts"][0]["reason"] = "changed_reason"
    added = copy.deepcopy(round_["attempts"][0])
    added["attempt_id"] = "r09-a99"
    round_["attempts"].append(added)
    old = dict(campaign_keyed_lines(before))
    changed = [text for key, text in campaign_keyed_lines(after) if old.get(key) != text]
    assert len(changed) == 2
    one, two = _progress(closed=0), _progress(closed=1, running=0)
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", progress=two)],
                                 CAMPAIGN: [before, after]},
                         [[("tick", 11), _frame(progress=two), _frame("finished", progress=two)]])
    code, stdout, _ = _run(client)
    lines = stdout.splitlines()
    at = lines.index(f"09:00:11 {_line(two)}")
    assert code == 0 and lines[at + 1:at + 3] == [f"09:00:11 {text}" for text in changed]


def test_a_new_local_day_starts_with_a_date_line(monkeypatch):
    from evolution_lab.cli import watch
    fake = FakeClock(datetime(2026, 10, 14, 23, 59, 58))
    monkeypatch.setattr(watch, "CLOCK", watch.Clock(fake.monotonic, fake.now, fake.sleep))
    one, two = ({**SCORED, "coverage": {"expected": 4, "scored": n}} for n in (1, 2))
    client = WatchClient(fake, {STATUS: [_status(kind="evaluation", progress=one),
                                         _status("finished", kind="evaluation", progress=SCORED)]},
                         [[("tick", 5), _frame(kind="evaluation", progress=two),
                           _frame("finished", kind="evaluation", progress=SCORED)]])
    assert _run(client)[1].splitlines() == [
        f"watch {OP} evaluation start 1  2026-10-14 23:59:58", "23:59:58 progress: scored 1/4", "--- 2026-10-15",
        "00:00:03 progress: scored 2/4", "00:00:03 progress: scored 4/4", "watch ended: finished start 1 exit 0"]


def test_a_new_start_on_the_stream_is_a_start_line(clock):
    one = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", start=2, progress=DONE)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame(progress=one, start=2), _frame("finished", progress=DONE, start=2)]])
    code, stdout, _ = _run(client)
    assert code == 0 and "09:00:00 start 2" in stdout.splitlines()
    assert stdout.splitlines()[-1] == "watch ended: finished start 2 exit 0"


PAUSED_CORE = {"suspended": True, "suspensions": [
    {"seq": 1, "reason": "editor_failing", "owner": "editor", "evidence_kind": "stage_record"}]}


@pytest.mark.parametrize(("frame_progress", "final_core", "expected"), [
    (DONE, PAUSED_CORE, (0, "finished")),
    (_progress("suspended", running=0), {"suspended": False}, (3, "suspended")),
])
def test_the_class_comes_from_the_terminal_frame_not_the_last_read(clock, frame_progress, final_core, expected):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), _status("finished", progress=frame_progress,
                                                                                 core=final_core)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=frame_progress)]])
    code, stdout, _ = _run(client)
    assert (code, stdout.splitlines()[-1]) == (expected[0], f"watch ended: {expected[1]} start 1 exit {expected[0]}")


@pytest.mark.parametrize(("final_core", "expected"), [(PAUSED_CORE, (3, "suspended")), ({"suspended": False}, (0, "finished"))])
def test_a_frame_without_progress_takes_the_class_from_the_last_read(clock, final_core, expected):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), _status("finished", core=final_core)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=None, progress_state="unavailable")]])
    code, stdout, _ = _run(client)
    assert (code, stdout.splitlines()[-1]) == (expected[0], f"watch ended: {expected[1]} start 1 exit {expected[0]}")


def test_a_resume_between_the_terminal_frame_and_the_last_read(clock):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), _status("reserved", start=2)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=_progress("suspended", running=0))]])
    code, stdout, _ = _run(client)
    assert code == 3
    assert stdout.splitlines()[-2:] == ["09:00:00 start 2 began after start 1 ended — run lab watch again",
                                        "watch ended: suspended start 1 exit 3"]
    assert " suspended: " not in stdout and "continuation:" not in stdout
    assert client.calls == [STATUS, CAMPAIGN, EVENTS, STATUS]


def test_a_frame_without_progress_and_a_later_start_is_finished(clock):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), _status("running", start=2)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=None, progress_state="unavailable")]])
    code, stdout, _ = _run(client)
    assert code == 0 and stdout.splitlines()[-1] == "watch ended: finished start 1 exit 0"


def test_a_campaign_view_not_ready_is_said_once_and_the_status_decides(clock):
    missing = ApiError({"error": {"code": "not_found", "stage": "read",
                                  "message": "The requested owner resource was not found."}})
    one, two = _progress(closed=0), _progress(closed=1, running=0)
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", progress=two, failure=FAILURE)],
                                 CAMPAIGN: [missing]},
                         [[("tick", 11), _frame(progress=two), _frame("finished", progress=two, failure=FAILURE)]])
    code, stdout, _ = _run(client)
    lines = stdout.splitlines()
    assert code == 4
    assert lines[:3] == [HEADER, "09:00:00 campaign view unavailable: not_found", f"09:00:00 {_line(one)}"]
    assert lines.count("09:00:11 campaign view unavailable: not_found") == 1  # the summary; the t=11 retry is silent
    assert lines[-3:] == [
        "09:00:11 abnormal: control_state=finished target_cleanup=not_needed failure=spawn_failed persistence=saved",
        f"09:00:11 hint: the operation failed (spawn_failed); read lab status {OP} and lab report {OP}",
        "watch ended: abnormal start 1 exit 4"]
    assert client.calls.count(CAMPAIGN) == 3


def test_a_failed_scheduled_view_read_does_not_stop_the_watch(clock):
    view = _view14()
    steps = [_progress(closed=n, running=2 - n) for n in range(3)]
    client = WatchClient(clock, {STATUS: [_status(progress=steps[0]), _status("finished", progress=DONE)],
                                 CAMPAIGN: [view, ClientError("transport error"), view]},
                         [[("tick", 11), _frame(progress=steps[1]), ("tick", 11), _frame(progress=steps[2]),
                           _frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    assert code == 0
    assert stdout.splitlines().count("09:00:11 campaign view unavailable: client_error") == 1
    assert not any(line.startswith("09:00:22 ") and "campaign view" in line for line in stdout.splitlines())


def test_a_failed_last_read_keeps_the_class_of_the_frame(clock):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), ClientError("transport error")],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    assert code == 0 and clock.slept == [1, 2, 5]
    assert stdout.splitlines()[-2:] == ["09:00:08 final read unavailable: client_error",
                                        "watch ended: finished start 1 exit 0"]
    assert client.calls == [STATUS, CAMPAIGN, EVENTS, STATUS, STATUS, STATUS, STATUS]


def test_a_failed_last_read_without_a_frame_class_is_exit_1(clock):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), ClientError("transport error")],
                                 CAMPAIGN: [_view14()]},
                         [[_frame("finished", progress=None, progress_state="unavailable")]])
    code, stdout, _ = _run(client)
    assert (code, stdout.splitlines()[-1]) == (1, "watch ended: unknown start 1 exit 1")


def test_a_refused_status_ends_the_watch_with_exit_1(clock):
    gone = {"error": {"code": "not_found", "stage": "read", "message": "The requested owner resource was not found."}}
    client = WatchClient(clock, {STATUS: [ApiError(gone)]})
    code, stdout, stderr = _run(client)
    assert (code, stdout) == (1, "") and json.loads(stderr) == gone


@pytest.mark.parametrize(("error", "expected"), [(RequestTimeout(), 124), (KeyboardInterrupt(), 130)])
def test_a_timeout_or_an_interrupt_ends_the_watch(clock, error, expected):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress())], CAMPAIGN: [_view14()]},
                         [[_frame(progress=_progress()), error]])
    assert _run(client)[0] == expected


# -- Task 9: reconnecting


def test_the_end_of_the_stream_reconnects_and_prints_the_difference(clock):
    view = _view14()
    steps = [_progress(closed=n, running=2 - n) for n in range(3)]
    client = WatchClient(clock, {STATUS: [_status(progress=steps[0]), _status(progress=steps[1]),
                                          _status("finished", progress=DONE)], CAMPAIGN: [view]},
                         [[_frame(progress=steps[0])], [_frame(progress=steps[2]), _frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    lines = stdout.splitlines()
    at = lines.index("09:00:00 reconnecting: interrupted_observation")
    assert code == 0 and clock.slept == [1]
    assert lines[at + 1:at + 5] == [f"09:00:01 {_line(steps[1])}", "09:00:01 reconnected",
                                    f"09:00:01 {_line(steps[2])}", f"09:00:01 {_line(DONE)}"]
    assert client.calls == [STATUS, CAMPAIGN, EVENTS, STATUS, CAMPAIGN, EVENTS, STATUS, CAMPAIGN]


def test_an_error_frame_reconnects_and_a_terminal_status_ends_with_the_summary(clock):
    one = _progress()
    error = {"event": "error", "data": {"error": {"code": "history_unavailable", "stage": "transport", "message": "m"}}}
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame(progress=one), error]])
    code, stdout, _ = _run(client)
    lines = stdout.splitlines()
    assert code == 0 and "09:00:00 reconnecting: history_unavailable" in lines
    assert lines[lines.index("09:00:01 reconnected") - 1] == f"09:00:01 {_line(DONE)}"
    assert lines[-1] == "watch ended: finished start 1 exit 0"
    assert client.calls == [STATUS, CAMPAIGN, EVENTS, STATUS, CAMPAIGN]


def test_service_stopping_and_transport_failures_are_retried_with_backoff(clock):
    one = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=one), _refusal("service_stopping"),
                                          ClientError("transport error"), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_view14()]},
                         [[_frame(progress=one)]])
    code, stdout, _ = _run(client)
    assert code == 0 and clock.slept == [1, 2, 5]
    assert stdout.count("reconnecting:") == 1 and "09:00:08 reconnected" in stdout.splitlines()


def test_a_stream_that_fails_to_open_is_retried_before_the_snapshot(clock):
    one = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status(progress=one),
                                          _status("finished", progress=DONE)], CAMPAIGN: [_view14()]},
                         [[ClientError("transport error")], [_frame(progress=one), _frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    lines = stdout.splitlines()
    assert code == 0
    assert lines[0] == "09:00:00 reconnecting: client_error"
    assert lines[1] == f"watch {OP} campaign start 1  2026-10-14 09:00:01"
    assert lines[lines.index(f"09:00:01 {_line(one)}") + 1] == "09:00:01 reconnected"


def test_a_transient_failure_of_the_first_read_is_retried(clock):
    one = _progress()
    client = WatchClient(clock, {STATUS: [ClientError("transport error"), _status(progress=one),
                                          _status("finished", progress=DONE)], CAMPAIGN: [_view14()]},
                         [[_frame(progress=one), _frame("finished", progress=DONE)]])
    code, stdout, _ = _run(client)
    assert code == 0 and stdout.splitlines()[:2] == [
        "09:00:00 reconnecting: client_error", f"watch {OP} campaign start 1  2026-10-14 09:00:01"]


def test_a_permanent_refusal_while_reconnecting_is_exit_1(clock):
    gone = {"error": {"code": "not_found", "stage": "read", "message": "The requested owner resource was not found."}}
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), ApiError(gone)], CAMPAIGN: [_view14()]},
                         [[_frame(progress=_progress())]])
    code, _, stderr = _run(client)
    assert (code, json.loads(stderr), clock.slept) == (1, gone, [1])


def test_ten_minutes_without_a_successful_attempt_is_exit_1(clock):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), ClientError("transport error")],
                                 CAMPAIGN: [_view14()]},
                         [[_frame(progress=_progress())]])
    code, stdout, stderr = _run(client)
    assert code == 1 and json.loads(stderr)["error"]["code"] == "interrupted_observation"
    assert clock.slept[:4] == [1, 2, 5, 10] and set(clock.slept[3:]) == {10}
    assert sum(clock.slept[:-1]) < 600 <= sum(clock.slept)
    assert stdout.count("reconnecting:") == 1 and "reconnected" not in stdout


# -- current view /9 of both families, final status /6 of the observed start

def _view15():
    from pathlib import Path
    routes = json.loads(Path("tests/fixtures/panel/family15/routes.json").read_text())
    from panel_fixtures import current_form
    return current_form(copy.deepcopy(next(r["body"] for r in routes.values() if isinstance(r.get("body"), dict)
                              and r["body"].get("schema") == "lab-campaign-view/10")))


def _changed_family15(kind):
    from evolution_lab.owner.independent_view import independent_view
    from test_owner_independent_view import PREFLIGHT_BLOCK
    before = _view15()
    before["independent"]["attainability"]["preflight"] = independent_view(PREFLIGHT_BLOCK)["attainability"]["preflight"]
    after = copy.deepcopy(before)
    ind = after["independent"]
    attempt = ind["attempts"][0]
    if kind == "scope":
        attempt["global_comparison"]["scopes"][0].update(status="skipped", display="status")
    elif kind == "comparison":
        attempt["global_comparison"]["reason"] = "changed_reason"
    elif kind == "round-preflight":
        ind["attainability"]["preflight"][0]["explanation"]["status"] = "possible"
    elif kind == "attempt-preflight":
        ind["attempts"][1]["preflight"]["status"] = "possible"
    elif kind == "new-attempt":
        added = copy.deepcopy(attempt)
        added.update(attempt_id="r01-a99", comparison=None, gate_comparison=None, global_comparison=None,
                     preflight=None, eligible_final_score=None, gate_binding=None, global_binding=None)
        ind["attempts"].append(added)
    return before, after


@pytest.mark.parametrize("change", ["scope", "comparison", "round-preflight", "attempt-preflight", "new-attempt"])
def test_watch_family_15_prints_only_changed_keyed_lines(clock, change):
    from evolution_lab.cli.output import campaign_keyed_lines
    before, after = _changed_family15(change)
    previous = dict(campaign_keyed_lines(before))
    changed = [text for key, text in campaign_keyed_lines(after) if previous.get(key) != text]
    assert len(changed) == 1
    if change == "scope":
        assert [key for key, text in campaign_keyed_lines(after) if previous.get(key) != text] == [
            "ind:1:r01-a01:global:scope:global"]
    progress = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=progress), _status("finished", progress=DONE)],
                                 CAMPAIGN: [before, after]},
                         [[("tick", 11), _frame(progress=progress), ("tick", 1), _frame("finished", progress=DONE)]])
    code, out, err = _run(client)
    feed_lines = [line for line in out.splitlines() if line.startswith("09:00:11 ")]
    assert (code, err) == (0, "")
    assert feed_lines == [f"09:00:11 {changed[0]}"]
    assert {s for p, s in client.schemas if p == STATUS} == {"lab-operation-status/8"}
    assert {s for p, s in client.schemas if p == CAMPAIGN} == {"lab-campaign-view/15"}


CORE_REFUSED = {"code": "core_refused", "stage": "running", "message": "m"}
CORE_REFUSAL = {"code": "corpus_inside_role_workspace", "path": "lab/corpus", "class": "hard", "instance_digest": None}


@pytest.mark.parametrize("cleanup_required", [False, True])
def test_watch_core_refused_exits_4_with_refused_line(clock, cleanup_required):
    final = _status("cleanup_required" if cleanup_required else "finished", progress=DONE, failure=CORE_REFUSED,
                    core=PAUSED_CORE, refusal=CORE_REFUSAL,
                    cleanup={"state": "pending", "complete": None} if cleanup_required else None)
    client = WatchClient(clock, {STATUS: [_status(progress=_progress()), final], CAMPAIGN: [_view15()]},
                         [[_frame(final["control_state"], progress=DONE, failure=CORE_REFUSED,
                                  target_cleanup=final["target_cleanup"])]])
    code, out, err = _run(client)
    assert code == 4 and err == "" and "abnormal: control_state=" in out and "failure=core_refused" in out
    assert "refused: corpus_inside_role_workspace at lab/corpus (hard)" in out
    assert out.index("refused:") < out.index("suspended: editor_failing")
    assert out.splitlines()[-2] == (f"09:00:00 {CLEANUP_HINT}" if cleanup_required else
        f"09:00:00 hint: the operation failed (core_refused); read lab status {OP} and lab report {OP}")


@pytest.mark.parametrize("final_start", [1, 3])
def test_watch_refusal_is_not_taken_from_a_different_start(clock, final_start):
    client = WatchClient(clock, {STATUS: [_status(progress=_progress(), start=2),
                                          _status("running", start=final_start, refusal=CORE_REFUSAL)], CAMPAIGN: [_view15()]},
                         [[_frame("finished", start=2, progress=DONE, failure=CORE_REFUSED)]])
    code, out, _ = _run(client)
    assert code == 4 and "failure=core_refused" in out and "refused:" not in out
    assert client.calls.count(CAMPAIGN) == 1


@pytest.mark.parametrize("family", [14, 15])
def test_watch_view_not_ready_continues(clock, family):
    view = _view14() if family == 14 else _view15()
    progress = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=progress), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_refusal("not_found"), view]},
                         [[("tick", 11), _frame(progress=progress), _frame("finished", progress=DONE)]])
    code, out, err = _run(client)
    assert code == 0 and err == "" and "campaign view unavailable: not_found" in out
    assert "09:00:11 " + _texts(view)[0] in out


@pytest.mark.parametrize("family", [14, 15])
@pytest.mark.parametrize("command", ["campaign", "view"])
def test_watch_campaign_lines_match_lab_campaign_text(clock, family, command):
    from test_service_cli import FakeClient
    view = _view14() if family == 14 else _view15()
    campaign_code, campaign_out, campaign_err = invoke(["--socket", "/tmp/lab.sock", command, OP], FakeClient([view]))
    client = WatchClient(clock, {STATUS: [_status("finished", progress=DONE)], CAMPAIGN: [view]})
    code, out, err = _run(client)
    note = ("Old name: lab campaign OPERATION reads the campaign view; use lab view OPERATION.\n"
            if command == "campaign" else "")
    assert (campaign_code, campaign_err, code, err) == (0, note, 0, "")
    # The watch header replaces lab campaign's generic schema/id envelope.
    campaign_lines = campaign_out.splitlines()[1:]
    assert out.splitlines()[1:1 + len(campaign_lines)] == campaign_lines


def test_watch_suspension_keeps_view_and_final_status_surfaces(clock):
    core = _family14_connection_suspended()
    view = _view(core, {"phase": "suspended"})
    client = WatchClient(clock, {STATUS: [_status("finished", progress=_progress("suspended"), core=core)], CAMPAIGN: [view]})
    code, out, _ = _run(client)
    assert code == 3
    assert "suspended: connection_incompatible\n" in out
    assert "09:00:00 suspended: connection_incompatible (owner=connection, evidence=stage_record)\n" in out


# -- Task 3 review fix: deferred reads have their own deadline, independent of SSE frames.

@pytest.mark.parametrize("family", [14, 15])
def test_watch_deferred_view_read_runs_at_interval_without_a_later_status(clock, family):
    before = _view14() if family == 14 else _view15()
    after = copy.deepcopy(before)
    after["outcome"] = {"outcome": "completed", "reason": "changed_reason"}
    one, two, three = _progress(), _progress(closed=1), _progress(closed=2)
    client = WatchClient(clock, {STATUS: [_status(progress=one), _status("finished", progress=DONE)],
                                 CAMPAIGN: [before, after]},
                         [[_frame(progress=one), ("tick", 3), _frame(progress=two), ("tick", 2),
                           _frame(progress=three), ("tick", 595), _frame("finished", progress=DONE)]])
    code, out, err = _run(client)
    assert (code, err) == (0, "")
    assert f"09:00:03 {_line(two)}" in out and f"09:00:05 {_line(three)}" in out
    assert "09:00:10 outcome: completed/changed_reason" in out
    assert [at for path, at in client.read_times if path == CAMPAIGN] == [0, 10, 600]
    assert all(reader.closed for reader in clock.readers)


def test_watch_optional_view_retries_on_its_interval_while_the_stream_is_silent(clock):
    view = _view15()
    progress = _progress()
    client = WatchClient(clock, {STATUS: [_status(progress=progress), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_refusal("not_found"), _refusal("not_found"), view]},
                         [[_frame(progress=progress), ("tick", 600), _frame("finished", progress=DONE)]])
    code, out, err = _run(client)
    assert (code, err) == (0, "")
    assert [at for path, at in client.read_times if path == CAMPAIGN] == [0, 10, 20, 600]
    assert out.count("campaign view unavailable: not_found") == 1
    assert "09:00:20 " + _texts(view)[0] in out
    assert all(reader.closed for reader in clock.readers)


@pytest.mark.parametrize("ending", ["terminal", "stream-end", "timeout", "interrupt"])
def test_watch_deferred_read_is_stopped_before_summary_reconnect_or_exit(clock, ending):
    progress = _progress()
    stream = [_frame(progress=progress), ("tick", 3), _frame(progress=progress), ("tick", 1)]
    if ending == "terminal":
        stream.append(_frame("finished", progress=DONE))
    elif ending == "timeout":
        stream.append(RequestTimeout())
    elif ending == "interrupt":
        stream.append(KeyboardInterrupt())
    client = WatchClient(clock, {STATUS: [_status(progress=progress), _status("finished", progress=DONE)],
                                 CAMPAIGN: [_view14()]}, [stream])
    code, out, _ = _run(client)
    expected = 124 if ending == "timeout" else 130 if ending == "interrupt" else 0
    assert code == expected
    assert [at for path, at in client.read_times if path == CAMPAIGN] == ([0] if expected else [0, 4 if ending == "terminal" else 5])
    assert clock.readers and all(reader.closed for reader in clock.readers)
    assert expected or out.splitlines()[-1] == "watch ended: finished start 1 exit 0"


def test_watch_event_reader_waits_on_demand_and_closes_its_stream():
    from threading import Event
    from evolution_lab.cli.watch import EventReader
    second_pull, closed = Event(), Event()

    def stream():
        try:
            yield {"event": "first"}
            second_pull.set()
            yield {"event": "second"}
        finally:
            closed.set()

    reader = EventReader(stream())
    try:
        assert reader.next(timeout=1) == {"event": "first"}
        assert not second_pull.is_set()  # no prefetch while the main thread handles its event
        assert reader.next(timeout=1) == {"event": "second"}
    finally:
        reader.close()
    assert closed.wait(1)


def test_watch_event_reader_coalesces_waits_and_stops_an_outstanding_pull():
    from threading import Event
    from evolution_lab.cli.watch import EventReader
    entered, release, closed, second_pull = Event(), Event(), Event(), Event()

    def stream():
        try:
            entered.set()
            release.wait()
            yield {"event": "late"}
            second_pull.set()
            yield {"event": "later"}
        finally:
            closed.set()

    reader = EventReader(stream())
    try:
        assert reader.next(timeout=0) is None
        assert entered.wait(1)
        assert reader.next(timeout=0) is None
        reader.close()
        release.set()
        assert closed.wait(1)
        assert not second_pull.is_set()
    finally:
        release.set()
        reader.close()


def test_watch_prints_owner_reflector_probe_status(clock):
    view = _view14()
    view.update(reflector_probe={'launched_by': 'operator', 'state': 'stopped', 'restarts': 0, 'last_exit': None},
                reflector_probe_state='present', reflector_probe_reason=None)
    client = WatchClient(clock, {STATUS: [_status('finished', progress=DONE, core={'suspended': False})], CAMPAIGN: [view]})
    code, stdout, stderr = _run(client)
    assert (code, stderr) == (0, '')
    assert 'reflector probe: launched_by=operator state=stopped restarts=0' in stdout
    assert (CAMPAIGN, 'lab-campaign-view/15') in client.schemas
