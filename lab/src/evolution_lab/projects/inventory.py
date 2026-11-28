"""Source-side inventories of project places (spec §3.4-§3.5, ADR-0017 p. 6-7)."""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from evolution_lab._fileset import entry_fields, excluded_name, excluded_source


class NotRegularFile(OSError):
    """A path that is not a regular file (FIFO, socket, device) where one is read."""


def read_regular_file(file: Path) -> bytes:
    """The bytes of ``file`` if it is a regular file (symlinks followed). Opened with ``O_NONBLOCK`` and checked on
    the open descriptor, so a FIFO or a device at the path can never hold the service's one I/O worker (codex r1
    of the dev35 branch). A directory raises ``IsADirectoryError`` as ``Path.read_bytes`` does, anything else that
    is not a regular file ``NotRegularFile``."""
    descriptor = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0))
    try:
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(str(file))
        if not stat.S_ISREG(mode):
            raise NotRegularFile(str(file))
        os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            return handle.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)


class HashCache(Protocol):
    def lookup(self, path: str, key: tuple[int, int, int, int]) -> str | None: ...
    def store(self, path: str, key: tuple[int, int, int, int], sha256: str) -> None: ...


class BufferedHashCache:
    """Per-measurement writes; flush under the caller's short registry transaction.

    Reads see this measurement's pending values, then the shared cache. Each value
    retains its complete stat key: overlapping measurements can replace each other's
    cache rows, but a different key always misses rather than reusing a stale digest.
    Discard the buffer if measurement or publication fails.
    """

    def __init__(self, cache: HashCache) -> None:
        self._cache = cache
        self._pending: dict[str, tuple[tuple[int, int, int, int], str]] = {}

    def lookup(self, path: str, key: tuple[int, int, int, int]) -> str | None:
        pending = self._pending.get(path)
        if pending is not None and pending[0] == key:
            return pending[1]
        return self._cache.lookup(path, key)

    def store(self, path: str, key: tuple[int, int, int, int], sha256: str) -> None:
        self._pending[path] = (key, sha256)

    def flush(self) -> None:
        for path, (key, digest) in self._pending.items():
            self._cache.store(path, key, digest)


@dataclass(frozen=True, slots=True)
class Observation:
    """One look at an artifact path per status request (spec 2026-10-02 §2.1)."""
    state: str  # missing | not_regular | unreadable | read
    digest: str | None = None  # "sha256:<hex>" of the bytes, as attest._file_digest gives it
    data: bytes | None = None  # only when the caller asked for the bytes


_NOT_REGULAR_OPEN = frozenset({errno.ENXIO, errno.ENODEV})  # a socket or device swapped in after stat()


def observe_file(file: Path, *, read: bool, cache: HashCache | None = None) -> Observation:
    """Establish a regular file first (``stat``: a FIFO, socket or device is never opened — codex r2 of the dev38
    spec), then open it with ``O_NONBLOCK`` and read it. Without ``read`` the digest may come from the inventory
    hash cache keyed on ``fstat`` of the open descriptor."""
    try:
        info = os.stat(file)
    except (FileNotFoundError, NotADirectoryError):
        return Observation("missing")
    except OSError:
        return Observation("not_regular")
    if stat.S_ISDIR(info.st_mode):
        return Observation("missing")
    if not stat.S_ISREG(info.st_mode):
        return Observation("not_regular")
    try:
        descriptor = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0))
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return Observation("missing")
    except OSError as error:
        return Observation("not_regular" if error.errno in _NOT_REGULAR_OPEN else "unreadable")
    try:
        current = os.fstat(descriptor)
        if stat.S_ISDIR(current.st_mode):
            return Observation("missing")
        if not stat.S_ISREG(current.st_mode):
            return Observation("not_regular")
        key = (current.st_size, current.st_mtime_ns, current.st_ino, current.st_ctime_ns)
        if not read and cache is not None:
            known = cache.lookup(str(file), key)
            if known is not None:
                return Observation("read", "sha256:" + known)
        os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            if read:
                data = handle.read()
                value = hashlib.sha256(data).hexdigest()
            else:  # bounded blocks: an excluded multi-GiB artifact is never held whole (codex r1)
                data = None
                digest = hashlib.sha256()
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
                value = digest.hexdigest()
    except OSError:
        return Observation("unreadable")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if cache is not None:
        cache.store(str(file), key, value)
    return Observation("read", "sha256:" + value, data)


@dataclass(frozen=True, slots=True)
class Entry:
    path: str
    type: str  # file | directory | symlink | other
    executable: int
    size: int | None
    sha256: str | None
    target: str | None = None

    def content_key(self) -> tuple:
        return (self.type, self.executable, self.sha256, self.target)

    def to_payload(self) -> dict[str, Any]:
        value = {"path": self.path, "type": self.type, "executable": self.executable,
                 "size": self.size, "sha256": self.sha256}
        if self.type == "symlink":
            value["target"] = self.target
        return value

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "Entry":
        return cls(value["path"], value["type"], value["executable"], value["size"], value["sha256"], value.get("target"))


def _hash(path: Path, info: os.stat_result, cache: HashCache | None) -> str:
    key = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns)
    if cache is not None:
        known = cache.lookup(str(path), key)
        if known is not None:
            return known
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    value = digest.hexdigest()
    if cache is not None:
        cache.store(str(path), key, value)
    return value


def collect(source: Path, *, prefix: str, exclude: Sequence[str] = (), cache: HashCache | None = None) -> tuple[Entry, ...]:
    """Inventory a file or directory; entry paths are ``prefix``-relative POSIX paths."""
    entries: list[Entry] = []

    def skipped(relative: str) -> bool:
        for pattern in exclude:
            if fnmatch.fnmatchcase(relative, pattern):
                return True
            # A "dir/**" glob also excludes "dir" itself, not only its contents
            # (fnmatch treats "/" as an ordinary character, so "dir/**" alone
            # never matches the bare "dir" it is meant to prune).
            if pattern.endswith("/**") and relative == pattern[:-3]:
                return True
        return False

    def visit(path: Path, relative: str) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            entries.append(Entry(relative, "symlink", 0, None, None, os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            if relative:
                entries.append(Entry(**entry_fields(relative, info, sha256=None)))
            with os.scandir(path) as listing:
                children = sorted(listing, key=lambda item: item.name)
            for child in children:
                if excluded_name(child.name, directory=child.is_dir(follow_symlinks=False)):
                    continue
                child_relative = f"{relative}/{child.name}" if relative else child.name
                if not skipped(child_relative):
                    visit(Path(child.path), child_relative)
        elif stat.S_ISREG(info.st_mode):
            entries.append(Entry(**entry_fields(relative, info, sha256=_hash(path, info, cache))))
        else:
            entries.append(Entry(relative, "other", 0, None, None))

    # ADR-0017 p. 6 at the source boundary too, the same rule as a Bundle root: visit() filters
    # only children, so a source that is, or lies inside, an excluded entry (materials_root
    # ".git" or ".git/hooks", a selected "*.pyc", a place under ".pytest_cache") adds nothing.
    if (source.exists() or source.is_symlink()) and not excluded_source(
            source, directory=stat.S_ISDIR(source.lstat().st_mode)):
        visit(source, prefix)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def inventory_digest(entries: Iterable[Entry], *, extra: Mapping[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"entries": [entry.to_payload() for entry in sorted(entries, key=lambda e: e.path)]}
    if extra:
        payload["extra"] = dict(extra)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Changes:
    renamed: tuple[tuple[str, str], ...]
    modified: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.renamed or self.modified or self.added or self.removed)

    def to_payload(self, limit: int = 20) -> dict[str, Any]:
        return {
            "renamed": [list(pair) for pair in self.renamed[:limit]], "modified": list(self.modified[:limit]),
            "added": list(self.added[:limit]), "removed": list(self.removed[:limit]),
            "counts": {"renamed": len(self.renamed), "modified": len(self.modified),
                       "added": len(self.added), "removed": len(self.removed)},
        }

    def message(self) -> str:
        parts = [f"{len(v)} {name}" for name, v in (("переименовано", self.renamed), ("изменено", self.modified),
                                                     ("добавлено", self.added), ("удалено", self.removed)) if v]
        return ", ".join(parts) or "изменений в файлах нет"


def diff(old: Sequence[Entry], new: Sequence[Entry]) -> Changes:
    before = {entry.path: entry for entry in old}
    after = {entry.path: entry for entry in new}
    modified = tuple(sorted(p for p in before.keys() & after.keys() if before[p] != after[p]))
    gone, came = sorted(before.keys() - after.keys()), sorted(after.keys() - before.keys())
    pool: dict[tuple, list[str]] = {}
    for path in came:
        if after[path].type in ("file", "symlink"):
            pool.setdefault(after[path].content_key(), []).append(path)
    renamed, removed = [], []
    for path in gone:
        candidates = pool.get(before[path].content_key()) if before[path].type in ("file", "symlink") else None
        if candidates:
            renamed.append((path, candidates.pop(0)))
        else:
            removed.append(path)
    taken = {target for _, target in renamed}
    return Changes(tuple(renamed), modified, tuple(p for p in came if p not in taken), tuple(removed))
