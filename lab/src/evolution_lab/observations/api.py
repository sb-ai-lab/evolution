"""Explicit local import. No core journal writes, model calls or auto replay."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from .models import (ImportGroup, MappingResult, MappedExecution, ObservationError,
                     ObservationSet, TraceEnvelope)
from .normalize import spans_from_payload, normalize_span, comparison, merge_missing, identifier
from .storage import clean_json, revision, write_artifact


def diagnostic(code, refs=(), *, trace_ref=None):
    return {'code': code, 'source_refs': list(refs), 'trace_ref': trace_ref}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key] = value
    return result


def _invalid_constant(value): raise ValueError('nonfinite')


def import_traces(groups: list[ImportGroup], output_directory: Path) -> ObservationSet:
    rev = revision()
    sources, group_info, all_spans, executions, diagnostics = [], [], [], [], []
    partial = False
    # Each entry is an isolated trace envelope with a group-scoped producer.
    envelopes = []
    for gi, group in enumerate(groups, 1):
        gid = f'group-{gi}'
        if not isinstance(group, ImportGroup) or group.namespace_origin not in ('recorded', 'declared'):
            raise ObservationError('mapping_failed')
        if group.namespace is not None and (not isinstance(group.namespace, str) or not group.namespace):
            raise ObservationError('mapping_failed')
        if group.namespace_origin == 'recorded' and group.namespace is not None and not group.namespace_source_ref:
            raise ObservationError('mapping_failed')
        policy = group.retention
        producer = {'namespace': group.namespace or f'{rev}/{gid}',
                    'scope': 'stable' if group.namespace is not None else 'operation_local',
                    'origin': group.namespace_origin if group.namespace is not None else 'declared',
                    'source_ref': group.namespace_source_ref}
        facts = [{'name': k, 'value': clean_json(v), 'origin': 'declared'}
                 for k,v in group.declared_facts.items()]
        rows, group_diagnostics = [], []
        trace_diagnostics = defaultdict(list)
        for source in group.sources:
            sid = f'source-{len(sources)+1}'
            item = {'id': sid, 'group_id': gid, 'locator': str(source.path),
                    'role': source.role, 'requested_format': source.format, 'sha256': None}
            sources.append(item)
            try:
                content = Path(source.path).read_bytes()
                item['sha256'] = hashlib.sha256(content).hexdigest()
            except OSError:
                group_diagnostics.append(diagnostic('source_unreadable',[sid])); partial=True; continue
            try:
                payload = json.loads(content, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
            except (ValueError, UnicodeError):
                group_diagnostics.append(diagnostic('source_decode_failed',[sid])); partial=True; continue
            if source.role == 'metadata':
                if not isinstance(payload, dict):
                    group_diagnostics.append(diagnostic('source_decode_failed',[sid])); partial=True; continue
                for key,value in payload.items():
                    if key in policy.metadata_keys:
                        facts.append({'name':key,'value':value,'origin':'recorded','source_ref':sid})
                if set(payload) - set(policy.metadata_keys): group_diagnostics.append(diagnostic('payload_reduced',[sid]))
                continue
            if source.role != 'trace':
                group_diagnostics.append(diagnostic('unsupported_format',[sid]));partial=True;continue
            try:
                raw_spans = spans_from_payload(payload, source.format)
            except (ValueError, TypeError, KeyError, AttributeError):
                group_diagnostics.append(diagnostic('unsupported_format',[sid]));partial=True;continue
            item['format'] = 'otlp-json' if isinstance(payload,dict) else 'aef-flat'
            for index, (raw, is_otlp, resource, scope) in enumerate(raw_spans):
                ref = f'{sid}#span-{index}'
                try:
                    row, reduced = normalize_span(raw,is_otlp,resource,scope,policy,ref)
                    if reduced: trace_diagnostics[row['trace_id']].append(diagnostic('payload_reduced',[ref]))
                    rows.append(row)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    issue = diagnostic('span_invalid', [ref])
                    try:
                        tid = identifier(raw.get('traceId' if is_otlp else 'trace_id'), otlp=is_otlp)
                    except (ValueError, TypeError, AttributeError):
                        tid = None
                    if tid:
                        trace_diagnostics[tid].append(issue)
                    else:
                        # Unattributed malformed span remains in the registry, not on other cases.
                        diagnostics.append(issue)
                    partial=True
        by_identity = defaultdict(list)
        for row in rows:
            key = row['trace_id'], row['span_id']
            copies = by_identity[key]
            if not copies:
                copies.append(row);continue
            # Compare all variants; never let the last duplicate erase a conflict.
            states = [comparison(existing,row) for existing in copies]
            if 'conflict' in states or 'uncertain' in states:
                code='span_conflict' if 'conflict' in states else 'representation_uncertain'
                trace_diagnostics[key[0]].append(diagnostic(code,row['source_refs']))
                copies.append(row)
            else:
                old = copies[0]
                refs = old['source_refs'] + row['source_refs']
                merged = merge_missing(old,row)
                old.clear();old.update(merged);old['source_refs']=refs
                if 'missing' in states:
                    trace_diagnostics[key[0]].append(diagnostic('span_incomplete',refs))
        by_trace=defaultdict(list)
        for copies in by_identity.values():
            for row in copies:
                row.update(ref=f'{rev}/spans/span-{len(all_spans)+1}', group_id=gid)
                all_spans.append(row);by_trace[row['trace_id']].append(row)
        mapper_info = None
        if group.mapper is not None:
            try:
                mid, mv = group.mapper.mapper_id, group.mapper.mapper_version
                if not all(isinstance(v,str) and v for v in (mid,mv)): raise ValueError()
                mapper_info={'id':mid,'version':mv}
            except (AttributeError, ValueError):
                raise ObservationError('mapping_failed') from None
        group_info.append({'id':gid,'producer':producer,'retention':asdict(policy),
                           'mapper':mapper_info,'facts':facts})
        diagnostics.extend(group_diagnostics)
        # Keep a trace entry even if all of its spans were malformed.
        for trace_id in dict.fromkeys([*by_trace, *trace_diagnostics]):
            spans = by_trace[trace_id]
            trace_ref = f'{rev}/traces/trace-{len(envelopes)+1}'
            local_diagnostics = trace_diagnostics[trace_id]
            for issue in local_diagnostics:
                issue['trace_ref'] = trace_ref
            diagnostics.extend(local_diagnostics)
            ds = group_diagnostics + local_diagnostics
            envelopes.append((group,mapper_info,TraceEnvelope(trace_id,spans,producer,facts,ds),trace_ref))
    # No merging across groups; identical/unknown producers can indicate fragments.
    for index, (group, mapper_info, env, trace_ref) in enumerate(envelopes):
        ds = list(env.diagnostics)
        trace_sources = list(dict.fromkeys(
            [ref for span in env.spans for ref in span['source_refs']]
            + [ref for issue in ds if issue.get('trace_ref') == trace_ref
               for ref in issue['source_refs']]))
        for other_index, (_,_,other,_) in enumerate(envelopes):
            if index == other_index or env.trace_id != other.trace_id: continue
            known_different = (env.producer['scope'] == other.producer['scope'] == 'stable'
                               and env.producer['namespace'] != other.producer['namespace'])
            if not known_different:
                issue=diagnostic('possible_trace_fragmentation', trace_sources, trace_ref=trace_ref)
                diagnostics.append(issue);ds.append(issue);break
        if any(d['code'] in ('span_conflict','representation_uncertain') for d in ds):
            partial=True;continue
        if group.mapper is None: continue
        try:
            mapped = group.mapper.map_trace(copy.deepcopy(TraceEnvelope(env.trace_id,env.spans,env.producer,env.facts,ds)))
            if not isinstance(mapped,MappingResult): raise ValueError('invalid mapping')
            candidates=[]
            valid_refs={s['ref'] for s in env.spans}
            fact_refs=valid_refs | set(trace_sources) | {
                f['source_ref'] for f in env.facts if f.get('origin')=='recorded' and f.get('source_ref')
            }
            seen_keys=set()
            for value in mapped.executions:
                if not isinstance(value,MappedExecution): raise ValueError('invalid execution')
                row=clean_json(asdict(value))
                refs=row['source_refs'];basis=row['identity_basis'];key=row['invocation_key']
                if not refs or not set(refs)<=valid_refs or not set(basis)<=valid_refs:
                    raise ValueError('invalid source refs')
                if key is not None:
                    if not isinstance(key,str) or not key or not basis or key in seen_keys: raise ValueError('invalid key')
                    seen_keys.add(key)
                if row['steps'] is not None:
                    for step in row['steps']:
                        if not isinstance(step,dict) or not set(step.get('source_refs',[]))<=valid_refs: raise ValueError('step refs')
                for fact in row['version_facts']:
                    if not isinstance(fact,dict) or fact.get('origin') not in ('recorded','declared','derived'):
                        raise ValueError('fact origin')
                    if not isinstance(fact.get('name'),str) or not fact['name'] or 'value' not in fact:
                        raise ValueError('fact shape')
                    anchors=fact.get('source_refs',[])
                    if not isinstance(anchors,list) or not all(isinstance(ref,str) for ref in anchors):
                        raise ValueError('fact refs')
                    anchors=list(anchors)
                    if 'source_ref' in fact:
                        if not isinstance(fact['source_ref'],str):raise ValueError('fact ref')
                        anchors.append(fact['source_ref'])
                    if (anchors and not set(anchors)<=fact_refs) or (fact['origin']!='declared' and not anchors):
                        raise ValueError('fact provenance')
                eid=f'{rev}/executions/execution-{len(executions)+len(candidates)+1}'
                known=env.producer['scope']=='stable' and key is not None and bool(basis)
                row.update(ref=eid, trace_id=env.trace_id, trace_ref=trace_ref, mapper=mapper_info,
                    identity={'status':'known' if known else 'unknown','producer':env.producer,
                              'invocation_key':key,'identity_basis':basis},
                    version_facts=clean_json(env.facts)+row['version_facts'],
                    diagnostics=ds + ([diagnostic('mapper_diagnostic', refs, trace_ref=trace_ref)] if row['diagnostics'] else []))
                candidates.append(row)
            executions.extend(candidates)
            for row in candidates:
                diagnostics.extend(d for d in row['diagnostics'] if d not in ds)
            if mapped.diagnostics: diagnostics.append(diagnostic('mapper_diagnostic',trace_sources,trace_ref=trace_ref))
            if not candidates: diagnostics.append(diagnostic('mapping_empty',trace_sources,trace_ref=trace_ref))
        except Exception:
            diagnostics.append(diagnostic('mapping_failed',trace_sources,trace_ref=trace_ref));partial=True
    # Registry rows have generated identity. Execution/plan diagnostics are linked
    # copies for convenient reading, never counted as additional events.
    for index, issue in enumerate(diagnostics, 1):
        issue['id'] = f'{rev}/diagnostics/diagnostic-{index}'
    manifest={'schema':'lab-observations/1','revision':rev,'sources':sources,
              'groups':group_info,'normalizer_version':'1','diagnostics':diagnostics,'partial':partial}
    records={'spans.jsonl':clean_json(all_spans),'executions.jsonl':clean_json(executions)}
    path=Path(output_directory)
    manifest=write_artifact(path,manifest,records)
    return ObservationSet(path,manifest,records)
