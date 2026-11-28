"""Read-only view of the gateway ledger for probe-turn cost (spec 2026-10-02 §4.3).

The ledger is ``budget.py``'s ``requests`` table (1 unit = 1e-9 of the currency). It may use WAL and is opened
read-only with a timeout; any failure to read is ``GatewayLedgerUnavailable`` and never a guess.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from decimal import Decimal
from pathlib import Path

_UNITS_PER_CURRENCY = 10**9


class GatewayLedgerUnavailable(RuntimeError):
    """The declared ledger cannot be read."""


def _connect(path: Path) -> sqlite3.Connection:
    # as_uri() percent-encodes the path: a `?`, `#` or `%` in it names the file, not URI parts.
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)


def ledger_mark(path: Path) -> int:
    """The highest ``requests`` rowid (0 when empty): the window edge before or after a turn."""
    try:
        with closing(_connect(path)) as db:
            return int(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM requests").fetchone()[0])
    except (sqlite3.Error, OSError) as exc:
        raise GatewayLedgerUnavailable(type(exc).__name__) from exc


def window_cost(path: Path, *, after: int, until: int, roles: Sequence[str]) -> tuple[int, bool]:
    """``(units, upper_bound)`` of rows with ``after < rowid <= until`` and ``role`` in ``roles``.

    A row counts ``COALESCE(settled, reserved)``; ``upper_bound`` is true when any such row is still unsettled."""
    roles = list(roles)
    if not roles:
        return 0, False
    marks = ",".join("?" * len(roles))
    try:
        with closing(_connect(path)) as db:
            units, unsettled = db.execute(
                f"SELECT COALESCE(SUM(COALESCE(settled, reserved)), 0), COALESCE(SUM(settled IS NULL), 0) "
                f"FROM requests WHERE rowid > ? AND rowid <= ? AND role IN ({marks})",
                (after, until, *roles),
            ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        raise GatewayLedgerUnavailable(type(exc).__name__) from exc
    return int(units), bool(unsettled)


def units_to_amount(units: int) -> str:
    return f"{Decimal(units) / Decimal(_UNITS_PER_CURRENCY):.9f}"
