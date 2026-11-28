"""One resume admission rule (PDR-0015, ADR-0015; spec 2026-09-24 §2.1).

``Owner.resume`` uses this admission table. Legacy status shows its projection
only for Core-suspended campaigns; explicit refusal continuation extends
admission without changing that status field. Rows 1–6 read the durable store record,
row 7 core's projection. The refusal exception is based on the last Core
admission/execution start with saved findings, not the latest accepted start.  Row 8, the target requirement, reads core's latest
suspension and the operation's starts (spec 2026-09-29).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .._suspension import last_suspension
from .models import OperationStatus, OwnerError, ResumeVerdict, TargetRef, TargetRequirement

_DELETED = frozenset({"deleting", "deleted", "delete_failed"})
_BUSY = frozenset({"reserved", "running", "stopping"})
_CLEANUP = frozenset({"cleanup_required", "orphaned"})
_CLEAN = frozenset({"confirmed", "not_needed"})
_WITH_TARGET = frozenset({"ready", "cleanup_first", "busy"})


def durable_verdict(status: OperationStatus, *, refusal: bool = False) -> ResumeVerdict | None:
    """Rows 1–6 from the durable store record; ``None`` when they admit."""
    if status.kind != "campaign":
        return ResumeVerdict("not_applicable", "invalid_argument")
    if status.results_state in _DELETED:
        return ResumeVerdict("refused", "results_deleted")
    if status.control_state in _BUSY:
        return ResumeVerdict("busy", "resource_busy")
    if status.control_state in _CLEANUP:
        return ResumeVerdict("cleanup_first", "cleanup_required")
    if status.control_state == "released" and status.target_cleanup.state not in _CLEAN:
        # A manual release is final; pending/unconfirmed is a lost cleanup
        # fact that `cleanup` repairs (cleanup._repair_released_cleanup_fact).
        state = "refused" if status.target_cleanup.state == "manual" else "cleanup_first"
        return ResumeVerdict(state, "cleanup_unconfirmed")
    unborn = (refusal and status.results_state == "not_created"
              and status.target_cleanup.state in _CLEAN and status.target_cleanup.complete is True)
    if status.results_state != "present" and not unborn:
        return ResumeVerdict("refused", "campaign_unreadable")
    return None


def core_suspended(core: Any) -> bool:
    """Row 7: core's projection says suspended and not complete."""
    return isinstance(core, Mapping) and core.get("complete") is not True \
        and core.get("suspended") is True


def used_targets(status: OperationStatus) -> tuple[TargetRef, ...]:
    """Every target the campaign used: each start's, then the top level, first seen first."""
    seen: list[TargetRef] = []
    for ref in [start.target_ref for start in status.starts] + [status.target_ref]:
        if ref not in seen:
            seen.append(ref)
    return tuple(seen)


def target_requirement(status: OperationStatus, core: Any) -> TargetRequirement | None:
    """Row 8: after a ``target_unstable`` suspension only a target the campaign never used; every other reason
    (``champion_incomplete`` and family 14's pauses, ``FINAL_SUSPENSIONS``, included) has
    none."""
    if not core_suspended(core):
        return None
    latest = last_suspension(core)
    if latest is None or latest["reason"] != "target_unstable":
        return None
    return TargetRequirement("fresh_instance", "target_unstable_fresh_required", used_targets(status))


def resume_verdict(status: OperationStatus, core: Any, *, refusal: bool = False) -> ResumeVerdict:
    blocked = durable_verdict(status, refusal=refusal)
    if blocked is None and not core_suspended(core) and not refusal:
        return ResumeVerdict("not_applicable", "campaign_not_suspended")
    verdict = blocked or ResumeVerdict("ready", None)
    if verdict.state in _WITH_TARGET:
        verdict = replace(verdict, target=target_requirement(status, core))
    return verdict


def status_field(status: OperationStatus, core: Any) -> ResumeVerdict | None:
    """Legacy /8 projection: only Core-suspended campaigns have resume readiness."""
    if status.kind != "campaign" or not core_suspended(core):
        return None
    return resume_verdict(status, core)


def raise_for(verdict: ResumeVerdict | None) -> None:
    if verdict is None or verdict.state == "ready":
        return
    raise OwnerError(verdict.reason, "resume" if verdict.reason == "resource_busy" else "before_start")


def raise_for_target(verdict: ResumeVerdict | None, target_ref: TargetRef) -> None:
    if verdict is not None and verdict.target is not None and verdict.target.excludes(target_ref):
        raise OwnerError(verdict.target.reason, "before_start")


def pick_ready_target(targets, excluded):
    """The first ``ready`` target of a Bundle the campaign used, that the campaign has not used.

    ``targets`` are ``lab-owner-target/1`` payloads, ``excluded`` the ``lab-target-ref/1``
    list of the resume requirement.  Identity is ``(id, owner_generation)``."""
    used = {(ref["id"], ref["owner_generation"]) for ref in excluded}
    bundles = {t["bundle_digest"] for t in targets if (t["id"], t["owner_generation"]) in used}
    for t in targets:
        if t["state"] == "ready" and t["bundle_digest"] in bundles and (t["id"], t["owner_generation"]) not in used:
            return {"id": t["id"], "owner_generation": t["owner_generation"]}
    return None
