"""G2b2: the Candidate inspection's own journal (G2 spec §4.5)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import evolution.tree.campaign_inspection as inspection_module
from evolution.tree.campaign_inspection import (
    INSPECTIONS_DIR,
    InspectionFailed,
    inspect_candidate,
    inspection_id,
)
from evolution.tree.execution.interaction import RolloutInfrastructureError
from evolution.tree.execution.journal import Journal
from tests.campaign_fakes import STRATEGY, Crash, FakeAdapter, FakeInstance
from tests.test_tree_campaign_measurement import CAMPAIGN_RUN_ID, Rig, build_rig

MAP = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}


async def _inspect(rig: Rig, adapter: FakeAdapter, inspection: int = 1, *, secrets=()) -> Any:
    return await inspect_candidate(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        attempt_id="r01-a01",
        inspection=inspection,
        identity=rig.family12_identity(),
        corpus=rig.cases,
        secrets=secrets,
        adapter=adapter,
        candidate=rig.ref,
        strategy=STRATEGY,
        call_attempts=None,
    )


def _events(rig: Rig, inspection: int = 1) -> list[str]:
    path = rig.run_root / INSPECTIONS_DIR / inspection_id("r01-a01", inspection) / "events.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


def test_an_inspection_is_named_after_its_attempt_and_ordinal() -> None:
    assert INSPECTIONS_DIR == "inspections"
    assert inspection_id("r01-a01", 2) == "r01-a01-i2"


async def test_an_inspection_declares_the_map_and_releases_its_one_instance(tmp_path):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    assert await _inspect(rig, adapter) == (MAP, 0)
    assert len(adapter.materialized) == 1 and adapter.released == adapter.materialized
    events = _events(rig)
    assert events.index("components_declared") < events.index("target_release_started")
    # The journal lives outside `measurements/`, which admission reserves for
    # siblings the campaign journal declares.
    assert not (rig.run_root / "measurements").exists()


async def test_an_instance_without_a_map_inspects_as_none(tmp_path):
    adapter = FakeAdapter()
    rig = await build_rig(tmp_path, adapter=adapter)
    assert await _inspect(rig, adapter) == (None, 0)
    assert "components_declared" not in _events(rig)
    assert adapter.released == adapter.materialized


async def test_an_instance_without_the_optional_components_attribute(tmp_path):
    class NoComponents(FakeAdapter):
        def instance_for(self, *args, **kwargs):
            instance = super().instance_for(*args, **kwargs)
            del instance.components
            return instance

    adapter = NoComponents()
    rig = await build_rig(tmp_path, adapter=adapter)
    assert await _inspect(rig, adapter) == (None, 0)
    assert "components_declared" not in _events(rig)
    assert adapter.released == adapter.materialized


@pytest.mark.parametrize("components", [{}, None])
async def test_inspection_keeps_empty_and_absent_maps_distinct(tmp_path, components, capsys):
    adapter = FakeAdapter(components=components)
    rig = await build_rig(tmp_path, adapter=adapter)
    assert await _inspect(rig, adapter) == (components, 0)
    assert ("components_declared" in _events(rig)) is (components is not None)
    assert capsys.readouterr().err == ""


async def test_inspection_warns_once_with_counts_only(tmp_path, capsys):
    secret = "inspection-secret-387"
    adapter = FakeAdapter(components={secret: next(iter(MAP.values()))})
    rig = await build_rig(tmp_path, adapter=adapter)
    assert await _inspect(rig, adapter, secrets=(secret,)) == ({}, 1)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "0 sample(s) and 1 component(s)" in lines[0]
    assert secret not in lines[0]


async def test_a_target_failure_is_inspection_failed(tmp_path):
    class Down(FakeAdapter):
        async def materialize(self, *args: Any, **kwargs: Any) -> Any:
            raise RolloutInfrastructureError("down", "target_unavailable")

    adapter = Down(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(InspectionFailed) as raised:
        await _inspect(rig, adapter)
    assert isinstance(raised.value.__cause__, RolloutInfrastructureError)


async def test_a_failed_materialize_whose_instance_the_target_created_is_released(tmp_path):
    # A lost answer: the target creates the instance, then the call fails. R1
    # still counts the id as owned, and journal k is never reopened once the
    # Attempt falls back, so the inspection releases it before failing.
    class LostAnswer(FakeAdapter):
        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            await super().materialize(candidate, strategy, instance_id)
            raise RolloutInfrastructureError("answer lost", "timeout")

    adapter = LostAnswer(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(InspectionFailed):
        await _inspect(rig, adapter)
    assert len(adapter.materialized) == 1 and adapter.released == adapter.materialized
    assert adapter.live == {}
    events = _events(rig)
    assert events.index("target_materialize_failed") < events.index("target_release_finished")


async def test_a_refused_release_after_a_failed_materialize_is_still_inspection_failed(
    tmp_path,
):
    class LostAnswer(FakeAdapter):
        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            self.refuse_release = frozenset({instance_id})
            await super().materialize(candidate, strategy, instance_id)
            raise RolloutInfrastructureError("answer lost", "timeout")

    adapter = LostAnswer(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(InspectionFailed) as raised:
        await _inspect(rig, adapter)
    # The materialize failure is the cause, not the refused release; the
    # instance stays owned (a known limitation, no leak reporting).
    assert str(raised.value.__cause__) == "answer lost"
    assert len(adapter.live) == 1
    assert "target_release_failed" in _events(rig)


async def test_an_inspection_interrupted_under_an_admitted_older_version_is_reopened(
    tmp_path, monkeypatch
):
    import evolution.tree.execution.journal as journal_module
    from evolution.tree.execution.journal_identity import IdentityError

    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)

    async def cut(*_args: Any, **_kwargs: Any) -> None:
        raise Crash("power cut")

    with monkeypatch.context() as patch:
        # Stamp journal k with an older producing version, then crash before
        # the release, leaving the instance owned.
        patch.setattr(journal_module, "resolve_package_version", lambda _resolver: "0.3.0a0")
        patch.setattr(inspection_module, "release_target", cut)
        with pytest.raises(Crash):
            await _inspect(rig, adapter)
    with pytest.raises(IdentityError, match="package_version_mismatch"):
        await _inspect(rig, adapter)
    reopened = await inspect_candidate(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        attempt_id="r01-a01",
        inspection=1,
        identity=rig.family12_identity(),
        corpus=rig.cases,
        secrets=(),
        adapter=adapter,
        candidate=rig.ref,
        strategy=STRATEGY,
        call_attempts=None,
        admitted_versions={"0.3.0a0"},
    )
    assert reopened == (MAP, 0)
    assert adapter.live == {}


async def test_an_attestation_mismatch_is_inspection_failed_and_releases(tmp_path):
    # As `test_a_failed_attestation_releases_the_instance_and_starts_no_sample`
    # does: the instance reports another Candidate's digest.
    class Misattesting(FakeAdapter):
        def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
            return FakeInstance(
                instance_id,
                boot_id,
                self._semantic_for(candidate.dependency_digest),
                "0" * 64,
                self.dispositions,
                components=MAP,
            )

    adapter = Misattesting(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(InspectionFailed):
        await _inspect(rig, adapter)
    assert adapter.materialized and adapter.released == adapter.materialized
    assert "components_declared" not in _events(rig)


async def test_a_crash_after_create_is_released_and_reinspected_in_the_same_journal(
    tmp_path, monkeypatch
):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    original = inspection_module.release_target

    async def cut(*_args: Any, **_kwargs: Any) -> None:
        # `release_target` wraps an adapter error into a suppressed
        # `RolloutInfrastructureError`, so the power cut is at the call itself.
        raise Crash("power cut")

    monkeypatch.setattr(inspection_module, "release_target", cut)
    with pytest.raises(Crash):
        await _inspect(rig, adapter)
    monkeypatch.setattr(inspection_module, "release_target", original)
    assert len(adapter.live) == 1
    assert await _inspect(rig, adapter) == (MAP, 0)
    assert adapter.live == {}
    assert _events(rig).count("components_declared") == 1


async def test_a_leftover_instance_the_target_will_not_release_is_inspection_failed(
    tmp_path, monkeypatch
):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)

    async def cut(*_args: Any, **_kwargs: Any) -> None:
        raise Crash("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(inspection_module, "release_target", cut)
        with pytest.raises(Crash):
            await _inspect(rig, adapter)
    adapter.refuse_release = frozenset(adapter.live)
    with pytest.raises(InspectionFailed, match="inspection_instance_unreleased"):
        await _inspect(rig, adapter)
    # Nothing new was loaded while the old instance is still owned.
    assert len(adapter.materialized) == 1


async def test_an_unborn_directory_is_replaced_by_a_new_journal(tmp_path):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    unborn = rig.run_root / INSPECTIONS_DIR / inspection_id("r01-a01", 1)
    unborn.mkdir(parents=True)
    (unborn / "events.jsonl").write_bytes(b"")
    assert await _inspect(rig, adapter) == (MAP, 0)
    assert _events(rig)[0] == "run_created"


async def test_cancellation_is_not_a_failure(tmp_path):
    class Cancelled(FakeAdapter):
        async def materialize(self, *args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

    adapter = Cancelled(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(asyncio.CancelledError):
        await _inspect(rig, adapter)


def _moved_identity(rig: Rig) -> Any:
    """The family-12 identity after an accepted change of the Target's settings."""

    from evolution.tree.execution.journal_identity import RunIdentityV1

    payload = rig.family12_identity().to_payload()
    payload["target"] = {**payload["target"], "settings_hash": "settings-v2"}
    return RunIdentityV1.from_payload(payload)


async def _crash_before_the_release(rig: Rig, adapter: FakeAdapter, monkeypatch) -> str:
    async def cut(*_args: Any, **_kwargs: Any) -> None:
        raise Crash("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(inspection_module, "release_target", cut)
        with pytest.raises(Crash):
            await _inspect(rig, adapter)
    (leftover,) = adapter.live
    return leftover


async def _inspect_as(rig: Rig, adapter: FakeAdapter, identity: Any) -> Any:
    return await inspect_candidate(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        attempt_id="r01-a01",
        inspection=1,
        identity=identity,
        corpus=rig.cases,
        secrets=(),
        adapter=adapter,
        candidate=rig.ref,
        strategy=STRATEGY,
        call_attempts=None,
    )


async def test_a_journal_left_under_another_identity_is_released_and_replaced(
    tmp_path, monkeypatch
):
    # Final review Important 1: journal k was born under identity A and holds
    # no `candidate_inspected`; the campaign now derives identity B.
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    leftover = await _crash_before_the_release(rig, adapter, monkeypatch)
    moved = _moved_identity(rig)
    assert moved.digest != rig.family12_identity().digest
    assert await _inspect_as(rig, adapter, moved) == (MAP, 0)
    assert leftover in adapter.released and adapter.live == {}
    path = rig.run_root / INSPECTIONS_DIR / inspection_id("r01-a01", 1)
    assert inspection_module.stored_identity(path).digest == moved.digest
    assert _events(rig).count("target_materialize_started") == 1


async def test_a_leftover_under_another_identity_the_target_keeps_is_inspection_failed(
    tmp_path, monkeypatch
):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    leftover = await _crash_before_the_release(rig, adapter, monkeypatch)
    adapter.refuse_release = frozenset({leftover})
    with pytest.raises(InspectionFailed, match="inspection_instance_unreleased"):
        await _inspect_as(rig, adapter, _moved_identity(rig))
    # The journal that still owns the instance is kept, under its own identity.
    path = rig.run_root / INSPECTIONS_DIR / inspection_id("r01-a01", 1)
    assert inspection_module.stored_identity(path).digest == rig.family12_identity().digest
    assert len(adapter.materialized) == 1


async def test_release_leftover_reports_whether_the_journal_still_owns_an_instance(
    tmp_path, monkeypatch
):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    leftover = await _crash_before_the_release(rig, adapter, monkeypatch)
    path = rig.run_root / INSPECTIONS_DIR / inspection_id("r01-a01", 1)
    adapter.refuse_release = frozenset({leftover})
    assert not await inspection_module.release_leftover(path, "r01-a01-i1", rig.cases, (), adapter)
    adapter.refuse_release = frozenset()
    assert await inspection_module.release_leftover(path, "r01-a01-i1", rig.cases, (), adapter)
    assert adapter.live == {}


async def test_release_leftover_closes_pending_calls_before_releasing_the_instance(tmp_path):
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    name = inspection_id("r01-a01", 1)
    path = rig.run_root / INSPECTIONS_DIR / name
    path.parent.mkdir()
    journal = Journal.create(path, name, rig.family12_identity(), rig.cases)
    instance_id = "interrupted-inspection"
    await adapter.materialize(rig.ref, STRATEGY, instance_id)
    instance = {
        "instance_id": instance_id,
        "candidate_digest": rig.ref.digest,
        "strategy": STRATEGY,
        "retry_ordinal": 0,
    }
    journal.append("target_materialize_started", instance, "materialize")
    journal.append("target_materialize_finished", {**instance, "latency_ms": 0}, "materialize")
    row = rig.plans()[0].rows[0]
    sample = {
        "case_id": row["case_id"],
        "sample_id": row["sample_id"],
        "candidate_digest": rig.ref.digest,
        # The native policy declares sample retries (PDR-0011).
        "retry_ordinal": 0,
    }
    journal.append("rollout_started", sample, "rollout")
    journal.append(
        "target_identity_started",
        {**sample, "instance_id": instance_id, "retry_ordinal": 0},
        "identity",
    )
    journal.close()

    assert await inspection_module.release_leftover(path, name, rig.cases, (), adapter)
    assert instance_id in adapter.released and adapter.live == {}
    assert _events(rig) == [
        "run_created",
        "target_materialize_started",
        "target_materialize_finished",
        "rollout_started",
        "target_identity_started",
        "operation_interrupted",
        "rollout_interrupted",
        "target_release_started",
        "target_release_finished",
    ]


async def test_a_journal_fault_on_the_declaration_releases_through_the_adapter(
    tmp_path, monkeypatch
):
    from evolution.tree.storage.journal_errors import JournalFault

    original = JournalFault("storage_capacity_exhausted")

    class Poisoned(inspection_module.Journal):
        poisoned = False

        @property
        def healthy(self) -> bool:
            return not self.poisoned and super().healthy

        def append(self, event: str, payload: Any, operation_id: Any = None) -> int:
            if self.poisoned:
                raise JournalFault("journal_unavailable")
            if event == "components_declared":
                self.poisoned = True
                raise original
            return super().append(event, payload, operation_id)

    monkeypatch.setattr(inspection_module, "Journal", Poisoned)
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(JournalFault) as raised:
        await _inspect(rig, adapter)
    assert raised.value is original
    assert len(adapter.materialized) == 1 and adapter.released == adapter.materialized
    assert adapter.live == {}
    # The release is unjournaled: the journal still owns the instance, so a
    # reopen releases it again by id, as after a crash.
    assert "target_release_started" not in _events(rig)


def _poisoned_on(event_name: str, original: Exception) -> type:
    from evolution.tree.storage.journal_errors import JournalFault

    class Poisoned(inspection_module.Journal):
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
    from evolution.tree.storage.journal_errors import JournalFault

    original = JournalFault("storage_capacity_exhausted")
    monkeypatch.setattr(
        inspection_module, "Journal", _poisoned_on("target_materialize_finished", original)
    )
    adapter = FakeAdapter(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(JournalFault) as raised:
        await _inspect(rig, adapter)
    assert raised.value is original
    assert len(adapter.materialized) == 1 and adapter.released == adapter.materialized
    assert adapter.live == {}
    # The journal still records the started materialize, so a reopen releases
    # the instance again by id.
    events = _events(rig)
    assert "target_materialize_started" in events
    assert "target_release_started" not in events


async def test_a_journal_fault_on_materialize_failed_releases_through_the_adapter(
    tmp_path, monkeypatch
):
    # The Target created the instance and the call then failed
    # (a lost answer), and the failure record cannot be written.
    from evolution.tree.storage.journal_errors import JournalFault

    class LostAnswer(FakeAdapter):
        async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> Any:
            await super().materialize(candidate, strategy, instance_id)
            raise RolloutInfrastructureError("answer lost", "timeout")

    original = JournalFault("journal_unavailable")
    monkeypatch.setattr(
        inspection_module, "Journal", _poisoned_on("target_materialize_failed", original)
    )
    adapter = LostAnswer(components=MAP)
    rig = await build_rig(tmp_path, adapter=adapter)
    with pytest.raises(JournalFault) as raised:
        await _inspect(rig, adapter)
    assert raised.value is original
    assert len(adapter.materialized) == 1 and adapter.released == adapter.materialized
    assert adapter.live == {}
    assert "target_release_started" not in _events(rig)
