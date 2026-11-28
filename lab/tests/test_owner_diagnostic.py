import json

import pytest

from evolution_lab.owner.diagnostic import read_campaign_refusal

IDS = {"operation_id": "a" * 32, "scope_id": "b" * 32}
REFUSAL = {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None}


def test_running_exception_writes_redacted_traceback_to_stderr(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import evolution.tree.evolve as core
    from evolution.tree.campaign import CampaignInvariantViolated
    from evolution_lab.owner import _campaign_worker as worker
    secret = "q7Z9x"
    config = {**IDS, "manifest_path": "unused", "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "owner_generation": "c" * 32, "run_id": "neutral-run", "run_directory": str(tmp_path),
              "settlement_path": str(tmp_path / "settlement.json")}
    admitted = SimpleNamespace(workflow=None, target=SimpleNamespace(timeouts=SimpleNamespace(
        connect_seconds=.1, read_seconds=.1)))
    monkeypatch.setattr(core, "admit_manifest", lambda *a: admitted)
    monkeypatch.setattr(core, "redactions_for", lambda *a: ())
    monkeypatch.setattr(core, "short_forwarded_names", lambda *a: ())
    async def broken(*args, **kwargs):
        raise CampaignInvariantViolated("instance_attested carries " + secret)
    monkeypatch.setattr(core, "evolve", broken)
    assert worker.run_worker(config, {"FORWARDED": secret}, lambda: False) == 1
    output = capsys.readouterr().err
    assert "Traceback" in output
    assert "CampaignInvariantViolated" in output
    assert "instance_attested" in output
    assert secret not in output


def _write(tmp_path, **over):
    path = tmp_path / "diagnostic.json"
    path.write_text(json.dumps({"schema": "lab-campaign-diagnostic/3", **IDS, "failure": None, "refusal": REFUSAL, "core_terminal": None, **over}))
    return path


def test_reads_the_refusal(tmp_path):
    assert read_campaign_refusal(_write(tmp_path), **IDS).code == "corpus_inside_role_workspace"


@pytest.mark.parametrize("over", [{"operation_id": "c" * 32}, {"scope_id": "c" * 32},
                                  {"schema": "lab-campaign-diagnostic/1"},
                                  {"failure": {"code": "x"}}, {"extra": 1},
                                  {"refusal": None}, {"refusal": {**REFUSAL, "class": "x"}}])
def test_anything_else_is_no_refusal(tmp_path, over):
    assert read_campaign_refusal(_write(tmp_path, **over), **IDS) is None


def test_garbage_and_absence_are_no_refusal(tmp_path):
    (tmp_path / "diagnostic.json").write_text("{")
    assert read_campaign_refusal(tmp_path / "diagnostic.json", **IDS) is None
    assert read_campaign_refusal(tmp_path / "missing.json", **IDS) is None
    (tmp_path / "list.json").write_text("[]")
    assert read_campaign_refusal(tmp_path / "list.json", **IDS) is None


def test_a_missing_key_is_no_refusal(tmp_path):
    path = _write(tmp_path)
    payload = json.loads(path.read_text())
    del payload["failure"]
    path.write_text(json.dumps(payload))
    assert read_campaign_refusal(path, **IDS) is None


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
@pytest.mark.parametrize("stage", ["before_start", "running"])
def test_reads_worker_lab_failure_with_fixed_message(tmp_path, kind, stage):
    from evolution_lab.owner import diagnostic
    failure = {"code": "lab_error", "stage": stage, "message": "The owner operation failed."}
    path = _write(tmp_path, failure=failure, refusal=None)
    if kind == "evaluation":
        path.write_text(json.dumps({"schema": "lab-evaluation-diagnostic/1", **IDS, "failure": failure}))
    value = diagnostic.read_worker_failure(path, kind=kind, **IDS)
    assert value.to_payload() == failure


@pytest.mark.parametrize("over", [
    {"code": "core_refused"}, {"stage": "invalid"}, {"message": "private text"}, {"extra": 1}])
def test_invalid_worker_failure_is_not_exposed(tmp_path, over):
    from evolution_lab.owner import diagnostic
    failure = {"code": "lab_error", "stage": "running", "message": "The owner operation failed.", **over}
    assert diagnostic.read_worker_failure(_write(tmp_path, failure=failure, refusal=None),
                                          kind="campaign", **IDS) is None


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
@pytest.mark.parametrize("phase", ["config_read", "run_directory"])
def test_worker_entrypoint_failure_prints_only_class(tmp_path, monkeypatch, capsys, kind, phase):
    import os
    from importlib import import_module
    worker = import_module(f"evolution_lab.owner._{kind}_worker")
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    secret = "private-entrypoint-credential"
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    monkeypatch.setenv(f"LAB_{kind.upper()}_CONFIG_FD", str(read_fd))
    def broken(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(worker, "read_frame", broken if phase == "config_read" else lambda *a, **kw: {})
    config = {**IDS, "mode": "new", "run_directory": str(tmp_path / secret),
              "diagnostic_path": str(tmp_path / "diagnostic.json")}
    monkeypatch.setattr(worker, f"validate_{kind}_worker_config", lambda value: config)
    monkeypatch.setattr(worker.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().err == ("RuntimeError\n" if phase == "config_read" else "ValueError\n")
    if phase == "run_directory":
        payload = json.loads((tmp_path / "diagnostic.json").read_text())
        assert payload["failure"] == {"code": "lab_error", "stage": "before_start",
                                      "message": "The owner operation failed."}
        assert payload["exception"] == {"class": "builtins.ValueError", "code": None}
        assert secret not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
def test_running_failure_diagnostic_carries_exception(tmp_path, monkeypatch, kind):
    from importlib import import_module
    worker = import_module(f"evolution_lab.owner._{kind}_worker")
    config = {**IDS, "manifest_path": "unused", "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "prepared_digest": "d" * 64, "storage_root": str(tmp_path),
              "settlement_path": str(tmp_path / "settlement.json"),
              "owner_generation": "c" * 32, "run_id": "neutral-run"}
    def broken(*a, **kw):
        raise RuntimeError("private-message-q7Z9x")
    if kind == "campaign":
        import evolution.tree.evolve as core
        monkeypatch.setattr(core, "admit_manifest", broken)
    else:
        monkeypatch.setattr(worker, "_bindings", broken)
    assert worker.run_worker(config, {"FORWARDED": "q7Z9x"}, lambda: False) == 1
    payload = json.loads((tmp_path / "diagnostic.json").read_text())
    assert payload["schema"] == ("lab-campaign-diagnostic/5" if kind == "campaign" else "lab-evaluation-diagnostic/2")
    assert payload["failure"] == {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    assert payload["exception"] == {"class": "builtins.RuntimeError", "code": None}
    assert "q7Z9x" not in json.dumps(payload)


@pytest.mark.parametrize("schema", ["lab-campaign-diagnostic/2", "lab-campaign-diagnostic/3",
                                   "lab-campaign-diagnostic/4", "lab-evaluation-diagnostic/1"])
def test_older_diagnostics_still_read(tmp_path, schema):
    from evolution_lab.owner import diagnostic
    failure = {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    payload = {"schema": schema, **IDS, "failure": failure}
    kind = "evaluation" if "evaluation" in schema else "campaign"
    if kind == "campaign":
        payload["refusal"] = None
        if not schema.endswith("/2"):
            payload["core_terminal"] = None
        if schema.endswith("/4"):
            payload["findings"] = []
    path = tmp_path / "diagnostic.json"
    path.write_text(json.dumps(payload))
    assert diagnostic.read_worker_failure(path, kind=kind, **IDS).to_payload() == failure
    assert diagnostic.read_worker_exception(path, kind=kind, **IDS) is None
    assert diagnostic.read_campaign_refusal(path, **IDS) is None
    assert diagnostic.read_campaign_core_terminal(path, **IDS) is None
    assert diagnostic.read_campaign_findings(path, **IDS) == ()


@pytest.mark.parametrize("over", [
    {"failure": None}, {"exception": {"class": "builtins.RuntimeError", "code": None, "message": "private"}},
    {"exception": {"class": "bad class", "code": None}},
    {"exception": {"class": "builtins.RuntimeError", "code": "Bad Code"}}])
def test_invalid_exception_invalidates_the_whole_diagnostic(tmp_path, over):
    from evolution_lab.owner import diagnostic
    failure = {"code": "lab_error", "stage": "running", "message": "The owner operation failed."}
    path = _write(tmp_path, **{"schema": "lab-campaign-diagnostic/5", "failure": failure, "refusal": None,
                  "findings": [], "exception": {"class": "builtins.RuntimeError", "code": None}, **over})
    assert diagnostic.read_worker_failure(path, kind="campaign", **IDS) is None
    assert diagnostic.read_worker_exception(path, kind="campaign", **IDS) is None
    assert diagnostic.read_campaign_refusal(path, **IDS) is None
    assert diagnostic.read_campaign_core_terminal(path, **IDS) is None
    assert diagnostic.read_campaign_findings(path, **IDS) == ()


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
def test_stderr_write_failure_keeps_worker_exit_and_diagnostic(tmp_path, monkeypatch, kind):
    from importlib import import_module
    worker = import_module(f"evolution_lab.owner._{kind}_worker")
    config = {**IDS, "manifest_path": "unused", "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "settlement_path": str(tmp_path / "settlement.json"),
              "owner_generation": "c" * 32, "run_id": "neutral-run"}
    def broken(*args, **kwargs):
        raise RuntimeError("private-secret")
    if kind == "campaign":
        import evolution.tree.evolve as core
        monkeypatch.setattr(core, "admit_manifest", broken)
    else:
        monkeypatch.setattr(worker, "_bindings", broken)
    class BrokenStream:
        def write(self, value):
            raise ValueError("closed stream")
        def flush(self):
            raise ValueError("closed stream")
    monkeypatch.setattr(worker.sys, "stderr", BrokenStream())
    assert worker.run_worker(config, {}, lambda: False) == 1
    assert json.loads((tmp_path / "diagnostic.json").read_text())["failure"]["code"] == "lab_error"


@pytest.mark.parametrize("secret", ["sensitive-credential-boundary", "traceback truncated"])
def test_traceback_is_bounded_after_redaction_even_at_byte_boundary(capsys, secret):
    from evolution_lab.owner._worker_stderr import write_exception
    try:
        raise RuntimeError("é" * 65000 + secret)
    except RuntimeError as error:
        write_exception(error, (secret,))
    output = capsys.readouterr().err
    assert "Traceback (most recent call last):" in output
    assert "RuntimeError" in output
    assert secret not in output
    assert len(output.encode()) <= 65536
    assert output.endswith("]\n" if secret == "traceback truncated" else "\n[traceback truncated]\n")


@pytest.mark.parametrize("schema", ["lab-campaign-diagnostic/2", "lab-campaign-diagnostic/3"])
def test_retained_refusal_and_empty_diagnostic_keep_their_meaning(tmp_path, schema):
    from evolution_lab.owner.diagnostic import read_worker_failure
    for refusal in (REFUSAL, None):
        path = _write(tmp_path, schema=schema, refusal=refusal)
        payload = json.loads(path.read_text())
        if schema.endswith("/2"):
            del payload["core_terminal"]
        path.write_text(json.dumps(payload))
        result = read_campaign_refusal(path, **IDS)
        assert (None if result is None else result.to_payload()) == refusal
        assert read_worker_failure(path, kind="campaign", **IDS) is None


@pytest.mark.parametrize("diagnostic_kind,prior", [
    ("campaign", "none"), ("evaluation", "none"), ("campaign", "failure"),
    ("campaign", "refusal"), ("campaign", "core_terminal")])
@pytest.mark.parametrize("stage", ["before_start", "running"])
def test_actual_finalizer_persists_worker_failure_and_requires_cleanup(tmp_path, diagnostic_kind, stage, prior):
    import asyncio
    from types import SimpleNamespace
    from evolution_lab.owner.cleanup import CleanupSupport
    from evolution_lab.owner.models import CoreTerminal, Failure, ProcessFacts, Refusal
    from evolution_lab.owner.store import OwnerStateStore
    from evolution.tree.evolve import derive_run_id
    from test_owner_store_starts import DIGEST, _executor, _ready_target
    failure = {"code": "lab_error", "stage": stage, "message": "The owner operation failed."}
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store),
            executor=_executor(f"lab-{diagnostic_kind}-worker/1"), kind=diagnostic_kind)
        prior_failure = Failure.make("io_error", "finalizing") if prior == "failure" else (
            Failure.make("core_refused", "running") if prior == "refusal" else None)
        prior_refusal = Refusal.from_payload(REFUSAL) if prior == "refusal" else None
        prior_terminal = CoreTerminal(owner="artifact_store", terminal="persistence_failed",
                                      code="record_matches_secret") if prior == "core_terminal" else None
        if prior != "none":
            store.update_operation(accepted.status.id, failure=prior_failure,
                                   refusal=prior_refusal, core_terminal=prior_terminal)
        scope = state / "scopes" / accepted.scope_id
        scope.mkdir(parents=True)
        run = state / "artifacts" / accepted.status.id / accepted.status.id
        run.mkdir(parents=True)
        diagnostic = {"schema": "lab-evaluation-diagnostic/1", "operation_id": accepted.status.id,
                      "scope_id": accepted.scope_id, "failure": failure}
        if diagnostic_kind == "campaign":
            diagnostic.update(schema="lab-campaign-diagnostic/3", refusal=None, core_terminal=None)
        path = scope / "diagnostic.json"
        path.write_text(json.dumps(diagnostic))
        (scope / "cleanup.receipt").write_text(json.dumps({
            "schema": "lab-scope-cleanup/1", "scope_id": accepted.scope_id,
            "owner_generation": store.generation, "child_launched": True,
            "child_exit_code": 1, "process_tree_empty": True, "forced": False, "spawn_closed": True}))
        # Even a quiet ledger with a bound transport is not positive after an exception.
        settlement = {"schema": f"lab-{diagnostic_kind}-settlement/2",
            "operation_id": accepted.status.id, "scope_id": accepted.scope_id,
            "owner_generation": store.generation, "run_id": derive_run_id(run),
            "normal_return": False, "ledger": {"namespace": derive_run_id(run),
                "admission_closed": True, "uncertain_any": False, "quiescent": True,
                "admitted": 0, "completed": 0, "not_sent": 0, "uncertain": 0, "pending": 0,
                "in_flight": 0, "refused": 0, "bound_transports": 1}}
        if diagnostic_kind == "evaluation":
            settlement.update(materialize_started=True, release_started=True)
        (scope / "settlement.json").write_text(json.dumps(settlement))
        class Finalizer(CleanupSupport):
            state_directory = state
            owner_generation = store.generation
            _operation_cache = {}
            async def _db(self, name, *args, **kwargs):
                return getattr(store, name)(*args, **kwargs)
        runtime = SimpleNamespace(id=accepted.status.id, scope_id=accepted.scope_id,
            paths=SimpleNamespace(scope_directory=scope, diagnostic_path=path,
                                  run_directory=run, settlement_path=scope / "settlement.json"),
            prepared=SimpleNamespace(kind=diagnostic_kind), refusal=None, core_terminal=None,
            process_facts=lambda _: ProcessFacts(True, 1, True, False))
        asyncio.run(Finalizer()._finalize_operation(runtime))
        payload = store.operation_status(accepted.status.id).to_payload()
        assert payload["schema"] == "lab-operation-status/8"
        expected_failure = failure if prior == "none" else (
            None if prior_failure is None else prior_failure.to_payload())
        assert payload["failure"] == payload["starts"][-1]["failure"] == expected_failure
        assert payload["refusal"] == (None if prior_refusal is None else prior_refusal.to_payload())
        assert payload["core_terminal"] == (None if prior_terminal is None else prior_terminal.to_payload())
        assert payload["control_state"] == "cleanup_required"
        assert payload["target_cleanup"] == {"state": "unconfirmed", "complete": None}
        from evolution_lab.cli.responses import validate
        assert validate(payload, "lab-operation-status/8").to_payload() == payload


@pytest.mark.parametrize("other", ["refusal", "core_terminal"])
def test_contradictory_failure_diagnostic_is_rejected(tmp_path, other):
    from evolution_lab.owner.diagnostic import read_campaign_core_terminal, read_worker_failure
    values = {"failure": {"code": "lab_error", "stage": "running", "message": "The owner operation failed."},
              "refusal": None, "core_terminal": None}
    values[other] = REFUSAL if other == "refusal" else {
        "owner": "artifact_store", "terminal": "persistence_failed", "code": "record_matches_secret"}
    path = _write(tmp_path, **values)
    assert read_worker_failure(path, kind="campaign", **IDS) is None
    assert read_campaign_refusal(path, **IDS) is None
    assert read_campaign_core_terminal(path, **IDS) is None


async def worker_result(tmp_path, monkeypatch, terminal, *, operation_id=IDS["operation_id"], scope_id=IDS["scope_id"], result_overrides=None, expected_exit=None):
    """Exercise the real campaign worker boundary with a public Core result double."""
    from types import SimpleNamespace
    import evolution.tree.evolve as core
    from evolution_lab.owner import _campaign_worker as worker
    status = SimpleNamespace(to_payload=lambda: {})
    result = SimpleNamespace(terminal=terminal, owner=None if terminal == "finished" else "campaign_journal",
        code=None if terminal == "finished" else "record_matches_secret", quiescent=True, finished=None,
        result=None, status=status, provider_attempt_lines=())
    for key, value in (result_overrides or {}).items():
        setattr(result, key, value)
    async def evolve(*args, **kwargs):
        return result
    monkeypatch.setattr(core, "evolve", evolve)
    config = {"operation_id": operation_id, "scope_id": scope_id, "owner_generation": "generation",
              "run_id": operation_id, "run_directory": str(tmp_path),
              "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "settlement_path": str(tmp_path / "settlement.json")}
    admitted = SimpleNamespace(workflow=None, target=SimpleNamespace(timeouts=SimpleNamespace(
        connect_seconds=.1, read_seconds=.1)))
    exit_code = await worker._evolve(config, {}, lambda: False, admitted, (), ())
    if expected_exit is not None:
        assert exit_code == expected_exit
    return tmp_path / "diagnostic.json"


def test_suspension_has_no_core_terminal(tmp_path, monkeypatch):
    import asyncio
    path = asyncio.run(worker_result(tmp_path, monkeypatch, "suspended"))
    assert json.loads(path.read_text())["core_terminal"] is None


def test_finished_has_no_core_terminal(tmp_path, monkeypatch):
    import asyncio
    path = asyncio.run(worker_result(tmp_path, monkeypatch, "finished"))
    assert json.loads(path.read_text())["core_terminal"] is None


def test_refusal_has_no_core_terminal(tmp_path, monkeypatch):
    import evolution.tree.evolve as core
    from evolution.tree.execution.output_admission import EvolveRefused
    from evolution_lab.owner import _campaign_worker as worker
    def refused(*args):
        raise EvolveRefused("run_dir_not_campaign")
    monkeypatch.setattr(core, "admit_manifest", refused)
    config = {**IDS, "owner_generation": "c" * 32, "manifest_path": "unused",
              "diagnostic_path": str(tmp_path / "diagnostic.json")}
    assert worker.run_worker(config, {}, lambda: False) == 2
    payload = json.loads((tmp_path / "diagnostic.json").read_text())
    assert payload["schema"] == "lab-campaign-diagnostic/5"
    assert payload["core_terminal"] is None
    assert payload["refusal"]["code"] == "run_dir_not_campaign"


@pytest.mark.parametrize("terminal", ["finished", "suspended"])
@pytest.mark.parametrize("code", [None, "another_code"])
def test_ordinary_terminals_remain_absent(tmp_path, monkeypatch, terminal, code):
    import asyncio
    from evolution_lab.owner.diagnostic import read_campaign_core_terminal
    path = asyncio.run(worker_result(tmp_path, monkeypatch, terminal,
                       result_overrides={"owner": "campaign_projection", "code": code}))
    assert json.loads(path.read_text())["core_terminal"] is None
    path = _write(tmp_path, refusal=None, core_terminal={
        "owner": "campaign_projection", "terminal": terminal, "code": code})
    assert read_campaign_core_terminal(path, **IDS) is None


@pytest.mark.parametrize("terminal", ["finished", "suspended", "persistence_failed"])
def test_projection_invalid_preserves_worker_result(tmp_path, monkeypatch, capsys, terminal):
    import asyncio
    finished = {"outcome": "completed"} if terminal == "finished" else None
    asyncio.run(worker_result(tmp_path, monkeypatch, terminal, expected_exit=1,
        result_overrides={"owner": "campaign_projection", "code": "projection_invalid",
                          "finished": finished}))
    document = json.loads(capsys.readouterr().out)
    assert document["terminal"] == terminal
    assert document["quiescent"] is True
    assert document["finished"] == finished
    assert document["result"] is None
    assert document["owner"] == "campaign_projection"
    assert document["code"] == "projection_invalid"
