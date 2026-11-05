"""Immutable auxiliary data, distinct from executable trees (ADR-0006 amendment).

The artifact lifecycle proves declaration and publication order. These helpers
verify local bytes and ledger references. The campaign caller and audit must prove
that declaration_ref exists in this run's campaign, is family 15, and authorizes
this exact purpose/content. As with SelectionDecision, no campaign reader or
campaign policy is inferred by the artifact store. Format-specific semantic
validation (weights, slots, diagnostic sections) belongs to that caller.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError, ArtifactUnsafeError
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.storage.append_log import _directory
from evolution.tree.storage.envelope import _identifier, _integer, _keys, _require
from evolution.tree.storage.journal_errors import JournalFault

_FORMATS = {
    "comparison": ("comparison-data/1", ("payload.json",), "artifacts/auxiliary"),
    "baseline_diagnostics": (
        "baseline-diagnostic-bundle/1",
        ("index.json", "payload", "gaps.json"),
        "diagnostic-imports",
    ),
}
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_LOCATOR = re.compile(r"(campaign|artifacts)\.jsonl#bytes=(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")
_CHUNK = 64 * 1024


def _sha(value: object) -> None:
    _require(isinstance(value, str) and _SHA.fullmatch(value) is not None)


@dataclass(frozen=True)
class AuxiliarySourceReference:
    """A bounded campaign or ledger byte reference; its target must be audited separately."""

    version: int
    kind: str
    run_id: str
    locator: str
    content_digest: str

    def __post_init__(self) -> None:
        _require(type(self.version) is int and self.version == 1)
        _require(self.kind in ("journal_record", "ledger_record"))
        _identifier(self.run_id)
        _sha(self.content_digest)
        _require(isinstance(self.locator, str) and len(self.locator) <= 512)
        match = _LOCATOR.fullmatch(self.locator)
        _require(match is not None)
        assert match is not None
        _require(int(match[2]) < int(match[3]))
        _require((match[1] == "campaign") == (self.kind == "journal_record"))

    def to_payload(self) -> dict[str, Any]:
        return dict(
            version=self.version,
            kind=self.kind,
            run_id=self.run_id,
            locator=self.locator,
            content_digest=self.content_digest,
        )

    @classmethod
    def from_payload(cls, value: Any) -> AuxiliarySourceReference:
        p = _keys(value, {"version", "kind", "run_id", "locator", "content_digest"})
        return cls(**p)


@dataclass(frozen=True)
class AuxiliaryFile:
    name: str
    byte_count: int
    digest: str

    def __post_init__(self) -> None:
        _require(self.name in ("payload.json", "index.json", "payload", "gaps.json"))
        _integer(self.byte_count)
        _sha(self.digest)

    def to_payload(self) -> dict[str, Any]:
        return dict(name=self.name, byte_count=self.byte_count, digest=self.digest)

    @classmethod
    def from_payload(cls, value: Any) -> AuxiliaryFile:
        return cls(**_keys(value, {"name", "byte_count", "digest"}))


@dataclass(frozen=True)
class AuxiliaryDeclaration:
    """Exact caller-declared bytes. No paths, source locations, or large lists.

    Comparison content identity is SHA256(payload.json). Diagnostic identity is
    SHA256(index.json), whose section hashes are format authority; all three file
    hashes are additionally bound here, including payload framing and gaps.
    """

    family_version: int
    purpose: str
    format: str
    content_digest: str
    files: tuple[AuxiliaryFile, ...]
    declaration_ref: AuxiliarySourceReference

    def __post_init__(self) -> None:
        _require(type(self.family_version) is int and self.family_version == 15)
        _require(isinstance(self.purpose, str) and self.purpose in _FORMATS)
        format_name, names, _ = _FORMATS[self.purpose]
        _require(self.format == format_name)
        _require(
            type(self.files) is tuple and all(isinstance(f, AuxiliaryFile) for f in self.files)
        )
        _require(tuple(f.name for f in self.files) == names)
        _sha(self.content_digest)
        _require(self.content_digest == self.files[0].digest)
        _require(isinstance(self.declaration_ref, AuxiliarySourceReference))
        _require(self.declaration_ref.kind == "journal_record")
        if self.purpose == "baseline_diagnostics":
            _require(self.byte_count <= 256 * 1024 * 1024)
            _require(self.files[2].byte_count <= 64 * 1024)

    @property
    def byte_count(self) -> int:
        """Diagnostic index+payload only; gaps have their separate bound."""
        return sum(f.byte_count for f in self.files if f.name != "gaps.json")

    @property
    def locator(self) -> str:
        return f"{_FORMATS[self.purpose][2]}/{self.content_digest}"

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return dict(
            family_version=self.family_version,
            purpose=self.purpose,
            format=self.format,
            content_digest=self.content_digest,
            files=[f.to_payload() for f in self.files],
            declaration_ref=self.declaration_ref.to_payload(),
        )

    @classmethod
    def from_payload(cls, value: Any) -> AuxiliaryDeclaration:
        p = _keys(
            value,
            {"family_version", "purpose", "format", "content_digest", "files", "declaration_ref"},
        )
        _require(isinstance(p["files"], list) and len(p["files"]) in (1, 3))
        return cls(
            p["family_version"],
            p["purpose"],
            p["format"],
            p["content_digest"],
            tuple(AuxiliaryFile.from_payload(f) for f in p["files"]),
            AuxiliarySourceReference.from_payload(p["declaration_ref"]),
        )


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    try:
        fd = _directory(path)
    except JournalFault:
        raise ArtifactUnsafeError("foreign_owner") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ArtifactUnsafeError("escape") from None
        raise
    try:
        yield fd
    finally:
        os.close(fd)


def _file_digest(
    directory: int, name: str, *, expected: AuxiliaryFile | None = None, output: int | None = None
) -> AuxiliaryFile:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ArtifactUnsafeError("symlink") from None
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactUnsafeError("special_file")
        if before.st_nlink != 1:
            raise ArtifactUnsafeError("hard_link")
        if before.st_uid != os.geteuid():
            raise ArtifactUnsafeError("foreign_owner")
        if expected is not None and before.st_size != expected.byte_count:
            raise ArtifactUnsafeError("source_changed")
        digest = hashlib.sha256()
        count = 0
        while count <= before.st_size:
            raw = os.read(fd, min(_CHUNK, before.st_size + 1 - count))
            if not raw:
                break
            count += len(raw)
            digest.update(raw)
            if output is not None:
                view = memoryview(raw)
                while view:
                    written = os.write(output, view)
                    view = view[written:]
        after = os.fstat(fd)
        if count != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ArtifactUnsafeError("source_changed")
        actual = AuxiliaryFile(name, count, digest.hexdigest())
        if expected is not None and actual != expected:
            raise ArtifactUnsafeError("source_changed")
        return actual
    finally:
        os.close(fd)


def _check_files(directory: int, names: tuple[str, ...]) -> None:
    if set(os.listdir(directory)) != set(names):
        raise ArtifactUnsafeError("artifact_unsafe")


def describe_auxiliary(
    directory: Path, *, purpose: str, declaration_ref: AuxiliarySourceReference
) -> AuxiliaryDeclaration:
    """Read-only streaming descriptor; caller supplies canonical, validated format.

    No publication or campaign authority is created. Comparison JSON semantics and
    canonical encoding are the producer's responsibility, like diagnostic section
    validation. This layer binds exact bytes without an extra basket-size limit.
    """
    files = describe_auxiliary_files(directory, purpose=purpose)
    return AuxiliaryDeclaration(
        15, purpose, _FORMATS[purpose][0], files[0].digest, files, declaration_ref
    )


def describe_auxiliary_files(directory: Path, *, purpose: str) -> tuple[AuxiliaryFile, ...]:
    """Describe exact bytes before the campaign writes its durable declaration.

    The caller can bind these descriptors and files[0].digest in its campaign
    event, then construct AuxiliaryDeclaration with that event's real reference.
    This avoids self-referential hashes and synthetic placeholder references.
    """
    _require(isinstance(purpose, str) and purpose in _FORMATS)
    _, names, _ = _FORMATS[purpose]
    with _open_directory(directory) as fd:
        _check_files(fd, names)
        files = tuple(_file_digest(fd, name) for name in names)
        _check_files(fd, names)
    if purpose == "baseline_diagnostics":
        _require(sum(f.byte_count for f in files[:2]) <= 256 * 1024 * 1024)
        _require(files[2].byte_count <= 64 * 1024)
    return files


def verify_directory(
    directory: Path, declaration: AuxiliaryDeclaration, *, committed: bool
) -> None:
    """Verify every byte, including diagnostic payload framing and gaps."""
    try:
        with _open_directory(directory) as fd:
            _check_files(fd, tuple(f.name for f in declaration.files))
            for entry in declaration.files:
                _file_digest(fd, entry.name, expected=entry)
            _check_files(fd, tuple(f.name for f in declaration.files))
    except FileNotFoundError:
        if committed:
            raise ArtifactCorruptionError("artifact_missing") from None
        raise
    except ArtifactUnsafeError as exc:
        if committed:
            code = "artifact_digest_mismatch" if exc.code == "source_changed" else "artifact_unsafe"
            raise ArtifactCorruptionError(code) from None
        raise


def destination_present(run_root: Path, declaration: AuxiliaryDeclaration) -> bool:
    """Observe a destination through no-follow ancestors, including absent roots."""
    try:
        with _open_directory(run_root / declaration.locator):
            return True
    except FileNotFoundError:
        return False


def finish_directory(run_root: Path, declaration: AuxiliaryDeclaration) -> None:
    """Complete the freeze/fsync barrier of an already-renamed owed publication."""
    verify_directory(run_root / declaration.locator, declaration, committed=True)
    with _open_directory(run_root / declaration.locator) as fd:
        for entry in declaration.files:
            file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.fchmod(file_fd, 0o400)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        os.fchmod(fd, 0o500)
        os.fsync(fd)
    with _open_directory((run_root / declaration.locator).parent) as fd:
        os.fsync(fd)


def publish_directory(run_root: Path, source: Path, declaration: AuxiliaryDeclaration) -> None:
    """Copy fixed files with bounded buffers, fsync, then atomic directory rename.

    The store has already durably declared this operation and owns the lease.
    Existing destinations are never replaced. Staging belongs to ordinary store
    cleanup, and can be regenerated only against the unchanged declaration.
    """
    parent_parts = declaration.locator.split("/")[:-1]
    with _open_directory(run_root) as root_fd:
        parent_fd = os.dup(root_fd)
        try:
            for name in parent_parts:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
                try:
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ArtifactUnsafeError("escape") from None
                    raise
                os.close(parent_fd)
                parent_fd = child
                if os.fstat(child).st_uid != os.geteuid():
                    raise ArtifactUnsafeError("foreign_owner")
            with _open_directory(run_root / "artifacts/.staging") as staging_fd:
                name = ".auxiliary." + uuid4().hex
                os.mkdir(name, mode=0o700, dir_fd=staging_fd)
                staged_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=staging_fd
                )
                try:
                    with _open_directory(source) as source_fd:
                        _check_files(source_fd, tuple(f.name for f in declaration.files))
                        for entry in declaration.files:
                            output = os.open(
                                entry.name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=staged_fd,
                            )
                            try:
                                _file_digest(source_fd, entry.name, expected=entry, output=output)
                                os.fchmod(output, 0o400)
                                os.fsync(output)
                            finally:
                                os.close(output)
                        _check_files(source_fd, tuple(f.name for f in declaration.files))
                    os.fsync(staged_fd)
                    try:
                        os.stat(declaration.content_digest, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ArtifactUnsafeError("destination_exists")
                    os.rename(
                        name,
                        declaration.content_digest,
                        src_dir_fd=staging_fd,
                        dst_dir_fd=parent_fd,
                    )
                    os.fchmod(staged_fd, 0o500)
                    os.fsync(staged_fd)
                    os.fsync(parent_fd)
                    os.fsync(staging_fd)
                finally:
                    os.close(staged_fd)
                    # Unpublished temporary files have no authority. Store startup
                    # also clears them after a process crash.
                    try:
                        cleanup = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=staging_fd
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        try:
                            for entry_name in os.listdir(cleanup):
                                os.unlink(entry_name, dir_fd=cleanup)
                        finally:
                            os.close(cleanup)
                        os.rmdir(name, dir_fd=staging_fd)
        finally:
            os.close(parent_fd)
