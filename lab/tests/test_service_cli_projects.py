"""Task 10: ``lab serve --home`` and the ``lab project`` command group.

Adapted from the task brief to ``tests/test_service_cli.py``'s real ``FakeClient``/``invoke``:
responses are a positional list (popped in call order, not keyed by (method, path)), calls
are recorded as ``client.calls`` tuples ``(method, path, payload, mutation, expected_schema)``
and keys as ``client.keys``, and a service error is delivered by putting an ``ApiError``
instance in the response list (raised by ``request_json``), not a dict plus a ``status=``.
"""
import io
import json

import pytest

from test_service_cli import DIGEST, HEALTH, FakeClient, invoke

from evolution_lab.cli import main as _main


@pytest.fixture
def tmp_path(tmp_path_factory):
    """Keep home-discovered socket paths within UNIX limits in the dev56 worktree."""
    return tmp_path_factory.mktemp("p")


def invoke_with_stdin(argv, client, text):
    """Like ``test_service_cli.invoke``, but with a scripted stdin (for the ``accept``
    ``[y/N]`` prompt); the shared ``invoke`` always uses ``sys.stdin``."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = _main(argv, stdout=stdout, stderr=stderr, stdin=io.StringIO(text), client_factory=lambda **_: client)
    return code, stdout.getvalue(), stderr.getvalue()

# A materials revision digest is "sha256:<64 hex>" (spec §3.4), unlike a Bundle digest.
REVISION = "sha256:" + "b" * 64

PROJECT = {"schema": "lab-project/2", "id": "acq", "name": "Acq", "state": "ready", "failure": None,
           "created_at": "t", "archived": None, "record_version": 1,
           "working_copy": {"path": "/w", "created_by_lab": False, "git": False, "source": None},
           "materials_root": "lab", "exclude": [], "implementations": []}


def test_create_from_path_sends_closed_body():
    client = FakeClient([PROJECT])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--json", "--no-request-id", "project", "create", "acq",
                             "--name", "Acq", "--path", "/w", "--root", "lab",
                             "--impl", "agent=agent,package=bundle-request.json"], client)
    assert code == 0, err
    (method, path, payload, mutation, expected_schema), = client.calls
    assert method == "POST" and path == "/v1/projects" and expected_schema == "lab-project/2"
    assert payload == {"schema": "lab-service-project-create/1", "id": "acq", "name": "Acq", "materials_root": "lab",
                        "implementations": [{"id": "agent", "place": "agent", "package_entry": "bundle-request.json"}],
                        "source": {"kind": "path", "path": "/w"}, "exclude": []}
    assert mutation is True
    assert json.loads(out)["schema"] == "lab-project/2"


def test_facts_is_a_read():
    facts = {"schema": "lab-project-facts/5", "project": "acq", "implementation": "agent",
             "pair": {"lab": "l", "core": "c"}, "revision": {"schema": "lab-materials-revision/1", "digest": REVISION,
             "places": [], "files": 0, "git": None}, "facts": []}
    client = FakeClient([facts])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--json", "project", "facts", "acq"], client)
    assert code == 0, err
    assert json.loads(out)["schema"] == "lab-project-facts/5"
    method, path, payload, mutation, expected_schema = client.calls[0]
    assert method == "GET" and path == "/v1/projects/acq/facts" and payload is None
    assert mutation is False


def _real_facts_payload():
    """Built from the real models, as the service emits it: a revision digest with its
    ``sha256:`` prefix, a form-only ``package.valid`` and a stale fact with a rename."""
    from evolution_lab.projects.inventory import Changes, inventory_digest
    from evolution_lab.projects.models import Fact, StaleReason
    from evolution_lab.projects.revision import Revision

    revision = Revision(inventory_digest(()), ({"role": "materials_root", "path": "lab", "digest": inventory_digest(())},),
                        0, None, ())
    stale = StaleReason(("inputs_changed",), Changes((("lab/a.md", "lab/b.md"),), (), (), ()))
    return {"schema": "lab-project-facts/5", "project": "acq", "implementation": "agent",
            "pair": {"lab": "l", "core": "c"}, "revision": revision.to_payload(),
            "facts": [Fact("package.valid", {"implementation": "agent"}, {"scope": "form"},
                           {"kind": "package_entry", "errors": []}, None).to_payload(),
                      Fact("check.passed", {"implementation": "agent"}, True, {"kind": "task", "id": "t"},
                           stale).to_payload()]}


def test_facts_validator_accepts_the_payload_the_service_emits():
    # FakeClient bypasses responses.validate; the real client runs it on every response.
    # Found by the installed Task 11 run: the validator decoded the revision digest as a
    # bare Bundle digest, so every real ``lab project facts`` ended in client_error.
    from evolution_lab.cli.responses import validate

    value = _real_facts_payload()
    assert validate(value, "lab-project-facts/5") == value


def test_facts_validator_refuses_a_malformed_revision_digest():
    import pytest
    from evolution_lab.cli.responses import validate

    for digest in (DIGEST, "sha256:" + "z" * 64, "sha256:", None):
        value = _real_facts_payload()
        value["revision"]["digest"] = digest
        with pytest.raises(ValueError):
            validate(value, "lab-project-facts/5")


def test_facts_with_implementation_query():
    facts = {"schema": "lab-project-facts/5", "project": "acq", "implementation": "agent",
             "pair": {"lab": "l", "core": "c"}, "revision": {"schema": "lab-materials-revision/1", "digest": REVISION,
             "places": [], "files": 0, "git": None}, "facts": []}
    client = FakeClient([facts])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--json", "project", "facts", "acq",
                             "--implementation", "agent"], client)
    assert code == 0, err
    assert client.calls[0][1] == "/v1/projects/acq/facts?implementation=agent"


def test_project_error_is_decoded():
    from evolution_lab.cli.client import ApiError

    envelope = {"error": {"code": "home_busy", "stage": "project",
                          "message": "Another Lab process writes this Lab home, or this handle cannot write it."}}
    client = FakeClient([ApiError(envelope)])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "list"], client)
    assert code != 0 and "home_busy" in err


def test_serve_accepts_home(monkeypatch):
    seen = {}
    monkeypatch.setattr("evolution_lab.service.server.run_service", lambda **kw: seen.update(kw) or 0)
    code, out, err = invoke(["serve", "--socket", "/tmp/s.sock", "--state", "/tmp/state", "--home", "/tmp/home"], None)
    assert code == 0, err
    assert seen["home_directory"].as_posix() == "/tmp/home"


def test_serve_without_home_passes_none(monkeypatch):
    seen = {}
    monkeypatch.setattr("evolution_lab.service.server.run_service", lambda **kw: seen.update(kw) or 0)
    invoke(["serve", "--socket", "/tmp/s.sock", "--state", "/tmp/state"], None)
    assert seen["home_directory"] is None


def test_start_sends_target_ref_built_from_health():
    operation_start_result = {
        "schema": "lab-project-start-result/5", "link": None,
        "operation": {
            "schema": "lab-operation-status/8", "id": "op", "owner_generation": "gen-1", "prepared_digest": DIGEST,
            "kind": "campaign", "target_ref": {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "gen-1"},
            "control_state": "reserved",
            "process": {"launched": False, "exit_code": None, "cleanup_complete": False, "forced": False},
            "target_cleanup": {"state": "not_needed", "complete": True}, "result": None, "failure": None,
            "results_state": "not_created", "refusal": None, "executor": {"schema": "lab-executor-identity/1", "distribution": "d"},
            "retained_summary": None, "starts": [], "resume": None,
        },
    }
    client = FakeClient([HEALTH, HEALTH, operation_start_result])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--json", "project", "start", "acq",
                             "--prepared", DIGEST, "--target", "target"], client)
    assert code == 0, err
    calls = client.calls
    assert calls[0][:2] == ("GET", "/v1/health")  # target_ref's owner_generation
    assert calls[1][:2] == ("GET", "/v1/health")  # fresh mutation key's owner_generation
    method, path, payload, mutation, expected_schema = calls[2]
    assert method == "POST" and path == "/v1/projects/acq/operations"
    assert payload["target_ref"] == {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "gen-1"}
    assert mutation is True
    assert expected_schema == 'lab-project-start-result/5'


def test_accept_without_yes_previews_then_applies_on_y():
    reason = "materials match"
    preview = {"schema": "lab-project-acceptance/1", "id": None, "project": "acq", "implementation": None,
               "facts": [], "across_pairs": False, "reason": reason, "channel": "cli", "author": "cli",
               "created_at": "t", "revoked": None, "dry_run": True}
    applied = {**preview, "id": "acc-1", "dry_run": False}
    client = FakeClient([HEALTH, preview, HEALTH, applied])
    code, out, err = invoke_with_stdin(["--socket", "/tmp/s.sock", "--json", "project", "accept", "acq",
                                        "--reason", reason], client, "y\n")
    assert code == 0, err
    posts = [call for call in client.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert posts[0][2]["dry_run"] is True and posts[1][2]["dry_run"] is False
    assert client.keys[1] != client.keys[3] and client.keys[1] is not None and client.keys[3] is not None
    lines = out.splitlines()
    assert json.loads(lines[0])["dry_run"] is True
    assert json.loads(lines[1])["dry_run"] is False


def test_accept_without_yes_and_without_confirmation_does_not_apply():
    preview = {"schema": "lab-project-acceptance/1", "id": None, "project": "acq", "implementation": None,
               "facts": [], "across_pairs": False, "reason": "r", "channel": "cli", "author": "cli",
               "created_at": "t", "revoked": None, "dry_run": True}
    client = FakeClient([HEALTH, preview])
    code, out, err = invoke_with_stdin(["--socket", "/tmp/s.sock", "project", "accept", "acq", "--reason", "r"],
                                       client, "n\n")
    assert code == 0, err
    assert len([call for call in client.calls if call[0] == "POST"]) == 1


def test_accept_without_yes_and_explicit_request_id_is_a_usage_error():
    # _dispatch_project's two-call accept flow (dry_run preview, then the real write)
    # needs two distinct mutation keys; an explicit --request-id would be replayed
    # verbatim on both, and the write's body differs (dry_run flips) from the preview's,
    # so the server would refuse the write with request_conflict (R18) *after* a
    # successful preview. Refused up front instead, before any service call.
    client = FakeClient([])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--request-id", "k" * 32, "--generation", "g",
                             "project", "accept", "acq", "--reason", "r"], client)
    assert code == 2
    assert err.startswith("usage:") and "--yes" in err
    assert client.calls == []


def test_accept_with_yes_and_explicit_request_id_sends_one_call_with_that_key():
    applied = {"schema": "lab-project-acceptance/1", "id": "acc-1", "project": "acq", "implementation": None,
               "facts": [], "across_pairs": False, "reason": "r", "channel": "cli", "author": "cli",
               "created_at": "t", "revoked": None, "dry_run": False}
    client = FakeClient([applied])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--request-id", "k" * 32, "--generation", "g",
                             "project", "accept", "acq", "--reason", "r", "--yes"], client)
    assert code == 0, err
    assert len(client.calls) == 1
    assert client.calls[0][2]["dry_run"] is False
    assert client.keys == [("k" * 32, "g")]


# --- Final-review fix wave ---

def test_accept_preview_in_human_mode_lists_what_will_be_carried():
    """I3: without --json the preview names every fact it would carry — source, codes, change
    counts and the first renamed/modified paths — before ``apply? [y/N]``."""
    changes = {"renamed": [["lab/a.md", "lab/b.md"]], "modified": ["lab/c.md", "lab/d.md", "lab/e.md"],
               "added": ["lab/f.md"], "removed": [],
               "counts": {"renamed": 1, "modified": 3, "added": 1, "removed": 0}}
    preview = {"schema": "lab-project-acceptance/1", "id": None, "project": "acq", "implementation": "agent",
               "facts": [{"id": "check.passed", "from": {"kind": "task", "id": "k1"},
                          "to": {"inputs_digest": "sha256:" + "c" * 64, "place": ["/w", "lab", "agent"],
                                 "pair": {"lab": "l", "core": "c"}},
                          "codes": ["inputs_changed"], "changes": changes},
                         {"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"},
                          "to": {"attests": "sha256:" + "d" * 64, "linked": []}, "codes": ["digest_changed"],
                          "changes": None}],
               "across_pairs": False, "reason": "r", "channel": "cli", "author": "cli",
               "created_at": "t", "revoked": None, "dry_run": True}
    client = FakeClient([HEALTH, preview])
    code, out, err = invoke_with_stdin(["--socket", "/tmp/s.sock", "project", "accept", "acq", "--reason", "r"],
                                       client, "n\n")
    assert code == 0, err
    lines = out.splitlines()
    assert ("fact check.passed from=task:k1 codes=inputs_changed renamed=1 modified=3 added=1 removed=0 "
            "paths=lab/a.md->lab/b.md,lab/c.md,lab/d.md") in lines
    assert "fact spec.confirmed from=confirmation:c-1 codes=digest_changed changes=none" in lines
    assert err.endswith("apply? [y/N] ")
    assert len([call for call in client.calls if call[0] == "POST"]) == 1


def test_facts_in_human_mode_print_one_line_per_fact():
    """M3: id, holds yes/no, stale codes if any and a carried marker."""
    from evolution_lab.projects.models import Fact, StaleReason

    value = _real_facts_payload()
    value["facts"].append(Fact("spec.confirmed", None, True, {"kind": "confirmation", "id": "c-1"}, None,
                               ({"acceptance": "a-1", "reason": "r", "across_pairs": False},)).to_payload())
    value["facts"].append(Fact("baseline.measured", {"implementation": "agent"}, True, {"kind": "operation", "id": "op"},
                               StaleReason(("other_pair", "inputs_changed"))).to_payload())
    client = FakeClient([value])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "facts", "acq"], client)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[2:] == ["fact package.valid holds=yes",
                         "fact check.passed holds=no stale=inputs_changed",
                         "fact spec.confirmed holds=yes carried",
                         "fact baseline.measured holds=no stale=other_pair,inputs_changed"]


def test_fact_line_names_the_source_reason():
    """E8 in text mode: a fact whose `source.reason` is set names it on its line (spec dev46 §3)."""
    from evolution_lab.cli.output import _fact_line
    item = {"id": "package.present", "holds": False,
            "source": {"kind": "package_entry", "entry": "agent.py", "reason": "missing"}}
    assert _fact_line(item) == "fact package.present holds=no reason=missing"
    assert _fact_line(dict(item, holds=True, source={"kind": "package_entry", "entry": "agent.py", "reason": None})) \
        == "fact package.present holds=yes"


def test_local_paths_are_sent_absolute(tmp_path, monkeypatch):
    """M1: the service runs elsewhere, so --path, --working-copy and a local --clone directory
    are resolved against the caller's directory; a remote --clone URL is sent as given."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wc").mkdir()
    (tmp_path / "team").mkdir()
    base = ["--socket", "/tmp/s.sock", "--json", "--no-request-id", "project"]
    client = FakeClient([PROJECT] * 4)
    for argv in (["create", "acq", "--name", "Acq", "--root", "lab", "--path", "wc"],
                 ["create", "acq", "--name", "Acq", "--root", "lab", "--clone", "team"],
                 ["create", "acq", "--name", "Acq", "--root", "lab", "--clone", "git@example.invalid:team/repo.git"],
                 ["set", "acq", "--working-copy", "wc"]):
        code, out, err = invoke(base + argv, client)
        assert code == 0, err
    payloads = [call[2] for call in client.calls]
    assert payloads[0]["source"] == {"kind": "path", "path": str(tmp_path / "wc")}
    assert payloads[1]["source"] == {"kind": "clone", "repo": str(tmp_path / "team"), "rev": None}
    assert payloads[2]["source"] == {"kind": "clone", "repo": "git@example.invalid:team/repo.git", "rev": None}
    assert payloads[3]["mutation"] == {"op": "set_working_copy", "path": str(tmp_path / "wc")}


def test_local_paths_are_normalised_and_local_clone_sources_recognised_by_syntax(tmp_path, monkeypatch):
    """Codex cli-local-path-canonicalization (R-d): ``..`` is normalised away, and a ``--clone``
    source without a URL scheme that is not ``host:path`` is local whatever it is on disk (a
    bundle file, a path not yet there); remote forms are sent untouched."""
    (tmp_path / "wc").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "repo.bundle").write_bytes(b"# v2 git bundle\n")
    monkeypatch.chdir(tmp_path / "sub")
    base = ["--socket", "/tmp/s.sock", "--json", "--no-request-id", "project"]
    create = base + ["create", "acq", "--name", "Acq", "--root", "lab"]
    remote = ("git@example.invalid:team/repo.git", "https://example.invalid/team/repo.git",
              "ssh://git@example.invalid/team/repo.git", "file:///srv/team.git")
    client = FakeClient([PROJECT] * (5 + len(remote)))
    for argv in ([*create, "--path", "../wc"], [*create, "--clone", "repo.bundle"], [*create, "--clone", "../team"],
                 [*create, "--clone", "./odd:name"], *([*create, "--clone", url] for url in remote),
                 base + ["set", "acq", "--working-copy", "../wc"]):
        code, out, err = invoke(argv, client)
        assert code == 0, err
    sources = [call[2].get("source") for call in client.calls]
    assert sources[0] == {"kind": "path", "path": str(tmp_path / "wc")}
    assert [source["repo"] for source in sources[1:4]] == [str(tmp_path / "sub" / "repo.bundle"),
                                                           str(tmp_path / "team"), str(tmp_path / "sub" / "odd:name")]
    assert [source["repo"] for source in sources[4:-1]] == list(remote)
    assert client.calls[-1][2]["mutation"] == {"op": "set_working_copy", "path": str(tmp_path / "wc")}


def _step(step_id, title, state, **fields):
    row = {"id": step_id, "title": title, "state": state, "optional": False, "human_only": False, "missing": [],
           "blocked_reason": None, "invalid": [], "stale_reason": None, "carried_over": [], "sources": [],
           "done_at": None}
    row.update(fields)
    return row


STATUS = {"schema": "lab-project-status/6", "project": "acq", "implementation": "agent",
          "pair": {"lab": "l", "core": "c"}, "catalogue": {"version": 2, "digest": "d" * 64}, "facts": [],
          "scenarios": [
              {"id": "connect", "version": 1, "repeatable": False, "state": "available", "missing": [],
               "steps": [_step("spec", "Write the spec", "ready"),
                         _step("check", "Check the connection", "stale",
                               stale_reason={"codes": ["inputs_changed"], "message": "m", "changes": None}),
                         _step("baseline", "Measure a short baseline", "blocked", human_only=True,
                               missing=["check", "conformance"])]},
              {"id": "evolve", "version": 2, "repeatable": True, "state": "available", "missing": [],
               "next_cycle": {"steps": [_step("plan", "Plan the campaign", "done", sources=[{"artifact": "@manifest"}])]},
               "cycles": [{"operation_id": "op-1", "order": 1, "pair": {"lab": "l", "core": "c"}, "current": True,
                           "detail": "available", "control_state": "finished",
                           "resume": {"state": "ready", "reason": "r", "target": None}, "outcome": None,
                           "complete": False,
                           "steps": [_step("run", "Run the campaign", "blocked", human_only=True,
                                           blocked_reason={"code": "campaign_suspended"})]}]}]}


def test_status_is_a_read_with_both_query_parameters():
    client = FakeClient([STATUS])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "status", "acq", "--implementation", "agent",
                             "--scenario", "evolve", "--json"], client)
    assert code == 0, err
    method, path, payload, mutation, expected_schema = client.calls[0]
    assert (method, path, payload, mutation) == ("GET", "/v1/projects/acq/status?implementation=agent&scenario=evolve", None, False)
    assert expected_schema == "lab-project-status/6"
    assert json.loads(out) == STATUS


def test_status_without_options_has_no_query():
    client = FakeClient([STATUS])
    code, _, err = invoke(["--socket", "/tmp/s.sock", "--json", "project", "status", "acq"], client)
    assert code == 0, err
    assert client.calls[0][1] == "/v1/projects/acq/status"


def test_status_in_human_mode_prints_scenarios_steps_and_cycles():
    client = FakeClient([STATUS])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "status", "acq"], client)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "lab-project-status/6"
    assert "project=acq implementation=agent catalogue=2" in lines[1]
    assert "scenario connect state=available" in lines
    assert "  step spec ready" in lines
    assert "  step check stale stale=inputs_changed" in lines
    assert "  step baseline blocked human_only missing=check,conformance" in lines
    assert "scenario evolve state=available" in lines
    assert "  next plan done" in lines
    assert "  cycle op-1 order=1 current control=finished resume=ready" in lines
    assert "    step run blocked human_only blocked=campaign_suspended" in lines


def test_status_cycle_line_marks_a_fresh_target_requirement_and_completion():
    import copy
    target = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
              "excluded": [{"schema": "lab-target-ref/1", "id": "T1", "owner_generation": "G1"}]}
    status = copy.deepcopy(STATUS)
    status["scenarios"][1]["cycles"][0]["resume"] = {"state": "ready", "reason": None, "target": target}
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "status", "acq"], FakeClient([status]))
    assert code == 0, err
    assert "  cycle op-1 order=1 current control=finished resume=ready target=fresh" in out.splitlines()
    status["scenarios"][1]["cycles"][0].update(resume=None, complete=True)
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "status", "acq"], FakeClient([status]))
    assert "  cycle op-1 order=1 current complete control=finished" in out.splitlines()


def test_status_validator_accepts_the_service_form_and_refuses_an_unknown_key():
    import pytest
    from evolution_lab.cli.responses import validate
    validate(STATUS, "lab-project-status/6")
    with pytest.raises(ValueError):
        validate({**STATUS, "extra": 1}, "lab-project-status/6")


def _status_with(step: dict) -> dict:
    import copy
    status = copy.deepcopy(STATUS)
    status["scenarios"][1]["cycles"][0]["steps"].append(step)
    return status


RESULT_DELETED = _step("result", "Collect the result", "done", optional=True,
                       sources=[{"export": "e" * 64, "results_state": "deleted"}])
RESULT_BLOCKED = {**RESULT_DELETED, "state": "blocked", "sources": [], "blocked_reason": {"code": "results_deleted"}}
CONFIRMED = _step("spec-confirm", "Confirm the spec", "done", done_at="2026-09-30T10:00:05Z",
                  sources=[{"fact": "spec.confirmed", "confirmations": [
                      {"id": "c-1", "author": "tester", "channel": "cli", "created_at": "2026-09-30T10:00:01Z"},
                      {"id": "c-2", "author": "lead", "channel": "auto-evolve", "created_at": "2026-09-30T10:00:05Z"}]}])
DIG = "sha256:" + "a" * 64
INVALID = _step("map", "Map the behaviour", "blocked", blocked_reason={"code": "artifact_invalid"},
                invalid=[{"artifact": "map.json", "digest": DIG, "path": "/behaviors/0",
                          "message": "'source' is a required property"},
                         {"artifact": "map.json", "digest": DIG, "path": "/behaviors/0/id", "message": "'' is too short"}])
UNUSABLE = _step("map", "Map the behaviour", "blocked",
                 blocked_reason={"code": "schema_unusable", "schema": "behavior-map/1"})
MAPPED = _step("map", "Map the behaviour", "done",
               sources=[{"artifact": "restructure/port-a/behavior-map.json", "digest": DIG}])
MANIFEST = _step("plan", "Plan", "done", sources=[{"artifact": "@manifest"}])
UNREAD = _step("map", "Map the behaviour", "blocked", blocked_reason={"code": "artifact_invalid"},
               invalid=[{"artifact": "map.json", "digest": None, "path": "", "message": "the file cannot be read"}])


def test_status_text_shows_deleted_export_blocked_reason_invalid_paths_and_confirmations():
    status = _status_with(RESULT_DELETED)
    status["scenarios"][1]["cycles"][0]["steps"] += [RESULT_BLOCKED, CONFIRMED, INVALID, UNUSABLE]
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "status", "acq"], FakeClient([status]))
    assert code == 0, err
    lines = out.splitlines()
    assert "    step result done optional export=deleted" in lines
    assert "    step result blocked optional blocked=results_deleted" in lines
    assert ("    step spec-confirm done confirmed=tester@cli:2026-09-30T10:00:01Z,"
            "lead@auto-evolve:2026-09-30T10:00:05Z done_at=2026-09-30T10:00:05Z") in lines
    assert "    step map blocked blocked=artifact_invalid invalid=map.json/behaviors/0(+1)" in lines
    assert "    step map blocked blocked=schema_unusable:behavior-map/1" in lines


def test_status_validator_checks_the_step_fields():
    import pytest
    from evolution_lab.cli.responses import validate
    for good in (CONFIRMED, RESULT_BLOCKED, INVALID, UNUSABLE, MAPPED, MANIFEST, UNREAD,
                 {**MAPPED, "sources": [{"artifact": "SPEC.md", "digest": None}]},
                 {**INVALID, "invalid": [dict(INVALID["invalid"][0], path=f"/{i}") for i in range(5)]
                  + [dict(INVALID["invalid"][0], artifact="b.json", path=f"/{i}") for i in range(5)]},
                 {**RESULT_BLOCKED, "blocked_reason": {"code": "campaign_state_unknown"}}):
        validate(_status_with(good), "lab-project-status/6")
    for bad in ({**RESULT_BLOCKED, "blocked_reason": {"code": "other"}},
                {**RESULT_BLOCKED, "blocked_reason": {"code": "results_deleted", "extra": 1}},
                {**RESULT_BLOCKED, "blocked_reason": {"code": "results_deleted", "schema": "x/1"}},
                {**UNUSABLE, "blocked_reason": {"code": "schema_unusable"}},
                {**INVALID, "invalid": [{"path": "/x"}]},
                {**INVALID, "invalid": [dict(INVALID["invalid"][0], path=f"/{i}") for i in range(6)]},  # 6 for one file
                {**INVALID, "invalid": [{"path": "/x", "message": "m"}]},                                # /5 record
                {**INVALID, "invalid": [dict(INVALID["invalid"][0], digest=None)]},                     # null with a pointer
                {**INVALID, "invalid": [INVALID["invalid"][0], dict(INVALID["invalid"][1], digest="sha256:" + "b" * 64)]},
                {**MAPPED, "sources": [{"artifact": "SPEC.md"}]},                                         # no digest
                {**MAPPED, "sources": [{"artifact": "SPEC.md", "digest": "a" * 64}]},                     # bare hex
                {**MANIFEST, "sources": [{"artifact": "@manifest", "digest": DIG}]},
                {**RESULT_BLOCKED, "invalid": [{"path": "/x", "message": "m"}]},
                {**RESULT_BLOCKED, "optional": "yes"},
                {k: v for k, v in RESULT_BLOCKED.items() if k != "human_only"},
                {k: v for k, v in RESULT_BLOCKED.items() if k != "blocked_reason"},
                {**RESULT_DELETED, "sources": [{"export": "e" * 64, "results_state": "gone"}]},
                {**CONFIRMED, "sources": [{"fact": "spec.confirmed", "confirmations": [{"id": "c-1"}]}]}):
        with pytest.raises(ValueError):
            validate(_status_with(bad), "lab-project-status/6")
    import copy
    for change in ({"order": "1"}, {"complete": None}, {"order": -1}):
        status = copy.deepcopy(STATUS)
        status["scenarios"][1]["cycles"][0].update(change)
        with pytest.raises(ValueError):
            validate(status, "lab-project-status/6")
    with pytest.raises(ValueError):
        validate({**STATUS, "schema": "lab-project-status/5"}, "lab-project-status/6")


SUMMARY_ROW = {"id": "op-1", "kind": "campaign", "entrypoint": None, "control_state": "finished",
               "results_state": "present", "resume": None, "progress": None, "progress_state": "unavailable",
               "times": None, "times_state": "unavailable", "times_source": None, "times_reason": "unreadable",
               "outcome": {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v1"}}
PROJECT_SUMMARY = {"schema": "lab-project-operation-list/2", "project": "acq", "operations": [
    {"link": None, "label": "Acq · evolve · цикл 1", "operation_state": "present", "operation": SUMMARY_ROW,
     "outcome": SUMMARY_ROW["outcome"]},
    {"link": None, "label": "Acq · evolve · цикл 2", "operation_state": "other_pair", "operation": None,
     "outcome": None}]}


def _project_summary():
    import copy
    value = copy.deepcopy(PROJECT_SUMMARY)
    link = {"schema": "lab-project-link/1", "kind": "operation", "project": "acq", "implementation": "agent",
            "pair": {"lab": "l", "core": "c"}, "state_root": "/s", "created_at": "t", "binding": {},
            "bundle_digest": "b" * 64, "prepared_digest": "p" * 64, "object_id": "op-1",
            "operation_kind": "campaign", "last_seen": None}
    value["operations"][0]["link"] = link
    value["operations"][1]["link"] = {**link, "object_id": "op-old", "pair": {"lab": "o", "core": "c"}}
    return value


def test_project_operations_summary_reads_the_summary_view():
    value = _project_summary()
    client = FakeClient([value])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--json", "project", "operations", "acq", "--implementation",
                             "agent", "--summary"], client)
    assert code == 0, err
    method, path, payload, mutation, expected_schema = client.calls[0]
    assert (method, path, expected_schema) == ("GET", "/v1/projects/acq/operations?implementation=agent&view=summary",
                                               "lab-project-operation-list/2")
    assert json.loads(out) == value
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "operations", "acq", "--summary"], FakeClient([value]))
    assert code == 0, err
    lines = out.splitlines()
    assert "Acq · evolve · цикл 1 present - completed" in lines
    assert "Acq · evolve · цикл 2 other_pair - -" in lines
    plain = FakeClient([{"schema": "lab-project-operation-list/1", "links": []}])
    assert invoke(["--socket", "/tmp/s.sock", "--json", "project", "operations", "acq"], plain)[0] == 0
    assert (plain.calls[0][1], plain.calls[0][4]) == ("/v1/projects/acq/operations", "lab-project-operation-list/1")


def test_project_summary_validator_is_closed():
    from evolution_lab.cli.responses import validate
    value = _project_summary()
    validate(value, "lab-project-operation-list/2")
    for broken in ({**value["operations"][0], "extra": 1}, {**value["operations"][1], "operation_state": "gone"},
                   {**value["operations"][0], "operation": {**SUMMARY_ROW, "result": None}}):
        with pytest.raises(ValueError):
            validate({**value, "operations": [broken]}, "lab-project-operation-list/2")


def _recording(client, seen):
    def factory(**kwargs):
        seen.update(kwargs)
        return client
    return factory


def test_client_finds_socket_in_home_flag(tmp_path):
    from evolution_lab.cli import main
    seen, client = {}, FakeClient([PROJECT])
    code = main(["--home", str(tmp_path), "--json", "project", "show", "acq"],
                stdout=io.StringIO(), stderr=io.StringIO(), client_factory=_recording(client, seen))
    assert code == 0 and seen["socket_path"] == str(tmp_path / "service.sock")


def test_client_finds_socket_in_lab_home(tmp_path, monkeypatch):
    from evolution_lab.cli import main
    monkeypatch.setenv("LAB_HOME", str(tmp_path))
    seen, client = {}, FakeClient([PROJECT])
    assert main(["--json", "project", "show", "acq"], stdout=io.StringIO(), stderr=io.StringIO(),
                client_factory=_recording(client, seen)) == 0
    assert seen["socket_path"] == str(tmp_path / "service.sock")


def test_client_socket_too_long_hints_socket(tmp_path):
    from evolution_lab.cli import main
    stderr = io.StringIO()
    code = main(["--home", "/" + "h" * 100, "project", "show", "acq"], stdout=io.StringIO(), stderr=stderr,
                client_factory=lambda **_: pytest.fail("no client for an unusable socket"))
    assert code == 2 and "--socket" in stderr.getvalue()


WORKSPACE = {"schema": "lab-project-workspace/1", "path": None}


@pytest.mark.parametrize("options, level", [([], "ask"), (["--autonomy", "yolo"], "yolo")])
def test_open_prints_path(tmp_path, options, level):
    client = FakeClient([dict(WORKSPACE, path=str(tmp_path))])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "open", "acq", *options], client)
    assert code == 0, err
    (method, path, payload, mutation, schema), = client.calls
    assert (method, path, mutation, schema) == ("POST", "/v1/projects/acq/workspace", True, "lab-project-workspace/1")
    assert payload == {"schema": "lab-service-project-workspace-request/1", "autonomy": level}
    assert out.strip() == str(tmp_path)


def test_workspace_response_passes_the_real_client_validator():
    # FakeClient bypasses responses.validate; without a validator the real client turned
    # every successful `project open` into acceptance_unknown (found on the installed wheel).
    from evolution_lab.cli.responses import validate
    value = dict(WORKSPACE, path="/h/workspaces/acq")
    assert validate(value, "lab-project-workspace/1") == value
    for bad in ({**value, "extra": 1}, {**value, "path": None},
                {**value, "schema": "lab-project-workspace/2"}):
        with pytest.raises(ValueError):
            validate(bad, "lab-project-workspace/1")


def test_open_print_writes_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Project acq\n")
    client = FakeClient([dict(WORKSPACE, path=str(tmp_path))])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "open", "acq", "--print"], client)
    assert code == 0 and out == "# Project acq\n"


def test_demo_sends_closed_body():
    client = FakeClient([PROJECT])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "--json", "project", "demo"], client)
    assert code == 0, err
    (method, path, payload, mutation, schema), = client.calls
    assert (method, path, schema, mutation) == ("POST", "/v1/projects/demo", "lab-project/2", True)
    assert payload == {"schema": "lab-service-project-demo-request/1", "id": None}
    client = FakeClient([PROJECT])
    invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "demo", "acq"], client)
    assert client.calls[0][2]["id"] == "acq"


def test_project_2_validator_checks_the_port():
    import pytest
    from evolution_lab.cli.responses import validate
    implementation = {"id": "agent-port", "name": "agent-port", "place": "/h/ports/acq/agent-port",
                      "origin": {"kind": "ported_from", "implementation": "agent"}, "package_entry": None,
                      "archived": None, "port": {"template": "/t", "ref": "HEAD", "commit": "c" * 40, "created_at": "t"}}
    value = {**PROJECT, "implementations": [implementation]}
    validate(value, "lab-project/2")
    validate({"schema": "lab-project-list/2", "projects": [value]}, "lab-project-list/2")
    for bad in ({**value, "implementations": [{**implementation, "port": {"template": "/t"}}]},
                {**value, "implementations": [{k: v for k, v in implementation.items() if k != "port"}]},
                {**value, "schema": "lab-project/1"}):
        with pytest.raises(ValueError):
            validate(bad, "lab-project/2")


def test_project_show_prints_one_line_per_implementation_with_its_port():
    implementation = {"id": "agent-port", "name": "agent-port", "place": "/h/ports/acq/agent-port",
                      "origin": {"kind": "ported_from", "implementation": "agent"}, "package_entry": None,
                      "archived": None, "port": {"template": "/t", "ref": "HEAD", "commit": "c" * 40, "created_at": "t"}}
    client = FakeClient([{**PROJECT, "implementations": [implementation]}])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "project", "show", "acq"], client)
    assert code == 0, err
    assert ("  impl agent-port place=/h/ports/acq/agent-port origin=ported_from:agent "
            f"port=/t@{'c' * 12}") in out.splitlines()


@pytest.mark.parametrize("value, expected", [
    ("/t/sylph", ("/t/sylph", None)),
    ("/t/sylph@v1", ("/t/sylph", "v1")),
    ("/t/sylph@feature/x", ("/t/sylph", "feature/x")),
    ("/t/a@b/repo@HEAD", ("/t/a@b/repo", "HEAD")),
    ("/t/sylph@", ("/t/sylph@", None)),
    ("https://u:tok@git.example.com/sylph.git", ("https://u:tok@git.example.com/sylph.git", None)),
    ("https://u:tok@git.example.com/sylph.git@main", ("https://u:tok@git.example.com/sylph.git", "main")),
    ("git@git.example.com:team/sylph.git", ("git@git.example.com:team/sylph.git", None)),
    ("git@git.example.com:team/sylph.git@v2", ("git@git.example.com:team/sylph.git", "v2")),
    ("file:///t/sylph@abc123", ("file:///t/sylph", "abc123")),
])
def test_split_template(value, expected):
    from evolution_lab.cli.project import split_template
    assert split_template(value) == expected


def test_port_sends_a_closed_body_with_absolute_local_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient([PROJECT])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "--json", "project", "port", "acq",
                             "--from", "agent", "--template", "sylph@v1", "--into", "ports/p", "--id", "p1"], client)
    assert code == 0, err
    (method, path, payload, mutation, schema), = client.calls
    assert (method, path, schema, mutation) == ("POST", "/v1/projects/acq/ports", "lab-project/2", True)
    assert payload == {"schema": "lab-service-project-port-request/1", "from": "agent",
                       "template": str(tmp_path / "sylph"), "ref": "v1", "id": "p1",
                       "into": str(tmp_path / "ports" / "p")}


def test_port_sends_a_url_as_given_and_leaves_the_defaults_to_the_service():
    client = FakeClient([PROJECT])
    url = "https://u:tok@git.example.com/sylph.git"
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "port", "acq", "--from", "agent",
                             "--template", url], client)
    assert code == 0, err
    assert client.calls[0][2] == {"schema": "lab-service-project-port-request/1", "from": "agent", "template": url,
                                  "ref": None, "id": None, "into": None}


def test_port_destination_exists_envelope_with_the_path_is_a_service_error_not_a_transport_error():
    """The real client validates the error envelope: the one detailed message must pass it (controller ruling C1)."""
    import httpx
    from evolution_lab.cli.client import ServiceClient

    path = "/h/ports/acq/p1"
    envelope = {"error": {"code": "port_destination_exists", "stage": "project",
                          "message": f"The port destination exists and is not empty. {path}"}}
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=HEALTH) if request.url.path == "/v1/health" else httpx.Response(409, json=envelope)

    stdout, stderr = io.StringIO(), io.StringIO()
    code = _main(["--socket", "/tmp/s.sock", "project", "port", "acq", "--from", "agent", "--template", "/t/sylph"],
                 stdout=stdout, stderr=stderr, stdin=io.StringIO(""),
                 client_factory=lambda **kw: ServiceClient(socket_path=kw["socket_path"],
                                                           transport=httpx.MockTransport(handler)))
    assert seen[-1] == "/v1/projects/acq/ports"
    assert code != 0
    assert "port_destination_exists" in stderr.getvalue() and path in stderr.getvalue()
    assert "transport_error" not in stderr.getvalue() + stdout.getvalue()


def test_a_forged_port_destination_message_is_still_invalid():
    from evolution_lab.cli.responses import _failure
    base = "The port destination exists and is not empty."
    assert _failure({"code": "port_destination_exists", "stage": "project", "message": base + " /p"}) is None
    with pytest.raises(ValueError):
        _failure({"code": "port_destination_exists", "stage": "project", "message": "other /p"})
    with pytest.raises(ValueError):
        _failure({"code": "home_busy", "stage": "project", "message": "Another Lab process writes this Lab home. /p"})


A, B = "a" * 40, "b" * 40
SYNC_CHECK = {"schema": "lab-project-sync-check/1", "project": "acq", "base_revision": B,
              "upstream": {"sha": A, "ref": "origin/develop"}, "mode": "merge",
              "commits": [{"sha": A, "author": "T", "date": "2026-10-08T00:00:00+00:00", "subject": "new"}],
              "commits_truncated": False,
              "files": [{"path": "x.py", "status": "A"}, {"path": "n.py", "status": "R", "old_path": "o.py"}],
              "files_truncated": False, "dirty": []}
SYNC_RESULT = {"schema": "lab-project-sync-result/1", "mode": "fast_forward", "merged": A, "head": A, "project": PROJECT}


def test_project_sync_check_request():
    client = FakeClient([SYNC_CHECK])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq", "--rev", "develop"], client)
    assert code == 0, err
    (method, path, payload, mutation, schema), = client.calls
    assert (method, path, schema) == ("POST", "/v1/projects/acq/sync/check", "lab-project-sync-check/1")
    assert payload == {"schema": "lab-service-project-sync-check-request/1", "rev": "develop"}
    assert "merge, 1 commits, 2 files" in out
    assert f"{A[:12]} new" in out and "R o.py -> n.py" in out and "A x.py" in out
    assert out.rstrip().endswith("next: lab project sync acq --apply " + A)


def test_up_to_date_but_unrecorded_upstream_still_hints_apply():
    unrecorded = {**SYNC_CHECK, "mode": "up_to_date", "commits": [], "files": []}
    assert unrecorded["upstream"]["sha"] != unrecorded["base_revision"]
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq"], FakeClient([unrecorded]))
    assert code == 0, err
    assert out.rstrip().endswith("next: lab project sync acq --apply " + A)
    recorded = {**unrecorded, "base_revision": A}
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq"], FakeClient([recorded]))
    assert "next:" not in out


def test_project_sync_dirty_has_no_apply_hint():
    dirty = {**SYNC_CHECK, "dirty": [{"path": "w.py"}, {"path": None, "state": "merge_in_progress"}],
             "commits_truncated": True}
    client = FakeClient([dirty])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq"], client)
    assert code == 0, err
    assert client.calls[0][2]["rev"] is None
    assert ", dirty" in out and "--apply" not in out and "(truncated)" in out and "merge_in_progress" in out


def test_project_sync_apply_request():
    client = FakeClient([SYNC_RESULT])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq", "--apply", A], client)
    assert code == 0, err
    (method, path, payload, mutation, schema), = client.calls
    assert (method, path, schema, mutation) == ("POST", "/v1/projects/acq/sync", "lab-project-sync-result/1", True)
    assert payload == {"schema": "lab-service-project-sync-request/1", "upstream": A}
    assert f"fast_forward {A[:12]} → head {A[:12]}" in out


def test_project_sync_rev_and_apply_are_exclusive():
    client = FakeClient([])
    code, _, err = invoke(["--socket", "/tmp/s.sock", "project", "sync", "acq", "--rev", "x", "--apply", A], client)
    assert code != 0 and not client.calls


def test_sync_responses_validate_closed():
    from evolution_lab.cli.responses import validate
    assert validate(SYNC_CHECK, "lab-project-sync-check/1") == SYNC_CHECK
    assert validate(SYNC_RESULT, "lab-project-sync-result/1") == SYNC_RESULT
    for bad, schema in ((SYNC_CHECK, "lab-project-sync-check/1"), (SYNC_RESULT, "lab-project-sync-result/1")):
        with pytest.raises(ValueError):
            validate({**bad, "extra": 1}, schema)
    with pytest.raises(ValueError):
        validate({**SYNC_CHECK, "mode": "rebase"}, "lab-project-sync-check/1")
    with pytest.raises(ValueError):
        validate({**SYNC_RESULT, "project": {"schema": "lab-project/2"}}, "lab-project-sync-result/1")


def test_sync_check_upstream_ref_is_null_for_a_commit():
    from evolution_lab.cli.responses import validate
    commit_form = {**SYNC_CHECK, "upstream": {"sha": A, "ref": None}}
    assert validate(commit_form, "lab-project-sync-check/1") == commit_form
    client = FakeClient([commit_form])
    code, out, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "sync", "acq", "--rev", A], client)
    assert code == 0, err
    assert out.rstrip().endswith("next: lab project sync acq --apply " + A)


def test_sync_check_upstream_ref_empty_string_is_invalid():
    from evolution_lab.cli.responses import validate
    with pytest.raises(ValueError):
        validate({**SYNC_CHECK, "upstream": {"sha": A, "ref": ""}}, "lab-project-sync-check/1")


@pytest.mark.parametrize("code,detail", [("merge_conflict", "agent/a.py, agent/b.py"),
                                         ("working_copy_dirty", "agent/a.py"),
                                         ("merge_failed", "The working copy is left in an unfinished state; a person must repair it."),
                                         ("revision_not_found", "The source has no default branch; pass --rev.")])
def test_sync_failures_with_detail_pass_through_the_real_service_envelope(code, detail):
    """Codex r1 #3: the envelope exactly as ``service/errors.py`` builds it for a detailed sync failure."""
    import httpx
    from evolution_lab.cli.client import ServiceClient
    from evolution_lab.projects.errors import ProjectError
    from evolution_lab.service.errors import error_response

    response = error_response(ProjectError(code, detail))
    status, body = response["status"], response["body"]
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=HEALTH) if request.url.path == "/v1/health" else httpx.Response(status, json=body)

    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code = _main(["--socket", "/tmp/s.sock", "project", "sync", "acq"],
                      stdout=stdout, stderr=stderr, stdin=io.StringIO(""),
                      client_factory=lambda **kw: ServiceClient(socket_path=kw["socket_path"],
                                                                transport=httpx.MockTransport(handler)))
    assert exit_code not in (0, 125), (exit_code, stderr.getvalue())
    assert code in stderr.getvalue() and detail in stderr.getvalue()
    assert "transport_error" not in stderr.getvalue() + stdout.getvalue()


def test_project_prepare_gets_2_only_when_the_request_names_a_split():
    """User decision 2026-10-09: an old request file (no `split`) still goes as project prepare /1."""
    from types import SimpleNamespace
    from evolution_lab.cli.project import request
    args = SimpleNamespace(project_command="prepare", project="acq", request="r.json", implementation=None)
    body = {"kind": "campaign", "bundle_digest": DIGEST, "stop_grace_seconds": None, "cleanup_wait_seconds": None}
    for value, schema in ((body, "lab-service-project-prepare-request/1"),
                          ({**body, "split": "test"}, "lab-service-project-prepare-request/2")):
        prepared = request(args, lambda _: value, lambda _: {}, lambda _: {})
        assert prepared.path == "/v1/projects/acq/prepared-executions" and prepared.payload["schema"] == schema


def _accept_preview():
    return {"schema": "lab-project-acceptance/1", "id": None, "project": "acq", "implementation": "agent",
            "facts": [{"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"},
                       "to": {"attests": "sha256:" + "a" * 64}, "codes": ["digest_changed"], "changes": None}],
            "across_pairs": False, "reason": "typo", "channel": "cli", "author": "cli", "created_at": "t",
            "revoked": None, "dry_run": True}


def test_cli_accept_sends_preview_expected_on_y():
    preview = _accept_preview()
    client = FakeClient([preview, {**preview, "id": "a-1", "dry_run": False}])
    code, _, err = invoke_with_stdin(["--socket", "/tmp/s.sock", "--no-request-id", "project", "accept", "acq",
                                     "--reason", "typo"], client, "y\n")
    assert code == 0, err
    assert client.calls[0][2]["schema"] == "lab-service-accept-request/2"
    assert client.calls[0][2]["expected"] is None
    assert client.calls[1][2]["expected"] == [{key: row[key] for key in ("id", "from", "to")}
                                             for row in preview["facts"]]


def test_cli_accept_change_after_preview_prints_hint():
    from evolution_lab.cli.client import ApiError
    from evolution_lab.projects.errors import ProjectError
    from evolution_lab.service.errors import error_response
    envelope = error_response(ProjectError("digest_mismatch"))["body"]
    client = FakeClient([_accept_preview(), ApiError(envelope)])
    code, _, err = invoke_with_stdin(["--socket", "/tmp/s.sock", "--no-request-id", "project", "accept", "acq",
                                     "--reason", "typo"], client, "y\n")
    assert code == 1 and "digest_mismatch" in err
    assert "hint: the facts changed since the preview; run lab project accept again\n" in err
    assert len(client.calls) == 2


def test_cli_accept_yes_sends_expected_null():
    client = FakeClient([{**_accept_preview(), "id": "a-1", "dry_run": False}])
    code, _, err = invoke(["--socket", "/tmp/s.sock", "--no-request-id", "project", "accept", "acq",
                           "--reason", "typo", "--yes"], client)
    assert code == 0, err
    assert len(client.calls) == 1
    assert client.calls[0][2]["schema"] == "lab-service-accept-request/2"
    assert client.calls[0][2]["expected"] is None
    assert client.calls[0][2]["dry_run"] is False


@pytest.mark.parametrize("setting", ["sample_retries", "min_coverage", "concurrency",
                                     "scorer_concurrency", "max_consecutive_unmeasured"])
def test_project_prepare_new_settings_use_complete_version_3(setting):
    from types import SimpleNamespace
    from evolution_lab.cli.project import request
    args = SimpleNamespace(project_command="prepare", project="acq", request="r.json", implementation="agent")
    body = {"kind": "evaluation", "bundle_digest": DIGEST, setting: 1}
    prepared = request(args, lambda _: body, lambda _: {}, lambda _: {})
    assert prepared.payload == {"schema": "lab-service-project-prepare-request/3", "implementation": "agent",
                       "kind": "evaluation", "bundle_digest": DIGEST, "split": None,
                       "sample_retries": None, "min_coverage": None, "concurrency": None,
                       "scorer_concurrency": None, "max_consecutive_unmeasured": None, setting: 1}
