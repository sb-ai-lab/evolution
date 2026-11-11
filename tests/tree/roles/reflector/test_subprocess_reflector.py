"""`SubprocessReflector`, the `subprocess` `CandidateReflector`.

`SubprocessReflector.reflect` is `SubprocessEditor.edit`'s twin over a
different request/result shape: `reflect_request/2` on stdin, one
`{"status", "groups", "note"}` result line back, the same private-stderr,
verify-before-and-after and cancellation machinery. Because that machinery is
imported from `subprocess_editor` rather than copied, most of its own test
coverage (deadlock avoidance, hard-link/FIFO stderr refusal, `PYTHONPATH`
containment, ...) already proves this module's process handling; the cases
here cover what actually differs -- the request document's shape and key
order, the three-way result contract, and that a Reflector's projection is
read-only to the program it runs.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.environment_docs import EnvironmentDocs
from evolution.tree.roles.reflector.reflect_brief import build_reflect_brief
from evolution.tree.roles.reflector.reflect_evidence import select_focus
from evolution.tree.roles.reflector.subprocess_reflector import (
    KILL_GRACE_SECONDS,
    MAX_NOTE_BYTES,
    SubprocessReflector,
    _classify,
    _request_document,
)
from evolution.tree.roles.reflector_contracts import ProbeAccess, ReflectRequest, ReflectResult
from evolution.tree.roles.workspace import discard, freeze_projection, project
from tests.tree.roles.reflector.test_reflect_brief import _CHAMPION, _corpus
from tests.tree.roles.reflector.test_reflect_evidence import _snapshot, rep

pytestmark = pytest.mark.slow

PROGRAM = """import json, sys
request = json.loads(sys.stdin.read())
assert request["version"] == "reflect_request/2"
assert request["probe"] is None
open(request["workspace"] + "/skills/SKILL.md").read()      # the projection is readable
try:
    open(request["workspace"] + "/skills/SKILL.md", "a").write("x")
    status = "failed"                                           # ... and not writable
except PermissionError:
    status = "reflected"
print("progress line")
print(json.dumps({"status": status, "groups": [{"root_cause": "r", "suggested_fix": "s",
    "evidence_ids": ["obs:a:1"], "affected_cases": ["a"], "target_paths": ["skills/SKILL.md"]}], "note": "done"}))
"""
DECLINED = 'import json; print(json.dumps({"status": "declined", "groups": [], "note": "nothing"}))'
EXIT_STATUS = "import sys; sys.exit(3)"
NO_RESULT = "pass"
BAD_RESULT = 'print("not json")'
TOO_LARGE = (
    'import sys; sys.stdout.write("x"*(1<<20+1)); print();'
    ' import json; print(json.dumps({"status":"reflected","groups":[],"note":""}))'
)
STDERR = (
    'import sys,json; print("secret-in-stderr", file=sys.stderr);'
    ' print(json.dumps({"status":"failed","groups":[],"note":"n"}))'
)
ENV = (
    "import os,json;"
    ' print(json.dumps({"status":"declined","groups":[],'
    ' "note":os.environ.get("W1_ALLOWED","")+"|"+os.environ.get("W1_OTHER","")}))'
)
REWRITES_SELF = (
    "import json,pathlib;"
    ' pathlib.Path(__file__).write_text("# replaced\\n");'
    ' print(json.dumps({"status":"reflected","groups":[],"note":"ok"}))'
)
SLEEPS = "import time; time.sleep(60)"
IGNORES_TERM = (
    "import os,signal,sys,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1],'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)


# -- fixtures -------------------------------------------------------------------


def _script(root: Path, body: str) -> Path:
    reflector_dir = root / "reflector"
    reflector_dir.mkdir(parents=True, exist_ok=True)
    script = reflector_dir / "reflect.py"
    script.write_text(body + "\n", encoding="utf-8")
    return script


def _seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "skills").mkdir(parents=True, exist_ok=True)
    (seed / "skills" / "SKILL.md").write_text("seed\n", encoding="utf-8")
    return seed


def _bind_reflector(
    tmp_path: Path, body: str, *, allowed: tuple[str, ...] = (), args: tuple[str, ...] = ()
) -> SubprocessReflector:
    script = _script(tmp_path, body)
    binding = CommandBinding(
        argv=(sys.executable, script.name, *args),
        cwd=script.parent,
        allowed_env=frozenset(allowed),
    )
    return SubprocessReflector(binding=binding, log_root=tmp_path / "run" / "reflector-logs")


@pytest.fixture
def bound_reflector(tmp_path: Path):
    def factory(body: str, **kwargs) -> SubprocessReflector:
        return _bind_reflector(tmp_path, body, **kwargs)

    return factory


def _evidence():
    reps = (
        rep("case-01", 1, failed=("answers",)),
        rep("case-01", 2),
        rep("case-02", 1),
    )
    focus = select_focus(reps, snapshot=_snapshot({}))
    return build_reflect_brief(
        focus=focus,
        repeats=reps,
        corpus=_corpus(),
        champion_digest=_CHAMPION,
        operator_context=None,
        history=EMPTY_HISTORY,
        editable_paths=("skills/**",),
    )


def _project_seed(tmp_path: Path):
    tmp_root = tmp_path / "run" / ".reflections"
    tmp_root.mkdir(parents=True, exist_ok=True)
    projection = project(_seed(tmp_path), tmp_root=tmp_root)
    freeze_projection(projection)
    return projection


@pytest.fixture
def reflect_request(tmp_path: Path):
    projection = _project_seed(tmp_path)
    try:
        yield ReflectRequest(
            "r01-x01",
            1,
            projection.root,
            _evidence(),
            None,
            None,
            ("skills/**",),
            30.0,
        )
    finally:
        discard(projection)


async def _await_marker(marker: Path, *, timeout: float = 15.0) -> int:
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - a recycled pid owned elsewhere
        return False
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat_line.rpartition(")")[2].split()[0] == "Z"


async def _assert_gone(pid: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"process {pid} survived the attempt")


# -- request document -------------------------------------------------------------


def test_request_document_shape(reflect_request: ReflectRequest) -> None:
    document = json.loads(_request_document(reflect_request))

    assert list(document) == [
        "version",
        "reflection_id",
        "round",
        "workspace",
        "editable_paths",
        "deadline_seconds",
        "environment_docs",
        "probe",
        "evidence",
    ]
    assert document["version"] == "reflect_request/2"
    assert document["reflection_id"] == "r01-x01"
    assert document["round"] == 1
    assert document["workspace"] == str(reflect_request.workspace)
    assert document["editable_paths"] == ["skills/**"]
    assert document["deadline_seconds"] == 30.0
    assert document["environment_docs"] is None
    assert document["probe"] is None
    assert document["evidence"]["version"] == "reflect_brief/1"


def test_request_document_carries_the_probe(tmp_path: Path) -> None:
    projection = _project_seed(tmp_path)
    try:
        request = ReflectRequest(
            "r01-x01",
            1,
            projection.root,
            _evidence(),
            # The document carries the rendering, not the snapshot's files:
            # a subprocess Reflector has no control plane to lay them out in.
            EnvironmentDocs("docs", "d" * 64, (("api.md", b"unused"),)),
            ProbeAccess("http://target", 3, ("call",), "desc"),
            ("skills/**",),
            30.0,
        )

        document = json.loads(_request_document(request))

        assert document["environment_docs"] == "docs"
        assert document["probe"] == {
            "url": "http://target",
            "budget": 3,
            "methods": ["call"],
            "description": "desc",
        }
    finally:
        discard(projection)


# -- results ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_runs_the_program_against_a_read_only_projection(
    tmp_path: Path, bound_reflector, reflect_request: ReflectRequest
) -> None:
    reflector = bound_reflector(PROGRAM)

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.note) == ("reflected", "done")
    assert result.groups[0]["root_cause"] == "r"
    assert (tmp_path / "run" / "reflector-logs" / "r01-x01.stderr").exists()


@pytest.mark.asyncio
async def test_declined(tmp_path: Path, bound_reflector, reflect_request: ReflectRequest) -> None:
    reflector = bound_reflector(DECLINED)

    result = await reflector.reflect(reflect_request)

    assert result == ReflectResult("declined", (), "nothing")


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
async def test_failure_codes(
    bound_reflector, reflect_request: ReflectRequest, body: str, note: str
) -> None:
    reflector = bound_reflector(body)

    result = await reflector.reflect(reflect_request)

    assert result == ReflectResult("failed", (), note, code=note)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        '{"status":"unknown","groups":[],"note":"n"}',
        '{"status":"reflected","note":"n"}',
        '{"status":"reflected","groups":"not-a-list","note":"n"}',
        '{"status":"reflected","groups":[],"note":1}',
        '{"status":"reflected","groups":[],"note":"n","extra":1}',
        '["reflected","n"]',
        '{"status":"reflected","groups":[],"note":"' + "n" * (MAX_NOTE_BYTES + 1) + '"}',
    ],
)
async def test_result_line_shapes_that_are_bad_result(
    bound_reflector, reflect_request: ReflectRequest, line: str
) -> None:
    body = f"import sys; sys.stdout.write({line + chr(10)!r})"
    reflector = bound_reflector(body)

    assert await reflector.reflect(reflect_request) == ReflectResult(
        "failed", (), "bad_result", code="bad_result"
    )


@pytest.mark.asyncio
async def test_last_nonblank_line_wins(bound_reflector, reflect_request: ReflectRequest) -> None:
    body = (
        'print("chatter"); print(); import json; '
        'print(json.dumps({"status":"declined","groups":[],"note":"last"})); print("")'
    )
    reflector = bound_reflector(body)

    assert await reflector.reflect(reflect_request) == ReflectResult("declined", (), "last")


@pytest.mark.asyncio
async def test_a_note_of_exactly_the_cap_is_accepted(
    bound_reflector, reflect_request: ReflectRequest
) -> None:
    body = (
        f'import json; print(json.dumps({{"status":"reflected","groups":[],'
        f'"note":"n"*{MAX_NOTE_BYTES}}}))'
    )
    reflector = bound_reflector(body)

    result = await reflector.reflect(reflect_request)

    assert result.status == "reflected"
    assert len(result.note.encode()) == MAX_NOTE_BYTES


def test_classify() -> None:
    assert _classify(1, b"", False).status == "failed" and _classify(1, b"", False).note == (
        "exit_status"
    )
    assert _classify(0, b"", False).note == "no_result"
    assert _classify(0, b'{"status": "reflected"}\n', False).note == "bad_result"
    assert (
        _classify(0, b'{"status": "declined", "groups": [], "note": "n"}\n', False).status
        == "declined"
    )
    assert _classify(0, b"x", True).note == "result_too_large"


# -- channel guarantees ---------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_goes_to_file_not_note(
    tmp_path: Path, bound_reflector, reflect_request: ReflectRequest
) -> None:
    reflector = bound_reflector(STDERR)

    result = await reflector.reflect(reflect_request)

    assert result == ReflectResult("failed", (), "n", code="program_failed")
    log = tmp_path / "run" / "reflector-logs" / "r01-x01.stderr"
    assert "secret-in-stderr" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run" / "reflector-logs").stat().st_mode) == 0o700
    assert "secret-in-stderr" not in result.note


@pytest.mark.asyncio
async def test_allowed_env_forwarded_and_others_absent(
    tmp_path: Path,
    bound_reflector,
    reflect_request: ReflectRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("W1_ALLOWED", "yes")
    monkeypatch.setenv("W1_OTHER", "no")
    reflector = bound_reflector(ENV, allowed=("W1_ALLOWED",))

    result = await reflector.reflect(reflect_request)

    assert result == ReflectResult("declined", (), "yes|")


# -- verification ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_run_verify_refuses_a_changed_command(
    tmp_path: Path, bound_reflector, reflect_request: ReflectRequest
) -> None:
    reflector = bound_reflector(PROGRAM)
    (tmp_path / "reflector" / "reflect.py").write_text("# replaced\n", encoding="utf-8")

    result = await reflector.reflect(reflect_request)

    assert result == ReflectResult("failed", (), "command_changed", code="command_changed")


@pytest.mark.asyncio
async def test_post_run_verify_turns_reflected_into_command_changed(
    bound_reflector, reflect_request: ReflectRequest
) -> None:
    reflector = bound_reflector(REWRITES_SELF)

    assert await reflector.reflect(reflect_request) == ReflectResult(
        "failed", (), "command_changed", code="command_changed"
    )


def test_identity_kind_and_digest(tmp_path: Path) -> None:
    one = _bind_reflector(tmp_path, DECLINED)
    two_root = tmp_path / "elsewhere"
    two_root.mkdir()
    two = _bind_reflector(two_root, PROGRAM)

    assert one.identity.kind == "subprocess"
    assert len(one.identity.digest) == 64
    assert one.identity != two.identity


# -- cancellation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_kills_a_term_ignoring_script(
    tmp_path: Path, bound_reflector, reflect_request: ReflectRequest
) -> None:
    marker = tmp_path / "child.pid"
    reflector = bound_reflector(IGNORES_TERM, args=(str(marker),))
    task = asyncio.ensure_future(reflector.reflect(reflect_request))
    pid = await _await_marker(marker)

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    assert KILL_GRACE_SECONDS - 0.2 <= elapsed < KILL_GRACE_SECONDS + 2.0
    await _assert_gone(pid)


@pytest.mark.asyncio
async def test_reflector_deadline_is_classified_by_the_run_wrapper(
    bound_reflector, reflect_request: ReflectRequest
) -> None:
    from evolution.tree.campaign_reflector import ReflectorOutcome, run_reflector

    slow_request = ReflectRequest(
        reflect_request.reflection_id,
        reflect_request.round,
        reflect_request.workspace,
        reflect_request.evidence,
        reflect_request.environment_docs,
        reflect_request.probe,
        reflect_request.editable_paths,
        0.5,
    )
    reflector = bound_reflector(SLEEPS)

    outcome = await run_reflector(reflector, slow_request, cancelled=lambda: False)

    assert outcome == ReflectorOutcome("failed", None, "timeout", "", False)


# -- log root admission -----------------------------------------------------------


@pytest.mark.asyncio
async def test_log_root_replaced_after_admission_is_refused(
    tmp_path: Path, bound_reflector, reflect_request: ReflectRequest
) -> None:
    reflector = bound_reflector(PROGRAM)
    elsewhere = tmp_path / "elsewhere-logs"
    elsewhere.mkdir()
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "reflector-logs").symlink_to(elsewhere)

    with pytest.raises(EvolveRefused) as refusal:
        await reflector.reflect(reflect_request)

    assert refusal.value.code == "run_dir_unsafe_entry"
    assert refusal.value.path == "reflector-logs"
    assert list(elsewhere.iterdir()) == []


async def test_probe_stderr_stream_redacts_split_and_final_partial_secrets(
    tmp_path, reflect_request
):
    import asyncio
    from dataclasses import replace

    token = "capability-" + "unique-secret-" * 5
    url = "http://127.0.0.1:9/" + token + "/probe"
    reflector = _bind_reflector(
        tmp_path,
        r"""import json, os, sys, time
# Fill stderr before reading stdin: all pipes must drain concurrently.
os.write(2, b"x" * 200000)
request = json.load(sys.stdin)
url = request["probe"]["url"]
token = url.split("/")[-2]
for value in (url, token):
    os.write(2, value[:31].encode())
    time.sleep(.03)
    os.write(2, value[31:].encode() + b"\n")
os.write(2, token[:29].encode())
print(json.dumps({"status":"declined", "groups":[], "note":"ok"}))
""",
    )
    request = replace(reflect_request, probe=ProbeAccess(url, 1, ("GET",), None))
    result = await asyncio.wait_for(reflector.reflect(request), 10)
    assert result.status == "declined", result.note
    output = (reflector.log_root / (request.reflection_id + ".stderr")).read_text()
    assert token not in output and token[:29] not in output and url not in output
    assert "[REDACTED]" in output


async def test_probe_stderr_cancellation_drains_until_process_is_killed(
    tmp_path, reflect_request, monkeypatch
):
    import asyncio
    from dataclasses import replace

    import evolution.tree.roles.reflector.subprocess_reflector as module

    marker = tmp_path / "started"
    reflector = _bind_reflector(
        tmp_path,
        f"""import json, os, pathlib, signal, sys
signal.signal(signal.SIGTERM, lambda *_: os.write(2, b"x" * 1000000))
request = json.load(sys.stdin)
pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))
while True:
    os.write(2, (request["probe"]["url"] * 1000).encode())
""",
    )
    original = asyncio.create_subprocess_exec
    processes = []
    stopped = asyncio.Event()
    draining_at_termination = []
    original_drain = module._redact_stderr
    original_terminate = module._terminate_shielded

    async def drain(*args):
        try:
            await original_drain(*args)
        finally:
            stopped.set()

    async def terminate(process, cancellation):
        if process.returncode is None:
            draining_at_termination.append(not stopped.is_set())
        await original_terminate(process, cancellation)

    monkeypatch.setattr(module, "_redact_stderr", drain)
    monkeypatch.setattr(module, "_terminate_shielded", terminate)

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("evolution.tree.roles.editor.subprocess_editor.KILL_GRACE_SECONDS", 0.1)
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None),
    )
    task = asyncio.create_task(reflector.reflect(request))
    try:
        await _await_marker(marker)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=4)
        assert done, "stderr stopped draining before process-group cleanup finished"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert draining_at_termination and all(draining_at_termination)
    finally:
        if not task.done():
            processes[0]._transport.close()
            await asyncio.gather(task, return_exceptions=True)


def test_probe_stderr_overlap_and_cutoff_redaction():
    from evolution.tree.roles.reflector.subprocess_reflector import _SecretStream

    stream = _SecretStream(("abcdefgh", "fghijklm"))
    output = b"".join(stream.feed(part) for part in [b"safe abc", b"defghi", b"jklm end abc"])
    output += stream.feed(b"", final=True)
    assert output == b"safe [REDACTED] end [REDACTED]"
    assert not stream.pending and not stream.mask


async def test_probe_stderr_sink_failure_still_drains_pipe():
    import asyncio

    from evolution.tree.roles.reflector.subprocess_reflector import _redact_stderr

    error = OSError("stderr unavailable")

    class Sink:
        def write(self, value):
            raise error

    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 200000)
    stream.feed_eof()
    with pytest.raises(OSError) as caught:
        await _redact_stderr(stream, Sink(), ("capability",))
    assert caught.value is error
    assert stream.at_eof(), "a failed log sink must not leave a full stderr pipe"


async def test_reflector_stderr_without_probe_is_redacted(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    reflector = replace(
        bound_reflector(
            "import json, os, time\n"
            "os.write(2, b'before run-secr')\n"
            "time.sleep(0.2)\n"
            "os.write(2, b'et-value end\\n')\n"
            'print(json.dumps({"status": "declined", "groups": [], "note": "ok"}))\n'
        ),
        secrets=("run-secret-value",),
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    log = tmp_path / "run" / "reflector-logs" / "r01-x01.stderr"
    assert log.read_bytes() == b"before [REDACTED] end\n"


def test_build_reflector_passes_the_run_secrets_to_the_subprocess_reflector(
    tmp_path: Path,
) -> None:
    from evolution.tree.bindings import build_reflector
    from evolution.tree.roles.config import ReflectorSection
    from tests.test_tree_bindings import editor_section

    seed = tmp_path / "seed"
    seed.mkdir()

    reflector = build_reflector(
        ReflectorSection(role=editor_section(tmp_path), environment_docs=None, probe=None),
        seed_root=seed,
        run_dir=tmp_path / "run",
        environ={},
        secrets=["run-secret-value"],
    )

    assert isinstance(reflector, SubprocessReflector)
    assert reflector.secrets == ("run-secret-value",)


# -- stderr capture without a probe (#200) ---------------------------------------

_ANSWER = "import json\nprint(json.dumps({'status':'declined','groups':[],'note':'ok'}))\n"
_SECRET = "run-secret-value"


def _reflector_log(tmp_path: Path) -> bytes:
    return (tmp_path / "run" / "reflector-logs" / "r01-x01.stderr").read_bytes()


def _subprocess_editor():
    import evolution.tree.roles.editor.subprocess_editor as module

    return module


async def test_reflector_whole_stderr_secret_is_redacted(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    reflector = replace(
        bound_reflector("import os\nos.write(2, b'before run-secret-value end\\n')\n" + _ANSWER),
        secrets=(_SECRET,),
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == b"before [REDACTED] end\n"


async def test_reflector_cancellation_does_not_flush_a_secret_prefix(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    marker = tmp_path / "pid"
    reflector = replace(
        bound_reflector(
            "import os,sys,time\n"
            "os.write(2, b'before run-secr')\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            args=(str(marker),),
        ),
        secrets=(_SECRET,),
    )

    task = asyncio.create_task(reflector.reflect(reflect_request))
    await _await_marker(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _reflector_log(tmp_path) == b"before [REDACTED]"


async def test_reflector_stderr_written_during_termination_is_redacted(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    marker = tmp_path / "pid"
    reflector = replace(
        bound_reflector(
            "import os,signal,sys,time\n"
            "def term(*_):\n"
            "    os.write(2, b'term run-secret-value\\n')\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            args=(str(marker),),
        ),
        secrets=(_SECRET,),
    )

    task = asyncio.create_task(reflector.reflect(reflect_request))
    await _await_marker(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _reflector_log(tmp_path) == b"term [REDACTED]\n"


async def test_reflector_stderr_is_redacted_when_the_exchange_fails(
    tmp_path: Path, bound_reflector, reflect_request, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evolution.tree.roles.reflector.subprocess_reflector as module

    marker = tmp_path / "pid"
    reflector = replace(
        bound_reflector(
            "import os,sys,time\n"
            "os.write(2, b'before run-secret-value end\\n')\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            args=(str(marker),),
        ),
        secrets=(_SECRET,),
    )

    async def failing(process, document):
        await _await_marker(marker)
        raise RuntimeError("exchange failed")

    monkeypatch.setattr(module, "_exchange", failing)

    with pytest.raises(RuntimeError, match="exchange failed"):
        await reflector.reflect(reflect_request)

    assert _reflector_log(tmp_path) == b"before [REDACTED] end\n"


async def test_reflector_empty_secret_set_logs_stderr_byte_for_byte(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    reflector = bound_reflector(f"import os\nos.write(2, bytes(range(256))*3)\n{_ANSWER}")

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == bytes(range(256)) * 3


async def test_reflector_large_stderr_before_reading_stdin_does_not_deadlock(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    reflector = replace(
        bound_reflector(
            "import json,os,sys\nos.write(2, b'x'*(1<<20))\nr=json.load(sys.stdin)\n" + _ANSWER
        ),
        secrets=(_SECRET,),
    )

    async with asyncio.timeout(20):
        result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == b"x" * (1 << 20)


class _ReflectorFullDiskSink:
    def write(self, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        pass


async def test_reflector_log_write_failure_keeps_the_valid_result(
    bound_reflector, reflect_request, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evolution.tree.roles.reflector.subprocess_reflector as module

    monkeypatch.setattr(module, "_open_private_log", lambda path: _ReflectorFullDiskSink())
    reflector = replace(
        bound_reflector("import os\nos.write(2, b'x'*(1<<20))\n" + _ANSWER),
        secrets=(_SECRET,),
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert result.note == "ok"


async def test_reflector_temp_file_failure_keeps_the_result_and_writes_the_fixed_line(
    tmp_path: Path, bound_reflector, reflect_request, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor_module = _subprocess_editor()

    def refuse(*args: object, **kwargs: object) -> object:
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(editor_module.tempfile, "TemporaryFile", refuse)
    reflector = bound_reflector("import os\nos.write(2, b'lost\\n')\n" + _ANSWER)

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == editor_module._STDERR_NOT_CAPTURED
    assert b"Too many" not in _reflector_log(tmp_path)


async def test_reflector_spawn_failure_closes_the_temp_file_and_the_log(
    bound_reflector, reflect_request, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evolution.tree.roles.reflector.subprocess_reflector as module
    from tests.tree.roles.editor.test_subprocess_editor import _RecordingLog

    editor_module = _subprocess_editor()
    created: list[object] = []
    real = editor_module.tempfile.TemporaryFile

    def recording(*args: object, **kwargs: object) -> object:
        created.append(real(*args, **kwargs))
        return created[-1]

    log = _RecordingLog()
    monkeypatch.setattr(editor_module.tempfile, "TemporaryFile", recording)
    monkeypatch.setattr(module, "_open_private_log", lambda path: log)

    async def refused(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", refused)
    reflector = bound_reflector(_ANSWER)

    with pytest.raises(OSError, match="temporarily unavailable"):
        await reflector.reflect(reflect_request)

    assert log.closed
    assert len(created) == 1 and created[0].closed


async def test_reflector_stderr_holder_adds_no_delay(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    from tests.tree.roles.editor.test_subprocess_editor import (
        HOLDER_BOUND,
        _gone,
        kill_holder,
        stderr_holder,
    )

    marker = tmp_path / "holder"
    reflector = bound_reflector(stderr_holder(_ANSWER), args=(str(marker),))

    try:
        started = time.monotonic()
        result = await reflector.reflect(reflect_request)
        elapsed = time.monotonic() - started
        assert not _gone(int(marker.read_text(encoding="utf-8")))
    finally:
        kill_holder(marker)

    assert result.status == "declined"
    assert elapsed < HOLDER_BOUND


async def test_reflector_stderr_holder_leaves_a_result_just_before_the_deadline_valid(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    from tests.tree.roles.editor.test_subprocess_editor import (
        EXITS_BEFORE_DEADLINE,
        NEAR_DEADLINE,
        _gone,
        exits_near_the_deadline,
        kill_holder,
        stderr_holder,
    )

    marker = tmp_path / "holder"
    exit_at = time.monotonic() + NEAR_DEADLINE - EXITS_BEFORE_DEADLINE
    reflector = bound_reflector(
        stderr_holder(exits_near_the_deadline(_ANSWER)),
        args=(str(marker), str(exit_at)),
    )

    try:
        # The deadline owner cancels this coroutine at the deadline.
        async with asyncio.timeout(NEAR_DEADLINE):
            result = await reflector.reflect(reflect_request)
        assert not _gone(int(marker.read_text(encoding="utf-8")))
    finally:
        kill_holder(marker)

    assert result.status == "declined"


async def test_reflector_stderr_written_by_a_holder_after_the_copy_is_not_in_the_log(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    from tests.tree.roles.editor.test_subprocess_editor import (
        HOLDER_LOG,
        SECRET_AFTER_COPY,
        SECRET_BEFORE_COPY,
        kill_holder,
        stderr_holder,
    )

    marker = tmp_path / "holder"
    reflector = replace(
        bound_reflector(
            stderr_holder(_ANSWER, first=SECRET_BEFORE_COPY, then=SECRET_AFTER_COPY),
            args=(str(marker),),
        ),
        secrets=(_SECRET,),
    )

    try:
        result = await reflector.reflect(reflect_request)
        Path(f"{marker}.go").touch()
        await _await_marker(Path(f"{marker}.done"))
    finally:
        kill_holder(marker)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == HOLDER_LOG


# -- the probed path -------------------------------------------------------------


async def test_probed_reflector_stderr_is_redacted_with_the_run_secrets(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    reflector = replace(
        bound_reflector("import os\nos.write(2, b'before run-secret-value end\\n')\n" + _ANSWER),
        secrets=(_SECRET,),
    )
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None),
    )

    result = await reflector.reflect(request)

    assert result.status == "declined"
    assert _reflector_log(tmp_path) == b"before [REDACTED] end\n"


async def test_cancelled_probed_reflect_reports_nothing_to_the_loop_exception_handler(
    tmp_path: Path, bound_reflector, reflect_request
) -> None:
    marker = tmp_path / "pid"
    reflector = bound_reflector(
        "import os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(60)\n",
        args=(str(marker),),
    )
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None),
    )
    reports: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        task = asyncio.create_task(reflector.reflect(request))
        await _await_marker(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)  # a report scheduled by the cleanup has run by now
    finally:
        loop.set_exception_handler(None)

    assert reports == []
