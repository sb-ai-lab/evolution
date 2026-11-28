"""V1: batch boundaries and duplicate copies must not change observed steps."""
from observations_support import CHILD, NeutralMapper, flat, otlp, source_file


def test_split_batches_and_cross_format_duplicate_keep_one_tool(tmp_path):
    from evolution_lab.observations import ImportGroup, RetentionPolicy, Source, import_traces
    root = flat(question='Sort: 3, 1, 2', answer='1, 2, 3')
    child = flat(CHILD, 'tool', result='sorted')
    files = [source_file(tmp_path / 'root.json', [root]),
             source_file(tmp_path / 'child.json', [child]),
             source_file(tmp_path / 'copy.json', otlp(child))]
    result = import_traces([ImportGroup(
        sources=[Source(p) for p in files], mapper=NeutralMapper(),
        retention=RetentionPolicy('neutral', '1', attribute_keys=('question','answer','result')),
    )], tmp_path / 'out')
    assert len(result.executions) == 1
    assert len(result.executions[0]['steps']) == 1
    assert len(result.spans) == 2
    assert result.executions[0]['identity']['status'] == 'unknown'


def test_conflicting_span_is_preserved_and_not_mapped(tmp_path):
    from evolution_lab.observations import ImportGroup, RetentionPolicy, Source, import_traces
    a = source_file(tmp_path / 'a.json', [flat(question='q', answer='a')])
    b = source_file(tmp_path / 'b.json', [flat(question='q', answer='different')])
    result = import_traces([ImportGroup(sources=[Source(a), Source(b)],
        mapper=NeutralMapper(), retention=RetentionPolicy('neutral','1',attribute_keys=('question','answer')))], tmp_path/'out')
    assert result.executions == []
    assert len(result.spans) == 2
    assert 'span_conflict' in {d['code'] for d in result.manifest['diagnostics']}


def test_isolated_groups_report_possible_fragmentation_on_execution(tmp_path):
    from evolution_lab.observations import ImportGroup, RetentionPolicy, Source, import_traces
    a = source_file(tmp_path/'a.json', [flat(question='q',answer='a')])
    b = source_file(tmp_path/'b.json', [flat(CHILD,'tool', result='r')])
    policy = RetentionPolicy('neutral','1',attribute_keys=('question','answer','result'))
    result = import_traces([ImportGroup(sources=[Source(p)], namespace='same',
        retention=policy, mapper=NeutralMapper()) for p in (a,b)],tmp_path/'out')
    assert len(result.executions) == 1
    assert 'possible_trace_fragmentation' in {d['code'] for d in result.executions[0]['diagnostics']}


def test_structural_import_keeps_trace_diagnostics_without_execution(tmp_path):
    from evolution_lab.observations import ImportGroup, Source, RetentionPolicy, import_traces
    a=flat(question='q')
    b=flat(question='q',answer='a')
    first=source_file(tmp_path/'first.json',[a,b])
    second=source_file(tmp_path/'second.json',[flat(CHILD,'tool')])
    result=import_traces([ImportGroup(sources=[Source(p)],retention=RetentionPolicy('p','1',attribute_keys=('question','answer')))
                          for p in (first,second)],tmp_path/'out')
    assert result.executions == []
    ds=result.manifest['diagnostics']
    assert 'span_incomplete' in {d['code'] for d in ds}
    fragments=[d for d in ds if d['code']=='possible_trace_fragmentation']
    assert len(fragments)==2
    assert all(d['trace_ref'] and d['source_refs'] for d in fragments)


def test_mapper_empty_and_failure_identify_their_trace(tmp_path):
    from evolution_lab.observations import ImportGroup, Source, MappingResult, import_traces
    a=flat();b=flat();b['trace_id']='44'*16
    class Mapper(NeutralMapper):
        def map_trace(self,trace):
            if trace.trace_id=='44'*16:raise ValueError('private error')
            return MappingResult()
    path=source_file(tmp_path/'input.json',[a,b])
    result=import_traces([ImportGroup(sources=[Source(path)],mapper=Mapper())],tmp_path/'out')
    ds=[d for d in result.manifest['diagnostics'] if d['code'] in ('mapping_empty','mapping_failed')]
    assert len(ds)==2 and len({d['trace_ref'] for d in ds})==2
    assert all(d['source_refs'] for d in ds)
