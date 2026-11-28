"""Light operation list: summary rows, no ``result.core``, campaign family guard refreshed."""
import asyncio
from pathlib import Path

import pytest

from evolution_lab.owner.summary import SUMMARY_KEYS, operation_summary, short_outcome

from test_owner_core_raw import finished_campaign, run_finished_campaign  # noqa: F401 — module fixture
from test_owner_measurements import service_cli_with_finished_campaign  # noqa: F401 — fixture
from test_owner_status_views import finished_campaign_status  # noqa: F401 — fixture

pytestmark = pytest.mark.slow


@pytest.fixture
def finished_evaluation_status(tmp_path, monkeypatch):
    return run_finished_evaluation(tmp_path, monkeypatch)


def run_finished_evaluation(tmp_path, monkeypatch):
    """One real fixed evaluation through Owner; its finished status."""
    from evolution_lab.owner import Owner, ResourceRef
    from test_owner_evaluation import _finished, _prepared, _ready_target

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(prepared.digest, target_ref=target,
                                         secrets={"SCORER_TOKEN": "private-neutral-scorer"})
            return await _finished(owner, ResourceRef("operation", accepted.id))
    return asyncio.run(scenario())


def test_summary_keys(finished_campaign_status):
    row = operation_summary(finished_campaign_status)
    assert set(row) == SUMMARY_KEYS == {"id", "kind", "entrypoint", "control_state", "results_state", "resume",
                                        "progress", "progress_state", "times", "times_state", "times_source",
                                        "times_reason", "outcome"}
    assert row["times_source"] == "core_raw" and row["times_reason"] is None
    assert "result" not in row and "client_ref" not in row


def test_short_outcome(finished_campaign_status):
    assert set(short_outcome(finished_campaign_status)) == {"outcome", "reason", "champion"}


def test_evaluation_has_no_outcome(finished_evaluation_status):
    assert short_outcome(finished_evaluation_status) is None
    assert operation_summary(finished_evaluation_status)["outcome"] is None
    # ADR-0019: an evaluation's times come from the public measurement journal only.
    row = operation_summary(finished_evaluation_status)
    assert row["times_state"] == "present" and row["times_source"] == "core_public"
    assert row["times_reason"] is None


@pytest.fixture
def own_finished_campaign(tmp_path):
    """A campaign of its own: deleting its results must not reach the module's shared one."""
    return run_finished_campaign(tmp_path)


async def test_summary_of_deleted_results_is_unavailable(own_finished_campaign):
    from evolution_lab.owner import Owner, ResourceRef
    state, operation_id, _ = own_finished_campaign
    ref = ResourceRef("operation", operation_id)
    async with Owner.open(state) as owner:
        (before,) = await owner.list_summaries()
        assert before["progress_state"] == "present"
        await owner.wait(await owner.delete_results(ref))
        (row,) = await owner.list_summaries()
        assert row["results_state"] == "deleted"
        assert row["progress"] is None and row["progress_state"] == "unavailable"
        assert row["times"] is None and row["times_state"] == "unavailable" and row["outcome"] is None
        assert row["times_source"] is None and row["times_reason"] == "unreadable"


async def test_settled_campaign_refreshes_its_row_and_ids_filter(finished_campaign, monkeypatch):
    from evolution_lab.owner import Owner, ResourceRef, readers
    state, operation_id, _ = finished_campaign
    async with Owner.open(state) as owner:
        status = await owner.status(ResourceRef("operation", operation_id))  # warms the row
        calls = []
        real = readers.project_operation
        monkeypatch.setattr(readers, "project_operation", lambda *a, **k: calls.append(1) or real(*a, **k))
        (row,) = await owner.list_summaries()
        assert calls == [] and row == operation_summary(status)
        assert await owner.list_summaries({"other"}) == ()
        assert [r["id"] for r in await owner.list_summaries({operation_id})] == [operation_id]
        assert calls == []
        # A changed signed journal must still refresh the campaign family guard.
        journal = state / "artifacts" / operation_id / operation_id / "campaign.jsonl"
        original = journal.read_bytes()
        try:
            journal.write_bytes((Path(__file__).parent / "fixtures/campaign-family-refusal/campaign.jsonl").read_bytes())
            (changed,) = await owner.list_summaries()
            assert calls == [1] and changed["results_state"] == "unavailable"
        finally:
            journal.write_bytes(original)
        (recovered,) = await owner.list_summaries()
        assert calls == [1, 1] and recovered == row


def test_list_summary_route_and_cli(service_cli_with_finished_campaign):
    cli, route, operation = service_cli_with_finished_campaign
    listed = cli("list", "--summary")[-1]
    assert listed["schema"] == "lab-operation-summary-list/1"
    assert listed == route("/v1/operations?view=summary")
    assert [row["id"] for row in listed["operations"]] == [operation]
    assert set(listed["operations"][0]) == SUMMARY_KEYS


async def test_degraded_row_is_retried_not_reused(finished_campaign, monkeypatch):
    from evolution_lab.owner import Owner, readers
    state, _, _ = finished_campaign
    async with Owner.open(state) as owner:
        real = readers._times_and_facts

        monkeypatch.setattr(readers, "_times_and_facts", lambda *args: (readers._times_view(state="unavailable", reason="unreadable"), {}))
        (row,) = await owner.list_summaries()
        assert row["times_state"] == "unavailable"
        monkeypatch.setattr(readers, "_times_and_facts", real)
        calls = []
        project = readers.project_operation
        monkeypatch.setattr(readers, "project_operation", lambda *a, **k: calls.append(1) or project(*a, **k))
        (row,) = await owner.list_summaries()
        assert calls and row["times_state"] == "present"
        calls.clear()
        (refreshed,) = await owner.list_summaries()
        assert calls == [] and refreshed == row  # the recovered, unchanged projection is reusable
