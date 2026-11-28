"""Saved evaluation choices use Core's baseline import and existing Owner retry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from evolution_lab.owner import Owner, OwnerError, ResourceRef, TargetRef, execution
from evolution_lab.preparation.storage import PreparationStorage
from test_baseline_import_main_path import evaluated_project  # noqa: F401 — shared /16 fixture
from test_owner_cleanup import finished
from test_owner_store_starts import DIGEST, _executor, _finish_normally, _ready_target
from test_preparation_bundle import fixture as bundle_fixture
from test_quickstart_campaign import _campaign

pytestmark = pytest.mark.slow


def _refused_worker(python):
    return [
        str(python),
        "-I",
        "-c",
        "import evolution.tree.evolve as e; from evolution.tree import EvolveRefused; "
        "e.admit_manifest=lambda p: (_ for _ in ()).throw("
        "EvolveRefused('baseline_source_unavailable', 'baseline_import')); "
        "from evolution_lab.owner._campaign_worker import main; main()",
    ]


def test_campaign_continue_owed_copy(evaluated_project, monkeypatch):  # noqa: F811
    from evolution_lab.owner import BaselineImportChoice

    from evolution.tree import inspect_journal

    project, source = evaluated_project
    prepared = _campaign(project).summary
    state = Path(prepared["state"])
    bundle = PreparationStorage(state).get_bundle(prepared["prepared"]["bundle"])
    prepared_digest = prepared["prepared"]["digest"]
    original_worker = execution.campaign_worker_command

    async def ready(owner):
        connection = await owner.bind(bundle.digest)
        status = await owner.wait(await owner.start_target(connection, secrets={}))
        assert status.state == "succeeded", status.to_payload()
        return TargetRef(status.ref.id, owner.owner_generation)

    async def scenario():
        async with Owner.open(state) as owner:
            monkeypatch.setattr(execution, "campaign_worker_command", _refused_worker)
            target = await ready(owner)
            choice = BaselineImportChoice(source.parent.name, state, source)
            accepted = await owner.start(
                prepared_digest, target_ref=target, secrets={}, baseline_import=choice
            )
            ref = ResourceRef("operation", accepted.id)
            first = await finished(owner, ref, seconds=60)
            assert first.failure.code == "core_refused", first.to_payload()
            assert (await owner.refusal(ref)).findings[0].code == "baseline_source_unavailable"
            operation = state / "artifacts" / ref.id
            metadata = operation / "baseline-import.json"
            assert json.loads(metadata.read_bytes()) == {
                "schema": "lab-operation-baseline-import/1",
                "operation_id": ref.id,
                "source_operation_id": source.parent.name,
                "source_state": str(state),
                "source": str(source.resolve()),
            }
            first_manifest = state / "scopes" / first.starts[-1].scope_id / "campaign-manifest.json"
            original_bytes = first_manifest.read_bytes()
            raw = json.loads(original_bytes)
            assert raw["baseline_import"] == {"source": str(source.resolve())}
            assert "linked_from" not in raw
            assert not (operation / ref.id / "campaign.jsonl").exists()
            assert (await owner.wait(await owner.cleanup(ref))).state == "succeeded"

            monkeypatch.setattr(execution, "campaign_worker_command", original_worker)
            replacement = await ready(owner)
            await owner.resume(ref, target_ref=replacement, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, done.to_payload()
            resumed_manifest = (
                state / "scopes" / done.starts[-1].scope_id / "campaign-manifest.json"
            )
            assert resumed_manifest.read_bytes() == original_bytes
            baseline = operation / ref.id / "measurements" / "m-r00-champion"
            identity = inspect_journal(baseline).records[0].payload["identity"]
            assert (
                identity["scorer"]
                == inspect_journal(source).records[0].payload["identity"]["scorer"]
            )
            assert done.result.core["baseline_import"]["imported"] == 3

    asyncio.run(scenario())


def test_linked_manifest_copy_keeps_legacy_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from evolution_lab.owner.linked import manifest_copy

    from evolution.tree import load_manifest_v2

    _, root, _ = bundle_fixture(tmp_path, monkeypatch)
    path = root / "manifest.json"
    manifest = load_manifest_v2(path)
    binding = SimpleNamespace(manifest_path=path, manifest=manifest)
    predecessor, seed = tmp_path / "run", tmp_path / "champion"
    actual = manifest_copy(
        binding,
        predecessor=predecessor,
        baseline="inherit",
        seed=seed,
        out=tmp_path / "linked.json",
    ).read_bytes()

    expected = json.loads(path.read_bytes())
    for role in ("scorer", "editor", "reflector"):
        command = expected[role]
        cwd = root / command["cwd"]
        command["cwd"] = str(cwd)
        command["argv"][1] = str(cwd / command["argv"][1])
    expected["corpus"]["path"] = str(root / expected["corpus"]["path"])
    expected.update(
        seed_root=str(seed), linked_from={"run_dir": str(predecessor), "baseline": "inherit"}
    )
    assert actual == json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_baseline_source_refusals(tmp_path):
    from evolution_lab.owner import BaselineImportChoice
    from evolution_lab.owner.store import OwnerStateStore

    state = tmp_path / "state"
    from types import SimpleNamespace

    def choice(operation):
        return BaselineImportChoice(
            operation, state, state / "artifacts" / operation / operation / "measurement"
        )

    with OwnerStateStore.open(state) as store:
        target = _ready_target(store)
        campaign = store.accept_operation(
            prepared_digest=DIGEST, target_ref=target, executor=_executor()
        )
        _finish_normally(store, campaign.status.id, campaign.scope_id)
        evaluation = store.accept_operation(
            prepared_digest=DIGEST,
            target_ref=target,
            executor=_executor("lab-evaluation-worker/1"),
            kind="evaluation",
        )
        _finish_normally(store, evaluation.status.id, evaluation.scope_id)
        store.update_operation(evaluation.status.id, results_state="deleted")

        async def db(method, identifier):
            return getattr(store, method)(identifier)

        owner = SimpleNamespace(
            _assert_mutation=lambda: None, _db=db, state_directory=state.resolve()
        )

        async def scenario():
            start = execution.ExecutionSupport.start
            for identifier, expected in (
                ("unknown", "baseline_source_unknown"),
                (campaign.status.id, "baseline_source_unknown"),
                (evaluation.status.id, "baseline_source_deleted"),
            ):
                with pytest.raises(OwnerError) as caught:
                    await start(
                        owner,
                        DIGEST,
                        target_ref=None,
                        secrets={},
                        baseline_import=choice(identifier),
                    )
                assert caught.value.code == expected
            with pytest.raises(ValueError):
                await start(
                    owner,
                    DIGEST,
                    target_ref=None,
                    secrets={},
                    baseline_import=choice(evaluation.status.id),
                    linked_from=campaign.status.id,
                )

        asyncio.run(scenario())


def test_bundle_refuses_baseline_import(tmp_path, monkeypatch):
    from evolution_lab.preparation.models import PreparationError

    bundle, root, request = bundle_fixture(tmp_path, monkeypatch)
    path = root / "manifest.json"
    raw = json.loads(path.read_bytes())
    raw["baseline_import"] = {"source": "/an/evaluation"}
    path.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "baseline_import_unsupported"


@pytest.mark.parametrize("kind,control,results,journal,code", [
    ("evaluation", "finished", "present", True, None),
    ("evaluation", "released", "present", True, None),
    ("evaluation", "running", "present", True, "baseline_source_unknown"),
    ("evaluation", "stopping", "present", True, "baseline_source_unknown"),
    ("evaluation", None, "present", True, "baseline_source_unknown"),
    ("evaluation", "finished", "present", False, "baseline_source_unknown"),
    ("evaluation", "finished", "not_created", True, "baseline_source_unknown"),
    ("evaluation", "finished", "deleted", True, "baseline_source_deleted"),
    ("campaign", "finished", "present", True, "baseline_source_unknown"),
])
def test_one_source_rule_for_quickstart_and_owner(tmp_path, kind, control, results, journal, code):
    """Review M3: the quickstart and Owner.start admit a source by the same facts."""
    from evolution_lab.owner import OwnerError
    from evolution_lab.owner.linked import require_evaluation_source

    if journal:
        (tmp_path / "events.jsonl").write_text("")
    if code is None:
        require_evaluation_source(kind, control, results, tmp_path)
        return
    with pytest.raises(OwnerError) as error:
        require_evaluation_source(kind, control, results, tmp_path)
    assert error.value.code == code
