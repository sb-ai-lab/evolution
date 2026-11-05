"""Private preparation and local resolution of baseline-diagnostic-bundle/1 (ADR-0043).

This module takes already admitted scalar rows and frozen source evidence. It
never admits scalar rows. Preparation is pure; the campaign owns source-prefix
verification and binds publication through its already-leased artifact store.

The canonical JSON index streams entries in destination (case_id, repeat)
order. Its SHA-256 is the bundle identity: each entry binds both section hashes.
The payload is HEADER + request/stdout pairs + FOOTER, without padding. Exact
index bytes, commas and payload framing all count against the bundle cap. The
separate canonical gap declaration is bounded independently.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Final

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.baseline_import import PROJECTION_SECTIONS, record_digest
from evolution.tree.execution.interaction import CandidateView, EvalCase, ScoringError
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.execution.rollout_scoring import (  # noqa: PLC2701
    _parse_score_result,
    parse_scorer_stdout,
)
from evolution.tree.execution.scorer_traces import ScorerTraceReader, ScorerTraceUnreadable
from evolution.tree.roles.editor.editor_evidence import _bound_view  # noqa: PLC2701
from evolution.tree.storage.append_log import _file
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalFault

if TYPE_CHECKING:
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.artifacts.artifacts import ArtifactStore

FORMAT: Final = "baseline-diagnostic-bundle/1"
MAX_SECTION_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_GAP_BYTES: Final = 64 * 1024
MAX_GAP_SAMPLES: Final = 64
GAP_REASONS: Final = (
    "missing",
    "unsafe_entry",
    "corrupt",
    "ambiguous",
    "invalid_id",
    "too_large",
    "source_mismatch",
    "projection_mismatch",
    "request_mismatch",
    "stdout_invalid",
    "scalar_mismatch",
    "bundle_budget",
)
_PAYLOAD_HEADER: Final = b"baseline-diagnostic-bundle/1\n"
_PAYLOAD_FOOTER: Final = b"\nend\n"
_INDEX_HEADER: Final = b'{"entries":['
_INDEX_FOOTER: Final = b'],"format":"baseline-diagnostic-bundle/1"}'
_CHUNK_BYTES: Final = 64 * 1024


class DiagnosticCorruptionError(ArtifactCorruptionError):
    """Committed local diagnostic authority is missing, unsafe or changed."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class DiagnosticSlot:
    """One independently admitted row and its original, frozen source evidence.

    `admitted` is a sample_imported payload from BaselineImport; it is copied
    and deeply frozen. `source_prefix_digest` belongs to the original source
    measurement. The caller has verified that prefix and the source identity;
    preparation cross-checks the row's execution hashes and authentication.
    `trace_run` is a relative location beneath source_root, never provenance.
    Thus transitive rows keep their original source even when storage moves.

    On a later link, the canonical envelopes in the committed bundle supply
    these same source records, without the original journals. Committed-bundle
    integrity verification and section resolution belong to its owner.
    """

    admitted: Mapping[str, Any]
    source_prefix_digest: str
    rollout: Envelope
    started: Envelope
    finished: Envelope
    case: EvalCase
    candidate: CandidateView
    source_projection: Mapping[str, Any]
    target_projection: Mapping[str, Any]
    trace_run: str = "."

    def __post_init__(self) -> None:
        for name in ("admitted", "source_projection", "target_projection"):
            value = json.loads(canonical_json(getattr(self, name)))
            object.__setattr__(self, name, _freeze(value))
        row = self.admitted
        if (
            not isinstance(row.get("case_id"), str)
            or not row["case_id"]
            or type(row.get("repeat")) is not int
            or row["repeat"] < 1
            or not isinstance(row.get("sample_id"), str)
            or not row["sample_id"]
            or not isinstance(row.get("source"), Mapping)
            or not isinstance(row.get("metrics"), Mapping)
        ):
            raise ValueError("invalid diagnostic destination or scalar row")
        if len(self.source_prefix_digest) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_prefix_digest
        ):
            raise ValueError("invalid diagnostic source prefix digest")

    @property
    def key(self) -> tuple[str, int]:
        return str(self.admitted["case_id"]), int(self.admitted["repeat"])


@dataclass(frozen=True)
class UnavailableDiagnosticSlot:
    """An admitted scalar whose original diagnostic descriptors are unavailable."""

    admitted: Mapping[str, Any]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "admitted", _freeze(json.loads(canonical_json(self.admitted))))
        if (
            self.reason not in GAP_REASONS
            or not isinstance(self.admitted.get("case_id"), str)
            or not self.admitted["case_id"]
            or type(self.admitted.get("repeat")) is not int
            or self.admitted["repeat"] < 1
            or not isinstance(self.admitted.get("sample_id"), str)
            or not self.admitted["sample_id"]
        ):
            raise ValueError("invalid unavailable diagnostic slot")

    @property
    def key(self) -> tuple[str, int]:
        return str(self.admitted["case_id"]), int(self.admitted["repeat"])


@dataclass(frozen=True)
class DiagnosticGaps:
    """Bounded immutable declaration; an index absence always means unavailable."""

    scheduled: int
    included: int
    counts: Mapping[str, int]
    samples: tuple[Mapping[str, Any], ...]
    encoded: bytes
    digest: str


@dataclass(frozen=True)
class PreparedDiagnosticBundle:
    """Private files owned by the caller; not yet published or ledger-bound.

    `byte_count` includes index and payload, excluding the separately capped
    gaps.json. `digest` hashes the canonical index, including section hashes.
    """

    directory: Path
    index_path: Path
    payload_path: Path
    gaps_path: Path
    digest: str
    byte_count: int
    included: int
    gaps: DiagnosticGaps


class _Gap(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _source(slot: DiagnosticSlot) -> tuple[str, str]:
    row, source = slot.admitted, slot.admitted["source"]
    execution, started, finished = slot.rollout.payload, slot.started.payload, slot.finished.payload
    request_id = started.get("score_request_id")
    if (
        slot.rollout.event != "rollout_finished"
        or slot.started.event != "scoring_started"
        or slot.finished.event != "scoring_finished"
        or record_digest(slot.rollout) != source.get("rollout_digest")
        or record_digest(slot.finished) != source.get("scoring_digest")
        or slot.started.to_payload()["run_id"] != slot.finished.to_payload()["run_id"]
        or slot.rollout.to_payload()["run_id"] != slot.finished.to_payload()["run_id"]
        or slot.started.operation_id != slot.finished.operation_id
        or not isinstance(request_id, str)
        or request_id != finished.get("score_request_id")
        or source.get("candidate_digest") != slot.candidate.digest
        or row["case_id"] != slot.case.case_id
        or any(
            payload.get("case_id") != row["case_id"]
            or payload.get("sample_id") != execution.get("sample_id")
            or payload.get("candidate_digest") != source.get("candidate_digest")
            or payload.get("retry_ordinal", 0) != source.get("retry_ordinal")
            for payload in (execution, started, finished)
        )
        or not isinstance(source.get("measurement_id"), str)
        or not source.get("measurement_id")
    ):
        raise _Gap("source_mismatch")
    if finished.get("metrics") != row["metrics"] or finished.get("critical_pass") != row.get(
        "critical_pass"
    ):
        raise _Gap("scalar_mismatch")
    if (
        any(
            section not in slot.source_projection
            or section not in slot.target_projection
            or slot.source_projection[section] != slot.target_projection[section]
            for section in PROJECTION_SECTIONS
        )
        or started.get("scorer") != slot.source_projection["scorer"]
    ):
        raise _Gap("projection_mismatch")
    return request_id, source["measurement_id"]


def _trace_root(source_root: Path, relative: str) -> Path:
    if relative == ".":
        return source_root
    if (
        not isinstance(relative, str)
        or not relative
        or PurePosixPath(relative).is_absolute()
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or "\\" in relative
    ):
        raise _Gap("unsafe_entry")
    return source_root / relative


class _TraceReaders:
    """Keep one pinned measurement reader and share its root's diagnostic index."""

    def __init__(self, source_root: Path, index: DiagnosticIndex | None = None) -> None:
        self.source_root = source_root
        self.index = index
        self.reader: ScorerTraceReader | None = None
        self.key: tuple[Path, str] | None = None

    def get(self, slot: DiagnosticSlot, measurement_id: str) -> ScorerTraceReader:
        root = _trace_root(self.source_root, slot.trace_run)
        key = (root, measurement_id)
        if self.reader is None or self.key != key:
            self.close()
            if self.index is None or self.index.root != root:
                self.index = DiagnosticIndex(root)
            self.reader = DiagnosticTraceReader(
                root, run_boundary="nofollow", _diagnostic_index=self.index
            )
            self.key = key
        return self.reader

    def close(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
            self.key = None


def _sections(slot: DiagnosticSlot, readers: _TraceReaders) -> tuple[bytes, bytes]:
    request_id, measurement_id = _source(slot)
    try:
        reader = readers.get(slot, measurement_id)
        trace = reader.read(
            request_id,
            measurement_id=measurement_id,
            sections=("request",),
            max_section_bytes=MAX_SECTION_BYTES,
        )
        if trace is None or trace.request is None:
            raise _Gap("missing")
        request = trace.request
        scorer = slot.source_projection["scorer"]
        if (
            _bound_view(request, slot.started, request_id, slot.case, slot.candidate, scorer)
            is None
        ):
            raise _Gap("request_mismatch")
        # The existing view authenticator hashes the trusted corpus case;
        # also require the private copied request's case data to match it.
        try:
            document = json.loads(request)
            case_matches = canonical_json(document["input"]["public"]) == canonical_json(
                slot.case.public
            ) and canonical_json(document["input"]["oracle"]) == canonical_json(slot.case.oracle)
        except (ValueError, TypeError, KeyError, RecursionError):
            # External JSON may parse but still fail canonical authentication
            # (for example NaN). Only this in-memory request check is guarded.
            raise _Gap("request_mismatch") from None
        if not case_matches:
            raise _Gap("request_mismatch")
        del document, trace
        trace = reader.read(
            request_id,
            measurement_id=measurement_id,
            sections=("stdout",),
            max_section_bytes=MAX_SECTION_BYTES,
        )
        if trace is None or not trace.stdout or "stdout" not in trace.sizes:
            raise _Gap("missing")
        stdout = trace.stdout
    except ScorerTraceUnreadable as exc:
        raise _Gap(exc.code) from None
    except FileNotFoundError:
        raise _Gap("missing") from None
    except OSError:
        raise _Gap("corrupt") from None
    try:
        result = _parse_score_result(parse_scorer_stdout(stdout.decode("utf-8")), diagnostics=True)
    except (ValueError, TypeError, RecursionError, ScoringError):
        raise _Gap("stdout_invalid") from None
    if result.metrics != slot.admitted["metrics"] or result.critical_pass != slot.admitted.get(
        "critical_pass"
    ):
        raise _Gap("scalar_mismatch")
    return request, stdout


def _hash(raw: bytes) -> str:
    digest = hashlib.sha256()
    view = memoryview(raw)
    for offset in range(0, len(raw), _CHUNK_BYTES):
        digest.update(view[offset : offset + _CHUNK_BYTES])
    return digest.hexdigest()


def _write(stream: BinaryIO, raw: bytes) -> None:
    view = memoryview(raw)
    for offset in range(0, len(raw), _CHUNK_BYTES):
        stream.write(view[offset : offset + _CHUNK_BYTES])


def _entry(slot: DiagnosticSlot, request: bytes, stdout: bytes, offset: int) -> bytes:
    sections = {}
    for name, raw in (("request", request), ("stdout", stdout)):
        sections[name] = dict(offset=offset, length=len(raw), sha256=_hash(raw))
        offset += len(raw)
    return canonical_json(
        dict(
            destination={key: slot.admitted[key] for key in ("case_id", "repeat", "sample_id")},
            source=slot.admitted["source"],
            source_prefix_digest=slot.source_prefix_digest,
            records={
                "rollout_finished": slot.rollout.to_payload(),
                "scoring_started": slot.started.to_payload(),
                "scoring_finished": slot.finished.to_payload(),
            },
            projection=slot.source_projection,
            scalar={
                "metrics": slot.admitted["metrics"],
                "critical_pass": slot.admitted.get("critical_pass"),
            },
            sections=sections,
            validation=dict(
                score_provenance="journal_bound",
                request="authenticated",
                stdout="captured_at_import",
                scalar="matched",
                projection="matched",
            ),
        )
    )


def _gap_sample(slot: DiagnosticSlot | UnavailableDiagnosticSlot, reason: str) -> Mapping[str, Any]:
    reference = {key: slot.admitted[key] for key in ("case_id", "repeat", "sample_id")}
    sample = dict(slot=reference, reason=reason)
    if len(canonical_json(sample)) > 512:
        sample = dict(
            slot_sha256=hashlib.sha256(canonical_json(reference)).hexdigest(), reason=reason
        )
    return _freeze(sample)


def _private_file(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def prepare_diagnostics(
    slots: Sequence[DiagnosticSlot | UnavailableDiagnosticSlot],
    *,
    source_root: Path,
    destination: Path,
    _diagnostic_index: DiagnosticIndex | None = None,
) -> PreparedDiagnosticBundle:
    """Prepare a new private directory; never replace an existing destination.

    Reader buffers contain at most one slot's two <=8 MiB sections, plus bounded
    JSON parsing overhead. Index and payload bytes are streamed to disk; only
    slot references are sorted, not trace bodies or corpus-sized entry metadata.
    Source-quality gaps are returned; destination storage failures propagate.
    """

    ordered = sorted(slots, key=lambda slot: slot.key)
    if any(left.key == right.key for left, right in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("duplicate diagnostic destination slot")
    reserved = len(_INDEX_HEADER) + len(_INDEX_FOOTER) + len(_PAYLOAD_HEADER) + len(_PAYLOAD_FOOTER)
    if MAX_BUNDLE_BYTES < reserved:
        raise ValueError("diagnostic budget cannot hold format framing")
    destination.mkdir(mode=0o700)
    index_path, payload_path, gaps_path = (
        destination / name for name in ("index.json", "payload", "gaps.json")
    )
    counts = dict.fromkeys(GAP_REASONS, 0)
    samples: list[Mapping[str, Any]] = []
    included, byte_count = 0, reserved
    index_hash = hashlib.sha256()
    with (
        closing(_TraceReaders(source_root, _diagnostic_index)) as readers,
        _private_file(index_path) as index_stream,
        _private_file(payload_path) as payload_stream,
    ):
        index_stream.write(_INDEX_HEADER)
        index_hash.update(_INDEX_HEADER)
        payload_stream.write(_PAYLOAD_HEADER)
        for slot in ordered:
            request = stdout = entry = b""
            try:
                if isinstance(slot, UnavailableDiagnosticSlot):
                    raise _Gap(slot.reason)
                request, stdout = _sections(slot, readers)
                entry = _entry(slot, request, stdout, payload_stream.tell())
                framing = b"," if included else b""
                cost = len(framing) + len(entry) + len(request) + len(stdout)
                if cost > MAX_BUNDLE_BYTES - byte_count:
                    raise _Gap("bundle_budget")
                index_stream.write(framing)
                _write(index_stream, entry)
                index_hash.update(framing)
                index_hash.update(entry)
                _write(payload_stream, request)
                _write(payload_stream, stdout)
                byte_count += cost
                included += 1
            except _Gap as exc:
                counts[exc.reason] += 1
                if len(samples) < MAX_GAP_SAMPLES:
                    samples.append(_gap_sample(slot, exc.reason))
            finally:
                # Release rejected bodies too, before reading the next slot.
                del request, stdout, entry
        index_stream.write(_INDEX_FOOTER)
        index_hash.update(_INDEX_FOOTER)
        payload_stream.write(_PAYLOAD_FOOTER)
    encoded = canonical_json(
        dict(
            format=FORMAT, scheduled=len(ordered), included=included, counts=counts, samples=samples
        )
    )
    if len(encoded) > MAX_GAP_BYTES:
        raise ValueError("diagnostic gap declaration exceeds format bound")
    with _private_file(gaps_path) as gaps_stream:
        gaps_stream.write(encoded)
    gaps = DiagnosticGaps(
        len(ordered), included, MappingProxyType(counts), tuple(samples), encoded, _hash(encoded)
    )
    return PreparedDiagnosticBundle(
        destination,
        index_path,
        payload_path,
        gaps_path,
        index_hash.hexdigest(),
        byte_count,
        included,
        gaps,
    )


@dataclass(frozen=True)
class DiagnosticRecord:
    """Authenticated local metadata and bounded original trace sections."""

    entry: Mapping[str, Any]
    request: bytes
    stdout: bytes

    def envelope(self, event: str) -> Envelope:
        return Envelope.from_payload(
            json.loads(canonical_json(self.entry["records"][event])), family=S1_FAMILY
        )


def commit_diagnostics(
    prepared: PreparedDiagnosticBundle,
    *,
    run_root: Path,
    declaration: Mapping[str, Any],
    store: ArtifactStore,
) -> AuxiliaryPublication:
    """Publish exact declared bytes through the caller's already-leased S2 owner."""
    from evolution.tree.artifacts.artifact_ledger import inspect_ledger
    from evolution.tree.artifacts.artifact_payloads import AuxiliaryDeclaration

    bound = AuxiliaryDeclaration.from_payload(dict(declaration))
    if (
        bound.purpose != "baseline_diagnostics"
        or bound.content_digest != prepared.digest
        or inspect_ledger(run_root).records[0].to_payload()["run_id"] != store.run_id
    ):
        raise DiagnosticCorruptionError("artifact_digest_mismatch")
    return store.publish_auxiliary(bound, prepared.directory)


def _entries(directory: Path):
    """Stream one canonical index entry at a time through pinned no-follow files."""
    from evolution.tree.artifacts.artifact_payloads import _open_directory

    decoder = json.JSONDecoder()
    with _open_directory(directory) as fd:
        handle = _file(fd, "index.json", os.O_RDONLY)
        with os.fdopen(handle, "rb") as stream:
            if stream.read(len(_INDEX_HEADER)) != _INDEX_HEADER:
                raise DiagnosticCorruptionError("artifact_digest_mismatch")
            buffer = b""
            first = True
            while True:
                while len(buffer) < len(_INDEX_FOOTER):
                    chunk = stream.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    buffer += chunk
                if buffer.startswith(_INDEX_FOOTER):
                    if buffer != _INDEX_FOOTER or stream.read(1):
                        raise DiagnosticCorruptionError("artifact_digest_mismatch")
                    return
                if not first:
                    if not buffer.startswith(b","):
                        raise DiagnosticCorruptionError("artifact_digest_mismatch")
                    buffer = buffer[1:]
                while True:
                    try:
                        text = buffer.decode("utf-8")
                        entry, end = decoder.raw_decode(text)
                        raw = text[:end].encode("utf-8")
                        break
                    except (ValueError, UnicodeDecodeError):
                        chunk = stream.read(_CHUNK_BYTES)
                        if not chunk or len(buffer) + len(chunk) > MAX_BUNDLE_BYTES:
                            raise DiagnosticCorruptionError("artifact_digest_mismatch") from None
                        buffer += chunk
                if not isinstance(entry, dict) or canonical_json(entry) != raw:
                    raise DiagnosticCorruptionError("artifact_digest_mismatch")
                buffer = buffer[len(raw) :]
                first = False
                yield entry


def _publications(run_root: Path):
    # Earlier families and standalone trace roots retain their existing behavior.
    from evolution.tree.artifacts.artifact_payloads import _open_directory
    from evolution.tree.artifacts.artifact_projections import inspect_auxiliary

    if not os.path.lexists(run_root / "campaign.jsonl"):
        return ()
    with _open_directory(run_root) as directory:
        fd = _file(directory, "campaign.jsonl", os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            first = stream.readline(8 * 1024 * 1024)
    if json.loads(first).get("payload", {}).get("family_version") != 15:
        return ()
    if not os.path.lexists(run_root / "artifacts.jsonl"):
        return ()
    return tuple(
        p for p in inspect_auxiliary(run_root) if p.declaration.purpose == "baseline_diagnostics"
    )


def _committed(run_root: Path, bundle_digest: str):
    from evolution.tree.family15_audit import authenticate_auxiliary

    found = [p for p in _publications(run_root) if p.declaration.content_digest == bundle_digest]
    if len(found) != 1:
        raise DiagnosticCorruptionError("artifact_missing")
    try:
        authenticate_auxiliary(run_root, found[0])
    except (OSError, ValueError, ArtifactCorruptionError) as exc:
        raise DiagnosticCorruptionError("artifact_digest_mismatch") from exc
    return found[0]


@contextmanager
def _local_bytes():
    from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError

    try:
        yield
    except (OSError, ValueError, KeyError, ArtifactUnsafeError, JournalFault) as exc:
        raise DiagnosticCorruptionError("artifact_digest_mismatch") from exc


def _read_entry(
    directory: Path,
    entry: Mapping[str, Any],
    *,
    requested: Sequence[str] = ("request", "stdout"),
    max_section_bytes: int | None = None,
) -> DiagnosticRecord:
    from evolution.tree.artifacts.artifact_payloads import _open_directory

    sections = {}
    with _local_bytes(), _open_directory(directory) as fd:
        handle = _file(fd, "payload", os.O_RDONLY)
        with os.fdopen(handle, "rb") as stream:
            for name in ("request", "stdout"):
                if name not in requested:
                    continue
                part = entry["sections"][name]
                offset, length = part["offset"], part["length"]
                if (
                    type(offset) is not int
                    or type(length) is not int
                    or offset < len(_PAYLOAD_HEADER)
                    or not 0 <= length <= MAX_SECTION_BYTES
                ):
                    raise DiagnosticCorruptionError("artifact_digest_mismatch")
                if max_section_bytes is not None and length > max_section_bytes:
                    raise ScorerTraceUnreadable("too_large")
                stream.seek(offset)
                raw = stream.read(length)
                if len(raw) != length or _hash(raw) != part["sha256"]:
                    raise DiagnosticCorruptionError("artifact_digest_mismatch")
                sections[name] = raw
    return DiagnosticRecord(
        _freeze(entry), sections.get("request", b""), sections.get("stdout", b"")
    )


def resolve_diagnostic_slot(
    run_root: Path, *, bundle_digest: str, case_id: str, repeat: int
) -> DiagnosticRecord | None:
    """Absent sparse slots are unavailable; damaged committed artifacts are faults."""
    publication = _committed(Path(run_root), bundle_digest)
    found = None
    for entry in _entries(Path(run_root) / publication.locator):
        if entry["destination"]["case_id"] == case_id and entry["destination"]["repeat"] == repeat:
            if found is not None:
                raise DiagnosticCorruptionError("artifact_digest_mismatch")
            found = _read_entry(Path(run_root) / publication.locator, entry)
    return found


@dataclass(frozen=True)
class _EntryLocation:
    directory: Path
    locator: str
    offset: int
    length: int
    digest: str

    def read(self) -> Mapping[str, Any]:
        from evolution.tree.artifacts.artifact_payloads import _open_directory

        with _local_bytes(), _open_directory(self.directory) as directory:
            fd = _file(directory, "index.json", os.O_RDONLY)
            try:
                raw = os.pread(fd, self.length, self.offset)
            finally:
                os.close(fd)
        if len(raw) != self.length or _hash(raw) != self.digest:
            raise DiagnosticCorruptionError("artifact_digest_mismatch")
        return json.loads(raw)


def _origin_key(source: Mapping[str, Any], case_id: str, repeat: int) -> str:
    return _hash(
        canonical_json(
            dict(
                source={k: v for k, v in source.items() if k != "condition_fields"},
                case_id=case_id,
                repeat=repeat,
            )
        )
    )


class DiagnosticIndex:
    """Invocation-local fixed-size lookup metadata; never retain trace bodies."""

    def __init__(self, root: Path) -> None:

        self.root = root
        self.requests: dict[tuple[str, str], _EntryLocation] = {}
        self.origins: dict[str, _EntryLocation] = {}
        for publication in _publications(root):
            _committed(root, publication.declaration.content_digest)
            offset = len(_INDEX_HEADER)
            directory = root / publication.locator
            for i, entry in enumerate(_entries(directory)):
                if i:
                    offset += 1
                raw = canonical_json(entry)
                location = _EntryLocation(
                    directory, publication.locator, offset, len(raw), _hash(raw)
                )
                offset += len(raw)
                started = entry["records"]["scoring_started"]
                request = (started["run_id"], started["payload"]["score_request_id"])
                # Multiple bundles cannot disagree about the same local execution.
                previous = self.requests.get(request)
                if previous is not None:
                    old = previous.read()
                    if old["sections"] != entry["sections"] or old["source"] != entry["source"]:
                        raise DiagnosticCorruptionError("artifact_digest_mismatch")
                self.requests[request] = location
                slot = entry["destination"]
                self.origins[_origin_key(entry["source"], slot["case_id"], slot["repeat"])] = (
                    location
                )

    def source(
        self, source: Mapping[str, Any], case_id: str, repeat: int
    ) -> DiagnosticRecord | None:
        location = self.origins.get(_origin_key(source, case_id, repeat))
        return None if location is None else DiagnosticRecord(_freeze(location.read()), b"", b"")


class DiagnosticTraceReader(ScorerTraceReader):
    """Internal local-bundle routing; the public trace reader stays unchanged."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        _diagnostic_index: DiagnosticIndex | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(run_dir, **kwargs)
        self._diagnostic_index = (
            DiagnosticIndex(Path(run_dir)) if _diagnostic_index is None else _diagnostic_index
        )

    def read(
        self,
        score_request_id: str,
        *,
        measurement_id: str | None = None,
        operation_id: str | None = None,
        sections: Any = ("request", "stdout", "stderr"),
        max_section_bytes: int | None = None,
    ):
        from evolution.tree.execution.scorer_traces import ScorerTraceRecord

        location = (
            None
            if measurement_id is None
            else self._diagnostic_index.requests.get((measurement_id, score_request_id))
        )
        if location is not None:
            entry = location.read()
            started = entry["records"]["scoring_started"]
            if operation_id is not None and started["operation_id"] != operation_id:
                return None
            record = _read_entry(
                location.directory,
                entry,
                requested=tuple(sections),
                max_section_bytes=max_section_bytes,
            )
            # The established result's layout vocabulary describes storage only;
            # the explicit auxiliary locator retains actual provenance.
            return ScorerTraceRecord(
                score_request_id,
                "pack",
                location.locator,
                record.request if "request" in sections else None,
                record.stdout if "stdout" in sections else None,
                None,
                None,
                None,
                {name: entry["sections"][name]["length"] for name in ("request", "stdout")},
            )
        return super().read(
            score_request_id,
            measurement_id=measurement_id,
            operation_id=operation_id,
            sections=sections,
            max_section_bytes=max_section_bytes,
        )


def diagnostic_source(
    run_root: Path,
    *,
    source: Mapping[str, Any],
    case_id: str,
    repeat: int,
) -> DiagnosticRecord | None:
    """Single-slot convenience resolver; bulk callers share DiagnosticIndex."""
    return DiagnosticIndex(Path(run_root)).source(source, case_id, repeat)


def prepare_import_diagnostics(
    source: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    target: Any,
    source_root: Path,
    destination: Path,
    corpus: Sequence[EvalCase],
    candidate: CandidateView,
) -> PreparedDiagnosticBundle:
    """Resolve original descriptors from the admitted carrier, never another run path."""
    from evolution.tree.baseline_import import FrozenSourceCorrupt, _SourceTries, read_frozen_source

    cases = {case.case_id: case for case in corpus}
    identity = target.to_payload()
    schedule = {(s["case_id"], s["sample_id"]): s for s in identity["schedule"]}
    target_projection = {k: identity[k] for k in PROJECTION_SECTIONS}
    slots: list[DiagnosticSlot | UnavailableDiagnosticSlot] = []
    local_index = DiagnosticIndex(source_root)
    for row in rows:
        repeat = schedule[row["case_id"], row["sample_id"]]["repeat"]
        admitted = {**row, "repeat": repeat}
        origin = row["source"]
        local = local_index.source(origin, row["case_id"], repeat)
        if local is not None:
            entry = local.entry
            slots.append(
                DiagnosticSlot(
                    admitted,
                    entry["source_prefix_digest"],
                    local.envelope("rollout_finished"),
                    local.envelope("scoring_started"),
                    local.envelope("scoring_finished"),
                    cases[row["case_id"]],
                    candidate,
                    entry["projection"],
                    target_projection,
                )
            )
            continue
        frozen = None
        if origin["run_id"] == source.run_id:
            if origin["measurement_id"] == source.measurement_id:
                frozen = source
            else:
                try:
                    frozen = read_frozen_source(
                        source_root,
                        source.run_id,
                        origin["measurement_id"],
                        expected_identity_digest=origin["identity_digest"],
                    )
                except FrozenSourceCorrupt:
                    pass
        if frozen is None:
            slots.append(UnavailableDiagnosticSlot(admitted, "missing"))
            continue
        tries = _SourceTries(frozen.records)
        pair = tries.scored(row["case_id"], repeat, origin)
        if pair is None:
            slots.append(UnavailableDiagnosticSlot(admitted, "source_mismatch"))
            continue
        source_identity = frozen.records[0].payload["identity"]
        slots.append(
            DiagnosticSlot(
                admitted,
                frozen.prefix_digest,
                tries.rollouts[origin["rollout_digest"]],
                *pair,
                cases[row["case_id"]],
                candidate,
                {k: source_identity[k] for k in PROJECTION_SECTIONS},
                target_projection,
            )
        )
    return prepare_diagnostics(
        slots,
        source_root=source_root,
        destination=destination,
        _diagnostic_index=local_index,
    )


def retain_diagnostic_binding(run_root: Path, body: Mapping[str, Any]) -> None:
    """Durable preparation bytes, authorized only by the later auxiliary digest."""
    from uuid import uuid4

    from evolution.tree.artifacts.artifact_payloads import _open_directory

    raw = canonical_json(body)
    digest = _hash(raw)
    parent = run_root
    for name in ("diagnostic-imports", ".prepared"):
        with _open_directory(parent) as fd:
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
        parent = parent / name
    with _open_directory(parent) as fd:
        name = digest + ".json"
        if os.path.lexists(parent / name):
            if read_prepared_binding(run_root, digest=digest, byte_count=len(raw)) != dict(body):
                raise DiagnosticCorruptionError("artifact_digest_mismatch")
            return
        temporary = ".pending-" + uuid4().hex
        handle = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                _write(stream, raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Same leased campaign writer and the existing publication convention:
            # check destination, then rename; never replace an existing proof.
            if os.path.lexists(parent / name):
                raise DiagnosticCorruptionError("artifact_digest_mismatch")
            os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def read_prepared_binding(run_root: Path, *, digest: str, byte_count: int) -> dict[str, Any]:
    """Read only exact declared regular owner-only preparation, without trusting its path."""
    import stat

    from evolution.tree.artifacts.artifact_payloads import _open_directory

    with _open_directory(run_root / "diagnostic-imports/.prepared") as directory:
        fd = _file(directory, digest + ".json", os.O_RDONLY)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size != byte_count
            ):
                raise DiagnosticCorruptionError("artifact_digest_mismatch")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(byte_count + 1)
        finally:
            os.close(fd)
    if len(raw) != byte_count or _hash(raw) != digest:
        raise DiagnosticCorruptionError("artifact_digest_mismatch")
    body = json.loads(raw)
    if canonical_json(body) != raw:
        raise DiagnosticCorruptionError("artifact_digest_mismatch")
    return body
