"""Bounded provenance inventory for declared environment documentation."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path


MAX_DOCUMENT_FILES = 4096
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_READ_SIZE = 64 * 1024


class DocumentInputsError(ValueError):
    """The complete documentation tree cannot be captured safely."""


@dataclass(frozen=True)
class DocumentFile:
    path: Path
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class DocumentInputs:
    root: Path
    files: tuple[DocumentFile, ...]
    digest: str


def _validate_name(name: str) -> None:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DocumentInputsError() from exc


def _read_file(path: Path, remaining: int) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DocumentInputsError() from exc
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    size = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > remaining:
            raise DocumentInputsError()
        while True:
            chunk = os.read(descriptor, min(_READ_SIZE, remaining - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > remaining:
                raise DocumentInputsError()
            digest.update(chunk)
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise DocumentInputsError() from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def capture_document_inputs(root: Path) -> DocumentInputs:
    """Capture every regular UTF-8 file under one real, nonempty directory."""
    declared = Path(root)
    try:
        root_info = declared.lstat()
    except OSError as exc:
        raise DocumentInputsError() from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise DocumentInputsError()
    try:
        resolved = declared.resolve(strict=True)
    except OSError as exc:
        raise DocumentInputsError() from exc

    pending = [resolved]
    found: list[tuple[Path, str]] = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError as exc:
            raise DocumentInputsError() from exc
        for entry in entries:
            _validate_name(entry.name)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DocumentInputsError() from exc
            path = Path(entry.path)
            if stat.S_ISLNK(info.st_mode):
                raise DocumentInputsError()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise DocumentInputsError()
            relative = path.relative_to(resolved).as_posix()
            _validate_name(relative)
            found.append((path, relative))
            if len(found) > MAX_DOCUMENT_FILES:
                raise DocumentInputsError()

    if not found:
        raise DocumentInputsError()

    total = 0
    files: list[DocumentFile] = []
    for path, relative in sorted(found, key=lambda item: item[1]):
        sha256, size = _read_file(path, MAX_DOCUMENT_BYTES - total)
        total += size
        files.append(DocumentFile(path.resolve(strict=True), relative, sha256, size))

    payload = {
        "schema": "lab-doc-inputs/1",
        "files": [
            {"path": item.relative_path, "sha256": item.sha256} for item in files
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return DocumentInputs(resolved, tuple(files), hashlib.sha256(encoded).hexdigest())
