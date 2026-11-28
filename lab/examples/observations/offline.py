"""Manually invoked, no agents, scorers, network or core campaigns."""
import argparse
import json
from pathlib import Path

from evolution_lab.observations import (
    ImportGroup, RetentionPolicy, Selection, Source,
    import_traces, build_basket, prepare_recorded_evaluation,
)
from mapper import ExampleMapper


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    # One new directory; existing results are never reused or overwritten.
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    data=Path(__file__).parent/'data'
    observations=import_traces([ImportGroup(
        sources=[Source(data/'manager.json'), Source(data/'otlp.json')],
        namespace='neutral-example', mapper=ExampleMapper(),
        retention=RetentionPolicy('neutral-example','1',
            attribute_keys=('example.input','example.output','example.result')),
    )],args.output/'observations')
    basket=build_basket([observations],
        [Selection(execution['ref'],criteria_ref='example:ascending-order')
         for execution in observations.executions],args.output/'basket')
    plan=prepare_recorded_evaluation(basket,[observations],args.output/'recorded-plan')
    print(json.dumps({'observations':observations.public(),'basket':basket.public(),
                      'recorded_plan':plan.public()},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
