"""Task 7: the campaign view, the pure planner, the executor, resume and the linked run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree import campaign_reflector
from evolution.tree.artifacts.artifact_errors import ArtifactCapacityError, ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.baseline_import import BaselineSourceUnavailable
from evolution.tree.campaign import (
    BaselineImportUnavailable,
    CampaignExists,
    CampaignInvariantViolated,
    CampaignMissing,
    HygieneNotAuthorizable,
    IdentityChanged,
    LineageChampionUnverified,
    LineageNotFinished,
    finish_owed_terminal,
)
from evolution.tree.campaign_identity import CapabilitiesInsufficient, measurement_id_for
from evolution.tree.campaign_measurement import Measurement, StagePlan
from evolution.tree.campaign_projections import champion_measurement, read_run_id
from evolution.tree.campaign_schema import (
    CampaignSnapshot,
)
from evolution.tree.campaign_view import (
    CampaignView,
    attempt_id_for,
)
from evolution.tree.execution.authorizations import (
    Authorization,
    hygiene_instance_digest,
)
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.check_modes import ENFORCE_ALL
from evolution.tree.execution.interaction import TargetIdentity
from evolution.tree.execution.journal_identity import IdentityError
from evolution.tree.execution.output_admission import HygieneFinding
from evolution.tree.execution.wire_v2 import IDENTIFIER, MAX_RUN_ID, new_instance_id
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.roles.editor_contracts import (
    EditBrief,
    EditRequest,
    EditResult,
    brief_digest,
    brief_payload,
)
from evolution.tree.roles.environment_docs import (
    ENVIRONMENT_DOCS_ROOT,
    _v2_create_document_snapshot,
)
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload
from evolution.tree.storage.append_log import AppendLog
from evolution.tree.storage.journal_errors import JournalFault
from tests.campaign_fakes import (
    ARTIFACT_POLICY,
    FAILING_VERDICT,
    PASSING_VERDICT,
    Crash,
    DeterministicScorer,
    EditorScript,
    FakeAdapter,
    PromotingScorer,
    ReflectorScript,
    RefusingAdapter,
    Rig,
    ScriptedEditor,
    ScriptedReflector,
    corpus,
    edits,
    policy,
    seed_tree,
    v5_policy,
)

pytestmark = pytest.mark.slow

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


# -- rigs -----------------------------------------------------------------


def view_of(snapshot: CampaignSnapshot) -> CampaignView:
    return CampaignView(snapshot=snapshot, measurements={}, lineage=None, siblings={})


def measurement_entry(
    measurement_id: str,
    attempt_id: str,
    round_number: int,
    *,
    stages: tuple[int, ...] = (),
    complete: bool = False,
) -> dict[str, Any]:
    return dict(
        measurement_id=measurement_id,
        attempt_id=attempt_id,
        round=round_number,
        candidate_digest=DIGEST_A,
        identity_digest=DIGEST_B,
        stages=tuple(
            dict(stage=index + 1, look=look, sample_count=6) for index, look in enumerate(stages)
        ),
        instances=(),
        complete=complete,
    )


FINGERPRINT = "f" * 64


def focus_payload(
    *,
    reason: str = "mass",
    cases: tuple[str, ...] = ("case-01",),
    unavailable: int = 0,
    seq: int = 7,
) -> dict[str, Any]:
    """One round's reduced `focus_selected` entry (payload minus evidence, plus `seq`)."""

    focused = reason != "none"
    return dict(
        round=1,
        policy_digest=DIGEST_C,
        clusters=[dict(fingerprint=FINGERPRINT, mass=len(cases), cases=list(cases), stalled=False)]
        if focused
        else [],
        focus_fingerprint=FINGERPRINT if focused else None,
        reason=reason,
        unavailable=unavailable,
        seq=seq,
    )


def reflection_entry(
    *,
    status: str = "reflected",
    error_class: str | None = None,
    groups: tuple[dict[str, Any], ...] = (),
    seq: int = 9,
) -> dict[str, Any]:
    """One reduced entry of `RoundState.reflections`."""

    return dict(
        reflection_id="r01-x01",
        status=status,
        groups=list(groups),
        error_class=error_class,
        probes=(),
        seq=seq,
        operation_id="0" * 32,
    )


def gate_scope_payload(
    *, source: str = "empty-claim", cases: tuple[str, ...] = ("case-01",), seq: int = 10
) -> dict[str, Any]:
    """One round's reduced `gate_scope_selected` entry."""

    return dict(round=1, source=source, cases=list(cases), seq=seq)


def settled(**overrides: Any) -> dict[str, Any]:
    """The `RoundState` fields a round carries once its reflector prelude is done.

    Every planner test that wants the round's *attempts* starts from here: the
    focus, one settled reflection and the promotion gate's scope are what
    `next_actions` now requires before the first `OpenAttempt` (spec §3.3).
    """

    fields: dict[str, Any] = dict(
        focus=focus_payload(),
        reflections=(reflection_entry(),),
        gate_scope=gate_scope_payload(),
    )
    fields.update(overrides)
    return fields


# -- the pure planner -----------------------------------------------------


# -- held Attempts and bounded reactivation (#91 spec §3.1, §3.2) ---------


# -- the reflector prelude (spec §3.3) ------------------------------------


# -- refusals before any write --------------------------------------------


async def test_run_campaign_refuses_a_run_root_that_already_holds_a_sibling(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.run_root.mkdir()
    (rig.run_root / "artifacts.jsonl").write_bytes(b"")

    with pytest.raises(CampaignExists) as excinfo:
        await rig.run()
    assert str(excinfo.value) == "run_root_not_empty"
    assert not (rig.run_root / "campaign.jsonl").exists()


async def test_run_campaign_refuses_an_oversubscribing_policy(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=policy(attempts_per_round=4, parallel_attempts=4, max_concurrency=4),
        adapter=FakeAdapter(max_instances=2, max_concurrent_sessions=1),
    )

    with pytest.raises(CapabilitiesInsufficient):
        await rig.run()
    assert not rig.run_root.exists() or not (rig.run_root / "campaign.jsonl").exists()


# -- the happy path -------------------------------------------------------


async def test_a_two_round_campaign_promotes_and_finishes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=2, attempts_per_round=1))
    result = await rig.run()

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert result.finished["outcome"] == "completed"
    assert result.finished["reason"] == "rounds_exhausted"
    events = rig.events()
    assert events[0] == "campaign_created"
    assert events[-1] == "campaign_finished"
    assert events.count("round_closed") == 2
    outcomes = [r["payload"]["outcome"] for r in rig.records() if r["event"] == "round_closed"]
    assert outcomes[0] == "promoted"
    # ADR-0032: the final measurement completes before the promoted baseline is reused.
    assert "final_measurement_completed" in events
    payload = result.result.to_payload() if result.result is not None else {}
    assert payload["claim_scope"] == "campaign"
    assert payload["final_champion"]["measurement_complete"] is True


async def test_declare_then_create_is_observable_through_the_store_hook(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    seen: list[str] = []

    def after_store_created() -> None:
        raw = (rig.run_root / "campaign.jsonl").read_bytes()
        seen.extend(json.loads(line)["event"] for line in raw.splitlines() if line)

    await rig.run(_hooks={"after_store_created": after_store_created})

    assert seen == ["campaign_created"]


async def test_the_decision_reference_names_the_selection_record_bytes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run()

    raw = (rig.run_root / "campaign.jsonl").read_bytes()
    offset = 0
    expected: dict[str, Any] | None = None
    for line in raw.splitlines(keepends=True):
        record = json.loads(line)
        if record["event"] == "selection_decided":
            expected = dict(
                version=1,
                kind="journal_record",
                run_id=record["run_id"],
                locator=f"campaign.jsonl#bytes={offset}:{offset + len(line)}",
                content_digest=hashlib.sha256(line).hexdigest(),
            )
        offset += len(line)
    assert expected is not None

    ledger = [
        json.loads(line)
        for line in (rig.run_root / "artifacts.jsonl").read_bytes().splitlines()
        if line
    ]
    pending = [r for r in ledger if r["event"] == "promotion_pending"]
    assert pending and pending[0]["payload"]["decision_ref"] == expected


# -- resume ---------------------------------------------------------------


async def test_resume_reissues_the_same_decision_after_a_crash_before_round_closed(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    class Boom(RuntimeError):
        pass

    def after_selection_decided() -> None:
        raise Boom("crash")

    with pytest.raises(Boom):
        await rig.run(_hooks={"after_selection_decided": after_selection_decided})
    assert rig.events()[-1] == "selection_decided"
    before = [r for r in rig.records() if r["event"] == "selection_decided"][0]

    result = await rig.resume()

    assert result.terminal == "finished"
    after = [r for r in rig.records() if r["event"] == "selection_decided"]
    assert len(after) == 1 and after[0] == before
    assert "round_closed" in rig.events()


async def test_resume_refuses_a_different_editor(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})

    with pytest.raises(IdentityError) as excinfo:
        await rig.resume(editor=ScriptedEditor(edits("other")))
    assert str(excinfo.value) == "run_identity_changed"


async def test_resume_abandons_an_attempt_whose_editor_never_finished(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_editor_finished() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_editor_finished": after_editor_finished})

    result = await rig.resume()

    assert result.terminal == "finished"
    closes = [r["payload"] for r in rig.records() if r["event"] == "attempt_closed"]
    assert closes and closes[0]["outcome"] == "abandoned"


# -- the terminal barrier -------------------------------------------------


async def test_a_cooperative_editor_is_awaited_at_the_barrier(tmp_path: Path) -> None:
    attempt_id = attempt_id_for(1, 1)
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, editor_timeout_seconds=0.05),
        editor=ScriptedEditor({attempt_id: EditorScript("sleep", seconds=3600.0)}),
    )

    result = await rig.run()

    assert result.quiescent is True
    failed = [r["payload"] for r in rig.records() if r["event"] == "editor_failed"]
    assert failed and failed[0]["error_class"] == "timeout"


# -- the linked run -------------------------------------------------------


async def predecessor_with_linked_continuation(tmp_path: Path) -> Rig:
    """A family-6 campaign that finished by a rollout threshold at its baseline.

    Every family-6 terminal offers a linked run (continuable-terminals spec
    §5.1); a threshold terminal keeps the champion at the seed, so a successor
    seeded from the same tree is comparable.
    """

    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1,
            attempts_per_round=1,
            # A generous `timeout_seconds`, not a tight one: `dispositions`'s
            # `time_budget` script raises the core's own expired-deadline
            # signal directly from `send` (campaign_fakes's "not a wall
            # clock" note), so it crosses this `max_count: 0` threshold
            # whatever the budget is. A tight budget here would instead put
            # every *unscripted* "completed" sample -- in this predecessor and
            # in any successor that reuses `first.policy` -- at the mercy of
            # real wall-clock scheduling delay on a loaded host, which is
            # indeterminate under this same threshold and was the source of
            # an intermittent failure in a full-suite run.
            budget={"max_turns": 4, "timeout_seconds": 30.0},
            indeterminate={
                owner: {"max_count": 0, "max_rate": "1"}
                for owner in ("target", "environment", "scorer", "rollout")
            },
        ),
        adapter=FakeAdapter(),
        run_name="first",
    )
    rig.adapter.dispositions = {
        (_seed_digest(tmp_path, "first-seed"), "case-01", "02"): "time_budget"
    }
    result = await rig.run()
    assert result.finished is not None
    assert result.finished["continuation"] == {
        "kind": "linked_run",
        "reason": "rollout_time_budget_exhausted",
    }
    return rig


def _seed_digest(tmp_path: Path, name: str = "first-seed") -> str:
    root = tmp_path / name
    seed_tree(root)
    return scan_tree(root, ARTIFACT_POLICY).candidate_digest


def successor_editor(rounds: int = 6) -> ScriptedEditor:
    """An editor for a successor's attempts, whatever round it starts at."""

    return ScriptedEditor(edits(*(attempt_id_for(number, 1) for number in range(1, rounds + 1))))


def champion_path(rig: Rig) -> Path:
    """The predecessor's final champion copy (ruling R4: straight from the store)."""

    projection = ArtifactStore.inspect(rig.run_root)
    assert projection.champion is not None
    return rig.run_root / "artifacts" / "champions" / projection.champion.version_id


async def finished_rig(
    tmp_path: Path, name: str, *, schema: str = SCHEMA_V15, **policy_overrides: Any
) -> Rig:
    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=1, attempts_per_round=1, **policy_overrides),
        run_name=name,
        schema=schema,
    )
    result = await rig.run()
    assert result.finished is not None
    return rig


def successor_of(
    first: Rig,
    tmp_path: Path,
    name: str = "second",
    *,
    schema: str = SCHEMA_V15,
    **policy_overrides: Any,
) -> Rig:
    second = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=1, attempts_per_round=1, **policy_overrides),
        editor=successor_editor(),
        run_name=name,
        schema=schema,
    )
    second.seed_root = champion_path(first)
    return second


def stage_counts(rig: Rig, measurement_id: str) -> list[int]:
    """The `sample_count` of every `stage_opened` of one measurement, in order."""

    return [
        r["payload"]["sample_count"]
        for r in rig.records()
        if r["event"] == "stage_opened" and r["payload"]["measurement_id"] == measurement_id
    ]


def denominator_of(rig: Rig) -> int:
    from evolution.tree.campaign_projections import inspect_view

    return inspect_view(rig.run_root).snapshot.denominator


def measured_tries(rig: Rig) -> int:
    """Every `rollout_started` across the run's born measurement journals."""

    from evolution.tree.campaign_projections import inspect_view

    total = 0
    for measurement_id in inspect_view(rig.run_root).snapshot.measurements:
        if (rig.run_root / "measurements" / measurement_id / "events.jsonl").exists():
            total += sum(
                r["event"] == "rollout_started" for r in rig.measurement_records(measurement_id)
            )
    return total


async def test_a_run_that_carries_nothing_counts_every_scheduled_row(tmp_path: Path) -> None:
    """Pre-D2b shape: with nothing carried every stage declares its whole plan,
    and the denominator is still exactly the measured tries."""

    rig = Rig(tmp_path, campaign_policy=v5_policy(rounds=1, attempts_per_round=1))
    await rig.run()

    assert stage_counts(rig, measurement_id_for(None)) == [len(rig.cases) * 3]
    assert denominator_of(rig) == measured_tries(rig)


async def test_nothing_admissible_measures_fresh_and_notes_it(tmp_path: Path) -> None:
    """§4.2, ruling R1: a changed `random_seed` under `exact` admits nothing;
    `inherit` with `imported: 0` is still journaled, the baseline is measured
    fresh, and no caveat is reported."""

    first = await finished_rig(tmp_path, "first")
    second = successor_of(first, tmp_path, random_seed=first.policy.random_seed + 1)
    noted: list[int] = []

    result = await second.run(
        linked_from=first.run_root,
        linked_baseline="inherit",
        on_baseline_inherited=lambda imp, _other: noted.append(imp.imported),
    )

    assert result.terminal == "finished"
    assert noted == [0]
    declared = second.records()[0]["payload"]["identity"]["linked_from"]["baseline"]
    assert declared["mode"] == "inherit" and declared["imported"] == 0
    baseline = second.measurement_records(measurement_id_for(None))
    assert not any(r["event"] == "sample_imported" for r in baseline)
    assert any(r["event"] == "rollout_started" for r in baseline)
    kinds = [c["kind"] for c in second.result_payload()["caveats"]]
    assert "baseline_inherited" not in kinds


async def test_an_unreadable_source_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.0: `baseline_source_unavailable` is raised before the target is
    contacted and before the run root holds anything."""

    first = await finished_rig(tmp_path, "first")
    shutil.rmtree(first.run_root / "measurements")
    second = successor_of(first, tmp_path)
    preflight = campaign_module._Campaign.preflight

    async def forbidden_preflight(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("preflight ran before the source was read")

    monkeypatch.setattr(campaign_module._Campaign, "preflight", forbidden_preflight)
    with pytest.raises(BaselineSourceUnavailable):
        await second.run(linked_from=first.run_root, linked_baseline="inherit")
    assert not (second.run_root / "campaign.jsonl").exists()
    assert not (second.run_root / "measurements").exists()
    # The same predecessor still links `fresh`.
    monkeypatch.setattr(campaign_module._Campaign, "preflight", preflight)
    result = await second.run(linked_from=first.run_root)
    assert result.terminal == "finished"


def substitute_champion_journal(first: Rig) -> None:
    """Replace the predecessor's champion measurement journal with another of its
    own, valid measurement journals (codex branch review round 1): the file
    replays `complete`, but it is not the measurement the snapshot bound."""

    from evolution.tree.campaign import _predecessor

    reduced = _predecessor(first.run_root, expected_family=14).reduced
    champion_id, _ = champion_measurement(reduced)
    assert champion_id is not None
    other = next(m for m in sorted(reduced.measurements) if m != champion_id)
    measurements = first.run_root / "measurements"
    shutil.copyfile(
        measurements / other / "events.jsonl", measurements / champion_id / "events.jsonl"
    )


async def test_a_substituted_source_journal_is_baseline_source_unavailable(
    tmp_path: Path,
) -> None:
    """§5.0: the source is the measurement the predecessor's snapshot bound; a
    journal whose own identity is not that binding cannot be read as it."""

    first = await finished_rig(tmp_path, "first")
    substitute_champion_journal(first)
    second = successor_of(first, tmp_path)

    with pytest.raises(BaselineSourceUnavailable):
        await second.run(linked_from=first.run_root, linked_baseline="inherit")
    assert not (second.run_root / "campaign.jsonl").exists()
    assert not (second.run_root / "measurements").exists()


async def test_a_source_locked_by_its_owner_is_baseline_source_unavailable(
    tmp_path: Path,
) -> None:
    """§5.0: a source measurement whose lease a process resuming the
    predecessor holds is not read past; nothing of the successor is written."""

    from evolution.tree.campaign import _predecessor
    from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal

    first = await finished_rig(tmp_path, "first")
    reduced = _predecessor(first.run_root, expected_family=14).reduced
    champion_id, _ = champion_measurement(reduced)
    assert champion_id is not None
    second = successor_of(first, tmp_path)
    owner = Journal._open(
        first.run_root / "measurements" / champion_id,
        create=False,
        max_record_bytes=MAX_RECORD_BYTES,
        max_journal_bytes=MAX_JOURNAL_BYTES,
        secrets=(),
    )
    try:
        with pytest.raises(BaselineSourceUnavailable):
            await second.run(linked_from=first.run_root, linked_baseline="inherit")
    finally:
        owner.close()
    assert not (second.run_root / "campaign.jsonl").exists()
    assert not (second.run_root / "measurements").exists()
    # Released, the same source is read.
    result = await second.run(linked_from=first.run_root, linked_baseline="inherit")
    assert result.terminal == "finished"


async def test_a_substituted_source_journal_refuses_an_owed_copy(tmp_path: Path) -> None:
    """§5.3: the owed-copy re-read goes through the same `read_source` binding check."""

    first = await finished_rig(tmp_path, "first")
    second = successor_of(first, tmp_path)
    with pytest.raises(CrashAt):
        await second.run(
            linked_from=first.run_root,
            linked_baseline="inherit",
            _hooks={"after_measurement_bound": crash},
        )
    assert second.records()[0]["payload"]["identity"]["linked_from"]["baseline"]["imported"] > 0
    substitute_champion_journal(first)
    before = (second.run_root / "campaign.jsonl").read_bytes()

    with pytest.raises(BaselineImportUnavailable):
        await second.resume(linked_from=first.run_root)
    assert (second.run_root / "campaign.jsonl").read_bytes() == before


def inheriting_successor(first: Rig, tmp_path: Path, name: str = "second") -> Rig:
    """The successor `test_campaign_03_shape_carries_all_but_the_indeterminate_pair` runs."""

    second = Rig(
        tmp_path,
        campaign_policy=first.policy,
        adapter=FakeAdapter(),
        editor=successor_editor(),
        run_name=name,
    )
    second.seed_root = first.seed_root
    return second


class CrashAt(RuntimeError):
    """A simulated power cut at a named seam of the first invocation."""


def crash() -> None:
    raise CrashAt("crash")


def _imported(rig: Rig) -> int:
    baseline = rig.measurement_records(measurement_id_for(None))
    return sum(r["event"] == "sample_imported" for r in baseline)


async def test_an_empty_declaration_owes_nothing_on_resume(tmp_path: Path) -> None:
    """Ruling R1: `imported: 0` owes no copy, so a resume reads nothing and
    cannot refuse `baseline_import_unavailable`."""

    first = await finished_rig(tmp_path, "first")
    second = successor_of(first, tmp_path, random_seed=first.policy.random_seed + 1)
    with pytest.raises(CrashAt):
        await second.run(
            linked_from=first.run_root,
            linked_baseline="inherit",
            _hooks={"after_measurement_bound": crash},
        )

    # No predecessor path: a resume that tried to read one would refuse.
    result = await second.resume()

    assert result.terminal == "finished"
    assert _imported(second) == 0


async def test_sample_retries_may_differ_across_a_link(tmp_path: Path) -> None:
    first = await finished_rig(tmp_path, "first", sample_retries=1)
    second = successor_of(first, tmp_path, sample_retries=3)

    assert (await second.run(linked_from=first.run_root)).terminal == "finished"


async def test_a_changed_scorer_links_and_reports_its_differences(tmp_path: Path) -> None:
    """Comparable links D2a §3: the link is admitted and the difference noted."""

    first = await finished_rig(tmp_path, "first")
    second = successor_of(first, tmp_path)
    seen: list[Any] = []

    result = await second.run(
        linked_from=first.run_root,
        scorer=second.scorer_with_identity_digest("f" * 64),
        on_condition_changed=seen.append,
    )

    assert result.terminal == "finished"
    assert len(seen) == 1
    assert [d.field for d in seen[0]] == ["identity.scorer.digest"]
    linked = second.records()[0]["payload"]["identity"]["linked_from"]
    assert linked["condition_changed"] == ["identity.scorer.digest"]


async def test_a_changed_policy_and_seed_report_every_difference(tmp_path: Path) -> None:
    first = await finished_rig(tmp_path, "first")
    second = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=2, attempts_per_round=1),
        editor=successor_editor(),
        run_name="second",
    )

    seen: list[Any] = []

    await second.run(linked_from=first.run_root, on_condition_changed=seen.append)

    assert [d.field for d in seen[0]] == [
        "identity.policy_digest",
        "seed_root.candidate_digest",
    ]
    linked = second.records()[0]["payload"]["identity"]["linked_from"]
    assert linked["condition_changed"] == [
        "identity.policy_digest",
        "seed_root.candidate_digest",
    ]


async def test_an_unfinished_predecessor_is_refused(tmp_path: Path) -> None:
    """A predecessor with no `campaign_finished` could still be resumed (spec §7)."""

    first = Rig(
        tmp_path, campaign_policy=v5_policy(rounds=1, attempts_per_round=1), run_name="first"
    )

    def cut() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await first.run(_hooks={"after_baseline_bound": cut})
    second = successor_of(first, tmp_path)
    before = first.records()

    with pytest.raises(LineageNotFinished, match="^lineage_not_finished$"):
        await second.run(linked_from=first.run_root)

    assert not second.run_root.exists()
    assert first.records() == before


async def test_an_altered_champion_copy_is_refused(tmp_path: Path) -> None:
    first = await finished_rig(tmp_path, "first")
    champion = champion_path(first)
    target = champion / "skill.py"
    mode = target.stat().st_mode
    target.chmod(mode | 0o200)
    target.write_text("tampered\n", encoding="utf-8")
    target.chmod(mode)
    second = successor_of(first, tmp_path)
    second.seed_root = seed_tree(tmp_path / "second-other-seed")

    with pytest.raises(LineageChampionUnverified, match="^lineage_champion_unverified$"):
        await second.run(linked_from=first.run_root)

    assert not second.run_root.exists()


async def closed_with_the_ledger_ahead(tmp_path: Path) -> Rig:
    """A predecessor closed while its ledger is one champion ahead.

    Round 1 promotes; the journal is cut right before its `round_closed
    (promoted)` -- a crash between the store's promotion and the journal's
    record -- and the campaign is then closed under a changed scorer. The
    journal finishes on champion `v0` while the ledger's latest is `v1`.
    """

    first = await finished_rig(tmp_path, "first")
    journal = first.run_root / "campaign.jsonl"
    lines = journal.read_bytes().splitlines(keepends=True)
    cut = next(
        index
        for index, line in enumerate(lines)
        if json.loads(line)["event"] == "round_closed"
        and json.loads(line)["payload"]["outcome"] == "promoted"
    )
    journal.write_bytes(b"".join(lines[:cut]))
    assert [entry.version_id for entry in ArtifactStore.inspect(first.run_root).champions] == [
        "v0",
        "v1",
    ]
    changed = first.scorer_with_identity_digest("f" * 64)
    with pytest.raises(IdentityChanged) as excinfo:
        await first.resume(scorer=changed)
    assert excinfo.value.effect == "close" and excinfo.value.instance_digest is not None
    decision = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "scorer v2",
        now="2026-09-18T10:00:00Z",
    )
    result = await first.resume(scorer=changed, authorizations=[decision])
    assert result.finished is not None and result.finished["reason"] == "condition_changed"
    # The printed seed is the journal's champion copy, not the ledger's latest.
    assert result.status is not None
    assert result.status.to_payload()["campaign_outcome"]["champion_path"] == (
        "artifacts/champions/v0"
    )
    return first


async def test_a_successor_links_from_the_journals_champion_when_the_ledger_moved_ahead(
    tmp_path: Path,
) -> None:
    """The journal's champion is verified among the ledger's."""

    first = await closed_with_the_ledger_ahead(tmp_path)
    ledger = (first.run_root / "artifacts.jsonl").read_bytes()
    before = first.records()
    second = successor_of(first, tmp_path)
    second.seed_root = first.run_root / "artifacts" / "champions" / "v0"

    result = await second.run(linked_from=first.run_root)

    assert result.terminal == "finished"
    linked = second.records()[0]["payload"]["identity"]["linked_from"]
    v0 = ArtifactStore.inspect(first.run_root).champions[0]
    assert linked["final_champion_digest"] == v0.candidate_digest
    assert linked["final_champion_dependency_digest"] == v0.dependency_digest
    # Read-only: nothing is written to the predecessor.
    assert first.records() == before
    assert (first.run_root / "artifacts.jsonl").read_bytes() == ledger


async def test_a_corrupted_journal_champion_still_refuses_when_the_ledger_moved_ahead(
    tmp_path: Path,
) -> None:
    first = await closed_with_the_ledger_ahead(tmp_path)
    target = first.run_root / "artifacts" / "champions" / "v0" / "skill.py"
    mode = target.stat().st_mode
    target.chmod(mode | 0o200)
    target.write_text("tampered\n", encoding="utf-8")
    target.chmod(mode)
    second = successor_of(first, tmp_path)
    second.seed_root = seed_tree(tmp_path / "second-other-seed")

    with pytest.raises(LineageChampionUnverified, match="^lineage_champion_unverified$"):
        await second.run(linked_from=first.run_root)

    assert not second.run_root.exists()


async def test_a_journal_champion_the_ledger_never_promoted_refuses(tmp_path: Path) -> None:
    """A champion digest the ledger never recorded does not verify, whatever the copy."""

    first = await finished_rig(tmp_path, "first")
    journal = first.run_root / "campaign.jsonl"
    records = [json.loads(line) for line in journal.read_bytes().splitlines()]
    from evolution.tree.campaign import _predecessor, _verify_champion

    reduced = _predecessor(first.run_root, expected_family=14).reduced
    forged = replace(reduced, champion=(reduced.champion[0], "0" * 64))
    assert records[-1]["event"] == "campaign_finished"
    with pytest.raises(LineageChampionUnverified):
        _verify_champion(first.run_root, forged)


# -- concurrency, vetoes, thresholds and cancellation ---------------------


async def test_parallel_attempts_open_together_and_rank_by_frozen_index(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1, attempts_per_round=2, parallel_attempts=2, max_concurrency=2
        ),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
    )
    result = await rig.run()

    assert result.terminal == "finished"
    events = rig.events()
    first_close = events.index("attempt_closed")
    assert events[:first_close].count("attempt_opened") == 2
    selection = [r["payload"] for r in rig.records() if r["event"] == "selection_decided"][0]
    assert [row["attempt_id"] for row in selection["ranking"]] == [
        attempt_id_for(1, 1),
        attempt_id_for(1, 2),
    ]
    assert selection["attempt_id"] in {attempt_id_for(1, 1), attempt_id_for(1, 2)}


async def test_a_critical_failure_vetoes_the_attempt_without_a_look(tmp_path: Path) -> None:
    cases = corpus(2, critical=("case-01",))
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        cases=cases,
    )
    rig.scorer = PromotingScorer(
        scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest,
        critical={"case-01": False},
    )
    result = await rig.run()

    assert result.terminal == "finished"
    events = rig.events()
    assert "look_evaluated" not in events
    assert "alpha_consumed" not in events
    closed = [r["payload"] for r in rig.records() if r["event"] == "attempt_closed"][0]
    assert (closed["outcome"], closed["reason"]) == ("rejected", "critical_failure")


async def test_the_caller_token_stops_the_campaign_after_the_barrier(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    stop = False

    def after_measurement_bound() -> None:
        nonlocal stop
        stop = True

    result = await rig.run(
        cancelled=lambda: stop, _hooks={"after_measurement_bound": after_measurement_bound}
    )

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")
    assert "attempt_opened" not in rig.events()


class CancellingEditor:
    """An editor that trips the caller's token and then ignores its deadline.

    It proves spec §7's one exception to "no attempt is closed on a stopping
    path": the attempt whose editor the barrier cancelled closes
    `abandoned(editor_failed, cancelled)`.
    """

    def __init__(self, stop: Callable[[], None]) -> None:
        self.identity = EditorIdentity("cancelling", "d" * 64)
        self.stop = stop

    async def edit(self, request: Any) -> Any:
        self.stop()
        await asyncio.sleep(3600.0)
        raise AssertionError("the editor was not cancelled")


async def test_cancelling_mid_edit_closes_only_the_cancelled_attempt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=2))
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    rig.editor = CancellingEditor(stop)  # type: ignore[assignment]

    result = await rig.run(cancelled=lambda: stopped)

    assert result.terminal == "finished"
    assert result.quiescent is True
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")
    failed = [r["payload"] for r in rig.records() if r["event"] == "editor_failed"]
    assert failed and failed[0]["error_class"] == "cancelled"
    closed = [r["payload"] for r in rig.records() if r["event"] == "attempt_closed"]
    assert [(row["outcome"], row["reason"]) for row in closed] == [("abandoned", "editor_failed")]
    assert "selection_decided" not in rig.events()


async def test_resume_refuses_a_run_root_without_a_campaign(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.run_root.mkdir()

    with pytest.raises(CampaignMissing):
        await rig.resume()


async def test_resume_recreates_a_declared_but_absent_measurement(tmp_path: Path) -> None:
    """Spec §4 step 3: the declaration is durable before the sibling exists."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_measurement_bound() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_measurement_bound": after_measurement_bound})
    assert rig.events() == ["campaign_created", "baseline_bound", "measurement_bound"]
    assert not (rig.run_root / "measurements" / measurement_id_for(None)).exists()

    result = await rig.resume()

    assert result.terminal == "finished"
    assert (rig.run_root / "measurements" / measurement_id_for(None) / "events.jsonl").exists()
    assert rig.events().count("measurement_bound") == 2


# -- invariants, boundary classification and a partly dispatched stage ----


async def test_an_impossible_record_is_an_invariant_violation_not_a_fault(
    tmp_path: Path,
) -> None:
    """A lifecycle refusal must never be laundered into `persistence_failed`."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run()
    before = (rig.run_root / "campaign.jsonl").read_bytes()

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
        cancelled=lambda: False,
        secrets=(),
        hooks=None,
        adaptive=rig.adaptive,
    )
    session.open_log(create=False)
    try:
        session.log.scan()  # type: ignore[union-attr]
        session.run_id = rig.records()[0]["run_id"]
        with pytest.raises(campaign_module.CampaignInvariantViolated) as excinfo:
            session.append(
                "baseline_bound",
                dict(
                    round=9,
                    measurement_id="m-nonsense",
                    champion_version="v0",
                    candidate_digest=DIGEST_A,
                ),
            )
        assert excinfo.value.rule
        assert session.log.healthy is True  # type: ignore[union-attr]
    finally:
        session.close()
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before


async def test_a_store_capacity_failure_after_creation_is_a_typed_terminal(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_store_created() -> None:
        raise ArtifactCapacityError("storage_capacity_exhausted")

    result = await rig.run(_hooks={"after_store_created": after_store_created})

    assert result.terminal == "storage_capacity_exhausted"
    assert (result.owner, result.code) == ("artifact_store", "storage_capacity_exhausted")
    assert result.finished is None
    assert rig.events() == ["campaign_created", "capacity_exhausted"]


async def test_a_preflight_shortfall_is_the_capacity_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §7: *every* S2 `ArtifactCapacityError` is the capacity terminal.

    `ArtifactStore._estimate` raises the preflight code
    `storage_capacity_insufficient`, which reaches C1 at `create` and again at
    `resume_campaign`'s own preflight; both are the resumable capacity terminal,
    not the non-resumable `persistence_failed`.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    full = os.statvfs_result((4096, 4096, 100, 1, 1, 0, 0, 0, 0, 255))
    monkeypatch.setattr(os, "statvfs", lambda path: full)

    result = await rig.run()

    assert result.terminal == "storage_capacity_exhausted"
    assert (result.owner, result.code) == ("artifact_store", "storage_capacity_insufficient")
    assert rig.events() == ["campaign_created", "capacity_exhausted"]
    capacity = [r["payload"] for r in rig.records() if r["event"] == "capacity_exhausted"]
    assert capacity[0]["code"] == "storage_capacity_insufficient"

    again = await rig.resume()

    assert again.terminal == "storage_capacity_exhausted"
    assert (again.owner, again.code) == ("artifact_store", "storage_capacity_insufficient")


async def test_a_store_io_failure_at_resume_is_persistence_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})

    def broken(*args: Any, **kwargs: Any) -> None:
        raise ArtifactError("io_failure")

    monkeypatch.setattr(campaign_module.ArtifactStore, "create", broken)
    result = await rig.resume()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("artifact_store", "io_failure")
    assert "capacity_exhausted" not in rig.events()


async def test_a_fresh_campaign_whose_journal_cannot_be_opened_is_persistence_failed(
    tmp_path: Path,
) -> None:
    """The §6 terminal, not a private `_Fault` escaping past every handler.

    An existing but non-writable run directory is empty, so W1's admission and
    C1's own preflight both pass; `AppendLog.open(create=True)` is the first
    thing that touches the file system for writing, and it cannot create
    `.campaign.lock`. Nothing durable exists at that point and there is no writer
    state to project, but the operator is still owed the persistence outcome the
    §6 contract promises, shaped exactly as the resume paths shape theirs.
    """

    if os.geteuid() == 0:
        pytest.skip("root writes into a mode 0o500 directory")
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    rig.run_root.mkdir(parents=True)
    rig.run_root.chmod(0o500)

    try:
        result = await rig.run()
    finally:
        # Restored so `tmp_path` cleanup can remove the directory.
        rig.run_root.chmod(0o700)

    assert result.terminal == "persistence_failed"
    assert result.owner == "campaign_journal"
    assert result.finished is None
    assert result.status is not None
    assert not (rig.run_root / "campaign.jsonl").exists()


async def test_a_measurement_journal_fault_names_its_own_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def broken(self: Any, imported: Any = ()) -> None:
        raise JournalFault("storage_capacity_exhausted")

    monkeypatch.setattr(campaign_module.Measurement, "create", broken)
    result = await rig.run()

    assert result.terminal == "storage_capacity_exhausted"
    assert (result.owner, result.code) == (
        "measurement_journal",
        "storage_capacity_exhausted",
    )
    capacity = [r["payload"] for r in rig.records() if r["event"] == "capacity_exhausted"]
    assert capacity[0]["owner"] == "measurement_journal"
    assert capacity[0]["measurement_id"] == measurement_id_for(None)


async def test_resume_refuses_a_target_that_can_no_longer_serve_the_policy(
    tmp_path: Path,
) -> None:
    """PDR-0010: a shrink *below* the demand is hard, and it keeps its own code.

    It used to be laundered into `run_identity_changed`, which offered the
    operator a decision that could not help: no authorization makes an
    oversubscribed policy runnable.
    """

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=2),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
    )

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})

    with pytest.raises(CapabilitiesInsufficient) as excinfo:
        await rig.resume(adapter=FakeAdapter(max_instances=1, max_concurrent_sessions=1))
    assert str(excinfo.value) == "capabilities_insufficient"


async def test_a_shrink_the_policy_still_fits_is_an_authorizable_difference(
    tmp_path: Path,
) -> None:
    """The other half of §3: admitted capabilities that moved are ordinary drift."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, max_concurrency=2),
        adapter=FakeAdapter(max_instances=4, max_concurrent_sessions=2),
    )

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})

    smaller = FakeAdapter(max_instances=2, max_concurrent_sessions=2)
    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(adapter=smaller)
    assert not excinfo.value.hard
    assert [d.field for d in excinfo.value.differences] == ["identity.capabilities.max_instances"]
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "the pool shrank",
        now="2026-09-17T10:00:00Z",
    )

    result = await rig.resume(adapter=smaller, authorizations=[auth])

    assert result.terminal == "finished"
    assert rig.events().count("identity_rebased") == 1


async def test_a_campaign_journal_fault_stays_a_typed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal's own append must not escape as a bare `JournalFault`."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    original = AppendLog.append

    def faulting(self: Any, run_id: str, event: str, *args: Any, **kwargs: Any) -> int:
        if event == "baseline_bound":
            self._healthy = False
            raise JournalFault("storage_capacity_exhausted")
        return original(self, run_id, event, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AppendLog, "append", faulting)
    result = await rig.run()

    assert result.terminal == "storage_capacity_exhausted"
    assert (result.owner, result.code) == ("campaign_journal", "storage_capacity_exhausted")
    assert result.finished is None
    assert rig.events() == ["campaign_created"]


async def test_a_wedged_measurement_is_a_persistence_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment 20: a measurement recovery cannot leave is a persistence terminal.

    A leaked instance keeps S1 in recovery, and S1 refuses every append while it
    still owns one — `JournalRecoveryRequired("journal_busy")`. §7 says a
    refusal from a log's own recovery "surfaces as the same `persistence_failed`
    with the log's code", so that is the terminal, reached through `_classify`
    like any other boundary error rather than by a rule of C1's own. The leak
    itself stays visible where §7 puts it: the status projection.
    """

    # Two looks, so the torn stage is not the measurement's last one and its
    # instances are still owned when the crash lands.
    rig = Rig(
        tmp_path,
        campaign_policy=policy(
            rounds=1,
            attempts_per_round=1,
            alpha={"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}},
        ),
    )
    original = Measurement.run_stage

    async def partial(self: Any, plan: Any, **kwargs: Any) -> Any:
        await original(self, StagePlan(plan.stage, plan.look, plan.rows[:1]), **kwargs)
        raise Crash("power cut")

    monkeypatch.setattr(Measurement, "run_stage", partial)
    with pytest.raises(Crash):
        await rig.run()
    monkeypatch.undo()

    result = await rig.resume(adapter=RefusingAdapter())

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("measurement_journal", "journal_busy")
    assert result.quiescent is True
    assert result.finished is None
    assert "campaign_finished" not in rig.events()
    assert "attempt_opened" not in rig.events()

    # The opened stage stays short, behind a persistence terminal like every
    # other admitted exception to ADR-0008's completeness rule: nothing can
    # complete it, because the journal refuses every append while its own
    # recovery is unfinished.
    seed = measurement_id_for(None)
    events = [
        json.loads(line)
        for line in (rig.run_root / "measurements" / seed / "events.jsonl")
        .read_bytes()
        .splitlines()
        if line
    ]
    terminals = {
        (row["payload"]["case_id"], row["payload"]["sample_id"])
        for row in events
        if row["event"] in ("rollout_finished", "rollout_failed", "rollout_interrupted")
    }
    opened = [r["payload"] for r in rig.records() if r["event"] == "stage_opened"]
    assert opened and len(terminals) < opened[0]["sample_count"]
    # The journal is the reason nothing can complete it: its own recovery never
    # finished, so S1 refuses the appends a completion would need.
    status = result.status.to_payload()
    entry = next(row for row in status["measurements"] if row["measurement_id"] == seed)
    assert entry["status"]["owned_instances"]


async def test_a_leak_on_a_complete_measurement_only_shows_in_the_status(
    tmp_path: Path,
) -> None:
    """Ruling 10's other half: the campaign continues past a released-late instance."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=RefusingAdapter(max_instances=8),
    )

    def after_editor_finished() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_editor_finished": after_editor_finished})
    assert "baseline_complete" in rig.events()

    result = await rig.resume(adapter=RefusingAdapter(max_instances=8))

    assert result.terminal == "finished"
    assert result.finished is not None
    assert result.finished["outcome"] == "completed"
    status = result.status.to_payload()
    baseline = next(
        entry
        for entry in status["measurements"]
        if entry["measurement_id"] == measurement_id_for(None)
    )
    assert baseline["status"]["owned_instances"]


# -- the caller-supplied run_id (W1 §6 step 5) ----------------------------


async def test_run_campaign_uses_the_caller_run_id(tmp_path: Path) -> None:
    """The id the caller hands in is the one every record and projection carries."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    result = await rig.run(run_id="c-my-run")

    assert result.terminal == "finished"
    assert result.status.to_payload()["run_id"] == "c-my-run"
    assert {record["run_id"] for record in rig.records()} == {"c-my-run"}
    assert read_run_id(rig.run_root) == "c-my-run"


async def test_resume_refuses_a_different_run_id(tmp_path: Path) -> None:
    """An adapter built on another id is a caller bug, refused before a record."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run(run_id="c-one")
    before = rig.records()

    with pytest.raises(CampaignInvariantViolated, match="run_id_mismatch"):
        await rig.resume(run_id="c-two")

    assert rig.records() == before
    assert (await rig.resume(run_id="c-one")).terminal == "finished"
    assert (await rig.resume()).terminal == "finished"
    assert read_run_id(rig.run_root) == "c-one"


@pytest.mark.parametrize("bad", ["", "c/one", "c-" + "x" * 94])
async def test_run_id_is_validated_before_any_write(tmp_path: Path, bad: str) -> None:
    """`IDENTIFIER` and the 95-character bound of `<run_id>-<32 hex>` (ADR-0007)."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    with pytest.raises(ValueError, match="run_id_invalid") as excinfo:
        await rig.run(run_id=bad)
    assert type(excinfo.value) is ValueError
    assert not rig.run_root.exists()

    with pytest.raises(ValueError, match="run_id_invalid") as resumed:
        await rig.resume(run_id=bad)
    assert type(resumed.value) is ValueError
    assert not rig.run_root.exists()


async def test_the_longest_accepted_run_id_still_makes_an_instance_id(
    tmp_path: Path,
) -> None:
    """95 characters is exactly what `new_instance_id` can suffix and keep legal."""

    longest = "c-" + "x" * 93
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    result = await rig.run(run_id=longest)

    assert len(longest) == MAX_RUN_ID
    assert result.terminal == "finished"
    assert read_run_id(rig.run_root) == longest
    assert IDENTIFIER.match(new_instance_id(longest)) is not None


async def test_default_run_id_is_still_generated(tmp_path: Path) -> None:
    """A library caller that passes nothing keeps C1's own `c-<uuid>`."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    result = await rig.run()

    generated = read_run_id(rig.run_root)
    assert generated == result.status.to_payload()["run_id"]
    assert generated.startswith("c-")
    assert len(generated) == len("c-") + 32


# -- edit_brief/2: one brief per round -------------------------------------


class RecordingEditor(ScriptedEditor):
    """A `ScriptedEditor` that keeps every brief it was handed, by attempt id."""

    def __init__(self, script, *, slow: dict[str, float] | None = None) -> None:
        super().__init__(script)
        self.briefs: dict[str, EditBrief] = {}
        self.slow = dict(slow or {})

    async def edit(self, request: EditRequest) -> EditResult:
        self.briefs[request.attempt_id] = request.brief
        await asyncio.sleep(self.slow.get(request.attempt_id, 0.0))
        return await super().edit(request)


def _two_round_script() -> dict:
    return {
        attempt_id_for(1, 1): EditorScript("edit", {"skill.py": b"VALUE = 2\n"}),
        attempt_id_for(1, 2): EditorScript("declined"),
        attempt_id_for(2, 1): EditorScript("edit", {"skill.py": b"VALUE = 3\n"}),
        attempt_id_for(2, 2): EditorScript("edit", {"skill.py": b"VALUE = 4\n"}),
    }


async def test_the_round_two_brief_carries_round_one_and_is_the_same_for_both_attempts(
    tmp_path: Path,
) -> None:
    editor = RecordingEditor(_two_round_script())
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=2, attempts_per_round=2, parallel_attempts=2),
        editor=editor,
    )
    result = await rig.run()

    assert result.terminal == "finished"
    first = editor.briefs[attempt_id_for(1, 1)]
    assert first.history.rounds_closed == 0 and first.history.rounds == ()
    second_a, second_b = editor.briefs[attempt_id_for(2, 1)], editor.briefs[attempt_id_for(2, 2)]
    # The adaptive current-family path freezes equivalent role inputs for the
    # round's attempts; the prior round's distinct brief is not reused.
    assert first is not second_a
    assert second_a == second_b
    assert brief_digest(second_a) == brief_digest(second_b)
    history = second_a.history
    assert history.rounds_closed == 1 and history.rounds_shown == (1,)
    [round_one] = history.rounds
    assert round_one.outcome == "promoted" and round_one.selected_attempt == attempt_id_for(1, 1)
    edited, declined = round_one.attempts
    assert edited.changes.status == "present" and edited.changes.paths == ("skill.py",)
    assert (
        edited.result.status == "present"
        and edited.result.cases[0].repeats[0].primary_score is not None
    )
    assert (
        declined.editor == "declined"
        and declined.changes.status == "absent"
        and declined.result.status == "absent"
    )
    # The deterministic fake scorer writes the same trace a subprocess scorer
    # does (the Reflector's evidence is read from it), so every sample's
    # observation authenticates.
    assert {repeat.observation.status for case in second_a.cases for repeat in case.repeats} == {
        "present"
    }
    assert second_a.champion_digest == edited.candidate_digest


async def test_the_brief_does_not_depend_on_which_attempt_finishes_first(tmp_path: Path) -> None:
    digests = []
    for slow in ({attempt_id_for(1, 1): 0.2}, {attempt_id_for(1, 2): 0.2}):
        editor = RecordingEditor(_two_round_script(), slow=slow)
        rig_root = tmp_path / str(len(digests))
        rig_root.mkdir(parents=True, exist_ok=True)
        rig = Rig(
            rig_root,
            campaign_policy=policy(rounds=2, attempts_per_round=2, parallel_attempts=2),
            editor=editor,
        )
        result = await rig.run()
        assert result.terminal == "finished"
        digests.append(brief_digest(editor.briefs[attempt_id_for(2, 1)]))
    assert digests[0] == digests[1]


async def test_resume_after_round_one_recomputes_the_same_brief(tmp_path: Path) -> None:
    editor = RecordingEditor(_two_round_script())
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=2, attempts_per_round=2, parallel_attempts=1),
        editor=editor,
    )

    def crash_after_round_one() -> None:
        raise Crash("after round_closed(1)")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_round_closed": crash_after_round_one})
    assert attempt_id_for(2, 1) not in editor.briefs

    resumed = RecordingEditor(_two_round_script())
    result = await rig.resume(editor=resumed)
    assert result.terminal == "finished"
    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir(parents=True, exist_ok=True)
    fresh = Rig(
        fresh_root,
        campaign_policy=policy(rounds=2, attempts_per_round=2, parallel_attempts=1),
        editor=RecordingEditor(_two_round_script()),
    )
    await fresh.run()
    assert brief_digest(resumed.briefs[attempt_id_for(2, 1)]) == brief_digest(
        fresh.editor.briefs[attempt_id_for(2, 1)]
    )


# -- the reflector round, end to end (spec §3.3) --------------------------

#: One diagnosis group over the fixtures' one writable path; the scripted
#: reflector fills in `evidence_ids` / `affected_cases` from the catalog it was
#: actually shown when it is built with `cite_first_exemplar`.
GROUP: dict[str, Any] = {
    "root_cause": "the skill never answers the asked question",
    "suggested_fix": "state the answer in the first line",
    "counterevidence": "No selected passing observation contradicts this diagnosis.",
    "regression_risks": "Keep the existing answer format.",
    "why_this_layer": "The failure is caused by the editable skill text.",
    "evidence_ids": [],
    "affected_cases": [],
    "target_paths": ["skill.py"],
}


@pytest.fixture
def failing_scorer() -> DeterministicScorer:
    """Every sample fails its one `answered` verdict, so the baseline has a
    failure mode to cluster and the round has something to chase."""

    return DeterministicScorer(verdicts=(FAILING_VERDICT,))


@pytest.fixture
def passing_scorer() -> DeterministicScorer:
    """Every sample passes: nothing to focus on, so the round is clean."""

    return DeterministicScorer(verdicts=(PASSING_VERDICT,))


@pytest.fixture
def recording_adapter() -> FakeAdapter:
    return FakeAdapter()


def corpus_with_oracle(needle: str, n: int = 2) -> tuple[Any, ...]:
    """`corpus(n)` with `needle` in every case's oracle and nowhere else."""

    return tuple(replace(case, oracle={**case.oracle, "answer": needle}) for case in corpus(n))


def after_event(rig: Rig, name: str) -> Callable[[], bool]:
    """A `cancelled()` predicate that flips once the journal holds `name`."""

    def cancelled() -> bool:
        path = rig.run_root / "campaign.jsonl"
        if not path.is_file():
            return False
        return name in [
            json.loads(line)["event"] for line in path.read_bytes().splitlines() if line
        ]

    return cancelled


def payloads_of(rig: Rig, event: str) -> list[dict[str, Any]]:
    return [record["payload"] for record in rig.records() if record["event"] == event]


def _truncate_after(rig: Rig, event: str) -> None:
    """Throw away everything the journal holds after the last `event` record.

    The durable shape a process killed mid-call leaves: a `reflection_started`
    with no terminal. A cancellation cannot stage it -- the wrapper writes the
    reflection's own terminal before the drive loop sees the token -- and no
    campaign seam fires between the two records.
    """

    path = rig.run_root / "campaign.jsonl"
    lines = [line for line in path.read_bytes().splitlines(keepends=True) if line.strip()]
    last = max(index for index, line in enumerate(lines) if json.loads(line)["event"] == event)
    path.write_bytes(b"".join(lines[: last + 1]))


async def test_clean_round_opens_no_attempt(
    tmp_path: Path, passing_scorer: DeterministicScorer
) -> None:
    rig = Rig(
        tmp_path,
        scorer=passing_scorer,
        campaign_policy=policy(rounds=1, attempts_per_round=2),
    )

    result = await rig.run()

    events = rig.events()
    assert "reflection_started" not in events and "attempt_opened" not in events
    assert payloads_of(rig, "focus_portion_selected")[0]["reason"] == "none"
    assert payloads_of(rig, "round_closed")[0]["outcome"] == "clean"
    assert result.finished is not None and result.finished["outcome"] == "completed"


async def test_a_reported_failure_is_the_rounds_diagnosis(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    """A Reflector that reports `failed` is one `reflection_failed(reported)`."""

    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=ScriptedReflector({1: ReflectorScript("failed", note="no idea")}),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    await rig.run()

    failed = payloads_of(rig, "reflection_failed")[0]
    assert failed["error_class"] == "reported" and failed["note_digest"] is not None
    assert rig.events().count("reflection_started") == 1


async def test_invalid_groups_are_a_failed_reflection(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    bad = {**GROUP, "evidence_ids": ["obs:nope:9"]}
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=ScriptedReflector({1: ReflectorScript("reflect", groups=[bad])}),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    await rig.run()

    assert payloads_of(rig, "reflection_failed")[0]["error_class"] == "invalid_result"
    assert "reflection_finished" not in rig.events()


async def test_oracle_needle_in_diagnosis_is_a_leak(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    needle = "the secret answer"
    reflector = ScriptedReflector(
        {1: ReflectorScript("reflect", groups=[{**GROUP, "suggested_fix": f"say {needle}"}])},
        cite_first_exemplar=True,
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        cases=corpus_with_oracle(needle),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    await rig.run()

    assert payloads_of(rig, "reflection_failed")[0]["error_class"] == "oracle_leak"
    assert needle not in (rig.run_root / "campaign.jsonl").read_text(encoding="utf-8")


async def test_a_run_secret_in_a_sentence_is_a_failed_reflection(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    """A model that echoes a forwarded credential back is a bad reflection, not
    a storage failure: without the scan the journal's own redactor refuses the
    `reflection_finished` record and `CampaignInvariantViolated` escapes the
    run."""

    secret = "sk-forwarded-credential"
    reflector = ScriptedReflector(
        {1: ReflectorScript("reflect", groups=[{**GROUP, "root_cause": f"it sends {secret}"}])},
        cite_first_exemplar=True,
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    result = await rig.run(secrets=(secret,))

    assert result.terminal == "finished"
    assert payloads_of(rig, "reflection_failed")[0]["error_class"] == "invalid_result"
    assert "reflection_finished" not in rig.events()
    assert secret not in (rig.run_root / "campaign.jsonl").read_text(encoding="utf-8")


async def test_a_run_secret_outside_the_sentences_is_a_failed_reflection(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    """Every model-controlled string of a group is scanned, not the sentences alone.

    Branch review round 2, `reflection-secret-outside-sentences-crashes`: with
    the scan limited to the note and the two sentences, a secret in
    `target_paths` passed group validation and was then refused by the journal
    envelope's own redactor inside `append`, so `CampaignInvariantViolated`
    escaped `_finish` -- which catches only `_Fault` -- and aborted the whole
    campaign after `reflection_started` instead of failing the one reflection.
    """

    secret = "sk-forwarded-credential"
    reflector = ScriptedReflector(
        {1: ReflectorScript("reflect", groups=[{**GROUP, "target_paths": [f"{secret}.py"]}])},
        cite_first_exemplar=True,
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    result = await rig.run(secrets=(secret,), artifact_policy=ArtifactPolicyV1((), (), ("**",)))

    assert result.terminal == "finished"
    assert payloads_of(rig, "reflection_failed")[0]["error_class"] == "invalid_result"
    assert "reflection_finished" not in rig.events()
    assert secret not in (rig.run_root / "campaign.jsonl").read_text(encoding="utf-8")


async def test_the_probe_capability_in_a_diagnosis_is_a_failed_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_scorer: DeterministicScorer
) -> None:
    """#318 part 2 review: final admission scans the reflection's own secrets.

    Once a probe mediator started, the reflection's run secrets include its
    capability, the path segment of the probe URL a non-chat Reflector is
    given. A diagnosis that echoes it is the reflection's own `invalid_result`,
    as before the admission block moved into `admit_reflection_result`; it is
    never journaled.
    """

    from evolution.tree.roles.reflector_contracts import ReflectRequest, ReflectResult
    from tests.tree.target.test_probe_session import config as probe_config

    async def ready(*_args: Any, **_kwargs: Any) -> bool:
        return True

    class EchoesTheCapability(ScriptedReflector):
        async def reflect(self, request: ReflectRequest) -> ReflectResult:
            assert request.probe is not None
            capability = request.probe.url.rstrip("/").split("/")[-2]
            assert len(capability) == 32
            echoed = {**GROUP, "root_cause": f"the probe at {capability} disagrees"}
            self.script = {request.round: ReflectorScript("reflect", groups=[echoed])}
            return await super().reflect(request)

    monkeypatch.setattr(campaign_module, "check_probe_health", ready)
    reflector = EchoesTheCapability({}, cite_first_exemplar=True)
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    result = await rig.run(probe=probe_config("http://127.0.0.1:9"))

    assert result.terminal == "finished"
    assert "probe_checked" in rig.events()
    (failed,) = payloads_of(rig, "reflection_failed")
    assert (failed["error_class"], failed["reason"]) == ("invalid_result", "invalid_result")
    assert "reflection_finished" not in rig.events()
    (request,) = reflector.requests
    assert request.probe is not None
    capability = request.probe.url.rstrip("/").split("/")[-2]
    assert capability not in (rig.run_root / "campaign.jsonl").read_text(encoding="utf-8")


async def test_a_reflection_workspace_io_failure_is_the_persistence_failed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_scorer: DeterministicScorer
) -> None:
    """`_file_boundary` is what keeps the campaign's own private file work --
    the Parent projection one reflection reads, the environment-docs snapshot
    -- inside §7's document: a bare `OSError` from `project` becomes the
    `persistence_failed`/`artifact_store` fault, not an unhandled exception."""

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(campaign_module, "project", broken)
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=ScriptedReflector({}),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    result = await rig.run()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("artifact_store", "reflection_workspace_unavailable")


async def test_a_focus_that_does_not_reproduce_is_an_invariant_violation(
    tmp_path: Path, failing_scorer: DeterministicScorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 10: a resume that recomputes another focus than the journal
    records is an executor bug, not a storage failure -- every later step of
    the round (the brief, the gate's scope) would otherwise be built on other
    evidence than `focus_portion_selected` claims."""

    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=ScriptedReflector({}),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    await rig.run(cancelled=after_event(rig, "focus_portion_selected"))
    _truncate_after(rig, "focus_portion_selected")

    original = campaign_module.select_portion

    def disagree(*args: Any, **kwargs: Any) -> Any:
        real = original(*args, **kwargs)
        return replace(real, selected_cohort_ids=())

    monkeypatch.setattr(campaign_module, "select_portion", disagree)
    resumed = Rig(
        tmp_path,
        scorer=DeterministicScorer(verdicts=(FAILING_VERDICT,)),
        reflector=ScriptedReflector({}),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    with pytest.raises(
        CampaignInvariantViolated, match="adaptive focus portion is not reproducible"
    ):
        await resumed.resume()


async def test_history_reaches_the_next_rounds_reflector(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    reflector = ScriptedReflector({})
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        campaign_policy=policy(rounds=2, attempts_per_round=1),
    )

    await rig.run()

    assert len(reflector.requests) == 2
    second = reflector.requests[1].evidence
    assert second.history.rounds_closed == 1
    assert second.history.rounds[0].diagnosis["status"] == "absent"
    assert second.history.rounds[0].diagnosis["reflection_id"] == "r01-x01"


async def test_exam_cases_are_dispatched_first(
    tmp_path: Path, failing_scorer: DeterministicScorer, recording_adapter: FakeAdapter
) -> None:
    reflector = ScriptedReflector(
        {1: ReflectorScript("reflect", groups=[GROUP])},
        cite_first_exemplar=True,
        claim_two_cases=True,
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        reflector=reflector,
        adapter=recording_adapter,
        cases=corpus(4),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    await rig.run()

    scope = payloads_of(rig, "gate_scope_selected")[0]
    assert scope["source"] == "claim" and len(scope["cases"]) == 2
    opened = recording_adapter.session_case_order(
        measurement=measurement_id_for(attempt_id_for(1, 1))
    )
    exam = len(scope["cases"]) * rig.policy.repeats
    assert len(opened) == len(rig.cases) * rig.policy.repeats
    assert set(opened[:exam]) == set(scope["cases"])
    # Ordering only: the baseline, which has no scope, keeps the schedule's own
    # order, and every case is still measured.
    baseline = recording_adapter.session_case_order(measurement=measurement_id_for(None))
    assert baseline[:2] == ("case-01", "case-01")


async def test_a_fault_writing_the_unresponsive_terminal_still_reports_the_live_reflector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag precedes the append, as the editor's does.

    A campaign journal that faults while `reflection_failed(unresponsive)` is
    being written must still report the run as non-quiescent: the Reflector's
    task survived its grace and is running under the operator's account
    whatever the journal manages to record about it (spec §7).
    """

    monkeypatch.setattr(campaign_reflector, "EDITOR_GRACE_SECONDS", 0.05)
    original = campaign_module._Campaign.append

    def faulting(
        self: Any, event: str, payload: dict[str, Any], operation_id: str | None = None
    ) -> Any:
        if event == "reflection_failed":
            raise campaign_module._Fault(
                "persistence_failed", "campaign_journal", "journal_unavailable"
            )
        return original(self, event, payload, operation_id)

    monkeypatch.setattr(campaign_module._Campaign, "append", faulting)
    rig = Rig(
        tmp_path,
        scorer=DeterministicScorer(verdicts=(FAILING_VERDICT,)),
        reflector=ScriptedReflector({1: ReflectorScript("swallow_cancel", seconds=3600.0)}),
        campaign_policy=policy(rounds=1, attempts_per_round=1, reflection_timeout_seconds=0.05),
    )

    result = await rig.run()

    assert (result.terminal, result.owner, result.code) == (
        "persistence_failed",
        "campaign_journal",
        "journal_unavailable",
    )
    assert result.quiescent is False
    assert "reflection_failed" not in rig.events()


# -- edit_brief/3: the diagnosis and the seal-time leak check (Task 10) ---


async def test_seal_refuses_a_candidate_that_writes_the_oracle(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    needle = "the secret answer"
    editor = ScriptedEditor(
        {
            attempt_id_for(1, 1): EditorScript(
                "edit", writes={"skill.py": f"VALUE = 1  # {needle}\n".encode()}
            )
        }
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        cases=corpus_with_oracle(needle),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    await rig.run()

    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["reason"], closed["code"]) == ("seal_refused", "oracle_leak")


async def test_seal_ignores_a_needle_already_in_the_parent(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    needle = "the secret answer"
    editor = ScriptedEditor(
        {
            attempt_id_for(1, 1): EditorScript(
                "edit", writes={"skill.py": f"# skill {needle}\nmore\n".encode()}
            )
        }
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        cases=corpus_with_oracle(needle),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    # The parent already carries the needle's own line; only the editor's
    # genuinely new line ("more") is an added line for the leak check.
    seed_tree(rig.seed_root, f"# skill {needle}\n")

    await rig.run()

    assert payloads_of(rig, "attempt_closed")[0]["reason"] != "seal_refused"


async def test_seal_refuses_a_second_copy_of_a_needle_line_the_parent_already_had(
    tmp_path: Path, failing_scorer: DeterministicScorer
) -> None:
    """Multiplicity counts. The Parent holds one line carrying the needle and
    the Candidate adds an identical second one: the two line *sets* are equal,
    so the set difference the check used to take saw no added line at all and
    sealed the leak (branch review round 1, `candidate-leak-set-diff`). The
    ordered diff names that second copy an added line."""

    needle = "the secret answer"
    line = f"# skill {needle}\n"
    editor = ScriptedEditor(
        {attempt_id_for(1, 1): EditorScript("edit", writes={"skill.py": (line * 2).encode()})}
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        cases=corpus_with_oracle(needle),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    seed_tree(rig.seed_root, line)

    await rig.run()

    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["reason"], closed["code"]) == ("seal_refused", "oracle_leak")


def _flaky_scan_tree(calls: dict[str, int], real: Any, *, raises: Exception, after: int = 1) -> Any:
    """A `scan_tree` double: the real function for the first `after` calls,
    then `raises` for every call after that -- so a caller's own earlier scan
    (`unchanged`'s, say) still succeeds and only the later one (`candidate_leak`'s)
    fails."""

    def flaky(root: Path, policy: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > after:
            raise raises
        return real(root, policy)

    return flaky


async def test_candidate_leak_scan_unsafe_tree_refuses_the_seal_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_scorer: DeterministicScorer
) -> None:
    """`candidate_leak`'s own scan/diff maps an `ArtifactUnsafeError` into the
    same `seal_refused` path `store.seal`'s own refusals already use, with
    the error's own code -- never a reclassified `persistence_failed` fault
    """

    editor = ScriptedEditor(edits(attempt_id_for(1, 1)))
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    monkeypatch.setattr(
        campaign_module,
        "scan_tree",
        _flaky_scan_tree(
            {"n": 0}, campaign_module.scan_tree, raises=ArtifactUnsafeError("symlink")
        ),
    )

    await rig.run()

    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["reason"], closed["code"]) == ("seal_refused", "symlink")


async def test_candidate_leak_scan_io_failure_is_the_persistence_failed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_scorer: DeterministicScorer
) -> None:
    """A bare `OSError` from `candidate_leak`'s scan/diff is `store.seal`'s own
    `io_failure`: wrapped into `ArtifactError` so `_boundary` classifies it
    into the campaign's `persistence_failed` terminal, not an unhandled
    exception."""

    editor = ScriptedEditor(edits(attempt_id_for(1, 1)))
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    monkeypatch.setattr(
        campaign_module,
        "scan_tree",
        _flaky_scan_tree({"n": 0}, campaign_module.scan_tree, raises=OSError("disk fell over")),
    )

    result = await rig.run()

    assert result.terminal == "persistence_failed"
    assert (result.owner, result.code) == ("artifact_store", "io_failure")


async def test_resume_refuses_a_leak_the_store_already_sealed_before_the_crash(
    tmp_path: Path, failing_scorer: DeterministicScorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ArtifactStore.seal` fires `after_seal_finished` between
    publishing the Candidate and removing the workspace (spec §6.4), and
    `_Campaign` forwards a hook under that name to the store unchanged (spec
    §2 ruling 1). A crash there leaves a durably *published* Candidate with
    no `attempt_sealed` record -- exactly `reconcile_attempts`'s branch.

    The live round's own needle set is patched empty for its one
    construction only (`oracle_needles` returns `()` on the first call), so
    the live `seal_attempt` -- which always runs the leak check first -- lets
    a genuinely leaking write through to `store.seal` uncontested; the
    resumed campaign's second, *real* needle set must then still refuse the
    already-published Candidate retroactively, rather than trusting the
    store's own record and re-appending `attempt_sealed`.

    (This leaves the store holding a published `candidate/<attempt_id>` copy
    the campaign's own journal now says was never sealed -- an orphan the
    store never reclaims, worth a docs note for Task 12.)
    """

    needle = "the secret answer"
    real_oracle_needles = campaign_module.oracle_needles
    calls = {"n": 0}

    def once_empty(corpus: Any, declared: Any = ()) -> tuple[str, ...]:
        calls["n"] += 1
        return () if calls["n"] == 1 else real_oracle_needles(corpus, declared)

    monkeypatch.setattr(campaign_module, "oracle_needles", once_empty)

    editor = ScriptedEditor(
        {
            attempt_id_for(1, 1): EditorScript(
                "edit", writes={"skill.py": f"VALUE = 1  # {needle}\n".encode()}
            )
        }
    )
    rig = Rig(
        tmp_path,
        scorer=failing_scorer,
        editor=editor,
        cases=corpus_with_oracle(needle),
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )

    def crash_after_seal() -> None:
        raise Crash("after_seal_finished")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_seal_finished": crash_after_seal})
    assert "attempt_sealed" not in rig.events()

    await rig.resume()

    closed = payloads_of(rig, "attempt_closed")
    assert closed and (closed[-1]["reason"], closed[-1]["code"]) == ("seal_refused", "oracle_leak")
    assert "attempt_sealed" not in rig.events()


# -- operator authorizations: the rebase of a live run ---------------------


def _rewrite_first_record(path: Path, **payload_updates: object) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[0])
    record["payload"].update(payload_updates)
    lines[0] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines))


def _ledger_records(rig: Rig) -> list[dict[str, Any]]:
    raw = (rig.run_root / "artifacts.jsonl").read_bytes()
    return [json.loads(line) for line in raw.splitlines() if line]


async def test_resume_under_another_editor_reports_the_difference_and_its_digest(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})

    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=ScriptedEditor(edits("other")))
    changed = excinfo.value
    assert str(changed) == "run_identity_changed"
    assert not changed.hard
    assert [d.field for d in changed.differences] == ["identity.editor.digest"]
    assert changed.instance_digest is not None
    assert rig.events() == ["campaign_created"]  # nothing written by the refusal


async def test_an_authorized_editor_change_rebases_the_run_and_finishes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})
    other = ScriptedEditor(edits("r01-a01", "other"))
    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=other)
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "new editor",
        now="2026-09-17T10:00:00Z",
    )

    result = await rig.resume(editor=other, authorizations=[auth])

    assert result.terminal == "finished"
    events = rig.events()
    assert events[:2] == ["campaign_created", "identity_rebased"]
    rebase = [r for r in rig.records() if r["event"] == "identity_rebased"][0]["payload"]
    assert rebase["changed_fields"] == ["identity.editor.digest"]
    assert rebase["authorization"]["id"] == auth.id
    ledger = _ledger_records(rig)
    assert ledger[0]["event"] == "store_created"
    rebased = [r for r in ledger if r["event"] == "store_rebased"]
    assert len(rebased) == 1
    assert rebased[0]["payload"]["campaign_rebase"] == rebase["identity_digest"]
    assert rebased[0]["payload"]["authorization_id"] == auth.id
    # A further resume by the same editor needs nothing: the journal is standing authority.
    again = await rig.resume(editor=other)
    assert again.terminal == "finished"
    assert rig.events().count("identity_rebased") == 1
    assert [r["event"] for r in _ledger_records(rig)].count("store_rebased") == 1


async def test_an_older_producing_version_is_admitted_by_authorization_and_siblings_follow(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born, incomplete champion measurement

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
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
    assert rig.events().count("baseline_bound") == 1  # the champion is not re-measured
    assert rig.events().count("identity_rebased") == 1


async def test_a_crash_between_identity_rebased_and_store_rebased_is_completed_next_time(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born store, one record behind the campaign

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
    other = ScriptedEditor(edits("r01-a01", "other"))
    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=other)
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "new editor",
        now="2026-09-17T10:00:00Z",
    )

    def after_identity_rebased() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.resume(
            editor=other,
            authorizations=[auth],
            _hooks={"after_identity_rebased": after_identity_rebased},
        )
    assert rig.events().count("identity_rebased") == 1
    assert "store_rebased" not in [r["event"] for r in _ledger_records(rig)]

    result = await rig.resume(editor=other)  # no authorization needed: the journal declared it
    assert result.terminal == "finished"
    rebase = [r for r in rig.records() if r["event"] == "identity_rebased"][0]["payload"]
    rebased = [r for r in _ledger_records(rig) if r["event"] == "store_rebased"]
    assert len(rebased) == 1
    assert rebased[0]["payload"]["campaign_rebase"] == rebase["identity_digest"]
    assert rebased[0]["payload"]["authorization_id"] == auth.id


async def test_an_owed_store_rebase_is_completed_first_and_keeps_its_own_version(
    tmp_path: Path,
) -> None:
    """§6.2: the ledger's debt is paid before the next declaration, stamped as declared.

    ADR-0015: "a resume that finds the first without the second writes the
    second first." An `identity_rebased` that `0.3.0a11` declared and crashed
    before completing is completed by this invocation *before* it appends its
    own `identity_rebased`, and the record that completes it carries `0.3.0a11`
    -- the declaring record's version, not the running package's (codex
    round 1).
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born store, one record behind the campaign

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
    other = ScriptedEditor(edits("r01-a01", "other"))
    with pytest.raises(IdentityChanged) as first:
        await rig.resume(editor=other)
    editor_change = Authorization.new(
        "run_identity_changed",
        first.value.instance_digest,
        "new editor",
        now="2026-09-17T10:00:00Z",
    )

    def crash_after_identity_rebased() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.resume(
            editor=other,
            authorizations=[editor_change],
            _hooks={"after_identity_rebased": crash_after_identity_rebased},
        )
    assert "store_rebased" not in [r["event"] for r in _ledger_records(rig)]
    # The crashed invocation was `0.3.0a11`: its declaration says so, and the
    # ledger record that completes it must say so too, however late it arrives.
    journal = rig.run_root / "campaign.jsonl"
    lines = journal.read_bytes().splitlines(keepends=True)
    index = next(i for i, raw in enumerate(lines) if json.loads(raw)["event"] == "identity_rebased")
    record = json.loads(lines[index])
    record["payload"]["producing_version"] = "0.3.0a11"
    # The record's own validator recomputes the diff it declares, so the version
    # it now moves has to be named in `changed_fields` beside the editor.
    record["payload"]["changed_fields"] = sorted(
        [*record["payload"]["changed_fields"], "producing_version"]
    )
    lines[index] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    journal.write_bytes(b"".join(lines))
    declared = record["payload"]["identity_digest"]

    # The running package is now a further change, and needs its own decision.
    with pytest.raises(IdentityChanged) as second:
        await rig.resume(editor=other)
    assert [d.field for d in second.value.differences] == ["producing_version"]
    upgrade = Authorization.new(
        "run_identity_changed",
        second.value.instance_digest,
        "upgraded the package",
        now="2026-09-17T11:00:00Z",
    )

    seen: list[list[str]] = []

    def after_identity_rebased() -> None:
        seen.append([r["event"] for r in _ledger_records(rig)])

    result = await rig.resume(
        editor=other,
        authorizations=[upgrade],
        _hooks={"after_identity_rebased": after_identity_rebased},
    )

    assert result.terminal == "finished"
    # The owed record was already durable when the second declaration was written.
    assert len(seen) == 1
    assert seen[0].count("store_rebased") == 1
    assert seen[0][-1] == "store_rebased"
    rebases = [r["payload"] for r in rig.records() if r["event"] == "identity_rebased"]
    assert len(rebases) == 2
    completions = [r["payload"] for r in _ledger_records(rig) if r["event"] == "store_rebased"]
    assert len(completions) == 2
    assert [c["campaign_rebase"] for c in completions] == [d["identity_digest"] for d in rebases]
    assert [c["producing_version"] for c in completions] == [
        "0.3.0a11",
        rebases[1]["producing_version"],
    ]
    assert completions[0]["campaign_rebase"] == declared
    assert completions[0]["authorization_id"] == editor_change.id
    assert completions[1]["authorization_id"] == upgrade.id


async def test_two_version_only_rebases_each_keep_their_own_ledger_completion(
    tmp_path: Path,
) -> None:
    """§6.2: the two halves pair by (campaign rebase, decision), never by digest.

    A rebase that moves nothing but the producing version leaves the identity
    digest where it was, so two consecutive version-only rebases declare the
    same digest twice under two different decisions. Matching a completion to a
    declaration by digest alone reads the first's `store_rebased` as the
    second's and leaves the second's debt unpaid for the life of the run
    (ADR-0015, codex round 2).
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born store, one record behind the campaign

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
    for path in [
        rig.run_root / "campaign.jsonl",
        rig.run_root / "artifacts.jsonl",
        *sorted((rig.run_root / "measurements").glob("*/events.jsonl")),
    ]:
        _rewrite_first_record(path, producing_version="0.3.0a0")

    def crash_after_identity_rebased() -> None:
        raise RuntimeError("crash")

    # The first version-only rebase: declared, and crashed before the ledger half.
    with pytest.raises(IdentityChanged) as first:
        await rig.resume()
    assert [d.field for d in first.value.differences] == ["producing_version"]
    one = Authorization.new(
        "run_identity_changed", first.value.instance_digest, "upgrade", now="2026-09-17T10:00:00Z"
    )
    with pytest.raises(RuntimeError):
        await rig.resume(
            authorizations=[one],
            _hooks={"after_identity_rebased": crash_after_identity_rebased},
        )
    assert "store_rebased" not in [r["event"] for r in _ledger_records(rig)]

    # The package moves again, so the second declaration repeats the identity
    # digest of the first under a decision of its own.
    journal = rig.run_root / "campaign.jsonl"
    lines = journal.read_bytes().splitlines(keepends=True)
    index = next(i for i, raw in enumerate(lines) if json.loads(raw)["event"] == "identity_rebased")
    record = json.loads(lines[index])
    record["payload"]["producing_version"] = "0.3.0a11"
    lines[index] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    journal.write_bytes(b"".join(lines))

    with pytest.raises(IdentityChanged) as second:
        await rig.resume()
    assert [d.field for d in second.value.differences] == ["producing_version"]
    two = Authorization.new(
        "run_identity_changed",
        second.value.instance_digest,
        "upgraded again",
        now="2026-09-17T11:00:00Z",
    )
    with pytest.raises(RuntimeError):
        await rig.resume(
            authorizations=[two],
            _hooks={"after_identity_rebased": crash_after_identity_rebased},
        )
    declarations = [r["payload"] for r in rig.records() if r["event"] == "identity_rebased"]
    assert len(declarations) == 2
    # Both declare the same identity: only the version moved.
    assert declarations[0]["identity_digest"] == declarations[1]["identity_digest"]
    # The first invocation's debt was paid before the second declaration.
    completions = [r["payload"] for r in _ledger_records(rig) if r["event"] == "store_rebased"]
    assert [c["authorization_id"] for c in completions] == [one.id]

    result = await rig.resume()  # the journal is standing authority for both

    assert result.terminal == "finished"
    completions = [r["payload"] for r in _ledger_records(rig) if r["event"] == "store_rebased"]
    assert [c["authorization_id"] for c in completions] == [one.id, two.id]
    assert [c["campaign_rebase"] for c in completions] == [
        d["identity_digest"] for d in declarations
    ]


async def test_a_rebase_declared_before_the_store_was_born_is_stamped_at_creation(
    tmp_path: Path,
) -> None:
    """§6.2: the debt is reconstructed whatever the store's birth state.

    A campaign declares `identity_rebased` before it adopts its siblings, so a
    crash in between leaves a run whose store does not exist yet and whose
    journal already carries a rebase. The owed completion is then the ledger's
    second record, straight after `store_created` and before `v0` (codex
    round 2).
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_campaign_created() -> None:
        raise RuntimeError("crash")  # an unborn store: only the first record stands

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_campaign_created": after_campaign_created})
    assert rig.events() == ["campaign_created"]
    assert not (rig.run_root / "artifacts.jsonl").exists()

    other = ScriptedEditor(edits("r01-a01", "other"))
    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=other)
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "new editor",
        now="2026-09-17T10:00:00Z",
    )

    def after_identity_rebased() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.resume(
            editor=other,
            authorizations=[auth],
            _hooks={"after_identity_rebased": after_identity_rebased},
        )
    assert rig.events().count("identity_rebased") == 1
    assert not (rig.run_root / "artifacts.jsonl").exists()

    result = await rig.resume(editor=other)

    assert result.terminal == "finished"
    ledger = _ledger_records(rig)
    assert [r["event"] for r in ledger[:2]] == ["store_created", "store_rebased"]
    rebase = [r for r in rig.records() if r["event"] == "identity_rebased"][0]["payload"]
    assert ledger[1]["payload"]["campaign_rebase"] == rebase["identity_digest"]
    assert ledger[1]["payload"]["authorization_id"] == auth.id
    assert ledger[1]["payload"]["producing_version"] == rebase["producing_version"]


async def test_a_new_run_journals_the_hygiene_findings_the_operator_accepted(
    tmp_path: Path,
) -> None:
    """§6.1: the accepted run-directory facts are durable before the run's first step."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    digest = hygiene_instance_digest("run", "not owner-only", "0o755")
    finding = HygieneFinding("run", "not owner-only", "0o755", digest)
    auth = Authorization.new(
        "run_dir_unsafe_entry", digest, "my own host", now="2026-09-17T10:00:00Z"
    )

    result = await rig.run(hygiene_applications=[(finding, auth)])

    assert result.terminal == "finished"
    assert rig.events()[:2] == ["campaign_created", "authorization_applied"]
    applied = [r for r in rig.records() if r["event"] == "authorization_applied"][0]["payload"]
    assert applied == dict(
        code="run_dir_unsafe_entry",
        path="run",
        instance_digest=digest,
        authorization=auth.journal_payload(),
    )


def _docs_tree(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(body, encoding="utf-8")
    return root


async def test_an_authorized_documentation_change_re_takes_the_run_snapshot(
    tmp_path: Path,
) -> None:
    """§6.2: the run directory ends up holding the snapshot the record names."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    source = _docs_tree(tmp_path / "docs", "the first documentation\n")
    before = _v2_create_document_snapshot(source)

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(
            environment_docs=before,
            _hooks={"after_campaign_created": after_campaign_created},
        )
    stored = rig.run_root / ENVIRONMENT_DOCS_ROOT
    assert _v2_create_document_snapshot(stored).digest == before.digest

    _docs_tree(source, "the second documentation\n")
    after = _v2_create_document_snapshot(source)
    assert after.digest != before.digest

    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(environment_docs=after)
    assert [d.field for d in excinfo.value.differences] == [
        "identity.reflector.environment_docs_digest"
    ]
    # The refusal wrote nothing, the run directory included.
    assert _v2_create_document_snapshot(stored).digest == before.digest
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "new documentation",
        now="2026-09-17T10:00:00Z",
    )

    result = await rig.resume(environment_docs=after, authorizations=[auth])

    assert result.terminal == "finished"
    assert _v2_create_document_snapshot(stored).digest == after.digest
    rebase = [r for r in rig.records() if r["event"] == "identity_rebased"][0]["payload"]
    assert rebase["identity"]["reflector"]["environment_docs_digest"] == after.digest
    # A further resume against the same documentation needs no decision.
    again = await rig.resume(environment_docs=after)
    assert again.terminal == "finished"
    assert rig.events().count("identity_rebased") == 1


async def test_a_hygiene_decision_the_journal_already_carries_is_not_journaled_again(
    tmp_path: Path,
) -> None:
    """§5 source 1: the journal is standing authority for its own instance digests."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    digest = hygiene_instance_digest("run", "not owner-only", "0o755")
    finding = HygieneFinding("run", "not owner-only", "0o755", digest)
    auth = Authorization.new(
        "run_dir_unsafe_entry", digest, "my own host", now="2026-09-17T10:00:00Z"
    )

    def after_campaign_created() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(
            hygiene_applications=[(finding, auth), (finding, auth)],  # and within one call
            _hooks={"after_campaign_created": after_campaign_created},
        )
    assert rig.events().count("authorization_applied") == 1

    result = await rig.resume(hygiene_applications=[(finding, auth)])

    assert result.terminal == "finished"
    assert rig.events().count("authorization_applied") == 1


async def test_a_sibling_stamped_with_a_foreign_version_refuses_before_the_rebase(
    tmp_path: Path,
) -> None:
    """ADR-0015 §5: the sibling's version is admitted read-only, before the first write.

    `Journal.resume` would reach it only after `adopt` had appended this
    invocation's `identity_rebased` and the ledger half that completes it, so a
    measurement stamped with a version the campaign never admitted would leave
    two consequence records behind and then raise (codex round 3).
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_baseline_bound() -> None:
        raise RuntimeError("crash")  # a born, incomplete champion measurement

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_baseline_bound": after_baseline_bound})
    journals = sorted((rig.run_root / "measurements").glob("*/events.jsonl"))
    assert journals
    _rewrite_first_record(journals[0], producing_version="9.9.9")

    other = ScriptedEditor(edits("other"))
    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=other)
    auth = Authorization.new(
        "run_identity_changed",
        excinfo.value.instance_digest,
        "new editor",
        now="2026-09-17T10:00:00Z",
    )
    before = (rig.run_root / "campaign.jsonl").read_bytes()

    with pytest.raises(IdentityChanged) as refusal:
        await rig.resume(editor=other, authorizations=[auth])

    assert refusal.value.hard
    assert refusal.value.instance_digest is None
    assert [d.field for d in refusal.value.differences] == ["producing_version"]
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before
    assert "identity_rebased" not in rig.events()
    assert "store_rebased" not in [r["event"] for r in _ledger_records(rig)]


async def test_a_finished_campaign_cannot_be_rebased(tmp_path: Path) -> None:
    """There is nothing to continue, so the difference is hard and carries no digest."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    assert (await rig.run()).terminal == "finished"
    assert rig.records()[-1]["event"] == "campaign_finished"

    with pytest.raises(IdentityChanged) as excinfo:
        await rig.resume(editor=ScriptedEditor(edits("other")))

    assert excinfo.value.hard
    assert excinfo.value.instance_digest is None
    assert [d.field for d in excinfo.value.differences] == ["identity.editor.digest"]
    assert "identity_rebased" not in rig.events()


async def test_a_finished_campaign_refuses_a_hygiene_application_before_any_write(
    tmp_path: Path,
) -> None:
    """PDR-0010 (amended 2026-09-17): a finished campaign takes no authorization.

    `evolve` refuses this during admission, but a library caller that assembled
    the applications itself does not pass through that check, and C1 must not
    discover it as a lifecycle violation from inside `_write_consequences` --
    that is after the invocation's first append, which is no longer a refusal
    (codex round 4).
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    result = await rig.run()
    assert result.terminal == "finished"
    before = (rig.run_root / "campaign.jsonl").read_bytes()
    finding = HygieneFinding(
        "scorer-traces",
        "not owner-only",
        "0755",
        hygiene_instance_digest("scorer-traces", "not owner-only", "0755"),
    )
    auth = Authorization.new(
        "run_dir_unsafe_entry", finding.instance_digest, "shared box", now="2026-09-17T10:00:00Z"
    )

    with pytest.raises(HygieneNotAuthorizable) as refused:
        await rig.resume(hygiene_applications=[(finding, auth)])

    assert str(refused.value) == "run_dir_unsafe_entry"
    assert (refused.value.code, refused.value.name) == ("run_dir_unsafe_entry", "scorer-traces")
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before


# -- family 6: sample tries and pair exclusion (continuable terminals) ----

#: Thresholds no fixture below reaches, so a test observes exclusion, not a crossing.
LENIENT = {
    owner: {"max_count": 100, "max_rate": "1"}
    for owner in ("target", "environment", "scorer", "rollout")
}
#: Two looks, so a pair excluded at look 3 leaves repeats 4-6 to feed the focus.
TWO_LOOKS = {"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}}


def seed_key(rig: Rig, case: str = "case-01", sample: str = "02") -> tuple[str, str, str]:
    return (scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest, case, sample)


def edited_key(
    tmp_path: Path, case: str = "case-01", sample: str = "01", value: str = "VALUE = 2\n"
) -> tuple[str, str, str]:
    """The key of the first attempt's edited Candidate (`edits` writes `VALUE = 2`)."""

    root = seed_tree(tmp_path / f"edited-{hashlib.sha256(value.encode()).hexdigest()[:8]}", value)
    return (scan_tree(root, ARTIFACT_POLICY).candidate_digest, case, sample)


def _denominator(rig: Rig) -> int:
    """The run's threshold denominator, read back off disk."""

    from evolution.tree.campaign_projections import inspect_view

    return inspect_view(rig.run_root).snapshot.denominator


def _cancel_on_sample_terminal(rig: Rig, needle: bytes = b'"target_failed"') -> Callable[[], bool]:
    """A caller token that flips once a measurement journal holds `needle`."""

    def cancelled() -> bool:
        root = rig.run_root / "measurements"
        if not root.is_dir():
            return False
        return any(needle in path.read_bytes() for path in root.rglob("events.jsonl"))

    return cancelled


def _cancelled_retry_rig(
    tmp_path: Path, indeterminate: dict[str, Any], name: str = "run", *, schema: str = SCHEMA_V15
) -> Rig:
    """One case, one sample failing on try 0, the stage dispatched in schedule order.

    `max_concurrency=1` keeps the cancellation deterministic: sample `01`'s
    terminal flips the token, so its retry never starts and the two repeats
    behind it end `cancelled` (an `exclude` disposition, never accounted).
    """

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=2,
            max_concurrency=1,
            indeterminate=indeterminate,
        ),
        cases=corpus(1),
        run_name=name,
        schema=schema,
    )
    rig.adapter.dispositions[seed_key(rig, "case-01", "01")] = ["target_failed"]
    return rig


async def test_a_cancelled_run_reserves_no_slot_for_the_retry_it_never_starts(
    tmp_path: Path,
) -> None:
    """ADR-0008/ADR-0016 as amended: cancellation is the one exception to the retry rule."""

    rig = _cancelled_retry_rig(tmp_path, LENIENT)

    result = await rig.run(cancelled=_cancel_on_sample_terminal(rig))

    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == ("stopped", "cancelled")
    observed = payloads_of(rig, "indeterminate_observed")
    assert [(o["retry_ordinal"], o["retry_follows"]) for o in observed] == [(0, False)]
    # The stage's three scheduled samples, and no fourth slot for a retry the
    # cancelled run did not and will not dispatch.
    assert _denominator(rig) == 3
    assert b'"retry_ordinal":1' not in (rig.run_root / "campaign.jsonl").read_bytes()


async def test_an_exhausted_attempt_sample_closes_the_attempt_without_alpha(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=1, attempts_per_round=1, sample_retries=1),
    )
    rig.adapter.dispositions[edited_key(tmp_path)] = "time_budget"

    await rig.run()

    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["outcome"], closed["reason"]) == ("insufficient_evidence", "incomplete_sample")
    assert "pairs_excluded" not in rig.events() and "alpha_consumed" not in rig.events()
    assert "look_not_started" in rig.events()
    # Both tries are accounted, each once (spec §3.5).
    observed = payloads_of(rig, "indeterminate_observed")
    assert [(o["measurement_id"], o["sample_id"], o["retry_ordinal"]) for o in observed] == [
        ("m-r01-a01", "01", 0),
        ("m-r01-a01", "01", 1),
    ]


class _PassingCaseScorer(PromotingScorer):
    """`case-02` passes its verdicts, so only `case-01` can be in a failing cohort."""

    async def score(self, case: Any, view: Any, candidate: Any) -> Any:
        result = await super().score(case, view, candidate)
        if case.case_id != "case-02":
            return result
        return replace(result, verdicts=(dict(PASSING_VERDICT),))


def _fully_excluded_baseline_rig(tmp_path: Path, name: str = "run") -> Rig:
    """Every baseline repeat of `case-01` is exhausted; `case-02` measures normally.

    Without the focus filter `case-01` carries the round's whole mass -- six
    unavailable repeats, all of them failing -- so the focus would land on the
    one case no look can compare (spec §4.3, final branch review).
    """

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=0,
            alpha=TWO_LOOKS,
            indeterminate=LENIENT,
        ),
        cases=corpus(2),
        adapter=FakeAdapter(used={"case-01": [], "case-02": ["skill.py"]}),
        run_name=name,
    )
    for repeat in range(1, 7):
        rig.adapter.dispositions[seed_key(rig, "case-01", f"{repeat:02d}")] = "target_failed"
    return rig


async def test_focus_selection_ignores_the_excluded_baseline_pairs(tmp_path: Path) -> None:
    rig = _fully_excluded_baseline_rig(tmp_path)

    await rig.run()

    focus = payloads_of(rig, "focus_portion_selected")[0]
    assert (focus["reason"], focus["case_ids"]) == ("selected", ["case-02"])
    # The Reflector's evidence still counts the excluded samples: the registry
    # keeps all twelve records, six of them unavailable and every one failing.
    assert (focus["available"], focus["unavailable"], focus["failing"]) == (6, 6, 12)
    assert payloads_of(rig, "pairs_excluded")[0]["count"] == 6
    assert payloads_of(rig, "gate_scope_selected")[0]["cases"] == ["case-02"]
    closed = payloads_of(rig, "attempt_closed")[0]
    assert closed["reason"] != "no_evaluable_focus"
    # ADR-0032: family 14 records its Candidate confidence comparison.
    assert "comparison_look_evaluated" in rig.events()


async def test_a_resume_across_the_focus_portion_reproduces_the_same_selection(
    tmp_path: Path,
) -> None:
    rig = _fully_excluded_baseline_rig(tmp_path)

    def crash_after_reflection_started() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash, match="power cut"):
        await rig.run(_hooks={"after_reflection_started": crash_after_reflection_started})
    journaled = payloads_of(rig, "focus_portion_selected")
    assert journaled and journaled[0]["case_ids"] == ["case-02"]

    await rig.resume()

    # `run_reflection` recomputes the portion and refuses a selection the
    # journal does not carry, so an unreproducible rule would not get this far.
    assert payloads_of(rig, "focus_portion_selected") == journaled
    assert payloads_of(rig, "gate_scope_selected")[0]["cases"] == ["case-02"]
    # ADR-0032: family 14 records its Candidate confidence comparison.
    assert "comparison_look_evaluated" in rig.events()


def _only_unmeasurable_failures_rig(tmp_path: Path, name: str = "unmeasurable") -> Rig:
    """`case-02` passes; every baseline repeat of `case-01`, the only failing case, is exhausted.

    The excluded pairs empty the one failing cohort, so the round has nothing an
    Attempt could be judged on (spec §4.3, codex branch review round 1).
    """

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(
            rounds=1,
            attempts_per_round=1,
            sample_retries=0,
            alpha=TWO_LOOKS,
            indeterminate=LENIENT,
        ),
        cases=corpus(2),
        run_name=name,
    )
    rig.scorer = _PassingCaseScorer(scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest)
    for repeat in range(1, 7):
        rig.adapter.dispositions[seed_key(rig, "case-01", f"{repeat:02d}")] = "target_failed"
    return rig


async def test_a_round_whose_failing_cohorts_are_all_unmeasurable_opens_no_attempt(
    tmp_path: Path,
) -> None:
    rig = _only_unmeasurable_failures_rig(tmp_path)

    result = await rig.run()

    portion = payloads_of(rig, "focus_portion_selected")[0]
    assert (portion["reason"], portion["cohort_ids"], portion["case_ids"]) == (
        "unmeasurable",
        [],
        [],
    )
    # The Reflector's own evidence is untouched: the six excluded repeats are
    # still counted as unavailable and failing.
    assert (portion["available"], portion["unavailable"], portion["failing"]) == (6, 6, 6)
    assert portion["grouping_digest"] is not None
    closed = payloads_of(rig, "round_closed")[0]
    assert closed["outcome"] == "unmeasurable"
    events = rig.events()
    assert "attempt_opened" not in events and "reflection_started" not in events
    assert "alpha_consumed" not in events
    assert result.terminal == "finished"
    from evolution.tree.campaign_projections import verify_evidence

    assert verify_evidence(rig.run_root, corpus=rig.cases)["mismatches"] == []


async def test_a_round_with_one_measurable_failing_cohort_still_focuses(tmp_path: Path) -> None:
    """The unchanged half: one measurable failing case keeps the ordinary round."""

    rig = _fully_excluded_baseline_rig(tmp_path, name="measurable")

    await rig.run()

    portion = payloads_of(rig, "focus_portion_selected")[0]
    assert (portion["reason"], portion["case_ids"]) == ("selected", ["case-02"])
    assert payloads_of(rig, "round_closed")[0]["outcome"] != "unmeasurable"
    assert "attempt_opened" in rig.events()


def _decisions(rig: Rig) -> list[tuple[Any, ...]]:
    """The records a parallelism change must not move, with their deciding fields."""

    rows: list[tuple[Any, ...]] = []
    for record in rig.records():
        p = record["payload"]
        event = record["event"]
        if event == "indeterminate_observed":
            rows.append(
                (
                    event,
                    p["measurement_id"],
                    p["case_id"],
                    p["sample_id"],
                    p["retry_ordinal"],
                    p["owner"],
                    p["count"],
                )
            )
        elif event == "pairs_excluded":
            rows.append(
                (
                    event,
                    p["measurement_id"],
                    p["stage"],
                    p["count"],
                    p["pairs_digest"],
                    json.dumps(p["pairs"], sort_keys=True),
                )
            )
        elif event == "look_evaluated":
            rows.append((event, p["attempt_id"], p["look"], p["decision"], p["lower"], p["upper"]))
        elif event == "attempt_closed":
            rows.append((event, p["attempt_id"], p["outcome"], p["reason"], p["look"]))
        elif event == "campaign_finished":
            rows.append((event, p["outcome"], p["reason"], json.dumps(p["continuation"])))
    return rows


async def test_a_resumed_stage_retries_a_sample_whose_latest_try_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8b: a crash between a failed try and its retry resumes into that retry.

    The stage is cut after try `0` of one baseline sample failed and before try
    `1` started, so every sample has an execution and the stage looks dispatched.
    The planner still owes the try and the resumed stage runs it.
    """

    from evolution.tree import campaign_measurement

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=1, attempts_per_round=1, sample_retries=1),
    )
    rig.adapter.dispositions[seed_key(rig)] = ["target_failed"]
    original = Measurement.run_stage

    async def no_retry_then_crash(self: Any, plan: Any, **kwargs: Any) -> Any:
        with monkeypatch.context() as patch:
            patch.setattr(campaign_measurement, "try_is_retryable", lambda *_: False)
            await original(self, plan, **kwargs)
        raise Crash("power cut")

    monkeypatch.setattr(Measurement, "run_stage", no_retry_then_crash)
    with pytest.raises(Crash):
        await rig.run()
    monkeypatch.undo()
    # ADR-0032: family 14 records the confidence Candidate comparison.
    assert "baseline_complete" not in rig.events()

    result = await rig.resume()

    assert result.terminal == "finished"
    assert "pairs_excluded" not in rig.events()
    observed = payloads_of(rig, "indeterminate_observed")
    assert [(o["sample_id"], o["retry_ordinal"]) for o in observed] == [("02", 0)]
    tries = [
        json.loads(line)["payload"].get("retry_ordinal")
        for line in (rig.run_root / "measurements" / measurement_id_for(None) / "events.jsonl")
        .read_bytes()
        .splitlines()
        if json.loads(line)["event"] == "rollout_started"
        and json.loads(line)["payload"]["sample_id"] == "02"
        and json.loads(line)["payload"]["case_id"] == "case-01"
    ]
    assert tries == [0, 1]
    assert "comparison_look_evaluated" in rig.events()


async def test_an_attempt_sample_that_recovers_on_retry_keeps_the_look(tmp_path: Path) -> None:
    """R8a: decisions read the latest try; the failed try is only accounted."""

    rig = Rig(
        tmp_path,
        campaign_policy=v5_policy(rounds=1, attempts_per_round=1, sample_retries=1),
    )
    rig.adapter.dispositions[edited_key(tmp_path)] = ["time_budget"]

    await rig.run()

    # ADR-0032: family 14 records the confidence Candidate comparison.
    assert "look_not_started" not in rig.events()
    assert payloads_of(rig, "comparison_look_evaluated")
    closed = payloads_of(rig, "attempt_closed")[0]
    assert closed["reason"] != "incomplete_sample"
    observed = payloads_of(rig, "indeterminate_observed")
    assert [(o["measurement_id"], o["retry_ordinal"]) for o in observed] == [("m-r01-a01", 0)]


async def test_finish_owed_terminal_leaves_a_root_that_owes_nothing_alone(tmp_path: Path) -> None:
    """Comparable-links §5.3: `None` unless the prefix ends on an applied close/terminal."""

    assert finish_owed_terminal(tmp_path / "absent") is None
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    result = await rig.run()
    assert result.finished is not None
    before = (rig.run_root / "campaign.jsonl").read_bytes()

    assert finish_owed_terminal(rig.run_root) is None
    assert (rig.run_root / "campaign.jsonl").read_bytes() == before


# -- the backend's instance description (W19, PDR-0014) -------------------


#: A value no role may ever see and no comparison may ever read. The canary is
#: a *value*, not a key name: the brief and the Reflector's evidence both carry
#: unrelated `description` keys of their own (a group's label, say), so only a
#: value this improbable proves the instance description did not leak.
DESCRIBED = {"config_hash": "abc", "note": "DESCRIPTION-CANARY"}


async def describing_run(
    tmp_path: Path, description: dict[str, Any] | None, *, schema: str = SCHEMA_V15
) -> Rig:
    """One finished campaign whose target described itself at materialization."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=FakeAdapter(description=description),
        schema=schema,
    )
    result = await rig.run()
    assert result.terminal == "finished"
    return rig


async def test_instance_attested_carries_the_description_once(tmp_path: Path) -> None:
    """One instance serving several samples produces exactly one record (§5)."""

    rig = await describing_run(tmp_path, DESCRIBED)
    records = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]

    assert records
    # One record per instance, not one per sample: `instance_attested` is
    # uniqueness-enforced on the instance id, and an instance serves many
    # samples. The adapter's own recorder proves the many: every session one
    # measurement opened, in dispatch order.
    assert len(records) == len({row["instance_id"] for row in records})
    measurements = {row["measurement_id"] for row in records}
    assert any(len(rig.adapter.session_case_order(measurement=m)) > 1 for m in measurements)
    for measurement_id in measurements:
        rows = [row for row in records if row["measurement_id"] == measurement_id]
        assert len(rows) == 1
    assert all(row["description"] == DESCRIBED for row in records)


async def test_instance_attested_omits_an_absent_description(tmp_path: Path) -> None:
    """A backend that publishes no `description` journals no key at all."""

    rig = await describing_run(tmp_path, None)
    records = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]

    assert records
    assert all("description" not in row for row in records)


async def test_the_description_is_not_part_of_identity(tmp_path: Path) -> None:
    """ADR-0007: `TargetIdentity` is compared whole after every session, so the
    description must not enter it or the journal payload it round-trips."""

    rig = await describing_run(tmp_path, DESCRIBED)
    records = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]

    assert records
    for row in records:
        assert "description" not in row["identity"]
        identity = TargetIdentity.from_payload(row["identity"])
        assert identity.to_payload() == row["identity"]


async def test_no_role_sees_the_instance_description(tmp_path: Path) -> None:
    """PDR-0014: the operator and the audit see it; the editor and the
    Reflector never do. An editor that could read its own configuration hash
    would be invited to optimize against it (PDR-0008 for the Reflector)."""

    rig = await describing_run(tmp_path, DESCRIBED)

    assert rig.editor.requests and rig.reflector.requests
    for request in rig.editor.requests:
        assert "DESCRIPTION-CANARY" not in json.dumps(brief_payload(request.brief))
    for request in rig.reflector.requests:
        assert "DESCRIPTION-CANARY" not in json.dumps(reflect_brief_payload(request.evidence))


async def test_a_journal_written_before_describe_still_resumes(tmp_path: Path) -> None:
    """The hazard the *optional* key exists for.

    Every `instance_attested` row already on disk carries no `description`, and
    family 6's key sets are closed: a required key would have refused every
    campaign written before this delivery the moment it was resumed. The run
    below writes its first rows through a target that describes nothing --
    byte-for-byte the pre-delivery shape -- and is then continued by one that
    does, so the journal the resume reads holds both shapes at once.
    """

    rig = Rig(tmp_path, campaign_policy=policy(rounds=2, attempts_per_round=1))

    class Boom(RuntimeError):
        pass

    def after_selection_decided() -> None:
        raise Boom("crash")

    with pytest.raises(Boom):
        await rig.run(_hooks={"after_selection_decided": after_selection_decided})
    before = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]
    assert before and all("description" not in row for row in before)

    rig.adapter = FakeAdapter(description=DESCRIBED)
    result = await rig.resume()

    assert result.terminal == "finished"
    after = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]
    # The prefix is untouched and whatever the resume added carries the new key.
    assert after[: len(before)] == before
    added = after[len(before) :]
    assert added and all(row["description"] == DESCRIBED for row in added)


# -- #287: a later instance description that holds a run secret ------------

#: A forwarded Scorer value -- a run secret by the length rule -- that the
#: Candidate's `describe()` reports, and the manifest path `evolve` names it by.
DESCRIBED_SECRET = "judge-model-secret-x"
DESCRIBED_SECRET_PATH = "scorer.allowed_env.JUDGE"
#: What the Champion's instances describe, and what a repaired `describe()`
#: reports for the Candidate's.
PUBLIC_DESCRIPTION = {"model": "public"}
REPAIRED_DESCRIPTION = {"model": "repaired"}
DESCRIPTION_SECRET_DETAIL = (
    "instance_attested.description.model holds the value of scorer.allowed_env.JUDGE; "
    "the measurement journal is intact; remove the value from the target's describe() "
    "and run the same command again"
)


class CandidateDescribing(FakeAdapter):
    """The Champion's instances describe `PUBLIC_DESCRIPTION`, each Candidate's
    `candidate` -- or, when `first` is given, the first Candidate instance
    `first`. One session per instance, so each measurement of the default
    two-way concurrency materializes two instances in one stage."""

    def __init__(self, candidate: dict[str, Any], *, first: dict[str, Any] | None = None) -> None:
        super().__init__(max_concurrent_sessions=1, description=PUBLIC_DESCRIPTION)
        self.candidate = candidate
        self.first = first
        self.candidate_count = 0

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> Any:
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind != "champion":
            self.candidate_count += 1
            first = self.candidate_count == 1 and self.first is not None
            instance.description = self.first if first else self.candidate
        return instance


def candidate_instances(adapter: FakeAdapter) -> set[str]:
    return {
        instance_id
        for instance_id, instance in adapter.created.items()
        if instance.description is not PUBLIC_DESCRIPTION
    }


def assert_nowhere(root: Path, value: str) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            assert value.encode() not in path.read_bytes(), path


async def description_secret_run(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run `rig` under the judge secret; the refusal's `_Fault` is spied on."""

    raised: list[Any] = []
    original = campaign_module._Campaign.refuse_description_secret

    def spy(self: Any, measurement_id: str, descriptions: Any) -> None:
        try:
            original(self, measurement_id, descriptions)
        except campaign_module._Fault as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(campaign_module._Campaign, "refuse_description_secret", spy)
    result = await rig.run(secrets=(DESCRIBED_SECRET,), secret_paths=(DESCRIBED_SECRET_PATH,))
    monkeypatch.setattr(campaign_module._Campaign, "refuse_description_secret", original)
    assert result is not None
    assert (result.terminal, result.owner, result.code) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
    )
    assert result.fault_detail == DESCRIPTION_SECRET_DETAIL
    (fault,) = raised
    assert fault.measurement == "m-r01-a01"
    # Appends nothing: neither of the Candidate's two new instances -- the
    # clean first one, the refused second -- is attested, and no terminal
    # record is written; the run stays unfinished.
    candidates = candidate_instances(rig.adapter)
    assert len(candidates) == 2
    attested = {
        row["payload"]["instance_id"]
        for row in rig.records()
        if row["event"] == "instance_attested"
    }
    assert attested and not attested & candidates
    # Refused before `stage_opened`: no row of the Candidate's was paid for.
    assert not [
        row
        for row in rig.records()
        if row["event"] == "stage_opened" and row["payload"]["measurement_id"] == "m-r01-a01"
    ]
    assert not set(rig.adapter.live)
    assert "campaign_finished" not in rig.events()
    assert "campaign_suspended" not in rig.events()
    return result


def test_a_candidate_description_secret_appends_nothing_and_fails_resumably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_tree_cli_family14 import _invoke, _stderr

    rig = Rig(
        tmp_path / "campaign",
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=CandidateDescribing({"model": DESCRIBED_SECRET}, first={"model": "clean"}),
    )
    # Synchronous: the CLI runner below starts its own event loop.
    result = asyncio.run(description_secret_run(rig, monkeypatch))

    invocation = _invoke(tmp_path / "cli", monkeypatch, result)
    assert invocation.exit_code == 1, invocation.output
    document = json.loads(invocation.stdout)
    assert (document["terminal"], document["owner"], document["code"]) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
    )
    assert f"evo tree evolve: {DESCRIPTION_SECRET_DETAIL}" in _stderr(invocation)
    assert DESCRIBED_SECRET not in invocation.stdout
    assert DESCRIBED_SECRET not in invocation.stderr
    assert_nowhere(tmp_path, DESCRIBED_SECRET)


async def test_resume_after_repairing_describe_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 3: the refused stage completes on resume through
    `account_resumed`, and the attestation that was never written is never
    re-proven -- the repaired instances are attested instead."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=CandidateDescribing({"model": DESCRIBED_SECRET}, first={"model": "clean"}),
    )
    await description_secret_run(rig, monkeypatch)
    refused = candidate_instances(rig.adapter)
    before = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]

    resumed: list[str] = []
    original = campaign_module._Campaign.account_resumed

    async def spy(self: Any) -> None:
        resumed.extend(self.measurements)
        await original(self)

    monkeypatch.setattr(campaign_module._Campaign, "account_resumed", spy)
    rig.adapter = CandidateDescribing(REPAIRED_DESCRIPTION)
    result = await rig.resume(secrets=(DESCRIBED_SECRET,), secret_paths=(DESCRIBED_SECRET_PATH,))

    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert result.finished is not None and result.suspended is None
    # Not the permanent promotion-admission refusal of an unattested measurement.
    assert "campaign_suspended" not in rig.events()
    assert "m-r01-a01" in resumed
    after = [r["payload"] for r in rig.records() if r["event"] == "instance_attested"]
    assert after[: len(before)] == before
    added = after[len(before) :]
    assert added
    assert {row["instance_id"] for row in added} <= set(rig.adapter.created)
    assert not {row["instance_id"] for row in after} & refused
    assert all(row["description"] in (PUBLIC_DESCRIPTION, REPAIRED_DESCRIPTION) for row in after)
    assert_nowhere(tmp_path, DESCRIBED_SECRET)


class DescribingAfterOpen(CandidateDescribing):
    """The Candidate's second instance describes `PUBLIC_DESCRIPTION` when it
    is created and the judge secret once it opens a session: the same body
    object, read again by `record_attestations` after the stage ran. It
    reaches the check the stage-opening one cannot see, as a description
    re-materialized inside a resumed open stage does."""

    def __init__(self) -> None:
        super().__init__({})
        self.candidates: list[str] = []

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> Any:
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            return instance
        self.candidates.append(instance_id)
        instance.description = dict(PUBLIC_DESCRIPTION)
        if len(self.candidates) == 2:
            open_session = instance.open

            async def opened(*args: Any, **kwargs: Any) -> Any:
                instance.description["model"] = DESCRIBED_SECRET
                return await open_session(*args, **kwargs)

            instance.open = opened
        return instance


async def test_a_description_secret_after_dispatch_appends_nothing(tmp_path: Path) -> None:
    """`record_attestations` checks every new instance before its first append:
    the clean first instance of the stage is not attested either."""

    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
        adapter=DescribingAfterOpen(),
    )
    result = await rig.run(secrets=(DESCRIBED_SECRET,), secret_paths=(DESCRIBED_SECRET_PATH,))

    assert result is not None
    assert (result.terminal, result.owner, result.code, result.fault_detail) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
        DESCRIPTION_SECRET_DETAIL,
    )
    assert len(rig.adapter.candidates) == 2
    opened = [
        row["payload"]["measurement_id"] for row in rig.records() if row["event"] == "stage_opened"
    ]
    assert "m-r01-a01" in opened
    assert not [
        row
        for row in rig.records()
        if row["event"] == "instance_attested" and row["payload"]["measurement_id"] == "m-r01-a01"
    ]
    assert "campaign_finished" not in rig.events()
    assert_nowhere(tmp_path, DESCRIBED_SECRET)


@pytest.mark.parametrize(
    ("description", "secrets", "secret_paths", "named"),
    [
        (
            {"model": DESCRIBED_SECRET},
            (DESCRIBED_SECRET,),
            (DESCRIBED_SECRET_PATH,),
            "description.model holds the value of scorer.allowed_env.JUDGE",
        ),
        # A key name that matches is never spelled, nor is a key path that does.
        (
            {DESCRIBED_SECRET: "x"},
            (DESCRIBED_SECRET,),
            (DESCRIBED_SECRET_PATH,),
            "description holds the value of scorer.allowed_env.JUDGE",
        ),
        (
            {"a": {"bcdefghi": DESCRIBED_SECRET}},
            (DESCRIBED_SECRET, "a.bcdefghi"),
            (DESCRIBED_SECRET_PATH, "editor.api_key_env"),
            "description holds the value of scorer.allowed_env.JUDGE",
        ),
        # A library caller's secret without a manifest path is named by position,
        # and a manifest path that itself matches falls back to its list key.
        (
            {"model": DESCRIBED_SECRET},
            ("unused-secret", DESCRIBED_SECRET),
            (),
            "description.model holds the value of secrets[1]",
        ),
        (
            {"model": "JUDGE-VALUE-x"},
            ("JUDGE-VALUE-x", "allowed_env.JUDGE-VALUE"),
            ("scorer.allowed_env.JUDGE-VALUE-x",),
            "description.model holds the value of scorer.allowed_env",
        ),
        # Two secrets, the later one walked first: the key path is the named
        # secret's (#287 final review), never the first match walked.
        (
            {"alpha": "bbbb-editor-value", "beta": DESCRIBED_SECRET},
            (DESCRIBED_SECRET, "bbbb-editor-value"),
            (DESCRIBED_SECRET_PATH, "editor.api_key_env"),
            "description.beta holds the value of scorer.allowed_env.JUDGE",
        ),
    ],
)
def test_the_description_refusal_names_paths_never_values(
    description: dict[str, Any], secrets: tuple[str, ...], secret_paths: tuple[str, ...], named: str
) -> None:
    from types import SimpleNamespace

    campaign = SimpleNamespace(
        checks=ENFORCE_ALL,
        attested_ids=lambda: {"known"},
        secrets=secrets,
        secret_paths=secret_paths,
        secret_names=(),
    )
    refuse = campaign_module._Campaign.refuse_description_secret
    # Already attested, or describing nothing: not this invocation's to refuse.
    refuse(campaign, "m-r01-a01", {"known": description, "quiet": None})  # type: ignore[arg-type]
    with pytest.raises(campaign_module._Fault) as caught:
        refuse(campaign, "m-r01-a01", {"known": None, "new": description})  # type: ignore[arg-type]

    fault = caught.value
    assert (fault.terminal, fault.owner, fault.code, fault.measurement) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
        "m-r01-a01",
    )
    assert fault.detail == (
        f"instance_attested.{named}; the measurement journal is intact; "
        "remove the value from the target's describe() and run the same command again"
    )
    assert not any(secret in fault.detail for secret in secrets)


def _refuse(
    description: Any,
    secrets: tuple[str, ...],
    secret_paths: tuple[str, ...] = (DESCRIBED_SECRET_PATH,),
    secret_names: tuple[str | None, ...] = (),
) -> Any:
    from types import SimpleNamespace

    campaign = SimpleNamespace(
        checks=ENFORCE_ALL,
        attested_ids=lambda: set(),
        secrets=secrets,
        secret_paths=secret_paths,
        secret_names=secret_names,
    )
    with pytest.raises(campaign_module._Fault) as caught:
        campaign_module._Campaign.refuse_description_secret(  # type: ignore[arg-type]
            campaign, "m-r01-a01", {"new": description}
        )
    return caught.value


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_the_description_refusal_names_the_first_name_sorted_within_a_role(role: str) -> None:
    """ADR-0029 amendment 2026-10-21, as the probe reports it: a `chat` role's
    key `Z_AUTH` precedes its proxy `A_PROXY` in the run's secrets, and the
    proxy is named, with its own key path (#287 branch review r1)."""

    key, proxy = "sk-z-auth-value-01", "https://a-proxy.example.test:8080"
    fault = _refuse(
        {"key": key, "proxy": proxy},
        (key, proxy),
        (f"{role}.api_key_env", f"{role}.proxy_env"),
        ("Z_AUTH", "A_PROXY"),
    )
    assert fault.detail.startswith(
        f"instance_attested.description.proxy holds the value of {role}.proxy_env;"
    )


def test_the_description_refusal_detail_is_utf8_safe_and_bounded() -> None:
    """#287 branch review r1: the key path is Target-controlled; a lone
    surrogate or a very long key cannot make the detail unprintable or
    unbounded."""

    from evolution.tree.target.connection_probe import MAX_TRACE_TEXT_BYTES

    fault = _refuse({"\ud800": DESCRIBED_SECRET}, (DESCRIBED_SECRET,))
    fault.detail.encode("utf-8")
    assert DESCRIBED_SECRET not in fault.detail
    assert "holds the value of scorer.allowed_env.JUDGE" in fault.detail

    fault = _refuse({"k" * 5000: {"model": DESCRIBED_SECRET}}, (DESCRIBED_SECRET,))
    location = fault.detail.removeprefix("instance_attested.").split(" holds ")[0]
    assert location.startswith("description.kkk")
    assert len(location.encode("utf-8")) <= MAX_TRACE_TEXT_BYTES

    # A key spelling another secret across the cut falls back whole: the cut
    # alone would keep that secret's prefix.
    other = "editor-secret-value-0001"
    fault = _refuse(
        {"k" * 240 + other: {"m": DESCRIBED_SECRET}},
        (DESCRIBED_SECRET, other),
        (DESCRIBED_SECRET_PATH, "editor.api_key_env"),
    )
    assert fault.detail.startswith("instance_attested.description holds the value of scorer.")
    assert other[:4] not in fault.detail.split(" holds ")[0]


@pytest.mark.parametrize("fresh_parent", [True, False], ids=["joint-stage", "single-stage"])
async def test_a_resumed_open_stage_with_a_description_secret_fails_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_parent: bool
) -> None:
    """A resumed, already-open stage re-materializes inside the measurement and
    dispatches in the same call, so the check runs where the instance's
    creation body arrives: refused there, nothing is dispatched on it and the
    repaired resume finishes instead of failing promotion admission."""

    secrets = dict(secrets=(DESCRIBED_SECRET,), secret_paths=(DESCRIBED_SECRET_PATH,))
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1, fresh_parent=fresh_parent),
        adapter=CandidateDescribing(REPAIRED_DESCRIPTION),
    )
    original = campaign_module._Campaign._append_stage_opened

    def cut(self: Any, measurement_id: str, plan: Any) -> None:
        original(self, measurement_id, plan)
        if measurement_id == "m-r01-a01":
            raise Crash("power cut after the Candidate's stage_opened")

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module._Campaign, "_append_stage_opened", cut)
        with pytest.raises(Crash):
            await rig.run(**secrets)
    opened = [r["payload"] for r in rig.records() if r["event"] == "stage_opened"]
    assert "m-r01-a01" in [row["measurement_id"] for row in opened]
    journal = rig.run_root / "measurements" / "m-r01-a01" / "events.jsonl"

    def rollouts() -> int:
        return sum(
            json.loads(line)["event"] == "rollout_started"
            for line in journal.read_text().splitlines()
        )

    assert rollouts() == 0

    # From then on the target describes the secret: the resumed open stage
    # refuses its new instances before any row is dispatched on them.
    rig.adapter = CandidateDescribing({"model": DESCRIBED_SECRET})
    result = await rig.resume(**secrets)
    assert (result.terminal, result.owner, result.code, result.fault_detail) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
        DESCRIPTION_SECRET_DETAIL,
    )
    refused = candidate_instances(rig.adapter)
    assert refused and rollouts() == 0
    assert not set(rig.adapter.live)
    assert not [
        r
        for r in rig.records()
        if r["event"] == "instance_attested" and r["payload"]["measurement_id"] == "m-r01-a01"
    ]
    assert "campaign_finished" not in rig.events()

    rig.adapter = CandidateDescribing(REPAIRED_DESCRIPTION)
    result = await rig.resume(**secrets)
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert "campaign_suspended" not in rig.events()
    assert rollouts() > 0
    attested = {
        r["payload"]["instance_id"]
        for r in rig.records()
        if r["event"] == "instance_attested" and r["payload"]["measurement_id"] == "m-r01-a01"
    }
    assert attested and not attested & refused
    assert_nowhere(tmp_path, DESCRIBED_SECRET)
