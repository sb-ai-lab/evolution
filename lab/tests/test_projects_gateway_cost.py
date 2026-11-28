import sqlite3

import pytest

from evolution_lab.projects.gateway_cost import GatewayLedgerUnavailable, ledger_mark, units_to_amount, window_cost

_DDL = ("CREATE TABLE requests (id TEXT PRIMARY KEY, role TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT NOT NULL, "
        "reserved INTEGER NOT NULL, settled INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP)")


def _ledger(path, rows=()):
    db = sqlite3.connect(path)
    db.execute(_DDL)
    for i, (role, reserved, settled) in enumerate(rows):
        db.execute("INSERT INTO requests(id, role, model, request_hash, reserved, settled) VALUES (?,?,?,?,?,?)",
                   (f"r{i}", role, "m", "h", reserved, settled))
    db.commit()
    db.close()
    return path


def test_ledger_mark_empty_and_filled(tmp_path):
    assert ledger_mark(_ledger(tmp_path / "a.db")) == 0
    assert ledger_mark(_ledger(tmp_path / "b.db", [("x", 5, 4), ("y", 5, 5)])) == 2


def test_window_cost_roles_window_and_settled_preference(tmp_path):
    path = _ledger(tmp_path / "l.db", [("agent", 100, 40), ("other", 999, 999), ("agent", 7, 3), ("agent", 10, 10)])
    assert window_cost(path, after=0, until=4, roles=["agent"]) == (53, False)
    assert window_cost(path, after=1, until=3, roles=["agent"]) == (3, False)
    assert window_cost(path, after=0, until=4, roles=["agent", "other"]) == (1052, False)
    assert window_cost(path, after=0, until=4, roles=[]) == (0, False)


def test_window_cost_unsettled_is_upper_bound(tmp_path):
    path = _ledger(tmp_path / "l.db", [("agent", 100, None), ("agent", 5, 5)])
    assert window_cost(path, after=0, until=2, roles=["agent"]) == (105, True)


def test_missing_or_broken_ledger_is_unavailable(tmp_path):
    with pytest.raises(GatewayLedgerUnavailable):
        ledger_mark(tmp_path / "missing.db")
    with pytest.raises(GatewayLedgerUnavailable):
        window_cost(tmp_path / "missing.db", after=0, until=1, roles=["a"])
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(GatewayLedgerUnavailable):
        ledger_mark(bare)


def test_ledger_is_not_modified(tmp_path):
    path = _ledger(tmp_path / "l.db", [("agent", 1, 1)])
    before = path.read_bytes()
    ledger_mark(path)
    assert path.read_bytes() == before


def test_units_to_amount():
    assert units_to_amount(12_000_000) == "0.012000000"
    assert units_to_amount(0) == "0.000000000"
    assert units_to_amount(10**9) == "1.000000000"


@pytest.mark.parametrize("name", ["l?mode=rw.db", "l#frag.db", "l%20x.db"])
def test_ledger_path_with_uri_characters_is_read(tmp_path, name):
    # The read-only URI is built from the path, so `?`, `#` and `%` in it name the file, not URI parts.
    path = _ledger(tmp_path / name, [("agent", 7, 3)])
    assert ledger_mark(path) == 1
    assert window_cost(path, after=0, until=1, roles=["agent"]) == (3, False)
