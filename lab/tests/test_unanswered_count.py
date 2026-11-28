"""The unanswered count describes admitted scorer verdicts, never failed execution."""
import asyncio
import json

import pytest

from evolution_lab.owner import core_raw
from test_owner_projection_equivalence import real_operation


def count_reader(tmp_path, monkeypatch):
    """Real Owner count path over two neutral, independently writable measurements."""
    from collections import Counter
    from types import SimpleNamespace
    from evolution_lab.owner.readers import ReaderSupport
    from evolution_lab.owner.measurements import MeasurementCache
    from test_owner_core_raw import _journal, _trace
    class Reader(ReaderSupport):
        state_directory = tmp_path
        _measurements = MeasurementCache()
        async def _result_thread(self, function, *args):
            return function(*args)
    root = tmp_path / 'artifacts/op/op'
    root.mkdir(parents=True)
    entries, outputs = [], {}
    for mid in ('first', 'second'):
        journal = root / 'measurements' / mid / 'events.jsonl'
        journal.parent.mkdir(parents=True)
        events, samples = [], []
        for number in range(2):
            request = f'{mid}-{number}'
            case = f'case-{number}'
            payload = {'case_id': case, 'sample_id': '1', 'score_request_id': request}
            events += [('scoring_started', payload), ('scoring_finished', {**payload, 'metrics': {}})]
            samples.append({'case_id': case, 'sample_id': '1', 'scoring_state': 'finished'})
            _trace(root, request, case)
            outputs[request] = root / 'scorer-traces/scorer_traces' / request / 'stdout'
            outputs[request].write_text('{"verdicts":[{"criterion":"answered","passed":false}]}')
        _journal(journal, events)
        entries.append({'measurement_id': mid, 'locator': str(journal.relative_to(root)), 'status': {'samples': samples}})
    reader, status, core = Reader(), SimpleNamespace(id='op', kind='campaign'), {'measurements': entries}
    calls = Counter()
    original = core_raw.scorer_trace
    def traced(*args, **kwargs):
        calls[args[1]] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(core_raw, 'scorer_trace', traced)
    async def read():
        return await reader._unanswered_counts(status, core)
    return SimpleNamespace(read=read, reader=reader, root=root, core=core, outputs=outputs, calls=calls)


async def test_unanswered_dependencies_are_per_measurement_and_trace(tmp_path, monkeypatch):
    stand = count_reader(tmp_path, monkeypatch)
    first = await stand.read()
    assert first['first']['unanswered'] == first['second']['unanswered'] == 2
    assert all(value == 1 for value in stand.calls.values())
    (stand.root / 'campaign.jsonl').write_text('unrelated journal append\n')
    (stand.root / 'editor-logs').mkdir()
    (stand.root / 'editor-logs/role.stderr').write_text('unrelated role output\n')
    assert await stand.read() == first
    assert all(value == 1 for value in stand.calls.values())
    stand.outputs['second-0'].write_text('{"verdicts":[{"criterion":"answered","passed":true}]}')
    changed = await stand.read()
    assert changed['second']['unanswered'] == 1 and changed['first']['unanswered'] == 2
    assert stand.calls == {'first-0': 1, 'first-1': 1, 'second-0': 2, 'second-1': 1}
    # An unrelated measurement journal must not invalidate either verified trace.
    journal = stand.root / 'measurements/second/events.jsonl'
    journal.write_bytes(journal.read_bytes() + b'\n')
    assert (await stand.read())['second']['unanswered_state'] == 'unavailable'
    assert stand.calls['first-0'] == stand.calls['first-1'] == 1


async def test_unanswered_parses_each_journal_once_per_change(tmp_path, monkeypatch):
    stand = count_reader(tmp_path, monkeypatch)
    calls = []
    original = core_raw._journal_events
    reads = []
    original_read = core_raw.read_inside
    def read_once(root, *parts, **kwargs):
        if parts == ('events.jsonl',):
            reads.append(root)
        return original_read(root, *parts, **kwargs)
    def counted(path):
        calls.append(path)
        yield from original(path)
    monkeypatch.setattr(core_raw, '_journal_events', counted)
    monkeypatch.setattr(core_raw, 'read_inside', read_once)
    assert (await stand.read())['first']['unanswered'] == 2
    assert len(calls) == 2
    assert len(reads) == 2  # No second read merely to calculate a discarded digest.
    await stand.read()
    assert len(calls) == 2
    assert len(reads) == 2


@pytest.mark.parametrize('damage', ['unbound_request', 'missing_critical', 'invalid_sample_key'])
async def test_unrelated_journal_damage_does_not_spoil_admitted_count_or_notice(tmp_path, monkeypatch, damage):
    from evolution_lab.owner.measurements import evaluation_focus_notices
    stand = count_reader(tmp_path, monkeypatch)
    journal = stand.root / 'measurements/first/events.jsonl'
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    for row in rows:
        if row['event'] == 'scoring_finished':
            row['payload']['critical_pass'] = None
    bad = {**rows[-1], 'payload': {**rows[-1]['payload'], 'case_id': 'not-admitted', 'sample_id': 'bad'}}
    if damage == 'missing_critical':
        rows.append({**rows[0], 'payload': {**bad['payload']}})
        del bad['payload']['critical_pass']
    if damage == 'invalid_sample_key':
        bad['payload']['case_id'] = []
    rows.append(bad)
    journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert (await stand.read())['first']['unanswered'] == 2
    samples = [{**sample, 'critical_pass': None} for sample in stand.core['measurements'][0]['status']['samples']]
    core = {'run_id': 'first', 'samples': samples}
    expected = {'lab_notices': [], 'lab_notices_state': 'present'}
    assert evaluation_focus_notices(core, stand.root, journal) == expected
    assert evaluation_focus_notices(core, stand.root, journal,
        cache=stand.reader._measurements, operation_id='op') == expected


async def test_focus_notice_is_independent_of_duplicate_answered_count_rule(tmp_path, monkeypatch):
    from evolution_lab.owner.measurements import evaluation_focus_notices
    stand = count_reader(tmp_path, monkeypatch)
    journal = stand.root / 'measurements/first/events.jsonl'
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    for row in rows:
        if row['event'] == 'scoring_finished':
            row['payload']['critical_pass'] = None
    journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    stand.outputs['first-0'].write_text('{"verdicts":[{"criterion":"answered","passed":true},{"criterion":"answered","passed":false}]}')
    samples = [{**s, 'critical_pass': None} for s in stand.core['measurements'][0]['status']['samples']]
    core = {'run_id': 'first', 'samples': samples}
    assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    for cache in (None, stand.reader._measurements):
        assert evaluation_focus_notices(core, stand.root, journal, cache=cache, operation_id='op') == {
            'lab_notices': [], 'lab_notices_state': 'present'}
@pytest.mark.parametrize('failure', ['missing', 'too_large', 'io'])
async def test_unavailable_count_retries_failed_evidence_but_reuses_verified_components(tmp_path, monkeypatch, failure):
    stand = count_reader(tmp_path, monkeypatch)
    path = stand.outputs['first-1']
    original_bytes = path.read_bytes()
    original_trace = core_raw.scorer_trace
    if failure == 'missing':
        path.unlink()
    elif failure == 'too_large':
        with path.open('wb') as stream:
            stream.truncate(core_raw.FILE_MAX + 1)
    else:
        def failing(*args, **kwargs):
            if args[1] == 'first-1':
                raise core_raw.CoreRawUnavailable('unreadable')
            return original_trace(*args, **kwargs)
        monkeypatch.setattr(core_raw, 'scorer_trace', failing)
    assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    assert stand.calls['first-0'] == 1
    path.write_bytes(original_bytes)
    monkeypatch.setattr(core_raw, 'scorer_trace', original_trace)
    assert (await stand.read())['first']['unanswered'] == 2


async def test_mutation_during_trace_read_does_not_publish_a_stale_cache_hit(tmp_path, monkeypatch):
    stand = count_reader(tmp_path, monkeypatch)
    original = core_raw.scorer_trace
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[1] == 'first-0':
            stand.outputs['first-0'].write_text('{"verdicts":[{"criterion":"answered","passed":true}]}')
        return value
    monkeypatch.setattr(core_raw, 'scorer_trace', changed)
    await stand.read()
    monkeypatch.setattr(core_raw, 'scorer_trace', original)
    assert (await stand.read())['first']['unanswered'] == 1
    assert stand.calls['first-0'] == 2


async def test_cached_trace_rechecks_nofollow_and_recovery(tmp_path, monkeypatch):
    stand = count_reader(tmp_path, monkeypatch)
    assert (await stand.read())['first']['unanswered'] == 2
    path = stand.outputs['first-0']
    saved = path.with_name('saved')
    path.rename(saved)
    path.symlink_to(saved)
    assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    path.unlink()
    saved.rename(path)
    assert (await stand.read())['first']['unanswered'] == 2


@pytest.mark.parametrize('failure', ['missing', 'too_large'])
async def test_static_unavailable_never_reads_trace_payload(tmp_path, monkeypatch, failure):
    from evolution.tree import ScorerTraceReader
    stand = count_reader(tmp_path, monkeypatch)
    path = stand.outputs['first-1']
    if failure == 'missing':
        path.unlink()
    else:
        with path.open('wb') as stream:
            stream.truncate(core_raw.FILE_MAX + 1)
    payload_reads = []
    original = ScorerTraceReader.read
    def counted(self, request, **kwargs):
        if request == 'first-1' and kwargs.get('sections'):
            payload_reads.append(request)
        return original(self, request, **kwargs)
    monkeypatch.setattr(ScorerTraceReader, 'read', counted)
    for _ in range(2):
        assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    assert payload_reads == []


async def test_pack_changes_segments_and_deletion_invalidate_verified_components(tmp_path, monkeypatch):
    import shutil
    from scorer_trace_pack import scorer_pack, PACK_FORMAT, encode_pack_record
    stand = count_reader(tmp_path, monkeypatch)
    # One sample in this measurement, kept solely in the packed layout.
    stand.core['measurements'][0]['status']['samples'] = stand.core['measurements'][0]['status']['samples'][:1]
    source = stand.outputs['first-0'].parent
    request = (source / 'request.json').read_bytes()
    original_stdout = stand.outputs['first-0'].read_bytes()
    shutil.rmtree(source)
    pack = scorer_pack(stand.root, 'first', 'first-0', request=request, stdout=original_stdout)
    assert (await stand.read())['first']['unanswered'] == 1
    assert (await stand.read())['first']['unanswered'] == 1
    assert stand.calls['first-0'] == 1
    scorer_pack(stand.root, 'second', 'foreign', request=request, stdout=original_stdout)
    await stand.read()
    assert stand.calls['first-0'] == 1
    scorer_pack(stand.root, 'first', 'first-0', request=request,
                stdout=b'{"verdicts":[{"criterion":"answered","passed":true}]}')
    assert (await stand.read())['first']['unanswered'] == 0
    segment = pack.with_name('first.s1.pack')
    segment.write_bytes(encode_pack_record({'format': PACK_FORMAT, 'kind': 'pack', 'pack_id': 'first', 'segment': 1})
        + encode_pack_record({'format': PACK_FORMAT, 'kind': 'request', 'score_request_id': 'first-0'}, [('request', request)]))
    assert (await stand.read())['first']['unanswered_state'] == 'unavailable'
    segment.unlink()
    assert (await stand.read())['first']['unanswered'] == 0
    before = stand.calls['first-0']
    stand.reader._measurements.drop('op')
    assert (await stand.read())['first']['unanswered'] == 0
    assert stand.calls['first-0'] == before + 1


async def test_uncertain_metadata_does_not_populate_a_component_cache(tmp_path, monkeypatch):
    from evolution_lab.owner import projection_signature
    stand = count_reader(tmp_path, monkeypatch)
    original = projection_signature._path_signature
    def failing(path, **kwargs):
        if path.name == 'stdout' and path.parent.name == 'first-0':
            raise OSError('transient metadata failure')
        return original(path, **kwargs)
    monkeypatch.setattr(projection_signature, '_path_signature', failing)
    for _ in range(2):
        assert (await stand.read())['first']['unanswered'] == 2
    assert stand.calls['first-0'] == 2
    monkeypatch.setattr(projection_signature, '_path_signature', original)
    await stand.read()
    await stand.read()
    assert stand.calls['first-0'] == 3


def attach_traces(stand, admitted, outputs):
    from test_owner_core_raw import _trace
    records = [json.loads(line) for line in (stand.run / "measurement/events.jsonl").read_text().splitlines()]
    for sample, output in zip(admitted, outputs):
        record = next(row for row in records if row["event"] == "scoring_finished"
                      and row["payload"]["case_id"] == sample["case_id"]
                      and row["payload"]["sample_id"] == sample["sample_id"])
        request_id = record["payload"]["score_request_id"]
        if output == "unreadable":
            continue
        root = stand.run / "measurement"
        _trace(root, request_id, sample["case_id"])
        path = root / "scorer-traces/scorer_traces" / request_id / "stdout"
        if output == "too_large":
            path.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
        else:
            entries = [] if output is None else [{"criterion": "answered", "passed": output}]
            path.write_text(json.dumps({"verdicts": entries}))


@pytest.mark.parametrize(("outputs", "expected"), [
    ([False, True], {"unanswered": 1, "unanswered_state": "present", "answered_reported": 2}),
    ([False, None], {"unanswered": 1, "unanswered_state": "partial", "answered_reported": 1}),
    ([None, None], {"unanswered": None, "unanswered_state": "not_reported", "answered_reported": 0}),
    ([False, "too_large"], {"unanswered": None, "unanswered_state": "unavailable", "answered_reported": None}),
    ([False, "unreadable"], {"unanswered": None, "unanswered_state": "unavailable", "answered_reported": None}),
])
def test_evaluation_count_and_status_are_built_from_admitted_scorer_output(tmp_path, outputs, expected):
    async def run():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            status = await stand.owner.status(stand.ref)
            samples = list(status.result.core["samples"])
            admitted = [s for s in samples if s["scoring_state"] == "finished"]
            assert len(admitted) == 2
            attach_traces(stand, admitted, outputs)
            result = await stand.owner.measurements(stand.ref)
            row = result["measurements"][0]
            assert "evidence_digest" not in json.dumps(result)
            assert result["schema"] == "lab-measurement-list/4"
            assert {key: row.get(key) for key in expected} == expected
            from evolution_lab.cli.responses import validate
            assert validate(result, "lab-measurement-list/4") == result
            result = (await stand.owner.status(stand.ref)).to_payload()["result"]
            assert result["schema"] == "lab-operation-result/3"
            assert {key: result.get(key) for key in expected} == expected
            assert "evidence_digest" not in json.dumps(result)
            detail = await stand.owner.sample(stand.ref, row["measurement_id"],
                                              admitted[0]["case_id"], admitted[0]["sample_id"], disclose=True)
            assert "evidence_digest" not in json.dumps(detail)
            report = (await stand.owner.report(stand.ref)).to_payload()
            assert "evidence_digest" not in json.dumps(report)
    asyncio.run(run())


def test_measurement_row_exposes_missing_scorer_evidence_as_unavailable():
    from evolution_lab.owner.measurements import measurement_rows
    core = {"run_id": "neutral", "samples": [{"case_id": "a", "sample_id": "1",
            "scoring_state": "finished", "metrics": {}}]}
    row = measurement_rows(core, "evaluation", {}, None)[0]
    assert row.get("unanswered_state") == "unavailable"
    assert row.get("unanswered") is None


def test_unmeasured_samples_are_not_counted(tmp_path):
    from evolution_lab.owner.measurements import unanswered_count
    from test_owner_core_raw import _journal, _trace
    sample = {"case_id": "one", "sample_id": "01", "scoring_state": "finished"}
    failed = {"case_id": "infra", "sample_id": "01", "scoring_state": "failed"}
    request = "a" * 32
    journal = tmp_path / "events.jsonl"
    base = {"case_id": "one", "sample_id": "01", "score_request_id": request}
    _journal(journal, [("scoring_started", base), ("scoring_finished", {**base, "metrics": {}})])
    _trace(tmp_path, request, "one")
    output = tmp_path / "scorer-traces/scorer_traces" / request / "stdout"
    output.write_text('{"verdicts":[{"criterion":"answered","passed":false}]}')
    count = unanswered_count([sample, failed], tmp_path, journal, "measurement")
    assert count == {"unanswered": 1, "unanswered_state": "present", "answered_reported": 1}


def test_second_read_uses_cached_trace_aggregate_and_changes_invalidate_it(tmp_path, monkeypatch):
    async def run():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            status = await stand.owner.status(stand.ref)
            admitted = [s for s in status.result.core["samples"] if s["scoring_state"] == "finished"]
            attach_traces(stand, admitted, [False, True])
            calls = []
            original = core_raw.scorer_trace
            def counted(*args, **kwargs):
                calls.append(args[1])
                return original(*args, **kwargs)
            monkeypatch.setattr(core_raw, "scorer_trace", counted)
            first = await stand.owner.measurements(stand.ref)
            initial = len(calls)
            assert first["measurements"][0]["unanswered"] == 1 and initial >= 2
            second = await stand.owner.measurements(stand.ref)
            assert second == first
            assert len(calls) == initial
            trace_dir = stand.run / "measurement/scorer-traces/scorer_traces"
            first_stdout = next(trace_dir.glob("*/stdout"))
            first_stdout.write_text('{"verdicts":[{"criterion":"answered","passed":false}]}')
            await stand.owner.measurements(stand.ref)
            assert len(calls) > initial
    asyncio.run(run())


def test_unavailable_count_is_retried_without_caching_it_in_the_base_projection(tmp_path, monkeypatch):
    from evolution_lab.owner import readers
    async def run():
        async with real_operation(tmp_path, kind='evaluation', finished=True) as stand:
            current = await stand.owner.status(stand.ref)
            admitted = [s for s in current.result.core['samples'] if s['scoring_state'] == 'finished']
            attach_traces(stand, admitted, [False, True])
            projections = []
            original_project = readers.project_operation
            def projected(*args, **kwargs):
                projections.append(True)
                return original_project(*args, **kwargs)
            monkeypatch.setattr(readers, 'project_operation', projected)
            original_trace = core_raw.scorer_trace
            def failing(*args, **kwargs):
                raise core_raw.CoreRawUnavailable('unreadable')
            monkeypatch.setattr(core_raw, 'scorer_trace', failing)
            first = await stand.owner.status(stand.ref)
            assert first.result.unanswered_state == 'unavailable'
            monkeypatch.setattr(core_raw, 'scorer_trace', original_trace)
            second = await stand.owner.status(stand.ref)
            assert second.result.unanswered == 1 and second.result.answered_reported == 2
            assert len(projections) == 1
    asyncio.run(run())


def test_snapshot_fallback_does_not_suppress_invalid_counter_types(tmp_path, monkeypatch):
    from evolution_lab.owner.models import OwnerError
    async def run():
        async with real_operation(tmp_path, kind='evaluation', finished=True) as stand:
            async def invalid(status, core):
                return {core['run_id']: {'unanswered': False, 'unanswered_state': 'present', 'answered_reported': 3}}
            monkeypatch.setattr(stand.owner, '_unanswered_counts', invalid)
            with pytest.raises(OwnerError) as caught:
                await stand.owner.status(stand.ref)
            assert caught.value.code == 'invalid_argument'
    asyncio.run(run())


def test_result_snapshot_preserves_project_link_contract(tmp_path):
    from evolution_lab.projects.session import last_seen_of
    async def run():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            status = await stand.owner.status(stand.ref)
            full = status.to_payload()["result"]
            assert full["schema"] == "lab-operation-result/3"
            snapshot = last_seen_of(status)["result"]
            assert snapshot == {**{key: value for key, value in full.items() if key not in {
                "schema", "unanswered", "unanswered_state", "answered_reported"}},
                "schema": "lab-operation-result/2"}
    asyncio.run(run())


def test_markdown_prints_only_present_or_partial_unanswered(tmp_path):
    from evolution_lab.report import build_report
    from evolution.tree import inspect_journal, project_result
    from test_owner_projection_completeness import CAPTURE
    projection = project_result(inspect_journal(CAPTURE / "measurements/m-r00-champion"))
    mid = projection.to_payload()["run_id"]
    for state, count, shown in [("present", 1, True), ("partial", 1, True),
                                ("not_reported", None, False), ("unavailable", None, False)]:
        report = build_report(projection, unanswered={mid: {"unanswered": count,
            "unanswered_state": state, "answered_reported": 1 if count is not None else None}})
        assert ("без ответа: 1 из 2" in report.markdown()) is shown


def test_campaign_baseline_and_candidate_have_independent_counts(tmp_path, monkeypatch):
    from pathlib import Path
    import test_owner_projection_equivalence as fixture_module
    from test_owner_core_raw import _trace
    monkeypatch.setattr(fixture_module, "CAPTURE", Path(__file__).parent / "fixtures/panel/b15/causal-open/run")
    async def run():
        async with real_operation(tmp_path, kind="campaign", finished=True) as stand:
            status = await stand.owner.status(stand.ref)
            entries = list(status.result.core["measurements"])
            baseline = next(entry for entry in entries if entry["measurement_id"] == "m-r00-champion")
            candidate = next(entry for entry in entries if entry["measurement_id"] == "m-r01-a01")
            for pack in (stand.run / "scorer-traces").glob("*.pack"):
                pack.unlink()
            for entry, verdict in [(baseline, False), (candidate, True)]:
                journal = stand.run / entry["locator"]
                records = [json.loads(line) for line in journal.read_text().splitlines()]
                for sample in entry["status"]["samples"]:
                    if sample["scoring_state"] != "finished":
                        continue
                    event = next(record for record in records if record["event"] == "scoring_finished"
                                 and record["payload"]["case_id"] == sample["case_id"]
                                 and record["payload"]["sample_id"] == sample["sample_id"])
                    request = event["payload"]["score_request_id"]
                    _trace(stand.run, request, sample["case_id"])
                    output = stand.run / "scorer-traces/scorer_traces" / request / "stdout"
                    output.write_text(json.dumps({"verdicts": [{"criterion": "answered", "passed": verdict}]}))
            rows = {row["measurement_id"]: row for row in (await stand.owner.measurements(stand.ref))["measurements"]}
            assert rows["m-r00-champion"]["unanswered"] == rows["m-r00-champion"]["coverage"]["scored"]
            assert rows["m-r01-a01"]["unanswered"] == 0, rows["m-r01-a01"]
            assert rows["m-r00-champion"]["unanswered_state"] == "present"
            assert rows["m-r01-a01"]["unanswered_state"] == "present"
    asyncio.run(run())


def test_measurement_basis_stays_at_the_baseline_fixture_digest(tmp_path, monkeypatch):
    import sys
    from test_measurement_basis import _basis, _campaign
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    *_, bundle = _campaign(tmp_path, monkeypatch)
    assert _basis(bundle) == "2919854c739fd076b56a50e2069107b433be50ff0941d3e7105cd52cfa5759c7"


def attach_public_api_traces(monkeypatch, scorer, root, verdicts):
    """Retain real scoring/journal side effects and supply matching scorer trace files."""
    import dataclasses
    import evolution_lab.evaluation as evaluation
    from test_owner_core_raw import _trace
    original_score = scorer.score
    original_run = evaluation.run_fixed
    emitted = []

    async def score(case, view, candidate):
        result = await original_score(case, view, candidate)
        passed = verdicts[len(emitted)]
        emitted.append(passed)
        values = () if passed is None else ({"criterion": "answered", "passed": passed},)
        return dataclasses.replace(result, verdicts=values)

    async def run(measurement):
        result = await original_run(measurement)
        records = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        finished = [row for row in records if row["event"] == "scoring_finished"]
        for row, passed in zip(finished, emitted, strict=True):
            request = row["payload"]["score_request_id"]
            _trace(root, request, row["payload"]["case_id"])
            values = [] if passed is None else [{"criterion": "answered", "passed": passed}]
            (root / "scorer-traces/scorer_traces" / request / "stdout").write_text(
                json.dumps({"metrics": {"quality": 0.75}, "verdicts": values}))
        return result

    monkeypatch.setattr(scorer, "score", score)
    monkeypatch.setattr(evaluation, "run_fixed", run)


async def test_evaluate_fixed_returns_unanswered_without_changing_saved_receipt(tmp_path, monkeypatch):
    from evolution_lab.evaluation import evaluate_fixed
    from test_evaluation import bindings
    args = bindings(tmp_path, repeats=2)
    attach_public_api_traces(monkeypatch, args["scorer"], args["run_directory"], [False, True])
    result = await evaluate_fixed(**args)
    assert {key: result.get(key) for key in ("unanswered", "unanswered_state", "answered_reported")} == {
        "unanswered": 1, "unanswered_state": "present", "answered_reported": 2}
    assert json.loads((args["run_directory"] / "evaluation-result.json").read_text()) == {
        "version": "lab-evaluation-result/2", "run_id": "evaluation", "coverage": {"expected": 2, "scored": 2},
        "unmeasured": [], "stopped_reason": None, "min_coverage": 1.0}


@pytest.mark.parametrize(("mode", "want"), [
    ("not_reported", {"unanswered": None, "unanswered_state": "not_reported", "answered_reported": 0}),
    ("unreadable", {"unanswered": None, "unanswered_state": "unavailable", "answered_reported": None}),
    ("infrastructure", {"unanswered": None, "unanswered_state": "not_reported", "answered_reported": 0}),
])
async def test_evaluate_fixed_returns_missing_unavailable_or_unscored_state(tmp_path, monkeypatch, mode, want):
    from evolution_lab.evaluation import evaluate_fixed
    from test_evaluation import bindings
    args = bindings(tmp_path, fail=mode == "infrastructure", repeats=2)
    if mode == "not_reported":
        attach_public_api_traces(monkeypatch, args["scorer"], args["run_directory"], [None, None])
    result = await evaluate_fixed(**args)
    assert {key: result[key] for key in want} == want
    assert result["coverage"]["scored"] == (0 if mode == "infrastructure" else 2)


@pytest.mark.parametrize(("state", "count", "reported"), [
    ("present", 0, 1),
    ("partial", 1, 2),
    ("partial", 1, 3),
    ("not_reported", None, False),
])
def test_operation_result_rejects_counts_inconsistent_with_scored_coverage(state, count, reported):
    from evolution_lab.owner.models import Coverage, OperationResult, OwnerError, ProcessFacts
    with pytest.raises(OwnerError):
        OperationResult("a" * 64, ProcessFacts(), kind="evaluation", coverage=Coverage(2, 2),
                        unanswered_state=state, unanswered=count, answered_reported=reported)


@pytest.mark.parametrize(("state", "count", "reported", "valid"), [
    ("present", 1, 2, True), ("present", False, 2, False), ("present", 1.0, 2, False),
    ("present", "1", 2, False), ("present", None, 2, False), ("present", 1, True, False),
    ("present", 1, 2.0, False), ("present", 1, "2", False), ("present", 1, None, False),
    ("partial", 1, 1, True), ("partial", True, 1, False), ("partial", 1.0, 1, False),
    ("partial", "1", 1, False), ("partial", None, 1, False), ("partial", 1, True, False),
    ("partial", 1, 1.0, False), ("partial", 1, "1", False), ("partial", 1, None, False),
    ("not_reported", None, 0, True), ("not_reported", None, False, False),
    ("not_reported", None, 0.0, False), ("not_reported", None, "0", False),
    ("not_reported", None, None, False), ("not_reported", 0, 0, False),
    ("not_reported", False, 0, False),
    ("unavailable", None, None, True), ("unavailable", None, False, False),
    ("unavailable", None, 0.0, False), ("unavailable", None, 0, False),
    ("unavailable", 0, None, False), ("unavailable", False, None, False),
])
@pytest.mark.parametrize("detail", [False, True])
def test_measurement_decoders_enforce_count_types_in_every_state(state, count, reported, valid, detail):
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import CORE
    rows = measurement_rows(CORE, "campaign", {}, "score", detail="m-r00-champion" if detail else None)
    row = rows[0]
    row["coverage"]["scored"] = 2
    row.update(unanswered_state=state, unanswered=count, answered_reported=reported)
    schema = "lab-measurement/4" if detail else "lab-measurement-list/4"
    body = {"schema": schema, "operation_id": "op1", **({"measurement": row} if detail else {"measurements": rows})}
    if valid:
        assert validate(body, schema) == body
    else:
        with pytest.raises(ValueError):
            validate(body, schema)
