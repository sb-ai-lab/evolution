"""`TargetBackend` for core over a neutral asynchronous agent (spec W19 §3.2)."""
from __future__ import annotations

import asyncio
import json
import secrets as _secrets
import sys
import threading
import uuid
from collections.abc import Collection, Mapping
from typing import Any, Literal

from evolution.tree import BackendError, BackendReply, LoadedTree, Message, TurnResult
from evolution.tree.execution.wire_v2 import (
    PROTOCOL_VERSION, REQUIRED_ATTESTS, STRATEGY, canonical_body,
    validate_instance_components, validate_instance_description,
)

from evolution_lab.target._contract import KEEP, AgentFactory, AgentTurn, AgentView, JsonValue, TurnAgent, TurnReason
from evolution_lab.target._fit import OutputTooLarge, fit

_UNPAIRED_BOUND = 2**31


class _Loop:
    """One thread, one event loop: a template allows one loop per application."""

    def __init__(self, name: str) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self.thread.start()

    def run(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


async def _guarded(coroutine: Any) -> Any:
    """Run `coroutine` and turn even `SystemExit`/`KeyboardInterrupt` into an
    ordinary exception, so a rogue agent coroutine can never kill the loop
    thread out from under `_call`'s `.result()` (a dead thread never resolves
    that future, and core would hang holding the instance lock forever). A
    `CancelledError` delivered to this guard itself — as opposed to one the
    agent raises on purpose — is not caught: it means this call, not the
    agent, was cancelled, and must propagate as cancellation."""
    try:
        return await coroutine
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        raise RuntimeError(f"agent coroutine raised {type(exc).__name__}: {exc}") from exc


def _call(loop: _Loop, coroutine: Any) -> Any:
    try:
        return loop.run(_guarded(coroutine))
    except BaseException as exc:  # CancelledError included: a cancelled turn is not an answer
        raise BackendError("agent call failed") from exc


async def _describe(agent: TurnAgent) -> Mapping[str, Any]:
    # `describe()` is synchronous, but every touch of the agent happens on its
    # own instance loop thread (spec: "фабрика и describe() — в цикле при load").
    return agent.describe()


async def _components(agent: TurnAgent) -> tuple[bool, Any]:
    method = getattr(agent, "components", None)
    return (True, method()) if callable(method) else (False, None)


def _creation_bound(tree: LoadedTree, description: Mapping[str, Any], components: Mapping[str, Any]) -> int:
    """Core b17 identity body, with maximum lengths for server-only identifiers."""
    entries = tree.artifact + tree.dependency
    return len(canonical_body({
        "instance_id": tree.instance_id, "boot_id": "0" * 32,
        "contract_digest": "x" * 128, "settings_hash": "x" * 128,
        "runtime_ref_digest": "x" * 128, "seed_mode": tree.seed_mode,
        "protocol_version": PROTOCOL_VERSION, "strategy": STRATEGY,
        "loaded_candidate_digest": tree.candidate_digest, "dependency_digest": tree.dependency_digest,
        "attested": list(REQUIRED_ATTESTS),
        "file_count": sum(entry.kind == "file" for entry in entries),
        "byte_count": sum(entry.size for entry in entries),
        "description": dict(description), "components": components,
    }))


def _scalar_json_text(value: bool | int | float | None) -> str:
    """The canonical JSON text of one non-string scalar, encoded with core's
    own `canonical_body` options (a bare scalar has no keys to sort). A value
    JSON cannot encode (NaN, an infinity) never reaches core's description,
    which refuses it; it falls back to `str()` so the scan itself never raises."""
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        return str(value)


def _contains_literal(value: Any, needles: Collection[str], *, _seen: set[int] | None = None) -> bool:
    """Walk `value` (mappings, lists, tuples) and test the text of every key
    and leaf against each non-empty needle with plain substring containment
    (the needle occurs in the text). A string is its own text. A number, a
    boolean and `None` are tested by their canonical JSON text -- exactly what
    core writes for them into the instance description (`123456`, `0.25`,
    `1e+100`, `true`, `null`), so a numeric secret cannot reach durable
    evidence as a number (ADR-0013); `True`/`None` are not their JSON text,
    so a secret `"None"` no longer matches a null. Any other leaf is tested
    by `str()`. Unlike a `json.dumps` scan of the whole value, this sees a
    secret through JSON's own escaping (a quote, a backslash, a newline) and
    cannot be fooled by punctuation the encoder adds -- each scalar is encoded
    alone, so no brace, quote, comma or colon is ever part of a tested text --
    and it cannot raise on a key or value JSON has no way to encode (a
    non-finite float falls back to `str()`). `_seen` guards a reference cycle
    by object id."""
    live = tuple(needle for needle in needles if needle)
    if not live:
        return False
    seen = _seen if _seen is not None else set()
    if isinstance(value, (Mapping, list, tuple)):
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        pairs = value.items() if isinstance(value, Mapping) else enumerate(value)
        return any(_contains_literal(key, live, _seen=seen) or _contains_literal(item, live, _seen=seen)
                   for key, item in pairs)
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float)):
        text = _scalar_json_text(value)
    else:
        text = str(value)
    return any(needle in text for needle in live)


def _json_state(value: Any, origin: str) -> JsonValue:
    """Check a session state against the `JsonValue` contract (spec §3.1) and
    return the state to store.

    The check is core's own `canonical_body` (it takes a mapping, hence the
    wrapper), so "JSON" means exactly what it means on core's wire: NaN and
    the infinities are refused along with anything `json.dumps` cannot encode.

    What is stored is the decoded copy, not the object the agent returned.
    The layer never serialises state itself, so keeping the original would be
    possible, but it would alias the agent's live object: a later in-place
    change by the agent (or by a framework that owns the dict) would silently
    rewrite the state the next turn receives, and the agent could come to
    rely on non-JSON shapes the contract does not promise. The copy is the
    state's JSON reading -- for a value that already is `JsonValue` it is
    equal to the original; a tuple comes back as a list and a non-string key
    as its JSON string, as they would through any serialising layer."""
    try:
        encoded = canonical_body({"state": value})
    except ValueError as exc:
        raise BackendError(f"{origin} returned a session state that is not JSON (finite numbers only)") from exc
    return json.loads(encoded)["state"]


class _Session:
    def __init__(self, instance: _Instance, session_id: str, seed: int | None, state: JsonValue) -> None:
        self._instance, self._id, self._seed, self._state = instance, session_id, seed, state
        self._closed = False

    @property
    def effective_seed(self) -> int | None:
        return self._seed

    def turn(self, message: Message) -> BackendReply:
        instance = self._instance
        if self._closed or instance.closed:
            # Checked before any coroutine is built: an instance/session that
            # already tore down its loop can no longer run one, and building
            # the coroutine anyway would only leak it unawaited.
            raise BackendError("session is closed")
        result = _call(instance.loop, instance.agent.turn(message, self._state, session_id=self._id, seed=self._seed))
        if not isinstance(result, AgentTurn):
            raise BackendError("agent turn did not return an AgentTurn")
        provenance = result.provenance.to_payload() if result.provenance is not None else None
        if provenance is not None and any(_contains_literal(item, instance.secrets) for item in provenance["used"]):
            raise BackendError("turn provenance carries a launch secret")
        # The reason is a turn-body field (CORE-W19-02): measured by `fit`,
        # never cut, and handed to core as `TurnResult.reason`.
        outcome, turn_reason = result.outcome, result.reason
        if instance.empty_answer == "target_failed" and outcome == "ok" and not result.text.strip():
            outcome, turn_reason = "target_failed", TurnReason("lab", "empty_answer")
        reason = turn_reason.to_payload() if turn_reason is not None else None
        try:
            events, cut, kept = fit(result.events, result.text, result.usage, instance.max_output_bytes,
                                    reason=reason, provenance=provenance)
            if not kept:
                provenance = None
        except OutputTooLarge as exc:
            raise BackendError("reply minimum exceeds max_output_bytes") from exc
        except (TypeError, ValueError) as exc:
            # `fit` measures with core's `canonical_body`: an event value JSON
            # cannot carry (NaN, an arbitrary object) is its `ValueError`, a
            # lone surrogate its `UnicodeEncodeError` (also a `ValueError`).
            # Nothing is committed yet, so the session keeps its state.
            raise BackendError("agent turn is not JSON the wire can carry") from exc
        state = self._state if result.state is KEEP else _json_state(result.state, "turn")
        try:
            # Core validates the reply here, more strictly than `AgentTurn`
            # does (a blank event type, say): build it before committing state.
            reply = BackendReply(TurnResult(events=events, usage=result.usage, latency_ms=0,
                                            outcome=outcome, reason=reason, provenance=provenance),
                                 truncated=cut or result.truncated)
        except ValueError as exc:
            raise BackendError("agent turn does not conform to core's turn result") from exc
        # Committed only once the whole reply is known to conform: a turn core
        # rejects leaves the session exactly where it was.
        self._state = state
        return reply

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._instance.sessions.pop(self._id, None)
            _call(self._instance.loop, self._instance.agent.close_session(self._id))


class _Instance:
    def __init__(self, loop: _Loop, agent: TurnAgent, tree: LoadedTree, description: Mapping[str, Any],
                 empty_answer: Literal["target_failed", "ok"], secrets: Collection[str]) -> None:
        self.loop, self.agent, self.description = loop, agent, description
        self.max_output_bytes, self.seed_mode = tree.max_output_bytes, tree.seed_mode
        self.empty_answer = empty_answer
        self.secrets = secrets
        self.sessions: dict[str, _Session] = {}
        self.closed = False

    def describe(self) -> Mapping[str, Any]:
        """Core's `DescribingInstance.describe()` (CORE-W19-03): called once,
        right after `load`, under this instance's lock. Hands back the
        description `_Backend.load` already captured and validated on the
        instance loop -- the agent is never touched again for it."""
        return self.description

    def open(self, public: Mapping[str, Any], seed: int | None) -> _Session:
        if self.closed:
            # Checked before `self.agent.open(...)` is even evaluated: a
            # closed instance stopped its loop, and building the coroutine
            # anyway would only leak it unawaited (RuntimeWarning) instead of
            # answering cleanly.
            raise BackendError("instance is closed")
        if self.seed_mode == "none":
            effective = None
        elif self.seed_mode == "exact":
            effective = seed
        else:
            effective = seed if seed is not None else _secrets.randbelow(_UNPAIRED_BOUND)
        session_id = uuid.uuid4().hex
        raw = _call(self.loop, self.agent.open(dict(public), session_id=session_id, seed=effective))
        try:
            state = _json_state(raw, "open")
        except BackendError:
            # The agent already set up this session's bookkeeping; release it
            # (best effort -- the refused state stays the reported cause).
            try:
                _call(self.loop, self.agent.close_session(session_id))
            except BackendError:
                pass
            raise
        session = _Session(self, session_id, effective, state)
        self.sessions[session_id] = session
        return session

    def close(self) -> None:
        if self.closed:
            # Idempotent: a second close is a no-op, not a re-run of teardown
            # -- the loop already stopped, so retrying it would only raise a
            # fresh "event loop is closed" error that masks whatever the
            # first close reported (or invents a failure where the first
            # close actually succeeded).
            return
        self.closed = True
        first_error: BaseException | None = None
        try:
            for session in list(self.sessions.values()):
                try:
                    session.close()
                except BaseException as exc:  # keep closing the rest; report only the first failure
                    if first_error is None:
                        first_error = exc
            try:
                _call(self.loop, self.agent.aclose())
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        finally:
            self.loop.stop()
        if first_error is not None:
            raise first_error


class _ComponentsInstance(_Instance):
    def __init__(self, *args: Any, components: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._components = components

    def components(self) -> Mapping[str, Any]:
        return self._components


class _Backend:
    def __init__(self, factory: AgentFactory, secrets: Collection[str],
                 empty_answer: Literal["target_failed", "ok"]) -> None:
        self._factory, self._secrets = factory, tuple(secrets)
        self._empty_answer = empty_answer

    def load(self, tree: LoadedTree) -> _Instance:
        loop = _Loop(f"lab-target-instance-{tree.instance_id}")
        agent: TurnAgent | None = None
        try:
            view = AgentView(tree.root, tree.candidate_digest, tree.dependency_digest, tree.policy, tree.seed_mode)
            agent = _call(loop, self._factory(view))
            try:
                raw = _call(loop, _describe(agent))
                try:
                    # Narrowed to core's shape (CORE-W19-03): a flat object of
                    # scalars, identifier keys, <=32 keys, <=4096 bytes of
                    # canonical JSON -- the same check core itself would run
                    # in `_instance_description`, called here so a malformed
                    # description fails at `load` with a clear reason instead
                    # of a generic `500 internal` from core. `TurnAgent.describe`
                    # is required (not optional), so `None` is as non-conforming
                    # as any other non-mapping -- `validate_instance_description`
                    # alone would treat `None` as "no description", which is a
                    # meaning reserved for a backend with no `describe` at all.
                    if not isinstance(raw, Mapping):
                        raise ValueError("describe() must return a Mapping")
                    description = validate_instance_description(raw)
                    assert description is not None  # a non-`None` Mapping never validates to `None`
                except ValueError as exc:
                    raise BackendError("instance description does not match core's shape") from exc
                if _contains_literal(description, self._secrets):
                    raise BackendError("instance description carries a launch secret")
            except BackendError:
                raise
            except BaseException as exc:  # e.g. describe() itself raised
                raise BackendError("instance description could not be read") from exc
            try:
                present, raw_components = _call(loop, _components(agent))
            except BackendError as exc:
                raise BackendError("instance components could not be read") from exc
            if present:
                try:
                    if not isinstance(raw_components, Mapping):
                        raise ValueError("components() must return a Mapping")
                    components = validate_instance_components(raw_components)
                    assert components is not None
                except ValueError as exc:
                    raise BackendError("instance components do not match core's shape") from exc
                if _contains_literal(components, self._secrets):
                    raise BackendError("instance components carry a launch secret")
                bound = _creation_bound(tree, description, components)
                if bound > tree.max_output_bytes:
                    diagnostic = (f"instance components: components={len(canonical_body(components))} bytes, "
                                  f"bound={bound} bytes, limit={tree.max_output_bytes} bytes; "
                                  "raise max_output_bytes in the manifest or use fewer components\n")
                    try:
                        sys.stderr.write(diagnostic)
                    except (OSError, ValueError):
                        # A closed or broken diagnostic stream must not reject the map.
                        pass
                return _ComponentsInstance(loop, agent, tree, description, self._empty_answer,
                                           self._secrets, components=components)
            return _Instance(loop, agent, tree, description, self._empty_answer, self._secrets)
        except BaseException:
            # Once the agent exists, every failure path below releases it before the
            # loop stops; the failure above stays the reported cause, an aclose()
            # failure here is not allowed to hide it.
            if agent is not None:
                try:
                    _call(loop, agent.aclose())
                except BaseException:
                    pass
            loop.stop()
            raise


def backend(factory: AgentFactory, *, secrets: Collection[str] = (),
            empty_answer: Literal["target_failed", "ok"] = "target_failed") -> _Backend:
    """Core `TargetBackend` over `factory`.

    `secrets` are literal values `describe()` must not carry. By default an
    `ok` turn with blank text becomes `target_failed` with `lab/empty_answer`,
    replacing any agent reason before fitting. `empty_answer="ok"` preserves
    such turns. Events, usage and session state follow the usual rules.
    """
    if not isinstance(empty_answer, str) or empty_answer not in ("target_failed", "ok"):
        raise ValueError("empty_answer must be target_failed or ok")
    return _Backend(factory, secrets, empty_answer)
