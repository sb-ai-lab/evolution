"""Core owns changed-input admission; Lab records only explicitly accepted findings."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

from test_owner_resume import _suspending, _suspended_operation, _replace_target
from test_owner_cleanup import finished
from test_owner_target import ready_target

pytestmark = pytest.mark.slow


def _changed_bundle(state, source, field):
    from evolution_lab.preparation import CampaignSource, EnvironmentInput, PreparationStore, SourceRoot
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    manifest_path, launch_path = source / "manifest.json", source / "launch.json"
    doc = json.loads(manifest_path.read_text())
    if field == "policy":
        doc["campaign"]["sample_retries"] = 2
    else:
        doc["reflector"]["max_consecutive_failures"] += 1
    manifest_path.write_text(json.dumps(doc))
    launch = json.loads(launch_path.read_text())
    prep = PreparationStore(state)
    bundle = prep.create_bundle(source=CampaignSource(manifest_path, launch_path),
        environments=(EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ()),),
        env=launch["env"], secret_names=["TARGET_TOKEN"], roots=(SourceRoot("project", source),))
    return bundle, prep.prepare_campaign(bundle.digest)


@pytest.mark.parametrize("field,effect", [("policy", "rebaseline"), ("adaptation", None)])
def test_tolerance_change_reaches_core_then_accepts(tmp_path, field, effect):
    from evolution.tree import read_decisions
    from evolution_lab.owner import Owner, Accepted, OwnerError
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            changed, next_prepared = _changed_bundle(state, tmp_path / "source", field)
            target = await _replace_target(owner, changed, target)
            await owner.resume(ref, target_ref=target, secrets={}, prepared=next_prepared.digest)
            refused = await finished(owner, ref, seconds=60)
            assert refused.failure.code == "core_refused", refused.to_payload()
            record = await owner.refusal(ref)
            finding = next(f for f in record.findings if f.code == "run_identity_changed")
            assert any(field in text for text in finding.differs), finding.to_payload()
            assert effect is None or effect in finding.effects, json.dumps(finding.to_payload())
            chosen = Accepted(finding.code, finding.instance_digest, effect)
            run = state / "artifacts" / ref.id / ref.id
            for items, reason, code in [([chosen], None, "authorization_reason_required"),
                    ([Accepted(chosen.code, "f" * 64, None)], "repair", "authorization_unknown_finding"),
                    ([Accepted(chosen.code, chosen.digest, "not-an-effect")], "repair", "authorization_effect_invalid")]:
                with pytest.raises(OwnerError) as error:
                    await owner.resume(ref, target_ref=target, secrets={}, accept=items, reason=reason)
                assert error.value.code == code
                assert not read_decisions(run)
            if refused.control_state == "cleanup_required":
                assert (await owner.wait(await owner.cleanup(ref))).state == "succeeded"
                _, target, _ = await ready_target(owner, changed)
            flag.unlink()
            await owner.resume(ref, target_ref=target, secrets={}, accept=[chosen], reason="repair")
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, json.dumps((await owner.refusal(ref)).to_payload())
            assert done.result.core["complete"] is True
            decisions = read_decisions(run)
            assert len(decisions) == 1
            assert decisions[0].code == chosen.code and decisions[0].digest == chosen.digest
            assert decisions[0].reason == "repair" and decisions[0].effect == (effect or "default")
            history = await owner.inputs(ref)
            assert [s["prepared_digest"] for s in history["starts"]] == [prepared.digest, next_prepared.digest, next_prepared.digest]
            assert history["starts"][0]["bundle_digest"] == bundle.digest
            markdown = (await owner.report_markdown(ref)).markdown
            assert prepared.digest in markdown and next_prepared.digest in markdown
    asyncio.run(scenario())


def test_first_refusal_without_journal_restarts_the_same_run(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, Accepted, ResourceRef
    from evolution_lab.owner import execution
    from test_owner_target import make_bundle
    from evolution_lab.preparation import PreparationStore
    state, bundle, _ = make_bundle(tmp_path)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    original = execution.campaign_worker_command
    # Inject only the Core admission result at the subprocess boundary; keep
    # the real worker, diagnostics, cleanup, store and subsequent evolve call.
    def refused_worker(python):
        return [str(python), "-c", "import evolution.tree.evolve as e; "
                "from evolution.tree import EvolveRefused; "
                "e.admit_manifest=lambda p: (_ for _ in ()).throw(EvolveRefused("
                "'run_dir_unsafe_entry', 'scorer-traces', instance_digest='d'*64, effect='hygiene')); "
                "from evolution_lab.owner._campaign_worker import main; main()"]
    monkeypatch.setattr(execution, "campaign_worker_command", refused_worker)
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", first.id)
            refused = await finished(owner, ref, seconds=60)
            assert refused.failure.code == "core_refused"
            run = state / "artifacts" / ref.id / ref.id
            assert not (run / "campaign.jsonl").exists()
            assert refused.resume is None  # /8 remains Core-suspension-only
            finding = (await owner.refusal(ref)).findings[0]
            assert (await owner.wait(await owner.cleanup(ref))).state == "succeeded"
            _, target, _ = await ready_target(owner, bundle)
            monkeypatch.setattr(execution, "campaign_worker_command", original)
            await owner.resume(ref, target_ref=target, secrets={},
                accept=[Accepted(finding.code, finding.instance_digest, None)], reason="owned output")
            done = await finished(owner, ref, seconds=120)
            assert done.failure is None, json.dumps((await owner.refusal(ref)).to_payload())
            assert (run / "campaign.jsonl").is_file()
            assert len(done.starts) == 2 and done.id == first.id
    asyncio.run(scenario())


@pytest.mark.parametrize("code,chosen,offered,written", [
    ("run_identity_changed", None, ("rebase",), "default"),
    ("run_identity_changed", None, ("continue", "rebaseline", "close"), "default"),
    ("run_identity_changed", "close", ("continue", "rebaseline", "close"), "close"),
    ("baseline_import_incompatible", None, ("import",), None),
    ("baseline_import_unavailable", None, ("fresh_baseline",), None),
    ("baseline_import_unavailable", None, ("terminal", "close"), "default"),
    ("baseline_import_unavailable", "close", ("terminal", "close"), "close"),
])
def test_decision_effect_writes_what_core_resolves_the_same(code, chosen, offered, written):
    """Omitting the effect only where Core resolves None exactly like "default"."""
    from evolution.tree import Authorization
    from evolution_lab.owner.execution import decision_effect

    assert decision_effect(code, chosen, offered) == written
    if chosen is None:
        resolved = Authorization.new(code, "f" * 64, "r", effect=written).resolved_effect(offered)
        assert resolved == offered[0]
