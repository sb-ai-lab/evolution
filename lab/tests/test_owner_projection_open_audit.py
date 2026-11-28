"""Audit real Core/Lab projection IO against the metadata cache's signed inputs."""
import asyncio
import json
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import sysconfig
import threading

import pytest
from platforms import fd_path

from evolution_lab.owner import readers
# Eager code imports keep the measured source-mode phase identical to installed
# mode without exempting source/data paths from the signed-input rule.
from evolution_lab import _outcomes  # noqa: F401
from evolution_lab.owner import execution  # noqa: F401
from test_owner_projection_completeness import campaign, evaluation, project  # noqa: F401


@contextmanager
def opened_paths(monkeypatch):
    """Retain every open, including nofollow traversal and dir_fd-relative leaves.

    Audit hooks cannot be removed, so disable the hook after this context. A
    thread-local wrapper supplies the dir_fd missing from CPython's open event.
    Both lexical and resolved paths are retained: symlink targets are inputs too.
    """
    records = []
    active = True
    context = threading.local()
    original = os.open

    def hook(event, args):
        if not active or event != 'open':
            return
        path, mode, flags = args
        if isinstance(path, int):
            path = fd_path(path)
        path = Path(os.fsdecode(path))
        if not path.is_absolute():
            path = getattr(context, 'base', Path.cwd()) / path
        records.append((path.absolute(), path.resolve(), flags))

    def open_at(path, flags, mode=0o777, *, dir_fd=None):
        previous = getattr(context, 'base', None)
        context.base = (Path(fd_path(dir_fd))
                        if dir_fd is not None else Path.cwd())
        try:
            return original(path, flags, mode, dir_fd=dir_fd)
        finally:
            if previous is None:
                del context.base
            else:
                context.base = previous

    sys.addaudithook(hook)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', open_at)
        try:
            yield records
        finally:
            active = False


def signed_files(root, operation_id, scope):
    signature = readers.run_signature(root, operation_id, scope)
    assert signature is not None
    run = root / 'artifacts' / operation_id / operation_id
    files = {run / row[0] for row in signature[0]
             if len(row) > 2 and row[1] != stat.S_IFDIR}
    if scope is not None:
        files.add(scope / 'evaluation-outcome.json')
    return files


def outside_inputs(records, signed):
    interpreter = Path(sysconfig.get_path('stdlib')).resolve()
    sites = [Path(entry).resolve() for entry in sys.path if 'site-packages' in entry]
    unexpected = []
    for lexical, resolved, flags in records:
        # Ancestor directory handles are traversal, not content reads. Require
        # O_DIRECTORY explicitly; never exempt a regular file by its location.
        if flags & os.O_DIRECTORY:
            continue
        if resolved.is_relative_to(interpreter) or any(resolved.is_relative_to(site) for site in sites):
            continue
        if lexical not in signed or resolved not in signed:
            unexpected.append((str(lexical), str(resolved), flags))
    return unexpected


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_projection_opens_only_signed_inputs(campaign, evaluation, monkeypatch, kind):
    root, status, _, *scopes = evaluation if kind == 'evaluation' else campaign
    scope = scopes[0] if scopes else None
    signed = signed_files(root, status.id, scope)
    # The plan is a prior content-addressed cache input. Measure exactly
    # project_operation + _views, including Core reads and measurement times.
    with opened_paths(monkeypatch) as records:
        value, complete = asyncio.run(project(root, status, scope))
    assert records
    assert complete and readers.cacheable(value, complete)
    assert not outside_inputs(records, signed), records
    assert any(path.name == ('campaign.jsonl' if kind == 'campaign' else 'events.jsonl')
               and not flags & os.O_DIRECTORY for path, _, flags in records)


def test_audit_resolves_dir_fd_and_rejects_symlink_data(tmp_path, monkeypatch):
    signed = tmp_path / 'signed'
    signed.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('external input')
    (signed / 'link').symlink_to(outside)
    with opened_paths(monkeypatch) as records:
        directory = os.open(signed, os.O_RDONLY | os.O_DIRECTORY)
        try:
            descriptor = os.open('link', os.O_RDONLY, dir_fd=directory)
            os.close(descriptor)
        finally:
            os.close(directory)
    assert outside_inputs(records, {signed / 'link'}) == [
        (str(signed / 'link'), str(outside), os.O_RDONLY | os.O_CLOEXEC)]


def test_reference_projection_open_audit(tmp_path, monkeypatch):
    from dev67_reference import reference_project
    from evolution_lab.owner import ResourceRef
    from evolution_lab.owner.measurements import MeasurementCache
    async def scenario():
        evidence = {}
        async with reference_project(tmp_path) as stand:
            owner = stand.owner
            for name, operation_id in stand.operation_ids.items():
                ref = ResourceRef('operation', operation_id)
                durable = await owner._db('operation_status', operation_id)
                scopes = await owner._db('resource_scopes', ref)
                scope = owner.state_directory / scopes[0]['root_relative'] if scopes else None
                signed = signed_files(owner.state_directory, operation_id, scope)
                owner._measurements = MeasurementCache()
                completeness = readers.ReadCompleteness()
                with opened_paths(monkeypatch) as records:
                    projected = await owner._result_thread(readers.project_operation,
                        owner.state_directory, durable, scope_directory=scope, completeness=completeness)
                    value = await owner._views(projected, completeness=completeness)
                assert completeness.complete and readers.cacheable(value, True), name
                assert not outside_inputs(records, signed), (name, records)
                files = [str(path) for path, _, flags in records if not flags & os.O_DIRECTORY]
                assert any(path.endswith('events.jsonl') for path in files), name
                assert any(path.endswith('campaign.jsonl' if name.startswith('campaign')
                                         else 'evaluation-outcome.json') for path in files), name
                evidence[name] = [dict(path=str(path), resolved=str(resolved), flags=flags,
                                       directory=bool(flags & os.O_DIRECTORY))
                                  for path, resolved, flags in records]
                print('OPEN_AUDIT', name, len(records), 'opens', len(set(files)), 'files')
        output = os.environ.get('LAB_PROJECTION_AUDIT_EVIDENCE')
        if output:
            Path(output).write_text(json.dumps(evidence, indent=2) + '\n')
    asyncio.run(scenario())


def test_baseline_reader_opens_only_signed_inputs(campaign, monkeypatch):
    from evolution_lab.owner import core_raw
    root, status, run = campaign
    snapshot = core_raw.campaign_read(run).view.snapshot
    signed = signed_files(root, status.id, None)
    with opened_paths(monkeypatch) as records:
        result = core_raw.baseline_provenance_state(run, snapshot)
    assert result['samples'] and result['known']
    assert not outside_inputs(records, signed)
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    assert not outside_inputs(records, signed - {journal}) == []
