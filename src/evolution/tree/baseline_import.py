"""The baseline admission rule and the source measurement reader (spec §3.2, §5.1).

A linked run whose manifest declares `baseline: inherit` carries its
predecessor's champion measurement forward: `read_source` opens that
measurement's journal read-only, and `admit_rows` reduces it against the
successor's own `RunIdentityV1` into the `sample_imported` payloads the
measurement journal's atomic birth writes (§5.2). Both are pure: neither
writes, holds a lease, or dispatches. §3.2's admission rule is exactly:

1. the target's schedule contains `(case_id, repeat)` with the source row's
   own `requested_seed` (ruling R8 keys on the schedule's `repeat`, not the
   sample id, which under `unpaired`/`none` carries the measurement side;
   the seed is compared literally, ruling R4): under `unpaired`/`none` both
   sides carry `None`, so this is `None == None`; a source row measured under
   `exact` (a real seed) is never admitted by an `unpaired`/`none` target,
   whose schedule entries always carry `None`. The admitted payload carries
   the target entry's own `sample_id`;
2. the source's latest try for that pair is scored: a `rollout_finished`
   whose terminal's registered disposition is `score` (`completed`,
   `turn_budget` or `candidate_failed`), with a `scoring_finished` (or itself a
   chain's `sample_imported`, which is always scored by construction);
3. its `metrics` hold the target policy's `metric.primary` -- and, where the
   admitting version owes the #97 range rule (`primary_range`, families 7-12
   from 0.3.0a26; ADR-0019 as amended 2026-10-03), its value lies in
   [0, 1]: a row the gate cannot read is measured afresh, never carried as a
   fault no repair can reach;
4. under the target's `seed_mode: exact` only, the row's own
   `dependency_digest` equals the target measurement's.

Nothing else bars a row (global constraint). A row that is itself a
`sample_imported` is admitted by the same rule and keeps its original
`source`, with `condition_fields` extended by the link's own
`condition_changed` (§3.4). Rows of a predecessor measurement bound before
the predecessor's last in-place change also name the fields changed since
(`SourceMeasurement.condition_fields`, I2 spec §6.3).

An in-place `continue` (I2 spec §5.3) reuses the same primitive inside one
run: `read_frozen_source` opens a frozen measurement of this run instead of a
predecessor's champion, and `admit_rows` runs over it unchanged.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.tree.campaign_measurement import MEASUREMENTS_DIR, Measurement, MeasurementError
from evolution.tree.campaign_projections import champion_measurement
from evolution.tree.campaign_schema import CampaignSnapshot
from evolution.tree.campaign_statistics import latest_rows
from evolution.tree.execution.authorizations import COMPARABLE_FIELDS
from evolution.tree.execution.journal import LEASE_FILE, inspect_journal
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalBoundaryError

__all__ = [
    "BaselineImport",
    "BaselineSourceUnavailable",
    "FrozenSourceCorrupt",
    "SourceMeasurement",
    "admit_rows",
    "carried_traces",
    "changed_since",
    "import_pairs_digest",
    "import_sealed",
    "read_frozen_source",
    "read_source",
    "record_digest",
]


class BaselineSourceUnavailable(Exception):
    """`baseline_source_unavailable` (§5.0): the predecessor's champion measurement cannot be read."""

    def __str__(self) -> str:
        return "baseline_source_unavailable"


class FrozenSourceCorrupt(Exception):
    """An in-run frozen source is not the measurement the campaign bound (I2 spec §5.3).

    Unlike `BaselineSourceUnavailable`, this is never an authorizable loss: the
    source is a measurement of the same run that nothing appends to again, so
    a journal that cannot be read, or one born under another identity, is
    corruption. `reason` is `unreadable` or `identity_digest_mismatch`, and for
    a sealed source (G2 spec §3.2) also `candidate_digest_mismatch` or
    `pairs_digest_mismatch`.
    """

    def __init__(self, measurement_id: str, reason: str) -> None:
        super().__init__(measurement_id, reason)
        self.measurement_id = measurement_id
        self.reason = reason

    def __str__(self) -> str:
        return f"frozen_source_corrupt: {self.measurement_id} ({self.reason})"


@dataclass(frozen=True)
class SourceMeasurement:
    """A born, read-only predecessor measurement journal, reduced enough to admit from.

    `run_id` is the campaign run that owns the measurement -- the predecessor's
    for `read_source` (ruling R2), this run's own for `read_frozen_source` (I2
    spec §5.3) -- the value every newly admitted row's `source.run_id` carries;
    it is not the measurement journal's own envelope `run_id`, which never
    leaves this module.

    `condition_fields` (I2 spec §6.3) are the fields the predecessor changed
    in place *after* this measurement's condition epoch: its rows were measured
    under a condition the predecessor no longer ended under, so every row
    admitted from it names them, beside the link's own `condition_changed`.
    Empty for a measurement of the predecessor's latest epoch, for every
    predecessor without an in-place change, and for `read_frozen_source`,
    whose carry names its own change.
    """

    run_id: str
    measurement_id: str
    identity_digest: str
    prefix_digest: str
    records: tuple[Envelope, ...]
    condition_fields: tuple[str, ...] = ()
    valid_bytes: int | None = None
    journal_integrity: str = "complete"


@dataclass(frozen=True)
class BaselineImport:
    """The admitted set for one successor's first-round baseline (spec §5.1).

    `rows` are complete `sample_imported` payloads in the target's schedule
    order -- exactly what the measurement's atomic birth writes verbatim. `fields`
    and `origin_runs` are derived from `rows`, never from the source at large,
    so a successor that admits only some origins reports only their history
    (§3.4). Ruling R1: an empty admission set still produces every field
    (`imported=0`, `rows=()`, `origin_runs=0`, `fields=()`, and `pairs_digest`
    the digest of the empty list) -- `declaration()` is never partial.
    """

    source_measurement_id: str
    source_prefix_digest: str
    pairs_digest: str
    imported: int
    scheduled: int
    fields: tuple[str, ...]
    origin_runs: int
    rows: tuple[Mapping[str, Any], ...]

    def declaration(self) -> dict[str, Any]:
        """The creation record's `identity.linked_from.baseline` (§5.1)."""

        return dict(
            mode="inherit",
            source_measurement_id=self.source_measurement_id,
            source_prefix_digest=self.source_prefix_digest,
            pairs_digest=self.pairs_digest,
            imported=self.imported,
            scheduled=self.scheduled,
            fields=list(self.fields),
            origin_runs=self.origin_runs,
        )


def record_digest(record: Envelope) -> str:
    """SHA-256 of one journal record's complete line, newline included.

    A carried row's `source.rollout_digest` and `source.scoring_digest` are this
    digest of the source try's `rollout_finished` and scoring terminal
    (`admit_rows`); `carried_traces` recomputes it to find that try again.
    """

    return hashlib.sha256(record.encode()).hexdigest()


def import_pairs_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the canonical list of complete `sample_imported` payloads (§5.1)."""

    return hashlib.sha256(canonical_json(list(rows))).hexdigest()


def read_source(predecessor_root: Path, snapshot: CampaignSnapshot) -> SourceMeasurement:
    """Open the predecessor's champion measurement read-only (§4.2, §5.0).

    Raises `BaselineSourceUnavailable` for every way it cannot be read: no
    champion measurement exists yet, the journal's directory or file is
    absent or unusable (`OSError`, wrapped or not, and `JournalBoundaryError`),
    it fails its own replay (`ValueError`, `SchemaError` is a `ValueError`),
    it holds no records, its `journal_integrity` is not `complete`, or its
    `run_created` is not the measurement the snapshot bound (identity and
    candidate digests of `measurement_bound`, the latter the snapshot
    champion's). A journal whose lease a process resuming the predecessor
    holds is unavailable too (§5.0): the lease is probed, never taken, and
    never waited on.
    """

    measurement_id, _ = champion_measurement(snapshot)
    if measurement_id is None:
        raise BaselineSourceUnavailable()
    directory = Path(predecessor_root) / MEASUREMENTS_DIR / measurement_id
    if _lease_held(directory):
        raise BaselineSourceUnavailable()
    try:
        journal_snapshot = inspect_journal(directory)
    except (OSError, JournalBoundaryError, ValueError):
        raise BaselineSourceUnavailable() from None
    if not journal_snapshot.records or journal_snapshot.journal_integrity != "complete":
        raise BaselineSourceUnavailable()
    # The file is the measurement only if it is the one the predecessor bound:
    # `measurement_bound` fixed its identity and candidate digests, and the
    # champion measurement measures the snapshot's champion. A journal that
    # replays `complete` but was put in its place is not that measurement.
    binding = snapshot.measurements.get(measurement_id)
    created = journal_snapshot.records[0].payload
    if (
        binding is None
        or created.get("identity_digest") != binding.get("identity_digest")
        or created.get("identity", {}).get("candidate_digest") != binding.get("candidate_digest")
        or binding.get("candidate_digest") != snapshot.champion[1]
    ):
        raise BaselineSourceUnavailable()
    identity_digest = created["identity_digest"]
    return SourceMeasurement(
        run_id=snapshot.run_id,
        measurement_id=measurement_id,
        identity_digest=str(identity_digest),
        prefix_digest=journal_snapshot.prefix_digest,
        records=journal_snapshot.records,
        condition_fields=changed_since(snapshot, measurement_id),
    )


def changed_since(snapshot: CampaignSnapshot, measurement_id: str) -> tuple[str, ...]:
    """The fields the campaign changed in place after `measurement_id`'s epoch (I2 §6.3).

    `condition_changes[k - 1]` opened epoch `k` (ADR-0020), so a measurement
    bound in epoch `e` was measured before every change from index `e` on.
    A snapshot without in-place changes (every family below 7, and a
    change-free family-7 run) names none.
    """

    changes = snapshot.condition_changes
    if not changes:
        return ()
    epoch = int(snapshot.measurements[measurement_id].get("epoch", 0))
    return tuple(sorted({str(f) for change in changes[epoch:] for f in change["fields"]}))


def fields_between(
    snapshot: CampaignSnapshot, source_id: str, target_id: str | None = None
) -> tuple[str, ...]:
    """The fields changed in place from `source_id`'s epoch up to `target_id`'s (G2 spec §3.2).

    A family-12 completion imports from a sealed winner, normally of its own
    epoch; across an accepted `continue` the changed fields accumulate per
    row. `target_id=None` means a target bound now, in the latest epoch.
    """

    changes = snapshot.condition_changes
    source = int(snapshot.measurements[source_id].get("epoch", 0))
    target = (
        len(snapshot.epochs) - 1
        if target_id is None
        else int(snapshot.measurements[target_id].get("epoch", 0))
    )
    return tuple(sorted({str(f) for change in changes[source:target] for f in change["fields"]}))


def read_frozen_source(
    run_root: Path, run_id: str, measurement_id: str, *, expected_identity_digest: str
) -> SourceMeasurement:
    """Open a frozen measurement of this run as an in-run import source (I2 spec §5.3).

    The same `SourceMeasurement` `read_source` returns, so `admit_rows` runs
    unchanged over it; `run_id` is this campaign's own run id, the value every
    newly carried row's `source.run_id` then names. It differs from
    `read_source` exactly where the source differs: the measurement is opened
    through `Measurement.open_frozen` (no corpus check, no recovery), no lease
    is probed (nothing appends to a frozen measurement, and this run's own
    process is the only one that could), and the measurement need not be
    complete -- an unfinished one is the point, and `admit_rows` takes only its
    scored latest tries. `expected_identity_digest` is the campaign snapshot's
    binding for the measurement; a journal born under any other identity, or
    one that cannot be read at all, raises `FrozenSourceCorrupt`.
    """

    try:
        frozen = Measurement.open_frozen(run_root, measurement_id)
    except MeasurementError:
        raise FrozenSourceCorrupt(measurement_id, "unreadable") from None
    if frozen.identity_digest != expected_identity_digest:
        raise FrozenSourceCorrupt(measurement_id, "identity_digest_mismatch")
    return SourceMeasurement(
        run_id=run_id,
        measurement_id=measurement_id,
        identity_digest=frozen.identity_digest,
        prefix_digest=frozen.prefix_digest,
        records=frozen.records(),
        valid_bytes=frozen.valid_bytes,
        journal_integrity=frozen.journal_integrity,
    )


def import_sealed(
    run_root: Path,
    run_id: str,
    measurement_id: str,
    target: RunIdentityV1,
    *,
    expected_identity_digest: str,
    primary: str,
    condition_changed: Sequence[str] = (),
    declared: tuple[int, str] | None = None,
    primary_range: bool = False,
) -> BaselineImport:
    """Admit a sealed measurement of this run into `target` (G2 spec §3.2, ADR-0017 amended).

    A sealed source is a measurement of the same run that the campaign
    journal guarantees nothing will be appended to again -- in family 12, the
    selected winner's Candidate measurement once the round's Promotion is
    durable. The caller owns that predicate; this function reads the source
    with I2's frozen-source reader (no lease: the reading campaign holds the
    run) and applies ADR-0017's unchanged admission rule, with two additions
    that hold for this source only:

    - **same tree:** the source's `candidate_digest` must equal the
      target's -- a completion carries the winner's own rows into the
      winner's own measurement;
    - **corruption is hard:** with `declared = (imported, pairs_digest)`, the
      owed copy's re-read must reproduce the declaration exactly.

    Every refusal is `FrozenSourceCorrupt`, never an authorizable loss.
    `primary_range` is `admit_rows`'s, under the version in force at the
    completion's `completion_bound`. `condition_changed` is empty within one condition epoch and names the
    accepted change's fields when a `continue` separates source and target.
    """

    source = read_frozen_source(
        run_root, run_id, measurement_id, expected_identity_digest=expected_identity_digest
    )
    source_digest = source.records[0].payload["identity"]["candidate_digest"]
    if source_digest != target.to_payload()["candidate_digest"]:
        raise FrozenSourceCorrupt(measurement_id, "candidate_digest_mismatch")
    admitted = admit_rows(
        source,
        target,
        primary=primary,
        condition_changed=condition_changed,
        primary_range=primary_range,
    )
    if declared is not None and (admitted.imported, admitted.pairs_digest) != tuple(declared):
        raise FrozenSourceCorrupt(measurement_id, "pairs_digest_mismatch")
    return admitted


def _lease_held(directory: Path) -> bool:
    """Whether a writer holds the measurement journal's lease, without taking it.

    The owner holds `LOCK_EX` on `.owner.lock` for as long as it writes, so a
    non-blocking `LOCK_SH` fails exactly while it is held; the probe releases
    and closes at once. A missing lock file (a missing directory, an unborn or
    legacy journal) is not "locked": the replay checks judge it. Any other
    failure to open the file -- a symlink refused by `O_NOFOLLOW` included --
    is an unreadable source.
    """

    try:
        fd = os.open(directory / LEASE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            return False
        raise BaselineSourceUnavailable() from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            raise BaselineSourceUnavailable() from None
        return False
    finally:
        os.close(fd)  # releases the probe's shared lock


def admit_rows(
    source: SourceMeasurement,
    target: RunIdentityV1,
    *,
    primary: str,
    condition_changed: Sequence[str],
    dotted: bool = False,
    primary_range: bool = False,
) -> BaselineImport:
    """Reduce `source` against `target`'s schedule into the admitted `sample_imported` set (§3.2).

    `primary_range` is `primary_range_required` under the version the carry
    is admitted under -- the creation's for a declared baseline import, the
    one in force at `measurement_rebound` or `completion_bound` for an in-run
    carry -- so a re-read reproduces the declaration (ADR-0019 as amended
    2026-10-03).

    `dotted` is set for a family-6 successor (codex branch review round 1,
    PDR-0016 "families 5 and 6 are unchanged"): the bare top-level names an
    in-place change records (`target`) are written in the dotted grammar
    0.3.0a17 reads (`identity.target`), in each row's `condition_fields` and so
    in the declaration's `fields`.
    """

    state = Lifecycle()
    for record in source.records:
        state.accept(record)

    source_identity = source.records[0].payload["identity"]
    source_target = source_identity["target"]
    rows = latest_rows(
        source.records,
        state,
        seed_mode=source_target["seed_mode"],
        dependency_digest=source_target["dependency_digest"],
    )
    # Ruling R8: a schedule's own key is `(case_id, repeat)`. Under
    # `unpaired`/`none` the sample id carries the measurement side
    # (`candidate-01` for a promoted winner's attempt, `champion-01` for the
    # successor's baseline), so matching the literal sample id would carry
    # nothing from a promoted predecessor.
    by_key = {(row.case_id, row.repeat): row for row in rows}

    target_payload = target.to_payload()
    schedule = target_payload["schedule"]
    target_field = target_payload["target"]
    target_exact = target_field["seed_mode"] == "exact"
    target_dependency_digest = target_field["dependency_digest"]
    target_candidate_digest = target_payload["candidate_digest"]

    def spelled(fields: Any) -> set[str]:
        names = {str(f) for f in fields}
        if not dotted:
            return names
        return {f"identity.{f}" if f in COMPARABLE_FIELDS else f for f in names}

    changed_fields = sorted(spelled(condition_changed) | spelled(source.condition_fields))
    admitted: list[dict[str, Any]] = []
    origin_runs: set[str] = set()

    for entry in schedule:
        row = by_key.get((str(entry["case_id"]), int(entry["repeat"])))
        if row is None:
            continue
        if entry["requested_seed"] != row.requested_seed:
            continue
        # `disposition == "score"` is the whole rule PDR-0013 and ADR-0017
        # wrote: it already means the terminal's registered kind is `score`
        # *and* the try was scored (`latest_rows`). Naming terminals beside
        # it would silently exclude a scoring terminal -- `candidate_failed`
        # was exactly that -- from evidence the records admit.
        if row.disposition != "score":
            continue
        if row.metrics is None or primary not in row.metrics:
            continue
        if primary_range and not 0.0 <= float(row.metrics[primary]) <= 1.0:
            continue
        if target_exact and row.dependency_digest != target_dependency_digest:
            continue

        key = (row.case_id, row.sample_id)
        carried = state.imported.get(key)
        if carried is not None:
            old_source = carried.payload["source"]
            source_field = dict(
                old_source,
                condition_fields=sorted(
                    spelled(old_source["condition_fields"]) | set(changed_fields)
                ),
            )
        else:
            execution = state.executions[key]
            scoring = state.scoring[key]
            source_field = dict(
                run_id=source.run_id,
                measurement_id=source.measurement_id,
                identity_digest=source.identity_digest,
                candidate_digest=source_identity["candidate_digest"],
                retry_ordinal=row.retry_ordinal,
                rollout_digest=record_digest(execution),
                scoring_digest=record_digest(scoring),
                condition_fields=changed_fields,
            )
        # G2b1: a family-12 target keeps what the source row says ran it --
        # from its own `rollout_finished`, or from the `sample_imported` a
        # transitive row was carried in with. An unmarked target never gets the
        # keys, so families 5-11 and their `pairs_digest` are unchanged.
        carried_fields: dict[str, Any] = {}
        if target.row_provenance:
            origin = carried if carried is not None else state.executions[key]
            for name in ("provenance", "components_digest"):
                if name in origin.payload:
                    carried_fields[name] = origin.payload[name]
        origin_runs.add(str(source_field["run_id"]))
        admitted.append(
            dict(
                case_id=entry["case_id"],
                sample_id=entry["sample_id"],
                candidate_digest=target_candidate_digest,
                outcome=row.outcome,
                metrics=dict(row.metrics),
                critical_pass=row.critical_pass,
                effective_seed=row.effective_seed,
                dependency_digest=row.dependency_digest,
                source=source_field,
                **carried_fields,
            )
        )

    fields = sorted({f for item in admitted for f in item["source"]["condition_fields"]})
    return BaselineImport(
        source_measurement_id=source.measurement_id,
        source_prefix_digest=source.prefix_digest,
        pairs_digest=import_pairs_digest(admitted),
        imported=len(admitted),
        scheduled=len(schedule),
        fields=tuple(fields),
        origin_runs=len(origin_runs),
        rows=tuple(admitted),
    )


#: The measurement identity sections a trace projection reads besides the tree
#: (`editor_evidence.project_observations`, `reflect_evidence.project_repeats`,
#: `reflection_inputs.FrozenReflectionInputs`): a carried row is trace-backed
#: only when its source journal's sections equal the importing measurement's.
PROJECTION_SECTIONS = ("environment", "scorer")


def carried_traces(
    records: Sequence[Envelope], *, run_root: Path, run_id: str
) -> dict[tuple[str, str], tuple[Envelope, Envelope]]:
    """Each in-run carried row's source scoring records, under the row's own key.

    ADR-0017, amended 2026-10-02 (family 12 only; the caller gates on the
    family). `records` is a measurement journal's durable prefix. For every
    `sample_imported` whose `source.run_id` is `run_id` -- a row carried from a
    measurement of this same run: a completion's import from its sealed
    winner, or a later in-run re-bind -- the source try is found in the source
    measurement journal named by `source.measurement_id`, and its own
    `scoring_started` and `scoring_finished` are returned under the **target**
    key `(case_id, sample_id)`. Nothing is rewritten: the scorer trace binds to
    the source record exactly as it was written, so `_observation` checks it as
    it would on the source.

    `source` always names the originally measured journal (a transitive carry
    keeps it), so one lookup suffices. The source try is the one whose
    `rollout_finished` and scoring terminal reproduce `source.rollout_digest`
    and `source.scoring_digest` (`record_digest`, as `admit_rows` wrote them),
    for the row's `(case_id, repeat)` in the source's schedule (ruling R8) and
    its `source.retry_ordinal`; the source journal must be the one
    `source.identity_digest` names, measuring the target's tree.

    The consumers project the trace under the *target's* identity: its
    `environment` decides which turn content is shown or withheld (`_turns`),
    and its `scorer` binds the trace and reads the scorer's output. So a source
    whose `environment` or `scorer` section differs from the target's -- an
    accepted `continue` may change either -- offers nothing, and its rows stay
    `unavailable` (PDR-0017: a `scripted` source's private script never
    becomes a `static` target's shown input).

    A row whose source is another run's, or whose source cannot be read or does
    not reproduce every check, is simply left out -- its consumers keep seeing
    it as they do today (`absent` / `unavailable`). This function never raises.
    """

    try:
        identity = records[0].payload["identity"]
        target_digest = str(identity["candidate_digest"])
        projection = {name: identity[name] for name in PROJECTION_SECTIONS}
        repeats = {
            (str(entry["case_id"]), str(entry["sample_id"])): int(entry["repeat"])
            for entry in identity["schedule"]
        }
    except Exception:
        return {}
    # Committed local diagnostics are persistence authority. Keep this outside
    # the historical best-effort guards below so corruption is never a gap.
    from evolution.tree.baseline_diagnostics import DiagnosticIndex

    committed: dict[tuple[str, str], tuple[Envelope, Envelope]] = {}
    local_index = DiagnosticIndex(run_root)
    for record in records:
        if record.event != "sample_imported":
            continue
        payload = record.payload
        key = (str(payload["case_id"]), str(payload["sample_id"]))
        local = local_index.source(payload["source"], key[0], repeats[key])
        if (
            local is not None
            and all(
                local.entry["projection"][name] == projection[name] for name in PROJECTION_SECTIONS
            )
            and local.entry["source"]["candidate_digest"] == target_digest
        ):
            committed[key] = (local.envelope("scoring_started"), local.envelope("scoring_finished"))
    by_source: dict[str, list[tuple[tuple[str, str], Mapping[str, Any]]]] = {}
    for record in records:
        try:
            if record.event != "sample_imported":
                continue
            payload = record.payload
            source = payload.get("source")
            if not isinstance(source, Mapping) or source.get("run_id") != run_id:
                continue
            key = (str(payload["case_id"]), str(payload["sample_id"]))
            by_source.setdefault(str(source["measurement_id"]), []).append((key, source))
        except Exception:
            continue
    found: dict[tuple[str, str], tuple[Envelope, Envelope]] = dict(committed)
    for measurement_id, rows in sorted(by_source.items()):
        try:
            frozen = Measurement.open_frozen(run_root, measurement_id)
            source_identity = frozen.records()[0].payload["identity"]
            if any(source_identity.get(name) != projection[name] for name in PROJECTION_SECTIONS):
                continue
            tries = _SourceTries(frozen.records())
        except Exception:
            continue
        for key, source in rows:
            try:
                if (
                    source.get("identity_digest") != frozen.identity_digest
                    or source.get("candidate_digest") != frozen.candidate_digest
                    or frozen.candidate_digest != target_digest
                    or key not in repeats
                ):
                    continue
                pair = tries.scored(key[0], repeats[key], source)
            except Exception:
                continue
            if pair is not None:
                found[key] = pair
    return found


class _SourceTries:
    """One source journal's scored tries, indexed by the digests `admit_rows` wrote."""

    def __init__(self, records: Sequence[Envelope]) -> None:
        schedule = records[0].payload["identity"]["schedule"]
        self.sample_ids = {
            (str(entry["case_id"]), int(entry["repeat"])): str(entry["sample_id"])
            for entry in schedule
        }
        self.rollouts: dict[str, Envelope] = {}
        self.scorings: dict[str, Envelope] = {}
        self.started: dict[str, Envelope] = {}
        for record in records:
            event = record.event
            if event == "rollout_finished":
                self.rollouts[record_digest(record)] = record
            elif event == "scoring_finished":
                self.scorings[record_digest(record)] = record
            elif event == "scoring_started" and record.operation_id is not None:
                self.started[record.operation_id] = record

    def scored(
        self, case_id: str, repeat: int, source: Mapping[str, Any]
    ) -> tuple[Envelope, Envelope] | None:
        sample_id = self.sample_ids.get((case_id, repeat))
        execution = self.rollouts.get(str(source.get("rollout_digest")))
        finished = self.scorings.get(str(source.get("scoring_digest")))
        if sample_id is None or execution is None or finished is None:
            return None
        started = self.started.get(str(finished.operation_id))
        if started is None:
            return None
        ordinal = source.get("retry_ordinal")
        for record in (execution, started, finished):
            payload = record.payload
            if (
                payload.get("case_id") != case_id
                or payload.get("sample_id") != sample_id
                or payload.get("retry_ordinal", 0) != ordinal
            ):
                return None
        if started.payload.get("score_request_id") != finished.payload.get("score_request_id"):
            return None
        return started, finished
