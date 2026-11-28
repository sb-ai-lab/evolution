"""The CLI's reads are the command layer's requests, with unchanged bytes (ADR-0070).

Goldens below were captured from the dispatch before it called the layer.
"""
from __future__ import annotations

import dataclasses
import io
import sys

import pytest

from evolution_lab.cli.client import ApiError
from evolution_lab.cli.output import write_json
from evolution_lab.commands import REGISTRY
from tests.test_commands_coverage import CLI_READS
from tests.test_service_cli import DIGEST, FakeClient, _status, invoke

S = ["--socket", "/tmp/lab.sock"]

#: (CLI variant, argv, layer kwargs): one or more cases per ``CLI_READS`` variant.
CASES: list[tuple[tuple[str, ...], list[str], dict]] = [
    (("bundle", "show"), [*S, "--json", "bundle", "show", DIGEST], {"digest": DIGEST}),
    (("bundle", "measurements"), [*S, "--json", "bundle", "measurements", DIGEST], {"digest": DIGEST}),
    (("prepared", "show"), [*S, "--json", "prepared", "show", DIGEST], {"digest": DIGEST}),
    (("approvals",), [*S, "--json", "approvals", "--operation", "op"], {"operation_id": "op"}),
    (("approvals",), [*S, "--json", "approvals", "--policy", DIGEST], {"policy_digest": DIGEST}),
    (("exec-policy", "list"), [*S, "--json", "exec-policy", "list"], {}),
    (("exec-policy", "show"), [*S, "--json", "exec-policy", "show", DIGEST], {"digest": DIGEST}),
    (("routing",), [*S, "--json", "routing", "op"], {"operation": "op"}),
    (("agent", "list"), [*S, "--json", "agent", "list"], {}),
    (("agent", "list"), [*S, "--json", "agent", "list", "--ready"], {"ready": True}),
    (("agent", "list"), [*S, "--json", "agent", "list", "--bundle", DIGEST, "--ready"],
     {"bundle": DIGEST, "ready": True}),
    (("agent", "list"), [*S, "--json", "agent", "list", "--bundle", "a b/c"], {"bundle": "a b/c"}),
    (("agent", "status"), [*S, "--json", "agent", "status", "t", "--generation", "g"],
     {"target": "t", "owner_generation": "g"}),
    (("job", "status"), [*S, "--json", "job", "status", "task", "--generation", "g"],
     {"task": "task", "owner_generation": "g"}),
    (("list",), [*S, "--json", "list"], {}),
    (("list", "--summary"), [*S, "--json", "list", "--summary"], {}),
    (("status",), [*S, "--json", "status", "op"], {"operation": "op"}),
    (("report",), [*S, "--json", "report", "op"], {"operation": "op"}),
    (("report", "--markdown"), [*S, "report", "op", "--markdown"], {"operation": "op"}),
    (("view",), [*S, "--json", "view", "op"], {"operation": "op"}),
    (("suspensions",), [*S, "--json", "suspensions", "op"], {"operation": "op"}),
    (("diff",), [*S, "--json", "diff", "op"], {"operation": "op"}),
    (("diff",), [*S, "--json", "diff", "op", "--from", "seed", "--to", "a b"],
     {"operation": "op", "tree_from": "seed", "tree_to": "a b"}),
    (("sample",), [*S, "--json", "sample", "op", "m@e2", "case@1", "00"],
     {"operation": "op", "measurement": "m@e2", "case": "case@1", "sample": "00"}),
    (("sample", "--disclose-private"), [*S, "--json", "sample", "op", "m", "case", "00", "--disclose-private"],
     {"operation": "op", "measurement": "m", "case": "case", "sample": "00"}),
    (("attempt",), [*S, "--json", "attempt", "op", "r01-a01"], {"operation": "op", "attempt": "r01-a01"}),
    (("attempt", "--disclose-private"), [*S, "--json", "attempt", "op", "r01-a01", "--disclose-private"],
     {"operation": "op", "attempt": "r01-a01"}),
    (("reflection",), [*S, "--json", "reflection", "op", "r01-x01"], {"operation": "op", "reflection": "r01-x01"}),
    (("reflection", "--disclose-private"), [*S, "--json", "reflection", "op", "r01-x01", "--disclose-private"],
     {"operation": "op", "reflection": "r01-x01"}),
    (("measurements",), [*S, "--json", "measurements", "op"], {"operation": "op"}),
    (("measurements", "MEASUREMENT"), [*S, "--json", "measurements", "op", "m@e2"],
     {"operation": "op", "measurement": "m@e2"}),
    (("health",), [*S, "--json", "health"], {}),
    (("guides", "list"), ["guides", "list", "--json"], {}),
    (("guides", "requirements"), ["guides", "requirements"], {}),
    (("scenarios", "list"), ["scenarios", "list", "--json"], {}),
    (("scenarios", "show"), ["scenarios", "show", "connect", "--json"], {"scenario": "connect"}),
    (("scenarios", "schema"), ["scenarios", "schema", "connect", "spec-answers/1"],
     {"scenario": "connect", "schema": "spec-answers/1"}),
    (("project", "list"), [*S, "--json", "project", "list", "--archived"], {"archived": True}),
    (("project", "show"), [*S, "--json", "project", "show", "p q"], {"project": "p q"}),
    (("project", "status"), [*S, "--json", "project", "status", "p", "--implementation", "i", "--scenario", "c"],
     {"project": "p", "implementation": "i", "scenario": "c"}),
    (("project", "facts"), [*S, "--json", "project", "facts", "p"], {"project": "p"}),
    (("project", "approvals"), [*S, "--json", "project", "approvals", "p"], {"project_id": "p"}),
    (("project", "confirmations"), [*S, "--json", "project", "confirmations", "p", "--revoked"],
     {"project": "p", "revoked": True}),
    (("project", "tree"), [*S, "--json", "project", "tree", "p", "--implementation", "i", "--path", "a/b", "--text"],
     {"project": "p", "implementation": "i", "path": "a/b", "text": True}),
    (("project", "operations"), [*S, "--json", "project", "operations", "p"], {"project": "p"}),
    (("project", "operations", "--summary"), [*S, "--json", "project", "operations", "p", "--summary"],
     {"project": "p"}),
]

#: Old names: the same layer command as their public name.
ALIAS_CASES = [
    ("operation_campaign", [*S, "--json", "campaign", "op"], {"operation": "op"}),
    ("operation_routing", [*S, "--json", "operation", "routing", "op"], {"operation": "op"}),
    ("targets_list", [*S, "--json", "target", "list"], {}),
    ("task_status", [*S, "--json", "task", "status", "task", "--generation", "g"],
     {"task": "task", "owner_generation": "g"}),
    ("execution_policies_list", [*S, "--json", "policy", "list"], {}),
]

#: The value every service read returns in these tests; non-ASCII to pin ``write_json``'s escaping.
CANNED = {"schema": "canned", "note": "é"}
MARKDOWN = "# report é\n"


def _json(value) -> str:
    stream = io.StringIO()
    write_json(stream, value)
    return stream.getvalue()


def _ids(cases):
    return [" ".join(case[1][2:] if case[1][:1] == ["--socket"] else case[1]) for case in cases]


def _run_service_read(name, argv, kwargs):
    request = REGISTRY[name].build(**kwargs)
    client = FakeClient([dict(CANNED)])
    if request.kind == "text":
        client.body = MARKDOWN.encode()
    code, out, err = invoke(argv, client)
    return request, client, code, out, err


# These reads require an authenticated on-disk campaign rather than canned service responses.
PREPARED_CASES = [("preflight",)]


def test_cases_cover_every_cli_read():
    assert {variant for variant, _, _ in CASES} | set(PREPARED_CASES) == set(CLI_READS)


@pytest.mark.parametrize("variant", PREPARED_CASES)
def test_prepared_cli_read_uses_the_layer(tmp_path, monkeypatch, variant):
    from test_context_preflight_inputs import prepared_chat

    state, digest, _ = prepared_chat(tmp_path, monkeypatch)
    command = REGISTRY[CLI_READS[variant]]
    expected = command.run(state, digest)
    calls = []

    def record_call(selected_state, selected_digest, **kwargs):
        calls.append((selected_state, selected_digest))
        return expected

    monkeypatch.setattr(sys.modules[command.run.__module__], command.run.__name__, record_call)
    code, out, err = invoke(["--json", *variant, str(state / "prepared" / digest)], None)
    assert (code, err) == (0, "")
    assert calls == [(state, digest)]
    assert out == _json(expected.as_dict())


@pytest.mark.parametrize(("variant", "argv", "kwargs"), CASES, ids=_ids(CASES))
def test_cli_read_variant_requests_come_from_the_layer(variant, argv, kwargs):
    name = CLI_READS[variant]
    command = REGISTRY[name]
    if not command.needs_service:
        value = command.run(**kwargs)
        code, out, err = invoke(argv, None)
        assert (code, err) == (0, "")
        assert out == (value if isinstance(value, str) else _json(value))
        return
    request, client, code, out, err = _run_service_read(name, argv, kwargs)
    assert (code, err) == (0, ""), err
    if request.kind == "text":
        assert client.calls == [("GET", request.path, None, False, request.content_type)]
        assert out == MARKDOWN
    else:
        assert client.calls == [(request.method, request.path, None, False, request.schema)]
        assert client.keys == [None]
        assert out == _json(CANNED)


@pytest.mark.parametrize(("name", "argv", "kwargs"), ALIAS_CASES, ids=_ids(ALIAS_CASES))
def test_old_names_send_the_same_layer_request(name, argv, kwargs):
    request, client, code, out, _ = _run_service_read(name, argv, kwargs)
    assert code == 0
    assert client.calls == [(request.method, request.path, None, False, request.schema)]
    assert out == _json(CANNED)


@pytest.mark.parametrize(("variant", "argv", "kwargs"), CASES, ids=_ids(CASES))
def test_cli_read_goes_through_the_layer_builder(monkeypatch, variant, argv, kwargs):
    """A sentinel in place of the layer function reaches the client or stdout: the CLI calls the layer."""
    command = REGISTRY[CLI_READS[variant]]
    function = command.build or command.run
    module = sys.modules[function.__module__]
    original = getattr(module, function.__name__)
    if not command.needs_service:
        value = original(**kwargs)
        sentinel = "sentinel\n" if isinstance(value, str) else (
            [{"name": "sentinel", "description": "d", "path": "p"}] if isinstance(value, list) else {"sentinel": 1})
        monkeypatch.setattr(module, function.__name__, lambda *a, **k: sentinel)
        code, out, _ = invoke(argv, None)
        assert code == 0
        assert out == (sentinel if isinstance(sentinel, str) else _json(sentinel))
        return
    monkeypatch.setattr(module, function.__name__,
                        lambda *a, **k: dataclasses.replace(original(*a, **k), path="/sentinel"))
    client = FakeClient([dict(CANNED)])
    invoke(argv, client)
    assert [call[1] for call in client.calls] == ["/sentinel"]


# --- text `lab status`: follow-up reads keep their order and bytes -------------------------------------------

FAILURE = {"schema": "lab-operation-failure/1", "operation_id": "op-1", "start": 1, "scope_id": "scope",
           "exception": {"class": "pkg.Boom", "code": "c1"}, "log": "scopes/scope/stderr.log"}
REFUSAL = {"schema": "lab-operation-refusal/1", "operation_id": "op-1", "start": 2,
           "findings": [{"code": "run_identity_changed", "path": "policy", "class": "authorizable",
                         "instance_digest": DIGEST, "effects": ["rebaseline"], "effect_texts": ["fresh baseline"],
                         "differs": ["policy changed"]}]}
INPUTS = {"schema": "lab-operation-inputs/1", "operation_id": "op-1",
          "starts": [{"start": 1, "prepared_digest": "1" * 64, "bundle_digest": "2" * 64},
                     {"start": 2, "prepared_digest": "3" * 64, "bundle_digest": "2" * 64}]}


def _followed_status():
    value = _status(None)
    second = {**value["starts"][0], "start": 2}
    value["starts"].append(second)
    value["failure"] = {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    value["refusal"] = {"code": "run_identity_changed", "path": "policy"}
    value["target_ref"] = {"schema": "lab-target-ref/1", "id": "target-1", "owner_generation": "generation-1"}
    return value


STATUS_FOLLOW_UP_STDOUT = (
    "lab-operation-status/8 op-1 finished process=exit:1\n"
    "refused: run_identity_changed at policy (None)\n"
    "worker exception: pkg.Boom (code c1)\n"
    "traceback: scopes/scope/stderr.log (in the service's state directory)\n"
    "finding: run_identity_changed at policy (authorizable)\n"
    f"instance_digest: {DIGEST}\n"
    "differs: policy changed\n"
    "effects: rebaseline (default)\n"
    "fresh baseline\n"
    "accept: lab resume op-1 --target target-1 --generation generation-1 "
    f"--accept run_identity_changed:{DIGEST} --effect rebaseline --reason REASON\n"
    f"start 1: prepared {'1' * 64} bundle {'2' * 64}\n"
    f"start 2: prepared {'3' * 64} bundle {'2' * 64}\n"
)  # captured from the dispatch before the layer


def test_cli_status_text_follow_ups_use_layer_paths(monkeypatch):
    from evolution_lab import _ownership
    monkeypatch.delenv(_ownership.SEAM_NAME, raising=False)
    client = FakeClient([_followed_status(), FAILURE, REFUSAL, INPUTS])
    code, out, err = invoke([*S, "status", "op-1"], client)
    assert (code, err) == (0, "")
    layer = [REGISTRY[name].build(operation="op-1")
             for name in ("operation_status", "operation_failure", "operation_refusal", "operation_inputs")]
    assert client.calls == [("GET", r.path, None, False, r.schema) for r in layer]
    assert out == STATUS_FOLLOW_UP_STDOUT


SERVICE_ERROR_STDERR = '{"error":{"code":"not_found","stage":"service","message":"No such operation."}}\n'


@pytest.mark.parametrize("argv", [[*S, "status", "op"], [*S, "--json", "status", "op"], [*S, "health"]])
def test_cli_service_error_envelope_unchanged(argv):
    error = ApiError({"error": {"code": "not_found", "stage": "service", "message": "No such operation."}})
    code, out, err = invoke(argv, FakeClient([error]))
    assert (code, out, err) == (1, "", SERVICE_ERROR_STDERR)


NO_SCENARIO = '{"error":{"code":"not_found","stage":"guides","message":"The scenario is not in the catalogue."}}\n'
NO_SCHEMA = ('{"error":{"code":"not_found","stage":"guides",'
             '"message":"The scenario or its schema is not in the catalogue."}}\n')


@pytest.mark.parametrize(("argv", "stderr"), [
    (["scenarios", "show", "nope"], NO_SCENARIO),
    (["scenarios", "show", "nope", "--json"], NO_SCENARIO),
    (["scenarios", "schema", "connect", "nope/1"], NO_SCHEMA),
    (["scenarios", "schema", "nope", "spec-answers/1"], NO_SCHEMA),
])
def test_offline_not_found_envelopes_unchanged(argv, stderr):
    code, out, err = invoke(argv, None)
    assert (code, out, err) == (1, "", stderr)


def _old_text(argv) -> str:
    """The text rendering of ``lab guides list`` and ``lab scenarios list|show`` as it was before the layer."""
    from evolution_lab import guides
    if argv[0] == "guides":
        return "".join(f"{g.name}\t{g.path}\t{g.description}\n" for g in guides.list_guides())
    catalogue = guides.scenarios()
    if argv[1] == "list":
        return "".join(f"{d['id']}\t{d['version']}\t{d['title']}\t{catalogue.skill_path(d['id'])}\n"
                       for d in catalogue.descriptors)
    descriptor = catalogue.descriptor(argv[2])
    lines = [f"{descriptor['id']}\t{descriptor['version']}\t{descriptor['title']}\t{catalogue.skill_path(argv[2])}"]
    for step in descriptor["steps"]:
        parts = [f"step {step['id']} {step['kind']}", "human_only" if step["human_only"] else "",
                 "optional" if step.get("optional", False) else "",
                 "requires=" + ",".join(step["requires"]) if step["requires"] else ""]
        lines.append(" ".join(part for part in parts if part))
    return "".join(line + "\n" for line in lines)


@pytest.mark.parametrize("argv", [["guides", "list"], ["scenarios", "list"],
                                  *[["scenarios", "show", s] for s in ("connect", "create-skill", "evolve", "restructure")]])
def test_offline_text_output_unchanged(argv):
    code, out, err = invoke(argv, None)
    assert (code, err) == (0, "")
    assert out == _old_text(argv)


def _sentinel(monkeypatch, name):
    """Replace the layer function ``name`` by one whose request path is ``/sentinel/<name>``."""
    function = REGISTRY[name].build
    module = sys.modules[function.__module__]
    original = getattr(module, function.__name__)
    monkeypatch.setattr(module, function.__name__,
                        lambda *a, **k: dataclasses.replace(original(*a, **k), path=f"/sentinel/{name}"))


def test_cli_status_follow_ups_go_through_the_layer(monkeypatch):
    for name in ("operation_failure", "operation_refusal", "operation_inputs"):
        _sentinel(monkeypatch, name)
    client = FakeClient([_followed_status(), FAILURE, REFUSAL, INPUTS])
    invoke([*S, "status", "op-1"], client)
    assert [call[1] for call in client.calls] == [
        "/v1/operations/op-1", "/sentinel/operation_failure", "/sentinel/operation_refusal",
        "/sentinel/operation_inputs"]


def test_slot_holders_and_ready_target_list_targets_through_the_layer(monkeypatch):
    from evolution_lab.cli.parser import _ready_target, _slot_holders
    _sentinel(monkeypatch, "targets_list")
    listing = {"schema": "lab-owner-target-list/1", "targets": []}
    client = FakeClient([listing, listing])
    _slot_holders(client)
    _ready_target(client, {"resume": {"target": {"bundle_digest": DIGEST, "excluded": []}}})
    assert [call[1] for call in client.calls] == ["/sentinel/targets_list", "/sentinel/targets_list"]
    unpatched = FakeClient([listing, listing])
    monkeypatch.undo()
    _slot_holders(unpatched)
    _ready_target(unpatched, {"resume": {"target": {"bundle_digest": DIGEST, "excluded": []}}})
    assert unpatched.calls == [("GET", "/v1/targets?state=ready", None, False, "lab-owner-target-list/1"),
                               ("GET", "/v1/targets", None, False, "lab-owner-target-list/1")]


# --- an unencodable --implementation is refused first, as before the layer -----------------------------------

#: ``"i\udcff"`` is how Python passes a non-UTF-8 argv byte; captured from the dispatch before the layer.
UNENCODABLE_STDERR = ('{"error":{"code":"client_error","stage":"transport",'
                      '"message":"The local service client failed."}}\n')


def _reading_request_files(monkeypatch):
    from evolution_lab.cli import parser
    reads, original = [], parser._read_json
    monkeypatch.setattr(parser, "_read_json", lambda path, stdin: (reads.append(path), original(path, stdin))[1])
    return reads


@pytest.mark.parametrize("argv", [
    [*S, "project", "prepare", "p", "--request", "REQUEST", "--implementation", "i\udcff"],
    [*S, "--json", "project", "prepare", "p", "--request", "REQUEST", "--implementation", "i\udcff"],
    [*S, "project", "bundle", "p", "--implementation", "i\udcff"],
    [*S, "project", "status", "p", "--implementation", "i\udcff"],
    [*S, "--json", "project", "status", "p", "--implementation", "i\udcff"],
], ids=["prepare", "prepare --json", "bundle", "status", "status --json"])
def test_unencodable_implementation_is_refused_before_request_file_and_service(tmp_path, monkeypatch, argv):
    request = tmp_path / "request.json"
    request.write_text('{"kind": "evaluation"}', encoding="utf-8")
    reads = _reading_request_files(monkeypatch)
    client = FakeClient()
    code, out, err = invoke([str(request) if item == "REQUEST" else item for item in argv], client)
    assert (code, out, err) == (1, "", UNENCODABLE_STDERR)
    assert reads == [] and client.calls == []


def test_encodable_implementation_still_reads_the_request_file(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text('{"kind": "evaluation"}', encoding="utf-8")
    reads = _reading_request_files(monkeypatch)
    client = FakeClient([{"schema": "canned"}])
    invoke([*S, "--no-request-id", "project", "prepare", "p", "--request", str(request), "--implementation", "i"],
           client)
    assert reads == [str(request)]
    assert [call[:2] for call in client.calls] == [("POST", "/v1/projects/p/prepared-executions")]
