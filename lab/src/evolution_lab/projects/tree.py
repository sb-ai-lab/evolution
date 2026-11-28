"""Skill inventory views shared by tree confirmations and project tree reads.

A tree is a folder or a single regular file (PDR-0032 as amended 2026-11-13).
"""
from __future__ import annotations

from dataclasses import replace
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat

from .errors import ProjectError
from .inventory import Entry
from .revision import _nested_cut


def parse_ref(ref: str) -> tuple[str, str]:
    from .attest import _safe_relative
    if not isinstance(ref, str):
        raise ProjectError('invalid_request')
    parts = ref.split(':', 2)
    if len(parts) != 3 or parts[0] != 'implementation' or not parts[1]:
        raise ProjectError('invalid_request')
    return parts[1], _safe_relative(parts[2])


def _place(project, implementation_id: str) -> tuple[Path, str]:
    implementation = project.implementation(implementation_id)
    if implementation is None or implementation.archived is not None:
        raise ProjectError('implementation_not_found')
    value = Path(implementation.place)
    root = Path(project.working_copy.path)
    return (value if value.is_absolute() else root / value,
            f'@{implementation_id}' if value.is_absolute() else ('' if str(value) == '.' else str(value)))


def _safe_folder(place: Path, relpath: str) -> str:
    from .attest import _safe_relative
    relpath = _safe_relative(relpath)
    try:
        if not (place / relpath).resolve().is_relative_to(place.resolve()):
            raise ProjectError('invalid_request')
    except (OSError, RuntimeError):
        raise ProjectError('invalid_request') from None
    return Path(relpath).as_posix()


def _regular_file(place: Path, relpath: str) -> bool:
    # lstat: a symlink is neither a folder nor a single-file tree.
    try:
        return relpath != '.' and stat.S_ISREG(os.lstat(place / relpath).st_mode)
    except OSError:
        return False


def _glob_tokens(pattern: str) -> list[str]:
    # The inventory uses fnmatch (where * and ? include '/'). Preserve bracket
    # expressions as one token and treat a malformed bracket as a literal.
    result = []
    i = 0
    while i < len(pattern):
        if pattern[i] == '[':
            start = i + 1
            if start < len(pattern) and pattern[start] == '!':
                start += 1
            if start < len(pattern) and pattern[start] == ']':
                start += 1
            end = pattern.find(']', start)
            if end >= 0:
                result.append(pattern[i:end + 1])
                i = end + 1
                continue
        result.append(pattern[i])
        i += 1
    return result


def _can_match_prefix(pattern: str, prefix: str) -> bool:
    tokens = _glob_tokens(pattern)

    def closure(states: set[int]) -> set[int]:
        states = set(states)
        for i in range(len(tokens)):
            if i in states and tokens[i] == '*':
                states.add(i + 1)
        return states

    states = closure({0})
    for char in prefix:
        following = set()
        for i in states:
            if i == len(tokens):
                continue
            token = tokens[i]
            if token == '*':
                following.add(i)
            elif token == '?' or token == char or (
                    token.startswith('[') and len(token) > 1 and fnmatch.fnmatchcase(char, token)):
                following.add(i + 1)
        states = closure(following)
        if not states:
            return False
    return bool(states)  # some suffix can satisfy all remaining glob tokens



def excluded(project, implementation_id: str, relpath: str) -> str | None:
    place, prefix = _place(project, implementation_id)
    relpath = _safe_folder(place, relpath)
    folder = '/'.join(part for part in (prefix, '' if relpath == '.' else relpath) if part)
    # A file has no contents: only the file itself and its ancestors can match.
    single = _regular_file(place, relpath)
    for pattern in project.exclude:
        ancestors = [folder[:i] for i, char in enumerate(folder) if char == '/'] + [folder]
        if (any(fnmatch.fnmatchcase(path, pattern) or
                (pattern.endswith('/**') and path == pattern[:-3]) for path in ancestors) or
                (not single and _can_match_prefix(pattern, folder + '/' if folder else ''))):
            return pattern
    materials = Path(project.working_copy.path) / project.materials_root
    cut = _nested_cut(place, prefix, materials)
    if cut is not None and (not folder or folder == cut or folder.startswith(cut + '/') or cut.startswith(folder + '/')):
        return cut
    return None


def tree_entries(project, revision, implementation_id: str, relpath: str) -> tuple[Entry, ...] | None:
    """Return the tree's entries relative to the selected path.

    A folder's entries are relative to the folder. A single regular file is one
    entry with path '.', which no folder entry can have, so replacing the file by
    a folder never keeps its digest. A symlink, an empty folder or a missing
    path has no tree.
    """
    place, prefix = _place(project, implementation_id)
    relpath = _safe_folder(place, relpath)
    folder = '/'.join(part for part in (prefix, '' if relpath == '.' else relpath) if part)
    own = revision.entries_for_place('implementation', implementation_id)
    if own is None:
        raise ProjectError('implementation_not_found')
    # Inventory never follows symlinks and records the folder itself only for a
    # non-root folder.
    if relpath != '.':
        entry = next((entry for entry in own if entry.path == folder), None)
        if entry is not None and entry.type == 'file':
            return (replace(entry, path='.'),)
        if entry is None or entry.type != 'directory':
            return None
    materials = Path(project.working_copy.path) / project.materials_root
    cut = _nested_cut(place, prefix, materials)
    begin = folder + '/' if folder else ''
    entries = tuple(replace(entry, path=entry.path[len(begin):]) for entry in own
                    if entry.path.startswith(begin) and entry.path != folder and
                    (cut is None or (entry.path != cut and not entry.path.startswith(cut + '/'))))
    return entries or None


def tree_digest(entries: tuple[Entry, ...]) -> str:
    payload = [(entry.path, entry.type, entry.executable, entry.sha256, entry.target)
               for entry in sorted(entries, key=lambda item: item.path)]
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def text_file(root: Path, relative: Path) -> bool:
    """Classify an inventoried file without following symlinks in its path."""
    import os
    import stat
    opened = []
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(root, flags | os.O_DIRECTORY)
        opened.append(descriptor)
        parts = relative.parts
        for part in parts[:-1]:
            descriptor = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            opened.append(descriptor)
        descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptor)
        opened.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                if b"\0" in block:
                    return False
                decoder.decode(block)
            decoder.decode(b"", final=True)
        return True
    except (OSError, UnicodeDecodeError, IndexError):
        return False
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
