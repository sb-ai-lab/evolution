"""I2: which effects a comparable change admits, and the `continue` transition.

Task 4 (spec §3.1-§3.3, §4.3): `admissible_effects` is the table of §3.3 with
§3.2's round-budget rule; `rounds_remaining` counts this run's begun rounds
against the new policy's budget. Admission reports the effects on
`IdentityChanged`, default first.

Task 7 (spec §5.2-§5.4, §6.1, §6.4, §6.5): an accepted `continue` keeps the
campaign in its run directory -- the block `authorization_applied`,
`identity_rebased`, `measurement_rebound`* -- carries every unfinished
measurement's scored rows into a re-bound one, and a crash anywhere inside
the block is finished by the next `evolve` without a new decision.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree.campaign import (
    IdentityChanged,
    ReboundSourceCorrupt,
    raised_after_first_write,
)
from evolution.tree.campaign_epochs import admissible_effects, rounds_begun, rounds_remaining
from evolution.tree.campaign_measurement import Measurement, StagePlan
from evolution.tree.campaign_schema import RoundState
from evolution.tree.execution.authorizations import (
    Authorization,
    AuthorizationError,
    RefusalDifference,
)
from evolution.tree.execution.interaction import SemanticIdentity, TargetIdentity
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from tests.campaign_fakes import (
    Crash,
    DeterministicScorer,
    FakeAdapter,
    FakeInstance,
    Rig,
    all_offline_campaign_fixtures,  # noqa: F401  (a pytest fixture)
    cut_before_the_champion_bind,
    v8_policy,
)

pytestmark = pytest.mark.slow


def diff(field: str) -> RefusalDifference:
    return RefusalDifference(field, "a" * 8, "b" * 8)


TARGET = diff("identity.target.settings_hash")
ENVIRONMENT = diff("identity.environment.digest")
STRATEGY = diff("identity.materialization_strategy")
SCORER = diff("identity.scorer.digest")
POLICY = diff("identity.policy_digest")
CORPUS = diff("identity.corpus_digest")
STORE = diff("identity.artifact_store.seed_candidate_digest")
EDITOR = diff("identity.editor.digest")
VERSION = RefusalDifference("producing_version", "0.3.0a16", "0.3.0a17")


# -- admissible_effects: spec §3.3 ---------------------------------------------


def test_a_set_with_no_comparable_field_admits_only_close() -> None:
    """Task 4 carry-forward: no comparable field is no condition change to continue."""

    from evolution.tree.execution.authorizations import RefusalDifference

    assert admissible_effects(7, (), 3) == ("close",)
    editor = (RefusalDifference("identity.editor.digest", "a", "b"),)
    assert admissible_effects(7, editor, 3) == ("close",)


@pytest.mark.parametrize(
    "differences",
    [(TARGET,), (ENVIRONMENT,), (STRATEGY,), (TARGET, ENVIRONMENT, STRATEGY), (TARGET, EDITOR)],
)
def test_an_agent_side_change_admits_continue_rebaseline_and_close(
    differences: tuple[RefusalDifference, ...],
) -> None:
    assert admissible_effects(7, differences, 1) == ("continue", "rebaseline", "close")


@pytest.mark.parametrize("differences", [(TARGET,), (ENVIRONMENT, STRATEGY)])
def test_an_agent_side_change_with_no_round_left_admits_continue_and_close(
    differences: tuple[RefusalDifference, ...],
) -> None:
    assert admissible_effects(7, differences, 0) == ("continue", "close")


@pytest.mark.parametrize(
    "differences",
    [
        (SCORER,),
        (POLICY,),
        (CORPUS,),
        (diff("identity.metric_plan_digest"),),
        (diff("identity.corpus_cases"),),
        (diff("identity.evaluation_set_id"),),
        (TARGET, SCORER),
        (SCORER, VERSION),
    ],
)
def test_a_scale_change_admits_rebaseline_and_close(
    differences: tuple[RefusalDifference, ...],
) -> None:
    assert admissible_effects(7, differences, 2) == ("rebaseline", "close")
    assert admissible_effects(7, differences, 0) == ("close",)


@pytest.mark.parametrize("differences", [(STORE,), (STORE, TARGET), (STORE, SCORER)])
def test_an_artifact_store_change_admits_only_close(
    differences: tuple[RefusalDifference, ...],
) -> None:
    assert admissible_effects(7, differences, 3) == ("close",)
    assert admissible_effects(7, differences, 0) == ("close",)


# -- rounds_remaining: spec §3.2 ------------------------------------------------


def snapshot(first_round: int, rounds: dict[int, RoundState]) -> Any:
    return SimpleNamespace(first_round=first_round, rounds=rounds)


def test_no_round_begun_leaves_the_whole_budget() -> None:
    assert rounds_begun(snapshot(1, {})) == 0
    assert rounds_remaining(snapshot(1, {}), 2) == 2


def test_a_round_is_begun_once_its_baseline_is_bound() -> None:
    unbound = snapshot(1, {1: RoundState(1)})
    assert rounds_remaining(unbound, 2) == 2
    closed = RoundState(1, baseline_measurement_id="m-r01", closed={"outcome": "promoted"})
    open_ = RoundState(2, baseline_measurement_id="m-r02")
    assert rounds_begun(snapshot(1, {1: closed, 2: open_})) == 2
    assert rounds_remaining(snapshot(1, {1: closed, 2: open_}), 2) == 0
    assert rounds_remaining(snapshot(1, {1: closed, 2: open_}), 3) == 1


def test_a_shrunk_budget_leaves_no_round_rather_than_a_negative_count() -> None:
    closed = RoundState(1, baseline_measurement_id="m-r01", closed={"outcome": "promoted"})
    assert rounds_remaining(snapshot(1, {1: closed}), 0) == 0


def test_a_linked_run_counts_its_own_rounds_not_absolute_numbers() -> None:
    """A successor starting at round 3 with `rounds: 1` and that round open has none left."""

    linked = snapshot(3, {3: RoundState(3, baseline_measurement_id="m-r03")})
    assert rounds_begun(linked) == 1
    assert rounds_remaining(linked, 1) == 0
    assert admissible_effects(7, (TARGET,), rounds_remaining(linked, 1)) == ("continue", "close")
    fresh = snapshot(3, {})
    assert rounds_remaining(fresh, 1) == 1


# -- admission reports the effects ---------------------------------------------


class ResettledAdapter(FakeAdapter):
    """The same fake target under other settings: a change of `identity.target`."""

    def _semantic_for(self, dependency_digest: str) -> SemanticIdentity:
        semantic = super()._semantic_for(dependency_digest)
        return SemanticIdentity(
            semantic.contract_digest,
            semantic.dependency_digest,
            "settings-v2",
            semantic.seed_mode,
            semantic.protocol_version,
            semantic.runtime_ref_digest,
        )


async def _crashed(rig: Rig, hook: str) -> None:
    def crash() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={hook: crash})


def _changeable(tmp_path: Path, rounds: int = 1, *, schema: str = SCHEMA_V15) -> Rig:
    """A campaign that accepts a comparable change in place (family 7 onward)."""

    return Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=rounds, attempts_per_round=1, sample_retries=0),
        schema=schema,
    )


async def test_a_begun_last_round_leaves_no_rebaseline(tmp_path: Path) -> None:
    """`rounds: 1` and round 1's baseline is bound: no round remains to begin."""

    rig = _changeable(tmp_path)
    await _crashed(rig, "after_baseline_bound")

    with pytest.raises(IdentityChanged) as target:
        await rig.resume(adapter=ResettledAdapter())
    assert target.value.effects == ("continue", "close")
    assert target.value.effect == "continue"

    with pytest.raises(IdentityChanged) as scorer:
        await rig.resume(scorer=rig.scorer_with_identity_digest("0" * 64))
    assert scorer.value.effects == ("close",)
    assert scorer.value.effect == "close"


async def test_a_hard_change_offers_no_effect(tmp_path: Path) -> None:
    rig = _changeable(tmp_path)
    await rig.run()  # finished: nothing left to continue

    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(adapter=ResettledAdapter())

    assert excinfo.value.hard
    assert excinfo.value.effects == ()


# -- Task 7: the `continue` transition and its recovery (spec §5.2-§5.4) --------


class SettingsAdapter(FakeAdapter):
    """The fake target under named settings; moving them is a change of `identity.target`.

    `leak_candidates` refuses to release every Candidate instance, so a
    measurement interrupted mid-stage still owns one on this target.
    """

    def __init__(
        self, settings: str = "settings-v1", *, leak_candidates: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.settings = settings
        self.leak_candidates = leak_candidates

    def _semantic_for(self, dependency_digest: str) -> SemanticIdentity:
        semantic = super()._semantic_for(dependency_digest)
        return SemanticIdentity(
            semantic.contract_digest,
            semantic.dependency_digest,
            self.settings,
            semantic.seed_mode,
            semantic.protocol_version,
            semantic.runtime_ref_digest,
        )

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
        instance = await super().materialize(candidate, strategy, instance_id)
        if self.leak_candidates and candidate.kind == "candidate":
            self.refuse_release = self.refuse_release | {instance_id}
        return instance

    def turns(self) -> int:
        """Every session any instance of this adapter opened: one per dispatched row."""

        return sum(len(instance.sessions) for instance in self.created.values())


class PowerCut(RuntimeError):
    """A simulated power cut inside a stage."""


def cut_stage(
    monkeypatch: pytest.MonkeyPatch, measurement_id: str, rows: int = 2, *, skip: int = 0
) -> None:
    """A stage of `measurement_id` dispatches `rows` rows, then the power goes.

    `skip` stages of that measurement run whole first.
    """

    original = Measurement.run_stage
    armed = {"skip": skip, "on": True}

    async def partial(self: Measurement, plan: StagePlan, **kwargs: Any) -> Any:
        if armed["on"] and self.measurement_id == measurement_id:
            if armed["skip"]:
                armed["skip"] -= 1
                return await original(self, plan, **kwargs)
            armed["on"] = False
            await original(self, StagePlan(plan.stage, plan.look, plan.rows[:rows]), **kwargs)
            raise PowerCut("power cut")
        return await original(self, plan, **kwargs)

    monkeypatch.setattr(Measurement, "run_stage", partial)


def scored_pairs(rig: Rig, measurement_id: str) -> set[tuple[str, str]]:
    return {
        (r["payload"]["case_id"], r["payload"]["sample_id"])
        for r in rig.measurement_records(measurement_id)
        if r["event"] == "scoring_finished"
    }


def imported_rows(rig: Rig, measurement_id: str) -> list[dict[str, Any]]:
    return [
        r["payload"]
        for r in rig.measurement_records(measurement_id)
        if r["event"] == "sample_imported"
    ]


def payloads(rig: Rig, event: str) -> list[dict[str, Any]]:
    return [r["payload"] for r in rig.records() if r["event"] == event]


async def target_changed_mid_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leak: bool = False,
    run_name: str = "run",
    schema: str = SCHEMA_V15,
) -> tuple[Rig, IdentityChanged]:
    """A family-7 (or family-8, under `SCHEMA_V9`) campaign cut mid-stage of
    its Attempt, resumed under moved target settings."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(leak_candidates=leak),
        run_name=run_name,
        schema=schema,
    )
    cut_stage(monkeypatch, "m-r01-a01")
    with pytest.raises(PowerCut):
        await rig.run()
    monkeypatch.undo()
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=SettingsAdapter("settings-v2"))
    return rig, refused.value


def accept(refused: IdentityChanged, effect: Any = "continue") -> Authorization:
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "the target moved",
        now="2026-09-23T10:00:00Z",
        effect=effect,
    )


async def test_continue_is_refused_for_a_scorer_change(tmp_path: Path) -> None:
    rig = _changeable(tmp_path, rounds=2)
    await _crashed(rig, "after_baseline_bound")
    scorer = rig.scorer_with_identity_digest("0" * 64)
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(scorer=scorer)
    assert refused.value.effects == ("rebaseline", "close")
    before = (rig.run_root / "campaign.jsonl").read_bytes()

    with pytest.raises(AuthorizationError) as invalid:
        await rig.resume(scorer=scorer, authorizations=[accept(refused.value)])

    assert str(invalid.value) == "authorization_invalid at effect"
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before


class DriftingInstance(FakeInstance):
    """A Candidate instance whose target settings move under it after its first session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._drifted = False

    async def identity(self) -> TargetIdentity:
        identity = await super().identity()
        if not self._drifted:
            return identity
        semantic = identity.semantic
        return TargetIdentity(
            SemanticIdentity(
                semantic.contract_digest,
                semantic.dependency_digest,
                "settings-v2",
                semantic.seed_mode,
                semantic.protocol_version,
                semantic.runtime_ref_digest,
            ),
            identity.instance,
            identity.attested,
        )

    async def close(self, session: Any) -> None:
        await super().close(session)
        self._drifted = True


async def test_continue_before_round_one_rebinds_nothing_and_measures_under_the_new_epoch(
    tmp_path: Path,
) -> None:
    rig = _changeable(tmp_path)
    await _crashed(rig, "after_campaign_created")
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=ResettledAdapter())

    result = await rig.resume(adapter=ResettledAdapter(), authorizations=[accept(refused.value)])

    assert result.terminal == "finished"
    assert rig.events()[1:3] == ["authorization_applied", "identity_rebased"]
    assert "measurement_rebound" not in rig.events()
    snapshot = rig.snapshot()
    assert snapshot.measurements["m-r00-champion"]["epoch"] == 1
    assert not snapshot.measurements["m-r00-champion"]["frozen"]


# -- Every measurement the planner still appends to --------------------------------


def _two_attempts(tmp_path: Path, name: str) -> Rig:
    return Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=1, attempts_per_round=2, parallel_attempts=2, sample_retries=0
        ),
        adapter=SettingsAdapter(),
        run_name=name,
    )


async def test_continue_rebinds_the_open_rounds_incomplete_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
    )
    cut_stage(monkeypatch, "m-r00-champion")
    with pytest.raises(PowerCut):
        await rig.run()
    monkeypatch.undo()
    scored = scored_pairs(rig, "m-r00-champion")
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=SettingsAdapter("settings-v2"))

    target = SettingsAdapter("settings-v2")
    result = await rig.resume(adapter=target, authorizations=[accept(refused.value)])

    assert result.terminal == "finished"
    assert [p["measurement_id"] for p in payloads(rig, "measurement_rebound")] == [
        "m-r00-champion@e1"
    ]
    carried = imported_rows(rig, "m-r00-champion@e1")
    assert {(r["case_id"], r["sample_id"]) for r in carried} == scored
    assert payloads(rig, "baseline_complete")[0]["measurement_id"] == "m-r00-champion@e1"
    assert audit(rig) == []  # Task 10: the audit accepts it


# -- Task 8: `rebaseline` and the round boundary (spec §3.2, §3.4, §5.4, §5.5) ---


def _two_rounds(
    tmp_path: Path, name: str = "run", *, schema: str = SCHEMA_V15, **overrides: Any
) -> Rig:
    return Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=overrides.pop("rounds", 2), attempts_per_round=1, sample_retries=0, **overrides
        ),
        adapter=SettingsAdapter(),
        run_name=name,
        schema=schema,
    )


async def _moved(rig: Rig, **overrides: Any) -> IdentityChanged:
    """The refusal of `rig` resumed under moved target settings (or `overrides`)."""

    overrides = overrides or {"adapter": SettingsAdapter("settings-v2")}
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(**overrides)
    return refused.value


def _journal(rig: Rig) -> bytes:
    return (rig.run_root / "campaign.jsonl").read_bytes()


async def _cut_round_one_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "run",
    *,
    schema: str = SCHEMA_V15,
) -> tuple[Rig, IdentityChanged]:
    """Round 1 of 2 is open, its Attempt cut mid-stage; the target then moves."""

    rig = _two_rounds(tmp_path, name, schema=schema)
    cut_stage(monkeypatch, "m-r01-a01")
    with pytest.raises(PowerCut):
        await rig.run()
    monkeypatch.undo()
    return rig, await _moved(rig)


async def test_rebaseline_before_the_focus_portion_closes_the_round_with_empty_coverage(
    tmp_path: Path,
) -> None:
    """Task 5 hand-off: a round closed before its focus portion carries
    coverage visits `[]` and cycle `1`; its incomplete baseline stays frozen."""

    rig = _two_rounds(tmp_path)
    await _crashed(rig, "after_baseline_bound")
    refused = await _moved(rig)

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, "rebaseline")]
    )

    assert result.terminal == "finished"
    closed = payloads(rig, "round_closed")[0]
    assert closed["outcome"] == "condition_changed"
    assert closed["coverage"]["selected_cohort_visits"] == []
    assert closed["coverage"]["cycle"] == 1
    assert "attempt_closed" not in rig.events()[: rig.events().index("round_closed")]
    assert payloads(rig, "baseline_bound")[1]["measurement_id"] == "m-r02-champion@e1"


@pytest.mark.parametrize(
    ("seam", "settled"),
    [
        ("after_grouping_started", "grouping_failed"),
        ("after_reflection_started", "reflection_failed"),
        ("after_editor_started", "editor_failed"),
    ],
)
async def test_a_rebaseline_settles_an_open_role_operation_before_its_block(
    tmp_path: Path, seam: str, settled: str
) -> None:
    """Task 5 carry-forward: the block never opens while a grouping, reflection
    or editor of the open round is started and unfinished -- the executor
    settles it `interrupted` first, and the lifecycle refuses the block
    otherwise (`test_tree_campaign_schema_condition_change`)."""

    rig = _two_rounds(tmp_path)
    await _crashed(rig, seam)
    refused = await _moved(rig)
    before = rig.events()

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, "rebaseline")]
    )

    assert result.terminal == "finished"
    events = rig.events()
    assert events[len(before)] == settled
    last = rig.records()[len(before)]["payload"]
    assert last.get("code", last.get("error_class")) == "interrupted"
    assert events[len(before) + 1 : len(before) + 3] == [
        "authorization_applied",
        "identity_rebased",
    ]
    assert payloads(rig, "round_closed")[0]["outcome"] == "condition_changed"


class Spy:
    """What the campaign did between two seams: its actions and the target's turns."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, target: SettingsAdapter) -> None:
        from evolution.tree import campaign as campaign_module

        self.target = target
        self.actions: list[str] = []
        self.watching = True
        original = campaign_module._Campaign.perform

        async def perform(campaign: Any, action: Any) -> None:
            if self.watching:
                self.actions.append(type(action).__name__)
            await original(campaign, action)

        monkeypatch.setattr(campaign_module._Campaign, "perform", perform)

    def stop(self) -> None:
        if self.watching:
            self.watching = False
            self.turns = self.target.turns()
            self.created = len(self.target.created)


async def _selected_round_one(
    tmp_path: Path, name: str = "run", *, schema: str = SCHEMA_V15
) -> tuple[Rig, IdentityChanged]:
    """Round 1 of 2 decided its selection (a promotion) before the target moved."""

    rig = _two_rounds(tmp_path, name, schema=schema)
    await _crashed(rig, "after_selection_decided")
    assert rig.events()[-1] == "selection_decided"
    return rig, await _moved(rig)


async def test_a_rebaseline_over_a_durable_selection_finishes_the_round_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, refused = await _selected_round_one(tmp_path)
    before = rig.events()
    target = SettingsAdapter("settings-v2")
    spy = Spy(monkeypatch, target)

    result = await rig.resume(
        adapter=target,
        authorizations=[accept(refused, "rebaseline")],
        _hooks={"after_condition_applied": spy.stop},
    )

    assert result.terminal == "finished"
    # The held span decided the round and did nothing else: no measurement
    # action and no target call (the target has already changed).
    assert spy.actions == ["DecideRound"]
    assert spy.turns == 0 and spy.created == 0
    events = rig.events()
    assert events[len(before) : len(before) + 4] == [
        "round_closed",
        "authorization_applied",
        "identity_rebased",
        "baseline_bound",
    ]
    assert payloads(rig, "round_closed")[0]["outcome"] == "promoted"
    # The promoted winner's completion is replaced by the fresh baseline.
    assert "champion_complete" not in events
    bound = payloads(rig, "baseline_bound")[1]
    assert bound["measurement_id"] == "m-r02-champion@e1"
    assert bound["candidate_digest"] == payloads(rig, "round_closed")[0]["champion_digest"]
    change = rig.snapshot().condition_changes[0]
    assert change["effect"] == "rebaseline" and change["round"] is None
    assert audit(rig) == []  # Task 10: the audit accepts it


async def test_a_crash_inside_the_held_span_leaves_the_decision_to_match_again(
    tmp_path: Path,
) -> None:
    rig, refused = await _selected_round_one(tmp_path)
    decision = accept(refused, "rebaseline")

    def cut() -> None:
        raise PowerCut("inside the held span")

    with pytest.raises(PowerCut):
        await rig.resume(
            adapter=SettingsAdapter("settings-v2"),
            authorizations=[decision],
            _hooks={"after_round_closed": cut},
        )
    assert rig.events()[-1] == "round_closed"
    assert "authorization_applied" not in rig.events()  # the decision is unconsumed

    # The next `evolve` matches the same decision, now between rounds.
    again = await _moved(rig)
    assert again.instance_digest == refused.instance_digest
    result = await rig.resume(adapter=SettingsAdapter("settings-v2"), authorizations=[decision])

    assert result.terminal == "finished"
    events = rig.events()
    at = events.index("round_closed")
    assert events[at + 1 : at + 4] == [
        "authorization_applied",
        "identity_rebased",
        "baseline_bound",
    ]
    assert payloads(rig, "baseline_bound")[1]["measurement_id"] == "m-r02-champion@e1"
    assert "champion_complete" not in events


async def test_a_rebaseline_between_rounds_replaces_the_owed_winner_completion(
    tmp_path: Path,
) -> None:
    rig = _two_rounds(tmp_path)
    await _crashed(rig, "after_round_closed")
    assert payloads(rig, "round_closed")[0]["outcome"] == "promoted"
    refused = await _moved(rig)
    before = _journal(rig)
    winner = rig.measurement_records("m-r01-a01")

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, "rebaseline")]
    )

    assert result.terminal == "finished"
    assert _journal(rig)[: len(before)] == before
    assert "champion_complete" not in rig.events()
    assert rig.measurement_records("m-r01-a01") == winner
    assert payloads(rig, "baseline_bound")[1]["measurement_id"] == "m-r02-champion@e1"
    assert audit(rig) == []  # Task 10: the audit accepts it


async def test_a_rebaseline_before_round_one_binds_the_ordinary_baseline_in_the_new_epoch(
    tmp_path: Path,
) -> None:
    rig = _two_rounds(tmp_path)
    await _crashed(rig, "after_campaign_created")
    refused = await _moved(rig)

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, "rebaseline")]
    )

    assert result.terminal == "finished"
    assert rig.events()[1:4] == ["authorization_applied", "identity_rebased", "baseline_bound"]
    snapshot = rig.snapshot()
    assert snapshot.rebaseline_from_round is None
    assert snapshot.measurements["m-r00-champion"]["epoch"] == 1
    assert "m-r00-champion@e1" not in snapshot.measurements
    assert audit(rig) == []  # Task 10: the audit accepts it


async def test_a_scorer_change_under_rebaseline_measures_the_fresh_baseline_with_the_new_scorer(
    tmp_path: Path,
) -> None:
    rig = _two_rounds(tmp_path)
    await _crashed(rig, "after_round_closed")
    scorer = rig.scorer_with_identity_digest("0" * 64)
    refused = await _moved(rig, scorer=scorer)
    assert refused.effects == ("rebaseline", "close")
    before = _journal(rig)

    result = await rig.resume(scorer=scorer, authorizations=[accept(refused, "default")])

    assert result.terminal == "finished"
    assert _journal(rig)[: len(before)] == before
    fresh = rig.measurement_records("m-r02-champion@e1")
    assert fresh[0]["payload"]["identity"]["scorer"]["digest"] == "0" * 64
    old = rig.measurement_records("m-r00-champion")
    assert old[0]["payload"]["identity"]["scorer"]["digest"] != "0" * 64
    snapshot = rig.snapshot()
    assert snapshot.epochs[1]["scorer"]["digest"] == "0" * 64
    assert snapshot.measurements["m-r02-champion@e1"]["epoch"] == 1


async def test_a_cut_before_the_fresh_baseline_is_declared_resumes_in_its_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#106: the fresh `@e1` baseline a `rebaseline` binds, cut before its
    `measurement_bound`, is declared once, in the new epoch, by the same command."""

    rig = _two_rounds(tmp_path)
    await _crashed(rig, "after_round_closed")
    refused = await _moved(rig)
    cut = cut_before_the_champion_bind(monkeypatch, "m-r02-champion@e1")
    target = SettingsAdapter("settings-v2")
    with pytest.raises(Crash):
        await rig.resume(adapter=target, authorizations=[accept(refused, "rebaseline")])
    assert cut == ["m-r02-champion@e1"]
    assert rig.events()[-1] == "baseline_bound"

    result = await rig.resume(adapter=target)

    assert result.terminal == "finished"
    assert [p["round"] for p in payloads(rig, "baseline_bound")] == [1, 2]
    fresh = [
        p for p in payloads(rig, "measurement_bound") if p["measurement_id"] == "m-r02-champion@e1"
    ]
    assert len(fresh) == 1
    assert rig.snapshot().measurements["m-r02-champion@e1"]["epoch"] == 1
    assert audit(rig) == []


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
async def test_a_condition_change_after_a_cut_before_the_champion_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effect: str
) -> None:
    """#106: round 1 names `m-r00-champion`, nothing declares it, and the
    target moves before the resume. `continue` declares it in the new epoch;
    `rebaseline` closes the round and the next binds a fresh baseline."""

    rig = _two_rounds(tmp_path)
    cut_before_the_champion_bind(monkeypatch)
    with pytest.raises(Crash):
        await rig.run()
    assert rig.events()[-1] == "baseline_bound"
    refused = await _moved(rig)
    assert effect in refused.effects

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, effect)]
    )

    assert result.terminal == "finished"
    snapshot = rig.snapshot()
    champion = [
        p for p in payloads(rig, "measurement_bound") if p["measurement_id"] == "m-r00-champion"
    ]
    if effect == "continue":
        assert len(champion) == 1
        assert snapshot.measurements["m-r00-champion"]["epoch"] == 1
    else:
        assert champion == []
        assert payloads(rig, "round_closed")[0]["outcome"] == "condition_changed"
        assert snapshot.measurements["m-r02-champion@e1"]["epoch"] == 1
    assert audit(rig) == []


async def _inheriting_before_round_one(tmp_path: Path) -> tuple[Rig, Rig, IdentityChanged]:
    """A successor declaring a baseline import, cut before round 1; the target moves."""

    from tests.test_tree_campaign import champion_path, successor_editor

    first = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        run_name="first",
    )
    assert (await first.run()).finished is not None
    second = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=2, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        editor=successor_editor(),
        run_name="second",
    )
    second.seed_root = champion_path(first)
    with pytest.raises(RuntimeError):
        await second.run(
            linked_from=first.run_root,
            linked_baseline="inherit",
            _hooks={"after_campaign_created": lambda: (_ for _ in ()).throw(RuntimeError())},
        )
    declared = second.records()[0]["payload"]["identity"]["linked_from"]["baseline"]
    assert declared["imported"] > 0
    refused = await _moved(
        second, linked_from=first.run_root, adapter=SettingsAdapter("settings-v2")
    )
    return first, second, refused


async def test_continue_before_round_one_births_the_declared_import_as_declared(
    tmp_path: Path,
) -> None:
    first, second, refused = await _inheriting_before_round_one(tmp_path)

    result = await second.resume(
        linked_from=first.run_root,
        adapter=SettingsAdapter("settings-v2"),
        authorizations=[accept(refused, "continue")],
    )

    assert result.terminal == "finished"
    declared = second.records()[0]["payload"]["identity"]["linked_from"]["baseline"]
    rows = imported_rows(second, "m-r00-champion")
    assert len(rows) == declared["imported"]
    snapshot = second.snapshot()
    assert snapshot.baseline_abandoned is None
    assert snapshot.measurements["m-r00-champion"]["epoch"] == 1


async def test_rebaseline_before_round_one_births_the_baseline_empty_and_abandons_the_import(
    tmp_path: Path,
) -> None:
    first, second, refused = await _inheriting_before_round_one(tmp_path)
    decision = accept(refused, "rebaseline")

    result = await second.resume(
        linked_from=first.run_root,
        adapter=SettingsAdapter("settings-v2"),
        authorizations=[decision],
    )

    assert result.terminal == "finished"
    assert not imported_rows(second, "m-r00-champion")
    snapshot = second.snapshot()
    assert snapshot.baseline_abandoned == {"authorization_id": decision.id}
    baseline = second.projection()["linked_from"]["baseline"]
    assert baseline["abandoned"] is True
    assert baseline["authorization_id"] == decision.id
    assert audit(second) == []  # Task 10: the audit accepts it


# -- A scale change under `rebaseline`, in every position --------------------------


class Flat(DeterministicScorer):
    """Every Candidate scores like the champion: the round stalls, no winner."""

    def value_for(self, candidate_digest: str, case_id: str) -> float:  # type: ignore[override]
        return 0.5


async def test_a_held_rebaseline_admits_only_the_rounds_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held span that would finish the campaign instead of deciding the round
    is a bug, never a silent drop of the operator's decision."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.campaign_view import FinishCampaign

    rig, refused = await _selected_round_one(tmp_path)
    before = _journal(rig)
    monkeypatch.setattr(
        campaign_module,
        "next_actions",
        lambda view, policy: (FinishCampaign("completed", "rounds_exhausted", "none"),),
    )

    with pytest.raises(AssertionError, match="a held rebaseline admits no"):
        await rig.resume(
            adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused, "rebaseline")]
        )

    assert _journal(rig) == before


# -- Task 10: reports and the audit (spec §4.4, §5) ------------------------------


def audit(rig: Rig) -> list[dict[str, Any]]:
    """Every mismatch `inspect_campaign(..., verify=True)` reports on `rig`."""

    verification = rig.projection(verify=True)["verification"]
    assert verification is not None
    return list(verification["mismatches"])


def _reported(rig: Rig) -> list[dict[str, Any]]:
    """The result and the status document, read back off disk."""

    return [rig.result_payload(), rig.projection()]


# -- Task 10: each audit rule catches a hand-tampered journal copy ---------------


def _copy(rig: Rig, tmp_path: Path, name: str) -> Path:
    """A hand-tamperable copy of `rig`'s run root (the original stays untouched)."""

    import shutil

    target = tmp_path / f"tampered-{name}"
    shutil.copytree(rig.run_root, target)
    for path in target.rglob("*"):
        path.chmod(path.stat().st_mode | 0o200)
    return target


def _lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().splitlines(keepends=True) if line.strip()]


def _findings(root: Path, **kwargs: Any) -> list[tuple[str, str, str]]:
    from evolution.tree.campaign_projections import verify_evidence

    return [
        (str(m["code"]), str(m["event"]), str(m["detail"]))
        for m in verify_evidence(root, **kwargs)["mismatches"]
    ]


# -- Task 10: the audit derives from the binding epoch (Task 2 carry-forwards) ---


def _drift(run_root: Path) -> list[str]:
    """Measurements whose audit epoch identity is not the lifecycle's (drift guard)."""

    from evolution.tree.campaign_epochs import epoch_identity
    from evolution.tree.campaign_projections import _evidence_of, inspect_view

    context, _log = _evidence_of(run_root)
    snapshot = inspect_view(run_root).snapshot
    assert snapshot.measurements
    return [
        measurement_id
        for measurement_id in snapshot.measurements
        if context.identity_for(measurement_id).digest
        != epoch_identity(snapshot, measurement_id).digest
    ]


# -- final fix wave: R19, a re-bound winner cites the rows that decided it -----


# -- final fix wave: a corpus change and a crash inside the rebaseline block ---


async def test_final_champion_after_a_continue_names_the_rebound_seed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 11 deferred: with no promotion, the report's `final_champion` is the
    latest round's bound baseline -- the re-bound `m-r00-champion@e1`, complete."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        scorer=Flat(),
    )
    cut_stage(monkeypatch, "m-r00-champion")
    with pytest.raises(PowerCut):
        await rig.run()
    monkeypatch.undo()
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=SettingsAdapter("settings-v2"))

    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused.value)]
    )

    assert result.terminal == "finished"
    assert payloads(rig, "round_closed")[0]["outcome"] == "stalled"
    for document in _reported(rig):
        champion = document["final_champion"]
        assert champion["measurement_id"] == "m-r00-champion@e1"
        assert champion["measurement_complete"] is True
        assert champion["baseline_origin"] == "seed"
    assert audit(rig) == []


# -- A re-bound stage counts every slot once -----------------------------------


def _crossing(rig: Rig) -> dict[str, Any]:
    suspended = payloads(rig, "campaign_suspended")
    assert len(suspended) == 1
    return dict(suspended[0]["evidence"]["crossing"])


async def test_a_rebound_source_with_no_open_stage_releases_nothing(tmp_path: Path) -> None:
    """A source that never opened a stage reserved no slot: `released: 0`."""

    rig = _changeable(tmp_path)
    await _crashed(rig, "after_baseline_bound")
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=ResettledAdapter())

    await rig.resume(adapter=ResettledAdapter(), authorizations=[accept(refused.value)])

    (rebound,) = payloads(rig, "measurement_rebound")
    assert rebound["from_measurement_id"] == "m-r00-champion"
    assert rebound["released"] == 0
    assert audit(rig) == []


async def _threshold_then_continue(tmp_path: Path, name: str, *, reserve: bool) -> Rig:
    """A failing baseline pair crosses the strict target threshold; with
    `reserve`, a resume reserves its repair and the power is cut before the
    repair starts. Then the target's settings move and `continue` is accepted."""

    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign import seed_key

    lenient = {
        owner: {"max_count": 99, "max_rate": "1"}
        for owner in ("target", "environment", "scorer", "rollout")
    }
    rig = Rig(
        tmp_path,
        campaign_policy=v8_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=0,
            indeterminate={**lenient, "target": {"max_count": 0, "max_rate": "1"}},
        ),
        adapter=SettingsAdapter(),
        run_name=name,
    )
    rig.adapter.dispositions[seed_key(rig)] = "target_failed"
    suspended = await rig.run()
    assert suspended.terminal == "suspended"
    if reserve:

        def crash() -> None:
            raise Crash("power cut after repairs_dispatched")

        with pytest.raises(Crash):
            await rig.resume(_hooks={"after_repairs_dispatched": crash})
        assert rig.events()[-1] == "repairs_dispatched"
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=SettingsAdapter("settings-v2"))
    result = await rig.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused.value)]
    )
    assert result.terminal == "finished", result
    return rig


async def test_continue_releases_an_unspent_threshold_reservation(tmp_path: Path) -> None:
    """#78 (spec §3.5, codex round 2): a reservation whose repairs never started
    leaves the unchanged owners' windows when continue freezes its measurement."""

    reserved = await _threshold_then_continue(tmp_path, "reserved", reserve=True)
    plain = await _threshold_then_continue(tmp_path, "plain", reserve=False)

    assert [p["count"] for p in payloads(reserved, "repairs_dispatched")] == [1]
    (rebound,) = payloads(reserved, "measurement_rebound")
    assert rebound["from_measurement_id"] == "m-r00-champion"
    assert rebound["released"] == 1  # the stage was complete: only the reservation
    (unreserved,) = payloads(plain, "measurement_rebound")
    assert unreserved["released"] == 0
    assert reserved.snapshot().window_denominator_for(
        "rollout"
    ) == plain.snapshot().window_denominator_for("rollout")
    assert reserved.snapshot().denominator == plain.snapshot().denominator
    for rig in (reserved, plain):
        assert rig.projection(verify=True)["verification"]["mismatches"] == []


async def test_a_new_run_has_written_once_its_creation_record_is_durable(tmp_path: Path) -> None:
    """Codex branch review round 1: `_Campaign.create` marks the first write, so
    an I2 refusal type raised after `campaign_created` is not reported as a
    refusal of an untouched run directory."""

    rig = _changeable(tmp_path)

    def corrupt() -> None:
        raise ReboundSourceCorrupt("m-r00-champion")

    with pytest.raises(ReboundSourceCorrupt) as raised:
        await rig.run(_hooks={"after_campaign_created": corrupt})

    assert rig.events() == ["campaign_created"]
    assert raised_after_first_write(raised.value)


@pytest.mark.parametrize("key,value", [("min_tries", 20), ("count_recovered", False)])
async def test_declaring_indeterminate_controls_on_resume_offers_rebaseline_and_close(
    tmp_path, monkeypatch, key, value
):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path, monkeypatch, cut="after_attempt_input_frozen", campaign={"rounds": 2}
        )

    def configure(document):
        document["campaign"]["indeterminate"]["target"][key] = value

    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=configure, campaign={"rounds": 2})
    assert caught.value.code == "run_identity_changed"
    assert caught.value.effects == ("rebaseline", "close")


@pytest.mark.parametrize("retries", [0, 9])
async def test_a_changed_preflight_sample_retries_is_a_rebase(tmp_path, monkeypatch, retries):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import audit_mismatches
    from tests.test_tree_campaign_family14_run import payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_attempt_input_frozen")

    def configure(document):
        document["target"]["preflight_sample_retries"] = retries

    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=configure)
    refused = caught.value
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    assert [d.field for d in refused.differences] == ["identity.preflight_sample_retries"]
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        authorizations=[_decision(refused, None, reason="accept preflight retries")],
    )
    assert result.terminal == "finished"
    (created,) = payloads(run, "campaign_created")
    assert "preflight_sample_retries" not in created["identity"]
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["identity"]["preflight_sample_retries"] == retries
    assert audit_mismatches(run) == []
