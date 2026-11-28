"""ADR-0028: read measurements by saved Bundle inputs and actual plan conditions."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from evolution_lab.owner.models import Coverage, OperationResult, ProcessFacts
from evolution_lab.owner.readers import ReaderSupport
from evolution_lab.preparation import PreparationStore
from evolution_lab.preparation.models import PreparationError
from test_preparation_api import _request

pytestmark = pytest.mark.slow


class _Reader(ReaderSupport):
    """Keep actual storage and projection code; isolate the threaded Owner runtime."""
    def __init__(self, storage, statuses):
        self._storage, self.statuses = storage, statuses

    def _assert_loop(self):
        pass

    async def _db(self, method):
        assert method == 'list_operations'
        return self.statuses

    async def _result_thread(self, function, *args):
        return function(*args)

    async def status(self, ref):
        return next(row for row in self.statuses if row.id == ref.id)


def _operation(prepared, id='evaluation', *, state='finished', first='2026-10-31T01:00:00Z',
               last='2026-10-31T02:00:00Z', scored=1):
    return SimpleNamespace(id=id, kind='evaluation', prepared_digest=prepared.digest,
        control_state=state, results_state='present', failure=None,
        times=None if first is None else {'first_event_at': first, 'last_event_at': last},
        result=OperationResult(prepared.digest, ProcessFacts(True, 0, True, False),
            core={'complete': scored == 1}, coverage=Coverage(1, scored),
            stopped_reason=None, unmeasured=[], min_coverage=0.8, kind='evaluation'))


def _campaign(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    root = request["source"].manifest_path.parent
    store = PreparationStore(tmp_path / 'state')
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(bundle.digest, selection={'count': 1}, repeat_seeds=[17, 29],
        evaluation_set_id='selected', max_turns=4, timeout_seconds=12,
        metric_plan={'score': 'mean'}, max_failed_turns=2,
        call_retries={'max_attempts': 3, 'turn_max_attempts': 2},
        sample_retries=2, min_coverage=0.8)
    return store, root, request, bundle, prepared


def _read(store, digest, statuses):
    return asyncio.run(_Reader(store._storage, statuses).bundle_measurements(digest))


def test_same_and_other_bundle_use_saved_plan_without_eligibility_filter(tmp_path, monkeypatch):
    store, root, request, original, prepared = _campaign(tmp_path, monkeypatch)
    manifest = root / 'manifest.json'
    raw = json.loads(manifest.read_text()); raw['campaign']['rounds'] = 7
    raw['campaign']['sample_retries'] = 4; raw['campaign']['parallel_attempts'] = 2; raw['campaign']['attempts_per_round'] = 2; raw['campaign']['round_stop'] = 'all_attempts'; raw['campaign']['attempt_feedback'] = 'none'
    manifest.write_text(json.dumps(raw))
    launch = root / 'launch.json'; raw = json.loads(launch.read_text())
    raw['limits']['max_instances'] = 20; launch.write_text(json.dumps(raw))
    other = store.create_bundle(**request)
    status = _operation(prepared, scored=0)
    same = _read(store, original.digest, [status])
    assert same['schema'] == 'lab-bundle-measurements/1'
    item, = same['items']
    assert item['same_bundle'] is True
    assert item['bundle_digest'] == original.digest
    assert item['operation_id'] == 'evaluation'
    assert item['prepared_digest'] == prepared.digest
    assert item['control_state'] == 'finished'
    assert item['outcome']['complete'] is False
    assert item['outcome']['coverage'] == {'expected': 1, 'scored': 0}
    ids = list(prepared.evaluation.case_ids)
    assert item['plan'] == {
        'evaluation_set_id': 'selected', 'split': 'train',
        'case_ids_digest': hashlib.sha256(json.dumps(ids, separators=(',', ':'), sort_keys=True).encode()).hexdigest(),
        'case_count': len(ids), 'repeat_seeds': [17, 29], 'max_turns': 4,
        'timeout_seconds': 12, 'metric_plan_digest': hashlib.sha256(b'{"score":"mean"}').hexdigest(),
        'concurrency': 1, 'scorer_concurrency': 1, 'sample_retries': 2, 'min_coverage': 0.8,
        'max_consecutive_unmeasured': 3, 'call_retries': {'max_attempts': 3, 'turn_max_attempts': 2},
        'max_failed_turns': 2,
    }
    reused = _read(store, other.digest, [status])
    assert reused['measurement_basis'] == same['measurement_basis']
    assert reused['items'] == [{**item, 'same_bundle': False}]
    raw = json.loads(launch.read_text()); raw['limits']['max_output_bytes'] = 8000
    launch.write_text(json.dumps(raw)); different = store.create_bundle(**request)
    assert _read(store, different.digest, [status])['items'] == []
    assert _read(store, other.digest, [SimpleNamespace(kind='campaign')])['items'] == []


def test_finished_first_newest_then_unfinished_by_start_time(tmp_path, monkeypatch):
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    statuses = [
        _operation(prepared, 'running-old', state='running', first='2026-10-31T03:00:00Z'),
        _operation(prepared, 'old'),
        _operation(prepared, 'running-new', state='running', first='2026-10-31T04:00:00Z'),
        _operation(prepared, 'new', last='2026-10-31T05:00:00Z'),
    ]
    payload = _read(store, bundle.digest, statuses)
    assert [item['operation_id'] for item in payload['items']] == ['new', 'old', 'running-new', 'running-old']
    assert payload['items'][-1]['control_state'] == 'running'


def test_unknown_bundle_keeps_preparation_get_bundle_refusal(tmp_path, monkeypatch):
    store, _, _, _, _ = _campaign(tmp_path, monkeypatch)
    with pytest.raises(PreparationError) as expected:
        store.get_bundle('f' * 64)
    with pytest.raises(PreparationError) as actual:
        _read(store, 'f' * 64, [])
    assert (actual.value.code, actual.value.stage) == (expected.value.code, expected.value.stage)


def test_null_basis_never_matches_null_candidates(tmp_path, monkeypatch):
    import evolution_lab.preparation.basis as basis
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(basis, 'measurement_basis', lambda bundle: None)
    assert _read(store, bundle.digest, [_operation(prepared)]) == {
        'schema': 'lab-bundle-measurements/1', 'bundle_digest': bundle.digest,
        'measurement_basis': None, 'items': [],
    }


def test_unloadable_candidate_fails_closed(tmp_path, monkeypatch):
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    missing = SimpleNamespace(digest='f' * 64)
    with pytest.raises(PreparationError):
        _read(store, bundle.digest, [_operation(missing)])


def test_cli_measurements_read_maps_to_closed_form():
    from test_service_cli import FakeClient, invoke, DIGEST
    payload = {'schema': 'lab-bundle-measurements/1', 'bundle_digest': DIGEST,
               'measurement_basis': None, 'items': []}
    client = FakeClient([payload])
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', '--json', 'bundle', 'measurements', DIGEST], client)
    assert code == 0, stderr
    assert json.loads(stdout) == payload
    assert client.calls == [('GET', f'/v1/bundles/{DIGEST}/measurements', None, False, 'lab-bundle-measurements/1')]


def test_response_validator_accepts_saved_conditions_and_rejects_extra_or_invalid_fields(tmp_path, monkeypatch):
    from evolution_lab.cli.responses import validate
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    payload = _read(store, bundle.digest, [_operation(prepared)])
    assert validate(payload, 'lab-bundle-measurements/1') == payload
    for path, value in [
        (('extra',), True), (('items', 0, 'plan', 'extra'), True),
        (('items', 0, 'plan', 'case_count'), True),
        (('items', 0, 'plan', 'max_consecutive_unmeasured'), 0),
        (('items', 0, 'plan', 'case_ids_digest'), 'bad'),
        (('items', 0, 'same_bundle'), 'yes'),
        (('items', 0, 'outcome', 'complete'), 'yes'),
        (('items', 0, 'outcome', 'coverage', 'scored'), 2),
    ]:
        broken = json.loads(json.dumps(payload)); section = broken
        for key in path[:-1]:
            section = section[key]
        section[path[-1]] = value
        with pytest.raises(ValueError):
            validate(broken, 'lab-bundle-measurements/1')


async def test_http_measurements_uses_owner_and_preserves_read_refusals(tmp_path, monkeypatch):
    import httpx
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / 's.sock', tmp_path / 'service'))
    runtime.owner = _Reader(store._storage, [_operation(prepared)])
    runtime._admission_open = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        endpoint = f'/v1/bundles/{bundle.digest}/measurements'
        response = await client.get(endpoint)
        assert response.status_code == 200
        assert response.json() == await runtime.owner.bundle_measurements(bundle.digest)
        missing = await client.get(f'/v1/bundles/{"f" * 64}/measurements')
        assert missing.status_code == 404
        assert missing.json()['error']['code'] == 'not_found'
        for suffix in ('?extra=1', '?split=train'):
            assert (await client.get(endpoint + suffix)).status_code == 400
        assert (await client.request('GET', endpoint, content=b'{}')).status_code == 400
        assert (await client.get(endpoint, headers={'Lab-Request-Id': 'test'})).status_code == 400
        assert (await client.get('/v1/bundles/invalid/measurements')).status_code == 400


def test_fake_target_evaluation_is_readable_on_campaign_only_changed_bundle(tmp_path, monkeypatch):
    """Execute a real fixed evaluation; only the local reference target is used."""
    from pathlib import Path
    import sys
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import CampaignSource, EnvironmentInput, SourceRoot
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    from test_owner_evaluation import _prepared, _ready_target, _finished

    state, _, _, score_log = _prepared(tmp_path, monkeypatch)
    root = tmp_path / 'evaluation-source'
    manifest_path, launch_path = root / 'manifest.json', root / 'launch.json'
    evaluation = json.loads((root / 'evaluation-input.json').read_text())
    manifest = json.loads(manifest_path.read_text())
    manifest['target'] = evaluation['target']
    manifest['environment'] = evaluation['environment']
    manifest['corpus'] = evaluation['corpus']
    manifest['scorer'].update({name: evaluation['scorer'][name] for name in (
        'argv', 'cwd', 'dependency_paths', 'allowed_env', 'timeout_seconds')})
    for role in ('scorer', 'editor', 'reflector'):
        manifest[role]['argv'][0] = sys.executable
    manifest_path.write_text(json.dumps(manifest))
    launch = json.loads(launch_path.read_text()); launch['policy'] = 'manifest.json'
    launch['limits']['max_instances'] = 4; launch_path.write_text(json.dumps(launch))
    request = dict(source=CampaignSource(manifest_path, launch_path),
        environments=(EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL)),),
        roots=(SourceRoot('project', root),),
        env={'MODE': 'offline', 'SCORER_LOG': str(score_log), 'SLOW_SCORE': 'no'},
        secret_names=('SCORER_TOKEN', 'TARGET_TOKEN'))
    store = PreparationStore(state)
    x = store.create_bundle(**request)
    prepared = store.prepare_evaluation(x.digest, selection={'case_ids': ['c-3', 'c-1']},
        repeat_seeds=[17, 29], evaluation_set_id='cross-bundle', max_turns=4,
        timeout_seconds=9, metric_plan={'primary': 'score'}, sample_retries=0,
        min_coverage=1, max_failed_turns=1, call_retries='off')

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, x)
            accepted = await owner.start(prepared.digest, target_ref=target,
                secrets={'SCORER_TOKEN': 'private-neutral-scorer'})
            status = await _finished(owner, ResourceRef('operation', accepted.id))
            assert status.control_state == 'finished' and status.process.exit_code == 0
            on_x = await owner.bundle_measurements(x.digest)
            row, = on_x['items']
            assert row['outcome']['complete'] is True
            assert row['outcome']['coverage'] == {'expected': 4, 'scored': 4}
            assert row['plan']['sample_retries'] == 0
            assert row['plan']['split'] == 'train'
            assert row['plan']['call_retries'] is None
            manifest['campaign'].update(sample_retries=3, rounds=7, parallel_attempts=2,
                attempts_per_round=2, round_stop='all_attempts', attempt_feedback='none')
            manifest_path.write_text(json.dumps(manifest))
            launch['limits']['max_instances'] = 20; launch_path.write_text(json.dumps(launch))
            y = store.create_bundle(**request)
            on_y = await owner.bundle_measurements(y.digest)
            assert on_y['measurement_basis'] == on_x['measurement_basis']
            assert on_y['items'] == [{**row, 'same_bundle': False}]
            launch['limits']['max_output_bytes'] //= 2; launch_path.write_text(json.dumps(launch))
            z = store.create_bundle(**request)
            assert (await owner.bundle_measurements(z.digest))['items'] == []
    asyncio.run(scenario())


def test_evaluation_bundle_has_no_split_and_preserves_order_and_explicit_parallelism(tmp_path, monkeypatch):
    from test_preparation_evaluation import _evaluation_request
    request = _evaluation_request(tmp_path, monkeypatch)
    launch_path = request['source'].launch_path
    launch = json.loads(launch_path.read_text())
    launch['limits']['max_concurrent_sessions'] = 3
    launch['limits']['max_instances'] = 3
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / 'state')
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(bundle.digest, selection={'case_ids': ['c-3', 'c-1']},
        concurrency=3, scorer_concurrency=2, call_retries='off', sample_retries=0, min_coverage=1)
    reversed_plan = store.prepare_evaluation(bundle.digest, selection={'case_ids': ['c-1', 'c-3']},
        concurrency=3, scorer_concurrency=2, call_retries='off', sample_retries=0, min_coverage=1)
    first, second = _read(store, bundle.digest, [_operation(prepared, 'a'), _operation(reversed_plan, 'b')])['items']
    assert first['plan']['split'] is None
    assert first['plan']['case_ids_digest'] == hashlib.sha256(b'["c-3","c-1"]').hexdigest()
    assert second['plan']['case_ids_digest'] == hashlib.sha256(b'["c-1","c-3"]').hexdigest()
    assert first['plan']['concurrency'] == 3
    assert first['plan']['scorer_concurrency'] == 2
    assert first['plan']['call_retries'] is None
    assert first['plan']['sample_retries'] == 0


def test_campaign_test_split_is_derived_from_selected_case_ids(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    root = request['source'].manifest_path.parent
    corpus_path = root / 'corpus.jsonl'
    cases = [json.loads(line) for line in corpus_path.read_text().splitlines()]
    for index, case in enumerate(cases):
        case['split'] = 'train' if index == 0 else 'test'
        case['group_id'] = f'group-{index}'
    corpus_path.write_text(''.join(json.dumps(case) + '\n' for case in cases))
    store = PreparationStore(tmp_path / 'state')
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(bundle.digest, selection={'all': True}, split='test',
        repeat_seeds=[17], evaluation_set_id='not-a-split-name', max_turns=1,
        timeout_seconds=2, metric_plan={'score': 'mean'}, max_failed_turns=1, call_retries='off')
    row, = _read(store, bundle.digest, [_operation(prepared)])['items']
    assert row['plan']['split'] == 'test'
    assert row['plan']['case_count'] == len(cases) - 1


def test_cli_text_lists_candidate_state_source_and_saved_conditions(tmp_path, monkeypatch):
    from test_service_cli import FakeClient, invoke
    store, _, _, bundle, prepared = _campaign(tmp_path, monkeypatch)
    payload = _read(store, bundle.digest, [_operation(prepared)])
    payload['items'][0]['plan']['evaluation_set_id'] = 'fixed\nset'
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', 'bundle', 'measurements', bundle.digest], FakeClient([payload]))
    assert code == 0, stderr
    lines = stdout.splitlines()
    assert f'bundle={bundle.digest}' in lines[0]
    assert f'measurement_basis={payload["measurement_basis"]}' in lines[0]
    assert 'operation_id=evaluation' in lines[1]
    assert f'bundle_digest={bundle.digest}' in lines[1]
    assert 'same_bundle=true' in lines[1] and 'control_state=finished' in lines[1]
    assert 'complete=true' in lines[1] and 'coverage=1/1' in lines[1]
    assert 'evaluation_set_id="fixed\\nset"' in lines[2]
    assert 'repeat_seeds=[17,29]' in lines[2]
    assert 'sample_retries=2' in lines[2] and 'max_failed_turns=2' in lines[2]
    assert len(lines) == 3
