"""Stage, freeze and durably publish artifact trees (S2 copy engine)."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from evolution.lineage.state_io import fsync_directory
from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import (
    ArtifactPolicyV1,
    Entry,
    TreeScan,
    entry_set,
    reraise_walk_error,
    scan_tree,
)

_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class StagedCopy:
    root: Path
    scan: TreeScan
    frozen: bool


def _same_tree(actual: TreeScan, expected: TreeScan) -> bool:
    """Full scan equality: both digests and both counts."""

    return (
        actual.candidate_digest == expected.candidate_digest
        and actual.dependency_digest == expected.dependency_digest
        and actual.file_count == expected.file_count
        and actual.byte_count == expected.byte_count
    )


def _copy_and_verify(
    source: Path, destination: Path, expected: TreeScan, policy: ArtifactPolicyV1, *, code: str
) -> TreeScan:
    """Copy exactly `expected.artifact ∪ expected.dependency` from `source` into the
    existing empty `destination`, re-scan it, and require both the copy and the
    source (re-scanned after the copy) to equal `expected`; a shape change during
    the copy or a mismatch raises `ArtifactUnsafeError(code)`. Returns the copy's scan.

    The staged tree can reproduce `expected` while the *source* no longer does:
    an included file added after the caller's pre-scan is simply never copied,
    and one removed from an included directory leaves that directory copied but
    empty, so neither shows up in the staged scan. Re-scanning the source is the
    only check that sees them, and it closes the copy over the same
    untrusted-source contract the pre-scan opened.
    """

    directories = tuple(
        sorted(
            (e for e in (expected.artifact + expected.dependency) if e.kind == "directory"),
            key=lambda e: e.path.count("/"),
        )
    )
    files = tuple(e for e in (expected.artifact + expected.dependency) if e.kind == "file")
    try:
        _copy_entries(source, destination, directories)
        _copy_entries(source, destination, files)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise ArtifactUnsafeError(code) from exc
    destination_scan = scan_tree(destination, policy)
    if not _same_tree(destination_scan, expected) or not _same_tree(
        scan_tree(source, policy), expected
    ):
        raise ArtifactUnsafeError(code)
    return destination_scan


def _copy_entries(source: Path, destination: Path, entries: tuple[Entry, ...]) -> None:
    for entry in entries:
        target = destination / PurePosixPath(entry.path)
        if entry.kind == "directory":
            target.mkdir(mode=0o700)
        else:
            shutil.copy2(source / entry.path, target)


def stage_copy(
    source: Path,
    expected: TreeScan,
    policy: ArtifactPolicyV1,
    staging_dir: Path,
    *,
    freeze: bool,
) -> StagedCopy:
    """Copy exactly `expected.artifact ∪ expected.dependency` into a fresh
    staging root under `staging_dir`, re-scan it, and require the recomputed
    digests to equal `expected`. Any failure removes the staging root before
    re-raising.
    """

    staging_dir.mkdir(mode=0o700, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(prefix=".copy.", dir=staging_dir))
    try:
        staged_scan = _copy_and_verify(
            source, staged_root, expected, policy, code="artifact_unsafe"
        )
        if freeze:
            _set_frozen(staged_root)
        return StagedCopy(staged_root, staged_scan, freeze)
    except BaseException:
        remove_tree(staged_root)
        raise


def snapshot_tree(source: Path, policy: ArtifactPolicyV1, destination: Path) -> TreeScan:
    """Copy exactly the artifact and dependency entries of `source` under
    `policy` into a fresh `destination`, freeze it root included, and return
    the destination's scan — which must equal the source's before and after
    the copy (`source_changed` otherwise, and the destination is removed).

    The target server's private snapshot (ADR-0009): the digests it attests
    are this scan's, and the backend reads only from `destination`.
    """

    expected = scan_tree(source, policy)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        copied = _copy_and_verify(source, destination, expected, policy, code="source_changed")
        _set_frozen(destination)
        _clear_root_write_bits(destination)
        return copied
    except BaseException:
        remove_tree(destination)
        raise


def _set_frozen(root: Path) -> None:
    # `root` itself keeps its write bit: `publish` may still need to
    # `os.replace` it into a destination under a different parent directory,
    # which requires write permission on the moved directory to update its
    # ".." entry. Every entry strictly inside it is frozen, which is what the
    # digest and tamper checks actually protect.
    for entry in root.rglob("*"):
        mode = stat.S_IMODE(entry.lstat().st_mode)
        os.chmod(entry, mode & ~_WRITE_BITS)


def fsync_tree(root: Path) -> None:
    """fsync every regular file and every directory of `root`, bottom-up."""

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, topdown=False, onerror=reraise_walk_error
    ):
        base = Path(dirpath)
        for name in filenames:
            _fsync_path(base / name, os.O_RDONLY | _NO_FOLLOW)
        for name in dirnames:
            _fsync_path(base / name, os.O_RDONLY | os.O_DIRECTORY | _NO_FOLLOW)
    _fsync_path(root, os.O_RDONLY | os.O_DIRECTORY | _NO_FOLLOW)


def _fsync_path(path: Path, flags: int) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clear_root_write_bits(root: Path) -> None:
    mode = stat.S_IMODE(root.lstat().st_mode)
    os.chmod(root, mode & ~_WRITE_BITS)


def publish(
    staged: StagedCopy, destination: Path, expected: TreeScan, policy: ArtifactPolicyV1
) -> Literal["published", "reused"]:
    """Durably publish `staged.root` to `destination`, or reuse an existing,
    identical destination. Never deletes a destination.
    """

    if not destination.exists() and not destination.is_symlink():
        try:
            fsync_tree(staged.root)
            os.replace(staged.root, destination)
        except BaseException:
            # Nothing durable has happened yet: `staged.root` is still the
            # only copy (or, if `os.replace` itself half-failed, still exists
            # at its staging path on every POSIX platform this runs on).
            remove_tree(staged.root)
            raise
        # Past this point `staged.root` no longer exists (it *is* now
        # `destination`), so a later failure here must not attempt to remove
        # it: there is nothing left to clean up, and `destination` is not
        # ours to delete (a `finished` record, not this function, decides
        # whether it is published).
        if staged.frozen:
            # `stage_copy` left the root itself writable so this `os.replace`
            # could update its ".." entry; now that it is at rest, close that
            # gap and fold the new mode into the same durability barrier.
            _clear_root_write_bits(destination)
            _fsync_path(destination, os.O_RDONLY | os.O_DIRECTORY | _NO_FOLLOW)
        fsync_directory(destination.parent)
        return "published"

    details = destination.lstat()
    if (
        stat.S_ISDIR(details.st_mode)
        and not destination.is_symlink()
        and details.st_uid == os.geteuid()
    ):
        try:
            destination_scan = scan_tree(destination, policy)
            # The digests cover only what the policy includes, so a destination that
            # also carries an *excluded* entry (a stray `.env`, a leftover
            # `__pycache__`) hashes exactly like the expectation while being a
            # different tree. Reuse adopts those bytes under this artifact's identity,
            # so the whole on-disk entry set must be what the copy would have written.
            reusable = entry_set(destination) == {
                entry.path for entry in expected.artifact + expected.dependency
            }
        except ArtifactUnsafeError:
            destination_scan, reusable = None, False
        if (
            destination_scan is not None
            and reusable
            and destination_scan.candidate_digest == expected.candidate_digest
            and destination_scan.dependency_digest == expected.dependency_digest
        ):
            remove_tree(staged.root)
            if staged.frozen:
                # Reuse adopts bytes this call did not write, so the destination's
                # modes are whatever an earlier publisher (or anything that touched
                # it since) left behind: clearing the root alone would publish a
                # tree holding a writable file under a frozen name. Freeze the whole
                # reused tree, root included, and do it *before* the barrier so the
                # new modes are as durable as the bytes they protect.
                _set_frozen(destination)
                _clear_root_write_bits(destination)
            fsync_tree(destination)
            fsync_directory(destination.parent)
            return "reused"

    remove_tree(staged.root)
    raise ArtifactUnsafeError("destination_exists")


def restore_writable(root: Path) -> None:
    """Add S_IWUSR to every entry of `root` (workspace materialization)."""

    for entry in (root, *root.rglob("*")):
        mode = stat.S_IMODE(entry.lstat().st_mode)
        os.chmod(entry, mode | stat.S_IWUSR)


def _clear_obstruction(func: Callable[[str], object], path: str, excinfo: object) -> None:
    """`shutil.rmtree` `onerror` hook: a frozen (write-bit-cleared) tree needs
    its *parent* directory writable to unlink or rmdir an entry, not the entry
    itself. Restore that, then retry the original operation.
    """

    parent = os.path.dirname(path) or "."
    try:
        info = os.lstat(parent)
        os.chmod(parent, stat.S_IMODE(info.st_mode) | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass
    func(path)


def remove_tree(root: Path) -> None:
    """Remove `root` without following symlinks inside it; ignore a missing root.

    Defence in depth against a caller passing a path whose last component is
    `.`, `..`, or empty: `shutil.rmtree` would happily delete `root`'s parent
    (or `root` itself, relabelled) wholesale rather than the intended
    directory. Checked by name, before any `lstat`, so it applies even to a
    root that does not exist.
    """

    if root.name in ("", ".", ".."):
        raise ArtifactUnsafeError("escape")
    try:
        details = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ArtifactUnsafeError("special_file")
    shutil.rmtree(root, ignore_errors=False, onerror=_clear_obstruction)


def map_os_error(exc: OSError) -> str:
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return "storage_capacity_exhausted"
    return "io_failure"


def check_destination_parent(parent: Path, run_root: Path) -> None:
    """Require `parent` to be a strict child of `run_root / "artifacts"`, with
    no symlink component (relative to `run_root`) and every existing
    component owned by the caller's uid.
    """

    artifacts_root = (run_root / "artifacts").resolve(strict=False)
    resolved = parent.resolve(strict=False)
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise ArtifactUnsafeError("escape")

    try:
        relative = parent.relative_to(run_root)
    except ValueError as exc:
        raise ArtifactUnsafeError("escape") from exc

    current = run_root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactUnsafeError("escape")
        if info.st_uid != os.geteuid():
            raise ArtifactUnsafeError("foreign_owner")
