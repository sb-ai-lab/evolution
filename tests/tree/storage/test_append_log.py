"""Generic append-log contracts, independent of the S1 journal lifecycle."""

import os
from datetime import UTC, datetime

import pytest

from evolution.tree.storage.append_log import AppendLog, inspect_log
from evolution.tree.storage.envelope import Envelope, EnvelopeFamily, LifecycleTransition
from evolution.tree.storage.journal_errors import JournalAdmissionRefused, JournalFault


class CounterState:
    """Minimal state: sequence + one open op reserve; first event must be 'opened'."""

    def __init__(self, is_start=None):
        self.sequence = 0
        self.pending = set()
        # Reserve accounting must agree with the family that admits the records.
        self._is_start = is_start or FAMILY.is_start

    @property
    def reserved_records(self):
        return len(self.pending)

    def prepare(self, record):
        v = record.to_payload()
        if v["sequence"] != self.sequence + 1:
            raise ValueError("sequence")
        if self.sequence == 0 and v["event"] != "opened":
            raise ValueError("first")
        started = self._is_start(v["event"])
        op = v["operation_id"]

        def apply():
            if started:
                self.pending.add(op)
            elif op in self.pending:
                self.pending.remove(op)
            self.sequence += 1

        return LifecycleTransition(
            self.reserved_records + (1 if started else -1 if op in self.pending else 0), apply
        )

    def accept(self, record):
        self.prepare(record).commit()


def validate(event, payload):
    if event not in {"opened", "x_started", "x_finished"}:
        raise ValueError(event)


FAMILY = EnvelopeFamily(
    validate_payload=validate,
    no_operation_events=frozenset({"opened"}),
    is_start=lambda event: event.endswith("_started"),
)


def validate_inverted(event, payload):
    if event not in {"opened", "workspace_opened_x", "x_started"}:
        raise ValueError(event)


# The start predicate belongs to the family, not to a suffix convention: this one
# starts on a suffixless event and treats the `_started` spelling as a terminal.
INVERTED_FAMILY = EnvelopeFamily(
    validate_payload=validate_inverted,
    no_operation_events=frozenset({"opened"}),
    is_start=lambda event: event.startswith("workspace_opened"),
)


def open_log(path, create, **kw):
    family = kw.pop("family", FAMILY)
    return AppendLog.open(
        path,
        file_name="log.jsonl",
        lock_name=".lock",
        create=create,
        state_factory=lambda: CounterState(family.is_start),
        max_record_bytes=4096,
        max_log_bytes=kw.pop("max", 1 << 20),
        secrets=(),
        envelope_family=family,
    )


def test_create_append_resume_and_inspect(tmp_path):
    path = tmp_path / "run"
    log = open_log(path, True)
    assert log.append("r", "opened", {}, None) == 1
    assert log.append("r", "x_started", {}, "op1") == 2
    log.close()
    with pytest.raises(JournalFault):
        open_log(path, True)  # nonempty directory
    log = open_log(path, False)
    snapshot, state = log.scan()
    assert snapshot.sequence == 2 and state.pending == {"op1"}
    assert log.append("r", "x_finished", {}, "op1") == 3
    assert (
        inspect_log(
            path,
            file_name="log.jsonl",
            max_record_bytes=4096,
            max_log_bytes=1 << 20,
            state_factory=CounterState,
            envelope_family=FAMILY,
        ).sequence
        == 3
    )
    log.close()


def test_second_owner_is_busy_and_tail_is_repaired(tmp_path):
    path = tmp_path / "run"
    log = open_log(path, True)
    log.append("r", "opened", {}, None)
    with pytest.raises(JournalFault) as info:
        open_log(path, False)
    assert str(info.value) == "journal_busy"
    log.close()
    with open(path / "log.jsonl", "ab") as fh:
        fh.write(b'{"torn')
    log = open_log(path, False)
    snapshot, _ = log.scan()
    assert snapshot.sequence == 1 and (path / "log.jsonl").stat().st_size == snapshot.valid_bytes
    log.close()


def test_limit_calls_on_limit_then_refuses(tmp_path):
    log = open_log(tmp_path / "run", True, max=700)
    log.append("r", "opened", {}, None)
    calls = []
    with pytest.raises(JournalAdmissionRefused):
        for i in range(50):
            log.append("r", "x_started", {}, f"op{i}", on_limit=lambda: calls.append(1))
    assert calls == [1]
    log.close()


def test_limit_branch_follows_the_family_start_predicate(tmp_path):
    """A family whose start event has no `_started` suffix still routes both ways."""
    log = open_log(tmp_path / "run", True, max=700, family=INVERTED_FAMILY)
    log.append("r", "opened", {}, None)
    calls = []
    with pytest.raises(JournalAdmissionRefused):
        for i in range(50):
            log.append("r", "workspace_opened_x", {}, f"op{i}", on_limit=lambda: calls.append(1))
    assert calls == [1]
    # The `_started` spelling this family declares terminal is faulted, never refused.
    with pytest.raises(JournalFault) as info:
        log.append("r", "x_started", dict(pad="p" * 700), "opN", on_limit=lambda: calls.append(2))
    assert str(info.value) == "journal_limit_exceeded" and calls == [1]
    log.close()


def _envelope(sequence: int, event: str, payload: dict | None = None) -> Envelope:
    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=sequence,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            event=event,
            operation_id=None,
            payload=payload or {},
        ),
        family=FAMILY,
    )


def test_append_envelope_writes_the_callers_bytes(tmp_path):
    log = open_log(tmp_path / "run", True)
    record = _envelope(1, "opened")
    assert log.append_envelope(record) == 1
    assert (tmp_path / "run" / "log.jsonl").read_bytes() == record.encode()
    with pytest.raises(JournalFault):
        log.append_envelope(_envelope(1, "opened"))  # not the next sequence
    log.close()


def test_append_envelope_keeps_the_record_bound(tmp_path):
    log = open_log(tmp_path / "run", True)
    with pytest.raises(JournalFault, match="journal_limit_exceeded"):
        log.append_envelope(_envelope(1, "opened", {"pad": "x" * 5000}))
    log.close()


def test_fork_bound_owner(tmp_path):
    log = open_log(tmp_path / "run", True)
    log.append("r", "opened", {}, None)
    pid = os.fork()
    if pid == 0:
        try:
            log.append("r", "x_started", {}, "child")
        except JournalFault:
            os._exit(0)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    log.close()
