"""Closed, immutable and secret-free value objects for the S2 Owner."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WORKER_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,255}\Z")
_WORKER_EXCEPTION_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# A measurement id may carry core's condition epoch (a18+: `m-r01-champion@e1`); only measurement
# positions accept it (spec 2026-09-30 §2.5). Operation and other resource ids keep `_ID`.
MEASUREMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(?:@e[1-9][0-9]*)?\Z")
# A diff side: the seed, a champion version, or an attempt's published candidate.
TREE_REF = re.compile(r"(initial|champion:v(0|[1-9][0-9]*)|attempt:[A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z")
# Core's own identifiers (case, sample: journal_schema.py IDENTIFIER, Unicode) are wider than Lab's `_ID`: they
# travel percent-encoded in a path segment (spec 2026-10-13 §3.1). Attempt and reflection ids are core's
# `r<NN>-a<MM>` / `r<NN>-x<MM>`, the number of digits may grow.
CORE_ID = re.compile(r"[\w][\w.:@+\-]{0,255}\Z")
ATTEMPT_ID = re.compile(r"r[0-9]{2,}-a[0-9]{2,}\Z")
REFLECTION_ID = re.compile(r"r[0-9]{2,}-x[0-9]{2,}\Z")
_RESOURCE_KINDS = frozenset({"operation", "target", "task"})
_TOKEN_CONSUMERS = frozenset({'target', 'editor', 'reflector', 'grouping', 'scorer'})
_APPROVAL_TOOLS = frozenset({
    "operation_start", "operation_resume", "policy_confirm", "delete_results", "release", "cleanup",
    "project_start", "project_confirm", "project_accept_commit", "project_revoke_confirmation",
    "project_revoke_acceptance", "project_sync",
})


def _token_conflict_message(name, consumers):
    if (not isinstance(name, str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is None
            or len(name) > 256 or not consumers or any(c not in _TOKEN_CONSUMERS for c in consumers)):
        raise ValueError('invalid token conflict diagnostic')
    return f"Token name {name} conflicts between consumers: {', '.join(sorted(set(consumers)))}."


def _valid_token_conflict(message):
    if not isinstance(message, str):
        return False
    match = re.fullmatch(r'Token name ([A-Za-z_][A-Za-z0-9_]*) conflicts between consumers: ([a-z, ]+)\.', message)
    if match is None:
        return False
    try:
        return message == _token_conflict_message(match[1], match[2].split(', '))
    except ValueError:
        return False


_ERROR_MESSAGES = {
    "baseline_source_unknown": "The source is not an available fixed evaluation operation.",
    "baseline_source_deleted": "The source evaluation's results were deleted.",
    "baseline_import_unsupported": "Lab writes baseline_import into the manifest copy. Use lab campaign --baseline-from.",
    "linked_predecessor_unknown": "The predecessor is not a campaign operation of this state.",
    "linked_predecessor_deleted": "The predecessor's results were deleted.",
    "authorization_reason_required": "Accepting a Core finding requires a reason.",
    "authorization_unknown_finding": "The acceptance does not match a current Core finding.",
    "authorization_effect_invalid": "The effect is not offered by this Core finding.",
    "policy_unconfirmed": "The execution policy has not been confirmed.",
    "gateway_port_busy": "The gateway port is already in use.",
    "gateway_unavailable": "The managed gateway is unavailable.",
    "secret_conflict": "A caller secret conflicts with a Lab-owned secret name.",
    "policy_continuation_mismatch": "An operation cannot change whether it uses an execution policy.",
    "policy_gateway_changed": "The gateway block must match the operation's first start.",
    "policy_route_mismatch": "Core role model and URL must match the execution policy route.",
    "manifest_provider_pin": "Move provider pins from the manifest to the execution policy.",
    "token_name_conflict": "Token names conflict between consumers.",
    "invalid_request": "The owner request is invalid.",
    "core_refused": "The core refused the campaign.",
    "invalid_argument": "An owner argument is invalid.",
    "unsupported_schema": "The owner state schema is not supported.",
    "not_found": "The requested owner resource was not found.",
    "busy": "The requested owner resource is busy.",
    "resource_busy": "The requested owner resource is busy.",
    "inputs_changed": "A declared input changed before execution.",
    "state_changed": "The state shown for approval has changed.",
    "digest_mismatch": "A saved digest does not match its content.",
    "binding_mismatch": "The prepared target binding does not match.",
    "target_slot_held": "Another target of this Owner is ready; stop it before starting a target of a different Bundle.",
    "target_not_running": "The requested target is not running.",
    "cleanup_required": "Cleanup evidence is required before dispatch.",
    "history_unavailable": "The requested owner history is unavailable.",
    "results_deleted": "The requested results were deleted.",
    "manifest_invalid": "The campaign manifest is invalid.",
    "artifacts_invalid": "The saved owner artifacts are invalid.",
    "environment_invalid": "The execution environment identity is invalid.",
    "environment_unavailable": "The execution environment is unavailable.",
    "unsupported_input": "A declared execution input is not supported.",
    "conformance_unavailable": "Conformance is unavailable.",
    "reserved_public_input": "The campaign contains a reserved public input.",
    "reserved_environment_name": "The environment name EVOLUTION_LAB_SCOPE is reserved by Lab.",
    "service_stopping": "The owner is stopping.",
    "reflector_probe_unavailable": "The declared reflector probe is unavailable.",
    "reflector_probe_port_busy": "The reflector probe port is already in use.",
    "target_unavailable": "The managed target is unavailable.",
    "target_mismatch": "The live target identity does not match.",
    "path_conflict": "The execution namespace is already allocated.",
    "dispatch_refused": "Campaign dispatch was refused.",
    "spawn_failed": "The owned process could not be started.",
    "lifecycle_unavailable": "Process lifecycle evidence is unavailable.",
    "campaign_unavailable": "Campaign evidence is unavailable.",
    "campaign_unreadable": "The campaign journal is unreadable.",
    "campaign_not_suspended": "The campaign is not suspended.",
    "target_unstable_fresh_required": (
        "The campaign was suspended because a target it used was unstable; continue it on a fresh target."),
    "callback_failed": "The campaign callback failed.",
    "cleanup_unconfirmed": "Owned process cleanup is unconfirmed.",
    "export_unavailable": "The requested export is unavailable.",
    "private_disclosure_required": "Trace export includes private case data; pass --disclose-private.",
    "trace_not_a_campaign": "Trace export requires a campaign operation; use sample reads for fixed evaluation.",
    "tree_unavailable": "The requested campaign tree is unavailable.",
    "io_error": "The owner storage operation failed.",
    "lab_error": "The owner operation failed.",
    "probe_sessions_open": "probe sessions are open on the target; close or reset them, or stop the target",
    "bundle_probe_only": "a probe Bundle only raises a target for probe sessions",
}
_STAGES = frozenset(
    {
        "connect", "bundle", "prepare", "target_start", "check", "conformance",
        "before_start", "spawning", "running", "finalizing", "stop", "cleanup",
        "release", "report", "diff", "export", "delete", "transport", "read", "resume",
        "probe",
    }
)


def _invalid(code: str = "invalid_argument", stage: str = "before_start") -> None:
    raise OwnerError(code, stage)


def _string(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        _invalid()
    return value


def _identifier(value: Any) -> str:
    value = _string(value)
    if _ID.fullmatch(value) is None or value in {".", ".."} or "/" in value or "\\" in value:
        _invalid()
    return value


def _digest(value: Any) -> str:
    value = _string(value)
    if _DIGEST.fullmatch(value) is None:
        _invalid()
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid()
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is not None and not isinstance(value, bool):
        _invalid()
    return value


def _closed(payload: Any, keys: set[str], *, schema: str | None = None) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise OwnerError("invalid_request", "read")
    if schema is not None and payload.get("schema") != schema:
        raise OwnerError("unsupported_schema", "read")
    return payload


def _freeze_json(value: Any) -> Any:
    if hasattr(value, "to_payload"):
        value = value.to_payload()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid()
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            frozen[_string(key)] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    _invalid()


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Approval:
    via: str
    autonomy: str
    tool: str

    def __post_init__(self) -> None:
        if not isinstance(self.via, str) or self.via not in {"form", "client_rules", "none"}:
            raise ValueError("invalid approval via")
        if not isinstance(self.autonomy, str) or self.autonomy not in {"ask", "yolo"}:
            raise ValueError("invalid approval autonomy")
        if not isinstance(self.tool, str) or self.tool not in _APPROVAL_TOOLS:
            raise ValueError("invalid approval tool")

    @classmethod
    def decode(cls, payload: Any) -> Approval:
        if not isinstance(payload, Mapping) or set(payload) != {"via", "autonomy", "tool"}:
            raise ValueError("invalid approval")
        return cls(payload["via"], payload["autonomy"], payload["tool"])


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    request_id: str
    tool: str
    via: str
    autonomy: str
    resource_kind: str
    resource_id: str
    start: int | None
    outcome: str
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "lab-approval/1", "id": self.id, "request_id": self.request_id,
            "tool": self.tool, "via": self.via, "autonomy": self.autonomy,
            "resource": {"kind": self.resource_kind, "id": self.resource_id},
            "start": self.start, "outcome": self.outcome, "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """The original, immutable confirmation of one execution policy digest."""

    digest: str
    policy: Mapping[str, Any]
    author: str
    channel: str
    note: str | None
    confirmed_at: float

    def __post_init__(self) -> None:
        _digest(self.digest)
        for value in (self.author, self.channel):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                _invalid()
        if self.note is not None and (not isinstance(self.note, str) or "\x00" in self.note):
            _invalid()
        try:
            for value in (self.author, self.channel, self.note):
                if value is not None:
                    value.encode("utf-8")
        except UnicodeError:
            _invalid()
        if not isinstance(self.policy, Mapping):
            _invalid()
        if (isinstance(self.confirmed_at, bool) or not isinstance(self.confirmed_at, (int, float))
                or not math.isfinite(self.confirmed_at) or self.confirmed_at <= 0):
            _invalid()
        object.__setattr__(self, "policy", _freeze_json(self.policy))

    def to_payload(self) -> dict[str, Any]:
        return {"schema": "lab-execution-policy-record/1", "digest": self.digest,
                "policy": _thaw_json(self.policy), "author": self.author, "channel": self.channel,
                "note": self.note, "confirmed_at": self.confirmed_at}

    @classmethod
    def from_payload(cls, payload: Any) -> "PolicyRecord":
        row = _closed(payload, {"schema", "digest", "policy", "author", "channel", "note", "confirmed_at"},
                      schema="lab-execution-policy-record/1")
        return cls(row["digest"], row["policy"], row["author"], row["channel"], row["note"], row["confirmed_at"])


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    stage: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in _ERROR_MESSAGES or self.stage not in _STAGES:
            raise ValueError("invalid owner failure")
        if self.message != _ERROR_MESSAGES[self.code] and not (
                self.code == 'token_name_conflict' and _valid_token_conflict(self.message)):
            raise ValueError("owner failure message must be the fixed public message")

    @classmethod
    def make(cls, code: str, stage: str) -> "Failure":
        try:
            return cls(code, stage, _ERROR_MESSAGES[code])
        except (KeyError, ValueError):
            raise ValueError("invalid owner failure") from None

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "message": self.message}


@dataclass(frozen=True, slots=True)
class Refusal:
    """Public projection of a core refusal (ADR-0023).

    The payload key is ``class``; the attribute is ``kind`` (Python keyword).
    """
    code: str
    path: str | None
    kind: str
    instance_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not 1 <= len(self.code) <= 256:
            raise ValueError("invalid owner refusal")
        if self.path is not None and (not isinstance(self.path, str) or len(self.path) > 4096):
            raise ValueError("invalid owner refusal")
        if self.kind not in {"hard", "authorizable"}:
            raise ValueError("invalid owner refusal")
        if self.instance_digest is not None and (
                not isinstance(self.instance_digest, str) or _DIGEST.fullmatch(self.instance_digest) is None):
            raise ValueError("invalid owner refusal")

    @classmethod
    def from_payload(cls, payload: Any) -> "Refusal":
        if not isinstance(payload, Mapping) or set(payload) != {"code", "path", "class", "instance_digest"}:
            raise ValueError("invalid owner refusal")
        return cls(payload["code"], payload["path"], payload["class"], payload["instance_digest"])

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "class": self.kind,
                "instance_digest": self.instance_digest}


@dataclass(frozen=True, slots=True)
class Accepted:
    code: str
    digest: str
    effect: str | None = None

    def __post_init__(self):
        _string(self.code)
        _digest(self.digest)
        if self.effect is not None:
            _string(self.effect)

    def to_payload(self):
        return {"code": self.code, "digest": self.digest, "effect": self.effect}


@dataclass(frozen=True, slots=True)
class RefusalFinding:
    """One Core finding; hard findings have no authorization digest."""
    code: str
    path: str | None
    refusal_class: str
    instance_digest: str | None
    effects: tuple[str, ...]
    effect_texts: tuple[str, ...]
    differs: tuple[str, ...]

    def __post_init__(self) -> None:
        Refusal(self.code, self.path, self.refusal_class, self.instance_digest)
        for name in ("effects", "effect_texts", "differs"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(not isinstance(v, str) for v in values):
                raise ValueError("invalid refusal finding")
            object.__setattr__(self, name, tuple(values))
        if len(self.effects) != len(self.effect_texts):
            raise ValueError("invalid refusal finding effects")

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "class": self.refusal_class,
                "instance_digest": self.instance_digest, "effects": list(self.effects),
                "effect_texts": list(self.effect_texts), "differs": list(self.differs)}

    @classmethod
    def from_payload(cls, payload: Any) -> "RefusalFinding":
        if not isinstance(payload, Mapping) or set(payload) != {
                "code", "path", "class", "instance_digest", "effects", "effect_texts", "differs"}:
            raise ValueError("invalid refusal finding")
        return cls(payload["code"], payload["path"], payload["class"], payload["instance_digest"],
                   payload["effects"], payload["effect_texts"], payload["differs"])


@dataclass(frozen=True, slots=True)
class RefusalRecord:
    operation_id: str
    start: int
    findings: tuple[RefusalFinding, ...]

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 1:
            raise ValueError("invalid refusal start")
        if not self.findings or any(not isinstance(f, RefusalFinding) for f in self.findings):
            raise ValueError("invalid refusal findings")
        object.__setattr__(self, "findings", tuple(self.findings))

    def to_payload(self) -> dict[str, Any]:
        return {"schema": "lab-operation-refusal/1", "operation_id": self.operation_id,
                "start": self.start, "findings": [f.to_payload() for f in self.findings]}

    @classmethod
    def from_payload(cls, payload: Any) -> "RefusalRecord":
        row = _closed(payload, {"schema", "operation_id", "start", "findings"},
                      schema="lab-operation-refusal/1")
        if not isinstance(row["findings"], (tuple, list)):
            raise ValueError("invalid refusal findings")
        return cls(row["operation_id"], row["start"],
                   tuple(RefusalFinding.from_payload(f) for f in row["findings"]))


def _worker_exception(value):
    if (not isinstance(value, Mapping) or set(value) != {"class", "code"}
            or not isinstance(value["class"], str)
            or _WORKER_EXCEPTION_CLASS.fullmatch(value["class"]) is None
            or (value["code"] is not None and (not isinstance(value["code"], str)
                or _WORKER_EXCEPTION_CODE.fullmatch(value["code"]) is None))):
        raise ValueError("invalid worker exception")
    return value


@dataclass(frozen=True, slots=True)
class WorkerFailureRecord:
    operation_id: str
    start: int
    scope_id: str
    exception: Any
    log: str

    def __post_init__(self):
        _identifier(self.operation_id)
        _identifier(self.scope_id)
        if type(self.start) is not int or self.start < 1:
            raise ValueError("invalid failure start")
        if self.log != f"scopes/{self.scope_id}/stderr.log":
            raise ValueError("invalid failure log")
        object.__setattr__(self, "exception", _freeze_json(_worker_exception(self.exception)))

    def to_payload(self):
        return {"schema": "lab-operation-failure/1", "operation_id": self.operation_id,
                "start": self.start, "scope_id": self.scope_id,
                "exception": _thaw_json(self.exception), "log": self.log}

    @classmethod
    def from_payload(cls, payload):
        row = _closed(payload, {"schema", "operation_id", "start", "scope_id", "exception", "log"},
                      schema="lab-operation-failure/1")
        return cls(row["operation_id"], row["start"], row["scope_id"], row["exception"], row["log"])


@dataclass(frozen=True, slots=True)
class CoreTerminal:
    """A Core failure terminal and its optional bilingual repair hint (ADR-0023)."""
    owner: str
    terminal: str
    code: str | None
    hint: Any = None

    def __post_init__(self) -> None:
        for value in (self.owner, self.terminal):
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise ValueError("invalid core terminal")
        if self.code is not None and (not isinstance(self.code, str) or not 1 <= len(self.code) <= 256):
            raise ValueError("invalid core terminal")
        if self.hint is not None and (not isinstance(self.hint, Mapping) or set(self.hint) != {"ru", "en"}
                or any(not isinstance(value, str) or not value for value in self.hint.values())):
            raise ValueError("invalid core terminal hint")
        object.__setattr__(self, "hint", _freeze_json(self.hint))

    @classmethod
    def from_payload(cls, payload: Any) -> "CoreTerminal":
        if not isinstance(payload, Mapping) or set(payload) != {"owner", "terminal", "code", "hint"}:
            raise ValueError("invalid core terminal")
        return cls(payload["owner"], payload["terminal"], payload["code"], payload["hint"])

    def to_payload(self) -> dict[str, Any]:
        return {"owner": self.owner, "terminal": self.terminal, "code": self.code, "hint": _thaw_json(self.hint)}


class OwnerError(RuntimeError):
    """Expected S2 refusal with a fixed, closed public failure."""

    def __init__(self, code: str, stage: str, *, token_conflict=None):
        if token_conflict is not None:
            if code != 'token_name_conflict':
                raise ValueError('diagnostic is only for token_name_conflict')
            self.failure = Failure(code, stage, _token_conflict_message(*token_conflict))
        else:
            self.failure = Failure.make(code, stage)
        super().__init__(self.failure.message)

    @property
    def code(self) -> str:
        return self.failure.code

    @property
    def stage(self) -> str:
        return self.failure.stage


@dataclass(frozen=True, slots=True)
class ResourceRef:
    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in _RESOURCE_KINDS:
            _invalid()
        _identifier(self.id)

    @classmethod
    def from_payload(cls, payload: Any) -> "ResourceRef":
        row = _closed(payload, {"kind", "id"})
        try:
            return cls(row["kind"], row["id"])
        except OwnerError as error:
            raise OwnerError(error.code, "read") from None

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class BaselineImportChoice:
    """The fixed evaluation selected for a campaign's first baseline."""

    source_operation_id: str
    source_state: Path
    source: Path

    @classmethod
    def for_operation(cls, state: Path, operation_id: str) -> BaselineImportChoice:
        identifier = ResourceRef("operation", operation_id).id
        root = state.resolve()
        measurement = root / "artifacts" / identifier / identifier / "measurement"
        return cls(identifier, root, measurement.resolve())


@dataclass(frozen=True, slots=True)
class TaskRef:
    id: str
    owner_generation: str
    schema: str = field(default="lab-task-ref/1", init=False)

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.owner_generation)

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskRef":
        row = _closed(payload, {"schema", "id", "owner_generation"}, schema="lab-task-ref/1")
        try:
            return cls(row["id"], row["owner_generation"])
        except OwnerError as error:
            raise OwnerError(error.code, "read") from None

    def to_payload(self) -> dict[str, str]:
        return {"schema": self.schema, "id": self.id, "owner_generation": self.owner_generation}


@dataclass(frozen=True, slots=True)
class TargetRef:
    id: str
    owner_generation: str
    schema: str = field(default="lab-target-ref/1", init=False)

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.owner_generation)

    @classmethod
    def from_payload(cls, payload: Any) -> "TargetRef":
        row = _closed(payload, {"schema", "id", "owner_generation"}, schema="lab-target-ref/1")
        try:
            return cls(row["id"], row["owner_generation"])
        except OwnerError as error:
            raise OwnerError(error.code, "read") from None

    def to_payload(self) -> dict[str, str]:
        return {"schema": self.schema, "id": self.id, "owner_generation": self.owner_generation}


@dataclass(frozen=True, slots=True)
class Connection:
    id: str
    source_kind: str
    bundle_digest: str | None
    launch_digest: str | None
    owner_generation: str
    schema: str = field(default="lab-owner-connection/1", init=False)

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.owner_generation)
        if self.source_kind not in {"campaign", "evaluation", "probe"}:
            _invalid()
        if self.bundle_digest is not None:
            _digest(self.bundle_digest)
        if self.launch_digest is not None:
            _digest(self.launch_digest)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "id": self.id, "source_kind": self.source_kind,
            "bundle_digest": self.bundle_digest, "launch_digest": self.launch_digest,
            "owner_generation": self.owner_generation,
        }


@dataclass(frozen=True, slots=True)
class ProcessFacts:
    launched: bool | None = None
    exit_code: int | None = None
    cleanup_complete: bool | None = None
    forced: bool | None = None

    def __post_init__(self) -> None:
        _optional_bool(self.launched)
        _optional_int(self.exit_code)
        _optional_bool(self.cleanup_complete)
        _optional_bool(self.forced)

    def to_payload(self) -> dict[str, Any]:
        return {
            "launched": self.launched, "exit_code": self.exit_code,
            "cleanup_complete": self.cleanup_complete, "forced": self.forced,
        }


@dataclass(frozen=True, slots=True)
class CleanupFacts:
    state: str
    complete: bool | None

    def __post_init__(self) -> None:
        if self.state not in {"not_needed", "pending", "confirmed", "unconfirmed", "manual"}:
            _invalid()
        _optional_bool(self.complete)
        if self.state == "confirmed" and self.complete is not True:
            _invalid()
        if self.state == "not_needed" and self.complete is not True:
            _invalid()
        if self.state in {"pending", "unconfirmed", "manual"} and self.complete is True:
            _invalid()

    @classmethod
    def pending(cls) -> "CleanupFacts":
        return cls("pending", None)

    def to_payload(self) -> dict[str, Any]:
        return {"state": self.state, "complete": self.complete}


@dataclass(frozen=True, slots=True)
class Persistence:
    state: str = "not_attempted"
    failure: Failure | None = None

    def __post_init__(self) -> None:
        if self.state not in {"not_attempted", "saved", "failed"}:
            _invalid()
        if self.state == "failed" and self.failure is None:
            _invalid()
        if self.state != "failed" and self.failure is not None:
            _invalid()

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "failure": None if self.failure is None else self.failure.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    environment_digest: str
    lab_wheel_sha256: str
    core_wheel_sha256: str
    module_hashes: Mapping[str, str]
    kind: str = "lab-campaign-worker/1"

    def __post_init__(self) -> None:
        if self.kind not in {
            "lab-campaign-worker/1",
            "lab-evaluation-worker/1",
            "lab-conformance-worker/1",
        }:
            _invalid()
        _digest(self.environment_digest)
        _digest(self.lab_wheel_sha256)
        _digest(self.core_wheel_sha256)
        if not isinstance(self.module_hashes, Mapping):
            _invalid()
        copied: dict[str, str] = {}
        for name, digest in self.module_hashes.items():
            copied[_string(name)] = _digest(digest)
        object.__setattr__(self, "module_hashes", MappingProxyType(dict(sorted(copied.items()))))

    @classmethod
    def from_payload(cls, payload: Any) -> "ExecutorIdentity":
        row = _closed(
            payload,
            {"kind", "environment_digest", "lab_wheel_sha256", "core_wheel_sha256", "module_hashes"},
        )
        if not isinstance(row["module_hashes"], Mapping):
            raise OwnerError("invalid_request", "read")
        try:
            return cls(
                environment_digest=row["environment_digest"],
                lab_wheel_sha256=row["lab_wheel_sha256"],
                core_wheel_sha256=row["core_wheel_sha256"],
                module_hashes=row["module_hashes"],
                kind=row["kind"],
            )
        except OwnerError as error:
            raise OwnerError(error.code, "read") from None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "environment_digest": self.environment_digest,
            "lab_wheel_sha256": self.lab_wheel_sha256,
            "core_wheel_sha256": self.core_wheel_sha256,
            "module_hashes": dict(self.module_hashes),
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    expected: int
    scored: int

    def __post_init__(self) -> None:
        if isinstance(self.expected, bool) or not isinstance(self.expected, int) or self.expected < 0:
            _invalid()
        if isinstance(self.scored, bool) or not isinstance(self.scored, int) or not 0 <= self.scored <= self.expected:
            _invalid()

    def to_payload(self) -> dict[str, int]:
        return {"expected": self.expected, "scored": self.scored}


@dataclass(frozen=True, slots=True)
class OperationResult:
    prepared_digest: str
    process: ProcessFacts
    core: Any = None
    coverage: Coverage | None = None
    stopped_reason: str | None = None
    campaign_execution: Any = None
    persistence: Persistence = field(default_factory=Persistence)
    kind: str = "campaign"
    unmeasured: Any = None
    min_coverage: float = 1.0
    unanswered: int | None = None
    unanswered_state: str = "unavailable"
    answered_reported: int | None = None
    schema: str = field(default="lab-operation-result/3", init=False)

    def __post_init__(self) -> None:
        from evolution_lab._outcomes import validate_coverage_threshold, validate_unmeasured
        try:
            validate_coverage_threshold(self.min_coverage)
            validate_unmeasured(_thaw_json(self.unmeasured))
        except ValueError:
            _invalid()
        object.__setattr__(self, "unmeasured", _freeze_json(self.unmeasured))
        _digest(self.prepared_digest)
        if self.kind not in {"campaign", "evaluation"}:
            _invalid()
        if self.unanswered_state not in {"present", "partial", "not_reported", "unavailable"}:
            _invalid()
        if self.unanswered_state in {"present", "partial"}:
            if (type(self.unanswered) is not int or type(self.answered_reported) is not int
                    or not 0 <= self.unanswered <= self.answered_reported or self.answered_reported == 0):
                _invalid()
            if self.kind == "evaluation" and self.coverage is not None:
                if self.answered_reported > self.coverage.scored or (
                        (self.unanswered_state == "present") != (self.answered_reported == self.coverage.scored)):
                    _invalid()
        elif self.unanswered is not None or self.answered_reported != (0 if self.unanswered_state == "not_reported" else None):
            _invalid()
        elif self.unanswered_state == "not_reported" and type(self.answered_reported) is not int:
            _invalid()
        if self.stopped_reason is not None:
            _string(self.stopped_reason)
        if self.kind == "evaluation" and self.campaign_execution is not None:
            _invalid()
        object.__setattr__(self, "core", _freeze_json(self.core))
        object.__setattr__(self, "campaign_execution", _freeze_json(self.campaign_execution))

    @classmethod
    def from_payload(cls, payload: Any) -> "OperationResult":
        if isinstance(payload, dict) and payload.get("schema") == "lab-operation-result/1":
            legacy = _closed(payload, {"schema", "kind", "prepared_digest", "process", "core",
                "coverage", "stop_reason", "campaign_execution", "persistence"}, schema="lab-operation-result/1")
            payload = {key: value for key, value in legacy.items() if key != "stop_reason"}
            payload.update(schema="lab-operation-result/2", stopped_reason=legacy["stop_reason"],
                           unmeasured=None, min_coverage=1.0)
        if isinstance(payload, dict) and payload.get("schema") == "lab-operation-result/2":
            legacy = _closed(payload, {"schema", "kind", "prepared_digest", "process", "core", "coverage",
                "stopped_reason", "campaign_execution", "persistence", "unmeasured", "min_coverage"},
                schema="lab-operation-result/2")
            payload = dict(legacy, schema="lab-operation-result/3", unanswered=None,
                           unanswered_state="unavailable", answered_reported=None)
        row = _closed(
            payload,
            {
                "schema", "kind", "prepared_digest", "process", "core", "coverage",
                "stopped_reason", "campaign_execution", "persistence", "unmeasured", "min_coverage",
                "unanswered", "unanswered_state", "answered_reported",
            },
            schema="lab-operation-result/3",
        )
        try:
            process = _closed(
                row["process"], {"launched", "exit_code", "cleanup_complete", "forced"}
            )
            persistence = _closed(row["persistence"], {"state", "failure"})
            failure = persistence["failure"]
            if failure is not None:
                failure_row = _closed(failure, {"code", "stage", "message"})
                failure = Failure(**failure_row)
            coverage = row["coverage"]
            if coverage is not None:
                coverage = Coverage(**_closed(coverage, {"expected", "scored"}))
            return cls(
                prepared_digest=row["prepared_digest"],
                process=ProcessFacts(**process),
                core=row["core"],
                coverage=coverage,
                stopped_reason=row["stopped_reason"],
                campaign_execution=row["campaign_execution"],
                persistence=Persistence(persistence["state"], failure),
                kind=row["kind"],
                unmeasured=row["unmeasured"], min_coverage=row["min_coverage"],
                unanswered=row["unanswered"], unanswered_state=row["unanswered_state"],
                answered_reported=row["answered_reported"],
            )
        except (OwnerError, TypeError, ValueError):
            raise OwnerError("invalid_request", "read") from None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "kind": self.kind, "prepared_digest": self.prepared_digest,
            "process": self.process.to_payload(), "core": _thaw_json(self.core),
            "coverage": None if self.coverage is None else self.coverage.to_payload(),
            "stopped_reason": self.stopped_reason,
            "unmeasured": _thaw_json(self.unmeasured), "min_coverage": self.min_coverage,
            "campaign_execution": _thaw_json(self.campaign_execution),
            "persistence": self.persistence.to_payload(),
            "unanswered": self.unanswered, "unanswered_state": self.unanswered_state,
            "answered_reported": self.answered_reported,
        }


def durable_campaign_outcome(value: Any) -> dict[str, Any] | None:
    """Lab's durable outcome triple from a core campaign outcome (PDR-0013).

    Core family 6 adds ``champion_path``, a path inside the run directory that
    deletion removes and a change-set archive does not contain. Durable places
    keep exactly ``{outcome, reason, continuation}``; a value that is not a
    well-formed core outcome is unknown (``None``).
    """
    if not isinstance(value, Mapping) or not {'outcome', 'reason', 'continuation'} <= set(value):
        return None
    continuation = value['continuation']
    if not (isinstance(value['outcome'], str) and isinstance(value['reason'], str)
            and isinstance(continuation, Mapping) and set(continuation) == {'kind', 'reason'}
            and all(item is None or isinstance(item, str) for item in continuation.values())):
        return None
    return {'outcome': value['outcome'], 'reason': value['reason'],
            'continuation': {'kind': continuation['kind'], 'reason': continuation['reason']}}


def _retained_summary(payload: Any) -> Any:
    if payload is None:
        return None
    row = _closed(payload, {'campaign_outcome', 'dispositions'})
    outcome = row['campaign_outcome']
    if outcome is not None:
        outcome = _closed(outcome, {'outcome', 'reason', 'continuation'})
        _string(outcome['outcome'], empty=True)
        _string(outcome['reason'], empty=True)
        continuation = _closed(outcome['continuation'], {'kind', 'reason'})
        for value in continuation.values():
            if value is not None:
                _string(value, empty=True)
    if not isinstance(row['dispositions'], (list, tuple)):
        _invalid()
    for item in row['dispositions']:
        disposition = _closed(item, {'scope_id', 'disposition', 'reason'})
        _identifier(disposition['scope_id'])
        _string(disposition['disposition'])
        if disposition['disposition'] not in {'normal', 'confirmed_cleanup', 'manual'}:
            _invalid()
        if disposition['reason'] is not None:
            _string(disposition['reason'])
        if (disposition['disposition'] == 'manual') != (disposition['reason'] is not None):
            _invalid()
    return _freeze_json(row)


_SETTLEMENTS = {"positive", "not_positive", "unknown"}


@dataclass(frozen=True, slots=True)
class StartFacts:
    """One start of a campaign operation (ADR-0015): execution facts only.

    The campaign's result is cumulative across starts and lives on the
    operation, never on a start.
    """
    start: int
    scope_id: str
    owner_generation: str
    target_ref: TargetRef
    process: ProcessFacts
    target_cleanup: CleanupFacts
    failure: Failure | None
    settlement: str
    refusal: Refusal | None = None
    core_terminal: CoreTerminal | None = None
    initiated_by: str = 'human'
    auto_resume_attempt: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 1:
            _invalid()
        _identifier(self.scope_id)
        _identifier(self.owner_generation)
        if (not isinstance(self.target_ref, TargetRef) or not isinstance(self.process, ProcessFacts)
                or not isinstance(self.target_cleanup, CleanupFacts)
                or (self.failure is not None and not isinstance(self.failure, Failure))):
            _invalid()
        if self.settlement not in _SETTLEMENTS:
            _invalid()
        if self.refusal is not None and not isinstance(self.refusal, Refusal):
            _invalid()
        if self.core_terminal is not None and not isinstance(self.core_terminal, CoreTerminal):
            _invalid()
        if self.initiated_by == 'human':
            if self.auto_resume_attempt is not None:
                _invalid()
        elif self.initiated_by == 'auto_resume':
            if (isinstance(self.auto_resume_attempt, bool) or not isinstance(self.auto_resume_attempt, int)
                    or self.auto_resume_attempt < 1):
                _invalid()
        else:
            _invalid()

    @classmethod
    def from_payload(cls, payload: Any) -> "StartFacts":
        # ``refusal`` is optional on read: starts stored before /6 have no such key.
        keys = {"start", "scope_id", "owner_generation", "target_ref", "process",
                "target_cleanup", "failure", "settlement"}
        if isinstance(payload, Mapping) and "refusal" in payload:
            keys = keys | {"refusal"}
        if isinstance(payload, Mapping) and "core_terminal" in payload:
            keys = keys | {"core_terminal"}
        if isinstance(payload, Mapping) and "initiated_by" in payload:
            keys = keys | {"initiated_by", "auto_resume_attempt"}
        row = _closed(payload, keys)
        try:
            process = row["process"]
            cleanup = row["target_cleanup"]
            failure = row["failure"]
            if not isinstance(process, Mapping) or not isinstance(cleanup, Mapping):
                raise OwnerError("invalid_request", "read")
            if failure is not None and (not isinstance(failure, Mapping)
                                        or set(failure) != {"code", "stage", "message"}):
                raise OwnerError("invalid_request", "read")
            return cls(
                row["start"], row["scope_id"], row["owner_generation"],
                TargetRef.from_payload(row["target_ref"]),
                ProcessFacts(**_closed(process, {"launched", "exit_code", "cleanup_complete", "forced"})),
                CleanupFacts(**_closed(cleanup, {"state", "complete"})),
                None if failure is None else Failure(failure["code"], failure["stage"], failure["message"]),
                row["settlement"],
                None if row.get("refusal") is None else Refusal.from_payload(row["refusal"]),
                None if row.get("core_terminal") is None else CoreTerminal.from_payload(row["core_terminal"]),
                row.get('initiated_by', 'human'), row.get('auto_resume_attempt'),
            )
        except OwnerError as error:
            raise OwnerError(error.code, "read") from None
        except (TypeError, ValueError):
            raise OwnerError("invalid_request", "read") from None

    def to_payload(self) -> dict[str, Any]:
        return {"start": self.start, "scope_id": self.scope_id,
                "owner_generation": self.owner_generation,
                "target_ref": self.target_ref.to_payload(),
                "process": self.process.to_payload(),
                "target_cleanup": self.target_cleanup.to_payload(),
                "failure": None if self.failure is None else self.failure.to_payload(),
                "settlement": self.settlement,
                "refusal": None if self.refusal is None else self.refusal.to_payload(),
                "core_terminal": None if self.core_terminal is None else self.core_terminal.to_payload(),
                "initiated_by": self.initiated_by, "auto_resume_attempt": self.auto_resume_attempt}


# The (state, reason) pairs of the resume verdict table (spec 2026-09-24 §2.1).
RESUME_PAIRS = frozenset({
    ("not_applicable", "invalid_argument"), ("not_applicable", "campaign_not_suspended"),
    ("refused", "results_deleted"), ("refused", "cleanup_unconfirmed"),
    ("refused", "campaign_unreadable"), ("busy", "resource_busy"),
    ("cleanup_first", "cleanup_required"), ("cleanup_first", "cleanup_unconfirmed"),
    ("ready", None),
})


# Target requirements of the resume verdict (spec 2026-09-29 §2.1): requirement -> refusal code.
TARGET_REQUIREMENTS = {"fresh_instance": "target_unstable_fresh_required"}
_TARGET_STATES = frozenset({"ready", "cleanup_first", "busy"})


@dataclass(frozen=True, slots=True)
class TargetRequirement:
    """Which targets a resume may not use (spec 2026-09-29 §2.1).

    ``fresh_instance``: any ready target of the same Bundle except ``excluded`` —
    every target the campaign has used.  ``reason`` is the ``OwnerError`` code
    ``Owner.resume`` refuses an excluded target with.
    """
    requirement: str
    reason: str
    excluded: tuple[TargetRef, ...]

    def __post_init__(self) -> None:
        if TARGET_REQUIREMENTS.get(self.requirement) != self.reason:
            _invalid()
        if isinstance(self.excluded, (str, bytes, Mapping)):
            _invalid()
        try:
            excluded = tuple(self.excluded)
        except TypeError:
            _invalid()
        if not excluded or any(not isinstance(ref, TargetRef) for ref in excluded) \
                or len(set(excluded)) != len(excluded):
            _invalid()
        object.__setattr__(self, "excluded", excluded)

    def excludes(self, target_ref: TargetRef) -> bool:
        return target_ref in self.excluded

    def to_payload(self) -> dict[str, Any]:
        return {"requirement": self.requirement, "reason": self.reason,
                "excluded": [ref.to_payload() for ref in self.excluded]}

    @classmethod
    def from_payload(cls, value: Any) -> "TargetRequirement":
        if not isinstance(value, Mapping) or set(value) != {"requirement", "reason", "excluded"} \
                or not isinstance(value["excluded"], list):
            raise ValueError("invalid target requirement")
        return cls(value["requirement"], value["reason"],
                   tuple(TargetRef.from_payload(item) for item in value["excluded"]))


@dataclass(frozen=True, slots=True)
class ResumeVerdict:
    """Resume readiness of one operation (spec 2026-09-24 §2.1).

    ``reason`` is the ``OwnerError`` code ``Owner.resume`` would refuse with on
    the same facts; ``None`` only for ``ready``.  ``ready`` is the operation's
    own readiness, not admission at call time (slots, leases, target).
    ``target`` — the target requirement, only on a continuable state.
    """
    state: str
    reason: str | None
    target: TargetRequirement | None = None

    def __post_init__(self) -> None:
        if (self.state, self.reason) not in RESUME_PAIRS:
            _invalid()
        if self.target is not None and (
                not isinstance(self.target, TargetRequirement) or self.state not in _TARGET_STATES):
            _invalid()

    def to_payload(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason,
                "target": None if self.target is None else self.target.to_payload()}

    @classmethod
    def from_payload(cls, value: Any) -> "ResumeVerdict":
        if not isinstance(value, Mapping) or set(value) != {"state", "reason", "target"}:
            raise ValueError("invalid resume verdict")
        target = None if value["target"] is None else TargetRequirement.from_payload(value["target"])
        return cls(value["state"], value["reason"], target)


#: ``results_state`` of an operation status; the panel's label table is checked against it.
RESULTS_STATES = frozenset({"present", "not_created", "deleting", "deleted", "delete_failed", "unavailable"})
#: ``progress_state`` / ``times_state`` of an operation status (spec 2026-09-30 §2.4).
SECTION_STATES = frozenset({"not_started", "present", "unavailable"})
#: ``state`` of a ``lab-owner-target/1`` row.
TARGET_ROW_STATES = frozenset({"starting", "ready", "stopping", "stopped", "unknown", "cleanup_required"})
TIMES_SOURCES = {"campaign": "core_raw", "evaluation": "core_public"}
TIMES_REASONS = frozenset({"core_raw_unsupported", "unreadable"})


def _auto_resume_off():
    return {'policy_digest': None, 'state': 'off', 'next_at': None, 'used': 0,
            'max': None, 'blocked': None, 'detail': None}


def _validate_auto_resume(value):
    row = _closed(value, {'policy_digest', 'state', 'next_at', 'used', 'max', 'blocked', 'detail'})
    if row['policy_digest'] is not None:
        _digest(row['policy_digest'])
    if row['state'] not in {'off', 'waiting', 'running', 'closed', 'exhausted', 'blocked', 'cancelled'}:
        _invalid()
    if isinstance(row['used'], bool) or not isinstance(row['used'], int) or row['used'] < 0:
        _invalid()
    if row['max'] is not None and (isinstance(row['max'], bool) or not isinstance(row['max'], int) or row['max'] < 1):
        _invalid()
    if row['next_at'] is not None and (not isinstance(row['next_at'], str) or not row['next_at'].endswith('Z')):
        _invalid()
    if row['blocked'] is not None:
        block = _closed(row['blocked'], {'code', 'detail'})
        if block['code'] not in {'secrets_unavailable', 'reason_not_listed', 'cleanup_failed',
                                  'target_start_failed', 'resume_refused', 'start_failed'}:
            _invalid()
        if block['detail'] is not None:
            detail = _closed(block['detail'], {'refusal'})
            _string(detail['refusal'])
    if (row['state'] == 'blocked') != (row['blocked'] is not None):
        _invalid()
    if row['detail'] is not None:
        detail = _closed(row['detail'], {'busy'})
        busy = _closed(detail['busy'], {'code'})
        if busy['code'] not in {'resource_busy', 'busy', 'target_slot_held'} or row['state'] != 'waiting':
            _invalid()
    if (row['state'] == 'waiting') != (row['next_at'] is not None):
        _invalid()
    return _freeze_json(row)


def times_marking_valid(kind, times, times_state, times_source, times_reason) -> bool:
    """ADR-0019 marking next to ``times``: where present times come from, why they are unavailable.

    A campaign's times include its campaign journal, read only by the pinned adapter
    (``core_raw``); an evaluation's come from the public measurement journal
    (``core_public``). ``times_reason`` is set exactly when ``times_state`` is unavailable."""
    if (times is None) != (times_source is None):
        return False
    if times_source is not None and times_source != TIMES_SOURCES.get(kind):
        return False
    if (times_reason is not None) != (times_state == "unavailable"):
        return False
    if times_reason is not None and (not isinstance(times_reason, str) or times_reason not in TIMES_REASONS or (
            times_reason == "core_raw_unsupported" and kind != "campaign")):
        return False
    return True


@dataclass(frozen=True, slots=True)
class OperationStatus:
    """Durable status of one Owner operation.

    For a campaign operation (ADR-0015) the top-level ``owner_generation``,
    ``target_ref``, ``process``, ``target_cleanup`` and ``failure`` are a copy
    of the latest start; ``starts`` lists every start in order.  ``result`` is
    the campaign's: core's cumulative projection, whose ``result.process`` is
    the process of the start that last produced the result (after a resume
    that failed before dispatch it is still the earlier start's).
    ``resume`` is the projection of the resume verdict (``owner/resume_rules.py``);
    ``None`` when there is nothing to continue.
    """
    id: str
    owner_generation: str
    prepared_digest: str
    target_ref: TargetRef
    control_state: str
    process: ProcessFacts
    target_cleanup: CleanupFacts
    result: OperationResult | None
    failure: Failure | None
    results_state: str
    executor: ExecutorIdentity
    dispatch_possible: bool = field(default=False, repr=False)
    kind: str = "campaign"
    retained_summary: Any = None
    starts: tuple[StartFacts, ...] = ()
    resume: ResumeVerdict | None = None
    entrypoint: str | None = None
    progress: Any = None
    progress_state: str = "not_started"
    times: Any = None
    times_state: str = "not_started"
    times_source: str | None = None
    times_reason: str | None = None
    refusal: Refusal | None = None
    core_terminal: CoreTerminal | None = None
    auto_resume: Any = field(default_factory=_auto_resume_off)
    schema: str = field(default="lab-operation-status/8", init=False)

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.owner_generation)
        _digest(self.prepared_digest)
        if self.kind not in {"campaign", "evaluation"}:
            _invalid()
        if self.refusal is not None and not isinstance(self.refusal, Refusal):
            _invalid()
        if self.core_terminal is not None and not isinstance(self.core_terminal, CoreTerminal):
            _invalid()
        if self.control_state not in {
            "reserved", "running", "stopping", "finished", "orphaned",
            "cleanup_required", "released",
        }:
            _invalid()
        if self.results_state not in RESULTS_STATES:
            _invalid()
        if not isinstance(self.dispatch_possible, bool):
            _invalid()
        expected_executor = f"lab-{self.kind}-worker/1"
        if self.executor.kind != expected_executor:
            _invalid()
        if self.result is not None and self.result.kind != self.kind:
            _invalid()
        object.__setattr__(self, 'retained_summary', _retained_summary(self.retained_summary))
        if isinstance(self.starts, (str, bytes, Mapping)):
            _invalid()
        try:
            starts = tuple(self.starts)
        except TypeError:
            _invalid()
        if any(not isinstance(start, StartFacts) for start in starts):
            _invalid()
        if [start.start for start in starts] != list(range(1, len(starts) + 1)):
            _invalid()
        if starts:
            # The latest start mirrors the top-level execution fields, so a live
            # overlay via dataclasses.replace(status, process=...) stays consistent.
            starts = starts[:-1] + (replace(
                starts[-1], owner_generation=self.owner_generation, target_ref=self.target_ref,
                process=self.process, target_cleanup=self.target_cleanup, failure=self.failure,
                refusal=self.refusal, core_terminal=self.core_terminal),)
        object.__setattr__(self, 'starts', starts)
        if self.resume is not None and (
                not isinstance(self.resume, ResumeVerdict) or self.resume.state == "not_applicable"):
            _invalid()
        from .progress import validate_progress
        for state, value in ((self.progress_state, self.progress), (self.times_state, self.times)):
            if state not in SECTION_STATES or (value is None) == (state == "present"):
                _invalid()
        if self.progress is not None:
            try:
                # A replace() re-validates the frozen value; the check reads plain JSON.
                validate_progress(_thaw_json(self.progress), self.kind)
            except ValueError:
                _invalid()
        if self.times is not None:
            if not isinstance(self.times, Mapping) or set(self.times) != {"first_event_at", "last_event_at"} \
                    or any(not isinstance(v, str) or not v.endswith("Z") for v in self.times.values()):
                _invalid()
        if not times_marking_valid(self.kind, self.times, self.times_state, self.times_source,
                                   self.times_reason):
            _invalid()
        if self.entrypoint is not None:
            _string(self.entrypoint)
        object.__setattr__(self, "progress", _freeze_json(self.progress))
        object.__setattr__(self, "times", _freeze_json(self.times))
        object.__setattr__(self, 'auto_resume', _validate_auto_resume(self.auto_resume))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "id": self.id, "owner_generation": self.owner_generation,
            "prepared_digest": self.prepared_digest, "kind": self.kind,
            "target_ref": self.target_ref.to_payload(), "control_state": self.control_state,
            "process": self.process.to_payload(), "target_cleanup": self.target_cleanup.to_payload(),
            "result": None if self.result is None else self.result.to_payload(),
            "failure": None if self.failure is None else self.failure.to_payload(),
            "refusal": None if self.refusal is None else self.refusal.to_payload(),
            "core_terminal": None if self.core_terminal is None else self.core_terminal.to_payload(),
            "results_state": self.results_state, "executor": self.executor.to_payload(),
            "retained_summary": _thaw_json(self.retained_summary),
            "starts": [start.to_payload() for start in self.starts],
            "resume": None if self.resume is None else self.resume.to_payload(),
            "entrypoint": self.entrypoint,
            "progress": _thaw_json(self.progress), "progress_state": self.progress_state,
            "times": _thaw_json(self.times), "times_state": self.times_state,
            "times_source": self.times_source, "times_reason": self.times_reason,
            "auto_resume": _thaw_json(self.auto_resume),
        }


@dataclass(frozen=True, slots=True)
class TargetStatus:
    id: str
    owner_generation: str
    launch_digest: str
    bundle_digest: str
    state: str
    base_url: str
    boot_id: str | None
    health: Any
    cleanup: CleanupFacts
    failure: Failure | None
    execution_policy: str | None = None
    schema: str = field(default="lab-owner-target/2", init=False)

    def __post_init__(self) -> None:
        if self.execution_policy is not None:
            _digest(self.execution_policy)
        _identifier(self.id)
        _identifier(self.owner_generation)
        _digest(self.launch_digest)
        _digest(self.bundle_digest)
        if self.state not in TARGET_ROW_STATES:
            _invalid()
        _string(self.base_url)
        if self.boot_id is not None:
            _string(self.boot_id)
        object.__setattr__(self, "health", _freeze_json(self.health))

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema, "id": self.id, "owner_generation": self.owner_generation,
            "launch_digest": self.launch_digest, "bundle_digest": self.bundle_digest,
            "state": self.state, "base_url": self.base_url, "boot_id": self.boot_id,
            "health": _thaw_json(self.health), "cleanup": self.cleanup.to_payload(),
            "failure": None if self.failure is None else self.failure.to_payload(),
        }

        if self.execution_policy is not None:
            payload["execution_policy"] = self.execution_policy
        return payload


@dataclass(frozen=True, slots=True)
class ClientTask:
    task_id: str
    action: str
    state: str
    target_id: str
    bundle_digest: str
    client_ref: str

    def to_payload(self) -> dict[str, str]:
        return {"task_id": self.task_id, "action": self.action, "state": self.state,
                "target_id": self.target_id, "bundle_digest": self.bundle_digest, "client_ref": self.client_ref}


@dataclass(frozen=True, slots=True)
class TaskResult:
    kind: str
    payload: Any

    def __post_init__(self) -> None:
        if self.kind not in {"target_start", "target_stop", "check", "cleanup", "conformance", "export", "delete"}:
            _invalid()
        payload = self.payload
        if self.kind in {"export", "delete"}:
            codec = ExportResult if self.kind == "export" else DeletionResult
            payload = codec.from_payload(
                payload.to_payload() if isinstance(payload, codec) else payload
            ).to_payload()
        object.__setattr__(self, "payload", _freeze_json(payload))

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": _thaw_json(self.payload)}


@dataclass(frozen=True, slots=True)
class TaskStatus:
    id: str
    owner_generation: str
    action: str
    ref: ResourceRef | None
    state: str
    result: TaskResult | None
    failure: Failure | None
    persistence: Persistence = field(default_factory=Persistence)
    dispatch_possible: bool = field(default=False, repr=False)
    schema: str = field(default="lab-task-status/1", init=False)

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.owner_generation)
        if self.action not in {"target_start", "target_stop", "check", "cleanup", "conformance", "export", "delete"}:
            _invalid()
        if self.state not in {"pending", "running", "succeeded", "failed", "unknown"}:
            _invalid()
        if not isinstance(self.dispatch_possible, bool):
            _invalid()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "id": self.id, "owner_generation": self.owner_generation,
            "action": self.action, "ref": None if self.ref is None else self.ref.to_payload(),
            "state": self.state, "result": None if self.result is None else self.result.to_payload(),
            "failure": None if self.failure is None else self.failure.to_payload(),
            "persistence": self.persistence.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class StopReceipt:
    ref: ResourceRef
    accepted: bool
    effect: str
    process: ProcessFacts
    schema: str = field(default="lab-owner-stop/1", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or self.effect not in {"requested", "already_requested", "too_late"}:
            _invalid()

    def to_payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "ref": self.ref.to_payload(), "accepted": self.accepted,
                "effect": self.effect, "process": self.process.to_payload()}


@dataclass(frozen=True, slots=True)
class ReleaseReceipt:
    ref: ResourceRef
    reason: str
    cleanup: CleanupFacts
    released: bool
    released_scopes: tuple[str, ...] = field(default=(), repr=False)
    disposition: str = field(default="manual", init=False)
    schema: str = field(default="lab-owner-release/1", init=False)

    def __post_init__(self) -> None:
        _string(self.reason)
        if not isinstance(self.released, bool):
            _invalid()
        for scope_id in self.released_scopes:
            _identifier(scope_id)

    def to_payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "ref": self.ref.to_payload(),
                "disposition": self.disposition, "reason": self.reason,
                "cleanup": self.cleanup.to_payload(), "released": self.released}


@dataclass(frozen=True, slots=True)
class SuspensionEvidenceResult:
    operation_id: str
    state: str
    suspensions: Any
    reason: str | None
    source: str = field(default="core_raw", init=False)
    schema: str = field(default="lab-suspension-evidence/1", init=False)

    def __post_init__(self) -> None:
        from .suspension_evidence import UnsupportedSuspensionShape, validate_suspensions

        _identifier(self.operation_id)
        reasons = {"not_campaign", "core_raw_unsupported", "campaign_family_unsupported",
                   "campaign_missing", "campaign_unreadable", "unsupported_schema"}
        if self.state == "available":
            if self.reason is not None:
                _invalid()
            try:
                values = validate_suspensions(self.suspensions)
            except UnsupportedSuspensionShape:
                _invalid()
            object.__setattr__(self, "suspensions", _freeze_json(values))
        elif self.state == "unavailable":
            if self.reason not in reasons or self.suspensions is not None:
                _invalid()
        else:
            _invalid()

    def to_payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "operation_id": self.operation_id, "state": self.state,
                "source": self.source, "reason": self.reason,
                "suspensions": _thaw_json(self.suspensions)}


@dataclass(frozen=True, slots=True)
class ReportResult:
    operation_id: str
    state: str
    report: Any
    reason: str | None
    starts: tuple[StartFacts, ...] = ()
    usage: Any = None
    reflector_probe: Any = None
    reflector_probe_state: str = "unavailable"
    reflector_probe_reason: str | None = "unreadable"
    routing: Any = None
    schema: str = field(default="lab-owner-report/15", init=False)

    def __post_init__(self) -> None:
        if self.routing is not None:
            from .routing import validate_routing
            routing = validate_routing(_thaw_json(self.routing))
            if routing['operation_id'] != self.operation_id or not any(
                    run['policy_digest'] is not None for run in routing['runs']):
                _invalid()
            object.__setattr__(self, "routing", _freeze_json(routing))
        _identifier(self.operation_id)
        if self.state not in {"available", "unavailable"}:
            _invalid()
        if self.reason not in {None, "unavailable", "invalid", "io_error", "unknown", "results_deleted"}:
            _invalid()
        from evolution_lab._reflector_probe import validate_probe
        validate_probe(self.reflector_probe, self.reflector_probe_state, self.reflector_probe_reason)
        object.__setattr__(self, "reflector_probe", _freeze_json(self.reflector_probe))
        object.__setattr__(self, "report", _freeze_json(self.report))
        if isinstance(self.starts, (str, bytes, Mapping)):
            _invalid()
        try:
            starts = tuple(self.starts)
        except TypeError:
            _invalid()
        if any(not isinstance(start, StartFacts) for start in starts):
            _invalid()
        object.__setattr__(self, "starts", starts)
        if self.usage is not None:
            # Target-call tokens per measurement (PDR-0017): `usage` is null next to its state.
            if not isinstance(self.usage, (list, tuple)) or any(
                    not isinstance(row, Mapping) or set(row) != {"measurement_id", "usage", "usage_state"}
                    for row in self.usage):
                _invalid()
        object.__setattr__(self, "usage", _freeze_json(self.usage))

    @property
    def core_terminal(self) -> CoreTerminal | None:
        return self.starts[-1].core_terminal if self.starts else None

    def to_payload(self) -> dict[str, Any]:
        return {**({"routing": _thaw_json(self.routing)} if self.routing is not None else {}),
                "schema": self.schema, "operation_id": self.operation_id, "state": self.state,
                "report": _thaw_json(self.report), "reason": self.reason,
                "reflector_probe": _thaw_json(self.reflector_probe),
                "reflector_probe_state": self.reflector_probe_state, "reflector_probe_reason": self.reflector_probe_reason,
                "starts": [s.to_payload() for s in self.starts], "usage": _thaw_json(self.usage),
                "core_terminal": None if self.core_terminal is None else self.core_terminal.to_payload()}


@dataclass(frozen=True, slots=True)
class ReportMarkdownResult:
    """``Owner.report_markdown`` (spec 2026-10-14 report-markdown §2, ADR-0022): the text of ``report.md``, or the
    reason it is unavailable — the reasons of ``report()``; nothing is stored."""

    operation_id: str
    state: str
    markdown: str | None
    reason: str | None

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        if self.state == "available":
            if not isinstance(self.markdown, str) or self.reason is not None:
                _invalid()
        elif self.state == "unavailable":
            if (self.markdown is not None or not isinstance(self.reason, str)
                    or self.reason not in {"results_deleted", "io_error", "unavailable"}):
                _invalid()
        else:
            _invalid()


@dataclass(frozen=True, slots=True)
class Health:
    lab_version: str
    core_version: str
    distributions: tuple[Any, ...]
    owner_generation: str
    active_operation_id: str | None
    blockers: tuple[ResourceRef, ...]
    service_state: str = "running"
    schema: str = field(default="lab-owner-health/1", init=False)

    def __post_init__(self) -> None:
        _string(self.lab_version)
        _string(self.core_version)
        _identifier(self.owner_generation)
        if self.active_operation_id is not None:
            _identifier(self.active_operation_id)
        if self.service_state not in {"running", "stopping"}:
            _invalid()
        object.__setattr__(self, "distributions", tuple(_freeze_json(v) for v in self.distributions))
        object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "lab_version": self.lab_version,
            "core_version": self.core_version,
            "distributions": [_thaw_json(v) for v in self.distributions],
            "owner_generation": self.owner_generation,
            "active_operation_id": self.active_operation_id,
            "blockers": [ref.to_payload() for ref in self.blockers],
            "service_state": self.service_state,
        }


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    owner_generation: str
    cleanup: CleanupFacts
    blockers: tuple[ResourceRef, ...]
    schema: str = field(default="lab-owner-shutdown/1", init=False)

    def __post_init__(self) -> None:
        _identifier(self.owner_generation)
        object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "owner_generation": self.owner_generation,
                "cleanup": self.cleanup.to_payload(),
                "blockers": [ref.to_payload() for ref in self.blockers]}


__all__ = [
    "Accepted",
    "RefusalFinding", "RefusalRecord",
    "CleanupFacts", "Connection", "Coverage", "ExecutorIdentity", "Failure", "Health",
    "OperationResult", "OperationStatus", "OwnerError", "Persistence", "ProcessFacts", "Refusal",
    "ReleaseReceipt", "ReportResult", "ReportMarkdownResult", "ResourceRef", "ShutdownResult", "StartFacts", "StopReceipt",
    "TargetRef", "TargetStatus", "TaskRef", "TaskResult", "TaskStatus",
    "DiffResult", "ExportResult", "DeletionResult",
]


@dataclass(frozen=True, slots=True)
class DiffResult:
    """``lab-operation-diff/2``: the change from tree ``source_ref`` to tree ``selected_ref``."""

    schema = "lab-operation-diff/2"
    operation_id: str
    source_digest: str
    selected_digest: str
    state: str
    text: str
    nontext_changes: tuple[Mapping[str, str], ...] = ()
    source_ref: str = field(kw_only=True)
    selected_ref: str = field(kw_only=True)

    def __post_init__(self):
        _identifier(self.operation_id)
        for ref in (self.source_ref, self.selected_ref):
            if not isinstance(ref, str) or TREE_REF.fullmatch(ref) is None:
                _invalid()
        _digest(self.source_digest)
        _digest(self.selected_digest)
        _string(self.text, empty=True)
        if self.state not in {"changed", "no_change"}:
            _invalid()
        if not isinstance(self.nontext_changes, (tuple, list)):
            _invalid()
        changes = []
        for change in self.nontext_changes:
            row = _closed(change, {"path", "kind"})
            path = _string(row["path"])
            if (any(part in {"", ".", ".."} for part in path.split("/"))
                    or "\\" in path or "\x00" in path or row["kind"] not in {"binary", "mode"}):
                _invalid()
            changes.append(_freeze_json(row))
        if self.state == "no_change" and (self.source_digest != self.selected_digest or self.text or changes):
            _invalid()
        object.__setattr__(self, "nontext_changes", tuple(changes))

    @classmethod
    def from_payload(cls, payload):
        row = dict(_closed(payload, {"schema", "operation_id", "from", "to", "source_digest",
                                     "selected_digest", "state", "text", "nontext_changes"},
                           schema=cls.schema))
        del row["schema"]
        return cls(source_ref=row.pop("from"), selected_ref=row.pop("to"), **row)

    def to_payload(self):
        return {"schema": self.schema, "operation_id": self.operation_id,
                "from": self.source_ref, "to": self.selected_ref,
                "source_digest": self.source_digest,
                "selected_digest": self.selected_digest, "state": self.state,
                "text": self.text, "nontext_changes": _thaw_json(self.nontext_changes)}


@dataclass(frozen=True, slots=True)
class ExportResult:
    operation_id: str
    state: str
    export_id: str | None
    sha256: str | None
    manifest: Any

    def __post_init__(self):
        _identifier(self.operation_id)
        if self.state == "no_change":
            if any(value is not None for value in (self.export_id, self.sha256, self.manifest)):
                _invalid()
        elif self.state == "ready":
            _digest(self.export_id)
            _digest(self.sha256)
            if self.export_id != self.sha256 or not isinstance(self.manifest, Mapping):
                _invalid()
        else:
            _invalid()
        object.__setattr__(self, "manifest", _freeze_json(self.manifest))

    @classmethod
    def from_payload(cls, payload):
        return cls(**_closed(payload, {"operation_id", "state", "export_id", "sha256", "manifest"}))

    def to_payload(self):
        return {"operation_id": self.operation_id, "state": self.state,
                "export_id": self.export_id, "sha256": self.sha256,
                "manifest": _thaw_json(self.manifest)}


@dataclass(frozen=True, slots=True)
class DeletionResult:
    operation_id: str
    state: str
    failure: Failure | None

    def __post_init__(self):
        _identifier(self.operation_id)
        if self.state not in {"deleted", "failed"} or (self.state == "failed") != isinstance(self.failure, Failure):
            _invalid()
        if self.state == "deleted" and self.failure is not None:
            _invalid()

    @classmethod
    def from_payload(cls, payload):
        row = dict(_closed(payload, {"operation_id", "state", "failure"}))
        if row["failure"] is not None:
            try:
                row["failure"] = Failure(**_closed(row["failure"], {"code", "stage", "message"}))
            except (TypeError, ValueError):
                raise OwnerError("invalid_request", "read") from None
        return cls(**row)

    def to_payload(self):
        return {"operation_id": self.operation_id, "state": self.state,
                "failure": None if self.failure is None else self.failure.to_payload()}
