"""Run files and the readout of `lab evaluate` (spec §3.4). Installed mode, like test_quickstart_evaluate."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import jsonschema
import pytest

from evolution_lab.quickstart import evaluate, new_project, summary_schema
from evolution_lab.quickstart._readout import (
    fenced, read_conversations, redact, redact_summary, render_readout, scorer_name,
)

from test_quickstart_evaluate import _edit_json, _installed_lab, _replace, _wheels  # noqa: F401

pytestmark = pytest.mark.slow


def _run(project: Path):
    return asyncio.run(evaluate(project, wheels=_wheels(), environ={}))


@pytest.fixture
def accepted(tmp_path):
    project = new_project(tmp_path / "p")
    return project, _run(project)


def test_run_files_written(accepted):
    project, result = accepted
    assert result.exit_code == 0, result.summary
    run = result.run_dir
    for name in ("report.md", "conversations.md", "resolved.json", "summary.json", "evaluate.log"):
        assert (run / name).is_file(), name
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(summary_schema()).validate(summary)
    assert summary == json.loads(json.dumps(result.summary))
    resolved = json.loads((run / "resolved.json").read_text(encoding="utf-8"))
    assert resolved["schema"] == "lab-evaluate-resolved/1"
    assert resolved["substituted"] and set(resolved["substituted"]) == set(result.summary["resolved"]["substituted"])
    assert resolved["sources"] == {name: "computed" for name in resolved["substituted"]}
    log = (run / "evaluate.log").read_text(encoding="utf-8")
    assert "conformance" in log and "evaluation" in log
    latest = project / ".lab" / "latest"
    assert latest.is_symlink() and os.readlink(latest) == "runs/001"
    assert (latest / "summary.json").is_file()


def test_latest_follows_newer_run(tmp_path):
    project = new_project(tmp_path / "p")
    _run(project)
    second = _run(project)
    assert os.readlink(project / ".lab" / "latest") == "runs/002"
    assert second.run_dir.name == "002"


def test_readout_names_substitutions(accepted):
    _, result = accepted
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert len(result.summary["resolved"]["substituted"]) == 9
    assert ("Lab filled: environment digest, port, scorer Python, 3 identity digests "
            "(listed in resolved.json); nothing to do.") in text.splitlines()
    for field in result.summary["resolved"]["substituted"]:
        assert field not in text                  # the paths are in resolved.json; the readout names them in words
    assert "Computed by Lab" not in text
    assert "What happened" in text
    assert "report.md" in text and "conversations.md" in text and "evaluate.log" in text
    assert "->" in text and "[" in text          # case question -> answer [score]
    assert "4. Lab ran the evaluation on a fresh target: 3 cases x 1 repeat," in text
    assert "Cleaned up after" not in text
    assert "Measurement not accepted" not in text


def test_readout_names_only_substituted_fields(accepted):
    _, result = accepted
    one = {"explicit": [], "schema": "lab-evaluate-resolved/1", "substituted": {"launch.json:environment_ref": "x"}}
    text = render_readout(dict(result.summary, resolved=one), result.run_dir, rows=result.rows)
    assert "Lab filled: environment digest (listed in resolved.json); nothing to do." in text
    # An explicit port and settings hash (not substituted) are not named; one identity digest is singular.
    substituted = {key: value for key, value in result.summary["resolved"]["substituted"].items()
                   if "base_url" not in key and "settings_hash" not in key and "contract_digest" not in key}
    some = dict(result.summary["resolved"], substituted=substituted)
    text = render_readout(dict(result.summary, resolved=some), result.run_dir, rows=result.rows)
    assert "Lab filled: environment digest, scorer Python, 1 identity digest (listed in resolved.json)" in text


def test_readout_counts_a_paired_substitution(accepted):
    """A marker Lab filled from the explicit value of its paired field is counted on the "Lab filled" line,
    and the line says it was copied."""
    _, result = accepted
    paired = {"schema": "lab-evaluate-resolved/1", "explicit": ["launch.json:endpoint.base_url"],
              "substituted": {"evaluation-input.json:target.base_url": "http://127.0.0.1:7001",
                              "launch.json:environment_ref": "x"},
              "sources": {"evaluation-input.json:target.base_url": "paired:launch.json:endpoint.base_url",
                          "launch.json:environment_ref": "computed"}}
    summary = dict(result.summary, resolved=paired)
    jsonschema.Draft202012Validator(summary_schema()).validate(summary)
    text = render_readout(summary, result.run_dir, rows=result.rows)
    assert ("Lab filled: environment digest, port (listed in resolved.json; 1 copied from the paired field "
            "you set); nothing to do.") in text.splitlines()


def test_redaction_rewrites_only_free_text_values(accepted):
    """Keys, the schema const and patterned values (sha256) stay; free text is redacted; the original stays."""
    _, result = accepted
    summary = json.loads(json.dumps(result.summary))
    summary["reason"] = "the key was a-secret"
    digest = summary["environment"]["target_environment"]
    secrets = {"KEY": "a-secret", "SHORT": digest[:2], "WORD": "state", "SCHEMA": "lab-evaluate-summary/1"}
    clean = redact_summary(summary, secrets)
    jsonschema.Draft202012Validator(summary_schema()).validate(clean)
    assert set(clean) == set(summary) and set(clean["conformance"]) == set(summary["conformance"])
    assert clean["schema"] == "lab-evaluate-summary/1"
    assert clean["environment"]["target_environment"] == digest
    assert clean["reason"] == "the key was <secret:KEY>"
    assert summary["reason"] == "the key was a-secret"          # a copy; the original is untouched
    assert redact({"state": "state"}, {"WORD": "state"}) == {"state": "<secret:WORD>"}


def test_scorer_is_named_from_the_run_copy(tmp_path):
    run = tmp_path / "run"
    (run / "source").mkdir(parents=True)
    path = run / "source" / "evaluation-input.json"
    assert scorer_name(run) == "the scorer"                      # no run copy to read
    for scorer, expected in (({"cwd": "judge", "argv": ["/py", "grade.py"]}, "the scorer (judge/grade.py)"),
                             ({"cwd": ".", "argv": ["/py", "-m", "grader"]}, "the scorer"),
                             ({"cwd": "judge", "argv": ["./grade"]}, "the scorer")):
        path.write_text(json.dumps({"scorer": scorer}), encoding="utf-8")
        assert scorer_name(run) == expected, scorer


def test_resolved_records_python_and_wheels(accepted):
    _, result = accepted
    resolved = json.loads((result.run_dir / "resolved.json").read_text(encoding="utf-8"))
    assert resolved["python"] == result.summary["environment"]["python"]
    assert resolved["pair"] == result.summary["pair"]
    assert [wheel["path"] for wheel in resolved["wheels"]] == [str(path) for path in _wheels()]
    for wheel in resolved["wheels"]:
        assert wheel["sha256"] == hashlib.sha256(Path(wheel["path"]).read_bytes()).hexdigest()
    assert result.summary["resolved"]["wheels"] == resolved["wheels"]


def test_readout_separates_acceptance_from_quality(accepted):
    _, result = accepted
    quality = result.summary["quality"]
    assert quality == {"metric": "score", "mean": pytest.approx(2 / 3), "scored": 3, "full_score_count": 2}
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert ("run 001: measurement accepted: 3/3 samples scored; "
            "mean score 0.67 (2 of 3 scored 1)") in text.splitlines()
    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["quality"] == json.loads(json.dumps(quality))


def test_readout_quality_unavailable_or_not_01(accepted):
    _, result = accepted
    none = dict(result.summary, quality={"metric": "score", "mean": None, "scored": 0, "full_score_count": None})
    text = render_readout(none, result.run_dir, rows=result.rows)
    assert "3/3 samples scored; mean score unavailable" in text and "scored 1)" not in text
    graded = dict(result.summary, quality={"metric": "f1", "mean": 0.5, "scored": 3, "full_score_count": None})
    text = render_readout(graded, result.run_dir, rows=result.rows)
    assert "3/3 samples scored; mean f1 0.50\n" in text
    old = {k: v for k, v in result.summary.items() if k != "quality"}      # a summary without the field
    assert "mean score unavailable" in render_readout(old, result.run_dir, rows=result.rows)
    jsonschema.Draft202012Validator(summary_schema()).validate(old)


def test_readout_partial(tmp_path):
    project = new_project(tmp_path / "p")
    _edit_json(project / "evaluation-input.json",
               lambda v: v["defaults"].update(max_consecutive_unmeasured=1, sample_retries=0))
    _replace(project / "agent" / "adapter.py", "        try:\n            # TODO(you): add a deadline",
             '        if "gamma" in message.content:\n'
             '            return AgentTurn("target_failed", "", reason=TurnReason("agent", "call_failed"))\n'
             "        try:\n            # TODO(you): add a deadline")
    result = _run(project)
    assert result.exit_code == 1
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert "Measurement not accepted" in text
    assert "consecutive_unmeasured" in text and "2/3" in text
    assert "  mean score 1.00 of the 2 scored samples (2 of 2 scored 1)" in text
    assert "->" in text                          # the scored samples are shown
    assert (result.run_dir / "conversations.md").is_file()
    assert (result.run_dir / "report.md").is_file()


def test_no_operation_files(tmp_path):
    project = new_project(tmp_path / "p")
    _replace(project / "agent" / "adapter.py", 'return {"agent": "my-agent"}',
             'return {"agent": {"nested": "not allowed"}}')
    result = _run(project)
    assert result.exit_code == 1
    for name in ("resolved.json", "summary.json", "evaluate.log"):
        assert (result.run_dir / name).is_file(), name
    assert not (result.run_dir / "report.md").exists()
    assert not (result.run_dir / "conversations.md").exists()
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert "Measurement not accepted" in text and "conformance" in text
    # The steps stop at the failed conformance: no evaluation is claimed.
    assert "ran conformance, the core's check that it speaks the turn protocol: it failed" in text
    assert "ran the evaluation" not in text and "stopped the target" not in text


def test_leftovers_line(accepted):
    _, result = accepted
    summary = dict(result.summary, cleaned_leftovers=[2, 3])
    text = render_readout(summary, result.run_dir, rows=result.rows)
    assert "Cleaned up after runs 002, 003" in text
    summary = dict(result.summary, cleaned_leftovers=[2])
    text = render_readout(summary, result.run_dir, rows=result.rows)
    assert "Cleaned up after run 002\n" in text


def test_conversations_md_per_sample(accepted):
    _, result = accepted
    text = (result.run_dir / "conversations.md").read_text(encoding="utf-8")
    samples = result.summary["samples"]
    assert len(samples) == 3
    assert text.count("- question:") == 3 and text.count("- answer:") == 3
    assert text.count("scorer verdicts:") == 3
    for sample in samples:
        assert f"case {sample['case_id']}, sample {sample['sample_id']}" in text
    assert len(result.rows) == 3


def test_unreadable_sample_degrades(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner
    original = Owner.sample
    calls = {"n": 0}

    async def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyError("message")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Owner, "sample", flaky)
    project = new_project(tmp_path / "p")
    result = _run(project)
    assert result.exit_code == 0, result.summary
    text = (result.run_dir / "conversations.md").read_text(encoding="utf-8")
    assert text.count("- question:") == 2 and "sample unreadable: KeyError" in text
    assert len(result.rows) == 3
    for name in ("resolved.json", "summary.json", "evaluate.log"):
        assert (result.run_dir / name).is_file()
    assert (project / ".lab" / "latest").exists()
    assert "unreadable" in (result.run_dir / "evaluate.log").read_text(encoding="utf-8")
    assert "unreadable" in render_readout(result.summary, result.run_dir, rows=result.rows)


def test_conversations_failure_keeps_base_files(tmp_path, monkeypatch):
    from evolution_lab.owner import Owner

    async def broken(self, *args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(Owner, "measurements", broken)
    project = new_project(tmp_path / "p")
    result = _run(project)
    assert result.exit_code == 0
    assert not (result.run_dir / "conversations.md").exists()
    for name in ("resolved.json", "summary.json", "evaluate.log"):
        assert (result.run_dir / name).is_file()
    assert "conversations.md not written: OSError" in (result.run_dir / "evaluate.log").read_text(encoding="utf-8")
    assert "conversations.md was not written" in render_readout(result.summary, result.run_dir, rows=result.rows)


def test_invalid_summary_keeps_log(tmp_path, monkeypatch):
    from evolution_lab.quickstart import QuickstartError, _readout
    monkeypatch.setattr(_readout, "_validated", lambda summary: (_ for _ in ()).throw(
        QuickstartError("summary_invalid", "bad", exit_code=3)))
    project = new_project(tmp_path / "p")
    with pytest.raises(QuickstartError) as raised:
        _run(project)
    assert raised.value.exit_code == 3
    run = project / ".lab" / "runs" / "001"
    assert (run / "evaluate.log").is_file() and (run / "resolved.json").is_file()
    assert not (run / "summary.json").exists()


def test_secret_values_redacted(tmp_path, capsys):
    secret = "target-secret-9d41be07"
    project = new_project(tmp_path / "p")
    _edit_json(project / "launch.json", lambda v: v.update(secret_names=["MY_AGENT_KEY"]))
    _replace(project / "agent" / "adapter.py", "        try:\n            # TODO(you): add a deadline",
             "        import os\n"
             "        if 'alpha' in message.content:\n"
             "            return AgentTurn('ok', 'leak ' + os.environ['MY_AGENT_KEY'])\n"
             "        try:\n            # TODO(you): add a deadline")
    result = asyncio.run(evaluate(project, wheels=_wheels(), environ={"MY_AGENT_KEY": secret}))
    assert result.exit_code in (0, 1, 3), result.summary
    conversations = (result.run_dir / "conversations.md").read_text(encoding="utf-8")
    assert "leak <secret:MY_AGENT_KEY>" in conversations
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert secret not in text and "<secret:MY_AGENT_KEY>" in text
    assert secret not in json.dumps(result.summary) and not any(secret in line for line in result.log)
    # Lab's own files (the run directories, report.md included). ADR-0032 p. 4 (amended 2026-11-09): what the
    # agent emits itself is recorded as is by the core and Owner in the pair state, which is not checked here.
    run_files = [path for path in (project / ".lab" / "runs").rglob("*") if path.is_file() and not path.is_symlink()]
    assert (result.run_dir / "report.md") in run_files
    for path in run_files:
        assert secret.encode() not in path.read_bytes(), path
    out = capsys.readouterr()
    assert secret not in out.out + out.err


def test_readout_without_conformance(tmp_path):
    """A launch that declares no conformance: the readout says it was skipped and never that it ran."""
    project = new_project(tmp_path / "p")
    _edit_json(project / "launch.json", lambda v: v.update(conformance=None))
    result = _run(project)
    assert result.exit_code == 0, result.summary
    assert result.summary["conformance"] is None and result.summary["conformance_declared"] is False
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert "  3. launch.json declares no conformance, so Lab skipped it." in text
    assert "ran conformance" not in text
    assert "  4. Lab ran the evaluation on a fresh target: 3 cases x 1 repeat," in text


def test_broken_scorer_is_named(tmp_path):
    project = new_project(tmp_path / "p")
    (project / "scorer" / "score.py").write_text("def main(:\n", encoding="utf-8")
    result = _run(project)
    assert result.exit_code == 1, result.summary
    jsonschema.Draft202012Validator(summary_schema()).validate(result.summary)
    assert [sample["outcome"] for sample in result.summary["samples"]] == ["scorer_failed"] * 3
    assert [row["score"] for row in result.rows] == ["scorer failed"] * 3
    text = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert "-> A1   [scorer failed]" in text and "not scored" not in text
    line = next(line for line in text.splitlines() if line.startswith("The scorer failed on 3 of 3 samples"))
    assert "but the scorer (scorer/score.py) did not return a score" in line
    trace = Path(line.rsplit("its stderr is recorded in ", 1)[1].rstrip("."))
    assert trace.is_file() and trace.is_relative_to(Path(result.summary["state"]))
    assert b"SyntaxError" in trace.read_bytes()
    conversations = (result.run_dir / "conversations.md").read_text(encoding="utf-8")
    assert conversations.count("- scorer failed (exit status 1); scorer verdicts:") == 3


def test_fence_is_longer_than_any_backtick_run():
    assert fenced("plain") == ["```text", "plain", "```"]
    text = "first line\n```python\nprint('x')\n```\nand ```` four"
    lines = fenced(text)
    assert lines[0] == "`````text" and lines[-1] == "`````" and lines[1:-1] == text.split("\n")


def test_conversations_keep_multiline_answers_with_fences():
    answer = "Here is code:\n```python\nprint('A1')\n```\nDone `inline`."

    class Owner:
        async def measurements(self, ref):
            return {"measurements": [{"measurement_id": "m1"}]}

        async def measurement(self, ref, measurement_id):
            return {"measurement": {"measurement_id": "m1", "role": "evaluation", "metrics": {},
                                    "samples": [{"case_id": "q1", "sample_id": "s1"}]}}

        async def sample(self, ref, measurement_id, case_id, sample_id, disclose):
            return {"turns": [{"message": {"content": "Show `code`\nplease"},
                               "events": [{"type": "assistant_message", "content": answer}]}],
                    "scoring": {"metrics": {"score": 1.0}, "verdicts": []}}

    text, rows = asyncio.run(read_conversations(Owner(), "ref"))
    assert "- answer:\n\n````text\n" + answer + "\n````\n" in text
    assert "- question:\n\n```text\nShow `code`\nplease\n```\n" in text
    assert rows[0]["answer"] == answer


def test_readouts_print_ownership_line(accepted, tmp_path):
    from evolution_lab._ownership import OPERATOR_LINE
    from evolution_lab.quickstart._readout import render_campaign_readout
    _, result = accepted
    summary_file = result.run_dir / 'summary.json'
    before = summary_file.read_bytes()
    baseline = render_readout(result.summary, result.run_dir, rows=result.rows)
    assert render_readout(result.summary, result.run_dir, rows=result.rows, ownership_line=None) == baseline
    text = render_readout(result.summary, result.run_dir, rows=result.rows, ownership_line=OPERATOR_LINE)
    assert text.count(OPERATOR_LINE) == 1
    assert text.index(OPERATOR_LINE) < text.index('Open first:')
    assert text.replace(OPERATOR_LINE + '\n\n', '', 1) == baseline
    assert summary_file.read_bytes() == before

    run = tmp_path / 'campaign'
    run.mkdir()
    summary = {'campaign': {'outcome': 'completed', 'final_champion': 'v1', 'diff_state': 'changed'},
        'next_command': 'lab campaign project --continue', 'exit_code': 0}
    summary_file = run / 'summary.json'
    summary_file.write_text(json.dumps(summary))
    before = summary_file.read_bytes()
    baseline = render_campaign_readout(summary, run)
    assert render_campaign_readout(summary, run, ownership_line=None) == baseline
    text = render_campaign_readout(summary, run, ownership_line=OPERATOR_LINE)
    assert text.count(OPERATOR_LINE) == 1
    assert text.index(OPERATOR_LINE) < text.index('Next:')
    assert text.replace(OPERATOR_LINE + '\n', '', 1) == baseline
    assert summary_file.read_bytes() == before


def test_campaign_readout_names_the_baseline_source(tmp_path):
    from evolution_lab.quickstart._baseline_source import baseline_summary
    from evolution_lab.quickstart._readout import render_campaign_readout
    from test_report import baseline_import_status

    block = baseline_import_status()
    baseline = baseline_summary("evaluation", run="003", operation_id="evaluation-operation")
    baseline.update(imported=1, to_measure=3, not_imported=block["not_imported"],
                    fields=block["fields"], authorization_id=block["authorization_id"])
    (tmp_path / "core-notices.json").write_text(json.dumps({
        "caveats": [{"kind": "imported_provenance_unknown"}],
    }))
    summary = {"baseline": baseline, "campaign": {"outcome": "completed"},
               "next_command": "lab campaign project", "exit_code": 0}
    text = render_campaign_readout(summary, tmp_path)
    for name in ("evaluation run 003", "1 pair", "3 to measure", "seed_differs=3",
                 "identity.scorer.digest", "a" * 64, "imported_provenance_unknown"):
        assert name in text


@pytest.mark.parametrize("code", ["baseline_source_unavailable", "baseline_import_inputs_changed"])
def test_campaign_baseline_hard_refusal_commands(tmp_path, code):
    from evolution_lab.quickstart._campaign import _next_command
    from evolution_lab.quickstart._readout import render_campaign_readout

    row = {"suspended": False, "outcome": None, "refusal": {"code": code, "kind": "hard"}}
    command = _next_command("project", (), None, row)
    text = render_campaign_readout({"campaign": row, "next_command": command, "exit_code": 5}, tmp_path)
    if code == "baseline_source_unavailable":
        assert "lab campaign project --baseline fresh" in text
        assert "lab campaign project --baseline-from RUN" in text
    else:
        assert command == "lab campaign project"
        assert "rerun" in text.lower() and "stable" in text.lower()
        assert "--baseline fresh" not in text and "--continue" not in text
