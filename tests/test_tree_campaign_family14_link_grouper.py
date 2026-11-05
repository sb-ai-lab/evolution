"""H12a: Grouper kinds across a link (Grouper addendum §8, A§10.8)."""

import pytest

from tests.model_grouping_fakes import (
    chat_grouper_section,
)
from tests.test_tree_campaign_family14_grouper_run import (
    FULL_INVALID,
    _chain_client,
    _fallbacks,
)
from tests.test_tree_campaign_family14_link_run import assert_clean, successor14
from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


async def test_a_full_fallback_chain_does_not_cross_a_link(tmp_path, monkeypatch):
    """ADR-0034 / H8 decision 7: a successor's chain is derived from its own journal."""
    with monkeypatch.context() as patch:
        _chain_client(patch, "garbage")
        pred, snap = await family14_run(
            tmp_path / "pred",
            patch,
            NO_FINALISTS,
            family14_settings={"grouper": chat_grouper_section()},
            campaign={"rounds": 1},
        )
    assert _fallbacks(pred) == [(1, FULL_INVALID)] and snap.finished is not None
    _chain_client(monkeypatch, "garbage")
    run, snapshot = await successor14(
        tmp_path,
        monkeypatch,
        pred,
        NO_FINALISTS,
        baseline="fresh",
        family14_settings={"grouper": chat_grouper_section()},
        campaign={"rounds": 1},
    )
    assert _fallbacks(run) == [(2, FULL_INVALID)]
    assert not payloads(run, "campaign_suspended")
    assert_clean(run, snapshot)
