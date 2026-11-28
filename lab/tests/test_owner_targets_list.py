"""Task 7: target list (store, route, CLI validator) and pick_ready_target."""
from __future__ import annotations

import httpx
import pytest

from evolution_lab.owner.resume_rules import pick_ready_target
from evolution_lab.owner.store import OwnerStateStore
from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

T = lambda i, state="ready", bundle="b" * 64, gen="g1": {"id": i, "owner_generation": gen,
                                                        "state": state, "bundle_digest": bundle}
EX = [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g1"}]


def test_picks_first_ready_same_bundle_not_excluded():
    targets = [T("t1"), T("t2", state="stopped"), T("t3", bundle="c" * 64), T("t4"), T("t5")]
    assert pick_ready_target(targets, EX) == {"id": "t4", "owner_generation": "g1"}


def test_none_when_only_excluded_or_other_bundles():
    assert pick_ready_target([T("t1"), T("t3", bundle="c" * 64)], EX) is None
    assert pick_ready_target([], EX) is None


def test_same_id_other_generation_is_not_excluded():
    assert pick_ready_target([T("t1"), T("t1", gen="g2")], EX) == {"id": "t1", "owner_generation": "g2"}


def _start(store, bundle, client_ref=None):
    connection = store.bind_connection(source_kind="campaign", bundle_digest=bundle, launch_digest="c" * 64)
    reservation = store.accept_target_start(connection_id=connection.id, launch_digest="c" * 64,
                                            bundle_digest=bundle, base_url="http://127.0.0.1:9", client_ref=client_ref)
    store.mark_target_ready(reservation.task_ref.id, boot_id="boot")
    return reservation.target_ref


def _stop(store, ref):
    """Stopped and out of the one-target slot (the real stop path frees it; a direct read test need not)."""
    store.set_target_state(ref.id, state="stopped")
    with store._database:
        store._database.execute("DELETE FROM reservations")


def test_store_list_targets_filters_and_keeps_rowid_order(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        first = _start(store, "b" * 64, client_ref='{"implementation":"agent","project":"acq"}')
        _stop(store, first)
        second = _start(store, "d" * 64)
        _stop(store, second)
        third = _start(store, "b" * 64)
        assert [t.id for t in store.list_targets(None, None)] == [first.id, second.id, third.id]
        assert [t.id for t in store.list_targets("b" * 64, None)] == [first.id, third.id]
        assert [t.id for t in store.list_targets(None, "ready")] == [third.id]
        assert [t.id for t in store.list_targets("d" * 64, "ready")] == []
        assert all("client_ref" not in t.to_payload() for t in store.list_targets(None, None))


class FakeOwner:
    owner_generation = "gen-1"

    def __init__(self):
        self.calls = []

    async def list_targets(self, *, bundle=None, state=None):
        self.calls.append((bundle, state))
        from evolution_lab.owner.models import CleanupFacts, TargetStatus
        return (TargetStatus("t1", "gen-1", "c" * 64, "b" * 64, "ready", "http://127.0.0.1:9", "boot", None,
                             CleanupFacts.pending(), None),)


@pytest.fixture
async def service(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2, home_directory=tmp_path / "home"))
    runtime.owner = FakeOwner()
    runtime._admission_open = True
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        yield runtime, client


async def test_route_lists_targets_with_the_list_schema(service):
    runtime, client = service
    response = await client.get("/v1/targets", params={"bundle": "b" * 64, "state": "ready"})
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "lab-owner-target-list/1"
    assert [t["schema"] for t in body["targets"]] == ["lab-owner-target/2"]
    assert "client_ref" not in body["targets"][0]
    assert runtime.owner.calls == [("b" * 64, "ready")]
    from evolution_lab.cli.responses import validate
    validate(body, "lab-owner-target-list/1")


async def test_route_rejects_unknown_state_and_params(service):
    _, client = service
    assert (await client.get("/v1/targets", params={"state": "stopped"})).status_code == 400
    assert (await client.get("/v1/targets", params={"x": "1"})).status_code == 400


async def test_owner_ready_filter_follows_the_live_status():
    """A live target already ready while its durable row still lags is listed as ready;
    a durable ``ready`` row whose live status moved on is not."""
    from dataclasses import replace
    from types import SimpleNamespace

    from evolution_lab.owner.api import Owner
    from evolution_lab.owner.models import CleanupFacts, TargetStatus

    def target(i, state):
        return TargetStatus(i, "gen-1", "c" * 64, "b" * 64, state, "http://127.0.0.1:9", "boot", None,
                            CleanupFacts.pending(), None)

    lagging, stopped = target("t1", "starting"), target("t2", "ready")
    calls = []

    async def db(name, bundle, state):
        calls.append((name, bundle, state))
        return (lagging, stopped)

    fake = SimpleNamespace(_assert_loop=lambda: None, _db=db,
                           _target_cache={"t1": replace(lagging, state="ready"),
                                          "t2": replace(stopped, state="stopping")})
    assert [t.id for t in await Owner.list_targets(fake, state="ready")] == ["t1"]
    assert calls == [("list_targets", None, None)]
