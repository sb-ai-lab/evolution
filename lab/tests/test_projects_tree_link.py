from pathlib import Path
import shutil

import pytest

from evolution_lab.projects.errors import ProjectError
from test_projects_session import session_env  # noqa: F401


def _setup(projects):
    project = projects.get('acq')
    root = Path(project.working_copy.path)
    (root / 'lab' / 'SPEC.md').write_text('brief')
    folder = root / 'agent' / 'skills' / 'new'
    (folder / 'ref').mkdir(parents=True)
    (folder / 'SKILL.md').write_text('skill')
    (folder / 'ref' / 'a.txt').write_text('a')
    (folder / 'alias').symlink_to('SKILL.md')
    return root, folder


async def _confirm(session, ref='implementation:agent:skills/new'):
    return await session.confirm('acq', None, claim='skill.confirmed', attests={'path': 'SPEC.md'},
                                 linked=[{'kind': 'tree', 'ref': ref}], channel='cli', author='human', note=None)


async def _fact(session):
    _, facts = await session.facts('acq')
    return next(f for f in facts if f.id == 'skill.confirmed')


async def test_tree_link_digest_stable_and_ignores_outside(session_env):
    session, projects, _ = session_env
    root, folder = _setup(projects)
    confirmation = await _confirm(session)
    assert confirmation.linked[0]['digest'].startswith('sha256:')
    assert (await _fact(session)).holds
    (root / 'agent' / 'a.py').write_text('changed')
    (folder / 'ignored.pyc').write_bytes(b'ignored')
    assert (await _fact(session)).holds


@pytest.mark.parametrize('change', ['edit', 'add', 'remove', 'mode', 'symlink'])
async def test_tree_link_stales_on_folder_edit(session_env, change):
    session, projects, _ = session_env
    _, folder = _setup(projects)
    await _confirm(session)
    if change == 'edit':
        (folder / 'ref' / 'a.txt').write_text('changed')
    elif change == 'add':
        (folder / 'new.txt').write_text('new')
    elif change == 'remove':
        (folder / 'ref' / 'a.txt').unlink()
    elif change == 'mode':
        (folder / 'SKILL.md').chmod(0o755)
    else:
        (folder / 'alias').unlink()
        (folder / 'alias').symlink_to('ref/a.txt')
    fact = await _fact(session)
    assert not fact.holds and fact.stale_reason.codes == ('digest_changed',)


async def test_tree_link_accept_carries(session_env):
    session, projects, _ = session_env
    _, folder = _setup(projects)
    await _confirm(session)
    (folder / 'SKILL.md').write_text('changed')
    await session.accept('acq', None, only=['skill.confirmed'], across_pairs=False,
                         reason='ok', channel='cli', author='human', dry_run=False)
    assert (await _fact(session)).holds


async def test_tree_link_missing_folder(session_env):
    session, projects, _ = session_env
    _, folder = _setup(projects)
    for path in ('missing', 'empty'):
        if path == 'empty':
            (folder.parent / path).mkdir()
        with pytest.raises(ProjectError) as caught:
            await _confirm(session, 'implementation:agent:skills/' + path)
        assert caught.value.code == 'tree_not_found'
    await _confirm(session)
    shutil.rmtree(folder)
    assert not (await _fact(session)).holds
    with pytest.raises(ProjectError) as caught:
        await session.accept('acq', None, only=['skill.confirmed'], across_pairs=False,
                             reason='ok', channel='cli', author='human', dry_run=False)
    assert caught.value.code == 'not_acceptable'


async def test_tree_link_project_excluded(session_env):
    session, projects, _ = session_env
    _setup(projects)
    pattern = 'agent/skills/new/ref/**'
    projects.update('acq', {'op': 'set_exclude', 'exclude': [pattern]})
    with pytest.raises(ProjectError) as caught:
        await _confirm(session)
    assert caught.value.code == 'tree_excluded' and pattern in caught.value.detail


@pytest.mark.parametrize('ref,code', [
    ('implementation:agent:../lab', 'invalid_request'),
    ('implementation:agent:/tmp', 'invalid_request'),
    ('implementation:agent', 'invalid_request'),
    ('implementation:missing:skills', 'implementation_not_found'),
    ('implementation:agent:escape/other', 'invalid_request'),
])
async def test_tree_link_path_safety(session_env, ref, code, tmp_path):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    (root / 'agent' / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ProjectError) as caught:
        await _confirm(session, ref)
    assert caught.value.code == code


async def test_tree_link_dot_and_nested_cut(session_env):
    from dataclasses import replace
    from evolution_lab.projects.revision import materials_revision
    from evolution_lab.projects.tree import excluded, tree_digest, tree_entries
    session, projects, _ = session_env
    root, _ = _setup(projects)
    project = projects.get('acq')
    revision = materials_revision(project)
    confirmation = await _confirm(session, 'implementation:agent:.')
    assert confirmation.linked[0]['digest'] == tree_digest(tree_entries(project, revision, 'agent', '.'))
    nested = replace(project, implementations=(replace(project.implementations[0], place='.'),))
    for path in ('.', 'lab', 'lab/nested'):
        assert excluded(nested, 'agent', path) is not None


@pytest.mark.parametrize('pattern,refuses', [
    ('agent/skills/new/ref/**', True),
    ('agent/skills/new', True),
    ('agent/skills/*/ref/**', True),
    ('agent/skills/other/**', False),
    ('agent/skills/n[!e]w/**', False),
    ('agent/skills/n[ae]w/**', True),
])
def test_exclusion_pattern_intersection(session_env, pattern, refuses):
    from dataclasses import replace
    from evolution_lab.projects.tree import excluded
    _, projects, _ = session_env
    _setup(projects)
    project = replace(projects.get('acq'), exclude=(pattern,))
    assert (excluded(project, 'agent', 'skills/new') is not None) is refuses


async def test_tree_link_becomes_unacceptable_if_excluded_later(session_env):
    session, projects, _ = session_env
    _setup(projects)
    await _confirm(session)
    projects.update('acq', {'op': 'set_exclude', 'exclude': ['agent/skills/new/ref/**']})
    fact = await _fact(session)
    assert not fact.holds and fact.state['linked'] == [None]
    with pytest.raises(ProjectError) as caught:
        await session.accept('acq', None, only=['skill.confirmed'], across_pairs=False,
                             reason='ok', channel='cli', author='human', dry_run=False)
    assert caught.value.code == 'not_acceptable'


async def test_tree_link_through_cli_json_and_service_decoder(session_env):
    import json
    from evolution_lab.cli.responses import _project_confirmation_list
    from test_service_cli import FakeClient, invoke
    session, projects, _ = session_env
    _setup(projects)
    confirmation = await _confirm(session)
    payload = {'schema': 'lab-project-confirmation-list/1', 'confirmations': [confirmation.to_payload()]}
    client = FakeClient([payload])
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', '--json', 'project', 'confirmations', 'acq'], client)
    assert code == 0, stderr
    assert json.loads(stdout) == payload
    assert _project_confirmation_list(payload)[0].linked[0]['kind'] == 'tree'
    assert client.calls[0][1] == '/v1/projects/acq/confirmations'


async def test_tree_link_service_round_trip(session_env, tmp_path):
    import httpx
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    from test_service_projects import FakeOwner
    session, projects, _ = session_env
    _setup(projects)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / 's.sock', tmp_path / 'service-state', 0.2,
                                           home_directory=tmp_path / 'service-home'))
    runtime.project_session = session
    runtime.owner = FakeOwner()
    runtime._admission_open = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        body = {'schema': 'lab-service-confirm-request/1', 'implementation': None,
                'claim': 'skill.confirmed', 'attests': {'path': 'SPEC.md'},
                'linked': [{'kind': 'tree', 'ref': 'implementation:agent:skills/new'}],
                'channel': 'cli', 'author': 'human', 'note': None}
        response = await client.post('/v1/projects/acq/confirmations', json=body)
        assert response.status_code == 200, response.text
        assert response.json()['linked'][0]['kind'] == 'tree'
        listed = await client.get('/v1/projects/acq/confirmations')
        assert listed.status_code == 200
        assert listed.json()['confirmations'][0] == response.json()


def test_tree_digest_canonical_content_and_order():
    import hashlib
    import json
    from evolution_lab.projects.inventory import Entry
    from evolution_lab.projects.tree import tree_digest
    entries = (Entry('z', 'symlink', 0, None, None, 'a'),
               Entry('a', 'file', 0o100, 12, 'abc'))
    canonical = [['a', 'file', 0o100, 'abc', None], ['z', 'symlink', 0, None, 'a']]
    expected = 'sha256:' + hashlib.sha256(json.dumps(canonical, ensure_ascii=False,
                                                     separators=(',', ':')).encode()).hexdigest()
    assert tree_digest(entries) == expected


async def test_tree_service_errors_preserve_exclusion_detail(session_env, tmp_path):
    import httpx
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    from evolution_lab.cli.responses import _failure
    from test_service_projects import FakeOwner
    session, projects, _ = session_env
    _setup(projects)
    pattern = 'agent/skills/new/ref/**'
    projects.update('acq', {'op': 'set_exclude', 'exclude': [pattern]})
    runtime = ServiceRuntime(ServiceConfig(tmp_path / 's.sock', tmp_path / 'state2', 0.2,
                                           home_directory=tmp_path / 'home2'))
    runtime.project_session, runtime.owner, runtime._admission_open = session, FakeOwner(), True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        body = {'schema': 'lab-service-confirm-request/1', 'implementation': None,
                'claim': 'skill.confirmed', 'attests': {'path': 'SPEC.md'},
                'linked': [{'kind': 'tree', 'ref': 'implementation:agent:skills/new'}],
                'channel': 'cli', 'author': 'human', 'note': None}
        response = await client.post('/v1/projects/acq/confirmations', json=body)
        assert response.status_code == 422
        failure = response.json()['error']
        assert failure['code'] == 'tree_excluded' and pattern in failure['message']
        assert _failure(failure) is None


def test_tree_entries_exclude_synthetic_other_place_and_keep_colliding_real_file(tmp_path):
    from evolution_lab.projects.models import Implementation, Project, WorkingCopy
    from evolution_lab.projects.revision import materials_revision
    from evolution_lab.projects.tree import tree_entries
    work = tmp_path / 'work'
    external = tmp_path / 'external'
    (work / '@other').mkdir(parents=True)
    external.mkdir()
    (work / '@other' / 'file.txt').write_text('real inside work')
    (external / 'file.txt').write_text('external data')
    project = Project('p', 'P', 'ready', None, 't', None, 1,
                      WorkingCopy(str(work), False, False, None), '.', (),
                      (Implementation('main', 'Main', '.', {'kind': 'connected'}, None, None),
                       Implementation('other', 'Other', str(external), {'kind': 'connected'}, None, None)))
    revision = materials_revision(project)
    entries = tree_entries(project, revision, 'main', '.')
    found = next(entry for entry in entries if entry.path == '@other/file.txt')
    import hashlib
    assert found.sha256 == hashlib.sha256(b'real inside work').hexdigest()


def test_tree_digest_ignores_unrelated_absolute_place(tmp_path):
    from evolution_lab.projects.models import Implementation, Project, WorkingCopy
    from evolution_lab.projects.revision import materials_revision
    from evolution_lab.projects.tree import tree_digest, tree_entries
    work, external = tmp_path / 'work', tmp_path / 'external'
    work.mkdir()
    external.mkdir()
    (work / 'SKILL.md').write_text('inside')
    (external / 'file.txt').write_text('before')
    project = Project('p', 'P', 'ready', None, 't', None, 1,
                      WorkingCopy(str(work), False, False, None), '.', (),
                      (Implementation('main', 'Main', '.', {'kind': 'connected'}, None, None),
                       Implementation('other', 'Other', str(external), {'kind': 'connected'}, None, None)))
    before = tree_entries(project, materials_revision(project), 'main', '.')
    assert [entry.path for entry in before] == ['SKILL.md']
    (external / 'file.txt').write_text('after')
    after = tree_entries(project, materials_revision(project), 'main', '.')
    assert tree_digest(before) == tree_digest(after)


def test_tree_uses_captured_place_inventory_without_new_walk(session_env, monkeypatch):
    from evolution_lab.projects import revision as revision_module
    from evolution_lab.projects.tree import tree_entries
    _, projects, _ = session_env
    _setup(projects)
    project = projects.get('acq')
    revision = revision_module.materials_revision(project)
    payload = revision.to_payload()
    monkeypatch.setattr(revision_module, 'collect', lambda *args, **kwargs: pytest.fail('unexpected inventory walk'))
    assert tree_entries(project, revision, 'agent', 'skills/new')
    assert revision.to_payload() == payload


def _one_file(root):
    skill = root / 'agent' / 'skills' / 'one.yaml'
    skill.write_text('recipe: one')
    return skill


async def test_tree_link_single_file_skill(session_env):
    # PDR-0032 amendment 2026-11-13: a skill is a folder or one file.
    from evolution_lab.projects.revision import materials_revision
    from evolution_lab.projects.tree import tree_entries
    session, projects, _ = session_env
    root, _ = _setup(projects)
    skill = _one_file(root)
    entries = tree_entries(projects.get('acq'), materials_revision(projects.get('acq')), 'agent', 'skills/one.yaml')
    assert [(entry.path, entry.type) for entry in entries] == [('.', 'file')]
    confirmation = await _confirm(session, 'implementation:agent:skills/one.yaml')
    assert confirmation.linked[0]['digest'].startswith('sha256:')
    assert (await _fact(session)).holds
    (root / 'agent' / 'skills' / 'new' / 'SKILL.md').write_text('a neighbouring skill changed')
    (root / 'agent' / 'skills' / 'other.yaml').write_text('a neighbouring file')
    assert (await _fact(session)).holds
    skill.write_text('recipe: changed')
    fact = await _fact(session)
    assert not fact.holds and fact.stale_reason.codes == ('digest_changed',)


@pytest.mark.parametrize('change', ['mode', 'symlink', 'remove', 'directory'])
async def test_tree_link_single_file_stales(session_env, change):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    skill = _one_file(root)
    await _confirm(session, 'implementation:agent:skills/one.yaml')
    if change == 'mode':
        skill.chmod(0o755)
    else:
        skill.unlink()
        if change == 'symlink':
            skill.symlink_to('new/SKILL.md')
        elif change == 'directory':
            # Same name and bytes one level down must not keep the file's digest.
            skill.mkdir()
            (skill / 'one.yaml').write_text('recipe: one')
    assert not (await _fact(session)).holds


async def test_tree_link_refuses_symlinked_file(session_env):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    (root / 'agent' / 'skills' / 'link.yaml').symlink_to('new/SKILL.md')
    with pytest.raises(ProjectError) as caught:
        await _confirm(session, 'implementation:agent:skills/link.yaml')
    assert caught.value.code == 'tree_not_found'


@pytest.mark.parametrize('pattern,refuses', [
    ('agent/skills/one.yaml', True),
    ('agent/skills/*.yaml', True),
    ('agent/skills/**', True),
    ('agent/skills/*.md', False),
    ('*.pyc', False),
    ('agent/skills/one.yaml/**', True),  # the inventory drops 'X' itself for 'X/**'
])
def test_exclusion_of_single_file(session_env, pattern, refuses):
    # A file has no contents below it: only the file and its ancestors can match.
    from dataclasses import replace
    from evolution_lab.projects.tree import excluded
    _, projects, _ = session_env
    root, _ = _setup(projects)
    _one_file(root)
    project = replace(projects.get('acq'), exclude=(pattern,))
    assert (excluded(project, 'agent', 'skills/one.yaml') is not None) is refuses
