"""Status /5: entrypoint, progress and journal time next to explicit states (spec 2026-09-29 §2.1)."""
import asyncio
import json
from dataclasses import replace

import pytest

from evolution_lab.owner import OwnerError
from evolution_lab.owner.progress import validate_progress

from test_owner_core_raw import finished_campaign, run_finished_campaign  # noqa: F401 — module fixture
from test_owner_store_starts import DIGEST, _executor, _ready_target


@pytest.fixture
def finished_campaign_status(finished_campaign):
    return finished_campaign[2]


@pytest.fixture
def reserved_campaign_status(tmp_path):
    """An operation the store only reserved (as ``test_owner_store_starts``), read through Owner."""
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store),
                                          executor=_executor())
        (store.state_directory / 'artifacts' / accepted.status.id / accepted.status.id).mkdir(parents=True)

    async def scenario():
        async with Owner.open(tmp_path / "state") as owner:
            return await owner.status(ResourceRef("operation", accepted.status.id))
    return asyncio.run(scenario())


@pytest.fixture
async def owner_with_finished_campaign(finished_campaign):
    from evolution_lab.owner import Owner, ResourceRef
    state, operation_id, _ = finished_campaign
    async with Owner.open(state) as owner:
        yield owner, ResourceRef("operation", operation_id)


def test_status_schema_and_new_keys(finished_campaign, finished_campaign_status):
    payload = finished_campaign_status.to_payload()
    assert payload["schema"] == "lab-operation-status/8"
    assert {"entrypoint", "progress", "progress_state", "times", "times_state", "times_source",
            "times_reason"} <= set(payload)
    assert payload["progress_state"] == "present"
    validate_progress(payload["progress"], "campaign")
    assert payload["progress"]["phase"] == "finished"
    assert payload["progress"]["policy_state"] == "present"
    manifest = json.loads((finished_campaign[0].parent / "source" / "manifest.json").read_text())
    assert payload["progress"]["rounds"]["planned"] == manifest["campaign"]["rounds"]
    assert payload["times_state"] == "present"
    assert set(payload["times"]) == {"first_event_at", "last_event_at"}
    # ADR-0019: the campaign journal part is read by the pinned adapter.
    assert payload["times_source"] == "core_raw" and payload["times_reason"] is None
    assert payload["progress"]["observed_at"] == payload["times"]["last_event_at"]
    assert payload["entrypoint"]  # manifest name from the PreparedExecution descriptor


def test_status_before_the_campaign_journal_is_not_started(reserved_campaign_status):
    payload = reserved_campaign_status.to_payload()
    assert payload["results_state"] == "not_created"
    assert payload["progress"] is None and payload["progress_state"] == "not_started"
    assert payload["times"] is None and payload["times_state"] == "not_started"
    assert payload["times_source"] is None and payload["times_reason"] is None


def test_unread_run_before_the_journal_is_not_started(tmp_path):
    """A run whose projection is unreadable (``results_state: unavailable``) and whose campaign
    journal does not exist yet has not started; it is not an unavailable progress."""
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    with OwnerStateStore.open(tmp_path / "state") as store:
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store),
                                          executor=_executor())

    async def scenario():
        async with Owner.open(tmp_path / "state") as owner:
            status = await owner.status(ResourceRef("operation", accepted.status.id))
            return await owner._views(replace(status, results_state="unavailable"))
    payload = asyncio.run(scenario()).to_payload()
    assert payload["times"] is None and payload["times_state"] == "not_started"
    assert payload["progress"] is None and payload["progress_state"] == "not_started"


async def test_unsupported_core_leaves_public_sections(monkeypatch, owner_with_finished_campaign):
    from evolution_lab.owner import core_raw
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    owner, ref = owner_with_finished_campaign
    payload = (await owner.status(ref)).to_payload()
    assert payload["times"] is None and payload["times_state"] == "unavailable"
    assert payload["times_source"] is None and payload["times_reason"] == "core_raw_unsupported"
    assert payload["progress_state"] == "present"
    # The per-measurement facts are public (``project_events``): the adapter failure keeps them.
    listed = (await owner.measurements(ref))["measurements"]
    assert listed and all(row["usage_state"] == "present" and row["usage"] is not None for row in listed)
    assert all(row["times"] is not None for row in listed)


async def test_unreadable_campaign_journal_is_marked_unreadable(monkeypatch, owner_with_finished_campaign):
    from evolution_lab.owner import core_raw

    def broken(run, family_version):
        raise ValueError("torn journal")
    monkeypatch.setattr(core_raw, "campaign_log", broken)
    owner, ref = owner_with_finished_campaign
    payload = (await owner.status(ref)).to_payload()
    assert payload["times"] is None and payload["times_state"] == "unavailable"
    assert payload["times_source"] is None and payload["times_reason"] == "unreadable"


@pytest.fixture
def lost_run_campaign(tmp_path):
    """A campaign of its own, launched and finished, whose run directory is then removed."""
    state, operation_id, status = run_finished_campaign(tmp_path)
    assert status.process.launched is True
    # Moved out rather than removed: core leaves its candidates read-only.
    (state / "artifacts" / operation_id / operation_id).rename(tmp_path / "lost-run")
    return state, operation_id, status


async def test_launched_campaign_with_a_lost_run_is_unavailable(lost_run_campaign):
    """inspectable-experiments: a run gone after launch is a failed read, not "not started"."""
    from evolution_lab.owner import Owner, ResourceRef
    state, operation_id, _ = lost_run_campaign
    async with Owner.open(state) as owner:
        status = await owner.status(ResourceRef("operation", operation_id))
        payload = status.to_payload()
        assert payload["results_state"] == "unavailable"
        assert payload["times"] is None and payload["times_state"] == "unavailable"
        assert payload["times_source"] is None and payload["times_reason"] == "unreadable"
        # Settled: progress comes from the stored core (the settled fallback of ``_views``), next
        # to the explicit ``results_state: unavailable`` — never "not_started".
        assert payload["progress_state"] == "present" and payload["result"]["core"] is not None
        # A running operation has no settled core to fall back on: progress is unavailable too.
        running = await owner._views(replace(status, control_state="running"))
        assert running.progress is None and running.progress_state == "unavailable"
        assert running.times_state == "unavailable"


@pytest.mark.parametrize('journal', ['absent', 'empty', 'corrupt', 'torn', 'dangling',
                                     'metadata_error', 'pin', 'unexpected_type', 'unexpected_io'])
def test_lost_run_projection_depends_on_launch(tmp_path, finished_campaign_status, monkeypatch, journal):
    """A missing run after launch is damage; an absent journal in an existing
    run is the creation window only when neither prior core nor other errors exist."""
    from evolution_lab.owner import core_raw, readers
    project_operation = readers.project_operation
    status = replace(finished_campaign_status, result=None)
    assert project_operation(tmp_path, status).results_state == "unavailable"
    for launched in (None, False):
        unlaunched = replace(status, process=replace(status.process, launched=launched, exit_code=None))
        assert project_operation(tmp_path, unlaunched).results_state == "not_created"
    run = tmp_path / 'artifacts' / status.id / status.id
    run.mkdir(parents=True)
    path = run / 'campaign.jsonl'
    if journal in {'empty', 'corrupt', 'torn'}:
        path.write_bytes({'empty': b'', 'corrupt': b'broken\n',
                          'torn': b'{"event":"campaign_created"'}[journal])
    elif journal == 'dangling':
        path.symlink_to(tmp_path / 'absent-journal')
    elif journal == 'metadata_error':
        def fail_metadata(*a, **k):
            raise PermissionError('temporary metadata refusal')
        monkeypatch.setattr(readers, 'open_directory_nofollow', fail_metadata)
    elif journal == 'pin':
        monkeypatch.setattr(core_raw, '_installed_core', lambda: 'wrong')
    elif journal in {'unexpected_type', 'unexpected_io'}:
        def fail_reader(*a, **k):
            raise TypeError('unexpected') if journal == 'unexpected_type' else OSError('unexpected')
        monkeypatch.setattr('evolution.tree.campaign_projections.inspect_view', fail_reader)
    for launched in (None, False, True):
        current = replace(status, process=replace(status.process, launched=launched, exit_code=None))
        value = project_operation(tmp_path, current)
        assert value.results_state == ('not_created' if journal == 'absent' else 'unavailable')
        assert value.result is None or value.result.core is None


@pytest.mark.parametrize("marking", [
    {"times_source": "core_public"},                       # a campaign's times are core_raw
    {"times_source": None},                                 # present times name their source
    {"times_reason": "unreadable"},                         # a reason only when unavailable
])
def test_times_marking_is_closed(finished_campaign_status, marking):
    with pytest.raises(OwnerError):
        replace(finished_campaign_status, **marking)


def test_unavailable_times_need_a_known_reason(finished_campaign_status):
    base = dict(times=None, times_source=None, times_state="unavailable")
    assert replace(finished_campaign_status, **base, times_reason="unreadable").times_reason == "unreadable"
    for reason in (None, "later"):
        with pytest.raises(OwnerError):
            replace(finished_campaign_status, **base, times_reason=reason)


@pytest.mark.parametrize("field,value", [("progress_state", "maybe"), ("times_state", "later")])
def test_states_are_closed(finished_campaign_status, field, value):
    with pytest.raises(OwnerError):
        replace(finished_campaign_status, **{field: value})


def test_progress_requires_present_state(finished_campaign_status):
    with pytest.raises(OwnerError):
        replace(finished_campaign_status, progress_state="unavailable")  # progress still set


def test_status_carries_no_project_fields(finished_campaign_status):
    payload = finished_campaign_status.to_payload()
    assert not {"client_ref", "project", "implementation", "label"} & set(payload)


async def test_status_during_a_result_write_keeps_a_known_entrypoint(owner_with_finished_campaign):
    owner, ref = owner_with_finished_campaign
    entrypoint = (await owner.status(ref)).entrypoint
    assert entrypoint
    writing = owner._result_io.writing
    owner._result_io.writing = lambda operation_id: True
    try:
        value = await owner.status(ref)
    finally:
        owner._result_io.writing = writing
    assert value.entrypoint == entrypoint
    assert value.progress_state == "unavailable" and value.times_state == "unavailable"


def test_launched_evaluation_with_a_lost_run_is_unavailable(tmp_path, monkeypatch):
    """The evaluation branch: a run gone after launch is unavailable; a run whose worker has
    not written (or never writes, e.g. cancelled) the measurement journal is not."""
    from evolution_lab.owner.readers import _times_and_facts, project_operation
    from test_owner_summary import run_finished_evaluation

    (tmp_path / "evaluation").mkdir()
    status = run_finished_evaluation(tmp_path / "evaluation", monkeypatch)
    assert status.kind == "evaluation" and status.process.launched is True
    empty = tmp_path / "empty"
    assert project_operation(empty, status).results_state == "unavailable"
    view, _ = _times_and_facts(empty, status, None)
    assert view == {"times": None, "times_state": "unavailable", "times_source": None,
                    "times_reason": "unreadable"}
    (empty / "artifacts" / status.id / status.id).mkdir(parents=True)
    assert project_operation(empty, status).results_state == "not_created"
    view, _ = _times_and_facts(empty, status, None)
    assert view["times_state"] == "not_started" and view["times_reason"] is None


def test_status_shows_stop_reason_and_unmeasured():
    from io import StringIO
    from evolution_lab.owner.models import Coverage, OperationResult, ProcessFacts
    from evolution_lab.cli.output import write_value
    result = OperationResult(DIGEST, ProcessFacts(), kind="evaluation", coverage=Coverage(20, 19),
        stopped_reason="budget_exhausted", min_coverage=0.95,
        unmeasured=[{"case_id": "c", "sample_id": "s", "outcome": "time_budget", "tries": 1}])
    payload = result.to_payload()
    assert payload["schema"] == "lab-operation-result/3" and "stop_reason" not in payload
    assert OperationResult.from_payload(payload).to_payload() == payload
    stream = StringIO()
    write_value(stream, {"schema": "lab-operation-status/8", "kind": "evaluation", "result": payload}, machine=False)
    assert "19 of 20 scored" in stream.getvalue()
    assert "unmeasured=1" in stream.getvalue() and "stopped_reason=budget_exhausted" in stream.getvalue()
    accepted = {**payload, "stopped_reason": None, "persistence": {"state": "saved", "failure": None}}
    stream = StringIO()
    write_value(stream, {"schema": "lab-operation-status/8", "kind": "evaluation", "control_state": "finished",
                        "result": accepted}, machine=False)
    assert "accepted" in stream.getvalue() and "19 of 20 scored" in stream.getvalue()
    legacy = {key: value for key, value in payload.items() if key not in {
        "stopped_reason", "min_coverage", "unmeasured", "unanswered", "unanswered_state", "answered_reported"}}
    legacy.update(schema="lab-operation-result/1", stop_reason="cancelled")
    restored = OperationResult.from_payload(legacy)
    assert restored.stopped_reason == "cancelled" and restored.min_coverage == 1.0


@pytest.mark.parametrize("fail_write", [False, True])
@pytest.mark.parametrize("terminal,owner,code", [
    ("persistence_failed", "campaign_journal", "record_matches_secret"),
    ("finished", "campaign_projection", "projection_invalid"),
    ("suspended", "campaign_projection", "projection_invalid"),
    ("storage_capacity_exhausted", "campaign_projection", "projection_invalid"),
    ("persistence_failed", "campaign_projection", "projection_invalid"),
])
def test_projection_invalid_reaches_core_terminal_for_any_terminal(tmp_path, monkeypatch, fail_write, terminal, owner, code, confirmed_cleanup=False):
    from types import SimpleNamespace
    from evolution_lab._suspension import suspension_hint
    from evolution_lab.owner.cleanup import CleanupSupport
    from evolution_lab.owner.models import OwnerError, ProcessFacts, ReportResult
    from evolution_lab.owner.store import OwnerStateStore
    from evolution_lab.owner import readers
    from test_owner_diagnostic import worker_result
    from evolution_lab.cli.responses import validate

    # Core projection is irrelevant to this failure: the terminal must survive even
    # when Core produced no readable result and when the first persistence write fails.
    monkeypatch.setattr(readers, "project_operation", lambda root, value, **kw: value)
    if confirmed_cleanup:
        # Isolate Lab propagation from Core journal projection and target HTTP cleanup.
        # These doubles describe positive cleanup of a normally exited worker (exit 1).
        from evolution_lab.owner import cleanup
        import evolution.tree.evolve as core
        monkeypatch.setattr(readers, "project_operation",
                            lambda root, value, **kw: replace(value, control_state="finished"))
        monkeypatch.setattr(cleanup, "read_cleanup_receipt", lambda *a: {
            "child_launched": True, "child_exit_code": 1, "forced": False})
        monkeypatch.setattr(core, "derive_run_id", lambda root: "test-run")
        monkeypatch.setattr(cleanup, "read_campaign_settlement", lambda *a, **kw: object())
        monkeypatch.setattr(cleanup, "positive_settlement", lambda *a, **kw: True)
        async def swept(*args, scratch=None):
            return {}
        monkeypatch.setattr(cleanup, "sweep_namespace", swept)
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        accepted = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store), executor=_executor())
        scope = state / "scopes" / accepted.scope_id
        scope.mkdir(parents=True)
        asyncio.run(worker_result(scope, monkeypatch, terminal,
                    operation_id=accepted.status.id, scope_id=accepted.scope_id,
                    result_overrides={"owner": owner, "code": code}))
        published = json.loads((scope / "diagnostic.json").read_text())
        triple = {"owner": owner, "terminal": terminal, "code": code}
        assert published["schema"] == "lab-campaign-diagnostic/5"
        assert published["core_terminal"] == triple and published["failure"] is None
        from evolution_lab.owner.execution import ExecutionSupport, OperationRuntime
        class Finalizer(CleanupSupport, ExecutionSupport):
            state_directory = state
            scratch_directory = None
            owner_generation = store.generation
            _operation_cache = {}
            async def _discard_released_snapshots(self):
                pass
            async def _db(self, name, *args, **kwargs):
                if fail_write and name == "update_operation" and not getattr(self, "failed", False):
                    self.failed = True
                    raise OwnerError("io_error", "finalizing")
                return getattr(store, name)(*args, **kwargs)
        runtime = SimpleNamespace(id=accepted.status.id, scope_id=accepted.scope_id,
            paths=SimpleNamespace(scope_directory=scope, diagnostic_path=scope / "diagnostic.json",
                                  run_directory=scope, settlement_path=scope / "settlement.json"),
            prepared=SimpleNamespace(kind="campaign", bundle=None), refusal=None, core_terminal=None, policy_use=None,
            target=SimpleNamespace(ref=accepted.status.target_ref, stop=SimpleNamespace(is_set=lambda: False)),
            process_facts=lambda previous: ProcessFacts(True, 1, True, False))
        runtime.close_policy_use = OperationRuntime.close_policy_use.__get__(runtime)
        async def finish():
            finalizer = Finalizer()
            try:
                await finalizer._finalize_operation(runtime)
            except OwnerError as error:
                await finalizer._operation_failure(runtime, error.failure)
        asyncio.run(finish())
        payload = store.operation_status(accepted.status.id).to_payload()
        assert payload["schema"] == "lab-operation-status/8"
        assert payload["control_state"] == ("finished" if confirmed_cleanup else "cleanup_required")
        assert payload["process"]["exit_code"] == 1
        expected = {**triple, "hint": {lang: suspension_hint(terminal, lang, code)
                                      for lang in ("ru", "en")}}
        assert all(expected["hint"].values())
        assert payload["core_terminal"] == payload["starts"][-1]["core_terminal"] == expected
        if code == "projection_invalid":
            for lang, hint in payload["core_terminal"]["hint"].items():
                assert ("journal is intact" if lang == "en" else "журнал сохранён") in hint
                assert ("Install a fixed Core" if lang == "en" else "Установите исправленный Core") in hint
                if terminal == "finished":
                    assert "evo tree watch --once" in hint and "evolve" not in hint
                elif terminal == "suspended":
                    assert "evo tree evolve" in hint and "watch" not in hint
                    assert ("same" if lang == "en" else "тем же") in hint
                    assert ("after one rebase" if lang == "en" else "после одного rebase") in hint
                else:
                    assert "evolve" not in hint and "watch" not in hint
        assert payload["refusal"] is None
        assert payload["failure"] is None if not fail_write else payload["failure"]["code"] == "io_error"
        assert validate(payload, "lab-operation-status/8").to_payload() == payload
        report = ReportResult(accepted.status.id, "unavailable", None, "unavailable",
                              starts=store.operation_status(accepted.status.id).starts).to_payload()
        assert report["schema"] == "lab-owner-report/15" and report["core_terminal"] == expected
        assert validate(report, "lab-owner-report/15").to_payload() == report
    with OwnerStateStore.open(state) as store:
        assert store.operation_status(accepted.status.id).to_payload()["core_terminal"] == expected


def test_projection_invalid_preserves_finished_state_with_confirmed_cleanup(tmp_path, monkeypatch):
    test_projection_invalid_reaches_core_terminal_for_any_terminal(
        tmp_path, monkeypatch, False, "finished", "campaign_projection", "projection_invalid",
        confirmed_cleanup=True)
