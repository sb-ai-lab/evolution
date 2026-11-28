"""Gateway dispatch must reserve each editor call and retain unknown charges."""
import json

import httpx
import pytest


def gateway(tmp_path, handler, *, limit=195000):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=limit, identity="offline-test")
    route = Route("https://example.test/v1", "synthetic-key", "editor-model", Price(1, 1, 1000), 100, client_token="client-key")
    return Gateway(ledger, {"editor": route}, transport=httpx.MockTransport(handler)), ledger


def body():
    return {"model": "editor-model", "messages": [{"role": "user", "content": "edit"}], "max_tokens": 100}


async def test_editor_reserves_before_dispatch_and_settles(tmp_path):
    def upstream(request):
        assert ledger.summary()["unknown_requests"] == 1
        assert ledger.summary()["reserved_upper_units"] == 195000
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    gw, ledger = gateway(tmp_path, upstream)
    status, _ = await gw.complete("editor", body(), "Bearer client-key")
    assert status == 200
    assert ledger.summary()["reserved_upper_units"] == 15000
    assert ledger.summary()["unknown_requests"] == 0
    with ledger.connection() as db:
        assert db.execute("SELECT role,model FROM requests").fetchall() == [("editor", "editor-model")]
    assert "synthetic-key" not in json.dumps(ledger.summary())


async def test_unknown_usage_remains_reserved_and_next_editor_call_is_refused(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": []})
    gw, ledger = gateway(tmp_path, upstream)
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    status, payload = await gw.complete("editor", body(), "Bearer client-key")
    assert status == 429 and payload["error"]["code"] == "budget_exhausted"
    assert len(calls) == 1 and ledger.summary()["unknown_requests"] == 1


async def test_provider_error_does_not_retry_or_release_unknown_charge(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic failure")
    gw, ledger = gateway(tmp_path, upstream)
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 502
    assert len(calls) == 1 and ledger.summary()["unknown_requests"] == 1


def _failures(ledger):
    import sqlite3
    with sqlite3.connect(ledger.path) as db:
        return db.execute("SELECT status, code FROM request_failures ORDER BY rowid").fetchall()


async def test_failures_are_recorded_by_precedence(tmp_path):
    replies = iter([
        httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}),
        httpx.Response(404, json={"error": {"code": "not_found", "message": "no endpoints"}}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, text="<html>oops</html>"),
    ])
    gw, ledger = gateway(tmp_path, lambda request: next(replies), limit=5000000)
    for _ in range(4):
        await gw.complete("editor", body(), "Bearer client-key")
    assert _failures(ledger) == [(402, "http_error"), (404, "not_found"), (200, "usage_missing"),
                                 (200, "upstream_invalid")]
    summary = ledger.summary()
    assert summary["failed_requests"] == 4 and summary["unknown_requests"] == 2
    assert summary["failed_by_status"] == {"200": 2, "402": 1, "404": 1, "transport": 0}


@pytest.mark.parametrize("status_code, error_code, recorded_code", [(200, "x", "x"), (201, "x", "x"), (200, "not a token", "http_error")])
async def test_2xx_error_without_choices_releases_reservation(tmp_path, status_code, error_code, recorded_code):
    payload = {"error": {"code": error_code, "message": "provider detail"}}
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(status_code, json=payload))
    status, returned = await gw.complete("editor", body(), "Bearer client-key")
    assert (status, returned) == (status_code, payload)
    assert settlement(ledger) == (0, "refused")
    assert _failures(ledger) == [(status_code, recorded_code)]
    assert ledger.summary()["unknown_requests"] == 0
    assert ledger.summary()["reserved_upper_units"] == 0


async def test_2xx_error_with_choices_uses_normal_usage_accounting(tmp_path):
    payload = {"error": {"code": "x"}, "choices": [],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(200, json=payload))
    assert await gw.complete("editor", body(), "Bearer client-key") == (200, payload)
    assert settlement(ledger) == (15000, "tariff")
    assert _failures(ledger) == []


async def test_2xx_without_error_or_usage_keeps_reservation(tmp_path):
    payload = {"other": "unusable"}
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(200, json=payload))
    assert await gw.complete("editor", body(), "Bearer client-key") == (200, payload)
    assert settlement(ledger) == (None, None)
    assert _failures(ledger) == [(200, "usage_missing")]
    assert ledger.summary()["reserved_upper_units"] == 195000


async def test_http_2xx_error_body_passes_through_byte_for_byte(tmp_path):
    import asyncio
    import socket
    import threading
    import uvicorn
    from evolution_lab.budget import create_app

    raw = '{  "error" : { "code": "x", "message": "Привет \\u0061" }, "number": 1.00 }\n'.encode("utf-8")
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(
        200, content=raw, headers={"content-type": "application/json"}))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(gw), log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{sock.getsockname()[1]}/editor/v1/chat/completions",
                                         json=body(), headers={"Authorization": "Bearer client-key"})
        assert response.status_code == 200
        assert response.content == raw
        assert settlement(ledger) == (0, "refused")
        assert _failures(ledger) == [(200, "x")]
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()
        assert not thread.is_alive()


async def test_refusal_release_failure_keeps_reservation_and_recorded_failure(tmp_path):
    import sqlite3

    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(200, json={"error": {"code": "x"}}))
    with ledger.connection() as db:
        db.execute("""CREATE TRIGGER fail_release BEFORE UPDATE OF settled ON requests
                   BEGIN SELECT RAISE(ABORT, 'simulated release failure'); END""")
        db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated release failure"):
        await gw.complete("editor", body(), "Bearer client-key")
    assert settlement(ledger) == (None, None)
    assert _failures(ledger) == [(200, "x")]
    with ledger.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM released_reservations").fetchone() == (0,)
    assert ledger.summary()["reserved_upper_units"] == 195000


async def test_html_error_keeps_status(tmp_path):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(404, text="<html>not found</html>"))
    status, _ = await gw.complete("editor", body(), "Bearer client-key")
    assert status == 502
    assert _failures(ledger) == [(404, "http_error")]
    assert ledger.summary()["failed_by_status"] == {"404": 1, "transport": 0}


async def test_json_null_body_keeps_invalid_upstream_response(tmp_path):
    for code, row in ((200, (200, "upstream_invalid")), (404, (404, "http_error"))):
        (tmp_path / str(code)).mkdir()
        gw, ledger = gateway(tmp_path / str(code), lambda request, code=code: httpx.Response(code, content=b"null"))
        status, payload = await gw.complete("editor", body(), "Bearer client-key")
        assert status == 502 and payload["error"]["code"] == "invalid_upstream_response"
        assert _failures(ledger) == [row]


async def test_transport_failure_has_no_status(tmp_path):
    def upstream(request):
        raise httpx.ReadTimeout("synthetic failure")
    gw, ledger = gateway(tmp_path, upstream)
    await gw.complete("editor", body(), "Bearer client-key")
    assert _failures(ledger) == [(None, "upstream_unavailable")]
    assert ledger.summary()["failed_by_status"] == {"transport": 1}


async def test_settled_request_records_no_failure(tmp_path):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(
        200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    await gw.complete("editor", body(), "Bearer client-key")
    assert _failures(ledger) == [] and ledger.summary()["failed_requests"] == 0


async def test_invalid_credentials_and_model_never_dispatch_or_reserve(tmp_path):
    def upstream(request):
        pytest.fail("refused request dispatched")
    gw, ledger = gateway(tmp_path, upstream)
    assert (await gw.complete("editor", body(), "Bearer wrong"))[0] == 401
    assert (await gw.complete("editor", {**body(), "model": "wrong"}, "Bearer client-key"))[0] == 400
    assert ledger.summary()["requests"] == 0


def test_budget_identity_cannot_be_reused_with_new_limit(tmp_path):
    from evolution_lab.budget import BudgetLedger
    BudgetLedger(tmp_path / "budget.sqlite", limit_units=10, identity="one")
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "budget.sqlite", limit_units=20, identity="one")


def test_account_floor_does_not_replace_new_experiment_spending_cap(tmp_path):
    from evolution_lab.budget import BudgetLedger, BudgetExhausted
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=10, identity="one")
    ledger.activate_balance_floor(5, authorization="synthetic authorization")
    with pytest.raises(BudgetExhausted):
        ledger.reserve(11, role="editor", model="test", request_hash="test")


async def test_arbitrary_agent_roles_are_supported(tmp_path):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=5000000, identity="another-agent")
    route = Route("https://example.test/v1", "synthetic-key", "other-model", Price(1, 1, 1000), 100, client_token="client-key")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    gw = Gateway(ledger, {"planner": route}, transport=transport, role_groups={"runtime": ("planner",)})
    status, _ = await gw.complete("runtime", {**body(), "model": "other-model"}, "Bearer client-key")
    assert status == 200
    with ledger.connection() as db:
        assert db.execute("SELECT role FROM requests").fetchall() == [("planner",)]


async def test_target_has_no_implicit_role_mapping(tmp_path):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=5000000, identity="no-alias")
    route = Route("https://example.test/v1", "synthetic-key", "editor-model", Price(1, 1, 1000), 100, client_token="client-key")
    def upstream(request): pytest.fail("implicit alias dispatched")
    gw = Gateway(ledger, {"agent": route}, transport=httpx.MockTransport(upstream))
    status, _ = await gw.complete("target", body(), "Bearer client-key")
    assert status == 400 and ledger.summary()["requests"] == 0


def test_ambiguous_group_configuration_is_refused(tmp_path):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=10, identity="ambiguous")
    route = Route("https://example.test/v1", "synthetic-key", "same-model", Price(1, 1, 1), 1)
    with pytest.raises(ValueError):
        Gateway(ledger, {"first": route, "second": route}, role_groups={"runtime": ("first", "second")})


def cost_gateway(tmp_path, usage, *, base_url="https://openrouter.ai/api/v1", currency="USD"):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route

    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=100_000_000, identity="cost-test")
    route = Route(base_url, "synthetic-key", "editor-model", Price(1, 2, 50_000), 100,
                  client_token="client-key")
    # Raw JSON also represents the non-finite number accepted by the JSON decoder.
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=json.dumps({"choices": [], "usage": usage}).replace("Infinity", "1e400").encode(),
        headers={"Content-Type": "application/json"}))
    return Gateway(ledger, {"editor": route}, currency=currency, transport=transport), ledger


def settlement(ledger):
    with ledger.connection() as db:
        return db.execute("SELECT settled, settled_source FROM requests").fetchone()


async def test_openrouter_cost_settles_provider_amount(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 44_000, "completion_tokens": 100,
                                         "cost": 0.00144})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (1_440_000, "upstream_cost")
    summary = ledger.summary()
    assert summary["settled_upstream_units"] == 1_440_000
    assert summary["settled_tariff_units"] == 0
    assert summary["usage_cost_missing"] == 0
    assert summary["released_refusals"] == 0


async def test_routerai_cost_on_rub_gateway(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 44_000, "completion_tokens": 100,
                                         "cost": 0.00144},
                              base_url="https://routerai.ru/api/v1", currency="RUB")
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (1_440_000, "upstream_cost")
    assert ledger.summary()["settled_upstream_units"] == 1_440_000


async def test_cost_currency_mismatch_uses_tariff(tmp_path):
    from evolution_lab.budget import BudgetLedger

    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 44_000, "completion_tokens": 100,
                                         "cost": 0.00144}, base_url="https://routerai.ru/api/v1")
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (44_200_000, "tariff")
    reopened = BudgetLedger(ledger.path, limit_units=100_000_000, identity="cost-test")
    assert reopened.summary()["settled_tariff_units"] == 44_200_000
    assert reopened.summary()["usage_cost_missing"] == 1


async def test_cost_without_tokens_keeps_reservation(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"cost": 0.01})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert ledger.summary()["unknown_requests"] == 1
    assert ledger.summary()["reserved_upper_units"] == 295_000
    assert _failures(ledger) == [(200, "usage_missing")]


@pytest.mark.parametrize("cost", [True, -1, "0.1", float("1e400")])
async def test_invalid_cost_values_use_tariff(tmp_path, cost):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 44_000, "completion_tokens": 100,
                                         "cost": cost})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (44_200_000, "tariff")
    assert ledger.summary()["usage_cost_missing"] == 1


async def test_unknown_protocol_route_uses_tariff(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 44_000, "completion_tokens": 100,
                                         "cost": 0.00144}, base_url="https://example.test/v1")
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (44_200_000, "tariff")
    assert ledger.summary()["settled_upstream_units"] == 0
    assert ledger.summary()["settled_tariff_units"] == 44_200_000
    assert ledger.summary()["usage_cost_missing"] == 0


def test_old_ledger_gains_settled_source_column(tmp_path):
    import sqlite3
    from evolution_lab.budget import BudgetLedger

    path = tmp_path / "budget.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE configuration (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                identity TEXT NOT NULL, limit_units INTEGER NOT NULL);
            INSERT INTO configuration VALUES (1, 'legacy-test', 100000000);
            CREATE TABLE requests (id TEXT PRIMARY KEY, role TEXT NOT NULL, model TEXT NOT NULL,
                request_hash TEXT NOT NULL, reserved INTEGER NOT NULL, settled INTEGER,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO requests VALUES ('settled', 'editor', 'editor-model', 'first', 10000, 3000,
                '2026-10-24 00:00:00');
            INSERT INTO requests VALUES ('pending', 'editor', 'editor-model', 'second', 20000, NULL,
                '2026-10-24 00:00:01');
            CREATE TABLE routing_observations (request_id TEXT PRIMARY KEY, requested_provider TEXT,
                reported_provider TEXT, reported_model TEXT, requested_providers TEXT);
            INSERT INTO routing_observations VALUES ('settled', NULL, NULL, 'editor-model', NULL);
        """)
    ledger = BudgetLedger(path, limit_units=100_000_000, identity="legacy-test")
    with ledger.connection() as db:
        assert "settled_source" in {row[1] for row in db.execute("PRAGMA table_info(requests)")}
        assert db.execute("SELECT id, reserved, settled, settled_source, created FROM requests ORDER BY id").fetchall() == [
            ("pending", 20000, None, None, "2026-10-24 00:00:01"),
            ("settled", 10000, 3000, None, "2026-10-24 00:00:00"),
        ]
    summary = ledger.summary()
    assert summary["settled_tariff_units"] == 3000
    assert summary["settled_upstream_units"] == 0
    assert summary["reserved_upper_units"] == 23000
    assert summary["unknown_requests"] == 1
    assert summary["usage_cost_missing"] == 0


async def test_reservation_uses_request_bytes(tmp_path):
    import hashlib
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route

    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=2_000_000_000,
                          identity="request-bound")
    price = Price(1, 2, 1_048_576)
    route = Route("https://openrouter.ai/api/v1", "synthetic-key", "editor-model", price, 16,
                  provider="provider-a", reasoning_effort="high", client_token="client-key")

    def upstream(request):
        sent = json.loads(request.content)
        assert sent["provider"] == {"only": ["provider-a"], "allow_fallbacks": False}
        assert sent["reasoning"] == {"effort": "high"}
        encoded = json.dumps(sent, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        with ledger.connection() as db:
            reserved, request_hash = db.execute("SELECT reserved,request_hash FROM requests").fetchone()
        assert reserved == price.units(len(encoded), 16)
        assert request_hash == hashlib.sha256(encoded).hexdigest()
        return httpx.Response(200, json={"choices": []})

    gw = Gateway(ledger, {"editor": route}, currency="USD", transport=httpx.MockTransport(upstream))
    request = {**body(), "messages": [{"role": "user", "content": "x" * 2048}], "max_tokens": 16}
    assert (await gw.complete("editor", request, "Bearer client-key"))[0] == 200


async def test_reservation_capped_by_context(tmp_path):
    from evolution_lab.budget import BudgetLedger, Gateway, Price, Route

    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=1_000_000, identity="context-bound")
    price = Price(1, 2, 100)
    route = Route("https://example.test/v1", "synthetic-key", "editor-model", price, 16,
                  client_token="client-key")

    def upstream(request):
        assert ledger.summary()["reserved_upper_units"] == price.units(100, 16)
        return httpx.Response(200, json={"choices": []})

    gw = Gateway(ledger, {"editor": route}, transport=httpx.MockTransport(upstream))
    request = {**body(), "messages": [{"role": "user", "content": "x" * 2048}], "max_tokens": 16}
    assert (await gw.complete("editor", request, "Bearer client-key"))[0] == 200


async def test_completion_over_max_tokens_within_tariff_is_observation(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 20, "completion_tokens": 600,
                                        "cost": 0.00122})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (1_220_000, "upstream_cost")
    assert ledger.summary()["bound_observations"] == 1
    assert ledger.summary()["overrun_requests"] == 0
    with ledger.connection() as db:
        assert db.execute("SELECT reason FROM bound_observations").fetchall() == [
            ("provider_usage_exceeds_bound",)]
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200


async def test_cost_above_tariff_for_reported_tokens_stops(tmp_path):
    # Tariff for 20 input + 100 output tokens is 0.00022; twice that exceeds the reservation.
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 20, "completion_tokens": 100,
                                        "cost": 0.00044})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (440_000, "upstream_cost")
    assert ledger.summary()["overrun_requests"] == 1
    with ledger.connection() as db:
        assert db.execute("SELECT reason FROM overruns").fetchall() == [("settled_exceeds_reservation",)]
    status, payload = await gw.complete("editor", body(), "Bearer client-key")
    assert status == 429 and payload["error"]["code"] == "budget_exhausted"


async def test_tariff_settlement_never_overruns(tmp_path):
    gw, ledger = cost_gateway(tmp_path, {"prompt_tokens": 500, "completion_tokens": 100})
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200
    assert settlement(ledger) == (700_000, "tariff")
    assert ledger.summary()["bound_observations"] == 1
    assert ledger.summary()["overrun_requests"] == 0
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 200


async def test_upstream_429_releases_reservation(tmp_path):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(
        429, json={"error": {"code": "rate_limited"}}))
    status, payload = await gw.complete("editor", body(), "Bearer client-key")
    assert status == 429 and payload["error"]["code"] == "rate_limited"
    assert settlement(ledger) == (0, "refused")
    summary = ledger.summary()
    assert summary["failed_by_status"]["429"] == 1
    assert summary["released_refusals"] == 1
    assert summary["unknown_requests"] == 0
    assert summary["available_units"] == 195000
    assert not gw.exhausted()
    with ledger.connection() as db:
        assert db.execute(
            "SELECT r.id=rr.request_id,rr.status,rr.reserved,rr.authorization,rr.created IS NOT NULL "
            "FROM requests r JOIN released_reservations rr ON r.id=rr.request_id"
        ).fetchall() == [(1, 429, 195000, None, 1)]


@pytest.mark.parametrize("content", [b'{"error":{"code":"not_found"}}', b"<html>missing</html>", b"null"])
async def test_upstream_404_releases_reservation(tmp_path, content):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(404, content=content))
    await gw.complete("editor", body(), "Bearer client-key")
    assert settlement(ledger) == (0, "refused")
    summary = ledger.summary()
    assert summary["failed_by_status"]["404"] == 1
    assert summary["released_refusals"] == 1
    assert summary["available_units"] == 195000


async def test_upstream_503_keeps_reservation(tmp_path):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(
        503, json={"error": {"code": "unavailable"}}))
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 503
    assert settlement(ledger) == (None, None)
    summary = ledger.summary()
    assert summary["failed_by_status"]["503"] == 1
    assert summary["released_refusals"] == 0
    assert summary["available_units"] == 0


async def test_transport_error_keeps_reservation(tmp_path):
    def upstream(request):
        raise httpx.ReadTimeout("synthetic failure")

    gw, ledger = gateway(tmp_path, upstream)
    assert (await gw.complete("editor", body(), "Bearer client-key"))[0] == 502
    assert settlement(ledger) == (None, None)
    summary = ledger.summary()
    assert summary["failed_by_status"]["transport"] == 1
    assert summary["released_refusals"] == 0
    assert summary["available_units"] == 0


@pytest.mark.parametrize("content", [b"<html>oops</html>", b"null", b'{"choices":[]}'])
async def test_invalid_2xx_body_keeps_reservation(tmp_path, content):
    gw, ledger = gateway(tmp_path, lambda request: httpx.Response(200, content=content))
    await gw.complete("editor", body(), "Bearer client-key")
    assert settlement(ledger) == (None, None)
    summary = ledger.summary()
    assert summary["failed_by_status"]["200"] == 1
    assert summary["released_refusals"] == 0
    assert summary["available_units"] == 0


def test_release_old_refusals_by_authorization(tmp_path):
    import sqlite3
    from evolution_lab.budget import BudgetExhausted, BudgetLedger

    path = tmp_path / "budget.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE requests (id TEXT PRIMARY KEY, role TEXT NOT NULL, model TEXT NOT NULL,
                request_hash TEXT NOT NULL, reserved INTEGER NOT NULL, settled INTEGER,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO requests (id,role,model,request_hash,reserved,settled) VALUES
                ('first','editor','editor-model','a',100,NULL),
                ('second','editor','editor-model','b',200,NULL),
                ('third','editor','editor-model','c',300,NULL),
                ('fourth','editor','editor-model','d',400,NULL),
                ('settled','editor','editor-model','e',500,50);
            CREATE TABLE request_failures (request_id TEXT PRIMARY KEY, status INTEGER,
                code TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO request_failures(request_id,status,code) VALUES
                ('first',429,'rate_limited'),('second',429,'rate_limited'),
                ('third',502,'unavailable'),('fourth',404,'not_found'),('settled',404,'not_found');
            CREATE TABLE overruns (request_id TEXT PRIMARY KEY, reason TEXT NOT NULL);
            INSERT INTO overruns VALUES ('stopped','settled_exceeds_reservation');
            CREATE TABLE refusals (id TEXT PRIMARY KEY, requested INTEGER NOT NULL,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO refusals(id,requested) VALUES ('refused',1000);
        """)
    ledger = BudgetLedger(path, limit_units=2000, identity="legacy-release")
    assert ledger.summary()["released_refusals"] == 0
    assert ledger.summary()["reserved_upper_units"] == 1050
    assert ledger.release_refused_reservations(authorization="operator approval") == 3
    assert ledger.release_refused_reservations(authorization="repeat approval") == 0
    with ledger.connection() as db:
        assert db.execute("SELECT id,settled,settled_source FROM requests ORDER BY id").fetchall() == [
            ("first", 0, "refused"), ("fourth", 0, "refused"), ("second", 0, "refused"),
            ("settled", 50, None), ("third", None, None),
        ]
        assert db.execute(
            "SELECT request_id,status,reserved,authorization FROM released_reservations ORDER BY request_id"
        ).fetchall() == [
            ("first", 429, 100, "operator approval"),
            ("fourth", 404, 400, "operator approval"),
            ("second", 429, 200, "operator approval"),
        ]
        assert db.execute(
            "SELECT authorization,released_refusals FROM release_authorizations ORDER BY rowid"
        ).fetchall() == [("operator approval", 3), ("repeat approval", 0)]
        assert db.execute("SELECT * FROM overruns").fetchall() == [
            ("stopped", "settled_exceeds_reservation")]
        assert db.execute("SELECT id,requested FROM refusals").fetchall() == [("refused", 1000)]
    summary = ledger.summary()
    assert summary["released_refusals"] == 3
    assert summary["reserved_upper_units"] == 350
    assert summary["unknown_requests"] == 1
    assert summary["failed_by_status"] == {"429": 2, "502": 1, "404": 2, "transport": 0}
    with pytest.raises(BudgetExhausted, match="stopped"):
        ledger.reserve(1, role="editor", model="editor-model", request_hash="new")


@pytest.mark.parametrize("authorization", ["", "  "])
def test_release_requires_authorization(tmp_path, authorization):
    from evolution_lab.budget import BudgetLedger

    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=2000, identity="release-auth")
    with pytest.raises(ValueError, match="authorization"):
        ledger.release_refused_reservations(authorization=authorization)
