"""The chat `CandidateReflector`: binding twin of `ChatEditor`.

One reflection is one OpenAI-compatible tool loop over a *frozen* projection of
the Parent: the same preflight dialect negotiation, the same retried provider
turn, the same pinned provider identity, the same protocol repairs and
consecutive-failure cap and the same delta-encoded transcript as the editor's
loop -- the preflight imported from `chat_editor`, the turns run by the shared
`chat_loop.run_tool_loop` rather than copied -- over five tools
that only read. `list_files` and `read_file` reach the projection through the
editor's own path guard; `list_evidence` and `read_evidence` page the
`reflect_brief` catalog with the editor's own paging constants; and
`finish_reflection` is the one terminal, carrying the claimed diagnosis groups
raw.

The campaign owns group admission over the catalog, corpus, oracle needles and
run secrets -- none of which this role is shown. On `/16` it supplies that same
gate as a private terminal callback, so a repairable refusal can continue this
conversation before the campaign journals an outcome. The native probe tool
calls the campaign-owned session; only the campaign can admit diagnostic references.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.chat_loop import (
    Correct,
    Done,
    LoopLimits,
    Repair,
    ToolOutput,
    run_tool_loop,
)
from evolution.tree.roles.config import (
    ChatEditorSection,
    effective_protocol_repairs,
    effective_tool_failures,
)
from evolution.tree.roles.editor.chat_editor import (  # noqa: PLC2701 - one tool loop, not two
    ChatCredentials,
    EditFailure,
    ToolError,
    _close_client,
    _compact_messages,
    _evidence_chunk,
    _evidence_page,
    _expect,
    _file_summary,
    _new_client,
    _offset,
    _tool,
    negotiate_style,
    read_file_tool,
    readable_files,
    record_request,
)
from evolution.tree.roles.editor.editor_brief import EvidenceRecord
from evolution.tree.roles.identities import ReflectorIdentity
from evolution.tree.roles.limits import adaptive_limits
from evolution.tree.roles.reflection_reader import ReaderArgumentError
from evolution.tree.roles.reflection_role_helpers import probe_log_secrets, probe_opening
from evolution.tree.roles.reflector.reflect_brief import (
    AdaptiveReflectBrief,
    render_reflect_catalog,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_budget import (
    FirstRequest,
    RequestBudget,
    chat_first_request,
    chat_request_size,
)
from evolution.tree.roles.reflector_contracts import (
    ADAPTIVE_GROUP_TEXT_KEYS,
    GroupError,
    ProbeAccess,
    ReflectRequest,
    ReflectResult,
    RejectionDetail,
    adaptive_group_schema,
    check_adaptive_group_bounds,
    drop_empty_unknown_keys,
)
from evolution.tree.roles.role_call import AttemptRecorder
from evolution.tree.roles.seals import FileSeal, SealChanged, chat_reflector_identity
from evolution.tree.roles.workspace import AttemptLog, bounded_note, open_attempt_log

__all__ = ["ChatReflector", "reflect_tools"]

_ROLE = "reflector"

_GROUP_KEYS = [
    "root_cause",
    "suggested_fix",
    "evidence_ids",
    "affected_cases",
    "target_paths",
]


def _group_schema(
    *, probe: bool, adaptive: bool = False, docs: bool = False, group_text_chars: int | None = 512
) -> dict[str, Any]:
    """The `finish_reflection` group shape, offered exactly as it will be judged.

    `campaign_reflector.validate_groups` requires *exactly* the five non-probe
    keys when no probe ran (`probe_seqs=None`), so advertising an optional
    `probe_refs` without a probe invites a provider-conforming result that the
    campaign then records as `reflection_failed/invalid_result` -- the schema
    would be promising a shape the one gate rejects. The key is therefore offered
    only when the request declares a probe.

    `group_text_chars` is the round's text limit: each adaptive text
    field's `maxLength`, omitted for `None`. At 512, today's limit, every byte
    stays as before, `evidence_ids`'s "distinct" included; any
    other limit drops that word, since the bound counts every listed id.
    """

    properties: dict[str, Any] = {
        "root_cause": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "affected_cases": {"type": "array", "items": {"type": "string"}},
        "target_paths": {"type": "array", "items": {"type": "string"}},
    }
    required = list(_GROUP_KEYS)
    if adaptive:
        limits = adaptive_limits(group_text_chars)
        chars = limits["sentence_chars"]
        for key in ADAPTIVE_GROUP_TEXT_KEYS:
            properties[key] = {
                "type": "string",
                **({"maxLength": chars} if chars is not None else {}),
            }
            if key not in required:
                required.append(key)
        distinct = " distinct" if group_text_chars == 512 else ""
        properties["evidence_ids"]["description"] = (
            f"At most {limits['evidence_ids']}{distinct} observation IDs."
        )
        for key in ("affected_cases", "target_paths"):
            properties[key]["maxItems"] = limits[key]
        if docs:
            properties["document_ids"] = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": limits["document_ids"],
            }
    if probe:
        properties["probe_refs"] = {"type": "array", "items": {"type": "integer"}}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def reflect_tools(
    *,
    probe: bool,
    adaptive: bool = False,
    docs: bool = False,
    history: bool = False,
    group_text_chars: int | None = 512,
    result_repair: bool = False,
    trace: bool = False,
    typed_body: bool = False,
    contracts: bool = False,
) -> list[dict[str, Any]]:
    """The five read-only tools, with the terminal built for the request's probe.

    `group_text_chars` (adaptive only) is the reflection's round limit, which
    the session and `measure_first_request` both read from the brief.
    `result_repair` (`/16` chat only) gives `finish_reflection` the
    corrected-result contract as its description; otherwise every byte stays."""

    tools = [
        # `list_files` is first: the preflight offers exactly `tools[:1]`.
        _tool("list_files", {}),
        _tool(
            "read_file",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        _tool("list_evidence", {"offset": {"type": "integer", "minimum": 0}}),
        _tool(
            "read_evidence",
            {"evidence_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
            ["evidence_id"],
        ),
        _tool(
            "finish_reflection",
            {
                "groups": {
                    "type": "array",
                    "items": _group_schema(
                        probe=probe,
                        adaptive=adaptive,
                        docs=docs,
                        group_text_chars=group_text_chars,
                    ),
                },
                "declined": {"type": "boolean"},
            },
            ["groups"],
        ),
    ]

    if result_repair:
        tools[4]["function"]["description"] = FINISH_REFLECTION_REPAIR_DESCRIPTION
    if probe:
        tools.insert(
            4,
            _tool(
                "probe_request",
                {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "body": (
                        {
                            "type": ["object", "array", "string", "number", "boolean", "null"],
                            "description": "the JSON value to send as the request body; not text that contains JSON",
                        }
                        if typed_body
                        else {}
                    ),
                },
                ["method", "path", "body"],
            ),
        )
    if adaptive:
        tools[2]["function"]["parameters"]["properties"].update(
            {"family_id": {"type": ["string", "null"]}, "case_id": {"type": ["string", "null"]}}
        )
        if contracts:
            tools[2]["function"]["parameters"]["properties"]["case_id"]["description"] = (
                "A case_handle or a case_id from a list_evidence catalog entry."
            )
        terminal = next(tool for tool in tools if tool["function"]["name"] == "finish_reflection")
        groups = terminal["function"]["parameters"]["properties"]["groups"]
        groups["maxItems"] = adaptive_limits(group_text_chars)["groups"]
        if docs:
            tools.extend(
                [
                    _tool("list_documents", {"offset": {"type": "integer", "minimum": 0}}),
                    _tool(
                        "read_document",
                        {
                            "document_id": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                        },
                        ["document_id"],
                    ),
                ]
            )
        if history:
            # (ADR-0039, `reflect_tools/2`): the reflection's history on demand.
            tools.extend(
                [
                    _tool("list_history", {"offset": {"type": "integer", "minimum": 0}}),
                    _tool(
                        "read_history",
                        {
                            "history_id": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                        },
                        ["history_id"],
                    ),
                ]
            )
    if trace:
        index = next(
            i for i, tool in enumerate(tools) if tool["function"]["name"] == "read_evidence"
        )
        tools.insert(
            index + 1,
            _tool(
                "read_trace",
                {"evidence_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
                ["evidence_id"],
            ),
        )
    return tools


def chat_first_messages(
    prompt_text: str,
    brief: Any,
    *,
    editable_paths: Sequence[str],
    environment_docs: str | None,
    probe: ProbeAccess | None,
    editor_max_changed_files: int | None,
    session_limits: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """The first two messages (one renderer measures and sends)."""

    messages = [
        {"role": "system", "content": prompt_text},
        {
            "role": "user",
            "content": render_reflect_opening(
                brief,
                render_reflect_catalog(brief),
                editable_paths,
                environment_docs,
                editor_max_changed_files=editor_max_changed_files,
            )
            + probe_opening(probe, typed_body=getattr(brief, "version", None) == "reflect_brief/6"),
        },
    ]
    if session_limits is not None:
        turns, repairs = session_limits
        messages[1]["content"] += (
            f"\n\nSession limits: at most {turns} model turns in this session, "
            f"the preflight included, and {repairs} protocol repairs."
        )
    return messages


#: `/16` chat (ADR-0048): the `finish_reflection` description that states
#: the corrected-result contract. Its presence enters the Reflector identity
#: (`chat_reflector_identity(..., result_repair=True)`).
FINISH_REFLECTION_REPAIR_DESCRIPTION = (
    "Finish the reflection. Each affected_cases entry is the case_id of a cited "
    "observation of that same case, never a catalog case_handle. The result is "
    "checked before the reflection ends: a rejected call returns field-path "
    "feedback; revise the result and call finish_reflection again."
)

# Base read-only names; per-request tool_names includes conditional tools.
_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in reflect_tools(probe=False))


@dataclass(frozen=True)
class ChatReflector:
    """A `CandidateReflector` that is one OpenAI-compatible tool loop per reflection."""

    section: ChatEditorSection
    credentials: ChatCredentials
    prompt: FileSeal
    log_root: Path
    secrets: tuple[str, ...]
    client_factory: Callable[[ChatEditorSection, ChatCredentials], Any] | None = None
    #: Instance digests of the run-directory hygiene findings the operator
    #: has accepted (PDR-0010), handed to `open_attempt_log` with the
    #: `log_root` it re-admits; empty by default.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    #: `/11` (PDR-0019, ADR-0023): `ChatEditor.role_resilience`'s own rule for
    #: the Reflector: true selects `max_retries=0` and `role_call_retry/1`
    #: over `section.retry`, which must not be `None`; false (the default)
    #: keeps every earlier family byte for byte.
    role_resilience: bool = field(default=False, kw_only=True)
    #: families 12 and later: a `finish_reflection` whose groups break
    #: `check_adaptive_group_bounds` gets a protocol repair naming the bound,
    #: instead of failing the reflection `invalid_result` at once. False (the
    #: default) keeps every earlier family's outcome unchanged.
    bounds_repair: bool = field(default=False, kw_only=True)
    #: (ADR-0048), `/16` only: the campaign's admission closure checks each
    #: attempted `finish_reflection` before the role may end, and a repairable
    #: refusal continues the conversation. It also selects the contract's tool
    #: description and enters the identity; false keeps `/15` byte for byte.
    result_repair: bool = field(default=False, kw_only=True)
    family15: bool = field(default=False, kw_only=True)
    identity: ReflectorIdentity = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_root", Path(self.log_root))
        object.__setattr__(
            self,
            "identity",
            chat_reflector_identity(
                self.section, self.prompt, result_repair=self.result_repair, family15=self.family15
            ),
        )
        if self.role_resilience:
            assert self.section.retry is not None, "family-10 chat reflector requires retry"

    def request_budget(self) -> RequestBudget:
        return RequestBudget(
            self.section.context_compact_chars,
            self.section.context_message_max_chars,
            self.section.max_output_tokens,
        )

    def measure_first_request(
        self,
        brief: Any,
        *,
        editable_paths: Sequence[str],
        probe: ProbeAccess | None,
        editor_max_changed_files: int | None,
    ) -> FirstRequest:
        messages = chat_first_messages(
            self.prompt.text(),
            brief,
            editable_paths=editable_paths,
            environment_docs=None,
            probe=probe,
            editor_max_changed_files=editor_max_changed_files,
            session_limits=(
                (
                    max(self.section.max_turns, 2 * probe.budget + 8)
                    if probe
                    else self.section.max_turns,
                    effective_protocol_repairs(self.section),
                )
                if self.family15
                else None
            ),
        )
        tools = reflect_tools(
            trace=brief.version == "reflect_brief/6",
            typed_body=brief.version == "reflect_brief/6",
            probe=probe is not None,
            adaptive=True,
            docs=brief.document_ids is not None,
            history=True,
            group_text_chars=brief.group_text_chars,
            result_repair=self.result_repair,
            contracts=self.family15,
        )
        return chat_first_request(
            self.prompt.text(), messages, tools, brief, probe=probe, budget=self.request_budget()
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Run one reflection: negotiate, read, and return what was claimed.

        Its provider attempts feed `request.attempts`, the executor's recorder
        for the operator's line, or a private one without it.
        """

        attempts = request.attempts if request.attempts is not None else AttemptRecorder()
        attempts.unauthenticated = self.credentials.api_key is None
        return await self._reflect(request, attempts)

    async def _reflect(self, request: ReflectRequest, attempts: AttemptRecorder) -> ReflectResult:
        if request.reflection_id != Path(request.reflection_id).name or request.reflection_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("reflection_id must be one path segment")
        try:
            self.prompt.verify()
        except SealChanged:
            return ReflectResult("failed", (), "prompt_changed", code="prompt_changed")
        # The transcript is opened before anything else, so its own refusal --
        # an entry at the reflection's pathname that is not a private regular
        # file -- is classified here, as the workspace refusal it is.
        try:
            attempt_log = open_attempt_log(
                self.log_root,
                request.reflection_id,
                secrets=(*self.secrets, *probe_log_secrets(request.probe)),
                admitted=self.admitted_hygiene,
            )
        except WorkspaceError as exc:
            return ReflectResult(
                "failed", (), bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        with attempt_log as log:
            try:
                return self._after_run(await self._session(request, log, attempts), log)
            except asyncio.CancelledError:
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
        """Verify the prompt seal after the reflection, whatever its outcome.

        `ChatEditor._after_run`'s rule for the other role (PDR-0005): a prompt
        rewritten under a reflection is a tampering the result must carry
        rather than mask, and a reflection has no publication whose own check
        could stand for it -- so every outcome passes through here.
        """

        try:
            self.prompt.verify()
        except SealChanged:
            if result.note == "prompt_changed":
                return result
            log.record({"phase": "refused", "error_class": "prompt_changed", "error": result.note})
            return ReflectResult("failed", (), "prompt_changed", code="prompt_changed")
        return result

    async def _session(
        self, request: ReflectRequest, log: AttemptLog, attempts: AttemptRecorder
    ) -> ReflectResult:
        """One tool loop, over its own client, closed whatever the outcome."""

        client: Any = None
        session: _ReflectSession | None = None
        try:
            if self.client_factory is not None:
                client = self.client_factory(self.section, self.credentials)
            elif self.role_resilience:
                client = _new_client(self.section, self.credentials, max_retries=0)
            else:
                client = _new_client(self.section, self.credentials)
            session = _ReflectSession(self, request, log, client, attempts)
            return await session.run()
        except EditFailure as exc:
            log.record({"phase": "failed", "error_class": exc.klass, "note": exc.note})
            attempts.end(exc)
            return ReflectResult(
                "failed",
                (),
                bounded_note(exc.note),
                code=exc.code,
                rejection=session.last_rejection
                if session is not None and exc.klass == "turns_exhausted"
                else None,
            )
        finally:
            if client is not None:
                await _close_client(client)


class _ReflectSession:
    """The per-reflection state of one tool loop; nothing here outlives `reflect`."""

    def __init__(
        self,
        reflector: ChatReflector,
        request: ReflectRequest,
        log: AttemptLog,
        client: Any,
        attempts: AttemptRecorder | None = None,
    ) -> None:
        self.section = reflector.section
        self.prompt = reflector.prompt
        self.request = request
        self.root = Path(request.workspace)
        self.log = log
        self.client = client
        self.attempts = attempts
        self.role_resilience = reflector.role_resilience
        self.family15 = reflector.family15
        self.bounds_repair = reflector.bounds_repair
        #: The campaign-bound admission closure, used only by a reflector built
        #: for it: the in-loop check is part of the identity-bearing behavior.
        self.admission = request.result_admission if reflector.result_repair else None
        self.result_repair = reflector.result_repair
        self.deadline = time.monotonic() + request.deadline_seconds
        self.catalog: tuple[EvidenceRecord, ...] = render_reflect_catalog(request.evidence)
        self.probe = request.probe is not None
        if self.probe != (request.probe_session is not None):
            raise EditFailure("probe_unavailable")
        self.probe_context_exhausted = False
        self.max_turns = (
            max(self.section.max_turns, 2 * request.probe.budget + 8)
            if request.probe
            else self.section.max_turns
        )
        self.adaptive = isinstance(request.evidence, AdaptiveReflectBrief)
        if self.adaptive and request.reader is None:
            raise EditFailure("reader_unavailable")
        self.history = (
            self.adaptive
            and request.reader is not None
            and "list_history" in request.reader.methods
        )
        #: the reflection's round limit, as its brief publishes it; the
        #: tool schema and the bounds repair below both hold this one value.
        self.group_text_chars: int | None = (
            request.evidence.group_text_chars if self.adaptive else 512
        )
        self.tools = reflect_tools(
            trace=self.adaptive and request.evidence.version == "reflect_brief/6",
            typed_body=self.adaptive and request.evidence.version == "reflect_brief/6",
            probe=self.probe,
            adaptive=self.adaptive,
            docs=self.adaptive and request.evidence.document_ids is not None,
            history=self.history,
            group_text_chars=self.group_text_chars,
            result_repair=self.result_repair,
            contracts=self.family15,
        )
        self.last_rejection: RejectionDetail | None = None
        #: What the conversation has read, for `evidence_usage` and for a
        #: non-adaptive `read_evidence` that continues where the last read stopped.
        self.evidence_progress: dict[str, int] = {}
        self.completed_evidence_ids: set[str] = set()
        self.listed_evidence = False
        self._turns = 0
        self._sent: list[str] = []

    # -- budgets ----------------------------------------------------------

    def _session_limits(self) -> tuple[int, int] | None:
        return (self.max_turns, effective_protocol_repairs(self.section)) if self.family15 else None

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EditFailure("provider_error", "reflector deadline reached")
        return remaining

    def _next_turn(self) -> int:
        if self._turns >= self.max_turns:
            raise EditFailure("turns_exhausted")
        turn = self._turns
        self._turns += 1
        return turn

    def _record_request(
        self,
        messages: list[dict[str, Any]],
        request_style: str,
        turn: int,
        tools: list[dict[str, Any]],
    ) -> None:
        self._sent = record_request(self.log, self._sent, messages, request_style, turn, tools)

    # -- the run ----------------------------------------------------------

    async def run(self) -> ReflectResult:
        if self.history:
            budget = RequestBudget(
                self.section.context_compact_chars, self.section.context_message_max_chars
            )
            measured = chat_request_size(
                chat_first_messages(
                    self.prompt.text(),
                    self.request.evidence,
                    editable_paths=self.request.editable_paths,
                    environment_docs=None,
                    probe=self.request.probe,
                    editor_max_changed_files=self.request.editor_max_changed_files,
                    session_limits=self._session_limits(),
                ),
                self._offered_tools(),
            )
            if measured > budget.refusal_limit:
                # The campaign refused such a request before probe setup with the
                # same renderer; reaching here is a programming error,
                # which `reflect` reports as an `exception` failure.
                raise RuntimeError("first request exceeds its refusal limit")
        try:
            request_style = await negotiate_style(
                client=self.client,
                section=self.section,
                log=self.log,
                tools=self.tools,
                next_turn=self._next_turn,
                remaining=self._remaining,
                record=self._record_request,
                role=_ROLE,
                retry=self.section.retry if self.role_resilience else None,
                deadline=self.deadline if self.role_resilience else None,
                attempts=self.attempts,
                family15=self.family15,
            )
        except EditFailure as exc:
            if exc.code in ("provider_exhausted", "provider_rejected"):
                raise
            raise EditFailure(
                exc.klass, exc.detail, separator=exc.separator, code="preflight_failed"
            ) from exc
        return await self._conversation(request_style)

    async def _conversation(self, request_style: str) -> ReflectResult:
        messages = chat_first_messages(
            self.prompt.text(),
            self.request.evidence,
            editable_paths=self.request.editable_paths,
            environment_docs=None
            if self.adaptive or self.request.environment_docs is None
            else self.request.environment_docs.text,
            probe=self.request.probe,
            editor_max_changed_files=self.request.editor_max_changed_files,
            session_limits=self._session_limits(),
        )
        result = await run_tool_loop(
            client=self.client,
            section=self.section,
            log=self.log,
            recorder=self.attempts,
            deadline=self.deadline,
            request_style=request_style,
            limits=LoopLimits(
                max_turns=self.max_turns,
                protocol_repairs=effective_protocol_repairs(self.section),
                tool_failures=effective_tool_failures(self.section),
                compact_chars=self.section.context_compact_chars,
                message_max_chars=self.section.context_message_max_chars,
            ),
            messages=messages,
            tools=self.tools,
            terminal_names=("finish_reflection",),
            run_tool=self._run_tool,
            terminal=self._finish,
            role=_ROLE,
            retry=self.section.retry if self.role_resilience else None,
            family15=self.family15,
            # The preflight's turns count toward `max_turns`.
            first_turn=self._turns,
            compact=_compact_messages,
        )
        assert isinstance(result, ReflectResult)
        return result

    def _finish(self, name: str, arguments: dict[str, Any], turn: int) -> Done | Repair | Correct:
        """The `finish_reflection` call: screened, checked and admitted, or refused.

        A leak ends the role at once with the raw groups (it outranks every
        repair, which would echo the arguments); a bounds or shape defect is a
        protocol repair; a repairable admission refusal is a correction that
        spends no repair budget.
        """

        if self.admission is not None:
            leaking = self._leaking_terminal(turn, arguments)
            if leaking is not None:
                # Leak screening outranks every repair: a protocol or
                # bounds repair would echo the arguments.
                return Done(leaking)
        schema = None
        if self.admission is not None:
            schema = adaptive_group_schema(
                probes=self.request.probe is not None,
                documents=self.adaptive and self.request.evidence.document_ids is not None,
            )
            normalized = drop_empty_unknown_keys(arguments.get("groups"), schema)
            if isinstance(normalized, tuple):
                arguments = {**arguments, "groups": list(normalized)}
        try:
            if self.adaptive:
                try:
                    if schema is None:
                        check_adaptive_group_bounds(arguments.get("groups"), self.group_text_chars)
                    else:
                        check_adaptive_group_bounds(
                            arguments.get("groups"), self.group_text_chars, schema=schema
                        )
                except GroupError as exc:
                    # The rejected arguments are otherwise lost:
                    # the private log is redacted through the run's secrets.
                    self._record_rejection(turn, exc.detail, arguments)
                    if not self.bounds_repair:
                        return Done(
                            ReflectResult("failed", (), "invalid_result", code="invalid_result")
                        )
                    return Repair(
                        f"{exc.correction}; call finish_reflection again"
                        if exc.correction
                        else f"finish_reflection: {exc.detail}; shorten or split the groups and call finish_reflection again"
                    )
            terminal = _terminal(arguments, probe=self.probe, adaptive=self.adaptive)
        except ToolError as exc:
            return Repair(str(exc))
        if self.admission is not None:
            try:
                self.admission(terminal)
            except GroupError as exc:
                self._record_rejection(turn, exc.detail, arguments, exc.correction)
                if exc.repair == "none":
                    # Unreachable after the screen above for equal
                    # inputs; kept so no leak can become a correction.
                    return Done(terminal)
                if exc.repair == "protocol":
                    return Repair(
                        f"finish_reflection: {exc.detail}; shorten or split the "
                        "groups and call finish_reflection again"
                    )
                self.last_rejection = exc.rejection()
                return Correct(exc.correction or exc.detail)
        self.log.record(
            {
                "phase": "tool_call",
                "turn": turn,
                "tool": name,
                "arguments": arguments,
                "result": {"finished": True},
            }
        )
        self.log.record(
            self._evidence_usage(self.listed_evidence, self.completed_evidence_ids, terminal.groups)
        )
        return Done(terminal)

    def _record_rejection(
        self,
        turn: int,
        error: str,
        arguments: Mapping[str, Any],
        correction: str | None = None,
    ) -> None:
        """The private, redacted record of a refused `finish_reflection`."""

        self.log.record(
            {
                "phase": "finish_reflection_rejected",
                "turn": turn,
                "error": error,
                **({"correction": correction} if correction is not None else {}),
                "arguments": arguments,
            }
        )

    def _leaking_terminal(self, turn: int, arguments: Mapping[str, Any]) -> ReflectResult | None:
        """A `finish_reflection` whose submitted groups leak, as the role's terminal.

        Run before the bounds check and the terminal's shape check, so a leak
        outranks every repair, also when the call is malformed or oversized.
        It needs only a `groups` list the closure's leak scans can read (an
        unreadable one keeps its protocol repair). A leak (`repair == "none"`)
        ends the role without a correction, with these raw groups: the
        campaign's final admission refuses them as the same leak and records
        the existing content failure, exactly as `2.0.0b11` did. Every other
        refusal is left to the ordinary checks that follow.
        """

        groups = arguments.get("groups")
        if self.admission is None or not isinstance(groups, list) or not groups:
            return None
        candidate = ReflectResult("reflected", tuple(groups), f"reflected {len(groups)} group(s)")
        try:
            (self.request.preview_admission or self.admission)(candidate)
        except GroupError as exc:
            if exc.repair != "none":
                return None
            self._record_rejection(turn, exc.detail, arguments)
            return candidate
        return None

    def _evidence_usage(
        self, listed: bool, read_ids: set[str], groups: Sequence[Mapping[str, Any]] = ()
    ) -> dict[str, Any]:
        """What the reflection actually read, as the editor's own audit records it."""

        if self.adaptive:
            selected: set[str] = set()
            for portion in self.request.evidence.payload["portion"]:
                selected.update(portion["exemplars"])
                if portion["same_case_success"] is not None:
                    selected.add(portion["same_case_success"])
            cited_ids = {
                value
                for group in groups
                for value in group.get("evidence_ids", ())
                if isinstance(value, str)
            }
            return {
                "phase": "evidence_usage",
                "catalog_listed": listed,
                "selected_ids": sorted(selected),
                "cited_ids": sorted(cited_ids),
                "read_ids": sorted(read_ids),
                "unread_citations": sorted(cited_ids - read_ids),
            }
        cited = {record.evidence_id for record in self.catalog if record.kind != "summary"}
        unread = sorted(cited - read_ids)
        return {
            "phase": "evidence_usage",
            "severity": "warning" if unread else "info",
            "catalog_listed": listed,
            "read_ids": sorted(read_ids),
            "unread_records": unread,
        }

    def _offered_tools(self, blocked: Collection[str] = ()) -> list[dict[str, Any]]:
        return [tool for tool in self.tools if tool["function"]["name"] not in blocked]

    # -- tools ------------------------------------------------------------

    async def _run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | ToolOutput:
        """One non-terminal call, and what it tells the session.

        A non-adaptive `read_evidence` without an offset continues from where
        the last read stopped; the offset is added to `arguments`, which the
        loop's record shows. Evidence progress counts only successful reads.
        """

        if not self.adaptive and name == "read_evidence" and "offset" not in arguments:
            evidence_id = arguments.get("evidence_id")
            if isinstance(evidence_id, str):
                arguments["offset"] = self.evidence_progress.get(evidence_id, 0)
        output = await self._dispatch(name, arguments)
        if name == "list_evidence":
            self.listed_evidence = True
        elif name == "read_evidence":
            evidence_id = str(arguments["evidence_id"])
            offset = arguments.get("offset", 0)
            if offset == self.evidence_progress.get(evidence_id, 0):
                next_offset = output["next_offset"]
                self.evidence_progress[evidence_id] = (
                    output["total_characters" if self.adaptive else "total_chars"]
                    if next_offset is None
                    else next_offset
                )
                if next_offset is None:
                    self.completed_evidence_ids.add(evidence_id)
        if name == "probe_request" and output.get("error") == "context_exhausted":
            self.probe_context_exhausted = True
            return ToolOutput(
                output,
                block=True,
                follow_up="Probe context is exhausted. Use evidence/document tools as needed, then finish_reflection.",
            )
        return output

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "probe_request":
            _expect(arguments, {"method", "path", "body"})
            if self.probe_context_exhausted:
                return {"error": "context_exhausted", "detail": "probe request context exhausted"}
            assert self.request.probe_session is not None
            return await self.request.probe_session.request(arguments)
        if self.adaptive and name in {
            "list_evidence",
            "read_evidence",
            "read_trace",
            "list_documents",
            "read_document",
            "list_history",
            "read_history",
        }:
            assert self.request.reader is not None
            try:
                return self.request.reader.dispatch(name, arguments)
            except ReaderArgumentError as exc:
                detail = f"{exc.code}: {exc.detail}" if self.family15 and exc.detail else exc.code
                raise ToolError(detail) from exc
        if name == "list_files":
            _expect(arguments, set())
            files = readable_files(self.root)
            return {
                "files": files,
                "file_summaries": [
                    _file_summary(self.root / relative, self.root) for relative in files
                ],
            }
        if name == "read_file":
            from evolution.tree.roles.config import effective_nearest_paths

            output, _written = read_file_tool(
                self.root,
                arguments,
                max_payload_bytes=self.section.budgets.max_payload_bytes,
                nearest_paths=effective_nearest_paths(self.section) if self.family15 else None,
            )
            return output
        if name == "list_evidence":
            _expect(arguments, set(), optional={"offset"})
            offset = _offset(arguments)
            records = _evidence_page(self.catalog, offset)
            next_offset = offset + len(records)
            return {
                "records": records,
                "next_offset": next_offset if next_offset < len(self.catalog) else None,
                "total": len(self.catalog),
            }
        if name == "read_evidence":
            _expect(arguments, {"evidence_id"}, optional={"offset"})
            offset = _offset(arguments)
            record = next(
                (item for item in self.catalog if item.evidence_id == arguments["evidence_id"]),
                None,
            )
            if record is None:
                raise ToolError("unknown evidence ID")
            content, following_offset = _evidence_chunk(record.content, offset)
            return {
                "id": record.evidence_id,
                "content": content,
                "next_offset": following_offset,
                "total_chars": len(record.content),
            }
        raise AssertionError(name)  # pragma: no cover - guarded by `_TOOL_NAMES`


def _terminal(arguments: dict[str, Any], *, probe: bool, adaptive: bool = False) -> ReflectResult:
    """The `finish_reflection` call's own result, claimed groups carried raw.

    Groups are returned exactly as the model wrote them: the campaign's
    `validate_groups` is the one gate that may reject them, and it holds the
    catalog, the corpus and the oracle needles this kind never sees. Only the
    call's *shape* is checked here -- enough to know which terminal was meant.

    The one exception is an *empty* `probe_refs` on a reflection that declared
    no probe. The key is not in that schema at all, so a model that writes it
    anyway is claiming nothing -- while the gate requires exactly the five
    non-probe keys and would fail the whole reflection `invalid_result` over a
    field carrying no claim. It is dropped here; a non-empty one is a claim
    about probes that did not run and is carried through raw, for the gate to
    refuse.
    """

    _expect(arguments, {"groups"}, optional={"declined"})
    groups = arguments["groups"]
    declined = arguments.get("declined", False)
    if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
        raise ToolError("groups must be an array of objects")
    if type(declined) is not bool:
        raise ToolError("declined must be a boolean")
    if not probe and not adaptive:
        groups = [
            {key: value for key, value in group.items() if key != "probe_refs"}
            if group.get("probe_refs") == []
            else group
            for group in groups
        ]
    if groups:
        return ReflectResult("reflected", tuple(groups), f"reflected {len(groups)} group(s)")
    if not declined:
        raise ToolError("finish_reflection without groups must set declined: true")
    return ReflectResult("declined", (), "declined")
