"""Typed schemas, identifier grammar and canonical encoding for the `/evolution/v2` wire.

Transport-neutral: no `http` import here (`http_adapter.py` owns the transport).
Every parser takes a plain JSON-decoded object and returns either a frozen
dataclass or a typed `WireError`; unknown keys, wrong types and non-grammar
identifiers are rejected the way `journal_schema.py` rejects a malformed durable
record (`_keys` closed sets, `type(x) is int`, an identifier regex). `attests`/
`attested` content is not compared against `REQUIRED_ATTESTS` here: a parser only
proves the wire shape (a list of identifiers); `attestation.verify_attestation`
is what decides whether the reported set is the required one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from evolution.tree.execution.usage import Usage

PROTOCOL_VERSION = "2"
PREFIX = "/evolution/v2"
IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
REQUIRED_ATTESTS: tuple[str, ...] = ("loaded_candidate_digest", "dependency_digest")
SEED_MODES = frozenset({"exact", "unpaired", "none"})
STRATEGY = "artifact_root_injection"
ERROR_CODES = frozenset(
    {
        "invalid_request",
        "request_conflict",
        "request_expired",
        "unknown_instance",
        "unknown_session",
        "instance_conflict",
        "instances_exhausted",
        "root_not_allowed",
        "policy_mismatch",
        "not_ready",
        "internal",
    }
)

#: The reason's two identifiers: the `tool_result.status` grammar, capped at
#: 64 characters (PDR-0014). Shorter than `IDENTIFIER` on purpose — a reason
#: is a code, not a name.
_REASON_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_REASON_KEYS = frozenset({"category", "code"})

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
#: The longest `run_id` a caller may choose: `128 - 1 - 32`, one literal
#: separator plus the 32 hex chars of the uuid suffix `new_instance_id` appends,
#: so every `<run_id>-<32 hex>` instance id still fits ADR-0007's grammar.
MAX_RUN_ID = 128 - 1 - 32
_MAX_RUN_ID = MAX_RUN_ID
_INSTANCE_ID = re.compile(
    r"(?P<run_id>[A-Za-z0-9._-]{1," + str(_MAX_RUN_ID) + r"})-(?P<hex>[0-9a-f]{32})\Z"
)


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError("invalid /evolution/v2 wire value")


def _identifier(value: object) -> None:
    _require(isinstance(value, str) and IDENTIFIER.match(value) is not None)


def _sha(value: object) -> None:
    _require(isinstance(value, str) and _SHA256.match(value) is not None)


def _int(value: object, minimum: int = 0) -> None:
    _require(type(value) is int and value >= minimum)


def _bool(value: object) -> None:
    _require(type(value) is bool)


def _keys(
    value: object, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    _require(isinstance(value, dict))
    assert isinstance(value, dict)
    _require(required <= value.keys() <= required | optional)
    return value


def validate_turn_reason(value: object) -> Mapping[str, str] | None:
    """The turn's optional `reason`: exactly `category` and `code`, both the
    64-char reason grammar (PDR-0014). Permitted on any outcome, `ok`
    included — core carries it and concludes nothing from it.

    Shared by `TurnResponse` and `interaction.Turn`. Both use the same
    grammar and detached-copy rule.

    Accepts any `Mapping`, because that is what the annotation on both
    `TurnResponse.reason` and `interaction.Turn.reason` promises -- a backend
    that hands out a `MappingProxyType` rather than the dict behind it is
    conforming, and a `dict`-only check would answer it `500 internal`.

    Returns a **detached copy**, never the caller's object: see
    `validate_instance_description` below for why validating a mapping and
    then keeping the producer's own object is not a validated value at all.
    The copy is taken *first* and everything below is checked on the copy, so
    the value that is validated is the value that is returned even when the
    caller's mapping is live.
    """
    if value is None:
        return None
    _require(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    reason = dict(value)
    _require(reason.keys() == _REASON_KEYS)
    for item in reason.values():
        _require(isinstance(item, str) and _REASON_ID.match(item) is not None)
    return reason


#: A backend's instance description (PDR-0014): a flat object of scalars,
#: bounded so a backend cannot make the journal carry an arbitrary payload.
_DESCRIPTION_MAX_KEYS = 32
_DESCRIPTION_MAX_BYTES = 4096


def validate_instance_description(value: object) -> Mapping[str, Any] | None:
    """The optional `describe()` body (ADR-0009, amended): a flat object of
    scalars only, at most 32 keys and 4096 bytes of canonical JSON. Core
    checks shape and size and nothing else — filtering secrets out of it is
    the backend's obligation, because core holds neither the names nor the
    provenance of the target's launch secrets.

    Returns a **detached copy**, never the caller's object. PDR-0014 says the
    description is "captured once from the creation body", and a validator
    that hands back the producer's own mapping captures nothing: a backend
    that mutates the dict it returned from `describe()` would make a later
    `GET /instances/{id}` — which rebuilds the body from the held mapping —
    serve a description that was never validated, and that a conforming
    client's `parse_instance` may then refuse. The copy is shallow because the
    grammar above is flat by construction: every admitted value is an
    immutable scalar, so there is nothing below the top level to alias.

    Accepts any `Mapping` on the way in, because ADR-0009 (as amended
    2026-10-15) binds the callback as `describe() -> Mapping | None` and
    `DescribingInstance.describe` declares exactly that: a backend returning a
    `MappingProxyType` -- the natural way to publish a description without
    publishing the dict behind it -- is conforming and must be served.

    A plain `dict` on the way out, not a `MappingProxyType`: `canonical_body`'s
    `json.dumps` does not serialize a mapping proxy, and this value is written
    straight into wire bodies and journal records. The copy is taken *first*,
    so the size and shape checks below -- `canonical_body` included -- run on
    the very object that is returned."""
    if value is None:
        return None
    _require(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    description = dict(value)
    _require(len(description) <= _DESCRIPTION_MAX_KEYS)
    for key, item in description.items():
        _identifier(key)
        if item is None or type(item) is bool or type(item) is str:
            continue
        if type(item) is int:
            continue
        _require(type(item) is float and isfinite(item))
    _require(len(canonical_body(description)) <= _DESCRIPTION_MAX_BYTES)
    return description


#: Provenance and components (ADR-0024, G1). Bounds keep one turn's report
#: and one tree's map inside a single journal record in family 12.
PROVENANCE_MAX_USED = 1024
_PROVENANCE_ID_MAX = 256
_COMPONENT_ID = re.compile(r"[A-Za-z0-9._/#:-]{1,256}\Z")
_COMPONENTS_MAX = 4096
_COMPONENTS_MAX_BYTES = 256 * 1024
#: A family-12 row's stored provenance union shares the component map's byte
#: budget, measured as canonical JSON of the sorted list; a larger union is
#: recorded as unknown (ADR-0024, 2026-10-01 amendment). Not a wire bound.
PROVENANCE_MAX_BYTES = _COMPONENTS_MAX_BYTES
#: The artifact path grammar the scoring digest and editor evidence use.
_PATH_MAX = 4096
_NAME_MAX = 255


def _artifact_path(value: object, limit: int = _PATH_MAX) -> bool:
    if not isinstance(value, str) or not value or len(value) > limit:
        return False
    if "\\" in value or value.startswith("/") or not value.isprintable():
        return False
    return all(part not in ("", ".", "..") and len(part) <= _NAME_MAX for part in value.split("/"))


def is_artifact_path(value: object) -> bool:
    """The artifact path grammar `used` entries are read under."""

    return _artifact_path(value, _PROVENANCE_ID_MAX)


def _str_list(value: object) -> list[Any]:
    # A backend may build it as a tuple; the wire (JSON) always gives a list.
    _require(isinstance(value, (list, tuple)))
    assert isinstance(value, (list, tuple))
    return list(value)


def validate_turn_provenance(value: object) -> dict[str, list[str]] | None:
    """The turn's optional `provenance` (ADR-0024): exactly `used`, a list of
    at most 1024 identifiers, each an artifact path or a component identifier
    of at most 256 characters. Shape only: whether an identifier names a
    declared component is family 12's question, not the wire's.

    Returns a detached copy, list included, for the same reason as
    `validate_instance_description`."""
    if value is None:
        return None
    _require(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    provenance = dict(value)
    _require(provenance.keys() == {"used"})
    used = _str_list(provenance["used"])
    _require(len(used) <= PROVENANCE_MAX_USED)
    for item in used:
        _require(
            isinstance(item, str)
            and (_artifact_path(item, _PROVENANCE_ID_MAX) or _COMPONENT_ID.match(item) is not None)
        )
    return {"used": used}


def validate_instance_components(value: object) -> dict[str, dict[str, Any]] | None:
    """The instance's optional `components` map (ADR-0024): at most 4096
    entries keyed by component identifier, each `{sha256, sources}` with
    distinct artifact-path sources, at most 256 KiB of canonical JSON.
    Returns a detached copy, inner dicts and lists included."""
    if value is None:
        return None
    _require(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    components: dict[str, dict[str, Any]] = {}
    _require(len(value) <= _COMPONENTS_MAX)
    for key, item in dict(value).items():
        _require(isinstance(key, str) and _COMPONENT_ID.match(key) is not None)
        _require(isinstance(item, Mapping))
        assert isinstance(item, Mapping)
        entry = dict(item)
        _require(entry.keys() == {"sha256", "sources"})
        _sha(entry["sha256"])
        sources = _str_list(entry["sources"])
        _require(all(_artifact_path(path) for path in sources))
        _require(len(set(sources)) == len(sources))
        components[key] = {"sha256": entry["sha256"], "sources": sources}
    _require(len(canonical_body(components)) <= _COMPONENTS_MAX_BYTES)
    return components


def _identifier_tuple(value: object) -> tuple[str, ...]:
    _require(isinstance(value, list))
    assert isinstance(value, list)
    for item in value:
        _identifier(item)
    return tuple(value)


def new_instance_id(run_id: str) -> str:
    """Build `f"{run_id}-{uuid4().hex}"`, validating `run_id` against `IDENTIFIER`.

    A 128-char identifier limit means `run_id` itself must be at most
    `_MAX_RUN_ID` (95) characters, else the built id could not fit the grammar.
    """
    _require(isinstance(run_id, str) and IDENTIFIER.match(run_id) is not None)
    _require(len(run_id) <= _MAX_RUN_ID)
    instance_id = f"{run_id}-{uuid4().hex}"
    _require(IDENTIFIER.match(instance_id) is not None)
    return instance_id


def is_instance_id(value: str, run_id: str | None = None) -> bool:
    """Check the `<run_id>-<32 hex>` shape, optionally pinned to a given `run_id`."""
    if not isinstance(value, str):
        return False
    match = _INSTANCE_ID.match(value)
    if match is None:
        return False
    return run_id is None or match.group("run_id") == run_id


def canonical_body(value: Mapping[str, Any]) -> bytes:
    """UTF-8 JSON, sorted keys, no insignificant whitespace; the replay key.

    `allow_nan=False` because `NaN`, `Infinity` and `-Infinity` are Python's
    extension to JSON, not JSON: a body carrying one would be canonical here and
    unparseable at a conforming peer, which would break the byte-identical replay
    the key exists for. `json.dumps` raises `ValueError` for them, so they land
    on the same failure as any other unserializable body.
    """
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("wire body is not JSON-serializable") from exc


class WireError(Exception):
    """A typed, closed-code wire error. `str()` is the code only: messages are
    never persisted."""

    def __init__(self, code: str, status: int, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Capabilities:
    max_instances: int
    max_concurrent_sessions: int
    materialization: tuple[str, ...]
    attests: tuple[str, ...]
    max_input_bytes: int
    max_output_bytes: int
    instantiable: bool

    def __post_init__(self) -> None:
        _int(self.max_instances, 1)
        _int(self.max_concurrent_sessions, 1)
        for item in self.materialization:
            _identifier(item)
        for item in self.attests:
            _identifier(item)
        _int(self.max_input_bytes, 0)
        _int(self.max_output_bytes, 0)
        _bool(self.instantiable)


def _parse_capabilities(obj: object) -> Capabilities:
    v = _keys(
        obj,
        frozenset(
            {
                "max_instances",
                "max_concurrent_sessions",
                "materialization",
                "attests",
                "max_input_bytes",
                "max_output_bytes",
                "instantiable",
            }
        ),
    )
    return Capabilities(
        v["max_instances"],
        v["max_concurrent_sessions"],
        _identifier_tuple(v["materialization"]),
        _identifier_tuple(v["attests"]),
        v["max_input_bytes"],
        v["max_output_bytes"],
        v["instantiable"],
    )


@dataclass(frozen=True)
class Health:
    ready: bool
    protocol_version: str
    target_id: str
    boot_id: str
    contract_digest: str
    settings_hash: str
    runtime_ref_digest: str
    seed_mode: Literal["exact", "unpaired", "none"]
    allowed_artifact_root: str
    policy_digest: str
    capabilities: Capabilities
    instances: tuple[str, ...]

    def __post_init__(self) -> None:
        _bool(self.ready)
        _require(self.protocol_version == PROTOCOL_VERSION)
        for name in (
            "target_id",
            "boot_id",
            "contract_digest",
            "settings_hash",
            "runtime_ref_digest",
        ):
            _identifier(getattr(self, name))
        _require(self.seed_mode in SEED_MODES)
        # The allowed root must be an *absolute* directory. A relative one
        # would be resolved against the client's working directory, so a
        # nonconforming target advertising "." could make the caller build its
        # run root — and decide containment — under an unrelated directory.
        _require(isinstance(self.allowed_artifact_root, str))
        _require("\x00" not in self.allowed_artifact_root)
        _require(PurePosixPath(self.allowed_artifact_root).is_absolute())
        _sha(self.policy_digest)
        _require(isinstance(self.capabilities, Capabilities))
        for item in self.instances:
            _identifier(item)


def parse_health(obj: object) -> Health:
    v = _keys(
        obj,
        frozenset(
            {
                "ready",
                "protocol_version",
                "target_id",
                "boot_id",
                "contract_digest",
                "settings_hash",
                "runtime_ref_digest",
                "seed_mode",
                "allowed_artifact_root",
                "policy_digest",
                "capabilities",
                "instances",
            }
        ),
    )
    return Health(
        v["ready"],
        v["protocol_version"],
        v["target_id"],
        v["boot_id"],
        v["contract_digest"],
        v["settings_hash"],
        v["runtime_ref_digest"],
        v["seed_mode"],
        v["allowed_artifact_root"],
        v["policy_digest"],
        _parse_capabilities(v["capabilities"]),
        _identifier_tuple(v["instances"]),
    )


@dataclass(frozen=True)
class InstanceIdentity:
    instance_id: str
    boot_id: str
    contract_digest: str
    settings_hash: str
    runtime_ref_digest: str
    seed_mode: Literal["exact", "unpaired", "none"]
    protocol_version: str
    strategy: str
    loaded_candidate_digest: str
    dependency_digest: str
    attested: tuple[str, ...]
    file_count: int
    byte_count: int
    description: Mapping[str, Any] | None = None
    components: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in (
            "instance_id",
            "boot_id",
            "contract_digest",
            "settings_hash",
            "runtime_ref_digest",
        ):
            _identifier(getattr(self, name))
        _require(self.seed_mode in SEED_MODES)
        _require(self.protocol_version == PROTOCOL_VERSION)
        _require(self.strategy == STRATEGY)
        _sha(self.loaded_candidate_digest)
        _sha(self.dependency_digest)
        for item in self.attested:
            _identifier(item)
        _int(self.file_count, 0)
        _int(self.byte_count, 0)
        # The validated *copy* is what this frozen record keeps, so a producer
        # that still holds the mapping it passed cannot change what was
        # validated (PDR-0014's "captured once"). Same idiom as
        # `rollout.Turn.__post_init__`'s `_frozen_mapping` on `events`.
        object.__setattr__(self, "description", validate_instance_description(self.description))
        object.__setattr__(self, "components", validate_instance_components(self.components))


def parse_instance(obj: object) -> InstanceIdentity:
    v = _keys(
        obj,
        frozenset(
            {
                "instance_id",
                "boot_id",
                "contract_digest",
                "settings_hash",
                "runtime_ref_digest",
                "seed_mode",
                "protocol_version",
                "strategy",
                "loaded_candidate_digest",
                "dependency_digest",
                "attested",
                "file_count",
                "byte_count",
            }
        ),
        frozenset({"description", "components"}),
    )
    # The server never writes `null`; `_keys` alone would let it through.
    _require("components" not in v or v["components"] is not None)
    return InstanceIdentity(
        v["instance_id"],
        v["boot_id"],
        v["contract_digest"],
        v["settings_hash"],
        v["runtime_ref_digest"],
        v["seed_mode"],
        v["protocol_version"],
        v["strategy"],
        v["loaded_candidate_digest"],
        v["dependency_digest"],
        _identifier_tuple(v["attested"]),
        v["file_count"],
        v["byte_count"],
        validate_instance_description(v.get("description")),
        validate_instance_components(v.get("components")),
    )


@dataclass(frozen=True)
class SessionCreated:
    session_id: str
    requested_seed: int | None
    effective_seed: int | None

    def __post_init__(self) -> None:
        _identifier(self.session_id)
        for name in ("requested_seed", "effective_seed"):
            value = getattr(self, name)
            if value is not None:
                _require(type(value) is int)


def parse_session(obj: object) -> SessionCreated:
    v = _keys(obj, frozenset({"session_id", "requested_seed", "effective_seed"}))
    return SessionCreated(v["session_id"], v["requested_seed"], v["effective_seed"])


@dataclass(frozen=True)
class InstanceReleased:
    """The closed body of `DELETE /instances/{instance_id}`."""

    instance_id: str
    released: bool

    def __post_init__(self) -> None:
        _identifier(self.instance_id)
        _bool(self.released)


def parse_release(obj: object) -> InstanceReleased:
    v = _keys(obj, frozenset({"instance_id", "released"}))
    return InstanceReleased(v["instance_id"], v["released"])


@dataclass(frozen=True)
class SessionClosed:
    """The closed body of `DELETE /instances/{id}/sessions/{session_id}`."""

    session_id: str
    closed: bool

    def __post_init__(self) -> None:
        _identifier(self.session_id)
        _bool(self.closed)


def parse_close(obj: object) -> SessionClosed:
    v = _keys(obj, frozenset({"session_id", "closed"}))
    return SessionClosed(v["session_id"], v["closed"])


@dataclass(frozen=True)
class TurnResponse:
    events: tuple[Mapping[str, Any], ...]
    usage: Usage | None
    latency_ms: int
    outcome: Literal["ok", "target_failed", "candidate_failed"]
    truncated: bool
    reason: Mapping[str, str] | None = None
    provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require(bool(self.events))
        for event in self.events:
            _require(isinstance(event, Mapping))
            event_type = event.get("type")
            _require(isinstance(event_type, str) and bool(event_type.strip()))
        _require(self.usage is None or isinstance(self.usage, Usage))
        _int(self.latency_ms, 0)
        _require(self.outcome in {"ok", "target_failed", "candidate_failed"})
        _bool(self.truncated)
        object.__setattr__(self, "reason", validate_turn_reason(self.reason))
        object.__setattr__(self, "provenance", validate_turn_provenance(self.provenance))


def _parse_usage(obj: object) -> Usage | None:
    if obj is None:
        return None
    v = _keys(obj, frozenset({"prompt", "completion", "cached", "total"}))
    try:
        return Usage(v["prompt"], v["completion"], v["cached"], v["total"])
    except ValueError as exc:
        raise ValueError("invalid /evolution/v2 wire value") from exc


def parse_turn(obj: object) -> TurnResponse:
    v = _keys(
        obj,
        frozenset({"events", "usage", "latency_ms", "outcome", "truncated"}),
        frozenset({"reason", "provenance"}),
    )
    _require("provenance" not in v or v["provenance"] is not None)
    _require(isinstance(v["events"], list))
    events = tuple(v["events"])
    usage = _parse_usage(v["usage"])
    return TurnResponse(
        events,
        usage,
        v["latency_ms"],
        v["outcome"],
        v["truncated"],
        validate_turn_reason(v.get("reason")),
        validate_turn_provenance(v.get("provenance")),
    )


def parse_error(status: int, obj: object) -> WireError:
    """Map a non-200 wire body to a typed `WireError`; never raise on a malformed
    or unknown-code body: both map to `internal`."""
    try:
        v = _keys(obj, frozenset({"error"}))
        err = _keys(v["error"], frozenset({"code", "message", "retryable"}))
        code, message, retryable = err["code"], err["message"], err["retryable"]
        _require(isinstance(code, str))
        _require(isinstance(message, str))
        _require(type(retryable) is bool)
    except ValueError:
        return WireError("internal", status, False)
    if code not in ERROR_CODES:
        return WireError("internal", status, False)
    return WireError(code, status, retryable)
