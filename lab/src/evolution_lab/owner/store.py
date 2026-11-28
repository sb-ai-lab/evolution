"""Serialized SQLite ownership state for the S2 Owner.

This module performs no process spawning.  Callers must create, use and close an
instance on one dedicated thread; the async Owner places these blocking calls on
its single-worker executor.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from evolution_lab.durability import Durability, effective
from evolution_lab._paths import canonical_root

from .models import (
    Approval,
    ApprovalRecord,
    CleanupFacts,
    CoreTerminal,
    ClientTask,
    DeletionResult,
    ExportResult,
    Connection,
    ExecutorIdentity,
    Failure,
    OperationResult,
    OperationStatus,
    OwnerError,
    Persistence,
    PolicyRecord,
    ProcessFacts,
    ReleaseReceipt,
    Refusal,
    RefusalFinding,
    RefusalRecord,
    ResourceRef,
    StartFacts,
    TargetRef,
    TargetStatus,
    TaskRef,
    TaskResult,
    TaskStatus,
    durable_campaign_outcome,
)
from .models import _digest, _freeze_json, _thaw_json
from .resume_rules import durable_verdict, raise_for, used_targets


_SCHEMA_VERSION = 14
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
_OPERATION_STATES = frozenset(
    {"reserved", "running", "stopping", "finished", "orphaned", "cleanup_required", "released"}
)


@dataclass(frozen=True, slots=True)
class TargetAcceptance:
    task_ref: TaskRef
    target_ref: TargetRef
    scope_id: str


@dataclass(frozen=True, slots=True)
class TaskAcceptance:
    task_ref: TaskRef
    scope_id: str | None


@dataclass(frozen=True, slots=True)
class OperationAcceptance:
    status: OperationStatus
    scope_id: str
    run_id: str
    artifact_root: Path
    run_directory: Path


def _json(value: Any) -> str:
    if hasattr(value, "to_payload"):
        value = value.to_payload()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _new_id() -> str:
    return uuid.uuid4().hex


def read_saved_operation_state(state: Path, operation_id: str) -> tuple[str, str, str] | None:
    """Read an older pair's operation without opening a new Owner generation."""
    identifier = _validate_internal_id(operation_id)
    path = state / "owner.sqlite3"
    if path.is_symlink():
        raise OwnerError("io_error", "read")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as database:
            if database.execute("PRAGMA user_version").fetchone()[0] not in {11, 12, 13, _SCHEMA_VERSION}:
                raise OwnerError("unsupported_schema", "read")
            return database.execute(
                "SELECT kind, control_state, results_state FROM operations WHERE id = ?", (identifier,),
            ).fetchone()
    except sqlite3.Error:
        raise OwnerError("io_error", "read") from None


def _validate_internal_id(value: Any) -> str:
    # Reuse the public ref validator so assigned internal IDs cannot become paths.
    return ResourceRef("operation", value).id


#: Allowed source states of each probe row move (spec 2026-10-02 §3.2–§3.5).
_PROBE_TRANSITIONS = {
    "open": ("admitted",),
    "releasing": ("admitted", "open"),
    "released": ("admitted", "open", "releasing"),
    "uncertain": ("admitted", "open", "releasing"),
}


def _validate_probe_session(value: Any) -> str:
    # A probe session id is a Lab 32-hex id: the ledger namespace and the adapter run_id.
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise OwnerError("invalid_argument", "probe")
    return value


def _client_ref(value: Any, stage: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024):
        raise OwnerError("invalid_argument", stage)
    return value


def _open_root_nofollow(path: Path) -> tuple[Path, int]:
    lexical = Path(os.path.abspath(os.fspath(path)))
    if lexical == Path(os.sep):
        raise OSError(errno.EINVAL, "owner state must be a dedicated directory")
    parts = lexical.parts
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(parts[1:]):
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, 0o700)
        return lexical, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_private_directory(root_fd: int, name: str) -> None:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def _open_regular_file(root_fd: int, name: str) -> int:
    descriptor = os.open(name, _FILE_FLAGS, 0o600, dir_fd=root_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise OSError(errno.EINVAL, "owner control file is not regular")
    os.fchmod(descriptor, 0o600)
    return descriptor


def _database_path(root_fd: int, path: Path) -> str:
    if sys.platform == 'darwin':
        directory = fcntl.fcntl(root_fd, getattr(fcntl, 'F_GETPATH'), bytes(1024))
        return os.path.join(directory.rstrip(b'\0').decode(), 'owner.sqlite3')
    return f'/proc/self/fd/{root_fd}/owner.sqlite3'


class OwnerStateStore:
    """Internal durable state boundary; it never accepts or stores secret values."""

    def __init__(
        self,
        state_directory: Path,
        root_fd: int,
        lock_fd: int,
        database: sqlite3.Connection,
        generation: str,
    ) -> None:
        self.state_directory = state_directory
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self._database = database
        self.generation = generation
        self._thread_id = threading.get_ident()
        self._closed = False

    @classmethod
    def open(cls, state_directory: Path | str, *, durability: Durability | None = None,
             scratch_directory: Path | None = None) -> "OwnerStateStore":
        durability = effective() if durability is None else durability
        root_fd = -1
        lock_fd = -1
        database: sqlite3.Connection | None = None
        try:
            try:
                state_root = canonical_root(state_directory)
            except ValueError:
                raise OwnerError('io_error', 'connect') from None
            path, root_fd = _open_root_nofollow(state_root)
            for name in ("scopes", "targets", "artifacts", "snapshots"):
                _ensure_private_directory(root_fd, name)
            lock_fd = _open_regular_file(root_fd, "owner.lock")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OwnerError("busy", "connect") from None

            from .scratch import bind_scratch
            scratch = bind_scratch(root_fd, path, scratch_directory)
            db_fd = _open_regular_file(root_fd, "owner.sqlite3")
            try:
                db_info = os.fstat(db_fd)
            finally:
                os.close(db_fd)
            db_path = _database_path(root_fd, path)
            database = sqlite3.connect(
                db_path,
                timeout=0,
                isolation_level=None,
            )
            if sys.platform == 'darwin':
                connected_info = os.stat(db_path)
                if (connected_info.st_dev, connected_info.st_ino) != (db_info.st_dev, db_info.st_ino):
                    raise OwnerError('io_error', 'connect')
            database.row_factory = sqlite3.Row
            journal = "wal" if durability == 'relaxed' else "delete"
            if database.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] != journal:
                raise sqlite3.OperationalError("could not set journal mode")
            database.execute("PRAGMA synchronous=NORMAL" if durability == 'relaxed' else "PRAGMA synchronous=FULL")
            database.execute("PRAGMA foreign_keys=ON")
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            tables = int(
                database.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = ? AND name NOT LIKE ?",
                    ("table", "sqlite_%"),
                ).fetchone()[0]
            )
            if version == 0 and tables == 0:
                cls._initialize_v7(database)
                version = 11
            elif version not in {11, 12, 13, _SCHEMA_VERSION}:
                raise OwnerError("unsupported_schema", "connect")
            if version == 11:
                cls._migrate_v11(database)
            if version <= 12:
                cls._migrate_v12(database)
            if version <= 13:
                cls._migrate_v13(database)
            generation = _new_id()
            store = cls(path, root_fd, lock_fd, database, generation)
            store.scratch_directory = scratch
            store._open_generation()
            store._clean_scratch(staging=True)
            return store
        except OwnerError:
            if database is not None:
                database.close()
            if lock_fd >= 0:
                os.close(lock_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise
        except (OSError, sqlite3.Error):
            if database is not None:
                database.close()
            if lock_fd >= 0:
                os.close(lock_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise OwnerError("io_error", "connect") from None

    @staticmethod
    def _initialize_v7(database: sqlite3.Connection) -> None:
        database.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE generations (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                closed_at REAL
            );
            CREATE TABLE connections (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                source_kind TEXT NOT NULL CHECK(source_kind IN ('campaign','evaluation','probe')),
                bundle_digest TEXT NOT NULL,
                launch_digest TEXT NOT NULL,
                UNIQUE(owner_generation, bundle_digest)
            );
            CREATE TABLE targets (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                connection_id TEXT NOT NULL REFERENCES connections(id),
                launch_digest TEXT NOT NULL,
                bundle_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('starting','ready','stopping','stopped','unknown','cleanup_required')),
                base_url TEXT NOT NULL,
                boot_id TEXT,
                health_json TEXT,
                cleanup_state TEXT NOT NULL,
                cleanup_complete INTEGER,
                failure_json TEXT,
                client_ref TEXT CHECK(client_ref IS NULL OR length(CAST(client_ref AS BLOB)) <= 1024),
                execution_policy TEXT REFERENCES execution_policies(digest)
            );
            CREATE TABLE operations (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                prepared_digest TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('campaign','evaluation')),
                target_id TEXT NOT NULL REFERENCES targets(id),
                run_id TEXT NOT NULL UNIQUE,
                control_state TEXT NOT NULL CHECK(control_state IN ('reserved','running','stopping','finished','orphaned','cleanup_required','released')),
                dispatch_possible INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_possible IN (0,1)),
                executor_json TEXT NOT NULL,
                process_json TEXT NOT NULL,
                target_cleanup_state TEXT NOT NULL,
                target_cleanup_complete INTEGER,
                result_json TEXT,
                failure_json TEXT,
                refusal_json TEXT,
                core_terminal_json TEXT,
                results_state TEXT NOT NULL,
                start INTEGER NOT NULL DEFAULT 1 CHECK(start >= 1),
                current_scope_id TEXT NOT NULL,
                client_ref TEXT CHECK(client_ref IS NULL OR length(CAST(client_ref AS BLOB)) <= 1024)
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                action TEXT NOT NULL CHECK(action IN ('target_start','target_stop','check','cleanup','conformance','export','delete')),
                resource_kind TEXT CHECK(resource_kind IN ('operation','target','task')),
                resource_id TEXT,
                state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','unknown')),
                result_json TEXT,
                failure_json TEXT,
                persistence_state TEXT NOT NULL,
                persistence_failure_json TEXT,
                dispatch_possible INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_possible IN (0,1)),
                CHECK((resource_kind IS NULL) = (resource_id IS NULL))
            );
            CREATE TABLE scopes (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                resource_kind TEXT NOT NULL CHECK(resource_kind IN ('operation','target','task','reflector_probe','target_gateway')),
                resource_id TEXT NOT NULL,
                target_id TEXT REFERENCES targets(id),
                operation_id TEXT REFERENCES operations(id),
                task_id TEXT REFERENCES tasks(id),
                root_relative TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('active','cleanup_required','orphaned','released')),
                helper_possible INTEGER NOT NULL DEFAULT 0 CHECK(helper_possible IN (0,1)),
                cleanup_state TEXT NOT NULL,
                cleanup_complete INTEGER,
                forced INTEGER,
                start INTEGER NOT NULL DEFAULT 1 CHECK(start >= 1),
                UNIQUE(resource_kind, resource_id, start),
                CHECK((target_id IS NOT NULL) + (operation_id IS NOT NULL) + (task_id IS NOT NULL) = 1)
            );
            CREATE TABLE operation_starts (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                start INTEGER NOT NULL CHECK(start >= 1),
                scope_id TEXT NOT NULL UNIQUE REFERENCES scopes(id),
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                target_id TEXT NOT NULL REFERENCES targets(id),
                process_json TEXT NOT NULL,
                target_cleanup_state TEXT NOT NULL,
                target_cleanup_complete INTEGER,
                failure_json TEXT,
                refusal_json TEXT,
                core_terminal_json TEXT,
                PRIMARY KEY(operation_id, start)
            );
            CREATE TABLE reservations (
                slot TEXT PRIMARY KEY CHECK(slot IN ('execution','target_lifetime','target_use')),
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                scope_id TEXT NOT NULL REFERENCES scopes(id),
                target_id TEXT REFERENCES targets(id),
                operation_id TEXT REFERENCES operations(id),
                task_id TEXT REFERENCES tasks(id),
                CHECK((target_id IS NOT NULL) + (operation_id IS NOT NULL) + (task_id IS NOT NULL) = 1)
            );
            CREATE TABLE dispositions (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL REFERENCES generations(id),
                scope_id TEXT NOT NULL REFERENCES scopes(id),
                resource_kind TEXT NOT NULL CHECK(resource_kind IN ('operation','target','task')),
                resource_id TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN ('normal','confirmed_cleanup','manual')),
                reason TEXT,
                created_at REAL NOT NULL,
                UNIQUE(scope_id, disposition),
                CHECK((disposition = 'manual') = (reason IS NOT NULL))
            );
            CREATE TABLE owned_inventory (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('tree','file')),
                PRIMARY KEY(operation_id, path)
            );
            CREATE TABLE operation_exports (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                export_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                start INTEGER NOT NULL CHECK(start >= 1),
                deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
                PRIMARY KEY(operation_id, export_id)
            );
            CREATE TABLE export_tasks (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                operation_id TEXT NOT NULL,
                export_id TEXT NOT NULL,
                FOREIGN KEY(operation_id,export_id) REFERENCES operation_exports(operation_id,export_id)
            );
            CREATE TABLE deletions (
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                inventory_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('deleting','deleted','delete_failed')),
                remaining_json TEXT NOT NULL,
                failure_json TEXT
            );
            CREATE TABLE reflector_probe_runs (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                start INTEGER NOT NULL,
                launched_by TEXT CHECK(launched_by IN ('lab','operator')),
                state TEXT NOT NULL CHECK(state IN ('running','stopped','exhausted','not_declared')),
                restarts INTEGER NOT NULL DEFAULT 0 CHECK(restarts >= 0),
                last_exit INTEGER,
                PRIMARY KEY(operation_id, start)
            );
            CREATE TABLE probe_instances(
              session_id TEXT PRIMARY KEY,
              owner_generation TEXT NOT NULL REFERENCES generations(id),
              target_id TEXT NOT NULL REFERENCES targets(id),
              instance_id TEXT,
              state TEXT NOT NULL CHECK(state IN ('admitted','open','releasing','released','uncertain')),
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE execution_policies (
                digest TEXT PRIMARY KEY,
                body_json TEXT NOT NULL,
                author TEXT NOT NULL,
                channel TEXT NOT NULL,
                note TEXT,
                confirmed_at REAL NOT NULL
            );
            CREATE TABLE operation_start_policies (
                operation_id TEXT NOT NULL,
                start INTEGER NOT NULL CHECK(start >= 1),
                policy_digest TEXT REFERENCES execution_policies(digest),
                lab_version TEXT NOT NULL,
                PRIMARY KEY(operation_id, start),
                FOREIGN KEY(operation_id) REFERENCES operations(id)
            );
            CREATE TABLE operation_start_initiators (
                operation_id TEXT NOT NULL,
                start INTEGER NOT NULL CHECK(start >= 1),
                initiated_by TEXT NOT NULL CHECK(initiated_by IN ('human','auto_resume')),
                auto_resume_attempt INTEGER,
                PRIMARY KEY(operation_id, start),
                UNIQUE(operation_id, auto_resume_attempt),
                FOREIGN KEY(operation_id, start) REFERENCES operation_start_policies(operation_id, start),
                CHECK((initiated_by = 'human' AND auto_resume_attempt IS NULL) OR
                      (initiated_by = 'auto_resume' AND auto_resume_attempt IS NOT NULL AND
                       typeof(auto_resume_attempt) = 'integer' AND auto_resume_attempt >= 1))
            );
            CREATE TABLE probe_session_policies (
                session_id TEXT PRIMARY KEY REFERENCES probe_instances(session_id),
                policy_digest TEXT REFERENCES execution_policies(digest)
            );
            PRAGMA user_version=11;
            COMMIT;
            """
        )

    @staticmethod
    def _migrate_v11(database: sqlite3.Connection) -> None:
        database.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE operation_refusals (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                start_seq INTEGER NOT NULL CHECK(start_seq >= 1),
                findings_json TEXT NOT NULL,
                PRIMARY KEY(operation_id, start_seq)
            );
            PRAGMA user_version=12;
            COMMIT;
        """)

    @staticmethod
    def _migrate_v12(database: sqlite3.Connection) -> None:
        database.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE operation_starts ADD COLUMN prepared_digest TEXT;
            UPDATE operation_starts SET prepared_digest=(
                SELECT prepared_digest FROM operations WHERE id=operation_starts.operation_id);
            PRAGMA user_version=13;
            COMMIT;
        """)

    @staticmethod
    def _migrate_v13(database: sqlite3.Connection) -> None:
        database.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY,
                owner_generation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                via TEXT NOT NULL CHECK (via IN ('form','client_rules','none')),
                autonomy TEXT NOT NULL CHECK (autonomy IN ('ask','yolo')),
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                start INTEGER,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version=14;
            COMMIT;
        """)

    def operation_inputs(self, operation_id: str):
        current = self.operation_status(operation_id)
        rows = [dict(row) for row in self._database.execute(
            "SELECT start,prepared_digest FROM operation_starts WHERE operation_id=? ORDER BY start", (operation_id,))]
        return (*rows, {"start": current.starts[-1].start, "prepared_digest": current.prepared_digest})

    def resume_refusal(self, operation_id: str) -> bool:
        current = self.operation_status(operation_id)
        record = self.operation_refusal(operation_id)
        if current.kind != 'campaign' or record is None:
            return False
        start = next(s for s in current.starts if s.start == record.start)
        if start.failure is None or start.failure.code != 'core_refused':
            return False
        from .receipts import open_directory_nofollow
        try:
            fd = open_directory_nofollow(self.state_directory / 'artifacts' / operation_id / operation_id)
        except OSError:
            return False
        os.close(fd)
        return True

    def record_refusal(self, operation_id: str, *, start: int,
                       findings: tuple[RefusalFinding, ...]) -> None:
        record = RefusalRecord(operation_id, start, findings)
        with self._transaction("read") as database:
            current = self.operation_status(operation_id)
            if current.starts[-1].start != start:
                raise OwnerError("resource_busy", "read")
            database.execute("INSERT OR REPLACE INTO operation_refusals VALUES(?,?,?)",
                (operation_id, start, _json([f.to_payload() for f in record.findings])))

    def operation_refusal(self, operation_id: str) -> RefusalRecord | None:
        """Findings from the last Core start, across later pre-Core failures.

        Old starts have no entry-receipt protocol. A saved refusal proves entry;
        an old launched/unknown start without that proof is a conservative barrier,
        never evidence that it failed before Core.
        """
        from .core_entry import NAME, read_core_entry
        current = self.operation_status(operation_id)
        if current.kind != 'campaign' or current.results_state in {'deleted', 'deleting', 'delete_failed'}:
            return None
        for start in reversed(current.starts):
            relative = f'scopes/{start.scope_id}/{NAME}'
            registered = self._database.execute(
                'SELECT 1 FROM owned_inventory WHERE operation_id=? AND path=?',
                (operation_id, relative)).fetchone() is not None
            row = self._database.execute(
                'SELECT findings_json FROM operation_refusals WHERE operation_id=? AND start_seq=?',
                (operation_id, start.start)).fetchone()
            if registered:
                try:
                    entered = read_core_entry(self.state_directory / relative,
                        operation_id=operation_id, scope_id=start.scope_id,
                        owner_generation=start.owner_generation)
                except (OSError, ValueError):
                    raise OwnerError('campaign_unreadable', 'before_start') from None
                if not entered and row is None:
                    continue
            elif row is None and start.process.launched is False:
                continue
            if row is None:
                return None
            return RefusalRecord(operation_id, start.start,
                tuple(RefusalFinding.from_payload(f) for f in json.loads(row['findings_json'])))
        return None

    def _check_thread(self) -> None:
        if self._closed or threading.get_ident() != self._thread_id:
            raise OwnerError("lab_error", "connect")

    @contextmanager
    def _transaction(self, stage: str) -> Iterator[sqlite3.Connection]:
        self._check_thread()
        try:
            self._database.execute("BEGIN IMMEDIATE")
            yield self._database
            self._database.execute("COMMIT")
        except OwnerError:
            try:
                self._database.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except sqlite3.Error:
            try:
                self._database.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise OwnerError("io_error", stage) from None

    def _insert_approval(
        self, conn: sqlite3.Connection, *, approval: Approval, request_id: str,
        resource_kind: str, resource_id: str, start: int | None, outcome: str = "admitted",
    ) -> str:
        """Write the approval using the caller's admission transaction."""
        record_id = _new_id()
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, self.generation, request_id, approval.tool, approval.via, approval.autonomy,
             resource_kind, resource_id, start, outcome, created_at),
        )
        return record_id

    def approvals_for(self, *, operation_id: str | None = None,
                      policy_digest: str | None = None) -> list[ApprovalRecord]:
        self._check_thread()
        if (operation_id is None) == (policy_digest is None):
            raise OwnerError("invalid_argument", "read")
        resource = ("operation", operation_id) if operation_id is not None else ("execution_policy", policy_digest)
        rows = self._database.execute(
            "SELECT id, request_id, tool, via, autonomy, resource_kind, resource_id, start, outcome, created_at "
            "FROM approvals a WHERE (a.resource_kind=? AND a.resource_id=?) "
            "OR (a.resource_kind='task' AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=a.resource_id "
            "AND t.action='cleanup' AND t.resource_kind='operation' AND t.resource_id=?)) "
            "ORDER BY created_at, id",
            (*resource, operation_id),
        )
        return [ApprovalRecord(**row) for row in rows]

    @staticmethod
    def _approved_start(database, ref: ResourceRef, observed_start: int | None, stage: str) -> int | None:
        if ref.kind != "operation":
            return None
        if type(observed_start) is not int or observed_start < 1:
            raise OwnerError("invalid_argument", stage)
        row = database.execute("SELECT start FROM operations WHERE id=?", (ref.id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", stage)
        if row["start"] != observed_start:
            raise OwnerError("state_changed", stage)
        return observed_start

    @staticmethod
    def _policy_record(row) -> PolicyRecord:
        return PolicyRecord(row["digest"], json.loads(row["body_json"]), row["author"],
                            row["channel"], row["note"], row["confirmed_at"])

    def confirm_execution_policy(self, document: Mapping, *, expected_digest: str,
                                 author: str, channel: str, note: str | None = None,
                                 approval: Approval | None = None, request_id: str | None = None) -> PolicyRecord:
        from evolution_lab.execution_policy import PolicyInvalid, parse_policy, safe_policy_pointer

        with self._transaction("before_start") as database:
            _digest(expected_digest)
            if not isinstance(document, Mapping):
                raise OwnerError("invalid_argument", "before_start")
            # Snapshot once before validation: the validated JSON and digest are
            # exactly the bytes inserted, even if the caller mutates its mapping.
            try:
                body_json = _json(_thaw_json(_freeze_json(document)))
                encoded = body_json.encode("utf-8")
                snapshot = json.loads(body_json)
                parse_policy(snapshot)
            except PolicyInvalid as error:
                if isinstance(snapshot, dict) and snapshot.get("schema") == "lab-execution-policy/2":
                    refusal = OwnerError("invalid_request", "before_start")
                    refusal.path = safe_policy_pointer(error.path)
                    raise refusal from None
                raise OwnerError("invalid_argument", "before_start") from None
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise OwnerError("invalid_argument", "before_start") from None
            digest = hashlib.sha256(encoded).hexdigest()
            if digest != expected_digest:
                raise OwnerError("digest_mismatch", "before_start")
            record = PolicyRecord(digest, json.loads(body_json), author, channel, note, time.time())
            existing = database.execute("SELECT * FROM execution_policies WHERE digest=?", (digest,)).fetchone()
            if existing is not None:
                if existing["body_json"] != body_json:
                    raise OwnerError("digest_mismatch", "before_start")
                return self._policy_record(existing)
            database.execute("INSERT INTO execution_policies VALUES(?,?,?,?,?,?)",
                             (digest, body_json, author, channel, note, record.confirmed_at))
            if approval is not None:
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind="execution_policy", resource_id=digest, start=None,
                                      outcome="applied")
            return record

    def execution_policies(self) -> tuple[PolicyRecord, ...]:
        self._check_thread()
        rows = self._database.execute("SELECT * FROM execution_policies ORDER BY confirmed_at, digest").fetchall()
        return tuple(self._policy_record(row) for row in rows)

    def execution_policy(self, digest: str) -> PolicyRecord:
        self._check_thread()
        _digest(digest)
        row = self._database.execute("SELECT * FROM execution_policies WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        return self._policy_record(row)

    def _open_generation(self) -> None:
        with self._transaction("connect") as database:
            database.execute(
                "INSERT INTO generations(id,created_at) VALUES(?,?)",
                (self.generation, time.time()),
            )
            database.execute(
                "UPDATE operations SET control_state = 'orphaned' "
                "WHERE owner_generation <> ? AND control_state <> 'released' "
                "AND EXISTS (SELECT 1 FROM scopes s WHERE s.operation_id = operations.id "
                "AND s.state <> 'released')",
                (self.generation,),
            )
            database.execute(
                "UPDATE targets SET state = 'cleanup_required' "
                "WHERE owner_generation <> ? AND state NOT IN ('stopped')",
                (self.generation,),
            )
            database.execute(
                "UPDATE tasks SET state = 'unknown' "
                "WHERE owner_generation <> ? AND state IN ('pending','running')",
                (self.generation,),
            )
            database.execute(
                "UPDATE scopes SET state = 'orphaned' "
                "WHERE owner_generation <> ? AND state IN ('active','cleanup_required')",
                (self.generation,),
            )
            # Spec 2026-10-02 §3.4: an earlier generation's probe instance may still live on a
            # target nobody owns now; only a confirmed stop or cleanup of that target releases it.
            database.execute(
                "UPDATE probe_instances SET state = 'uncertain', updated_at = ? "
                "WHERE owner_generation <> ? AND state NOT IN ('released','uncertain')",
                (time.time(), self.generation),
            )

    def _clean_scratch(self, target_id: str | None = None, *, staging: bool = False) -> None:
        if self.scratch_directory is None:
            return
        from .scratch import clean_scratch_directory
        query = "SELECT id FROM targets WHERE state='stopped' AND cleanup_state='confirmed' AND cleanup_complete=1"
        rows = self._database.execute(query if target_id is None else query + ' AND id=?',
            () if target_id is None else (target_id,)).fetchall()
        clean_scratch_directory(self.scratch_directory, (row['id'] for row in rows), staging=staging)

    def close(self) -> None:
        if self._closed:
            return
        self._check_thread()
        try:
            with self._transaction("finalizing") as database:
                database.execute(
                    "UPDATE generations SET closed_at = ? WHERE id = ?",
                    (time.time(), self.generation),
                )
        finally:
            self._database.close()
            os.close(self._lock_fd)
            os.close(self._root_fd)
            self._closed = True

    def __enter__(self) -> "OwnerStateStore":
        self._check_thread()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def bind_connection(
        self,
        *,
        bundle_digest: str,
        launch_digest: str,
        source_kind: str = "campaign",
    ) -> Connection:
        candidate = Connection(_new_id(), source_kind, bundle_digest, launch_digest, self.generation)
        with self._transaction("connect") as database:
            existing = database.execute(
                "SELECT * FROM connections WHERE owner_generation = ? AND bundle_digest = ?",
                (self.generation, bundle_digest),
            ).fetchone()
            if existing is not None:
                if existing["launch_digest"] != launch_digest or existing["source_kind"] != source_kind:
                    raise OwnerError("binding_mismatch", "connect")
                return self._connection(existing)
            database.execute(
                "INSERT INTO connections(id,owner_generation,source_kind,bundle_digest,launch_digest) "
                "VALUES(?,?,?,?,?)",
                (candidate.id, candidate.owner_generation, candidate.source_kind,
                 candidate.bundle_digest, candidate.launch_digest),
            )
        return candidate

    @staticmethod
    def _connection(row: sqlite3.Row) -> Connection:
        return Connection(
            row["id"], row["source_kind"], row["bundle_digest"],
            row["launch_digest"], row["owner_generation"],
        )

    def _reject_orphan_blockers(self, database: sqlite3.Connection) -> None:
        row = database.execute(
            "SELECT 1 FROM reservations r JOIN scopes s ON s.id = r.scope_id "
            "WHERE s.state IN ('orphaned','cleanup_required') LIMIT 1"
        ).fetchone()
        if row is not None:
            raise OwnerError("cleanup_required", "before_start")

    @staticmethod
    def _slot_available(database: sqlite3.Connection, slots: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in slots)
        row = database.execute(
            f"SELECT s.state FROM reservations r JOIN scopes s ON s.id = r.scope_id "
            f"WHERE r.slot IN ({placeholders}) LIMIT 1",
            slots,
        ).fetchone()
        if row is not None:
            code = "cleanup_required" if row["state"] in {"orphaned", "cleanup_required"} else "busy"
            raise OwnerError(code, "before_start")

    @staticmethod
    def _reject_probes(database: sqlite3.Connection) -> None:
        """Refuse a target_use admission while any probe row is not released (spec 2026-10-02 §3.4).

        Checked in the same transaction that takes the slot. An ``uncertain`` row blocks with the
        existing ``cleanup_required``; the target stop does not call this (it closes probes first).
        """
        states = {row["state"] for row in database.execute(
            "SELECT DISTINCT state FROM probe_instances WHERE state <> 'released'")}
        if states & {"admitted", "open", "releasing"}:
            raise OwnerError("probe_sessions_open", "before_start")
        if "uncertain" in states:
            raise OwnerError("cleanup_required", "before_start")

    @staticmethod
    def _release_probe_rows(database: sqlite3.Connection, target_id: str) -> None:
        # No instance outlives its target: once the target is confirmed stopped (or cleaned up),
        # every probe row on it is released, uncertain ones included (spec 2026-10-02 §3.5).
        database.execute(
            "UPDATE probe_instances SET state = 'released', updated_at = ? "
            "WHERE target_id = ? AND state <> 'released'",
            (time.time(), target_id),
        )

    @staticmethod
    def _reject_quarantined_target(database: sqlite3.Connection, target_id: str) -> None:
        """Spec 2026-10-02 §3.5, ADR-0021: after an unsettled release (an ``uncertain`` probe row) or on a
        ``cleanup_required`` target the only way on is a target stop: no probe admission and no probe send."""
        target = database.execute("SELECT state FROM targets WHERE id = ?", (target_id,)).fetchone()
        if (target is not None and target["state"] == "cleanup_required") or database.execute(
                "SELECT 1 FROM probe_instances WHERE target_id = ? AND state = 'uncertain' LIMIT 1",
                (target_id,)).fetchone() is not None:
            raise OwnerError("cleanup_required", "before_start")

    def check_probe_target(self, target_id: str) -> None:
        """Before every probe send on an admitted session: refused ``cleanup_required`` while the target is
        quarantined (see ``_reject_quarantined_target``); the session itself stays as it is."""
        _validate_internal_id(target_id)
        self._check_thread()
        self._reject_quarantined_target(self._database, target_id)

    def admit_probe(self, *, session_id: str, target_id: str, max_instances: int) -> None:
        """Admit one probe instance in one transaction, before any target call (spec 2026-10-02 §3.2)."""
        _validate_probe_session(session_id)
        _validate_internal_id(target_id)
        if type(max_instances) is not int or max_instances < 1:
            raise OwnerError("invalid_argument", "probe")
        now = time.time()
        with self._transaction("probe") as database:
            target = database.execute(
                "SELECT state, execution_policy FROM targets WHERE id = ? AND owner_generation = ?",
                (target_id, self.generation),
            ).fetchone()
            if target is None or target["state"] != "ready":
                raise OwnerError("target_not_running", "probe")
            if database.execute(
                "SELECT 1 FROM probe_instances WHERE session_id = ?", (session_id,)
            ).fetchone() is not None:
                raise OwnerError("invalid_argument", "probe")
            self._reject_orphan_blockers(database)
            self._reject_quarantined_target(database, target_id)
            self._slot_available(database, ("target_use",))
            (live,) = database.execute(
                "SELECT count(*) FROM probe_instances WHERE target_id = ? AND state <> 'released'",
                (target_id,),
            ).fetchone()
            if live >= max_instances:
                raise OwnerError("resource_busy", "probe")
            database.execute(
                "INSERT INTO probe_instances VALUES(?,?,?,?,?,?,?)",
                (session_id, self.generation, target_id, None, "admitted", now, now),
            )
            database.execute("INSERT INTO probe_session_policies VALUES(?,?)",
                             (session_id, target["execution_policy"]))

    def set_probe_state(self, session_id: str, state: str, *, instance_id: str | None = None) -> None:
        """Move a probe row of this generation forward; a settled (released/uncertain) row never
        moves here, and a row a close already took to ``releasing`` never reopens."""
        _validate_probe_session(session_id)
        if state not in _PROBE_TRANSITIONS or (
                instance_id is not None and (not isinstance(instance_id, str) or not instance_id)):
            raise OwnerError("invalid_argument", "probe")
        sources = _PROBE_TRANSITIONS[state]
        with self._transaction("probe") as database:
            database.execute(
                "UPDATE probe_instances SET state = ?, instance_id = coalesce(?, instance_id), "
                "updated_at = ? WHERE session_id = ? AND owner_generation = ? "
                f"AND state IN ({','.join('?' for _ in sources)})",
                (state, instance_id, time.time(), session_id, self.generation, *sources),
            )

    def release_probes_of_target(self, target_id: str) -> None:
        _validate_internal_id(target_id)
        with self._transaction("probe") as database:
            self._release_probe_rows(database, target_id)

    def probe_rows(self) -> list[tuple[str, str, str]]:
        """``(session_id, owner_generation, state)`` of every probe row, oldest first."""
        self._check_thread()
        return [tuple(row) for row in self._database.execute(
            "SELECT session_id, owner_generation, state FROM probe_instances ORDER BY created_at, session_id")]

    def accept_target_start(
        self,
        *,
        connection_id: str,
        launch_digest: str,
        bundle_digest: str,
        base_url: str,
        target_id: str | None = None,
        task_id: str | None = None,
        scope_id: str | None = None,
        client_ref: str | None = None,
        execution_policy: str | None = None,
    ) -> TargetAcceptance:
        if execution_policy is not None:
            self.execution_policy(execution_policy)
        client_ref = _client_ref(client_ref, "target_start")
        target_id = _validate_internal_id(target_id or _new_id())
        task_id = _validate_internal_id(task_id or _new_id())
        scope_id = _validate_internal_id(scope_id or _new_id())
        target_ref = TargetRef(target_id, self.generation)
        task_ref = TaskRef(task_id, self.generation)
        # Constructors validate the persisted digests and URL/string fields.
        Connection(connection_id, "campaign", bundle_digest, launch_digest, self.generation)
        TargetStatus(
            target_id, self.generation, launch_digest, bundle_digest, "starting", base_url,
            None, None, CleanupFacts.pending(), None,
        )
        with self._transaction("target_start") as database:
            self._reject_orphan_blockers(database)
            self._slot_available(database, ("target_lifetime", "target_use"))
            connection = database.execute(
                "SELECT * FROM connections WHERE id = ? AND owner_generation = ?",
                (connection_id, self.generation),
            ).fetchone()
            if connection is None:
                raise OwnerError("not_found", "target_start")
            if connection["bundle_digest"] != bundle_digest or connection["launch_digest"] != launch_digest:
                raise OwnerError("binding_mismatch", "target_start")
            if database.execute("SELECT 1 FROM targets WHERE id = ?", (target_id,)).fetchone():
                raise OwnerError("path_conflict", "target_start")
            database.execute(
                "INSERT INTO targets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (target_id, self.generation, connection_id, launch_digest, bundle_digest,
                 "starting", base_url, None, None, "pending", None, None, client_ref, execution_policy),
            )
            database.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, self.generation, "target_start", "target", target_id, "pending",
                 None, None, "not_attempted", None, 0),
            )
            database.execute(
                "INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope_id, self.generation, "target", target_id, target_id, None, None,
                 f"scopes/{scope_id}", "active", 0, "pending", None, None, 1),
            )
            database.execute(
                "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                ("target_lifetime", self.generation, scope_id, target_id, None, None),
            )
            database.execute(
                "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                ("target_use", self.generation, scope_id, None, None, task_id),
            )
        return TargetAcceptance(task_ref, target_ref, scope_id)

    def mark_target_ready(self, task_id: str, *, boot_id: str, health: dict | None = None) -> TargetStatus:
        TaskRef(task_id, self.generation)
        if not isinstance(boot_id, str) or not boot_id:
            raise OwnerError("invalid_argument", "target_start")
        with self._transaction("target_start") as database:
            task = database.execute(
                "SELECT * FROM tasks WHERE id = ? AND owner_generation = ? AND action = 'target_start'",
                (task_id, self.generation),
            ).fetchone()
            if task is None:
                raise OwnerError("not_found", "target_start")
            if task["state"] not in {"pending", "running"}:
                raise OwnerError("invalid_argument", "target_start")
            database.execute(
                "UPDATE targets SET state = 'ready', boot_id = ?, health_json = ? WHERE id = ?",
                (boot_id, None if health is None else _json(health), task["resource_id"]),
            )
            result = TaskResult("target_start", self.target_status(task["resource_id"]).to_payload())
            database.execute("UPDATE tasks SET state = 'succeeded', result_json = ?, persistence_state = 'saved' WHERE id = ?",
                (_json(result), task_id))
            database.execute(
                "DELETE FROM reservations WHERE slot = 'target_use' AND task_id = ?",
                (task_id,),
            )
        return self.target_status(task["resource_id"])

    def accept_task(
        self,
        *,
        action: str,
        ref: ResourceRef | None,
        reserve_target_use: bool = False,
        reserve_execution: bool = False,
        task_id: str | None = None,
        completed_conformance: TaskStatus | None = None,
        require_probe_free: bool = False,
        approval: Approval | None = None,
        request_id: str | None = None,
        observed_start: int | None = None,
    ) -> TaskAcceptance:
        if (
            action not in {"target_start", "target_stop", "check", "cleanup", "conformance", "export"}
            or (action == "export" and (ref is None or ref.kind != "operation" or reserve_target_use))
            or (action == "target_start" and reserve_target_use)
            or (action == "conformance") != reserve_execution
            or (reserve_execution and (not reserve_target_use or ref is None or ref.kind != "target"))
            or (approval is not None and (action != "cleanup" or ref is None))
        ):
            raise OwnerError("invalid_argument", "before_start")
        task_id = _validate_internal_id(task_id or _new_id())
        task_ref = TaskRef(task_id, self.generation)
        scope_id = _new_id() if action == "cleanup" or reserve_target_use else None
        with self._transaction("before_start") as database:
            if approval is not None:
                approved_start = self._approved_start(database, ref, observed_start, "cleanup")
            if action == "export":
                self.operation_status(ref.id)
            if action == "cleanup" and ref is not None:
                self._conformance_cleanup_state(database, ref, completed_conformance)
                if require_probe_free:
                    if ref.kind != 'operation':
                        raise OwnerError('invalid_argument', 'before_start')
                    self._reject_probes(database)
                    scope = database.execute(
                        'SELECT s.state FROM scopes s JOIN operations o ON s.id=o.current_scope_id WHERE o.id=?',
                        (ref.id,)).fetchone()
                    reservation = database.execute(
                        "SELECT operation_id FROM reservations WHERE slot='target_use'").fetchone()
                    if (scope is None or (reservation is not None and reservation['operation_id'] != ref.id)
                            or (scope['state'] != 'released' and reservation is None)):
                        raise OwnerError('resource_busy', 'before_start')
            if action in {"target_stop", "cleanup"} and ref is not None and ref.kind == "target":
                if self._conformance_holds_target(database, ref.id):
                    raise OwnerError("resource_busy", "before_start")
            if reserve_target_use or reserve_execution:
                self._reject_orphan_blockers(database)
                if reserve_target_use and (action != "target_stop" or require_probe_free):
                    self._reject_probes(database)
                slots = tuple(
                    slot for slot, reserved in (
                        ("execution", reserve_execution), ("target_use", reserve_target_use)
                    ) if reserved
                )
                self._slot_available(database, slots)
            if reserve_execution:
                target = database.execute(
                    "SELECT state FROM targets WHERE id = ? AND owner_generation = ?",
                    (ref.id, self.generation),
                ).fetchone()
                if target is None:
                    raise OwnerError("not_found", "before_start")
                if target["state"] != "ready":
                    raise OwnerError("target_not_running", "before_start")
            database.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, self.generation, action, None if ref is None else ref.kind,
                 None if ref is None else ref.id, "pending", None, None, "not_attempted", None, 0),
            )
            if scope_id is not None:
                database.execute(
                    "INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scope_id, self.generation, "task", task_id, None, None, task_id,
                     f"scopes/{scope_id}", "active", 0, "pending", None, None, 1),
                )
            if reserve_target_use:
                database.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                    ("target_use", self.generation, scope_id, None, None, task_id),
                )
            if reserve_execution:
                database.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                    ("execution", self.generation, scope_id, None, None, task_id),
                )
            if approval is not None:
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind="task", resource_id=task_id, start=approved_start)
        return TaskAcceptance(task_ref, scope_id)

    @staticmethod
    def _conformance_holds_target(database, target_id: str) -> bool:
        return database.execute(
            "SELECT 1 FROM reservations r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.slot='target_use' AND t.action='conformance' "
            "AND t.resource_kind='target' AND t.resource_id=? LIMIT 1",
            (target_id,),
        ).fetchone() is not None

    def _conformance_cleanup_state(self, database, ref, completed=None):
        """Check cleanup admission atomically; live terminal evidence is monotonic."""
        if ref.kind == "task":
            task = self.task_status(ref.id)
            if task.action == "conformance":
                state = task.state
                if completed is not None:
                    if (completed.id != task.id or completed.action != "conformance"
                        or completed.owner_generation != task.owner_generation
                        or completed.owner_generation != self.generation
                        or completed.state not in {"succeeded", "failed", "unknown"}):
                        raise OwnerError("invalid_argument", "before_start")
                    state = completed.state
                if state in {"pending", "running"}:
                    raise OwnerError("resource_busy", "before_start")
                return state
            if task.action == "target_start" and task.ref is not None:
                ref = task.ref
        if ref.kind == "target" and self._conformance_holds_target(database, ref.id):
            raise OwnerError("resource_busy", "before_start")
        return None

    def target_status_for_stop(self, target_id: str) -> TargetStatus:
        """Read stop eligibility with conformance ownership taking precedence."""
        _validate_internal_id(target_id)
        with self._transaction("before_start") as database:
            if self._conformance_holds_target(database, target_id):
                raise OwnerError("resource_busy", "before_start")
            return self.target_status(target_id)

    def task_status(self, task_id: str) -> TaskStatus:
        self._check_thread()
        _validate_internal_id(task_id)
        row = self._database.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        result = None if row['result_json'] is None else TaskResult(**json.loads(row['result_json']))
        return TaskStatus(row['id'], row['owner_generation'], row['action'],
            None if row['resource_kind'] is None else ResourceRef(row['resource_kind'], row['resource_id']),
            row['state'], result, self._failure(row['failure_json']),
            Persistence(row['persistence_state'], self._failure(row['persistence_failure_json'])),
            bool(row['dispatch_possible']))

    def mark_task_dispatch_possible(self, task_id: str) -> TaskStatus:
        """Commit conformance dispatch possibility separately from acceptance."""
        _validate_internal_id(task_id)
        with self._transaction("before_start") as database:
            row = database.execute(
                "SELECT t.state,t.action,s.helper_possible FROM tasks t "
                "JOIN scopes s ON s.task_id=t.id WHERE t.id=? AND t.owner_generation=?",
                (task_id, self.generation),
            ).fetchone()
            if (
                row is None
                or row["action"] != "conformance"
                or row["state"] not in {"pending", "running"}
                or row["helper_possible"] != 1
            ):
                raise OwnerError("dispatch_refused", "before_start")
            database.execute("UPDATE tasks SET dispatch_possible=1 WHERE id=?", (task_id,))
        return self.task_status(task_id)

    def task_running(self, task_id: str) -> None:
        _validate_internal_id(task_id)
        with self._transaction('running') as database:
            cursor = database.execute("UPDATE tasks SET state = 'running' WHERE id = ? AND owner_generation = ? AND state = 'pending'",
                (task_id, self.generation))
            if cursor.rowcount != 1:
                raise OwnerError('invalid_argument', 'running')

    def finish_task(self, task_id: str, *, result: TaskResult | None = None,
                    failure: Failure | None = None, state: str | None = None,
                    persistence_failure: Failure | None = None) -> TaskStatus:
        """Finish a task; only its own positively no-helper scope is released."""
        current = self.task_status(task_id)
        state = state or ('succeeded' if failure is None else 'failed')
        if current.owner_generation != self.generation or state not in {'succeeded', 'failed', 'unknown'}:
            raise OwnerError('invalid_argument', 'finalizing')
        if result is not None and (not isinstance(result, TaskResult) or result.kind != current.action):
            raise OwnerError('invalid_argument', 'finalizing')
        if persistence_failure is not None and not isinstance(persistence_failure, Failure):
            raise OwnerError('invalid_argument', 'finalizing')
        ready = None
        if result is not None and result.kind == 'export':
            exported = ExportResult.from_payload(result.to_payload()['payload'])
            if current.ref != ResourceRef('operation', exported.operation_id):
                raise OwnerError('invalid_argument', 'finalizing')
            if exported.state == 'ready':
                ready = exported
        with self._transaction('finalizing') as database:
            if ready is not None:
                if self.published_export(ready.operation_id, ready.export_id) is None:
                    raise OwnerError('export_unavailable', 'finalizing')
                database.execute('INSERT OR REPLACE INTO export_tasks VALUES(?,?,?)',
                    (task_id, ready.operation_id, ready.export_id))
            scope = database.execute('SELECT * FROM scopes WHERE task_id = ?', (task_id,)).fetchone()
            if scope is not None and not scope['helper_possible']:
                database.execute("UPDATE scopes SET state = 'released', cleanup_state = 'not_needed', cleanup_complete = 1 WHERE id = ?", (scope['id'],))
                database.execute("DELETE FROM reservations WHERE scope_id = ?", (scope['id'],))
            database.execute(
                "UPDATE tasks SET state=?, result_json=?, failure_json=?, persistence_state=?, "
                "persistence_failure_json=? WHERE id=?",
                (state, None if result is None or ready is not None else _json(result),
                 None if failure is None else _json(failure),
                 'failed' if persistence_failure is not None else 'saved',
                 None if persistence_failure is None else _json(persistence_failure), task_id),
            )
        return self.task_status(task_id)

    def mark_task_cleanup_required(self, task_id: str) -> TaskStatus:
        """Retain a conformance task's reservations after uncertain cleanup."""
        _validate_internal_id(task_id)
        with self._transaction("cleanup") as database:
            task = database.execute(
                "SELECT action FROM tasks WHERE id=? AND owner_generation=?",
                (task_id, self.generation),
            ).fetchone()
            if task is None or task["action"] != "conformance":
                raise OwnerError("not_found", "cleanup")
            database.execute(
                "UPDATE scopes SET state='cleanup_required', "
                "cleanup_state=CASE WHEN cleanup_complete=1 THEN cleanup_state ELSE 'unconfirmed' END, "
                "cleanup_complete=CASE WHEN cleanup_complete=1 THEN 1 ELSE NULL END "
                "WHERE task_id=?",
                (task_id,),
            )
        return self.task_status(task_id)

    def accept_target_gateway_scope(self, target_id: str) -> str:
        scope_id = _new_id()
        with self._transaction('target_start') as database:
            row = database.execute("SELECT state,execution_policy FROM targets WHERE id=? AND owner_generation=?",
                                   (target_id, self.generation)).fetchone()
            if row is None or row['state'] != 'starting' or row['execution_policy'] is None:
                raise OwnerError('dispatch_refused', 'target_start')
            database.execute("INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope_id, self.generation, 'target_gateway', target_id, target_id, None, None,
                 f'scopes/{scope_id}', 'active', 0, 'pending', None, None, 1))
        return scope_id

    def target_scope(self, target_id: str) -> dict[str, Any]:
        self._check_thread()
        _validate_internal_id(target_id)
        row = self._database.execute("SELECT * FROM scopes WHERE target_id = ? AND resource_kind = 'target'", (target_id,)).fetchone()
        if row is None:
            raise OwnerError('not_found', 'read')
        return dict(row)

    def resource_scopes(self, ref: ResourceRef, *, include_target: bool = False) -> tuple[dict, ...]:
        self._check_thread()
        if not isinstance(ref, ResourceRef):
            raise OwnerError('invalid_argument', 'cleanup')
        if ref.kind == 'operation':
            # ADR-0015: an operation's live scope is its current start's; earlier
            # starts' scopes are released history.
            rows = list(self._database.execute(
                'SELECT s.* FROM scopes s JOIN operations o ON s.operation_id=o.id AND s.start=o.start WHERE o.id = ? ORDER BY s.rowid',
                (ref.id,)))
        elif ref.kind == 'target':
            rows = list(self._database.execute('SELECT * FROM scopes WHERE target_id = ?', (ref.id,)))
        else:
            rows = list(self._database.execute('SELECT * FROM scopes WHERE resource_kind = ? AND resource_id = ?',
                (ref.kind, ref.id)))
        if include_target and ref.kind == 'operation':
            operation = self.operation_status(ref.id)
            rows.extend(self.resource_scopes(ResourceRef('target', operation.target_ref.id)))
        elif include_target and ref.kind == 'task':
            task = self.task_status(ref.id)
            if task.action == 'conformance' and task.ref is not None and task.ref.kind == 'target':
                rows.extend(self.resource_scopes(ResourceRef('target', task.ref.id)))
        return tuple(dict(row) for row in rows)

    def begin_reflector_probe(self, operation_id: str, *, launched_by: str | None):
        if launched_by not in {'lab', 'operator', None}:
            raise OwnerError('invalid_argument', 'before_start')
        with self._transaction('before_start') as database:
            row = database.execute("SELECT start FROM operations WHERE id=? AND owner_generation=?",
                                   (operation_id, self.generation)).fetchone()
            if row is None:
                raise OwnerError('not_found', 'before_start')
            database.execute("INSERT INTO reflector_probe_runs VALUES(?,?,?,?,0,NULL)",
                             (operation_id, row['start'], launched_by,
                              "not_declared" if launched_by is None else "stopped"))

    def accept_reflector_probe_scope(self, operation_id: str, *, restart: bool = False) -> str | None:
        """Accept a scope and its restart count together; None means this owned run ended."""
        scope_id = _new_id()
        with self._transaction('spawning') as database:
            row = database.execute("SELECT start,control_state FROM operations WHERE id=? AND owner_generation=?",
                                   (operation_id, self.generation)).fetchone()
            if row is None:
                raise OwnerError('dispatch_refused', 'spawning')
            if row['control_state'] in {'stopping', 'finished', 'released'}:
                return None
            if row['control_state'] not in {'reserved', 'running'}:
                raise OwnerError('dispatch_refused', 'spawning')
            database.execute("INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope_id, self.generation, 'reflector_probe', scope_id, None, operation_id, None,
                 f'scopes/{scope_id}', 'active', 0, 'pending', None, None, row['start']))
            if restart:
                updated = database.execute("UPDATE reflector_probe_runs SET restarts=restarts+1 "
                    "WHERE operation_id=? AND start=?", (operation_id, row['start']))
                if updated.rowcount != 1:
                    raise OwnerError('dispatch_refused', 'spawning')
            database.executemany("INSERT INTO owned_inventory VALUES(?,?,?)",
                [(operation_id, f'scopes/{scope_id}/{name}', 'file') for name in ('stdout.log', 'stderr.log')]
                + [(operation_id, f'scopes/{scope_id}/work', 'tree')])
        return scope_id

    def update_reflector_probe(self, operation_id: str, *, start: int, state: str,
                               restarts: int, last_exit: int | None):
        with self._transaction('running') as database:
            database.execute("UPDATE reflector_probe_runs SET state=?,restarts=?,last_exit=? "
                             "WHERE operation_id=? AND start=?",
                             (state, restarts, last_exit, operation_id, start))

    def reflector_probe_status(self, operation_id: str, *, start: int | None = None):
        """Private durable facts; None is unavailable evidence, never not_declared.

        A live fact from an orphaned/manual scope is not an assertion of liveness.
        Confirmed cleanup restores a stopped fact; explicit absence is its own row.
        """
        self._check_thread()
        row = self._database.execute(
            "SELECT p.launched_by,p.state,p.restarts,p.last_exit FROM reflector_probe_runs p "
            "JOIN operations o ON o.id=p.operation_id WHERE p.operation_id=? AND p.start=coalesce(?,o.start)",
            (operation_id, start)).fetchone()
        uncertain = self._database.execute(
            "SELECT 1 FROM scopes s JOIN operations o ON o.id=s.operation_id WHERE o.id=? "
            "AND s.start=coalesce(?,o.start) AND (s.state='orphaned' OR s.cleanup_state='manual')",
            (operation_id, start)).fetchone()
        return None if row is None or uncertain else dict(row)

    def target_has_operation(self, target_id: str) -> bool:
        self._check_thread()
        return self._database.execute('SELECT 1 FROM reservations r JOIN operations o ON o.id = r.operation_id WHERE o.target_id = ?',
            (target_id,)).fetchone() is not None

    def settle_scope_no_helper(self, scope_id: str):
        with self._transaction('cleanup') as database:
            row = database.execute('SELECT * FROM scopes WHERE id=?', (scope_id,)).fetchone()
            if row is None or row['helper_possible']:
                raise OwnerError('cleanup_required', 'cleanup')
            database.execute("UPDATE scopes SET cleanup_state='not_needed', cleanup_complete=1 WHERE id=?", (scope_id,))

    def mark_operation_running(self, operation_id: str) -> OperationStatus:
        """Publish launch without undoing a concurrent stop or terminal state."""
        self.operation_status(operation_id)
        with self._transaction('running') as database:
            database.execute("UPDATE operations SET control_state='running' "
                "WHERE id=? AND owner_generation=? AND control_state='reserved' AND dispatch_possible=1",
                (operation_id, self.generation))
        return self.operation_status(operation_id)

    def request_operation_stop(self, operation_id: str) -> OperationStatus:
        """Linearize stop against finalization on the owning database worker.

        Reserved remains reserved so a pre-GO stop can use the no-helper
        finalizer. The caller sends stop only for a returned active state.
        """
        self.operation_status(operation_id)
        with self._transaction('stop') as database:
            database.execute("UPDATE operations SET control_state='stopping' "
                "WHERE id=? AND owner_generation=? AND control_state='running'",
                (operation_id, self.generation))
        return self.operation_status(operation_id)

    def update_operation(self, operation_id: str, *, process=None,
                         target_cleanup=None, result=None, failure=None, refusal=None, core_terminal=None,
                         results_state=None):
        """Store facts only; control transitions have dedicated conditional SQL."""
        current = self.operation_status(operation_id)
        process = process or current.process
        cleanup = target_cleanup or current.target_cleanup
        result = result or current.result
        if current.results_state in {'deleting', 'delete_failed', 'deleted'}:
            result = current.result
            results_state = current.results_state
        value = OperationStatus(current.id, current.owner_generation, current.prepared_digest,
            current.target_ref, current.control_state, process, cleanup, result, failure or current.failure,
            results_state or current.results_state, current.executor, current.dispatch_possible,
            current.kind, starts=current.starts, refusal=refusal or current.refusal,
            core_terminal=core_terminal or current.core_terminal)
        with self._transaction('finalizing') as database:
            database.execute('UPDATE operations SET process_json=?, target_cleanup_state=?, target_cleanup_complete=?, result_json=?, failure_json=?, refusal_json=?, core_terminal_json=?, results_state=? WHERE id=?',
                (_json(process), cleanup.state, cleanup.complete,
                 None if result is None else _json(result), None if value.failure is None else _json(value.failure),
                 None if value.refusal is None else _json(value.refusal.to_payload()),
                 None if value.core_terminal is None else _json(value.core_terminal.to_payload()),
                 value.results_state, operation_id))
        return self.operation_status(operation_id)

    def set_target_state(self, target_id: str, *, state: str,
                         cleanup: CleanupFacts | None = None, failure: Failure | None = None) -> TargetStatus:
        current = self.target_status(target_id)
        if current.owner_generation != self.generation or state not in {'starting', 'ready', 'stopping', 'stopped', 'unknown', 'cleanup_required'}:
            raise OwnerError('invalid_argument', 'finalizing')
        cleanup = cleanup or current.cleanup
        with self._transaction('finalizing') as database:
            database.execute('UPDATE targets SET state = ?, cleanup_state = ?, cleanup_complete = ?, failure_json = ? WHERE id = ?',
                (state, cleanup.state, cleanup.complete, None if failure is None else _json(failure), target_id))
        if (state == 'stopped' and cleanup.state == 'confirmed' and cleanup.complete is True
                and not (current.state == 'stopped' and current.cleanup.state == 'confirmed'
                    and current.cleanup.complete is True)):
            self._clean_scratch(target_id)
        return self.target_status(target_id)

    def settle_target_no_helper(self, target_id: str, failure: Failure) -> TargetStatus:
        scope = self.target_scope(target_id)
        if scope['owner_generation'] != self.generation:
            raise OwnerError('invalid_argument', 'finalizing')
        with self._transaction('finalizing') as database:
            if scope['helper_possible'] or database.execute(
                    "SELECT 1 FROM scopes WHERE target_id=? AND id<>? AND (cleanup_complete IS NOT 1)",
                    (target_id, scope['id'])).fetchone():
                raise OwnerError('cleanup_required', 'finalizing')
            database.execute("UPDATE scopes SET state = 'released', cleanup_state = 'not_needed', cleanup_complete = 1 WHERE id = ?", (scope['id'],))
            database.execute('DELETE FROM reservations WHERE scope_id = ?', (scope['id'],))
            database.execute("UPDATE scopes SET state='released' WHERE target_id=?", (target_id,))
            database.execute("UPDATE targets SET state = 'stopped', cleanup_state = 'not_needed', cleanup_complete = 1, failure_json = ? WHERE id = ?",
                (_json(failure), target_id))
        return self.target_status(target_id)

    def accept_operation(
        self,
        *,
        prepared_digest: str,
        target_ref: TargetRef,
        executor: ExecutorIdentity,
        kind: str = "campaign",
        operation_id: str | None = None,
        scope_id: str | None = None,
        client_ref: str | None = None,
        approval: Approval | None = None,
        request_id: str | None = None,
    ) -> OperationAcceptance:
        if (
            not isinstance(target_ref, TargetRef)
            or not isinstance(executor, ExecutorIdentity)
            or kind not in {"campaign", "evaluation"}
            or executor.kind != f"lab-{kind}-worker/1"
        ):
            raise OwnerError("invalid_argument", "before_start")
        client_ref = _client_ref(client_ref, "before_start")
        operation_id = _validate_internal_id(operation_id or _new_id())
        scope_id = _validate_internal_id(scope_id or _new_id())
        if target_ref.owner_generation != self.generation:
            raise OwnerError("cleanup_required", "before_start")
        # Validate every stored typed value before opening the acceptance transaction.
        OperationStatus(
            id=operation_id,
            owner_generation=self.generation,
            prepared_digest=prepared_digest,
            target_ref=target_ref,
            control_state="reserved",
            process=ProcessFacts(),
            target_cleanup=CleanupFacts("not_needed", True),
            result=None,
            failure=None,
            results_state="not_created",
            executor=executor,
            kind=kind,
        )
        run_id = f"{'c' if kind == 'campaign' else 'e'}-{operation_id}"
        artifact_root = self.state_directory / "artifacts" / operation_id
        run_directory = artifact_root / operation_id
        process_json = _json(ProcessFacts())
        executor_json = _json(executor)
        with self._transaction("before_start") as database:
            if database.execute(
                "SELECT 1 FROM operations WHERE id = ? OR run_id = ?", (operation_id, run_id)
            ).fetchone():
                raise OwnerError("path_conflict", "before_start")
            self._reject_orphan_blockers(database)
            self._reject_probes(database)
            self._slot_available(database, ("execution", "target_use"))
            target = database.execute(
                "SELECT * FROM targets WHERE id = ? AND owner_generation = ?",
                (target_ref.id, self.generation),
            ).fetchone()
            if target is None:
                raise OwnerError("not_found", "before_start")
            if target["state"] != "ready":
                raise OwnerError("target_not_running", "before_start")
            database.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, self.generation, prepared_digest, kind, target_ref.id,
                 run_id, "reserved", 0, executor_json, process_json,
                 "not_needed", 1, None, None, None, None, "not_created", 1, scope_id, client_ref),
            )
            database.execute(
                "INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope_id, self.generation, "operation", operation_id, None, operation_id, None,
                 f"scopes/{scope_id}", "active", 0, "pending", None, None, 1),
            )
            scope_files = ["diagnostic.json", "stderr.log", "stdout.log"]
            if kind == "campaign":
                scope_files.append("core-entry.json")
            if kind == "evaluation":
                scope_files += ["evaluation-inputs.json", "evaluation-outcome.json"]
            database.executemany(
                "INSERT INTO owned_inventory VALUES(?,?,?)",
                [(operation_id, f"artifacts/{operation_id}", "tree")]
                + [(operation_id, f"scopes/{scope_id}/{name}", "file") for name in scope_files],
            )
            for slot in ("execution", "target_use"):
                database.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                    (slot, self.generation, scope_id, None, operation_id, None),
                )
            self._record_start_policy(database, operation_id, 1, target['execution_policy'])
            self._record_start_initiator(database, operation_id, 1, 'human')
            if approval is not None:
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind="operation", resource_id=operation_id, start=1)
        status = self.operation_status(operation_id)
        return OperationAcceptance(status, scope_id, run_id, artifact_root, run_directory)

    @staticmethod
    def _record_start_policy(database, operation_id, start, digest):
        from importlib.metadata import version
        database.execute("INSERT INTO operation_start_policies VALUES(?,?,?,?)",
                         (operation_id, start, digest, version('evolution-lab')))

    @staticmethod
    def _record_start_initiator(database, operation_id, start, initiated_by):
        if initiated_by not in {'human', 'auto_resume'}:
            raise OwnerError('invalid_argument', 'before_start')
        attempt = None
        if initiated_by == 'auto_resume':
            attempt = database.execute(
                "SELECT count(*) + 1 FROM operation_start_initiators WHERE operation_id=? AND initiated_by='auto_resume'",
                (operation_id,)).fetchone()[0]
        database.execute('INSERT INTO operation_start_initiators VALUES(?,?,?,?)',
                         (operation_id, start, initiated_by, attempt))

    def operation_policy_history(self, operation_id):
        self._check_thread()
        self.operation_status(operation_id)
        return tuple(dict(row) for row in self._database.execute(
            "SELECT start,policy_digest,lab_version FROM operation_start_policies WHERE operation_id=? ORDER BY start",
            (operation_id,)))

    def check_operation_policy(self, operation_id, policy_digest):
        from .policy import check_continuation
        self._check_thread()
        row = self._database.execute(
            "SELECT policy_digest FROM operation_start_policies WHERE operation_id=? AND start=1",
            (operation_id,)).fetchone()
        if row is None:
            raise OwnerError('artifacts_invalid', 'before_start')
        def document(digest):
            return None if digest is None else self.execution_policy(digest).to_payload()['policy']
        check_continuation(document(row['policy_digest']), document(policy_digest))

    def resume_precheck(self, operation_id: str, *, expected_start: int | None = None) -> OperationStatus:
        """Admission steps 1–3 (spec §2.3) from durable facts; no write."""
        self._check_thread()
        _validate_internal_id(operation_id)
        try:
            current = self.operation_status(operation_id)
        except OwnerError:
            raise OwnerError("not_found", "before_start") from None
        self._check_expected_start(current, expected_start)
        return self._resume_admissible(current)

    @staticmethod
    def _check_expected_start(current: OperationStatus, expected_start: int | None) -> None:
        if expected_start is not None and current.starts[-1].start != expected_start:
            raise OwnerError("state_changed", "before_start")

    def _resume_admissible(self, current: OperationStatus) -> OperationStatus:
        # Rows 1–6 of the one resume rule (owner/resume_rules.py).
        raise_for(durable_verdict(current, refusal=self.resume_refusal(current.id)))
        return current

    def accept_resume(self, operation_id: str, *, target_ref: TargetRef,
                      executor: ExecutorIdentity,
                      scope_id: str | None = None,
                      fresh_required: bool = False,
                      observed_start: int | None = None,
                      initiated_by: str = 'human', cancellation=None,
                      prepared_digest: str | None = None, expected_start: int | None = None,
                      approval: Approval | None = None, request_id: str | None = None) -> OperationAcceptance:
        """Freeze the current start and open the next one in one transaction (ADR-0015).

        ``fresh_required``: the caller's verdict carried a target requirement; re-check it
        against the durable starts. ``observed_start``: the start number the caller's verdict
        was computed from; a different durable start means the verdict is stale, so it is
        refused with ``resource_busy`` and the caller re-reads the status.
        ``expected_start``: the request's approved start; a mismatch refuses with ``state_changed``
        before evaluating a verdict that may only apply to the newer start.
        """
        if (not isinstance(target_ref, TargetRef) or not isinstance(executor, ExecutorIdentity)
                or executor.kind != "lab-campaign-worker/1"):
            raise OwnerError("invalid_argument", "before_start")
        _validate_internal_id(operation_id)
        if prepared_digest is not None:
            _digest(prepared_digest)
        scope_id = _validate_internal_id(scope_id or _new_id())
        if target_ref.owner_generation != self.generation:
            raise OwnerError("cleanup_required", "before_start")
        with (cancellation.guard() if cancellation is not None else nullcontext()), self._transaction("before_start") as database:
            if cancellation is not None and cancellation.is_set():
                raise OwnerError('dispatch_refused', 'before_start')
            try:
                current = self.operation_status(operation_id)
            except OwnerError:
                raise OwnerError("not_found", "before_start") from None
            self._check_expected_start(current, expected_start)
            self._resume_admissible(current)
            # A start accepted since the caller read its verdict makes the whole verdict stale
            # (a new suspension may carry a requirement the caller never saw): refuse, re-read.
            if observed_start is not None and current.starts[-1].start != observed_start:
                raise OwnerError("resource_busy", "resume")
            # Row 8 again from the durable record (spec 2026-09-29 §2.3): a start accepted
            # since the caller read its verdict is a used target too.
            if fresh_required and target_ref in used_targets(current):
                raise OwnerError("target_unstable_fresh_required", "before_start")
            self._reject_orphan_blockers(database)
            self._reject_probes(database)
            self._slot_available(database, ("execution", "target_use"))
            target = database.execute(
                "SELECT * FROM targets WHERE id = ? AND owner_generation = ?",
                (target_ref.id, self.generation),
            ).fetchone()
            if target is None:
                raise OwnerError("not_found", "before_start")
            if target["state"] != "ready":
                raise OwnerError("target_not_running", "before_start")
            self.check_operation_policy(operation_id, target['execution_policy'])
            row = database.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            database.execute(
                "INSERT INTO operation_starts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, row["start"], row["current_scope_id"], row["owner_generation"],
                 row["target_id"], row["process_json"], row["target_cleanup_state"],
                 row["target_cleanup_complete"], row["failure_json"], row["refusal_json"], row["core_terminal_json"], row["prepared_digest"]),
            )
            start = row["start"] + 1
            database.execute(
                "INSERT INTO scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope_id, self.generation, "operation", operation_id, None, operation_id, None,
                 f"scopes/{scope_id}", "active", 0, "pending", None, None, start),
            )
            database.execute(
                "UPDATE operations SET owner_generation=?, target_id=?, control_state='reserved', "
                "dispatch_possible=0, executor_json=?, process_json=?, target_cleanup_state='not_needed', "
                "target_cleanup_complete=1, failure_json=NULL, refusal_json=NULL, core_terminal_json=NULL, start=?, current_scope_id=?, prepared_digest=? WHERE id=?",
                (self.generation, target_ref.id, _json(executor), _json(ProcessFacts()),
                 start, scope_id, prepared_digest or row["prepared_digest"], operation_id),
            )
            database.executemany(
                "INSERT OR IGNORE INTO owned_inventory VALUES(?,?,?)",
                [(operation_id, f"scopes/{scope_id}/{name}", "file")
                 for name in ("diagnostic.json", "stderr.log", "stdout.log", "core-entry.json")],
            )
            for slot in ("execution", "target_use"):
                database.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                    (slot, self.generation, scope_id, None, operation_id, None),
                )
            self._record_start_policy(database, operation_id, start, target['execution_policy'])
            self._record_start_initiator(database, operation_id, start, initiated_by)
            if approval is not None:
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind="operation", resource_id=operation_id, start=start)
            if cancellation is not None and cancellation.is_set():
                raise OwnerError('dispatch_refused', 'before_start')
        status = self.operation_status(operation_id)
        artifact_root = self.state_directory / "artifacts" / operation_id
        return OperationAcceptance(status, scope_id, row["run_id"], artifact_root,
                                   artifact_root / operation_id)

    def owned_inventory(self, operation_id: str) -> tuple[dict, ...]:
        self.operation_status(operation_id)
        rows = self._database.execute(
            'SELECT path,kind FROM owned_inventory WHERE operation_id=? ORDER BY path',
            (operation_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def published_export(self, operation_id: str, export_id: str | None = None):
        """Without ``export_id``: the archive published by the operation's
        current start, if any (an earlier start's archive is stale after a
        resume, ADR-0015). With ``export_id``: that exact archive, of any start.
        A row marked deleted by a successful ``delete_results`` is never returned (ADR-0006)."""
        self.operation_status(operation_id)
        query = ('SELECT operation_id,export_id,sha256,path FROM operation_exports '
                 'WHERE operation_id=? AND deleted=0')
        args = (operation_id,)
        if export_id is not None:
            if not isinstance(export_id, str) or len(export_id) != 64 or any(c not in '0123456789abcdef' for c in export_id):
                raise OwnerError('invalid_argument', 'read')
            query += ' AND export_id=?'
            args += (export_id,)
        else:
            query += ' AND start=(SELECT start FROM operations WHERE id=?)'
            args += (operation_id,)
        row = self._database.execute(query + ' ORDER BY export_id LIMIT 1', args).fetchone()
        return None if row is None else dict(row)

    def publish_export(self, operation_id: str, export_id: str):
        """Register already fsynced bytes; caller verifies/publishes before this commit."""
        # Reuse the closed value's digest validation without persisting a manifest.
        ExportResult(operation_id, 'ready', export_id, export_id, {})
        with self._transaction('export') as database:
            current = self.operation_status(operation_id)
            if current.results_state in {'deleted', 'deleting', 'delete_failed'}:
                raise OwnerError('results_deleted' if current.results_state == 'deleted' else 'export_unavailable', 'export')
            # Identical bytes from a later start re-own the same archive file.
            database.execute(
                'INSERT INTO operation_exports(operation_id,export_id,sha256,path,start) '
                'SELECT ?,?,?,?,start FROM operations WHERE id=? '
                'ON CONFLICT(operation_id,export_id) DO UPDATE SET start=excluded.start',
                (operation_id, export_id, export_id,
                 f'artifacts/{operation_id}/exports/{export_id}.zip', operation_id))
        return self.published_export(operation_id, export_id)

    def task_export(self, task_id: str):
        task = self.task_status(task_id)
        if task.action != 'export' or task.ref is None:
            return None
        if self.operation_status(task.ref.id).results_state in {'deleting', 'delete_failed', 'deleted'}:
            return None
        row = self._database.execute(
            'SELECT e.operation_id,e.export_id,e.sha256,e.path FROM export_tasks t JOIN operation_exports e '
            'ON e.operation_id=t.operation_id AND e.export_id=t.export_id AND e.deleted=0 WHERE t.task_id=?',
            (task_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def deletion_record(self, operation_id: str):
        self.operation_status(operation_id)
        row = self._database.execute('SELECT * FROM deletions WHERE operation_id=?', (operation_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        for field in ('inventory', 'summary', 'remaining', 'failure'):
            value = record.pop(field + '_json')
            record[field] = None if value is None else json.loads(value)
        return record

    def accept_deletion(self, operation_id: str, *, summary_result=None, task_id=None,
                        completed_deletion: TaskStatus | None = None,
                        approval: Approval | None = None, request_id: str | None = None,
                        observed_start: int | None = None):
        """Caller holds write lease and checks live finalizers before this transaction.

        Snapshot fixed inventory once. Retries reuse it; this method does no IO.
        summary_result may supply a just-read projection whose full core is discarded.
        completed_deletion comes only from Owner._completed_deletion while holding
        the new write lease, after the prior Owner-held runner has actually ended.
        It authorizes retry without rewriting the prior failed persistence facts.
        """
        task_id = _validate_internal_id(task_id or _new_id())
        task_ref = TaskRef(task_id, self.generation)
        with self._transaction('delete') as database:
            if approval is not None:
                approved_start = self._approved_start(
                    database, ResourceRef('operation', operation_id), observed_start, 'delete')
            current = self.operation_status(operation_id)
            if current.control_state in {'reserved', 'running', 'stopping'}:
                raise OwnerError('resource_busy', 'delete')
            if current.control_state not in {'finished', 'released'}:
                raise OwnerError('cleanup_required', 'delete')
            scopes = database.execute('SELECT state FROM scopes WHERE operation_id=?', (operation_id,)).fetchall()
            if not scopes or any(row['state'] != 'released' for row in scopes):
                raise OwnerError('cleanup_required', 'delete')
            if database.execute('SELECT 1 FROM reservations WHERE operation_id=?', (operation_id,)).fetchone():
                raise OwnerError('resource_busy', 'delete')
            previous = self.deletion_record(operation_id)
            if completed_deletion is not None and previous is None:
                raise OwnerError('invalid_argument', 'delete')
            if previous is not None:
                prior_task = self.task_status(previous['task_id'])
                prior_state = prior_task.state
                if completed_deletion is not None:
                    if (not isinstance(completed_deletion, TaskStatus)
                            or completed_deletion.id != prior_task.id
                            or completed_deletion.owner_generation != prior_task.owner_generation
                            or completed_deletion.owner_generation != self.generation
                            or completed_deletion.action != prior_task.action
                            or completed_deletion.action != 'delete'
                            or completed_deletion.ref != prior_task.ref
                            or completed_deletion.ref != ResourceRef('operation', operation_id)
                            or completed_deletion.state not in {'succeeded', 'failed', 'unknown'}):
                        raise OwnerError('invalid_argument', 'delete')
                    prior_state = completed_deletion.state
                if prior_task.owner_generation == self.generation and prior_state in {'pending', 'running'}:
                    raise OwnerError('resource_busy', 'delete')
            result = summary_result or current.result
            if result is not None and (not isinstance(result, OperationResult) or result.kind != current.kind or result.prepared_digest != current.prepared_digest):
                raise OwnerError('invalid_argument', 'delete')
            if previous is None:
                retained = None if result is None else OperationResult(
                    current.prepared_digest, current.process, coverage=result.coverage,
                    stopped_reason=result.stopped_reason, persistence=result.persistence, kind=current.kind,
                    unmeasured=result.unmeasured, min_coverage=result.min_coverage)
                core = None if result is None else result.to_payload()['core']
                # Retain Lab's small outcome triple, never core's run-directory paths.
                outcome = durable_campaign_outcome(
                    core.get('campaign_outcome') if isinstance(core, dict) else None)
                summary = {'operation_id': operation_id, 'prepared_digest': current.prepared_digest,
                    'kind': current.kind, 'campaign_outcome': outcome,
                    'result': None if retained is None else retained.to_payload(),
                    'process': current.process.to_payload(), 'cleanup': current.target_cleanup.to_payload(),
                    'dispositions': [dict(row) for row in database.execute(
                        "SELECT scope_id,disposition,reason FROM dispositions WHERE resource_kind='operation' AND resource_id=? ORDER BY scope_id,disposition", (operation_id,))]}
                inventory = list(self.owned_inventory(operation_id))
            else:
                summary, inventory = previous['summary'], previous['inventory']
                retained = current.result
            state = 'deleted' if current.results_state == 'deleted' else 'deleting'
            database.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, self.generation, 'delete', 'operation', operation_id, 'pending', None, None, 'not_attempted', None, 0))
            database.execute('INSERT OR REPLACE INTO deletions VALUES(?,?,?,?,?,?,?)',
                (operation_id, task_id, _json(inventory), _json(summary), state,
                 _json([] if state == 'deleted' else inventory), None))
            database.execute('UPDATE operations SET results_state=?, result_json=? WHERE id=?',
                (state, None if retained is None else _json(retained), operation_id))
            if approval is not None:
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind='operation', resource_id=operation_id, start=approved_start)
        return TaskAcceptance(task_ref, None)

    def finish_deletion(self, operation_id: str, task_id: str, *, failure=None, remaining=()):
        """Commit filesystem outcome and small task result together."""
        result = DeletionResult(operation_id, 'deleted' if failure is None else 'failed', failure)
        with self._transaction('delete') as database:
            record = self.deletion_record(operation_id)
            task = self.task_status(task_id)
            if record is None or record['task_id'] != task_id or task.owner_generation != self.generation:
                raise OwnerError('invalid_argument', 'delete')
            if any(item not in record['inventory'] for item in remaining):
                raise OwnerError('invalid_argument', 'delete')
            if failure is None and remaining:
                raise OwnerError('invalid_argument', 'delete')
            state = 'deleted' if failure is None else 'delete_failed'
            database.execute('UPDATE deletions SET state=?,remaining_json=?,failure_json=? WHERE operation_id=?',
                (state, _json(list(remaining)), None if failure is None else _json(failure), operation_id))
            database.execute('UPDATE operations SET results_state=? WHERE id=?', (state, operation_id))
            database.execute("UPDATE tasks SET state=?,result_json=?,failure_json=?,persistence_state='saved' WHERE id=?",
                ('succeeded' if failure is None else 'failed', _json(TaskResult('delete', result)),
                 None if failure is None else _json(failure), task_id))
            if failure is None:
                database.execute('DELETE FROM export_tasks WHERE operation_id=?', (operation_id,))
                # W55 (1): keep the id as a mark of what was exported; its bytes are gone and no
                # reader returns a marked row (ADR-0006). Same transaction as results_state 'deleted'.
                database.execute('UPDATE operation_exports SET deleted=1 WHERE operation_id=?', (operation_id,))
        return self.task_status(task_id)

    def mark_helper_possible(self, scope_id: str) -> None:
        """Commit conservative spawn intent immediately before the Popen attempt."""
        _validate_internal_id(scope_id)
        with self._transaction("spawning") as database:
            cursor = database.execute(
                "UPDATE scopes SET helper_possible = 1 WHERE id = ? AND owner_generation = ? AND state = 'active'",
                (scope_id, self.generation),
            )
            if cursor.rowcount != 1:
                raise OwnerError("not_found", "spawning")

    def mark_dispatch_possible(self, operation_id: str) -> OperationStatus:
        _validate_internal_id(operation_id)
        with self._transaction("before_start") as database:
            row = database.execute(
                "SELECT o.control_state,s.helper_possible FROM operations o "
                "JOIN scopes s ON s.id = o.current_scope_id WHERE o.id = ? AND o.owner_generation = ?",
                (operation_id, self.generation),
            ).fetchone()
            if row is None:
                raise OwnerError("not_found", "before_start")
            if row["control_state"] != "reserved" or row["helper_possible"] != 1:
                raise OwnerError("dispatch_refused", "before_start")
            database.execute(
                "UPDATE operations SET dispatch_possible = 1 WHERE id = ?", (operation_id,)
            )
        return self.operation_status(operation_id)

    def settle_pre_dispatch_failure(self, operation_id: str, failure: Failure) -> OperationStatus:
        _validate_internal_id(operation_id)
        with self._transaction("finalizing") as database:
            row = database.execute(
                "SELECT o.control_state,o.dispatch_possible,s.id AS scope_id,s.helper_possible "
                "FROM operations o JOIN scopes s ON s.id = o.current_scope_id WHERE o.id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise OwnerError("not_found", "finalizing")
            if row["control_state"] != "reserved":
                raise OwnerError("invalid_argument", "finalizing")
            probes = database.execute(
                "SELECT s.* FROM scopes s JOIN operations o ON s.operation_id=o.id AND s.start=o.start "
                "WHERE o.id=? AND s.resource_kind='reflector_probe'", (operation_id,)).fetchall()
            probe_clean = all(s['cleanup_state'] in {'confirmed', 'not_needed'}
                              and s['cleanup_complete'] == 1 for s in probes)
            if row["dispatch_possible"] == 0 and row["helper_possible"] == 0 and probe_clean:
                database.execute(
                    "UPDATE operations SET control_state = 'released', failure_json = ?, process_json = ? WHERE id = ?",
                    (_json(failure), _json(ProcessFacts(False, None, True, False)), operation_id),
                )
                database.execute(
                    "UPDATE scopes SET state = 'released', cleanup_state = 'not_needed', "
                    "cleanup_complete = 1 WHERE id = ?",
                    (row["scope_id"],),
                )
                database.execute("DELETE FROM reservations WHERE scope_id = ?", (row["scope_id"],))
                for probe in probes:
                    database.execute("UPDATE scopes SET state='released' WHERE id=?", (probe['id'],))
            else:
                database.execute(
                    "UPDATE operations SET control_state = 'cleanup_required', failure_json = ? WHERE id = ?",
                    (_json(failure), operation_id),
                )
                database.execute(
                    "UPDATE scopes SET state = 'cleanup_required', cleanup_state = 'unconfirmed', "
                    "cleanup_complete = NULL WHERE id = ?",
                    (row["scope_id"],),
                )
        return self.operation_status(operation_id)

    def mark_operation_cleanup_required(self, operation_id: str) -> OperationStatus:
        _validate_internal_id(operation_id)
        self.operation_status(operation_id)
        with self._transaction("cleanup") as database:
            database.execute(
                "UPDATE operations SET control_state = 'cleanup_required' "
                "WHERE id = ? AND control_state IN ('reserved','running','stopping')",
                (operation_id,),
            )
            database.execute(
                "UPDATE scopes SET state = 'cleanup_required', cleanup_state = 'pending', "
                "cleanup_complete = NULL WHERE id = (SELECT current_scope_id FROM operations WHERE id = ?) "
                "AND EXISTS (SELECT 1 FROM operations o WHERE o.id=scopes.operation_id "
                "AND o.control_state='cleanup_required')",
                (operation_id,),
            )
        return self.operation_status(operation_id)

    def mark_target_cleanup_required(self, target_id: str) -> TargetStatus:
        _validate_internal_id(target_id)
        with self._transaction("cleanup") as database:
            cursor = database.execute(
                "UPDATE targets SET state = 'cleanup_required', cleanup_state = 'pending', "
                "cleanup_complete = NULL WHERE id = ? AND state <> 'stopped'",
                (target_id,),
            )
            if cursor.rowcount != 1:
                raise OwnerError("not_found", "cleanup")
            database.execute(
                "UPDATE scopes SET state = 'cleanup_required', cleanup_state = 'pending', "
                "cleanup_complete = NULL WHERE target_id = ?",
                (target_id,),
            )
        return self.target_status(target_id)

    def confirm_scope_cleanup(self, scope_id: str, owner_generation: str, *, forced: bool, child_exit_code: int | None = None) -> bool:
        """Record exact receipt facts without authorizing resource-slot release."""
        _validate_internal_id(scope_id)
        _validate_internal_id(owner_generation)
        if not isinstance(forced, bool):
            raise OwnerError("invalid_argument", "cleanup")
        with self._transaction("cleanup") as database:
            scope = database.execute(
                "SELECT * FROM scopes WHERE id = ? AND owner_generation = ?",
                (scope_id, owner_generation),
            ).fetchone()
            if scope is None:
                return False
            database.execute(
                "UPDATE scopes SET cleanup_state = 'confirmed', cleanup_complete = 1, forced = ? "
                "WHERE id = ?",
                (int(forced or scope['forced'] == 1), scope_id),
            )
            if scope['resource_kind'] == 'reflector_probe':
                database.execute(
                    "UPDATE reflector_probe_runs SET state=CASE WHEN state='exhausted' THEN state ELSE 'stopped' END, "
                    "last_exit=? WHERE operation_id=? AND start=? AND NOT EXISTS ("
                    "SELECT 1 FROM scopes later, scopes current WHERE current.id=? "
                    "AND later.operation_id=current.operation_id AND later.start=current.start "
                    "AND later.resource_kind='reflector_probe' AND later.rowid>current.rowid)",
                    (child_exit_code, scope['operation_id'], scope['start'], scope_id))
            if scope["operation_id"] is not None and forced:
                database.execute(
                    "UPDATE operations SET control_state = 'cleanup_required' "
                    "WHERE id = ? AND control_state IN ('reserved','running','stopping')",
                    (scope["operation_id"],),
                )
        return True

    def release_evidenced_reservations(
        self,
        ref: ResourceRef,
        *,
        scope_ids: tuple[str, ...],
        disposition: str,
    ) -> tuple[str, ...]:
        """Release exact linked scopes after the caller checked all non-process evidence.

        A receipt alone never calls this seam.  The higher-level finalizer supplies
        the complete scope set only after settlement and namespace sweep succeed.
        """
        if (
            not isinstance(ref, ResourceRef)
            or not scope_ids
            or len(set(scope_ids)) != len(scope_ids)
            or disposition not in {"normal", "confirmed_cleanup"}
        ):
            raise OwnerError("invalid_argument", "finalizing")
        checked_ids = tuple(_validate_internal_id(scope_id) for scope_id in scope_ids)
        with self._transaction("finalizing") as database:
            scopes = database.execute(
                f"SELECT * FROM scopes WHERE id IN ({','.join('?' for _ in checked_ids)})",
                checked_ids,
            ).fetchall()
            if len(scopes) != len(checked_ids):
                raise OwnerError("not_found", "finalizing")
            related_target_id: str | None = None
            if ref.kind == "operation":
                operation = database.execute(
                    "SELECT target_id FROM operations WHERE id = ?", (ref.id,)
                ).fetchone()
                if operation is None:
                    raise OwnerError("not_found", "finalizing")
                related_target_id = operation["target_id"]
            elif ref.kind == "task":
                task = database.execute(
                    "SELECT action,resource_kind,resource_id FROM tasks WHERE id=?",
                    (ref.id,),
                ).fetchone()
                if (
                    task is not None
                    and task["action"] == "conformance"
                    and task["resource_kind"] == "target"
                ):
                    related_target_id = task["resource_id"]
            for scope in scopes:
                directly_owned = (scope["resource_kind"] == ref.kind and scope["resource_id"] == ref.id
                    or ref.kind == 'target' and scope['resource_kind'] == 'target_gateway' and scope['target_id'] == ref.id)
                linked_target = (
                    ref.kind in {"operation", "task"}
                    and related_target_id is not None
                    and scope["resource_kind"] in {"target", "target_gateway"}
                    and scope["resource_id"] == related_target_id
                )
                linked_probe = (ref.kind == "operation" and scope["resource_kind"] == "reflector_probe"
                    and scope["operation_id"] == ref.id)
                if not (directly_owned or linked_target or linked_probe):
                    raise OwnerError("invalid_argument", "finalizing")
                if scope["cleanup_state"] not in {"confirmed", "not_needed"} or scope["cleanup_complete"] != 1:
                    raise OwnerError("cleanup_required", "finalizing")
            if ref.kind == "operation":
                pending = database.execute(
                    "SELECT s.id FROM scopes s JOIN operations o ON s.operation_id=o.id AND s.start=o.start "
                    "WHERE o.id=? AND s.resource_kind='reflector_probe' AND s.state<>'released'", (ref.id,))
                if any(row['id'] not in checked_ids for row in pending):
                    raise OwnerError("cleanup_required", "finalizing")
            target_ids = {scope['target_id'] for scope in scopes if scope['target_id'] is not None}
            for target_id in target_ids:
                pending = database.execute("SELECT id FROM scopes WHERE target_id=? AND state<>'released'", (target_id,))
                if any(row['id'] not in checked_ids for row in pending):
                    raise OwnerError("cleanup_required", "finalizing")
            for scope_id in checked_ids:
                database.execute("UPDATE scopes SET state = 'released' WHERE id = ?", (scope_id,))
                database.execute(
                    "INSERT OR IGNORE INTO dispositions VALUES(?,?,?,?,?,?,?,?)",
                    (_new_id(), self.generation, scope_id, ref.kind, ref.id,
                     disposition, None, time.time()),
                )
            database.execute(
                f"DELETE FROM reservations WHERE scope_id IN ({','.join('?' for _ in checked_ids)})",
                checked_ids,
            )
            if ref.kind == "operation":
                database.execute(
                    "UPDATE operations SET control_state = CASE "
                    "WHEN ? = 'normal' AND control_state IN ('reserved','running','stopping') THEN 'finished' "
                    "WHEN ? = 'confirmed_cleanup' AND control_state IN ('orphaned','cleanup_required') THEN 'released' "
                    "ELSE control_state END WHERE id = ?",
                    (disposition, disposition, ref.id),
                )
            if ref.kind == 'operation':
                database.execute("UPDATE reflector_probe_runs SET state='stopped' "
                    "WHERE operation_id=? AND launched_by='operator' AND start=(SELECT start FROM operations WHERE id=?)",
                    (ref.id, ref.id))
            if any(scope["resource_kind"] == "target" for scope in scopes):
                database.execute(
                    "UPDATE targets SET state = 'stopped', cleanup_state = 'confirmed', cleanup_complete = 1 WHERE id = ?", (related_target_id or ref.id,)
                )
                self._release_probe_rows(database, related_target_id or ref.id)
        if any(scope['resource_kind'] == 'target' for scope in scopes):
            self._clean_scratch(related_target_id or ref.id)
        return tuple(sorted(checked_ids))

    def release_after_manual_cleanup(self, ref: ResourceRef, *, reason: str, include_target: bool = False,
                                     completed_conformance: TaskStatus | None = None,
                                     approval: Approval | None = None, request_id: str | None = None,
                                     observed_start: int | None = None,
                                     approval_ref: ResourceRef | None = None) -> ReleaseReceipt:
        if not isinstance(ref, ResourceRef):
            raise OwnerError("invalid_argument", "release")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise OwnerError("invalid_argument", "release")
        with self._transaction("release") as database:
            if approval is not None:
                approved_start = self._approved_start(database, ref, observed_start, "release")
            conformance_state = self._conformance_cleanup_state(database, ref, completed_conformance)
            if ref.kind == "operation":
                scopes = database.execute(
                    "SELECT s.* FROM scopes s JOIN operations o ON s.operation_id=o.id AND s.start=o.start "
                    "WHERE o.id = ? ORDER BY s.rowid",
                    (ref.id,),
                ).fetchall()
            elif ref.kind == 'target':
                scopes = database.execute("SELECT * FROM scopes WHERE target_id=?", (ref.id,)).fetchall()
            else:
                scopes = database.execute(
                    "SELECT * FROM scopes WHERE resource_kind = ? AND resource_id = ?",
                    (ref.kind, ref.id),
                ).fetchall()
            if not scopes:
                raise OwnerError("not_found", "release")
            if ref.kind == "operation":
                resource = database.execute(
                    "SELECT control_state AS state FROM operations WHERE id = ?", (ref.id,)
                ).fetchone()
                eligible = resource is not None and resource["state"] in {"orphaned", "cleanup_required"}
            elif ref.kind == "target":
                resource = database.execute(
                    "SELECT state FROM targets WHERE id = ?", (ref.id,)
                ).fetchone()
                eligible = resource is not None and resource["state"] in {"unknown", "cleanup_required"}
            else:
                resource = database.execute(
                    "SELECT state FROM tasks WHERE id = ?", (ref.id,)
                ).fetchone()
                state = conformance_state if conformance_state is not None else (
                    None if resource is None else resource["state"]
                )
                eligible = state in {"unknown", "failed"}
            if not eligible:
                raise OwnerError("invalid_argument", "release")
            if include_target and ref.kind == 'operation':
                target_id = database.execute('SELECT target_id FROM operations WHERE id = ?', (ref.id,)).fetchone()[0]
                scopes.extend(database.execute('SELECT * FROM scopes WHERE target_id = ?', (target_id,)).fetchall())
            released_scopes: list[str] = []
            for scope in scopes:
                released_scopes.append(scope["id"])
                database.execute(
                    "UPDATE scopes SET state = 'released', cleanup_state = 'manual', "
                    "cleanup_complete = NULL WHERE id = ?",
                    (scope["id"],),
                )
                database.execute("DELETE FROM reservations WHERE scope_id = ?", (scope["id"],))
                database.execute(
                    "INSERT OR IGNORE INTO dispositions VALUES(?,?,?,?,?,?,?,?)",
                    (_new_id(), self.generation, scope["id"], ref.kind, ref.id,
                     "manual", reason, time.time()),
                )
            if ref.kind == "operation":
                database.execute(
                    "UPDATE operations SET control_state = 'released', target_cleanup_state = 'manual', target_cleanup_complete = NULL WHERE id = ?", (ref.id,)
                )
            for scope in scopes:
                if scope['target_id'] is not None:
                    database.execute("UPDATE targets SET state='stopped', cleanup_state='manual', cleanup_complete=NULL WHERE id=?", (scope['target_id'],))
                    self._release_probe_rows(database, scope['target_id'])
            if approval is not None:
                resource = approval_ref or ref
                self._insert_approval(database, approval=approval, request_id=request_id,
                                      resource_kind=resource.kind, resource_id=resource.id,
                                      start=approved_start, outcome="applied")
        return ReleaseReceipt(
            ref, reason, CleanupFacts("manual", None), True, tuple(sorted(released_scopes))
        )

    def operation_status(self, operation_id: str) -> OperationStatus:
        self._check_thread()
        _validate_internal_id(operation_id)
        row = self._database.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        executor = ExecutorIdentity.from_payload(json.loads(row["executor_json"]))
        failure = self._failure(row["failure_json"])
        refusal = self._refusal(row["refusal_json"])
        core_terminal = self._core_terminal(row["core_terminal_json"])
        target = self._database.execute("SELECT owner_generation FROM targets WHERE id = ?", (row["target_id"],)).fetchone()
        deletion = self._database.execute(
            'SELECT summary_json FROM deletions WHERE operation_id=?', (operation_id,)).fetchone()
        retained_summary = None
        if deletion is not None:
            saved = json.loads(deletion['summary_json'])
            retained_summary = {'campaign_outcome': saved['campaign_outcome'],
                'dispositions': saved['dispositions']}

        def settlement(scope_id):
            kinds = {r[0] for r in self._database.execute(
                "SELECT disposition FROM dispositions WHERE scope_id = ?", (scope_id,))}
            if "normal" in kinds:
                return "positive"
            return "not_positive" if kinds else "unknown"

        def target_ref(target_id):
            generation = self._database.execute(
                "SELECT owner_generation FROM targets WHERE id = ?", (target_id,)).fetchone()[0]
            return TargetRef(target_id, generation)

        initiators = {r['start']: (r['initiated_by'], r['auto_resume_attempt'])
                      for r in self._database.execute(
                          'SELECT start,initiated_by,auto_resume_attempt FROM operation_start_initiators WHERE operation_id=?',
                          (operation_id,))}
        starts = [StartFacts(
            r["start"], r["scope_id"], r["owner_generation"], target_ref(r["target_id"]),
            ProcessFacts(**json.loads(r["process_json"])),
            CleanupFacts(r["target_cleanup_state"], self._bool(r["target_cleanup_complete"])),
            self._failure(r["failure_json"]), settlement(r["scope_id"]),
            self._refusal(r["refusal_json"]), self._core_terminal(r["core_terminal_json"]),
            *initiators[r['start']],
        ) for r in self._database.execute(
            "SELECT * FROM operation_starts WHERE operation_id = ? ORDER BY start", (operation_id,))]
        current_process = ProcessFacts(**json.loads(row["process_json"]))
        current_cleanup = CleanupFacts(row["target_cleanup_state"], self._bool(row["target_cleanup_complete"]))
        starts.append(StartFacts(
            row["start"], row["current_scope_id"], row["owner_generation"],
            target_ref(row["target_id"]), current_process, current_cleanup, failure,
            settlement(row["current_scope_id"]), refusal, core_terminal, *initiators[row['start']]))
        return OperationStatus(
            id=row["id"], owner_generation=row["owner_generation"],
            prepared_digest=row["prepared_digest"],
            target_ref=TargetRef(row["target_id"], target["owner_generation"]),
            control_state=row["control_state"],
            process=current_process,
            target_cleanup=current_cleanup,
            result=self._operation_result(row['result_json']), failure=failure, refusal=refusal, core_terminal=core_terminal, results_state=row["results_state"],
            executor=executor, dispatch_possible=bool(row["dispatch_possible"]),
            kind=row["kind"],
            retained_summary=retained_summary,
            starts=tuple(starts),
        )

    def target_status(self, target_id: str) -> TargetStatus:
        self._check_thread()
        _validate_internal_id(target_id)
        row = self._database.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        return TargetStatus(
            row["id"], row["owner_generation"], row["launch_digest"], row["bundle_digest"],
            row["state"], row["base_url"], row["boot_id"],
            None if row["health_json"] is None else json.loads(row["health_json"]),
            CleanupFacts(row["cleanup_state"], self._bool(row["cleanup_complete"])),
            self._failure(row["failure_json"]), row["execution_policy"],
        )

    def list_operations(self) -> tuple[OperationStatus, ...]:
        self._check_thread()
        ids = [row[0] for row in self._database.execute("SELECT id FROM operations ORDER BY rowid")]
        return tuple(self.operation_status(operation_id) for operation_id in ids)

    def client_operations(self) -> tuple[tuple[str, str], ...]:
        self._check_thread()
        rows = self._database.execute("SELECT id, client_ref FROM operations WHERE client_ref IS NOT NULL ORDER BY rowid")
        return tuple((row[0], row[1]) for row in rows)

    def cycle_starts(self) -> dict[str, dict]:
        """operation id -> ``{"start", "export_id", "export_deleted", "results_state"}`` in ONE read.

        ``start`` is the order key of the operation's current start; larger means later.
        ``export_id`` is the archive that start published, or ``None`` (an earlier start's
        archive is stale after a resume, ADR-0015). ``export_deleted`` is true when a
        successful ``delete_results`` marked that archive (its bytes are gone; W55).
        ``results_state`` is the operation's. All come from one SELECT, which SQLite
        evaluates against a single read snapshot, and every write goes through this store's
        one thread, so a resume or a deletion cannot land between them (codex r1 C1).

        The key is the rowid of the start's scope. Every start (accept_operation and
        accept_resume) inserts exactly one scope row in the transaction that makes it the
        operation's ``current_scope_id`` and sets ``operations.start``; scope rows are never
        deleted and the schema is never rebuilt or vacuumed, so SQLite's max(rowid)+1 makes
        the key follow start order across operations, resumes and Owner restarts (spec
        §3.10, ADR-0015)."""
        self._check_thread()
        rows = self._database.execute(
            "SELECT o.id, s.rowid, e.export_id, e.deleted, o.results_state FROM operations o "
            "JOIN scopes s ON s.id = o.current_scope_id "
            "LEFT JOIN operation_exports e ON e.rowid = (SELECT x.rowid FROM operation_exports x "
            "WHERE x.operation_id = o.id AND x.start = o.start ORDER BY x.export_id LIMIT 1)")
        return {row[0]: {"start": row[1], "export_id": row[2], "export_deleted": row[3] == 1,
                         "results_state": row[4]} for row in rows}

    def client_targets(self) -> tuple[tuple[str, str, str], ...]:
        self._check_thread()
        rows = self._database.execute("SELECT id, bundle_digest, client_ref FROM targets WHERE client_ref IS NOT NULL ORDER BY rowid")
        return tuple((row[0], row[1], row[2]) for row in rows)

    def list_targets(self, bundle_digest: str | None = None, state: str | None = None) -> tuple[TargetStatus, ...]:
        self._check_thread()
        rows = self._database.execute(
            "SELECT id FROM targets WHERE (? IS NULL OR bundle_digest = ?) AND (? IS NULL OR state = ?) ORDER BY rowid",
            (bundle_digest, bundle_digest, state, state)).fetchall()
        return tuple(self.target_status(row[0]) for row in rows)

    def client_tasks(self) -> tuple[ClientTask, ...]:
        self._check_thread()
        rows = self._database.execute(
            "SELECT t.id, t.action, t.state, g.id, g.bundle_digest, g.client_ref FROM tasks t "
            "JOIN targets g ON t.resource_kind = 'target' AND t.resource_id = g.id "
            "WHERE g.client_ref IS NOT NULL AND t.action IN ('check', 'conformance') ORDER BY t.rowid"
        )
        return tuple(ClientTask(*row) for row in rows)

    def target_client_ref(self, target_id: str) -> str | None:
        self._check_thread()
        row = self._database.execute("SELECT client_ref FROM targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        return row["client_ref"]

    def blockers(self) -> tuple[ResourceRef, ...]:
        self._check_thread()
        rows = self._database.execute(
            "SELECT DISTINCT s.resource_kind,s.resource_id FROM reservations r "
            "JOIN scopes s ON s.id = r.scope_id ORDER BY s.resource_kind,s.resource_id"
        )
        return tuple(ResourceRef(row[0], row[1]) for row in rows)

    def reservations(self) -> dict[str, tuple[str, str]]:
        """Internal scheduling projection: slot -> (scope_id, generation)."""
        self._check_thread()
        return {
            row["slot"]: (row["scope_id"], row["owner_generation"])
            for row in self._database.execute("SELECT * FROM reservations ORDER BY slot")
        }

    def scope_state(self, scope_id: str) -> str:
        self._check_thread()
        _validate_internal_id(scope_id)
        row = self._database.execute("SELECT state FROM scopes WHERE id = ?", (scope_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        return row["state"]

    def debug_operation_row(self, operation_id: str) -> dict[str, Any]:
        """Intentional internal diagnostic used by Owner evidence/reporting."""
        self._check_thread()
        _validate_internal_id(operation_id)
        row = self._database.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            raise OwnerError("not_found", "read")
        return dict(row)

    @staticmethod
    def _operation_result(value):
        if value is None:
            return None
        return OperationResult.from_payload(json.loads(value))

    @staticmethod
    def _bool(value: Any) -> bool | None:
        return None if value is None else bool(value)

    @staticmethod
    def _failure(value: str | None) -> Failure | None:
        if value is None:
            return None
        payload = json.loads(value)
        return Failure(payload["code"], payload["stage"], payload["message"])

    @staticmethod
    def _core_terminal(value: str | None) -> CoreTerminal | None:
        return None if value is None else CoreTerminal.from_payload(json.loads(value))

    @staticmethod
    def _refusal(value: str | None) -> Refusal | None:
        if value is None:
            return None
        return Refusal.from_payload(json.loads(value))


__all__ = [
    "OperationAcceptance", "OwnerStateStore", "TargetAcceptance", "TaskAcceptance",
]
