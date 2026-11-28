"""Task-oriented primary commands retain the old wire-compatible aliases."""
import io

import pytest

GROUPS = [
    ('Start here', ['new-project', 'evaluate', 'campaign', 'trace']),
    ('Run step by step', ['bundle', 'prepare', 'prepared', 'preflight', 'connect', 'check', 'conformance', 'start', 'resume', 'stop']),
    ('Follow and inspect', ['list', 'status', 'events', 'watch', 'report', 'view', 'suspensions', 'sample', 'attempt', 'reflection', 'measurements', 'diff']),
    ('Results', ['export', 'export-content', 'delete-results']),
    ('Agent processes and policies', ['agent', 'exec-policy', 'routing', 'probe']),
    ('Projects', ['project']),
    ('Service and maintenance', ['serve', 'mcp', 'health', 'panel', 'job', 'cleanup', 'release-operation', 'guides', 'scenarios']),
]


def _help():
    from evolution_lab.cli import main
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(['--help'], stdout=stdout, stderr=stderr) == 0, stderr.getvalue()
    return stdout.getvalue()


def test_help_lists_the_groups_in_order():
    text = _help()
    headings = [name for name, commands in GROUPS] + ['Old names']
    positions = [text.index(heading + ':') for heading in headings]
    assert positions == sorted(positions)
    for index, (heading, commands) in enumerate(GROUPS):
        section = text[positions[index] + len(heading) + 1:positions[index + 1]]
        shown = [item.strip() for item in section.replace('\n', ' ').split(',') if item.strip()]
        assert shown == commands
    old = text[positions[-1]:]
    for name in ['target', 'policy', 'operation routing', 'task', 'release', 'campaign OPERATION']:
        assert name in old


def test_no_lab_command_reuses_an_evo_word_with_another_meaning():
    text = _help()
    assert 'Old names:' in text
    primary = text.split('Old names:', 1)[0]
    for name in ('task', 'target', 'release', 'policy'):
        assert not any(name == token.strip() for line in primary.splitlines()
                       for token in line.split(','))


@pytest.mark.parametrize('old,new', [
    (['target', 'list'], ['agent', 'list']),
    (['policy', 'list'], ['exec-policy', 'list']),
    (['operation', 'routing', 'operation'], ['routing', 'operation']),
    (['task', 'status', 'job', '--generation', 'generation'], ['job', 'status', 'job', '--generation', 'generation']),
    (['release', 'operation', 'operation', '--reason', 'confirmed cleanup'],
     ['release-operation', 'operation', '--reason', 'confirmed cleanup']),
    (['campaign', 'operation'], ['view', 'operation']),
    (['target', 'probe', 'list', 'project'], ['probe', 'list', 'project']),
])
def test_every_old_name_still_runs(old, new):
    from test_service_cli import FakeClient, invoke
    clients = [FakeClient([{'schema': 'test/1'}]), FakeClient([{'schema': 'test/1'}])]
    options = ['--json', *(['--no-request-id'] if old[0] == 'release' else [])]
    results = [invoke([*options, *argv], client)
               for argv, client in zip((old, new), clients)]
    assert all(code == 0 for code, stdout, stderr in results), results
    assert results[0][1] == results[1][1]
    assert clients[0].calls == clients[1].calls
