"""R3-04: pre-Core starts cannot consume an earlier refusal's recovery path."""
import asyncio
import json

import pytest

from test_owner_cleanup import finished
from test_owner_target import make_bundle, ready_target

pytestmark = pytest.mark.slow


def _worker(python, *, refused):
    expression = ("(_ for _ in ()).throw(EvolveRefused('run_dir_unsafe_entry', "
                  "'scorer-traces', instance_digest='d'*64, effect='hygiene'))" if refused else
                  "(_ for _ in ()).throw(RuntimeError('after Core entry'))")
    return [str(python), '-c', 'import evolution.tree.evolve as e; '
            'from evolution.tree import EvolveRefused; '
            f'e.admit_manifest=lambda p: {expression}; '
            'from evolution_lab.owner._campaign_worker import main; main()']


async def _refused(owner, bundle, prepared, monkeypatch):
    from evolution_lab.owner import ResourceRef, execution
    monkeypatch.setattr(execution, 'campaign_worker_command', lambda python: _worker(python, refused=True))
    _, target, _ = await ready_target(owner, bundle)
    first = await owner.start(prepared.digest, target_ref=target, secrets={})
    ref = ResourceRef('operation', first.id)
    value = await finished(owner, ref, seconds=60)
    assert value.failure.code == 'core_refused', value.to_payload()
    assert value.resume is None
    record = await owner.refusal(ref)
    assert record.start == 1 and len(record.findings) == 1
    assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
    return ref, record


@pytest.mark.parametrize('cut', ['target', 'cancel', 'manifest', 'decision', 'worker'])
def test_pre_core_failure_retains_refusal_and_saved_decisions(tmp_path, monkeypatch, cut):
    from evolution.tree import read_decisions
    import evolution.tree as core
    from evolution_lab.owner import Owner, OwnerError, Accepted, execution, linked, target as target_module
    from evolution_lab.preparation import PreparationStore
    state, bundle, _ = make_bundle(tmp_path)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    original_worker = execution.campaign_worker_command
    original_health = target_module.matched_health
    original_manifest = linked.campaign_manifest
    original_write = core.write_decision

    async def scenario():
        async with Owner.open(state) as owner:
            ref, record = await _refused(owner, bundle, prepared, monkeypatch)
            finding = record.findings[0]
            chosen = Accepted(finding.code, finding.instance_digest, None)
            run = state / 'artifacts' / ref.id / ref.id
            assert not (run / 'campaign.jsonl').exists()
            assert not read_decisions(run)
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(execution, 'campaign_worker_command', original_worker)
            if cut in {'target', 'cancel'}:
                async def health(*args, **kwargs):
                    if cut == 'target':
                        assert (await owner.wait(await owner.stop_target(target))).state == 'succeeded'
                        raise OwnerError('target_not_running', 'before_start')
                    await owner.stop(ref)
                    return await original_health(*args, **kwargs)
                monkeypatch.setattr(target_module, 'matched_health', health)
            elif cut == 'manifest':
                async def manifest(*args, **kwargs):
                    raise OSError('campaign manifest copy failed')
                monkeypatch.setattr(linked, 'campaign_manifest', manifest)
            elif cut == 'worker':
                def before_core(python):
                    return [str(python), '-c',
                        'from evolution_lab.owner import _campaign_worker as w; '
                        'w.check_run_directory=lambda *a: (_ for _ in ()).throw(OSError()); w.main()']
                monkeypatch.setattr(execution, 'campaign_worker_command', before_core)
            else:
                def write(*args, **kwargs):
                    raise OSError('decision write failed after accept_resume')
                monkeypatch.setattr(core, 'write_decision', write)
            if cut == 'decision':
                with pytest.raises(OwnerError):
                    await owner.resume(ref, target_ref=target, secrets={}, accept=[chosen], reason='owned output')
            else:
                await owner.resume(ref, target_ref=target, secrets={}, accept=[chosen], reason='owned output')
            failed = await finished(owner, ref, seconds=60)
            assert len(failed.starts) == 2 and failed.failure is not None, failed.to_payload()
            assert failed.failure.code != 'core_refused'
            assert not (run / 'campaign.jsonl').exists()
            assert failed.resume is None
            assert not (state / 'scopes' / failed.starts[-1].scope_id / 'core-entry.json').exists()
            if cut == 'worker':
                assert failed.process.launched is True
            saved = {p.name: p.read_bytes() for p in (run / 'authorizations').glob('*.json')}
            assert len(read_decisions(run)) == (0 if cut == 'decision' else 1)
            assert len(saved) == (0 if cut == 'decision' else 1)
            retained = await owner.refusal(ref)
            assert retained is not None
            assert retained.to_payload() == record.to_payload()
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
        monkeypatch.setattr(execution, 'campaign_worker_command', original_worker)
        monkeypatch.setattr(target_module, 'matched_health', original_health)
        monkeypatch.setattr(linked, 'campaign_manifest', original_manifest)
        monkeypatch.setattr(core, 'write_decision', original_write)
        async with Owner.open(state) as owner:
            assert (await owner.refusal(ref)).to_payload() == record.to_payload()
            _, target, _ = await ready_target(owner, bundle)
            await owner.resume(ref, target_ref=target, secrets={},
                accept=[chosen] if cut == 'decision' else (),
                reason='owned output' if cut == 'decision' else None)
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, json.dumps(done.to_payload())
            assert done.result.core['complete'] is True
            assert (run / 'campaign.jsonl').is_file()
            assert len(done.starts) == 3
            assert len(read_decisions(run)) == 1
            assert all((run / 'authorizations' / name).read_bytes() == raw for name, raw in saved.items())
            assert await owner.refusal(ref) is None
    asyncio.run(scenario())


def test_core_entry_without_refusal_clears_exception_even_without_journal(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError, execution
    from evolution_lab.preparation import PreparationStore
    state, bundle, _ = make_bundle(tmp_path)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, record = await _refused(owner, bundle, prepared, monkeypatch)
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(execution, 'campaign_worker_command', lambda python: _worker(python, refused=False))
            await owner.resume(ref, target_ref=target, secrets={})
            failed = await finished(owner, ref, seconds=60)
            assert failed.failure.code == 'lab_error'
            assert not (state / 'artifacts' / ref.id / ref.id / 'campaign.jsonl').exists()
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
        async with Owner.open(state) as owner:
            assert await owner.refusal(ref) is None
            _, target, _ = await ready_target(owner, bundle)
            with pytest.raises(OwnerError) as error:
                await owner.resume(ref, target_ref=target, secrets={})
            assert error.value.code == 'campaign_unreadable'
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['operation_id', 'scope_id', 'owner_generation', 'extra', 'symlink', 'garbage'])
def test_core_entry_receipt_authenticates_its_start(tmp_path, mutation):
    from evolution_lab.owner.core_entry import publish_core_entry, read_core_entry
    from evolution_lab.owner.receipts import ReceiptError
    config = {'operation_id': 'a'*32, 'scope_id': 'b'*32, 'owner_generation': 'c'*32,
              'diagnostic_path': str(tmp_path / 'diagnostic.json')}
    path = tmp_path / 'core-entry.json'
    ids = {key: config[key] for key in ('operation_id', 'scope_id', 'owner_generation')}
    assert not read_core_entry(path, **ids)
    publish_core_entry(config)
    assert read_core_entry(path, **ids)
    assert path.stat().st_mode & 0o777 == 0o600
    if mutation in ids:
        ids[mutation] = 'e'*32
    elif mutation == 'symlink':
        destination = path.with_name('other.json')
        path.rename(destination)
        path.symlink_to(destination)
    elif mutation == 'garbage':
        path.write_text('{')
    else:
        value = json.loads(path.read_text())
        value['extra'] = True
        path.write_text(json.dumps(value))
    with pytest.raises((OSError, ReceiptError)):
        read_core_entry(path, **ids)


def test_failed_entry_publication_prevents_the_core_call(tmp_path, monkeypatch):
    import evolution.tree.evolve as core
    from evolution_lab.owner import core_entry, _campaign_worker as worker
    called = []
    monkeypatch.setattr(core, 'admit_manifest', lambda path: called.append(path))
    def fail(config):
        raise OSError('no durable entry evidence')
    monkeypatch.setattr(core_entry, 'publish_core_entry', fail)
    config = {'operation_id': 'a'*32, 'scope_id': 'b'*32, 'owner_generation': 'c'*32,
              'diagnostic_path': str(tmp_path / 'diagnostic.json'), 'manifest_path': 'unused'}
    assert worker.run_worker(config, {}, lambda: False) == 1
    assert called == []
