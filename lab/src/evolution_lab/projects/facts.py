"""Project facts as pure functions over a gathered context (spec §3.5-§3.6)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .inventory import Entry, diff
from .models import Fact, Implementation, Pair, Project, StaleReason
from .revision import Revision

COMPUTED = ("implementation.available", "implementation.ported", "materials.revision", "package.present",
            "package.valid", "check.passed", "conformance.passed", "baseline.measured", "connected", "campaigns")
OPERATION_FACTS = ("check.passed", "conformance.passed", "baseline.measured")
ACCEPTABLE_CODES = frozenset({"inputs_changed", "digest_changed", "catalogue_changed", "place_changed", "other_pair"})
# An operation is still active (not eligible as "the latest terminal evaluation") while it is
# reserved/running/stopping; anything else (finished, released, orphaned, cleanup_required, ...)
# is terminal enough to be *selected* as latest — whether it actually counts as measured is a
# separate question, answered by ``baseline_failure`` below (fix round 1, I2).
_ACTIVE_CONTROL_STATES = frozenset({"reserved", "running", "stopping"})


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    action: str
    state: str
    target_id: str
    pair: Pair
    binding: dict
    inputs_digest: str
    recorded_entries: tuple[Entry, ...]
    current_digest: str | None
    current_entries: tuple[Entry, ...]
    order: int
    bundle_digest: str | None = None
    measurement_basis: str | None = None  # original Bundle, current pair/state only


@dataclass(frozen=True, slots=True)
class OperationView:
    operation_id: str
    kind: str
    pair: Pair
    control_state: str
    result: dict | None
    binding: dict
    inputs_digest: str
    recorded_entries: tuple[Entry, ...]
    current_digest: str | None
    current_entries: tuple[Entry, ...]
    order: int
    bundle_digest: str | None = None
    measurement_basis: str | None = None  # never the basis of an acceptance's to-state


@dataclass(frozen=True, slots=True)
class FactContext:
    project: Project
    implementation: Implementation | None
    pair: Pair
    revision: Revision
    package_present: bool
    package_errors: tuple[dict, ...] | None  # None: no entry to check
    place_available: bool  # filesystem: the implementation's place exists and is readable/traversable
    tasks: tuple[TaskView, ...]
    operations: tuple[OperationView, ...]
    campaigns: tuple[dict, ...]
    confirmations: tuple  # Confirmation (Task 8)
    confirmation_now: Mapping[str, dict]  # confirmation id -> {"attests", "linked", "catalogue"}
    acceptances: tuple  # Acceptance (Task 8)
    inventories: Mapping[str, tuple[Entry, ...]]
    package_entry_path: str | None = None  # the implementation's package_entry, relative to the materials root
    package_reason: str | None = None  # None when present; "not_configured" | "missing" | "not_a_file"
    current_bundle_digest: str | None = None
    current_measurement_basis: str | None = None


def _place_key(binding: Mapping[str, Any]) -> tuple:
    return (binding.get("working_copy"), binding.get("materials_root"), binding.get("place"))


def _current_place_key(ctx: FactContext) -> tuple:
    return (ctx.project.working_copy.path, ctx.project.materials_root,
            None if ctx.implementation is None else ctx.implementation.place)


def _state(view, ctx: FactContext) -> dict:
    return {"inputs_digest": view.current_digest, "place": list(_current_place_key(ctx)), "pair": ctx.pair.to_payload()}


def _source_key(view) -> dict:
    if isinstance(view, TaskView):
        return {"kind": "task", "id": view.task_id}
    return {"kind": "operation", "id": view.operation_id}


def _carry(fact_id: str, source: dict, state: dict, acceptances) -> tuple[tuple[dict, ...], dict | None]:
    """The unbroken chain of acceptances of this fact's source; its last ``to`` state, if any.

    Spec §3.8: each acceptance extends the chain that carried the fact when it was made, so a
    revoked link breaks the chain there — the links after it built on it and do not carry
    either (final review I1). The chain is replayed in creation order (store ``seq``); before
    a link is added, links revoked strictly earlier than its ``created_at`` are cut, so an
    acceptance made *after* a revocation starts from what was left (re-accepting still works).
    A revocation in the same second as a later link is taken as having happened after it — the
    conservative reading, which leaves the fact stale rather than carried.
    """
    chain: list[tuple] = []

    def cut(before: str | None) -> None:
        for index, (acceptance, _) in enumerate(chain):
            at = None if acceptance.revoked is None else acceptance.revoked.get("at")
            if acceptance.revoked is not None and (before is None or (at is not None and at < before)):
                del chain[index:]
                return

    for acceptance in acceptances:
        for entry in acceptance.facts:
            if entry["id"] == fact_id and entry["from"] == source:
                cut(acceptance.created_at)
                chain.append((acceptance, entry))
    cut(None)
    links = tuple({"acceptance": acceptance.id, "reason": acceptance.reason, "across_pairs": acceptance.across_pairs}
                  for acceptance, _ in chain)
    return links, (chain[-1][1]["to"] if chain else None)


def _stale(view, ctx: FactContext, baseline_entries, place: tuple | None = None) -> StaleReason | None:
    codes: list[str] = []
    changes = None
    if view.pair != ctx.pair:
        codes.append("other_pair")
    if (place if place is not None else _place_key(view.binding)) != _current_place_key(ctx):
        codes.append("place_changed")
    if view.current_digest != view.inputs_digest:
        codes.append("inputs_changed")
        changes = diff(baseline_entries, view.current_entries)
    return StaleReason(tuple(codes), changes) if codes else None


def _evidence_fact(fact_id: str, view, value: bool, ctx: FactContext, extra: dict | None = None) -> Fact:
    source = {**_source_key(view), "pair": view.pair.to_payload(), "inputs_digest": view.inputs_digest, **(extra or {})}
    subject = {"implementation": ctx.implementation.id} if ctx.implementation else None
    state = _state(view, ctx)
    if not value:
        return Fact(fact_id, subject, False, source, None, (), state)
    chain, latest = _carry(fact_id, _source_key(view), state, ctx.acceptances)
    if latest is not None and latest == state:
        return Fact(fact_id, subject, True, source, None, chain, state)
    baseline = ctx.inventories.get(latest["inputs_digest"], view.recorded_entries) if latest else view.recorded_entries
    place = None
    if latest is not None:
        view = replace(view, inputs_digest=latest["inputs_digest"], pair=Pair.from_payload(latest["pair"]))
        place = tuple(latest["place"])
    return Fact(fact_id, subject, True, source, _stale(view, ctx, baseline, place), chain, state)


def baseline_failure(view: OperationView) -> str | None:
    if view.kind != "evaluation" or view.control_state not in ("finished", "released"):
        return "not_finished"
    result = view.result or {}
    coverage = result.get("coverage")
    threshold = result.get("min_coverage", 1.0)
    if (not coverage or coverage.get("expected", 0) <= 0
            or coverage.get("scored", 0) / coverage["expected"] < threshold):
        return "coverage"
    if result.get("stopped_reason", result.get("stop_reason")) is not None:
        return "stop_reason"
    if (result.get("persistence") or {}).get("state") != "saved":
        return "persistence"
    return None


def _effective_digest(fact: Fact) -> str | None:
    if fact.carried_over:
        return (fact.state or {}).get("inputs_digest")
    return (fact.source or {}).get("inputs_digest")


def _baseline_fact(view: OperationView, ctx: FactContext) -> Fact:
    failure = baseline_failure(view)
    extra = {"bundle_digest": view.bundle_digest, "measurement_basis": view.measurement_basis,
             "same_bundle": view.bundle_digest is not None and view.bundle_digest == ctx.current_bundle_digest}
    if failure is not None:
        extra["failed"] = failure
    fact = _evidence_fact("baseline.measured", view, failure is None, ctx, extra)
    if failure is not None:
        return fact
    # Preserve the unchanged-input and exact-acceptance rules without requiring a Bundle proof.
    if fact.holds:
        return fact
    basis_matches = (ctx.package_errors == () and view.pair == ctx.pair
                     and _place_key(view.binding) == _current_place_key(ctx)
                     and view.measurement_basis is not None
                     and view.measurement_basis == ctx.current_measurement_basis)
    if basis_matches:
        # A stale acceptance chain is not an effective digest (or a new basis).
        return replace(fact, stale_reason=None, carried_over=())
    return fact


def _ported(ctx: FactContext, subject: dict | None) -> Fact:
    """``implementation.ported`` (spec 2026-10-01 §2.5): the implementation was ported from another one the
    project still registers, archived included. A false value names its reason in the source."""
    origin = {} if ctx.implementation is None else ctx.implementation.origin
    source_id = origin.get("implementation") if origin.get("kind") == "ported_from" else None
    if source_id is None:
        return Fact("implementation.ported", subject, False,
                    {"kind": "origin", "implementation": None, "reason": "not_ported"}, None)
    if ctx.project.implementation(source_id) is None:
        return Fact("implementation.ported", subject, False,
                    {"kind": "origin", "implementation": source_id, "reason": "source_missing"}, None)
    return Fact("implementation.ported", subject, True,
                {"kind": "origin", "implementation": source_id, "reason": None}, None)


def compute_facts(ctx: FactContext) -> tuple[Fact, ...]:
    subject = {"implementation": ctx.implementation.id} if ctx.implementation else None
    facts: list[Fact] = []
    active = ctx.implementation is not None and ctx.implementation.archived is None
    available = active and ctx.place_available
    facts.append(Fact("implementation.available", subject, available, {"kind": "filesystem"}, None))
    facts.append(_ported(ctx, subject))
    facts.append(Fact("materials.revision", None, {"digest": ctx.revision.digest, "git": ctx.revision.git},
                      {"kind": "revision"}, None))
    facts.append(Fact("package.present", subject, ctx.package_present,
                      {"kind": "package_entry", "entry": ctx.package_entry_path, "reason": ctx.package_reason}, None))
    valid = ctx.package_errors is not None and not ctx.package_errors
    facts.append(Fact("package.valid", subject, {"scope": "form"} if valid else False,
                      {"kind": "package_entry", "errors": list(ctx.package_errors or ())}, None))
    for fact_id, action in (("check.passed", "check"), ("conformance.passed", "conformance")):
        views = sorted((t for t in ctx.tasks if t.action == action), key=lambda t: t.order)
        if not views:
            facts.append(Fact(fact_id, subject, False, None, None))
            continue
        latest = views[-1]
        facts.append(_evidence_fact(fact_id, latest, latest.state == "succeeded", ctx))
    terminal = sorted((o for o in ctx.operations
                       if o.kind == "evaluation" and o.control_state not in _ACTIVE_CONTROL_STATES),
                      key=lambda o: o.order)
    if not terminal:
        facts.append(Fact("baseline.measured", subject, False, None, None))
    else:
        facts.append(_baseline_fact(terminal[-1], ctx))
    by_id = {fact.id: fact for fact in facts}
    evidence = [by_id[name] for name in OPERATION_FACTS]
    check, conformance, baseline = evidence
    same_checks = _effective_digest(check) == _effective_digest(conformance)
    same_baseline = _effective_digest(baseline) == _effective_digest(check)
    check_view = max((t for t in ctx.tasks if t.action == "check"), key=lambda t: t.order, default=None)
    same_basis = (bool(terminal) and check_view is not None and check_view.pair == ctx.pair
                  and terminal[-1].pair == ctx.pair and terminal[-1].measurement_basis is not None
                  and terminal[-1].measurement_basis == check_view.measurement_basis)
    same = same_checks and (same_baseline or same_basis)
    facts.append(Fact("connected", subject, all(f.holds for f in evidence) and same,
                      {"kind": "derived", "facts": list(OPERATION_FACTS)}, None))
    facts.append(Fact("campaigns", subject, [dict(item) for item in ctx.campaigns], {"kind": "links"}, None))
    facts.extend(confirmed_facts(ctx))
    return tuple(facts)


def confirmed_facts(ctx: FactContext) -> tuple[Fact, ...]:
    latest: dict[tuple, object] = {}
    for confirmation in ctx.confirmations:
        applies = confirmation.implementation in (None, None if ctx.implementation is None else ctx.implementation.id)
        if confirmation.revoked is None and applies:
            latest[(confirmation.claim, confirmation.implementation)] = confirmation
    facts = []
    for (claim, implementation), confirmation in sorted(latest.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        now = ctx.confirmation_now.get(confirmation.id)
        subject = None if implementation is None else {"implementation": implementation}
        source = {"kind": "confirmation", "id": confirmation.id, "attests": dict(confirmation.attests),
                  "channel": confirmation.channel, "author": confirmation.author,
                  "created_at": confirmation.created_at}
        state = None if now is None else {"attests": now["attests"], "linked": [l["digest"] for l in now["linked"]]}
        # Compare the current digests against the latest non-revoked acceptance's "to" state
        # when one exists (fix round 1, I1) — not against the confirmation's original digests,
        # which would keep reporting a code an acceptance already carried forward. Absent any
        # acceptance, the confirmation itself is the baseline. Every changed position is
        # reported (not just the first), attests/file/place/revision as "digest_changed" and
        # catalogue items as "catalogue_changed", deduplicated and ordered digest-then-catalogue.
        chain, latest_to = _carry(claim, {"kind": "confirmation", "id": confirmation.id}, state or {}, ctx.acceptances)
        baseline = latest_to if latest_to is not None else {
            "attests": confirmation.attests["digest"], "linked": [item["digest"] for item in confirmation.linked]}
        # A missing current digest (file gone, place unknown, ...) never matches a baseline —
        # not even a ``None`` an older acceptance may have recorded (final review I2).
        codes: list[str] = []
        now_attests = None if state is None else state["attests"]
        if now_attests is None or now_attests != baseline.get("attests"):
            codes.append("digest_changed")
        current_linked = () if state is None else state["linked"]
        for item, base_digest, now_digest in zip(confirmation.linked, baseline.get("linked") or (), current_linked):
            if now_digest is None or base_digest != now_digest:
                code = "catalogue_changed" if item["kind"] == "catalogue" else "digest_changed"
                if code not in codes:
                    codes.append(code)
        codes = [code for code in ("digest_changed", "catalogue_changed") if code in codes]
        stale = StaleReason(tuple(codes)) if codes else None
        # Fresh relative to the baseline: holds. Carried_over is populated only while an
        # acceptance's "to" is that baseline and it still matches the current state — a
        # confirmation that was simply never stale carries nothing.
        carried_over = chain if stale is None and latest_to is not None else ()
        facts.append(Fact(claim, subject, True, source, stale, carried_over, state))
    return tuple(facts)
