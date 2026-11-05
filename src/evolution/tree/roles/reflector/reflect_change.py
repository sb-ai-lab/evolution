"""Change fingerprints and change descriptions (ADR-0039 amendment).

One fingerprint identifies what a sealed Candidate did to one visible path
relative to its Parent: `sha256:` over the canonical JSON of a normalized
record (`reflect_change_fingerprint/1`). Text sides are strict UTF-8 with
`\\r\\n` and `\\r` read as `\\n` (as the seal scan's `Path.read_text` reads
them); hunks are the seal scan's matcher opcodes as ordered removed/added line
lists with no line numbers and no context; binary sides, kind changes and
sides beyond the per-path work bound are identified by content digests. A side
of a path that is not itself a visible file contributes only the kind `file`:
its bytes are never read. The record never includes the path.

The description is derived from the record alone (counts plus the first
non-blank added line, screened and cut by its serialized literal); it never
holds Editor or Reflector prose. The screen never changes a fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any

from evolution.tree.artifacts.artifact_policy import Entry
from evolution.tree.execution.check_modes import HitSink
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.reflector_contracts import (
    CHANGE_EXCERPT_MAX_CHARS,
    CHANGE_FINGERPRINT_SCHEME,
    FINGERPRINT_MAX_BYTES,
    FINGERPRINT_MAX_LINES,
    PATH_LITERAL_MAX_CHARS,
    find_needle,
)

__all__ = [
    "PathChange",
    "Screen",
    "Side",
    "SideChanged",
    "change_record",
    "cut_to_literal",
    "describe",
    "excerpt_screen",
    "fingerprint",
    "literal_chars",
    "path_change",
    "path_display",
    "read_side",
]

_LINE_KINDS = frozenset({"absent", "text", "text+x"})

#: `True` drops an excerpt line (it matched an oracle needle or a run secret).
Screen = Callable[[str], bool]


class SideChanged(Exception):
    """A side no longer matches the scan `_changes` made of it."""


@dataclass(frozen=True)
class Side:
    """One side of one path: its kind, its raw bytes when it is a visible file,
    and its normalized lines when it is text within the work bound (`()` for
    `absent`, `None` when not diffable)."""

    kind: str
    raw: bytes | None = None
    lines: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PathChange:
    """One (fact row, path): its fingerprint and description, or both `None`."""

    fingerprint: str | None
    change: Mapping[str, Any] | None


def _parent_fd(root: Path, path: str) -> tuple[int, str]:
    parts = PurePosixPath(path).parts
    if (
        PurePosixPath(path).is_absolute()
        or not parts
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise SideChanged(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, directory_flags)
    except (FileNotFoundError, NotADirectoryError):
        # The published scan root is an identity boundary. A missing root is
        # stale evidence even when this particular path was absent in the scan.
        raise SideChanged(path) from None
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except NotADirectoryError:
                # A file ancestor can explain a genuinely absent descendant,
                # but a symlink ancestor is stale evidence and must not be
                # reclassified as an ordinary absence.
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise SideChanged(path) from None
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _is_executable(mode: int) -> bool:
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _same_file(info: os.stat_result, other: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and (
        info.st_dev,
        info.st_ino,
        info.st_size,
        _is_executable(info.st_mode),
    ) == (other.st_dev, other.st_ino, other.st_size, _is_executable(other.st_mode))


def read_side(root: Path, path: str, entry: Entry | None, *, hidden: bool) -> Side:
    try:
        parent_fd, name = _parent_fd(root, path)
    except (FileNotFoundError, NotADirectoryError):
        if entry is None:
            return Side("absent", None, ())
        raise SideChanged(path) from None
    try:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if entry is None:
                return Side("absent", None, ())
            raise SideChanged(path) from None
        if stat.S_ISLNK(info.st_mode) or entry is None:
            raise SideChanged(path)
        if entry.kind == "directory":
            if not stat.S_ISDIR(info.st_mode) or entry.executable or entry.size != 0:
                raise SideChanged(path)
            return Side("directory")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != entry.size
            or _is_executable(info.st_mode) != entry.executable
        ):
            raise SideChanged(path)
        if hidden:
            return Side("file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not _same_file(info, opened):
                raise SideChanged(path)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent_fd)
    if len(raw) != entry.size:
        raise SideChanged(path)
    flag = "+x" if entry.executable else ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Side("binary" + flag, raw)
    if len(raw) > FINGERPRINT_MAX_BYTES:
        return Side("text" + flag, raw)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    return Side("text" + flag, raw, tuple(lines) if len(lines) <= FINGERPRINT_MAX_LINES else None)


def _content(side: Side) -> str | None:
    return None if side.raw is None else "sha256:" + hashlib.sha256(side.raw).hexdigest()


def change_record(before: Side, after: Side) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scheme": CHANGE_FINGERPRINT_SCHEME,
        "before": before.kind,
        "after": after.kind,
    }
    if before.kind in _LINE_KINDS and after.kind in _LINE_KINDS:
        if before.lines is not None and after.lines is not None:
            old, new = list(before.lines), list(after.lines)
            hunks = [
                [old[i1:i2], new[j1:j2]]
                for tag, i1, i2, j1, j2 in SequenceMatcher(
                    None, old, new, autojunk=False
                ).get_opcodes()
                if tag != "equal"
            ]
            return {**record, "basis": "lines", "hunks": hunks, "bytes": None}
        basis = "too_large"
    else:
        basis = "content"
    return {
        **record,
        "basis": basis,
        "hunks": None,
        "bytes": {"before": _content(before), "after": _content(after)},
    }


def fingerprint(record: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(record)).hexdigest()


def literal_chars(text: str) -> int:
    """The length of `text`'s JSON string literal, quotes included, as
    `canonical_json` writes it (`ensure_ascii=False`)."""

    return len(json.dumps(text, ensure_ascii=False))


def cut_to_literal(text: str, limit: int) -> tuple[str, bool]:
    """The longest prefix (in code points) whose literal is at most `limit`."""

    if literal_chars(text) <= limit:
        return text, False
    size, end = 2, 0
    for character in text:
        width = literal_chars(character) - 2
        if size + width > limit:
            break
        size += width
        end += 1
    return text[:end], True


def excerpt_screen(
    needles: Sequence[str],
    secrets: Sequence[str],
    *,
    mode: str = "enforce",
    hits: HitSink | None = None,
) -> Screen:
    """The excerpt screen checks the seal's oracle needles
    (`oracle_needles(corpus, declared)`) and the run's secret values."""

    values = tuple(secret for secret in secrets if secret)

    def blocked(line: str) -> bool:
        if mode == "off":
            return False
        matched = find_needle(line, needles) or any(value in line for value in values)
        if matched and mode == "shadow" and hits is not None:
            hits.hit("history_excerpt_screen", "excerpt_dropped")
        return matched and mode == "enforce"

    return blocked


def describe(record: Mapping[str, Any], *, screen: Screen) -> dict[str, Any]:
    hunks = record["hunks"]
    added: int | None = None
    removed: int | None = None
    count: int | None = None
    first: str | None = None
    if hunks is not None:
        added = sum(len(hunk[1]) for hunk in hunks)
        removed = sum(len(hunk[0]) for hunk in hunks)
        count = len(hunks)
        first = next((line.strip() for hunk in hunks for line in hunk[1] if line.strip()), None)
    excerpt: str | None = None
    cut = False
    if first is not None and not screen(first):
        excerpt, cut = cut_to_literal(first, CHANGE_EXCERPT_MAX_CHARS)
    return {
        "basis": record["basis"],
        "before": record["before"],
        "after": record["after"],
        "added": added,
        "removed": removed,
        "hunks": count,
        "excerpt": excerpt,
        "excerpt_cut": cut,
    }


def path_change(
    parent_root: Path,
    child_root: Path,
    path: str,
    parent_entries: Mapping[str, Entry],
    child_entries: Mapping[str, Entry],
    *,
    visible: Callable[[str], bool] | None,
    screen: Screen,
) -> PathChange:
    """The fingerprint and description of one listed path, or `PathChange(None,
    None)` when a side cannot be read although `_changes` listed it."""

    hidden = visible is not None and not visible(path)
    try:
        before = read_side(parent_root, path, parent_entries.get(path), hidden=hidden)
        after = read_side(child_root, path, child_entries.get(path), hidden=hidden)
    except (OSError, SideChanged):
        return PathChange(None, None)
    record = change_record(before, after)
    return PathChange(fingerprint(record), describe(record, screen=screen))


def path_display(path: str) -> dict[str, str | None]:
    """The path display rule, over the serialized literal."""

    if literal_chars(path) <= PATH_LITERAL_MAX_CHARS:
        return {"path": path, "path_sha256": None}
    return {"path": None, "path_sha256": hashlib.sha256(path.encode("utf-8")).hexdigest()}
