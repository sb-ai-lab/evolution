"""Owned, bounded append-only log. Inspection never acquires a lease or mutates files.

The log knows records, descriptors, leases and byte bounds; it does not know any
event vocabulary. An `EnvelopeFamily` supplies validation and the start-event
predicate, and a `LogState` supplies the admission rules of one such vocabulary.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    LifecycleTransition,
    SchemaError,
)
from evolution.tree.storage.journal_errors import JournalAdmissionRefused, JournalFault


@dataclass(frozen=True)
class Snapshot:
    records: tuple[Envelope, ...]
    consistency: Literal["observed", "durable"]
    prefix_digest: str
    valid_bytes: int
    complete: bool
    journal_integrity: str
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def sequence(self) -> int:
        return len(self.records)


class LogState(Protocol):
    """Admission rules of one event vocabulary over a contiguous record prefix."""

    sequence: int

    def prepare(self, record: Envelope) -> LifecycleTransition: ...

    def accept(self, record: Envelope) -> None: ...

    @property
    def reserved_records(self) -> int: ...


def _fault(exc: OSError) -> JournalFault:
    return JournalFault(
        "storage_capacity_exhausted"
        if exc.errno in (errno.ENOSPC, errno.EDQUOT)
        else "journal_unavailable"
    )


def _validate_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise JournalFault()


def _directory(path: Path, *, create: bool = False) -> int:
    # Walk every component using descriptor-relative O_NOFOLLOW, including ancestors.
    parts = Path(os.path.abspath(path)).parts
    if ".." in path.parts:
        raise JournalFault()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts[1:]):
            if create and index == len(parts) - 2:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if os.fstat(fd).st_uid != os.getuid():
            raise JournalFault()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(directory: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        _validate_file(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, length: int) -> bytes:
    chunks = []
    offset = 0
    while offset < length:
        chunk = os.pread(fd, min(length - offset, 1024 * 1024), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _scan(
    raw: bytes,
    max_record_bytes: int,
    state_factory: Callable[[], LogState],
    envelope_family: EnvelopeFamily,
    secrets: Sequence[str] = (),
) -> tuple[Snapshot, LogState]:
    state = state_factory()
    records = []
    offset = 0
    integrity = "complete"
    diagnostics = []
    while offset < len(raw):
        end = raw.find(b"\n", offset)
        if end < 0:
            integrity = "incomplete_tail"
            diagnostics.append(
                dict(
                    scope="journal",
                    code="journal_unavailable",
                    offset=offset,
                    category=integrity,
                    length=len(raw) - offset,
                    digest=hashlib.sha256(raw[offset:]).hexdigest(),
                )
            )
            break
        line = raw[offset : end + 1]
        try:
            if len(line) > max_record_bytes:
                raise SchemaError("record limit")
            record = Envelope.from_bytes(line, secrets=secrets, family=envelope_family)
            state.accept(record)
        except (ValueError, TypeError):
            integrity = "corrupt_suffix"
            diagnostics.append(
                dict(
                    scope="journal",
                    code="journal_unavailable",
                    offset=offset,
                    category=integrity,
                    length=len(raw) - offset,
                    digest=hashlib.sha256(raw[offset:]).hexdigest(),
                )
            )
            break
        records.append(record)
        offset = end + 1
    if not raw:
        integrity = "incomplete_tail"
        diagnostics.append(
            dict(
                scope="journal",
                code="journal_unavailable",
                offset=0,
                category="incomplete_creation",
                length=0,
                digest=hashlib.sha256(b"").hexdigest(),
            )
        )
    return Snapshot(
        tuple(records),
        "observed",
        hashlib.sha256(raw[:offset]).hexdigest(),
        offset,
        integrity == "complete",
        integrity,
        tuple(diagnostics),
    ), state


def inspect_log(
    path: str | Path,
    *,
    file_name: str,
    max_record_bytes: int,
    max_log_bytes: int,
    state_factory: Callable[[], LogState],
    envelope_family: EnvelopeFamily,
    read: Callable[[int, int], bytes] = _read,
) -> Snapshot:
    """`read` is the descriptor reader seam; a caller substitutes an unstable one."""
    directory = fd = -1
    try:
        directory = _directory(Path(path))
        fd = _file(directory, file_name, os.O_RDONLY)
        for _ in range(2):
            size = os.fstat(fd).st_size
            if size > max_log_bytes:
                raise JournalFault("journal_limit_exceeded")
            raw = read(fd, size)
            end = raw.rfind(b"\n") + 1
            if len(raw) != size or read(fd, end) != raw[:end]:
                continue
            return _scan(raw, max_record_bytes, state_factory, envelope_family)[0]
        raise JournalFault("journal_busy")
    except OSError as exc:
        raise _fault(exc) from None
    finally:
        if fd >= 0:
            os.close(fd)
        if directory >= 0:
            os.close(directory)


class AppendLog:
    """One process-bound lease and one mutex for each log-lifetime writer."""

    def __init__(
        self,
        *,
        state_factory: Callable[[], LogState],
        max_record_bytes: int,
        max_log_bytes: int,
        secrets: Sequence[str],
        envelope_family: EnvelopeFamily,
    ) -> None:
        self._mutex = threading.RLock()
        self._pid = os.getpid()
        self._fd = self._directory_fd = self._lease = -1
        self._healthy = True
        self._state_factory = state_factory
        self._family = envelope_family
        self._state = state_factory()
        self._records: list[Envelope] = []
        self._bytes = 0
        self._hash = hashlib.sha256()
        self._max_record = max_record_bytes
        self._max_log = max_log_bytes
        self._secrets = tuple(secrets)

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        file_name: str,
        lock_name: str,
        create: bool,
        state_factory: Callable[[], LogState],
        max_record_bytes: int,
        max_log_bytes: int,
        secrets: Sequence[str],
        envelope_family: EnvelopeFamily,
        require_empty: bool = True,
        initial: bytes | None = None,
    ) -> AppendLog:
        """`require_empty` owns the whole directory on creation, as the S1 journal
        does. A log that shares a directory with foreign entries (the S2 ledger in a
        run root) passes False and owns its own names instead.

        `initial`, only meaningful with `create=True`, births the file whole: the
        bytes are staged next to it under `<file_name>.partial` and `os.replace`d
        into `file_name` after an fsync, so a reader never observes a file that
        holds a strict prefix of `initial`. A crash before the rename leaves the
        partial file behind and no `file_name` at all; the caller adopts the
        written content by scanning it back, exactly as a resume would.
        """
        log = cls(
            state_factory=state_factory,
            max_record_bytes=max_record_bytes,
            max_log_bytes=max_log_bytes,
            secrets=secrets,
            envelope_family=envelope_family,
        )
        try:
            if max_record_bytes < 1 or max_log_bytes < 1:
                raise JournalFault("journal_limit_exceeded")
            log._directory_fd = _directory(directory, create=create)
            if create and require_empty and os.listdir(log._directory_fd):
                raise JournalFault()
            log._lease = _file(
                log._directory_fd,
                lock_name,
                os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0),
            )
            try:
                fcntl.flock(log._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalFault("journal_busy") from None
            if create:
                os.fchmod(log._directory_fd, 0o700)
                os.fchmod(log._lease, 0o600)
            if create and initial is not None:
                partial_name = f"{file_name}.partial"
                fd = _file(log._directory_fd, partial_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    try:
                        offset = 0
                        while offset < len(initial):
                            written = os.write(fd, initial[offset:])
                            if written <= 0:
                                raise OSError(errno.EIO, "short partial write")
                            offset += written
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(
                        partial_name,
                        file_name,
                        src_dir_fd=log._directory_fd,
                        dst_dir_fd=log._directory_fd,
                    )
                except BaseException:
                    with suppress(OSError):
                        os.unlink(partial_name, dir_fd=log._directory_fd)
                    raise
                os.fsync(log._directory_fd)
                log._fd = _file(log._directory_fd, file_name, os.O_RDWR | os.O_APPEND)
                os.fchmod(log._fd, 0o600)
                log.scan()
                return log
            log._fd = _file(
                log._directory_fd,
                file_name,
                os.O_RDWR | os.O_APPEND | (os.O_CREAT | os.O_EXCL if create else 0),
            )
            if create:
                os.fchmod(log._fd, 0o600)
            return log
        except OSError as exc:
            log.close()
            raise _fault(exc) from None
        except BaseException:
            log.close()
            raise

    def scan(
        self, *, validate_prefix: Callable[[Snapshot], None] | None = None
    ) -> tuple[Snapshot, LogState]:
        """Adopt the durable prefix: read, validate, repair a torn tail, fsync.

        `validate_prefix` inspects the accepted prefix before any repair, so a
        caller that refuses the prefix leaves the file exactly as it found it.
        """
        self._check_process()
        with self._mutex:
            self.check()
            try:
                size = os.fstat(self._fd).st_size
                if size > self._max_log:
                    raise JournalFault("journal_limit_exceeded")
                raw = _read(self._fd, size)
                snapshot, state = _scan(
                    raw, self._max_record, self._state_factory, self._family, self._secrets
                )
                if (
                    len(raw) != size
                    or not snapshot.records
                    or snapshot.journal_integrity == "corrupt_suffix"
                ):
                    raise JournalFault()
                if validate_prefix is not None:
                    validate_prefix(snapshot)
                if snapshot.valid_bytes + state.reserved_records * self._max_record > self._max_log:
                    raise JournalFault("journal_limit_exceeded")
                if snapshot.valid_bytes != size:
                    os.ftruncate(self._fd, snapshot.valid_bytes)
                os.fsync(self._fd)
                self._state = state
                self._records = list(snapshot.records)
                self._bytes = snapshot.valid_bytes
                self._hash = hashlib.sha256()
                self._hash.update(raw[: snapshot.valid_bytes])
                return snapshot, state
            except OSError as exc:
                raise _fault(exc) from None

    def append(
        self,
        run_id: str,
        event: str,
        payload: dict[str, Any],
        operation_id: str | None = None,
        *,
        on_limit: Callable[[], None] | None = None,
    ) -> int:
        self._check_process()
        with self._mutex:
            self.check()
            try:
                record = Envelope.from_payload(
                    dict(
                        schema_version=1,
                        run_id=run_id,
                        sequence=self._state.sequence + 1,
                        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        event=event,
                        operation_id=operation_id,
                        payload=payload,
                    ),
                    secrets=self._secrets,
                    family=self._family,
                )
            except (ValueError, TypeError):
                self._healthy = False
                raise JournalFault() from None
            return self._commit(record, on_limit)

    def append_envelope(self, record: Envelope) -> int:
        """Append an envelope the caller built and already admitted.

        The record is re-admitted here — same secrets, same family — and its
        sequence must be the next one; then it takes exactly `append`'s path,
        so the bytes on disk are the caller's, timestamp included.
        """

        self._check_process()
        with self._mutex:
            self.check()
            try:
                admitted = Envelope.from_payload(
                    record.to_payload(), secrets=self._secrets, family=self._family
                )
                if admitted.to_payload()["sequence"] != self._state.sequence + 1:
                    # The caller's envelope does not continue this log's sequence.
                    self._healthy = False
                    raise JournalFault() from None
            except (ValueError, TypeError):
                self._healthy = False
                raise JournalFault() from None
            return self._commit(admitted, None)

    def _commit(self, record: Envelope, on_limit: Callable[[], None] | None) -> int:
        """Admit a built record to the lifecycle and write it (mutex already held).

        Shared by `append`, which builds the record from its parts, and
        `append_envelope`, which re-admits a record the caller already built —
        so the bytes this lays down are identical either way, timestamp
        included.
        """

        try:
            transition = self._state.prepare(record)
            encoded = record.encode()
            if len(encoded) > self._max_record:
                raise JournalFault("journal_limit_exceeded")
            if (
                self._bytes + len(encoded) + transition.reserved_records * self._max_record
                > self._max_log
            ):
                # Reserved capacity still admits terminals; only starts are refused.
                if self._family.is_start(record.event):
                    if on_limit is not None:
                        on_limit()
                    raise JournalAdmissionRefused()
                raise JournalFault("journal_limit_exceeded")
            if os.fstat(self._fd).st_size != self._bytes:
                raise JournalFault()
            offset = 0
            while offset < len(encoded):
                written = os.write(self._fd, encoded[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short journal write")
                offset += written
            os.fsync(self._fd)
            transition.commit()
            self._records.append(record)
            self._bytes += len(encoded)
            self._hash.update(encoded)
            return self._state.sequence
        except JournalAdmissionRefused:
            raise
        except OSError as exc:
            self._healthy = False
            raise _fault(exc) from None
        except JournalFault:
            self._healthy = False
            raise
        except (ValueError, TypeError):
            self._healthy = False
            raise JournalFault() from None

    def snapshot(self) -> Snapshot:
        self._check_process()
        with self._mutex:
            self.check()
            return Snapshot(
                tuple(self._records),
                "durable",
                self._hash.hexdigest(),
                self._bytes,
                True,
                "complete",
            )

    @contextmanager
    def guard(self) -> Iterator[LogState]:
        """Hold the owner's mutex over a state read plus an optional append."""
        self._check_process()
        with self._mutex:
            self.check()
            yield self._state

    def invalidate(self) -> None:
        """Poison this owner after a boundary translation fault, without writing."""
        self._check_process()
        with self._mutex:
            self._healthy = False

    @property
    def state(self) -> LogState:
        return self._state

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def secrets(self) -> tuple[str, ...]:
        """The secret set every appended record is validated against."""
        return self._secrets

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def fd(self) -> int:
        return self._fd

    @property
    def directory_fd(self) -> int:
        return self._directory_fd

    @property
    def mutex(self) -> threading.RLock:
        return self._mutex

    def _check_process(self) -> None:
        # A fork may inherit a mutex held by a vanished thread. Never enter it.
        if os.getpid() != self._pid:
            raise JournalFault()

    def check(self) -> None:
        self._check_process()
        if not self._healthy or self._fd < 0:
            raise JournalFault()

    def _close_descriptors(self) -> None:
        # Never explicitly flock-unlock: inherited descriptors share the parent's lease.
        for name in ("_fd", "_lease", "_directory_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)
        self._healthy = False

    def close(self) -> None:
        if os.getpid() != self._pid:
            self._close_descriptors()
            return
        with self._mutex:
            self._close_descriptors()
