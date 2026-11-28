"""Wave B: Owner continues a suspended campaign (PDR-0015, ADR-0015)."""
import asyncio
import json
from pathlib import Path

import pytest
from scaffold_15 import scaffold_15_with_legacy_failures

from test_owner_cleanup import finished
from test_owner_target import make_bundle, ready_target
from test_owner_core_raw import finished_campaign

pytestmark = pytest.mark.slow


def _suspending(tmp_path):
    from evolution_lab.preparation import PreparationStore
    flag = tmp_path / "target-broken"
    flag.write_text("")
    # b3 probes one static-environment turn before baseline, in a separate instance.
    state, bundle, _ = make_bundle(tmp_path, mode=f"fail_after:1:{flag}",
                                   max_instances=4, launch_schema=2, scaffold=scaffold_15_with_legacy_failures)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    return state, bundle, prepared, flag


async def _replace_target(owner, bundle, old):
    task = await owner.stop_target(old)
    assert (await owner.wait(task)).state == "succeeded"
    _, target, _ = await ready_target(owner, bundle)
    assert target != old
    return target


async def _suspended_operation(owner, bundle, prepared):
    _, target, _ = await ready_target(owner, bundle)
    first = await owner.start(prepared.digest, target_ref=target, secrets={})
    from evolution_lab.owner import ResourceRef
    ref = ResourceRef("operation", first.id)
    value = await finished(owner, ref, seconds=60)
    assert value.result.core["suspended"] is True, value.to_payload()
    assert value.result.core["suspensions"][-1]["reason"] == "target_unstable"
    run = owner.state_directory / "artifacts" / ref.id / ref.id
    journal = [json.loads(line) for line in (run / "campaign.jsonl").read_text().splitlines()]
    events = [row["event"] for row in journal]
    suspension = next(i for i, row in enumerate(journal)
                      if row["event"] == "campaign_suspended"
                      and row["payload"]["reason"] == "target_unstable")
    assert events.index("connection_preflight_finished") < suspension
    return ref, target, value


def test_resume_on_another_target_finishes_the_same_campaign(tmp_path):
    from evolution.tree import inspect_campaign
    from evolution_lab.owner import Owner
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, first_target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.control_state == "finished"
            assert value.resume is not None
            assert value.resume.to_payload() == {"state": "ready", "reason": None, "target": {
                "requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
                "excluded": [first_target.to_payload()]}}
            second_target = await _replace_target(owner, bundle, first_target)
            flag.unlink()
            accepted = await owner.resume(ref, target_ref=second_target, secrets={})
            assert accepted.id == ref.id and accepted.control_state == "reserved"
            busy = await owner.status(ref)
            # The projection may already have moved past the suspension.
            assert busy.resume is None or busy.resume.state == "busy"
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished", done.to_payload()
            assert done.resume is None  # the campaign is complete
            assert [s.start for s in done.starts] == [1, 2]
            one, two = done.starts
            assert one.target_ref == first_target and two.target_ref == second_target
            assert one.settlement == "positive" and two.settlement == "positive"
            assert one.process.exit_code == 1 and two.process.exit_code == 0
            assert one.scope_id != two.scope_id
            run = state / "artifacts" / ref.id / ref.id
            final = inspect_campaign(run, verify=True).to_payload()
            assert final["complete"] is True and len(final["suspensions"]) >= 1
            # Core a24 (#78): a threshold resume gives the target's exhausted pairs one repair try
            # instead of excluding them; the champion measurement shows what the repairs did.
            champion = next(m for m in final["measurements"] if m["measurement_id"].endswith("-champion"))
            assert champion["repair_tries"] >= 1, champion
            assert champion["repairs_scored"] >= 1, champion
            excluded = champion.get("excluded_pairs") or {"by_owner": {}}
            assert excluded["by_owner"].get("target", 0) == 0, excluded
            from evolution_lab.owner import core_raw
            details = core_raw.campaign_details(run)
            assert details["journal"] != "missing"
            report = await owner.report(ref)
            assert report.state == "available"
            payload = report.to_payload()
            assert payload["schema"] == "lab-owner-report/15"
            assert [s["start"] for s in payload["starts"]] == [1, 2]
    asyncio.run(scenario())


def test_resume_refuses_a_target_the_campaign_used(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, TargetRequirement
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.result.core["suspensions"][-1]["reason"] == "target_unstable"
            assert value.resume.state == "ready"
            assert value.resume.target == TargetRequirement(
                "fresh_instance", "target_unstable_fresh_required", (target,))
            report = (await owner.report(ref)).to_payload()["report"]
            assert report["owner_resume_target"] == dict(value.resume.target.to_payload(), ready_target=None)
            flag.unlink()  # the cause is gone, but the instance is still the one that failed
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert (caught.value.code, caught.value.stage) == ("target_unstable_fresh_required", "before_start")
            after = await owner.status(ref)
            assert after.control_state == "finished" and len(after.starts) == 1  # nothing written
            fresh = await _replace_target(owner, bundle, target)
            # A stopped excluded target: the precise reason, not target_not_running.
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "target_unstable_fresh_required"
            await owner.resume(ref, target_ref=fresh, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished" and [s.target_ref for s in done.starts] == [target, fresh]
            assert done.resume is None
    asyncio.run(scenario())


def test_finished_waits_for_the_operation_runtime(tmp_path, monkeypatch):
    # The durable finish comes before the runtime's own finalization (policy use, probe notes);
    # resume refuses with resource_busy until that ends (#475). finished() covers both.
    from evolution_lab.owner import Owner
    from evolution_lab.owner.execution import OperationRuntime
    real = OperationRuntime.close_policy_use
    async def slow(self):
        await real(self)
        await asyncio.sleep(.5)
    monkeypatch.setattr(OperationRuntime, "close_policy_use", slow)
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, _, _ = await _suspended_operation(owner, bundle, prepared)
            assert owner._operations[ref.id].done.done()
    asyncio.run(scenario())


def test_accept_resume_rechecks_used_targets_in_its_transaction(tmp_path, monkeypatch):
    # A verdict read before another start used the target must not be trusted: accept_resume
    # re-checks the durable starts (spec 2026-09-29 §2.3, codex r2). The fixture campaign
    # completes on its second start, so the plan's two-start shape cannot be built; the stale
    # verdict is instead one that has not seen the first start (it excludes only some other target).
    from evolution_lab.owner import Owner, OwnerError, TargetRef, TargetRequirement, resume_rules
    from evolution_lab.owner.binding import open_prepared, declared_executor_identity
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, first, _ = await _suspended_operation(owner, bundle, prepared)  # `first` stays ready
            other = TargetRef(id="e" * 32, owner_generation=first.owner_generation)
            stale = TargetRequirement("fresh_instance", "target_unstable_fresh_required", (other,))
            monkeypatch.setattr(resume_rules, "target_requirement", lambda status, core: stale)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=first, secrets={})
            assert (caught.value.code, caught.value.stage) == ("target_unstable_fresh_required", "before_start")
            assert len((await owner.status(ref)).starts) == 1
            # The store itself, without the Owner's verdict.
            current = await owner.status(ref)
            declared = declared_executor_identity(
                open_prepared(owner._storage, current.prepared_digest).bundle, kind="campaign")
            with pytest.raises(OwnerError) as caught:
                await owner._db("accept_resume", ref.id, target_ref=first, executor=declared,
                                scope_id=None, fresh_required=True)
            assert (caught.value.code, caught.value.stage) == ("target_unstable_fresh_required", "before_start")
            assert len((await owner.status(ref)).starts) == 1
    asyncio.run(scenario())


def test_accept_resume_refuses_a_stale_observed_start(tmp_path):
    # A verdict read before another start was accepted must not be trusted (PDR-0015, codex r1):
    # the caller passes the start it observed; a differing durable start forces a re-read.
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner.binding import open_prepared, declared_executor_identity
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, first, _ = await _suspended_operation(owner, bundle, prepared)
            current = await owner.status(ref)
            observed = current.starts[-1].start
            declared = declared_executor_identity(
                open_prepared(owner._storage, current.prepared_digest).bundle, kind="campaign")
            for stale in (observed - 1, observed + 1):
                with pytest.raises(OwnerError) as caught:
                    await owner._db("accept_resume", ref.id, target_ref=first, executor=declared,
                                    scope_id=None, observed_start=stale)
                assert (caught.value.code, caught.value.stage) == ("resource_busy", "resume")
                assert len((await owner.status(ref)).starts) == observed
    asyncio.run(scenario())


def test_cleanup_required_then_cleanup_then_resume(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner import cleanup
    original = cleanup.read_campaign_settlement
    calls = {"n": 0}
    def uncertain_first(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            result["ledger"]["uncertain_any"] = True
        return result
    monkeypatch.setattr(cleanup, "read_campaign_settlement", uncertain_first)
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, first_target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.control_state == "cleanup_required"
            assert value.resume.to_payload() == {"state": "cleanup_first", "reason": "cleanup_required", "target": {
                "requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
                "excluded": [first_target.to_payload()]}}
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=first_target, secrets={})
            assert caught.value.code == "cleanup_required"
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            cleaned = await owner.status(ref)
            assert cleaned.control_state == "released"
            assert cleaned.target_cleanup.state == "confirmed"
            assert cleaned.resume.state == "ready"
            _, target, _ = await ready_target(owner, bundle)  # cleanup stopped the old one
            flag.unlink()
            await owner.resume(ref, target_ref=target, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished"
            assert done.starts[0].settlement == "not_positive"
            assert done.starts[0].target_cleanup.state == "confirmed"
            assert done.starts[0].process.exit_code == 1
            assert done.starts[0].target_ref == first_target
    asyncio.run(scenario())


def test_cleanup_repairs_a_release_whose_cleanup_fact_was_lost(tmp_path, monkeypatch):
    # Crash window: the confirmed-cleanup release commits, the Owner dies
    # before update_operation(target_cleanup=confirmed). A later cleanup must
    # repair the fact rather than report success over it.
    from evolution_lab.owner import CleanupFacts, Owner, OwnerError
    from evolution_lab.owner import cleanup
    original = cleanup.read_campaign_settlement
    calls = {"n": 0}
    def uncertain_first(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            result["ledger"]["uncertain_any"] = True
        return result
    monkeypatch.setattr(cleanup, "read_campaign_settlement", uncertain_first)
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, first_target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.control_state == "cleanup_required"
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            # Rewind the fact exactly as the crash leaves it: released, scope
            # confirmed, operation target_cleanup still unconfirmed.
            def lose_fact():
                owner._store.update_operation(
                    ref.id, target_cleanup=CleanupFacts("unconfirmed", None))
            await owner._loop.run_in_executor(owner._executor, lose_fact)
            owner._operation_cache.pop(ref.id, None)
            crashed = await owner.status(ref)
            assert crashed.control_state == "released"
            assert crashed.target_cleanup.state == "unconfirmed"
            _, target, _ = await ready_target(owner, bundle)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "cleanup_unconfirmed"
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            repaired = await owner.status(ref)
            assert repaired.control_state == "released"
            assert repaired.target_cleanup.state == "confirmed"
            assert repaired.target_cleanup.complete is True
            flag.unlink()
            await owner.resume(ref, target_ref=target, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished", done.to_payload()
            assert [s.start for s in done.starts] == [1, 2]
    asyncio.run(scenario())


def test_resume_refusals(tmp_path):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ResourceRef("operation", "f" * 32), target_ref=target, secrets={})
            assert caught.value.code == "not_found"
            # A finished, not suspended campaign.
            flag.unlink()
            fresh = await _replace_target(owner, bundle, target)
            await owner.resume(ref, target_ref=fresh, secrets={})
            assert (await finished(owner, ref, seconds=120)).control_state == "finished"
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "campaign_not_suspended"
    asyncio.run(scenario())


def test_resume_refuses_manual_release_and_deleted_results(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.owner import cleanup
    original = cleanup.read_campaign_settlement
    def uncertain(*args, **kwargs):
        result = original(*args, **kwargs)
        result["ledger"]["uncertain_any"] = True
        return result
    monkeypatch.setattr(cleanup, "read_campaign_settlement", uncertain)
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            await owner.release_after_manual_cleanup(ref, reason="checked by hand")
            released = await owner.status(ref)
            assert released.resume.to_payload() == {"state": "refused", "reason": "cleanup_unconfirmed", "target": None}
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "cleanup_unconfirmed"
            # A later cleanup never turns a manual release into a confirmed one.
            task = await owner.cleanup(ref)
            await owner.wait(task)
            assert (await owner.status(ref)).target_cleanup.state == "manual"
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "cleanup_unconfirmed"
            task = await owner.delete_results(ref)
            assert (await owner.wait(task)).state == "succeeded"
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "results_deleted"
    asyncio.run(scenario())


def test_resume_retry_after_a_pre_dispatch_failure(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner
    from evolution_lab.owner import binding
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            real = binding.revalidate_prepared
            def broken(*args, **kwargs):
                raise OSError("simulated before dispatch")
            monkeypatch.setattr(binding, "revalidate_prepared", broken)
            fresh = await _replace_target(owner, bundle, target)
            await owner.resume(ref, target_ref=fresh, secrets={})
            failed = await finished(owner, ref, seconds=30)
            assert failed.control_state == "released"
            assert failed.target_cleanup.state == "not_needed"
            # ADR-0015: the campaign result keeps the process of start 1,
            # which produced it; start 2 never launched.
            assert failed.process.launched is False
            assert failed.result.process == failed.starts[0].process
            monkeypatch.setattr(binding, "revalidate_prepared", real)
            flag.unlink()
            # The failed start used `fresh`; the requirement still excludes every used target.
            retry = await _replace_target(owner, bundle, fresh)
            await owner.resume(ref, target_ref=retry, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished"
            assert [s.start for s in done.starts] == [1, 2, 3]
            assert done.starts[1].failure is not None
            assert done.starts[1].settlement == "unknown"
            assert done.result.process == done.starts[-1].process
    asyncio.run(scenario())


def test_resume_after_owner_restart(tmp_path):
    from evolution_lab.owner import Owner
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, _, _ = await _suspended_operation(owner, bundle, prepared)
            first_generation = owner.owner_generation
        async with Owner.open(state) as owner:
            assert owner.owner_generation != first_generation
            status = await owner.status(ref)
            assert status.control_state == "finished"
            _, target, _ = await ready_target(owner, bundle)
            flag.unlink()
            await owner.resume(ref, target_ref=target, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.owner_generation == owner.owner_generation
            assert [s.owner_generation for s in done.starts] == [first_generation, owner.owner_generation]
    asyncio.run(scenario())


def test_stop_during_resume_and_delete_two_starts(tmp_path):
    from evolution_lab.owner import Owner
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            flag.unlink()
            fresh = await _replace_target(owner, bundle, target)
            await owner.resume(ref, target_ref=fresh, secrets={})
            runtime = owner._operations[ref.id]
            async with asyncio.timeout(15):
                while runtime.helper is None:
                    await asyncio.sleep(.01)
            await runtime.helper.wait_event("child_started", timeout=10)
            await owner.stop(ref)
            stopped = await finished(owner, ref, seconds=60)
            assert stopped.control_state in {"finished", "cleanup_required"}
            if stopped.control_state == "cleanup_required":
                task = await owner.cleanup(ref)
                assert (await owner.wait(task)).state == "succeeded"
            task = await owner.delete_results(ref)
            assert (await owner.wait(task)).state == "succeeded"
            assert not (state / "artifacts" / ref.id).exists()
            for start in (await owner.status(ref)).starts:
                assert not (state / "scopes" / start.scope_id / "stdout.log").exists()
            # PDR-0015: the start history survives delete in the report too.
            report = (await owner.report(ref)).to_payload()
            assert report["state"] == "unavailable"
            assert report["reason"] == "results_deleted"
            assert [s["start"] for s in report["starts"]] == [1, 2]
    asyncio.run(scenario())


async def _scope_rows(owner, operation_id):
    def read():
        return {row["id"]: (row["state"], row["cleanup_state"]) for row in
                owner._store._database.execute(
                    "SELECT id,state,cleanup_state FROM scopes WHERE operation_id=?",
                    (operation_id,))}
    return await owner._loop.run_in_executor(owner._executor, read)


def test_uncertain_second_settlement_cleanup_keeps_first_scope_then_delete(
        tmp_path, monkeypatch):
    # A stop of the resumed start never reaches the settlement read in this
    # fixture (it is cleanup_required already), so the second start runs to
    # its end and only its settlement is forced uncertain.
    from evolution_lab.owner import Owner
    from evolution_lab.owner import cleanup
    original = cleanup.read_campaign_settlement
    calls = {"n": 0}
    def uncertain_second(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:
            result["ledger"]["uncertain_any"] = True
        return result
    monkeypatch.setattr(cleanup, "read_campaign_settlement", uncertain_second)
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.control_state == "finished"
            first_scope = value.starts[0].scope_id
            before = (await _scope_rows(owner, ref.id))[first_scope]
            assert before[0] == "released"
            flag.unlink()
            fresh = await _replace_target(owner, bundle, target)
            await owner.resume(ref, target_ref=fresh, secrets={})
            stopped = await finished(owner, ref, seconds=120)
            assert stopped.control_state == "cleanup_required", stopped.to_payload()
            assert calls["n"] == 2
            second_scope = stopped.starts[1].scope_id
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            rows = await _scope_rows(owner, ref.id)
            assert rows[first_scope] == before
            assert rows[second_scope][0] == "released"
            task = await owner.delete_results(ref)
            assert (await owner.wait(task)).state == "succeeded"
            assert not (state / "artifacts" / ref.id).exists()
            for scope in (first_scope, second_scope):
                assert not (state / "scopes" / scope / "stdout.log").exists()
    asyncio.run(scenario())


def test_resume_refuses_while_a_result_lease_is_held(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            flag.unlink()
            target = await _replace_target(owner, bundle, target)  # a fresh one: the first is used
            # Early check: a read lease already held.
            lease = owner._result_io.acquire_read(ref.id)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "resource_busy"
            lease.release()
            # Late check: a lease taken after the early check, before accept_resume.
            real_target = owner.target
            held = []
            async def target_then_lease(target_ref):
                held.append(owner._result_io.acquire_read(ref.id))
                return await real_target(target_ref)
            owner.target = target_then_lease
            try:
                with pytest.raises(OwnerError) as caught:
                    await owner.resume(ref, target_ref=target, secrets={})
            finally:
                del owner.target
                for lease in held:
                    lease.release()
            assert caught.value.code == "resource_busy"
            status = await owner.status(ref)
            assert status.control_state == "finished" and len(status.starts) == 1
            await owner.resume(ref, target_ref=target, secrets={})
            assert (await finished(owner, ref, seconds=120)).control_state == "finished"
    asyncio.run(scenario())


def test_resume_refuses_a_running_resumed_start(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, _ = await _suspended_operation(owner, bundle, prepared)
            flag.unlink()
            target = await _replace_target(owner, bundle, target)  # a fresh one: the first is used
            await owner.resume(ref, target_ref=target, secrets={})
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "resource_busy"
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished"
            assert [s.start for s in done.starts] == [1, 2]
    asyncio.run(scenario())


def test_resume_refuses_a_campaign_killed_before_any_suspension(tmp_path):
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, prepared, flag = _suspending(tmp_path)
    flag.unlink()  # the first start would run to completion; it is killed instead
    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            from evolution_lab.owner import ResourceRef
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            runtime = owner._operations[operation.id]
            async with asyncio.timeout(15):
                while runtime.helper is None:
                    await asyncio.sleep(.01)
            await runtime.helper.wait_event("child_started", timeout=10)
            await runtime.helper.force()
            value = await finished(owner, ref, seconds=60)
            assert value.control_state == "cleanup_required", value.to_payload()
            task = await owner.cleanup(ref)
            assert (await owner.wait(task)).state == "succeeded"
            _, target, _ = await ready_target(owner, bundle)  # cleanup stopped the old one
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            # Killed at child_started: an absent journal is not_created (§3.2);
            # an existing unreadable journal remains unavailable. Neither can resume.
            assert (caught.value.code, caught.value.stage) == ("campaign_unreadable", "before_start")
            journal = state / "artifacts" / ref.id / ref.id / "campaign.jsonl"
            expected = "unavailable" if journal.exists() else "not_created"
            after = await owner.status(ref)
            assert after.results_state == expected, after.to_payload()
            assert after.id == operation.id and len(after.starts) == 1
    asyncio.run(scenario())


def test_resume_refuses_an_evaluation_operation(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from test_owner_evaluation import _finished, _prepared
    from test_owner_evaluation import _ready_target as _evaluation_target
    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    async def scenario():
        async with Owner.open(state) as owner:
            target = await _evaluation_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"})
            ref = ResourceRef("operation", accepted.id)
            value = await _finished(owner, ref)
            assert value.kind == "evaluation" and value.control_state == "finished"
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "invalid_argument"
    asyncio.run(scenario())


def test_resume_field_ignores_a_stale_cached_projection(tmp_path):
    # A persisted suspended projection must not advertise `ready` once the
    # fresh read of core's journal fails (ADR-0015: fresh projection).
    from evolution_lab.owner import Owner, OwnerError
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.resume.state == "ready"
            run = state / "artifacts" / ref.id / ref.id
            moved = run.with_name(run.name + "-moved")
            run.rename(moved)  # the run tree is read-only; moving it hides the journal
            stale = await owner.status(ref)
            assert stale.results_state != "present"
            assert stale.result is not None and stale.result.core["suspended"] is True
            assert stale.resume is None
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "campaign_unreadable"
            moved.rename(run)
    asyncio.run(scenario())


def test_resume_admission_raises_from_the_shared_verdict(tmp_path, monkeypatch):
    # ADR-0015 (amended 2026-09-24): admission raises from resume_verdict itself,
    # so changing the verdict changes the refusal — no second copy of row 7.
    from evolution_lab.owner import Owner, OwnerError, resume_rules
    from evolution_lab.owner.models import ResumeVerdict
    state, bundle, prepared, flag = _suspending(tmp_path)
    async def scenario():
        async with Owner.open(state) as owner:
            ref, target, value = await _suspended_operation(owner, bundle, prepared)
            assert value.resume.state == "ready"
            seen = []
            def verdict(status, core, *, refusal=False):
                assert refusal is False
                seen.append((status.id, core.get("suspended")))
                return ResumeVerdict("not_applicable", "campaign_not_suspended")
            monkeypatch.setattr(resume_rules, "resume_verdict", verdict)
            with pytest.raises(OwnerError) as caught:
                await owner.resume(ref, target_ref=target, secrets={})
            assert caught.value.code == "campaign_not_suspended"
            assert seen == [(ref.id, True)]
    asyncio.run(scenario())


@pytest.mark.parametrize('content', ['family', 'empty', 'corrupt'])
def test_owner_resume_reader_refuses_without_dispatch(tmp_path, monkeypatch, content, finished_campaign):
    import shutil
    from evolution_lab.owner import Owner, OwnerError, ResourceRef, OperationResult
    from test_owner_store_starts import _ready_target, _executor, _finish_normally, DIGEST
    source_state, source_op, source_status = finished_campaign
    source_run = source_state / 'artifacts' / source_op / source_op

    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            def settled():
                target = _ready_target(owner._store)
                value = owner._store.accept_operation(prepared_digest=DIGEST,
                    target_ref=target, executor=_executor())
                _finish_normally(owner._store, value.status.id, value.scope_id)
                owner._store.update_operation(value.status.id,
                    result=OperationResult(DIGEST, source_status.process, core=source_status.result.core),
                    results_state='present')
                return value.status.id, target
            op, target = await owner._loop.run_in_executor(owner._executor, settled)
            ref = ResourceRef('operation', op)
            run = owner.state_directory / 'artifacts' / op / op
            shutil.copytree(source_run, run)
            if content == 'family':
                shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                                run / 'campaign.jsonl')
            else:
                (run / 'campaign.jsonl').write_text('' if content == 'empty' else 'broken\n')
            before = (run / 'campaign.jsonl').read_bytes()
            async def forbidden(*a, **k):
                pytest.fail('resume dispatch reached after reader refusal')
            monkeypatch.setattr(owner, '_admit', forbidden)
            with pytest.raises(OwnerError) as raised:
                await owner.resume(ref, target_ref=target, secrets={})
            assert (raised.value.code, raised.value.stage) == ('campaign_unreadable', 'before_start')
            durable = await owner._db('operation_status', op)
            assert len(durable.starts) == 1
            assert durable.result.core == source_status.result.core
            assert (run / 'campaign.jsonl').read_bytes() == before
    asyncio.run(scenario())
