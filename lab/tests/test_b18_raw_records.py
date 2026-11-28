"""b18 raw records (#387 ``components_withheld``) across the pinned Lab readers, from a real offline Core campaign."""
import asyncio
import json
from pathlib import Path
import sys

from evolution_lab.owner import core_raw
from evolution_lab.owner.measurements import measurement_rows, read_journal_facts
from dev62_default_outputs import read_outputs
import pytest

pytestmark = pytest.mark.slow

SECRET = 'lab-b18-component-secret-74'
SECRET_ENV = 'LAB_B18_COMPONENT_SECRET'
RETRIEVER = {'sha256': 'a' * 64, 'sources': ['skills/SKILL.md']}


def _campaign(tmp_path, monkeypatch):
    from evolution.tree.evolve import evolve
    from b10_campaigns import NeutralInstance, NeutralTarget, SCORER, EDITOR
    from scaffold_16 import scaffold_16

    monkeypatch.setenv(SECRET_ENV, SECRET)
    # A component ID carrying a run secret: b17 ended such a run `persistence_failed`; b18 leaves it out of the
    # published map and counts it as `components_withheld`.
    monkeypatch.setattr(NeutralInstance, 'components', {'retriever': RETRIEVER, f'{SECRET}-index': RETRIEVER})
    sample = scaffold_16(tmp_path, mode='point')
    document = json.loads(sample.manifest.read_text())
    for role, program, member in (('scorer', SCORER, 'score.py'), ('editor', EDITOR, 'edit.py')):
        (tmp_path / role / member).write_text(program)
        document[role]['argv'] = [sys.executable, member]
    document['scorer']['allowed_env'] = [SECRET_ENV]
    document['scorer']['metric_contract'] = {
        'version': 'metric_contract/1', 'kind': 'pointwise', 'target_fields': []}
    group = {'root_cause': 'r', 'suggested_fix': 'f', 'evidence_ids': ['obs:c1:1'],
             'affected_cases': ['c1'], 'target_paths': ['skills/SKILL.md'],
             'counterevidence': 'c', 'regression_risks': 'r', 'why_this_layer': 'w'}
    diagnosis = {'status': 'reflected', 'note': 'neutral', 'groups': [group]}
    (tmp_path / 'reflector/reflect.py').write_text('print(' + repr(json.dumps(diagnosis)) + ')\n')
    document['reflector']['argv'] = [sys.executable, 'reflect.py']
    rows = [{'case_id': case, 'group_id': 'g1', 'stratum': 's1',
             'public': {'text': 'hello', 'case_id': case}, 'oracle': {'critical': True},
             'reflection_metadata': {'uses': []}} for case in ('c1', 'c2')]
    (tmp_path / 'corpus.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    document['campaign'].update(rounds=1, attempts_per_round=1, max_concurrency=1,
                                baseline_refresh_after_rounds=0, random_seed=7)
    sample.manifest.write_text(json.dumps(document, indent=2) + '\n')
    run = tmp_path / 'run'
    target = NeutralTarget()
    result = asyncio.run(evolve(sample.manifest, run, adapter_factory=lambda *_a, **_k: target))
    assert result.terminal == 'finished', (result.terminal, result.code)
    return run


def _declarations(run: Path):
    for journal in sorted(run.glob('*/*/events.jsonl')):
        for line in journal.read_bytes().splitlines():
            record = json.loads(line)
            if record['event'] == 'components_declared':
                yield record['payload']


def test_withheld_component_campaign_reads_all_surfaces(tmp_path, monkeypatch):
    run = _campaign(tmp_path, monkeypatch)
    declared = list(_declarations(run))
    assert declared and all(d.get('components_withheld') == 1 and d['components'] == {'retriever': RETRIEVER}
                            for d in declared)

    log = core_raw.campaign_log(run, families=core_raw.FAMILIES)
    assert log.journal_integrity == 'complete'
    read = core_raw.campaign_read(run, with_result=True, with_bindings=True)
    assert read.family == 15
    assert core_raw.campaign_suspensions(run) == []
    assert core_raw.campaign_details(run, read=read)['journal'] == 'present'
    assert isinstance(core_raw.campaign_content(run), dict)
    facts = core_raw.baseline_provenance_state(run, read.view.snapshot)
    assert facts['samples'] == facts['admitted'] == facts['known'] > 0
    outputs = read_outputs(run)
    assert outputs['campaign.json']['details_state'] == 'present'
    assert outputs['owner-report.json']['state'] == 'available'
    measurement = run / 'measurements' / 'm-r00-champion'
    outputs['measurements'] = measurement_rows(read.status.to_payload(), 'campaign',
                                               {'m-r00-champion': read_journal_facts(measurement)}, 'score')
    assert outputs['measurements']
    assert SECRET not in json.dumps(outputs, ensure_ascii=False, default=str)
    for path in run.rglob('*'):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path.relative_to(run)
