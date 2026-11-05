"""H12a: family-14 links through the pre-assembly driver (R70, /15 §9)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.campaign_drivers import (
    EDITED,
    OBSERVE,
    STATISTICAL,
    _champion_copy,
    _cut_hook,
    _events,
    _measurement_events,
    _records,
    _uncut,
)
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

#: A second valid contract the probe passes on `FakeInstance`, whose events are
#: `{"type": "message", "text": ...}` (`tests/campaign_fakes.py:358`).
MESSAGE_CONTRACT = {
    "version": "metric_contract/1",
    "kind": "pointwise",
    "target_fields": [{"event": "message", "pointer": "/text", "value": "nonempty_string"}],
}


def edit_document(monkeypatch, edit):
    """Apply `edit(document)` to every `/15` document `family14_run` writes from now on."""

    from tests import test_tree_campaign_family14_run as driver

    original = driver.family14_document

    def written(document, **options):
        moved = original(document, **options)
        edit(moved)
        return moved

    monkeypatch.setattr(driver, "family14_document", written)


def linked(root):
    """`(predecessor run, its final champion copy)`, `_two_case_run`'s `linked_from`."""

    return root, _champion_copy(root)


def link_of(run) -> dict[str, Any]:
    (created,) = payloads(run, "campaign_created")
    return created["identity"]["linked_from"]


async def predecessor14(tmp_path, monkeypatch, config=OBSERVE, **options):
    """A finished family-14 run under `tmp_path / "pred"`; returns its run directory."""

    with monkeypatch.context() as patch:
        run, snapshot = await family14_run(tmp_path / "pred", patch, config, **options)
    assert snapshot.finished is not None
    return run


async def successor14(
    tmp_path, monkeypatch, predecessor, config=OBSERVE, *, baseline="inherit", **options
):
    """A family-14 successor under `tmp_path / "succ"`; returns `(run, snapshot)`."""

    with monkeypatch.context() as patch:
        if baseline == "fresh":
            edit_document(patch, lambda d: d["linked_from"].update(baseline="fresh"))
        return await family14_run(
            tmp_path / "succ", patch, config, linked_from=linked(predecessor), **options
        )


async def refused_successor(tmp_path, monkeypatch, predecessor, config=OBSERVE, **options):
    """The successor's pre-write refusal; the predecessor's bytes are unchanged."""

    from evolution.tree.evolve import EvolveRefused

    before = (predecessor / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as refused:
        await successor14(tmp_path, monkeypatch, predecessor, config, **options)
    assert not (tmp_path / "succ" / "run" / "campaign.jsonl").exists()
    assert (predecessor / "campaign.jsonl").read_bytes() == before
    return refused.value


def assert_clean(run, snapshot):
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []


async def test_a_14_successor_links_a_finished_14_predecessor(tmp_path, monkeypatch):
    predecessor = await predecessor14(tmp_path, monkeypatch)
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor)
    link = link_of(run)
    assert link["predecessor_family"] == 14
    assert link["first_round"] == 2 and link["rounds_closed"] == 1
    assert link["alpha_rounds"] == {}
    assert "condition_changed" not in link
    assert snapshot.finished["continuation"] == {"kind": "linked_run", "reason": "rounds_exhausted"}
    assert_clean(run, snapshot)


def _forged_creation(created, family):
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    forged = copy.deepcopy(created)
    identity = forged["payload"]["identity"]
    identity["linked_from"]["predecessor_family"] = family
    forged["payload"]["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
    return forged


def _admit(record, family_version):
    from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
    from evolution.tree.storage.envelope import Envelope

    state = CampaignLifecycle(selector=None, adaptive=True, family_version=family_version)
    state.accept(Envelope.from_payload(record, family=campaign_family_for(family_version)))


@pytest.mark.parametrize("family", [13, 14])
async def test_the_family_14_lifecycle_admits_predecessor_families_13_and_14(
    tmp_path, monkeypatch, family
):
    predecessor = await predecessor14(tmp_path, monkeypatch)
    run, _ = await successor14(tmp_path, monkeypatch, predecessor)
    (created,) = _events(run, "campaign_created")
    _admit(_forged_creation(created, family), 14)


@pytest.mark.parametrize("family", [12, 15])
async def test_the_family_14_lifecycle_refuses_other_predecessor_families(
    tmp_path, monkeypatch, family
):
    import re

    from evolution.tree.storage.envelope import SchemaError

    predecessor = await predecessor14(tmp_path, monkeypatch)
    run, _ = await successor14(tmp_path, monkeypatch, predecessor)
    (created,) = _events(run, "campaign_created")
    rule = "linked_from.predecessor_family is not a linkable family"
    with pytest.raises(SchemaError, match=f"^{re.escape(rule)}$"):
        _admit(_forged_creation(created, family), 14)


# -- Task 4: the /15 §15.7 link matrix, refusals and inherit sources ------------------


def _imported(run, measurement_id="m-r00-champion"):
    return [
        r
        for r in _records(_measurement_events(run, measurement_id))
        if r["event"] == "sample_imported"
    ]


def _assert_predecessor_clean(run):
    assert journal_consistent(run).finished is not None
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("source", [14])
@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
async def test_links_13_and_14_with_fresh_and_inherit(tmp_path, monkeypatch, source, baseline):
    if source == 13:
        predecessor = await _uncut(tmp_path / "pred", monkeypatch, OBSERVE)
    else:
        predecessor = await predecessor14(tmp_path, monkeypatch)
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor, baseline=baseline)
    link = link_of(run)
    assert link["predecessor_family"] == source
    assert ("baseline" in link) == (baseline == "inherit")
    assert bool(_imported(run)) == (baseline == "inherit")
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


@pytest.mark.parametrize("source", ["14", "14-suspended", "14-champion-incomplete"])
async def test_an_unfinished_predecessor_is_a_hard_refusal(tmp_path, monkeypatch, source):
    from tests.campaign_drivers import _arguments, _ResettableTagged
    from tests.model_grouping_fakes import ScriptedClient, chat_grouper_section, use_chat_grouper
    from tests.test_tree_campaign_family14_grouper_run import _provider_script

    with monkeypatch.context() as patch:
        if source == "14-champion-incomplete":
            # #194 handoff: the seed baseline's cut stop, then the resume that
            # suspends `champion_incomplete` listing its gaps (unfinished).
            from tests.test_tree_campaign_family14_champion_gap import (
                _assert_listed,
                _cut_stop,
                _GapTarget,
                _run,
            )
            from tests.test_tree_campaign_family14_refresh_run import CHAMPION0

            adapter = _GapTarget(tmp_path / "pred" / "run", CHAMPION0)
            await _cut_stop(tmp_path / "pred", patch, adapter)
            run, snapshot = await _run(tmp_path / "pred", patch, adapter)
            _assert_listed(run, snapshot, CHAMPION0)
        elif source == "14-suspended":
            use_chat_grouper(patch, ScriptedClient(_provider_script("outage", patch)))
            run, snapshot = await family14_run(
                tmp_path / "pred", patch, family14_settings={"grouper": chat_grouper_section()}
            )
            assert snapshot.finished is None
            assert [p["reason"] for p in payloads(run, "campaign_suspended")] == [
                "grouper_provider_failed"
            ]
        else:
            _cut_hook(patch, "after_round_closed")
            with pytest.raises(Crash):
                if source == "13":
                    adapter = _ResettableTagged(used=EDITED["used"])
                    await _two_case_run(
                        tmp_path / "pred",
                        terminal=None,
                        campaign={"rounds": 2},
                        **_arguments(OBSERVE, adapter),
                    )
                else:
                    await family14_run(tmp_path / "pred", patch, campaign={"rounds": 2})
    predecessor = tmp_path / "pred" / "run"
    assert not _events(predecessor, "campaign_finished")
    closed = [] if source in ("14-suspended", "14-champion-incomplete") else [1]
    assert [p["round"] for p in payloads(predecessor, "round_closed")] == closed
    refused = await refused_successor(tmp_path, monkeypatch, predecessor)
    assert (refused.code, refused.path) == ("lineage_not_finished", "linked_from")
    assert refused.instance_digest is None  # hard: only family 5 is authorizable


async def _observe_then_statistical14(tmp_path, monkeypatch):
    """`_observe_then_statistical` through `family14_run`: a two-round `observe`
    run cut after round 1 closed, moved to `statistical` (repeats 4) under an
    accepted `rebaseline`, then finished. Returns its run directory."""

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _change, _ResettableTagged

    root = tmp_path / "pred"
    statistical = {**OBSERVE, "gate": STATISTICAL["gate"], "repeats": STATISTICAL["repeats"]}
    adapter = _ResettableTagged(used=EDITED["used"])
    options = {"adapter": adapter, "campaign": {"rounds": 2}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(root, patch, OBSERVE, **options)
    with monkeypatch.context() as patch:
        with pytest.raises(EvolveRefused) as refused:
            await family14_run(root, patch, statistical, **options)
    with monkeypatch.context() as patch:
        run, snapshot = await family14_run(
            root,
            patch,
            statistical,
            authorizations=[_change(refused.value, "rebaseline")],
            **options,
        )
    assert snapshot.finished is not None
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["identity"]["policy"]["gate"]["mode"] == "statistical"
    return run


@pytest.mark.parametrize("history", ["13-observe", "14-observe", "14-observe-then-statistical"])
async def test_a_statistical_successor_refuses_an_observe_history(tmp_path, monkeypatch, history):
    if history == "13-observe":
        predecessor = await _uncut(tmp_path / "pred", monkeypatch, OBSERVE)
    elif history == "14-observe":
        predecessor = await predecessor14(tmp_path, monkeypatch)
    else:
        predecessor = await _observe_then_statistical14(tmp_path, monkeypatch)
    refused = await refused_successor(tmp_path, monkeypatch, predecessor, STATISTICAL)
    assert (refused.code, refused.path) == ("lineage_mode_unsupported", "linked_from")


@pytest.mark.parametrize("source", [14])
async def test_an_observe_successor_links_a_statistical_predecessor(tmp_path, monkeypatch, source):
    if source == 13:
        predecessor = await _uncut(tmp_path / "pred", monkeypatch, STATISTICAL)
    else:
        predecessor = await predecessor14(tmp_path, monkeypatch, STATISTICAL)
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor, OBSERVE)
    link = link_of(run)
    assert link["predecessor_family"] == source
    assert snapshot.identity["policy"]["gate"]["mode"] == "observe"
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


async def test_a_stopped_predecessor_is_finished_and_links(tmp_path, monkeypatch):
    """An orderly stop finishes a run (AGENTS.md); its successor links."""

    journal = tmp_path / "pred" / "run" / "campaign.jsonl"

    def stop():
        return journal.is_file() and b'"event":"round_closed"' in journal.read_bytes()

    predecessor = await predecessor14(tmp_path, monkeypatch, cancelled=stop, campaign={"rounds": 2})
    finished = journal_consistent(predecessor).finished
    assert (finished["outcome"], finished["reason"]) == ("stopped", "cancelled")
    assert [p["round"] for p in payloads(predecessor, "round_closed")] == [1]
    assert audit(predecessor)["mismatches"] == []
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor)
    link = link_of(run)
    assert link["predecessor_family"] == 14
    assert link["first_round"] == 2 and link["rounds_closed"] == 1
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


def _closure_predecessor(closure, tmp_path, monkeypatch):
    """`(config, family14_settings, expected source)` of each closure's predecessor;
    `config` is also the successor's, so the inherited rows share its scorer."""

    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS
    from tests.test_tree_campaign_family14_stored_run import _two_round_scorer

    if closure == "promoted":
        return OBSERVE, None, "m-r01-final-01-a01"
    if closure == "stalled":
        return NO_FINALISTS, None, "m-r00-champion"
    scorer = _two_round_scorer(monkeypatch, tmp_path / "veto.flag", True)
    if closure == "stable-retained":
        return {**OBSERVE, "scorer": scorer}, {"stability": "stable"}, "m-r00-champion"
    return {**OBSERVE, "scorer": scorer}, {"stability": "unstable"}, "m-r01-final-01-champion"


@pytest.mark.parametrize("closure", ["promoted", "stable-retained", "unstable-retained", "stalled"])
async def test_inherit_reads_the_champion_measurement_after_each_closure(
    tmp_path, monkeypatch, closure
):
    from evolution.tree import campaign_projections

    with monkeypatch.context() as patch:
        config, settings, expected = _closure_predecessor(closure, tmp_path, patch)
        predecessor = await predecessor14(tmp_path, patch, config, family14_settings=settings)
    outcome = {
        "promoted": "promoted",
        "stable-retained": "retained",
        "unstable-retained": "retained",
        "stalled": "stalled",
    }[closure]
    assert [p["outcome"] for p in payloads(predecessor, "round_closed")] == [outcome]
    _assert_predecessor_clean(predecessor)
    run, snapshot = await successor14(
        tmp_path, monkeypatch, predecessor, config, family14_settings=settings
    )
    baseline = link_of(run)["baseline"]
    source = campaign_projections.champion_measurement(journal_consistent(predecessor))[0]
    assert baseline["source_measurement_id"] == source == expected
    assert baseline["imported"] > 0
    assert len(_imported(run)) == baseline["imported"]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


# -- Task 5: `condition_changed`, policy changes and the bare `metric_contract` -------


async def test_a_changed_contract_is_reported_alone_and_the_scorer_is_not(tmp_path, monkeypatch):
    """Carried rows bring no traces (PDR-0013), so a carried failure that is a
    verdict only is unavailable to focus. This predecessor's Champion fails no
    case by a critical veto or a non-completed outcome, so the inheriting round
    stalls with focus `none` and dispatches nothing, and no probe runs there.
    Both runs therefore refresh after one round and the successor runs two:
    its refresh dispatches under the new contract and the probe runs on it."""

    settings = {"refresh": 1}
    options = {"family14_settings": settings, "campaign": {"rounds": 2}}
    predecessor = await predecessor14(tmp_path, monkeypatch, **options)
    with monkeypatch.context() as patch:
        edit_document(
            patch, lambda d: d["scorer"].update(metric_contract=copy.deepcopy(MESSAGE_CONTRACT))
        )
        run, snapshot = await family14_run(
            tmp_path / "succ", patch, OBSERVE, linked_from=linked(predecessor), **options
        )
    link = link_of(run)
    assert link["condition_changed"] == ["identity.metric_contract"]
    assert link["baseline"]["imported"] > 0  # carried rows are not revalidated (ADR-0032)
    # The probe ran on the new contract: `connection_preflight_finished` names
    # no contract, so its start's `metric_contract` digest ties it to the
    # successor identity's, which differs from the predecessor's.
    (started,) = payloads(run, "connection_preflight_started")
    (finished,) = payloads(run, "connection_preflight_finished")
    assert finished["status"] == "compatible"
    contract = snapshot.identity["metric_contract"]
    assert started["metric_contract"] == contract
    assert contract != journal_consistent(predecessor).identity["metric_contract"]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


async def test_a_role_visibility_change_is_not_a_comparison_condition(tmp_path, monkeypatch):
    """The fixture seed (`write_workspace`) holds `skills/SKILL.md`, and the
    successor seeds from the predecessor's Champion copy, which keeps it."""

    predecessor = await predecessor14(tmp_path, monkeypatch)
    assert (_champion_copy(predecessor) / "skills" / "SKILL.md").is_file()
    with monkeypatch.context() as patch:
        edit_document(
            patch, lambda d: d["role_visibility"].update(readable_paths=["skills/SKILL.md"])
        )
        run, snapshot = await successor14(tmp_path, patch, predecessor)
    assert (
        snapshot.identity["role_visibility"]
        != journal_consistent(predecessor).identity["role_visibility"]
    )
    assert "condition_changed" not in link_of(run)
    assert_clean(run, snapshot)


@pytest.mark.parametrize(
    "predecessor_settings,change",
    [
        (None, {"stability": "stable"}),
        ({"stability": "stable"}, {"stability": "unstable"}),
        (None, {"round_stop": "first_accepted"}),
        (None, {"refresh": 2}),
    ],
    ids=["stability", "stability-reverse", "round_stop", "refresh"],
)
async def test_a_policy_change_across_a_link_is_admitted_and_reported(
    tmp_path, monkeypatch, predecessor_settings, change
):
    predecessor = await predecessor14(tmp_path, monkeypatch, family14_settings=predecessor_settings)
    run, snapshot = await successor14(tmp_path, monkeypatch, predecessor, family14_settings=change)
    assert link_of(run)["condition_changed"] == ["identity.policy_digest"]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


def _c1_veto(scorer):
    """`scorer` whose `c1` also fails by a critical veto until the skill is
    edited: a failure a carried row keeps without traces (PDR-0013)."""

    old = '"critical_pass": True'
    assert scorer.count(old) == 1
    return scorer.replace(old, '"critical_pass": edited or payload["case_id"] != "c1"')


@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
@pytest.mark.parametrize("predecessor_stability", ["unstable", "stable"])
async def test_a_stable_successor_compares_against_its_own_round_0_baseline(
    tmp_path, monkeypatch, predecessor_stability, baseline
):
    """The predecessor's every round stalls, so `c1` still fails, by a critical
    veto that a carried row keeps without traces; the successor opens an
    Attempt from its round-2 baseline -- carried (`inherit`) or measured
    (`fresh`) -- and its stored Champion is that baseline: `m-r00-champion`
    of its own journal, never the predecessor's same-named measurement."""

    from tests.campaign_drivers import VARYING_SCORER, WORSE_SCORER
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await predecessor14(
        tmp_path,
        monkeypatch,
        {**NO_FINALISTS, "scorer": _c1_veto(WORSE_SCORER)},
        family14_settings={"stability": predecessor_stability},
    )
    assert [p["outcome"] for p in payloads(predecessor, "round_closed")] == ["stalled"]
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        {**STATISTICAL, "scorer": _c1_veto(VARYING_SCORER)},
        baseline=baseline,
        family14_settings={"stability": "stable"},
    )
    link = link_of(run)
    expected = ["identity.scorer.digest"]
    if predecessor_stability == "unstable":
        expected = ["identity.policy_digest", *expected]
    assert link["condition_changed"] == expected
    assert link["first_round"] == 2
    assert {p["round"]: p["measurement_id"] for p in payloads(run, "baseline_bound")} == {
        2: "m-r00-champion"
    }
    (theirs,) = payloads(predecessor, "baseline_complete")
    theirs = theirs["evidence"][0]["content_digest"]
    if baseline == "inherit":
        assert link["baseline"]["source_measurement_id"] == "m-r00-champion"
        assert link["baseline"]["imported"] == len(_imported(run)) > 0
        assert link["baseline"]["source_prefix_digest"] == theirs
    (complete,) = [p for p in payloads(run, "baseline_complete") if p["round"] == 2]
    stored = {
        "measurement_id": "m-r00-champion",
        "prefix_digest": complete["evidence"][0]["content_digest"],
    }
    assert stored["prefix_digest"] != theirs
    (focus,) = payloads(run, "focus_portion_selected")
    assert focus["reason"] == "selected" and focus["failing"] > 0
    finals = payloads(run, "final_measurement_bound")
    assert payloads(run, "attempt_opened") and finals
    for final in finals:
        assert final["champion_side"] == "stored"
        assert final["stored_champion"] == stored
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
async def test_an_unstable_successor_measures_the_champion_fresh_in_each_final_measurement(
    tmp_path, monkeypatch, baseline
):
    """/15 §9: an `unstable` successor stores no Champion; each of its final
    measurements measures the Champion fresh. Same critical-veto setup as the
    stable sibling above, so the successor opens an Attempt from its carried
    (`inherit`) or measured (`fresh`) round-2 baseline."""

    from tests.campaign_drivers import VARYING_SCORER, WORSE_SCORER
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await predecessor14(
        tmp_path,
        monkeypatch,
        {**NO_FINALISTS, "scorer": _c1_veto(WORSE_SCORER)},
        family14_settings={"stability": "stable"},
    )
    assert [p["outcome"] for p in payloads(predecessor, "round_closed")] == ["stalled"]
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        {**STATISTICAL, "scorer": _c1_veto(VARYING_SCORER)},
        baseline=baseline,
        family14_settings={"stability": "unstable"},
    )
    assert link_of(run)["first_round"] == 2
    (focus,) = payloads(run, "focus_portion_selected")
    assert focus["reason"] == "selected" and focus["failing"] > 0
    finals = payloads(run, "final_measurement_bound")
    assert payloads(run, "attempt_opened") and finals
    for final in finals:
        assert final["stability"] == "unstable"
        assert final["champion_side"] == "fresh"
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


async def _rebased14(tmp_path, monkeypatch, settings, edit=None, cancelled=None):
    """A two-round family-14 predecessor under `tmp_path / "pred"`, cut after
    round 1 closed, resumed with `settings` (and `edit` on its document): the
    run is refused `run_identity_changed` and resumed again under an accepted
    `rebaseline`, with `cancelled` as its stop token. Returns its run directory."""

    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_refresh_recovery import _decision

    root = tmp_path / "pred"
    options = {"campaign": {"rounds": 2}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(root, patch, **options)
    run = root / "run"
    assert not _events(run, "campaign_finished")
    with monkeypatch.context() as patch:
        if edit is not None:
            edit_document(patch, edit)
        with pytest.raises(EvolveRefused) as refused:
            await family14_run(root, patch, family14_settings=settings, **options)
    assert refused.value.code == "run_identity_changed"
    decision = _decision(refused.value, "rebaseline")
    if cancelled is not None:
        options["cancelled"] = cancelled
    with monkeypatch.context() as patch:
        if edit is not None:
            edit_document(patch, edit)
        run, snapshot = await family14_run(
            root, patch, family14_settings=settings, authorizations=[decision], **options
        )
    assert snapshot.finished is not None
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "rebaseline"
    assert len(payloads(run, "identity_rebased")) == 1
    _assert_predecessor_clean(run)
    return run


@pytest.mark.parametrize("successor_stability", ["stable", "unstable"])
async def test_the_link_compares_against_the_latest_rebased_identity(
    tmp_path, monkeypatch, successor_stability
):
    """`campaign.rounds` is part of the policy, so the successor declares the
    predecessor's two rounds: only `stability` can differ from either identity."""

    predecessor = await _rebased14(tmp_path, monkeypatch, {"stability": "stable"})
    (rebased,) = payloads(predecessor, "identity_rebased")
    (created,) = payloads(predecessor, "campaign_created")
    assert created["identity"]["policy"]["gate"]["stability"] == "unstable"
    assert rebased["identity"]["policy"]["gate"]["stability"] == "stable"
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        family14_settings={"stability": successor_stability},
        campaign={"rounds": 2},
    )
    changed = link_of(run).get("condition_changed")
    assert changed == (None if successor_stability == "stable" else ["identity.policy_digest"])
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


async def test_a_bare_metric_contract_reaches_a_family_14_declaration(tmp_path, monkeypatch):
    """H1b handoff, genuine: a predecessor rebaselined for a contract change and
    stopped before the new round's baseline is bound; the inherit source is the
    old-epoch measurement, so `changed_since` names the bare `metric_contract`."""

    from tests.test_tree_campaign_family14_refresh_recovery import _journal_has

    def contract(document):
        document["scorer"]["metric_contract"] = copy.deepcopy(MESSAGE_CONTRACT)

    stop = _journal_has(tmp_path / "pred" / "run", b'"event":"identity_rebased"', 1)
    predecessor = await _rebased14(tmp_path, monkeypatch, None, edit=contract, cancelled=stop)
    finished = journal_consistent(predecessor).finished
    assert (finished["outcome"], finished["reason"]) == ("stopped", "cancelled")
    assert [p["round"] for p in payloads(predecessor, "baseline_bound") if p["round"] == 2] == []

    # The successor declares the predecessor's latest identity exactly
    # (`campaign.rounds` is policy), so only the old-epoch source differs.
    rounds = {"campaign": {"rounds": 2}}

    def declaration(run):
        return [r["payload"]["source"] for r in _imported(run)], link_of(run)

    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        run, snapshot = await successor14(tmp_path, patch, predecessor, **rounds)
    sources, link = declaration(run)
    assert "condition_changed" not in link
    assert link["baseline"]["fields"] == ["metric_contract"]
    assert sources and all("metric_contract" in s["condition_fields"] for s in sources)
    assert snapshot.finished is not None
    assert_clean(run, snapshot)

    # The resume path reads the same declaration back (Task 2's RESUME hunk).
    again = tmp_path / "again"
    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        _cut_hook(patch, "after_measurement_bound")
        with pytest.raises(Crash):
            await successor14(again, patch, predecessor, **rounds)
    cut = again / "succ" / "run"
    assert payloads(cut, "campaign_created") and not _events(cut, "campaign_finished")
    with monkeypatch.context() as patch:
        edit_document(patch, contract)
        resumed, resumed_snapshot = await successor14(again, patch, predecessor, **rounds)
    assert link_of(resumed) == link
    assert declaration(resumed)[0] == sources
    assert resumed_snapshot.finished is not None
    assert_clean(resumed, resumed_snapshot)


# -- Task 6: refresh lineage across a link --------------------------------------------


async def _stalled_predecessor(tmp_path, monkeypatch, source):
    """A finished predecessor whose one round closed `stalled` (refresh count 1)."""

    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    if source == 13:
        predecessor = await _uncut(tmp_path / "pred", monkeypatch, NO_FINALISTS)
    else:
        predecessor = await predecessor14(
            tmp_path, monkeypatch, NO_FINALISTS, family14_settings={"refresh": 2}
        )
    assert [p["outcome"] for p in payloads(predecessor, "round_closed")] == ["stalled"]
    if source == 14:  # the replay and audit here are family 14's
        _assert_predecessor_clean(predecessor)
    return predecessor


@pytest.mark.parametrize("source", [14])
async def test_a_linked_successor_starts_its_refresh_count_at_0(tmp_path, monkeypatch, source):
    """/15 §9: the predecessor's count does not carry. N = 2; the predecessor closed one
    `stalled` round (count 1). Carried, the successor's second round would refresh."""
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await _stalled_predecessor(tmp_path, monkeypatch, source)
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        NO_FINALISTS,
        family14_settings={"refresh": 2},
        campaign={"rounds": 2},
    )
    assert link_of(run)["first_round"] == 2
    assert [p["round"] for p in payloads(run, "round_closed")] == [2, 3]
    assert not payloads(run, "baseline_refresh_bound")
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


@pytest.mark.parametrize("source", [14])
async def test_the_successor_refreshes_after_its_own_n_rounds(tmp_path, monkeypatch, source):
    """N = 2 counted from the successor's first round: rounds 2 and 3 count on
    its round-2 baseline `m-r00-champion`, and round 4 refreshes it."""
    from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS

    predecessor = await _stalled_predecessor(tmp_path, monkeypatch, source)
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        NO_FINALISTS,
        family14_settings={"refresh": 2},
        campaign={"rounds": 3},
    )
    assert [p["round"] for p in payloads(run, "round_closed")] == [2, 3, 4]
    (bound,) = payloads(run, "baseline_refresh_bound")
    assert bound["round"] == 4
    assert bound["counted_rounds"] == {"first": 2, "last": 3}
    assert bound["replaced"]["measurement_id"] == CHAMPION0
    assert [p["round"] for p in payloads(run, "baseline_refresh_completed")] == [4]
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


#: The H7 predecessor shapes whose last round (2) holds a refresh that never
#: completed, and whether that round's `baseline_bound` is journaled.
INCOMPLETE_REFRESH = {
    "stopped-in-refresh-stage": True,
    "stopped-after-refresh-stage": True,
    "closed-in-refresh-stage": True,
    "stopped-refresh-bound-only": False,
}


async def _incomplete_refresh(tmp_path, monkeypatch, shape):
    """H7's shared scenario (`NO_FINALISTS`, N = 1) under `tmp_path / "pred"`,
    finished with round 2's refresh uncompleted. Returns its run directory."""

    from tests.campaign_drivers import _cancel_when
    from tests.test_tree_campaign_family14_refresh_recovery import (
        REFRESH,
        _authorized,
        _journal_has,
        _scenario,
        _target,
    )

    root = tmp_path / "pred"
    run = root / "run"
    with monkeypatch.context() as patch:
        if shape == "closed-in-refresh-stage":
            run, snapshot, _ = await _authorized(
                root, patch, "e-after-stage-before-barrier", "close"
            )
            assert snapshot.finished["reason"] == "condition_changed"
            return run
        stop = {
            "stopped-in-refresh-stage": _cancel_when(run, REFRESH, b'"scoring_finished"'),
            "stopped-after-refresh-stage": _journal_has(run, b'"event":"baseline_complete"', 2),
            "stopped-refresh-bound-only": _journal_has(run, b'"event":"baseline_refresh_bound"', 1),
        }[shape]
        run, snapshot = await _scenario(root, patch, _target(root, faults={}), cancelled=stop)
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    return run


@pytest.mark.parametrize("shape", list(INCOMPLETE_REFRESH))
@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
async def test_a_predecessor_whose_last_round_holds_an_incomplete_refresh_links(
    tmp_path, monkeypatch, shape, baseline
):
    """Routine choice 5 (ADR-0032 amendment, 2026-10-13): `inherit` reads the
    refresh measurement once the predecessor journaled round 2's
    `baseline_bound`, and the replaced `m-r00-champion` before that. The
    successor's count starts at 0, so its round 3 binds its own
    `m-r00-champion` and refreshes nothing."""
    from tests.campaign_drivers import LENIENT, SERIAL
    from tests.test_tree_campaign_family14_refresh_recovery import REFRESH, _baselines, _target
    from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS

    predecessor = await _incomplete_refresh(tmp_path, monkeypatch, shape)
    replayed = journal_consistent(predecessor)
    assert replayed.finished is not None
    assert replayed.refreshes[-1]["round"] == 2
    assert replayed.refreshes[-1]["completed"] is None
    assert (2 in _baselines(predecessor)) == INCOMPLETE_REFRESH[shape]
    assert audit(predecessor)["mismatches"] == []
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        predecessor,
        NO_FINALISTS,
        baseline=baseline,
        adapter=_target(tmp_path / "succ", faults={}),
        family14_settings={"refresh": 1},
        campaign={"rounds": 1, "indeterminate": LENIENT, **SERIAL},
    )
    link = link_of(run)
    assert link["predecessor_family"] == 14
    assert link["first_round"] == 3
    if baseline == "inherit":
        source = REFRESH if INCOMPLETE_REFRESH[shape] else CHAMPION0
        assert link["baseline"]["source_measurement_id"] == source
        assert link["baseline"]["imported"] == len(_imported(run)) > 0
    else:
        assert "baseline" not in link
    assert _baselines(run) == {3: CHAMPION0}
    assert not payloads(run, "baseline_refresh_bound")
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


# -- Final review F1: pin of current behavior, awaiting a maintainer decision ---------


@pytest.mark.parametrize("source", [14])
async def test_an_inherit_successor_with_verdict_only_carried_failures_stalls_until_a_refresh(
    tmp_path, monkeypatch, source
):
    """Documented behavior (PDR-0013 amendment 2026-10-13, #208): with refresh
    off, a wholly inherited baseline whose carried failures are verdict-only
    never opens an Attempt. The fix belongs to a later family.

    Carried rows have no scorer traces (PDR-0013), and
    `reflection_inputs._header_flags` counts a carried row as failing only
    on a critical veto or a non-completed outcome. Carried rows do not change
    between rounds, so with a wholly inherited baseline whose every failure
    is verdict-only (`NO_FINALISTS` as is) and refresh off (0, the driver's
    default), every successor round selects focus `none`, opens no Attempt
    and closes `stalled`: the successor finishes without ever editing."""

    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await _stalled_predecessor(tmp_path, monkeypatch, source)
    run, snapshot = await successor14(
        tmp_path, monkeypatch, predecessor, NO_FINALISTS, campaign={"rounds": 2}
    )
    link = link_of(run)
    assert link["first_round"] == 2
    assert link["baseline"]["imported"] == len(_imported(run)) > 0
    assert [p["reason"] for p in payloads(run, "focus_portion_selected")] == ["none", "none"]
    assert not payloads(run, "attempt_opened")
    assert [(p["round"], p["outcome"]) for p in payloads(run, "round_closed")] == [
        (2, "stalled"),
        (3, "stalled"),
    ]
    assert not payloads(run, "baseline_refresh_bound")
    assert snapshot.finished is not None
    assert_clean(run, snapshot)


# -- Review M2: the family-13 producer refuses a family-14 predecessor ----------------
