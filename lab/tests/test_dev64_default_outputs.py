"""No-policy dev64 output parity, with the declared additive fields and schema majors."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

import report_md_compat
from dev64_default_outputs import SCENARIOS, read_outputs


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed; it is an optional test runner (ADR-0018 p. 5)")
@pytest.mark.parametrize("scenario,fixture", SCENARIOS.items())
def test_no_policy_dev64_reader_cli_panel_bytes(scenario, fixture):
    tests = Path(__file__).parent
    saved = tests / "data/dev64-default-outputs" / scenario
    for name, value in read_outputs(tests / "fixtures/panel" / fixture / "run").items():
        if name == "campaign.json":
            assert value.pop("lab_notices_state") == "present"
        elif name == "owner-report.json":
            assert value["report"].pop("lab_notices_state") == "present"
            assert value["report"]["core"].pop("baseline_import") is None
            for measurement in value["report"]["measurements"]:
                for field in ("unanswered", "unanswered_state", "answered_reported"):
                    measurement.pop(field)
        elif name == "report.md":
            assert report_md_compat.current(value) == report_md_compat.legacy((saved / name).read_text()), name
            continue
        if name in {"campaign.json", "owner-report.json", "owner-report.txt"}:
            value = report_md_compat.without_operator_progress(value)
        actual = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        expected = (saved / name).read_bytes()
        if name in {"campaign.json", "campaign.txt", "cli-campaign.txt"}:
            expected = expected.replace(b"lab-campaign-view/14", b"lab-campaign-view/15", 1)
        if name in {"owner-report.json", "owner-report.txt"}:
            expected = expected.replace(b"lab-owner-report/12", b"lab-owner-report/15", 1)
        assert actual.encode() == expected, name


def test_dev64_reference_hashes():
    saved = Path(__file__).parent / "data/dev64-default-outputs"
    manifest = json.loads((saved / "manifest.json").read_text())
    for name, expected in manifest["files_sha256"].items():
        assert hashlib.sha256((saved / name).read_bytes()).hexdigest() == expected, name
