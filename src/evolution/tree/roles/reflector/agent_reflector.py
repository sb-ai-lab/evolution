"""The agent-CLI `CandidateReflector`: binding twin of `AgentCliEditor`.

One reflection is: verify the two seals, write the control plane into the
`.evolution/` directory of the projection the campaign froze -- the
`reflect_request/1` document plus the rendered opening and the limits, the
evidence index and one file per record, and the rendered environment
documentation when the request carries any -- then run the CLI once over that
projection through a backend built for that call alone, and read
`.evolution/REFLECTION.json` back.

What comes back is not admitted, published or diffed: a Reflector proposes no
change, the projection is read-only outside the control plane, and the campaign
discards it whatever happens. The one check is structural -- the file is one
`{"status", "groups", "note"}` object whose `status` is `reflected` or
`declined` and whose groups pass `campaign_reflector.validate_groups` over the
catalog this kind itself rendered. That check runs with *no* needles: the
oracle is the campaign's to hold, and the campaign re-runs the same validation
with them before any group becomes durable. A missing or malformed file, or a
group the pre-check refuses, buys exactly one repair call framed by
`.evolution/VALIDATION.json`; a second failure is `failed/self_check_failed`.
`max_turns` is then applied as an acceptance limit over the turns both calls
reported, exactly as the editor applies its own.

The trust model, the backend seam, the child environment and the transcript's
secret set are `agent_editor`'s, unchanged: the sealed program's path is added
to the run's secrets so neither a forwarded credential nor the operator's
filesystem layout can reach the log through an echoed invocation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from evolution.llm.agent_backend import AgentBackend, reports_tool_turns
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.agent_role import (
    AgentPromptChanged,
    agent_log_secrets,
    call_agent,
    verify_agent_seals,
)
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.editor.agent_editor import (  # noqa: PLC2701 - one control plane, not two
    CONTROL_PREFIX,
    _refusal,
    _Refused,
    _write_json,
)
from evolution.tree.roles.editor.editor_brief import EvidenceRecord
from evolution.tree.roles.identities import ReflectorIdentity
from evolution.tree.roles.reflection_role_helpers import (
    RoleHelpers,
    groups_without_probe_refs,
    install_helpers,
    install_probe_helper,
    probe_log_secrets,
    probe_opening,
)
from evolution.tree.roles.reflector.reflect_brief import (
    AdaptiveReflectBrief,
    catalog_case_ids,
    render_reflect_catalog,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_budget import FirstRequest, document_first_request
from evolution.tree.roles.reflector.subprocess_reflector import (  # noqa: PLC2701
    MAX_NOTE_BYTES,
    _request_document,
)
from evolution.tree.roles.reflector_contracts import (
    GroupError,
    ProbeAccess,
    ReflectRequest,
    ReflectResult,
    adaptive_group_schema,
    check_adaptive_group_bounds,
    drop_empty_unknown_keys,
    validate_groups,
)
from evolution.tree.roles.seals import FileSeal, agent_reflector_identity
from evolution.tree.roles.workspace import AttemptLog, bounded_note, open_attempt_log

__all__ = [
    "CONTROL_PREFIX",
    "REFLECTION_FILE",
    "REFLECT_REQUEST_FILE",
    "VALIDATION_FILE",
    "AgentCliReflector",
]

REFLECT_REQUEST_FILE = "REFLECT_REQUEST.json"
REFLECTION_FILE = "REFLECTION.json"
VALIDATION_FILE = "VALIDATION.json"
_EVIDENCE_DIR = "evidence"
_EVIDENCE_INDEX = "index.json"
_DOCS_DIR = "environment-docs"
_DOCS_FILE = "RENDERED.txt"

_STATUSES = frozenset({"reflected", "declined"})


@dataclass(frozen=True)
class AgentCliReflector:
    """A `CandidateReflector` that is one agent-CLI run, plus at most one repair."""

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
    family15: bool = field(default=False, kw_only=True)
    identity: ReflectorIdentity = field(init=False)

    #: The campaign reads this with `getattr(reflector, "control_prefix", None)`
    #: and projects with it, so `freeze_projection` leaves this one subtree
    #: writable: it is where the kind's request and the CLI's result live.
    control_prefix: ClassVar[str] = CONTROL_PREFIX

    def __post_init__(self) -> None:
        if self.program.path is None:
            raise ValueError("the agent CLI executable must be sealed as a file")
        if self.section.max_turns is not None and not reports_tool_turns(self.section.model):
            # The loader refuses this too; refusing it here as
            # well keeps an inert acceptance limit out of every other caller.
            raise ValueError("max_turns needs a CLI that reports turn telemetry")
        object.__setattr__(self, "log_root", Path(self.log_root))
        object.__setattr__(
            self,
            "identity",
            agent_reflector_identity(
                self.section, self.prompt, self.program, family15=self.family15
            ),
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
            system_prompt=self.prompt.text(),
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Run one reflection: write the control plane, run the CLI, read the result."""

        if request.reflection_id != Path(request.reflection_id).name or request.reflection_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("reflection_id must be one path segment")
        started = time.monotonic()
        try:
            self._verify_seals()
        except _Refused as exc:
            return ReflectResult("failed", (), exc.note, code=exc.code)
        # The transcript is opened before anything else, so its own refusal --
        # an entry at the reflection's pathname that is not a private regular
        # file -- has no log to be recorded in and is classified here.
        try:
            attempt_log = open_attempt_log(
                self.log_root,
                request.reflection_id,
                secrets=(*self._log_secrets(), *probe_log_secrets(request.probe)),
                admitted=self.admitted_hygiene,
            )
        except WorkspaceError as exc:
            return ReflectResult(
                "failed", (), bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        with attempt_log as log:
            try:
                return self._after_run(await self._attempt(request, log, started), log)
            except _Refused as exc:
                log.record(_refusal(exc.klass, exc.detail))
                return self._after_run(
                    ReflectResult("failed", (), bounded_note(exc.note), code=exc.code), log
                )
            except asyncio.CancelledError:
                # The backend has already killed the CLI's process group; the
                # projection goes with it and nothing of it was ever admitted.
                raise
            except Exception as exc:
                log.record(
                    {"phase": "exception", "error_type": type(exc).__name__, "error": str(exc)}
                )
                return self._after_run(
                    ReflectResult(
                        "failed", (), f"exception:{type(exc).__name__}", code="exception"
                    ),
                    log,
                )

    def _after_run(self, result: ReflectResult, log: AttemptLog) -> ReflectResult:
        """Verify both seals after the reflection, whatever its outcome (PDR-0005).

        `AgentCliEditor._after_run`'s rule for the other role: a prompt or a
        CLI executable rewritten under a reflection is a tampering the result
        must carry rather than mask. A reflection publishes nothing, so unlike
        the editor's there is no outcome whose own check stands for this one --
        every result passes through here.
        """

        try:
            self._verify_seals()
        except _Refused as exc:
            if result.note.startswith(exc.klass):
                return result
            log.record(_refusal(exc.klass, f"after {result.note}"))
            return ReflectResult(
                "failed",
                (),
                bounded_note(f"{exc.klass} (after {result.note})"),
                code=exc.klass,
            )
        return result

    # ----------------------------------------------------------------------
    # The reflection
    # ----------------------------------------------------------------------

    async def _attempt(
        self, request: ReflectRequest, log: AttemptLog, started: float
    ) -> ReflectResult:
        root = Path(request.workspace)
        catalog = render_reflect_catalog(request.evidence)
        docs = request.environment_docs
        opening = render_reflect_opening(
            request.evidence,
            catalog,
            request.editable_paths,
            None
            if isinstance(request.evidence, AdaptiveReflectBrief) or docs is None
            else docs.text,
            editor_max_changed_files=request.editor_max_changed_files,
        )
        # Session deadlines use the owning loop's clock. Convert the remaining
        # budget for the standalone helper's cross-process monotonic clock.
        deadline_at = started + request.deadline_seconds
        if request.probe_session is not None:
            remaining = request.probe_session.deadline_at - asyncio.get_running_loop().time()
            deadline_at = time.monotonic() + remaining
        helpers = self._write_control_plane(
            root, request, opening, catalog, deadline_at=deadline_at
        )
        opening += probe_opening(
            request.probe, typed_body=request.evidence.version == "reflect_brief/6"
        )

        if helpers is not None:
            helpers.verify()
        try:
            turns = await self._call(request, root, log, started, call=1, prompt=opening)
        finally:
            if helpers is not None:
                helpers.verify()
        result, issues = _read_result(root, request, catalog)
        if issues and isinstance(request.evidence, AdaptiveReflectBrief):
            return ReflectResult("failed", (), "invalid_result", code="invalid_result")
        if issues:
            log.record({"phase": "self_check", "call": 1, "issues": list(issues)})
            _write_json(root / CONTROL_PREFIX / VALIDATION_FILE, {"issues": list(issues)})
            if helpers is not None:
                helpers.verify()
            try:
                turns += await self._call(request, root, log, started, call=2, prompt=opening)
            finally:
                if helpers is not None:
                    helpers.verify()
            result, issues = _read_result(root, request, catalog)
            if issues:
                log.record({"phase": "self_check", "call": 2, "issues": list(issues)})
                return ReflectResult("failed", (), "self_check_failed", code="self_check_failed")

        limit = self.section.max_turns
        if limit is not None and turns > limit:
            raise _Refused("budget", f"turn budget exceeded: {turns} over {limit}")
        assert result is not None  # no issues means a result was read
        log.record({"phase": "reflected", "status": result.status, "groups": len(result.groups)})
        return result

    async def _call(
        self,
        request: ReflectRequest,
        root: Path,
        log: AttemptLog,
        started: float,
        *,
        call: int,
        prompt: str,
    ) -> int:
        """One CLI call over the projection, on a backend built for it alone."""
        try:
            return await call_agent(
                section=self.section,
                program=self.program,
                prompt=self.prompt,
                child_env=self.child_env,
                root=root,
                opening=prompt,
                deadline_seconds=request.deadline_seconds,
                started=started,
                log=log,
                call=call,
                backend_factory=self.backend_factory
                if self.backend_factory is not None
                else AgentBackend,
                on_backend_error=self._backend_error,
            )
        except AgentPromptChanged as exc:
            raise _Refused("prompt_changed") from exc

    @staticmethod
    def _backend_error(exc: Exception, backend: Any, log: AttemptLog) -> None:
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

    # ----------------------------------------------------------------------
    # The control plane
    # ----------------------------------------------------------------------

    def _write_control_plane(
        self,
        root: Path,
        request: ReflectRequest,
        opening: str,
        catalog: Sequence[EvidenceRecord],
        *,
        deadline_at: float,
    ) -> RoleHelpers | None:
        """`REFLECT_REQUEST.json`, the evidence index, one file per record, the
        environment documentation's own files and their rendering.

        The control directory is the one writable place in a frozen
        projection. It is created here when the caller did not create it (an
        unfrozen projection); under the campaign's own frozen one it already
        exists, and a projection that offers neither is a workspace refusal
        rather than a traceback.
        """

        control = root / CONTROL_PREFIX
        probe_helpers = None
        if request.probe is not None:
            probe_helpers = install_probe_helper(
                control / "probe", request.probe, deadline_at=deadline_at
            )
            opening += probe_opening(
                request.probe,
                probe_helpers.descriptors["probe_request"],
                typed_body=request.evidence.version == "reflect_brief/6",
            )
        if isinstance(request.evidence, AdaptiveReflectBrief):
            if request.reader is None:
                raise _Refused("reader_unavailable")
            helpers = install_helpers(control / "readers", request.reader)
            if probe_helpers is not None:
                helpers = RoleHelpers(helpers.descriptors, (*helpers.seals, *probe_helpers.seals))
            document = json.loads(_request_document(request, readers=helpers.descriptors))
            document["opening"] = opening
            document["role_budget"] = {
                "max_turns": self.section.max_turns,
                "deadline_seconds": request.deadline_seconds,
            }
            _write_json(control / REFLECT_REQUEST_FILE, document)
            # The request is stable control data too, including its transient access.
            request_file = control / REFLECT_REQUEST_FILE
            if probe_helpers is not None:
                request_file.chmod(0o400)
            return RoleHelpers(
                helpers.descriptors,
                (
                    *helpers.seals,
                    (request_file, hashlib.sha256(request_file.read_bytes()).hexdigest()),
                ),
            )
        try:
            (control / _EVIDENCE_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
            index: list[dict[str, str]] = []
            for number, record in enumerate(catalog):
                relative = f"{_EVIDENCE_DIR}/{number:06d}.txt"
                (control / relative).write_text(record.content, encoding="utf-8")
                index.append({"id": record.evidence_id, "kind": record.kind, "path": relative})
            _write_json(control / _EVIDENCE_DIR / _EVIDENCE_INDEX, index)
            docs = request.environment_docs
            if docs is not None:
                # `environment-docs/` carries *the snapshot's
                # files*, not only the combined rendering. An agent that wants
                # one document reads it by its own relative path; `RENDERED.txt`
                # stays beside them as the whole rendering, which is what the
                # `chat` and `subprocess` kinds receive and what the opening
                # already quotes. The relative paths come from
                # `render_environment_docs`'s own guarded walk of the
                # operator's directory, so each is a plain relative path under
                # the docs root.
                root = control / _DOCS_DIR
                root.mkdir(mode=0o700, exist_ok=True)
                (root / _DOCS_FILE).write_text(docs.text, encoding="utf-8")
                for relative, content in docs.files:
                    target = root / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.write_bytes(content)
            document = json.loads(_request_document(request).decode("utf-8"))
            document["opening"] = opening
            document["limits"] = {
                "max_turns": self.section.max_turns,
                "deadline_seconds": request.deadline_seconds,
            }
            _write_json(control / REFLECT_REQUEST_FILE, document)
        except OSError as exc:
            raise _Refused("workspace_changed", f"control plane is not writable: {exc}") from exc
        if probe_helpers is not None:
            request_file = control / REFLECT_REQUEST_FILE
            request_file.chmod(0o400)
            return RoleHelpers(
                probe_helpers.descriptors,
                (
                    *probe_helpers.seals,
                    (request_file, hashlib.sha256(request_file.read_bytes()).hexdigest()),
                ),
            )
        return None

    # ----------------------------------------------------------------------
    # Seals and redaction
    # ----------------------------------------------------------------------

    def _verify_seals(self) -> None:
        changed = verify_agent_seals(self.prompt, self.program)
        if changed is not None:
            raise _Refused(changed)

    def _log_secrets(self) -> tuple[str, ...]:
        """The run's secrets, plus the sealed CLI's path (`AgentCliEditor`'s rule)."""

        return agent_log_secrets(self.secrets, self.program)


def _read_result(
    root: Path, request: ReflectRequest, catalog: Sequence[EvidenceRecord]
) -> tuple[ReflectResult | None, tuple[str, ...]]:
    """`.evolution/REFLECTION.json`, and the issues that make it unusable.

    The issues are what `VALIDATION.json` shows the repair call, so each names
    what to fix and nothing else: no absolute path, and -- since the check runs
    with no needles -- no oracle string can travel back through one either.
    """

    path = root / CONTROL_PREFIX / REFLECTION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, (f"{CONTROL_PREFIX}/{REFLECTION_FILE} is missing or unreadable",)
    except UnicodeDecodeError:
        return None, (f"{CONTROL_PREFIX}/{REFLECTION_FILE} is not UTF-8 text",)
    try:
        payload = json.loads(text)
    except ValueError:
        return None, (f"{CONTROL_PREFIX}/{REFLECTION_FILE} is not one JSON object",)
    if not isinstance(payload, dict) or set(payload) != {"status", "groups", "note"}:
        return None, ('the result object must have exactly the keys "status", "groups", "note"',)
    status, groups, note = payload["status"], payload["groups"], payload["note"]
    issues: list[str] = []
    if status not in _STATUSES:
        issues.append('"status" must be "reflected" or "declined"')
    if type(note) is not str or len(note.encode()) > MAX_NOTE_BYTES:
        issues.append(f'"note" must be a string of at most {MAX_NOTE_BYTES} bytes')
    try:
        checked = groups
        if request.normalize_empty_keys and isinstance(request.evidence, AdaptiveReflectBrief):
            checked = drop_empty_unknown_keys(
                groups,
                adaptive_group_schema(
                    probes=request.probe is not None,
                    documents=request.evidence.document_ids is not None,
                ),
            )
        checked_groups = groups_without_probe_refs(checked, probe=request.probe is not None)
        if isinstance(request.evidence, AdaptiveReflectBrief):
            brief = request.evidence
            check_adaptive_group_bounds(checked, brief.group_text_chars)
            validate_groups(
                checked_groups,
                catalog_ids=brief.observation_ids,
                observation_ids=brief.observation_ids,
                failing_evidence_ids=brief.failing_evidence_ids,
                case_ids=set(brief.observation_ids.values()),
                editable_paths=request.editable_paths,
                probe_seqs=None,
                needles=(),
                document_ids=brief.document_ids,
                group_text_chars=brief.group_text_chars,
            )
        else:
            validate_groups(
                checked_groups,
                catalog_ids=catalog_case_ids(catalog),
                case_ids={case.case_id for case in request.evidence.cases},
                editable_paths=request.editable_paths,
                probe_seqs=None,
                needles=(),
            )
    except GroupError as exc:
        issues.append(f'"groups": {exc.detail}')
    if issues:
        return None, tuple(issues)
    return ReflectResult(status, tuple(groups), note), ()
