"""Policy preflight, closed secret binding, atomic history and use lifetime."""
import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from test_execution_policy import policy_payload
from test_owner_state import OwnerStateStore, _ready_target, _executor, ONE_DIGEST


def role_binding(tmp_path, *, consumer='editor', changes=None, declared=()):
    from evolution.tree import load_manifest_v2
    from evolution_lab.owner.binding import BundleBinding
    from evolution_lab.execution_policy import parse_policy
    from scaffold_15 import scaffold_15
    source = tmp_path / 'source'
    scaffold_15(source)
    path = source / 'manifest.json'
    document = json.loads(path.read_text())
    document['editor'] = dict(kind='chat', model='editor-client', base_url='http://127.0.0.1:8765/editor/v1',
        api_key_env='CHAT_KEY', context_compact_chars=128000, context_message_max_chars=12000,
        retry={'version': 'role_call_retry/1', 'max_attempts': 2, 'total_budget_seconds': 60,
               'backoff': {'initial_seconds': 1, 'multiplier': 2, 'max_seconds': 8, 'jitter': 'full'}})
    document['scorer']['allowed_env'] = ['SCORE_TOKEN', 'SHARED_TOKEN']
    path.write_text(json.dumps(document))
    binding = BundleBinding('a'*64, 'b'*64, 'campaign', source, path, path, source/'launch.json',
        load_manifest_v2(path), {'secret_names': ['TARGET_TOKEN', 'SHARED_TOKEN'], 'env': {}},
        None, None, {}, {}, ('TARGET_TOKEN', 'SHARED_TOKEN', 'CHAT_KEY', 'SCORE_TOKEN'), None, {})
    section = dataclasses.replace(binding.manifest.workflow.editor,
        **dict(dict(base_url='http://127.0.0.1:8765/editor/v1', model='editor-client'), **(changes or {})))
    workflow = dataclasses.replace(binding.manifest.workflow, editor=section)
    if consumer == 'reflector':
        workflow = dataclasses.replace(workflow, reflector=dataclasses.replace(workflow.reflector, role=section))
    if consumer == 'grouping':
        from test_owner_binding import MODEL_GROUPING
        from evolution.tree import load_manifest_v2
        document = json.loads(binding.manifest_path.read_text())
        document['grouper'] = dict(MODEL_GROUPING, api_key_env='CHAT_KEY', proxy_env=None,
            model=section.model, base_url=section.base_url)
        binding.manifest_path.write_text(json.dumps(document))
        workflow = dataclasses.replace(workflow, grouper=load_manifest_v2(binding.manifest_path).workflow.grouper)
    document = json.loads(binding.manifest_path.read_text())
    key = 'grouper' if consumer == 'grouping' else consumer
    document[key].update(dict(declared))
    binding.manifest_path.write_text(json.dumps(document))
    binding = dataclasses.replace(binding, manifest=dataclasses.replace(binding.manifest, workflow=workflow))
    policy = policy_payload()
    policy['scorer_token_names'] = {'SCORE_TOKEN': 'editor', 'SHARED_TOKEN': 'target'}
    # Shared target name is deliberately NOT the generated target token.
    policy['scorer_token_names'].pop('SHARED_TOKEN')
    return binding, parse_policy(policy)


@pytest.mark.parametrize('changes,code', [
    ({'model': 'wrong'}, 'policy_route_mismatch'),
    ({'base_url': 'http://127.0.0.1:8766/editor/v1'}, 'policy_route_mismatch'),
    ({'base_url': 'http://127.0.0.1:8765/unknown/v1'}, 'policy_route_mismatch'),
])
def test_role_route_refusal_precedes_execution(tmp_path, changes, code):
    from evolution_lab.owner.policy import execution_tokens
    from evolution_lab.owner import OwnerError
    binding, policy = role_binding(tmp_path, changes=changes)
    target = SimpleNamespace(execution_policy=policy, token_bindings={}, gateway=SimpleNamespace(tokens={}))
    with pytest.raises(OwnerError) as caught:
        execution_tokens(binding, target, kind='campaign')
    assert caught.value.code == code


@pytest.mark.parametrize('consumer', ['editor', 'reflector', 'grouping'])
@pytest.mark.parametrize('pin,value', [('provider_only', ['neutral']), ('provider_order', ['neutral']), ('allow_fallbacks', False)])
def test_declared_provider_pin_refused_even_false(tmp_path, consumer, pin, value):
    from evolution_lab.owner.policy import execution_tokens
    from evolution_lab.owner import OwnerError
    binding, policy = role_binding(tmp_path, consumer=consumer, declared=((pin, value),))
    target = SimpleNamespace(execution_policy=policy, token_bindings={}, gateway=SimpleNamespace(tokens={}))
    with pytest.raises(OwnerError) as caught:
        execution_tokens(binding, target, kind='campaign')
    assert caught.value.code == 'manifest_provider_pin'


def test_shared_route_token_supplies_both_roles_and_scorer(tmp_path):
    from evolution_lab.owner.policy import execution_tokens
    from evolution_lab.owner.secrets import bind_execution_secrets, bind_target_secrets, secret_partition
    binding, policy = role_binding(tmp_path, consumer='reflector')
    target_secrets = bind_target_secrets(binding, {'SHARED_TOKEN': 'shared', 'TARGET_TOKEN': 'target'})
    target = SimpleNamespace(execution_policy=policy, gateway=SimpleNamespace(tokens={'editor': 'generated', 'target': 'target'}))
    values = execution_tokens(binding, target, kind='campaign')
    assert values['CHAT_KEY'] == values['SCORE_TOKEN'] == 'generated'
    remaining = {name: 'ordinary' for name in secret_partition(binding).execution if name not in values}
    remaining['SHARED_TOKEN'] = 'shared'
    bound = bind_execution_secrets(binding, remaining, target_secrets=target_secrets, lab_filled=values)
    assert bound.environment['CHAT_KEY'] == bound.environment['SCORE_TOKEN'] == 'generated'
    from evolution_lab.owner import OwnerError
    with pytest.raises(OwnerError) as caught:
        bind_execution_secrets(binding, dict(remaining, CHAT_KEY='override'), target_secrets=target_secrets, lab_filled=values)
    assert caught.value.code == 'secret_conflict'


def test_first_start_policy_history_is_atomic_and_versioned(tmp_path):
    from importlib.metadata import version
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        accepted = store.accept_operation(prepared_digest=ONE_DIGEST, target_ref=target, executor=_executor())
        rows = store._database.execute('SELECT * FROM operation_start_policies').fetchall()
        assert [dict(row) for row in rows] == [dict(operation_id=accepted.status.id, start=1, policy_digest=None, lab_version=version('evolution-lab'))]
        assert store._database.execute('PRAGMA foreign_key_check').fetchall() == []


def test_probe_history_written_in_admission(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        store.admit_probe(session_id='a' * 32, target_id=target.id, max_instances=2)
        assert [dict(row) for row in store._database.execute('SELECT * FROM probe_session_policies')] == [dict(session_id='a' * 32, policy_digest=None)]


def test_overlapping_gateway_use_updates_keep_both_ids_until_close():
    from evolution_lab.owner.target_gateway import TargetGateway
    async def scenario():
        gateway = TargetGateway(None, None)
        observed = []
        async def acknowledged(uses):
            await asyncio.sleep(0)
            observed.append(uses)
        gateway.set_uses = acknowledged  # transport boundary; the actual set-update logic runs
        await asyncio.gather(gateway.open_use('probe:a'), gateway.open_use('probe:b'))
        assert observed[-1] == frozenset({'probe:a', 'probe:b'})
        await gateway.close_use('probe:a')
        assert observed[-1] == frozenset({'probe:b'})
        await gateway.close_use('probe:b')
        assert observed[-1] == frozenset()
    asyncio.run(scenario())


@pytest.mark.parametrize('first,current,code', [
    (None, policy_payload(), 'policy_continuation_mismatch'),
    (policy_payload(), None, 'policy_continuation_mismatch'),
    (policy_payload(), dict(policy_payload(), gateway=dict(policy_payload()['gateway'], port=9876)), 'policy_gateway_changed'),
])
def test_continuation_requires_original_policy_presence_and_gateway(first, current, code):
    from evolution_lab.owner.policy import check_continuation
    from evolution_lab.owner import OwnerError
    with pytest.raises(OwnerError) as caught:
        check_continuation(first, current)
    assert caught.value.code == code


@pytest.mark.parametrize('table,kind', [('operation_start_policies', 'operation'), ('probe_session_policies', 'probe')])
def test_policy_history_failure_rolls_back_admission(tmp_path, table, kind):
    import sqlite3
    from evolution_lab.owner import OwnerError
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        store._database.set_authorizer(lambda action, name, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and name == table else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(OwnerError):
                if kind == 'operation':
                    store.accept_operation(prepared_digest=ONE_DIGEST, target_ref=target, executor=_executor())
                else:
                    store.admit_probe(session_id='a'*32, target_id=target.id, max_instances=2)
        finally:
            store._database.set_authorizer(None)
        assert store._database.execute('SELECT * FROM operations').fetchall() == []
        assert store._database.execute('SELECT * FROM probe_instances').fetchall() == []
        assert set(store.reservations()) == {'target_lifetime'}


def test_resume_history_is_atomic_and_keeps_first_start(tmp_path):
    import sqlite3
    from evolution_lab.owner import Failure, OwnerError
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=ONE_DIGEST, target_ref=target, executor=_executor())
        store.settle_pre_dispatch_failure(first.status.id, Failure.make('dispatch_refused', 'before_start'))
        store.update_operation(first.status.id, results_state='present')
        store._database.set_authorizer(lambda action, name, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and name == 'operation_start_policies' else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(OwnerError):
                store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        finally:
            store._database.set_authorizer(None)
        assert [row['start'] for row in store.operation_policy_history(first.status.id)] == [1]
        assert [start.start for start in store.operation_status(first.status.id).starts] == [1]
        assert set(store.reservations()) == {'target_lifetime'}
        store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert [(row['start'], row['policy_digest']) for row in store.operation_policy_history(first.status.id)] == [(1, None), (2, None)]
        assert store._database.execute('PRAGMA foreign_key_check').fetchall() == []


def test_cross_route_role_token_name_conflict_and_undeclared_binding(tmp_path):
    from evolution_lab.owner.policy import execution_tokens
    from evolution_lab.owner import OwnerError
    binding, policy = role_binding(tmp_path, consumer='reflector')
    section = dataclasses.replace(binding.manifest.workflow.reflector.role, model='target-client', base_url='http://127.0.0.1:8765/target/v1')
    workflow = dataclasses.replace(binding.manifest.workflow, reflector=dataclasses.replace(binding.manifest.workflow.reflector, role=section))
    binding = dataclasses.replace(binding, manifest=dataclasses.replace(binding.manifest, workflow=workflow))
    target = SimpleNamespace(execution_policy=policy, gateway=SimpleNamespace(tokens={'editor': 'generated', 'target': 'target'}))
    with pytest.raises(OwnerError) as caught:
        execution_tokens(binding, target, kind='campaign')
    assert caught.value.code == 'token_name_conflict'
    assert caught.value.failure.message == 'Token name CHAT_KEY conflicts between consumers: editor, reflector.'
    binding = dataclasses.replace(binding, secret_names=('TARGET_TOKEN', 'SHARED_TOKEN', 'SCORE_TOKEN'))
    with pytest.raises(OwnerError) as caught:
        execution_tokens(binding, target, kind='campaign')
    assert caught.value.code == 'binding_mismatch'


def test_no_policy_task_zero_reader_and_saved_cli_outputs_match():
    from io import StringIO
    from pathlib import Path
    from dev62_default_outputs import read_outputs
    from evolution_lab.cli.output import write_value
    import report_md_compat
    tests = Path(__file__).parent
    saved = tests / 'data/dev64-default-outputs/task0'
    for name, value in read_outputs(tests / 'fixtures/panel/b14/agent-finished/run').items():
        expected = (saved / name).read_bytes()
        if name == 'campaign.json':
            assert value.pop('lab_notices_state') == 'present'
            expected = expected.replace(b'lab-campaign-view/14', b'lab-campaign-view/15', 1)
        elif name == 'owner-report.json':
            assert value['report'].pop('lab_notices_state') == 'present'
            assert value['report']['core'].pop('baseline_import') is None
            expected = expected.replace(b'lab-owner-report/12', b'lab-owner-report/15', 1)
            for measurement in value['report']['measurements']:
                for field in ('unanswered', 'unanswered_state', 'answered_reported'):
                    measurement.pop(field)
        elif name == 'report.md':
            assert report_md_compat.current(value) == report_md_compat.legacy(expected.decode())
            continue
        if name in {'campaign.json', 'owner-report.json'}:
            value = report_md_compat.without_operator_progress(value)
        actual = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        assert actual.encode() == expected
    for name in ('target', 'operation', 'campaign', 'owner-report', 'live-owner-report'):
        value = json.loads((saved / (name + '.json')).read_text())
        if name == 'target':
            value['schema'] = 'lab-owner-target/2'
        if name == 'campaign':
            value.update(schema='lab-campaign-view/15', lab_notices_state='present')
        if name in ('owner-report', 'live-owner-report'):
            value['schema'] = 'lab-owner-report/15'
        stream = StringIO()
        write_value(stream, value, machine=False)
        expected = (saved / (name + '.txt')).read_bytes()
        if name == 'campaign':
            expected = expected.replace(b'lab-campaign-view/14', b'lab-campaign-view/15', 1)
        if name == 'target':
            expected = expected.replace(b'lab-owner-target/1', b'lab-owner-target/2')
        if name in ('owner-report', 'live-owner-report'):
            expected = expected.replace(b'lab-owner-report/12', b'lab-owner-report/15', 1)
        assert stream.getvalue().encode() == expected


def test_lost_open_use_ack_stops_target_before_another_use():
    from evolution_lab.owner.target_gateway import TargetGateway
    from evolution_lab.owner import OwnerError
    async def scenario():
        stopped = asyncio.Event()
        async def stop():
            stopped.set()
        gateway = TargetGateway(None, SimpleNamespace(request_stop=stop))
        async def lost_ack(uses):
            raise OwnerError('gateway_unavailable', 'before_start')
        gateway.set_uses = lost_ack
        with pytest.raises(OwnerError):
            await gateway.open_use('operation:neutral:start:1')
        assert stopped.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize('mapping,code', [(None, 'token_name_conflict'), ('editor', None), ('target', 'token_name_conflict')])
def test_scorer_overlap_requires_explicit_same_route_mapping(tmp_path, mapping, code):
    from evolution_lab.execution_policy import parse_policy
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.policy import execution_tokens
    binding, policy = role_binding(tmp_path)
    workflow = dataclasses.replace(binding.manifest.workflow,
        scorer=dataclasses.replace(binding.manifest.workflow.scorer, allowed_env=frozenset({'CHAT_KEY'})))
    binding = dataclasses.replace(binding, manifest=dataclasses.replace(binding.manifest, workflow=workflow))
    document = policy.to_payload()
    document['scorer_token_names'] = {} if mapping is None else {'CHAT_KEY': mapping}
    target = SimpleNamespace(execution_policy=parse_policy(document), gateway=SimpleNamespace(tokens={'editor':'generated','target':'target'}))
    if code:
        with pytest.raises(OwnerError) as caught:
            execution_tokens(binding, target, kind='campaign')
        assert caught.value.code == code
        assert caught.value.failure.message == 'Token name CHAT_KEY conflicts between consumers: editor, scorer.'
    else:
        assert execution_tokens(binding, target, kind='campaign')['CHAT_KEY'] == 'generated'


def test_cancelled_close_use_ack_stops_target_and_refuses_later_use():
    from evolution_lab.owner.target_gateway import TargetGateway
    from evolution_lab.owner import OwnerError
    async def scenario():
        stopped = asyncio.Event()
        async def stop():
            stopped.set()
        gateway = TargetGateway(None, SimpleNamespace(request_stop=stop))
        async def acknowledged(uses):
            pass
        gateway.set_uses = acknowledged
        await gateway.open_use('operation:neutral:start:1')
        async def lost_ack(uses):
            raise asyncio.CancelledError()
        gateway.set_uses = lost_ack
        with pytest.raises(asyncio.CancelledError):
            await gateway.close_use('operation:neutral:start:1')
        assert stopped.is_set()
        gateway.set_uses = acknowledged
        with pytest.raises(OwnerError) as caught:
            await gateway.open_use('operation:later:start:1')
        assert caught.value.code == 'gateway_unavailable'
    asyncio.run(scenario())


def test_continuation_allows_changed_auto_resume_and_compares_absent_gateways():
    from evolution_lab.owner.policy import check_continuation
    from evolution_lab.owner import OwnerError
    first = {'schema': 'lab-execution-policy/2', 'auto_resume': {'reasons': ['editor_failing'], 'max_resumes': 1, 'delay_seconds': 0}}
    current = {'schema': 'lab-execution-policy/2'}
    check_continuation(first, current)
    with pytest.raises(OwnerError) as caught:
        check_continuation(first, policy_payload())
    assert caught.value.code == 'policy_gateway_changed'
