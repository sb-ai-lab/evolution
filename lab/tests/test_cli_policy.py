"""Policy CLI uses explicit routes; digest is strictly local."""
import io
import json

import pytest

from evolution_lab.execution_policy import policy_digest
from test_execution_policy import policy_payload
from test_service_cli import FakeClient, invoke


def test_digest_offline_without_socket_resolution_or_client(tmp_path, monkeypatch):
    from evolution_lab.cli import parser
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_payload()))
    def forbidden(*args, **kwargs):
        pytest.fail("offline digest resolved a service dependency")
    monkeypatch.setattr(parser, "resolve_socket", forbidden)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert parser.main(["policy", "digest", str(path)], stdout=stdout, stderr=stderr,
                       client_factory=forbidden) == 0
    assert stdout.getvalue() == policy_digest(policy_payload()) + "\n"
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("action", ["confirm", "list", "show"])
def test_policy_routes(tmp_path, action):
    digest = policy_digest(policy_payload())
    client = FakeClient([{"policies": []} if action == "list" else
                         {"schema": "lab-execution-policy-record/1", "digest": digest}])
    args = ["--socket", "/tmp/lab.sock", "--json"]
    if action == "confirm":
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(policy_payload()))
        args += ["--no-request-id", "policy", "confirm", str(path), "--expect", digest,
                 "--author", "operator", "--channel", "cli", "--note", "chosen"]
    else:
        args += ["policy", action] + ([digest] if action == "show" else [])
    code, output, error = invoke(args, client)
    assert code == 0, error
    assert json.loads(output) == ({"policies": []} if action == "list" else
                                  {"schema": "lab-execution-policy-record/1", "digest": digest})
    method, path, body, mutation, schema = client.calls[0]
    assert method == ("POST" if action == "confirm" else "GET")
    assert path == "/v1/execution-policies" + ("/" + digest if action == "show" else "")
    assert mutation == (action == "confirm")
    if action == "confirm":
        assert body == {"schema": "lab-service-policy-confirm-request/1", "policy": policy_payload(),
                        "expected_digest": digest, "author": "operator", "channel": "cli", "note": "chosen"}


def test_v2_digest_refuses_unknown_reason_with_safe_pointer(tmp_path):
    from evolution_lab.cli import parser
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['misspelled'], 'max_resumes': 1, 'delay_seconds': 0}}))
    stdout, stderr = io.StringIO(), io.StringIO()
    assert parser.main(['policy', 'digest', str(path)], stdout=stdout, stderr=stderr) == 1
    error = json.loads(stderr.getvalue())['error']
    assert error['code'] == 'invalid_request'
    assert '/auto_resume/reasons/0' in error['message']


@pytest.mark.parametrize('field', ['delay_seconds', 'busy_recheck_seconds'])
@pytest.mark.parametrize('value', [31_536_001, 2**72])
def test_v2_confirm_refuses_interval_locally_with_safe_pointer(tmp_path, field, value):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 0, 'busy_recheck_seconds': 1}}
    document['auto_resume'][field] = value
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(document))
    client = FakeClient()
    code, _, stderr = invoke(['--socket', '/tmp/lab.sock', '--no-request-id', 'policy', 'confirm',
                              str(path), '--expect', policy_digest(document), '--author', 'operator',
                              '--channel', 'cli'], client)
    assert code == 1
    error = json.loads(stderr)['error']
    assert error['code'] == 'invalid_request'
    assert '/auto_resume/' + field in error['message']
    assert client.calls == []


def test_v2_confirm_accepts_interval_max(tmp_path):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 31_536_000, 'busy_recheck_seconds': 31_536_000}}
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(document))
    client = FakeClient([{'schema': 'lab-execution-policy-record/1'}])
    code, _, stderr = invoke(['--socket', '/tmp/lab.sock', '--no-request-id', 'policy', 'confirm',
                              str(path), '--expect', policy_digest(document), '--author', 'operator',
                              '--channel', 'cli'], client)
    assert code == 0, stderr
    assert client.calls[0][2]['policy'] == document
