"""Routing attribution reads Lab SQLite only; no target or Core execution."""
import asyncio
import io
import json
import sqlite3

import httpx
import pytest

from evolution_lab.cli import responses
from evolution_lab.cli.output import write_value
from evolution_lab.owner.models import ReportResult
from test_service_cli import FakeClient, invoke


def ledger(path, rows):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE requests(id TEXT PRIMARY KEY, role TEXT)')
        db.execute('CREATE TABLE routing_observations(request_id TEXT PRIMARY KEY, endpoint TEXT, '
                   'upstream_model TEXT, client_model TEXT, requested_provider TEXT, '
                   'requested_providers TEXT, reported_provider TEXT, uses_json TEXT)')
        for i, row in enumerate(rows):
            row = dict(endpoint='https://upstream.invalid/v1', upstream_model='upstream',
                       client_model='client', requested_provider='chosen', requested_providers=None,
                       reported_provider='chosen', uses_json='["operation:op:start:1"]') | row
            role = row.pop('role', 'target')
            db.execute('INSERT INTO requests VALUES(?,?)', (str(i), role))
            columns = ','.join(row)
            db.execute(f'INSERT INTO routing_observations(request_id,{columns}) VALUES({",".join("?" for _ in range(len(row)+1))})',
                       (str(i), *row.values()))


def inputs(path):
    history = [dict(start=1, policy_digest='a'*64, lab_version='0.0.3.dev65'),
               dict(start=2, policy_digest='b'*64, lab_version='0.0.3.dev65')]
    records = {digest*64: dict(author='human-'+digest, channel='cli', confirmed_at=float(i),
                   policy={'gateway': {'ledger_path': str(path)}})
               for i, digest in enumerate(('a', 'b'), 1)}
    return history, records


def read(path):
    from evolution_lab.owner.routing import build_routing
    return build_routing('op', *inputs(path))


def test_groups_exact_operation_starts_without_duplicate_paths(tmp_path):
    path = tmp_path / 'ledger?#%.sqlite'
    ledger(path, [{}, {}, {'reported_provider': 'other'},
                  {'uses_json': '["operation:op:start:2"]', 'requested_provider': None,
                   'requested_providers': '["second","first"]', 'reported_provider': 'first'},
                  {'uses_json': '["operation:op:start:1","probe:peer"]'},
                  {'uses_json': '[]'}, {'uses_json': '["operation:other:start:1"]'},
                  {'uses_json': '["operation:op:start:12"]'},
                  {'requested_provider': None, 'reported_provider': 'unknown'},
                  {'reported_provider': None}])
    before = path.read_bytes()
    result = read(path)
    assert result['available'] is True and result['reason'] is None
    assert result['shared'] == result['outside'] == 1
    rows = result['roles']['target']
    assert sum(row['calls'] for row in rows) == 6
    assert sum(row['mismatched'] for row in rows) == 1
    assert next(row for row in rows if row['reported_provider'] == 'chosen')['calls'] == 2
    assert next(row for row in rows if row['reported_provider'] == 'first')['requested'] == ['second', 'first']
    assert [(row['start'], row['author'], row['channel'], row['confirmed_at']) for row in result['runs']] == [
        (1, 'human-a', 'cli', 1.0), (2, 'human-b', 'cli', 2.0)]
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize('uses', [None, 'null', '{}', 'false', '[7]', '["x","x"]', 'broken'])
def test_invalid_uses_never_become_outside(tmp_path, uses):
    path = tmp_path / 'ledger'
    ledger(path, [{'uses_json': uses}])
    value = read(path)
    assert value['available'] is False and value['reason'] == 'ledger_invalid'
    assert value['outside'] is None and value['roles'] is None
    assert len(value['runs']) == 2


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'legacy', 'missing_observation'])
def test_failed_ledger_keeps_history_without_creation_or_migration(tmp_path, kind):
    path = tmp_path / 'ledger'
    if kind == 'corrupt':
        path.write_bytes(b'private invalid database')
    elif kind == 'legacy':
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE requests(id TEXT, role TEXT)')
    elif kind == 'missing_observation':
        ledger(path, [{}])
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM routing_observations')
    before = path.read_bytes() if path.exists() else None
    result = read(path)
    assert result['available'] is False and len(result['runs']) == 2
    assert result['roles'] is result['shared'] is result['outside'] is None
    assert (path.read_bytes() if path.exists() else None) == before
    assert 'private' not in json.dumps(result)


def test_no_policy_closed_read_and_report_absence():
    from evolution_lab.owner.routing import build_routing
    value = build_routing('op', [dict(start=1, policy_digest=None, lab_version='v')], {})
    assert value['available'] is False and value['reason'] == 'no_policy'
    assert value['runs'] == [dict(start=1, policy_digest=None, author=None, channel=None,
                                  confirmed_at=None, lab_version='v')]
    plain = ReportResult('op', 'unavailable', None, 'unavailable').to_payload()
    assert 'routing' not in plain
    assert responses.validate(plain, 'lab-owner-report/15').to_payload() == plain


def test_cli_route_and_one_line_per_role_row(tmp_path):
    path = tmp_path / 'ledger'
    ledger(path, [{}, {'role': 'editor'}])
    value = read(path)
    assert responses.validate(value, 'lab-operation-routing/1') == value
    client = FakeClient([value])
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', '--json', 'operation', 'routing', 'op'], client)
    assert code == 0, stderr
    assert json.loads(stdout) == value
    assert client.calls[0] == ('GET', '/v1/operations/op/routing', None, False, 'lab-operation-routing/1')
    stream = io.StringIO()
    write_value(stream, value, machine=False)
    lines = stream.getvalue().splitlines()
    role_lines = [line for line in lines if line.startswith(('editor ', 'target '))]
    assert len(role_lines) == 2
    assert role_lines[0].startswith('editor ') and role_lines[1].startswith('target ')
    assert all('calls=1' in line and 'mismatched=0' in line for line in role_lines)
    assert any('shared=0 outside=0' in line for line in lines)
    for i, letter in enumerate(('a', 'b'), 1):
        line = next(line for line in lines if line.startswith(f'run start={i} '))
        assert all(token in line for token in (f'policy_digest={letter*64}', f'author=human-{letter}',
                   'channel=cli', f'confirmed_at={i}.0', 'lab_version=0.0.3.dev65'))
    report = ReportResult('op', 'unavailable', None, 'unavailable', routing=value).to_payload()
    assert responses.validate(report, 'lab-owner-report/15').to_payload() == report


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(extra='unknown'), lambda v: v.update(available=1),
    lambda v: v.update(shared=True), lambda v: v['runs'][0].update(extra=True),
    lambda v: v['runs'][0].update(start=True),
    lambda v: v.update(operation_id='space id'),
    lambda v: v.update(operation_id='id\n'),
    lambda v: v.update(operation_id='../id'),
    lambda v: v['roles']['target'][0].update(calls=-1),
    lambda v: v['roles']['target'][0].update(mismatched=2),
    lambda v: v['roles']['target'][0].update(requested=['x', 'x']),
    lambda v: v['roles']['target'][0].update(endpoint='https://key@upstream.invalid/v1'),
])
def test_cli_rejects_malformed_routing_and_optional_report_routing(tmp_path, mutation):
    path = tmp_path / 'ledger'
    ledger(path, [{}])
    value = read(path)
    mutation(value)
    with pytest.raises(ValueError):
        responses.validate(value, 'lab-operation-routing/1')
    report = ReportResult('op', 'unavailable', None, 'unavailable').to_payload()
    report['routing'] = value
    with pytest.raises(ValueError):
        responses.validate(report, 'lab-owner-report/15')


async def test_http_routing_is_explicit_read_route(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.owner import ResourceRef
    path = tmp_path / 'ledger'
    ledger(path, [{}])
    value = read(path)
    class Runtime:
        async def call_owner(self, method, ref):
            assert method == 'operation_routing' and ref == ResourceRef('operation', 'op')
            return value
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Runtime())), base_url='http://lab') as client:
        response = await client.get('/v1/operations/op/routing')
        assert response.status_code == 200 and response.json() == value
        assert (await client.get('/v1/operations/op/routing?unknown=1')).status_code == 400
        assert (await client.get('/v1/operations/op/routing', headers={'lab-request-id': 'r'*24})).status_code == 400


def test_unavailable_cli_keeps_confirmation_history(tmp_path):
    value = read(tmp_path / 'missing-ledger')
    stream = io.StringIO()
    write_value(stream, value, machine=False)
    lines = stream.getvalue().splitlines()
    assert any('reason=ledger_unreadable' in line for line in lines)
    assert any('shared=None outside=None' in line for line in lines)
    assert any('start=1' in line and 'author=human-a' in line for line in lines)
    assert any('start=2' in line and 'author=human-b' in line for line in lines)


def test_real_resume_policy_fixture_provider_endpoints_parse_without_io(tmp_path):
    from evolution_lab.execution_policy import parse_policy
    from test_policy_resume_e2e import resume_policy_documents
    a, b = resume_policy_documents(tmp_path, 4567, 4568, 4569)
    for document in (a, b):
        assert parse_policy(document).to_payload() == document


def test_report_null_routing_rejected_by_real_transport():
    from evolution_lab.cli.client import ServiceClient, ClientError
    value = ReportResult('op', 'unavailable', None, 'unavailable').to_payload()
    value['routing'] = None
    with pytest.raises(ValueError):
        responses.validate(value, 'lab-owner-report/15')
    with ServiceClient(socket_path='/tmp/lab.sock', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=value))) as client:
        with pytest.raises(ClientError, match='invalid service response'):
            client.request_json('GET', '/v1/operations/op/report', expected_schema='lab-owner-report/15')


async def test_owner_routing_checks_ref_and_retains_no_policy_report_shape(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from test_owner_delete import register, REF
    async with Owner.open(tmp_path / 'state') as owner:
        await register(owner)
        routing = await owner.operation_routing(REF)
        assert routing['reason'] == 'no_policy' and len(routing['runs']) == 1
        assert routing['runs'][0]['policy_digest'] is None
        assert 'routing' not in (await owner.report(REF)).to_payload()
        with pytest.raises(OwnerError) as error:
            await owner.operation_routing(ResourceRef('target', REF.id))
        assert error.value.code == 'invalid_argument'
        with pytest.raises(OwnerError) as error:
            await owner.operation_routing(ResourceRef('operation', 'missing'))
        assert error.value.code == 'not_found'
    with pytest.raises(OwnerError) as error:
        await owner.operation_routing(REF)
    assert error.value.code == 'service_stopping'


async def test_cancelled_routing_read_stays_owned_until_io_finishes(tmp_path, monkeypatch):
    import threading
    from evolution_lab.owner import Owner
    from evolution_lab.owner import routing
    from test_owner_delete import register, REF
    entered, release = threading.Event(), threading.Event()
    original = routing.build_routing
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(routing, 'build_routing', blocked)
    async with Owner.open(tmp_path / 'state') as owner:
        await register(owner)
        request = asyncio.create_task(owner.operation_routing(REF))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert owner._result_owned
            assert owner._result_io.held(REF.id)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*owner._result_owned), 3)
        assert not owner._result_owned
        assert not owner._result_io.held(REF.id)


def test_routing_for_policy_without_gateway_is_unavailable():
    from evolution_lab.owner.routing import build_routing
    history = [dict(start=1, policy_digest='a' * 64, lab_version='v')]
    records = {'a' * 64: {'policy': {'schema': 'lab-execution-policy/2'},
                           'author': 'operator', 'channel': 'api', 'confirmed_at': 1}}
    result = build_routing('op', history, records)
    assert result['available'] is False
    assert result['reason'] == 'no_policy'
    assert result['runs'][0]['policy_digest'] == 'a' * 64
