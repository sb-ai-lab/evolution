"""No-clobber publication of one explicitly requested download: a verified export ZIP or report.md."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets

from evolution_lab.durability import sync_file


def publish_bytes(output: Path | str, data: bytes) -> dict[str, object]:
    """A new file with mode 0600 through a private temporary file and ``link``; an existing path is refused
    (``FileExistsError``) and left untouched."""
    path = Path(output)
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.partial")
    descriptor = None
    owned = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owned = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            sync_file(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if owned:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise
    return {"path": str(path), "bytes": len(data)}


def publish_export(output: Path | str, data: bytes, export_id: str) -> dict[str, object]:
    if not Path(output).is_absolute():
        raise ValueError("output path must be absolute")
    if hashlib.sha256(data).hexdigest() != export_id:
        raise ValueError("export digest mismatch")
    return {"export_id": export_id, "sha256": export_id, **publish_bytes(output, data)}
