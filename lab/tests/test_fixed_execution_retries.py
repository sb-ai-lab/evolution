"""Fixed samples use Core's retry lifecycle and retain incomplete coverage."""
import pytest

from evolution.tree import RunIdentityV1
from evolution.tree.execution.interaction import RolloutInfrastructureError, TurnResult
from evolution.tree.execution.usage import Usage

from evolution_lab._fixed_execution import prepare_fixed, run_fixed
from test_evaluation import bindings


def retry_bindings(tmp_path, *, sample_retries=1, failures=(2,), recover=False,
                   failure="target", repeats=5):
    kwargs = bindings(tmp_path, repeats=repeats)
    if sample_retries is not None:
        kwargs["identity"] = RunIdentityV1({
            **kwargs["identity"].to_payload(), "sample_retries": sample_retries,
        })
    instance = kwargs["instance"]
    original_send = instance.send
    attempts = {}

    async def send(session, message, request_id):
        seed = session.requested_seed
        attempts[seed] = attempts.get(seed, 0) + 1
        failing = seed in failures and (not recover or attempts[seed] == 1)
        if failing and failure in {"infrastructure", "call"}:
            instance.sends += 1
            raise RolloutInfrastructureError(
                "PRIVATE-ERROR", "transport_error", retryable=True,
            )
        if failing and failure == "target":
            instance.sends += 1
            return TurnResult(events=(), usage=Usage(), latency_ms=0, outcome="target_failed")
        return await original_send(session, message, request_id)

    instance.send = send
    if failure == "scoring":
        original_score = kwargs["scorer"].score
        scoring_calls = 0

        async def score(case, view, candidate):
            nonlocal scoring_calls
            scoring_calls += 1
            if scoring_calls == 1:
                raise ValueError("PRIVATE-SCORER-ERROR")
            return await original_score(case, view, candidate)

        kwargs["scorer"].score = score
    if failure == "call":
        kwargs["evaluation_budget"] = {
            "max_failed_turns": 1,
            "call_retries": {"max_attempts": 1, "turn_max_attempts": 1},
        }
    return kwargs, attempts


async def execute(kwargs):
    measurement = prepare_fixed(**kwargs)
    with measurement.journal:
        result = await run_fixed(measurement)
        tries = {
            sample.sample_id: measurement.journal.sample_tries(case.case_id, sample.sample_id)
            for case, sample in measurement.schedule
        }
    return result, tries


async def test_indeterminate_sample_retried_then_continues(tmp_path):
    kwargs, attempts = retry_bindings(tmp_path)
    result, tries = await execute(kwargs)
    assert [row.payload["retry_ordinal"] for row, _ in tries["sample-2"]] == [0, 1]
    assert attempts == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}
    assert result["coverage"] == {"expected": 5, "scored": 4}
    assert result["unmeasured"] == [
        {"case_id": "c", "sample_id": "sample-2", "outcome": "target_failed", "tries": 2},
    ]
    assert result["stopped_reason"] is None
    assert [row["scoring_state"] for row in result["core"]["samples"]] == [
        "finished", "not_started", "finished", "finished", "finished",
    ]


@pytest.mark.parametrize("failure", ["target", "scoring"])
@pytest.mark.parametrize("concurrency", [1, 3])
async def test_retry_succeeds_second_try(tmp_path, failure, concurrency):
    kwargs, attempts = retry_bindings(tmp_path, failures=(1,), recover=True, failure=failure)
    kwargs["identity"] = RunIdentityV1({
        **kwargs["identity"].to_payload(), "max_concurrency": concurrency,
    })
    result, tries = await execute(kwargs)
    assert len(tries["sample-1"]) == 2
    assert [scoring.payload["retry_ordinal"] for _, scoring in tries["sample-1"]
            if scoring is not None] == ([1] if failure == "target" else [0, 1])
    assert attempts == {1: 2, 2: 1, 3: 1, 4: 1, 5: 1}
    assert result["coverage"] == {"expected": 5, "scored": 5}
    assert result["unmeasured"] == []
    assert result["stopped_reason"] is None


async def test_target_unstable_exception_is_sample_outcome(tmp_path):
    kwargs, attempts = retry_bindings(tmp_path, failure="call")
    result, tries = await execute(kwargs)
    assert [row.payload["outcome"] for row, _ in tries["sample-2"]] == ["target_failed"] * 2
    assert attempts == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}
    assert result["unmeasured"] == [
        {"case_id": "c", "sample_id": "sample-2", "outcome": "target_failed", "tries": 2},
    ]
    assert result["coverage"] == {"expected": 5, "scored": 4}
    assert result["stopped_reason"] is None
    assert "PRIVATE-ERROR" not in str(result)


async def test_infrastructure_failed_stops_even_when_closed(tmp_path):
    kwargs, attempts = retry_bindings(tmp_path, failure="infrastructure")
    result, tries = await execute(kwargs)
    assert tries["sample-2"][0][0].payload["outcome"] == "infrastructure_failed"
    assert attempts == {1: 1, 2: 1}
    assert tries["sample-3"] == ()
    assert result["coverage"] == {"expected": 5, "scored": 1}
    assert result["stopped_reason"] == "infrastructure_failed"


async def test_three_consecutive_unmeasured_stop(tmp_path):
    # The score on sample 3 resets the counter; stop after samples 4, 5, 6.
    kwargs, attempts = retry_bindings(tmp_path, failures=(1, 2, 4, 5, 6, 7), repeats=7)
    result, tries = await execute(kwargs)
    assert attempts == {1: 2, 2: 2, 3: 1, 4: 2, 5: 2, 6: 2}
    assert tries["sample-7"] == ()
    assert result["coverage"] == {"expected": 7, "scored": 1}
    assert [row["sample_id"] for row in result["unmeasured"]] == [
        "sample-1", "sample-2", "sample-4", "sample-5", "sample-6",
    ]
    assert result["stopped_reason"] == "consecutive_unmeasured"


@pytest.mark.parametrize("limit, expected", [(1, 1), (2, 2), (4, 4), (None, 3), ("off", 6)])
async def test_configurable_consecutive_unmeasured_stop(tmp_path, limit, expected):
    kwargs, attempts = retry_bindings(tmp_path, failures=tuple(range(1, 7)), repeats=6)
    kwargs["max_consecutive_unmeasured"] = limit
    result, tries = await execute(kwargs)
    assert attempts == {seed: 2 for seed in range(1, expected + 1)}
    assert len(result["unmeasured"]) == expected
    assert result["stopped_reason"] == (None if limit == "off" else "consecutive_unmeasured")
    if expected < 6:
        assert tries["sample-6"] == ()
    else:
        assert len(tries["sample-6"]) == 2


async def test_nondefault_limit_resets_after_scored_sample(tmp_path):
    kwargs, attempts = retry_bindings(tmp_path, failures=(1, 3, 4, 5), repeats=5)
    kwargs["max_consecutive_unmeasured"] = 2
    result, _ = await execute(kwargs)
    assert attempts == {1: 2, 2: 1, 3: 2, 4: 2}
    assert result["coverage"] == {"expected": 5, "scored": 1}
    assert result["stopped_reason"] == "consecutive_unmeasured"


@pytest.mark.parametrize("sample_retries", [None, 0])
async def test_no_retries_without_sample_retries(tmp_path, sample_retries):
    kwargs, attempts = retry_bindings(tmp_path, sample_retries=sample_retries)
    result, tries = await execute(kwargs)
    assert attempts == {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}
    assert [row.payload.get("retry_ordinal") for row, _ in tries["sample-2"]] == [sample_retries]
    assert result["unmeasured"] == [
        {"case_id": "c", "sample_id": "sample-2", "outcome": "target_failed", "tries": 1},
    ]
    assert result["coverage"] == {"expected": 5, "scored": 4}
    assert result["stopped_reason"] is None


def concurrent_bindings(tmp_path, *, concurrency=3, scorer_concurrency=1, repeats=10):
    kwargs = bindings(tmp_path, repeats=repeats)
    kwargs["identity"] = RunIdentityV1({
        **kwargs["identity"].to_payload(),
        "max_concurrency": concurrency,
        "max_scorer_concurrency": scorer_concurrency,
    })
    return kwargs


async def test_concurrency_bounds_in_flight_rollouts(tmp_path):
    import asyncio

    kwargs = concurrent_bindings(tmp_path, scorer_concurrency=2)
    instance, scorer = kwargs["instance"], kwargs["scorer"]
    original_send, original_close, original_score = instance.send, instance.close, scorer.score
    three_sending, all_closed = asyncio.Event(), asyncio.Event()
    sending = scoring = peak_sending = peak_scoring = 0

    async def send(session, message, request_id):
        nonlocal sending, peak_sending
        sending += 1
        peak_sending = max(peak_sending, sending)
        if sending == 3:
            three_sending.set()
        await three_sending.wait()
        await asyncio.sleep(0)
        try:
            return await original_send(session, message, request_id)
        finally:
            sending -= 1

    async def close(session):
        await original_close(session)
        if instance.closed == 10:
            all_closed.set()

    async def score(case, view, candidate):
        nonlocal scoring, peak_scoring
        scoring += 1
        peak_scoring = max(peak_scoring, scoring)
        # Scoring cannot hold rollout slots: all ten sessions must close
        # while these scorers are blocked, with their own limit of two.
        await all_closed.wait()
        await asyncio.sleep(0)
        try:
            return await original_score(case, view, candidate)
        finally:
            scoring -= 1

    instance.send, instance.close, scorer.score = send, close, score
    async with asyncio.timeout(5):
        result, _ = await execute(kwargs)
    assert peak_sending == 3
    assert peak_scoring == 2
    assert instance.closed == 10
    assert result["coverage"] == {"expected": 10, "scored": 10}
    assert result["stopped_reason"] is None


async def test_start_order_is_schedule_order(tmp_path):
    import asyncio

    kwargs = concurrent_bindings(tmp_path)
    instance = kwargs["instance"]
    original_open, original_send = instance.open, instance.send
    starts = []

    async def open_session(public, seed, request_id):
        starts.append(seed)
        return await original_open(public, seed, request_id)

    async def send(session, message, request_id):
        for _ in range(11 - session.requested_seed):
            await asyncio.sleep(0)
        return await original_send(session, message, request_id)

    instance.open, instance.send = open_session, send
    measurement = prepare_fixed(**kwargs)
    with measurement.journal:
        result = await run_fixed(measurement)
        journal_starts = [row.payload["sample_id"] for row in measurement.journal.snapshot().records
                          if row.event == "rollout_started"]
    assert starts == list(range(1, 11))
    assert journal_starts == [f"sample-{i}" for i in range(1, 11)]
    assert result["coverage"] == {"expected": 10, "scored": 10}


@pytest.mark.parametrize("stop", ["infrastructure_failed", "consecutive_unmeasured"])
async def test_stop_does_not_start_new_samples(tmp_path, stop):
    import asyncio

    # Sample 1 completes last. Three failures completing first must trip the
    # consecutive counter even while that earlier scheduled sample is running.
    concurrency = 4 if stop == "consecutive_unmeasured" else 3
    kwargs = concurrent_bindings(tmp_path, concurrency=concurrency)
    instance = kwargs["instance"]
    original_send = instance.send
    all_started = asyncio.Event()
    release_running = asyncio.Event()
    starts = []
    measurement = prepare_fixed(**kwargs)

    async def send(session, message, request_id):
        seed = session.requested_seed
        starts.append(seed)
        if len(starts) == concurrency:
            all_started.set()
        await all_started.wait()
        failing = seed in (2, 3, 4) if stop == "consecutive_unmeasured" else seed == 2
        if not failing:
            await release_running.wait()
            return await original_send(session, message, request_id)
        instance.sends += 1
        if stop == "infrastructure_failed":
            raise RolloutInfrastructureError("PRIVATE-ERROR", "transport_error", retryable=True)
        return TurnResult(events=(), usage=Usage(), latency_ms=0, outcome="target_failed")

    instance.send = send
    with measurement.journal:
        async with asyncio.timeout(5):
            task = asyncio.create_task(run_fixed(measurement))
            try:
                await all_started.wait()
                failures = (2, 3, 4) if stop == "consecutive_unmeasured" else (2,)
                while not all(
                    (row := measurement.journal.sample_records("c", f"sample-{i}")[0]) is not None
                    and row.event != "rollout_started" for i in failures
                ):
                    await asyncio.sleep(0)
                starts_at_stop = list(starts)
                release_running.set()
                result = await task
            finally:
                release_running.set()
                if not task.done():
                    task.cancel()
                    await task
    # Up to two replacements may start before the third failure completes.
    # After that point, no new sample may start, even as running ones finish.
    assert concurrency <= len(starts_at_stop) <= concurrency + len(failures) - 1
    assert starts == starts_at_stop == list(range(1, len(starts_at_stop) + 1))
    assert instance.closed == len(starts_at_stop)
    assert result["coverage"] == {"expected": 10, "scored": len(starts_at_stop) - len(failures)}
    assert {row["sample_id"] for row in result["unmeasured"]} == {f"sample-{i}" for i in failures}
    assert result["stopped_reason"] == stop


async def test_concurrency_one_is_sequential(tmp_path):
    kwargs = concurrent_bindings(tmp_path, concurrency=1, repeats=3)
    measurement = prepare_fixed(**kwargs)
    with measurement.journal:
        result = await run_fixed(measurement)
        events = [(row.event, row.payload.get("sample_id"))
                  for row in measurement.journal.snapshot().records]
    # Exact dev59/b11 success lifecycle, repeated without interleaving samples.
    sample_events = [
        "rollout_started", "target_identity_started", "target_identity_finished",
        "target_open_started", "target_open_finished",
        "environment_next_started", "environment_next_finished",
        "target_send_started", "target_send_finished",
        "environment_next_started", "environment_next_finished",
        "target_close_started", "target_close_finished",
        "target_identity_started", "target_identity_finished", "rollout_finished",
        "scoring_started", "scoring_finished",
    ]
    assert events == [("run_created", None)] + [
        (event, f"sample-{i}") for i in range(1, 4) for event in sample_events
    ]
    assert result["coverage"] == {"expected": 3, "scored": 3}
