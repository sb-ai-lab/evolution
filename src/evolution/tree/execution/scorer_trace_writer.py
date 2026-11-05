"""Private streaming scorer packs (ADR-0040).

Owners retain a writer for a scoring session and close it at session end. Owners
of the same absolute pack path share a lock, adopted index and first fault. The
last close (also best-effort on finalization) releases that session. Pack file
descriptors are open only during an operation, never during external scoring.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import tempfile
import threading
import weakref
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.interaction import ScoringError
from evolution.tree.execution.scorer_traces import (
    PACK_FORMAT,
    PACK_ID,
    TRACE_ID,
    ScorerTraceStorageFault,
    TraceStatus,
    TraceStream,
    _encode_header,
    _Location,
    _name_ids,
    _scan,
    close_trace_streams,
    trace_location,
    trace_storage_fault,
)
from evolution.tree.storage.append_log import _directory
from evolution.tree.storage.journal_errors import JournalFault

_CHUNK = 1024 * 1024
# Storage fault injection seams; all writes make complete progress or fault.
_write = os.write
_fsync = os.fsync
_truncate = os.ftruncate


@dataclass(frozen=True)
class PackedRecord:
    path: Path
    sections: Mapping[str, _Location]


@dataclass
class _Session:
    lock: Any = field(default_factory=threading.RLock)
    owners: int = 0
    root_synced: bool = False
    adopted: bool = False
    segment: int = 0
    identity: tuple[int, int, int] | None = None
    known: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)
    fault: ScorerTraceStorageFault | None = None


_sessions: weakref.WeakValueDictionary[Path, _Session] = weakref.WeakValueDictionary()
# Allocating a session can collect a writer and invoke _release on this thread.
_sessions_lock = threading.RLock()


def _release(key: Path, reference: weakref.ReferenceType[_Session]) -> None:
    with _sessions_lock:
        session = reference()
        if session is not None:
            session.owners -= 1
            if session.owners == 0 and _sessions.get(key) is session:
                del _sessions[key]


def _identity(fd: int) -> tuple[int, int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_size


def _open(directory: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise JournalFault()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, data: bytes | memoryview) -> None:
    remaining = memoryview(data)
    while remaining:
        written = _write(fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "pack write made no progress")
        remaining = remaining[written:]


def _chunks(source: bytes | TraceStream, length: int) -> Iterator[bytes | memoryview]:
    offset = 0
    while offset < length:
        if isinstance(source, bytes):
            chunk: bytes | memoryview = memoryview(source)[offset : offset + _CHUNK]
        else:
            chunk = os.pread(source.fileno(), min(_CHUNK, length - offset), offset)
            if not chunk:
                raise OSError(errno.EIO, "trace buffer ended early")
        offset += len(chunk)
        yield chunk


def _describe(source: bytes | TraceStream) -> tuple[int, str]:
    length = len(source) if isinstance(source, bytes) else os.fstat(source.fileno()).st_size
    digest = hashlib.sha256()
    for chunk in _chunks(source, length):
        digest.update(chunk)
    return length, digest.hexdigest()


class PackedTraceWriter:
    def __init__(
        self, trace_root: Path, pack_id: str, *, admitted_hygiene: Collection[str] = ()
    ) -> None:
        if not isinstance(pack_id, str) or PACK_ID.fullmatch(pack_id) is None:
            raise ValueError("pack id is not a safe file name")
        if ".." in Path(trace_root).parts:
            raise ValueError("trace root must not traverse parents")
        self.trace_root = Path(os.path.abspath(trace_root))
        self.pack_id = pack_id
        self.admitted_hygiene = tuple(admitted_hygiene)
        key = self.trace_root / f"{pack_id}.pack"
        with _sessions_lock:
            self._session = _sessions.setdefault(key, _Session())
            self._session.owners += 1
        self._finalizer = weakref.finalize(self, _release, key, weakref.ref(self._session))

    def __enter__(self) -> PackedTraceWriter:
        self._check()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._session.lock:
            self._finalizer()

    def _check(self) -> None:
        if not self._finalizer.alive:
            raise ValueError("packed trace writer is closed")
        if self._session.fault is not None:
            raise self._session.fault

    def _name(self, segment: int | None = None) -> str:
        number = self._session.segment if segment is None else segment
        return f"{self.pack_id}.s{number}.pack" if number else f"{self.pack_id}.pack"

    def _fault(
        self, exc: BaseException, operation: str, name: str | None = None
    ) -> ScorerTraceStorageFault:
        with self._session.lock:
            if self._session.fault is None:
                self._session.fault = (
                    exc
                    if isinstance(exc, ScorerTraceStorageFault)
                    else trace_storage_fault(
                        exc,
                        trace_location(self.trace_root, self.trace_root / (name or self._name())),
                        operation,
                    )
                )
            return self._session.fault

    def _close(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError as exc:
            raise self._fault(exc, "close") from exc

    def _root(self) -> int:
        from evolution.tree.execution.output_admission import EvolveRefused, admit_output_root

        try:
            admit_output_root(self.trace_root, admitted=self.admitted_hygiene)
            ensure_private_directory(self.trace_root)
            directory = _directory(self.trace_root)
        except (EvolveRefused, OSError, ValueError, JournalFault) as exc:
            raise self._fault(exc, "admit") from exc
        try:
            if not self._session.root_synced:
                self._sync_root_parents()
                self._session.root_synced = True
            return directory
        except BaseException:
            self._close(directory)
            raise

    def _sync_root_parents(self) -> None:
        # mkdir(parents=True) can create several entries. Existing entries also
        # need syncing: an earlier session may have failed after creating them.
        # Walk without following links; ancestors retain their existing policy
        # (only the trace root must be owned/private). No descriptors survive
        # initialization, and shared owners do this only once per session.
        with ExitStack() as parents:
            try:
                fd = None
                for part in self.trace_root.parts[:-1]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    parents.callback(self._close, fd)
                    _fsync(fd)
            except OSError as exc:
                # Record the sync/open fault before cleanup can fail as well.
                raise self._fault(exc, "sync root parents") from exc

    def _adopt(self, directory: int) -> None:
        session = self._session
        candidates = sorted(
            (segment, name)
            for name in os.listdir(directory)
            for pack_id, segment in _name_ids(name)
            if pack_id == self.pack_id
        )
        highest = candidates[-1][0] if candidates else 0
        if any(segment != expected for expected, (segment, _name) in enumerate(candidates)):
            raise self._fault(JournalFault(), "adopt noncontiguous segments")
        for segment, name in candidates:
            try:
                fd = _open(directory, name)
            except (OSError, JournalFault) as exc:
                raise self._fault(exc, "adopt", name) from exc
            try:
                index = _scan(fd, os.fstat(fd).st_size, name, _name_ids(name))
                if index.pack_id is not None and (index.pack_id, index.segment) != (
                    self.pack_id,
                    segment,
                ):
                    # A valid other interpretation of foo.s1.pack cannot be rotated
                    # around: explicit reader lookup stops at this collision.
                    raise JournalFault()
                session.known.update(index.entries)
                if index.corrupt_at is not None:
                    print(
                        f"scorer trace {trace_location(self.trace_root, self.trace_root / name)}: corrupt record at byte {index.corrupt_at}; continuing in a later segment",
                        file=sys.stderr,
                    )
                if segment != highest:
                    continue
                session.segment = segment
                if index.corrupt_at is not None:
                    session.segment += 1
                    continue
                if index.complete_end < index.size:
                    _truncate(fd, index.complete_end)
                    _fsync(fd)
                if index.complete_end == 0:
                    self._append(
                        fd, {"kind": "pack", "pack_id": self.pack_id, "segment": segment}, ()
                    )
                session.identity = _identity(fd)
            except (OSError, JournalFault) as exc:
                raise self._fault(exc, "adopt", name) from exc
            finally:
                self._close(fd)
        if session.identity is None:
            fd = _open(directory, self._name(), create=True)
            try:
                self._append(
                    fd, {"kind": "pack", "pack_id": self.pack_id, "segment": session.segment}, ()
                )
                session.identity = _identity(fd)
            finally:
                self._close(fd)
        # A previous session may have persisted the header but failed to sync
        # its directory entry. Adoption must establish that durability too.
        _fsync(directory)
        session.adopted = True

    def _append(
        self,
        fd: int,
        fields: Mapping[str, Any],
        sections: Sequence[tuple[str, bytes | TraceStream]],
    ) -> PackedRecord:
        header = {"format": PACK_FORMAT, **fields}
        declared = [(name, *_describe(source)) for name, source in sections]
        if sections:
            header["sections"] = [list(item) for item in declared]
        line = _encode_header(header)
        start = os.fstat(fd).st_size
        cursor = start + len(line)
        located = {}
        try:
            _write_all(fd, line)
            for (name, length, digest), (_, source) in zip(declared, sections, strict=True):
                located[name] = _Location(cursor, length, digest)
                for chunk in _chunks(source, length):
                    _write_all(fd, chunk)
                cursor += length
            _write_all(fd, b"\n")
            _fsync(fd)
        except OSError as exc:
            fault = self._fault(exc, "append")
            try:
                _truncate(fd, start)
                _fsync(fd)
            except OSError:
                pass
            raise fault from exc
        self._session.identity = _identity(fd)
        return PackedRecord(self.trace_root / self._name(), MappingProxyType(located))

    @contextmanager
    def _pack(self) -> Iterator[int]:
        with self._session.lock:
            self._check()
            directory = self._root()
            try:
                if not self._session.adopted:
                    self._adopt(directory)
                fd = _open(directory, self._name())
                try:
                    if _identity(fd) != self._session.identity:
                        raise JournalFault()
                    yield fd
                finally:
                    self._close(fd)
            except (OSError, JournalFault) as exc:
                raise self._fault(exc, "write") from exc
            finally:
                self._close(directory)

    def append_request(self, trace_id: str, request: bytes) -> PackedRecord:
        if TRACE_ID.fullmatch(trace_id) is None:
            raise ValueError("invalid scorer trace id")
        with self._pack() as fd:
            if trace_id in self._session.known:
                raise ScoringError("scorer trace id is already in use")
            record = self._append(
                fd, {"kind": "request", "score_request_id": trace_id}, (("request", request),)
            )
            self._session.known.add(trace_id)
            self._session.pending.add(trace_id)
            return record

    def append_output(
        self,
        trace_id: str,
        stdout: TraceStream,
        stderr: TraceStream,
        *,
        status: TraceStatus,
        returncode: int | None,
    ) -> PackedRecord:
        with self._pack() as fd:
            if trace_id not in self._session.pending:
                raise ScoringError("scorer trace id is already in use or has no request")
            record = self._append(
                fd,
                {
                    "kind": "output",
                    "score_request_id": trace_id,
                    "status": status,
                    "returncode": returncode,
                },
                (("stdout", stdout), ("stderr", stderr)),
            )
            self._session.pending.remove(trace_id)
            return record

    @contextmanager
    def streams(self) -> Iterator[tuple[TraceStream, TraceStream]]:
        """Two anonymous owner-only sinks; external scoring holds no pack lock."""
        streams: list[TraceStream] = []
        failure: BaseException | None = None
        try:
            with self._session.lock:
                self._check()
                directory = self._root()
                self._close(directory)
            try:
                for _ in range(2):
                    with tempfile.TemporaryFile(dir=self.trace_root, buffering=0) as temporary:
                        fd = os.dup(temporary.fileno())
                        try:
                            stream = TraceStream(fd, "r+")
                        except OSError as exc:
                            fault = self._fault(exc, "buffer")
                            self._close(fd)
                            raise fault from exc
                        stream.location = trace_location(
                            self.trace_root, self.trace_root / self._name()
                        )
                        streams.append(stream)
            except OSError as exc:
                raise self._fault(exc, "buffer") from exc
            yield streams[0], streams[1]
        except BaseException as exc:
            failure = exc
            if isinstance(exc, ScorerTraceStorageFault):
                raise self._fault(exc, "buffer") from exc
            raise
        finally:
            try:
                close_trace_streams(streams, quiet=isinstance(failure, JournalFault))
            except JournalFault as exc:
                raise self._fault(exc, "close") from exc
