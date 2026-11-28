"""HTTP adapter keeps authentication and reservation in the common gateway."""
import httpx
import pytest
from test_budget import body, gateway


async def test_http_role_endpoint_reserves_and_refuses_without_fallback(tmp_path):
    from evolution_lab.budget import create_app
    calls = []
    def upstream(request):
        calls.append(request)
        assert ledger.summary()["unknown_requests"] == 1
        return httpx.Response(200, json={"choices": []})
    gw, ledger = gateway(tmp_path, upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url="http://test") as client:
        assert (await client.post("/editor/v1/chat/completions", json=body())).status_code == 401
        headers = {"Authorization": "Bearer client-key"}
        assert (await client.post("/editor/v1/chat/completions", json=body(), headers=headers)).status_code == 200
        assert (await client.post("/editor/v1/chat/completions", json=body(), headers=headers)).status_code == 429
        assert (await client.get("/health")).json()["budget_exhausted"]
    assert len(calls) == 1
    assert ledger.summary()["requests"] == 1


def no_upstream(request):
    raise AssertionError("model catalogue must stay local")


async def test_models_lists_route_model(tmp_path):
    from evolution_lab.budget import create_app
    gw, ledger = gateway(tmp_path, no_upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url="http://test") as client:
        response = await client.get("/editor/v1/models", headers={"Authorization": "Bearer client-key"})
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [
        {"id": "editor-model", "object": "model", "owned_by": "lab-gateway"},
    ]}
    assert ledger.summary()["requests"] == 0


@pytest.mark.parametrize("token", ["client-key", "second-client-key"])
async def test_models_lists_group_members(tmp_path, token):
    from dataclasses import replace
    from evolution_lab.budget import Gateway, create_app
    gw, ledger = gateway(tmp_path, no_upstream)
    route = gw.routes["editor"]
    gw = Gateway(ledger, {"first": route, "second": replace(route, model="second-model", client_token="second-client-key")},
                 role_groups={"editor": ("second", "first")}, transport=httpx.MockTransport(no_upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url="http://test") as client:
        response = await client.get("/editor/v1/models", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [
        {"id": "second-model", "object": "model", "owned_by": "lab-gateway"},
        {"id": "editor-model", "object": "model", "owned_by": "lab-gateway"},
    ]}
    assert ledger.summary()["requests"] == 0


async def test_models_unknown_role_404(tmp_path):
    from evolution_lab.budget import create_app
    gw, ledger = gateway(tmp_path, no_upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url="http://test") as client:
        response = await client.get("/missing/v1/models", headers={"Authorization": "Bearer client-key"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_role_or_model"
    assert ledger.summary()["requests"] == 0


@pytest.mark.parametrize("token", [None, "wrong-key", "synthetic-key", b"\xff"])
async def test_models_bad_token_401(tmp_path, token):
    from evolution_lab.budget import create_app
    gw, ledger = gateway(tmp_path, no_upstream)
    headers = {} if token is None else {
        "Authorization": b"Bearer " + token if isinstance(token, bytes) else "Bearer " + token,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gw)), base_url="http://test") as client:
        response = await client.get("/editor/v1/models", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_gateway_credentials"
    assert ledger.summary()["requests"] == 0


async def test_gateway_control_still_checks_the_credential(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import uvicorn
    from evolution_lab import _gateway_helper, budget
    from evolution_lab.owner.protocol import encode_frame
    from test_owner_target_gateway import policy_document
    directory = tmp_path / 'control'
    directory.mkdir(mode=0o700)
    path = directory / 'c'
    changes = []
    done = asyncio.Event()
    fake_gateway = SimpleNamespace(set_uses=lambda uses: changes.append(uses),
                                   ledger=SimpleNamespace(close_anchor=lambda: None))
    monkeypatch.setattr(budget, 'gateway_from_policy', lambda *a, **kw: fake_gateway)
    monkeypatch.setattr(budget, 'create_app', lambda gateway: object())
    monkeypatch.setattr(uvicorn, 'Server', lambda config: SimpleNamespace(
        started=True, should_exit=False, serve=done.wait))
    task = asyncio.create_task(_gateway_helper.serve({
        'durability': 'relaxed', 'policy': policy_document(tmp_path), 'upstream_keys': {}, 'tokens': {},
        'control_path': str(path), 'control_credential': 'private-credential'}))
    try:
        for _ in range(300):
            if path.exists() or task.done():
                break
            await asyncio.sleep(.01)
        if task.done():
            await task
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(encode_frame({'credential': 'wrong', 'uses': ['forged']}))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b''
        writer.close()
        await writer.wait_closed()
        assert changes == []
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(encode_frame({'credential': 'private-credential', 'uses': ['authorized']}))
        await writer.drain()
        assert await _gateway_helper.control_frame(reader) == {'ready': True}
        writer.close()
        await writer.wait_closed()
        assert changes == [frozenset({'authorized'})]
    finally:
        done.set()
        await task


@pytest.mark.parametrize('invalid', ['relative', 'public_parent', 'file_parent', 'foreign_parent'])
async def test_gateway_control_path_requires_private_owned_directory(tmp_path, monkeypatch, invalid):
    import os
    from pathlib import Path
    from evolution_lab import _gateway_helper
    from test_owner_target_gateway import policy_document
    directory = tmp_path / 'control'
    directory.mkdir(mode=0o700)
    path = directory / 'c'
    if invalid == 'relative':
        path = Path('relative/c')
    elif invalid == 'public_parent':
        directory.chmod(0o755)
    elif invalid == 'file_parent':
        directory.rmdir()
        directory.write_text('file')
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(ValueError):
        await _gateway_helper.serve({'policy': policy_document(tmp_path),
            'upstream_keys': {'UPSTREAM_KEY': 'offline-key'}, 'tokens': {'target': 'private-token'},
            'control_path': str(path), 'control_credential': 'private-credential'})
