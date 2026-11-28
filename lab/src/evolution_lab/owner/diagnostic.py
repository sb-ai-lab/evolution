"""Owner-side reader of the campaign worker's ``diagnostic.json`` (ADR-0023)."""
from __future__ import annotations

import json
from pathlib import Path

from .._suspension import suspension_hint
from .models import CoreTerminal, Failure, Refusal, RefusalFinding, _worker_exception

_SCHEMA = "lab-campaign-diagnostic/5"
_MAX_BYTES = 65536
_KEYS = frozenset({"schema", "operation_id", "scope_id", "failure", "refusal", "core_terminal"})


def _read(path: Path, *, operation_id: str, scope_id: str, kind: str = "campaign") -> dict | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        # Retained diagnostics from the preceding worker have no terminal field.
        keys = _KEYS - {"core_terminal"} if payload.get("schema") == "lab-campaign-diagnostic/2" else _KEYS
        schemas = {_SCHEMA, "lab-campaign-diagnostic/4", "lab-campaign-diagnostic/3", "lab-campaign-diagnostic/2"}
        if payload.get("schema") in {_SCHEMA, "lab-campaign-diagnostic/4"}:
            keys = keys | {"findings"}
        if kind == "evaluation":
            keys = _KEYS - {"refusal", "core_terminal"}
            schemas = {"lab-evaluation-diagnostic/1", "lab-evaluation-diagnostic/2"}
        elif kind != "campaign":
            return None
        current = payload.get("schema") in {_SCHEMA, "lab-evaluation-diagnostic/2"}
        if current:
            keys = keys | {"exception"}
        if (set(payload) != keys or payload.get("schema") not in schemas
                or payload.get("operation_id") != operation_id or payload.get("scope_id") != scope_id
                or (payload.get("refusal") is not None and payload.get("core_terminal") is not None)):
            return None
        if payload["failure"] is not None:
            failure = Failure(**payload["failure"])
            if (failure.code != "lab_error" or payload.get("refusal") is not None
                    or payload.get("core_terminal") is not None):
                return None
        if current:
            if payload["exception"] is not None:
                _worker_exception(payload["exception"])
                if payload["failure"] is None:
                    return None
            elif payload["failure"] is not None:
                return None
        return payload
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def read_campaign_findings(path: Path, *, operation_id: str, scope_id: str) -> tuple[RefusalFinding, ...]:
    """All findings from the current worker; older diagnostics have no full record."""
    payload = _read(path, operation_id=operation_id, scope_id=scope_id)
    try:
        if payload is None or payload.get("refusal") is None:
            return ()
        raw = payload.get("findings", [])
        if not isinstance(raw, list):
            return ()
        findings = tuple(RefusalFinding.from_payload(f) for f in raw)
        if findings and Refusal(findings[0].code, findings[0].path, findings[0].refusal_class,
                               findings[0].instance_digest).to_payload() != payload["refusal"]:
            return ()
        return findings
    except (TypeError, ValueError):
        return ()


def read_worker_failure(path: Path, *, operation_id: str, scope_id: str, kind: str) -> Failure | None:
    """A validated, fixed Lab worker failure for this exact start. Never raises."""
    payload = _read(path, operation_id=operation_id, scope_id=scope_id, kind=kind)
    if payload is None or payload["failure"] is None:
        return None
    return Failure(**payload["failure"])


def read_worker_exception(path: Path, *, operation_id: str, scope_id: str, kind: str) -> dict | None:
    """Closed exception identifiers for this exact worker start, without its message."""
    payload = _read(path, operation_id=operation_id, scope_id=scope_id, kind=kind)
    return None if payload is None else payload.get("exception")


def read_campaign_refusal(path: Path, *, operation_id: str, scope_id: str) -> Refusal | None:
    """The core refusal published for this exact operation and scope, else None. Never raises."""
    payload = _read(path, operation_id=operation_id, scope_id=scope_id)
    try:
        return None if payload is None or payload["refusal"] is None else Refusal.from_payload(payload["refusal"])
    except (TypeError, ValueError):
        return None


def read_campaign_core_terminal(path: Path, *, operation_id: str, scope_id: str) -> CoreTerminal | None:
    """The exact worker terminal, with Lab's existing bilingual hint, else None. Never raises."""
    payload = _read(path, operation_id=operation_id, scope_id=scope_id)
    try:
        value = None if payload is None else payload.get("core_terminal")
        if not isinstance(value, dict) or set(value) != {"owner", "terminal", "code"}:
            return None
        if value["terminal"] in {"finished", "suspended"} and value["code"] != "projection_invalid":
            return None
        terminal = CoreTerminal(**value)
        hints = {lang: suspension_hint(terminal.terminal, lang, terminal.code) for lang in ("ru", "en")}
        return CoreTerminal(**value, hint=hints if all(hints.values()) else None)
    except (TypeError, ValueError):
        return None
