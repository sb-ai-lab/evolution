"""Resume verdict (spec 2026-09-24 §2.1): one rule for admission and status."""
from dataclasses import replace

import pytest

from evolution_lab._suspension import FINAL_SUSPENSIONS
from evolution_lab.owner.models import (
    CleanupFacts, ExecutorIdentity, OperationStatus, OwnerError, ProcessFacts,
    ResumeVerdict, StartFacts, TargetRef, TargetRequirement,
)
from evolution_lab.owner.resume_rules import (
    core_suspended, durable_verdict, raise_for, raise_for_target, resume_verdict, status_field,
    target_requirement, used_targets,
)

D = "d" * 64
SUSPENDED = {"suspended": True, "complete": False, "suspensions": []}


def _status(control="finished", cleanup=("confirmed", True), results="present", kind="campaign"):
    executor = ExecutorIdentity(environment_digest="0" * 64, lab_wheel_sha256="1" * 64,
        core_wheel_sha256="2" * 64, module_hashes={"m": "2" * 64}, kind=f"lab-{kind}-worker/1")
    return OperationStatus("op1", "gen1", D, TargetRef("t1", "gen1"), control,
        ProcessFacts(True, 1, True, False), CleanupFacts(*cleanup), None, None, results,
        executor, kind=kind)


@pytest.mark.parametrize("kw,expected", [
    (dict(kind="evaluation"), ("not_applicable", "invalid_argument")),
    (dict(results="deleted"), ("refused", "results_deleted")),
    (dict(results="deleting"), ("refused", "results_deleted")),
    (dict(control="running", cleanup=("not_needed", True)), ("busy", "resource_busy")),
    (dict(control="cleanup_required", cleanup=("unconfirmed", None)), ("cleanup_first", "cleanup_required")),
    (dict(control="orphaned", cleanup=("pending", None)), ("cleanup_first", "cleanup_required")),
    (dict(control="released", cleanup=("manual", None)), ("refused", "cleanup_unconfirmed")),
    (dict(control="released", cleanup=("pending", None)), ("cleanup_first", "cleanup_unconfirmed")),
    (dict(control="released", cleanup=("unconfirmed", None)), ("cleanup_first", "cleanup_unconfirmed")),
    (dict(results="not_created"), ("refused", "campaign_unreadable")),
    (dict(), ("ready", None)),
    (dict(control="released", cleanup=("not_needed", True)), ("ready", None)),
])
def test_verdict_rows(kw, expected):
    assert resume_verdict(_status(**kw), SUSPENDED) == ResumeVerdict(*expected)


@pytest.mark.parametrize("core", [None, {}, {"suspended": False}, {"suspended": True, "complete": True}])
def test_not_suspended_is_row_seven_and_field_null(core):
    assert resume_verdict(_status(), core) == ResumeVerdict("not_applicable", "campaign_not_suspended")
    assert status_field(_status(), core) is None
    assert core_suspended(core) is False


def test_field_is_null_for_evaluation_and_verdict_otherwise():
    assert status_field(_status(kind="evaluation"), SUSPENDED) is None
    assert status_field(_status(control="released", cleanup=("manual", None)), SUSPENDED) == \
        ResumeVerdict("refused", "cleanup_unconfirmed")


def test_raise_for_uses_admission_codes_and_stages():
    raise_for(None)
    raise_for(ResumeVerdict("ready", None))
    for verdict, stage in ((ResumeVerdict("busy", "resource_busy"), "resume"),
                           (ResumeVerdict("cleanup_first", "cleanup_unconfirmed"), "before_start"),
                           (ResumeVerdict("not_applicable", "invalid_argument"), "before_start")):
        with pytest.raises(OwnerError) as caught:
            raise_for(verdict)
        assert (caught.value.code, caught.value.stage) == (verdict.reason, stage)
    assert durable_verdict(_status()) is None


def test_model_payload_and_validation():
    status = replace(_status(), resume=ResumeVerdict("ready", None))
    payload = status.to_payload()
    assert payload["schema"] == "lab-operation-status/8"
    assert payload["resume"] == {"state": "ready", "reason": None, "target": None}
    assert _status().to_payload()["resume"] is None
    assert ResumeVerdict.from_payload({"state": "busy", "reason": "resource_busy", "target": None}) == \
        ResumeVerdict("busy", "resource_busy")
    for bad in (("ready", "busy"), ("busy", None), ("maybe", None), ("refused", "nope")):
        with pytest.raises(OwnerError):
            ResumeVerdict(*bad)
    with pytest.raises(OwnerError):
        replace(_status(), resume=ResumeVerdict("not_applicable", "invalid_argument"))
    with pytest.raises(ValueError):
        ResumeVerdict.from_payload({"state": "ready", "reason": None, "target": None, "extra": 1})


@pytest.mark.parametrize("state,reason", [
    ("cleanup_first", "results_deleted"), ("busy", "cleanup_required"),
    ("refused", "resource_busy"), ("refused", "campaign_not_suspended"),
    ("not_applicable", "cleanup_required"),
])
def test_verdict_rejects_pairs_outside_the_table(state, reason):
    with pytest.raises(OwnerError):
        ResumeVerdict(state, reason)


def _row(seq, reason, owner="target", evidence="threshold"):
    return {"seq": seq, "reason": reason, "owner": owner, "evidence_kind": evidence}


UNSTABLE = {"suspended": True, "complete": False, "suspensions": [_row(3, "target_unstable")]}


def _with_starts(status, *targets):
    """Starts 1..n on the given targets; the last one mirrors the top level (ADR-0015)."""
    starts = tuple(StartFacts(i, f"scope{i}", "gen1", TargetRef(t, "gen1"), status.process,
                              status.target_cleanup, None, "positive")
                   for i, t in enumerate(targets, 1))
    return replace(status, target_ref=TargetRef(targets[-1], "gen1"), starts=starts)


def test_unstable_target_requires_a_target_the_campaign_never_used():
    status = _with_starts(_status(), "t1", "t2", "t1")
    requirement = target_requirement(status, UNSTABLE)
    assert requirement == TargetRequirement(
        "fresh_instance", "target_unstable_fresh_required", (TargetRef("t1", "gen1"), TargetRef("t2", "gen1")))
    assert used_targets(status) == (TargetRef("t1", "gen1"), TargetRef("t2", "gen1"))
    verdict = resume_verdict(status, UNSTABLE)
    assert (verdict.state, verdict.reason, verdict.target) == ("ready", None, requirement)
    assert verdict.to_payload() == {"state": "ready", "reason": None, "target": {
        "requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
        "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "gen1"},
                     {"schema": "lab-target-ref/1", "id": "t2", "owner_generation": "gen1"}]}}


def test_without_starts_the_top_level_target_is_excluded():
    assert target_requirement(_status(), UNSTABLE).excluded == (TargetRef("t1", "gen1"),)


@pytest.mark.parametrize("reason,owner,evidence", [
    ("target_contract_violated", "target", "fault_rows"), ("probe_unavailable", "campaign", "stage_record")])
def test_other_reasons_have_no_requirement(reason, owner, evidence):
    core = {"suspended": True, "complete": False, "suspensions": [_row(3, reason, owner, evidence)]}
    assert target_requirement(_status(), core) is None
    assert resume_verdict(_status(), core).target is None


@pytest.mark.parametrize("reason,owner", [
    ("reflector_provider_failed", "reflector"), ("reflector_failing", "reflector"),
    ("editor_provider_failed", "editor"), ("editor_failing", "editor")])
def test_role_suspension_is_ready_without_a_target(reason, owner):
    """Spec 2026-09-30 §2.4: a role failure resumes as-is; the fresh-target rule is target_unstable only."""
    core = {"suspended": True, "complete": False, "suspensions": [_row(3, reason, owner, "stage_record")]}
    assert target_requirement(_status(), core) is None
    assert resume_verdict(_status(), core) == ResumeVerdict("ready", None)
    assert status_field(_status(), core).to_payload() == {"state": "ready", "reason": None, "target": None}


@pytest.mark.parametrize("reason", ["scorer_metric_out_of_range", "scorer_metric_missing"])
def test_scorer_suspension_resume_stays_ready(reason):
    """Spec 2026-10-03 §3: words only; resume after a scorer suspension is not refused and needs no fresh target."""
    core = {"suspended": True, "complete": False, "suspensions": [_row(3, reason, "scorer", "fault_rows")]}
    assert target_requirement(_status(), core) is None
    assert resume_verdict(_status(), core) == ResumeVerdict("ready", None)
    assert status_field(_status(), core).to_payload() == {"state": "ready", "reason": None, "target": None}


def test_only_the_latest_suspension_counts():
    core = {"suspended": True, "complete": False, "suspensions": [
        _row(3, "target_unstable"), _row(9, "probe_unavailable", "campaign", "stage_record")]}
    assert target_requirement(_status(), core) is None
    core["suspensions"].reverse()
    assert target_requirement(_status(), core) is not None


@pytest.mark.parametrize("core", [None, {}, {"suspended": False, "suspensions": [_row(1, "target_unstable")]},
                                  {"suspended": True, "complete": True, "suspensions": [_row(1, "target_unstable")]}])
def test_no_requirement_without_an_open_suspension(core):
    assert target_requirement(_status(), core) is None


@pytest.mark.parametrize("kw,state,has_target", [
    (dict(), "ready", True),
    (dict(control="cleanup_required", cleanup=("unconfirmed", None)), "cleanup_first", True),
    (dict(control="running", cleanup=("not_needed", True)), "busy", True),
    (dict(control="released", cleanup=("manual", None)), "refused", False),
    (dict(results="not_created"), "refused", False),
    (dict(kind="evaluation"), "not_applicable", False),
])
def test_target_rides_only_on_continuable_states(kw, state, has_target):
    verdict = resume_verdict(_status(**kw), UNSTABLE)
    assert verdict.state == state and (verdict.target is not None) is has_target
    field = status_field(_status(**kw), UNSTABLE)
    assert field is None or field == verdict


def test_raise_for_target():
    status = _with_starts(_status(), "t1")
    verdict = resume_verdict(status, UNSTABLE)
    raise_for_target(None, TargetRef("t1", "gen1"))
    raise_for_target(resume_verdict(status, SUSPENDED), TargetRef("t1", "gen1"))
    raise_for_target(verdict, TargetRef("t9", "gen1"))
    raise_for_target(verdict, TargetRef("t1", "gen2"))  # another generation's instance is another target
    with pytest.raises(OwnerError) as caught:
        raise_for_target(verdict, TargetRef("t1", "gen1"))
    assert (caught.value.code, caught.value.stage) == ("target_unstable_fresh_required", "before_start")


def test_target_requirement_model_is_closed():
    one = (TargetRef("t1", "gen1"),)
    for bad in (("stable_instance", "target_unstable_fresh_required", one),
                ("fresh_instance", "target_not_running", one),
                ("fresh_instance", "target_unstable_fresh_required", ()),
                ("fresh_instance", "target_unstable_fresh_required", one + one),
                ("fresh_instance", "target_unstable_fresh_required", ("t1",))):
        with pytest.raises(OwnerError):
            TargetRequirement(*bad)
    requirement = TargetRequirement("fresh_instance", "target_unstable_fresh_required", one)
    assert TargetRequirement.from_payload(requirement.to_payload()) == requirement
    for bad in ({**requirement.to_payload(), "extra": 1}, {"requirement": "fresh_instance"},
                {**requirement.to_payload(), "excluded": "t1"}):
        with pytest.raises((ValueError, OwnerError)):
            TargetRequirement.from_payload(bad)
    for state, reason in (("refused", "results_deleted"), ("not_applicable", "campaign_not_suspended")):
        with pytest.raises(OwnerError):
            ResumeVerdict(state, reason, requirement)
    payload = ResumeVerdict("ready", None, requirement).to_payload()
    assert ResumeVerdict.from_payload(payload) == ResumeVerdict("ready", None, requirement)
    with pytest.raises(ValueError):
        ResumeVerdict.from_payload({"state": "ready", "reason": None})  # /3 form is not /4


def test_continuation_names_the_ready_target():
    from evolution_lab.cli.output import continuation_line
    value = _SUSPENDED_UNSTABLE
    line = continuation_line(value, {"id": "t9", "owner_generation": "g1"})
    assert "lab resume" in line and "--target t9" in line and "--generation g1" in line
    assert "lab agent start" not in line
    # The current target is still stopped first, in the fresh-target wording, before the resume.
    stop = line.index("stop target ")
    assert "if it is still running (lab agent stop " in line and "--wait)" in line
    assert stop < line.index("lab resume")


def test_continuation_without_ready_target_is_unchanged():
    from evolution_lab.cli.output import continuation_line
    assert continuation_line(_SUSPENDED_UNSTABLE) == continuation_line(_SUSPENDED_UNSTABLE, None)
    assert "lab agent start" in continuation_line(_SUSPENDED_UNSTABLE)


_SUSPENDED_UNSTABLE = {"id": "OP", "control_state": "finished", "resume": {
    "state": "ready", "reason": None, "target": {
        "requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
        "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g1"}]}}}


def test_final_incomplete_resumes_ready_without_a_target_requirement():
    """Core family14 re-measures the missing final pairs; no fresh target, the same `lab resume`."""
    core = {"suspended": True, "complete": False, "suspensions": [_row(39, "final_incomplete", "scorer", "final_rows")]}
    assert target_requirement(_status(), core) is None
    assert resume_verdict(_status(), core) == ResumeVerdict("ready", None)
    assert status_field(_status(), core).to_payload() == {"state": "ready", "reason": None, "target": None}


@pytest.mark.parametrize("reason", sorted(FINAL_SUSPENSIONS))
def test_family_14_suspensions_need_no_fresh_target(reason):
    core = {"suspended": True, "complete": False, "suspensions": [_row(43, reason, None, "final_rows")]}
    assert target_requirement(_status(), core) is None


def test_champion_incomplete_resumes_ready_without_a_target_requirement():
    """Spec 2026-10-13 §2.3: the same `lab resume`, no fresh target (only `target_unstable` asks for one)."""
    core = {"suspended": True, "complete": False,
            "suspensions": [_row(41, "champion_incomplete", None, "champion_rows")]}
    assert target_requirement(_status(), core) is None
    assert resume_verdict(_status(), core) == ResumeVerdict("ready", None)
    assert status_field(_status(), core).to_payload() == {"state": "ready", "reason": None, "target": None}
