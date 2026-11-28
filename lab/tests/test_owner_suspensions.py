"""Task2 synthetic forms/errors/guards; actual b6 journals belong to Task6."""
import asyncio
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from evolution_lab.owner import Owner, OwnerError, ResourceRef, core_raw
from test_owner_delete import register, REF


ROWS = [
    ('fault_rows', {'count': 9, 'rows_digest': 'a' * 64, 'truncated': True},
     [{'measurement_id': 'm1', 'case_id': 'case', 'sample_id': 'sample', 'retry_ordinal': 2,
       'reason': 'future_reason', 'code': None}]),
    ('final_rows', {'count': 1, 'rows_digest': 'b' * 64, 'truncated': False},
     [{'measurement_id': 'm2', 'case_id': 'case', 'sample_id': 'sample', 'retry_ordinal': 0,
       'owner': 'future_owner'}]),
    ('champion_rows', {'measurement_id': 'm3', 'count': 1, 'rows_digest': 'c' * 64, 'truncated': False},
     [{'measurement_id': 'm3', 'case_id': 'case', 'sample_id': 'sample', 'retry_ordinal': 1,
       'owner': 'target'}]),
    ('threshold', {'crossing': {'owner': 'target', 'kind': 'future_kind', 'count': 2,
       'denominator': 3, 'max_count': 1, 'max_rate': '0.5'}, 'window_start': None}, None),
    ('stage_record', {'seq': 2, 'operation_id': None}, None),
    ('stage_record', {'seq': 2, 'operation_id': 'operation2',
       'prefix': 'measurements/m4/events.jsonl#bytes=0:129'}, None),
]


def raw_entry(kind, evidence, rows, seq=3):
    metadata = deepcopy(evidence)
    metadata['evidence_kind'] = kind
    if rows is not None:
        metadata['rows'] = deepcopy(rows)
    return {'seq': seq, 'reason': 'future_code', 'owner': 'target', 'evidence_kind': kind,
            'evidence': metadata, 'version': '2.0.0b6'}


def reduced(monkeypatch, tmp_path, entries, *, integrity='valid', identity=True, family=14):
    """Only replace the Core inspection/reducer boundary; exercise real Lab projection."""
    (tmp_path / 'campaign.jsonl').write_text('Core boundary double\n')
    log = SimpleNamespace(records=(object(),), journal_integrity=integrity)
    monkeypatch.setattr(core_raw, 'campaign_log', lambda run, **kwargs: log)
    monkeypatch.setattr(core_raw, '_journal_family', lambda run: family)
    snapshot = SimpleNamespace(identity={'run_id': 'run'} if identity else None,
                               family_version=family, suspensions=entries)
    monkeypatch.setattr('evolution.tree.campaign_schema.reduce', lambda *a, **kw: snapshot)
    return log


@pytest.mark.parametrize('kind,evidence,rows', ROWS)
def test_adapter_projects_each_closed_shape(monkeypatch, tmp_path, kind, evidence, rows):
    reduced(monkeypatch, tmp_path, [raw_entry(kind, evidence, rows)])
    assert core_raw.campaign_suspensions(tmp_path) == [{
        'seq': 3, 'reason': 'future_code', 'owner': 'target', 'evidence_kind': kind,
        'evidence': evidence, 'rows': rows}]
    # prefix is an opaque locator; its named measurement is intentionally absent.
    assert not (tmp_path / 'measurements').exists()


def test_history_is_in_core_sequence_order_and_incomplete_tail_is_usable(monkeypatch, tmp_path):
    entries = [raw_entry(*ROWS[0], seq=3), raw_entry(*ROWS[4], seq=8)]
    reduced(monkeypatch, tmp_path, entries, integrity='incomplete_tail')
    assert [row['seq'] for row in core_raw.campaign_suspensions(tmp_path)] == [3, 8]


@pytest.mark.parametrize('mutation', ['unknown_kind', 'cohort_rows', 'extra_evidence',
    'missing_evidence', 'extra_row', 'missing_row', 'extra_crossing', 'nested_private', 'kind_mismatch'])
def test_unknown_structure_atomically_refuses_history(monkeypatch, tmp_path, mutation):
    bad = raw_entry(*ROWS[0], seq=9)
    if mutation in {'unknown_kind', 'cohort_rows'}:
        bad['evidence_kind'] = bad['evidence']['evidence_kind'] = mutation
    elif mutation == 'extra_evidence':
        bad['evidence']['diagnostics'] = 'PRIVATE'
    elif mutation == 'missing_evidence':
        del bad['evidence']['count']
    elif mutation == 'extra_row':
        bad['evidence']['rows'][0]['oracle'] = 'PRIVATE'
    elif mutation == 'missing_row':
        del bad['evidence']['rows'][0]['code']
    elif mutation == 'extra_crossing':
        bad = raw_entry(*ROWS[3], seq=9)
        bad['evidence']['crossing']['details'] = 'PRIVATE'
    elif mutation == 'nested_private':
        bad['evidence']['rows'][0]['code'] = {'diagnostics': 'PRIVATE'}
    else:
        bad['evidence']['evidence_kind'] = 'final_rows'
    reduced(monkeypatch, tmp_path, [raw_entry(*ROWS[4]), bad])
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(tmp_path)
    assert caught.value.code == 'unsupported_schema'


@pytest.mark.parametrize('integrity,identity,family,reason', [
    ('corrupt_suffix', True, 14, 'campaign_unreadable'),
    ('incomplete_tail', False, 14, 'campaign_unreadable'),
    ('valid', True, 13, 'campaign_family_unsupported')])
def test_invalid_journal_facts_are_unavailable(monkeypatch, tmp_path, integrity, identity, family, reason):
    reduced(monkeypatch, tmp_path, [], integrity=integrity, identity=identity, family=family)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(tmp_path)
    assert caught.value.code == reason


@pytest.mark.parametrize('content', [b'', b'broken\n', b'{"event":"campaign_created"'])
def test_real_unreadable_journal_is_not_empty_success(tmp_path, content):
    (tmp_path / 'campaign.jsonl').write_bytes(content)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(tmp_path)
    assert caught.value.code == 'campaign_unreadable'


def test_actual_old_family_is_typed_unavailable():
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(Path(__file__).parent / 'fixtures/campaign-family-refusal')
    assert caught.value.code == 'campaign_family_unsupported'


def test_missing_and_wrong_pin_are_distinct(monkeypatch, tmp_path):
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(tmp_path)
    assert caught.value.code == 'campaign_missing'
    monkeypatch.setattr(core_raw, '_installed_core', lambda: 'wrong')
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_suspensions(tmp_path)
    assert caught.value.code == 'core_raw_unsupported'


@pytest.mark.parametrize('boundary', ['inspection', 'reducer'])
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError, KeyboardInterrupt])
def test_only_core_exceptions_are_mapped(monkeypatch, tmp_path, boundary, error_type):
    reduced(monkeypatch, tmp_path, [])
    def fail(*args, **kwargs):
        raise error_type('private details')
    if boundary == 'inspection':
        monkeypatch.setattr(core_raw, 'campaign_log', fail)
    else:
        monkeypatch.setattr('evolution.tree.campaign_schema.reduce', fail)
    expected = core_raw.CoreRawUnavailable if error_type is RuntimeError else error_type
    with pytest.raises(expected) as caught:
        core_raw.campaign_suspensions(tmp_path)
    if error_type is RuntimeError:
        assert caught.value.code == 'campaign_unreadable'
        assert 'private' not in str(caught.value)


def test_lab_projection_programmer_error_propagates(monkeypatch, tmp_path):
    from evolution_lab.owner import suspension_evidence
    reduced(monkeypatch, tmp_path, [])
    def fail(*args):
        raise TypeError('projection bug')
    monkeypatch.setattr(suspension_evidence, 'project_suspensions', fail)
    with pytest.raises(TypeError, match='projection bug'):
        core_raw.campaign_suspensions(tmp_path)


@pytest.mark.parametrize('kind', ['campaign', 'evaluation', 'conformance'])
def test_owner_does_not_require_existing_projection_and_checks_kind_before_pin(tmp_path, monkeypatch, kind):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner, kind='evaluation' if kind == 'conformance' else kind)
            with monkeypatch.context() as patch:
                if kind == 'conformance':
                    # Boundary-only coverage: current conformance is a TaskStatus,
                    # never an admitted operation. Do not invent a live OperationRef.
                    durable = await owner._db('operation_status', REF.id)
                    async def conformance_boundary(method, operation_id):
                        assert method == 'operation_status' and operation_id == REF.id
                        return SimpleNamespace(kind='conformance', results_state=durable.results_state)
                    patch.setattr(owner, '_db', conformance_boundary)
                patch.setattr(core_raw, '_installed_core', lambda: 'wrong')
                value = await owner.suspensions(REF)
                assert value.to_payload() == {
                    'schema': 'lab-suspension-evidence/1', 'operation_id': REF.id,
                    'state': 'unavailable', 'source': 'core_raw',
                    'reason': 'core_raw_unsupported' if kind == 'campaign' else 'not_campaign',
                    'suspensions': None}
            if kind == 'campaign':
                assert (await owner.suspensions(REF)).reason == 'campaign_missing'
    asyncio.run(scenario())


def test_persistence_secret_fault_is_not_a_suspension_for_owner_resume(tmp_path, monkeypatch):
    """PDR-0015: a readable, incomplete persistence fault is not campaign_suspended."""
    from evolution_lab.owner import OperationResult, ProcessFacts

    core = {'complete': False, 'suspended': False, 'suspensions': [], 'campaign_outcome': None}

    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            operation = await register(owner)
            result = OperationResult(operation.status.prepared_digest, ProcessFacts(exit_code=1),
                                     kind='campaign', core=core)
            await owner._db('update_operation', REF.id, result=result, results_state='present')
            # Fresh public-reader boundary, not a fabricated Core journal. The terminal/code are
            # worker process-log facts, not a new suspension or new OperationResult fields.
            def read_core(*args, **kwargs):
                return core
            monkeypatch.setattr('evolution_lab.owner.execution._fresh_core', read_core)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(REF, target_ref=operation.status.target_ref, secrets={})
            assert caught.value.code == 'campaign_not_suspended'
            current = await owner._db('operation_status', REF.id)
            assert len(current.starts) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['core_raw_unsupported', 'campaign_family_unsupported',
    'campaign_missing', 'campaign_unreadable', 'unsupported_schema'])
def test_owner_maps_adapter_unavailability_without_partial_history(tmp_path, monkeypatch, reason):
    def fail(*args):
        raise core_raw.CoreRawUnavailable(reason)
    monkeypatch.setattr(core_raw, 'campaign_suspensions', fail, raising=False)
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            value = await owner.suspensions(REF)
            assert value.state == 'unavailable' and value.reason == reason
            assert value.suspensions is None and value.to_payload()['source'] == 'core_raw'
    asyncio.run(scenario())


def test_owner_available_empty_and_full_history(tmp_path, monkeypatch):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            run = tmp_path / 'state' / 'artifacts' / REF.id / REF.id
            run.mkdir(parents=True)
            entries = []
            reduced(monkeypatch, run, entries)
            assert (await owner.suspensions(REF)).to_payload()['suspensions'] == []
            entries.append(raw_entry(*ROWS[0]))
            payload = (await owner.suspensions(REF)).to_payload()
            assert payload['state'] == 'available' and payload['reason'] is None
            assert payload['suspensions'][0]['rows'][0]['code'] is None
            assert set(payload['suspensions'][0]['evidence']) == {'count', 'rows_digest', 'truncated'}
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
@pytest.mark.parametrize('state,code', [('deleted', 'results_deleted'), ('deleting', 'not_found'),
                                     ('delete_failed', 'not_found')])
def test_delete_state_precedes_kind_and_pin(tmp_path, monkeypatch, kind, state, code):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner, kind=kind)
            await owner._db('update_operation', REF.id, results_state=state)
            monkeypatch.setattr(core_raw, '_installed_core', lambda: 'wrong')
            with pytest.raises(OwnerError) as caught:
                await owner.suspensions(REF)
            assert caught.value.code == code
    asyncio.run(scenario())


def test_unknown_operation_uses_durable_lookup(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            with pytest.raises(OwnerError) as caught:
                await owner.suspensions(ResourceRef('operation', 'unknown'))
            assert caught.value.code == 'not_found'
    asyncio.run(scenario())


def test_cancellation_keeps_read_lease_until_io_ends_and_deleted_results_stay_unavailable(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def read(*args):
        entered.set()
        assert release.wait(5), 'test worker release timed out'
        return []
    monkeypatch.setattr(core_raw, 'campaign_suspensions', read, raising=False)
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            task = asyncio.create_task(owner.suspensions(REF))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(OwnerError) as caught:
                    await owner.delete_results(REF)
                assert caught.value.code == 'resource_busy'
            finally:
                release.set()
                await owner._drain_result_work()
            assert (await owner.wait(await owner.delete_results(REF))).state == 'succeeded'
            with pytest.raises(OwnerError) as caught:
                await owner.suspensions(REF)
            assert caught.value.code == 'results_deleted'
    asyncio.run(scenario())
