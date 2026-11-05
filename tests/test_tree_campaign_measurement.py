"""Task 5: the measurement layer - journals, instances, stages, accounting."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign_measurement
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.campaign_identity import (
    CampaignIdentityV1,
    build_identity,
    case_subset_for,
    schedule_for,
    schedule_for_ids,
)
from evolution.tree.campaign_identity import (
    derived_identity as native_derived_identity,
)
from evolution.tree.campaign_identity import (
    measurement_identity as native_measurement_identity,
)
from evolution.tree.campaign_measurement import (
    Measurement,
    MeasurementError,
    StagePlan,
    stage_plans,
)
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_statistics import stage_horizon
from evolution.tree.execution.config import EvaluationPolicy
from evolution.tree.execution.interaction import AttestationError, EvalCase, SemanticIdentity
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1
from evolution.tree.execution.journal_schema import components_digest
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalFault,
    JournalRecoveryRequired,
)
from tests.campaign_fakes import (
    PRIMARY,
    STRATEGY,
    DeterministicScorer,
    FakeAdapter,
    FakeInstance,
    ScriptedEnvironment,
    artifact,
    corpus,
    policy,
    v5_policy,
    v9_policy,
    v11_policy,
)

CAMPAIGN_RUN_ID = "campaign-run"
MEASUREMENT_ID = "m-r00-champion"


def v7_payload(**overrides):
    """Raw call-retry policy blueprint used by the isolated measurement tests."""
    from tests.test_tree_campaign_identity import policy_payload

    payload = policy_payload(attempt_feedback="none")
    payload["budget"] = {"max_turns": 8, "timeout_seconds": 120, "max_failed_turns": 3}
    payload.update(overrides)
    return payload


def measurement_identity(*args, **kwargs):
    """Shared S1 fixture: execute without campaign connection admission.

    Policies are native, but these isolated measurements deliberately exercise
    the public unbound S1 dispatch contract, as standalone evaluation does.
    Current campaign measurements still bind/require their connection token.
    """
    payload = native_measurement_identity(*args, **kwargs).to_payload()
    payload.pop("connection_binding")
    return RunIdentityV1.from_payload(payload)


def derived_identity(*args, **kwargs):
    """Derive the same unbound shared-S1 identity without a corpus."""
    payload = native_derived_identity(*args, **kwargs).to_payload()
    payload.pop("connection_binding")
    return RunIdentityV1.from_payload(payload)


@dataclass
class Rig:
    """One campaign identity plus everything `run_stage` needs to be called."""

    run_root: Path
    ref: ArtifactRef
    adapter: FakeAdapter
    environment: ScriptedEnvironment
    scorer: DeterministicScorer
    policy: EvaluationPolicy
    cases: tuple[EvalCase, ...]
    identity: CampaignIdentityV1
    expected: SemanticIdentity

    def measurement(
        self, measurement_id: str = MEASUREMENT_ID, side: str = "champion"
    ) -> Measurement:
        return Measurement.declare(
            run_root=self.run_root,
            campaign_run_id=CAMPAIGN_RUN_ID,
            measurement_id=measurement_id,
            identity=measurement_identity(
                self.identity,
                self.cases,
                self.ref.digest,
                side,  # type: ignore[arg-type]
            ),
            corpus=self.cases,
        )

    def subset_measurement(
        self,
        case_ids: Sequence[str],
        measurement_id: str = "m-r01-a01",
        side: str = "candidate",
        ref: ArtifactRef | None = None,
    ) -> Measurement:
        """A measurement over `case_ids` only (G2 spec §3.1), in corpus order.

        `ref` is the artifact it measures, the rig's own by default.
        """

        return Measurement.declare(
            run_root=self.run_root,
            campaign_run_id=CAMPAIGN_RUN_ID,
            measurement_id=measurement_id,
            identity=measurement_identity(
                self.identity,
                self.cases,
                (ref or self.ref).digest,
                side,  # type: ignore[arg-type]
                case_subset=case_subset_for(self.identity, case_ids),
            ),
            corpus=self.cases,
        )

    def family12_identity(self, side: str = "candidate") -> RunIdentityV1:
        """The full-corpus measurement identity with the family-12 marker (G2b2).

        The b1 tests get `row_provenance` by building the rig over a `/13`
        policy (`v9_policy()`), where `measurement_identity` derives it. A
        rig over any policy gets the same marker here, so a test of a
        family-12-only record need not rebuild the rig.
        """

        payload = measurement_identity(
            self.identity,
            self.cases,
            self.ref.digest,
            side,  # type: ignore[arg-type]
        ).to_payload()
        return RunIdentityV1.from_payload({**payload, "row_provenance": 1})

    def plans(
        self, side: str = "champion", *, cases: Sequence[EvalCase] | None = None
    ) -> tuple[StagePlan, ...]:
        # Shared S1 fixtures declare arbitrary stage cuts, independently of
        # the native campaign policy's confidence-decision schedule.
        looks = {6: (3, 6), 10: (3, 6, 10)}.get(self.policy.repeats, self.policy.looks)
        return campaign_measurement._partition(
            looks,
            schedule_for(
                self.policy, self.cases if cases is None else cases, self.adapter.seed_mode, side
            ),
        )

    def stage_kwargs(self) -> dict[str, Any]:
        return dict(
            adapter=self.adapter,
            candidate=self.ref,
            strategy=STRATEGY,
            environment=self.environment,
            scorer=self.scorer,
            policy=self.policy,
            expected=self.expected,
            cancelled=lambda: False,
        )


async def build_rig(
    tmp_path: Path,
    *,
    campaign_policy: CampaignPolicyV11 | None = None,
    adapter: FakeAdapter | None = None,
    scorer: DeterministicScorer | None = None,
    cases: tuple[EvalCase, ...] | None = None,
) -> Rig:
    run_root = tmp_path / "run"
    run_root.mkdir()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    ref = artifact(candidate_root)
    campaign_policy = campaign_policy or policy()
    adapter = adapter or FakeAdapter()
    scorer = scorer or DeterministicScorer()
    cases = cases if cases is not None else corpus(2)
    environment = ScriptedEnvironment()
    preflight = await adapter.preflight(ref.expected_attestation())
    store = ArtifactStoreIdentityV1(ref._policy, ref.digest, ref.dependency_digest)
    identity = build_identity(
        campaign_policy,
        preflight,
        environment.identity,
        scorer,
        EditorIdentity("fake", "editor-v1"),
        cases,
        store,
        STRATEGY,
        None,
        reflector=ReflectorIdentity("fake", "9" * 64),
        environment_docs_digest=None,
        probe=None,
    )
    return Rig(
        run_root=run_root,
        ref=ref,
        adapter=adapter,
        environment=environment,
        scorer=scorer,
        # These component tests execute the standalone/unbound measurement
        # contract; the native campaign policy above supplies the same frozen
        # schedule/settings, not campaign connection or call-retry admission.
        policy=EvaluationPolicy(
            repeats=campaign_policy.repeats,
            random_seed=campaign_policy.random_seed,
            max_concurrency=campaign_policy.max_concurrency,
            max_scorer_concurrency=campaign_policy.max_scorer_concurrency,
            metric_primary=campaign_policy.metric_primary,
            budget=(
                campaign_policy.budget
                if campaign_policy.budget.call_retries.max_attempts > 1
                or campaign_policy.budget.call_retries.turn_max_attempts > 1
                else replace(campaign_policy.budget, call_retries=None)
            ),
            sample_retries=campaign_policy.sample_retries,
        ),
        cases=cases,
        identity=identity,
        expected=preflight.semantic,
    )


def two_looks() -> CampaignPolicyV11:
    return policy(alpha={"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}})


def keys(rows: Any) -> list[tuple[str, str]]:
    return sorted((row["case_id"], row["sample_id"]) for row in rows)


def journal_bytes(rig: Rig, measurement_id: str = MEASUREMENT_ID) -> bytes:
    return (rig.run_root / "measurements" / measurement_id / "events.jsonl").read_bytes()


def byte_range(locator: str) -> tuple[int, int]:
    start, end = locator.rsplit("bytes=", 1)[1].split(":")
    return int(start), int(end)


async def wait_for_event(
    rig: Rig, event: str, *, count: int, measurement_id: str = MEASUREMENT_ID
) -> None:
    """Poll the journal file until `event` has been appended `count` times."""

    needle = f'"event":"{event}"'.encode()
    deadline = time.monotonic() + 5.0
    while journal_bytes(rig, measurement_id).count(needle) < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"{event} was not journaled {count} times")
        await asyncio.sleep(0.001)


# -- stage plans ----------------------------------------------------------


def test_stage_plans_partition_the_frozen_schedule_by_look() -> None:
    campaign_policy = two_looks()
    cases = corpus(2)
    plans = campaign_measurement._partition(
        (3, 6), schedule_for(campaign_policy, cases, "exact", "champion")
    )

    assert [(plan.stage, plan.look) for plan in plans] == [(1, 3), (2, 6)]
    assert [sorted({row["repeat"] for row in plan.rows}) for plan in plans] == [
        [1, 2, 3],
        [4, 5, 6],
    ]
    assert keys(row for plan in plans for row in plan.rows) == keys(
        schedule_for(campaign_policy, cases, "exact", "champion")
    )


def test_stage_plans_of_a_single_look_hold_the_whole_schedule() -> None:
    campaign_policy = policy()
    cases = corpus(2)
    plans = stage_plans(campaign_policy, cases, "exact", "candidate")

    assert plans == (
        StagePlan(1, 3, tuple(schedule_for(campaign_policy, cases, "exact", "candidate"))),
    )


def stage_plans_from_rows(policy, rows):
    """The production partition over a given schedule (test helper)."""

    from evolution.tree.campaign_measurement import _partition

    return _partition(policy.looks, rows)


def test_the_last_stage_takes_every_remaining_row() -> None:
    policy = v11_policy(fresh_parent=True, mode="observe", repeats=1)
    rows = schedule_for_ids(policy, ["c1", "c2"], "exact", "champion", case_repeats={"c2": 2})
    (plan,) = stage_plans_from_rows(policy, rows)
    assert [(r["case_id"], r["repeat"]) for r in plan.rows] == [("c1", 1), ("c2", 1), ("c2", 2)]
    assert plan.look == 1 and plan.horizon == 2
    assert plan.horizon == stage_horizon(policy.looks, plan.look, 2)


def test_without_case_repeats_every_plan_keeps_its_rows_and_its_horizon_is_its_look() -> None:
    policy = v11_policy(fresh_parent=True, mode="statistical", repeats=10)  # looks (3, 5, 10)
    plans = stage_plans(policy, corpus(2), "exact", "candidate")
    assert [p.look for p in plans] == [p.horizon for p in plans] == [3, 5, 10]
    assert [p.horizon for p in plans] == [stage_horizon(policy.looks, p.look, 10) for p in plans]
    rows = schedule_for(policy, corpus(2), "exact", "candidate")
    assert stage_plans_from_rows(policy, rows) == plans


def test_stage_horizon_reads_past_the_last_look_only() -> None:
    assert stage_horizon((3, 5, 10), 5, 10) == 5
    assert stage_horizon((3, 5, 10), 10, 10) == 10
    assert stage_horizon((1,), 1, 2) == 2
    # Never less than the look, even over an unscheduled (0) or shorter schedule.
    assert stage_horizon((3, 5, 10), 10, 0) == 10
    assert stage_horizon((3, 5, 10), 10, 7) == 10


async def test_a_resumed_case_repeats_last_stage_reads_the_whole_stage_to_its_horizon(
    tmp_path: Path,
) -> None:
    """Design §5 through the campaign's stage helpers, over a real S1 journal.

    An R=1 measurement with `case_repeats` [[1, 2]]: its one stage plans
    case-01 r1, case-02 r1 and case-02 r2 (horizon 2). An earlier session ran
    case-02's rows, r2 exhausting; the resumed dispatch owes case-01 r1 alone,
    a subset whose own horizon is 1. The finished outcome still carries the
    stage's horizon, so `attempt_stage`'s `exhausted_rows(outcome.horizon)`
    sees the exhausted r2 it lists as indeterminate.
    """

    from types import SimpleNamespace

    from evolution.tree.campaign import _full_outcome, _remaining, _unfilled_slots
    from evolution.tree.campaign_measurement import _partition

    adapter = FakeAdapter(dispositions={})
    one_look = policy(alpha={"budget": "0.05", "spend": {"1": "0.05"}})
    rig = await build_rig(tmp_path, campaign_policy=one_look, adapter=adapter, cases=corpus(2))
    identity = measurement_identity(
        rig.identity, rig.cases, rig.ref.digest, "champion", case_repeats=[[1, 2]]
    )
    measurement = Measurement.declare(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        measurement_id=MEASUREMENT_ID,
        identity=identity,
        corpus=rig.cases,
    )
    measurement.create()
    try:
        (plan,) = _partition(one_look.looks, identity.to_payload()["schedule"])
        assert plan.horizon == measurement.max_repeat == 2
        # Before any try: every planned row is owed, the extra repeat included,
        # and each holds an unfilled slot of the opened stage.
        assert _remaining(measurement, plan).rows == plan.rows
        snapshot = SimpleNamespace(
            measurements={MEASUREMENT_ID: {"stages": [{"stage": 1, "look": 1}]}},
            accounted=set(),
            retries_declined=set(),
            listing_restored=frozenset(),
            family_version=14,
        )
        assert _unfilled_slots(snapshot, MEASUREMENT_ID, measurement, one_look.looks) == 3

        adapter.dispositions[(measurement.candidate_digest, "case-02", "02")] = "time_budget"
        earlier = StagePlan(1, 1, tuple(row for row in plan.rows if row["case_id"] == "case-02"))
        await measurement.run_stage(earlier, **rig.stage_kwargs())

        remaining = _remaining(measurement, plan)
        assert [(row["case_id"], row["repeat"]) for row in remaining.rows] == [("case-01", 1)]
        assert remaining.horizon == 1
        dispatched = await measurement.run_stage(remaining, **rig.stage_kwargs())
        outcome = _full_outcome(measurement, plan, one_look, dispatched)

        assert outcome.horizon == plan.horizon == 2
        assert [(row["case_id"], row["sample_id"]) for row in outcome.indeterminate] == [
            ("case-02", "02")
        ]
        exhausted = {
            (row.case_id, row.sample_id) for row in measurement.exhausted_rows(outcome.horizon)
        }
        assert all((row["case_id"], row["sample_id"]) in exhausted for row in outcome.indeterminate)
    finally:
        measurement.close()


async def test_a_started_try_after_a_declined_one_holds_no_stage_slot(tmp_path: Path) -> None:
    """Pre-F8 spec §2.4: a try started after a declined one is the D1 retry a
    listing's grant restored, which takes the grant's slot, not a stage slot.

    case-01/01's try 0 fails and its D1 retry (try 1) is cut mid-flight, so the
    row's latest try is a `rollout_started` at ordinal 1. Without `declined`
    it holds an unfilled slot; with try 0 declined it does not.
    """

    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=1),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = [
        "target_failed",
        "slow",
    ]
    task = asyncio.ensure_future(measurement.run_stage(rig.plans()[0], **rig.stage_kwargs()))
    try:
        deadline = time.monotonic() + 5.0
        while (
            measurement.sample_rows(1)[0].retry_ordinal != 1
            or measurement.sample_rows(1)[0].disposition != "not_started"
        ):
            assert time.monotonic() < deadline, "try 1 never started"
            await asyncio.sleep(0.001)
        # Read while try 1 is in flight: cancelling it writes its terminal.
        execution, _scoring = measurement._require_journal().sample_records("case-01", "01")
        assert execution is not None and execution.event == "rollout_started"
        assert execution.payload["retry_ordinal"] == 1
        look = measurement.max_repeat
        counted = measurement.unfilled_slots(look, ())
        assert measurement.unfilled_slots(look, (), declined=()) == counted
        assert measurement.unfilled_slots(look, (), declined={("case-01", "01", 0)}) == counted - 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        measurement.close()


# -- declare, classify, create, unborn, born ------------------------------


async def test_declare_touches_no_disk_and_classifies_absent(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    measurement = rig.measurement()

    assert not (rig.run_root / "measurements").exists()
    assert Measurement.classify(rig.run_root, MEASUREMENT_ID) == "absent"
    assert measurement.measurement_id == MEASUREMENT_ID
    assert measurement.path == rig.run_root / "measurements" / MEASUREMENT_ID


async def test_create_after_declare_is_born_and_refuses_removal(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    measurement = rig.measurement()
    measurement.create()
    try:
        assert Measurement.classify(rig.run_root, MEASUREMENT_ID) == "born"
        with pytest.raises(MeasurementError) as refusal:
            Measurement.remove_unborn(rig.run_root, MEASUREMENT_ID)
        assert refusal.value.code == "measurement_born"
        assert (rig.run_root / "measurements" / MEASUREMENT_ID / "events.jsonl").exists()
    finally:
        measurement.close()


async def test_unborn_measurement_is_removed_and_created_again(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    directory = rig.run_root / "measurements" / MEASUREMENT_ID
    directory.mkdir(parents=True)
    # A creation that died between making the directory and fsyncing its first
    # record leaves bytes with no complete `run_created`.
    (directory / "events.jsonl").write_bytes(b'{"schema_version":1,"run_id"')

    assert Measurement.classify(rig.run_root, MEASUREMENT_ID) == "unborn"

    Measurement.remove_unborn(rig.run_root, MEASUREMENT_ID)
    assert Measurement.classify(rig.run_root, MEASUREMENT_ID) == "absent"

    measurement = rig.measurement()
    measurement.create()
    try:
        assert Measurement.classify(rig.run_root, MEASUREMENT_ID) == "born"
    finally:
        measurement.close()


async def test_remove_unborn_clears_a_partial_birth(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    measurement = rig.measurement()
    directory = Measurement.directory_for(rig.run_root, measurement.measurement_id)
    directory.mkdir(parents=True)
    (directory / "events.jsonl.partial").write_bytes(b"{}\n")
    assert Measurement.classify(rig.run_root, measurement.measurement_id) == "unborn"
    Measurement.remove_unborn(rig.run_root, measurement.measurement_id)
    assert Measurement.classify(rig.run_root, measurement.measurement_id) == "absent"


def test_remove_unborn_of_an_absent_measurement_is_a_no_op(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()

    Measurement.remove_unborn(run_root, MEASUREMENT_ID)

    assert Measurement.classify(run_root, MEASUREMENT_ID) == "absent"
    assert not (run_root / "measurements" / MEASUREMENT_ID).exists()


# -- run_stage ------------------------------------------------------------


async def test_run_stage_runs_every_row_to_a_terminal_within_its_instance_budget(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(max_instances=4, max_concurrent_sessions=2)
    rig = await build_rig(tmp_path, campaign_policy=policy(max_concurrency=4), adapter=adapter)
    measurement = rig.measurement()
    measurement.create()
    try:
        plan = rig.plans()[0]
        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        assert (outcome.stage, outcome.look) == (1, 3)
        assert outcome.terminals == len(plan.rows) == 6
        assert not outcome.indeterminate and not outcome.faults
        assert not outcome.critical_failed and outcome.leaked == ()
        # ceil(max_concurrency / max_concurrent_sessions) instances, never more.
        assert adapter.peak_live == 2 <= adapter.capabilities.max_instances
        assert measurement.complete()
        rows = measurement.sample_rows(3)
        assert len(rows) == 6
        assert {row.disposition for row in rows} == {"score"}
        assert all(row.metrics is not None and PRIMARY in row.metrics for row in rows)
        assert {row.boot_id for row in rows} == {"boot-1", "boot-2"}
        assert [row.effective_seed for row in rows] == [row.requested_seed for row in rows]
        assert {row.dependency_digest for row in rows} == {rig.ref.dependency_digest}
        # The measurement's last stage releases its instances, but what they
        # attested outlives them: the executor writes `instance_attested` from it.
        assert sorted(adapter.released) == sorted(adapter.materialized)
        assert not adapter.live and not measurement.instances
        assert set(measurement.attested) == set(adapter.materialized)
        assert dict(outcome.attested) == dict(measurement.attested)
        for instance_id, identity in outcome.attested.items():
            assert identity.instance.loaded_candidate_digest == rig.ref.digest
            assert identity.instance.boot_id == adapter.created[instance_id].boot_id
    finally:
        measurement.close()


async def test_run_stage_keeps_the_verified_identity_of_every_instance(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert set(measurement.instances) == set(adapter.live)
        for instance_id, identity in measurement.instances.items():
            assert identity.instance.loaded_candidate_digest == rig.ref.digest
            assert identity.semantic.dependency_digest == rig.ref.dependency_digest
            assert identity.instance.boot_id == adapter.live[instance_id].boot_id
            assert instance_id.startswith(f"{CAMPAIGN_RUN_ID}-")
    finally:
        measurement.close()


async def test_a_queued_sample_keeps_its_whole_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permit is taken before `durable_rollout`, so waiting costs no budget."""

    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(
        tmp_path,
        campaign_policy=policy(max_concurrency=1, budget={"max_turns": 4, "timeout_seconds": 0.5}),
        adapter=adapter,
        cases=corpus(1),
    )
    adapter.dispositions[(rig.ref.digest, "case-01", "01")] = "time_budget"

    spans: list[tuple[str, float, float]] = []
    original = campaign_measurement.durable_rollout

    async def timed(**kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return await original(**kwargs)
        finally:
            spans.append((kwargs["sample"].sample_id, started, time.monotonic()))

    monkeypatch.setattr(campaign_measurement, "durable_rollout", timed)

    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        rows = {row.sample_id: row for row in measurement.sample_rows(3)}

        assert outcome.terminals == 3
        assert (rows["01"].disposition, rows["01"].outcome) == ("indeterminate", "time_budget")
        # A stalled sibling never eats a queued sample's deadline.
        assert rows["02"].disposition == "score" and rows["03"].disposition == "score"
        spans.sort(key=lambda span: span[1])
        assert [span[0] for span in spans] == ["01", "02", "03"]
        assert all(spans[i][2] <= spans[i + 1][1] for i in range(len(spans) - 1)), spans
    finally:
        measurement.close()


async def test_a_time_budget_sample_never_cancels_its_siblings(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    rig = await build_rig(
        tmp_path,
        campaign_policy=policy(max_concurrency=3, budget={"max_turns": 4, "timeout_seconds": 0.5}),
        adapter=adapter,
        cases=corpus(1),
    )
    adapter.dispositions[(rig.ref.digest, "case-01", "02")] = "time_budget"

    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert outcome.terminals == 3
        assert not measurement.complete()
        assert [
            (row["case_id"], row["sample_id"], row["owner"], row["reason"])
            for row in outcome.indeterminate
        ] == [("case-01", "02", "rollout", "rollout_time_budget_exhausted")]
        assert {row.sample_id: row.disposition for row in measurement.sample_rows(3)} == {
            "01": "score",
            "02": "indeterminate",
            "03": "score",
        }
    finally:
        measurement.close()


async def test_fault_rows_name_infrastructure_and_missing_metric_samples(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    scorer = DeterministicScorer(missing_primary_for=frozenset({"case-02"}))
    rig = await build_rig(
        tmp_path,
        campaign_policy=policy(max_concurrency=2),
        adapter=adapter,
        scorer=scorer,
        cases=corpus(2),
    )
    adapter.dispositions[(rig.ref.digest, "case-01", "01")] = "infrastructure_failed"

    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert outcome.terminals == 6
        faults = {(row["case_id"], row["sample_id"]): row["reason"] for row in outcome.faults}
        assert faults == {
            ("case-01", "01"): "infrastructure_failed",
            ("case-02", "01"): "scorer_metric_missing",
            ("case-02", "02"): "scorer_metric_missing",
            ("case-02", "03"): "scorer_metric_missing",
        }
        assert measurement.fault_rows(PRIMARY) == outcome.faults
        assert not measurement.complete()
        # An `exclude` sample is a fault, never an accounted indeterminate.
        assert not outcome.indeterminate
        assert all(row["reference"].kind == "journal_record" for row in outcome.faults)
    finally:
        measurement.close()


async def test_a_critical_failure_is_reported_by_the_stage(tmp_path: Path) -> None:
    rig = await build_rig(
        tmp_path,
        scorer=DeterministicScorer(critical={"case-01": False}),
        cases=corpus(1, critical=("case-01",)),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert outcome.critical_failed
        assert all(row.critical_pass is False for row in measurement.sample_rows(3))
    finally:
        measurement.close()


async def test_a_later_stage_reuses_the_instances_of_the_first(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        first, second = rig.plans()
        await measurement.run_stage(first, **rig.stage_kwargs())

        assert len(adapter.materialized) == 1 and not adapter.released
        rows = measurement.sample_rows(6)
        assert len(rows) == 6
        assert [row.disposition for row in rows[3:]] == ["not_started"] * 3
        assert [row.outcome for row in rows[3:]] == [None] * 3
        assert [row.metrics for row in rows[3:]] == [None] * 3
        assert not measurement.complete()

        await measurement.run_stage(second, **rig.stage_kwargs())

        assert len(adapter.materialized) == 1
        assert adapter.released == adapter.materialized
        assert measurement.complete()
    finally:
        measurement.close()


async def test_a_failed_attestation_releases_the_instance_and_starts_no_sample(
    tmp_path: Path,
) -> None:
    class Misattesting(FakeAdapter):
        def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
            return FakeInstance(
                instance_id,
                boot_id,
                self._semantic_for(candidate.dependency_digest),
                "0" * 64,
                self.dispositions,
            )

    adapter = Misattesting(max_instances=2, max_concurrent_sessions=2)
    rig = await build_rig(tmp_path, adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        with pytest.raises(AttestationError):
            await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert adapter.released == adapter.materialized
        assert not measurement.instances
        assert {row.disposition for row in measurement.sample_rows(3)} == {"not_started"}
    finally:
        measurement.close()


async def test_cancellation_excludes_samples_without_naming_a_fault(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(
            rig.plans()[0], **(rig.stage_kwargs() | {"cancelled": lambda: True})
        )

        assert outcome.terminals == 3
        assert not outcome.faults and not outcome.indeterminate
        rows = measurement.sample_rows(3)
        assert {row.outcome for row in rows} == {"cancelled"}
        assert {row.disposition for row in rows} == {"exclude"}
    finally:
        measurement.close()


async def test_a_second_run_of_the_same_stage_reuses_its_durable_terminals(
    tmp_path: Path,
) -> None:
    """Resume re-opens a partly complete stage; nothing durable is executed twice."""

    rig = await build_rig(tmp_path, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        plan = rig.plans()[0]
        await measurement.run_stage(plan, **rig.stage_kwargs())
        first = journal_bytes(rig)

        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        assert outcome.terminals == 3
        second = journal_bytes(rig)
        assert second.startswith(first)
        assert b"rollout_started" not in second[len(first) :]
    finally:
        measurement.close()


# -- accounting, references, release, resume ------------------------------


async def test_release_all_reports_the_ids_the_target_refused(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        stale = tuple(measurement.instances)
        adapter.refuse_release = frozenset(stale)

        leaked = await measurement.release_all(adapter)

        assert leaked == stale
        assert not measurement.instances
        assert await measurement.release_all(adapter) == ()
    finally:
        measurement.close()


async def test_release_all_finishes_the_release_then_raises_the_journal_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy journal's release-time fault is the caller's to classify.

    The journal-free release exists for a writer that is *already* unusable. A
    fault raised while the journal was healthy still has to reach the caller —
    at a measurement's last look that is `run_stage`'s own boundary, and so the
    `persistence_failed` terminal — but not before every instance is back: §7
    lets no terminal leave one live. So the release finishes through the adapter
    and the first fault is raised after it.
    """

    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        live = tuple(measurement.instances)
        assert live

        original = Journal.append

        def faulting(self: Any, event: str, payload: dict[str, Any], *a: Any, **kw: Any) -> int:
            if event == "target_release_started":
                raise JournalFault("journal_unavailable")
            return original(self, event, payload, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Journal, "append", faulting)

        with pytest.raises(JournalFault) as caught:
            await measurement.release_all(adapter)

        assert caught.value.code == "journal_unavailable"
        # Raised only after the target had every instance back, unjournaled.
        assert set(adapter.released) == set(live)
        assert not adapter.live
        assert not measurement.instances
        assert set(measurement.released_unjournaled) == set(live)
        assert not measurement.leaked
    finally:
        monkeypatch.undo()
        measurement.close()


async def test_resume_releases_stale_instances_before_anything_else(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
    owned = tuple(measurement.instances)
    measurement.close()

    restarted = rig.measurement()
    released = await restarted.resume(adapter)
    try:
        assert released == owned
        assert not restarted.instances
        assert restarted.status()["owned_instances"] == []
        # The measurement keeps working after recovery: the skipped stage runs.
        await restarted.run_stage(rig.plans()[1], **rig.stage_kwargs())
        assert restarted.complete()
    finally:
        restarted.close()


async def test_resume_closes_pending_calls_before_releasing_their_instance(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(tmp_path, adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    await measurement.materialize(adapter, rig.ref, STRATEGY, rig.policy)
    owned = tuple(measurement.instances)
    instance_id = owned[0]
    row = rig.plans()[0].rows[0]
    sample = {
        "case_id": row["case_id"],
        "sample_id": row["sample_id"],
        "candidate_digest": measurement.candidate_digest,
        # Native policies declare sample retries (PDR-0011).
        "retry_ordinal": 0,
    }
    journal = measurement._require_journal()
    journal.append("rollout_started", sample, "pending-rollout")
    journal.append(
        "target_identity_started",
        {**sample, "instance_id": instance_id, "retry_ordinal": 0},
        "pending-identity",
    )
    measurement.close()

    restarted = rig.measurement()
    try:
        assert await restarted.resume(adapter) == owned
        events = [json.loads(line)["event"] for line in journal_bytes(rig).splitlines()]
        assert events.index("operation_interrupted") < events.index("rollout_interrupted")
        assert events.index("rollout_interrupted") < events.index("target_release_started")
        assert restarted.status()["owned_instances"] == []
        assert restarted.sample_rows(3)[0].disposition == "indeterminate"
    finally:
        restarted.close()

    prefix = journal_bytes(rig)
    again = rig.measurement()
    try:
        assert await again.resume(adapter) == ()
        assert journal_bytes(rig) == prefix
    finally:
        again.close()


async def test_both_reference_kinds_round_trip_against_the_journal_file(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        raw = journal_bytes(rig)

        prefix = measurement.prefix_reference()
        assert prefix.kind == "journal_prefix"
        assert prefix.run_id == MEASUREMENT_ID
        assert prefix.locator == f"measurements/{MEASUREMENT_ID}/events.jsonl#bytes=0:{len(raw)}"
        assert prefix.content_digest == hashlib.sha256(raw).hexdigest()

        record = measurement.record_reference("case-01", "02")
        assert record.kind == "journal_record"
        assert record.run_id == MEASUREMENT_ID
        start, end = byte_range(record.locator)
        assert 0 < start < end <= len(raw)
        assert hashlib.sha256(raw[start:end]).hexdigest() == record.content_digest
        assert b'"scoring_finished"' in raw[start:end]
        assert raw[start:end].endswith(b"\n")
        assert raw[start:end] in raw
    finally:
        measurement.close()


async def test_indeterminate_rows_carry_the_registry_owner_and_a_reference(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    rig = await build_rig(
        tmp_path, campaign_policy=policy(max_concurrency=2), adapter=adapter, cases=corpus(1)
    )
    adapter.dispositions[(rig.ref.digest, "case-01", "03")] = "target_failed"
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        rows = measurement.indeterminate_rows()

        assert [
            (row["case_id"], row["sample_id"], row["owner"], row["reason"]) for row in rows
        ] == [("case-01", "03", "target", "target_unstable")]
        raw = journal_bytes(rig)
        start, end = byte_range(rows[0]["reference"].locator)
        assert b'"target_failed"' in raw[start:end]
        assert hashlib.sha256(raw[start:end]).hexdigest() == rows[0]["reference"].content_digest
    finally:
        measurement.close()


async def test_status_is_the_s1_projection_and_leaks_no_case_bytes(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        status = measurement.status()

        assert status["kind"] == "status"
        assert status["run_id"] == MEASUREMENT_ID
        assert status["candidate_digest"] == rig.ref.digest
        assert len(status["samples"]) == 3
        assert {sample["scoring_state"] for sample in status["samples"]} == {"finished"}
        raw = journal_bytes(rig)
        for canary in (b"PUBLIC-CANARY", b"PRIVATE-CANARY", b"ORACLE-CANARY"):
            assert canary not in raw
    finally:
        measurement.close()


async def test_no_instance_serves_more_sessions_than_it_was_admitted_for(
    tmp_path: Path,
) -> None:
    """Uneven sample durations must not push an instance past `max_concurrent_sessions`."""

    adapter = FakeAdapter(max_instances=4, max_concurrent_sessions=2)
    rig = await build_rig(
        tmp_path, campaign_policy=policy(max_concurrency=4), adapter=adapter, cases=corpus(2)
    )
    for case_id, sample_id in (("case-01", "01"), ("case-01", "03"), ("case-02", "02")):
        adapter.dispositions[(rig.ref.digest, case_id, sample_id)] = "slow"

    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        # A refused session would surface as `infrastructure_failed`, so an
        # all-scored stage is itself part of the proof.
        assert outcome.terminals == 6 and not outcome.faults
        assert {row.disposition for row in measurement.sample_rows(3)} == {"score"}
        peaks = [instance.peak_sessions for instance in adapter.created.values()]
        assert len(peaks) == 2
        assert max(peaks) == 2, peaks
        assert all(peak <= adapter.capabilities.max_concurrent_sessions for peak in peaks), peaks
    finally:
        measurement.close()


@pytest.mark.parametrize(
    "failure", [IdentityError("run_identity_changed"), JournalRecoveryRequired()]
)
async def test_an_unclassified_failure_reaches_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """R1 and S1 record nothing for these, so a success-shaped outcome would lie."""

    rig = await build_rig(tmp_path, cases=corpus(1))
    original = campaign_measurement.durable_rollout

    async def failing(**kwargs: Any) -> Any:
        if kwargs["sample"].sample_id == "02":
            raise failure
        return await original(**kwargs)

    monkeypatch.setattr(campaign_measurement, "durable_rollout", failing)

    measurement = rig.measurement()
    measurement.create()
    try:
        with pytest.raises(type(failure)):
            await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        # The siblings still ran to their own terminals: the stage is not cancelled.
        rows = {row.sample_id: row.disposition for row in measurement.sample_rows(3)}
        assert rows == {"01": "score", "02": "not_started", "03": "score"}
    finally:
        measurement.close()


async def test_a_scoreable_sample_without_a_scoring_terminal_is_unscored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash S1 cannot re-score: a durable `rollout_finished`, no scoring record."""

    rig = await build_rig(tmp_path, cases=corpus(1))

    async def unrecorded(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(campaign_measurement, "durable_score", unrecorded)

    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        assert {row.disposition for row in measurement.sample_rows(3)} == {"unscored"}
        # An unscored row is terminal for the stage and not evidence for a look.
        assert outcome.terminals == 3
        assert not measurement.complete()
        assert not outcome.faults
        # An unscored try left its sample without a score: resume classifies it as
        # `indeterminate(rollout_interrupted)` and it consumes a try (spec §3.4).
        # Without `sample_retries` it is the last try, so the row is exhausted.
        assert [
            (row["sample_id"], row["retry_ordinal"], row["owner"], row["reason"])
            for row in outcome.indeterminate
        ] == [(sample, 0, "rollout", "rollout_interrupted") for sample in ("01", "02", "03")]
        assert measurement.indeterminate_rows() == outcome.indeterminate
        assert measurement.settled()
    finally:
        measurement.close()


async def test_a_refused_release_during_resume_is_a_leak_not_a_failed_resume(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=1)
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
    owned = sorted(measurement.instances)
    measurement.close()

    adapter.refuse_release = frozenset(owned)
    restarted = rig.measurement()
    released = await restarted.resume(adapter)
    try:
        assert released == ()
        assert sorted(restarted.leaked) == owned
        # The leak is reported, not hidden, and the recovery actions still ran.
        assert (
            sorted(instance["instance_id"] for instance in restarted.status()["owned_instances"])
            == owned
        )
        assert restarted.sample_rows(3)[0].disposition == "score"
    finally:
        restarted.close()


# -- sample tries ---------------------------------------------------------


async def test_a_try_that_fails_once_is_retried_and_scores(tmp_path: Path) -> None:
    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=1),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = ["target_failed"]
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        rows = measurement.sample_rows(1)
        assert [r.disposition for r in rows] == ["score"] and rows[0].retry_ordinal == 1
        assert not rows[0].exhausted
        indeterminate = measurement.indeterminate_rows()
        assert [row["retry_ordinal"] for row in indeterminate] == [0]
        assert (indeterminate[0]["owner"], indeterminate[0]["reason"]) == (
            "target",
            "target_unstable",
        )
        raw = journal_bytes(rig)
        start, end = byte_range(indeterminate[0]["reference"].locator)
        assert b'"target_failed"' in raw[start:end] and b'"retry_ordinal":0' in raw[start:end]
        latest = measurement.record_reference("case-01", "01")
        assert latest == measurement.record_reference("case-01", "01", 1)
        start, end = byte_range(latest.locator)
        assert b'"scoring_finished"' in raw[start:end] and b'"retry_ordinal":1' in raw[start:end]
        assert measurement.complete() and measurement.settled()
        assert measurement.exhausted_rows() == ()
        # The stage outcome reports only rows whose latest try is unsettled.
        assert outcome.indeterminate == ()
    finally:
        measurement.close()


async def test_exhausted_tries_settle_without_completing(tmp_path: Path) -> None:
    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=2),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = "time_budget"
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        (row,) = measurement.sample_rows(1)
        assert (row.disposition, row.retry_ordinal, row.exhausted) == ("indeterminate", 2, True)
        assert [r["retry_ordinal"] for r in measurement.indeterminate_rows()] == [0, 1, 2]
        assert measurement.settled() and not measurement.complete()
        assert [r.case_id for r in measurement.exhausted_rows()] == ["case-01"]
        assert [r.sample_id for r in measurement.exhausted_rows(1)] == ["01"]
        # The stage outcome names the exhausted row once, at its latest try.
        assert [(r["sample_id"], r["retry_ordinal"]) for r in outcome.indeterminate] == [("01", 2)]
    finally:
        measurement.close()


async def test_infrastructure_failed_is_never_retried(tmp_path: Path) -> None:
    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=3),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = (
            "infrastructure_failed"
        )
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        (row,) = measurement.sample_rows(1)
        assert row.retry_ordinal == 0
        assert (row.disposition, row.exhausted) == ("exclude", False)
        assert b'"retry_ordinal":1' not in journal_bytes(rig)
    finally:
        measurement.close()


async def test_a_measurement_without_sample_retries_never_retries(tmp_path: Path) -> None:
    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=policy(),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = ["target_failed"]
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())

        (row,) = measurement.sample_rows(1)
        assert (row.disposition, row.retry_ordinal, row.exhausted) == ("indeterminate", 0, True)
        assert b'"retry_ordinal":1' not in journal_bytes(rig)
        assert measurement.settled() and not measurement.complete()
    finally:
        measurement.close()


async def test_a_stopped_stage_starts_no_further_try(tmp_path: Path) -> None:
    """A stop that arrives after a try's terminal leaves the row unsettled, not retried."""

    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=2),
        adapter=adapter,
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = ["target_failed"]

        def stopped() -> bool:
            # The campaign stops as soon as the first try's terminal is durable.
            return b'"target_failed"' in journal_bytes(rig)

        await measurement.run_stage(rig.plans()[0], **(rig.stage_kwargs() | {"cancelled": stopped}))

        (row,) = measurement.sample_rows(1)
        assert (row.disposition, row.retry_ordinal, row.exhausted) == ("indeterminate", 0, False)
        assert not measurement.settled()
        assert b'"retry_ordinal":1' not in journal_bytes(rig)
    finally:
        measurement.close()


async def test_a_crash_during_a_retry_is_interrupted_counted_and_retried(tmp_path: Path) -> None:
    adapter = FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v5_policy(sample_retries=2),
        adapter=adapter,
        scorer=DeterministicScorer(),
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    key = (measurement.candidate_digest, "case-01", "01")
    adapter.dispositions[key] = ["target_failed", "slow"]
    task = asyncio.ensure_future(measurement.run_stage(rig.plans()[0], **rig.stage_kwargs()))
    try:
        await wait_for_event(rig, "rollout_started", count=4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        measurement.close()

    resumed = rig.measurement()
    await resumed.resume(adapter)
    try:
        await resumed.run_stage(rig.plans()[0], **rig.stage_kwargs())

        (row,) = resumed.sample_rows(1)
        assert row.retry_ordinal == 2 and row.disposition == "score"
        # Only `01` is scripted to retry; a sibling the cancellation caught mid-try
        # is retried too, but it is not what this test is about.
        assert [
            (r["retry_ordinal"], r["reason"])
            for r in resumed.indeterminate_rows()
            if r["sample_id"] == "01"
        ] == [(0, "target_unstable"), (1, "rollout_interrupted")]
        assert resumed.complete() and resumed.settled()
    finally:
        resumed.close()


async def test_refuse_rows_closes_only_untried_rows_as_their_first_try(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, campaign_policy=v5_policy(sample_retries=2), cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        plan = rig.plans()[0]
        tried = StagePlan(plan.stage, plan.look, plan.rows[:1])
        await measurement.run_stage(tried, **rig.stage_kwargs())

        assert measurement.refuse_rows(plan) == ("02", "03")
        rows = {row.sample_id: row for row in measurement.sample_rows(3)}
        assert rows["01"].disposition == "score"
        assert [(rows[s].outcome, rows[s].retry_ordinal) for s in ("02", "03")] == [
            ("target_failed", 0),
            ("target_failed", 0),
        ]
        assert [(r["sample_id"], r["retry_ordinal"]) for r in measurement.indeterminate_rows()] == [
            ("02", 0),
            ("03", 0),
        ]
        with pytest.raises(ValueError, match="no terminal record"):
            measurement.record_reference("case-01", "02", 1)
    finally:
        measurement.close()


# -- carried (imported) pairs ---------------------------------------------


def imported_row_for(measurement: Measurement, row: Any) -> dict[str, Any]:
    """A Task-1 `sample_imported` payload for one plan row of `measurement`."""

    from tests.tree.execution.test_journal_durability import imported_payload

    return imported_payload(measurement.identity, row, metrics={PRIMARY: 1.0})


async def test_a_stage_dispatches_only_the_pairs_it_lacks(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    measurement = rig.measurement()
    plan = rig.plans()[0]
    carried = [imported_row_for(measurement, row) for row in plan.rows[1:]]
    measurement.create(imported=carried)
    try:
        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        # Only plan.rows[0] opened a session.
        assert rig.adapter.session_case_order(measurement=MEASUREMENT_ID) == (
            plan.rows[0]["case_id"],
        )
        rows = measurement.sample_rows(plan.look)
        assert all(row.disposition == "score" for row in rows)
        assert sum(row.imported for row in rows) == len(carried)
        assert measurement.try_count(plan.rows[1]["case_id"], plan.rows[1]["sample_id"]) == 0
        assert outcome.faults == () and outcome.indeterminate == ()
        assert outcome.terminals == len(plan.rows)
        assert measurement.indeterminate_rows() == ()
        # The carried pair's reference is its own `sample_imported` record.
        reference = measurement.record_reference(plan.rows[1]["case_id"], plan.rows[1]["sample_id"])
        start, end = byte_range(reference.locator)
        record = journal_bytes(rig)[start:end]
        assert b'"event":"sample_imported"' in record
        assert f'"sample_id":"{plan.rows[1]["sample_id"]}"'.encode() in record
        assert measurement.status()["samples"][1]["execution_state"] == "imported"
    finally:
        measurement.close()


async def test_a_wholly_carried_stage_opens_no_session(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path)
    measurement = rig.measurement()
    plan = rig.plans()[0]
    measurement.create(imported=[imported_row_for(measurement, row) for row in plan.rows])
    try:
        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        assert rig.adapter.session_case_order(measurement=MEASUREMENT_ID) == ()
        assert outcome.terminals == len(plan.rows) and outcome.indeterminate == ()
        assert all(row.imported for row in measurement.sample_rows(plan.look))
    finally:
        measurement.close()


async def test_refuse_rows_leaves_imported_pairs_alone(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, campaign_policy=v5_policy(sample_retries=2), cases=corpus(1))
    measurement = rig.measurement()
    plan = rig.plans()[0]
    measurement.create(imported=[imported_row_for(measurement, plan.rows[0])])
    try:
        refused = measurement.refuse_rows(plan)

        assert plan.rows[0]["sample_id"] not in refused
        assert len(refused) == len(plan.rows) - 1
        assert measurement.try_count(plan.rows[0]["case_id"], plan.rows[0]["sample_id"]) == 0
    finally:
        measurement.close()


async def test_a_transient_materialize_and_release_are_retried_under_call_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: with `/8`'s `call_retries` the measurement materializes and releases
    through the retry loop, under one instance id, and the journal admits the
    repeated start; without it (every earlier policy) nothing is retried."""

    import evolution.tree.execution.rollout as rollout_core
    from evolution.tree.campaign_policy import CampaignPolicyV11
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(rollout_core, "_SLEEP", no_sleep)

    class Flaky(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(max_instances=1, max_concurrent_sessions=1)
            self.materialize_calls = 0
            self.release_calls = 0

        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            self.materialize_calls += 1
            if self.materialize_calls == 1:
                raise RolloutInfrastructureError("x", "not_ready", retryable=True)
            return await super().materialize(candidate, strategy, instance_id)

        async def release(self, instance_id: str) -> None:
            self.release_calls += 1
            if self.release_calls == 1:
                raise RolloutInfrastructureError("x", "transport_error", retryable=True)
            await super().release(instance_id)

    adapter = Flaky()
    rig = await build_rig(tmp_path, adapter=adapter, cases=corpus(1))
    v8 = CampaignPolicyV11.from_payload(
        {
            **v7_payload(max_concurrency=1),
            "call_retries": {"max_attempts": 2, "turn_max_attempts": 1},
        }
    )
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.materialize(adapter, rig.ref, STRATEGY, v8)
        assert adapter.materialize_calls == 2 and len(adapter.materialized) == 1
        assert await measurement.release_all(adapter) == ()
        assert adapter.release_calls == 2
        assert adapter.released == adapter.materialized
    finally:
        measurement.close()
    events = [
        json.loads(line)
        for line in journal_bytes(rig).splitlines()
        if b"target_materialize_" in line or b"target_release_" in line
    ]
    assert [(e["event"], e["payload"].get("call_attempt")) for e in events] == [
        ("target_materialize_started", 1),
        ("target_materialize_failed", 1),
        ("target_materialize_started", 2),
        ("target_materialize_finished", 2),
        ("target_release_started", 1),
        ("target_release_failed", 1),
        ("target_release_started", 2),
        ("target_release_finished", 2),
    ]


async def test_an_exhausted_transient_send_is_target_failed_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1 Task 4 (spec §4): a transient send that exhausts `/8`'s `call_retries`
    ends the sample `target_failed` -- indeterminate, owned by `target`, reason
    `target_unstable` -- not a fault, and is retried like any other
    indeterminate sample up to `sample_retries`."""

    import evolution.tree.execution.rollout as rollout_core
    from evolution.tree.campaign_policy import CampaignPolicyV11
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(rollout_core, "_SLEEP", no_sleep)

    class FlakyInstance(FakeInstance):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.send_calls = 0

        async def send(self, session: Any, message: Any, request_id: str) -> Any:
            self.send_calls += 1
            raise RolloutInfrastructureError("x", "internal", retryable=True)

    class FlakyAdapter(FakeAdapter):
        def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
            return FlakyInstance(
                instance_id,
                boot_id,
                self._semantic_for(candidate.dependency_digest),
                candidate.digest,
                self.dispositions,
                self.capabilities.max_concurrent_sessions,
                self.description,
            )

    adapter = FlakyAdapter(max_instances=1, max_concurrent_sessions=1)
    v8 = CampaignPolicyV11.from_payload(
        {
            **v7_payload(max_concurrency=1, sample_retries=1),
            "call_retries": {"max_attempts": 2, "turn_max_attempts": 1},
        }
    )
    rig = await build_rig(tmp_path, campaign_policy=v8, adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        plan = rig.plans()[0]
        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        assert outcome.faults == ()
        assert measurement.fault_rows(PRIMARY) == ()
        assert outcome.indeterminate
        assert [(row["owner"], row["reason"]) for row in outcome.indeterminate] == [
            ("target", "target_unstable")
        ] * len(outcome.indeterminate)

        rows = measurement.sample_rows(plan.look)
        assert rows
        assert all(row.outcome == "target_failed" for row in rows)
        assert all(row.disposition == "indeterminate" for row in rows)
        assert all(row.exhausted for row in rows)
        # `sample_retries == 1`: two tries per sample, both fail the same way.
        assert all(row.retry_ordinal == 1 for row in rows)
        instance = next(iter(adapter.created.values()))
        assert instance.send_calls == 2 * len(rows)
    finally:
        measurement.close()


async def test_an_unretried_contract_send_failure_stays_infrastructure_failed_and_a_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same `/8` policy, but the send failure's code is a contract one
    (never transient, spec §3.1): the sample still ends `infrastructure_failed`
    and is still a fault, exactly as an earlier manifest classified it."""

    import evolution.tree.execution.rollout as rollout_core
    from evolution.tree.campaign_policy import CampaignPolicyV11
    from evolution.tree.execution.interaction import RolloutInfrastructureError

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(rollout_core, "_SLEEP", no_sleep)

    class FlakyInstance(FakeInstance):
        async def send(self, session: Any, message: Any, request_id: str) -> Any:
            raise RolloutInfrastructureError("x", "invalid_request", retryable=True)

    class FlakyAdapter(FakeAdapter):
        def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
            return FlakyInstance(
                instance_id,
                boot_id,
                self._semantic_for(candidate.dependency_digest),
                candidate.digest,
                self.dispositions,
                self.capabilities.max_concurrent_sessions,
                self.description,
            )

    adapter = FlakyAdapter(max_instances=1, max_concurrent_sessions=1)
    v8 = CampaignPolicyV11.from_payload(
        {
            **v7_payload(max_concurrency=1, sample_retries=1),
            "call_retries": {"max_attempts": 2, "turn_max_attempts": 1},
        }
    )
    rig = await build_rig(tmp_path, campaign_policy=v8, adapter=adapter, cases=corpus(1))
    measurement = rig.measurement()
    measurement.create()
    try:
        plan = rig.plans()[0]
        outcome = await measurement.run_stage(plan, **rig.stage_kwargs())

        assert not outcome.indeterminate
        assert outcome.faults
        assert {row["reason"] for row in outcome.faults} == {"infrastructure_failed"}
        assert measurement.fault_rows(PRIMARY) == outcome.faults

        rows = measurement.sample_rows(plan.look)
        assert rows
        assert all(row.outcome == "infrastructure_failed" for row in rows)
        assert all(row.disposition == "exclude" for row in rows)
        # A contract failure is never retried at the sample level either.
        assert all(row.retry_ordinal == 0 for row in rows)
    finally:
        measurement.close()


# -- frozen measurements (I2 spec §5.1, §5.3) -------------------------------


async def sealed_subset_measurement(
    tmp_path: Path,
    *,
    case_ids: tuple[str, ...] = ("case-01", "case-03"),
    campaign_policy: CampaignPolicyV11 | None = None,
    stages: int | None = None,
    adapter: FakeAdapter | None = None,
) -> tuple[Rig, Measurement]:
    """A winner's Candidate measurement over a subset of `corpus(3)`, closed after `stages`.

    G2's completion source (spec §3.2, §4.7): every stage up to `stages` (all
    by default) runs to a terminal, then the measurement is closed and never
    appended to again -- what the round's Promotion seals in family 12.
    """

    rig = await build_rig(
        tmp_path, campaign_policy=campaign_policy, adapter=adapter, cases=corpus(3)
    )
    winner = rig.subset_measurement(case_ids)
    plans = rig.plans("candidate", cases=winner.scheduled_cases)
    winner.create()
    try:
        for plan in plans[:stages]:
            await winner.run_stage(plan, **rig.stage_kwargs())
    finally:
        winner.close()
    return rig, winner


async def interrupted_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, carried: bool = True
) -> Rig:
    """A real measurement journal left unfinished mid-stage, as a condition change finds it.

    Over `corpus(2)` (one look, repeats `01`-`03`): `case-01/03` is carried in by
    `sample_imported` when `carried`, `case-01/02` is a `target_failed`
    (indeterminate, and the last try: `policy()` declares no `sample_retries`),
    `case-01/01` is `slow` and is still in flight when the stage is cancelled, and
    every `case-02` sample is scored. The measurement is closed afterwards and never
    resumed, so its journal ends on an open try.
    """

    from tests import campaign_fakes

    # Long enough that the in-flight sample can never finish before the cancel.
    monkeypatch.setattr(campaign_fakes, "SLOW_SECONDS", 60.0)
    rig = await build_rig(tmp_path, cases=corpus(2))
    rig.adapter.dispositions[(rig.ref.digest, "case-01", "01")] = "slow"
    rig.adapter.dispositions[(rig.ref.digest, "case-01", "02")] = "target_failed"
    measurement = rig.measurement()
    plan = rig.plans()[0]
    measurement.create(
        imported=[
            imported_row_for(measurement, row)
            for row in plan.rows
            if carried and (row["case_id"], row["sample_id"]) == ("case-01", "03")
        ]
    )
    task = asyncio.ensure_future(measurement.run_stage(plan, **rig.stage_kwargs()))
    try:
        await wait_for_event(rig, "scoring_finished", count=3)
        await wait_for_event(rig, "rollout_finished", count=4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        measurement.close()
    return rig


async def test_open_frozen_reads_a_measurement_the_session_corpus_no_longer_derives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch)
    before = journal_bytes(rig)

    # The session's corpus changed: resuming the measurement under it refuses.
    changed = rig.measurement()
    changed.corpus = corpus(3)
    with pytest.raises(IdentityError):
        await changed.resume(rig.adapter)
    changed.close()

    frozen = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID)

    created = frozen.records()[0].payload
    assert frozen.measurement_id == MEASUREMENT_ID
    assert frozen.identity_digest == created["identity_digest"]
    assert frozen.candidate_digest == rig.ref.digest
    assert frozen.prefix_digest == hashlib.sha256(before).hexdigest()
    rows = {(row.case_id, row.sample_id): row for row in frozen.sample_rows(3)}
    assert rows[("case-01", "03")].imported
    assert rows[("case-01", "02")].disposition == "indeterminate"
    assert rows[("case-01", "01")].disposition != "score"
    assert {rows[("case-02", s)].disposition for s in ("01", "02", "03")} == {"score"}
    assert [row.sample_id for row in frozen.sample_rows(1)] == ["01", "01"]
    assert not frozen.complete()
    # Read-only: nothing appended, and no recovery closed the open try.
    assert journal_bytes(rig) == before


async def test_open_frozen_sample_rows_match_a_live_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch)
    frozen = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID)
    frozen_rows = frozen.sample_rows(3)
    frozen_records = frozen.records()

    # `open_frozen` holds no lease: a writer can still take the journal.
    live = rig.measurement()
    await live.resume(rig.adapter)
    try:
        # Resume closes the open try, so compare only the rows it did not touch.
        live_rows = {(r.case_id, r.sample_id): r for r in live.sample_rows(3)}
        for row in frozen_rows:
            if (row.case_id, row.sample_id) != ("case-01", "01"):
                assert live_rows[(row.case_id, row.sample_id)] == row
        assert live.records()[: len(frozen_records)] == frozen_records
    finally:
        live.close()


def test_open_frozen_refuses_an_absent_or_unborn_measurement(tmp_path: Path) -> None:
    with pytest.raises(MeasurementError, match="frozen_measurement_unreadable"):
        Measurement.open_frozen(tmp_path, MEASUREMENT_ID)
    Measurement.directory_for(tmp_path, MEASUREMENT_ID).mkdir(parents=True)
    with pytest.raises(MeasurementError, match="frozen_measurement_unreadable"):
        Measurement.open_frozen(tmp_path, MEASUREMENT_ID)


def tear_last_record(rig: Rig, measurement_id: str = MEASUREMENT_ID) -> tuple[bytes, bytes]:
    """Cut the journal's last record mid-line, as a crash mid-append leaves it.

    Returns `(valid_prefix, torn_file_bytes)`.
    """

    file = rig.run_root / "measurements" / measurement_id / "events.jsonl"
    raw = file.read_bytes()
    last_start = raw.rstrip(b"\n").rfind(b"\n") + 1
    torn = raw[: last_start + (len(raw) - last_start) // 2]
    file.write_bytes(torn)
    return raw[:last_start], torn


async def test_open_frozen_reads_the_valid_prefix_of_a_torn_tail_and_never_repairs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch)
    last_record = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID).records()[-1]
    valid, torn = tear_last_record(rig)

    first = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID)
    second = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID)

    assert first.journal_integrity == second.journal_integrity == "incomplete_tail"
    assert last_record not in first.records()
    assert b"".join(record.encode() for record in first.records()) == valid
    assert first.prefix_digest == second.prefix_digest == hashlib.sha256(valid).hexdigest()
    assert first.records() == second.records()
    assert first.sample_rows(3) == second.sample_rows(3)
    # Nothing truncated the torn tail: a frozen measurement is never repaired.
    assert journal_bytes(rig) == torn


async def test_open_frozen_refuses_a_corrupt_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch, carried=False)
    file = rig.run_root / "measurements" / MEASUREMENT_ID / "events.jsonl"
    file.write_bytes(file.read_bytes() + b'{"not":"an envelope"}\n')

    with pytest.raises(MeasurementError, match="frozen_measurement_unreadable"):
        Measurement.open_frozen(rig.run_root, MEASUREMENT_ID)


async def test_open_frozen_refuses_another_measurements_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal copied in from another measurement id is not this measurement."""

    import shutil

    rig = await interrupted_measurement(tmp_path, monkeypatch, carried=False)
    shutil.copytree(
        Measurement.directory_for(rig.run_root, MEASUREMENT_ID),
        Measurement.directory_for(rig.run_root, "m-r01-a01"),
    )

    with pytest.raises(MeasurementError, match="frozen_measurement_unreadable"):
        Measurement.open_frozen(rig.run_root, "m-r01-a01")


# -- subset measurements (G2 spec §3.1) --------------------------------------


async def test_a_subset_measurement_stages_and_scores_only_its_cases(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, cases=corpus(3))
    measurement = rig.subset_measurement(["case-03", "case-01"])
    assert [case.case_id for case in measurement.scheduled_cases] == ["case-01", "case-03"]
    plans = stage_plans(rig.policy, measurement.scheduled_cases, rig.adapter.seed_mode, "candidate")
    assert [row for plan in plans for row in plan.rows] == measurement.identity.to_payload()[
        "schedule"
    ]
    measurement.create()
    try:
        for plan in plans:
            await measurement.run_stage(plan, **rig.stage_kwargs())
        assert measurement.complete()
        assert set(rig.adapter.session_case_order(measurement=MEASUREMENT_ID)) == {
            "case-01",
            "case-03",
        }
        rows = measurement.sample_rows(plans[-1].look)
        assert {row.case_id for row in rows} == {"case-01", "case-03"}
        assert {row.disposition for row in rows} == {"score"}
    finally:
        measurement.close()


async def test_a_full_corpus_measurement_schedules_every_case(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, cases=corpus(3))
    assert rig.measurement().scheduled_cases == rig.cases


async def test_a_subset_measurement_resumes_under_its_own_selector_only(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, campaign_policy=two_looks(), cases=corpus(3))
    measurement = rig.subset_measurement(["case-02"])
    plans = campaign_measurement._partition(
        (3, 6),
        schedule_for(rig.policy, measurement.scheduled_cases, rig.adapter.seed_mode, "candidate"),
    )
    measurement.create()
    await measurement.run_stage(plans[0], **rig.stage_kwargs())
    measurement.close()

    other = rig.subset_measurement(["case-01"])
    with pytest.raises(IdentityError):
        await other.resume(rig.adapter)
    other.close()

    restarted = rig.subset_measurement(["case-02"])
    await restarted.resume(rig.adapter)
    try:
        await restarted.run_stage(plans[1], **rig.stage_kwargs())
        assert restarted.complete()
        assert set(rig.adapter.session_case_order(measurement=MEASUREMENT_ID)) == {"case-02"}
    finally:
        restarted.close()


async def test_the_audit_derives_a_born_subset_measurement_without_the_corpus(
    tmp_path: Path,
) -> None:
    rig, winner = await sealed_subset_measurement(tmp_path)
    frozen = Measurement.open_frozen(rig.run_root, winner.measurement_id)
    stored = RunIdentityV1.from_payload(frozen.records()[0].payload["identity"])
    assert stored.case_subset == (0, 2)
    derived = derived_identity(
        rig.identity, rig.ref.digest, "candidate", case_subset=stored.case_subset
    )
    assert derived.digest == frozen.identity_digest == winner.identity.digest


# -- one stage as prepare + settle; interleaved stages (G2 spec §4.6) --------


async def test_run_stage_is_prepare_then_settle_and_unchanged(tmp_path: Path) -> None:
    """`run_stage` is exactly `prepare_stage`, one task per row, then `settle_stage`."""

    outcomes = []
    events = []
    for name in ("whole", "split"):
        (tmp_path / name).mkdir()
        rig = await build_rig(tmp_path / name, cases=corpus(3))
        measurement = rig.measurement()
        measurement.create()
        plan = rig.plans()[0]
        try:
            if name == "whole":
                outcome = await measurement.run_stage(plan, **rig.stage_kwargs())
            else:
                dispatch = await measurement.prepare_stage(
                    plan,
                    **rig.stage_kwargs(),
                    permits=asyncio.Semaphore(rig.policy.max_concurrency),
                    scoring_permits=asyncio.Semaphore(rig.policy.max_scorer_concurrency),
                )
                assert dispatch.measurement is measurement and dispatch.plan == plan
                assert dispatch.rows == plan.rows
                results = await asyncio.gather(
                    *(dispatch.start(row) for row in dispatch.rows), return_exceptions=True
                )
                outcome = await measurement.settle_stage(
                    dispatch, results, adapter=rig.adapter, policy=rig.policy
                )
        finally:
            measurement.close()
        outcomes.append(outcome)
        events.append(s1_events(rig))
    whole, split = outcomes
    for name in ("stage", "look", "terminals", "indeterminate", "faults", "critical_failed"):
        assert getattr(whole, name) == getattr(split, name), name
    assert whole.terminals == len(corpus(3)) * 3
    assert len(whole.leaked) == len(split.leaked)
    assert len(whole.attested) == len(split.attested) == 1
    assert events[0] == events[1]


async def test_run_interleaved_starts_rows_in_the_given_order(tmp_path: Path) -> None:
    """Two subset measurements, one permit: rows run strictly in the merged order.

    One Parent row raises an unclassified error: it comes back at its own
    dispatch and position, and only the Parent's `settle_stage` raises it.
    """

    rig = await build_rig(tmp_path, cases=corpus(3))
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    (parent_root / "skill.py").write_text("VALUE = 2\n", encoding="utf-8")
    parent_ref = artifact(parent_root)
    assert parent_ref.digest != rig.ref.digest
    case_ids = ["case-01", "case-03"]
    candidate = rig.subset_measurement(case_ids, "m-r01-a01", "candidate")
    parent = rig.subset_measurement(case_ids, "m-r01-a01-parent", "champion", ref=parent_ref)
    permits = asyncio.Semaphore(1)
    scoring = asyncio.Semaphore(rig.policy.max_scorer_concurrency)
    dispatches = []
    for measurement, ref, side in (
        (candidate, rig.ref, "candidate"),
        (parent, parent_ref, "champion"),
    ):
        measurement.create()
        (plan,) = stage_plans(rig.policy, measurement.scheduled_cases, rig.adapter.seed_mode, side)
        kwargs = {**rig.stage_kwargs(), "candidate": ref}
        dispatches.append(
            await measurement.prepare_stage(
                plan, **kwargs, permits=permits, scoring_permits=scoring
            )
        )
    boom = RuntimeError("boom")
    failing = next(
        index
        for index, row in enumerate(dispatches[1].rows)
        if (row["case_id"], int(row["repeat"])) == ("case-03", 2)
    )
    start = dispatches[1].start

    async def raising() -> None:
        raise boom

    dispatches[1] = replace(
        dispatches[1],
        start=lambda row: raising() if row is dispatches[1].rows[failing] else start(row),
    )
    position = {case.case_id: index for index, case in enumerate(rig.cases)}
    try:
        results = await campaign_measurement.run_interleaved(
            dispatches,
            lambda index, row: (position[row["case_id"]], int(row["repeat"]), index),
        )
        assert [len(result) for result in results] == [6, 6]
        assert results[0] == [None] * 6
        assert results[1] == [boom if index == failing else None for index in range(6)]
        outcome = await candidate.settle_stage(
            dispatches[0], results[0], adapter=rig.adapter, policy=rig.policy
        )
        assert outcome.terminals == 6 and not outcome.indeterminate
        with pytest.raises(RuntimeError, match="boom"):
            await parent.settle_stage(
                dispatches[1], results[1], adapter=rig.adapter, policy=rig.policy
            )
    finally:
        candidate.close()
        parent.close()
    expected = [
        (digest, case_id)
        for case_id in case_ids
        for repeat in range(1, 4)
        for digest in (rig.ref.digest, parent_ref.digest)
        if (digest, case_id, repeat) != (parent_ref.digest, "case-03", 2)
    ]
    assert rig.adapter.session_order == expected


# -- components_declared (G2b1, family 12) ----------------------------------

MAP = {"catalog": {"sha256": "a" * 64, "sources": ["skills/a/SKILL.md"]}}
OTHER = {"catalog": {"sha256": "b" * 64, "sources": ["skills/a/SKILL.md"]}}


def s1_records(rig: Rig, measurement_id: str = MEASUREMENT_ID) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal_bytes(rig, measurement_id).splitlines()]


def s1_events(rig: Rig, measurement_id: str = MEASUREMENT_ID) -> list[str]:
    return [record["event"] for record in s1_records(rig, measurement_id)]


async def run_first_stage(rig: Rig) -> Measurement:
    measurement = rig.measurement()
    measurement.create()
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
    finally:
        measurement.close()
    return measurement


async def test_each_distinct_map_is_declared_once_before_any_rollout(tmp_path: Path) -> None:
    # Two instances declaring the same map: one record, written once the first
    # attested and before any row ran.
    adapter = FakeAdapter(max_concurrent_sessions=2, components=MAP)
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(max_concurrency=4), adapter=adapter)
    measurement = await run_first_stage(rig)

    events = s1_events(rig)
    assert events.count("target_materialize_finished") == 2
    declared = [i for i, e in enumerate(events) if e == "components_declared"]
    assert len(declared) == 1
    assert declared[0] < events.index("rollout_started")
    assert declared[0] > events.index("target_materialize_finished")
    record = s1_records(rig)[declared[0]]
    digest = components_digest(MAP)
    assert record["operation_id"] is None
    assert record["payload"] == {"components_digest": digest, "components": MAP}
    assert set(measurement._components_digest) == set(adapter.materialized)
    assert set(measurement._components_digest.values()) == {digest}


async def test_two_distinct_maps_give_two_records(tmp_path: Path) -> None:
    adapter = FakeAdapter(max_concurrent_sessions=2, components=[MAP, OTHER])
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(max_concurrency=4), adapter=adapter)
    measurement = await run_first_stage(rig)

    records = [r for r in s1_records(rig) if r["event"] == "components_declared"]
    assert [r["payload"]["components"] for r in records] == [MAP, OTHER]
    first, second = adapter.materialized
    assert measurement._components_digest == {
        first: components_digest(MAP),
        second: components_digest(OTHER),
    }


async def test_an_instance_without_a_map_declares_nothing(tmp_path: Path) -> None:
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=FakeAdapter())
    measurement = await run_first_stage(rig)

    assert "components_declared" not in s1_events(rig)
    assert set(measurement._components_digest.values()) == {None}


async def test_a_rematerialized_instance_does_not_declare_its_map_again(tmp_path: Path) -> None:
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    measurement = rig.measurement()
    measurement.create()
    try:
        kwargs = rig.stage_kwargs()
        await measurement.materialize(adapter, rig.ref, STRATEGY, rig.policy)
        assert await measurement.release_all(adapter) == ()
        await measurement.materialize(adapter, rig.ref, STRATEGY, rig.policy)
        await measurement.run_stage(rig.plans()[0], **kwargs)
    finally:
        measurement.close()

    events = s1_events(rig)
    assert events.count("target_materialize_finished") == 2
    assert events.count("components_declared") == 1
    assert set(measurement._components_digest.values()) == {components_digest(MAP)}


async def test_a_refused_declaration_releases_the_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    measurement = rig.measurement()
    measurement.create()
    original = Journal.append

    def refusing(self: Any, event: str, payload: dict[str, Any], *a: Any, **kw: Any) -> int:
        if event == "components_declared":
            raise JournalAdmissionRefused()
        return original(self, event, payload, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Journal, "append", refusing)
    try:
        with pytest.raises(JournalAdmissionRefused):
            await measurement.materialize(adapter, rig.ref, STRATEGY, rig.policy)
        assert adapter.released == adapter.materialized != []
        assert not measurement.instances
        assert "rollout_started" not in s1_events(rig)
    finally:
        measurement.close()


async def test_an_empty_map_is_declared_and_the_stage_completes(tmp_path: Path) -> None:
    # `{}` is a declared map (spec §4.2); only `None` means none was declared.
    adapter = FakeAdapter(components={})
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    measurement = rig.measurement()
    measurement.create()
    try:
        outcome = await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        assert outcome.terminals == len(rig.plans()[0].rows)
        assert measurement.complete()
    finally:
        measurement.close()

    records = [r for r in s1_records(rig) if r["event"] == "components_declared"]
    assert [r["payload"] for r in records] == [
        {"components_digest": components_digest({}), "components": {}}
    ]
    assert set(measurement._components_digest.values()) == {components_digest({})}


# -- provenance and components_digest on rollout_finished (G2b1) --------------


def rollout_finished_payloads(rig: Rig) -> list[dict[str, Any]]:
    return [r["payload"] for r in s1_records(rig) if r["event"] == "rollout_finished"]


ROW_KEYS = {"provenance", "provenance_overflow", "components_digest"}


async def test_a_row_of_an_instance_without_a_map_names_no_digest(tmp_path: Path) -> None:
    adapter = FakeAdapter(used=lambda case_id, turn: ["a.md"])
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    await run_first_stage(rig)

    for row in rollout_finished_payloads(rig):
        assert row["provenance"] == ["a.md"]
        assert "components_digest" not in row


async def test_an_empty_union_is_recorded_as_an_empty_list(tmp_path: Path) -> None:
    adapter = FakeAdapter(components={}, used=lambda case_id, turn: [])
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    await run_first_stage(rig)

    for row in rollout_finished_payloads(rig):
        assert row["provenance"] == []
        assert row["components_digest"] == components_digest({})


MANY = [f"p/{i:04d}.md" for i in range(1025)]


async def test_1024_is_recorded_and_1025_overflows(tmp_path: Path) -> None:
    def used(case_id: str, turn: int) -> list[str]:
        return MANY[:1024] if turn == 0 else MANY[1024:]

    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=FakeAdapter(used=used))
    rig.environment = ScriptedEnvironment(messages=2)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert rows
    for row in rows:
        assert row["outcome"] == "completed"
        assert "provenance" not in row and row["provenance_overflow"] is True


async def test_exactly_1024_identifiers_are_recorded(tmp_path: Path) -> None:
    adapter = FakeAdapter(used=lambda case_id, turn: MANY[:1024])
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert rows
    for row in rows:
        assert len(row["provenance"]) == 1024
        assert "provenance_overflow" not in row


# A 1024-item reproducer (ADR-0024, 2026-10-01 amendment): 1024 wire-valid
# identifiers whose union is about 1 MiB of canonical JSON. The count rule
# alone records it, and the record would exceed S1's 1 MiB limit.
WIDE = ["\U0001f600" * 127 + "/" + "\U0001f600" * 127 + chr(0x20000 + i) for i in range(1024)]
#: 1024 ASCII identifiers whose sorted list is exactly 256 KiB of canonical
#: JSON (1023 of 253 characters and one of 252), and the same list with the
#: short one lengthened, one byte over.
AT_BUDGET = sorted(f"p/{i:04d}" + "x" * (247 if i else 246) for i in range(1024))
OVER_BUDGET = sorted(f"p/{i:04d}" + "x" * 247 for i in range(1024))


async def test_a_union_over_the_byte_budget_overflows(tmp_path: Path) -> None:
    adapter = FakeAdapter(used=lambda case_id, turn: WIDE)
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert {r["case_id"] for r in rows} == {"case-01", "case-02"}
    for row in rows:
        assert row["outcome"] == "completed"
        assert "provenance" not in row and row["provenance_overflow"] is True


@pytest.mark.parametrize(("used", "recorded"), [(AT_BUDGET, True), (OVER_BUDGET, False)])
async def test_the_byte_budget_is_inclusive(
    tmp_path: Path, used: list[str], recorded: bool
) -> None:
    assert len(json.dumps(used, separators=(",", ":"))) == 256 * 1024 + (not recorded)
    adapter = FakeAdapter(used=lambda case_id, turn: used)
    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=adapter)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert rows
    for row in rows:
        if recorded:
            assert row["provenance"] == used and "provenance_overflow" not in row
        else:
            assert "provenance" not in row and row["provenance_overflow"] is True


async def test_one_turn_without_provenance_makes_the_row_unknown(tmp_path: Path) -> None:
    def used(case_id: str, turn: int) -> list[str] | None:
        return ["a.md"] if turn == 0 else None

    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=FakeAdapter(used=used))
    rig.environment = ScriptedEnvironment(messages=2)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert rows
    for row in rows:
        assert not ROW_KEYS & row.keys()


# -- Collection is opt-in and bounded while the rollout runs ------------------


async def test_many_full_turns_overflow_with_a_bounded_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evolution.tree.execution.rollout as rollout_core

    peak = 0

    class _Spy(rollout_core._ProvenanceUnion):
        def add(self, used: list[str] | None) -> None:
            nonlocal peak
            super().add(used)
            peak = max(peak, 0 if self.ids is None else len(self.ids))

    monkeypatch.setattr(rollout_core, "_ProvenanceUnion", _Spy)

    def used(case_id: str, turn: int) -> list[str]:
        return [f"t{turn}/{i:04d}.md" for i in range(1024)]

    rig = await build_rig(tmp_path, campaign_policy=v9_policy(), adapter=FakeAdapter(used=used))
    rig.environment = ScriptedEnvironment(messages=4)
    await run_first_stage(rig)

    rows = rollout_finished_payloads(rig)
    assert rows
    for row in rows:
        assert row["outcome"] == "completed"
        assert "provenance" not in row and row["provenance_overflow"] is True
    assert peak == 1024


# -- #194: a restored row starts one D1 retry after a cancelled try ----------


async def _cancelled_rig(tmp_path: Path, *, retries: int, adapter: FakeAdapter | None = None):
    """A `row_provenance` rig whose one-case stage closed every sample `cancelled` at 0."""

    adapter = adapter or FakeAdapter(dispositions={})
    rig = await build_rig(
        tmp_path,
        campaign_policy=v9_policy(sample_retries=retries),
        adapter=adapter,
        cases=corpus(1),
    )
    measurement = rig.measurement()
    measurement.create()
    await measurement.run_stage(
        rig.plans()[0], **(rig.stage_kwargs() | {"cancelled": lambda: True})
    )
    assert {r.outcome for r in measurement.sample_rows(3)} == {"cancelled"}
    assert measurement.identity.to_payload().get("row_provenance") == 1
    return rig, measurement


def _tries(measurement: Measurement, sample_id: str) -> int:
    journal = measurement._require_journal()
    return len(journal.sample_tries("case-01", sample_id))


async def test_a_restored_row_starts_one_d1_retry_and_continues_within_the_dispatch(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(dispositions={})
    rig, measurement = await _cancelled_rig(tmp_path, retries=2, adapter=adapter)
    try:
        adapter.dispositions[(measurement.candidate_digest, "case-01", "01")] = ["target_failed"]
        await measurement.run_stage(
            rig.plans()[0],
            **(rig.stage_kwargs() | {"restored_rows": frozenset({("case-01", "01")})}),
        )
        journal = measurement._require_journal()
        tries = journal.sample_tries("case-01", "01")
        # 0 cancelled, 1 target_failed (the restored retry), 2 scored (ordinary D1).
        assert [measurement._ordinal_of(t[0]) for t in tries] == [0, 1, 2]
        assert tries[1][0].payload["outcome"] == "target_failed"
        assert tries[2][1] is not None
        assert all("repair" not in t[0].payload for t in tries)
        assert not journal.sample_repairs("case-01", "01")
        # Rows outside `restored_rows` start nothing.
        assert _tries(measurement, "02") == 1 and _tries(measurement, "03") == 1
    finally:
        measurement.close()


async def test_a_row_outside_restored_rows_starts_nothing(tmp_path: Path) -> None:
    rig, measurement = await _cancelled_rig(tmp_path, retries=2)
    try:
        await measurement.run_stage(rig.plans()[0], **rig.stage_kwargs())
        assert [_tries(measurement, s) for s in ("01", "02", "03")] == [1, 1, 1]
    finally:
        measurement.close()


async def test_a_restored_row_at_the_retry_limit_starts_nothing(tmp_path: Path) -> None:
    rig, measurement = await _cancelled_rig(tmp_path, retries=0)
    try:
        await measurement.run_stage(
            rig.plans()[0],
            **(rig.stage_kwargs() | {"restored_rows": frozenset({("case-01", "01")})}),
        )
        assert _tries(measurement, "01") == 1
    finally:
        measurement.close()


async def test_a_token_set_before_dispatch_starts_no_restored_retry(tmp_path: Path) -> None:
    rig, measurement = await _cancelled_rig(tmp_path, retries=2)
    try:
        await measurement.run_stage(
            rig.plans()[0],
            **(
                rig.stage_kwargs()
                | {
                    "cancelled": lambda: True,
                    "restored_rows": frozenset({("case-01", "01")}),
                }
            ),
        )
        assert _tries(measurement, "01") == 1
    finally:
        measurement.close()
