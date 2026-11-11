"""The `model_grouping/2` runtime of the family-15 chat Grouper (b16 spec §3, §8, §10; ADR-0055).

`run_model_grouping_v2` is the `/16` counterpart of
`model_grouping_runtime.run_model_grouping`. It composes the pieces in the
order of spec §3:

1. code builds the reference table, glimpses, case headers, sets and the
   portion plan (`grouping_portions`), and writes the table to the private
   log as one ``ref_registry`` record before any request;
2. the tool dialect is negotiated once per grouping (Builder decision 2),
   with ``list_case`` as the probe; the preflight counts toward
   ``max_requests`` and toward no session's ``max_turns``;
3. the map runs one tool session per portion (`portion_session.map_portions`),
   each portion checkpointed through the caller's `CheckpointSink`;
4. consolidation compares the groups of different portions in bounded
   batches (`consolidation.consolidate`), each batch one request without
   tools on `/15`'s text-request path, checkpointed the same way;
5. `admit_model_groups` admits the result with today's fallback, scrub and
   secret rules: an observation no admitted group covers takes the declared
   fallback (scope ``uncovered``, or ``all`` when no group covers anything).

`timeout_seconds` (``session_seconds``) bounds one map session, the preflight
or one batch with its repairs, never the whole operation (§6.2), so the total
time grows with the data. ``max_requests`` caps all requests of the grouping:
once reached nothing more is sent, unadmitted portions are uncovered,
consolidation stops at its level, and the log says ``max_requests_reached``.

Failures follow family 14's routes (ADR-0034): a provider exhaustion or
rejection, a pinned provider that answered as another one, and a
one-observation session timeout (``deadline_exhausted``, Builder decision 9)
raise `GrouperRoleFailure` with class ``provider``; portions checkpointed
before it stay for the resume to adopt (§9). A run secret in any model text is
`GroupingExecutionError("secret_detected")`, a stop is ``cancelled``. An
input budget the settings leave no room for, or a unit too large for any
portion, is the fallback ``("all", "input_too_large")``; a preflight the model
never answers with the probe call is ``("all", "invalid_output")``.

`calls` keeps `/15`'s closed shape (Builder decision 7): ``map_requests``
counts the requests of every portion session, never the preflight or a
batch; ``reduce`` is consolidation's state; ``attempts`` counts every
provider attempt and ``repairs`` every protocol repair.

This module is in `roles/` and imports nothing from the campaign (ADR-0045):
the caller passes the checkpoint record budget, the run-secret redaction of
component IDs and the reported provenance.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from evolution.tree.execution.check_modes import check_field, scan_grouping_wire
from evolution.tree.roles.chat_loop import LoopStop, chat_request_size, elide_to_fit
from evolution.tree.roles.config import effective_grouper_repairs
from evolution.tree.roles.editor.chat_editor import (  # noqa: PLC2701 - one provider layer
    EditFailure,
    negotiate_style,
    record_request,
)
from evolution.tree.roles.grouping import model_grouping_runtime
from evolution.tree.roles.grouping.consolidation import (
    _HEADER_KEYS as _MERGE_HEADER_KEYS,
)
from evolution.tree.roles.grouping.consolidation import (
    INVALID,
    BatchAnswer,
    BatchCheckpoint,
    Card,
    ConsolidationLimits,
    batch_input,
    card_evidence,
    consolidate,
    normalized_answer,
)
from evolution.tree.roles.grouping.grouping_portions import (
    MapPortion,
    PortionPlan,
    RefTable,
    build_units,
    case_header,
    glimpse,
    plan_portions,
    provenance_counts,
    reference_table,
    render_portion,
    representative_payload,
    split_portion,
)
from evolution.tree.roles.grouping.grouping_tools import RESULT_ENVELOPE_CHARS, grouping_tools
from evolution.tree.roles.grouping.model_grouping import (
    CaseProfile,
    InputTooLarge,
    ModelGroup,
    build_case_profiles,
    parse_reduce_response,
    resolve_overlaps,
)
from evolution.tree.roles.grouping.model_grouping_runtime import (
    _FRESH_NOTE,
    _REDUCE_REPAIR,
    REPAIR_RESERVE,
    Fallback,
    ModelGroupingOutcome,
    _bounded_defects,
    _close,
    _close_client,
    _DeadlineElapsed,
    _provider_failure,
    _scrub,
    _Session,
    admit_model_groups,
    split_prompt,
)
from evolution.tree.roles.grouping.portion_session import (
    PORTION_LEAD,
    MaxRequestsReached,
    PortionContext,
    PortionOutcome,
    RequestBudget,
    _model_texts,
    chat_section,
    map_portions,
    model_group,
)
from evolution.tree.roles.grouping.reflect_grouping import GroupingRegistry, _digest
from evolution.tree.roles.role_call import AttemptRecorder, RoleCallExhausted
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError

if TYPE_CHECKING:
    from evolution.tree.roles.config import GroupingV2Limits, ModelGroupingSection
    from evolution.tree.roles.editor.chat_editor import ChatCredentials
    from evolution.tree.roles.reflection_reader import ReflectionReadSession
    from evolution.tree.roles.workspace import AttemptLog

__all__ = [
    "Adopted",
    "AdoptedBatch",
    "AdoptedPortion",
    "CheckpointSink",
    "input_budget",
    "portion_digest",
    "run_model_grouping_v2",
]

_ROLE: Final = "grouping"
_PORTION_FORMAT: Final = "grouping_portion_input/1"
_PREFLIGHT: Final = "Call list_case now with case {case}; do not group yet."
#: EditFailure codes of the shared provider layer that are the provider's
#: failure, not the content's: they suspend as family 14's do (ADR-0034).
_PROVIDER_CODES: Final = frozenset(
    {"provider_exhausted", "provider_rejected", "provider_changed", "provider_error"}
)


class CheckpointSink(Protocol):
    """Where each model decision is checkpointed before anything uses it (§9).

    ``portion`` sees every newly finished portion, as it finishes, with the
    plan digest and its own input digest (`portion_digest`). ``batch`` sees
    every newly finished leaf batch: its level, its id (``b1``, ``b1.2``),
    its answer, the plan digest and its input digest. An adopted one is never
    passed again.
    """

    def portion(self, outcome: PortionOutcome, plan_digest: str, input_digest: str) -> None: ...

    def batch(
        self, level: int, index: str, answer: BatchAnswer, plan_digest: str, input_digest: str
    ) -> None: ...


class _NoCheckpoints:
    def portion(self, outcome: PortionOutcome, plan_digest: str, input_digest: str) -> None:
        return None

    def batch(
        self, level: int, index: str, answer: BatchAnswer, plan_digest: str, input_digest: str
    ) -> None:
        return None


@dataclass(frozen=True)
class AdoptedPortion:
    """A checkpointed portion with the digests it was recorded under."""

    plan_digest: str
    input_digest: str
    outcome: PortionOutcome


@dataclass(frozen=True)
class AdoptedBatch:
    """A checkpointed leaf batch with the plan digest it was recorded under."""

    plan_digest: str
    checkpoint: BatchCheckpoint


@dataclass(frozen=True)
class Adopted:
    """The checkpoints of an interrupted grouping offered for adoption (§9).

    A portion is replayed only when its plan digest equals this grouping's
    and its input digest equals the digest of its input now; a batch only
    when its plan digest equals this grouping's and its input digest equals
    the batch's (`consolidation.consolidate`). Nothing is adopted across a
    different plan. Everything else is requested again.
    """

    portions: Mapping[str, AdoptedPortion] = field(default_factory=dict)
    batches: Mapping[tuple[int, str], AdoptedBatch] = field(default_factory=dict)


def portion_digest(text: str) -> str:
    """The input digest of a portion: the digest of its user message."""

    return _digest({"format": _PORTION_FORMAT, "input": text})


def input_budget(
    section: ModelGroupingSection, limits: GroupingV2Limits, system: str
) -> tuple[int, int]:
    """``(input_budget, reserve)`` of a portion's rendered input (§6.2).

    The reserve is what a session adds around the portion: the fixed opening
    every request carries (the system message and the portion lead with
    every tool, as `chat_request_size` counts them), plus one tool result of
    ``read_chars`` with its envelope and the repair reserve. Escape-heavy
    results and long protocol corrections are not reserved for: the session
    elides the oldest tool results, and a request that still does not fit
    ends ``context_exhausted`` and splits the portion (§6.5).
    """

    tools = grouping_tools(traces=True)
    opening = [{"role": "system", "content": system}, {"role": "user", "content": PORTION_LEAD}]
    reserve = (
        chat_request_size(opening, tools)
        + limits.read_chars
        + RESULT_ENVELOPE_CHARS
        + REPAIR_RESERVE
    )
    return section.max_input_chars - reserve, reserve


class _Log:
    """The private log as one grouping writes it: counts provider attempts.

    `calls`' ``attempts`` is the number of ``provider_attempt`` records,
    whichever path (the shared runner or `/15`'s text request) wrote them.
    """

    def __init__(self, log: AttemptLog) -> None:
        self._log = log
        self.attempts = 0

    def record(self, value: Mapping[str, Any]) -> None:
        if value.get("phase") == "provider_attempt":
            self.attempts += 1
        self._log.record(value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._log, name)


class _ScreenedLog:
    """The preflight's log: a response carrying a run secret is written
    redacted and raises ``secret_detected``, as a portion session's does."""

    def __init__(self, log: _Log, forbidden_secret: Callable[[str], bool]) -> None:
        self._log = log
        self._forbidden = forbidden_secret

    def record(self, value: Mapping[str, Any]) -> None:
        if value.get("phase") == "model_response":
            for text in _model_texts(value):
                if scan_grouping_wire(self._forbidden, text, decoded_enforcement=False):
                    self._log.record(
                        {**value, "stage": "preflight", "content": "[redacted]", "calls": []}
                    )
                    raise GroupingExecutionError("secret_detected")
        self._log.record({**value, "stage": "preflight"})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._log, name)


class _PreflightFailed(Exception):
    """The model never answered the preflight with the probe call."""


def _portion_named(plan: PortionPlan, portion_id: str) -> MapPortion | None:
    """The plan's portion ``portion_id``, deriving a half (``p3.1``) by splitting."""

    top, *halves = portion_id.split(".")
    portion = next((p for p in plan.portions if p.id == top), None)
    for half in halves:
        if portion is None or half not in ("1", "2"):
            return None
        try:
            portion = split_portion(portion)[int(half) - 1]
        except ValueError:
            return None
    return portion


def _needs_session(portion: MapPortion, adopted: Mapping[str, PortionOutcome]) -> bool:
    """Whether a portion, or a half of it, is not adopted and must be asked."""

    if portion.id in adopted:
        return False
    prefix = portion.id + "."
    if not any(key.startswith(prefix) for key in adopted):
        return True
    return any(_needs_session(half, adopted) for half in split_portion(portion))


def _admit_merge_headers(
    merges: Sequence[tuple[tuple[str, ...], ModelGroup | None]],
    forbidden_secret: Callable[[str], bool],
) -> None:
    """Refuse a run secret in a merged header's decoded text (PDR-0044).

    ``forbidden_secret`` is the mode-bound ``grouping_run_secret`` check.
    Each field is located as the batch's raw-wire screen locates it
    (``field/merges/<n>/<key>``), so a shadowed hit that screen already
    counted is one field, counted once.
    """

    for position, (_ids, header) in enumerate(merges):
        if header is None:
            continue
        for key in _MERGE_HEADER_KEYS:
            with check_field(f"field/merges/{position}/{key}"):
                if forbidden_secret(getattr(header, key)):
                    raise GroupingExecutionError("secret_detected")


def _batch_repair(
    session: _Session, messages: list[dict[str, str]], answer: str, follow_up: str
) -> tuple[list[dict[str, str]], Literal["conversation", "fresh"]] | None:
    """`/15`'s bounded repair, measured as every `/16` request is (Builder decision 3)."""

    limit = session.section.max_input_chars
    conversation = [
        *messages,
        {"role": "assistant", "content": answer},
        {"role": "user", "content": follow_up},
    ]
    if chat_request_size(conversation, []) <= limit:
        return conversation, "conversation"
    fresh = [
        messages[0],
        {
            "role": "user",
            "content": messages[1]["content"] + "\n\n" + _FRESH_NOTE.format(follow_up=follow_up),
        },
    ]
    if chat_request_size(fresh, []) <= limit:
        return fresh, "fresh"
    return None


async def run_model_grouping_v2(
    registry: GroupingRegistry,
    *,
    section: ModelGroupingSection,
    limits: GroupingV2Limits,
    prompt: str,
    metric_primary: str,
    policy_identity: str,
    credentials: ChatCredentials,
    log: AttemptLog,
    session_seconds: float,
    reader: ReflectionReadSession,
    provenance: Mapping[str, tuple[str, ...] | None] | None,
    checkpoints: CheckpointSink | None,
    adopted: Adopted | None,
    cancelled: Callable[[], bool],
    forbidden_secret: Callable[[str], bool],
    oracle_needle: Callable[[str], bool],
    record_budget: int,
    redact: Callable[[str], str] | None = None,
    normalize: Callable[[Any], Any] | None = None,
    client_factory: Callable[[ModelGroupingSection, ChatCredentials], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    attempts: AttemptRecorder | None = None,
) -> ModelGroupingOutcome:
    """Group ``registry``'s failing observations in bounded portions (§3).

    ``session_seconds`` bounds one map session (on the runner's
    `time.monotonic`), the preflight, and one batch with its repairs (on
    ``clock``). ``provenance`` maps an evidence ID to its stored
    `SampleProvenance.used` union (`None`: none reported); ``redact`` is
    applied to every component ID shown. ``record_budget`` bounds a
    checkpoint's variable part (``MAX_RECORD_BYTES -
    CHECKPOINT_ENVELOPE_BYTES``). ``normalize`` maps a decision's JSON
    form (a portion's group record, a batch's merges) to the form its
    checkpoint persists (PDR-0044); each decision is sized, checkpointed and
    used in that form, so a resume replays what an uninterrupted run used.
    ``checkpoints`` and ``adopted`` may be `None`: nothing is checkpointed,
    nothing adopted. ``attempts`` sees every provider attempt and the
    provider failure that ends the grouping.
    """

    sink: CheckpointSink = checkpoints if checkpoints is not None else _NoCheckpoints()
    persisted: Callable[[Any], Any] = normalize if normalize is not None else (lambda value: value)
    offered = adopted if adopted is not None else Adopted()
    used: Mapping[str, tuple[str, ...] | None] = provenance or {}
    recorder = attempts if attempts is not None else AttemptRecorder()
    counted = _Log(log)
    wire_log = cast("AttemptLog", counted)
    map_system, reduce_system = split_prompt(prompt)
    calls: dict[str, Any] = {"map_requests": 0, "reduce": "none", "attempts": 0, "repairs": 0}
    fallback_features = None if section.fallback is None else section.fallback.features

    def finish(
        groups: Sequence[ModelGroup], fallback: Fallback | None, failing: frozenset[str]
    ) -> ModelGroupingOutcome:
        return admit_model_groups(
            registry,
            groups,
            fallback,
            describe_fields=True,
            family14=True,
            # `model_grouping/2` runs only in family 15 (ADR-0055): whole
            # descriptions and the family-15 bounds (ADR-0057).
            family15=True,
            failing=failing,
            policy_identity=policy_identity,
            fallback_features=fallback_features,
            forbidden_secret=forbidden_secret,
            oracle_needle=oracle_needle,
            log=wire_log,
            calls=calls,
        )

    if cancelled():
        raise GroupingExecutionError("cancelled")
    profiles = build_case_profiles(
        registry,
        metric_primary=metric_primary,
        representatives_per_case=section.representatives_per_case,
    )
    if not profiles:
        return finish((), None, frozenset())

    # --- Code: references, glimpses, headers, sets and the plan (§5) -----------
    by_id = {record.evidence_id: record for record in registry.records}
    table = reference_table(registry)
    glimpses = {
        record.evidence_id: glimpse(record, chars=limits.glimpse_chars)
        for record in registry.records
        if record.available
    }
    headers = {}
    for profile in profiles:
        counts, unknown = provenance_counts(profile, used)
        headers[profile.case_id] = case_header(
            profile, provenance=counts, unknown=unknown, chars=limits.glimpse_chars, redact=redact
        )
    representatives = {
        evidence_id: representative_payload(by_id[evidence_id])
        for profile in profiles
        for evidence_id in profile.representatives
    }
    contrast = {
        profile.case_id: glimpses[profile.contrast]
        for profile in profiles
        if profile.contrast is not None
    }
    reported = {
        evidence_id: frozenset(redact(c) if redact is not None else c for c in components)
        for evidence_id in table.observations
        if (components := used.get(evidence_id)) is not None
    }
    units = build_units(
        registry,
        table,
        glimpses,
        max_set_members=limits.max_set_members,
        representatives=frozenset(representatives),
    )
    table = table.with_sets(units)

    def render(portion: MapPortion) -> str:
        return render_portion(portion, table, headers, glimpses, representatives, contrast)

    capacity = max(1, section.max_output_tokens // limits.group_output_tokens)
    budget_chars, reserve = input_budget(section, limits, map_system)
    failing = frozenset(table.observations)
    if budget_chars < 1:
        counted.record(
            {
                "phase": "input_budget_refused",
                "max_input_chars": section.max_input_chars,
                "read_chars": limits.read_chars,
                "reserve": reserve,
            }
        )
        return finish((), ("all", "input_too_large"), frozenset())
    try:
        plan = plan_portions(
            units,
            capacity=capacity,
            input_budget=budget_chars,
            record_budget=record_budget,
            render=render,
            registry_digest=registry.digest,
            identity_digest=policy_identity,
        )
    except InputTooLarge as exc:
        counted.record({"phase": "portion_unit_too_large", "detail": str(exc)})
        return finish((), ("all", "input_too_large"), frozenset())
    counted.record({"phase": "ref_registry", **table.to_log()})
    counted.record(
        {
            "phase": "portion_plan",
            "digest": plan.digest,
            "portions": len(plan.portions),
            "capacity": capacity,
            "input_budget": budget_chars,
            "record_budget": record_budget,
        }
    )
    case_of = {record.evidence_id: record.case_id for record in registry.records}

    def input_digest(portion: MapPortion) -> str:
        return portion_digest(PORTION_LEAD + render(portion))

    # --- Adoption: the plan digest and the portion's own input digest (§9) ------
    replayed: dict[str, PortionOutcome] = {}
    for portion_id, entry in offered.portions.items():
        named = _portion_named(plan, portion_id)
        if (
            named is not None
            and entry.plan_digest == plan.digest
            and entry.input_digest == input_digest(named)
        ):
            replayed[portion_id] = entry.outcome
    if offered.portions:
        counted.record(
            {
                "phase": "portions_adopted",
                "offered": len(offered.portions),
                "adopted": len(replayed),
            }
        )
    # A batch of another plan is never offered to consolidation, which then
    # replays one only when its input digest matches as well.
    same_plan = {
        key: entry.checkpoint
        for key, entry in offered.batches.items()
        if entry.plan_digest == plan.digest
    }
    if offered.batches:
        counted.record(
            {
                "phase": "batches_offered",
                "offered": len(offered.batches),
                "same_plan": len(same_plan),
            }
        )

    # Read at call time, as `/15` does: the module's client is the test seam.
    factory = client_factory or model_grouping_runtime.new_grouping_client
    try:
        client = factory(section, credentials)
    except OSError as exc:
        # Building the client is part of the role call (family 14, F0 R47).
        raise GrouperRoleFailure("exception", "unknown") from exc

    budget = RequestBudget(limits.max_requests)
    preflight_requests = 0
    batch_requests = 0
    groups: tuple[ModelGroup, ...] = ()
    failed = preflight_failed = False

    async def preflight() -> str:
        """The one dialect negotiation of the grouping (Builder decision 2)."""

        nonlocal preflight_requests
        deadline = time.monotonic() + session_seconds
        turns = 0
        sent: list[str] = []
        screened = cast("AttemptLog", _ScreenedLog(counted, forbidden_secret))

        def next_turn() -> int:
            nonlocal turns
            turns += 1
            return turns - 1

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise EditFailure(
                    "provider_error", "grouping deadline reached", code="deadline_exhausted"
                )
            return left

        def record(
            messages: list[dict[str, Any]], style: str, turn: int, tools: list[dict[str, Any]]
        ) -> None:
            nonlocal sent, preflight_requests
            # The sessions' size guard (§6.2), before anything is counted:
            # the preflight holds no tool result to elide, so a request over
            # the limit, a long rejected call echoed in a repair, is not sent.
            if elide_to_fit(messages, tools, limit=section.max_input_chars) is None:
                counted.record(
                    {
                        "phase": "preflight_context_exhausted",
                        "stage": "preflight",
                        "turn": turn,
                        "max_input_chars": section.max_input_chars,
                        "request_chars": chat_request_size(messages, tools),
                    }
                )
                raise LoopStop("context_exhausted")
            budget.take()
            preflight_requests += 1
            sent = record_request(screened, sent, messages, style, turn, tools)

        try:
            return await negotiate_style(
                client=client,
                section=chat_section(section),
                log=screened,
                tools=grouping_tools(traces=False)[:1],
                next_turn=next_turn,
                remaining=remaining,
                record=record,
                role=_ROLE,
                retry=section.retry,
                deadline=deadline,
                attempts=recorder,
                probe_tool="list_case",
                probe_message=_PREFLIGHT.format(case=table.c_ref(0)),
                family15=True,
            )
        except LoopStop as exc:
            # No request of the negotiation fits: the probe call never came.
            raise _PreflightFailed from exc
        except EditFailure as exc:
            if exc.code in _PROVIDER_CODES or exc.code == "deadline_exhausted":
                raise
            raise _PreflightFailed from exc

    async def ask(batch: tuple[Card, ...], level: int, batch_id: str) -> BatchAnswer:
        """One batch request without tools, with `/15`'s reduce repairs (§7.2)."""

        nonlocal batch_requests
        session = _Session(
            client,
            section,
            wire_log,
            clock() + session_seconds,
            cancelled,
            forbidden_secret,
            clock,
            sleep,
            rng,
            recorder=recorder,
            family15=True,
        )
        requests = 0

        def sent(repair: bool) -> None:
            nonlocal requests, batch_requests
            requests += 1
            batch_requests += 1
            if repair:
                budget.repairs += 1

        async def request(
            messages: list[dict[str, str]], request_id: str, *, repair: bool = False
        ) -> str | None:
            """The answer text, or `None` once the batch's deadline passed.

            A request is counted, and a repair counted as one, only once it
            is sent: neither the deadline nor ``max_requests`` spends a repair.
            """

            if clock() >= session.deadline:
                return None  # nothing is sent, so nothing is counted
            budget.take()
            try:
                text = await session.request(messages, request_id)
            except _DeadlineElapsed:
                budget.requests -= 1  # not sent after all
                return None
            except RoleCallExhausted as exc:
                # Sent, whatever ended it: `map_requests` must not count it.
                sent(repair)
                if exc.reason != "deadline_exhausted":
                    raise
                return None
            sent(repair)
            return text

        def ended(outcome: str) -> BatchAnswer:
            return BatchAnswer((), 0, outcome, requests)

        stem = f"batch-{level}-{batch_id}"
        messages = [
            {"role": "system", "content": reduce_system},
            {"role": "user", "content": batch_input(batch)},
        ]
        text = await request(messages, f"{stem}-0")
        if text is None:
            return ended("session_timeout")
        if session.finish_reason == "length":
            return ended("cut")
        map_ids = [card.local_id for card in batch]
        merges = parse_reduce_response(text, map_ids=map_ids)
        repair = 0
        while merges is None and repair < effective_grouper_repairs(section):
            _, defects = _bounded_defects(["invalid_merge_list"])
            repaired = _batch_repair(session, messages, text, _REDUCE_REPAIR)
            if repaired is None:
                counted.record(
                    {
                        "phase": "repair_skipped",
                        "request_id": f"{stem}-{repair}",
                        "reason": "size",
                        "defects": defects,
                    }
                )
                break
            repair_messages, form = repaired
            repair += 1
            request_id = f"{stem}-{repair}"
            counted.record(
                {"phase": "repair", "request_id": request_id, "form": form, "defects": defects}
            )
            answer = await request(repair_messages, request_id, repair=True)
            if answer is None:
                return ended("session_timeout")
            if session.finish_reason == "length":
                return ended("cut")
            text = answer
            merges = parse_reduce_response(text, map_ids=map_ids)
        if merges is None:
            return ended(INVALID)
        # The answer is checkpointed as it is returned, so each decoded
        # header text is admitted first, as a portion's groups are: the
        # raw-wire screen leaves decoded text to admission, and the
        # persisted form below would hide a run secret from final admission
        # (PDR-0044: modes decide admission, never persistence). Then a
        # header with an oracle needle is scrubbed and the answer takes its
        # persisted form before anything sizes it.
        _admit_merge_headers(merges, forbidden_secret)
        scrubbed = tuple(
            (ids, None if header is None else _scrub(header, oracle_needle))
            for ids, header in merges
        )
        return normalized_answer(BatchAnswer(scrubbed, 0, None, requests), persisted)

    try:
        try:
            outcomes: tuple[PortionOutcome, ...] = ()
            stopped = False
            style = "modern"
            # A grouping whose every portion is adopted opens no session and
            # needs no preflight.
            if any(_needs_session(portion, replayed) for portion in plan.portions):
                try:
                    style = await preflight()
                except MaxRequestsReached:
                    counted.record({"phase": "max_requests_reached", "stage": "preflight"})
                    stopped = True
            if not stopped:
                ctx = PortionContext(
                    client=client,
                    section=section,
                    log=wire_log,
                    recorder=recorder,
                    limits=limits,
                    request_style=style,
                    system=map_system,
                    render=render,
                    reader=reader,
                    table=table,
                    glimpses=glimpses,
                    reported=reported,
                    record_budget=record_budget,
                    session_deadline=lambda: time.monotonic() + session_seconds,
                    forbidden_secret=forbidden_secret,
                    oracle_needle=oracle_needle,
                    cancelled=cancelled,
                    budget=budget,
                    normalize=persisted,
                )

                def on_finished(outcome: PortionOutcome) -> None:
                    named = _portion_named(plan, outcome.portion_id)
                    assert named is not None, outcome.portion_id
                    sink.portion(outcome, plan.digest, input_digest(named))

                outcomes = await map_portions(ctx, plan, adopted=replayed, on_finished=on_finished)
            groups = tuple(
                model_group(record, case_of) for outcome in outcomes for record in outcome.groups
            )
            if stopped:
                calls["reduce"] = "skipped"
            elif len(outcomes) > 1 and len(groups) > 1:
                groups = await _consolidate(
                    groups,
                    ask=ask,
                    profiles=profiles,
                    table=table,
                    glimpses=glimpses,
                    used=used,
                    redact=redact,
                    section=section,
                    limits=limits,
                    capacity=capacity,
                    record_budget=record_budget,
                    reduce_system=reduce_system,
                    adopted=same_plan,
                    plan_digest=plan.digest,
                    sink=sink,
                    log=counted,
                    calls=calls,
                )
                groups = resolve_overlaps(groups)
        except RoleCallExhausted as exc:
            recorder.end(exc)
            raise _provider_failure(exc.reason) from exc
        except EditFailure as exc:
            # The shared runner's provider failures: an exhaustion or rejection
            # (code `provider_exhausted`/`provider_rejected`), a pinned provider
            # answering as another one (`provider_changed`), any other provider
            # error, or the preflight's own deadline.
            if exc.code not in _PROVIDER_CODES and exc.code != "deadline_exhausted":
                raise
            cause = exc.__cause__
            if isinstance(cause, RoleCallExhausted):
                recorder.end(cause)
                raise _provider_failure(cause.reason) from exc
            reason = "deadline_exhausted" if exc.code == "deadline_exhausted" else exc.code
            recorder.fail(reason)
            counted.record({"phase": "provider_failed", "code": reason})
            raise _provider_failure(
                "provider_rejected" if exc.code == "provider_rejected" else reason
            ) from exc
        except _PreflightFailed:
            counted.record({"phase": "preflight_failed"})
            preflight_failed = True
        finally:
            calls.update(
                map_requests=budget.requests - preflight_requests - batch_requests,
                attempts=counted.attempts,
                repairs=budget.repairs,
            )
    except BaseException:
        failed = True
        raise
    finally:
        if failed:
            # An exception already leaving keeps its own route (family 14).
            with suppress(OSError):
                await _close(client)
        else:
            await _close_client(client, family14=True)
    if preflight_failed:
        # The model cannot make the probe call, so no portion could be asked.
        return finish((), ("all", "invalid_output"), failing)
    return finish(groups, None, failing)


async def _consolidate(
    groups: Sequence[ModelGroup],
    *,
    ask: Callable[[tuple[Card, ...], int, str], Awaitable[BatchAnswer]],
    profiles: Sequence[CaseProfile],
    table: RefTable,
    glimpses: Mapping[str, str],
    used: Mapping[str, tuple[str, ...] | None],
    redact: Callable[[str], str] | None,
    section: ModelGroupingSection,
    limits: GroupingV2Limits,
    capacity: int,
    record_budget: int,
    reduce_system: str,
    adopted: Mapping[tuple[int, str], BatchCheckpoint],
    plan_digest: str,
    sink: CheckpointSink,
    log: _Log,
    calls: dict[str, Any],
) -> tuple[ModelGroup, ...]:
    """Consolidation over the map's groups in portion plan order (§7)."""

    opening = [{"role": "system", "content": reduce_system}, {"role": "user", "content": ""}]
    budget = section.max_input_chars - chat_request_size(opening, []) - REPAIR_RESERVE
    if budget < 1:
        log.record({"phase": "reduce_skipped", "reason": "input_budget"})
        calls["reduce"] = "skipped"
        return tuple(groups)
    consolidated = await consolidate(
        groups,
        evidence=card_evidence(profiles, table, glimpses=glimpses, used=used),
        ask=ask,
        limits=ConsolidationLimits(
            capacity=capacity,
            input_budget=budget,
            record_budget=record_budget,
            concurrency=section.concurrency,
            max_levels=limits.max_reduce_levels,
            card_examples=limits.card_examples,
            card_chars=limits.glimpse_chars,
        ),
        adopted=adopted,
        on_batch=lambda done: sink.batch(
            done.level, done.batch_id, done.answer, plan_digest, done.input_digest
        ),
        log=log.record,
        redact=redact,
    )
    calls["reduce"] = consolidated.reduce_state
    log.record(
        {
            "phase": "consolidated",
            "levels": consolidated.levels,
            "groups": len(consolidated.groups),
            "reduce": consolidated.reduce_state,
            "skipped": consolidated.skipped,
        }
    )
    return consolidated.groups
