"""Project trace uses saved campaign names and the caller's secret environment."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_owner_binding import CORE_WHEEL, _installed_lab_wheel

pytestmark = pytest.mark.slow


@pytest.fixture
def secret_project(tmp_path):
    from evolution_lab.projects.demo import campaign_project
    from evolution_lab.quickstart._campaign import campaign
    project = campaign_project(tmp_path / 'project')
    secret = 'trace-project-private-sentinel-73951'
    manifest = project / 'manifest.json'
    doc = json.loads(manifest.read_text())
    doc['scorer']['allowed_env'] = ['TRACE_PROJECT_KEY']
    manifest.write_text(json.dumps(doc))
    corpus = project / 'corpus.jsonl'
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    for row in rows:
        row['oracle']['private_trace_marker'] = secret
    corpus.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = asyncio.run(campaign(project, wheels=(_installed_lab_wheel(), CORE_WHEEL),
                                  environ={'TRACE_PROJECT_KEY': secret}))
    assert result.summary['campaign'] is not None, result.summary
    state = Path(result.summary['state'])
    operation = result.summary['campaign']['operation_id']
    # Trace must resolve saved names, even after the editable project changes.
    doc['scorer']['allowed_env'] = ['EDITED_PROJECT_KEY']
    manifest.write_text(json.dumps(doc))
    return project, secret, state, operation, result.summary['prepared']['digest']


def _trace(project, out, *extra):
    from evolution_lab.cli import main
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(['--json', 'trace', str(project), *extra, '--out', str(out), '--disclose-private'],
                stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_dir_trace_masks_environment_secret_and_equals_core(secret_project, tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.binding import open_prepared
    project, secret, state, operation, prepared = secret_project
    monkeypatch.setenv('TRACE_PROJECT_KEY', secret)
    async def binding():
        async with Owner.open(state) as owner:
            status = await owner.status(ResourceRef('operation', operation))
            assert status.prepared_digest == prepared
            return open_prepared(owner._storage, prepared).bundle.manifest_path
    manifest = asyncio.run(binding())
    run = state / 'artifacts' / operation / operation
    reference = tmp_path / 'core.jsonl'
    completed = subprocess.run([str(Path(sys.executable).parent / 'evo'), 'tree', 'trace',
        '--run-dir', str(run), '--manifest', str(manifest), '--out', str(reference)],
        env=dict(os.environ), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert secret not in reference.read_text()
    unmasked = tmp_path / 'core-without-secret.jsonl'
    control = subprocess.run([str(Path(sys.executable).parent / 'evo'), 'tree', 'trace',
        '--run-dir', str(run), '--manifest', str(manifest), '--out', str(unmasked)],
        env={}, capture_output=True, text=True)
    assert control.returncode == 0, control.stderr
    assert secret in unmasked.read_text()  # This fixture really exercises masking.
    for index, selected in enumerate([[], [operation]]):
        out = tmp_path / f'project-{index}.jsonl'
        code, stdout, stderr = _trace(project, out, *selected)
        assert code == 0, stderr
        summary = json.loads(stdout)
        assert summary['tries'] > 0 and summary['unset_secrets'] == 0
        assert secret not in out.read_text()
        assert out.read_bytes() == reference.read_bytes()


def test_dir_trace_missing_secret_refuses_before_output(secret_project, tmp_path, monkeypatch):
    project, secret, _, _, _ = secret_project
    monkeypatch.delenv('TRACE_PROJECT_KEY', raising=False)
    out = tmp_path / 'missing.jsonl'
    code, stdout, stderr = _trace(project, out)
    assert code != 0
    assert json.loads(stderr)['error']['code'] == 'secret_missing', stderr
    assert not out.exists()
    assert secret not in stdout + stderr


@pytest.mark.parametrize('source', ['file', 'fd'])
def test_dir_trace_explicit_secret_overrides_environment(secret_project, tmp_path, monkeypatch, source):
    project, secret, _, operation, _ = secret_project
    if source == 'file':
        monkeypatch.setenv('TRACE_PROJECT_KEY', 'different-shell-value-7348')
    else:
        monkeypatch.delenv('TRACE_PROJECT_KEY', raising=False)
    path = tmp_path / 'secrets.json'
    path.write_text(json.dumps({'TRACE_PROJECT_KEY': secret}))
    out = tmp_path / 'explicit.jsonl'
    with path.open() as stream:
        options = ['--secrets-file', str(path)] if source == 'file' else ['--secrets-fd', str(stream.fileno())]
        code, stdout, stderr = _trace(project, out, operation, *options)
    assert code == 0, stderr
    assert json.loads(stdout)['unset_secrets'] == 0
    assert secret not in out.read_text()


def test_dir_trace_keeps_evaluation_refusal_before_secret_binding(tmp_path):
    from evolution_lab.owner.store import OwnerStateStore
    from evolution_lab.quickstart._evaluate import installed_versions, pair_label
    from test_owner_store_starts import _executor, _ready_target, DIGEST
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.lab').mkdir(mode=0o700)
    state = project / '.lab' / f'state-{pair_label(*installed_versions())}'
    with OwnerStateStore.open(state) as store:
        operation = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store),
            executor=_executor('lab-evaluation-worker/1'), kind='evaluation')
    out = tmp_path / 'evaluation.jsonl'
    code, stdout, stderr = _trace(project, out, operation.status.id)
    assert code == 3
    assert json.loads(stderr)['error']['code'] == 'trace_not_a_campaign', stderr
    assert not out.exists()
