"""In-memory mutation keys of one Owner generation (PDR-0008, ADR-0009).

Nothing here is persisted or logged: fingerprints may cover secret values.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from evolution_lab.owner.models import Approval

from .codecs import decode_identifier
from .errors import ServiceError, error_response


REQUEST_ID_HEADER = "lab-request-id"
GENERATION_HEADER = "lab-owner-generation"
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
WORK, LIFECYCLE, PROJECT, PROBE = "work", "lifecycle", "project", "probe"
DEFAULT_LIMIT = 10_000
PROBE_LIMIT = 1_000
ROUTE_CLASSES = {
    "confirm_execution_policy": WORK,
    "create_bundle": WORK, "prepare": WORK, "bind": WORK, "start_target": WORK,
    "check": WORK, "conformance": WORK, "start": WORK, "export": WORK, "resume": WORK,
    "stop_target": LIFECYCLE, "stop_task": LIFECYCLE, "stop_operation": LIFECYCLE,
    "delete_results": LIFECYCLE, "cleanup": LIFECYCLE, "release": LIFECYCLE,
    "project_create": PROJECT, "project_update": PROJECT, "project_bundle": PROJECT,
    "project_prepare": PROJECT, "project_target": PROJECT, "project_start": PROJECT,
    "project_confirm": PROJECT, "project_revoke": PROJECT, "project_accept": PROJECT,
    "project_workspace": PROJECT, "project_demo": PROJECT, "project_port": PROJECT,
    "project_sync_check": PROJECT, "project_sync": PROJECT,
    "project_probe_start": PROBE, "probe_turn": PROBE, "probe_reset": PROBE, "probe_close": PROBE,
}


@dataclass(frozen=True, slots=True)
class MutationKey:
    request_id: str
    owner_generation: str


def _header_values(headers, name: str) -> list[str]:
    # Starlette's own getlist() only case-folds the search key, not the stored
    # raw list (ASGI servers are required to hand it over already lowercase),
    # so a mixed-case raw header would not match through getlist(). Fold both
    # sides ourselves via .items() instead; a no-op for already-lowercase ASGI
    # headers, and correct for headers built from mixed-case raw pairs too.
    lowered = name.lower()
    return [value for key, value in headers.items() if key.lower() == lowered]


def decode_mutation_key(headers) -> MutationKey | None:
    ids = _header_values(headers, REQUEST_ID_HEADER)
    generations = _header_values(headers, GENERATION_HEADER)
    if not ids and not generations:
        return None
    if len(ids) != 1 or len(generations) != 1 or REQUEST_ID.fullmatch(ids[0]) is None:
        raise ServiceError("invalid_request")
    return MutationKey(ids[0], decode_identifier(generations[0]))


def reject_mutation_key(headers) -> None:
    if _header_values(headers, REQUEST_ID_HEADER) or _header_values(headers, GENERATION_HEADER):
        raise ServiceError("invalid_request")


def _approval_kwargs(key: MutationKey | None, approval: Approval | None, *,
                     observed_start: int | None = None) -> dict[str, Any]:
    if approval is None:
        return {}
    if key is None:
        raise ServiceError("invalid_request")
    kwargs = {"approval": approval, "request_id": key.request_id}
    if observed_start is not None:
        kwargs["observed_start"] = observed_start
    return kwargs


def fingerprint(route: str, *, path: Mapping[str, str] | None = None,
                query: Mapping[str, str] | None = None, body: Any = None) -> bytes:
    document = {"route": route, "path": dict(path or {}), "query": dict(query or {}), "body": body}
    # ensure_ascii keeps this total: a lone surrogate the JSON decoder accepted
    # becomes an escape instead of failing UTF-8 encoding.
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("ascii")).digest()


@dataclass(slots=True)
class _Entry:
    route: str
    fingerprint: bytes
    future: asyncio.Future


class MutationTable:
    """No eviction: forgetting a key would allow a completed action to run again."""

    def __init__(self, *, work_limit: int = DEFAULT_LIMIT, lifecycle_limit: int = DEFAULT_LIMIT,
                 project_limit: int = DEFAULT_LIMIT, probe_limit: int = PROBE_LIMIT):
        self._limits = {WORK: work_limit, LIFECYCLE: lifecycle_limit, PROJECT: project_limit, PROBE: probe_limit}
        self._counts = {WORK: 0, LIFECYCLE: 0, PROJECT: 0, PROBE: 0}
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, request_id: str, route: str, digest: bytes) -> asyncio.Future | None:
        entry = self._entries.get(request_id)
        if entry is None:
            return None
        if entry.route != route or entry.fingerprint != digest:
            raise ServiceError("request_conflict")
        return entry.future

    def ensure_capacity(self, route: str) -> None:
        kind = ROUTE_CLASSES[route]
        if self._counts[kind] >= self._limits[kind]:
            raise ServiceError("request_table_full")

    def record(self, request_id: str, route: str, digest: bytes, future: asyncio.Future) -> None:
        if request_id in self._entries:
            raise RuntimeError("mutation key already recorded")
        self.ensure_capacity(route)
        entry = _Entry(route, digest, future)
        self._entries[request_id] = entry
        self._counts[ROUTE_CLASSES[route]] += 1
        future.add_done_callback(lambda done: self._settled(request_id, entry, done))

    def _settled(self, request_id: str, entry: _Entry, done: asyncio.Future) -> None:
        # not_found after dispatch means no action happened: the next retry runs again.
        if done.cancelled() or done.exception() is None:
            return
        if error_response(done.exception())["body"]["error"]["code"] != "not_found":
            return
        if self._entries.get(request_id) is entry:
            del self._entries[request_id]
            self._counts[ROUTE_CLASSES[entry.route]] -= 1
