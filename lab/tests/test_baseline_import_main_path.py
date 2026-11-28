"""Lab's /16 project measures the inputs and traces Core needs for import."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from evolution_lab.quickstart import new_project
from scaffold_16 import as_manifest_16
from test_quickstart_campaign import _campaign
from test_quickstart_evaluate import _run as _evaluate

from evolution.tree import inspect_journal, read_scorer_trace
from evolution.tree.campaign import inspect_campaign
from evolution.tree.campaign_identity import metric_plan_digest

pytestmark = pytest.mark.slow


def _run_directory(summary, kind):
    operation = summary[kind]["operation_id"]
    return Path(summary["state"]) / "artifacts" / operation / operation


@pytest.fixture(scope="module")
def evaluated_project(tmp_path_factory):
    project = new_project(tmp_path_factory.mktemp("baseline-import") / "p")
    manifest = project / "manifest.json"
    document = as_manifest_16(json.loads(manifest.read_text(encoding="utf-8")))
    manifest.write_text(json.dumps(document), encoding="utf-8")
    result = _evaluate(project)
    assert result.exit_code == 0, result.summary
    return project, _run_directory(result.summary, "evaluation") / "measurement"


def test_lab_evaluation_and_campaign_bind_the_same_inputs_16(evaluated_project):
    project, measurement = evaluated_project
    result = _campaign(project, baseline="fresh")
    assert result.exit_code in (0, 1), result.summary
    evaluation = inspect_journal(measurement).records[0].payload["identity"]
    baseline = _run_directory(result.summary, "campaign") / "measurements" / "m-r00-champion"
    campaign = inspect_journal(baseline).records[0].payload["identity"]
    for field in ("candidate_digest", "scorer", "environment"):
        assert evaluation[field] == campaign[field], field

    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    primary = manifest["campaign"]["metric"]["primary"]
    assert campaign["metric_plan_digest"] == metric_plan_digest(primary)
    bare_primary = json.dumps({"primary": primary}, separators=(",", ":")).encode()
    assert evaluation["metric_plan_digest"] in {
        campaign["metric_plan_digest"],
        hashlib.sha256(bare_primary).hexdigest(),
    }


def test_fixed_evaluation_packs_scorer_traces(evaluated_project):
    _, measurement = evaluated_project
    records = inspect_journal(measurement).records
    finished = [record for record in records if record.event == "scoring_finished"]
    assert finished
    for record in finished:
        trace = read_scorer_trace(
            measurement,
            record.payload["score_request_id"],
            measurement_id=record.to_payload()["run_id"],
            sections=("request", "stdout"),
            run_boundary="nofollow",
        )
        assert trace is not None
        assert trace.layout == "pack"
        assert trace.request is not None and trace.stdout is not None


def test_lab_main_path_16(evaluated_project):
    project, _ = evaluated_project
    result = _campaign(project)
    assert result.exit_code in (0, 1), result.summary
    run = _run_directory(result.summary, "campaign")
    imported = inspect_campaign(run).to_payload()["baseline_import"]
    assert imported["imported"] == imported["scheduled"] > 0
    assert imported["authorization_id"] is None
    assert imported["held_out_authorization_id"] is None
    baseline = inspect_journal(run / "measurements" / "m-r00-champion")
    assert not any(record.event == "rollout_started" for record in baseline.records)
    records = [json.loads(line) for line in (run / "campaign.jsonl").read_bytes().splitlines()]
    declaration = next(
        record["payload"] for record in records if record["event"] == "diagnostic_import_declared"
    )
    assert declaration["sufficiency"] == "complete"

    import jsonschema
    from evolution_lab.quickstart._campaign import summary_schema
    from evolution_lab.quickstart._readout import render_campaign_readout

    jsonschema.Draft202012Validator(summary_schema()).validate(result.summary)
    assert result.summary["baseline"]["imported"] == imported["imported"]
    assert result.summary["baseline"]["to_measure"] == 0
    assert result.summary["baseline"]["not_imported"] == imported["not_imported"]
    assert "evaluation run 001" in render_campaign_readout(result.summary, result.run_dir)
    assert "evaluation run 001" in (result.run_dir / "report.md").read_text()
