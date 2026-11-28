"""Explicit case selection; historical output never enters the target payload."""
from pathlib import Path
from .models import Basket, ObservationError, Selection
from .storage import clean_json, revision, write_artifact


def execution_index(observations):
    index={}
    for obs in observations:
        for execution in obs.executions:
            ref=execution['ref']
            if ref in index and index[ref]!=execution: raise ObservationError('selection_unresolved')
            index[ref]=execution
    return index


def build_basket(observations, selections: list[Selection], output_directory: Path,
                 *, purpose='unspecified', declared_usage=None):
    if purpose not in ('development','evaluation','unspecified'): raise ObservationError('selection_unresolved')
    observations=list(observations)
    index=execution_index(observations)
    rev=revision();cases=[]
    for selection in selections:
        source=index.get(selection.execution_ref)
        if source is None or set(selection.replacements)-{'input','context'}:
            raise ObservationError('selection_unresolved')
        input_value=selection.replacements.get('input',source['input'])
        context=selection.replacements.get('context',source['context'])
        modified=clean_json([input_value,context])!=clean_json([source['input'],source['context']])
        cases.append({'ref':f'{rev}/cases/case-{len(cases)+1}',
                      'input':clean_json(input_value),'context':clean_json(context),
                      'source_execution_ref':source['ref'],'input_modified':modified,
                      'replacements':clean_json(selection.replacements),
                      'group_ref':selection.group_ref,'criteria_ref':selection.criteria_ref,
                      'world_ref':selection.world_ref})
    manifest={'schema':'lab-basket/1','revision':rev,'purpose':purpose,
              'observation_revisions':[o.manifest['revision'] for o in observations],
              'declared_usage':None if declared_usage is None else {'origin':'declared','value':clean_json(declared_usage)},
              'diagnostics':[],'partial':False}
    records={'cases.jsonl':cases};path=Path(output_directory)
    return Basket(path,write_artifact(path,manifest,records),records)
