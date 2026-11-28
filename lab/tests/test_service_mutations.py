"""S7 W28: header keys, canonical fingerprints and the per-generation table."""
from __future__ import annotations

import asyncio

import pytest
from starlette.datastructures import Headers

from evolution_lab.owner import OwnerError
from evolution_lab.service.errors import ServiceError


def _headers(*pairs):
    return Headers(raw=[(k.encode(), v.encode()) for k, v in pairs])


def test_decode_key_both_or_none():
    from evolution_lab.service.mutations import MutationKey, decode_mutation_key
    assert decode_mutation_key(_headers()) is None
    assert decode_mutation_key(_headers(("Lab-Request-Id", "r-1:a.b"), ("Lab-Owner-Generation", "gen"))) == \
        MutationKey("r-1:a.b", "gen")


@pytest.mark.parametrize("pairs", [
    [("Lab-Request-Id", "r")],
    [("Lab-Owner-Generation", "gen")],
    [("Lab-Request-Id", "r"), ("Lab-Request-Id", "r"), ("Lab-Owner-Generation", "gen")],
    [("Lab-Request-Id", "r"), ("Lab-Owner-Generation", "gen"), ("Lab-Owner-Generation", "gen")],
    [("Lab-Request-Id", "has space"), ("Lab-Owner-Generation", "gen")],
    [("Lab-Request-Id", "x" * 129), ("Lab-Owner-Generation", "gen")],
    [("Lab-Request-Id", "r"), ("Lab-Owner-Generation", "../gen")],
])
def test_decode_key_refusals_are_invalid_request(pairs):
    from evolution_lab.service.mutations import decode_mutation_key
    with pytest.raises(ServiceError) as caught:
        decode_mutation_key(_headers(*pairs))
    assert caught.value.code == "invalid_request"


def test_reject_key_on_reads():
    from evolution_lab.service.mutations import reject_mutation_key
    reject_mutation_key(_headers())
    with pytest.raises(ServiceError):
        reject_mutation_key(_headers(("lab-request-id", "r")))


def test_fingerprint_is_canonical_and_covers_route_path_query_body():
    from evolution_lab.service.mutations import fingerprint
    a = fingerprint("check", path={"connection_id": "c"}, body={"b": 1, "a": {"y": 2, "x": 1}})
    b = fingerprint("check", path={"connection_id": "c"}, body={"a": {"x": 1, "y": 2}, "b": 1})
    assert a == b and len(a) == 32
    assert a != fingerprint("conformance", path={"connection_id": "c"}, body={"b": 1, "a": {"y": 2, "x": 1}})
    assert a != fingerprint("check", path={"connection_id": "d"}, body={"b": 1, "a": {"y": 2, "x": 1}})
    assert fingerprint("stop_target", path={"target_id": "t"}, query={"owner_generation": "g"}) != \
        fingerprint("stop_target", path={"target_id": "t"}, query={"owner_generation": "h"})
    assert fingerprint("delete_results", body=None) != fingerprint("delete_results", body={})


def test_fingerprint_is_total_for_lone_surrogates():
    from evolution_lab.service.mutations import fingerprint
    body = {"reason": "\ud800", "env": {"K": "x\udfff"}}
    assert fingerprint("release", body=body) == fingerprint("release", body=dict(body))
    assert fingerprint("release", body={"reason": "\ud800"}) != fingerprint("release", body={"reason": "\udc00"})
    assert fingerprint("release", body={"reason": "\ud800"}) != fingerprint("release", body={"reason": "\\ud800"})


def test_route_classes_cover_declared_owner_work_and_lifecycle_routes():
    from evolution_lab.service.mutations import ROUTE_CLASSES
    assert sorted(k for k, v in ROUTE_CLASSES.items() if v == "work") == sorted(
        ["create_bundle", "prepare", "bind", "start_target", "check", "conformance", "start", "export", "resume",
         "confirm_execution_policy"])
    assert sorted(k for k, v in ROUTE_CLASSES.items() if v == "lifecycle") == sorted(
        ["stop_target", "stop_task", "stop_operation", "delete_results", "cleanup", "release"])


@pytest.mark.asyncio
async def test_table_lookup_conflict_and_limits_per_class():
    from evolution_lab.service.mutations import MutationTable
    table = MutationTable(work_limit=1, lifecycle_limit=1)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    table.record("k1", "check", b"1" * 32, future)
    assert table.lookup("k1", "check", b"1" * 32) is future
    assert table.lookup("absent", "check", b"1" * 32) is None
    for route, digest in (("check", b"2" * 32), ("conformance", b"1" * 32)):
        with pytest.raises(ServiceError) as caught:
            table.lookup("k1", route, digest)
        assert caught.value.code == "request_conflict"
    with pytest.raises(ServiceError) as caught:
        table.ensure_capacity("start")
    assert caught.value.code == "request_table_full"
    table.ensure_capacity("cleanup")  # lifecycle is independent
    future.set_result("done")


@pytest.mark.asyncio
async def test_not_found_after_dispatch_removes_entry_other_failures_stay():
    from evolution_lab.service.mutations import MutationTable
    table = MutationTable(work_limit=2, lifecycle_limit=1)
    loop = asyncio.get_running_loop()
    missing, busy = loop.create_future(), loop.create_future()
    table.record("missing", "start", b"1" * 32, missing)
    table.record("busy", "start", b"2" * 32, busy)
    missing.set_exception(OwnerError("not_found", "read"))
    busy.set_exception(OwnerError("busy", "before_start"))
    await asyncio.sleep(0)
    for future in (missing, busy):
        future.exception()
    assert table.lookup("missing", "start", b"1" * 32) is None
    assert table.lookup("busy", "start", b"2" * 32) is busy
    assert len(table) == 1
    table.ensure_capacity("start")  # the removed entry released its slot
