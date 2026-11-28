"""The interrupted fixtures retain genuine, verifiable Core stage boundaries."""
import hashlib
import json
from pathlib import Path

import pytest

from report_md_compat import with_b15_operator_progress


@pytest.mark.parametrize("scenario,scope,decision,measurement", [
    pytest.param("focus-open", "focus", None, "bound", id="focus-open-focus-None"),
    pytest.param("focus-decided-open", "focus", "comparison_not_passed", "scored",
                 id="focus-decided-open-focus-comparison_not_passed"),
    pytest.param("causal-open", "causal", "passed", "scored", id="causal-open-causal-passed"),
])
def test_interrupted_b15_capture(scenario, scope, decision, measurement):
    from evolution.tree.campaign import inspect_campaign

    directory = Path(__file__).parent / "fixtures/panel/b15" / scenario
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["core_version"] == "2.0.0b15" and not meta["preliminary"]
    assert meta["lab_version"] == "0.0.3.dev64"
    assert meta["interruption"]["hook"] and meta["interruption"]["reason"]
    for member, digest in meta["files_sha256"].items():
        assert hashlib.sha256((directory / member).read_bytes()).hexdigest() == digest
    projection = inspect_campaign(directory / "run", verify=True).to_payload()
    captured = json.loads((directory / "core-projection.json").read_text())
    # Core verifies one more precondition and emits the optional b20 baseline key for this run.
    captured["verification"]["preconditions_checked"] += 1
    captured["baseline_import"] = None
    assert projection == with_b15_operator_progress(captured, measurement)
    assert projection["verification"]["mismatches"] == []
    assert projection["independent"]["settings"]["stages"] == "focus_first"
    records = [json.loads(line) for line in (directory / "run/campaign.jsonl").read_text().splitlines()]
    stages = [row["payload"]["scope"] for row in records
              if row["event"] == "stage_opened" and "scope" in row["payload"]]
    assert stages[-1] == scope
    decisions = [row["payload"]["outcome"] for row in records if row["event"] == "focus_decided"]
    assert decisions == ([] if decision is None else [decision])
    assert not any(row["event"] in {"attempt_closed", "campaign_finished"} for row in records)
    expected_last = "focus_decided" if scenario == "focus-decided-open" else "stage_opened"
    assert records[-1]["event"] == expected_last
    for member in ("campaign.json", "owner-report.json", "report.md"):
        assert (directory / member).is_file()
