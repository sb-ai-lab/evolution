"""Inventory only: no scoring, repeats, core admission or raw-source rereads."""
from .basket import execution_index
from .models import RecordedEvaluationPlan
from .storage import clean_json, revision, write_plan


def _overlap(a,b):
    ia,ib=a['identity'],b['identity']
    pa,pb=ia['producer'],ib['producer']
    if pa['scope']==pb['scope']=='stable' and pa['namespace']!=pb['namespace']: return None
    if a['trace_id']!=b['trace_id']: return None
    if (ia['status']==ib['status']=='known' and a['mapper']==b['mapper']
        and ia['invocation_key']==ib['invocation_key']): return 'same_stable_key'
    return 'same_trace_id'


def prepare_recorded_evaluation(basket, observations, output_directory):
    index=execution_index(observations);rows=[]
    for case in basket.cases:
        execution=index.get(case['source_execution_ref'])
        row={'case_ref':case['ref'],'source_execution_ref':case['source_execution_ref'],
             'record_available':execution is not None,
             'parts':{k:'known' if execution is not None and execution.get(k) is not None else 'unknown'
                      for k in ('input','context','output','steps')},
             'input_modified':case['input_modified'],
             'identity_status':execution['identity']['status'] if execution else 'unknown',
             'known_conditions':execution['version_facts'] if execution else [],
             'possible_overlap_with':[], 'diagnostics':list(execution['diagnostics']) if execution else [{'code':'execution_unavailable'}]}
        if case['input_modified']:row['diagnostics'].append({'code':'input_modified'})
        if execution:
            for other in basket.cases:
                if other['ref']==case['ref']:continue
                target=index.get(other['source_execution_ref'])
                basis=_overlap(execution,target) if target else None
                if basis:row['possible_overlap_with'].append({'case_ref':other['ref'],'basis':basis})
        rows.append(row)
    data=clean_json({'schema':'lab-recorded-evaluation-plan/1','revision':revision(),
                    'basket_revision':basket.manifest['revision'],'cases':rows,
                    'core_questions':['historical_baseline_admission','measurement_identity','scorer_requirements']})
    return RecordedEvaluationPlan(write_plan(output_directory,data),data)
