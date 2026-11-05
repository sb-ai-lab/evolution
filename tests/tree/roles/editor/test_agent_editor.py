"""`AgentCliEditor` at its v2 seam: one projection, one CLI, one published tree.

The fake backend is the dormant test file's, on the v2 constructor: it records
every keyword argument the editor built it with, mutates the *real* projection
it is handed (so every guardrail below is exercised through actual files), and
is rebuilt once per CLI call, which is how a test sees that the first call and
the repair call never share `last_usage`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.prompts import TREE_AGENT_EDITOR_SYSTEM
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.editor.agent_editor import (
    AGENT_VALIDATION_LINE,
    CONTROL_PREFIX,
    REQUEST_VERSION,
    AgentCliEditor,
)
from evolution.tree.roles.editor.editor_brief import render_catalog, render_opening
from evolution.tree.roles.editor_contracts import (
    Diagnosis,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    EditRequest,
    brief_payload,
)
from evolution.tree.roles.seals import agent_identity, seal_program, seal_prompt
from evolution.tree.roles.workspace import Budgets

SECRET = "sekret-key-value"
CHILD_ENV = {"PATH": "/usr/bin", "OPENAI_API_KEY": SECRET}

# A neutral diagnosis for tests not themselves about the diagnosis field.
_DEFAULT_DIAGNOSIS = Diagnosis("absent", None, None, (), None)

# --------------------------------------------------------------------------
# Fakes and fixtures
# --------------------------------------------------------------------------


class FakeBackend:
    """One CLI call: the editor's kwargs, the projection it is given, one edit."""

    def __init__(self, *, edit: Any, usage: Any, diag: Any, content: str, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._edit = edit
        self._content = content
        self.calls = 0
        self.work_dir: Path | None = None
        self.opening: str | None = None
        self.system: str | None = None
        self.timeout_s: float | None = None
        self.last_usage = dict(usage)
        self.last_codex_diagnostics = dict(diag)

    async def complete_file_edit(
        self,
        work_dir: Path,
        prompt: str,
        system: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        self.calls += 1
        self.work_dir = Path(work_dir)
        self.opening = prompt
        self.system = system
        self.timeout_s = timeout_s
        self._edit(Path(work_dir))
        return SimpleNamespace(content=self._content, tokens_in=2, tokens_out=3, time_ms=4)


def factory(
    *,
    edits: list[Any],
    usage: list[dict[str, Any]] | None = None,
    diag: dict[str, Any] | None = None,
    content: str = "done",
) -> tuple[Any, list[FakeBackend]]:
    """A `backend_factory` building a fresh instance per call, kept in one list."""

    instances: list[FakeBackend] = []
    usages = list(usage or [{"tool_turns": 1}])

    def build(**kwargs: Any) -> FakeBackend:
        index = len(instances)
        backend = FakeBackend(
            edit=edits[min(index, len(edits) - 1)],
            usage=usages[min(index, len(usages) - 1)],
            diag=dict(diag if diag is not None else {"last_message": "done"}),
            content=content,
            **kwargs,
        )
        instances.append(backend)
        return backend

    return build, instances


def _program(tmp_path: Path) -> Path:
    program = tmp_path / "bin" / "codex"
    if not program.exists():
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o755)
    return program


def _editor(
    tmp_path: Path,
    edits: list[Any],
    *,
    usage: list[dict[str, Any]] | None = None,
    diag: dict[str, Any] | None = None,
    content: str = "done",
    prompt: Any = None,
    log_root: Path | None = None,
    **section_kw: Any,
) -> tuple[AgentCliEditor, list[FakeBackend]]:
    build, instances = factory(edits=edits, usage=usage, diag=diag, content=content)
    section_kw.setdefault("model", "codex")
    editor = AgentCliEditor(
        section=AgentCliEditorSection(**section_kw),
        program=seal_program(_program(tmp_path)),
        prompt=prompt
        if prompt is not None
        else seal_prompt(None, packaged=TREE_AGENT_EDITOR_SYSTEM),
        child_env=dict(CHILD_ENV),
        log_root=log_root if log_root is not None else tmp_path / "run" / "editor-logs",
        secrets=(SECRET,),
        backend_factory=build,
    )
    return editor, instances


def _candidate(tmp_path: Path, name: str = "candidate") -> Path:
    root = tmp_path / name
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "SKILL.md").write_text("first\n", encoding="utf-8")
    (root / "skills" / "config.yaml").write_text("key: value\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    return root


def _brief(cases: int = 2) -> EditBrief:
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=tuple(
            EditBriefCase(
                case_id=f"c{index}",
                group_id="g1",
                stratum="s1",
                tags=("t",),
                public={"text": "hello"},
                repeats=(EditBriefRepeat(1, f"c{index}-r1", "rollout_finished", 0.5, True),),
            )
            for index in range(1, cases + 1)
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )


def _request(
    workspace: Path,
    *,
    attempt_id: str = "a1",
    writable: tuple[str, ...] = ("skills/**",),
    brief: EditBrief | None = None,
    deadline: float = 30.0,
) -> EditRequest:
    return EditRequest(attempt_id, 1, workspace, writable, brief or _brief(), deadline)


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every regular file's mode and bytes: what "the Candidate is untouched" means."""

    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _log_text(editor: AgentCliEditor, attempt_id: str = "a1") -> str:
    return (Path(editor.log_root) / f"{attempt_id}.jsonl").read_text(encoding="utf-8")


def _log_lines(editor: AgentCliEditor, attempt_id: str = "a1") -> list[dict[str, Any]]:
    return [json.loads(line) for line in _log_text(editor, attempt_id).splitlines() if line]


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Identity and construction
# --------------------------------------------------------------------------


def test_identity_is_declared_configuration_with_program_bytes(tmp_path: Path) -> None:
    editor, _instances = _editor(tmp_path, [lambda work: None])

    assert editor.identity == agent_identity(editor.section, editor.prompt, editor.program)
    assert editor.identity.kind == "agent_cli"

    # The CLI's bytes are in the digest; its path is not.
    moved = tmp_path / "elsewhere" / "codex"
    moved.parent.mkdir()
    moved.write_bytes(_program(tmp_path).read_bytes())
    assert agent_identity(editor.section, editor.prompt, seal_program(moved)) == editor.identity

    _program(tmp_path).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    assert (
        agent_identity(editor.section, editor.prompt, seal_program(_program(tmp_path)))
        != editor.identity
    )


def test_max_turns_needs_a_cli_that_reports_turns(tmp_path: Path) -> None:
    """The loader refuses this; the editor refuses to be built inert too."""

    with pytest.raises(ValueError):
        _editor(tmp_path, [lambda work: None], model="hermes/GigaChat-2-M", max_turns=4)

    editor, _instances = _editor(tmp_path, [lambda work: None], model="hermes/GigaChat-2-M")
    assert editor.section.max_turns is None


async def test_attempt_id_must_be_one_path_segment(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    editor, instances = _editor(tmp_path, [lambda work: None])

    with pytest.raises(ValueError):
        await editor.edit(_request(root, attempt_id="../escape"))
    assert instances == []


# --------------------------------------------------------------------------
# The control plane
# --------------------------------------------------------------------------


async def test_projection_carries_control_plane_and_evidence(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    brief = _brief(cases=2)
    observed: dict[str, Any] = {}

    def edit(work: Path) -> None:
        control = work / CONTROL_PREFIX
        observed["request"] = json.loads((control / "EDIT_REQUEST.json").read_text("utf-8"))
        observed["index"] = json.loads((control / "evidence" / "index.json").read_text("utf-8"))
        observed["first"] = (control / "evidence" / "000000.txt").read_text("utf-8")
        observed["files"] = sorted(
            path.relative_to(work).as_posix() for path in work.rglob("*") if path.is_file()
        )
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root, brief=brief))

    assert result.status == "edited"
    document = observed["request"]
    assert set(document) == {
        "version",
        "attempt_id",
        "round",
        "brief",
        "opening",
        "editable_paths",
        "limits",
    }
    assert document["version"] == REQUEST_VERSION == "edit_request/1"
    assert document["attempt_id"] == "a1"
    assert document["round"] == 1
    assert document["brief"] == brief_payload(brief)
    assert document["editable_paths"] == ["skills/**"]
    assert set(document["limits"]) == {
        "max_changed_files",
        "max_written_bytes",
        "max_payload_bytes",
        "max_turns",
        "deadline_seconds",
    }
    assert document["limits"]["max_payload_bytes"] == 1024 * 1024
    assert document["limits"]["deadline_seconds"] == 30.0
    assert document["limits"]["max_turns"] is None
    assert "readable_paths" not in json.dumps(document)

    catalog = render_catalog(brief)
    assert document["opening"] == render_opening(brief, catalog, ("skills/**",))
    assert observed["index"] == [
        {"id": record.evidence_id, "kind": record.kind, "path": f"evidence/{index:06d}.txt"}
        for index, record in enumerate(catalog)
    ]
    assert observed["index"][0]["kind"] == "summary"
    assert observed["first"] == catalog[0].content
    assert observed["files"] == [
        f"{CONTROL_PREFIX}/EDIT_REQUEST.json",
        f"{CONTROL_PREFIX}/evidence/000000.txt",
        f"{CONTROL_PREFIX}/evidence/000001.txt",
        f"{CONTROL_PREFIX}/evidence/000002.txt",
        f"{CONTROL_PREFIX}/evidence/000003.txt",
        f"{CONTROL_PREFIX}/evidence/index.json",
        "README.md",
        "skills/SKILL.md",
        "skills/config.yaml",
    ]

    backend = instances[0]
    assert backend.opening == document["opening"]
    assert backend.system == TREE_AGENT_EDITOR_SYSTEM
    assert backend.work_dir is not None and root not in backend.work_dir.parents
    # The control plane never reaches the real Candidate.
    assert not (root / CONTROL_PREFIX).exists()
    assert (root / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"


def _brief_v2() -> EditBrief:
    from evolution.tree.roles.editor_contracts import (
        AttemptChanges,
        AttemptResult,
        BriefHistory,
        HistoryAttempt,
        HistoryRound,
        Observation,
        ObservationInput,
        ObservationTurn,
    )

    observation = Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "ok",
                ObservationInput("present", "hello", False),
                ({"type": "assistant_message", "content": "TARGET-SAID-THIS", "truncated": False},),
                0,
                False,
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        "a" * 64,
        "edited",
        None,
        "rejected",
        "upper_bound_nonpositive",
        None,
        3,
        "c" * 64,
        AttemptChanges("present", ("skills/SKILL.md",), False),
        AttemptResult("absent"),
    )
    history = BriefHistory(1, (1,), 0, (HistoryRound(1, "stalled", None, (attempt,), False),))
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=(
            EditBriefCase(
                case_id="c1",
                group_id="g1",
                stratum="s1",
                tags=("t",),
                public={"text": "hello"},
                repeats=(EditBriefRepeat(1, "c1-r1", "rollout_finished", 0.5, True, observation),),
            ),
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
        history=history,
    )


async def test_v2_control_plane_evidence_index_lists_observation_and_attempt_records(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    brief = _brief_v2()
    observed: dict[str, Any] = {}

    def edit(work: Path) -> None:
        control = work / CONTROL_PREFIX
        observed["index"] = json.loads((control / "evidence" / "index.json").read_text("utf-8"))
        observed["fifth"] = (control / "evidence" / "000004.txt").read_text("utf-8")
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root, brief=brief))

    assert result.status == "edited"
    assert [record["kind"] for record in observed["index"]] == [
        "summary",
        "diagnosis",
        "case",
        "observation",
        "attempt",
    ]
    attempt = json.loads(observed["fifth"])
    assert attempt["attempt_id"] == "r01-a01"


async def test_untouched_control_plane_is_no_change_and_cli_write_under_it_is_refused(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)

    editor, _instances = _editor(tmp_path, [lambda work: None])
    declined = await editor.edit(_request(root))

    assert declined.status == "declined"
    assert _snapshot(root) == before

    def edit(work: Path) -> None:
        _write(work / CONTROL_PREFIX / "notes.txt", "actor was here\n")

    refusing, _instances = _editor(tmp_path, [edit])
    result = await refusing.edit(_request(root, attempt_id="a2"))

    assert result.status == "failed"
    assert result.note.startswith("actor_violation")
    assert "control plane" in result.note
    assert _snapshot(root) == before


async def test_control_collision_on_seed_with_dot_evolution(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    (root / CONTROL_PREFIX).mkdir()
    _write(root / CONTROL_PREFIX / "seeded.txt", "seed\n")
    before = _snapshot(root)

    editor, instances = _editor(tmp_path, [lambda work: None])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("control_collision")
    assert instances == []
    assert _snapshot(root) == before


# --------------------------------------------------------------------------
# Edited, declined
# --------------------------------------------------------------------------


async def test_a_fifo_at_the_attempt_log_path_fails_the_attempt_without_blocking(
    tmp_path: Path,
) -> None:
    """The attempt log is opened first, so its refusal is the whole attempt's.

    A FIFO at the predicted pathname used to block the open on the event-loop
    thread; now it is `failed/workspace_changed`, the CLI is never built and
    the Candidate is never touched.
    """

    root = _candidate(tmp_path)
    before = _snapshot(root)
    log_root = tmp_path / "run" / "editor-logs"
    log_root.mkdir(parents=True)
    log_root.chmod(0o700)
    os.mkfifo(log_root / "a1.jsonl")
    editor, instances = _editor(tmp_path, [lambda work: _write(work / "skills" / "x.md", "x\n")])

    started = time.monotonic()
    result = await editor.edit(_request(root))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the open must not wait for a reader"
    assert result.status == "failed"
    assert result.note.startswith("workspace_changed ")
    assert instances == [], "the CLI is never built"
    assert _snapshot(root) == before


async def test_edited_publishes_and_declined_on_no_change(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    (root / "skills" / "SKILL.md").chmod(0o644)

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        _write(work / "skills" / "extra.md", "new\n")

    editor, _instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root))

    assert result.status == "edited"
    assert result.note == "done"
    assert (root / "skills" / "SKILL.md").read_text(encoding="utf-8") == "second\n"
    assert (root / "skills" / "extra.md").read_text(encoding="utf-8") == "new\n"
    # A modified file keeps the Candidate's own mode; an untouched one is not a change.
    assert stat.S_IMODE((root / "skills" / "SKILL.md").lstat().st_mode) == 0o644
    assert (root / "README.md").read_text(encoding="utf-8") == "readme\n"

    silent, _instances = _editor(tmp_path, [lambda work: None], content="", diag={})
    declined = await silent.edit(_request(root, attempt_id="a2"))

    assert declined.status == "declined"
    assert declined.note == "no change proposed"


async def test_declined_note_is_the_cli_last_message(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    editor, _instances = _editor(
        tmp_path, [lambda work: None], content="", diag={"last_message": "nothing to do"}
    )

    result = await editor.edit(_request(root))

    assert result.status == "declined"
    assert result.note == "nothing to do"


# --------------------------------------------------------------------------
# Admission guardrails (the shared module's authority, reached through the CLI)
# --------------------------------------------------------------------------


def _violation(kind: str) -> Any:
    def edit(work: Path) -> None:
        if kind == "mode":
            _write(work / "skills" / "SKILL.md", "second\n")
            os.chmod(work / "skills" / "SKILL.md", 0o644)
        elif kind == "outside":
            _write(work / "README.md", "rewritten\n")
        elif kind == "unsafe_new_mode":
            _write(work / "skills" / "tool.md", "new\n")
            os.chmod(work / "skills" / "tool.md", 0o755)
        elif kind == "payload":
            _write(work / "skills" / "SKILL.md", "x" * 64)
        elif kind == "written":
            _write(work / "skills" / "SKILL.md", "x" * 64)
        elif kind == "count":
            _write(work / "skills" / "SKILL.md", "second\n")
            _write(work / "skills" / "extra.md", "new\n")
        elif kind == "kind_flip":
            (work / "skills" / "SKILL.md").unlink()
            (work / "skills" / "SKILL.md").mkdir()
        elif kind == "not_utf8":
            (work / "skills" / "SKILL.md").write_bytes(b"\xff\xfe\x00")
        elif kind == "unused_directory":
            (work / "skills" / "empty").mkdir()
        else:  # pragma: no cover - a typo in the parametrisation
            raise AssertionError(kind)

    return edit


_BUDGETS = {
    "payload": Budgets(max_changed_files=None, max_written_bytes=1 << 20, max_payload_bytes=8),
    "written": Budgets(max_changed_files=None, max_written_bytes=8, max_payload_bytes=1 << 20),
    "count": Budgets(max_changed_files=1, max_written_bytes=1 << 20, max_payload_bytes=1 << 20),
}


_CODES = {"written": "written_byte_budget", "count": "changed_file_budget"}


@pytest.mark.parametrize(
    "violation",
    [
        "mode",
        "outside",
        "unsafe_new_mode",
        "payload",
        "written",
        "count",
        "kind_flip",
        "not_utf8",
        "unused_directory",
    ],
)
async def test_admission_guardrails(tmp_path: Path, violation: str) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    section_kw: dict[str, Any] = {}
    if violation in _BUDGETS:
        section_kw["budgets"] = _BUDGETS[violation]

    editor, instances = _editor(tmp_path, [_violation(violation)], **section_kw)
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("actor_violation")
    assert result.code == _CODES.get(violation, "actor_violation")
    # D4: a changed-file breach spends the repair call, which reran the same edit.
    assert len(instances) == (2 if violation == "count" else 1)
    assert _snapshot(root) == before
    assert [line["error_class"] for line in _log_lines(editor) if line["phase"] == "refused"] == [
        "actor_violation"
    ]


# --------------------------------------------------------------------------
# The self-check repair, and the acceptance limit
# --------------------------------------------------------------------------


async def test_self_check_repair_is_the_second_and_last_call(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    seen: list[Any] = []

    def broken(work: Path) -> None:
        seen.append(
            json.loads((work / CONTROL_PREFIX / "VALIDATION.json").read_text("utf-8"))
            if (work / CONTROL_PREFIX / "VALIDATION.json").exists()
            else None
        )
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")

    editor, instances = _editor(tmp_path, [broken, broken])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("self_check_failed")
    assert result.code == "self_check_failed"
    assert "skills/config.yaml" in result.note
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert seen[0] is None
    assert isinstance(seen[1], dict) and seen[1]["issues"]
    assert "skills/config.yaml" in seen[1]["issues"][0]
    assert _snapshot(root) == before

    def repaired(work: Path) -> None:
        _write(work / "skills" / "config.yaml", "key: repaired\n")

    second, instances = _editor(tmp_path, [broken, repaired])
    good = await second.edit(_request(root, attempt_id="a2"))

    assert good.status == "edited"
    assert len(instances) == 2
    assert (root / "skills" / "config.yaml").read_text(encoding="utf-8") == "key: repaired\n"


async def test_max_turns_is_an_acceptance_limit_summed_over_repair(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)

    def broken(work: Path) -> None:
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")

    def repaired(work: Path) -> None:
        _write(work / "skills" / "config.yaml", "key: repaired\n")

    editor, instances = _editor(
        tmp_path,
        [broken, repaired],
        usage=[{"tool_turns": 3}, {"tool_turns": 2}],
        max_turns=4,
    )
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("budget")
    assert result.code == "budget"
    assert len(instances) == 2
    assert _snapshot(root) == before

    under, _instances = _editor(
        tmp_path,
        [broken, repaired],
        usage=[{"tool_turns": 3}, {"tool_turns": 1}],
        max_turns=4,
    )
    accepted = await under.edit(_request(root, attempt_id="a2"))

    assert accepted.status == "edited"


async def test_a_call_without_turn_telemetry_counts_as_one_turn(tmp_path: Path) -> None:
    root = _candidate(tmp_path)

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, _instances = _editor(tmp_path, [edit], usage=[{"tool_turns": None}], max_turns=1)
    result = await editor.edit(_request(root))

    assert result.status == "edited"


# --------------------------------------------------------------------------
# The backend seam
# --------------------------------------------------------------------------


async def test_backend_per_attempt_and_explicit_env(tmp_path: Path) -> None:
    first = _candidate(tmp_path, "one")
    second = _candidate(tmp_path, "two")

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, instances = _editor(tmp_path, [edit])

    results = await asyncio.gather(
        editor.edit(_request(first, attempt_id="a1")),
        editor.edit(_request(second, attempt_id="a2")),
    )

    assert [result.status for result in results] == ["edited", "edited"]
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    program = str(_program(tmp_path))
    for backend in instances:
        assert backend.kwargs["env"] == CHILD_ENV
        assert backend.kwargs["env"] is not editor.child_env
        assert backend.kwargs["explicit"] is True
        assert backend.kwargs["agent_bin"] == program
        assert backend.kwargs["model"] == "codex"
        assert backend.kwargs["agent_permission"] == "safe"
        assert backend.kwargs["sandbox"] is None
        assert backend.kwargs["reasoning_effort"] is None
        assert 0 < backend.kwargs["timeout_s"] <= 30.0
        assert backend.timeout_s == backend.kwargs["timeout_s"]
    assert instances[0].work_dir != instances[1].work_dir


async def test_permission_and_reasoning_effort_reach_the_backend(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    editor, instances = _editor(
        tmp_path, [lambda work: None], permission="unsafe", reasoning_effort="high"
    )

    await editor.edit(_request(root))

    assert instances[0].kwargs["agent_permission"] == "unsafe"
    assert instances[0].kwargs["reasoning_effort"] == "high"


async def test_backend_exception_is_agent_error_and_candidate_untouched(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        raise RuntimeError("codex exited rc=1")

    editor, _instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("agent_error")
    assert result.code == "agent_error"
    assert "codex exited rc=1" in result.note
    assert _snapshot(root) == before


async def test_cancellation_discards_and_reraises(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    seen: dict[str, Path] = {}

    def edit(work: Path) -> None:
        seen["work"] = work
        _write(work / "skills" / "SKILL.md", "second\n")
        raise asyncio.CancelledError()

    editor, _instances = _editor(tmp_path, [edit])
    with pytest.raises(asyncio.CancelledError):
        await editor.edit(_request(root))

    assert _snapshot(root) == before
    assert not seen["work"].exists()


async def test_an_unsupported_entry_in_the_projection_is_an_actor_violation(
    tmp_path: Path,
) -> None:
    """Ruling R13: the projection is the actor's output, so its walk refusals are its class."""

    root = _candidate(tmp_path)
    before = _snapshot(root)

    def edit(work: Path) -> None:
        (work / "skills" / "link.md").symlink_to(work / "skills" / "SKILL.md")

    editor, _instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("actor_violation")
    assert "unsupported entry" in result.note
    assert _snapshot(root) == before
    assert [line["error_class"] for line in _log_lines(editor) if line["phase"] == "refused"] == [
        "actor_violation"
    ]


async def test_a_backend_that_cannot_be_constructed_is_an_exception_class(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)

    def refusing_factory(**kwargs: Any) -> Any:
        raise ValueError("agent_permission must be 'safe' or 'unsafe'")

    editor, _instances = _editor(tmp_path, [lambda work: None])
    editor = AgentCliEditor(
        section=editor.section,
        program=editor.program,
        prompt=editor.prompt,
        child_env=editor.child_env,
        log_root=editor.log_root,
        secrets=editor.secrets,
        backend_factory=refusing_factory,
    )
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "exception:ValueError"
    assert _snapshot(root) == before
    assert [line["phase"] for line in _log_lines(editor)][-1] == "exception"


# --------------------------------------------------------------------------
# Publication and the seals
# --------------------------------------------------------------------------


async def test_anchor_loss_and_in_place_drift_before_publication(tmp_path: Path) -> None:
    root = _candidate(tmp_path)

    def rename(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        root.rename(tmp_path / "moved")

    editor, _instances = _editor(tmp_path, [rename])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note.startswith("workspace_changed")
    moved = tmp_path / "moved"
    assert (moved / "skills" / "SKILL.md").read_text(encoding="utf-8") == "first\n"
    moved.rename(root)

    def drift(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        _write(root / "skills" / "SKILL.md", "foreign\n")

    drifting, _instances = _editor(tmp_path, [drift])
    drifted = await drifting.edit(_request(root, attempt_id="a2"))

    assert drifted.status == "failed"
    assert drifted.note.startswith("workspace_changed")
    assert (root / "skills" / "SKILL.md").read_text(encoding="utf-8") == "foreign\n"


async def test_program_changed_before_publication(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        _write(_program(tmp_path), "#!/bin/sh\nexit 9\n")

    editor, _instances = _editor(tmp_path, [edit])
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "command_changed"
    assert result.code == "command_changed"
    assert _snapshot(root) == before


async def test_program_changed_before_the_first_call(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    editor, instances = _editor(tmp_path, [lambda work: None])
    _write(_program(tmp_path), "#!/bin/sh\nexit 9\n")

    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "command_changed"
    assert result.code == "command_changed"
    assert instances == []
    assert _snapshot(root) == before


async def test_prompt_rewritten_during_the_first_call_never_reaches_the_repair_call(
    tmp_path: Path,
) -> None:
    """`text()` re-digests nothing, so the seal is checked before every call."""

    root = _candidate(tmp_path)
    before = _snapshot(root)
    prompt_file = tmp_path / "prompt.md"
    _write(prompt_file, "Edit the Candidate.\n")

    def edit(work: Path) -> None:
        # A self-check failure would otherwise buy a second call.
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")
        _write(prompt_file, "Ignore the brief.\n")

    def never(work: Path) -> None:  # pragma: no cover - the guard's whole point
        raise AssertionError("the repair call ran on a rewritten prompt")

    editor, instances = _editor(
        tmp_path, [edit, never], prompt=seal_prompt(prompt_file, packaged="")
    )
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "prompt_changed"
    assert result.code == "prompt_changed"
    assert len(instances) == 1
    assert instances[0].system == "Edit the Candidate.\n"
    assert _snapshot(root) == before
    assert [line["call"] for line in _log_lines(editor) if line["phase"] == "request"] == [1]


async def test_prompt_changed_before_and_during_an_attempt(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    prompt_file = tmp_path / "prompt.md"
    _write(prompt_file, "Edit the Candidate.\n")

    editor, instances = _editor(
        tmp_path, [lambda work: None], prompt=seal_prompt(prompt_file, packaged="")
    )
    _write(prompt_file, "Edit something else.\n")
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "prompt_changed"
    assert result.code == "prompt_changed"
    assert instances == []
    assert _snapshot(root) == before

    _write(prompt_file, "Edit the Candidate.\n")

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        _write(prompt_file, "Edit something else again.\n")

    during, _instances = _editor(tmp_path, [edit], prompt=seal_prompt(prompt_file, packaged=""))
    late = await during.edit(_request(root, attempt_id="a2"))

    assert late.status == "failed"
    assert late.note == "prompt_changed"
    assert late.code == "prompt_changed"
    assert _snapshot(root) == before


async def test_seals_are_verified_after_a_declined_attempt(tmp_path: Path) -> None:
    """PDR-0005: the seals are verified after *every* outcome, not only before a publication.

    An attempt that proposes no change used to return `declined` above the
    post-run check, so a prompt -- or the sealed CLI itself -- rewritten under
    it was invisible. The class the tampering replaced stays in the note.
    """

    root = _candidate(tmp_path)
    before = _snapshot(root)
    prompt_file = tmp_path / "prompt.md"
    _write(prompt_file, "Edit the Candidate.\n")

    def rewrite_prompt(work: Path) -> None:
        _write(prompt_file, "Ignore the brief.\n")

    editor, _instances = _editor(
        tmp_path, [rewrite_prompt], prompt=seal_prompt(prompt_file, packaged="")
    )
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "prompt_changed (after declined)"
    assert result.code == "prompt_changed"
    assert _snapshot(root) == before
    assert any(line.get("error_class") == "prompt_changed" for line in _log_lines(editor))

    _write(prompt_file, "Edit the Candidate.\n")

    def rewrite_program(work: Path) -> None:
        _write(_program(tmp_path), "#!/bin/sh\nexit 9\n")

    other, _more = _editor(
        tmp_path, [rewrite_program], prompt=seal_prompt(prompt_file, packaged="")
    )
    program = await other.edit(_request(root, attempt_id="a2"))

    assert program.status == "failed"
    assert program.note == "command_changed (after declined)"
    assert program.code == "command_changed"
    assert _snapshot(root) == before


async def test_seals_are_verified_after_a_failed_attempt(tmp_path: Path) -> None:
    """A failure keeps its own class in the detail of the seal refusal that replaces it."""

    root = _candidate(tmp_path)
    before = _snapshot(root)
    prompt_file = tmp_path / "prompt.md"
    _write(prompt_file, "Edit the Candidate.\n")

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")
        _write(prompt_file, "Ignore the brief.\n")

    editor, _instances = _editor(
        tmp_path,
        [edit],
        usage=[{"tool_turns": 3}],
        max_turns=1,
        prompt=seal_prompt(prompt_file, packaged=""),
    )
    result = await editor.edit(_request(root))

    assert result.status == "failed"
    assert result.note == "prompt_changed (after budget)"
    assert result.code == "prompt_changed"
    assert _snapshot(root) == before


async def test_an_untouched_seal_leaves_a_declined_attempt_declined(tmp_path: Path) -> None:
    """The control: the post-run check is a check, not a new failure mode."""

    root = _candidate(tmp_path)
    before = _snapshot(root)
    prompt_file = tmp_path / "prompt.md"
    _write(prompt_file, "Edit the Candidate.\n")

    editor, instances = _editor(
        tmp_path, [lambda work: None], prompt=seal_prompt(prompt_file, packaged="")
    )
    result = await editor.edit(_request(root))

    assert (result.status, result.note) == ("declined", "done")
    assert len(instances) == 1
    assert _snapshot(root) == before


# --------------------------------------------------------------------------
# The transcript
# --------------------------------------------------------------------------


async def test_transcript_redacts_a_curated_credential_echoed_in_diagnostics(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, _instances = _editor(
        tmp_path,
        [edit],
        diag={"stderr_tail": f"OPENAI_API_KEY={SECRET}", "last_message": "done"},
    )
    result = await editor.edit(_request(root))

    assert result.status == "edited"
    text = _log_text(editor)
    assert SECRET not in text
    assert "[REDACTED]" in text
    lines = _log_lines(editor)
    request_line = next(line for line in lines if line["phase"] == "request")
    response_line = next(line for line in lines if line["phase"] == "response")
    assert request_line["opening"].startswith("context")
    assert request_line["system_sha256"] == editor.prompt.sha256
    assert response_line["content"] == "done"
    assert response_line["usage"] == {"tool_turns": 1}
    assert response_line["diagnostics"]["stderr_tail"] == "OPENAI_API_KEY=[REDACTED]"


async def test_transcript_holds_neither_the_program_path_nor_a_child_env_value(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    program = str(_program(tmp_path))

    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")

    editor, _instances = _editor(
        tmp_path,
        [edit],
        diag={"command": [program, "-C", "/work"], "last_message": "done"},
    )
    await editor.edit(_request(root))

    text = _log_text(editor)
    assert program not in text
    for value in CHILD_ENV.values():
        assert value not in text


# --------------------------------------------------------------------------
# A changed-file budget breach spends the repair call (ADR-0037 D4, D4a)
# --------------------------------------------------------------------------


_THREE = Budgets(max_changed_files=3, max_written_bytes=1 << 20, max_payload_bytes=1 << 20)


def _four(work: Path) -> None:
    for index in range(4):
        _write(work / "skills" / f"new{index}.md", "new\n")


def _validation(work: Path) -> Any:
    path = work / CONTROL_PREFIX / "VALIDATION.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


async def test_a_budget_breach_spends_the_repair_call_and_a_compliant_repair_publishes(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    seen: list[Any] = []

    def repair(work: Path) -> None:
        seen.append(_validation(work))
        (work / "skills" / "new3.md").unlink()

    editor, instances = _editor(tmp_path, [_four, repair], budgets=_THREE)
    result = await editor.edit(_request(root))

    assert result.status == "edited"
    assert len(instances) == 2
    (validation,) = seen
    assert validation["changed_paths"] == [f"skills/new{i}.md" for i in range(4)]
    assert validation["issues"][0] == (
        "changed-file budget exceeded: 4 files changed, limit 3; "
        "remove files you created or write updated files' original bytes back "
        "until at most 3 differ; a deleted file cannot be restored this way"
    )
    assert sorted(p.name for p in (root / "skills").glob("new*.md")) == [
        "new0.md",
        "new1.md",
        "new2.md",
    ]


async def test_a_repair_that_ignores_the_budget_fails_with_its_code(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    before = _snapshot(root)
    editor, instances = _editor(tmp_path, [_four, lambda work: None], budgets=_THREE)
    result = await editor.edit(_request(root))

    assert (result.status, result.code) == ("failed", "changed_file_budget")
    assert result.note == "actor_violation changed-file budget exceeded"
    assert len(instances) == 2
    assert _snapshot(root) == before


async def test_one_repair_message_carries_the_budget_and_the_self_check(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    seen: list[Any] = []

    def first(work: Path) -> None:
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")
        for index in range(3):
            _write(work / "skills" / f"new{index}.md", "new\n")

    def repair(work: Path) -> None:
        seen.append(_validation(work))
        _write(work / "skills" / "config.yaml", "key: value\n")  # restored: frees its slot too

    editor, instances = _editor(tmp_path, [first, repair], budgets=_THREE)
    result = await editor.edit(_request(root))

    assert result.status == "edited" and len(instances) == 2
    (validation,) = seen
    assert validation["issues"][0].startswith(
        "changed-file budget exceeded: 4 files changed, limit 3"
    )
    assert any("skills/config.yaml" in issue for issue in validation["issues"][1:])


async def test_a_breach_on_the_continuation_spends_the_unused_repair(tmp_path: Path) -> None:
    root = _candidate(tmp_path)

    def repair(work: Path) -> None:
        (work / "skills" / "new3.md").unlink()

    editor, instances = _editor(tmp_path, [lambda work: None, _four, repair], budgets=_THREE)
    editor = dataclasses.replace(editor, role_resilience=True)
    result = await editor.edit(_request(root))

    assert (result.status, result.continued) == ("edited", True)
    assert len(instances) == 3


async def test_a_continuation_breach_report_also_carries_the_self_check(tmp_path: Path) -> None:
    """D4a holds on the continuation path too: one report, budget first."""

    root = _candidate(tmp_path)
    seen: list[Any] = []

    def broken_four(work: Path) -> None:
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")
        for index in range(3):
            _write(work / "skills" / f"new{index}.md", "new\n")

    def repair(work: Path) -> None:
        seen.append(_validation(work))
        _write(work / "skills" / "config.yaml", "key: value\n")

    editor, instances = _editor(tmp_path, [lambda work: None, broken_four, repair], budgets=_THREE)
    editor = dataclasses.replace(editor, role_resilience=True)
    result = await editor.edit(_request(root))

    assert result.status == "edited" and len(instances) == 3
    (validation,) = seen
    assert validation["issues"][0].startswith(
        "changed-file budget exceeded: 4 files changed, limit 3"
    )
    assert any("skills/config.yaml" in issue for issue in validation["issues"][1:])


async def test_an_attempt_never_makes_more_than_three_calls(tmp_path: Path) -> None:
    root = _candidate(tmp_path)

    def empty(work: Path) -> None:
        for index in range(4):
            (work / "skills" / f"new{index}.md").unlink(missing_ok=True)

    editor, instances = _editor(tmp_path, [_four, empty, _four, _four], budgets=_THREE)
    editor = dataclasses.replace(editor, role_resilience=True)
    result = await editor.edit(_request(root))

    assert (result.status, result.code) == ("failed", "changed_file_budget")
    assert len(instances) == 3


async def test_the_self_check_repair_document_is_unchanged_without_a_breach(
    tmp_path: Path,
) -> None:
    root = _candidate(tmp_path)
    seen: list[Any] = []

    def broken(work: Path) -> None:
        seen.append(_validation(work))
        _write(work / "skills" / "config.yaml", "key: [unclosed\n")

    editor, _instances = _editor(tmp_path, [broken, broken], budgets=_THREE)
    await editor.edit(_request(root))
    assert set(seen[1]) == {"issues"}


async def test_an_unreadable_projection_in_the_breach_report_fails_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `WorkspaceError` from the repair report's live count propagates.

    The same choice as the chat Editor's budget checks: it is neither a budget
    pass nor a repair, the attempt ends `failed` with the generic `exception`
    code, no repair call is made and the Candidate is untouched."""

    from evolution.tree.execution.private_files import WorkspaceError
    from evolution.tree.roles.editor import agent_editor

    def unreadable(projection: Any) -> tuple[str, ...]:
        raise WorkspaceError("Candidate workspace holds an unsupported entry: skills/x")

    monkeypatch.setattr(agent_editor, "changed_file_paths", unreadable)
    root = _candidate(tmp_path)
    before = _snapshot(root)
    editor, instances = _editor(tmp_path, [_four], budgets=_THREE)
    result = await editor.edit(_request(root))

    assert (result.status, result.code) == ("failed", "exception")
    assert len(instances) == 1
    assert _snapshot(root) == before


async def test_the_agent_opening_states_the_budget_and_the_wider_validation_report(tmp_path):
    def edit(work: Path) -> None:
        _write(work / "skills" / "SKILL.md", "second\n")

    root = _candidate(tmp_path)
    editor, instances = _editor(tmp_path, [edit], budgets=_THREE)
    assert (await editor.edit(_request(root))).status == "edited"
    opening = instances[0].opening
    assert "Budget: change at most 3 files in this Attempt" in opening
    assert opening.endswith(AGENT_VALIDATION_LINE)
    assert AGENT_VALIDATION_LINE == (
        "VALIDATION.json may also report the file budget: an issue that says you changed "
        "more files than this budget allows, with the changed files in `changed_paths`; "
        "remove files you created or write updated files' original bytes back until the count "
        "fits; a deleted file cannot be restored this way."
    )

    plain_root = _candidate(tmp_path, "plain")
    plain, plain_instances = _editor(tmp_path, [edit], log_root=tmp_path / "plain-logs")
    assert (await plain.edit(_request(plain_root))).status == "edited"
    assert "Budget:" not in plain_instances[0].opening
    assert AGENT_VALIDATION_LINE not in plain_instances[0].opening


async def test_agent_cli_editor_trace_files_match_the_chat_editor_pages(tmp_path):
    from dataclasses import replace

    from evolution.tree.roles.editor_contracts import DiagnosisGroup
    from tests.test_tree_baseline_traces import reader
    from tests.tree.roles.editor.test_editor_evidence import _Trace

    trace = _Trace(tmp_path / "baseline-trace", turns=1)
    try:
        traces = reader(trace)
        diagnosis = replace(
            _DEFAULT_DIAGNOSIS,
            groups=(
                DiagnosisGroup(
                    "h1", "cause", "fix", ("obs:c:1", "obs:c:1"), ("c",), ("skills/SKILL.md",), 1
                ),
            ),
        )
        brief = replace(_brief(), diagnosis=diagnosis)
        observed = []

        def edit(root):
            directory = root / ".evolution" / "traces"
            index = json.loads((directory / "index.json").read_text())
            assert index == [
                {
                    "evidence_id": "obs:c:1",
                    "path": "traces/000000.json",
                    "total_characters": len(traces.whole("obs:c:1")),
                }
            ]
            assert (directory / "000000.json").read_text() == traces.whole("obs:c:1")
            observed.append(True)

        editor, _ = _editor(tmp_path, [edit])
        result = await editor.edit(
            replace(_request(_candidate(tmp_path), brief=brief), traces=traces)
        )
        assert result.status == "declined", result.note
        assert observed == [True]
    finally:
        trace.close()


@pytest.mark.parametrize("include_available", [False, True])
async def test_agent_cli_editor_skips_unavailable_cited_traces(tmp_path, include_available):
    from dataclasses import replace

    from evolution.tree.roles.editor_contracts import DiagnosisGroup
    from tests.test_tree_baseline_traces import reader
    from tests.tree.roles.editor.test_editor_evidence import _Trace

    trace = _Trace(tmp_path / "baseline-trace", turns=1, samples=2)
    try:
        records = tuple(
            row
            for row in trace.journal.snapshot().records
            if not (
                row.event in {"scoring_started", "scoring_finished"}
                and row.payload.get("sample_id") == "s1"
            )
        )
        traces = reader(trace, measurement_records=records)
        ids = ("obs:c:1", "obs:c:2") if include_available else ("obs:c:1",)
        diagnosis = replace(
            _DEFAULT_DIAGNOSIS,
            groups=(DiagnosisGroup("h1", "cause", "fix", ids, ("c",), ("skills/SKILL.md",), 1),),
        )
        observed = []

        def edit(root):
            directory = root / ".evolution/traces"
            index = json.loads((directory / "index.json").read_text())
            assert [row["evidence_id"] for row in index] == (
                ["obs:c:2"] if include_available else []
            )
            assert len(list(directory.glob("*.json"))) == 1 + len(index)
            for row in index:
                assert (root / ".evolution" / row["path"]).read_text() == traces.whole("obs:c:2")
            observed.append(True)

        editor, _ = _editor(tmp_path, [edit])
        result = await editor.edit(
            replace(
                _request(_candidate(tmp_path), brief=replace(_brief(), diagnosis=diagnosis)),
                traces=traces,
            )
        )
        assert result.status == "declined", result.note
        assert observed == [True]
    finally:
        trace.close()
