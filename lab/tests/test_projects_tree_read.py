from pathlib import Path

import pytest

from evolution_lab.projects.errors import ProjectError
from test_projects_session import session_env  # noqa: F401
from test_projects_tree_link import _setup, _confirm


async def test_tree_read_matches_link_and_lists_entries(session_env):
    session, projects, _ = session_env
    _, folder = _setup(projects)
    (folder / 'binary').write_bytes(b'\x89PNG\x00')
    (folder / 'nul').write_bytes(b'a\x00b')
    confirmation = await _confirm(session)
    result = await session.tree('acq', 'agent', 'skills/new')
    assert result['schema'] == 'lab-project-tree/1'
    assert result['digest'] == confirmation.linked[0]['digest']
    assert result['exists'] is True
    entries = {e['path']: e for e in result['files']}
    assert entries['ref']['type'] == 'directory'
    assert entries['ref']['size'] is None and entries['ref']['sha256'] is None
    assert entries['alias']['target'] == 'SKILL.md'
    assert all(e['text'] is None for e in entries.values())
    text = await session.tree('acq', 'agent', 'skills/new', text=True)
    entries = {e['path']: e for e in text['files']}
    assert entries['SKILL.md']['text'] is True
    assert entries['binary']['text'] is False and entries['nul']['text'] is False
    assert entries['ref']['text'] is None and entries['alias']['text'] is None


async def test_tree_read_absent_and_excluded(session_env):
    session, projects, _ = session_env
    _setup(projects)
    result = await session.tree('acq', 'agent', 'not-there')
    assert not result['exists'] and result['digest'] is None and result['files'] == []
    projects.update('acq', {'op': 'set_exclude', 'exclude': ['agent/skills/new/ref/**']})
    with pytest.raises(ProjectError) as caught:
        await session.tree('acq', 'agent', 'skills/new')
    assert caught.value.code == 'tree_excluded'
    overview = await session.tree('acq', 'agent', '.')
    assert all(not entry['path'].startswith('skills/new/ref') for entry in overview['files'])


async def test_tree_read_unknown_and_escape(session_env, tmp_path):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    (root / 'agent' / 'escape').symlink_to(tmp_path, target_is_directory=True)
    for implementation, path, code in [('agent', '../lab', 'invalid_request'),
                                       ('agent', 'escape/x', 'invalid_request'),
                                       ('missing', '.', 'implementation_not_found')]:
        with pytest.raises(ProjectError) as caught:
            await session.tree('acq', implementation, path)
        assert caught.value.code == code


async def test_tree_read_bundle_coverage(session_env):
    import json
    session, projects, _ = session_env
    root, _ = _setup(projects)
    package = root / 'lab' / 'bundle-request.json'
    data = json.loads(package.read_text())
    data['roots'] = [{'alias': 'skills', 'source_path': '../agent/skills'}]
    package.write_text(json.dumps(data))
    assert (await session.tree('acq', 'agent', 'skills/new'))['bundle_covers'] is True
    assert (await session.tree('acq', 'agent', '.'))['bundle_covers'] is False
    projects.update('acq', {'op': 'set_package', 'implementation': 'agent', 'package_entry': None})
    result = await session.tree('acq', 'agent', 'skills/new')
    assert result['bundle_covers'] is None and result['bundle_reason'] == 'target_undeclared'


async def test_default_tree_uses_cached_hashes_and_never_reads_contents(session_env, monkeypatch):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    await session.tree('acq', 'agent', 'skills/new')  # fill the cache
    original_open = Path.open
    def guard(path, *args, **kwargs):
        if Path(path).is_relative_to(root / 'agent'):
            pytest.fail('opened implementation content for the default read')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guard)
    import evolution_lab.projects.tree as tree_module
    monkeypatch.setattr(tree_module, 'text_file', lambda *a: pytest.fail('text classifier called'))
    result = await session.tree('acq', 'agent', 'skills/new')
    assert all(item['text'] is None for item in result['files'])


async def test_tree_surfaces_match(session_env, tmp_path):
    import httpx
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    from evolution_lab.cli.responses import validate
    from evolution_lab.cli.project import add_parser, request
    from test_service_projects import FakeOwner
    import argparse
    session, projects, _ = session_env
    _setup(projects)
    expected = await session.tree('acq', 'agent', 'skills/new')
    runtime = ServiceRuntime(ServiceConfig(tmp_path / 's.sock', tmp_path / 'state', 0.2,
                                           home_directory=tmp_path / 'home'))
    runtime.project_session, runtime.owner, runtime._admission_open = session, FakeOwner(), True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        response = await client.get('/v1/projects/acq/tree', params={'implementation': 'agent', 'path': 'skills/new'})
        assert response.status_code == 200 and response.json() == expected
        response_text = await client.get('/v1/projects/acq/tree', params={'implementation': 'agent', 'path': 'skills/new', 'text': '1'})
        expected_text = await session.tree('acq', 'agent', 'skills/new', text=True)
        assert response_text.status_code == 200 and response_text.json() == expected_text
        assert (await client.get('/v1/projects/acq/tree', params={'implementation': 'agent', 'path': '.', 'text': '0'})).json()['error']['code'] == 'invalid_request'
        assert (await client.get('/v1/projects/acq/tree', params={'implementation': 'agent', 'path': '.', 'bad': '1'})).json()['error']['code'] == 'invalid_request'
        assert (await client.get('/v1/projects/acq/tree', params={'implementation': 'agent', 'path': '.'}, headers={'lab-request-id': 'abc'})).json()['error']['code'] == 'invalid_request'
    parser = argparse.ArgumentParser(); add_parser(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['project', 'tree', 'acq', '--implementation', 'agent', '--path', 'skills/new'])
    tree_request = request(args, None, None, None)
    assert tree_request.method == 'GET'
    assert tree_request.path == '/v1/projects/acq/tree?implementation=agent&path=skills%2Fnew'
    assert tree_request.payload is None and tree_request.schema == 'lab-project-tree/1'
    assert validate(expected, tree_request.schema) == expected


async def test_tree_cli_json_and_text_output(session_env):
    import json
    from test_service_cli import FakeClient, invoke
    session, projects, _ = session_env
    _setup(projects)
    payload = await session.tree('acq', 'agent', 'skills/new')
    client = FakeClient([payload])
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', 'project', 'tree', 'acq',
                                   '--implementation', 'agent', '--path', 'skills/new', '--json'], client)
    assert code == 0, stderr
    assert json.loads(stdout) == payload
    assert client.calls[0][1] == '/v1/projects/acq/tree?implementation=agent&path=skills%2Fnew'
    client = FakeClient([payload])
    code, stdout, stderr = invoke(['--socket', '/tmp/lab.sock', 'project', 'tree', 'acq',
                                   '--implementation', 'agent', '--path', 'skills/new'], client)
    assert code == 0, stderr
    assert 'bundle_covers=True' in stdout and 'SKILL.md' in stdout and payload['digest'] in stdout


async def test_tree_dot_applies_nested_materials_cut(session_env):
    session, projects, _ = session_env
    root, _ = _setup(projects)
    projects.update('acq', {'op': 'move_implementation', 'implementation': 'agent', 'place': '.'})
    data = await session.tree('acq', 'agent', '.')
    assert all(row['path'] != 'lab' and not row['path'].startswith('lab/') for row in data['files'])
    assert any(row['path'].startswith('agent/') for row in data['files'])
    with pytest.raises(ProjectError) as caught:
        await session.tree('acq', 'agent', 'lab')
    assert caught.value.code == 'tree_excluded'


async def test_tree_decoder_rejects_bad_payloads(session_env):
    from copy import deepcopy
    from evolution_lab.cli.responses import validate
    session, projects, _ = session_env
    _setup(projects)
    payload = await session.tree('acq', 'agent', 'skills/new')
    for edit in (
        lambda value: value.update(extra=True),
        lambda value: value.update(digest='sha256:bad'),
        lambda value: value['files'][0].update(text='true'),
        lambda value: value['files'][0].update(type='bogus'),
    ):
        value = deepcopy(payload); edit(value)
        with pytest.raises(ValueError):
            validate(value, 'lab-project-tree/1')


async def test_tree_uses_probe_bundle_root_without_package_entry(session_env):
    import json
    session, projects, _ = session_env
    root, _ = _setup(projects)
    place = root / 'agent'
    (place / 'lab-target-entry.json').write_text(json.dumps({
        'schema': 'lab-target-entry/1',
        'agent': {'module': 'a', 'attribute': 'agent', 'import_root': '.'},
        'program_paths': ['a.py'], 'environment': {'kind': 'service'},
        'env': {}, 'secret_names': [],
    }))
    projects.update('acq', {'op': 'set_package', 'implementation': 'agent', 'package_entry': None})
    project = projects.get('acq')
    implementation = project.implementation('agent')
    request = session._bundle_request(project, implementation)
    assert [(item.alias, item.source_path) for item in request.roots] == [('implementation', place)]
    result = await session.tree('acq', 'agent', 'skills/new')
    assert result['bundle_covers'] is True and result['bundle_reason'] is None
    assert result['exists'] is True


async def test_tree_read_single_file_skill(session_env):
    import json
    session, projects, _ = session_env
    root, _ = _setup(projects)
    (root / 'agent' / 'skills' / 'one.yaml').write_text('recipe: one')
    confirmation = await _confirm(session, 'implementation:agent:skills/one.yaml')
    result = await session.tree('acq', 'agent', 'skills/one.yaml', text=True)
    assert result['exists'] is True and result['digest'] == confirmation.linked[0]['digest']
    assert [(row['path'], row['type'], row['text']) for row in result['files']] == [('.', 'file', True)]
    package = root / 'lab' / 'bundle-request.json'
    data = json.loads(package.read_text())
    data['roots'] = [{'alias': 'skills', 'source_path': '../agent/skills'}]
    package.write_text(json.dumps(data))
    assert (await session.tree('acq', 'agent', 'skills/one.yaml'))['bundle_covers'] is True
    data['roots'] = [{'alias': 'skills', 'source_path': '../agent/skills/new'}]
    package.write_text(json.dumps(data))
    assert (await session.tree('acq', 'agent', 'skills/one.yaml'))['bundle_covers'] is False
