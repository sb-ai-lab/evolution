"""`SubprocessEditor`, the first v2 `CandidateEditor`.

Every case runs a real declared program: a fixture script written under
`tmp_path/editor/main.py` and bound by a `CommandBinding`, exactly as
`build_editor` binds the manifest's `editor` section. The scripts cover the
whole result contract — `edited`, `declined`, and each of the four failure
notes — plus the channel guarantees the spec makes about what core hands a
declared program: stderr never reaches the note, only `allowed_env` names
travel, and the `edit_request/1` document carries exactly seven keys with the
redacted brief payload and nothing else.

The termination cases use marker files rather than a private attribute on the
editor: the script writes its own pid (and, for the fork case, its child's) to
a path passed in `argv`, so the test can assert on the real process table after
the cancellation returns. A killed process that has not yet been reaped is a
zombie, which `os.kill(pid, 0)` still finds, so `_gone` reads `/proc` to tell
"dead but unreaped" from "alive".
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from evolution.tree.campaign_editor import EditorOutcome, run_editor
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.roles.editor.subprocess_editor import (
    KILL_GRACE_SECONDS,
    MAX_NOTE_BYTES,
    REQUEST_VERSION,
    SubprocessEditor,
)
from evolution.tree.roles.editor_contracts import (
    Diagnosis,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    EditRequest,
    EditResult,
    brief_payload,
)

pytestmark = pytest.mark.slow

# A neutral diagnosis for tests not themselves about the diagnosis field.
_DEFAULT_DIAGNOSIS = Diagnosis("absent", None, None, (), None)

EDITED = (
    "import json,sys; r=json.load(sys.stdin);"
    ' open(r["workspace"]+"/skills/SKILL.md","a").write("edited\\n");'
    ' print(json.dumps({"status":"edited","note":"ok"}))'
)
DECLINED = 'import json; print(json.dumps({"status":"declined","note":"nothing to do"}))'
EXIT_STATUS = "import sys; sys.exit(3)"
NO_RESULT = "pass"
BAD_RESULT = 'print("not json")'
TOO_LARGE = (
    'import sys; sys.stdout.write("x"*(1<<20+1)); print();'
    ' import json; print(json.dumps({"status":"edited","note":""}))'
)
STDERR = (
    'import sys,json; print("secret-in-stderr", file=sys.stderr);'
    ' print(json.dumps({"status":"failed","note":"n"}))'
)
ENV = (
    "import os,json;"
    ' print(json.dumps({"status":"declined",'
    ' "note":os.environ.get("W1_ALLOWED","")+"|"+os.environ.get("W1_OTHER","")}))'
)
ECHO_REQUEST = (
    "import json,sys;"
    " r=json.load(sys.stdin);"
    ' open(r["workspace"]+"/request.json","w").write(json.dumps(r));'
    ' print(json.dumps({"status":"edited","note":"ok"}))'
)
REWRITES_SELF = (
    "import json,pathlib;"
    ' pathlib.Path(__file__).write_text("# replaced\\n");'
    ' print(json.dumps({"status":"edited","note":"ok"}))'
)
WRITES_BEFORE_READING = (
    "import json,sys\n"
    "sys.stdout.write('x'*(512*1024))\n"
    "sys.stdout.flush()\n"
    "r=json.load(sys.stdin)\n"
    "print()\n"
    "print(json.dumps({'status':'declined','note':'cases:%d' % len(r['brief']['cases'])}))\n"
)
SLEEPS = "import time; time.sleep(60)"
IGNORES_TERM = (
    "import os,signal,sys,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1],'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)
FORKS = (
    "import os,sys,time\n"
    "pid=os.fork()\n"
    "if pid==0:\n"
    "    open(sys.argv[2],'w').write(str(os.getpid()))\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "open(sys.argv[1],'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)


# -- fixtures -------------------------------------------------------------------


def _script(root: Path, body: str) -> Path:
    editor_dir = root / "editor"
    editor_dir.mkdir(parents=True, exist_ok=True)
    script = editor_dir / "main.py"
    script.write_text(body + "\n", encoding="utf-8")
    return script


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "skills" / "SKILL.md").write_text("seed\n", encoding="utf-8")
    return workspace


def _brief() -> EditBrief:
    case = EditBriefCase(
        case_id="c1",
        group_id="g1",
        stratum="s1",
        tags=("t",),
        public={"text": "hello"},
        repeats=(EditBriefRepeat(1, "s1-r1", "rollout_finished", 0.5, True),),
    )
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=(case,),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )


def _large_brief(cases: int = 400) -> EditBrief:
    """A brief the size a full corpus makes: far more than one pipe buffer."""

    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=tuple(
            EditBriefCase(
                case_id=f"case-{index:04d}",
                group_id=f"g{index % 7}",
                stratum="s1",
                tags=("t",),
                public={"text": f"{index:04d}-" + "payload " * 64},
                repeats=(
                    EditBriefRepeat(1, f"case-{index:04d}-r1", "rollout_finished", 0.5, True),
                ),
            )
            for index in range(cases)
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )


def _bind(
    tmp_path: Path,
    body: str,
    *,
    allowed: tuple[str, ...] = (),
    attempt: str = "a1",
    args: tuple[str, ...] = (),
    deadline: float = 30.0,
    root: Path | None = None,
    brief: EditBrief | None = None,
) -> tuple[SubprocessEditor, EditRequest]:
    script = _script(root if root is not None else tmp_path, body)
    binding = CommandBinding(
        argv=(sys.executable, script.name, *args),
        cwd=script.parent,
        allowed_env=frozenset(allowed),
    )
    editor = SubprocessEditor(binding=binding, log_root=tmp_path / "run" / "editor-logs")
    request = EditRequest(
        attempt_id=attempt,
        round=1,
        workspace=_workspace(tmp_path),
        writable_paths=("skills/**",),
        brief=_brief() if brief is None else brief,
        deadline_seconds=deadline,
    )
    return editor, request


async def run_edit(
    tmp_path: Path, body: str, *, allowed: tuple[str, ...] = (), attempt: str = "a1"
) -> EditResult:
    editor, request = _bind(tmp_path, body, allowed=allowed, attempt=attempt)
    return await editor.edit(request)


async def _await_marker(marker: Path, *, timeout: float = 15.0) -> int:
    """The pid a fixture script writes to `marker`, once it is completely written."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.isdigit():
            return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError(f"fixture script never wrote {marker}")


def _gone(pid: int) -> bool:
    """True when `pid` is dead — including dead-but-unreaped, which is a zombie."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - a recycled pid owned elsewhere
        return False
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        # The `/proc` entry went away under us (or there is no `/proc`): report
        # "still there", and let the next `os.kill` poll settle it.
        return False
    # The state field follows the last ")" of the comm field.
    return stat_line.rpartition(")")[2].split()[0] == "Z"


async def _assert_gone(pid: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"process {pid} survived the attempt")


# -- results --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edited_and_declined(tmp_path: Path) -> None:
    result = await run_edit(tmp_path, EDITED)

    assert result == EditResult("edited", "ok")
    assert (tmp_path / "workspace" / "skills" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "seed\nedited\n"

    other = tmp_path / "second"
    other.mkdir()
    editor, request = _bind(other, DECLINED)

    assert await editor.edit(request) == EditResult("declined", "nothing to do")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "note"),
    [
        (EXIT_STATUS, "exit_status"),
        (NO_RESULT, "no_result"),
        (BAD_RESULT, "bad_result"),
        (TOO_LARGE, "result_too_large"),
    ],
)
async def test_failure_codes(tmp_path: Path, body: str, note: str) -> None:
    assert await run_edit(tmp_path, body) == EditResult("failed", note, code=note)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        '{"status":"unknown","note":"n"}',
        '{"status":"edited"}',
        '{"status":"edited","note":1}',
        '{"status":"edited","note":"n","extra":1}',
        '["edited","n"]',
        '{"status":"edited","note":"' + "n" * (MAX_NOTE_BYTES + 1) + '"}',
    ],
)
async def test_result_line_shapes_that_are_bad_result(tmp_path: Path, line: str) -> None:
    body = f"import sys; sys.stdout.write({line + chr(10)!r})"

    assert await run_edit(tmp_path, body) == EditResult("failed", "bad_result", code="bad_result")


@pytest.mark.asyncio
async def test_last_nonblank_line_wins(tmp_path: Path) -> None:
    body = (
        'print("chatter"); print(); '
        'import json; print(json.dumps({"status":"declined","note":"last"})); print("")'
    )

    assert await run_edit(tmp_path, body) == EditResult("declined", "last")


@pytest.mark.asyncio
async def test_a_note_of_exactly_the_cap_is_accepted(tmp_path: Path) -> None:
    body = f'import json; print(json.dumps({{"status":"edited","note":"n"*{MAX_NOTE_BYTES}}}))'

    result = await run_edit(tmp_path, body)

    assert result.status == "edited"
    assert len(result.note.encode()) == MAX_NOTE_BYTES


# -- channel guarantees ---------------------------------------------------------


@pytest.mark.asyncio
async def test_request_document_carries_exactly_the_declared_keys(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, ECHO_REQUEST)

    assert await editor.edit(request) == EditResult("edited", "ok")

    document = json.loads((request.workspace / "request.json").read_text(encoding="utf-8"))
    assert set(document) == {
        "version",
        "attempt_id",
        "round",
        "workspace",
        "writable_paths",
        "deadline_seconds",
        "brief",
    }
    assert document["version"] == REQUEST_VERSION == "edit_request/1"
    assert document["attempt_id"] == "a1"
    assert document["round"] == 1
    assert document["workspace"] == str(request.workspace)
    assert document["writable_paths"] == ["skills/**"]
    assert document["deadline_seconds"] == 30.0
    assert document["brief"] == brief_payload(request.brief)


ASSERTS_V4 = (
    "import json,sys; r=json.load(sys.stdin); "
    'assert r["brief"]["version"]=="edit_brief/4", r["brief"]["version"]; '
    'assert set(r["brief"]) == '
    '{"version","champion_digest","operator_context","limits","cases","history","diagnosis"}; '
    'assert r["brief"]["diagnosis"]["status"] == "absent"; '
    'print(json.dumps({"status":"declined","note":"v4"}))'
)


@pytest.mark.asyncio
async def test_request_document_carries_the_edit_brief_4_payload(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, ASSERTS_V4)

    result = await editor.edit(request)

    assert result.status == "declined"
    assert result.note == "v4"


@pytest.mark.asyncio
async def test_a_large_request_and_an_early_writer_do_not_deadlock(tmp_path: Path) -> None:
    """Both directions run at once: neither side can block the other into the deadline.

    A real brief passes the pipe buffer easily, and a child is free to write
    before it reads. Writing the whole request before starting to read would
    park the parent in `drain()` while the child parks on a full stdout pipe,
    and the attempt would burn its whole deadline for C1 to journal as
    `timeout` rather than returning the child's own answer.
    """

    brief = _large_brief()
    assert len(json.dumps(brief_payload(brief))) > 200 * 1024
    editor, request = _bind(tmp_path, WRITES_BEFORE_READING, brief=brief)

    result = await asyncio.wait_for(editor.edit(request), 10.0)

    assert result == EditResult("declined", "cases:400")


@pytest.mark.asyncio
async def test_stderr_goes_to_file_not_note(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, STDERR, attempt="a7")

    result = await editor.edit(request)

    assert result == EditResult("failed", "n", code="program_failed")
    log = tmp_path / "run" / "editor-logs" / "a7.stderr"
    assert "secret-in-stderr" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run" / "editor-logs").stat().st_mode) == 0o700
    assert "secret-in-stderr" not in result.note


@pytest.mark.asyncio
async def test_allowed_env_forwarded_and_others_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("W1_ALLOWED", "yes")
    monkeypatch.setenv("W1_OTHER", "no")

    result = await run_edit(tmp_path, ENV, allowed=("W1_ALLOWED",))

    assert result == EditResult("declined", "yes|")


# -- verification ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_run_verify_refuses_a_changed_command(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, EDITED)
    (tmp_path / "editor" / "main.py").write_text("# replaced\n", encoding="utf-8")

    result = await editor.edit(request)

    assert result == EditResult("failed", "command_changed", code="command_changed")
    assert (tmp_path / "workspace" / "skills" / "SKILL.md").read_text(encoding="utf-8") == "seed\n"


@pytest.mark.asyncio
async def test_post_run_verify_turns_edited_into_command_changed(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, REWRITES_SELF)

    assert await editor.edit(request) == EditResult(
        "failed", "command_changed", code="command_changed"
    )


def test_identity_changes_with_script_bytes_not_with_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    three = tmp_path / "three"
    for root in (one, two, three):
        root.mkdir()
    here, _ = _bind(tmp_path, DECLINED, root=one)
    there, _ = _bind(tmp_path, DECLINED, root=two)
    other, _ = _bind(tmp_path, EDITED, root=three)

    assert here.identity.kind == "subprocess"
    assert len(here.identity.digest) == 64
    assert here.identity == there.identity  # the path the script lives at is not identity
    assert here.identity != other.identity  # its bytes are

    # The declared name and its presence are identity; its value is not
    # (PDR-0003, amendment A13). A rotated value is the same campaign; a name
    # that disappears from the environment is not.
    monkeypatch.setenv("W1_ALLOWED", "v1")
    first, _ = _bind(tmp_path, DECLINED, root=one, allowed=("W1_ALLOWED",))
    monkeypatch.setenv("W1_ALLOWED", "v2")
    second, _ = _bind(tmp_path, DECLINED, root=one, allowed=("W1_ALLOWED",))
    monkeypatch.delenv("W1_ALLOWED")
    absent, _ = _bind(tmp_path, DECLINED, root=one, allowed=("W1_ALLOWED",))

    assert first.identity == second.identity
    assert first.identity != absent.identity
    assert first.identity != here.identity


# -- cancellation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_kills_a_term_ignoring_script(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    editor, request = _bind(tmp_path, IGNORES_TERM, args=(str(marker),))
    task = asyncio.ensure_future(editor.edit(request))
    pid = await _await_marker(marker)

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    # SIGTERM is ignored, so the full grace elapses and SIGKILL ends it.
    assert KILL_GRACE_SECONDS - 0.2 <= elapsed < KILL_GRACE_SECONDS + 2.0
    await _assert_gone(pid)


@pytest.mark.asyncio
async def test_fork_child_dies_with_group(tmp_path: Path) -> None:
    parent_marker = tmp_path / "parent.pid"
    child_marker = tmp_path / "fork.pid"
    editor, request = _bind(tmp_path, FORKS, args=(str(parent_marker), str(child_marker)))
    task = asyncio.ensure_future(editor.edit(request))
    parent_pid = await _await_marker(parent_marker)
    fork_pid = await _await_marker(child_marker)

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    # Neither process ignores SIGTERM, so the group dies well inside the grace.
    assert elapsed < KILL_GRACE_SECONDS
    await _assert_gone(parent_pid)
    await _assert_gone(fork_pid)


@pytest.mark.asyncio
async def test_run_editor_deadline_is_classified_by_c1(tmp_path: Path) -> None:
    editor, request = _bind(tmp_path, SLEEPS, deadline=0.5)

    outcome = await run_editor(editor, request, cancelled=lambda: False)

    assert outcome == EditorOutcome("failed", None, "timeout", "", False)


@pytest.mark.asyncio
async def test_log_root_replaced_after_admission_is_refused(tmp_path: Path) -> None:
    """The output root is re-admitted before the first write.

    The editor is built while `editor-logs/` is still absent -- as `build_editor`
    builds it, against a run directory C1 has not created yet -- and the root is
    replaced by a symlink afterwards. The point-of-use check refuses it, so no
    attempt's stderr is ever written outside the run directory.
    """

    editor, request = _bind(tmp_path, EDITED)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "editor-logs").symlink_to(elsewhere)

    with pytest.raises(EvolveRefused) as refusal:
        await editor.edit(request)

    assert refusal.value.code == "run_dir_unsafe_entry"
    assert refusal.value.path == "editor-logs"
    assert list(elsewhere.iterdir()) == []


def _log_root(tmp_path: Path) -> Path:
    """The editor's own stderr root, as `_bind` names it, created private."""

    log_root = tmp_path / "run" / "editor-logs"
    log_root.mkdir(parents=True, mode=0o700)
    return log_root


@pytest.mark.asyncio
async def test_a_fifo_at_the_stderr_path_fails_the_attempt_without_blocking(
    tmp_path: Path,
) -> None:
    """The stderr pathname is predictable, so the sink is proved before the spawn.

    `O_NOFOLLOW` alone would have opened a FIFO planted here and waited for a
    reader forever, on the event-loop thread, before C1's deadline could fire.
    """

    os.mkfifo(_log_root(tmp_path) / "a1.stderr")
    editor, request = _bind(tmp_path, EDITED)

    started = time.monotonic()
    result = await asyncio.wait_for(editor.edit(request), 10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the open must not wait for a reader"
    assert result.status == "failed"
    assert result.note == (
        "workspace_changed attempt stderr is not a private regular file: a1.stderr"
    )
    # Nothing was spawned: the Candidate is the seed it was.
    assert (request.workspace / "skills" / "SKILL.md").read_text(encoding="utf-8") == "seed\n"


@pytest.mark.asyncio
async def test_a_hard_link_at_the_stderr_path_is_refused(tmp_path: Path) -> None:
    """The second name is the same inode, so `O_NOFOLLOW` sees no link to refuse."""

    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("untouched\n", encoding="utf-8")
    os.chmod(elsewhere, 0o644)
    os.link(elsewhere, _log_root(tmp_path) / "a1.stderr")
    editor, request = _bind(tmp_path, STDERR)

    result = await editor.edit(request)

    assert result.status == "failed"
    assert result.note.startswith("workspace_changed ")
    assert elsewhere.read_text(encoding="utf-8") == "untouched\n"
    # Not even the `fchmod` or the truncation reached it: the refusal precedes
    # every write, so `O_TRUNC` is applied only to a descriptor already proved.
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o644


@pytest.mark.asyncio
async def test_an_admissible_pre_existing_stderr_file_is_truncated(tmp_path: Path) -> None:
    """The admissible entry: a plain, owner-owned, single-link file, emptied per attempt."""

    path = _log_root(tmp_path) / "a1.stderr"
    path.write_text("from an earlier attempt\n", encoding="utf-8")
    os.chmod(path, 0o600)
    editor, request = _bind(tmp_path, STDERR)

    result = await editor.edit(request)

    assert result == EditResult("failed", "n", code="program_failed")
    assert path.read_text(encoding="utf-8") == "secret-in-stderr\n"


ECHO_PUBLIC = (
    "import json,sys; r=json.load(sys.stdin);"
    ' print(json.dumps({"status":"declined","note":json.dumps(r["brief"]["cases"][0]["public"], sort_keys=True)}))'
)


@pytest.mark.asyncio
async def test_a_nested_public_reaches_the_child_verbatim(tmp_path: Path) -> None:
    from evolution.tree.execution.interaction import EvalCase

    public = {"history": [{"speaker": "CUSTOMER", "text": "earlier"}], "meta": {"k": [1, 2]}}
    frozen = EvalCase("c1", "g1", "s1", (), public, {}, {}).public
    case = EditBriefCase(
        case_id="c1", group_id="g1", stratum="s1", tags=(), public=frozen, repeats=()
    )
    brief = EditBrief(
        champion_digest="a" * 64,
        operator_context=None,
        cases=(case,),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )
    editor, request = _bind(tmp_path, ECHO_PUBLIC, brief=brief)

    result = await editor.edit(request)

    assert result.status == "declined"
    assert json.loads(result.note) == public


def test_the_subprocess_editor_gets_no_reader(tmp_path):
    from dataclasses import replace

    from evolution.tree.roles.editor.subprocess_editor import _request_document
    from tests.test_tree_baseline_traces import reader
    from tests.tree.roles.editor.test_chat_editor import _request, _ws
    from tests.tree.roles.editor.test_editor_evidence import _Trace

    trace = _Trace(tmp_path / "baseline-trace", turns=1)
    try:
        request = _request(_ws(tmp_path))
        assert _request_document(request, request.workspace) == _request_document(
            replace(request, traces=reader(trace)), request.workspace
        )
    finally:
        trace.close()
