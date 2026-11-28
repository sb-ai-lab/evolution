"""`lab evaluate`: the first measurement of a project folder (spec §3.3, §3.3.1; ADR-0032).

Installed mode: these tests start real target processes and the core offline,
like `examples/owner/evaluation.py`, so they need Lab installed from the wheel
the suite passes as `--wheel` (CONTRIBUTING.md "Two modes").
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import jsonschema
import pytest

from test_owner_binding import CORE_WHEEL, _installed_lab_wheel

from evolution_lab.quickstart import QuickstartError, evaluate, new_project, summary_schema
from ports import free_port

pytestmark = pytest.mark.slow

TARGET_SENTINEL = "target-sentinel-7f3a91"
JUDGE_SENTINEL = "judge-sentinel-c40e22"


@pytest.fixture(autouse=True)
def _installed_lab():
    """Workers and targets import the installed Lab; `src/` on sys.path makes the
    execution environment check refuse (CONTRIBUTING.md "Two modes")."""
    import evolution_lab
    source = Path(__file__).resolve().parents[1] / "src"
    if Path(evolution_lab.__file__).resolve().is_relative_to(source):
        pytest.fail("installed mode required: rebuild and install the Lab wheel, run pytest with "
                    "-o pythonpath= (D73_TEST_ORIGIN=installed)", pytrace=False)


def _wheels() -> tuple[Path, Path]:
    return _installed_lab_wheel(), CORE_WHEEL


def _project(tmp_path: Path) -> Path:
    return new_project(tmp_path / "p")


def _edit_json(path: Path, change) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _run(project: Path, **kwargs):
    kwargs.setdefault("environ", {})
    return asyncio.run(evaluate(project, wheels=_wheels(), **kwargs))


def _declare_secrets(project: Path) -> None:
    _edit_json(project / "launch.json", lambda v: v.update(secret_names=["MY_AGENT_KEY"]))
    _edit_json(project / "evaluation-input.json", lambda v: v["scorer"].update(allowed_env=["JUDGE_KEY"]))


def _validate(summary: dict) -> None:
    jsonschema.Draft202012Validator(summary_schema()).validate(summary)


def _files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


async def _read_state(state: Path, read):
    from evolution_lab.owner import Owner
    async with Owner.open(state) as owner:
        return await read(owner)


@pytest.mark.parametrize('parent_relative', [False, True])
def test_evaluate_skeleton_accepted(tmp_path, monkeypatch, parent_relative):
    project = _project(tmp_path)
    argument = project
    if parent_relative:
        work = tmp_path / 'work'
        work.mkdir()
        monkeypatch.chdir(work)
        argument = Path('../p')
    result = _run(argument)
    assert result.exit_code == 0, result.summary
    assert result.reason is None
    assert result.run_dir == project / ".lab" / "runs" / "001"
    assert result.run_dir.is_dir() and (result.run_dir / "source" / "launch.json").is_file()
    assert not (result.run_dir / ".active").exists()
    summary = result.summary
    _validate(summary)
    assert summary["schema"] == "lab-evaluate-summary/1"
    assert summary["exit_code"] == 0 and summary["reason"] is None and summary["run"] == 1
    assert summary["cleaned_leftovers"] == []
    assert summary["evaluation"]["coverage"] == {"expected": 3, "scored": 3}
    assert summary["provenance"] == {
        "recorded": True, "reported": 3, "overflow": 0, "not_reported": 0, "notice": None,
    }
    assert summary["evaluation"]["control_state"] == "finished"
    assert summary["evaluation"]["stopped_reason"] is None
    assert summary["evaluation"]["report_state"] == "available"
    assert summary["conformance"]["core_ok"] is True
    assert summary["final_target"]["state"] == "stopped"
    assert summary["blocker_count"] == 0
    scores = [sample["score"] for sample in sorted(summary["samples"], key=lambda s: s["case_id"])]
    assert scores == [1.0, 1.0, 0.0]
    # The source project is untouched: markers stay in the user's files.
    assert json.loads((project / "launch.json").read_text())["environment_ref"] == "lab:auto"


def test_evaluate_twice_reuses_state(tmp_path):
    project = _project(tmp_path)
    first, second = _run(project), _run(project)
    assert (first.exit_code, second.exit_code) == (0, 0)
    assert second.run_dir == project / ".lab" / "runs" / "002"
    assert second.summary["run"] == 2
    assert first.summary["pair"] == second.summary["pair"]
    states = sorted(path.name for path in (project / ".lab").iterdir() if path.name.startswith("state-"))
    assert states == [f"state-{first.summary['pair']}"]
    assert first.summary["state"] == second.summary["state"]


def test_pair_label_follows_the_justfile_rule():
    from evolution_lab.quickstart._evaluate import pair_label
    assert pair_label("0.0.3.dev73", "2.0.0b15") == "dev73-b15"
    assert pair_label("0.0.4", "2.1.0rc1") == "4-rc1"
    assert pair_label("0.0.4", "2.1.0") == "4-2.1.0"     # an empty suffix: the full version, as in the justfile


def _verdict(*, result_kind="evaluation", has_core=True, **changes):
    from evolution_lab.quickstart._evaluate import _evaluation_verdict
    evaluation = {"control_state": "finished", "failure": None, "coverage": {"expected": 20, "scored": 20},
                  "min_coverage": 0.95, "stopped_reason": None, "persistence": {"state": "saved", "failure": None},
                  "report_state": "available"}
    summary = {"evaluation": evaluation, "final_target": {"state": "stopped"}, "blocker_count": 0}
    for key, value in changes.items():
        (summary if key in summary else evaluation)[key] = value
    return _evaluation_verdict(summary, result_kind, has_core)


def test_success_rules_and_exit_codes():
    assert _verdict() == (0, None)
    assert _verdict(coverage={"expected": 20, "scored": 19}) == (0, None)  # 0.95 of 20
    code, reason = _verdict(coverage={"expected": 20, "scored": 18})
    assert code == 1 and "coverage below 0.95" in reason and "18/20" in reason
    assert _verdict(min_coverage=1.0, coverage={"expected": 3, "scored": 2})[0] == 1
    code, reason = _verdict(stopped_reason="consecutive_unmeasured")  # a stop at any coverage
    assert code == 1 and "consecutive_unmeasured" in reason
    # report.py's rule: scored / expected >= min_coverage (0.07 * 100 is 7.000000000000001 in floats).
    assert _verdict(min_coverage=0.07, coverage={"expected": 100, "scored": 7}) == (0, None)
    assert _verdict(min_coverage=0.07, coverage={"expected": 100, "scored": 6})[0] == 1
    code, reason = _verdict(has_core=False)
    assert code == 3 and "core" in reason
    assert _verdict(result_kind="campaign")[0] == 3
    assert _verdict(persistence={"state": "failed", "failure": {}})[0] == 3
    assert _verdict(report_state="unavailable")[0] == 3
    assert _verdict(control_state="cleanup_required")[0] == 3
    assert _verdict(final_target={"state": "ready"})[0] == 3
    code, reason = _verdict(blocker_count=1, stopped_reason="consecutive_unmeasured")
    assert code == 3 and "blocker" in reason and "consecutive_unmeasured" in reason


def test_missing_secret_names_role(tmp_path):
    project = _project(tmp_path)
    _declare_secrets(project)
    with pytest.raises(QuickstartError) as target:
        _run(project, environ={"JUDGE_KEY": JUDGE_SENTINEL})
    assert (target.value.code, target.value.role, target.value.field) == ("secret_missing", "target", "MY_AGENT_KEY")
    assert target.value.exit_code == 2
    with pytest.raises(QuickstartError) as scorer:
        _run(project, environ={"MY_AGENT_KEY": TARGET_SENTINEL})
    assert (scorer.value.code, scorer.value.role, scorer.value.field) == ("secret_missing", "scorer", "JUDGE_KEY")
    assert not (project / ".lab").exists()


def test_judge_key_not_in_target_env(tmp_path):
    project = _project(tmp_path)
    _declare_secrets(project)
    seen = tmp_path / "target-env"
    seen.mkdir()
    # Every target process reports the names in its environment. The instance
    # description (describe()) is not persisted by an evaluation, so the
    # adapter's build() writes them to a file outside the project instead.
    _replace(project / "agent" / "adapter.py", "def build(context):\n",
             "def build(context):\n"
             "    import json, os\n"
             f"    (Path({str(seen)!r}) / f'{{os.getpid()}}.json').write_text(json.dumps(sorted(os.environ)))\n")
    _replace(project / "scorer" / "score.py", '{"criterion": "matched", "passed": matched},',
             '{"criterion": "matched", "passed": matched},\n'
             '                    {"criterion": "judge_key", "passed": True, "note": __import__("hashlib")'
             '.sha256(__import__("os").environ.get("JUDGE_KEY", "").encode()).hexdigest()},')
    result = _run(project, environ={"MY_AGENT_KEY": TARGET_SENTINEL, "JUDGE_KEY": JUDGE_SENTINEL,
                                    "UNRELATED": "not-passed"})
    assert result.exit_code == 0, result.summary
    state = Path(result.summary["state"])

    async def read(owner):
        from evolution_lab.owner import ResourceRef
        ref = ResourceRef("operation", result.summary["evaluation"]["operation_id"])
        measurement = (await owner.measurements(ref))["measurements"][0]["measurement_id"]
        sample = result.summary["samples"][0]
        return await owner.sample(ref, measurement, sample["case_id"], sample["sample_id"], disclose=True)

    sample = asyncio.run(_read_state(state, read))
    notes = {item["criterion"]: item["note"] for item in sample["private"]["verdict_notes"]}
    assert notes["judge_key"] == hashlib.sha256(JUDGE_SENTINEL.encode()).hexdigest()
    reports = [json.loads(path.read_text()) for path in sorted(seen.iterdir())]
    assert len(reports) == 2  # the conformance target and the evaluation target
    for names in reports:
        assert "MY_AGENT_KEY" in names
        assert "JUDGE_KEY" not in names and "UNRELATED" not in names


def test_conformance_failure_exit_1(tmp_path):
    project = _project(tmp_path)
    # A nested instance description breaks the core target contract at instance load.
    _replace(project / "agent" / "adapter.py", 'return {"agent": "my-agent"}',
             'return {"agent": {"nested": "not allowed"}}')
    result = _run(project)
    assert result.exit_code == 1, result.summary
    summary = result.summary
    _validate(summary)
    assert summary["conformance"]["core_ok"] is False
    assert summary["conformance"]["failed_checks"]
    assert all(isinstance(name, str) for name in summary["conformance"]["failed_checks"])
    assert summary["evaluation"] is None
    assert "conformance" in result.reason
    operations = asyncio.run(_read_state(Path(summary["state"]), lambda owner: owner.list_operations()))
    assert len(operations) == 0
    assert summary["blocker_count"] == 0


def test_stopped_measurement_not_accepted(tmp_path):
    project = _project(tmp_path)
    _edit_json(project / "evaluation-input.json",
               lambda v: v["defaults"].update(max_consecutive_unmeasured=1, sample_retries=0))
    # The target fails every turn about "gamma" (the last case): that sample stays unmeasured.
    _replace(project / "agent" / "adapter.py", "        try:\n            # TODO(you): add a deadline",
             '        if "gamma" in message.content:\n'
             '            return AgentTurn("target_failed", "", reason=TurnReason("agent", "call_failed"))\n'
             "        try:\n            # TODO(you): add a deadline")
    result = _run(project)
    assert result.exit_code == 1, result.summary
    _validate(result.summary)
    assert result.summary["evaluation"]["stopped_reason"] == "consecutive_unmeasured"
    assert "consecutive_unmeasured" in result.reason
    assert result.summary["blocker_count"] == 0


def test_interrupted_run_cleaned_next_time(tmp_path):
    project = _project(tmp_path)
    flags = tmp_path / "flags"
    flags.mkdir()
    (flags / "slow").write_text("")
    # While flags/slow exists, an evaluation turn reports that it started and then hangs.
    _replace(project / "agent" / "adapter.py", "        try:\n            # TODO(you): add a deadline",
             f"        import asyncio, pathlib\n"
             f"        flags = pathlib.Path({str(flags)!r})\n"
             f"        if (flags / 'slow').exists():\n"
             f"            (flags / 'turning').write_text('')\n"
             f"            await asyncio.sleep(120)\n"
             "        try:\n            # TODO(you): add a deadline")

    async def interrupted():
        task = asyncio.create_task(evaluate(project, wheels=_wheels(), environ={}))
        for _ in range(600):
            if (flags / "turning").exists() or task.done():
                break
            await asyncio.sleep(0.1)
        assert (flags / "turning").exists(), "the evaluation never reached a turn"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted())
    assert (project / ".lab" / "runs" / "001" / ".active").exists()
    (flags / "slow").unlink()
    result = _run(project)
    assert result.exit_code == 0, result.summary
    assert result.run_dir.name == "002"
    assert result.summary["cleaned_leftovers"] == [1]
    assert not (project / ".lab" / "runs" / "001" / ".active").exists()


def test_no_secret_on_disk(tmp_path, monkeypatch):
    from evolution_lab.cli import main
    project = _project(tmp_path)
    _declare_secrets(project)
    monkeypatch.setenv("MY_AGENT_KEY", TARGET_SENTINEL)
    monkeypatch.setenv("JUDGE_KEY", JUDGE_SENTINEL)
    stdout, stderr = io.StringIO(), io.StringIO()
    wheels = [arg for wheel in _wheels() for arg in ("--wheel", str(wheel))]
    code = main(["evaluate", str(project), *wheels, "--json"], stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    summary = json.loads(stdout.getvalue())
    _validate(summary)
    assert summary["exit_code"] == 0
    for text in (stdout.getvalue(), stderr.getvalue()):
        assert TARGET_SENTINEL not in text and JUDGE_SENTINEL not in text
    for path in _files(project / ".lab"):
        data = path.read_bytes()
        assert TARGET_SENTINEL.encode() not in data, path
        assert JUDGE_SENTINEL.encode() not in data, path


def test_cli_requires_wheel(tmp_path):
    from evolution_lab.cli import main
    project = _project(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(["evaluate", str(project)], stdout=stdout, stderr=stderr) == 2
    assert "--wheel" in stderr.getvalue() and "just evaluate" in stderr.getvalue()
    assert not (project / ".lab").exists()


def test_cli_secret_missing_names_role(tmp_path, monkeypatch):
    from evolution_lab.cli import main
    project = _project(tmp_path)
    _declare_secrets(project)
    monkeypatch.delenv("MY_AGENT_KEY", raising=False)
    monkeypatch.setenv("JUDGE_KEY", JUDGE_SENTINEL)
    stdout, stderr = io.StringIO(), io.StringIO()
    wheels = [arg for wheel in _wheels() for arg in ("--wheel", str(wheel))]
    assert main(["evaluate", str(project), *wheels], stdout=stdout, stderr=stderr) == 2
    error = json.loads(stderr.getvalue())["error"]
    assert (error["code"], error["role"], error["field"]) == ("secret_missing", "target", "MY_AGENT_KEY")
    assert JUDGE_SENTINEL not in stderr.getvalue() + stdout.getvalue()


def test_target_that_will_not_start_shows_its_stderr(tmp_path, monkeypatch):
    """The likeliest newcomer failure: the edited adapter does not import. The traceback is only in the
    target scope's stderr.log; the readout and evaluate.log show its last lines and the path."""
    from evolution_lab.cli import main
    project = _project(tmp_path)
    _edit_json(project / "launch.json", lambda v: v.update(secret_names=["MY_AGENT_KEY"]))
    monkeypatch.setenv("MY_AGENT_KEY", TARGET_SENTINEL)
    # The agent echoes its key to stderr before the import fails: the copy Lab prints is redacted.
    _replace(project / "agent" / "adapter.py", "from my_agent import MyAgent",
             "import os as _os, sys as _sys\n"
             "_sys.stderr.write('key ' + _os.environ.get('MY_AGENT_KEY', '') + '\\n')\n"
             "from my_agentt import MyAgent")
    stdout, stderr = io.StringIO(), io.StringIO()
    wheels = [arg for wheel in _wheels() for arg in ("--wheel", str(wheel))]
    assert main(["evaluate", str(project), *wheels], stdout=stdout, stderr=stderr) == 3, stderr.getvalue()
    readout = stdout.getvalue()
    state = project / ".lab" / f"state-{json.loads((project / '.lab/latest/summary.json').read_text())['pair']}"
    logs = sorted(state.glob("scopes/*/stderr.log"))
    assert any("my_agentt" in path.read_text() for path in logs)
    shown = next(path for path in logs if "my_agentt" in path.read_text())
    assert "target start not confirmed" in readout
    assert "ModuleNotFoundError: No module named 'my_agentt'" in readout
    assert f"The agent's process wrote (last lines of {shown}):" in readout
    assert f"Open first: {shown}" in readout
    assert TARGET_SENTINEL not in readout and "key <secret:MY_AGENT_KEY>" in readout
    # The steps name only what ran: the target never started, so no conformance and nothing scored.
    assert "Run stopped: target start not confirmed" in readout
    assert "Measurement not accepted" not in readout
    assert "ran conformance" not in readout and "scored" not in readout and "measured your agent" not in readout
    assert "The run stopped before the conformance check." in readout
    log = (project / ".lab" / "latest" / "evaluate.log").read_text()
    assert "No module named 'my_agentt'" in log and str(shown) in log and TARGET_SENTINEL not in log


def test_python_option(tmp_path):
    from evolution_lab.cli import main
    from evolution_lab.preparation import EnvironmentInput, describe_environment
    alternative = os.environ.get("S7_ALT_PYTHON")
    if not alternative:
        pytest.skip("not run: S7_ALT_PYTHON (a second interpreter with this Lab and Core) is not set")
    alternative = Path(alternative).absolute()
    project = _project(tmp_path)
    seen = tmp_path / "target-python"
    seen.mkdir()
    # Every target process reports the interpreter it runs under.
    _replace(project / "agent" / "adapter.py", "def build(context):\n",
             "def build(context):\n"
             "    import os, sys\n"
             f"    (Path({str(seen)!r}) / f'{{os.getpid()}}.txt').write_text(sys.executable)\n")
    stdout, stderr = io.StringIO(), io.StringIO()
    wheels = [arg for wheel in _wheels() for arg in ("--wheel", str(wheel))]
    code = main(["evaluate", str(project), *wheels, "--python", str(alternative), "--json"],
                stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    summary = json.loads(stdout.getvalue())
    assert summary["environment"]["python"] == str(alternative)
    digest = describe_environment(EnvironmentInput(alternative, _wheels())).digest
    assert digest != describe_environment(EnvironmentInput(Path(sys.executable), _wheels())).digest
    assert summary["environment"]["target_environment"] == digest
    source = project / ".lab" / "runs" / "001" / "source"
    launch = json.loads((source / "launch.json").read_text())
    assert launch["environment_ref"] == digest == launch["identity"]["runtime_ref_digest"]
    evaluation = json.loads((source / "evaluation-input.json").read_text())
    assert evaluation["scorer"]["argv"][0] == str(alternative)
    assert summary["resolved"]["substituted"]["evaluation-input.json:scorer.argv.0"] == str(alternative)
    # The scorer argv and the environment digest are declarations; the target processes prove it ran there.
    executables = [path.read_text() for path in sorted(seen.iterdir())]
    assert len(executables) == 2  # the conformance target and the evaluation target
    assert all(Path(value) == alternative for value in executables), executables


def _cli(project: Path, *extra: str, wheels: Sequence[str] | None = None) -> tuple[int, str, str]:
    from evolution_lab.cli import main
    stdout, stderr = io.StringIO(), io.StringIO()
    if wheels is None:
        wheels = [str(wheel) for wheel in _wheels()]
    arguments = [arg for wheel in wheels for arg in ("--wheel", wheel)]
    code = main(["evaluate", str(project), *arguments, *extra], stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_unexpected_error_exits_3_with_the_state(tmp_path):
    """A read-only project is no mistake Lab names in advance: exit 3 and an evaluate error, never the
    service client's exit 1."""
    project = _project(tmp_path)
    project.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    try:
        code, _, stderr = _cli(project)
    finally:
        project.chmod(0o755)
    assert code == 3, stderr
    error = json.loads(stderr)["error"]
    assert (error["code"], error["stage"]) == ("unexpected_error", "evaluate")
    assert "PermissionError" in error["message"]
    assert error["state"].startswith(str(project / ".lab" / "state-"))
    assert "client" not in stderr


_CORPUS_MISTAKES = {
    "not JSON": (lambda lines: [lines[0], '{"case_id": "q2", ', lines[2]], 2, "not valid JSON (not_json)"),
    "duplicate case_id": (lambda lines: [lines[0], lines[1].replace('"q2"', '"q1"'), lines[2]], 2,
                          "a case_id an earlier line already uses (duplicate_case_id)"),
    "missing public": (lambda lines: [lines[0], lines[1], json.dumps(
        {k: v for k, v in json.loads(lines[2]).items() if k != "public"})], 3,
        "a required field is missing (missing_key)"),
    "reflection_metadata": (lambda lines: [lines[0], json.dumps(dict(json.loads(lines[1]), reflection_metadata={})),
                                           lines[2]], 2, "(unknown_key)"),
}


@pytest.mark.parametrize("mistake", sorted(_CORPUS_MISTAKES))
def test_corpus_mistakes_exit_2_with_file_and_line(tmp_path, mistake):
    project = _project(tmp_path)
    change, line, what = _CORPUS_MISTAKES[mistake]
    corpus = project / "corpus.jsonl"
    corpus.write_text("\n".join(change(corpus.read_text(encoding="utf-8").splitlines())) + "\n", encoding="utf-8")
    code, stdout, stderr = _cli(project)
    assert code == 2, stdout + stderr
    error = json.loads(stderr)["error"]
    assert (error["code"], error["file"]) == ("corpus_invalid", "corpus.jsonl")
    assert error["field"].startswith(f"line {line}: ") and what in error["field"], error
    assert not (project / ".lab").exists()


def test_corpus_check_is_the_core_loader(tmp_path, monkeypatch):
    """Lab keeps no corpus rules of its own: the check is core's ``load_eval_corpus`` (the loader preparation
    uses), and a ``CorpusError`` Lab has no words for is still named with its code and line."""
    import evolution.tree.execution.corpus as core_corpus

    from evolution_lab.quickstart import _evaluate
    project = _project(tmp_path)
    evaluation = json.loads((project / "evaluation-input.json").read_text(encoding="utf-8"))
    seen = []

    def refuse(path):
        seen.append(Path(path))
        raise core_corpus.CorpusError("brand_new_rule", 4)

    monkeypatch.setattr(core_corpus, "load_eval_corpus", refuse)
    with pytest.raises(QuickstartError) as info:
        _evaluate.check_corpus(project, evaluation)
    assert seen == [project / "corpus.jsonl"]
    assert (info.value.code, info.value.file, info.value.field, info.value.exit_code) == (
        "corpus_invalid", "corpus.jsonl", "line 4: brand new rule (brand_new_rule)", 2)
    monkeypatch.setattr(core_corpus, "load_eval_corpus",
                        lambda path: (_ for _ in ()).throw(core_corpus.CorpusError("empty_corpus", 0)))
    with pytest.raises(QuickstartError) as info:
        _evaluate.check_corpus(project, evaluation)
    assert str(info.value) == "corpus.jsonl: the corpus has no cases (empty_corpus)" and info.value.field is None


def test_invalid_artifacts_without_a_project_place_are_exit_3(tmp_path, monkeypatch):
    """An artifacts_invalid whose diagnosis names no corpus line or project file is Lab's or the
    environment's failure: exit 3, not a project error."""
    from evolution_lab.preparation import PreparationError, PreparationStore

    def refuse(self, digest, **kwargs):
        raise PreparationError("artifacts_invalid", "prepare")

    monkeypatch.setattr(PreparationStore, "prepare_evaluation", refuse)
    result = _run(_project(tmp_path))
    assert result.exit_code == 3, result.summary
    _validate(result.summary)
    assert result.summary["error"]["code"] == "artifacts_invalid"
    assert (result.summary["error"]["file"], result.summary["error"]["field"]) == (None, None)
    assert result.reason.startswith("preparation refused: ")


def test_secret_equal_to_a_summary_key_keeps_the_summary(tmp_path):
    """Redaction rewrites free text values only: a secret whose value is a summary key ("state") leaves the
    keys and the schema-owned values as they are, and an accepted run stays accepted."""
    project = _project(tmp_path)
    _edit_json(project / "launch.json", lambda v: v.update(secret_names=["MY_AGENT_KEY"]))
    result = _run(project, environ={"MY_AGENT_KEY": "state"})
    assert result.exit_code == 0, result.summary
    _validate(result.summary)
    written = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    _validate(written)
    assert "state" in written and "<secret:MY_AGENT_KEY>" not in written
    assert written["schema"] == "lab-evaluate-summary/1" and written["exit_code"] == 0
    assert "<secret:MY_AGENT_KEY>" in written["state"]   # the free text value is still redacted
    assert set(written["conformance"]) >= {"state", "target_state"}


def test_invalid_artifacts_from_the_fresh_copy_are_a_project_error(tmp_path, monkeypatch):
    """Should a corpus mistake get past Lab's own check, preparation's artifacts_invalid of the fresh run
    copy is still exit 2, naming the corpus and the line."""
    from evolution_lab.quickstart import _evaluate
    monkeypatch.setattr(_evaluate, "check_corpus", lambda project, evaluation: None)
    project = _project(tmp_path)
    corpus = project / "corpus.jsonl"
    corpus.write_text(corpus.read_text(encoding="utf-8").replace('"q2"', '"q1"'), encoding="utf-8")
    result = _run(project)
    assert result.exit_code == 2, result.summary
    _validate(result.summary)
    assert result.summary["error"]["code"] == "artifacts_invalid"
    assert (result.summary["error"]["file"], result.summary["error"]["field"]) == (
        "corpus.jsonl", "line 2: duplicate_case_id")
    assert result.reason.startswith("preparation refused (corpus.jsonl, line 2: duplicate_case_id): ")


def test_invalid_evaluation_input_names_the_file(tmp_path):
    project = _project(tmp_path)
    _edit_json(project / "evaluation-input.json", lambda v: v.update(evaluation_set_id=5))
    result = _run(project)
    assert result.exit_code == 2, result.summary
    assert result.summary["error"]["file"] == "evaluation-input.json"
    assert result.reason == "preparation refused (evaluation-input.json): The preparation request is invalid."


def test_missing_scorer_script_names_the_field(tmp_path):
    project = _project(tmp_path)
    (project / "scorer" / "score.py").unlink()
    code, _, stderr = _cli(project)
    assert code == 2, stderr
    error = json.loads(stderr)["error"]
    assert (error["file"], error["field"]) == ("evaluation-input.json", "scorer.argv.1")
    assert "scorer/score.py does not exist" in error["message"]
    assert not (project / ".lab").exists()


def test_missing_wheel_or_python_path_names_the_argument(tmp_path):
    project = _project(tmp_path)
    missing = str(tmp_path / "nowhere.whl")
    code, _, stderr = _cli(project, wheels=[missing, str(CORE_WHEEL)])
    assert code == 2, stderr
    error = json.loads(stderr)["error"]
    assert error["field"] == "--wheel" and missing in error["message"]
    code, _, stderr = _cli(project, "--python", str(tmp_path / "no-python"))
    assert code == 2, stderr
    error = json.loads(stderr)["error"]
    assert error["field"] == "--python" and "no-python" in error["message"]
    assert not (project / ".lab").exists()


def test_owner_evaluation_example_runs(tmp_path):
    """examples/owner/evaluation.py, the Owner walkthrough first-evaluation.md shows, run as a reader would."""
    script = Path(__file__).resolve().parents[1] / "examples" / "owner" / "evaluation.py"
    lab_wheel, core_wheel = _wheels()
    port = free_port()
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(script), "--python", sys.executable, "--lab-wheel", str(lab_wheel),
         "--core-wheel", str(core_wheel), "--state-directory", str(tmp_path / "state"),
         "--source-directory", str(tmp_path / "source"), "--port", str(port)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stderr[-4000:]
    result = json.loads(done.stdout)
    assert result["schema"] == "lab-neutral-evaluation-example/1"
    assert result["prepared"] == {"kind": "evaluation", "case_count": 3, "repeat_count": 1}
    assert result["conformance"]["state"] == "succeeded" and result["conformance"]["core_ok"] is True
    assert result["evaluation"]["control_state"] == "finished"
    assert result["evaluation"]["coverage"] == {"expected": 3, "scored": 3}
    assert result["evaluation"]["stop_reason"] is None and result["evaluation"]["report_state"] == "available"
    assert result["final_target"]["state"] == "stopped" and result["blocker_count"] == 0


def test_new_project_under_symlinked_ancestor(tmp_path):
    from evolution_lab.cli.parser import main
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    out, err = io.StringIO(), io.StringIO()
    assert main(['new-project', str(link / 'p')], stdout=out, stderr=err) == 0
    canonical = real.resolve() / 'p'
    assert f'lab evaluate {canonical}' in out.getvalue()
    # An operator may still type the alias. Both reported and Core paths must agree.
    result = _run(link / 'p')
    assert result.exit_code == 0, result.summary
    assert result.run_dir == canonical / '.lab/runs/001'
    assert result.run_dir == Path(os.path.realpath(result.run_dir))


def test_new_project_accepts_parent_relative_path(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    project = new_project('../project')
    assert project == tmp_path.resolve() / 'project'
    assert (project / 'manifest.json').is_file()
    (tmp_path / 'alias').symlink_to(project, target_is_directory=True)
    with pytest.raises(QuickstartError) as caught:
        new_project('../alias')
    assert caught.value.code == 'invalid_directory'


def test_evaluate_provenance_notice(tmp_path):
    from evolution_lab.quickstart._readout import render_readout

    project = _project(tmp_path)
    _replace(project / "agent" / "adapter.py",
             'provenance=TurnProvenance(used=(_PROMPT_PATH,))', 'provenance=None')
    result = _run(project)
    assert result.exit_code == 0, result.summary
    summary = result.summary
    assert summary["provenance"] == {
        "recorded": True, "reported": 0, "overflow": 0, "not_reported": 3,
        "notice": "baseline_provenance_unknown",
    }
    _validate(summary)
    displayed = render_readout(summary, result.run_dir)
    assert "treat every case as affected" in displayed
    without = dict(summary)
    del without["provenance"]
    _validate(without)
    assert [line for line in displayed.splitlines() if "treat every case as affected" not in line] == (
        render_readout(without, result.run_dir).splitlines()
    )

    from evolution_lab.quickstart._evaluate import _evaluation_provenance
    operation_id = summary["evaluation"]["operation_id"]
    measurement = Path(summary["state"]) / "artifacts" / operation_id / operation_id / "measurement"
    _edit_json(project / "manifest.json", lambda v: v["campaign"]["gate"]["provenance"].update(kind="metadata"))
    assert _evaluation_provenance(measurement, project)["notice"] is None
    (project / "campaign-launch.json").unlink()
    assert _evaluation_provenance(measurement, project)["notice"] is None


def test_evaluate_provenance_before_the_marker(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    from evolution_lab.quickstart._evaluate import _evaluation_provenance
    from evolution_lab.quickstart._readout import render_readout
    from test_evaluation import bindings

    kwargs = bindings(tmp_path)
    asyncio.run(evaluate_fixed(**kwargs))
    provenance = _evaluation_provenance(kwargs["run_directory"], tmp_path)
    assert provenance == {
        "recorded": False, "reported": None, "overflow": None, "not_reported": None,
        "notice": "provenance_not_recorded",
    }
    readout = render_readout({"provenance": provenance}, tmp_path)
    assert "before provenance was recorded" in readout
