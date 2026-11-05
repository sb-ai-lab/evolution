"""CLI diagnostic output and the unchanged refusal boundary on real runs."""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import select
import warnings
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.tree import campaign, process_log
from evolution.tree import evolve as evolve_module
from evolution.tree.execution.authorizations import (
    Authorization,
    hygiene_instance_digest,
    write_decision,
)
from evolution.tree.execution.output_admission import EvolveRefused
from tests.campaign_drivers import _cut_hook
from tests.campaign_fakes import Crash, FakeAdapter, family14_admitted
from tests.test_tree_evolve import held_campaign_lock, listing
from tests.test_tree_family14_public_commands import (
    _evaluation_manifest,
)
from tests.test_tree_family14_public_commands import (
    scaffold15 as scaffold15,
)
from tests.test_tree_family14_public_links import _workspace15

pytestmark = pytest.mark.slow

RUNNER = CliRunner()


def invoke(kind, manifest, run, *extra):
    return RUNNER.invoke(
        app, ["tree", kind, "--manifest", str(manifest), "--run-dir", str(run), *extra]
    )


def logs(run, command="evolve"):
    return sorted((run / "logs").glob(f"{command}-*.log"))


def copied(path, stream):
    result = []
    for line in path.read_text().split("\n"):
        if not line:
            continue
        _, tag, text = line.split(" ", 2)
        if tag in (stream, stream + "+"):
            result.append(text + ("\n" if tag == stream else ""))
    return "".join(result)


@contextmanager
def without_capture(*args, **kwargs):
    yield SimpleNamespace(set_redactions=lambda *a: None, materialize=lambda *a: None)


def unfinished(manifest, run, monkeypatch):
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            asyncio.run(evolve_module.evolve(manifest, run))


@pytest.mark.parametrize("progress", [False, True])
def test_a_completed_run_leaves_one_private_log_and_reopen_adds_none(scaffold15, progress):
    run = scaffold15.parent / "run"
    extra = ("--progress",) if progress else ()
    first = invoke("evolve", scaffold15, run, *extra)
    assert first.exit_code == 0, first.output
    (path,) = logs(run)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    text = path.read_text()
    assert "· evolve ·" in text and f"pid {os.getpid()}" in text
    assert text.endswith("log exit 0\n")
    assert copied(path, "stdout") == first.stdout
    assert copied(path, "stderr") == first.stderr
    assert "continue:" in first.stderr
    before = listing(run)
    again = invoke("evolve", scaffold15, run, *extra)
    assert again.exit_code == 0 and json.loads(again.stdout) == json.loads(first.stdout)
    assert listing(run) == before


def test_a_suspended_run_and_resume_leave_one_log_each(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_connection_run import ConnectedAdapter, _script

    path = _workspace15(tmp_path)
    adapter = ConnectedAdapter()
    # This workspace's contract reads message/text, unlike connected_run's
    # assistant_message/content contract.
    adapter.probe_events = ({"type": "message", "text": "probe"},)
    _script(adapter, "materialize")
    family14_admitted(monkeypatch)
    monkeypatch.setattr(evolve_module, "_http_session_adapter", lambda *a, **k: adapter)
    run = tmp_path / "run"
    first = invoke("evolve", path, run)
    assert first.exit_code == 1, first.output
    (original,) = logs(run)
    assert original.read_text().endswith("log exit 1\n")
    assert json.loads(first.stdout)["terminal"] == "suspended"
    adapter.probe_materialize = None
    second = invoke("evolve", path, run)
    assert second.exit_code == 0, second.output
    assert len(logs(run)) == 2
    (new,) = set(logs(run)) - {original}
    assert new.read_text().endswith("log exit 0\n")
    assert copied(new, "stdout") == second.stdout


@pytest.mark.parametrize(
    "problem", ["run_identity_changed", "run_dir_not_campaign", "hygiene", "journal_busy"]
)
@pytest.mark.parametrize("progress", [False, True])
def test_refusals_keep_stderr_and_the_run_directory_unchanged(
    scaffold15, monkeypatch, problem, progress
):
    path = scaffold15
    run = path.parent / "run"
    unfinished(path, run, monkeypatch)
    if problem in ("run_identity_changed", "journal_busy"):
        scorer = path.parent / "scorer" / "score.py"
        scorer.write_text(scorer.read_text() + "\n# changed\n")
    elif problem == "run_dir_not_campaign":
        run = path.parent / "foreign"
        run.mkdir(mode=0o700)
        (run / "foreign.txt").write_text("untouched")
    elif problem == "hygiene":
        (run / "scorer-traces").mkdir(mode=0o750, exist_ok=True)
        (run / "scorer-traces").chmod(0o750)
    if problem == "journal_busy":
        # Preserve the existing state-first busy-refusal semantics (U1).
        with pytest.raises(EvolveRefused) as caught:
            asyncio.run(evolve_module.evolve(path, run))
        decision = Authorization.new(
            "run_identity_changed",
            caught.value.instance_digest,
            "close changed condition",
            effect="close",
        )
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_close_applied")
            with pytest.raises(Crash):
                asyncio.run(evolve_module.evolve(path, run, authorizations=[decision]))
    before = listing(run)
    extra = ("--progress",) if progress else ()
    with held_campaign_lock(run) if problem == "journal_busy" else nullcontext():
        with monkeypatch.context() as patch:
            patch.setattr(process_log, "capture", without_capture)
            baseline = invoke("evolve", path, run, *extra)
        result = invoke("evolve", path, run, *extra)
    assert result.exit_code == baseline.exit_code == 2, result.output
    assert result.stdout == baseline.stdout == ""
    assert result.stderr == baseline.stderr
    assert listing(run) == before and not logs(run)


def test_a_retired_manifest_refusal_creates_no_log(tmp_path):
    from tests.campaign_drivers import v14_document
    from tests.tree.manifest.test_manifest_v2 import write_v5_document

    path = write_v5_document(tmp_path, v14_document())
    run = tmp_path / "refused"
    result = invoke("evolve", path, run)
    assert result.exit_code == 2 and "manifest_schema" in result.stderr
    assert not run.exists()


@pytest.mark.parametrize("after_admission", [False, True])
def test_an_unhandled_exception_is_logged_only_after_the_last_refusal_point(
    scaffold15, monkeypatch, after_admission
):
    key = "PROCESS_LOG_SECRET"
    secret = "private-secret-value"
    monkeypatch.setenv(key, secret)
    document = json.loads(scaffold15.read_text())
    document["editor"]["allowed_env"] = [key]
    scaffold15.write_text(json.dumps(document))

    def fail(*args, **kwargs):
        raise RuntimeError("boom " + secret)

    if after_admission:
        monkeypatch.setattr(campaign._Campaign, "_write_hygiene_applications", fail)
    else:
        monkeypatch.setattr(evolve_module, "run_dir_state", fail)
    run = scaffold15.parent / "run"
    result = invoke("evolve", scaffold15, run)
    assert isinstance(result.exception, RuntimeError)
    if after_admission:
        (path,) = logs(run)
        text = path.read_text()
        assert "RuntimeError: boom [REDACTED]" in text and secret not in text
        assert text.endswith("log exit 1 (unhandled RuntimeError)\n")
    else:
        assert not logs(run) and not run.exists()


def test_notes_warnings_and_exceptions_use_the_campaign_secret_set(scaffold15, monkeypatch):
    secret = "private-secret-value"
    monkeypatch.setenv("PROCESS_LOG_SECRET", secret)
    monkeypatch.setenv("SHORT_VALUE", "abcdef")
    document = json.loads(scaffold15.read_text())
    document["editor"]["allowed_env"] = ["PROCESS_LOG_SECRET", "SHORT_VALUE"]
    scaffold15.write_text(json.dumps(document))
    # Intercept the caller-owned note boundary while keeping the real engine.
    real_evolve = evolve_module.evolve

    async def engine(*args, **kwargs):
        kwargs["notes"]("note contains " + secret)
        return await real_evolve(*args, **kwargs)

    def terminal_warning(message, category, filename, lineno, file=None, line=None):
        import sys

        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

    def fail(*args, **kwargs):
        warnings.warn("warning contains " + secret, UserWarning, stacklevel=1)
        raise RuntimeError("exception contains " + secret)

    monkeypatch.setattr(warnings, "showwarning", terminal_warning)
    monkeypatch.setattr(evolve_module, "evolve", engine)
    monkeypatch.setattr(campaign._Campaign, "_write_hygiene_applications", fail)
    run = scaffold15.parent / "run"
    result = invoke("evolve", scaffold15, run)
    assert isinstance(result.exception, RuntimeError)
    (path,) = logs(run)
    text = path.read_text()
    assert secret not in text and "[REDACTED]" in text
    assert "SHORT_VALUE is shorter than 8 characters" in text
    short_line = next(
        line
        for line in result.stderr.splitlines(keepends=True)
        if "forwarded value SHORT_VALUE " in line
    )
    assert short_line in copied(path, "stderr")
    assert "note contains [REDACTED]" in text and "warning contains [REDACTED]" in text


UNAUTHENTICATED_NOTE = (
    "evo tree evolve: reflector calls https://models.example/v1 without credentials "
    "(no api_key_env declared)\n"
)


def test_an_unauthenticated_role_is_noted_before_the_run_and_on_resume(scaffold15, monkeypatch):
    """#290 §4.1: one note per unauthenticated role on stderr before any role
    runs, kept by the process log, and repeated on the resume."""

    from tests.tree.manifest.test_manifest_v15 import BOTH, chat_document

    document = json.loads(scaffold15.read_text())
    reflector = chat_document(**BOTH)["reflector"]
    del reflector["api_key_env"]
    reflector["base_url"] = "https://models.example/v1"
    document["reflector"] = reflector
    scaffold15.write_text(json.dumps(document))
    started = []

    def fail(*args, **kwargs):
        started.append(True)
        raise RuntimeError("stop before any role")

    monkeypatch.setattr(campaign._Campaign, "_write_hygiene_applications", fail)
    run = scaffold15.parent / "run"
    first = invoke("evolve", scaffold15, run)
    assert isinstance(first.exception, RuntimeError), first.output
    assert first.stderr.count(UNAUTHENTICATED_NOTE) == 1
    assert first.stderr.index(UNAUTHENTICATED_NOTE) == 0
    (path,) = logs(run)
    assert UNAUTHENTICATED_NOTE in copied(path, "stderr")
    second = invoke("evolve", scaffold15, run)
    assert isinstance(second.exception, RuntimeError), second.output
    assert second.stderr.count(UNAUTHENTICATED_NOTE) == 1
    (new,) = set(logs(run)) - {path}
    assert UNAUTHENTICATED_NOTE in copied(new, "stderr")
    assert len(started) == 2


CREDENTIAL_WARNING = (
    "evo tree evolve: WARNING: editor sends its credential unencrypted to "
    "http://llm.internal:8000/v1\n"
)
TRAFFIC_WARNING = (
    "evo tree evolve: WARNING: reflector talks to http://10.0.0.5:8080/v1 without encryption\n"
)
INTERNAL_UNAUTHENTICATED_NOTE = (
    "evo tree evolve: reflector calls http://10.0.0.5:8080/v1 without credentials "
    "(no api_key_env declared)\n"
)


class _NoStdin(io.RawIOBase):
    """A standard input that fails the test if anything reads it (#295: no prompt)."""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        raise AssertionError("evo tree evolve read standard input")


def _internal_http_roles(manifest, monkeypatch, *, editor_url, reflector_url):
    """A chat Editor with a key at `editor_url` and a chat Reflector without one
    at `reflector_url`; the run stops before any role, as the #290 test does."""

    from tests.tree.manifest.test_manifest_v15 import BOTH, chat_document

    document = json.loads(manifest.read_text())
    chat = chat_document(**BOTH)
    document.update(editor=chat["editor"], reflector=chat["reflector"])
    document["editor"]["base_url"] = editor_url
    del document["reflector"]["api_key_env"]
    document["reflector"]["base_url"] = reflector_url
    manifest.write_text(json.dumps(document))
    monkeypatch.setenv("EVO_EDITOR_API_KEY", "editor-key-value-long")
    started = []

    def fail(*args, **kwargs):
        started.append(True)
        raise RuntimeError("stop before any role")

    monkeypatch.setattr(campaign._Campaign, "_write_hygiene_applications", fail)
    return started


def test_each_non_loopback_http_role_is_warned_before_the_run_and_on_resume(
    scaffold15, monkeypatch
):
    """#295: one `WARNING:` line per chat role on plain `http` to a host that is
    not loopback, before any role runs, on every invocation, kept by the process
    log; the credential form for a declared key, the traffic form without one,
    which also keeps #290's note. Nothing waits for input."""

    started = _internal_http_roles(
        scaffold15,
        monkeypatch,
        editor_url="http://llm.internal:8000/v1",
        reflector_url="http://10.0.0.5:8080/v1",
    )
    import typer.testing

    # The runner's standard input fails the test on any read.
    monkeypatch.setattr(
        typer.testing, "make_input_stream", lambda *_: io.BufferedReader(_NoStdin())
    )
    run = scaffold15.parent / "run"
    paths = []
    for _ in range(2):
        result = invoke("evolve", scaffold15, run)
        assert isinstance(result.exception, RuntimeError), result.output
        lines = result.stderr.splitlines(keepends=True)
        assert lines[:3] == [CREDENTIAL_WARNING, TRAFFIC_WARNING, INTERNAL_UNAUTHENTICATED_NOTE]
        assert "\x1b" not in result.stderr
        (path,) = set(logs(run)) - set(paths)
        paths.append(path)
        kept = copied(path, "stderr")
        for line in (CREDENTIAL_WARNING, TRAFFIC_WARNING, INTERNAL_UNAUTHENTICATED_NOTE):
            assert kept.count(line) == 1
    assert len(started) == 2


def test_the_warning_label_is_highlighted_on_a_terminal_only(scaffold15, monkeypatch):
    """#295: on a terminal the `WARNING:` label is highlighted; the text is the
    same once the styling is removed, and the process log keeps the plain text
    with no terminal colour code."""

    import re

    monkeypatch.delenv("NO_COLOR", raising=False)
    _internal_http_roles(
        scaffold15,
        monkeypatch,
        editor_url="http://llm.internal:8000/v1",
        reflector_url="http://10.0.0.5:8080/v1",
    )
    run = scaffold15.parent / "run"
    arguments = ["tree", "evolve", "--manifest", str(scaffold15), "--run-dir", str(run)]
    result = RUNNER.invoke(app, arguments, color=True)
    assert isinstance(result.exception, RuntimeError), result.output
    styled = result.stderr.splitlines(keepends=True)[:2]
    assert all("\x1b[" in line for line in styled)
    assert all(line.index("\x1b[") == len("evo tree evolve: ") for line in styled)
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in styled]
    assert plain == [CREDENTIAL_WARNING, TRAFFIC_WARNING]
    (path,) = logs(run)
    kept = copied(path, "stderr")
    assert "\x1b" not in path.read_text()
    assert kept.count(CREDENTIAL_WARNING) == kept.count(TRAFFIC_WARNING) == 1


@pytest.mark.parametrize(("no_color", "styled"), [("1", False), ("", True)])
def test_no_color_turns_the_warning_highlight_off_on_a_terminal(
    scaffold15, monkeypatch, no_color, styled
):
    """#295 and https://no-color.org: a non-empty `NO_COLOR` keeps the
    `WARNING:` label plain even on a terminal; an empty one does not."""

    monkeypatch.setenv("NO_COLOR", no_color)
    _internal_http_roles(
        scaffold15,
        monkeypatch,
        editor_url="http://llm.internal:8000/v1",
        reflector_url="http://10.0.0.5:8080/v1",
    )
    run = scaffold15.parent / "run"
    arguments = ["tree", "evolve", "--manifest", str(scaffold15), "--run-dir", str(run)]
    result = RUNNER.invoke(app, arguments, color=True)
    assert isinstance(result.exception, RuntimeError), result.output
    assert ("\x1b" in result.stderr) is styled
    if not styled:
        assert result.stderr.splitlines(keepends=True)[:2] == [CREDENTIAL_WARNING, TRAFFIC_WARNING]


@pytest.mark.parametrize(
    "editor_url",
    ["https://llm.internal:8000/v1", "http://127.0.0.1:8000/v1", "http://localhost:8000/v1"],
)
def test_https_and_loopback_http_print_no_warning(scaffold15, monkeypatch, editor_url):
    """#295, per-role isolation: only the non-loopback `http` Reflector is warned."""

    _internal_http_roles(
        scaffold15, monkeypatch, editor_url=editor_url, reflector_url="http://10.0.0.5:8080/v1"
    )
    result = invoke("evolve", scaffold15, scaffold15.parent / "run")
    assert isinstance(result.exception, RuntimeError), result.output
    assert "WARNING: editor" not in result.stderr
    assert result.stderr.count("WARNING:") == 1
    assert result.stderr.count(TRAFFIC_WARNING) == 1


def test_a_log_creation_failure_never_changes_the_campaign_outcome(scaffold15, monkeypatch):
    materialize = process_log.ProcessLog.materialize

    def fail(log, root, admitted=()):
        (root / "logs").write_text("unsafe after admission")
        materialize(log, root, admitted)

    monkeypatch.setattr(process_log.ProcessLog, "materialize", fail)
    result = invoke("evolve", scaffold15, scaffold15.parent / "run")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["terminal"] == "finished"
    assert result.stderr.count("evo tree evolve: note: process log disabled: unsafe\n") == 1


def test_a_hygiene_decision_reaches_log_materialization(scaffold15, monkeypatch):
    run = scaffold15.parent / "run"
    unfinished(scaffold15, run, monkeypatch)
    (run / "logs").mkdir(mode=0o750)
    (run / "logs").chmod(0o750)
    digest = hygiene_instance_digest("logs", "not owner-only", "0o750")
    write_decision(run, Authorization.new("run_dir_unsafe_entry", digest, "accept logs"))
    result = invoke("evolve", scaffold15, run)
    assert result.exit_code == 0, result.output
    (path,) = logs(run)
    assert path.read_text().endswith("log exit 0\n")


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("insufficient", [False, True])
def test_evaluation_logs_its_output_secrets_and_post_birth_refusal(
    scaffold15, monkeypatch, progress, insufficient
):
    campaign_root = scaffold15.parent / "campaign"
    result = asyncio.run(evolve_module.evolve(scaffold15, campaign_root))
    version = result.result.to_payload()["final_champion"]["version"]
    manifest = _evaluation_manifest(scaffold15, campaign_root, version)
    document = json.loads(manifest.read_text())
    document["scorer"]["allowed_env"] = ["EVALUATION_SECRET"]
    if insufficient:
        document["max_concurrency"] = 2
    monkeypatch.setenv("EVALUATION_SECRET", "evaluation-secret-value")
    manifest.write_text(json.dumps(document))
    from evolution.tree import evaluation

    real = evaluation.evaluate

    async def measured(*args, **kwargs):
        materialize = kwargs.get("process_log", lambda *a: None)

        def reached(*args):
            materialize(*args)
            warnings.warn("evaluation-secret-value", UserWarning, stacklevel=1)

        kwargs["process_log"] = reached
        if insufficient:
            kwargs["adapter_factory"] = lambda *a: FakeAdapter(
                max_instances=1, max_concurrent_sessions=1
            )
        return await real(*args, **kwargs)

    def terminal_warning(message, category, filename, lineno, file=None, line=None):
        import sys

        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

    monkeypatch.setattr(warnings, "showwarning", terminal_warning)
    monkeypatch.setattr(evaluation, "evaluate", measured)
    run = scaffold15.parent / "evaluation"
    extra = ("--progress",) if progress else ()
    first = invoke("evaluate", manifest, run, *extra)
    assert first.exit_code == (2 if insufficient else 0), first.output
    (path,) = logs(run, "evaluate")
    text = path.read_text()
    assert "evaluation-secret-value" not in text and "[REDACTED]" in text
    assert text.endswith(f"log exit {first.exit_code}\n")
    if insufficient:
        assert "capabilities_insufficient" in text
    else:
        assert copied(path, "stdout") == first.stdout
        before = listing(run)
        again = invoke("evaluate", manifest, run, *extra)
        assert again.exit_code == 0 and len(logs(run, "evaluate")) == 1
        assert listing(run) == before


def test_conformance_appends_a_complete_report_per_invocation(scaffold15):
    path = scaffold15.parent / "conformance.log"
    command = ["tree", "conformance", "--manifest", str(scaffold15), "--log-file", str(path)]
    first = RUNNER.invoke(app, command)
    assert first.exit_code == 0, first.output
    assert path.stat().st_mode & 0o777 == 0o600
    assert copied(path, "stdout") == first.stdout
    assert path.read_text().endswith("log exit 0\n")
    second = RUNNER.invoke(app, command)
    assert second.exit_code == 0, second.output
    text = path.read_text()
    assert text.count("log evolution ") == 2
    assert copied(path, "stdout") == first.stdout + second.stdout


@pytest.mark.parametrize("command", ["conformance", "serve"])
def test_explicit_log_records_a_manifest_refusal(tmp_path, command):
    from evolution.tree.reference_target_cli import app as target_app

    manifest = tmp_path / "bad.json"
    manifest.write_text('{"schema": "evolution-v2-manifest/999"}')
    path = tmp_path / "command.log"
    args = (
        ["tree", "conformance", "--manifest", str(manifest)]
        if command == "conformance"
        else ["serve", str(manifest)]
    )
    result = RUNNER.invoke(
        app if command == "conformance" else target_app, [*args, "--log-file", str(path)]
    )
    assert result.exit_code == 2, result.output
    # P4 / ADR-0046: every non-/15 or /16 schema has the same loader refusal.
    expected = (
        "conformance: manifest manifest_schema: schema: "
        if command == "conformance"
        else "evo-target: serve refused: manifest_schema; "
    ) + "expected evolution-v2-manifest/15 or /16\n"
    assert result.stderr == expected
    assert copied(path, "stderr") == result.stderr
    assert path.read_text().endswith("log exit 2\n")


@pytest.mark.parametrize("command", ["conformance", "serve"])
@pytest.mark.parametrize("unsafe", [True, False])
def test_log_file_refusal_precedes_manifest_loading(tmp_path, monkeypatch, command, unsafe):
    from evolution.tree import cli, reference_target_cli

    manifest = tmp_path / "manifest.json"
    manifest.write_text("unread")
    path = tmp_path / "output.log" if unsafe else tmp_path / "absent" / "output.log"
    if unsafe:
        path.write_text("untouched")
        path.chmod(0o644)

    def never_load(*args):
        pytest.fail("the log path must be proved before reading the manifest")

    module = cli if command == "conformance" else reference_target_cli
    monkeypatch.setattr(module, "load_manifest_v2", never_load)
    args = (
        ["tree", "conformance", "--manifest", str(manifest)]
        if command == "conformance"
        else ["serve", str(manifest)]
    )
    result = RUNNER.invoke(
        app if command == "conformance" else reference_target_cli.app,
        [*args, "--log-file", str(path)],
    )
    code = "log_file_unsafe" if unsafe else "log_file_unavailable"
    prefix = "conformance: log file" if command == "conformance" else "evo-target: serve refused:"
    assert result.exit_code == 2 and result.stdout == ""
    assert result.stderr == f"{prefix} {code}\n"
    if unsafe:
        assert path.read_text() == "untouched" and path.stat().st_mode & 0o777 == 0o644


def test_conformance_usage_error_does_not_open_the_requested_log(tmp_path):
    path = tmp_path / "output.log"
    result = RUNNER.invoke(
        app,
        ["tree", "conformance", "--manifest", str(tmp_path / "missing"), "--log-file", str(path)],
    )
    assert result.exit_code == 2 and not path.exists()


def test_serve_records_the_ready_line_and_clean_sigint(tmp_path):
    from tests.test_tree_reference_target_cli import free_port, scaffold_on_port, start_serve, stop

    manifest = scaffold_on_port(tmp_path, free_port())
    path = tmp_path / "serve.log"
    child = start_serve(manifest, "--log-file", str(path))
    try:
        assert select.select([child.stdout], [], [], 15)[0], "serve did not become ready"
        ready = child.stdout.readline()
        assert json.loads(ready)["status"] == "listening"
    finally:
        if child.poll() is None:
            code, out, err = stop(child)
        else:
            out, err = child.communicate(timeout=15)
            code = child.returncode
    assert (code, out, err) == (0, "", "")
    assert copied(path, "stdout") == ready
    assert path.read_text().endswith("log exit 0\n")


def test_readers_return_identical_evidence_with_or_without_logs(scaffold15, monkeypatch):
    from evolution.tree import run_watch
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evaluation import inspect_evaluation

    run = scaffold15.parent / "campaign"
    first = invoke("evolve", scaffold15, run)
    assert first.exit_code == 0, first.output
    champion = json.loads(first.stdout)["result"]["final_champion"]["version"]
    manifest = _evaluation_manifest(scaffold15, run, champion)
    measured = scaffold15.parent / "evaluation"
    assert invoke("evaluate", manifest, measured).exit_code == 0
    fixed_wall = datetime.now(UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_wall if tz is None else fixed_wall.astimezone(tz)

    # Evaluation watch summaries use observation time, independently of logs.
    # Hold it still so crossing a second cannot fail a byte comparison.
    monkeypatch.setattr(run_watch, "datetime", FixedDateTime)

    for root, inspector in (
        (run, lambda r: inspect_campaign(r, verify=True).to_payload()),
        (measured, lambda r: inspect_evaluation(r, verify=True)),
    ):
        commands = [
            ["tree", "watch", "--run-dir", str(root), "--once"],
            ["tree", "trace", "--run-dir", str(root), "--out", "-"],
            ["tree", "authorize", str(root), "--list"],
        ]
        before = inspector(root)
        assert not before.get("verification", {}).get("mismatches")
        present = [RUNNER.invoke(app, args) for args in commands]
        saved = root.parent / f"{root.name}-logs-saved"
        (root / "logs").rename(saved)
        try:
            assert inspector(root) == before
            absent = [RUNNER.invoke(app, args) for args in commands]
            assert [(r.exit_code, r.stdout, r.stderr) for r in present] == [
                (r.exit_code, r.stdout, r.stderr) for r in absent
            ]
            assert all(r.exit_code == 0 for r in present)
        finally:
            saved.rename(root / "logs")


def test_a_linked_successor_has_its_own_log_and_preserves_the_predecessor(tmp_path, monkeypatch):
    from tests.test_tree_family14_public_links import _cli, _fingerprint, _link

    predecessor_manifest = _workspace15(tmp_path / "first")
    predecessor = predecessor_manifest.parent / "run"
    first = _cli(predecessor_manifest, predecessor, monkeypatch)
    assert first.exit_code == 0, first.output
    assert len(logs(predecessor)) == 1
    before = (listing(predecessor), _fingerprint(predecessor))
    manifest = _workspace15(tmp_path / "second", **_link(predecessor, "fresh"))
    successor = manifest.parent / "run"
    second = _cli(manifest, successor, monkeypatch)
    assert second.exit_code == 0, second.output
    (path,) = logs(successor)
    assert path.read_text().endswith("log exit 0\n")
    assert (listing(predecessor), _fingerprint(predecessor)) == before


@pytest.mark.parametrize("problem", ["loader", "busy"])
@pytest.mark.parametrize("progress", [False, True])
def test_an_early_evaluation_refusal_keeps_stderr_and_files_unchanged(
    scaffold15, monkeypatch, problem, progress
):
    from evolution.tree.evolve import evolve

    campaign_root = scaffold15.parent / "campaign"
    result = asyncio.run(evolve(scaffold15, campaign_root))
    champion = result.result.to_payload()["final_champion"]["version"]
    manifest = _evaluation_manifest(scaffold15, campaign_root, champion)
    run = scaffold15.parent / "refused-evaluation"
    lock_fd = None
    if problem == "loader":
        manifest.write_text('{"schema": "evolution-v2-evaluation/999"}')
    else:
        run.mkdir(mode=0o700)
        lock_fd = os.open(run / "evaluation.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = listing(run) if run.exists() else None
    extra = ("--progress",) if progress else ()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(process_log, "capture", without_capture)
            baseline = invoke("evaluate", manifest, run, *extra)
        actual = invoke("evaluate", manifest, run, *extra)
        assert actual.exit_code == baseline.exit_code == (2 if problem == "loader" else 1)
        assert actual.stdout == baseline.stdout == ""
        assert actual.stderr == baseline.stderr
        assert (listing(run) if run.exists() else None) == before
        assert not logs(run, "evaluate")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
