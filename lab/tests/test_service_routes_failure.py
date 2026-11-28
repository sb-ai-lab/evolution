"""The new read route uses latest-start diagnostic evidence and fixed DTOs."""
import json
from types import SimpleNamespace

import httpx
import pytest

from evolution_lab.owner.readers import ReaderSupport


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
async def test_failure_route_returns_class_code_and_log(tmp_path, kind):
    from evolution_lab.owner.models import OwnerError, ResourceRef, WorkerFailureRecord
    from evolution_lab.service.app import create_app
    from evolution_lab.cli.responses import validate
    operation, scope = 'a' * 32, 'b' * 32
    exception = {'class': 'evolution.tree.execution.config.ManifestError', 'code': 'manifest_invalid'}
    failure = {'code': 'lab_error', 'stage': 'running', 'message': 'The owner operation failed.'}
    path = tmp_path / 'scopes' / scope / 'diagnostic.json'
    path.parent.mkdir(parents=True)
    payload = {'schema': 'lab-evaluation-diagnostic/2', 'operation_id': operation,
               'scope_id': scope, 'failure': failure, 'exception': exception}
    if kind == 'campaign':
        payload.update(schema='lab-campaign-diagnostic/5', refusal=None, core_terminal=None, findings=[])
    path.write_text(json.dumps(payload))
    start = SimpleNamespace(start=2, scope_id=scope)
    starts = [start]
    class Owner(ReaderSupport):
        state_directory = tmp_path
        def _assert_loop(self): pass
        async def _db(self, method, identifier):
            assert method == 'operation_status' and identifier == operation
            return SimpleNamespace(id=operation, kind=kind, starts=tuple(starts), results_state='present')
    owner = Owner()
    class Runtime:
        service_environment = None
        async def call_owner(self, method, ref):
            assert method == 'failure' and ref == ResourceRef('operation', operation)
            return await owner.failure(ref)
    expected = {'schema': 'lab-operation-failure/1', 'operation_id': operation, 'start': 2,
                'scope_id': scope, 'exception': exception, 'log': f'scopes/{scope}/stderr.log'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Runtime())), base_url='http://lab') as client:
        response = await client.get(f'/v1/operations/{operation}/failure')
        assert response.status_code == 200, response.text
        assert response.json() == expected
        assert validate(expected, 'lab-operation-failure/1').to_payload() == expected
        for over in ({'extra': 1}, {'log': '/private/log'}, {'start': True}, {'exception': {**exception, 'extra': 1}}):
            with pytest.raises((ValueError, OwnerError)):
                WorkerFailureRecord.from_payload({**expected, **over})
        response = await client.get(f'/v1/operations/{operation}/failure', headers={'Lab-Request-Id': 'key'})
        assert response.status_code == 400
        # No fallback to an older start, even while a new one has no evidence.
        starts.append(SimpleNamespace(start=3, scope_id='c' * 32))
        response = await client.get(f'/v1/operations/{operation}/failure')
        assert response.status_code == 404 and response.json()['error']['code'] == 'not_found'


async def test_real_service_dispatch_admits_failure_reader(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceRuntime
    from test_service_runtime import config
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
            response = await client.get('/v1/operations/missing/failure')
            assert response.status_code == 404, response.text
            assert response.json()['error']['code'] == 'not_found'
    finally:
        await runtime.shutdown()
