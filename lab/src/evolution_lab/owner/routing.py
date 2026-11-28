"""Read-only Lab routing projection. No Core reads or budget-ledger migrations."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


_REASONS = {'no_policy', 'ledger_unreadable', 'ledger_unsupported', 'ledger_invalid'}
_OBSERVATION_COLUMNS = {'request_id', 'endpoint', 'upstream_model', 'client_model',
                        'requested_provider', 'requested_providers', 'reported_provider', 'uses_json'}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def _count(value):
    return type(value) is int and value >= 0


def _strings(value):
    return isinstance(value, list) and all(_text(s) for s in value) and len(set(value)) == len(value)


def _endpoint(value):
    if not _text(value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in {'http', 'https'} and bool(url.hostname)
                and url.username is None and url.password is None and not url.query and not url.fragment)
    except ValueError:
        return False


def validate_routing(value):
    """Validate the shared closed Python/service/CLI form, returning detached JSON."""
    if not isinstance(value, dict) or set(value) != {
            'schema', 'operation_id', 'runs', 'available', 'reason', 'roles', 'shared', 'outside'}:
        raise ValueError('invalid routing')
    if (value['schema'] != 'lab-operation-routing/1' or not _text(value['operation_id'])
            or type(value['available']) is not bool or not isinstance(value['runs'], list)):
        raise ValueError('invalid routing')
    from .models import _identifier
    _identifier(value['operation_id'])
    previous = 0
    policy_present = False
    for run in value['runs']:
        if not isinstance(run, dict) or set(run) != {
                'start', 'policy_digest', 'author', 'channel', 'confirmed_at', 'lab_version'}:
            raise ValueError('invalid routing run')
        if not _count(run['start']) or run['start'] <= previous or not _text(run['lab_version']):
            raise ValueError('invalid routing run')
        previous = run['start']
        if run['policy_digest'] is None:
            if any(run[key] is not None for key in ('author', 'channel', 'confirmed_at')):
                raise ValueError('invalid routing confirmation')
        else:
            policy_present = True
            if not isinstance(run['policy_digest'], str) or re.fullmatch('[0-9a-f]{64}', run['policy_digest']) is None:
                raise ValueError('invalid policy digest')
            when = run['confirmed_at']
            if (not _text(run['author']) or not _text(run['channel'])
                    or type(when) not in {int, float} or not math.isfinite(when) or when <= 0):
                raise ValueError('invalid routing confirmation')
    if not value['available']:
        if (value['reason'] not in _REASONS or any(value[key] is not None for key in ('roles', 'shared', 'outside'))
                or (value['reason'] == 'no_policy') == policy_present):
            raise ValueError('invalid routing availability')
    else:
        if (value['reason'] is not None or not policy_present or not isinstance(value['roles'], dict)
                or not _count(value['shared']) or not _count(value['outside'])):
            raise ValueError('invalid routing availability')
        for role, rows in value['roles'].items():
            if not _text(role) or not isinstance(rows, list):
                raise ValueError('invalid routing role')
            for row in rows:
                if not isinstance(row, dict) or set(row) != {'endpoint', 'upstream_model', 'client_model',
                        'requested', 'reported_provider', 'calls', 'mismatched'}:
                    raise ValueError('invalid routing row')
                requested, reported = row['requested'], row['reported_provider']
                if (not _endpoint(row['endpoint']) or not _text(row['upstream_model'])
                        or not _text(row['client_model'])
                        or not (requested is None or _text(requested) or (_strings(requested) and requested))
                        or not (reported is None or _text(reported)) or not _count(row['calls']) or row['calls'] == 0
                        or not _count(row['mismatched']) or row['mismatched'] > row['calls']):
                    raise ValueError('invalid routing row')
                expected = (reported is not None and requested is not None and
                            (reported != requested if isinstance(requested, str) else reported not in requested))
                if row['mismatched'] != (row['calls'] if expected else 0):
                    raise ValueError('invalid routing mismatch')
    return json.loads(json.dumps(value, allow_nan=False))


class _LedgerUnavailable(Exception):
    def __init__(self, reason):
        self.reason = reason


class SnapshotUnavailable(ValueError):
    """The input database cannot be inspected without replaying a live journal."""


@contextmanager
def saved_snapshot(path: Path):
    """Read an existing checkpoint without creating or modifying SQLite sidecars."""
    path = path.absolute()

    def identity():
        if path.is_symlink() or not path.is_file():
            raise SnapshotUnavailable("saved database unavailable")
        for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")):
            if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
                raise SnapshotUnavailable("supply a checkpointed database for offline preflight")
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    before = identity()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)) as db:
        yield db
        if identity() != before:
            raise SnapshotUnavailable("saved database changed during preflight")


def _read_ledger(path, operation_uses, groups, *, expected_identity=None):
    """One snapshot, named public columns only. URI escaping preserves ?, # and %."""
    shared = outside = 0
    try:
        connection = (closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=5))
                      if expected_identity is None else saved_snapshot(Path(path)))
        with connection as db:
            db.execute('BEGIN')
            if expected_identity is not None and db.execute(
                    "SELECT identity FROM configuration WHERE singleton=1").fetchone() != (expected_identity,):
                raise _LedgerUnavailable('ledger_invalid')
            columns = {r[1] for r in db.execute('PRAGMA table_info(routing_observations)')}
            if not _OBSERVATION_COLUMNS <= columns:
                raise _LedgerUnavailable('ledger_unsupported')
            for role, endpoint, upstream, client, single, ordered, reported, raw_uses in db.execute(
                    'SELECT r.role,o.endpoint,o.upstream_model,o.client_model,o.requested_provider,'
                    'o.requested_providers,o.reported_provider,o.uses_json FROM requests r '
                    'LEFT JOIN routing_observations o ON o.request_id=r.id ORDER BY r.rowid'):
                try:
                    uses = json.loads(raw_uses)
                    if not _strings(uses):
                        raise ValueError()
                except (TypeError, ValueError):
                    raise _LedgerUnavailable('ledger_invalid') from None
                if not uses:
                    outside += 1
                elif operation_uses.intersection(uses):
                    if len(uses) > 1:
                        shared += 1
                        continue
                    try:
                        requested = json.loads(ordered) if ordered is not None else single
                        row = dict(endpoint=endpoint, upstream_model=upstream, client_model=client,
                                   requested=requested, reported_provider=reported, calls=1, mismatched=0)
                        # Validate metadata before grouping, including non-secret endpoint form.
                        if (not _text(role) or not _endpoint(endpoint) or not _text(upstream) or not _text(client)
                                or not (requested is None or _text(requested) or (_strings(requested) and requested))
                                or not (reported is None or _text(reported)) or (ordered is not None and single is not None)):
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise _LedgerUnavailable('ledger_invalid') from None
                    mismatch = (reported is not None and requested is not None and
                                (reported != requested if isinstance(requested, str) else reported not in requested))
                    key = (role, endpoint, upstream, client, json.dumps(requested), reported)
                    if key not in groups:
                        groups[key] = row
                        row['calls'] = 0
                    groups[key]['calls'] += 1
                    groups[key]['mismatched'] += int(mismatch)
    except (sqlite3.Error, OSError, ValueError):
        raise _LedgerUnavailable('ledger_unreadable') from None
    return shared, outside


def preflight_route_observations(policy, operation_id, starts):
    """Operation-attributed route rows from the ledger the selected policy confirmed."""
    groups = {}
    uses = {f"operation:{operation_id}:start:{start}" for start in starts}
    try:
        _read_ledger(policy.gateway.ledger_path, uses, groups, expected_identity=policy.gateway.ledger_identity)
    except _LedgerUnavailable:
        return {}
    roles = defaultdict(list)
    for key, row in groups.items():
        roles[key[0]].append(row)
    return roles


def build_routing(operation_id, history, records):
    """Project persisted start confirmations and each distinct policy ledger once."""
    runs, paths = [], set()
    for start in history:
        record = records.get(start['policy_digest'])
        runs.append(dict(start, **{key: record[key] if record is not None else None
                                  for key in ('author', 'channel', 'confirmed_at')}))
        if record is not None and 'gateway' in record['policy']:
            paths.add(record['policy']['gateway']['ledger_path'])
    result = dict(schema='lab-operation-routing/1', operation_id=operation_id, runs=runs,
                  available=False, reason='no_policy', roles=None, shared=None, outside=None)
    if not paths:
        return result
    operation_uses = {f'operation:{operation_id}:start:{run["start"]}' for run in runs}
    groups = {}
    shared = outside = 0
    try:
        for path in sorted(paths):
            new_shared, new_outside = _read_ledger(path, operation_uses, groups)
            shared += new_shared
            outside += new_outside
    except _LedgerUnavailable as error:
        result['reason'] = error.reason
        return result
    roles = defaultdict(list)
    for key in sorted(groups, key=lambda k: json.dumps(k, ensure_ascii=True)):
        roles[key[0]].append(groups[key])
    result.update(available=True, reason=None, roles=dict(roles), shared=shared, outside=outside)
    return validate_routing(result)


async def operation_routing(owner, ref):
    """Use Owner's DB executor and tracked result thread; caller owns the read lease."""
    history = await owner._db('operation_policy_history', ref.id)
    records = {}
    for start in history:
        digest = start['policy_digest']
        if digest is not None and digest not in records:
            records[digest] = (await owner.execution_policy(digest)).to_payload()
    return await owner._result_thread(build_routing, ref.id, history, records)
