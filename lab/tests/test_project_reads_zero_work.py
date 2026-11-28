"""Project read integration: real catalogue loaders, projection work and wire bytes."""
import asyncio
from collections import Counter

import pytest

from evolution_lab import guides
from evolution_lab.guides import _scenarios
from evolution_lab.owner import readers
from dev67_reference import reference_project

pytestmark = pytest.mark.slow

FACTS = '/v1/projects/acq/facts?implementation=port-a'
STATUS = '/v1/projects/acq/status?implementation=port-a'
OPERATIONS = '/v1/projects/acq/operations?implementation=port-a'


async def get(stand, path):
    response = await stand.client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def count_work(stand, patch):
    counts = Counter()
    projected = Counter()
    original_project = readers.project_operation
    original_times = readers._times_and_facts
    original_read = _scenarios.read_catalogue
    original_validate = guides.validate_catalogue
    original_execute = stand.projects.store._execute

    def project(root, status, **kwargs):
        counts['project'] += 1
        projected[status.id] += 1
        return original_project(root, status, **kwargs)

    def times(*args, **kwargs):
        counts['times'] += 1
        return original_times(*args, **kwargs)

    def read(*args, **kwargs):
        counts['scenarios'] += 1
        return original_read(*args, **kwargs)

    def validate(*args, **kwargs):
        counts['requirements'] += 1
        return original_validate(*args, **kwargs)

    def execute(sql, params=()):
        if sql.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            counts['write'] += 1
        return original_execute(sql, params)

    patch.setattr(readers, 'project_operation', project)
    patch.setattr(readers, '_times_and_facts', times)
    patch.setattr(_scenarios, 'read_catalogue', read)
    patch.setattr(guides, 'validate_catalogue', validate)
    patch.setattr(stand.projects.store, '_execute', execute)
    return counts, projected


def test_repeated_project_reads_do_zero_work_then_project_one_append(tmp_path):
    async def scenario():
        async with reference_project(tmp_path, real_catalogues=True) as stand:
            with pytest.MonkeyPatch.context() as patch:
                counts, projected = count_work(stand, patch)
                getattr(stand.owner, '_projection_cache', {}).clear()
                await get(stand, FACTS)
                status = await get(stand, STATUS)
                await get(stand, '/v1/guides/requirements')
                assert counts['scenarios'] == counts['requirements'] == 1
                assert counts['project'] == counts['times'] == 3
                assert counts['write'] > 0  # Prove the registry counter sees real writes.
                assert projected == Counter(stand.operation_ids.values())
                step = status['scenarios'][0]['steps'][0]
                assert step['id'] == 'map' and step['state'] == 'done'
                counts.clear()
                projected.clear()
                await get(stand, FACTS)
                await get(stand, STATUS)
                await get(stand, '/v1/guides/requirements')
                assert not counts, dict(counts)
                assert not projected

                operation_id = stand.operation_ids['campaign-running']
                from test_owner_projection_equivalence import append_hygiene
                append_hygiene(tmp_path / 'state/artifacts' / operation_id / operation_id)
                await get(stand, STATUS)
                assert counts['project'] == counts['times'] == 1
                assert projected == {operation_id: 1}
                assert counts['scenarios'] == counts['requirements'] == 0
                counts.clear()
                projected.clear()
                await get(stand, FACTS)
                await get(stand, STATUS)
                assert not counts and not projected
    asyncio.run(scenario())


def test_one_cold_status_projects_each_operation_only_once(tmp_path):
    async def scenario():
        async with reference_project(tmp_path, real_catalogues=True) as stand:
            getattr(stand.owner, '_projection_cache', {}).clear()
            with pytest.MonkeyPatch.context() as patch:
                counts, projected = count_work(stand, patch)
                await get(stand, STATUS)
                assert projected == Counter(stand.operation_ids.values())
                assert counts['project'] == counts['times'] == 3
    asyncio.run(scenario())


def test_last_seen_at_is_snapshot_change_time(tmp_path):
    async def scenario():
        from evolution_lab.projects import session
        async with reference_project(tmp_path) as stand:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(session, 'utc_now', lambda: '2026-11-01T01:00:00Z')
                before = await get(stand, OPERATIONS)
                patch.setattr(session, 'utc_now', lambda: '2026-11-01T02:00:00Z')
                await get(stand, FACTS)
                await get(stand, STATUS)
                unchanged = await get(stand, OPERATIONS)
                assert unchanged == before
                operation_id = stand.operation_ids['campaign-running']
                await stand.owner._db('mark_operation_cleanup_required', operation_id)
                after = await get(stand, OPERATIONS)
                rows_before = {row['object_id']: row for row in before['links']}
                rows_after = {row['object_id']: row for row in after['links']}
                for identifier, row in rows_after.items():
                    if identifier == operation_id:
                        assert rows_before[identifier]['last_seen']['at'] == '2026-11-01T01:00:00Z'
                        assert row['last_seen']['at'] == '2026-11-01T02:00:00Z'
                        assert row['last_seen']['control_state'] == 'cleanup_required'
                    else:
                        assert row == rows_before[identifier]
    asyncio.run(scenario())



def test_six_operation_reference_respects_owner_execution_slot(tmp_path):
    async def scenario():
        async with reference_project(tmp_path, real_catalogues=True, operation_copies=2) as stand:
            payload = await get(stand, OPERATIONS)
            rows = payload['links']
            assert len(rows) == len({row['object_id'] for row in rows}) == 6
            assert Counter(row['last_seen']['control_state'] for row in rows) == {
                'finished': 5, 'running': 1}
            assert Counter(row['last_seen']['kind'] for row in rows) == {
                'campaign': 4, 'evaluation': 2}
    asyncio.run(scenario())
