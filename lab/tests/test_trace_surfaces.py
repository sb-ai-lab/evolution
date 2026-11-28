"""New private trace surfaces keep existing service shapes untouched."""
import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.slow

SUMMARY = {'schema': 'lab-trace-summary/1', 'tries': 2, 'scored': 1, 'failed': 1,
           'unavailable': 0, 'unverified': 0, 'in_flight': 0, 'unset_secrets': 0}


def test_trace_has_an_explicit_closed_summary_and_request(tmp_path):
    from evolution.tree import TraceSummary
    from evolution_lab.service.codecs import serialize_public, decode_trace_request
    from evolution_lab.service.errors import ServiceError
    from evolution_lab.cli.responses import validate
    assert serialize_public(TraceSummary(2, 1, 1, 0, 0, 0, 0)) == SUMMARY
    assert validate(SUMMARY, 'lab-trace-summary/1') == SUMMARY
    for change in [{'oracle': 'private'}, {'tries': -1}, {'unset_secrets': True}]:
        with pytest.raises(ValueError):
            validate({**SUMMARY, **change}, 'lab-trace-summary/1')
    payload = {'schema': 'lab-service-trace-request/1', 'out': str(tmp_path / 'out.jsonl'),
               'secrets': {'TRACE_SECRET': 'value'}, 'disclose_private': True,
               'measurements': ['m-r00-champion'], 'max_field_bytes': 1024}
    decoded = decode_trace_request(payload)
    assert decoded.out == tmp_path / 'out.jsonl'
    assert decoded.measurements == ('m-r00-champion',) and decoded.max_field_bytes == 1024
    for change in [{'extra': 1}, {'disclose_private': 'yes'}, {'max_field_bytes': 0}, {'out': '-'}]:
        with pytest.raises(ServiceError):
            decode_trace_request({**payload, **change})


async def test_trace_route_passes_named_private_export_options(tmp_path):
    from evolution.tree import TraceSummary
    from evolution_lab.service.app import create_app
    class Runtime:
        service_environment = None
        def mutation_key(self, headers): return None
        async def call_mutation(self, *args, **kwargs):
            self.call = args, kwargs
            return TraceSummary(2, 1, 1, 0, 0, 0, 0)
    runtime = Runtime()
    out = tmp_path / 'trace.jsonl'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        response = await client.post('/v1/operations/operation/trace', json={
            'schema': 'lab-service-trace-request/1', 'out': str(out), 'secrets': {},
            'disclose_private': True, 'measurements': [], 'max_field_bytes': 65536})
        assert response.status_code == 200, response.text
        assert response.json() == SUMMARY
        assert runtime.call[0][1] == 'trace'
        assert runtime.call[1]['out'] == out and runtime.call[1]['disclose_private'] is True


async def test_trace_is_admitted_by_the_real_service_runtime(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceRuntime
    from test_service_runtime import config
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
            private = await client.post('/v1/operations/missing/trace', json={
                'schema': 'lab-service-trace-request/1', 'out': str(tmp_path / 'out.jsonl'),
                'secrets': {}, 'disclose_private': False, 'measurements': [], 'max_field_bytes': 65536})
            assert private.status_code == 403, private.text
            assert private.json()['error']['code'] == 'private_disclosure_required'
            response = await client.post('/v1/operations/missing/trace', json={
                'schema': 'lab-service-trace-request/1', 'out': str(tmp_path / 'out.jsonl'),
                'secrets': {}, 'disclose_private': True, 'measurements': [], 'max_field_bytes': 65536})
            assert response.status_code == 404 and response.json()['error']['code'] == 'not_found', response.text
    finally:
        await runtime.shutdown()


def test_trace_service_cli_and_missing_disclosure(tmp_path):
    from test_service_cli import FakeClient, invoke
    out = tmp_path / 'out.jsonl'
    client = FakeClient([SUMMARY])
    code, stdout, stderr = invoke(['--json', '--no-request-id', 'trace', 'operation',
        '--out', str(out), '--disclose-private', '--measurement', 'm-r00-champion',
        '--max-field-bytes', '1024'], client)
    assert code == 0, stderr
    assert json.loads(stdout) == SUMMARY
    method, path, payload, mutation, schema = client.calls[-1]
    assert (method, path, mutation, schema) == ('POST', '/v1/operations/operation/trace', True, 'lab-trace-summary/1')
    assert payload['measurements'] == ['m-r00-champion'] and payload['out'] == str(out)
    missing = FakeClient()
    code, stdout, stderr = invoke(['trace', 'operation', '--out', str(out)], missing)
    assert code == 2 and '--disclose-private' in stderr and not missing.calls


def test_project_trace_uses_saved_inputs_and_optional_operation(tmp_path):
    from evolution_lab.cli import main
    from test_owner_linked_successor import _promoted_project
    result = _promoted_project(tmp_path)
    project = tmp_path / 'project'
    operation = result.summary['campaign']['operation_id']
    data = []
    for index, selected in enumerate([[], [operation]]):
        out = tmp_path / f'trace-{index}.jsonl'
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(['--json', 'trace', str(project), *selected, '--out', str(out), '--disclose-private'],
                    stdout=stdout, stderr=stderr)
        assert code == 0, stderr.getvalue()
        summary = json.loads(stdout.getvalue())
        assert summary['schema'] == 'lab-trace-summary/1' and summary['tries'] > 0
        data.append(out.read_bytes())
    assert data[0] == data[1]
