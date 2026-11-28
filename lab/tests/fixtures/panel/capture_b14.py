"""Genuine installed offline b14 campaigns for the probe availability contract.

The Core-required agent_cli model token selects its protocol only. The neutral
executor below replaces _call and runs only Core's sealed probe helper through
the current Python; no agent CLI, provider, credentials or paid model is used.
Campaign admission, role projection, helper installation, health checks,
bookkeeping, journaling, reduction and reports use the installed exact wheels.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import socket
import sys
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'tests'))
from b10_campaigns import EDITOR, SCORER, NeutralTarget
from scaffold_16 import scaffold_16

SCENARIOS = ('agent-finished', 'agent-failed', 'subprocess-finished',
             'subprocess-failed', 'agent-unreachable')
REQUEST = {'method': 'GET', 'path': '/neutral', 'body': None}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + '\n')


async def capture(work, name):
    from evolution.tree.agent_reflector import AgentCliReflector, CONTROL_PREFIX, REFLECTION_FILE
    from evolution.tree import campaign as campaign_module
    from evolution.tree.evolve import evolve
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.family15_report import report_lines
    from evolution_lab.owner import core_raw
    from evolution_lab.report import read_campaign_report

    agent = name.startswith('agent-')
    failed = name.endswith('-failed')
    mediator_failed = name != 'agent-unreachable'
    sample = scaffold_16(work / 'input', mode='point', repeats=1)
    inputs = sample.manifest.parent
    document = json.loads(sample.manifest.read_text())
    for role, program, member in (('scorer', SCORER, 'score.py'), ('editor', EDITOR, 'edit.py')):
        (inputs / role / member).write_text(program)
        document[role]['argv'] = [sys.executable, member]
    document['scorer']['metric_contract'] = {
        'version': 'metric_contract/1', 'kind': 'pointwise', 'target_fields': []}
    rows = [{'case_id': case, 'group_id': 'neutral', 'stratum': 'neutral',
             'public': {'text': 'hello', 'case_id': case}, 'oracle': {'critical': True},
             'reflection_metadata': {'uses': []}} for case in ('c1', 'c2')]
    (inputs / 'corpus.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    document['campaign'].update(rounds=1, attempts_per_round=1, max_concurrency=1,
        baseline_refresh_after_rounds=0, random_seed=7)
    helper_answers = []
    with socket.socket() as origin, ExitStack() as stack:
        # Bind without listen: the address stays reserved and every real connect refuses.
        origin.bind(('127.0.0.1', 0))
        port = origin.getsockname()[1]
        keep = {key: document['reflector'][key] for key in
                ('selection', 'families', 'max_consecutive_failures')}
        probe = {'base_url': f'http://127.0.0.1:{port}', 'on_unavailable': 'continue',
                 'budget': 1, 'request_timeout_seconds': .2}
        if agent:
            document['reflector'] = {**keep, 'kind': 'agent_cli', 'model': 'codex',
                'agent_bin': sys.executable, 'environment_docs': None, 'probe': probe}

            async def neutral_call(self, request, root, log, started, *, call, prompt):
                helper = root / CONTROL_PREFIX / 'probe/probe_request.py'
                for _ in range(2):
                    process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-I',
                        str(helper), json.dumps(REQUEST), stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await process.communicate()
                    assert process.returncode == 0, stderr
                    answer = json.loads(stdout)
                    assert answer['error'] == 'unavailable', answer
                    helper_answers.append(answer)
                if failed:
                    raise RuntimeError('neutral offline executor failure after helper calls')
                dump(root / CONTROL_PREFIX / REFLECTION_FILE,
                     {'status': 'declined', 'groups': [], 'note': 'neutral offline completion'})
                return 1

            stack.enter_context(patch.object(AgentCliReflector, '_call', neutral_call))
        else:
            document['reflector']['probe'] = probe
            document['reflector']['argv'] = [sys.executable, 'reflect.py']
            program = ('import json,sys\np=json.load(sys.stdin)\n'
                       'assert not (p.get("probe") or {}).get("url")\n' +
                       ('raise RuntimeError("neutral offline role failure")\n' if failed else
                        'print(json.dumps({"status":"declined","groups":[],"note":"neutral offline completion"}))\n'))
            (inputs / 'reflector/reflect.py').write_text(program)
        sample.manifest.write_text(json.dumps(document, indent=2) + '\n')
        if mediator_failed:
            async def cannot_listen(self):
                raise OSError('neutral injected mediator startup failure')
            stack.enter_context(patch.object(campaign_module.ProbeMediator, 'start', cannot_listen))
        target = NeutralTarget()
        notes = []
        run = work / 'run'
        result = await evolve(sample.manifest, run, adapter_factory=lambda *_a, **_k: target,
                              notes=notes.append)
        assert not target.live
        projection = inspect_campaign(run, verify=True).to_payload()
        assert projection['verification']['mismatches'] == [], projection['verification']
        records = [json.loads(line) for line in (run / 'campaign.jsonl').read_bytes().splitlines()]
        probes = [row['payload'] for row in records if row['event'] == 'probe_completed']
        terminal = 'reflection_failed' if failed else 'reflection_finished'
        terminals = [row['payload'] for row in records if row['event'] == terminal]
        assert len(terminals) == 1, (name, [row['event'] for row in records], result)
        availability = terminals[0]['probe_availability']
        assert availability == {'health': 'unavailable',
            'mediator': 'failed' if mediator_failed else 'started',
            'unreachable': 2 if agent else 0, 'transport_failures': 0}, availability
        assert [row['seq'] for row in probes] == ([1, 2] if agent else [])
        assert all(row['error'] == 'unreachable' for row in probes)
        assert projection['independent']['probe_availability'] == [{'round': 1, **availability}]
        dump(work / 'core-projection.json', projection)
        dump(work / 'helper-answers.json', helper_answers)
        (work / 'core-report.txt').write_text('\n'.join(report_lines(projection['independent'])) + '\n')
        (work / 'worker-notes.txt').write_text('\n'.join(notes) + '\n')
        report = read_campaign_report(run, families=core_raw.FAMILIES)
        (work / 'lab-report.md').write_text(report.markdown())
        dump(work / 'result.json', {'terminal': result.terminal, 'owner': result.owner, 'code': result.code,
                                  'event': terminal, 'availability': availability})
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-wheel', type=Path, required=True)
    parser.add_argument('--core-wheel', type=Path, required=True)
    parser.add_argument('--retained', type=Path, required=True)
    args = parser.parse_args()
    import evolution_lab
    import evolution.tree
    assert version('evo-skills') == '2.0.0b14' and version('evolution-lab') == '0.0.3.dev62'
    assert 'site-packages' in evolution_lab.__file__ and 'site-packages' in evolution.tree.__file__
    destination = Path(__file__).resolve().parent / 'b14'
    if destination.exists():
        raise RuntimeError(f'preserve original captures: {destination}')
    retained = args.retained / ('probe-' + uuid.uuid4().hex)
    retained.mkdir(parents=True, mode=0o700)
    wheels = retained / 'producing-wheels'
    wheels.mkdir()
    provenance = {}
    for label, path in (('lab', args.lab_wheel), ('core', args.core_wheel)):
        shutil.copyfile(path, wheels / path.name)
        provenance[label] = {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    helpers = [Path(__file__), REPO / 'tests/b10_campaigns.py', REPO / 'tests/scaffold_16.py']
    generators = [{'path': str(path.relative_to(REPO)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in helpers]
    for path in helpers:
        shutil.copyfile(path, retained / path.name)
    for name in SCENARIOS:
        work = retained / name
        work.mkdir()
        run = asyncio.run(capture(work, name))
        exported = destination / name
        exported.mkdir(parents=True)
        # Export selected neutral evidence only; retain all original runs outside the repository.
        for pattern in ('campaign.jsonl', 'measurements/*/events.jsonl', 'scorer-traces/*.pack',
                        'reflector-logs/*.jsonl', 'reflector-logs/*.stderr',
                        'reflector-logs/probes/*/startup.json'):
            for source in run.glob(pattern):
                if not source.is_file() or source.is_symlink():
                    continue
                target = exported / 'run' / source.relative_to(run)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        for member in ('core-projection.json', 'helper-answers.json', 'core-report.txt',
                       'worker-notes.txt', 'lab-report.md', 'result.json'):
            shutil.copyfile(work / member, exported / member)
        files = {str(path.relative_to(exported)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(exported.rglob('*')) if path.is_file()}
        dump(exported / 'meta.json', {'schema': 'lab-offline-core-capture/1',
            'scenario': name, 'capture_id': retained.name,
            'captured_at': datetime.datetime.now(datetime.UTC).isoformat(),
            'core_version': version('evo-skills'), 'lab_version': version('evolution-lab'),
            'producing_wheels': provenance, 'generators': generators,
            'core_functions': ['evolution.tree.evolve.evolve', 'evolution.tree.campaign.inspect_campaign'],
            'fault_injections': ['ProbeMediator.start raises OSError'] if name != 'agent-unreachable' else [],
            'executor': 'neutral _call replacement using real sealed helper' if name.startswith('agent-') else 'neutral subprocess',
            'files_sha256': files})
        print(f'captured {name}: {work}', flush=True)
    dump(retained / 'receipt.json', {'completed': True, 'scenarios': SCENARIOS,
        'producing_wheels': provenance, 'generators': generators})


if __name__ == '__main__':
    main()
