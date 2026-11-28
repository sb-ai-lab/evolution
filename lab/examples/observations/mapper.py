"""Neutral example mapping explicitly selected attributes to an execution."""
from evolution_lab.observations import MappedExecution, MappingResult


class ExampleMapper:
    mapper_id = 'neutral-sort-example'
    mapper_version = '1'

    def map_trace(self, trace):
        root = next((span for span in trace.spans if span['name'] == 'request'), None)
        if root is None:
            return MappingResult()
        attrs = root['attributes']
        return MappingResult(executions=[MappedExecution(
            invocation_key=root['span_id'], identity_basis=[root['ref']],
            source_refs=[span['ref'] for span in trace.spans],
            input=attrs.get('example.input'), output=attrs.get('example.output'),
            context=[], steps=[{'kind':'tool','source_refs':[span['ref']],
                               'payload':span['attributes']}
                              for span in trace.spans if span['name']=='sort'],
        )])
