"""An ordered provider set routes within its members only, and is recorded whole."""
import json
import sqlite3

import httpx
import pytest


def route(**kwargs):
    from evolution_lab.budget import Price, Route
    return Route(
        "https://routerai.ru/api/v1", "synthetic-key", "vendor/model", Price(1, 1, 1000), 100,
        client_token="client-key", **kwargs,
    )


def gateway(tmp_path, handler, **kwargs):
    from evolution_lab.budget import BudgetLedger, Gateway
    ledger = BudgetLedger(tmp_path / "budget.sqlite", limit_units=10**9, identity="offline-test")
    return Gateway(ledger, {"agent": route(**kwargs)}, transport=httpx.MockTransport(handler)), ledger


def body():
    return {"model": "vendor/model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}


def upstream_capture(sent, reported="novita"):
    def upstream(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [], "provider": reported, "model": "vendor/model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
    return upstream


async def test_provider_set_sends_only_and_order_in_route_order(tmp_path):
    sent = []
    gw, _ = gateway(tmp_path, upstream_capture(sent), providers=("nextbit", "novita", "deepinfra"))
    assert (await gw.complete("agent", body(), "Bearer client-key"))[0] == 200
    assert sent[0]["provider"] == {
        "only": ["nextbit", "novita", "deepinfra"],
        "order": ["nextbit", "novita", "deepinfra"],
        "allow_fallbacks": False,
    }


async def test_single_provider_body_is_unchanged(tmp_path):
    sent = []
    gw, _ = gateway(tmp_path, upstream_capture(sent, "deepinfra"), provider="deepinfra")
    assert (await gw.complete("agent", body(), "Bearer client-key"))[0] == 200
    assert sent[0]["provider"] == {"only": ["deepinfra"], "allow_fallbacks": False}


@pytest.mark.parametrize("kwargs", [
    {"providers": ()},
    {"providers": ("novita", "novita")},
    {"providers": ("novita", "Bad Name")},
    {"providers": ("novita", "")},
    {"providers": ["novita"]},
    {"provider": "novita", "providers": ("nextbit",)},
])
def test_invalid_provider_sets_are_refused_at_route_creation(kwargs):
    with pytest.raises(ValueError):
        route(**kwargs)


def test_provider_set_requires_a_routing_protocol():
    from evolution_lab.budget import Price, Route
    with pytest.raises(ValueError):
        Route("https://example.test/v1", "synthetic-key", "vendor/model", Price(1, 1, 1000), 100,
              client_token="client-key", providers=("novita",))


async def test_routing_observation_keeps_the_whole_set_and_reported_provider(tmp_path):
    gw, ledger = gateway(tmp_path, upstream_capture([], "novita"), providers=("nextbit", "novita"))
    assert (await gw.complete("agent", body(), "Bearer client-key"))[0] == 200
    with ledger.connection() as db:
        rows = db.execute(
            "SELECT requested_provider, requested_providers, reported_provider FROM routing_observations"
        ).fetchall()
    assert rows == [(None, '["nextbit","novita"]', "novita")]


async def test_single_provider_observation_keeps_its_name(tmp_path):
    gw, ledger = gateway(tmp_path, upstream_capture([], "deepinfra"), provider="deepinfra")
    assert (await gw.complete("agent", body(), "Bearer client-key"))[0] == 200
    with ledger.connection() as db:
        rows = db.execute(
            "SELECT requested_provider, requested_providers, reported_provider FROM routing_observations"
        ).fetchall()
    assert rows == [("deepinfra", None, "deepinfra")]


def test_existing_ledger_gains_the_provider_set_column(tmp_path):
    from evolution_lab.budget import BudgetLedger
    path = tmp_path / "budget.sqlite"
    BudgetLedger(path, limit_units=10, identity="one")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE routing_observations")
        db.execute(
            "CREATE TABLE routing_observations (request_id TEXT PRIMARY KEY, requested_provider TEXT, reported_provider TEXT, reported_model TEXT)"
        )
        db.execute("INSERT INTO routing_observations VALUES ('old','deepinfra','deepinfra','m')")
    ledger = BudgetLedger(path, limit_units=10, identity="one")
    ledger.record_routing("new", ("nextbit", "novita"))
    with ledger.connection() as db:
        rows = db.execute(
            "SELECT request_id, requested_provider, requested_providers FROM routing_observations ORDER BY request_id"
        ).fetchall()
    assert rows == [("new", None, '["nextbit","novita"]'), ("old", "deepinfra", None)]


def test_ledger_without_failure_table_opens_with_zero_counters(tmp_path):
    import sqlite3
    from evolution_lab.budget import BudgetLedger
    path = tmp_path / "old.sqlite3"
    BudgetLedger(path, limit_units=10, identity="one")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE request_failures")
    summary = BudgetLedger(path, limit_units=10, identity="one").summary()
    assert summary["failed_requests"] == 0 and summary["failed_by_status"] == {"transport": 0}
