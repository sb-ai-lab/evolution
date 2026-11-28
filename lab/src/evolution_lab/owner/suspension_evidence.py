"""Closed suspension shapes; no journal IO or reason/prefix eligibility policy."""
from __future__ import annotations

from collections.abc import Mapping


class UnsupportedSuspensionShape(ValueError):
    """Core supplied a structural form this Lab producing pair cannot expose."""


_COMMON = {'seq', 'reason', 'owner', 'evidence_kind', 'evidence'}
_ROW_IDS = {'measurement_id', 'case_id', 'sample_id', 'retry_ordinal'}
_ROW_METADATA = {'count', 'rows_digest', 'truncated'}
_EVIDENCE = {
    'fault_rows': _ROW_METADATA,
    'final_rows': _ROW_METADATA,
    'champion_rows': _ROW_METADATA | {'measurement_id'},
    'threshold': {'crossing', 'window_start'},
    'stage_record': {'seq', 'operation_id'},
}
_CROSSING = {'owner', 'kind', 'count', 'denominator', 'max_count', 'max_rate'}


def _require(condition):
    if not condition:
        raise UnsupportedSuspensionShape('unsupported_schema')


def _keys(value, required, optional=frozenset()):
    _require(isinstance(value, Mapping))
    _require(required <= set(value) <= required | optional)


def _text(value, *, nullable=False):
    _require((nullable and value is None) or (isinstance(value, str) and bool(value)))


def _integer(value, *, nullable=False):
    _require((nullable and value is None) or type(value) is int)


def _entry(value):
    _keys(value, _COMMON | {'rows'})
    _integer(value['seq'])
    for name in ('reason', 'owner', 'evidence_kind'):
        _text(value[name])
    kind = value['evidence_kind']
    _require(kind in _EVIDENCE)
    evidence, rows = value['evidence'], value['rows']
    _keys(evidence, _EVIDENCE[kind], {'prefix'} if kind == 'stage_record' else frozenset())
    metadata = dict(evidence)
    if kind in {'fault_rows', 'final_rows', 'champion_rows'}:
        _integer(evidence['count'])
        _text(evidence['rows_digest'])
        _require(type(evidence['truncated']) is bool)
        if kind == 'champion_rows':
            _text(evidence['measurement_id'])
        _require(isinstance(rows, (list, tuple)))
        projected_rows = []
        for row in rows:
            _keys(row, _ROW_IDS | ({'reason', 'code'} if kind == 'fault_rows' else {'owner'}))
            for name in ('measurement_id', 'case_id', 'sample_id'):
                _text(row[name])
            _integer(row['retry_ordinal'])
            if kind == 'fault_rows':
                _text(row['reason'])
                _text(row['code'], nullable=True)
            else:
                _text(row['owner'])
            projected_rows.append(dict(row))
    else:
        _require(rows is None)
        projected_rows = None
        if kind == 'threshold':
            crossing = evidence['crossing']
            _keys(crossing, _CROSSING)
            for name in ('owner', 'kind'):
                _text(crossing[name])
            for name in ('count', 'denominator'):
                _integer(crossing[name])
            _integer(crossing['max_count'])
            _text(crossing['max_rate'])
            _integer(evidence['window_start'], nullable=True)
            metadata['crossing'] = dict(crossing)
        else:
            _integer(evidence['seq'])
            _text(evidence['operation_id'], nullable=True)
            if 'prefix' in evidence:
                _text(evidence['prefix'])
    return {**{name: value[name] for name in ('seq', 'reason', 'owner', 'evidence_kind')},
            'evidence': metadata, 'rows': projected_rows}


def validate_suspensions(values):
    """Copy a closed public history, refusing the entire value on a bad entry."""
    _require(isinstance(values, (list, tuple)))
    return [_entry(value) for value in values]


def project_suspensions(entries):
    """Split rows/kind from validated Core evidence, keeping Core history order.

    ``version`` is Core's internal producing-version marker, not a public field.
    The reducer already admitted reason, owner, prefix and row semantics.
    """
    _require(isinstance(entries, (list, tuple)))
    projected = []
    for entry in entries:
        _keys(entry, _COMMON, {'version'})
        kind, evidence = entry['evidence_kind'], entry['evidence']
        _require(isinstance(evidence, Mapping))
        _require(evidence.get('evidence_kind') == kind)
        projected.append(_entry({
            **{name: entry[name] for name in ('seq', 'reason', 'owner', 'evidence_kind')},
            'evidence': {k: v for k, v in evidence.items() if k not in {'evidence_kind', 'rows'}},
            'rows': evidence.get('rows'),
        }))
        # Non-row evidence cannot silently discard an unexpected rows field.
        _require('rows' in evidence if kind in {'fault_rows', 'final_rows', 'champion_rows'}
                 else 'rows' not in evidence)
    return projected
