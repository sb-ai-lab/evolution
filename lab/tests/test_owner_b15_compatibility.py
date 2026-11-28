"""Current readers over genuine offline b15 captures; no synthesized journals."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from evolution_lab.owner import core_raw
from evolution_lab.owner.independent_view import REMAINING_KEYS, independent_view
import evolution_lab.panel as panel_package

from dev62_default_outputs import read_outputs
from report_md_compat import with_b15_operator_progress

CAPTURES = Path(__file__).parent / "fixtures/panel/b15"


def test_insufficient_evidence_campaign_is_readable(tmp_path):
    directory = CAPTURES / "insufficient-evidence"
    run = directory / "run"
    read = core_raw.campaign_read(run, families=core_raw.FAMILIES, with_result=True, with_bindings=True)
    block = read.status.to_payload()["independent"]
    outcome = lambda row: (row["outcome"], row["reason"])
    assert [outcome(a) for a in block["attempts"]] == [("insufficient_evidence", "incomplete_sample")]
    assert outcome(independent_view(block)["attempts"][0]) == outcome(block["attempts"][0])

    # Re-read through the campaign, Owner JSON-report and report.md paths rather
    # than trusting the producer's saved Lab projections.
    outputs = read_outputs(run)
    view, report, markdown = outputs["campaign.json"], outputs["owner-report.json"], outputs["report.md"]
    assert view["details_state"] == "present" and view["details_reason"] is None
    assert outcome(view["independent"]["attempts"][0]) == outcome(block["attempts"][0])
    assert view["independent"]["attempts"][0]["group"] == "infrastructure"
    assert view["independent"]["work"]["remaining"] == {
        key: block["remaining_work"][key] for key in REMAINING_KEYS
    }
    assert report["state"] == "available" and report["reason"] is None
    assert report["report"]["lab_independent_state"] == "present"
    assert outcome(report["report"]["lab_independent"]["attempts"][0]) == outcome(block["attempts"][0])
    assert "insufficient_evidence" in markdown and "incomplete_sample" in markdown

    # Node also checks the *fresh* current Python projection, so an old saved
    # campaign.json cannot hide a regression in today's reader or wire shape.
    if node := shutil.which("node"):
        live = tmp_path / "campaign.json"
        live.write_text(json.dumps(view))
        env = {**os.environ, "LAB_B15_CAMPAIGN": str(live),
               "LAB_PANEL_STATIC": str(Path(panel_package.__file__).parent / "static"),
               "LAB_PANEL_FIXTURES": str(CAPTURES.parent)}
        result = subprocess.run([node, "--test", str(Path(__file__).parent / "panel_js/b15-compatibility.test.mjs")],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


def test_b15_insufficient_evidence_capture_is_authentic_and_complete():
    from evolution.tree.campaign import inspect_campaign

    directory = CAPTURES / "insufficient-evidence"
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["executor"] == "Core ReferenceTarget over loopback HTTP; neutral subprocess roles"
    assert set(meta["producing_wheels"]) == {"lab", "core"}
    for member, digest in meta["files_sha256"].items():
        assert hashlib.sha256((directory / member).read_bytes()).hexdigest() == digest
    projection = inspect_campaign(directory / "run", verify=True).to_payload()
    assert projection["verification"]["mismatches"] == []
    expected = json.loads((directory / "core-projection.json").read_text())
    assert (expected["verification"]["preconditions_checked"],
            projection["verification"]["preconditions_checked"]) == (19, 20)
    expected["verification"]["preconditions_checked"] = 20
    expected["baseline_import"] = None
    assert projection == with_b15_operator_progress(expected, "scored")
    records = [json.loads(line) for line in (directory / "run/campaign.jsonl").read_text().splitlines()]
    closures = [r["payload"] for r in records if r["event"] == "attempt_closed"]
    assert [(r["outcome"], r["reason"]) for r in closures] == [("insufficient_evidence", "incomplete_sample")]
    assert records[-1]["event"] == "campaign_finished"


def test_focus_first_campaign_passes_through(tmp_path):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.family15_report import report_lines
    from evolution_lab.cli import responses

    directory = CAPTURES / 'focus-first'
    meta = json.loads((directory / 'meta.json').read_text())
    assert meta['fault_injections'] == []
    for member, digest in meta['files_sha256'].items():
        assert hashlib.sha256((directory / member).read_bytes()).hexdigest() == digest
    run = directory / 'run'
    core = inspect_campaign(run, verify=True).to_payload()
    assert core['verification']['mismatches'] == []
    expected = json.loads((directory / 'core-projection.json').read_text())
    assert (expected['verification']['preconditions_checked'],
            core['verification']['preconditions_checked']) == (49, 50)
    expected['verification']['preconditions_checked'] = 50
    expected['baseline_import'] = None
    assert core == with_b15_operator_progress(expected, "scored", "scored")
    outputs = read_outputs(run)
    view, report = outputs['campaign.json'], outputs['owner-report.json']
    assert view['details_state'] == 'present'
    assert view['schema'] == 'lab-campaign-view/15'
    assert view['independent']['settings']['stages'] == 'focus_first'
    assert report['schema'] == 'lab-owner-report/15'
    assert report['report']['lab_independent_state'] == 'present'
    assert report['report']['lab_independent'] == view['independent']
    assert view['independent']['work']['remaining'] == {
        key: core['independent']['remaining_work'][key] for key in REMAINING_KEYS
    }
    for actual, expected in zip(view['independent']['attempts'], core['independent']['attempts'], strict=True):
        assert actual['slots'] == expected['slots']
        assert set(actual['slots']) == {'candidate', 'parent'}
        assert actual['conditional_cost'] == expected['conditional_cost']
    attempts = view['independent']['attempts']
    assert attempts[0]['gate_comparison']['scopes'][1]['status'] == 'not_measured'
    assert attempts[1]['gate_comparison']['scopes'][1]['status'] == 'available'
    lines = [line for line in report_lines(core['independent']) if 'slots not owed after focus rejection' in line]
    assert lines == ['r01-a01 · causal not_measured · 2 slots not owed after focus rejection']
    # dev64 §4 withdraws the dev63 economy injection; Core and JSON remain unchanged.
    assert all(line not in outputs['report.md'].splitlines() for line in lines)
    assert '- stages: focus rejected · causal not_run' in outputs['report.md'].splitlines()
    assert '- stages: focus passed · causal passed' in outputs['report.md'].splitlines()
    responses.validate(view, 'lab-campaign-view/15')
    responses.validate(report, 'lab-owner-report/15')
    if node := shutil.which('node'):
        live = tmp_path / 'focus-first.json'
        live.write_text(json.dumps(view))
        env = {**os.environ, 'LAB_FOCUS_FIRST_CAMPAIGN': str(live),
               'LAB_PANEL_STATIC': str(Path(panel_package.__file__).parent / 'static'),
               'LAB_PANEL_FIXTURES': str(CAPTURES.parent)}
        result = subprocess.run([node, '--test', str(Path(__file__).parent / 'panel_js/b15-focus-first.test.mjs')],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


def test_default_stage_outputs_unchanged():
    import report_md_compat

    for scenario in ('agent-failed', 'agent-finished'):
        outputs = read_outputs(CAPTURES.parent / 'b14' / scenario / 'run')
        baseline = Path(__file__).parent / 'data/dev62-default-outputs' / scenario
        for member, actual in outputs.items():
            if member.endswith('.md'):
                assert report_md_compat.current(actual) == report_md_compat.legacy((baseline / member).read_text())
            else:
                actual = report_md_compat.without_operator_progress(actual)
                if member == 'campaign.json':
                    assert actual.pop('lab_notices_state') == 'present'
                elif member == 'owner-report.json':
                    assert actual['report'].pop('lab_notices_state') == 'present'
                    assert actual['report']['core'].pop('baseline_import') is None
                    for measurement in actual['report']['measurements']:
                        for field in ('unanswered', 'unanswered_state', 'answered_reported'):
                            measurement.pop(field)
                expected = (baseline / member).read_text().replace(
                    '"schema": "lab-campaign-view/12"', '"schema": "lab-campaign-view/15"', 1).replace(
                    '"schema": "lab-owner-report/10"', '"schema": "lab-owner-report/15"', 1)
                assert json.dumps(actual, ensure_ascii=False, indent=2) + '\n' == expected
