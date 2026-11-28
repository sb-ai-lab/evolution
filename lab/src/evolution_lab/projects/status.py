"""Scenario status ``lab-project-status/6`` (spec §3.10; dev32 §3; dev35: spec 2026-10-01 §2, §4; dev38: spec
2026-10-02 §2): one pure function for the CLI and the service. Inputs are the facts of part 1, link snapshots and
what the session found at the artifact paths; nothing here reads disk."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from evolution_lab.guides._scenarios import MAX_INVALID, substitute

from .models import Fact, Pair

SCHEMA = "lab-project-status/6"
ACTIVE = frozenset({"reserved", "running", "stopping"})
# blocked_reason codes (docs/api/projects.md "Status"); the panel's label table is pinned to these literals.
BLOCKED_CODES = ("results_deleted", "artifact_invalid", "schema_unusable", "campaign_suspended",
                 "campaign_state_unknown")
_RESUMABLE = frozenset({"ready", "cleanup_first"})
_SETTLED = frozenset({"finished", "released"})
_FACT_OF = {("check", "passed"): "check.passed", ("conformance", "passed"): "conformance.passed",
            ("evaluation", "measured"): "baseline.measured"}


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    """A schema-checked artifact as the session observed it (spec 2026-10-01 §2.3; dev38 §2.1); a missing file has
    none."""
    state: str  # valid | invalid | schema_unusable
    invalid: tuple[dict, ...] = ()  # at most MAX_INVALID {path (JSON Pointer), message}
    schema: str | None = None  # the schema of a schema_unusable check
    digest: str | None = None  # "sha256:<hex>" of the observed bytes; None when they were not read


@dataclass(frozen=True, slots=True)
class CycleView:
    operation_id: str
    pair: Pair
    here: bool  # same pair and state root: this Owner knows the operation
    control_state: str | None
    resume: dict | None
    bundle_digest: str
    prepared_digest: str | None
    export_id: str | None
    order: int  # sort key: this Owner's order of the latest start (ADR-0015)
    outcome: dict | None = None  # short campaign outcome: Owner's summary, else the registry's snapshot
    last_event_at: str | None = None  # Owner times of this cycle; None for another pair's cycle
    export_deleted: bool = False  # the export of the current start was deleted with the results (W55)
    results_state: str | None = None  # the operation's results_state: Owner's here, the snapshot elsewhere
    position: int = 0  # Lab's cycle number among the project's campaign links (W68): `order` on the wire


@dataclass(frozen=True, slots=True)
class StatusInputs:
    pair: Pair
    facts: tuple[Fact, ...]
    # path -> digest of a held `exists` output (None: a regular file that could not be read); a schema-only path in
    # the map is never read from it
    artifacts: Mapping[str, str | None]
    manifest_ok: bool  # the package entry is a campaign entry and package.valid holds
    prepared_current: tuple[str, ...]  # campaign PreparedExecutions of this pair on current inputs, oldest first
    cycles: tuple[CycleView, ...]
    # When an output became current: ("fact", id), ("bundle", digest), ("prepared", digest) -> ISO time
    moments: Mapping[tuple[str, str], str] = field(default_factory=dict)
    implementation: str | None = None  # the id `{implementation}` stands for (spec 2026-10-01 §2.2)
    # (scenario, substituted path, schema) -> check; `exists` outputs are in `artifacts`
    checks: Mapping[tuple[str, str, str], ArtifactCheck] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Out:
    state: str  # held | stale | absent
    source: dict | None = None
    carried: tuple = ()
    stale_reason: dict | None = None
    at: str | None = None  # when a held output became current, if its source records it
    blocked: dict | None = None  # an absent output that cannot be done as it is: its blocked_reason
    invalid: tuple = ()  # artifact_invalid: where the artifact fails its schema


_ABSENT = _Out("absent")


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, (dict, list, tuple)) and bool(value))


def _holds(facts: tuple[Fact, ...], name: str) -> bool:
    return any(f.id == name and f.holds for f in facts)


def _confirmations(found: list[Fact]) -> list[dict]:
    """The held confirmations that give this output, oldest first (ties by id)."""
    rows = [{"id": s["id"], "author": s["author"], "channel": s["channel"], "created_at": s["created_at"]}
            for s in ((f.source or {}) for f in found if f.holds) if s.get("kind") == "confirmation"]
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


def _fact(facts: tuple[Fact, ...], name: str, moments: Mapping, *, path: str | None = None) -> _Out:
    found = [f for f in facts if f.id == name
             and (path is None or ((f.source or {}).get("attests") or {}).get("path") == path)]
    held = next((f for f in found if f.holds), None)
    if held is not None:
        confirmations = _confirmations(found)
        # A confirmed output became current with its latest confirmation (decision Д); a computed
        # one keeps the moment its source records, if any.
        at = confirmations[-1]["created_at"] if confirmations else moments.get(("fact", name))
        return _Out("held", {"fact": name, "confirmations": confirmations}, tuple(held.carried_over), at=at)
    stale = next((f for f in found if f.stale_reason is not None and _truthy(f.value)), None)
    if stale is not None:
        return _Out("stale", stale_reason=stale.stale_reason.to_payload())
    return _ABSENT


def _artifact(body: dict, inputs: StatusInputs, scenario_id: str) -> _Out:
    path = substitute(body["path"], inputs.implementation)
    if body["check"] == "exists":
        return _Out("held", {"artifact": path, "digest": inputs.artifacts[path]}) if path in inputs.artifacts else _ABSENT
    found = inputs.checks.get((scenario_id, path, body["schema"]))
    if found is None:  # no file: not done, like a missing `exists` artifact
        return _ABSENT
    if found.state == "valid":
        return _Out("held", {"artifact": path, "digest": found.digest})
    if found.state == "invalid":
        # W80: each record names its file and the digest of the bytes that failed (spec 2026-10-02 §2.2).
        return _Out("absent", blocked={"code": "artifact_invalid"},
                    invalid=tuple({"artifact": path, "digest": found.digest, **item} for item in found.invalid))
    return _Out("absent", blocked={"code": "schema_unusable", "schema": body["schema"]})


def _output(output: dict, inputs: StatusInputs, cycle: CycleView | None, scenario_id: str) -> _Out:
    (kind, body), = output.items()
    if kind == "attestation":
        return _fact(inputs.facts, body["claim"], inputs.moments, path=substitute(body["path"], inputs.implementation))
    if kind == "artifact":
        # A started cycle took its documents from its Bundle. No `check: "schema"` output reaches here: the catalogue
        # refuses them in repeatable scenarios (user decision 2026-10-01, codex r2).
        if cycle is not None:
            return _Out("held", {"bundle": cycle.bundle_digest}, at=inputs.moments.get(("bundle", cycle.bundle_digest)))
        if body["check"] == "package":
            return _fact(inputs.facts, "package.valid", inputs.moments)
        if body["check"] == "manifest":
            return _Out("held", {"artifact": "@manifest"}) if inputs.manifest_ok else _ABSENT
        return _artifact(body, inputs, scenario_id)
    pair = (body["kind"], body["outcome"])
    if pair in _FACT_OF:
        return _fact(inputs.facts, _FACT_OF[pair], inputs.moments)
    if pair == ("prepare.campaign", "prepared"):
        digest = cycle.prepared_digest if cycle is not None else (inputs.prepared_current[-1] if inputs.prepared_current else None)
        return _Out("held", {"prepared": digest}, at=inputs.moments.get(("prepared", digest))) if digest else _ABSENT
    if cycle is None:  # a run and its export belong to a started cycle
        return _ABSENT
    if pair == ("campaign", "terminal"):
        # Done only when core's projection says the campaign is terminal (ADR-0015; spec 2026-10-01 §2.4): a
        # settled start of a suspended campaign is not the end of it.
        if cycle.control_state is None or cycle.control_state in ACTIVE:
            return _ABSENT
        if cycle.outcome is not None:
            return _Out("held", {"operation": cycle.operation_id}, at=cycle.last_event_at)
        if cycle.resume is not None:
            return _Out("absent", blocked={"code": "campaign_suspended"})
        return _Out("absent", blocked={"code": "campaign_state_unknown"})
    if cycle.export_id:
        return _Out("held", {"export": cycle.export_id,
                             "results_state": "deleted" if cycle.export_deleted else "present"})
    # Deleted without an export: a re-export refuses results_deleted, so the step can never be done.
    return _Out("absent", blocked={"code": "results_deleted"}) if cycle.results_state == "deleted" else _ABSENT


def _invalid(outs: list[_Out], files: list[str]) -> list[dict]:
    """One unit for the cap — the file (codex r1 of the dev38 spec): files in the order of their first output in the
    step (`files`, valid or not; a file without records drops out), a file's records in descriptor order then
    pointer, a (path, message) pair a second schema repeats not doubled."""
    by_file: dict[str, list[dict]] = {file: [] for file in files}
    for out in outs:
        for item in out.invalid:
            rows = by_file.setdefault(item["artifact"], [])
            if len(rows) < MAX_INVALID and all((r["path"], r["message"]) != (item["path"], item["message"]) for r in rows):
                rows.append(dict(item))
    return [row for rows in by_file.values() for row in rows]


def _steps(descriptor: dict, inputs: StatusInputs, cycle: CycleView | None) -> list[dict]:
    states: dict[str, str] = {}
    rows = []
    for step in descriptor["steps"]:
        outs = [_output(output, inputs, cycle, descriptor["id"]) for output in step["outputs"]]
        missing: list[str] = []
        blocked = next((o.blocked for o in outs if o.blocked is not None), None)
        if all(o.state == "held" for o in outs):
            state = "done"
        elif all(o.state != "absent" for o in outs):
            state = "stale"
        else:
            missing = [name for name in step["requires"]
                       if not (states[name] == "done" if name in states else _holds(inputs.facts, name))]
            state = "blocked" if missing or blocked is not None else "ready"
        states[step["id"]] = state
        # Pointers belong to the reason shown: `invalid` is empty unless the chosen code is artifact_invalid.
        files = list(dict.fromkeys(
            substitute(body["path"], inputs.implementation)
            for output in step["outputs"] for kind, body in output.items()
            if kind == "artifact" and "path" in body))
        invalid = (_invalid(outs, files)
                   if state == "blocked" and blocked is not None and blocked["code"] == "artifact_invalid" else [])
        rows.append({
            "id": step["id"], "title": step["title"], "state": state,
            "optional": step.get("optional", False), "human_only": step["human_only"], "missing": missing,
            "blocked_reason": dict(blocked) if state == "blocked" and blocked is not None else None,
            "invalid": invalid,
            "stale_reason": next((o.stale_reason for o in outs if o.state == "stale"), None) if state == "stale" else None,
            "carried_over": [dict(item) for o in outs for item in o.carried] if state == "done" else [],
            "sources": [o.source for o in outs if o.state == "held"],
            # The latest moment of its outputs; unknown when any output does not record one.
            "done_at": max(o.at for o in outs) if state == "done" and all(o.at for o in outs) else None,
        })
    return rows


def _complete(rows: list[dict]) -> bool:
    """A cycle is complete when every non-optional step is done (spec 2026-10-01 §2.4)."""
    return all(row["state"] == "done" for row in rows if not row["optional"])


def _current(cycles: tuple[CycleView, ...]) -> CycleView | None:
    here = [c for c in cycles if c.here]
    active = [c for c in here if c.control_state in ACTIVE]
    if active:
        return max(active, key=lambda c: c.order)
    resumable = [c for c in here if c.control_state in _SETTLED
                 and isinstance(c.resume, dict) and c.resume.get("state") in _RESUMABLE]
    return max(resumable, key=lambda c: c.order) if resumable else None


def scenario_status(descriptor: dict, inputs: StatusInputs) -> dict:
    missing = [name for name in descriptor["requires"] if not _holds(inputs.facts, name)]
    repeatable = descriptor["repeatable"] is not None
    if missing:
        state = "unavailable"
    elif not repeatable and all(_holds(inputs.facts, name) for name in descriptor["produces"]):
        state = "produced"
    else:
        state = "available"
    row: dict[str, Any] = {"id": descriptor["id"], "version": descriptor["version"], "repeatable": repeatable,
                           "state": state, "missing": missing}
    if not repeatable:
        row["steps"] = _steps(descriptor, inputs, None)
        return row
    current = _current(inputs.cycles)
    rest = sorted((c for c in inputs.cycles if c is not current), key=lambda c: -c.order)
    row["next_cycle"] = {"steps": _steps(descriptor, inputs, None)}
    cycles = []
    for c in ([current] if current is not None else []) + rest:
        rows = _steps(descriptor, inputs, c)
        cycles.append({
            "operation_id": c.operation_id, "order": c.position, "pair": c.pair.to_payload(), "current": c is current,
            "detail": "available" if c.pair == inputs.pair else "unavailable",
            "control_state": c.control_state, "resume": c.resume, "outcome": c.outcome,
            "complete": _complete(rows), "steps": rows,
        })
    row["cycles"] = cycles
    return row


def status_payload(*, project_id: str, implementation_id: str, catalogue, inputs: StatusInputs,
                   scenario: str | None = None) -> dict:
    descriptors = catalogue.descriptors if scenario is None else (catalogue.descriptor(scenario),)
    return {"schema": SCHEMA, "project": project_id, "implementation": implementation_id,
            "pair": inputs.pair.to_payload(),
            "catalogue": {"version": catalogue.catalogue["catalogue_version"], "digest": catalogue.catalogue["digest"]},
            "facts": [fact.to_payload() for fact in inputs.facts],
            "scenarios": [scenario_status(descriptor, inputs) for descriptor in descriptors]}
