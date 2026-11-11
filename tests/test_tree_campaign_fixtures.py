"""Task 8: the spec §13 acceptance fixtures, one test per bullet.

Every test here is named after the §13 bullet it proves and runs the whole
stack — the R1 in-memory adapter, a scripted Environment, a deterministic
Scorer, a scripted `CandidateEditor` and real S1, S2 and campaign journals on
disk. Each asserts on the three logs (`campaign.jsonl`, `artifacts.jsonl` and
the S1 `measurements/<id>/events.jsonl`) and on the §12 projections, and every
run root is checked for §4 containment before the test returns.

Bullets already proven by an earlier task's module are not duplicated here; the
execution record's fixture table names the test that carries each one.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.artifacts.artifact_errors import ArtifactCapacityError
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.artifacts.artifacts import ArtifactStore, SelectionDecision
from evolution.tree.campaign import (
    CorpusTooLarge,
    RecordMatchesSecret,
    UndeclaredSibling,
    inspect_campaign,
    secret_location,
)
from evolution.tree.campaign_identity import CapabilitiesInsufficient, measurement_id_for
from evolution.tree.campaign_measurement import Measurement, StagePlan
from evolution.tree.campaign_schema import (
    FINAL_CAMPAIGN_FAMILY,
)
from evolution.tree.campaign_view import attempt_id_for, classify_store
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.interaction import (
    EvalCase,
    InstanceProvenance,
    RolloutEnvironmentError,
    ScoreResult,
    TargetIdentity,
)
from evolution.tree.execution.journal import MAX_RECORD_BYTES, Journal
from evolution.tree.execution.journal_identity import IdentityError, canonical_json
from evolution.tree.execution.rollout_scoring import ScoringError
from evolution.tree.roles import editor_contracts
from evolution.tree.roles.editor_contracts import EditResult
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.statistics import GatePlan, ScoreSample, gate_at_look
from evolution.tree.storage.append_log import AppendLog
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalFault
from tests.campaign_fakes import (
    ARTIFACT_POLICY,
    RANDOM_SEED,
    Crash,
    DeterministicScorer,
    EditorScript,
    FakeAdapter,
    FakeInstance,
    PromotingScorer,
    Rig,
    ScriptedEditor,
    ScriptedEnvironment,
    all_attempt_ids,
    corpus,
    edits,
    policy,
    v5_policy,
)
from tests.test_tree_campaign_projections import (
    _forge,
    _lines,
    _prefix_reference,
    _rewrite,
)

pytestmark = pytest.mark.slow

# -- shared readers -------------------------------------------------------

#: Exactly the run-root entries §4 assigns to C1, S1 and S2, plus
#: `scorer-traces`, W1's own output root, which the fixtures' scorer now writes
#: because the Reflector's evidence is read from it. The Reflector's read-only
#: private copy of the Parent (spec §3.3) lives in the temporary directory, not
#: in the run root.
CONTAINED = frozenset(
    {
        "campaign.jsonl",
        ".campaign.lock",
        "artifacts.jsonl",
        ".artifacts.lock",
        "artifacts",
        "measurements",
        "scorer-traces",
        # ADR-0029 and PDR-0030: /15 owns connection evidence and role logs.
        "connection",
        "connection-logs",
        "reflector-logs",
    }
)


@pytest.fixture(autouse=True)
def _scratch(tmp_path: Path) -> Iterator[Path]:
    """A private temporary directory, so `assert_contained` can list it.

    Its own `MonkeyPatch`, because several tests call `monkeypatch.undo()`
    before their containment check.
    """

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(scratch))
        yield scratch


def assert_contained(run_root: Path) -> None:
    """§13 *Containment*: the run root holds only the §4 entries."""

    assert set(entry.name for entry in run_root.iterdir()) <= CONTAINED
    # Every private projection is discarded as its reflection returns, so a
    # quiescent run leaves none behind in the (patched) temporary directory.
    assert tempfile.tempdir is not None
    assert list(Path(tempfile.tempdir).glob("evo-tree-editor-*")) == []
    measurements = run_root / "measurements"
    if measurements.is_dir():
        for entry in measurements.iterdir():
            assert entry.is_dir()
            assert set(child.name for child in entry.iterdir()) <= {
                "events.jsonl",
                ".owner.lock",
            }


def records_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


def assert_verified(run_root: Path) -> None:
    """Every E record's evidence *and* its own E-precondition re-verify (ADR-0008).

    Asserted on the fixtures that produce the record kinds the happy path of the
    §13 evidence fixture does not reach — an indeterminate observation, a barred
    look, a `campaign_finished` that names a deciding sample.
    """

    verification = inspect_campaign(run_root, verify=True).to_payload()["verification"]
    assert verification["mismatches"] == []


def ledger_of(run_root: Path) -> list[dict[str, Any]]:
    return records_of(run_root / "artifacts.jsonl")


def measurement_records(run_root: Path, measurement_id: str) -> list[dict[str, Any]]:
    return records_of(run_root / "measurements" / measurement_id / "events.jsonl")


def payloads(rig: Rig, event: str) -> list[dict[str, Any]]:
    return [record["payload"] for record in rig.records() if record["event"] == event]


def s2_champion(rig: Rig) -> str | None:
    lineage = rig_status(rig)["lineage"]
    return None if lineage is None else lineage["champion"]


def rig_status(rig: Rig) -> dict[str, Any]:
    return inspect_campaign(rig.run_root).to_payload()


def seed_digest_of(rig: Rig) -> str:
    return scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest


#: Thresholds no fixture reaches, so a fixture observes exclusion, not a crossing.
LENIENT = {
    owner: {"max_count": 100, "max_rate": "1"}
    for owner in ("target", "environment", "scorer", "rollout")
}


def digest_of(tmp_path: Path, text: str, name: str) -> str:
    """The Candidate digest a `ScriptedEditor` edit of `skill.py` will produce."""

    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.py").write_text(text, encoding="utf-8")
    return scan_tree(root, ARTIFACT_POLICY).candidate_digest


# -- happy path -----------------------------------------------------------


async def test_happy_path(tmp_path: Path) -> None:
    """Two rounds, one promotion then one stall; the champion is S2's own."""

    attempt_ids = all_attempt_ids(policy(rounds=2, attempts_per_round=2))
    script = edits(*attempt_ids)
    # Three attempts seal a Candidate; the fourth editor declines.
    script[attempt_ids[-1]] = EditorScript("declined", note="nothing to do")
    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=2, attempts_per_round=2, parallel_attempts=2, max_concurrency=2
        ),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
        editor=ScriptedEditor(script),
    )

    result = await rig.run()

    assert result.terminal == "finished"
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == (
        "completed",
        "rounds_exhausted",
    )
    outcomes = [row["outcome"] for row in payloads(rig, "round_closed")]
    assert outcomes == ["promoted", "stalled"]
    sealed = {
        row["payload"]["artifact_id"]
        for row in ledger_of(rig.run_root)
        if "artifact_id" in row["payload"]
    }
    assert len([entry for entry in sealed if entry.startswith("r0")]) == 3
    assert result.result is not None
    payload = result.result.to_payload()
    assert payload["claim_scope"] == "campaign"
    assert payload["final_champion"]["version"] == s2_champion(rig)
    assert payload["final_champion"]["measurement_complete"] is True
    assert payload["complete"] is True
    assert_contained(rig.run_root)


async def test_containment(tmp_path: Path) -> None:
    """§13 *Containment*: nothing but the §4 entries, on a resumed run too."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_editor_finished() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_editor_finished": after_editor_finished})
    assert_contained(rig.run_root)

    result = await rig.resume()

    assert result.terminal == "finished"
    assert_contained(rig.run_root)


async def test_completion(tmp_path: Path) -> None:
    """A promotion and a stall finish with the same `campaign_finished` shape."""

    promoted = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await promoted.run()
    stalled = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        run_name="stall",
    )
    stalled.scorer = DeterministicScorer()
    stalled.editor = ScriptedEditor({attempt_id_for(1, 1): EditorScript("declined")})
    await stalled.run()

    first = payloads(promoted, "campaign_finished")[0]
    second = payloads(stalled, "campaign_finished")[0]
    assert payloads(promoted, "round_closed")[0]["outcome"] == "promoted"
    assert payloads(stalled, "round_closed")[0]["outcome"] == "unscored"
    assert sorted(first) == sorted(second)
    for record in (first, second):
        assert (record["outcome"], record["reason"]) == ("completed", "rounds_exhausted")
        # From family 6, a completed campaign continues as "more rounds from the champion".
        assert record["continuation"] == {"kind": "linked_run", "reason": "rounds_exhausted"}
    assert_contained(promoted.run_root)
    assert_contained(stalled.run_root)


async def test_stalled_baseline_reuse(tmp_path: Path) -> None:
    """A stalled round 1 binds the seed champion's measurement again for round 2."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=2, attempts_per_round=1))
    rig.scorer = DeterministicScorer()
    rig.editor = ScriptedEditor(
        {attempt_id: EditorScript("declined") for attempt_id in all_attempt_ids(rig.policy)}
    )

    result = await rig.run()

    assert result.terminal == "finished"
    assert result.finished is not None
    assert result.finished["outcome"] == "completed"
    bound = payloads(rig, "baseline_bound")
    seed = measurement_id_for(None)
    assert [row["measurement_id"] for row in bound] == [seed, seed]
    assert [row["round"] for row in bound] == [1, 2]
    # The seed measurement is created once and reused, never re-created.
    assert rig.events().count("measurement_bound") == 1
    assert [row["outcome"] for row in payloads(rig, "round_closed")] == ["unscored", "unscored"]
    assert result.result is not None
    assert result.result.to_payload()["final_champion"]["version"] == "v0"
    assert_contained(rig.run_root)


# -- target instances, schedule and statistics ----------------------------


async def test_target_instances(tmp_path: Path) -> None:
    """The bound holds at every instant, every instance is released, resume refreshes."""

    # A policy whose peak demand exceeds the preflight `max_instances` is
    # refused before a single byte is written.
    refused = Rig(
        tmp_path,
        campaign_policy=policy(attempts_per_round=4, parallel_attempts=4, max_concurrency=4),
        adapter=FakeAdapter(max_instances=2, max_concurrent_sessions=1),
        run_name="refused",
    )
    with pytest.raises(CapabilitiesInsufficient):
        await refused.run()
    assert not (refused.run_root / "campaign.jsonl").exists()

    # An admitted run never holds more instances than the target admitted and
    # releases every one of them before its terminal.
    clean = FakeAdapter(max_instances=4, max_concurrent_sessions=2)
    whole = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1, attempts_per_round=2, parallel_attempts=2, max_concurrency=2
        ),
        adapter=clean,
        run_name="whole",
    )
    assert (await whole.run()).terminal == "finished"
    assert clean.peak_live <= clean.capabilities.max_instances
    assert set(clean.materialized) == set(clean.released)
    assert not clean.live
    assert_contained(whole.run_root)

    adapter = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    rig = Rig(
        tmp_path,
        # Two looks, so the torn stage is not the last one and its instances
        # are still owned when the crash lands.
        campaign_policy=policy(
            rounds=1,
            attempts_per_round=1,
            max_concurrency=2,
            alpha={"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}},
        ),
        adapter=adapter,
    )

    def partial(original: Any) -> Any:
        async def torn(self: Any, plan: Any, **kwargs: Any) -> Any:
            await original(self, StagePlan(plan.stage, plan.look, plan.rows[:1]), **kwargs)
            raise Crash("power cut")

        return torn

    original = Measurement.run_stage
    Measurement.run_stage = partial(original)  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            await rig.run()
    finally:
        Measurement.run_stage = original  # type: ignore[method-assign]
    stale = tuple(adapter.live)
    assert stale

    fresh = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    result = await rig.resume(adapter=fresh)

    assert result.terminal == "finished"
    # R1 recovery released every id the crashed run left behind ...
    assert set(stale) <= set(fresh.released)
    # ... and the resumed run materialized -- and released -- its own.
    assert set(fresh.materialized) - set(stale)
    assert set(fresh.materialized) <= set(fresh.released)
    assert fresh.peak_live <= fresh.capabilities.max_instances
    assert not fresh.live
    assert_contained(rig.run_root)


async def test_schedule(tmp_path: Path) -> None:
    """`exact` pairs by `random_seed + repeat`; `unpaired` carries the side, no seed."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run()

    seed = measurement_id_for(None)
    winner = payloads(rig, "selection_decided")[0]["attempt_id"]
    champion_rows = _schedule_of(rig.run_root, seed)
    candidate_rows = _schedule_of(rig.run_root, f"m-{winner}")
    assert champion_rows == candidate_rows
    for row in champion_rows:
        assert row["requested_seed"] == RANDOM_SEED + row["repeat"]
        assert row["sample_id"] == f"{row['repeat']:02d}"

    unpaired = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=FakeAdapter(seed_mode="unpaired"),
        run_name="unpaired",
    )
    await unpaired.run()
    rows = _schedule_of(unpaired.run_root, seed)
    for row in rows:
        assert row["requested_seed"] is None
        assert row["sample_id"] == f"champion-{row['repeat']:02d}"
    assert_contained(rig.run_root)
    assert_contained(unpaired.run_root)


def _schedule_of(run_root: Path, measurement_id: str) -> list[dict[str, Any]]:
    created = measurement_records(run_root, measurement_id)[0]
    return [dict(row) for row in created["payload"]["identity"]["schedule"]]


class RebootingAdapter(FakeAdapter):
    """Every materialization boots a fresh instance, so no two share a `boot_id`."""

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
        self.live.pop(instance_id, None)
        return await super().materialize(candidate, strategy, instance_id)


async def test_statistics_adaptation(tmp_path: Path) -> None:
    """Different `boot_id`s still evaluate under `exact`; a `none` target gets a plan."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=RebootingAdapter(),
    )
    result = await rig.run()

    boots = {instance.boot_id for instance in rig.adapter.created.values()}
    assert len(boots) > 1
    # ADR-0032: family 14 records Candidate checks as comparison_look_evaluated.
    assert result.terminal == "finished"
    assert payloads(rig, "comparison_look_evaluated")

    none_mode = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=FakeAdapter(seed_mode="none"),
        run_name="none-mode",
    )
    none_result = await none_mode.run()

    assert none_result.terminal == "finished"
    created = measurement_records(none_mode.run_root, measurement_id_for(None))[0]
    identity = created["payload"]["identity"]
    assert identity["target"]["seed_mode"] == "none"
    assert all(row["requested_seed"] is None for row in identity["schedule"])
    assert payloads(none_mode, "comparison_look_evaluated")

    # A mismatched `dependency_digest` across the two sides is still refused,
    # whatever the seed mode (spec §8, `statistics._validate_exact_proof`).
    plan = GatePlan("exact", RANDOM_SEED, {3: Fraction("0.01")})
    left = tuple(_score_sample(index, "dep-a") for index in range(1, 4))
    right = tuple(_score_sample(index, "dep-b") for index in range(1, 4))
    with pytest.raises(ValueError, match="dependency digest"):
        gate_at_look(left, right, plan, 3)

    assert_contained(rig.run_root)
    assert_contained(none_mode.run_root)


def _score_sample(repeat: int, dependency_digest: str) -> ScoreSample:
    return ScoreSample(
        case_id="case-01",
        sample_id=f"{repeat:02d}",
        score=repeat / 10.0,
        requested_seed=RANDOM_SEED + repeat,
        effective_seed=RANDOM_SEED + repeat,
        boot_id=f"boot-{repeat}",
        dependency_digest=dependency_digest,
        group_id="group-01",
        stratum="stratum-01",
    )


# -- declaration, birth and identity --------------------------------------


async def test_seed_changed_between_scans(tmp_path: Path) -> None:
    """A seed root altered before `ArtifactStore.create` refuses on resume."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})
    assert rig.events() == ["campaign_created"]
    assert classify_store(rig.run_root) != "born"

    original = (rig.seed_root / "skill.py").read_bytes()
    (rig.seed_root / "skill.py").write_bytes(b"VALUE = 99\n")
    with pytest.raises(IdentityError) as excinfo:
        await rig.resume()
    assert str(excinfo.value) == "run_identity_changed"
    assert classify_store(rig.run_root) != "born"

    (rig.seed_root / "skill.py").write_bytes(original)
    result = await rig.resume()

    assert result.terminal == "finished"
    assert classify_store(rig.run_root) == "born"
    assert_contained(rig.run_root)


async def test_unborn_child(tmp_path: Path) -> None:
    """An unborn store and an unborn measurement both resume by removal."""

    store_rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await store_rig.run(_hooks={"after_campaign_created": after_campaign_created})
    (store_rig.run_root / "artifacts.jsonl").write_bytes(b"")
    assert classify_store(store_rig.run_root) == "unborn"

    assert (await store_rig.resume()).terminal == "finished"
    assert classify_store(store_rig.run_root) == "born"
    assert_contained(store_rig.run_root)

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), run_name="child")

    def after_measurement_bound() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_measurement_bound": after_measurement_bound})
    seed = measurement_id_for(None)
    directory = rig.run_root / "measurements" / seed
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events.jsonl").write_bytes(b"")
    assert Measurement.classify(rig.run_root, seed) == "unborn"

    assert (await rig.resume()).terminal == "finished"
    assert Measurement.classify(rig.run_root, seed) == "born"
    assert_contained(rig.run_root)


async def test_declare_then_create(tmp_path: Path) -> None:
    """Both declarations are adopted on resume; an undeclared sibling is refused."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_measurement_bound() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_measurement_bound": after_measurement_bound})
    assert rig.events() == ["campaign_created", "baseline_bound", "measurement_bound"]
    born = rig.run_root / "measurements" / measurement_id_for(None) / "events.jsonl"
    assert not born.exists()

    # A directory nothing declared is a foreign writer, refused before a write.
    foreign = rig.run_root / "measurements" / "m-foreign"
    foreign.mkdir(parents=True)
    with pytest.raises(UndeclaredSibling):
        await rig.resume()
    foreign.rmdir()

    result = await rig.resume()

    assert result.terminal == "finished"
    assert born.exists()
    # A born sibling is adopted, never removed and recreated: the seed
    # measurement's journal holds exactly one `run_created`. The two
    # `measurement_bound` records are two *different* measurements -- the
    # baseline the crash interrupted, re-declared on resume, and the round's one
    # attempt -- not the same one declared twice.
    assert [row["event"] for row in records_of(born)].count("run_created") == 1
    bound = [
        row["payload"]["measurement_id"]
        for row in rig.records()
        if row["event"] == "measurement_bound"
    ]
    assert bound == [measurement_id_for(None), f"m-{attempt_id_for(1, 1)}"]
    assert_contained(rig.run_root)


# -- alpha, accounting and the round barrier ------------------------------


async def test_exactly_once_accounting(tmp_path: Path) -> None:
    """A crash after `indeterminate_observed` never re-counts the sample on resume."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1,
            attempts_per_round=1,
            budget={"max_turns": 4, "timeout_seconds": 30.0},
            indeterminate={
                owner: {"max_count": 2, "max_rate": "1"}
                for owner in ("target", "environment", "scorer", "rollout")
            },
        ),
    )
    # The indeterminate lands in the *attempt* stage, so an `attempt_closed`
    # is the record the crash lands before.
    candidate = digest_of(tmp_path, "VALUE = 2\n", "candidate-tree")
    rig.adapter = FakeAdapter(dispositions={(candidate, "case-01", "02"): "time_budget"})

    original = campaign_module._Campaign.close_attempt

    async def crash_before_close(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise Crash("power cut")

    campaign_module._Campaign.close_attempt = crash_before_close  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            await rig.run()
    finally:
        campaign_module._Campaign.close_attempt = original  # type: ignore[method-assign]

    observed = payloads(rig, "indeterminate_observed")
    assert observed
    assert observed[0]["measurement_id"] == f"m-{attempt_id_for(1, 1)}"
    assert "attempt_closed" not in rig.events()

    result = await rig.resume()

    assert result.terminal == "finished"
    after = payloads(rig, "indeterminate_observed")
    keys = [(row["measurement_id"], row["case_id"], row["sample_id"]) for row in after]
    assert len(keys) == len(set(keys))
    assert after[: len(observed)] == observed
    assert result.status is not None
    counts = result.status.to_payload()["indeterminate"]["counts"]
    assert sum(counts.values()) == len(after)
    assert_contained(rig.run_root)


# -- owners, thresholds and vetoes ----------------------------------------


class Trip:
    """A shared arming flag: the fault only fires once a fixture arms it."""

    def __init__(self, armed: bool = True) -> None:
        self.armed = armed


class TrippingInstance(FakeInstance):
    def __init__(self, trip: Trip, script: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._trip = trip
        self._trip_script = script

    def _script(self, session: Any) -> str:
        return self._trip_script if self._trip.armed else "completed"


class TrippingAdapter(FakeAdapter):
    """A target whose every sample takes `script` while the trip is armed."""

    def __init__(self, trip: Trip, script: str, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.trip = trip
        self.script = script

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        return TrippingInstance(
            self.trip,
            self.script,
            instance_id,
            boot_id,
            self._semantic_for(candidate.dependency_digest),
            candidate.digest,
            self.dispositions,
            self.capabilities.max_concurrent_sessions,
        )


class TrippingEnvironment(ScriptedEnvironment):
    """An Environment that fails its own boundary while the trip is armed."""

    def __init__(self, trip: Trip) -> None:
        super().__init__()
        self.trip = trip

    async def next(self, case: Any, history: Any) -> Any:
        if self.trip.armed:
            raise RolloutEnvironmentError("scripted environment failure")
        return await super().next(case, history)


class TrippingScorer(PromotingScorer):
    """A Scorer that fails its own boundary while the trip is armed."""

    def __init__(self, trip: Trip, seed_digest: str, **overrides: Any) -> None:
        super().__init__(seed_digest, **overrides)
        self.trip = trip

    async def score(self, case: Any, view: Any, candidate: Any) -> Any:
        if self.trip.armed:
            raise ScoringError("scripted scorer failure")
        return await super().score(case, view, candidate)


OWNER_TERMINALS = {
    "target": ("infrastructure_failed", "target_unstable"),
    "environment": ("infrastructure_failed", "environment_unstable"),
    "scorer": ("infrastructure_failed", "scorer_unstable"),
    "rollout": ("budget_exhausted", "rollout_time_budget_exhausted"),
}


class ScorerFailingFor(PromotingScorer):
    """A Scorer that fails only on the named Candidate digests, once armed."""

    def __init__(self, trip: Trip, seed_digest: str, digests: frozenset[str]) -> None:
        super().__init__(seed_digest)
        self.trip = trip
        self.digests = digests

    async def score(self, case: Any, view: Any, candidate: Any) -> Any:
        if self.trip.armed and candidate.digest in self.digests:
            raise ScoringError("scripted scorer failure")
        return await super().score(case, view, candidate)


async def test_critical_veto(tmp_path: Path) -> None:
    """A Candidate critical failure vetoes; a champion one is a brief fact only."""

    cases = corpus(2, critical=("case-01",))
    candidate = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        cases=cases,
        run_name="candidate-veto",
    )
    candidate.scorer = CriticalFor(
        seed_digest_of(candidate),
        frozenset({digest_of(tmp_path, "VALUE = 2\n", "veto-a")}),
    )
    result = await candidate.run()

    assert result.terminal == "finished"
    assert "look_evaluated" not in candidate.events()
    assert "alpha_consumed" not in candidate.events()
    closed = payloads(candidate, "attempt_closed")[0]
    assert (closed["outcome"], closed["reason"]) == ("rejected", "critical_failure")
    assert_contained(candidate.run_root)

    briefs: list[Any] = []

    class Recording(ScriptedEditor):
        async def edit(self, request: Any) -> Any:
            briefs.append(request.brief)
            return await super().edit(request)

    champion = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        cases=cases,
        run_name="champion-veto",
    )
    seed = seed_digest_of(champion)
    champion.scorer = CriticalFor(seed, frozenset({seed}))
    champion.editor = Recording(edits(*all_attempt_ids(champion.policy)))
    champion_result = await champion.run()

    assert champion_result.terminal == "finished"
    # The champion's critical failure never closes an attempt; it reaches the
    # editor as a fact of the baseline and nothing else.
    assert "attempt_opened" in champion.events()
    assert briefs
    flags = [
        repeat.critical_pass
        for case in briefs[0].cases
        for repeat in case.repeats
        if case.case_id == "case-01"
    ]
    assert False in flags
    rejected = [
        row for row in payloads(champion, "attempt_closed") if row["reason"] == "critical_failure"
    ]
    assert not rejected
    assert_contained(champion.run_root)


class CriticalFor(PromotingScorer):
    """`critical_pass is False` for exactly the named Candidate digests."""

    def __init__(self, seed_digest: str, digests: frozenset[str]) -> None:
        super().__init__(seed_digest)
        self.digests = digests

    async def score(self, case: Any, view: Any, candidate: Any) -> Any:
        result = await super().score(case, view, candidate)
        if case.oracle.get("critical") is True and candidate.digest in self.digests:
            return ScoreResult(result.metrics, False)
        return result


# -- looks, promotion and the round decision ------------------------------


@pytest.mark.parametrize("point", ["selection_decided", "promotion_pending", "promotion_committed"])
async def test_interrupted_promotion(
    tmp_path: Path, point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each crash point resumes to the same champion and exactly one `round_closed`."""

    # Two attempts, so the round has a published loser to forge a decision with.
    campaign_policy = policy(rounds=1, attempts_per_round=2, parallel_attempts=2, max_concurrency=2)
    reference = Rig(
        tmp_path,
        campaign_policy=campaign_policy,
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
        run_name="whole",
    )
    assert (await reference.run()).terminal == "finished"
    expected = payloads(reference, "round_closed")[0]

    rig = Rig(
        tmp_path,
        campaign_policy=campaign_policy,
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
        run_name=f"torn-{point}",
    )

    def crash() -> None:
        raise Crash("power cut")

    hooks: dict[str, Any] = {}
    if point == "selection_decided":
        hooks["after_selection_decided"] = crash
    elif point == "promotion_pending":
        hooks["after_pending"] = crash
    else:
        # S2 has no `after_committed` seam, so the crash lands on C1's own
        # `round_closed` append: `promotion_committed` is already durable.
        original = AppendLog.append

        def faulting(self: Any, run_id: str, event: str, *args: Any, **kwargs: Any) -> int:
            if event == "round_closed":
                raise Crash("power cut")
            return original(self, run_id, event, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(AppendLog, "append", faulting)

    with pytest.raises(Crash):
        await rig.run(_hooks=hooks)
    monkeypatch.undo()
    assert "round_closed" not in rig.events()
    if point == "promotion_committed":
        assert [row["event"] for row in ledger_of(rig.run_root)].count("promotion_committed") == 1

    result = await rig.resume()

    assert result.terminal == "finished"
    closed = payloads(rig, "round_closed")
    assert len(closed) == 1
    assert (closed[0]["outcome"], closed[0]["champion_version"]) == (
        expected["outcome"],
        expected["champion_version"],
    )
    assert closed[0]["champion_digest"] == expected["champion_digest"]
    # S2 committed exactly one champion for the round.
    committed = [
        row["payload"] for row in ledger_of(rig.run_root) if row["event"] == "promotion_committed"
    ]
    assert len(committed) == 1
    assert s2_champion(rig) == closed[0]["champion_version"]

    # A forged different decision for the same round is refused by S2.
    pending = [
        row["payload"] for row in ledger_of(rig.run_root) if row["event"] == "promotion_pending"
    ][0]
    selection = payloads(rig, "selection_decided")[0]
    loser = next(
        row for row in selection["ranking"] if row["attempt_id"] != selection["attempt_id"]
    )
    store = ArtifactStore.resume(rig.run_root, ARTIFACT_POLICY)
    try:
        with pytest.raises(ArtifactError) as excinfo:
            store.promote(
                SelectionDecision(
                    round_id=1,
                    attempt_id=loser["attempt_id"],
                    candidate_digest=_published_digest(rig.run_root, loser["attempt_id"]),
                    dependency_digest=pending["dependency_digest"],
                    decision_ref=dict(pending["decision_ref"]),
                )
            )
        assert getattr(excinfo.value, "code", None) == "round_decided"
    finally:
        store.close()
    assert_contained(rig.run_root)


def _published_digest(run_root: Path, artifact_id: str) -> str:
    return next(
        row["payload"]["candidate_digest"]
        for row in ledger_of(run_root)
        if row["payload"].get("artifact_id") == artifact_id
    )


FOREIGN_DIGEST = "f" * 64


class LyingAdapter(FakeAdapter):
    """Attests an identity it never loaded, in one of the two digests §8 admits.

    `lie="candidate"` moves `InstanceProvenance.loaded_candidate_digest`, which is
    what the campaign lifecycle checks; `lie="dependency"` moves
    `SemanticIdentity.dependency_digest`, which it does not. By default only past
    the seed champion, so the baseline still measures; `lie_about_the_seed` makes
    the very first materialization lie. `honest_first` attests that many
    qualifying instances truthfully before it starts lying, so a measurement that
    materializes several instances has attested rows of its own when the lie
    lands.
    """

    def __init__(
        self,
        seed_digest: str,
        *,
        lie: str = "candidate",
        lie_about_the_seed: bool = False,
        honest_first: int = 0,
        **overrides: Any,
    ) -> None:
        super().__init__(**overrides)
        self.seed_digest = seed_digest
        self.lie = lie
        self.lie_about_the_seed = lie_about_the_seed
        self.honest_first = honest_first
        self._qualified = 0

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        instance = super().instance_for(candidate, instance_id, boot_id)
        if not (self.lie_about_the_seed or candidate.digest != self.seed_digest):
            return instance
        self._qualified += 1
        if self._qualified <= self.honest_first:
            return instance
        identity = instance._identity
        if self.lie == "candidate":
            instance._identity = TargetIdentity(
                identity.semantic,
                InstanceProvenance(boot_id, FOREIGN_DIGEST),
                identity.attested,
            )
        else:
            instance._identity = TargetIdentity(
                replace(identity.semantic, dependency_digest=FOREIGN_DIGEST),
                identity.instance,
                identity.attested,
            )
        return instance


async def test_promotion_admission_refuses_a_foreign_dependency_row(tmp_path: Path) -> None:
    """`admitted()` runs R3's verifier over the durable identity (§8, ADR-0007).

    No live target reaches this: R1 stops a mismatching instance at the sample
    boundary (above). The check still has to hold for a journal some other writer
    produced, so it is proved over the record list itself — including the half a
    digest comparison cannot see, an attested set that is missing a required
    field while both digests match.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    assert (await rig.run()).terminal == "finished"

    session = _session_over(rig)
    try:
        snapshot = session.snapshot()
        winner_id = payloads(rig, "selection_decided")[0]["attempt_id"]
        winner = snapshot.rounds[1].attempts[winner_id]
        assert winner.measurement_id is not None
        assert session.admitted(winner) is True

        for field, value in (
            ("dependency_digest", FOREIGN_DIGEST),
            ("loaded_candidate_digest", FOREIGN_DIGEST),
            ("attested", None),
        ):
            forged = _with_forged_instances(snapshot, winner.measurement_id, field, value)
            if field == "attested":
                # Both flat digests still say exactly what the sealed Candidate
                # says; only the attested set is short, which is what the
                # hand-rolled digest comparison used to miss.
                rows = forged.measurements[winner.measurement_id]["instances"]
                assert all(
                    row["loaded_candidate_digest"] == winner.candidate_digest for row in rows
                )
                assert all(row["dependency_digest"] == winner.dependency_digest for row in rows)
            session.snapshot = lambda forged=forged: forged  # type: ignore[assignment, misc]
            assert session.admitted(winner) is False

        # An identity that cannot be parsed at all is refused, never guessed.
        broken = _with_forged_instances(snapshot, winner.measurement_id, "identity", None)
        session.snapshot = lambda broken=broken: broken  # type: ignore[assignment, misc]
        assert session.admitted(winner) is False

        # No attested row at all is refused the same way.
        empty = _with_forged_instances(snapshot, winner.measurement_id, None, None)
        session.snapshot = lambda empty=empty: empty  # type: ignore[assignment, misc]
        assert session.admitted(winner) is False
    finally:
        session.close()
    assert_contained(rig.run_root)


def _session_over(rig: Rig) -> Any:
    """A `_Campaign` bound to a finished run root, for record-list assertions."""

    session = campaign_module._Campaign(
        rig.policy,
        run_root=rig.run_root,
        seed_root=rig.seed_root,
        artifact_policy=ARTIFACT_POLICY,
        adapter=rig.adapter,
        environment=rig.environment,
        scorer=rig.scorer,
        editor=rig.editor,
        reflector=rig.reflector,
        environment_docs=None,
        corpus=rig.cases,
        adaptive=rig.adaptive,
        cancelled=lambda: False,
        secrets=(),
        hooks=None,
    )
    session.open_log(create=False)
    session.log.scan()
    session.run_id = rig.records()[0]["run_id"]
    return session


def _forged_row(row: Mapping[str, Any], field: str, value: str | None) -> dict[str, Any]:
    """One `instances` row with `field` moved, in the row *and* in its identity.

    `admitted()` verifies the durable `identity` payload, so a forgery that only
    moved a flat digest would prove nothing about the check under test; the two
    are kept in the agreement the lifecycle itself requires. `field = "attested"`
    is the identity-only forgery: a required attested field goes missing while
    both digests still match the sealed Candidate. `field = "identity"` replaces
    the payload with something unparseable.
    """

    forged = dict(row)
    identity = dict(forged["identity"])
    identity["semantic"] = dict(identity["semantic"])
    identity["instance"] = dict(identity["instance"])
    if field == "identity":
        forged["identity"] = value
        return forged
    if field == "attested":
        identity["attested"] = sorted(set(identity["attested"]) - {"dependency_digest"})
    elif field == "dependency_digest":
        forged[field] = value
        identity["semantic"]["dependency_digest"] = value
    else:
        forged[field] = value
        identity["instance"]["loaded_candidate_digest"] = value
    forged["identity"] = identity
    return forged


def _with_forged_instances(
    snapshot: Any, measurement_id: str, field: str | None, value: str | None
) -> Any:
    """The same snapshot with one measurement's `instances` rewritten or emptied."""

    measurements = {key: dict(entry) for key, entry in snapshot.measurements.items()}
    entry = measurements[measurement_id]
    if field is None:
        entry["instances"] = ()
    else:
        entry["instances"] = tuple(_forged_row(row, field, value) for row in entry["instances"])
    return replace(snapshot, measurements=measurements)


async def test_lifecycle_recomputes_the_selector(tmp_path: Path) -> None:
    """A forged selection and a forged stall are both refused by the lifecycle."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1, attempts_per_round=2, parallel_attempts=2, max_concurrency=2
        ),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
    )
    await rig.run()
    before = (rig.run_root / "campaign.jsonl").read_bytes()
    selection = payloads(rig, "selection_decided")[0]
    ranking = selection["ranking"]
    assert len(ranking) == 2

    session = campaign_module._Campaign(
        rig.policy,
        run_root=rig.run_root,
        seed_root=rig.seed_root,
        artifact_policy=ARTIFACT_POLICY,
        adapter=rig.adapter,
        environment=rig.environment,
        scorer=rig.scorer,
        editor=rig.editor,
        reflector=rig.reflector,
        environment_docs=None,
        corpus=rig.cases,
        adaptive=rig.adaptive,
        cancelled=lambda: False,
        secrets=(),
        hooks=None,
    )
    session.open_log(create=False)
    try:
        session.log.scan()  # type: ignore[union-attr]
        session.run_id = rig.records()[0]["run_id"]
        loser = next(row for row in ranking if row["attempt_id"] != selection["attempt_id"])
        with pytest.raises(campaign_module.CampaignInvariantViolated):
            session.append(
                "selection_decided",
                dict(
                    round=1,
                    attempt_id=loser["attempt_id"],
                    candidate_digest=selection["candidate_digest"],
                    dependency_digest=selection["dependency_digest"],
                    ranking=ranking,
                    evidence=selection["evidence"],
                ),
            )
        with pytest.raises(campaign_module.CampaignInvariantViolated):
            session.append(
                "round_closed",
                dict(
                    round=1,
                    outcome="stalled",
                    champion_version="v99",
                    champion_digest="a" * 64,
                    evidence=None,
                ),
            )
        assert session.log.healthy is True  # type: ignore[union-attr]
    finally:
        session.close()
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before
    assert_contained(rig.run_root)


# -- the baseline barrier and the linked run ------------------------------


class LadderScorer(DeterministicScorer):
    """A fixed score per Candidate digest, so every round's delta is a constant."""

    def __init__(self, values: Mapping[str, float]) -> None:
        super().__init__()
        self.values = dict(values)

    def value_for(self, candidate_digest: str, case_id: str) -> float:  # type: ignore[override]
        return self.values.get(candidate_digest, 0.0)


def _two_look_policy(**overrides: Any) -> Any:
    return policy(
        rounds=2,
        attempts_per_round=1,
        alpha={"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}},
        **overrides,
    )


async def test_baseline_barrier(tmp_path: Path) -> None:
    """From family 6, an exhausted baseline sample is excluded, never a terminal (spec §4.4).

    Every repeat of `case-01` is exhausted, so the baseline excludes those pairs,
    completes, and the round's attempt is evaluated on the pairs that remain.
    """

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=0,
            indeterminate=LENIENT,
            budget={"max_turns": 4, "timeout_seconds": 30.0},
        ),
        # Three cases: the two that keep their pairs form the larger failing
        # cohort, so the round's focus is theirs whichever tie the grouping
        # would otherwise break.
        cases=corpus(3),
    )
    rig.adapter = FakeAdapter(
        dispositions={
            (seed_digest_of(rig), "case-01", f"{repeat:02d}"): "time_budget" for repeat in (1, 2, 3)
        }
    )

    result = await rig.run()

    # ADR-0032: family 14 records Candidate checks as comparison_look_evaluated.
    assert result.terminal == "finished"
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == (
        "completed",
        "rounds_exhausted",
    )
    assert result.finished["continuation"] == {"kind": "linked_run", "reason": "rounds_exhausted"}
    excluded = payloads(rig, "pairs_excluded")[0]
    assert (excluded["count"], excluded["by_owner"]) == (3, {"rollout": 3})
    events = rig.events()
    assert events.index("pairs_excluded") < events.index("baseline_complete")
    assert "attempt_opened" in events
    assert "comparison_look_evaluated" in events and "look_not_started" not in events
    assert_contained(rig.run_root)

    # Every family-6 terminal offers the linked run, a completed one included.
    finished = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        run_name="finished",
    )
    await finished.run()
    assert payloads(finished, "campaign_finished")[0]["continuation"] == {
        "kind": "linked_run",
        "reason": "rounds_exhausted",
    }


# -- faults, the editor and the terminal barrier ---------------------------


async def test_unchanged_edit(tmp_path: Path) -> None:
    """An untouched workspace is `editor_no_change`; a foreign path is `seal_refused`."""

    unchanged = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        editor=ScriptedEditor({attempt_id_for(1, 1): EditorScript("edit", writes={})}),
        run_name="unchanged",
    )
    result = await unchanged.run()

    assert result.terminal == "finished"
    closed = payloads(unchanged, "attempt_closed")[0]
    assert (closed["outcome"], closed["reason"]) == ("abandoned", "editor_no_change")
    assert "attempt_sealed" not in unchanged.events()
    sealed = [
        row for row in ledger_of(unchanged.run_root) if row["event"] == "artifact_copy_started"
    ]
    assert [row["payload"]["artifact_id"] for row in sealed] == ["v0"]
    assert_contained(unchanged.run_root)

    outside = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        editor=ScriptedEditor(
            {attempt_id_for(1, 1): EditorScript("edit", writes={"other.py": b"X = 1\n"})}
        ),
        run_name="outside",
    )
    outside_result = await outside.run()

    assert outside_result.terminal == "finished"
    refused = payloads(outside, "attempt_closed")[0]
    assert (refused["outcome"], refused["reason"]) == ("abandoned", "seal_refused")
    assert refused["code"]
    assert_contained(outside.run_root)


async def test_terminal_barrier(tmp_path: Path) -> None:
    """Cancellation waits for a cooperative editor and abandons only its attempt."""

    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    class Cancelling:
        def __init__(self) -> None:
            self.identity = EditorIdentity("cancelling", "d" * 64)

        async def edit(self, request: Any) -> Any:
            stop()
            try:
                await asyncio.sleep(3600.0)
            except asyncio.CancelledError:
                # Cooperative: the workspace bytes written after the cancel are
                # never sealed, because the attempt is already abandoned.
                (request.workspace / "skill.py").write_bytes(b"VALUE = 999\n")
                raise
            raise AssertionError("the editor was not cancelled")

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=2))
    rig.editor = Cancelling()  # type: ignore[assignment]

    result = await rig.run(cancelled=lambda: stopped)

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")
    failed = payloads(rig, "editor_failed")
    assert failed and failed[0]["error_class"] == "cancelled"
    closed = payloads(rig, "attempt_closed")
    assert [(row["outcome"], row["reason"]) for row in closed] == [("abandoned", "editor_failed")]
    assert "selection_decided" not in rig.events()
    # The bytes the cancelled editor wrote are in no published artifact.
    assert not [
        row
        for row in ledger_of(rig.run_root)
        if row["payload"].get("artifact_id", "").startswith("r0")
    ]
    for published in (rig.run_root / "artifacts").rglob("skill.py"):
        assert published.read_bytes() != b"VALUE = 999\n"
    assert_contained(rig.run_root)


async def test_cancellation_bars_an_editor_that_finished_anyway(tmp_path: Path) -> None:
    """Spec §7: cancellation stops new publication even from an editor that returned.

    `run_editor` polls the token only while the task is unfinished, so an editor
    that sets the caller's token and returns `edited` inside one poll window
    leaves the loop as an ordinary success. Nothing it wrote may be published:
    the editor's own terminal is recorded truthfully — it did finish — and the
    attempt closes `abandoned(cancelled)`, which abandons the workspace and with
    it the seal eligibility, so no `attempt_sealed`, no measurement and no
    further stage follow.
    """

    stopped = False

    class Racing:
        """Sets the token and returns in the same poll window `run_editor` waits."""

        def __init__(self) -> None:
            self.identity = EditorIdentity("racing", "d" * 64)

        async def edit(self, request: Any) -> EditResult:
            nonlocal stopped
            (request.workspace / "skill.py").write_bytes(b"VALUE = 999\n")
            stopped = True
            return EditResult("edited", "raced the token")

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    rig.editor = Racing()  # type: ignore[assignment]

    result = await rig.run(cancelled=lambda: stopped)

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")

    events = rig.events()
    finished = payloads(rig, "editor_finished")
    assert finished and finished[0]["status"] == "edited"
    assert "editor_failed" not in events
    closed = payloads(rig, "attempt_closed")
    assert [(row["outcome"], row["reason"]) for row in closed] == [("abandoned", "cancelled")]
    assert "attempt_sealed" not in events
    # Only the baseline was ever bound and measured: the cancelled attempt opened
    # no measurement and no second stage.
    assert [row["measurement_id"] for row in payloads(rig, "measurement_bound")] == [
        measurement_id_for(None)
    ]
    assert {row["measurement_id"] for row in payloads(rig, "stage_opened")} == {
        measurement_id_for(None)
    }
    assert "selection_decided" not in events
    assert events[-1] == "campaign_finished"

    # The bytes that editor wrote are in no published artifact.
    assert not [
        row
        for row in ledger_of(rig.run_root)
        if row["payload"].get("artifact_id", "").startswith("r0")
    ]
    for published in (rig.run_root / "artifacts").rglob("skill.py"):
        assert published.read_bytes() != b"VALUE = 999\n"
    assert_contained(rig.run_root)


async def test_cancellation_during_an_attempt_stage_opens_no_further_stage(
    tmp_path: Path,
) -> None:
    """The mirror: the token set while an attempt stage runs (spec §7 barrier).

    §7 forbids cancelling a sample, so the stage that is running finishes its own
    rows — each one scored, or `rollout_cancelled` if the token beat its start —
    and the attempt then takes no new step at all: no look is evaluated, no alpha
    charged, no second stage opened.
    """

    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    class CancellingScorer(PromotingScorer):
        """Sets the caller's token the first time a Candidate's sample is scored."""

        async def score(self, case: Any, view: Any, candidate: Any) -> Any:
            if candidate.digest != self.seed_digest:
                stop()
            return await super().score(case, view, candidate)

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=1),
    )
    rig.scorer = CancellingScorer(seed_digest_of(rig))

    result = await rig.run(cancelled=lambda: stopped)

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")

    attempt = f"m-{attempt_id_for(1, 1)}"
    opened = [row for row in payloads(rig, "stage_opened") if row["measurement_id"] == attempt]
    assert len(opened) == 1
    started, terminal = _started_and_terminal(rig.run_root, attempt)
    assert started == terminal
    outcomes = {
        row["payload"]["outcome"]
        for row in measurement_records(rig.run_root, attempt)
        if row["event"] in {"rollout_finished", "rollout_failed"}
    }
    # R2a's `cancelled` outcome is `exclude(rollout, rollout_cancelled)`.
    assert outcomes <= {"completed", "cancelled"}
    assert "look_evaluated" not in rig.events()
    assert "alpha_consumed" not in rig.events()
    assert "selection_decided" not in rig.events()
    assert rig.events()[-1] == "campaign_finished"
    assert_contained(rig.run_root)


# -- capacity, persistence, determinism and the canaries ------------------


async def test_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three capacity fault sites drain, publish nothing and report the terminal."""

    # 1. `materialize_candidate` runs out of room: nothing opens, the terminal
    # is typed and the campaign log carries the `capacity_exhausted` record.
    store_rig = Rig(
        tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), run_name="store"
    )

    def after_store_created() -> None:
        raise ArtifactCapacityError("storage_capacity_exhausted")

    store_result = await store_rig.run(_hooks={"after_store_created": after_store_created})

    assert store_result.terminal == "storage_capacity_exhausted"
    assert (store_result.owner, store_result.code) == (
        "artifact_store",
        "storage_capacity_exhausted",
    )
    assert store_result.finished is None
    assert store_rig.events() == ["campaign_created", "capacity_exhausted"]
    assert_contained(store_rig.run_root)

    # Resume with capacity still short refuses the same way: the S2 preflight
    # of §11 step 0 runs before anything else.
    def short(*args: Any, **kwargs: Any) -> None:
        raise ArtifactCapacityError("storage_capacity_exhausted")

    monkeypatch.setattr(campaign_module.ArtifactStore, "preflight", short)
    again = await store_rig.resume()
    assert again.terminal == "storage_capacity_exhausted"
    monkeypatch.undo()

    # Resume once capacity is back continues to the same champion an
    # uninterrupted run reaches.
    reference = Rig(
        tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), run_name="whole"
    )
    await reference.run()
    recovered = await store_rig.resume()
    assert recovered.terminal == "finished"
    assert (
        payloads(store_rig, "round_closed")[0]["champion_digest"]
        == (payloads(reference, "round_closed")[0]["champion_digest"])
    )
    assert_contained(store_rig.run_root)

    # 2. An S1 `storage_capacity_exhausted` inside a measurement names its own
    # owner and the measurement journal it faulted on.
    measurement_rig = Rig(
        tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), run_name="s1"
    )

    def broken_measurement(self: Any, imported: Any = ()) -> None:
        raise JournalFault("storage_capacity_exhausted")

    monkeypatch.setattr(campaign_module.Measurement, "create", broken_measurement)
    measurement_result = await measurement_rig.run()
    monkeypatch.undo()

    assert measurement_result.terminal == "storage_capacity_exhausted"
    assert (measurement_result.owner, measurement_result.code) == (
        "measurement_journal",
        "storage_capacity_exhausted",
    )
    capacity = payloads(measurement_rig, "capacity_exhausted")
    assert capacity[0]["owner"] == "measurement_journal"
    assert capacity[0]["measurement_id"] == measurement_id_for(None)
    assert "selection_decided" not in measurement_rig.events()
    assert_contained(measurement_rig.run_root)

    # 3. A capacity-coded fault on `campaign.jsonl` itself leaves no
    # `capacity_exhausted` record and the terminal is still reported.
    log_rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), run_name="log")
    original = AppendLog.append

    def faulting(self: Any, run_id: str, event: str, *args: Any, **kwargs: Any) -> int:
        if event == "baseline_bound":
            self._healthy = False
            raise JournalFault("storage_capacity_exhausted")
        return original(self, run_id, event, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AppendLog, "append", faulting)
    log_result = await log_rig.run()
    monkeypatch.undo()

    assert log_result.terminal == "storage_capacity_exhausted"
    assert (log_result.owner, log_result.code) == ("campaign_journal", "storage_capacity_exhausted")
    assert log_rig.events() == ["campaign_created"]
    assert "capacity_exhausted" not in log_rig.events()
    assert_contained(log_rig.run_root)


def _fault_on_scoring(
    monkeypatch: pytest.MonkeyPatch, measurement_id: str, sample_id: str, code: str
) -> None:
    """Fault one sample's `scoring_started` append on its own S1 journal."""

    original = Journal.append

    def faulting(self: Any, event: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> int:
        if (
            event == "scoring_started"
            and self.run_id == measurement_id
            and payload.get("sample_id") == sample_id
        ):
            raise JournalFault(code)
        return original(self, event, payload, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Journal, "append", faulting)


def _poison_on_scoring(
    monkeypatch: pytest.MonkeyPatch, measurement_id: str, sample_id: str, code: str
) -> None:
    """Fault one sample's `scoring_started` append *and poison its writer*.

    `_fault_on_scoring` raises above `AppendLog`, so the log stays healthy and a
    later append still works. A real journal I/O failure does both — `AppendLog.
    append` marks itself unhealthy and refuses every later write — which is what
    makes the barrier's release path a journal-free one.
    """

    original = Journal.append

    def faulting(self: Any, event: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> int:
        if (
            event == "scoring_started"
            and self.run_id == measurement_id
            and payload.get("sample_id") == sample_id
        ):
            self.invalidate()
            raise JournalFault(code)
        return original(self, event, payload, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Journal, "append", faulting)


def _poison_on_release(monkeypatch: pytest.MonkeyPatch, measurement_id: str, code: str) -> None:
    """Fault the first `target_release_started` append *and poison its writer*.

    The release path's own I/O failure: the journal was healthy when the release
    began, and is not once it fails.
    """

    original = Journal.append
    faulted = False

    def faulting(self: Any, event: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> int:
        nonlocal faulted
        if not faulted and event == "target_release_started" and self.run_id == measurement_id:
            faulted = True
            self.invalidate()
            raise JournalFault(code)
        return original(self, event, payload, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Journal, "append", faulting)


def _started_and_terminal(run_root: Path, measurement_id: str) -> tuple[set[Any], set[Any]]:
    rows = measurement_records(run_root, measurement_id)
    started = {
        (row["payload"]["case_id"], row["payload"]["sample_id"])
        for row in rows
        if row["event"] == "rollout_started"
    }
    terminal = {
        (row["payload"]["case_id"], row["payload"]["sample_id"])
        for row in rows
        if row["event"] in {"rollout_finished", "rollout_failed", "rollout_interrupted"}
    }
    return started, terminal


async def test_a_poisoned_measurement_journal_still_gives_the_instances_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7's barrier releases every instance, even when the journal cannot record it.

    A journal I/O failure poisons the measurement's writer, and
    `release_target`'s first act is to append `target_release_started` through
    it — so the release used to raise before the adapter was ever called, and a
    persistence terminal was returned with target instances still live. The
    barrier now releases straight through the adapter when the journal is
    unusable: the terminal is unchanged, and the target is given everything back.
    """

    adapter = FakeAdapter(max_instances=4, max_concurrent_sessions=2)
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=2),
        adapter=adapter,
    )
    seed = measurement_id_for(None)
    _poison_on_scoring(monkeypatch, seed, "01", "journal_unavailable")

    result = await rig.run()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("measurement_journal", "journal_unavailable")
    assert result.quiescent is True
    # Every instance the target handed out came back, and none is live.
    assert adapter.materialized
    assert set(adapter.released) == set(adapter.materialized)
    assert not adapter.live
    # The release could not be journaled, so S1 still shows the instances as
    # owned — R1's own recovery releases by id, exactly as after a crash.
    journal = measurement_records(rig.run_root, seed)
    assert not [row for row in journal if row["event"] == "target_release_started"]
    assert "campaign_finished" not in rig.events()
    assert_contained(rig.run_root)


async def test_a_release_time_journal_fault_is_still_the_persistence_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The barrier's journal-free release does not hide a healthy journal's fault.

    Releasing at a measurement's last look runs inside `run_stage`'s measurement
    boundary, so a journal I/O failure there is the `persistence_failed` terminal
    the operator has to see. The bypass exists for a journal that is *already*
    unusable; on a healthy one the instances still all come back — that is what
    §7 requires — and the fault is raised once they have, not swallowed.
    """

    adapter = FakeAdapter(max_instances=4, max_concurrent_sessions=1)
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=2),
        adapter=adapter,
    )
    seed = measurement_id_for(None)
    # Two instances, so the fault lands on the first release while the journal is
    # healthy and the second takes the poisoned journal's bypass.
    _poison_on_release(monkeypatch, seed, "journal_unavailable")

    result = await rig.run()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("measurement_journal", "journal_unavailable")
    assert result.quiescent is True
    assert len(adapter.materialized) == 2
    assert set(adapter.released) == set(adapter.materialized)
    assert not adapter.live
    # The stage's samples all scored: the failure is the release, not the work.
    assert "campaign_finished" not in rig.events()
    assert "baseline_complete" not in rig.events()
    assert_contained(rig.run_root)


def _bounded_measurement_journals(monkeypatch: pytest.MonkeyPatch, max_bytes: int) -> None:
    """Create every measurement journal with a small admission bound.

    The seam the S1 durability tests use, reached through `Measurement.create`:
    only measurement journals shrink, `campaign.jsonl` keeps its own bound.
    """

    original = Journal.create

    def small(path: Any, run_id: str, identity: Any, cases: Any, **kwargs: Any) -> Journal:
        if "measurements" in Path(path).parts:
            kwargs["max_record_bytes"] = 4096
            kwargs["max_journal_bytes"] = max_bytes
        return original(path, run_id, identity, cases, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Journal, "create", small)


async def test_an_admission_refusal_with_no_durable_start_is_a_persistence_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008: a stage short of its `sample_count` is only ever a persistence terminal.

    A measurement journal that refuses `rollout_started` at its admission bound
    closes nothing — there is no started operation for
    `close_execution("rollout_interrupted")` to close — so that row has no S1
    terminal at all and the stage cannot complete. §7 names
    `journal_limit_exceeded` a persistence terminal, and ADR-0005 makes it
    non-resumable: that is the one admitted exception, and it is what the
    campaign must report instead of evaluating a short stage's consequences.
    """

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=1),
        cases=corpus(4),
    )
    _bounded_measurement_journals(monkeypatch, 70000)

    result = await rig.run()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("measurement_journal", "journal_limit_exceeded")
    assert result.quiescent is True
    seed = measurement_id_for(None)
    # The stage is genuinely short — that is the fact the terminal stands for.
    opened = payloads(rig, "stage_opened")
    assert opened and opened[0]["measurement_id"] == seed
    journal = measurement_records(rig.run_root, seed)
    terminals = [
        row
        for row in journal
        if row["event"] in ("rollout_finished", "rollout_failed", "rollout_interrupted")
    ]
    assert 0 < len(terminals) < opened[0]["sample_count"]
    # No consequence was drawn from it: no completion, no campaign terminal.
    assert "baseline_complete" not in rig.events()
    assert "campaign_finished" not in rig.events()
    assert "indeterminate_observed" not in rig.events()
    status = result.status.to_payload()
    assert seed in {entry["measurement_id"] for entry in status["measurements"]}
    assert_contained(rig.run_root)


async def test_a_corrupt_campaign_suffix_on_resume_is_a_persistence_terminal(
    tmp_path: Path,
) -> None:
    """§7: every persistence fault is a terminal, including a corrupt prefix.

    A complete but unparseable line after `campaign_created` is refused by the
    append log's own scan, so the lifecycle is never adopted. The result has to
    come from a read-only inspection of the valid prefix — the way a resume that
    never opened the log builds it — not from the writer state `adopt` did not
    reach, which projects nothing and used to raise `SchemaError` at the
    projection's own validator instead of returning the terminal.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def crash() -> None:
        raise Crash("power cut right after the first record")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_campaign_created": crash})
    journal = rig.run_root / "campaign.jsonl"
    assert rig.events() == ["campaign_created"]
    journal.write_bytes(journal.read_bytes() + b"{}\n")

    result = await rig.resume()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("campaign_journal", "journal_unavailable")
    assert result.quiescent is True
    assert result.finished is None
    assert result.result is None
    # The valid prefix is what the status projection reports.
    status = result.status.to_payload()
    assert status["kind"] == "campaign_status"
    assert status["run_id"] == rig.records()[0]["run_id"]
    assert status["complete"] is False


async def test_capacity_mid_baseline_sample_resumes_into_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline sample whose own journal faulted mid-sample is retried on resume.

    From family 6 (continuable-terminals spec §3.4): the unscored try is an
    interrupted try that consumes one; the resumed stage takes the next.
    """

    # No threshold can cross, so the retry is what the resume reaches.
    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=1,
            indeterminate={
                owner: {"max_count": 9, "max_rate": "1"}
                for owner in ("target", "environment", "scorer", "rollout")
            },
        ),
    )
    seed = measurement_id_for(None)
    _fault_on_scoring(monkeypatch, seed, "02", "storage_capacity_exhausted")

    faulted = await rig.run()
    monkeypatch.undo()

    assert faulted.terminal == "storage_capacity_exhausted"
    assert (faulted.owner, faulted.code) == ("measurement_journal", "storage_capacity_exhausted")
    started, terminal = _started_and_terminal(rig.run_root, seed)
    assert started == terminal
    # The faulted sample has a durable execution terminal and no scoring one.
    rows = measurement_records(rig.run_root, seed)
    scored = {row["payload"]["sample_id"] for row in rows if row["event"] == "scoring_finished"}
    assert "02" not in scored
    assert "attempt_opened" not in rig.events()

    # Capacity is back: resume reclassifies the unscored sample as interrupted
    # and the resumed stage retries it, so the baseline completes unexcluded.
    result = await rig.resume()

    assert result.terminal == "finished"
    assert result.finished is not None
    assert result.finished["reason"] != "baseline_incomplete"
    assert result.finished["continuation"]["kind"] == "linked_run"
    assert "pairs_excluded" not in rig.events()
    observed = payloads(rig, "indeterminate_observed")
    assert observed
    # Ruling 7: an execution terminal whose scoring never reached one is an
    # interrupted sample at resume, never replayed.
    assert all(
        (row["owner"], row["reason"]) == ("rollout", "rollout_interrupted") for row in observed
    )
    assert "02" in {row["sample_id"] for row in observed}
    assert {row["retry_ordinal"] for row in observed} == {0}
    assert "baseline_complete" in rig.events() and "attempt_opened" in rig.events()
    assert_contained(rig.run_root)


async def test_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-capacity journal or store fault is `persistence_failed` with its owner."""

    limit = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        run_name="limit",
    )

    def over_limit(self: Any, imported: Any = ()) -> None:
        raise JournalFault("journal_limit_exceeded")

    monkeypatch.setattr(campaign_module.Measurement, "create", over_limit)
    result = await limit.run()
    monkeypatch.undo()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("measurement_journal", "journal_limit_exceeded")
    assert "capacity_exhausted" not in limit.events()
    assert_contained(limit.run_root)

    seal = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        run_name="seal",
    )

    def after_seal_finished() -> None:
        raise ArtifactError("io_failure")

    seal_result = await seal.run(_hooks={"after_seal_finished": after_seal_finished})

    assert seal_result.terminal == "persistence_failed"
    assert (seal_result.owner, seal_result.code) == ("artifact_store", "io_failure")
    assert "capacity_exhausted" not in seal.events()
    assert_contained(seal.run_root)

    # Resume surfaces the log's own recovery refusal rather than waiting.
    def broken_resume(*args: Any, **kwargs: Any) -> None:
        raise ArtifactError("io_failure")

    monkeypatch.setattr(campaign_module.ArtifactStore, "resume", broken_resume)
    resumed = await seal.resume()
    assert resumed.terminal == "persistence_failed"
    assert (resumed.owner, resumed.code) == ("artifact_store", "io_failure")


#: Record fields two runs of the same manifest may legitimately differ in:
#: spec §10's sequence and timestamp, the `operation_id` that pairs a look with
#: its charge, and `run_id`, which is per-run identity rather than a decision.
#: `evidence` joins them inside the payload — an E row's references are byte
#: ranges into the measurement journals, which move with the sample interleaving
#: even when the decision does not.
VOLATILE = frozenset({"sequence", "timestamp", "operation_id", "run_id"})


def _without_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One record payload with its byte-range evidence dropped (see `VOLATILE`)."""

    return {key: value for key, value in payload.items() if key != "evidence"}


def _masked(
    record: Mapping[str, Any], *, cohort_cases: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """One campaign record with everything a schedule may legitimately reorder gone."""

    masked = {key: value for key, value in record.items() if key not in VOLATILE}
    payload = {key: value for key, value in record["payload"].items() if key != "evidence"}
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        coverage = dict(coverage)
        # Coverage's counts, cycle, assessment fact and omitted-reference count
        # are decision semantics.  The visit ids are hashes of the independent
        # run's journal-prefix digest, so map them back to the admitted cohort's
        # stable case membership.  Each closure reference embeds that run id,
        # byte schedule and content digest; compare its closed kind/version and
        # list position instead of those per-run locator identities.
        visits = coverage.get("selected_cohort_visits")
        if isinstance(visits, list) and cohort_cases is not None:
            coverage["selected_cohort_visits"] = [cohort_cases[visit] for visit in visits]
        refs = coverage.get("closure_refs")
        if isinstance(refs, list):
            coverage["closure_refs"] = [
                {"kind": ref.get("kind"), "version": ref.get("version")}
                for ref in refs
                if isinstance(ref, dict)
            ]
        payload["coverage"] = coverage
    masked["payload"] = payload
    return masked


def _decisions(rig: Rig) -> list[dict[str, Any]]:
    cohort_cases: dict[int, dict[str, str]] = {}
    for portion in payloads(rig, "focus_portion_selected"):
        assert len(portion["cohort_ids"]) == len(portion["case_ids"])
        cohort_cases[portion["round"]] = dict(
            zip(portion["cohort_ids"], portion["case_ids"], strict=True)
        )
    return [
        _masked(record, cohort_cases=cohort_cases.get(record["payload"].get("round")))
        for record in rig.records()
        if record["event"] in {"selection_decided", "round_closed", "campaign_finished"}
    ]


def _champion_chain(rig: Rig) -> list[tuple[str, str]]:
    return [
        (row["champion_version"], row["champion_digest"]) for row in payloads(rig, "round_closed")
    ]


async def test_determinism(tmp_path: Path) -> None:
    """`parallel_attempts` 1 vs 3 and `max_concurrency` 1 vs 4 decide identically."""

    async def run(parallel_attempts: int, max_concurrency: int) -> Rig:
        campaign_policy = policy(
            rounds=2,
            attempts_per_round=3,
            parallel_attempts=parallel_attempts,
            max_concurrency=max_concurrency,
        )
        rig = Rig(
            tmp_path,
            campaign_policy=campaign_policy,
            adapter=FakeAdapter(max_instances=8, max_concurrent_sessions=4),
            run_name=f"p{parallel_attempts}-c{max_concurrency}",
        )
        assert (await rig.run()).terminal == "finished"
        return rig

    serial = await run(1, 1)
    wide = await run(3, 4)

    assert _decisions(serial) == _decisions(wide)
    assert _champion_chain(serial) == _champion_chain(wide)
    assert [_masked(row) for row in serial.records() if row["event"] == "look_evaluated"] == [
        _masked(row) for row in wide.records() if row["event"] == "look_evaluated"
    ]
    assert_contained(serial.run_root)
    assert_contained(wide.run_root)


CANARIES = (b"PRIVATE-CANARY", b"ORACLE-CANARY", b"SECRET-CANARY", b"note-r01-a01")


async def test_leakage_canaries(tmp_path: Path) -> None:
    """No private, oracle, secret, note or extra-metric byte reaches a log or a brief."""

    briefs: list[Any] = []

    class Recording(ScriptedEditor):
        async def edit(self, request: Any) -> Any:
            briefs.append(request.brief)
            return await super().edit(request)

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        editor=Recording(edits(attempt_id_for(1, 1))),
    )
    rig.scorer = PromotingScorer(seed_digest_of(rig), extra_metric="EXTRA-CANARY")

    result = await rig.run(secrets=["SECRET-CANARY"])

    assert result.terminal == "finished"
    campaign_bytes = (rig.run_root / "campaign.jsonl").read_bytes()
    ledger_bytes = (rig.run_root / "artifacts.jsonl").read_bytes()
    result_bytes = json.dumps(result.result.to_payload() if result.result else {}).encode()
    status_bytes = json.dumps(result.status.to_payload()).encode()
    # The brief exactly as the editor module serialises it, not a restatement.
    brief_bytes = canonical_json(editor_contracts._brief_payload(briefs[0]))

    for canary in CANARIES:
        assert canary not in campaign_bytes
        assert canary not in ledger_bytes
        assert canary not in result_bytes
        assert canary not in status_bytes
        assert canary not in brief_bytes
    # A metric other than `metric.primary` reaches neither the campaign journal,
    # the brief nor C1's own result claim. The status projection embeds S1's
    # measurement status verbatim, and what that reports is S1's own contract.
    for surface in (campaign_bytes, ledger_bytes, result_bytes, brief_bytes):
        assert b"EXTRA-CANARY" not in surface
    # The public half of every case is exactly what the brief carries.
    assert b"PUBLIC-CANARY" in brief_bytes
    assert b"PUBLIC-CANARY" not in campaign_bytes
    assert_contained(rig.run_root)


async def test_evidence_semantic_forgery(tmp_path: Path) -> None:
    """§13 *Evidence references*: the audit re-verifies the fact, not only the digest.

    ADR-0008 asks for evidence "an audit over the view can re-verify". A digest
    check alone cannot: a record another writer produced can cite bytes that
    hash perfectly and still claim something they do not say. Each forgery here
    passes every lifecycle rule and every reference check, and is caught only by
    re-evaluating the record's own E-precondition over the cited bytes.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    assert (await rig.run()).terminal == "finished"
    honest = inspect_campaign(rig.run_root, verify=True).to_payload()["verification"]
    assert honest["mismatches"] == []

    attested = payloads(rig, "instance_attested")[0]
    sealed = payloads(rig, "attempt_sealed")[0]
    forgeries = (
        # An instance the referenced measurement prefix never materialized.
        ("instance_attested", dict(attested, instance_id="run-instance-nowhere")),
        # A Candidate S2 never published under that attempt's publication record.
        ("attempt_sealed", dict(sealed, candidate_digest="c" * 64)),
        # A baseline whose own measurement prefix is intact but incomplete.
        ("baseline_complete", None),
    )
    for event, forged in forgeries:
        run_root = tmp_path / f"forged-{event}"
        shutil.copytree(rig.run_root, run_root)
        if forged is None:
            measurement = measurement_id_for(None)
            forged = dict(
                round=1,
                measurement_id=measurement,
                evidence=[_prefix_reference(run_root, measurement, 2)],
            )
        _forge(run_root, event, forged, instead_of=event)

        verification = inspect_campaign(run_root, verify=True).to_payload()["verification"]

        assert [row["code"] for row in verification["mismatches"]] == ["precondition_failed"], event
        assert verification["mismatches"][0]["event"] == event
        assert verification["mismatches"][0]["detail"]


async def test_a_refused_suffix_is_a_verification_finding(tmp_path: Path) -> None:
    """A clean `verification` must mean the whole file replayed (attack TH-10).

    `inspect_log` stops at the first record the lifecycle refuses and hands back
    the prefix, so a journal whose tail was thrown away used to report no
    mismatch at all. The refusal is now a finding of its own, with the
    lifecycle's own message.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    assert (await rig.run()).terminal == "finished"
    assert_verified(rig.run_root)

    journal = rig.run_root / "campaign.jsonl"
    first = json.loads(_lines(journal)[0])
    forged = Envelope.from_payload(
        dict(
            first,
            sequence=len(_lines(journal)) + 1,
            event="attempt_opened",
            operation_id="forged",
            payload=dict(
                round=1,
                attempt_index=1,
                attempt_id=attempt_id_for(1, 1),
                parent_version="v0",
                parent_digest=seed_digest_of(rig),
            ),
        ),
        family=FINAL_CAMPAIGN_FAMILY,
    )
    journal.write_bytes(journal.read_bytes() + forged.encode())

    verification = inspect_campaign(rig.run_root, verify=True).to_payload()["verification"]

    assert [row["code"] for row in verification["mismatches"]] == ["suffix_refused"]
    assert verification["mismatches"][0]["detail"]
    assert verification["mismatches"][0]["event"] is None


async def test_a_torn_tail_is_not_a_refused_suffix(tmp_path: Path) -> None:
    """A crash mid-append is not a forgery (the re-run's TT-1).

    The append log's own tail rule repairs a torn last line and `resume_campaign`
    continues from it, so the audit reports it by the scan's own label instead of
    accusing the run root of a record the lifecycle refused.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    assert (await rig.run()).terminal == "finished"
    assert_verified(rig.run_root)

    journal = rig.run_root / "campaign.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"schema_version": 1, "run')

    verification = inspect_campaign(rig.run_root, verify=True).to_payload()["verification"]

    assert [row["code"] for row in verification["mismatches"]] == ["incomplete_tail"]
    assert verification["mismatches"][0]["detail"] == "incomplete_tail"


async def test_a_forged_look_not_started_naming_a_scored_sample_is_reported(
    tmp_path: Path,
) -> None:
    """§5: "the named samples are not `score`" — every one of them (sweep, table 2).

    The audit used to accept any `look_not_started` as long as *some* sample of
    a cited prefix was short of the look, so a record could name samples that
    scored. Each named sample is now resolved in its own measurement's schedule
    and checked.
    """

    # The same shape as `test_look_not_started`: one attempt's look 6 is barred
    # by a single indeterminate at repeat 5, so a cited prefix really is short —
    # which is all the audit used to require.
    campaign_policy = policy(
        rounds=1,
        attempts_per_round=1,
        parallel_attempts=1,
        budget={"max_turns": 4, "timeout_seconds": 30.0},
        alpha={"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}},
        indeterminate={
            owner: {"max_count": 3, "max_rate": "0.99"}
            for owner in ("target", "environment", "scorer", "rollout")
        },
    )
    rig = Rig(tmp_path, campaign_policy=campaign_policy, run_name="lns-ok")
    rig.scorer = DeterministicScorer()
    rig.adapter = FakeAdapter(
        dispositions={(digest_of(tmp_path, "VALUE = 2\n", "lns"), "case-01", "05"): "time_budget"}
    )
    assert (await rig.run()).terminal == "finished"
    barred = payloads(rig, "look_not_started")
    # ADR-0032/ADR-0027: the per-Candidate confidence schedule first lacks repeat 5 at look 5.
    assert barred and barred[0]["look"] == 5
    assert_verified(rig.run_root)

    attempt = barred[0]["attempt_id"]
    measurement = f"m-{attempt}"
    run_root = tmp_path / "lns-forged"
    shutil.copytree(rig.run_root, run_root)
    _rewrite(
        run_root,
        "look_not_started",
        dict(
            barred[0],
            missing=[
                dict(
                    measurement_id=measurement,
                    case_id="case-01",
                    sample_id="01",
                    state="not_started",
                )
            ],
        ),
    )

    verification = inspect_campaign(run_root, verify=True).to_payload()["verification"]

    assert [row["code"] for row in verification["mismatches"]] == ["precondition_failed"]
    assert verification["mismatches"][0]["event"] == "look_not_started"
    assert verification["mismatches"][0]["detail"]


# -- the creation probe (C1-identity task 4) -------------------------------


def _big_corpus(n: int, width: int) -> tuple[EvalCase, ...]:
    return tuple(
        EvalCase(f"case-{i:06d}-" + "x" * width, "g", "s", (), {}, {}, {}) for i in range(n)
    )


async def test_a_corpus_the_creation_record_cannot_hold_is_refused_before_the_run_root(
    tmp_path: Path,
) -> None:
    """Spec §3: the bound is the encoded record, exact, and nothing is created."""

    # ~110 bytes per case in the identity; 10 000 of them cross 1 MiB.
    rig = Rig(tmp_path, cases=_big_corpus(10_000, 60))
    with pytest.raises(CorpusTooLarge, match="^corpus_too_large$"):
        # Cancelled immediately: a refused probe never opens a log at all, but
        # an *admitted* one would otherwise run the whole campaign to its
        # `rounds_exhausted` terminal over a 10 000-case corpus.
        await rig.run(cancelled=lambda: True)
    assert not rig.run_root.exists()

    # The boundary is the encoded size: bisect to the largest admitted `n`,
    # then `n + 1` is refused. Each probe is one fake campaign (cheap): the
    # cancellation token stops it at its first barrier, right after
    # `campaign_created` is durable, instead of dispatching any sample. `low`
    # and `high` are themselves earlier `mid`s, so the closing re-checks of
    # `admitted(low)` / `admitted(low + 1)` would probe an `n` a prior
    # iteration already ran -- each call therefore gets its own run root
    # (`calls` makes the name unique) rather than one keyed on `n` alone.
    calls = 0
    admitted_bytes = 0

    async def admitted(n: int) -> bool:
        nonlocal calls, admitted_bytes
        calls += 1
        probe = Rig(tmp_path, cases=_big_corpus(n, 60), run_name=f"run-{n}-{calls}")
        try:
            await probe.run(cancelled=lambda: True)
        except CorpusTooLarge:
            assert not probe.run_root.exists()
            return False
        assert probe.run_root.exists()
        admitted_bytes = len(
            (probe.run_root / "campaign.jsonl").read_bytes().splitlines(keepends=True)[0]
        )
        return True

    low, high = 1, 10_000  # admitted, refused
    while high - low > 1:
        mid = (low + high) // 2
        low, high = (mid, high) if await admitted(mid) else (low, mid)
    assert await admitted(low) and not await admitted(low + 1)

    # The admitted boundary is the encoded record bound itself, not a guess.
    assert admitted_bytes <= MAX_RECORD_BYTES


@pytest.mark.parametrize(
    ("case", "location"),
    [
        (
            EvalCase("case-01", "grp-hunter2secret", "s", (), {}, {}, {}),
            "identity.corpus_cases[0].group_id",
        ),
        (
            EvalCase("case-hunter2secret", "g", "s", (), {}, {}, {}),
            "identity.corpus_cases[0].case_id",
        ),
        (
            EvalCase("case-01", "g", "hunter2secret-x", (), {}, {}, {}),
            "identity.corpus_cases[0].stratum",
        ),
    ],
)
async def test_an_identity_string_containing_a_secret_is_refused_by_location(
    tmp_path: Path, case: EvalCase, location: str
) -> None:
    rig = Rig(tmp_path, cases=(case,))
    with pytest.raises(RecordMatchesSecret) as failure:
        await rig.run(secrets=("hunter2secret",))
    assert str(failure.value) == "record_matches_secret"
    assert failure.value.location == location
    assert "hunter2secret" not in repr(failure.value.args) + failure.value.location
    assert not rig.run_root.exists()


async def test_a_run_id_containing_a_secret_is_located_in_the_envelope(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(RecordMatchesSecret) as failure:
        await rig.run(secrets=("c-hunter2",), run_id="c-hunter2secret")
    assert failure.value.location == "envelope.run_id"


@pytest.mark.parametrize("secret", ["group_id", "timestamp", "corpus_cases"])
async def test_a_secret_equal_to_a_key_name_is_located_opaquely(
    tmp_path: Path, secret: str
) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(RecordMatchesSecret) as failure:
        await rig.run(secrets=(secret,))
    assert failure.value.location == "record-key"
    assert secret not in str(failure.value)


async def test_admit_creation_a_non_secret_schema_refusal_is_an_invariant_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `SchemaError` `admit_creation` cannot locate as a secret is `CampaignInvariantViolated`.

    Not every refusal `Envelope.from_payload` can raise is a secret hit:
    `secret_location` returning `None` for one used to let the bare
    `SchemaError` re-raise past `run_campaign`/`evolve` as a traceback, instead
    of the module's own classification for a record the lifecycle refuses.
    """

    from evolution.tree.campaign_identity import CampaignIdentityV1
    from evolution.tree.storage.envelope import SchemaError

    rig = Rig(tmp_path)
    await rig.run()
    identity = CampaignIdentityV1.from_payload(payloads(rig, "campaign_created")[0]["identity"])
    run_root = tmp_path / "unused-run-root"
    session = campaign_module._Campaign(
        rig.policy,
        run_root=run_root,
        seed_root=rig.seed_root,
        artifact_policy=ARTIFACT_POLICY,
        adapter=rig.adapter,
        environment=rig.environment,
        scorer=rig.scorer,
        editor=rig.editor,
        reflector=rig.reflector,
        environment_docs=None,
        corpus=rig.cases,
        adaptive=rig.adaptive,
        cancelled=lambda: False,
        secrets=(),
        hooks=None,
    )
    monkeypatch.setattr(campaign_module, "secret_location", lambda envelope, secrets: None)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise SchemaError("x")

    monkeypatch.setattr(campaign_module.Envelope, "from_payload", staticmethod(_refuse))
    with pytest.raises(campaign_module.CampaignInvariantViolated) as failure:
        session.admit_creation(identity, "run-fresh")
    assert str(failure.value) == "x"
    assert not run_root.exists()


async def test_the_creation_record_is_the_admitted_envelope(tmp_path: Path) -> None:
    """Spec §3: admission and append are one object, timestamp included."""

    rig = Rig(tmp_path)
    seen: list[bytes] = []
    original = campaign_module._Campaign.admit_creation

    def recording(self: Any, identity: Any, run_id: str) -> Envelope:
        record = original(self, identity, run_id)
        seen.append(record.encode())
        return record

    campaign_module._Campaign.admit_creation = recording  # type: ignore[method-assign]
    try:
        await rig.run()
    finally:
        campaign_module._Campaign.admit_creation = original  # type: ignore[method-assign]
    first = (rig.run_root / "campaign.jsonl").read_bytes().splitlines(keepends=True)[0]
    assert seen == [first]


def test_secret_location_walks_values_and_keys() -> None:
    envelope = {
        "run_id": "r",
        "event": "campaign_created",
        "payload": {
            "identity": {
                "corpus_cases": [
                    {"case_id": "a"},
                    {"case_id": "b", "group_id": "gSECRETX"},
                ]
            }
        },
    }
    assert secret_location(envelope, ("SECRET",)) == "identity.corpus_cases[1].group_id"
    assert secret_location({**envelope, "run_id": "rSECRET"}, ("SECRET",)) == "envelope.run_id"
    assert secret_location(envelope, ("case_id",)) == "record-key"
    assert secret_location(envelope, ("nothing",)) is None


async def test_a_resume_without_a_caller_id_over_an_illegible_creation_is_persistence_failed(
    tmp_path: Path,
) -> None:
    """W2 fix pack: no id from the caller and none from the prefix is not a traceback.

    `resume_campaign(run_id=None)` over a journal whose first record is garbage
    used to raise `SchemaError("invalid public projection")` from the status
    projection inside the fault report itself; the report names `unknown`.
    """

    rig = Rig(tmp_path)
    await rig.run()
    (rig.run_root / "campaign.jsonl").write_bytes(b"\x00garbage\n")
    result = await rig.resume()
    assert result.terminal == "persistence_failed"
    assert result.code == "journal_unavailable"
    assert result.status.to_payload()["run_id"] == "unknown"


# -- the default rig (retirement of /5 to /12) ------------------------------


async def test_the_default_rig_is_family_14(tmp_path):
    rig = Rig(tmp_path)
    await rig.run()
    created = payloads(rig, "campaign_created")[0]
    assert created["family_version"] == 14
    assert created["adaptation"]["version"] == "campaign_adaptation/7"
    assert (
        inspect_campaign(rig.run_root, verify=True).to_payload()["verification"]["mismatches"] == []
    )


def test_the_default_workspace_is_v15(tmp_path):
    from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
    from tests.tree_evolve_support import write_workspace

    manifest = write_workspace(tmp_path)
    assert json.loads(manifest.read_text())["schema"] == SCHEMA_V15


def test_native_creation_leaves_no_scratch_in_the_checkout(tmp_path, monkeypatch):
    from tests import campaign_drivers

    scratch = tmp_path / ".superpowers"
    assert not scratch.exists()
    monkeypatch.setattr(
        campaign_drivers, "__file__", str(tmp_path / "tests" / "campaign_drivers.py")
    )
    campaign_drivers._native14_creation.cache_clear()
    try:
        created = campaign_drivers.native14_created()
        assert created["family_version"] == 14
        assert created["identity"]["metric_contract"] != "c" * 64
        assert created["identity"]["role_visibility"] != "d" * 64
        assert not scratch.exists()
    finally:
        campaign_drivers._native14_creation.cache_clear()
