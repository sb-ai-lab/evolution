"""Owner probe instances on a live target: admission, wire, settlement (spec 2026-10-02 §3.2–§3.5).

One probe session is one core instance with one core session on the Owner's ready target. The
Owner admits it in one store transaction before any target call (a ``probe_instances`` row that
blocks every ``target_use`` admission), owns a public ``TargetCallLedger`` per session
(namespace = session id = adapter ``run_id``) and releases the row only on a positive settlement
of the same criteria as a fixed evaluation (ADR-0008, ADR-0021). Turns of all sessions go one at a
time. Core is imported lazily, as in ``target.py``: the package initializer stays stdlib-only.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import OwnerError, TargetRef
from .settlement import ledger_payload, positive_settlement, settle_timeout

if TYPE_CHECKING:
    from evolution.tree import TurnResult

_SESSION_ID = re.compile(r"[0-9a-f]{32}")
#: Seeds are ``0 <= seed < SEED_BOUND``: a Lab-picked seed for an ``exact`` target when the caller names
#: none, and the range ``ProjectSession.probe_start`` accepts; well inside core's wire int.
SEED_BOUND = 2**31
#: Core transport codes for "the call did not reach an answering target".
_UNREACHABLE = frozenset({"transport_error", "callee_timeout"})
_SETTLED = frozenset({"released", "uncertain"})
#: The transport read timeout of a probe's turns exceeds the session's turn timeout by this much, so the turn
#: timeout, not the transport, ends a turn that runs too long (spec §3.3).
_TURN_READ_MARGIN = 5.0
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeOpened:
    session_id: str
    instance_id: str
    target_id: str
    owner_generation: str
    boot_id: str
    bundle_digest: str
    launch_digest: str
    source_kind: str
    seed_mode: str
    requested_seed: int | None
    effective_seed: int | None


@dataclass(frozen=True)
class ProbeTurnOutcome:
    result: TurnResult | None
    failure: str | None  # "turn_timeout" | "target_unreachable" | "protocol_error"
    closed: str | None  # "released" | "uncertain" when the failure closed the session


@dataclass
class _ProbeRuntime:
    """One probe row plus ownership retained until its surviving I/O and local store cleanup end."""

    session_id: str
    target_id: str
    binding: Any  # the manifest's core TargetBinding: bounds every call but a turn, and the settlement
    ledger: Any  # core TargetCallLedger, namespace = session_id
    facts: dict = field(default_factory=lambda: {"materialize_started": False, "release_started": False})
    opening: asyncio.Task | None = None  # the Owner-held open, until it returns
    turn: asyncio.Task | None = None  # the in-flight core send
    adapter: Any = None
    store: Any = None  # core ArtifactStore holding the candidate snapshot
    instance: Any = None
    session: Any = None
    instance_id: str | None = None
    settled: asyncio.Task | None = None
    aborted: bool = False  # a target abort passed the opening/settlement deadline
    forced: bool = False  # sticky: an interrupted turn can never inherit normal settlement
    store_opening: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None  # owns deferred store cleanup and surviving transport evidence
    policy_gateway: Any = None
    policy_use: str | None = None


def _session_id(value) -> str:
    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise OwnerError("invalid_argument", "probe")
    return value


def _turn_binding(binding, turn_timeout_seconds: float | None):
    """The binding the adapter talks through: the manifest's, with a read timeout that lets a turn run for the
    session's whole turn timeout (core's default read timeout is 120 s, below the 600 s a session may ask)."""
    if turn_timeout_seconds is None:
        return binding
    read = max(float(binding.timeouts.read_seconds), turn_timeout_seconds + _TURN_READ_MARGIN)
    return dataclasses.replace(binding, timeouts=dataclasses.replace(binding.timeouts, read_seconds=read))


def _call_seconds(binding) -> float:
    """The declared bound of one target call (connect + read): every probe call but a turn keeps it."""
    return float(binding.timeouts.connect_seconds) + float(binding.timeouts.read_seconds)


def _open_error(error: BaseException) -> OwnerError:
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    if isinstance(error, OwnerError):
        return error
    if isinstance(error, RolloutInfrastructureError):
        code = "target_unavailable" if error.code in _UNREACHABLE else "target_mismatch"
        return OwnerError(code, "probe")
    if isinstance(error, TimeoutError):  # a call over its declared bound (_call_seconds)
        return OwnerError("target_unavailable", "probe")
    if isinstance(error, OSError):
        return OwnerError("io_error", "probe")
    return OwnerError("lab_error", "probe")


def _check_opening(probe):
    if probe.aborted:
        raise OwnerError("target_not_running", "probe")


class ProbeSupport:
    async def probe_open(self, target_ref, *, session_id, seed, turn_timeout_seconds=None) -> ProbeOpened:
        """Open one probe session on the ready target; the seed follows the target's ``seed_mode``.

        ``turn_timeout_seconds`` is the session's turn timeout: the transport lets a turn run that long (plus a
        margin) whatever the manifest's read timeout; ``None`` keeps the manifest's."""
        self._assert_mutation()
        _session_id(session_id)
        if not isinstance(target_ref, TargetRef) or (seed is not None and type(seed) is not int):
            raise OwnerError("invalid_argument", "probe")
        if turn_timeout_seconds is not None and (
                isinstance(turn_timeout_seconds, bool) or not isinstance(turn_timeout_seconds, (int, float))
                or not math.isfinite(turn_timeout_seconds) or turn_timeout_seconds <= 0):
            raise OwnerError("invalid_argument", "probe")
        status = await self.target(target_ref)
        runtime = self._targets.get(target_ref.id)
        if target_ref.owner_generation != self.owner_generation or status.state != "ready" or runtime is None:
            raise OwnerError("target_not_running", "probe")
        binding = runtime.binding
        mode = binding.manifest.target.seed_mode
        if seed is not None and mode != "exact":
            raise OwnerError("invalid_argument", "probe")
        if mode == "exact" and seed is None:
            seed = secrets.randbelow(SEED_BOUND)
        if session_id in self._probes:
            raise OwnerError("invalid_argument", "probe")
        turn_timeout = None if turn_timeout_seconds is None else float(turn_timeout_seconds)
        return await asyncio.shield(self._hold(self._probe_open(binding, status, session_id, seed, turn_timeout)))

    async def _probe_open(self, binding, status, session_id, seed, turn_timeout):
        from evolution.tree import TargetCallLedger

        manifest = binding.manifest
        probe = _ProbeRuntime(session_id, status.id, manifest.target, TargetCallLedger())
        probe.opening = asyncio.current_task()
        if session_id in self._probes:
            raise OwnerError("invalid_argument", "probe")
        # Registered before the admission commits, so a close or a stop never sees a row
        # without its runtime.
        self._probes[session_id] = probe
        try:
            await self._db("admit_probe", session_id=session_id, target_id=status.id,
                           max_instances=binding.launch["limits"]["max_instances"])
        except BaseException:
            self._probes.pop(session_id, None)
            raise
        try:
            if self._stopping:
                raise OwnerError("service_stopping", "probe")
            runtime = self._targets[status.id]
            if runtime.gateway is not None:
                probe.policy_gateway = runtime.gateway
                probe.policy_use = f'probe:{self.owner_generation}:{session_id}'
                await probe.policy_gateway.open_use(probe.policy_use)
            await self._probe_connect(probe, manifest, seed, turn_timeout)
            _check_opening(probe)
            await self._db("set_probe_state", session_id, "open", instance_id=probe.instance_id)
            _check_opening(probe)  # a concurrent abort may have made the store write a no-op
        except BaseException as error:
            # A refused preflight, an error answer or a failure before the wire is definitive: the
            # ledger decides. Only a cancelled or timed-out open is never a normal return (spec §3.3, §3.5).
            await self._settle_probe(
                probe, normal=not probe.aborted and not isinstance(error, (asyncio.CancelledError, TimeoutError)))
            if probe.aborted:
                raise OwnerError("target_not_running", "probe") from None
            raise _open_error(error) from None
        finally:
            probe.opening = None
        return ProbeOpened(
            session_id, probe.instance_id, status.id, self.owner_generation, status.boot_id,
            status.bundle_digest, status.launch_digest, binding.source_kind, manifest.target.seed_mode,
            probe.session.requested_seed, probe.session.effective_seed,
        )

    async def _probe_connect(self, probe, manifest, seed, turn_timeout):
        """Snapshot, preflight, materialize, open: the evaluation worker's sequence (spec §3.2).

        The adapter's read timeout follows the session's turn timeout; these calls keep the manifest's bound."""
        from evolution.tree.artifacts.artifact_policy import scan_tree
        from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
        from evolution.tree.artifacts.artifacts import ArtifactStore, ExpectedAttestation
        from evolution.tree.target.http_adapter import HttpSessionAdapter
        from evolution.tree.execution.wire_v2 import new_instance_id

        source_root = await asyncio.to_thread(lambda: Path(manifest.seed_root).resolve(strict=True))
        scan = await asyncio.to_thread(scan_tree, source_root, manifest.policy)
        _check_opening(probe)
        bound = _call_seconds(probe.binding)
        probe.adapter = HttpSessionAdapter(
            _turn_binding(manifest.target, turn_timeout), manifest.policy, run_id=probe.session_id,
            ledger=probe.ledger)
        await asyncio.wait_for(
            probe.adapter.preflight(ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)), bound)
        _check_opening(probe)
        # Under <state>/artifacts, the target's allowed_artifact_root (target.py runtime_config).
        # Cancellation must not lose a store created by a worker that has already started.
        probe.store_opening = self._hold(asyncio.to_thread(
            ArtifactStore.create, self.state_directory / "artifacts" / f"probe-{probe.session_id}",
            source_root, manifest.policy, parallel_attempts=1,
            expected=ArtifactStoreIdentityV1(manifest.policy, scan.candidate_digest, scan.dependency_digest),
        ))
        probe.store = await asyncio.shield(probe.store_opening)
        probe.store_opening = None
        probe.instance_id = new_instance_id(probe.session_id)
        _check_opening(probe)
        # The admission checked the quarantine; another session may have gone uncertain since (ADR-0021).
        await self._db("check_probe_target", probe.target_id)
        _check_opening(probe)
        probe.facts["materialize_started"] = True
        probe.instance = await asyncio.wait_for(probe.adapter.materialize(
            probe.store.champion(), "artifact_root_injection", probe.instance_id), bound)
        _check_opening(probe)  # a late materialize must not open a session after the target abort
        probe.session = await asyncio.wait_for(probe.instance.open({}, seed, uuid.uuid4().hex), bound)

    async def probe_turn(self, session_id, content, *, timeout) -> ProbeTurnOutcome:
        """One turn; a failed turn closes the session (its state on the target is unknown, spec §3.3)."""
        self._assert_mutation()
        _session_id(session_id)
        if (not isinstance(content, str) or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
            raise OwnerError("invalid_argument", "probe")
        return await asyncio.shield(self._hold(self._probe_turn(session_id, content, float(timeout))))

    async def _probe_turn(self, session_id, content, timeout):
        from evolution.tree import Message
        from evolution.tree.execution.interaction import RolloutInfrastructureError

        async with self._probe_turn_lock:
            probe = self._probes.get(session_id)
            if probe is None or probe.session is None or probe.settled is not None:
                raise OwnerError("not_found", "probe")
            # Another session's unsettled release quarantines the whole target (ADR-0021, spec §3.5): this one
            # sends nothing more and stays open until a target stop closes it.
            await self._db("check_probe_target", probe.target_id)
            probe.turn = asyncio.create_task(
                probe.instance.send(probe.session, Message(content), uuid.uuid4().hex))
            # A timed-out or cancelled send is never a normal return; any other failure is definitive
            # (the target answered with an error, or the call never reached it) and the ledger decides
            # whether the release settles (spec §3.3).
            forced = False
            cancelled = False
            try:
                result = await asyncio.wait_for(probe.turn, timeout)
            except TimeoutError:
                failure, forced = "turn_timeout", True
            except RolloutInfrastructureError as error:
                failure = "target_unreachable" if error.code in _UNREACHABLE else "protocol_error"
            except asyncio.CancelledError:
                cancelled = bool(asyncio.current_task().cancelling())
                failure, forced = "target_unreachable", True  # a target stop or Owner close cancelled the send
            except Exception:
                failure = "protocol_error"
            else:
                return ProbeTurnOutcome(result, None, None)
            finally:
                probe.forced |= forced
                probe.turn = None
            closed = await self._settle_probe(probe, normal=not forced)
            if forced:
                self._hold(self._abort_probe_target(probe.target_id))
            if cancelled:
                raise asyncio.CancelledError
            return ProbeTurnOutcome(None, failure, closed)

    async def probe_close(self, session_id) -> str:
        """Close and release; ``released`` only on a positive settlement. Idempotent."""
        self._assert_mutation()
        _session_id(session_id)
        return await asyncio.shield(self._hold(self._probe_close(session_id)))

    async def _probe_close(self, session_id):
        probe = self._probes.get(session_id)
        if probe is not None:
            if probe.opening is not None:
                await asyncio.wait((probe.opening,))
            async with self._probe_turn_lock:
                return await self._settle_probe(probe, normal=True)
        state = await self._probe_state(session_id)
        # A row left unsettled by a failed store write has no runtime any more: its instance is
        # unaccounted for, so the answer is uncertain until a target stop releases it.
        return state if state in _SETTLED else "uncertain"

    async def probe_states(self) -> dict[str, str]:
        """``session_id -> state`` of this generation's rows plus ``uncertain`` rows of any generation."""
        self._assert_loop()
        return {session: state for session, generation, state in await self._db("probe_rows")
                if generation == self.owner_generation or state == "uncertain"}

    async def _probe_state(self, session_id):
        for session, _, state in await self._db("probe_rows"):
            if session == session_id:
                return state
        raise OwnerError("not_found", "probe")

    async def _close_probes_for(self, target_id, *, deadline: float | None = None):
        """Close a target's sessions; an abort deadline never waits for a late opening or settlement."""
        probes = [probe for probe in self._probes.values() if probe.target_id == target_id]
        if deadline is None:
            await asyncio.gather(*(self._shut_probe(probe) for probe in probes), return_exceptions=True)
            return
        if not probes:
            return
        shutting = {self._hold(self._shut_probe(probe)): probe for probe in probes}
        _, late = await asyncio.wait(shutting, timeout=max(0.0, deadline - self._loop.time()))
        for task in late:
            probe = shutting[task]
            already_aborted = probe.aborted
            probe.aborted = True
            probe.ledger.close()  # refuses NEW admissions; already-started I/O still needs draining
            if probe.opening is not None and not already_aborted:
                probe.opening.cancel()  # Core withdraws queued calls; started workers remain ledgered
            try:
                await self._db("set_probe_state", probe.session_id, "uncertain")
            except Exception:
                pass  # as at Owner close: an unsettled row still blocks target use

    async def _abort_probe_target(self, target_id: str) -> None:
        """Owner-held abort after a timed-out/cancelled turn; never part of the turn's reply wait."""
        runtime = self._targets.get(target_id)
        if runtime is None:
            return
        if runtime.probe_abort_deadline is None:
            runtime.probe_abort_deadline = self._loop.time()
        runtime.probe_abort_started.set()
        for probe in list(self._probes.values()):
            if probe.target_id == target_id and probe.turn is not None:
                probe.turn.cancel()
        await self._close_probes_for(target_id, deadline=runtime.probe_abort_deadline)
        # Shutdown already owns stopping every target; a finished watcher already recorded its cleanup.
        if self._stopping or runtime.ended.done():
            return
        await self._admit_target_stop(runtime.ref, runtime)

    async def _close_all_probes(self, deadline):
        """Close every probe session at an Owner close, waiting for openings and settlements at most until
        ``deadline`` (loop time). A session still opening or settling then is marked ``uncertain``, never
        ``released`` (its instance is unaccounted for), and the close goes on to stop the targets; the late
        settlement can no longer move the row (``set_probe_state`` keeps a settled row)."""
        probes = list(self._probes.values())
        if not probes:
            return
        shutting = {self._hold(self._shut_probe(probe)): probe for probe in probes}
        _, late = await asyncio.wait(shutting, timeout=max(0.0, deadline - self._loop.time()))
        for task in late:
            try:
                await self._db("set_probe_state", shutting[task].session_id, "uncertain")
            except Exception:
                pass  # the row stays unsettled: the next generation marks it uncertain

    async def _shut_probe(self, probe):
        if probe.opening is not None:
            await asyncio.wait((probe.opening,))  # an open settles itself on failure
        turn = probe.turn
        interrupted = turn is not None and (
            not turn.done() or turn.cancelled() or isinstance(turn.exception(), TimeoutError))
        if interrupted:
            probe.forced = True
            probe.turn.cancel()  # a cancelled send is never a normal return (spec §3.5)
        await self._settle_probe(probe, normal=not interrupted and not probe.aborted)

    async def _settle_probe(self, probe, *, normal):
        """Single flight; an interrupted turn's sticky forced flag forbids normal settlement.

        ``normal=False`` forces ``uncertain`` (a timed-out or cancelled send, spec §3.5); otherwise
        ``normal_return`` is whether our own close/release path raised nothing, and the ledger decides.
        """
        probe.forced |= not normal
        if probe.settled is None:
            probe.settled = self._hold(self._settle_probe_once(probe, normal))
        return await asyncio.shield(probe.settled)

    async def _settle_probe_once(self, probe, normal):
        """Settle one runtime; the answer is only ever ``released`` or ``uncertain``."""
        clean = normal
        state = "uncertain"
        try:
            await self._db("set_probe_state", probe.session_id, "releasing")
            if not normal or probe.forced or probe.aborted:
                # Timeout/cancel is unconditionally uncertain: no wire cleanup or ledger wait can improve it.
                # The ledger closes now; owned store cleanup is deferred, never part of the reply wait.
                pass
            elif probe.adapter is None:
                # No transport was ever bound, so no call can have reached the target.
                state = "released"
            else:
                # Cleanup keeps the manifest's call bound, not the turn's (a timed-out call is not clean).
                bound = _call_seconds(probe.binding)
                if probe.session is not None:
                    try:
                        await asyncio.wait_for(probe.instance.close(probe.session), bound)
                    except Exception:
                        clean = False
                if probe.facts["materialize_started"]:
                    probe.facts["release_started"] = True
                    try:
                        await asyncio.wait_for(probe.adapter.release(probe.instance_id), bound)
                    except Exception:
                        clean = False
                probe.ledger.close()
                settled = await probe.ledger.settle(settle_timeout(probe.binding))
                value = {"normal_return": clean, **probe.facts, "ledger": ledger_payload(settled)}
                positive = positive_settlement(value, run_id=probe.session_id, kind="evaluation")
                state = "released" if positive else "uncertain"
        except Exception:
            state = "uncertain"
        finally:
            probe.ledger.close()
            if not normal or probe.forced or probe.aborted:
                self._defer_probe_cleanup(probe)
            elif probe.store is not None:
                try:
                    await asyncio.to_thread(probe.store.close)
                    probe.store = None
                except Exception:
                    pass
        forced = not normal or probe.forced or probe.aborted
        if not forced and not probe.ledger.report().in_flight:
            await self._close_probe_policy_use(probe)
        try:
            if forced:
                state = "uncertain"
            await self._db("set_probe_state", probe.session_id, state)
            # A confirmed target stop may have released the row meanwhile: the store has the answer.
            stored = await self._probe_state(probe.session_id)
        except Exception:
            stored = None
        if forced:
            # _db uses Owner's dedicated executor, never Core HTTP's default executor. No deferred
            # store close, surviving socket I/O, or snapshot deletion belongs to this reply wait.
            return "uncertain"
        if probe.ledger.report().in_flight:
            self._defer_probe_cleanup(probe)
        else:
            self._probes.pop(probe.session_id, None)
        answer = stored if stored in _SETTLED else "uncertain"
        if answer == "released":
            # A late aborted opening may finish only after a confirmed stop released its row.
            await self._discard_probe_snapshot(probe.session_id)
        return answer

    @staticmethod
    async def _close_probe_policy_use(probe):
        if probe.policy_use is not None:
            await probe.policy_gateway.close_use(probe.policy_use)
            probe.policy_use = None

    def _defer_probe_cleanup(self, probe):
        if probe.cleanup is None:
            probe.cleanup = self._hold(self._retire_probe(probe))

    async def _retire_probe(self, probe):
        """Keep ownership after the reply: cancelled coroutines do not stop Core's HTTP workers."""
        if probe.settled is not None:
            await asyncio.gather(asyncio.shield(probe.settled), return_exceptions=True)
        probe.ledger.close()
        if probe.ledger.report().in_flight:
            await probe.ledger.settle()  # public waiter has its own thread, not the default executor
        if probe.store_opening is not None:
            try:
                probe.store = await asyncio.shield(probe.store_opening)
            except Exception:
                pass
            probe.store_opening = None
        if probe.store is not None:
            try:
                await asyncio.to_thread(probe.store.close)
            except Exception:
                pass
            probe.store = None
        await self._close_probe_policy_use(probe)
        self._probes.pop(probe.session_id, None)
        try:
            released = await self._probe_state(probe.session_id) == "released"
        except Exception:
            released = False
        if released:
            await self._discard_probe_snapshot(probe.session_id)

    def _probe_io_clear(self, target_id):
        # A closed ledger with zero in_flight proves no started/queued mutating worker can still
        # send. uncertain/pending delivery is NOT positive settlement and does not release a row.
        reports = [probe.ledger.report() for probe in self._probes.values() if probe.target_id == target_id]
        return all(report.admission_closed and report.in_flight == 0 for report in reports)

    async def _wait_probe_io(self, target_id):
        probes = [probe for probe in self._probes.values() if probe.target_id == target_id]
        for probe in probes:
            probe.ledger.close()
        await asyncio.gather(*(probe.ledger.settle() for probe in probes if probe.ledger.report().in_flight))

    async def _discard_released_snapshots(self):
        """Best effort: once a confirmed target stop or cleanup released probe rows, their snapshots go too.

        Only rows the store holds ``released`` and that no runtime still owns for settlement/cleanup (spec §3.5).
        """
        root = self.state_directory / "artifacts"
        try:
            released = {session for session, _, state in await self._db("probe_rows")
                        if state == "released" and session not in self._probes}
            names = await asyncio.to_thread(lambda: [path.name for path in root.iterdir()] if root.is_dir() else [])
        except Exception:
            return
        for name in names:
            if name.startswith("probe-") and name[len("probe-"):] in released:
                await self._discard_probe_snapshot(name[len("probe-"):])

    async def _discard_probe_snapshot(self, session_id):
        """Best effort: a released session's candidate snapshot is no longer evidence of anything.

        An ``uncertain`` session keeps it: the instance that may still read it is unaccounted for.
        """
        from .results import delete_inventory

        entry = {"path": f"artifacts/probe-{session_id}", "kind": "tree"}
        try:
            remaining = await asyncio.to_thread(delete_inventory, self.state_directory, (entry,))
        except Exception:
            remaining = (entry,)
        if remaining:
            _log.warning("probe %s: snapshot directory not removed", session_id)
