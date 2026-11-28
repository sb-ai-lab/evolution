from pathlib import Path

from evolution_lab.owner import core_raw
from evolution_lab.report import _lab_fields

RUN = Path(__file__).parent / 'fixtures/panel/family15/run/bba08804984a456c81c5b7e09e545254'


def test_campaign_report_notices_latest_unknown_baseline():
    read = core_raw.campaign_read(RUN, with_result=True)
    fields = _lab_fields(RUN, read.result.to_payload(), read.family, read)
    assert fields['lab_notices_state'] == 'present'
    assert {'code': 'baseline_provenance_unknown', 'round': 2,
            'measurement_id': 'm-r01-a01-completion', 'samples': 2} in fields['lab_notices']


def test_campaign_notice_presentation():
    from evolution_lab.cli.output import notice_line
    from evolution_lab.report import _notice_md
    notice = {'code': 'baseline_provenance_unknown', 'round': 2,
              'measurement_id': 'm-r01-a01-completion', 'samples': 2}
    assert 'no stored provenance' in notice_line(notice).lower()
    assert 'all train' in notice_line(notice).lower()
    text = _notice_md(notice)
    assert 'не хранит provenance' in text
    assert 'всему train' in text
    assert 'm-r01-a01-completion' in text


def test_unreadable_baseline_is_unavailable_then_recovers(tmp_path):
    import shutil
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    run = tmp_path / 'run'
    shutil.copytree(RUN, run)
    snapshot = core_raw.campaign_read(run).view.snapshot
    journal = run / 'measurements/m-r01-a01-completion/events.jsonl'
    original = journal.read_bytes()
    journal.write_bytes(b'bad')
    assert baseline_notice_fields(run, snapshot) == {
        'lab_notices': [], 'lab_notices_state': 'unavailable'}
    journal.write_bytes(original)
    assert baseline_notice_fields(run, snapshot)['lab_notices'][0]['round'] == 2


def test_known_baseline_has_no_notice():
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    run = Path(__file__).parent / 'fixtures/panel/b15/focus-open/run'
    snapshot = core_raw.campaign_read(run).view.snapshot
    assert baseline_notice_fields(run, snapshot) == {'lab_notices': [], 'lab_notices_state': 'present'}


def test_owner_report_rejects_malformed_campaign_notice():
    import pytest
    from evolution_lab.cli.responses import _report_independent
    with pytest.raises(ValueError):
        _report_independent({'operation': 'campaign', 'lab_notices_state': 'present',
                             'lab_notices': [{'code': 'baseline_provenance_unknown', 'round': 2}]})


def test_unknown_gate_kind_is_unavailable():
    from dataclasses import replace
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    snapshot = core_raw.campaign_read(RUN).view.snapshot
    identity = dict(snapshot.identity)
    identity['policy'] = {**identity['policy'], 'gate': {**identity['policy']['gate'],
        'provenance': {'kind': 'bogus'}}}
    fields = baseline_notice_fields(RUN, replace(snapshot, identity=identity))
    assert fields['lab_notices_state'] == 'unavailable'


def test_partial_and_excluded_baselines_do_not_warn(monkeypatch):
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    snapshot = core_raw.campaign_read(RUN).view.snapshot
    actual = core_raw.baseline_provenance_state(RUN, snapshot)
    for changes in ({'known': 1}, {'admitted': 0}, {'samples': 0}):
        monkeypatch.setattr(core_raw, 'baseline_provenance_state', lambda *_: {**actual, **changes})
        assert baseline_notice_fields(RUN, snapshot) == {'lab_notices': [], 'lab_notices_state': 'present'}


def test_invalid_gate_missing_kind_is_unavailable():
    from dataclasses import replace
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    snapshot = core_raw.campaign_read(RUN).view.snapshot
    identity = dict(snapshot.identity)
    identity['policy'] = {**identity['policy'], 'gate': {**identity['policy']['gate'], 'provenance': {}}}
    assert baseline_notice_fields(RUN, replace(snapshot, identity=identity))['lab_notices_state'] == 'unavailable'


def test_captured_view_and_report_use_same_baseline_evidence():
    from dev63_default_outputs import read_outputs
    for name in ('b14/agent-failed', 'b15/insufficient-evidence'):
        outputs = read_outputs(Path(__file__).parent / 'fixtures/panel' / name / 'run', include_panel=False)
        view, report = outputs['campaign.json'], outputs['owner-report.json']['report']
        assert view['lab_notices_state'] == report['lab_notices_state']
        assert view['lab_notices'] == report['lab_notices']


def test_owner_cached_status_retries_corrupt_baseline(campaign, monkeypatch):
    import asyncio
    from test_owner_projection_cache import owner_for
    root, status, run = campaign
    journal = run / 'measurements/m-r00-champion/events.jsonl'
    original = journal.read_bytes()

    async def scenario():
        async with owner_for((root, status, run), monkeypatch) as (owner, _, ref):
            await owner.status(ref)
            assert ref.id in owner._projection_cache
            initial = await owner.campaign(ref)
            assert initial['lab_notices_state'] == 'present'
            assert not any(n['code'] == 'baseline_provenance_unknown' for n in initial['lab_notices'])
            journal.write_bytes(b'bad')
            view = await owner.campaign(ref)
            report = await owner.report(ref)
            assert view['lab_notices_state'] == 'unavailable'
            assert report.state == 'unavailable'
            journal.write_bytes(original)
            real_reader = core_raw.baseline_provenance_state
            def unavailable(*args, **kwargs):
                raise core_raw.CoreRawUnavailable('unreadable')
            monkeypatch.setattr(core_raw, 'baseline_provenance_state', unavailable)
            view = await owner.campaign(ref)
            report = await owner.report(ref)
            assert view['lab_notices_state'] == 'unavailable'
            assert report.state == 'available' and report.report['lab_notices_state'] == 'unavailable'
            monkeypatch.setattr(core_raw, 'baseline_provenance_state', real_reader)
            view = await owner.campaign(ref)
            report = await owner.report(ref)
            assert view['lab_notices_state'] == report.report['lab_notices_state'] == 'present'
            assert not any(n['code'] == 'baseline_provenance_unknown' for n in view['lab_notices'])
            assert not any(n['code'] == 'baseline_provenance_unknown' for n in report.report['lab_notices'])
    asyncio.run(scenario())


from test_owner_projection_completeness import campaign  # noqa: E402,F401


def test_owner_view_and_report_share_successful_baseline_cache(campaign, monkeypatch):
    import asyncio
    from test_owner_projection_cache import owner_for
    original = core_raw.baseline_provenance_state
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'baseline_provenance_state', read)
    async def scenario():
        async with owner_for(campaign, monkeypatch) as (owner, _, ref):
            view = await owner.campaign(ref)
            assert view['lab_notices_state'] == 'present'
            report = await owner.report(ref)
            assert report.state == 'available'
            assert report.report['lab_notices_state'] == 'present'
            assert len(calls) == 1
            owner._invalidate_deleted_payloads(ref.id)
            await owner.campaign(ref)
            assert len(calls) == 2
    asyncio.run(scenario())


def test_baseline_unavailable_markdown_is_russian():
    import json
    from evolution_lab.report import Report, read_campaign_report
    report = read_campaign_report(RUN, families=core_raw.FAMILIES)
    data = report.data
    data['lab_notices_state'] = 'unavailable'
    data['lab_notices'] = []
    text = Report(json.dumps(data)).markdown()
    assert 'Проверка provenance baseline недоступна' in text
    assert 'Baseline provenance notices are unavailable' not in text
