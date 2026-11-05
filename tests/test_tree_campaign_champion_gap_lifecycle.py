"""#194 Task 4: the lifecycle admits `champion_incomplete`, grants its pairs and
restores their tries (spec §2 G2, G3, G6 and §3.3; plan choices 4, 6, 7, 9, 11).

The prefixes are genuine admitted runs cut where a Champion measurement is
still open: family 13's seed baseline before its `baseline_complete` (the
`observe_barrier` chain of `test_tree_campaign_family13_cohort`), family 14's
round-2 refresh before its own `baseline_complete`, and H8's family-14 Grouper
prefix for the outstanding-cause twin of G6. The records offered after the cut
are synthetic, as in the family-13 cohort listing tests. Family 12 is out of
scope: its lifecycle refuses the reason.
"""

from __future__ import annotations

import asyncio

import pytest

from evolution.tree.campaign_schema import SchemaError
from evolution.tree.champion_gaps import champion_rows_evidence
from tests.campaign_drivers import _live_prefix
from tests.campaign_drivers import observe_barrier as observe_barrier

SEED = "m-r00-champion"
REFRESH = "m-r02-refresh"


def _before_baseline_complete(records, call=1):
    """`records` cut just before their `call`-th `baseline_complete`, and that record."""

    found = [i for i, record in enumerate(records) if record["event"] == "baseline_complete"]
    index = found[call - 1]
    return records[:index], records[index]


@pytest.fixture(scope="module")
def seed_records(tmp_path_factory):
    """Family 13, observe, `sample_retries: 2`: the seed baseline is bound and
    its one stage opened over (c1, 01) and (c2, 01); `baseline_complete` is
    the record the cut leaves out."""

    from tests.campaign_drivers import EDITED

    records = asyncio.run(
        _live_prefix(
            tmp_path_factory.mktemp("champion-gap-seed"),
            gate={"mode": "observe"},
            repeats=1,
            campaign={"sample_retries": 2},
            **EDITED,
        )
    )
    return records


def _seed(records):
    from tests.campaign_fakes import LifecycleFixture

    prefix, _ = _before_baseline_complete(records)
    life = LifecycleFixture(prefix, transcode=False)
    assert life.state.policy.sample_retries == 2
    assert not life.snapshot.measurements[SEED]["complete"]
    return life


def _champion_row(ordinal, *, mid=SEED, case_id="c1", owner="target"):
    return {
        "measurement_id": mid,
        "case_id": case_id,
        "sample_id": "01",
        "retry_ordinal": ordinal,
        "owner": owner,
    }


def _listing(life, *rows, owner="target", mid=SEED):
    """Admit a `champion_incomplete` suspension listing `rows`; returns its sequence."""

    return life.accept(
        "campaign_suspended",
        reason="champion_incomplete",
        owner=owner,
        evidence=champion_rows_evidence(mid, list(rows)),
    )


def _refused(rule):
    return pytest.raises(SchemaError, match=f"^{rule}$")


# -- admission -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def refresh_records(tmp_path_factory):
    """Family 14, N = 1, two rounds: round 2's refresh is bound and staged; the
    cut is right after its `baseline_complete`, which `_refresh` leaves out."""

    from tests.campaign_drivers import SERIAL, _records
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_refresh_recovery import _cut_after_append
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS
    from tests.test_tree_campaign_family14_run import family14_run

    root = tmp_path_factory.mktemp("champion-gap-refresh")

    async def run():
        with pytest.MonkeyPatch.context() as patch:
            _cut_after_append(patch, "baseline_complete", call=2)
            with pytest.raises(Crash):
                await family14_run(
                    root,
                    patch,
                    NO_FINALISTS,
                    family14_settings={"refresh": 1},
                    campaign={"rounds": 2, **SERIAL},
                )
        return _records(root / "run" / "campaign.jsonl")

    return asyncio.run(run())


def _refresh(records):
    from tests.campaign_fakes import Family14Life

    prefix, _ = _before_baseline_complete(records, call=2)
    life = Family14Life(prefix)
    state = life.snapshot.rounds[2]
    assert (state.baseline_measurement_id, state.baseline_complete) == (REFRESH, False)
    assert life.snapshot.refreshes[-1]["completed"] is None
    return life


def test_an_open_family_14_refresh_is_listed(refresh_records):
    life = _refresh(refresh_records)
    seq = _listing(life, _champion_row(0, mid=REFRESH), mid=REFRESH)
    assert life.snapshot.threshold_named[REFRESH][("c1", "01")] == (0, seq)
    # Below the limit (`sample_retries: 1`), the cancelled try is restored.
    assert life.snapshot.listing_restored == {(REFRESH, "c1", "01", 0)}


def _rebound(seed_records):
    """The seed baseline re-bound by an accepted `continue` into epoch 1, whose
    first stage then opens (which ends the block)."""

    life = _seed(seed_records)
    ((_, rebound),) = life.continue_block([SEED])
    life.stage(rebound, 1, 1)
    assert life.snapshot.pending_change is None
    assert life.snapshot.measurements[SEED]["frozen"]
    return life, rebound


# -- G6's lifecycle twin (plan choice 9) ------------------------------------------------

OWED = "a champion listing waits for every owed record"


async def test_no_listing_while_a_grouper_suspension_is_owed(tmp_path, monkeypatch):
    """H8 (family 14): a Grouper `role_failed` owes its suspension, an
    outstanding `suspension_causes` entry; G6 is checked before the measurement."""

    from tests.test_tree_campaign_family14_grouper import _family14_life, _role_failed, _suspend

    life = await _family14_life(tmp_path, monkeypatch, "chat")
    seq = _role_failed(life, "provider_exhausted")
    with _refused(OWED):
        _listing(life, _champion_row(0))
    _suspend(life, "grouper_provider_failed", seq)
    with _refused("the Champion measurement is already complete"):
        _listing(life, _champion_row(0))


# -- the grant (G2, plan choice 4) -----------------------------------------------------


# -- restored tries (G3, plan choice 6) ------------------------------------------------


# -- unchanged: family 13's cohort listing -----------------------------------------------


# -- The producing-version gate ----------------------------------------------------------
