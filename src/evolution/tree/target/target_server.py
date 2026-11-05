"""The `/evolution/v2` server library (ADR-0009).

Its public surface is the eight names `evolution.tree` re-exports —
`TargetServer`, `TargetBackend`, `BackendInstance`, `BackendSession`,
`BackendReply`, `LoadedTree`, `BackendError`, `serve_target` — and it imports
nothing outside `evolution.tree` and the standard library directly (the
transitive edge `artifact_copy` → `evolution.lineage.state_io` exists; see F0,
`docs/target-server.md`).

`TargetServer` is the wire's state machine over a backend a caller supplies:
the identifier grammar, the `request_id` idempotency store with its
tombstones, the authority checks over `candidate_root`, the epoch tuple
carried by every instance identity, the per-instance linearizability that
makes "released" a real boundary, and the `Misbehave` fault hooks a test
target may be configured with. `ReferenceTarget` (`reference_target.py`) is
the demonstration backend bound to this library; `HttpSessionAdapter` is
written against `TargetServer.dispatch` and the conformance suite runs
against it, so every rule in the contract lives here as code rather than as prose.

Design notes worth keeping in mind when reading:

*Two locks, and the global one is never held while waiting on the other*
(ADR-0009). Instance creation and the instance table are guarded by one
global `RLock`; every instance carries its own `Lock`, held for the whole of
any instance-scoped handler (open/turn/close and the instance's own
get/delete), and — new in the three-phase creation below — for the whole of
that instance's *loading*, from admission to publication or failure. A
handler takes the global lock only to look the instance up or to mutate the
table, releases it, then takes the instance lock (which blocks for as long as
the instance is loading) and re-checks `released`/`live`. No filesystem access
and no parsing of caller data happens under the global lock either: creation's
admission is split into a locked record/tombstone decision, an *unlocked*
validation phase (the policy payload, the containment walk) and a locked
re-decision before the table is mutated, so a wedged path under
`allowed_artifact_root` can stall one admission and nothing else. `_release`
takes the instance lock first and the global lock *inside* it — the reverse of
a handler's order — which the rule still allows, because nothing ever waits on
an instance lock while the global lock is held: `_release`'s inner
`with self._lock:` blocks only decide table state (un-publishing the instance
and dropping its creation records before teardown, then popping it and
appending its tombstone), never wait. That ordering
is exactly the guarantee: a session creation either commits before a
release (and is closed by it) or observes the released instance and fails
with `unknown_instance`; a request addressed to a still-loading instance
waits for that instance's load to finish (or fail) rather than for the whole
server.

*Request records belong to their resources.* The contract says a request record lives exactly as
long as the instance it created or addressed. Rather than one dict keyed by
`(scope, request_id)` with a separate ownership index, the two scopes are two
dicts: `_creation_records` (scope `"instances"`, under the global lock, with
`_pending_creations` standing in for it while a creation is still loading) and
`_Instance.records` (scope = the instance id, under that instance's lock). The
key is the same pair; splitting it by scope means release drops an instance's
records by dropping the instance, and no dict is mutated under two different
locks. "Exactly as long" is read strictly at the release boundary: the
instance's creation records go in the same locked block that un-publishes it,
before its teardown, so a creation replay never outlives the publication it
replays (ADR-0007 — replay is byte-identical *while the owning instance is
live*). A replay is byte-identical because the stored response bytes are
returned verbatim, not re-serialized.

*Errors carry no caller data.* Every refusal is a `_Refused` built from a code
in `wire_v2.ERROR_CODES` and a `detail` that is always a module literal (a field
name or a key path). No path, body fragment or identifier from the request ever
reaches the message, which the contract requires of a target whose messages a client
may log.

The transport — the `ThreadingHTTPServer`, the request handler and the
`Content-Length` guard — lives in `reference_target_http.py`; `dispatch` here is
pure, so the whole contract is testable without a socket.

*Two deliberate silences.* `max_concurrent_sessions` is advertised in
`capabilities` but not enforced: the contract's error code set is closed and holds no
session-exhaustion code, so refusing would mean inventing a wire meaning for
`instances_exhausted` that the adapter would then have to decode. And the wire
defines no query parameters, so anything after `?` in a path is ignored rather
than allowed to change routing.

The `Misbehave` hooks exist so conformance can prove the *adapter* rejects a
non-conforming target, but the product value is frozen: `TargetServer.misbehave`
is a read-only property returning the one neutral `NEUTRAL_MISBEHAVE` constant, so
the packaged target itself cannot be made to violate the wire. The hooks live on
`tests.reference_target_faults.FaultInjectingReferenceTarget`, a test-only subclass
that overrides `misbehave` with a per-instance value it replaces through
`fault(**changes)`. They are read at request time, so a test can flip one on a
live server between two calls.

*The backend boundary.* `TargetServer` owns the wire, the identity attested for
an instance and every piece of state a replay depends on; it never answers a
turn itself. Instance creation snapshots the requested tree under the server's
own `snapshot_root` (`artifact_copy.snapshot_tree`) and hands the backend a
`LoadedTree` naming that snapshot, never the caller's live root, so bytes the
backend reads later cannot drift from what the identity attested. Every
backend callback — `load`, `open`, `turn`, every `close` — runs under its
instance's own lock and never under the global one (ADR-0009): `load` runs
between admission and publication, holding only the instance lock taken at
admission; `open` and `turn` are called with the instance and session locks
already held for the whole handler. A callback runs under the same
linearizability the wire promises either way; one that raises, returns an
object without the callbacks the server would later have to call, or reports a
seed the declared `seed_mode` cannot accept, is folded into `internal` without
leaking why, and leaves no instance, no session and no record behind
(ADR-0009). The same rule reaches the reply itself: a turn body is parsed with
the wire's own `parse_turn` before it is recorded, so a backend cannot make
this surface serve — or replay — a 200 the wire rejects.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from evolution.tree.artifacts.artifact_copy import remove_tree, snapshot_tree
from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, Entry
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.interaction import Message, SeedMode, TurnResult
from evolution.tree.execution.wire_v2 import (
    ERROR_CODES,
    IDENTIFIER,
    PREFIX,
    PROTOCOL_VERSION,
    REQUIRED_ATTESTS,
    SEED_MODES,
    STRATEGY,
    WireError,
    canonical_body,
    parse_turn,
    validate_instance_components,
    validate_instance_description,
)
from evolution.tree.target.reference_target_http import RETRYABLE as RETRYABLE
from evolution.tree.target.reference_target_http import _error_payload

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

# A retryable code invites the caller to try again against the same target; the
# rest are terminal for that request. Kept here rather than on `WireError` so the
# adapter's parser stays a parser.
_LIE_DIGEST = "0" * 64


class BackendError(Exception):
    """The one exception a backend raises on purpose; the server answers `internal`."""


@dataclass(frozen=True)
class BackendReply:
    """A backend's answer to one turn: the `TurnResult` and the target's own
    truncation statement, which the adapter turns into `output_truncated`."""

    result: TurnResult
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, TurnResult):
            raise ValueError("BackendReply.result must be a TurnResult")
        if type(self.truncated) is not bool:
            raise ValueError("BackendReply.truncated must be a bool")


@dataclass(frozen=True)
class LoadedTree:
    """What `TargetBackend.load` receives: the server-owned read-only snapshot
    and the facts the server attested about it."""

    instance_id: str
    root: Path
    policy: ArtifactPolicyV1
    candidate_digest: str
    dependency_digest: str
    artifact: tuple[Entry, ...]
    dependency: tuple[Entry, ...]
    max_output_bytes: int
    seed_mode: SeedMode


class BackendSession(Protocol):
    @property
    def effective_seed(self) -> int | None: ...

    def turn(self, message: Message) -> BackendReply: ...

    def close(self) -> None: ...


class BackendInstance(Protocol):
    def open(self, public: Mapping[str, Any], seed: int | None) -> BackendSession: ...

    def close(self) -> None: ...


class DescribingInstance(Protocol):
    """The optional sixth callback (ADR-0009, amended 2026-09-21). A backend
    whose loaded instance implements it states how it assembled the agent
    from the loaded Candidate — a configuration hash, the source of its
    settings, its hermeticity mode, where it passed the seed. The server
    calls it once, right after `load` returns and its shape check passes,
    validates the result's shape and size, and never compares it.

    This is a sibling of `BackendInstance`, not a member of it: a `Protocol`
    member is required by default, so adding `describe` to `BackendInstance`
    itself would make every backend without it non-conforming. Detection
    stays structural (`getattr`/`callable`, see `_instance_description`),
    exactly like the `open`/`close` shape check below — a loaded instance is
    never required to satisfy `isinstance(loaded, DescribingInstance)`.

    Keeping secrets out of the returned mapping is the backend's obligation:
    the server holds neither the names nor the provenance of the target's
    launch secrets.

    Returning `None` means "not reported": the instance is created without a
    `description`, as for a backend that has no `describe` (ADR-0009, amended
    2026-10-15).
    """

    def describe(self) -> Mapping[str, Any] | None: ...


class ComponentsInstance(Protocol):
    """The optional seventh callback (ADR-0009, amended for ADR-0024). A
    loaded instance that implements it declares the components of the
    agent's context it renders from the loaded tree: identifier ->
    `{"sha256": <hash of the rendered bytes>, "sources": [artifact paths]}`.
    Called once right after `load` (and `describe`), validated for shape and
    size, served on the instance body, never compared by the server.

    A sibling of `BackendInstance` for the reason `DescribingInstance` is,
    and not exported: detection is structural (`_instance_components`)."""

    def components(self) -> Mapping[str, Any]: ...


class TargetBackend(Protocol):
    def load(self, tree: LoadedTree) -> BackendInstance: ...


class _Refused(Exception):
    """One typed wire refusal. `detail` is always a module literal, never caller data."""

    def __init__(self, code: str, status: int, detail: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown wire error code: {code}")
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


def _bad(detail: str) -> _Refused:
    return _Refused("invalid_request", 400, detail)


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _object(raw_body: bytes) -> dict[str, Any]:
    """`NaN`/`Infinity` are refused at the parser: `canonical_body` would raise
    on them later, and that is a 500, not the 400 a malformed body earns."""
    try:
        obj = json.loads(raw_body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise _bad("body") from None
    if not isinstance(obj, dict):
        raise _bad("body")
    return obj


def _fields(obj: dict[str, Any], *names: str) -> list[Any]:
    """Closed key set: exactly `names`, no more and no fewer."""
    if set(obj) != set(names):
        raise _bad("keys")
    return [obj[name] for name in names]


def _identifier(value: object, detail: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.match(value) is None:
        raise _bad(detail)
    return value


def _seed(value: object, detail: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _bad(detail)
    return value


def _json_safe(value: Any) -> Any:
    """Recursively fold a backend's event mapping into plain `dict`/`list`.

    `TurnResult.__post_init__` (`rollout.Turn`) freezes every event through
    `_frozen_mapping`, which turns any nested mapping into a `MappingProxyType`
    and any nested list/tuple into a `tuple`. `canonical_body`'s `json.dumps`
    only special-cases `dict`, so a shallow `dict(event)` still holds
    unserializable nested values whenever a backend's event carries one (a
    tool call's `arguments`, for instance) — this walks the whole value so
    `canonical_body` always sees a plain, serializable structure.
    """
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _inflate(payload: bytes, extra: int) -> bytes:
    """Append `extra` spaces inside one string field of a 200 body (`oversize_bytes`).

    Preference goes to a field the wire declares free-form — a turn's answer
    first, then the first top-level string in sorted-key order, which for a
    health body is `allowed_artifact_root` — so those two bodies stay
    schema-valid and only their size changes. An identity, session or release
    body has no free-form string, so a grammar-bound one is padded instead: the
    adapter is meant to abort on the byte count long before it parses, and this
    hook exists to prove exactly that.
    """
    body = json.loads(payload)
    events = body.get("events")
    if (
        isinstance(events, list)
        and events
        and isinstance(events[0], dict)
        and isinstance(events[0].get("content"), str)
    ):
        events[0]["content"] += " " * extra
        return canonical_body(body)
    for key in sorted(body):
        if isinstance(body[key], str):
            body[key] += " " * extra
            return canonical_body(body)
    return payload


@dataclass(frozen=True)
class Misbehave:
    """Deliberate contract violations a *test* target may be configured with.

    The product `ReferenceTarget` exposes one frozen neutral value and cannot be
    changed; `tests.reference_target_faults.FaultInjectingReferenceTarget`
    overrides `misbehave` with a per-instance value it replaces through
    `fault(**changes)`. `dispatch` and the transport read the hooks through
    `self.misbehave` at request time, so a replaced value applies to the next
    request.
    """

    lie_candidate_digest: bool = False
    drop_attested: bool = False
    internal_error: bool = False
    sleep_seconds: float = 0.0
    oversize_bytes: int = 0
    slow_drip_seconds: float = 0.0
    slow_drip_headers_seconds: float = 0.0
    chunked_response: bool = False
    response_content_type: str = "application/json"
    # `None` keeps `response_content_type` for refusals too; a value applies to
    # every non-200, which is the one target shape that answers its successes
    # under the contract's media type and its refusals under another.
    error_content_type: str | None = None
    # An `unpaired` session that names no effective seed: the mode says the
    # target chose one, so the adapter must refuse the answer.
    drop_effective_seed: bool = False
    # Refuse every instance `DELETE` after this many have succeeded (`None`
    # never refuses). A target that releases what a client asks for and then
    # keeps the next instances alive is the one shape a conformance run cannot
    # notice by reading its own checks: releases are cleanup, not verdicts.
    # Session closes are not counted; only `DELETE /instances/{id}`.
    refuse_release_after: int | None = None
    # Never release the N-th instance created (1-based): every `DELETE` of it
    # answers 500, the others are served. The shape `refuse_release_after`
    # cannot make — a single kept instance whose releasing check reports
    # nothing, so every check passes and only the sweep's re-read of `health`
    # tells that the target kept something. The sweep's own retry is refused
    # too, which is what makes the instance a leak rather than a hiccup.
    keep_nth_instance: int | None = None
    # Append `<run prefix of the last created instance>` + this suffix to the
    # `health` instance list: a name that wears a run's prefix without being one
    # of its ids, which the conformance sweep must never repeat verbatim.
    forge_health_instance: str | None = None


NEUTRAL_MISBEHAVE = Misbehave()


@dataclass(frozen=True)
class _Record:
    """A served `request_id`: the canonical request bytes and the exact reply."""

    canonical: bytes
    status: int
    body: bytes


@dataclass
class _Session:
    session_id: str
    public: dict[str, Any]
    requested_seed: int | None
    effective_seed: int | None
    backend: BackendSession


@dataclass
class _Instance:
    instance_id: str
    root: Path
    policy: ArtifactPolicyV1
    candidate_digest: str
    dependency_digest: str
    file_count: int
    byte_count: int
    # `None` while loading (between admission and publication): a backend
    # callback is only ever reachable through `instance.lock`, and nothing
    # reads this field without holding it, so the `None` window is invisible
    # outside `_create_instance` itself.
    backend: BackendInstance | None
    snapshot: Path
    # Every `request_id` whose creation resolved to this instance, guarded by
    # the *global* lock (like `_creation_records`, which it indexes): release
    # drops the instance and its records together, so the two must be mutated
    # under the same lock. They are dropped when the instance is un-published,
    # in the same block that clears `live`, not after its teardown: a creation
    # replay must not outlive the publication it replays (ADR-0007).
    creation_request_ids: set[str]
    # The optional `describe()` result (ADR-0009, amended), validated and
    # frozen at load time; `None` when the backend has no `describe` at all,
    # which is exactly as conforming as one that has it. Never compared —
    # `_identity_body` only ever emits it.
    description: Mapping[str, Any] | None = None
    # The optional `components()` map (ADR-0024), captured once like `description`.
    components: Mapping[str, Any] | None = None
    created_body: bytes = b""
    released: bool = False
    # Set under the global lock at publication, once loading succeeds and the
    # server is not closed; never set for an instance whose load failed or
    # whose publication lost the race to `close()`; cleared again under the
    # global lock at the start of a release, before its teardown and together
    # with this instance's creation records, so a same-id admission reading
    # the table sees a releasing instance as pending rather than reusable and
    # a resend of its own creation `request_id` has nothing to replay.
    # `_live` refuses any instance that is not (yet, or any longer) live.
    live: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    sessions: dict[str, _Session] = field(default_factory=dict, repr=False)
    records: dict[str, _Record] = field(default_factory=dict, repr=False)


def _discard_owned_root(root: Path) -> None:
    """Remove a server's own snapshot directory when its `TargetServer` is
    collected without `close()` ever being called.

    A module-level function on purpose: `weakref.finalize` keeps its arguments
    alive, so a bound method (or any closure over `self`) would make the server
    immortal and the finalizer would never run. Failures are suppressed for the
    same reason `close()` suppresses them — a finalizer runs at an arbitrary
    point, possibly during interpreter shutdown, and has no caller to report to.
    """
    with contextlib.suppress(OSError, ArtifactError):
        remove_tree(root)


def _close_quietly(obj: object) -> None:
    """Best-effort `close()` of an object the server is discarding.

    A backend that handed back a non-conforming instance or session may still
    hold a resource inside it, so the server closes it when it has a callable
    `close` and swallows whatever that raises: the request's own answer is
    already decided, and a failed cleanup must not replace it.

    The *attribute lookup* is inside the suppression, not only the call. An
    object whose `close` is a descriptor that raises — the very shape
    `_require_instance_shape` rejects when it reads that attribute — would
    otherwise escape this function and skip the cleanup its caller still owes
    (ADR-0009: a non-conforming callback leaves no instance, no session and no
    record behind). Callers additionally guarantee that cleanup with a
    `finally`, so this function raising could no longer strand an entry; both
    belong here, because "best effort" must mean the whole effort.
    """
    with contextlib.suppress(Exception):
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _require_instance_shape(loaded: object) -> None:
    """Refuse a `load` result that is not a `BackendInstance` (ADR-0009).

    Structural, not `isinstance`: `BackendInstance` is a `Protocol` a backend
    satisfies by shape and never has to inherit from, so the honest check is
    that the two callbacks the server will later call exist and are callable.
    A non-conforming result is handled exactly like a raising `load` — without
    this check, `load` returning `None` would publish a live instance whose
    every session request answers `unknown_instance`.
    """
    if not callable(getattr(loaded, "open", None)) or not callable(getattr(loaded, "close", None)):
        raise ValueError("backend.load did not return a BackendInstance")


def _instance_description(loaded: object) -> Mapping[str, Any] | None:
    """The optional `describe()` (`DescribingInstance`, ADR-0009 amended):
    called once, validated for shape and size only, and never compared.

    A backend without the method is served exactly as before — this is
    structural detection, the same way `_require_instance_shape` detects
    `open`/`close`, and `describe` is deliberately absent from that function:
    a backend without it is fully conforming. A `describe` that raises, or
    that returns something `validate_instance_description` refuses, fails
    instance creation on the same path a raising `load` already takes — a
    target that cannot state its own shape correctly is a broken target.
    """
    describe = getattr(loaded, "describe", None)
    if not callable(describe):
        return None
    return validate_instance_description(describe())


def _instance_components(loaded: object) -> Mapping[str, Any] | None:
    """The optional `components()` (`ComponentsInstance`): called once,
    validated for shape and size only, and never compared. A raising or
    non-conforming result, `None` included, fails creation on the path a raising
    `load` takes; only an absent or non-callable attribute means "no callback"."""
    components = getattr(loaded, "components", None)
    if not callable(components):
        return None
    value = components()
    if value is None:
        raise ValueError("components() returned None")
    return validate_instance_components(value)


def _require_session_shape(opened: object) -> None:
    """The same rule for an `open` result: the session must carry the `turn`
    and `close` the server will call, or it is never registered.

    `effective_seed` is not read here because reading it is the very next thing
    `_open_session` does, under the same handler: an object without the
    attribute, or with a property that raises, fails there and takes the same
    close-and-refuse path as a non-conforming shape.
    """
    if not callable(getattr(opened, "turn", None)) or not callable(getattr(opened, "close", None)):
        raise ValueError("backend.open did not return a BackendSession")


def _replay(records: dict[str, _Record], request_id: str, canonical: bytes) -> _Record | None:
    record = records.get(request_id)
    if record is None:
        return None
    if record.canonical != canonical:
        raise _Refused("request_conflict", 409, "request_id")
    return record


class TargetServer:
    """A complete `/evolution/v2` target over in-memory state and a backend."""

    def __init__(
        self,
        backend: TargetBackend,
        *,
        allowed_artifact_root: Path,
        policy: ArtifactPolicyV1,
        seed_mode: SeedMode,
        target_id: str,
        contract_digest: str,
        settings_hash: str,
        runtime_ref_digest: str,
        max_instances: int = 4,
        max_concurrent_sessions: int = 8,
        max_input_bytes: int = 1 << 20,
        max_output_bytes: int = 1 << 20,
        tombstones: int = 1024,
        snapshot_root: Path | None = None,
    ) -> None:
        self.backend = backend
        root = Path(allowed_artifact_root)
        if not root.is_absolute():
            raise ValueError("allowed_artifact_root must be absolute")
        # Resolved once at start-up: containment is then decided against a path
        # that has no symlink left in it, so only components *below* it need the
        # per-component `lstat` walk.
        self.allowed_artifact_root = Path(os.path.realpath(root))
        if not isinstance(policy, ArtifactPolicyV1):
            raise ValueError("policy must be an ArtifactPolicyV1")
        self.policy = policy
        if seed_mode not in SEED_MODES:
            raise ValueError(f"unknown seed_mode: {seed_mode!r}")
        self.seed_mode: SeedMode = seed_mode
        for name, value, minimum in (
            ("max_instances", max_instances, 1),
            ("max_concurrent_sessions", max_concurrent_sessions, 1),
            ("max_input_bytes", max_input_bytes, 1),
            ("max_output_bytes", max_output_bytes, 1),
            ("tombstones", tombstones, 1),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        self.max_instances = max_instances
        self.max_concurrent_sessions = max_concurrent_sessions
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        for name, text in (
            ("target_id", target_id),
            ("contract_digest", contract_digest),
            ("settings_hash", settings_hash),
            ("runtime_ref_digest", runtime_ref_digest),
        ):
            if not isinstance(text, str) or IDENTIFIER.match(text) is None:
                raise ValueError(f"{name} must match the identifier grammar")
        self.target_id = target_id
        self.contract_digest = contract_digest
        self.settings_hash = settings_hash
        self.runtime_ref_digest = runtime_ref_digest
        self.boot_id = uuid4().hex

        # The server always owns the directory it puts snapshots in, so that
        # neither `close()` nor a failed creation ever removes a directory this
        # server did not create. A caller who names a `snapshot_root` names a
        # *container*: the server creates one `snapshot-<boot_id>` subdirectory
        # under it and owns only that, which is what lets two servers share one
        # caller-named root without either deleting the other's live snapshot.
        # The placement refusal is evaluated against the caller's directory
        # (that is the path a caller can get wrong) and before any directory of
        # ours exists, so a refused construction leaves nothing behind.
        if snapshot_root is not None:
            container = Path(os.path.realpath(snapshot_root))
            if not container.is_dir() or not os.access(container, os.W_OK):
                raise ValueError("snapshot_root must be an existing writable directory")
            if (
                container == self.allowed_artifact_root
                or container in self.allowed_artifact_root.parents
            ):
                raise ValueError("snapshot_root must not equal or contain allowed_artifact_root")
            owned = container / f"snapshot-{self.boot_id}"
            owned.mkdir(mode=0o700, exist_ok=False)
            self.snapshot_root = owned
        else:
            # A directory `mkdtemp` has just created is empty, so it can
            # neither equal nor contain the allowed root: the refusal above is
            # the only place that rule can fire.
            self.snapshot_root = Path(tempfile.mkdtemp(prefix="evo-target-", dir=None))
        # `close()` is the intended release; this is the backstop for a server
        # that is dropped without it (a test fixture, an aborted script), so an
        # owned snapshot root is not leaked for the life of the process.
        self._finalizer = weakref.finalize(self, _discard_owned_root, self.snapshot_root)

        self._lock = threading.RLock()
        self._instances: dict[str, _Instance] = {}
        self._last_run_prefix = ""
        self._creation_records: dict[str, _Record] = {}
        # A creation `request_id` reserved by an admission that is still
        # loading: request id -> that loading instance. Admission bookkeeping
        # under the global lock like `_creation_records`, and the stand-in for
        # the record that instance has not written yet (ADR-0007). Entered in
        # the same locked block that inserts the loading table entry, dropped
        # in the block that publishes it (the record replaces it) or wherever
        # that entry is unlinked.
        self._pending_creations: dict[str, _Instance] = {}
        self._tombstones: deque[str] = deque(maxlen=tombstones)
        self._releases_served = 0
        self._instances_created = 0
        self._kept: set[str] = set()
        self._closed = False

    @property
    def misbehave(self) -> Misbehave:
        """The packaged target never misbehaves; subclasses in tests may.

        `NEUTRAL_MISBEHAVE` is one frozen process-wide constant shared by every
        product target instance, not a per-instance value: it is a design
        boundary that keeps the packaged target's behavior fixed, not a
        security boundary.
        """
        return NEUTRAL_MISBEHAVE

    # -- dispatch --------------------------------------------------------------

    def dispatch(self, method: str, path: str, raw_body: bytes = b"") -> tuple[int, bytes]:
        """Handle one request. Never raises: every failure becomes a typed wire error.

        `sleep_seconds` and `oversize_bytes` are applied here rather than inside
        one handler: the adapter must be provable against `callee_timeout` and
        `response_too_large` on *every* endpoint, not only on a turn. `started`
        is captured before the stall and threaded to the turn handler, so the
        `latency_ms` a turn reports is the latency the client actually saw.
        """
        started = time.monotonic()
        misbehave = self.misbehave
        try:
            with self._lock:
                closed = self._closed
            if closed:
                # Every path, `health` included: `close()` is the one boundary
                # (ADR-0009) — once it has set `_closed`, no later dispatch may
                # observe any instance state, live or not.
                raise _Refused("not_ready", 503, "closed")
            if misbehave.internal_error:
                raise _Refused("internal", 500, "internal")
            if misbehave.sleep_seconds > 0:
                time.sleep(misbehave.sleep_seconds)
            status, payload = self._route(method, path, raw_body, started)
        except _Refused as exc:
            return exc.status, _error_payload(exc.code, exc.detail)
        except Exception:
            # A fixture bug must still leave the wire well-formed: the caller is a
            # conformance client that must see a typed code, not a torn connection.
            return 500, _error_payload("internal", "internal")
        if status == 200 and misbehave.oversize_bytes > 0:
            payload = _inflate(payload, misbehave.oversize_bytes)
        return status, payload

    def _route(self, method: str, path: str, raw_body: bytes, started: float) -> tuple[int, bytes]:
        # The wire defines no query parameters; anything after `?` is ignored
        # rather than allowed to change routing.
        clean = path.split("?", 1)[0]
        if not clean.startswith(PREFIX):
            raise _Refused("invalid_request", 404, "path")
        tail = clean[len(PREFIX) :].strip("/")
        parts = tail.split("/") if tail else []
        if parts == ["health"] and method == "GET":
            return 200, self._health()
        if parts == ["instances"] and method == "POST":
            return self._create_instance(raw_body)
        if len(parts) >= 2 and parts[0] == "instances":
            instance_id = _identifier(parts[1], "instance_id")
            if len(parts) == 2:
                if method == "GET":
                    return 200, self._identity_of(instance_id)
                if method == "DELETE":
                    self._release_hook(instance_id)
                    return 200, self._release(instance_id)
            elif len(parts) == 3 and parts[2] == "sessions" and method == "POST":
                return self._open_session(instance_id, raw_body)
            elif len(parts) == 4 and parts[2] == "sessions" and method == "DELETE":
                return 200, self._close_session(instance_id, _identifier(parts[3], "session_id"))
            elif len(parts) == 5 and parts[2] == "sessions" and parts[4] == "turns":
                session_id = _identifier(parts[3], "session_id")
                if method == "POST":
                    return self._turn(instance_id, session_id, raw_body, started)
        raise _Refused("invalid_request", 404, "path")

    # -- bodies ----------------------------------------------------------------

    def _health(self) -> bytes:
        with self._lock:
            live = sorted(i for i, inst in self._instances.items() if inst.live)
            forged = self.misbehave.forge_health_instance
            if forged is not None and self._last_run_prefix:
                live.append(self._last_run_prefix + forged)
        return canonical_body(
            {
                "ready": True,
                "protocol_version": PROTOCOL_VERSION,
                "target_id": self.target_id,
                "boot_id": self.boot_id,
                "contract_digest": self.contract_digest,
                "settings_hash": self.settings_hash,
                "runtime_ref_digest": self.runtime_ref_digest,
                "seed_mode": self.seed_mode,
                "allowed_artifact_root": str(self.allowed_artifact_root),
                "policy_digest": self.policy.digest,
                "capabilities": {
                    "max_instances": self.max_instances,
                    "max_concurrent_sessions": self.max_concurrent_sessions,
                    "materialization": [STRATEGY],
                    "attests": list(REQUIRED_ATTESTS),
                    "max_input_bytes": self.max_input_bytes,
                    "max_output_bytes": self.max_output_bytes,
                    "instantiable": True,
                },
                "instances": live,
            }
        )

    def _identity_body(self, instance: _Instance) -> bytes:
        """The instance identity, including the six semantic fields and `boot_id`."""
        misbehave = self.misbehave
        digest = _LIE_DIGEST if misbehave.lie_candidate_digest else instance.candidate_digest
        attested: list[str] = [] if misbehave.drop_attested else list(REQUIRED_ATTESTS)
        body: dict[str, Any] = {
            "instance_id": instance.instance_id,
            "boot_id": self.boot_id,
            "contract_digest": self.contract_digest,
            "settings_hash": self.settings_hash,
            "runtime_ref_digest": self.runtime_ref_digest,
            "seed_mode": self.seed_mode,
            "protocol_version": PROTOCOL_VERSION,
            "strategy": STRATEGY,
            "loaded_candidate_digest": digest,
            "dependency_digest": instance.dependency_digest,
            "attested": attested,
            "file_count": instance.file_count,
            "byte_count": instance.byte_count,
        }
        # Omitted entirely when the backend has no `describe` (ADR-0009,
        # amended), so an old client's closed key set still parses this body —
        # `description` is optional in `wire_v2.parse_instance` for the same
        # reason.
        if instance.description is not None:
            body["description"] = dict(instance.description)
        if instance.components is not None:
            body["components"] = {
                key: {"sha256": item["sha256"], "sources": list(item["sources"])}
                for key, item in instance.components.items()
            }
        return canonical_body(body)

    # -- instances -------------------------------------------------------------

    def _contained(self, candidate_root: str) -> Path:
        """Prove the requested root lies inside the allowed root with no symlink below it.

        The allowed root is already a realpath, so the check is textual down to
        it and then a component-by-component `lstat` walk: any symlink, any
        non-directory and any unreadable component is a refusal. Resolving the
        requested path instead would let a symlink inside the allowed root point
        anywhere and still "resolve" inside it on a second call.
        """
        given = Path(candidate_root)
        allowed = self.allowed_artifact_root
        refused = _Refused("root_not_allowed", 403, "candidate_root")
        if not given.is_absolute() or ".." in given.parts:
            raise refused
        try:
            relative = given.relative_to(allowed)
        except ValueError:
            raise refused from None
        current = allowed
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise refused
            for part in relative.parts:
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise refused
        except OSError:
            raise refused from None
        return current

    def _open_for_admission(self) -> None:
        """Under the global lock: refuse every admission after `close()`."""
        if self._closed:
            raise _Refused("not_ready", 503, "closed")

    def _unlink_failed(self, instance: _Instance, *, remove_snapshot: bool = True) -> None:
        """A load that did not publish — it raised, handed back a
        non-conforming object, or lost the race to `close()`: the instance was
        never published, so it leaves no table entry, no record and no
        reserved creation `request_id` — only the snapshot remains to
        remove. `_create_instance`'s `finally` calls this
        on every such path, so the guarantee does not depend on which one it
        was.

        The removal happens *first*, while this thread still holds the
        instance's lock, and the table entry is dropped after it: a `close()`
        concurrent with this failure then either finds the entry and waits on
        that lock (so the two never remove trees inside one another), or finds
        it already gone and knows this removal is finished. `remove_tree` is
        best-effort: a race with `close()` already removing the same snapshot,
        or any other failure, must not replace the caller's intended 400
        `candidate_root` (or 500 `internal`) with an unrelated crash.

        `remove_snapshot=False` is the one case where the server must *not*
        remove the destination: `snapshot_tree` refused an already-existing
        one, which means those bytes belong to another owner (a second server
        sharing a caller-named container), not to this copy.
        """
        if remove_snapshot:
            with contextlib.suppress(Exception):
                remove_tree(instance.snapshot)
        with self._lock:
            self._instances.pop(instance.instance_id, None)
            # The entry and the `request_id` it reserved go together: a
            # contender waiting on this instance's lock must wake to a table
            # with neither, and is then free to create the instance this load
            # failed to. Guarded on identity, so a reservation another
            # in-flight creation owns is never taken from it.
            for recorded in instance.creation_request_ids:
                if self._pending_creations.get(recorded) is instance:
                    del self._pending_creations[recorded]

    def _teardown(self, instance: _Instance) -> None:
        """Close every backend session, then the backend instance, then remove
        the snapshot; each step swallows its own exceptions so one
        misbehaving backend — or a filesystem failure removing the snapshot —
        cannot block the bookkeeping every release or close must still do.
        Never called under the global lock: every callback here runs under
        `instance.lock` alone (ADR-0009)."""
        for session in list(instance.sessions.values()):
            with contextlib.suppress(Exception):
                session.backend.close()
        instance.sessions.clear()
        if instance.backend is not None:
            with contextlib.suppress(Exception):
                instance.backend.close()
        with contextlib.suppress(Exception):
            remove_tree(instance.snapshot)

    def _create_instance(self, raw_body: bytes) -> tuple[int, bytes]:
        """Admission in three phases (ADR-0009), then loading, then publication.

        *Phase A (global lock): the record decides before the request's own
        subject is judged.* A replay "returns the byte-identical original
        response, without re-executing anything", so only what the record is
        *keyed* by runs ahead of the lookup: the object, the two identifiers
        and the canonical body. Validating the mutable resource first would
        let a renamed `candidate_root` turn a recorded 200 into 403
        `root_not_allowed`, and would answer a reused `request_id` with the
        refusal the *new* body earns instead of the 409 `request_conflict`
        the contract requires (proved by
        `test_create_replays_after_the_published_root_moved_away`). A
        `request_id` phase C has *reserved* for a still-loading creation
        decides here for the same reason and ahead of the same checks: its
        record is not written yet, so the request waits on that instance's
        lock outside the lock and loops, and the next pass replays it,
        refuses it 409 `request_conflict`, or finds the reservation gone. The
        tombstone keeps its precedence: a released instance has no live
        records left, so the two never both apply. What else this phase
        judges is exactly the body checks that read no filesystem — that
        `candidate_root` is a non-empty string, the `strategy`, and the
        `instance_id` dot-name guard, which is lexical `Path` arithmetic over
        an attribute fixed at construction.

        *Phase B (no lock at all): every check that touches the filesystem or
        parses caller data.* `ArtifactPolicyV1.from_payload` with its digest
        comparison, `_contained`'s per-component `lstat` walk and the
        snapshot-root containment check run here, outside both locks. Each
        refusal is the same code it was; what changes is that a wedged
        automount under `allowed_artifact_root` can no longer hold the global
        lock and stall `health`, an unrelated creation or a release — the
        target-wide liveness failure the lock split exists to prevent. Nothing
        in this phase mutates server state, so running it unlocked (and again
        on a later pass of the loop) is free of consequence.

        *Phase C (global lock): re-decide, then mutate.* `_closed`, the
        tombstone, the replay and the `request_id` reservation are consulted a
        second time because phase B held no lock: a record written meanwhile
        is returned rather than turned into a second instance, a reservation
        entered meanwhile is waited on rather than raced, and a tombstone
        written meanwhile is `request_expired`. The reservation is read before
        the instance table, because the `request_id` decides ahead of the
        instance id. Then the existing-instance, pending and capacity logic
        runs and, for a brand-new id, a not-yet-live `_Instance` is inserted,
        its lock taken, and its creation `request_id` reserved in
        `_pending_creations` — all before the global lock is released, so
        every later admission for the same id, *and* every one reusing that
        `request_id` for any id, finds it and waits on that lock instead of
        racing it (ADR-0007: two concurrent creations sharing a `request_id`
        must not both publish). Waiting on a pending instance's lock happens
        outside all three phases, as before.

        Loading (instance lock only, global lock released): the requested
        tree is snapshotted and handed to the backend. No global state is
        touched here, so a slow or blocked backend never stalls any other
        instance, `health`, or a fresh id's own admission. Past the snapshot
        copy the cleanup is a `finally` keyed on publication: whatever the
        backend, the shape check or the quiet close of a rejected object does,
        a load that did not publish removes its snapshot and its table entry
        (ADR-0009), so no non-live entry with a free lock is ever left for a
        later same-id admission to wait on forever.

        Publication (global lock, briefly): a successful load is invisible
        until this step marks the instance `live` and records the reply — and
        never happens at all if `close()` won the race, in which case the
        instance is torn down here and the caller sees `not_ready`.

        The admission loop terminates: every path out of it either removes the
        instance's table entry and its reservation or marks it live and
        replaces the reservation with a record, in both cases before
        releasing that instance's lock, so an iteration that waited on a
        pending instance finds a decided table on its next pass, and
        `max_instances` bounds how many undecided entries can exist at once.
        """
        obj = _object(raw_body)
        request_id, instance_id, candidate_root, policy_payload, strategy = _fields(
            obj, "request_id", "instance_id", "candidate_root", "policy", "strategy"
        )
        _identifier(request_id, "request_id")
        _identifier(instance_id, "instance_id")
        canonical = canonical_body(obj)
        while True:
            # -- phase A: the record and the filesystem-free body checks ----
            with self._lock:
                self._open_for_admission()
                if instance_id in self._tombstones:
                    raise _Refused("request_expired", 410, "instance_id")
                record = _replay(self._creation_records, request_id, canonical)
                if record is not None:
                    return record.status, record.body
                # An in-flight creation already holds this `request_id`:
                # phase C reserved it in the same locked block that inserted
                # its loading table entry, and its record is not written yet,
                # so there is nothing here to replay and nothing to compare
                # against. The reservation decides before this request's own
                # subject is judged, exactly as the record above does. Wait on
                # that instance's lock outside the global lock and loop: the
                # next pass either finds the record (a replay, or 409
                # `request_conflict` for a different body) or finds the
                # reservation gone, dropped by a load that published nothing,
                # and is free to proceed (ADR-0007).
                reserved = self._pending_creations.get(request_id)
                if reserved is None:
                    if not isinstance(candidate_root, str) or not candidate_root:
                        raise _bad("candidate_root")
                    if strategy != STRATEGY:
                        raise _bad("strategy")
                    # The identifier grammar admits "." and "..". "." aliases the
                    # snapshot root itself (`Path` drops a trailing "." at
                    # construction, so `snapshot_root / "."` *is* `snapshot_root`).
                    # ".." is worse and not caught by the containment check in
                    # phase B: `Path` does *not* normalise a trailing "..", so
                    # `snapshot_root / ".."` names the snapshot root's *parent*,
                    # and `Path.parent` strips that ".." lexically without
                    # resolving it — `(snapshot_root / "..").parent ==
                    # snapshot_root` holds even though the path itself names the
                    # parent. A load failure removes `instance.snapshot` with
                    # `shutil.rmtree`, so an unrefused ".." would let a failed
                    # candidate delete everything in the snapshot root's parent
                    # directory. Refuse both explicitly by name, in addition to
                    # the general containment check. Pure path arithmetic over
                    # `self.snapshot_root`, which never changes after
                    # construction, so it belongs in the locked phase.
                    if (
                        instance_id in {".", ".."}
                        or (self.snapshot_root / instance_id).parent != self.snapshot_root
                    ):
                        raise _bad("instance_id")
            if reserved is not None:
                with reserved.lock:
                    pass
                continue
            # -- phase B: parsing and filesystem checks, no lock held -------
            try:
                policy = ArtifactPolicyV1.from_payload(policy_payload)
            except (AttributeError, KeyError, TypeError, ValueError):
                raise _bad("policy") from None
            if policy.digest != self.policy.digest:
                raise _Refused("policy_mismatch", 403, "policy")
            root = self._contained(candidate_root)
            if root == self.snapshot_root or self.snapshot_root.is_relative_to(root):
                raise _Refused("root_not_allowed", 403, "candidate_root")
            # -- phase C: re-decide under the lock, then mutate the table ----
            with self._lock:
                self._open_for_admission()
                if instance_id in self._tombstones:
                    raise _Refused("request_expired", 410, "instance_id")
                record = _replay(self._creation_records, request_id, canonical)
                if record is not None:
                    return record.status, record.body
                # The reservation again (phase B held no lock, so one may
                # have been entered meanwhile), and for the same reason the
                # record is re-read: this request must not become a second
                # instance sharing a `request_id` whose owner has not
                # published yet. It is consulted *before* the instance table
                # because the `request_id` decides ahead of the instance id —
                # a reserved id with a brand-new `instance_id` is the very
                # race this reservation exists for, and both concurrent
                # creations would otherwise pass here and publish
                # (ADR-0007).
                pending: _Instance | None = self._pending_creations.get(request_id)
                existing = self._instances.get(instance_id) if pending is None else None
                if existing is not None and not existing.live:
                    # Still loading (or its load just finished and is racing
                    # publication): wait outside the global lock, then loop
                    # back and look again — never block the table on it.
                    pending = existing
                elif existing is not None:
                    if existing.root == root and existing.policy == policy:
                        # A served request, so it is recorded like any other:
                        # The contract makes every POST idempotent on its `request_id`
                        # while the owning instance is live, and an id that
                        # resolved here must therefore be 409
                        # `request_conflict` when it is later reused for a
                        # different creation — not a second instance.
                        self._creation_records[request_id] = _Record(
                            canonical, 200, existing.created_body
                        )
                        existing.creation_request_ids.add(request_id)
                        return 200, existing.created_body
                    raise _Refused("instance_conflict", 409, "instance_id")
                elif pending is None:
                    if len(self._instances) >= self.max_instances:
                        raise _Refused("instances_exhausted", 429, "instances")
                    instance = _Instance(
                        instance_id=instance_id,
                        root=root,
                        policy=policy,
                        candidate_digest="",
                        dependency_digest="",
                        file_count=0,
                        byte_count=0,
                        backend=None,
                        snapshot=self.snapshot_root / instance_id,
                        creation_request_ids={request_id},
                    )
                    instance.lock.acquire()
                    self._instances[instance_id] = instance
                    # The reservation and the loading entry are entered under
                    # one lock: from here no concurrent creation reusing this
                    # `request_id` can pass phase A or phase C, whatever
                    # `instance_id` it names, until this load publishes a
                    # record or fails and drops both (ADR-0007).
                    self._pending_creations[request_id] = instance
            if pending is not None:
                with pending.lock:
                    pass
                continue
            break
        try:
            # The snapshot copy stands in its own block, so `FileExistsError`
            # here means the one thing it can mean: `snapshot_tree` refused a
            # destination that already existed. A `FileExistsError` a *backend*
            # raises is a backend failure like any other and takes the cleanup
            # below — read as a collision, it would leave this copy's snapshot
            # behind, and every later creation of the same `instance_id` would
            # then find that abandoned destination and answer `internal` until
            # the server closed.
            try:
                scan = snapshot_tree(root, policy, instance.snapshot)
            except FileExistsError:
                # The destination already existed, so `snapshot_tree` refused
                # before it created anything: those bytes are not this copy's
                # to delete (a second server sharing a caller-named container
                # owns a live snapshot there). Forget the table entry and
                # answer `internal` — the server never removes a destination
                # its own copy did not create.
                self._unlink_failed(instance, remove_snapshot=False)
                raise _Refused("internal", 500, "internal") from None
            except ArtifactUnsafeError:
                self._unlink_failed(instance)
                raise _bad("candidate_root") from None
            except Exception:
                self._unlink_failed(instance)
                raise _Refused("internal", 500, "internal") from None
            # From here to publication the cleanup is unconditional: a
            # `finally` keyed on `published` runs `_unlink_failed` on every
            # path that did not publish (ADR-0009). It replaces the cleanup
            # that used to sit in the `except` arm below, which a
            # `_close_quietly` of its own — or anything else raising between
            # the shape check and publication — could skip, leaving a non-live
            # table entry whose lock is free and a later same-id admission
            # looping on it forever. `remove_snapshot` is not in question here:
            # `snapshot_tree` above succeeded, so this copy owns those bytes.
            loaded: BackendInstance | None = None
            published = False
            try:
                try:
                    tree = LoadedTree(
                        instance_id=instance_id,
                        root=instance.snapshot,
                        policy=policy,
                        candidate_digest=scan.candidate_digest,
                        dependency_digest=scan.dependency_digest,
                        artifact=scan.artifact,
                        dependency=scan.dependency,
                        max_output_bytes=self.max_output_bytes,
                        seed_mode=self.seed_mode,
                    )
                    loaded = self.backend.load(tree)
                    _require_instance_shape(loaded)
                    description = _instance_description(loaded)
                    components = _instance_components(loaded)
                    instance.backend = loaded
                    instance.candidate_digest = scan.candidate_digest
                    instance.dependency_digest = scan.dependency_digest
                    instance.file_count = scan.file_count
                    instance.byte_count = scan.byte_count
                    instance.description = description
                    instance.components = components
                    created_body = self._identity_body(instance)
                    # A reported `components` map counts toward the client's
                    # response cap like a turn body: a valid map can make the
                    # creation body larger than a low `max_output_bytes`. The
                    # plain identity body is not measured, so a target that
                    # reports no components behaves exactly as before.
                    if components is not None and len(created_body) > self.max_output_bytes:
                        raise ValueError("creation body over max_output_bytes")
                    instance.created_body = created_body
                except Exception:
                    # A raising *or* non-conforming `load` leaves no instance,
                    # no record and no snapshot behind (ADR-0009); whatever
                    # the backend did hand back is closed first, since nothing
                    # on the server will ever reach it again. The instance and
                    # its snapshot are dropped by the `finally`, so that
                    # happens even if this close does not return normally.
                    _close_quietly(loaded)
                    raise _Refused("internal", 500, "internal") from None
                with self._lock:
                    # `_teardown` calls into the backend (`close`); it must
                    # never run while `self._lock` is held (ADR-0009), so this
                    # block only decides — under the table lock — whether
                    # publication or teardown applies, and does the chosen one
                    # after. The entry a lost race leaves behind is dropped
                    # *after* that teardown (by the `finally`), not here:
                    # while it is still in the table a concurrent `close()`
                    # waits on this instance's lock instead of removing the
                    # snapshot root from under the teardown.
                    closed_after_load = self._closed
                    if not closed_after_load:
                        instance.live = True
                        published = True
                        self._instances_created += 1
                        if self._instances_created == self.misbehave.keep_nth_instance:
                            self._kept.add(instance_id)
                        self._last_run_prefix = instance_id.rsplit("-", 1)[0] + "-"
                        self._creation_records[request_id] = _Record(
                            canonical, 200, instance.created_body
                        )
                    # The reservation ends here on either arm, in the same
                    # block that decided: published, the record above
                    # replaces it and a contender replays; lost to `close()`,
                    # nothing will ever replace it and a contender waiting on
                    # this instance's lock is free to create its own (the
                    # `finally`'s `_unlink_failed` drops it too, idempotently
                    # — this is the drop that matters, because it happens
                    # before the teardown rather than after it).
                    if self._pending_creations.get(request_id) is instance:
                        del self._pending_creations[request_id]
                if closed_after_load:
                    self._teardown(instance)
                    raise _Refused("not_ready", 503, "closed")
                return 200, instance.created_body
            finally:
                if not published:
                    self._unlink_failed(instance)
        finally:
            instance.lock.release()

    def _acquire(self, instance_id: str) -> _Instance:
        """Look the instance up under the global lock and hand it back unlocked."""
        with self._lock:
            instance = self._instances.get(instance_id)
        if instance is None:
            raise _Refused("unknown_instance", 404, "instance_id")
        return instance

    def _live(self, instance: _Instance) -> None:
        """Under the instance lock: refuse an instance that is released, or
        not (yet, or ever) published — a release may have won a race with
        this handler, or this handler may have caught the instance while it
        was still loading and is only now getting its turn at the lock."""
        if instance.released or not instance.live:
            raise _Refused("unknown_instance", 404, "instance_id")

    def _identity_of(self, instance_id: str) -> bytes:
        instance = self._acquire(instance_id)
        with instance.lock:
            self._live(instance)
            return self._identity_body(instance)

    def _release_hook(self, instance_id: str) -> None:
        """`keep_nth_instance`: refuse every release of the kept instance.
        `refuse_release_after`: succeed N times, then refuse every release."""
        with self._lock:
            if instance_id in self._kept:
                raise _Refused("internal", 500, "internal")
            limit = self.misbehave.refuse_release_after
            if limit is None:
                return
            if self._releases_served >= limit:
                raise _Refused("internal", 500, "internal")
            self._releases_served += 1

    def _release(self, instance_id: str) -> bytes:
        """Instance lock first, global lock inside it (ADR-0009's one allowed
        reversal of the handler order): the global lock is only ever taken
        here for table lookups and mutations, never held while waiting on
        `instance.lock`, so this can never deadlock against admission or
        publication taking the locks the other way round.

        The release un-publishes *before* it tears down. `released` alone is
        an instance-lock fact, and a same-id admission reads the table under
        the global lock without taking that lock: it would see `live` still
        true for the whole of a teardown and answer 200 with the identity of
        an instance already being closed — a 200 the very next request
        answers `unknown_instance`. Clearing `live` under the global lock (a
        table decision, taken briefly from inside the instance lock, never a
        wait) makes that admission treat the entry as pending: it waits on
        this instance's lock, loops, and finds the tombstone this call
        appends — 410 `request_expired`, which is what the contract says about an
        instance that no longer exists. `released` stays set as well: it is
        what distinguishes a released instance from one that is merely still
        loading, and `_live` refuses on either.

        Un-publishing drops the instance's creation records in the same
        locked block, and that is the half ADR-0007 asks for: replay is
        byte-identical "while the owning instance is live", so the records
        keyed to this instance cannot outlive its publication. Left until
        after the teardown, a resend of the *original* creation `request_id`
        would find its record in phase A and return the old 200 immediately —
        before any instance state is consulted — for an instance the next
        request answers `unknown_instance`. With the records gone that resend
        has nothing to replay: it validates in phase B, finds the not-live
        entry in phase C, waits on this instance's lock, loops, and sees the
        tombstone (410 `request_expired`), which is the same answer the
        different-`request_id` case gets. The post-teardown pop below stays,
        deliberately idempotent: nothing can add to `creation_request_ids`
        after `live` is false (phase C only adds under the global lock to an
        instance it found live), so it is a no-op belt-and-braces rather than
        the pop that matters."""
        with self._lock:
            instance = self._instances.get(instance_id)
        if instance is None:
            return canonical_body({"instance_id": instance_id, "released": False})
        with instance.lock:
            if instance.released or not instance.live:
                # Not yet published (still loading, or lost the race to
                # `close()` and was already torn down at publication) or
                # already released: nothing more for this call to do.
                return canonical_body({"instance_id": instance_id, "released": False})
            instance.released = True
            with self._lock:
                instance.live = False
                # Un-publish and forget this instance's creation records
                # together, under one global-lock block: a record must not
                # survive the publication it replays (ADR-0007).
                for recorded in instance.creation_request_ids:
                    self._creation_records.pop(recorded, None)
            self._teardown(instance)
            instance.records.clear()
            with self._lock:
                # A no-op after the pop above — kept idempotent so the
                # instance and its records are still dropped together here,
                # whatever a future path adds between the two blocks.
                for recorded in instance.creation_request_ids:
                    self._creation_records.pop(recorded, None)
                self._instances.pop(instance_id, None)
                self._tombstones.append(instance_id)
        return canonical_body({"instance_id": instance_id, "released": True})

    # -- sessions and turns ----------------------------------------------------

    def _open_session(self, instance_id: str, raw_body: bytes) -> tuple[int, bytes]:
        """The contract's replay order again: the record lives on the instance, so the
        instance is resolved and the record consulted before the seed rule and
        the public input are judged."""
        obj = _object(raw_body)
        request_id, public, raw_seed = _fields(obj, "request_id", "public", "seed")
        _identifier(request_id, "request_id")
        canonical = canonical_body(obj)
        instance = self._acquire(instance_id)
        with instance.lock:
            self._live(instance)
            record = _replay(instance.records, request_id, canonical)
            if record is not None:
                return record.status, record.body
            if not isinstance(public, dict):
                raise _bad("public")
            requested = _seed(raw_seed, "seed")
            if self.seed_mode == "exact" and requested is None:
                raise _bad("seed")
            if self.seed_mode == "none" and requested is not None:
                raise _bad("seed")
            # `_live` above has already refused an instance that is not
            # `live`; `live` is only ever set after `backend` is assigned at
            # publication (`_create_instance`), and clearing it at release
            # only ever refuses more, so `backend` is never `None` past this
            # point — but this is a typed
            # wire boundary, not an internal invariant to crash on: an
            # `assert` here would also vanish under `-O`, turning an
            # unreachable case into an unhandled `AttributeError` instead of
            # the refusal below.
            backend = instance.backend
            if backend is None:
                raise _Refused("unknown_instance", 404, "instance_id")
            # The mirror of the load path's guarantee (ADR-0009): a session
            # object `open` handed back is never in `instance.sessions` until
            # the registration below, so `_teardown` would never reach it —
            # the `finally` closes it on every path that did not register,
            # whatever `open`, the shape check, the `effective_seed` read or
            # the quiet close itself does.
            opened: BackendSession | None = None
            registered = False
            try:
                try:
                    # A read-only view: the stored `_Session.public` stays the
                    # dict the wire parsed, but a backend cannot mutate the
                    # server's copy of it through the mapping it is handed.
                    opened = backend.open(MappingProxyType(public), requested)
                    _require_session_shape(opened)
                    effective = opened.effective_seed
                except Exception:
                    # Whatever raised — `open` itself, the shape check over
                    # what it returned, or reading `effective_seed` off that
                    # object — is one `internal`, and the `finally` closes
                    # what was handed back.
                    raise _Refused("internal", 500, "internal") from None
                valid = (
                    (self.seed_mode == "exact" and effective == requested)
                    or (self.seed_mode == "unpaired" and type(effective) is int)
                    or (self.seed_mode == "none" and effective is None)
                )
                if not valid:
                    raise _Refused("internal", 500, "internal")
                reported = (
                    None
                    if (self.seed_mode == "unpaired" and self.misbehave.drop_effective_seed)
                    else effective
                )
                session_id = uuid4().hex
                instance.sessions[session_id] = _Session(
                    session_id, public, requested, reported, opened
                )
                registered = True
                body = canonical_body(
                    {
                        "session_id": session_id,
                        "requested_seed": requested,
                        "effective_seed": reported,
                    }
                )
                instance.records[request_id] = _Record(canonical, 200, body)
                return 200, body
            finally:
                if not registered:
                    _close_quietly(opened)

    def _close_session(self, instance_id: str, session_id: str) -> bytes:
        instance = self._acquire(instance_id)
        with instance.lock:
            self._live(instance)
            session = instance.sessions.pop(session_id, None)
            if session is not None:
                with contextlib.suppress(Exception):
                    session.backend.close()
            return canonical_body({"session_id": session_id, "closed": session is not None})

    def _turn(
        self, instance_id: str, session_id: str, raw_body: bytes, started: float
    ) -> tuple[int, bytes]:
        """The contract's replay order, with the session check where it already was: the
        record is scoped to the instance and outlives the session, so a replayed
        turn answers after the session closed while a fresh `request_id` on that
        session is 404 `unknown_session` (check 9W depends on both)."""
        obj = _object(raw_body)
        request_id, message = _fields(obj, "request_id", "message")
        _identifier(request_id, "request_id")
        canonical = canonical_body(obj)
        instance = self._acquire(instance_id)
        with instance.lock:
            self._live(instance)
            record = _replay(instance.records, request_id, canonical)
            if record is not None:
                return record.status, record.body
            if not isinstance(message, dict):
                raise _bad("message")
            content, message_fields = _fields(message, "content", "fields")
            if not isinstance(content, str):
                raise _bad("message.content")
            if not isinstance(message_fields, dict):
                raise _bad("message.fields")
            if session_id not in instance.sessions:
                raise _Refused("unknown_session", 404, "session_id")
            session = instance.sessions[session_id]
            try:
                reply = session.backend.turn(Message(content, message_fields))
            except Exception:
                raise _Refused("internal", 500, "internal") from None
            if not isinstance(reply, BackendReply):
                raise _Refused("internal", 500, "internal")
            result = reply.result
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            turn_body: dict[str, Any] = {
                "events": [_json_safe(event) for event in result.events],
                "usage": result.usage.to_payload() if result.usage is not None else None,
                "latency_ms": latency_ms,
                "outcome": result.outcome,
                "truncated": reply.truncated,
            }
            # Omit-not-null, exactly like `description` on the instance body
            # (PDR-0014, W19): the key appears only when the backend set one,
            # so an old client's closed key set still parses this body. The
            # server never interprets it; `parse_turn` below validates its
            # shape, and a bad one is `500 internal` like any other reply the
            # wire rejects.
            if result.reason is not None:
                turn_body["reason"] = dict(result.reason)
            if result.provenance is not None:
                turn_body["provenance"] = {"used": list(result.provenance["used"])}
            body = canonical_body(turn_body)
            try:
                parse_turn(json.loads(body))
            except (WireError, ValueError):
                # The wire's own parser is the judge of the body the server is
                # about to record: `TurnResult` admits an empty event tuple and
                # `TurnResponse` does not, so without this a backend could make
                # this surface serve — and then replay from its record — a 200
                # every conforming client rejects (ADR-0009). Like any other
                # turn failure: `500 internal`, nothing recorded, session open.
                raise _Refused("internal", 500, "internal") from None
            if len(body) > self.max_output_bytes:
                raise _Refused("internal", 500, "internal")
            instance.records[request_id] = _Record(canonical, 200, body)
            return 200, body

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """The one lifecycle boundary (ADR-0009): set `_closed` first, so
        every dispatch from this point on — including one already admitted
        but still loading, whose publication will find `_closed` and tear
        itself down — answers `not_ready`; then release every instance still
        in the table, each wait bounded by that instance's own load or turn
        in flight rather than by this call blocking the whole server; then
        remove the one snapshot directory this server created — its own
        `snapshot-<boot_id>` subdirectory when the caller named a container,
        the whole temporary directory when it did not. A sibling of that
        subdirectory belongs to someone else (a second server, or the caller)
        and is never touched. Idempotent — a second call finds nothing left to
        release and nothing left to remove; the removal is best-effort for the
        same reason `_teardown`'s is: a root already gone, or something the
        caller left unremovable beside it, is not a reason for the one
        lifecycle boundary to raise."""
        with self._lock:
            self._closed = True
            live = list(self._instances)
        for instance_id in live:
            self._release(instance_id)
        with contextlib.suppress(OSError, ArtifactError):
            remove_tree(self.snapshot_root)
        # The finalizer is the backstop for a server dropped without
        # `close()`; after an explicit close it has nothing left to do.
        self._finalizer.detach()


def serve_target(
    server: TargetServer, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """Bind a threading HTTP server for `server`; the caller runs `serve_forever`.

    `reference_target_http` owns the transport and consumes a dispatch protocol.
    It does not import this state machine.

    `port=0` takes an ephemeral port, readable afterwards as
    `server.server_address[1]`. Run it as
    `Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})`: the
    stdlib default polls every 0.5 s, which is 0.5 s of dead time in every
    `shutdown()` and the dominant cost of a suite that starts a target per case.

    `host` may be `127.0.0.1`, `localhost` or `::1`; the address family follows
    the host. Any other host is refused with `ValueError` before a socket is
    constructed.
    """
    from evolution.tree.target.reference_target_http import serve as _serve

    return _serve(server, host, port)
