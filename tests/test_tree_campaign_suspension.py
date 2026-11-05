"""I1 Task 8: every infrastructure cause suspends a family-7 campaign.

A `/8` policy creates family 7 (spec §6). Where family 6 writes an
infrastructure `campaign_finished`, family 7 writes the non-terminal
`campaign_suspended{reason, owner, evidence}` after the same terminal barrier
(spec §5.1-§5.2), returns `CampaignResult.terminal == "suspended"`, and a plain
resume continues the run (spec §5.4). The family-6 twins at the end pin that
nothing changed below family 7.

Task 9 adds the repair try (spec §5.4): on resume every fault row the latest
`fault_rows` suspension named gets exactly one more try, marked `repair: true`,
outside `sample_retries`; a fault that was not repaired suspends again on the
repair try's row, one try per resume.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree import campaign_editor
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.campaign import CampaignInvariantViolated, CampaignResult, IdentityChanged
from evolution.tree.campaign_identity import measurement_id_for
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.campaign_schema import fault_rows_digest
from evolution.tree.execution.authorizations import Authorization
from evolution.tree.execution.interaction import (
    InstanceProvenance,
    RolloutEnvironmentError,
    ScorerIdentity,
    TargetIdentity,
)
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.storage.append_log import AppendLog
from tests.campaign_fakes import (
    ARTIFACT_POLICY,
    EditorScript,
    FakeAdapter,
    FakeInstance,
    PromotingScorer,
    Rig,
    ScriptedEditor,
    ScriptedEnvironment,
    corpus,
    v8_policy,
)
from tests.test_tree_campaign import edited_key, seed_key
from tests.test_tree_campaign_fixtures import (
    Trip,
    TrippingAdapter,
    TrippingEnvironment,
    TrippingScorer,
)
from tests.tree.target.test_probe_session import config as probe_config

pytestmark = pytest.mark.slow

STRICT = {
    owner: {"max_count": 0, "max_rate": "1"}
    for owner in ("target", "environment", "scorer", "rollout")
}
LENIENT = {
    owner: {"max_count": 99, "max_rate": "1"}
    for owner in ("target", "environment", "scorer", "rollout")
}
REASON = {
    "target": "target_unstable",
    "environment": "environment_unstable",
    "scorer": "scorer_unstable",
    "rollout": "rollout_time_budget_exhausted",
}


def payloads_of(rig: Rig, event: str) -> list[dict[str, Any]]:
    return [record["payload"] for record in rig.records() if record["event"] == event]


def seq_of(rig: Rig, event: str) -> list[int]:
    return [record["sequence"] for record in rig.records() if record["event"] == event]


def assert_suspended(
    rig: Rig, result: CampaignResult, reason: str, owner: str, kind: str
) -> dict[str, Any]:
    """(a) the last record is the suspension, (b) nothing finished, (c) the result says so."""

    last = rig.records()[-1]
    assert last["event"] == "campaign_suspended"
    payload = last["payload"]
    assert (payload["reason"], payload["owner"]) == (reason, owner)
    assert payload["evidence"]["evidence_kind"] == kind
    assert "campaign_finished" not in rig.events()
    assert "threshold_reached" not in rig.events()
    assert result.terminal == "suspended"
    assert result.finished is None and result.result is None
    assert result.suspended == payload
    assert (result.owner, result.code) == (owner, reason)
    return dict(payload)


def v8(**overrides: Any) -> Any:
    return v8_policy(**{"rounds": 1, "attempts_per_round": 1, "sample_retries": 0, **overrides})


def _owner_rig(
    tmp_path: Path, owner: str, trip: Trip, campaign_policy: Any, *, schema: str = SCHEMA_V15
) -> Rig:
    rig = Rig(tmp_path, campaign_policy=campaign_policy, run_name=f"{owner}-run", schema=schema)
    if owner == "target":
        rig.adapter = TrippingAdapter(trip, "target_failed")
    elif owner == "rollout":
        rig.adapter = TrippingAdapter(trip, "time_budget")
    elif owner == "environment":
        rig.environment = TrippingEnvironment(trip)
    else:
        rig.scorer = TrippingScorer(
            trip, scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
        )
    return rig


# -- causes 1-3: thresholds -------------------------------------------------


async def test_an_unrepaired_target_is_repaired_once_per_resume(tmp_path: Path) -> None:
    """#78 (spec §3.1): no repair loop inside one run; each resume costs one try."""

    trip = Trip(armed=True)
    rig = _owner_rig(tmp_path, "target", trip, v8(indeterminate=STRICT))
    await rig.run()
    first = len(payloads_of(rig, "indeterminate_observed"))

    second = await rig.resume()  # still broken
    assert_suspended(rig, second, "target_unstable", "target", "threshold")
    assert rig.events().count("repairs_dispatched") == 1
    assert len(payloads_of(rig, "indeterminate_observed")) == 2 * first

    third = await rig.resume()  # still broken: one more try each
    assert_suspended(rig, third, "target_unstable", "target", "threshold")
    assert rig.events().count("repairs_dispatched") == 2
    assert len(payloads_of(rig, "indeterminate_observed")) == 3 * first
    assert "pairs_excluded" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


def _crash_after_reservation() -> dict[str, Any]:
    from tests.campaign_fakes import Crash

    def crash() -> None:
        raise Crash("power cut after repairs_dispatched")

    return {"after_repairs_dispatched": crash}


def _repair_starts(rig: Rig) -> list[dict[str, Any]]:
    return [
        r["payload"]
        for r in rig.measurement_records(measurement_id_for(None))
        if r["event"] == "rollout_started" and r["payload"].get("repair") is True
    ]


async def test_a_crash_after_the_reservation_resumes_without_a_second_record(
    tmp_path: Path,
) -> None:
    """#78 (spec §3.5): one reservation per measurement per window."""

    from tests.campaign_fakes import Crash

    trip = Trip(armed=True)
    rig = _owner_rig(tmp_path, "target", trip, v8(indeterminate=STRICT, rounds=2))
    first = await rig.run()
    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    exhausted = len(payloads_of(rig, "indeterminate_observed"))

    trip.armed = False
    with pytest.raises(Crash):
        await rig.resume(_hooks=_crash_after_reservation())
    assert rig.events()[-1] == "repairs_dispatched"
    assert _repair_starts(rig) == []

    resumed = await rig.resume()

    assert rig.events().count("repairs_dispatched") == 1
    assert payloads_of(rig, "repairs_dispatched")[0]["count"] == exhausted
    assert len(_repair_starts(rig)) == exhausted
    assert "pairs_excluded" not in rig.events()
    assert "attempt_opened" in rig.events()
    assert resumed.terminal == "finished" and resumed.finished is not None
    assert resumed.finished["outcome"] == "completed"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("exempt", [False, True], ids=["a27", "exempt"])
async def test_a_grant_survives_an_attestation_suspension_and_is_reserved_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exempt: bool
) -> None:
    """#78 (codex spec rounds 1-2): crash after the reservation, then a resume
    whose attestation fails (target_changed) before any repair starts, then a
    healthy resume: the grant still stands and a second repairs_dispatched
    reserves it in the new window."""

    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_fixtures import LyingAdapter

    if exempt:
        _refusal_prefix_exempt(monkeypatch)
    trip = Trip(armed=True)
    rig = _owner_rig(tmp_path, "target", trip, v8(indeterminate=STRICT, rounds=2))
    await rig.run()
    exhausted = len(payloads_of(rig, "indeterminate_observed"))
    trip.armed = False
    with pytest.raises(Crash):
        await rig.resume(_hooks=_crash_after_reservation())

    seed = scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
    refused = await rig.resume(adapter=LyingAdapter(seed, lie_about_the_seed=True))

    changed = assert_suspended(rig, refused, "target_changed", "target", "stage_record")
    assert rig.events().count("repairs_dispatched") == 1  # no new window yet
    assert _repair_starts(rig) == []
    changed_at = seq_of(rig, "campaign_suspended")[-1]

    resumed = await rig.resume(adapter=FakeAdapter())

    first, second = seq_of(rig, "repairs_dispatched")
    assert first < changed_at < second
    assert [p["count"] for p in payloads_of(rig, "repairs_dispatched")] == [exhausted] * 2
    starts = _repair_starts(rig)
    assert len(starts) == exhausted
    assert len({(p["case_id"], p["sample_id"]) for p in starts}) == exhausted
    assert "pairs_excluded" not in rig.events()
    assert resumed.terminal == "finished" and resumed.finished is not None
    assert resumed.finished["outcome"] == "completed"
    # The threshold repairs audit clean. From 0.3.0a27 (#94) the suspension
    # cites its refusal prefix and the audit decides it over those bytes.
    # Under an exempt version it carries none, and the audit's whole-journal
    # check (`_check_suspension_attestation`) reads the baseline's honest
    # attestations from the first run and cannot see a refusal at
    # materialization on the resume: the one finding of #94, kept as it was.
    assert ("prefix" in changed["evidence"]) is not exempt
    findings = rig.projection(verify=True)["verification"]["mismatches"]
    expected = (
        [
            (
                "campaign_suspended",
                "the measurement's attested identities admit the sealed Candidate, "
                "so nothing refused it",
            )
        ]
        if exempt
        else []
    )
    assert [(row["event"], row["detail"]) for row in findings] == expected


async def test_another_owners_exhausted_pair_is_still_excluded(tmp_path: Path) -> None:
    """#78 (spec §3.1): only the suspending owner's pairs are repaired."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate={**LENIENT, "target": STRICT["target"]}),
        cases=corpus(4),
    )
    rig.adapter.dispositions[seed_key(rig, "case-01", "01")] = ["target_failed"]
    rig.adapter.dispositions[seed_key(rig, "case-02", "01")] = "time_budget"
    first = await rig.run()
    assert_suspended(rig, first, "target_unstable", "target", "threshold")

    await rig.resume()

    excluded = payloads_of(rig, "pairs_excluded")
    assert len(excluded) == 1 and excluded[0]["by_owner"] == {"rollout": 1}
    assert payloads_of(rig, "repairs_dispatched")[0]["count"] == 1
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


async def test_a_rate_only_threshold_resuspends_on_failed_repairs(tmp_path: Path) -> None:
    """#78 (spec §3.5): the repairs' own slots let a rate threshold cross again."""

    rate_only = {owner: {"max_count": 99, "max_rate": "0.1"} for owner in STRICT}
    trip = Trip(armed=True)
    rig = _owner_rig(tmp_path, "target", trip, v8(indeterminate=rate_only))
    first = await rig.run()
    assert_suspended(rig, first, "target_unstable", "target", "threshold")

    second = await rig.resume()  # still broken
    payload = assert_suspended(rig, second, "target_unstable", "target", "threshold")
    crossing = payload["evidence"]["crossing"]
    assert crossing["kind"] == "rate"
    assert crossing["count"] == crossing["denominator"] >= 1
    assert "pairs_excluded" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


#: Looks 3 and 6, so the winner's completion runs repeats 04-06 after it promotes.
TWO_LOOKS = {"budget": "0.05", "spend": {"3": "0.01", "6": "0.02"}}


async def test_every_opened_stage_completes_before_the_suspension(tmp_path: Path) -> None:
    """ADR-0008: the crossing on one sample still lets its stage run every sample."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=STRICT),
        cases=corpus(4),
    )
    rig.adapter.dispositions[seed_key(rig, "case-01", "01")] = "target_failed"

    result = await rig.run()

    assert_suspended(rig, result, "target_unstable", "target", "threshold")
    opened = payloads_of(rig, "stage_opened")
    assert len(opened) == 1
    terminals = [
        record
        for record in rig.measurement_records(measurement_id_for(None))
        if record["event"] in ("rollout_finished", "rollout_failed")
    ]
    assert opened[0]["sample_count"] >= 4
    assert len(terminals) == opened[0]["sample_count"]


async def test_a_resumed_window_restarts_and_a_later_crossing_suspends_again(
    tmp_path: Path,
) -> None:
    """Spec §5.3: a threshold counts only the tries since the latest suspension."""

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=STRICT))
    rig.adapter.dispositions[seed_key(rig)] = ["target_failed"]

    first = await rig.run()

    one = assert_suspended(rig, first, "target_unstable", "target", "threshold")
    assert one["evidence"]["window_start"] is None
    assert one["evidence"]["crossing"]["count"] == 1
    first_seq = seq_of(rig, "campaign_suspended")[0]

    # The resumed baseline repairs its exhausted pair and completes with nothing
    # excluded (#78), and the Attempt's own target failure crosses the new
    # window at the round barrier.
    rig.adapter.dispositions[edited_key(tmp_path)] = "target_failed"
    second = await rig.resume()

    two = assert_suspended(rig, second, "target_unstable", "target", "threshold")
    assert two["evidence"]["window_start"] == first_seq
    # Only the Attempt's try is in the window; the campaign total is 2.
    assert two["evidence"]["crossing"]["count"] == 1
    assert len(payloads_of(rig, "indeterminate_observed")) == 2
    events = rig.events()
    assert "baseline_complete" in events
    assert "pairs_excluded" not in events
    # #91: closed at the round barrier -- a crossing there leaves the Attempt held open.
    assert "attempt_closed" not in events and "look_not_started" not in events
    assert events[-1] == "campaign_suspended"
    assert rig.events().count("campaign_suspended") == 2
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


# -- causes 4-5: fault rows --------------------------------------------------


@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize(
    "script, reason, code",
    [
        ("invalid_request", "target_contract_violated", "invalid_request"),
        ("pin_mismatch", "target_changed", "pin_mismatch"),
    ],
)
async def test_a_target_fault_row_suspends_with_its_rows(
    tmp_path: Path, script: str, reason: str, code: str, attempts: int
) -> None:
    # Under `/8` the failure's code is journaled at every attempt count, `1`
    # included, so a structural fault is never read as a
    # contract one; neither class is ever resent.
    retries = {"max_attempts": attempts, "turn_max_attempts": attempts}
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, call_retries=retries),
    )
    rig.adapter.dispositions[seed_key(rig)] = script

    result = await rig.run()

    payload = assert_suspended(rig, result, reason, "target", "fault_rows")
    evidence = payload["evidence"]
    row = {
        "measurement_id": measurement_id_for(None),
        "case_id": "case-01",
        "sample_id": "02",
        "retry_ordinal": 0,
        "reason": "infrastructure_failed",
        "code": code,
    }
    assert evidence["rows"] == [row]
    assert (evidence["count"], evidence["truncated"]) == (1, False)
    assert evidence["rows_digest"] == fault_rows_digest(
        [(row["measurement_id"], "case-01", "02", 0, "infrastructure_failed")]
    )
    assert "baseline_complete" not in rig.events()

    # The target still fails the same way: a resume spends exactly one repair
    # try on the row and suspends again on that try's row, without opening
    # another stage (Task 9).
    stages = len(payloads_of(rig, "stage_opened"))
    again = await rig.resume()

    assert_suspended(rig, again, reason, "target", "fault_rows")
    suspensions = payloads_of(rig, "campaign_suspended")
    assert len(suspensions) == 2
    assert suspensions[1]["evidence"]["rows"] == [{**row, "retry_ordinal": 1}]
    assert len(payloads_of(rig, "stage_opened")) == stages
    assert "baseline_complete" not in rig.events()


async def test_an_attempt_fault_row_suspends_and_leaves_the_attempt_open(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.adapter.dispositions[edited_key(tmp_path)] = "invalid_request"

    result = await rig.run()

    payload = assert_suspended(rig, result, "target_contract_violated", "target", "fault_rows")
    assert payload["evidence"]["rows"][0]["measurement_id"] == "m-r01-a01"
    # A closed Attempt could never be repaired: it stays open over its stage.
    assert "attempt_closed" not in rig.events()
    assert "selection_decided" not in rig.events()


class _DriftingInstance(FakeInstance):
    """It reboots while closing its first session, so the after-close identity
    of that rollout no longer matches the one that opened it. Later rollouts
    see the drifted provenance throughout and pass the same check."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._drifted = False

    async def identity(self) -> TargetIdentity:
        identity = await super().identity()
        if not self._drifted:
            return identity
        return TargetIdentity(
            identity.semantic,
            InstanceProvenance("drifted-boot", self.candidate_digest),
            identity.attested,
        )

    async def close(self, session: Any) -> None:
        await super().close(session)
        self._drifted = True


class _WrongSeedInstance(FakeInstance):
    """Its first session confirms a seed the sample never requested."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._opens = 0

    async def open(self, case_public: Any, seed: int | None, request_id: str) -> Any:
        session = await super().open(case_public, seed, request_id)
        self._opens += 1
        if self._opens == 1:
            session.effective_seed = (seed or 0) + 1000
        return session


class _CoreCheckAdapter(FakeAdapter):
    """A target whose instances break one of the core's own deterministic checks."""

    def __init__(self, instance_class: type[FakeInstance], **overrides: Any) -> None:
        super().__init__(**overrides)
        self._instance_class = instance_class

    def instance_for(self, candidate: Any, instance_id: str, boot_id: str) -> FakeInstance:
        return self._instance_class(
            instance_id,
            boot_id,
            self._semantic_for(candidate.dependency_digest),
            candidate.digest,
            self.dispositions,
            self.capabilities.max_concurrent_sessions,
        )


@pytest.mark.parametrize(
    "instance_class, reason, code",
    [
        (_DriftingInstance, "target_changed", "identity_changed"),
        (_WrongSeedInstance, "target_contract_violated", "invalid_response"),
    ],
)
async def test_a_core_detected_target_failure_suspends_under_its_own_code(
    tmp_path: Path, instance_class: type[FakeInstance], reason: str, code: str
) -> None:
    """Final review, Important 2: the three checks the core runs on a value a
    *successful* call returned must reach the journal with their own
    `error_code`, or `_fault_reason` reads a structural drift as a contract
    violation. A drift is `target_changed`; a seed-contract breach is
    `target_contract_violated`."""

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.adapter = _CoreCheckAdapter(instance_class)

    result = await rig.run()

    payload = assert_suspended(rig, result, reason, "target", "fault_rows")
    rows = payload["evidence"]["rows"]
    assert rows and all(row["reason"] == "infrastructure_failed" for row in rows)
    assert {row["code"] for row in rows} == {code}


async def test_a_missing_primary_metric_suspends_on_the_scorer(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.scorer = PromotingScorer(
        scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest,
        missing_primary_for=frozenset({"case-01"}),
    )

    result = await rig.run()

    payload = assert_suspended(rig, result, "scorer_metric_missing", "scorer", "fault_rows")
    rows = payload["evidence"]["rows"]
    assert rows and all(row["reason"] == "scorer_metric_missing" for row in rows)
    assert all(row["case_id"] == "case-01" and row["code"] is None for row in rows)
    assert payload["evidence"]["count"] == len(rows)


class _EditedOutOfRangeScorer(PromotingScorer):
    """The edited Candidates score `2.0` on `edited_out_of_range`; the seed scores in range."""

    def __init__(self, seed_digest: str, edited_out_of_range: frozenset[str]) -> None:
        super().__init__(seed_digest)
        self.edited_out_of_range = edited_out_of_range

    def value_for(self, candidate_digest: str, case_id: str) -> float:
        if candidate_digest != self.seed_digest and case_id in self.edited_out_of_range:
            return 2.0
        return super().value_for(candidate_digest, case_id)


def _fixed_scorer(broken: PromotingScorer) -> PromotingScorer:
    """`broken` repaired and under another identity digest: a comparable change."""

    fixed = copy.copy(broken)
    fixed.out_of_range_for = frozenset()
    if isinstance(fixed, _EditedOutOfRangeScorer):
        fixed.edited_out_of_range = frozenset()
    fixed.identity = ScorerIdentity(broken.identity.kind, "1" * 64)
    return fixed


def _accept_scorer_change(refused: IdentityChanged, effect: str) -> Authorization:
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "the scorer was fixed or the version moved",
        now="2026-10-03T10:00:00Z",
        effect=effect,
    )


async def test_an_out_of_range_attempt_row_suspends_and_leaves_the_attempt_open(
    tmp_path: Path,
) -> None:
    """Attempt stage: the champion's rows are valid, the edited tree's `case-01`
    is out of range -- suspended, the Attempt open, repaired once, then the fixed scorer."""

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT, rounds=2))
    seed = scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
    broken = _EditedOutOfRangeScorer(seed, frozenset({"case-01"}))
    rig.scorer = broken

    result = await rig.run()

    payload = assert_suspended(rig, result, "scorer_metric_out_of_range", "scorer", "fault_rows")
    rows = payload["evidence"]["rows"]
    assert rows and {row["measurement_id"] for row in rows} == {"m-r01-a01"}
    assert "attempt_closed" not in rig.events()
    assert "baseline_complete" in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []

    # The same broken scorer: each named row gets exactly one repair try, is
    # still out of range, and the campaign suspends again on those rows.
    again = await rig.resume()

    assert_suspended(rig, again, "scorer_metric_out_of_range", "scorer", "fault_rows")
    repairs = [
        r["payload"]
        for r in rig.measurement_records("m-r01-a01")
        if r["event"] == "rollout_started" and r["payload"].get("repair") is True
    ]
    assert {(p["case_id"], p["sample_id"]) for p in repairs} == {
        (row["case_id"], row["sample_id"]) for row in rows
    }
    assert len(repairs) == len(rows)
    assert "attempt_closed" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []

    # The fixed scorer is a comparable change: `continue` is not offered for a
    # scorer, so the operator accepts `rebaseline` (spec §4.4); the frozen
    # Attempt is never reused and the next round measures with the fixed scorer.
    fixed = _fixed_scorer(broken)
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(scorer=fixed)
    assert refused.value.effects == ("rebaseline", "close")
    resumed = await rig.resume(
        scorer=fixed, authorizations=[_accept_scorer_change(refused.value, "rebaseline")]
    )

    assert resumed.terminal == "finished"
    assert payloads_of(rig, "round_closed")[0]["outcome"] == "condition_changed"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


async def test_a_crash_after_the_baseline_stage_suspends_before_baseline_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#97 spec §4.1: the baseline's stage callback never ran; the completion decides fault rows first."""

    from tests.campaign_fakes import Crash, crash_after_the_stage_once

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT, rounds=2))
    seed = scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
    rig.scorer = PromotingScorer(seed, out_of_range_for=frozenset({"case-01"}))
    cut = crash_after_the_stage_once(monkeypatch, measurement_prefix="m-r00-")

    with pytest.raises(Crash):
        await rig.run()
    assert cut == ["m-r00-champion"]
    assert "baseline_complete" not in rig.events()

    resumed = await rig.resume()

    assert_suspended(rig, resumed, "scorer_metric_out_of_range", "scorer", "fault_rows")
    assert "baseline_complete" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


# -- Task 9: repair tries --------------------------------------------------------


def _starts(rig: Rig, measurement_id: str, case_id: str, sample_id: str) -> list[dict[str, Any]]:
    """The `rollout_started` payloads of one sample, in try order."""

    return [
        record["payload"]
        for record in rig.measurement_records(measurement_id)
        if record["event"] == "rollout_started"
        and (record["payload"]["case_id"], record["payload"]["sample_id"]) == (case_id, sample_id)
    ]


def _accounting(rig: Rig) -> list[tuple[Any, ...]]:
    return [
        (o["measurement_id"], o["case_id"], o["sample_id"], o["retry_ordinal"], o["retry_follows"])
        for o in payloads_of(rig, "indeterminate_observed")
    ] + [(o["count"],) for o in payloads_of(rig, "indeterminate_observed")]


async def test_a_resume_repairs_a_fault_row_once_and_the_stage_completes(
    tmp_path: Path,
) -> None:
    policy = v8(indeterminate=LENIENT, sample_retries=1)
    # A D1 retry elsewhere in the same stage, so the accounting is not trivial.
    flaky = ("case-02", "01")
    rig = Rig(tmp_path / "faulted", campaign_policy=policy)
    rig.adapter.dispositions[seed_key(rig)] = ["invalid_request"]
    rig.adapter.dispositions[seed_key(rig, *flaky)] = ["target_failed"]

    first = await rig.run()

    assert_suspended(rig, first, "target_contract_violated", "target", "fault_rows")
    baseline = measurement_id_for(None)
    assert [start.get("repair") for start in _starts(rig, baseline, "case-01", "02")] == [None]

    result = await rig.resume()

    starts = _starts(rig, baseline, "case-01", "02")
    assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    assert len(payloads_of(rig, "campaign_suspended")) == 1
    assert "baseline_complete" in rig.events()
    assert result.terminal != "suspended"
    # The repair try is outside `sample_retries` and outside the denominator:
    # the accounting is exactly that of the same run without the fault.
    clean = Rig(tmp_path / "clean", campaign_policy=policy)
    clean.adapter.dispositions[seed_key(clean, *flaky)] = ["target_failed"]
    await clean.run()
    assert _accounting(rig) == _accounting(clean)
    assert _accounting(rig)[0][3:] == (0, True)


async def test_a_truncated_suspension_repairs_every_fault_row_it_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence lists only the first rows; the owed set is re-derived from
    the measurement prefix, so an unlisted row is repaired on the same resume."""

    from evolution.tree import campaign_schema

    monkeypatch.setattr(campaign_schema, "MAX_FAULT_ROWS", 1)
    monkeypatch.setattr(campaign_module, "MAX_FAULT_ROWS", 1)
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, sample_retries=1),
    )
    faulted = [("case-01", "02"), ("case-02", "01")]
    for case_id, sample_id in faulted:
        rig.adapter.dispositions[seed_key(rig, case_id, sample_id)] = ["invalid_request"]
    baseline = measurement_id_for(None)

    first = await rig.run()

    evidence = assert_suspended(rig, first, "target_contract_violated", "target", "fault_rows")[
        "evidence"
    ]
    assert (evidence["count"], len(evidence["rows"]), evidence["truncated"]) == (2, 1, True)

    await rig.resume()

    for case_id, sample_id in faulted:
        starts = _starts(rig, baseline, case_id, sample_id)
        assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    assert "baseline_complete" in rig.events()
    assert len(payloads_of(rig, "campaign_suspended")) == 1


async def test_a_crash_after_the_listed_repairs_still_repairs_the_unlisted_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 1: a truncated suspension on a baseline stage that is not the
    last look, and a crash after the listed row's repair but before the
    unlisted row's. The planner and the executor read one owed set (listed
    rows or the prefix reconstruction), and the executor dispatches it from the
    measurement's whole schedule, so a resume repairs the unlisted row instead
    of opening the next stage -- or, once past it, planning the same stage
    forever."""

    from evolution.tree import campaign_schema
    from evolution.tree.campaign_measurement import Measurement, StagePlan
    from tests.campaign_fakes import Crash

    monkeypatch.setattr(campaign_schema, "MAX_FAULT_ROWS", 1)
    monkeypatch.setattr(campaign_module, "MAX_FAULT_ROWS", 1)
    two_looks = {"budget": "0.05", "spend": {"2": "0.01", "3": "0.02"}}
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, alpha=two_looks),
    )
    faulted = [("case-01", "01"), ("case-02", "01")]
    for case_id, sample_id in faulted:
        rig.adapter.dispositions[seed_key(rig, case_id, sample_id)] = ["invalid_request"]
    baseline = measurement_id_for(None)

    first = await rig.run()

    evidence = assert_suspended(rig, first, "target_contract_violated", "target", "fault_rows")[
        "evidence"
    ]
    assert (evidence["count"], evidence["truncated"]) == (2, True)
    listed = (evidence["rows"][0]["case_id"], evidence["rows"][0]["sample_id"])
    (unlisted,) = [key for key in faulted if key != listed]
    assert len(payloads_of(rig, "stage_opened")) == 1

    original = Measurement.run_stage

    async def torn(self: Any, plan: Any, **kwargs: Any) -> Any:
        rows = tuple(r for r in plan.rows if (r["case_id"], r["sample_id"]) != unlisted)
        kwargs["repair_rows"] = kwargs.get("repair_rows", frozenset()) - {unlisted}
        await original(self, StagePlan(plan.stage, plan.look, rows), **kwargs)
        raise Crash("power cut")

    monkeypatch.setattr(Measurement, "run_stage", torn)
    with pytest.raises(Crash):
        await rig.resume()
    monkeypatch.setattr(Measurement, "run_stage", original)
    assert [s.get("repair") for s in _starts(rig, baseline, *listed)] == [None, True]
    assert [s.get("repair") for s in _starts(rig, baseline, *unlisted)] == [None]

    # A livelock would plan the same stage forever: bound the dispatches.
    calls = 0
    run_stage = campaign_module._Campaign.run_stage

    async def bounded(self: Any, measurement_id: str, stage: int) -> Any:
        nonlocal calls
        calls += 1
        assert calls <= 20, "the planner keeps returning to a stage that dispatches nothing"
        return await run_stage(self, measurement_id, stage)

    monkeypatch.setattr(campaign_module._Campaign, "run_stage", bounded)
    result = await rig.resume()

    for key in faulted:
        starts = _starts(rig, baseline, *key)
        assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    assert "baseline_complete" in rig.events()
    assert len(payloads_of(rig, "campaign_suspended")) == 1
    assert result.terminal != "suspended"


async def test_an_unrepaired_fault_costs_one_try_per_resume(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, sample_retries=1),
    )
    rig.adapter.dispositions[seed_key(rig)] = "invalid_request"
    baseline = measurement_id_for(None)

    await rig.run()
    for resume in range(1, 4):
        before = len(_starts(rig, baseline, "case-01", "02"))
        again = await rig.resume()

        payload = assert_suspended(rig, again, "target_contract_violated", "target", "fault_rows")
        starts = _starts(rig, baseline, "case-01", "02")
        assert len(starts) == before + 1
        assert starts[-1]["repair"] is True and starts[-1]["retry_ordinal"] == resume
        assert [row["retry_ordinal"] for row in payload["evidence"]["rows"]] == [resume]
    assert "baseline_complete" not in rig.events()


async def test_an_indeterminate_repair_exhausts_a_baseline_sample(tmp_path: Path) -> None:
    # `sample_retries: 2` leaves D1 room after the repair's ordinal 1: the
    # repair still exhausts the sample (spec §5.4 as refined).
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, sample_retries=2),
    )
    rig.adapter.dispositions[seed_key(rig)] = ["invalid_request", "target_failed"]
    baseline = measurement_id_for(None)

    await rig.run()
    await rig.resume()

    starts = _starts(rig, baseline, "case-01", "02")
    assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    excluded = payloads_of(rig, "pairs_excluded")[0]
    assert excluded["pairs"] == [
        {
            "case_id": "case-01",
            "sample_id": "02",
            "owner": "target",
            "reason": "target_unstable",
            "tries": 2,
        }
    ]
    observed = payloads_of(rig, "indeterminate_observed")
    assert [(o["sample_id"], o["retry_ordinal"], o["retry_follows"]) for o in observed] == [
        ("02", 1, False)
    ]
    assert "baseline_complete" in rig.events()
    assert len(payloads_of(rig, "campaign_suspended")) == 1


async def test_an_indeterminate_repair_closes_the_attempt_on_incomplete_sample(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, sample_retries=2),
    )
    rig.adapter.dispositions[edited_key(tmp_path)] = ["invalid_request", "target_failed"]

    first = await rig.run()
    assert_suspended(rig, first, "target_contract_violated", "target", "fault_rows")
    await rig.resume()

    starts = _starts(rig, "m-r01-a01", "case-01", "01")
    assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["outcome"], closed["reason"]) == ("insufficient_evidence", "incomplete_sample")
    assert len(payloads_of(rig, "campaign_suspended")) == 1
    # #91: closed at the round barrier, right before the round's selection.
    records = rig.records()
    at = _index_of(records, "attempt_closed", "r01-a01")
    assert records[at - 1]["event"] == "look_not_started"
    assert records[at + 1]["event"] == "selection_decided"


async def test_a_repaired_attempt_row_that_scores_lets_the_attempt_decide(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.adapter.dispositions[edited_key(tmp_path)] = ["invalid_request"]

    await rig.run()
    result = await rig.resume()

    starts = _starts(rig, "m-r01-a01", "case-01", "01")
    assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    closed = payloads_of(rig, "attempt_closed")[0]
    assert closed["reason"] != "incomplete_sample"
    assert result.terminal != "suspended"


# -- #91 spec §3.1: which Attempts the executor names lacking -----------------


async def test_lacking_names_an_attempt_only_once_its_stage_is_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R11: while a row of the Attempt's stage has not started or is
    still owed a D1 try the Attempt is active, not lacking; once every row
    scored or exhausted, the one exhausted pair makes it lacking."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, sample_retries=1),
    )
    failing = edited_key(tmp_path)
    rig.adapter.dispositions[failing] = ["target_failed", "target_failed"]
    executor: list[Any] = []
    during: list[tuple[frozenset[str], str, int, bool]] = []
    at_bar: list[frozenset[str]] = []

    original_stage = campaign_module._Campaign.attempt_stage

    async def attempt_stage(self: Any, attempt_id: str, stage: int) -> None:
        executor[:] = [self]
        await original_stage(self, attempt_id, stage)

    def probe() -> None:
        campaign = executor[0]
        row = next(
            row
            for row in campaign.measurements["m-r01-a01"].sample_rows(3)
            if (row.case_id, row.sample_id) == failing[1:]
        )
        during.append(
            (campaign.lacking_attempts(), row.disposition, row.retry_ordinal, row.exhausted)
        )

    original_send = FakeInstance.send

    async def send(self: FakeInstance, session: Any, message: Any, request_id: str) -> Any:
        if executor and self.candidate_digest == failing[0]:
            probe()
        return await original_send(self, session, message, request_id)

    original_append = AppendLog.append

    def append(
        self: AppendLog, run_id: str, event: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> int:
        sequence = original_append(self, run_id, event, payload, *a, **k)
        if (
            executor
            and self is not executor[0].log
            and (payload.get("case_id"), payload.get("sample_id")) == failing[1:]
            and payload.get("retry_ordinal") == 0
            and "m-r01-a01" in executor[0].measurements
        ):
            # Each durable measurement record of the first try, its terminal
            # included: after it the D1 retry is owed and not yet started.
            probe()
        return sequence

    original_bar = campaign_module._Campaign.bar_look

    def bar_look(self: Any, attempt_id: str, look: int) -> bool:
        at_bar.append(self.lacking_attempts())
        return original_bar(self, attempt_id, look)

    monkeypatch.setattr(campaign_module._Campaign, "attempt_stage", attempt_stage)
    monkeypatch.setattr(FakeInstance, "send", send)
    monkeypatch.setattr(AppendLog, "append", append)
    monkeypatch.setattr(campaign_module._Campaign, "bar_look", bar_look)

    await rig.run()

    assert during and all(lacking == frozenset() for lacking, *_row in during)
    # The failing row was seen owed its D1 try: its first try finished
    # indeterminate below the try limit, with the retry not yet scored.
    assert any(
        (disposition, ordinal, exhausted) == ("indeterminate", 0, False)
        for _lacking, disposition, ordinal, exhausted in during
    )
    assert at_bar == [frozenset({"r01-a01"})]
    closed = payloads_of(rig, "attempt_closed")[0]
    assert (closed["outcome"], closed["reason"]) == ("insufficient_evidence", "incomplete_sample")


async def test_an_attempt_owing_a_fault_repair_is_active_and_reactivates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R13: a fault row keeps the Attempt out of `lacking` -- it is
    active, owing its repair -- and the repair try is dispatched (I1 §5.4). A
    threshold-granted repair keeps a held Attempt in `lacking` instead
    (`test_an_outage_holds_the_attempt_and_the_round_barrier_suspends`)."""

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.adapter.dispositions[edited_key(tmp_path)] = ["invalid_request"]
    first = await rig.run()
    assert_suspended(rig, first, "target_contract_violated", "target", "fault_rows")

    views: list[Any] = []
    original_view = campaign_module._Campaign.view

    def view(self: Any) -> Any:
        result = original_view(self)
        views.append(result)
        return result

    monkeypatch.setattr(campaign_module._Campaign, "view", view)
    resumed = await rig.resume()

    assert views[0].lacking == frozenset()
    assert views[0].repairs["m-r01-a01"] == frozenset({("case-01", "01")})
    starts = _starts(rig, "m-r01-a01", "case-01", "01")
    assert [(s["retry_ordinal"], s.get("repair")) for s in starts] == [(0, None), (1, True)]
    assert resumed.terminal != "suspended"


# -- #91 spec §3.1-§3.2: held Attempts are decided at the round barrier --------

#: Every Candidate row of `corpus(2)` under looks `{3}`.
ROWS = tuple((case, sample) for case in ("case-01", "case-02") for sample in ("01", "02", "03"))


def _fail_candidate(
    rig: Rig, tmp_path: Path, value: str, disposition: str = "target_failed"
) -> None:
    """Every row of the Candidate `edits` writes as `value` plays `disposition`."""

    for case, sample in ROWS:
        rig.adapter.dispositions[edited_key(tmp_path, case, sample, value)] = disposition


def _attempt_trace(rig: Rig, attempt_id: str) -> list[str]:
    """The campaign records of one Attempt and its Candidate measurement, in order."""

    measurement_id = f"m-{attempt_id}"
    return [
        record["event"]
        for record in rig.records()
        if record["payload"].get("attempt_id") == attempt_id
        or record["payload"].get("measurement_id") == measurement_id
    ]


def _closed(rig: Rig) -> dict[str, dict[str, Any]]:
    return {p["attempt_id"]: p for p in payloads_of(rig, "attempt_closed")}


def _repairs_of(rig: Rig, measurement_id: str) -> list[tuple[str, str]]:
    return [
        (r["payload"]["case_id"], r["payload"]["sample_id"])
        for r in rig.measurement_records(measurement_id)
        if r["event"] == "rollout_started" and r["payload"].get("repair") is True
    ]


def _last_index(records: list[dict[str, Any]], attempt_id: str) -> int:
    """The index of the Attempt's last record before the round's selection."""

    # ADR-0032: the Candidate barrier precedes the family-14 final-measurement phase.
    selected = (
        _index_of(records, "finalists_bound")
        if any(r["event"] == "finalists_bound" for r in records)
        else _index_of(records, "selection_decided")
    )
    return max(
        index
        for index, record in enumerate(records[:selected])
        if record["payload"].get("attempt_id") == attempt_id
        or record["payload"].get("measurement_id") == f"m-{attempt_id}"
    )


def _index_of(records: list[dict[str, Any]], event: str, attempt_id: str | None = None) -> int:
    return next(
        index
        for index, record in enumerate(records)
        if record["event"] == event
        and (attempt_id is None or record["payload"].get("attempt_id") == attempt_id)
    )


def _held_rig(
    tmp_path: Path, parallel: int, indeterminate: Any, attempts: int = 2, schema: str = SCHEMA_V15
) -> Rig:
    return Rig(
        tmp_path / f"parallel-{parallel}",
        campaign_policy=v8(
            indeterminate=indeterminate,
            attempts_per_round=attempts,
            parallel_attempts=parallel,
        ),
        schema=schema,
    )


async def test_an_unrepaired_held_attempt_is_repaired_once_per_resume(tmp_path: Path) -> None:
    """#91 spec §3.2: the still-failing target costs each named pair one try."""

    rig = _held_rig(tmp_path, 1, STRICT)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")
    await rig.run()

    second = await rig.resume()

    assert_suspended(rig, second, "target_unstable", "target", "threshold")
    assert sorted(_repairs_of(rig, "m-r01-a01")) == sorted(ROWS)
    for case, sample in ROWS:
        assert len(_starts(rig, "m-r01-a01", case, sample)) == 2
    assert "r01-a01" not in _closed(rig)
    assert "look_not_started" not in rig.events()
    assert rig.events().count("campaign_suspended") == 2
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("parallel", [1, 2])
async def test_below_the_threshold_a_held_attempt_closes_at_the_round_barrier(
    tmp_path: Path, parallel: int
) -> None:
    """#91 spec §3.1 step 2: `look_not_started` and `incomplete_sample` move to
    the round barrier, after every other Attempt's last record, before selection."""

    rig = _held_rig(tmp_path, parallel, LENIENT)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")

    result = await rig.run()

    assert result.terminal == "finished"
    records = rig.records()
    closed = _index_of(records, "attempt_closed", "r01-a01")
    assert (records[closed]["payload"]["outcome"], records[closed]["payload"]["reason"]) == (
        "insufficient_evidence",
        "incomplete_sample",
    )
    assert _index_of(records, "look_not_started", "r01-a01") == closed - 1
    assert _last_index(records, "r01-a02") < closed - 1
    assert closed < _index_of(records, "selection_decided")
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("exempt", [False])
async def test_another_owners_exhausted_pairs_close_their_attempt_at_the_next_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exempt: bool
) -> None:
    """#91 spec §3.2: under a held-repair-exempt version scorer-exhausted pairs carry no `target`
    grant; after the healthy resume their Attempt closes `incomplete_sample` at
    the round barrier. a27 spec §3.1 (#116): the `target` suspension is the
    first after their tries, so it names them -- each gets one repair try, the
    scorer fails it again, and the Attempt closes `incomplete_sample` at the
    next barrier all the same."""

    from tests.test_tree_campaign_fixtures import ScorerFailingFor, digest_of

    if exempt:
        _held_repair_exempt(monkeypatch)
    rig = _held_rig(tmp_path, 1, {**LENIENT, "target": STRICT["target"]})
    rig.scorer = ScorerFailingFor(
        Trip(armed=True),
        scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest,
        frozenset({digest_of(tmp_path, "VALUE = 2\n", "scorer-a")}),
    )
    rig.adapter.dispositions[edited_key(tmp_path, "case-01", "01", "VALUE = 3\n")] = "target_failed"

    first = await rig.run()

    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    assert "attempt_closed" not in rig.events()
    owners = {(o["measurement_id"], o["owner"]) for o in payloads_of(rig, "indeterminate_observed")}
    assert owners == {("m-r01-a01", "scorer"), ("m-r01-a02", "target")}

    rig.adapter.dispositions.clear()
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    dispatched = [p["measurement_id"] for p in payloads_of(rig, "repairs_dispatched")]
    if exempt:
        assert dispatched == ["m-r01-a02"]
        assert _repairs_of(rig, "m-r01-a01") == []
    else:
        assert sorted(dispatched) == ["m-r01-a01", "m-r01-a02"]
        assert sorted(_repairs_of(rig, "m-r01-a01")) == sorted(ROWS)
    records = rig.records()
    closed = _index_of(records, "attempt_closed", "r01-a01")
    assert records[closed]["payload"]["reason"] == "incomplete_sample"
    assert _index_of(records, "attempt_closed", "r01-a02") < closed
    # ADR-0032: a Candidate that passes focus and causal closes with gate_passed.
    assert _closed(rig)["r01-a02"]["reason"] == "gate_passed"
    assert closed < _index_of(records, "selection_decided")
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


# -- a27 spec §3 (#116): a held Attempt is repaired after any suspension ------


def _held_repair_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the running producer exempt from held repair, as `0.3.0a26` is."""

    from evolution import __version__
    from evolution.tree import campaign_schema

    monkeypatch.setattr(
        campaign_schema,
        "HELD_REPAIR_EXEMPT_VERSIONS",
        campaign_schema.HELD_REPAIR_EXEMPT_VERSIONS | {__version__},
    )


class _MissingFor(PromotingScorer):
    """Omits the primary metric on `cases` for the `digests` Candidates while armed."""

    def __init__(self, seed_digest: str, digests: frozenset[str], cases: frozenset[str]) -> None:
        super().__init__(seed_digest)
        self.digests = digests
        self.cases = cases
        self.armed = True

    async def score(self, case: Any, view: Any, candidate: Any) -> Any:
        self.missing_primary_for = (
            self.cases if self.armed and candidate.digest in self.digests else frozenset()
        )
        return await super().score(case, view, candidate)


def _held_then_faulted_rig(
    tmp_path: Path, parallel: int, indeterminate: Any = LENIENT, *, schema: str = SCHEMA_V15
) -> tuple[Rig, _MissingFor]:
    """Attempt 1's Candidate fails every row on the `target` below the count
    threshold (held); Attempt 2's Candidate faults on the scorer on `case-01`."""

    from tests.test_tree_campaign_fixtures import digest_of

    rig = _held_rig(tmp_path, parallel, indeterminate, schema=schema)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")
    scorer = _MissingFor(
        scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest,
        frozenset({digest_of(tmp_path, "VALUE = 3\n", f"missing-{parallel}")}),
        frozenset({"case-01"}),
    )
    rig.scorer = scorer
    return rig, scorer


async def _fault_suspended(rig: Rig) -> int:
    """Run to Attempt 2's fault-row suspension, with Attempt 1 held open; its seq."""

    first = await rig.run()

    payload = assert_suspended(rig, first, "scorer_metric_missing", "scorer", "fault_rows")
    assert {row["measurement_id"] for row in payload["evidence"]["rows"]} == {"m-r01-a02"}
    owners = {(o["measurement_id"], o["owner"]) for o in payloads_of(rig, "indeterminate_observed")}
    assert owners == {("m-r01-a01", "target")}
    assert "attempt_closed" not in rig.events()
    assert "repairs_dispatched" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []
    return seq_of(rig, "campaign_suspended")[-1]


@pytest.mark.parametrize("exempt", [False])
@pytest.mark.parametrize("parallel", [1, 2])
async def test_a_held_attempt_is_repaired_after_another_attempts_fault_suspension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int, exempt: bool
) -> None:
    """a27 spec §3.1, §7: Attempt 1 is held after a `target` outage below the
    threshold; Attempt 2 then suspends on a scorer fault row, before the round
    barrier. On the healthy resume Attempt 1's exhausted pairs get one repair
    try each, in one reserved slot each, and its look is evaluated on them.
    Under an exempt version (as `0.3.0a26`) nothing names them, and the round
    barrier closes Attempt 1 `incomplete_sample` as before."""

    if exempt:
        _held_repair_exempt(monkeypatch)
    rig, scorer = _held_then_faulted_rig(tmp_path, parallel)
    await _fault_suspended(rig)

    scorer.armed = False
    rig.adapter.dispositions.clear()
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    assert rig.events().count("campaign_suspended") == 1
    # ADR-0032: a Candidate that passes focus and causal closes with gate_passed.
    assert _closed(rig)["r01-a02"]["reason"] == "gate_passed"
    dispatched = [
        (p["measurement_id"], p["count"])
        for p in payloads_of(rig, "repairs_dispatched")
        if p["measurement_id"] == "m-r01-a01"
    ]
    if not exempt:
        assert dispatched == [("m-r01-a01", len(ROWS))]
        assert sorted(_repairs_of(rig, "m-r01-a01")) == sorted(ROWS)
        assert "r01-a01" in [p["attempt_id"] for p in payloads_of(rig, "comparison_look_evaluated")]
        assert _closed(rig)["r01-a01"]["outcome"] != "insufficient_evidence"
        assert "look_not_started" not in rig.events()
    else:
        assert dispatched == []
        assert _repairs_of(rig, "m-r01-a01") == []
        closed = _closed(rig)["r01-a01"]
        assert (closed["outcome"], closed["reason"]) == (
            "insufficient_evidence",
            "incomplete_sample",
        )
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("parallel", [1, 2])
async def test_a_held_attempt_failing_its_repair_is_held_for_the_new_window(
    tmp_path: Path, parallel: int
) -> None:
    """a27 spec §3.1: the target still fails Attempt 1's repairs. It is held
    again, and the round barrier checks the window the repairs ran in: their
    reserved slots make it cross the rate, so the campaign suspends on the
    target with Attempt 1 open rather than closing it."""

    indeterminate = {**LENIENT, "target": {"max_count": 99, "max_rate": "0.5"}}
    rig, scorer = _held_then_faulted_rig(tmp_path, parallel, indeterminate)
    fault = await _fault_suspended(rig)

    scorer.armed = False
    resumed = await rig.resume()

    payload = assert_suspended(rig, resumed, "target_unstable", "target", "threshold")
    assert payload["evidence"]["window_start"] == fault
    assert payload["evidence"]["crossing"]["count"] == len(ROWS)
    assert sorted(_repairs_of(rig, "m-r01-a01")) == sorted(ROWS)
    dispatched = [
        (p["measurement_id"], p["count"])
        for p in payloads_of(rig, "repairs_dispatched")
        if p["measurement_id"] == "m-r01-a01"
    ]
    assert dispatched == [("m-r01-a01", len(ROWS))]
    assert "r01-a01" not in _closed(rig)
    assert "look_not_started" not in rig.events()
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("parallel", [1, 2])
async def test_a_held_attempts_grant_is_spent_once_across_a_second_suspension(
    tmp_path: Path, parallel: int
) -> None:
    """a27 spec §3.1 (Review Focus 4): the target is healthy on the first
    resume but Attempt 2's scorer fault recurs, so a second suspension follows.
    A second slot (parallel 2) lets Attempt 1's repairs start before it, and the
    second suspension owes them nothing more; with one slot Attempt 1 waits
    behind Attempt 2's repair, and its unspent grant survives into the next
    window. Either way each pair gets exactly one repair try."""

    rig, scorer = _held_then_faulted_rig(tmp_path, parallel)
    await _fault_suspended(rig)

    rig.adapter.dispositions.clear()
    second = await rig.resume()

    assert_suspended(rig, second, "scorer_metric_missing", "scorer", "fault_rows")
    started = bool(_repairs_of(rig, "m-r01-a01"))
    assert started == (parallel == 2)
    assert rig.projection(verify=True)["verification"]["mismatches"] == []

    scorer.armed = False
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    for case, sample in ROWS:
        starts = _starts(rig, "m-r01-a01", case, sample)
        assert [s.get("repair") for s in starts] == [None, True]
    dispatched = [
        (p["measurement_id"], p["count"])
        for p in payloads_of(rig, "repairs_dispatched")
        if p["measurement_id"] == "m-r01-a01"
    ]
    assert dispatched == [("m-r01-a01", len(ROWS))]
    assert _closed(rig)["r01-a01"]["outcome"] != "insufficient_evidence"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("parallel", [1, 2])
async def test_a_held_attempt_is_repaired_after_a_suspension_following_a_version_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    """Codex branch review round 1 (`held-repair-version-transition`): the
    first fault suspension is written under `0.3.0a26`, which owes no held
    repair, so it names nothing. The resume rebases to the current version
    and Attempt 2's fault recurs: that a27 suspension names Attempt 1's
    exhausted pairs -- the exempt one does not mask it -- and the healthy
    resume repairs each pair once."""

    rig, scorer = _held_then_faulted_rig(tmp_path, parallel)
    with monkeypatch.context() as old:
        old.setattr(campaign_module, "resolve_package_version", lambda: "0.3.0a26")
        await _fault_suspended(rig)
    assert rig.records()[0]["payload"]["producing_version"] == "0.3.0a26"

    rig.adapter.dispositions.clear()
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume()
    assert refused.value.effects == ("rebase",)  # the version change alone
    second = await rig.resume(authorizations=[_accept_scorer_change(refused.value, "default")])

    assert_suspended(rig, second, "scorer_metric_missing", "scorer", "fault_rows")
    assert _rebased_seq(rig) < seq_of(rig, "campaign_suspended")[-1]
    assert _repairs_of(rig, "m-r01-a01") == []
    assert rig.projection(verify=True)["verification"]["mismatches"] == []

    scorer.armed = False
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    for case, sample in ROWS:
        starts = _starts(rig, "m-r01-a01", case, sample)
        assert [s.get("repair") for s in starts] == [None, True]
    dispatched = [
        (p["measurement_id"], p["count"])
        for p in payloads_of(rig, "repairs_dispatched")
        if p["measurement_id"] == "m-r01-a01"
    ]
    assert dispatched == [("m-r01-a01", len(ROWS))]
    assert _closed(rig)["r01-a01"]["outcome"] != "insufficient_evidence"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


async def test_held_attempts_reactivate_one_slot_at_a_time_in_index_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#91 spec §3.2: three held Attempts, one slot: on the resume each one's
    repairs start only once the one before it is evaluated."""

    rig = _held_rig(tmp_path, 1, STRICT, attempts=3)
    for value in ("VALUE = 2\n", "VALUE = 3\n", "VALUE = 4\n"):
        _fail_candidate(rig, tmp_path, value)

    first = await rig.run()

    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    assert "attempt_closed" not in rig.events()
    assert rig.events().count("attempt_opened") == 3

    order: list[tuple[str, str]] = []
    original_append = AppendLog.append

    def append(
        self: AppendLog, run_id: str, event: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> int:
        sequence = original_append(self, run_id, event, payload, *a, **k)
        if event == "rollout_started" and payload.get("repair") is True:
            order.append(("repair", run_id))
        elif event == "comparison_look_evaluated":
            order.append(("look", f"m-{payload['attempt_id']}"))
        return sequence

    rig.adapter.dispositions.clear()
    monkeypatch.setattr(AppendLog, "append", append)
    resumed = await rig.resume()
    monkeypatch.undo()

    assert resumed.terminal == "finished"
    assert order == [
        *[("repair", "m-r01-a01")] * len(ROWS),
        ("look", "m-r01-a01"),
        *[("repair", "m-r01-a02")] * len(ROWS),
        ("look", "m-r01-a02"),
        *[("repair", "m-r01-a03")] * len(ROWS),
        ("look", "m-r01-a03"),
    ]
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


def _mirror_views(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any, list[Any]]]:
    """#114: each executor view, beside the read-only `inspect_view` and the
    status report's `actions` read off disk at the same moment."""

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import inspect_view

    mirrors: list[tuple[Any, Any, list[Any]]] = []
    original_view = campaign_module._Campaign.view

    def view(self: Any) -> Any:
        live = original_view(self)
        actions = inspect_campaign(self.run_root).to_payload()["actions"]
        mirrors.append((live, inspect_view(self.run_root), actions))
        return live

    monkeypatch.setattr(campaign_module._Campaign, "view", view)
    return mirrors


def _planned(view: Any, policy: Any) -> list[tuple[str, Any]]:
    from evolution.tree.campaign_view import next_actions

    return [
        # PDR-0028: family-14 status uses final names for the existing planner classes.
        (
            {
                "BindCohortMember": "BindFinalMember",
                "RunCohortStage": "RunFinalStage",
                "CompleteCohortMember": "CompleteFinalMember",
            }.get(type(action).__name__, type(action).__name__),
            getattr(action, "attempt_id", None),
        )
        for action in next_actions(view, policy)
    ]


def _assert_report_plans_as_the_executor(rig: Rig, mirrors: list[Any]) -> None:
    for live, read_only, actions in mirrors:
        assert read_only.lacking == live.lacking
        assert read_only.rebound_held == live.rebound_held
        planned = _planned(live, rig.policy)
        assert _planned(read_only, rig.policy) == planned
        assert [(a["kind"], a["fields"].get("attempt_id")) for a in actions] == planned


@pytest.mark.parametrize("parallel", [1, 2])
async def test_the_status_report_holds_a_held_attempt_as_the_executor_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    """#114: the read-only `inspect_view` names the held Attempts the executor
    names (`lacking`, over the same `rows_held`), so at every step of the run
    and of the healthy resume the status report's `actions` are the executor's
    -- the held Attempt has no step, and the round barrier follows the last
    open Attempt; once its repair is owed it reactivates."""

    rig = _held_rig(tmp_path, parallel, STRICT)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")
    mirrors = _mirror_views(monkeypatch)

    first = await rig.run()

    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    _assert_report_plans_as_the_executor(rig, mirrors)
    barrier = [
        actions
        for live, _ro, actions in mirrors
        if "r01-a01" in live.lacking and not live.repairs.get("m-r01-a01")
    ]
    # ADR-0032: the family-14 final-stage barrier binds finalists before selection.
    assert barrier and [a["kind"] for a in barrier[-1]] == ["BindFinalists"]
    # Suspended at the barrier: the owed repair reactivates the held Attempt.
    assert [(a["kind"], a["fields"]["attempt_id"]) for a in rig.projection()["actions"]] == [
        ("RunAttemptStage", "r01-a01")
    ]

    mirrors.clear()
    rig.adapter.dispositions.clear()
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    assert mirrors and any("r01-a01" in live.lacking for live, _ro, _a in mirrors)
    _assert_report_plans_as_the_executor(rig, mirrors)


async def test_the_status_report_admits_rebound_held_attempts_as_the_executor_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#114, ruling R17: after an I2 `continue` on held Attempts, the read-only
    view names the re-bound held Attempts (`rebound_held`) as the executor
    does, so the report admits them within `parallel_attempts`, in index order."""

    from tests.test_tree_campaign_condition_change import ResettledAdapter, accept

    rig = _held_rig(tmp_path, 1, STRICT, attempts=3)
    for value in ("VALUE = 2\n", "VALUE = 3\n", "VALUE = 4\n"):
        _fail_candidate(rig, tmp_path, value)
    await rig.run()
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=ResettledAdapter())

    mirrors = _mirror_views(monkeypatch)
    resumed = await rig.resume(adapter=ResettledAdapter(), authorizations=[accept(refused.value)])

    assert resumed.terminal == "finished"
    assert any(live.rebound_held for live, _ro, _a in mirrors)
    _assert_report_plans_as_the_executor(rig, mirrors)


async def test_a_sequential_held_attempt_is_decided_before_the_next_freezes_its_input(
    tmp_path: Path,
) -> None:
    """#91 spec §3.1, sequential feedback: the outage suspends right after
    Attempt 1's stage; on the resume Attempt 2 freezes its input only after
    Attempt 1 closed, and its feedback includes it."""

    rig = Rig(
        tmp_path,
        campaign_policy=v8(
            indeterminate=STRICT,
            attempts_per_round=2,
            parallel_attempts=1,
            attempt_feedback="sequential",
        ),
    )
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")

    first = await rig.run()

    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    frozen = [p["attempt_id"] for p in payloads_of(rig, "attempt_input_frozen")]
    assert frozen == ["r01-a01"]
    assert "attempt_closed" not in rig.events()
    assert "attempt_opened" in rig.events() and rig.events().count("attempt_opened") == 1
    assert rig.projection(verify=True)["verification"]["mismatches"] == []

    rig.adapter.dispositions.clear()
    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    records = rig.records()
    assert _index_of(records, "attempt_closed", "r01-a01") < _index_of(
        records, "attempt_input_frozen", "r01-a02"
    )
    second = records[_index_of(records, "attempt_input_frozen", "r01-a02")]["payload"]
    assert [row["attempt_id"] for row in second["feedback"]["attempts"]] == ["r01-a01"]
    assert _closed(rig)["r01-a01"]["outcome"] != "insufficient_evidence"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


@pytest.mark.parametrize("planner_bug", ["not_held", "owes_a_repair"])
async def test_the_round_barrier_refuses_an_attempt_it_must_not_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planner_bug: str
) -> None:
    """Ruling R3: an open Attempt that is not held, or a held one still owed a
    repair, at `DecideRound` is a planner bug; the run stops loudly rather than
    close a live Attempt or plan the same barrier forever."""

    rig = _held_rig(tmp_path, 1, LENIENT)
    _fail_candidate(rig, tmp_path, "VALUE = 2\n")
    original = campaign_module._Campaign.decide_round

    async def decide_round(self: Any, round_number: int) -> None:
        if planner_bug == "not_held":
            monkeypatch.setattr(self, "lacking_attempts", lambda: frozenset())
        else:
            monkeypatch.setattr(self, "repair_rows", lambda mid: frozenset({("case-01", "01")}))
        await original(self, round_number)

    monkeypatch.setattr(campaign_module._Campaign, "decide_round", decide_round)
    message = "open and not held" if planner_bug == "not_held" else "owing a repair on m-r01-a01"

    with pytest.raises(CampaignInvariantViolated, match=message):
        await rig.run()

    assert "r01-a01" not in _closed(rig)
    assert "selection_decided" not in rig.events()


# -- causes 6-9: stage records -------------------------------------------------


async def test_an_unavailable_probe_suspends_and_resume_rechecks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.probe_fakes import ProbeHTTPServer

    health = {"ready": False}

    async def check(*_args: Any, **_kwargs: Any) -> bool:
        return health["ready"]

    monkeypatch.setattr(campaign_module, "check_probe_health", check)
    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    with ProbeHTTPServer() as server:
        probe = probe_config(server.base_url)

        result = await rig.run(probe=probe)

        payload = assert_suspended(rig, result, "probe_unavailable", "campaign", "stage_record")
        checked = [r for r in rig.records() if r["event"] == "probe_checked"]
        assert checked[0]["payload"]["status"] == "unavailable"
        assert payload["evidence"]["seq"] == checked[0]["sequence"]
        assert "reflection_started" not in rig.events()

        health["ready"] = True
        resumed = await rig.resume(probe=probe)

    assert resumed.terminal == "finished"
    statuses = [p["status"] for p in payloads_of(rig, "probe_checked")]
    assert statuses == ["unavailable", "ready"]
    assert "reflection_started" in rig.events()


async def test_an_unresponsive_editor_suspends_and_the_rounds_other_slots_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign_editor, "EDITOR_GRACE_SECONDS", 0.05)
    first, second = "r01-a01", "r01-a02"
    rig = Rig(
        tmp_path,
        campaign_policy=v8(
            indeterminate=LENIENT,
            attempts_per_round=2,
            parallel_attempts=1,
            editor_timeout_seconds=0.05,
        ),
        editor=ScriptedEditor(
            {
                first: EditorScript("swallow_cancel", seconds=3600.0),
                second: EditorScript("edit", writes={"skill.py": b"VALUE = 2\n"}),
            }
        ),
    )

    result = await rig.run()

    payload = assert_suspended(rig, result, "editor_unresponsive", "editor", "stage_record")
    assert result.quiescent is False
    failed = [r for r in rig.records() if r["event"] == "editor_failed"]
    assert failed[0]["payload"]["error_class"] == "unresponsive"
    assert payload["evidence"]["seq"] == failed[0]["sequence"]
    assert payload["evidence"]["operation_id"] == failed[0]["operation_id"]
    closed = payloads_of(rig, "attempt_closed")
    assert [(c["attempt_id"], c["outcome"], c["reason"]) for c in closed] == [
        (first, "abandoned", "editor_unresponsive")
    ]

    resumed = await rig.resume()

    assert resumed.terminal == "finished"
    assert resumed.finished is not None and resumed.finished["outcome"] == "completed"
    closed = payloads_of(rig, "attempt_closed")
    assert [c["attempt_id"] for c in closed] == [first, second]
    assert closed[0]["outcome"] == "abandoned"


async def test_a_crash_before_the_suspension_suspends_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable `editor_failed(unresponsive)` with no suspension yet is still owed one."""

    monkeypatch.setattr(campaign_editor, "EDITOR_GRACE_SECONDS", 0.05)
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT, editor_timeout_seconds=0.05),
        editor=ScriptedEditor({"r01-a01": EditorScript("swallow_cancel", seconds=3600.0)}),
    )

    def crash() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_editor_finished": crash})
    assert "campaign_suspended" not in rig.events()

    result = await rig.resume()
    assert_suspended(rig, result, "editor_unresponsive", "editor", "stage_record")

    # The suspension resolved it: a further resume does not stop on it again.
    again = await rig.resume()
    assert again.terminal == "finished"
    assert rig.events().count("campaign_suspended") == 1


# -- Causes outside the table --------------------------------------------------


async def test_an_attestation_refusal_suspends_as_target_changed(tmp_path: Path) -> None:
    from tests.test_tree_campaign_fixtures import LyingAdapter

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    seed = scan_tree(rig.seed_root, ARTIFACT_POLICY).candidate_digest
    adapter = LyingAdapter(seed, lie_about_the_seed=True)
    rig.adapter = adapter

    result = await rig.run()

    payload = assert_suspended(rig, result, "target_changed", "target", "stage_record")
    # The latest record binding the refused measurement (`measurement_bound`
    # follows `baseline_bound` for the seed champion's).
    bound = [
        r
        for r in rig.records()
        if r["event"] in ("baseline_bound", "measurement_bound")
        and r["payload"]["measurement_id"] == measurement_id_for(None)
    ]
    assert payload["evidence"]["seq"] == bound[-1]["sequence"]
    assert payload["evidence"]["operation_id"] == bound[-1]["operation_id"]
    assert "stage_opened" not in rig.events()

    # A target that lies again refuses again, on the same record; from
    # 0.3.0a27 each refusal cites its own prefix, through its own release (#94).
    again = await rig.resume()
    assert_suspended(rig, again, "target_changed", "target", "stage_record")
    first, second = (p["evidence"] for p in payloads_of(rig, "campaign_suspended"))
    assert {k: v for k, v in second.items() if k != "prefix"} == {
        k: v for k, v in first.items() if k != "prefix"
    }
    assert first["prefix"] != second["prefix"]

    # Honest about the seed now, the baseline measures; the adapter still lies
    # about every Candidate, so the Attempt's measurement is refused next, on
    # the record that binds it.
    adapter.lie_about_the_seed = False
    third = await rig.resume()
    assert "baseline_complete" in rig.events()
    last = assert_suspended(rig, third, "target_changed", "target", "stage_record")
    attempt_bound = [
        r
        for r in rig.records()
        if r["event"] == "measurement_bound" and r["payload"]["measurement_id"] == "m-r01-a01"
    ]
    assert last["evidence"]["seq"] == attempt_bound[0]["sequence"]


async def test_a_probe_setup_failure_suspends_and_resume_retries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.probe_fakes import ProbeHTTPServer

    real_start = campaign_module.ProbeMediator.start
    broken = {"on": True}

    async def start(self: Any) -> Any:
        if broken["on"]:
            raise OSError("listener could not start")
        return await real_start(self)

    monkeypatch.setattr(campaign_module.ProbeMediator, "start", start)
    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    with ProbeHTTPServer() as server:
        probe = probe_config(server.base_url)

        result = await rig.run(probe=probe)

        payload = assert_suspended(rig, result, "probe_unavailable", "campaign", "stage_record")
        checked = [r for r in rig.records() if r["event"] == "probe_checked"]
        assert checked[0]["payload"]["status"] == "ready"
        assert payload["evidence"]["seq"] == checked[0]["sequence"]
        assert "reflection_started" not in rig.events()

        broken["on"] = False
        resumed = await rig.resume(probe=probe)

    assert resumed.terminal == "finished"
    assert [p["status"] for p in payloads_of(rig, "probe_checked")] == ["ready", "ready"]
    assert "reflection_started" in rig.events()


# -- the authorization ruling ------------------------------------------------


# -- 11: family 6 twins -------------------------------------------------------


# -- I2 Task 9: a condition change restarts only its owner's window -----------
#
# I2 spec §6.5, ADR-0019 as amended: a change of `target` or
# `materialization_strategy` restarts the `target` window, of `environment`
# the `environment` window, of `scorer` the `scorer` window; `rollout` never
# restarts on a change. The restart point is the block's `identity_rebased`,
# and the restarted window counts only what was measured under the new
# condition: a try of a measurement frozen by the change says nothing about
# the new provider even when it is accounted after the change.


def test_each_changed_field_restarts_its_owners_window() -> None:
    from evolution.tree.campaign_epochs import restarted_owners

    assert restarted_owners(["target"]) == {"target"}
    assert restarted_owners(["materialization_strategy"]) == {"target"}
    assert restarted_owners(["environment", "target"]) == {"environment", "target"}
    assert restarted_owners(["scorer", "corpus_digest"]) == {"scorer"}
    assert restarted_owners(["policy_digest", "corpus_digest", "metric_plan_digest"]) == set()
    # The journaled `changed_fields` are dotted paths into the identity record.
    assert restarted_owners(
        ["identity.target.settings_hash", "identity.environment.digest", "producing_version"]
    ) == {"target", "environment"}


class _FailingFirstCalls(ScriptedEnvironment):
    """The scripted Environment (same identity) failing its first `failures` samples."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def next(self, case: Any, history: Any) -> Any:
        if not history.turns and self.failures > 0:
            self.failures -= 1
            raise RolloutEnvironmentError("scripted environment failure")
        return await super().next(case, history)


def _window_findings(rig: Rig) -> list[dict[str, Any]]:
    """Every audit finding over the run, the windows' accounting included.

    (Before Task 10 this kept only the accounting and the suspensions: the
    audit then flagged the looks and the selection after any `continue`.)
    """

    return list(verify_evidence(rig.run_root, corpus=rig.cases)["mismatches"])


def _rebased_seq(rig: Rig) -> int:
    return seq_of(rig, "identity_rebased")[-1]


class _Record:
    """The three things `_window_records` reads of an envelope."""

    def __init__(self, sequence: int, event: str, **payload: Any) -> None:
        self.sequence, self.event, self.payload = sequence, event, payload

    def to_payload(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "event": self.event, "payload": self.payload}


def test_the_audits_window_starts_only_at_a_suspension_or_the_owners_own_change() -> None:
    from evolution.tree.campaign_projections import _PreconditionFailed, _window_records

    records: Any = (
        _Record(1, "baseline_bound", measurement_id="m-r01-champion"),
        _Record(2, "stage_opened", measurement_id="m-r01-champion", sample_count=4),
        _Record(3, "identity_rebased", changed_fields=["identity.environment.digest"]),
        _Record(4, "identity_rebased", changed_fields=["identity.target.settings_hash"]),
        _Record(5, "measurement_rebound", measurement_id="m-r01-a01@e2"),
        _Record(6, "indeterminate_observed", measurement_id="m-r01-champion", owner="target"),
        _Record(7, "indeterminate_observed", measurement_id="m-r01-a01@e2", owner="target"),
    )

    # The target's own change: only what the new condition measured.
    window = _window_records(records, 4, "target")
    assert [record.sequence for record in window] == [5, 7]
    # A change of another field is no window start for this owner.
    with pytest.raises(_PreconditionFailed):
        _window_records(records, 3, "target")
    assert [record.sequence for record in _window_records(records, 3, "environment")] == [
        4,
        5,
        7,
    ]
    with pytest.raises(_PreconditionFailed):
        _window_records(records, 2, "target")


def test_the_audits_window_counts_a_carried_stages_slots() -> None:
    """#109 (a28 spec §3.8): `stage_redispatched` adds its `count` to the
    window it is written in and marks its measurement as opened there, so a
    later `measurement_rebound` in that window takes `released` out of it, as
    the lifecycle does. In a window an in-place change restarted, it counts
    only for a measurement bound in that window, as `repairs_dispatched` does."""

    from evolution.tree.campaign_projections import _window_records, _window_totals

    records: Any = (
        _Record(1, "campaign_suspended"),
        _Record(2, "stage_redispatched", measurement_id="m-r01-a01", count=2),
        _Record(
            3,
            "indeterminate_observed",
            measurement_id="m-r01-a01",
            owner="target",
            case_id="c",
            sample_id="s",
            retry_ordinal=0,
            retry_follows=False,
        ),
        _Record(4, "measurement_rebound", from_measurement_id="m-r01-a01", released=2),
    )
    assert _window_totals(records[1:3], "target") == (1, 2)
    assert _window_totals(records[1:], "target") == (1, 0)
    # Without the record, the source's slots were never this window's.
    assert _window_totals((records[2], records[3]), "target") == (1, 0)

    changed: Any = (
        _Record(1, "identity_rebased", changed_fields=["identity.target.settings_hash"]),
        _Record(2, "stage_redispatched", measurement_id="m-r01-champion", count=3),
        _Record(
            3,
            "measurement_rebound",
            measurement_id="m-r01-a01@e1",
            from_measurement_id="m-r01-a01",
            released=0,
        ),
        _Record(4, "stage_redispatched", measurement_id="m-r01-a01@e1", count=2),
    )
    window = _window_records(changed, 1, "target")
    assert [record.sequence for record in window] == [3, 4]
    assert _window_totals(window, "target") == (0, 2)


async def _suspended_then_changed(tmp_path: Path, *, schema: str = SCHEMA_V15) -> tuple[Rig, Any]:
    """Final fix wave: a target threshold suspends round 1, the operator resumes
    under moved target settings (`continue`), and the new target crosses again
    in round 2."""

    from tests.test_tree_campaign_condition_change import SettingsAdapter, accept

    target_only = {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}}
    rig = Rig(
        tmp_path,
        campaign_policy=v8(rounds=2, indeterminate=target_only),
        adapter=SettingsAdapter(),
        schema=schema,
    )
    failing = edited_key(tmp_path, "case-01", "01")
    rig.adapter.dispositions[failing] = "target_failed"
    first = await rig.run()
    assert_suspended(rig, first, "target_unstable", "target", "threshold")
    with pytest.raises(IdentityChanged) as refused:
        await rig.resume(adapter=SettingsAdapter("settings-v2"))
    moved = SettingsAdapter("settings-v2")
    # Round 2's Attempt (`edits` writes `VALUE = 3`) fails under the new target.
    moved.dispositions[edited_key(tmp_path, "case-01", "01", "VALUE = 3\n")] = "target_failed"
    result = await rig.resume(adapter=moved, authorizations=[accept(refused.value)])
    return rig, result


async def test_a_change_after_a_suspension_restarts_the_owner_window_at_the_change(
    tmp_path: Path,
) -> None:
    """Suspension -> change order: the restarted window starts at the change's
    `identity_rebased`, after the suspension, and the next crossing names it."""

    rig, result = await _suspended_then_changed(tmp_path)

    payload = assert_suspended(rig, result, "target_unstable", "target", "threshold")
    first_suspension, _second = seq_of(rig, "campaign_suspended")
    rebased = _rebased_seq(rig)
    assert first_suspension < rebased
    assert payload["evidence"]["window_start"] == rebased
    assert _window_findings(rig) == []


class _SeedOutOfRangeScorer(PromotingScorer):
    """What a pre-fix producer scored: the seed's `case-01` primary is `2.0`,
    every edited Candidate's is valid."""

    def value_for(self, candidate_digest: str, case_id: str) -> float:
        if candidate_digest == self.seed_digest and case_id == "case-01":
            return 2.0
        return super().value_for(candidate_digest, case_id)


# -- Rows written before the fix (ADR-0019, 2026-10-03) ------------------------


async def _pre_fix_predecessor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """A run finished by 0.3.0a25 whose champion measurement -- its seed
    baseline: the gate refused the `2.0` and the Attempt closed
    `infrastructure_failed` -- holds a `2.0` for every `case-01` row, and
    an exhausted `case-02/01` that no successor carries: measured afresh
    there, it gives the successor's first round a failing case to work on."""

    from evolution.tree.campaign_projections import champion_measurement

    first = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT),
        run_name="first",
    )
    first.scorer = _SeedOutOfRangeScorer(
        scan_tree(first.seed_root, ARTIFACT_POLICY).candidate_digest
    )
    first.adapter.dispositions[seed_key(first, "case-02", "01")] = "target_failed"
    with monkeypatch.context() as old:
        old.setattr(campaign_module, "resolve_package_version", lambda: "0.3.0a25")
        result = await first.run()
    assert result.finished is not None
    assert champion_measurement(first.snapshot())[0] == measurement_id_for(None)
    return first


def _inheriting_successor(first: Rig, tmp_path: Path) -> Rig:
    """A successor seeded from `first`'s champion, under a valid scorer."""

    from tests.test_tree_campaign import champion_path, successor_editor

    second = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=LENIENT),
        editor=successor_editor(),
        run_name="second",
    )
    second.seed_root = champion_path(first)
    second.scorer = PromotingScorer(scan_tree(second.seed_root, ARTIFACT_POLICY).candidate_digest)
    return second


def _carried(rig: Rig) -> list[dict[str, Any]]:
    return [
        record["payload"]
        for record in rig.measurement_records(measurement_id_for(None))
        if record["event"] == "sample_imported"
    ]


async def test_an_out_of_range_champion_row_is_not_carried_and_is_measured_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 (amended 2026-10-03): from 0.3.0a26 the admission rule does not
    carry a row whose primary lies outside [0, 1], as it does not carry one
    lacking the primary. The declaration counts only the admitted rows, the
    `case-01` pairs are measured afresh, and the run neither suspends nor
    reports a finding."""

    first = await _pre_fix_predecessor(tmp_path, monkeypatch)
    second = _inheriting_successor(first, tmp_path)

    result = await second.run(linked_from=first.run_root, linked_baseline="inherit")

    declared = second.records()[0]["payload"]["identity"]["linked_from"]["baseline"]
    carried = _carried(second)
    assert carried and {row["case_id"] for row in carried} == {"case-02"}
    assert declared["imported"] == len(carried) < declared["scheduled"]
    fresh = {
        (record["payload"]["case_id"], record["payload"]["sample_id"])
        for record in second.measurement_records(measurement_id_for(None))
        if record["event"] == "rollout_started"
    }
    assert len(fresh) == declared["scheduled"] - declared["imported"]
    assert sum(case_id == "case-01" for case_id, _sample in fresh) == len(fresh) - 1
    assert result.terminal == "finished"
    assert "campaign_suspended" not in second.events()
    assert second.projection(verify=True)["verification"]["mismatches"] == []


# -- a27 spec §4 (#94): `target_changed` cites its refusal prefix -------------


def _refusal_prefix_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the running producer exempt from the refusal prefix, as `0.3.0a26` is."""

    from evolution import __version__
    from evolution.tree import campaign_schema

    monkeypatch.setattr(
        campaign_schema,
        "REFUSAL_PREFIX_EXEMPT_VERSIONS",
        campaign_schema.REFUSAL_PREFIX_EXEMPT_VERSIONS | {__version__},
    )


class _RefusesAtMaterialize(FakeAdapter):
    """An adapter that refuses inside `materialize` itself, as the HTTP adapter
    does for a dependency digest it never preflighted (`attestation_mismatch`)
    or an unsupported strategy (`strategy_unsupported`)."""

    def __init__(self, code: str, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.code = code

    async def materialize(self, candidate: Any, strategy: str, instance_id: str) -> FakeInstance:
        from evolution.tree.execution.interaction import AttestationError

        raise AttestationError("refused before the wire", self.code)  # type: ignore[arg-type]


def _failed_materialization_end(rig: Rig, measurement_id: str, locator: str) -> dict[str, Any]:
    """The record a `prefix` ends at, held to be the journal's last-cited record."""

    raw = (rig.run_root / "measurements" / measurement_id / "events.jsonl").read_bytes()
    lines = raw.splitlines(keepends=True)
    ends = [sum(len(line) for line in lines[: index + 1]) for index in range(len(lines))]
    head = f"measurements/{measurement_id}/events.jsonl#bytes=0:"
    assert locator.startswith(head)
    return dict(json.loads(lines[ends.index(int(locator[len(head) :]))]))


async def test_a_refusal_inside_the_first_materialize_suspends_with_its_prefix(
    tmp_path: Path,
) -> None:
    """The same refusal while a stage opens: nothing is open, nothing attested."""

    rig = Rig(tmp_path, campaign_policy=v8(indeterminate=LENIENT))
    rig.adapter = _RefusesAtMaterialize("attestation_mismatch")

    result = await rig.run()

    payload = assert_suspended(rig, result, "target_changed", "target", "stage_record")
    assert "stage_opened" not in rig.events()
    last = _failed_materialization_end(rig, measurement_id_for(None), payload["evidence"]["prefix"])
    assert last["event"] == "target_materialize_failed"
    assert rig.projection(verify=True)["verification"]["mismatches"] == []


# -- #120: a forced stop's cancelled rows hold the Attempt ---------------------


async def test_the_audit_reads_each_measurements_repair_tries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#95: the repair ordinals of a measurement are derived once per audit,
    not rescanned from its whole journal for every indeterminate try."""

    from evolution.tree import campaign_projections

    unbounded = {
        owner: {"max_count": 10**6, "max_rate": "1"}
        for owner in ("target", "environment", "scorer", "rollout")
    }
    rig = Rig(
        tmp_path,
        campaign_policy=v8(indeterminate=unbounded, sample_retries=1),
        cases=corpus(6),
    )
    rig.adapter = TrippingAdapter(Trip(armed=True), "target_failed")
    await rig.run()
    observed = payloads_of(rig, "indeterminate_observed")
    measurements = {payload["measurement_id"] for payload in observed}
    assert "threshold_reached" not in rig.events()
    assert len(observed) > 2 * len(measurements)
    calls: list[int] = []
    original = campaign_projections._repair_ordinals

    def counted(records: Any) -> Any:
        calls.append(len(records))
        return original(records)

    monkeypatch.setattr(campaign_projections, "_repair_ordinals", counted)

    assert verify_evidence(rig.run_root)["mismatches"] == []
    assert len(calls) <= len(measurements)
