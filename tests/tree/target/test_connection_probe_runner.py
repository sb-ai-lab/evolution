"""The connection probe runner.

Every case runs one real probe end to end: a `FakeAdapter` subclass whose
instance emits the declared events, `ScriptedEnvironment`, and a
`DeterministicScorer` or a `SubprocessScorer` over an inline `-c` script. Each
asserts the returned `ProbeResult`, the probe journal's S1 grammar, that no
instance remains owned, the owner-only trace under `connection-logs/<op>/`, and
that nothing is written under `measurements/` or `scorer-traces/`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import evolution.tree.target.connection_probe as probe_module
from evolution.tree.campaign_identity import measurement_identity
from evolution.tree.execution.interaction import (
    RolloutBudget,
    RolloutInfrastructureError,
    ScoringError,
    TurnResult,
)
from evolution.tree.execution.output_admission import FAMILY14_OUTPUT_ROOTS, OUTPUT_ROOTS
from evolution.tree.execution.rollout_scoring import SubprocessScorer
from evolution.tree.execution.usage import Usage
from evolution.tree.storage.journal_errors import JournalFault
from evolution.tree.target.connection_contract import MetricContract, TargetField
from evolution.tree.target.connection_probe import (
    CONNECTION_DIR,
    CONNECTION_LOGS_DIR,
    MAX_TRACE_TEXT_BYTES,
    TRACE_FILE,
    ProbeOutcome,
    ProbeResult,
    probe_identity,
    probe_path,
    probe_seed,
    probe_settlement,
    release_probe_leftover,
    run_probe,
    scorer_with_trace_root,
)
from tests.campaign_fakes import (
    PRIMARY,
    STRATEGY,
    DeterministicScorer,
    FakeAdapter,
    FakeInstance,
    ScriptedDeadlineExceeded,
    ScriptedEnvironment,
)
from tests.test_tree_campaign_measurement import CAMPAIGN_RUN_ID, Rig, build_rig

SECRET = "sk-live-0123456789abcdef-SECRET"
OP = "op-probe-0001"
BINDING = "b" * 64
CONTRACT = MetricContract(
    "pointwise", (TargetField("assistant_message", "/content", "nonempty_string"),)
)
GOOD_EVENTS: tuple[dict[str, Any], ...] = ({"type": "assistant_message", "content": "hello"},)

#: The S1 events a probe journal may hold: operation records, the rollout and
#: scoring lifecycles, and nothing a measurement imports.
_ALLOWED_PREFIXES = (
    "target_materialize_",
    "target_identity_",
    "target_open_",
    "target_send_",
    "environment_next_",
    "target_close_",
    "rollout_",
    "scoring_",
    "target_release_",
)
_TERMINAL_SUFFIXES = ("_finished", "_failed", "_interrupted")


class PowerCut(BaseException):
    """A crash: no handler below the runner classifies a bare `BaseException`."""


# --- the fake Target ------------------------------------------------------------------


class ProbeInstance(FakeInstance):
    """A `FakeInstance` whose turns emit the adapter's scripted events and outcome."""

    adapter: ProbeAdapter

    async def identity(self) -> Any:
        if self.adapter.identity_failure is not None:
            raise self.adapter.identity_failure
        return await super().identity()

    async def send(self, session: Any, message: Any, request_id: str) -> TurnResult:
        script = self.adapter.send_script
        if script is not None:
            if isinstance(script, BaseException):
                raise script
            if script == "time_budget":
                raise ScriptedDeadlineExceeded("scripted rollout deadline")
            if callable(script):
                script()
        return TurnResult(
            events=tuple(dict(event) for event in self.adapter.events),
            usage=Usage(prompt=1, completion=1, cached=0, total=2),
            latency_ms=1,
            outcome=self.adapter.turn_outcome,
        )


class ProbeAdapter(FakeAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: tuple[dict[str, Any], ...] = GOOD_EVENTS
        self.turn_outcome = "ok"
        self.send_script: Any = None
        self.identity_failure: BaseException | None = None
        self.materialize_failure: BaseException | None = None
        self.foreign_digest: str | None = None

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
        if self.materialize_failure is not None:
            raise self.materialize_failure
        return await super().materialize(candidate, strategy, instance_id)

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        instance = ProbeInstance(
            instance_id,
            boot_id,
            self._semantic_for(candidate.dependency_digest),
            self.foreign_digest or candidate.digest,
            self.dispositions,
            self.capabilities.max_concurrent_sessions,
            self._description_for(instance_id),
        )
        instance.adapter = self
        return instance


# --- the rig ---------------------------------------------------------------------------


def subprocess_scorer(tmp_path: Path, stdout: str, *, timeout: float = 5.0) -> SubprocessScorer:
    script = f"import sys; sys.stdin.read(); print({stdout!r})"
    return SubprocessScorer(
        argv=(sys.executable, "-c", script),
        cwd=Path.cwd(),
        timeout_seconds=timeout,
        trace_root=tmp_path / "run" / "scorer-traces",
    )


async def make_rig(tmp_path: Path, *, scorer: Any = None, adapter: Any = None) -> Rig:
    return await build_rig(
        tmp_path,
        adapter=adapter or ProbeAdapter(),
        scorer=scorer or DeterministicScorer(trace_root=tmp_path / "run" / "scorer-traces"),
    )


def identity_for(rig: Rig) -> Any:
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    return probe_identity(base, rig.cases, probe_seed(BINDING, rig.ref.digest))


def budget_for(rig: Rig, cancelled: Any = None) -> RolloutBudget:
    budget = rig.policy.budget
    return RolloutBudget(
        budget.max_turns,
        budget.timeout_seconds,
        max_failed_turns=budget.max_failed_turns,
        call_retries=budget.call_retries,
        cancelled=cancelled or (lambda: False),
    )


async def probe(
    rig: Rig,
    *,
    operation_id: str = OP,
    contract: MetricContract = CONTRACT,
    secrets: tuple[str, ...] = (),
    secret_paths: tuple[str, ...] = (),
    secret_names: tuple[str | None, ...] = (),
    environment: Any = None,
    budget: RolloutBudget | None = None,
    call_attempts: int | None = None,
) -> ProbeResult:
    return await run_probe(
        run_root=rig.run_root,
        operation_id=operation_id,
        campaign_run_id=CAMPAIGN_RUN_ID,
        identity=identity_for(rig),
        corpus=rig.cases,
        secrets=secrets,
        secret_paths=secret_paths,
        secret_names=secret_names,
        adapter=rig.adapter,
        candidate=rig.ref,
        strategy=STRATEGY,
        call_attempts=call_attempts,
        environment=environment or rig.environment,
        scorer=rig.scorer,
        budget=budget or budget_for(rig),
        expected=rig.expected,
        contract=contract,
        primary_metric=PRIMARY,
    )


def journal_path(rig: Rig, operation_id: str = OP) -> Path:
    return rig.run_root / CONNECTION_DIR / operation_id / "events.jsonl"


def records(rig: Rig, operation_id: str = OP) -> list[dict[str, Any]]:
    raw = journal_path(rig, operation_id).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in raw]


def events(rig: Rig, operation_id: str = OP) -> list[str]:
    return [record["event"] for record in records(rig, operation_id)]


def assert_s1_grammar(rig: Rig, operation_id: str = OP) -> None:
    """`run_created`, then operations each started once and closed exactly once."""

    written = records(rig, operation_id)
    assert written[0]["event"] == "run_created"
    assert written[0]["run_id"] == operation_id
    identity = written[0]["payload"]["identity"]
    assert identity["profile"] == "connection_preflight/1"
    assert "connection_binding" not in identity
    starts: Counter[str] = Counter()
    terminals: Counter[str] = Counter()
    for record in written[1:]:
        name = record["event"]
        assert name.startswith(_ALLOWED_PREFIXES), name
        if name.endswith("_started"):
            starts[record["operation_id"]] += 1
        else:
            assert name.endswith(_TERMINAL_SUFFIXES), name
            terminals[record["operation_id"]] += 1
    assert set(starts.values()) <= {1}
    assert starts == terminals
    assert "sample_imported" not in [record["event"] for record in written]


def trace_root(rig: Rig, operation_id: str = OP) -> Path:
    return rig.run_root / CONNECTION_LOGS_DIR / operation_id


def assert_trace(rig: Rig, result: ProbeResult, operation_id: str = OP) -> dict[str, Any]:
    logs = rig.run_root / CONNECTION_LOGS_DIR
    for directory in (logs, trace_root(rig, operation_id)):
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    summary = trace_root(rig, operation_id) / TRACE_FILE
    assert stat.S_IMODE(summary.lstat().st_mode) == 0o600
    raw = summary.read_bytes()
    assert result.trace_digest == hashlib.sha256(raw).hexdigest()
    return json.loads(raw)


def assert_untouched(rig: Rig) -> None:
    assert not (rig.run_root / "measurements").exists()
    assert not (rig.run_root / "scorer-traces").exists()


def assert_evidence(rig: Rig, result: ProbeResult, operation_id: str = OP) -> None:
    raw = journal_path(rig, operation_id).read_bytes()
    assert [dict(reference) for reference in result.evidence] == [
        {
            "version": 1,
            "kind": "journal_prefix",
            "run_id": operation_id,
            "locator": f"connection/{operation_id}/events.jsonl#bytes=0:{len(raw)}",
            "content_digest": hashlib.sha256(raw).hexdigest(),
        }
    ]


def assert_probe(rig: Rig, result: ProbeResult, *, leaked: bool = False) -> dict[str, Any]:
    """The common end-to-end assertions; returns the trace summary."""

    assert_s1_grammar(rig)
    if not leaked:
        assert rig.adapter.live == {}
    assert_untouched(rig)
    assert_evidence(rig, result)
    return assert_trace(rig, result)


def unavailable(cause: str) -> ProbeOutcome:
    return ProbeOutcome("connection_preflight_unavailable", None, None, None, cause)


def incompatible(code: str, manifest: str, observed: str, cause: str | None = None) -> Any:
    return ProbeOutcome("connection_incompatible", code, manifest, observed, cause)  # type: ignore[arg-type]


FIELD_PATH = "scorer.metric_contract.target_fields[0]"
FIELD_OBSERVED = "events[event=assistant_message,last]/content"


# --- compatible ------------------------------------------------------------------------


async def test_a_valid_connection_is_compatible_and_released(tmp_path):
    rig = await make_rig(tmp_path)
    result = await probe(rig)
    assert result.outcome == ProbeOutcome("compatible", None, None, None, None)
    assert result.cleanup == "released"
    summary = assert_probe(rig, result)
    assert summary["status"] == "compatible" and summary["cleanup"] == "released"
    names = events(rig)
    assert names.index("rollout_finished") < names.index("scoring_started")
    assert names.index("scoring_finished") < names.index("target_release_started")
    assert names[-1] == "target_release_finished"
    assert len(rig.adapter.materialized) == 1
    # The probe's one row is the probe sample under its own seed, never a schedule slot.
    start = next(record for record in records(rig) if record["event"] == "rollout_started")
    assert start["payload"]["sample_id"] == "probe"
    assert "connection_admission" not in start["payload"]
    event, payload = result.terminal()
    assert event == "connection_preflight_finished"
    assert payload == {
        "status": "compatible",
        "trace_digest": result.trace_digest,
        "cleanup": "released",
        "evidence": [dict(reference) for reference in result.evidence],
    }


async def test_the_result_never_carries_the_reply_or_the_score(tmp_path):
    rig = await make_rig(tmp_path)
    result = await probe(rig)
    assert {field.name for field in dataclasses.fields(ProbeResult)} == {
        "outcome",
        "cleanup",
        "trace_digest",
        "evidence",
    }
    flat = json.dumps(result.terminal()[1])
    assert "hello" not in flat
    value = DeterministicScorer.value_for(rig.ref.digest, rig.cases[0].case_id)
    assert repr(value) not in flat and repr(value) not in (trace_root(rig) / TRACE_FILE).read_text()
    assert "hello" not in (trace_root(rig) / TRACE_FILE).read_text()


async def test_a_subprocess_scorer_traces_under_connection_logs(tmp_path):
    rig = await make_rig(tmp_path, scorer=subprocess_scorer(tmp_path, '{"metrics":{"score":0.5}}'))
    result = await probe(rig)
    assert result.outcome is not None and result.outcome.status == "compatible"
    assert_probe(rig, result)
    from evolution.tree import read_scorer_trace

    pack = trace_root(rig) / f"{OP}.pack"
    assert stat.S_IMODE(pack.lstat().st_mode) == 0o600
    assert not (trace_root(rig) / "scorer_traces").exists()
    started = next(record for record in records(rig) if record["event"] == "scoring_started")
    trace = read_scorer_trace(rig.run_root, started["payload"]["score_request_id"], operation_id=OP)
    assert trace is not None and trace.layout == "pack" and trace.status == "completed"


def test_scorer_with_trace_root_repoints_only_the_trace(tmp_path):
    scorer = subprocess_scorer(tmp_path, "{}")
    moved = scorer_with_trace_root(scorer, tmp_path / "elsewhere")
    assert moved.trace_root == (tmp_path / "elsewhere").resolve()
    assert moved.identity == scorer.identity and scorer.trace_root != moved.trace_root
    deterministic = DeterministicScorer(trace_root=tmp_path / "x")
    copied = scorer_with_trace_root(deterministic, tmp_path / "y")
    assert copied.trace_root == tmp_path / "y" and deterministic.trace_root == tmp_path / "x"
    plain = object()
    assert scorer_with_trace_root(plain, tmp_path) is plain


# --- incompatible: the Target fields -------------------------------------------


@pytest.mark.parametrize(
    ("emitted", "rule", "code", "expected", "observed"),
    [
        ([{"type": "message", "text": "x"}], "nonempty_string", "connection_event_missing",
         "string", "missing"),
        ([{"type": "assistant_message"}], "nonempty_string", "connection_field_missing",
         "string", "missing"),
        ([{"type": "assistant_message", "content": "  "}], "nonempty_string",
         "connection_field_empty", "string", "string"),
        ([{"type": "assistant_message", "content": 5}], "nonempty_string",
         "connection_field_type", "string", "number"),
        # Ruling 9: a boolean Target value *can* be built into a frozen turn, so
        # it reaches the field check and fails `finite_number` as a type.
        ([{"type": "assistant_message", "content": True}], "finite_number",
         "connection_field_type", "number", "boolean"),
    ],
)  # fmt: skip
async def test_a_target_field_failure_is_incompatible_and_never_scored(
    tmp_path, emitted, rule, code, expected, observed
):
    adapter = ProbeAdapter()
    adapter.events = tuple(emitted)
    rig = await make_rig(tmp_path, adapter=adapter)
    contract = MetricContract("pointwise", (TargetField("assistant_message", "/content", rule),))
    result = await probe(rig, contract=contract)
    assert result.outcome == incompatible(code, FIELD_PATH, FIELD_OBSERVED)
    assert result.cleanup == "released"
    summary = assert_probe(rig, result)
    assert summary["field"] == {"expected_type": expected, "observed_type": observed}
    assert not any(name.startswith("scoring_") for name in events(rig))
    event, payload = result.terminal()
    assert event == "connection_preflight_failed"
    assert payload == {
        "status": "connection_incompatible",
        "detail_code": code,
        "manifest_path": FIELD_PATH,
        "observed_path": FIELD_OBSERVED,
        "cause_code": None,
        "trace_digest": result.trace_digest,
        "cleanup": "released",
        "evidence": [dict(reference) for reference in result.evidence],
    }
    # Q2: the types are in the owner-only trace only.
    assert "expected_type" not in payload and "observed_type" not in payload


async def test_a_trailing_truncation_event_does_not_hide_the_declared_event(tmp_path):
    adapter = ProbeAdapter()
    adapter.events = (*GOOD_EVENTS, {"type": "output_truncated"})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    assert result.outcome is not None and result.outcome.status == "compatible"


async def test_a_non_finite_target_value_is_unavailable_never_a_score(tmp_path):
    # Ruling 9: a non-finite value cannot be built into a frozen `TurnResult`, so
    # the Target call itself fails; the probe is unavailable, not incompatible.
    adapter = ProbeAdapter()
    adapter.events = ({"type": "assistant_message", "content": float("nan")},)
    rig = await make_rig(tmp_path, adapter=adapter)
    contract = MetricContract(
        "pointwise", (TargetField("assistant_message", "/content", "finite_number"),)
    )
    result = await probe(rig, contract=contract)
    assert result.outcome == unavailable("infrastructure_failed")
    assert_probe(rig, result)
    assert not any(name.startswith("scoring_") for name in events(rig))


# --- incompatible and unavailable: the Scorer -----------------------------------------


async def test_a_missing_primary_is_incompatible(tmp_path):
    rig = await make_rig(tmp_path)
    rig.scorer.missing_primary_for = frozenset({rig.cases[0].case_id})
    result = await probe(rig)
    assert result.outcome == incompatible(
        "connection_primary_missing", "campaign.metric.primary", f"scorer.metrics.{PRIMARY}"
    )
    assert_probe(rig, result)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"metrics":{"score":true}}',
         incompatible("connection_primary_type", "campaign.metric.primary", "scorer.metrics.score")),
        ('{"metrics":{"score":"high"}}',
         incompatible("connection_primary_type", "campaign.metric.primary", "scorer.metrics.score")),
        ('{"metrics":{"score":1e999}}',
         incompatible("connection_primary_nonfinite", "campaign.metric.primary",
                      "scorer.metrics.score")),
        ('{"metrics":{"score":1,"extra":1e999}}',
         incompatible("connection_scorer_output_invalid", "scorer", "scorer.metrics",
                      "metric_nonfinite")),
        ('{"metrics":{"score":' + str(10**400) + '}}',
         incompatible("connection_primary_nonfinite", "campaign.metric.primary",
                      "scorer.metrics.score")),
        ('{"metrics":{"score":1,"extra":' + str(10**400) + '}}',
         incompatible("connection_scorer_output_invalid", "scorer", "scorer.metrics",
                      "metric_nonfinite")),
        ('{"metrics":{"score":1,"extra":"x"}}',
         incompatible("connection_scorer_output_invalid", "scorer", "scorer.metrics",
                      "metric_type")),
        ("not-json",
         incompatible("connection_scorer_output_invalid", "scorer", "scorer.output",
                      "output_invalid")),
    ],
)  # fmt: skip
async def test_a_scorer_output_failure_is_incompatible(tmp_path, stdout, expected):
    rig = await make_rig(tmp_path, scorer=subprocess_scorer(tmp_path, stdout))
    result = await probe(rig)
    assert result.outcome == expected
    summary = assert_probe(rig, result)
    assert summary["scorer"]["code"] == (expected.cause_code or summary["scorer"]["code"])
    assert "scoring_failed" in events(rig)


async def test_scorer_process_failures_are_unavailable(tmp_path):
    exits = SubprocessScorer(
        argv=(sys.executable, "-c", "raise SystemExit(7)"),
        cwd=Path.cwd(),
        timeout_seconds=5,
        trace_root=tmp_path / "run" / "scorer-traces",
    )
    rig = await make_rig(tmp_path, scorer=exits)
    result = await probe(rig)
    assert result.outcome == unavailable("process_failed")
    assert_probe(rig, result)


async def test_a_scorer_timeout_is_unavailable(tmp_path):
    sleeps = SubprocessScorer(
        argv=(sys.executable, "-c", "import time; time.sleep(5)"),
        cwd=Path.cwd(),
        timeout_seconds=0.05,
        trace_root=tmp_path / "run" / "scorer-traces",
    )
    rig = await make_rig(tmp_path, scorer=sleeps)
    result = await probe(rig)
    assert result.outcome == unavailable("timeout")
    assert_probe(rig, result)


async def test_an_uncoded_scorer_failure_is_unavailable_scorer_failed(tmp_path):
    class Broken(DeterministicScorer):
        async def score(self, *_args: Any) -> Any:
            raise RuntimeError(f"broken {SECRET}")

    rig = await make_rig(tmp_path, scorer=Broken())
    result = await probe(rig, secrets=(SECRET,))
    assert result.outcome == unavailable("scorer_failed")
    summary = assert_probe(rig, result)
    assert summary["scorer"] == {"code": None, "metric": None}


async def test_a_scorer_metric_name_is_bounded_and_redacted_in_the_trace(tmp_path):
    name = SECRET + "x" * 5000
    stdout = json.dumps({"metrics": {"score": 1, name: "x"}})
    rig = await make_rig(tmp_path, scorer=subprocess_scorer(tmp_path, stdout))
    result = await probe(rig, secrets=(SECRET,))
    assert result.outcome == incompatible(
        "connection_scorer_output_invalid", "scorer", "scorer.metrics", "metric_type"
    )
    summary = assert_probe(rig, result)
    metric = summary["scorer"]["metric"]
    assert SECRET not in metric and len(metric.encode("utf-8")) <= 256
    assert SECRET not in json.dumps(result.terminal()[1])
    assert SECRET.encode() not in journal_path(rig).read_bytes()
    assert SECRET.encode() not in (trace_root(rig) / TRACE_FILE).read_bytes()


# --- unavailable: Target, Environment and rollout infrastructure -----------------------


async def test_a_materialize_failure_is_unavailable_and_released(tmp_path):
    adapter = ProbeAdapter()
    adapter.materialize_failure = RolloutInfrastructureError("down", "upstream_unavailable")
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    assert result.outcome == unavailable("upstream_unavailable")
    assert result.cleanup == "released"
    assert_probe(rig, result)
    names = events(rig)
    assert names.index("target_materialize_failed") < names.index("target_release_finished")
    assert "rollout_started" not in names


async def test_an_attestation_refusal_is_unavailable_and_released(tmp_path):
    adapter = ProbeAdapter()
    adapter.foreign_digest = "0" * 64
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    assert result.outcome == unavailable("attestation_mismatch")
    assert_probe(rig, result)
    assert adapter.released == adapter.materialized
    assert "rollout_started" not in events(rig)


async def test_an_identity_failure_is_unavailable_and_released(tmp_path):
    adapter = ProbeAdapter()
    adapter.identity_failure = RolloutInfrastructureError("gone", "target_unavailable")
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    # A custom adapter's own code is outside the closed family-14 cause set:
    # the record says `infrastructure`, and
    # only the owner-only trace keeps the code itself.
    assert result.outcome == unavailable("infrastructure")
    assert_probe(rig, result)
    summary = json.loads((trace_root(rig) / TRACE_FILE).read_bytes())
    assert (summary["cause_code"], summary["infrastructure_code"]) == (
        "infrastructure",
        "target_unavailable",
    )


@pytest.mark.parametrize(
    ("script", "outcome", "cause"),
    [
        (RolloutInfrastructureError("x", "target_unavailable"), "unavailable",
         "infrastructure_failed"),
        ("target_failed", "unavailable", "target_failed"),
        ("time_budget", "inconclusive", "time_budget"),
        ("candidate_failed", "inconclusive", "candidate_failed"),
    ],
)  # fmt: skip
async def test_a_rollout_without_a_scoreable_terminal_is_classified(
    tmp_path, script, outcome, cause
):
    adapter = ProbeAdapter()
    if script in ("target_failed", "candidate_failed"):
        adapter.turn_outcome = script
    else:
        adapter.send_script = script
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    status = f"connection_preflight_{outcome}"
    assert result.outcome == ProbeOutcome(status, None, None, None, cause)  # type: ignore[arg-type]
    assert_probe(rig, result)
    assert not any(name.startswith("scoring_") for name in events(rig))
    _, payload = result.terminal()
    assert payload["status"] == status and payload["cause_code"] == cause
    assert payload["detail_code"] is None and payload["manifest_path"] is None


async def test_a_turn_budget_is_inconclusive(tmp_path):
    rig = await make_rig(tmp_path)
    # The Environment wants one more turn than the run's budget admits.
    turns = rig.policy.budget.max_turns + 1
    result = await probe(rig, environment=ScriptedEnvironment(messages=turns))
    assert result.outcome == ProbeOutcome(
        "connection_preflight_inconclusive", None, None, None, "turn_budget"
    )
    assert_probe(rig, result)


async def test_an_environment_failure_is_unavailable(tmp_path):
    class Failing(ScriptedEnvironment):
        async def next(self, case: Any, history: Any) -> Any:
            raise RuntimeError("environment down")

    rig = await make_rig(tmp_path)
    result = await probe(rig, environment=Failing())
    assert result.outcome == unavailable("environment_failed")
    assert_probe(rig, result)


# --- cleanup ---------------------------------------------------------------------------


async def test_a_refused_release_turns_a_compatible_probe_unavailable(tmp_path):
    adapter = ProbeAdapter()

    def refuse() -> None:
        adapter.refuse_release = frozenset(adapter.live)

    adapter.send_script = refuse
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    assert result.outcome == unavailable("cleanup_failed")
    assert result.cleanup == "failed"
    summary = assert_probe(rig, result, leaked=True)
    assert len(adapter.live) == 1
    assert "target_release_failed" in events(rig)
    assert summary["masked"] == {
        "status": "compatible",
        "detail_code": None,
        "manifest_path": None,
        "observed_path": None,
        "cause_code": None,
    }
    assert summary["cleanup_failures"] == [{"failure": "infrastructure_error"}]
    _, payload = result.terminal()
    assert payload["cleanup"] == "failed" and payload["cause_code"] == "cleanup_failed"


async def test_a_refused_release_masks_an_incompatibility_into_the_trace(tmp_path):
    adapter = ProbeAdapter()
    adapter.events = ({"type": "message", "text": "x"},)

    def refuse() -> None:
        adapter.refuse_release = frozenset(adapter.live)

    adapter.send_script = refuse
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig)
    assert result.outcome == unavailable("cleanup_failed")
    summary = assert_probe(rig, result, leaked=True)
    assert summary["masked"]["detail_code"] == "connection_event_missing"
    assert summary["masked"]["manifest_path"] == FIELD_PATH
    assert summary["field"] == {"expected_type": "string", "observed_type": "missing"}


# --- interruptions ---------------------------------------------------------------------


async def test_a_cancellation_propagates_after_the_release(tmp_path):
    adapter = ProbeAdapter()
    adapter.send_script = asyncio.CancelledError()
    rig = await make_rig(tmp_path, adapter=adapter)
    with pytest.raises(asyncio.CancelledError):
        await probe(rig)
    assert adapter.live == {} and adapter.released == adapter.materialized
    assert_s1_grammar(rig)
    assert "rollout_interrupted" in events(rig)
    assert_untouched(rig)
    settled = probe_settlement(rig.run_root, OP)
    assert settled.outcome is None and settled.cleanup == "released"
    summary = assert_trace(rig, settled)
    assert summary["status"] == "interrupted"
    assert_evidence(rig, settled)
    assert settled.interrupted("cancelled") == {
        "reason": "cancelled",
        "trace_digest": settled.trace_digest,
        "cleanup": "released",
        "evidence": [dict(reference) for reference in settled.evidence],
    }


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
async def test_other_interruptions_propagate_after_the_release(tmp_path, interruption):
    adapter = ProbeAdapter()
    adapter.identity_failure = interruption()
    rig = await make_rig(tmp_path, adapter=adapter)
    with pytest.raises(interruption):
        await probe(rig)
    assert adapter.live == {}
    assert probe_settlement(rig.run_root, OP).cleanup == "released"


async def test_an_interruption_mid_rollout_releases_through_the_adapter(tmp_path):
    # S1 closes a rollout in-process only for a cancellation; with a
    # `KeyboardInterrupt` the rollout is still open, the journal cannot record
    # a release, so the Target is given the instance back directly and the
    # journal keeps the ownership for recovery to settle.
    adapter = ProbeAdapter()
    adapter.send_script = KeyboardInterrupt()
    rig = await make_rig(tmp_path, adapter=adapter)
    with pytest.raises(KeyboardInterrupt):
        await probe(rig)
    assert adapter.live == {} and adapter.released == adapter.materialized
    names = events(rig)
    assert "rollout_started" in names and "target_release_started" not in names
    assert probe_settlement(rig.run_root, OP).cleanup == "failed"
    summary = json.loads((trace_root(rig) / TRACE_FILE).read_bytes())
    assert summary["status"] == "interrupted" and summary["error_class"] == "KeyboardInterrupt"
    adapter.send_script = None
    path = probe_path(rig.run_root, OP)
    assert await release_probe_leftover(path, rig.cases, (), adapter) == "released"
    assert_s1_grammar(rig)
    assert "rollout_interrupted" in events(rig)


async def test_a_non_probe_identity_is_refused_before_anything_is_written(tmp_path):
    rig = await make_rig(tmp_path)
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    with pytest.raises(probe_module.IdentityError, match="invalid_identity_profile"):
        await run_probe(
            run_root=rig.run_root,
            operation_id=OP,
            campaign_run_id=CAMPAIGN_RUN_ID,
            identity=base,
            corpus=rig.cases,
            secrets=(),
            adapter=rig.adapter,
            candidate=rig.ref,
            strategy=STRATEGY,
            call_attempts=None,
            environment=rig.environment,
            scorer=rig.scorer,
            budget=budget_for(rig),
            expected=rig.expected,
            contract=CONTRACT,
            primary_metric=PRIMARY,
        )
    assert not (rig.run_root / CONNECTION_DIR).exists()
    assert not (rig.run_root / CONNECTION_LOGS_DIR).exists()


async def test_a_cooperative_stop_returns_no_outcome(tmp_path):
    stopped = False
    adapter = ProbeAdapter()

    def stop() -> None:
        nonlocal stopped
        stopped = True

    adapter.send_script = stop
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(
        rig, environment=ScriptedEnvironment(messages=2), budget=budget_for(rig, lambda: stopped)
    )
    assert result.outcome is None and result.cleanup == "released"
    assert_probe(rig, result)
    assert not any(name.startswith("scoring_") for name in events(rig))
    with pytest.raises(ValueError):
        result.terminal()
    assert result.interrupted("interrupted")["reason"] == "interrupted"


# --- boundaries ------------------------------------------------------------------------


async def test_a_run_secret_in_an_event_value_never_reaches_the_journal_or_trace(tmp_path):
    adapter = ProbeAdapter()
    adapter.events = ({"type": "assistant_message", "content": f"leak {SECRET}"},)
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(SECRET,))
    assert result.outcome is not None and result.outcome.status == "compatible"
    assert_probe(rig, result)
    for path in (journal_path(rig), trace_root(rig) / TRACE_FILE):
        assert SECRET.encode() not in path.read_bytes()
    assert SECRET not in json.dumps(result.terminal()[1])


#: a model id a Scorer forwards as its judge model, and the backend's
#: `describe()` reports as the agent's model.
JUDGE = "deepseek/judge-model-x"
JUDGE_PATH = "scorer.allowed_env.B2E_LAB_JUDGE_MODEL"


def assert_absent(rig: Rig, result: ProbeResult, value: str) -> None:
    """`value` is in no byte the probe wrote and in neither campaign payload."""

    for path in rig.run_root.rglob("*"):
        if path.is_file():
            assert value.encode() not in path.read_bytes(), path
    assert value not in json.dumps(result.terminal()[1])
    assert value not in json.dumps(dataclasses.asdict(result.outcome))


async def test_a_description_with_a_run_secret_is_incompatible_before_the_rollout(tmp_path):
    adapter = ProbeAdapter(description={"model": JUDGE})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome == incompatible(
        "connection_description_secret", JUDGE_PATH, "target.description"
    )
    assert result.outcome.cause_code is None
    assert result.cleanup == "released"
    summary = assert_probe(rig, result)
    names = events(rig)
    assert "rollout_started" not in names and "scoring_started" not in names
    assert names.index("target_materialize_finished") < names.index("target_release_finished")
    assert adapter.released == adapter.materialized
    # Only the owner-only trace names the Target's key; the record has none.
    assert summary["description_location"] == "description.model"
    assert summary["detail_code"] == "connection_description_secret"
    event, payload = result.terminal()
    assert event == "connection_preflight_failed"
    assert (payload["status"], payload["detail_code"], payload["manifest_path"]) == (
        "connection_incompatible",
        "connection_description_secret",
        JUDGE_PATH,
    )
    assert "description_location" not in payload
    assert_absent(rig, result, JUDGE)


async def test_a_description_without_a_run_secret_runs_on(tmp_path):
    adapter = ProbeAdapter(description={"model": "a-hash-of-the-model"})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome == ProbeOutcome("compatible", None, None, None, None)
    summary = assert_probe(rig, result)
    # The trace's key set is unchanged when nothing matched.
    assert "description_location" not in summary


async def test_the_first_secret_in_role_order_is_reported(tmp_path):
    key = "sk-editor-key-0123456789"
    adapter = ProbeAdapter(description={"key": key, "model": JUDGE})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE, key), secret_paths=(JUDGE_PATH, "editor.api_key_env"))
    assert result.outcome == incompatible(
        "connection_description_secret", JUDGE_PATH, "target.description"
    )
    # The record names the first secret in role order, which matched a later
    # key; the trace names that secret's key path, not the first match walked.
    assert assert_probe(rig, result)["description_location"] == "description.model"
    assert_absent(rig, result, key)


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
async def test_the_first_name_sorted_within_a_role_is_reported(tmp_path, role):
    """ADR-0029 amendment 2026-10-21: names sorted within a role. A `chat`
    role's key `Z_AUTH` precedes its proxy `A_PROXY` in the run's secrets, and
    the proxy is named ."""

    key, proxy = "sk-z-auth-value-01", "https://a-proxy.example.test:8080"
    adapter = ProbeAdapter(description={"key": key, "proxy": proxy})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(
        rig,
        secrets=(key, proxy),
        secret_paths=(f"{role}.api_key_env", f"{role}.proxy_env"),
        secret_names=("Z_AUTH", "A_PROXY"),
    )
    assert result.outcome == incompatible(
        "connection_description_secret", f"{role}.proxy_env", "target.description"
    )
    assert assert_probe(rig, result)["description_location"] == "description.proxy"
    assert_absent(rig, result, key)
    assert_absent(rig, result, proxy)


async def test_a_lone_surrogate_key_is_recorded_not_raised(tmp_path):
    """The key path is Target-controlled. A library
    adapter's lone surrogate cannot be encoded as UTF-8; the trace is still
    written and the connection refusal recorded."""

    adapter = ProbeAdapter(description={"\ud800": JUDGE})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome == incompatible(
        "connection_description_secret", JUDGE_PATH, "target.description"
    )
    location = assert_probe(rig, result)["description_location"]
    assert location.startswith("description.")
    location.encode("utf-8")
    assert_absent(rig, result, JUDGE)


async def test_a_very_long_key_path_is_bounded_in_the_trace(tmp_path):
    adapter = ProbeAdapter(description={"k" * 5000: {"model": JUDGE}})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome is not None
    assert result.outcome.detail_code == "connection_description_secret"
    location = assert_probe(rig, result)["description_location"]
    assert location.startswith("description.kkk")
    assert len(location.encode("utf-8")) <= MAX_TRACE_TEXT_BYTES
    assert_absent(rig, result, JUDGE)


async def test_a_library_secret_is_named_by_its_index(tmp_path):
    adapter = ProbeAdapter(description={"nested": [{"model": JUDGE}]})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=("unrelated-secret-1", JUDGE))
    assert result.outcome == incompatible(
        "connection_description_secret", "secrets[1]", "target.description"
    )
    assert assert_probe(rig, result)["description_location"] == "description.nested[0].model"


async def test_a_key_that_holds_a_secret_is_located_as_record_key(tmp_path):
    adapter = ProbeAdapter(description={JUDGE: "on"})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome is not None
    assert result.outcome.detail_code == "connection_description_secret"
    assert assert_probe(rig, result)["description_location"] == "record-key"
    assert_absent(rig, result, JUDGE)


async def test_a_diagnostic_path_that_matches_a_secret_falls_back(tmp_path):
    name = "B2E_LAB_JUDGE_MODEL"
    adapter = ProbeAdapter(description={"model": name})
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(name,), secret_paths=(f"scorer.allowed_env.{name}",))
    assert result.outcome == incompatible(
        "connection_description_secret", "scorer.allowed_env", "target.description"
    )
    assert_probe(rig, result)
    assert_absent(rig, result, name)


async def test_a_refused_release_masks_a_description_secret_into_the_trace(tmp_path):
    adapter = ProbeAdapter(description={"model": JUDGE})
    real = adapter.materialize

    async def refusing(candidate: Any, strategy: str, instance_id: str) -> Any:
        instance = await real(candidate, strategy, instance_id)
        adapter.refuse_release = frozenset({instance_id})
        return instance

    adapter.materialize = refusing  # type: ignore[method-assign]
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(rig, secrets=(JUDGE,), secret_paths=(JUDGE_PATH,))
    assert result.outcome == unavailable("cleanup_failed")
    summary = assert_probe(rig, result, leaked=True)
    assert summary["masked"]["detail_code"] == "connection_description_secret"
    assert summary["masked"]["manifest_path"] == JUDGE_PATH
    assert "rollout_started" not in events(rig)
    assert_absent(rig, result, JUDGE)


async def test_an_existing_probe_journal_directory_is_refused_before_any_target_call(tmp_path):
    rig = await make_rig(tmp_path)
    leftover = probe_path(rig.run_root, OP)
    leftover.mkdir(parents=True)
    (leftover / "foreign").write_text("x", encoding="utf-8")
    with pytest.raises(JournalFault):
        await probe(rig)
    assert rig.adapter.materialized == []
    assert not (rig.run_root / CONNECTION_LOGS_DIR / OP).exists()


async def test_two_probes_use_two_journals_and_two_traces(tmp_path):
    rig = await make_rig(tmp_path)
    first = await probe(rig, operation_id="op-a")
    second = await probe(rig, operation_id="op-b")
    assert first.trace_digest != second.trace_digest
    assert first.evidence[0]["run_id"] == "op-a" and second.evidence[0]["run_id"] == "op-b"
    assert rig.adapter.live == {}


def test_the_probe_identity_is_one_row_on_case_zero(tmp_path):
    rig = asyncio.run(make_rig(tmp_path))
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    seed = probe_seed(BINDING, rig.ref.digest)
    payload = probe_identity(base, rig.cases, seed).to_payload()
    assert payload["profile"] == "connection_preflight/1"
    assert payload["case_subset"] == [0]
    assert payload["schedule"] == [
        {"case_id": rig.cases[0].case_id, "sample_id": "probe", "repeat": 1,
         "requested_seed": seed}
    ]  # fmt: skip
    assert payload["candidate_digest"] == rig.ref.digest
    for key in ("target", "environment", "scorer", "budget", "materialization_strategy"):
        assert payload[key] == base.to_payload()[key]
    assert "connection_binding" not in payload and "row_provenance" not in payload


def test_the_probe_identity_drops_the_seed_outside_exact_mode(tmp_path):
    rig = asyncio.run(make_rig(tmp_path, adapter=ProbeAdapter(seed_mode="unpaired")))
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    payload = probe_identity(base, rig.cases, probe_seed(BINDING, rig.ref.digest)).to_payload()
    assert payload["schedule"][0]["requested_seed"] is None


async def test_an_unpaired_target_probes_with_no_requested_seed(tmp_path):
    rig = await make_rig(tmp_path, adapter=ProbeAdapter(seed_mode="unpaired"))
    result = await probe(rig)
    assert result.outcome is not None and result.outcome.status == "compatible"
    assert_probe(rig, result)


# --- release_probe_leftover ------------------------------------------------------------


async def _cut(rig: Rig, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    async def cut(*_args: Any, **_kwargs: Any) -> None:
        raise PowerCut

    adapter = rig.adapter
    if where == "after_materialize":
        adapter.identity_failure = PowerCut()  # type: ignore[attr-defined]
    else:
        adapter.send_script = PowerCut()  # type: ignore[attr-defined]
    with monkeypatch.context() as patch:
        # The power cut is at the release itself, so nothing gives the instance back.
        patch.setattr(probe_module, "_release", cut)
        with pytest.raises(PowerCut):
            await probe(rig)
    adapter.identity_failure = None  # type: ignore[attr-defined]
    adapter.send_script = None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("where", "last"),
    [("after_materialize", "target_materialize_finished"), ("after_rollout_started", None)],
)
async def test_a_leftover_is_released_under_its_stored_identity(tmp_path, monkeypatch, where, last):
    rig = await make_rig(tmp_path)
    await _cut(rig, monkeypatch, where)
    assert len(rig.adapter.live) == 1
    names = events(rig)
    if last is not None:
        assert names[-1] == last
    else:
        assert "rollout_started" in names and "rollout_finished" not in names
    path = probe_path(rig.run_root, OP)
    assert probe_settlement(rig.run_root, OP).cleanup == "failed"
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "released"
    assert rig.adapter.live == {}
    assert_s1_grammar(rig)
    if last is None:
        assert "rollout_interrupted" in events(rig)
    assert probe_settlement(rig.run_root, OP).cleanup == "released"
    # A second pass finds nothing left to release.
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "released"


async def test_a_leftover_the_target_keeps_is_reported_failed(tmp_path, monkeypatch):
    rig = await make_rig(tmp_path)
    await _cut(rig, monkeypatch, "after_rollout_started")
    rig.adapter.refuse_release = frozenset(rig.adapter.live)
    path = probe_path(rig.run_root, OP)
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "failed"
    assert len(rig.adapter.live) == 1
    rig.adapter.refuse_release = frozenset()
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "released"
    assert rig.adapter.live == {}


async def test_an_unborn_or_absent_probe_journal_needs_no_release(tmp_path):
    rig = await make_rig(tmp_path)
    path = probe_path(rig.run_root, OP)
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "not_needed"
    path.mkdir(parents=True)
    (path / "events.jsonl").write_bytes(b"")
    assert await release_probe_leftover(path, rig.cases, (), rig.adapter) == "not_needed"
    settled = probe_settlement(rig.run_root, OP)
    assert settled.evidence == () and settled.trace_digest is None
    assert settled.cleanup == "not_needed"
    assert settled.interrupted("interrupted") == {
        "reason": "interrupted",
        "trace_digest": None,
        "cleanup": "not_needed",
        "evidence": [],
    }


async def test_an_unsettled_leftover_gives_the_cleanup_failed_terminal(tmp_path, monkeypatch):
    rig = await make_rig(tmp_path)
    await _cut(rig, monkeypatch, "after_materialize")
    rig.adapter.refuse_release = frozenset(rig.adapter.live)
    assert (
        await release_probe_leftover(probe_path(rig.run_root, OP), rig.cases, (), rig.adapter)
        == "failed"
    )
    settled = probe_settlement(rig.run_root, OP)
    assert settled.cleanup == "failed"
    assert settled.unsettled() == (
        "connection_preflight_failed",
        {
            "status": "connection_preflight_unavailable",
            "detail_code": None,
            "manifest_path": None,
            "observed_path": None,
            "cause_code": "cleanup_failed",
            "trace_digest": settled.trace_digest,
            "cleanup": "failed",
            "evidence": [dict(reference) for reference in settled.evidence],
        },
    )


# --- bindings: the owner-only output root ----------------------------------------------


def test_connection_logs_is_an_output_root():
    assert "connection-logs" in FAMILY14_OUTPUT_ROOTS
    assert "connection-logs" not in OUTPUT_ROOTS
    assert CONNECTION_LOGS_DIR == "connection-logs"


def test_scoring_error_is_the_rollout_reexport():
    # The runner catches the class `durable_score` raises.
    from evolution.tree.execution.rollout_scoring import ScoringError as Scoring

    assert Scoring is ScoringError


# --- Journal and storage faults ---------------------------------------------------------


def _poisoned_on(event_name: str, original: Exception) -> type:
    """A `Journal` that faults on `event_name`, then refuses every later append.

    As `tests/test_tree_campaign_inspection.py`'s seam: the poisoned journal
    reports itself unhealthy, so the probe's release falls back to the adapter.
    """

    class Poisoned(probe_module.Journal):
        poisoned = False

        @property
        def healthy(self) -> bool:
            return not self.poisoned and super().healthy

        def append(self, event: str, payload: Any, operation_id: Any = None) -> int:
            if self.poisoned:
                raise JournalFault("journal_unavailable")
            if event == event_name:
                self.poisoned = True
                raise original
            return super().append(event, payload, operation_id)

    return Poisoned


async def test_a_journal_fault_on_materialize_finished_releases_through_the_adapter(
    tmp_path, monkeypatch
):
    # The Target created the instance, but its creation cannot be recorded.
    original = JournalFault("storage_capacity_exhausted")
    monkeypatch.setattr(
        probe_module, "Journal", _poisoned_on("target_materialize_finished", original)
    )
    rig = await make_rig(tmp_path)
    with pytest.raises(JournalFault) as raised:
        await probe(rig)
    assert raised.value is original
    assert len(rig.adapter.materialized) == 1
    assert rig.adapter.released == rig.adapter.materialized and rig.adapter.live == {}
    names = events(rig)
    # The journal still records the started materialize, so recovery releases
    # the instance again by id.
    assert "target_materialize_started" in names and "target_release_started" not in names
    assert probe_settlement(rig.run_root, OP).cleanup == "failed"


async def test_a_journal_fault_on_materialize_failed_releases_through_the_adapter(
    tmp_path, monkeypatch
):
    # A lost answer: the Target created the instance, the call failed, and the
    # failure record cannot be written.
    class LostAnswer(ProbeAdapter):
        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            await FakeAdapter.materialize(self, candidate, strategy, instance_id)
            raise RolloutInfrastructureError("answer lost", "timeout")

    original = JournalFault("journal_unavailable")
    monkeypatch.setattr(
        probe_module, "Journal", _poisoned_on("target_materialize_failed", original)
    )
    adapter = LostAnswer()
    rig = await make_rig(tmp_path, adapter=adapter)
    with pytest.raises(JournalFault) as raised:
        await probe(rig)
    assert raised.value is original
    assert len(adapter.materialized) == 1
    assert adapter.released == adapter.materialized and adapter.live == {}
    assert "target_release_started" not in events(rig)


@pytest.mark.parametrize("event_name", ["target_release_started", "target_release_finished"])
async def test_a_journal_fault_inside_the_release_is_raised_after_the_adapter_release(
    tmp_path, monkeypatch, event_name
):
    # No other error is travelling: the probe itself completed, and the fault
    # the release meets is returned by `_release` and raised by the runner.
    original = JournalFault("storage_capacity_exhausted")
    monkeypatch.setattr(probe_module, "Journal", _poisoned_on(event_name, original))
    rig = await make_rig(tmp_path)
    with pytest.raises(JournalFault) as raised:
        await probe(rig)
    assert raised.value is original
    assert rig.adapter.live == {}
    assert rig.adapter.materialized[0] in rig.adapter.released
    names = events(rig)
    assert "scoring_finished" in names
    assert ("target_release_started" in names) is (event_name == "target_release_finished")
    assert_untouched(rig)


async def test_an_unwritable_trace_summary_is_a_classified_storage_fault(tmp_path):
    rig = await make_rig(tmp_path)
    # A leftover entry at the summary's name: the exclusive create refuses it.
    traces = trace_root(rig)
    traces.mkdir(parents=True, mode=0o700)
    (rig.run_root / CONNECTION_LOGS_DIR).chmod(0o700)
    (traces / TRACE_FILE).write_text("leftover", encoding="utf-8")
    with pytest.raises(JournalFault) as raised:
        await probe(rig)
    assert str(raised.value) == "journal_unavailable"
    assert isinstance(raised.value.__cause__, FileExistsError)
    # The instance was released before the summary was written.
    assert rig.adapter.live == {}
    assert probe_settlement(rig.run_root, OP).cleanup == "released"


async def test_a_trace_directory_that_is_not_a_directory_is_a_classified_storage_fault(
    tmp_path,
):
    rig = await make_rig(tmp_path)
    (rig.run_root / CONNECTION_LOGS_DIR).write_text("not a directory", encoding="utf-8")
    with pytest.raises(JournalFault) as raised:
        await probe(rig)
    assert str(raised.value) == "journal_unavailable"
    assert rig.adapter.materialized == []


async def test_a_cancellation_with_a_refused_release_settles_as_failed(tmp_path):
    # Ruling 14: the gate reads the settlement; the leak is visible as `failed`.
    adapter = ProbeAdapter()

    def refuse_then_cancel() -> None:
        adapter.refuse_release = frozenset(adapter.live)
        raise asyncio.CancelledError

    adapter.send_script = refuse_then_cancel
    rig = await make_rig(tmp_path, adapter=adapter)
    with pytest.raises(asyncio.CancelledError):
        await probe(rig)
    assert len(adapter.live) == 1
    settled = probe_settlement(rig.run_root, OP)
    assert settled.outcome is None and settled.cleanup == "failed"
    assert settled.interrupted("cancelled")["cleanup"] == "failed"


async def test_a_cooperative_stop_with_a_refused_release_has_no_outcome_and_failed_cleanup(
    tmp_path,
):
    # Ruling 14: no outcome is invented for a stop; the cleanup says `failed`.
    stopped = False
    adapter = ProbeAdapter()

    def refuse_and_stop() -> None:
        nonlocal stopped
        adapter.refuse_release = frozenset(adapter.live)
        stopped = True

    adapter.send_script = refuse_and_stop
    rig = await make_rig(tmp_path, adapter=adapter)
    result = await probe(
        rig, environment=ScriptedEnvironment(messages=2), budget=budget_for(rig, lambda: stopped)
    )
    assert result.outcome is None and result.cleanup == "failed"
    assert len(adapter.live) == 1
    assert_probe(rig, result, leaked=True)


async def test_a_leftover_under_an_unadmitted_version_propagates_the_identity_error(
    tmp_path, monkeypatch
):
    # Ruling 16, as `campaign_inspection.release_leftover`: nothing is written,
    # and the operator admits the producing version and resumes.
    import evolution.tree.execution.journal as journal_module

    rig = await make_rig(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "resolve_package_version", lambda _resolver: "0.3.0a0")
        await _cut(rig, monkeypatch, "after_rollout_started")
    path = probe_path(rig.run_root, OP)
    before = journal_path(rig).read_bytes()
    with pytest.raises(probe_module.IdentityError, match="package_version_mismatch"):
        await release_probe_leftover(path, rig.cases, (), rig.adapter)
    assert journal_path(rig).read_bytes() == before
    assert len(rig.adapter.live) == 1
    released = await release_probe_leftover(
        path, rig.cases, (), rig.adapter, admitted_versions={"0.3.0a0"}
    )
    assert released == "released" and rig.adapter.live == {}


async def test_probe_ends_its_clone_session_and_preserves_the_callers_binding(
    tmp_path, monkeypatch
):
    import errno

    from evolution.tree.execution import scorer_traces
    from evolution.tree.target import connection_probe
    from tests.tree.execution.test_scorer_traces import _inputs

    scorer = subprocess_scorer(tmp_path, '{"metrics":{"score":0.5}}')
    rig = await make_rig(tmp_path, scorer=scorer)
    caller = tmp_path / "caller"
    caller.mkdir()
    case, view, candidate = _inputs(caller)
    await scorer.score_traced(case, view, candidate, trace_id="caller-first", pack_id="caller")
    caller_writer = scorer._writers["caller"]
    clones = []
    clone = connection_probe.scorer_with_trace_root

    def retain_clone(*args, **kwargs):
        result = clone(*args, **kwargs)
        clones.append(result)
        return result

    monkeypatch.setattr(connection_probe, "scorer_with_trace_root", retain_clone)
    with monkeypatch.context() as patch:

        def enospc(*args):
            raise OSError(errno.ENOSPC, "full")

        patch.setattr(scorer_traces, "_raw_write", enospc)
        with pytest.raises(scorer_traces.ScorerTraceStorageFault) as fault:
            await probe(rig)
    assert fault.value.code == "storage_capacity_exhausted"
    assert clones and all(not moved._writers for moved in clones)
    assert scorer._writers == {"caller": caller_writer}
    await scorer.score_traced(case, view, candidate, trace_id="caller-second", pack_id="caller")
    scorer._close_traces()


@pytest.mark.parametrize("terminal", ["time_budget", "turn_budget"])
@pytest.mark.parametrize("retries", [None, 0, 1, 9])
async def test_preflight_sample_retries_remeasure_an_indeterminate_sample(
    tmp_path, retries, terminal
):
    adapter = ProbeAdapter()
    calls = []

    def send():
        calls.append(1)
        if len(calls) == 1 and terminal == "time_budget":
            raise ScriptedDeadlineExceeded("one indeterminate try")

    adapter.send_script = send
    rig = await make_rig(tmp_path, adapter=adapter)

    class FirstTryTooLong(ScriptedEnvironment):
        tries = 0

        async def next(self, case, history):
            if not history.turns:
                self.tries += 1
            self.messages = rig.policy.budget.max_turns + 1 if self.tries == 1 else 1
            return await super().next(case, history)

    environment = FirstTryTooLong() if terminal == "turn_budget" else rig.environment
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    identity = probe_identity(
        base, rig.cases, probe_seed(BINDING, rig.ref.digest), sample_retries=retries
    )
    result = await run_probe(
        run_root=rig.run_root,
        operation_id=OP,
        campaign_run_id=CAMPAIGN_RUN_ID,
        identity=identity,
        corpus=rig.cases,
        secrets=(),
        adapter=rig.adapter,
        candidate=rig.ref,
        strategy=STRATEGY,
        call_attempts=None,
        environment=environment,
        scorer=rig.scorer,
        budget=budget_for(rig),
        expected=rig.expected,
        contract=CONTRACT,
        primary_metric=PRIMARY,
    )
    count = 2 if retries else 1
    if terminal == "time_budget":
        assert len(calls) == count
    assert result.outcome.status == (
        "compatible" if retries else "connection_preflight_inconclusive"
    )
    starts = [row for row in records(rig) if row["event"] == "rollout_started"]
    assert len(starts) == count
    executions = [
        row for row in records(rig) if row["event"] in {"rollout_finished", "rollout_failed"}
    ]
    assert executions[0]["payload"]["outcome"] == terminal
    assert [row["payload"].get("retry_ordinal") for row in starts] == (
        [None] if retries is None else list(range(count))
    )
    assert rig.adapter.live == {}


async def test_retries_above_eight_are_admitted_only_for_connection_probe(tmp_path):
    from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1

    rig = await make_rig(tmp_path)
    base = measurement_identity(rig.identity, rig.cases, rig.ref.digest, "champion")
    declared = probe_identity(base, rig.cases, 1, sample_retries=9)
    assert declared.sample_retries == 9
    with pytest.raises(IdentityError):
        RunIdentityV1.from_payload({**base.to_payload(), "sample_retries": 9})
