"""Read completeness is private; captured Core bytes and public projections stay unchanged."""
import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution_lab.owner import readers, core_raw
from evolution_lab.owner.measurements import MeasurementCache
from evolution_lab.owner.models import CleanupFacts, OperationStatus, ProcessFacts, TargetRef
from evolution_lab.owner.progress import CampaignPlan
from evolution_lab.owner.result_io import ResultIOSupport
from test_owner_store_starts import DIGEST, _executor

CAPTURE = Path(__file__).parent / 'fixtures/panel/b15/focus-open/run'


def fail(error):
    def broken(*args, **kwargs):
        raise error
    return broken


@pytest.fixture
def campaign(tmp_path):
    status = OperationStatus('1' * 32, 'generation-1', DIGEST, TargetRef('target-1', 'generation-1'),
        'running', ProcessFacts(True), CleanupFacts('pending', None), None, None, 'not_created', _executor())
    run = tmp_path / 'artifacts' / status.id / status.id
    shutil.copytree(CAPTURE, run)
    return tmp_path, status, run


@pytest.fixture
def evaluation(campaign):
    from evolution.tree.evolve import derive_run_id
    root, status, run = campaign
    shutil.copytree(run / 'measurements/m-r00-champion', run / 'measurement')
    status = replace(status, kind='evaluation', executor=_executor('lab-evaluation-worker/1'))
    scope = root / ('2' * 32)
    scope.mkdir()
    (scope / 'evaluation-outcome.json').write_text(json.dumps({
        'schema': 'lab-evaluation-outcome/3', 'operation_id': status.id, 'scope_id': scope.name,
        'run_id': derive_run_id(run), 'measurement_returned': True, 'coverage': None,
        'stopped_reason': None, 'evaluation_budget': {'max_failed_turns': 3, 'call_retries': None},
        'unmeasured': None, 'min_coverage': 1.0,
    }))
    return root, status, run, scope


class Views(readers.ReaderSupport, ResultIOSupport):
    def __init__(self, root):
        self.state_directory = root
        self._measurements = MeasurementCache()
        self._plan_cache = {DIGEST: CampaignPlan('neutral', 1, 1, 'quality')}


async def project(root, status, scope=None, owner=None):
    owner = owner or Views(root)
    completeness = readers.ReadCompleteness()
    projected = await owner._result_thread(readers.project_operation, root, status,
        scope_directory=scope, completeness=completeness)
    value = await owner._views(projected, completeness=completeness)
    return value, completeness.complete


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_clean_projection_and_old_callers(campaign, evaluation, kind):
    root, status, _, *scopes = evaluation if kind == 'evaluation' else campaign
    scope = scopes[0] if scopes else None
    value, complete = asyncio.run(project(root, status, scope))
    legacy = readers.project_operation(root, status, scope_directory=scope)
    legacy = asyncio.run(Views(root)._views(legacy))
    assert value.to_payload() == legacy.to_payload()
    assert complete
    assert readers.cacheable(value, complete)
    assert not readers.cacheable(value, False)


@pytest.mark.parametrize('acquire', [False, True])
@pytest.mark.parametrize('error_kind', ['unsupported', 'journal', 'generic'])
def test_campaign_read_exceptions(campaign, monkeypatch, error_kind, acquire):
    from evolution.tree import JournalFault
    from evolution.tree.campaign import CampaignFamilyUnsupported
    error = {'unsupported': CampaignFamilyUnsupported(13, reader=15),
             'journal': JournalFault('journal_unavailable'), 'generic': OSError('denied')}[error_kind]
    monkeypatch.setattr(core_raw, 'campaign_read', fail(error))
    root, status, _ = campaign
    if acquire:
        completeness = readers.ReadCompleteness()
        value = readers.project_operation(root, status, acquired=readers.acquire_campaign(root, status.id),
                                          completeness=completeness)
        complete = completeness.complete
    else:
        value, complete = asyncio.run(project(root, status))
    assert not complete and not readers.cacheable(value, complete)


@pytest.mark.parametrize('point', ['open', 'stat'])
@pytest.mark.parametrize('error', [FileNotFoundError(), PermissionError()])
def test_campaign_absence_probe_exceptions(campaign, monkeypatch, point, error):
    root, status, run = campaign
    completeness = readers.ReadCompleteness()
    if point == 'open':
        monkeypatch.setattr(readers, 'open_directory_nofollow', fail(error))
    else:
        real = readers.os.stat
        def stat(path, *args, **kwargs):
            if path == 'campaign.jsonl':
                raise error
            return real(path, *args, **kwargs)
        monkeypatch.setattr(readers.os, 'stat', stat)
    readers._campaign_not_created(status, run, completeness=completeness)
    assert not completeness.complete


def test_evaluation_journal_exception(evaluation, monkeypatch):
    import evolution.tree
    root, status, _, scope = evaluation
    monkeypatch.setattr(evolution.tree, 'inspect_journal', fail(OSError('denied')))
    value, complete = asyncio.run(project(root, status, scope))
    assert not complete and not readers.cacheable(value, complete)


@pytest.mark.parametrize('error', [OSError('denied'), ValueError('invalid')])
def test_outcome_defaults_are_incomplete_even_with_present_result(evaluation, monkeypatch, error):
    from evolution_lab.owner import execution
    root, status, _, scope = evaluation
    monkeypatch.setattr(execution, 'read_evaluation_outcome', fail(error))
    value, complete = asyncio.run(project(root, status, scope))
    assert value.results_state == 'present'
    assert value.result.stopped_reason == 'unknown'
    assert value.result.unmeasured is None and value.result.min_coverage == 1.0
    assert not complete and not readers.cacheable(value, complete)


@pytest.mark.parametrize('kind,point', [('evaluation', 'journal'), ('campaign', 'raw'),
    ('campaign', 'generic'), ('campaign', 'measurement')])
def test_times_read_exceptions(campaign, evaluation, monkeypatch, kind, point):
    from evolution_lab.owner import measurements
    root, status, _, *scopes = evaluation if kind == 'evaluation' else campaign
    scope = scopes[0] if scopes else None
    projected = readers.project_operation(root, status, scope_directory=scope)
    if point in {'journal', 'measurement'}:
        monkeypatch.setattr(measurements, 'read_journal_facts', fail(OSError('denied')))
    else:
        error = core_raw.CoreRawUnavailable('unreadable') if point == 'raw' else OSError('denied')
        monkeypatch.setattr(core_raw, 'campaign_journal_times', fail(error))
    completeness = readers.ReadCompleteness()
    view, _ = readers._times_and_facts(root, projected, MeasurementCache(), completeness=completeness)
    assert view['times_state'] == 'unavailable'
    assert not completeness.complete


@pytest.mark.parametrize('point', ['prepared_missing', 'prepared_invalid', 'prepared_os',
                                   'binding_owner', 'binding_preparation', 'binding_os'])
def test_plan_read_exceptions(campaign, monkeypatch, point):
    from evolution_lab.owner import binding
    from evolution_lab.owner.models import OwnerError
    from evolution_lab.preparation.models import PreparationError
    root, status, _ = campaign
    owner = Views(root)
    owner._plan_cache.clear()
    errors = {'prepared_missing': PreparationError('not_found', 'read'),
        'prepared_invalid': PreparationError('io_error', 'read'), 'prepared_os': OSError('denied'),
        'binding_owner': OwnerError('io_error', 'read'),
        'binding_preparation': PreparationError('io_error', 'read'), 'binding_os': OSError('denied')}
    owner._storage = SimpleNamespace(get_prepared=(fail(errors[point]) if point.startswith('prepared') else
        lambda _: SimpleNamespace(descriptor=SimpleNamespace(entrypoint='neutral'))))
    if point.startswith('binding'):
        monkeypatch.setattr(binding, 'open_prepared', fail(errors[point]))
    value, complete = asyncio.run(project(root, status, owner=owner))
    assert not complete and not readers.cacheable(value, complete)
    # A definitively missing plan retains its existing plan-cache behavior but must remain incomplete.
    if point == 'prepared_missing':
        _, again = asyncio.run(project(root, status, owner=owner))
        assert not again


@pytest.mark.parametrize('point', ['prefix', 'bindings', 'stages'])
def test_acquired_adapter_fallbacks(campaign, monkeypatch, point):
    root, status, _ = campaign
    monkeypatch.setattr(core_raw, {'prefix': '_family15_prefix', 'bindings': 'family15_bindings',
                                 'stages': 'family15_stage_facts'}[point], fail(OSError('denied')))
    acquired = readers.acquire_campaign(root, status.id)
    assert not isinstance(acquired, Exception)
    completeness = readers.ReadCompleteness()
    readers.project_operation(root, status, acquired=acquired, completeness=completeness)
    assert not completeness.complete


# Exact b15 public state paths; siblings is a map of dynamically named measurement/store states.
STATE_PATHS = [
    ('results_state',), ('times_state',), ('progress_state',), ('progress', 'policy_state'),
    ('result', 'core', 'siblings', 'm-r00-champion'),
    ('result', 'core', 'measurements', 0, 'classification'),
    ('result', 'core', 'lineage', 'store_state'),
    ('result', 'core', 'lineage', 'candidates', 0, 'state'),
    ('result', 'core', 'independent', 'evidence'),
    ('result', 'core', 'independent', 'probe_availability', 0, 'health'),
    ('result', 'core', 'independent', 'attempts', 0, 'comparison', 'scopes', 0, 'status'),
    ('result', 'core', 'measurements', 0, 'status', 'samples', 0, 'scoring_state'),
]


@pytest.mark.parametrize('path', STATE_PATHS)
@pytest.mark.parametrize('state', ['unavailable', 'unreadable'])
def test_nested_degraded_state(campaign, path, state):
    root, status, _ = campaign
    value, complete = asyncio.run(project(root, status))
    payload = value.to_payload()
    node = payload
    for key, following in zip(path[:-1], path[1:]):
        if isinstance(node, list):
            while len(node) <= key:
                node.append({})
        if (node.get(key) if isinstance(node, dict) else node[key]) is None:
            node[key] = [] if isinstance(following, int) else {}
        node = node[key]
    node[path[-1]] = state
    assert not readers.cacheable(payload, complete)


def test_arbitrary_text_is_not_a_state(campaign):
    root, status, _ = campaign
    value, complete = asyncio.run(project(root, status))
    payload = value.to_payload()
    payload['result']['core']['text'] = {'message': 'unavailable', 'name': 'unreadable'}
    assert readers.cacheable(payload, complete)


def test_request_signal_isolated_across_result_threads(campaign):
    root, status, _ = campaign
    async def scenario():
        owner = Views(root)
        bad, good = readers.ReadCompleteness(), readers.ReadCompleteness()
        await asyncio.gather(owner._result_thread(readers._evaluation_outcome, root / 'missing', status,
            'run-id', completeness=bad), owner._result_thread(readers.project_operation, root,
            status, completeness=good))
        return bad.complete, good.complete
    assert asyncio.run(scenario()) == (False, True)


def test_missing_scope_is_incomplete(evaluation):
    root, status, _, _ = evaluation
    value, complete = asyncio.run(project(root, status))
    assert value.result.stopped_reason == 'unknown'
    assert not complete


def test_core_pin_metadata_failure(campaign, monkeypatch):
    from importlib.metadata import PackageNotFoundError
    monkeypatch.setattr(core_raw, 'version', fail(PackageNotFoundError('evo-skills')))
    monkeypatch.setattr(core_raw, '_installed_core', core_raw._installed_core.__wrapped__)
    root, status, _ = campaign
    value, complete = asyncio.run(project(root, status))
    assert not complete and value.results_state == 'unavailable'


def test_campaign_times_adapter_inner_exception(campaign, monkeypatch):
    root, status, _ = campaign
    projected = readers.project_operation(root, status)
    monkeypatch.setattr(core_raw, 'campaign_log', fail(OSError('denied')))
    completeness = readers.ReadCompleteness()
    view, _ = readers._times_and_facts(root, projected, MeasurementCache(), completeness)
    assert not completeness.complete and view['times_reason'] == 'unreadable'


def test_stage_facts_inner_exception(campaign, monkeypatch):
    from evolution.tree import campaign_projections
    root, status, _ = campaign
    original = core_raw.family15_stage_facts
    def stage_read(*args, **kwargs):
        with monkeypatch.context() as fault:
            fault.setattr(campaign_projections, '_evidence_of', fail(OSError('denied')))
            return original(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'family15_stage_facts', stage_read)
    acquired = readers.acquire_campaign(root, status.id)
    assert acquired.bindings_reason is None and acquired.stages_reason == 'unreadable'
    completeness = readers.ReadCompleteness()
    readers.project_operation(root, status, acquired=acquired, completeness=completeness)
    assert not completeness.complete


def test_status_projection_carries_signal(evaluation, monkeypatch):
    from evolution_lab.owner import execution
    from evolution_lab.owner.models import ResourceRef
    root, status, _, scope = evaluation
    owner = Views(root)
    owner._operation_cache, owner._operations, owner._summary_cache = {}, {}, {}
    owner._projection_cache = {}
    async def db(method, ref):
        assert method == 'resource_scopes'
        return [{'root_relative': str(scope.relative_to(root))}]
    owner._db = db
    monkeypatch.setattr(execution, 'read_evaluation_outcome', fail(OSError('denied')))
    completeness = readers.ReadCompleteness()
    value = asyncio.run(owner._status_projection(ResourceRef('operation', status.id), status,
                                                 completeness=completeness))
    assert value.results_state == 'present' and not completeness.complete
