"""Worker metadata is a human status addition, with machine bytes preserved."""
import pytest

from test_service_cli import FakeClient, invoke, _status

EXCEPTION = {"class": "evolution.tree.campaign.CampaignInvariantViolated", "code": None}
RECORD = {"schema": "lab-operation-failure/1", "operation_id": "operation", "start": 1,
          "scope_id": "scope", "exception": EXCEPTION, "log": "scopes/scope/stderr.log"}


@pytest.mark.parametrize("code", [None, "manifest_invalid"])
def test_human_status_prints_worker_exception(code):
    status = _status(None)
    status['failure'] = {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    record = {**RECORD, "exception": {**EXCEPTION, "code": code}}
    client = FakeClient([status, record])
    result, output, error = invoke(['status', 'operation'], client)
    assert result == 0, error
    suffix = '' if code is None else ' (code manifest_invalid)'
    assert 'worker exception: ' + EXCEPTION['class'] + suffix + '\n' in output
    assert "traceback: scopes/scope/stderr.log (in the service's state directory)" in output
    assert output.index('worker exception:') > output.index('lab-operation-status/8')
    assert client.calls[-1][1] == '/v1/operations/operation/failure'
    before = invoke(['--json', 'status', 'operation'], FakeClient([status]))
    after_client = FakeClient([status, record])
    assert invoke(['--json', 'status', 'operation'], after_client) == before
    assert before[0] == 0
    assert len(after_client.calls) == 1


def test_missing_failure_metadata_keeps_status_exit_and_output():
    from evolution_lab.cli.client import ClientError
    status = _status(None)
    baseline = invoke(['status', 'operation'], FakeClient([status]))
    status['failure'] = {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    result, output, error = invoke(['status', 'operation'], FakeClient([status, ClientError('unavailable')]))
    assert result == baseline[0] and error == baseline[2]
    assert 'worker exception:' not in output
