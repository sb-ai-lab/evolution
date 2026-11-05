"""The declared-program `CandidateEditor`: one child, one document, one line.

`SubprocessEditor` is W1's only `editor.kind`. It runs a program the operator
declared in the manifest, bound once by a :class:`CommandBinding` (the absolute
executable, the resolved program bytes, `cwd` and the complete retained child
environment), hands it one `edit_request/1` JSON document on stdin and reads
one JSON result line from stdout. It has one responsibility: spawn, feed, read,
classify, kill.

What core hands the program is exactly the request document: the redacted
`edit_brief/1` payload C1 hashes, the workspace path, the writable set and the
deadline. No `private`, no `oracle`, no target address, no provider secret --
the child environment is `minimal_child_env(allow=allowed_env)`, sealed at bind
time. Stderr is redirected to a private `<log_root>/<attempt_id>.stderr` file
and never enters the note or any journal payload; that sink is opened by the
one open-and-verify `editor_workspace` uses for the attempt transcript, so an
entry planted at the predictable pathname is `failed`/`workspace_changed`
before the child is spawned rather than a blocked open or a diverted stream.

Through family 13 the workspace path is the real Attempt workspace, which the
program edits in place. In family 14 (`EditRequest.projection`, F0 R65) it is
a private sparse role projection holding only the visible set, and the
program's changes reach the Candidate only through `editor_workspace.admit`
and `publish`, as the in-process editors' do. The real path is no longer in
the document, but the program is operator-trusted, not sandboxed, and may
still guess it.

The execution seal is verified twice, before the spawn and after the exit, so a
program or environment replaced around the run never returns `edited` for a
workspace it touched; either mismatch is `EditResult("failed",
"command_changed")`. In family 14 it is verified once more, after admission
and immediately before publication.

The editor runs no timer of its own: the deadline is C1's (ADR-0001), enforced
by `campaign_editor.run_editor`, which cancels this coroutine. On cancellation
the child's whole process group -- its own session, so an ordinary fork dies
with it -- gets `SIGTERM`, then `SIGKILL` after `KILL_GRACE_SECONDS`, and the
cancellation is re-raised. The kill is shielded, so it completes even while the
task is being cancelled. A descendant that starts its own session is outside
the group and is best-effort only: this is a bounded-termination guarantee, not
an OS sandbox (the operator trust model and ADR-0005's containment scope).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.command_binding import CommandBinding, CommandChanged
from evolution.tree.execution.private_files import WorkspaceError, open_private_file
from evolution.tree.roles.editor_contracts import (
    PROJECTION_REFUSED_NOTE,
    PUBLICATION_REFUSED_NOTE,
    EditRequest,
    EditResult,
    RoleProjection,
    brief_payload,
)
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.workspace import (
    MAX_CAPTURE_BYTES,
    ActorViolation,
    Budgets,
    Projection,
    admit,
    bounded_note,
    discard,
    project,
    publish,
)

REQUEST_VERSION = "edit_request/1"
MAX_STDOUT_BYTES = 1 << 20
MAX_NOTE_BYTES = 4096
KILL_GRACE_SECONDS = 5.0

_DRAIN_CHUNK_BYTES = 1 << 16
_STATUSES = frozenset({"edited", "declined", "failed"})

#: Family 14's admission caps for the `command` Editor (H11 routine choice 6):
#: the manifest's `command` section declares no budgets, so admission takes the
#: capture ceilings rather than inventing a new size limit.
PROJECTED_BUDGETS = Budgets(None, MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES)

__all__ = [
    "KILL_GRACE_SECONDS",
    "MAX_NOTE_BYTES",
    "MAX_STDOUT_BYTES",
    "PROJECTED_BUDGETS",
    "REQUEST_VERSION",
    "SubprocessEditor",
]


@dataclass(frozen=True)
class SubprocessEditor:
    """A `CandidateEditor` that is one operator-declared program per attempt."""

    binding: CommandBinding
    log_root: Path
    #: Instance digests of the run-directory hygiene findings the operator has
    #: accepted (PDR-0010); empty by default, so the point-of-use re-check below
    #: refuses exactly what it refused before a decision existed.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    identity: EditorIdentity = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_root", Path(self.log_root).resolve())
        object.__setattr__(
            self, "identity", EditorIdentity("subprocess", self.binding.identity_digest())
        )

    async def edit(self, request: EditRequest) -> EditResult:
        """Run the declared program for one attempt and classify its one result line."""

        if request.attempt_id != Path(request.attempt_id).name or request.attempt_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("attempt_id must be one path segment")
        try:
            self.binding.verify()
        except CommandChanged:
            return EditResult("failed", "command_changed", code="command_changed")
        self._admit(self.log_root)
        log_root = ensure_private_directory(self.log_root)
        if request.projection is not None:
            return await self._edit_projected(request, request.projection, log_root)
        document = _request_document(request, request.workspace)
        try:
            stderr = _open_private_log(log_root / f"{request.attempt_id}.stderr")
        except WorkspaceError as exc:
            # W1's own note vocabulary has no class for a sink that is not the
            # private file it must be, so the attempt takes the in-process
            # editors' `workspace_changed`: nothing was spawned and nothing was
            # written, and the entry at the pathname is not this run's.
            return EditResult(
                "failed", bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        result = await self._run(document, stderr)
        try:
            self.binding.verify()
        except CommandChanged:
            return EditResult("failed", "command_changed", code="command_changed")
        return result

    async def _edit_projected(
        self, request: EditRequest, role: RoleProjection, log_root: Path
    ) -> EditResult:
        """Family 14: project, run, admit and publish like the other kinds.

        The program is handed a private sparse projection of the Attempt
        workspace -- the visible set only -- and never the real path; its
        changes reach the Candidate only through `admit` and `publish`, which
        stages onto the full capture and keeps every hidden byte and mode. A
        refusal of the real workspace can name a hidden entry, so its note is
        fixed text; an `ActorViolation` names only what the program itself
        created or changed. Only an `edited` outcome is admitted, and nothing
        is published unless the seal still holds after admission. The
        projection is discarded whatever happens, cancellation included.
        """

        try:
            stderr = _open_private_log(log_root / f"{request.attempt_id}.stderr")
        except WorkspaceError as exc:
            return EditResult(
                "failed", bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        try:
            projection = project(
                request.workspace, visible=role.visible, expected_digest=role.expected_digest
            )
        except WorkspaceError:
            stderr.close()
            return EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
        except BaseException:
            stderr.close()
            raise
        try:
            try:
                document = _request_document(request, projection.root)
            except BaseException:
                stderr.close()
                raise
            result = await self._run(document, stderr)
            try:
                self.binding.verify()
            except CommandChanged:
                return EditResult("failed", "command_changed", code="command_changed")
            if result.status != "edited":
                return result
            return self._publish(request, projection, result)
        finally:
            discard(projection)

    def _publish(
        self, request: EditRequest, projection: Projection, result: EditResult
    ) -> EditResult:
        """Admit an `edited` projection, re-verify the seal, then publish onto the full capture.

        Any other failure here -- an `OSError` while staging, say -- is the
        `exception:<Name>` class the in-process editors give it, and its text,
        which can name a staged hidden path, never reaches the note.
        """

        try:
            changes = admit(
                projection, writable_paths=request.writable_paths, budgets=PROJECTED_BUDGETS
            )
            self.binding.verify()
            # Unconditionally, as `chat` does: `publish` is the one step that
            # re-captures the real workspace and compares it with the capture,
            # so an empty projection diff must not skip it -- a program that
            # wrote straight into the real workspace is refused here.
            publish(changes, projection)
        except ActorViolation as exc:
            return EditResult("failed", bounded_note(f"actor_violation {exc}"), code=exc.code)
        except CommandChanged:
            return EditResult("failed", "command_changed", code="command_changed")
        except WorkspaceError:
            return EditResult("failed", PUBLICATION_REFUSED_NOTE, code="workspace_changed")
        except Exception as exc:
            return EditResult("failed", f"exception:{type(exc).__name__}", code="exception")
        return result

    async def _run(self, document: bytes, stderr: IO[bytes]) -> EditResult:
        """Spawn the program, exchange the document and its result line, and classify.

        `stderr` is closed here, after the child has exited, whatever happens.
        """

        try:
            process = await asyncio.create_subprocess_exec(
                *self.binding.spawn_argv(),
                cwd=self.binding.cwd,
                env=dict(self.binding.child_env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                start_new_session=True,
            )
        except BaseException:
            stderr.close()
            raise
        try:
            try:
                stdout, oversized = await _exchange(process, document)
            except asyncio.CancelledError as cancellation:
                await _terminate_shielded(process, cancellation)
                raise
            except BaseException:
                await _terminate_group(process)
                raise
        finally:
            # The child holds its own dup of the descriptor; this closes ours,
            # after the process has exited, so no pipe or file is left open.
            stderr.close()
        return _classify(process.returncode, stdout, oversized)

    def _admit(self, log_root: Path) -> None:
        """Re-admit the editor's output root at the point of use.

        W1 admitted this root before the campaign started; a root replaced
        between then and now -- by a symlink, by a file, by a group-readable
        directory -- must not receive an attempt's stderr, so the same check
        runs again immediately before the first write of every `edit`.
        `admit_output_root` lives in `bindings`, which imports this module, so
        the import is deferred to the call rather than made at module scope. A
        finding the operator accepted at admission is carried here as
        `admitted_hygiene` and passes; the return value is dropped, because the
        decision was journaled once, when it was made.
        """

        from evolution.tree.execution.output_admission import (
            admit_output_root,  # circular at module scope
        )

        admit_output_root(log_root, admitted=self.admitted_hygiene)


async def _exchange(process: asyncio.subprocess.Process, document: bytes) -> tuple[bytes, bool]:
    """Write the request and read the capped stdout concurrently, then wait for the exit.

    The protocol is one document in and one result line out, but the two
    directions cannot be scheduled one after the other: a request larger than
    the pipe buffer (a full corpus's `public` payloads pass 64 KiB easily) plus
    a child that writes before it reads is a deadlock -- the parent blocked in
    `drain()`, the child blocked on a full stdout pipe -- that would burn the
    whole attempt and be journaled as C1's `timeout` rather than as what it is.
    Both directions therefore run as tasks; whichever way the exchange ends,
    both are settled before this returns, so no task is left pending.
    """

    writer = asyncio.ensure_future(_write_request(process, document))
    reader = asyncio.ensure_future(_read_stdout(process))
    try:
        await asyncio.gather(writer, reader)
    except BaseException:
        for task in (writer, reader):
            task.cancel()
        await asyncio.gather(writer, reader, return_exceptions=True)
        raise
    stdout, oversized = reader.result()
    await process.wait()
    return stdout, oversized


async def _write_request(process: asyncio.subprocess.Process, document: bytes) -> None:
    stdin = process.stdin
    if stdin is None:  # pragma: no cover - stdin is always a pipe here
        raise RuntimeError("editor stdin is not a pipe")
    try:
        # A child that exits without reading breaks the pipe; that is its
        # result contract's problem (`exit_status`, `no_result`), not an error
        # of this call.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(document)
            await stdin.drain()
    finally:
        stdin.close()


async def _read_stdout(process: asyncio.subprocess.Process) -> tuple[bytes, bool]:
    """Up to `MAX_STDOUT_BYTES` of stdout, and whether the child wrote more."""

    stream = process.stdout
    if stream is None:  # pragma: no cover - stdout is always a pipe here
        raise RuntimeError("editor stdout is not a pipe")
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_STDOUT_BYTES:
        chunk = await stream.read(MAX_STDOUT_BYTES + 1 - total)
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        total += len(chunk)
    # Over the cap: the bytes are already refused, but keep reading and
    # discarding until EOF so the child never blocks on a full pipe.
    while await stream.read(_DRAIN_CHUNK_BYTES):
        pass
    return b"".join(chunks), True


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    """End the child's whole process group: `SIGTERM`, then `SIGKILL` after the grace."""

    if process.returncode is not None:
        return
    _signal_group(process.pid, signal.SIGTERM)
    try:
        async with asyncio.timeout(KILL_GRACE_SECONDS):
            await process.wait()
        return
    except TimeoutError:
        pass
    _signal_group(process.pid, signal.SIGKILL)
    await process.wait()


async def _terminate_shielded(
    process: asyncio.subprocess.Process, cancellation: asyncio.CancelledError
) -> None:
    """Terminate the group even though this task is already being cancelled."""

    killer = asyncio.ensure_future(_terminate_group(process))
    while not killer.done():
        try:
            await asyncio.shield(killer)
        except asyncio.CancelledError:
            continue
    failure = killer.exception()
    if failure is not None:  # pragma: no cover - the kill path raises nothing
        cancellation.add_note(f"Editor cleanup failure: {type(failure).__name__}")


def _signal_group(pid: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, number)


def _request_document(request: EditRequest, workspace: Path) -> bytes:
    """The one `edit_request/1` document, and nothing else, the child reads.

    `workspace` is the real Attempt workspace for families up to 13 and the
    private role projection's root in family 14; the keys never change.
    """

    document: dict[str, Any] = {
        "version": REQUEST_VERSION,
        "attempt_id": request.attempt_id,
        "round": request.round,
        "workspace": str(workspace),
        "writable_paths": list(request.writable_paths),
        "deadline_seconds": float(request.deadline_seconds),
        "brief": brief_payload(request.brief),
    }
    return (json.dumps(document, ensure_ascii=False) + "\n").encode()


def _open_private_log(path: Path) -> IO[bytes]:
    """An owner-only, proved stderr sink for one attempt.

    The attempt pathname is predictable and the run directory may already hold
    entries on resume, so the descriptor is proved rather than trusted:
    `O_NOFOLLOW` alone rejects a symlink but neither a FIFO planted at the name,
    which would block this open before C1's deadline can fire, nor a hard link
    to a file outside the run directory, which would then receive the child's
    stderr. `open_private_file` is the same open-and-verify the attempt
    transcript uses; a refusal is `WorkspaceError` and the caller classifies it.
    """

    descriptor = open_private_file(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        refusal=f"attempt stderr is not a private regular file: {path.name}",
    )
    return os.fdopen(descriptor, "wb", buffering=0)


def _classify(returncode: int | None, stdout: bytes, oversized: bool) -> EditResult:
    """The contract result mapping, over an exited child's status and stdout."""

    if returncode != 0:
        return EditResult("failed", "exit_status", code="exit_status")
    if oversized:
        return EditResult("failed", "result_too_large", code="result_too_large")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return EditResult("failed", "bad_result", code="bad_result")
    line = next((row for row in reversed(text.splitlines()) if row.strip()), None)
    if line is None:
        return EditResult("failed", "no_result", code="no_result")
    return _result_line(line)


def _result_line(line: str) -> EditResult:
    """`{"status":..., "note":...}` and nothing else, or `bad_result`."""

    try:
        payload = json.loads(line)
    except ValueError:
        return EditResult("failed", "bad_result", code="bad_result")
    if not isinstance(payload, dict) or set(payload) != {"status", "note"}:
        return EditResult("failed", "bad_result", code="bad_result")
    status = payload["status"]
    note = payload["note"]
    if status not in _STATUSES or type(note) is not str:
        return EditResult("failed", "bad_result", code="bad_result")
    if len(note.encode()) > MAX_NOTE_BYTES:
        return EditResult("failed", "bad_result", code="bad_result")
    if status == "failed":
        return EditResult(status, note, code="program_failed")
    return EditResult(status, note)
