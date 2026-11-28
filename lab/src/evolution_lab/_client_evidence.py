"""Private H1 execution evidence; never a campaign journal or owner record."""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from evolution_lab._client_models import ExecutionSnapshot


_RESULT_NAME = "h1-result.json"
_MAX_RESULT_BYTES = 65536
_RESULT_KEYS = frozenset({
    "schema", "operation_id", "state", "stage", "launched", "exit_code",
    "stop_requested", "stop_reason", "stop_effect", "core_force_requested",
    "cleanup_complete", "failure", "secondary_failures",
})
_STATES = frozenset({"exited", "stopped", "failed", "unknown"})
_STAGES = frozenset({
    "before_start", "spawning", "running", "stopping", "finalizing", "done",
})
_STOP_REASONS = frozenset({
    "user", "cancelled", "budget_exhausted", "dispatch_refused",
    "callback_failed", "lab_error", None,
})
_STOP_EFFECTS = frozenset({
    "not_requested", "prevented_start", "delivered", "too_late", "unknown",
})
_FAILURE_CODES = frozenset({
    "manifest_invalid", "artifacts_invalid", "environment_invalid", "inputs_changed",
    "target_unavailable", "target_mismatch", "path_conflict", "invalid_argument",
    "dispatch_refused", "spawn_failed", "lifecycle_unavailable",
    "campaign_unavailable", "callback_failed", "io_error", "lab_error",
})
_FAILURE_STAGES = frozenset({
    "connect", "check", "prepare", "before_start", "spawning", "running",
    "stopping", "finalizing", "status",
})


class EvidenceInvalid(ValueError):
    """A Lab-owned H1 result is present but outside its closed format."""


def publish_execution(logs: Path, snapshot: ExecutionSnapshot) -> None:
    """Publish one immutable snapshot with a no-overwrite same-directory link."""
    if type(snapshot) is not ExecutionSnapshot:
        raise TypeError("execution snapshot required")
    root = Path(logs)
    encoded = (
        json.dumps(
            snapshot.public(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root, prefix=".h1-result-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        os.link(temporary, root / _RESULT_NAME)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _failure(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == {"code", "stage"}
        and type(value["code"]) is str
        and value["code"] in _FAILURE_CODES
        and type(value["stage"]) is str
        and value["stage"] in _FAILURE_STAGES
    )


def _valid(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == _RESULT_KEYS
        and value["schema"] == "lab-execution-result/1"
        and type(value["operation_id"]) is str
        and type(value["state"]) is str
        and value["state"] in _STATES
        and type(value["stage"]) is str
        and value["stage"] in _STAGES
        and (type(value["launched"]) is bool or value["launched"] is None)
        and (type(value["exit_code"]) is int or value["exit_code"] is None)
        and type(value["stop_requested"]) is bool
        and (type(value["stop_reason"]) is str or value["stop_reason"] is None)
        and value["stop_reason"] in _STOP_REASONS
        and type(value["stop_effect"]) is str
        and value["stop_effect"] in _STOP_EFFECTS
        and (
            type(value["core_force_requested"]) is bool
            or value["core_force_requested"] is None
        )
        and (
            type(value["cleanup_complete"]) is bool
            or value["cleanup_complete"] is None
        )
        and (value["failure"] is None or _failure(value["failure"]))
        and type(value["secondary_failures"]) is list
        and all(_failure(item) for item in value["secondary_failures"])
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceInvalid()
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise EvidenceInvalid()


def read_execution(logs: Path) -> dict[str, Any] | None:
    """Read and validate only the companion H1 result artifact, if present."""
    path = Path(logs) / _RESULT_NAME
    try:
        information = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(information.st_mode) or information.st_size > _MAX_RESULT_BYTES:
        raise EvidenceInvalid()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise EvidenceInvalid() from exc
        raise
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or current.st_size > _MAX_RESULT_BYTES:
            raise EvidenceInvalid()
        raw = stream.read(_MAX_RESULT_BYTES + 1)
    if len(raw) > _MAX_RESULT_BYTES:
        raise EvidenceInvalid()
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique,
            parse_constant=_invalid_constant,
        )
    except EvidenceInvalid:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise EvidenceInvalid() from None
    if not _valid(value):
        raise EvidenceInvalid()
    return value
