"""Deterministic readers for the dev62/b14 default-stage compatibility baseline.

The fixture directories contain Core-written runs, without Owner databases.
Owner-only inputs are deliberately fixed here; no journal or captured data is
rewritten. Run this script with the installed dev62/b14 environment to capture
the baseline, and reuse ``read_outputs`` on the new pin for comparison.
"""
from pathlib import Path
import json

SCENARIOS = ("agent-failed", "agent-finished")


def read_outputs(run):
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.campaign_views import baseline_notice_fields, campaign_view, lineage_champions
    from evolution_lab.owner.models import ReportResult
    from evolution_lab.owner.progress import CampaignPlan, campaign_progress
    from evolution_lab.owner.measurements import measurement_rows
    from evolution_lab.report import read_campaign_report

    read = core_raw.campaign_read(run, families=core_raw.FAMILIES,
                                  with_result=True, with_bindings=True)
    assert read.family == 15
    core = read.status.to_payload()
    plan = CampaignPlan("neutral", 1, 1, "score")
    try:
        champions, trees = lineage_champions(run)
    except Exception:
        # Owner.campaign uses the same unavailable result when the captured
        # run omits the artifact ledger; do not invent an empty lineage.
        champions, trees = None, None
    rows = measurement_rows(core, "campaign", {}, plan.metric_primary)
    view = campaign_view("a" * 32, core, plan, campaign_progress(core, plan, None),
                         champions, trees, rows, core_raw.campaign_details(run, read=read),
                         baseline=baseline_notice_fields(run, read.view.snapshot))
    report = read_campaign_report(run, owner_run=True, families=core_raw.FAMILIES, read=read)
    usage = [{key: row[key] for key in ("measurement_id", "usage", "usage_state")}
             for row in rows]
    owner_report = ReportResult("a" * 32, "available", report.data, None, usage=usage).to_payload()
    return {"campaign.json": view, "owner-report.json": owner_report,
            "report.md": report.markdown()}


def main():
    from importlib.metadata import version
    assert version("evolution-lab") == "0.0.3.dev62"
    assert version("evo-skills") == "2.0.0b14"
    tests = Path(__file__).resolve().parent
    for name in SCENARIOS:
        destination = tests / "data" / "dev62-default-outputs" / name
        outputs = read_outputs(tests / "fixtures" / "panel" / "b14" / name / "run")
        destination.mkdir(parents=True, exist_ok=True)
        for filename, value in outputs.items():
            assert not (destination / filename).exists(), "Never overwrite a captured baseline"
            text = value if filename.endswith(".md") else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            (destination / filename).write_text(text)


if __name__ == "__main__":
    main()
