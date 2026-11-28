"""Durability modes through real SQLite files, including crash leftovers and readers."""
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

from evolution_lab.budget import BudgetLedger
from evolution_lab.owner.models import OwnerError
from evolution_lab.owner.store import OwnerStateStore
from evolution_lab.projects.probes import ProbeStore
from evolution_lab.projects.store import RegistryStore


@pytest.mark.parametrize('existing', [False, True])
def test_budget_wal_and_shm_are_private_in_existing_directory(monkeypatch, tmp_path, existing):
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    tmp_path.chmod(0o755)
    path = tmp_path / 'budget.sqlite3'
    if existing:
        path.touch()
        path.chmod(0o644)
    old_umask = os.umask(0o022)
    ledger = None
    try:
        ledger = BudgetLedger(path, limit_units=100, identity='test')
        ledger.reserve(2, role='target', model='test', request_hash='one')
        for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
            assert stat.S_IMODE(file.stat().st_mode) == 0o600, file.name
    finally:
        os.umask(old_umask)
        if ledger is not None:
            ledger.close_anchor()


@pytest.mark.parametrize('mode,synchronous,rollback_mode', [('relaxed', 1, 'wal'), ('strict', 2, 'delete')])
def test_store_modes(monkeypatch, tmp_path, mode, synchronous, rollback_mode):
    monkeypatch.setenv('EVOLUTION_DURABILITY', mode)
    owner = OwnerStateStore.open(tmp_path / 'state')
    probes = ProbeStore.open(tmp_path)
    registry = RegistryStore.open(tmp_path / 'registry.sqlite3')
    ledger = BudgetLedger(tmp_path / 'budget.sqlite3', limit_units=100, identity='test')
    try:
        with ledger.connection() as db:
            for connection, journal in [(owner._database, rollback_mode), (probes._db, rollback_mode), (registry._db, 'wal'), (db, rollback_mode)]:
                assert connection.execute('PRAGMA journal_mode').fetchone()[0] == journal
                assert connection.execute('PRAGMA synchronous').fetchone()[0] == synchronous
    finally:
        owner.close()
        probes.close()
        registry.close()
        ledger.close_anchor()


def test_relaxed_then_strict_with_leftover_wal(monkeypatch, tmp_path):
    state = tmp_path / 'state'
    code = '''import os, sys
from evolution_lab.owner.store import OwnerStateStore
store = OwnerStateStore.open(sys.argv[1])
print(store.generation, flush=True)
os._exit(0)
'''
    env = dict(os.environ, EVOLUTION_DURABILITY='relaxed')
    generation = subprocess.check_output([sys.executable, '-I', '-c', code, str(state)], env=env, text=True).strip()
    assert (state / 'owner.sqlite3-wal').exists()
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'strict')
    with OwnerStateStore.open(state) as store:
        assert store._database.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert store._database.execute('SELECT id FROM generations WHERE id=?', (generation,)).fetchone()[0] == generation


def test_strict_open_refused_while_held(monkeypatch, tmp_path):
    state = tmp_path / 'state'
    with OwnerStateStore.open(state, durability='relaxed'):
        pass
    code = '''import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    db.execute('SELECT * FROM generations').fetchall()
    print('ready', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-I', '-c', code, str(state / 'owner.sqlite3')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        monkeypatch.setenv('EVOLUTION_DURABILITY', 'strict')
        with pytest.raises(OwnerError) as exc:
            OwnerStateStore.open(state)
        assert exc.value.code == 'io_error'
        assert exc.value.stage == 'connect'
        with sqlite3.connect(state / 'owner.sqlite3') as db:
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    finally:
        child.communicate('', timeout=10)
    assert child.returncode == 0


def test_budget_anchor_prevents_per_operation_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    path = tmp_path / 'budget.sqlite3'
    ledger = BudgetLedger(path, limit_units=100, identity='test')
    ledger.open_anchor()
    try:
        for i in range(20):
            request = ledger.reserve(2, role='target', model='test', request_hash=str(i))
            ledger.settle(request, 1, source="upstream_cost", tariff_actual=None)
        assert Path(str(path) + '-wal').stat().st_size > 0
        assert ledger.summary()['settled_upstream_units'] == 20
    finally:
        ledger.close_anchor()
    assert not Path(str(path) + '-wal').exists()


def test_readonly_readers_after_relaxed(monkeypatch, tmp_path):
    from evolution_lab.owner.routing import _read_ledger
    from evolution_lab.projects.gateway_cost import ledger_mark, window_cost

    monkeypatch.setenv('EVOLUTION_DURABILITY', 'relaxed')
    path = tmp_path / 'budget.sqlite3'
    ledger = BudgetLedger(path, limit_units=100, identity='test')
    request = ledger.reserve(2, role='target', model='test', request_hash='one', routing=('https://example.invalid/v1', 'test', 'test', '["operation:op:start:1"]'))
    ledger.settle(request, 1, source="upstream_cost", tariff_actual=None)
    ledger.close_anchor()
    assert ledger_mark(path) == 1
    assert window_cost(path, after=0, until=1, roles=['target']) == (1, False)
    groups = {}
    assert _read_ledger(path, {'operation:op:start:1'}, groups) == (0, 0)
    assert groups
    registry_path = tmp_path / 'registry.sqlite3'
    registry = RegistryStore.open(registry_path)
    version = registry.version
    registry.close()
    reader = RegistryStore.open(registry_path, read_only=True)
    try:
        assert reader.version == version
    finally:
        reader.close()
