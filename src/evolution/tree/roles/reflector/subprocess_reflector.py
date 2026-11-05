"""The declared-program `CandidateReflector`: binding twin of `SubprocessEditor`.

`reflect` mirrors `SubprocessEditor.edit` exactly: the same one-segment id
check (here on `request.reflection_id`), the same pre- and post-run
`CommandBinding.verify()`, the same private stderr sink opened by
`open_private_file`'s proof, the same spawn (`start_new_session=True`, a pipe
each way), the same `_exchange`/cancellation/kill protocol, and the same
five-way failure classification -- all imported from `subprocess_editor`
rather than copied, so the two kinds' process handling stays one
implementation. Only the request document, the result shape and the result
line's key set differ: `reflect_request/1` instead of `edit_request/1`, and
`{"status", "groups", "note"}` (`status` one of `reflected`/`declined`/
`failed`) instead of the editor's `{"status", "note"}`.

The workspace a Reflector reads is never the live Candidate: the caller
projects a read-only copy (`editor_workspace.project` + `freeze_projection`,
own test fixture) and this module never writes to it or to anything
outside `log_root`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.command_binding import CommandBinding, CommandChanged
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.editor.subprocess_editor import (  # noqa: PLC2701
    KILL_GRACE_SECONDS,
    MAX_NOTE_BYTES,
    _exchange,
    _open_private_log,
    _terminate_group,
    _terminate_shielded,
)
from evolution.tree.roles.identities import ReflectorIdentity
from evolution.tree.roles.reflection_reader import ReaderIntegrityError
from evolution.tree.roles.reflection_role_helpers import (
    groups_without_probe_refs,
    install_helpers,
    probe_log_secrets,
    probe_opening,
)
from evolution.tree.roles.reflector.reflect_brief import (
    AdaptiveReflectBrief,
    reflect_brief_payload,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_budget import FirstRequest, document_first_request
from evolution.tree.roles.reflector_contracts import (
    REFLECT_REQUEST_VERSION,
    GroupError,
    ProbeAccess,
    ReflectRequest,
    ReflectResult,
    adaptive_group_schema,
    check_adaptive_group_bounds,
    drop_empty_unknown_keys,
)
from evolution.tree.roles.workspace import bounded_note

__all__ = [
    "KILL_GRACE_SECONDS",
    "MAX_NOTE_BYTES",
    "SubprocessReflector",
]

_STATUSES = frozenset({"reflected", "declined", "failed"})


@dataclass(frozen=True)
class SubprocessReflector:
    """A `CandidateReflector` that is one operator-declared program per reflection."""

    control_prefix: ClassVar[str] = ".evolution"
    binding: CommandBinding
    log_root: Path
    #: The accepted hygiene findings' instance digests (PDR-0010), as for
    #: `SubprocessEditor`; empty by default.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    identity: ReflectorIdentity = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_root", Path(self.log_root).resolve())
        object.__setattr__(
            self, "identity", ReflectorIdentity("subprocess", self.binding.identity_digest())
        )

    def measure_first_request(
        self,
        brief: Any,
        *,
        editable_paths: Sequence[str],
        probe: ProbeAccess | None,
        editor_max_changed_files: int | None,
    ) -> FirstRequest:
        """Record sizes without enforcing a first-request budget."""

        return document_first_request(
            brief,
            editable_paths=editable_paths,
            probe=probe,
            editor_max_changed_files=editor_max_changed_files,
            system_prompt=None,
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Run the declared program for one reflection and classify its one result line."""

        if request.reflection_id != Path(request.reflection_id).name or request.reflection_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("reflection_id must be one path segment")
        try:
            self.binding.verify()
        except CommandChanged:
            return ReflectResult("failed", (), "command_changed", code="command_changed")
        self._admit(self.log_root)
        log_root = ensure_private_directory(self.log_root)
        helpers = None
        if isinstance(request.evidence, AdaptiveReflectBrief):
            if request.reader is None:
                return ReflectResult("failed", (), "reader_unavailable", code="reader_unavailable")
            helpers = install_helpers(
                request.workspace / self.control_prefix / "readers", request.reader
            )
        document = _request_document(
            request, readers=None if helpers is None else helpers.descriptors
        )
        try:
            stderr = _open_private_log(log_root / f"{request.reflection_id}.stderr")
        except WorkspaceError as exc:
            # Mirrors `SubprocessEditor.edit`: nothing was spawned, so the
            # in-process editors' own `workspace_changed` note applies here too.
            return ReflectResult(
                "failed", (), bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.binding.spawn_argv(),
                cwd=self.binding.cwd,
                env=dict(self.binding.child_env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if request.probe is not None else stderr,
                start_new_session=True,
            )
        except BaseException:
            stderr.close()
            raise
        try:
            try:
                stdout, oversized = (
                    await _probe_exchange(
                        process, document, stderr, probe_log_secrets(request.probe)
                    )
                    if request.probe is not None
                    else await _exchange(process, document)
                )
            except asyncio.CancelledError as cancellation:
                await _terminate_shielded(process, cancellation)
                raise
            except BaseException:
                await _terminate_group(process)
                raise
        finally:
            stderr.close()
        result = _classify(process.returncode, stdout, oversized)
        if helpers is not None:
            try:
                helpers.verify()
            except ReaderIntegrityError:
                return ReflectResult("failed", (), "helper_changed", code="helper_changed")
            try:
                # `helpers` exist only for an adaptive brief (with its round limit).
                checked = result.groups
                if request.normalize_empty_keys:
                    checked = drop_empty_unknown_keys(
                        checked,
                        adaptive_group_schema(
                            probes=request.probe is not None,
                            documents=request.evidence.document_ids is not None,
                        ),
                    )
                check_adaptive_group_bounds(checked, request.evidence.group_text_chars)
                groups_without_probe_refs(checked, probe=request.probe is not None)
            except GroupError:
                return ReflectResult("failed", (), "invalid_result", code="invalid_result")
        try:
            self.binding.verify()
        except CommandChanged:
            return ReflectResult("failed", (), "command_changed", code="command_changed")
        return result

    def _admit(self, log_root: Path) -> None:
        """Re-admit the reflector's output root at the point of use.

        Same rationale as `SubprocessEditor._admit`: the import is deferred to
        the call because `bindings` imports this module, and an accepted finding
        (PDR-0010) passes rather than refusing.
        """

        from evolution.tree.execution.output_admission import (
            admit_output_root,  # circular at module scope
        )

        admit_output_root(log_root, admitted=self.admitted_hygiene)


def _request_document(
    request: ReflectRequest, *, readers: Mapping[str, Sequence[str]] | None = None
) -> bytes:
    """The one `reflect_request/1` document, and nothing else, the child reads."""

    probe = request.probe
    docs = request.environment_docs
    document: dict[str, Any] = {
        "version": REFLECT_REQUEST_VERSION,
        "reflection_id": request.reflection_id,
        "round": request.round,
        "workspace": str(request.workspace),
        "editable_paths": list(request.editable_paths),
        "deadline_seconds": float(request.deadline_seconds),
        # Only the legacy request carries rendered documentation bodies.
        **(
            {}
            if isinstance(request.evidence, AdaptiveReflectBrief)
            else {"environment_docs": None if docs is None else docs.text}
        ),
        "probe": None
        if probe is None
        else {
            "url": probe.url,
            "budget": probe.budget,
            "methods": list(probe.methods),
            "description": probe.description,
        },
        "evidence": reflect_brief_payload(request.evidence),
    }
    if isinstance(request.evidence, AdaptiveReflectBrief):
        document["version"] = "reflect_request/2"
        document["opening"] = render_reflect_opening(
            request.evidence,
            (),
            request.editable_paths,
            editor_max_changed_files=request.editor_max_changed_files,
        ) + probe_opening(request.probe, typed_body=request.evidence.version == "reflect_brief/6")
        document["readers"] = {name: list(argv) for name, argv in (readers or {}).items()}
        document["reader_arguments"] = (
            "Append one JSON object argument to the selected argv; explicit offsets continue a page, default zero."
        )
    return (json.dumps(document, ensure_ascii=False) + "\n").encode()


def _classify(returncode: int | None, stdout: bytes, oversized: bool) -> ReflectResult:
    """The same four failure classes `subprocess_editor._classify` uses, over a
    three-way status result instead of the editor's two-way one."""

    if returncode != 0:
        return ReflectResult("failed", (), "exit_status", code="exit_status")
    if oversized:
        return ReflectResult("failed", (), "result_too_large", code="result_too_large")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return ReflectResult("failed", (), "bad_result", code="bad_result")
    line = next((row for row in reversed(text.splitlines()) if row.strip()), None)
    if line is None:
        return ReflectResult("failed", (), "no_result", code="no_result")
    return _result_line(line)


def _result_line(line: str) -> ReflectResult:
    """`{"status":..., "groups":..., "note":...}` and nothing else, or `bad_result`."""

    try:
        payload = json.loads(line)
    except ValueError:
        return ReflectResult("failed", (), "bad_result", code="bad_result")
    if not isinstance(payload, dict) or set(payload) != {"status", "groups", "note"}:
        return ReflectResult("failed", (), "bad_result", code="bad_result")
    status = payload["status"]
    groups = payload["groups"]
    note = payload["note"]
    if status not in _STATUSES or not isinstance(groups, list) or type(note) is not str:
        return ReflectResult("failed", (), "bad_result", code="bad_result")
    if len(note.encode()) > MAX_NOTE_BYTES:
        return ReflectResult("failed", (), "bad_result", code="bad_result")
    if status == "failed":
        return ReflectResult(status, tuple(groups), note, code="program_failed")
    return ReflectResult(status, tuple(groups), note)


class _SecretStream:
    """Bounded overlap with union masking, including truncated final prefixes."""

    def __init__(self, secrets: Sequence[str]) -> None:
        self.secrets = tuple(secret.encode() for secret in secrets if secret)
        self.overlap = max((len(secret) for secret in self.secrets), default=1) - 1
        self.pending = b""
        self.mask = bytearray()
        self.redacting = False

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        self.pending += chunk
        self.mask.extend(b"\0" * len(chunk))
        for secret in self.secrets:
            start = 0
            while (start := self.pending.find(secret, start)) >= 0:
                self.mask[start : start + len(secret)] = b"\1" * len(secret)
                start += 1
            if final:
                for length in range(1, min(len(secret), len(self.pending) + 1)):
                    if self.pending.endswith(secret[:length]):
                        self.mask[-length:] = b"\1" * length
        count = len(self.pending) if final else max(0, len(self.pending) - self.overlap)
        output = bytearray()
        for index in range(count):
            if self.mask[index]:
                if not self.redacting:
                    output.extend(b"[REDACTED]")
                self.redacting = True
            else:
                self.redacting = False
                output.append(self.pending[index])
        self.pending = self.pending[count:]
        del self.mask[:count]
        return bytes(output)


async def _redact_stderr(
    stream: asyncio.StreamReader, sink: IO[bytes], secrets: Sequence[str]
) -> None:
    redactor = _SecretStream(secrets)
    failure: OSError | None = None
    try:
        while chunk := await stream.read(8192):
            if failure is None:
                try:
                    sink.write(redactor.feed(chunk))
                except OSError as exc:
                    # Preserve the failure while discarding future bytes so a
                    # full pipe cannot prevent child exit or cancellation cleanup.
                    failure = exc
    finally:
        # Cancellation/cutoff cannot flush an unresolved capability prefix raw.
        if failure is None:
            sink.write(redactor.feed(b"", final=True))
    if failure is not None:
        raise failure


async def _probe_exchange(
    process: asyncio.subprocess.Process, document: bytes, stderr: IO[bytes], secrets: Sequence[str]
) -> tuple[bytes, bool]:
    assert process.stderr is not None
    exchange = asyncio.create_task(_exchange(process, document))
    drain = asyncio.create_task(_redact_stderr(process.stderr, stderr, secrets))
    combined = asyncio.gather(exchange, drain)
    try:
        # Keep every pipe flowing until process-group termination completes.
        await asyncio.shield(combined)
        return exchange.result()
    except asyncio.CancelledError as cancellation:
        await _terminate_shielded(process, cancellation)
        raise
    except BaseException:
        await _terminate_group(process)
        raise
    finally:
        for task in (exchange, drain):
            if not task.done():
                task.cancel()
        await asyncio.gather(exchange, drain, return_exceptions=True)
        if combined.done() and not combined.cancelled():
            combined.exception()
