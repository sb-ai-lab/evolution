"""Baseline provenance follows the Core-admitted campaign snapshot."""
from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from evolution_lab.owner import core_raw

CAPTURE = Path(__file__).parent / 'fixtures/panel/b15/focus-open/run'


def test_baseline_provenance_reads_cited_baseline():
    snapshot = core_raw.campaign_read(CAPTURE).view.snapshot
    assert core_raw.baseline_provenance_state(CAPTURE, snapshot) == {
        'round': 1, 'measurement_id': 'm-r00-champion', 'samples': 2, 'known': 2, 'admitted': 2}


def test_changed_cited_bytes_are_unavailable(tmp_path):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    raw = journal.read_bytes()
    changed = raw.replace(b'skills/SKILL.md', b'skills/OTHER.md', 1)
    assert changed != raw
    journal.write_bytes(changed)
    with pytest.raises(core_raw.CoreRawUnavailable):
        core_raw.baseline_provenance_state(run, snapshot)


def test_unreported_manifest_skips_read():
    snapshot = core_raw.campaign_read(CAPTURE).view.snapshot
    identity = dict(snapshot.identity)
    identity['policy'] = {**identity['policy'], 'gate': {**identity['policy']['gate'],
        'provenance': {'kind': 'none'}}}
    assert core_raw.baseline_provenance_state(CAPTURE, replace(snapshot, identity=identity)) is None


def test_all_excluded_and_partial_provenance(tmp_path, monkeypatch):
    import evolution.tree
    import evolution.tree.affected_cases as affected

    snapshot = core_raw.campaign_read(CAPTURE).view.snapshot
    original_recovery = evolution.tree.recovery_plan
    original_provenance = affected.baseline_provenance
    def excluded(records):
        plan = original_recovery(records)
        return replace(plan, executions=tuple(replace(row, scoring_state='failed', evaluation=None)
                                              for row in plan.executions))
    def partial(records):
        rows = original_provenance(records)
        first = next(iter(rows))
        return {**rows, first: (replace(rows[first][0], used=None),)}
    monkeypatch.setattr(evolution.tree, 'recovery_plan', excluded)
    monkeypatch.setattr(affected, 'baseline_provenance', partial)
    result = core_raw.baseline_provenance_state(CAPTURE, snapshot)
    assert (result['samples'], result['known'], result['admitted']) == (2, 1, 0)


def test_core_recovery_counts_imported_as_scored():
    from evolution.tree import inspect_journal, recovery_plan
    run = Path(__file__).parent / 'fixtures/panel/b15/focus-first/run'
    journal = run / 'measurements/m-r01-a02-completion'
    decisions = recovery_plan(inspect_journal(journal).records).executions
    imported = [row for row in decisions if row.imported]
    assert imported
    assert all(row.scoring_state == 'finished' and row.evaluation.kind == 'score' for row in imported)


@pytest.mark.parametrize('reported', [True, False])
def test_real_b17_campaign_baseline(tmp_path, monkeypatch, reported):
    """Offline Core campaign with neutral in-process target and command roles."""
    import asyncio
    import json
    import sys
    from evolution.tree.evolve import evolve
    from evolution.tree.execution.interaction import TurnResult
    from b10_campaigns import NeutralInstance, NeutralTarget, SCORER, EDITOR
    from scaffold_16 import scaffold_16

    if not reported:
        async def send(self, session, message, request_id):
            return TurnResult(events=({'type': 'message', 'text': message.content},),
                              usage=None, latency_ms=1, outcome='ok')
        monkeypatch.setattr(NeutralInstance, 'send', send)
    sample = scaffold_16(tmp_path, mode='point')
    document = json.loads(sample.manifest.read_text())
    for role, program, member in (('scorer', SCORER, 'score.py'), ('editor', EDITOR, 'edit.py')):
        (tmp_path / role / member).write_text(program)
        document[role]['argv'] = [sys.executable, member]
    document['scorer']['metric_contract'] = {
        'version': 'metric_contract/1', 'kind': 'pointwise', 'target_fields': []}
    group = {'root_cause': 'r', 'suggested_fix': 'f', 'evidence_ids': ['obs:c1:1'],
             'affected_cases': ['c1'], 'target_paths': ['skills/SKILL.md'],
             'counterevidence': 'c', 'regression_risks': 'r', 'why_this_layer': 'w'}
    diagnosis = {'status': 'reflected', 'note': 'neutral', 'groups': [group]}
    (tmp_path / 'reflector/reflect.py').write_text('print(' + repr(json.dumps(diagnosis)) + ')\n')
    document['reflector']['argv'] = [sys.executable, 'reflect.py']
    rows = [{'case_id': case, 'group_id': 'g1', 'stratum': 's1',
             'public': {'text': 'hello', 'case_id': case}, 'oracle': {'critical': True},
             'reflection_metadata': {'uses': []}} for case in ('c1', 'c2')]
    (tmp_path / 'corpus.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    document['campaign'].update(rounds=1, attempts_per_round=1, max_concurrency=1,
                                baseline_refresh_after_rounds=0, random_seed=7)
    sample.manifest.write_text(json.dumps(document, indent=2) + '\n')
    run = tmp_path / 'run'
    target = NeutralTarget()
    result = asyncio.run(evolve(sample.manifest, run, adapter_factory=lambda *_a, **_k: target))
    assert result.terminal == 'finished'
    assert not target.live
    snapshot = core_raw.campaign_read(run).view.snapshot
    facts = core_raw.baseline_provenance_state(run, snapshot)
    assert facts['samples'] == facts['admitted'] > 0
    assert facts['known'] == (facts['samples'] if reported else 0)


def test_symlinked_baseline_is_unavailable(tmp_path):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    outside = tmp_path / 'outside'
    journal.rename(outside)
    journal.symlink_to(outside)
    with pytest.raises(core_raw.CoreRawUnavailable):
        core_raw.baseline_provenance_state(run, snapshot)


def test_no_completed_baseline_returns_none():
    snapshot = core_raw.campaign_read(CAPTURE).view.snapshot
    snapshot = replace(snapshot, rounds={n: replace(s, baseline_complete=False)
                                        for n, s in snapshot.rounds.items()})
    assert core_raw.baseline_provenance_state(CAPTURE, snapshot) is None


def test_imported_latest_baseline_is_counted():
    run = Path(__file__).parent / 'fixtures/panel/family15/run/bba08804984a456c81c5b7e09e545254'
    snapshot = core_raw.campaign_read(run).view.snapshot
    facts = core_raw.baseline_provenance_state(run, snapshot)
    assert facts == {'round': 2, 'measurement_id': 'm-r01-a01-completion',
                     'samples': 2, 'known': 0, 'admitted': 2}


def test_large_cited_prefix_and_large_uncited_tail(tmp_path):
    """A valid Core prefix can exceed the generic disclosure-section cap."""
    import hashlib
    import json
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    # JSON whitespace preserves the real Core envelopes/lifecycle. Each record
    # stays below Core's 1 MiB cap; the 41-record prefix exceeds 32 MiB.
    raw = b''.join(line + b' ' * (850_000 - len(line)) + b'\n'
                   for line in journal.read_bytes().splitlines())
    assert len(raw) > 32 * 1024 * 1024
    journal.write_bytes(raw)
    campaign = run / 'campaign.jsonl'
    records = [json.loads(line) for line in campaign.read_bytes().splitlines()]
    for record in records:
        if record['event'] == 'baseline_complete':
            ref = record['payload']['evidence'][0]
            ref['locator'] = f'measurements/m-r00-champion/events.jsonl#bytes=0:{len(raw)}'
            ref['content_digest'] = hashlib.sha256(raw).hexdigest()
            records = records[:records.index(record) + 1]
            break
    campaign.write_text(''.join(json.dumps(row) + '\n' for row in records))
    snapshot = core_raw.campaign_read(run).view.snapshot
    # An untrusted tail is outside the citation and must not be read or parsed.
    with journal.open('ab') as handle:
        handle.truncate(300 * 1024 * 1024)
    assert core_raw.baseline_provenance_state(run, snapshot) == {
        'round': 1, 'measurement_id': 'm-r00-champion', 'samples': 2, 'known': 2, 'admitted': 2}


@pytest.mark.parametrize('tail,available', [(b'{"event":', True), (b'broken\n', False)])
def test_campaign_tail_preserves_only_valid_admitted_prefix(tmp_path, tail, available):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    with (run / 'campaign.jsonl').open('ab') as handle:
        handle.write(tail)
    if available:
        assert core_raw.baseline_provenance_state(run, snapshot)['known'] == 2
    else:
        with pytest.raises(core_raw.CoreRawUnavailable):
            core_raw.baseline_provenance_state(run, snapshot)


def test_baseline_cache_reuses_success_invalidates_and_retries(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    cache = core_raw.BaselineProvenanceCache()
    expected = cache.read(run, snapshot)
    actual_read = core_raw.baseline_provenance_state
    calls = []
    def counted(*args, **kwargs):
        calls.append(args)
        return actual_read(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'baseline_provenance_state', counted)
    expected['known'] = -1
    assert cache.read(run, snapshot)['known'] == 2
    assert calls == []
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    raw = journal.read_bytes()
    journal.write_bytes(raw.replace(b'skills/SKILL.md', b'skills/OTHER.md', 1))
    for _ in range(2):
        with pytest.raises(core_raw.CoreRawUnavailable):
            cache.read(run, snapshot)
    assert len(calls) == 2
    journal.write_bytes(raw)
    assert cache.read(run, snapshot)['known'] == 2
    cache.drop(run)
    assert cache.read(run, snapshot)['known'] == 2
    assert len(calls) == 4


def test_baseline_cache_open_audit_matches_its_narrow_key(tmp_path, monkeypatch):
    import os
    from test_owner_projection_open_audit import opened_paths, outside_inputs
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    read = core_raw.campaign_read(run, with_bindings=True)
    snapshot = read.view.snapshot
    cache = core_raw.BaselineProvenanceCache()
    evidence = core_raw._baseline_evidence(run, snapshot, read)
    signature = cache.signature(run, evidence)
    signed = {Path(path) for path, _ in signature}
    with opened_paths(monkeypatch) as opened:
        assert cache.read(run, snapshot, read=read)['known'] == 2
    assert not outside_inputs(opened, signed)
    for path in signed:
        assert outside_inputs(opened, signed - {path}), path
    with opened_paths(monkeypatch) as opened:
        assert cache.read(run, snapshot, read=read)['known'] == 2
    assert not [path for path, _, flags in opened if not flags & os.O_DIRECTORY]


@pytest.mark.parametrize('change', ['partial_tail', 'during_read', 'signature_failure'])
def test_baseline_cache_does_not_retain_incomplete_or_unstable_reads(tmp_path, monkeypatch, change):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    cache = core_raw.BaselineProvenanceCache()
    journal = run / 'campaign.jsonl'
    if change == 'partial_tail':
        with journal.open('ab') as handle:
            handle.write(b'{')
    if change == 'signature_failure':
        monkeypatch.setattr(cache, 'signature', lambda *_: None)
    original = core_raw.baseline_provenance_state
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        value = original(*args, **kwargs)
        if change == 'during_read' and len(calls) == 1:
            # A changed input after authenticating the prefix cannot fill cache.
            (run / 'measurements/m-r00-champion/events.jsonl').touch()
        return value
    monkeypatch.setattr(core_raw, 'baseline_provenance_state', read)
    assert cache.read(run, snapshot)['known'] == 2
    assert cache.read(run, snapshot)['known'] == 2
    assert len(calls) == 2


@pytest.mark.parametrize('part', ['campaign.jsonl', 'measurements/m-r00-champion/events.jsonl', 'measurements'])
def test_baseline_cache_rejects_replaced_symlink_input(tmp_path, part):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    cache = core_raw.BaselineProvenanceCache()
    assert cache.read(run, snapshot)['known'] == 2
    path = run / part
    outside = tmp_path / 'outside'
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(core_raw.CoreRawUnavailable):
        cache.read(run, snapshot)


def test_cited_prefix_read_is_exact_and_keeps_section_bound(tmp_path, monkeypatch):
    import os
    (tmp_path / 'events.jsonl').write_bytes(b'abc\n' + b'untrusted tail')
    requested = []
    original = os.read
    def read(fd, count):
        requested.append(count)
        return original(fd, count)
    monkeypatch.setattr(os, 'read', read)
    assert core_raw.read_inside(tmp_path, 'events.jsonl', prefix_bytes=4, limit=4) == b'abc\n'
    assert requested == [4]
    with pytest.raises(core_raw.CoreRawUnavailable, match='too_large'):
        core_raw.read_inside(tmp_path, 'events.jsonl', limit=4)
    with pytest.raises(core_raw.CoreRawUnavailable, match='too_large'):
        core_raw.read_inside(tmp_path, 'events.jsonl', limit=4, prefix_bytes=5)
    with pytest.raises(core_raw.CoreRawUnavailable, match='unreadable'):
        core_raw.read_inside(tmp_path, 'events.jsonl', prefix_bytes=100)


@pytest.mark.parametrize('capture,family', [('b15/focus-open/run', 15), ('family14/run/071de7faeeb34743b121d3e4e14867e8', 14)])
@pytest.mark.parametrize('share_read', [False, True])
def test_baseline_cache_survives_real_campaign_append(tmp_path, monkeypatch, capture, family, share_read):
    import json
    run = tmp_path / 'run'
    shutil.copytree(Path(__file__).parent / 'fixtures/panel' / capture, run)
    journal = run / 'campaign.jsonl'
    lines = journal.read_bytes().splitlines(keepends=True)
    end = next(i + 1 for i, line in enumerate(lines) if json.loads(line)['event'] == 'baseline_complete')
    journal.write_bytes(b''.join(lines[:end]))
    cache = core_raw.BaselineProvenanceCache()
    first = core_raw.campaign_read(run, with_bindings=True)
    assert first.family == family
    expected = cache.read(run, first.view.snapshot, **({"read": first} if share_read else {}))
    with journal.open('ab') as handle:
        handle.write(b''.join(lines[end:end + 3]))
    fresh = core_raw.campaign_read(run, with_bindings=True)
    assert fresh.view.snapshot != first.view.snapshot
    reads = []
    actual = core_raw.read_inside
    def read(*args, **kwargs):
        reads.append(args)
        return actual(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'read_inside', read)
    if share_read:
        def duplicate_replay(*args):
            pytest.fail('an admitted request prefix must not be replayed again')
        monkeypatch.setattr(core_raw, '_family15_prefix', duplicate_replay)
    assert cache.read(run, fresh.view.snapshot, **({"read": fresh} if share_read else {})) == expected
    assert reads == [], 'an unchanged cited baseline must not be reopened after campaign progress'


def test_baseline_cache_tracks_latest_completion_and_request_snapshot(tmp_path):
    import json
    capture = Path(__file__).parent / 'fixtures/panel/family15/run/bba08804984a456c81c5b7e09e545254'
    run = tmp_path / 'run'
    shutil.copytree(capture, run)
    journal = run / 'campaign.jsonl'
    lines = journal.read_bytes().splitlines(keepends=True)
    end = next(i + 1 for i, line in enumerate(lines) if json.loads(line)['event'] == 'baseline_complete')
    journal.write_bytes(b''.join(lines[:end]))
    first = core_raw.campaign_read(run, with_bindings=True)
    cache = core_raw.BaselineProvenanceCache()
    one = cache.read(run, first.view.snapshot, read=first)
    assert one['round'] == 1
    with journal.open('ab') as handle:
        handle.write(b''.join(lines[end:]))
    # Appended work cannot replace the completion of an already admitted request.
    assert cache.read(run, first.view.snapshot, read=first) == one
    latest = core_raw.campaign_read(run, with_bindings=True)
    two = cache.read(run, latest.view.snapshot, read=latest)
    assert two['round'] == 2 and two['measurement_id'] == 'm-r01-a01-completion'
    assert two['known'] == 0 and two['admitted'] == 2
    with pytest.raises(core_raw.CoreRawUnavailable):
        cache.read(run, latest.view.snapshot, read=first)


@pytest.mark.parametrize('tail', [b'{', b'broken\n'])
def test_baseline_cache_never_serves_stale_hit_for_partial_or_corrupt_campaign(tmp_path, monkeypatch, tail):
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    first = core_raw.campaign_read(run, with_bindings=True)
    cache = core_raw.BaselineProvenanceCache()
    assert cache.read(run, first.view.snapshot, read=first)['known'] == 2
    with (run / 'campaign.jsonl').open('ab') as handle:
        handle.write(tail)
    fresh = core_raw.campaign_read(run, with_bindings=True)
    calls = []
    original = core_raw.read_inside
    def read(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'read_inside', read)
    if tail == b'{':
        for _ in range(2):
            assert cache.read(run, fresh.view.snapshot, read=fresh)['known'] == 2
        assert len(calls) == 2
    else:
        for _ in range(2):
            with pytest.raises(core_raw.CoreRawUnavailable):
                cache.read(run, fresh.view.snapshot, read=fresh)
        assert calls == []


@pytest.mark.parametrize('change', ['digest', 'locator'])
def test_changed_completion_cannot_reuse_previous_digest_verified_aggregate(tmp_path, change):
    import json
    run = tmp_path / 'run'
    shutil.copytree(CAPTURE, run)
    journal = run / 'campaign.jsonl'
    records = [json.loads(line) for line in journal.read_bytes().splitlines()]
    end = next(i + 1 for i, row in enumerate(records) if row['event'] == 'baseline_complete')
    records = records[:end]
    journal.write_text(''.join(json.dumps(row) + '\n' for row in records))
    first = core_raw.campaign_read(run, with_bindings=True)
    cache = core_raw.BaselineProvenanceCache()
    assert cache.read(run, first.view.snapshot, read=first)['known'] == 2
    reference = records[-1]['payload']['evidence'][0]
    if change == 'digest':
        reference['content_digest'] = '0' * 64
    else:
        reference['locator'] = reference['locator'].split('#')[0] + '#bytes=0:1'
    journal.write_text(''.join(json.dumps(row) + '\n' for row in records))
    fresh = core_raw.campaign_read(run, with_bindings=True)
    assert fresh.records is not None
    with pytest.raises(core_raw.CoreRawUnavailable):
        cache.read(run, fresh.view.snapshot, read=fresh)


@pytest.mark.parametrize('capture', ['b15/focus-open/run', 'family14/run/071de7faeeb34743b121d3e4e14867e8'])
def test_baseline_records_do_not_change_core_status_or_status_only_io(monkeypatch, capture):
    run = Path(__file__).parent / 'fixtures/panel' / capture
    plain = core_raw.campaign_read(run)
    with_records = core_raw.campaign_read(run, with_bindings=True)
    assert with_records.records and with_records.journal_integrity == 'complete'
    assert with_records.status.to_payload() == plain.status.to_payload()
    def unused(*args):
        pytest.fail('status-only reads do not authenticate baseline evidence')
    monkeypatch.setattr(core_raw, '_family15_prefix', unused)
    assert core_raw.campaign_read(run).status.to_payload() == plain.status.to_payload()
