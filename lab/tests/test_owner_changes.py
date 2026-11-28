"""S4 neutral public-core lineage and private snapshot contracts; no campaign run."""
from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from evolution.tree import (
    ArtifactPolicyV1, ArtifactStore, CampaignSnapshot, CampaignView,
    SelectionDecision, project_campaign_status, project_campaign_result,
)
from evolution.tree import campaign_projections
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.evolve import derive_run_id
from evolution_lab.owner import OwnerError

OP = '11111111-1111-4111-8111-111111111111'
# The real ``core_raw.campaign_read`` runs; only Core's on-disk view read is replaced by a fixture view.
REAL_INSPECT_VIEW = campaign_projections.inspect_view


def test_snapshot_interface_available():
    assert importlib.util.find_spec('evolution_lab.owner.changes') is not None


@pytest.fixture
def changes():
    if importlib.util.find_spec('evolution_lab.owner.changes') is None:
        pytest.skip('snapshot implementation is absent in RED wheel')
    from evolution_lab.owner import changes
    return changes


@pytest.fixture
def campaign_identity():
    from evolution_lab.owner.core_raw import campaign_log
    journal = next((Path(__file__).parent / 'fixtures/panel/family14/run').glob('*/campaign.jsonl'))
    creation = campaign_log(journal.parent).records[0].payload
    return creation['identity'], creation['adaptation_payload']['grouping']['kind']


@pytest.fixture
def case(tmp_path, monkeypatch, changes, campaign_identity):
    campaign_identity, grouper_kind = campaign_identity
    seed = tmp_path / 'seed'
    seed.mkdir()
    (seed / 'editable.txt').write_bytes(b'before\n')
    (seed / 'fixed.txt').write_bytes(b'fixed\n')
    (seed / 'dependency.dat').write_bytes(b'dependency\n')
    policy = ArtifactPolicyV1((), ('dependency.dat',), ('editable.txt', 'new', 'gone', 'empty'))
    state = tmp_path / 'state'
    run = state / 'artifacts' / OP / OP
    run.parent.mkdir(parents=True)
    with ArtifactStore.create(run, seed, policy, parallel_attempts=1) as store:
        work = store.materialize_candidate('attempt1')
        (work / 'editable.txt').write_bytes(b'after\n')
        candidate = store.seal('attempt1')
        selected = store.promote(SelectionDecision(1, 'attempt1', candidate.digest,
            candidate.dependency_digest, {'version': 1, 'kind': 'journal_record',
            'run_id': 'fixture-decision', 'locator': 'fixture#bytes=0:1',
            'content_digest': '0' * 64}))
        unused = store.materialize_candidate('unused')
        (unused / 'editable.txt').write_bytes(b'unselected\n')
        unused_ref = store.seal('unused')
    initial = scan_tree(seed, policy)
    snapshot = CampaignSnapshot(
        family_version=14, grouper_kind=grouper_kind, identity={**campaign_identity,
            'artifact_store': {'seed_candidate_digest': initial.candidate_digest}},
        run_id=derive_run_id(run), alpha_plan={}, rounds={},
        champion=(selected.artifact_id, selected.digest),
        champion_dependency_digest=selected.dependency_digest, measurements={},
        accounted=frozenset(), counts={}, denominator=0, alpha_rounds={}, threshold=None,
        finished=None, capacity=None, first_round=1, total_rounds=1,
    )
    view = CampaignView(snapshot, {}, None, {})
    binding = SimpleNamespace(prepared_digest='1' * 64, kind='campaign', bundle=SimpleNamespace(
        bundle_digest='2' * 64, bundle_path=tmp_path,
        manifest=SimpleNamespace(seed_root=seed, policy=policy),
        execution_environment=SimpleNamespace(distributions=()),
    ))
    status = SimpleNamespace(id=OP, kind='campaign', prepared_digest='1' * 64)
    value = SimpleNamespace(seed=seed, state=state, run=run, selected=selected.root,
        unused=unused_ref.root, policy=policy, binding=binding, status=status,
        view=view, inventory=({'path': f'artifacts/{OP}', 'kind': 'tree'},),
        storage=object())
    monkeypatch.setattr(changes, 'open_prepared', lambda *args, **kwargs: value.binding)
    monkeypatch.setattr(campaign_projections, 'inspect_view', lambda *args, **kwargs: value.view)
    value.open = lambda **kwargs: changes.snapshot_changes(value.state, value.storage,
        value.status, value.inventory, **kwargs)
    return value


def overwrite(path, data):
    path.chmod(0o600)
    path.write_bytes(data)


def test_scratch_snapshot_moves_only_staging_and_preserves_diff_zip_bytes(case, tmp_path):
    from evolution_lab.owner.results import diff_snapshot, archive_snapshot
    with case.open() as snapshot:
        expected_diff = diff_snapshot(snapshot).to_payload()
        expected_archive = archive_snapshot(snapshot)
    scratch = tmp_path / 'pair'
    scratch.mkdir(mode=0o700)
    with case.open(scratch_directory=scratch) as snapshot:
        call = snapshot.call_directory
        assert call.parent == scratch / 'staging'
        assert call.is_dir()
        assert diff_snapshot(snapshot).to_payload() == expected_diff
        assert archive_snapshot(snapshot) == expected_archive
    assert not call.exists()
    assert not list((scratch / 'staging').iterdir())
    assert case.run.is_dir()


def test_matching_public_store_snapshots_and_cleanup(case):
    with case.open() as result:
        call = result.call_directory
        assert call.parent == case.run.parent / 'staging'
        assert result.source_files['editable.txt'].data == b'before\n'
        assert result.selected_files['editable.txt'].data == b'after\n'
        assert result.changed_paths == ('editable.txt',)
        assert result.source_digest == scan_tree(case.seed, case.policy).candidate_digest
        assert result.selected_digest == scan_tree(case.selected, case.policy).candidate_digest
        assert 'dependency.dat' not in result.source_files
        assert (call / 'source' / 'dependency.dat').read_bytes() == b'dependency\n'
        assert result.campaign_complete is False and result.measurement_complete is False
        assert result.campaign_outcome is None
        assert result.selected_version == 'v1'
        assert result.no_change is False
        with pytest.raises(TypeError):
            result.source_files['injected'] = result.source_files['editable.txt']
    assert not call.exists()


def test_unselected_candidate_corruption_does_not_block(case):
    overwrite(case.unused / 'editable.txt', b'corrupt unrelated candidate')
    assert ArtifactStore.inspect(case.run, verify=True).candidates[-1].corruption is not None
    with case.open() as result:
        assert result.selected_files['editable.txt'].data == b'after\n'


@pytest.mark.parametrize('side', ['source', 'selected', 'source_dependency', 'selected_dependency'])
def test_corrupt_identity_refused(case, side):
    root = case.seed if side.startswith('source') else case.selected
    name = 'dependency.dat' if side.endswith('dependency') else 'editable.txt'
    overwrite(root / name, b'changed outside expected identity')
    with pytest.raises(OwnerError) as error:
        with case.open(stage='export'):
            pytest.fail('corruption exposed')
    assert (error.value.code, error.value.stage) == ('export_unavailable', 'export')
    assert not list((case.run.parent / 'staging').glob('*')) if (case.run.parent / 'staging').exists() else True


@pytest.mark.parametrize('field', ['initial_digest', 'initial_version', 'final_digest', 'final_version', 'run_id', 'missing_final'])
def test_campaign_identity_mismatch_refused(case, field, monkeypatch, changes):
    snap = case.view.snapshot
    if field == 'initial_digest':
        snap = replace(snap, identity={**snap.identity,
            'artifact_store': {'seed_candidate_digest': 'f' * 64}})
    elif field == 'initial_version':
        # Public result fixes initial to v0; change the projected metadata itself.
        import evolution.tree as tree
        def altered_result(snapshot, **kwargs):
            payload = project_campaign_result(snapshot, **kwargs).to_payload()
            payload['initial_champion']['version'] = 'missing-version'
            return SimpleNamespace(to_payload=lambda: payload)
        monkeypatch.setattr(tree, 'project_campaign_result', altered_result)
    elif field == 'final_digest':
        snap = replace(snap, champion=('v1', 'f' * 64))
    elif field == 'final_version':
        snap = replace(snap, champion=('v9', snap.champion[1]))
    elif field == 'run_id':
        snap = replace(snap, run_id='foreign-campaign')
    else:
        snap = replace(snap, champion=('', ''))
    case.view = replace(case.view, snapshot=snap)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('unmatched identity exposed')
    assert error.value.code == 'export_unavailable'


@pytest.mark.parametrize('which', ['seed_parent', 'selected_parent', 'included_symlink', 'included_hardlink', 'included_fifo', 'selected_excluded'])
def test_unsafe_paths_refused(case, which, tmp_path):
    if which == 'seed_parent':
        moved = tmp_path / 'moved-seed'
        case.seed.rename(moved)
        case.seed.symlink_to(moved, target_is_directory=True)
    elif which == 'selected_parent':
        parent = case.selected.parent
        moved = parent.with_name('outside-champions')
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    elif which == 'selected_excluded':
        case.selected.chmod(0o700)
        (case.selected / '.env').write_text('excluded')
    else:
        target = case.seed / 'editable.txt'
        target.unlink()
        if which == 'included_symlink':
            target.symlink_to(case.seed / 'fixed.txt')
        elif which == 'included_hardlink':
            os.link(case.seed / 'fixed.txt', target)
        else:
            os.mkfifo(target)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('unsafe input exposed')
    assert error.value.code == 'export_unavailable'


def test_source_exclusions_are_not_copied_or_followed(case, tmp_path):
    (case.seed / '.venv').symlink_to(tmp_path / 'absent', target_is_directory=True)
    (case.seed / '.env').write_bytes(b'excluded secret')
    with case.open() as result:
        assert not (result.call_directory / 'source' / '.env').exists()
        assert not (result.call_directory / 'source' / '.venv').exists()


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a\\b', 'a//b', 'a/./b'])
def test_inventory_path_refuses_traversal(case, path):
    case.inventory = ({'path': path, 'kind': 'tree'},)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('unregistered topology used')
    assert error.value.code == 'export_unavailable'


def test_policy_writable_refusal(case):
    case.binding.bundle.manifest.policy = ArtifactPolicyV1((), ('dependency.dat',), ('new',))
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('read-only file change exposed')
    assert error.value.code == 'export_unavailable'


def test_snapshot_bytes_are_verified_after_copy(case, monkeypatch, changes):
    copy = changes._copy_tree
    def corrupt(*args, **kwargs):
        copy(*args, **kwargs)
        destination_fd = args[1]
        fd = os.open('editable.txt', os.O_WRONLY | os.O_TRUNC, dir_fd=destination_fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'copy drift')
    monkeypatch.setattr(changes, '_copy_tree', corrupt)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('unverified snapshot exposed')
    assert error.value.code == 'export_unavailable'


def test_concurrent_calls_and_crash_leftover_are_independent(case):
    staging = case.run.parent / 'staging'
    orphan = staging / 'abandoned'
    orphan.mkdir(parents=True)
    (orphan / 'source').write_bytes(b'do not trust or overwrite')
    barrier = threading.Barrier(2)
    def reader():
        with case.open() as result:
            barrier.wait(timeout=10)
            assert len(tuple(staging.iterdir())) == 3
            barrier.wait(timeout=10)
            return result.call_directory
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reader), pool.submit(reader)]
        calls = [future.result(timeout=15) for future in futures]
    assert calls[0] != calls[1]
    assert all(not call.exists() for call in calls)
    assert (orphan / 'source').read_bytes() == b'do not trust or overwrite'


def test_exception_inside_context_cleans_only_its_snapshot(case):
    with pytest.raises(KeyboardInterrupt):
        with case.open() as result:
            call = result.call_directory
            raise KeyboardInterrupt
    assert not call.exists()


@pytest.mark.parametrize('change', ['add_empty', 'delete_empty', 'file_to_directory', 'directory_to_file', 'ordinary_parents', 'same_tree_new_version'])
def test_directory_representability_uses_public_digest(case, change):
    # A different neutral public store at this operation's exact root.
    case.run.rename(case.run.with_name('replaced-fixture-run'))
    if change in ('delete_empty', 'directory_to_file'):
        (case.seed / 'empty').mkdir()
    with ArtifactStore.create(case.run, case.seed, case.policy, parallel_attempts=1) as store:
        work = store.materialize_candidate('shape')
        if change == 'add_empty':
            (work / 'empty').mkdir()
        elif change == 'delete_empty':
            (work / 'empty').rmdir()
        elif change == 'file_to_directory':
            (work / 'editable.txt').unlink()
            (work / 'editable.txt').mkdir()
        elif change == 'directory_to_file':
            (work / 'empty').rmdir()
            (work / 'empty').write_bytes(b'now a file')
        elif change == 'ordinary_parents':
            (work / 'new' / 'nested').mkdir(parents=True)
            (work / 'new' / 'nested' / 'value').write_bytes(b'added')
        candidate = store.seal('shape')
        selected = store.promote(SelectionDecision(1, 'shape', candidate.digest,
            candidate.dependency_digest, {'version': 1, 'kind': 'journal_record',
            'run_id': 'fixture-decision', 'locator': 'fixture#bytes=0:1',
            'content_digest': '0' * 64}))
    initial = scan_tree(case.seed, case.policy)
    case.view = replace(case.view, snapshot=replace(case.view.snapshot,
        family_version=14, identity={**case.view.snapshot.identity,
            'artifact_store': {'seed_candidate_digest': initial.candidate_digest}},
        champion=(selected.artifact_id, selected.digest)))
    if change in ('ordinary_parents', 'same_tree_new_version'):
        with case.open() as result:
            assert result.no_change is (change == 'same_tree_new_version')
            assert result.changed_paths == (() if change == 'same_tree_new_version' else ('new/nested/value',))
    else:
        with pytest.raises(OwnerError) as error:
            with case.open():
                pytest.fail('file-only unsupported shape exposed')
        assert error.value.code == 'export_unavailable'


@pytest.mark.parametrize('mismatch', ['reference_run', 'reference_path', 'duplicate_selected', 'dependency'])
def test_reference_binding_is_exact(case, monkeypatch, changes, mismatch):
    real_inspect = ArtifactStore.inspect
    def inspect(*args, **kwargs):
        projection = real_inspect(*args, **kwargs)
        champion = projection.champions[-1]
        reference = SimpleNamespace(**vars(champion.reference))
        altered = SimpleNamespace(**vars(champion))
        altered.reference = reference
        if mismatch == 'reference_run':
            reference.run_id = 'foreign-store'
        elif mismatch == 'reference_path':
            reference.locator = '../escape'
        elif mismatch == 'dependency':
            altered.dependency_digest = reference.dependency_digest = 'f' * 64
        champs = (*projection.champions[:-1], altered)
        if mismatch == 'duplicate_selected':
            champs += (altered,)
        return SimpleNamespace(run_id=projection.run_id, champions=champs)
    monkeypatch.setattr(ArtifactStore, 'inspect', inspect)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('invalid public reference accepted')
    assert error.value.code == 'export_unavailable'


@pytest.mark.parametrize('field', ['complete', 'campaign_outcome', 'final_champion'])
def test_independent_campaign_projections_must_agree(case, monkeypatch, changes, field):
    def changed(*args, **kwargs):
        payload = project_campaign_status(*args, **kwargs).to_payload()
        payload[field] = (True if field == 'complete' else None)
        if field == 'campaign_outcome':
            payload[field] = {'outcome': 'completed', 'reason': 'fixture', 'continuation': {'kind': 'none', 'reason': None}}
        return SimpleNamespace(to_payload=lambda: payload)
    monkeypatch.setattr(campaign_projections, 'project_status', changed)
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('inconsistent projection accepted')
    assert error.value.code == 'export_unavailable'


def test_missing_dependency_reports_safe_unavailability(case):
    (case.seed / 'dependency.dat').unlink()
    with pytest.raises(OwnerError) as error:
        with case.open():
            pytest.fail('missing dependency accepted')
    assert error.value.code == 'export_unavailable'


def test_real_saved_preparation_needs_no_live_environment(tmp_path, monkeypatch, changes, campaign_identity):
    campaign_identity, grouper_kind = campaign_identity
    import shutil
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage
    from test_owner_binding import make_prepared

    inputs = make_prepared(tmp_path)
    storage = PreparationStorage(inputs['state'])
    binding = open_prepared(storage, inputs['prepared'].digest, stage='read')
    state = inputs['state']
    run = state / 'artifacts' / OP / OP
    run.parent.mkdir(parents=True)
    with ArtifactStore.create(run, binding.bundle.manifest.seed_root,
            binding.bundle.manifest.policy, parallel_attempts=0) as store:
        selected = store.champion()
    snapshot = CampaignSnapshot(
        family_version=14, grouper_kind=grouper_kind, identity={**campaign_identity,
            'artifact_store': {'seed_candidate_digest': selected.digest}},
        run_id=derive_run_id(run), alpha_plan={}, rounds={}, champion=('v0', selected.digest),
        champion_dependency_digest=selected.dependency_digest, measurements={},
        accounted=frozenset(), counts={}, denominator=0, alpha_rounds={}, threshold=None,
        finished=None, capacity=None, first_round=1, total_rounds=1)
    view = CampaignView(snapshot, {}, None, {})
    monkeypatch.setattr(campaign_projections, 'inspect_view', lambda *a, **k: view)
    # Only temporary fixture inputs disappear; producing environments stay intact.
    shutil.rmtree(inputs['source'])
    inputs['runtime'].unlink()
    inputs['lab_wheel'].unlink()
    status = SimpleNamespace(id=OP, kind='campaign', prepared_digest=inputs['prepared'].digest)
    with changes.snapshot_changes(state, storage, status,
            ({'kind': 'tree', 'path': f'artifacts/{OP}'},)) as result:
        assert result.no_change
        assert result.prepared_digest == inputs['prepared'].digest
        assert result.bundle_digest == inputs['bundle'].digest
        assert result.distributions == tuple(pin.to_payload() for pin in binding.bundle.execution_environment.distributions)
        assert {pin['name'] for pin in result.distributions} >= {'evolution-lab', 'evo-skills'}
        assert all(len(pin['wheel_sha256']) == 64 for pin in result.distributions)


@pytest.mark.parametrize('kind', ['journal', 'io', 'missing'])
def test_public_projection_read_errors_are_closed(case, monkeypatch, changes, kind):
    from evolution.tree import JournalBoundaryError
    import errno
    errors = {'journal': JournalBoundaryError('journal_unavailable'),
        'io': OSError(errno.EIO, 'private fixture path'),
        'missing': FileNotFoundError(errno.ENOENT, 'private fixture path')}
    def fail(*args, **kwargs):
        raise errors[kind]
    monkeypatch.setattr(campaign_projections, 'project_status', fail)
    with pytest.raises(OwnerError) as error:
        with case.open(stage='export'):
            pytest.fail('unreadable projection accepted')
    assert error.value.code == ('io_error' if kind == 'io' else 'export_unavailable')
    assert error.value.stage == 'export'
    assert 'private fixture path' not in str(error.value)


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
@pytest.mark.parametrize('selected_is_initial', [False, True])
def test_historical_initial_copy_health_only_matters_when_selected(case, damage, selected_is_initial):
    initial = ArtifactStore.inspect(case.run, verify=False).champions[0]
    if selected_is_initial:
        case.view = replace(case.view, snapshot=replace(case.view.snapshot,
            champion=(initial.version_id, initial.candidate_digest)))
    initial_root = case.run / initial.reference.locator
    if damage == 'missing':
        initial_root.rename(initial_root.with_name('displaced-initial'))
    else:
        overwrite(initial_root / 'editable.txt', b'corrupt historical bytes')
    assert ArtifactStore.inspect(case.run, verify=True).champions[0].corruption is not None
    if selected_is_initial:
        with pytest.raises(OwnerError) as error:
            with case.open():
                pytest.fail('unhealthy selected initial exposed')
        assert error.value.code == 'export_unavailable'
    else:
        with case.open() as result:
            assert result.source_files['editable.txt'].data == b'before\n'
            assert result.source_digest == initial.candidate_digest
            assert result.selected_files['editable.txt'].data == b'after\n'
            assert result.selected_version == 'v1'
            assert result.changed_paths == ('editable.txt',)


@pytest.mark.parametrize("failure", ["family", "pin"])
def test_diff_and_export_refuse_before_lineage(case, changes, monkeypatch, failure):
    from evolution.tree.campaign import CampaignFamilyUnsupported
    from evolution_lab.owner.core_raw import CoreRawUnavailable
    def refused(*a, **k):
        raise CampaignFamilyUnsupported(13, reader=14) if failure == "family" else CoreRawUnavailable("core_raw_unsupported")
    monkeypatch.setattr(changes, "campaign_read", refused)
    monkeypatch.setattr(ArtifactStore, "inspect", lambda *a, **k: pytest.fail("lineage read after refusal"))
    for stage in ("diff", "export"):
        with pytest.raises(OwnerError) as raised:
            with case.open(stage=stage):
                pytest.fail("unsupported campaign admitted")
        assert raised.value.code == "export_unavailable"


@pytest.mark.parametrize('reader', ['evolution.tree.campaign_projections.inspect_view',
                                    'evolution.tree.campaign_projections.project_status',
                                    'evolution.tree.project_campaign_result'])
def test_unexpected_projection_typeerror_is_not_a_guard_refusal(case, changes, monkeypatch, reader):
    def fail(*a, **k):
        raise TypeError('unexpected projection error')
    monkeypatch.setattr(reader, fail)
    with pytest.raises(TypeError, match='unexpected projection error'):
        with case.open():
            pytest.fail('unexpected projection failure accepted')


def test_real_old_prefix_refuses_diff_and_export_before_lineage(case, changes, monkeypatch):
    import shutil
    shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                    case.run / 'campaign.jsonl')
    monkeypatch.setattr(campaign_projections, 'inspect_view', REAL_INSPECT_VIEW)
    monkeypatch.setattr(ArtifactStore, 'inspect', lambda *a, **k: pytest.fail('lineage read after refusal'))
    for stage in ('diff', 'export'):
        with pytest.raises(OwnerError) as raised:
            with case.open(stage=stage):
                pytest.fail('old prefix admitted')
        assert (raised.value.code, raised.value.stage) == ('export_unavailable', stage)


def test_bundle_under_symlinked_ancestor_is_accepted(case, tmp_path):
    prefix = tmp_path / 'prefix'
    prefix.symlink_to(tmp_path.parent, target_is_directory=True)
    bundle = prefix / tmp_path.name
    case.binding.bundle.bundle_path = bundle
    case.binding.bundle.manifest.seed_root = bundle / case.seed.relative_to(tmp_path)
    with case.open() as snapshot:
        assert snapshot.source_ref == 'initial'


def test_symlink_below_bundle_root_stays_refused(case, tmp_path):
    import shutil
    bundle = tmp_path / 'bundle'
    copied = bundle / 'trees/seed'
    shutil.copytree(case.seed, copied)
    case.binding.bundle.bundle_path = bundle
    case.binding.bundle.manifest.seed_root = copied
    moved = tmp_path / 'moved-trees'
    copied.parent.rename(moved)
    copied.parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OwnerError):
        with case.open():
            pytest.fail('a symlink below the Bundle root exposed seed bytes')
