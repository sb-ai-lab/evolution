"""Verify selected Candidate files with shared integrity errors and digest rules."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class ArtifactError(RuntimeError):
    code: str

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SnapshotError(ValueError):
    """A snapshot source is unsafe or a destination is not fresh."""


def has_hidden_path_segment(relative: str) -> bool:
    """Return whether a relative POSIX path includes a dot-prefixed segment."""

    return any(part.startswith(".") for part in PurePosixPath(relative).parts)


def artifact_digest(root: Path, paths: Iterable[str]) -> str:
    """Digest the regular files selected by the manifest's path patterns."""

    if root.is_symlink():
        raise SnapshotError(f"artifact root must not be a symlink: {root}")
    source = root.resolve(strict=True)
    _assert_regular_tree(source)
    patterns = tuple(sorted(set(paths)))
    if not patterns:
        raise SnapshotError("artifact paths must not be empty")
    artifacts: set[Path] = set()
    for pattern in patterns:
        path = Path(pattern)
        if not pattern or path.is_absolute() or ".." in path.parts:
            raise SnapshotError(f"unsafe artifact path: {pattern}")
        matches = {
            match
            for match in source.glob(pattern)
            if not has_hidden_path_segment(match.relative_to(source).as_posix())
        }
        if not matches:
            raise SnapshotError(f"artifact path did not match a file: {pattern}")
        artifacts.update(matches)
    digest = hashlib.sha256(b"evolution-artifact-v1\0")
    for artifact in sorted(artifacts, key=lambda item: item.relative_to(source).as_posix()):
        relative = artifact.relative_to(source).as_posix()
        if artifact.is_symlink() or not artifact.is_file():
            raise SnapshotError(f"artifact must be a regular file: {relative}")
        encoded_path = relative.encode("utf-8")
        content = artifact.read_bytes()
        digest.update(encoded_path)
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _assert_regular_tree(root: Path) -> None:
    for entry in (root, *root.rglob("*")):
        status = entry.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise SnapshotError(f"snapshot tree contains symlink: {entry}")
        if not stat.S_ISDIR(status.st_mode) and not stat.S_ISREG(status.st_mode):
            raise SnapshotError(f"snapshot tree contains unsupported entry: {entry}")
