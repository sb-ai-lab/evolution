"""Hand-authored neutral evidence for the approved V1–V4 scenarios."""
import base64
import json

TRACE = '11' * 16
ROOT = '22' * 8
CHILD = '33' * 8


def flat(span_id=ROOT, name='request', **attrs):
    return {'trace_id': TRACE, 'span_id': span_id,
            'parent_id': '' if span_id == ROOT else ROOT, 'name': name,
            'kind': 'INTERNAL', 'start_time': '2026-09-12T00:00:00Z',
            'attributes': attrs}


def otlp(span):
    def ident(value):
        return base64.b64encode(bytes.fromhex(value)).decode()
    attrs = [{'key': key, 'value': {'stringValue': value}}
             for key, value in span['attributes'].items()]
    attrs.append({'key': 'aef.kind', 'value': {'stringValue': span['kind'].lower()}})
    return {'resourceSpans': [{'scopeSpans': [{'spans': [{
        'traceId': ident(span['trace_id']), 'spanId': ident(span['span_id']),
        'parentSpanId': ident(span['parent_id']), 'name': span['name'],
        'startTimeUnixNano': '1789171200000000000', 'attributes': attrs,
    }]}]}]}


def source_file(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


class NeutralMapper:
    mapper_id = 'neutral'
    mapper_version = '1'

    def map_trace(self, trace):
        from evolution_lab.observations import MappedExecution, MappingResult
        root = next((s for s in trace.spans if s['name'] == 'request'), None)
        if root is None:
            return MappingResult()
        attrs = root['attributes']
        return MappingResult(executions=[MappedExecution(
            invocation_key=root['span_id'], identity_basis=[root['ref']],
            source_refs=[s['ref'] for s in trace.spans],
            input=attrs.get('question'), output=attrs.get('answer'),
            context=None,
            steps=[{'kind': 'tool', 'source_refs': [s['ref']], 'payload': s['attributes']}
                   for s in trace.spans if s['name'] == 'tool'],
        )])


def make_import(tmp_path, *, namespace='producer', mapper=None, attrs=None):
    from evolution_lab.observations import ImportGroup, RetentionPolicy, Source, import_traces
    attrs = attrs or {'question': 'Sort: 3, 1, 2', 'answer': '1, 2, 3'}
    path = source_file(tmp_path / 'source.json', [flat(**attrs)])
    group = ImportGroup(sources=[Source(path)], namespace=namespace,
                        mapper=mapper or NeutralMapper(),
                        retention=RetentionPolicy('neutral', '1', attribute_keys=tuple(attrs)))
    return import_traces([group], tmp_path / 'observations')
