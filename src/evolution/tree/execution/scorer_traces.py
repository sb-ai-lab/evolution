"""Scorer traces: one reader for both layouts, and the directory writer's storage rules.

A *trace* is what the scorer subprocess wrote for one scored try: its request
document, its stdout and its stderr (ADR-0040, PDR-0032). Runs written before
the packed writer keep each try in the *directory layout*,
`<trace root>/scorer_traces/<score_request_id>/{request.json,stdout,stderr}`.
The packed writer (a later release) appends every try of one measurement
journal to `<trace root>/<pack_id>.pack` in the format `scorer_trace_pack/1`.
`read_scorer_trace` and `ScorerTraceReader` read both, under either trace
root: `scorer-traces/` for campaigns and evaluations, and
`connection-logs/<operation_id>/` for a family-14 connection preflight.

Reading never repairs anything. A pack is read up to the size taken when it
was opened: a record torn at that size is one its writer is still appending,
and it is ignored.

The directory writer's side lives in `rollout_scoring.SubprocessScorer`; this
module gives it the storage-fault type (ADR-0040 D7) and the stream files
whose every storage failure is that fault.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, cast, get_args

from evolution.tree.storage.append_log import _directory, _file, _read
from evolution.tree.storage.journal_errors import JournalCode, JournalFault

__all__ = [
    "PACK_FORMAT",
    "PACK_ID",
    "TRACE_ID",
    "TRACE_ROOT",
    "TRACE_SUBDIR",
    "ScorerTraceReader",
    "ScorerTraceRecord",
    "ScorerTraceStorageFault",
    "ScorerTraceUnreadable",
    "TraceStream",
    "decode_trace_text",
    "drop_empty_streams",
    "encode_pack_record",
    "open_trace_stream",
    "read_scorer_trace",
    "settle_trace_streams",
    "trace_location",
    "trace_storage_fault",
]

#: The campaign's and the evaluation's trace root (`bindings.build_scorer`).
TRACE_ROOT: Final = "scorer-traces"
#: The directory layout's subdirectory under a trace root.
TRACE_SUBDIR: Final = "scorer_traces"
#: The connection preflight's trace roots live under this (`connection_probe`).
CONNECTION_LOGS: Final = "connection-logs"
PACK_FORMAT: Final = "scorer_trace_pack/1"
#: A header line, its `\n` included, is at most this many bytes (ADR-0040).
MAX_HEADER_BYTES: Final = 4096
#: One path segment, never hidden, never a traversal: the try's id.
TRACE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: A pack id: the S1 journal's `run_id` as a safe file name (ADR-0040, L6).
PACK_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")

Section = Literal["request", "stdout", "stderr"]
SECTIONS: Final[tuple[Section, ...]] = ("request", "stdout", "stderr")
TraceStatus = Literal["completed", "failed", "timeout", "start_failed"]
UnreadableCode = Literal["unsafe_entry", "corrupt", "ambiguous", "invalid_id", "too_large"]

_STATUSES: Final = frozenset(get_args(TraceStatus))
_CODES: Final = frozenset(get_args(UnreadableCode))
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_SEGMENT: Final = re.compile(r"(?P<pack_id>.+)\.s(?P<segment>[1-9][0-9]{0,8})")
_DIRECTORY_FILES: Final[dict[Section, str]] = {
    "request": "request.json",
    "stdout": "stdout",
    "stderr": "stderr",
}
_KEYS: Final = {
    "pack": frozenset({"format", "kind", "pack_id", "segment"}),
    "request": frozenset({"format", "kind", "score_request_id", "sections"}),
    "output": frozenset({"format", "kind", "score_request_id", "status", "returncode", "sections"}),
}
_SECTION_NAMES: Final = {"request": ("request",), "output": ("stdout", "stderr")}


# -- public surface (PDR-0026 amendment of 2026-10-16) -------------------------


class ScorerTraceUnreadable(Exception):
    """A try's trace exists but cannot be read safely; `code` is closed."""

    def __init__(self, code: UnreadableCode) -> None:
        if code not in _CODES:
            raise ValueError("invalid scorer trace code")
        super().__init__(code)
        self.code: UnreadableCode = code


@dataclass(frozen=True)
class ScorerTraceRecord:
    """One try's trace, from either layout.

    A stream is `b""` when the trace holds an empty stream and `None` only when
    the trace holds no output for the try, or when it was not asked for. In
    the directory layout a missing stream file next to `request.json` reads as
    `b""`; without `request.json` all three are `None`. `status` and
    `returncode` are always `None` in the directory layout. `sizes` holds the
    byte length of every section present for the try, read or not.
    """

    score_request_id: str
    layout: Literal["directory", "pack"]
    location: str
    request: bytes | None
    stdout: bytes | None
    stderr: bytes | None
    status: TraceStatus | None
    returncode: int | None
    sizes: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), hash=False)

    def request_document(self) -> Any:
        """`json.loads` of `request`; `ValueError` when absent or unparseable."""

        if self.request is None:
            raise ValueError("the trace holds no request document")
        return json.loads(self.request.decode("utf-8"))


def read_scorer_trace(
    run_dir: str | os.PathLike[str],
    score_request_id: str,
    *,
    measurement_id: str | None = None,
    operation_id: str | None = None,
    sections: Collection[str] = SECTIONS,
    max_section_bytes: int | None = None,
    run_boundary: Literal["resolved", "nofollow"] = "resolved",
) -> ScorerTraceRecord | None:
    """One try's trace; optionally cap each selected section's bytes inclusively.

    `None` keeps unlimited reads. Construct the reader before validating read
    arguments, so constructor errors may precede an invalid limit.
    """

    with ScorerTraceReader(run_dir, run_boundary=run_boundary) as reader:
        return reader.read(
            score_request_id,
            measurement_id=measurement_id,
            operation_id=operation_id,
            sections=sections,
            max_section_bytes=max_section_bytes,
        )


class ScorerTraceReader:
    """Many reads over one run directory; caches each pack's header index.

    The index of a pack is built once against its first-opened size. Later
    appends and new packs require a new reader (or resolved-mode `close()`).
    `nofollow` pins the run descriptor until terminal close; `resolved`
    retains reusable close and holds no descriptor between calls.
    """

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        *,
        run_boundary: Literal["resolved", "nofollow"] = "resolved",
    ) -> None:
        if run_boundary not in ("resolved", "nofollow"):
            raise ValueError("invalid scorer trace run boundary")
        self._strict = run_boundary == "nofollow"
        # Keep lifecycle serialization separate from the pack-cache lock.
        self._lifecycle_lock = threading.Lock()
        self._lock = threading.Lock()
        self._root_fd: int | None = None
        self._closed = False
        self._segments: dict[tuple[tuple[str, ...], str], tuple[_PackIndex, ...]] = {}
        self._every_pack: dict[tuple[str, ...], tuple[_PackIndex, ...]] = {}
        if self._strict:
            self._root_fd = _nofollow_root(run_dir)
        else:
            # Resolved mode permits caller-root ancestry aliases, as in b5.
            self._root = Path(os.path.realpath(os.fspath(run_dir)))

    def __enter__(self) -> ScorerTraceReader:
        if self._strict:
            with self._lifecycle_lock:
                self._ensure_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Construction may have failed, or interpreter teardown started.
            pass

    def close(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._segments.clear()
                self._every_pack.clear()
            if self._strict:
                self._closed = True
                fd, self._root_fd = self._root_fd, None
                if fd is not None:
                    os.close(fd)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("scorer trace reader is closed")

    def read(
        self,
        score_request_id: str,
        *,
        measurement_id: str | None = None,
        operation_id: str | None = None,
        sections: Collection[str] = SECTIONS,
        max_section_bytes: int | None = None,
    ) -> ScorerTraceRecord | None:
        """Read selected bytes, optionally bounded by `max_section_bytes` each.

        Finite limits reject oversize with `too_large` and observed directory
        content changes with `corrupt`; `None` retains opened-size reads.
        """

        if self._strict:
            with self._lifecycle_lock:
                self._ensure_open()
                return self._read(
                    score_request_id, measurement_id, operation_id, sections, max_section_bytes
                )
        return self._read(
            score_request_id, measurement_id, operation_id, sections, max_section_bytes
        )

    def _read(
        self,
        score_request_id: str,
        measurement_id: str | None,
        operation_id: str | None,
        sections: Collection[str],
        max_section_bytes: int | None,
    ) -> ScorerTraceRecord | None:
        _validate_limit(max_section_bytes)
        wanted = _wanted(sections)
        if measurement_id is not None and operation_id is not None:
            raise ValueError("measurement_id and operation_id are mutually exclusive")
        if not isinstance(score_request_id, str) or TRACE_ID.fullmatch(score_request_id) is None:
            raise ScorerTraceUnreadable("invalid_id")
        pack_ids: tuple[str, ...] | None
        if operation_id is not None:
            if not isinstance(operation_id, str) or PACK_ID.fullmatch(operation_id) is None:
                raise ScorerTraceUnreadable("invalid_id")
            root: tuple[str, ...] = (CONNECTION_LOGS, operation_id)
            pack_ids = (operation_id,)
        else:
            root = (TRACE_ROOT,)
            if measurement_id is None:
                pack_ids = None
            elif not isinstance(measurement_id, str):
                raise ScorerTraceUnreadable("invalid_id")
            else:
                # An id outside the pack-id grammar names no pack; its
                # directory-layout traces stay readable.
                pack_ids = (measurement_id,) if PACK_ID.fullmatch(measurement_id) else ()
        packs = self._every(root) if pack_ids is None else self._of(root, pack_ids)
        hits = [
            (index, index.entries[score_request_id])
            for index in packs
            if score_request_id in index.entries
        ]
        if max_section_bytes is not None:
            return self._bounded_record(
                root, packs, hits, score_request_id, wanted, max_section_bytes
            )
        directory = self._directory_record(root, score_request_id, wanted)
        if len(hits) > 1 or (hits and (hits[0][1].ambiguous or directory is not None)):
            raise ScorerTraceUnreadable("ambiguous")
        if hits:
            index, entry = hits[0]
            return self._pack_record(root, index, entry, score_request_id, wanted)
        if directory is not None:
            return directory
        if any(index.corrupt_at is not None for index in packs):
            # The id may lie beyond a corrupt point (ADR-0040, D5).
            raise ScorerTraceUnreadable("corrupt")
        return None

    def _bounded_record(
        self,
        root: tuple[str, ...],
        packs: tuple[_PackIndex, ...],
        hits: list[tuple[_PackIndex, _Entry]],
        score_request_id: str,
        wanted: frozenset[str],
        limit: int,
    ) -> ScorerTraceRecord | None:
        admitted = self._admit_directory(root, score_request_id, wanted)
        try:
            if len(hits) > 1 or (hits and (hits[0][1].ambiguous or admitted is not None)):
                raise ScorerTraceUnreadable("ambiguous")
            if hits:
                index, entry = hits[0]
                return self._pack_record(root, index, entry, score_request_id, wanted, limit)
            if admitted is not None:
                if admitted.entries["request"].present:
                    # Admission of every selected entry precedes any size refusal.
                    if any(info.st_size > limit for _fd, info in admitted.files.values()):
                        raise ScorerTraceUnreadable("too_large")
                    for section, (fd, info) in admitted.files.items():
                        data = _read_bounded(fd, info, limit)
                        admitted.entries[section] = _Probed(True, len(data), data)
                return _directory_result(root, score_request_id, admitted.entries, wanted)
            if any(index.corrupt_at is not None for index in packs):
                raise ScorerTraceUnreadable("corrupt")
            return None
        finally:
            if admitted is not None:
                admitted.close()

    # -- packs ------------------------------------------------------------

    def _of(self, root: tuple[str, ...], pack_ids: Sequence[str]) -> tuple[_PackIndex, ...]:
        found: list[_PackIndex] = []
        for pack_id in pack_ids:
            with self._lock:
                cached = self._segments.get((root, pack_id))
                if cached is None:
                    cached = self._segments[(root, pack_id)] = self._walk_segments(root, pack_id)
            found.extend(cached)
        return tuple(found)

    def _walk_segments(self, root: tuple[str, ...], pack_id: str) -> tuple[_PackIndex, ...]:
        directory = self._open_root(root)
        if directory is None:
            return ()
        segments: list[_PackIndex] = []
        try:
            segment = 0
            while True:
                name = f"{pack_id}.pack" if segment == 0 else f"{pack_id}.s{segment}.pack"
                index = _index_file(directory, name)
                if index is None:
                    break
                if index.pack_id is not None and (index.pack_id, index.segment) != (
                    pack_id,
                    segment,
                ):
                    # `m-x.s1.pack` may be segment 0 of the pack id `m-x.s1`:
                    # it is another pack id's file, not this id's segment.
                    break
                segments.append(index)
                segment += 1
        finally:
            os.close(directory)
        return tuple(segments)

    def _every(self, root: tuple[str, ...]) -> tuple[_PackIndex, ...]:
        with self._lock:
            cached = self._every_pack.get(root)
            if cached is not None:
                return cached
            directory = self._open_root(root)
            if directory is None:
                self._every_pack[root] = ()
                return ()
            try:
                try:
                    names = sorted(name for name in os.listdir(directory) if _name_ids(name))
                except NotImplementedError:
                    if self._strict:
                        raise ScorerTraceUnreadable("unsafe_entry") from None
                    raise
                indexes = tuple(
                    index for name in names if (index := _index_file(directory, name)) is not None
                )
            finally:
                os.close(directory)
            self._every_pack[root] = indexes
            return indexes

    def _pack_record(
        self,
        root: tuple[str, ...],
        index: _PackIndex,
        entry: _Entry,
        score_request_id: str,
        wanted: frozenset[str],
        max_section_bytes: int | None = None,
    ) -> ScorerTraceRecord:
        sizes: dict[str, int] = {}
        if entry.request is not None:
            sizes["request"] = entry.request.length
        if entry.output is not None:
            sizes["stdout"] = entry.output.stdout.length
            sizes["stderr"] = entry.output.stderr.length
        reads: dict[str, _Location] = {}
        if "request" in wanted and entry.request is not None:
            reads["request"] = entry.request
        if entry.output is not None:
            if "stdout" in wanted:
                reads["stdout"] = entry.output.stdout
            if "stderr" in wanted:
                reads["stderr"] = entry.output.stderr
        data: dict[str, bytes] = {}
        if reads:
            if max_section_bytes is None:
                data = self._read_sections(root, index, reads)
            else:
                data = self._read_sections(root, index, reads, max_section_bytes)
        return ScorerTraceRecord(
            score_request_id,
            "pack",
            "/".join((*root, index.name)),
            data.get("request"),
            data.get("stdout"),
            data.get("stderr"),
            None if entry.output is None else entry.output.status,
            None if entry.output is None else entry.output.returncode,
            MappingProxyType(sizes),
        )

    def _read_sections(
        self,
        root: tuple[str, ...],
        index: _PackIndex,
        reads: Mapping[str, _Location],
        max_section_bytes: int | None = None,
    ) -> dict[str, bytes]:
        directory = self._open_root(root)
        if directory is None:
            raise ScorerTraceUnreadable("corrupt")
        try:
            try:
                fd = _file(directory, index.name, os.O_RDONLY)
            except FileNotFoundError:
                raise ScorerTraceUnreadable("corrupt") from None
            except (OSError, JournalFault):
                raise ScorerTraceUnreadable("unsafe_entry") from None
        finally:
            os.close(directory)
        try:
            if max_section_bytes is not None and any(
                location.length > max_section_bytes for location in reads.values()
            ):
                raise ScorerTraceUnreadable("too_large")
            data: dict[str, bytes] = {}
            for name, location in reads.items():
                chunk = _pread(fd, location.length, location.offset)
                if (
                    len(chunk) != location.length
                    or hashlib.sha256(chunk).hexdigest() != location.digest
                ):
                    raise ScorerTraceUnreadable("corrupt")
                data[name] = chunk
            return data
        finally:
            os.close(fd)

    # -- the directory layout -----------------------------------------------

    def _directory_record(
        self, root: tuple[str, ...], score_request_id: str, wanted: frozenset[str]
    ) -> ScorerTraceRecord | None:
        directory = self._open_directory((*root, TRACE_SUBDIR, score_request_id))
        if directory is None:
            return None
        try:
            entries = {
                section: _probe_file(directory, name, read=section in wanted)
                for section, name in _DIRECTORY_FILES.items()
            }
        finally:
            os.close(directory)
        return _directory_result(root, score_request_id, entries, wanted)

    def _admit_directory(
        self, root: tuple[str, ...], score_request_id: str, wanted: frozenset[str]
    ) -> _AdmittedDirectory | None:
        directory = self._open_directory((*root, TRACE_SUBDIR, score_request_id))
        if directory is None:
            return None
        admitted = _AdmittedDirectory(directory)
        try:
            for section, name in _DIRECTORY_FILES.items():
                if section not in wanted:
                    admitted.entries[section] = _probe_file(directory, name, read=False)
                    continue
                try:
                    fd = _file(directory, name, os.O_RDONLY)
                except FileNotFoundError:
                    admitted.entries[section] = _Probed(False, None, None)
                    continue
                except (OSError, JournalFault):
                    raise ScorerTraceUnreadable("unsafe_entry") from None
                try:
                    info = os.fstat(fd)
                except BaseException:
                    os.close(fd)
                    raise
                admitted.files[section] = (fd, info)
                admitted.entries[section] = _Probed(True, info.st_size, None)
            return admitted
        except BaseException:
            admitted.close()
            raise

    def _open_root(self, root: tuple[str, ...]) -> int | None:
        return self._open_directory(root)

    def _open_directory(self, parts: tuple[str, ...]) -> int | None:
        try:
            if not self._strict:
                return _directory(self._root.joinpath(*parts))
            assert self._root_fd is not None
            fd = os.dup(self._root_fd)
            try:
                for part in parts:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                # Match _directory's final-directory owner admission.
                if os.fstat(fd).st_uid != os.getuid():
                    raise JournalFault()
                return fd
            except BaseException:
                os.close(fd)
                raise
        except FileNotFoundError:
            return None
        except NotImplementedError:
            if self._strict:
                raise ScorerTraceUnreadable("unsafe_entry") from None
            raise
        except (OSError, JournalFault):
            raise ScorerTraceUnreadable("unsafe_entry") from None


def _nofollow_root(run_dir: str | os.PathLike[str]) -> int:
    """Establish a physical-cwd, no-follow root or refuse without path details."""

    try:
        if (
            not getattr(os, "O_DIRECTORY", 0)
            or not getattr(os, "O_NOFOLLOW", 0)
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
            or os.listdir not in os.supports_fd
            or any(
                not callable(getattr(os, name, None))
                for name in ("dup", "fstat", "pread", "geteuid")
            )
        ):
            raise ScorerTraceUnreadable("unsafe_entry")
        supplied = Path(run_dir)
        if ".." in supplied.parts:
            raise ScorerTraceUnreadable("unsafe_entry")
        root = supplied if supplied.is_absolute() else Path(os.getcwd()) / supplied
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            for part in root.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            if os.fstat(fd).st_uid != os.geteuid():
                raise ScorerTraceUnreadable("unsafe_entry")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except (OSError, JournalFault, NotImplementedError, AttributeError, ValueError):
        raise ScorerTraceUnreadable("unsafe_entry") from None


def _directory_result(
    root: tuple[str, ...],
    score_request_id: str,
    entries: Mapping[Section, _Probed],
    wanted: frozenset[str],
) -> ScorerTraceRecord:
    location = "/".join((*root, TRACE_SUBDIR, score_request_id))
    request = entries["request"]
    if not request.present:
        return ScorerTraceRecord(
            score_request_id, "directory", location, None, None, None, None, None
        )
    sizes: dict[str, int] = {}
    values: dict[str, bytes | None] = {}
    for section, probed in entries.items():
        size, data = probed.size, probed.data
        if section != "request" and not probed.present:
            # An empty stream file is removed once the scorer ends
            # (ADR-0040, D6): missing reads as empty.
            size, data = 0, b""
        if size is not None:
            sizes[section] = size
        values[section] = data if section in wanted else None
    return ScorerTraceRecord(
        score_request_id,
        "directory",
        location,
        values["request"],
        values["stdout"],
        values["stderr"],
        None,
        None,
        MappingProxyType(sizes),
    )


# -- the pack index -------------------------------------------------------------


@dataclass(frozen=True)
class _Location:
    offset: int
    length: int
    digest: str


@dataclass(frozen=True)
class _Output:
    status: TraceStatus
    returncode: int | None
    stdout: _Location
    stderr: _Location


@dataclass
class _Entry:
    request: _Location | None = None
    output: _Output | None = None
    ambiguous: bool = False


@dataclass(frozen=True)
class _PackIndex:
    name: str
    size: int
    entries: Mapping[str, _Entry]
    #: Offset of the first corrupt record, or `None`.
    corrupt_at: int | None
    #: The `pack` record's id and segment; `None` when no complete `pack`
    #: record exists yet (an empty file, or a first record still torn).
    pack_id: str | None
    segment: int | None
    #: End of the complete prefix; smaller than size without corruption means torn.
    complete_end: int


@dataclass(frozen=True)
class _Probed:
    present: bool
    size: int | None
    data: bytes | None


@dataclass
class _AdmittedDirectory:
    """Call-scoped directory observations and the selected objects they describe."""

    directory: int
    entries: dict[Section, _Probed] = field(default_factory=dict)
    files: dict[Section, tuple[int, os.stat_result]] = field(default_factory=dict)

    def close(self) -> None:
        try:
            for fd, _info in self.files.values():
                os.close(fd)
        finally:
            os.close(self.directory)


def _read_bounded(fd: int, admitted: os.stat_result, limit: int) -> bytes:
    """Read the pinned object through EOF or one byte beyond the inclusive cap."""

    chunks: list[bytes] = []
    consumed = 0
    while consumed <= limit:
        chunk = os.read(fd, min(limit + 1 - consumed, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        consumed += len(chunk)
    final = os.fstat(fd)
    if consumed > limit or final.st_size > limit:
        raise ScorerTraceUnreadable("too_large")
    if (
        final.st_size != admitted.st_size
        or final.st_mtime_ns != admitted.st_mtime_ns
        or consumed != final.st_size
    ):
        raise ScorerTraceUnreadable("corrupt")
    return b"".join(chunks)


def _validate_limit(limit: int | None) -> None:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("max_section_bytes must be a nonnegative integer or None")


def _index_file(directory: int, name: str) -> _PackIndex | None:
    """The header index of one pack file, `None` when it does not exist."""

    names = _name_ids(name)
    try:
        fd = _file(directory, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except (OSError, JournalFault):
        raise ScorerTraceUnreadable("unsafe_entry") from None
    try:
        return _scan(fd, os.fstat(fd).st_size, name, names)
    finally:
        os.close(fd)


def _scan(fd: int, size: int, name: str, names: frozenset[tuple[str, int]]) -> _PackIndex:
    """ADR-0040's torn and corrupt rule, applied against `size`.

    Torn (ignored, never an error): the last header line has no `\\n` while
    fewer than `MAX_HEADER_BYTES` bytes remain, or a header whose `hsha`
    verifies declares a body and terminator that run past `size`. Every
    other failure is corrupt, and the index stops there. Section digests are
    checked when a section is read, not here.
    """

    entries: dict[str, _Entry] = {}
    offset = 0
    pack_id: str | None = None
    segment: int | None = None
    corrupt_at: int | None = None
    while offset < size:
        window = _pread(fd, min(MAX_HEADER_BYTES, size - offset), offset)
        end = window.find(b"\n")
        if end < 0:
            if size - offset < MAX_HEADER_BYTES:
                break
            corrupt_at = offset
            break
        fields = _verified_header(window[:end])
        if fields is None:
            corrupt_at = offset
            break
        body = offset + end + 1
        declared = fields.get("sections", [])
        terminator = body + sum(int(item[1]) for item in declared)
        if terminator + 1 > size:
            break
        if _pread(fd, 1, terminator) != b"\n":
            corrupt_at = offset
            break
        kind = fields["kind"]
        if offset == 0:
            if kind != "pack" or (fields["pack_id"], fields["segment"]) not in names:
                corrupt_at = offset
                break
            pack_id, segment = fields["pack_id"], fields["segment"]
        elif kind == "pack":
            corrupt_at = offset
            break
        else:
            located: list[_Location] = []
            cursor = body
            for _name, length, digest in declared:
                located.append(_Location(cursor, int(length), str(digest)))
                cursor += int(length)
            entry = entries.setdefault(str(fields["score_request_id"]), _Entry())
            if kind == "request":
                if entry.request is not None:
                    entry.ambiguous = True
                else:
                    entry.request = located[0]
            elif entry.output is not None:
                entry.ambiguous = True
            else:
                entry.output = _Output(
                    cast(TraceStatus, fields["status"]),
                    cast("int | None", fields["returncode"]),
                    located[0],
                    located[1],
                )
        offset = terminator + 1
    return _PackIndex(name, size, entries, corrupt_at, pack_id, segment, offset)


def _verified_header(line: bytes) -> dict[str, Any] | None:
    """The header's fields without `hsha`, or `None` unless every rule holds."""

    try:
        fields = json.loads(line.decode("utf-8"))
        if not isinstance(fields, dict) or _canonical(fields) != line:
            return None
        claimed = fields.pop("hsha", None)
        if (
            not isinstance(claimed, str)
            or hashlib.sha256(_canonical(fields)).hexdigest() != claimed
        ):
            return None
        return fields if _well_formed(fields) else None
    except (UnicodeError, ValueError, RecursionError, TypeError):
        # A complete malformed header is a corrupt point in the pack. In
        # particular, parsed JSON may contain unhashable values in closed
        # fields or escaped lone surrogates that the UTF-8 canonicalizer
        # cannot encode; none may abort indexing of the valid prefix.
        return None


def _well_formed(fields: Mapping[str, Any]) -> bool:
    kind = fields.get("kind")
    if (
        fields.get("format") != PACK_FORMAT
        or not isinstance(kind, str)
        or kind not in _KEYS
        or set(fields) != _KEYS[kind]
    ):
        return False
    if kind == "pack":
        return (
            isinstance(fields["pack_id"], str)
            and PACK_ID.fullmatch(fields["pack_id"]) is not None
            and _count(fields["segment"])
        )
    request_id = fields["score_request_id"]
    if not isinstance(request_id, str) or TRACE_ID.fullmatch(request_id) is None:
        return False
    if kind == "output":
        returncode = fields["returncode"]
        status = fields["status"]
        if not isinstance(status, str) or status not in _STATUSES:
            return False
        if returncode is not None and (type(returncode) is not int):
            return False
    names = _SECTION_NAMES[kind]
    declared = fields["sections"]
    return (
        isinstance(declared, list)
        and len(declared) == len(names)
        and all(
            isinstance(item, list)
            and len(item) == 3
            and item[0] == name
            and _count(item[1])
            and isinstance(item[2], str)
            and _DIGEST.fullmatch(item[2]) is not None
            for item, name in zip(declared, names, strict=True)
        )
    )


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _name_ids(name: str) -> frozenset[tuple[str, int]]:
    """Every `(pack_id, segment)` a file name can stand for."""

    if not name.endswith(".pack"):
        return frozenset()
    base = name[: -len(".pack")]
    ids: set[tuple[str, int]] = set()
    if PACK_ID.fullmatch(base):
        ids.add((base, 0))
    match = _SEGMENT.fullmatch(base)
    if match is not None and PACK_ID.fullmatch(match["pack_id"]):
        ids.add((match["pack_id"], int(match["segment"])))
    return frozenset(ids)


def _pread(fd: int, length: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    read = 0
    while read < length:
        chunk = os.pread(fd, min(length - read, 1024 * 1024), offset + read)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def _probe_file(directory: int, name: str, *, read: bool) -> _Probed:
    """One directory-layout file: absent, present but unsafe, or sized (and read)."""

    try:
        fd = _file(directory, name, os.O_RDONLY)
    except FileNotFoundError:
        return _Probed(False, None, None)
    except (OSError, JournalFault):
        if read:
            raise ScorerTraceUnreadable("unsafe_entry") from None
        return _Probed(True, None, None)
    try:
        size = os.fstat(fd).st_size
        return _Probed(True, size, _read(fd, size) if read else None)
    finally:
        os.close(fd)


def _wanted(sections: Collection[str]) -> frozenset[str]:
    if isinstance(sections, str):
        raise ValueError("sections is a collection of section names")
    wanted = frozenset(sections)
    if not wanted <= set(SECTIONS):
        raise ValueError("unknown scorer trace section")
    return wanted


# -- the pack format, shared with the packed writer ---------------------------------


def encode_pack_record(
    fields: Mapping[str, Any], sections: Sequence[tuple[str, bytes]] | None = None
) -> bytes:
    """One `scorer_trace_pack/1` record: header line with `hsha`, raw sections, `\\n`.

    `fields` holds every header key except `hsha` and `sections`; `sections`
    is `None` for a `pack` record and the ordered `(name, bytes)` pairs
    otherwise. The bytes are refused unless this module's own reader accepts
    the header, so writer and reader cannot drift apart.
    """

    header = dict(fields)
    body = b""
    if sections is not None:
        header["sections"] = [
            [name, len(data), hashlib.sha256(data).hexdigest()] for name, data in sections
        ]
        body = b"".join(data for _name, data in sections)
    return _encode_header(header) + body + b"\n"


def _encode_header(fields: Mapping[str, Any]) -> bytes:
    """Shared canonical framing for byte fixtures and the streaming writer."""

    header = dict(fields)
    header["hsha"] = hashlib.sha256(_canonical(header)).hexdigest()
    line = _canonical(header)
    if len(line) + 1 > MAX_HEADER_BYTES or _verified_header(line) is None:
        raise ValueError("not a scorer_trace_pack/1 header")
    return line + b"\n"


# -- the internal readers' text rule ------------------------------------------------


def decode_trace_text(data: bytes | None) -> str:
    """`Path.read_text(encoding="utf-8")` applied to bytes already read.

    Strict UTF-8 and universal newlines, exactly as the internal readers read
    trace files before the shared reader, so their projections stay byte for
    byte (ADR-0037 I1). `None` (no such section) raises `ValueError`.
    """

    if data is None:
        raise ValueError("the trace holds no such section")
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


# -- the directory writer's storage rules (ADR-0040 D7) ------------------------------


class ScorerTraceStorageFault(JournalFault):
    """A failed trace write: a storage fault, never a scorer failure.

    `detail` names the trace entry by its run-relative path, the category (an
    `errno` name or `unsafe_entry`) and the operation; it never carries trace
    content or an absolute path.
    """

    def __init__(self, code: JournalCode, detail: str) -> None:
        super().__init__(code)
        self.detail = detail


def trace_storage_fault(
    exc: BaseException, location: str, operation: str
) -> ScorerTraceStorageFault:
    """`append_log._fault`'s classification, with the trace's own detail line."""

    if isinstance(exc, OSError):
        exhausted = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        code: JournalCode = "storage_capacity_exhausted" if exhausted else "journal_unavailable"
        category = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        remedy = (
            "free space or inodes on the run directory's filesystem"
            if exhausted
            else "check the filesystem"
        )
    else:
        code, category, remedy = "journal_unavailable", "unsafe_entry", "repair the entry"
    return ScorerTraceStorageFault(
        code,
        f"scorer trace {location}: {category} ({operation}); "
        f"{remedy} and run the same command again",
    )


def trace_location(trace_root: Path, path: Path) -> str:
    """`path` relative to the run directory that holds `trace_root`."""

    base = trace_root.parent
    if base.name == CONNECTION_LOGS:
        base = base.parent
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


#: Test seams: the raw write and fsync every `TraceStream` performs.
_raw_write: Callable[[io.FileIO, Any], int | None] = io.FileIO.write
_fsync: Callable[[int], None] = os.fsync
_raw_close: Callable[[io.FileIO], None] = io.FileIO.close


class TraceStream(io.FileIO):
    """A directory-layout stream file whose every storage failure is a fault."""

    location: str = ""

    def write(self, data: Any, /) -> int:
        remaining = memoryview(data).cast("B")
        size = len(remaining)
        try:
            while remaining:
                written = _raw_write(self, remaining)
                if written is None or written <= 0:
                    raise OSError(errno.EIO, "trace stream write made no progress")
                remaining = remaining[written:]
        except OSError as exc:
            raise trace_storage_fault(exc, self.location, "write") from exc
        return size

    def sync(self) -> None:
        try:
            _fsync(self.fileno())
        except OSError as exc:
            raise trace_storage_fault(exc, self.location, "fsync") from exc

    def close(self) -> None:
        try:
            _raw_close(self)
        except OSError as exc:
            raise trace_storage_fault(exc, self.location, "close") from exc


def open_trace_stream(path: Path, location: str) -> TraceStream:
    """Create one owner-only stream file exclusively, as the scorer did before."""

    try:
        stream = TraceStream(path, "xb")
    except OSError as exc:
        raise trace_storage_fault(exc, location, "create") from exc
    stream.location = location
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        fault = trace_storage_fault(exc, location, "create")
        # Keep the chmod failure as the first storage fault. Cleanup still
        # closes the descriptor and removes the empty file when possible.
        close_trace_streams((stream,), quiet=True)
        drop_empty_streams((stream,), quiet=True)
        raise fault from exc
    return stream


def settle_trace_streams(streams: Sequence[TraceStream], *, quiet: bool) -> None:
    """fsync each stream; `quiet` while a storage fault already travels.

    The first storage fault of a try is the one reported: a later fsync
    failure of the same trace adds nothing an operator can act on.
    """

    fault: JournalFault | None = None
    for stream in streams:
        try:
            stream.sync()
        except JournalFault as exc:
            if fault is None:
                fault = exc
    if fault is not None and not quiet:
        raise fault


def close_trace_streams(streams: Sequence[TraceStream], *, quiet: bool) -> None:
    """Close every stream and keep the first close fault, if any."""

    fault: JournalFault | None = None
    for stream in streams:
        try:
            stream.close()
        except JournalFault as exc:
            if fault is None:
                fault = exc
    if fault is not None and not quiet:
        raise fault


def drop_empty_streams(streams: Sequence[TraceStream], *, quiet: bool) -> None:
    """Remove the stream files the scorer left empty (ADR-0040 D13, PDR-0032).

    Not made durable: an empty file that reappears after a crash reads the
    same as a missing one (`b""`). `quiet` as for `settle_trace_streams`.
    """

    fault: JournalFault | None = None
    for stream in streams:
        path = Path(os.fsdecode(stream.name))
        try:
            if path.lstat().st_size == 0:
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if fault is None:
                fault = trace_storage_fault(exc, stream.location, "remove")
    if fault is not None and not quiet:
        raise fault
