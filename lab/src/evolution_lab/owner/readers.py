"""Read-only public core projections, independent of process control."""
from __future__ import annotations

import asyncio
import functools
import os
import threading
from collections.abc import Mapping
from dataclasses import replace

from .models import (
    Coverage, OperationResult, OwnerError, Persistence, ReportMarkdownResult, ReportResult, ResourceRef, SuspensionEvidenceResult, _thaw_json,
)
from .resume_rules import status_field
from .receipts import open_directory_nofollow
from .projection_signature import run_signature


class ReadCompleteness:
    """Request-local monotonic evidence, explicitly shared with worker threads.

    Pass one signal through project_operation and _views (or _status_projection),
    then inspect complete after the reads finish. Public DTOs stay unchanged.
    """

    def __init__(self):
        self._failed = threading.Event()

    @property
    def complete(self):
        return not self._failed.is_set()

    def mark_incomplete(self):
        self._failed.set()


def _incomplete(completeness):
    if completeness is not None:
        completeness.mark_incomplete()


def cacheable(projection, complete):
    """Base-projection eligibility, separate from signature/acquired/writer gates.

    Includes b15's classification and independent evidence. Siblings is a map
    keyed by measurement ids and the artifact store. Arbitrary text is not state.
    Unanswered is an independent overlay, assembled on *every* status read; no
    unavailable count is stored or served by the base-projection cache.
    """
    payload = projection.to_payload() if hasattr(projection, 'to_payload') else projection
    if isinstance(payload.get('result'), Mapping):
        payload = {**payload, 'result': {key: value for key, value in payload['result'].items()
            if key not in {'unanswered', 'unanswered_state', 'answered_reported'}}}
    if not complete or payload.get('results_state') != 'present' \
            or (payload.get('kind') == 'campaign' and payload.get('entrypoint') is None):
        return False

    def degraded(value, states=False):
        if isinstance(value, Mapping):
            for key, item in value.items():
                state = states or key.endswith('_state') or key in {
                    'state', 'status', 'classification', 'evidence', 'health'}
                if state and isinstance(item, str) and item in {'unavailable', 'unreadable'}:
                    return True
                if degraded(item, key == 'siblings'):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(degraded(item) for item in value)
        return False

    return not degraded(payload)


def operation_ref(value):
    if not isinstance(value, ResourceRef) or value.kind != 'operation':
        raise OwnerError('invalid_argument', 'read')
    return value


def acquire_campaign(root, operation_id, *, with_result=True):
    """The one Core acquisition of an Owner view/report: ``campaign_read`` with requested binding DTOs, or the
    exception it raised. The report, its status metadata and usage all take it (``project_operation(acquired=)``,
    ``read_campaign_report(read=)``), so they read one snapshot; a failed read fails each of them as it would."""
    from .core_raw import FAMILIES, campaign_read
    try:
        return campaign_read(root / 'artifacts' / operation_id / operation_id, families=FAMILIES,
                             with_result=with_result, with_bindings=True)
    except Exception as error:
        return error


def acquired_read(acquired):
    """The ``CampaignRead`` of ``acquire_campaign``; its exception is raised here, where its reader would raise it."""
    if isinstance(acquired, BaseException):
        raise acquired
    return acquired


def _project_campaign(root, status, acquired=None, *, completeness=None):
    from evolution.tree import JournalFault
    from evolution.tree.campaign import CampaignFamilyUnsupported
    from .core_raw import FAMILIES, campaign_read
    run = root / 'artifacts' / status.id / status.id
    core = None
    unsupported = False
    try:
        read = campaign_read(run, families=FAMILIES) if acquired is None else acquired_read(acquired)
        if not read.complete:
            _incomplete(completeness)
        core = read.status.to_payload()
        results_state = 'present'
    except CampaignFamilyUnsupported:
        _incomplete(completeness)
        unsupported = True
        results_state = 'unavailable'
    except JournalFault as error:
        _incomplete(completeness)
        results_state = ('not_created' if error.code == 'journal_unavailable'
                         and _campaign_not_created(status, run, completeness=completeness) else 'unavailable')
    except Exception:
        _incomplete(completeness)
        results_state = 'unavailable'
    prior = status.result
    if core is None and prior is not None and not unsupported:
        core = prior.core
    if core is None and status.process.exit_code is None and prior is None:
        return replace(status, results_state=results_state)
    # ADR-0015: the result is the campaign's.  A start that never launched
    # (e.g. a resume refused before dispatch) produced nothing, so the result
    # keeps the process of the start that last produced it.
    process = status.process
    if prior is not None and status.process.launched is not True:
        process = prior.process
    result = OperationResult(status.prepared_digest, process, core=core,
        persistence=prior.persistence if prior is not None else Persistence())
    return replace(status, result=result, results_state=results_state)


def _campaign_not_created(status, run, *, completeness=None):
    """Only confirmed absence before a known campaign is the creation window."""
    if status.result is not None and status.result.core is not None:
        return False
    try:
        directory = open_directory_nofollow(run)
    except FileNotFoundError:
        _incomplete(completeness)
        return status.process.launched is not True
    except OSError:
        _incomplete(completeness)
        return False
    try:
        try:
            os.stat('campaign.jsonl', dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            _incomplete(completeness)
            return True
        except OSError:
            _incomplete(completeness)
            return False
        return False  # Includes empty, damaged and dangling journal entries.
    finally:
        os.close(directory)


def _run_lost(status, run):
    """A launched operation whose run directory is gone: damage, not "not started".

    Lab creates ``artifacts/<id>/<id>`` before it launches the child (``create_campaign_paths``),
    so after launch a missing run is a failed read. Before launch (``launched`` None or False)
    a missing run is the ordinary pre-start window."""
    return status.process.launched is True and not run.exists()


def _evaluation_outcome(scope_directory, status, run_id, *, completeness=None):
    if scope_directory is None:
        _incomplete(completeness)
        return {"stopped_reason": "unknown", "unmeasured": None, "min_coverage": 1.0}
    from .execution import read_evaluation_outcome

    try:
        outcome = read_evaluation_outcome(
            scope_directory / "evaluation-outcome.json",
            operation_id=status.id,
            scope_id=scope_directory.name,
            run_id=run_id,
        )
        return outcome
    except (OSError, ValueError):
        _incomplete(completeness)
        return {"stopped_reason": "unknown", "unmeasured": None, "min_coverage": 1.0}


def _project_evaluation(root, status, scope_directory, *, completeness=None):
    from evolution.tree import inspect_journal, project_result
    from evolution.tree.evolve import derive_run_id

    run = root / "artifacts" / status.id / status.id
    measurement = run / "measurement"
    run_id = derive_run_id(run)
    core = None
    coverage = None
    try:
        snapshot = inspect_journal(measurement)
        if not snapshot.records:
            raise ValueError("measurement identity is missing")
        identity = snapshot.records[0].payload["identity"]
        schedule = identity["schedule"]
        if not isinstance(schedule, list):
            raise ValueError("measurement schedule is invalid")
        core = project_result(snapshot).to_payload()
        samples = core["samples"]
        if not isinstance(samples, list):
            raise ValueError("measurement samples are invalid")
        coverage = Coverage(
            expected=len(schedule),
            scored=sum(
                row.get("scoring_state") == "finished"
                for row in samples
                if isinstance(row, dict)
            ),
        )
        results_state = "present"
    except Exception:
        _incomplete(completeness)
        # The worker creates the measurement journal after launch and may stop before it
        # (cancelled): a missing journal under an existing run is not damage.
        results_state = "unavailable" if _run_lost(status, run) or measurement.exists() else "not_created"
    prior = status.result
    outcome = _evaluation_outcome(scope_directory, status, run_id, completeness=completeness)
    result = OperationResult(
        status.prepared_digest,
        status.process,
        core=core,
        coverage=coverage,
        stopped_reason=outcome["stopped_reason"],
        unmeasured=outcome["unmeasured"], min_coverage=outcome["min_coverage"],
        persistence=prior.persistence if prior is not None else Persistence(),
        kind="evaluation",
    )
    return replace(status, result=result, results_state=results_state)


def project_operation(root, status, *, scope_directory=None, acquired=None, completeness=None):
    """``acquired`` (``acquire_campaign``) is a campaign's Core read already made for this request."""
    if status.kind == "evaluation":
        return _project_evaluation(root, status, scope_directory, completeness=completeness)
    return _project_campaign(root, status, acquired, completeness=completeness)


def _times_view(times=None, state="not_started", source=None, reason=None):
    return {"times": times, "times_state": state, "times_source": source, "times_reason": reason}


def _times_and_facts(root, status, cache, completeness=None):
    """(times view, facts_by_measurement) from public journals and the adapter.

    The times view is ``times`` with ``times_state``, ``times_source`` and ``times_reason``
    (ADR-0019). The campaign journal's times (the adapter) and the per-measurement facts
    (public ``project_events``) are read separately: an adapter failure leaves ``times`` null
    with ``times_state: unavailable`` and the adapter's reason, but keeps every measurement's
    facts."""
    from . import core_raw
    from .measurements import read_journal_facts
    run = root / "artifacts" / status.id / status.id
    moments, facts = [], {}
    if _run_lost(status, run):
        _incomplete(completeness)
        return _times_view(state="unavailable", reason="unreadable"), facts
    if status.kind == "evaluation":
        source = "core_public"
        try:
            if not (run / "measurement").exists():
                return _times_view(), facts
            row = read_journal_facts(run / "measurement")
        except Exception:
            _incomplete(completeness)
            return _times_view(state="unavailable", reason="unreadable"), facts
        facts[status.id] = row
        moments += [row.first_at, row.last_at]
    else:
        source = "core_raw"
        reason = None
        try:
            campaign = core_raw.campaign_journal_times(run)
            times_state = None if campaign is not None else "not_started"
        except core_raw.CoreRawUnavailable as error:
            _incomplete(completeness)
            campaign, times_state, reason = None, "unavailable", error.code
        except Exception:
            _incomplete(completeness)
            campaign, times_state, reason = None, "unavailable", "unreadable"
        moments += list(campaign or ())
        core = status.result.core if status.result is not None else None
        for entry in (core or {}).get("measurements") or ():
            try:
                measured = entry.get("status")
                if not isinstance(measured, Mapping):  # core is frozen (MappingProxyType)
                    continue
                key = (status.id, measured["run_id"], entry["measurement_id"], measured["last_sequence"])
                row = cache.get(key, lambda e=entry: read_journal_facts((run / e["locator"]).parent))
            except Exception:  # this measurement has no facts; the rest are still read (spec 2.10)
                _incomplete(completeness)
                times_state, reason = "unavailable", reason or "unreadable"
                continue
            facts[entry["measurement_id"]] = row
            moments += [row.first_at, row.last_at]
        if times_state is not None:
            return _times_view(state=times_state, reason=reason), facts
    moments = [m for m in moments if m is not None]
    if not moments:
        return _times_view(), facts
    return _times_view({"first_event_at": min(moments), "last_event_at": max(moments)}, "present",
                       source), facts


def _journal_of(state_directory, status, core, measurement_id):
    run = state_directory / "artifacts" / status.id / status.id
    if status.kind == "evaluation":
        root = run / "measurement"
        return root, root / "events.jsonl"
    for entry in core.get("measurements") or ():
        if entry["measurement_id"] == measurement_id:
            return run, run / entry["locator"]
    raise OwnerError("not_found", "read")


class ReaderSupport:
    async def inputs(self, ref):
        """Prepared and Bundle identities per start, on a separate closed surface."""
        self._assert_loop()
        ref = operation_ref(ref)
        rows = await self._db('operation_inputs', ref.id)
        starts = []
        for row in rows:
            stored = await asyncio.to_thread(self._storage.get_prepared, row['prepared_digest'])
            starts.append({**row, 'bundle_digest': stored.descriptor.bundle_digest})
        return {'schema': 'lab-operation-inputs/1', 'operation_id': ref.id, 'starts': starts}

    async def refusal(self, ref):
        """Core findings for the last Core start, retained across pre-Core failures."""
        self._assert_loop()
        ref = operation_ref(ref)
        return await self._db('operation_refusal', ref.id)

    async def failure(self, ref):
        """Worker exception evidence for the latest start only."""
        from .diagnostic import read_worker_exception
        from .models import WorkerFailureRecord
        self._assert_loop()
        ref = operation_ref(ref)
        status = await self._db('operation_status', ref.id)
        if not status.starts or status.results_state in {'deleted', 'deleting', 'delete_failed'}:
            return None
        start = status.starts[-1]
        exception = await asyncio.to_thread(read_worker_exception,
            self.state_directory / 'scopes' / start.scope_id / 'diagnostic.json',
            operation_id=ref.id, scope_id=start.scope_id, kind=status.kind)
        if exception is None:
            return None
        return WorkerFailureRecord(ref.id, start.start, start.scope_id, exception,
                                   f'scopes/{start.scope_id}/stderr.log')

    async def bundle_measurements(self, bundle_digest):
        """Fixed evaluations on equal non-null Bundle inputs, with their saved conditions."""
        self._assert_loop()
        from .bundle_measurements import read
        return await read(self, bundle_digest)

    async def _campaign_plan(self, prepared_digest, *, completeness=None):
        """Entry name and campaign policy, read once per content-addressed PreparedExecution.

        Cached only when the answer is definitive: the descriptor and its bundle were both
        read (a missing campaign policy included), or the PreparedExecution is gone. A
        partial or failed read is retried on the next status."""
        cached = self._plan_cache.get(prepared_digest)
        if cached is not None:
            if cached.entrypoint is None:
                _incomplete(completeness)
            return cached

        def read():
            from evolution_lab.preparation.models import PreparationError
            from .binding import open_prepared
            from .progress import CampaignPlan
            try:
                stored = self._storage.get_prepared(prepared_digest)
            except PreparationError as error:
                _incomplete(completeness)
                return CampaignPlan(None, None, None, None), error.code == "not_found"
            except OSError:
                _incomplete(completeness)
                return CampaignPlan(None, None, None, None), False
            entrypoint = stored.descriptor.entrypoint
            try:
                binding = open_prepared(self._storage, prepared_digest, stage="read")
            except (OwnerError, PreparationError, OSError):
                _incomplete(completeness)
                return CampaignPlan(entrypoint, None, None, None), False
            workflow = getattr(binding.bundle.manifest, "workflow", None)
            policy = getattr(workflow, "campaign", None)
            if policy is None:
                return CampaignPlan(entrypoint, None, None, None), True
            return CampaignPlan(entrypoint, policy.rounds, policy.attempts_per_round,
                                getattr(policy, "metric_primary", None)), True

        plan, definitive = await asyncio.to_thread(read)
        if definitive:
            self._plan_cache[prepared_digest] = plan
        return plan

    async def _views(self, status, *, completeness=None):
        from .progress import campaign_progress, evaluation_progress
        plan = await self._campaign_plan(status.prepared_digest, completeness=completeness)
        if status.results_state in {"deleting", "delete_failed", "deleted"}:
            return replace(status, entrypoint=plan.entrypoint, progress_state="unavailable",
                           **_times_view(state="unavailable", reason="unreadable"))
        view, _ = await self._result_thread(
            _times_and_facts, self.state_directory, status, self._measurements, completeness)
        times, times_state = view["times"], view["times_state"]
        observed = None if times is None else times["last_event_at"]
        core = status.result.core if status.result is not None else None
        usable = status.results_state == "present" or (
            core is not None and status.control_state not in {"reserved", "running", "stopping"})
        if status.kind == "evaluation":
            coverage = status.result.coverage if status.result is not None else None
            progress = evaluation_progress(coverage, observed) if usable and coverage is not None else None
        else:
            progress = campaign_progress(core, plan, observed) if usable else None
        if progress is not None:
            progress_state = "present"
        elif status.results_state == "not_created" or (status.results_state == "present" and core is None) \
                or (core is None and times_state == "not_started"):
            # Before the journal exists nothing has started; "unavailable" is for real read failures.
            # A launched run that is gone reads times_state "unavailable" (_run_lost), never here.
            progress_state = "not_started"
        else:
            progress_state = "unavailable"
        value = replace(status, entrypoint=plan.entrypoint, progress=progress,
                        progress_state=progress_state, **view)
        return value

    async def _status_projection(self, ref, durable=None, acquired=None, *, completeness=None):
        """Private projection; caller owns a result lease for the full read. ``acquired`` is the request's own
        campaign read (``acquire_campaign``), used instead of a fresh one."""
        completeness = completeness if completeness is not None else ReadCompleteness()
        durable = durable or await self._db('operation_status', ref.id)
        if durable.results_state in {'deleting', 'delete_failed', 'deleted'}:
            return await self._views(durable, completeness=completeness)
        value = self._operation_cache.get(ref.id, durable)
        runtime = self._operations.get(ref.id)
        if runtime is not None and not runtime.done.done():
            value = replace(value, process=runtime.process_facts(value.process))
        if runtime is not None and runtime.paths is not None:
            scope_directory = runtime.paths.scope_directory
        else:
            scopes = await self._db('resource_scopes', ref)
            scope_directory = (
                self.state_directory / scopes[0]['root_relative'] if scopes else None
            )
        # This is a separate cache from the finalization overlay. Values may
        # contain frozen mappings, so compare the key rather than hashing it.
        key = None
        if acquired is None:
            plan = await self._campaign_plan(value.prepared_digest, completeness=completeness)
            signature = await self._result_thread(
                run_signature, self.state_directory, ref.id, scope_directory)
            if signature is not None:
                key = (durable, value, plan, scope_directory, signature)
                cached = self._projection_cache.get(ref.id) if completeness.complete else None
                if cached is not None and cached[0] == key:
                    self._remember_summary(durable, cached[1])
                    return await self._status_unanswered(cached[1])
        projected = await self._result_thread(project_operation, self.state_directory,
            value, scope_directory=scope_directory, acquired=acquired, completeness=completeness)
        # Row 7 needs the fresh projection, as admission does (ADR-0015): when the
        # fresh read failed, `result.core` is the cached prior projection and
        # `results_state` is not `present`.
        fresh = projected.results_state == 'present' and projected.result is not None
        core = projected.result.core if fresh else None
        # Rows 1–6 read the durable record, as admission does (spec 2026-09-24 §2.2).
        value = await self._views(replace(projected, resume=status_field(durable, core)),
                                  completeness=completeness)
        # Cache the base projection independently of the count overlay. The
        # overlay is always assembled from current evidence below, including
        # retries of unavailable evidence; it is never cached as a failed read.
        if key is not None and cacheable(value, completeness.complete):
            self._projection_cache[ref.id] = (key, value)
        self._remember_summary(durable, value)
        return await self._status_unanswered(value)

    async def _status_unanswered(self, value):
        if value.kind == 'evaluation' and value.result is not None and value.result.core is not None:
            counts = await self._unanswered_counts(value, _thaw_json(value.result.core))
            count = counts[value.result.core['run_id']]
            coverage = value.result.coverage
            reported, unanswered = count['answered_reported'], count['unanswered']
            state = count['unanswered_state']
            # Concurrent evidence can describe a newer/older admitted set than
            # this coverage snapshot. Degrade only that inconsistency; malformed
            # counters and other DTO/programming errors still fail validation.
            valid_count = (state in {'present', 'partial'} and type(reported) is int
                           and type(unanswered) is int and 0 <= unanswered <= reported and reported > 0)
            if valid_count and coverage is not None and (
                    reported > coverage.scored or ((state == 'present') != (reported == coverage.scored))):
                count = {'unanswered': None, 'unanswered_state': 'unavailable', 'answered_reported': None}
            return replace(value, result=replace(value.result, **count))
        return value

    @staticmethod
    def _summary_signature(durable):
        # Everything the resume verdict and the row read from the durable record; a resume
        # adds a start, a delete or cleanup changes a state.  Only fully read rows are cached
        # (see _remember_summary), so a degraded read is retried rather than kept.
        return (durable.control_state, durable.results_state, len(durable.starts),
                durable.target_cleanup.state, durable.target_ref)

    def _remember_summary(self, durable, value):
        """Keep the row of a settled, fully readable operation (spec 2026-09-29 §2.2).

        ``_operation_cache`` holds only the runtime's transient value (dropped once settled),
        never the projection: keep the row."""
        from .summary import operation_summary
        progress = value.progress if isinstance(value.progress, Mapping) else None
        degraded = (value.times_state == "unavailable" or value.progress_state == "unavailable"
                    or (value.kind == "campaign" and (value.entrypoint is None or (
                        progress is not None and progress.get("policy_state") == "unavailable"))))
        if durable.control_state in {"reserved", "running", "stopping"} or value.results_state != "present" \
                or degraded:
            self._summary_cache.pop(durable.id, None)
            return
        self._summary_cache[durable.id] = (self._summary_signature(durable), operation_summary(value))

    async def status(self, ref):
        self._assert_loop()
        ref = operation_ref(ref)
        if self._result_io.writing(ref.id):
            value = await self._db('operation_status', ref.id)
            # The writer may still be collecting the summary before its intent
            # commits. Even then, return only small durable facts.
            if value.result is not None:
                value = replace(value, result=replace(value.result,
                    core=None, campaign_execution=None))
            # The entry name only from a plan already read: no disk read while a writer holds it.
            plan = self._plan_cache.get(value.prepared_digest)
            value = replace(value, entrypoint=plan.entrypoint if plan is not None else None,
                            progress_state="unavailable",
                            **_times_view(state="unavailable", reason="unreadable"))
            return replace(value, auto_resume=await self._auto_resume.project(value))
        async def work():
            return await self._status_projection(ref)
        value = await self._result_work(work,
            self._result_io.acquire_read(ref.id, 'read'))
        return replace(value, auto_resume=await self._auto_resume.project(value))

    async def list_operations(self):
        self._assert_loop()
        values = await self._db('list_operations')
        return tuple([await self.status(ResourceRef('operation', item.id)) for item in values])

    async def list_summaries(self, ids=None):
        """Active operations read their projection; settled ones reuse their last row (§2.2)."""
        from .summary import operation_summary
        self._assert_loop()
        rows = []
        for item in await self._db('list_operations'):
            if ids is not None and item.id not in ids:
                continue
            cached = self._summary_cache.get(item.id)
            if item.kind != "campaign" and cached is not None and cached[0] == self._summary_signature(item) \
                    and not self._result_io.writing(item.id):
                rows.append(cached[1])
                continue
            rows.append(operation_summary(await self.status(ResourceRef('operation', item.id))))
        return tuple(rows)

    async def _readable_status(self, ref, acquired=None):
        """Status projection and its core; caller owns a result lease."""
        status = (await self._status_projection(ref) if acquired is None
                  else await self._status_projection(ref, acquired=acquired))
        if status.results_state == "deleted":
            raise OwnerError("results_deleted", "read")  # as diff/export after delete_results
        if status.results_state in {"deleting", "delete_failed"}:
            raise OwnerError("not_found", "read")
        core = status.result.core if status.result is not None else None
        if core is None:
            raise OwnerError("not_found", "read")
        return status, core

    async def _measurement_views(self, ref, detail=None):
        """Measurement rows from the fresh projection; caller owns a result lease."""
        status, core = await self._readable_status(ref)
        plan = await self._campaign_plan(status.prepared_digest)
        return await self._rows_of(status, core, plan.metric_primary, detail=detail)

    async def _rows_of(self, status, core, metric_primary, *, detail=None):
        """``measurement_rows`` over every core measurement; unread facts stay ``unavailable``."""
        from .measurements import measurement_rows
        _, facts = await self._result_thread(_times_and_facts, self.state_directory, status,
                                                self._measurements)
        core = _thaw_json(core)
        if status.kind == "evaluation":
            # The journal facts are keyed by operation id; core names the measurement by its run id.
            facts = {core["run_id"]: facts.get(status.id)}
        notices = await self._evaluation_notices(status, core) if status.kind == "evaluation" else None
        unanswered = await self._unanswered_counts(status, core)
        return measurement_rows(core, status.kind, facts, metric_primary, detail=detail, notices=notices,
                                unanswered=unanswered)

    async def _unanswered_counts(self, status, core):
        from .measurements import unanswered_count
        def read():
            entries = ([{"measurement_id": core["run_id"], "status": {"samples": core.get("samples") or []}}]
                       if status.kind == "evaluation" else core.get("measurements") or [])
            values = {}
            for entry in entries:
                mid = entry["measurement_id"]
                root, journal = _journal_of(self.state_directory, status, core, mid)
                samples = (entry.get("status") or {}).get("samples") or []
                values[mid] = unanswered_count(samples, root, journal, mid,
                    cache=self._measurements, operation_id=status.id)
            return values
        return await self._result_thread(read)

    async def _evaluation_notices(self, status, core):
        """Cache the private aggregate under the dev67 nofollow metadata rules.

        The only files read belong to the run: the outcome scope is not an input.
        The status value covers durable/overlay/plan effects on this projection.
        A second signature prevents caching evidence changed during its read.
        """
        from .measurements import evaluation_focus_notices
        def read():
            signature = run_signature(self.state_directory, status.id, None)
            key = (status, core, signature) if signature is not None and cacheable(status, True) else None
            root, journal = _journal_of(self.state_directory, status, core, core["run_id"])
            def load():
                value = evaluation_focus_notices(core, root, journal,
                    terminal=status.control_state not in {"reserved", "running", "stopping"}
                             and status.result is not None, cache=self._measurements, operation_id=status.id)
                return value, signature is not None and signature == run_signature(self.state_directory, status.id, None)
            return self._measurements.notices(status.id, key, load)
        return await self._result_thread(read)

    async def measurements(self, ref):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            rows = await self._measurement_views(ref)
            return {"schema": "lab-measurement-list/4", "operation_id": ref.id, "measurements": rows}
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def measurement(self, ref, measurement_id):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            rows = await self._measurement_views(ref, detail=measurement_id)
            if not rows:
                raise OwnerError("not_found", "read")
            return {"schema": "lab-measurement/4", "operation_id": ref.id, "measurement": rows[0]}
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def sample(self, ref, measurement_id, case_id, sample_id, *, disclose=False):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            status, core = await self._readable_status(ref)
            plan = await self._campaign_plan(status.prepared_digest)
            rows = await self._rows_of(status, core, plan.metric_primary, detail=measurement_id)
            selected = next(((measured["measurement_id"], sample) for measured in rows
                             for sample in measured.get("samples") or ()
                             if sample["case_id"] == case_id and sample["sample_id"] == sample_id), None)
            if selected is None:
                raise OwnerError("not_found", "read")
            resolved_id, row = selected
            root, journal = _journal_of(self.state_directory, status, _thaw_json(core), resolved_id)
            from .sample_view import sample_view
            return await self._result_thread(sample_view, ref.id, resolved_id, row, root, journal, disclose)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def _content_campaign(self, ref):
        durable = await self._db("operation_status", ref.id)
        if durable.kind != "campaign":
            raise OwnerError("invalid_argument", "read")
        acquired = await self._result_thread(acquire_campaign, self.state_directory, ref.id, with_result=False)
        status, core = await self._readable_status(ref, acquired=acquired)
        plan = await self._campaign_plan(status.prepared_digest)
        rows = await self._rows_of(status, core, plan.metric_primary)
        from .campaign_views import campaign_view, details_unavailable
        from . import core_raw
        run = self.state_directory / "artifacts" / ref.id / ref.id
        try:
            # Family14 raw details are independent of sibling inspection and
            # remain readable even when status retained a cached projection.
            # Only family15 needs the shared, authenticated acquisition.
            details = await self._result_thread(core_raw.campaign_details, run,
                                               **({"read": acquired_read(acquired)}
                                                  if core.get("family_version") == 15 else {}))
        except core_raw.CoreRawUnavailable as error:
            details = details_unavailable(error.code)
        except Exception:
            details = details_unavailable("unreadable")
        view = campaign_view(ref.id, _thaw_json(core), plan, _thaw_json(status.progress), None, None, rows, details)
        return status, _thaw_json(core), view, run

    async def _role_kind(self, prepared_digest, role):
        def read():
            from .binding import open_prepared
            try:
                binding = open_prepared(self._storage, prepared_digest, stage="read")
                kind = getattr(getattr(binding.bundle.manifest, role, None), "kind", None)
                return kind if kind in {"chat", "agent_cli", "subprocess"} else None
            except (OwnerError, OSError, ValueError):
                return None
        return await self._result_thread(read)

    async def _content_facts(self, run):
        from . import core_raw
        try:
            return await self._result_thread(core_raw.campaign_content, run)
        except core_raw.CoreRawUnavailable as error:
            return error

    async def attempt(self, ref, attempt_id, *, disclose=False):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            status, core, view, run = await self._content_campaign(ref)
            found = next(((r, a) for r in view["rounds"] for a in r["attempts"]
                          if a["attempt_id"] == attempt_id), None)
            if found is None:
                raise OwnerError("not_found", "read")
            round_view, attempt = found
            confidence = next((a.get("confidence") for r in core["rounds"] for a in r["attempts"]
                               if a["attempt_id"] == attempt_id), None)
            kind = await self._role_kind(status.prepared_digest, "editor")
            facts = await self._content_facts(run)
            from .sample_view import attempt_view
            return await self._result_thread(attempt_view, ref.id, attempt_id, round_view["round"], confidence,
                                             attempt.get("failure"), kind, run, facts, disclose)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def reflection(self, ref, reflection_id, *, disclose=False):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            status, _, view, run = await self._content_campaign(ref)
            round_view = next((r for r in view["rounds"]
                               if (r.get("reflection") or {}).get("reflection_id") == reflection_id), None)
            if round_view is None:
                raise OwnerError("not_found", "read")
            kind = await self._role_kind(status.prepared_digest, "reflector")
            facts = await self._content_facts(run)
            from .sample_view import reflection_view
            return await self._result_thread(reflection_view, ref.id, reflection_id, round_view["round"],
                                             kind, run, facts, disclose)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def suspensions(self, ref):
        """Closed Core history without requiring an existing status projection."""
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            durable = await self._db('operation_status', ref.id)
            if durable.results_state == "deleted":
                raise OwnerError("results_deleted", "read")
            if durable.results_state in {"deleting", "delete_failed"}:
                raise OwnerError("not_found", "read")
            if durable.kind != "campaign":
                return SuspensionEvidenceResult(ref.id, "unavailable", None, "not_campaign")
            from . import core_raw
            run = self.state_directory / "artifacts" / ref.id / ref.id
            try:
                history = await self._result_thread(core_raw.campaign_suspensions, run)
            except core_raw.CoreRawUnavailable as error:
                return SuspensionEvidenceResult(ref.id, "unavailable", None, error.code)
            return SuspensionEvidenceResult(ref.id, "available", history, None)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def _reflector_probe_record(self, ref):
        try:
            return await self._db("reflector_probe_status", ref.id)
        except Exception:
            return None

    async def campaign(self, ref):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            durable = await self._db('operation_status', ref.id)
            if durable.kind != "campaign":
                raise OwnerError("invalid_argument", "read")
            acquired = await self._result_thread(acquire_campaign, self.state_directory, ref.id, with_result=False)
            status, core = await self._readable_status(ref, acquired=acquired)
            plan = await self._campaign_plan(status.prepared_digest)
            rows = await self._rows_of(status, core, plan.metric_primary)
            run = self.state_directory / "artifacts" / ref.id / ref.id
            from .campaign_views import campaign_view, lineage_champions, baseline_notice_fields
            try:
                champions, trees = await self._result_thread(lineage_champions, run)
            except Exception:
                # An unread lineage is not "nothing promoted": null next to an explicit state.
                champions, trees = None, None
            from . import core_raw
            from .campaign_views import details_unavailable
            try:
                details = await self._result_thread(core_raw.campaign_details, run,
                                                   **({"read": acquired_read(acquired)}
                                                      if core.get("family_version") == 15 else {}))
            except core_raw.CoreRawUnavailable as error:
                details = details_unavailable(error.code)
            except Exception:
                details = details_unavailable("unreadable")
            snapshot = getattr(getattr(acquired, "view", None), "snapshot", None)
            baseline = await self._result_thread(functools.partial(baseline_notice_fields, cache=self._baseline_provenance,
                                                                              read=acquired if not isinstance(acquired, BaseException) else None),
                                                 run, snapshot)
            return campaign_view(ref.id, _thaw_json(core), plan, _thaw_json(status.progress),
                                 champions, trees, rows, details,
                                 reflector_probe=await self._reflector_probe_record(ref), baseline=baseline)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, "read"))

    async def _report_usage(self, status):
        """Target-call tokens per core measurement (PDR-0017), null next to ``usage_state``.

        One row per measurement of the core, from the same rows the measurement views
        return, so an unread journal is an explicit ``unavailable`` row, never a gap."""
        core = status.result.core if status.result is not None else None
        if core is None:
            return None
        rows = await self._rows_of(status, core, None)
        return [{"measurement_id": row["measurement_id"], "usage": row["usage"],
                 "usage_state": row["usage_state"]} for row in rows]

    async def operation_routing(self, ref):
        """Lab ledger routing and immutable confirmations, independent of Core results."""
        self._assert_loop()
        ref = operation_ref(ref)
        from .routing import operation_routing
        async def work():
            return await operation_routing(self, ref)
        return await self._result_work(work, self._result_io.acquire_read(ref.id, 'read'))

    async def report(self, ref):
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            durable = await self._db('operation_status', ref.id)
            from .routing import operation_routing
            routing = await operation_routing(self, ref)
            routing_fields = ({'routing': routing} if any(
                run['policy_digest'] is not None for run in routing['runs']) else {})
            if durable.results_state == 'deleted':
                return ReportResult(ref.id, 'unavailable', None, 'results_deleted',
                    starts=durable.starts, **routing_fields)
            if durable.results_state in {'deleting', 'delete_failed'}:
                return ReportResult(ref.id, 'unavailable', None, 'io_error',
                    starts=durable.starts, **routing_fields)
            # One Core acquisition per request (spec 2026-10-19 D3): the report, its status metadata (kind, starts,
            # resume target) and usage come from the same campaign read.
            acquired = (None if durable.kind == 'evaluation' else
                        await self._result_thread(acquire_campaign, self.state_directory, ref.id))
            status = await self._status_projection(ref, durable, acquired)
            if status.kind == 'evaluation':
                if status.result is not None and status.result.core is not None:
                    from evolution.tree.execution.journal_projections import Projection
                    from evolution_lab.report import build_report
                    result = status.result.to_payload()
                    # Reuse the Markdown report's builder and this request's Core projection.
                    # Coverage comes from the already-read identity; do not reopen the journal.
                    expected = ({result['core']['run_id']: result['coverage']['expected']}
                                if result['coverage'] is not None else None)
                    value = await self._result_thread(
                        functools.partial(build_report, expected_samples=expected,
                            lab_notices=await self._evaluation_notices(status, result["core"]),
                            unanswered={result['core']['run_id']: {key: result[key] for key in
                                ('unanswered', 'unanswered_state', 'answered_reported')}},
                            lab_result={key: result[key] for key in
                                        ('coverage', 'stopped_reason', 'unmeasured', 'min_coverage', 'persistence')}),
                        Projection.from_payload(result['core']))
                    return ReportResult(ref.id, 'available', value.data, None,
                        starts=status.starts, usage=await self._report_usage(status), **routing_fields)
                return ReportResult(ref.id, 'unavailable', None, 'unavailable', starts=status.starts, **routing_fields)
            from evolution_lab._reflector_probe import probe_fields
            probe = await self._reflector_probe_record(ref)
            fields = probe_fields(probe)
            try:
                value = await self._campaign_report(ref, status, acquired, reflector_probe=probe)
                return ReportResult(ref.id, 'available', value.data, None, starts=status.starts,
                                    usage=await self._report_usage(status), **fields, **routing_fields)
            except OSError:
                return ReportResult(ref.id, 'unavailable', None, 'io_error', starts=status.starts, **fields, **routing_fields)
            except Exception:
                return ReportResult(ref.id, 'unavailable', None, 'unavailable', starts=status.starts, **fields, **routing_fields)
        return await self._result_work(work,
            self._result_io.acquire_read(ref.id, 'read'))

    async def _campaign_report(self, ref, status, acquired, *, reflector_probe=None):
        """Build both report formats from the caller's leased campaign acquisition."""
        from evolution_lab.report import read_campaign_report
        from .core_raw import FAMILIES
        target = status.resume.target if status.resume is not None else None
        resume_target = None
        if target is not None:
            from .resume_rules import pick_ready_target
            payload = target.to_payload()
            try:
                listed = await self.list_targets()
                ready = pick_ready_target([t.to_payload() for t in listed], payload["excluded"])
            except Exception:
                ready = None
            resume_target = dict(payload, ready_target=ready)
        counts = (await self._unanswered_counts(status, _thaw_json(status.result.core))
                  if status.result is not None and status.result.core is not None else {})
        return await self._result_thread(
            functools.partial(read_campaign_report, owner_run=True,
                              owner_resume_target=resume_target, owner_reflector_probe=reflector_probe, families=FAMILIES,
                              read=acquired_read(acquired), unanswered=counts,
                              baseline_cache=self._baseline_provenance),
            self.state_directory / 'artifacts' / ref.id / ref.id)

    async def report_markdown(self, ref):
        """``report.md`` of an operation (spec 2026-10-14 report-markdown §2): the text ``Report.markdown()`` builds,
        with the preconditions, the lease and the reasons of ``report``; a snapshot, nothing is stored."""
        self._assert_loop()
        ref = operation_ref(ref)
        async def work():
            durable = await self._db('operation_status', ref.id)
            if durable.results_state == 'deleted':
                return ReportMarkdownResult(ref.id, 'unavailable', None, 'results_deleted')
            if durable.results_state in {'deleting', 'delete_failed'}:
                return ReportMarkdownResult(ref.id, 'unavailable', None, 'io_error')
            acquired = (None if durable.kind == 'evaluation' else
                        await self._result_thread(acquire_campaign, self.state_directory, ref.id))
            status = await self._status_projection(ref, durable, acquired)
            try:
                if status.kind == 'evaluation':
                    if status.result is None or status.result.core is None:
                        return ReportMarkdownResult(ref.id, 'unavailable', None, 'unavailable')
                    from evolution_lab.report import read_evaluation_report
                    value = await self._result_thread(
                        read_evaluation_report,
                        self.state_directory / 'artifacts' / ref.id / ref.id / 'measurement',
                        lab_notices=await self._evaluation_notices(status, _thaw_json(status.result.core)),
                        unanswered={status.result.core['run_id']: {key: getattr(status.result, key) for key in
                            ('unanswered', 'unanswered_state', 'answered_reported')}},
                        lab_result={key: status.result.to_payload()[key] for key in
                                    ('coverage', 'stopped_reason', 'unmeasured', 'min_coverage', 'persistence')})
                else:
                    value = await self._campaign_report(ref, status, acquired,
                        reflector_probe=await self._reflector_probe_record(ref))
                markdown = await self._result_thread(value.markdown)
                rows = await self._db('operation_inputs', ref.id)
                if len({row['prepared_digest'] for row in rows}) > 1:
                    markdown += '\n## Подготовленные входы по запускам\n\n' + '\n'.join(
                        f"- Запуск {row['start']}: `{row['prepared_digest']}`" for row in rows) + '\n'
                return ReportMarkdownResult(ref.id, 'available', markdown, None)
            except OSError:
                return ReportMarkdownResult(ref.id, 'unavailable', None, 'io_error')
            except Exception:
                return ReportMarkdownResult(ref.id, 'unavailable', None, 'unavailable')
        return await self._result_work(work, self._result_io.acquire_read(ref.id, 'read'))
