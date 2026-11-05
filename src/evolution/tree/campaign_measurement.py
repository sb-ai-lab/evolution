"""One measurement: an S1 journal, its target instances and its staged dispatch.

A `Measurement` is one S1 `Journal` for one Candidate (spec section 4's
"measurement derivation"), the target instances the campaign materializes for
it (section 6 "Target instances"), and the staged dispatch of its frozen
schedule over the R1 rollout and the S1 scorer (section 6 "Staged
measurement"). It owns no campaign record: the executor (Task 7) writes
`stage_opened`, `measurement_bound` and `instance_attested` to
`campaign.jsonl`; this layer only produces the durable facts and the evidence
references those records carry.

The measurement invariants in this module amend
the task brief:

1. `sample_rows(look)` replaces status-based extraction: one row per scheduled
   `(case, repeat <= look)` **including not-started ones**
   (`disposition = "not_started"`, `outcome = None`, `metrics = None`), so a
   caller's union of rows equals the schedule. Provenance is derived from
   durable facts, never from S1 payloads (which carry none): `requested_seed`
   from the frozen schedule; `effective_seed = requested_seed` under `exact`
   (R1 refuses a session that does not confirm the requested seed, so a
   `rollout_finished` proves it) and `None` otherwise; `dependency_digest`
   from the measurement identity's `target`; `boot_id` from the identity the
   instance reported at materialization when this process materialized it,
   else `""`; `outcome` from the sample's terminal -- its execution record, or
   its scoring record when scoring reached a terminal of its own -- and
   `metrics`/`critical_pass` from `scoring_finished`.
2. Attestation at materialization is C1's: after `materialize_target`, the
   instance's `identity()` is verified with `verify_attestation` and the
   verified `TargetIdentity` is kept per instance, in `Measurement.instances`
   while the instance is live and in `Measurement.attested` (and in every
   `StageOutcome`) for the life of the measurement, so the executor can write
   `instance_attested` after a stage that already released its instances. On
   `AttestationError` the instance is released, the journal's prefix through
   that release kept as `Measurement.refusal_prefix`, and the error raised, and
   no sample is started. An adapter that refuses inside `materialize` leaves
   no instance: the prefix then ends at its `target_materialize_failed`.
3. `prefix_reference()` is a `journal_prefix` over a fresh `journal.snapshot()`
   and `record_reference()` a `journal_record` over one try's terminal
   record. Both locators are run-root-relative
   (`measurements/<id>/events.jsonl#bytes=<start>:<end>`) and both byte ranges
   are the ones `append_log.append` laid down: `Envelope.encode()` (payload
   bytes plus the record separator `\\n`) is written whole, so a record's range
   is the running sum of the encoded lengths before it.
4. `declare(...)` creates nothing on disk; `create()` is called only after the
   executor's `measurement_bound` is durable. `classify` and `remove_unborn`
   are static and lease-free (`inspect_log`), so a crashed creation can be
   classified without taking the journal's lease.
5. `run_stage` materializes `instances_per_measurement` instances when none are
   live, with campaign-scoped ids (`new_instance_id(campaign_run_id)`, the R3
   grammar). A semaphore of `max_concurrency` bounds concurrent rollouts and
   the permit is taken **before** `durable_rollout`, so queue time never comes
   out of a sample's deadline (R2a rule 3); `durable_score` runs under
   `max_scorer_concurrency`. Instances are not assigned by a static
   round-robin: they are **checked out of a pool** that holds each instance
   `max_concurrent_sessions` times, so the admitted per-instance session bound
   holds whatever the sample durations are. The pool has
   `instances_per_measurement * max_concurrent_sessions >= max_concurrency`
   slots, so a holder of the global permit never queues behind a busy
   instance. No sample's *classified* failure cancels a sibling: R1 and S1
   have already written that sample's durable terminal and it stands; an
   unclassified failure is re-raised once the whole stage is terminal (never
   mid-stage), because nothing durable records it. Instances are released at
   the measurement's last stage; the executor also calls `release_all` at
   attempt closure and at the terminal barrier. `resume` first closes journal
   operations interrupted by the earlier process, then recovers instances. A
   release the target refuses there is a `leaked` instance, not a failed resume.

Two names this module fixes that the brief left open. The S1 journal's own
`run_id` is the `measurement_id` (so a reference's `run_id` and its locator
name the same thing), and the campaign's run id is a separate `declare`
argument used only to scope instance ids, per ruling 5's "campaign-scoped".
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.artifacts.attestation import ComponentsChanged, verify_attestation
from evolution.tree.campaign_identity import Side, instances_per_measurement, schedule_for
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_statistics import (
    NO_PRIMARY_RANGE,
    PrimaryRange,
    SampleRow,
    latest_rows,
    owed_repair_rows,
    owed_threshold_rows,
    row_fault,
    sample_row,
)
from evolution.tree.component_drift import ComponentsCheck
from evolution.tree.execution import config as _config
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.interaction import (
    AttestationError,
    Environment,
    EvalCase,
    MaterializationStrategy,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    Scorer,
    SemanticIdentity,
    TargetAdapter,
    TargetIdentity,
    TargetInstance,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    PARTIAL_FILE,
    Journal,
    inspect_journal,
)
from evolution.tree.execution.journal_identity import RunIdentityV1
from evolution.tree.execution.journal_projections import project_status
from evolution.tree.execution.journal_recovery import close_interrupted_operations, recovery_plan
from evolution.tree.execution.journal_rollout import (
    ConnectionAdmission,
    JournalRolloutAdapter,
    durable_rollout,
)
from evolution.tree.execution.journal_schema import (
    S1_FAMILY,
    Lifecycle,
    components_digest,
    try_is_retryable,
)
from evolution.tree.execution.journal_scoring import durable_score
from evolution.tree.execution.rollout import (
    materialize_target,
    recover_target_instances,
    release_target,
)
from evolution.tree.execution.rollout_scoring import ScoringError
from evolution.tree.execution.sample_outcomes import (
    SampleDisposition,
    SampleOutcome,
    disposition_for,
)
from evolution.tree.execution.wire_v2 import new_instance_id
from evolution.tree.storage.append_log import Snapshot, inspect_log
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalBoundaryError,
    JournalFault,
    JournalRecoveryRequired,
)

MEASUREMENTS_DIR = "measurements"


Birth = Literal["absent", "unborn", "born"]

# Every failure `durable_rollout` and `durable_score` turn into a durable
# terminal of their own: `rollout_failed(environment_failed | time_budget |
# infrastructure_failed | target_failed)` and `scoring_failed(scorer_error)`.
# Such a failure is that sample's recorded outcome, so it never leaves the
# stage. `RolloutTargetUnstable` (I1, spec §4) is an exhausted transient
# target call under `/8`'s `call_retries` -- indeterminate, owned by `target`,
# retried like any other indeterminate sample, never a fault. Anything else --
# an `IdentityError`, a `JournalRecoveryRequired`, a `JournalFault`, a bug --
# records nothing and must reach the executor.
_CLASSIFIED: tuple[type[BaseException], ...] = (
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    ScoringError,
)

__all__ = [
    "MEASUREMENTS_DIR",
    "Birth",
    "FrozenMeasurement",
    "Measurement",
    "MeasurementError",
    "StageOutcome",
    "StagePlan",
    "stage_plans",
]


class _SampleReads(Protocol):
    """The two row reads `unfilled_slots` needs: a live `Journal` or a frozen view's."""

    def sample_records(
        self, case_id: str, sample_id: str
    ) -> tuple[Envelope | None, Envelope | None]: ...

    def imported_record(self, case_id: str, sample_id: str) -> Envelope | None: ...


def unfilled_slots(
    schedule: Sequence[Mapping[str, Any]],
    reads: _SampleReads,
    look: int,
    reserved: Collection[tuple[str, str, int]],
    declined: Collection[tuple[str, str, int]] = (),
) -> int:
    """The denominator slots the campaign reserved on a measurement that no try filled.

    I2 (codex branch review round 1, PDR-0016) declares them as a frozen
    source's `released`; #109 (a28 spec §3.1) moves them, on a live
    measurement, into the window a carried stage is dispatched again in. One
    derivation for both. `schedule` is the measurement identity's, `reads` its
    journal (frozen or live), `look` the latest stage the campaign opened on it
    and `reserved` the `(case_id, sample_id, retry_ordinal)` of every try whose
    `indeterminate_observed` said a further try follows it. A scheduled, not
    imported row up to `look` holds one open slot when it never started, when
    its latest try started without a terminal (unless that try is an I1
    repair, which holds no slot), or when its latest try ended having reserved
    a retry that never started. Everything else was filled by a terminal of
    its own.

    `declined` (family 13, pre-F8 spec §2.4) is the `(case_id, sample_id,
    retry_ordinal)` of every try the campaign declined to retry. A try started
    after one of them is the D1 retry a listing's grant restored: like a
    repair, it takes its grant's slot and holds no stage slot.
    """

    unfilled = 0
    for entry in schedule:
        if int(entry["repeat"]) > look:
            continue
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        if reads.imported_record(*key) is not None:
            continue
        execution, _scoring = reads.sample_records(*key)
        if execution is None:
            unfilled += 1
        elif execution.event == "rollout_started":
            ordinal = int(execution.payload.get("retry_ordinal", 0))
            unfilled += int(
                execution.payload.get("repair") is not True and (*key, ordinal - 1) not in declined
            )
        else:
            ordinal = int(execution.payload.get("retry_ordinal", 0))
            unfilled += int((*key, ordinal) in reserved)
    return unfilled


class MeasurementError(ValueError):
    """A closed-code refusal from the measurement layer; `str()` is the code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FrozenMeasurement:
    """A read-only view of a measurement no one will append to again (I2 spec §5.1).

    After an in-place condition change, a measurement bound in an earlier
    condition epoch is frozen. It cannot be reopened with `Measurement.resume`:
    `Journal.resume` checks the journal against the *session's* corpus, and
    after a corpus change the session's corpus is no longer the one that
    measurement was derived from. It must not be resumed either, since resume
    closes interrupted tries and releases instances through the adapter.

    This view reads the durable prefix once with `inspect_journal` (no lease,
    no append, no recovery, no adapter, no corpus) and answers the same row
    questions `Measurement` does, over those bytes. A torn tail
    (`incomplete_tail`) is tolerated: nothing will append or truncate it, so the
    valid prefix is stable and `prefix_digest` names exactly that prefix.
    """

    __slots__ = (
        "_records",
        "_snapshot",
        "_state",
        "candidate_digest",
        "identity_digest",
        "journal_integrity",
        "measurement_id",
        "prefix_digest",
        "valid_bytes",
    )

    def __init__(self, run_root: Path, measurement_id: str) -> None:
        directory = Measurement.directory_for(run_root, measurement_id)
        try:
            snapshot = inspect_journal(directory)
        except (OSError, JournalBoundaryError, ValueError) as exc:
            raise MeasurementError("frozen_measurement_unreadable") from exc
        if (
            not snapshot.records
            or snapshot.journal_integrity not in ("complete", "incomplete_tail")
            or snapshot.records[0].event != "run_created"
            or snapshot.records[0].to_payload()["run_id"] != measurement_id
        ):
            raise MeasurementError("frozen_measurement_unreadable")
        state = Lifecycle()
        for record in snapshot.records:
            state.accept(record)
        created = snapshot.records[0].payload
        self.measurement_id = measurement_id
        self.identity_digest = str(created["identity_digest"])
        self.candidate_digest = str(created["identity"]["candidate_digest"])
        self.prefix_digest = snapshot.prefix_digest
        self.valid_bytes = snapshot.valid_bytes
        # `complete` or `incomplete_tail`: a report or audit can say the frozen
        # prefix ends on a torn record (tested; no Task 6 caller reads it yet).
        self.journal_integrity = snapshot.journal_integrity
        self._records = snapshot.records
        self._snapshot = snapshot
        self._state = state

    def records(self) -> tuple[Envelope, ...]:
        """The durable prefix as read at open; it cannot grow."""

        return self._records

    def reader(
        self,
        run_root: Path,
        *,
        campaign_run_id: str,
        corpus: Sequence[EvalCase],
        secrets: Sequence[str] = (),
    ) -> Measurement:
        """A `Measurement` whose every read answers from this frozen prefix (I2 Task 7).

        A campaign reads a frozen measurement exactly as it reads a live one --
        a complete baseline is still the other side of an Attempt's looks, and
        its rows, references and status go into evidence and reports -- so the
        reader is a `Measurement` over a journal stand-in that refuses every
        write. It holds no instance handle, so a release has nothing to give
        back; the instances its journal still owns on the old target are its
        `leaked` ones (never released through the adapter: that target is no
        longer the one this campaign talks to).
        """

        created = self._records[0].payload
        measurement = Measurement(
            run_root=run_root,
            campaign_run_id=campaign_run_id,
            measurement_id=self.measurement_id,
            identity=RunIdentityV1.from_payload(created["identity"]),
            corpus=corpus,
            secrets=secrets,
        )
        journal = _FrozenJournal(self._snapshot, self._state, secrets=tuple(secrets))
        measurement._journal = cast(Journal, journal)
        for owned in recovery_plan(self._records).cleanup:
            measurement._leaked[owned.instance_id] = None
        return measurement

    def sample_rows(self, look: int) -> tuple[SampleRow, ...]:
        """`Measurement.sample_rows` over the frozen prefix: each scheduled pair's latest try.

        No instance of this process served any sample, so `boot_id` is `""`
        throughout, as for another process's rows.
        """

        target = self._records[0].payload["identity"]["target"]
        return latest_rows(
            self._records,
            self._state,
            look=look,
            seed_mode=str(target["seed_mode"]),
            dependency_digest=str(target["dependency_digest"]),
        )

    def complete(self) -> bool:
        """Every scheduled sample scored (spec section 6's baseline barrier)."""

        return all(row.disposition == "score" for row in self.sample_rows(self._max_repeat()))

    def unfilled_slots(
        self,
        look: int,
        reserved: Collection[tuple[str, str, int]],
        declined: Collection[tuple[str, str, int]] = (),
    ) -> int:
        """`unfilled_slots` over the frozen prefix: the slots no try ever filled.

        I2 (codex branch review round 1, PDR-0016): a `measurement_rebound`'s
        `released`, which the executor and the audit both read here.
        """

        return unfilled_slots(
            self._records[0].payload["identity"]["schedule"],
            _FrozenJournal(self._snapshot, self._state, secrets=()),
            look,
            reserved,
            declined,
        )

    def unfilled_threshold_repairs(self, named: Mapping[tuple[str, str], tuple[int, int]]) -> int:
        """#78 (spec §3.5): named pairs whose repair never started, or started with no terminal.

        `named` is the campaign's `threshold_named` for this measurement, `(case_id,
        sample_id) -> (ordinal, naming_seq)`. A pair still at its named ordinal has
        not started its repair; one whose latest try is the next ordinal's
        `rollout_started` started it without a terminal. Neither filled the slot
        its `repairs_dispatched` reserved; `unfilled_slots` counts neither (a
        started repair holds no stage slot).
        """

        unfilled = 0
        for key, (ordinal, _naming) in named.items():
            execution = self._state.executions.get(key)
            if execution is None:
                continue
            latest = int(execution.payload.get("retry_ordinal", 0))
            if latest == ordinal or (
                latest == ordinal + 1 and execution.event == "rollout_started"
            ):
                unfilled += 1
        return unfilled

    @property
    def max_repeat(self) -> int:
        """The largest scheduled repeat: R, or a `case_repeats` count (design §5)."""

        return self._max_repeat()

    def _max_repeat(self) -> int:
        schedule = self._records[0].payload["identity"]["schedule"]
        return max((int(row["repeat"]) for row in schedule), default=0)


class _FrozenJournal:
    """The read half of `Journal` over a frozen prefix; every write is refused.

    Only what `Measurement`'s reads call: the snapshot and the lifecycle
    indexes, answered from the prefix `FrozenMeasurement` read once.
    """

    recovery_required = False
    healthy = False

    def __init__(self, snapshot: Snapshot, state: Lifecycle, *, secrets: tuple[str, ...]) -> None:
        self._snapshot = snapshot
        self._state = state
        self.secrets = secrets

    def snapshot(self) -> Snapshot:
        return self._snapshot

    def sample_records(
        self, case_id: str, sample_id: str
    ) -> tuple[Envelope | None, Envelope | None]:
        key = (case_id, sample_id)
        return self._state.executions.get(key), self._state.scoring.get(key)

    def imported_record(self, case_id: str, sample_id: str) -> Envelope | None:
        return self._state.imported.get((case_id, sample_id))

    def declared_components(self) -> Mapping[str, Envelope]:
        return MappingProxyType(dict(self._state.declared_components))

    def sample_tries(
        self, case_id: str, sample_id: str
    ) -> tuple[tuple[Envelope, Envelope | None], ...]:
        key = (case_id, sample_id)
        if key not in self._state.executions:
            return ()
        latest = (self._state.executions[key], self._state.scoring.get(key))
        return (*self._state.tries.get(key, ()), latest)

    def sample_repairs(self, case_id: str, sample_id: str) -> int:
        return self._state.repairs.get((case_id, sample_id), 0)

    def append(self, *args: Any, **kwargs: Any) -> int:
        raise MeasurementError("measurement_frozen")

    def finish_recovery(self) -> None:
        raise MeasurementError("measurement_frozen")

    def invalidate(self) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class StagePlan:
    """The schedule rows of one look stage: repeats in `(L_{k-1}, L_k]`, and on
    the last stage every remaining row (design §5)."""

    stage: int
    look: int
    rows: tuple[dict[str, Any], ...]
    scope: Literal["focus", "causal"] | None = None

    @property
    def horizon(self) -> int:
        """The largest repeat this stage reads: its look, or past it on the
        last stage of a `case_repeats` measurement (design §5)."""

        return max((self.look, *(int(row["repeat"]) for row in self.rows)))


@dataclass(frozen=True)
class StageOutcome:
    """What one completed stage proves about the measurement, for the executor."""

    stage: int
    look: int
    #: The plan's `horizon`: the largest repeat the stage reads (design §5).
    horizon: int
    terminals: int
    indeterminate: tuple[dict[str, Any], ...]
    faults: tuple[dict[str, Any], ...]
    critical_failed: bool
    leaked: tuple[str, ...]
    # Every instance this measurement has attested so far, so the executor can
    # write `instance_attested` from a stage that already released them.
    attested: Mapping[str, TargetIdentity]
    # What each of those instances reported about itself at materialization
    # (PDR-0014), keyed the same way and absent for an instance that described
    # nothing. It travels beside `attested`, never inside it: the identity is
    # compared whole after every session and the description is not identity.
    attested_description: Mapping[str, Mapping[str, Any] | None]
    # G2 spec §4.2: the digest of each attested instance's `components_declared`
    # in this measurement's S1 journal, `None` for an unmarked (pre-family-12)
    # measurement. Like the description it travels beside the identity.
    attested_components_digest: Mapping[str, str | None] = field(
        default_factory=lambda: MappingProxyType({})
    )


def stage_plans(
    policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    corpus: Sequence[EvalCase],
    seed_mode: str,
    side: Side,
) -> tuple[StagePlan, ...]:
    """Partition the frozen schedule into one plan per declared look (spec section 6)."""

    return _partition(policy.looks, schedule_for(policy, corpus, seed_mode, side))


def _partition_by_scope(
    look: int, rows: Sequence[Mapping[str, Any]], focus: frozenset[str], causal: frozenset[str]
) -> tuple[StagePlan, StagePlan]:
    """Keep the frozen row order, partitioning cases across all R repeats."""
    if focus & causal or any(row["case_id"] not in focus | causal for row in rows):
        raise ValueError("gate scopes must partition the frozen measurement schedule")
    return (
        StagePlan(1, look, tuple(dict(row) for row in rows if row["case_id"] in focus), "focus"),
        StagePlan(2, look, tuple(dict(row) for row in rows if row["case_id"] in causal), "causal"),
    )


def _partition(looks: Sequence[int], rows: Sequence[Mapping[str, Any]]) -> tuple[StagePlan, ...]:
    """One plan per declared look: repeats in `(L_{k-1}, L_k]`, and the last
    stage every remaining row (design §5, a `case_repeats` measurement).

    Without `case_repeats` no row passes the last look, so the plans are the
    `(L_{k-1}, L_k]` partition they always were.
    """

    plans: list[StagePlan] = []
    previous = 0
    last = len(looks)
    for stage, look in enumerate(looks, start=1):
        chosen = tuple(
            dict(row)
            for row in rows
            if previous < int(row["repeat"]) and (stage == last or int(row["repeat"]) <= look)
        )
        plans.append(StagePlan(stage, look, chosen))
        previous = look
    return tuple(plans)


@dataclass(frozen=True)
class StageDispatch:
    """One measurement's prepared stage: the rows it dispatches, each ready to start.

    `Measurement.prepare_stage` builds it and `Measurement.settle_stage` reads
    it back; between the two a caller starts every row, alone (`run_stage`) or
    interleaved with another measurement's (`run_interleaved`, G2 spec §4.6).
    """

    measurement: Measurement
    plan: StagePlan
    # The rows this stage dispatches, in plan order: the carried ones are left out.
    rows: tuple[Mapping[str, Any], ...]
    # One row's `_run_row` coroutine, under the permits the stage was prepared with.
    start: Callable[[Mapping[str, Any]], Awaitable[None]]


async def run_interleaved(
    dispatches: Sequence[StageDispatch],
    order: Callable[[int, Mapping[str, Any]], tuple[Any, ...]],
) -> list[list[Any]]:
    """Start every dispatch's rows in one merged order (G2 spec §4.6).

    `order` keys a row by its dispatch's index and the row itself. The tasks
    are created in that order, and the dispatches share their permits, so
    with FIFO semaphores the rows start in that order: the Candidate's and the
    fresh Parent's rows of one case and repeat run side by side. Each
    dispatch's results come back in its own `rows` order, as `settle_stage`
    pairs them, with `return_exceptions=True`'s semantics.
    """

    merged = sorted(
        (
            (order(index, row), index, position)
            for index, dispatch in enumerate(dispatches)
            for position, row in enumerate(dispatch.rows)
        ),
        key=lambda item: item[0],
    )
    tasks = [
        (
            index,
            position,
            asyncio.ensure_future(dispatches[index].start(dispatches[index].rows[position])),
        )
        for _key, index, position in merged
    ]
    gathered = await asyncio.gather(
        *(task for _index, _position, task in tasks), return_exceptions=True
    )
    results: list[list[Any]] = [[None] * len(dispatch.rows) for dispatch in dispatches]
    for (index, position, _task), result in zip(tasks, gathered, strict=True):
        results[index][position] = result
    return results


class Measurement:
    """One S1 journal for one Candidate, its instances and its staged dispatch."""

    def __init__(
        self,
        *,
        run_root: Path,
        campaign_run_id: str,
        measurement_id: str,
        identity: RunIdentityV1,
        corpus: Sequence[EvalCase],
        secrets: Sequence[str] = (),
        primary_range: PrimaryRange = NO_PRIMARY_RANGE,
    ) -> None:
        payload = identity.to_payload()
        self.run_root = Path(run_root)
        self.campaign_run_id = campaign_run_id
        self.measurement_id = measurement_id
        self.identity = identity
        self.corpus = tuple(corpus)
        self.secrets = tuple(secrets)
        #: #97 spec §4.1: which scored rows a primary outside [0, 1] makes a
        #: fault row -- the live ones under the version in force, the carried
        #: ones also under their carry's (ADR-0019 as amended 2026-10-03).
        self.primary_range = primary_range
        self.candidate_digest = str(payload["candidate_digest"])
        self._schedule = tuple(dict(row) for row in payload["schedule"])
        self._seed_mode = str(payload["target"]["seed_mode"])
        self._dependency_digest = str(payload["target"]["dependency_digest"])
        self._max_repeat = max(int(row["repeat"]) for row in self._schedule)
        self._journal: Journal | None = None
        self._instances: dict[str, TargetIdentity] = {}
        self._live: dict[str, TargetInstance] = {}
        # `_attested` outlives every release: the executor's `instance_attested`
        # records are written from it, and a sample row's `boot_id` reads it.
        self._attested: dict[str, TargetIdentity] = {}
        # Parallel to `_attested` and written at the same moment: the creation
        # body's `description`, captured once per instance and never re-read.
        self._attested_description: dict[str, Mapping[str, Any] | None] = {}
        #: G2b1: the digest of the component map each instance declared at
        #: creation, `None` when it declared none or the measurement is not
        #: family 12. Rows (Task 6) and attestations (Task 7) name it.
        self._components_digest: dict[str, str | None] = {}
        self._leaked: dict[str, None] = {}
        # Released through the adapter alone, because the journal could not
        # record it; the ids are not leaked -- the target has them back.
        self._unjournaled: dict[str, None] = {}
        self._sample_instances: dict[tuple[str, str], str] = {}
        self._materialization: tuple[str, MaterializationStrategy] | None = None
        #: How often materialize/release is attempted (I1), set with
        #: `_materialization` from the policy's `call_retries`.
        self._call_attempts: int | None = None
        #: G2b3b: the reference map an instance must render, armed by the
        #: campaign before each materialization; `None` checks nothing.
        self.components_check: ComponentsCheck | None = None
        #: #287: called with each new instance's id and creation-body
        #: description once it attested, before any row runs on it; it raises
        #: to refuse the instance, which is then released. Armed by the
        #: campaign before each materialization; `None` checks nothing.
        self.description_check: Callable[[str, Mapping[str, Any] | None], None] | None = None
        #: a27 spec §4.1 (#94): the `prefix_reference()` payload taken right
        #: after the latest materialization journaled a refused instance's
        #: release -- or, when the adapter refused inside `materialize`, its
        #: `target_materialize_failed` -- before the `AttestationError` left it;
        #: `None` until a refusal, and reset at each materialization. The
        #: executor cites it, not a prefix taken later: on a resumed open stage
        #: `refuse_rows` appends before the refusal is decided.
        self.refusal_prefix: dict[str, Any] | None = None

    # -- declaration and birth --------------------------------------------

    @classmethod
    def declare(
        cls,
        *,
        run_root: Path,
        campaign_run_id: str,
        measurement_id: str,
        identity: RunIdentityV1,
        corpus: Sequence[EvalCase],
        secrets: Sequence[str] = (),
        primary_range: PrimaryRange = NO_PRIMARY_RANGE,
    ) -> Measurement:
        """Freeze a measurement's derivation. Nothing is created on disk here."""

        return cls(
            run_root=run_root,
            campaign_run_id=campaign_run_id,
            measurement_id=measurement_id,
            identity=identity,
            corpus=corpus,
            secrets=secrets,
            primary_range=primary_range,
        )

    @staticmethod
    def open_frozen(run_root: Path, measurement_id: str) -> FrozenMeasurement:
        """Open a frozen measurement read-only (I2 spec §5.1); see `FrozenMeasurement`.

        Raises `MeasurementError("frozen_measurement_unreadable")` for a journal
        that is absent, unborn, fails its replay or has a corrupt suffix.
        """

        return FrozenMeasurement(run_root, measurement_id)

    @staticmethod
    def directory_for(run_root: Path, measurement_id: str) -> Path:
        return Path(run_root) / MEASUREMENTS_DIR / measurement_id

    @property
    def path(self) -> Path:
        return self.directory_for(self.run_root, self.measurement_id)

    @property
    def max_repeat(self) -> int:
        """The largest scheduled repeat: R, or a `case_repeats` count (design §5)."""

        return self._max_repeat

    @property
    def scheduled_cases(self) -> tuple[EvalCase, ...]:
        """The corpus cases this measurement's identity schedules, in corpus order.

        The whole corpus for a full-corpus measurement; the selected cases of a
        subset measurement (G2 spec §3.1). `stage_plans` over these cases plans
        exactly the identity's schedule.
        """

        scheduled = {str(row["case_id"]) for row in self._schedule}
        return tuple(case for case in self.corpus if case.case_id in scheduled)

    def create(self, imported: Sequence[Mapping[str, Any]] = ()) -> None:
        """Create the declared journal; called only after `measurement_bound` is durable."""

        os.makedirs(self.path.parent, mode=0o700, exist_ok=True)
        self._journal = Journal.create(
            self.path,
            self.measurement_id,
            self.identity,
            self.corpus,
            secrets=self.secrets,
            imported=imported,
        )

    async def resume(
        self, adapter: TargetAdapter[Any], *, admitted_versions: Collection[str] = ()
    ) -> tuple[str, ...]:
        """Reopen a born journal, releasing whatever instances it still owns.

        Close interrupted lifecycles before recovering owned Target instances.
        A release is a new external operation and the lifecycle refuses it
        while a prior call is pending. Returns the released instance ids.

        A release the target refuses is a leak, not a failed resume (spec
        section 7: it "is reported in the status projection and does not block
        the terminal"), so the refusal is caught, the refused ids are recorded
        in `leaked`, and recovery continues. S1 will not leave recovery while
        it still owns an instance, so `finish_recovery` is attempted and its
        refusal accepted: the executor sees the leak and rules on it.
        """

        journal = Journal.resume(
            self.path,
            self.measurement_id,
            self.identity,
            self.corpus,
            secrets=self.secrets,
            admitted_versions=admitted_versions,
        )
        self._journal = journal
        self._instances.clear()
        self._live.clear()
        owned = self._owned_instances(journal)
        close_interrupted_operations(journal)
        try:
            released = await recover_target_instances(
                adapter, self._instance_records(journal), JournalRolloutAdapter(journal)
            )
        except RolloutInfrastructureError:
            still_owned = set(self._owned_instances(journal))
            released = tuple(name for name in owned if name not in still_owned)
            for name in owned:
                if name in still_owned:
                    self._leaked[name] = None
        if journal.recovery_required:
            with suppress(JournalRecoveryRequired):
                journal.finish_recovery()
        return released

    @staticmethod
    def classify(run_root: Path, measurement_id: str) -> Birth:
        """Classify a declared sibling without taking its lease (spec section 4 step 3)."""

        directory = Measurement.directory_for(run_root, measurement_id)
        if not directory.is_dir():
            return "absent"
        if not (directory / EVENTS_FILE).exists():
            return "unborn"
        snapshot = inspect_log(
            directory,
            file_name=EVENTS_FILE,
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            state_factory=Lifecycle,
            envelope_family=S1_FAMILY,
        )
        return "born" if snapshot.records else "unborn"

    @staticmethod
    def remove_unborn(run_root: Path, measurement_id: str) -> None:
        """Remove a directory that holds nothing durable; refuse a born journal."""

        birth = Measurement.classify(run_root, measurement_id)
        if birth == "born":
            raise MeasurementError("measurement_born")
        if birth == "absent":
            return
        directory = Measurement.directory_for(run_root, measurement_id)
        with suppress(FileNotFoundError):
            (directory / PARTIAL_FILE).unlink()
        shutil.rmtree(directory)

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    # -- staged dispatch ---------------------------------------------------

    @property
    def instances(self) -> Mapping[str, TargetIdentity]:
        """Every *live* instance's verified `TargetIdentity`, by instance id."""

        return MappingProxyType(self._instances)

    @property
    def attested(self) -> Mapping[str, TargetIdentity]:
        """Every instance this measurement ever attested; release never clears it."""

        return MappingProxyType(self._attested)

    @property
    def attested_description(self) -> Mapping[str, Mapping[str, Any] | None]:
        """What each attested instance described itself as; release never clears it."""

        return MappingProxyType(self._attested_description)

    @property
    def attested_components_digest(self) -> Mapping[str, str | None]:
        """Each attested instance's declared component-map digest; release never clears it."""

        return MappingProxyType(self._components_digest)

    @property
    def leaked(self) -> tuple[str, ...]:
        """Instance ids the target refused to release, in the order they were refused."""

        return tuple(self._leaked)

    def refuse_rows(
        self, plan: StagePlan, *, connection: ConnectionAdmission | None = None
    ) -> tuple[str, ...]:
        """Give every not-yet-terminal row of an opened stage a target terminal.

        ADR-0008 requires an opened stage to run to completion, and a target that
        refuses this measurement's Candidate on resume leaves no other way to
        complete it: the rows cannot be dispatched, and inventing a C1-only
        outcome for them would put a fact in the journal S1 does not own. So each
        remaining row gets S1's own pair, `rollout_started` then
        `rollout_finished(target_failed)`, written through S1's own writer
        (`JournalRolloutAdapter`) with a fresh operation id. `target_failed` is
        the registered outcome whose disposition is `indeterminate` owned by
        `target` (`SAMPLE_REASON_REGISTRY_V1`, reason `target_unstable`), and S1
        carries it on the `rollout_finished` terminal — `rollout_failed` admits
        only the environment, infrastructure and time-budget outcomes. That is
        exactly what happened, and what the executor then accounts.

        Only a row with no execution at all is closed, as its try `0`: a row that
        already holds a try keeps it, and whether that try is retried is the
        dispatch loop's decision, never this refusal's. A pair carried in by
        `sample_imported` is already scored and admits no try, so it is left alone.

        Family 14 (R59): `connection` is the dispatch's admission token. Each
        synthetic start names its binding and never claims the admission
        (`admit=False`), so it carries `connection_admission: null`.

        Returns the sample ids it closed, in plan order.
        """

        journal = self._require_journal()
        first_try = None if self.identity.sample_retries is None else 0
        closed: list[str] = []
        for row in plan.rows:
            if self._carried(row) or journal.sample_tries(row["case_id"], row["sample_id"]):
                continue
            writer = JournalRolloutAdapter(journal, first_try, connection=connection, admit=False)
            payload = {
                "operation_id": uuid4().hex,
                "case_id": row["case_id"],
                "sample_id": row["sample_id"],
                "candidate_digest": self.candidate_digest,
            }
            writer.append({"event": "rollout_started", **payload})
            writer.close_execution("rollout_finished", "target_failed")
            closed.append(str(row["sample_id"]))
        return tuple(closed)

    async def materialize(
        self,
        adapter: TargetAdapter[ArtifactRef],
        candidate: ArtifactRef,
        strategy: MaterializationStrategy,
        policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    ) -> None:
        """Materialize and attest this measurement's instances, at most once.

        Public and separate from `run_stage` because ADR-0008 requires every
        opened stage to run to completion: C1 calls this *before* it makes
        `stage_opened` durable, so an `AttestationError` refuses the target
        while no stage is open. A measurement that already holds live instances
        — a later stage of the same measurement, or a stage this session already
        materialized for — returns without touching the target, so `run_stage`
        can call it unconditionally and a caller that never splits the two keeps
        the behaviour it had.
        """

        self._require_journal()
        if self._instances:
            return
        await self._materialize(adapter, candidate, strategy, policy)

    async def prepare_stage(
        self,
        plan: StagePlan,
        *,
        adapter: TargetAdapter[ArtifactRef],
        candidate: ArtifactRef,
        strategy: MaterializationStrategy,
        environment: Environment,
        scorer: Scorer,
        policy: CampaignPolicyV11 | _config.EvaluationPolicy,
        expected: TargetIdentity | SemanticIdentity,
        cancelled: Callable[[], bool],
        repair_rows: frozenset[tuple[str, str]] = frozenset(),
        restored_rows: frozenset[tuple[str, str]] = frozenset(),
        permits: asyncio.Semaphore,
        scoring_permits: asyncio.Semaphore,
        connection: ConnectionAdmission | None = None,
    ) -> StageDispatch:
        """Materialize for one stage and return its rows, each ready to start.

        The first half of `run_stage`: nothing is dispatched here. Each row's
        `start` is its `_run_row` coroutine under the caller's `permits` and
        `scoring_permits`, so a caller that interleaves several measurements'
        stages (`run_interleaved`, G2 spec §4.6) bounds them all by one
        concurrency.

        A row is tried again, within the stage and under the same permits, while
        its latest try is retryable and the identity admits a further ordinal
        (spec §3.4); `cancelled` stops a new try from starting, never a running
        one.

        A pair carried in by `sample_imported` is already scored and is never
        dispatched: only the rows the measurement lacks are returned, and a stage
        every row of which was carried materializes nothing.

        `repair_rows` names the `(case_id, sample_id)` rows a family-7 fault-row
        suspension still owes a repair try (I1 spec §5.4): each gets exactly one,
        marked `repair: true`, and nothing after it.

        `restored_rows` names the rows whose latest try is a closed `cancelled`
        one that the campaign restored (#194): each starts one D1 retry (no
        `repair` key) while the ordinal limit allows, and its later tries follow
        the ordinary D1 rules. `cancelled` set before dispatch starts nothing.

        `connection` is the family-14 admission token (R59): every row's starts
        name its binding, and the first start of the binding claims its one
        admission. `None` (families up to 13) changes nothing.
        """

        self._require_journal()
        dispatched = tuple(row for row in plan.rows if not self._carried(row))
        if dispatched:
            await self.materialize(adapter, candidate, strategy, policy)
        cases = {case.case_id: case for case in self.corpus}
        # Every field of the policy's own budget, `cancelled` apart: the
        # tolerance a `/7` manifest declares governs the turn loop, and a
        # budget rebuilt from `max_turns` and `timeout_seconds` alone would
        # silently drop it back to the first-failure default (W19, ruling E).
        # `call_retries` (I1, `/8`) is carried the same way -- `None` for an
        # earlier manifest, which is today's classification (Task 3 consumes
        # it; nothing here changes runtime retry behaviour yet).
        budget = RolloutBudget(
            policy.budget.max_turns,
            policy.budget.timeout_seconds,
            max_failed_turns=policy.budget.max_failed_turns,
            call_retries=policy.budget.call_retries,
            cancelled=cancelled,
        )
        view = candidate.candidate_view()
        # One slot per admitted session: the pool enforces the adapter's
        # per-instance `max_concurrent_sessions` whatever the sample durations
        # are, which a static round-robin over uneven samples does not.
        sessions: asyncio.Queue[str] = asyncio.Queue()
        for _ in range(adapter.capabilities.max_concurrent_sessions):
            for instance_id in self._instances:
                sessions.put_nowait(instance_id)

        def start(row: Mapping[str, Any]) -> Awaitable[None]:
            return self._run_row(
                row=row,
                case=cases[row["case_id"]],
                sessions=sessions,
                candidate=view,
                environment=environment,
                scorer=scorer,
                budget=budget,
                expected=expected,
                permits=permits,
                scoring_permits=scoring_permits,
                cancelled=cancelled,
                repair=(str(row["case_id"]), str(row["sample_id"])) in repair_rows,
                restored=(str(row["case_id"]), str(row["sample_id"])) in restored_rows,
                connection=connection,
            )

        return StageDispatch(self, plan, dispatched, start)

    async def settle_stage(
        self,
        dispatch: StageDispatch,
        results: Sequence[Any],
        *,
        adapter: TargetAdapter[ArtifactRef],
        policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    ) -> StageOutcome:
        """Read one dispatched stage back once every row of it is terminal.

        The second half of `run_stage`. `results` are the rows' gathered
        results (`return_exceptions=True`), in `dispatch.rows` order: an
        unclassified failure is raised here, once the whole stage is terminal.
        `StageOutcome.indeterminate` names the rows whose *latest* try left
        them without a score -- exhausted ones included -- once each, with that
        try's ordinal; `indeterminate_rows()` is the every-try view.
        """

        plan = dispatch.plan
        self._raise_unclassified(dispatch.rows, results)
        keys = {(row["case_id"], row["sample_id"]) for row in plan.rows}
        rows = [
            row for row in self.sample_rows(plan.horizon) if (row.case_id, row.sample_id) in keys
        ]
        leaked: tuple[str, ...] = ()
        if plan.look >= max(policy.looks):
            leaked = await self.release_all(adapter)
        return StageOutcome(
            stage=plan.stage,
            look=plan.look,
            horizon=plan.horizon,
            terminals=sum(1 for row in rows if row.disposition != "not_started"),
            indeterminate=self._unsettled_latest(rows),
            faults=tuple(
                row
                for row in self.fault_rows(policy.metric_primary)
                if (row["case_id"], row["sample_id"]) in keys
            ),
            critical_failed=any(row.critical_pass is False for row in rows),
            leaked=leaked,
            attested=MappingProxyType(dict(self._attested)),
            attested_description=MappingProxyType(dict(self._attested_description)),
            attested_components_digest=MappingProxyType(dict(self._components_digest)),
        )

    async def run_stage(
        self,
        plan: StagePlan,
        *,
        adapter: TargetAdapter[ArtifactRef],
        candidate: ArtifactRef,
        strategy: MaterializationStrategy,
        environment: Environment,
        scorer: Scorer,
        policy: CampaignPolicyV11 | _config.EvaluationPolicy,
        expected: TargetIdentity | SemanticIdentity,
        cancelled: Callable[[], bool],
        repair_rows: frozenset[tuple[str, str]] = frozenset(),
        restored_rows: frozenset[tuple[str, str]] = frozenset(),
        connection: ConnectionAdmission | None = None,
    ) -> StageOutcome:
        """Run every row of one stage to a settled terminal; never cancel a sibling.

        The single-measurement composition of `prepare_stage` and
        `settle_stage`, under this stage's own `max_concurrency` and
        `max_scorer_concurrency` permits: one task per dispatched row, in plan
        order, gathered whole. See the two halves for the rules.
        """

        dispatch = await self.prepare_stage(
            plan,
            adapter=adapter,
            candidate=candidate,
            strategy=strategy,
            environment=environment,
            scorer=scorer,
            policy=policy,
            expected=expected,
            cancelled=cancelled,
            repair_rows=repair_rows,
            restored_rows=restored_rows,
            permits=asyncio.Semaphore(policy.max_concurrency),
            scoring_permits=asyncio.Semaphore(policy.max_scorer_concurrency),
            connection=connection,
        )
        tasks = [asyncio.ensure_future(dispatch.start(row)) for row in dispatch.rows]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return await self.settle_stage(dispatch, results, adapter=adapter, policy=policy)

    def _raise_unclassified(
        self, rows: Sequence[Mapping[str, Any]], results: Sequence[Any]
    ) -> None:
        """Surface every failure R1 and S1 did not turn into a durable terminal.

        A classified sample failure is that sample's recorded outcome and never
        leaves the stage (spec section 6, "No mid-stage cancellation"). A
        `JournalAdmissionRefused` is classified *when the row has a durable
        record*: `durable_rollout` closes the execution `rollout_interrupted`
        before re-raising it. A refusal that arrived before `rollout_started` was
        durable closed nothing -- there is no started operation to close -- so
        that row has no S1 terminal and the stage cannot complete. ADR-0008
        admits exactly one *kind* of exception to completeness -- a persistence
        terminal, behind which the campaign draws no consequence at all -- and
        this is one: the measurement journal reached its admission bound, which
        spec section 7 names the persistence terminal `journal_limit_exceeded`
        (ADR-0005: no restored capacity repairs it). It is raised here like any boundary error, once
        every sibling has reached its own terminal, so the campaign reports the
        fault instead of drawing consequences from a short stage. Everything
        else -- a `JournalFault`, a `JournalRecoveryRequired`, an
        `IdentityError`, a `CancelledError`, a bug -- recorded nothing either and
        is raised the same way. Boundary errors are raised ahead of the rest: a
        broken journal explains every later failure.
        """

        def unclassified(value: Any) -> bool:
            if not isinstance(value, BaseException) or isinstance(value, JournalAdmissionRefused):
                return False
            return isinstance(value, JournalBoundaryError) or not isinstance(value, _CLASSIFIED)

        offenders = [value for value in results if unclassified(value)]
        for offender in offenders:
            if isinstance(offender, JournalBoundaryError):
                raise offender
        journal = self._require_journal()
        for row, value in zip(rows, results, strict=True):
            if not isinstance(value, JournalAdmissionRefused):
                continue
            execution, _scoring = journal.sample_records(row["case_id"], row["sample_id"])
            if execution is None:
                raise JournalFault("journal_limit_exceeded") from value
        if offenders:
            raise offenders[0]

    async def release_all(self, adapter: TargetAdapter[Any]) -> tuple[str, ...]:
        """Release every live instance; return the ids the target refused (leaked).

        The journaled path is `release_target`, whose first act is to append
        `target_release_started`. A measurement journal a boundary fault has
        poisoned refuses that append and would raise before the adapter is ever
        called, leaving a live instance behind a persistence terminal — which
        C1's §7 barrier must never do. So an unusable journal is asked about
        first and the release goes straight to the adapter, unjournaled; a fault
        raised *during* a journaled release falls through to the same call,
        because a release the journal could not finish recording is exactly the
        case R1's own `recover_target_instances` re-releases by id after a crash.
        A target that refuses is `leaked` either way.

        The bypass is for an unusable journal, not a way to lose a fault: a
        boundary error raised while the journal was still healthy is remembered,
        every instance is still given back, and it is re-raised once they are.
        Its caller classifies it — at a measurement's last look that is
        `run_stage`'s own measurement boundary, and so the `persistence_failed`
        terminal the operator has to see; at the §7 barrier the terminal already
        exists and the executor keeps the first fault.
        """

        if not self._instances:
            return ()
        journal = self._require_journal()
        assert self._materialization is not None
        candidate_digest, strategy = self._materialization
        leaked: list[str] = []
        fault: JournalBoundaryError | None = None
        for instance_id in list(self._instances):
            if journal.healthy:
                try:
                    await release_target(
                        adapter,
                        instance_id,
                        candidate_digest,
                        strategy,
                        JournalRolloutAdapter(journal),
                        attempts=self._call_attempts,
                    )
                    continue
                except RolloutInfrastructureError:
                    leaked.append(instance_id)
                    self._leaked[instance_id] = None
                    continue
                except JournalBoundaryError as exc:
                    fault = fault or exc
            if await self._release_unjournaled(adapter, instance_id):
                self._unjournaled[instance_id] = None
            else:
                leaked.append(instance_id)
                self._leaked[instance_id] = None
        # Only the live handles go: `_attested` is what the executor's
        # `instance_attested` records are written from, and it outlives release.
        self._instances.clear()
        self._live.clear()
        if fault is not None:
            raise fault
        return tuple(leaked)

    async def _release_unjournaled(self, adapter: TargetAdapter[Any], instance_id: str) -> bool:
        """Give one instance back through the adapter alone; True if the target let go.

        The same call `release_target` makes after its journal write, classified
        the same way: an interrupt travels, and every other failure is the
        target refusing, which the caller records as a leak.
        """

        try:
            await adapter.release(instance_id)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return False
        return True

    @property
    def released_unjournaled(self) -> tuple[str, ...]:
        """Instance ids given back through the adapter with no durable record.

        The journal still says the measurement owns them, so a later resume
        releases them again by id (R1's `recover_target_instances`) exactly as it
        would after a crash between the release and its record.
        """

        return tuple(self._unjournaled)

    # -- accounting --------------------------------------------------------

    def sample_rows(self, look: int) -> tuple[SampleRow, ...]:
        """One row per scheduled `(case, repeat <= look)`, not-started ones included.

        A row's `disposition` is its R2a disposition (`score | indeterminate |
        exclude`) once the sample has a durable evaluation, and otherwise one of
        two states this layer adds: `not_started` for a sample with no execution
        terminal at all, and `unscored` for one whose execution terminal is
        scoreable but whose scoring has no durable terminal yet (the sample
        S1 cannot re-score after a crash, since it never replays rollout
        inputs). `StageOutcome.terminals` counts an `unscored` row -- its
        execution *is* terminal -- while `complete()` does not.

        `effective_seed` is `None` on a row without a `rollout_finished` even
        under `exact`: the confirmed seed is a fact the session reported, and a
        sample that never opened one confirmed nothing.

        A row is the sample's latest try, carrying its `retry_ordinal` and
        whether it is `exhausted`; earlier tries are `indeterminate_rows()`'s.
        """

        journal = self._require_journal()
        rows = []
        for entry in self._schedule:
            if int(entry["repeat"]) > look:
                continue
            execution, scoring = journal.sample_records(entry["case_id"], entry["sample_id"])
            ordinal = 0 if execution is None else self._ordinal_of(execution)
            imported = journal.imported_record(entry["case_id"], entry["sample_id"])
            rows.append(self._row(entry, execution, scoring, ordinal, imported))
        return tuple(rows)

    def unfilled_slots(
        self,
        look: int,
        reserved: Collection[tuple[str, str, int]],
        declined: Collection[tuple[str, str, int]] = (),
    ) -> int:
        """`unfilled_slots` over the open journal as it stands now (#109, a28 spec §3.3)."""

        return unfilled_slots(self._schedule, self._require_journal(), look, reserved, declined)

    def records(self) -> tuple[Envelope, ...]:
        """The open journal's durable prefix, for a read-only projection.

        `sample_rows` reduces the two records a row needs; the brief's
        observation projection (sprint design §4.2) also needs the sample's
        `scoring_started` — the record that names the scorer trace — which the
        lifecycle index replaces once `scoring_finished` lands.
        """

        return self._require_journal().snapshot().records

    def indeterminate_rows(self) -> tuple[dict[str, Any], ...]:
        """Every indeterminate **try**, with its ordinal, R2a owner, reason and reference.

        One dict per try, in schedule order and then try order: an indeterminate
        execution terminal, a scoring terminal without a score
        (`scorer_failed`, `interrupted`), and a scoreable execution that was
        never scored. The last is what resume classifies as
        `indeterminate(rollout_interrupted)` (spec §3.4: it consumes a try), so it
        carries that owner and reason here, whether it is the latest try or one
        a later try already followed.
        """

        journal = self._require_journal()
        records = journal.snapshot().records
        rows = []
        for entry in self._schedule:
            for execution, scoring in journal.sample_tries(entry["case_id"], entry["sample_id"]):
                ordinal = self._ordinal_of(execution)
                row = self._row(entry, execution, scoring, ordinal)
                owner_reason = self._owner_reason(row)
                if owner_reason is None:
                    continue
                rows.append(
                    dict(
                        case_id=row.case_id,
                        sample_id=row.sample_id,
                        retry_ordinal=ordinal,
                        owner=owner_reason[0],
                        reason=owner_reason[1],
                        reference=self._reference(self._terminal_of(execution, scoring), records),
                    )
                )
        return tuple(rows)

    def try_count(self, case_id: str, sample_id: str) -> int:
        """How many tries of one sample the journal holds, an open one included.

        Accounting reads it to say whether a further try followed the one it is
        recording: a try the journal already carries a successor for reserves
        that successor's terminal in the denominator whatever the caller's
        cancellation token says (spec §3.5, ADR-0008).
        """

        return len(self._require_journal().sample_tries(case_id, sample_id))

    def settled(self) -> bool:
        """Every scheduled sample scored or exhausted its tries (spec §3.4)."""

        return all(
            row.disposition == "score" or row.exhausted
            for row in self.sample_rows(self._max_repeat)
        )

    def exhausted_rows(self, look: int | None = None) -> tuple[SampleRow, ...]:
        """The rows up to `look` (every look when `None`) whose tries are exhausted."""

        return tuple(
            row
            for row in self.sample_rows(self._max_repeat if look is None else look)
            if row.exhausted
        )

    def fault_rows(self, primary: str) -> tuple[dict[str, Any], ...]:
        """Campaign-level fault samples: `infrastructure_failed`, a missing primary and,
        where `primary_range` covers the row, one outside [0, 1] (#97 spec §4.1).

        `exclude` with reason `rollout_cancelled` is the caller's token, never a
        fault (spec section 7), so only `infrastructure_failed` is named here.

        Each row also carries its try's `retry_ordinal` and the `code` of the
        call failure that ended it -- the last `error_code` that try journaled
        (I1 spec §3.4), `None` when it journaled none -- which a family-7
        suspension lists and classifies (spec §5.1-§5.2).
        """

        rows = []
        codes: dict[tuple[str, str, int], str] | None = None
        for row in self.sample_rows(self._max_repeat):
            reason = row_fault(row, primary, primary_range=self.primary_range)
            if reason is not None:
                code = None
                if reason == "infrastructure_failed":
                    if codes is None:
                        codes = self._error_codes()
                    code = codes.get((row.case_id, row.sample_id, row.retry_ordinal))
                rows.append(
                    dict(
                        case_id=row.case_id,
                        sample_id=row.sample_id,
                        reason=reason,
                        reference=self.record_reference(row.case_id, row.sample_id),
                        retry_ordinal=row.retry_ordinal,
                        code=code,
                    )
                )
        return tuple(rows)

    def repair_count(self, case_id: str, sample_id: str) -> int:
        """How many repair tries (I1 spec §5.4) one sample started."""

        return self._require_journal().sample_repairs(case_id, sample_id)

    def owed_repairs(
        self, evidence: Mapping[str, Any], primary: str, primary_range: PrimaryRange
    ) -> frozenset[tuple[str, str]]:
        """The rows a `fault_rows` suspension named here that still owe a repair try.

        `campaign_statistics.owed_repair_rows` over this live journal: the
        listed rows' latest ordinals come from the lifecycle index, and the
        prefix is read only when the evidence was truncated. `primary_range`
        is the rule in force at that suspension (codex branch review round 1,
        as the audit reads it since ruling R5), not this measurement's current
        one: a truncated named set is rebuilt as the suspension counted it.
        """

        journal = self._require_journal()

        def latest(key: tuple[str, str]) -> int | None:
            tries = journal.sample_tries(*key)
            return self._ordinal_of(tries[-1][0]) if tries else None

        return owed_repair_rows(
            evidence,
            self.measurement_id,
            primary,
            schedule=self._schedule,
            latest_ordinal=latest,
            records=lambda: journal.snapshot().records,
            primary_range=primary_range,
        )

    def owed_threshold_repairs(
        self, named: Mapping[tuple[str, str], tuple[int, int]]
    ) -> frozenset[tuple[str, str]]:
        """#78: `owed_threshold_rows` over this live journal's latest ordinals."""

        journal = self._require_journal()

        def latest(key: tuple[str, str]) -> int | None:
            tries = journal.sample_tries(*key)
            return self._ordinal_of(tries[-1][0]) if tries else None

        return owed_threshold_rows(named, latest)

    def _error_codes(self) -> dict[tuple[str, str, int], str]:
        """`(case_id, sample_id, retry_ordinal) -> ` the try's last journaled `error_code`."""

        codes: dict[tuple[str, str, int], str] = {}
        for record in self._require_journal().snapshot().records:
            payload = record.payload
            code = payload.get("error_code")
            if isinstance(code, str) and "case_id" in payload and "sample_id" in payload:
                key = (
                    str(payload["case_id"]),
                    str(payload["sample_id"]),
                    int(payload.get("retry_ordinal", 0)),
                )
                codes[key] = code
        return codes

    def complete(self) -> bool:
        """Spec section 6's baseline barrier: every scheduled sample scored."""

        return all(row.disposition == "score" for row in self.sample_rows(self._max_repeat))

    def status(self) -> Mapping[str, Any]:
        """The S1 status projection of this measurement's durable prefix."""

        journal = self._require_journal()
        return project_status(journal.snapshot(), secrets=self.secrets).to_payload()

    def prefix_reference(self) -> _durable_reference.DurableReference:
        """A `journal_prefix` over the whole durable prefix as it stands now."""

        snapshot = self._require_journal().snapshot()
        return _durable_reference.DurableReference(
            version=1,
            kind="journal_prefix",
            run_id=self.measurement_id,
            locator=self._locator(0, snapshot.valid_bytes),
            content_digest=snapshot.prefix_digest,
        )

    def record_reference(
        self, case_id: str, sample_id: str, retry_ordinal: int | None = None
    ) -> _durable_reference.DurableReference:
        """A `journal_record` over one try's terminal record bytes; `None` is the latest try.

        The terminal is the try's scoring terminal when scoring reached one, and
        its execution terminal otherwise. A record without `retry_ordinal` is
        try `0`. A pair carried in by `sample_imported` has no try: its terminal
        is that record, for the latest try and for ordinal `0` alike.
        """

        journal = self._require_journal()
        carried = journal.imported_record(case_id, sample_id)
        if carried is not None and retry_ordinal in (None, 0):
            return self._reference(carried, journal.snapshot().records)
        tries = journal.sample_tries(case_id, sample_id)
        if retry_ordinal is not None:
            tries = tuple(pair for pair in tries if self._ordinal_of(pair[0]) == retry_ordinal)
        if not tries:
            raise ValueError("sample has no terminal record")
        execution, scoring = tries[-1]
        return self._reference(self._terminal_of(execution, scoring), journal.snapshot().records)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _ordinal_of(execution: Envelope) -> int:
        return int(execution.payload.get("retry_ordinal", 0))

    @staticmethod
    def _terminal_of(execution: Envelope, scoring: Envelope | None) -> Envelope:
        target = (
            scoring if scoring is not None and scoring.event != "scoring_started" else execution
        )
        if target.event.endswith("_started"):
            raise ValueError("sample has no terminal record")
        return target

    def _owner_reason(self, row: SampleRow) -> tuple[str, str] | None:
        """The accounted owner and reason of a try that left its sample unscored."""

        if row.disposition == "unscored":
            return ("rollout", "rollout_interrupted")
        if row.disposition != "indeterminate":
            return None
        disposition = self._disposition_of(row.outcome)
        return (str(disposition.owner), str(disposition.reason))

    def _unsettled_latest(self, rows: Sequence[SampleRow]) -> tuple[dict[str, Any], ...]:
        """One dict per row whose latest try is indeterminate or unscored."""

        records = self._require_journal().snapshot().records
        found = []
        for row in rows:
            owner_reason = self._owner_reason(row)
            if owner_reason is None:
                continue
            found.append(
                dict(
                    case_id=row.case_id,
                    sample_id=row.sample_id,
                    retry_ordinal=row.retry_ordinal,
                    owner=owner_reason[0],
                    reason=owner_reason[1],
                    reference=self._reference_of(row.case_id, row.sample_id, records),
                )
            )
        return tuple(found)

    def _reference_of(
        self, case_id: str, sample_id: str, records: Sequence[Envelope]
    ) -> _durable_reference.DurableReference:
        execution, scoring = self._require_journal().sample_records(case_id, sample_id)
        if execution is None:
            raise ValueError("sample has no terminal record")
        return self._reference(self._terminal_of(execution, scoring), records)

    def _reference(
        self, target: Envelope, records: Sequence[Envelope]
    ) -> _durable_reference.DurableReference:
        offset = 0
        for record in records:
            encoded = record.encode()
            if record == target:
                return _durable_reference.DurableReference(
                    version=1,
                    kind="journal_record",
                    run_id=self.measurement_id,
                    locator=self._locator(offset, offset + len(encoded)),
                    content_digest=hashlib.sha256(encoded).hexdigest(),
                )
            offset += len(encoded)
        raise ValueError("terminal record is not in the durable prefix")

    def _locator(self, start: int, end: int) -> str:
        return f"{MEASUREMENTS_DIR}/{self.measurement_id}/{EVENTS_FILE}#bytes={start}:{end}"

    def _require_journal(self) -> Journal:
        if self._journal is None:
            raise ValueError("measurement journal is not open")
        return self._journal

    @staticmethod
    def _disposition_of(outcome: str | None) -> SampleDisposition:
        return disposition_for(cast(SampleOutcome, outcome))

    def _boot_id(self, key: tuple[str, str]) -> str:
        """The boot id the sample's instance attested, or `""` for another process's."""

        identity = self._attested.get(self._sample_instances.get(key, ""))
        return identity.instance.boot_id if identity is not None else ""

    def _owned_instances(self, journal: Journal) -> tuple[str, ...]:
        """Instance ids the journal still owns: a materialize start, no release finish."""

        return tuple(
            owned.instance_id for owned in recovery_plan(journal.snapshot().records).cleanup
        )

    def _instance_records(self, journal: Journal) -> tuple[Mapping[str, Any], ...]:
        """The instance-lifecycle records in R1's flat `{event, ...payload}` shape."""

        return tuple(
            {"event": record.event, **record.payload}
            for record in journal.snapshot().records
            if record.event.startswith(("target_materialize_", "target_release_"))
        )

    def _row(
        self,
        entry: Mapping[str, Any],
        execution: Envelope | None,
        scoring: Envelope | None,
        retry_ordinal: int,
        imported: Envelope | None = None,
    ) -> SampleRow:
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        return sample_row(
            entry,
            execution,
            scoring,
            seed_mode=self._seed_mode,
            dependency_digest=self._dependency_digest,
            boot_id=self._boot_id(key),
            retry_ordinal=retry_ordinal,
            limit=self.identity.sample_retries or 0,
            imported=imported,
            repaired=self._require_journal().sample_repairs(*key) > 0,
        )

    def _carried(self, row: Mapping[str, Any]) -> bool:
        """Was this plan row carried in by `sample_imported` (never dispatched)?"""

        journal = self._require_journal()
        return journal.imported_record(str(row["case_id"]), str(row["sample_id"])) is not None

    async def _materialize(
        self,
        adapter: TargetAdapter[ArtifactRef],
        candidate: ArtifactRef,
        strategy: MaterializationStrategy,
        policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    ) -> None:
        journal = self._require_journal()
        self._materialization = (candidate.digest, strategy)
        self.refusal_prefix = None
        # I1: materialize and release are target calls like any other, attempted
        # up to `call_retries.max_attempts` times; `1` before `/8`.
        retries = policy.budget.call_retries
        self._call_attempts = None if retries is None else retries.max_attempts
        for _ in range(instances_per_measurement(policy, adapter.capabilities)):
            instance_id = new_instance_id(self.campaign_run_id)
            # R1's `CandidateReference.digest` is a settable protocol member, so a
            # frozen `ArtifactRef` -- the very reference S2 publishes for
            # materialization -- is not a structural match for it. The narrow
            # ignore keeps this signature honest instead of widening it to `Any`.
            try:
                instance = await materialize_target(
                    adapter,  # type: ignore[type-var]
                    candidate,
                    strategy,
                    instance_id,
                    JournalRolloutAdapter(journal),
                    attempts=self._call_attempts,
                )
            except AttestationError:
                # The adapter refused inside `materialize` itself (the HTTP
                # adapter's `attestation_mismatch`, `strategy_unsupported`):
                # nothing to release, and R1 has journaled the failure as
                # `target_materialize_failed`, so that record is the boundary.
                self.refusal_prefix = self.prefix_reference().to_payload()
                raise
            try:
                identity = await instance.identity()
                verify_attestation(identity, candidate.expected_attestation())
                if self.description_check is not None:
                    self.description_check(instance_id, getattr(instance, "description", None))
                digest = self._declare_components(journal, instance)
                # G2 spec §4.6: the map is durable first, so the audit reads
                # what the refused instance rendered. An instance that declares
                # no map is not compared: drift is a declared, different map.
                check = self.components_check
                if check is not None and digest is not None and digest != check.reference:
                    if check.suspends or check.reference in journal.declared_components():
                        raise ComponentsChanged(self.measurement_id, check.reference, digest)
            except BaseException as error:
                with suppress(RolloutInfrastructureError):
                    await release_target(
                        adapter,
                        instance_id,
                        candidate.digest,
                        strategy,
                        JournalRolloutAdapter(journal),
                        attempts=self._call_attempts,
                    )
                if isinstance(error, AttestationError):
                    # a27 spec §4.1: the refusal's boundary is this release.
                    self.refusal_prefix = self.prefix_reference().to_payload()
                raise
            self._instances[instance_id] = identity
            self._live[instance_id] = instance
            self._attested[instance_id] = identity
            # Read off the handle the adapter returned, not off `identity()`:
            # the creation body is the durable value (PDR-0014). `getattr`
            # because `description` is not a member of `TargetInstance` at all
            # -- it lives on the sibling `DescribingTargetInstance`
            # (rollout.py) -- so an adapter that declares none is still a
            # `TargetInstance`.
            self._attested_description[instance_id] = getattr(instance, "description", None)
            self._components_digest[instance_id] = digest

    def _declare_components(self, journal: Journal, instance: object) -> str | None:
        """Make the instance's component map durable before any row names it.

        G2 spec §4.2: written once per distinct map, after the instance
        attested and before it runs anything, in this measurement's own S1
        journal; a later row or attestation names it by digest only. Only a
        family-12 identity (`row_provenance`) carries maps, so families 5-11
        write nothing new. A map already declared -- a second instance, or a
        re-materialization on resume -- is not written again. Called inside
        `_materialize`'s release guard: a refused declaration releases the
        instance like a failed attestation does.
        """

        # `getattr`: `components` is not a `TargetInstance` member; the HTTP
        # adapter's handle carries the creation body's validated map.
        components = getattr(instance, "components", None)
        if not self.identity.row_provenance or components is None:
            return None
        digest = components_digest(components)
        if digest not in journal.declared_components():
            journal.append(
                "components_declared",
                {"components_digest": digest, "components": dict(components)},
            )
        return digest

    async def _run_row(
        self,
        *,
        row: Mapping[str, Any],
        case: EvalCase,
        sessions: asyncio.Queue[str],
        candidate: Any,
        environment: Environment,
        scorer: Scorer,
        budget: RolloutBudget,
        expected: TargetIdentity | SemanticIdentity,
        permits: asyncio.Semaphore,
        scoring_permits: asyncio.Semaphore,
        cancelled: Callable[[], bool],
        repair: bool = False,
        restored: bool = False,
        connection: ConnectionAdmission | None = None,
    ) -> None:
        """Try one row until its latest try scored, cannot be retried, or is the last.

        A `repair` row (I1 spec §5.4) whose latest try is a closed fault gets
        exactly one repair try and stops, whatever it ends in; a sample that has
        had a repair try is never retried under `sample_retries` again. A
        `restored` row (#194) whose latest try is a closed `cancelled` one starts
        one D1 retry, once per dispatch.

        A durable execution record is never replayed: a closed try stands as that
        try's outcome and only a *further* try may follow it, and a
        started-but-unfinished one belongs to recovery, not to a second dispatch.
        A classified failure is the try's durable terminal, so the loop reads
        the journal again rather than the exception; one that left no new try
        behind would loop forever, so it travels instead.
        """

        journal = self._require_journal()
        sample_id = str(row["sample_id"])
        if journal.imported_record(case.case_id, sample_id) is not None:
            # A carried pair is scored and admits no try (defence in depth:
            # `run_stage` never starts a task for one).
            return
        retries = self.identity.sample_retries
        while True:
            tries = journal.sample_tries(case.case_id, sample_id)
            retry_ordinal: int | None
            repairing = False
            if not tries:
                retry_ordinal = None if retries is None else 0
            else:
                execution, scoring = tries[-1]
                if execution.event == "rollout_started" or retries is None:
                    return
                ordinal = self._ordinal_of(execution)
                retryable = try_is_retryable(execution, scoring)
                # #194: a restored row's first try after a closed `cancelled`
                # one is a D1 retry; the grant is spent once, so later tries
                # follow the ordinary rules.
                resume = restored and execution.payload.get("outcome") == "cancelled"
                restored = False
                if repair:
                    # One repair per dispatch: the flag is spent whether or not
                    # this try starts, so the loop can never repair twice. A
                    # repair follows a try no D1 retry may follow: a fault row,
                    # or an indeterminate try whose retries are spent (#78).
                    repair = False
                    spent = ordinal >= retries or journal.sample_repairs(case.case_id, sample_id)
                    if (
                        retryable
                        and not spent
                        and self.identity.to_payload()["profile"] != "rollout_evaluation/1"
                    ) or cancelled():
                        return
                    repairing = True
                elif (
                    not (retryable or resume)
                    or ordinal >= retries
                    or journal.sample_repairs(case.case_id, sample_id)
                    or cancelled()
                ):
                    return
                retry_ordinal = ordinal + 1
            try:
                await self._run_try(
                    row=row,
                    case=case,
                    sessions=sessions,
                    candidate=candidate,
                    environment=environment,
                    scorer=scorer,
                    budget=budget,
                    expected=expected,
                    permits=permits,
                    scoring_permits=scoring_permits,
                    retry_ordinal=retry_ordinal,
                    repair=repairing,
                    connection=connection,
                )
            except JournalBoundaryError:
                raise
            except _CLASSIFIED:
                if len(journal.sample_tries(case.case_id, sample_id)) <= len(tries):
                    raise

    async def _run_try(
        self,
        *,
        row: Mapping[str, Any],
        case: EvalCase,
        sessions: asyncio.Queue[str],
        candidate: Any,
        environment: Environment,
        scorer: Scorer,
        budget: RolloutBudget,
        expected: TargetIdentity | SemanticIdentity,
        permits: asyncio.Semaphore,
        scoring_permits: asyncio.Semaphore,
        retry_ordinal: int | None,
        repair: bool = False,
        connection: ConnectionAdmission | None = None,
    ) -> None:
        journal = self._require_journal()
        sample_id = str(row["sample_id"])
        sample = RolloutSample(sample_id, self.candidate_digest, row["requested_seed"])
        # Both the concurrency permit and the instance's session slot are taken
        # before `durable_rollout`, which is where the try's deadline starts
        # (R2a rule 3), so neither queue costs the try any of its budget.
        async with permits:
            instance_id = await sessions.get()
            try:
                self._sample_instances[(case.case_id, sample_id)] = instance_id
                trajectory = await durable_rollout(
                    journal=journal,
                    identity=self.identity,
                    corpus=self.corpus,
                    case=case,
                    sample=sample,
                    instance=self._live[instance_id],
                    environment=environment,
                    scorer=scorer,
                    budget=budget,
                    expected_target=expected,
                    retry_ordinal=retry_ordinal,
                    repair=repair,
                    components_digest=self._components_digest.get(instance_id),
                    connection=connection,
                )
            finally:
                sessions.put_nowait(instance_id)
        if trajectory.disposition.kind != "score":
            return
        async with scoring_permits:
            await durable_score(
                journal, scorer, case, trajectory, candidate, retry_ordinal=retry_ordinal
            )
