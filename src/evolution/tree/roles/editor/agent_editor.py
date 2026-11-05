"""A Candidate editor that is one coding-agent CLI run per attempt.

One attempt is: verify the two seals, project the Candidate privately with a
`.evolution/` control plane (in family 14 a sparse role projection of the
visible set, `EditRequest.projection`), write `EDIT_REQUEST.json` and the
rendered evidence catalog into it and mark them as the editor's own, then run the CLI
once over the projection through a backend built for that call alone. What
comes back is diffed against the projection's baseline and handed to the
shared admission; an empty diff is `declined`. The changed files are
self-checked in the projection, and a failing check buys exactly one repair
call, framed by `.evolution/VALIDATION.json`. A changed-file budget breach buys
that same repair call instead of ending the attempt (ADR-0037 D4): the report
then names the budget first, the files that differ, and the self-check of the
breached tree. In family 10 an empty diff -- after the first call or after the
repair -- buys one continuation call too (PDR-0019), and a breach on the
continuation spends the repair if it is still unused; an attempt makes at most
three calls. `max_turns` is then applied as an
acceptance limit over the turns every call reported -- no CLI is given a turn
budget on its command line, so the campaign can stop one only by the deadline
-- the seals are verified again, and `publish` writes the real Candidate,
atomically or not at all. That second verification is not the publishing
path's alone: a `declined` attempt, every `failed` class and the exception
fallback are checked too, before the result is returned, so a prompt or an
executable replaced under a run that changed nothing is still refused. Nothing else in this module touches the Candidate:
the capture, the guardrails, the staging and the swap all live in
`editor_workspace`, and the brief seam in `editor_brief`.

The trust model is the `subprocess` editor's. The agent CLI is a program the
operator declared in a trusted manifest and Evolution seals its bytes, but it
runs under the operator's account with the child environment the binding
computed, and Evolution is not an OS sandbox: in `safe` mode the CLI's own
sandbox confines its writes to the projection and the diff-back publishes only
what admission accepts, while in `unsafe` mode approvals and sandboxing are off
by the operator's declaration. The editor is therefore the authority for what
the CLI receives -- every backend knob is passed explicitly, so no campaign
semantic comes from `os.environ` -- and for what leaves the run: the attempt
transcript records the opening, the response, the reported usage and the
backend's diagnostics through the run's secret set, to which the sealed
program's path is added, so neither a forwarded credential nor the operator's
filesystem layout can reach the log through an echoed invocation or a stderr
tail. The child environment's values are never recorded at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evolution.llm.agent_backend import AgentBackend, reports_tool_turns
from evolution.prompts import TREE_EDITOR_CONTINUATION
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.editor.editor_brief import EvidenceRecord, render_catalog, render_opening
from evolution.tree.roles.editor_contracts import (
    PROJECTION_REFUSED_NOTE,
    PUBLICATION_REFUSED_NOTE,
    EditRequest,
    EditResult,
    brief_payload,
    outcome_class,
)
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.seals import FileSeal, SealChanged, agent_identity
from evolution.tree.roles.workspace import (
    ActorViolation,
    AttemptLog,
    BudgetViolation,
    Change,
    ControlCollision,
    Projection,
    WorkspaceChanged,
    admit,
    bounded_note,
    changed_file_paths,
    discard,
    open_attempt_log,
    project,
    publish,
    self_check,
)

CONTROL_PREFIX = ".evolution"
"""The projection's control plane: the editor's own, never Candidate content."""

REQUEST_VERSION = "edit_request/1"

_REQUEST_FILE = "EDIT_REQUEST.json"
_VALIDATION_FILE = "VALIDATION.json"
_EVIDENCE_DIR = "evidence"
_EVIDENCE_INDEX = "index.json"

Outcome = tuple[Literal["edited", "declined", "failed"], str, bool]
"""The `EditResult` status, its note, and whether a family-10 continuation turn was
taken (`EditResult.continued`; `False` for every editor without `role_resilience`)."""

#: ADR-0037 D4 via the D1a channel: the packaged prompt (unchanged, its digest is
#: identity) says VALIDATION.json lists files that no longer parse or compile.
AGENT_VALIDATION_LINE = (
    "VALIDATION.json may also report the file budget: an issue that says you changed "
    "more files than this budget allows, with the changed files in `changed_paths`; "
    "remove files you created or write updated files' original bytes back until the count "
    "fits; a deleted file cannot be restored this way."
)
_NO_CHANGE = "no change proposed"
_PUBLISHED = "edit published"


class _Refused(RuntimeError):
    """An attempt that ends in `failed` with a stable class as the note's first token."""

    def __init__(self, klass: str, detail: str = "", *, code: str | None = None) -> None:
        super().__init__(detail)
        self.klass = klass
        self.detail = detail
        self.code = code or klass

    @property
    def note(self) -> str:
        return f"{self.klass} {self.detail}".strip()


@dataclass(frozen=True)
class AgentCliEditor:
    """A `CandidateEditor` that is one agent-CLI run, plus at most one repair."""

    section: AgentCliEditorSection
    program: FileSeal
    prompt: FileSeal
    child_env: Mapping[str, str]
    log_root: Path
    secrets: tuple[str, ...]
    backend_factory: Callable[..., Any] | None = None
    #: Instance digests of the run-directory hygiene findings the operator
    #: has accepted (PDR-0010), handed to `open_attempt_log` with the
    #: `log_root` it re-admits; empty by default.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    #: `/11` (PDR-0019, ADR-0023): true for a family-10 run. `_attempt` may
    #: then take one continuation turn after an empty-diff decline, and the
    #: continuation text's SHA-256 enters the identity digest.
    role_resilience: bool = field(default=False, kw_only=True)
    family15: bool = field(default=False, kw_only=True)
    identity: EditorIdentity = field(init=False)

    @property
    def max_changed_files(self) -> int | None:
        """ADR-0037 D10: the file budget the campaign states to the family-14 Reflector."""

        return self.section.budgets.max_changed_files

    def __post_init__(self) -> None:
        if self.program.path is None:
            raise ValueError("the agent CLI executable must be sealed as a file")
        if self.section.max_turns is not None and not reports_tool_turns(self.section.model):
            # The loader refuses this too; refusing it here as well
            # keeps an inert acceptance limit out of every other caller.
            raise ValueError("max_turns needs a CLI that reports turn telemetry")
        object.__setattr__(self, "log_root", Path(self.log_root))
        continuation_sha256 = (
            hashlib.sha256(TREE_EDITOR_CONTINUATION.encode()).hexdigest()
            if self.role_resilience
            else None
        )
        object.__setattr__(
            self,
            "identity",
            agent_identity(
                self.section,
                self.prompt,
                self.program,
                continuation_sha256=continuation_sha256,
                family15=self.family15,
            ),
        )

    async def edit(self, request: EditRequest) -> EditResult:
        """Run one attempt: project, run the CLI, admit, self-check and publish."""

        if request.attempt_id != Path(request.attempt_id).name or request.attempt_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("attempt_id must be one path segment")
        started = time.monotonic()
        try:
            self._verify_seals()
        except _Refused as exc:
            return EditResult("failed", exc.note, code=exc.code)
        # The attempt log is opened before the projection, so its own refusal
        # -- an entry at the attempt pathname that is not a private regular
        # file -- has no log to be recorded in and is classified here, as the
        # workspace refusal it is.
        try:
            attempt_log = open_attempt_log(
                self.log_root,
                request.attempt_id,
                secrets=self._log_secrets(),
                admitted=self.admitted_hygiene,
            )
        except WorkspaceError as exc:
            return EditResult(
                "failed", bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        role = request.projection
        with attempt_log as log:
            try:
                # Family 14: the control-collision check still runs on the full
                # capture, so a hidden `.evolution/` entry is refused too.
                projection = project(
                    request.workspace,
                    control_prefix=CONTROL_PREFIX,
                    visible=None if role is None else role.visible,
                    expected_digest=None if role is None else role.expected_digest,
                )
            except ControlCollision as exc:
                log.record(_refusal("control_collision", str(exc)))
                return EditResult(
                    "failed", bounded_note(f"control_collision {exc}"), code="control_collision"
                )
            except WorkspaceError as exc:
                log.record(_refusal("workspace_changed", str(exc)))
                return EditResult(
                    "failed",
                    bounded_note(f"workspace_changed {exc}")
                    if role is None
                    # Family 14: a refusal of the real workspace can name a
                    # hidden entry; the transcript keeps the detail.
                    else PROJECTION_REFUSED_NOTE,
                    code="workspace_changed",
                )
            try:
                status, note, continued = await self._attempt(request, projection, log, started)
                return self._after_run(
                    EditResult(status, bounded_note(note), continued=continued), log
                )
            except _Refused as exc:
                log.record(_refusal(exc.klass, exc.detail))
                return self._after_run(
                    EditResult("failed", bounded_note(exc.note), code=exc.code), log
                )
            except ActorViolation as exc:
                log.record(_refusal("actor_violation", str(exc)))
                return self._after_run(
                    EditResult("failed", bounded_note(f"actor_violation {exc}"), code=exc.code),
                    log,
                )
            except WorkspaceChanged as exc:
                log.record(_refusal("workspace_changed", str(exc)))
                return self._after_run(
                    EditResult(
                        "failed",
                        bounded_note(f"workspace_changed {exc}")
                        if role is None
                        else PUBLICATION_REFUSED_NOTE,
                        code="workspace_changed",
                    ),
                    log,
                )
            except asyncio.CancelledError:
                # The backend has already killed the CLI's process group; the
                # projection goes with it and the Candidate was never written.
                raise
            except Exception as exc:
                log.record(
                    {"phase": "exception", "error_type": type(exc).__name__, "error": str(exc)}
                )
                return self._after_run(
                    EditResult(
                        "failed",
                        bounded_note(f"exception:{type(exc).__name__}"),
                        code="exception",
                    ),
                    log,
                )
            finally:
                discard(projection)

    def _after_run(self, result: EditResult, log: AttemptLog) -> EditResult:
        """Verify both seals after the attempt, whatever its outcome.

        The seals are verified "before and after every `edit`" (PDR-0005), not
        only before a publication: a prompt or a CLI executable rewritten under
        a run that declines or fails is a tampering the result must carry
        rather than mask. An `edited` outcome is not re-checked here because
        `_attempt`'s own `_verify_seals` stands immediately before `publish`,
        which keeps publication strictly after the check; every other way an
        attempt ends -- `declined`, each `failed` class and the exception
        fallback -- passes through here. The replaced result keeps the outcome
        it overrode in its detail.
        """

        if result.status == "edited":
            return result
        try:
            self._verify_seals()
        except _Refused as exc:
            replaced = outcome_class(result)
            if replaced == exc.klass:
                return result
            log.record(_refusal(exc.klass, f"after {replaced}"))
            return EditResult(
                "failed", bounded_note(f"{exc.klass} (after {replaced})"), code=exc.klass
            )
        return result

    # ----------------------------------------------------------------------
    # The attempt
    # ----------------------------------------------------------------------

    async def _attempt(
        self, request: EditRequest, projection: Projection, log: AttemptLog, started: float
    ) -> Outcome:
        catalog = render_catalog(request.brief)
        budget = self.section.budgets.max_changed_files
        opening = render_opening(
            request.brief, catalog, request.writable_paths, max_changed_files=budget
        )
        if budget is not None:
            opening = f"{opening}\n{AGENT_VALIDATION_LINE}"
        self._write_control_plane(projection, request, opening, catalog)
        projection.mark_control()

        message, turns = await self._call(request, projection, log, started, call=1, prompt=opening)
        limit = self.section.max_turns
        continued = False
        calls = 1
        # Admission first, even for an empty diff: `changes()` is blind to the
        # control plane, so only `admit` can see a CLI that wrote nothing but
        # `.evolution/`, and it returns exactly the changes `changes()` would.
        admitted, breach, issues = self._review(projection, request, log, call=calls)
        # The one repair call answers a failing self-check or a changed-file
        # breach (ADR-0037 D4), and both in one report when both hold (D4a).
        repaired = breach is not None or bool(issues)
        if repaired:
            calls += 1
            reply, more = await self._repair(
                request,
                projection,
                log,
                started,
                call=calls,
                prompt=opening,
                breach=breach,
                issues=issues,
            )
            message, turns = reply, turns + more
            admitted = self._settle(projection, request, log, call=calls)
        if (
            not admitted
            and self.role_resilience
            and (limit is None or turns < limit)
            and time.monotonic() - started < request.deadline_seconds
        ):
            # `/11` (PDR-0019, ADR-0023): one continuation turn on
            # an empty diff -- after the first call, or after a repair that
            # emptied it; the two may both happen. Same shape as the repair
            # call -- one more `_call` on the same projection, then re-admit
            # and re-check -- and at most one per invocation.
            continued = True
            calls += 1
            reply, more = await self._call(
                request, projection, log, started, call=calls, prompt=TREE_EDITOR_CONTINUATION
            )
            message, turns = reply, turns + more
            if repaired:
                admitted = self._settle(projection, request, log, call=calls)
            else:
                admitted, breach, issues = self._review(projection, request, log, call=calls)
                if breach is not None:
                    # D4: the continuation breached and the one repair call is
                    # still unused; it is the third and last call.
                    calls += 1
                    reply, more = await self._repair(
                        request,
                        projection,
                        log,
                        started,
                        call=calls,
                        prompt=opening,
                        breach=breach,
                        issues=issues,
                    )
                    message, turns = reply, turns + more
                    admitted = self._settle(projection, request, log, call=calls)
                elif issues:
                    # A self-check failure after the continuation stays terminal.
                    raise _Refused("self_check_failed", issues[0])

        if limit is not None and turns > limit:
            raise _Refused("budget", f"turn budget exceeded: {turns} over {limit}")
        if not admitted:
            return ("declined", message or _NO_CHANGE, continued)
        self._verify_seals()
        publish(admitted, projection)
        log.record({"phase": "published", "changed": [change.path for change in admitted]})
        return ("edited", message or _PUBLISHED, continued)

    async def _call(
        self,
        request: EditRequest,
        projection: Projection,
        log: AttemptLog,
        started: float,
        *,
        call: int,
        prompt: str,
    ) -> tuple[str, int]:
        """One CLI call over the projection, on a backend built for it alone."""

        remaining = max(0.0, request.deadline_seconds - (time.monotonic() - started))
        try:
            # `text()` returns the file's current bytes and re-digests nothing,
            # so the seal is checked here rather than at publication: a prompt
            # rewritten during the first call must never reach the repair call.
            self.prompt.verify()
            system = self.prompt.text()
        except SealChanged as exc:
            raise _Refused("prompt_changed") from exc
        log.record(
            {
                "phase": "request",
                "call": call,
                "model": self.section.model,
                "system_sha256": self.prompt.sha256,
                "opening": prompt,
                "timeout_s": remaining,
            }
        )
        backend = self._backend(remaining)
        try:
            # `asyncio.timeout`, not `wait_for`: on 3.11 `wait_for` drops a stop that
            # lands as the call finishes. A spent budget still never starts
            # the agent, as `wait_for(..., 0)` did.
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                response = await backend.complete_file_edit(
                    projection.root, prompt, system, timeout_s=remaining
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.record(
                {
                    "phase": "refused",
                    "error_class": "agent_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "diagnostics": dict(getattr(backend, "last_codex_diagnostics", None) or {}),
                }
            )
            raise _Refused("agent_error", f"{type(exc).__name__}: {exc}") from exc

        usage = dict(getattr(backend, "last_usage", None) or {})
        diagnostics = dict(getattr(backend, "last_codex_diagnostics", None) or {})
        reported = usage.get("tool_turns")
        turns = reported if isinstance(reported, int) and reported > 0 else 1
        content = str(getattr(response, "content", "") or "")
        message = content or str(diagnostics.get("last_message") or "")
        log.record(
            {
                "phase": "response",
                "call": call,
                "content": content,
                "usage": usage,
                "diagnostics": diagnostics,
                "tool_turns": turns,
            }
        )
        return message, turns

    def _admit(self, projection: Projection, request: EditRequest) -> tuple[Change, ...]:
        return admit(
            projection,
            writable_paths=request.writable_paths,
            budgets=self.section.budgets,
        )

    def _review(
        self, projection: Projection, request: EditRequest, log: AttemptLog, *, call: int
    ) -> tuple[tuple[Change, ...], BudgetViolation | None, tuple[str, ...]]:
        """Admit and self-check a call whose tree may still buy the repair call.

        A changed-file breach is returned rather than raised (ADR-0037 D4), with
        no admitted changes; the self-check then runs over the breached tree, so
        one repair report can carry both (D4a). Every other refusal raises.
        """

        try:
            admitted, breach = self._admit(projection, request), None
        except BudgetViolation as exc:
            if exc.code != "changed_file_budget":
                raise
            admitted, breach = (), exc
        changed = _changed_paths(projection.changes() if breach is not None else admitted)
        issues = self_check(projection.root, changed) if changed else ()
        if issues:
            log.record({"phase": "self_check", "call": call, "issues": list(issues)})
        return admitted, breach, issues

    def _settle(
        self, projection: Projection, request: EditRequest, log: AttemptLog, *, call: int
    ) -> tuple[Change, ...]:
        """Admit and self-check the tree a call left when no repair is left to buy.

        A budget breach now raises from admission, and a failing self-check ends
        the attempt `self_check_failed`.
        """

        admitted = self._admit(projection, request)
        issues = self_check(projection.root, _changed_paths(admitted))
        if issues:
            log.record({"phase": "self_check", "call": call, "issues": list(issues)})
            raise _Refused("self_check_failed", issues[0])
        return admitted

    async def _repair(
        self,
        request: EditRequest,
        projection: Projection,
        log: AttemptLog,
        started: float,
        *,
        call: int,
        prompt: str,
        breach: BudgetViolation | None,
        issues: Sequence[str],
    ) -> tuple[str, int]:
        """The one repair call, framed by `VALIDATION.json`.

        Without a breach the document is `{"issues": [...]}`, byte for byte as
        before ADR-0037. With one it leads with the budget issue and adds the
        files that differ now as `changed_paths` (D4, D4a).
        """

        report: dict[str, Any] = {"issues": list(issues)}
        if breach is not None:
            paths = changed_file_paths(projection)
            limit = self.section.budgets.max_changed_files
            assert limit is not None  # a changed-file breach implies a limit
            log.record({"phase": "budget", "call": call - 1, "changed": list(paths)})
            report = {
                "issues": [
                    f"changed-file budget exceeded: {len(paths)} files changed, limit {limit}; "
                    "remove files you created or write updated files' original bytes back "
                    f"until at most {limit} differ; a deleted file cannot be restored this way",
                    *issues,
                ],
                "changed_paths": list(paths),
            }
        _write_json(projection.root / CONTROL_PREFIX / _VALIDATION_FILE, report)
        projection.mark_control()
        return await self._call(request, projection, log, started, call=call, prompt=prompt)

    def _backend(self, remaining: float) -> Any:
        """A fresh backend per call: `last_usage` is instance state."""

        build = self.backend_factory if self.backend_factory is not None else AgentBackend
        return build(
            model=self.section.model,
            agent_permission=self.section.permission,
            reasoning_effort=self.section.reasoning_effort,
            timeout_s=remaining,
            sandbox=None,
            agent_bin=str(self.program.path),
            env=dict(self.child_env),
            explicit=True,
        )

    # ----------------------------------------------------------------------
    # The control plane
    # ----------------------------------------------------------------------

    def _write_control_plane(
        self,
        projection: Projection,
        request: EditRequest,
        opening: str,
        catalog: Sequence[EvidenceRecord],
    ) -> None:
        """`EDIT_REQUEST.json`, the evidence index and one file per record."""

        control = projection.root / CONTROL_PREFIX
        (control / _EVIDENCE_DIR).mkdir(parents=True)
        index: list[dict[str, str]] = []
        for number, record in enumerate(catalog):
            relative = f"{_EVIDENCE_DIR}/{number:06d}.txt"
            (control / relative).write_text(record.content, encoding="utf-8")
            index.append({"id": record.evidence_id, "kind": record.kind, "path": relative})
        _write_json(control / _EVIDENCE_DIR / _EVIDENCE_INDEX, index)

        if request.traces is not None:
            from evolution.tree.roles.editor_contracts import cited_evidence_ids
            from evolution.tree.roles.reflection_reader import ReaderArgumentError

            ids = [
                id_
                for id_ in cited_evidence_ids(request.brief.diagnosis)
                if id_ in request.traces.ids()
            ]
            if ids:
                (control / "traces").mkdir()
                trace_index = []
                for number, id_ in enumerate(ids):
                    try:
                        content = request.traces.whole(id_)
                    except ReaderArgumentError as exc:
                        if exc.code != "trace_unavailable":
                            raise
                        continue
                    relative = f"traces/{number:06d}.json"
                    (control / relative).write_text(content, encoding="utf-8")
                    trace_index.append(
                        {"evidence_id": id_, "path": relative, "total_characters": len(content)}
                    )
                _write_json(control / "traces" / "index.json", trace_index)

        budgets = self.section.budgets
        _write_json(
            control / _REQUEST_FILE,
            {
                "version": REQUEST_VERSION,
                "attempt_id": request.attempt_id,
                "round": request.round,
                "brief": brief_payload(request.brief),
                "opening": opening,
                "editable_paths": list(request.writable_paths),
                "limits": {
                    "max_changed_files": budgets.max_changed_files,
                    "max_written_bytes": budgets.max_written_bytes,
                    "max_payload_bytes": budgets.max_payload_bytes,
                    "max_turns": self.section.max_turns,
                    "deadline_seconds": request.deadline_seconds,
                },
            },
        )

    # ----------------------------------------------------------------------
    # Seals and redaction
    # ----------------------------------------------------------------------

    def _verify_seals(self) -> None:
        try:
            self.prompt.verify()
        except SealChanged as exc:
            raise _Refused("prompt_changed") from exc
        try:
            self.program.verify()
        except SealChanged as exc:
            raise _Refused("command_changed") from exc

    def _log_secrets(self) -> tuple[str, ...]:
        """The run's secrets, plus the sealed CLI's path.

        The backend's diagnostics carry the invocation it built, and the
        transcript is not the place for the operator's filesystem layout.
        """

        return (*self.secrets, str(self.program.path))


def _refusal(error_class: str, detail: str = "") -> dict[str, Any]:
    return {"phase": "refused", "error_class": error_class, "error": detail}


def _changed_paths(admitted: Sequence[Change]) -> tuple[str, ...]:
    return tuple(change.path for change in admitted if change.action != "delete")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
