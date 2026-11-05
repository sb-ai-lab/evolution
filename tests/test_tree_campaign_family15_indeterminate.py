"""Threshold windows share rate gating and exhausted-sample accounting."""

import pytest

from evolution.tree.campaign_policy import OwnerThreshold
from tests.campaign_drivers import audit_mismatches
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_campaign_family14_connection_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


def test_min_tries_delays_only_the_rate_test():
    from evolution.tree.campaign_policy import crosses

    limits = OwnerThreshold(99, "0.100000000000", min_tries=20)
    assert not crosses(1, 1, limits)
    assert not crosses(3, 19, limits)
    assert crosses(3, 20, limits, "rate")
    assert crosses(100, 1, limits, "count")


def observed(ordinal, follows, *, sample="s", owner="target"):
    return dict(
        measurement_id="m",
        case_id="c",
        sample_id=sample,
        retry_ordinal=ordinal,
        retry_follows=follows,
        owner=owner,
    )


def test_recovered_window_counts_only_in_window_tries_of_exhausted_samples():
    from evolution.tree.campaign_policy import OwnerWindow

    window = OwnerWindow()
    window.observe(observed(0, True), 1)
    assert window.count(True) == 1 and window.count(False) == 0
    window.observe(observed(1, False), 2)
    assert window.count(False) == 2
    window.observe(observed(0, True, sample="recovered"), 3)
    assert window.count(True) == 3 and window.count(False) == 2
    window.reset(3)
    window.observe(observed(2, False), 4)
    assert window.count(True) == window.count(False) == 1


def test_exhaustion_is_shared_across_owners_but_counts_are_not():
    from evolution.tree.campaign_policy import OwnerWindow

    target = OwnerWindow("target")
    scorer = OwnerWindow("scorer")
    for window in (target, scorer):
        window.observe(observed(0, True), 1)
        window.observe(observed(1, False, owner="scorer"), 2)
    assert target.count(False) == scorer.count(False) == 1


class FlakyBaseline(FakeAdapter):
    failures = 1

    def instance_for(self, candidate, instance_id, boot_id):
        from tests.campaign_fakes import CONNECTION_PROBING

        instance = super().instance_for(candidate, instance_id, boot_id)
        if not CONNECTION_PROBING.get():
            calls = []

            def lookup(session):
                if session.case_id == "c1":
                    calls.append(1)
                    if len(calls) <= self.failures:
                        return "target_failed"
                return "completed"

            instance._lookup = lookup
        return instance


@pytest.mark.parametrize("count_recovered", [True, False])
@pytest.mark.parametrize("min_tries", [1, 20])
async def test_recovered_baseline_threshold_matches_replay_and_audit(
    tmp_path, monkeypatch, count_recovered, min_tries
):
    def configure(document):
        document["campaign"]["sample_retries"] = 1
        document["campaign"]["indeterminate"]["target"] = dict(
            max_count=99,
            max_rate="0.100000000000",
            min_tries=min_tries,
            count_recovered=count_recovered,
        )

    run, result = await public_run(
        tmp_path, monkeypatch, configure=configure, adapter_class=FlakyBaseline
    )
    expected = count_recovered and min_tries == 1
    crossings = [
        p["evidence"]["crossing"]
        for p in payloads(run, "campaign_suspended")
        if p["evidence"]["evidence_kind"] == "threshold"
    ]
    assert bool(crossings) == expected
    assert result.terminal == ("suspended" if expected else "finished")
    assert payloads(run, "indeterminate_observed")
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("exhausted", [False, True])
async def test_exhausted_only_numerator_also_controls_max_count(tmp_path, monkeypatch, exhausted):
    class Adapter(FlakyBaseline):
        failures = 2 if exhausted else 1

    def configure(document):
        document["campaign"]["sample_retries"] = 1
        document["campaign"]["indeterminate"]["target"] = dict(
            max_count=1,
            max_rate="1.000000000000",
            count_recovered=False,
        )

    run, result = await public_run(
        tmp_path, monkeypatch, configure=configure, adapter_class=Adapter
    )
    crossings = [
        p["evidence"]["crossing"]
        for p in payloads(run, "campaign_suspended")
        if p["evidence"]["evidence_kind"] == "threshold"
    ]
    assert bool(crossings) == exhausted
    if exhausted:
        assert crossings[0]["count"] == 2 and crossings[0]["kind"] == "count"
        assert result.terminal == "suspended"
    else:
        assert result.terminal == "finished"
    assert audit_mismatches(run) == []


async def test_exhausted_sample_counts_only_the_repair_in_the_new_window(tmp_path, monkeypatch):
    class Adapter(FlakyBaseline):
        failures = 99

    def configure(document):
        document["campaign"]["sample_retries"] = 1
        document["campaign"]["indeterminate"]["target"] = dict(
            max_count=1,
            max_rate="1.000000000000",
            count_recovered=False,
        )

    run, first = await public_run(tmp_path, monkeypatch, configure=configure, adapter_class=Adapter)
    assert first.terminal == "suspended"
    first_observed = len(payloads(run, "indeterminate_observed"))
    assert first_observed == 2
    _, second = await public_run(tmp_path, monkeypatch, configure=configure, adapter_class=Adapter)
    assert second.terminal == "finished"
    assert len(payloads(run, "indeterminate_observed")) == 3
    assert len(payloads(run, "campaign_suspended")) == 1
    assert audit_mismatches(run) == []


async def test_unfinished_v16_without_new_threshold_keys_resumes_unchanged(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family15_delivery import PowerCut

    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_attempt_input_frozen")
    run, result = await public_run(tmp_path, monkeypatch)
    assert result.terminal == "finished"
    assert not payloads(run, "identity_rebased")
    original = payloads(run, "campaign_created")[0]["identity"]["policy"]["indeterminate"]
    assert all(
        "min_tries" not in limits and "count_recovered" not in limits
        for limits in original.values()
    )
    assert audit_mismatches(run) == []
