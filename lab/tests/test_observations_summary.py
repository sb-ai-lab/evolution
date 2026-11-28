"""V4: public summaries reveal counts and generated refs, never payload text."""
import json
from observations_support import NeutralMapper, make_import


def test_payload_and_source_name_do_not_leak_into_summary(tmp_path):
    secret='PRIVATE-MARKER'
    result = make_import(tmp_path,attrs={'question':secret,'answer':secret,'credential':secret})
    summary = json.dumps(result.public())
    assert secret not in summary
    assert str(tmp_path) not in summary
    assert result.public()['counts']['executions'] == 1


def test_mapper_exception_text_is_not_public(tmp_path):
    class Broken(NeutralMapper):
        def map_trace(self, trace): raise ValueError('PRIVATE-MARKER')
    result=make_import(tmp_path,mapper=Broken())
    assert result.executions == []
    assert 'PRIVATE-MARKER' not in json.dumps(result.public())
    assert result.public()['diagnostic_counts']['mapping_failed'] == 1


def test_reduction_count_and_case_diagnostics_are_trace_local(tmp_path):
    from evolution_lab.observations import ImportGroup, Source, RetentionPolicy, import_traces, Selection, build_basket, prepare_recorded_evaluation
    from observations_support import flat,source_file
    a=flat(question='one',answer='a',omitted='PRIVATE-MARKER')
    b=flat(question='two',answer='b');b['trace_id']='44'*16
    path=source_file(tmp_path/'input.json',[a,b])
    obs=import_traces([ImportGroup(sources=[Source(path)],mapper=NeutralMapper(),
         retention=RetentionPolicy('p','1',attribute_keys=('question','answer')))],tmp_path/'obs')
    assert obs.public()['diagnostic_counts']['payload_reduced']==1
    by_input={row['input']:row for row in obs.executions}
    assert 'payload_reduced' in {d['code'] for d in by_input['one']['diagnostics']}
    assert 'payload_reduced' not in {d['code'] for d in by_input['two']['diagnostics']}
    basket=build_basket([obs],[Selection(by_input['two']['ref'])],tmp_path/'basket')
    row=prepare_recorded_evaluation(basket,[obs],tmp_path/'plan').data['cases'][0]
    assert 'payload_reduced' not in {d['code'] for d in row['diagnostics']}


def test_mapper_facts_require_origin_and_trace_bound_source(tmp_path):
    from evolution_lab.observations import ImportGroup, Source, RetentionPolicy, import_traces
    from observations_support import flat,source_file
    class Facts(NeutralMapper):
        def __init__(self,mode):self.mode=mode
        def map_trace(self,trace):
            result=super().map_trace(trace)
            fact={'name':'version','value':'1'}
            if self.mode!='missing':fact['origin']='recorded'
            if self.mode=='foreign':fact['source_ref']='source-999'
            if self.mode=='valid':fact['source_ref']=trace.spans[0]['ref']
            result.executions[0].version_facts=[fact]
            return result
    raw=source_file(tmp_path/'input.json',[flat(question='q',answer='a')])
    for mode in ('missing','foreign','valid'):
        obs=import_traces([ImportGroup(sources=[Source(raw)],mapper=Facts(mode),
            retention=RetentionPolicy('p','1',attribute_keys=('question','answer')))],tmp_path/mode)
        assert len(obs.executions)==(1 if mode=='valid' else 0)
        if mode!='valid':assert obs.public()['diagnostic_counts']['mapping_failed']==1
