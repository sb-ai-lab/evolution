"""A linked start keeps the predecessor's promoted tree and Core's provenance."""
import asyncio
import json
from pathlib import Path

import pytest

from test_owner_target import ready_target
from test_owner_cleanup import finished
from test_owner_binding import CORE_WHEEL, _installed_lab_wheel

pytestmark = pytest.mark.slow


def test_unknown_and_deleted_predecessors_are_refused(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.store import OwnerStateStore
    from test_owner_store_starts import _ready_target, _executor, _finish_normally, DIGEST
    state = tmp_path / "state"
    with OwnerStateStore.open(state) as store:
        first = store.accept_operation(prepared_digest=DIGEST, target_ref=_ready_target(store), executor=_executor())
        _finish_normally(store, first.status.id, first.scope_id)
        store.update_operation(first.status.id, results_state="deleted")
    async def scenario():
        async with Owner.open(state) as owner:
            for predecessor, expected in [("unknown", "linked_predecessor_unknown"),
                                           (first.status.id, "linked_predecessor_deleted")]:
                with pytest.raises(OwnerError) as error:
                    await owner.start(DIGEST, target_ref=None, secrets={}, linked_from=predecessor)
                assert error.value.code == expected
    asyncio.run(scenario())


def test_unfinished_predecessor_gets_core_refusal(tmp_path):
    from evolution.tree import scaffold
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore
    from scaffold_15 import apply_legacy_failure_profile
    from test_owner_target import make_bundle
    from test_owner_resume import _suspended_operation, _replace_target
    flag = tmp_path / 'broken'; flag.write_text('')
    def statistical(path):
        sample = scaffold(path)
        raw = json.loads(sample.manifest.read_text())
        apply_legacy_failure_profile(raw)
        sample.manifest.write_text(json.dumps(raw))
        return sample
    state, bundle, _ = make_bundle(tmp_path, mode=f'fail_after:1:{flag}', launch_schema=2,
                                   scaffold=statistical)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            target = await _replace_target(owner, bundle, target)
            accepted = await owner.start(prepared.digest, target_ref=target, secrets={}, linked_from=ref.id)
            done = await finished(owner, ResourceRef('operation', accepted.id), seconds=60)
            assert done.failure.code == 'core_refused', done.to_payload()
            assert done.refusal.code == 'lineage_not_finished', done.refusal
    asyncio.run(scenario())


def _promoted_project(tmp_path):
    from evolution.tree import scaffold
    from evolution_lab.projects.demo import campaign_project
    from evolution_lab.quickstart._campaign import campaign
    project = campaign_project(tmp_path / "project")
    reference = scaffold(tmp_path / "reference")
    doc = json.loads((project / "manifest.json").read_text())
    template = json.loads(reference.manifest.read_text())
    doc["campaign"]["gate"] = template["campaign"]["gate"]
    doc["campaign"]["evaluation"] = template["campaign"]["evaluation"]
    (project / "manifest.json").write_text(json.dumps(doc))
    result = asyncio.run(campaign(project, wheels=(_installed_lab_wheel(), CORE_WHEEL), environ={}))
    assert result.summary["campaign"]["promoted"] is True, result.summary
    return result


@pytest.mark.parametrize("seed", ["champion", "project"])
def test_linked_successor_seed_and_provenance(tmp_path, seed):
    from evolution_lab.owner import Owner, ResourceRef, OwnerError
    from evolution_lab.preparation.storage import PreparationStorage
    result = _promoted_project(tmp_path)
    state = Path(result.summary["state"])
    predecessor = result.summary["campaign"]["operation_id"]
    storage = PreparationStorage(state)
    bundle = storage.get_bundle(result.summary["prepared"]["bundle"])
    async def scenario():
        async with Owner.open(state) as owner:
            from evolution_lab.owner import TargetRef
            connection = await owner.bind(bundle.digest)
            started = await owner.wait(await owner.start_target(connection, secrets={}))
            assert started.state == 'succeeded', started.to_payload()
            target = TargetRef(started.ref.id, owner.owner_generation)
            prior = await owner.status(ResourceRef("operation", predecessor))
            for identifier in ["unknown"]:
                with pytest.raises(OwnerError) as error:
                    await owner.start(result.summary["prepared"]["digest"], target_ref=target,
                                      secrets={}, linked_from=identifier)
                assert error.value.code == "linked_predecessor_unknown"
            accepted = await owner.start(result.summary["prepared"]["digest"], target_ref=target,
                secrets={}, linked_from=predecessor, seed=seed)
            ref = ResourceRef("operation", accepted.id)
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, (None if done.refusal is None else done.refusal.to_payload())
            core = done.result.core
            from evolution.tree import inspect_campaign
            run = state / "artifacts" / accepted.id / accepted.id
            raw = inspect_campaign(run, verify=True).to_payload()
            assert core["linked_from"] is not None
            journal = [json.loads(line) for line in (run / "campaign.jsonl").read_text().splitlines()]
            created = next(row["payload"] for row in journal if row["event"] == "campaign_created")
            prior_journal = [json.loads(line) for line in
                (state / "artifacts" / predecessor / predecessor / "campaign.jsonl").read_text().splitlines()]
            prior_created = next(row["payload"] for row in prior_journal if row["event"] == "campaign_created")
            assert created["identity"]["artifact_store"]["seed_candidate_digest"] == (
                prior.result.core["final_champion"]["digest"] if seed == "champion" else
                prior_created["identity"]["artifact_store"]["seed_candidate_digest"])
            if seed == "champion":
                assert core['linked_from']['baseline']['mode'] == 'inherit'
                assert core['linked_from']['baseline']['imported'] > 0
                diff = await owner.diff(ref)
                assert diff.state == "no_change"
                assert diff.source_digest == prior.result.core["final_champion"]["digest"]
            else:
                assert "condition_changed" in json.dumps(raw)
            report = await owner.report(ref)
            assert report.state == "available"
    asyncio.run(scenario())


def test_refused_link_refreshes_its_unbound_champion_copy(tmp_path, monkeypatch):
    """A first refusal must not freeze an obsolete seed before Core creates a run."""
    from evolution_lab.owner import Owner, ResourceRef, TargetRef, execution
    from evolution_lab.preparation.storage import PreparationStorage
    first = _promoted_project(tmp_path)
    state = Path(first.summary['state'])
    predecessor = first.summary['campaign']['operation_id']
    bundle = PreparationStorage(state).get_bundle(first.summary['prepared']['bundle'])
    original = execution.campaign_worker_command
    def refused(python):
        return [str(python), '-c', "import evolution.tree.evolve as e; from evolution.tree import EvolveRefused; "
            "e.admit_manifest=lambda p: (_ for _ in ()).throw(EvolveRefused('lineage_not_finished')); "
            "from evolution_lab.owner._campaign_worker import main; main()"]
    async def target(owner):
        connection = await owner.bind(bundle.digest)
        started = await owner.wait(await owner.start_target(connection, secrets={}))
        assert started.state == 'succeeded'
        return TargetRef(started.ref.id, owner.owner_generation)
    async def scenario():
        async with Owner.open(state) as owner:
            monkeypatch.setattr(execution, 'campaign_worker_command', refused)
            current = await target(owner)
            accepted = await owner.start(first.summary['prepared']['digest'], target_ref=current,
                                          secrets={}, linked_from=predecessor)
            ref = ResourceRef('operation', accepted.id)
            status = await finished(owner, ref, seconds=60)
            assert status.failure.code == 'core_refused'
            run = state / 'artifacts' / ref.id / ref.id
            assert not (run / 'campaign.jsonl').exists()
            # This copy was never bound by a Core creation record. It can be
            # stale after a failed launch while the predecessor was unfinished.
            copied = run.parent / 'linked-seed/skills/SKILL.md'
            assert copied.read_text().startswith('IMPROVED:')
            copied.write_text('Reference skill: answer with this line.\n')
            assert (await owner.wait(await owner.cleanup(ref))).state == 'succeeded'
            monkeypatch.setattr(execution, 'campaign_worker_command', original)
            current = await target(owner)
            await owner.resume(ref, target_ref=current, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, done.refusal
            assert done.result.core['linked_from']['baseline']['imported'] > 0
            assert copied.read_text().startswith('IMPROVED:')
    asyncio.run(scenario())
