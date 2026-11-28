"""Lab delegates private campaign trace bytes and authentication to Core."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from test_owner_core_raw import finished_campaign

pytestmark = pytest.mark.slow


def test_lab_trace_equals_core_export(finished_campaign, tmp_path):
    from evolution.tree import TraceSummary
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.binding import open_prepared
    state, operation_id, _ = finished_campaign
    out = tmp_path / 'lab.jsonl'
    async def scenario():
        async with Owner.open(state) as owner:
            ref = ResourceRef('operation', operation_id)
            status = await owner.status(ref)
            binding = open_prepared(owner._storage, status.prepared_digest)
            summary = await owner.trace(ref, out=out, secrets={}, disclose_private=True)
            assert isinstance(summary, TraceSummary)
            return summary, binding.bundle.manifest_path, state / 'artifacts' / operation_id / operation_id
    summary, manifest, run = asyncio.run(scenario())
    reference = tmp_path / 'core.jsonl'
    result = subprocess.run([str(Path(sys.executable).parent / 'evo'), 'tree', 'trace',
        '--run-dir', str(run), '--manifest', str(manifest), '--out', str(reference)],
        env={}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == reference.read_bytes()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert summary.tries > 0
    assert f'{summary.tries} tries written' in result.stderr


def test_trace_requires_disclose_private(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    out = tmp_path / 'private.jsonl'
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            with pytest.raises(OwnerError) as error:
                await owner.trace(ResourceRef('operation', 'missing'), out=out,
                                  secrets={}, disclose_private=False)
            assert error.value.code == 'private_disclosure_required'
    asyncio.run(scenario())
    assert not out.exists()


def test_trace_refuses_evaluation_operations(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_store_starts import _executor, _ready_target, DIGEST
    state = tmp_path / 'state'
    with OwnerStateStore.open(state) as store:
        operation = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store),
            executor=_executor('lab-evaluation-worker/1'), kind='evaluation')
    async def scenario():
        async with Owner.open(state) as owner:
            with pytest.raises(OwnerError) as error:
                await owner.trace(ResourceRef('operation', operation.status.id), out=tmp_path / 'out.jsonl',
                                  secrets={}, disclose_private=True)
            assert error.value.code == 'trace_not_a_campaign'
    asyncio.run(scenario())
    assert not (tmp_path / 'out.jsonl').exists()


def test_trace_redacts_campaign_secrets(tmp_path):
    from evolution.tree import load_manifest_v2, manifest_dependencies
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from scaffold_15 import scaffold_15
    from test_owner_target import make_bundle, ready_target
    from test_owner_cleanup import finished
    secret = 'secret-trace-sentinel-1737'
    def with_secret(path):
        sample = scaffold_15(path)
        document = json.loads(sample.manifest.read_text())
        document['scorer']['allowed_env'] = ['TRACE_SECRET']
        sample.manifest.write_text(json.dumps(document))
        corpus = next(item.resolved for item in manifest_dependencies(
            load_manifest_v2(sample.manifest)).dependencies if item.field == 'corpus.path')
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        for row in rows:
            oracle = row['oracle']
            row['oracle'] = {**oracle, 'trace_secret': secret} if isinstance(oracle, dict) else f'{oracle} {secret}'
        corpus.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return sample
    state, bundle, _ = make_bundle(tmp_path, scaffold=with_secret, bundle_secret_names=('TRACE_SECRET',))
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    out = tmp_path / 'redacted.jsonl'
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target, secrets={'TRACE_SECRET': secret})
            ref = ResourceRef('operation', accepted.id)
            done = await finished(owner, ref, seconds=120)
            assert done.result is not None, done.to_payload()
            summary = await owner.trace(ref, out=out, secrets={'TRACE_SECRET': secret}, disclose_private=True)
            assert summary.tries > 0 and summary.unset_secrets == 0
    asyncio.run(scenario())
    assert secret not in out.read_text()


def test_cancelled_trace_caller_keeps_the_result_lease(finished_campaign, tmp_path, monkeypatch):
    import threading
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    import evolution_lab.owner.traces as traces
    entered, release = threading.Event(), threading.Event()
    original = traces.export_run
    def blocked(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    monkeypatch.setattr(traces, 'export_run', blocked)
    state, operation_id, _ = finished_campaign
    out = tmp_path / 'cancelled-caller.jsonl'
    async def scenario():
        async with Owner.open(state) as owner:
            ref = ResourceRef('operation', operation_id)
            task = asyncio.create_task(owner.trace(ref, out=out, secrets={}, disclose_private=True))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(OwnerError) as error:
                    await owner.delete_results(ref)
                assert error.value.code == 'resource_busy'
            finally:
                release.set()
    asyncio.run(scenario())
    assert out.is_file() and stat.S_IMODE(out.stat().st_mode) == 0o600
