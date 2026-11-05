"""H12b: family-14 report blocks over lifecycle-built snapshots (F0 M7, R42, C§6.3).

The `roles`, `settings` and `connection` blocks are pure functions of the
snapshot (plan choices 9-11). Here each one is read from snapshots the
lifecycle built: genuine engine prefixes (the H7 refresh prefixes, the H8
Grouper prefixes and its authorized-change cell) where the cell needs
engine-written records, and synthetic lifecycle inputs over a family-14
creation (the H10 connection grammar's helpers) otherwise. Hand-built
snapshots and the validators' refusals are in `test_tree_final_report.py`.
"""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import copy
import hashlib
from typing import Any

import pytest

from evolution.tree.campaign_identity import AdaptationIdentityV1, adaptation_identity_slot
from evolution.tree.execution.authorizations import (
    Authorization,
    identity_differences,
    identity_instance_digest,
    identity_record,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.final_report import (
    connection_block,
    roles_block,
    settings_block,
    validate_connection_block,
    validate_roles_block,
    validate_settings_block,
)
from tests.campaign_fakes import Family14Life
from tests.test_tree_campaign_family14_connection import (
    BINDING,
    FAILED,
    FINISHED,
    INTERRUPTED,
    OP,
    STARTED,
    _failed,
    _finished,
    _interrupted,
    _started,
    _suspension,
    _unavailable,
)
from tests.test_tree_campaign_family14_refresh import (  # noqa: F401
    _complete,
    _first_close,
    _life,
    _measure_refresh,
    due_life,
    off_life,
    refresh_bound_payload,
    two_records,
)
from tests.test_tree_campaign_family14_visibility_change import (
    NOW,
    _change,
    _row,
    _triple,
)
from tests.test_tree_family14_identity import _created14


def _moved(created, *, identity=None, adaptation_payload=None, effect=None):
    """`(authorization_applied, identity_rebased)` moving `created`'s identity or adaptation.

    Synthetic lifecycle inputs, consistent with each other and the creation
    (digests, slots, instance, decision id, `changed_fields`), as
    `test_tree_campaign_family14_visibility_change._change` builds them.
    """

    identity = copy.deepcopy(identity or created["identity"])
    digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    full = AdaptationIdentityV1(adaptation_payload or created["adaptation_payload"])
    slot = adaptation_identity_slot(full)
    journaled = identity_record(created)
    observed = identity_record(
        {
            **created,
            "identity": identity,
            "identity_digest": digest,
            "adaptation": slot,
            "adaptation_payload": full.to_payload(),
        }
    )
    instance = identity_instance_digest(journaled, observed)
    decision = Authorization.new("run_identity_changed", instance, "accept", now=NOW, effect=effect)
    applied = dict(
        effect=effect,
        code="run_identity_changed",
        instance_digest=instance,
        authorization=decision.decision_payload(),
        journaled=_triple(journaled),
        observed=_triple(observed),
    )
    rebased = dict(
        identity=identity,
        identity_digest=digest,
        producing_version=created["producing_version"],
        adaptation=slot,
        previous_digest=created["identity_digest"],
        changed_fields=[d.field for d in identity_differences(journaled, observed)],
        instance_digest=instance,
        authorization=decision.journal_payload(),
        adaptation_payload=full.to_payload(),
    )
    return applied, rebased


def _roles(snapshot) -> dict[str, Any]:
    block = roles_block(snapshot)
    validate_roles_block(block)
    return block


def _settings(snapshot) -> dict[str, Any]:
    block = settings_block(snapshot)
    validate_settings_block(block)
    return block


def _connection(life) -> dict[str, Any]:
    block = connection_block(life.snapshot)
    validate_connection_block(block)
    return block


# -- `grouper_kind` and the `roles` block (M7, plan choice 9) ---------------------


@pytest.mark.slow
@pytest.mark.parametrize("name", ["fields", "command", "chat", "agent_cli"])
async def test_grouper_kind_is_the_creations_grouper_kind(tmp_path, monkeypatch, name):
    from tests.test_tree_campaign_family14_grouper import _family14_life

    life = await _family14_life(tmp_path, monkeypatch, name)
    snapshot = life.snapshot
    assert snapshot.grouper_kind == name
    identity = snapshot.identity
    assert _roles(snapshot) == {
        "grouper": {"kind": name},
        "reflector": {"kind": identity["reflector"]["kind"]},
        "editor": {"kind": identity["editor"]["kind"]},
    }


def test_grouper_kind_follows_a_rebase_that_changes_the_kind():
    from tests.test_tree_campaign_family14_grouper import _agent_slot

    life = Family14Life([_row(_created14())])
    assert life.snapshot.grouper_kind == "fields"
    created = life.envelopes[0].payload
    adaptation = dict(created["adaptation_payload"])
    adaptation["grouping"] = _agent_slot(copy.deepcopy(adaptation["grouping"]))
    _, rebased = _moved(created, adaptation_payload=adaptation)
    assert rebased["changed_fields"] == ["adaptation"]
    life.accept("identity_rebased", **rebased)
    assert life.snapshot.grouper_kind == "agent_cli"
    assert _roles(life.snapshot)["grouper"] == {"kind": "agent_cli"}
    # The Reflector's and the Editor's kinds come from the identity in force.
    _, moved = _moved(life.envelopes[-1].payload, identity=_reflector_kind(life, "agent"))
    life.accept("identity_rebased", **moved)
    assert _roles(life.snapshot)["reflector"] == {"kind": "agent"}
    assert life.snapshot.grouper_kind == "agent_cli"


def _reflector_kind(life, kind: str) -> dict[str, Any]:
    identity = copy.deepcopy(dict(life.snapshot.identity))
    identity["reflector"]["kind"] = kind
    return identity


@pytest.mark.slow
async def test_grouper_kind_follows_the_h8_authorized_grouper_change(tmp_path, monkeypatch):
    """The H8 genuine cell: round 1 falls back in full, the operator authorizes
    a Grouper change (`max_output_tokens`) with `rebase`, round 2 suspends
    `grouper_failing`. The kind in force is still `chat`, read from the
    rebased policy; the refresh of round 2 is reported as performed."""

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_grouper_run import _chain_client, _chain_run

    _chain_client(monkeypatch, "garbage", "garbage")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await _chain_run(tmp_path, monkeypatch, 2)
    changed = {"max_output_tokens": 4096}
    with pytest.raises(EvolveRefused) as refused:
        await _chain_run(tmp_path, monkeypatch, 2, grouper=changed)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "a smaller Grouper answer budget",
        now=NOW,
    )
    _, snapshot = await _chain_run(
        tmp_path, monkeypatch, 2, grouper=changed, authorizations=[decision]
    )
    assert [entry["changed_fields"] for entry in snapshot.rebases] == [["adaptation"]]
    assert snapshot.suspensions[-1]["reason"] == "grouper_failing"
    assert snapshot.grouper_kind == "chat"
    assert _roles(snapshot)["grouper"] == {"kind": "chat"}
    refresh = _settings(snapshot)["baseline_refresh"]
    assert refresh["every_rounds"] == 1
    assert [(entry["round"], entry["completed"]) for entry in refresh["performed"]] == [(2, True)]
    # Round 2 (the refresh round) is open on its refresh measurement.
    assert refresh["count"] == 0


# -- the `settings` block over genuine refresh prefixes (plan choice 10) ----------


@pytest.mark.slow
async def test_settings_are_off_when_the_policy_refreshes_nothing(off_life):
    assert _settings(off_life.snapshot)["baseline_refresh"] == "off"


@pytest.mark.slow
async def test_the_count_after_zero_one_and_two_closed_rounds(two_records):
    assert _settings(_life(two_records[:1]).snapshot)["baseline_refresh"] == {
        "every_rounds": 2,
        "count": 0,
        "performed": [],
    }
    assert _settings(_life(_first_close(two_records)).snapshot)["baseline_refresh"]["count"] == 1
    assert _settings(_life(two_records).snapshot)["baseline_refresh"]["count"] == 2


@pytest.mark.slow
async def test_a_bound_refresh_replaces_the_count_and_is_reported(due_life):
    life = due_life
    assert _settings(life.snapshot)["baseline_refresh"]["count"] == 1
    # PDR-0016: a `continue` between rounds resets nothing.
    life.continue_block()
    assert _settings(life.snapshot)["baseline_refresh"]["count"] == 1
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    refresh = _settings(life.snapshot)["baseline_refresh"]
    assert refresh == {
        "every_rounds": 1,
        "count": 0,
        "performed": [
            {
                "round": 2,
                "measurement_id": "m-r02-refresh",
                "replaced_measurement_id": "m-r00-champion",
                "completed": False,
            }
        ],
    }


@pytest.mark.slow
async def test_a_completed_refresh_is_reported_completed(due_life):
    life = due_life
    mid = _measure_refresh(life)
    assert _settings(life.snapshot)["baseline_refresh"]["count"] == 0
    reference = _complete(life, 2, mid)
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    refresh = _settings(life.snapshot)["baseline_refresh"]
    assert refresh["count"] == 0
    assert [entry["completed"] for entry in refresh["performed"]] == [True]


@pytest.mark.slow
async def test_a_rebaseline_supersedes_the_refresh_and_resets_the_count(due_life):
    life = due_life
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    life.continue_block(effect="rebaseline")
    assert life.snapshot.rebaseline_from_round == 2
    refresh = _settings(life.snapshot)["baseline_refresh"]
    # The rebaseline round binds its fresh id: nothing is counted on it, and
    # the superseded refresh stays reported as bound, never completed.
    assert refresh["count"] == 0
    assert [entry["completed"] for entry in refresh["performed"]] == [False]


def test_stability_and_round_stop_follow_a_rebaseline_that_changes_them():
    created = _created14(stability="unstable", round_stop="all_attempts", refresh=2)
    life = Family14Life([_row(created)])
    assert _settings(life.snapshot) == {
        "stability": "unstable",
        "round_stop": "all_attempts",
        "baseline_refresh": {"every_rounds": 2, "count": 0, "performed": []},
    }
    target = _created14(stability="stable", round_stop="first_accepted", refresh=0)
    identity = copy.deepcopy(created["identity"])
    identity["policy"] = target["identity"]["policy"]
    identity["policy_digest"] = target["identity"]["policy_digest"]
    applied, rebased = _moved(created, identity=identity, effect="rebaseline")
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    assert _settings(life.snapshot) == {
        "stability": "stable",
        "round_stop": "first_accepted",
        "baseline_refresh": "off",
    }


# -- the `connection` object (C§6.3, plan choice 11, ruling Q1) ------------------


def _life14() -> Family14Life:
    return Family14Life([_row(_created14())])


def _metric(life) -> str:
    return str(life.snapshot.epochs[-1]["policy"]["metric"]["primary"])


def test_no_operation_is_not_started():
    assert _connection(_life14()) == {
        "state": "not_started",
        "binding_digest": None,
        "operation_id": None,
        "attempts": 0,
        "outcome": None,
        "detail": None,
    }


def test_an_open_start_is_running():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    assert _connection(life) == {
        "state": "running",
        "binding_digest": BINDING,
        "operation_id": OP,
        "attempts": 1,
        "outcome": None,
        "detail": None,
    }


def test_a_compatible_terminal_is_compatible():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(FINISHED, op=OP, **_finished())
    block = _connection(life)
    assert (block["state"], block["outcome"], block["detail"]) == ("compatible", "compatible", None)


def test_an_interrupted_terminal_stays_interrupted_after_the_campaign_finishes():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(INTERRUPTED, op=OP, **_interrupted())
    expected = {
        "state": "interrupted",
        "binding_digest": BINDING,
        "operation_id": OP,
        "attempts": 1,
        "outcome": "interrupted",
        "detail": None,
    }
    assert _connection(life) == expected
    life.accept(
        "campaign_finished",
        outcome="stopped",
        reason="cancelled",
        continuation={"kind": "linked_run", "reason": "cancelled"},
        evidence=None,
    )
    assert life.snapshot.finished is not None
    # The campaign terminal keeps the last probe outcome.
    assert _connection(life) == expected


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            _failed(),
            {
                "code": "connection_field_missing",
                "metric": None,
                "manifest_path": "scorer.metric_contract.target_fields[0]",
                "observed_path": "events[event=assistant_message,last]/content",
            },
        ),
        (
            _failed(
                detail_code="connection_scorer_output_invalid",
                manifest_path="scorer",
                observed_path="scorer.output",
                cause_code="output_invalid",
            ),
            {
                "code": "connection_scorer_output_invalid",
                "metric": None,
                "manifest_path": "scorer",
                "observed_path": "scorer.output",
            },
        ),
        (_unavailable(), {"cause_code": "target_failed"}),
        (
            _unavailable(status="connection_preflight_inconclusive", cause_code="turn_budget"),
            {"cause_code": "turn_budget"},
        ),
    ],
)
def test_a_failed_terminal_runs_until_its_suspension(payload, detail):
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    seq = life.accept(FAILED, op=OP, **payload)
    if "metric" in detail:
        detail = {**detail, "metric": _metric(life)}
    block = _connection(life)
    # C§6.3: a failed terminal whose owed suspension is not yet appended is `running`.
    assert block == {
        "state": "running",
        "binding_digest": BINDING,
        "operation_id": OP,
        "attempts": 1,
        "outcome": payload["status"],
        "detail": detail,
    }
    life.accept("campaign_suspended", **_suspension(seq, payload["status"]))
    assert _connection(life) == {**block, "state": "suspended"}


def test_a_resume_after_a_suspension_probes_again():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    seq = life.accept(FAILED, op=OP, **_unavailable())
    life.accept("campaign_suspended", **_suspension(seq, "connection_preflight_unavailable"))
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    block = _connection(life)
    assert (block["state"], block["operation_id"], block["attempts"]) == ("running", "cp-0002", 2)
    assert (block["outcome"], block["detail"]) == (None, None)


def test_attempts_count_interrupted_starts():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(INTERRUPTED, op=OP, **_interrupted(reason="interrupted"))
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    life.accept(FINISHED, op="cp-0002", **_finished("cp-0002"))
    block = _connection(life)
    assert (block["state"], block["operation_id"], block["attempts"]) == (
        "compatible",
        "cp-0002",
        2,
    )


def test_a_second_binding_after_a_continue():
    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(FINISHED, op=OP, **_finished())
    applied, rebased = _change(life.envelopes[0].payload, target=True, effect="continue")
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    # The block keeps naming the latest operation until a new start.
    assert _connection(life)["binding_digest"] == BINDING
    other = "c" * 64
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002", binding_digest=other))
    block = _connection(life)
    assert (block["state"], block["binding_digest"], block["attempts"]) == ("running", other, 2)
    seq = life.accept(FAILED, op="cp-0002", **_failed("cp-0002"))
    # The metric is the primary metric of the operation's own epoch identity.
    assert life.snapshot.connection.operations[-1]["epoch"] == 1
    assert _connection(life)["detail"]["metric"] == _metric(life)
    life.accept("campaign_suspended", **_suspension(seq, op="cp-0002"))
    assert _connection(life)["state"] == "suspended"


def test_the_metric_is_the_failed_operations_epochs_not_the_latest():
    """Plan choice 11 pinned: a `rebaseline` that changes the primary metric
    after a failed probe's suspension, before the next start, leaves the block
    naming that operation; its `metric` is its own epoch identity's."""

    from evolution.tree.campaign_policy import policy_class_for_payload

    life = _life14()
    life.accept(STARTED, op=OP, **_started(life))
    seq = life.accept(FAILED, op=OP, **_failed())
    life.accept("campaign_suspended", **_suspension(seq))
    created = life.envelopes[0].payload
    identity = copy.deepcopy(created["identity"])
    identity["policy"]["metric"]["primary"] = "other_metric"
    policy = policy_class_for_payload(identity["policy"]).from_payload(identity["policy"])
    identity["policy_digest"] = policy.digest
    applied, rebased = _moved(created, identity=identity, effect="rebaseline")
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    snapshot = life.snapshot
    assert [epoch["policy"]["metric"]["primary"] for epoch in snapshot.epochs] == [
        "score",
        "other_metric",
    ]
    assert snapshot.connection.operations[-1]["epoch"] == 0
    block = _connection(life)
    assert (block["state"], block["operation_id"]) == ("suspended", OP)
    assert block["detail"]["metric"] == "score"


# -- Task 6: genuine family-14 runs return validated public documents ------------


def _documents(result) -> tuple[dict[str, Any], dict[str, Any]]:
    """The run's `result` and `status` payloads, each round-tripped through its bytes."""

    from evolution.tree.campaign_projections import CampaignProjection

    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert isinstance(result.result, CampaignProjection)
    assert isinstance(result.status, CampaignProjection)
    for projection in (result.result, result.status):
        raw = projection.encode()
        assert CampaignProjection.from_bytes(raw).encode() == raw
    return result.result.to_payload(), result.status.to_payload()


def _campaign_kinds(document) -> list[str]:
    return [item["kind"] for item in document["caveats"] if item.get("round", 0) is None]


def _assert_family14_document(document, *, stability, round_stop, observe, probed=True):
    """The family-14 shape (plan choice 2) and its campaign-level items.

    `probed`: the run executed a rollout locally, so its connection preflight
    ran (a wholly inherited baseline whose round stalls runs none, H12a F1/F4).
    """

    from evolution.tree.final_report import (
        CONFIDENCE_REPORT_KEYS14,
        ESTIMATE_KEYS14,
        validate_final_entries,
    )

    assert document["schema_version"] == 1 and document["alpha"] is None
    validate_roles_block(document["roles"])
    validate_settings_block(document["settings"])
    validate_connection_block(document["connection"])
    assert (document["settings"]["stability"], document["settings"]["round_stop"]) == (
        stability,
        round_stop,
    )
    # The genuine run probed its Target and found it compatible.
    connection = document["connection"]
    assert (connection["state"], connection["outcome"], connection["detail"]) == (
        ("compatible", "compatible", None) if probed else ("not_started", None, None)
    )
    assert (connection["attempts"] >= 1) is probed
    assert document["roles"]["grouper"]["kind"] == "fields"
    for entry in document["rounds"]:
        assert "cohort" not in entry
        validate_final_entries(entry["final"])
        for final in entry["final"]:
            assert final["stability"] == stability
        for attempt in entry["attempts"]:
            assert attempt["alpha_total"] is None
            block = attempt["confidence"]
            if block is None:
                continue
            assert set(block) == CONFIDENCE_REPORT_KEYS14
            for look in block["evaluated"]:
                assert set(look["focus"]) == ESTIMATE_KEYS14
                assert (look["focus"]["exact"] is None) == observe
    expected = ["observe_gate_not_enforced", "uncalibrated_point_estimates"] if observe else []
    if stability == "stable":
        expected.append("champion_not_contemporaneous")
    assert _campaign_kinds(document) == expected


def _genuine_config(tmp_path, monkeypatch, mode, outcome):
    """`test_genuine_acceptance_cells`' configurations (first-accepted run module)."""

    from tests.campaign_drivers import (
        OBSERVE,
        WORSE_SCORER,
        _cohort_scorer,
        _flag_on_finalists_bound,
        _statistical,
    )

    config = dict(OBSERVE if mode == "observe" else _statistical(mode))
    flag = tmp_path / "final.flag"
    if outcome == "no-accepted-finalist":
        _flag_on_finalists_bound(monkeypatch, flag)
        config["scorer"] = _cohort_scorer(
            flag, flip=mode in ("point", "bound"), critical=mode in ("observe", "off")
        )
    elif outcome == "no-finalist":
        if mode == "observe":
            flag.touch()
            config["scorer"] = _cohort_scorer(flag, critical=True)
        else:
            config["scorer"] = WORSE_SCORER
    return config


@pytest.mark.slow
@pytest.mark.parametrize("stability", ["unstable", "stable"])
@pytest.mark.parametrize("round_stop", ["all_attempts", "first_accepted"])
@pytest.mark.parametrize("mode", ["observe", "bound"])
@pytest.mark.parametrize("outcome", ["winner", "no-accepted-finalist", "no-finalist"])
async def test_a_genuine_family_14_run_returns_validated_documents(
    tmp_path, monkeypatch, stability, round_stop, mode, outcome
):
    from tests.family14_public import family14_public_run

    config = _genuine_config(tmp_path, monkeypatch, mode, outcome)
    first_accepted = round_stop == "first_accepted"
    run, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": stability, "round_stop": round_stop},
        campaign={
            "attempts_per_round": 3 if first_accepted and outcome == "winner" else 1,
            "parallel_attempts": 1,
        },
    )
    observe = mode == "observe"
    for document in _documents(result):
        _assert_family14_document(
            document, stability=stability, round_stop=round_stop, observe=observe
        )
        (entry,) = document["rounds"]
        if outcome == "no-finalist":
            assert entry["final"] == [] and entry["outcome"] == "stalled"
            continue
        (final,) = entry["final"]
        assert final["champion_side"] == ("stored" if stability == "stable" else "fresh")
        roles = [member["role"] for member in final["members"]]
        if stability == "stable":
            assert final["stored_measurement_id"] == "m-r00-champion"
            assert roles == ["finalist"]
        else:
            assert final["stored_measurement_id"] is None
            assert roles == ["champion", "finalist"]
        (contrast,) = final["contrasts"]
        assert contrast["accepted"] is (outcome == "winner")
        assert final["selection"] is not None
        assert entry["outcome"] == ("promoted" if outcome == "winner" else "retained")
        if not observe:
            # A statistical comparison binds its margin on every Attempt block.
            (attempt,) = entry["attempts"]
            assert attempt["confidence"]["margin"] == 0
    result_document, status_document = _documents(result)
    assert result_document["caveats"] == status_document["caveats"]
    for name in ("connection", "roles", "settings", "rounds"):
        assert result_document[name] == status_document[name]


@pytest.mark.slow
async def test_a_refresh_run_reports_its_performed_refreshes_and_count(tmp_path, monkeypatch):
    """§15.12: N = 1 over three rounds refreshes rounds 2 and 3."""

    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    _, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        NO_FINALISTS,
        family14_settings={"refresh": 1},
        campaign={"rounds": 3},
    )
    for document in _documents(result):
        assert document["settings"]["baseline_refresh"] == {
            "every_rounds": 1,
            "count": 1,
            "performed": [
                {
                    "round": 2,
                    "measurement_id": "m-r02-refresh",
                    "replaced_measurement_id": "m-r00-champion",
                    "completed": True,
                },
                {
                    "round": 3,
                    "measurement_id": "m-r03-refresh",
                    "replaced_measurement_id": "m-r02-refresh",
                    "completed": True,
                },
            ],
        }
        assert [entry["baseline_measurement_id"] for entry in document["rounds"]] == [
            "m-r00-champion",
            "m-r02-refresh",
            "m-r03-refresh",
        ]


@pytest.mark.slow
async def test_a_run_without_refresh_reports_it_off(tmp_path, monkeypatch):
    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    _, result = await family14_public_run(
        tmp_path, monkeypatch, NO_FINALISTS, campaign={"rounds": 2}
    )
    for document in _documents(result):
        assert document["settings"]["baseline_refresh"] == "off"


@pytest.mark.slow
@pytest.mark.parametrize("cell", ["exact", "drawn"])
async def test_genuine_statistical_attempt_blocks_and_contrasts(tmp_path, monkeypatch, cell):
    """R21-R25, R28: a deterministic scorer under `measured_noise` gives exact
    looks, a zero calibration and an exact global contrast; a varying one
    gives drawn ones. One `exact_contrast` item per Attempt (plan choice 13)."""

    from tests.campaign_drivers import OBSERVE, _statistical
    from tests.family14_public import family14_public_run

    if cell == "exact":
        gate = {"causal": {"confidence": 0.9, "tolerance": "measured_noise"}, "global": "bound"}
        config = {**OBSERVE, "gate": gate, "repeats": 4}
        settings = {"margin": 0.1}
    else:
        config, settings = _statistical("bound"), None
    _, result = await family14_public_run(tmp_path, monkeypatch, config, family14_settings=settings)
    exact = cell == "exact"
    for document in _documents(result):
        _assert_family14_document(
            document, stability="unstable", round_stop="all_attempts", observe=False
        )
        (entry,) = document["rounds"]
        (attempt,) = entry["attempts"]
        block = attempt["confidence"]
        assert block["margin"] == (0.1 if exact else 0)
        looks = block["evaluated"]
        assert [look["look"] for look in looks] == [3, 4]
        for look in looks:
            focus = look["focus"]
            assert focus["exact"] is exact
            if exact:
                assert focus["lower"] == focus["point"]
                assert focus["upper"] in (None, focus["point"])
            assert ("exact_contrast" in look["caveats"]) is exact
        final_look = looks[-1]
        assert (final_look["decision"], final_look["reason"]) == ("finalist", "gate_passed")
        assert final_look["causal"]["exact"] is exact
        assert ("noise_not_observed" in final_look["caveats"]) is exact
        if exact:
            assert block["calibration"] == {
                "allowance": 0.0,
                "degenerate": True,
                "unavailable": None,
            }
        (final,) = entry["final"]
        (contrast,) = final["contrasts"]
        assert (contrast["accepted"], contrast["reason"], contrast["exact"]) == (
            True,
            "global_bound_positive",
            exact,
        )
        if exact:
            assert contrast["lower"] == contrast["point"]
        attempt_kinds = [
            item["kind"] for item in document["caveats"] if item.get("attempt_id") == "r01-a01"
        ]
        assert attempt_kinds.count("exact_contrast") == int(exact)
        assert ("noise_not_observed" in attempt_kinds) is exact


@pytest.mark.slow
async def test_an_exact_global_contrast_alone_yields_its_exact_contrast_item(tmp_path, monkeypatch):
    """A drawn look and an exact global contrast (the finalist equals the
    Champion repeat by repeat): the item comes from the contrast alone."""

    from tests.family14_public import family14_public_run

    config = _genuine_config(tmp_path, monkeypatch, "bound", "no-accepted-finalist")
    _, result = await family14_public_run(tmp_path, monkeypatch, config)
    for document in _documents(result):
        (entry,) = document["rounds"]
        (attempt,) = entry["attempts"]
        assert not any(
            "exact_contrast" in look["caveats"] for look in attempt["confidence"]["evaluated"]
        )
        (contrast,) = entry["final"][0]["contrasts"]
        assert (contrast["exact"], contrast["point"], contrast["lower"]) == (True, 0, 0)
        items = [item for item in document["caveats"] if item["kind"] == "exact_contrast"]
        assert items == [{"kind": "exact_contrast", "round": 1, "attempt_id": "r01-a01"}]


@pytest.mark.slow
@pytest.mark.parametrize(
    ("round_stop", "kind"),
    [("all_attempts", "candidate"), ("all_attempts", "champion"), ("first_accepted", "candidate")],
)
async def test_a_final_member_drift_after_a_continue_is_a_final_side(
    tmp_path, monkeypatch, round_stop, kind
):
    """U2 carry, genuine: cut after the final's stage 1, the Target's settings
    change and a `continue` re-binds the unfinished members into epoch 1; the
    armed member then renders another map. `read_drift_items` names the side
    `final_finalist` on the Attempt's item, or `final_champion` on a round
    item, and both public documents carry it (plan choice 14)."""

    from evolution.tree.affected_report import read_drift_items
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import (
        EDITED,
        STATISTICAL,
        _assert_component_selection,
        _cut_after_cohort_stage,
        _map_of_the_tree,
        _ResettableTaggedDrifts,
    )
    from tests.campaign_fakes import Crash
    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_audit import audit
    from tests.test_tree_campaign_family14_gate_audit import journal_consistent

    adapter = _ResettableTaggedDrifts(components=_map_of_the_tree, used=EDITED["used"])
    adapter.kind = kind
    options = {"adapter": adapter, "family14_settings": {"round_stop": round_stop}}
    with monkeypatch.context() as patch:
        _cut_after_cohort_stage(patch, 1)
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, STATISTICAL, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_public_run(tmp_path, monkeypatch, STATISTICAL, **options)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-10T00:00:00Z",
        effect="continue",
    )
    adapter.on = True
    run, result = await family14_public_run(
        tmp_path, monkeypatch, STATISTICAL, authorizations=[decision], **options
    )
    attempt = _assert_component_selection(run)
    if kind == "candidate":
        item = {"attempt_id": attempt, "sides": ["final_finalist"]}
    else:
        item = {"attempt_id": None, "sides": ["final_champion"]}
    drift = {"kind": "components_changed_by_condition", "round": 1, **item}
    assert read_drift_items(run, journal_consistent(run)) == {1: (drift,)}
    for document in _documents(result):
        assert [i for i in document["caveats"] if i["kind"] == drift["kind"]] == [drift]
        # The `continue` is shown on the final entry it changed (PDR-0016).
        (final,) = document["rounds"][0]["final"]
        assert final["condition_changed_in_place"] is True
        assert [c["kind"] for c in document["caveats"]].count("condition_changed_in_place") == 1
    assert audit(run)["mismatches"] == []


def _suspended_status(result, reason, owner, evidence_kind) -> dict[str, Any]:
    from evolution.tree.campaign_projections import CampaignProjection

    assert (result.terminal, result.code, result.owner) == ("suspended", reason, owner)
    assert result.result is None
    raw = result.status.encode()
    assert CampaignProjection.from_bytes(raw).encode() == raw
    status = result.status.to_payload()
    assert status["suspended"] is True
    entry = status["suspensions"][-1]
    assert (entry["reason"], entry["owner"], entry["evidence_kind"]) == (
        reason,
        owner,
        evidence_kind,
    )
    validate_connection_block(status["connection"])
    return status


@pytest.mark.slow
async def test_a_genuine_final_incomplete_suspension_is_a_family_14_document(tmp_path, monkeypatch):
    """Plan choice 15: `final_incomplete` with `final_rows`, any threshold owner."""

    from tests.campaign_drivers import EDITED, LENIENT, _arm_on_finalists_bound, _CohortFaults
    from tests.family14_public import family14_public_run

    adapter = _CohortFaults(faults={("champion", "c2", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"indeterminate": LENIENT, "sample_retries": 0}}
    _, result = await family14_public_run(tmp_path, monkeypatch, **options)
    status = _suspended_status(result, "final_incomplete", "rollout", "final_rows")
    (final,) = status["rounds"][0]["final"]
    assert [(row["measurement_id"], row["owner"]) for row in final["listed"]] == [
        ("m-r01-final-01-champion", "rollout")
    ]
    adapter.faults.clear()
    _, result = await family14_public_run(tmp_path, monkeypatch, **options)
    for document in _documents(result):
        assert [entry["reason"] for entry in document["suspensions"]] == ["final_incomplete"]
        assert document["suspended"] is False


@pytest.mark.slow
async def test_a_genuine_champion_incomplete_suspension_is_a_family_14_document(
    tmp_path, monkeypatch
):
    """#194, as landed: `champion_incomplete` with `champion_rows`."""

    from tests.campaign_drivers import LENIENT, SERIAL
    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_champion_gap import _cut_stop, _GapTarget
    from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS

    adapter = _GapTarget(tmp_path / "run", CHAMPION0)
    await _cut_stop(tmp_path, monkeypatch, adapter)
    options = {
        "adapter": adapter,
        "family14_settings": {"refresh": 0},
        "campaign": {"rounds": 1, "indeterminate": LENIENT, **SERIAL},
    }
    _, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    _suspended_status(result, "champion_incomplete", "target", "champion_rows")
    adapter.faults.clear()
    _, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    for document in _documents(result):
        assert [entry["reason"] for entry in document["suspensions"]] == ["champion_incomplete"]


@pytest.mark.slow
async def test_a_genuine_connection_suspension_shows_the_connection_object(tmp_path, monkeypatch):
    """C§6.3: the unavailable probe's class and `cause_code`, then a repaired resume."""

    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_champion_gap import _GapTarget
    from tests.test_tree_campaign_family14_refresh_run import CHAMPION0

    adapter = _GapTarget(tmp_path / "run", CHAMPION0, faults={})
    adapter.fail_probe = True
    _, result = await family14_public_run(tmp_path, monkeypatch, adapter=adapter)
    reason = "connection_preflight_unavailable"
    status = _suspended_status(result, reason, "connection", "stage_record")
    connection = status["connection"]
    assert (connection["state"], connection["outcome"]) == ("suspended", reason)
    assert connection["detail"] == {"cause_code": "infrastructure"}
    adapter.fail_probe = False
    _, result = await family14_public_run(tmp_path, monkeypatch, adapter=adapter)
    for document in _documents(result):
        connection = document["connection"]
        assert (connection["state"], connection["outcome"], connection["detail"]) == (
            "compatible",
            "compatible",
            None,
        )
        assert connection["attempts"] == 2
        assert [entry["reason"] for entry in document["suspensions"]] == [reason]


@pytest.mark.slow
async def test_a_genuine_grouper_suspension_is_a_family_14_document(tmp_path, monkeypatch):
    from tests.family14_public import family14_public_run
    from tests.model_grouping_fakes import ScriptedClient, chat_grouper_section, use_chat_grouper
    from tests.test_tree_campaign_family14_grouper_run import _provider_script

    use_chat_grouper(monkeypatch, ScriptedClient(_provider_script("outage", monkeypatch)))
    _, result = await family14_public_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )
    status = _suspended_status(result, "grouper_provider_failed", "grouper", "stage_record")
    assert status["roles"]["grouper"] == {"kind": "chat"}


@pytest.mark.slow
async def test_a_genuine_stable_document_refuses_a_missing_champion_item(tmp_path, monkeypatch):
    """U2 carry (PDR-0028): a document showing `stable` in `settings` or in a
    final entry carries `champion_not_contemporaneous`."""

    from evolution.tree.campaign_projections import CampaignProjection
    from evolution.tree.storage.envelope import SchemaError
    from tests.family14_public import family14_public_run

    _, result = await family14_public_run(
        tmp_path, monkeypatch, family14_settings={"stability": "stable"}
    )
    for document in _documents(result):
        assert (document["rounds"][0]["final"][0]["stability"]) == "stable"
        stripped = copy.deepcopy(document)
        stripped["caveats"] = [
            item for item in document["caveats"] if item["kind"] != "champion_not_contemporaneous"
        ]
        with pytest.raises(SchemaError):
            CampaignProjection(stripped)
        # Shown by the final entry alone.
        stripped["settings"]["stability"] = "unstable"
        with pytest.raises(SchemaError):
            CampaignProjection(stripped)
        # Neither shows it: admitted without the item.
        for entry in stripped["rounds"]:
            entry["final"] = []
        CampaignProjection(stripped)


# -- Task 7: the lineage block of a family-14 successor (H12a links) ---------------


def _linked_created(
    *, family, baseline=None, condition_changed=(), lineage=(), stability="unstable"
):
    """A family-14 creation (N = 2) whose identity carries `linked_from` as H12a
    writes it: first round 2, no alpha, the successor-family context."""

    from tests.test_tree_campaign_identity import _linked_from

    created = _created14(refresh=2, stability=stability)
    created["identity"]["linked_from"] = _linked_from(
        predecessor_family=family,
        first_round=2,
        alpha_rounds={},
        condition_changed=tuple(condition_changed),
        lineage_caveats=tuple(lineage),
        baseline=baseline,
        successor_family=14,
    ).to_payload()
    created["identity_digest"] = hashlib.sha256(canonical_json(created["identity"])).hexdigest()
    return created


def _linked_result(life) -> dict[str, Any]:
    """The family-14 result document of a creation-only (or rebased) successor."""

    from evolution.tree.campaign_projections import CampaignProjection, project_result

    projection = project_result(life.snapshot, tries={})
    raw = projection.encode()
    assert CampaignProjection.from_bytes(raw).encode() == raw
    return projection.to_payload()


def _declaration(**overrides):
    from tests.test_tree_campaign_identity import DECLARATION

    return {**DECLARATION, **overrides}


def test_a_13_to_14_fresh_link_renders_its_condition_change():
    """R70: every 13 -> 14 link reports `identity.policy_digest`, and a changed
    contract `identity.metric_contract`; a fresh link has no baseline block."""

    changed = ("identity.metric_contract", "identity.policy_digest")
    life = Family14Life([_row(_linked_created(family=13, condition_changed=changed))])
    document = _linked_result(life)
    assert document["linked_from"] == {
        "run_id": "predecessor",
        "family": 13,
        "first_round": 2,
        "rounds_closed": 1,
        "alpha_rounds": {},
        "lineage_caveats": [],
        "baseline": None,
    }
    assert document["caveats"] == [
        {"kind": "condition_changed", "from_run": "predecessor", "fields": list(changed)}
    ]
    # ADR-0032: a linked successor's count starts at 0 at its first round.
    assert document["settings"]["baseline_refresh"] == {
        "every_rounds": 2,
        "count": 0,
        "performed": [],
    }
    assert document["rounds"] == [] and document["alpha"] is None


def test_a_14_to_14_inherit_link_renders_a_bare_metric_contract_and_lineage():
    """H12a: a family-14 declaration may hold the bare `metric_contract`, and a
    family-14 predecessor's lineage caveats carry over."""

    from evolution.tree.campaign_caveats import LineageCaveat

    lineage = (LineageCaveat("baseline_inherited", 1, ("identity.target.digest",), "older"),)
    baseline = _declaration(fields=["metric_contract"])
    created = _linked_created(family=14, baseline=baseline, lineage=lineage)
    document = _linked_result(Family14Life([_row(created)]))
    linked = document["linked_from"]
    assert (linked["family"], linked["first_round"], linked["alpha_rounds"]) == (14, 2, {})
    assert linked["lineage_caveats"] == [caveat.to_payload() for caveat in lineage]
    assert linked["baseline"] == {
        "mode": "inherit",
        "imported": baseline["imported"],
        "scheduled": baseline["scheduled"],
        "abandoned": False,
    }
    (inherited,) = document["caveats"]
    assert inherited["kind"] == "baseline_inherited"
    assert inherited["fields"] == ["metric_contract"]
    assert document["settings"]["baseline_refresh"]["count"] == 0


def test_an_abandoned_inherit_renders_its_authorization():
    """A `rebaseline` before the first round abandons the declared import
    (comparable-links spec §5.3): the block names the decision and the
    `baseline_inherited` caveat is withdrawn."""

    from evolution.tree.campaign_policy import policy_class_for_payload

    created = _linked_created(family=14, baseline=_declaration(), stability="stable")
    life = Family14Life([_row(created)])
    identity = copy.deepcopy(created["identity"])
    identity["policy"]["gate"]["stability"] = "unstable"
    policy = policy_class_for_payload(identity["policy"]).from_payload(identity["policy"])
    identity["policy_digest"] = policy.digest
    applied, rebased = _moved(created, identity=identity, effect="rebaseline")
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    abandoned = life.snapshot.baseline_abandoned
    assert abandoned is not None
    document = _linked_result(life)
    baseline = document["linked_from"]["baseline"]
    assert (baseline["abandoned"], baseline["authorization_id"]) == (
        True,
        abandoned["authorization_id"],
    )
    # The creation ran `stable`, so its campaign item stays (any identity of
    # the run, as `ran_observe` reads the mode).
    assert [item["kind"] for item in document["caveats"]] == [
        "condition_changed_in_place",
        "champion_not_contemporaneous",
    ]
    assert document["settings"]["stability"] == "unstable"
    assert document["settings"]["baseline_refresh"]["count"] == 0


async def _public_successor(
    tmp_path, monkeypatch, predecessor, config=None, *, baseline, **options
):
    """H12a's `successor14` through the public driver: `(run, CampaignResult)`."""

    from tests.campaign_drivers import OBSERVE
    from tests.family14_public import family14_public_run
    from tests.test_tree_campaign_family14_link_run import edit_document, linked

    with monkeypatch.context() as patch:
        if baseline == "fresh":
            edit_document(patch, lambda d: d["linked_from"].update(baseline="fresh"))
        return await family14_public_run(
            tmp_path / "succ",
            patch,
            config or OBSERVE,
            linked_from=linked(predecessor),
            **options,
        )


def _assert_linked(document, *, source, baseline, changed):
    linked = document["linked_from"]
    assert (linked["family"], linked["first_round"], linked["alpha_rounds"]) == (source, 2, {})
    if baseline == "fresh":
        assert linked["baseline"] is None
    else:
        block = linked["baseline"]
        assert (block["mode"], block["abandoned"]) == ("inherit", False)
        assert block["imported"] > 0
    lineage = [item for item in document["caveats"] if "from_run" in item]
    assert [item["kind"] for item in lineage] == (
        (["condition_changed"] if changed else [])
        + (["baseline_inherited"] if baseline == "inherit" else [])
    )
    if changed:
        assert lineage[0]["fields"] == changed


@pytest.mark.slow
@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
@pytest.mark.parametrize("source", [14])
async def test_a_genuine_linked_successor_renders_its_link(tmp_path, monkeypatch, source, baseline):
    """§15.7 through the public documents: 13 -> 14 reports the contract and the
    policy (R70), 14 -> 14 nothing; `alpha_rounds` is `{}`."""

    from tests.campaign_drivers import OBSERVE, _uncut
    from tests.test_tree_campaign_family14_link_run import predecessor14

    if source == 13:
        predecessor = await _uncut(tmp_path / "pred", monkeypatch, OBSERVE)
    else:
        predecessor = await predecessor14(tmp_path, monkeypatch)
    _, result = await _public_successor(tmp_path, monkeypatch, predecessor, baseline=baseline)
    changed = ["identity.metric_contract", "identity.policy_digest"] if source == 13 else None
    for document in _documents(result):
        _assert_linked(document, source=source, baseline=baseline, changed=changed)
        # A carried baseline whose round stalls runs nothing locally (H12a F1).
        _assert_family14_document(
            document,
            stability="unstable",
            round_stop="all_attempts",
            observe=True,
            probed=baseline == "fresh",
        )
        assert [entry["round"] for entry in document["rounds"]] == [2]


@pytest.mark.slow
@pytest.mark.parametrize("change", ["stable", "unstable"])
async def test_a_genuine_link_that_changes_stability(tmp_path, monkeypatch, change):
    """A 14 -> 14 link that changes `stability` reports `identity.policy_digest`;
    the successor's documents show its own stability, final entries and
    campaign items. H12a's critical-veto setup: the predecessor's round
    stalls, so `c1` still fails and the successor measures a final."""

    from tests.campaign_drivers import STATISTICAL, VARYING_SCORER, WORSE_SCORER
    from tests.test_tree_campaign_family14_link_run import _c1_veto, predecessor14
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    other = "unstable" if change == "stable" else "stable"
    predecessor = await predecessor14(
        tmp_path,
        monkeypatch,
        {**NO_FINALISTS, "scorer": _c1_veto(WORSE_SCORER)},
        family14_settings={"stability": other},
    )
    _, result = await _public_successor(
        tmp_path,
        monkeypatch,
        predecessor,
        {**STATISTICAL, "scorer": _c1_veto(VARYING_SCORER)},
        baseline="fresh",
        family14_settings={"stability": change},
    )
    changed = ["identity.policy_digest", "identity.scorer.digest"]
    for document in _documents(result):
        _assert_linked(document, source=14, baseline="fresh", changed=changed)
        _assert_family14_document(
            document, stability=change, round_stop="all_attempts", observe=False
        )
        (entry,) = document["rounds"]
        assert entry["final"]
        for final in entry["final"]:
            assert final["champion_side"] == ("stored" if change == "stable" else "fresh")


@pytest.mark.slow
@pytest.mark.parametrize("baseline", ["fresh", "inherit"])
async def test_a_stable_successor_reports_its_own_stored_baseline(tmp_path, monkeypatch, baseline):
    """H12a's stable-successor cell: the stored Champion is the successor's own
    `m-r00-champion` (carried or measured). `epoch_of` on it neither raises nor
    flags a condition change, and its measurement reports epoch 0."""

    from tests.campaign_drivers import STATISTICAL, VARYING_SCORER, WORSE_SCORER
    from tests.test_tree_campaign_family14_link_run import _c1_veto, predecessor14
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await predecessor14(
        tmp_path,
        monkeypatch,
        {**NO_FINALISTS, "scorer": _c1_veto(WORSE_SCORER)},
        family14_settings={"stability": "stable"},
    )
    _, result = await _public_successor(
        tmp_path,
        monkeypatch,
        predecessor,
        {**STATISTICAL, "scorer": _c1_veto(VARYING_SCORER)},
        baseline=baseline,
        family14_settings={"stability": "stable"},
    )
    for document in _documents(result):
        _assert_linked(document, source=14, baseline=baseline, changed=["identity.scorer.digest"])
        (entry,) = document["rounds"]
        assert entry["final"]
        for final in entry["final"]:
            assert final["stored_measurement_id"] == "m-r00-champion"
            assert final["condition_changed_in_place"] is False
            assert all(not c["condition_changed_in_place"] for c in final["contrasts"])
        (stored,) = [m for m in document["measurements"] if m["measurement_id"] == "m-r00-champion"]
        assert stored["condition_epoch"] == 0
        assert (stored["imported"] > 0) == (baseline == "inherit")


@pytest.mark.slow
@pytest.mark.parametrize("source", [14])
async def test_a_genuine_successor_counts_its_refreshes_from_0(tmp_path, monkeypatch, source):
    """ADR-0032: the predecessor closed one `stalled` round (its count 1). The
    successor's status shows 0 at its first round (inspected at a cut after
    its `baseline_bound`) and 1 once that round closed, never the carried 2."""

    from evolution.tree.campaign_projections import inspect_view, project_status
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_link_run import _stalled_predecessor
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    predecessor = await _stalled_predecessor(tmp_path, monkeypatch, source)
    options = {"baseline": "inherit", "family14_settings": {"refresh": 2}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_baseline_bound")
        with pytest.raises(Crash):
            await _public_successor(tmp_path, patch, predecessor, NO_FINALISTS, **options)
    status = project_status(inspect_view(tmp_path / "succ" / "run")).to_payload()
    assert [entry["round"] for entry in status["rounds"]] == [2]
    assert status["settings"]["baseline_refresh"] == {
        "every_rounds": 2,
        "count": 0,
        "performed": [],
    }
    assert status["linked_from"]["alpha_rounds"] == {}
    _, result = await _public_successor(tmp_path, monkeypatch, predecessor, NO_FINALISTS, **options)
    for document in _documents(result):
        assert [entry["outcome"] for entry in document["rounds"]] == ["stalled"]
        assert document["settings"]["baseline_refresh"] == {
            "every_rounds": 2,
            "count": 1,
            "performed": [],
        }
