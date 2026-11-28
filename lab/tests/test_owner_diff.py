"""S4 direct Owner diff and neutral snapshot file-change contracts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from evolution_lab.owner import Owner, OwnerError, ResourceRef, Failure
from evolution_lab.owner.changes import SnapshotFile
from test_owner_changes import OP, campaign_identity, case, changes
from test_owner_results_state import accepted
from test_owner_state import _ready_target, _executor


def snapshot(before, after, *, changed=None, same_identity=False):
    paths = tuple(sorted(set(before) | set(after))) if changed is None else changed
    return SimpleNamespace(operation_id=OP, source_digest='0' * 64,
        selected_digest=('0' if same_identity else '1') * 64,
        source_files=before, selected_files=after, changed_paths=paths,
        no_change=same_identity, source_ref='initial', selected_ref='champion:v1')


def test_ordered_change_set_encodes_add_modify_delete_mode_and_rename():
    from evolution_lab.owner.results import ordered_changes
    before = {'z.txt': SnapshotFile(b'old\n', 0),
        'mode': SnapshotFile(b'same', 0), 'old-name': SnapshotFile(b'renamed', 0),
        'both': SnapshotFile(b'before', 0)}
    after = {'a.txt': SnapshotFile(b'new\n', 0),
        'mode': SnapshotFile(b'same', 1), 'new-name': SnapshotFile(b'renamed', 0),
        'both': SnapshotFile(b'after', 1)}
    changes = ordered_changes(snapshot(before, after))
    assert [(c.path, c.kind) for c in changes] == [
        ('a.txt', 'add'), ('both', 'modify'), ('mode', 'mode'),
        ('new-name', 'add'), ('old-name', 'delete'), ('z.txt', 'delete')]
    assert changes[0].before_sha256 is None and changes[0].before_executable is None
    assert changes[0].after_executable == 0 and changes[0].payload == b'new\n'
    assert changes[1].before_executable == 0 and changes[1].after_executable == 1
    assert changes[2].payload is None and changes[2].after_executable == 1
    assert changes[-1].after_sha256 is None and changes[-1].payload is None
    assert len(changes[0].after_sha256) == 64
    assert changes[0].manifest_entry(0)['payload'] == 'payload/00000000'
    assert changes[2].manifest_entry(2)['payload'] is None


def test_unified_diff_preserves_no_final_newline_and_dev_null():
    from evolution_lab.owner.results import diff_snapshot
    result = diff_snapshot(snapshot({'remove.txt': SnapshotFile(b'gone', 0),
        'edit.txt': SnapshotFile(b'old', 0)},
        {'add.txt': SnapshotFile(b'new', 0), 'edit.txt': SnapshotFile(b'changed', 0)}))
    assert result.state == 'changed'
    assert '--- /dev/null\n+++ b/add.txt\n' in result.text
    assert '--- a/remove.txt\n+++ /dev/null\n' in result.text
    assert '--- a/edit.txt\n+++ b/edit.txt\n' in result.text
    assert result.text.count('\\ No newline at end of file\n') == 4


@pytest.mark.parametrize('separator', ['\v', '\f', '\r', '\u0085', '\u2028', '\u2029'])
def test_unified_diff_only_lf_ends_a_line(separator):
    from evolution_lab.owner.results import diff_snapshot
    before = f'a{separator}b\n'.encode('utf-8')
    after = f'a{separator}c\n'.encode('utf-8')
    result = diff_snapshot(snapshot({'x.txt': SnapshotFile(before, 0)},
        {'x.txt': SnapshotFile(after, 0)}))
    assert '@@ -1 +1 @@\n' in result.text
    assert f'-a{separator}b\n+a{separator}c\n' in result.text
    assert '\\ No newline at end of file\n' not in result.text


def test_binary_and_executable_changes_are_explicit_nontext():
    from evolution_lab.owner.results import diff_snapshot
    result = diff_snapshot(snapshot({'binary': SnapshotFile(b'\x00old', 0),
        'mode': SnapshotFile(b'old\n', 0)},
        {'binary': SnapshotFile(b'\x00new', 0),
        'mode': SnapshotFile(b'new\n', 1)}))
    assert '--- a/mode\n+++ b/mode\n' in result.text
    assert '-old\n+new\n' in result.text
    assert result.nontext_changes == (
        {'path': 'binary', 'kind': 'binary'}, {'path': 'mode', 'kind': 'mode'})


def test_mode_only_has_no_fabricated_text_hunk():
    from evolution_lab.owner.results import diff_snapshot
    result = diff_snapshot(snapshot({'x': SnapshotFile(b'same\n', 0)},
        {'x': SnapshotFile(b'same\n', 1)}))
    assert result.text == ''
    assert result.nontext_changes == ({'path': 'x', 'kind': 'mode'},)


def test_same_tree_new_version_is_no_change():
    from evolution_lab.owner.results import diff_snapshot, ordered_changes
    value = snapshot({'x': SnapshotFile(b'same', 0)},
        {'x': SnapshotFile(b'same', 0)}, changed=(), same_identity=True)
    assert ordered_changes(value) == ()
    assert diff_snapshot(value).state == 'no_change'
    assert diff_snapshot(value).text == ''


def test_empty_change_list_without_matching_identity_is_unavailable():
    from evolution_lab.owner.results import diff_snapshot
    with pytest.raises(OwnerError) as error:
        diff_snapshot(snapshot({}, {}, changed=()))
    assert (error.value.code, error.value.stage) == ('export_unavailable', 'diff')


def test_invalid_utf8_and_binary_mode_are_both_explicit():
    from evolution_lab.owner.results import diff_snapshot
    result = diff_snapshot(snapshot({'x': SnapshotFile(b'\xff', 0)},
        {'x': SnapshotFile(b'\xfe', 1)}))
    assert result.text == ''
    assert result.nontext_changes == (
        {'path': 'x', 'kind': 'binary'}, {'path': 'x', 'kind': 'mode'})


def test_diff_refuses_evaluation_and_unreleased_states_without_snapshot(tmp_path, monkeypatch):
    from evolution_lab.owner import changes
    called = []
    monkeypatch.setattr(changes, 'snapshot_changes', lambda *a, **k: called.append(True))
    async def scenario():
        for kind, expected in [('campaign', 'resource_busy'),
                               ('evaluation', 'export_unavailable')]:
            async with Owner.open(tmp_path / kind) as owner:
                operation = await owner._loop.run_in_executor(owner._executor,
                    accepted, owner._store, kind)
                with pytest.raises(OwnerError) as error:
                    await owner.diff(ResourceRef('operation', operation.status.id))
                assert (error.value.code, error.value.stage) == (expected, 'diff')
    asyncio.run(scenario())
    assert called == []


def test_diff_rejects_non_operation_ref(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            with pytest.raises(OwnerError) as error:
                await owner.diff(ResourceRef('target', OP))
            assert (error.value.code, error.value.stage) == ('invalid_argument', 'diff')
    asyncio.run(scenario())


def test_direct_diff_uses_saved_snapshot_without_durable_task(case):
    async def scenario():
        async with Owner.open(case.state) as owner:
            target = await owner._loop.run_in_executor(owner._executor,
                _ready_target, owner._store)
            await owner._db('accept_operation', prepared_digest=case.status.prepared_digest,
                target_ref=target, executor=_executor(), kind='campaign', operation_id=OP)
            await owner._db('settle_pre_dispatch_failure', OP,
                Failure.make('dispatch_refused', 'before_start'))
            await owner._db('update_operation', OP, results_state='present')
            result = await owner.diff(ResourceRef('operation', OP))
            assert result.state == 'changed'
            assert '--- a/editable.txt\n+++ b/editable.txt\n' in result.text
            assert '-before\n+after\n' in result.text
            assert owner._tasks == {}
            assert not list((case.run.parent / 'staging').glob('*'))
    asyncio.run(scenario())


def test_deleted_evaluation_returns_results_deleted(tmp_path):
    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            operation = await owner._loop.run_in_executor(owner._executor,
                accepted, owner._store, 'evaluation')
            await owner._db('settle_pre_dispatch_failure', operation.status.id,
                Failure.make('dispatch_refused', 'before_start'))
            deletion = await owner._db('accept_deletion', operation.status.id)
            await owner._db('finish_deletion', operation.status.id, deletion.task_ref.id)
            with pytest.raises(OwnerError) as error:
                await owner.diff(ResourceRef('operation', operation.status.id))
            assert (error.value.code, error.value.stage) == ('results_deleted', 'diff')
    asyncio.run(scenario())


# Panel 1a Task 6: a diff between any pair of campaign trees, lab-operation-diff/2.
from evolution_lab.owner.changes import parse_tree_ref
from test_owner_core_raw import finished_campaign  # noqa: F401,E402 — module fixture
from test_owner_measurements import service_cli_with_finished_campaign  # noqa: F401,E402 — fixture


@pytest.mark.parametrize("value,expected", [
    ("initial", ("initial", None)), ("champion:v2", ("champion", "v2")), ("attempt:r01-a02", ("attempt", "r01-a02")),
])
def test_parse(value, expected):
    assert parse_tree_ref(value) == expected


@pytest.mark.parametrize("value", ["", "champion:", "champion:x2", "attempt:../x", "final", "attempt:a b",
                                   "initial\n", "attempt:x\n"])
def test_parse_refuses(value):
    from evolution_lab.owner.models import OwnerError
    with pytest.raises(OwnerError) as caught:
        parse_tree_ref(value)
    assert caught.value.code == "invalid_argument"


def test_default_diff_is_unchanged_but_versioned(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    value = route(f"/v1/operations/{operation}/diff")
    assert value["schema"] == "lab-operation-diff/2"
    assert value["from"] == "initial" and value["to"].startswith("champion:")


def test_diff_between_two_trees(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    view = cli("campaign", operation)[-1]
    attempt = next(a["attempt_id"] for r in view["rounds"] for a in r["attempts"] if a["has_tree"])
    value = cli("diff", operation, "--from", "champion:v0", "--to", f"attempt:{attempt}")[-1]
    assert value["from"] == "champion:v0" and value["to"] == f"attempt:{attempt}"
    assert value["state"] in {"changed", "no_change"}


def test_diff_of_an_attempt_without_a_tree_is_tree_unavailable(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    view = cli("campaign", operation)[-1]
    missing = [a["attempt_id"] for r in view["rounds"] for a in r["attempts"] if not a["has_tree"]]
    if not missing:
        pytest.skip("the fixture campaign has no attempt without a tree")
    result = cli("diff", operation, "--to", f"attempt:{missing[0]}", check=False, raw=True)
    assert result.returncode != 0 and "tree_unavailable" in result.stdout + result.stderr


def test_selected_tree_refuses_an_attempt_without_a_published_tree():
    """Review Focus: an abandoned candidate has no reference, so it has no tree to diff."""
    from evolution_lab.owner.changes import _selected_tree
    abandoned = SimpleNamespace(attempt_id="r01-a02", parent_id="v0", state="abandoned",
                                candidate_digest=None, dependency_digest=None, reference=None, corruption=None)
    lineage = SimpleNamespace(run_id="run", champions=(), candidates=(abandoned,))
    with pytest.raises(OwnerError) as caught:
        _selected_tree("attempt", "r01-a02", {}, lineage)
    assert (caught.value.code, caught.value.stage) == ("tree_unavailable", "diff")
    with pytest.raises(OwnerError) as caught:
        _selected_tree("attempt", "r09-a09", {}, lineage)
    assert (caught.value.code, caught.value.stage) == ("not_found", "diff")
