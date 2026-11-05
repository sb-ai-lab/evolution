"""Immutable run-owned copies of a target artefact tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.lineage.state_io import (
    PRIVATE_DIRECTORY_MODE,
    ensure_private_directory,
    fsync_directory,
)
from evolution.tree.execution.candidate_integrity import (
    SnapshotError,
    _assert_regular_tree,
)


@dataclass(frozen=True)
class Snapshot:
    version_id: str
    root: Path
    digest: str


def _validate_snapshot_id(snapshot_id: str) -> None:
    if (
        not snapshot_id
        or len(snapshot_id) > 80
        or not snapshot_id[0].isalnum()
        or any(not (character.isalnum() or character in "._-") for character in snapshot_id)
    ):
        raise SnapshotError(f"invalid snapshot ID: {snapshot_id!r}")


class SnapshotStore:
    """Create immutable version, candidate, and rejected snapshots for one run."""

    def __init__(self, run_root: Path) -> None:
        self._run_root = ensure_private_directory(run_root)

    def initialize(self, source_root: Path) -> Snapshot:
        source_digest = snapshot_content_digest(source_root)
        snapshot = self._copy(
            source_root, self._destination("versions", "v0"), "v0", frozen=True, reuse=True
        )
        if snapshot_content_digest(snapshot.root) != source_digest:
            raise SnapshotError("existing initial snapshot differs from seed source")
        return snapshot

    def candidate(self, parent: Snapshot, attempt_id: str) -> Snapshot:
        return self._copy(
            parent.root,
            self._destination("candidates", attempt_id),
            attempt_id,
            frozen=False,
        )

    def seal(self, candidate: Snapshot) -> Snapshot:
        """Make an edited Candidate read-only before evaluation."""

        _set_frozen(candidate.root, True)
        return Snapshot(candidate.version_id, candidate.root, snapshot_digest(candidate.root))

    def promote(self, candidate: Snapshot, version_id: str) -> Snapshot:
        if snapshot_digest(candidate.root) != candidate.digest:
            raise SnapshotError("candidate changed after sealing")
        promoted = self._copy(
            candidate.root,
            self._destination("versions", version_id),
            version_id,
            frozen=True,
            reuse=True,
        )
        if promoted.digest != candidate.digest:
            raise SnapshotError("existing promoted snapshot differs from sealed candidate")
        return promoted

    def reject(self, candidate: Snapshot, attempt_id: str) -> Snapshot:
        return self._copy(
            candidate.root,
            self._destination("rejected", attempt_id),
            attempt_id,
            frozen=True,
            reuse=True,
        )

    def _copy(
        self,
        source_root: Path,
        destination: Path,
        version_id: str,
        *,
        frozen: bool,
        reuse: bool = False,
    ) -> Snapshot:
        destination = self._checked_destination(destination, version_id)
        if source_root.is_symlink():
            raise SnapshotError(f"snapshot source must not be a symlink: {source_root}")
        source = source_root.resolve(strict=True)
        if not source.is_dir():
            raise SnapshotError("snapshot source must be a directory")
        resolved_destination = destination.resolve(strict=False)
        if resolved_destination.is_relative_to(source) or source.is_relative_to(
            resolved_destination
        ):
            raise SnapshotError("snapshot source and destination must not overlap")
        _assert_regular_tree(source)
        if destination.exists() or destination.is_symlink():
            details = destination.lstat()
            if not stat.S_ISDIR(details.st_mode) or destination.is_symlink():
                raise SnapshotError(f"snapshot destination is unsafe: {destination}")
            if details.st_uid != os.geteuid():
                raise SnapshotError(f"snapshot destination has a foreign owner: {destination}")
            if reuse:
                return Snapshot(version_id, destination, snapshot_digest(destination))
            raise SnapshotError(f"snapshot destination already exists: {destination}")
        self._ensure_destination_parent(destination.parent)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        try:
            shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
            _assert_regular_tree(staging)
            _set_frozen(staging, frozen)
            digest = snapshot_digest(staging)
            os.replace(staging, destination)
            fsync_directory(destination.parent)
            return Snapshot(version_id, destination, digest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def initial(self) -> Snapshot:
        """Return the immutable initial snapshot without rereading the seed source."""

        root = self._destination("versions", "v0")
        if not root.exists():
            raise SnapshotError("initial snapshot is missing")
        if root.is_symlink() or not root.is_dir():
            raise SnapshotError("initial snapshot is unsafe")
        return Snapshot("v0", root, snapshot_digest(root))

    def rejected(self, attempt_id: str) -> Snapshot:
        """Return an archived rejected Candidate used as a Working Draft."""

        root = self._destination("rejected", attempt_id)
        if not root.exists():
            raise SnapshotError(f"rejected snapshot {attempt_id} is missing")
        if root.is_symlink() or not root.is_dir():
            raise SnapshotError(f"rejected snapshot {attempt_id} is unsafe")
        return Snapshot(attempt_id, root, snapshot_digest(root))

    def _destination(self, group: str, snapshot_id: str) -> Path:
        _validate_snapshot_id(snapshot_id)
        return self._checked_destination(self._run_root / group / snapshot_id, snapshot_id)

    def _checked_destination(self, destination: Path, snapshot_id: str) -> Path:
        _validate_snapshot_id(snapshot_id)
        try:
            relative = destination.relative_to(self._run_root)
        except ValueError as exc:
            raise SnapshotError("snapshot destination must be inside the run root") from exc
        if len(relative.parts) != 2 or relative.name != snapshot_id:
            raise SnapshotError("snapshot destination must be a strict run child")
        parent = self._run_root / relative.parts[0]
        self._ensure_destination_parent(parent)
        return parent / snapshot_id

    def _ensure_destination_parent(self, parent: Path) -> None:
        try:
            details = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=PRIVATE_DIRECTORY_MODE)
            details = parent.lstat()
        if not stat.S_ISDIR(details.st_mode) or parent.is_symlink():
            raise SnapshotError(f"snapshot destination parent is unsafe: {parent}")
        if details.st_uid != os.geteuid():
            raise SnapshotError(f"snapshot destination parent has a foreign owner: {parent}")
        os.chmod(parent, PRIVATE_DIRECTORY_MODE)


def snapshot_digest(root: Path) -> str:
    """Digest paths, entry kinds, POSIX modes, and raw bytes in canonical order."""

    if root.is_symlink():
        raise SnapshotError(f"snapshot root must not be a symlink: {root}")
    tree = root.resolve(strict=True)
    _assert_regular_tree(tree)
    digest = hashlib.sha256()
    for entry in sorted(
        (tree, *tree.rglob("*")), key=lambda path: path.relative_to(tree).as_posix()
    ):
        relative = entry.relative_to(tree).as_posix() or "."
        mode = stat.S_IMODE(entry.stat().st_mode)
        if entry.is_dir():
            _feed(digest, relative, "directory", oct(mode), b"")
        else:
            _feed(digest, relative, "file", oct(mode), entry.read_bytes())
    return digest.hexdigest()


def snapshot_content_digest(root: Path) -> str:
    """Digest a regular tree's paths, kinds, and bytes without mutable mode bits."""

    if root.is_symlink():
        raise SnapshotError(f"snapshot root must not be a symlink: {root}")
    tree = root.resolve(strict=True)
    _assert_regular_tree(tree)
    digest = hashlib.sha256()
    for entry in sorted(
        (tree, *tree.rglob("*")), key=lambda path: path.relative_to(tree).as_posix()
    ):
        relative = entry.relative_to(tree).as_posix() or "."
        _feed(
            digest,
            relative,
            "directory" if entry.is_dir() else "file",
            "",
            entry.read_bytes() if entry.is_file() else b"",
        )
    return digest.hexdigest()


def _set_frozen(root: Path, frozen: bool) -> None:
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for entry in (root, *root.rglob("*")):
        mode = stat.S_IMODE(entry.stat().st_mode)
        os.chmod(entry, mode & ~write_bits if frozen else mode | stat.S_IWUSR)


def _feed(digest: Any, path: str, kind: str, mode: str, content: bytes) -> None:
    for value in (path.encode(), kind.encode(), mode.encode(), content):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
