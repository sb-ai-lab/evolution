"""Reusable offline real Owner/Core reference stand for dev67 (test-only)."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

import httpx
import pytest

from evolution_lab.owner import Owner, ResourceRef
from evolution_lab.owner.models import ProcessFacts
from evolution_lab.projects.models import Pair
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from test_owner_store_starts import _executor, _finish_normally
from test_projects_session import session_env, _restructure_catalogue

HERE = Path(__file__).parent
PAIR = Pair('0.0.3.dev66', '2.0.0b15')
OP_IDS = {name: f'{i:032x}' for i, name in enumerate(
    ('campaign-finished', 'evaluation-finished', 'campaign-running'), 1)}


@asynccontextmanager
async def reference_project(root, monkeypatch=None, *, real_catalogues=False, operation_copies=1):
    """Yield real read stand using unchanged committed neutral Core captures.

    The session_env layout is reused; its synthetic capture is replaced by genuine
    installed-wheel byte capture before building any bundle. Parent source-import
    identity is deliberately outside this test stand; all saved receipts are real.
    Existing store acceptance/settlement helpers write control-plane records;
    all operation projections, journal reads and schema validation are genuine.
    Keep the context open while exercising the running campaign. Extra copies
    add only terminal operations; the sole running operation is always created
    last, respecting the real Owner execution slot. Defaults preserve Task0.
    """
    if operation_copies < 1:
        raise ValueError('operation_copies must be positive')
    operation_ids = dict(OP_IDS)
    for copy in range(1, operation_copies):
        for name in OP_IDS:
            terminal = 'campaign-extra-finished' if name == 'campaign-running' else name
            operation_ids[f'copy-{copy}-{terminal}'] = f'{len(operation_ids) + 1:032x}'
    operation_ids = dict(sorted(operation_ids.items(), key=lambda item: item[0].endswith('running')))
    from evolution_lab import guides
    scenarios_loader, shipped_guides = guides.scenarios, guides.root()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    own_patch = monkeypatch is None
    patch = pytest.MonkeyPatch() if own_patch else monkeypatch
    from evolution_lab.preparation import bundle as bundle_module
    capture_environment = bundle_module.capture_environment
    fixture = session_env.__wrapped__(root, patch)
    session, projects, _ = next(fixture)
    session.pair = PAIR
    # Probe declared installed wheels genuinely, independently of source imports.
    # This is a test-only preparation seam; it does not test runtime admission.
    def offline_capture(value, *, execution):
        return capture_environment(value, execution=False)
    patch.setattr(bundle_module, "capture_environment", offline_capture)
    try:
        _restructure_catalogue(root, patch)
        projects.update('acq', {'op': 'add_implementation', 'implementation': {
            'id': 'port-a', 'place': 'agent', 'package_entry': 'bundle-request.json',
            'origin': {'kind': 'ported_from', 'implementation': 'agent'}}})
        artifact = root / 'work/lab/restructure/port-a/behavior-map.json'
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps({'schema': 'restructure-behavior-map/1',
            'behaviors': [{'id': 'echo', 'source': 'agent/a.py'}]}))
        (root / 'work/lab/SPEC.md').write_text('Neutral offline reference.\n')
        from evolution_lab.preparation import EnvironmentInput
        from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
        wheels = (_installed_lab_wheel(), CORE_WHEEL)
        descriptor = offline_capture(EnvironmentInput(Path(sys.executable), wheels, ()), execution=False).descriptor
        launch_path = root / 'work/lab/launch.json'
        launch = json.loads(launch_path.read_text())
        launch['environment_ref'] = descriptor.digest
        launch_path.write_text(json.dumps(launch))
        entry_path = root / 'work/lab/bundle-request.json'
        entry = json.loads(entry_path.read_text())
        entry['environments'][0]['wheel_paths'] = [str(wheel) for wheel in wheels]
        entry_path.write_text(json.dumps(entry))
        bundle, _ = await session.bundle('acq', 'port-a')
        prepared, _ = await session.prepare('acq', 'port-a', bundle_digest=bundle.digest,
                                             kind='campaign', params={})
        await session.confirm('acq', 'port-a', claim='spec.confirmed', attests={'path': 'SPEC.md'},
                              linked=[], channel='cli', author='neutral', note=None)
        async with Owner.open(root / 'state') as owner:
            session.owner = owner
            from evolution_lab.projects.session import client_ref
            ref = client_ref('acq', 'port-a')
            connection = await owner._db('bind_connection', bundle_digest=bundle.digest,
                                        launch_digest='1' * 64, source_kind='campaign')
            target = await owner._db('accept_target_start', connection_id=connection.id,
                launch_digest='1' * 64, bundle_digest=bundle.digest,
                base_url='http://127.0.0.1:8123', client_ref=ref)
            await owner._db('mark_target_ready', target.task_ref.id, boot_id='neutral-boot')
            task = await owner._db('accept_task', action='check', ref=ResourceRef('target', target.target_ref.id))
            await owner._db('finish_task', task.task_ref.id)
            for name, operation_id in operation_ids.items():
                kind = 'evaluation' if 'evaluation' in name else 'campaign'
                accepted = await owner._db('accept_operation', prepared_digest=prepared.digest,
                    target_ref=target.target_ref, executor=_executor(f'lab-{kind}-worker/1'),
                    kind=kind, operation_id=operation_id, client_ref=ref)
                run = root / 'state/artifacts' / operation_id / operation_id
                if kind == 'campaign':
                    capture = 'focus-first' if name.endswith('finished') else 'focus-open'
                    shutil.copytree(HERE / 'fixtures/panel/b15' / capture / 'run', run)
                else:
                    source = HERE / 'fixtures/panel/content/run/68207c8e2014428d8fa735d1220f0dae/measurement'
                    shutil.copytree(source, run / 'measurement')
                    from evolution.tree.evolve import derive_run_id
                    outcome = {'schema': 'lab-evaluation-outcome/3', 'operation_id': operation_id,
                        'scope_id': accepted.scope_id, 'run_id': derive_run_id(run),
                        'min_coverage': 0.95, 'unmeasured': [], 'measurement_returned': True,
                        'coverage': {'expected': 1, 'scored': 1}, 'stopped_reason': None,
                        'evaluation_budget': {'max_failed_turns': 1, 'call_retries': None}}
                    scope = root / 'state/scopes' / accepted.scope_id
                    scope.mkdir(parents=True, exist_ok=True)
                    (scope / 'evaluation-outcome.json').write_text(json.dumps(outcome))
                if name.endswith('finished'):
                    await owner._loop.run_in_executor(owner._executor, _finish_normally,
                        owner._store, operation_id, accepted.scope_id)
                else:
                    await owner._db('mark_helper_possible', accepted.scope_id)
                    await owner._db('mark_dispatch_possible', operation_id)
                    await owner._db('mark_operation_running', operation_id)
                    await owner._db('update_operation', operation_id,
                                    process=ProcessFacts(launched=True))
            runtime = ServiceRuntime(ServiceConfig(root / 's.sock', root / 'state', .2,
                                                   home_directory=root / 'home'))
            runtime.owner, runtime.project_session = owner, session
            runtime._admission_open = True
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                         base_url='http://lab') as client:
                stand = SimpleNamespace(root=root, session=session, owner=owner, projects=projects,
                    client=client, runtime=runtime, operation_ids=operation_ids, target=target.target_ref,
                    prepared=prepared, bundle=bundle)
                for name, operation_id in operation_ids.items():
                    status = await owner.status(ResourceRef('operation', operation_id))
                    assert status.results_state == 'present', (name, status.to_payload())
                    assert status.result is not None and status.result.core is not None
                    assert status.control_state == ('running' if name.endswith('running') else 'finished')
                    assert status.result.core['complete'] is not name.endswith('running')
                    assert status.entrypoint
                    assert status.times_state == status.progress_state == 'present'
                    if status.kind == 'campaign':
                        assert status.progress['policy_state'] == 'present'
                    def complete(value):
                        if isinstance(value, dict):
                            return all(complete(v) for v in value.values())
                        if isinstance(value, (list, tuple)):
                            return all(complete(v) for v in value)
                        return value not in ('unavailable', 'unreadable')
                    # This capture predates answered verdicts. Only the known
                    # independently refreshed count overlay is outside this audit.
                    base = status.to_payload()
                    del base['result']['unanswered_state']
                    assert complete(base), name
                if real_catalogues:
                    # Preserve the default golden path. Counters/timing must exercise
                    # the product loaders, not _restructure_catalogue's lambda.
                    guide_root = root / 'guide-root'
                    guide_root.mkdir()
                    (guide_root / 'scenarios').symlink_to(root / 'catalogue', target_is_directory=True)
                    (guide_root / 'requirements').symlink_to(shipped_guides / 'requirements',
                                                           target_is_directory=True)
                    patch.setattr(guides, 'root', lambda: guide_root)
                    patch.setattr(guides, 'scenarios', scenarios_loader)
                yield stand
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        if own_patch:
            patch.undo()


async def read_outputs(stand):
    paths = {'facts': '/v1/projects/acq/facts?implementation=port-a',
        'status': '/v1/projects/acq/status?implementation=port-a',
        'status-scenario': '/v1/projects/acq/status?implementation=port-a&scenario=restructure',
        'operations': '/v1/projects/acq/operations?implementation=port-a',
        'operations-summary': '/v1/projects/acq/operations?implementation=port-a&view=summary'}
    paths.update({name: '/v1/operations/' + value for name, value in stand.operation_ids.items()})
    outputs = {}
    for name, path in paths.items():
        response = await stand.client.get(path)
        assert response.status_code == 200, (path, response.text)
        outputs[name] = response.json()
    facts = {fact['id']: fact for fact in outputs['facts']['facts']}
    assert facts['check.passed']['holds'] and facts['spec.confirmed']['holds']
    scenario = outputs['status-scenario']['scenarios'][0]
    assert scenario['id'] == 'restructure'
    step = next(step for step in scenario['steps'] if step['id'] == 'map')
    assert step['state'] == 'done' and step['sources'][0]['artifact'].endswith('behavior-map.json')
    assert len(outputs['operations']['links']) == len(stand.operation_ids)
    return outputs


class Normalizer:
    """Preserve every key; normalize times, root and bijective generated IDs only."""
    def __init__(self, root):
        self.root, self.ids = str(root), {}

    def __call__(self, value, key=''):
        if isinstance(value, dict):
            return {name: self(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key == 'at' or key.endswith('_at'):
            return '<time>'
        value = value.replace(self.root, '<ROOT>')
        def replace_id(match):
            identifier = match.group()
            return self.ids.setdefault(identifier, f'<id-{len(self.ids) + 1}>')
        return re.sub(r'(?<![0-9a-f])(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}|c-[0-9a-f]{16})(?![0-9a-f])', replace_id, value)


async def capture(root, output):
    async with reference_project(root) as stand:
        raw = await read_outputs(stand)
        normalizer = Normalizer(root)
        output.mkdir(parents=True, exist_ok=True)
        for name, payload in raw.items():
            (output / f'{name}.json').write_text(json.dumps(normalizer(payload), ensure_ascii=False,
                                                          indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    import argparse
    import asyncio
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(capture(args.root, args.output))
