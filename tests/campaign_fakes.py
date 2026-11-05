"""Shared C1 test doubles: adapter, environment, scorer, corpus, policy, artifacts.

This is an ordinary test *module* (imported as `tests.campaign_fakes`), not a
`conftest` fixture file, so the campaign tests of Tasks 5-7 build the same
target, Environment and scorer without copying them.

`FakeAdapter` is promotion-capable (R3 `preflight`) and attests the S2 digests
of whatever Candidate it materializes, so `attestation.verify_attestation`
passes for a well-formed run and the whole R1 execution path is exercised.

**How a scripted disposition finds its sample.** `dispositions` is keyed by
`(candidate_digest, case_id, sample_id)`, but the R1 target boundary never
tells an instance which sample it is serving: `open` receives `case.public`
and the requested seed, and nothing else. The fakes therefore recover both
halves from what the boundary does pass:

* `case_id` travels in `case.public` (`corpus()` puts it there);
* `sample_id` is recovered from the seed. Under `exact` the frozen schedule is
  `requested_seed = random_seed + repeat` and `sample_id = f"{repeat:02d}"`
  (spec section 4), so `repeat = seed - RANDOM_SEED` with `RANDOM_SEED` the
  constant `policy()` freezes. Under `unpaired`/`none` there is no seed at all,
  so a scripted entry matches every sample of its case and the key's
  `sample_id` is ignored.

Scripts: `completed` (the default), `slow` (completes after `SLOW_SECONDS`, so a
stage has uneven sample durations), `time_budget`, `target_failed`,
`infrastructure_failed`, and the I1 fault codes `invalid_request` (contract)
and `pin_mismatch` (structural). A value is either one script, which every try of the
sample plays, or a `list` of scripts consumed one per opened session -- one per
try -- and `completed` once it is empty; the list object is shared by every
instance of the adapter, so a retry on another instance takes the next script.

**`time_budget` is not a wall clock.** The script used to sleep past a rollout
budget the fixtures set to fifty milliseconds, which decided the *other* samples
too: a stage that ran slowly -- late in a full suite, on a loaded host -- turned
healthy samples into rollout-owned indeterminates and a promotion into a stall.
The script now raises the core's own deadline signal from `send`, so the sample
takes exactly the `time_budget` terminal `rollout` gives an expired deadline and
nothing else in the stage is timed at all. The fixtures can then carry a budget
no stage can reach.

A `FakeInstance` counts its own live sessions and refuses one past the
`max_concurrent_sessions` the adapter admitted, so the campaign's per-instance
bound is asserted by the target itself rather than by a caller's arithmetic.

`ScriptedEditor` (Task 6's `CandidateEditor` double) lives here too, keyed by
`attempt_id` against a `Mapping[str, EditorScript]`, and `ScriptedReflector`
(the v2 Reflector X1 `CandidateReflector` double) beside it, keyed by round.

**Why the scorer writes traces.** The Reflector's evidence is read from the
scorer's own trace directory (`reflect_evidence.project_repeats` authenticates
`request.json` and reads the verdicts from `stdout`), so `DeterministicScorer`
implements `score_traced` and lays one down exactly as `SubprocessScorer` does.
Without it every repeat of every baseline projects as `unavailable`, no cluster
forms, and every round of every fixture would close `stalled` before opening an
attempt. For the same reason its default verdict *fails*: a baseline with
nothing failing has nothing to diagnose, which is a real state of the system
(`PASSING_VERDICT` asks for it) but not the campaign these fixtures are about.

Task 8 promoted the executor rig here as well -- `Rig`, `PromotingScorer`,
`RefusingAdapter`, `Crash`, `seed_tree`, `edits`, `all_attempt_ids` and
`ARTIFACT_POLICY` -- so the §13 acceptance fixtures and the Task 7 tests build
the same campaign without a second copy of it.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import pytest

from evolution import prompts
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifacts import ArtifactRef, ExpectedAttestation
from evolution.tree.artifacts.attestation import TargetPreflight
from evolution.tree.campaign import CampaignResult, IdentityChanged, resume_campaign, run_campaign
from evolution.tree.campaign_identity import build_adaptation_identity, measurement_id_for
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
)
from evolution.tree.campaign_schema import CampaignSnapshot
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.campaign_view import attempt_id_for
from evolution.tree.execution.authorizations import Authorization
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    CandidateView,
    EnvironmentIdentity,
    EnvironmentView,
    EvalCase,
    History,
    InstanceProvenance,
    Message,
    RolloutInfrastructureError,
    ScoreResult,
    ScorerIdentity,
    ScoringView,
    SemanticIdentity,
    TargetIdentity,
    TurnResult,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.rollout import _RolloutDeadlineExceeded
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import REQUIRED_ATTESTS
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.roles.config import ModelGroupingSection
from evolution.tree.roles.editor.editor_evidence import TRACE_ROOT
from evolution.tree.roles.editor_contracts import EditRequest, EditResult
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    SelectionRuntimeConfig,
)
from evolution.tree.roles.grouping.reflect_grouping import FieldFeature
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity
from evolution.tree.roles.reflector.reflect_brief import (
    reflect_brief_payload,
    render_reflect_catalog,
)
from evolution.tree.roles.reflector_contracts import ReflectRequest, ReflectResult
from evolution.tree.roles.role_call import RoleCallRetry

RANDOM_SEED = 12345
PRIMARY = "score"
STRATEGY = "artifact_root_injection"
SLOW_SECONDS = 0.05
ARTIFACT_POLICY = ArtifactPolicyV1((), (), ("skill.py",))


class ScriptedDeadlineExceeded(_RolloutDeadlineExceeded):
    """The `time_budget` script's signal: the core's own expired-deadline type.

    `rollout` turns a `_RolloutDeadlineExceeded` out of an external call into the
    `time_budget` terminal, whoever raised it, so a scripted stall reaches the
    campaign as exactly what an expired deadline reaches it as -- with no sleep,
    and with no bearing on how long any other sample in the stage takes.
    """


# `_external_call` journals `type(exc).__name__` as the `error_class` of an
# exception it did not raise itself, and `journal_rollout._failure` classifies
# that string. The class is renamed to the literal the core writes when its own
# deadline expires, so a scripted stall leaves the record a real one leaves
# rather than a second, fixture-only shape.
ScriptedDeadlineExceeded.__name__ = "deadline_timeout"

__all__ = [
    "ARTIFACT_POLICY",
    "FAILING_VERDICT",
    "PASSING_VERDICT",
    "PRIMARY",
    "RANDOM_SEED",
    "SLOW_SECONDS",
    "STRATEGY",
    "Crash",
    "DeterministicScorer",
    "EditorScript",
    "FakeAdapter",
    "FakeInstance",
    "PromotingScorer",
    "RefusingAdapter",
    "ReflectorScript",
    "Rig",
    "ScriptedEditor",
    "ScriptedEnvironment",
    "ScriptedReflector",
    "SessionOverflow",
    "all_attempt_ids",
    "artifact",
    "corpus",
    "edits",
    "all_offline_campaign_fixtures",
    "campaign_with_version_rebase",
    "finished_campaign",
    "policy",
    "reflections",
    "score_rows",
    "seed_tree",
    "v5_policy",
    "v8_policy",
]


def score_rows(
    scores: Mapping[str, Sequence[float | None]],
    *,
    seed_mode: str = "exact",
    dependency_digest: str = "deps",
    boot_id: str = "boot",
    primary: str = PRIMARY,
) -> tuple[SampleRow, ...]:
    """One measurement side's latest rows, for the case-set comparison (G2 spec §3.4).

    `scores[case_id][repeat - 1]` is that pair's primary metric; `None` is an
    indeterminate try (`target_failed`), which is not evidence. Under `exact`
    every side built here requests and gets the same seed per repeat, so two
    sides pair. Scores must be dyadic (0, 0.25, 0.5, ...) for the kernel.
    """

    rows: list[SampleRow] = []
    for case_id, values in scores.items():
        for repeat, score in enumerate(values, start=1):
            seed = RANDOM_SEED + repeat if seed_mode == "exact" else None
            scored = score is not None
            rows.append(
                SampleRow(
                    case_id=case_id,
                    sample_id=f"{repeat:02d}",
                    repeat=repeat,
                    requested_seed=seed,
                    effective_seed=seed,
                    boot_id=boot_id,
                    dependency_digest=dependency_digest,
                    outcome="completed" if scored else "target_failed",
                    disposition="score" if scored else "indeterminate",
                    metrics={primary: score} if scored else None,
                    critical_pass=None,
                )
            )
    return tuple(rows)


#: A fake's per-turn `provenance.used` (G2b1): by case id, or a callable of
#: the case id and the turn index; `None` (or a `None` value) reports none.
UsedScript = Mapping[str, Sequence[str] | None] | Callable[[str, int], Sequence[str] | None] | None


class SessionOverflow(RuntimeError):
    """More live sessions on one instance than the adapter admitted."""


class _FakeSession:
    def __init__(self, case_id: str, requested_seed: int | None, effective_seed: int | None):
        self.session_id = f"session-{case_id}-{requested_seed}"
        self.case_id = case_id
        self.requested_seed = requested_seed
        self.effective_seed = effective_seed


class FakeInstance:
    """One materialized target; every sample of it attests the same digests."""

    def __init__(
        self,
        instance_id: str,
        boot_id: str,
        semantic: SemanticIdentity,
        candidate_digest: str,
        dispositions: Mapping[tuple[str, str, str], str | list[str]],
        max_concurrent_sessions: int = 1,
        description: Mapping[str, Any] | None = None,
        components: Mapping[str, Any] | None = None,
        used: UsedScript = None,
    ) -> None:
        #: Set by `FakeAdapter.materialize` to the list the adapter keeps for
        #: this instance's measurement, so every `open` is recorded in dispatch
        #: order (Task 9's exam-first ordering).
        self.recorder: list[str] | None = None
        #: Set by `FakeAdapter.materialize` to the adapter's `session_order`,
        #: so sessions of every measurement are recorded in one global order
        #: (G2b3a's interleaved joint stage).
        self.order: list[tuple[str, str]] | None = None
        self.instance_id = instance_id
        self.boot_id = boot_id
        #: What a backend's optional `describe()` published on the creation
        #: body (PDR-0014). Captured once, never compared, never shown to a
        #: role: the measurement reads it straight into the attestation record.
        self.description = description
        #: The component map the creation body declared (ADR-0024, G2b1), as
        #: `HttpInstance.components` holds it; `None` when it declared none.
        self.components = components
        #: What each turn reports as `provenance.used` (G2b1): by case id, or
        #: a callable of the case id and turn index; `None` reports none.
        self._used = used
        self._turns: dict[_FakeSession, int] = {}
        self.candidate_digest = candidate_digest
        self.max_concurrent_sessions = max_concurrent_sessions
        self.sessions: list[_FakeSession] = []
        self.closed: list[str] = []
        self.live_sessions = 0
        self.peak_sessions = 0
        self._semantic = semantic
        # A shallow copy: a per-try script list stays the adapter's own object.
        self._dispositions = dict(dispositions)
        # One script per session, chosen when the session first sends, so a
        # multi-turn session plays one script and a retry's new session the next.
        self._chosen: dict[_FakeSession, str] = {}
        self._identity = TargetIdentity(
            semantic,
            InstanceProvenance(boot_id, candidate_digest),
            frozenset(REQUIRED_ATTESTS),
        )

    async def identity(self) -> TargetIdentity:
        return self._identity

    async def open(
        self, case_public: Mapping[str, Any], seed: int | None, request_id: str
    ) -> _FakeSession:
        case_id = str(case_public.get("case_id", ""))
        effective = seed if self._semantic.seed_mode == "exact" else None
        session = _FakeSession(case_id, seed, effective)
        self.live_sessions += 1
        self.peak_sessions = max(self.peak_sessions, self.live_sessions)
        if self.live_sessions > self.max_concurrent_sessions:
            self.live_sessions -= 1
            raise SessionOverflow(
                f"{self.instance_id} was admitted for {self.max_concurrent_sessions} sessions"
            )
        self.sessions.append(session)
        if self.recorder is not None:
            self.recorder.append(case_id)
        if self.order is not None:
            self.order.append((self.candidate_digest, case_id))
        return session

    async def send(self, session: _FakeSession, message: Message, request_id: str) -> TurnResult:
        script = self._script(session)
        if script == "time_budget":
            raise ScriptedDeadlineExceeded("scripted rollout deadline")
        if script == "slow":
            await asyncio.sleep(SLOW_SECONDS)
        if script == "infrastructure_failed":
            raise RolloutInfrastructureError("scripted target failure", "target_unavailable")
        if script == "invalid_request":
            # A contract failure (I1 spec §3.1): never retried, a fault row.
            raise RolloutInfrastructureError("scripted contract failure", "invalid_request")
        if script == "pin_mismatch":
            # A structural failure: never retried, a fault row.
            raise RolloutInfrastructureError("scripted structural failure", "pin_mismatch")
        turn = self._turns.get(session, 0)
        self._turns[session] = turn + 1
        used = self._used_for(session.case_id, turn)
        return TurnResult(
            events=({"type": "message", "text": message.content},),
            usage=Usage(prompt=1, completion=1, cached=0, total=2),
            latency_ms=1,
            outcome="target_failed" if script == "target_failed" else "ok",
            provenance=None if used is None else {"used": list(used)},
        )

    def _used_for(self, case_id: str, turn: int) -> Sequence[str] | None:
        if self._used is None:
            return None
        if callable(self._used):
            return self._used(case_id, turn)
        return self._used.get(case_id)

    async def close(self, session: _FakeSession) -> None:
        self.live_sessions = max(0, self.live_sessions - 1)
        self.closed.append(session.session_id)

    def _script(self, session: _FakeSession) -> str:
        chosen = self._chosen.get(session)
        if chosen is None:
            chosen = self._lookup(session)
            self._chosen[session] = chosen
        return chosen

    def _lookup(self, session: _FakeSession) -> str:
        value = self._stored(session)
        if isinstance(value, list):
            return value.pop(0) if value else "completed"
        return value

    def _stored(self, session: _FakeSession) -> str | list[str]:
        if self._semantic.seed_mode == "exact" and session.requested_seed is not None:
            sample_id = f"{session.requested_seed - RANDOM_SEED:02d}"
            return self._dispositions.get(
                (self.candidate_digest, session.case_id, sample_id), "completed"
            )
        for (digest, case_id, _), script in self._dispositions.items():
            if digest == self.candidate_digest and case_id == session.case_id:
                return script
        return "completed"


#: H10 plan choice 18: true while a family-14 connection probe runs, in the
#: probing task only (`family14_admitted` wraps `campaign.run_probe`). A
#: probe-neutral `FakeAdapter` then materializes a plain instance outside
#: every script and counter, so existing trajectories keep their bytes.
CONNECTION_PROBING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "connection_probing", default=False
)


class FakeAdapter:
    """A promotion-capable in-memory adapter that counts its live instances."""

    def __init__(
        self,
        seed_mode: str = "exact",
        max_instances: int = 4,
        max_concurrent_sessions: int = 2,
        dispositions: Mapping[tuple[str, str, str], str | list[str]] | None = None,
        refuse_release: frozenset[str] = frozenset(),
        description: Mapping[str, Any] | Callable[[str], Mapping[str, Any] | None] | None = None,
        components: (
            Mapping[str, Any]
            | Callable[[str], Mapping[str, Any] | None]
            | Sequence[Mapping[str, Any] | None]
            | None
        ) = None,
        used: UsedScript = None,
    ) -> None:
        self.capabilities = AdapterCapabilities(
            seed_mode,  # type: ignore[arg-type]
            max_concurrent_sessions,
            True,
            frozenset({STRATEGY}),
            max_instances,
        )
        self.seed_mode = seed_mode
        #: Reported by every instance this adapter materializes, as a backend
        #: that implements `describe()` reports the same body for each; or a
        #: callable of the instance id, for a body that differs by instance.
        self.description = description
        #: Each instance's component map (G2b1): one map for all, a callable
        #: of the candidate digest, or a sequence consumed one per
        #: materialization (`None` once it runs out).
        self.components = list(components) if isinstance(components, Sequence) else components
        #: Each turn's reported `provenance.used` (G2b1), passed to every
        #: instance: see `FakeInstance`.
        self.used = used
        self.dispositions = dict(dispositions or {})
        self.refuse_release = frozenset(refuse_release)
        self.live: dict[str, FakeInstance] = {}
        # Every instance ever materialized, kept past release so a test can
        # still read what one instance did.
        self.created: dict[str, FakeInstance] = {}
        self.materialized: list[str] = []
        self.released: list[str] = []
        self.peak_live = 0
        #: Immutable snapshots after every materialize/release transition:
        #: (operation, instance id, all live (instance id, attested tree digest)).
        #: Several instances of one tree still count as one loaded version.
        self.live_tree_samples: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.semantic: SemanticIdentity | None = None
        #: Measurement id -> the case ids its instances opened sessions for, in
        #: dispatch order; `session_case_order` reads it.
        self.case_order: dict[str, list[str]] = {}
        #: `(candidate_digest, case_id)` per session opened, across every
        #: measurement, in global order (G2b3a's interleaved joint stage).
        self.session_order: list[tuple[str, str]] = []
        self._boots = 0
        #: H10 plan choice 18: a connection probe's instance is a plain
        #: `FakeInstance` with no scripted disposition, kept out of
        #: `materialized`, `released`, the live-tree samples, the boot count and
        #: every subclass seam, but still live until released. A probe test
        #: sets `probe_neutral = False` to script the probe itself.
        self.probe_neutral = True
        self.probe_instances: list[str] = []
        self.probe_released: list[str] = []

    async def preflight(self, expected: ExpectedAttestation) -> TargetPreflight:
        self.semantic = self._semantic_for(expected.dependency_digest)
        return TargetPreflight(self.semantic, self.capabilities, "epoch-1")

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> FakeInstance:
        # An instance id is minted once per materialization (`new_instance_id`),
        # so a repeat is a caller bug, not a request for the live instance:
        # answering it would hide a double-materialize behind a working run.
        if instance_id in self.live:
            raise RolloutInfrastructureError("instance id is already live", "target_unavailable")
        if len(self.live) + 1 > self.capabilities.max_instances:
            raise RolloutInfrastructureError("max_instances exceeded", "capacity")
        if self.probe_neutral and CONNECTION_PROBING.get():
            probe = FakeInstance(
                instance_id,
                f"probe-boot-{len(self.probe_instances) + 1}",
                self._semantic_for(candidate.dependency_digest),
                candidate.digest,
                {},
                self.capabilities.max_concurrent_sessions,
            )
            self.live[instance_id] = probe
            self.probe_instances.append(instance_id)
            return probe
        self._boots += 1
        instance = self.instance_for(candidate, instance_id, f"boot-{self._boots}")
        # The R1 boundary never names the measurement, but the Candidate it
        # materializes does: a measurement is one side of one artifact, so the
        # published reference's kind and id give the measurement id back.
        measurement_id = measurement_id_for(
            None if candidate.kind == "champion" else str(candidate.artifact_id)
        )
        instance.recorder = self.case_order.setdefault(measurement_id, [])
        instance.order = self.session_order
        self.live[instance_id] = instance
        self.created[instance_id] = instance
        self.materialized.append(instance_id)
        self.peak_live = max(self.peak_live, len(self.live))
        self._sample_live_trees("materialize", instance_id)
        return instance

    async def release(self, instance_id: str) -> None:
        if instance_id in self.refuse_release:
            self._sample_live_trees("release_refused", instance_id)
            raise RuntimeError("target refuses to release")
        if instance_id in self.probe_instances:
            self.live.pop(instance_id, None)
            self.probe_released.append(instance_id)
            return
        self.live.pop(instance_id, None)
        self.released.append(instance_id)
        self._sample_live_trees("release", instance_id)

    def _sample_live_trees(self, operation: str, instance_id: str) -> None:
        self.live_tree_samples.append(
            (
                operation,
                instance_id,
                tuple(
                    (iid, instance._identity.instance.loaded_candidate_digest)
                    for iid, instance in sorted(self.live.items())
                ),
            )
        )

    def session_case_order(self, *, measurement: str) -> tuple[str, ...]:
        """The case ids one measurement's instances opened sessions for, in order.

        `measurement` is the measurement id (`m-r01-a01`, `m-r00-champion`), not
        the attempt id: a round's baseline and its attempts are different
        measurements of the same campaign.
        """

        return tuple(self.case_order.get(measurement, ()))

    # -- seams a test subclasses ------------------------------------------

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        return FakeInstance(
            instance_id,
            boot_id,
            self._semantic_for(candidate.dependency_digest),
            candidate.digest,
            self.dispositions,
            self.capabilities.max_concurrent_sessions,
            self._description_for(instance_id),
            self._components_for(candidate.digest),
            self.used,
        )

    def _description_for(self, instance_id: str) -> Mapping[str, Any] | None:
        description = self.description
        if callable(description):
            return description(instance_id)
        return description

    def _components_for(self, candidate_digest: str) -> Mapping[str, Any] | None:
        components = self.components
        if isinstance(components, list):
            return components.pop(0) if components else None
        if callable(components):
            return components(candidate_digest)
        return components

    def _semantic_for(self, dependency_digest: str) -> SemanticIdentity:
        return SemanticIdentity(
            "contract-v1",
            dependency_digest,
            "settings-v1",
            self.seed_mode,  # type: ignore[arg-type]
            "2",
            "runtime-v1",
        )


class ScriptedEnvironment:
    """`messages` messages (one by default), then stop; a fixed identity so the
    campaign identity is stable."""

    def __init__(self, content: str = "PROMPT", messages: int = 1) -> None:
        self.identity = EnvironmentIdentity(
            "scripted", hashlib.sha256(b"scripted-environment-v1").hexdigest()
        )
        self.content = content
        self.messages = messages

    async def next(self, case: EnvironmentView, history: History) -> Message | None:
        if len(history.turns) >= self.messages:
            return None
        return Message(self.content)


#: The verdict every `DeterministicScorer` emits unless a caller says otherwise.
#: A baseline with no failing verdict has no failure mode to cluster, so its
#: round has nothing to focus on and closes `clean` without opening an attempt
#: (spec §3.3) -- which is not the campaign most of these fixtures are about.
FAILING_VERDICT: Mapping[str, Any] = MappingProxyType(
    {"criterion": "answered", "passed": False, "note": None}
)
PASSING_VERDICT: Mapping[str, Any] = MappingProxyType(
    {"criterion": "answered", "passed": True, "note": None}
)


class DeterministicScorer:
    """A pure scorer: the primary metric is a hash of the Candidate and the case.

    It is also a `TracingScorer`: when `trace_root` is set (the `Rig` points it
    at `<run_root>/scorer-traces`) every scored sample leaves the same
    `request.json` / `stdout` pair `SubprocessScorer` writes, so the Reflector's
    own evidence -- the verdicts, the public feedback and the authenticated view
    (`reflect_evidence.project_repeats`) -- is readable from an in-memory
    campaign. Without a trace every repeat projects as `unavailable`, which is a
    real state of the system but not the one a campaign fixture wants.
    """

    def __init__(
        self,
        *,
        primary: str = PRIMARY,
        critical: Mapping[str, bool] | None = None,
        extra_metric: str | None = None,
        missing_primary_for: frozenset[str] = frozenset(),
        out_of_range_for: frozenset[str] = frozenset(),
        verdicts: Sequence[Mapping[str, Any]] = (FAILING_VERDICT,),
        feedback: Mapping[str, Any] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
        trace_root: Path | None = None,
    ) -> None:
        self.identity = ScorerIdentity(
            "deterministic", hashlib.sha256(b"deterministic-scorer-v1").hexdigest()
        )
        self.primary = primary
        self.critical = dict(critical or {})
        self.extra_metric = extra_metric
        self.missing_primary_for = frozenset(missing_primary_for)
        #: #97: the cases whose primary is `2.0`, outside the metric's [0, 1].
        self.out_of_range_for = frozenset(out_of_range_for)
        self.verdicts = tuple(dict(verdict) for verdict in verdicts)
        self.feedback = dict(feedback or {"format": "ok"})
        #: Family 8 only (ADR-0021): written into the trace's `stdout` only
        #: when not `None`, so a family 5-7 fixture's trace is unchanged.
        self.diagnostics = None if diagnostics is None else dict(diagnostics)
        self.trace_root = trace_root

    async def score(
        self, case: EvalCase, view: ScoringView, candidate: CandidateView
    ) -> ScoreResult:
        metrics: dict[str, float] = {}
        if case.case_id not in self.missing_primary_for:
            metrics[self.primary] = (
                2.0
                if case.case_id in self.out_of_range_for
                else self.value_for(candidate.digest, case.case_id)
            )
        if self.extra_metric is not None:
            metrics[self.extra_metric] = 1.0
        if not metrics:
            # `ScoreResult` refuses empty metrics, so a missing primary still
            # carries one unrelated metric -- exactly spec section 8's
            # `scorer_metric_missing` shape.
            metrics["unrelated"] = self.value_for(candidate.digest, case.case_id)
        critical_pass = (
            self.critical.get(case.case_id, True) if case.oracle.get("critical") is True else None
        )
        return ScoreResult(
            metrics,
            critical_pass=critical_pass,
            verdicts=self.verdicts,
            public_feedback=self.feedback,
            diagnostics=self.diagnostics,
        )

    async def score_traced(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        *,
        trace_id: str,
        pack_id: str,
    ) -> ScoreResult:
        if CONNECTION_PROBING.get() and getattr(self, "probe_neutral", False):
            result = ScoreResult(
                {self.primary: 0.0},
                critical_pass=True if case.oracle.get("critical") is True else None,
                verdicts=(FAILING_VERDICT,),
            )
        else:
            result = await self.score(case, view, candidate)
        if self.trace_root is not None:
            self._write_trace(case, view, candidate, trace_id, result)
        return result

    def _write_trace(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        trace_id: str,
        result: ScoreResult,
    ) -> None:
        """The trace `editor_evidence._read_and_bind` authenticates, byte for byte.

        The request document is `SubprocessScorer._score_sync`'s own: the four
        keys, the scorer digest, and `input_sha256` over the very serialization
        the reader recomputes.
        """

        directory = Path(self.trace_root or ".") / "scorer_traces" / trace_id
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "case_id": case.case_id,
            "public": dict(case.public),
            "oracle": dict(case.oracle),
            "view": json.loads(json.dumps(view.to_payload())),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        (directory / "request.json").write_text(
            json.dumps(
                {
                    "scorer": self.identity.digest,
                    "trace_id": trace_id,
                    "input": payload,
                    "input_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        stdout_payload: dict[str, Any] = {
            "metrics": dict(result.metrics),
            "critical_pass": result.critical_pass,
            "verdicts": [dict(verdict) for verdict in result.verdicts],
            "public_feedback": dict(result.public_feedback),
        }
        if result.diagnostics is not None:
            stdout_payload["diagnostics"] = dict(result.diagnostics)
        (directory / "stdout").write_text(json.dumps(stdout_payload), encoding="utf-8")

    @staticmethod
    def value_for(candidate_digest: str, case_id: str) -> float:
        raw = hashlib.sha256((candidate_digest + case_id).encode()).hexdigest()
        return int(raw[:8], 16) / 0xFFFFFFFF


def corpus(n: int = 3, groups: int = 1, critical: Sequence[str] = ()) -> tuple[EvalCase, ...]:
    """`n` cases over `groups` groups; `public`, `private` and `oracle` carry canaries."""

    critical_ids = set(critical)
    cases = []
    for index in range(n):
        case_id = f"case-{index + 1:02d}"
        oracle: dict[str, Any] = {"expected": "ORACLE-CANARY"}
        if case_id in critical_ids:
            oracle["critical"] = True
        cases.append(
            EvalCase(
                case_id,
                f"group-{index % groups + 1:02d}",
                f"stratum-{index % 2 + 1:02d}",
                ("tag",),
                {"case_id": case_id, "input": "PUBLIC-CANARY"},
                {"secret": "PRIVATE-CANARY"},
                oracle,
            )
        )
    return tuple(cases)


def v11_payload(
    *,
    mode: str = "statistical",
    repeats: int = 3,
    global_mode: str | None = None,
    tolerance: object = "measured_noise",
    fresh_parent: bool = False,
    round_stop: object = "first_accepted",
    refresh: object = 3,
    stability: object = "stable",
    margin: object = 0,
    **overrides: Any,
) -> dict[str, Any]:
    """Native /15 payload, including the exact former fixture defaults.

    This is raw data, so validation tests can override it with invalid values.
    No retired product parser participates in construction or normalization.
    """
    causal: dict[str, Any] = {"tolerance": tolerance}
    focus: dict[str, Any] = {"margin": margin}
    if mode == "statistical":
        focus = {"confidence": 0.9, **focus}
        causal = {"confidence": 0.9, **causal}
    payload = {
        "rounds": 2,
        "attempts_per_round": 2,
        "parallel_attempts": 1,
        "evaluation": {"repeats_per_question": repeats},
        "round_stop": round_stop,
        "baseline_refresh_after_rounds": refresh,
        "indeterminate": {
            owner: {"max_count": 2, "max_rate": "0.20"}
            for owner in ("target", "environment", "scorer", "rollout")
        },
        "budget": {"max_turns": 4, "timeout_seconds": 30.0, "max_failed_turns": 3},
        "call_retries": {"max_attempts": 1, "turn_max_attempts": 1},
        "max_concurrency": 2,
        "max_scorer_concurrency": 2,
        "metric": {"primary": PRIMARY},
        "random_seed": RANDOM_SEED,
        "editor_timeout_seconds": 900.0,
        "editor_context": None,
        "reflection_timeout_seconds": 30.0,
        "gate": {
            "mode": mode,
            "focus": focus,
            "causal": causal,
            "provenance": {"kind": "reported"},
            "fresh_parent": fresh_parent,
            "global": global_mode or ("off" if mode == "observe" else "point"),
            "stability": stability,
        },
        "attempt_feedback": "none",
    }
    payload.update(overrides)
    return payload


def v11_policy(**kwargs: Any) -> CampaignPolicyV11:
    """The /15 policy, validated once from its native payload."""
    return CampaignPolicyV11.from_payload(v11_payload(**kwargs))


def policy(**overrides: Any) -> CampaignPolicyV11:
    """The common small Rig settings, written directly as /15 controls."""
    # The old fixture's last look chose R when Rig built its native policy.
    # Retain that test input convention, never construct an alpha policy.
    alpha = overrides.pop("alpha", None)
    repeats = 3 if alpha is None else max(int(look) for look in alpha["spend"])
    budget = {"max_turns": 4, "timeout_seconds": 30.0, "max_failed_turns": 1}
    budget.update(overrides.pop("budget", {}))
    # The former margin was removed when the Rig selected native gate controls.
    overrides.pop("gate", None)
    return v11_policy(
        **{
            "mode": "observe" if repeats == 1 else "statistical",
            "repeats": repeats,
            "fresh_parent": True,
            "round_stop": "all_attempts",
            "refresh": 0,
            "sample_retries": 0,
            "budget": budget,
            **overrides,
        }
    )


def v5_policy(**overrides: Any) -> CampaignPolicyV11:
    """Native Rig settings with the shared sample-retry default of one."""
    return policy(**{"sample_retries": 1, **overrides})


def v8_policy(**overrides: Any) -> CampaignPolicyV11:
    """Native Rig settings with failed-turn tolerance and call retries."""
    budget = {"max_turns": 4, "timeout_seconds": 30.0, "max_failed_turns": 3}
    budget.update(overrides.pop("budget", {}))
    return v5_policy(budget=budget, **overrides)


def v9_policy(**overrides: Any) -> CampaignPolicyV11:
    """Native provenance fixture; kept helper spelling avoids unrelated churn."""
    return v8_policy(**overrides)


def with_final_controls(
    base: Mapping[str, Any],
    *,
    round_stop: object = "first_accepted",
    refresh: object = 3,
    stability: object = "stable",
    margin: object = 0,
) -> dict[str, Any]:
    """Select native final controls on a raw policy blueprint, without parsing."""
    payload = copy.deepcopy(dict(base))
    payload["round_stop"] = round_stop
    payload["baseline_refresh_after_rounds"] = refresh
    gate = payload["gate"]
    gate["stability"] = stability
    focus: dict[str, Any] = {"margin": margin}
    if gate["mode"] == "statistical":
        focus = {"confidence": gate["focus"]["confidence"], "margin": margin}
    gate["focus"] = focus
    return payload


def family14_admitted(patch):
    """Mark the native connection probe so scripted campaign faults stay separate."""
    from evolution.tree import campaign as campaign_module

    real_probe = campaign_module.run_probe
    if not getattr(real_probe, "marks_probing", False):

        async def probing(**kwargs):
            token = CONNECTION_PROBING.set(True)
            try:
                return await real_probe(**kwargs)
            finally:
                CONNECTION_PROBING.reset(token)

        probing.marks_probing = True
        patch.setattr(campaign_module, "run_probe", probing)


def estimate(
    point: float | None,
    lower: float | None = None,
    upper: float | None = None,
    level: float | None = None,
    unavailable: str | None = None,
) -> dict[str, Any]:
    """A design §6.5 `{point, lower, upper, level, unavailable}` estimate."""

    return {
        "point": point,
        "lower": lower,
        "upper": upper,
        "level": level,
        "unavailable": unavailable,
    }


def prefix_reference(run_id: str, locator: str) -> dict[str, Any]:
    """A `journal_prefix` durable reference with a placeholder digest; the
    lifecycle checks its shape only, the audit its bytes."""

    return {
        "version": 1,
        "kind": "journal_prefix",
        "run_id": run_id,
        "locator": locator,
        "content_digest": "0" * 64,
    }


def with_baseline_exclusion(
    records: Sequence[Mapping[str, Any]], case_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """`records` with a round-1 baseline `pairs_excluded` before its
    `baseline_complete` that excludes every repeat (samples `01`-`03`) of each
    case in `case_ids` (F3 fixtures). Sequences are renumbered; campaign-journal
    locators after the insert are not rebased, so the result feeds a
    lifecycle, never the audit."""

    from evolution.tree.campaign_schema import SAMPLE_REASONS, pairs_digest

    rows = copy.deepcopy([dict(record) for record in records])
    complete = next(
        index
        for index, row in enumerate(rows)
        if row["event"] == "baseline_complete" and row["payload"]["round"] == 1
    )
    baseline = rows[complete]["payload"]["measurement_id"]
    stage = max(
        row["payload"]["stage"]
        for row in rows[:complete]
        if row["event"] == "stage_opened" and row["payload"]["measurement_id"] == baseline
    )
    reason = sorted(SAMPLE_REASONS)[0]
    keys = [(case_id, f"{repeat:02d}") for case_id in case_ids for repeat in (1, 2, 3)]
    pairs = [
        {
            "case_id": case_id,
            "sample_id": sample_id,
            "owner": "target",
            "reason": reason,
            "tries": 1,
        }
        for case_id, sample_id in keys
    ]
    rows.insert(
        complete,
        {
            "schema_version": 1,
            "run_id": rows[0]["run_id"],
            "sequence": 0,
            "timestamp": rows[complete]["timestamp"],
            "event": "pairs_excluded",
            "operation_id": "op-baseline-excluded",
            "payload": {
                "round": 1,
                "measurement_id": baseline,
                "stage": stage,
                "count": len(pairs),
                "by_owner": {"target": len(pairs)},
                "pairs_digest": pairs_digest(keys),
                "pairs": pairs,
                "truncated": False,
                "evidence": copy.deepcopy(rows[complete]["payload"]["evidence"]),
            },
        },
    )
    for sequence, row in enumerate(rows, start=1):
        row["sequence"] = sequence
    return rows


class LifecycleFixture:
    """Shared payload and journal operations for native lifecycle fixtures.

    Family14Life and Family15Life explicitly initialize the admitted family.
    """

    def _initialize(self, rows: Sequence[Mapping[str, Any]], family_version: int) -> None:
        from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
        from evolution.tree.storage.envelope import Envelope

        self.family = campaign_family_for(family_version)
        self.state = CampaignLifecycle(selector=None, adaptive=True, family_version=family_version)
        self.envelopes: list[Any] = []
        self.run_id = str(rows[0]["run_id"])
        for row in rows:
            envelope = Envelope.from_payload(row, family=self.family)
            self.state.accept(envelope)
            self.envelopes.append(envelope)

    @property
    def snapshot(self) -> CampaignSnapshot:
        return self.state.snapshot

    @property
    def sealed(self) -> str:
        """The last sealed Attempt's id."""

        sequence = self.sequence_of("attempt_sealed")
        return str(self.envelopes[sequence - 1].payload["attempt_id"])

    def attempt(self, attempt_id: str) -> Any:
        return next(
            state.attempts[attempt_id]
            for state in self.snapshot.rounds.values()
            if attempt_id in state.attempts
        )

    def _envelope(self, event: str, payload: Mapping[str, Any], op: str | None) -> Any:
        from evolution.tree.storage.envelope import Envelope

        sequence = len(self.envelopes) + 1
        if op is None and event not in self.family.no_operation_events:
            op = f"op-{sequence}"
        return Envelope.from_payload(
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "sequence": sequence,
                "timestamp": "2026-10-04T00:00:00Z",
                "event": event,
                "operation_id": op,
                "payload": dict(payload),
            },
            family=self.family,
        )

    def accept(self, event: str, *, op: str | None = None, **payload: Any) -> int:
        """Admit one record; returns its sequence. A refusal leaves no trace."""

        envelope = self._envelope(event, payload, op)
        self.state.accept(envelope)
        self.envelopes.append(envelope)
        return len(self.envelopes)

    def append_unchecked(self, event: str, *, op: str | None = None, **payload: Any) -> int:
        """Keep a record the lifecycle never saw (an audit test's forgery); the
        envelope's own shape is still checked. Use it last."""

        self.envelopes.append(self._envelope(event, payload, op))
        return len(self.envelopes)

    def reference(self, sequence: int) -> dict[str, Any]:
        """A `journal_record` durable reference to admitted record `sequence`."""

        start = sum(len(envelope.encode()) for envelope in self.envelopes[: sequence - 1])
        data = self.envelopes[sequence - 1].encode()
        return {
            "version": 1,
            "kind": "journal_record",
            "run_id": self.run_id,
            "locator": f"campaign.jsonl#bytes={start}:{start + len(data)}",
            "content_digest": hashlib.sha256(data).hexdigest(),
        }

    def sequence_of(self, event: str, **match: Any) -> int:
        """The sequence of the last kept `event` whose payload holds every `match` item."""

        for sequence in range(len(self.envelopes), 0, -1):
            envelope = self.envelopes[sequence - 1]
            if envelope.event == event and all(
                envelope.payload.get(key) == value for key, value in match.items()
            ):
                return sequence
        raise LookupError(event)

    def write(self, path: Path) -> None:
        path.write_bytes(b"".join(envelope.encode() for envelope in self.envelopes))

    # -- the Attempt's family-12 steps ------------------------------------------

    def select(self, attempt_id: str, cases: Sequence[int]) -> int:
        attempt = self.attempt(attempt_id)
        return self.accept(
            "affected_cases_selected",
            round=attempt.round,
            attempt=attempt_id,
            source="reported",
            mode="path",
            changed=["skills/SKILL.md"],
            cases=list(cases),
            unknown_cases=[],
            fallback_reason=None,
        )

    def bind(self, attempt_id: str) -> None:
        """The Candidate's `measurement_bound` and, under `fresh_parent`, the Parent's."""

        from evolution.tree.campaign_identity import measurement_id_for, parent_measurement_id_for

        attempt = self.attempt(attempt_id)
        self.accept(
            "measurement_bound",
            attempt_id=attempt_id,
            round=attempt.round,
            measurement_id=measurement_id_for(attempt_id),
            candidate_digest=attempt.candidate_digest,
            identity_digest="b" * 64,
        )
        if self.state.policy.fresh_parent:
            self.accept(
                "parent_measurement_bound",
                attempt_id=attempt_id,
                round=attempt.round,
                measurement_id=parent_measurement_id_for(attempt_id),
                candidate_digest=attempt.parent_digest,
                identity_digest="b" * 64,
            )

    def stage(self, measurement_id: str, stage: int, look: int) -> int:
        return self.accept(
            "stage_opened", measurement_id=measurement_id, stage=stage, look=look, sample_count=1
        )

    def stages(self, attempt_id: str, stage: int, look: int) -> None:
        """One stage on the Candidate, then on the fresh Parent when bound."""

        attempt = self.attempt(attempt_id)
        self.stage(str(attempt.measurement_id), stage, look)
        if attempt.parent_measurement_id is not None:
            self.stage(attempt.parent_measurement_id, stage, look)

    # -- an accepted `continue` (F7, G11) ----------------------------------------

    def continue_block(
        self, sources: Sequence[str] = (), *, effect: str = "continue", **rebound: Any
    ) -> list[tuple[str, str]]:
        """An accepted `continue` that moves the target's settings, then one
        `measurement_rebound` per source, in order (ADR-0020, I2 spec §5.2).
        With `effect="rebaseline"` the same change is accepted as a
        `rebaseline` instead, whose block then owes the open round's close.

        Family 12's `_continue_block` over a family-13 lifecycle: the journaled
        triple carries the run's own adaptation slot and the rebase restates
        the creation's adaptation payload. `sources` are the measurement ids
        the block re-binds -- an Attempt's Candidate and fresh Parent, or (F7
        Task 4) cohort members; `rebound` overrides every re-bind's payload
        (`rebound_payload`). Returns the `(from, to)` id pairs."""

        from evolution.tree.execution.authorizations import (
            Authorization,
            identity_differences,
            identity_instance_digest,
            identity_record,
        )
        from evolution.tree.execution.journal_identity import canonical_json

        state = self.state
        assert state.created is not None and state._adaptation is not None
        before = {
            "identity": copy.deepcopy(dict(state.snapshot.identity)),
            "identity_digest": state.snapshot.identity_digest,
            "producing_version": state.snapshot.producing_version,
            "adaptation": dict(state._adaptation),
        }
        identity = copy.deepcopy(before["identity"])
        identity["target"]["settings_hash"] = f"settings-{len(state.snapshot.epochs) + 1}"
        rebuilt = {
            **before,
            "identity": identity,
            "identity_digest": hashlib.sha256(canonical_json(identity)).hexdigest(),
        }

        def triple(record: Mapping[str, Any]) -> dict[str, Any]:
            projected = identity_record(record)
            return {name: projected[name] for name in ("identity_digest", "producing_version")} | {
                "adaptation_digest": projected["adaptation_digest"]
            }

        journaled, observed = triple(before), triple(rebuilt)
        instance = identity_instance_digest(journaled, observed)
        decision = Authorization.new(
            "run_identity_changed",
            instance,
            "the target moved",
            now="2026-10-05T10:00:00Z",
            effect=effect,
        )
        self.accept(
            "authorization_applied",
            effect=effect,
            code="run_identity_changed",
            instance_digest=instance,
            authorization=decision.decision_payload(),
            journaled=journaled,
            observed=observed,
        )
        differences = identity_differences(identity_record(before), identity_record(rebuilt))
        self.accept(
            "identity_rebased",
            **rebuilt,
            adaptation_payload=state.created.payload["adaptation_payload"],
            previous_digest=before["identity_digest"],
            changed_fields=sorted(difference.field for difference in differences),
            instance_digest=instance,
            authorization=decision.journal_payload(),
        )
        moved = []
        for source in sources:
            payload = self.rebound_payload(source, **rebound)
            self.accept("measurement_rebound", **payload)
            moved.append((source, str(payload["measurement_id"])))
        return moved

    def rebound_payload(self, source: str, **overrides: Any) -> dict[str, Any]:
        """The `measurement_rebound` of `source` into the current epoch, carrying nothing."""

        from evolution.tree.campaign_epochs import base_measurement_id, measurement_id_for_epoch

        epoch = len(self.snapshot.epochs) - 1
        payload: dict[str, Any] = {
            "round": self.snapshot.measurements[source]["round"],
            "from_measurement_id": source,
            "measurement_id": measurement_id_for_epoch(base_measurement_id(source), epoch),
            "epoch": epoch,
            "identity_digest": "b" * 64,
            "imported": 0,
            "pairs_digest": "b" * 64,
            "released": 0,
        }
        payload.update(overrides)
        return payload

    # -- the family-13 gate records (F3) ----------------------------------------

    def calibration_bound_payload(self, attempt_id: str, **overrides: Any) -> dict[str, Any]:
        """The `noise_calibration_bound` the comparison owes (design §6.3)."""

        attempt = self.attempt(attempt_id)
        comparison = attempt.comparison
        assert comparison is not None
        fresh = comparison["parent_source"] == "fresh_parent"
        payload: dict[str, Any] = {
            "round": attempt.round,
            "attempt_id": attempt_id,
            "comparison": comparison["seq"],
            "supersedes": None,
            "source_measurement_id": (
                comparison["parent_measurement_id"]
                if fresh
                else comparison["baseline_measurement_id"]
            ),
            "source": comparison["parent_source"],
            "prefix": None if fresh else self.snapshot.rounds[attempt.round].baseline_prefix,
            "sizes": {"first": comparison["repeats"], "second": comparison["repeats"]},
            "method": dict(comparison["method"]),
        }
        payload.update(overrides)
        return payload

    def calibration_completed_payload(self, attempt_id: str, **overrides: Any) -> dict[str, Any]:
        """A `noise_calibration_completed` for the bound calibration (design
        §6.4): a finite allowance of 0.05 over hand-picked values. The values
        are the audit's to recompute (F4), never the lifecycle's."""

        attempt = self.attempt(attempt_id)
        calibration = attempt.calibration
        assert calibration is not None
        prefix = (
            calibration["prefix"]
            if calibration["source"] == "baseline"
            else f"measurements/{calibration['source_measurement_id']}/events.jsonl#bytes=0:4096"
        )
        payload: dict[str, Any] = {
            "round": attempt.round,
            "attempt_id": attempt_id,
            "binding": calibration["seq"],
            "prefix": prefix,
            "allowance": 0.05,
            "degenerate": False,
            "unavailable": None,
            "result_digest": "d" * 64,
        }
        payload.update(overrides)
        return payload

    def attempt_scopes(self, attempt_id: str) -> tuple[list[int], list[int]]:
        """F and C of the selected Attempt, as sorted corpus indices
        (`confidence_scopes` over its selection and its round's exam)."""

        from evolution.tree.campaign_schema import confidence_scopes

        attempt = self.attempt(attempt_id)
        scope = self.snapshot.rounds[attempt.round].gate_scope
        assert attempt.selection is not None and scope is not None
        return confidence_scopes(
            attempt.selection, self.state.identity["corpus_cases"], scope["cases"]
        )

    def comparison_payload(self, attempt_id: str, **overrides: Any) -> dict[str, Any]:
        """The `comparison_bound` the lifecycle expects next for `attempt_id`
        (design §6.2): F and C from the selection, the policy's mode, R, looks,
        confidence and tolerance, and every case retained. When the round's
        baseline journaled a `pairs_excluded`, `exclusions` repeats it and the
        evidence cites it; `retained` stays whole, for the test to narrow."""

        from evolution.tree.campaign_schema import comparison_confidence, comparison_tolerance
        from evolution.tree.confidence_statistics import method_object

        attempt = self.attempt(attempt_id)
        snapshot = self.snapshot
        state = snapshot.rounds[attempt.round]
        focus, causal = self.attempt_scopes(attempt_id)
        policy = self.state.policy
        payload: dict[str, Any] = {
            "round": attempt.round,
            "attempt_id": attempt_id,
            "supersedes": None,
            "mode": policy.gate_mode,
            "epoch": len(snapshot.epochs) - 1,
            "candidate_measurement_id": attempt.measurement_id,
            "parent_measurement_id": attempt.parent_measurement_id,
            "baseline_measurement_id": state.baseline_measurement_id,
            "parent_source": "fresh_parent" if policy.fresh_parent else "baseline",
            "focus": focus,
            "causal": causal,
            "retained": {"focus": focus, "causal": causal},
            "exclusions": None,
            "repeats": policy.repeats,
            "looks": list(policy.looks),
            "confidence": comparison_confidence(policy),
            "tolerance": comparison_tolerance(policy),
            "method": method_object(),
            "evidence": [
                self.reference(self.sequence_of("affected_cases_selected", attempt=attempt_id))
            ],
        }
        try:
            excluded = self.sequence_of(
                "pairs_excluded", measurement_id=state.baseline_measurement_id
            )
        except LookupError:
            excluded = None
        if excluded is not None:
            record = self.envelopes[excluded - 1].payload
            payload["exclusions"] = {
                "prefix": record["evidence"][0]["locator"],
                "pairs_digest": record["pairs_digest"],
                "count": record["count"],
            }
            payload["evidence"].append(self.reference(excluded))
        payload.update(overrides)
        return payload

    def look_payload(self, attempt_id: str, look: int, **overrides: Any) -> dict[str, Any]:
        """A `comparison_look_evaluated` (design §6.5) whose hand-picked published
        values pass: an interim look continues, a final statistical look gives
        `gate_passed`, an observe look `observe_finalist`. Tolerance and caveats
        follow the bound comparison and calibration."""

        from evolution.tree.campaign_schema import look_caveats, look_tolerance
        from evolution.tree.confidence_statistics import global_level, would_pass_point

        attempt = self.attempt(attempt_id)
        comparison = attempt.comparison
        assert comparison is not None
        final = look == max(comparison["looks"])
        tolerance = look_tolerance(comparison, attempt.calibration_result)
        causal_empty = not comparison["causal"]
        would: bool | None = None
        causal: dict[str, Any] | None
        overall: dict[str, Any] | None
        reason: str | None
        if comparison["mode"] == "observe":
            observed = "not_requested_in_observe_mode"
            focus = estimate(0.25, unavailable=observed)
            causal = None if causal_empty else estimate(0.0, unavailable=observed)
            overall = estimate(0.2, unavailable=observed)
            decision, reason = "accepted", "observe_finalist"
            would = would_pass_point(0.25, None if causal_empty else 0.0, tolerance["value"])
        else:
            confidence = comparison["confidence"]
            if not final:
                focus = estimate(0.25, 0.05, 0.45, confidence["focus"])
                causal, overall = None, None
                decision, reason = "continue", None
            else:
                level = global_level(
                    confidence["focus"], confidence["causal"], causal_empty=causal_empty
                )
                focus = estimate(0.25, 0.05, None, confidence["focus"])
                causal = None if causal_empty else estimate(0.0, 0.0, None, confidence["causal"])
                overall = estimate(0.2, 0.02, None, level)
                decision, reason = "accepted", "gate_passed"
        parent = comparison["parent_measurement_id"] or comparison["baseline_measurement_id"]
        prefixes = {
            "candidate": f"measurements/{comparison['candidate_measurement_id']}/events.jsonl#bytes=0:4096",
            "parent": f"measurements/{parent}/events.jsonl#bytes=0:4096",
        }
        payload: dict[str, Any] = {
            "round": attempt.round,
            "attempt_id": attempt_id,
            "comparison": comparison["seq"],
            "look": look,
            "final": final,
            "prefixes": prefixes,
            "focus": focus,
            "causal": causal,
            "overall": overall,
            "tolerance": tolerance,
            "decision": decision,
            "reason": reason,
            "would_pass_point": would,
            "caveats": look_caveats(comparison, tolerance, attempt.calibration_result),
            "evidence": [
                prefix_reference(self.run_id, prefixes["candidate"]),
                prefix_reference(self.run_id, prefixes["parent"]),
            ],
        }
        payload.update(overrides)
        return payload


def adaptation_identity_fields(
    cases: Sequence[EvalCase] | None = None, campaign_policy: CampaignPolicyV11 | None = None
) -> dict[str, Any]:
    """`build_adaptation_identity`'s keyword arguments other than `grouping`,
    `family_membership`, `profile` and `reflection`, exactly as
    `_current_adaptive_config` passes them (defaults: `corpus(2)` and
    `v8_policy()`)."""

    cases = corpus(2) if cases is None else cases
    campaign_policy = v8_policy() if campaign_policy is None else campaign_policy
    selection = SelectionRuntimeConfig(focus_cases=min(16, max(1, len(cases))))
    assert campaign_policy.attempt_feedback is not None
    return {
        "selection": {
            "focus_cases": selection.focus_cases,
            "representatives_per_case": selection.representatives_per_case,
            "context_page_size": selection.context_page_size,
        },
        "families": {"kind": "none"},
        "attempt_feedback": campaign_policy.attempt_feedback,
        "reflection_metadata_digest": hashlib.sha256(canonical_json({})).hexdigest(),
        "environment_docs_digest": None,
    }


def adaptation_identity_kwargs(cases=None, campaign_policy=None, *, schema=SCHEMA_V15):
    """The /15 Rig's declared grouping, train partition and reflection controls."""
    if schema != SCHEMA_V15:
        raise ValueError("Rig fixtures support only /15")
    cases = corpus(2) if cases is None else cases
    from evolution.tree.execution.journal_identity import corpus_digest as digest_cases

    partition = {
        "profile": "corpus_partition/1",
        "mode": "implicit_train",
        "assignments": [[case.case_id, "train"] for case in cases],
        "corpus_digest": digest_cases(tuple(cases)),
        "metadata_digest": hashlib.sha256(
            canonical_json(
                {
                    "train": {case.case_id: {} for case in cases},
                    "test": {},
                }
            )
        ).hexdigest(),
        "train_digest": digest_cases(tuple(cases)),
        "test_digest": hashlib.sha256(canonical_json([])).hexdigest(),
    }
    return {
        "grouping": {
            "kind": "fields",
            "features": [
                {
                    "name": "terminal",
                    "source": "observation",
                    "path": "/terminal",
                    "description": "terminal result",
                }
            ],
        },
        "family_membership": {},
        "profile": "campaign_adaptation/7",
        "reflection": {"max_consecutive_failures": 2},
        "corpus_partition": partition,
        **adaptation_identity_fields(cases, campaign_policy),
    }


def _current_adaptive_config(
    cases: Sequence[EvalCase], campaign_policy: CampaignPolicyV11, *, schema: str = SCHEMA_V15
) -> AdaptiveCampaignRuntimeConfig:
    """Build sealed current adaptive inputs without a production legacy converter."""

    kwargs = adaptation_identity_kwargs(cases, campaign_policy, schema=schema)
    selection = SelectionRuntimeConfig(**kwargs["selection"])
    identity = build_adaptation_identity(**kwargs)
    return AdaptiveCampaignRuntimeConfig(
        identity=identity,
        grouping_features=(
            FieldFeature("terminal", "observation", "/terminal", "terminal result"),
        ),
        selection=selection,
        families=kwargs["families"],
    )


#: `tests/tree/manifest/test_manifest_v2.py`'s `MODEL_GROUPING` manifest section, parsed:
#: every optional left at its default.
_MODEL_GROUPING_RETRY = {
    "version": "role_call_retry/1",
    "max_attempts": 3,
    "total_budget_seconds": 90,
    "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 15, "jitter": "full"},
}


def model_grouping_section() -> ModelGroupingSection:
    """The `ModelGroupingSection` `MODEL_GROUPING` parses to (Task 2's fixture)."""

    return ModelGroupingSection(
        backend="openai_chat",
        model="grouper-small",
        base_url="https://llm.example.test/v1",
        api_key_env="GROUPER_KEY",
        timeout_seconds=600,
        concurrency=4,
        max_input_chars=200000,
        max_output_tokens=8192,
        retry=RoleCallRetry.from_payload(_MODEL_GROUPING_RETRY),
    )


def model_grouping_identity_payload() -> dict[str, Any]:
    """`model_grouping_section()`'s adaptation identity grouping payload (ADR-0022).

    Written out by hand rather than through `evolve._model_grouping_identity`,
    so a test can compare the two. `prompt_sha256` is the packaged prompt's
    digest, sealed as a chat role's packaged default is.
    """

    return {
        "kind": "model",
        "protocol": "model_grouping/1",
        "backend": "openai_chat",
        "model": "grouper-small",
        "base_url": "https://llm.example.test/v1",
        "api_key_env": "GROUPER_KEY",
        "proxy_env": None,
        "verify_ssl_certs": True,
        "reasoning_effort": None,
        "provider_only": None,
        "timeout_seconds": 600,
        "concurrency": 4,
        "max_input_chars": 200000,
        "max_output_tokens": 8192,
        "representatives_per_case": 3,
        "retry": copy.deepcopy(_MODEL_GROUPING_RETRY),
        "prompt_sha256": hashlib.sha256(
            prompts.TREE_GROUPING_MODEL_SYSTEM.encode("utf-8")
        ).hexdigest(),
        "fallback": None,
    }


def artifact(root: Path, *, kind: str = "champion", artifact_id: str = "a-1") -> ArtifactRef:
    """A real `ArtifactRef` over `root`, so `CandidateView.verify` reads real bytes."""

    artifact_policy = ArtifactPolicyV1((), (), ())
    scan = scan_tree(root, artifact_policy)
    return ArtifactRef(
        kind,  # type: ignore[arg-type]
        artifact_id,
        root,
        scan.candidate_digest,
        scan.dependency_digest,
        artifact_policy,
    )


@dataclass(frozen=True)
class EditorScript:
    """One `ScriptedEditor` action for one attempt.

    `writes` (relative path -> bytes) are written into the workspace before
    `edit` returns `edited`. `swallow_cancel` catches exactly the *first*
    `asyncio.CancelledError` and keeps sleeping, to prove `run_editor`'s
    `unresponsive` outcome (still alive after `EDITOR_GRACE_SECONDS`, which
    `run_editor` itself never re-cancels past); it then waits on a second,
    ordinary sleep that a caller's own later cancellation (including a test
    harness's own event-loop teardown) reaps normally, so the fake never
    leaks a truly immortal task past the test that created it. `sleep` sleeps
    `seconds` without catching cancellation, so a deadline or a `cancelled()`
    signal cancels it promptly.
    """

    action: Literal["edit", "declined", "failed", "raise", "sleep", "swallow_cancel"]
    writes: Mapping[str, bytes] = field(default_factory=dict)
    note: str = ""
    seconds: float = 0.0

    @staticmethod
    def sequence(steps: Sequence[str]) -> SequencedEditor:
        """An Editor that plays `steps` in call order, whatever the Attempt,
        then edits (see `SequencedEditor` for the step grammar)."""

        return SequencedEditor(list(steps), {})

    @staticmethod
    def per_attempt(steps: Mapping[int, Sequence[str]]) -> SequencedEditor:
        """An Editor whose Attempt of 0-based index `i` in its round plays
        `steps[i]` in call order, then edits."""

        return SequencedEditor([], {index: list(entry) for index, entry in steps.items()})

    @staticmethod
    def slow(*, seconds: float) -> SequencedEditor:
        """An Editor whose every call sleeps `seconds` before it edits."""

        return SequencedEditor([f"slow:{seconds}"] * 64, {})


class SequencedEditor:
    """A `CandidateEditor` double that plays a list of steps (family-10 tests).

    Steps: `edited` writes a new `skill.py`; `fail:<code>[:<s>]` returns
    `EditResult("failed", "<code> scripted", code=<code>)`, after sleeping `s`
    seconds when given; `slow:<s>` sleeps
    `s` seconds, then edits; `raise` raises `RuntimeError`;
    `edited_after_continuation` writes `skill.py` and returns
    `EditResult("edited", ..., continued=True)`, modelling an Editor whose
    outcome carries family 10's one continuation turn. Once a list is spent
    every call edits. The identity is fixed, so steps a test changes between
    runs model a repaired environment, not a changed declaration.
    """

    identity = EditorIdentity("scripted-sequence", hashlib.sha256(b"sequence").hexdigest())

    def __init__(self, steps: list[str], per_attempt: dict[int, list[str]]) -> None:
        self.steps = steps
        self.per_attempt = per_attempt
        self.requests: list[EditRequest] = []

    async def edit(self, request: EditRequest) -> EditResult:
        self.requests.append(request)
        index = int(request.attempt_id.rsplit("-a", 1)[1]) - 1
        queue = self.per_attempt.get(index) if self.per_attempt else self.steps
        step = queue.pop(0) if queue else "edited"
        if step.startswith("fail:"):
            code, _, delay = step.removeprefix("fail:").partition(":")
            if delay:
                await asyncio.sleep(float(delay))
            return EditResult("failed", f"{code} scripted", code=code)
        if step.startswith("slow:"):
            await asyncio.sleep(float(step.removeprefix("slow:")))
        elif step == "raise":
            raise RuntimeError("scripted editor exception")
        elif step == "edited_after_continuation":
            target = request.workspace / "skill.py"
            target.write_text(f"VALUE = {len(self.requests) + 1}\n", encoding="utf-8")
            return EditResult("edited", "edited after continuation", continued=True)
        elif step != "edited":
            raise AssertionError(f"unknown editor step: {step!r}")
        target = request.workspace / "skill.py"
        target.write_text(f"VALUE = {len(self.requests) + 1}\n", encoding="utf-8")
        return EditResult("edited", "")


class ScriptedEditor:
    """A `CandidateEditor` double keyed by `attempt_id` against a script mapping."""

    def __init__(self, script: Mapping[str, EditorScript]) -> None:
        #: Every `EditRequest` this editor was handed, in call order.
        self.requests: list[EditRequest] = []
        self.script = dict(script)
        canonical = canonical_json(
            {
                attempt_id: {
                    "action": entry.action,
                    "writes": {path: entry.writes[path].hex() for path in sorted(entry.writes)},
                    "note": entry.note,
                    "seconds": entry.seconds,
                }
                for attempt_id, entry in sorted(self.script.items())
            }
        )
        self.identity = EditorIdentity("scripted", hashlib.sha256(canonical).hexdigest())

    async def edit(self, request: EditRequest) -> EditResult:
        self.requests.append(request)
        entry = self.script[request.attempt_id]
        if entry.action == "edit":
            for rel_path, content in entry.writes.items():
                target = request.workspace / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return EditResult("edited", entry.note)
        if entry.action == "declined":
            return EditResult("declined", entry.note)
        if entry.action == "failed":
            return EditResult("failed", entry.note)
        if entry.action == "raise":
            raise RuntimeError(entry.note)
        if entry.action == "sleep":
            await asyncio.sleep(entry.seconds)
            return EditResult("edited", entry.note)
        if entry.action == "swallow_cancel":
            try:
                await asyncio.sleep(entry.seconds or 3600.0)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(3600.0)
            return EditResult("edited", entry.note)
        raise AssertionError(f"unknown EditorScript action: {entry.action!r}")


@dataclass(frozen=True)
class ReflectorScript:
    """One `ScriptedReflector` action for one round.

    `reflect` returns `groups` (each completed with real citations when the
    reflector was built with `cite_first_exemplar`); `declined` and `failed`
    are the two other `ReflectResult` statuses; `raise` raises; `sleep` sleeps
    `seconds` without catching cancellation, so a deadline or a `cancelled()`
    signal reaps it promptly. `swallow_cancel` is `EditorScript`'s own: it
    catches exactly the first cancellation and keeps sleeping, which is what
    proves the `unresponsive` outcome, then waits on an ordinary sleep a
    caller's later cancellation reaps, so no task outlives its test.
    """

    action: Literal["reflect", "declined", "failed", "raise", "sleep", "swallow_cancel"]
    groups: Sequence[Mapping[str, Any]] = ()
    note: str = ""
    seconds: float = 0.0
    #: `failed` only: the explicit failure code the result carries
    #: (`ReflectResult.code`, family 10's closed reasons, ADR-0023).
    code: str | None = None

    @staticmethod
    def step(text: str) -> ReflectorScript:
        """One step of `sequence`: `fail:<code>`, `declined`, `reflected`,
        `raise` or `timeout` (a sleep far past any test deadline)."""

        if text.startswith("fail:"):
            code = text.removeprefix("fail:")
            return ReflectorScript("failed", note=f"{code} scripted", code=code)
        if text == "declined":
            return ReflectorScript("declined")
        if text == "reflected":
            return ReflectorScript("reflect", groups=[_FAMILY_GROUP])
        if text == "raise":
            return ReflectorScript("raise")
        if text == "timeout":
            return ReflectorScript("sleep", seconds=3600.0)
        raise AssertionError(f"unknown ReflectorScript step: {text!r}")

    @classmethod
    def failing(cls, *, code: str) -> ProgrammableReflector:
        """A Reflector whose every call fails with `code` (family 10, spec §4.1)."""

        return ProgrammableReflector([], default=cls.step(f"fail:{code}"))

    @classmethod
    def reflected(cls) -> ProgrammableReflector:
        """A Reflector whose every call returns one claim-bearing group."""

        return ProgrammableReflector([], default=cls.step("reflected"))

    @classmethod
    def slow(cls, *, seconds: float) -> ProgrammableReflector:
        """A Reflector whose every call sleeps `seconds` and then declines; a
        stop or a caller's cancellation cuts it short."""

        return ProgrammableReflector([], default=ReflectorScript("sleep", seconds=seconds))

    @classmethod
    def sequence(cls, steps: Sequence[str]) -> ProgrammableReflector:
        """A Reflector that plays `steps` (see `step`) one per call, then declines."""

        return ProgrammableReflector([cls.step(text) for text in steps])


def _reflector_script_payload(entry: ReflectorScript) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": entry.action,
        "groups": [dict(group) for group in entry.groups],
        "note": entry.note,
        "seconds": entry.seconds,
    }
    # Only a scripted code enters the identity, so every earlier fixture's
    # Reflector digest is unchanged.
    if entry.code is not None:
        payload["code"] = entry.code
    return payload


class ScriptedReflector:
    """A `CandidateReflector` double keyed by round against a script mapping.

    `cite_first_exemplar` rewrites every scripted group's `evidence_ids` and
    `affected_cases` from the request's own catalog -- the first exemplar
    observation record, or the first two distinct cases' with
    `claim_two_cases` -- so the fixture cites evidence the executor's
    `validate_groups` accepts instead of inventing ids the campaign would
    refuse.
    """

    def __init__(
        self,
        script: Mapping[int, ReflectorScript],
        *,
        default: ReflectorScript = ReflectorScript("declined"),
        cite_first_exemplar: bool = False,
        claim_two_cases: bool = False,
        control_prefix: str | None = None,
    ) -> None:
        self.script = dict(script)
        self.default = default
        self.cite_first_exemplar = cite_first_exemplar
        self.claim_two_cases = claim_two_cases
        #: What the `agent_cli` kind declares: the one subtree of the frozen
        #: projection the campaign must leave writable. The campaign reads it
        #: with `getattr`, so `None` stands for a kind that declares none.
        self.control_prefix = control_prefix
        #: Every `ReflectRequest` this reflector was handed, in call order.
        self.requests: list[ReflectRequest] = []
        #: Per request, every regular file of the workspace and its mode, read
        #: while the call is live: the projection is discarded the moment
        #: `reflect` returns, so a test cannot look at it afterwards.
        self.workspaces: list[dict[str, int]] = []
        canonical = canonical_json(
            {
                "default": _reflector_script_payload(default),
                "rounds": {
                    str(number): _reflector_script_payload(entry)
                    for number, entry in sorted(self.script.items())
                },
            }
        )
        self.identity = ReflectorIdentity("scripted", hashlib.sha256(canonical).hexdigest())

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        self.requests.append(request)
        root = Path(request.workspace)
        self.workspaces.append(
            {
                str(path.relative_to(root)): path.stat().st_mode
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
        )
        entry = self.script.get(request.round, self.default)
        if entry.action == "swallow_cancel":
            try:
                await asyncio.sleep(entry.seconds or 3600.0)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(3600.0)
            return ReflectResult("declined", (), entry.note)
        return await self._play(request, entry)

    async def _play(self, request: ReflectRequest, entry: ReflectorScript) -> ReflectResult:
        if entry.action == "raise":
            raise RuntimeError(entry.note or "scripted reflector failure")
        if entry.action == "sleep":
            await asyncio.sleep(entry.seconds)
            return ReflectResult("declined", (), entry.note)
        if entry.action in ("declined", "failed"):
            return ReflectResult(entry.action, (), entry.note, code=entry.code)
        if entry.action != "reflect":
            raise AssertionError(f"unknown ReflectorScript action: {entry.action!r}")
        groups = tuple(self._cited(request, group) for group in entry.groups)
        return ReflectResult("reflected", groups, entry.note)

    def _cited(self, request: ReflectRequest, group: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.cite_first_exemplar:
            return dict(group)
        wanted = 2 if self.claim_two_cases else 1
        ids: list[str] = []
        cases: list[str] = []
        observation_ids = getattr(request.evidence, "observation_ids", None)
        failing_ids = getattr(request.evidence, "failing_evidence_ids", frozenset())
        if isinstance(observation_ids, Mapping):
            for evidence_id, case_id in observation_ids.items():
                if evidence_id not in failing_ids or case_id in cases:
                    continue
                ids.append(str(evidence_id))
                cases.append(str(case_id))
                if len(cases) == wanted:
                    break
            return {**dict(group), "evidence_ids": ids, "affected_cases": cases}
        for record in render_reflect_catalog(request.evidence):
            if record.kind != "observation" or record.case_id in cases:
                continue
            ids.append(record.evidence_id)
            cases.append(str(record.case_id))
            if len(cases) == wanted:
                break
        return {**dict(group), "evidence_ids": ids, "affected_cases": cases}


class ProgrammableReflector(ScriptedReflector):
    """A `ScriptedReflector` that plays its steps per *call*, not per round.

    Family 10 (PDR-0019) re-runs a suspended reflection under a new id in the
    same round, so a per-round script cannot tell the retry from the first
    call. Its identity is fixed, whatever it plays: a test that swaps one in
    for a resume models an operator who repaired the role's environment, not
    one who changed its declaration (which would be an identity change).
    """

    def __init__(
        self,
        steps: Sequence[ReflectorScript],
        *,
        default: ReflectorScript = ReflectorScript("declined"),
    ) -> None:
        super().__init__({}, default=default, cite_first_exemplar=True)
        self.steps = list(steps)
        self.identity = ReflectorIdentity(
            "scripted", hashlib.sha256(b"programmable-reflector").hexdigest()
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        self.requests.append(request)
        entry = self.steps.pop(0) if self.steps else self.default
        return await self._play(request, entry)


class DiagnosticsRecordingReflector(ScriptedReflector):
    """`ScriptedReflector` plus a record of the opening and every `read_evidence`
    response, so a test can see exactly where a scorer's diagnostics (ADR-0021,
    family 8) travel -- and only there.

    `openings` holds one `reflect_brief_payload(request.evidence)` per call, in
    round order (this is the same canonical document `render_reflect_opening`
    inlines into the Reflector's first message, so a diagnostics value opened
    whole or bounded there is visible in this recording too). `reads` holds one
    dict per call, mapping every evidence id of the request's own catalog to
    the `read_evidence` response the reader actually returned -- a real
    dispatch through `request.reader`, not a shortcut around it, so a reader
    admission or integrity rule that would refuse the read is exercised the
    same way a live Reflector would trigger it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.openings: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        #: Each call's `AdaptiveReflectBrief.families_expand` (the opening's
        #: family-expansion sentence), so a test can see the campaign's wiring.
        self.families_expand: list[bool] = []

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        observation_ids = getattr(request.evidence, "observation_ids", None) or {}
        reads: dict[str, Any] = {}
        for evidence_id in observation_ids:
            reads[evidence_id] = request.reader.dispatch(
                "read_evidence", {"evidence_id": evidence_id}
            )
        self.reads.append(reads)
        self.families_expand.append(bool(getattr(request.evidence, "families_expand", False)))
        self.openings.append(json.loads(canonical_json(reflect_brief_payload(request.evidence))))
        return await super().reflect(request)


def reflections(
    *rounds: int, groups: Sequence[Mapping[str, Any]] = ()
) -> dict[int, ReflectorScript]:
    """A `ScriptedReflector` script that reflects `groups` in each named round."""

    return {number: ReflectorScript("reflect", groups=groups) for number in rounds}


def seed_tree(root: Path, value: str = "VALUE = 1\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.py").write_text(value, encoding="utf-8")
    return root


def edits(*attempt_ids: str, action: str = "edit") -> dict[str, EditorScript]:
    return {
        attempt_id: EditorScript(
            action,  # type: ignore[arg-type]
            writes={"skill.py": f"VALUE = {index + 2}\n".encode()},
            note=f"note-{attempt_id}",
        )
        for index, attempt_id in enumerate(attempt_ids)
    }


def all_attempt_ids(campaign_policy: CampaignPolicyV11) -> tuple[str, ...]:
    return tuple(
        attempt_id_for(round_number, index)
        for round_number in range(1, campaign_policy.rounds + 1)
        for index in range(1, campaign_policy.attempts_per_round + 1)
    )


class PromotingScorer(DeterministicScorer):
    """The seed scores 0 and every edited Candidate scores 1, so a look promotes.

    The gate needs a real signal: `DeterministicScorer` hashes the digest, which
    makes promotion a coin flip and leaves the promoted half of spec §6 untested.
    """

    def __init__(self, seed_digest: str, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.seed_digest = seed_digest

    def value_for(self, candidate_digest: str, case_id: str) -> float:  # type: ignore[override]
        return 0.0 if candidate_digest == self.seed_digest else 1.0


class _ProbeNeutralAdapter:
    """Keep a shared rig's scripted stage faults out of its neutral connection probe."""

    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    async def materialize(self, *args: Any, **kwargs: Any) -> Any:
        if CONNECTION_PROBING.get():
            return await FakeAdapter.materialize(self.adapter, *args, **kwargs)
        return await self.adapter.materialize(*args, **kwargs)

    async def release(self, instance_id: str) -> None:
        if CONNECTION_PROBING.get():
            await FakeAdapter.release(self.adapter, instance_id)
        else:
            await self.adapter.release(instance_id)


class Rig:
    """Everything `run_campaign` needs, over real logs under `tmp_path`."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        campaign_policy: CampaignPolicyV11 | None = None,
        adapter: FakeAdapter | None = None,
        scorer: DeterministicScorer | None = None,
        editor: ScriptedEditor | None = None,
        reflector: ScriptedReflector | None = None,
        cases: Any = None,
        run_name: str = "run",
        schema: str = SCHEMA_V15,
    ) -> None:
        self.tmp_path = tmp_path
        self.run_root = tmp_path / run_name
        self.seed_root = seed_tree(tmp_path / f"{run_name}-seed")
        # Native /15 binds profile /7, policy V11 and the probe-neutral contract.
        if schema != SCHEMA_V15:
            raise ValueError("Rig fixtures support only /15")
        self.schema = schema
        self.policy = campaign_policy or v11_policy(round_stop="all_attempts", refresh=0)
        self.adapter = adapter or FakeAdapter()
        self.environment = ScriptedEnvironment()
        self.scorer = scorer or PromotingScorer(
            scan_tree(self.seed_root, ARTIFACT_POLICY).candidate_digest
        )
        self.cases = cases if cases is not None else corpus(2)
        self.adaptive = _current_adaptive_config(self.cases, self.policy, schema=schema)
        self.editor = editor or ScriptedEditor(edits(*all_attempt_ids(self.policy)))
        if reflector is None:
            # Family 9 onward dispatches no Editor without a valid hypothesis
            # (ADR-0022): the default Reflector cites one failing case in
            # every round, so the rig opens its Attempts.
            reflector = ScriptedReflector(
                {},
                default=ReflectorScript("reflect", groups=[_FAMILY_GROUP]),
                cite_first_exemplar=True,
            )
        self.reflector = reflector or ScriptedReflector({})
        #: Set by fixtures that need it (`tests/test_tree_campaign_epochs.py`):
        #: the `campaign_created` payload's `identity` field, for comparison
        #: against `CampaignSnapshot.epochs[0]`.
        self.creation_identity_payload: dict[str, Any] | None = None

    def kwargs(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = dict(
            run_root=self.run_root,
            seed_root=self.seed_root,
            artifact_policy=ARTIFACT_POLICY,
            adapter=self.adapter,
            environment=self.environment,
            scorer=self.scorer,
            editor=self.editor,
            reflector=self.reflector,
            environment_docs=None,
            corpus=self.cases,
            adaptive=self.adaptive,
        )
        from evolution.tree.target.connection_contract import (
            ConnectionContract,
            MetricContract,
            RoleVisibility,
        )

        base["connection"] = ConnectionContract(MetricContract("pointwise", ()), RoleVisibility(()))
        base.update(overrides)
        # The scorer's traces belong to *this* run root: the Reflector's own
        # evidence is read from `<run_root>/scorer-traces`, so whichever scorer
        # is about to run -- the rig's, one a test assigned afterwards, one
        # passed as an override -- is pointed at it here, at the last moment
        # before the campaign starts.
        scorer = base["scorer"]
        adapter = base["adapter"]
        neutral = isinstance(adapter, FakeAdapter) and adapter.probe_neutral
        if neutral:
            base["adapter"] = _ProbeNeutralAdapter(adapter)
        if isinstance(scorer, DeterministicScorer):
            scorer.probe_neutral = neutral
        if hasattr(scorer, "trace_root"):
            scorer.trace_root = self.run_root / TRACE_ROOT
        return base

    def scorer_with_identity_digest(self, digest: str) -> DeterministicScorer:
        """This rig's scorer under another identity digest: a comparable change.

        The scorer's behaviour is deliberately unchanged -- only the identity
        the campaign compares moves -- so a test can assert the refusal without
        also changing what the run would score.
        """

        other = copy.copy(self.scorer)
        other.identity = ScorerIdentity(self.scorer.identity.kind, digest)
        return other

    async def run(
        self,
        *,
        crash_after: str | None = None,
        fault_stop_during: str | None = None,
        cancel_during: str | None = None,
        **overrides: Any,
    ) -> CampaignResult | None:
        """Run the campaign; `crash_after="<event>"` cuts the power right after
        that record is durable (the `after_<event>` hook) and returns `None`.

        `fault_stop_during="<stage>"` models a fault stop: it sets the
        executor's `stopping` right after `<stage>_started` is durable, with no
        terminal decided, as `reap` does for a `_Fault`. It is not a
        suspension (nothing concurrent with a reflection can decide one). It
        patches `_Campaign.hook` for the run because an `after_*` hook takes no
        argument and cannot reach the executor. `cancel_during="<stage>"` trips
        the caller's cancellation token at the same point."""

        with pytest.MonkeyPatch.context() as patch:
            family14_admitted(patch)
            if cancel_during is not None:
                tripped = False

                def trip() -> None:
                    nonlocal tripped
                    tripped = True

                hooks = {**overrides.pop("_hooks", {}), f"after_{cancel_during}_started": trip}
                return await run_campaign(
                    self.policy, **self.kwargs(_hooks=hooks, cancelled=lambda: tripped, **overrides)
                )
            if fault_stop_during is not None:
                from evolution.tree.campaign import _Campaign

                original = _Campaign.hook

                def hook(campaign: _Campaign, name: str) -> None:
                    original(campaign, name)
                    if name == f"after_{fault_stop_during}_started":
                        campaign.stopping = True

                _Campaign.hook = hook  # type: ignore[method-assign]
                try:
                    return await run_campaign(self.policy, **self.kwargs(**overrides))
                finally:
                    _Campaign.hook = original  # type: ignore[method-assign]
            if crash_after is None:
                return await run_campaign(self.policy, **self.kwargs(**overrides))

            def crash() -> None:
                raise Crash(f"power cut after {crash_after}")

            hooks = {**overrides.pop("_hooks", {}), f"after_{crash_after}": crash}
            with pytest.raises(Crash):
                await run_campaign(self.policy, **self.kwargs(_hooks=hooks, **overrides))
            return None

    async def resume(self, **overrides: Any) -> CampaignResult:
        with pytest.MonkeyPatch.context() as patch:
            family14_admitted(patch)
            return await resume_campaign(self.policy, **self.kwargs(**overrides))

    def records(self) -> list[dict[str, Any]]:
        raw = (self.run_root / "campaign.jsonl").read_bytes()
        return [json.loads(line) for line in raw.splitlines() if line]

    def events(self) -> list[str]:
        return [record["event"] for record in self.records()]

    def measurement_records(self, measurement_id: str) -> list[dict[str, Any]]:
        """The JSON lines of one measurement journal, as the run root holds them."""

        path = self.run_root / "measurements" / measurement_id / "events.jsonl"
        return [json.loads(line) for line in path.read_bytes().splitlines() if line]

    def projection(self, *, verify: bool = False) -> dict[str, Any]:
        """The §12 status projection of the run root, read back off disk."""

        from evolution.tree.campaign import inspect_campaign

        return inspect_campaign(self.run_root, verify=verify).to_payload()

    def result_payload(self) -> dict[str, Any]:
        """The §12 result projection of the run root, read back off disk."""

        from evolution.tree.campaign_projections import inspect_view, project_result

        view = inspect_view(self.run_root)
        return project_result(view.snapshot, tries=view.tries).to_payload()

    async def reopen_read_only(self) -> CampaignResult:
        """Resume a finished campaign: it re-admits every sibling and appends nothing."""

        before = (self.run_root / "campaign.jsonl").read_bytes()
        result = await self.resume()
        assert (self.run_root / "campaign.jsonl").read_bytes() == before
        return result

    def snapshot(self) -> CampaignSnapshot:
        """The `CampaignSnapshot` of the run root, read back off disk."""

        from evolution.tree.campaign_projections import inspect_view

        return inspect_view(self.run_root).snapshot

    @property
    def root(self) -> Path:
        """`run_root` under the name Task 7's family-8 fixtures use."""

        return self.run_root

    @property
    def reflector_requests(self) -> list[Any]:
        """A `DiagnosticsRecordingReflector`'s opening plus every `read_evidence`
        response, flattened in call order (`[]` for any other reflector)."""

        reflector = self.reflector
        openings = list(getattr(reflector, "openings", ()))
        reads = getattr(reflector, "reads", ())
        return [*openings, *(content for round_reads in reads for content in round_reads.values())]


class Crash(RuntimeError):
    """A simulated power cut in the middle of a stage."""


def cut_before_the_champion_bind(
    monkeypatch: pytest.MonkeyPatch, measurement_id: str | None = None
) -> list[str]:
    """Cut the power once, after `baseline_bound` and before `measurement_bound` (#106).

    `bind_baseline` derives a fresh champion measurement between the two
    records; the first such derivation (of `measurement_id`, when given)
    raises `Crash`, every other one is the real one. Returns the ids cut.
    """

    from evolution.tree.campaign import _Campaign

    original = _Campaign.derive
    cut: list[str] = []

    def derive(self: Any, measured: str, digest: str, side: str, **kwargs: Any) -> Any:
        if side == "champion" and not cut and measurement_id in (None, measured):
            cut.append(measured)
            raise Crash("power cut")
        return original(self, measured, digest, side, **kwargs)

    monkeypatch.setattr(_Campaign, "derive", derive)
    return cut


def crash_after_the_stage_once(
    monkeypatch: pytest.MonkeyPatch, *, measurement_prefix: str
) -> list[str]:
    """Cut the power once, after a stage journal is complete and before its callback (#97).

    The first `run_stage` of a measurement whose id starts with
    `measurement_prefix` runs for real and then raises `Crash`, so the stage's
    terminals are durable but the barrier (fault rows, thresholds, completion)
    never ran. Returns the ids cut.
    """

    from evolution.tree.campaign import _Campaign

    original = _Campaign.run_stage
    cut: list[str] = []

    async def run_stage(self: Any, measurement_id: str, stage: int) -> Any:
        outcome = await original(self, measurement_id, stage)
        if not cut and measurement_id.startswith(measurement_prefix):
            cut.append(measurement_id)
            raise Crash("power cut after the stage, before its callback")
        return outcome

    monkeypatch.setattr(_Campaign, "run_stage", run_stage)
    return cut


def crash_after_the_joint_stage_once(
    monkeypatch: pytest.MonkeyPatch, *, measurement_prefix: str
) -> list[str]:
    """`crash_after_the_stage_once` for an Attempt with a fresh Parent (#97).

    Such an Attempt measures both sides in `run_joint_stage`, not `run_stage`.
    Its first joint stage whose Candidate id starts with `measurement_prefix`
    runs for real and then raises `Crash`: both sides' terminals are durable,
    but the stage callback (fault rows, thresholds) never ran. Returns the
    Candidate ids cut.
    """

    from evolution.tree.campaign import _Campaign

    original = _Campaign.run_joint_stage
    cut: list[str] = []

    async def run_joint_stage(
        self: Any, measurement_ids: tuple[str, ...], stage: int, **kwargs: Any
    ) -> Any:
        outcomes = await original(self, measurement_ids, stage, **kwargs)
        if not cut and measurement_ids[0].startswith(measurement_prefix):
            cut.append(measurement_ids[0])
            raise Crash("power cut after the joint stage, before its callback")
        return outcomes

    monkeypatch.setattr(_Campaign, "run_joint_stage", run_joint_stage)
    return cut


class RefusingAdapter(FakeAdapter):
    """A target that never lets go of an instance: every release is refused."""

    async def release(self, instance_id: str) -> None:
        raise RuntimeError("target refuses to release")


def _rewrite_first_record(path: Path, **payload_updates: object) -> None:
    """Patch the first record's payload of a journal file in place (test-only)."""

    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[0])
    record["payload"].update(payload_updates)
    lines[0] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines))


async def _finished_once(tmp_path: Path, run_name: str = "run", *, schema: str) -> Rig:
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1),
        run_name=run_name,
        schema=schema,
    )
    result = await rig.run()
    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    rig.creation_identity_payload = rig.records()[0]["payload"]["identity"]
    return rig


@pytest.fixture
async def finished_campaign(tmp_path: Path) -> Rig:
    """A `/15` (family-14) campaign run straight through to `completed`, no rebase.

    `snapshot().epochs` has exactly the creation entry: every measurement
    binds into epoch 0 (`tests/test_tree_campaign_epochs.py`).
    """

    return await _finished_once(tmp_path, schema=SCHEMA_V15)


#: One diagnosis group over the fixtures' one writable path, `test_tree_campaign.GROUP`'s
#: own shape kept as a private copy here to avoid importing that test module (it imports
#: this one): the scripted reflector fills in `evidence_ids`/`affected_cases` from the
#: catalog it is actually shown (`cite_first_exemplar`).
_FAMILY_GROUP: dict[str, Any] = {
    "root_cause": "the skill never answers the asked question",
    "suggested_fix": "state the answer in the first line",
    "counterevidence": "No selected passing observation contradicts this diagnosis.",
    "regression_risks": "Keep the existing answer format.",
    "why_this_layer": "The failure is caused by the editable skill text.",
    "evidence_ids": [],
    "affected_cases": [],
    "target_paths": ["skill.py"],
}


async def _version_rebased(tmp_path: Path, run_name: str = "run", *, schema: str) -> Rig:
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1),
        run_name=run_name,
        schema=schema,
    )

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born, incomplete champion measurement

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
    rig.creation_identity_payload = rig.records()[0]["payload"]["identity"]

    _rewrite_first_record(rig.run_root / "campaign.jsonl", producing_version="0.3.0a0")
    _rewrite_first_record(rig.run_root / "artifacts.jsonl", producing_version="0.3.0a0")
    journals = sorted((rig.run_root / "measurements").glob("*/events.jsonl"))
    assert journals  # the champion measurement is born
    for journal in journals:
        _rewrite_first_record(journal, producing_version="0.3.0a0")

    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume()
    assert [d.field for d in excinfo.value.differences] == ["producing_version"]
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "fixed a bug",
        now="2026-09-17T10:00:00Z",
    )

    result = await rig.resume(authorizations=[auth])

    assert result.terminal == "finished"
    assert rig.events().count("identity_rebased") == 1
    return rig


@pytest.fixture
async def campaign_with_version_rebase(tmp_path: Path) -> Rig:
    """A `/15` campaign resumed under a patched producing version through an
    accepted `run_identity_changed` decision -- the same path
    `tests/test_tree_campaign.py`'s
    `test_an_older_producing_version_is_admitted_by_authorization_and_siblings_follow`
    exercises, replayed under family 14. `producing_version` is
    never a comparable field (`authorizations.comparable_field` only matches
    `identity.*`), so the resulting `identity_rebased` opens no epoch.
    """

    return await _version_rebased(tmp_path, schema=SCHEMA_V15)


@pytest.fixture(scope="module")
def all_offline_campaign_fixtures(tmp_path_factory):
    """Native /15 runs, one straight and one resumed after a version authorization."""
    root = tmp_path_factory.mktemp("offline-campaigns")

    async def build():
        return [
            await _finished_once(root / "native", schema=SCHEMA_V15),
            await _version_rebased(root / "native-rebased", schema=SCHEMA_V15),
        ]

    return asyncio.run(build())


def family13_document(
    document: Mapping[str, Any], *, gate: Mapping[str, Any] | None = None, repeats: int = 3
) -> dict[str, Any]:
    """A `/13` workspace document moved to `/14` (as `test_tree_manifest_v14.v14_document`).

    The gate is built in this order, each step overriding the one before it:
    1. the base: `OBSERVE_GATE` when `gate.get("mode") == "observe"`, else
       `STATISTICAL_GATE` with `global: "off"` (F5 and F6 read `global`, F4 does not);
    2. `provenance` and `fresh_parent` copied from the `/13` document's gate;
    3. `gate`, applied per top-level key (`causal`, `focus` and `fresh_parent`
       each replace the whole value).
    `campaign.alpha` and `gate.regression_margin` are dropped, and
    `campaign.evaluation.repeats_per_question` is `repeats`.
    """

    from tests.campaign_drivers import OBSERVE_GATE, STATISTICAL_GATE

    moved = copy.deepcopy(dict(document))
    campaign = moved["campaign"]
    old = campaign["gate"]
    observe = (gate or {}).get("mode") == "observe"
    built = copy.deepcopy(OBSERVE_GATE if observe else {**STATISTICAL_GATE, "global": "off"})
    built.update(provenance=copy.deepcopy(old["provenance"]), fresh_parent=old["fresh_parent"])
    built.update(copy.deepcopy(dict(gate or {})))
    moved["schema"] = "evolution-v2-manifest/14"
    campaign.pop("alpha", None)
    campaign["evaluation"] = {"repeats_per_question": repeats}
    campaign["gate"] = built
    return moved


class Family14Life(LifecycleFixture):
    """Explicit family-14 lifecycle fixture with an opt-in pre-look bridge.

    `transcode=True` selects synthetic policy controls on a native family-14
    prefix and renews its identity digest. Pre-look comparison bindings gain
    the declared V11 margin. Completed looks and calibrations are refused;
    append genuine family-14 records after the bridge. Other records and
    native connection bindings are unchanged. H3 settings are explicit.
    """

    def __init__(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        transcode: bool = False,
        stability: str = "unstable",
        round_stop: str = "all_attempts",
        refresh: int = 0,
        margin: float = 0,
        **policy: Any,
    ) -> None:
        rows = copy.deepcopy([dict(record) for record in records])
        if transcode:
            if any(
                row["event"] in {"comparison_look_evaluated", "noise_calibration_completed"}
                for row in rows
            ):
                raise ValueError(
                    "family-14 fixture conversion stops before completed gate evidence"
                )
            created = rows[0]["payload"]
            if created["family_version"] != 14:
                raise ValueError("family-14 lifecycle fixtures require a native family-14 prefix")
            identity = created["identity"]
            declared = copy.deepcopy(identity["policy"])
            if policy:
                mode = policy.pop("mode", "statistical")
                declared["evaluation"]["repeats_per_question"] = policy.pop(
                    "repeats", declared["evaluation"]["repeats_per_question"]
                )
                gate = declared["gate"]
                gate.update(
                    mode=mode,
                    **{
                        "global": policy.pop("global_mode", None)
                        or ("off" if mode == "observe" else "point")
                    },
                )
                gate["fresh_parent"] = policy.pop("fresh_parent", gate["fresh_parent"])
                gate["causal"] = {"tolerance": policy.pop("tolerance", "measured_noise")}
                if mode == "statistical":
                    gate["focus"] = {"confidence": 0.9}
                    gate["causal"]["confidence"] = 0.9
                else:
                    gate.pop("focus", None)
                if policy:
                    raise ValueError(f"unknown synthetic policy settings: {sorted(policy)}")
            built = CampaignPolicyV11.from_payload(
                with_final_controls(
                    declared,
                    stability=stability,
                    round_stop=round_stop,
                    refresh=refresh,
                    margin=margin,
                )
            )
            identity.update(policy=built.to_payload(), policy_digest=built.digest)
            created["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
            for row in rows:
                if row["event"] == "comparison_bound":
                    row["payload"]["margin"] = margin
        self._initialize(rows, 14)

    def comparison_payload(self, attempt_id: str, **overrides: Any) -> dict[str, Any]:
        payload = super().comparison_payload(attempt_id, margin=self.state.policy.focus_margin)
        payload.update(overrides)
        return payload

    def look_payload(self, attempt_id: str, look: int, **overrides: Any) -> dict[str, Any]:
        """Synthetic published values in family 14's grammar, never row evidence."""
        from evolution.tree.campaign_schema import look_caveats, look_decision, look_tolerance
        from evolution.tree.final_statistics import would_pass_point14

        payload = super().look_payload(attempt_id, look)
        attempt = self.attempt(attempt_id)
        comparison = attempt.comparison
        assert comparison is not None
        payload["tolerance"] = look_tolerance(
            comparison, attempt.calibration_result, family_version=14
        )
        for scope in ("focus", "causal"):
            value = payload[scope]
            if value is not None:
                value["exact"] = None if value["level"] is None else False
        decision = look_decision(
            comparison,
            final=payload["final"],
            focus=payload["focus"],
            causal=payload["causal"],
            tolerance=payload["tolerance"]["value"],
            family_version=14,
        )
        payload.update(decision=decision.decision, reason=decision.reason)
        if comparison["mode"] == "observe":
            payload["would_pass_point"] = would_pass_point14(
                payload["focus"]["point"],
                None if payload["causal"] is None else payload["causal"]["point"],
                payload["tolerance"]["value"],
                margin=comparison["margin"],
            )
        payload.update(overrides)
        if "caveats" not in overrides:
            payload["caveats"] = look_caveats(
                comparison,
                payload["tolerance"],
                attempt.calibration_result,
                family_version=14,
                focus=payload["focus"],
                causal=payload["causal"],
            )
        return payload


def family14_document(
    document: Mapping[str, Any],
    *,
    gate: Mapping[str, Any] | None = None,
    repeats: int = 3,
    stability: str = "unstable",
    round_stop: str = "all_attempts",
    refresh: int = 0,
    margin: float = 0,
    grouper: Mapping[str, Any] | None = None,
    metric_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit H3 `/15` fixture, with connection/adaptation declarations together.

    `grouper`, when given, is the `/15` `grouper` section (H8); by default the
    document's `reflector.grouping` moves there, as before.

    H10 (plan choice 18): the default `metric_contract` declares no Target
    field (`PROBE_NEUTRAL_CONTRACT`, valid per C§3), so the connection probe
    every family-14 run now makes passes on `FakeInstance`'s `message` events
    and existing trajectories keep their bytes. A probe test passes a real one.
    """
    from tests.tree.manifest.test_manifest_v15 import v15_from

    base = family13_document(document, gate=gate, repeats=repeats)
    policy = with_final_controls(
        base["campaign"], stability=stability, round_stop=round_stop, refresh=refresh, margin=margin
    )
    return v15_from(
        base,
        gate=policy["gate"],
        repeats=repeats,
        round_stop=round_stop,
        refresh=refresh,
        grouper=None if grouper is None else copy.deepcopy(dict(grouper)),
        metric_contract=dict(
            PROBE_NEUTRAL_CONTRACT if metric_contract is None else metric_contract
        ),
    )


#: H10 plan choice 18: a family-14 metric contract with no Target field.
PROBE_NEUTRAL_CONTRACT: Mapping[str, Any] = MappingProxyType(
    {"version": "metric_contract/1", "kind": "pointwise", "target_fields": []}
)
