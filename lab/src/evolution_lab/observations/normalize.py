"""Normalize JSON representations without importing an agent or AEF SDK."""
from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Any

from .models import ObservationError, RetentionPolicy

RESERVED = frozenset({'status_message', 'events', 'links', 'resource_attributes', 'scope_attributes'})


def identifier(value, *, otlp=False):
    if value in ('', None): return ''
    if not isinstance(value, str): raise ValueError('invalid identifier')
    # Standard OTLP JSON uses hex; legacy protobuf JSON dumps use base64.
    if re.fullmatch(r'[0-9a-fA-F]+', value) and len(value) % 2 == 0:
        return value.lower()
    if otlp:
        return base64.b64decode(value, validate=True).hex()
    return value


def timestamp(value, *, otlp=False):
    if value is None: return None
    if otlp:
        ns = int(value)
        # Canonical value strips only redundant zero precision, no rounding.
        seconds, fraction = divmod(ns, 1_000_000_000)
        return {'seconds': seconds, 'fraction': f'{fraction:09d}'.rstrip('0')}
    if not isinstance(value, str): raise ValueError('invalid timestamp')
    match = re.fullmatch(r'(.*?)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value)
    if not match: raise ValueError('timezone required')
    stem, fraction, zone = match.groups()
    date = datetime.fromisoformat(stem + ('+00:00' if zone == 'Z' else zone))
    return {'seconds': int(date.astimezone(timezone.utc).timestamp()),
            'fraction': (fraction or '').rstrip('0')}


def attribute_value(value):
    if not isinstance(value, dict) or len(value) != 1: raise ValueError('bad attribute')
    kind, payload = next(iter(value.items()))
    if kind == 'stringValue' and isinstance(payload, str): return payload
    if kind == 'boolValue' and isinstance(payload, bool): return payload
    if kind == 'intValue' and not isinstance(payload, bool): return int(payload)
    if kind == 'doubleValue' and not isinstance(payload, bool): return float(payload)
    if kind == 'arrayValue': return [attribute_value(v) for v in payload.get('values', [])]
    if kind == 'kvlistValue': return attributes(payload.get('values', []))
    if kind == 'bytesValue': return {'bytesValue': payload}
    raise ValueError('bad attribute')


def attributes(items):
    if not isinstance(items, list): raise ValueError('bad attributes')
    result = {}
    for item in items:
        key = item['key']
        if not isinstance(key, str) or key in result: raise ValueError('duplicate attribute')
        result[key] = attribute_value(item['value'])
    return result


def spans_from_payload(payload, requested='auto'):
    if requested not in ('auto', 'aef-flat', 'otlp-json'):
        raise ObservationError('unsupported_format')
    if isinstance(payload, list) and requested in ('auto', 'aef-flat'):
        return [(s, False, {}, {}) for s in payload]
    if isinstance(payload, dict) and isinstance(payload.get('resourceSpans'), list) and requested in ('auto', 'otlp-json'):
        result = []
        for resource in payload['resourceSpans']:
            resource_attrs = attributes(resource.get('resource', {}).get('attributes', []))
            for scope in resource.get('scopeSpans', []):
                scope_attrs = attributes(scope.get('scope', {}).get('attributes', []))
                result.extend((s, True, resource_attrs, scope_attrs) for s in scope.get('spans', []))
        return result
    raise ObservationError('unsupported_format')


def normalize_span(raw, is_otlp, resource, scope, policy: RetentionPolicy, source_ref):
    if not isinstance(raw, dict): raise ValueError('invalid span')
    attrs = attributes(raw.get('attributes', [])) if is_otlp else raw.get('attributes', {})
    if not isinstance(attrs, dict): raise ValueError('invalid attributes')
    trace_id = identifier(raw.get('traceId' if is_otlp else 'trace_id'), otlp=is_otlp)
    span_id = identifier(raw.get('spanId' if is_otlp else 'span_id'), otlp=is_otlp)
    if not trace_id or not span_id: raise ValueError('missing identity')
    kind = attrs.get('aef.kind', '') if is_otlp else raw.get('kind', attrs.get('aef.kind', ''))
    status = raw.get('status', {})
    if not isinstance(status, dict): status = {'code': status}
    extra = {'status_message': status.get('message'), 'events': raw.get('events'),
             'links': raw.get('links'), 'resource_attributes': resource or None,
             'scope_attributes': scope or None}
    result = {
        'trace_id': trace_id, 'span_id': span_id,
        'parent_id': identifier(raw.get('parentSpanId' if is_otlp else 'parent_id'), otlp=is_otlp),
        'name': raw.get('name', ''), 'kind': str(kind).upper(),
        'start_time': timestamp(raw.get('startTimeUnixNano' if is_otlp else 'start_time'), otlp=is_otlp),
        'end_time': timestamp(raw.get('endTimeUnixNano' if is_otlp else 'end_time'), otlp=is_otlp),
        'status_code': status.get('code'),
        'attributes': {k: v for k, v in attrs.items() if k in policy.attribute_keys and k not in RESERVED},
        'payload': {k: v for k, v in extra.items() if k in policy.attribute_keys and v is not None},
        'source_refs': [source_ref],
    }
    reduced = any(k not in policy.attribute_keys and k != 'aef.kind' for k in attrs)
    reduced |= any(v is not None and k not in policy.attribute_keys for k, v in extra.items())
    return result, reduced


def compare(a: Any, b: Any):
    """Equal / missing-only / ambiguous representation / conflicting values."""
    if type(a) is not type(b):
        return 'missing' if a is None or b is None else 'uncertain'
    if isinstance(a, dict):
        states = [compare(a[k], b[k]) if k in a and k in b else 'missing' for k in a.keys() | b.keys()]
    elif isinstance(a, list):
        if len(a) != len(b): return 'conflict'
        states = [compare(x, y) for x, y in zip(a, b)]
    else:
        return 'equal' if a == b else 'conflict'
    for state in ('conflict', 'uncertain', 'missing'):
        if state in states: return state
    return 'equal'


def comparison(a, b):
    ignored = {'ref', 'source_refs', 'diagnostics', 'group_id', 'variant'}
    left, right = ({k: v for k, v in row.items() if k not in ignored} for row in (a, b))
    uncertain_time = False
    for key in ('start_time', 'end_time'):
        x, y = left.get(key), right.get(key)
        if x and y and x != y and x['seconds'] == y['seconds']:
            fx, fy = x['fraction'], y['fraction']
            if fx.startswith(fy) or fy.startswith(fx):
                uncertain_time = True
                left[key] = right[key] = None
    state = compare(left, right)
    return 'uncertain' if uncertain_time and state != 'conflict' else state


def merge_missing(a, b):
    if a is None: return b
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merge_missing(a.get(k), b.get(k)) for k in a.keys() | b.keys()}
    return a
