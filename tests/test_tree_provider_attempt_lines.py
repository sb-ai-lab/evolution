"""#290 §4.4, G6, C4: the operator's line about failed provider attempts.

A chat Editor Attempt, a chat reflection or a chat grouping operation that
had a failed provider attempt tells the operator once (PDR-0015, as amended
2026-10-21), on stderr and in the process log, listing every failed attempt
in order, the preflight's included. One that went on gets `<role>
<operation> needed provider retries (...); it continued`, labelled
`(tool-dialect preflight)` when all the failed attempts were the
preflight's, through the campaign's `notes` sink as the invocation returns.
One that ended in a provider failure gets only `<role> <operation> provider
attempts: ... (<reason>)`, which
`CampaignResult.provider_attempt_lines` carries out so `evo tree evolve`
prints it right after the suspension report. Both are built from attempt
telemetry only (`role_call.AttemptTelemetry`): statuses, provider codes and
counts, never request content or provider text. The journal, the stdout
document and the projections are unchanged.

The campaigns run on family 14 against the loopback `fake_chat_server`, as
`tests/test_tree_chat_credentials.py` does.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tests.fake_chat_server as fake_chat_server
from evolution.tree import evolve as evolve_module
from evolution.tree.campaign import CampaignResult
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.grouping.model_grouping_runtime import run_model_grouping
from evolution.tree.roles.role_call import (
    AttemptRecorder,
    AttemptSummary,
    AttemptTelemetry,
    attempt_line,
    is_attempt_line,
)
from evolution.tree.roles.role_failures import GrouperRoleFailure
from evolution.tree.roles.workspace import open_attempt_log

#: `role_call_retry/1` with no back-off, so a scripted failure costs no time.
FAST_RETRY = {
    "version": "role_call_retry/1",
    "max_attempts": 3,
    "total_budget_seconds": 60,
    "backoff": {"initial_seconds": 0, "multiplier": 1, "max_seconds": 0, "jitter": "full"},
}
CREDENTIAL = "; the endpoint asked for a credential; this role declares no api_key_env"
HTTP_503 = {"status": 503}
PREFIX = "evo tree evolve: "


# -- the line itself ---------------------------------------------------------


def test_a_recovered_line_lists_its_statuses_and_says_it_continued() -> None:
    summary = AttemptSummary(((503, None), (503, None)), preflight=False)

    assert attempt_line("reflector", "r01", summary, failure=None, unauthenticated=False) == (
        "reflector r01 needed provider retries (2 failed attempts: http 503, http 503); "
        "it continued"
    )


def test_a_recovered_preflight_is_labelled_and_one_attempt_is_singular() -> None:
    summary = AttemptSummary(((503, None),), preflight=True)

    assert attempt_line("reflector", "r01", summary, failure=None, unauthenticated=False) == (
        "reflector r01 needed provider retries (1 failed attempt: http 503); it continued "
        "(tool-dialect preflight)"
    )


def test_a_failed_line_ends_with_the_failure_reason() -> None:
    summary = AttemptSummary(((503, None),) * 3, preflight=False)

    assert attempt_line(
        "reflector", "r01", summary, failure="provider_unavailable", unauthenticated=False
    ) == ("reflector r01 provider attempts: http 503, http 503, http 503 (provider_unavailable)")


@pytest.mark.parametrize("status", [401, 403])
def test_a_credential_status_reads_credential_and_names_the_missing_key(status: int) -> None:
    summary = AttemptSummary(((status, None),), preflight=True)
    line = attempt_line(
        "editor", "r01-a01", summary, failure="provider_rejected", unauthenticated=True
    )
    declared = attempt_line(
        "editor", "r01-a01", summary, failure="provider_rejected", unauthenticated=False
    )

    assert line == (
        f"editor r01-a01 provider attempts: http {status} (credential) (provider_rejected)"
        + CREDENTIAL
    )
    assert declared == (
        f"editor r01-a01 provider attempts: http {status} (credential) (provider_rejected)"
    )


def test_provider_codes_no_response_classes_and_the_eight_status_cap() -> None:
    failed = ((429, "rate_limit_exceeded"), (None, "timeout"), (None, "connection"))
    many = AttemptSummary(failed + ((503, None),) * 7, preflight=False)

    line = attempt_line("grouper", "r02", many, failure=None, unauthenticated=False)

    assert line == (
        "grouper r02 needed provider retries (10 failed attempts: http 429 "
        "rate_limit_exceeded, timeout, connection, http 503, http 503, http 503, http 503, "
        "http 503, …); it continued"
    )


def test_nothing_failed_is_no_line() -> None:
    empty = AttemptSummary((), preflight=False)

    assert attempt_line("editor", "r01-a01", empty, failure=None, unauthenticated=True) is None
    assert attempt_line("editor", "r01-a01", empty, failure="x", unauthenticated=True) is None


def test_the_cli_recognises_its_attempt_lines() -> None:
    summary = AttemptSummary(((503, None),), preflight=False)
    for failure in (None, "provider_unavailable"):
        line = attempt_line("reflector", "r01", summary, failure=failure, unauthenticated=False)
        assert line is not None and is_attempt_line(line)
    assert not is_attempt_line("forwarded value X is shorter than 8 characters")


def _telemetry(outcome: str, status: int | None = None, code: str | None = None) -> Any:
    return AttemptTelemetry("reflector", "r-t0", 1, 0.0, outcome, status, code)


def test_the_recorder_folds_every_phase_into_one_summary() -> None:
    """Telemetry only: an `ok` attempt is not a failure, a failure without a
    response is its outcome class, and the preflight's failures come first in
    the one summary, which is `preflight` only when all of them are."""

    recorder = AttemptRecorder()
    with recorder.preflight():
        recorder.observe(_telemetry("http_5xx", 503))
        recorder.observe(_telemetry("ok"))
    recorder.observe(_telemetry("timeout"))
    recorder.observe(_telemetry("http_429", 429, "slow_down"))
    recorder.observe(_telemetry("ok"))

    report = recorder.report(unauthenticated=False)

    assert report is not None
    assert report.summary == AttemptSummary(
        ((503, None), (None, "timeout"), (429, "slow_down")), preflight=False
    )
    assert report.failure is None
    assert report.lines("reflector", "r01") == (
        (
            "reflector r01 needed provider retries (3 failed attempts: http 503, timeout, "
            "http 429 slow_down); it continued",
        ),
        (),
    )
    assert AttemptRecorder().report(unauthenticated=False) is None


def test_a_withdrawn_dialect_rejection_keeps_the_earlier_failures() -> None:
    from evolution.tree.roles.role_call import RoleCallExhausted

    recorder = AttemptRecorder()
    rejection = RoleCallExhausted("provider_rejected", 2, "http_4xx")
    with recorder.preflight():
        recorder.observe(_telemetry("http_5xx", 503))
        recorder.observe(_telemetry("http_4xx", 400))
        recorder.exhausted(rejection)
        recorder.withdraw(rejection)
        recorder.observe(_telemetry("ok"))

    report = recorder.report(unauthenticated=False)

    assert report is not None
    assert report.summary == AttemptSummary(((503, None),), preflight=True)


def test_an_ended_invocation_gives_only_its_failed_line() -> None:
    from evolution.tree.roles.role_call import RoleCallExhausted

    recorder = AttemptRecorder()
    with recorder.preflight():
        recorder.observe(_telemetry("http_5xx", 503))
        recorder.observe(_telemetry("ok"))
    exhausted = RoleCallExhausted("provider_unavailable", 1, "http_5xx")
    recorder.observe(_telemetry("http_5xx", 502))
    recorder.exhausted(exhausted)
    recorder.end(exhausted)

    report = recorder.report(unauthenticated=False)

    assert report is not None
    assert report.lines("editor", "r01-a01") == (
        (),
        ("editor r01-a01 provider attempts: http 503, http 502 (provider_unavailable)",),
    )


# -- campaigns ---------------------------------------------------------------


def _section(base_url: str, key: str | None) -> dict[str, Any]:
    section = {
        "kind": "chat",
        "model": fake_chat_server.MODEL,
        "base_url": base_url,
        "max_turns": 8,
        "retry": copy.deepcopy(FAST_RETRY),
    }
    if key is not None:
        section["api_key_env"] = key
    return section


@pytest.fixture
def servers() -> Iterator[list[Any]]:
    started: list[Any] = []
    yield started
    for server in started:
        server.shutdown()
        server.server_close()


async def _campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: list[Any],
    *,
    editor_script: list[Any] | None = None,
    reflect_script: list[Any] | None = None,
    reflector_key: str | None = "E1_KEY",
    campaign: dict[str, Any] | None = None,
) -> tuple[CampaignResult, list[str], Path]:
    from evolution.tree.evolve import evolve
    from tests.test_tree_evolve import (
        CHAT_KEY,
        CHAT_REFLECT_SCRIPT,
        CHAT_SCRIPT,
        fake_adapters,
    )
    from tests.tree_evolve_support import CAMPAIGN_PAYLOAD, write_workspace

    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    edit_url, edit = fake_chat_server.start(editor_script or CHAT_SCRIPT)
    servers.append(edit)
    reflect_url, reflect = fake_chat_server.start(reflect_script or CHAT_REFLECT_SCRIPT)
    servers.append(reflect)
    workspace = tmp_path / "ws"
    manifest = write_workspace(
        workspace,
        editor=_section(edit_url, "E1_KEY"),
        reflector=_section(reflect_url, reflector_key),
        campaign={**copy.deepcopy(CAMPAIGN_PAYLOAD), **(campaign or {})},
    )
    notes: list[str] = []
    run_dir = workspace / "run"
    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters(), notes=notes.append)
    return result, notes, run_dir


def _attempt_notes(notes: list[str]) -> list[str]:
    return [note for note in notes if is_attempt_line(note)]


def _list_files() -> dict[str, Any]:
    return fake_chat_server.tool_call("list_files")


def _hanging(item: Any) -> dict[str, Any]:
    """`item`, served only after the role's deadline has passed."""

    return {"delay": 6, "body": item}


@pytest.fixture
def outer_deadline_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push the chat sessions' own deadline a minute out, so the runner's role
    deadline (`run_editor`, `run_reflector`) is what ends a hanging request:
    the task is cancelled and never returns a result."""

    from evolution.tree.roles.editor import chat_editor
    from evolution.tree.roles.reflector import chat_reflector

    for cls in (chat_editor._Session, chat_reflector._ReflectSession):
        real = cls.__init__

        def init(self: Any, *args: Any, _real: Any = real, **kwargs: Any) -> None:
            _real(self, *args, **kwargs)
            self.deadline += 60

        monkeypatch.setattr(cls, "__init__", init)


async def test_no_line_when_nothing_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    result, notes, _ = await _campaign(tmp_path, monkeypatch, servers)

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == []
    assert result.provider_attempt_lines == ()


async def test_a_recovered_preflight_gives_one_labelled_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    from tests.test_tree_evolve import CHAT_REFLECT_SCRIPT

    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        reflect_script=[HTTP_503, HTTP_503, *CHAT_REFLECT_SCRIPT],
    )

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == [
        "reflector r01 needed provider retries (2 failed attempts: http 503, http 503); "
        "it continued (tool-dialect preflight)"
    ]
    assert result.provider_attempt_lines == ()


async def test_a_recovered_preflight_and_a_recovered_request_give_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    from tests.test_tree_evolve import CHAT_REFLECT_SCRIPT

    first, *rest = CHAT_REFLECT_SCRIPT
    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        reflect_script=[HTTP_503, first, {"status": 429}, *rest],
    )

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == [
        "reflector r01 needed provider retries (2 failed attempts: http 503, http 429); "
        "it continued"
    ]


async def test_a_dialect_fallback_is_no_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    """The first style's 400 is answered by the next style: negotiation, not
    a failure, so no line; the role log still records the refusal."""

    from tests.test_tree_evolve import CHAT_REFLECT_SCRIPT

    result, notes, run_dir = await _campaign(
        tmp_path, monkeypatch, servers, reflect_script=[{"status": 400}, *CHAT_REFLECT_SCRIPT]
    )

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == []
    records = [
        json.loads(line)
        for path in (run_dir / "reflector-logs").glob("r01-x01.jsonl")
        for line in path.read_text().splitlines()
    ]
    failed = [r for r in records if r.get("phase") == "model_request_failed"]
    assert [r["http_status"] for r in failed] == [400]


async def test_a_retried_preflight_503_before_a_dialect_fallback_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    from tests.test_tree_evolve import CHAT_REFLECT_SCRIPT

    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        reflect_script=[HTTP_503, {"status": 400}, *CHAT_REFLECT_SCRIPT],
    )

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == [
        "reflector r01 needed provider retries (1 failed attempt: http 503); it continued "
        "(tool-dialect preflight)"
    ]


async def test_an_editor_attempt_that_recovered_a_429_gives_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    from tests.test_tree_evolve import CHAT_SCRIPT

    first, *rest = CHAT_SCRIPT
    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        editor_script=[first, {"status": 429, "code": "rate_limited"}, *rest],
    )

    assert result.terminal == "finished"
    assert _attempt_notes(notes) == [
        "editor r01-a01 needed provider retries (1 failed attempt: http 429 rate_limited); "
        "it continued"
    ]


@pytest.mark.parametrize("failed_first", [True, False], ids=["503-then-deadline", "deadline"])
async def test_a_reflection_cancelled_at_its_deadline_tells_its_failed_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: list[Any],
    outer_deadline_first: None,
    failed_first: bool,
) -> None:
    """A 503, then a request that hangs past the role deadline: the runner
    cancels the reflection, and its one failed line still names the 503."""

    from tests.test_tree_evolve import CHAT_REFLECT_SCRIPT

    first, *rest = CHAT_REFLECT_SCRIPT
    hang = [first, _hanging(rest[0])]
    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        reflect_script=[*([HTTP_503] if failed_first else []), *hang, *CHAT_REFLECT_SCRIPT],
        campaign={"reflection_timeout_seconds": 2},
    )

    lines = [*_attempt_notes(notes), *result.provider_attempt_lines]
    if failed_first:
        assert lines == ["reflector r01 provider attempts: http 503 (deadline_exhausted)"]
    else:
        assert lines == []


@pytest.mark.parametrize("failed_first", [True, False], ids=["503-then-deadline", "deadline"])
async def test_an_editor_attempt_cancelled_at_its_deadline_tells_its_failed_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: list[Any],
    outer_deadline_first: None,
    failed_first: bool,
) -> None:
    from tests.test_tree_evolve import CHAT_SCRIPT

    first, second, *rest = CHAT_SCRIPT
    result, notes, _ = await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        editor_script=[
            first,
            *([HTTP_503] if failed_first else []),
            _hanging(second),
            *rest,
        ],
        campaign={"editor_timeout_seconds": 2},
    )

    lines = [*_attempt_notes(notes), *result.provider_attempt_lines]
    if failed_first:
        assert lines == ["editor r01-a01 provider attempts: http 503 (deadline_exhausted)"]
    else:
        assert lines == []


async def _failed_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> tuple[CampaignResult, list[str], Path]:
    """Review Focus 4: the preflight recovers from a 503, then the first full
    request fails three times; the reflection ends `provider_exhausted`."""

    return await _campaign(
        tmp_path,
        monkeypatch,
        servers,
        reflect_script=[HTTP_503, _list_files(), HTTP_503, HTTP_503, HTTP_503],
    )


#: The preflight's 503 first, then the full request's three.
FAILED_LINE = (
    "reflector r01 provider attempts: http 503, http 503, http 503, http 503 (provider_unavailable)"
)


async def test_a_failed_full_request_after_a_recovered_preflight_gives_one_failed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    result, notes, _ = await _failed_reflection(tmp_path, monkeypatch, servers)

    assert (result.terminal, result.code) == ("suspended", "reflector_provider_failed")
    assert _attempt_notes(notes) == []
    assert result.provider_attempt_lines == (FAILED_LINE,)


@pytest.mark.parametrize("key", [None, "E1_KEY"], ids=["unauthenticated", "declared"])
async def test_a_401_names_the_missing_credential_only_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any], key: str | None
) -> None:
    """Review Focus 5."""

    result, notes, _ = await _campaign(
        tmp_path, monkeypatch, servers, reflect_script=[{"status": 401}], reflector_key=key
    )

    assert (result.terminal, result.code) == ("suspended", "reflector_provider_failed")
    assert _attempt_notes(notes) == []
    line = "reflector r01 provider attempts: http 401 (credential) (provider_rejected)"
    assert result.provider_attempt_lines == ((line + CREDENTIAL) if key is None else line,)


def _invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: CampaignResult, notes: list[str]
) -> Any:
    """`evo tree evolve` over a replayed run: its notes, then its result (the
    `tests/test_tree_cli_family14.py` harness)."""

    from evolution.tree.cli import tree_app
    from tests.campaign_fakes import family14_admitted
    from tests.test_tree_cli_family14 import _manifest

    family14_admitted(monkeypatch)

    async def evolved(*args: Any, notes: Any = None, **kwargs: Any) -> CampaignResult:
        for note in replayed:
            notes(note)
        return result

    replayed = list(notes)
    monkeypatch.setattr(evolve_module, "evolve", evolved)
    root = tmp_path / "cli"
    manifest = _manifest(root)
    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(root / "run")]
    )
    assert invocation.exception is None or isinstance(invocation.exception, SystemExit)
    return invocation


def test_the_failed_line_follows_the_suspension_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    result, notes, _ = asyncio.run(_failed_reflection(tmp_path, monkeypatch, servers))

    invocation = _invoke(tmp_path, monkeypatch, result, notes)

    lines = invocation.stderr.splitlines()
    suspended = next(
        i for i, line in enumerate(lines) if line.startswith("evo tree evolve: suspended: ")
    )
    assert lines[suspended].startswith("evo tree evolve: suspended: reflector_provider_failed — ")
    assert [line for line in lines if is_attempt_line(line.removeprefix(PREFIX))] == [
        f"evo tree evolve: {FAILED_LINE}"
    ]
    assert lines[suspended + 1] == f"evo tree evolve: {FAILED_LINE}"
    assert lines[suspended + 2].startswith("evo tree evolve: continue: evo tree evolve ")
    assert all("note: reflector" not in line for line in lines)


def test_the_journal_stdout_and_projections_are_unchanged_by_the_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[Any]
) -> None:
    """The stdout document is byte-equal with and without the lines, and no
    durable record or projection of the run carries their text."""

    from evolution.tree.campaign import inspect_campaign

    result, notes, run_dir = asyncio.run(_failed_reflection(tmp_path, monkeypatch, servers))
    with_lines = _invoke(tmp_path / "with", monkeypatch, result, notes)
    without = _invoke(
        tmp_path / "without",
        monkeypatch,
        replace(result, provider_attempt_lines=()),
        [note for note in notes if not is_attempt_line(note)],
    )

    assert with_lines.stdout == without.stdout
    assert f"evo tree evolve: {FAILED_LINE}" in with_lines.stderr
    assert FAILED_LINE not in without.stderr
    projections = [
        json.dumps(result.status.to_payload(), default=str),
        json.dumps(inspect_campaign(run_dir).to_payload(), default=str),
    ]
    for text in projections:
        assert "provider attempts" not in text and "needed provider retries" not in text
    for path in run_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            assert b"provider attempts:" not in data, path
            assert b"needed provider retries" not in data, path


# -- the Grouper ---------------------------------------------------------------


async def _grouping(tmp_path: Path, script: list[Any], attempts: AttemptRecorder) -> Any:
    from tests.test_tree_chat_credentials import grouping_section
    from tests.tree.roles.grouping.test_model_grouping_runtime import NEEDLE, PROMPT, REG, SECRET

    async def no_wait(_seconds: float) -> None:
        return None

    base_url, server = fake_chat_server.start(script)
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=(SECRET,))
    try:
        return await run_model_grouping(
            REG,
            section=grouping_section(base_url, None),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials(None, None),
            log=log,
            deadline=float("inf"),
            cancelled=lambda: False,
            forbidden_secret=lambda text: SECRET in text,
            oracle_needle=lambda text: NEEDLE in text,
            sleep=no_wait,
            family14=True,
            attempts=attempts,
        )
    finally:
        log.close()
        server.shutdown()
        server.server_close()


async def test_a_grouping_that_recovered_records_its_line(tmp_path: Path) -> None:
    from tests.test_tree_chat_credentials import _grouping_answer

    attempts = AttemptRecorder()
    outcome = await _grouping(tmp_path, [HTTP_503, _grouping_answer], attempts)

    assert outcome.fallback is None
    report = attempts.report(unauthenticated=True)
    assert report is not None
    assert report.lines("grouper", "r01") == (
        ("grouper r01 needed provider retries (1 failed attempt: http 503); it continued",),
        (),
    )


async def test_a_grouping_provider_failure_records_its_failed_line(tmp_path: Path) -> None:
    attempts = AttemptRecorder()
    with pytest.raises(GrouperRoleFailure) as caught:
        await _grouping(tmp_path, [{"status": 401}], attempts)

    assert caught.value.reason == "provider_rejected"
    report = attempts.report(unauthenticated=True)
    assert report is not None
    assert report.lines("grouper", "r01") == (
        (),
        ("grouper r01 provider attempts: http 401 (credential) (provider_rejected)" + CREDENTIAL,),
    )


async def test_a_campaign_grouping_that_recovered_tells_the_operator_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor reads its own recorder whatever ends the operation."""

    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family14_run import family14_run
    from tests.test_tree_chat_credentials import _grouper, _grouping_answer

    told: list[tuple[str, str, Any]] = []
    real = _Campaign.note_provider_attempts

    def note(self: Any, role: str, operation: str, report: Any) -> None:
        told.append((role, operation, report))
        real(self, role, operation, report)

    monkeypatch.setattr(_Campaign, "note_provider_attempts", note)
    base_url, server = fake_chat_server.start([HTTP_503, _grouping_answer])
    try:
        _, snapshot = await family14_run(
            tmp_path, monkeypatch, family14_settings={"grouper": _grouper(base_url, None)}
        )
    finally:
        server.shutdown()
        server.server_close()

    assert snapshot.finished is not None
    grouper = [(operation, report) for role, operation, report in told if role == "grouper"]
    assert len(grouper) == 1
    operation, report = grouper[0]
    assert report is not None
    assert report.lines("grouper", operation) == (
        ("grouper r01 needed provider retries (1 failed attempt: http 503); it continued",),
        (),
    )


@pytest.mark.parametrize("inner", ["401", "403"])
@pytest.mark.parametrize("unauthenticated", [False, True])
def test_router_credential_body_marks_the_line_and_missing_key(inner, unauthenticated):
    summary = AttemptSummary(((200, inner),), preflight=True)
    line = attempt_line(
        "reflector", "r01", summary, failure="provider_rejected", unauthenticated=unauthenticated
    )
    assert line == (
        f"reflector r01 provider attempts: http 200 {inner} (credential) (provider_rejected)"
        + (CREDENTIAL if unauthenticated else "")
    )


@pytest.mark.parametrize(
    "status,inner", [(400, "401"), (503, "403"), (200, "503"), (200, None), (None, "401")]
)
def test_noncredential_transport_or_body_keeps_its_attempt_line(status, inner):
    summary = AttemptSummary(((status, inner),), preflight=False)
    line = attempt_line(
        "grouper", "r01", summary, failure="provider_unavailable", unauthenticated=True
    )
    assert "(credential)" not in line and CREDENTIAL not in line
