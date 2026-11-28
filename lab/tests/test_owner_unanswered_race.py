"""Neutral installed-worker regression: count and coverage must share a snapshot."""
import asyncio

import pytest

from evolution_lab.owner import Owner, ResourceRef
from evolution_lab.owner.models import _thaw_json
from test_owner_evaluation import _prepared, _ready_target

pytestmark = pytest.mark.slow


@pytest.mark.parametrize('snapshot', ['journal_advanced', 'newer_count'])
def test_live_three_case_evaluation_keeps_count_consistent_with_coverage(tmp_path, monkeypatch, snapshot):
    release = tmp_path / 'continue-scoring'
    source = '''import json, sys, time
from pathlib import Path
value = json.load(sys.stdin)
if value['case_id'] != 'c-1':
    deadline = time.monotonic() + 20
    while not Path(%r).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('neutral synchronization timeout')
        time.sleep(.01)
json.dump({'metrics': {'score': 1.0}, 'critical_pass': True, 'verdicts': [
    {'criterion': 'answered', 'passed': value['case_id'] != 'c-2'},
    {'criterion': 'matched', 'passed': value['case_id'] == 'c-3'}]}, sys.stdout)
''' % str(release)
    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch, case_ids=('c-1', 'c-2', 'c-3'),
        repeat_seeds=(17,), scorer_source=source, critical_cases=True)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target,
                secrets={'SCORER_TOKEN': 'neutral-test-only'})
            ref = ResourceRef('operation', accepted.id)
            original = owner._unanswered_counts
            observed = []
            advanced = False

            async def count_after_worker_progresses(status, core):
                nonlocal advanced
                if not advanced and status.result.coverage.scored == 1:
                    advanced = True
                    # Keep the public status's older Core snapshot, then let
                    # the real subprocess finish the remaining two samples.
                    release.touch()
                    await asyncio.wait_for(asyncio.shield(owner._operations[ref.id].done), 20)
                    if snapshot == 'newer_count':
                        latest = await owner.status(ref)
                        return await original(latest, _thaw_json(latest.result.core))
                return await original(status, core)

            monkeypatch.setattr(owner, '_unanswered_counts', count_after_worker_progresses)
            try:
                async with asyncio.timeout(25):
                    while not advanced:
                        value = await owner.status(ref)
                        if value.result is not None and value.result.coverage is not None:
                            observed.append(value)
                        await asyncio.sleep(.01)
                during = next(row for row in observed if row.result.coverage.scored == 1)
                if snapshot == 'newer_count':
                    assert during.result.unanswered_state == 'unavailable'
                    assert during.result.unanswered is during.result.answered_reported is None
                else:
                    assert during.result.unanswered_state == 'present'
                    assert during.result.answered_reported == 1
                final = await owner.status(ref)
                assert final.result.coverage.to_payload() == {'expected': 3, 'scored': 3}
                assert (final.result.unanswered_state, final.result.unanswered,
                        final.result.answered_reported) == ('present', 1, 3)
                assert final.process.exit_code == 0 and final.control_state == 'finished'
                # The same public result remains consistent on a cache hit.
                assert (await owner.status(ref)).result.to_payload() == final.result.to_payload()
            finally:
                release.touch()
    asyncio.run(scenario())
