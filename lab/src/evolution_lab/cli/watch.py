"""``lab watch OPERATION``: a text feed of one Owner operation until it ends or pauses (spec
docs/codex/specs/2026-10-14-lab-watch-design.md). A client of existing reads only — the status, the campaign view and
the event stream; every line is a ``lab status`` / ``lab campaign`` render, and only changed lines are printed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import itertools
from queue import Empty, Full, Queue
import threading
import time
from typing import Any, Callable, Iterator, TextIO
from urllib.parse import quote

from evolution_lab._suspension import last_suspension

from .client import AcceptanceUnknown, ApiError, ClientError, RequestInterrupted, RequestTimeout
from .output import (_progress_line, _token, campaign_keyed_lines, local_error, outcome_line, refusal_lines, core_terminal_lines, suspension_lines,
                     warning_lines, write_json)

TERMINAL = frozenset({"finished", "orphaned", "cleanup_required", "released"})  # as lab events
EXIT = {"finished": 0, "suspended": 3, "abnormal": 4}

FINAL_READ_DELAYS = (1, 2, 5)  # seconds before each retry of the last status read (§4.2)
RECONNECT_DELAYS = (1, 2, 5, 10)  # seconds before each attempt; the last one repeats (§4.3)
GIVE_UP_SECONDS = 600  # ten minutes without a successful attempt (§4.3)


@dataclass
class Clock:
    """Time sources of ``lab watch``; tests replace the module's ``CLOCK``."""

    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = time.sleep


CLOCK = Clock()


class Broken(Exception):
    """A transient failure of a status or stream read (§4.2); ``code`` goes on the ``reconnecting:`` line."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _api_code(error: ApiError) -> str | None:
    failure = error.payload.get("error") if isinstance(error.payload, dict) else None
    code = failure.get("code") if isinstance(failure, dict) else None
    return code if isinstance(code, str) else None


def transient_code(error: BaseException) -> str | None:
    """§4.2: the code of a transient failure — a transport failure (``client_error``) or ``service_stopping``; None
    for a permanent refusal, a timeout or an interrupt."""
    if isinstance(error, ApiError):
        return "service_stopping" if _api_code(error) == "service_stopping" else None
    if isinstance(error, (RequestTimeout, RequestInterrupted, AcceptanceUnknown)):
        return None
    return "client_error" if isinstance(error, ClientError) else None


def facts_of_status(status: dict[str, Any]) -> dict[str, Any]:
    """An operation status in the shape of an event payload: a terminal status read after a reconnect plays the
    terminal frame (§4.3); ``start`` is the latest start, counted as the event stream counts it."""
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    starts = status.get("starts") if isinstance(status.get("starts"), list) else []
    last = starts[-1] if starts and isinstance(starts[-1], dict) else {}
    return {"kind": status.get("kind"), "control_state": status.get("control_state"),
            "target_cleanup": status.get("target_cleanup"), "failure": status.get("failure"),
            "persistence": result.get("persistence"), "progress": status.get("progress"),
            "progress_state": status.get("progress_state"), "start": last.get("start", 1)}


def _cleanup_open(facts: dict[str, Any]) -> bool:
    cleanup = facts.get("target_cleanup") if isinstance(facts.get("target_cleanup"), dict) else {}
    state = facts.get("control_state")
    return state in {"orphaned", "cleanup_required"} or (state == "released" and cleanup.get("complete") is not True)


def outcome_class(facts: dict[str, Any]) -> str | None:
    """§5 by one terminal frame, in the order abnormal → suspended → finished; None when the frame has no
    ``progress`` (the last read decides, §3.3)."""
    persistence = facts.get("persistence") if isinstance(facts.get("persistence"), dict) else {}
    if _cleanup_open(facts) or isinstance(facts.get("failure"), dict) or persistence.get("state") == "failed":
        return "abnormal"
    progress = facts.get("progress")
    if not isinstance(progress, dict):
        return None
    return "suspended" if progress.get("phase") == "suspended" else "finished"


def abnormal_lines(facts: dict[str, Any], operation_id: str) -> list[str]:
    """§3.4: every cause as codes and states, then the hint of the first one (target cleanup → failure →
    persistence)."""
    cleanup = facts.get("target_cleanup") if isinstance(facts.get("target_cleanup"), dict) else {}
    failure = facts.get("failure") if isinstance(facts.get("failure"), dict) else None
    persistence = facts.get("persistence") if isinstance(facts.get("persistence"), dict) else {}
    lines = [f"abnormal: control_state={_token(facts.get('control_state'))} "
             f"target_cleanup={_token(cleanup.get('state') or '-')} "
             f"failure={_token(failure.get('code')) if failure else '-'} "
             f"persistence={_token(persistence.get('state') or '-')}"]
    if _cleanup_open(facts):
        lines.append(f"hint: run lab cleanup operation {operation_id}, then read lab status {operation_id}")
    elif failure is not None:
        lines.append(f"hint: the operation failed ({_token(failure.get('code'))}); "
                     f"read lab status {operation_id} and lab report {operation_id}")
    else:
        lines.append(f"hint: the result was not saved (persistence failed); read lab status {operation_id}")
    return lines


class EventReader:
    """Wait for one event or a watch deadline without changing ServiceClient's iterator.

    The daemon reads only on demand: at most one ``next`` is outstanding, including
    across deadline wakeups. Only the watch's main thread reads views and writes output.
    ``close`` stops further pulls. An already blocked stream read unwinds when it
    returns or the owning ServiceClient closes; its result is discarded after stop.
    The worker closes the generator itself, never concurrently with its ``next``.
    """

    def __init__(self, events: Iterator[dict[str, Any]]):
        self.events = iter(events)
        self.requests: Queue[bool] = Queue(maxsize=1)
        self.results: Queue[tuple[bool, Any]] = Queue(maxsize=1)
        self.stopped = threading.Event()
        self.pending = False
        threading.Thread(target=self._read, daemon=True, name="lab-watch-events").start()

    def _read(self) -> None:
        try:
            while self.requests.get():
                if self.stopped.is_set():
                    return
                try:
                    result = (True, next(self.events))
                except BaseException as error:
                    result = (False, error)
                if self.stopped.is_set():
                    return
                self.results.put(result)
                if not result[0]:
                    return
        finally:
            close = getattr(self.events, "close", None)
            if close is not None:
                close()

    def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        if not self.pending:
            self.requests.put(True)
            self.pending = True
        try:
            ok, value = self.results.get(timeout=timeout)
        except Empty:
            return None
        self.pending = False
        if not ok:
            raise value
        return value

    def close(self) -> None:
        if not self.stopped.is_set():
            self.stopped.set()
            # There is at most one request; the worker checks stopped before pulling it.
            try:
                self.requests.put_nowait(False)
            except Full:
                pass


class Watch:
    """One ``lab watch`` run (spec §2–§5). ``ready_target(status)`` gives the ready target of the continuation line,
    as for ``lab status`` (``parser._ready_target``)."""

    def __init__(self, client, operation_id: str, *, interval: int, stdout: TextIO, stderr: TextIO,
                 ready_target: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                 clock: Clock | None = None):
        self.client, self.id, self.interval = client, operation_id, interval
        self.out, self.err = stdout, stderr
        self.ready_target = ready_target or (lambda status: None)
        self.clock = CLOCK if clock is None else clock
        self.kind: str | None = None
        self.start: int | None = None  # the start of the last printed status
        self.progress: str | None = None  # the last printed progress: line
        self.view: dict[str, str] = {}  # campaign line key -> the last printed text
        self.view_down: str | None = None  # the code of the announced view outage, None while the view reads
        self.view_read_at: float | None = None
        self.day = None  # the local date of the last printed line

    # -- output

    def _line(self, text: str, *, stamp: bool = True, observed_at: datetime | None = None) -> None:
        now = self.clock.now() if observed_at is None else observed_at
        if stamp:
            if self.day is not None and now.date() != self.day:
                self.out.write(f"--- {now:%Y-%m-%d}\n")
            text = f"{now:%H:%M:%S} {text}"
        self.day = now.date()
        self.out.write(text + "\n")
        self.out.flush()

    def _path(self, *tail: str) -> str:
        return "/" + "/".join(quote(part, safe="") for part in ("v1", "operations", self.id, *tail))

    # -- reads

    def _status(self) -> dict[str, Any]:
        try:
            return self.client.request_json("GET", self._path(), expected_schema="lab-operation-status/8")
        except ClientError as error:
            code = transient_code(error)
            if code is None:
                raise
            raise Broken(code) from None

    def _view(self) -> tuple[dict[str, Any] | None, str | None]:
        """The campaign view, or ``(None, code)``: an optional detail whose failure never ends the watch (§4.2)."""
        self.view_read_at = self.clock.monotonic()
        try:
            return self.client.request_json("GET", self._path("campaign"), expected_schema="lab-campaign-view/15"), None
        except (RequestTimeout, RequestInterrupted):
            raise
        except ApiError as error:
            return None, _api_code(error) or "client_error"
        except ClientError:
            return None, "client_error"

    def _due(self) -> bool:
        return self.view_read_at is None or self.clock.monotonic() - self.view_read_at >= self.interval

    def _final_status(self) -> tuple[dict[str, Any] | None, str | None]:
        """The last status read (§4.2): a transient failure is retried after 1, 2 and 5 s."""
        delays = iter(FINAL_READ_DELAYS)
        while True:
            try:
                return self._status(), None
            except Broken as broken:
                delay = next(delays, None)
                if delay is None:
                    return None, broken.code
                self.clock.sleep(delay)

    # -- changes

    def _show_start(self, facts: dict[str, Any]) -> None:
        if self.start is not None and facts["start"] != self.start:
            self._line(f"start {facts['start']}")
        self.start = facts["start"]

    def _show_progress(self, facts: dict[str, Any], *, observed_at: datetime | None = None) -> None:
        line = _progress_line(facts)
        if line is not None and line != self.progress:
            self._line(line, observed_at=observed_at)
            self.progress = line

    def _show_view(self, view, code, *, stamp: bool = True, announce: bool = False,
                   observed_at: datetime | None = None) -> None:
        """Changed campaign lines (§3.2); an unavailable view is said once until the next good read, and always when
        ``announce`` (the snapshot, a fresh last read)."""
        if view is None:
            if announce or self.view_down is None:
                self._line(f"campaign view unavailable: {code}", observed_at=observed_at)
            self.view_down = code
            return
        self.view_down = None
        for key, text in campaign_keyed_lines(view):
            if self.view.get(key) != text:
                self._line(text, stamp=stamp, observed_at=observed_at)
                self.view[key] = text

    def _snapshot(self, status, view, code, *, observed_at: datetime) -> None:
        """§3.1: the first line, then lab campaign's lines as they are, then the stamped progress line."""
        facts = facts_of_status(status)
        self.kind, self.start = status.get("kind"), facts["start"]
        self._line(f"watch {self.id} {self.kind} start {self.start}  {observed_at:%Y-%m-%d %H:%M:%S}",
                   stamp=False, observed_at=observed_at)
        if self.kind == "campaign":
            self._show_view(view, code, stamp=False, announce=True, observed_at=observed_at)
        self._show_progress(facts, observed_at=observed_at)

    def _changes(self, status, view, code) -> None:
        facts = facts_of_status(status)
        self._show_start(facts)
        if self.kind == "campaign":
            self._show_view(view, code)
        self._show_progress(facts)

    # -- the run

    def run(self) -> int:
        try:
            outcome = self._connect(first=True, recovering=False)
        except Broken as broken:
            outcome = self._reconnect(broken.code, first=True)
        while not isinstance(outcome, int):
            try:
                return self._follow(outcome)
            except Broken as broken:
                outcome = self._reconnect(broken.code, first=False)
        return outcome

    def _connect(self, *, first: bool, recovering: bool) -> int | Iterator[dict[str, Any]]:
        """One connection (§3.1, §4.3): the status, the campaign view, then the stream up to its first event. Prints
        the snapshot (``first``) or the changes and, after a failure, ``reconnected``; returns the exit code when the
        status is already terminal, else the stream with its first event put back. ``Broken`` on a transient failure
        of the status or the stream; a permanent refusal propagates (exit 1)."""
        status = self._status()
        view, code = self._view() if status.get("kind") == "campaign" else (None, None)
        # The first event can arrive later; the snapshot records when its reads completed.
        observed_at = self.clock.now()
        facts = facts_of_status(status)
        if facts["control_state"] not in TERMINAL:
            try:
                events = iter(self.client.stream_events(self._path("events")))
                head = next(events)
            except StopIteration:
                raise Broken("interrupted_observation") from None
            except ClientError as error:
                transient = transient_code(error)
                if transient is None:
                    raise
                raise Broken(transient) from None
        if first:
            self._snapshot(status, view, code, observed_at=observed_at)
        else:
            self._changes(status, view, code)
        if recovering:
            self._line("reconnected")
        if facts["control_state"] in TERMINAL:
            return self._finish(facts, status=status, view=view, view_code=code)
        def connected_events():
            try:
                yield head
                yield from events
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    close()

        return connected_events()

    def _follow(self, events: Iterator[dict[str, Any]]) -> int:
        """§3.2: print status immediately; coalesce campaign changes into one interval
        deadline. Optional view failures retry on that deadline even on a silent stream.
        """
        reader = EventReader(events)
        deadline = self.view_read_at + self.interval if self.view_down is not None else None
        try:
            while True:
                wait = None if deadline is None else max(0, deadline - self.clock.monotonic())
                event = reader.next(timeout=wait)
                if event is None:
                    self._show_view(*self._view())
                    deadline = self.view_read_at + self.interval if self.view_down is not None else None
                    continue
                if event.get("event") == "error":
                    data = event.get("data")
                    failure = data.get("error") if isinstance(data, dict) else None
                    code = failure.get("code") if isinstance(failure, dict) else None
                    raise Broken(code if isinstance(code, str) else "client_error")
                facts = event["data"]["payload"]
                self._show_start(facts)
                self._show_progress(facts)
                if facts["control_state"] in TERMINAL:
                    reader.close()
                    return self._finish(facts)
                if self.kind == "campaign":
                    if self._due():
                        self._show_view(*self._view())
                        deadline = self.view_read_at + self.interval if self.view_down is not None else None
                    elif deadline is None:
                        deadline = self.view_read_at + self.interval
        except StopIteration:
            raise Broken("interrupted_observation") from None
        except ClientError as error:
            code = transient_code(error)
            if code is None:
                raise
            raise Broken(code) from None
        finally:
            reader.close()

    def _reconnect(self, code: str, *, first: bool) -> int | Iterator[dict[str, Any]]:
        """§4.3: one ``reconnecting:`` line, then attempts after 1, 2, 5, 10, 10… s; a successful attempt prints the
        changes and ``reconnected``. Ten minutes without one end the watch: exit 1 and ``interrupted_observation``.
        A permanent refusal propagates (exit 1)."""
        self._line(f"reconnecting: {code}")
        began = self.clock.monotonic()
        for attempt in itertools.count():
            if self.clock.monotonic() - began >= GIVE_UP_SECONDS:
                write_json(self.err, local_error("interrupted_observation",
                                                 "The service event observation could not be resumed for ten minutes."))
                return 1
            self.clock.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
            try:
                return self._connect(first=first, recovering=True)
            except Broken:
                continue
        raise AssertionError("unreachable")

    def _finish(self, facts: dict[str, Any], *, status=None, view=None, view_code=None) -> int:
        """§3.3: the class and the exit code come from the terminal frame of start N; the last read (the status, then
        — for the same start — the campaign view) only adds the summary lines."""
        n = facts["start"]
        klass = outcome_class(facts)
        fresh, failed = status is None, None
        if fresh:
            status, failed = self._final_status()
            if status is not None and self.kind == "campaign" and facts_of_status(status)["start"] == n:
                view, view_code = self._view()
        if status is None:
            self._line(f"final read unavailable: {failed}")
        elif facts_of_status(status)["start"] > n:
            klass = klass or "finished"  # already continued by another client: nothing for a person to do
            self._line(f"start {facts_of_status(status)['start']} began after start {n} ended — run lab watch again")
        elif facts_of_status(status)["start"] == n:
            if klass is None:
                result = status.get("result") if isinstance(status.get("result"), dict) else {}
                klass = "suspended" if last_suspension(result.get("core")) is not None else "finished"
            if fresh:
                self._show_progress(facts_of_status(status))
                if self.kind == "campaign":
                    self._show_view(view, view_code, announce=True)
            for line in refusal_lines(status) + core_terminal_lines(status):
                self._line(line)
            for line in suspension_lines(status, self.ready_target(status)):
                self._line(line)
            if view is not None:
                self._line(outcome_line(view.get("outcome")))
            for line in warning_lines(status):
                self._line(line)
        if klass == "abnormal":
            for line in abnormal_lines(facts, self.id):
                self._line(line)
        code = 1 if klass is None else EXIT[klass]
        self._line(f"watch ended: {klass or 'unknown'} start {n} exit {code}", stamp=False)
        return code
