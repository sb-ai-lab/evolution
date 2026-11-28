"""All findings survive the worker boundary without changing the status contract."""
import asyncio
import json

from test_owner_store_starts import DIGEST, _executor, _ready_target, _finish_normally
from evolution_lab.owner.store import OwnerStateStore


def test_worker_publishes_all_findings(tmp_path, monkeypatch):
    from evolution.tree import EvolveRefused, effect_sentence
    import evolution.tree.evolve as core
    from evolution_lab.owner import _campaign_worker as worker, diagnostic
    error = EvolveRefused.from_findings((
        EvolveRefused("run_identity_changed", "policy", instance_digest="a" * 64,
                      effect="continue", effects=("continue", "rebaseline", "close")).findings[0],
        EvolveRefused("run_dir_unsafe_entry", "private-credential", instance_digest="b" * 64,
                      effect="hygiene").findings[0],
    ))
    def refused(*args):
        raise error
    monkeypatch.setattr(core, "admit_manifest", refused)
    config = {"operation_id": "op1", "scope_id": "scope1", "owner_generation": "c" * 32,
              "manifest_path": "unused",
              "diagnostic_path": str(tmp_path / "diagnostic.json")}
    assert worker.run_worker(config, {}, lambda: False) == 2
    findings = diagnostic.read_campaign_findings(tmp_path / "diagnostic.json",
                                               operation_id="op1", scope_id="scope1")
    assert [f.code for f in findings] == ["run_identity_changed", "run_dir_unsafe_entry"]
    assert findings[0].effects == ("continue", "rebaseline", "close")
    assert findings[0].effect_texts == tuple(" ".join(effect_sentence(e))
                                          for e in findings[0].effects)
    legacy = diagnostic.read_campaign_refusal(tmp_path / "diagnostic.json",
                                            operation_id="op1", scope_id="scope1")
    assert legacy.to_payload() == {"code": "run_identity_changed", "path": "policy",
                                  "class": "authorizable", "instance_digest": "a" * 64}


def test_findings_are_redacted_and_bound_to_scope(tmp_path):
    from evolution.tree import EvolveRefused
    from evolution_lab.owner import _campaign_worker as worker, diagnostic
    config = {"operation_id": "op1", "scope_id": "s1", "diagnostic_path": str(tmp_path / "d.json")}
    error = EvolveRefused("run_dir_unsafe_entry", "private-credential", instance_digest="b" * 64,
                          effect="hygiene")
    worker._publish_diagnostic(config, refusal=worker.refusal_payload(error, ("private-credential",)),
                              findings=worker.refusal_findings(error, ("private-credential",)))
    assert "private-credential" not in (tmp_path / "d.json").read_text()
    assert len(diagnostic.read_campaign_findings(tmp_path / "d.json", operation_id="op1", scope_id="s1")) == 1
    assert diagnostic.read_campaign_findings(tmp_path / "d.json", operation_id="op1", scope_id="s2") == ()


def test_refusal_record_survives_reopen_and_pre_core_acceptance(tmp_path):
    from evolution_lab.owner.models import RefusalFinding
    from evolution_lab.owner import Owner
    from evolution_lab.owner.models import ResourceRef
    findings = (RefusalFinding("run_identity_changed", "policy", "authorizable", "a" * 64,
                               ("continue",), ("Continue.",), ()),
                RefusalFinding("journal_busy", None, "hard", None, (), (), ()))
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=target, executor=_executor())
        store.record_refusal(first.status.id, start=1, findings=findings)
        _finish_normally(store, first.status.id, first.scope_id)
        assert store.operation_refusal(first.status.id).findings == findings
        second = store.accept_resume(first.status.id, target_ref=target, executor=_executor())
        assert store.operation_refusal(first.status.id).start == 1
        store.record_refusal(first.status.id, start=2, findings=findings)
        _finish_normally(store, first.status.id, second.scope_id)
    async def read():
        async with Owner.open(state) as owner:
            record = await owner.refusal(ResourceRef("operation", first.status.id))
            assert record.start == 2 and record.findings == findings
    asyncio.run(read())


def test_operation_status_keys_unchanged(tmp_path):
    from evolution_lab.owner.models import Refusal
    with OwnerStateStore.open(tmp_path / "state") as store:
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store), executor=_executor())
        payload = store.operation_status(first.status.id).to_payload()
    assert payload["schema"] == "lab-operation-status/8"
    assert set(payload) == {"schema", "id", "owner_generation", "prepared_digest", "kind", "target_ref",
                            "control_state", "process", "target_cleanup", "result", "failure", "refusal",
                            "core_terminal", "results_state", "executor", "retained_summary", "starts",
                            "resume", "entrypoint", "progress", "progress_state", "times", "times_state",
                            "times_source", "times_reason", "auto_resume"}
    assert set(Refusal("journal_busy", None, "hard", None).to_payload()) == {
        "code", "path", "class", "instance_digest"}


def test_v11_store_migrates_without_losing_operations(tmp_path):
    import sqlite3
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store), executor=_executor())
    with sqlite3.connect(state / "owner.sqlite3") as database:
        database.execute("DROP TABLE approvals")
        database.execute("DROP TABLE IF EXISTS operation_refusals")
        database.execute("ALTER TABLE operation_starts DROP COLUMN prepared_digest")
        database.execute("PRAGMA user_version=11")
    with OwnerStateStore.open(state) as store:
        assert store.operation_status(first.status.id).prepared_digest == DIGEST
        assert store.operation_refusal(first.status.id) is None


def test_finalizer_persists_the_current_worker_findings(tmp_path):
    from types import SimpleNamespace
    from evolution_lab.owner.cleanup import CleanupSupport
    from evolution_lab.owner.models import ProcessFacts
    from evolution_lab.owner._campaign_worker import _publish_diagnostic
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store), executor=_executor())
        scope = state / "scopes" / first.scope_id
        scope.mkdir()
        run = state / "artifacts" / first.status.id / first.status.id
        run.mkdir(parents=True)
        finding = {"code": "journal_busy", "path": None, "class": "hard", "instance_digest": None,
                   "effects": [], "effect_texts": [], "differs": []}
        _publish_diagnostic({"operation_id": first.status.id, "scope_id": first.scope_id,
                            "diagnostic_path": str(scope / "diagnostic.json")},
                            refusal={k: finding[k] for k in ("code", "path", "class", "instance_digest")},
                            findings=(finding,))
        class Finalizer(CleanupSupport):
            state_directory = state
            owner_generation = store.generation
            _operation_cache = {}
            async def _db(self, method, *args, **kwargs):
                return getattr(store, method)(*args, **kwargs)
        runtime = SimpleNamespace(id=first.status.id, scope_id=first.scope_id, refusal=None,
            core_terminal=None, prepared=SimpleNamespace(kind="campaign"),
            process_facts=lambda _: ProcessFacts(True, 2, True, False),
            paths=SimpleNamespace(scope_directory=scope, run_directory=run,
                diagnostic_path=scope / "diagnostic.json", settlement_path=scope / "settlement.json"))
        asyncio.run(Finalizer()._finalize_operation(runtime))
        record = store.operation_refusal(first.status.id)
        assert [f.to_payload() for f in record.findings] == [finding]
        assert store.operation_status(first.status.id).refusal.code == "journal_busy"
