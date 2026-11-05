"""#287 in family 15: a run secret in the instance description, at the `/16` preflight.

Family 15 inherits `connection_preflight_failed` and `instance_attested` from the
family-14 grammar (`family15_schema.INHERITED_EVENTS`), so the `/16` campaign
refuses a description the same way: the probe ends `connection_incompatible /
connection_description_secret` before the baseline, the campaign suspends with
owner `connection`, and the same command resumes once `describe()` is repaired.
Every run is the genuine two-case `/16` campaign (`independent_run`) under a
Scorer that forwards the judge model the description reports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.campaign_drivers import EDITED, audit_mismatches
from tests.test_tree_campaign_family14_connection_run import (
    CONNECTION_EVENTS,
    JUDGE,
    JUDGE_PATH,
    REPAIRED,
    ConnectedAdapter,
    assert_nowhere,
    forward_judge,
    measurement_starts,
    names,
    payloads,
)

pytestmark = pytest.mark.slow


async def description_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: Any
) -> tuple[Path, Any, ConnectedAdapter]:
    from tests.family15_fakes import independent_run

    adapter = ConnectedAdapter(used=EDITED["used"], description=description)
    with monkeypatch.context() as patch:
        forward_judge(patch)
        run, result = await independent_run(tmp_path, patch, adapter=adapter)
    return run, result, adapter


async def test_a_description_secret_suspends_the_campaign_before_the_baseline(
    tmp_path, monkeypatch
):
    run, result, adapter = await description_run(tmp_path, monkeypatch, {"model": JUDGE})
    assert payloads(run, "campaign_created")[0]["family_version"] == 15
    assert (result.terminal, result.owner, result.code) == (
        "suspended",
        "connection",
        "connection_incompatible",
    )
    events = names(run)
    assert events[-2:] == ["connection_preflight_failed", "campaign_suspended"]
    (failed,) = payloads(run, "connection_preflight_failed")
    assert (
        failed["detail_code"],
        failed["manifest_path"],
        failed["observed_path"],
        failed["cause_code"],
    ) == ("connection_description_secret", JUDGE_PATH, "target.description", None)
    assert "stage_opened" not in events and "instance_attested" not in events
    assert not any(measurement_starts(run).values())
    assert adapter.materialized == [] and adapter.live == {}
    connection = result.status.to_payload()["connection"]
    assert connection["detail"]["code"] == "connection_description_secret"
    assert connection["detail"]["manifest_path"] == JUDGE_PATH
    assert audit_mismatches(run) == []
    assert_nowhere(run, JUDGE)


async def test_resume_after_repairing_describe_finishes(tmp_path, monkeypatch):
    await description_run(tmp_path, monkeypatch, {"model": JUDGE})
    run, result, _ = await description_run(tmp_path, monkeypatch, REPAIRED)
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert [e for e in names(run) if e in CONNECTION_EVENTS] == [
        "connection_preflight_started",
        "connection_preflight_failed",
        "connection_preflight_started",
        "connection_preflight_finished",
    ]
    attested = payloads(run, "instance_attested")
    assert attested and all(p["description"] == REPAIRED for p in attested)
    assert audit_mismatches(run) == []
    assert_nowhere(run, JUDGE)
