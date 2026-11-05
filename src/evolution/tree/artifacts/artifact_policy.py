"""Per-root artifact membership, one pattern language and canonical digests."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".env",
    ".env.*",
    "*.log",
)
ARTIFACT_DOMAIN = b"evolution-artifact-set-v1\0"
DEPENDENCY_DOMAIN = b"evolution-dependency-set-v1\0"


def validate_pattern(pattern: str) -> None:
    """Refuse a pattern outside the artifact path-rule grammar: empty, absolute,
    negated, or with an empty, '.' or '..' segment.
    """
    parts = pattern.split("/")
    if not pattern or pattern.startswith(("/", "!")) or any(p in ("", "..", ".") for p in parts):
        raise ValueError(f"invalid pattern: {pattern!r}")


def _segment_match(pattern_parts: list[str], path_parts: list[str]) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *rest = pattern_parts
    if head == "**":
        return any(_segment_match(rest, path_parts[i:]) for i in range(len(path_parts) + 1))
    return (
        bool(path_parts)
        and fnmatch.fnmatchcase(path_parts[0], head)
        and _segment_match(rest, path_parts[1:])
    )


def matches(pattern: str, relative: PurePosixPath, is_dir: bool) -> bool:
    """gitignore-like: no '/' matches the basename at any depth; with '/' the
    pattern is anchored at the copy root and matched segment-wise, with '**'
    for zero or more segments. An anchored pattern matches the path itself or
    any ancestor of it, so matching a directory also covers its subtree.
    """
    parts = list(relative.parts)
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    pattern_parts = pattern.split("/")
    return any(_segment_match(pattern_parts, parts[:i]) for i in range(1, len(parts) + 1))


@dataclass(frozen=True)
class ArtifactPolicyV1:
    excludes: tuple[str, ...]
    dependency_paths: tuple[str, ...]
    writable_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("excludes", "dependency_paths", "writable_paths"):
            values = tuple(getattr(self, field))
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate {field}")
            for value in values:
                validate_pattern(value)
            object.__setattr__(self, field, values)
        for dep in self.dependency_paths:
            if any(ch in dep for ch in "*?[") or self.is_excluded(PurePosixPath(dep), False):
                raise ValueError(f"dependency path is not a plain included file: {dep}")
        for w in self.writable_paths:
            if any(matches(w, PurePosixPath(dep), False) for dep in self.dependency_paths):
                raise ValueError(f"writable path matches a dependency path: {w}")
            if w in self.effective_excludes or any(
                matches(e, PurePosixPath(w.replace("**", "x")), False)
                for e in self.effective_excludes
            ):
                raise ValueError(f"writable path matches an exclusion: {w}")

    @property
    def effective_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES + tuple(e for e in self.excludes if e not in DEFAULT_EXCLUDES)

    def is_excluded(self, relative: PurePosixPath, is_dir: bool) -> bool:
        return any(matches(p, relative, is_dir) for p in self.effective_excludes)

    def is_writable(self, relative: PurePosixPath) -> bool:
        return any(matches(p, relative, False) for p in self.writable_paths)

    def to_payload(self) -> dict[str, Any]:
        return dict(
            version=1,
            excludes=list(self.excludes),
            dependency_paths=list(self.dependency_paths),
            writable_paths=list(self.writable_paths),
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ArtifactPolicyV1:
        if set(payload) != {"version", "excludes", "dependency_paths", "writable_paths"}:
            raise ValueError("policy payload keys")
        if payload["version"] != 1:
            raise ValueError("policy version")
        return cls(
            tuple(payload["excludes"]),
            tuple(payload["dependency_paths"]),
            tuple(payload["writable_paths"]),
        )

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Entry:
    path: str
    kind: Literal["file", "directory"]
    executable: bool
    size: int


@dataclass(frozen=True)
class TreeScan:
    artifact: tuple[Entry, ...]
    dependency: tuple[Entry, ...]
    candidate_digest: str
    dependency_digest: str
    file_count: int
    byte_count: int


def tree_inodes(scan: TreeScan) -> int:
    """F of a scanned tree: its files and directories, plus the copy's root."""

    return len(scan.artifact) + len(scan.dependency) + 1


def _feed(digest: Any, entry: Entry, content: bytes) -> None:
    for value in (
        entry.path.encode(),
        entry.kind.encode(),
        b"x" if entry.executable else b"-",
        content,
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _digest(domain: bytes, root: Path, entries: tuple[Entry, ...]) -> str:
    digest = hashlib.sha256(domain)
    for entry in entries:
        content = (root / entry.path).read_bytes() if entry.kind == "file" else b""
        _feed(digest, entry, content)
    return digest.hexdigest()


EMPTY_DEPENDENCY_DIGEST = hashlib.sha256(DEPENDENCY_DOMAIN).hexdigest()


def reraise_walk_error(exc: OSError) -> None:
    """`os.walk` `onerror` hook: an unreadable directory must fail the walk.

    By default `os.walk` swallows the error and simply yields a smaller tree, so
    a root holding one unreadable *included* directory would scan, digest and
    publish fewer entries under an identity that claims to be exact. Every caller
    of a walk here already translates `OSError` at its own boundary (`io_failure`
    for the store's scans, `unreadable` for inspection), so re-raising turns a
    silent truncation into that boundary.
    """

    raise exc


def entry_set(root: Path) -> frozenset[str]:
    """Every entry under `root` (files and directories) as root-relative POSIX paths.

    Unlike `scan_tree` this applies no policy: it is the on-disk truth a caller
    compares against `artifact_set ∪ dependency_set` when the *whole* tree must be
    exactly what the copy would have written (destination reuse, published-copy
    verification), since the digests cover only what the policy includes.
    """

    entries: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=reraise_walk_error
    ):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            entries.add((base / name).relative_to(root).as_posix())
    return frozenset(entries)


def scan_tree(root: Path, policy: ArtifactPolicyV1) -> TreeScan:
    """Walk the non-excluded entries under `root`, checking the safe-tree contract.

    Exclusion is tested first: an excluded entry is skipped and an excluded
    directory is pruned from the walk, so it is never entered and nothing inside
    it is stat'ed. The safe-tree checks (no symlink, no special file, no
    hard-link alias) therefore apply to every non-excluded entry only. This
    changes no digest — an excluded entry was never in either set — and
    containment still holds: an excluded entry is neither copied nor digested,
    and a hard-link alias between an excluded file and an included one is caught
    by the included file's own `st_nlink`. Scanning into the default exclusions
    would instead make every real tree unsafe, since `.venv` and `node_modules`
    routinely hold symlinks and hard links. An unreadable *included* directory
    fails the walk instead of being silently skipped (`reraise_walk_error`): the
    alternative is a truncated scan whose digests claim an exact identity for a
    tree that was never fully read.
    """
    if root.is_symlink() or not root.is_dir():
        raise ArtifactUnsafeError("symlink" if root.is_symlink() else "special_file")
    root = root.resolve(strict=True)
    artifact: list[Entry] = []
    dependency: dict[str, Entry] = {}
    deps = set(policy.dependency_paths)
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=reraise_walk_error
    ):
        base = Path(dirpath)
        relatives = {
            name: PurePosixPath((base / name).relative_to(root).as_posix())
            for name in dirnames + filenames
        }
        # Pruned in place, so `os.walk` never descends into an excluded directory.
        dirnames[:] = sorted(n for n in dirnames if not policy.is_excluded(relatives[n], True))
        included = dirnames + [n for n in filenames if not policy.is_excluded(relatives[n], False)]
        for name in sorted(included):
            full = base / name
            info = full.lstat()
            relative = relatives[name]
            if stat.S_ISLNK(info.st_mode):
                raise ArtifactUnsafeError("symlink")
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and not stat.S_ISREG(info.st_mode):
                raise ArtifactUnsafeError("special_file")
            if not is_dir and info.st_nlink != 1:
                raise ArtifactUnsafeError("hard_link")
            entry = Entry(
                str(relative),
                "directory" if is_dir else "file",
                bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)) and not is_dir,
                0 if is_dir else info.st_size,
            )
            if str(relative) in deps:
                if is_dir:
                    raise ArtifactUnsafeError("dependency_missing")
                dependency[str(relative)] = entry
            else:
                artifact.append(entry)
    missing = deps - set(dependency)
    if missing:
        raise ArtifactUnsafeError("dependency_missing")
    artifact_entries = tuple(sorted(artifact, key=lambda e: e.path))
    dependency_entries = tuple(dependency[p] for p in sorted(dependency))
    return TreeScan(
        artifact_entries,
        dependency_entries,
        _digest(ARTIFACT_DOMAIN, root, artifact_entries),
        _digest(DEPENDENCY_DOMAIN, root, dependency_entries),
        sum(1 for e in artifact_entries + dependency_entries if e.kind == "file"),
        sum(e.size for e in artifact_entries + dependency_entries),
    )


def changes(
    parent: TreeScan, child: TreeScan, root_parent: Path, root_child: Path
) -> tuple[str, ...]:
    """Paths present in exactly one artifact set, or differing in kind, exec flag or bytes."""
    left = {e.path: e for e in parent.artifact}
    right = {e.path: e for e in child.artifact}
    changed = set(left) ^ set(right)
    for path in set(left) & set(right):
        a, b = left[path], right[path]
        if (a.kind, a.executable) != (b.kind, b.executable):
            changed.add(path)
        elif (
            a.kind == "file"
            and (root_parent / path).read_bytes() != (root_child / path).read_bytes()
        ):
            changed.add(path)
    return tuple(sorted(changed))
