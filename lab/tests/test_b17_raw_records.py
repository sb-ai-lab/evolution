"""b17 raw records across the pinned Lab readers; historical fixture bytes stay untouched."""
import hashlib
import json
from pathlib import Path
import shutil

from evolution.tree.execution.journal_identity import canonical_json
from evolution_lab.owner import core_raw
from evolution_lab.owner.measurements import measurement_rows, read_journal_facts
from evolution_lab.owner.sample_view import attempt_view, reflection_view
from dev62_default_outputs import read_outputs

CAPTURE = Path(__file__).parent / 'fixtures/panel/family15/run/bba08804984a456c81c5b7e09e545254'
COMPLETE_CAPTURE = Path(__file__).parent / 'fixtures/panel/b15/focus-first/run'
SENTINEL = 'PRIVATE_PROVIDER_REFUSAL_SENTINEL_72'


def _copied_run(tmp_path, source=CAPTURE):
    return Path(shutil.copytree(source, tmp_path / 'run'))


def test_b17_full_description_campaign_reads_all_surfaces(tmp_path):
    run = _copied_run(tmp_path, COMPLETE_CAPTURE)
    journal = run / 'campaign.jsonl'
    lines = journal.read_bytes().splitlines(keepends=True)
    started, terminal = json.loads(lines[8]), json.loads(lines[9])
    assert started['event'] == 'grouping_started' and terminal['event'] == 'grouping_finished'
    entry = {'group': terminal['payload']['result']['assignments'][0], 'label': 'full label',
             'description': 'Observed: x\nExpected: y\nQuestion: z'}
    record = {'event': 'grouping_full_description', 'operation_id': None,
              'payload': {'grouping_id': started['payload']['grouping_id'], **entry},
              'run_id': started['run_id'], 'schema_version': 1, 'sequence': 10,
              'timestamp': started['timestamp']}
    terminal['sequence'] = 11
    terminal['payload']['full_descriptions_digest'] = hashlib.sha256(canonical_json([entry])).hexdigest()
    # Preserve the original offset-bearing prefix; end immediately after the new terminal.
    journal.write_bytes(b''.join(lines[:9]) + canonical_json(record) + b'\n' + canonical_json(terminal) + b'\n')

    log = core_raw.campaign_log(run, families=core_raw.FAMILIES)
    assert log.journal_integrity == 'complete'
    assert [r.event for r in log.records[-2:]] == ['grouping_full_description', 'grouping_finished']
    assert log.records[-2].payload['description'] == entry['description']
    read = core_raw.campaign_read(run, with_result=True, with_bindings=True)
    assert read.family == 15
    assert read.status.to_payload()['independent']['evidence'] == 'authenticated_publications'
    assert core_raw.campaign_suspensions(run) == []
    assert core_raw.campaign_details(run, read=read)['journal'] == 'present'
    assert isinstance(core_raw.campaign_content(run), dict)
    outputs = read_outputs(run)
    assert outputs['campaign.json']['details_state'] == 'present'
    assert outputs['owner-report.json']['state'] == 'available'
    assert outputs['report.md']
    core = read.status.to_payload()
    measurement = run / 'measurements' / 'm-r00-champion'
    facts = read_journal_facts(measurement)
    assert facts.last_sequence > 0
    rows = measurement_rows(core, 'campaign', {'m-r00-champion': facts}, 'score')
    assert any(row['measurement_id'] == 'm-r00-champion' for row in rows)


def test_provider_refusal_message_stays_private_across_lab_outputs(tmp_path):
    run = _copied_run(tmp_path)
    records = [
        {'phase': 'model_request', 'messages_after': 0,
         'messages': [{'role': 'user', 'content': 'normal opening'}]},
        {'phase': 'provider_refused', 'request_style': 'modern_single', 'turn': 1,
         'attempt': 1, 'http_status': 200, 'error_code': 'unsupported',
         'provider_name': 'neutral', 'message': SENTINEL},
    ]
    for role, ident in (('editor', 'r01-a01'), ('reflector', 'r01-x01')):
        directory = run / f'{role}-logs'
        directory.mkdir(exist_ok=True)
        (directory / f'{ident}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        loaded, broken = core_raw.role_log(run, role, ident)
        assert not broken and loaded[-1]['message'] == SENTINEL
    facts = core_raw.campaign_content(run)
    assert 'r01-a01' in facts['closed'] and 'r01-x01' in facts['reflections']
    read = core_raw.campaign_read(run, with_result=True, with_bindings=True)
    outputs = read_outputs(run)
    outputs['suspensions'] = core_raw.campaign_suspensions(run)
    measurement = run / 'measurements' / 'm-r00-champion'
    journal_facts = read_journal_facts(measurement)
    outputs['measurements'] = measurement_rows(read.status.to_payload(), 'campaign',
                                                {'m-r00-champion': journal_facts}, 'score')
    for disclose in (False, True):
        attempt = attempt_view('operation', 'r01-a01', 1, None, None, 'chat', run, facts, disclose)
        reflection = reflection_view('operation', 'r01-x01', 1, 'chat', run, facts, disclose)
        assert attempt['editor']['state'] == 'available' and reflection['state'] == 'available'
        assert SENTINEL not in json.dumps([attempt, reflection, outputs], ensure_ascii=False)
