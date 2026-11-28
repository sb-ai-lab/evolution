"""Versioned operation and project responses include auto-resume facts."""
import pytest

from evolution_lab.cli.responses import validate
from evolution_lab.owner.store import OwnerStateStore
from test_owner_store_starts import DIGEST, _executor, _ready_target


def operation_payload(tmp_path):
    with OwnerStateStore.open(tmp_path / 'state') as store:
        target = _ready_target(store)
        return store.accept_operation(prepared_digest=DIGEST, target_ref=target,
                                      executor=_executor()).status.to_payload()


def test_status_and_project_start_new_majors_decode(tmp_path):
    status = operation_payload(tmp_path)
    assert status['schema'] == 'lab-operation-status/8'
    assert status['auto_resume'] == {'policy_digest': None, 'state': 'off', 'next_at': None,
                                      'used': 0, 'max': None, 'blocked': None, 'detail': None}
    assert status['starts'][0]['initiated_by'] == 'human'
    assert status['starts'][0]['auto_resume_attempt'] is None
    assert validate(status, 'lab-operation-status/8').to_payload() == status
    payload = {'schema': 'lab-project-start-result/5', 'operation': status, 'link': None}
    assert validate(payload, 'lab-project-start-result/5') == payload


def test_status_decoder_rejects_inconsistent_auto_attempt(tmp_path):
    status = operation_payload(tmp_path)
    status['starts'][0]['auto_resume_attempt'] = 1
    with pytest.raises(ValueError):
        validate(status, 'lab-operation-status/8')


def test_cli_status_prints_auto_resume(tmp_path):
    import io
    from evolution_lab.cli.output import write_value
    payload = operation_payload(tmp_path)
    stream = io.StringIO()
    write_value(stream, payload, machine=False)
    assert 'auto-resume: off' in stream.getvalue()
