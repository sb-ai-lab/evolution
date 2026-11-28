"""Offline installed /15 Core calls through two policies on the same saved Bundle."""
import asyncio
import json
import secrets
import sqlite3
from pathlib import Path

import pytest

from test_owner_campaign import _GroupingUpstream
from test_owner_cleanup import finished
from test_owner_target import make_bundle
from scaffold_15 import scaffold_15_with_legacy_failures
from test_owner_target_gateway import confirmed, policy_document
from ports import free_port

pytestmark = pytest.mark.slow


def routed_launcher(port):
    launcher = (Path(__file__).parent / 'owner_fixtures/launcher.py').read_text()
    # A neutral target uses its generated token on each real turn. Preserve the
    # fixture's existing fail_after hook and the producing Core implementation.
    insertion = f'''
import json
import urllib.request
original_dispatch = server.dispatch
def routed_dispatch(method, path, raw_body=b''):
    if method == 'POST' and path.endswith('/turns'):
        request = urllib.request.Request('http://127.0.0.1:{port}/target/v1/chat/completions',
            data=json.dumps({{'model': 'target-client', 'messages': [{{'role': 'user', 'content': 'neutral'}}], 'max_tokens': 10}}).encode(),
            headers={{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['TARGET_TOKEN']}})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    return original_dispatch(method, path, raw_body)
server.dispatch = routed_dispatch
'''
    return launcher.replace('http = None', insertion + '\nhttp = None')


async def target_under(owner, bundle, document, keys=None):
    from evolution_lab.owner import TargetRef
    digest = await confirmed(owner, document)
    result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest), secrets=keys or {}, execution_policy=digest))
    assert result.state == 'succeeded', result.to_payload()
    return TargetRef(result.ref.id, owner.owner_generation), digest


def ledger_uses(document):
    with sqlite3.connect(document['gateway']['ledger_path']) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT uses_json FROM routing_observations')]


def resume_policy_documents(tmp_path, port, upstream_a_port, upstream_b_port):
    a = policy_document(tmp_path, port)
    a['routes']['target']['endpoint'] = f'http://127.0.0.1:{upstream_a_port}/routerai/api/v1'
    a['routes']['target']['provider'] = {'only': ['chosen-a']}
    b = json.loads(json.dumps(a))
    b['routes']['target'].update(endpoint=f'http://127.0.0.1:{upstream_b_port}/routerai/api/v1', api_key_secret=None)
    b['routes']['target']['provider'] = {'order': ['answered-b', 'other-b'], 'allow_fallbacks': True}
    return a, b


def test_real_core_resume_switches_keyed_a_to_keyless_b_without_identity_change(tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.owner import Owner, OwnerError, ResourceRef, TargetRef
    from evolution_lab.preparation import PreparationStore
    port = free_port()
    flag = tmp_path / 'target-broken'
    flag.touch()
    state, bundle, _ = make_bundle(tmp_path, mode=f'fail_after:1:{flag}', launcher_source=routed_launcher(port),
                                   scaffold=scaffold_15_with_legacy_failures)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    upstream_a = _GroupingUpstream(lambda _: 'neutral', provider='answered-a')
    upstream_b = _GroupingUpstream(lambda _: 'neutral', provider='answered-b')
    key = secrets.token_urlsafe(32)
    a, b = resume_policy_documents(tmp_path, port, upstream_a.port, upstream_b.port)
    async def scenario():
        async with Owner.open(state) as owner:
            first_target, first_digest = await target_under(owner, bundle, a, {'UPSTREAM_KEY': key})
            accepted = await owner.start(prepared.digest, target_ref=first_target, secrets={})
            ref = ResourceRef('operation', accepted.id)
            first = await finished(owner, ref, seconds=90)
            assert first.failure is None, first.to_payload()
            assert first.result.core['suspended'] is True, first.to_payload()
            assert upstream_a.requests
            run = state / 'artifacts' / ref.id / ref.id
            before = inspect_campaign(run, verify=True).to_payload()
            assert (await owner.wait(await owner.stop_target(first_target))).state == 'succeeded'
            # Both refusal paths precede any target turns or resumed history.
            result = await owner.wait(await owner.start_target(await owner.bind(bundle.digest), secrets={'TARGET_TOKEN': 'neutral'}))
            plain = TargetRef(result.ref.id, owner.owner_generation)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=plain, secrets={})
            assert caught.value.code == 'policy_continuation_mismatch'
            await owner.wait(await owner.stop_target(plain))
            changed = json.loads(json.dumps(b))
            changed['gateway']['port'] = free_port()
            changed_target, _ = await target_under(owner, bundle, changed)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=changed_target, secrets={})
            assert caught.value.code == 'policy_gateway_changed'
            assert len(await owner._db('operation_policy_history', ref.id)) == 1
            assert not upstream_b.requests
            await owner.wait(await owner.stop_target(changed_target))
            flag.unlink()
            second_target, second_digest = await target_under(owner, bundle, b)
            resumed = await owner.resume(ref, target_ref=second_target, secrets={})
            assert resumed.id == ref.id and resumed.prepared_digest == prepared.digest
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None and done.result.core['complete'], done.to_payload()
            after = inspect_campaign(run, verify=True).to_payload()
            assert before['identity_digest'] == after['identity_digest']
            assert before['rebases'] == after['rebases'] == []
            assert (await owner.target(second_target)).bundle_digest == bundle.digest
            assert upstream_b.requests
            assert all(request['authorization'] is None for request in upstream_b.requests)
            assert all(request['authorization'] == f'Bearer {key}' for request in upstream_a.requests)
            rows = await owner._db('operation_policy_history', ref.id)
            assert [(row['start'], row['policy_digest']) for row in rows] == [(1, first_digest), (2, second_digest)]
            uses = ledger_uses(a)
            assert [f'operation:{ref.id}:start:1'] in uses
            assert [f'operation:{ref.id}:start:2'] in uses
            assert owner._targets[second_target.id].gateway.uses == frozenset()
            routing = await owner.operation_routing(ref)
            assert routing['available'] is True
            assert [(row['start'], row['policy_digest']) for row in routing['runs']] == [(1, first_digest), (2, second_digest)]
            for run_row, digest in zip(routing['runs'], (first_digest, second_digest)):
                record = (await owner.execution_policy(digest)).to_payload()
                assert {key: run_row[key] for key in ('author', 'channel', 'confirmed_at')} == {
                    key: record[key] for key in ('author', 'channel', 'confirmed_at')}
                assert run_row['lab_version'] == '0.0.3.dev81'
            rows_by_provider = {row['reported_provider']: row for row in routing['roles']['target']}
            first_row, second_row = rows_by_provider['answered-a'], rows_by_provider['answered-b']
            assert first_row['calls'] == first_row['mismatched'] == len(upstream_a.requests)
            assert first_row['requested'] == ['chosen-a']
            assert second_row['calls'] == len(upstream_b.requests) and second_row['mismatched'] == 0
            assert second_row['requested'] == ['answered-b', 'other-b']
            assert routing['shared'] == routing['outside'] == 0
            report_before = await owner.report(ref)
            assert report_before.to_payload()['routing'] == routing
            markdown_before = await owner.report_markdown(ref)
            assert markdown_before.state == 'available' and markdown_before.markdown
            # Routing data failure cannot hide saved starts or the independent Core report.
            ledger_path = Path(a['gateway']['ledger_path'])
            saved_path = ledger_path.with_suffix('.saved')
            ledger_path.rename(saved_path)
            saved_sidecars = []
            try:
                missing = await owner.operation_routing(ref)
                assert missing['available'] is False and missing['reason'] == 'ledger_unreadable'
                assert missing['runs'] == routing['runs'] and not ledger_path.exists()
                report_missing = await owner.report(ref)
                assert report_missing.state == report_before.state
                assert report_missing.to_payload()['report'] == report_before.to_payload()['report']
                assert report_missing.to_payload()['routing'] == missing
                markdown_missing = await owner.report_markdown(ref)
                assert markdown_missing == markdown_before
                assert markdown_missing.markdown == markdown_before.markdown
                # WAL can hold the valid database pages even after the main file is
                # replaced. Remove the entire saved SQLite family for this damage cut.
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(ledger_path) + suffix)
                    if sidecar.exists():
                        saved = Path(str(saved_path) + suffix)
                        sidecar.rename(saved)
                        saved_sidecars.append((sidecar, saved))
                ledger_path.write_bytes(b'not a ledger')
                unreadable = await owner.operation_routing(ref)
                assert unreadable['available'] is False and unreadable['runs'] == routing['runs']
                assert (await owner.report(ref)).to_payload()['report'] == report_before.to_payload()['report']
            finally:
                ledger_path.unlink(missing_ok=True)
                saved_path.rename(ledger_path)
                for sidecar, saved in saved_sidecars:
                    saved.rename(sidecar)
    try:
        asyncio.run(scenario())
    finally:
        upstream_a.close()
        upstream_b.close()


def test_consecutive_campaigns_and_overlapping_probes_have_distinct_use_sets(tmp_path):
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    port = free_port()
    state, bundle, _ = make_bundle(tmp_path, launcher_source=routed_launcher(port))
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    upstream = _GroupingUpstream(lambda _: 'neutral')
    document = policy_document(tmp_path, port)
    document['routes']['target'].update(endpoint=f'http://127.0.0.1:{upstream.port}/v1', api_key_secret=None)
    async def scenario():
        async with Owner.open(state) as owner:
            target, digest = await target_under(owner, bundle, document)
            ids = []
            refs = []
            for _ in range(2):
                accepted = await owner.start(prepared.digest, target_ref=target, secrets={})
                done = await finished(owner, ResourceRef('operation', accepted.id), seconds=90)
                assert done.failure is None and done.result.core['complete'], done.to_payload()
                ids.append(f'operation:{accepted.id}:start:1')
                refs.append(ResourceRef('operation', accepted.id))
            async def call_gateway(uses):
                import httpx
                gateway = owner._targets[target.id].gateway
                await gateway.set_uses(frozenset(uses))
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(f'http://127.0.0.1:{port}/target/v1/chat/completions',
                        headers={'Authorization': 'Bearer ' + gateway.tokens['target']},
                        json={'model': 'target-client', 'messages': [{'role': 'user', 'content': 'neutral'}], 'max_tokens': 10})
                assert response.status_code == 200
                await gateway.set_uses(frozenset())
            # A genuine empty-use request is outside; a multi-use request is shared by
            # both named operations, without attributing it to either role aggregate.
            await call_gateway([])
            await call_gateway(ids)
            sessions = ['a'*32, 'b'*32]
            await asyncio.gather(*(owner.probe_open(target, session_id=s, seed=17) for s in sessions))
            outcome = await owner.probe_turn(sessions[0], 'neutral', timeout=10)
            assert outcome.failure is None
            expected = sorted(f'probe:{owner.owner_generation}:{s}' for s in sessions)
            assert expected in ledger_uses(document)
            with sqlite3.connect(state / 'owner.sqlite3') as db:
                rows = db.execute('SELECT session_id,policy_digest FROM probe_session_policies ORDER BY session_id').fetchall()
            assert rows == [(session, digest) for session in sessions]
            for session in sessions:
                assert await owner.probe_close(session) == 'released'
            assert owner._targets[target.id].gateway.uses == frozenset()
            uses = ledger_uses(document)
            assert all([use] in uses for use in ids)
            assert sum(set(ids) <= set(use) for use in uses) == 1
            for ref, use in zip(refs, ids):
                routing = await owner.operation_routing(ref)
                assert routing['available'] is True
                assert routing['shared'] == routing['outside'] == 1
                assert sum(row['calls'] for row in routing['roles']['target']) == uses.count([use])
                assert len(routing['runs']) == 1 and routing['runs'][0]['policy_digest'] == digest
    try:
        asyncio.run(scenario())
    finally:
        upstream.close()


def test_real_grouping_reasoning_and_evaluation_scorer_receive_generated_tokens(tmp_path):
    from scaffold_15 import scaffold_15
    from test_owner_binding import MODEL_GROUPING
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    port = free_port()
    def scaffold(source):
        result = scaffold_15(source)
        path = source / 'manifest.json'
        doc = json.loads(path.read_text())
        doc['scorer']['allowed_env'] = ['SCORER_TOKEN']
        score = source / doc['scorer']['cwd'] / doc['scorer']['argv'][1]
        # The neutral scorer uses its declared token, then performs its usual scoring.
        original = score.read_text()
        call = f"""
import os, json, urllib.request
request = urllib.request.Request('http://127.0.0.1:{port}/target/v1/chat/completions',
    data=json.dumps({{'model':'target-client', 'messages':[{{'role':'user','content':'scorer'}}], 'max_tokens':10}}).encode(),
    headers={{'Content-Type':'application/json','Authorization':'Bearer '+os.environ['SCORER_TOKEN']}})
with urllib.request.urlopen(request, timeout=10) as response:
    response.read()
"""
        # Preserve any future import at the original start of the scorer.
        score.write_text(original.replace('import json', call + '\nimport json', 1))
        path.write_text(json.dumps(doc))
        return result
    grouping = dict(MODEL_GROUPING, base_url=f'http://127.0.0.1:{port}/editor/v1',
                    model='editor-client', reasoning_effort='low')
    grouping.pop('proxy_env')
    state, bundle, _ = make_bundle(tmp_path, scaffold=scaffold, model_grouping=grouping,
        bundle_secret_names=('GROUPING_KEY', 'SCORER_TOKEN'), launcher_source=routed_launcher(port))
    prep = PreparationStore(state)
    campaign = prep.prepare_campaign(bundle.digest)
    evaluation = prep.prepare_evaluation(bundle.digest, selection={'all': True},
        repeat_seeds=[31], evaluation_set_id='neutral-evaluation', max_turns=2,
        timeout_seconds=30, metric_plan={'primary': 'score'}, max_failed_turns=1, call_retries='off')
    upstream = _GroupingUpstream(lambda _: json.dumps({'groups': []}))
    document = policy_document(tmp_path, port)
    document['routes']['target'].update(endpoint=f'http://127.0.0.1:{upstream.port}/v1', api_key_secret=None)
    document['routes']['editor'] = dict(document['routes']['target'], client_model='editor-client',
                                      model='grouping-upstream', max_tokens=grouping['max_output_tokens'])
    document['scorer_token_names'] = {'SCORER_TOKEN': 'target'}
    async def scenario():
        async with Owner.open(state) as owner:
            target, digest = await target_under(owner, bundle, document)
            tokens = tuple(owner._targets[target.id].gateway.tokens.values())
            for prepared in (campaign, evaluation):
                call_start = len(upstream.requests)
                accepted = await owner.start(prepared.digest, target_ref=target, secrets={})
                done = await finished(owner, ResourceRef('operation', accepted.id), seconds=90)
                assert done.failure is None, done.to_payload()
                assert done.control_state == 'finished', done.to_payload()
                if prepared == campaign:
                    diagnostic = {
                        'complete': done.result.core['complete'],
                        'suspended': done.result.core.get('suspended'),
                        'suspensions': [{key: row.get(key) for key in ('reason', 'owner')} for row in done.result.core.get('suspensions', [])],
                        'terminal': None if done.core_terminal is None else done.core_terminal.to_payload(),
                        'grouping_max_output_tokens': grouping['max_output_tokens'],
                        'route_max_tokens': document['routes']['editor']['max_tokens'],
                        'requests': [{key: r['body'].get(key) for key in ('model', 'reasoning', 'max_tokens')} for r in upstream.requests],
                    }
                    assert done.result.core['complete'], diagnostic
                else:
                    assert done.kind == 'evaluation' and done.result is not None
                    coverage = done.result.to_payload()['coverage']
                    assert coverage['expected'] == coverage['scored'] > 0
                    assert any(r['body']['messages'][0]['content'] == 'scorer' for r in upstream.requests[call_start:])
                rows = await owner._db('operation_policy_history', accepted.id)
                assert [(row['start'], row['policy_digest']) for row in rows] == [(1, digest)]
                assert owner._targets[target.id].gateway.uses == frozenset()
                assert [f'operation:{accepted.id}:start:1'] in ledger_uses(document)
                routing = await owner.operation_routing(ResourceRef('operation', accepted.id))
                assert routing['available'] is True and routing['shared'] == routing['outside'] == 0
                assert sum(row['calls'] for rows in routing['roles'].values() for row in rows) == len(upstream.requests) - call_start
                assert sum(row['mismatched'] for rows in routing['roles'].values() for row in rows) == 0
                assert ('editor' in routing['roles']) == (prepared == campaign)
                assert 'target' in routing['roles']
            group_calls = [r['body'] for r in upstream.requests if r['body']['model'] == 'grouping-upstream']
            assert group_calls and all(call['reasoning'] == {'effort': 'low'} for call in group_calls)
            assert any(r['body']['messages'][0]['content'] == 'scorer' for r in upstream.requests)
            for path in state.rglob('*'):
                if path.is_file():
                    payload = path.read_bytes()
                    assert not any(token.encode() in payload for token in tokens), str(path)
    try:
        asyncio.run(scenario())
    finally:
        upstream.close()


def test_plain_campaign_cannot_resume_under_policy(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from test_owner_target import ready_target
    flag = tmp_path / 'target-broken'
    flag.touch()
    state, bundle, _ = make_bundle(tmp_path, mode=f'fail_after:1:{flag}', scaffold=scaffold_15_with_legacy_failures)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    document = policy_document(tmp_path, free_port())
    document['routes']['target']['api_key_secret'] = None
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            value = await finished(owner, ref, seconds=90)
            assert value.result.core['suspended'] and value.failure is None
            assert (await owner.operation_routing(ref))['reason'] == 'no_policy'
            assert 'routing' not in (await owner.report(ref)).to_payload()
            await owner.wait(await owner.stop_target(target))
            routed, _ = await target_under(owner, bundle, document)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=routed, secrets={})
            assert caught.value.code == 'policy_continuation_mismatch'
            assert len(await owner._db('operation_policy_history', ref.id)) == 1
            assert ledger_uses(document) == []
    asyncio.run(scenario())
