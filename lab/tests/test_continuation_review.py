"""Regressions for the approved PR3 review correction pass."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.slow


@pytest.mark.parametrize('result_state', ['unavailable', 'not_created', 'present'])
def test_champion_selection_refuses_an_unavailable_predecessor(tmp_path, result_state):
    from evolution.tree import scaffold, load_manifest_v2
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.linked import campaign_manifest
    sample = scaffold(tmp_path / 'source')
    binding = SimpleNamespace(manifest_path=sample.manifest, manifest=load_manifest_v2(sample.manifest))
    predecessor = tmp_path / 'artifacts/prior/prior'; predecessor.mkdir(parents=True)
    # An existing Core journal may have finished even when Lab has no usable
    # projection. Its mere presence cannot justify using project seed bytes.
    (predecessor / 'campaign.jsonl').write_text('unreadable journal')
    operation = tmp_path / 'successor'; operation.mkdir()
    scope = operation / 'scope'; scope.mkdir()
    run = operation / 'run'; run.mkdir()
    runtime = SimpleNamespace(paths=SimpleNamespace(operation_directory=operation,
        scope_directory=scope, run_directory=run), baseline_import=None, linked={'operation_id': 'prior', 'baseline': 'inherit', 'seed': 'champion'})
    async def db(*args):
        return SimpleNamespace(results_state=result_state, result=None)
    async def work(fn, lease):
        return await fn()
    owner = SimpleNamespace(state_directory=tmp_path, _db=db, _result_work=work,
        _result_io=SimpleNamespace(acquire_read=lambda *args: None))
    with pytest.raises(OwnerError) as error:
        asyncio.run(campaign_manifest(owner, runtime, binding))
    assert error.value.code == 'artifacts_invalid'
    assert not (scope / 'campaign-manifest.json').exists()


def _suspended_project(tmp_path):
    from evolution_lab.projects.demo import campaign_project
    from test_quickstart_campaign import _campaign
    project = campaign_project(tmp_path / 'project')
    flag = tmp_path / 'unavailable'; flag.write_text('')
    agent = project / 'agent/my_agent.py'
    agent.write_text(agent.read_text().replace('    def answer(self, question):',
        '    def answer(self, question):\n'
        f'        from pathlib import Path\n        if Path({str(flag)!r}).exists():\n'
        '            raise OSError("temporary test fault")'))
    manifest = project / 'manifest.json'
    raw = json.loads(manifest.read_text())
    raw['campaign']['sample_retries'] = 0
    raw['campaign']['budget']['max_failed_turns'] = 1
    for limits in raw['campaign']['indeterminate'].values():
        limits.update(max_count=1, max_rate='1')
    manifest.write_text(json.dumps(raw))
    first = _campaign(project)
    assert first.summary['campaign']['suspended'] is True, first.summary
    assert first.exit_code == 4
    return project, flag, first


@pytest.mark.parametrize('field', ['policy', 'adaptation'])
def test_project_reprepares_changed_inputs_then_explicitly_accepts(tmp_path, field):
    from test_quickstart_campaign import _campaign
    from evolution_lab.owner import Accepted
    project, flag, first = _suspended_project(tmp_path)
    flag.unlink()
    manifest = project / 'manifest.json'; raw = json.loads(manifest.read_text())
    if field == 'policy':
        raw['campaign']['sample_retries'] = 2
    else:
        raw['reflector']['max_consecutive_failures'] += 1
    manifest.write_text(json.dumps(raw))
    refused = _campaign(project, continue_run=True, reprepare=True)
    assert refused.exit_code == 5, refused.summary
    op = first.summary['campaign']['operation_id']
    assert refused.summary['campaign']['operation_id'] == op
    assert first.summary['prepared']['digest'] != refused.summary['prepared']['digest']
    record = json.loads((refused.run_dir / 'refusal.json').read_text())
    findings = [f for f in record['findings'] if f['class'] == 'authorizable']
    assert any(field in str(f['differs']) for f in findings), record
    done = _campaign(project, continue_run=True,
        accept=[Accepted(f['code'], f['instance_digest']) for f in findings], reason='repair tolerance')
    assert done.exit_code == 0, done.summary
    assert done.summary['campaign']['operation_id'] == op
    history = json.loads((done.run_dir / 'inputs.json').read_text())
    assert [r['prepared_digest'] for r in history['starts']] == [first.summary['prepared']['digest'],
        refused.summary['prepared']['digest'], refused.summary['prepared']['digest']]
    from evolution_lab.quickstart._readout import render_campaign_readout
    text = render_campaign_readout(done.summary, done.run_dir)
    assert first.summary['prepared']['digest'] in text and refused.summary['prepared']['digest'] in text


def test_project_cli_selects_reprepare_explicitly(tmp_path, monkeypatch):
    import io
    import evolution_lab.quickstart._campaign as api
    from evolution_lab.cli import main
    (tmp_path / 'campaign-launch.json').write_text('{}')
    seen = {}
    async def campaign(project, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(exit_code=0, summary={'done': True}, run_dir=tmp_path)
    monkeypatch.setattr(api, 'campaign', campaign)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(['campaign', str(tmp_path), '--continue', '--reprepare', '--wheel', 'lab.whl',
                 '--wheel', 'core.whl', '--json'], stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    assert seen['continue_run'] is True and seen['reprepare'] is True


def test_reprepare_and_prepared_are_exclusive_before_work(tmp_path):
    from evolution_lab.quickstart._campaign import campaign
    from evolution_lab.quickstart import QuickstartError
    with pytest.raises(QuickstartError) as error:
        asyncio.run(campaign(tmp_path, wheels=(), continue_run=True, reprepare=True, prepared='a' * 64))
    assert error.value.code == 'invalid_argument'


@pytest.mark.parametrize('explicit_python', [False, True])
def test_continuation_keeps_the_saved_target_python(tmp_path, monkeypatch, explicit_python):
    import io
    import shlex
    import subprocess
    import sys
    from tempfile import TemporaryDirectory
    from evolution_lab.cli import main
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    import test_quickstart_campaign as helpers
    with TemporaryDirectory(prefix='wave2-target-python-') as scratch:
        target = Path(scratch) / 'target/bin/python'
        subprocess.run(['uv', 'venv', '--python', sys.executable, str(target.parent.parent)],
                       check=True, capture_output=True, text=True)
        subprocess.run(['uv', 'pip', 'install', '--offline', '--python', str(target),
                        str(_installed_lab_wheel()) + '[service]', str(CORE_WHEEL) + '[tree]'],
                       check=True, capture_output=True, text=True)
        original = helpers._campaign
        def separate_target(project, **kwargs):
            return original(project, python=target, **kwargs)
        monkeypatch.setattr(helpers, '_campaign', separate_target)
        project, flag, first = _suspended_project(tmp_path)
        assert first.summary['environment']['python'] == str(target)
        second = original(project, continue_run=True, **({'python': target} if explicit_python else {}))
        assert second.exit_code in {4, 5}, second.summary
        command = shlex.split(second.summary['next_command'])
        assert command[command.index('--python') + 1] == str(target)
        assert second.summary['environment']['python'] == str(target)
        assert second.summary['resolved']['python'] == str(target)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(command[1:], stdout=stdout, stderr=stderr)
        assert code in {4, 5}, stderr.getvalue()
        assert 'argument_invalid' not in stderr.getvalue()
