"""H11 Task 7: a family-14 `role_visibility` change offers only `close`.

PDR-0023 and ADR-0029: a visibility change offers only `close`, so no frozen
role state (a reflection's input digest, an Attempt's frozen common input,
any free-form role output) is ever carried under a new projection digest.
PDR-0023 narrows PDR-0010's "a non-comparable identity change is authorizable
with `rebase`" for exactly this field.

Three backstops (H11 routine choice 9), each tested here:

- the identity-change producer forces `close` whatever else changed;
- `campaign_epochs.admissible_effects` returns `("close",)` for family 14;
- the family-14 shape of `identity_rebased` refuses `identity.role_visibility`
  in `changed_fields`, plain or inside an in-place `continue`/`rebaseline`
  block.

`_check_condition_changed_close` is deliberately unchanged: at this base it
only ends a `rebaseline` block's round closure, and no in-place block can be
admitted for a visibility change (the producer never offers one and the shape
refuses its rebase), so it never meets one.

Genuine runs change `role_visibility.readable_paths` between invocations by
wrapping `tests.test_tree_campaign_family14_run.family14_document` (routine
choice 10); the shared drivers stay untouched.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from evolution.tree.campaign_schema import SchemaError, validate_payload
from evolution.tree.execution.authorizations import (
    Authorization,
    identity_differences,
    identity_instance_digest,
    identity_record,
)
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import EDITED, _cut_hook, _records, _Resettable
from tests.campaign_fakes import Crash, Family14Life
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

FIELD = "identity.role_visibility"
BEFORE = ("prompts/system.md",)
AFTER = ("prompts/**", "docs/**")
NOW = "2026-10-12T00:00:00Z"


def _readable(monkeypatch, readable):
    """Declare `readable` as `role_visibility.readable_paths` for later invocations.

    Wraps whatever `family14_document` is in force, so a second call moves the
    declaration again: the outermost wrapper wins.
    """

    from tests import test_tree_campaign_family14_run as driver

    document = driver.family14_document

    def family14_document(*args, **kwargs):
        moved = document(*args, **kwargs)
        moved["role_visibility"]["readable_paths"] = list(readable)
        return moved

    monkeypatch.setattr(driver, "family14_document", family14_document)


def _journal(run: Path) -> bytes:
    return (run / "campaign.jsonl").read_bytes()


def _decision(refused, effect, reason="accept the visibility change"):
    return Authorization.new(
        "run_identity_changed", str(refused.instance_digest), reason, now=NOW, effect=effect
    )


def _version(monkeypatch, version):
    from evolution.tree import campaign as campaign_module

    monkeypatch.setattr(campaign_module, "resolve_package_version", lambda: version)


async def _refused(tmp_path, monkeypatch, **options):
    from evolution.tree.evolve import EvolveRefused

    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, **options)
    return refused.value


def _assert_close_only(refused, *fields):
    assert refused.code == "run_identity_changed"
    # Soft: an instance digest an operator can quote.
    assert refused.instance_digest is not None
    assert refused.effects == ("close",)
    assert refused.effect == "close"
    assert sorted(difference.field for difference in refused.differences) == sorted(
        (FIELD, *fields)
    )


def _assert_closed_by_visibility(run: Path, before: bytes, snapshot) -> None:
    """The close wrote exactly `authorization_applied(close)` + `campaign_finished`."""

    after = _journal(run)
    assert after.startswith(before)
    appended = [record["event"] for record in _records(run / "campaign.jsonl")][
        len(_records_of(before)) :
    ]
    assert appended == ["authorization_applied", "campaign_finished"]
    (applied,) = payloads(run, "authorization_applied")
    assert (applied["effect"], applied["code"]) == ("close", "run_identity_changed")
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == (
        "stopped",
        "condition_changed",
    )
    assert not payloads(run, "identity_rebased")
    assert not snapshot.rebases
    assert len(snapshot.epochs) == 1
    assert audit(run)["mismatches"] == []


def _records_of(data: bytes) -> list[bytes]:
    return [line for line in data.splitlines() if line]


# -- genuine refusals ----------------------------------------------------------


#: Each cut leaves different role state open when the visibility moves.
CUTS = {
    "between_rounds": ("after_round_closed", 1),
    "open_grouping": ("after_grouping_started", 1),
    "open_reflection": ("after_reflection_started", 1),
    "open_editor": ("after_editor_started", 1),
    "open_attempt": ("after_comparison_bound", 1),
}


@pytest.mark.parametrize("cut", sorted(CUTS))
async def test_a_visibility_change_is_refused_close_only_wherever_it_lands(
    tmp_path, monkeypatch, cut
):
    _readable(monkeypatch, BEFORE)
    hook, call = CUTS[cut]
    with monkeypatch.context() as patch:
        _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    run = tmp_path / "run"
    before = _journal(run)
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, campaign={"rounds": 2})
    _assert_close_only(refused)
    assert _journal(run) == before


async def test_a_visibility_change_beside_an_agent_side_change_is_close_only(tmp_path, monkeypatch):
    """A Target change alone offers `continue` first; with visibility, only `close`."""

    adapter = _Resettable(used=EDITED["used"])
    options = {"adapter": adapter, "campaign": {"rounds": 2}}
    _readable(monkeypatch, BEFORE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    before = _journal(run)
    adapter.settings = "settings-v2"
    alone = await _refused(tmp_path, monkeypatch, **options)
    assert alone.effects[0] == "continue"
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    _assert_close_only(refused, "identity.target.settings_hash")
    assert _journal(run) == before


async def test_a_visibility_change_beside_a_producing_version_change_is_close_only(
    tmp_path, monkeypatch
):
    """A version-only change offers `rebase`; with visibility, only `close`."""

    _readable(monkeypatch, BEFORE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    run = tmp_path / "run"
    before = _journal(run)
    with monkeypatch.context() as patch:
        _version(patch, "0.3.0a99")
        alone = await _refused(tmp_path, monkeypatch, campaign={"rounds": 2})
        assert alone.effects == ("rebase",)
        assert [d.field for d in alone.differences] == ["producing_version"]
        _readable(patch, AFTER)
        refused = await _refused(tmp_path, monkeypatch, campaign={"rounds": 2})
    _assert_close_only(refused, "producing_version")
    assert _journal(run) == before


async def test_a_visibility_change_beside_a_policy_change_is_close_only(tmp_path, monkeypatch):
    """A larger round budget alone offers `rebaseline` first; with visibility, only `close`."""

    _readable(monkeypatch, BEFORE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    run = tmp_path / "run"
    before = _journal(run)
    alone = await _refused(tmp_path, monkeypatch, campaign={"rounds": 3})
    assert alone.effects[0] == "rebaseline"
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, campaign={"rounds": 3})
    assert refused.instance_digest is not None
    assert refused.effects == ("close",)
    fields = {difference.field for difference in refused.differences}
    assert {FIELD, "identity.policy_digest"} <= fields
    assert _journal(run) == before


async def test_a_visibility_change_inside_an_applied_block_is_hard(tmp_path, monkeypatch):
    """An applied `continue` block is finished only for the identity it recorded.

    Moving the visibility after the block's `authorization_applied` is the
    hard `authorized_identity_changed` (I2 §5.4): no decision can carry the
    block's role state across it.
    """

    adapter = _Resettable(used=EDITED["used"])
    run, options = await _between_rounds(tmp_path, monkeypatch, adapter)
    adapter.settings = "settings-v2"
    refused = await _refused(tmp_path, monkeypatch, **options)
    decision = _decision(refused, "continue", reason="accept the changed target")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_condition_applied")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, authorizations=[decision], **options)
    assert [record["event"] for record in _records(run / "campaign.jsonl")][-1] == (
        "authorization_applied"
    )
    before = _journal(run)
    _readable(monkeypatch, AFTER)
    hard = await _refused(tmp_path, monkeypatch, authorizations=[decision], **options)
    assert hard.code == "authorized_identity_changed"
    assert hard.instance_digest is None
    assert _journal(run) == before


async def test_a_visibility_change_on_a_finished_campaign_stays_hard(tmp_path, monkeypatch):
    _readable(monkeypatch, BEFORE)
    run, snapshot = await family14_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] == "completed"
    before = _journal(run)
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch)
    assert refused.code == "run_identity_changed"
    assert refused.instance_digest is None
    assert _journal(run) == before


# -- decisions -----------------------------------------------------------------


async def _between_rounds(tmp_path, monkeypatch, adapter=None, *, hook="after_round_closed"):
    options = {"campaign": {"rounds": 2}}
    if adapter is not None:
        options["adapter"] = adapter
    _readable(monkeypatch, BEFORE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, hook)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    return tmp_path / "run", options


@pytest.mark.parametrize("cut", ["after_round_closed", "after_reflection_started"])
@pytest.mark.parametrize("effect", [None, "default", "close"])
async def test_an_effectless_default_or_close_decision_closes_the_campaign(
    tmp_path, monkeypatch, effect, cut
):
    """Between rounds, or over a reflection the cut left open (which the close
    does not settle or re-run: the run ends at the owed terminal)."""

    run, options = await _between_rounds(tmp_path, monkeypatch, hook=cut)
    before = _journal(run)
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    decision = _decision(refused, effect)
    run, snapshot = await family14_run(tmp_path, monkeypatch, authorizations=[decision], **options)
    _assert_closed_by_visibility(run, before, snapshot)
    (applied,) = payloads(run, "authorization_applied")
    assert applied["instance_digest"] == refused.instance_digest
    assert applied["authorization"]["id"] == decision.id


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
@pytest.mark.parametrize("agent_side", [False, True])
async def test_any_other_effect_is_refused_at_effect_before_any_write(
    tmp_path, monkeypatch, effect, agent_side
):
    """`continue` and `rebaseline` are the other nameable effects.

    A decision cannot name `rebase` at all (`Authorization` refuses it as
    `authorization_invalid at effect` when it is written): a rebase is chosen
    only by `None`/`default` on an instance that offers it, and these resolve
    to `close` here.
    """

    from evolution.tree.evolve import EvolveRefused

    adapter = _Resettable(used=EDITED["used"])
    run, options = await _between_rounds(tmp_path, monkeypatch, adapter)
    before = _journal(run)
    ledger = (run / "artifacts.jsonl").read_bytes()
    if agent_side:
        adapter.settings = "settings-v2"
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    _assert_close_only(refused, *(("identity.target.settings_hash",) if agent_side else ()))
    with pytest.raises(EvolveRefused) as invalid:
        await family14_run(
            tmp_path, monkeypatch, authorizations=[_decision(refused, effect)], **options
        )
    assert (invalid.value.code, invalid.value.path) == ("authorization_invalid", "effect")
    assert _journal(run) == before
    assert (run / "artifacts.jsonl").read_bytes() == ledger


async def test_rebaseline_over_a_contradicted_policy_is_refused_under_a_visibility_change_too(
    tmp_path, monkeypatch
):
    """A round budget below the rounds already begun, plus a visibility change.

    Without the visibility change the contradicted policy withholds
    `rebaseline`, and a decision naming it is the hard
    `policy_contradicts_prefix`. With it the instance offers only `close`, and
    the same decision is still the hard `policy_contradicts_prefix`, as in
    families 8-13 (PDR-0016; H11 review B1): that refusal precedes the check
    of the chosen effect against the offered ones. Neither writes a byte.
    """

    from evolution.tree.evolve import EvolveRefused

    _readable(monkeypatch, BEFORE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, campaign={"rounds": 3})
    run = tmp_path / "run"
    assert len(payloads(run, "round_closed")) == 2
    before = _journal(run)
    ledger = (run / "artifacts.jsonl").read_bytes()
    shrunk = {"campaign": {"rounds": 1}}
    alone = await _refused(tmp_path, monkeypatch, **shrunk)
    assert "rebaseline" not in alone.effects
    with pytest.raises(EvolveRefused) as contradicted:
        await family14_run(
            tmp_path, monkeypatch, authorizations=[_decision(alone, "rebaseline")], **shrunk
        )
    assert contradicted.value.code == "policy_contradicts_prefix"
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **shrunk)
    assert refused.effects == ("close",)
    assert {FIELD, "identity.policy_digest"} <= {d.field for d in refused.differences}
    with pytest.raises(EvolveRefused) as closed_too:
        await family14_run(
            tmp_path, monkeypatch, authorizations=[_decision(refused, "rebaseline")], **shrunk
        )
    assert closed_too.value.code == "policy_contradicts_prefix"
    assert _journal(run) == before
    assert (run / "artifacts.jsonl").read_bytes() == ledger


async def test_family_14_non_visibility_rebase_is_still_offered(tmp_path, monkeypatch):
    """Control: the narrowing is the visibility field's, not every family-14 rebase."""

    run, options = await _between_rounds(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        _version(patch, "0.3.0a99")
        refused = await _refused(tmp_path, monkeypatch, **options)
        assert refused.effects == ("rebase",)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, authorizations=[_decision(refused, None)], **options
        )
    assert snapshot.finished["outcome"] == "completed"
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == ["producing_version"]


# -- the closed run's historical role inputs -----------------------------------


def _files(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


async def test_a_visibility_close_leaves_every_frozen_role_input_untouched(tmp_path, monkeypatch):
    run, options = await _between_rounds(tmp_path, monkeypatch)
    before = _journal(run)
    files = _files(run)
    frozen = {
        event: payloads(run, event)
        for event in (
            "reflection_started",
            "reflection_finished",
            "attempt_input_frozen",
            "editor_started",
            "editor_finished",
        )
    }
    assert all(frozen.values())
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, authorizations=[_decision(refused, "default")], **options
    )
    _assert_closed_by_visibility(run, before, snapshot)
    for event, recorded in frozen.items():
        assert payloads(run, event) == recorded
    now = _files(run)
    for name, content in files.items():
        if name == "campaign.jsonl":
            continue
        assert now.get(name) == content, name


# -- H6-F3: the applied visibility close cut ------------------------------------


async def test_a_crash_after_the_applied_visibility_close_finishes_on_resume(tmp_path, monkeypatch):
    """H6-F3: the same command writes the owed terminal state-first, and the
    recovered result is assembled (the public driver, plan choice 17)."""

    from evolution.tree.campaign_projections import CampaignProjection, inspect_view
    from tests.family14_public import family14_public_run

    run, options = await _between_rounds(tmp_path, monkeypatch)
    before = _journal(run)
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    decision = _decision(refused, "default")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_close_applied")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, authorizations=[decision], **options)
    assert [record["event"] for record in _records(run / "campaign.jsonl")][-1] == (
        "authorization_applied"
    )
    # The same command again: the owed terminal is written state-first.
    run, result = await family14_public_run(
        tmp_path, monkeypatch, authorizations=[decision], **options
    )
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert isinstance(result.result, CampaignProjection)
    raw = result.result.encode()
    assert CampaignProjection.from_bytes(raw).encode() == raw
    _assert_closed_by_visibility(run, before, inspect_view(run).snapshot)


# -- lifecycle backstops over synthetic journals --------------------------------


def _created14():
    from tests.test_tree_family14_identity import _created14 as created

    return created()


def _row(payload):
    return {
        "schema_version": 1,
        "run_id": "run",
        "sequence": 1,
        "timestamp": NOW,
        "event": "campaign_created",
        "operation_id": None,
        "payload": payload,
    }


def _triple(record):
    return {
        name: record[name] for name in ("identity_digest", "producing_version", "adaptation_digest")
    }


def _change(created, *, visibility=False, target=False, reflector=False, effect=None):
    """`(authorization_applied, identity_rebased)` payloads moving `created`'s identity.

    Both are consistent with each other and the creation (digests, instance,
    decision id, `changed_fields`), so only a rule about the moved fields can
    refuse them.
    """

    identity = copy.deepcopy(created["identity"])
    if visibility:
        identity["role_visibility"] = "e" * 64
    if target:
        identity["target"]["settings_hash"] = "settings-v2"
    if reflector:
        identity["reflector"]["digest"] = "f" * 64
    digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    journaled = identity_record(created)
    observed = identity_record({**created, "identity": identity, "identity_digest": digest})
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
        adaptation=created["adaptation"],
        previous_digest=created["identity_digest"],
        changed_fields=[d.field for d in identity_differences(journaled, observed)],
        instance_digest=instance,
        authorization=decision.journal_payload(),
        adaptation_payload=created["adaptation_payload"],
    )
    return applied, rebased


REFUSAL = "^identity_rebased changes role_visibility$"
#: A shape refusal at the envelope: `Envelope.from_payload` keeps no detail.
ENVELOPE = "^invalid journal record$"


def test_the_family_14_shape_refuses_a_rebase_that_moves_role_visibility():
    created = _created14()
    _, rebased = _change(created, visibility=True)
    assert rebased["changed_fields"] == [FIELD]
    with pytest.raises(SchemaError, match=REFUSAL):
        validate_payload("identity_rebased", rebased, final=True)
    _, mixed = _change(created, visibility=True, target=True, reflector=True)
    with pytest.raises(SchemaError, match=REFUSAL):
        validate_payload("identity_rebased", mixed, final=True)
    _, other = _change(created, reflector=True)
    validate_payload("identity_rebased", other, final=True)


def test_a_plain_family_14_rebase_moving_role_visibility_is_refused():
    life = Family14Life([_row(_created14())])
    _, rebased = _change(life.envelopes[0].payload, visibility=True)
    with pytest.raises(SchemaError, match=REFUSAL):
        validate_payload("identity_rebased", rebased, final=True)
    with pytest.raises(SchemaError, match=ENVELOPE):
        life.accept("identity_rebased", **rebased)
    assert len(life.envelopes) == 1
    assert not life.snapshot.rebases


def test_a_plain_family_14_rebase_of_another_field_is_admitted():
    life = Family14Life([_row(_created14())])
    _, rebased = _change(life.envelopes[0].payload, reflector=True)
    life.accept("identity_rebased", **rebased)
    assert [entry["changed_fields"] for entry in life.snapshot.rebases] == [
        ["identity.reflector.digest"]
    ]


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
def test_an_in_place_family_14_block_whose_rebase_moves_role_visibility_is_refused(effect):
    life = Family14Life([_row(_created14())])
    applied, rebased = _change(
        life.envelopes[0].payload, visibility=True, target=True, effect=effect
    )
    life.accept("authorization_applied", **applied)
    assert life.snapshot.pending_change is not None
    with pytest.raises(SchemaError, match=REFUSAL):
        validate_payload("identity_rebased", rebased, final=True)
    with pytest.raises(SchemaError, match=ENVELOPE):
        life.accept("identity_rebased", **rebased)
    assert not life.snapshot.rebases
    assert len(life.snapshot.epochs) == 1


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
def test_without_the_shape_rule_the_block_is_still_refused_by_admissible_effects(
    monkeypatch, effect
):
    """Defence in depth: `_rebase_in_place` asks `admissible_effects`, which closes."""

    from evolution.tree import campaign_schema

    monkeypatch.setattr(campaign_schema, "moves_role_visibility", lambda field: False)
    life = Family14Life([_row(_created14())])
    applied, rebased = _change(
        life.envelopes[0].payload, visibility=True, target=True, effect=effect
    )
    life.accept("authorization_applied", **applied)
    with pytest.raises(SchemaError, match="^the effect is not admissible for this change$"):
        life.accept("identity_rebased", **rebased)
    assert not life.snapshot.rebases


@pytest.mark.parametrize("effect", ["continue", "rebaseline"])
def test_an_in_place_family_14_block_of_the_target_alone_is_admitted(effect):
    life = Family14Life([_row(_created14())])
    applied, rebased = _change(life.envelopes[0].payload, target=True, effect=effect)
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    assert len(life.snapshot.epochs) == 2
