"""Chain-limit regression captured before changing driver, reducer or audit."""

import pytest

from tests.campaign_drivers import audit_mismatches
from tests.model_grouping_fakes import PortionGrouper, chat_grouper_section, use_chat_grouper
from tests.test_tree_campaign_family14_connection_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run
from tests.test_tree_campaign_family15_role_failures import DECLINING_REFLECTOR

#: Requests of one `model_grouping/2` grouping whose one portion is answered
#: invalidly: the preflight, the answer and its two protocol repairs.
INVALID_GROUPING = 4

pytestmark = pytest.mark.slow


async def test_a_family15_second_full_fallback_passes_the_audit(tmp_path, monkeypatch):
    client = use_chat_grouper(monkeypatch, PortionGrouper(invalid=True))

    def configure(document):
        document["grouper"] = chat_grouper_section()
        document["campaign"]["baseline_refresh_after_rounds"] = 1

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        campaign={"rounds": 2},
        reflector=DECLINING_REFLECTOR,
    )
    assert payloads(run, "campaign_created")[0]["family_version"] == 15
    assert (result.terminal, result.owner, result.code) == (
        "suspended",
        "grouper",
        "grouper_failing",
    )
    assert len(client.requests) == 2 * INVALID_GROUPING
    (first,) = payloads(run, "grouping_finished")
    assert first["round"] == 1
    assert first["fallback"] == {
        "kind": "singletons",
        "scope": "all",
        "reason": "invalid_output",
    }
    (second,) = payloads(run, "grouping_failed")
    assert (second["round"], second["code"], second["reason"], second["error_class"]) == (
        2,
        "role_failed",
        "invalid_output",
        "content",
    )
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_full_fallback_limit_agrees_across_driver_reducer_and_audit(
    tmp_path, monkeypatch, limit
):
    client = use_chat_grouper(monkeypatch, PortionGrouper(invalid=True))

    def configure(document):
        document["grouper"] = chat_grouper_section(max_consecutive_full_fallbacks=limit)
        document["campaign"]["baseline_refresh_after_rounds"] = 1
        document["reflector"]["max_consecutive_skips"] = 10

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        campaign={"rounds": limit + 1},
        reflector=DECLINING_REFLECTOR,
    )
    assert (result.terminal, result.code) == ("suspended", "grouper_failing")
    assert len(client.requests) == INVALID_GROUPING * limit
    assert len(payloads(run, "grouping_finished")) == limit - 1
    assert payloads(run, "grouping_failed")[0]["round"] == limit
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_skip_limit_agrees_across_driver_reducer_and_audit(tmp_path, monkeypatch, limit):
    from tests.test_tree_campaign_family15_role_failures import reflector

    def configure(document):
        document["reflector"]["max_consecutive_failures"] = 1
        document["reflector"]["max_consecutive_skips"] = limit

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        campaign={"rounds": limit + 1},
        reflector=reflector(invalid_calls=99),
    )
    assert (result.terminal, result.code) == ("suspended", "reflector_failing")
    closed = payloads(run, "round_closed")
    assert len(closed) == limit and all(p["visitation"] == "skipped" for p in closed)
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("kind", ["grouper", "reflector"])
async def test_lowered_chain_limit_applies_to_the_existing_chain(tmp_path, monkeypatch, kind):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut
    from tests.test_tree_campaign_family15_role_failures import reflector

    # Three groupings in all: rounds 1 and 2 before the cut, and round 3 after
    # the resume, which fails at the lowered limit. Each is the preflight, one
    # invalid answer and its two repairs (`INVALID_GROUPING`), so 3 x 4
    # requests; one more grouping would exceed the budget and fail the run.
    groupings = 3
    client = PortionGrouper(invalid=True, budget=groupings * INVALID_GROUPING)
    if kind == "grouper":
        use_chat_grouper(monkeypatch, client)
    script = DECLINING_REFLECTOR if kind == "grouper" else reflector(invalid_calls=99)

    def settings(limit):
        def configure(document):
            document["campaign"]["baseline_refresh_after_rounds"] = 1
            document["reflector"]["max_consecutive_failures"] = 1
            document["reflector"]["max_consecutive_skips"] = limit if kind == "reflector" else 10
            if kind == "grouper":
                document["grouper"] = chat_grouper_section(max_consecutive_full_fallbacks=limit)

        return configure

    original = _Campaign.hook
    closed = []

    def cut(self, name):
        original(self, name)
        if name == "after_round_closed":
            closed.append(1)
            if len(closed) == 2:
                raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", cut)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, patch, configure=settings(4), campaign={"rounds": 4}, reflector=script
            )
    with pytest.raises(EvolveRefused) as refused:
        await public_run(
            tmp_path, monkeypatch, configure=settings(1), campaign={"rounds": 4}, reflector=script
        )
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=settings(1),
        campaign={"rounds": 4},
        reflector=script,
        authorizations=[_decision(refused.value, None, "lower chain limit")],
    )
    assert (result.terminal, result.code) == ("suspended", kind + "_failing")
    if kind == "grouper":
        assert [p["round"] for p in payloads(run, "grouping_failed")] == [3]
        assert len(payloads(run, "grouping_finished")) == groupings - 1
        assert len(client.requests) == groupings * INVALID_GROUPING
    else:
        assert len(payloads(run, "round_closed")) == 3
    assert audit_mismatches(run) == []


async def test_first_skip_limit_recovers_the_owed_suspension(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family15_delivery import PowerCut
    from tests.test_tree_campaign_family15_role_failures import reflector

    def configure(document):
        document["reflector"]["max_consecutive_failures"] = 1
        document["reflector"]["max_consecutive_skips"] = 1

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=configure,
            campaign={"rounds": 2},
            reflector=reflector(invalid_calls=99),
            cut="after_round_closed",
        )
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        campaign={"rounds": 2},
        reflector=reflector(invalid_calls=99),
    )
    assert (result.terminal, result.code) == ("suspended", "reflector_failing")
    assert len(payloads(run, "reflection_failed")) == 1
    assert audit_mismatches(run) == []


async def test_skip_limit_is_bound_when_the_round_closes_after_rebase(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut
    from tests.test_tree_campaign_family15_role_failures import reflector

    def settings(limit):
        def configure(document):
            document["reflector"]["max_consecutive_failures"] = 1
            document["reflector"]["max_consecutive_skips"] = limit

        return configure

    script = reflector(invalid_calls=99)
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=settings(3),
            campaign={"rounds": 2},
            reflector=script,
            cut="after_reflection_failed",
        )
    with pytest.raises(EvolveRefused) as refused:
        await public_run(
            tmp_path, monkeypatch, configure=settings(1), campaign={"rounds": 2}, reflector=script
        )
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=settings(1),
        campaign={"rounds": 2},
        reflector=script,
        authorizations=[_decision(refused.value, None, "lower skip limit before close")],
    )
    assert (result.terminal, result.code) == ("suspended", "reflector_failing")
    assert len(payloads(run, "reflection_failed")) == 1
    assert audit_mismatches(run) == []


async def test_bind_shadow_after_rebase_preserves_an_owed_grouper_suspension(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut

    # One grouping, before the cut: the resumed runs must not group again, so
    # the budget is that grouping's preflight, answer and two repairs.
    client = use_chat_grouper(monkeypatch, PortionGrouper(invalid=True, budget=INVALID_GROUPING))

    def configure(document):
        document["grouper"] = chat_grouper_section(max_consecutive_full_fallbacks=1)

    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, configure=configure, cut="after_grouping_failed")

    def changed(document):
        configure(document)
        document["checks"] = {
            "version": "checks/1",
            "modes": {"verbatim_answer_unmatched": "shadow"},
        }
        document["verbatim_answers"] = {
            "version": "verbatim_answers/1",
            "strings": ["a declared answer absent from the corpus"],
        }

    with pytest.raises(EvolveRefused) as refused:
        await public_run(tmp_path, monkeypatch, configure=changed)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=changed,
        authorizations=[_decision(refused.value, None, "accept bind check before owed suspension")],
    )
    assert (result.terminal, result.code) == ("suspended", "grouper_failing")
    assert payloads(run, "check_shadowed")[0]["check"] == "verbatim_answer_unmatched"
    assert len(payloads(run, "grouping_failed")) == 1
    assert len(client.requests) == INVALID_GROUPING
    assert audit_mismatches(run) == []


def test_single_skip_limit_agrees_after_a_post_diagnosis_suspension(monkeypatch):
    import importlib
    from dataclasses import replace
    from types import SimpleNamespace

    from evolution.tree.campaign_schema import CampaignLifecycle, CampaignSnapshot, RoundState

    driver = importlib.import_module("evolution.tree.campaign")
    audit = importlib.import_module("evolution.tree.campaign_projections")
    diagnosis = dict(
        status="failed",
        error_class="invalid_result",
        reason="invalid_result",
        seq=2,
        operation_id="reflection-op",
    )
    state = RoundState(
        round=1,
        reflections=(diagnosis,),
        focus_portion=dict(neighborhood_id="g", baseline_binding="a" * 64, policy_digest="b" * 64),
    )
    opened = CampaignSnapshot(
        identity={},
        run_id="run",
        alpha_plan={},
        rounds={1: state},
        champion=("v0000", "a" * 64),
        champion_dependency_digest="b" * 64,
        measurements={},
        accounted=frozenset(),
        counts={},
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=1,
        adaptive=True,
        family_version=15,
        suspensions=({"seq": 3},),
    )
    closure = dict(round=1, outcome="unplanned", reason="reflection_failed", visitation="skipped")
    closed = replace(opened, rounds={1: replace(state, closed=closure)})
    assert driver.skip_chain_length(closed, 1) == 0
    # No live path currently places this suspension before round_closed.
    # Inject only the reduced snapshot to exercise the audit's actual decision
    # on that latent shape without forging a supposedly valid journal.
    monkeypatch.setattr(audit, "reduce", lambda *args, **kwargs: closed)
    context = SimpleNamespace(
        family_version=15,
        adaptation_identity=lambda index: SimpleNamespace(
            to_payload=lambda: {"reflection": {"max_consecutive_skips": 1}}
        ),
    )
    assert not audit._second_skip_diagnosis(
        context, [SimpleNamespace(event="round_closed", payload=closure)], 2
    )
    reducer = SimpleNamespace(snapshot=opened, _reflection_threshold=1, _skip_chain_limit=1)
    assert CampaignLifecycle._resilient_unplanned_close(reducer, closure, 1, diagnosis) is None
    assert opened.rounds[1].closed is None
