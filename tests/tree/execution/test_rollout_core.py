"""Behavioral contracts for the Evolution v2 rollout core."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import evolution.tree.execution.rollout as rollout_core
from evolution.tree.execution.candidate_integrity import artifact_digest
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    CallRetries,
    CandidateIntegrityError,
    CandidateView,
    EnvironmentIdentity,
    EvalCase,
    History,
    InstanceProvenance,
    Message,
    Provenance,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutKey,
    RolloutRecoveryError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    SampleIdentity,
    ScoreResult,
    ScorerIdentity,
    ScriptedEnvironment,
    SeedMode,
    SemanticIdentity,
    StaticInputEnvironment,
    TargetIdentity,
    Terminal,
    Trajectory,
    Turn,
    TurnResult,
    are_samples_paired,
    condition_key,
    validate_score_result,
)
from evolution.tree.execution.rollout import classify_rollout_resume, rollout
from evolution.tree.execution.rollout_scoring import ScoringError, SubprocessScorer
from evolution.tree.execution.sample_outcomes import (
    SampleOutcome,
    classify_sample_outcome,
    disposition_for,
)
from evolution.tree.execution.usage import Usage


class _RecordingJournal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, event: dict[str, Any]) -> int:
        self.records.append(event)
        return len(self.records)


class _Session:
    def __init__(self, requested_seed: int | None, effective_seed: int | None) -> None:
        self.session_id = "session-1"
        self.requested_seed = requested_seed
        self.effective_seed = effective_seed


class _InMemoryInstance:
    def __init__(
        self,
        candidate_digest: str = "candidate",
        instance_id: str = "instance-1",
        *,
        seed_mode: SeedMode = "exact",
        effective_seed: int | None = None,
        outcomes: list[str] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.seed_mode = seed_mode
        self.effective_seed = effective_seed
        self.target_identity = TargetIdentity(
            SemanticIdentity("contract", "deps", "settings", seed_mode, "2", "runtime"),
            InstanceProvenance("boot", candidate_digest),
            frozenset({"loaded_candidate_digest", "dependency_digest"}),
        )
        self.opened_public: dict[str, Any] | None = None
        self.open_request_ids: list[str] = []
        self.sent: list[tuple[str, Message]] = []
        self._send_results: dict[str, TurnResult] = {}
        self.closed = False
        # Scripted outcomes for successive `send` calls; today's fixed "ok" is
        # the default so every pre-existing call site is untouched. A call
        # beyond the scripted list also falls back to "ok". The sentinel
        # "time_budget" is not a real outcome: it tells this fake target to
        # sleep well past the rollout deadline, so the caller's `_external_call`
        # times the send out instead of returning a `TurnResult`.
        self._outcomes = outcomes
        self._send_index = 0

    async def identity(self) -> TargetIdentity:
        return self.target_identity

    async def open(self, case_public, seed: int | None, request_id: str) -> _Session:
        self.opened_public = dict(case_public)
        self.open_request_ids.append(request_id)
        effective_seed = seed if self.seed_mode == "exact" else self.effective_seed
        return _Session(seed, effective_seed)

    async def send(self, session: _Session, message: Message, request_id: str) -> TurnResult:
        if request_id in self._send_results:
            return self._send_results[request_id]
        outcome = "ok"
        if self._outcomes is not None and self._send_index < len(self._outcomes):
            outcome = self._outcomes[self._send_index]
        self._send_index += 1
        if outcome == "time_budget":
            await asyncio.sleep(5.0)
        self.sent.append((request_id, message))
        result = TurnResult(
            events=({"type": "message", "text": message.content.upper()},),
            usage=Usage(prompt=1, completion=1, cached=0, total=2),
            latency_ms=3,
            outcome=outcome,
        )
        self._send_results[request_id] = result
        return result

    async def close(self, session: _Session) -> None:
        self.closed = True


class _InMemoryAdapter:
    """Strict test adapter with idempotent materialize/release ownership."""

    capabilities = AdapterCapabilities("exact", 1, True, frozenset({"artifact_root_injection"}))

    def __init__(
        self,
        instances: dict[str, _InMemoryInstance] | None = None,
        journal: _RecordingJournal | None = None,
    ) -> None:
        self.instances = instances if instances is not None else {}
        self.journal = journal
        self.materialize_entry_events: list[str | None] = []
        self.release_entry_events: list[str | None] = []
        self.released: set[str] = set()

    async def materialize(
        self, candidate: CandidateView, strategy: str, instance_id: str
    ) -> _InMemoryInstance:
        self.materialize_entry_events.append(
            self.journal.records[-1]["event"] if self.journal and self.journal.records else None
        )
        if strategy not in self.capabilities.materialization:
            raise ValueError("unsupported materialization strategy")
        candidate.verify()
        if instance_id not in self.instances:
            self.instances[instance_id] = _InMemoryInstance(candidate.digest, instance_id)
        return self.instances[instance_id]

    async def release(self, instance_id: str) -> None:
        self.release_entry_events.append(
            self.journal.records[-1]["event"] if self.journal and self.journal.records else None
        )
        self.instances.pop(instance_id, None)
        self.released.add(instance_id)


def test_visibility_projections_physically_exclude_forbidden_data() -> None:
    """Adding oracle/scheduler fields to narrow views would cross a trust boundary."""

    case = EvalCase(
        case_id="case-1",
        group_id="group-1",
        stratum="support",
        tags=("offline",),
        public={"text": "hello"},
        private={"script": [{"content": "hello"}]},
        oracle={"answer": "secret"},
    )
    semantic = SemanticIdentity(
        contract_digest="contract",
        dependency_digest="dependencies",
        settings_hash="settings",
        seed_mode="exact",
        protocol_version="2",
        runtime_ref_digest="runtime",
    )
    target = TargetIdentity(
        semantic=semantic,
        instance=InstanceProvenance(boot_id="boot-1", loaded_candidate_digest="candidate-1"),
        attested=frozenset({"loaded_candidate_digest", "dependency_digest"}),
    )
    trajectory = Trajectory(
        case_id="case-1",
        sample_id="01",
        candidate_digest="candidate-1",
        environment_id="scripted-v1",
        history=History(),
        terminal="completed",
        provenance=Provenance(target=target, requested_seed=7, effective_seed=7),
    )

    environment_view = case.environment_view()
    scoring_view = trajectory.scoring_view()

    assert environment_view.public == {"text": "hello"}
    assert environment_view.private == {"script": ({"content": "hello"},)}
    assert not hasattr(environment_view, "oracle")
    assert scoring_view.to_payload() == {
        "history": {"turns": []},
        "terminal": "completed",
        "target": {
            "contract_digest": "contract",
            "dependency_digest": "dependencies",
            "settings_hash": "settings",
            "seed_mode": "exact",
            "protocol_version": "2",
            "runtime_ref_digest": "runtime",
        },
    }
    for forbidden in (
        "sample_id",
        "request_id",
        "boot_id",
        "loaded_candidate_digest",
        "candidate_digest",
        "requested_seed",
        "effective_seed",
    ):
        assert forbidden not in repr(scoring_view.to_payload())


def test_boundary_values_detach_from_mutable_caller_data() -> None:
    """Mutating caller-owned JSON after construction must not alter frozen evidence."""

    public = {"text": "hello", "nested": {"items": [1, 2]}}
    fields = {"route": {"labels": ["a"]}}
    case = EvalCase("case", "group", "stratum", (), public, {}, {})
    message = Message("hello", fields)

    public["nested"]["items"].append(3)
    fields["route"]["labels"].append("b")

    assert case.public == {"text": "hello", "nested": {"items": (1, 2)}}
    assert message.fields == {"route": {"labels": ("a",)}}
    with pytest.raises(TypeError):
        case.public["new"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        message.fields["new"] = "forbidden"  # type: ignore[index]


def test_boundary_values_reject_non_json_mutability_and_freeze_tags() -> None:
    """Frozen contracts cannot retain caller-owned sets or mutable tag lists."""

    tags = ["offline"]
    case = EvalCase("case", "group", "stratum", tags, {}, {}, {})  # type: ignore[arg-type]
    tags.append("changed")

    assert case.tags == ("offline",)
    with pytest.raises(ValueError, match="JSON-compatible"):
        EvalCase("case", "group", "stratum", (), {"bad": {1, 2}}, {}, {})
    with pytest.raises(ValueError, match="mapping keys"):
        Message("hello", {1: "bad"})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "factory",
    (
        lambda: EvalCase("", "group", "stratum", (), {}, {}, {}),
        lambda: EvalCase("case", "", "stratum", (), {}, {}, {}),
        lambda: EvalCase("case", "group", "", (), {}, {}, {}),
        lambda: SemanticIdentity("", "deps", "settings", "exact", "2", "runtime"),
        lambda: InstanceProvenance("", "candidate"),
        lambda: InstanceProvenance("boot", ""),
    ),
)
def test_blank_identity_parts_are_rejected(factory) -> None:
    """Blank identity fields would make cache/provenance comparisons ambiguous."""

    with pytest.raises(ValueError, match="must not be blank"):
        factory()


def test_adapter_capabilities_reject_undeclared_strategies_and_non_boolean_flags() -> None:
    """Materialization semantics must be declared, not inferred from arbitrary strings."""

    with pytest.raises(ValueError, match="instantiable"):
        AdapterCapabilities("exact", 1, 1, frozenset())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="materialization"):
        AdapterCapabilities("exact", 1, True, frozenset({"copy_some_files"}))


def test_history_rejects_untyped_events_and_noncontiguous_turns() -> None:
    """Missing event types or index gaps would make citations ambiguous."""

    usage = Usage(prompt=2, completion=3, cached=None, total=5)
    turn = Turn(
        index=0,
        message=Message("hello"),
        events=({"type": "message", "text": "answer"},),
        usage=usage,
        latency_ms=12,
        outcome="ok",
    )

    assert History((turn,)).to_payload() == {
        "turns": [
            {
                "index": 0,
                "message": {"content": "hello", "fields": {}},
                "events": [{"type": "message", "text": "answer"}],
                "usage": {"prompt": 2, "completion": 3, "cached": None, "total": 5},
                "latency_ms": 12,
                "outcome": "ok",
                "reason": None,
            }
        ]
    }
    with pytest.raises(ValueError, match="event type"):
        Turn(0, Message("hello"), ({"text": "answer"},), None, 0, "ok")
    with pytest.raises(ValueError, match="contiguous"):
        History((Turn(1, Message("hello"), (), None, 0, "ok"),))


@pytest.mark.parametrize(
    "result",
    (
        lambda: Usage(prompt=-1, completion=0, cached=0, total=0),
        lambda: Turn(0, Message("hello"), (), None, -1, "ok"),
        lambda: ScoreResult(metrics={"quality": float("nan")}),
    ),
)
def test_numeric_evidence_must_be_finite_and_nonnegative(result) -> None:
    """Negative or non-finite telemetry cannot enter durable evidence."""

    with pytest.raises(ValueError):
        result()


def test_trajectory_rejects_candidate_or_seed_provenance_mismatch() -> None:
    """A trajectory must not claim bytes or exact seeds its target did not attest."""

    semantic = SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime")
    target = TargetIdentity(
        semantic,
        InstanceProvenance("boot", "loaded-candidate"),
        frozenset({"loaded_candidate_digest", "dependency_digest"}),
    )
    with pytest.raises(ValueError, match="loaded candidate"):
        Trajectory(
            "case",
            "01",
            "different-candidate",
            "env",
            History(),
            "completed",
            Provenance(target, 7, 7),
        )
    with pytest.raises(ValueError, match="exact seed"):
        Provenance(target, 7, 8)


def test_target_identity_round_trips_through_its_payload() -> None:
    """C1 stores the attested identity in `instance_attested` and rebuilds it there."""

    identity = TargetIdentity(
        SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime"),
        InstanceProvenance("boot", "loaded-candidate"),
        frozenset({"dependency_digest", "loaded_candidate_digest"}),
    )

    payload = identity.to_payload()

    # A sorted list, so one identity always serializes to the same bytes.
    assert payload["attested"] == ["dependency_digest", "loaded_candidate_digest"]
    assert payload["instance"] == dict(boot_id="boot", loaded_candidate_digest="loaded-candidate")
    assert payload["semantic"]["seed_mode"] == "exact"
    assert TargetIdentity.from_payload(payload) == identity
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    "mutate",
    (
        lambda p: p.pop("attested"),
        lambda p: p.update(extra="unknown"),
        lambda p: p["semantic"].pop("settings_hash"),
        lambda p: p["semantic"].update(extra="unknown"),
        lambda p: p["instance"].pop("boot_id"),
        lambda p: p["semantic"].update(seed_mode="sometimes"),
        lambda p: p["instance"].update(boot_id=""),
        lambda p: p.update(attested=["dependency_digest", "dependency_digest"]),
        lambda p: p.update(attested="dependency_digest"),
        lambda p: p.update(attested=[7]),
        lambda p: p.update(instance=None),
    ),
)
def test_target_identity_from_payload_is_strict(mutate) -> None:
    """The payload is read back from a journal C1 did not write: nothing is guessed."""

    payload = TargetIdentity(
        SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime"),
        InstanceProvenance("boot", "loaded-candidate"),
        frozenset({"dependency_digest", "loaded_candidate_digest"}),
    ).to_payload()
    mutate(payload)

    with pytest.raises(ValueError):
        TargetIdentity.from_payload(payload)


def test_condition_key_excludes_instance_provenance() -> None:
    """Different boot/candidate instances under one semantic target remain comparable."""

    semantic = SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime")
    environment = EnvironmentIdentity("scripted", "env-digest")
    scorer = ScorerIdentity("subprocess", "scorer-digest")
    first = TargetIdentity(semantic, InstanceProvenance("boot-a", "champion"), frozenset())
    second = TargetIdentity(semantic, InstanceProvenance("boot-b", "candidate"), frozenset())

    assert condition_key(first, environment, scorer, "set", 3, (1, 2, 3), "metrics") == (
        "contract",
        "deps",
        "settings",
        "exact",
        "2",
        "runtime",
        "scripted",
        "env-digest",
        "subprocess",
        "scorer-digest",
        "set",
        3,
        (1, 2, 3),
        "metrics",
    )
    assert condition_key(
        first, environment, scorer, "set", 3, (1, 2, 3), "metrics"
    ) == condition_key(second, environment, scorer, "set", 3, (1, 2, 3), "metrics")


def test_only_exact_same_case_repeat_and_confirmed_seed_form_a_pair() -> None:
    """Unpaired modes or mismatched effective seeds must never enter paired statistics."""

    champion = SampleIdentity("case", 2, "champion", 9, 9)
    candidate = SampleIdentity("case", 2, "candidate", 9, 9)

    assert are_samples_paired(champion, candidate, "exact")
    assert not are_samples_paired(champion, candidate, "unpaired")
    assert not are_samples_paired(champion, candidate, "none")
    assert not are_samples_paired(champion, SampleIdentity("case", 2, "candidate", 9, 10), "exact")
    assert not are_samples_paired(champion, SampleIdentity("other", 2, "candidate", 9, 9), "exact")


def test_seed_evidence_cannot_use_boolean_or_mismatched_schedule_modes() -> None:
    """Python booleans are integers but must never masquerade as exact seeds."""

    with pytest.raises(ValueError, match="requested_seed"):
        SampleIdentity("case", 1, "candidate", True, True)  # type: ignore[arg-type]
    target = TargetIdentity(
        SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime"),
        InstanceProvenance("boot", "candidate"),
        frozenset(),
    )
    with pytest.raises(ValueError, match="exact seed schedule"):
        condition_key(
            target,
            EnvironmentIdentity("scripted", "environment"),
            ScorerIdentity("subprocess", "scorer"),
            "set",
            1,
            (None,),
            "metrics",
        )


def test_candidate_view_detects_mutation_of_declared_artifacts(tmp_path: Path) -> None:
    """A scorer mutation must invalidate its read-only Candidate view."""

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    skill = candidate / "skill.py"
    skill.write_text("VALUE = 1\n", encoding="utf-8")
    view = CandidateView(candidate, artifact_digest(candidate, ("skill.py",)), ("skill.py",))

    view.verify()
    skill.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(CandidateIntegrityError, match="Candidate bytes changed"):
        view.verify()


@pytest.mark.parametrize(
    ("oracle", "critical_pass"),
    (({"critical": True}, None), ({}, True)),
)
def test_critical_score_shape_follows_the_case_oracle(oracle, critical_pass) -> None:
    """Critical verdicts must be present exactly for cases declared critical."""

    case = EvalCase("case", "group", "stratum", (), {}, {}, oracle)
    result = ScoreResult({"quality": 1.0}, critical_pass=critical_pass)

    with pytest.raises(ValueError, match="critical_pass"):
        validate_score_result(case, result)


@pytest.mark.asyncio
async def test_static_one_turn_uses_common_loop_and_completes_after_final_none() -> None:
    """Checking max_turns before final Environment.next would misclassify this sample."""

    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {"text": "hello", "locale": "ru"},
        {"private_canary": "PRIVATE-SECRET"},
        {"answer": "ORACLE-SECRET"},
    )
    instance = _InMemoryInstance()
    journal = _RecordingJournal()

    trajectory = await rollout(
        case,
        RolloutSample("01", "candidate", 7),
        instance,
        StaticInputEnvironment(),
        RolloutBudget(max_turns=1, timeout_seconds=1),
        journal,
    )

    assert trajectory.terminal == "completed"
    assert [turn.message.content for turn in trajectory.history.turns] == ["hello"]
    assert instance.opened_public == {"text": "hello", "locale": "ru"}
    assert instance.closed
    assert len(instance.sent) == 1
    assert all("hello" not in repr(record) for record in journal.records)
    assert all("PRIVATE-SECRET" not in repr(record) for record in journal.records)
    assert all("ORACLE-SECRET" not in repr(record) for record in journal.records)
    assert {"target_open_started", "target_open_finished"} <= {
        record["event"] for record in journal.records
    }
    assert {"target_send_started", "target_send_finished"} <= {
        record["event"] for record in journal.records
    }
    assert {"target_close_started", "target_close_finished"} <= {
        record["event"] for record in journal.records
    }
    finished = [record for record in journal.records if record["event"] == "rollout_finished"]
    assert len(finished) == 1
    assert finished[0]["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_unpaired_rollout_records_the_targets_effective_seed() -> None:
    """Treating unpaired like none would discard honest provenance from the target."""

    instance = _InMemoryInstance(seed_mode="unpaired", effective_seed=41)

    trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("candidate-01", "candidate", None),
        instance,
        StaticInputEnvironment(),
        RolloutBudget(max_turns=1, timeout_seconds=1),
        _RecordingJournal(),
    )

    assert trajectory.provenance.requested_seed is None
    assert trajectory.provenance.effective_seed == 41
    assert not are_samples_paired(
        SampleIdentity("case", 1, "champion", None, 17),
        SampleIdentity("case", 1, "candidate", None, 41),
        "unpaired",
    )


@pytest.mark.asyncio
async def test_none_seed_mode_rejects_an_effective_seed_claim() -> None:
    """A seedless target must not claim a seed that its capability says it cannot expose."""

    instance = _InMemoryInstance(seed_mode="none", effective_seed=41)

    with pytest.raises(RolloutInfrastructureError, match="target open failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("candidate-01", "candidate", None),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )

    assert instance.closed


@pytest.mark.asyncio
async def test_a_candidate_mismatch_stays_infrastructure_failed_under_call_retries() -> None:
    """An identity mismatch is structural, never target instability --
    `classify_failure` must not fall through to its transient default for it,
    whatever `call_retries` says (review fix: the raise now carries an explicit
    `identity_changed` code instead of the default `"infrastructure"`)."""

    instance = _InMemoryInstance(candidate_digest="other")
    budget = RolloutBudget(3, 30, call_retries=CallRetries(2, 2))
    journal = _RecordingJournal()

    # Under `/8` the check runs as the identity call's own `result_summary`
    # (final review, Important 2), so the failure is journaled as that call's
    # `_failed` record carrying `identity_changed` -- which is what lets the
    # campaign suspend as `target_changed` rather than
    # `target_contract_violated`. The wrapper's `__cause__` still carries it too.
    with pytest.raises(RolloutInfrastructureError) as failure:
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            budget,
            journal,
        )
    assert getattr(failure.value.__cause__, "code", None) == "identity_changed"
    failed = [e for e in journal.records if e["event"] == "target_identity_failed"]
    assert [(e["call_attempt"], e["error_code"]) for e in failed] == [(1, "identity_changed")]
    # A core check is not target instability: it is never resent.
    assert "target_identity_finished" not in [e["event"] for e in journal.records]


@pytest.mark.asyncio
async def test_a_candidate_mismatch_journals_a_finished_call_without_call_retries() -> None:
    """`/5`-`/7` is untouched: the identity call still finishes and the core
    check raises after it, so no `_failed` record is written at all."""

    journal = _RecordingJournal()

    with pytest.raises(RolloutInfrastructureError, match="target identity failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            _InMemoryInstance(candidate_digest="other"),
            StaticInputEnvironment(),
            RolloutBudget(3, 30),
            journal,
        )

    events = [e["event"] for e in journal.records]
    assert "target_identity_finished" in events
    assert "target_identity_failed" not in events
    assert all("error_code" not in e for e in journal.records)


@pytest.mark.asyncio
async def test_an_identity_drift_after_close_journals_its_code_under_call_retries() -> None:
    """The third core check that reads a successful call's value: the drift is
    journaled on the after-close identity call, coded `identity_changed`."""

    class DriftingInstance(_InMemoryInstance):
        def __init__(self) -> None:
            super().__init__()
            self.identity_calls = 0

        async def identity(self) -> TargetIdentity:
            self.identity_calls += 1
            identity = await super().identity()
            if self.identity_calls == 1:
                return identity
            return TargetIdentity(
                identity.semantic,
                InstanceProvenance("different-boot", "candidate"),
                identity.attested,
            )

    journal = _RecordingJournal()

    with pytest.raises(RolloutInfrastructureError, match="target identity failed after close"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            DriftingInstance(),
            StaticInputEnvironment(),
            RolloutBudget(3, 30, call_retries=CallRetries(2, 2)),
            journal,
        )

    failed = [e for e in journal.records if e["event"] == "target_identity_failed"]
    assert [(e["call_attempt"], e["error_code"], e["phase"]) for e in failed] == [
        (1, "identity_changed", "after_close")
    ]


@pytest.mark.asyncio
async def test_a_seed_contract_violation_stays_infrastructure_failed_under_call_retries() -> None:
    """The same for a seed-contract violation (contract, not
    transient) -- review fix: an explicit `invalid_response` code."""

    instance = _InMemoryInstance(seed_mode="none", effective_seed=41)
    budget = RolloutBudget(3, 30, call_retries=CallRetries(2, 2))
    journal = _RecordingJournal()

    # Same shape: `_validate_session_for_sample` runs as the open call's own
    # `result_summary` under `/8`, so its `invalid_response` reaches the
    # journal and the suspension reads `target_contract_violated` off it.
    with pytest.raises(RolloutInfrastructureError) as failure:
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("candidate-01", "candidate", None),
            instance,
            StaticInputEnvironment(),
            budget,
            journal,
        )
    assert getattr(failure.value.__cause__, "code", None) == "invalid_response"
    failed = [e for e in journal.records if e["event"] == "target_open_failed"]
    assert [(e["call_attempt"], e["error_code"]) for e in failed] == [(1, "invalid_response")]
    # The session the target opened is still this rollout's to close, even
    # though the check that rejected it ran inside the open call.
    assert instance.closed
    assert [e["event"] for e in journal.records].count("target_close_finished") == 1


@pytest.mark.asyncio
async def test_scripted_environment_drives_multiple_turns_through_the_same_loop() -> None:
    """Bypassing the common loop for scripted dialogue would hide budget/journal bugs."""

    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {"locale": "ru"},
        {
            "script": [
                {"content": "first", "fields": {"step": 1}},
                {"content": "second"},
            ]
        },
        {"answer": "secret"},
    )
    instance = _InMemoryInstance()

    trajectory = await rollout(
        case,
        RolloutSample("01", "candidate", 7),
        instance,
        ScriptedEnvironment(),
        RolloutBudget(max_turns=2, timeout_seconds=1),
        _RecordingJournal(),
    )

    assert trajectory.terminal == "completed"
    assert [turn.message.to_payload() for turn in trajectory.history.turns] == [
        {"content": "first", "fields": {"step": 1}},
        {"content": "second", "fields": {}},
    ]
    assert [turn.index for turn in trajectory.history.turns] == [0, 1]
    assert instance.closed


@pytest.mark.asyncio
async def test_environment_failure_is_distinct_and_still_closes_the_session() -> None:
    """A failed Environment role must not be mislabeled as target/Candidate failure."""

    class BrokenEnvironment:
        identity = EnvironmentIdentity("broken", "broken-v1")

        async def next(self, case, history):
            raise RuntimeError("PRIVATE-SECRET")

    instance = _InMemoryInstance()
    journal = _RecordingJournal()

    with pytest.raises(RolloutEnvironmentError, match="Environment failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {}, {"secret": "PRIVATE-SECRET"}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            BrokenEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            journal,
        )

    assert instance.closed
    assert all("PRIVATE-SECRET" not in repr(record) for record in journal.records)


@pytest.mark.asyncio
async def test_malformed_environment_message_is_role_failure() -> None:
    """An invalid Environment result must not be blamed on the target adapter."""

    class MalformedEnvironment:
        identity = EnvironmentIdentity("broken", "broken-v1")

        async def next(self, case, history):
            return {"content": "not-a-Message"}

    instance = _InMemoryInstance()

    with pytest.raises(RolloutEnvironmentError, match="invalid Message"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            MalformedEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )

    assert instance.closed


@pytest.mark.asyncio
async def test_turn_budget_means_environment_requested_an_extra_turn() -> None:
    """A turn budget must not bypass the Environment's final stop decision."""

    class CountingEnvironment:
        identity = EnvironmentIdentity("counting", "counting-v1")

        def __init__(self) -> None:
            self.calls = 0

        async def next(self, case, history):
            self.calls += 1
            return Message(f"turn-{self.calls}")

    environment = CountingEnvironment()
    instance = _InMemoryInstance()

    trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {}, {}, {}),
        RolloutSample("01", "candidate", 7),
        instance,
        environment,
        RolloutBudget(max_turns=1, timeout_seconds=1),
        _RecordingJournal(),
    )

    assert trajectory.terminal == "turn_budget"
    assert environment.calls == 2
    assert len(instance.sent) == 1
    assert instance.closed


@pytest.mark.asyncio
async def test_case_turn_budget_caps_the_evaluation_set_budget() -> None:
    """Scenario max_turns is a stricter cap, never permission to exceed the set budget."""

    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {},
        {
            "max_turns": 1,
            "script": [{"content": "first"}, {"content": "second"}],
        },
        {},
    )
    instance = _InMemoryInstance()

    trajectory = await rollout(
        case,
        RolloutSample("01", "candidate", 7),
        instance,
        ScriptedEnvironment(),
        RolloutBudget(max_turns=3, timeout_seconds=1),
        _RecordingJournal(),
    )

    assert trajectory.terminal == "turn_budget"
    assert [turn.message.content for turn in trajectory.history.turns] == ["first"]
    assert len(instance.sent) == 1


@pytest.mark.asyncio
async def test_queue_wait_precedes_the_active_rollout_deadline() -> None:
    """Queue delay before rollout entry must not consume the active deadline."""

    permit = asyncio.Semaphore(0)

    async def run_after_permit() -> Trajectory:
        async with permit:
            return await rollout(
                EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
                RolloutSample("01", "candidate", 7),
                _InMemoryInstance(),
                StaticInputEnvironment(),
                RolloutBudget(max_turns=1, timeout_seconds=0.05),
                _RecordingJournal(),
            )

    task = asyncio.create_task(run_after_permit())
    await asyncio.sleep(0.1)
    permit.release()
    trajectory = await task

    assert trajectory.terminal == "completed"
    assert trajectory.disposition.kind == "score"


@pytest.mark.parametrize("slow_boundary", ("identity", "open"))
@pytest.mark.asyncio
async def test_pre_session_core_deadline_is_rollout_owned(slow_boundary: str) -> None:
    """A core deadline before session creation must not become target failure."""

    class SlowPreSessionInstance(_InMemoryInstance):
        async def identity(self):
            if slow_boundary == "identity":
                await asyncio.sleep(1)
            return await super().identity()

        async def open(self, case_public, seed, request_id):
            if slow_boundary == "open":
                await asyncio.sleep(1)
            return await super().open(case_public, seed, request_id)

    journal = _RecordingJournal()

    with pytest.raises(RolloutTimeBudgetExceeded) as failure:
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            SlowPreSessionInstance(),
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=0.02),
            journal,
        )

    assert failure.value.terminal == "time_budget"
    assert failure.value.disposition.kind == "indeterminate"
    assert failure.value.disposition.owner == "rollout"
    assert failure.value.disposition.reason == "rollout_time_budget_exhausted"
    failed = [record for record in journal.records if record["event"].endswith("_failed")]
    assert len(failed) == 1
    assert failed[0]["event"] == f"target_{slow_boundary}_failed"
    assert failed[0]["case_id"] == "case"
    assert failed[0]["sample_id"] == "01"
    assert failed[0]["candidate_digest"] == "candidate"
    assert failed[0]["instance_id"] == "instance-1"
    assert isinstance(failed[0]["latency_ms"], int)
    assert failed[0]["error_class"] == "deadline_timeout"
    if slow_boundary == "open":
        assert isinstance(failed[0]["request_id"], str)
    else:
        assert "request_id" not in failed[0]
    assert not any(record["event"] == "rollout_finished" for record in journal.records)


@pytest.mark.parametrize("slow_boundary", ("environment", "target"))
@pytest.mark.asyncio
async def test_deadline_bounds_environment_and_target_calls(slow_boundary: str) -> None:
    """Neither next nor send may outlive the sample's wall-clock deadline."""

    class SlowEnvironment:
        identity = EnvironmentIdentity("slow", "slow-v1")

        async def next(self, case, history):
            await asyncio.sleep(1)
            return None

    class SlowInstance(_InMemoryInstance):
        async def send(self, session, message, request_id):
            await asyncio.sleep(1)
            return await super().send(session, message, request_id)

    instance = SlowInstance() if slow_boundary == "target" else _InMemoryInstance()
    environment = StaticInputEnvironment() if slow_boundary == "target" else SlowEnvironment()

    trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("01", "candidate", 7),
        instance,
        environment,
        RolloutBudget(max_turns=1, timeout_seconds=0.02),
        _RecordingJournal(),
    )

    assert trajectory.terminal == "time_budget"
    assert trajectory.disposition.kind == "indeterminate"
    assert trajectory.disposition.owner == "rollout"
    assert trajectory.disposition.reason == "rollout_time_budget_exhausted"
    assert instance.closed


@pytest.mark.asyncio
async def test_environment_timeout_before_deadline_is_role_failure() -> None:
    """A callee timeout before the rollout deadline must not become scoreable time_budget."""

    class TimedOutEnvironment:
        identity = EnvironmentIdentity("timed-out", "timed-out-v1")

        async def next(self, case, history):
            raise TimeoutError("provider request timed out")

    instance = _InMemoryInstance()
    journal = _RecordingJournal()

    with pytest.raises(RolloutEnvironmentError, match="Environment failed") as failure:
        await rollout(
            EvalCase("case", "group", "stratum", (), {}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            TimedOutEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            journal,
        )

    assert isinstance(failure.value.__cause__, TimeoutError)
    failed = [record for record in journal.records if record["event"] == "environment_next_failed"]
    assert failed[0]["error_class"] == "TimeoutError"
    assert isinstance(failed[0]["latency_ms"], int)
    assert "duration_seconds" not in failed[0]
    assert instance.closed


@pytest.mark.asyncio
async def test_target_timeout_before_deadline_is_infrastructure_failure() -> None:
    """A transport timeout before the rollout deadline must not blame the Candidate."""

    class TimedOutInstance(_InMemoryInstance):
        async def send(self, session, message, request_id):
            raise TimeoutError("transport request timed out")

    instance = TimedOutInstance()
    journal = _RecordingJournal()

    with pytest.raises(RolloutInfrastructureError, match="target send failed") as failure:
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            journal,
        )

    assert isinstance(failure.value.__cause__, TimeoutError)
    failed = [record for record in journal.records if record["event"] == "target_send_failed"]
    assert failed[0]["error_class"] == "TimeoutError"
    assert isinstance(failed[0]["latency_ms"], int)
    assert "duration_seconds" not in failed[0]
    assert instance.closed


@pytest.mark.asyncio
async def test_boundary_journal_uses_core_latency_ms_without_target_collision() -> None:
    """Boundary timing must not reuse the target-reported Turn latency field."""

    journal = _RecordingJournal()
    await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("01", "candidate", 7),
        _InMemoryInstance(),
        StaticInputEnvironment(),
        RolloutBudget(max_turns=1, timeout_seconds=1),
        journal,
    )

    terminal_boundary_records = [
        record
        for record in journal.records
        if record["event"].endswith(("_finished", "_failed"))
        and record["event"] != "rollout_finished"
    ]
    assert terminal_boundary_records
    assert all(
        isinstance(record["latency_ms"], int)
        and record["latency_ms"] >= 0
        and "duration_seconds" not in record
        for record in terminal_boundary_records
    )
    send_finished = next(
        record for record in journal.records if record["event"] == "target_send_finished"
    )
    assert send_finished["target_latency_ms"] == 3


@pytest.mark.asyncio
async def test_target_failed_and_controlled_cancellation_are_distinct() -> None:
    """Target behavior and scheduler cancellation must remain separate terminals."""

    class FailingTarget(_InMemoryInstance):
        async def send(self, session, message, request_id):
            return TurnResult(
                events=({"type": "target_error", "code": "unavailable"},),
                usage=None,
                latency_ms=1,
                outcome="target_failed",
            )

    failed = FailingTarget()
    failed_trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("01", "candidate", 7),
        failed,
        StaticInputEnvironment(),
        RolloutBudget(max_turns=1, timeout_seconds=1),
        _RecordingJournal(),
    )
    cancelled = _InMemoryInstance()
    cancelled_trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("01", "candidate", 7),
        cancelled,
        StaticInputEnvironment(),
        RolloutBudget(max_turns=1, timeout_seconds=1, cancelled=lambda: True),
        _RecordingJournal(),
    )

    assert failed_trajectory.terminal == "target_failed"
    assert len(failed_trajectory.history.turns) == 1
    assert failed.closed
    assert cancelled_trajectory.terminal == "cancelled"
    assert cancelled.sent == []
    assert cancelled.closed


@pytest.mark.asyncio
async def test_cancellation_observed_after_environment_does_not_send() -> None:
    """A stop arriving during Environment.next must prevent the next target call."""

    state = {"cancelled": False}

    class CancellingEnvironment:
        identity = EnvironmentIdentity("cancelling", "cancelling-v1")

        async def next(self, case, history):
            state["cancelled"] = True
            return Message("must-not-send")

    instance = _InMemoryInstance()
    trajectory = await rollout(
        EvalCase("case", "group", "stratum", (), {}, {}, {}),
        RolloutSample("01", "candidate", 7),
        instance,
        CancellingEnvironment(),
        RolloutBudget(
            max_turns=1,
            timeout_seconds=1,
            cancelled=lambda: state["cancelled"],
        ),
        _RecordingJournal(),
    )

    assert trajectory.terminal == "cancelled"
    assert instance.sent == []
    assert instance.closed


@pytest.mark.asyncio
async def test_target_protocol_failure_is_infrastructure_and_closes_session() -> None:
    """An adapter exception is infrastructure failure, never target_failed."""

    class BrokenInstance(_InMemoryInstance):
        async def send(self, session, message, request_id):
            raise RuntimeError("RAW-TRANSPORT-SECRET")

    instance = BrokenInstance()
    journal = _RecordingJournal()

    with pytest.raises(RolloutInfrastructureError, match="target send failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            journal,
        )

    assert instance.closed
    assert all("RAW-TRANSPORT-SECRET" not in repr(record) for record in journal.records)


@pytest.mark.asyncio
async def test_malformed_turn_result_is_infrastructure_failure() -> None:
    """An adapter cannot smuggle an unvalidated result into History."""

    class MalformedInstance(_InMemoryInstance):
        async def send(self, session, message, request_id):
            return {"outcome": "ok", "events": []}

    instance = MalformedInstance()

    with pytest.raises(RolloutInfrastructureError, match="target send failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )

    assert instance.closed


@pytest.mark.asyncio
async def test_malformed_turn_result_stays_infrastructure_failed_under_call_retries() -> None:
    """The core's own `TurnResult` shape check (review fix: explicit
    `invalid_response`) is a deterministic contract failure too, whatever
    `call_retries` says."""

    class MalformedInstance(_InMemoryInstance):
        async def send(self, session, message, request_id):
            return {"outcome": "ok", "events": []}

    instance = MalformedInstance()
    journal = _RecordingJournal()
    budget = RolloutBudget(3, 30, call_retries=CallRetries(2, 2))

    with pytest.raises(RolloutInfrastructureError):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            budget,
            journal,
        )
    failed = [e for e in journal.records if e["event"] == "target_send_failed"]
    assert failed and failed[-1]["error_code"] == "invalid_response"


@pytest.mark.asyncio
async def test_malformed_open_result_is_still_closed() -> None:
    """Cleanup cannot depend on fields missing from a malformed Session."""

    class MalformedSessionInstance(_InMemoryInstance):
        async def open(self, case_public, seed, request_id):
            return object()

        async def close(self, session):
            self.closed = True

    instance = MalformedSessionInstance()

    with pytest.raises(RolloutInfrastructureError, match="target open failed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )

    assert instance.closed


@pytest.mark.asyncio
async def test_identity_drift_after_close_is_infrastructure_failure() -> None:
    """A boot-id change inside one instance invalidates the rollout evidence."""

    class DriftingInstance(_InMemoryInstance):
        def __init__(self) -> None:
            super().__init__()
            self.identity_calls = 0

        async def identity(self):
            self.identity_calls += 1
            identity = await super().identity()
            if self.identity_calls == 1:
                return identity
            return TargetIdentity(
                identity.semantic,
                InstanceProvenance("different-boot", "candidate"),
                identity.attested,
            )

    instance = DriftingInstance()

    with pytest.raises(RolloutInfrastructureError, match="identity changed"):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )

    assert instance.closed


@pytest.mark.asyncio
async def test_close_failure_is_infrastructure_and_preserves_primary_failure() -> None:
    """Cleanup failure must surface without discarding the Environment failure."""

    class BrokenEnvironment:
        identity = EnvironmentIdentity("broken", "broken-v1")

        async def next(self, case, history):
            raise RuntimeError("environment detail")

    class BrokenClose(_InMemoryInstance):
        async def close(self, session):
            self.closed = True
            raise RuntimeError("close detail")

    instance = BrokenClose()
    journal = _RecordingJournal()

    with pytest.raises(RolloutInfrastructureError, match="target close failed") as caught:
        await rollout(
            EvalCase("case", "group", "stratum", (), {}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            BrokenEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            journal,
        )

    assert isinstance(caught.value.primary_failure, RolloutEnvironmentError)
    assert instance.closed
    assert [record["event"] for record in journal.records].count("target_close_started") == 1
    assert [record["event"] for record in journal.records].count("target_close_failed") == 1


@pytest.mark.asyncio
async def test_request_ids_are_scoped_to_one_live_rollout_attempt() -> None:
    """Reusing durable sample identity must not recreate ids from an earlier attempt."""

    async def run_sample(sample_id: str):
        instance = _InMemoryInstance()
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample(sample_id, "candidate", 7),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(max_turns=1, timeout_seconds=1),
            _RecordingJournal(),
        )
        return instance.open_request_ids[0], instance.sent[0][0]

    first = await run_sample("01")
    repeated = await run_sample("01")
    second = await run_sample("02")

    assert first != repeated
    assert first != second
    assert repeated != second
    assert first[0] != first[1]


def test_resume_reuses_finished_interrupts_started_and_schedules_only_absent() -> None:
    """A crash must never turn an unfinished target session into a replay command."""

    finished = RolloutKey("case-a", "01", "candidate")
    interrupted = RolloutKey("case-b", "01", "candidate")
    absent = RolloutKey("case-c", "01", "candidate")
    records = (
        {"event": "rollout_started", **finished.to_payload()},
        {
            "event": "rollout_finished",
            **finished.to_payload(),
            "terminal": "completed",
            "turns": 1,
            "duration_seconds": 0.1,
        },
        {"event": "rollout_started", **interrupted.to_payload()},
        {
            "event": "target_send_started",
            **interrupted.to_payload(),
            "request_id": "unfinished-request",
            "turn": 0,
        },
    )

    dispositions = classify_rollout_resume((finished, interrupted, absent), records)

    assert [(item.key, item.action) for item in dispositions] == [
        (finished, "reuse"),
        (interrupted, "interrupt"),
        (absent, "schedule"),
    ]
    assert dispositions[0].terminal == "completed"
    assert dispositions[0].sample_disposition is None
    assert dispositions[1].terminal == "interrupted"
    interrupted_disposition = dispositions[1].sample_disposition
    assert interrupted_disposition is not None
    assert interrupted_disposition.outcome == "interrupted"
    assert interrupted_disposition.kind == "indeterminate"
    assert interrupted_disposition.owner == "rollout"
    assert interrupted_disposition.reason == "rollout_interrupted"
    assert dispositions[2].terminal is None
    assert dispositions[2].sample_disposition is None


@pytest.mark.parametrize(
    "records",
    (
        (
            {
                "event": "rollout_finished",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
                "terminal": "completed",
                "turns": 1,
                "duration_seconds": 0.1,
            },
        ),
        (
            {
                "event": "rollout_started",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
            },
            {
                "event": "rollout_finished",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
                "turns": 1,
                "duration_seconds": 0.1,
            },
        ),
        (
            {
                "event": "rollout_started",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
            },
            {
                "event": "rollout_finished",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
                "terminal": "completed",
                "turns": 1,
                "duration_seconds": 0.1,
            },
            {
                "event": "rollout_finished",
                "case_id": "case",
                "sample_id": "01",
                "candidate_digest": "candidate",
                "terminal": "completed",
                "turns": 1,
                "duration_seconds": 0.2,
            },
        ),
    ),
)
def test_resume_rejects_torn_or_ambiguous_terminal_evidence(records) -> None:
    """Recovery must fail closed instead of guessing from malformed journal state."""

    key = RolloutKey("case", "01", "candidate")

    with pytest.raises(RolloutRecoveryError):
        classify_rollout_resume((key,), records)


@pytest.mark.asyncio
async def test_instance_lifecycle_journals_ownership_before_external_calls(
    tmp_path: Path,
) -> None:
    """Moving lifecycle journal writes after side effects would make crash cleanup unknowable."""

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(
        candidate_root,
        artifact_digest(candidate_root, ("skill.py",)),
        ("skill.py",),
    )
    journal = _RecordingJournal()
    adapter = _InMemoryAdapter(journal=journal)

    instance = await rollout_core.materialize_target(
        adapter,
        candidate,
        "artifact_root_injection",
        "instance-owned",
        journal,
    )
    await rollout_core.release_target(
        adapter,
        "instance-owned",
        candidate.digest,
        "artifact_root_injection",
        journal,
    )

    assert instance.instance_id == "instance-owned"
    assert adapter.materialize_entry_events == ["target_materialize_started"]
    assert adapter.release_entry_events == ["target_release_started"]
    assert [record["event"] for record in journal.records] == [
        "target_materialize_started",
        "target_materialize_finished",
        "target_release_started",
        "target_release_finished",
    ]
    assert all(
        record["instance_id"] == "instance-owned"
        and record["candidate_digest"] == candidate.digest
        and record["strategy"] == "artifact_root_injection"
        and isinstance(record.get("latency_ms"), int)
        for record in journal.records[1::2]
    )
    restarted = _InMemoryAdapter(adapter.instances, journal)
    assert (
        await rollout_core.recover_target_instances(restarted, tuple(journal.records), journal)
        == ()
    )
    assert restarted.release_entry_events == []


@pytest.mark.asyncio
async def test_instance_resume_releases_orphan_before_materializing_a_new_id(
    tmp_path: Path,
) -> None:
    """Reusing an orphaned id after a crash would violate durable instance ownership."""

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(
        candidate_root,
        artifact_digest(candidate_root, ("skill.py",)),
        ("skill.py",),
    )
    journal = _RecordingJournal()
    durable_resources: dict[str, _InMemoryInstance] = {}
    before_crash = _InMemoryAdapter(durable_resources, journal)
    await rollout_core.materialize_target(
        before_crash,
        candidate,
        "artifact_root_injection",
        "instance-before-crash",
        journal,
    )

    after_crash = _InMemoryAdapter(durable_resources, journal)
    recovered = await rollout_core.recover_target_instances(
        after_crash, tuple(journal.records), journal
    )
    replacement = await rollout_core.materialize_target(
        after_crash,
        candidate,
        "artifact_root_injection",
        "instance-after-crash",
        journal,
    )

    assert recovered == ("instance-before-crash",)
    assert after_crash.released == {"instance-before-crash"}
    assert "instance-before-crash" not in durable_resources
    assert replacement.instance_id == "instance-after-crash"
    assert set(durable_resources) == {"instance-after-crash"}


@pytest.mark.asyncio
async def test_instance_resume_collapses_idempotent_materialize_retries(
    tmp_path: Path,
) -> None:
    """A same-id transport retry must not become two owned resources or ambiguous evidence."""

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(
        candidate_root,
        artifact_digest(candidate_root, ("skill.py",)),
        ("skill.py",),
    )
    journal = _RecordingJournal()
    durable_resources: dict[str, _InMemoryInstance] = {}
    adapter = _InMemoryAdapter(durable_resources, journal)

    first = await rollout_core.materialize_target(
        adapter, candidate, "artifact_root_injection", "instance-retried", journal
    )
    retried = await rollout_core.materialize_target(
        adapter, candidate, "artifact_root_injection", "instance-retried", journal
    )
    restarted = _InMemoryAdapter(durable_resources, journal)
    recovered = await rollout_core.recover_target_instances(
        restarted, tuple(journal.records), journal
    )

    assert retried is first
    assert recovered == ("instance-retried",)
    assert restarted.released == {"instance-retried"}


@pytest.mark.asyncio
async def test_instance_resume_attempts_every_orphan_before_reporting_failures() -> None:
    """One failed cleanup must not leave later independently addressable orphans untouched."""

    class PartiallyFailingAdapter(_InMemoryAdapter):
        async def release(self, instance_id: str) -> None:
            self.release_entry_events.append(
                self.journal.records[-1]["event"] if self.journal and self.journal.records else None
            )
            if instance_id == "instance-a":
                raise RuntimeError("resource is temporarily busy")
            self.instances.pop(instance_id, None)
            self.released.add(instance_id)

    records = tuple(
        {
            "event": "target_materialize_started",
            "instance_id": instance_id,
            "candidate_digest": "candidate",
            "strategy": "artifact_root_injection",
        }
        for instance_id in ("instance-a", "instance-b", "instance-c")
    )
    journal = _RecordingJournal()
    adapter = PartiallyFailingAdapter(journal=journal)

    with pytest.raises(RolloutInfrastructureError, match="instance-a"):
        await rollout_core.recover_target_instances(adapter, records, journal)

    assert adapter.release_entry_events == [
        "target_release_started",
        "target_release_started",
        "target_release_started",
    ]
    assert adapter.released == {"instance-b", "instance-c"}
    assert [
        (record["event"], record["instance_id"])
        for record in journal.records
        if record["event"] in {"target_release_failed", "target_release_finished"}
    ] == [
        ("target_release_failed", "instance-a"),
        ("target_release_finished", "instance-b"),
        ("target_release_finished", "instance-c"),
    ]


@pytest.mark.asyncio
async def test_instance_resume_rejects_release_without_materialize_evidence() -> None:
    """Torn lifecycle evidence must not authorize release of an unproven instance id."""

    journal = _RecordingJournal()
    records = (
        {
            "event": "target_release_started",
            "instance_id": "unknown",
            "candidate_digest": "candidate",
            "strategy": "artifact_root_injection",
        },
    )
    adapter = _InMemoryAdapter(journal=journal)

    with pytest.raises(RolloutRecoveryError, match="without materialize"):
        await rollout_core.recover_target_instances(adapter, records, journal)

    assert adapter.release_entry_events == []


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (
        ("completed", "score"),
        ("turn_budget", "score"),
        ("time_budget", "indeterminate"),
        ("target_failed", "indeterminate"),
        ("environment_failed", "indeterminate"),
        ("scorer_failed", "indeterminate"),
        ("interrupted", "indeterminate"),
        ("cancelled", "exclude"),
        ("infrastructure_failed", "exclude"),
    ),
)
def test_sample_outcome_table_is_explicit(outcome: SampleOutcome, expected: str) -> None:
    """Scoring eligibility must never be inferred from a numeric score."""

    assert classify_sample_outcome(outcome) == expected


@pytest.mark.asyncio
async def test_offline_scripted_rollout_uses_production_subprocess_scorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first fixture exercises the real seams without network, models, or credentials."""

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(
        candidate_root,
        artifact_digest(candidate_root, ("skill.py",)),
        ("skill.py",),
    )
    case = EvalCase(
        "case",
        "group",
        "stratum",
        ("offline",),
        {"locale": "ru"},
        {"script": [{"content": "first"}, {"content": "second"}]},
        {"critical": True, "answer": "expected"},
    )
    journal = _RecordingJournal()
    adapter = _InMemoryAdapter(journal=journal)
    instance = await rollout_core.materialize_target(
        adapter,
        candidate,
        "artifact_root_injection",
        "instance-offline",
        journal,
    )
    try:
        trajectory = await rollout(
            case,
            RolloutSample("01", candidate.digest, 7),
            instance,
            ScriptedEnvironment(),
            RolloutBudget(max_turns=2, timeout_seconds=1),
            journal,
        )
    finally:
        await rollout_core.release_target(
            adapter,
            "instance-offline",
            candidate.digest,
            "artifact_root_injection",
            journal,
        )
    fixture = Path(__file__).parents[2] / "fixtures" / "rollout_scorer.py"
    monkeypatch.setenv("OPENAI_API_KEY", "MUST-NOT-REACH-SCORER")
    scorer_audit: list[dict[str, Any]] = []
    scorer = SubprocessScorer(
        argv=(sys.executable, str(fixture)),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run" / "scorer-traces",
        dependency_paths=(fixture,),
        audit=scorer_audit.append,
    )
    from uuid import UUID

    from evolution.tree.execution import rollout_scoring

    monkeypatch.setattr(rollout_scoring, "uuid4", lambda: UUID(int=1))

    result = await scorer.score(case, trajectory.scoring_view(), candidate)

    assert result.metrics == {"quality": 1.0}
    assert result.critical_pass is True
    assert result.verdicts == ()
    assert result.public_feedback == {"summary": "offline fixture passed"}
    assert result.details == {"turns": 2}
    assert adapter.released == {"instance-offline"}
    assert {
        "target_materialize_started",
        "target_materialize_finished",
        "target_release_started",
        "target_release_finished",
    } <= {record["event"] for record in journal.records}
    assert len(scorer_audit) == 1
    assert scorer_audit[0]["status"] == "completed"
    candidate.verify()
    from evolution.tree import read_scorer_trace

    trace = read_scorer_trace(tmp_path / "run", "00000000000000000000000000000001")
    assert trace is not None and trace.request is not None
    request = json.loads(trace.request)
    assert set(request["input"]) == {"case_id", "public", "oracle", "view"}
    for forbidden in (
        "sample_id",
        "request_id",
        "boot_id",
        "candidate_digest",
        "requested_seed",
        "effective_seed",
        "MUST-NOT-REACH-SCORER",
    ):
        assert forbidden not in repr(request)
        assert forbidden not in repr(scorer_audit)


def _scoring_inputs(tmp_path: Path, *, critical: bool = False, terminal: Terminal = "completed"):
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(
        candidate_root,
        artifact_digest(candidate_root, ("skill.py",)),
        ("skill.py",),
    )
    semantic = SemanticIdentity("contract", "deps", "settings", "exact", "2", "runtime")
    target = TargetIdentity(
        semantic,
        InstanceProvenance("boot", candidate.digest),
        frozenset({"loaded_candidate_digest", "dependency_digest"}),
    )
    trajectory = Trajectory(
        "case",
        "01",
        candidate.digest,
        "environment",
        History(),
        terminal,
        Provenance(target, 7, 7),
    )
    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {"text": "hello"},
        {},
        {"critical": True} if critical else {},
    )
    return case, trajectory.scoring_view(), candidate


def test_candidate_failed_is_a_scoring_terminal_on_a_trajectory(tmp_path: Path) -> None:
    """`Trajectory.__post_init__` admits `candidate_failed` as a scoring terminal.

    `_scoring_inputs` builds the `Trajectory` internally and hands back only
    its `ScoringView` (see the three existing callers above), so constructing
    it here with `terminal="candidate_failed"` and getting a view back at all
    is the proof that `__post_init__` accepted the terminal rather than
    raising `ValueError`. `disposition_for` is asserted directly rather than
    through the view, which does not carry `disposition`.
    """
    _case, view, _candidate = _scoring_inputs(tmp_path, terminal="candidate_failed")
    assert view.terminal == "candidate_failed"
    assert disposition_for("candidate_failed").kind == "score"


@pytest.mark.asyncio
async def test_subprocess_scorer_rejects_time_budget_before_process_start(
    tmp_path: Path,
) -> None:
    """Operational wall-clock exhaustion must never reach a Candidate scorer."""

    case, view, candidate = _scoring_inputs(tmp_path, terminal="time_budget")
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", 'print(\'{"metrics":{"quality":1}}\')'),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )

    with pytest.raises(ScoringError, match="terminal 'time_budget' is not scoreable"):
        await scorer.score(case, view, candidate)

    assert not (tmp_path / "run" / "scorer_traces").exists()


@pytest.mark.parametrize(
    ("script", "timeout", "error"),
    (
        ('print("not-json")', 1, "JSON object"),
        ("raise SystemExit(7)", 1, "exited 7"),
        ('print(\'{"metrics":{"quality":true}}\')', 1, "invalid ScoreResult"),
        ('print(\'{"metrics":{"quality":1},"unknown":1}\')', 1, "unknown fields"),
        (
            'print(\'{"metrics":{"quality":1},"verdicts":[{"citation":"event:0"}]}\')',
            1,
            "keys outside the grammar",
        ),
        (
            'print(\'{"metrics":{"quality":0},"metrics":{"quality":1}}\')',
            1,
            "duplicate JSON field",
        ),
        ("import time; time.sleep(1)", 0.02, "timed out"),
    ),
)
@pytest.mark.asyncio
async def test_subprocess_scorer_fails_closed_on_process_and_json_errors(
    tmp_path: Path, script: str, timeout: float, error: str
) -> None:
    """Timeout, exit status and malformed output remain scorer failures."""

    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", script),
        cwd=Path.cwd(),
        timeout_seconds=timeout,
        trace_root=tmp_path / "run",
    )

    with pytest.raises(ScoringError, match=error):
        await scorer.score(case, view, candidate)

    candidate.verify()


@pytest.mark.parametrize(
    ("script", "timeout", "code", "metric"),
    (
        ('print("not-json")', 1, "output_invalid", None),
        ('print("NaN")', 1, "output_invalid", None),
        ("raise SystemExit(7)", 1, "process_failed", None),
        ("import time; time.sleep(1)", 0.02, "timeout", None),
        ('print(\'{"metrics":{"quality":1},"unknown":1}\')', 1, "output_invalid", None),
        ('print(\'{"metrics":{"quality":0},"metrics":{"quality":1}}\')', 1, "output_invalid", None),
        ('print(\'{"metrics":{"quality":true}}\')', 1, "metric_type", "quality"),
        ('print(\'{"metrics":{"quality":"x"}}\')', 1, "metric_type", "quality"),
        ('print(\'{"metrics":{"quality":1e999}}\')', 1, "metric_nonfinite", "quality"),
        ('print(\'{"metrics":{"quality":1,"extra":1e999}}\')', 1, "metric_nonfinite", "extra"),
        ('print(\'{"metrics":{"quality":1,"extra":"x"}}\')', 1, "metric_type", "extra"),
        (
            'import json; print(json.dumps({"metrics": {"quality": 10**400}}))',
            1,
            "metric_nonfinite",
            "quality",
        ),
        (
            'import json; print(json.dumps({"metrics": {"quality": 1, "extra": 10**400}}))',
            1,
            "metric_nonfinite",
            "extra",
        ),
    ),
)
@pytest.mark.asyncio
async def test_subprocess_scorer_failures_carry_a_code(
    tmp_path: Path, script: str, timeout: float, code: str, metric: str | None
) -> None:
    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", script),
        cwd=Path.cwd(),
        timeout_seconds=timeout,
        trace_root=tmp_path / "run",
    )
    with pytest.raises(ScoringError) as exc:
        await scorer.score(case, view, candidate)
    assert (exc.value.code, exc.value.metric) == (code, metric)


@pytest.mark.asyncio
async def test_subprocess_scorer_start_failure_is_process_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("cannot exec")

    monkeypatch.setattr("evolution.tree.execution.rollout_scoring.run_capture", refuse)
    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", "pass"),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )
    with pytest.raises(ScoringError, match="could not be started") as exc:
        await scorer.score(case, view, candidate)
    assert (exc.value.code, exc.value.metric) == ("process_failed", None)


@pytest.mark.asyncio
async def test_non_scoreable_terminal_error_stays_untyped(tmp_path: Path) -> None:
    case, view, candidate = _scoring_inputs(tmp_path, terminal="time_budget")
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", "pass"),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )
    with pytest.raises(ScoringError, match="not scoreable") as exc:
        await scorer.score(case, view, candidate)
    assert (exc.value.code, exc.value.metric) == (None, None)


@pytest.mark.asyncio
async def test_subprocess_scorer_wraps_critical_shape_violation(tmp_path: Path) -> None:
    """Invalid critical_pass is a scorer failure, not an uncategorized ValueError."""

    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(
            sys.executable,
            "-c",
            'print(\'{"metrics":{"quality":1},"critical_pass":false}\')',
        ),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )

    with pytest.raises(ScoringError, match="critical_pass") as exc:
        await scorer.score(case, view, candidate)
    assert (exc.value.code, exc.value.metric) == ("output_invalid", None)


@pytest.mark.asyncio
async def test_unexpected_worker_exception_is_process_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", "pass"),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("worker blew up")

    monkeypatch.setattr(SubprocessScorer, "_score_in_worker", broken)
    with pytest.raises(ScoringError, match="subprocess scorer failed") as exc:
        await scorer.score(case, view, candidate)
    assert (exc.value.code, exc.value.metric) == ("process_failed", None)


@pytest.mark.asyncio
async def test_subprocess_scorer_detects_candidate_mutation(tmp_path: Path) -> None:
    """A scorer that writes Candidate artifacts invalidates the read-only view."""

    case, view, candidate = _scoring_inputs(tmp_path)
    script = (
        "import json, os; from pathlib import Path; "
        "Path(os.environ['EVO_CANDIDATE_ROOT'], 'skill.py').write_text('CHANGED'); "
        "print(json.dumps({'metrics': {'quality': 1}}))"
    )
    scorer = SubprocessScorer(
        argv=(sys.executable, "-c", script),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )

    with pytest.raises(CandidateIntegrityError, match="Candidate bytes changed"):
        await scorer.score(case, view, candidate)


@pytest.mark.asyncio
async def test_subprocess_scorer_rejects_command_inside_candidate(tmp_path: Path) -> None:
    """Candidate-controlled bytes cannot also define the trusted scorer."""

    case, view, candidate = _scoring_inputs(tmp_path)
    command = candidate.root / "scorer.py"
    command.write_text('print(\'{"metrics":{"quality":1}}\')\n', encoding="utf-8")
    scorer = SubprocessScorer(
        argv=(sys.executable, str(command)),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )

    with pytest.raises(ScoringError, match="outside the Candidate"):
        await scorer.score(case, view, candidate)


@pytest.mark.asyncio
async def test_subprocess_scorer_identity_seals_command_bytes(tmp_path: Path) -> None:
    """Changing a scorer entrypoint after planning must invalidate its identity proof."""

    case, view, candidate = _scoring_inputs(tmp_path)
    command = tmp_path / "scorer.py"
    command.write_text('print(\'{"metrics":{"quality":1}}\')\n', encoding="utf-8")
    scorer = SubprocessScorer(
        argv=(sys.executable, str(command)),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )
    sealed_identity = scorer.identity
    command.write_text('print(\'{"metrics":{"quality":0}}\')\n', encoding="utf-8")

    with pytest.raises(ScoringError, match="proof changed before execution"):
        await scorer.score(case, view, candidate)

    assert scorer.identity == sealed_identity


def test_subprocess_scorer_identity_distinguishes_absent_and_empty_allowed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment presence is part of command semantics even when its value is empty."""

    monkeypatch.delenv("EVO_OPTIONAL_SCORER_SETTING", raising=False)
    absent = SubprocessScorer(
        argv=(sys.executable, "-c", "print('{}')"),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "absent",
        allowed_env=frozenset({"EVO_OPTIONAL_SCORER_SETTING"}),
    )
    monkeypatch.setenv("EVO_OPTIONAL_SCORER_SETTING", "")
    empty = SubprocessScorer(
        argv=(sys.executable, "-c", "print('{}')"),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "empty",
        allowed_env=frozenset({"EVO_OPTIONAL_SCORER_SETTING"}),
    )

    assert absent.identity != empty.identity


def test_subprocess_scorer_module_command_is_refused(
    tmp_path: Path,
) -> None:
    """A module name alone cannot attest the bytes that implement a scorer."""

    with pytest.raises(ValueError, match="module command is not supported"):
        SubprocessScorer(
            argv=(sys.executable, "-m", "unsealed_scorer"),
            cwd=Path.cwd(),
            timeout_seconds=1,
            trace_root=tmp_path / "run",
        )


_PINNED_SCORER_DIGEST = "01129edf30005dae7e5c0ec600bd5dcf31252d7cc2bb82e732bdcb943e4b18f4"


def test_subprocess_scorer_identity_digest_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journaled scorer identity is a frozen value, not an incidental one.

    Every input is fixed: a script whose bytes are literal, a fixed cwd layout,
    and one allowed environment name with a fixed value. ``argv[0]`` is the
    script itself rather than an interpreter, so no host binary's bytes reach
    the proof and the digest is the same on every machine.

    The pin was re-established on 2026-09-08 by amendment A13: the identity
    records the declared environment names and whether each is present, not a
    digest of their values (PDR-0003), which changed this value once, and
    deliberately, from the review base's
    ``043da8bd2da5165b57a7171d825d08d36023f01e155a8b91577f307cf3ba23cb``. The
    ``W1_PIN`` value below therefore no longer reaches the digest -- it is
    sealed in the execution seal, which is never journaled -- and the pin must
    not move again.
    """

    cwd = tmp_path / "scorer"
    cwd.mkdir()
    (cwd / "score.py").write_bytes(b"print('{}')\n")
    monkeypatch.setenv("W1_PIN", "value")

    scorer = SubprocessScorer(
        argv=("./score.py",),
        cwd=cwd,
        timeout_seconds=5.0,
        trace_root=tmp_path / "traces",
        allowed_env=frozenset({"W1_PIN"}),
    )

    proof = scorer._command_proof()

    # The path label is relative to cwd, so the digest is layout-independent.
    assert [entry["path"] for entry in proof["dependencies"]] == ["score.py"]
    assert scorer.identity.digest == _PINNED_SCORER_DIGEST


def test_subprocess_scorer_accepts_a_matching_supplied_binding(tmp_path: Path) -> None:
    """A caller may bind the command itself, as a seed-checked manifest binding does."""

    cwd = tmp_path / "scorer"
    cwd.mkdir()
    (cwd / "score.py").write_bytes(b"print('{}')\n")
    seed = tmp_path / "seed"
    seed.mkdir()
    binding = CommandBinding(argv=("./score.py",), cwd=cwd, seed_root=seed)

    scorer = SubprocessScorer(
        argv=("./score.py",),
        cwd=cwd,
        timeout_seconds=5.0,
        trace_root=tmp_path / "traces",
        binding=binding,
    )

    assert scorer.binding is binding
    assert scorer.argv == ("./score.py",)
    assert (
        scorer.identity
        == SubprocessScorer(
            argv=("./score.py",),
            cwd=cwd,
            timeout_seconds=5.0,
            trace_root=tmp_path / "traces",
        ).identity
    )


@pytest.mark.parametrize("mismatch", ("argv", "cwd", "dependency_paths", "allowed_env"))
def test_subprocess_scorer_refuses_a_binding_that_disagrees_with_its_declaration(
    tmp_path: Path, mismatch: str
) -> None:
    """A declaration and the binding meant to bind it can never describe two commands."""

    cwd = tmp_path / "scorer"
    cwd.mkdir()
    (cwd / "score.py").write_bytes(b"print('{}')\n")
    (cwd / "other.py").write_bytes(b"print('{}')\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "score.py").write_bytes(b"print('{}')\n")
    dependency = tmp_path / "dep.txt"
    dependency.write_text("x", encoding="utf-8")
    declaration: dict[str, Any] = {
        "argv": ("./score.py",),
        "cwd": cwd,
        "dependency_paths": (dependency,),
        "allowed_env": frozenset({"EVO_SCORER_SETTING"}),
    }
    binding = CommandBinding(**declaration)
    declaration[mismatch] = {
        "argv": ("./other.py",),
        "cwd": elsewhere,
        "dependency_paths": (),
        "allowed_env": frozenset(),
    }[mismatch]

    with pytest.raises(ValueError, match="must match the supplied binding"):
        SubprocessScorer(
            **declaration,
            timeout_seconds=5.0,
            trace_root=tmp_path / "traces",
            binding=binding,
        )


@pytest.mark.asyncio
async def test_subprocess_scorer_cancellation_waits_for_bounded_child_cleanup(
    tmp_path: Path,
) -> None:
    """Cancellation must not leave a scorer process racing Candidate verification."""

    case, view, candidate = _scoring_inputs(tmp_path)
    scorer = SubprocessScorer(
        argv=(
            sys.executable,
            "-c",
            'import time; time.sleep(0.1); print(\'{"metrics":{"quality":1}}\')',
        ),
        cwd=Path.cwd(),
        timeout_seconds=1,
        trace_root=tmp_path / "run",
    )
    task = asyncio.create_task(scorer.score(case, view, candidate))
    await asyncio.sleep(0.01)
    cancelled_at = asyncio.get_running_loop().time()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert asyncio.get_running_loop().time() - cancelled_at >= 0.05
    candidate.verify()


@pytest.mark.asyncio
async def test_legacy_journal_operation_ids_correlate_every_boundary():
    journal = _RecordingJournal()
    instance = _InMemoryInstance()
    await rollout(
        EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
        RolloutSample("sample", "candidate", 42),
        instance,
        StaticInputEnvironment(),
        RolloutBudget(2, 1.0),
        journal,
    )
    starts = {}
    terminals = set()
    for record in journal.records:
        operation_id = record["operation_id"]
        assert isinstance(operation_id, str) and operation_id
        family, _, suffix = record["event"].rpartition("_")
        if suffix == "started":
            assert operation_id not in starts
            starts[operation_id] = family
        else:
            assert suffix in ("finished", "failed")
            assert starts[operation_id] == family
            assert operation_id not in terminals
            terminals.add(operation_id)
    assert starts.keys() == terminals
    assert journal.records[0]["operation_id"] == journal.records[-1]["operation_id"]


@pytest.mark.asyncio
async def test_legacy_initial_append_latency_spends_active_deadline(monkeypatch):
    loop = asyncio.get_running_loop()
    real_clock = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_clock() + offset)

    class SlowJournal(_RecordingJournal):
        def append(self, event):
            nonlocal offset
            result = super().append(event)
            if event["event"] == "rollout_started":
                offset += 2.0
            return result

    class Instance(_InMemoryInstance):
        async def identity(self):
            pytest.fail("identity dispatched after initial append exhausted deadline")

    journal = SlowJournal()
    instance = Instance()
    with pytest.raises(RolloutTimeBudgetExceeded):
        await rollout(
            EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {}),
            RolloutSample("sample", "candidate", 42),
            instance,
            StaticInputEnvironment(),
            RolloutBudget(2, 1.0),
            journal,
        )
    assert instance.opened_public is None
    assert [record["event"] for record in journal.records] == [
        "rollout_started",
        "target_identity_started",
        "target_identity_failed",
    ]
    assert journal.records[-1]["error_class"] == "deadline_timeout"


def test_turn_admits_candidate_failed():
    turn = Turn(0, Message("x"), ({"type": "assistant_message"},), None, 1, "candidate_failed")
    assert turn.outcome == "candidate_failed"
    assert turn.to_payload()["outcome"] == "candidate_failed"


def test_turn_result_admits_candidate_failed():
    assert TurnResult(({"type": "assistant_message"},), None, 1, "candidate_failed").outcome == (
        "candidate_failed"
    )


def test_turn_refuses_an_unknown_outcome():
    with pytest.raises(ValueError):
        Turn(0, Message("x"), ({"type": "assistant_message"},), None, 1, "nope")


def test_turn_payload_carries_the_reason():
    turn = Turn(
        0,
        Message("x"),
        ({"type": "assistant_message"},),
        None,
        1,
        "candidate_failed",
        {"category": "format", "code": "no_json"},
    )
    assert turn.to_payload()["reason"] == {"category": "format", "code": "no_json"}


def test_turn_payload_carries_a_null_reason_when_absent():
    turn = Turn(0, Message("x"), ({"type": "assistant_message"},), None, 1, "ok")
    assert turn.to_payload()["reason"] is None


async def _drive_scripted_rollout(
    outcomes: list[str],
    max_turns: int,
    max_failed_turns: int,
) -> tuple[Trajectory, _RecordingJournal]:
    """Drive `rollout` against a scripted target and a `ScriptedEnvironment`.

    `outcomes` scripts successive `send` calls; the `ScriptedEnvironment`'s
    script is padded past both `max_turns` and `len(outcomes)` so the
    Environment always has one more turn to offer, and the loop's own
    `max_turns`/tolerance checks are what end the rollout — never the
    Environment running dry first.
    """

    script_len = max(len(outcomes), max_turns) + 1
    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {},
        {"script": [{"content": f"turn-{i}"} for i in range(script_len)]},
        {},
    )
    instance = _InMemoryInstance(outcomes=outcomes)
    journal = _RecordingJournal()
    trajectory = await rollout(
        case,
        RolloutSample("01", "candidate", 7),
        instance,
        ScriptedEnvironment(),
        RolloutBudget(max_turns=max_turns, timeout_seconds=1.0, max_failed_turns=max_failed_turns),
        journal,
    )
    return trajectory, journal


@pytest.fixture
def rollout_harness():
    async def run(outcomes: list[str], max_turns: int, max_failed_turns: int = 1) -> Trajectory:
        trajectory, _ = await _drive_scripted_rollout(outcomes, max_turns, max_failed_turns)
        return trajectory

    return run


@pytest.fixture
def rollout_harness_journal():
    """Same as `rollout_harness`, but returns the journal's records."""

    async def run(
        outcomes: list[str], max_turns: int, max_failed_turns: int = 1
    ) -> list[dict[str, Any]]:
        _, journal = await _drive_scripted_rollout(outcomes, max_turns, max_failed_turns)
        return journal.records

    return run


def test_budget_defaults_to_terminating_on_the_first_failure():
    assert RolloutBudget(max_turns=4, timeout_seconds=1.0).max_failed_turns == 1


def test_budget_refuses_a_nonpositive_tolerance():
    with pytest.raises(ValueError):
        RolloutBudget(max_turns=4, timeout_seconds=1.0, max_failed_turns=0)


def test_budget_refuses_a_bool_tolerance():
    with pytest.raises(ValueError):
        RolloutBudget(max_turns=4, timeout_seconds=1.0, max_failed_turns=True)


@pytest.mark.asyncio
async def test_one_failure_ends_the_rollout_at_tolerance_one(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed"], max_turns=8, max_failed_turns=1
    )
    assert trajectory.terminal == "candidate_failed"
    assert len(trajectory.history.turns) == 1


@pytest.mark.asyncio
async def test_a_failure_below_tolerance_does_not_end_the_rollout(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed", "ok", "ok"], max_turns=3, max_failed_turns=2
    )
    # The scripted Environment never stops on its own (the harness pads the
    # script), so `max_turns=3` is what binds: the failure was tolerated and
    # the rollout ran to its turn budget.
    assert trajectory.terminal == "turn_budget"
    assert [t.outcome for t in trajectory.history.turns] == ["candidate_failed", "ok", "ok"]


@pytest.mark.asyncio
async def test_an_ok_turn_resets_the_consecutive_counter(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed", "ok", "candidate_failed", "ok"],
        max_turns=8,
        max_failed_turns=2,
    )
    assert trajectory.terminal != "candidate_failed"


@pytest.mark.asyncio
async def test_consecutive_failures_reaching_the_tolerance_end_the_rollout(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed", "candidate_failed", "ok"],
        max_turns=8,
        max_failed_turns=2,
    )
    assert trajectory.terminal == "candidate_failed"
    assert len(trajectory.history.turns) == 2


@pytest.mark.asyncio
async def test_a_failed_turn_consumes_the_turn_budget(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed", "candidate_failed"], max_turns=2, max_failed_turns=8
    )
    assert trajectory.terminal == "turn_budget"


@pytest.mark.asyncio
async def test_a_tolerance_above_max_turns_never_binds(rollout_harness):
    trajectory = await rollout_harness(
        outcomes=["candidate_failed"] * 3, max_turns=2, max_failed_turns=99
    )
    assert trajectory.terminal == "turn_budget"


@pytest.mark.asyncio
async def test_a_tolerated_failure_leaves_a_summary_and_no_new_record(rollout_harness_journal):
    # Spec section 3.4 and 7.3, asserted rather than implied: a below-tolerance
    # failure on a sample that ends indeterminate leaves the per-send summary
    # carrying `candidate_failed`, and NO new journal record family. A future
    # reader must not mistake the absence for an oversight.
    records = await rollout_harness_journal(
        outcomes=["candidate_failed", "time_budget"], max_turns=8, max_failed_turns=2
    )
    events = {r["event"] for r in records}
    assert "turn_failed" not in events
    sends = [r for r in records if r["event"] == "target_send_finished" and "outcome" in r]
    assert any(r["outcome"] == "candidate_failed" for r in sends)


# --- the call-retry loop and its evidence -------------------------


class _FlakyInstance(_InMemoryInstance):
    """Fails the first `failures` calls of one boundary, then behaves.

    `boundary` names the failing method; `code`/`retryable` shape the typed
    failure. Every `send` records its `request_id` per turn, so a resend is
    visible as a repeat of the same id.
    """

    def __init__(
        self,
        failures: int,
        *,
        boundary: str = "send",
        code: str = "internal",
        retryable: bool = True,
        error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.failures = failures
        self.boundary = boundary
        self.code = code
        self.retryable = retryable
        self.error = error
        self.calls = 0
        self.request_ids_per_turn: dict[int, list[str]] = {}
        self._turn = 0

    def _maybe_fail(self, boundary: str) -> None:
        if boundary != self.boundary:
            return
        self.calls += 1
        if self.calls <= self.failures:
            if self.error is not None:
                raise self.error
            raise RolloutInfrastructureError("x", self.code, retryable=self.retryable)

    async def open(self, case_public, seed: int | None, request_id: str) -> _Session:
        self._maybe_fail("open")
        return await super().open(case_public, seed, request_id)

    async def send(self, session: _Session, message: Message, request_id: str) -> TurnResult:
        ids = self.request_ids_per_turn.setdefault(self._turn, [])
        ids.append(request_id)
        self._maybe_fail("send")
        self._turn += 1
        return await super().send(session, message, request_id)

    async def close(self, session: _Session) -> None:
        await super().close(session)
        self._maybe_fail("close")


def _retry_case() -> EvalCase:
    return EvalCase("case", "group", "stratum", (), {"text": "hello"}, {}, {})


async def _no_sleep(_delay: float) -> None:
    return None


def _recording_sleep(slept: list[float]):
    async def sleep(delay: float) -> None:
        slept.append(delay)
        await asyncio.sleep(delay)

    return sleep


async def _retry_run(
    monkeypatch: pytest.MonkeyPatch,
    instance: _InMemoryInstance,
    budget: RolloutBudget,
    journal: _RecordingJournal,
    *,
    sleep=_no_sleep,
    jitter: float = 1.0,
) -> Trajectory:
    monkeypatch.setattr(rollout_core, "_SLEEP", sleep)
    monkeypatch.setattr(rollout_core, "_JITTER", lambda: jitter)
    return await rollout(
        _retry_case(),
        RolloutSample("01", "candidate", 7),
        instance,
        StaticInputEnvironment(),
        budget,
        journal,
    )


@pytest.mark.asyncio
async def test_a_transient_send_is_resent_with_the_same_request_id(monkeypatch) -> None:
    instance = _FlakyInstance(2)
    journal = _RecordingJournal()
    budget = RolloutBudget(3, 30, call_retries=CallRetries(4, 3))

    trajectory = await _retry_run(monkeypatch, instance, budget, journal)

    assert trajectory.terminal == "completed"
    ids = instance.request_ids_per_turn[0]
    assert len(ids) == 3 and set(ids) == {ids[0]}
    failed = [e for e in journal.records if e["event"] == "target_send_failed"]
    assert [e["call_attempt"] for e in failed] == [1, 2]
    assert {e["error_code"] for e in failed} == {"internal"}
    started = [e for e in journal.records if e["event"] == "target_send_started"]
    finished = [e for e in journal.records if e["event"] == "target_send_finished"]
    assert [e["call_attempt"] for e in started] == [1, 2, 3]
    assert [e["call_attempt"] for e in finished] == [3]
    assert "error_code" not in finished[0]
    # Each attempt is its own operation.
    assert len({e["operation_id"] for e in started}) == 3


@pytest.mark.asyncio
async def test_turn_max_attempts_one_never_resends(monkeypatch) -> None:
    instance = _FlakyInstance(1)
    budget = RolloutBudget(3, 30, call_retries=CallRetries(4, 1))
    with pytest.raises(RolloutTargetUnstable):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())
    assert len(instance.request_ids_per_turn[0]) == 1


@pytest.mark.parametrize(
    ("code", "retryable", "expected"),
    [
        # A contract code is never transient, whatever the wire says.
        ("invalid_request", True, RolloutInfrastructureError),
        # A transient code the wire withholds a retry for is still transient
        # (the wire flag only withholds a retry) -- exhausted on
        # its one attempt, so it closes `target_failed` like any other.
        ("internal", False, RolloutTargetUnstable),
        # A code nobody here knows is transient by default (call_failures'
        # own rule) and is never resent either way.
        ("admission_closed", True, RolloutTargetUnstable),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_the_classification_does_not_retry_is_never_resent(
    monkeypatch, code: str, retryable: bool, expected: type[BaseException]
) -> None:
    instance = _FlakyInstance(1, code=code, retryable=retryable)
    budget = RolloutBudget(3, 30, call_retries=CallRetries(4, 4))
    with pytest.raises(expected):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())
    assert instance.calls == 1


@pytest.mark.asyncio
async def test_an_untyped_failure_is_not_resent_and_is_coded_by_class(monkeypatch) -> None:
    instance = _FlakyInstance(1, error=ValueError("custom adapter"))
    journal = _RecordingJournal()
    budget = RolloutBudget(3, 30, call_retries=CallRetries(4, 4))
    # An exception that is not a `RolloutInfrastructureError` at all is
    # transient by default (call_failures.py), so it too closes `target_failed`
    # once exhausted -- the sample retry budget, not an infrastructure fault.
    with pytest.raises(RolloutTargetUnstable):
        await _retry_run(monkeypatch, instance, budget, journal)
    assert instance.calls == 1
    failed = [e for e in journal.records if e["event"] == "target_send_failed"]
    assert [(e["call_attempt"], e["error_code"]) for e in failed] == [(1, "ValueError")]


@pytest.mark.asyncio
async def test_a_cancelled_call_is_never_resent(monkeypatch) -> None:
    instance = _FlakyInstance(1, error=asyncio.CancelledError())
    budget = RolloutBudget(3, 30, call_retries=CallRetries(4, 4))
    with pytest.raises(asyncio.CancelledError):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())
    assert instance.calls == 1


@pytest.mark.asyncio
async def test_a_cancelled_campaign_starts_no_further_call_attempt(monkeypatch) -> None:
    """A cancelled campaign starts no further retry. `instance`
    would recover on its 4th `send`, and `turn_max_attempts` admits that many,
    but `cancelled()` turns true right after the first failure -- so no
    resend is started, and the last failure (a transient one) propagates."""

    instance = _FlakyInstance(3)

    budget = RolloutBudget(
        3,
        30,
        call_retries=CallRetries(1, 4),
        cancelled=lambda: instance.calls >= 1,
    )
    with pytest.raises(RolloutTargetUnstable):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())
    assert instance.calls == 1


@pytest.mark.asyncio
async def test_non_send_calls_use_max_attempts(monkeypatch) -> None:
    instance = _FlakyInstance(2, boundary="open")
    journal = _RecordingJournal()
    budget = RolloutBudget(3, 30, call_retries=CallRetries(3, 1))

    trajectory = await _retry_run(monkeypatch, instance, budget, journal)

    assert trajectory.terminal == "completed"
    assert instance.calls == 3
    # The same deterministic open request_id is resent.
    assert len(set(instance.open_request_ids)) == 1
    failed = [e for e in journal.records if e["event"] == "target_open_failed"]
    assert [e["call_attempt"] for e in failed] == [1, 2]
    # environment_next is never retried, and so carries no attempt number.
    assert all(
        "call_attempt" not in e for e in journal.records if e["event"].startswith("environment_")
    )


@pytest.mark.asyncio
async def test_non_send_calls_stop_after_max_attempts(monkeypatch) -> None:
    instance = _FlakyInstance(3, boundary="open")
    budget = RolloutBudget(3, 30, call_retries=CallRetries(3, 10))
    # Default `_FlakyInstance` code "internal" is transient, so exhausting
    # `max_attempts` closes `target_failed`, not an infrastructure fault.
    with pytest.raises(RolloutTargetUnstable):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())
    assert instance.calls == 3


@pytest.mark.asyncio
async def test_the_back_off_sleeps_between_attempts(monkeypatch) -> None:
    slept: list[float] = []

    async def record(delay: float) -> None:
        slept.append(delay)

    instance = _FlakyInstance(3)
    budget = RolloutBudget(3, 30, call_retries=CallRetries(1, 4))
    await _retry_run(monkeypatch, instance, budget, _RecordingJournal(), sleep=record, jitter=0.5)
    assert slept == [0.25, 0.5, 1.0]


@pytest.mark.asyncio
async def test_no_retry_is_started_past_the_deadline(monkeypatch) -> None:
    slept: list[float] = []
    budget = RolloutBudget(3, 0.2, call_retries=CallRetries(10, 10))
    with pytest.raises((RolloutTimeBudgetExceeded, RolloutTargetUnstable)):
        await _retry_run(
            monkeypatch,
            _FlakyInstance(100),
            budget,
            _RecordingJournal(),
            sleep=_recording_sleep(slept),
            jitter=0.1,
        )
    assert slept  # the loop did back off before giving up
    assert sum(slept) <= 0.2


def test_backoff_is_full_jitter_and_capped() -> None:
    assert rollout_core.backoff_delay(1, 1.0) == 0.5
    assert rollout_core.backoff_delay(3, 0.5) == 1.0
    assert rollout_core.backoff_delay(20, 1.0) == 30.0
    assert rollout_core.backoff_delay(4, 0.0) == 0.0


@pytest.mark.asyncio
async def test_an_exhausted_transient_close_ends_the_sample_target_failed(monkeypatch) -> None:
    """The scorer-free part of close is one of the five calls an
    exhausted transient failure closes `target_failed` on. Before the final
    review's fix `_close_session` wrapped every failure in a default-coded
    `RolloutInfrastructureError`, so a network blink on close became a fault
    row and suspended the campaign as `target_contract_violated`."""

    instance = _FlakyInstance(3, boundary="close")
    journal = _RecordingJournal()
    budget = RolloutBudget(3, 30, call_retries=CallRetries(3, 3))

    with pytest.raises(RolloutTargetUnstable, match="target close failed") as failure:
        await _retry_run(monkeypatch, instance, budget, journal)

    assert failure.value.code == "internal"
    assert instance.calls == 3
    failed = [e for e in journal.records if e["event"] == "target_close_failed"]
    assert [(e["call_attempt"], e["error_code"]) for e in failed] == [
        (1, "internal"),
        (2, "internal"),
        (3, "internal"),
    ]


@pytest.mark.asyncio
async def test_a_contract_close_failure_stays_a_fault_under_call_retries(monkeypatch) -> None:
    """Only the transient class moves: a contract code still ends the sample
    `infrastructure_failed`, and is never resent."""

    instance = _FlakyInstance(1, boundary="close", code="invalid_response")
    budget = RolloutBudget(3, 30, call_retries=CallRetries(3, 3))

    with pytest.raises(RolloutInfrastructureError, match="target close failed"):
        await _retry_run(monkeypatch, instance, budget, _RecordingJournal())

    assert instance.calls == 1


@pytest.mark.asyncio
async def test_a_transient_close_failure_without_call_retries_stays_infrastructure(
    monkeypatch,
) -> None:
    """`/5`-`/7` is untouched: the same failure is the plain wrap it has always been."""

    instance = _FlakyInstance(1, boundary="close")

    with pytest.raises(RolloutInfrastructureError, match="target close failed"):
        await _retry_run(monkeypatch, instance, RolloutBudget(3, 30), _RecordingJournal())

    assert instance.calls == 1


@pytest.mark.asyncio
async def test_a_close_failure_never_masks_a_primary_failure_under_call_retries() -> None:
    """The cleanup path is unchanged whatever the manifest says: the close
    failure keeps carrying the primary one, and is never reclassified into
    target instability that would hide what really ended the sample."""

    class BrokenEnvironment:
        identity = EnvironmentIdentity("broken", "broken-v1")

        async def next(self, case, history):
            raise RuntimeError("environment detail")

    instance = _FlakyInstance(1, boundary="close")

    with pytest.raises(RolloutInfrastructureError, match="target close failed") as caught:
        await rollout(
            EvalCase("case", "group", "stratum", (), {}, {}, {}),
            RolloutSample("01", "candidate", 7),
            instance,
            BrokenEnvironment(),
            RolloutBudget(3, 30, call_retries=CallRetries(1, 1)),
            _RecordingJournal(),
        )

    assert isinstance(caught.value.primary_failure, RolloutEnvironmentError)


@pytest.mark.asyncio
async def test_without_call_retries_nothing_is_retried_and_no_key_is_written(monkeypatch) -> None:
    instance = _FlakyInstance(1)
    journal = _RecordingJournal()
    with pytest.raises(RolloutInfrastructureError):
        await _retry_run(monkeypatch, instance, RolloutBudget(3, 30), journal)
    assert instance.calls == 1
    assert all("call_attempt" not in e and "error_code" not in e for e in journal.records)


class _FlakyAdapter(_InMemoryAdapter):
    """Fails materialize/release `failures` times with a retryable code."""

    def __init__(self, journal: _RecordingJournal, failures: int, *, code: str = "internal"):
        super().__init__(journal=journal)
        self.failures = failures
        self.code = code
        self.materialize_request_ids: list[str | None] = []
        self.release_calls = 0

    async def materialize(  # type: ignore[override]
        self,
        candidate: CandidateView,
        strategy: str,
        instance_id: str,
        *,
        request_id: str | None = None,
    ) -> _InMemoryInstance:
        self.materialize_request_ids.append(request_id)
        if len(self.materialize_request_ids) <= self.failures:
            raise RolloutInfrastructureError("x", self.code, retryable=True)
        return await super().materialize(candidate, strategy, instance_id)

    async def release(self, instance_id: str) -> None:
        self.release_calls += 1
        if self.release_calls <= self.failures:
            raise RolloutInfrastructureError("x", self.code, retryable=True)
        await super().release(instance_id)


def _retry_candidate(tmp_path: Path) -> CandidateView:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    return CandidateView(root, artifact_digest(root, ("skill.py",)), ("skill.py",))


@pytest.mark.asyncio
async def test_materialize_and_release_are_retried_with_one_request_id(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rollout_core, "_SLEEP", _no_sleep)
    candidate = _retry_candidate(tmp_path)
    journal = _RecordingJournal()
    adapter = _FlakyAdapter(journal, 2)

    instance = await rollout_core.materialize_target(
        adapter, candidate, "artifact_root_injection", "instance-r", journal, attempts=3
    )
    await rollout_core.release_target(
        adapter, "instance-r", candidate.digest, "artifact_root_injection", journal, attempts=3
    )

    assert instance.instance_id == "instance-r"
    ids = adapter.materialize_request_ids
    assert len(ids) == 3 and ids[0] is not None and set(ids) == {ids[0]}
    assert adapter.release_calls == 3
    for operation in ("target_materialize", "target_release"):
        events = [e for e in journal.records if e["event"].startswith(operation)]
        assert [(e["event"].rsplit("_", 1)[1], e["call_attempt"]) for e in events] == [
            ("started", 1),
            ("failed", 1),
            ("started", 2),
            ("failed", 2),
            ("started", 3),
            ("finished", 3),
        ]
        assert {e["error_code"] for e in events if e["event"].endswith("_failed")} == {"internal"}
    assert rollout_core._instances_requiring_release(tuple(journal.records)) == ()


@pytest.mark.asyncio
async def test_materialize_without_attempts_is_tried_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rollout_core, "_SLEEP", _no_sleep)
    candidate = _retry_candidate(tmp_path)
    journal = _RecordingJournal()
    adapter = _FlakyAdapter(journal, 1)

    with pytest.raises(RolloutInfrastructureError):
        await rollout_core.materialize_target(
            adapter, candidate, "artifact_root_injection", "instance-once", journal
        )
    assert len(adapter.materialize_request_ids) == 1
    assert all("call_attempt" not in e for e in journal.records)


@pytest.mark.asyncio
async def test_an_adapter_without_the_request_id_keyword_is_called_as_before(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rollout_core, "_SLEEP", _no_sleep)
    candidate = _retry_candidate(tmp_path)
    journal = _RecordingJournal()
    adapter = _InMemoryAdapter(journal=journal)

    instance = await rollout_core.materialize_target(
        adapter, candidate, "artifact_root_injection", "instance-plain", journal, attempts=3
    )
    assert instance.instance_id == "instance-plain"


def test_turn_result_carries_a_validated_copy_of_provenance():
    used = ["skills/SKILL.md"]
    result = TurnResult(
        events=({"type": "assistant_message", "content": "a"},),
        usage=None,
        latency_ms=0,
        outcome="ok",
        provenance={"used": used},
    )
    used.append("mutated")
    assert result.provenance == {"used": ["skills/SKILL.md"]}


def test_turn_result_refuses_malformed_provenance():
    with pytest.raises(ValueError):
        TurnResult(
            events=({"type": "x"},),
            usage=None,
            latency_ms=0,
            outcome="ok",
            provenance={"used": ["a b\\c"]},
        )


def test_turn_payload_has_no_provenance_key():
    # Families 5-10 persist `Turn.to_payload()`; G1 must not change it.
    turn = Turn(0, Message("m"), ({"type": "x"},), None, 0, "ok")
    assert "provenance" not in turn.to_payload()


# -- the sample's provenance union -----------------------


class _ProvenanceInstance(_InMemoryInstance):
    """Reports `per_turn[i]` as turn `i`'s `provenance.used` (`None`: none)."""

    def __init__(self, per_turn: list[list[str] | None]) -> None:
        super().__init__()
        self._per_turn = per_turn

    async def send(self, session: _Session, message: Message, request_id: str) -> TurnResult:
        used = self._per_turn[len(self.sent)]
        self.sent.append((request_id, message))
        return TurnResult(
            events=({"type": "message", "text": message.content},),
            usage=None,
            latency_ms=0,
            outcome="ok",
            provenance=None if used is None else {"used": used},
        )


class _CollectingJournal(_RecordingJournal):
    """A journal that asks for the sample's provenance, as a family-12
    `JournalRolloutAdapter` does (`collects_provenance`)."""

    collects_provenance = True


async def run_scripted(
    turns: list[list[str] | None], journal: _RecordingJournal | None = None
) -> list[dict[str, Any]]:
    case = EvalCase(
        "case",
        "group",
        "stratum",
        (),
        {},
        {"script": [{"content": f"m{i}"} for i in range(len(turns))]},
        {},
    )
    journal = _CollectingJournal() if journal is None else journal
    await rollout(
        case,
        RolloutSample("01", "candidate", 7),
        _ProvenanceInstance(turns),
        ScriptedEnvironment(),
        RolloutBudget(max_turns=max(1, len(turns)), timeout_seconds=1),
        journal,
    )
    return journal.records


@pytest.mark.parametrize(
    ("per_turn", "expected"),
    [
        ([["b", "a"], ["a", "c"]], ["a", "b", "c"]),
        ([["a"], None], None),
        ([None, ["a"]], None),
        ([], []),
    ],
)
async def test_rollout_finished_carries_the_sorted_union(per_turn, expected) -> None:
    events = await run_scripted(turns=per_turn)
    finished = next(e for e in events if e["event"] == "rollout_finished")
    assert finished["used"] == expected
    assert finished["turns"] == len(per_turn)


# -- Collection is opt-in and bounded while the rollout runs ------------------


async def test_an_unmarked_journal_gets_no_provenance_and_nothing_accumulates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Families 5-11 and standalone evaluation: the event is the base branch's.
    class _NoUnion:
        def __init__(self) -> None:
            raise AssertionError("an unmarked rollout must not accumulate provenance")

    monkeypatch.setattr(rollout_core, "_ProvenanceUnion", _NoUnion, raising=False)
    events = await run_scripted([["a"], ["b"]], journal=_RecordingJournal())
    finished = next(e for e in events if e["event"] == "rollout_finished")
    assert set(finished) == {
        "event",
        "operation_id",
        "case_id",
        "sample_id",
        "candidate_digest",
        "instance_id",
        "terminal",
        "turns",
        "duration_seconds",
    }


def _distinct(turn: int) -> list[str]:
    return [f"t{turn:03d}/{i:04d}.md" for i in range(1024)]


async def test_many_full_turns_switch_to_overflow_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each turn reports 1024 distinct identifiers.
    peaks: list[int] = []

    class _Spy(rollout_core._ProvenanceUnion):
        def add(self, used: list[str] | None) -> None:
            super().add(used)
            peaks.append(0 if self.ids is None else len(self.ids))

    monkeypatch.setattr(rollout_core, "_ProvenanceUnion", _Spy)
    events = await run_scripted([_distinct(t) for t in range(40)])
    finished = next(e for e in events if e["event"] == "rollout_finished")
    assert finished["turns"] == 40
    assert finished["used"] is None and finished["used_overflow"] is True
    assert peaks[0] == 1024
    assert all(peak == 0 for peak in peaks[1:])


def _union(*turns: list[str] | None) -> Any:
    union = rollout_core._ProvenanceUnion()
    for used in turns:
        union.add(used)
    return union


def test_the_union_records_1024_and_overflows_at_1025() -> None:
    many = [f"p/{i:04d}.md" for i in range(1025)]
    assert _union(many[:1024], many[:3]).event_fields() == {"used": sorted(many[:1024])}
    over = _union(many[:1024], many[1024:])
    assert over.ids is None
    assert over.event_fields() == {"used": None, "used_overflow": True}


#: As in the measurement tests: exactly 256 KiB of canonical JSON, and one over.
AT_BUDGET = sorted(f"p/{i:04d}" + "x" * (247 if i else 246) for i in range(1024))
OVER_BUDGET = sorted(f"p/{i:04d}" + "x" * 247 for i in range(1024))


def test_the_byte_budget_switches_to_overflow_on_the_turn_that_crosses_it() -> None:
    assert len(json.dumps(AT_BUDGET, separators=(",", ":"))) == 256 * 1024
    assert _union(AT_BUDGET[:512], AT_BUDGET[512:]).event_fields() == {"used": AT_BUDGET}
    crossing = _union(OVER_BUDGET[:512])
    assert crossing.ids is not None
    crossing.add(OVER_BUDGET[512:])
    assert crossing.ids is None
    assert crossing.event_fields() == {"used": None, "used_overflow": True}
    # Non-ASCII identifiers count in UTF-8 bytes, not characters.
    wide = ["\U0001f600" * 127 + "/" + "\U0001f600" * 127 + chr(0x20000 + i) for i in range(1024)]
    assert _union(wide).event_fields() == {"used": None, "used_overflow": True}


def test_a_turn_without_provenance_makes_the_union_unknown_even_after_overflow() -> None:
    many = [f"p/{i:04d}.md" for i in range(1025)]
    assert _union(many[:1024], many[1024:], None).event_fields() == {"used": None}
    assert _union(None, many).event_fields() == {"used": None}
    assert _union(None, ["a"]).ids is None


@pytest.mark.asyncio
async def test_a_stop_that_lands_as_the_call_completes_is_not_swallowed() -> None:
    """#164: a cancellation delivered in the same loop step as the reply wins.

    On Python 3.11 `asyncio.wait_for` returned the finished inner result and
    dropped the outer cancellation, so a stop mid-row was journaled as a
    completed call. The boundary call must re-raise `CancelledError` instead.
    It discriminates only on Python 3.11 (3.12+ builds `wait_for` on `timeout`),
    which is why CI keeps a 3.11 leg.
    """

    loop = asyncio.get_running_loop()
    reply: asyncio.Future[str] = loop.create_future()
    journal = _RecordingJournal()
    call = asyncio.create_task(
        rollout_core._external_call(lambda: reply, loop.time() + 30, journal, "instance_send", {})
    )
    while not journal.records:
        await asyncio.sleep(0)
    reply.set_result("answer")
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert [record["event"] for record in journal.records] == [
        "instance_send_started",
        "instance_send_failed",
    ]
    assert journal.records[-1]["error_class"] == "CancelledError"


def test_no_product_code_bounds_a_call_with_asyncio_wait_for() -> None:
    """#164: `asyncio.timeout` is the only deadline construct under `src/`.

    `asyncio.wait_for` swallows an outer cancellation on Python 3.11 when the
    inner awaitable has already finished; this guard keeps it from returning.
    """

    root = Path(__file__).resolve().parents[3] / "src" / "evolution"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            found = (
                isinstance(node, ast.Attribute)
                and node.attr == "wait_for"
                and isinstance(node.value, ast.Name)
                and node.value.id == "asyncio"
            ) or (
                isinstance(node, ast.ImportFrom)
                and node.module == "asyncio"
                and any(alias.name == "wait_for" for alias in node.names)
            )
            if found:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
