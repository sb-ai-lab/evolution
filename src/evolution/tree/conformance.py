"""The two v2 conformance suites and the `evo tree conformance` runner (spec §6).

Conformance is the admission test for a promotion-capable target: it proves that
what a target *declares* it can do it actually does, and that what it *attests*
matches the bytes S2 published. Two suites, one shared scenario:

*The generic suite* (`adapter_conformance`, checks tagged `G`) speaks only the R1
Python protocols, so an in-process adapter and an HTTP one pass exactly the same
suite (ADR-0005). Its first check is `preflight`: an adapter without one is not
promotion-capable, because the complete declared `SemanticIdentity` must exist
before S1's first journal write, and the suite stops there rather than pretending
the rest means anything.

*The wire suite* (`wire_conformance`, checks tagged `W`) speaks raw
`/evolution/v2` over its own transport and checks what the adapter deliberately
hides: status codes, the typed error object, byte-identical replay, the tombstone
after release, and the declared input limit.

Two consequences of §3 shape the wire suite, and they are the subtle part:

*A released instance is gone, tombstoned and unreplayable.* §3 keeps a request
record exactly as long as the instance it addressed and refuses a creation naming
a tombstoned id with 410 `request_expired`. So the wire checks that need a live
resource — `instance_conflict`, the byte-identical turn replay, `unknown_session`
— cannot address instance `A` after the generic suite's release step (check 11G).
They create their own instance from the same published `candidate_root` and the
same policy, prove the property there, and release it. `A` is used for exactly
the two checks that *require* it to be gone: the repeated `DELETE` reporting
`released: false` and the 410 replay of its recorded creation request.

*Nothing may be left behind.* Every instance this module creates carries the
run's `run_id` prefix, so the runner's final sweep can release them by reading
`health` — a probe that failed halfway leaves nothing on the target, and the
temporary run root is removed on every exit path. Ids are fresh uuids, never
fixed names, so a sweep can never touch another run's instance. The sweep then
re-reads `health` and reports what survived as `leaked`, which fails the report:
cleanup nobody verifies is how a target passes while holding the probe's
instances.

The report carries the health body and its capabilities, the ordered checks, the
instances the sweep could not reclaim and a duration. Its `checks` always name
every check in `CHECK_NAMES`, in order: a suite that stopped early leaves the
rest recorded as `not run` rather than absent, so a report is never shorter for
having proved less. It carries no message, no public input and no candidate
path: §9 forbids that, and the CLI prints this document to stdout. The one
exception is `leaked`, whose ids this run itself minted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import uuid4

from evolution.tree.artifacts.artifact_copy import remove_tree
from evolution.tree.artifacts.artifacts import ArtifactRef, ArtifactStore, ExpectedAttestation
from evolution.tree.artifacts.attestation import (
    PromotionCapableAdapter,
    TargetPreflight,
    verify_attestation,
)
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.interaction import (
    MaterializationStrategy,
    Message,
    RolloutInfrastructureError,
    SeedMode,
    SemanticIdentity,
    TargetIdentity,
    TargetInstance,
    TargetSession,
    TurnResult,
)
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import (
    PREFIX,
    REQUIRED_ATTESTS,
    Health,
    canonical_body,
    is_instance_id,
    new_instance_id,
    parse_error,
    parse_health,
    parse_instance,
    parse_release,
    parse_session,
    parse_turn,
)
from evolution.tree.manifest.manifest_v2 import ManifestV2
from evolution.tree.target.config import TargetBinding
from evolution.tree.target.http_adapter import (
    Exchange,
    HttpSessionAdapter,
    HttpTransport,
    is_json_media,
)
from evolution.tree.target.target_calls import TargetCallLedger

VERSION: Literal["conformance/v2"] = "conformance/v2"
STRATEGY: MaterializationStrategy = "artifact_root_injection"

_REQUIRED_ATTESTS = frozenset(REQUIRED_ATTESTS)
_OUTCOMES = frozenset({"ok", "target_failed", "candidate_failed"})
_PUBLIC: dict[str, Any] = {"conformance": True}
_CONTENT = "conformance"
_NOT_A_ROOT = "not-a-root"

_E_HEALTH = "target health matches the binding and declares the required attestation"
_E_PREFLIGHT = "promotion-capable adapter exposes preflight"
_E_STORE = "the store publishes a champion carrying an expected attestation"
_E_ATTESTED = "materialize attests the published champion digests"
_E_SECOND = "a second instance materializes and attests independently"
_E_SESSION = "open returns a session whose seeds obey the declared seed mode"
_E_TURN = "one turn returns typed events, a usage shape and a closed outcome"
_E_TURN_IDEMPOTENT = "a repeated request_id returns an equal TurnResult"
_E_CLOSED = "close succeeds and is idempotent"
_E_STABLE = "identity after the session equals the identity at materialize"
_E_RELEASED = "release succeeds for both instances and is idempotent"

_E_HEALTH_WIRE = "GET /health answers 200 with the required attests and compatible limits"
_E_CONFLICT = "a different candidate_root is 409 instance_conflict; the same root replays"
_E_TRUNCATED = "the turn response carries a boolean truncated"
_E_REPLAY = "identical request bytes replay byte-identically; a different body is 409"
_E_UNKNOWN = "a turn on a closed session is 404 unknown_session"
_E_RELEASED_WIRE = "a repeated release reports released false and the creation replay is 410"
_E_LIMITS = "a turn body of exactly the declared input limit is accepted"

_E_NOT_ABSOLUTE = "allowed_artifact_root not absolute"
_E_WORKDIR_EXISTS = "workdir already exists"
_E_WORKDIR_OUTSIDE = "workdir outside allowed_artifact_root"

_NOT_RUN = "not run"
_DRIFTED = "identity.semantic differs from preflight.semantic"
# What the sweep reports when it cannot re-read `health` and so cannot say
# whether anything survived. Outside the identifier grammar on purpose: it is a
# verdict about the sweep, never a value the target supplied.
_UNVERIFIED = "<unverified>"

# Every check this module can record, in the order a report prints them. It is
# the report's shape, not a schedule: a suite that stops early leaves its
# remaining names recorded as `not run` rather than absent, so two reports are
# comparable position by position and a shorter one never reads as a cleaner one.
CHECK_NAMES: tuple[tuple[str, str, Literal["G", "W"]], ...] = (
    ("health", _E_HEALTH, "G"),
    ("store", _E_STORE, "G"),
    ("instance_attested", _E_ATTESTED, "G"),
    ("instance_second", _E_SECOND, "G"),
    ("session_opened", _E_SESSION, "G"),
    ("turn_shape", _E_TURN, "G"),
    ("turn_idempotent", _E_TURN_IDEMPOTENT, "G"),
    ("session_closed", _E_CLOSED, "G"),
    ("identity_stable", _E_STABLE, "G"),
    ("released", _E_RELEASED, "G"),
    ("health_wire", _E_HEALTH_WIRE, "W"),
    ("instance_conflict", _E_CONFLICT, "W"),
    ("turn_truncated_flag", _E_TRUNCATED, "W"),
    ("turn_idempotent_wire", _E_REPLAY, "W"),
    ("session_unknown", _E_UNKNOWN, "W"),
    ("released_wire", _E_RELEASED_WIRE, "W"),
    ("limits", _E_LIMITS, "W"),
)


class _NotConforming(Exception):
    """A shape rule a check proves itself; `str()` is the check's `got` string.

    Kept separate from `RolloutInfrastructureError` because it carries no wire
    code: it is this module's own verdict on a body that parsed but is wrong.
    """


@dataclass(frozen=True)
class Check:
    """One recorded verdict. `suite` is `G` for generic and `W` for wire (§6)."""

    name: str
    ok: bool
    expected: str
    got: str
    suite: Literal["G", "W"]

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "expected": self.expected,
            "got": self.got,
            "suite": self.suite,
        }


@dataclass
class Scenario:
    """The resources the generic suite created, handed to the wire suite.

    Mutable, and deliberately optional throughout: the generic suite fills it as
    far as it got, so a probe that failed at `materialize` still names the
    instance the target may be holding. `policy` and `turn_exchange` are the two
    fields the transport-neutral suite cannot know; the runner fills them.
    """

    run_id: str
    instance_a: str = ""
    instance_b: str | None = None
    root_a: Path | None = None
    root_b: Path | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    turn_exchange: Exchange | None = None
    expected: ExpectedAttestation | None = None
    turn_result: TurnResult | None = None


@dataclass(frozen=True)
class ConformanceReport:
    """What `evo tree conformance` prints: the ordered checks plus the epoch.

    `leaked` names the run-scoped instances the final sweep could not get the
    target to give up (or the `<unverified>` sentinel when it could not read
    `health` afterwards). It is not a check — no single check owns cleanup — but
    it does decide `ok`: §6 requires that the target keep no instance from the
    probe, so a run that left one behind has not conformed however well every
    check went. The ids are this run's own `<run_id>-<uuid>` names, so unlike
    every other value in the report they may be printed (§9).
    """

    version: Literal["conformance/v2"]
    ok: bool
    checks: tuple[Check, ...]
    leaked: tuple[str, ...]
    health: dict[str, Any] | None
    capabilities: dict[str, Any] | None
    duration_ms: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ok": self.ok,
            "checks": [check.to_payload() for check in self.checks],
            "leaked": list(self.leaked),
            "health": self.health,
            "capabilities": self.capabilities,
            "duration_ms": self.duration_ms,
        }


def _complete(checks: Sequence[Check]) -> tuple[Check, ...]:
    """The recorded verdicts, with every name a suite never reached filled in.

    `ok` is unchanged by this: a `not run` filler is `ok=False`, exactly as an
    absent check already made the report fail by leaving a property unproven.
    """
    recorded = {check.name: check for check in checks}
    return tuple(
        recorded.get(name) or Check(name, False, expected, _NOT_RUN, suite)
        for name, expected, suite in CHECK_NAMES
    )


def _got(exc: BaseException) -> str:
    """A check's `got` never carries caller data: a wire code, this module's own
    verdict, or the exception's class name."""
    if isinstance(exc, _NotConforming):
        return str(exc)
    if isinstance(exc, RolloutInfrastructureError):
        return exc.code
    return type(exc).__name__


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise _NotConforming(detail)


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


async def _wire_call(
    transport: HttpTransport, method: str, path: str, body: Mapping[str, Any] | None = None
) -> tuple[int, bytes]:
    """Status and bytes, under §3's media type.

    Required of *every* response, an expected refusal included: a target that
    answers its 4xx under another media type would otherwise pass the whole
    report, since the adapter never puts an error body in front of the generic
    suite. The violation is raised, so the check that made the call records it
    (check 1W reads the header itself, because it must also report a `/health`
    that never reached this helper).
    """
    status, raw, media = await transport.request(method, path, body)
    if not is_json_media(media):
        raise RolloutInfrastructureError(
            "target answered under another media type", "invalid_response"
        )
    return status, raw


def _expect_ok(status: int, raw: bytes) -> Any:
    """A call whose refusal is not the point of the check: the typed wire code is
    the verdict, exactly as the adapter would have reported it."""
    if status != 200:
        error = parse_error(status, _decode(raw))
        raise RolloutInfrastructureError("target refused a conformance call", error.code)
    body = _decode(raw)
    _require(body is not None, "200 body is not JSON")
    return body


def _expect_error(status: int, raw: bytes, want_status: int, want_code: str) -> None:
    _require(status == want_status, f"status {status}, expected {want_status}")
    error = parse_error(status, _decode(raw))
    _require(error.code == want_code, f"code {error.code}, expected {want_code}")


def _seed_for(seed_mode: SeedMode) -> int | None:
    """`exact` must send a seed; the other two modes must not (spec §3)."""
    return 0 if seed_mode == "exact" else None


def _check_seeds(seed_mode: SeedMode, requested: int | None, session: TargetSession) -> None:
    if seed_mode == "exact":
        _require(
            session.requested_seed == requested and session.effective_seed == requested,
            "exact mode did not echo the requested seed",
        )
    elif seed_mode == "unpaired":
        _require(session.requested_seed is None, "unpaired mode echoed a requested seed")
        _require(
            isinstance(session.effective_seed, int),
            "unpaired mode returned no effective seed",
        )
    else:
        _require(
            session.requested_seed is None and session.effective_seed is None,
            "seedless mode returned a seed",
        )


_SEMANTIC_FIELDS: tuple[str, ...] = (
    "contract_digest",
    "dependency_digest",
    "settings_hash",
    "seed_mode",
    "protocol_version",
    "runtime_ref_digest",
)


def _check_preflight(
    adapter: PromotionCapableAdapter, preflight: object, expected: ExpectedAttestation
) -> TargetPreflight:
    """Check 1's real subject: the complete declared identity, not a return.

    ADR-0007 requires preflight to yield the whole declared `SemanticIdentity`,
    the epoch and the capabilities *before* S1's first journal write, so an
    object that merely carries `capabilities` — or a `SemanticIdentity` written
    past its own validation — must fail here rather than pass the whole suite.
    `dependency_digest` is the one semantic field health cannot declare: S2
    seals it, so the preflight must echo what the store published. Every `got`
    names a field and never a value, because §9 forbids printing what the target
    said.
    """
    _require(isinstance(preflight, TargetPreflight), "preflight is not a TargetPreflight")
    assert isinstance(preflight, TargetPreflight)
    semantic = preflight.semantic
    _require(isinstance(semantic, SemanticIdentity), "preflight.semantic")
    for name in _SEMANTIC_FIELDS:
        value = getattr(semantic, name, None)
        _require(isinstance(value, str) and bool(value.strip()), f"preflight.semantic.{name}")
    _require(
        semantic.dependency_digest == expected.dependency_digest,
        "preflight.semantic.dependency_digest",
    )
    _require(isinstance(preflight.epoch, str) and bool(preflight.epoch.strip()), "preflight.epoch")
    try:
        frozen: object = adapter.capabilities
    except Exception:  # noqa: BLE001 - an adapter that hides them froze nothing
        frozen = None
    _require(frozen == preflight.capabilities, "preflight.capabilities")
    return preflight


def _bind(
    identity: TargetIdentity, expected: ExpectedAttestation, preflight: TargetPreflight
) -> None:
    """Attest and bind one materialized instance (checks 3G and 4G).

    Two rules, and both hold for *every* instance the suite materializes:
    `verify_attestation` proves the bytes the target loaded are the ones S2
    published, and the comparison with `preflight.semantic` proves the identity
    is the one S1 already journalled (ADR-0007). An adapter that attests
    correctly and reports another `settings_hash` on its second instance
    contradicts the frozen identity just as loudly as one that drifts on the
    first, so the two checks share this step rather than each keeping half of it.
    """
    verify_attestation(identity, expected)
    _require(identity.semantic == preflight.semantic, _DRIFTED)


def _check_turn(result: TurnResult) -> None:
    _require(bool(result.events), "the turn returned no events")
    for event in result.events:
        kind = event.get("type")
        _require(isinstance(kind, str) and bool(kind.strip()), "an event has no typed kind")
        if kind == "output_truncated":
            _require(
                dict(event) == {"type": "output_truncated"},
                "the output_truncated event carries extra fields",
            )
    _require(result.usage is None or isinstance(result.usage, Usage), "usage has the wrong shape")
    # The outcome is never interpolated: a non-conforming target could put
    # anything there, and a `got` string is printed (spec §9).
    _require(result.outcome in _OUTCOMES, "the outcome is outside the closed set")


# -- the generic suite --------------------------------------------------------


async def adapter_conformance(
    adapter: PromotionCapableAdapter, store: ArtifactStore, *, run_id: str
) -> tuple[tuple[Check, ...], Scenario]:
    """Run checks 1G–11G against any promotion-capable adapter (spec §6).

    Every failure is caught and recorded; the suite continues wherever the spec
    allows and short-circuits only when a resource a later check needs is
    missing. It always ends by releasing what it materialized, so a failed probe
    leaves the target as it found it.
    """
    checks: list[Check] = []
    scenario = Scenario(run_id=run_id)

    # 1G — a `getattr` probe, never `hasattr`: `HttpSessionAdapter` guards its
    # frozen members with `__getattr__`, so `hasattr` before preflight raises.
    preflight_call: Any = getattr(adapter, "preflight", None)
    if not callable(preflight_call):
        checks.append(Check("health", False, _E_PREFLIGHT, "adapter exposes no preflight", "G"))
        return tuple(checks), scenario

    # Check 2's work runs first because preflight needs the champion's expected
    # attestation, but the two verdicts are recorded in the spec's order.
    champion: ArtifactRef | None = None
    store_ok, store_got = True, "ok"
    try:
        champion = store.champion()
        scenario.expected = champion.expected_attestation()
        scenario.root_a = champion.root
    except Exception as exc:  # noqa: BLE001 - every failure is a recorded verdict
        store_ok, store_got = False, _got(exc)

    preflight: TargetPreflight | None = None
    if champion is None or scenario.expected is None:
        checks.append(Check("health", False, _E_HEALTH, "no champion to preflight", "G"))
    else:
        try:
            preflight = _check_preflight(
                adapter, await preflight_call(scenario.expected), scenario.expected
            )
            checks.append(Check("health", True, _E_HEALTH, "ok", "G"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("health", False, _E_HEALTH, _got(exc), "G"))
    checks.append(Check("store", store_ok, _E_STORE, store_got, "G"))
    if champion is None or preflight is None or scenario.expected is None:
        return tuple(checks), scenario

    expected = scenario.expected
    capabilities = preflight.capabilities

    # 3G — the id exists before the call, so a target that created the instance
    # and then failed attestation is still released by the caller's sweep.
    scenario.instance_a = new_instance_id(run_id)
    instance: TargetInstance | None = None
    identity: TargetIdentity | None = None
    try:
        instance = await adapter.materialize(champion, STRATEGY, scenario.instance_a)
        identity = await instance.identity()
        _bind(identity, expected, preflight)
        checks.append(Check("instance_attested", True, _E_ATTESTED, "ok", "G"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("instance_attested", False, _E_ATTESTED, _got(exc), "G"))

    # 4G — a Candidate sealed with no edits carries the champion's digests, so a
    # second instance proves coexistence without changing what is attested.
    if capabilities.max_instances >= 2:
        try:
            attempt = f"conf{uuid4().hex[:8]}"
            store.materialize_candidate(attempt)
            sealed = store.seal(attempt)
            scenario.instance_b = new_instance_id(run_id)
            scenario.root_b = sealed.root
            second = await adapter.materialize(sealed, STRATEGY, scenario.instance_b)
            _bind(await second.identity(), sealed.expected_attestation(), preflight)
            checks.append(Check("instance_second", True, _E_SECOND, "ok", "G"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("instance_second", False, _E_SECOND, _got(exc), "G"))
    else:
        skipped = f"skipped: max_instances={capabilities.max_instances}"
        checks.append(Check("instance_second", True, _E_SECOND, skipped, "G"))

    if instance is not None:
        checks.extend(await _session_checks(instance, capabilities.seed_mode, identity, scenario))

    # 11G — release is the last thing the suite does, and it releases only the
    # ids it built from this run's `run_id`. Each attempt stands on its own: the
    # suite promises to release everything it materialized on every exit path, so
    # a target refusing one release must not cost the caller the instance it
    # would still have released. Every failure is reported, not just the first.
    refused: list[str] = []
    for owned in (scenario.instance_a, scenario.instance_b, scenario.instance_a):
        if owned is None:
            continue
        try:
            await adapter.release(owned)
        except Exception as exc:  # noqa: BLE001
            refused.append(_got(exc))
    checks.append(Check("released", not refused, _E_RELEASED, "; ".join(refused) or "ok", "G"))
    return tuple(checks), scenario


async def _session_checks(
    instance: TargetInstance,
    seed_mode: SeedMode,
    identity: TargetIdentity | None,
    scenario: Scenario,
) -> list[Check]:
    """Checks 6G–10G: one session, one turn, its replay, close and identity."""
    checks: list[Check] = []
    session: TargetSession | None = None
    requested = _seed_for(seed_mode)
    try:
        session = await instance.open(_PUBLIC, requested, uuid4().hex)
        _check_seeds(seed_mode, requested, session)
        scenario.session_id = session.session_id
        checks.append(Check("session_opened", True, _E_SESSION, "ok", "G"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("session_opened", False, _E_SESSION, _got(exc), "G"))
    if session is None:
        return checks

    turn_request_id = uuid4().hex
    result: TurnResult | None = None
    try:
        result = await instance.send(session, Message(_CONTENT), turn_request_id)
        _check_turn(result)
        # Kept for 7W, which is the only check that can compare what the wire
        # said with what the adapter made of it.
        scenario.turn_result = result
        checks.append(Check("turn_shape", True, _E_TURN, "ok", "G"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("turn_shape", False, _E_TURN, _got(exc), "G"))

    if result is not None:
        try:
            repeat = await instance.send(session, Message(_CONTENT), turn_request_id)
            _require(repeat == result, "the repeated request_id returned a different result")
            checks.append(Check("turn_idempotent", True, _E_TURN_IDEMPOTENT, "ok", "G"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("turn_idempotent", False, _E_TURN_IDEMPOTENT, _got(exc), "G"))

    try:
        await instance.close(session)
        await instance.close(session)
        checks.append(Check("session_closed", True, _E_CLOSED, "ok", "G"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("session_closed", False, _E_CLOSED, _got(exc), "G"))

    if identity is not None:
        try:
            _require(await instance.identity() == identity, "identity changed across the session")
            checks.append(Check("identity_stable", True, _E_STABLE, "ok", "G"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("identity_stable", False, _E_STABLE, _got(exc), "G"))
    return checks


# -- the wire suite -----------------------------------------------------------


def _instances_path(instance_id: str) -> str:
    return f"{PREFIX}/instances/{instance_id}"


def _creation_body(scenario: Scenario, instance_id: str, root: Path) -> dict[str, Any]:
    return {
        "request_id": uuid4().hex,
        "instance_id": instance_id,
        "candidate_root": str(root),
        "policy": scenario.policy,
        "strategy": STRATEGY,
    }


def _turn_body(request_id: str, content: str) -> dict[str, Any]:
    return {"request_id": request_id, "message": {"content": content, "fields": {}}}


def _sized_turn_body(request_id: str, limit: int) -> dict[str, Any]:
    """A turn body whose canonical encoding is exactly `limit` bytes.

    The filler is ASCII, so one character is one encoded byte and the padding is
    computed after encoding the frame, `request_id` and escaping included (§6).
    """
    frame = len(canonical_body(_turn_body(request_id, "")))
    _require(limit >= frame, f"declared input limit {limit} is below the minimal turn body")
    return _turn_body(request_id, "c" * (limit - frame))


def _creation_exchange(exchanges: Sequence[Exchange], instance_id: str) -> Exchange | None:
    for exchange in exchanges:
        if exchange.method != "POST" or exchange.path != f"{PREFIX}/instances":
            continue
        body = _decode(exchange.request_bytes)
        if isinstance(body, dict) and body.get("instance_id") == instance_id:
            return exchange
    return None


async def wire_conformance(
    binding: TargetBinding,
    scenario: Scenario,
    *,
    recorder_exchanges: Sequence[Exchange],
    ledger: TargetCallLedger | None = None,
) -> tuple[Check, ...]:
    """Run checks 1W, 5, 7W, 8W, 9W, 11W and 12 over a raw transport (spec §6).

    Its own `HttpTransport` with no recorder: these bytes are the suite's, not
    the adapter's, and mixing them into the recorded exchanges would make the
    replay probes ambiguous.

    Check 1W is also the one check that reads a *header*: §3 requires
    `Content-Type: application/json` on every response, and the adapter — which
    refuses anything else — is exactly what hides that from the generic suite.

    `ledger`, when given, accounts this suite's own mutating calls (ADR-0014).
    `wire_conformance` does not bind it to a namespace itself — `run_conformance`
    does that on the shared ledger before this suite ever runs — so a caller
    that drives `wire_conformance` on its own, without going through
    `run_conformance`, gets a ledger whose records carry no instance ids until
    that caller binds one.
    """
    transport = HttpTransport(binding, ledger=ledger)
    checks: list[Check] = []

    health: Health | None = None
    try:
        status, raw, media = await transport.request("GET", f"{PREFIX}/health", None)
        _require(is_json_media(media), "the health response media type is not application/json")
        health = parse_health(_expect_ok(status, raw))
        _require(health.ready, "the target reports not ready")
        _require(
            frozenset(health.capabilities.attests) == _REQUIRED_ATTESTS,
            "capabilities.attests is not the required set",
        )
        _require(
            health.capabilities.max_output_bytes <= binding.max_response_bytes,
            "max_output_bytes exceeds the binding's response limit",
        )
        _require(
            health.capabilities.max_input_bytes >= binding.min_input_bytes,
            "max_input_bytes is below the binding's input minimum",
        )
        checks.append(Check("health_wire", True, _E_HEALTH_WIRE, "ok", "W"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("health_wire", False, _E_HEALTH_WIRE, _got(exc), "W"))

    if health is None or scenario.root_a is None or not scenario.policy:
        return tuple(checks)

    probe = new_instance_id(scenario.run_id)
    try:
        checks.extend(await _live_checks(transport, binding, scenario, probe))
    finally:
        # None of 5/7W/8W/9W is about cleanup, so a refused release here is not
        # their verdict to carry: the sweep re-reads `health` and reports the
        # instance as leaked, which fails the report without misnaming a check.
        await _release_quietly(transport, probe)
    checks.append(await _released_check(transport, scenario, recorder_exchanges))
    checks.append(await _limits_check(transport, binding, scenario, health))
    return tuple(checks)


async def _live_checks(
    transport: HttpTransport, binding: TargetBinding, scenario: Scenario, probe: str
) -> list[Check]:
    """Checks 5, 7W, 8W and 9W, all against one instance this suite owns.

    They cannot address the generic suite's instance `A`: check 11G released it,
    and §3 makes a released instance untouchable — its creation request is 410
    `request_expired` and its records are gone, so neither the conflict probe nor
    the byte-identical replay could run there.
    """
    checks: list[Check] = []
    assert scenario.root_a is not None
    live = False
    try:
        created = _creation_body(scenario, probe, scenario.root_a)
        status, raw = await _wire_call(transport, "POST", f"{PREFIX}/instances", created)
        _expect_ok(status, raw)
        live = True
        # A directory that exists but is not the published root: §3 decides the
        # conflict on the root, so a path the target cannot resolve inside its
        # allowed root would be refused earlier with `root_not_allowed` and prove
        # nothing about the conflict rule.
        other, made = _other_root(scenario.root_a)
        try:
            conflicting = dict(created, request_id=uuid4().hex, candidate_root=str(other))
            status, raw = await _wire_call(transport, "POST", f"{PREFIX}/instances", conflicting)
            _expect_error(status, raw, 409, "instance_conflict")
        finally:
            if made:
                _remove_directory(other)
        again = dict(created, request_id=uuid4().hex)
        status, raw = await _wire_call(transport, "POST", f"{PREFIX}/instances", again)
        replayed = parse_instance(_expect_ok(status, raw))
        status, raw = await _wire_call(transport, "GET", _instances_path(probe), None)
        _require(
            replayed == parse_instance(_expect_ok(status, raw)), "the replayed identity drifted"
        )
        checks.append(Check("instance_conflict", True, _E_CONFLICT, "ok", "W"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("instance_conflict", False, _E_CONFLICT, _got(exc), "W"))

    session_id: str | None = None
    turn_request_id = uuid4().hex
    body = _turn_body(turn_request_id, _CONTENT)
    response: bytes | None = None
    if live:
        # Preparation, not a verdict: a failure here is reported by the three
        # checks below, each of which says what it could not observe.
        try:
            opened = {
                "request_id": uuid4().hex,
                "public": _PUBLIC,
                "seed": _seed_for(binding.seed_mode),
            }
            status, raw = await _wire_call(
                transport, "POST", f"{_instances_path(probe)}/sessions", opened
            )
            session_id = parse_session(_expect_ok(status, raw)).session_id
            status, raw = await _wire_call(
                transport, "POST", f"{_instances_path(probe)}/sessions/{session_id}/turns", body
            )
            _expect_ok(status, raw)
            response = raw
        except Exception:  # noqa: BLE001
            response = None

    checks.append(_truncated_check(scenario, response))

    try:
        _require(response is not None and session_id is not None, "no turn to replay")
        assert response is not None and session_id is not None
        path = f"{_instances_path(probe)}/sessions/{session_id}/turns"
        status, raw = await _wire_call(transport, "POST", path, body)
        _expect_ok(status, raw)
        _require(raw == response, "the replayed turn response is not byte-identical")
        status, raw = await _wire_call(
            transport, "POST", path, _turn_body(turn_request_id, f"{_CONTENT}-other")
        )
        _expect_error(status, raw, 409, "request_conflict")
        checks.append(Check("turn_idempotent_wire", True, _E_REPLAY, "ok", "W"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("turn_idempotent_wire", False, _E_REPLAY, _got(exc), "W"))

    try:
        _require(session_id is not None, "no session to close")
        path = f"{_instances_path(probe)}/sessions/{session_id}"
        status, raw = await _wire_call(transport, "DELETE", path, None)
        _expect_ok(status, raw)
        # A fresh `request_id`: §3 answers a replayed one from the record store
        # before it ever looks for the session, which would hide the 404.
        status, raw = await _wire_call(
            transport, "POST", f"{path}/turns", _turn_body(uuid4().hex, _CONTENT)
        )
        _expect_error(status, raw, 404, "unknown_session")
        checks.append(Check("session_unknown", True, _E_UNKNOWN, "ok", "W"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("session_unknown", False, _E_UNKNOWN, _got(exc), "W"))
    return checks


def _truncated_check(scenario: Scenario, response: bytes | None) -> Check:
    """7W: the adapter's own recorded turn response, or this suite's when the
    generic suite never got that far.

    A boolean on the wire is only half of the rule: §4 says the adapter turns
    `truncated: true` into exactly one `output_truncated` event, because that is
    where the Scorer looks. The mapping can only be asserted against the response
    the adapter itself read, so it is checked when the scenario carries both that
    exchange and the `TurnResult` the adapter built from it.
    """
    paired = scenario.turn_exchange is not None and scenario.turn_result is not None
    raw = scenario.turn_exchange.response_bytes if scenario.turn_exchange else response
    try:
        _require(raw is not None, "no turn response to inspect")
        assert raw is not None
        turn = parse_turn(_decode(raw))
        _require(type(turn.truncated) is bool, "truncated is not a boolean")
        if paired:
            result = scenario.turn_result
            assert result is not None
            events = [event for event in result.events if event.get("type") == "output_truncated"]
            if turn.truncated:
                _require(
                    len(events) == 1,
                    "a truncated response did not become one output_truncated event",
                )
            else:
                _require(not events, "an untruncated response produced an output_truncated event")
        return Check("turn_truncated_flag", True, _E_TRUNCATED, "ok", "W")
    except Exception as exc:  # noqa: BLE001
        return Check("turn_truncated_flag", False, _E_TRUNCATED, _got(exc), "W")


async def _released_check(
    transport: HttpTransport, scenario: Scenario, exchanges: Sequence[Exchange]
) -> Check:
    """11W: the generic suite already released `A`, so the repeat reports
    `released: false` and its creation request is a tombstone."""
    try:
        _require(bool(scenario.instance_a), "the generic suite materialized no instance")
        status, raw = await _wire_call(
            transport, "DELETE", _instances_path(scenario.instance_a), None
        )
        body = _expect_ok(status, raw)
        _require(
            isinstance(body, dict) and body.get("released") is False,
            "a repeated release did not report released false",
        )
        creation = _creation_exchange(exchanges, scenario.instance_a)
        _require(creation is not None, "no recorded creation request to replay")
        assert creation is not None
        replay = _decode(creation.request_bytes)
        _require(isinstance(replay, dict), "the recorded creation request is not an object")
        status, raw = await _wire_call(transport, "POST", f"{PREFIX}/instances", replay)
        _expect_error(status, raw, 410, "request_expired")
        return Check("released_wire", True, _E_RELEASED_WIRE, "ok", "W")
    except Exception as exc:  # noqa: BLE001
        return Check("released_wire", False, _E_RELEASED_WIRE, _got(exc), "W")


async def _limits_check(
    transport: HttpTransport, binding: TargetBinding, scenario: Scenario, health: Health
) -> Check:
    """12: the probe owns its instance from creation to release, so the check
    does not depend on what the rest of the suite left behind.

    `min_input_bytes` is what the binding needs the target to accept and
    `max_input_bytes` is what it declares; preflight already proved the second is
    at least the first, so the smaller of the two never exceeds the declaration.
    A binding that needs less than the smallest turn body (#327, a valid manifest
    value) is checked at that body; a declaration below it still fails.
    """
    probe = new_instance_id(scenario.run_id)
    assert scenario.root_a is not None
    try:
        request_id = uuid4().hex
        declared = health.capabilities.max_input_bytes
        minimal = len(canonical_body(_turn_body(request_id, "")))
        limit = min(max(binding.min_input_bytes, minimal), declared)
        status, raw = await _wire_call(
            transport,
            "POST",
            f"{PREFIX}/instances",
            _creation_body(scenario, probe, scenario.root_a),
        )
        _expect_ok(status, raw)
        opened = {
            "request_id": uuid4().hex,
            "public": _PUBLIC,
            "seed": _seed_for(binding.seed_mode),
        }
        status, raw = await _wire_call(
            transport, "POST", f"{_instances_path(probe)}/sessions", opened
        )
        session_id = parse_session(_expect_ok(status, raw)).session_id
        body = _sized_turn_body(request_id, limit)
        _require(len(canonical_body(body)) == limit, "the probe body missed the declared limit")
        status, raw = await _wire_call(
            transport, "POST", f"{_instances_path(probe)}/sessions/{session_id}/turns", body
        )
        _expect_ok(status, raw)
        # Released inside the check, not only in the `finally`: this probe owns
        # its instance end to end, so a target that will not give it back is
        # this check's own failure rather than a silent leak.
        await _release(transport, probe)
        return Check("limits", True, _E_LIMITS, "ok", "W")
    except Exception as exc:  # noqa: BLE001
        return Check("limits", False, _E_LIMITS, _got(exc), "W")
    finally:
        # The failure paths still try: after a successful release above this is
        # a 200 `released: false`, which `_release` accepts as proof.
        await _release_quietly(transport, probe)


def _other_root(root_a: Path) -> tuple[Path, bool]:
    """A real directory inside the allowed root that is not the published copy.

    A sibling of the champion copy, created for the probe and removed with it;
    when the run root refuses the write, the champion's own parent directory
    serves — it exists, it is inside the allowed root, and it is not `root_a`.
    """
    sibling = root_a.parent / _NOT_A_ROOT
    try:
        sibling.mkdir()
    except OSError:
        return (sibling if sibling.is_dir() else root_a.parent), False
    return sibling, True


def _remove_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


async def _release(transport: HttpTransport, instance_id: str) -> None:
    """Prove one wire probe's instance is gone, or say why it is not.

    Nothing else reports a release: the adapter is not in this path and no check
    calls it as its subject, so a helper that ignored the answer let a target
    return 500 to every raw-wire `DELETE` and still pass every recorded check.
    Two answers are proof: 200 with the closed release body echoing the addressed
    id (`released` may be either — §3 says a repeat reports `false`), and 404
    `unknown_instance`, which says the same thing more bluntly. Anything else
    raises with the target's own wire code, or `invalid_response` when the body
    is not one this module can read.
    """
    status, raw = await _wire_call(transport, "DELETE", _instances_path(instance_id), None)
    body = _decode(raw)
    if status == 200:
        try:
            released = parse_release(body)
        except ValueError:
            raise RolloutInfrastructureError(
                "release answered outside the closed body", "invalid_response"
            ) from None
        if released.instance_id != instance_id:
            raise RolloutInfrastructureError("release echoed another instance", "invalid_response")
        return
    if body is None:
        raise RolloutInfrastructureError("release answered no JSON body", "invalid_response")
    error = parse_error(status, body)
    if status == 404 and error.code == "unknown_instance":
        return
    raise RolloutInfrastructureError("the target refused a release", error.code)


async def _release_quietly(transport: HttpTransport, instance_id: str) -> None:
    """A release on a path that has already recorded its verdict, or whose leak
    the final sweep is the right place to report. Never the only attempt."""
    try:
        await _release(transport, instance_id)
    except Exception:  # noqa: BLE001
        pass


# -- the runner ---------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _turn_exchange(exchanges: Sequence[Exchange], scenario: Scenario) -> Exchange | None:
    if not scenario.instance_a or scenario.session_id is None:
        return None
    path = f"{_instances_path(scenario.instance_a)}/sessions/{scenario.session_id}/turns"
    for exchange in exchanges:
        if exchange.method == "POST" and exchange.path == path:
            return exchange
    return None


async def _run_instances(transport: HttpTransport, run_id: str) -> tuple[str, ...]:
    """This run's instances as `health` lists them, sorted; raises if it cannot ask.

    Only names that satisfy the §3 grammar for this run's ids are ever returned:
    `leaked` reaches stdout and stderr verbatim, so a target that pads its list
    with `<run_id>-` followed by anything else earns the `<unverified>` sentinel
    in place of its bytes (§9).
    """
    status, raw = await _wire_call(transport, "GET", f"{PREFIX}/health", None)
    body = _expect_ok(status, raw)
    instances = body.get("instances") if isinstance(body, dict) else None
    listed = instances if isinstance(instances, list) else ()
    prefix = f"{run_id}-"
    ours: set[str] = set()
    for name in listed:
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        ours.add(name if is_instance_id(name, run_id) else _UNVERIFIED)
    return tuple(sorted(ours))


async def _sweep(transport: HttpTransport, run_id: str) -> tuple[str, ...]:
    """Release every instance this run created, then say what survived.

    Instance ids are `<run_id>-<uuid>` (§3), so reading `health` and deleting the
    matching ids can never touch another run's instance — and re-reading `health`
    afterwards turns the sweep from a gesture into an answer. Each release stands
    on its own, because one instance a target refuses to give up must not hide
    the next. A `health` this run cannot read after the sweep proves nothing
    either way, so it reports the `<unverified>` sentinel rather than silence.
    """
    try:
        listed = await _run_instances(transport, run_id)
    except Exception:  # noqa: BLE001
        listed = ()
    for instance_id in listed:
        if instance_id != _UNVERIFIED:
            await _release_quietly(transport, instance_id)
    try:
        return await _run_instances(transport, run_id)
    except Exception:  # noqa: BLE001
        return (_UNVERIFIED,)


def _make_run_root(root: Path, *, explicit: bool) -> Check | None:
    """Create the run root; `None` means it now exists and this run made it.

    `exist_ok` only for the generated root, which is a fresh uuid child nothing
    else can hold. The cleanup removes the run root *recursively*, so a caller's
    existing directory is neither this run's to take nor this run's to delete: it
    is a recorded `store` failure and nothing is written or removed.
    """
    try:
        root.mkdir(parents=True, exist_ok=not explicit)
    except FileExistsError as exc:
        got = _E_WORKDIR_EXISTS if explicit else _got(exc)
        return Check("store", False, _E_STORE, got, "G")
    except OSError as exc:
        return Check("store", False, _E_STORE, _got(exc), "G")
    return None


def _open_store(root: Path, manifest: ManifestV2) -> tuple[ArtifactStore | None, Check | None]:
    """Open the temporary S2 store inside a run root this run created."""
    try:
        store = ArtifactStore.create(
            root / "run", manifest.seed_root, manifest.policy, parallel_attempts=1
        )
    except (OSError, ArtifactError, ValueError) as exc:
        return None, Check("store", False, _E_STORE, _got(exc), "G")
    return store, None


async def run_conformance(
    manifest: ManifestV2,
    *,
    workdir: Path | None = None,
    ledger: TargetCallLedger | None = None,
) -> ConformanceReport:
    """Run both suites against the manifest's target (spec §6).

    The temporary run root must lie inside the target's `allowed_artifact_root`
    (§3 gives the target authority over `candidate_root`), which only `health`
    knows — hence the raw call before anything else exists.

    An explicit `workdir` must lie inside that root too — it is the store the
    Candidate roots are published from, so one outside could never conform —
    and must not exist yet: this run creates it, fills it and removes it, so
    pointing it at a directory that already holds something would delete a
    caller's data. Only a directory this run created is ever removed.

    `ledger`, when given, is bound to this run's `conf-` namespace before any
    socket — a ledger bound to another operation is refused with `ValueError`
    before the `health` GET and before any transport is built — and then handed
    to all three of this run's transports: the runner and sweep, the adapter,
    and the wire suite, so it accounts every mutating call the run makes
    (ADR-0014). The report does not change.
    """
    started = time.monotonic()
    binding = manifest.target
    run_id = f"conf-{uuid4().hex[:12]}"
    if ledger is not None:
        ledger.bind_namespace(run_id)
    transport = HttpTransport(binding, ledger=ledger)

    try:
        status, raw = await _wire_call(transport, "GET", f"{PREFIX}/health")
        decoded = _expect_ok(status, raw)
        parsed = parse_health(decoded)
    except Exception as exc:  # noqa: BLE001
        failed = _complete((Check("health", False, _E_HEALTH, _got(exc), "G"),))
        return ConformanceReport(VERSION, False, failed, (), None, None, _elapsed_ms(started))

    # Before anything exists: the run root is built inside the advertised root
    # and `Path.resolve()` would silently anchor a relative one in the CLI's own
    # working directory. `parse_health` already refuses it, so this is the
    # runner's own guard over the one value that decides where it writes.
    if not PurePosixPath(parsed.allowed_artifact_root).is_absolute():
        failed = _complete((Check("health", False, _E_HEALTH, _E_NOT_ABSOLUTE, "G"),))
        return ConformanceReport(VERSION, False, failed, (), None, None, _elapsed_ms(started))

    allowed_root = Path(parsed.allowed_artifact_root).resolve()
    explicit = workdir is not None
    root = workdir
    if root is None:
        root = allowed_root / f".conformance-{uuid4().hex}"
    elif not root.resolve().is_relative_to(allowed_root):
        # Refused before the directory exists and before the target is asked for
        # anything: the same early exit as a relative root, for the same reason.
        failed = _complete((Check("store", False, _E_STORE, _E_WORKDIR_OUTSIDE, "G"),))
        return ConformanceReport(VERSION, False, failed, (), None, None, _elapsed_ms(started))
    exchanges: list[Exchange] = []
    store: ArtifactStore | None = None
    created = False
    checks: tuple[Check, ...] = ()
    leaked: tuple[str, ...] = ()
    try:
        # `created` is bound the moment the directory exists, before the store is
        # opened: whatever the store raises, the cleanup still owns the root.
        failure = _make_run_root(root, explicit=explicit)
        if failure is None:
            created = True
            store, failure = _open_store(root, manifest)
        if failure is not None:
            checks = (failure,)
        else:
            assert store is not None
            adapter = HttpSessionAdapter(
                binding, manifest.policy, run_id=run_id, recorder=exchanges.append, ledger=ledger
            )
            # The cast is a typing artefact, not a doubt about the adapter: R1's
            # `TargetSession`/`TargetInstance` protocols declare settable members,
            # so a frozen `HttpSession` is not a structural subtype of
            # `TargetSession` and the return types do not line up. The suite
            # exercises exactly the members the protocols name.
            generic, scenario = await adapter_conformance(
                cast(PromotionCapableAdapter, adapter), store, run_id=run_id
            )
            # The two fields a transport-neutral suite cannot know: the canonical
            # policy the wire sends and the exchange the adapter's turn recorded.
            scenario.policy = manifest.policy.to_payload()
            scenario.turn_exchange = _turn_exchange(exchanges, scenario)
            wire = await wire_conformance(
                binding, scenario, recorder_exchanges=tuple(exchanges), ledger=ledger
            )
            checks = generic + wire
    finally:
        # Nested, not sequential: the sweep is the only `await` in the cleanup,
        # so a cancellation delivered at it would otherwise skip the store lease
        # and the workdir — the two things that must go whatever happened.
        try:
            leaked = await _sweep(transport, run_id)
        finally:
            if store is not None:
                store.close()
            if created:
                _remove_workdir(root)

    complete = _complete(checks)
    # §6: "the target keeps no instance from the probe". A leak is not a check —
    # no check owns cleanup — but a run that left one behind has not conformed.
    ok = all(check.ok for check in complete) and not leaked
    health = dict(decoded)
    return ConformanceReport(
        VERSION, ok, complete, leaked, health, dict(health["capabilities"]), _elapsed_ms(started)
    )


def _remove_workdir(root: Path) -> None:
    try:
        remove_tree(root)
    except (OSError, ArtifactError, ValueError):
        pass
