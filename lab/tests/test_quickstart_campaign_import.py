"""Project campaigns select evaluation sources and carry Core findings."""

from __future__ import annotations

import io
import json
import shlex
import shutil
from pathlib import Path

import pytest
from evolution_lab.cli import main
from evolution_lab.quickstart import QuickstartError, new_project
from evolution_lab.quickstart._readout import render_campaign_readout
from scaffold_16 import as_manifest_16
from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from test_quickstart_campaign import _campaign
from test_quickstart_evaluate import _run as _evaluate

pytestmark = pytest.mark.slow


def _project(tmp_path, *, family=16):
    project = new_project(tmp_path / "p")
    if family == 16:
        manifest = project / "manifest.json"
        manifest.write_text(json.dumps(as_manifest_16(json.loads(manifest.read_bytes()))))
    return project


def _operation(summary, kind):
    op = summary[kind]["operation_id"]
    return Path(summary["state"]) / "artifacts" / op


def _run_cli(project, *args):
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "campaign",
            str(project),
            "--wheel",
            str(_installed_lab_wheel()),
            "--wheel",
            str(CORE_WHEEL),
            *args,
        ],
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_campaign_baseline_option_conflicts(tmp_path):
    project = _project(tmp_path)
    for args, message in (
        (("--from", "operation", "--baseline-from", "001"), "--baseline-from"),
        (("--continue", "--baseline", "fresh"), "--continue"),
        (("--continue", "--baseline-from", "001"), "--baseline-from"),
        (("--baseline", "inherit"), "--baseline-from"),
        (("--baseline", "fresh", "--baseline-from", "001"), "--baseline-from"),
    ):
        code, _, error = _run_cli(project, *args)
        assert code == 2
        assert message in error
    assert not (project / ".lab").exists()


def test_campaign_default_import(tmp_path, monkeypatch):
    from evolution_lab.quickstart import _evaluate as module

    project = _project(tmp_path)
    with monkeypatch.context() as prior:
        prior.setattr(module, "installed_versions", lambda: ("0.0.3.dev74", "2.0.0b18"))
        evaluated = _evaluate(project)
    source = _operation(evaluated.summary, "evaluation")
    state = Path(evaluated.summary["state"])
    runs = evaluated.run_dir.parent
    # Keep the real operation in the older pair. Provide two newer summaries
    # from an unreadable pair whose terminal facts exclude them.
    old_state = state.parent / "state-dev73-b17"
    for number, control, results in ((2, "running", "present"), (3, "finished", "deleted")):
        identifier = f"skipped-{number}"
        directory = old_state / "artifacts" / identifier / identifier / "measurement"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").touch()
        run = runs / f"{number:03d}"
        run.mkdir()
        summary = {**evaluated.summary, "run": number, "state": str(old_state)}
        summary["evaluation"] = {
            **summary["evaluation"],
            "operation_id": identifier,
            "control_state": control,
            "results_state": results,
        }
        (run / "summary.json").write_text(json.dumps(summary))
    result = _campaign(project)
    assert result.exit_code in (0, 1), result.summary
    assert result.summary["baseline"]["run"] == "001"
    assert result.summary["baseline"]["operation_id"] == source.name
    saved = json.loads(
        (_operation(result.summary, "campaign") / "baseline-import.json").read_bytes()
    )
    assert saved["source_state"] == str(state)
    for run, expected in (("003", "baseline_source_deleted"), ("999", "baseline_source_unknown")):
        with pytest.raises(QuickstartError) as caught:
            _campaign(project, baseline_from=run)
        assert caught.value.code == expected and caught.value.exit_code == 2

    explicit = _campaign(project, baseline_from=source.name)
    assert explicit.exit_code in (0, 1), explicit.summary
    assert explicit.summary["baseline"]["run"] == "001"
    fresh = _campaign(project, baseline="fresh")
    assert fresh.summary["baseline"]["reason"] == "requested"
    assert not (_operation(fresh.summary, "campaign") / "baseline-import.json").exists()


def test_campaign_default_import_uses_saved_summary_if_old_state_is_unreadable(
    tmp_path, monkeypatch
):
    from evolution_lab.quickstart import _evaluate as module

    project = _project(tmp_path)
    with monkeypatch.context() as prior:
        prior.setattr(module, "installed_versions", lambda: ("0.0.3.dev74", "2.0.0b18"))
        evaluated = _evaluate(project)
    state = Path(evaluated.summary["state"])
    (state / "owner.sqlite3").unlink()

    code, stdout, stderr = _run_cli(project)
    assert code in (0, 1), stderr
    warning = (
        "This pair state could not be read. Lab used the evaluation summary and saved journal."
    )
    assert warning in stderr
    assert warning in stdout
    run = (project / ".lab/latest").resolve()
    saved = json.loads((run / "summary.json").read_bytes())
    assert saved["baseline"]["run"] == "001"
    assert saved["baseline"]["operation_id"] == evaluated.summary["evaluation"]["operation_id"]
    assert saved["baseline"]["imported"] == 3
    assert warning in render_campaign_readout(saved, run)


def test_campaign_readout_explains_abandoned_import(tmp_path):
    from evolution_lab.owner.campaign_views import baseline_import_fields
    from evolution_lab.quickstart._baseline_source import baseline_summary
    from evolution_lab.quickstart._campaign import summary_schema
    from jsonschema import Draft202012Validator
    from test_report import baseline_import_status

    baseline = baseline_summary("evaluation", run="001", operation_id="evaluation")
    baseline.update(baseline_import_fields({**baseline_import_status(), "abandoned": True}))
    schema = summary_schema()
    Draft202012Validator(schema["properties"]["baseline"]).validate(baseline)
    summary = {"baseline": baseline, "next_command": "lab campaign project", "exit_code": 1}
    readout = render_campaign_readout(summary, tmp_path)
    assert "Core abandoned the import. The campaign measures the full baseline." in readout
    assert "Imported 0 pairs with 4 to measure." in readout


@pytest.mark.parametrize(
    "family,reason,warning",
    [
        (15, "manifest_15", "until the first baseline refresh (#208)"),
        (16, "no_scorer_traces", "imported rows carry no source verdicts"),
    ],
)
def test_lab_default_import_rules(tmp_path, family, reason, warning):
    project = _project(tmp_path, family=family)
    evaluated = _evaluate(project)
    source = _operation(evaluated.summary, "evaluation")
    if family == 16:
        shutil.rmtree(source / source.name / "measurement" / "scorer-traces")
    fresh = _campaign(project)
    assert fresh.exit_code in (0, 1), fresh.summary
    assert fresh.summary["baseline"]["reason"] == reason
    assert "--baseline-from RUN" in render_campaign_readout(fresh.summary, fresh.run_dir)
    code, _, stderr = _run_cli(project, "--baseline-from", "001")
    assert code in (0, 1), stderr
    assert warning in stderr
    latest = json.loads((project / ".lab/latest/summary.json").read_bytes())
    assert latest["baseline"]["source"] == "evaluation"


@pytest.mark.parametrize("changed", ["scorer", "held_out"])
def test_campaign_import_refusals_pass_through(tmp_path, changed):
    project = _project(tmp_path)
    _evaluate(project)
    if changed == "scorer":
        scorer = project / "scorer" / "score.py"
        scorer.write_text(scorer.read_text() + "\n# Different scorer identity\n")
    else:
        corpus = project / "corpus.jsonl"
        cases = [json.loads(line) for line in corpus.read_text().splitlines()]
        for i, case in enumerate(cases):
            case.update(split="test" if i == 2 else "train", group_id=f"group-{i}")
        corpus.write_text("".join(json.dumps(case) + "\n" for case in cases))
    result = _campaign(project)
    assert result.exit_code == 5, result.summary
    record = json.loads((result.run_dir / "refusal.json").read_bytes())
    [finding] = record["findings"]
    expected = "baseline_import_incompatible" if changed == "scorer" else "baseline_import_held_out"
    assert finding["code"] == expected and finding["effects"] == ["import"]
    readout = render_campaign_readout(result.summary, result.run_dir)
    assert finding["effect_texts"][0].rstrip(";")[:30] in readout
    if changed == "scorer":
        assert any("identity.scorer.digest" in line for line in finding["differs"])
        assert "differs: identity.scorer.digest" in readout
        accept = shlex.split(
            next(
                line.removeprefix("accept: ")
                for line in readout.splitlines()
                if line.startswith("accept: ")
            )
        )
        accept[-1] = "verify the scorer change"
        output, error = io.StringIO(), io.StringIO()
        assert main(accept[1:] + ["--json"], stdout=output, stderr=error) in (0, 1), (
            error.getvalue()
        )
        summary = json.loads(output.getvalue())
        operation = _operation(summary, "campaign")
        [decision] = (operation / operation.name / "authorizations").glob("*.json")
        from typer.testing import CliRunner

        from evolution.tree.cli import tree_app

        expected_dir = tmp_path / "core-decision"
        expected_dir.mkdir()
        core = CliRunner().invoke(
            tree_app,
            [
                "authorize",
                str(expected_dir),
                "--accept",
                f"{expected}:{finding['instance_digest']}",
                "--reason",
                accept[-1],
            ],
        )
        assert core.exit_code == 0, core.output
        [reference] = (expected_dir / "authorizations").glob("*.json")
        observed, expected_decision = [
            json.loads(path.read_bytes()) for path in (decision, reference)
        ]
        for document in (observed, expected_decision):
            document.pop("issued_at")
        assert observed == expected_decision
        assert summary["campaign"]["operation_id"] == result.summary["campaign"]["operation_id"]
