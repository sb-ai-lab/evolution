"""V-S5-4: explicit CLI mapping, acceptance, secrets and ZIP publication."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest


DIGEST = "a" * 64
TASK_REF = {"schema": "lab-task-ref/1", "id": "task", "owner_generation": "generation"}
HEALTH = {"schema": "lab-service-health/2", "lab_version": "l", "core_version": "c", "distributions": [],
          "owner_generation": "gen-1", "active_operation_id": None, "blockers": [],
          "service_state": "running", "service_environment": None}
CONNECTION = {"schema": "lab-owner-connection/1", "id": "connection", "source_kind": "campaign",
              "bundle_digest": DIGEST, "launch_digest": DIGEST, "owner_generation": "gen-1"}


def _event_frame(control_state="finished", **overrides):
    """A complete closed ``lab-operation-event/3`` status frame body."""
    value = {
        "schema": "lab-operation-event/3", "operation_id": "operation", "owner_generation": "generation",
        "stream_id": "stream", "sequence": 1, "kind": "status",
        "payload": {
            "kind": "campaign", "control_state": control_state,
            "process": {"launched": True, "exit_code": 0, "cleanup_complete": True, "forced": False},
            "target_cleanup": {"state": "not_needed", "complete": True},
            "results_state": "present", "failure": None, "result_available": True,
            "persistence": {"state": "saved", "failure": None},
            "progress": {"kind": "campaign", "phase": "finished", "stages": ["finishing"],
                         "rounds": {"completed": 1, "current": None, "planned": 1},
                         "attempts": {"current_round": {"closed": 0, "running": 0, "planned": 2}, "total": 2},
                         "coverage": None, "policy_state": "present",
                         "observed_at": "2026-09-29T00:01:00Z"},
            "progress_state": "present",
            "times": {"first_event_at": "2026-09-29T00:00:00Z", "last_event_at": "2026-09-29T00:01:00Z"},
            "times_state": "present", "times_source": "core_raw", "times_reason": None,
            "start": 1,
        },
    }
    value.update(overrides)
    return value


class FakeClient:
    """Controlled service boundary; no listener, Owner or target is created."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.keys = []
        self.body = b"zip"

    def request_json(self, method, path, *, payload=None, mutation=False, expected_schema=None, mutation_key=None):
        self.calls.append((method, path, payload, mutation, expected_schema))
        self.keys.append(mutation_key)
        value = self.responses.pop(0) if self.responses else {"schema": "lab-health/1"}
        if isinstance(value, BaseException):
            raise value
        if mutation and expected_schema == "lab-task-ref/1":
            from evolution_lab.owner import TaskRef
            try:
                self._accepted_task = TaskRef.from_payload(value)
            except Exception:
                pass
            else:
                self._accepted_task_ref = value
        return value

    def stream_events(self, path):
        self.calls.append(("GET", path, None, False, "events"))
        yield {"event": "status", "data": _event_frame()}

    def get_bytes(self, path, content_type):
        self.calls.append(("GET", path, None, False, content_type))
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


def invoke(argv, client):
    from evolution_lab.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout=stdout, stderr=stderr, client_factory=lambda **_: client)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(("argv", "expected"), [
    (["--socket", "/tmp/lab.sock", "bundle", "show", DIGEST],
     ("GET", f"/v1/bundles/{DIGEST}", None, False, "lab-bundle-summary/3")),
    (["--socket", "/tmp/lab.sock", "prepared", "show", DIGEST],
     ("GET", f"/v1/prepared-executions/{DIGEST}", None, False, "lab-prepared-summary/1")),
    (["--socket", "/tmp/lab.sock", "--no-request-id", "connect", DIGEST],
     ("POST", "/v1/connections/from-bundle", {"schema": "lab-service-bind-request/1", "bundle_digest": DIGEST}, True, "lab-owner-connection/1")),
    (["--socket", "/tmp/lab.sock", "target", "status", "target", "--generation", "generation"],
     ("GET", "/v1/targets/target?owner_generation=generation", None, False, "lab-owner-target/2")),
    (["--socket", "/tmp/lab.sock", "--no-request-id", "task", "stop", "task"],
     ("POST", "/v1/tasks/task/stop", {}, True, "lab-owner-stop/1")),
    (["--socket", "/tmp/lab.sock", "status", "operation"],
     ("GET", "/v1/operations/operation", None, False, "lab-operation-status/8")),
    (["--socket", "/tmp/lab.sock", "--no-request-id", "delete-results", "operation"],
     ("DELETE", "/v1/operations/operation/results", None, True, "lab-task-ref/1")),
    (['--socket', '/tmp/lab.sock', 'sample', 'operation', 'm', 'case', '00'],
     ("GET", "/v1/operations/operation/measurements/m/cases/case/samples/00", None, False, "lab-sample/1")),
    (['--socket', '/tmp/lab.sock', 'attempt', 'operation', 'r01-a01'],
     ("GET", "/v1/operations/operation/attempts/r01-a01", None, False, "lab-attempt/1")),
    (['--socket', '/tmp/lab.sock', 'reflection', 'operation', 'r01-x01'],
     ("GET", "/v1/operations/operation/reflections/r01-x01", None, False, "lab-reflection/1")),
    (["--socket", "/tmp/lab.sock", "health"],
     ("GET", "/v1/health", None, False, "lab-service-health/2")),
])
def test_commands_have_one_explicit_public_request(argv, expected):
    client = FakeClient([{"schema": expected[-1]}])
    code, _, stderr = invoke(argv, client)
    assert code == 0, stderr
    assert client.calls == [expected]


def test_bundle_create_sends_schema_2_and_accepts_service_marker(tmp_path):
    request_file = tmp_path / "bundle.json"
    request_file.write_text(json.dumps({
        "source": {"kind": "evaluation", "input_path": "/abs/i.json", "launch_path": "/abs/l.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [], "roots": [], "path_bindings": [],
    }))
    client = FakeClient([{"schema": "lab-bundle-summary/3"}])
    code, _, stderr = invoke(
        ["--socket", "/tmp/lab.sock", "--no-request-id", "bundle", "create", "--request", str(request_file)], client)
    assert code == 0, stderr
    (method, path, payload, mutation, expected_schema), = client.calls
    assert method == "POST"
    assert path == "/v1/bundles"
    assert payload["schema"] == "lab-service-bundle-request/2"
    assert payload["environments"] == [{"kind": "service"}]
    assert mutation is True
    assert expected_schema == "lab-bundle-summary/3"


def test_bundle_create_prints_warning_lines_in_human_output(tmp_path):
    request_file = tmp_path / "bundle.json"
    request_file.write_text(json.dumps({
        "source": {"kind": "evaluation", "input_path": "/abs/i.json", "launch_path": "/abs/l.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [], "roots": [], "path_bindings": [],
    }))
    bundle_summary = {
        "schema": "lab-bundle-summary/3", "digest": DIGEST, "source_kind": "evaluation",
        "measurement_basis": DIGEST,
        "entrypoint": "inputs/project/input.json", "launch_digest": DIGEST,
        "environment_digests": [DIGEST], "secret_names": [], "entry_count": 1, "limitations": [],
        "warnings": [{"code": "lab_version_mismatch", "detail": {"expected": "a", "found": None}}],
    }
    client = FakeClient([bundle_summary])
    code, stdout, stderr = invoke(
        ["--socket", "/tmp/lab.sock", "--no-request-id", "bundle", "create", "--request", str(request_file)], client)
    assert code == 0, stderr
    lines = stdout.splitlines()
    assert lines[0].startswith("lab-bundle-summary/3")
    assert lines[1] == "warning: lab_version_mismatch expected=a found=-"


def test_client_rejects_bundle_summary_schema_1_response():
    import httpx
    from evolution_lab.cli.client import AcceptanceUnknown, ServiceClient

    legacy_bundle = {
        "schema": "lab-bundle-summary/1", "digest": DIGEST, "source_kind": "evaluation",
        "entrypoint": "inputs/project/input.json", "launch_digest": DIGEST,
        "environment_digests": [DIGEST], "secret_names": [], "entry_count": 1, "limitations": [],
    }
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=legacy_bundle)))
    with pytest.raises(AcceptanceUnknown):
        client.request_json("POST", "/v1/bundles", mutation=True, expected_schema="lab-bundle-summary/3")


def test_wait_flushes_accepted_ref_then_polls_without_stop():
    client = FakeClient([TASK_REF, {
        "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation",
        "action": "delete", "ref": None, "state": "succeeded", "result": None,
        "failure": None, "persistence": {"state": "saved", "failure": None},
    }])
    code, stdout, stderr = invoke([
        "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "delete-results", "operation", "--wait",
    ], client)
    assert code == 0, stderr
    assert [json.loads(line) for line in stdout.splitlines()] == [TASK_REF, client.calls[1] and {
        "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation",
        "action": "delete", "ref": None, "state": "succeeded", "result": None,
        "failure": None, "persistence": {"state": "saved", "failure": None},
    }]
    assert client.calls == [
        ("DELETE", "/v1/operations/operation/results", None, True, "lab-task-ref/1"),
        ("GET", "/v1/tasks/task?owner_generation=generation", None, False, "lab-task-status/1"),
    ]


def test_ambiguous_mutation_response_is_exit_125_without_retry():
    from evolution_lab.cli.client import AcceptanceUnknown

    client = FakeClient([AcceptanceUnknown("timeout")])
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--no-request-id", "connect", DIGEST], client)
    assert code == 125
    assert stdout == ""
    assert json.loads(stderr)["acceptance"] == "unknown"
    assert len(client.calls) == 1


def test_mutation_without_flags_reads_health_then_sends_generated_key():
    from evolution_lab.service.mutations import REQUEST_ID
    client = FakeClient([HEALTH, CONNECTION])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "connect", DIGEST], client)
    assert code == 0, stderr
    assert [call[:2] for call in client.calls] == [("GET", "/v1/health"), ("POST", "/v1/connections/from-bundle")]
    assert client.keys[0] is None
    request_id, generation = client.keys[1]
    assert REQUEST_ID.fullmatch(request_id) and len(request_id) >= 32 and generation == "gen-1"


class RefusingClient(FakeClient):
    def request_json(self, method, path, **values):
        self.calls.append((method, path))
        raise AssertionError("local inputs must be validated before any service call")


@pytest.mark.parametrize(("command", "content"), [
    (["bundle", "create", "--request"], {"schema": "lab-service-bundle-request/2"}),
    (["bundle", "create", "--request"], {"source": "not-a-source"}),
    (["prepare", "--request"], {"schema": "lab-service-prepare-request/2"}),
    (["prepare", "--request"], {"kind": "unknown"}),
    (["start", DIGEST, "--target", "target", "--generation", "gen-1", "--secrets-file"], {"TOKEN": 1}),
    (["target", "start", "connection", "--secrets-file"], ["not", "a", "map"]),
    (["conformance", "connection", "--target", "target", "--generation", "gen-1", "--runtime-policy"], {"bad": 1}),
])
def test_invalid_local_inputs_fail_before_key_resolution_as_in_s5(tmp_path, command, content):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(content))
    s5 = invoke(["--socket", "/tmp/lab.sock", "--no-request-id", *command, str(path)], RefusingClient([]))
    client = RefusingClient([])
    keyed = invoke(["--socket", "/tmp/lab.sock", *command, str(path)], client)
    assert keyed == s5 and keyed[0] in {1, 2}
    assert client.calls == []
    if "schema" in content:
        assert keyed[0] == 2 and keyed[2].startswith("usage:")


def test_explicit_key_skips_health():
    client = FakeClient([CONNECTION])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "--request-id", "r-1", "--generation", "gen-1",
                              "connect", DIGEST], client)
    assert code == 0, stderr
    assert client.keys == [("r-1", "gen-1")]


def test_no_request_id_sends_s5_mutation():
    client = FakeClient([CONNECTION])
    assert invoke(["--socket", "/tmp/lab.sock", "--no-request-id", "connect", DIGEST], client)[0] == 0
    assert client.keys == [None] and len(client.calls) == 1


@pytest.mark.parametrize("prefix", [
    ["--request-id", "r-1"], ["--generation", "gen-1"],
    ["--no-request-id", "--generation", "gen-1"], ["--request-id", "bad id", "--generation", "gen-1"],
])
def test_incomplete_or_invalid_key_flags_are_usage_errors(prefix):
    client = FakeClient([])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", *prefix, "connect", DIGEST], client)
    assert code == 2 and stderr.startswith("usage:") and client.calls == []


@pytest.mark.parametrize("command", [["health"], ["status", "operation"], ["list"]])
def test_key_flags_on_reads_are_usage_errors(command):
    client = FakeClient([])
    for prefix in (["--request-id", "r", "--generation", "g"], ["--no-request-id"]):
        assert invoke(["--socket", "/tmp/lab.sock", *prefix, *command], client)[0] == 2
    assert client.calls == []


def test_key_flags_on_serve_are_usage_errors():
    code, _, _ = invoke(["--request-id", "r", "--generation", "g", "serve", "--socket", "/tmp/s.sock",
                         "--state", "/tmp/state"], FakeClient([]))
    assert code == 2


@pytest.mark.parametrize("machine", [[], ["--json"]])
@pytest.mark.parametrize("nonce", ["a" * 32, "-" + "a" * 31], ids=["normal", "leading-dash"])
def test_acceptance_unknown_prints_key_and_manual_retry_reuses_it(machine, nonce, monkeypatch):  # W28-6
    from evolution_lab.cli import parser
    from evolution_lab.cli.client import AcceptanceUnknown
    monkeypatch.setattr(parser.secrets, "token_urlsafe", lambda count: nonce)
    client = FakeClient([HEALTH, AcceptanceUnknown("timeout")])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", *machine, "connect", DIGEST], client)
    assert code == 125
    printed = json.loads(stderr)
    assert printed["acceptance"] == "unknown"
    assert (printed["request_id"], printed["owner_generation"]) == client.keys[1]
    retry = FakeClient([CONNECTION])
    code, _, _ = invoke(["--socket", "/tmp/lab.sock", "--request-id", printed["request_id"],
                         "--generation", printed["owner_generation"], "connect", DIGEST], retry)
    assert code == 0 and retry.keys == [client.keys[1]]


def test_table_full_suggests_no_request_id_without_applying_it():
    from evolution_lab.cli.client import ApiError
    envelope = {"error": {"code": "request_table_full", "stage": "transport",
                          "message": "The service request key table is full."}}
    client = FakeClient([HEALTH, ApiError(envelope)])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "connect", DIGEST], client)
    printed = json.loads(stderr)
    assert code == 1 and printed["error"] == envelope["error"]
    assert "--no-request-id" in printed["hint"]
    assert len(client.calls) == 2


def test_service_client_sends_key_headers_only_for_mutations():
    import httpx
    from evolution_lab.cli.client import ServiceClient
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, json=CONNECTION)

    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(handler))
    client.request_json("POST", "/v1/connections/from-bundle", payload={}, mutation=True,
                        expected_schema="lab-owner-connection/1", mutation_key=("r-1", "gen-1"))
    assert seen[0]["lab-request-id"] == "r-1" and seen[0]["lab-owner-generation"] == "gen-1"
    with pytest.raises(ValueError):
        client.request_json("GET", "/v1/health", mutation_key=("r-1", "gen-1"))
    assert len(seen) == 1


def test_secret_file_is_closed_json_and_never_echoed(tmp_path):
    secret_file = tmp_path / "secrets.json"
    secret_file.write_text('{"token":"not-for-output"}')
    client = FakeClient([TASK_REF])
    code, stdout, stderr = invoke([
        "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "target", "start", "connection",
        "--secrets-file", str(secret_file),
    ], client)
    assert code == 0
    assert "not-for-output" not in stdout + stderr
    assert client.calls == [(
        "POST", "/v1/connections/connection/target",
        {"schema": "lab-service-target-start-request/1", "secrets": {"token": "not-for-output"}},
        True, "lab-task-ref/1",
    )]


def test_secret_fd_and_help_need_no_service_listener_or_server_import():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"token":"fd-secret"}')
    finally:
        os.close(write_fd)
    try:
        client = FakeClient([TASK_REF])
        code, stdout, stderr = invoke([
            "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "target", "start", "connection",
            "--secrets-fd", str(read_fd),
        ], client)
    finally:
        os.close(read_fd)
    assert code == 0, stderr
    assert "fd-secret" not in stdout + stderr
    assert client.calls[0][2]["secrets"] == {"token": "fd-secret"}

    from evolution_lab.cli import main
    help_stdout, help_stderr = io.StringIO(), io.StringIO()
    assert main(["--help"], stdout=help_stdout, stderr=help_stderr) == 0
    assert "usage:" in help_stdout.getvalue()
    assert help_stderr.getvalue() == ""


def test_verified_download_does_not_clobber_and_removes_its_partial_file(tmp_path):
    from evolution_lab.cli.download import publish_export

    payload = b"zip bytes"
    digest = hashlib.sha256(payload).hexdigest()
    output = tmp_path / "export.zip"
    publish_export(output, payload, digest)
    assert output.read_bytes() == payload
    with pytest.raises(FileExistsError):
        publish_export(output, payload, digest)
    assert list(tmp_path.glob(".export.zip.*")) == []


def test_download_does_not_remove_a_colliding_foreign_temporary_file(tmp_path, monkeypatch):
    from evolution_lab.cli.download import publish_export
    import evolution_lab.cli.download as download

    monkeypatch.setattr(download.secrets, "token_hex", lambda _: "foreign")
    output = tmp_path / "export.zip"
    foreign = tmp_path / ".export.zip.foreign.partial"
    foreign.write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        publish_export(output, b"zip", hashlib.sha256(b"zip").hexdigest())
    assert foreign.read_bytes() == b"foreign"


def test_client_marks_malformed_task_acceptance_unknown_and_allows_schema_free_diff():
    import httpx
    from evolution_lab.cli.client import AcceptanceUnknown, ServiceClient

    responses = iter([
        httpx.Response(202, json={"schema": "lab-task-ref/1", "id": "task"}),
        httpx.Response(200, json={"operation_id": "operation", "state": "no_change", "text": None, "nontext_changes": []}),
    ])
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: next(responses)))
    with pytest.raises(AcceptanceUnknown):
        client.request_json("DELETE", "/v1/operations/operation/results", mutation=True, expected_schema="lab-task-ref/1")
    assert client.request_json("GET", "/v1/operations/operation/diff") == {
        "operation_id": "operation", "state": "no_change", "text": None, "nontext_changes": [],
    }


def test_local_ref_and_digest_validation_happens_before_any_http_request():
    client = FakeClient()
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "bundle", "show", "not-a-digest"], client)
    assert code == 2
    assert client.calls == []
    assert "usage:" in stderr


def test_wait_interrupt_and_nonterminal_event_eof_have_distinct_local_failures():
    client = FakeClient([TASK_REF, KeyboardInterrupt()])
    code, stdout, stderr = invoke([
        "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "delete-results", "operation", "--wait",
    ], client)
    assert code == 130
    assert json.loads(stdout) == TASK_REF
    assert json.loads(stderr)["error"]["code"] == "wait_interrupted"

    class Events(FakeClient):
        def stream_events(self, path):
            yield {"event": "status", "data": _event_frame("running")}

    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "events", "operation"], Events())
    assert code == 1
    assert json.loads(stderr)["error"]["code"] == "interrupted_observation"


def test_client_keeps_configured_timeout_and_refuses_incomplete_mutation_success_or_error():
    import httpx
    from evolution_lab.cli.client import AcceptanceUnknown, ServiceClient

    seen = []
    responses = iter([
        httpx.Response(201, json={"schema": "lab-owner-connection/1", "id": "connection"}),
        httpx.Response(409, json={"error": {"code": "busy", "stage": "read", "message": "busy"}, "extra": True}),
        httpx.Response(200, json={"operation_id": "operation", "state": "no_change", "text": None, "nontext_changes": []}),
    ])
    def handler(request):
        seen.append(request.extensions["timeout"])
        return next(responses)
    client = ServiceClient(socket_path="/tmp/lab.sock", timeout=2, transport=httpx.MockTransport(handler))
    with pytest.raises(AcceptanceUnknown):
        client.request_json("POST", "/v1/connections/from-bundle", mutation=True, expected_schema="lab-owner-connection/1")
    with pytest.raises(AcceptanceUnknown):
        client.request_json("POST", "/v1/connections/from-bundle", mutation=True, expected_schema="lab-owner-connection/1")
    client.request_json("GET", "/v1/operations/operation/diff")
    assert seen[-1]["connect"] == 5
    assert seen[-1]["read"] == 2


def test_client_rejects_closed_dto_and_failure_defects_and_marks_decode_interrupt_unknown(monkeypatch):
    import httpx
    import evolution_lab.cli.client as client_module
    from evolution_lab.cli.client import AcceptanceUnknown, ClientError, ServiceClient

    task_status = {
        "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation",
        "action": "delete", "ref": None, "state": "succeeded", "result": None,
        "failure": {"code": "invented", "stage": "read", "message": "untrusted"},
        "persistence": {"state": "saved", "failure": None},
    }
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=task_status)))
    with pytest.raises(ClientError):
        client.request_json("GET", "/v1/tasks/task?owner_generation=generation", expected_schema="lab-task-status/1")

    response = httpx.Response(202, json=TASK_REF)
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: response))
    monkeypatch.setattr(client_module, "decode_json_object", lambda *_, **__: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(AcceptanceUnknown) as caught:
        client.request_json("DELETE", "/v1/operations/operation/results", mutation=True, expected_schema="lab-task-ref/1")
    assert caught.value.cause == "interrupted"


def test_wait_timeout_is_immediate_and_interrupt_after_acceptance_keeps_ref(monkeypatch):
    class TimeoutOnce(FakeClient):
        def request_json(self, *args, **kwargs):
            self.calls.append(args)
            if len(self.calls) == 1:
                from evolution_lab.owner import TaskRef
                self._accepted_task = TaskRef.from_payload(TASK_REF)
                self._accepted_task_ref = TASK_REF
                return TASK_REF
            if len(self.calls) == 2:
                from evolution_lab.cli.client import RequestTimeout
                raise RequestTimeout()
            raise AssertionError("poll retried after its timeout")

    code, stdout, stderr = invoke([
        "--socket", "/tmp/lab.sock", "--json", "--timeout", "30", "--no-request-id", "delete-results", "operation", "--wait",
    ], TimeoutOnce())
    assert code == 124
    assert json.loads(stdout) == TASK_REF
    assert json.loads(stderr)["task_ref"] == TASK_REF

    import evolution_lab.cli.parser as parser_module
    original = parser_module.write_value
    calls = []
    def interrupted(stream, value, *, machine):
        calls.append(value)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        original(stream, value, machine=machine)
    monkeypatch.setattr(parser_module, "write_value", interrupted)
    client = FakeClient([TASK_REF, {
        "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation",
        "action": "delete", "ref": None, "state": "succeeded", "result": None,
        "failure": None, "persistence": {"state": "saved", "failure": None},
    }])
    code, stdout, stderr = invoke([
        "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "delete-results", "operation", "--wait",
    ], client)
    assert code == 130
    assert json.loads(stdout) == TASK_REF
    assert json.loads(stderr)["task_ref"] == TASK_REF


def test_client_rejects_invalid_primitive_preparation_summary_fields():
    import httpx
    from evolution_lab.cli.client import AcceptanceUnknown, ServiceClient

    invalid_bundle = {
        "schema": "lab-bundle-summary/3", "digest": None, "source_kind": "wrong",
        "measurement_basis": None,
        "entrypoint": "/absolute", "launch_digest": "a" * 64,
        "environment_digests": [], "secret_names": [], "entry_count": "not-an-int", "limitations": [],
        "warnings": [],
    }
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=invalid_bundle)))
    with pytest.raises(AcceptanceUnknown):
        client.request_json("POST", "/v1/bundles", mutation=True, expected_schema="lab-bundle-summary/3")


def test_interrupt_between_validated_acceptance_and_wait_uses_accepted_ref(monkeypatch):
    import httpx
    import evolution_lab.cli.parser as parser_module
    import evolution_lab.cli.responses as responses_module
    from evolution_lab.cli import main
    from evolution_lab.cli.client import ServiceClient

    calls = 0
    original = responses_module.TaskRef.from_payload.__func__

    def task_ref_from_payload(cls, payload):
        nonlocal calls
        calls += 1
        return original(cls, payload)

    monkeypatch.setattr(responses_module.TaskRef, "from_payload", classmethod(task_ref_from_payload))
    monkeypatch.setattr(parser_module, "_wait_for_task", lambda *_, **__: (_ for _ in ()).throw(KeyboardInterrupt()))
    client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: httpx.Response(202, json=TASK_REF)))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main([
        "--socket", "/tmp/lab.sock", "--json", "--no-request-id", "delete-results", "operation", "--wait",
    ], stdout=stdout, stderr=stderr, client_factory=lambda **_: client)
    assert code == 130
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["task_ref"] == TASK_REF
    assert calls == 1


@pytest.mark.parametrize(("process", "expected"), [
    ({"launched": True, "exit_code": 0, "cleanup_complete": True, "forced": False}, "process=exit:0"),
    ({"launched": True, "exit_code": None, "cleanup_complete": None, "forced": None}, "process=launched"),
    ({"launched": False, "exit_code": None, "cleanup_complete": None, "forced": None}, "process=not_launched"),
    ({"launched": True, "exit_code": -9, "cleanup_complete": True, "forced": True}, "process=exit:-9,forced"),
])
def test_human_output_summarises_process_evidence(process, expected):
    from evolution_lab.cli.output import write_value

    stream = io.StringIO()
    write_value(stream, {"schema": "s", "id": "x", "state": "finished", "process": process}, machine=False)
    line = stream.getvalue().splitlines()[0]
    assert expected in line.split()
    assert "None" not in line


def _status(core, refusal=None):
    process = {"launched": True, "exit_code": 1, "forced": False}
    return {"schema": "lab-operation-status/8", "id": "op-1", "control_state": "finished",
            "process": process,
            "resume": ({"state": "ready", "reason": None, "target": None}
                       if isinstance(core, dict) and core.get("suspended") is True else None),
            "result": {"kind": "campaign", "core": core},
            "starts": [{"start": 1, "scope_id": "scope-1", "owner_generation": "generation-1",
                        "target_ref": {"schema": "lab-target-ref/1", "id": "target-1",
                                       "owner_generation": "generation-1"},
                        "process": process,
                        "target_cleanup": {"state": "confirmed", "complete": True},
                        "failure": None, "settlement": "positive", "refusal": refusal}],
            "refusal": refusal}


def test_human_output_names_a_suspended_campaign_and_the_continuation_line():
    import io
    from evolution_lab.cli.output import write_value
    core = {"suspended": True, "suspensions": [
        {"seq": 5, "reason": "scorer_unstable", "owner": "scorer", "evidence_kind": "threshold"},
        {"seq": 9, "reason": "target_unstable", "owner": "target", "evidence_kind": "threshold"}]}
    stream = io.StringIO()
    write_value(stream, _status(core), machine=False)
    lines = stream.getvalue().splitlines()
    assert "process=exit:1" in lines[0]
    assert lines[1] == "suspended: target_unstable (owner=target, evidence=threshold)"
    assert lines[2] == ("continuation: lab resume op-1 --target <ready target of the same bundle> "
                        "--generation <owner generation>")


@pytest.mark.parametrize(("reason", "owner", "evidence"), [
    ("editor_failing", "editor", "stage_record"), ("reflector_provider_failed", "reflector", "stage_record"),
    ("scorer_metric_out_of_range", "scorer", "fault_rows"), ("scorer_metric_missing", "scorer", "fault_rows")])
def test_human_output_prints_the_hint_after_the_suspended_line(reason, owner, evidence):
    import io
    from evolution_lab._suspension import suspension_hint
    from evolution_lab.cli.output import write_value
    core = {"suspended": True, "suspensions": [
        {"seq": 4, "reason": reason, "owner": owner, "evidence_kind": evidence}]}
    stream = io.StringIO()
    write_value(stream, _status(core), machine=False)
    lines = stream.getvalue().splitlines()
    assert lines[1] == f"suspended: {reason} (owner={owner}, evidence={evidence})"
    assert lines[2] == "hint: " + suspension_hint(reason, "en")
    assert "lab resume" not in lines[2]
    assert lines[3].startswith("continuation: lab resume op-1")
    assert "fresh target" not in stream.getvalue() and "target_unstable" not in stream.getvalue()


def test_machine_output_has_no_hint_for_a_scorer_suspension():
    import io, json
    from evolution_lab.cli.output import write_value
    core = {"suspended": True, "suspensions": [
        {"seq": 4, "reason": "scorer_metric_out_of_range", "owner": "scorer", "evidence_kind": "fault_rows"}]}
    stream = io.StringIO()
    write_value(stream, _status(core), machine=True)
    assert "hint" not in json.dumps(json.loads(stream.getvalue()))


def test_human_output_has_no_hint_for_target_unstable():
    import io
    from evolution_lab.cli.output import write_value
    core = {"suspended": True, "suspensions": [
        {"seq": 9, "reason": "target_unstable", "owner": "target", "evidence_kind": "threshold"}]}
    stream = io.StringIO()
    write_value(stream, _status(core), machine=False)
    assert "hint:" not in stream.getvalue()


_FRESH_TARGET = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
                 "excluded": [{"schema": "lab-target-ref/1", "id": "T1", "owner_generation": "G1"},
                              {"schema": "lab-target-ref/1", "id": "T2", "owner_generation": "G2"}]}
_FRESH_LINE = ("on a fresh target: stop target T2 if it is still running "
               "(lab agent stop T2 --generation G2 --wait), start a new target of the same bundle (lab agent start <connection>), "
               "then lab resume OP --target <new target> --generation <owner generation>")


@pytest.mark.parametrize("state,reason,expected", [
    ("ready", None, f"continuation: {_FRESH_LINE}"),
    ("cleanup_first", "cleanup_required", f"continuation: lab cleanup operation OP, then {_FRESH_LINE}"),
    ("busy", "resource_busy", None),
])
def test_continuation_line_names_a_fresh_target(state, reason, expected):
    from evolution_lab.cli.output import continuation_line
    value = {"id": "OP", "control_state": "finished",
             "resume": {"state": state, "reason": reason, "target": _FRESH_TARGET}}
    assert continuation_line(value) == expected


def test_continuation_line_stop_hint_uses_target_ref_not_last_excluded():
    from evolution_lab.cli.output import continuation_line
    # starts A, B, A: ``excluded`` keeps first occurrences [A, B]; the current target is A (T1).
    ref = {"schema": "lab-target-ref/1", "id": "T1", "owner_generation": "G1"}
    value = {"id": "OP", "control_state": "finished", "target_ref": ref,
             "resume": {"state": "ready", "reason": None, "target": _FRESH_TARGET}}
    line = continuation_line(value)
    assert "stop target T1 if it is still running (lab agent stop T1 --generation G1 --wait)" in line
    assert "T2" not in line


_RESUME_LINE = "lab resume OP --target <ready target of the same bundle> --generation <owner generation>"


@pytest.mark.parametrize("resume,expected", [
    ({"state": "ready", "reason": None, "target": None}, f"continuation: {_RESUME_LINE}"),
    ({"state": "cleanup_first", "reason": "cleanup_required", "target": None},
     f"continuation: lab cleanup operation OP, then {_RESUME_LINE}"),
    ({"state": "cleanup_first", "reason": "cleanup_unconfirmed", "target": None},
     f"continuation: lab cleanup operation OP, then {_RESUME_LINE}"),
    ({"state": "refused", "reason": "cleanup_unconfirmed", "target": None},
     "continuation: not available after manual release; the run directory is kept until results are deleted"),
    ({"state": "refused", "reason": "campaign_unreadable", "target": None}, None),
    ({"state": "busy", "reason": "resource_busy", "target": None}, None),
    (None, None),
])
def test_continuation_line_follows_the_resume_field(resume, expected):
    from evolution_lab.cli.output import continuation_line
    value = {"id": "OP", "control_state": "finished", "resume": resume,
             "result": {"core": {"suspended": True, "suspensions": [
                 {"seq": 3, "reason": "target_unstable", "owner": "target", "evidence_kind": "threshold"}]}}}
    assert continuation_line(value) == expected


def _full_status(resume):
    from dataclasses import replace
    from evolution_lab.owner.models import (
        CleanupFacts, ExecutorIdentity, OperationStatus, ProcessFacts, ResumeVerdict, TargetRef,
    )
    executor = ExecutorIdentity(environment_digest="0" * 64, lab_wheel_sha256="1" * 64,
        core_wheel_sha256="2" * 64, module_hashes={"m": "2" * 64})
    status = OperationStatus("op1", "gen1", "d" * 64, TargetRef("t1", "gen1"), "finished",
        ProcessFacts(True, 1, True, False), CleanupFacts("confirmed", True), None, None,
        "present", executor)
    return replace(status, resume=None if resume is None else ResumeVerdict(*resume)).to_payload()


def test_status_decoder_is_closed_on_schema_and_resume():
    from evolution_lab.cli.responses import validate
    payload = _full_status(("cleanup_first", "cleanup_unconfirmed"))
    decoded = validate(payload, "lab-operation-status/8")
    assert decoded.resume.to_payload() == {
        "state": "cleanup_first", "reason": "cleanup_unconfirmed", "target": None}
    assert validate(_full_status(None), "lab-operation-status/8").resume is None
    listed = validate({"schema": "lab-operation-list/8", "operations": [payload]}, "lab-operation-list/8")
    assert listed[0].resume.state == "cleanup_first"
    old = dict(payload, schema="lab-operation-status/2")
    missing = {key: value for key, value in payload.items() if key != "resume"}
    extra = dict(payload, resume={"state": "ready", "reason": None, "target": None, "x": 1})
    for bad, schema in ((old, "lab-operation-status/2"), (old, "lab-operation-status/8"),
                        (missing, "lab-operation-status/8"),
                        (dict(old, schema="lab-operation-status/5"), "lab-operation-status/8"),
                        (extra, "lab-operation-status/8")):
        with pytest.raises(ValueError):
            validate(bad, schema)


@pytest.mark.parametrize("core", [None, {"suspended": False, "suspensions": []}, {"complete": True}])
def test_human_output_has_no_suspension_line_otherwise(core):
    import io
    from evolution_lab.cli.output import write_value
    stream = io.StringIO()
    write_value(stream, _status(core), machine=False)
    assert "suspended:" not in stream.getvalue()


def test_machine_output_is_the_payload_unchanged():
    import io, json
    from evolution_lab.cli.output import write_value
    value = _status({"suspended": True, "suspensions": [
        {"seq": 1, "reason": "probe_unavailable", "owner": "campaign", "evidence_kind": "stage_record"}]})
    stream = io.StringIO()
    write_value(stream, value, machine=True)
    assert json.loads(stream.getvalue()) == value


def _sse_client(*frames):
    import httpx
    from evolution_lab.cli.client import ServiceClient

    body = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in frames)
    return ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})))


def test_event_stream_accepts_a_closed_v2_status_frame_and_passes_error_frames():
    frame = _event_frame()
    error = {"error": {"code": "not_found", "stage": "read", "message": "not found"}}
    events = list(_sse_client(("status", frame), ("error", error)).stream_events("/v1/operations/o/events"))
    assert events == [{"event": "status", "data": frame}, {"event": "error", "data": error}]


@pytest.mark.parametrize("frame", [
    _event_frame(schema="lab-operation-event/1"),
    _event_frame(extra=True),
    {**_event_frame(), "payload": {**_event_frame()["payload"], "extra": 1}},
    {**_event_frame(), "payload": {k: v for k, v in _event_frame()["payload"].items() if k != "start"}},
    _event_frame(sequence=True),
    {**_event_frame(), "payload": {**_event_frame()["payload"], "progress": {"kind": "evaluation", "coverage": None}}},
])
def test_event_stream_rejects_frames_outside_the_closed_v2_contract(frame):
    from evolution_lab.cli.client import ClientError

    with pytest.raises(ClientError, match="invalid service response"):
        list(_sse_client(("status", frame)).stream_events("/v1/operations/o/events"))


def test_event_stream_rejects_an_unknown_event_name():
    from evolution_lab.cli.client import ClientError

    with pytest.raises(ClientError, match="invalid service response"):
        list(_sse_client(("progress", _event_frame())).stream_events("/v1/operations/o/events"))


@pytest.mark.parametrize(("extra", "path"), [
    ([], "/v1/targets"),
    (["--ready"], "/v1/targets?state=ready"),
    (["--bundle", DIGEST, "--ready"], f"/v1/targets?bundle={DIGEST}&state=ready"),
])
def test_target_list_reads_the_target_list_route(extra, path):
    client = FakeClient([{"schema": "lab-owner-target-list/1", "targets": []}])
    code, out, _ = invoke(["--socket", "/tmp/lab.sock", "target", "list", *extra], client)
    assert code == 0 and out.startswith("lab-owner-target-list/1")
    assert client.calls == [("GET", path, None, False, "lab-owner-target-list/1")]


def test_status_lookup_of_a_ready_target_only_drops_the_substitution_on_failure():
    from evolution_lab.cli.parser import _ready_target
    value = {"resume": {"target": _FRESH_TARGET}}
    row = {"id": "T3", "owner_generation": "G1", "state": "ready", "bundle_digest": "b" * 64}
    used = {"id": "T1", "owner_generation": "G1", "state": "stopped", "bundle_digest": "b" * 64}
    class Listing(FakeClient):
        def request_json(self, method, path, **kw):
            return {"schema": "lab-owner-target-list/1", "targets": [used, row]}
    assert _ready_target(Listing(), value) == {"id": "T3", "owner_generation": "G1"}
    assert _ready_target(FakeClient([RuntimeError("down")]), value) is None
    assert _ready_target(FakeClient(), {"resume": {"target": None}}) is None


SLOT_HELD = {"error": {"code": "target_slot_held", "stage": "before_start",
                       "message": "Another target of this Owner is ready; stop it before starting a target of a different Bundle."}}


def test_slot_held_prints_the_holding_target_in_text_mode():
    from evolution_lab.cli.client import ApiError
    listing = {"schema": "lab-owner-target-list/1", "targets": [
        {"id": "t-old", "owner_generation": "g1", "state": "ready", "bundle_digest": "b" * 64}]}
    client = FakeClient([ApiError(SLOT_HELD), listing])
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--no-request-id", "target", "start", "conn"], client)
    assert code == 1
    assert json.loads(stderr)["error"]["code"] == "target_slot_held"
    assert stdout == "held by: t-old (generation g1) — lab agent stop t-old --generation g1 --wait\n"
    assert client.calls[-1][1] == "/v1/targets?state=ready"


def test_slot_held_lookup_failure_prints_only_the_refusal():
    from evolution_lab.cli.client import ApiError
    client = FakeClient([ApiError(SLOT_HELD), RuntimeError("down")])
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--no-request-id", "target", "start", "conn"], client)
    assert code == 1 and stdout == "" and json.loads(stderr)["error"]["code"] == "target_slot_held"


def test_slot_held_in_json_mode_makes_no_lookup():
    from evolution_lab.cli.client import ApiError
    client = FakeClient([ApiError(SLOT_HELD)])
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "--no-request-id", "target", "start", "conn"], client)
    assert code == 1 and stdout == "" and len(client.calls) == 1


@pytest.mark.parametrize(("measurement", "quoted"), [
    ("m-r01-champion", "m-r01-champion"), ("m-r01-champion@e1", "m-r01-champion%40e1"),
])
def test_measurements_accepts_an_epoch_id_and_sends_it_percent_encoded(measurement, quoted):
    client = FakeClient([{"schema": "lab-measurement/4"}])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "measurements", "operation", measurement], client)
    assert code == 0, stderr
    assert [call[:2] for call in client.calls] == [("GET", f"/v1/operations/operation/measurements/{quoted}")]


@pytest.mark.parametrize("measurement", ["m@e0", "m@e01", "m@e1@e2", "m/../x"])
def test_measurements_rejects_a_malformed_epoch_id_before_any_request(measurement):
    client = FakeClient()
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "measurements", "operation", measurement], client)
    assert code == 2
    assert client.calls == []


def test_epoch_suffix_is_not_accepted_on_an_operation_id():
    client = FakeClient()
    code, _, _ = invoke(["--socket", "/tmp/lab.sock", "measurements", "operation@e1"], client)
    assert code == 2
    assert client.calls == []


def _repair_rows(**base_extra):
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import BASE, CAND, CORE
    core = dict(CORE, measurements=[dict(BASE, **base_extra), CAND])
    return measurement_rows(core, "campaign", {}, "score")


def _repair_list(rows):
    return {"schema": "lab-measurement-list/4", "operation_id": "op-" + "0" * 32, "measurements": rows}


def test_measurement_list_2_accepts_repairs_object_and_null():
    from evolution_lab.cli import responses
    responses.validate(_repair_list(_repair_rows(repair_tries=2, repairs_scored=1)), "lab-measurement-list/4")
    responses.validate(_repair_list(_repair_rows()), "lab-measurement-list/4")


@pytest.mark.parametrize("bad", [{"tries": 1}, {"tries": 1, "pairs_scored": 0, "x": 0},
                                 {"tries": -1, "pairs_scored": 0}, {"tries": 1, "pairs_scored": 2},
                                 {"tries": 1.0, "pairs_scored": 0}, {"tries": True, "pairs_scored": 0}])
def test_measurement_row_refuses_bad_repairs(bad):
    from evolution_lab.cli import responses
    rows = _repair_rows(repair_tries=2, repairs_scored=1)
    rows[0]["repairs"] = bad
    with pytest.raises(ValueError):
        responses.validate(_repair_list(rows), "lab-measurement-list/4")


def test_old_schema_literals_are_refused():
    from evolution_lab.cli import responses
    value = _repair_list(_repair_rows())
    for old in ("lab-measurement-list/1", "lab-measurement/1", "lab-campaign-view/2", "lab-campaign-view/3",
                "lab-campaign-view/4", "lab-campaign-view/5", "lab-campaign-view/7", "lab-campaign-view/8", "lab-owner-report/4", "lab-owner-report/5"):
        with pytest.raises(ValueError):
            responses.validate(dict(value, schema=old), old)
    with pytest.raises(ValueError, match="invalid public response"):
        responses.validate(dict(value, schema="lab-measurement-list/1"), "lab-measurement-list/4")


def test_measurement_line_shows_repairs_only_when_core_has_them():
    from evolution_lab.cli.output import _measurement_line
    row = {"measurement_id": "m-r00-champion", "role": "baseline", "coverage": {"scored": 3, "expected": 3},
           "metrics": {"score": {"mean": 0.5, "count": 3}}, "baseline": None,
           "repairs": {"tries": 3, "pairs_scored": 1}}
    assert _measurement_line(row).endswith("repairs=3/1")
    assert "repairs=" not in _measurement_line(dict(row, repairs=None))


def test_measurement_lines_read_the_new_schemas():
    from evolution_lab.cli.output import _measurement_lines
    row = {"measurement_id": "m", "role": "baseline", "coverage": {}, "metrics": {}, "baseline": None,
           "repairs": None}
    assert len(_measurement_lines({"schema": "lab-measurement-list/4", "measurements": [row]})) == 1
    assert len(_measurement_lines({"schema": "lab-measurement/4", "measurement": row})) == 1
    assert _measurement_lines({"schema": "lab-measurement-list/1", "measurements": [row]}) == []


def test_detailed_preparation_failure_is_a_known_failure():
    from evolution_lab.cli import responses
    from evolution_lab.preparation import PreparationError
    detail = "missing_key at campaign.gate.mode: expected exactly 'statistical' or 'observe'"
    payload = PreparationError("manifest_invalid", "bundle", detail).failure.to_payload()
    assert payload == {"code": "manifest_invalid", "stage": "bundle",
                       "message": "The campaign manifest is invalid. " + detail}
    assert responses._failure(payload) is None
    corpus = PreparationError("artifacts_invalid", "prepare", "partial_split_labels at line 2").failure.to_payload()
    assert responses._failure(corpus) is None
    with pytest.raises(ValueError):
        responses._failure(dict(payload, message="Another text."))
    with pytest.raises(ValueError):
        PreparationError("io_error", "bundle", "x")  # only the two codes that carry core's diagnosis


def test_failure_with_a_non_string_message_is_invalid():
    from evolution_lab.cli import responses
    for code, stage in (("manifest_invalid", "bundle"), ("merge_failed", "x")):
        with pytest.raises(ValueError):
            responses._failure({"code": code, "stage": stage, "message": 5})


def test_report_text_prints_the_lab_notice_for_family_10():
    from evolution_lab.cli.output import _report_lines
    value = {"schema": "lab-owner-report/15", "report": {"core": {"rounds": []}, "lab_notices": [
        {"code": "no_candidate_measured", "rounds": 2, "by_reason": {"reflection_failed": 2}}]}}
    assert "no candidate was measured: reflection_failed 2" in _report_lines(value)


def test_notice_with_no_rounds_prints_a_dash_not_a_dangling_colon():
    """A campaign with a core outcome and zero rounds: the empty `by_reason` reads `-`."""
    from evolution_lab.cli.output import notice_line
    assert notice_line({"code": "no_candidate_measured", "rounds": 0, "by_reason": {},
                        "reflection_errors": {}}) == "no candidate was measured: -"


def test_campaign_text_prints_core_outcome_before_the_notice():
    """PDR-0025 (codex r1 #1): the notice stands next to core's outcome, never instead of it; the outcome line is
    printed always, `-` while core has no terminal outcome."""
    from evolution_lab.cli.output import _campaign_lines
    notice = {"code": "no_candidate_measured", "rounds": 1, "by_reason": {"reflection_failed": 1},
              "reflection_errors": {}}
    view = {"schema": "lab-campaign-view/15", "family": 14, "gate_mode": "statistical", "caveats": [], "details_state": "present",
            "outcome": {"outcome": "completed", "reason": "rounds_exhausted"}, "lab_notices": [notice], "rounds": []}
    assert _campaign_lines(view) == ["outcome: completed/rounds_exhausted",
                                     "gate: statistical", "no candidate was measured: reflection_failed 1"]
    assert _campaign_lines(dict(view, outcome=None, lab_notices=[])) == ["outcome: -", "gate: statistical"]
    assert _campaign_lines(dict(view, outcome={"outcome": "stopped", "reason": None}, lab_notices=[])) == [
        "outcome: stopped/-", "gate: statistical"]


def test_report_text_prints_core_outcome_before_the_notice():
    """PDR-0025 (codex r1 #1): `lab report` prints core's outcome before Lab's notice, for family14; the gate line stays beside it."""
    import json
    from pathlib import Path
    from evolution_lab.cli.output import _report_lines
    notice = {"code": "no_candidate_measured", "rounds": 2, "by_reason": {"reflection_failed": 2}}
    core14_minimal = {"campaign_outcome": {"outcome": "completed", "reason": "rounds_exhausted", "continuation": None},
              "rounds": [], "caveats": []}
    value = {"schema": "lab-owner-report/15", "report": {"operation": "campaign", "core": core14_minimal, "lab_notices": [notice]}}
    assert _report_lines(value) == ["outcome: completed/rounds_exhausted",
                                    "no candidate was measured: reflection_failed 2", "gate: statistical"]
    core14_minimal["campaign_outcome"] = None
    assert _report_lines(value)[0] == "outcome: -"
    core14 = json.loads((Path(__file__).parent / "fixtures/family14/observe-result.json").read_text())
    value = {"schema": "lab-owner-report/15", "report": {"operation": "campaign", "core": core14, "lab_notices": []}}
    outcome = core14["campaign_outcome"]
    assert _report_lines(value) == [
        f"outcome: {outcome['outcome']}/{outcome['reason'] or '-'}",
        "gate: observe — not evidence of quality (observe_gate_not_enforced, uncalibrated_point_estimates, champion_not_contemporaneous)"]
    evaluation = {"schema": "lab-owner-report/15", "report": {"operation": "evaluation", "core": {"rounds": []}}}
    assert _report_lines(evaluation) == []


def test_round_line_prints_a_dash_for_a_missing_error_class():
    from evolution_lab.cli.output import _campaign_lines
    view = {"schema": "lab-campaign-view/15", "family": 14, "gate_mode": "statistical", "caveats": [], "details_state": "present",
            "lab_notices": [], "rounds": [{"round": 1, "outcome": "unplanned", "baseline_version": "v0",
                                           "attempts": [], "reflection": {"state": "unsettled", "error_class": None,
                                                                          "reason": "some_reason"}}]}
    assert "round 1 unplanned baseline=v0 reflection=unsettled(-: some_reason)" in _campaign_lines(view)


EVALUATION_REQUEST = {
    "schema": "lab-service-prepare-request/3",
    "kind": "evaluation",
    "bundle_digest": DIGEST,
    "selection": {"count": 1},
    "repeat_seeds": [1, None],
    "evaluation_set_id": None,
    "max_turns": None,
    "timeout_seconds": None,
    "metric_plan": None,
    "runtime_policy": {"stop_grace_seconds": 1, "cleanup_wait_seconds": 2},
    "max_failed_turns": None,
    "call_retries": None,
    "split": None,
}


def test_prepare_request_file_gets_3_only_when_it_names_a_split(tmp_path):
    from evolution_lab.cli.parser import _request_file
    body = {k: v for k, v in EVALUATION_REQUEST.items() if k not in {"schema", "split"}}
    old = tmp_path / "old.json"; old.write_text(json.dumps(body))
    new = tmp_path / "new.json"; new.write_text(json.dumps({**body, "split": "test"}))
    assert _request_file(str(old), "lab-service-prepare-request", None)["schema"] == "lab-service-prepare-request/2"
    assert _request_file(str(new), "lab-service-prepare-request", None)["schema"] == "lab-service-prepare-request/3"


def test_report_human_output_names_the_observe_gate():
    import json
    from pathlib import Path
    from evolution_lab.cli.output import _report_lines
    core = json.loads((Path(__file__).parent / "fixtures/family14/observe-result.json").read_text())
    value = {"schema": "lab-owner-report/15", "report": {"operation": "campaign", "core": core}}
    assert _report_lines(value) == [
        "outcome: completed/rounds_exhausted",
        "gate: observe — not evidence of quality (observe_gate_not_enforced, uncalibrated_point_estimates, champion_not_contemporaneous)"]
    assert _report_lines({"schema": "lab-owner-report/15", "report": {"core": {"alpha": {}, "caveats": []}}}) == []
    assert _report_lines({"schema": "lab-owner-report/15", "report": None}) == []


def test_report_human_output_of_family_15_has_no_family_14_gate():
    """A family-15 report reads its ``lab_independent`` block (spec 2026-10-19 D7), never the family-14 gate."""
    from evolution_lab.cli.output import _report_lines
    from evolution_lab.owner.independent_view import independent_view
    from test_owner_independent_view import BLOCK
    core = {"family_version": 15, "campaign_outcome": None, "caveats": [], "rounds": []}
    report = {"operation": "campaign", "core": core, "lab_independent": independent_view(BLOCK)}
    lines = _report_lines({"schema": "lab-owner-report/15", "report": report})
    assert lines[0] == "outcome: -" and not any(line.startswith("gate:") for line in lines)
    assert "r01-a01 in_progress finalist/passed" in lines
    assert "  gate passed/passed (focus) · vs round champion · stored measurement · ref m-reference" in lines
    assert _report_lines({"schema": "lab-owner-report/15", "report": dict(report, lab_independent=None)}) == [
        "outcome: -", "independent: unavailable (unreadable)"]
    unreadable = dict(report, lab_independent=None, lab_independent_state="unavailable",
                      lab_independent_reason="unreadable")
    assert _report_lines({"schema": "lab-owner-report/15", "report": unreadable})[-1] == \
        "independent: unavailable (unreadable)"
    assert _report_lines({"schema": "lab-owner-report/4", "report": report}) == []


def test_report_human_output_marks_unknown_gate_when_caveats_are_missing():
    import json
    from pathlib import Path
    from evolution_lab.cli.output import _report_lines
    core = json.loads((Path(__file__).parent / "fixtures/family14/observe-result.json").read_text())
    del core["caveats"]
    value = {"schema": "lab-owner-report/15", "report": {"operation": "campaign", "core": core}}
    assert _report_lines(value) == ["outcome: completed/rounds_exhausted",
                                     "gate: unknown (unreadable) — not evidence of quality"]


def test_sample_command_percent_encodes_core_ids():
    client = FakeClient([{"schema": "lab-sample/1"}])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "sample", "op1", "m-r01-a01", "кейс+1@a", "00", "--disclose-private"], client)
    assert code == 0, stderr
    assert client.calls[-1][:2] == ("GET", "/v1/operations/op1/measurements/m-r01-a01/cases/%D0%BA%D0%B5%D0%B9%D1%81%2B1%40a/samples/00?disclose=private")


def test_sample_command_refuses_bad_ids_before_any_request():
    client = FakeClient()
    code, _, _ = invoke(["--socket", "/tmp/lab.sock", "sample", "op1", "m", "a b", "00"], client)
    assert code == 2 and client.calls == []


def _admitted_sample(critical_pass, *, disclose=False):
    return {
        "schema": "lab-sample/1", "operation_id": "op1", "measurement_id": "m-r01-a01",
        "case_id": "case", "sample_id": "00", "source": "core_raw",
        "disclosure": "private" if disclose else "public",
        "masking": "keys", "truncated": False, "truncation": [], "execution_state": "finished",
        "trace": {"state": "available"},
        "try": {"score_request_id": "score1", "retry_ordinal": 0, "tries_total": 1},
        "input": {"public": {"question": "hello"}}, "turns": [], "turns_omitted": 0,
        "terminal": "completed", "scoring": {
            "state": "admitted", "metrics": {"score": 0.75}, "critical_pass": critical_pass,
            "verdicts": [], "public_feedback": None,
        }, "private": ({"oracle": "expected", "diagnostics": None, "details": None, "verdict_notes": []}
                       if disclose else {"withheld": "private"}),
    }


@pytest.mark.parametrize("critical_pass", [None, True, False])
@pytest.mark.parametrize("disclose", [False, True])
def test_sample_service_client_preserves_optional_critical_pass(critical_pass, disclose):
    import httpx
    body = _admitted_sample(critical_pass, disclose=disclose)
    with _bytes_client(httpx.Response(200, json=body)) as client:
        result = client.request_json("GET", "/v1/operations/op1/measurements/m-r01-a01/cases/case/samples/00",
                                     expected_schema="lab-sample/1")
    assert result["scoring"] == {"state": "admitted", "metrics": {"score": 0.75},
                                  "critical_pass": critical_pass, "verdicts": [], "public_feedback": None}


@pytest.mark.parametrize("critical_pass", [None, True, False])
@pytest.mark.parametrize("disclose", [False, True])
def test_sample_cli_json_preserves_optional_critical_pass(critical_pass, disclose):
    import httpx
    body = _admitted_sample(critical_pass, disclose=disclose)
    with _bytes_client(httpx.Response(200, json=body)) as client:
        code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "sample", "op1",
                                       "m-r01-a01", "case", "00"] + (["--disclose-private"] if disclose else []), client)
    assert code == 0, stderr
    assert json.loads(stdout)["scoring"]["critical_pass"] is critical_pass
    assert json.loads(stdout)["scoring"]["verdicts"] == []


@pytest.mark.parametrize(("critical_pass", "text"), [(None, "—"), (True, "true"), (False, "false")])
def test_sample_cli_text_shows_optional_critical_pass(critical_pass, text):
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "sample", "op1", "m-r01-a01", "case", "00"],
                                   FakeClient([_admitted_sample(critical_pass)]))
    assert code == 0, stderr
    assert f"critical_pass: {text}" in stdout.splitlines()


@pytest.mark.parametrize("invalid", ["yes", 1, 0, 1.0, [], {}])
def test_sample_decoder_rejects_non_boolean_critical_pass(invalid):
    from evolution_lab.cli.responses import validate
    with pytest.raises(ValueError):
        validate(_admitted_sample(invalid), "lab-sample/1")


def test_sample_decoder_requires_the_critical_pass_key():
    from evolution_lab.cli.responses import validate
    body = _admitted_sample(True)
    del body["scoring"]["critical_pass"]
    with pytest.raises(ValueError):
        validate(body, "lab-sample/1")


@pytest.mark.parametrize("state", ["failed", "not_scored"])
@pytest.mark.parametrize(("field", "value"), [("metrics", {}), ("critical_pass", False),
                                               ("verdicts", []), ("public_feedback", "feedback")])
def test_sample_decoder_still_rejects_unadmitted_scorer_output(state, field, value):
    from evolution_lab.cli.responses import validate
    body = _admitted_sample(None)
    body["scoring"] = {"state": state, "metrics": None, "critical_pass": None,
                       "verdicts": None, "public_feedback": None}
    validate(body, "lab-sample/1")
    body["scoring"][field] = value
    with pytest.raises(ValueError):
        validate(body, "lab-sample/1")


def test_measurement_and_report_clients_preserve_null_critical_pass():
    import httpx
    from evolution_lab.owner.measurements import measurement_rows
    from evolution_lab.owner.models import ReportResult
    from test_owner_measurements import _entry, _s
    baseline = _entry("m-r00-champion", [_s("case", "00", 0.5, critical=None)])
    candidate = _entry("m-r01-a01", [_s("case", "00", 0.75, critical=None)])
    core = {"measurements": [baseline, candidate], "rounds": [{"round": 1,
            "baseline_measurement_id": "m-r00-champion", "attempts": [
                {"attempt_id": "r01-a01", "measurement_id": "m-r01-a01"}]}]}
    (row,) = measurement_rows(core, "campaign", {}, "score", detail="m-r01-a01")
    measurement = {"schema": "lab-measurement/4", "operation_id": "op1", "measurement": row}
    report = ReportResult("op1", "available", {"operation": "campaign", "core": core, "lab_notices": [], "lab_notices_state": "present"}, None).to_payload()
    for body, path in [(measurement, "/measurements/m-r01-a01"), (report, "/report")]:
        with _bytes_client(httpx.Response(200, json=body)) as client:
            result = client.request_json("GET", "/v1/operations/op1" + path, expected_schema=body["schema"])
        if body is measurement:
            sample = result["measurement"]["samples"][0]
            assert sample["critical_pass"] is None
            assert sample["baseline"]["critical_pass"] is None
        else:
            assert result["report"]["core"]["measurements"][0]["status"]["samples"][0]["critical_pass"] is None


FAMILY14 = Path(__file__).parent / "fixtures" / "family14"


def _view(core, progress=None):
    """The view of a core projection without lineage or round details (they are not what these tests check)."""
    from evolution_lab.owner.campaign_views import campaign_view, details_unavailable
    from evolution_lab.owner.progress import CampaignPlan
    return campaign_view("op14", core, CampaignPlan("c.json", 5, 4, "score"), progress, None, None, [],
                         details_unavailable("core_raw_unsupported"))


def _family14_connection_suspended():
    core = json.loads((FAMILY14 / "observe-status.json").read_text())
    core["connection"] = {"state": "suspended", "outcome": "connection_incompatible", "attempts": 1,
                          "binding_digest": "a" * 64, "operation_id": "connection-1",
                          "detail": {"code": "connection_field_empty", "metric": "score",
                                     "manifest_path": "/content", "observed_path": "/content"}}
    core["suspended"] = True
    core["suspensions"] = [{"seq": 3, "reason": "connection_incompatible", "owner": "connection",
                            "evidence_kind": "stage_record"}]
    core["campaign_outcome"] = None
    core["rounds"] = []
    return core


def test_campaign_text_of_family_14():
    from evolution_lab.cli.output import _campaign_lines
    lines = _campaign_lines(_view(json.loads((FAMILY14 / "observe-result.json").read_text())))
    assert lines[0].startswith("outcome: ")
    assert any(line.startswith("gate: observe — not evidence of quality") for line in lines)
    assert any(line.startswith("settings: stability=stable round_stop=first_accepted refresh=") for line in lines)
    assert any(line.startswith("connection: compatible compatible attempts=") for line in lines)
    promoted = next(line for line in lines if line.startswith("round ") and " promoted " in line)
    assert " final=selected:" in promoted and "cohort=" not in promoted
    finalist = next(line for line in lines if " decision=accepted/" in line)
    assert finalist.lstrip().startswith("r0") and " accepted " in finalist


def test_campaign_text_of_a_connection_suspension():
    from evolution_lab.cli.output import _campaign_lines
    lines = _campaign_lines(_view(_family14_connection_suspended(), {"phase": "suspended"}))
    assert "connection: suspended connection_incompatible attempts=1 detail=connection_field_empty at /content" in lines
    marker = lines.index("suspended: connection_incompatible")
    assert lines[marker + 1].startswith("hint: The target's or the scorer's answer does not meet")
    assert "continue with the same action" in lines[marker + 1] and "start a new campaign" in lines[marker + 1]


def test_status_prints_the_refusal_of_the_latest_start():
    import io
    from evolution_lab.cli.output import write_value
    stream = io.StringIO()
    write_value(stream, _status(None, refusal={"code": "corpus_inside_role_workspace", "path": "lab/corpus",
                                               "class": "hard", "instance_digest": None}), machine=False)
    assert "refused: corpus_inside_role_workspace at lab/corpus (hard)" in stream.getvalue()


def test_status_without_refusal_prints_no_refused_line():
    import io
    from evolution_lab.cli.output import write_value
    stream = io.StringIO()
    write_value(stream, _status(None, refusal=None), machine=False)
    assert "refused:" not in stream.getvalue()


def test_start_decoder_requires_the_refusal_key():
    from evolution_lab.cli.responses import validate
    payload = _full_status(None)
    start = {"start": 1, "scope_id": "s", "owner_generation": "gen1",
             "target_ref": {"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "gen1"},
             "process": payload["process"], "target_cleanup": payload["target_cleanup"],
             "failure": None, "settlement": "positive", "initiated_by": "human", "auto_resume_attempt": None}
    with pytest.raises(ValueError):
        validate(dict(payload, starts=[start]), "lab-operation-status/8")
    ok = validate(dict(payload, starts=[dict(start, refusal=None, core_terminal=None)]), "lab-operation-status/8")
    assert ok.starts[0].refusal is None


def test_refused_line_is_not_printed_from_a_start_alone():
    import io
    from evolution_lab.cli.output import write_value
    value = _status(None, refusal=None)
    value["starts"][0]["refusal"] = {"code": "corpus_inside_role_workspace", "path": None, "class": "hard",
                                     "instance_digest": None}
    stream = io.StringIO()
    write_value(stream, value, machine=False)
    assert "refused:" not in stream.getvalue()


def test_refused_line_strips_control_characters():
    import io
    from evolution_lab.cli.output import write_value
    stream = io.StringIO()
    write_value(stream, _status(None, refusal={"code": "bad\x1b[31mcode", "path": "lab/\x07corpus\nx",
                                               "class": "hard", "instance_digest": None}), machine=False)
    line = next(item for item in stream.getvalue().splitlines() if item.startswith("refused:"))
    assert line == "refused: bad?[31mcode at lab/?corpus?x (hard)"
    assert "\x1b" not in stream.getvalue() and "\x07" not in stream.getvalue()


def test_status_decoder_requires_the_top_level_refusal_key():
    from evolution_lab.cli.responses import validate
    payload = _full_status(None)
    assert "refusal" in payload
    del payload["refusal"]
    with pytest.raises(ValueError):
        validate(payload, "lab-operation-status/8")


def test_report_7_decoder_reads_lab_independent_next_to_its_state():
    """``lab-owner-report/15`` (both comparison layers): a family-15 report carries ``lab_independent`` with
    ``lab_independent_state``/``lab_independent_reason`` (ADR-0019 p.3); family 14 carries none of the three."""
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner.independent_view import independent_view
    from evolution_lab.owner.models import ReportResult
    from test_owner_independent_view import BLOCK

    def payload(**report):
        if any(key.startswith("lab_independent") for key in report):
            report.update(lab_checks=None, lab_checks_state="present", lab_checks_reason=None)
        return ReportResult("op1", "available", {"operation": "campaign", "lab_notices": [], "lab_notices_state": "present", **report}, None).to_payload()

    block = independent_view(BLOCK)
    good = [payload(), payload(lab_independent=block, lab_independent_state="present", lab_independent_reason=None),
            payload(lab_independent=None, lab_independent_state="unavailable", lab_independent_reason="unreadable")]
    for value in good:
        assert validate(value, "lab-owner-report/15").to_payload() == value
    bad = [payload(lab_independent=block),                                                  # no state
           payload(lab_independent=None),
           payload(lab_independent_state="present", lab_independent_reason=None),           # no block key
           payload(lab_independent=None, lab_independent_state="present", lab_independent_reason=None),
           payload(lab_independent=block, lab_independent_state="unavailable", lab_independent_reason="unreadable"),
           payload(lab_independent=None, lab_independent_state="unavailable", lab_independent_reason=None),
           payload(lab_independent=None, lab_independent_state="unavailable", lab_independent_reason="other"),
           payload(lab_independent=block, lab_independent_state="present", lab_independent_reason="unreadable"),
           payload(lab_independent=block, lab_independent_state="maybe", lab_independent_reason=None),
           payload(lab_independent=dict(block, evidence="maybe"), lab_independent_state="present",
                   lab_independent_reason=None)]
    for value in bad:
        with pytest.raises(ValueError):
            validate(value, "lab-owner-report/15")


def test_report_start_with_refusal_round_trips_through_the_cli_decoder():
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner.models import (
        CleanupFacts, ProcessFacts, Refusal, ReportResult, StartFacts, TargetRef)
    start = StartFacts(1, "scope1", "gen1", TargetRef("t1", "gen1"), ProcessFacts(True, 2, True, False),
                       CleanupFacts("confirmed", True), None, "not_positive",
                       Refusal("corpus_inside_role_workspace", None, "hard", None))
    payload = ReportResult("op1", "unavailable", None, "unavailable", starts=(start,)).to_payload()
    assert payload["schema"] == "lab-owner-report/15"
    assert payload["starts"][0]["refusal"]["code"] == "corpus_inside_role_workspace"
    assert validate(payload, "lab-owner-report/15").starts[0].refusal.code == "corpus_inside_role_workspace"
    assert validate(payload, "lab-owner-report/15").to_payload() == payload


REPORT_MD = "# Кампания\n\nRun: r1\n".encode("utf-8")
UNAVAILABLE = {"error": {"code": "report_unavailable", "stage": "transport",
                         "message": "The operation report is unavailable. Owner reason: io_error."}}


def test_report_markdown_prints_the_text_as_is():
    client = FakeClient(); client.body = REPORT_MD
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "report", "op1", "--markdown"], client)
    assert (code, stderr) == (0, "")
    assert stdout == REPORT_MD.decode("utf-8")
    assert client.calls == [("GET", "/v1/operations/op1/report.md", None, False, "text/markdown")]


@pytest.mark.parametrize("machine", [False, True])
def test_report_markdown_output_writes_a_new_private_file(tmp_path, machine):
    client = FakeClient(); client.body = REPORT_MD
    out = tmp_path / "report-op1.md"
    argv = ["--socket", "/tmp/lab.sock", *(["--json"] if machine else []), "report", "op1", "--markdown",
            "--output", str(out)]
    code, stdout, _ = invoke(argv, client)
    assert code == 0
    assert out.read_bytes() == REPORT_MD and (out.stat().st_mode & 0o777) == 0o600
    if machine:
        assert json.loads(stdout) == {"path": str(out), "bytes": len(REPORT_MD)}
    else:
        assert stdout == f"{out} {len(REPORT_MD)} bytes\n"


def test_report_markdown_output_never_overwrites(tmp_path):
    client = FakeClient(); client.body = REPORT_MD
    out = tmp_path / "report-op1.md"
    out.write_bytes(b"old")
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "report", "op1", "--markdown", "--output", str(out)], client)
    assert code == 1 and json.loads(stderr)["error"]["code"] == "client_error"
    assert out.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report-op1.md"]


@pytest.mark.parametrize("argv", [
    ["report", "op1", "--output", "/abs/report.md"],
    ["--json", "report", "op1", "--markdown"],
    ["report", "op1", "--markdown", "--output", "relative.md"],
])
def test_report_markdown_usage_errors_make_no_request(argv):
    client = FakeClient()
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", *argv], client)
    assert code == 2 and client.calls == [] and stderr.startswith("usage:")


def test_report_markdown_unavailable_is_the_service_error_and_exit_1():
    from evolution_lab.cli.client import ApiError
    client = FakeClient(); client.body = ApiError(UNAVAILABLE)
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "report", "op1", "--markdown"], client)
    assert (code, stdout) == (1, "") and json.loads(stderr) == UNAVAILABLE


def test_report_without_markdown_reads_the_json_report_as_before():
    client = FakeClient([{"schema": "lab-owner-report/15"}])
    code, _, stderr = invoke(["--socket", "/tmp/lab.sock", "--json", "report", "op1"], client)
    assert code == 0, stderr
    assert client.calls == [("GET", "/v1/operations/op1/report", None, False, "lab-owner-report/15")]


def _bytes_client(response):
    import httpx
    from evolution_lab.cli.client import ServiceClient
    return ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(lambda _: response))


def test_get_bytes_accepts_only_the_expected_type():
    import httpx
    from evolution_lab.cli.client import ClientError
    markdown = httpx.Response(200, content=REPORT_MD, headers={"content-type": "text/markdown; charset=utf-8"})
    assert _bytes_client(markdown).get_bytes("/v1/operations/o/report.md", "text/markdown") == REPORT_MD
    with pytest.raises(ClientError):
        _bytes_client(markdown).get_bytes("/v1/operations/o/exports/e/content", "application/zip")
    archive = httpx.Response(200, content=b"zip", headers={"content-type": "application/zip"})
    assert _bytes_client(archive).get_bytes("/v1/operations/o/exports/e/content", "application/zip") == b"zip"
    with pytest.raises(ClientError):
        _bytes_client(archive).get_bytes("/v1/operations/o/report.md", "text/markdown")


def test_get_bytes_reads_a_detailed_report_unavailable_envelope():
    import httpx
    from evolution_lab.cli.client import ApiError
    response = httpx.Response(409, json=UNAVAILABLE)
    with pytest.raises(ApiError) as error:
        _bytes_client(response).get_bytes("/v1/operations/o/report.md", "text/markdown")
    assert error.value.payload == UNAVAILABLE


def test_publish_bytes_writes_a_new_private_file_and_never_clobbers(tmp_path):
    from evolution_lab.cli.download import publish_bytes
    out = tmp_path / "r.md"
    assert publish_bytes(out, REPORT_MD) == {"path": str(out), "bytes": len(REPORT_MD)}
    assert out.read_bytes() == REPORT_MD and (out.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        publish_bytes(out, b"other")
    assert out.read_bytes() == REPORT_MD and list(tmp_path.glob(".r.md.*")) == []


@pytest.mark.parametrize("argv", [
    ["--json", "watch", "operation"], ["watch", "operation", "--interval", "0"],
    ["watch", "operation", "--interval", "1.5"], ["watch", "bad/id"],
])
def test_watch_usage_errors_make_no_request(argv):
    client = FakeClient()
    code, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", *argv], client)
    assert code == 2 and stdout == "" and stderr.startswith("usage:") and client.calls == []


def test_family_15_measurement_summary_has_no_detail_or_baseline_delta():
    from evolution_lab.cli import responses
    from evolution_lab.cli.output import _measurement_lines
    from evolution_lab.owner.measurements import measurement_rows
    from test_owner_measurements import BASE, CAND, CORE

    core = dict(CORE, family_version=15, measurements=[BASE, CAND])
    rows = measurement_rows(core, "campaign", {}, "score")
    value = _repair_list(rows)
    assert responses.validate(value, "lab-measurement-list/4") is value
    attempt = next(row for row in rows if row["role"] == "attempt")
    assert attempt["baseline"] is None and "samples" not in attempt
    line = next(line for line in _measurement_lines(value) if line.startswith(attempt["measurement_id"] + " "))
    assert "Δ=-" in line and "better/same/worse=" not in line


@pytest.mark.parametrize("field", ["comparison", "gate_comparison", "global_comparison"])
@pytest.mark.parametrize("reference", [3, True, [], {}])
def test_current_decoders_refuse_malformed_reference_in_every_comparison(field, reference):
    import copy
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner.models import ReportResult
    from test_owner_campaign_views import _view15
    view = _view15()
    row = view["independent"]["attempts"][0]
    row[field] = copy.deepcopy(row["comparison"])
    row[field]["reference_measurement_id"] = reference
    with pytest.raises(ValueError):
        validate(view, "lab-campaign-view/15")
    report = ReportResult("op1", "available", {"operation": "campaign", "lab_independent": view["independent"],
        "lab_independent_state": "present", "lab_independent_reason": None}, None).to_payload()
    with pytest.raises(ValueError):
        validate(report, "lab-owner-report/15")


@pytest.mark.parametrize("snapshot,old,new", [("family15", "lab-campaign-view/9", "lab-campaign-view/15"),
    ("family15", "lab-owner-report/6", "lab-owner-report/15")])
def test_previous_closed_forms_follow_existing_cli_rejection_path(snapshot, old, new):
    from panel_fixtures import FIXTURES, load_snapshot
    import httpx
    from evolution_lab.cli.client import ClientError, ServiceClient
    from evolution_lab.cli.responses import validate
    bodies = [r["body"] for r in load_snapshot(snapshot)["routes"].values()
              if isinstance(r.get("body"), dict)]
    if new == "lab-owner-report/15":
        from evolution_lab.owner.models import ReportResult
        value = ReportResult("op1", "unavailable", None, "unavailable").to_payload()
        raw = json.loads((FIXTURES / snapshot / "routes.json").read_text())
        captured_previous = next(row["body"] for row in raw.values()
                                 if row.get("body", {}).get("schema") == "lab-owner-report/7")
        rejected_bodies = (captured_previous, dict(value, schema=old))
    else:
        value = next(body for body in bodies if body.get("schema") == new)
        rejected_bodies = (dict(value, schema=old),)
    validate(value, new)
    for rejected in rejected_bodies:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=rejected))
        with ServiceClient(socket_path="/not-used", transport=transport) as client:
            with pytest.raises(ClientError, match="invalid service response"):
                client.request_json("GET", "/v1/test", expected_schema=new)
        with pytest.raises(ValueError, match="invalid public response"):
            validate(rejected, rejected["schema"])



def test_cli_status_prints_core_terminal():
    from evolution_lab.cli.output import write_value
    value = _status(None)
    value["core_terminal"] = {"owner": "campaign_journal", "terminal": "persistence_failed",
                              "code": "record_matches_secret", "hint": {"en": "Repair the journal.", "ru": "Исправьте журнал."}}
    stream = io.StringIO()
    write_value(stream, value, machine=False)
    assert "core terminal: persistence_failed (owner=campaign_journal, code=record_matches_secret)" in stream.getvalue()
    assert "hint: Repair the journal." in stream.getvalue()


@pytest.mark.parametrize("mutation", [
    lambda v: v.pop("checks"),
    lambda v: v.update(checks_state="unavailable"),
    lambda v: v.update(checks_reason="unreadable"),
    lambda v: v.update(checks={"modes": {}, "shadow_hits": {}, "extra": 1}),
    lambda v: v.update(checks={"modes": {"check": "enforce"}, "shadow_hits": {}}),
    lambda v: v.update(checks={"modes": {}, "shadow_hits": {"check": True}}),
    lambda v: v.update(checks={"modes": {}, "shadow_hits": {"check": 0}}),
])
def test_b13_checks_decoder_closes_shape_and_state(mutation):
    from evolution_lab.cli.responses import validate
    from test_owner_campaign_views import _view15
    view = _view15()
    mutation(view)
    with pytest.raises(ValueError):
        validate(view, "lab-campaign-view/15")


def test_b13_report_requires_checks_triplet_and_rejects_old_majors():
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner.models import ReportResult
    from test_owner_campaign_views import _view15
    view = _view15()
    data = {"operation": "campaign", "lab_notices": [], "lab_notices_state": "present", "lab_independent": view["independent"],
            "lab_independent_state": "present", "lab_independent_reason": None,
            "lab_checks": None, "lab_checks_state": "present", "lab_checks_reason": None}
    payload = ReportResult("op1", "available", data, None).to_payload()
    assert validate(payload, "lab-owner-report/15").to_payload() == payload
    for key in ("lab_checks", "lab_checks_state", "lab_checks_reason"):
        bad = {**payload, "report": {k: v for k, v in data.items() if k != key}}
        with pytest.raises(ValueError):
            validate(bad, "lab-owner-report/15")
    for value, old in ((view, "lab-campaign-view/10"), (view, "lab-campaign-view/14"), (payload, "lab-owner-report/8"), (payload, "lab-owner-report/14")):
        with pytest.raises(ValueError):
            validate(dict(value, schema=old), old)
