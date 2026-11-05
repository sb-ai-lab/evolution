"""The `evolution-v2-trace/1` record builder and export driver (#197).

`evo tree trace` writes one record per try of a campaign or evaluation run;
this module builds those records (decisions 7, 8, 9, 11, 12; maintainer
ruling 1 of 2026-10-12) and drives the export (decisions 9, 10, 13, 15) — the
CLI itself lives in `cli.py`. Every part of a record carries exactly the
provenance spec §3.4 allows: journal-bound fields from the measurement
journal, the request authenticated against `scoring_started.input_digest`
when the corpus is supplied, scorer output labelled but never authenticated,
and nothing labelled beyond what a check proves. Scorer traces are read
through `append_log._directory`/`_file` (owner, no-follow, single link); an
unreadable trace file is `scoring.trace: "missing"`, never an exception.
Nothing here writes into a run directory.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from evolution import __version__
from evolution.lineage.state_io import open_private_file
from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.baseline_import import carried_traces
from evolution.tree.campaign_projections import (
    CAMPAIGN_FILE,
    MEASUREMENTS_DIR,
)
from evolution.tree.campaign_schema import (
    CampaignSnapshot,
)
from evolution.tree.evaluation_journal import EVALUATION_FILE
from evolution.tree.evaluation_manifest import EVALUATION_SCHEMA
from evolution.tree.execution.config import ManifestError
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal import (
    inspect_journal,
)
from evolution.tree.execution.journal_identity import (
    canonical_json,
    corpus_digest,
    scoring_input_digest,
)
from evolution.tree.execution.rollout_scoring import ScoringError, _json_value, parse_scorer_stdout
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15, SCHEMA_V16, WorkflowSections
from evolution.tree.roles.editor.editor_evidence import (
    _INPUT_KEYS,
    _REQUEST_KEYS,
    CarriedTraces,
    _TraceView,
)
from evolution.tree.run_reader import (
    JournalUnreadable,
    MeasurementInfo,
    RunHandle,
    RunKind,
    TryScoring,
    TryState,
    measurement_infos,
    open_run,
    tries,
)
from evolution.tree.storage.append_log import (
    _directory,
    _fault,
    _file,
)
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalFault

__all__ = [
    "ScorerTrace",
    "TraceDirReader",
    "TraceOutputUnavailable",
    "TraceRefused",
    "TraceSummary",
    "build_try",
    "cap_fields",
    "check_request",
    "export_run",
    "header",
    "redact_try",
    "summary_line",
]

TRUNCATED = "[REDACTED]"


@dataclass(frozen=True)
class _JournaledCandidate:
    """The journaled candidate stand-in for the request binding (decision 7).

    A journaled candidate digest names no readable paths: like the store's
    `ArtifactRef.candidate_view` it carries `artifact_paths=()` and a verifier,
    which is the encoding `scoring_input_digest` hashes.
    """

    digest: str

    @property
    def artifact_paths(self) -> tuple[str, ...]:
        return ()

    @property
    def verifier(self) -> Callable[[], str]:
        return self.verify

    def verify(self) -> str:
        return self.digest


def check_request(
    document: Any,
    started: Envelope,
    case: EvalCase | None,
    scorer: Mapping[str, Any],
    candidate_digest: str,
) -> str:
    """How far the trace's `request.json` is checked (decision 7, spec §3.4).

    Mirrors `editor_evidence._read_and_bind` on the already-parsed document
    (one read, no TOCTOU): shape, `trace_id` against the journaled
    `score_request_id`, `scorer` against the journaled scorer digest, the
    document's own `input_sha256` over its `input`, then — only with a corpus
    — the corpus case id, `public`/`oracle` by canonical JSON, and the
    journal binding `scoring_input_digest(case, view, candidate, scorer)`
    against `scoring_started.input_digest`. `authenticated` needs every
    check; `mismatch` when any fails; `unauthenticated` when no corpus was
    given and the self-hash holds. Never raises: a check that cannot run is
    a failed check.
    """

    try:
        if not isinstance(document, Mapping) or set(document) != _REQUEST_KEYS:
            return "mismatch"
        if document["trace_id"] != str(started.payload["score_request_id"]):
            return "mismatch"
        if document["scorer"] != scorer.get("digest"):
            return "mismatch"
        payload = document["input"]
        if not isinstance(payload, Mapping) or set(payload) != _INPUT_KEYS:
            return "mismatch"
        if not isinstance(payload["view"], Mapping):
            return "mismatch"
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != document["input_sha256"]:
            return "mismatch"
        if case is None:
            return "unauthenticated"
        if payload["case_id"] != case.case_id:
            return "mismatch"
        if canonical_json(payload["public"]) != canonical_json(_json_value(case.public)):
            return "mismatch"
        if canonical_json(payload["oracle"]) != canonical_json(_json_value(case.oracle)):
            return "mismatch"
        digest = scoring_input_digest(
            case,
            _TraceView(payload["view"]),
            _JournaledCandidate(candidate_digest),
            scorer,
        )
        if digest != started.payload.get("input_digest"):
            return "mismatch"
        return "authenticated"
    except Exception:
        # The same reach as `_read_and_bind`'s guarded caller: a document,
        # corpus or digest layer that fails to compare is a mismatch, not a
        # traceback in the middle of an export.
        return "mismatch"


@dataclass(frozen=True)
class ScorerTrace:
    """One scorer trace's files, read through the safe-entry checks."""

    request: Any  #: the parsed `request.json` document
    stdout: str | None  #: the raw `stdout`, `None` when it cannot be read


class TraceDirReader:
    """One measurement's trace inputs for the record builder.

    A thin wrapper over the shared `ScorerTraceReader` (ADR-0040 D6, ADR-0035
    amendment of 2026-10-16), which reads the directory layout and packs
    through the owner, no-follow and single-link checks. A trace that cannot
    be read -- absent, unsafe, corrupt, ambiguous, unparseable -- makes
    `trace()` return `None`, never an exception, and the record labels
    `scoring.trace: "missing"`. `records` is the measurement's journaled
    prefix: the source of ruling 1's journal-filled turns when no scorer
    trace exists (the journal data needs no manifest). The export driver
    builds one reader per measurement journal it reads and closes it after
    the last try.
    """

    def __init__(self, run_dir: str | Path, records: Sequence[Envelope]) -> None:
        self._records = tuple(records)
        from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

        self._traces = DiagnosticTraceReader(run_dir)

    def trace(
        self, score_request_id: str, *, measurement_id: str | None = None
    ) -> ScorerTrace | None:
        """The trace's `request.json` and `stdout`, or `None` when unreadable.

        `measurement_id` is the `run_id` of the try's `scoring_started`
        envelope, which names its pack; the two sections are read apart, as
        the two files were, so an unreadable `stdout` leaves the request.
        """

        try:
            head = self._traces.read(
                score_request_id, measurement_id=measurement_id, sections=("request",)
            )
            if head is None or head.request is None:
                return None
            document = json.loads(head.request.decode("utf-8"))
        except ArtifactCorruptionError:
            raise
        except Exception:
            return None
        stdout: str | None
        try:
            output = self._traces.read(
                score_request_id, measurement_id=measurement_id, sections=("stdout",)
            )
            stdout = (
                None if output is None or output.stdout is None else output.stdout.decode("utf-8")
            )
        except ArtifactCorruptionError:
            raise
        except Exception:
            stdout = None
        return ScorerTrace(request=document, stdout=stdout)

    def journal_turns(self, try_state: TryState) -> list[dict[str, Any]]:
        """Ruling 1's turns for the try: one entry per `target_send_finished`."""

        return _journal_turns(self._records, try_state)

    def close(self) -> None:
        self._traces.close()

    def __enter__(self) -> TraceDirReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _journal_turns(records: Sequence[Envelope], try_state: TryState) -> list[dict[str, Any]]:
    """One entry per `target_send_finished` of the try (maintainer ruling 1).

    `index` is the journaled turn, `latency_ms` the `target_latency_ms` and
    `usage` the journaled usage; `message`, `events`, `outcome` and `reason`
    stay `null` — only a scorer trace carries them. `[]` only when no turn
    finished. An imported row sent nothing in this journal: `[]`.
    """

    if try_state.retry_ordinal is None:
        return []
    key = (try_state.case_id, try_state.sample_id, try_state.retry_ordinal)
    turns = []
    for record in records:
        if record.event != "target_send_finished":
            continue
        payload = record.payload
        if (
            str(payload["case_id"]),
            str(payload["sample_id"]),
            int(payload.get("retry_ordinal", 0)),
        ) != key:
            continue
        turns.append(
            {
                "index": payload["turn"],
                "message": None,
                "events": None,
                "usage": payload.get("usage"),
                "latency_ms": payload.get("target_latency_ms"),
                "outcome": None,
                "reason": None,
            }
        )
    return turns


def _scorer_output(stdout: str, metrics: Any) -> dict[str, Any] | None:
    """The parsed scorer output, always labelled, `None` when it does not parse."""

    try:
        parsed = parse_scorer_stdout(stdout)
    except ScoringError:
        return None
    return {
        "check": "unbound" if parsed.get("metrics") == metrics else "metrics_differ",
        "metrics": parsed.get("metrics"),
        "verdicts": parsed.get("verdicts"),
        "public_feedback": parsed.get("public_feedback"),
        "details": parsed.get("details"),
        "diagnostics": parsed.get("diagnostics"),
    }


def _trace_turns(view: Mapping[str, Any]) -> Any:
    history = view.get("history")
    turns = history.get("turns") if isinstance(history, Mapping) else None
    return turns if isinstance(turns, list) else None


def _measurement(info: MeasurementInfo) -> dict[str, Any]:
    measurement = {
        "id": info.id,
        "role": info.role,
        "round": info.round,
        "attempt_id": info.attempt_id,
        "candidate_digest": info.candidate_digest,
        "epoch": info.epoch,
        "rebound_from": info.rebound_from,
    }
    if info.cohort_role is not None:
        measurement["cohort_role"] = info.cohort_role
    return measurement


def _imported(try_state: TryState) -> dict[str, Any] | None:
    if try_state.imported is None:
        return None
    return {
        "run_id": try_state.imported.run_id,
        "measurement_id": try_state.imported.measurement_id,
        "retry_ordinal": try_state.imported.retry_ordinal,
        "candidate_digest": try_state.imported.candidate_digest,
    }


def _candidate_digest(info: MeasurementInfo, try_state: TryState) -> str:
    if try_state.imported is not None:
        return try_state.imported.candidate_digest
    return info.candidate_digest


def build_try(
    info: MeasurementInfo,
    try_state: TryState,
    *,
    trace_dir_reader: TraceDirReader,
    corpus: Mapping[str, EvalCase] | None,
    carried: CarriedTraces | None,
) -> dict[str, Any]:
    """One `evolution-v2-trace/1` try record (spec §3.2, §3.4).

    Journal-bound fields come from `try_state`; the scorer trace — when the
    journal started a scoring and the trace reads — supplies the request,
    turns and oracle, with `request.check` per `check_request`; otherwise the
    corpus supplies `request.public`/`oracle` for a journal-bound try and the
    journal supplies ruling 1's turns. A `mismatch` keeps the check and
    suppresses the content; an imported try takes its content from the
    `carried` source pair when there is one, and otherwise keeps the row's
    provenance and journaled metrics with `scoring.status: "scored"` —
    content stays null in both no-pair shapes (decision 9, spec §3.2).
    """

    if try_state.imported is not None:
        pair = (carried or {}).get((try_state.case_id, try_state.sample_id))
        started = pair[0] if pair is not None else None
        terminal = pair[1] if pair is not None else None
    else:
        started = try_state.scoring.started
        terminal = try_state.scoring.terminal

    if started is None:
        request_id = None
        trace = None
        trace_label = "missing" if try_state.imported is not None else "not_applicable"
    else:
        request_id = str(started.payload["score_request_id"])
        trace = trace_dir_reader.trace(
            request_id, measurement_id=str(started.to_payload()["run_id"])
        )
        trace_label = "present" if trace is not None else "missing"

    if try_state.imported is None:
        status = try_state.scoring.status
    elif terminal is not None:
        status = TryScoring(started=started, terminal=terminal).status
    else:
        # No source pair: the imported row itself is the journal's scoring
        # evidence — `sample_imported` requires a score-class outcome and the
        # journaled metrics, so "scored" is the truthful label (spec §3.2).
        status = "scored"

    metrics = None
    critical_pass = None
    if terminal is not None and terminal.event == "scoring_finished":
        metrics = terminal.payload["metrics"]
        critical_pass = terminal.payload["critical_pass"]
    elif try_state.imported is not None:
        # A carry without its source's trace keeps identity, metrics and
        # provenance from the row (spec §3.2); content stays null (decision 9).
        metrics = try_state.imported.metrics
        critical_pass = try_state.imported.critical_pass

    output = None
    if trace is not None and trace.stdout is not None:
        output = _scorer_output(trace.stdout, metrics)

    case = None if corpus is None else corpus.get(try_state.case_id)
    if trace is not None:
        assert started is not None  # a trace is only read for a started scoring
        check = check_request(
            trace.request,
            started,
            case,
            started.payload["scorer"],
            _candidate_digest(info, try_state),
        )
        if check == "mismatch":
            request: dict[str, Any] | None = {
                "source": "scorer_trace",
                "check": "mismatch",
                "public": None,
            }
            turns = None
            oracle = None
        else:
            payload = trace.request["input"]
            request = {"source": "scorer_trace", "check": check, "public": payload["public"]}
            view = payload["view"]
            turns = _trace_turns(view) if isinstance(view, Mapping) else None
            oracle = payload["oracle"]
    elif try_state.imported is not None:
        # A carry without its source's trace gets provenance only (decision 9),
        # never corpus content: the corpus belongs to this measurement's own tries.
        request = None
        turns = None
        oracle = None
    elif case is not None:
        request = {"source": "corpus", "check": "corpus", "public": _json_value(case.public)}
        turns = trace_dir_reader.journal_turns(try_state)
        oracle = _json_value(case.oracle)
    else:
        request = None
        turns = trace_dir_reader.journal_turns(try_state)
        oracle = None

    return {
        "record": "try",
        "measurement": _measurement(info),
        "case_id": try_state.case_id,
        "sample_id": try_state.sample_id,
        "repeat": try_state.repeat,
        "retry_ordinal": try_state.retry_ordinal,
        "repair": try_state.repair,
        "imported": _imported(try_state),
        "request": request,
        "turns": turns,
        "oracle": oracle,
        "rollout": {
            "outcome": try_state.rollout.outcome,
            "class": try_state.rollout.kind,
            "owner": try_state.rollout.owner,
            "error_code": try_state.rollout.error_code,
            "started": try_state.rollout.started,
            "finished": try_state.rollout.finished,
            "target_calls": try_state.rollout.target_calls,
        },
        "scoring": {
            "status": status,
            "score_request_id": request_id,
            "trace": trace_label,
            "metrics": metrics,
            "critical_pass": critical_pass,
            "output": output,
        },
    }


def header(
    *,
    run_id: str,
    kind: str,
    family: int | None,
    producing_version: str | None,
    journal_heads: Mapping[str, int],
    corpus: str,
    redaction: str,
    max_field_bytes: int,
) -> dict[str, Any]:
    """The `evolution-v2-trace/1` header record (decision 15, spec §3.2).

    `corpus` is `verified`/`absent`, `redaction` one of
    `campaign_secret_set`/`evaluation_secret_set`/`none`; the header claims
    only secret-set redaction, so the driver never runs `redact_try` or
    `cap_fields` over it.
    """

    return {
        "record": "header",
        "schema": "evolution-v2-trace/1",
        "run_id": run_id,
        "kind": kind,
        "family": family,
        "producing_version": producing_version,
        "generated_by": __version__,
        "journal_heads": dict(journal_heads),
        "corpus": corpus,
        "redaction": redaction,
        "max_field_bytes": max_field_bytes,
    }


def redact_try(record: dict[str, Any], secrets: Sequence[str]) -> dict[str, Any]:
    """Replace every secret occurrence in string values and dict keys (decision 11).

    A value replacer, not `storage.redaction.safe_record`: task-data keys such as `token`
    are not credential-shaped and stay untouched. Returns a new record; the
    header never goes through this.
    """

    needles = tuple(sorted((secret for secret in secrets if secret), key=len, reverse=True))
    if not needles:
        return record

    def scrub(text: str) -> str:
        for needle in needles:
            text = text.replace(needle, TRUNCATED)
        return text

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, dict):
            return {
                (scrub(key) if isinstance(key, str) else key): walk(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(record)


def cap_fields(record: dict[str, Any], max_field_bytes: int) -> dict[str, Any]:
    """Cap every string value of a try record at `max_field_bytes` (decision 12).

    Applied after `redact_try`, so no `head` or hash carries a secret: a
    longer string becomes `{"truncated": true, "bytes", "sha256", "head"}`,
    `head` cut at a UTF-8 boundary. Keys and the header are never capped.
    Returns a new record.
    """

    def cap(value: Any) -> Any:
        if isinstance(value, str):
            data = value.encode("utf-8")
            if len(data) <= max_field_bytes:
                return value
            return {
                "truncated": True,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "head": data[:max_field_bytes].decode("utf-8", errors="ignore"),
            }
        if isinstance(value, dict):
            return {key: cap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cap(item) for item in value]
        return value

    return cap(record)


# -- the export driver (Task 4: decisions 9, 10, 13, 15; spec §3.1) -------------

#: Preserve the existing trace binding distinction: /15 selects train; /16
#: uses load_reflection_corpus. ADR-0046 removes retired entries, not native
#: behavior differences. A /16 trace partition correction is separate work.
_PARTITIONED_SCHEMAS = (SCHEMA_V15,)


class TraceRefused(Exception):
    """`evo tree trace` refuses this invocation (exit 2).

    `code` is one of the documented refusal codes (`not_a_run_directory`,
    `run_dir_ambiguous`, `trace_out_exists`, `trace_out_inside_run_dir`,
    `manifest_kind_mismatch`, `corpus_digest_mismatch`, `measurement_unknown`),
    or a loader/family code passed through with that refusal's own `str()` —
    which names a code and a manifest key path, never a filesystem path or a
    document value (spec §9).
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TraceOutputUnavailable(Exception):
    """The `--out` file could not be created (exit 1, the generic error exit).

    `code` is the lowercase errno name (`eacces`, `enospc`, ...), rendered on
    stderr without any filesystem path (spec §9). This line is not a promised
    surface; the exit code is the contract.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, "oserror").lower()


@dataclass(frozen=True)
class TraceSummary:
    """The export's counts, rendered on stderr by `summary_line`."""

    tries: int
    scored: int
    failed: int
    unavailable: int
    unverified: int
    in_flight: int
    unset_secrets: int


def summary_line(summary: TraceSummary) -> str:
    """The one stderr line every export ends with (decision 13, spec §3.1)."""

    line = (
        f"evo tree trace: {summary.tries} tries written · {summary.scored} scored · "
        f"{summary.failed} failed · {summary.unavailable} unavailable · "
        f"{summary.unverified} unverified · {summary.in_flight} in flight"
    )
    if summary.unset_secrets:
        line += f" · {summary.unset_secrets} declared secret variables unset"
    return line


def export_run(
    *,
    run_dir: str | Path,
    out: str | Path,
    manifest: str | Path | None = None,
    measurements: Sequence[str] = (),
    max_field_bytes: int = 65536,
    environ: Mapping[str, str] | None = None,
) -> TraceSummary:
    """Export every try of one run as `evolution-v2-trace/1` JSON lines.

    The run directory is only ever read; `--out` is created owner-only
    (`0600`) beside the operator's cwd, line by line, and removed again when a
    journal fault interrupts the export after creation. `--out -` writes the
    trace to stdout. Refusals raise `TraceRefused` (exit 2); a journal that
    cannot be read raises the journal package's own `JournalFault` or
    `JournalUnreadable` (exit 1); a failure to create `--out` raises
    `TraceOutputUnavailable` (exit 1, path-free). Nothing is written under
    `run_dir`.
    """

    environment = dict(os.environ) if environ is None else dict(environ)
    root = Path(run_dir)
    out_path = Path(out)
    to_stdout = str(out) == "-"
    if not to_stdout:
        if out_path.exists():
            raise TraceRefused("trace_out_exists")
        run_real = os.path.realpath(root)
        parent_real = os.path.realpath(out_path.parent)
        if parent_real == run_real or Path(run_real) in Path(parent_real).parents:
            raise TraceRefused("trace_out_inside_run_dir")

    from evolution.tree.campaign import CampaignFamilyUnsupported

    try:
        handle = open_run(root)
    except CampaignFamilyUnsupported as exc:
        location = f" at {exc.path}; expected {exc.expected}" if exc.expected else ""
        raise TraceRefused(f"{exc}{location}") from exc

    corpus, secrets, unset, redaction = _manifest_binding(handle, manifest, environment)

    infos = measurement_infos(handle.snapshot)
    if measurements:
        known = {info.id for info in infos}
        if any(name not in known for name in measurements):
            raise TraceRefused("measurement_unknown")
        requested = set(measurements)
        selected = [info for info in infos if info.id in requested]
    else:
        selected = list(infos)

    run_journal, run_head = _run_journal_head(handle)
    journal_heads: dict[str, int] = {run_journal: run_head}
    if isinstance(handle.snapshot, CampaignSnapshot):
        campaign = handle.snapshot
        run_id = campaign.run_id
        producing_version: str | None = campaign.producing_version or None
    else:
        log = handle.snapshot
        run_id = str(log.records[0].to_payload()["run_id"])
        producing_version = None  # the bound child's `run_created`, read below

    prepared = []
    for info in selected:
        if not info.complete and _measurement_journal_unborn(handle.run_dir, info.id):
            continue
        directory = handle.run_dir / MEASUREMENTS_DIR / info.id
        journal = inspect_journal(directory)
        if journal.journal_integrity not in ("complete", "incomplete_tail") or not journal.records:
            raise JournalUnreadable()
        journal_heads[f"{MEASUREMENTS_DIR}/{info.id}/events.jsonl"] = journal.valid_bytes
        if corpus is not None and handle.kind is RunKind.EVALUATION:
            identity = journal.records[0].payload["identity"]
            if corpus_digest(list(corpus.values())) != identity["corpus_digest"]:
                raise TraceRefused("corpus_digest_mismatch")
        if handle.kind is RunKind.EVALUATION:
            producing_version = journal.records[0].payload.get("producing_version")
        prepared.append(
            (
                info,
                journal.records,
                carried_traces(journal.records, run_root=handle.run_dir, run_id=run_id),
            )
        )

    if corpus is not None and handle.kind is RunKind.EVALUATION and not prepared:
        # An evaluation's primary journal cannot authenticate the corpus:
        # only a readable child's creation identity supplies the digest.
        raise JournalFault("journal_unavailable")

    header_record = header(
        run_id=run_id,
        kind=str(handle.kind),
        family=handle.family,
        producing_version=producing_version,
        journal_heads=journal_heads,
        corpus="verified" if manifest is not None else "absent",
        redaction=redaction,
        max_field_bytes=max_field_bytes,
    )

    counts = {
        "tries": 0,
        "scored": 0,
        "failed": 0,
        "unavailable": 0,
        "unverified": 0,
        "in_flight": 0,
    }

    def render() -> Iterator[dict[str, Any]]:
        yield header_record
        for info, records, carried in prepared:
            with TraceDirReader(handle.run_dir, records) as reader:
                for state in tries(records, records[0].payload["identity"]):
                    if state.terminal is None and state.imported is None:
                        counts["in_flight"] += 1
                        continue
                    record = build_try(
                        info,
                        state,
                        trace_dir_reader=reader,
                        corpus=corpus,
                        carried=carried,
                    )
                    record = cap_fields(redact_try(record, secrets), max_field_bytes)
                    _count(record, counts)
                    yield record

    if to_stdout:
        _write_lines(sys.stdout, render())
    else:
        try:
            fd = open_private_file(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except ValueError as exc:
            # A dangling symlink (or another non-regular entry) slid past the
            # existence check: the name is taken, and the code says so.
            raise TraceRefused("trace_out_exists") from exc
        except FileExistsError as exc:
            # The `exists()` pre-check and this `O_EXCL` create are not
            # atomic: a name another actor won in between is the same
            # refusal, never a traceback naming the path.
            raise TraceRefused("trace_out_exists") from exc
        except OSError as exc:
            raise TraceOutputUnavailable(_errno_name(exc)) from exc
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # The fd is not owned by any file object yet, and the just-created
            # file must not be left behind (decision 13's partial-file rule).
            os.close(fd)
            with suppress(OSError):
                os.unlink(out_path)
            raise
        try:
            with stream:
                _write_lines(stream, render())
        except BaseException:
            with suppress(OSError):
                os.unlink(out_path)
            raise
    return TraceSummary(
        tries=counts["tries"],
        scored=counts["scored"],
        failed=counts["failed"],
        unavailable=counts["unavailable"],
        unverified=counts["unverified"],
        in_flight=counts["in_flight"],
        unset_secrets=unset,
    )


def _count(record: Mapping[str, Any], counts: dict[str, int]) -> None:
    """One try's contribution to the summary buckets (decision 13)."""

    counts["tries"] += 1
    status = record["scoring"]["status"]
    if record["rollout"]["class"] == "score" and status == "scored":
        counts["scored"] += 1
    if record["rollout"]["class"] != "score" or status in ("failed", "interrupted"):
        counts["failed"] += 1
    request = record["request"]
    if request is None:
        counts["unavailable"] += 1
    elif request["check"] in ("unauthenticated", "mismatch"):
        counts["unverified"] += 1


def _write_lines(stream: TextIO, records: Iterator[dict[str, Any]]) -> None:
    for record in records:
        # No `sort_keys`: the header's `journal_heads` is ordered — it names
        # every journal read, in read order (decision 15).
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()


def _run_journal_head(handle: RunHandle) -> tuple[str, int]:
    """The run journal's `(relative name, valid_bytes)`, in read order first."""

    name = EVALUATION_FILE if handle.kind is RunKind.EVALUATION else CAMPAIGN_FILE
    return name, handle.valid_bytes


def _measurement_journal_unborn(root: Path, measurement_id: str) -> bool:
    """Recognize safe ENOENT only after admitting every existing parent.

    A bound child can be not yet born; symlinks, foreign owners and other
    read failures cannot stand in for that state. Check descriptor-relative
    components so a missing descendant never hides an unsafe parent.
    """

    if Path(measurement_id).name != measurement_id or measurement_id in (".", ".."):
        raise JournalFault()
    try:
        directory = _directory(root)
    except OSError as exc:
        raise _fault(exc) from None
    fd = -1
    try:
        for name in (MEASUREMENTS_DIR, measurement_id):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            if os.fstat(directory).st_uid != os.getuid():
                raise JournalFault()
        fd = _file(directory, "events.jsonl", os.O_RDONLY)
        return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise _fault(exc) from None
    finally:
        if fd >= 0:
            os.close(fd)
        if directory >= 0:
            os.close(directory)


def _manifest_binding(
    handle: RunHandle,
    manifest: str | Path | None,
    environ: Mapping[str, str],
) -> tuple[Mapping[str, EvalCase] | None, tuple[str, ...], int, str]:
    """The corpus map, the secret set, the unset-name count, the redaction label.

    With no `--manifest` the export runs unauthenticated: no corpus, no
    redaction, no unset count. The manifest's `schema` string decides the
    kind (decision 10): the other kind refuses `manifest_kind_mismatch`, and a
    document neither schema family admits falls to the run kind's own loader,
    whose refusal keeps its own code.
    """

    if manifest is None:
        return None, (), 0, "none"
    path = Path(manifest)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        schema = document.get("schema") if isinstance(document, dict) else None
    except (OSError, UnicodeError, ValueError):
        schema = None
    if schema is not None:
        if str(schema).startswith("evolution-v2-manifest/") and handle.kind is not RunKind.CAMPAIGN:
            raise TraceRefused("manifest_kind_mismatch")
        if schema == EVALUATION_SCHEMA and handle.kind is not RunKind.EVALUATION:
            raise TraceRefused("manifest_kind_mismatch")
    if handle.kind is RunKind.CAMPAIGN:
        return _bind_campaign_manifest(path, cast(CampaignSnapshot, handle.snapshot), environ)
    return _bind_evaluation_manifest(path, environ)


def _bind_campaign_manifest(
    path: Path,
    campaign: CampaignSnapshot,
    environ: Mapping[str, str],
) -> tuple[Mapping[str, EvalCase], tuple[str, ...], int, str]:
    from evolution.tree.bindings import BindingError
    from evolution.tree.execution.corpus import CorpusError, load_reflection_corpus
    from evolution.tree.execution.corpus_partition import load_partition
    from evolution.tree.execution.output_admission import EvolveRefused
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2

    try:
        admitted = load_manifest_v2(path)
    except (ManifestError, BindingError, EvolveRefused) as exc:
        if isinstance(exc, ManifestError) and exc.code == "manifest_schema":
            raise TraceRefused(f"{exc}; {exc.expected}") from exc
        raise TraceRefused(str(exc)) from exc
    workflow = admitted.workflow
    if admitted.schema not in (SCHEMA_V15, SCHEMA_V16) or workflow is None:
        # File-loaded native campaigns always have a workflow. In-memory
        # conformance input is not a campaign manifest, despite sharing a type.
        raise TraceRefused("manifest_schema: schema; expected evolution-v2-manifest/15 or /16")
    try:
        if admitted.schema in _PARTITIONED_SCHEMAS:
            bound = load_partition(workflow.corpus_path).select("train")
        else:
            bound = load_reflection_corpus(workflow.corpus_path)
    except CorpusError as exc:
        raise TraceRefused(str(exc)) from exc
    if corpus_digest(bound.cases) != campaign.identity["corpus_digest"]:
        raise TraceRefused("corpus_digest_mismatch")
    secrets, unset = _campaign_trace_secrets(workflow, environ)
    return (
        {case.case_id: case for case in bound.cases},
        secrets,
        unset,
        "campaign_secret_set",
    )


def _campaign_trace_secrets(
    workflow: WorkflowSections, environ: Mapping[str, str]
) -> tuple[tuple[str, ...], int]:
    """Available campaign needles and the count of unset declared names.

    Unlike pre-run `redactions_for`, an export must keep every available
    value when another role cannot run in this shell. Reuse the pure name,
    child-environment and floor helpers, without execution-time credential
    validation. Chat proxies contribute their original bytes even when the
    current value is invalid. Agent values keep the campaign's curated and
    unsafe-environment rules, including its runtime-name exclusion.
    """

    from evolution.eval.child_env import _RUNTIME_ENV_KEYS
    from evolution.tree.bindings import (
        bound_child_environment,
        editor_environment_names,
        grouper_environment_names,
    )
    from evolution.tree.evolve import _MIN_SECRET_LENGTH, _secrets
    from evolution.tree.roles.config import (
        AgentCliEditorSection,
        AgentCliGrouperSection,
        CommandGroupingSection,
        EditorSection,
        GrouperSection,
        ModelGroupingSection,
    )

    names = set(workflow.scorer.allowed_env)
    values = list(_secrets(workflow.scorer.allowed_env, environ=environ))

    def role_values(section: EditorSection) -> tuple[str, ...]:
        declared = editor_environment_names(section)
        names.update(declared)
        if isinstance(section, AgentCliEditorSection):
            bound = bound_child_environment(section, environ)
            return tuple(
                value
                for name, value in sorted(bound.items())
                if name not in _RUNTIME_ENV_KEYS and len(value) >= _MIN_SECRET_LENGTH
            )
        return _secrets(declared, environ=environ)

    def grouping_values(section: GrouperSection) -> tuple[str, ...]:
        if isinstance(section, AgentCliGrouperSection):
            return role_values(section.role)
        declared = grouper_environment_names(section)
        names.update(declared)
        return _secrets(declared, environ=environ)

    values.extend(role_values(workflow.editor))
    values.extend(role_values(workflow.reflector.role))
    grouping = workflow.reflector.grouping
    if isinstance(grouping, ModelGroupingSection):
        values.extend(grouping_values(grouping))
    if workflow.grouper is not None:
        values.extend(grouping_values(workflow.grouper))
    if isinstance(grouping, CommandGroupingSection):
        values.extend(grouping_values(grouping))
    unset = sum(1 for name in names if not environ.get(name))
    return tuple(dict.fromkeys(values)), unset


def _bind_evaluation_manifest(
    path: Path,
    environ: Mapping[str, str],
) -> tuple[Mapping[str, EvalCase], tuple[str, ...], int, str]:
    from evolution.tree.bindings import BindingError
    from evolution.tree.evaluation import evaluation_secrets
    from evolution.tree.evaluation_manifest import load_evaluation_manifest
    from evolution.tree.execution.corpus import CorpusError
    from evolution.tree.execution.corpus_partition import load_partition
    from evolution.tree.execution.output_admission import EvolveRefused

    try:
        admitted = load_evaluation_manifest(path)
    except (ManifestError, BindingError, EvolveRefused) as exc:
        raise TraceRefused(str(exc)) from exc
    try:
        bound = load_partition(admitted.corpus).select(admitted.split)
    except CorpusError as exc:
        raise TraceRefused(str(exc)) from exc
    unset = sum(1 for name in admitted.scorer.allowed_env if not environ.get(name))
    return (
        {case.case_id: case for case in bound.cases},
        evaluation_secrets(admitted, environ),
        unset,
        "evaluation_secret_set",
    )
