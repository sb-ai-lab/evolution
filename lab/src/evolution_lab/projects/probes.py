"""Probe session and turn history: ``LAB_HOME/probes.sqlite3``, written only by the service (spec 2026-10-02 §3.6).

Not the registry and not pair state. A turn is ``pending`` from ``begin_turn`` until ``finish_turn``; a pending turn
found at start-up is an interrupted one (``mark_interrupted``). A pending turn's tokens and cost are unknown until
its record is written (PDR-0019: a ceiling that cannot be checked lets no turn through)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from evolution_lab.durability import Durability, effective

from .probe_turns import turn_record

_VERSION = 1
_SCHEMA = """
CREATE TABLE sessions(id TEXT PRIMARY KEY, project TEXT NOT NULL, implementation TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','closed')), closed_reason TEXT, reset_of TEXT REFERENCES sessions(id),
  created_at TEXT NOT NULL, closed_at TEXT, record TEXT NOT NULL);
CREATE TABLE turns(session_id TEXT NOT NULL REFERENCES sessions(id), n INTEGER NOT NULL, state TEXT NOT NULL
  CHECK(state IN ('pending','done')), request TEXT NOT NULL, started_at TEXT NOT NULL, record TEXT,
  tokens INTEGER NOT NULL DEFAULT 0, tokens_known INTEGER NOT NULL DEFAULT 0,
  cost_units INTEGER, cost_known INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id, n));
CREATE INDEX sessions_by_impl ON sessions(project, implementation, created_at);
"""
_SESSION_COLUMNS = ("id", "project", "implementation", "state", "closed_reason", "reset_of", "created_at", "closed_at")


class ProbeHistoryUnavailable(RuntimeError):
    """``probes.sqlite3`` cannot be opened: another history version, a damaged file or an IO failure. The service
    answers ``history_unavailable`` for probe calls until the file is fixed (spec §5.1)."""


class ProbeStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._lock = threading.RLock()

    @classmethod
    def open(cls, home: Path, *, durability: Durability | None = None) -> ProbeStore:
        durability = effective() if durability is None else durability
        path = Path(home) / "probes.sqlite3"
        db = None
        try:
            fresh = not path.exists()
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            journal = "wal" if durability == 'relaxed' else "delete"
            if db.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] != journal:
                raise sqlite3.OperationalError("could not set journal mode")
            db.execute("PRAGMA synchronous=NORMAL" if durability == 'relaxed' else "PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys = ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if fresh or version == 0:
                db.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version = {_VERSION}; COMMIT;")
            elif version != _VERSION:
                raise ProbeHistoryUnavailable("unsupported probe history version")
        except (sqlite3.Error, OSError, ProbeHistoryUnavailable) as error:
            if db is not None:
                db.close()
            if isinstance(error, ProbeHistoryUnavailable):
                raise
            raise ProbeHistoryUnavailable(type(error).__name__) from error
        return cls(db)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _session_row(self, row: tuple) -> dict[str, Any]:
        out = dict(zip(_SESSION_COLUMNS, row[:-1]))
        out["record"] = json.loads(row[-1])
        return out

    def create_session(self, row: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions(id, project, implementation, state, reset_of, created_at, record) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?)",
                (row["id"], row["project"], row["implementation"], row.get("reset_of"), row["created_at"],
                 json.dumps(row["record"])),
            )

    def session(self, id: str) -> dict | None:
        with self._lock:
            found = self._db.execute(
                f"SELECT {', '.join(_SESSION_COLUMNS)}, record FROM sessions WHERE id = ?", (id,)).fetchone()
        return self._session_row(found) if found else None

    def sessions(self, project: str, implementation: str | None = None) -> list[dict]:
        query = f"SELECT {', '.join(_SESSION_COLUMNS)}, record FROM sessions WHERE project = ?"
        args: list[Any] = [project]
        if implementation is not None:
            query += " AND implementation = ?"
            args.append(implementation)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY created_at, rowid", args).fetchall()
        return [self._session_row(row) for row in rows]

    def open_sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(_SESSION_COLUMNS)}, record FROM sessions WHERE state = 'open' "
                "ORDER BY created_at, rowid").fetchall()
        return [self._session_row(row) for row in rows]

    def close_session(self, id: str, reason: str, closed_at: str) -> None:
        with self._lock:
            self._db.execute("UPDATE sessions SET state = 'closed', closed_reason = ?, closed_at = ? WHERE id = ? AND state = 'open'",
                             (reason, closed_at, id))  # a repeated close is a no-op

    def begin_turn(self, session_id: str, n: int, request: str, started_at: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO turns(session_id, n, state, request, started_at) VALUES (?, ?, 'pending', ?, ?)",
                             (session_id, n, request, started_at))

    def discard_turn(self, session_id: str, n: int) -> None:
        """Drop a pending turn the Owner refused before sending anything: it never was a turn."""
        with self._lock:
            self._db.execute("DELETE FROM turns WHERE session_id = ? AND n = ? AND state = 'pending'", (session_id, n))

    def finish_turn(self, session_id: str, n: int, record: dict, *, tokens: int, tokens_known: bool,
                    cost_units: int | None, cost_known: bool,
                    ceiling: Callable[[dict], dict] | None = None) -> dict | None:
        """Finish a pending turn: its record, spend and, with ``ceiling``, the chain ceiling after it
        (``ceiling(chain_usage)``) in the record, all in one transaction, so a failed write leaves the turn
        pending with its spend unknown, never finished without its ceiling. Returns the stored record, or
        ``None`` when the turn was not pending (a repeated finish is a no-op)."""
        with self._lock, self._transaction():
            if self._db.execute(
                    "UPDATE turns SET state = 'done', record = ?, tokens = ?, tokens_known = ?, cost_units = ?, "
                    "cost_known = ? WHERE session_id = ? AND n = ? AND state = 'pending'",
                    (json.dumps(record), tokens, int(tokens_known), cost_units, int(cost_known), session_id,
                     n)).rowcount == 0:
                return None
            if ceiling is not None:
                record = {**record, "ceiling": ceiling(self.chain_usage(session_id))}
                self._db.execute("UPDATE turns SET record = ? WHERE session_id = ? AND n = ?",
                                 (json.dumps(record), session_id, n))
            return record

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self._db.in_transaction:  # SQLite may have rolled back on its own (a full disk, an IO error)
                self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def turns(self, session_id: str) -> list[dict]:
        """Finished turn records of one session, in order."""
        with self._lock:
            rows = self._db.execute("SELECT record FROM turns WHERE session_id = ? AND state = 'done' ORDER BY n",
                                    (session_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def pending_turns(self) -> list[tuple[str, int]]:
        with self._lock:
            return [(r[0], r[1]) for r in self._db.execute(
                "SELECT session_id, n FROM turns WHERE state = 'pending' ORDER BY started_at, session_id, n")]

    def mark_interrupted(self, session_id: str, n: int, finished_at: str, *,
                         ceiling: Callable[[dict], dict] | None = None) -> None:
        """A pending turn the service did not finish: a failed record, tokens and cost unknown."""
        with self._lock:
            found = self._db.execute("SELECT request, started_at FROM turns WHERE session_id = ? AND n = ? "
                                     "AND state = 'pending'", (session_id, n)).fetchone()
            if found is None:
                return
            record = turn_record(session_id=session_id, n=n, started_at=found[1], finished_at=finished_at,
                                 content=found[0], result=None, failure="interrupted", cost=None)
            self.finish_turn(session_id, n, record, tokens=0, tokens_known=False, cost_units=None, cost_known=False,
                             ceiling=ceiling)

    def chain_usage(self, session_id: str) -> dict[str, Any]:
        """Usage summed over the whole ``reset_of`` chain the session belongs to (spec §3.5, §4.4)."""
        with self._lock:
            ids = self._chain(session_id)
            marks = ",".join("?" * len(ids))
            turns, tokens, unknown_tokens, cost, unknown_cost = self._db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens), 0), COALESCE(SUM(1 - tokens_known), 0), "
                f"COALESCE(SUM(cost_units), 0), COALESCE(SUM(1 - cost_known), 0) "
                f"FROM turns WHERE session_id IN ({marks})", ids).fetchone()
        return {"turns": turns, "tokens": tokens, "tokens_incomplete": bool(unknown_tokens),
                "cost_units": cost, "cost_unavailable": bool(unknown_cost)}

    def _chain(self, session_id: str) -> list[str]:
        root = session_id
        while (parent := self._db.execute("SELECT reset_of FROM sessions WHERE id = ?", (root,)).fetchone()) \
                and parent[0]:
            root = parent[0]
        ids, frontier = [root], [root]
        while frontier:
            frontier = [r[0] for current in frontier
                        for r in self._db.execute("SELECT id FROM sessions WHERE reset_of = ?", (current,))]
            ids += frontier
        return ids
