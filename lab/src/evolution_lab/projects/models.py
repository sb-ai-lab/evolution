"""Closed payload models of the project layer (spec §3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

PROJECT_SCHEMA = "lab-project/1"
#: ``state`` of a project record (docs/api/projects.md); the panel's label table is checked against it.
PROJECT_STATES = frozenset({"creating", "ready", "failed"})
LINK_SCHEMA = "lab-project-link/1"
LINK_KINDS = ("bundle", "prepared", "target", "operation")
CONFIRMATION_SCHEMA = "lab-project-confirmation/1"
ACCEPTANCE_SCHEMA = "lab-project-acceptance/1"


def closed(value: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("closed payload mismatch")
    return value


@dataclass(frozen=True, slots=True)
class Pair:
    lab: str
    core: str

    def to_payload(self) -> dict[str, str]:
        return {"lab": self.lab, "core": self.core}

    @classmethod
    def from_payload(cls, value: Any) -> "Pair":
        row = closed(value, {"lab", "core"})
        return cls(row["lab"], row["core"])


@dataclass(frozen=True, slots=True)
class WorkingCopy:
    path: str
    created_by_lab: bool
    git: bool
    source: dict | None  # {"repo", "base_revision", "branch"}

    def to_payload(self) -> dict:
        return {"path": self.path, "created_by_lab": self.created_by_lab, "git": self.git,
                "source": None if self.source is None else dict(self.source)}

    @classmethod
    def from_payload(cls, value: Any) -> "WorkingCopy":
        row = closed(value, {"path", "created_by_lab", "git", "source"})
        source = row["source"]
        if source is not None:
            source = dict(closed(source, {"repo", "base_revision", "branch"}))
        return cls(row["path"], bool(row["created_by_lab"]), bool(row["git"]), source)


@dataclass(frozen=True, slots=True)
class Implementation:
    id: str
    name: str
    place: str
    origin: dict  # {"kind": "connected"|"created"} | {"kind": "ported_from", "implementation": id}
    package_entry: str | None
    archived: dict | None  # {"at", "reason"}

    def to_payload(self) -> dict:
        return {"id": self.id, "name": self.name, "place": self.place, "origin": dict(self.origin),
                "package_entry": self.package_entry,
                "archived": None if self.archived is None else dict(self.archived)}

    @classmethod
    def from_payload(cls, value: Any) -> "Implementation":
        row = closed(value, {"id", "name", "place", "origin", "package_entry", "archived"})
        return cls(row["id"], row["name"], row["place"], dict(row["origin"]), row["package_entry"],
                   None if row["archived"] is None else dict(row["archived"]))


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    state: str  # creating | ready | failed
    failure: str | None
    created_at: str
    archived: dict | None
    record_version: int
    working_copy: WorkingCopy
    materials_root: str
    exclude: tuple[str, ...]
    implementations: tuple[Implementation, ...] = field(default=())

    def active_implementations(self) -> tuple[Implementation, ...]:
        return tuple(item for item in self.implementations if item.archived is None)

    def implementation(self, implementation_id: str) -> Implementation | None:
        return next((item for item in self.implementations if item.id == implementation_id), None)

    def to_payload(self) -> dict:
        return {
            "schema": PROJECT_SCHEMA, "id": self.id, "name": self.name, "state": self.state,
            "failure": self.failure, "created_at": self.created_at,
            "archived": None if self.archived is None else dict(self.archived),
            "record_version": self.record_version, "working_copy": self.working_copy.to_payload(),
            "materials_root": self.materials_root, "exclude": list(self.exclude),
            "implementations": [item.to_payload() for item in self.implementations],
        }

    @classmethod
    def from_payload(cls, value: Any) -> "Project":
        row = closed(value, {
            "schema", "id", "name", "state", "failure", "created_at", "archived", "record_version",
            "working_copy", "materials_root", "exclude", "implementations",
        })
        if row["schema"] != PROJECT_SCHEMA or row["state"] not in PROJECT_STATES:
            raise ValueError("invalid project payload")
        return cls(
            row["id"], row["name"], row["state"], row["failure"], row["created_at"],
            None if row["archived"] is None else dict(row["archived"]), int(row["record_version"]),
            WorkingCopy.from_payload(row["working_copy"]), row["materials_root"],
            tuple(row["exclude"]), tuple(Implementation.from_payload(item) for item in row["implementations"]),
        )


PROJECT_WIRE_SCHEMA = "lab-project/2"
PROJECT_LIST_SCHEMA = "lab-project-list/2"
PORT_KEYS = frozenset({"template", "ref", "commit", "created_at"})


def project_wire(project: Project, ports: Mapping[str, Mapping[str, str]]) -> dict:
    """``lab-project/2`` (spec 2026-10-01 §2.6, §3.4): the stored ``lab-project/1`` record plus ``port`` on every
    implementation — ``null`` or the template provenance from the ``ports`` table. The record is unchanged
    (ADR-0017); only the wire form gains the key."""
    payload = project.to_payload()
    payload["schema"] = PROJECT_WIRE_SCHEMA
    payload["implementations"] = [
        {**item, "port": None if ports.get(item["id"]) is None else dict(ports[item["id"]])}
        for item in payload["implementations"]]
    return payload


def project_from_wire(value: Any) -> tuple[Project, dict[str, dict]]:
    """Decode ``lab-project/2`` into the record and its ports by implementation id; closed like every wire form."""
    row = closed(value, {"schema", "id", "name", "state", "failure", "created_at", "archived", "record_version",
                         "working_copy", "materials_root", "exclude", "implementations"})
    if row["schema"] != PROJECT_WIRE_SCHEMA or not isinstance(row["implementations"], list):
        raise ValueError("invalid project payload")
    items, ports = [], {}
    for item in row["implementations"]:
        if not isinstance(item, Mapping) or "port" not in item:
            raise ValueError("invalid implementation payload")
        port = item["port"]
        if port is not None:
            port = closed(port, set(PORT_KEYS))
            if not all(isinstance(port[key], str) and port[key] for key in PORT_KEYS):
                raise ValueError("invalid port payload")
            ports[item["id"]] = dict(port)
        items.append({key: entry for key, entry in item.items() if key != "port"})
    return Project.from_payload({**row, "schema": PROJECT_SCHEMA, "implementations": items}), ports


@dataclass(frozen=True, slots=True)
class StaleReason:
    codes: tuple[str, ...]
    changes: Any = None  # inventory.Changes | None

    def to_payload(self) -> dict:
        order = ("other_pair", "place_changed", "inputs_changed", "digest_changed", "catalogue_changed")
        names = {"other_pair": "другая пара", "place_changed": "сменилось место",
                 "inputs_changed": "изменились входы", "digest_changed": "изменились заверенные байты",
                 "catalogue_changed": "сменился каталог"}
        message = "; ".join(names[code] for code in order if code in self.codes)
        if self.changes is not None and not self.changes.empty:
            message += f" ({self.changes.message()})"
        return {"codes": list(self.codes), "message": message,
                "changes": None if self.changes is None else self.changes.to_payload()}


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    subject: dict | None
    value: Any
    source: dict | None
    stale_reason: StaleReason | None
    carried_over: tuple[dict, ...] = ()
    state: dict | None = None  # current state key; used by acceptance, not serialized

    @property
    def holds(self) -> bool:
        truthy = self.value is True or (isinstance(self.value, (dict, list, tuple)) and bool(self.value))
        return truthy and self.stale_reason is None

    def to_payload(self) -> dict:
        return {"id": self.id, "subject": self.subject, "holds": self.holds, "value": self.value,
                "source": self.source, "stale_reason": None if self.stale_reason is None else self.stale_reason.to_payload(),
                "carried_over": [dict(item) for item in self.carried_over]}


@dataclass(frozen=True, slots=True)
class Link:
    kind: str
    project: str
    implementation: str
    pair: Pair
    state_root: str
    created_at: str
    binding: dict
    bundle_digest: str
    prepared_digest: str | None
    object_id: str  # bundle digest, prepared digest, target id or operation id
    operation_kind: str | None
    last_seen: dict | None

    def dedupe_key(self) -> str:
        return "|".join((self.kind, self.project, self.implementation, self.pair.lab, self.pair.core,
                         self.state_root, self.object_id))

    def to_payload(self) -> dict:
        return {"schema": LINK_SCHEMA, "kind": self.kind, "project": self.project,
                "implementation": self.implementation, "pair": self.pair.to_payload(),
                "state_root": self.state_root, "created_at": self.created_at, "binding": dict(self.binding),
                "bundle_digest": self.bundle_digest, "prepared_digest": self.prepared_digest,
                "object_id": self.object_id, "operation_kind": self.operation_kind,
                "last_seen": None if self.last_seen is None else dict(self.last_seen)}

    @classmethod
    def from_payload(cls, value) -> "Link":
        row = closed(value, {"schema", "kind", "project", "implementation", "pair", "state_root", "created_at",
                             "binding", "bundle_digest", "prepared_digest", "object_id", "operation_kind", "last_seen"})
        if row["schema"] != LINK_SCHEMA or row["kind"] not in LINK_KINDS:
            raise ValueError("invalid link payload")
        return cls(row["kind"], row["project"], row["implementation"], Pair.from_payload(row["pair"]),
                   row["state_root"], row["created_at"], dict(row["binding"]), row["bundle_digest"],
                   row["prepared_digest"], row["object_id"], row["operation_kind"],
                   None if row["last_seen"] is None else dict(row["last_seen"]))


@dataclass(frozen=True, slots=True)
class Confirmation:
    id: str
    project: str
    implementation: str | None
    claim: str
    attests: dict  # {"path", "digest"}
    linked: tuple[dict, ...]  # {"kind", "ref", "digest"}
    channel: str
    author: str
    note: str | None
    created_at: str
    revoked: dict | None

    def to_payload(self) -> dict:
        return {"schema": CONFIRMATION_SCHEMA, "id": self.id, "project": self.project,
                "implementation": self.implementation, "claim": self.claim, "attests": dict(self.attests),
                "linked": [dict(item) for item in self.linked], "channel": self.channel, "author": self.author,
                "note": self.note, "created_at": self.created_at, "revoked": self.revoked}

    @classmethod
    def from_payload(cls, value) -> "Confirmation":
        row = closed(value, {"schema", "id", "project", "implementation", "claim", "attests", "linked",
                             "channel", "author", "note", "created_at", "revoked"})
        if row["schema"] != CONFIRMATION_SCHEMA:
            raise ValueError("invalid confirmation")
        return cls(row["id"], row["project"], row["implementation"], row["claim"], dict(row["attests"]),
                   tuple(dict(item) for item in row["linked"]), row["channel"], row["author"], row["note"],
                   row["created_at"], row["revoked"])


@dataclass(frozen=True, slots=True)
class Acceptance:
    id: str | None  # None in a dry-run preview
    project: str
    implementation: str | None
    facts: tuple[dict, ...]
    across_pairs: bool
    reason: str
    channel: str
    author: str
    created_at: str
    revoked: dict | None

    def to_payload(self) -> dict:
        return {"schema": ACCEPTANCE_SCHEMA, "id": self.id, "project": self.project,
                "implementation": self.implementation, "facts": [dict(item) for item in self.facts],
                "across_pairs": self.across_pairs, "reason": self.reason, "channel": self.channel,
                "author": self.author, "created_at": self.created_at, "revoked": self.revoked,
                "dry_run": self.id is None}

    @classmethod
    def from_payload(cls, value) -> "Acceptance":
        row = closed(value, {"schema", "id", "project", "implementation", "facts", "across_pairs", "reason",
                             "channel", "author", "created_at", "revoked", "dry_run"})
        if row["schema"] != ACCEPTANCE_SCHEMA:
            raise ValueError("invalid acceptance")
        return cls(row["id"], row["project"], row["implementation"], tuple(dict(i) for i in row["facts"]),
                   bool(row["across_pairs"]), row["reason"], row["channel"], row["author"], row["created_at"],
                   row["revoked"])
