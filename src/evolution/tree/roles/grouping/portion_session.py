"""Map sessions of the family-15 chat Grouper (spec §6.2-§6.5, ADR-0055).

One portion is one tool session on the shared chat runner
(`chat_loop.run_tool_loop`): the portion input, the read tools of
`grouping_tools`, and `finish_portion` as the terminal. The session's own
controls are the runner's hooks:

* ``fit`` elides the oldest tool results so every request stays within
  ``max_input_chars`` (`elide_to_fit`, one `ToolReads` per session); a request
  that still does not fit is not sent and the session ends
  ``context_exhausted``. It also counts each request against the grouping's
  `RequestBudget` (``max_requests``) and checks cancellation;
* ``inspect`` (`cut_answer`) ends the session on an answer cut at the output
  limit, before any repair;
* ``screen`` scans each response's text for a run secret before the first
  record that holds it; a secret-bearing response is logged redacted and
  raises ``GroupingExecutionError("secret_detected")``. It also tags every
  record of the loop with the portion id, since sessions share one log;
* ``session_timeout``: ``timeout_seconds`` bounds one session (§6.2).

The terminal expands the answer's references (`parse_portion_answer`). A
defect goes back through the runner's protocol repair, within
`effective_grouper_repairs`, with o-refs in its text; once that budget is
spent, the answer's valid groups are admitted and the rest of the portion is
uncovered, as `/15` keeps a chunk's valid groups after its repairs.
Admission then applies today's map rules to the portion: `resolve_overlaps`,
the secret scan of every group text and `_scrub`.

`map_portions` runs the plan under ``section.concurrency``. A cut answer,
``context_exhausted``, a ``session_timeout`` or a checkpoint over the record
budget splits the portion (`split_portion`) and runs both halves. A
one-observation portion that still fails is ``portion_unsplittable``, except a
``session_timeout``, which is the provider's failure: ``deadline_exhausted``
(Builder decision 9). The first provider failure stops new sessions and
cancels the running ones (Builder decision 10); every portion finished before
it was already reported through ``on_finished``, the checkpoint callback.

This module is in `roles/` and imports nothing from the campaign (ADR-0045).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from evolution.tree.execution.check_modes import (
    check_field,
    grouping_field_key,
    scan_grouping_wire,
)
from evolution.tree.roles.chat_loop import (
    Done,
    LoopHooks,
    LoopLimits,
    LoopStop,
    Repair,
    SessionTimeout,
    ToolReads,
    cut_answer,
    elide_to_fit,
    run_tool_loop,
)
from evolution.tree.roles.config import (
    ChatEditorSection,
    effective_grouper_repairs,
    effective_tool_failures,
)
from evolution.tree.roles.editor.chat_editor import EditFailure
from evolution.tree.roles.grouping.grouping_portions import (
    MapPortion,
    PortionPlan,
    RefTable,
    split_portion,
)
from evolution.tree.roles.grouping.grouping_tools import GroupingToolbox, grouping_tools
from evolution.tree.roles.grouping.model_grouping import (
    ModelGroup,
    ParsedResponse,
    _named,
    parse_portion_answer,
    resolve_overlaps,
)
from evolution.tree.roles.grouping.model_grouping_runtime import (
    _bounded_defects,
    _model_strings,
    _scrub,
)
from evolution.tree.roles.grouping.reflect_grouping import _canonical_json
from evolution.tree.roles.role_call import AttemptRecorder, RoleCallExhausted
from evolution.tree.roles.role_failures import GroupingExecutionError

if TYPE_CHECKING:
    from evolution.tree.roles.config import GroupingV2Limits, ModelGroupingSection
    from evolution.tree.roles.reflection_reader import ReflectionReadSession
    from evolution.tree.roles.workspace import AttemptLog

__all__ = [
    "OUTCOMES",
    "PORTION_LEAD",
    "MAX_REQUESTS",
    "MaxRequestsReached",
    "PortionContext",
    "PortionOutcome",
    "PortionSplit",
    "RequestBudget",
    "chat_section",
    "checkpoint_body",
    "checkpoint_size",
    "group_record",
    "map_portions",
    "model_group",
    "run_portion",
]

#: The role name of the Grouper's provider requests, as `/15` records them.
_ROLE: Final = "grouping"
#: The user message of a portion session is this lead plus `render_portion`.
PORTION_LEAD: Final = (
    "One portion of failing observations to group. Every o<n> listed under units, "
    "by itself or through its set, must be in exactly one group; call "
    "finish_portion with the groups.\n"
)
_PORTION_REPAIR: Final = (
    "finish_portion rejected: {defects}. Every failing observation of this portion "
    "(each o<n> under units, by itself or through its s<n>) must be in exactly one "
    "group, exactly once, and only references of this portion may be used. Call "
    "finish_portion again with the complete corrected groups, not a patch."
)
#: Outcomes a portion's checkpoint records when it admitted no group (§9).
OUTCOMES: Final = frozenset(
    {"turns_exhausted", "context_exhausted", "portion_unsplittable", "invalid_output"}
)
#: A portion `max_requests` left unsent. Never checkpointed: it is not a
#: model decision, and `on_finished` never sees it.
MAX_REQUESTS: Final = "max_requests"
#: Why a portion splits, and the limit its `portion_unsplittable` names.
_SPLIT_LIMITS: Final = {
    "cut": "max_output_tokens",
    "context_exhausted": "max_input_chars",
    "record_budget": "record_budget",
}


@dataclass(frozen=True)
class PortionOutcome:
    """One portion's admitted groups, or why it admitted none.

    ``groups`` are `group_record` mappings with full evidence-ID members.
    ``outcome`` is `None` when groups were admitted, one of `OUTCOMES`
    otherwise, or `MAX_REQUESTS` for a portion never finished. ``requests``
    and ``repairs`` are this session's own.
    """

    portion_id: str
    groups: tuple[dict[str, Any], ...]
    outcome: str | None
    requests: int
    repairs: int


class RequestBudget:
    """Requests and protocol repairs of one grouping, with its optional cap.

    Every session's requests count, split ones included, so the totals are
    `calls`' ``map_requests`` and ``repairs`` (Builder decision 7). The
    caller counts its own preflight with `take` (Builder decision 2). Once
    ``cap`` requests were taken, `take` raises `MaxRequestsReached` and no
    further request is sent (§10).
    """

    def __init__(self, cap: int | None = None) -> None:
        self.cap = cap
        self.requests = 0
        self.repairs = 0

    @property
    def exhausted(self) -> bool:
        return self.cap is not None and self.requests >= self.cap

    def take(self) -> None:
        if self.exhausted:
            raise MaxRequestsReached
        self.requests += 1


class MaxRequestsReached(Exception):
    """``max_requests`` was reached; the request was not sent."""


class PortionSplit(Exception):
    """A session ended in a way that splits its portion (§6.5).

    ``reason`` is ``cut``, ``context_exhausted``, ``session_timeout`` or
    ``record_budget``; the counts are the ended session's.
    """

    def __init__(self, reason: str, requests: int, repairs: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.requests = requests
        self.repairs = repairs


def _now_monotonic() -> float:
    return time.monotonic()


def _as_is(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class PortionContext:
    """Everything a portion session needs, shared by one grouping's sessions.

    ``system`` is the map part of the prompt; ``render`` is the planner's
    `render_portion` over this grouping's headers, glimpses, representatives
    and contrasts. ``request_style`` was negotiated once per grouping (Builder
    decision 2). ``reported`` maps an evidence ID to its reported component
    IDs as the case headers show them. ``record_budget`` bounds a portion's
    `checkpoint_size`. ``session_deadline()`` returns a new session's
    absolute `time.monotonic()` deadline, the clock the runner reads.
    ``normalize`` maps an admitted group record to the form its checkpoint
    persists (PDR-0044: run secrets as ``[REDACTED]``); that form is sized,
    checkpointed and used, so a resumed grouping reads what this one did.
    """

    client: Any
    section: ModelGroupingSection
    log: AttemptLog
    recorder: AttemptRecorder | None
    limits: GroupingV2Limits
    request_style: str
    system: str
    render: Callable[[MapPortion], str]
    reader: ReflectionReadSession
    table: RefTable
    glimpses: Mapping[str, str]
    reported: Mapping[str, frozenset[str]]
    record_budget: int
    session_deadline: Callable[[], float]
    forbidden_secret: Callable[[str], bool]
    oracle_needle: Callable[[str], bool]
    cancelled: Callable[[], bool] = lambda: False
    budget: RequestBudget = field(default_factory=RequestBudget)
    clock: Callable[[], float] = _now_monotonic
    normalize: Callable[[Any], Any] = _as_is


def chat_section(section: ModelGroupingSection) -> ChatEditorSection:
    """The grouping section as the shared runner's provider layer reads it.

    The provider pin, reasoning effort, output limit, TLS setting and retry
    policy are the Grouper's own; the runner's compaction keys are unused
    (sessions pass ``compact=None`` and fit by elision).
    """

    return ChatEditorSection(
        model=section.model,
        base_url=section.base_url,
        api_key_env=section.api_key_env,
        proxy_env=section.proxy_env,
        reasoning_effort=section.reasoning_effort,
        provider_only=section.provider_only,
        max_output_tokens=section.max_output_tokens,
        verify_ssl_certs=section.verify_ssl_certs,
        retry=section.retry,
        max_protocol_repairs=section.max_repairs,
        provider_order=section.provider_order,
        allow_fallbacks=section.allow_fallbacks,
        require_parameters=section.require_parameters,
    )


# --- Checkpoint records ---------------------------------------------------------


def group_record(group: ModelGroup, provenance: str | None = None) -> dict[str, Any]:
    """An admitted group as its portion checkpoint holds it, with full evidence IDs."""

    record: dict[str, Any] = {
        "id": group.local_id,
        "title": group.title,
        "failure_mode": group.failure_mode,
        "expected_behaviour": group.expected_behaviour,
        "question": group.question,
        "priority": group.priority,
        "citations": list(group.citations),
        "metrics": dict(group.metrics),
        "members": [
            {"evidence_id": evidence_id, "variant": variant}
            for evidence_id, variant in group.members
        ],
        "affected_cases": list(group.affected_cases),
    }
    if provenance is not None:
        record["provenance"] = provenance
    return record


def model_group(record: Mapping[str, Any], case_of: Mapping[str, str]) -> ModelGroup:
    """The `ModelGroup` of a `group_record`; ``case_of`` maps evidence IDs to cases."""

    members = tuple((m["evidence_id"], m["variant"]) for m in record["members"])
    member_cases = tuple(case_of[evidence_id] for evidence_id, _ in members)
    return ModelGroup(
        local_id=record["id"],
        title=record["title"],
        failure_mode=record["failure_mode"],
        expected_behaviour=record["expected_behaviour"],
        question=record["question"],
        members=members,
        affected_cases=tuple(sorted(set(member_cases))),
        citations=tuple(record["citations"]),
        priority=record["priority"],
        metrics=tuple(sorted(record["metrics"].items())),
        member_cases=member_cases,
    )


def checkpoint_body(outcome: PortionOutcome) -> dict[str, Any]:
    """The variable part of a `grouping_portion_finished` record (§9).

    Its groups, or its outcome when it admitted none, and its request count.
    The journal envelope and the grouping id and digests are the rest of the
    record, which `CHECKPOINT_ENVELOPE_BYTES` reserves.
    """

    body: dict[str, Any] = {"portion_id": outcome.portion_id, "requests": outcome.requests}
    if outcome.groups:
        body["groups"] = list(outcome.groups)
    else:
        body["outcome"] = outcome.outcome
    return body


def checkpoint_size(outcome: PortionOutcome) -> int:
    """Bytes of `checkpoint_body` as canonical JSON, measured against ``record_budget``."""

    return len(_canonical_json(checkpoint_body(outcome)))


# --- One session ------------------------------------------------------------------


def _model_texts(record: Mapping[str, Any]) -> Iterable[str]:
    """The model's text in a `model_response` audit: content and call arguments."""

    content = record.get("content")
    if isinstance(content, str):
        yield content
    for call in record.get("calls") or ():
        arguments = call.get("arguments") if isinstance(call, Mapping) else None
        if isinstance(arguments, str):
            yield arguments
        elif arguments is not None:
            yield json.dumps(arguments, ensure_ascii=False, default=str)


class _Session:
    """The hooks and the terminal of one portion session, and its counts."""

    def __init__(self, ctx: PortionContext, portion: MapPortion, limits: LoopLimits) -> None:
        self.ctx = ctx
        self.portion = portion
        self.limits = limits
        self.reads = ToolReads()
        self.requests = 0
        self.repairs = 0

    def fit(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        if self.ctx.cancelled():
            raise GroupingExecutionError("cancelled")
        fitted = elide_to_fit(
            messages, tools, limit=self.ctx.section.max_input_chars, reads=self.reads
        )
        if fitted is None:
            return None
        self.ctx.budget.take()
        self.requests += 1
        return fitted

    def screen(self, record: dict[str, Any]) -> dict[str, Any]:
        phase = record.get("phase")
        if phase == "model_response":
            for text in _model_texts(record):
                if scan_grouping_wire(
                    self.ctx.forbidden_secret,
                    text,
                    decoded_enforcement=False,
                    locate=self.locations,
                ):
                    # Written here: the runner writes nothing for a raising screen.
                    self.ctx.log.record(
                        {
                            "phase": "model_response",
                            "portion": self.portion.id,
                            "turn": record.get("turn"),
                            "request_style": record.get("request_style"),
                            "response_id": record.get("response_id"),
                            "finish_reason": record.get("finish_reason"),
                            "usage": record.get("usage"),
                            "content": "[redacted]",
                            "calls": [],
                        }
                    )
                    raise GroupingExecutionError("secret_detected")
        elif phase == "protocol_repair_requested":
            self.repairs += 1
            self.ctx.budget.repairs += 1
        return {**record, "portion": self.portion.id}

    def locations(self, value: Mapping[str, Any]) -> dict[tuple[str, ...], str]:
        """The check locations of a `finish_portion` answer's fields.

        As `check_modes.grouping_field_locations` names a `/15` answer's, with
        each member reference expanded to its evidence IDs, so a text the
        screen scans here and admission scans again is one field, counted
        once in a shadow mode. A set's variant is located at its first member.
        """

        refs = self.portion.references()
        observations = self.ctx.table.observations
        locations: dict[tuple[str, ...], str] = {}
        groups = value.get("groups")
        for index, group in enumerate(groups if isinstance(groups, list) else ()):
            if not isinstance(group, Mapping):
                continue
            members = group.get("members")
            members = members if isinstance(members, list) else []
            expanded: list[list[str]] = []
            for member in members:
                ref = member.get("ref") if isinstance(member, Mapping) else None
                indices = refs.get(ref, ()) if isinstance(ref, str) else ()
                expanded.append([observations[i] for i in indices])
            ids = [evidence_id for eids in expanded for evidence_id in eids]
            key = grouping_field_key(ids) if ids else f"unknown/{index}"
            at = ("groups", str(index))
            for field_name in ("title", "failure_mode", "expected_behaviour", "question"):
                kind = "label" if field_name == "title" else "description"
                locations[(*at, field_name)] = f"{kind}/{key}"
            locations[(*at, "priority")] = f"priority/{key}"
            locations[(*at, "provenance")] = f"provenance/{key}"
            citations = group.get("citations")
            for position in range(len(citations) if isinstance(citations, list) else 0):
                locations[(*at, "citations", str(position))] = f"citation/{key}/{position}"
            for position, eids in enumerate(expanded):
                if eids:
                    locations[(*at, "members", str(position), "variant")] = f"variant/{eids[0]}"
        return locations

    def terminal(self, name: str, arguments: dict[str, Any], turn: int) -> Done | Repair:
        defects = [_named("unexpected_key", key) for key in sorted(set(arguments) - {"groups"})]
        parsed = parse_portion_answer(
            arguments.get("groups"),
            portion=self.portion,
            table=self.ctx.table,
            reported=self.ctx.reported,
        )
        defects += parsed.defects
        self.ctx.log.record(
            {
                "phase": "portion_answer",
                "portion": self.portion.id,
                "turn": turn,
                "groups": len(parsed.groups),
                "defects": list(defects),
            }
        )
        if defects and self.repairs < self.limits.protocol_repairs:
            text, _ = _bounded_defects(defects)
            return Repair(_PORTION_REPAIR.format(defects=text))
        return Done(parsed)


def _provider_exhaustion(exc: EditFailure) -> RoleCallExhausted | None:
    """The retry layer's exhaustion behind a provider `EditFailure`, if it is one."""

    if exc.code not in ("provider_exhausted", "provider_rejected"):
        return None
    cause = exc.__cause__
    if isinstance(cause, RoleCallExhausted):
        return cause
    reason = "provider_rejected" if exc.code == "provider_rejected" else "provider_unavailable"
    return RoleCallExhausted(reason, 0, exc.code)


def _admit(
    ctx: PortionContext, portion: MapPortion, parsed: ParsedResponse
) -> tuple[dict[str, Any], ...]:
    """Today's map rules over one portion's parsed answer (§6.4).

    Overlaps go to the group ranking first; each group gets a portion-scoped
    id (``p3.1-g1``) so groups of different portions never share one. A run
    secret in any group text is an integrity failure; a text or variant with
    an oracle needle is scrubbed, and a needled provenance hint is dropped.
    """

    hints = dict(parsed.provenance)
    records: list[dict[str, Any]] = []
    for group in resolve_overlaps(parsed.groups):
        provenance = hints.get(group.local_id)
        group = dataclasses.replace(group, local_id=f"{portion.id}-{group.local_id}")
        strings = _model_strings(group)
        if provenance is not None:
            key = grouping_field_key([evidence_id for evidence_id, _ in group.members])
            strings.append((f"provenance/{key}", provenance))
        for location, text in strings:
            with check_field(location):
                if ctx.forbidden_secret(text):
                    raise GroupingExecutionError("secret_detected")
        if provenance is not None and ctx.oracle_needle(provenance):
            provenance = None
        records.append(group_record(_scrub(group, ctx.oracle_needle), provenance))
    return tuple(records)


async def run_portion(ctx: PortionContext, portion: MapPortion) -> PortionOutcome:
    """One map session over ``portion``; its admitted groups or why it has none.

    Returns ``turns_exhausted`` when the session reached ``max_turns``
    without an admitted answer, and ``invalid_output`` when its protocol
    repairs ran out or no group of its answer was valid. Raises `PortionSplit`
    for an ending that splits the portion (a cut answer, ``context_exhausted``,
    ``session_timeout``, or admitted groups whose checkpoint would exceed
    ``record_budget``), `MaxRequestsReached` when ``max_requests`` stopped
    it, the retry layer's `RoleCallExhausted` for a provider failure, and
    `GroupingExecutionError` for a secret or a cancellation.
    """

    if ctx.cancelled():
        raise GroupingExecutionError("cancelled")
    if ctx.budget.exhausted:
        raise MaxRequestsReached
    section = chat_section(ctx.section)
    limits = LoopLimits(
        max_turns=ctx.limits.max_turns,
        protocol_repairs=effective_grouper_repairs(ctx.section),
        tool_failures=effective_tool_failures(section),
        compact_chars=ctx.section.max_input_chars,
        message_max_chars=ctx.section.max_input_chars,
    )
    session = _Session(ctx, portion, limits)
    toolbox = GroupingToolbox(
        portion, ctx.table, ctx.reader, read_chars=ctx.limits.read_chars, glimpses=ctx.glimpses
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ctx.system},
        {"role": "user", "content": PORTION_LEAD + ctx.render(portion)},
    ]
    observations = sum(len(unit.observations) for unit in portion.units)
    ctx.log.record(
        {
            "phase": "portion_started",
            "portion": portion.id,
            "units": len(portion.units),
            "observations": observations,
        }
    )
    started = ctx.clock()

    def finished(groups: tuple[dict[str, Any], ...], outcome: str | None) -> PortionOutcome:
        # The persisted form, before the budget measures it (PDR-0044).
        groups = tuple(ctx.normalize(group) for group in groups)
        result = PortionOutcome(portion.id, groups, outcome, session.requests, session.repairs)
        if groups and checkpoint_size(result) > ctx.record_budget:
            raise split("record_budget")
        ctx.log.record(
            {
                "phase": "portion_finished",
                "portion": portion.id,
                "outcome": outcome,
                "groups": len(groups),
                "requests": session.requests,
                "repairs": session.repairs,
                "seconds": round(ctx.clock() - started, 3),
            }
        )
        return result

    def split(reason: str) -> PortionSplit:
        return PortionSplit(reason, session.requests, session.repairs)

    try:
        parsed = await run_tool_loop(
            client=ctx.client,
            section=section,
            log=ctx.log,
            recorder=ctx.recorder,
            deadline=ctx.session_deadline(),
            request_style=ctx.request_style,
            limits=limits,
            messages=messages,
            tools=grouping_tools(traces=toolbox.traces),
            terminal_names=("finish_portion",),
            run_tool=toolbox.run,
            terminal=session.terminal,
            hooks=LoopHooks(
                fit=session.fit,
                inspect=cut_answer,
                screen=session.screen,
                session_timeout=True,
                reads=session.reads,
            ),
            role=_ROLE,
            retry=ctx.section.retry,
            family15=True,
            compact=None,
        )
    except LoopStop as exc:
        raise split(exc.reason) from exc
    except SessionTimeout as exc:
        raise split("session_timeout") from exc
    except EditFailure as exc:
        exhausted = _provider_exhaustion(exc)
        if exhausted is not None:
            raise exhausted from exc
        if exc.klass == "turns_exhausted":
            return finished((), "turns_exhausted")
        if exc.klass == "protocol":
            return finished((), "invalid_output")
        raise
    assert isinstance(parsed, ParsedResponse)
    groups = _admit(ctx, portion, parsed)
    return finished(groups, None if groups else "invalid_output")


# --- The map over a plan ------------------------------------------------------------


class _Stopped(Exception):
    """A session not started because another one failed."""


def _unsplittable(portion: MapPortion) -> bool:
    """Whether `split_portion` refuses ``portion``: one observation is left."""

    try:
        split_portion(portion)
    except ValueError as exc:
        return str(exc) == "portion_unsplittable"
    return False


def _first_failure(failures: Sequence[BaseException]) -> BaseException | None:
    """The failure that ends the map: integrity, then any other grouping
    error, then an unexpected one, then a provider exhaustion (as `_map_all`)."""

    real = [failure for failure in failures if not isinstance(failure, _Stopped)]
    for failure in real:
        if isinstance(failure, GroupingExecutionError) and failure.code == "secret_detected":
            return failure
    for failure in real:
        if isinstance(failure, GroupingExecutionError):
            return failure
    for failure in real:
        if not isinstance(failure, RoleCallExhausted):
            return failure
    return real[0] if real else None


async def map_portions(
    ctx: PortionContext,
    plan: PortionPlan,
    *,
    adopted: Mapping[str, PortionOutcome],
    on_finished: Callable[[PortionOutcome], None],
) -> tuple[PortionOutcome, ...]:
    """Every portion's outcome, halves in place of a split portion, in plan order.

    ``adopted`` holds checkpointed outcomes by portion id; such a portion is
    not requested again. A portion with an adopted half (``p3.1``) was split
    before, so it is split again without a request: splitting is
    deterministic. ``on_finished`` sees each newly finished portion once, as
    it finishes, before any later failure can end the map; an adopted one and
    a `MAX_REQUESTS` one never.
    """

    semaphore = asyncio.Semaphore(ctx.section.concurrency)
    split_before = {key.rsplit(".", 1)[0] for key in adopted if "." in key}
    for key in list(split_before):
        while "." in key:
            key = key.rsplit(".", 1)[0]
            split_before.add(key)
    stopped = False

    async def session(portion: MapPortion) -> PortionOutcome:
        nonlocal stopped
        async with semaphore:
            # A released slot must not start a session after another one failed.
            if stopped:
                raise _Stopped
            try:
                return await run_portion(ctx, portion)
            except PortionSplit as exc:
                if exc.reason == "session_timeout" and _unsplittable(portion):
                    # `process` raises this as a provider failure (Builder
                    # decision 9): stop before the slot is released, so a
                    # queued sibling never starts (decision 10).
                    stopped = True
                raise
            except MaxRequestsReached:
                raise
            except BaseException:
                stopped = True
                raise

    def unsplittable(portion: MapPortion, reason: str, ended: PortionSplit) -> PortionOutcome:
        limit = _SPLIT_LIMITS[reason]
        value = ctx.record_budget if limit == "record_budget" else getattr(ctx.section, limit)
        ctx.log.record(
            {
                "phase": "portion_unsplittable",
                "portion": portion.id,
                "reason": reason,
                "limit": {limit: value},
            }
        )
        return PortionOutcome(portion.id, (), "portion_unsplittable", ended.requests, ended.repairs)

    async def process(portion: MapPortion) -> list[PortionOutcome]:
        if portion.id in adopted:
            return [adopted[portion.id]]
        reason = "adopted_split"
        ended = PortionSplit(reason, 0, 0)
        if portion.id not in split_before:
            try:
                outcome = await session(portion)
            except PortionSplit as exc:
                reason, ended = exc.reason, exc
            except MaxRequestsReached:
                ctx.log.record({"phase": "max_requests_reached", "portion": portion.id})
                return [PortionOutcome(portion.id, (), MAX_REQUESTS, 0, 0)]
            else:
                on_finished(outcome)
                return [outcome]
        try:
            halves = split_portion(portion)
        except ValueError as exc:
            if str(exc) != "portion_unsplittable":
                raise
            if reason == "session_timeout":
                ctx.log.record(
                    {
                        "phase": "portion_deadline_exhausted",
                        "portion": portion.id,
                        "timeout_seconds": ctx.section.timeout_seconds,
                    }
                )
                raise RoleCallExhausted(
                    "deadline_exhausted", ended.requests, "session_timeout"
                ) from None
            outcome = unsplittable(portion, reason, ended)
            on_finished(outcome)
            return [outcome]
        ctx.log.record(
            {
                "phase": "portion_split",
                "portion": portion.id,
                "reason": reason,
                "halves": [half.id for half in halves],
                "requests": ended.requests,
                "repairs": ended.repairs,
            }
        )
        tasks = [asyncio.create_task(process(half)) for half in halves]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return [outcome for result in results for outcome in result]

    async def top(portion: MapPortion) -> list[PortionOutcome]:
        nonlocal stopped
        try:
            return await process(portion)
        except BaseException:
            stopped = True
            raise

    tasks = [asyncio.create_task(top(portion)) for portion in plan.portions]
    try:
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    failures = [
        failure
        for task in tasks
        if not task.cancelled() and (failure := task.exception()) is not None
    ]
    first = _first_failure(failures)
    if first is not None:
        raise first
    return tuple(outcome for task in tasks for outcome in task.result())
