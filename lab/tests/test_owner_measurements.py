"""Measurement views (spec 2026-09-29 §2.4): means, Δ by (case_id, sample_id) pairs, target tokens."""
import asyncio
import contextlib
import io
import json
import threading
import types

import pytest

from evolution_lab.owner.measurements import JournalFacts, measurement_rows, metric_means

from test_owner_core_raw import finished_campaign, run_finished_campaign  # noqa: F401 — module fixture

pytestmark = pytest.mark.slow


def _s(case, sample, score, state="finished", critical=True):
    return {"case_id": case, "sample_id": sample, "execution_state": "finished", "scoring_state": state,
            "metrics": {} if score is None else {"score": score}, "critical_pass": critical}


def _entry(mid, samples, excluded=None, **extra):
    return {"measurement_id": mid, "candidate_digest": "d" * 64, "locator": f"measurements/{mid}/events.jsonl",
            "excluded_pairs": excluded, "classification": "measured", "call_retries": 0,
            "call_failures_by_code": {},
            "status": {"run_id": mid, "last_sequence": 9, "complete": True, "samples": samples}, **extra}


BASE = _entry("m-r00-champion", [_s("q1", "01", 0.0), _s("q2", "01", 1.0), _s("q3", "01", 0.5)])
CAND = _entry("m-r01-a01", [_s("q1", "01", 1.0), _s("q2", "01", 1.0), _s("q3", "01", 0.0),
                            _s("q4", "01", 1.0)])
CORE = {"rounds": [{"round": 1, "baseline_measurement_id": "m-r00-champion",
                    "attempts": [{"attempt_id": "r01-a01", "measurement_id": "m-r01-a01"}]}],
        "measurements": [BASE, CAND]}
FACTS = {"m-r00-champion": JournalFacts("2026-09-29T10:00:00Z", "2026-09-29T10:05:00Z", 9,
                                        {"input_tokens": 1, "output_tokens": 2, "unknown_calls": 0,
                                         "coverage": "target_calls_only"}),
         "m-r01-a01": None}


def test_metric_means_count_only_scored():
    assert metric_means([_s("a", "1", 1.0), _s("b", "1", 0.0), _s("c", "1", 5.0, state="started")]) == \
        {"score": {"mean": 0.5, "count": 2}}


def test_pairs_are_case_and_sample():
    rows = {r["measurement_id"]: r for r in measurement_rows(CORE, "campaign", FACTS, "score")}
    cand = rows["m-r01-a01"]
    assert cand["role"] == "attempt" and cand["round"] == 1 and cand["attempt_id"] == "r01-a01"
    assert cand["baseline"]["state"] == "present"
    assert cand["baseline"]["cases"] == {"better": 1, "same": 1, "worse": 1, "unpaired": 1}
    assert cand["baseline"]["delta"] == {"score": {"mean": 0.5 / 3, "pairs": 3}}
    assert rows["m-r00-champion"]["role"] == "baseline" and rows["m-r00-champion"]["baseline"] is None


def test_same_sample_id_in_two_cases_is_two_pairs():
    base = _entry("b", [_s("q1", "01", 0.0), _s("q2", "01", 0.0)])
    cand = _entry("a", [_s("q1", "01", 1.0), _s("q2", "01", 0.0)])
    core = {"rounds": [{"round": 1, "baseline_measurement_id": "b",
                        "attempts": [{"attempt_id": "r01-a01", "measurement_id": "a"}]}],
            "measurements": [base, cand]}
    row = next(r for r in measurement_rows(core, "campaign", {}, "score") if r["measurement_id"] == "a")
    assert row["baseline"]["cases"] == {"better": 1, "same": 1, "worse": 0, "unpaired": 0}


def test_excluded_pairs_leave_the_comparison():
    excluded = {"count": 1, "by_owner": {}, "truncated": False,
                "pairs": [{"case_id": "q1", "sample_id": "01", "owner": "target", "reason": "x", "tries": 3}]}
    core = dict(CORE, measurements=[BASE, dict(CAND, excluded_pairs=excluded)])
    row = next(r for r in measurement_rows(core, "campaign", {}, "score") if r["role"] == "attempt")
    assert row["baseline"]["cases"] == {"better": 0, "same": 1, "worse": 1, "unpaired": 1}
    assert row["coverage"]["excluded_pairs"] == 1


def test_truncated_exclusions_give_no_delta():
    excluded = {"count": 300, "by_owner": {}, "truncated": True, "pairs": []}
    core = dict(CORE, measurements=[BASE, dict(CAND, excluded_pairs=excluded)])
    row = next(r for r in measurement_rows(core, "campaign", {}, "score", detail="m-r01-a01"))
    assert row["baseline"] == {"measurement_id": "m-r00-champion", "state": "exclusions_truncated",
                               "delta": None, "cases": None}
    assert {s["comparison"] for s in row["samples"]} == {"unknown"}
    assert row["metrics"]["score"]["count"] == 4  # the measurement's own means stay


def test_usage_and_times_states():
    rows = {r["measurement_id"]: r for r in measurement_rows(CORE, "campaign", FACTS, "score")}
    assert rows["m-r00-champion"]["usage_state"] == "present"
    assert rows["m-r00-champion"]["times"] == {"started_at": "2026-09-29T10:00:00Z",
                                               "last_event_at": "2026-09-29T10:05:00Z"}
    assert rows["m-r01-a01"]["usage"] is None and rows["m-r01-a01"]["usage_state"] == "unavailable"
    assert rows["m-r01-a01"]["times"] is None


def test_detail_has_samples_with_baseline():
    row = measurement_rows(CORE, "campaign", {}, "score", detail="m-r01-a01")[0]
    q1 = next(s for s in row["samples"] if s["case_id"] == "q1")
    assert q1["baseline"] == {"metrics": {"score": 0.0}, "critical_pass": True}
    assert q1["comparison"] == "better"
    q4 = next(s for s in row["samples"] if s["case_id"] == "q4")
    assert q4["baseline"] is None and q4["comparison"] == "unpaired"


def test_evaluation_has_one_measurement():
    core = {"run_id": "e1", "samples": [_s("q1", "01", 1.0)]}
    (row,) = measurement_rows(core, "evaluation", {"e1": None}, None)
    assert row["role"] == "evaluation" and row["baseline"] is None and row["round"] is None


def test_cache_key_separates_operations_with_the_same_measurement_id():
    from evolution_lab.owner.measurements import MeasurementCache
    cache, calls = MeasurementCache(), []
    load = lambda tag: (lambda: calls.append(tag) or JournalFacts(None, None, 1, None))
    cache.get(("op1", "run", "m-r01-a01", 1), load("a"))
    cache.get(("op2", "run", "m-r01-a01", 1), load("b"))
    cache.get(("op1", "run", "m-r01-a01", 1), load("c"))
    assert calls == ["a", "b"]
    cache.drop("op1")
    cache.get(("op1", "run", "m-r01-a01", 1), load("d"))
    assert calls == ["a", "b", "d"]


@pytest.fixture
def service_cli_with_finished_campaign(finished_campaign):
    """The real Owner behind the service app on a loop thread; the CLI's own client reaches it
    through a bridging transport, so ``cli`` and ``route`` read the same routes."""
    import httpx

    from evolution_lab.cli import main
    from evolution_lab.cli.client import ServiceClient
    from evolution_lab.owner import Owner
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

    state, operation_id, _ = finished_campaign
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    run = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)
    stack = contextlib.AsyncExitStack()

    async def open_service():
        owner = await stack.enter_async_context(Owner.open(state))
        runtime = ServiceRuntime(ServiceConfig("/tmp/lab-measurements.sock", state, 0.2))
        runtime.owner, runtime._admission_open = owner, True
        open_service.owner = owner
        return await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://lab"))

    client = run(open_service())

    async def forward(request):
        response = await client.request(request.method, request.url.raw_path.decode(),
                                        headers=request.headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    transport = httpx.MockTransport(lambda request: run(forward(request)))

    def cli(*argv, check=True, raw=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["--socket", "/tmp/lab-measurements.sock", "--json", *argv], stdout=stdout, stderr=stderr,
                    client_factory=lambda **kwargs: ServiceClient(transport=transport, **kwargs))
        if raw:
            return types.SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())
        if check:
            assert code == 0, stderr.getvalue()
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def route(path, status=200):
        response = run(client.get(path))
        assert response.status_code == status
        return response.json()

    from evolution_lab.owner import ResourceRef
    route.owner, route.run, route.ref = open_service.owner, run, ResourceRef("operation", operation_id)
    try:
        yield cli, route, operation_id
    finally:
        run(stack.aclose())
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


def test_owner_measurements_routes_match_cli(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    listed = cli("measurements", operation)[-1]
    assert listed["schema"] == "lab-measurement-list/4" and listed == route(f"/v1/operations/{operation}/measurements")
    mid = listed["measurements"][0]["measurement_id"]
    one = cli("measurements", operation, mid)[-1]
    assert one["schema"] == "lab-measurement/4" and "samples" in one["measurement"]


@pytest.fixture
def own_finished_campaign(tmp_path):
    """A campaign of its own: deleting its results must not reach the module's shared one."""
    return run_finished_campaign(tmp_path)


async def test_deleting_results_drops_the_measurement_cache(own_finished_campaign):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    state, operation_id, _ = own_finished_campaign
    ref = ResourceRef("operation", operation_id)
    async with Owner.open(state) as owner:
        listed = await owner.measurements(ref)
        assert listed["measurements"]
        assert any(k[0] == ref.id for k in owner._measurements._rows)
        task = await owner.wait(await owner.delete_results(ref))
        assert task.result.payload["state"] == "deleted"
        assert not any(k[0] == ref.id for k in owner._measurements._rows)
        with pytest.raises(OwnerError) as refused:
            await owner.measurements(ref)
        assert refused.value.code == "results_deleted"


def test_unknown_primary_metric_leaves_the_baseline_unavailable():
    """Policy unread (metric_primary None): no first-metric guess, Δ and cases are unavailable."""
    rows = {r["measurement_id"]: r for r in measurement_rows(CORE, "campaign", {}, None, detail="m-r01-a01")}
    cand = rows["m-r01-a01"]
    assert cand["baseline"] == {"measurement_id": "m-r00-champion", "state": "unavailable",
                                "delta": None, "cases": None}
    assert {s["comparison"] for s in cand["samples"]} == {"unknown"}
    assert cand["metrics"]["score"]["count"] == 4  # the measurement's own means stay
    base = measurement_rows(CORE, "campaign", {}, None)[0]
    assert base["role"] == "baseline" and base["baseline"] is None


def test_times_and_facts_survive_one_unreadable_journal(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from evolution_lab.owner import core_raw, measurements, readers
    good = JournalFacts("2026-09-29T10:00:00Z", "2026-09-29T10:05:00Z", 9, None)

    def fake(directory):
        if directory.name == "m-r00-champion":
            raise OSError("corrupt journal")
        return good

    monkeypatch.setattr(measurements, "read_journal_facts", fake)
    monkeypatch.setattr(core_raw, "campaign_journal_times", lambda run: None)
    run = tmp_path / "artifacts" / "op" / "op"
    run.mkdir(parents=True)
    status = NS(id="op", kind="campaign", process=NS(launched=True), result=NS(core=CORE))
    view, facts = readers._times_and_facts(tmp_path, status, measurements.MeasurementCache())
    assert set(facts) == {"m-r01-a01"}
    assert view["times_state"] == "unavailable" and view["times_reason"] == "unreadable"
    rows = {r["measurement_id"]: r for r in measurement_rows(CORE, "campaign", facts, "score")}
    assert rows["m-r00-champion"]["usage_state"] == "unavailable" and rows["m-r00-champion"]["times"] is None
    assert rows["m-r01-a01"]["times"] == {"started_at": good.first_at, "last_event_at": good.last_at}


def test_repairs_come_from_core_counters():
    core = dict(CORE, measurements=[dict(BASE, repair_tries=3, repairs_scored=1), CAND])
    rows = {r["measurement_id"]: r for r in measurement_rows(core, "campaign", {}, "score")}
    assert rows["m-r00-champion"]["repairs"] == {"tries": 3, "pairs_scored": 1}


def test_repairs_zero_is_an_object_not_null():
    core = dict(CORE, measurements=[dict(BASE, repair_tries=0, repairs_scored=0), CAND])
    row = next(r for r in measurement_rows(core, "campaign", {}, "score") if r["role"] == "baseline")
    assert row["repairs"] == {"tries": 0, "pairs_scored": 0}


def test_repairs_null_when_a_key_is_missing():
    core = dict(CORE, measurements=[dict(BASE, repair_tries=2), CAND])
    rows = {r["measurement_id"]: r for r in measurement_rows(core, "campaign", {}, "score")}
    assert rows["m-r00-champion"]["repairs"] is None
    assert rows["m-r01-a01"]["repairs"] is None


def test_evaluation_has_no_repairs():
    core = {"run_id": "e1", "candidate_digest": "d" * 64, "samples": [_s("q1", "01", 1.0)],
            "repair_tries": 1, "repairs_scored": 1}
    (row,) = measurement_rows(core, "evaluation", {}, "score")
    assert row["repairs"] is None


CASES = 3
MID15 = "m-r01-a01"


def CORE15(repeats):
    samples = [_s(f"q{c}", f"{r:02d}", 1.0) for c in range(CASES) for r in range(1, repeats + 1)]
    return {"family_version": 15,
            "rounds": [{"round": 1, "baseline_measurement_id": "m-r00-champion",
                        "attempts": [{"attempt_id": "r01-a01", "measurement_id": MID15}]}],
            "measurements": [BASE, _entry(MID15, samples)]}


@pytest.mark.parametrize("repeats", [1, 3])
def test_family_15_measurement_has_no_lab_delta(repeats):
    rows = measurement_rows(CORE15(repeats), "campaign", {}, "score", detail=MID15)
    assert rows[0]["baseline"] is None and {s["comparison"] for s in rows[0]["samples"]} == {"unknown"}
    assert len(rows[0]["samples"]) == CASES * repeats
