import json

import httpx
import pytest

from evolution_lab.budget import BudgetLedger, Gateway, Price, Route, create_app
from evolution_lab.execution_policy import parse_policy


def policy(tmp_path, provider=None):
    route = {"client_model": "alias", "endpoint": "http://localhost/openrouter/api/v1",
             "api_key_secret": "KEY", "model": "upstream", "price": {"input_per_million": 1,
             "output_per_million": 1, "context_tokens": 1000}, "max_tokens": 10, "timeout_seconds": 5}
    if provider is not None:
        route['provider'] = provider
    return parse_policy({"schema": "lab-execution-policy/1", "gateway": {"host": "localhost", "port": 1234,
        "ledger_path": str(tmp_path / 'ledger'), "limit_units": 10000000, "ledger_identity": "id", "currency": "USD"},
        "routes": {"editor": route}, "role_groups": {"group": ["editor"]}})


def reply():
    return httpx.Response(200, json={"model": "actual", "provider": "p", "usage": {"prompt_tokens": 1, "completion_tokens": 2}})


async def test_policy_alias_fallback_reasoning_and_snapshot(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        gw.set_uses(frozenset({'later'}))
        return reply()
    gw = gateway_from_policy(policy(tmp_path, {"order": ["p", "q"], "allow_fallbacks": True}),
                             upstream_keys={"KEY": "key"}, tokens={"editor": "token"}, transport=httpx.MockTransport(handler))
    gw.set_uses(frozenset({'z', 'a'}))
    status, payload = await gw.complete('group', {"model": "alias", "messages": [], "max_tokens": 10, "reasoning": {"effort": "high"}}, 'Bearer token')
    assert status == 200 and payload['model'] == 'actual'
    assert seen[0]['model'] == 'upstream' and seen[0]['reasoning'] == {'effort': 'high'}
    assert seen[0]['provider'] == {'order': ['p', 'q'], 'allow_fallbacks': True}
    with gw.ledger.connection() as db:
        assert db.execute('select endpoint,client_model,upstream_model,uses_json from routing_observations').fetchone() == ('http://localhost/openrouter/api/v1', 'alias', 'upstream', '["a","z"]')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url='http://test') as client:
        assert (await client.get('/group/v1/models', headers={'Authorization': 'Bearer token'})).json()['data'][0]['id'] == 'alias'


async def test_keyless_and_legacy_reasoning(tmp_path):
    seen = []
    def handler(req):
        seen.append(req)
        return reply()
    ledger = BudgetLedger(tmp_path / 'db', limit_units=10000000, identity='id')
    route = Route('http://localhost/v1', '', 'upstream', Price(1, 1, 1000), 10, client_token='token', keyless=True, client_model='alias')
    gw = Gateway(ledger, {'editor': route}, transport=httpx.MockTransport(handler))
    body = {'model': 'alias', 'messages': [], 'max_tokens': 10}
    assert (await gw.complete('editor', {**body, 'reasoning': {}}, 'Bearer token'))[0] == 400
    assert (await gw.complete('editor', body, 'Bearer token'))[0] == 200
    assert 'authorization' not in seen[0].headers


async def test_legacy_golden(tmp_path):
    seen = []
    ledger = BudgetLedger(tmp_path / 'db', limit_units=10000000, identity='test')
    route = Route('https://openrouter.ai/api/v1', 'key', 'model', Price(1, 1, 1000), 10, providers=('p', 'q'), client_token='token')
    gw = Gateway(ledger, {'editor': route}, transport=httpx.MockTransport(lambda r: (seen.append(json.loads(r.content)), reply())[1]))
    await gw.complete('editor', {'model': 'model', 'messages': [], 'max_tokens': 10}, 'Bearer token')
    assert seen == [{'model': 'model', 'messages': [], 'max_tokens': 10, 'provider': {'only': ['p', 'q'], 'order': ['p', 'q'], 'allow_fallbacks': False}}]
    with ledger.connection() as db:
        assert db.execute('select role,model,request_hash,reserved,settled,settled_source from requests').fetchone() == ('editor', 'model', 'b9f4dc0af9a01b2bab2ed802fa222c19c4473451a866d217ee9cc2e6b132f12b', 143000, 3000, 'tariff')
        assert db.execute('select requested_provider,reported_provider,reported_model,requested_providers from routing_observations').fetchone() == (None, 'p', 'actual', '["p","q"]')


async def test_policy_only_and_binding_validation(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    pol = policy(tmp_path, {'only': ['p', 'q']})
    for keys, tokens in [({}, {'editor': 'token'}), ({'KEY': 'key', 'EXTRA': 'x'}, {'editor': 'token'}), ({'KEY': 'key'}, {})]:
        with pytest.raises(ValueError, match='bindings'):
            gateway_from_policy(pol, upstream_keys=keys, tokens=tokens)
    seen = []
    gw = gateway_from_policy(pol, upstream_keys={'KEY': 'key'}, tokens={'editor': 'token'},
                             transport=httpx.MockTransport(lambda req: (seen.append(json.loads(req.content)), reply())[1]))
    assert (await gw.complete('editor', {'model': 'alias', 'messages': [], 'max_tokens': 10}, 'Bearer token'))[0] == 200
    assert seen[0]['provider'] == {'only': ['p', 'q'], 'allow_fallbacks': False}


async def test_policy_floor_is_independent_of_ledger_floor(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    payload = policy(tmp_path).to_payload()
    payload['routes']['editor']['balance_floor'] = 5
    pol = parse_policy(payload)
    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'data': {'limit_remaining': 10}})
        return reply()
    gw = gateway_from_policy(pol, upstream_keys={'KEY': 'key'}, tokens={'editor': 'token'}, transport=httpx.MockTransport(handler))
    gw.ledger.activate_balance_floor(20, authorization='test')
    assert (await gw.complete('editor', {'model': 'alias', 'messages': [], 'max_tokens': 10}, 'Bearer token'))[0] == 200
    assert gw.ledger.summary()['refusals'] == 0


async def test_keyless_policy_ignores_legacy_floor(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    payload = policy(tmp_path).to_payload()
    payload['routes']['editor'].update(endpoint='http://localhost/v1', api_key_secret=None)
    seen = []
    gw = gateway_from_policy(parse_policy(payload), upstream_keys={}, tokens={'editor': 'token'}, transport=httpx.MockTransport(lambda req: (seen.append(req), reply())[1]))
    gw.ledger.activate_balance_floor(20, authorization='test')
    assert (await gw.complete('editor', {'model': 'alias', 'messages': [], 'max_tokens': 10}, 'Bearer token'))[0] == 200
    assert len(seen) == 1 and 'authorization' not in seen[0].headers


async def test_policy_order_without_fallbacks_does_not_become_only(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    seen = []
    gw = gateway_from_policy(policy(tmp_path, {'order': ['p', 'q'], 'allow_fallbacks': False}),
                             upstream_keys={'KEY': 'key'}, tokens={'editor': 'token'},
                             transport=httpx.MockTransport(lambda req: (seen.append(json.loads(req.content)), reply())[1]))
    await gw.complete('editor', {'model': 'alias', 'messages': [], 'max_tokens': 10}, 'Bearer token')
    assert seen[0]['provider'] == {'order': ['p', 'q'], 'allow_fallbacks': False}


async def test_wrong_client_model_is_refused_before_reservation(tmp_path):
    from evolution_lab.budget import gateway_from_policy
    def handler(request):
        pytest.fail("wrong client model reached upstream")
    gw = gateway_from_policy(policy(tmp_path), upstream_keys={'KEY': 'key'},
                             tokens={'editor': 'token'}, transport=httpx.MockTransport(handler))
    status, payload = await gw.complete('editor', {'model': 'upstream', 'messages': [], 'max_tokens': 10}, 'Bearer token')
    assert status == 400 and payload['error']['code'] == 'invalid_role_or_model'
    assert gw.ledger.summary()['requests'] == 0
