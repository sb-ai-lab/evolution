"""Read-only fixed-evaluation candidates for one Bundle (ADR-0028)."""
from __future__ import annotations

import hashlib

from evolution.tree import ManifestError, load_manifest_v2
from evolution.tree.execution.corpus import CorpusError

from evolution_lab._manifest_schema import campaign_cases

from evolution_lab.preparation import basis
from evolution_lab.preparation.models import PreparationError
from evolution_lab.preparation.storage import canonical_json_bytes

from .models import ResourceRef, _thaw_json


def _split(bundle, plan):
    if bundle.descriptor.source_kind != 'campaign':
        return None
    # This is the same public corpus partition used at preparation. The plan
    # freezes selected IDs, so recover its split from those IDs, never its name.
    try:
        manifest = load_manifest_v2(bundle.path / bundle.descriptor.entrypoint)
        selected = set(plan.case_ids)
        matches = []
        for name in ('train', 'test'):
            try:
                cases = campaign_cases(manifest, name)
            except CorpusError as error:
                if error.code != 'empty_split':
                    raise
                cases = ()
            if selected <= {case.case_id for case in cases}:
                matches.append(name)
        if len(matches) != 1:
            raise ValueError('ambiguous saved evaluation split')
        return matches[0]
    except (ManifestError, OSError, ValueError, AttributeError):
        raise PreparationError('artifacts_invalid', 'read') from None


def candidates(storage, bundle_digest, operations):
    """Read saved descriptors and inputs; failures retain preparation's boundary."""
    requested = storage.get_bundle(bundle_digest)
    identity = basis.measurement_basis(requested)
    rows = []
    if identity is None:
        return identity, rows
    for operation in operations:
        if operation.kind != 'evaluation':
            continue
        prepared = storage.get_prepared(operation.prepared_digest).descriptor
        if prepared.kind != 'evaluation' or prepared.evaluation is None:
            raise PreparationError('artifacts_invalid', 'read')
        bundle = storage.get_bundle(prepared.bundle_digest)
        if basis.measurement_basis(bundle) != identity:
            continue
        plan = prepared.evaluation
        payload = plan.to_payload()
        conditions = {key: getattr(plan, key) for key in (
            'evaluation_set_id', 'max_turns', 'timeout_seconds', 'concurrency',
            'scorer_concurrency', 'sample_retries', 'min_coverage', 'max_failed_turns',
        )}
        conditions.update(
            split=_split(bundle, plan), case_count=len(plan.case_ids),
            case_ids_digest=hashlib.sha256(canonical_json_bytes(list(plan.case_ids))).hexdigest(),
            repeat_seeds=list(plan.repeat_seeds),
            metric_plan_digest=hashlib.sha256(canonical_json_bytes(payload['metric_plan'])).hexdigest(),
            call_retries=payload['call_retries'],
            max_consecutive_unmeasured=plan.max_consecutive_unmeasured,
        )
        rows.append((operation.id, prepared.bundle_digest, conditions))
    return identity, rows


def item(status, bundle_digest, requested_digest, conditions):
    result = status.result
    core = result.core if result is not None else None
    outcome = None if result is None else {
        'complete': None if core is None else core.get('complete'),
        'coverage': None if result.coverage is None else result.coverage.to_payload(),
        'stopped_reason': result.stopped_reason,
        'unmeasured': _thaw_json(result.unmeasured), 'min_coverage': result.min_coverage,
    }
    return {
        'operation_id': status.id, 'prepared_digest': status.prepared_digest,
        'bundle_digest': bundle_digest, 'same_bundle': bundle_digest == requested_digest,
        'control_state': status.control_state, 'results_state': status.results_state,
        'failure': None if status.failure is None else status.failure.to_payload(),
        'times': _thaw_json(status.times), 'outcome': outcome, 'plan': conditions,
    }


def ordered(rows):
    """Public journal times proxy start/finish; no finish timestamp is invented.

    Missing times sort after known times within each group; ID breaks ties.
    """
    def time(row):
        active = row['control_state'] in {'reserved', 'running', 'stopping'}
        return (row['times'] or {}).get('first_event_at' if active else 'last_event_at') or ''
    rows.sort(key=lambda row: row['operation_id'])
    rows.sort(key=time, reverse=True)
    rows.sort(key=lambda row: row['control_state'] in {'reserved', 'running', 'stopping'})
    return rows


async def read(owner, bundle_digest):
    operations = await owner._db('list_operations')
    identity, selected = await owner._result_thread(candidates, owner._storage, bundle_digest, operations)
    rows = []
    for operation_id, source_digest, conditions in selected:
        status = await owner.status(ResourceRef('operation', operation_id))
        rows.append(item(status, source_digest, bundle_digest, conditions))
    return {'schema': 'lab-bundle-measurements/1', 'bundle_digest': bundle_digest,
            'measurement_basis': identity, 'items': ordered(rows)}
