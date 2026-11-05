"""Read-only run-directory access for the observability commands (PDR-0026 as
amended 2026-10-12, ADR-0035): run kind and family detection, incremental
journal tails with half-line handling, the measurement registry projected
from the campaign or evaluation journal, and per-measurement try state.

Every reader here is a pure reader: opens are `O_RDONLY` through the journal
package's own safe-entry checks, no lease is taken or tested, nothing is
created under a run directory, and unknown entries (`connection/`,
`connection-logs/`, stray files, future roots) are neither read nor refused.
`trace`, `watch` and `evolve --progress` build on this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from evolution.tree.campaign_projections import (
    CAMPAIGN_FILE,
    _reader_family_version,
)
from evolution.tree.campaign_schema import (
    CampaignLifecycle,
    CampaignSnapshot,
    campaign_family_for,
    reduce,
)
from evolution.tree.evaluation_journal import (
    EVALUATION_FILE,
    inspect_evaluation_journal,
)
from evolution.tree.execution.journal import (
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
)
from evolution.tree.execution.sample_outcomes import SampleOutcome, disposition_for
from evolution.tree.storage.append_log import (
    LogState,
    _directory,
    _fault,
    _file,
    _scan,
    _validate_file,
    inspect_log,
)
from evolution.tree.storage.append_log import (
    Snapshot as LogSnapshot,
)
from evolution.tree.storage.envelope import Envelope, EnvelopeFamily
from evolution.tree.storage.journal_errors import JournalFault

__all__ = [
    "JournalRewritten",
    "JournalTail",
    "JournalUnreadable",
    "MeasurementInfo",
    "RunHandle",
    "RunKind",
    "RunRefused",
    "TryRollout",
    "TryScoring",
    "TryState",
    "ImportedProvenance",
    "measurement_infos",
    "open_run",
    "tries",
]


class RunKind(StrEnum):
    """What kind of run a run directory holds (decision 1).

    The two journals are the only kind markers: `campaign.jsonl` present
    means campaign, `evaluation.jsonl` present means evaluation, both is a
    refusal, neither is "not a run directory".
    """

    CAMPAIGN = "campaign"
    EVALUATION = "evaluation"


class RunRefused(Exception):
    """A run directory is not openable as one run.

    `code` is `not_a_run_directory` (absent path, not a directory, or no
    journal at all) or `run_dir_ambiguous` (both journals present). The
    observability commands render these as exit-2 refusals.
    """

    def __init__(self, code: Literal["not_a_run_directory", "run_dir_ambiguous"]) -> None:
        self.code = code
        super().__init__(code)


class JournalRewritten(Exception):
    """The journal shrank below the consumed offset or was replaced.

    A follower cannot trust a byte offset into a different file, so this is
    never retried within a follow session.
    """

    code = "journal_rewritten"

    def __init__(self, detail: str = "the journal was rewritten") -> None:
        super().__init__(f"{self.code}: {detail}")


class JournalUnreadable(Exception):
    """A complete journal line failed admission (`corrupt_suffix`)."""

    code = "corrupt_suffix"

    def __init__(self, detail: str = "a complete journal line failed admission") -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class RunHandle:
    """One run directory, opened once, read-only.

    `family` is the campaign record family (`_reader_family_version`), or
    `None` for an evaluation run, which carries no family. `snapshot` is the
    campaign reducer's frozen `CampaignSnapshot`, or the evaluation journal's
    replay snapshot; `measurement_infos` projects either. `records` is the
    admitted prefix used to derive that snapshot, so consumers never need a
    second journal read. `valid_bytes` retains that prefix's raw byte length,
    including admitted JSON whitespace. Defaults preserve constructor calls.
    """

    kind: RunKind
    family: int | None
    run_dir: Path
    snapshot: CampaignSnapshot | LogSnapshot
    records: tuple[Envelope, ...] = ()
    valid_bytes: int = 0


def open_run(run_dir: str | Path) -> RunHandle:
    """Open a run directory once, read-only, and reduce its own journal.

    Kind comes from which journal exists (decision 1); family selection is
    the one `inspect_view` uses (decision 2), so an unknown family integer
    refuses with `CampaignFamilyUnsupported` exactly as a resume would.
    """

    path = Path(run_dir)
    if not path.is_dir():
        raise RunRefused("not_a_run_directory")
    has_campaign = (path / CAMPAIGN_FILE).exists()
    has_evaluation = (path / EVALUATION_FILE).exists()
    if has_campaign and has_evaluation:
        raise RunRefused("run_dir_ambiguous")
    if has_campaign:
        family_version = _reader_family_version(path)
        log = inspect_log(
            path,
            file_name=CAMPAIGN_FILE,
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
            envelope_family=campaign_family_for(family_version),
        )
        snapshot = reduce(log.records, selector=None, family_version=family_version)
        return RunHandle(
            RunKind.CAMPAIGN, family_version, path, snapshot, log.records, log.valid_bytes
        )
    if has_evaluation:
        evaluation_snapshot = inspect_evaluation_journal(path)
        return RunHandle(
            RunKind.EVALUATION,
            None,
            path,
            evaluation_snapshot,
            evaluation_snapshot.records,
            evaluation_snapshot.valid_bytes,
        )
    raise RunRefused("not_a_run_directory")


@dataclass(frozen=True)
class MeasurementInfo:
    """One bound measurement, projected from the run's own journal (decision 5).

    `role` is `refresh` when the id or its re-bound origin chain is listed in
    the campaign's refreshes (the registry stores the binding as `baseline`;
    the observability vocabulary names it `refresh`), else the registry role,
    else `None` (families 5-11). `epoch` and `rebound_from` are the binding
    history's facts, never parsed from an `@e<k>` suffix. An evaluation run
    reports one child with role `evaluation`.
    """

    id: str
    role: str | None
    round: int | None
    attempt_id: str | None
    candidate_digest: str
    epoch: int
    rebound_from: str | None
    cohort_role: str | None
    complete: bool


def measurement_infos(snapshot: CampaignSnapshot | LogSnapshot) -> list[MeasurementInfo]:
    """Project the measurement registry of either journal kind (decision 5)."""

    if isinstance(snapshot, CampaignSnapshot):
        return _campaign_measurement_infos(snapshot)
    return _evaluation_measurement_infos(snapshot)


def _campaign_measurement_infos(snapshot: CampaignSnapshot) -> list[MeasurementInfo]:
    refresh_ids = {str(entry["measurement_id"]) for entry in snapshot.refreshes}

    def is_refresh(measurement_id: str) -> bool:
        # The refresh listing names the bound id; a `continue` re-bind moves
        # the measurement to an `@e<k>` id, so follow the rebound_from chain.
        current: str | None = measurement_id
        for _ in range(len(snapshot.measurements) + 1):
            if current is None:
                return False
            if current in refresh_ids:
                return True
            entry = snapshot.measurements.get(current)
            current = None if entry is None else entry.get("rebound_from")
        return False

    infos = []
    for measurement_id, entry in snapshot.measurements.items():
        infos.append(
            MeasurementInfo(
                id=measurement_id,
                role="refresh" if is_refresh(measurement_id) else entry.get("role"),
                round=int(entry["round"]),
                attempt_id=str(entry["attempt_id"]),
                candidate_digest=str(entry["candidate_digest"]),
                epoch=int(entry.get("epoch", 0)),
                rebound_from=(
                    None if entry.get("rebound_from") is None else str(entry["rebound_from"])
                ),
                cohort_role=entry.get("cohort_role"),
                complete=bool(entry["complete"]),
            )
        )
    return infos


def _evaluation_measurement_infos(snapshot: LogSnapshot) -> list[MeasurementInfo]:
    from evolution.tree.evaluation import MEASUREMENT_ID

    bound = next(
        (record for record in snapshot.records if record.event == "measurement_bound"),
        None,
    )
    if bound is None:
        return []
    identity = bound.payload["identity"]
    return [
        MeasurementInfo(
            id=MEASUREMENT_ID,
            role="evaluation",
            round=None,
            attempt_id=None,
            candidate_digest=str(identity["candidate_digest"]),
            epoch=0,
            rebound_from=None,
            cohort_role=None,
            complete=any(record.event == "evaluation_completed" for record in snapshot.records),
        )
    ]


@dataclass(frozen=True)
class ImportedProvenance:
    """Where a carried (`sample_imported`) row was measured, and its result.

    `metrics` and `critical_pass` are the row's own journaled scoring result:
    the `sample_imported` payload requires both, so a consumer that cannot
    reach the source try's trace (a cross-run carry, or an in-run carry whose
    source offers nothing) still reports the metrics the journal binds.
    """

    run_id: str
    measurement_id: str
    retry_ordinal: int
    candidate_digest: str
    metrics: Mapping[str, Any]
    critical_pass: bool | None


@dataclass(frozen=True)
class TryRollout:
    """The rollout part of one try (the trace record's `rollout` object).

    `kind` is the disposition registry's class (`score`/`indeterminate`/
    `exclude`); `owner` is the disposition owner. `target_calls` counts
    `target_send_started` records, call attempts included; `error_code` is the
    last journaled `target_*_failed` code. All are `None` until the journal
    says them, and for an imported row except `outcome`/`kind`/`owner`.
    """

    outcome: str | None = None
    kind: str | None = None
    owner: str | None = None
    error_code: str | None = None
    started: str | None = None
    finished: str | None = None
    target_calls: int | None = None


_SCORING_TERMINALS = ("scoring_finished", "scoring_failed", "scoring_interrupted")


@dataclass(frozen=True)
class TryScoring:
    """The scoring part of one try: the `scoring_started` record, if any, and
    its terminal, matched by the try key else by `score_request_id`.

    `status` is the trace's scoring status: `not_started` before any
    `scoring_started`, `unsettled` while no terminal is journaled, else the
    terminal's name (`scored`/`failed`/`interrupted`).
    """

    started: Envelope | None = None
    terminal: Envelope | None = None

    @property
    def status(self) -> str:
        if self.started is None:
            return "not_started"
        if self.terminal is None:
            return "unsettled"
        return {
            "scoring_finished": "scored",
            "scoring_failed": "failed",
            "scoring_interrupted": "interrupted",
        }[self.terminal.event]


@dataclass(frozen=True)
class TryState:
    """One try: one execution of one schedule pair (decision 6).

    A try is a `rollout_started` record keyed by `(case_id, sample_id,
    retry_ordinal)` (a missing ordinal is `0`), or one `sample_imported` row
    (`retry_ordinal` and `repair` are `None`/`False`, and `imported` carries
    the row's provenance and its journaled scoring result). `repeat` comes
    from the measurement identity's schedule. `start`/`terminal` are the
    rollout envelopes; envelope timestamps surface on
    `rollout.started`/`rollout.finished`.
    """

    case_id: str
    sample_id: str
    repeat: int | None
    retry_ordinal: int | None
    repair: bool
    imported: ImportedProvenance | None
    start: Envelope | None
    terminal: Envelope | None
    rollout: TryRollout
    scoring: TryScoring


def _ordinal(payload: Mapping[str, Any]) -> int:
    return int(payload.get("retry_ordinal", 0))


def tries(
    records: tuple[Envelope, ...] | list[Envelope], identity: Mapping[str, Any]
) -> list[TryState]:
    """Reduce one measurement journal prefix to its tries, in journal order.

    `identity` is the measurement identity (`records[0].payload["identity"]`);
    only its `schedule` is read, for each pair's `repeat`.
    """

    schedule = {
        (str(entry["case_id"]), str(entry["sample_id"])): int(entry["repeat"])
        for entry in identity.get("schedule", ())
    }

    def repeat_of(case_id: str, sample_id: str) -> int | None:
        return schedule.get((case_id, sample_id))

    # Index everything else once, keyed the way tries match them.
    rollout_terminals: dict[tuple[str, str, int], list[Envelope]] = {}
    target_failures: dict[tuple[str, str, int], list[Envelope]] = {}
    target_send_starts: dict[tuple[str, str, int], int] = {}
    scoring_started: dict[tuple[str, str, int], list[Envelope]] = {}
    scoring_by_request: dict[str, list[Envelope]] = {}
    for record in records:
        event = record.event
        payload = record.payload
        if event.startswith("rollout_") and event in (
            "rollout_finished",
            "rollout_failed",
            "rollout_interrupted",
        ):
            key = (str(payload["case_id"]), str(payload["sample_id"]), _ordinal(payload))
            rollout_terminals.setdefault(key, []).append(record)
        elif event == "target_send_started":
            key = (str(payload["case_id"]), str(payload["sample_id"]), _ordinal(payload))
            target_send_starts[key] = target_send_starts.get(key, 0) + 1
        elif event.startswith("target_") and event.endswith("_failed") and "case_id" in payload:
            key = (str(payload["case_id"]), str(payload["sample_id"]), _ordinal(payload))
            target_failures.setdefault(key, []).append(record)
        elif event == "scoring_started":
            key = (str(payload["case_id"]), str(payload["sample_id"]), _ordinal(payload))
            scoring_started.setdefault(key, []).append(record)
            scoring_by_request.setdefault(str(payload["score_request_id"]), []).append(record)
        elif event in _SCORING_TERMINALS:
            scoring_by_request.setdefault(str(payload["score_request_id"]), []).append(record)

    states = []
    for record in records:
        if record.event not in ("rollout_started", "sample_imported"):
            continue
        payload = record.payload
        case_id, sample_id = str(payload["case_id"]), str(payload["sample_id"])
        if record.event == "sample_imported":
            disposition = disposition_for(payload["outcome"])
            source = payload["source"]
            states.append(
                TryState(
                    case_id=case_id,
                    sample_id=sample_id,
                    repeat=repeat_of(case_id, sample_id),
                    retry_ordinal=None,
                    repair=False,
                    imported=ImportedProvenance(
                        run_id=str(source["run_id"]),
                        measurement_id=str(source["measurement_id"]),
                        retry_ordinal=int(source["retry_ordinal"]),
                        candidate_digest=str(source["candidate_digest"]),
                        metrics=dict(payload["metrics"]),
                        critical_pass=payload["critical_pass"],
                    ),
                    start=None,
                    terminal=None,
                    rollout=TryRollout(
                        outcome=str(payload["outcome"]),
                        kind=disposition.kind,
                        owner=disposition.owner,
                    ),
                    scoring=TryScoring(),
                )
            )
            continue
        key = (case_id, sample_id, _ordinal(payload))
        terminal = rollout_terminals.get(key, [None])[-1]
        outcome = None if terminal is None else cast(SampleOutcome, terminal.payload["outcome"])
        rollout_disposition = None if outcome is None else disposition_for(outcome)
        failures = target_failures.get(key, [])
        started_records = scoring_started.get(key, [])
        scoring_start = started_records[-1] if started_records else None
        scoring_terminal = None
        if scoring_start is not None:
            request_records = scoring_by_request.get(
                str(scoring_start.payload["score_request_id"]), []
            )
            terminals = [r for r in request_records if r.event in _SCORING_TERMINALS]
            scoring_terminal = terminals[-1] if terminals else None
        states.append(
            TryState(
                case_id=case_id,
                sample_id=sample_id,
                repeat=repeat_of(case_id, sample_id),
                retry_ordinal=_ordinal(payload),
                repair=payload.get("repair") is True,
                imported=None,
                start=record,
                terminal=terminal,
                rollout=TryRollout(
                    outcome=None if outcome is None else str(outcome),
                    kind=None if rollout_disposition is None else rollout_disposition.kind,
                    owner=None if rollout_disposition is None else rollout_disposition.owner,
                    error_code=(None if not failures else str(failures[-1].payload["error_code"])),
                    started=record.to_payload()["timestamp"],
                    finished=None if terminal is None else terminal.to_payload()["timestamp"],
                    target_calls=target_send_starts.get(key),
                ),
                scoring=TryScoring(started=scoring_start, terminal=scoring_terminal),
            )
        )
    return states


class JournalTail:
    """An incremental, read-only tail of one journal (decision 3).

    Opens through `append_log._directory`/`_file` with `O_RDONLY`, keeps the
    consumed byte offset, reads only `[offset, size)` and feeds the bytes up
    to the last newline to `append_log._scan` with the caller's existing
    state, so a half-written last line is never consumed and a resumed state
    continues exactly where the previous poll ended. Each poll re-validates
    the descriptor (`_validate_file`: regular, owner, `st_nlink == 1`) and
    checks the path's inode and size, so a replaced or truncated journal is a
    `JournalRewritten` and a corrupt complete line is `JournalUnreadable`.
    Nothing is ever locked or written.
    """

    def __init__(
        self,
        directory: str | Path,
        file_name: str,
        *,
        family: EnvelopeFamily,
        state: LogState,
        max_record_bytes: int,
        max_log_bytes: int,
    ) -> None:
        self._file_name = file_name
        self._family = family
        self._state = state
        self._max_record = max_record_bytes
        self._max_log = max_log_bytes
        self._directory_fd = -1
        self._fd = -1
        try:
            self._directory_fd = _directory(Path(directory))
            self._fd = _file(self._directory_fd, file_name, os.O_RDONLY)
        except OSError as exc:
            self.close()
            raise _fault(exc) from None
        except BaseException:
            # A refused open (unsafe entry, foreign owner) still holds the
            # directory descriptor; never leak it.
            self.close()
            raise
        self._offset = 0
        self._observed_size = 0
        self._inode = os.fstat(self._fd).st_ino

    def poll(self) -> tuple[Envelope, ...]:
        """Read, admit and return the records appended since the last poll."""
        # The path check comes first: a replaced file leaves the open
        # descriptor pointing at an unlinked inode (nlink 0), which the
        # descriptor validation below would fault on as journal_unavailable;
        # the observability contract names that fact `journal_rewritten`.
        try:
            info = os.stat(self._file_name, dir_fd=self._directory_fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError) as exc:
            # The path no longer names the followed file; deletion is the
            # replacement fact under decision 3's taxonomy.
            raise JournalRewritten("the journal was deleted mid-tail") from exc
        except OSError as exc:
            raise _fault(exc) from None
        if info.st_ino != self._inode:
            raise JournalRewritten("the journal file was replaced")
        if info.st_size < self._offset:
            raise JournalRewritten("the journal shrank below the consumed offset")
        _validate_file(self._fd)
        if info.st_size > self._max_log:
            raise JournalFault("journal_limit_exceeded")
        self._observed_size = info.st_size
        if info.st_size == self._offset:
            return ()
        try:
            raw = os.pread(self._fd, info.st_size - self._offset, self._offset)
        except OSError as exc:
            raise _fault(exc) from None
        end = raw.rfind(b"\n") + 1
        snapshot, _state = _scan(raw[:end], self._max_record, lambda: self._state, self._family)
        if snapshot.journal_integrity == "corrupt_suffix":
            raise JournalUnreadable()
        self._offset += snapshot.valid_bytes
        return snapshot.records

    @property
    def position(self) -> int:
        """The consumed byte offset: the journal head after the last poll."""
        return self._offset

    @property
    def observed_size(self) -> int:
        """Last verified physical size, including any incomplete last line."""
        return self._observed_size

    def close(self) -> None:
        for name in ("_fd", "_directory_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)

    def __enter__(self) -> JournalTail:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _empty_unborn_journal(journal: LogSnapshot) -> bool:
    """The scanner's exact zero-byte creation state, not an invalid prefix."""
    return (
        not journal.records
        and journal.valid_bytes == 0
        and journal.journal_integrity == "incomplete_tail"
        and len(journal.diagnostics) == 1
        and journal.diagnostics[0].get("category") == "incomplete_creation"
        and journal.diagnostics[0].get("length") == 0
    )


def _measurement_has_born_evidence(snapshot: CampaignSnapshot, measurement_id: str) -> bool:
    from evolution.tree.campaign_schema import recorded_complete

    entry = snapshot.measurements[measurement_id]
    return bool(
        snapshot.cited_ends.get(measurement_id, 0)
        or entry["stages"]
        or recorded_complete(entry, snapshot.rounds)
    )


def _captured_cost_records(
    snapshot: CampaignSnapshot,
    info: MeasurementInfo,
    prefixes: Mapping[str, tuple[Envelope, ...]],
) -> tuple[Envelope, ...]:
    records = prefixes.get(info.id, ())
    if not records and _measurement_has_born_evidence(snapshot, info.id):
        raise JournalFault("journal_unavailable")
    return records


def _cost_measurement_records(
    run_dir: Path, snapshot: CampaignSnapshot, info: MeasurementInfo
) -> tuple[Envelope, ...]:
    """Use inspection's exact prefix rule without treating lost work as unborn."""
    from evolution.tree.campaign_projections import inspection_prefix_readable
    from evolution.tree.execution.journal import inspect_journal

    cited_end = snapshot.cited_ends.get(info.id, 0)
    born_evidence = _measurement_has_born_evidence(snapshot, info.id)
    path = run_dir / "measurements" / info.id / "events.jsonl"
    if not path.exists() and not path.is_symlink():
        if born_evidence:
            raise JournalFault("journal_unavailable")
        return ()
    journal = inspect_journal(path.parent)
    if not born_evidence and _empty_unborn_journal(journal):
        return ()
    if (
        not inspection_prefix_readable(snapshot, info.id, journal)
        or journal.valid_bytes < cited_end
        or born_evidence
        and not journal.records
    ):
        raise JournalFault("journal_unavailable")
    return journal.records


def committed_campaign_work(
    run_dir: Path,
    snapshot: CampaignSnapshot,
    campaign_records: tuple[Envelope, ...],
    *,
    measurement_records: Mapping[str, tuple[Envelope, ...]] | None = None,
) -> dict[str, Any]:
    """Journal starts count actual new work; carried scores buy no executions.

    Connection journals are read only for operations explicitly bound by the
    campaign. Unborn measurements/probes contribute zero; unreadable evidence
    raises rather than inventing a paid-work count. Scorer starts count work,
    not unknown provider calls inside a declared Scorer program.
    """
    from evolution.tree.execution.journal import inspect_journal

    def counts(records: tuple[Envelope, ...]) -> dict[str, int]:
        return dict(
            scenario_slots=sum(r.event == "rollout_started" for r in records),
            imported_slots=sum(r.event == "sample_imported" for r in records),
            target_turns=sum(
                r.event == "target_send_started" and r.payload.get("call_attempt", 1) == 1
                for r in records
            ),
            transport_retries=sum(
                r.event.startswith("target_")
                and r.event.endswith("_started")
                and r.payload.get("call_attempt", 1) > 1
                for r in records
            ),
            scorer_executions=sum(r.event == "scoring_started" for r in records),
        )

    measured = counts(())
    preflight = counts(())
    for info in measurement_infos(snapshot):
        records = (
            _cost_measurement_records(run_dir, snapshot, info)
            if measurement_records is None
            else _captured_cost_records(snapshot, info, measurement_records)
        )
        for name, value in counts(records).items():
            measured[name] += value
    for operation in snapshot.connection.operations if measurement_records is None else ():
        path = run_dir / "connection" / str(operation["operation_id"])
        evidence = [
            ref
            for record in campaign_records
            if record.operation_id == operation["operation_id"]
            and record.event
            in {
                "connection_preflight_finished",
                "connection_preflight_failed",
                "connection_preflight_interrupted",
            }
            for ref in record.payload["evidence"]
        ]
        cited_end = max((int(ref["locator"].rsplit(":", 1)[1]) for ref in evidence), default=0)
        if not (path / "events.jsonl").exists() and not (path / "events.jsonl").is_symlink():
            if cited_end:
                raise JournalFault("journal_unavailable")
            continue
        journal = inspect_journal(path)
        if not cited_end and _empty_unborn_journal(journal):
            continue
        if journal.journal_integrity != "complete" or journal.valid_bytes < cited_end:
            raise JournalFault("journal_unavailable")
        for name, value in counts(journal.records).items():
            preflight[name] += value
    return dict(
        measurement=measured,
        connection_preflight=preflight if measurement_records is None else None,
        role_requests=sum(
            r.event in {"grouping_started", "reflection_started", "editor_started"}
            for r in campaign_records
        ),
        reflector_probe_requests=sum(r.event == "probe_completed" for r in campaign_records),
        interpretation="journaled_work_not_tokens_currency_or_provider_call_estimates",
    )


def remaining_measurement_slots(
    run_dir: Path,
    snapshot: CampaignSnapshot,
    *,
    measurement_records: Mapping[str, tuple[Envelope, ...]] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """Unscored scheduled slots, crediting effective imported/scored pairs once.

    This is a complete-scoring, no-further-retry scenario. A bound but unborn
    measurement is left to its declaration's known schedule/import counts.
    """
    from evolution.tree.campaign_view import owed_rows

    remaining = {}
    scheduled = {}
    for info in measurement_infos(snapshot):
        records = (
            _cost_measurement_records(run_dir, snapshot, info)
            if measurement_records is None
            else _captured_cost_records(snapshot, info, measurement_records)
        )
        if not records:
            continue
        identity = records[0].payload["identity"]
        effective = {(s.case_id, s.sample_id): s for s in tries(records, identity)}
        schedule = owed_rows(snapshot, info.id)
        keys = {(s["case_id"], s["sample_id"]) for s in schedule}
        scored = sum(
            (s.imported is not None or s.scoring.status == "scored")
            and (s.case_id, s.sample_id) in keys
            for s in effective.values()
        )
        remaining[info.id] = max(0, len(schedule) - scored)
        scheduled[info.id] = len(schedule)
    return remaining, scheduled
