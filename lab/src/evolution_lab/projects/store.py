"""SQLite registry above pairs (ADR-0017 p. 3): major/minor version, additive migrations."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from evolution_lab.durability import Durability, effective
from evolution_lab.owner.models import Approval, ApprovalRecord

from .errors import ProjectError
from .models import Project

REGISTRY_MAJOR = 1
REGISTRY_MINOR = 5
# ``start`` is the Owner start key (``Owner.cycle_starts``) the export belongs to: an export whose
# start is not the cycle's current start never counts (ADR-0015). Minor 1 shipped in dev28, so new
# data arrives only by a new minor (MIGRATIONS), never by editing released DDL.
_LINK_EXPORTS = ("CREATE TABLE link_exports (dedupe TEXT PRIMARY KEY, export_id TEXT NOT NULL, "
                 "start INTEGER NOT NULL)")
# Target requirements of campaign cycles' resume verdicts (minor 2): a table, not a key in
# record_json — last_seen.resume keeps its {state, reason} form (ADR-0017, spec 2026-09-29 §2.4).
_LINK_RESUME_TARGETS = "CREATE TABLE link_resume_targets (dedupe TEXT PRIMARY KEY, target_json TEXT NOT NULL)"
# The template provenance of a port (minor 3, ADR-0020 p. 4): written with the port's implementation in one
# transaction; the implementation's record_json keeps its frozen form (ADR-0017 amendment 2026-09-27).
_PORTS = ("CREATE TABLE ports (project_id TEXT NOT NULL, implementation_id TEXT NOT NULL, "
          "template_source TEXT NOT NULL, template_ref TEXT NOT NULL, template_commit TEXT NOT NULL, "
          "created_at TEXT NOT NULL, PRIMARY KEY (project_id, implementation_id))")
# Environment-aware input identities (minor 4): metadata stays out of frozen record_json.
_INPUT_ENVIRONMENTS = ("CREATE TABLE input_environments (digest TEXT PRIMARY KEY, "
                       "declarations_json TEXT NOT NULL, sources_json TEXT NOT NULL)")
_APPROVALS = """CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('form','client_rules','none')),
    autonomy TEXT NOT NULL CHECK (autonomy IN ('ask','yolo')),
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    start INTEGER,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""
# Minor -> additive DDL. Released definitions stay immutable.
MIGRATIONS: dict[int, str] = {
    1: _LINK_EXPORTS, 2: _LINK_RESUME_TARGETS, 3: _PORTS, 4: _INPUT_ENVIRONMENTS, 5: _APPROVALS,
}

_SCHEMA = f"""
CREATE TABLE projects (id TEXT PRIMARY KEY, archived INTEGER NOT NULL, record_json TEXT NOT NULL);
CREATE TABLE links (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL, project TEXT NOT NULL, implementation TEXT NOT NULL,
    object_id TEXT NOT NULL, pair_json TEXT NOT NULL, state_root TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX links_project ON links(project, implementation, kind);
CREATE TABLE inventories (digest TEXT PRIMARY KEY, entries_json TEXT NOT NULL);
CREATE TABLE hash_cache (path TEXT PRIMARY KEY, key_json TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE confirmations (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL, record_json TEXT NOT NULL);
CREATE TABLE acceptances (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL, record_json TEXT NOT NULL);
{_LINK_EXPORTS};
{_LINK_RESUME_TARGETS};
{_PORTS};
{_INPUT_ENVIRONMENTS};
{_APPROVALS};
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class RegistryStore:
    """Thread-safe registry connection; every statement runs under one lock."""

    def __init__(self, database: sqlite3.Connection, version: int) -> None:
        self._db = database
        self._lock = threading.RLock()
        self.version = version

    @classmethod
    def open(cls, path: Path, *, read_only: bool = False, durability: Durability | None = None) -> "RegistryStore":
        durability = effective() if durability is None else durability
        path = Path(path)
        ours = REGISTRY_MAJOR * 1000 + REGISTRY_MINOR
        if read_only and not path.exists():
            database = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            database.executescript(_SCHEMA)
            return cls(database, ours)
        database = None
        try:
            if read_only:
                database = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, isolation_level=None)
            else:
                database = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
                if database.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise sqlite3.OperationalError("could not set journal mode")
            database.execute("PRAGMA synchronous=NORMAL" if durability == 'relaxed' else "PRAGMA synchronous=FULL")
            database.execute("PRAGMA busy_timeout=5000")
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            if database is not None:
                database.close()
            raise ProjectError("registry_invalid") from None
        if version == 0:
            if read_only:
                raise ProjectError("registry_invalid")
            database.executescript(f"BEGIN IMMEDIATE; {_SCHEMA} PRAGMA user_version={ours}; COMMIT;")
            return cls(database, ours)
        major, minor = divmod(version, 1000)
        if major > REGISTRY_MAJOR:
            database.close()
            raise ProjectError("registry_newer")
        if major < REGISTRY_MAJOR:
            database.close()
            raise ProjectError("registry_invalid")
        if minor < REGISTRY_MINOR and not read_only:
            backup = sqlite3.connect(path.with_name(f"{path.name}.bak-{version}"))
            with backup:
                database.backup(backup)
            backup.close()
            for step in range(minor + 1, REGISTRY_MINOR + 1):
                database.executescript(f"BEGIN IMMEDIATE; {MIGRATIONS[step]}; PRAGMA user_version={REGISTRY_MAJOR * 1000 + step}; COMMIT;")
            version = ours
        return cls(database, version)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "RegistryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self):
        """One write transaction: statements run inside it commit together or not at all.

        The lock is re-entrant, so ``_execute`` inside the block does not deadlock. Writable stores only.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            before = self._db.total_changes
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            # Keep the same snapshot/lock even when guarded writes all skip. An empty
            # read transaction must not commit (ADR-0029), nor append a WAL frame.
            self._db.execute("COMMIT" if self._db.total_changes != before else "ROLLBACK")

    # projects
    def put_project(self, project: Project) -> None:
        self._execute(
            "INSERT INTO projects(id, archived, record_json) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET archived=excluded.archived, record_json=excluded.record_json",
            (project.id, int(project.archived is not None), _dumps(project.to_payload())),
        )

    def project(self, project_id: str) -> Project | None:
        rows = self._execute("SELECT record_json FROM projects WHERE id = ?", (project_id,))
        return Project.from_payload(json.loads(rows[0][0])) if rows else None

    def projects(self, *, archived: bool) -> tuple[Project, ...]:
        sql = "SELECT record_json FROM projects" + ("" if archived else " WHERE archived = 0") + " ORDER BY id"
        return tuple(Project.from_payload(json.loads(row[0])) for row in self._execute(sql))

    def known_id(self, project_id: str) -> bool:
        return bool(self._execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)))

    # inventories and hash cache
    def put_inventory(self, digest: str, entries) -> None:
        self._execute("INSERT OR IGNORE INTO inventories(digest, entries_json) VALUES(?,?)",
                      (digest, _dumps([entry.to_payload() for entry in entries])))

    def inventory(self, digest: str):
        from .inventory import Entry
        rows = self._execute("SELECT entries_json FROM inventories WHERE digest = ?", (digest,))
        return tuple(Entry.from_payload(item) for item in json.loads(rows[0][0])) if rows else None

    def put_input_environment(self, digest: str, declarations, sources) -> None:
        """Publish alongside the inventory, before any link can carry this identity."""
        self._execute("INSERT OR IGNORE INTO input_environments(digest, declarations_json, sources_json) VALUES(?,?,?)",
                      (digest, _dumps(declarations), _dumps(sources)))

    def input_environment(self, digest: str):
        # Read-only access to a pre-dev60 registry never migrates it.
        if self.version < 1004:
            return None
        rows = self._execute("SELECT declarations_json, sources_json FROM input_environments WHERE digest=?", (digest,))
        return (json.loads(rows[0][0]), json.loads(rows[0][1])) if rows else None

    def lookup(self, path: str, key: tuple[int, int, int, int]) -> str | None:
        rows = self._execute("SELECT key_json, sha256 FROM hash_cache WHERE path = ?", (path,))
        return rows[0][1] if rows and json.loads(rows[0][0]) == list(key) else None

    def store(self, path: str, key: tuple[int, int, int, int], sha256: str) -> None:
        key_json = _dumps(list(key))
        rows = self._execute("SELECT key_json, sha256 FROM hash_cache WHERE path=?", (path,))
        if rows and json.loads(rows[0][0]) == list(key) and rows[0][1] == sha256:
            return
        self._execute("INSERT INTO hash_cache(path, key_json, sha256) VALUES(?,?,?) "
                      "ON CONFLICT(path) DO UPDATE SET key_json=excluded.key_json, sha256=excluded.sha256",
                      (path, key_json, sha256))

    # links
    def insert_link(self, link) -> bool:
        before = self._execute("SELECT 1 FROM links WHERE dedupe = ?", (link.dedupe_key(),))
        self._execute(
            "INSERT OR IGNORE INTO links(dedupe, kind, project, implementation, object_id, pair_json, state_root, record_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (link.dedupe_key(), link.kind, link.project, link.implementation, link.object_id,
             _dumps(link.pair.to_payload()), link.state_root, _dumps(link.to_payload())))
        return not before

    def upsert_link(self, link):
        """Insert a new link, or refresh an existing dedupe-matched one's binding in place.

        ``dedupe_key()`` deliberately excludes ``binding`` (spec §3.3): the same content
        (bundle/prepared digest) linked twice from the same project/implementation/pair/
        state_root is one row, not two. But the *binding* it carries (working copy path,
        materials revision, ...) can legitimately change between those two links — e.g. the
        project's working copy moved — and a stale binding would silently propagate to every
        downstream reader (``prepare()``'s source link, ``sync()``'s target/operation seeding).
        So a dedupe hit here updates the stored row's ``binding``/``created_at`` and returns the
        refreshed stored row, rather than ignoring the write and handing back the caller's
        (unstored) link like a plain ``insert_link`` would.
        """
        from dataclasses import replace
        from .models import Link
        rows = self._execute("SELECT record_json FROM links WHERE dedupe = ?", (link.dedupe_key(),))
        if rows:
            existing = Link.from_payload(json.loads(rows[0][0]))
            updated = replace(existing, binding=dict(link.binding), created_at=link.created_at)
            self._execute("UPDATE links SET record_json=? WHERE dedupe=?",
                          (_dumps(updated.to_payload()), link.dedupe_key()))
            return updated
        self._execute(
            "INSERT INTO links(dedupe, kind, project, implementation, object_id, pair_json, state_root, record_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (link.dedupe_key(), link.kind, link.project, link.implementation, link.object_id,
             _dumps(link.pair.to_payload()), link.state_root, _dumps(link.to_payload())))
        return link

    def link_for(self, kind, object_id, pair, state_root):
        from .models import Link
        rows = self._execute("SELECT record_json FROM links WHERE kind=? AND object_id=? AND pair_json=? AND state_root=?",
                             (kind, object_id, _dumps(pair.to_payload()), state_root))
        return Link.from_payload(json.loads(rows[0][0])) if rows else None

    def set_last_seen(self, kind, object_id, pair, state_root, last_seen: dict) -> None:
        from dataclasses import replace
        link = self.link_for(kind, object_id, pair, state_root)
        if link is not None:
            # Only the snapshot's own timestamp is metadata about this write. A nested
            # ``at`` in result/tasks remains part of the value being compared.
            prior = {key: value for key, value in (link.last_seen or {}).items() if key != "at"}
            current = {key: value for key, value in last_seen.items() if key != "at"}
            if prior == current:
                return
            self._execute("UPDATE links SET record_json=? WHERE dedupe=?",
                          (_dumps(replace(link, last_seen=last_seen).to_payload()), link.dedupe_key()))

    def links(self, project, *, implementation=None, kind=None):
        from .models import Link
        sql, params = "SELECT record_json FROM links WHERE project = ?", [project]
        if implementation is not None:
            sql, params = sql + " AND implementation = ?", params + [implementation]
        if kind is not None:
            sql, params = sql + " AND kind = ?", params + [kind]
        return tuple(Link.from_payload(json.loads(row[0])) for row in self._execute(sql + " ORDER BY seq", tuple(params)))

    # exports of campaign cycles (minor 1): a table, not a key in record_json (ADR-0017 amendment)
    def set_link_export(self, dedupe: str, export_id: str | None, start: int | None) -> None:
        """Record the export of the cycle's current start ``start``, or forget it (``None``)."""
        rows = self._execute("SELECT export_id, start FROM link_exports WHERE dedupe=?", (dedupe,))
        if (rows[0] if rows else None) == ((export_id, start) if export_id is not None else None):
            return
        if export_id is None:
            self._execute("DELETE FROM link_exports WHERE dedupe=?", (dedupe,))
        else:
            self._execute("INSERT INTO link_exports(dedupe, export_id, start) VALUES(?,?,?) "
                          "ON CONFLICT(dedupe) DO UPDATE SET export_id=excluded.export_id, start=excluded.start",
                          (dedupe, export_id, start))

    def link_exports(self, project: str) -> dict[str, tuple[str, int]]:
        """dedupe -> (export_id, Owner start key it belongs to)."""
        if self.version % 1000 < 1:  # an unmigrated registry opened read-only has no table
            return {}
        rows = self._execute("SELECT e.dedupe, e.export_id, e.start FROM link_exports e "
                             "JOIN links l ON l.dedupe = e.dedupe WHERE l.project = ?", (project,))
        return {dedupe: (export_id, start) for dedupe, export_id, start in rows}

    def set_link_resume_target(self, dedupe: str, target: dict | None) -> None:
        """Record the target requirement of the cycle's resume verdict, or forget it (``None``)."""
        rows = self._execute("SELECT target_json FROM link_resume_targets WHERE dedupe=?", (dedupe,))
        if (json.loads(rows[0][0]) if rows else None) == target:
            return
        if target is None:
            self._execute("DELETE FROM link_resume_targets WHERE dedupe=?", (dedupe,))
        else:
            self._execute("INSERT INTO link_resume_targets(dedupe, target_json) VALUES(?,?) "
                          "ON CONFLICT(dedupe) DO UPDATE SET target_json=excluded.target_json",
                          (dedupe, _dumps(target)))

    def link_resume_targets(self, project: str) -> dict[str, dict]:
        """dedupe -> target requirement payload."""
        if self.version % 1000 < 2:  # an unmigrated registry opened read-only has no table
            return {}
        rows = self._execute("SELECT t.dedupe, t.target_json FROM link_resume_targets t "
                             "JOIN links l ON l.dedupe = t.dedupe WHERE l.project = ?", (project,))
        return {dedupe: json.loads(text) for dedupe, text in rows}

    # template provenance of ports (minor 3, ADR-0020 p. 4)
    def insert_port(self, project_id: str, implementation_id: str, *, template_source: str, template_ref: str,
                    template_commit: str, created_at: str) -> None:
        self._execute("INSERT INTO ports(project_id, implementation_id, template_source, template_ref, "
                      "template_commit, created_at) VALUES(?,?,?,?,?,?)",
                      (project_id, implementation_id, template_source, template_ref, template_commit, created_at))

    def ports(self, project_id: str) -> dict[str, dict]:
        """implementation id -> {template, ref, commit, created_at} (the ``port`` of ``lab-project/2``)."""
        if self.version % 1000 < 3:  # an unmigrated registry opened read-only has no table
            return {}
        rows = self._execute("SELECT implementation_id, template_source, template_ref, template_commit, created_at "
                             "FROM ports WHERE project_id = ? ORDER BY implementation_id", (project_id,))
        return {item: {"template": source, "ref": ref, "commit": commit, "created_at": at}
                for item, source, ref, commit, at in rows}

    # confirmations and acceptances
    def put_confirmation(self, confirmation) -> None:
        self._execute("INSERT INTO confirmations(id, project, record_json) VALUES(?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json",
                      (confirmation.id, confirmation.project, _dumps(confirmation.to_payload())))

    def confirmations(self, project):
        from .models import Confirmation
        return tuple(Confirmation.from_payload(json.loads(r[0])) for r in
                     self._execute("SELECT record_json FROM confirmations WHERE project=? ORDER BY seq", (project,)))

    def put_acceptance(self, acceptance) -> None:
        self._execute("INSERT INTO acceptances(id, project, record_json) VALUES(?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json",
                      (acceptance.id, acceptance.project, _dumps(acceptance.to_payload())))

    def acceptances(self, project):
        from .models import Acceptance
        return tuple(Acceptance.from_payload(json.loads(r[0])) for r in
                     self._execute("SELECT record_json FROM acceptances WHERE project=? ORDER BY seq", (project,)))

    def insert_approval(self, project_id: str, *, approval: Approval, request_id: str | None,
                        resource_kind: str, resource_id: str, outcome: str) -> str:
        """Use the caller's registry transaction when the effect is a registry write."""
        record_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self._execute(
            "INSERT INTO approvals (id, project_id, request_id, tool, via, autonomy, resource_kind, "
            "resource_id, start, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (record_id, project_id, request_id, approval.tool, approval.via, approval.autonomy,
             resource_kind, resource_id, outcome, created_at),
        )
        return record_id

    def set_approval_outcome(self, approval_id: str, outcome: str) -> None:
        self._execute("UPDATE approvals SET outcome=? WHERE id=?", (outcome, approval_id))

    def project_approvals(self, project_id: str) -> list[dict]:
        if self.version < 1005:  # An older read-only registry has no approvals table.
            return []
        rows = self._execute(
            "SELECT id, request_id, tool, via, autonomy, resource_kind, resource_id, start, outcome, created_at "
            "FROM approvals WHERE project_id=? ORDER BY created_at, id", (project_id,),
        )
        return [ApprovalRecord(*row).to_payload() for row in rows]
