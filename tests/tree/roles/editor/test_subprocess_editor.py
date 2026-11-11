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
import errno
import json
import os
import signal
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import evolution.tree.roles.editor.subprocess_editor as module
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


# -- stderr redaction and capture (#200) ----------------------------------------

SECRET = "run-secret-value"
ANSWERS = "import json\nprint(json.dumps({'status':'declined','note':'ok'}))\n"


def _stderr_log(tmp_path: Path, attempt: str = "a1") -> bytes:
    return (tmp_path / "run" / "editor-logs" / f"{attempt}.stderr").read_bytes()


async def test_stderr_secret_is_redacted(tmp_path: Path) -> None:
    editor, request = _bind(
        tmp_path, "import os\nos.write(2, b'before run-secret-value end\\n')\n" + ANSWERS
    )

    result = await replace(editor, secrets=(SECRET,)).edit(request)

    assert result.status == "declined"
    assert _stderr_log(tmp_path) == b"before [REDACTED] end\n"


async def test_stderr_secret_split_across_writes_is_redacted(tmp_path: Path) -> None:
    editor, request = _bind(
        tmp_path,
        "import os,sys,time\n"
        "os.write(2, b'before run-secr')\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.2)\n"
        "os.write(2, b'et-value end\\n')\n" + ANSWERS,
    )

    result = await replace(editor, secrets=(SECRET,)).edit(request)

    assert result.status == "declined"
    assert _stderr_log(tmp_path) == b"before [REDACTED] end\n"


async def test_cancellation_does_not_flush_a_secret_prefix(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    editor, request = _bind(
        tmp_path,
        "import os,sys,time\n"
        "os.write(2, b'before run-secr')\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        args=(str(marker),),
    )

    task = asyncio.create_task(replace(editor, secrets=(SECRET,)).edit(request))
    pid = await _await_marker(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_gone(pid)
    assert _stderr_log(tmp_path) == b"before [REDACTED]"


async def test_stderr_written_during_termination_is_redacted(tmp_path: Path) -> None:
    """The copy runs after termination, so a program's last words are in the log."""

    marker = tmp_path / "pid"
    editor, request = _bind(
        tmp_path,
        "import os,signal,sys,time\n"
        "def term(*_):\n"
        "    os.write(2, b'term run-secret-value\\n')\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        args=(str(marker),),
    )

    task = asyncio.create_task(replace(editor, secrets=(SECRET,)).edit(request))
    pid = await _await_marker(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_gone(pid)
    assert _stderr_log(tmp_path) == b"term [REDACTED]\n"


async def test_stderr_is_redacted_when_the_exchange_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pid"
    editor, request = _bind(
        tmp_path,
        "import os,sys,time\n"
        "os.write(2, b'before run-secret-value end\\n')\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        args=(str(marker),),
    )

    async def failing(process: asyncio.subprocess.Process, document: bytes) -> tuple[bytes, bool]:
        await _await_marker(marker)
        raise RuntimeError("exchange failed")

    monkeypatch.setattr(module, "_exchange", failing)

    with pytest.raises(RuntimeError, match="exchange failed"):
        await replace(editor, secrets=(SECRET,)).edit(request)

    await _assert_gone(int(marker.read_text(encoding="utf-8")))
    assert _stderr_log(tmp_path) == b"before [REDACTED] end\n"


class _FullDiskSink:
    def write(self, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        pass


async def test_log_write_failure_keeps_the_valid_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor, request = _bind(tmp_path, "import os\nos.write(2, b'x'*(1<<20))\n" + ANSWERS)
    monkeypatch.setattr(module, "_open_private_log", lambda path: _FullDiskSink())

    result = await replace(editor, secrets=(SECRET,)).edit(request)

    assert result == EditResult("declined", "ok")


def _refuse_temp_file(*args: object, **kwargs: object) -> object:
    raise OSError(errno.EMFILE, "Too many open files")


async def test_temp_file_failure_keeps_the_result_and_writes_the_fixed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.tempfile, "TemporaryFile", _refuse_temp_file)
    editor, request = _bind(tmp_path, "import os\nos.write(2, b'lost\\n')\n" + ANSWERS)

    result = await editor.edit(request)

    assert result == EditResult("declined", "ok")
    assert _stderr_log(tmp_path) == module._STDERR_NOT_CAPTURED
    assert b"Too many" not in _stderr_log(tmp_path)


async def test_temp_file_failure_with_a_failing_log_keeps_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.tempfile, "TemporaryFile", _refuse_temp_file)
    monkeypatch.setattr(module, "_open_private_log", lambda path: _FullDiskSink())
    editor, request = _bind(tmp_path, ANSWERS)

    result = await editor.edit(request)

    assert result == EditResult("declined", "ok")


async def test_temp_file_failure_of_another_kind_keeps_the_result_and_writes_the_fixed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise RuntimeError("not an OSError")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", refuse)
    editor, request = _bind(tmp_path, ANSWERS)

    result = await editor.edit(request)

    assert result == EditResult("declined", "ok")
    assert _stderr_log(tmp_path) == module._STDERR_NOT_CAPTURED


def _raising_redaction(monkeypatch: pytest.MonkeyPatch, kind: str) -> tuple[str, ...]:
    """The secret set of a run whose redaction raises, when built or while feeding."""

    if kind == "construction":
        return ("\ud800token-value",)

    def feed(self: object, chunk: bytes, *, final: bool = False) -> bytes:
        raise MemoryError

    monkeypatch.setattr(module._SecretStream, "feed", feed)
    return (SECRET,)


@pytest.mark.parametrize("kind", ["construction", "feed"])
async def test_raising_redaction_keeps_the_valid_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    secrets = _raising_redaction(monkeypatch, kind)
    log = _RecordingLog()
    monkeypatch.setattr(module, "_open_private_log", lambda path: log)
    editor, request = _bind(tmp_path, "import os\nos.write(2, b'raw\\n')\n" + ANSWERS)

    result = await replace(editor, secrets=secrets).edit(request)

    assert result == EditResult("declined", "ok")
    assert log.data == b""
    assert log.closed


@pytest.mark.parametrize("kind", ["construction", "feed"])
async def test_raising_redaction_keeps_the_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    secrets = _raising_redaction(monkeypatch, kind)
    marker = tmp_path / "pid"
    editor, request = _bind(
        tmp_path,
        "import os,sys,time\n"
        "os.write(2, b'raw\\n')\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        args=(str(marker),),
    )

    task = asyncio.create_task(replace(editor, secrets=secrets).edit(request))
    pid = await _await_marker(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_gone(pid)
    assert _stderr_log(tmp_path) == b""


async def test_spawn_failure_closes_the_temp_file_and_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[object] = []
    real = module.tempfile.TemporaryFile

    def recording(*args: object, **kwargs: object) -> object:
        created.append(real(*args, **kwargs))
        return created[-1]

    log = _RecordingLog()
    monkeypatch.setattr(module.tempfile, "TemporaryFile", recording)
    monkeypatch.setattr(module, "_open_private_log", lambda path: log)

    async def refused(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", refused)
    editor, request = _bind(tmp_path, ANSWERS)

    with pytest.raises(OSError, match="temporarily unavailable"):
        await editor.edit(request)

    assert log.closed
    assert len(created) == 1 and created[0].closed


async def test_empty_secret_set_logs_stderr_byte_for_byte(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 3  # not valid UTF-8
    editor, request = _bind(tmp_path, f"import os\nos.write(2, bytes(range(256))*3)\n{ANSWERS}")

    result = await replace(editor, secrets=()).edit(request)

    assert result.status == "declined"
    assert _stderr_log(tmp_path) == payload


async def test_large_stderr_before_reading_stdin_does_not_deadlock(tmp_path: Path) -> None:
    editor, request = _bind(
        tmp_path,
        "import json,os,sys\n"
        "os.write(2, b'x'*(1<<20))\n"
        "r=json.load(sys.stdin)\n"
        "print(json.dumps({'status':'declined','note':r['version']}))\n",
        brief=_large_brief(),
    )

    async with asyncio.timeout(20):
        result = await replace(editor, secrets=(SECRET,)).edit(request)

    assert result == EditResult("declined", REQUEST_VERSION)
    assert _stderr_log(tmp_path) == b"x" * (1 << 20)


def test_build_editor_passes_the_run_secrets_to_the_subprocess_editor(tmp_path: Path) -> None:
    from evolution.tree.bindings import build_editor
    from tests.test_tree_bindings import editor_section

    seed = tmp_path / "seed"
    seed.mkdir()

    editor = build_editor(
        editor_section(tmp_path),
        seed_root=seed,
        run_dir=tmp_path / "run",
        environ={},
        secrets=[SECRET],
    )

    assert isinstance(editor, SubprocessEditor)
    assert editor.secrets == (SECRET,)


async def test_secret_straddling_the_copy_read_boundary_is_redacted(tmp_path: Path) -> None:
    before = module._DRAIN_CHUNK_BYTES - 5
    editor, request = _bind(
        tmp_path,
        f"import os\nos.write(2, b'x'*{before} + b'run-secret-value end\\n')\n" + ANSWERS,
    )

    result = await replace(editor, secrets=(SECRET,)).edit(request)

    assert result.status == "declined"
    assert _stderr_log(tmp_path) == b"x" * before + b"[REDACTED] end\n"


def test_secret_stream_accepts_a_surrogate_escaped_secret() -> None:
    secret = b"tok\xffen".decode("utf-8", "surrogateescape")

    stream = module._SecretStream((secret,))

    assert stream.feed(b"a tok\xffen b\n", final=True) == b"a [REDACTED] b\n"


# -- the capture itself -----------------------------------------------------------


class _RecordingLog:
    """A log that keeps what it is given and can fail one chosen call."""

    def __init__(self, *, fail_write: int | None = None, fail_close: bool = False) -> None:
        self.data = b""
        self.writes = 0
        self.fail_write = fail_write
        self.fail_close = fail_close
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes += 1
        if self.writes == self.fail_write:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise OSError(errno.EIO, "Input/output error")


def _capture_with(log: _RecordingLog, data: bytes = b"") -> module._StderrCapture:
    capture = module._StderrCapture(log, (SECRET,))
    os.write(capture.child_stderr.fileno(), data)
    return capture


def _refuse_pread(*args: object) -> bytes:
    raise OSError(errno.EIO, "Input/output error")


@pytest.mark.parametrize(
    ("fault", "written"),
    [("read", b""), ("write", b""), ("final_write", b"a [REDACTED]")],
)
def test_capture_stops_at_a_copy_fault_and_closes_both_files(
    monkeypatch: pytest.MonkeyPatch, fault: str, written: bytes
) -> None:
    log = _RecordingLog(fail_write={"write": 1, "final_write": 2}.get(fault))
    capture = _capture_with(log, b"a run-secret-value b\n")
    scratch = capture.child_stderr
    if fault == "read":
        monkeypatch.setattr(module.os, "pread", _refuse_pread)

    capture.finish()

    assert log.data == written
    assert log.closed and scratch.closed


class _Stop(BaseException):
    pass


def test_capture_lets_a_base_exception_through_after_closing_both_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def feed(self: object, chunk: bytes, *, final: bool = False) -> bytes:
        raise _Stop

    log = _RecordingLog()
    capture = _capture_with(log, b"payload\n")
    scratch = capture.child_stderr
    monkeypatch.setattr(module._SecretStream, "feed", feed)

    with pytest.raises(_Stop):
        capture.finish()

    assert log.closed and scratch.closed


def test_capture_survives_a_failing_scratch_close(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _RecordingLog()
    capture = _capture_with(log, b"payload\n")
    scratch = capture.child_stderr
    real_close = scratch.close

    class Closing:
        def fileno(self) -> int:
            return scratch.fileno()

        def close(self) -> None:
            real_close()
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(capture, "_scratch", Closing())

    capture.finish()

    assert log.data == b"payload\n"
    assert log.closed and scratch.closed


def test_capture_survives_a_failing_log_close() -> None:
    log = _RecordingLog(fail_close=True)
    capture = _capture_with(log, b"payload\n")
    scratch = capture.child_stderr

    capture.finish()

    assert log.data == b"payload\n"
    assert log.closed and scratch.closed


def test_capture_copies_only_the_size_taken_when_the_copy_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    capture = _capture_with(log, b"before\n")
    real_pread = os.pread

    def flooded(fd: int, length: int, offset: int) -> bytes:
        os.pwrite(fd, b"x" * 4096, os.fstat(fd).st_size)  # a holder that keeps writing
        return real_pread(fd, length, offset)

    monkeypatch.setattr(module.os, "pread", flooded)

    capture.finish()

    assert log.data == b"before\n"


def test_capture_does_not_move_the_offset_the_child_shares() -> None:
    log = _RecordingLog()
    capture = _capture_with(log, b"written\n")
    shared = os.dup(capture.child_stderr.fileno())  # the same open file description
    os.lseek(shared, 3, os.SEEK_SET)  # where a surviving process would write next

    try:
        capture.finish()
        assert os.lseek(shared, 0, os.SEEK_CUR) == 3
    finally:
        os.close(shared)

    assert log.data == b"written\n"


# -- a background process that outlives the program and holds its stderr ---------

#: Generous for a loaded machine and still far below the holder's 30 s lifetime.
HOLDER_BOUND = 20.0


def stderr_holder(
    answer: str,
    *,
    first: tuple[str, ...] = (),
    then: tuple[str, ...] = ("time.sleep(30)",),
) -> str:
    """A program that answers and exits, leaving a child that holds only its stderr.

    `first` runs in the child before the program may exit, `then` after. The
    child's pid is in `argv[1]`, so the test can kill it whatever happens, and
    its sleep is bounded, so it cannot outlive a test that never gets there.
    """

    child = (
        "null=os.open(os.devnull,os.O_RDWR); os.dup2(null,0); os.dup2(null,1)",
        *first,
        "open(sys.argv[1],'w').write(str(os.getpid()))",
        *then,
        "os._exit(0)",
    )
    return (
        "import os,sys,time\n"
        "if os.fork()==0:\n    " + "\n    ".join(child) + "\n"
        "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n" + answer
    )


def kill_holder(marker: Path) -> None:
    try:
        os.kill(int(marker.read_text(encoding="utf-8")), signal.SIGKILL)
    except (OSError, ValueError):
        pass


#: A role that waited 5 s after the program's exit would miss this deadline.
NEAR_DEADLINE = 8.0
EXITS_BEFORE_DEADLINE = 2.5


def exits_near_the_deadline(answer: str) -> str:
    """`answer`, held until the monotonic time in `argv[2]`.

    The test computes that time, so a slow interpreter start cannot eat the
    margin. On Linux the monotonic clock is shared between processes.
    """

    return "time.sleep(max(0.0,float(sys.argv[2])-time.monotonic()))\n" + answer


async def test_stderr_holder_adds_no_delay(tmp_path: Path) -> None:
    marker = tmp_path / "holder"
    editor, request = _bind(tmp_path, stderr_holder(ANSWERS), args=(str(marker),))

    try:
        started = time.monotonic()
        result = await editor.edit(request)
        elapsed = time.monotonic() - started
        assert not _gone(int(marker.read_text(encoding="utf-8")))
    finally:
        kill_holder(marker)

    assert result == EditResult("declined", "ok")
    assert elapsed < HOLDER_BOUND


async def test_stderr_holder_leaves_a_result_just_before_the_deadline_valid(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "holder"
    exit_at = time.monotonic() + NEAR_DEADLINE - EXITS_BEFORE_DEADLINE
    editor, request = _bind(
        tmp_path,
        stderr_holder(exits_near_the_deadline(ANSWERS)),
        args=(str(marker), str(exit_at)),
        deadline=NEAR_DEADLINE,
    )

    try:
        outcome = await run_editor(editor, request, cancelled=lambda: False)
        assert not _gone(int(marker.read_text(encoding="utf-8")))
    finally:
        kill_holder(marker)

    assert (outcome.terminal, outcome.status, outcome.error_class) == (
        "finished",
        "declined",
        None,
    )


SECRET_BEFORE_COPY = ("os.write(2,b'early run-secret-value \\nsplit run-secr')",)
#: The test creates the `.go` file once the role has returned, so these bytes
#: are written after the copy however slow the machine is; the wait is bounded
#: so the holder cannot outlive a test that never gets there.
SECRET_AFTER_COPY = (
    "for _ in range(1500):",
    "    if os.path.exists(sys.argv[1]+'.go'): break",
    "    time.sleep(0.01)",
    "os.write(2,b'et-value late run-secret-value\\n')",
    "open(sys.argv[1]+'.done','w').write(str(os.getpid()))",
)
HOLDER_LOG = b"early [REDACTED] \nsplit [REDACTED]"


async def test_stderr_written_by_a_holder_after_the_copy_is_not_in_the_log(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "holder"
    editor, request = _bind(
        tmp_path,
        stderr_holder(ANSWERS, first=SECRET_BEFORE_COPY, then=SECRET_AFTER_COPY),
        args=(str(marker),),
    )

    try:
        result = await replace(editor, secrets=(SECRET,)).edit(request)
        Path(f"{marker}.go").touch()
        await _await_marker(Path(f"{marker}.done"))
    finally:
        kill_holder(marker)

    assert result.status == "declined"
    assert _stderr_log(tmp_path) == HOLDER_LOG
