"""Reports/progress share aggregates, preserving task and Editor boundaries."""

import json

import pytest

pytestmark = pytest.mark.slow


async def test_general_progress_and_public_report_keep_task_text_private(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_watch import follow
    from tests.family15_fakes import independent_run

    run, _ = await independent_run(tmp_path, monkeypatch, script="private-script-canary-123")
    status = inspect_campaign(run, verify=True).to_payload()
    lines = []
    assert follow(run, emit=lines.append, once=True) == 0
    public = json.dumps(status["independent"]) + "\n".join(lines)
    for needle in (
        "private-script-canary-123",
        "hello",
        "suggested_fix",
        "counterevidence",
        "oracle",
        "case_id",
        "repeat_diagnostics",
        "retained_pairs",
    ):
        assert needle not in public
    # Public criterion-ID rules remain enforced by the existing Editor owner;
    # this report never introduces criterion/verdict/private trace input.
    assert "criterion" not in json.dumps(status["independent"])


@pytest.mark.parametrize(
    "location", ["root", "comparison", "gate_comparison", "global_comparison", "noise", "scope"]
)
async def test_closed_projection_rejects_unknown_private_fields(tmp_path, monkeypatch, location):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution.tree.storage.envelope import SchemaError
    from tests.family15_fakes import independent_run

    run, _ = await independent_run(tmp_path, monkeypatch)
    data = inspect_campaign(run).to_payload()
    report = data["independent"]
    target = report
    if location != "root":
        target = report["attempts"][0][
            location if location in {"gate_comparison", "global_comparison"} else "comparison"
        ]
    if location == "noise":
        target = target["noise"]
    if location == "scope":
        target = target["scopes"][0]
    target["private_diagnostics"] = "canary"
    with pytest.raises(SchemaError):
        CampaignProjection(data)


async def test_train_only_report_counts_and_progress_exclude_test_content(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_watch import Snapshot
    from tests import campaign_drivers as fixtures
    from tests.family15_fakes import independent_run

    original = fixtures.evolve

    async def split(path, *args, **kwargs):
        document = json.loads(path.read_text())
        corpus = path.parent / document["corpus"]["path"]
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        for row in rows:
            row["split"] = "train"
        rows.append(
            dict(
                case_id="held_out_id",
                group_id="held_out_group",
                stratum="s1",
                split="test",
                public={"text": "held-out-private-canary"},
                oracle={"secret": "oracle-canary"},
            )
        )
        corpus.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(fixtures, "evolve", split)
    run, _ = await independent_run(tmp_path, monkeypatch)
    report = inspect_campaign(run).to_payload()["independent"]
    assert report["settings"]["train_cases"] == 2
    public = json.dumps(report) + "\n".join(Snapshot.from_state(run).lines())
    assert "held_out" not in public and "held-out-private-canary" not in public
