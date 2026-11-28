"""Confirmed policies are immutable, pair-local records, independent of projects."""
import json
import sqlite3
from types import MappingProxyType

import pytest

from evolution_lab.execution_policy import policy_digest
from evolution_lab.owner import Owner, OwnerError
from evolution_lab.owner.store import OwnerStateStore
from test_execution_policy import policy_payload


async def test_confirmation_round_trip_repeat_and_restart(tmp_path):
    document = policy_payload()
    digest = policy_digest(document)
    original = json.loads(json.dumps(document))
    async with Owner.open(tmp_path / "state") as owner:
        assert await owner.execution_policies() == ()
        record = await owner.confirm_execution_policy(MappingProxyType(document), expected_digest=digest,
            author="operator", channel="cli", note="chosen route")
        payload = record.to_payload()
        assert payload == {"schema": "lab-execution-policy-record/1", "digest": digest,
            "policy": original, "author": "operator", "channel": "cli", "note": "chosen route",
            "confirmed_at": record.confirmed_at}
        assert record.confirmed_at > 0
        repeated = await owner.confirm_execution_policy(original, expected_digest=digest,
            author="another", channel="api", note=None)
        assert repeated == record
        document["gateway"]["port"] = 1111
        payload["policy"]["gateway"]["port"] = 2222
        assert record.to_payload()["policy"] == original
        with pytest.raises(TypeError):
            record.policy["gateway"]["port"] = 3333
        assert await owner.execution_policy(digest) == record
        assert await owner.execution_policies() == (record,)
    async with Owner.open(tmp_path / "state") as owner:
        assert (await owner.execution_policy(digest)).to_payload() == record.to_payload()


async def test_mismatch_and_invalid_document_write_nothing(tmp_path):
    async with Owner.open(tmp_path / "state") as owner:
        with pytest.raises(OwnerError) as error:
            await owner.confirm_execution_policy(policy_payload(), expected_digest="0" * 64,
                author="operator", channel="api")
        assert error.value.code == "digest_mismatch"
        invalid = policy_payload()
        invalid["unexpected"] = "not retained"
        with pytest.raises(OwnerError) as error:
            await owner.confirm_execution_policy(invalid, expected_digest=policy_digest(invalid),
                author="operator", channel="api")
        assert error.value.code == "invalid_argument"
        assert await owner.execution_policies() == ()
        with pytest.raises(OwnerError) as error:
            await owner.execution_policy("0" * 64)
        assert error.value.code == "not_found"


@pytest.mark.parametrize("kwargs", [{"author": ""}, {"channel": None}, {"note": 1},
                                      {"expected_digest": "bad"}, {"author": "\ud800"},
                                      {"channel": "\ud800"}, {"note": "\ud800"}])
async def test_confirmation_validates_metadata(tmp_path, kwargs):
    document = policy_payload()
    arguments = {"expected_digest": policy_digest(document), "author": "operator", "channel": "api"}
    arguments.update(kwargs)
    async with Owner.open(tmp_path / "state") as owner:
        with pytest.raises(OwnerError) as error:
            await owner.confirm_execution_policy(document, **arguments)
        assert error.value.code == "invalid_argument"
        assert await owner.execution_policies() == ()


def test_snapshot_validation_and_digest_are_inside_transaction(tmp_path, monkeypatch):
    import evolution_lab.execution_policy as policy_module
    original_parse = policy_module.parse_policy
    document = policy_payload()
    digest = policy_digest(document)
    expected = json.loads(json.dumps(document))
    with OwnerStateStore.open(tmp_path / "state") as store:
        def mutate_after_validation(snapshot):
            assert store._database.in_transaction
            parsed = original_parse(snapshot)
            document["routes"]["target"]["model"] = "changed outside snapshot"
            return parsed
        monkeypatch.setattr(policy_module, "parse_policy", mutate_after_validation)
        record = store.confirm_execution_policy(document, expected_digest=digest,
            author="operator", channel="api")
        saved = store._database.execute("SELECT body_json FROM execution_policies").fetchone()[0]
        assert json.loads(saved) == expected == record.to_payload()["policy"]
        assert policy_digest(json.loads(saved)) == digest


def test_commit_failure_leaves_no_confirmation(tmp_path):
    with OwnerStateStore.open(tmp_path / "state") as store:
        store._database.set_authorizer(lambda action, arg, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
            else sqlite3.SQLITE_OK)
        with pytest.raises(OwnerError) as error:
            store.confirm_execution_policy(policy_payload(), expected_digest=policy_digest(policy_payload()),
                author="operator", channel="api")
        assert error.value.code == "io_error"
        store._database.set_authorizer(None)
        assert store.execution_policies() == ()


def test_fresh_schema_ten_and_v9_refused_without_migration(tmp_path):
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        db = store._database
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"execution_policies", "operation_start_policies", "probe_session_policies"} <= tables
        assert "execution_policy" in {r[1] for r in db.execute("PRAGMA table_info(targets)")}
    path = state / "owner.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=9")
    before = path.read_bytes()
    with pytest.raises(OwnerError) as error:
        OwnerStateStore.open(state)
    assert error.value.code == "unsupported_schema"
    assert path.read_bytes() == before


async def test_confirmation_preserves_optional_absence_and_closed_record(tmp_path):
    from evolution_lab.owner import PolicyRecord
    document = policy_payload()
    for key in ("target_token_name", "scorer_token_names", "role_groups"):
        del document[key]
    async with Owner.open(tmp_path / "state") as owner:
        record = await owner.confirm_execution_policy(document, expected_digest=policy_digest(document),
                                                       author="operator", channel="api")
        assert record.to_payload()["policy"] == document
        assert record.note is None
        assert PolicyRecord.from_payload(record.to_payload()) == record
        with pytest.raises(OwnerError) as error:
            PolicyRecord.from_payload({**record.to_payload(), "unknown": True})
        assert error.value.code == "invalid_request"


async def test_confirm_v2_policy_without_gateway(tmp_path):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 1, 'delay_seconds': 0}}
    async with Owner.open(tmp_path / 'state') as owner:
        record = await owner.confirm_execution_policy(document, expected_digest=policy_digest(document),
            author='operator', channel='api')
        assert record.to_payload()['policy'] == document


async def test_invalid_v2_confirmation_reports_safe_pointer(tmp_path):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['misspelled'], 'max_resumes': 1, 'delay_seconds': 0}}
    async with Owner.open(tmp_path / 'state') as owner:
        with pytest.raises(OwnerError) as caught:
            await owner.confirm_execution_policy(document, expected_digest=policy_digest(document),
                author='operator', channel='api')
        assert caught.value.code == 'invalid_request'
        assert caught.value.path == '/auto_resume/reasons/0'
        assert await owner.execution_policies() == ()


def test_store_invalid_v2_confirmation_reports_safe_pointer(tmp_path):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['misspelled'], 'max_resumes': 1, 'delay_seconds': 0}}
    with OwnerStateStore.open(tmp_path / 'state') as store:
        with pytest.raises(OwnerError) as caught:
            store.confirm_execution_policy(document, expected_digest=policy_digest(document),
                author='operator', channel='api')
        assert caught.value.code == 'invalid_request'
        assert caught.value.path == '/auto_resume/reasons/0'
        assert store.execution_policies() == ()


@pytest.mark.parametrize('field', ['delay_seconds', 'busy_recheck_seconds'])
@pytest.mark.parametrize('value', [31_536_001, 2**72])
def test_store_confirmation_rejects_interval_with_safe_pointer(tmp_path, field, value):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 0, 'busy_recheck_seconds': 1}}
    document['auto_resume'][field] = value
    with OwnerStateStore.open(tmp_path / 'state') as store:
        with pytest.raises(OwnerError) as caught:
            store.confirm_execution_policy(document, expected_digest=policy_digest(document),
                                           author='operator', channel='api')
        assert caught.value.code == 'invalid_request'
        assert caught.value.path == '/auto_resume/' + field
        assert store.execution_policies() == ()


@pytest.mark.parametrize('field', ['delay_seconds', 'busy_recheck_seconds'])
async def test_public_owner_confirmation_interval_boundary(tmp_path, field):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 31_536_000, 'busy_recheck_seconds': 31_536_000}}
    with OwnerStateStore.open(tmp_path / 'state') as store:
        owner = Owner()
        owner._assert_mutation = lambda: None
        async def db(method, *args, **kwargs):
            return getattr(store, method)(*args, **kwargs)
        owner._db = db
        accepted = await owner.confirm_execution_policy(document, expected_digest=policy_digest(document),
                                                          author='operator', channel='api')
        assert accepted.to_payload()['policy'] == document
        document['auto_resume'][field] = 31_536_001
        with pytest.raises(OwnerError) as caught:
            await owner.confirm_execution_policy(document, expected_digest=policy_digest(document),
                                                 author='operator', channel='api')
        assert caught.value.code == 'invalid_request'
        assert caught.value.path == '/auto_resume/' + field
        assert len(store.execution_policies()) == 1
