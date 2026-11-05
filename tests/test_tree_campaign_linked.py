"""I2 Task 11: linked runs from a campaign whose condition changed in place (spec §6.3).

A successor compares its identity with the predecessor's *latest* identity --
the condition the predecessor ended under -- not its creation record; the
predecessor's in-place changes travel in the bounded lineage aggregate as one
`condition_changed_in_place` item (`{kind, runs, fields, nearest_run}`, like
every other lineage kind); and `baseline: inherit` reads the champion
measurement the predecessor names, whatever its epoch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign import IdentityChanged
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from tests.campaign_fakes import (
    Rig,
    all_offline_campaign_fixtures,  # noqa: F401  (a pytest fixture)
    v8_policy,
)
from tests.test_tree_campaign import champion_path, successor_editor
from tests.test_tree_campaign_condition_change import (
    PowerCut,
    SettingsAdapter,
    accept,
    audit,
    cut_stage,
)

pytestmark = pytest.mark.slow

IN_PLACE = "condition_changed_in_place"


def _successor(
    first: Rig,
    tmp_path: Path,
    settings: str,
    name: str = "second",
    *,
    schema: str = SCHEMA_V15,
    **kw: Any,
) -> Rig:
    second = Rig(
        tmp_path,
        campaign_policy=kw.pop("campaign_policy", None)
        or v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(settings),
        editor=successor_editor(),
        run_name=name,
        schema=schema,
    )
    second.seed_root = champion_path(first)
    return second


def _linked(rig: Rig) -> dict[str, Any]:
    linked = rig.records()[0]["payload"]["identity"]["linked_from"]
    assert linked is not None
    return dict(linked)


async def test_a_change_free_predecessor_carries_no_in_place_lineage(tmp_path: Path) -> None:
    first = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        run_name="first",
    )
    assert (await first.run()).finished is not None
    second = _successor(first, tmp_path, "settings-v1")

    await second.run(linked_from=first.run_root)

    assert "lineage_caveats" not in _linked(second)


async def test_a_baseline_is_inherited_from_a_rebound_seed_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No round promotes, and the seed's baseline was re-bound by a `continue`:
    the measurement that names the champion is `m-r00-champion@e1`, complete;
    the frozen `m-r00-champion` it was carried from is not."""

    from tests.test_tree_campaign_condition_change import Flat

    first = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        scorer=Flat(),
        run_name="first",
    )
    cut_stage(monkeypatch, "m-r00-champion")
    with pytest.raises(PowerCut):
        await first.run()
    monkeypatch.undo()
    with pytest.raises(IdentityChanged) as refused:
        await first.resume(adapter=SettingsAdapter("settings-v2"))
    await first.resume(
        adapter=SettingsAdapter("settings-v2"), authorizations=[accept(refused.value)]
    )
    snapshot = first.snapshot()
    assert snapshot.finished is not None
    assert all(state.closed["outcome"] != "promoted" for state in snapshot.rounds.values())
    second = _successor(first, tmp_path, "settings-v2")

    result = await second.run(linked_from=first.run_root, linked_baseline="inherit")

    assert result.terminal == "finished"
    declared = _linked(second)["baseline"]
    assert declared["source_measurement_id"] == "m-r00-champion@e1"
    assert declared["imported"] > 0
    assert audit(second) == []


async def test_a_linked_run_whose_own_budget_is_spent_offers_no_rebaseline(
    tmp_path: Path,
) -> None:
    """Task 4 deferred minor, over a real journal: a successor starting at round 2
    with `rounds: 1` whose round's baseline is bound has no round left."""

    first = Rig(
        tmp_path,
        campaign_policy=v8_policy(rounds=1, attempts_per_round=1, sample_retries=0),
        adapter=SettingsAdapter(),
        run_name="first",
    )
    assert (await first.run()).finished is not None
    second = _successor(first, tmp_path, "settings-v1")

    def crash() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await second.run(linked_from=first.run_root, _hooks={"after_baseline_bound": crash})
    snapshot = second.snapshot()
    assert snapshot.first_round == 2 and list(snapshot.rounds) == [2]

    with pytest.raises(IdentityChanged) as target:
        await second.resume(linked_from=first.run_root, adapter=SettingsAdapter("settings-v2"))
    assert target.value.effects == ("continue", "close")
    with pytest.raises(IdentityChanged) as scorer:
        await second.resume(
            linked_from=first.run_root, scorer=second.scorer_with_identity_digest("0" * 64)
        )
    assert scorer.value.effects == ("close",)


def test_two_in_place_changes_fold_into_one_run_with_the_union_of_fields() -> None:
    from evolution.tree.campaign_caveats import LineageCaveat, aggregate_lineage

    changes = [
        dict(seq=4, effect="continue", fields=["target"], round=1),
        dict(seq=9, effect="rebaseline", fields=["environment", "target"], round=1),
    ]
    inherited = dict(
        run_id="c-first",
        lineage_caveats=[
            dict(kind=IN_PLACE, runs=2, fields=["scorer"], nearest_run="c-zero"),
        ],
    )

    assert aggregate_lineage(inherited, "c-second", predecessor_changes=changes) == (
        LineageCaveat(IN_PLACE, 3, ("environment", "scorer", "target"), "c-second"),
    )
    assert aggregate_lineage(inherited, "c-second") == (
        LineageCaveat(IN_PLACE, 2, ("scorer",), "c-zero"),
    )
    assert aggregate_lineage(None, "c-second") == ()
    # A family-6 successor drops the kind,
    # the predecessor's own changes and an inherited item alike.
    assert (
        aggregate_lineage(inherited, "c-second", predecessor_changes=changes, in_place=False) == ()
    )
