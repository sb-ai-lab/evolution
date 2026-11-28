import httpx
import pytest
from evolution_lab.budget import BudgetLedger, Gateway, Price, Route, BudgetExhausted

async def test_overrun_is_recorded_and_stops_next_request(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget", limit_units=10_000_000, identity="overrun")
    def upstream(request):
        assert request.headers["authorization"] == "Bearer upstream-secret"
        return httpx.Response(200, json={"usage": {"prompt_tokens": 2000, "completion_tokens": 5, "cost": .003}})
    # 3,000,000 billed units exceed both the reservation and the 2,005,000-unit tariff.
    route = Route("https://routerai.ru/api/v1", "upstream-secret", "model", Price(1, 1, 1000), 100,
                  client_token="client-secret")
    gateway = Gateway(ledger, {"editor": route}, transport=httpx.MockTransport(upstream))
    body = {"model": "model", "messages": [], "max_tokens": 100}
    assert (await gateway.complete("editor", body, "Bearer upstream-secret"))[0] == 401
    assert (await gateway.complete("editor", body, "Bearer client-secret"))[0] == 200
    summary = ledger.summary()
    assert summary["reserved_upper_units"] == 3_000_000
    assert summary["overrun_requests"] == 1
    assert summary["unknown_requests"] == 0
    assert (await gateway.complete("editor", body, "Bearer client-secret"))[0] == 429
    reopened = BudgetLedger(tmp_path / "budget", limit_units=10_000_000, identity="overrun")
    assert reopened.summary()["overrun_requests"] == 1
    with pytest.raises(BudgetExhausted):
        reopened.reserve(1, role="editor", model="model", request_hash="next")


def test_valid_usage_over_token_limits_is_not_unknown():
    price = Price(1, 1, 1000)
    assert price.tariff_units({"prompt_tokens": 2000, "completion_tokens": 101}) == 2_101_000


def test_route_never_uses_upstream_secret_as_client_token():
    route = Route("https://example.test/v1", "upstream-secret", "model", Price(1, 1, 1000), 100)
    assert route.client_token and route.client_token != route.api_key
    assert route.client_token not in repr(route)
    with pytest.raises(ValueError):
        Route("https://example.test/v1", "same", "model", Price(1, 1, 1000), 100, client_token="same")


def test_overrun_stop_cannot_be_cleared_by_account_floor(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget", limit_units=100, identity="overrun")
    reservation = ledger.reserve(10, role="editor", model="model", request_hash="test")
    ledger.settle(reservation, 21, source="upstream_cost", tariff_actual=20)
    ledger.activate_balance_floor(5, authorization="explicit floor")
    with pytest.raises(BudgetExhausted):
        ledger.reserve(1, role="editor", model="model", request_hash="test2")
    assert ledger.summary()["overrun_requests"] == 1


def _bounded_route(max_tokens=100, context_tokens=1000):
    return Route("https://example.test/v1", "upstream-secret", "model",
                 Price(1, 1, context_tokens), max_tokens, client_token="client-secret")


async def test_usage_beyond_declared_bounds_within_reservation_does_not_stop_dispatch(tmp_path):
    """Reasoning tokens past max_tokens are an observation, not an accounting breach."""
    ledger = BudgetLedger(tmp_path / "budget", limit_units=10_000_000, identity="bounds")
    def upstream(request):
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 101}})
    gateway = Gateway(ledger, {"editor": _bounded_route()}, transport=httpx.MockTransport(upstream))
    body = {"model": "model", "messages": [], "max_tokens": 100}
    assert (await gateway.complete("editor", body, "Bearer client-secret"))[0] == 200
    summary = ledger.summary()
    assert summary["overrun_requests"] == 0
    assert summary["bound_observations"] == 1
    assert summary["refusals"] == 0
    assert not gateway.exhausted()
    assert (await gateway.complete("editor", body, "Bearer client-secret"))[0] == 200


async def test_latched_dispatch_reports_budget_exhausted_under_an_account_floor(tmp_path):
    """An accounting stop must never be reported as a reached account balance floor."""
    ledger = BudgetLedger(tmp_path / "budget", limit_units=10_000_000, identity="latch")
    def upstream(request):
        return httpx.Response(200, json={"usage": {"prompt_tokens": 2000, "completion_tokens": 5, "cost": .003}})
    # 3,000,000 billed units exceed both the reservation and the 2,005,000-unit tariff.
    route = Route("https://routerai.ru/api/v1", "upstream-secret", "model", Price(1, 1, 1000), 100,
                  client_token="client-secret")
    gateway = Gateway(ledger, {"editor": route}, transport=httpx.MockTransport(upstream))
    body = {"model": "model", "messages": [], "max_tokens": 100}
    assert (await gateway.complete("editor", body, "Bearer client-secret"))[0] == 200
    assert ledger.summary()["overrun_requests"] == 1
    ledger.activate_balance_floor(5, authorization="operator floor")
    status, payload = await gateway.complete("editor", body, "Bearer client-secret")
    assert (status, payload["error"]["code"]) == (429, "budget_exhausted")


def test_authorize_resume_clears_the_stop_and_keeps_an_audit_trail(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget", limit_units=100, identity="resume")
    reservation = ledger.reserve(10, role="editor", model="model", request_hash="one")
    ledger.settle(reservation, 21, source="upstream_cost", tariff_actual=20, bounds_exceeded=True)
    assert ledger.summary()["overrun_requests"] == 1
    with pytest.raises(BudgetExhausted):
        ledger.reserve(1, role="editor", model="model", request_hash="two")
    ledger.authorize_resume(authorization="operator reviewed the provider invoice")
    summary = ledger.summary()
    assert summary["overrun_requests"] == 0
    assert summary["refusals"] == 0
    assert summary["bound_observations"] == 1  # observations are history, not a stop
    assert ledger.reserve(1, role="editor", model="model", request_hash="two")
    reopened = BudgetLedger(tmp_path / "budget", limit_units=100, identity="resume")
    assert reopened.resume_authorizations() == ["operator reviewed the provider invoice"]


def test_authorize_resume_requires_explicit_authorization(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget", limit_units=100, identity="resume")
    with pytest.raises(ValueError):
        ledger.authorize_resume(authorization="")
