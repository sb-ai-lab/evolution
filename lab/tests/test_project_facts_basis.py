"""Project facts use the original measurement basis, never an accepted state's basis."""
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from evolution_lab.owner.models import ClientTask
from evolution_lab.projects.facts import compute_facts
from evolution_lab.projects.session import client_ref
from test_projects_facts import MEASURED, by_id, ctx, evaluation, task
from test_projects_facts import environment_facts  # noqa: F401
from test_projects_session import session_env  # noqa: F401

pytestmark = pytest.mark.slow


@pytest.fixture
def basis_session(environment_facts):
    session, projects, owner, entry, request, *_ = environment_facts
    async def inline(function):
        return function()
    session._run_io = inline  # real gathering/storage, synthetic capture/Owner only
    return session, projects, owner, entry, request


async def _facts(session):
    return by_id((await session.facts('acq'))[1])


async def _check(env):
    session, _, owner, *_ = env
    bundle, link = await session.bundle('acq')
    await session.start_target('acq', None, bundle_digest=bundle.digest, secrets={})
    target = owner.targets[-1][0]
    for action in ('check', 'conformance'):
        owner.tasks.append(ClientTask(f'{action}-{target}', action, 'succeeded', target,
                                      bundle.digest, client_ref('acq', 'agent')))
    return bundle, link


async def _measured(env):
    session, _, owner, *_ = env
    bundle, link = await _check(env)
    prepared, _ = await session.prepare('acq', None, bundle_digest=bundle.digest, kind='evaluation', params={
        'selection': {'count': 1}, 'repeat_seeds': [17], 'evaluation_set_id': 'facts-basis',
        'max_turns': 4, 'timeout_seconds': 12, 'metric_plan': {'score': 'mean'},
        'max_failed_turns': 1, 'call_retries': 'off',
    })
    status, _ = await session.start('acq', None, prepared_digest=prepared.digest, target_ref=object(), secrets={})
    owner.statuses[status.id].control_state = 'finished'
    owner.statuses[status.id].result = MEASURED
    assert (await _facts(session))['connected'].holds
    return bundle, link


def _edit(env, *, scorer=False):
    path = env[3].parent / env[4]['source']['manifest_path']
    raw = json.loads(path.read_text())
    if scorer:
        raw['scorer']['timeout_seconds'] += 1
    else:
        raw['campaign']['sample_retries'] = raw['campaign'].get('sample_retries', 0) + 1
    path.write_text(json.dumps(raw))


async def test_campaign_edit_reuses_original_basis_after_new_check(basis_session):
    env = basis_session
    original, _ = await _measured(env)
    _edit(env)
    assert not (await _facts(env[0]))['baseline.measured'].holds  # no current Bundle yet
    current, _ = await _check(env)
    facts = await _facts(env[0])
    assert original.digest != current.digest
    assert facts['baseline.measured'].holds and facts['connected'].holds
    source = facts['baseline.measured'].source
    assert source['bundle_digest'] == original.digest
    assert source['measurement_basis'] == original.measurement_basis
    assert source['same_bundle'] is False


@pytest.mark.parametrize('change', ['env', 'secret_names', 'path_bindings'])
@pytest.mark.parametrize('changed_materials', [False, True])
async def test_request_metadata_outside_roots_only_disables_basis_reuse(basis_session, change, changed_materials):
    env = basis_session
    original, original_link = await _measured(env)
    current_link = original_link
    if changed_materials:
        _edit(env)
        _, current_link = await _check(env)
        facts = await _facts(env[0])
        assert facts['baseline.measured'].holds and facts['connected'].holds
        assert current_link.binding['inputs']['digest'] != original_link.binding['inputs']['digest']
    request = env[4]
    if change == 'env':
        request['env']['MODE'] = 'changed'
    elif change == 'secret_names':
        request['secret_names'].append('SECOND_TOKEN')
    else:
        request['path_bindings'] = [{'document': request['source']['manifest_path'],
            'pointer': '/seed_root', 'root_alias': 'agent', 'relative_path': 'a.py'}]
    env[3].write_text(json.dumps(request))
    from evolution_lab.projects.package import inputs_inventory, read_package_entry
    digest = inputs_inventory(read_package_entry(env[3]), working_copy=Path(env[1].get('acq').working_copy.path))[0]
    assert digest == current_link.binding['inputs']['digest']  # request bytes are outside roots
    facts = await _facts(env[0])
    baseline = facts['baseline.measured']
    assert baseline.holds is (not changed_materials)
    assert facts['connected'].holds is (not changed_materials)
    assert baseline.carried_over == ()
    assert baseline.source['measurement_basis'] == original.measurement_basis
    assert baseline.source['same_bundle'] is False
    if changed_materials:
        assert baseline.stale_reason.codes == ('inputs_changed',)
        assert baseline.stale_reason.changes.modified == ('lab/payload/manifest.json',)
    else:
        assert baseline.stale_reason is None


async def test_scorer_acceptance_then_campaign_edit_requires_new_acceptance(basis_session):
    env = basis_session
    original, _ = await _measured(env)
    _edit(env, scorer=True)
    await _check(env)
    assert not (await _facts(env[0]))['baseline.measured'].holds
    preview = await env[0].accept('acq', None, only=['baseline.measured'], across_pairs=False,
        reason='reviewed scorer', channel='cli', author='human', dry_run=True)
    accepted = await env[0].accept('acq', None, only=['baseline.measured'], across_pairs=False,
        reason='reviewed scorer', channel='cli', author='human', dry_run=False, expected=list(preview.facts))
    facts = await _facts(env[0])
    assert facts['baseline.measured'].carried_over[0]['acceptance'] == accepted.id
    assert facts['connected'].holds
    _edit(env)
    await _check(env)
    facts = await _facts(env[0])
    assert not facts['baseline.measured'].holds and not facts['connected'].holds
    assert facts['baseline.measured'].source['measurement_basis'] == original.measurement_basis


async def test_invalid_package_and_missing_original_bundle_prevent_basis_reuse(basis_session):
    env = basis_session
    original, _ = await _measured(env)
    env[3].write_text('{}')
    assert not (await _facts(env[0]))['baseline.measured'].holds
    env[3].write_text(json.dumps(env[4]))
    path = env[0].preparation._storage.state_directory / 'bundles' / original.digest
    for file in path.rglob('*'):
        file.chmod(0o700)
    path.chmod(0o700)
    shutil.rmtree(path)
    facts = await _facts(env[0])
    baseline = facts['baseline.measured']
    assert baseline.holds and facts['connected'].holds
    assert baseline.stale_reason is None and baseline.carried_over == ()
    assert baseline.source['measurement_basis'] is None
    _edit(env)
    await _check(env)
    facts = await _facts(env[0])
    assert not facts['baseline.measured'].holds and not facts['connected'].holds
    assert facts['baseline.measured'].stale_reason.codes == ('inputs_changed',)
    assert facts['baseline.measured'].source['measurement_basis'] is None
    accepted = await env[0].accept('acq', None, only=['baseline.measured'], across_pairs=False,
        reason='reviewed unavailable bundle', channel='cli', author='human', dry_run=False)
    assert (await _facts(env[0]))['connected'].holds
    await env[0].revoke_acceptance('acq', accepted.id, reason='withdrawn')
    assert not (await _facts(env[0]))['baseline.measured'].holds


async def test_facts_never_capture_or_create_bundle(basis_session, monkeypatch):
    env = basis_session
    await _measured(env)
    _edit(env)
    def forbidden(*args, **kwargs):
        pytest.fail('facts must only read stored Bundles')
    monkeypatch.setattr(env[0].preparation, 'create_bundle', forbidden)
    from evolution_lab.preparation import bundle as module
    monkeypatch.setattr(module, 'capture_environment', forbidden)
    before = tuple(env[0].preparation._storage.state_directory.rglob('*'))
    assert not (await _facts(env[0]))['baseline.measured'].holds
    assert tuple(env[0].preparation._storage.state_directory.rglob('*')) == before


@pytest.mark.parametrize('field', ['pair', 'state_root'])
async def test_original_basis_never_crosses_pair_or_state(basis_session, field):
    from evolution_lab.projects.models import Pair
    env = basis_session
    await _measured(env)
    _edit(env)
    await _check(env)
    assert (await _facts(env[0]))['baseline.measured'].holds
    # All links and their snapshots remain; no live Owner object exists in the new context.
    env[2].operations.clear(); env[2].targets.clear(); env[2].tasks.clear()
    if field == 'pair':
        env[0].pair = Pair('other-lab', env[0].pair.core)
    else:
        env[0].state_root += '-other'
    facts = await _facts(env[0])
    assert not facts['baseline.measured'].holds
    assert facts['baseline.measured'].source['measurement_basis'] is None


async def test_missing_receipt_only_disables_basis_reuse(basis_session):
    env = basis_session
    original, _ = await _measured(env)
    receipt = env[0].preparation._storage.state_directory / 'receipts' / 'bundles' / f'{original.digest}.json'
    receipt.unlink()
    facts = await _facts(env[0])
    assert facts['baseline.measured'].holds and facts['connected'].holds
    assert facts['baseline.measured'].stale_reason is None
    assert facts['baseline.measured'].carried_over == ()
    _edit(env)
    current, _ = await _check(env)
    assert (await _facts(env[0]))['connected'].holds
    current_receipt = receipt.with_name(f'{current.digest}.json')
    current_receipt.unlink()
    facts = await _facts(env[0])
    assert not facts['baseline.measured'].holds and not facts['connected'].holds
    assert facts['baseline.measured'].stale_reason.codes == ('inputs_changed',)
    assert facts['baseline.measured'].source['measurement_basis'] == original.measurement_basis


def _basis_context(**overrides):
    values = dict(
        operations=(replace(evaluation(current='D2'), bundle_digest='bundle-a', measurement_basis='basis-a'),),
        tasks=tuple(replace(task(action, order=i), inputs_digest='D2', current_digest='D2',
                            bundle_digest='bundle-b', measurement_basis='basis-a')
                    for i, action in enumerate(('check', 'conformance'))),
        current_bundle_digest='bundle-b', current_measurement_basis='basis-a',
    )
    values.update(overrides)
    return ctx(**values)


def test_unchanged_inputs_keep_baseline_and_connected_without_bundle_basis():
    context = ctx(
        operations=(replace(evaluation(), bundle_digest=None, measurement_basis=None),),
        tasks=tuple(replace(task(action, order=i), bundle_digest=None, measurement_basis=None)
                    for i, action in enumerate(('check', 'conformance'))),
        current_bundle_digest=None, current_measurement_basis=None,
    )
    facts = by_id(compute_facts(context))
    baseline = facts['baseline.measured']
    assert baseline.holds
    assert baseline.stale_reason is None
    assert baseline.carried_over == ()
    assert baseline.source['bundle_digest'] is None
    assert baseline.source['measurement_basis'] is None
    assert baseline.source['same_bundle'] is False
    assert facts['connected'].holds


def test_pure_basis_reuses_original_and_keeps_original_source_digest():
    facts = by_id(compute_facts(_basis_context()))
    assert facts['baseline.measured'].holds and facts['connected'].holds
    assert facts['baseline.measured'].source['inputs_digest'] == 'D1'
    assert facts['baseline.measured'].source['same_bundle'] is False
    assert facts['baseline.measured'].carried_over == ()


@pytest.mark.parametrize('control,result', [('finished', {**MEASURED, 'stop_reason': 'cancelled'}),
                                          ('orphaned', None)])
def test_latest_failed_terminal_shadows_older_basis_match(control, result):
    context = _basis_context()
    newer = replace(context.operations[0], operation_id='new', order=99, control_state=control, result=result)
    facts = by_id(compute_facts(replace(context, operations=(*context.operations, newer))))
    assert not facts['baseline.measured'].holds and not facts['connected'].holds
    assert facts['baseline.measured'].source['id'] == 'new'
    assert facts['baseline.measured'].value is False


def test_connected_still_requires_check_and_conformance_effective_digest_agreement():
    context = _basis_context()
    changed = replace(context.tasks[1], inputs_digest='D3', current_digest='D3')
    facts = by_id(compute_facts(replace(context, tasks=(context.tasks[0], changed))))
    assert all(facts[name].holds for name in ('baseline.measured', 'check.passed', 'conformance.passed'))
    assert not facts['connected'].holds


def test_connected_compares_with_the_actual_check_bundle():
    context = _basis_context()
    changed = replace(context.tasks[0], measurement_basis='different-check-basis')
    facts = by_id(compute_facts(replace(context, tasks=(changed, context.tasks[1]))))
    assert facts['baseline.measured'].holds and not facts['connected'].holds


async def test_revocation_leaves_original_basis_rule(basis_session):
    env = basis_session
    original, _ = await _measured(env)
    _edit(env, scorer=True)
    await _check(env)
    accepted = await env[0].accept('acq', None, only=['baseline.measured'], across_pairs=False,
        reason='reviewed scorer', channel='cli', author='human', dry_run=False)
    await env[0].revoke_acceptance('acq', accepted.id, reason='withdrawn')
    assert not (await _facts(env[0]))['connected'].holds
    _edit_path = env[3].parent / env[4]['source']['manifest_path']
    raw = json.loads(_edit_path.read_text()); raw['scorer']['timeout_seconds'] -= 1
    raw['campaign']['sample_retries'] = raw['campaign'].get('sample_retries', 0) + 1
    _edit_path.write_text(json.dumps(raw))
    await _check(env)
    facts = await _facts(env[0])
    assert facts['connected'].holds and facts['baseline.measured'].carried_over == ()
    assert facts['baseline.measured'].source['measurement_basis'] == original.measurement_basis


def test_installed_project_basis_and_acceptance_with_real_owner(tmp_path, monkeypatch):
    """Installed-wheel proof: real checks/conformance/worker, neutral local target."""
    import asyncio
    import sys
    from importlib.metadata import version
    from evolution_lab.owner import Owner, ResourceRef, TargetRef
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.projects.models import Pair
    from evolution_lab.projects.registry import Projects
    from evolution_lab.projects.session import ProjectSession
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    from test_owner_evaluation import _prepared, _finished

    state, _, _, score_log = _prepared(tmp_path, monkeypatch)
    root = tmp_path / 'evaluation-source'
    manifest_path, launch_path = root / 'manifest.json', root / 'launch.json'
    evaluation_input = json.loads((root / 'evaluation-input.json').read_text())
    manifest = json.loads(manifest_path.read_text())
    for key in ('target', 'environment', 'corpus'):
        manifest[key] = evaluation_input[key]
    manifest['scorer'].update({key: evaluation_input['scorer'][key] for key in (
        'argv', 'cwd', 'dependency_paths', 'allowed_env', 'timeout_seconds')})
    for role in ('scorer', 'editor', 'reflector'):
        manifest[role]['argv'][0] = sys.executable
    manifest_path.write_text(json.dumps(manifest))
    launch = json.loads(launch_path.read_text())
    launch['policy'] = 'manifest.json'
    launch['limits']['max_instances'] = 4
    launch['conformance'] = {'kind': 'core-session-public/1'}
    launch_path.write_text(json.dumps(launch))
    (root / 'bundle-request.json').write_text(json.dumps({
        'source': {'kind': 'campaign', 'manifest_path': 'manifest.json', 'launch_path': 'launch.json'},
        'environments': [{'executable': sys.executable,
            'wheel_paths': [str(_installed_lab_wheel()), str(CORE_WHEEL)], 'runtime_files': []}],
        'roots': [{'alias': 'project', 'source_path': '.'}], 'path_bindings': [],
        'env': {'MODE': 'offline', 'SCORER_LOG': str(score_log), 'SLOW_SCORE': 'no'},
        'secret_names': ['SCORER_TOKEN', 'TARGET_TOKEN'],
    }))
    projects = Projects.open(tmp_path / 'home')
    projects.create(id='acq', name='Facts basis', materials_root='evaluation-source',
        implementations=[{'id': 'agent', 'name': 'Agent', 'place': 'evaluation-source',
                          'package_entry': 'bundle-request.json'}], path=str(tmp_path))

    async def scenario():
        async with Owner.open(state) as owner:
            session = ProjectSession(projects, preparation=PreparationStore(state), owner=owner,
                pair=Pair(version('evolution-lab'), version('evo-skills')), state_root=state)

            async def ready(bundle):
                # Checks and evaluation may leave targets ready. End every prior
                # Bundle's target before crossing the Owner's Bundle boundary;
                # stopping retains the historical tasks used by project facts.
                for previous in await owner.list_targets(state='ready'):
                    if previous.bundle_digest != bundle.digest:
                        target = TargetRef(previous.id, owner.owner_generation)
                        stopped = await asyncio.wait_for(owner.wait(await owner.stop_target(target)), 40)
                        assert stopped.state == 'succeeded', stopped.to_payload()
                        assert (await owner.target(target)).state == 'stopped'
                ref, _ = await session.start_target('acq', None, bundle_digest=bundle.digest,
                    secrets={'TARGET_TOKEN': 'private-neutral-target'})
                status = await asyncio.wait_for(owner.wait(ref), 15)
                assert status.state == 'succeeded', status.to_payload()
                return TargetRef(status.ref.id, owner.owner_generation)

            async def checked():
                bundle, _ = await session.bundle('acq')
                target = await ready(bundle)
                connection = await owner.bind(bundle.digest)
                for action in (owner.check, owner.conformance):
                    status = await asyncio.wait_for(owner.wait(await action(connection, target_ref=target)), 30)
                    assert status.state == 'succeeded', status.to_payload()
                return bundle

            original = await checked()
            prepared, _ = await session.prepare('acq', None, bundle_digest=original.digest, kind='evaluation', params={
                'selection': {'case_ids': ['c-3', 'c-1']}, 'repeat_seeds': [17],
                'evaluation_set_id': 'project-basis', 'max_turns': 4, 'timeout_seconds': 9,
                'metric_plan': {'primary': 'score'}, 'sample_retries': 0, 'min_coverage': 1,
                'max_failed_turns': 1, 'call_retries': 'off',
            })
            accepted, _ = await session.start('acq', None, prepared_digest=prepared.digest,
                target_ref=await ready(original), secrets={'SCORER_TOKEN': 'private-neutral-scorer'})
            status = await _finished(owner, ResourceRef('operation', accepted.id))
            assert status.control_state == 'finished' and status.process.exit_code == 0
            assert (await _facts(session))['connected'].holds

            manifest['campaign']['sample_retries'] = manifest['campaign'].get('sample_retries', 0) + 1
            manifest_path.write_text(json.dumps(manifest))
            changed = await checked()
            facts = await _facts(session)
            assert facts['connected'].holds and facts['baseline.measured'].holds
            assert facts['baseline.measured'].source['bundle_digest'] == original.digest
            assert facts['baseline.measured'].source['measurement_basis'] == changed.measurement_basis
            assert facts['baseline.measured'].source['same_bundle'] is False
            assert (await session.facts_payload('acq'))['schema'] == 'lab-project-facts/5'

            manifest['scorer']['timeout_seconds'] += 1
            manifest_path.write_text(json.dumps(manifest))
            await checked()
            assert not (await _facts(session))['connected'].holds
            preview = await session.accept('acq', None, only=['baseline.measured'], across_pairs=False,
                reason='reviewed scorer', channel='cli', author='human', dry_run=True)
            await session.accept('acq', None, only=['baseline.measured'], across_pairs=False,
                reason='reviewed scorer', channel='cli', author='human', dry_run=False, expected=list(preview.facts))
            assert (await _facts(session))['connected'].holds
            manifest['campaign']['sample_retries'] += 1
            manifest_path.write_text(json.dumps(manifest))
            await checked()
            assert not (await _facts(session))['baseline.measured'].holds
    try:
        asyncio.run(scenario())
    finally:
        projects.close()


def test_connected_uses_selected_check_when_another_pair_has_same_task_id():
    from test_projects_facts import OLD
    context = _basis_context()
    old = replace(context.tasks[0], pair=OLD, order=-1, measurement_basis=None)
    facts = by_id(compute_facts(replace(context, tasks=(old, *context.tasks))))
    assert facts['check.passed'].holds and facts['baseline.measured'].holds
    assert facts['connected'].holds


async def test_across_pair_acceptance_keeps_existing_rule(basis_session):
    from evolution_lab.projects.models import Pair
    env = basis_session
    await _measured(env)
    env[2].operations.clear(); env[2].targets.clear(); env[2].tasks.clear()
    env[0].pair = Pair('other-lab', env[0].pair.core)
    accepted = await env[0].accept('acq', None, only=['baseline.measured', 'check.passed', 'conformance.passed'],
        across_pairs=True, reason='reviewed pair', channel='cli', author='human', dry_run=False)
    facts = await _facts(env[0])
    assert facts['connected'].holds
    assert facts['baseline.measured'].source['measurement_basis'] is None
    assert facts['baseline.measured'].carried_over[0]['acceptance'] == accepted.id


async def test_current_bundle_uses_composer_secret_name_ordering(basis_session):
    env = basis_session
    env[4]['secret_names'] = ['TOKEN', 'ANOTHER_TOKEN']
    env[3].write_text(json.dumps(env[4]))
    original, _ = await _measured(env)
    facts = await _facts(env[0])
    assert facts['connected'].holds
    assert facts['baseline.measured'].source['same_bundle'] is True
    assert facts['baseline.measured'].source['bundle_digest'] == original.digest


async def test_bundle_verification_leaves_registry_available(basis_session, monkeypatch):
    from test_projects_sync_registry import assert_registry_available
    session = basis_session[0]
    await _measured(basis_session)
    original = session.preparation._storage.get_bundle
    verified = []

    def verify(*args, **kwargs):
        assert_registry_available(session.projects.store)
        verified.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(session.preparation._storage, 'get_bundle', verify)
    facts = await _facts(session)
    assert facts['baseline.measured'].holds
    assert verified
