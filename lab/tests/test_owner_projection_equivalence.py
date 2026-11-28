"""Real projection equivalence, IO faults and lifecycle invalidation (dev67 §7)."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
from platforms import fd_path

from evolution_lab.owner import Owner, ResourceRef, readers
from evolution_lab.owner.execution import OperationRuntime
from evolution_lab.owner.models import ProcessFacts
from evolution_lab.owner.progress import CampaignPlan
from dev67_reference import reference_project
from test_owner_projection_completeness import CAPTURE
from test_owner_store_starts import DIGEST, _executor, _finish_normally, _ready_target

pytestmark = pytest.mark.slow


@asynccontextmanager
async def real_operation(root, *, kind='campaign', finished=False):
    """Real Owner database and b15 captures; no patched projection or database."""
    async with Owner.open(root) as owner:
        target = await owner._loop.run_in_executor(owner._executor, _ready_target, owner._store)
        accepted = await owner._db('accept_operation', prepared_digest=DIGEST,
            target_ref=target, executor=_executor(f'lab-{kind}-worker/1'), kind=kind)
        ref = ResourceRef('operation', accepted.status.id)
        run = root / 'artifacts' / ref.id / ref.id
        scope = root / 'scopes' / accepted.scope_id
        scope.mkdir(parents=True, exist_ok=True)
        if kind == 'campaign':
            shutil.copytree(CAPTURE, run)
        else:
            from evolution.tree.evolve import derive_run_id
            shutil.copytree(CAPTURE / 'measurements/m-r00-champion', run / 'measurement')
            (scope / 'evaluation-outcome.json').write_text(json.dumps({
                'schema': 'lab-evaluation-outcome/3', 'operation_id': ref.id,
                'scope_id': scope.name, 'run_id': derive_run_id(run),
                'measurement_returned': True, 'coverage': {'expected': 2, 'scored': 2},
                'stopped_reason': None, 'unmeasured': [], 'min_coverage': .95,
                'evaluation_budget': {'max_failed_turns': 1, 'call_retries': None}}))
        if finished:
            await owner._loop.run_in_executor(owner._executor, _finish_normally,
                                             owner._store, ref.id, scope.name)
        else:
            await owner._db('mark_helper_possible', scope.name)
            await owner._db('mark_dispatch_possible', ref.id)
            await owner._db('mark_operation_running', ref.id)
            await owner._db('update_operation', ref.id, process=ProcessFacts(launched=True))
        owner._plan_cache[DIGEST] = CampaignPlan('neutral', 1, 1, 'score')
        yield SimpleNamespace(owner=owner, ref=ref, run=run, scope=scope, target=target)


async def fresh(owner, ref, *, acquired=None):
    owner._projection_cache.clear()
    async def work():
        return await owner._status_projection(ref, acquired=acquired)
    return await owner._result_work(work, owner._result_io.acquire_read(ref.id))


async def same_as_fresh(owner, ref):
    observed = await owner.status(ref)
    expected = await fresh(owner, ref)
    assert observed.to_payload() == expected.to_payload()
    return observed


def test_reference_status_and_acquired_are_field_for_field_equal(tmp_path):
    async def scenario():
        async with reference_project(tmp_path) as stand:
            for name, operation_id in stand.operation_ids.items():
                ref = ResourceRef('operation', operation_id)
                first = await stand.owner.status(ref)
                assert operation_id in stand.owner._projection_cache
                cached = stand.owner._projection_cache[operation_id]
                assert (await stand.owner.status(ref)).to_payload() == first.to_payload()
                assert stand.owner._projection_cache[operation_id] is cached
                value = await same_as_fresh(stand.owner, ref)
                assert value.progress is not None and value.times is not None
                assert value.result.process is not None
                if name.startswith('campaign'):
                    acquired = await stand.owner._result_thread(readers.acquire_campaign,
                        stand.owner.state_directory, operation_id)
                    assert not isinstance(acquired, Exception)
                    assert (await fresh(stand.owner, ref, acquired=acquired)).to_payload() == value.to_payload()
                    assert operation_id not in stand.owner._projection_cache
    asyncio.run(scenario())


def test_acceptance_dry_run_and_locked_write_observe_equal_facts(tmp_path, monkeypatch):
    async def scenario():
        async with reference_project(tmp_path) as stand:
            (tmp_path / 'work/lab/SPEC.md').write_text('Neutral offline reference with corrected wording.\n')
            snapshots = []
            original = stand.session.facts
            async def facts(*args, **kwargs):
                result = await original(*args, **kwargs)
                snapshots.append((stand.session._mutation_lock.locked(), result[1]))
                return result
            monkeypatch.setattr(stand.session, 'facts', facts)
            params = dict(only=['spec.confirmed'], across_pairs=False,
                          reason='reviewed wording', channel='cli', author='neutral')
            preview = await stand.session.accept('acq', 'port-a', dry_run=True, **params)
            assert preview.facts and preview.id is None
            expected = [{key: row[key] for key in ('id', 'from', 'to')} for row in preview.facts]
            stand.owner._projection_cache.clear()  # write recomputes from real input
            written = await stand.session.accept('acq', 'port-a', dry_run=False,
                                                  expected=expected, **params)
            assert written.id and written.facts == preview.facts
            assert len(snapshots) == 2
            assert [locked for locked, _ in snapshots] == [False, True]
            assert snapshots[0][1] == snapshots[1][1]
    asyncio.run(scenario())


def append_hygiene(run, count=1):
    """Valid b15 metadata records; no model, execution or external corpus."""
    path = run / 'campaign.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    last = records[-1]
    with path.open('a') as stream:
        for offset in range(1, count + 1):
            sequence = last['sequence'] + offset
            row = dict(schema_version=1, sequence=sequence, run_id=last['run_id'],
                timestamp='2026-11-01T00:00:00.000000Z', operation_id=None,
                event='authorization_applied', payload={
                    'code': 'run_dir_unsafe_entry', 'path': f'neutral-{sequence}',
                    'instance_digest': f'{sequence:064x}',
                    'authorization': {'id': f'{sequence:064x}', 'source': 'caller',
                                      'issued_at': '2026-11-01T00:00:00Z', 'reason': 'neutral test'}})
            stream.write(json.dumps(row, separators=(',', ':')) + '\n')
    return len(records) + count


@pytest.mark.parametrize('event', ['journal_append', 'inode_replaced', 'run_removed',
                                  'resume', 'cleanup_required', 'delete_results'])
def test_real_input_event_invalidates_projection(tmp_path, event):
    async def scenario():
        async with real_operation(tmp_path, finished=event in {'resume', 'delete_results'}) as stand:
            owner, ref = stand.owner, stand.ref
            before = await owner.status(ref)
            assert ref.id in owner._projection_cache
            prior_entry = owner._projection_cache[ref.id]
            if event == 'journal_append':
                append_hygiene(stand.run)
            elif event == 'inode_replaced':
                path = stand.run / 'campaign.jsonl'
                original = path.stat()
                other = path.with_suffix('.replacement')
                other.write_bytes(path.read_bytes())
                os.utime(other, ns=(original.st_atime_ns, original.st_mtime_ns))
                other.replace(path)
                assert path.stat().st_ino != original.st_ino
            elif event == 'run_removed':
                shutil.rmtree(stand.run)
            elif event == 'resume':
                resumed = await owner._db('accept_resume', ref.id,
                                          target_ref=stand.target, executor=_executor())
                assert resumed.scope_id != stand.scope.name
            elif event == 'cleanup_required':
                await owner._db('mark_operation_cleanup_required', ref.id)
            else:
                assert (await owner.wait(await owner.delete_results(ref))).state == 'succeeded'
                assert ref.id not in owner._projection_cache
            after = await owner.status(ref)
            if event == 'inode_replaced':
                assert after.to_payload() == before.to_payload()
                assert owner._projection_cache[ref.id] is not prior_entry
            else:
                assert after.to_payload() != before.to_payload()
            assert after.to_payload() == (await fresh(owner, ref)).to_payload()
            if event == 'journal_append':
                assert after.results_state == 'present'
                assert readers.cacheable(after, True)
                assert after.times['last_event_at'] != before.times['last_event_at']
            elif event == 'run_removed':
                assert after.results_state == after.times_state == 'unavailable'
            elif event == 'resume':
                assert len(after.starts) == 2 and after.control_state == 'reserved'
            elif event == 'cleanup_required':
                assert after.control_state == 'cleanup_required'
            elif event == 'delete_results':
                assert after.results_state == 'deleted'
                assert after.result is None or after.result.core is None
                assert ref.id not in owner._projection_cache
    asyncio.run(scenario())


def runtime_for(stand, helper=None):
    return OperationRuntime(stand.ref.id, stand.scope.name,
        SimpleNamespace(kind='campaign'), None, None,
        asyncio.get_running_loop().create_future(), helper=helper,
        paths=SimpleNamespace(scope_directory=stand.scope, run_directory=stand.run,
                              diagnostic_path=stand.scope / 'diagnostic.json'))


def test_live_helper_child_events_invalidate_result_process(tmp_path):
    from evolution_lab.owner.protocol import HelperHandle
    from test_owner_scopes import finish
    async def scenario():
        async with real_operation(tmp_path) as stand:
            owner, ref = stand.owner, stand.ref
            await owner._db('update_operation', ref.id, process=ProcessFacts())
            handle = HelperHandle.launch(sys.executable, stand.scope, stand.scope.name,
                                         owner.owner_generation)
            runtime = runtime_for(stand, handle)
            owner._operations[ref.id] = runtime
            release = stand.scope / 'child-release'
            try:
                script = ('import pathlib,time; p=pathlib.Path(' + repr(str(release)) + '); '
                          '\nwhile not p.exists(): time.sleep(.01)\nraise SystemExit(7)')
                await handle.configure(argv=[sys.executable, '-I', '-c', script], env={},
                                       stop_grace_seconds=.2)
                await handle.wait_event('ready', 5)
                before = await same_as_fresh(owner, ref)
                await handle.go()
                await handle.wait_event('child_started', 5)
                started = await same_as_fresh(owner, ref)
                assert before.process.launched is not True and started.process.launched is True
                assert started.result.process.launched is True
                release.touch()
                await handle.wait_event('child_exited', 5)
                exited = await same_as_fresh(owner, ref)
                assert started.result.process.exit_code is None
                assert exited.result.process.exit_code == 7
            finally:
                release.touch()
                await finish(handle)
                owner._operations.pop(ref.id)
    asyncio.run(scenario())


def test_real_finalization_replaces_overlay_with_durable_result(tmp_path, monkeypatch):
    async def scenario():
        async with real_operation(tmp_path) as stand:
            owner, ref = stand.owner, stand.ref
            before = await owner.status(ref)
            seen = []
            original = owner._db
            async def db(method, *args, **kwargs):
                if method == 'update_operation':
                    assert ref.id in owner._operation_cache
                    interim = await same_as_fresh(owner, ref)
                    assert interim.control_state == before.control_state
                    seen.append(interim)
                return await original(method, *args, **kwargs)
            monkeypatch.setattr(owner, '_db', db)
            # No cleanup receipt: the actual finalizer conservatively persists
            # cleanup_required, without a live target or a namespace sweep.
            await owner._finalize_operation(runtime_for(stand))
            assert seen and ref.id not in owner._operation_cache
            after = await same_as_fresh(owner, ref)
            durable = await owner._db('operation_status', ref.id)
            assert after.control_state == durable.control_state == 'cleanup_required'
            assert after.result.persistence.state == durable.result.persistence.state == 'saved'
            assert before.to_payload() != after.to_payload()
    asyncio.run(scenario())


@contextmanager
def fail_file_opens(monkeypatch, target):
    """Deny a real content open without changing bytes or signature metadata."""
    import builtins
    import io
    target = target.resolve()
    hits = []
    with monkeypatch.context() as patch:
        for module, name in ((os, 'open'), (builtins, 'open'), (io, 'open')):
            original = getattr(module, name)
            def denied(path, *args, _original=original, **kwargs):
                if not isinstance(path, int):
                    resolved = Path(path)
                    if not resolved.is_absolute():
                        base = (Path(fd_path(kwargs["dir_fd"]))
                                if kwargs.get('dir_fd') is not None else Path.cwd())
                        resolved = base / resolved
                    if resolved.resolve() == target:
                        hits.append(str(target))
                        raise OSError(errno.EIO, 'injected unchanged-file read error', str(target))
                return _original(path, *args, **kwargs)
            patch.setattr(module, name, denied)
        yield hits


@pytest.mark.parametrize('point', ['campaign_journal', 'publications', 'independent_evidence',
                                  'evaluation_journal', 'outcome', 'times'])
def test_unchanged_file_fault_is_not_cached_and_next_read_recovers(tmp_path, monkeypatch, point):
    async def scenario():
        kind = 'evaluation' if point in {'evaluation_journal', 'outcome'} else 'campaign'
        async with real_operation(tmp_path, kind=kind) as stand:
            owner, ref = stand.owner, stand.ref
            expected = await owner.status(ref)
            assert readers.cacheable(expected, True)
            signature = readers.run_signature(tmp_path, ref.id, stand.scope)
            targets = {'campaign_journal': stand.run / 'campaign.jsonl',
                'publications': stand.run / 'artifacts.jsonl',
                'independent_evidence': next(stand.run.glob('artifacts/auxiliary/*/payload.json'), None),
                'evaluation_journal': stand.run / 'measurement/events.jsonl',
                'outcome': stand.scope / 'evaluation-outcome.json'}
            owner._projection_cache.clear()  # a cache hit cannot observe an uncalled reader fault
            owner._measurements.drop(ref.id)
            completeness = readers.ReadCompleteness()
            async def fault_read():
                async def work():
                    return await owner._status_projection(ref, completeness=completeness)
                return await owner._result_work(work, owner._result_io.acquire_read(ref.id))
            if point == 'times':
                from evolution_lab.owner import core_raw
                original = core_raw.campaign_journal_times
                hits = []
                def denied(*args, **kwargs):
                    hits.append(True)
                    raise OSError(errno.EIO, 'injected times read error')
                with monkeypatch.context() as patch:
                    patch.setattr(core_raw, 'campaign_journal_times', denied)
                    failed = await fault_read()
                assert hits and core_raw.campaign_journal_times is original
                assert failed.times_state == 'unavailable'
            else:
                assert targets[point] is not None
                with fail_file_opens(monkeypatch, targets[point]) as hits:
                    failed = await fault_read()
                assert hits
            assert failed.to_payload() != expected.to_payload()
            assert not readers.cacheable(failed, completeness.complete)
            assert ref.id not in owner._projection_cache
            assert readers.run_signature(tmp_path, ref.id, stand.scope) == signature
            recovered = await owner.status(ref)
            assert recovered.to_payload() == expected.to_payload()
            assert ref.id in owner._projection_cache
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['file_vanished', 'directory_vanished', 'subdirectory_eacces'])
def test_signature_walk_failure_bypasses_existing_entry_and_recovers(tmp_path, monkeypatch, fault):
    from evolution_lab.owner import projection_signature
    async def scenario():
        async with real_operation(tmp_path) as stand:
            owner, ref = stand.owner, stand.ref
            expected = await owner.status(ref)
            cached = owner._projection_cache[ref.id]
            calls = []
            original_signature = readers.run_signature
            def signature(*args):
                # Fail only the signature walk. The subsequent actual projection
                # reads unchanged files, exactly as a transient stat/open race.
                with monkeypatch.context() as patch:
                    point = 'stat' if fault == 'file_vanished' else 'open'
                    original = getattr(projection_signature.os, point)
                    def denied(path, *args, **kwargs):
                        if str(path) == ('campaign.jsonl' if fault == 'file_vanished' else 'measurements'):
                            calls.append(path)
                            if fault == 'subdirectory_eacces':
                                raise PermissionError(errno.EACCES, 'denied')
                            raise FileNotFoundError(errno.ENOENT, 'vanished mid-walk')
                        return original(path, *args, **kwargs)
                    patch.setattr(projection_signature.os, point, denied)
                    return original_signature(*args)
            with monkeypatch.context() as patch:
                patch.setattr(readers, 'run_signature', signature)
                observed = await owner.status(ref)
                assert calls and observed is not expected
                assert observed.to_payload() == expected.to_payload()
                assert owner._projection_cache[ref.id] is cached  # neither used nor replaced
                assert (await fresh(owner, ref)).to_payload() == expected.to_payload()
                assert ref.id not in owner._projection_cache
            assert (await owner.status(ref)).to_payload() == expected.to_payload()
            assert ref.id in owner._projection_cache
    asyncio.run(scenario())


def retained_size(value):
    """Unique reachable Python data bytes, excluding shared classes/code/modules.

    Includes cache keys, frozen mappings, dataclasses and Path internals. This is
    an upper bound for marginal retention: some values are also held by Owner's
    other caches. It is not parser peak RSS or allocator fragmentation.
    """
    import gc
    import types
    ignored = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
               types.MethodType, types.CodeType)
    seen = set()
    def size(item):
        if id(item) in seen or isinstance(item, ignored):
            return 0
        seen.add(id(item))
        return sys.getsizeof(item) + sum(size(child) for child in gc.get_referents(item))
    return size(value)


def test_reference_and_long_journal_retained_cache_memory(tmp_path):
    from evolution_lab.owner import core_raw
    async def scenario():
        async with reference_project(tmp_path / 'reference') as stand:
            owner = stand.owner
            for operation_id in stand.operation_ids.values():
                await owner.status(ResourceRef('operation', operation_id))
            assert len(owner._projection_cache) == 3
            reference = {'entries': len(owner._projection_cache),
                         'cache_bytes': retained_size(owner._projection_cache),
                         'entry_bytes': {name: retained_size(owner._projection_cache[operation_id])
                            for name, operation_id in stand.operation_ids.items()}}
        async with real_operation(tmp_path / 'long') as stand:
            before = await stand.owner.status(stand.ref)
            small_bytes = retained_size(stand.owner._projection_cache)
            count = append_hygiene(stand.run, 5000)
            log = core_raw.campaign_log(stand.run, families=core_raw.FAMILIES)
            assert log.journal_integrity == 'complete'
            assert len(log.records) == count >= 5000
            assert log.records[-1].to_payload()['sequence'] == count
            del log
            after = await same_as_fresh(stand.owner, stand.ref)
            assert readers.cacheable(after, True)
            assert after.times['last_event_at'] != before.times['last_event_at']
            assert stand.ref.id in stand.owner._projection_cache
            long = {'records': count, 'journal_bytes': (stand.run / 'campaign.jsonl').stat().st_size,
                    'cache_bytes': retained_size(stand.owner._projection_cache),
                    'before_cache_bytes': small_bytes, 'entries': 1,
                    'shape': '25-record family15 capture plus 5000 distinct valid hygiene authorizations'}
        evidence = {'method': retained_size.__doc__, 'reference': reference, 'long': long}
        print('CACHE_MEMORY', json.dumps(evidence, sort_keys=True))
    asyncio.run(scenario())
