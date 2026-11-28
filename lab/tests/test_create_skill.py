import copy
import pytest
from evolution_lab.guides import scenarios

D = 'sha256:' + 'a' * 64
SAMPLES = {
    'task': {'schema': 'create-skill-task/1', 'implementation': 'agent', 'request': 'Organize notes', 'inputs': []},
    'scenarios': {'schema': 'create-skill-scenarios/1', 'scenarios': [{'id': 'one', 'title': 'Organize', 'summary': 'Organize notes', 'decision': 'include', 'examples': [{'id': 'q1', 'question': 'Organize this note', 'expected': 'Returns a grouped note'}], 'tools': ['read']}]},
    'tools': {'schema': 'create-skill-tools/1', 'tools': [{'id': 'read', 'name': 'Read', 'description': 'Reads files', 'origin': 'existing', 'needed': 'yes', 'sources': ['agent/read.py']}]},
    'brief': {'schema': 'create-skill-brief/1', 'implementation': 'agent', 'skill': {'name': 'organize', 'path': 'skills/organize', 'summary': 'Organize notes'}, 'scenarios': ['one'], 'tools': ['read'], 'constraints': [], 'check_plan': 'Ask one question'},
    'build': {'schema': 'create-skill-build/1', 'skill_path': 'skills/organize', 'files': [{'path': 'SKILL.md', 'role': 'instructions'}], 'place_before': D, 'outside_changes': [], 'review': {'status': 'approved', 'notes': []}},
    'check': {'schema': 'create-skill-check/1', 'skill_path': 'skills/organize', 'skill_digest': D, 'target': {'bundle_digest': D, 'started_at': '2026-01-01'}, 'status': 'checked', 'reason': None, 'scenarios': [{'id': 'one', 'verdict': 'passed', 'by': 'reviewer', 'examples': [{'id': 'q1', 'turn': {'session_id': 's', 'turn_id': 't'}, 'verdict': 'passed', 'note': None}], 'human': None, 'closed': None}], 'previous': []},
}

def test_create_skill_catalogue_and_descriptor():
    cat = scenarios()
    assert cat.catalogue['catalogue_version'] == 12
    assert [x['id'] for x in cat.descriptors] == ['connect', 'create-skill', 'evolve', 'restructure']
    d = cat.descriptor('create-skill')
    assert (d['subject'], d['requires'], d['produces'], d['repeatable']) == ('implementation', [], ['create_skill.accepted'], None)
    assert [(x['id'], x['kind'], x['human_only'], x['requires']) for x in d['steps']] == [
        ('task','decide',False,[]),('scenarios','decide',False,['task']),('tools','decide',False,['scenarios']),('brief','decide',False,['tools']),('brief-confirm','confirm',True,['brief']),('build','work',False,['brief-confirm','implementation.available']),('check','work',False,['build']),('accept','confirm',True,['check'])]

@pytest.mark.parametrize('name', SAMPLES)
def test_examples_valid(name):
    assert scenarios().check_artifact('create-skill', name+'/1', SAMPLES[name]) == []

@pytest.mark.parametrize('name,change', [
    ('task', lambda d: d['inputs'].append({'path':'x','read':False,'kind':None,'note':None})),
    ('tools', lambda d: d['tools'][0].update(origin='new')),
    ('brief', lambda d: d['skill'].update(path='a/../b')),
    ('build', lambda d: d['review'].update(status='changes_requested')),
    ('check', lambda d: d.update(skill_digest=None)),
    ('check', lambda d: d.update(target=None)),
    ('check', lambda d: d['scenarios'][0]['examples'][0].update(verdict='not_checked')),
    ('check', lambda d: d.update(status='partial', reason='interrupted', target=None)),
    ('check', lambda d: d.update(status='not_run', reason='interrupted', target=None, skill_digest=None)),
    ('check', lambda d: d.update(status='not_run', reason='not_connected', target=None, skill_digest=None)),
])
def test_invalid_documents(name, change):
    doc = copy.deepcopy(SAMPLES[name]); change(doc)
    assert scenarios().check_artifact('create-skill', name+'/1', doc)

def test_partial_may_have_no_checked_examples():
    doc = copy.deepcopy(SAMPLES['check']); doc.update(status='partial', reason='interrupted')
    doc['scenarios'][0]['verdict'] = 'not_checked'
    doc['scenarios'][0]['examples'][0].update(verdict='not_checked', turn=None)
    assert scenarios().check_artifact('create-skill','check/1',doc) == []

from test_projects_session import session_env  # noqa: F401
import json
import shutil

async def test_fresh_and_invalid_status(session_env):
    session, projects, _ = session_env
    async def status():
        return (await session.status_payload('acq', 'agent', scenario='create-skill'))['scenarios'][0]
    row = await status()
    assert row['state'] == 'available' and row['steps'][0]['state'] == 'ready'
    folder = projects.root_path(projects.get('acq')) / 'create-skill' / 'agent'
    folder.mkdir(parents=True)
    (folder / 'task.json').write_text(json.dumps(SAMPLES['task']))
    (folder / 'scenarios.json').write_text('{}')
    row = await status(); steps = {s['id']: s for s in row['steps']}
    assert (steps['scenarios']['state'], steps['scenarios']['blocked_reason']['code']) == ('blocked', 'artifact_invalid')
    shutil.rmtree(projects.place_path(projects.get('acq'), projects.get('acq').implementation('agent')))
    steps = {s['id']: s for s in (await status())['steps']}
    assert 'implementation.available' in steps['build']['missing']

async def test_archive_requires_revoke(session_env):
    session, projects, _ = session_env
    folder = projects.root_path(projects.get('acq')) / 'create-skill' / 'agent'
    folder.mkdir(parents=True)
    for name, doc in SAMPLES.items():
        (folder / (name + '.json')).write_text(json.dumps(doc))
    place = projects.place_path(projects.get('acq'), projects.get('acq').implementation('agent'))
    skill = place / 'skills' / 'organize'; skill.mkdir(parents=True); (skill / 'SKILL.md').write_text('skill')
    prefix = 'create-skill/agent/'
    def file_link(name): return {'kind': 'file', 'ref': prefix + name + '.json'}
    brief = await session.confirm('acq', 'agent', claim='create_skill.brief_confirmed', attests={'path':prefix+'brief.json'}, linked=[file_link(n) for n in ('task','scenarios','tools')], channel='cli', author='human', note=None)
    accepted = await session.confirm('acq', 'agent', claim='create_skill.accepted', attests={'path':prefix+'check.json'}, linked=[*[file_link(n) for n in ('task','scenarios','tools','brief','build')], {'kind':'tree','ref':'implementation:agent:skills/organize'}], channel='cli', author='human', note=None)
    async def status(): return (await session.status_payload('acq', 'agent', scenario='create-skill'))['scenarios'][0]
    assert (await status())['state'] == 'produced'
    archive = folder / 'done' / 'organize'; archive.mkdir(parents=True)
    for f in folder.glob('*.json'): shutil.move(str(f), archive / f.name)
    old = {s['id']:s for s in (await status())['steps']}
    assert old['brief-confirm']['state'] == old['accept']['state'] == 'stale'
    for name in ('task','scenarios','tools','brief'):
        doc = copy.deepcopy(SAMPLES[name]);
        if name == 'brief': doc['skill']['summary'] = 'A new skill'
        (folder / (name + '.json')).write_text(json.dumps(doc))
    dry = await session.accept('acq', 'agent', only=['create_skill.brief_confirmed'], across_pairs=False, reason='inspect', channel='cli', author='human', dry_run=True)
    assert any(f['id'] == 'create_skill.brief_confirmed' for f in dry.facts)
    await session.revoke_confirmation('acq', brief.id, 'archived')
    await session.revoke_confirmation('acq', accepted.id, 'archived')
    row = await status(); steps = {s['id']:s for s in row['steps']}
    assert steps['brief-confirm']['state'] == 'ready' and steps['accept']['state'] == 'blocked'
    assert all(steps[name]['state'] == 'done' for name in ('task','scenarios','tools','brief'))
    assert row['state'] != 'produced'

def test_descriptor_outputs_and_anchors():
    d = scenarios().descriptor('create-skill')
    prefix = 'create-skill/{implementation}/'
    assert d['schemas'] == {n+'/1': 'schemas/'+n+'-1.json' for n in SAMPLES}
    for step in d['steps']:
        assert not step['optional'] and step['skill'] == 'SKILL.md#'+step['id']
        output = step['outputs'][0]
        if step['kind'] == 'confirm':
            assert output['attestation']['path'] == prefix + ('brief.json' if step['id'] == 'brief-confirm' else 'check.json')
        else:
            assert output['artifact'] == {'path': prefix + step['id']+'.json', 'check': 'schema', 'schema': step['id']+'/1'}

@pytest.mark.parametrize('name,change,pointer', [
    ('task', lambda d: d['inputs'].append({'path':'x','read':False,'kind':None,'note':None}), '/inputs/0/note'),
    ('tools', lambda d: d['tools'][0].update(origin='new'), '/tools/0/origin'),
    ('brief', lambda d: d['skill'].update(path='a/../b'), '/skill/path'),
    ('build', lambda d: d['review'].update(status='changes_requested'), '/review/notes'),
    ('check', lambda d: d.update(skill_digest=None), '/skill_digest'),
    ('check', lambda d: d.update(target=None), '/target'),
    ('check', lambda d: d['scenarios'][0]['examples'][0].update(verdict='not_checked'), '/scenarios/0/examples/0/verdict'),
    ('check', lambda d: d.update(status='partial', reason='interrupted', target=None), '/target'),
    ('check', lambda d: d.update(status='not_run', reason='interrupted', target=None, skill_digest=None), '/reason'),
    ('check', lambda d: d.update(status='not_run', reason='not_connected', target=None, skill_digest=None), '/scenarios/0/verdict'),
])
def test_schema_errors_at_pointers(name, change, pointer):
    doc = copy.deepcopy(SAMPLES[name]); change(doc)
    assert pointer in [error['path'] for error in scenarios().check_artifact('create-skill', name+'/1', doc)]

def test_create_skill_cli_examples_are_valid_concrete_commands():
    """Every complete command, including inline procedures, must match the actual CLI."""
    import re
    import shlex
    from pathlib import Path
    from evolution_lab.cli.parser import build_parser
    text = (Path(__file__).parents[1] / 'src/evolution_lab/guides/scenarios/create-skill/SKILL.md').read_text()
    fenced = re.findall(r'^lab (.+)$', text, re.MULTILINE)
    inline = re.findall(r'`lab ([^`]+)`', text)
    complete = [line for line in fenced + inline if '<' in line]
    assert len(complete) >= 15
    for command in complete:
        command = re.sub(r'<number-of-example-questions>', '3', command)
        command = re.sub(r'<generation>', '1', command)
        command = re.sub(r'<[^>]+>', 'example', command)
        build_parser().parse_args(shlex.split(command))

@pytest.mark.parametrize('status', ['checked', 'partial'])
def test_checked_and_partial_require_at_least_one_example(status):
    doc = copy.deepcopy(SAMPLES['check'])
    doc['status'] = status
    doc['reason'] = 'interrupted' if status == 'partial' else None
    doc['scenarios'][0]['examples'] = []
    assert any(e['path'] == '/scenarios/0/examples' for e in scenarios().check_artifact('create-skill', 'check/1', doc))


def test_interrupted_before_first_answer_is_valid():
    doc = copy.deepcopy(SAMPLES['check'])
    doc.update(status='partial', reason='interrupted')
    doc['scenarios'][0].update(verdict='not_checked')
    doc['scenarios'][0]['examples'][0].update(verdict='not_checked', turn=None, note='interrupted')
    assert scenarios().check_artifact('create-skill', 'check/1', doc) == []


def test_not_run_preserves_legacy_empty_examples():
    doc = copy.deepcopy(SAMPLES['check'])
    doc.update(status='not_run', reason='not_connected', skill_digest=None, target=None)
    doc['scenarios'][0].update(verdict='not_checked', examples=[])
    assert scenarios().check_artifact('create-skill', 'check/1', doc) == []


def test_check_keeps_verdicts_only_on_same_live_target():
    from pathlib import Path
    skill = (Path(__file__).parents[1] / 'src/evolution_lab/guides/scenarios/create-skill/SKILL.md').read_text()
    check = skill.split('<a id="check"></a>', 1)[1].split('<a id="accept"></a>', 1)[0]
    assert 'same live target' in check
    assert 'even when the skill and Bundle digests are unchanged' in check
    assert 'start over with no prior verdicts' in check


def test_build_verifies_snapshot_before_creating_review_report():
    from pathlib import Path
    skill = (Path(__file__).parents[1] / 'src/evolution_lab/guides/scenarios/create-skill/SKILL.md').read_text()
    build = skill.split('<a id="build"></a>', 1)[1].split('<a id="check"></a>', 1)[0]
    assert 'compute a fresh SHA-256 of `place-before.json`' in build
    assert 'compare it with the independently recorded hash' in build
    assert 'after approval, write the verified hash into `build.json.place_before`' in build
    assert 'without requiring an existing `build.json`' in build
