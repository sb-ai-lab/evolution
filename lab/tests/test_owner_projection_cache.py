"""Input invalidation and lease safety of Owner's operation projection cache."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
import stat
from types import SimpleNamespace

import pytest

from evolution_lab.owner import Owner, ResourceRef, readers
from evolution_lab.owner.progress import CampaignPlan
from test_owner_projection_completeness import campaign, evaluation  # noqa: F401
from test_owner_store_starts import DIGEST


@asynccontextmanager
async def owner_for(fixture, monkeypatch):
    root, status, run, *scopes = fixture
    async with Owner.open(root) as owner:
        original_db = owner._db
        state = SimpleNamespace(durable=status, scope=scopes[0] if scopes else None)
        async def db(method, *args, **kwargs):
            if method == 'operation_status':
                return state.durable
            if method == 'resource_scopes':
                return [] if state.scope is None else [{'root_relative': str(state.scope.relative_to(root))}]
            if method == 'operation_policy_history':
                return []  # This synthetic operation has no execution policy.
            return await original_db(method, *args, **kwargs)
        monkeypatch.setattr(owner, '_db', db)
        owner._plan_cache[DIGEST] = CampaignPlan('neutral', 1, 1, 'quality')
        try:
            yield owner, state, ResourceRef('operation', status.id)
        finally:
            owner._operations.clear()  # fake live process has no worker to close


def spy_projection(monkeypatch):
    calls = []
    original = readers.project_operation
    def project(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(readers, 'project_operation', project)
    return calls


@pytest.mark.parametrize('kind', ['campaign', 'evaluation'])
def test_unchanged_values_reuse_projection_and_remember_summary(campaign, evaluation, monkeypatch, kind):
    async def scenario():
        async with owner_for(evaluation if kind == 'evaluation' else campaign, monkeypatch) as (owner, state, ref):
            calls = spy_projection(monkeypatch)
            first = await owner.status(ref)
            assert readers.cacheable(first, True)
            state.durable = replace(state.durable)  # equality, not identity
            owner._operation_cache[ref.id] = replace(state.durable)
            owner._plan_cache[DIGEST] = replace(owner._plan_cache[DIGEST])
            owner._summary_cache.clear()
            remembers = []
            original = owner._remember_summary
            def remember(*args):
                remembers.append(args)
                original(*args)
            monkeypatch.setattr(owner, '_remember_summary', remember)
            assert (await owner.status(ref)).to_payload() == first.to_payload()
            assert len(calls) == 1
            assert len(remembers) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('part', ['durable', 'overlay', 'live_process', 'plan', 'scope', 'run_file', 'scope_file'])
def test_changed_key_recomputes_and_replaces(evaluation, monkeypatch, part):
    async def scenario():
        async with owner_for(evaluation, monkeypatch) as (owner, state, ref):
            calls = spy_projection(monkeypatch)
            process = [state.durable.process]
            if part == 'durable':
                # Hold the overlay constant: durable is an independent input.
                owner._operation_cache[ref.id] = replace(state.durable)
            if part == 'live_process':
                done = asyncio.get_running_loop().create_future()
                owner._operations[ref.id] = SimpleNamespace(done=done, paths=None,
                    process_facts=lambda prior: process[0])
            await owner.status(ref)
            if part == 'durable':
                state.durable = replace(state.durable, control_state='stopping')
            elif part == 'overlay':
                owner._operation_cache[ref.id] = replace(state.durable, control_state='stopping')
            elif part == 'live_process':
                process[0] = replace(process[0], exit_code=0)
            elif part == 'plan':
                owner._plan_cache[DIGEST] = CampaignPlan('changed', 2, 3, 'quality')
            elif part == 'scope':
                import shutil
                scope = state.scope.with_name('3' * 32)
                shutil.copytree(state.scope, scope)
                # Outcome validates the scope id, so change its id with the scope.
                import json
                path = scope / 'evaluation-outcome.json'
                outcome = json.loads(path.read_text())
                outcome['scope_id'] = scope.name
                path.write_text(json.dumps(outcome))
                state.scope = scope
            elif part == 'run_file':
                (evaluation[2] / 'new-file').write_text('new input')
            else:
                path = state.scope / 'evaluation-outcome.json'
                path.write_text(path.read_text() + ' ')
            second = await owner.status(ref)
            third = await owner.status(ref)
            assert second.to_payload() == third.to_payload()
            assert len(calls) == 2
    asyncio.run(scenario())


class ForbiddenCache(dict):
    def get(self, *args):
        pytest.fail('excluded path looked up projection cache')
    def __setitem__(self, *args):
        pytest.fail('excluded path filled projection cache')


@pytest.mark.parametrize('gate', ['acquired', 'deleting', 'delete_failed', 'deleted', 'writer'])
def test_excluded_paths_never_use_or_fill(campaign, monkeypatch, gate):
    async def scenario():
        async with owner_for(campaign, monkeypatch) as (owner, state, ref):
            expected = await owner.status(ref)
            owner._projection_cache = ForbiddenCache(owner._projection_cache)
            calls = spy_projection(monkeypatch)
            if gate == 'acquired':
                acquired = readers.acquire_campaign(owner.state_directory, ref.id)
                async def work():
                    return await owner._status_projection(ref, acquired=acquired)
                value = await owner._result_work(work, owner._result_io.acquire_read(ref.id))
                assert value.to_payload() == expected.to_payload()
                assert len(calls) == 1
            elif gate == 'writer':
                lease = owner._result_io.acquire_write(ref.id)
                try:
                    value = await owner.status(ref)
                    assert value.progress_state == 'unavailable'
                    assert value.result is None or value.result.core is None
                finally:
                    lease.release()
                assert not calls
            else:
                state.durable = replace(state.durable, results_state=gate)
                value = await owner.status(ref)
                assert value.results_state == gate
                assert value.progress_state == 'unavailable'
                assert not calls
    asyncio.run(scenario())


def test_incomplete_signature_neither_reuses_nor_fills(campaign, monkeypatch):
    async def scenario():
        async with owner_for(campaign, monkeypatch) as (owner, state, ref):
            expected = await owner.status(ref)
            owner._projection_cache = ForbiddenCache(owner._projection_cache)
            monkeypatch.setattr(readers, 'run_signature', lambda *args: None)
            calls = spy_projection(monkeypatch)
            for _ in range(2):
                assert (await owner.status(ref)).to_payload() == expected.to_payload()
            assert len(calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['incomplete', 'degraded'])
def test_failed_read_is_retried_even_when_public_result_is_present(campaign, monkeypatch, failure):
    async def scenario():
        async with owner_for(campaign, monkeypatch) as (owner, state, ref):
            original = readers.project_operation
            calls = []
            def project(*args, **kwargs):
                calls.append(None)
                value = original(*args, **kwargs)
                if failure == 'incomplete':
                    kwargs['completeness'].mark_incomplete()
                else:
                    core = dict(value.result.core)
                    core['independent'] = {'evidence': 'unreadable'}
                    value = replace(value, result=replace(value.result, core=core))
                return value
            monkeypatch.setattr(readers, 'project_operation', project)
            for _ in range(2):
                assert (await owner.status(ref)).results_state == 'present'
            assert len(calls) == 2
            assert ref.id not in owner._projection_cache
    asyncio.run(scenario())


def test_signature_precedes_projection_and_fill_keeps_same_lease(campaign, monkeypatch):
    async def scenario():
        async with owner_for(campaign, monkeypatch) as (owner, state, ref):
            original_signature, original_project = readers.run_signature, readers.project_operation
            events = []
            access = []
            def under_lease():
                current = owner._result_io._operations[ref.id]
                assert current.readers == 1 and not current.writer
                access.append(current)
            def signature(*args):
                under_lease()
                events.append('signature')
                return original_signature(*args)
            def project(*args, **kwargs):
                under_lease()
                events.append('project')
                # A change during projection must miss the pre-projection key on the next read.
                (campaign[2] / 'during-read').write_text(str(len(events)))
                return original_project(*args, **kwargs)
            class LeaseCache(dict):
                def get(self, *args):
                    under_lease()
                    return super().get(*args)
                def __setitem__(self, *args):
                    under_lease()
                    events.append('fill')
                    super().__setitem__(*args)
            owner._projection_cache = LeaseCache()
            monkeypatch.setattr(readers, 'run_signature', signature)
            monkeypatch.setattr(readers, 'project_operation', project)
            await owner.status(ref)
            assert not owner._result_io.held(ref.id)
            await owner.status(ref)
            assert events == ['signature', 'project', 'fill'] * 2
            assert all(item is access[0] for item in access)
    asyncio.run(scenario())


def test_delete_results_drops_projection(tmp_path):
    from test_owner_delete import register, files, REF
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            files(owner.state_directory, await owner._db('owned_inventory', REF.id))
            owner._projection_cache[REF.id] = ('old key', 'private payload')
            terminal = await owner.wait(await owner.delete_results(REF))
            assert terminal.state == 'succeeded'
            assert REF.id not in owner._projection_cache
    asyncio.run(scenario())


@pytest.fixture
def signature_tree(tmp_path):
    run = tmp_path / 'artifacts' / ('1' * 32) / ('1' * 32)
    run.mkdir(parents=True)
    (run / 'nested').mkdir()
    (run / 'nested/data').write_bytes(b'one')
    scope = tmp_path / 'scopes' / ('2' * 32)
    scope.mkdir(parents=True)
    (scope / 'evaluation-outcome.json').write_bytes(b'{}')
    return tmp_path, run, scope


def signature(tree):
    root, run, scope = tree
    return readers.run_signature(root, run.name, scope)


@pytest.mark.parametrize('change', ['path', 'type', 'size', 'mtime_ns', 'ino', 'ctime_ns', 'root_absent', 'scope_file'])
def test_signature_covers_every_input(signature_tree, monkeypatch, change):
    tree = signature_tree
    root, run, scope = tree
    before = signature(tree)
    assert before is not None and before == signature(tree)
    file = run / 'nested/data'
    if change == 'path':
        file.rename(file.with_name('renamed'))
    elif change in {'type', 'size', 'mtime_ns', 'ino', 'ctime_ns'}:
        original = os.stat
        def changed_stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if str(path) == 'data':
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}
                if change == 'type':
                    fields['st_mode'] = stat.S_IFLNK | stat.S_IMODE(fields['st_mode'])
                else:
                    fields['st_' + change] += 1
                return SimpleNamespace(**fields)
            return result
        monkeypatch.setattr(os, 'stat', changed_stat)
    elif change == 'root_absent':
        import shutil
        shutil.rmtree(run)
    else:
        (scope / 'evaluation-outcome.json').write_bytes(b'{ }')
    after = signature(tree)
    assert after is not None and after != before


def test_absent_roots_are_recorded_and_creation_invalidates(tmp_path):
    before = readers.run_signature(tmp_path, '1' * 32, None)
    assert before is not None
    run = tmp_path / 'artifacts' / ('1' * 32) / ('1' * 32)
    run.mkdir(parents=True)
    assert readers.run_signature(tmp_path, run.name, None) != before


def test_signature_does_not_follow_leaf_symlinks(signature_tree, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'data'
    target.write_text('one')
    (signature_tree[1] / 'link').symlink_to(outside, target_is_directory=True)
    before = signature(signature_tree)
    target.write_text('changed outside tree')
    assert before is not None and signature(signature_tree) == before


@pytest.mark.parametrize('boundary', ['artifacts', 'operation', 'run', 'scope_parent', 'scope'])
def test_signature_refuses_symlink_boundaries(signature_tree, boundary):
    root, run, scope = signature_tree
    path = {'artifacts': root / 'artifacts', 'operation': run.parent, 'run': run,
            'scope_parent': scope.parent, 'scope': scope}[boundary]
    moved = path.with_name(path.name + '-moved')
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=True)
    assert signature(signature_tree) is None


@pytest.mark.parametrize('point', ['stat', 'scandir', 'open', 'fstat'])
@pytest.mark.parametrize('error', [PermissionError('denied'), FileNotFoundError('vanished'), OSError('io')])
def test_signature_failure_midwalk_is_incomplete(signature_tree, monkeypatch, point, error):
    original = getattr(os, point)
    def fail(path, *args, **kwargs):
        if (point == 'stat' and str(path) == 'data') or point in {'scandir', 'fstat'} or (
                point == 'open' and str(path) == 'nested'):
            raise error
        return original(path, *args, **kwargs)
    # Restore global fstat before pytest's fd-capture hook touches its own file.
    with monkeypatch.context() as patch:
        patch.setattr(os, point, fail)
        assert signature(signature_tree) is None
