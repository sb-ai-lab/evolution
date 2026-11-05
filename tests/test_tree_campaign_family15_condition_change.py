"""Family 15 applies authorized condition transitions by the ordinary rule."""

from dataclasses import replace

import pytest

import evolution
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import _journaled_docs_digest, evolve
from evolution.tree.roles.environment_docs import _v2_load_document_snapshot
from tests.campaign_drivers import _records
from tests.family15_fakes import family15_admitted, independent_run

pytestmark = pytest.mark.slow


def no_provider(*args, **kwargs):
    pytest.fail("recovery of an owed terminal prepared a provider")


async def _interrupted(tmp_path, monkeypatch, adapter, cut):
    """Leave a two-round run unfinished right after `cut` in round 1."""
    from evolution.tree import campaign
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    original = campaign._Campaign.hook

    def interrupt(self, name):
        original(self, name)
        if name == cut and (
            cut != "after_global_comparison_completed"
            or all(
                a.family15.global_result is not None
                for a in self.snapshot().rounds[1].attempts.values()
            )
        ):
            raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(campaign._Campaign, "hook", interrupt)
        with pytest.raises(GatePowerCut):
            await independent_run(tmp_path, patch, adapter=adapter, campaign_options={"rounds": 2})
    family15_admitted(monkeypatch)
    return tmp_path / "run"


async def _condition_finding(path, run, adapter):
    """Change the target's seed mode and return the authentic identity finding."""
    from evolution.tree.evolve import EvolveRefused

    adapter.seed_mode = "unpaired"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter)
    return refused.value.instance_digest


async def _held_change_authorizations(path, run, adapter, *, effect, hygiene=False):
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization

    condition = Authorization.new(
        "run_identity_changed",
        await _condition_finding(path, run, adapter),
        "Changed target",
        now="2026-10-19T10:00:00Z",
        effect=effect,
    )
    if not hygiene:
        return [condition]
    (run / "scorer-traces").chmod(0o750)
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter, authorizations=[condition])
    (finding,) = refused.value.findings
    assert finding.code == "run_dir_unsafe_entry"
    assert finding.path == "scorer-traces"
    accepted_hygiene = Authorization.new(
        finding.code,
        finding.instance_digest,
        "Accept group-readable scorer traces",
        now="2026-10-19T10:00:00Z",
    )
    return [condition, accepted_hygiene]


async def test_hygiene_accepted_with_a_held_change_is_journaled_after_the_owed_closure(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    authorizations = await _held_change_authorizations(
        path, run, adapter, effect="continue", hygiene=True
    )
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=authorizations
    )
    assert result.terminal == "finished"
    assert result.finished["reason"] == "rounds_exhausted"
    records = _records(run / "campaign.jsonl")
    closed = next(i for i, r in enumerate(records) if r["event"] == "round_closed")
    hygiene, condition = (
        next(
            i
            for i, r in enumerate(records)
            if r["event"] == "authorization_applied"
            and r["payload"]["authorization"]["id"] == authorization.id
        )
        for authorization in reversed(authorizations)
    )
    assert closed < hygiene < condition
    assert verify_evidence(run)["mismatches"] == []


async def test_cancellation_over_a_durable_selection_settles_the_round_then_stops(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    result = await evolve(
        tmp_path / "manifest.json",
        run,
        adapter_factory=lambda *a: adapter,
        cancelled=lambda: True,
    )
    assert result.finished["outcome"] == "stopped"
    assert result.finished["reason"] == "cancelled"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert names.index("round_closed") < names.index("campaign_finished") == len(names) - 1
    assert names.count("campaign_finished") == 1
    assert verify_evidence(run)["mismatches"] == []


async def test_cancellation_during_owed_closure_remains_latched_when_the_token_clears(
    tmp_path, monkeypatch
):
    from evolution.tree import campaign
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    requested = True
    original_hook = campaign._Campaign.hook

    def clear_cancellation(self, name):
        nonlocal requested
        original_hook(self, name)
        if name == "after_round_closed":
            requested = False

    monkeypatch.setattr(campaign._Campaign, "hook", clear_cancellation)
    result = await evolve(
        tmp_path / "manifest.json",
        run,
        adapter_factory=lambda *a: adapter,
        cancelled=lambda: requested,
    )
    assert not requested
    assert result.finished["outcome"] == "stopped"
    assert result.finished["reason"] == "cancelled"
    names = [record["event"] for record in _records(run / "campaign.jsonl")]
    assert names.index("round_closed") < names.index("campaign_finished") == len(names) - 1
    assert names.count("round_closed") == names.count("campaign_finished") == 1
    assert verify_evidence(run)["mismatches"] == []


async def test_cancellation_with_a_held_continue_applies_the_decision_then_stops(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    authorizations = await _held_change_authorizations(path, run, adapter, effect="continue")
    result = await evolve(
        path,
        run,
        adapter_factory=lambda *a: adapter,
        authorizations=authorizations,
        cancelled=lambda: True,
    )
    assert result.finished["outcome"] == "stopped"
    assert result.finished["reason"] == "cancelled"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert (
        names.index("round_closed")
        < names.index("authorization_applied")
        < names.index("campaign_finished")
        == len(names) - 1
    )
    applied = records[names.index("authorization_applied")]["payload"]
    assert applied["effect"] == "continue"
    assert applied["authorization"]["id"] == authorizations[0].id
    assert names.count("authorization_applied") == names.count("campaign_finished") == 1
    assert verify_evidence(run)["mismatches"] == []


async def test_a_non_comparable_rebase_over_a_durable_selection_waits_for_the_closure(
    tmp_path, monkeypatch
):
    from evolution.tree import campaign
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    monkeypatch.setattr(campaign, "resolve_package_version", lambda: "2.0.0b99")
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter)
    assert refused.value.effects == ("rebase",)
    assert [difference.field for difference in refused.value.differences] == ["producing_version"]
    authorization = Authorization.new(
        "run_identity_changed",
        refused.value.instance_digest,
        "Changed producing version",
        now="2026-10-19T10:00:00Z",
    )
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    assert result.terminal == "finished"
    assert result.finished["reason"] == "rounds_exhausted"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert names.index("round_closed") < names.index("identity_rebased")
    assert records[names.index("identity_rebased")]["payload"]["changed_fields"] == [
        "producing_version"
    ]
    assert verify_evidence(run)["mismatches"] == []


async def _docs_repair_over_a_durable_selection(tmp_path, monkeypatch, *, effect, keep_copy=False):
    """Resume a durable selection with lost stored docs and an accepted docs change.

    With `keep_copy` the stored docs stay intact and the repair is reported
    regardless. Return the run, the result and, per documentation re-take of
    the resume, the journal's events at that moment.
    """

    import shutil

    from evolution.tree import campaign
    from evolution.tree import evolve as evolve_module
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests import campaign_drivers as fixtures
    from tests.campaign_drivers import _Resettable

    workspace = fixtures.write_workspace

    def with_documents(*args, **kwargs):
        return workspace(*args, **{**kwargs, "environment_docs": {"api.md": "v1\n"}})

    monkeypatch.setattr(fixtures, "write_workspace", with_documents)
    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    if not keep_copy:
        shutil.rmtree(run / "environment-docs")
    (tmp_path / "env-docs" / "api.md").write_text("v2\n")
    if effect == "rebase":
        with pytest.raises(EvolveRefused) as refused:
            await evolve(path, run, adapter_factory=lambda *a: adapter)
        assert refused.value.effects == ("rebase",)
        instance = refused.value.instance_digest
    else:
        # A comparable target change beside it, so the decision is a held one.
        instance = await _condition_finding(path, run, adapter)
    authorization = Authorization.new(
        "run_identity_changed",
        instance,
        "Changed docs",
        now="2026-10-19T10:00:00Z",
        **({} if effect == "rebase" else {"effect": effect}),
    )
    # `evolve` reports a repair only while the live digest is the journaled
    # one; a library caller may report both, which is what this models.
    original_docs = evolve_module._environment_docs

    def lost_copy(workflow, run_dir, state):
        docs, repair = original_docs(workflow, run_dir, state)
        return docs, repair or keep_copy or not (run_dir / "environment-docs").exists()

    monkeypatch.setattr(evolve_module, "_environment_docs", lost_copy)
    retakes = []
    original_retake = campaign._Campaign._retake_environment_docs

    def retake(self):
        retakes.append([r["event"] for r in _records(run / "campaign.jsonl")])
        original_retake(self)

    monkeypatch.setattr(campaign._Campaign, "_retake_environment_docs", retake)
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    return run, result, retakes


@pytest.mark.parametrize("effect", ["rebase", "rebaseline"])
async def test_a_docs_repair_waits_for_a_deferred_rebase(tmp_path, monkeypatch, effect):
    """#278 F3: the repair follows the `identity_rebased` its re-take belongs to.

    A `rebase` waits in the deferred consequences and a `rebaseline` is held
    until the selected round closes; neither may let the immediate repair
    re-take the new documentation before the record that names its digest.
    """

    run, result, retakes = await _docs_repair_over_a_durable_selection(
        tmp_path, monkeypatch, effect=effect
    )
    assert result.terminal == "finished"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    rebased = names.index("identity_rebased")
    assert names.index("round_closed") < rebased
    changed = records[rebased]["payload"]["changed_fields"]
    assert "identity.reflector.environment_docs_digest" in changed
    assert retakes, "the stored documentation was never re-taken"
    assert all("identity_rebased" in events for events in retakes)
    assert len(retakes) == 1
    assert (run / "environment-docs" / "api.md").read_text() == "v2\n"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("keep_copy", [False, True], ids=["lost", "intact"])
async def test_a_held_close_with_a_changed_docs_digest_does_not_retake_the_docs(
    tmp_path, monkeypatch, keep_copy
):
    """ADR-0012, #278 F3: a repair uses only bytes matching the journaled identity.

    A held `close` declares no `identity_rebased`, so the journal keeps the
    old documentation digest; re-taking the changed live documentation after
    `campaign_finished` would store a snapshot no record names.
    """

    run, result, retakes = await _docs_repair_over_a_durable_selection(
        tmp_path, monkeypatch, effect="close", keep_copy=keep_copy
    )
    assert result.terminal == "finished"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert names[-1] == "campaign_finished"
    assert records[-1]["payload"]["reason"] == "condition_changed"
    assert "identity_rebased" not in names
    (applied,) = [r for r in records if r["event"] == "authorization_applied"]
    assert applied["payload"]["effect"] == "close"
    assert retakes == []
    # The stored copy is left as the resume found it, never replaced by bytes
    # whose digest the journal does not name: an intact copy still loads
    # against the journaled digest.
    if not keep_copy:
        assert not (run / "environment-docs").exists()
        return
    journaled = _journaled_docs_digest(run)
    assert journaled is not None
    stored = _v2_load_document_snapshot(run, expected_digest=journaled)
    assert stored.digest == journaled
    assert (run / "environment-docs" / "api.md").read_text() == "v1\n"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("effect", ["none", "rebaseline", "continue", "close"])
async def test_a_docs_repair_with_an_unchanged_digest_follows_the_round_boundary(
    tmp_path, monkeypatch, effect
):
    """#278 F3: a lost docs copy is re-taken once, after the owed closure's records.

    The documentation digest does not move, so no `identity_rebased` re-takes
    the copy on the repair's behalf: only the repair that `adopt` defers
    behind the owed closure (no authorization) or hands to the held
    decision's block restores it.
    """

    import shutil

    from evolution.tree import campaign
    from evolution.tree.execution.authorizations import Authorization
    from tests import campaign_drivers as fixtures
    from tests.campaign_drivers import _Resettable

    workspace = fixtures.write_workspace

    def with_documents(*args, **kwargs):
        return workspace(*args, **{**kwargs, "environment_docs": {"api.md": "v1\n"}})

    monkeypatch.setattr(fixtures, "write_workspace", with_documents)
    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    shutil.rmtree(run / "environment-docs")
    authorizations = []
    if effect != "none":
        authorizations.append(
            Authorization.new(
                "run_identity_changed",
                await _condition_finding(path, run, adapter),
                "Changed target",
                now="2026-10-19T10:00:00Z",
                effect=effect,
            )
        )
    retakes = []
    original_retake = campaign._Campaign._retake_environment_docs

    def retake(self):
        retakes.append([r["event"] for r in _records(run / "campaign.jsonl")])
        original_retake(self)

    monkeypatch.setattr(campaign._Campaign, "_retake_environment_docs", retake)
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=authorizations
    )
    assert result.terminal == "finished"
    assert len(retakes) == 1, retakes
    (events,) = retakes
    block = {
        "none": [],
        "rebaseline": ["authorization_applied", "identity_rebased"],
        "continue": ["authorization_applied", "identity_rebased"],
        "close": ["authorization_applied", "campaign_finished"],
    }[effect]
    assert events[-1 - len(block) :] == ["round_closed", *block]
    assert events.count("round_closed") == 1
    if effect == "close":
        assert events == [r["event"] for r in _records(run / "campaign.jsonl")]
    assert (run / "environment-docs" / "api.md").read_text() == "v1\n"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
async def test_cancellation_with_hygiene_and_a_held_change_settles_all_consequences_in_order(
    tmp_path, monkeypatch, effect
):
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    authorizations = await _held_change_authorizations(
        path, run, adapter, effect=effect, hygiene=True
    )
    result = await evolve(
        path,
        run,
        adapter_factory=lambda *a: adapter,
        authorizations=authorizations,
        cancelled=lambda: True,
    )
    assert result.finished["outcome"] == "stopped"
    assert result.finished["reason"] == "cancelled"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    closed = names.index("round_closed")
    hygiene, condition = (
        next(
            i
            for i, r in enumerate(records)
            if r["event"] == "authorization_applied"
            and r["payload"]["authorization"]["id"] == authorization.id
        )
        for authorization in reversed(authorizations)
    )
    assert closed < hygiene < condition < names.index("campaign_finished") == len(names) - 1
    assert records[hygiene]["payload"]["code"] == "run_dir_unsafe_entry"
    assert records[condition]["payload"]["effect"] == effect
    assert names.count("authorization_applied") == 2
    assert names.count("campaign_finished") == 1
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize(
    "cut,effect",
    [
        ("after_global_comparison_completed", "close"),
        ("after_attempt_opened", "rebaseline"),
        ("after_comparison_completed", "rebaseline"),
        ("after_global_comparison_completed", "rebaseline"),
        ("after_selection_decided", "rebaseline"),
        ("after_selection_decided", "close"),
        ("after_selection_decided", "continue"),
    ],
)
async def test_authorized_transition_on_a_settled_round_follows_the_ordinary_rule(
    tmp_path, monkeypatch, effect, cut
):
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, cut)
    path = tmp_path / "manifest.json"
    authorization = Authorization.new(
        "run_identity_changed",
        await _condition_finding(path, run, adapter),
        "Changed target",
        now="2026-10-19T10:00:00Z",
        effect=effect,
    )
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    applied = names.index("authorization_applied")
    first_round = [r for r in records[:applied] if r["payload"].get("round") == 1]
    if cut == "after_selection_decided":
        # A durable selection completes its promotion before the transition.
        assert [r["event"] for r in first_round].count("selection_decided") == 1
        closed = next(r for r in first_round if r["event"] == "round_closed")
        assert closed["payload"]["outcome"] == "promoted"
    else:
        # Settled eligible evidence owes nothing: no selection precedes the transition.
        assert not any(r["event"] == "selection_decided" for r in first_round)
        assert not any(
            r["event"] == "selection_decided" and r["payload"]["round"] == 1
            for r in records[applied:]
        )
    if effect == "close":
        assert result.terminal == "finished"
        assert result.finished["reason"] == "condition_changed"
        assert names.count("campaign_finished") == 1
    if effect == "continue":
        from evolution.tree.campaign_projections import inspect_view

        # #263: the held block is written at the round boundary, so it finds
        # no open round and round 2 opens after it, under the new epoch.
        rebased = names.index("identity_rebased")
        opened = next(i for i, r in enumerate(records) if r["payload"].get("round") == 2)
        assert applied < rebased < opened
        (change,) = inspect_view(run).snapshot.condition_changes
        assert change["effect"] == "continue"
        assert change["round"] is None
    if effect == "rebaseline" and cut != "after_selection_decided":
        # The open round closes under the old condition with family 15's full closure.
        closed = next(
            r
            for r in records[applied:]
            if r["event"] == "round_closed" and r["payload"]["round"] == 1
        )
        assert closed["payload"]["outcome"] == "condition_changed"
        assert "attempts_opened" in closed["payload"]
        assert result.terminal == "finished"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("effect", ["close", "continue"])
async def test_a_crash_after_the_held_round_closes_leaves_the_decision_unconsumed(
    tmp_path, monkeypatch, effect
):
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _cut_hook, _Resettable
    from tests.campaign_fakes import Crash

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    path = tmp_path / "manifest.json"
    authorization = Authorization.new(
        "run_identity_changed",
        await _condition_finding(path, run, adapter),
        "Changed target",
        now="2026-10-19T10:00:00Z",
        effect=effect,
    )
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await evolve(
                path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
            )
    names = [r["event"] for r in _records(run / "campaign.jsonl")]
    assert "authorization_applied" not in names  # unconsumed
    assert names[-1] == "round_closed"
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert names.count("authorization_applied") == 1
    # Round 1's selection is not repeated; a `continue` may select again in
    # round 2, under the new condition.
    assert (
        sum(1 for r in records if r["event"] == "selection_decided" and r["payload"]["round"] == 1)
        == 1
    )
    if effect == "close":
        assert result.terminal == "finished"
        assert result.finished["reason"] == "condition_changed"
        assert names.count("campaign_finished") == 1
    assert verify_evidence(run)["mismatches"] == []


async def _close_under_recorded_store_identity(tmp_path, monkeypatch, *, target_change):
    import json

    from evolution.tree.artifacts.artifact_ledger import ArtifactLedger
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization, identity_instance_digest
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_selection_decided")
    recorded_stamp = ArtifactLedger.latest_store_record(ArtifactLedger.inspect(run))
    path = tmp_path / "manifest.json"
    document = json.loads(path.read_bytes())
    document.setdefault("artifacts", {}).setdefault("excludes", []).append("probe/**")
    path.write_text(json.dumps(document))
    if target_change:
        adapter.seed_mode = "unpaired"
        adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter)
    finding = refused.value
    assert finding.code == "run_identity_changed"
    assert finding.effects == ("close",)
    changed = {difference.field.split(".")[1] for difference in finding.differences}
    assert changed == ({"artifact_store", "target"} if target_change else {"artifact_store"})
    authorization = Authorization.new(
        "run_identity_changed",
        finding.instance_digest,
        "Changed target and artifact policy" if target_change else "Changed artifact policy",
        now="2026-10-19T10:00:00Z",
        effect="close",
    )
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    assert result.terminal == "finished"
    assert result.finished["outcome"] == "stopped"
    assert result.finished["reason"] == "condition_changed"
    records = _records(run / "campaign.jsonl")
    names = [r["event"] for r in records]
    assert (
        names.index("round_closed")
        < names.index("authorization_applied")
        < names.index("campaign_finished")
        == len(names) - 1
    )
    assert names.count("selection_decided") == names.count("round_closed") == 1
    assert names.count("authorization_applied") == names.count("campaign_finished") == 1
    assert records[names.index("round_closed")]["payload"]["outcome"] == "promoted"
    applied = records[names.index("authorization_applied")]["payload"]
    assert applied["effect"] == "close"
    assert applied["authorization"]["id"] == authorization.id
    assert applied["instance_digest"] == finding.instance_digest
    # The observed triple must still bind every difference in the authentic
    # finding, even though settlement itself uses the recorded store identity.
    assert identity_instance_digest(applied["journaled"], applied["observed"]) == (
        finding.instance_digest
    )
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    assert not any(r["event"] == "store_rebased" for r in ledger)
    assert ArtifactLedger.latest_store_record(ArtifactLedger.inspect(run)) == recorded_stamp
    assert verify_evidence(run)["mismatches"] == []


async def test_a_close_over_a_durable_selection_settles_under_the_recorded_store_identity(
    tmp_path, monkeypatch
):
    await _close_under_recorded_store_identity(tmp_path, monkeypatch, target_change=False)


async def test_a_close_with_a_target_and_store_change_records_both_differences(
    tmp_path, monkeypatch
):
    await _close_under_recorded_store_identity(tmp_path, monkeypatch, target_change=True)


async def test_already_applied_close_recovers_terminal_before_provider(tmp_path, monkeypatch):
    from evolution.tree import campaign
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _Resettable
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_round_closed")
    path = tmp_path / "manifest.json"
    authorization = Authorization.new(
        "run_identity_changed",
        await _condition_finding(path, run, adapter),
        "Close old condition",
        now="2026-10-19T10:00:00Z",
        effect="close",
    )
    append = campaign._Campaign.append

    def cut(self, event, payload, *args, **kwargs):
        result = append(self, event, payload, *args, **kwargs)
        if event == "authorization_applied" and payload.get("effect") == "close":
            raise GatePowerCut()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(campaign._Campaign, "append", cut)
        with pytest.raises(GatePowerCut):
            await evolve(
                path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
            )
    assert _records(run / "campaign.jsonl")[-1]["event"] == "authorization_applied"
    result = await evolve(path, run, adapter_factory=no_provider)
    assert result.terminal == "finished"
    assert result.finished["reason"] == "condition_changed"


# -- #282: the b9 -> b10 `/16` resume (R11) -------------------------------------------------


def _as_v16(monkeypatch):
    """Turn the family-14 driver's `/15` document into `/16` (family 15)."""
    from tests import test_tree_campaign_family14_run as driver
    from tests.family15_fakes import v16_document

    document = driver.family14_document

    def patched(*args, **kwargs):
        moved = document(*args, **kwargs)
        moved["schema"] = "evolution-v2-manifest/16"
        moved["campaign"]["gate"] = v16_document()["campaign"]["gate"]
        return moved

    monkeypatch.setattr(driver, "family14_document", patched)


def _as_b9(patch):
    """Create the run as `2.0.0b9` did: its `/16` adaptation identity bound no
    `reflection.group_text_chars`, so the journal means 512 (R2)."""
    from evolution.tree import evolve as evolve_module
    from tests.test_tree_campaign_family14_visibility_change import _version

    original = evolve_module.build_adaptation_identity

    def b9(*args, reflection=None, **kwargs):
        if reflection is not None:
            reflection = {k: v for k, v in reflection.items() if k != "group_text_chars"}
        return original(*args, reflection=reflection, **kwargs)

    patch.setattr(evolve_module, "build_adaptation_identity", b9)
    _version(patch, "2.0.0b9")


async def _b9_run_resumed(tmp_path, monkeypatch, declared):
    """A two-round `/16` chat-Reflector run created by b9 and cut after round 1
    closes, then resumed by the current release with `declared` as
    `reflector.group_text_chars`.
    Return the refusal, the request limits seen, the server and a resume."""
    from evolution.tree.evolve import EvolveRefused
    from tests import fake_chat_server
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_reflector_history import (
        NONPROMOTING,
        chat_reflector,
        text_limit_reply,
        with_chat_reflector,
        with_text_limit,
    )
    from tests.test_tree_campaign_family14_run import family14_run
    from tests.test_tree_campaign_family14_visibility_change import _version

    seen = []
    # 512 characters in the b9 round (its limit), 900 in a round at 4,096.
    base_url, server = fake_chat_server.start(
        [text_limit_reply(lambda limit: 512 if limit == 512 else 900, seen)]
    )
    with_chat_reflector(monkeypatch, chat_reflector(base_url))
    with_text_limit(monkeypatch, declared)
    _as_v16(monkeypatch)
    config = {**NONPROMOTING, "repeats": 1}
    options = {"campaign": {"rounds": 2}}
    with monkeypatch.context() as patch:
        _as_b9(patch)
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    # The current release, not a fixed one: the artifact store stamps the
    # installed version, so a pinned string breaks at the next version bump.
    _version(monkeypatch, evolution.__version__)
    with pytest.raises(EvolveRefused) as caught:
        await family14_run(tmp_path, monkeypatch, config, **options)

    async def resume(decision):
        return await family14_run(
            tmp_path, monkeypatch, config, authorizations=[decision], **options
        )

    return caught.value, seen, server, resume


def _stored_root_causes(run):
    from tests.test_tree_campaign_family14_run import payloads

    return {
        p["reflection_id"][:3]: len(p["groups"][0]["root_cause"])
        for p in payloads(run, "reflection_finished")
    }


async def test_a_b9_family15_run_without_the_key_rebases_once(tmp_path, monkeypatch):
    """R11: one finding (`producing_version` and `adaptation`), one
    `--accept`, one `identity_rebased`; round 2 runs at 4,096 and round 1's
    512-era groups replay."""
    from tests.test_tree_campaign_family14_run import payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision

    refused, seen, server, resume = await _b9_run_resumed(tmp_path, monkeypatch, {})
    try:
        assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
        fields = [difference.field for difference in refused.differences]
        assert sorted(fields) == ["adaptation", "producing_version"]
        assert seen and set(seen) == {512}
        decision = _decision(refused, None, reason="accept this release and its 4096 limit")
        run, snapshot = await resume(decision)
    finally:
        server.shutdown()
        server.server_close()
    assert snapshot.finished is not None
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == fields
    assert [snapshot.round_group_text_chars(number) for number in (1, 2)] == [512, 4096]
    assert [value for i, value in enumerate(seen) if i == 0 or seen[i - 1] != value] == [512, 4096]
    assert _stored_root_causes(run) == {"r01": 512, "r02": 900}
    assert verify_evidence(run)["mismatches"] == []


async def test_declaring_512_keeps_a_b9_family15_run_at_512_but_still_rebases_once(
    tmp_path, monkeypatch
):
    """Declaring `512` keeps a b9 run's limit: both rounds run at 512.

    ADR-0047 emits a declared key into the adaptation identity, and the b9
    journal has none, so the finding still names `adaptation` beside
    `producing_version`; one `--accept` covers both, as without the key."""
    from tests.test_tree_campaign_family14_run import payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision

    refused, seen, server, resume = await _b9_run_resumed(tmp_path, monkeypatch, {"limit": 512})
    try:
        assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
        fields = [difference.field for difference in refused.differences]
        assert sorted(fields) == ["adaptation", "producing_version"]
        run, snapshot = await resume(_decision(refused, None, reason="accept this release at 512"))
    finally:
        server.shutdown()
        server.server_close()
    assert snapshot.finished is not None
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == fields
    assert [snapshot.round_group_text_chars(number) for number in (1, 2)] == [512, 512]
    assert set(seen) == {512}
    assert _stored_root_causes(run) == {"r01": 512, "r02": 512}
    assert verify_evidence(run)["mismatches"] == []


async def _faulted_over_a_durable_selection(tmp_path, monkeypatch):
    """Fill the artifact ledger at round 1's promotion: the selection is durable, its closure owed."""
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from evolution.tree.storage.journal_errors import JournalFault
    from tests.campaign_drivers import _Resettable

    def full(self, *args, **kwargs):
        raise JournalFault("storage_capacity_exhausted")

    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "promote", full)
        return await independent_run(
            tmp_path, patch, adapter=_Resettable(), campaign_options={"rounds": 2}
        )


async def test_finish_calls_write_terminal_once(tmp_path, monkeypatch):
    # #278 F4: a classified fault that leaves the closure owed takes the same
    # single `write_terminal` call as every other exit of `_finish`; the fault
    # branch settles it before the owed-closure invariant is consulted.
    from evolution.tree import campaign

    calls = []
    write_terminal = campaign._Campaign.write_terminal

    def spy(self):
        calls.append(self._owed_closure())
        write_terminal(self)

    monkeypatch.setattr(campaign._Campaign, "write_terminal", spy)
    run, result = await _faulted_over_a_durable_selection(tmp_path, monkeypatch)
    assert (result.terminal, result.owner, result.code) == (
        "storage_capacity_exhausted",
        "artifact_store",
        "storage_capacity_exhausted",
    )
    assert result.finished is None and result.suspended is None
    names = [r["event"] for r in _records(run / "campaign.jsonl")]
    # The nonterminal capacity fact follows the selection; no closure, no terminal.
    assert names[-2:] == ["selection_decided", "capacity_exhausted"]
    assert "campaign_finished" not in names
    assert names.count("round_closed") == 0
    assert calls == [True]


async def test_a_connection_suspension_cannot_follow_a_selection_owing_its_closure(
    tmp_path, monkeypatch
):
    # #278 F4: `write_terminal` now returns on `connection_suspended` before it
    # consults the owed closure. That order changes no terminal, because the
    # flag is set only once `campaign_suspended{owner: connection}` is durable
    # (`_connection_stop`), and family 15 refuses that record -- and the
    # preflight start that would lead to it -- while a selection owes its round
    # closure. Recovery's owed suspension cannot be pending there either: a
    # failed preflight admits only its suspension next, never a selection.
    from evolution.tree import campaign
    from evolution.tree.campaign_schema import CONNECTION_START_EVENT
    from tests.campaign_drivers import _Resettable
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    refusals = []
    original = campaign._Campaign.hook

    def offer(self, name):
        original(self, name)
        if name != "after_selection_decided":
            return
        records = _records(self.run_root / "campaign.jsonl")
        started = [r for r in records if r["event"] == CONNECTION_START_EVENT][-1]
        fresh = "f" * 32
        offers = [
            (CONNECTION_START_EVENT, {**started["payload"], "operation_id": fresh}, fresh),
            (
                "campaign_suspended",
                dict(
                    reason="connection_preflight_unavailable",
                    owner="connection",
                    evidence=dict(
                        evidence_kind="stage_record",
                        seq=started["sequence"],
                        operation_id=started["operation_id"],
                    ),
                ),
                None,
            ),
        ]
        for event, payload, operation_id in offers:
            with pytest.raises(campaign.CampaignInvariantViolated) as refused:
                self.prove(event, payload, operation_id)
            refusals.append((event, refused.value.rule))
        raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(campaign._Campaign, "hook", offer)
        with pytest.raises(GatePowerCut):
            await independent_run(
                tmp_path, patch, adapter=_Resettable(), campaign_options={"rounds": 2}
            )
    assert refusals == [
        (CONNECTION_START_EVENT, "selection owes its exact round closure"),
        ("campaign_suspended", "selection owes its exact round closure"),
    ]


@pytest.mark.parametrize("initial", [None, "focus_first"])
@pytest.mark.parametrize("mid_stage", [False, True])
async def test_stage_policy_change_requires_new_conditions(
    tmp_path, monkeypatch, initial, mid_stage
):
    import json

    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _Resettable
    from tests.test_tree_campaign_family15_gate import GatePowerCut, gate_run

    original = _Campaign._append_stage_opened

    def cut(self, mid, plan):
        original(self, mid, plan)
        if mid_stage and self.snapshot().measurements[mid].get("role") == "candidate":
            raise GatePowerCut()

    adapter = _Resettable()
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "_append_stage_opened", cut)
        run = await gate_run(
            tmp_path,
            patch,
            adapter=adapter,
            hard=True,
            stop="mid_stage" if mid_stage else "after_baseline_complete",
            gate_options={} if initial is None else {"stages": initial},
            campaign_options={"rounds": 2},
        )
    path = tmp_path / "manifest.json"
    document = json.loads(path.read_text())
    gate = document["campaign"]["gate"]
    assert gate.get("stages") == initial
    if initial is None:
        gate["stages"] = "focus_first"
    else:
        del gate["stages"]
    path.write_text(json.dumps(document))
    before = (run / "campaign.jsonl").read_bytes()
    if mid_stage:
        opened = [
            r["payload"] for r in _records(run / "campaign.jsonl") if r["event"] == "stage_opened"
        ][-1]
        assert opened.get("scope") == ("focus" if initial else None)
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter)
    assert refused.value.code == "run_identity_changed"
    assert refused.value.effects == ("rebaseline", "close")
    assert (run / "campaign.jsonl").read_bytes() == before
