"""The one path that binds preparation, targets and operations to projects (ADR-0017 p. 4)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from evolution_lab.durability import Durability
from evolution_lab.owner.models import Approval, OwnerError, ResourceRef, TargetRef
from evolution_lab.owner.probe import SEED_BOUND

from evolution_lab.preparation import EnvironmentInput, ProbeSource, SourceRoot
from evolution_lab.service.codecs import SERVICE_ENVIRONMENT, BundleRequest

from .demo import free_port
from .entry import ENTRY_FILE, probe_launch, read_target_entry
from .errors import ProjectError
from .gateway_cost import GatewayLedgerUnavailable, ledger_mark, units_to_amount, window_cost
from .ids import validate_id
from .inventory import BufferedHashCache, Observation, observe_file
from .models import Link, Pair
from .package import (covers_place, environment_declarations, environment_inputs_digest, environment_inventory,
                      input_sources, inputs_inventory, inventory_sources, read_package_entry)
from .probe_turns import check_ceiling, turn_record
from .probes import ProbeStore
from .registry import Projects, utc_now
from .revision import materials_revision

_ACTIVE = {"reserved", "running", "stopping"}


def client_ref(project: str, implementation: str) -> str:
    return json.dumps({"implementation": implementation, "project": project}, sort_keys=True, separators=(",", ":"))


def parse_client_ref(value: str) -> tuple[str, str] | None:
    try:
        row = json.loads(value)
    except (TypeError, ValueError):
        return None
    if isinstance(row, dict) and set(row) == {"implementation", "project"}:
        # Owner keeps any opaque label; only two valid ids make it a Lab label — anything
        # else (a list, a number, "Agent!") is someone else's and is skipped (codex r2).
        try:
            return validate_id(row["project"]), validate_id(row["implementation"])
        except ProjectError:
            return None
    return None


def seen_resume(resume: dict | None) -> dict | None:
    """The resume verdict as last_seen stores it: {state, reason} (record_json frozen, ADR-0017)."""
    return None if resume is None else {"state": resume["state"], "reason": resume["reason"]}


def registry_resume(seen: dict | None, target: dict | None) -> dict | None:
    """The resume verdict a project schema /2 shows: last_seen's plus the recorded target."""
    if seen is None:
        return None
    # A crash between set_last_seen and set_link_resume_target can leave a stale target next to a
    # state that cannot continue: the requirement only applies to continuable states.
    keep = seen["state"] in {"ready", "cleanup_first", "busy"}
    return {"state": seen["state"], "reason": seen["reason"], "target": target if keep else None}


def last_seen_of(status) -> dict:
    payload = status.to_payload()
    result = payload.get("result")
    if result is not None and result.get("schema") == "lab-operation-result/3":
        # The link's frozen snapshot keeps the pre-dev72 result contract.
        result = {key: value for key, value in result.items()
                  if key not in {"unanswered", "unanswered_state", "answered_reported"}}
        result["schema"] = "lab-operation-result/2"
    return {"at": utc_now(), "control_state": payload["control_state"], "kind": payload["kind"],
            "results_state": payload.get("results_state"), "result": result,
            "resume": seen_resume(payload.get("resume"))}


def _frozen_result(stored: dict | None) -> bool:
    """Whether a stored ``result`` is fixed and must survive a later ``sync()``.

    Spec §3.6 / PDR-0015: a fixed (terminal) evaluation is never continued, so once its result is recorded it
    cannot legitimately change — only a later, unreadable results journal could make a resync *look* like it
    changed. The same holds for a campaign whose result carries core's ``campaign_outcome`` (terminal by core's
    projection, ADR-0015; spec 2026-10-01 §2.4): it is never continued, and after ``delete-results`` its snapshot
    is the only record that it ended. ``control_state`` itself keeps refreshing (fix round 1, I3).
    """
    if not stored or stored.get("control_state") in _ACTIVE or stored.get("result") is None:
        return False
    if stored.get("kind") == "evaluation":
        return True
    from evolution_lab.owner.summary import short_outcome_of_result
    return stored.get("kind") == "campaign" and short_outcome_of_result("campaign", stored["result"]) is not None


def _current_inputs(project, binding, store, implementation=None, service_environment=None, *,
                    cache=None) -> tuple[str | None, tuple]:
    """Legacy bindings keep their sources; new ones also follow the current declaration."""
    from .inventory import inventory_digest
    from evolution_lab.service.environment import expand_service_environment
    from evolution_lab.service.errors import ServiceError
    cache = store if cache is None else cache
    working_copy = Path(project.working_copy.path)
    metadata = store.input_environment(binding["inputs"]["digest"])
    if metadata is None:
        entries = inventory_sources(binding["inputs"]["sources"], working_copy=working_copy, cache=cache)
        return inventory_digest(entries), entries
    _, base_sources = metadata
    if implementation is None:
        return None, ()
    try:
        if implementation.package_entry is not None:
            request = read_package_entry(working_copy / project.materials_root / implementation.package_entry)
            environments = request.environments
        else:
            entry = read_target_entry(working_copy / implementation.place)
            if entry is None:
                return None, ()
            environments = (SERVICE_ENVIRONMENT,)
            if entry.environment is not None:
                environments += (EnvironmentInput(Path(entry.environment["executable"]),
                    tuple(map(Path, entry.environment["wheel_paths"])),
                    tuple(map(Path, entry.environment["runtime_files"]))),)
        environments, _ = expand_service_environment(environments, service_environment)
    except (ProjectError, ServiceError):
        return None, ()
    declarations = environment_declarations(environments)
    # Replacing the environment portion must not drop a document/root which happened to
    # consume the same file. Retain its source role, even after the environment drops it.
    entries = environment_inventory(base_sources, declarations, working_copy=working_copy, cache=cache)
    return environment_inputs_digest(entries, declarations, base_sources), entries


def _entries_for_state(project, fact, store, service_environment=None, *, cache=None) -> tuple[str | None, tuple]:
    """Current ``(digest, entries)`` of the sources behind an evidence fact (stored with an
    acceptance); ``(None, ())`` when no link carries the fact's source."""
    source = fact.source or {}
    for link in store.links(project.id):
        if source.get("kind") == "operation" and link.kind == "operation" and link.object_id == source.get("id"):
            return _current_inputs(project, link.binding, store, project.implementation(link.implementation),
                                   service_environment, cache=cache)
        if source.get("kind") == "task" and link.kind == "target" and any(
                item.get("task_id") == source.get("id") for item in (link.last_seen or {}).get("tasks", ())):
            return _current_inputs(project, link.binding, store, project.implementation(link.implementation),
                                   service_environment, cache=cache)
    return None, ()


def _moments(links, facts, here, summaries) -> dict[tuple[str, str], str]:
    """When each status output became current (``lab-project-status/6`` ``done_at``): a Bundle
    or PreparedExecution — its link's ``created_at`` (``upsert_link`` rewrites it on a repeat, so
    it is the last time that input was made current); a confirmed fact takes its moment from its
    own source (status.py, decision Д), not from here; a held fact from an operation of this
    pair — Owner's ``times.last_event_at`` of that operation (spec §2.5). A fact from a task, or from another pair's operation, has no moment:
    no record says when that work finished. Several held facts with one id keep the latest."""
    moments: dict[tuple[str, str], str] = {}

    def note(key, at):
        if at and (key not in moments or at > moments[key]):
            moments[key] = at

    for link in links:
        if link.kind in ("bundle", "prepared"):
            note((link.kind, link.object_id), link.created_at)
    for fact in facts:
        source = fact.source or {}
        if fact.holds and source.get("kind") == "operation" and source.get("id") in here:
            note(("fact", fact.id), ((summaries.get(source["id"]) or {}).get("times") or {}).get("last_event_at"))
    return moments


def _fact_operations(facts) -> set[str]:
    return {(fact.source or {}).get("id") for fact in facts
            if fact.holds and (fact.source or {}).get("kind") == "operation"}


def _cycle_positions(links) -> dict[str, int]:
    """Lab's cycle number (W68): a campaign link's position, 1-based, among the project's campaign links in store
    order (``seq``) — every pair and implementation, so a filter never renumbers a cycle (ruling R7).
    ``lab-project-status/6`` ``order`` and the summary label "цикл N" both read it."""
    positions: dict[str, int] = {}
    for link in links:
        if link.kind == "operation" and link.operation_kind == "campaign":
            positions[link.dedupe_key()] = len(positions) + 1
    return positions


def _reject_constant(name: str):
    """``NaN``/``Infinity``/``-Infinity`` are not JSON (spec 2026-10-01 §2.3), as the preparation readers hold."""
    raise ValueError(f"non-JSON constant {name}")


def _parse(seen: Observation) -> tuple[object, str | None]:
    """One parse per path (spec 2026-10-02 §2.1): the document, or the message of a whole-file invalid record."""
    try:
        return json.loads(seen.data.decode("utf-8"), parse_constant=_reject_constant), None
    except (UnicodeDecodeError, ValueError):
        return None, "the file is not UTF-8 JSON"
    except RecursionError:  # the parser's own stack: one deep file must not break the whole status (§2.3)
        return None, "nested too deeply"


def _artifact_check(catalogue, scenario_id: str, schema: str, seen: Observation, parsed: tuple[object, str | None]):
    """A schema-checked artifact judged against its path's one observation (spec 2026-10-01 §2.3, 2026-10-02 §2.1):
    ``None`` when there is no file (not done, as for ``exists``), otherwise valid, invalid or schema_unusable."""
    from evolution_lab.guides._scenarios import SchemaUnusable
    from .status import ArtifactCheck
    if seen.state == "missing":
        return None
    if seen.state == "not_regular":
        return ArtifactCheck("invalid", ({"path": "", "message": "not a regular file"},))
    if seen.state == "unreadable":
        return ArtifactCheck("invalid", ({"path": "", "message": "the file cannot be read"},))
    document, problem = parsed
    if problem is not None:
        return ArtifactCheck("invalid", ({"path": "", "message": problem},), digest=seen.digest)
    try:
        errors = catalogue.check_artifact(scenario_id, schema, document)
    except SchemaUnusable:
        return ArtifactCheck("schema_unusable", schema=schema)
    return (ArtifactCheck("valid", digest=seen.digest) if not errors
            else ArtifactCheck("invalid", tuple(errors), digest=seen.digest))


# probe sessions (spec 2026-10-02 §3, §4)
PROBE_SESSION_SCHEMA = "lab-probe-session/1"
PROBE_SESSION_LIST_SCHEMA = "lab-probe-session-list/1"
_PROBE_ID = re.compile(r"[0-9a-f]{32}\Z")
_DECIMAL = re.compile(r"(0|[1-9][0-9]{0,17})(\.[0-9]{1,9})?\Z")


def _probe_id(value) -> str:
    if not isinstance(value, str) or _PROBE_ID.match(value) is None:
        raise ProjectError("invalid_request")
    return value


def _probe_ceiling(value) -> dict:
    """§4.4: ``max_turns`` 1…1000 required; ``max_tokens`` a positive int; ``max_cost`` a decimal string > 0."""
    if not isinstance(value, Mapping) or not set(value) <= {"max_turns", "max_tokens", "max_cost"}:
        raise ProjectError("invalid_request")
    turns, tokens, cost = value.get("max_turns"), value.get("max_tokens"), value.get("max_cost")
    if type(turns) is not int or not 1 <= turns <= 1000:
        raise ProjectError("invalid_request")
    if tokens is not None and (type(tokens) is not int or tokens < 1):
        raise ProjectError("invalid_request")
    if cost is not None and (not isinstance(cost, str) or _DECIMAL.match(cost) is None or Decimal(cost) <= 0):
        raise ProjectError("invalid_request")
    ceiling = {"max_turns": turns}
    if tokens is not None:
        ceiling["max_tokens"] = tokens
    if cost is not None:
        ceiling["max_cost"] = cost
    return ceiling


def _probe_budget(value) -> dict | None:
    """§4.3: the declared gateway ``{ledger_path (absolute), roles (non-empty, unique), currency}``."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"ledger_path", "roles", "currency"}:
        raise ProjectError("invalid_request")
    path, roles, currency = value["ledger_path"], value["roles"], value["currency"]
    if not isinstance(path, str) or "\x00" in path or not Path(path).is_absolute():
        raise ProjectError("invalid_request")
    if (not isinstance(roles, (list, tuple)) or not roles or len(set(roles)) != len(roles)
            or not all(isinstance(role, str) and role for role in roles)):
        raise ProjectError("invalid_request")
    if currency not in {"RUB", "USD"}:
        raise ProjectError("invalid_request")
    return {"ledger_path": path, "roles": list(roles), "currency": currency}


def _probe_timeout(value) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 < value <= 600:
        raise ProjectError("invalid_request")
    return value


def _ceiling_view(record: dict, used: dict) -> dict:
    """``ceiling`` of the chain, one shape in the session and the turn form (§4.1): ``tokens``/``cost`` are ``null``
    when that ceiling is not set; ``cost`` carries the currency of the session's gateway."""
    ceiling = record["ceiling"]
    tokens = cost = None
    if "max_tokens" in ceiling:
        tokens = {"used": used["tokens"], "max": ceiling["max_tokens"]}
    if "max_cost" in ceiling:
        cost = {"used": units_to_amount(used["cost_units"]), "max": ceiling["max_cost"],
                "currency": record["budget"]["currency"]}
    return {"turns": {"used": used["turns"], "max": ceiling["max_turns"]}, "tokens": tokens, "cost": cost}

_LOG = logging.getLogger("evolution_lab.projects")


async def _to_the_end(work):
    """Await ``work`` as a task that runs to its end even when the caller is cancelled: the cancelled caller waits
    for it and only then re-raises its ``CancelledError`` (the task's own result or error is dropped)."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # retrieved: the caller's cancellation is the answer
        raise


class ProjectSession:
    def __init__(self, projects: Projects, *, preparation, owner, pair: Pair, state_root: Path,
                 run_io=asyncio.to_thread, service_environment=None, socket_path: Path | None = None,
                 durability: Durability | None = None) -> None:
        self.projects, self.preparation, self.owner = projects, preparation, owner
        self.pair, self.state_root = pair, str(state_root)
        self._run_io, self._service_environment = run_io, service_environment
        self._socket_path = socket_path
        self._durability = durability
        self._mutation_lock = asyncio.Lock()
        # Per-project lock of a Lab clone (spec 2026-10-08-lab-project-sync §2.2 p. 8): a fetch and a merge of one
        # copy never overlap; taken before the mutation lock.
        self._copy_locks: dict[str, asyncio.Lock] = {}
        self._probe_store: ProbeStore | None = None  # LAB_HOME/probes.sqlite3, opened on first use
        # One per service: turns, closes and resets of every probe session go one at a time, so the
        # gateway windows of different sessions never overlap and a reset chain stays linear (spec §3.3, §4.3).
        self._probe_lock = asyncio.Lock()
        self._probe_busy: set[str] = set()  # sessions whose Owner call is in flight; reconcile leaves them
        # Service shutdown (spec §5.1): ``_probe_stopping`` refuses the calls still queued on the lock;
        # ``_probe_interrupted`` means the Owner is closing and a turn it interrupts stays pending.
        self._probe_stopping = False
        self._probe_interrupted = False
        # Set once the Owner is closed: a probe call still waiting on it answers service_stopping at once.
        self._probe_abandoned = asyncio.Event()

    # helpers
    def _binding(self, project, implementation, revision, inputs) -> dict:
        digest, _, sources = inputs
        return {"working_copy": project.working_copy.path, "materials_root": project.materials_root,
                "place": implementation.place, "materials_revision": revision.digest,
                "inputs": {"digest": digest, "sources": sources}}

    def _link(self, kind, project, implementation, binding, *, bundle, prepared=None, object_id, operation_kind=None, last_seen=None):
        return Link(kind, project.id, implementation.id, self.pair, self.state_root, utc_now(), binding,
                    bundle, prepared, object_id, operation_kind, last_seen)

    def _source_link(self, kind: str, project: str, implementation: str, object_id: str) -> Link:
        # A repeated Bundle/prepare with the same digest keeps a single dedupe-keyed row (see
        # store.upsert_link), so this normally has one candidate; the newest-match scan is
        # defense in depth against any stray duplicate rather than the load-bearing fix.
        matches = [link for link in self.projects.store.links(project, implementation=implementation, kind=kind)
                   if link.object_id == object_id and link.pair == self.pair and link.state_root == self.state_root]
        if not matches:
            raise ProjectError("not_linked")
        return matches[-1]

    def _record(self, link: Link) -> Link:
        """Insert or, on a dedupe hit, refresh the stored binding — and return what is stored.

        A repeated ``bundle()``/``prepare()`` for content that already has a link (same digest,
        same project/implementation/pair/state_root) must not keep serving a binding computed
        against a working copy that has since moved (spec §3.3); ``store.upsert_link`` updates
        the stored row's ``binding``/``created_at`` in place and hands back that stored row, so
        every reader of this link (``_source_link``, ``sync()``) sees the refreshed binding too.
        """
        try:
            return self.projects.store.upsert_link(link)
        except Exception:
            raise ProjectError("link_failed") from None

    # preparation
    def _probe_request(self, project, implementation) -> BundleRequest:
        """No package: the implementation's own ``lab-target-entry.json`` is the Bundle request (W75)."""
        place = self.projects.place_path(project, implementation)
        entry = read_target_entry(place)
        if entry is None:
            raise ProjectError("target_undeclared")
        launch = probe_launch(entry, target_id=f"lab-probe-{project.id}-{implementation.id}",
                              base_url=f"http://127.0.0.1:{free_port()}")
        declared: list = [SERVICE_ENVIRONMENT]
        if entry.environment is not None:
            declared.append(EnvironmentInput(
                Path(entry.environment["executable"]), tuple(map(Path, entry.environment["wheel_paths"])),
                tuple(map(Path, entry.environment["runtime_files"]))))
        return BundleRequest(
            source=ProbeSource(place / ENTRY_FILE, launch), environments=tuple(declared), env=entry.env,
            secret_names=entry.secret_names, roots=(SourceRoot("implementation", place),), path_bindings=())

    def _bundle_request(self, project, implementation) -> BundleRequest:
        """Build exactly the request used by Bundle without creating one."""
        if implementation.package_entry is None:
            return self._probe_request(project, implementation)
        request = read_package_entry(self.projects.root_path(project) / implementation.package_entry)
        if not covers_place(request, self.projects.place_path(project, implementation)):
            raise ProjectError("package_mismatch")
        return request

    async def tree(self, project_id: str, implementation_id: str, path: str, *, text: bool = False) -> dict:
        from .tree import excluded, tree_digest, tree_entries, text_file
        if not isinstance(text, bool) or not isinstance(implementation_id, str) or not implementation_id:
            raise ProjectError("invalid_request")
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        place = self.projects.place_path(project, implementation)
        # Validate even the overview path, including symlink ancestors.
        reason = excluded(project, implementation.id, path)
        if Path(path).as_posix() != "." and reason is not None:
            raise ProjectError("tree_excluded", reason)
        if not place.is_dir() or not os.access(place, os.R_OK | os.X_OK):
            raise ProjectError("working_copy_unavailable")

        def gather():
            hashes = BufferedHashCache(self.projects.store)
            revision = materials_revision(project, cache=hashes)
            entries = tree_entries(project, revision, implementation.id, path)
            with self.projects.store.transaction():
                hashes.flush()
            try:
                request = self._bundle_request(project, implementation)
                target = (place / path).resolve()
                covers = any(target.is_relative_to(Path(root.source_path).resolve()) for root in request.roots)
                bundle_reason = None
            except ProjectError as error:
                covers, bundle_reason = None, error.code
            rows = []
            for entry in entries or ():
                marker = text_file(place.resolve(), Path(path) / entry.path) if text and entry.type == "file" else None
                rows.append({"path": entry.path, "type": entry.type, "size": entry.size,
                             "sha256": entry.sha256, "executable": entry.executable,
                             "target": entry.target, "text": marker})
            return {"schema": "lab-project-tree/1", "project": project.id, "implementation": implementation.id,
                    "path": path, "exists": entries is not None,
                    "digest": None if entries is None else tree_digest(entries),
                    "bundle_covers": covers, "bundle_reason": bundle_reason, "files": rows}
        return await self._run_io(gather)

    async def bundle(self, project_id: str, implementation_id: str | None = None):
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        request = self._bundle_request(project, implementation)
        working_copy = Path(project.working_copy.path)
        cache = self.projects.store

        # Resolve the service marker from declared configuration; no new capture/probe.
        from evolution_lab.service.environment import expand_service_environment
        environments, expected = expand_service_environment(request.environments, self._service_environment)
        resolved_request = replace(request, environments=environments)

        def measure():
            hashes = BufferedHashCache(cache)
            measured = (materials_revision(project, cache=hashes),
                        inputs_inventory(resolved_request, working_copy=working_copy, cache=hashes))
            with cache.transaction():
                hashes.flush()
            return measured
        revision, inputs = await self._run_io(measure)
        extra = {} if expected is None else {"expected_execution_environment": expected}
        summary = await self._run_io(lambda: self.preparation.create_bundle(
            source=request.source, environments=environments, env=request.env, secret_names=request.secret_names,
            roots=request.roots, path_bindings=request.path_bindings, **extra))
        after_revision, after_inputs = await self._run_io(measure)
        if after_inputs[0] != inputs[0] or after_revision.digest != revision.digest:
            raise ProjectError("inputs_changed")
        binding = self._binding(project, implementation, revision, inputs)
        # The digest names both bytes and declarations. Publish them atomically with the
        # binding; a new binding must never be mistaken for a legacy one after a failed write.
        with self.projects.store.transaction():
            self.projects.store.put_inventory(inputs[0], inputs[1])
            self.projects.store.put_input_environment(inputs[0], environment_declarations(environments),
                                                     input_sources(request, working_copy))
            link = self._record(self._link("bundle", project, implementation, binding,
                                          bundle=summary.digest, object_id=summary.digest))
        return summary, link

    async def prepare(self, project_id, implementation_id, *, bundle_digest: str, kind: str, params: Mapping[str, Any]):
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        source = self._source_link("bundle", project.id, implementation.id, bundle_digest)
        method = self.preparation.prepare_campaign if kind == "campaign" else self.preparation.prepare_evaluation
        summary = await self._run_io(lambda: method(bundle_digest, **params))
        link = self._link("prepared", project, implementation, source.binding, bundle=bundle_digest,
                          prepared=summary.digest, object_id=summary.digest, operation_kind=kind)
        return summary, self._record(link)

    # execution
    async def start_target(self, project_id, implementation_id, *, bundle_digest: str, secrets, execution_policy: str | None = None):
        # The mutation lock (R12 fix 3) is held only through Owner's acceptance: a concurrent
        # archive/move must not land between "ready" and the Owner call, but the registry work
        # after acceptance is exactly the link bookkeeping fix 1 below already tolerates failing.
        async with self._mutation_lock:
            project = self.projects.ready(project_id)
            implementation = self.projects.implementation(project, implementation_id)
            self._source_link("bundle", project.id, implementation.id, bundle_digest)  # not_linked guard
            connection = await self.owner.bind(bundle_digest)
            task = await self.owner.start_target(connection, secrets=secrets,
                                                 client_ref=client_ref(project.id, implementation.id),
                                                 **({"execution_policy": execution_policy} if execution_policy is not None else {}))
        # Owner already accepted the target; a failure from here on (insert_link/sqlite, or a
        # failing status read inside sync()) must not surface as an error — spec §3.3 requires
        # `link: None` in that case, healed later by a plain sync() (R12 fix 1).
        # The link is the one of the target THIS task created or reused, named by Owner's task
        # status — not the newest link of the same Bundle: a concurrent start_target may have
        # linked another target while this call waited in sync() (codex r2).
        try:
            await self.sync(project.id)
            status = await self.owner.task(task)
            if status.ref is None or status.ref.kind != "target":
                return task, None
            return task, self.projects.store.link_for("target", status.ref.id, self.pair, self.state_root)
        except Exception:
            return task, None

    async def start(self, project_id, implementation_id, *, prepared_digest: str, target_ref, secrets,
                    approval: Approval | None = None, request_id: str | None = None):
        async with self._mutation_lock:
            project = self.projects.ready(project_id)
            implementation = self.projects.implementation(project, implementation_id)
            source = self._source_link("prepared", project.id, implementation.id, prepared_digest)
            conditions = {} if approval is None else {"approval": approval, "request_id": request_id}
            status = await self.owner.start(prepared_digest, target_ref=target_ref, secrets=secrets,
                                            client_ref=client_ref(project.id, implementation.id), **conditions)
        link = self._link("operation", project, implementation, source.binding, bundle=source.bundle_digest,
                          prepared=prepared_digest, object_id=status.id, operation_kind=getattr(status, "kind", None),
                          last_seen=last_seen_of(status))
        try:
            self.projects.store.insert_link(link)
        except Exception:
            return status, None  # client_ref keeps the attribution; sync() writes the link later
        return status, link

    # reconciliation
    async def sync(self, project_id: str | None = None) -> None:
        store = self.projects.store
        campaigns = []
        for operation_id, ref in await self.owner.client_operations():
            parsed = parse_client_ref(ref)
            if parsed is None or (project_id is not None and parsed[0] != project_id):
                continue
            status = await self.owner.status(ResourceRef("operation", operation_id))
            link = store.link_for("operation", operation_id, self.pair, self.state_root)
            # One transaction: last_seen and the resume-target row are one snapshot (ADR-0017).
            with store.transaction():
                if link is None:
                    prepared_matches = [l for l in store.links(parsed[0], implementation=parsed[1], kind="prepared")
                                         if l.object_id == status.prepared_digest and l.pair == self.pair
                                         and l.state_root == self.state_root]
                    prepared = prepared_matches[-1] if prepared_matches else None
                    if prepared is None:
                        continue
                    link = Link("operation", parsed[0], parsed[1], self.pair, self.state_root, utc_now(),
                                prepared.binding, prepared.bundle_digest, prepared.object_id, operation_id,
                                status.kind, last_seen_of(status))
                    store.insert_link(link)
                else:
                    if _frozen_result(link.last_seen):
                        payload = status.to_payload()
                        seen = {**link.last_seen, "at": utc_now(), "control_state": payload["control_state"],
                                "results_state": payload.get("results_state"), "resume": seen_resume(payload.get("resume"))}
                    else:
                        seen = last_seen_of(status)
                    store.set_last_seen("operation", operation_id, self.pair, self.state_root, seen)
                if status.kind == "campaign":
                    # The target requirement lives in its own table, not in last_seen (ADR-0017, spec §2.4).
                    store.set_link_resume_target(link.dedupe_key(), (status.to_payload().get("resume") or {}).get("target"))
                    campaigns.append(operation_id)
        if campaigns:
            await self._record_exports(campaigns)
        tasks = await self.owner.client_tasks()
        targets = await self.owner.client_targets()
        with store.transaction():
            for target_id, bundle_digest, ref in targets:
                parsed = parse_client_ref(ref)
                if parsed is None or (project_id is not None and parsed[0] != project_id):
                    continue
                seen = {"at": utc_now(),
                        "tasks": [{**t.to_payload(), "position": i} for i, t in enumerate(tasks) if t.target_id == target_id]}
                if store.link_for("target", target_id, self.pair, self.state_root) is None:
                    bundle_matches = [l for l in store.links(parsed[0], implementation=parsed[1], kind="bundle")
                                      if l.object_id == bundle_digest and l.pair == self.pair
                                      and l.state_root == self.state_root]
                    bundle = bundle_matches[-1] if bundle_matches else None
                    if bundle is None:
                        continue
                    store.insert_link(Link("target", parsed[0], parsed[1], self.pair, self.state_root, utc_now(),
                                           bundle.binding, bundle_digest, None, target_id, None, seen))
                else:
                    store.set_last_seen("target", target_id, self.pair, self.state_root, seen)

    async def _record_exports(self, operation_ids) -> None:
        """Record each campaign cycle's export with the Owner start it belongs to (spec §3.10
        step ``result``, ADR-0015). One ``Owner.cycle_starts`` read gives the start key and
        that start's export together, so they never mix starts. After ``delete_results`` Owner still
        reports the id, marked deleted, so the recorded id stays (W55); whether it was deleted is
        read from Owner here and from ``last_seen.results_state`` elsewhere."""
        store = self.projects.store
        try:
            starts = await self.owner.cycle_starts()
        except OwnerError:
            # Keep what was recorded: it carries its start key, so after a resume it no
            # longer matches the current start and never counts for it. The next sync asks again.
            return
        with store.transaction():
            for operation_id in operation_ids:
                link = store.link_for("operation", operation_id, self.pair, self.state_root)
                current = starts.get(operation_id)
                if link is None or current is None:
                    continue
                store.set_link_export(link.dedupe_key(), current["export_id"], current["start"])

    async def operation_ids(self, project_id: str, implementation_id: str | None = None) -> tuple[str, ...]:
        result = []
        for operation_id, ref in await self.owner.client_operations():
            parsed = parse_client_ref(ref)
            if parsed and parsed[0] == project_id and implementation_id in (None, parsed[1]):
                result.append(operation_id)
        return tuple(result)

    async def active_operation(self, project_id: str, implementation_id: str | None = None) -> str | None:
        for operation_id in await self.operation_ids(project_id, implementation_id):
            status = await self.owner.status(ResourceRef("operation", operation_id))
            if status.control_state in _ACTIVE:
                return operation_id
        return None

    async def operations(self, project_id: str, implementation_id: str | None = None) -> tuple[Link, ...]:
        self.projects.get(project_id)
        await self.sync(project_id)
        return self.projects.store.links(project_id, implementation=implementation_id, kind="operation")

    def _here(self, link: Link) -> bool:
        return link.pair == self.pair and link.state_root == self.state_root

    async def _summaries(self, ids) -> dict[str, dict]:
        """Owner's summary rows of this pair's operations; none when Owner cannot answer."""
        from evolution_lab.owner.models import OwnerError
        if not ids:
            return {}
        try:
            return {row["id"]: row for row in await self.owner.list_summaries(set(ids))}
        except OwnerError:
            return {}

    async def operations_summary(self, project_id: str, implementation_id: str | None = None) -> dict:
        """``lab-project-operation-list/2``: one row per operation link with its label and this
        Owner's summary. Another pair's operation is never opened (ADR-0017): ``operation`` is
        null and ``outcome`` comes from the link's stored ``last_seen.result`` snapshot. A cycle
        number is the campaign's position among all campaign links of the project (store order,
        all pairs and implementations), so a filter never renumbers a cycle (ruling R7)."""
        from evolution_lab.owner.summary import short_outcome_of_result
        every = await self.operations(project_id)
        project = self.projects.get(project_id)
        numbers = _cycle_positions(every)
        links = [link for link in every if implementation_id in (None, link.implementation)]
        summaries = await self._summaries({link.object_id for link in links if self._here(link)})
        rows = []
        for link in links:
            if link.operation_kind == "campaign":
                label = f"{project.name} · evolve · цикл {numbers[link.dedupe_key()]}"
            else:
                label = f"{project.name} · {link.operation_kind}"
            here = self._here(link)
            operation = summaries.get(link.object_id) if here else None
            state = "present" if operation is not None else ("unavailable" if here else "other_pair")
            outcome = operation["outcome"] if operation is not None else (
                None if here else short_outcome_of_result(link.operation_kind, (link.last_seen or {}).get("result")))
            rows.append({"link": link.to_payload(), "label": label, "operation_state": state,
                         "operation": operation, "outcome": outcome})
        return {"schema": "lab-project-operation-list/2", "project": project.id, "operations": rows}

    # facts
    async def facts(self, project_id: str, implementation_id: str | None = None):
        from .facts import FactContext, OperationView, TaskView, compute_facts
        from ._facts_bundle import current_bundle, linked_basis
        from .package import validate_form
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        await self.sync(project.id)
        store = self.projects.store
        entry = None if implementation.package_entry is None else self.projects.root_path(project) / implementation.package_entry
        place = self.projects.place_path(project, implementation)

        def gather():
            hashes = BufferedHashCache(store)
            revision = materials_revision(project, cache=hashes)
            present = entry is not None and entry.is_file()
            reason = (None if present else "not_configured" if entry is None
                      else "not_a_file" if entry.exists() else "missing")
            errors = None
            if present:
                try:
                    errors = tuple(validate_form(read_package_entry(entry), place=place))
                except ProjectError:
                    errors = ({"code": "package_unavailable", "detail": "entry unreadable"},)
            place_available = place.is_dir() and os.access(place, os.R_OK | os.X_OK)
            current: dict[str, tuple] = {}

            def now(binding):
                key = binding["inputs"]["digest"]
                if key not in current:
                    current[key] = _current_inputs(project, binding, store, implementation,
                                                   self._service_environment, cache=hashes)
                return current[key]
            order = 0
            # Target tasks of the current pair+state_root are ranked by Owner's own recording
            # order ("position", set in sync()), not by which target's link this loop happens to
            # reach first — an older link can otherwise outrank a task Owner recorded later for a
            # newer one (fix round 1, I1). Tasks of other pairs/state roots keep plain link order
            # and always sort before the current-context ones.
            other_tasks, current_tasks = [], []
            operations, campaigns = [], []
            # Capture related registry rows together, then release the lock before I/O.
            with store.transaction():
                links = store.links(project.id, implementation=implementation.id)
                targets = store.link_resume_targets(project.id)
                recorded_inputs = {digest: store.inventory(digest) or ()
                                   for digest in {link.binding["inputs"]["digest"] for link in links}}
            bundle_cache = {}
            def basis(link):
                return linked_basis(link, pair=self.pair, state_root=self.state_root,
                                    storage=self.preparation._storage, cache=bundle_cache)
            current_digest, current_basis = current_bundle(
                project, implementation, entry=entry, errors=errors, links=links, pair=self.pair,
                state_root=self.state_root, storage=self.preparation._storage, registry=hashes,
                service_environment=self._service_environment, cache=bundle_cache)
            for link in links:
                recorded = recorded_inputs[link.binding["inputs"]["digest"]]
                digest, entries = now(link.binding)
                if link.kind == "target":
                    same_context = link.pair == self.pair and link.state_root == self.state_root
                    for item in (link.last_seen or {}).get("tasks", ()):
                        args = (item["task_id"], item["action"], item["state"], link.object_id, link.pair,
                                link.binding, link.binding["inputs"]["digest"], recorded, digest, entries,
                                link.bundle_digest, basis(link))
                        if same_context:
                            current_tasks.append((item.get("position", 0), args))
                        else:
                            other_tasks.append(args)
                if link.kind == "operation":
                    order += 1
                    seen = link.last_seen or {}
                    view = OperationView(link.object_id, link.operation_kind or seen.get("kind"), link.pair,
                                         seen.get("control_state"), seen.get("result"), link.binding,
                                         link.binding["inputs"]["digest"], recorded, digest, entries, order,
                                         link.bundle_digest, basis(link))
                    operations.append(view)
                    if view.kind == "campaign":
                        campaigns.append({"operation_id": link.object_id, "pair": link.pair.to_payload(),
                                          "control_state": seen.get("control_state"),
                                          "resume": registry_resume(seen.get("resume"), targets.get(link.dedupe_key())),
                                          "results_state": seen.get("results_state"),
                                          "detail": "available" if link.pair == self.pair else "unavailable"})
            current_tasks.sort(key=lambda entry: entry[0])
            ordered_task_args = other_tasks + [args for _, args in current_tasks]
            tasks = tuple(TaskView(*args[:10], order=i, bundle_digest=args[10], measurement_basis=args[11])
                          for i, args in enumerate(ordered_task_args))
            with store.transaction():
                hashes.flush()
            return (revision, present, reason, errors, place_available, tasks, tuple(operations), tuple(campaigns),
                    current_digest, current_basis)
        (revision, present, reason, errors, place_available, tasks, operations, campaigns,
         current_digest, current_basis) = await self._run_io(gather)
        confirmations, confirmation_now, acceptances, inventories = await self._run_io(
            lambda: self._attestations(project, implementation, revision))
        ctx = FactContext(project=project, implementation=implementation, pair=self.pair, revision=revision,
                          package_present=present, package_errors=errors, place_available=place_available,
                          tasks=tasks, operations=operations, campaigns=campaigns, confirmations=confirmations,
                          confirmation_now=confirmation_now, acceptances=acceptances, inventories=inventories,
                          package_entry_path=implementation.package_entry, package_reason=reason,
                          current_bundle_digest=current_digest, current_measurement_basis=current_basis)
        return revision, compute_facts(ctx)

    def _attestations(self, project, implementation, revision):
        from .attest import current_digests
        store = self.projects.store
        confirmations = store.confirmations(project.id)
        now = {}
        for confirmation in confirmations:
            if confirmation.revoked is not None:
                continue
            try:
                now[confirmation.id] = current_digests(self.projects, project, implementation, revision,
                                                       attests_path=confirmation.attests["path"],
                                                       linked=confirmation.linked)
            except ProjectError:
                now[confirmation.id] = {"attests": None, "linked": [dict(l, digest=None) for l in confirmation.linked]}
        acceptances = store.acceptances(project.id)
        inventories = {}
        for acceptance in acceptances:
            for entry in acceptance.facts:
                digest = entry["to"].get("inputs_digest")
                if digest and digest not in inventories:
                    inventories[digest] = store.inventory(digest) or ()
        return confirmations, now, acceptances, inventories

    # confirmations and acceptance of changes (Task 8)
    async def confirm(self, project_id, implementation_id, *, claim, attests, linked, channel, author, note,
                      approval: Approval | None = None, request_id: str | None = None):
        # implementation_id=None means a project-level confirmation (spec §3.7): no default
        # lookup, so it stays buildable on a project with several active implementations
        # (fix round 1, I2). Naming an implementation still resolves and validates it as usual;
        # a linked "place" ref like "implementation:<id>" resolves against the revision either way.
        from .attest import make_confirmation
        project = self.projects.ready(project_id)
        implementation = None if implementation_id is None else self.projects.implementation(project, implementation_id)
        def record():
            store = self.projects.store
            hashes = BufferedHashCache(store)
            revision = materials_revision(project, cache=hashes)
            confirmation = make_confirmation(self.projects, project, implementation, claim=claim,
                                             attests=attests, linked=linked, channel=channel, author=author,
                                             note=note, revision=revision)
            with store.transaction():
                hashes.flush()
                store.put_confirmation(confirmation)
                if approval is not None:
                    store.insert_approval(project_id, approval=approval, request_id=request_id,
                                          resource_kind="confirmation", resource_id=confirmation.id, outcome="applied")
            return confirmation
        async with self._mutation_lock:
            return await self._run_io(record)

    async def project_approvals(self, project_id: str) -> list[dict]:
        self.projects.get(project_id)
        return self.projects.store.project_approvals(project_id)

    async def confirmations(self, project_id, *, revoked: bool = False):
        self.projects.get(project_id)
        return tuple(c for c in self.projects.store.confirmations(project_id) if revoked or c.revoked is None)

    async def revoke_confirmation(self, project_id, confirmation_id, reason, *,
                                  approval: Approval | None = None, request_id: str | None = None):
        async with self._mutation_lock:
            store = self.projects.store
            for confirmation in store.confirmations(project_id):
                if confirmation.id == confirmation_id:
                    if confirmation.revoked is not None:
                        return confirmation  # re-revoke is a no-op: the first revocation stands (R-a)
                    revoked = replace(confirmation, revoked={"at": utc_now(), "reason": reason})
                    with store.transaction():
                        store.put_confirmation(revoked)
                        if approval is not None:
                            store.insert_approval(project_id, approval=approval, request_id=request_id,
                                                  resource_kind="confirmation", resource_id=confirmation_id,
                                                  outcome="applied")
                    return revoked
            raise ProjectError("project_not_found")

    async def accept(self, project_id, implementation_id, *, only, across_pairs, reason, channel, author, dry_run,
                     expected: list[dict] | None = None, approval: Approval | None = None,
                     request_id: str | None = None):
        from .attest import accept
        if approval is not None and not dry_run and expected is None:
            raise ProjectError("invalid_request")
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)

        def collect_inventories(facts):
            # The inventories are content-addressed rows (INSERT OR IGNORE): one stored under
            # ``changes_to`` must be exactly what that digest names. The sources are re-read here,
            # after facts(); if they moved in between, refuse rather than poison the row (final
            # review I4) — the caller repeats with fresh facts.
            hashes = BufferedHashCache(self.projects.store)
            inventories = {}
            for fact in facts:
                if fact.state and fact.state.get("inputs_digest") and fact.stale_reason is not None:
                    changes_to = fact.state["inputs_digest"]
                    digest, entries = _entries_for_state(project, fact, self.projects.store,
                                                      self._service_environment, cache=hashes)
                    if digest != changes_to:
                        raise ProjectError("inputs_changed")
                    inventories[changes_to] = entries
            with self.projects.store.transaction():
                hashes.flush()
            return inventories

        def record(facts, inventories):
            store = self.projects.store
            with store.transaction():
                acceptance = accept(self.projects, project, implementation, facts=facts, only=only, across_pairs=across_pairs,
                                    reason=reason, channel=channel, author=author, dry_run=dry_run, inventories=inventories,
                                    expected=expected)
                if approval is not None and acceptance.id is not None:
                    store.insert_approval(project_id, approval=approval, request_id=request_id,
                                          resource_kind="acceptance", resource_id=acceptance.id, outcome="applied")
                return acceptance

        if not dry_run and expected is not None:
            # ADR-0024: facts, inventories, planning, comparison and write share the
            # existing session-wide lock. facts() itself does not acquire this lock.
            async with self._mutation_lock:
                _, facts = await self.facts(project.id, implementation.id)
                inventories = await self._run_io(lambda: collect_inventories(facts))
                return record(facts, inventories)
        _, facts = await self.facts(project.id, implementation.id)
        inventories = await self._run_io(lambda: collect_inventories(facts))
        if dry_run:
            return record(facts, inventories)
        async with self._mutation_lock:
            return record(facts, inventories)

    async def revoke_acceptance(self, project_id, acceptance_id, reason, *,
                                approval: Approval | None = None, request_id: str | None = None):
        async with self._mutation_lock:
            store = self.projects.store
            for acceptance in store.acceptances(project_id):
                if acceptance.id == acceptance_id:
                    if acceptance.revoked is not None:
                        # Re-revoke is a no-op: moving ``revoked.at`` past a later re-acceptance
                        # would make _carry cut that new chain too (codex review, R-a).
                        return acceptance
                    revoked = replace(acceptance, revoked={"at": utc_now(), "reason": reason})
                    with store.transaction():
                        store.put_acceptance(revoked)
                        if approval is not None:
                            store.insert_approval(project_id, approval=approval, request_id=request_id,
                                                  resource_kind="acceptance", resource_id=acceptance_id,
                                                  outcome="applied")
                    return revoked
            raise ProjectError("project_not_found")

    async def facts_payload(self, project_id: str, implementation_id: str | None = None) -> dict:
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        revision, facts = await self.facts(project.id, implementation.id)
        return {"schema": "lab-project-facts/5", "project": project.id, "implementation": implementation.id,
                "pair": self.pair.to_payload(), "revision": revision.to_payload(),
                "facts": [fact.to_payload() for fact in facts]}

    async def status_payload(self, project_id: str, implementation_id: str | None = None,
                             scenario: str | None = None) -> dict:
        from evolution_lab.guides import scenarios
        from .status import status_payload
        project = self.projects.ready(project_id)  # a missing project wins over a bad scenario
        implementation = self.projects.implementation(project, implementation_id)
        catalogue = await self._run_io(scenarios)
        if scenario is not None and catalogue.descriptor(scenario) is None:
            raise ProjectError("invalid_request")
        _, facts = await self.facts(project.id, implementation.id)  # syncs links and exports first
        starts = await self.owner.cycle_starts()  # one read: start keys and their exports
        sources = _fact_operations(facts)
        summaries = await self._summaries({  # one read: this pair's cycles and the operations behind facts
            link.object_id for link in self.projects.store.links(project.id, implementation=implementation.id, kind="operation")
            if self._here(link) and (link.operation_kind == "campaign" or link.object_id in sources)})
        inputs = await self._run_io(lambda: self._status_inputs(project, implementation, facts, catalogue, starts,
                                                                summaries))
        return status_payload(project_id=project.id, implementation_id=implementation.id, catalogue=catalogue,
                              inputs=inputs, scenario=scenario)

    def _status_inputs(self, project, implementation, facts, catalogue, starts, summaries):
        from evolution_lab.guides._scenarios import inside_root, substitute
        from evolution_lab.owner.summary import short_outcome_of_result
        from .status import CycleView, StatusInputs
        store = self.projects.store
        hashes = BufferedHashCache(store)
        root = self.projects.root_path(project)
        outputs: list[tuple[str, str, dict]] = []
        needs_bytes: dict[str, bool] = {}
        for descriptor in catalogue.descriptors:
            for step in descriptor["steps"]:
                for output in step["outputs"]:
                    body = output.get("artifact")
                    if body is None or body["path"].startswith("@"):
                        continue
                    path = substitute(body["path"], implementation.id)
                    if not inside_root(path):  # the substituted result stays inside the materials root (§2.2)
                        continue
                    outputs.append((descriptor["id"], path, body))
                    needs_bytes[path] = needs_bytes.get(path, False) or body["check"] == "schema"
        # One observation per path per request (codex r1 of the dev38 spec): every output on it is judged by it.
        seen = {path: observe_file(root / path, read=read, cache=hashes) for path, read in needs_bytes.items()}
        parsed = {path: _parse(look) for path, look in seen.items() if look.data is not None}
        artifacts = {path: look.digest for path, look in seen.items() if look.state in ("read", "unreadable")}
        checks = {}
        for scenario_id, path, body in outputs:
            if body["check"] != "schema":
                continue
            found = _artifact_check(catalogue, scenario_id, body["schema"], seen[path], parsed.get(path, (None, None)))
            if found is not None:  # the whole identity of a check: one path may have several schemas
                checks[(scenario_id, path, body["schema"])] = found
        manifest_ok = False
        if implementation.package_entry is not None and any(f.id == "package.valid" and f.holds for f in facts):
            try:
                entry = read_package_entry(root / implementation.package_entry)
                manifest_ok = getattr(entry.source, "manifest_path", None) is not None
            except ProjectError:
                manifest_ok = False
        with store.transaction():
            exports = store.link_exports(project.id)
            targets = store.link_resume_targets(project.id)
            links = store.links(project.id, implementation=implementation.id)
            positions = _cycle_positions(store.links(project.id, kind="operation"))
        here_operations = {link.object_id for link in links if link.kind == "operation" and self._here(link)}
        moments = _moments(links, facts, here_operations, summaries)
        prepared, cycles, unknown = [], [], []
        for link in links:
            here = self._here(link)
            if link.kind == "prepared" and here and link.operation_kind == "campaign":
                digest, _ = _current_inputs(project, link.binding, store, project.implementation(link.implementation),
                                            self._service_environment, cache=hashes)
                if digest == link.binding["inputs"]["digest"]:
                    prepared.append(link.object_id)
            elif link.kind == "operation" and link.operation_kind == "campaign":
                seen = link.last_seen or {}
                current = starts.get(link.object_id) if here else None
                if current is not None:
                    # The export of the current start, its deletion mark and results_state, from
                    # the same read as its key (C1).
                    export_id, order = current["export_id"], current["start"]
                    results_state, export_deleted = current["results_state"], current["export_deleted"]
                else:
                    # Not this Owner's: the recorded (export, start) pair and the last seen
                    # results_state are the cycle's snapshot (ADR-0017).
                    export_id, order = (exports.get(link.dedupe_key()) or (None, None))[0], 0
                    results_state = seen.get("results_state")
                    export_deleted = export_id is not None and results_state == "deleted"
                    unknown.append(len(cycles))
                summary = summaries.get(link.object_id) if here else None
                outcome = None if summary is None else summary["outcome"]
                last_event_at = None if summary is None else (summary["times"] or {}).get("last_event_at")
                if outcome is None:
                    # No outcome from Owner (another pair, an unreadable or deleted result): the registry's snapshot,
                    # which sync() keeps for a terminal campaign (ADR-0017; spec 2026-10-01 §2.4).
                    outcome = short_outcome_of_result(link.operation_kind, seen.get("result"))
                cycles.append(CycleView(link.object_id, link.pair, here, seen.get("control_state"),
                                        registry_resume(seen.get("resume"), targets.get(link.dedupe_key())),
                                        link.bundle_digest, link.prepared_digest, export_id, order,
                                        outcome, last_event_at,
                                        export_deleted=export_deleted, results_state=results_state,
                                        position=positions.get(link.dedupe_key(), 0)))
        # Spec §3.10: order = this Owner's order of the operation's latest start (a resume is a
        # new start, ADR-0015). Cycles this Owner does not know keep their link order, below
        # every known cycle (Owner keys are >= 1).
        for position, index in enumerate(unknown):
            cycles[index] = replace(cycles[index], order=position - len(unknown))
        with store.transaction():
            hashes.flush()
        return StatusInputs(self.pair, tuple(facts), artifacts, manifest_ok, tuple(prepared), tuple(cycles),
                            moments, implementation=implementation.id, checks=checks)

    async def update(self, project_id: str, mutation: Mapping, *, expected_record_version=None):
        # Held from the active-operation read through the registry write (R12 fix 3): otherwise
        # a start() in flight for the same project could accept from Owner *after* this read but
        # *before* projects.update() lands, so archive/move would win a race it must lose.
        async with self._mutation_lock:
            active = {None: await self.active_operation(project_id)}
            target = mutation.get("implementation") if isinstance(mutation, Mapping) else None
            if isinstance(target, str):
                active[target] = await self.active_operation(project_id, target)
            return self.project_payload(self.projects.update(
                project_id, mutation, expected_record_version=expected_record_version,
                active=lambda project, implementation: active.get(implementation)))

    # registry reads and creation for the service routes (Task 9); the wire form is lab-project/2 (dev35)
    def project_payload(self, project) -> dict:
        """``lab-project/2`` of a registry record, with its ports from the ``ports`` table."""
        from .models import project_wire
        return project_wire(project, self.projects.store.ports(project.id))

    async def create(self, **kwargs):
        # A clone is slow, so creation runs on the IO executor, never on the service loop.
        return self.project_payload(await self._run_io(lambda: self.projects.create(**kwargs)))

    async def list_projects(self, *, archived: bool):
        from .models import PROJECT_LIST_SCHEMA
        return {"schema": PROJECT_LIST_SCHEMA,
                "projects": [self.project_payload(project) for project in self.projects.list(archived=archived)]}

    async def get_project(self, project_id: str):
        return self.project_payload(self.projects.get(project_id))

    # presentation (part 3)
    async def open_workspace(self, project_id: str, *, autonomy: str = "ask") -> dict:
        from .workspace import build_workspace
        async with self._mutation_lock:
            project = self.projects.ready(project_id)  # project_not_found | project_not_ready | archived
            path = await self._run_io(lambda: build_workspace(
                self.projects.home, project, pair=self.pair, socket_path=self._socket_path, now=utc_now(),
                autonomy=autonomy))
        return {"schema": "lab-project-workspace/1", "path": str(path)}

    async def port(self, project_id: str, *, source: str, template: str, ref: str | None = None,
                   port_id: str | None = None, into: str | None = None) -> dict:
        """``lab project port`` (spec 2026-10-01 §3): the checks, then the slow template fetch outside the mutation
        lock, then the recheck, ``rename`` and registry transaction inside it (ADR-0020 p. 5)."""
        from . import port as porting
        plan = await self._run_io(lambda: porting.plan_port(self.projects, project_id, source_id=source,
                                                            template=template, ref=ref, port_id=port_id, into=into))
        # Its own thread, not the service's shared IO executor: a remote fetch may take minutes and touches no
        # registry state, so it must not queue every other project and preparation call behind it.
        staged = await asyncio.to_thread(porting.stage, self.projects.home, plan)
        async with self._mutation_lock:
            project = await self._run_io(lambda: porting.publish(self.projects, plan, staged))
        return self.project_payload(project)

    async def sync_check(self, project_id: str, *, rev: str | None = None) -> dict:
        """``lab project sync`` (PDR-0024): fetch off the mutation lock on its own thread, then the report. Runs to its
        end even when the caller is cancelled (spec §2.2 p. 9)."""
        from . import sync
        from .clone import _option_like
        if rev is not None and (not isinstance(rev, str) or not rev or _option_like(rev)):
            raise ProjectError("invalid_request")

        async def work():
            async with self._copy_locks.setdefault(project_id, asyncio.Lock()):
                project, clone = await self._run_io(lambda: self.projects.sync_clone(project_id))
                await asyncio.to_thread(sync.fetch, clone)
                base = project.working_copy.source["base_revision"]
                return await self._run_io(lambda: sync.inspect(clone, project_id=project_id, base_revision=base, rev=rev))
        return await _to_the_end(work())

    async def sync_apply(self, project_id: str, *, upstream: str,
                         approval: Approval | None = None, request_id: str | None = None) -> dict:
        """``lab project sync --apply SHA``: merge exactly ``upstream`` under the copy and mutation locks; git is the
        source of truth, a failed registry write after the merge is ``sync_not_recorded`` (spec §2.2 p. 7)."""
        from . import sync
        if not isinstance(upstream, str) or not sync._SHA.fullmatch(upstream):
            raise ProjectError("invalid_request")

        async def work():
            async with self._copy_locks.setdefault(project_id, asyncio.Lock()):
                async with self._mutation_lock:
                    project, clone = await self._run_io(lambda: self.projects.sync_clone(project_id))
                    source = project.working_copy.source
                    store = self.projects.store
                    approval_id = None
                    if approval is not None:
                        approval_id = await self._run_io(lambda: store.insert_approval(
                            project_id, approval=approval, request_id=request_id, resource_kind="sync",
                            resource_id=upstream, outcome="admitted"))
                    try:
                        result = await self._run_io(lambda: sync.integrate(
                            clone, branch=source["branch"], base_revision=source["base_revision"], upstream=upstream))
                    except ProjectError as error:
                        if approval_id is not None and error.code != "sync_not_recorded":
                            try:
                                await self._run_io(lambda: store.set_approval_outcome(approval_id, "failed"))
                            except (OSError, sqlite3.Error):
                                _LOG.warning("could not record the approval outcome for a refused sync",
                                             extra={"lab_code": error.code})
                        raise
                    conditions = {} if approval_id is None else {"approval_id": approval_id}
                    try:
                        updated = await self._run_io(lambda: self.projects.record_sync(project_id, upstream, **conditions))
                    except Exception:  # every failure, ProjectError included, leaves the merge unrecorded (spec §2.2 p. 7)
                        _LOG.exception("sync merged %s but the registry write failed", upstream[:12],
                                       extra={"lab_code": "sync_not_recorded"})
                        raise ProjectError("sync_not_recorded") from None
            return {"schema": "lab-project-sync-result/1", **result, "project": self.project_payload(updated)}
        return await _to_the_end(work())

    async def demo(self, project_id: str | None):
        """The free demo project (spec §3.12). The route checks ``service_environment``
        is configured before calling here (rulings, Task 3): this method assumes it is."""
        environment = self._service_environment
        async with self._mutation_lock:
            return self.project_payload(await self._run_io(lambda: self.projects.create_demo(
                id=project_id, environment_digest=environment.digest,
                executable=environment.descriptor.executable)))

    # probe sessions (spec 2026-10-02 §3, §4); the service is the only writer of their history (§3.6)
    def _probes(self) -> ProbeStore:
        if self._probe_abandoned.is_set():
            # Shutdown let go of the probe calls: a late one neither writes nor reopens the history (§5.1).
            raise OwnerError("service_stopping", "probe")
        if self._probe_store is None:
            self._probe_store = ProbeStore.open(self.projects.home, durability=self._durability)
        return self._probe_store

    def stop_probes(self) -> None:
        """Shutdown begins: a probe call still waiting for the probe lock is refused ``service_stopping`` (§5.1)."""
        self._probe_stopping = True

    def interrupt_probes(self) -> None:
        """The Owner is about to close: a turn it interrupts is not recorded now; it stays pending and its session
        open, and the next start records it ``interrupted`` and closes the session ``target_gone`` (§5.1)."""
        self._probe_stopping = self._probe_interrupted = True

    def abandon_probes(self) -> None:
        """The Owner is closed (§5.1): a probe call still waiting on an Owner open, turn or close answers
        ``service_stopping`` now instead of holding the shutdown, and a probe call that reaches for the history
        after that is refused ``service_stopping`` (the history is never reopened). The Owner's late settlement goes
        on without it; its row stays ``uncertain`` and its history session open, which the next start closes
        ``target_gone``."""
        self._probe_stopping = self._probe_interrupted = True
        self._probe_abandoned.set()

    async def _owner_probe(self, call):
        """Await one Owner probe call (``call`` is its coroutine) unless shutdown abandons the probe calls first:
        then the wait ends with ``service_stopping`` (the Owner holds its own work; nothing is cancelled there)."""
        work = asyncio.ensure_future(call)
        abandoned = asyncio.ensure_future(self._probe_abandoned.wait())
        try:
            await asyncio.wait((work, abandoned), return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # the caller is cancelled: the wait ends as a direct await would
            if work.done():
                if not work.cancelled():
                    work.exception()  # retrieved: the caller's cancellation is the answer
            else:
                work.cancel()
            raise
        finally:
            abandoned.cancel()
        if not work.done():
            work.cancel()  # only this wait: the Owner call shields its Owner-held task
            raise OwnerError("service_stopping", "probe")
        return work.result()

    def _refuse_when_stopping(self) -> None:
        if self._probe_stopping:
            raise OwnerError("service_stopping", "probe")

    def close(self) -> None:
        """Release the probe history handle (the service calls it on shutdown)."""
        if self._probe_store is not None:
            self._probe_store.close()
            self._probe_store = None

    def _probe_row(self, session_id: str) -> dict:
        row = self._probes().session(session_id)
        if row is None:
            raise ProjectError("probe_session_not_found")
        return row

    def _probe_form(self, row: dict, *, turns: bool) -> dict:
        record, store = row["record"], self._probes()
        used = store.chain_usage(row["id"])
        form = {"schema": PROBE_SESSION_SCHEMA, "id": row["id"], "project": row["project"],
                "implementation": row["implementation"], "state": row["state"], "closed_reason": row["closed_reason"],
                "reset_of": row["reset_of"], "created_at": row["created_at"], "closed_at": row["closed_at"],
                "target": record["target"], "seed": record["seed"], "ceiling": _ceiling_view(record, used),
                "budget": record["budget"], "tokens_incomplete": used["tokens_incomplete"],
                "cost_unavailable": record["budget"] is not None and used["cost_unavailable"],
                "turn_timeout_seconds": record["turn_timeout_seconds"]}
        if turns:
            form["turns"] = store.turns(row["id"])  # each finished with its ceiling, in one write
        return form

    async def _live_target(self, project_id: str, implementation_id: str):
        """The ready target this service runs for the pair (project, implementation) (§3.2)."""
        ref = client_ref(project_id, implementation_id)
        mine = {target_id for target_id, _, label in await self.owner.client_targets() if label == ref}
        ready = [target for target in await self.owner.list_targets(state="ready") if target.id in mine]
        if not ready:
            raise ProjectError("target_not_ready")
        return ready[0]

    async def _open_probe(self, project_id, implementation_id, *, ceiling, budget, turn_timeout_seconds, seed,
                          reset_of=None) -> dict:
        target = await self._live_target(project_id, implementation_id)
        # The whole transition runs to its end even when the caller goes away: the Owner open, then the history
        # row, or the Owner close on any failure. Otherwise a cancelled caller leaves an Owner probe without
        # history, holding an instance slot and blocking the target (§3.2).
        return await _to_the_end(self._open_probe_transition(
            target, project_id, implementation_id, ceiling=ceiling, budget=budget,
            turn_timeout_seconds=turn_timeout_seconds, seed=seed, reset_of=reset_of))

    async def _open_probe_transition(self, target, project_id, implementation_id, *, ceiling, budget,
                                     turn_timeout_seconds, seed, reset_of) -> dict:
        session_id = uuid.uuid4().hex
        try:
            opened = await self._owner_probe(self.owner.probe_open(
                TargetRef(target.id, target.owner_generation), session_id=session_id, seed=seed,
                turn_timeout_seconds=turn_timeout_seconds))
        except OwnerError as error:
            if error.code == "target_not_running":
                raise ProjectError("target_not_ready") from None
            if error.code == "resource_busy" and error.stage == "probe":
                raise ProjectError("probe_limit_reached") from None
            if error.code == "invalid_argument" and error.stage == "probe" and seed is not None:
                # The arguments Lab passes are valid by construction; the one the caller controls is a seed,
                # refused by a target whose seed_mode is not ``exact`` (§3.2).
                raise ProjectError("invalid_request") from None
            raise
        record = {"target": {"id": opened.target_id, "owner_generation": opened.owner_generation,
                             "boot_id": opened.boot_id, "bundle_digest": opened.bundle_digest,
                             "launch_digest": opened.launch_digest,
                             "source": "entry" if opened.source_kind == "probe" else "package",
                             "seed_mode": opened.seed_mode},
                  "seed": {"requested": opened.requested_seed, "effective": opened.effective_seed},
                  "ceiling": ceiling, "budget": budget, "turn_timeout_seconds": turn_timeout_seconds}
        try:
            self._probes().create_session({"id": session_id, "project": project_id, "implementation": implementation_id,
                                           "reset_of": reset_of, "created_at": utc_now(), "record": record})
        except BaseException:
            # A session without history must not keep the target's instance (§3.2): close and release it.
            try:
                await self._owner_probe(self.owner.probe_close(session_id))
            except Exception:
                pass
            raise
        return self._probe_form(self._probe_row(session_id), turns=True)

    async def probe_start(self, project_id: str, implementation_id: str, *, ceiling: dict, budget: dict | None,
                          turn_timeout_seconds: float = 120, seed: int | None = None) -> dict:
        """Open a probe session on the implementation's ready target: ``lab-probe-session/1``."""
        project = self.projects.ready(project_id)
        implementation = self.projects.implementation(project, implementation_id)
        ceiling, budget = _probe_ceiling(ceiling), _probe_budget(budget)
        if "max_cost" in ceiling and budget is None:
            raise ProjectError("invalid_request")
        turn_timeout_seconds = _probe_timeout(turn_timeout_seconds)
        if seed is not None and (type(seed) is not int or not 0 <= seed < SEED_BOUND):
            raise ProjectError("invalid_request")
        await self.probe_reconcile()
        return await self._open_probe(project.id, implementation.id, ceiling=ceiling, budget=budget,
                                      turn_timeout_seconds=turn_timeout_seconds, seed=seed)

    async def _ledger_mark(self, budget: dict | None) -> int | None:
        if budget is None:
            return None
        try:
            return await self._run_io(lambda: ledger_mark(Path(budget["ledger_path"])))
        except GatewayLedgerUnavailable:
            return None

    async def _turn_cost(self, budget: dict, before: int | None) -> tuple[dict | None, int | None]:
        """The turn's ``cost`` and its units from the gateway window, or ``(None, None)`` when unreadable."""
        if before is None:
            return None, None
        path = Path(budget["ledger_path"])
        try:
            units, upper = await self._run_io(lambda: window_cost(
                path, after=before, until=ledger_mark(path), roles=budget["roles"]))
        except GatewayLedgerUnavailable:
            return None, None
        return {"amount": units_to_amount(units), "currency": budget["currency"], "attribution": "ledger_window",
                "upper_bound": upper}, units

    async def probe_turn(self, session_id: str, content: str) -> dict:
        """One turn: ``lab-probe-turn/1``. The ceiling of the reset chain is checked before the turn (§4.4)."""
        _probe_id(session_id)
        if not isinstance(content, str):
            raise ProjectError("invalid_request")
        async with self._probe_lock:
            self._refuse_when_stopping()
            await self.probe_reconcile()
            row = self._probe_row(session_id)
            if row["state"] != "open":
                raise ProjectError("probe_session_closed")
            store = self._probes()
            refusal = check_ceiling(row["record"]["ceiling"], store.chain_usage(session_id))
            if refusal is not None:
                raise ProjectError(refusal)
            numbers = [int(turn["turn_id"].rsplit("/", 1)[1]) for turn in store.turns(session_id)]
            numbers += [n for pending, n in store.pending_turns() if pending == session_id]
            n = max(numbers, default=0) + 1
            # The turn runs to its end under the lock even when the caller goes away: a cancelled caller must
            # neither open the gateway window to the next turn nor leave this turn pending.
            return await _to_the_end(self._run_probe_turn(row, n, content))

    async def _run_probe_turn(self, row: dict, n: int, content: str) -> dict:
        session_id, record, store = row["id"], row["record"], self._probes()
        started_at = utc_now()
        budget = record["budget"]
        self._probe_busy.add(session_id)
        try:
            store.begin_turn(session_id, n, content, started_at)
            mark = await self._ledger_mark(budget)
            try:
                out = await self._owner_probe(
                    self.owner.probe_turn(session_id, content, timeout=record["turn_timeout_seconds"]))
            except OwnerError as error:
                if error.code == "cleanup_required":
                    # The target is quarantined (ADR-0021, §3.5): the Owner sent nothing, so this is not a turn;
                    # the session stays open until a target stop closes it.
                    store.discard_turn(session_id, n)
                # The Owner no longer has the session (a stop raced the turn): reconcile closes it and records
                # this turn as interrupted. Its own failure must not hide the Owner's answer.
                self._probe_busy.discard(session_id)
                try:
                    await self.probe_reconcile()
                except Exception:
                    pass
                if error.code == "not_found":
                    raise ProjectError("probe_session_closed") from None
                raise
            if self._probe_interrupted and out.failure is not None:
                # The Owner close interrupted the turn (§5.1): it stays pending, the session open; the next start
                # records the turn interrupted and closes the session target_gone (an earlier generation).
                raise OwnerError("service_stopping", "probe")
            cost, units = (None, None) if budget is None else await self._turn_cost(budget, mark)
        finally:
            self._probe_busy.discard(session_id)
        usage = out.result.usage if out.result is not None else None
        tokens = usage.total if usage is not None else None
        turn = turn_record(session_id=session_id, n=n, started_at=started_at, finished_at=utc_now(),
                           content=content, result=out.result, failure=out.failure, cost=cost)
        turn = store.finish_turn(session_id, n, turn, tokens=tokens or 0, tokens_known=tokens is not None,
                                 cost_units=units, cost_known=budget is None or cost is not None,
                                 ceiling=lambda used: _ceiling_view(record, used))
        if out.closed is not None:
            store.close_session(session_id, "turn_failed" if out.closed == "released" else "release_failed",
                                utc_now())
        return turn

    async def _release_probe(self, session_id: str) -> str:
        self._probe_busy.add(session_id)
        try:
            return await self._owner_probe(self.owner.probe_close(session_id))
        finally:
            self._probe_busy.discard(session_id)

    async def probe_close(self, session_id: str) -> dict:
        """Close and release: ``closed``, or ``release_failed`` when the settlement is not positive (§3.5)."""
        _probe_id(session_id)
        async with self._probe_lock:
            self._refuse_when_stopping()
            await self.probe_reconcile()
            if self._probe_row(session_id)["state"] == "open":
                answer = await self._release_probe(session_id)
                self._probes().close_session(session_id, "closed" if answer == "released" else "release_failed",
                                             utc_now())
            return self._probe_form(self._probe_row(session_id), turns=True)

    async def probe_reset(self, session_id: str) -> dict:
        """Close the session as ``reset`` and open a new one in the same chain (``reset_of``), same ceiling."""
        _probe_id(session_id)
        async with self._probe_lock:
            self._refuse_when_stopping()
            await self.probe_reconcile()
            row = self._probe_row(session_id)
            if row["state"] != "open":
                raise ProjectError("probe_session_closed")
            # Release, record and replacement open run to their end even when the caller goes away (as a start).
            return await _to_the_end(self._reset_probe(row))

    async def _reset_probe(self, row: dict) -> dict:
        session_id = row["id"]
        answer = await self._release_probe(session_id)
        if answer != "released":
            # The row is uncertain: the Owner admits no new probe on this target until a stop (§3.5).
            self._probes().close_session(session_id, "release_failed", utc_now())
            raise OwnerError("cleanup_required", "before_start")
        self._probes().close_session(session_id, "reset", utc_now())
        record = row["record"]
        return await self._open_probe(row["project"], row["implementation"], ceiling=record["ceiling"],
                                      budget=record["budget"], turn_timeout_seconds=record["turn_timeout_seconds"],
                                      seed=None, reset_of=session_id)

    async def probe_session(self, session_id: str) -> dict:
        """``lab-probe-session/1`` with its turns."""
        _probe_id(session_id)
        await self.probe_reconcile()
        return self._probe_form(self._probe_row(session_id), turns=True)

    async def probe_list(self, project_id: str, implementation_id: str | None = None) -> dict:
        """``lab-probe-session-list/1``: the sessions without their turns, oldest first."""
        project = self.projects.get(project_id)
        if implementation_id is not None and project.implementation(implementation_id) is None:
            raise ProjectError("implementation_not_found")
        await self.probe_reconcile()
        return {"schema": PROBE_SESSION_LIST_SCHEMA,
                "sessions": [self._probe_form(row, turns=False)
                             for row in self._probes().sessions(project.id, implementation_id)]}

    async def probe_reconcile(self) -> None:
        """Close every open session the Owner has released (``target_gone``), holds ``uncertain``
        (``release_failed``) or no longer knows (``target_gone``), and record the pending turns of closed sessions
        as interrupted (§3.5, §5.1). A session opened by an earlier Owner generation is ``target_gone`` whatever
        the Owner holds: its target is gone with that generation. ``admitted`` and ``releasing`` are transient and
        left alone.

        The store is read before the Owner, so a session published meanwhile is not taken for a gone one;
        sessions with an Owner call in flight are left to that call."""
        store = self._probes()
        candidates = store.open_sessions()
        states = await self.owner.probe_states()
        generation = self.owner.owner_generation
        now = utc_now()
        for row in candidates:
            if row["id"] in self._probe_busy:
                continue
            if row["record"]["target"]["owner_generation"] != generation:
                store.close_session(row["id"], "target_gone", now)
                continue
            state = states.get(row["id"])
            if state not in {None, "released", "uncertain"}:
                continue
            store.close_session(row["id"], "release_failed" if state == "uncertain" else "target_gone", now)
        for session_id, n in store.pending_turns():
            if session_id in self._probe_busy:
                continue
            row = store.session(session_id)
            if row["state"] == "closed":
                store.mark_interrupted(session_id, n, now,
                                       ceiling=lambda used, record=row["record"]: _ceiling_view(record, used))
