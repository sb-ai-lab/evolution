"""ADR-0028: fixed evaluation inputs, rather than campaign choices, define reuse."""
from __future__ import annotations

import ast
from dataclasses import replace
import inspect
import json
from pathlib import Path

import pytest

from evolution_lab.preparation import PreparationStore
from evolution_lab.preparation.models import BundleEntry
from evolution_lab.preparation.storage import PreparationStorage
from test_preparation_bundle import fixture
from test_preparation_evaluation import _evaluation_request
from test_preparation_probe import _probe_request


def _basis(stored):
    from evolution_lab.preparation.basis import measurement_basis
    return measurement_basis(stored)


def _rewrite(path, route, value):
    document = json.loads(path.read_text())
    section = document
    for key in route[:-1]:
        section = section[key]
    section[route[-1]] = value
    path.write_text(json.dumps(document))


def _campaign(tmp_path, monkeypatch):
    module, root, request = fixture(tmp_path, monkeypatch)
    storage = PreparationStorage(tmp_path / 'state')
    return module, root, request, storage, module.create_bundle(storage, **request)


@pytest.mark.parametrize(('route', 'value'), [
    (('environment', 'input_text_key'), 'other_text'),
    (('scorer', 'timeout_seconds'), 31),
    (('seed_root',), 'seed-other'),
    (('target', 'timeouts'), {'connect_seconds': 2, 'read_seconds': 40}),
    (('artifacts', 'excludes'), ['ignored.txt']),
    (('corpus', 'path'), 'other-corpus.jsonl'),
    (('schema',), 'evolution-v2-manifest/16'),
])
def test_included_manifest_part_changes_basis(tmp_path, monkeypatch, route, value):
    module, root, request, storage, before = _campaign(tmp_path, monkeypatch)
    # All changed inputs remain valid under the pinned public Core loader.
    if route == ('seed_root',):
        import shutil
        shutil.copytree(root / 'seed', root / 'seed-other')
        # Keep inventories equal: this pre-existing alternate seed is part of both Bundles.
        before = module.create_bundle(storage, **request)
    if route == ('corpus', 'path'):
        (root / value).write_bytes((root / 'corpus.jsonl').read_bytes())
        before = module.create_bundle(storage, **request)
    if route == ('schema',):
        from scaffold_16 import as_manifest_16
        path = root / 'manifest.json'
        path.write_text(json.dumps(as_manifest_16(json.loads(path.read_text()))))
    else:
        _rewrite(root / 'manifest.json', route, value)
    after = module.create_bundle(storage, **request)
    assert _basis(before) is not None
    assert _basis(after) is not None
    assert _basis(after) != _basis(before)


@pytest.mark.parametrize(('route', 'value'), [
    (('campaign', 'sample_retries'), 2),
    (('campaign', 'indeterminate', 'target', 'max_count'), 19),
    (('campaign', 'call_retries', 'max_attempts'), 5),
    (('campaign', 'rounds'), 3),
    (('campaign', 'max_concurrency'), 1),
    (('campaign', 'budget', 'max_failed_turns'), 1),
    (('editor', 'argv'), ['python3', 'edit.py', '--model=alternate']),
    (('reflector', 'max_consecutive_failures'), 4),
    (('campaign', 'gate', 'focus', 'margin'), 0.1),
])
def test_campaign_choices_change_bundle_but_preserve_basis(tmp_path, monkeypatch, route, value):
    module, root, request, storage, before = _campaign(tmp_path, monkeypatch)
    # Keep interpreter relocation valid when changing editor's model argument.
    if route == ('editor', 'argv'):
        value = list(value)
        value[0] = json.loads((root / 'manifest.json').read_text())['editor']['argv'][0]
    _rewrite(root / 'manifest.json', route, value)
    after = module.create_bundle(storage, **request)
    assert after.digest != before.digest
    assert _basis(before) is not None
    assert _basis(after) == _basis(before)


@pytest.mark.parametrize(('name', 'value', 'included'), [
    ('max_instances', 8, False),
    ('max_concurrent_sessions', 2, False),
    ('max_output_bytes', 8192, True),
    ('max_input_bytes', 8192, True),
])
def test_launch_capacity_only_is_excluded(tmp_path, monkeypatch, name, value, included):
    module, root, request, storage, before = _campaign(tmp_path, monkeypatch)
    _rewrite(root / 'launch.json', ('limits', name), value)
    after = module.create_bundle(storage, **request)
    assert after.digest != before.digest
    assert _basis(before) is not None
    assert (_basis(after) != _basis(before)) is included


def test_unknown_launch_limit_is_conservatively_included(tmp_path, monkeypatch):
    _, _, _, _, before = _campaign(tmp_path, monkeypatch)
    # Future launch keys are conservatively included without requiring today's closed launch reader to admit them.
    path = before.path / before.descriptor.launch
    path.chmod(0o600)
    _rewrite(path, ('limits', 'future_limit'), 5)
    first = _basis(before)
    _rewrite(path, ('limits', 'future_limit'), 6)
    assert first is not None
    assert _basis(before) != first


@pytest.mark.parametrize('part', ['corpus_byte', 'script_bytes', 'script_executable', 'extra_file', 'editor_brief'])
def test_inventory_inputs_change_basis(tmp_path, monkeypatch, part):
    module, root, request, storage, before = _campaign(tmp_path, monkeypatch)
    if part == 'corpus_byte':
        path = root / 'corpus.jsonl'
        path.write_bytes(path.read_bytes().replace(b'text', b'texT', 1))
    elif part in {'script_bytes', 'script_executable'}:
        path = root / 'scorer' / 'score.py'
        if part == 'script_bytes':
            path.write_bytes(path.read_bytes() + b'\n# changed scorer\n')
        else:
            path.chmod(path.stat().st_mode ^ 0o100)
    else:
        (root / ('editor-brief.txt' if part == 'editor_brief' else 'future-input')).write_text('extra input')
    after = module.create_bundle(storage, **request)
    assert _basis(before) is not None
    assert _basis(after) != _basis(before)


@pytest.mark.parametrize(('field', 'value'), [
    ('path', 'renamed-file'), ('type', 'directory'), ('executable', 0o100),
    ('size', 1), ('sha256', 'd' * 64),
])
def test_each_inventory_metadata_field_is_included(tmp_path, monkeypatch, field, value):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    entry = next(e for e in stored.descriptor.entries if e.type == 'file' and e.path not in {
        stored.descriptor.entrypoint, stored.descriptor.launch,
    })
    if field == 'type':
        changed = BundleEntry(entry.path, 'directory', entry.executable, None, None)
    else:
        changed = replace(entry, **{field: value})
    descriptor = replace(stored.descriptor, entries=tuple(changed if e == entry else e for e in stored.descriptor.entries))
    assert _basis(stored) is not None
    assert _basis(replace(stored, descriptor=descriptor)) != _basis(stored)


@pytest.mark.parametrize('part', ['env', 'secret_names', 'environment', 'conformance_entrypoint'])
def test_descriptor_inputs_change_basis(tmp_path, monkeypatch, part):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    if part == 'env':
        changed = replace(stored.descriptor, env={'MODE': 'other'})
    elif part == 'secret_names':
        changed = replace(stored.descriptor, secret_names=('TOKEN', 'NEW_ROLE_SECRET'))
    elif part == 'environment':
        changed = replace(stored.descriptor, environments=(replace(stored.descriptor.environments[0], python_version='3.12.14'),))
    else:
        # Campaign descriptors require equal entrypoint/conformance paths: relabel both, keeping logical bytes.
        alias = stored.path / stored.descriptor.entrypoint
        alias = alias.with_name('manifest-alias.json')
        alias.parent.chmod(0o700)
        alias.write_bytes((stored.path / stored.descriptor.entrypoint).read_bytes())
        logical = alias.relative_to(stored.path).as_posix()
        entries = tuple(replace(e, path=logical) if e.path == stored.descriptor.entrypoint else e for e in stored.descriptor.entries)
        changed = replace(stored.descriptor, entrypoint=logical, conformance_entrypoint=logical, entries=entries)
    assert _basis(stored) is not None
    assert _basis(replace(stored, descriptor=changed)) != _basis(stored)


@pytest.mark.parametrize('kind', ['evaluation', 'probe'])
def test_other_source_kinds_cover_input_and_internal_manifest(tmp_path, monkeypatch, kind):
    if kind == 'evaluation':
        store = PreparationStore(tmp_path / 'state')
        request = _evaluation_request(tmp_path, monkeypatch)
    else:
        store, request = _probe_request(tmp_path, monkeypatch)
    summary = store.create_bundle(**request)
    stored = store._storage.get_bundle(summary.digest)
    original = _basis(stored)
    assert original is not None
    assert summary.measurement_basis == original
    input_path = stored.path / stored.descriptor.entrypoint
    input_path.chmod(0o600)
    raw = input_path.read_bytes()
    document = json.loads(raw)
    if kind == 'evaluation':
        document['defaults']['max_turns'] += 1
    else:
        document['env']['A'] = '2'
    input_path.write_text(json.dumps(document))
    assert _basis(stored) != original
    input_path.write_bytes(raw)
    internal = stored.path / stored.descriptor.conformance_entrypoint
    internal.chmod(0o600)
    _rewrite(internal, ('target', 'base_url'), 'http://127.0.0.1:6001')
    assert _basis(stored) != original


def test_source_kind_discriminates_identical_documents(tmp_path, monkeypatch):
    store = PreparationStore(tmp_path / 'state')
    summary = store.create_bundle(**_evaluation_request(tmp_path, monkeypatch))
    stored = store._storage.get_bundle(summary.digest)
    other = replace(stored, descriptor=replace(stored.descriptor, source_kind='probe'))
    assert _basis(stored) is not None
    assert _basis(other) != _basis(stored)


def test_basis_survives_reads_and_new_store_without_mutating_bundle(tmp_path, monkeypatch):
    module, _, request, storage, stored = _campaign(tmp_path, monkeypatch)
    snapshot = {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob('*') if p.is_file()}
    first = PreparationStore(storage.state_directory).get_bundle(stored.digest)
    second = PreparationStore(storage.state_directory).get_bundle(stored.digest)
    assert first.schema == 'lab-bundle-summary/3'
    assert first.measurement_basis is not None
    assert first.to_payload() == second.to_payload()
    assert first.measurement_basis == _basis(stored) == _basis(storage.get_bundle(stored.digest))
    assert snapshot == {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob('*') if p.is_file()}


def test_unloadable_manifest_has_no_basis(tmp_path, monkeypatch):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    path = stored.path / stored.descriptor.entrypoint
    path.chmod(0o600)
    _rewrite(path, ('schema',), 'evolution-v2-manifest/future')
    assert _basis(stored) is None
    assert stored.summary().measurement_basis is None


def test_fixed_evaluation_manifest_read_set_is_covered():
    """ADR-0028: every new consumer read requires expanding basis and a mutation test.

    This independent list describes public Core attributes, not basis implementation constants.
    The corresponding saved fields are environment/scorer, seed_root, target and artifacts;
    corpus and schema additionally enter via campaign_cases/is_campaign_manifest.
    """
    from evolution_lab.preparation.evaluation import bind_campaign_evaluation, confirm_schedule
    from evolution_lab.owner import _evaluation_worker
    sources = [inspect.getsource(bind_campaign_evaluation), inspect.getsource(confirm_schedule), inspect.getsource(_evaluation_worker)]
    accessed = {node.attr for source in sources for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'manifest'}
    assert accessed <= {'workflow', 'seed_root', 'target', 'policy'}
    workflow_accessed = {
        node.attr for source in sources for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and (
            (isinstance(node.value, ast.Name) and node.value.id == 'workflow')
            or (isinstance(node.value, ast.Attribute) and node.value.attr == 'workflow'
                and isinstance(node.value.value, ast.Name) and node.value.value.id == 'manifest')
        )
    }
    assert workflow_accessed <= {'environment', 'scorer'}


@pytest.mark.parametrize(('route', 'value'), [
    (('env', 'MODE'), 'changed'),
    (('secret_names',), ['TOKEN', 'OTHER']),
    (('startup_timeout_seconds',), 12),
    (('command', 'args'), [{'literal': 'different-launch'}]),
])
def test_launch_noncapacity_inputs_are_included(tmp_path, monkeypatch, route, value):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    original = _basis(stored)
    path = stored.path / stored.descriptor.launch
    path.chmod(0o600)
    _rewrite(path, route, value)
    assert original is not None
    assert _basis(stored) != original


def test_corpus_split_labels_are_included(tmp_path, monkeypatch):
    module, root, request, storage, before = _campaign(tmp_path, monkeypatch)
    corpus = root / 'corpus.jsonl'
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    for row in rows:
        row['split'] = 'train'
    corpus.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    after = module.create_bundle(storage, **request)
    assert _basis(before) is not None
    assert _basis(after) != _basis(before)


def test_projected_documents_ignore_formatting_and_inventory_metadata(tmp_path, monkeypatch):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    original = _basis(stored)
    for logical in (stored.descriptor.entrypoint, stored.descriptor.launch):
        path = stored.path / logical
        path.chmod(0o600)
        path.write_text(json.dumps(json.loads(path.read_text()), indent=4, sort_keys=True))
    entries = tuple(replace(e, size=1, executable=0o100, sha256='e' * 64)
                    if e.path in {stored.descriptor.entrypoint, stored.descriptor.launch} else e
                    for e in stored.descriptor.entries)
    changed = replace(stored, descriptor=replace(stored.descriptor, entries=entries))
    assert original is not None
    assert _basis(changed) == original


@pytest.mark.parametrize('basis', [None, 'a' * 64])
def test_summary_3_cli_decoder_preserves_nullable_basis(tmp_path, monkeypatch, basis):
    from evolution_lab.cli.responses import validate
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    payload = stored.summary().to_payload()
    payload['measurement_basis'] = basis
    assert validate(payload, 'lab-bundle-summary/3').to_payload() == payload


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'invalid_basis', 'legacy_schema'])
def test_summary_3_cli_decoder_is_closed(tmp_path, monkeypatch, mutation):
    from evolution_lab.cli.responses import validate
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    payload = stored.summary().to_payload()
    if mutation == 'missing':
        del payload['measurement_basis']
    elif mutation == 'extra':
        payload['future'] = True
    elif mutation == 'invalid_basis':
        payload['measurement_basis'] = 'wrong'
    else:
        payload['schema'] = 'lab-bundle-summary/2'
    with pytest.raises(ValueError):
        validate(payload, 'lab-bundle-summary/3')


@pytest.mark.asyncio
async def test_http_basis_is_stable_after_preparation_service_restart(tmp_path, monkeypatch):
    """Recreate service runtime, executor, app and preparation store; no target or Owner work is needed."""
    from concurrent.futures import ThreadPoolExecutor
    import httpx
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

    _, _, _, storage, stored = _campaign(tmp_path, monkeypatch)
    replies = []
    for _ in range(2):
        runtime = ServiceRuntime(ServiceConfig(tmp_path / 's.sock', storage.state_directory))
        runtime.preparation = PreparationStore(storage.state_directory)
        runtime._admission_open = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            runtime._executor = executor
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
                for _ in range(2):
                    reply = await client.get(f'/v1/bundles/{stored.digest}')
                    assert reply.status_code == 200, reply.text
                    replies.append(reply.json())
    assert replies[0]['measurement_basis'] is not None
    assert all(reply == replies[0] for reply in replies)


@pytest.mark.parametrize('kind', ['evaluation', 'probe'])
def test_schema_less_internal_manifest_accepted_by_public_loader_has_basis(tmp_path, monkeypatch, kind):
    """dev58 retained pre-dev57 no-schema internal manifests; use the same public reader boundary."""
    from evolution_lab.preparation.internal_manifest import load_internal_manifest
    if kind == 'evaluation':
        store = PreparationStore(tmp_path / 'state')
        request = _evaluation_request(tmp_path, monkeypatch)
    else:
        store, request = _probe_request(tmp_path, monkeypatch)
    stored = store._storage.get_bundle(store.create_bundle(**request).digest)
    path = stored.path / stored.descriptor.conformance_entrypoint
    path.chmod(0o600)
    document = json.loads(path.read_text())
    del document['schema']
    path.write_text(json.dumps(document))
    assert load_internal_manifest(path).workflow is None
    assert _basis(stored) is not None


def test_directory_metadata_is_included(tmp_path, monkeypatch):
    _, _, _, _, stored = _campaign(tmp_path, monkeypatch)
    empty = next(entry for entry in stored.descriptor.entries if entry.path.endswith('/empty'))
    changed = replace(empty, executable=empty.executable ^ 0o100)
    descriptor = replace(stored.descriptor, entries=tuple(changed if e == empty else e for e in stored.descriptor.entries))
    assert _basis(stored) is not None
    assert _basis(replace(stored, descriptor=descriptor)) != _basis(stored)
