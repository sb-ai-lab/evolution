"""Async half of model grouping (PDR-0018, ADR-0022).

Sends the deterministic case bundles of `model_grouping.py` to the dedicated
OpenAI-compatible grouping role: one map request per character-bounded chunk
(at most ``section.concurrency`` at once), an optional reduce over the map
groups' headers, and at most two protocol-repair requests per logical request.
Every provider request goes through the shared `role_call_retry/1` contract
(`role_call.call_with_retry`), which is the sole retry owner: the client is
built with ``max_retries=0``.

Results are collected by chunk index, never in completion order, so the
admitted grouping does not depend on concurrency or provider timing. A run
secret in any model text is an integrity failure
(`GroupingExecutionError("secret_detected")`), cancellation is
``GroupingExecutionError("cancelled")``; every other failure -- provider
exhaustion, the operation deadline, an oversized case bundle, an answer still
invalid after repair -- becomes the declared ``fields`` or singleton fallback of
the declared policy and is reported in `ModelGroupingOutcome.fallback`.

Family 14 (``family14=True``, ADR-0034 and its 2026-10-09 amendment) runs the
same requests, retries, repairs and log as the `chat` Grouper, with two
differences: a provider exhaustion or the operation deadline raises
`GrouperRoleFailure` (class ``provider``) for the executor to journal instead of
falling back, and an answer whose admitted groups cover no failing available
observation falls back with scope ``all`` rather than ``uncovered``
(`admit_model_groups`).

`openai` and `httpx` belong to the ``tree`` extra, so they are imported only
inside `new_grouping_client`; this module loads on a core install.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from evolution.tree.execution.check_modes import check_field, grouping_field_key, scan_grouping_wire
from evolution.tree.roles.config import effective_grouper_repairs, effective_require_parameters
from evolution.tree.roles.grouping.model_grouping import (
    BUNDLE_OVERHEAD,
    CaseBundle,
    FallbackOutcome,
    InputTooLarge,
    ModelGroup,
    apply_merges,
    build_bundle,
    build_case_profiles,
    chunk_bundles,
    covered,
    parse_map_response,
    parse_reduce_response,
    project,
    resolve_overlaps,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    AdmittedGrouping,
    FieldFeature,
    GroupingRegistry,
    _canonical_json,
)
from evolution.tree.roles.role_call import (
    AttemptRecorder,
    AttemptTelemetry,
    RoleCallExhausted,
    RouterRefusal,
    call_with_retry,
    error_body,
    router_refusal,
    status_error_body,
)
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from evolution.tree.roles.config import ModelGroupingSection
    from evolution.tree.roles.editor.chat_editor import ChatCredentials
    from evolution.tree.roles.workspace import AttemptLog

__all__ = [
    "MAP_LEAD",
    "REDUCE_LEAD",
    "ModelGroupingOutcome",
    "admit_model_groups",
    "new_grouping_client",
    "run_model_grouping",
    "split_prompt",
]

#: The user message of a map request is this lead plus the chunk's bundle
#: texts joined by ``"\n"``.
MAP_LEAD: Final = (
    "Failing observations to group (every evidence_id listed under rows with "
    "state failing must appear exactly once):\n"
)
REDUCE_LEAD: Final = "Map groups to consider merging:\n"
_REDUCE_HEADING: Final = "## Reduce"
_ROLE: Final = "grouping"
#: Space retained in each initial request for a fresh repair's note and defects.
REPAIR_RESERVE: Final = 600
#: Maximum joined defect text, including the omitted-code suffix.
DEFECTS_BOUND: Final = 256
#: A fresh repair retains the original request and omits the rejected answer.
_FRESH_NOTE: Final = "An earlier answer to this request was rejected. {follow_up}"

_MAP_REPAIR: Final = (
    "Your answer has these validation defects: {defects}. Every evidence_id listed "
    "under rows with state failing must appear in exactly one group, exactly once. "
    "Return the complete corrected JSON object, not a patch, and nothing before it."
)
#: `parse_reduce_response` reports no defects, so a reduce repair restates the
#: merge rules instead of naming what was wrong.
_REDUCE_REPAIR: Final = (
    "Your answer is not a valid merge list. Answer with exactly one JSON object "
    'with the single key "merges": a list of merges, each naming in "groups" two '
    "or more distinct listed group IDs, with no group in more than one merge, and "
    "either all or none of the header keys title, failure_mode, "
    "expected_behaviour, question and priority, within their bounds. Return the "
    "complete corrected JSON object, not a patch, and nothing before it."
)

ReduceState = Literal["none", "done", "skipped", "invalid"]
Fallback = tuple[Literal["uncovered", "all"], str]


@dataclass(frozen=True)
class ModelGroupingOutcome:
    """The admitted grouping plus the fallback that shaped it, if any.

    ``calls`` is ``{"map_requests", "reduce", "attempts", "repairs"}``:
    initial map requests sent, the reduce state (``none``, ``done``,
    ``skipped`` or ``invalid``), provider attempts across every logical
    request, and protocol-repair requests.

    ``full_descriptions`` (family 15 only, ADR-0057): the whole label and
    description of every model group whose descriptions-map entry the fit
    trimmed or dropped, keyed by the group number the admitted result uses.
    The campaign journals each as a full-description record before
    `grouping_finished`; an empty mapping journals none.
    """

    grouped: AdmittedGrouping
    fallback: FallbackOutcome | None
    calls: dict[str, Any]
    full_descriptions: Mapping[int, Mapping[str, str]] = dataclasses.field(default_factory=dict)


def split_prompt(prompt: str) -> tuple[str, str]:
    """The map and reduce system prompts, split on the ``## Reduce`` heading.

    A declared prompt without that heading serves as both; a reduce answer it
    does not describe only makes the reduce ``invalid``, keeping the map groups.
    """

    lines = prompt.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == _REDUCE_HEADING:
            return "\n".join(lines[:index]).strip(), "\n".join(lines[index + 1 :]).strip()
    return prompt.strip(), prompt.strip()


def new_grouping_client(section: ModelGroupingSection, credentials: ChatCredentials) -> AsyncOpenAI:
    """The default client: one network attempt per call over an env-blind `httpx`.

    ``max_retries=0`` leaves retries to `call_with_retry`; ``timeout=None``
    because each attempt's timeout is applied by `asyncio.timeout`.
    """

    try:
        import httpx  # noqa: F401 - the packaging guard covers `chat_http_client`'s import
        from openai import AsyncOpenAI
    except ImportError as exc:
        # `evolve` refuses this at bind (`bindings.grouping_model_credentials`);
        # a library caller that skipped it gets a controlled failure.
        raise GroupingExecutionError("provider_sdk_missing") from exc
    # The chat roles' own credential handling: a declared key exactly as
    # before, none at all through the transport that strips credential headers.
    from evolution.tree.roles.editor.chat_editor import chat_http_client, sdk_credentials

    return AsyncOpenAI(
        **sdk_credentials(credentials, AsyncOpenAI),
        base_url=section.base_url,
        max_retries=0,
        timeout=None,
        http_client=cast(Any, chat_http_client(section.verify_ssl_certs, credentials)),
    )


def _request_options(section: ModelGroupingSection) -> dict[str, Any]:
    """The provider pin and reasoning effort, exactly as `chat_editor.request_options`
    passes them (the grouping section has no quantizations to add)."""

    extra_body: dict[str, Any] = {}
    if section.provider_only or section.provider_order:
        extra_body["provider"] = {
            **(
                {"only": list(section.provider_only)}
                if section.provider_only
                else {"order": list(section.provider_order or ())}
            ),
            "allow_fallbacks": bool(section.allow_fallbacks),
            "require_parameters": effective_require_parameters(section),
        }
    if section.reasoning_effort:
        extra_body["reasoning"] = {"effort": section.reasoning_effort}
    return {"extra_body": extra_body} if extra_body else {}


class _DeadlineElapsed(Exception):
    """The operation deadline passed outside a provider call."""


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


@dataclass
class _Session:
    client: Any
    section: ModelGroupingSection
    log: AttemptLog
    deadline: float
    cancelled: Callable[[], bool]
    forbidden_secret: Callable[[str], bool]
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    rng: Callable[[], float]
    attempts: int = 0
    repairs: int = 0
    map_requests: int = 0
    recorder: AttemptRecorder = field(default_factory=AttemptRecorder)
    #: The last answer's `finish_reason`; `model_grouping/2` reads it to tell a
    #: batch answer cut at the output limit (one session per batch).
    finish_reason: str | None = None
    family15: bool = False

    async def request(
        self, messages: list[dict[str, str]], request_id: str, *, initial_map: bool = False
    ) -> str:
        """One logical request under `role_call_retry/1`; the answer text."""

        if self.cancelled():
            raise GroupingExecutionError("cancelled")
        if self.clock() >= self.deadline:
            raise _DeadlineElapsed
        if initial_map:
            self.map_requests += 1  # counted only once the request is really sent
        self.log.record({"phase": "request", "request_id": request_id, "messages": messages})
        options = _request_options(self.section)
        sent = 0

        async def send(timeout: float) -> Any:
            nonlocal sent
            sent += 1
            try:
                async with asyncio.timeout(timeout):
                    response = await self.client.chat.completions.create(
                        model=self.section.model,
                        messages=messages,
                        max_tokens=self.section.max_output_tokens,
                        **options,
                    )
                if self.family15 and (body := error_body(response)) is not None:
                    raise router_refusal(body, http_status=200)
                return response
            except Exception as exc:
                if self.family15:
                    import openai

                    refused = exc if isinstance(exc, RouterRefusal) else None
                    if isinstance(exc, openai.APIStatusError):
                        body = status_error_body(exc.body)
                        if body is not None:
                            refused = router_refusal(body, http_status=exc.status_code)
                    if refused is not None:
                        self.log.record(
                            {
                                "phase": "provider_refused",
                                "request_id": request_id,
                                "request_style": None,
                                "turn": None,
                                "attempt": sent,
                                "http_status": refused.http_status,
                                "error_code": refused.inner_code,
                                "provider_name": refused.provider_name,
                                "message": refused.message,
                            }
                        )
                raise

        def on_attempt(telemetry: AttemptTelemetry) -> None:
            self.attempts += 1
            self.log.record({"phase": "provider_attempt", **asdict(telemetry)})
            self.recorder.observe(telemetry)

        response = await call_with_retry(
            send,
            policy=self.section.retry,
            role=_ROLE,
            request_id=request_id,
            deadline=self.deadline,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
            on_attempt=on_attempt,
        )
        from evolution.tree.roles.editor.chat_editor import response_telemetry

        text = _response_text(response)
        try:
            finish_reason, usage = response_telemetry(response)
        except (AttributeError, IndexError, KeyError, TypeError):
            finish_reason, usage = None, None
        telemetry = {"finish_reason": finish_reason, "usage": usage}
        self.finish_reason = finish_reason if isinstance(finish_reason, str) else None
        # Checked before logging: a secret-bearing answer is never written,
        # even to the redacting private log.
        if scan_grouping_wire(self.forbidden_secret, text, decoded_enforcement=False):
            self.log.record(
                {"phase": "response", "request_id": request_id, "text": "[redacted]", **telemetry}
            )
            raise GroupingExecutionError("secret_detected")
        self.log.record({"phase": "response", "request_id": request_id, "text": text, **telemetry})
        return text


def _repair_messages(
    messages: Sequence[dict[str, str]], answer: str, follow_up: str
) -> list[dict[str, str]]:
    return [
        *messages,
        {"role": "assistant", "content": answer},
        {"role": "user", "content": follow_up},
    ]


def _fits(session: _Session, messages: Sequence[dict[str, str]]) -> bool:
    """Whether a request stays within ``max_input_chars``.

    The conversation form is tried first; a repair that does not fit in either
    form is skipped and counts as repair exhausted.
    """

    return sum(len(message["content"]) for message in messages) <= session.section.max_input_chars


def _bounded_defects(defects: Sequence[str]) -> tuple[str, list[str]]:
    """Retain whole defect codes within the bound, including an omission count."""

    codes = list(defects)
    joined = ", ".join(codes)
    if len(joined) <= DEFECTS_BOUND:
        return joined, codes
    retained: list[str] = []
    for code in codes:
        candidate = [*retained, code]
        remaining = len(codes) - len(candidate)
        records = [*candidate, f"… and {remaining} more"]
        if len(", ".join(records)) > DEFECTS_BOUND:
            break
        retained = candidate
    records = [*retained, f"… and {len(codes) - len(retained)} more"]
    return ", ".join(records), records


def _repair_request(
    session: _Session, messages: list[dict[str, str]], answer: str, follow_up: str
) -> tuple[list[dict[str, str]], Literal["conversation", "fresh"]] | None:
    """Choose a bounded repair, preserving the original input in either form."""

    conversation = _repair_messages(messages, answer, follow_up)
    if _fits(session, conversation):
        return conversation, "conversation"
    fresh = [
        messages[0],
        {
            "role": "user",
            "content": messages[1]["content"] + "\n\n" + _FRESH_NOTE.format(follow_up=follow_up),
        },
    ]
    if _fits(session, fresh):
        return fresh, "fresh"
    return None


async def _map_chunk(
    session: _Session,
    index: int,
    chunk: Sequence[CaseBundle],
    *,
    system: str,
    case_of: Mapping[str, str],
) -> tuple[ModelGroup, ...]:
    """One chunk's map groups, renamed ``m{index}-{local}``, after repair."""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": MAP_LEAD + "\n".join(bundle.text for bundle in chunk)},
    ]
    # `build_bundle` lists only failing *available* rows as failing.
    failing = [evidence_id for bundle in chunk for evidence_id in bundle.failing_ids]
    text = await session.request(messages, f"map-{index}-0", initial_map=True)
    parsed = parse_map_response(
        text, chunk_failing=failing, case_of=case_of, provenance_available=False
    )
    repair = 0
    while parsed.defects and repair < effective_grouper_repairs(session.section):
        defect_text, defects = _bounded_defects(parsed.defects)
        follow_up = _MAP_REPAIR.format(defects=defect_text)
        request = _repair_request(session, messages, text, follow_up)
        if request is None:
            session.log.record(
                {
                    "phase": "repair_skipped",
                    "request_id": f"map-{index}-{repair}",
                    "reason": "size",
                    "defects": defects,
                }
            )
            break  # repair exhausted: the uncovered path applies
        repair_messages, form = request
        repair += 1
        session.repairs += 1
        request_id = f"map-{index}-{repair}"
        session.log.record(
            {"phase": "repair", "request_id": request_id, "form": form, "defects": defects}
        )
        text = await session.request(repair_messages, request_id)
        parsed = parse_map_response(
            text, chunk_failing=failing, case_of=case_of, provenance_available=False
        )
    return tuple(
        dataclasses.replace(group, local_id=f"m{index}-{group.local_id}") for group in parsed.groups
    )


async def _map_all(
    session: _Session,
    chunks: Sequence[Sequence[CaseBundle]],
    *,
    system: str,
    case_of: Mapping[str, str],
) -> list[ModelGroup]:
    """Every chunk's groups in chunk order; the first failure cancels the rest."""

    semaphore = asyncio.Semaphore(session.section.concurrency)
    failed = False

    async def guarded(index: int, chunk: Sequence[CaseBundle]) -> tuple[ModelGroup, ...]:
        nonlocal failed
        async with semaphore:
            # A released slot must not start a chunk after another one failed:
            # the wait below returns only after this task has run.
            if failed:
                return ()
            try:
                return await _map_chunk(session, index, chunk, system=system, case_of=case_of)
            except BaseException:
                failed = True
                raise

    tasks = [asyncio.create_task(guarded(i, chunk)) for i, chunk in enumerate(chunks)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    failures: list[BaseException] = []
    for task in tasks:
        failure = None if task.cancelled() else task.exception()
        if failure is not None:
            failures.append(failure)
    # Integrity outranks everything, then an unexpected defect; only then a
    # provider or deadline exhaustion, which the caller turns into a fallback.
    for failure in failures:
        if isinstance(failure, GroupingExecutionError) and failure.code == "secret_detected":
            raise failure
    for failure in failures:
        if isinstance(failure, GroupingExecutionError):
            raise failure
    for failure in failures:
        if not isinstance(failure, RoleCallExhausted | _DeadlineElapsed):
            raise failure
    if failures:
        raise failures[0]
    return [group for task in tasks for group in task.result()]


def _reduce_input(groups: Sequence[ModelGroup]) -> str:
    return REDUCE_LEAD + _canonical_json(
        {
            "groups": [
                {
                    "id": group.local_id,
                    "title": group.title,
                    "failure_mode": group.failure_mode,
                    "expected_behaviour": group.expected_behaviour,
                    "question": group.question,
                    "priority": group.priority,
                    "member_count": len(group.members),
                    "affected_cases": list(group.affected_cases),
                }
                for group in groups
            ]
        }
    ).decode("utf-8")


async def _reduce(
    session: _Session, groups: Sequence[ModelGroup], *, system: str
) -> tuple[tuple[ModelGroup, ...], ReduceState]:
    user = _reduce_input(groups)
    if len(system) + len(user) + REPAIR_RESERVE > session.section.max_input_chars:
        return tuple(groups), "skipped"
    map_ids = [group.local_id for group in groups]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    text = await session.request(messages, "reduce-0-0")
    merges = parse_reduce_response(text, map_ids=map_ids)
    repair = 0
    while merges is None and repair < effective_grouper_repairs(session.section):
        _, defects = _bounded_defects(["invalid_merge_list"])
        request = _repair_request(session, messages, text, _REDUCE_REPAIR)
        if request is None:
            session.log.record(
                {
                    "phase": "repair_skipped",
                    "request_id": f"reduce-0-{repair}",
                    "reason": "size",
                    "defects": defects,
                }
            )
            break  # repair exhausted: the reduce is `invalid`, map groups stay
        repair_messages, form = request
        repair += 1
        session.repairs += 1
        request_id = f"reduce-0-{repair}"
        session.log.record(
            {"phase": "repair", "request_id": request_id, "form": form, "defects": defects}
        )
        text = await session.request(repair_messages, request_id)
        merges = parse_reduce_response(text, map_ids=map_ids)
    if merges is None:
        return tuple(groups), "invalid"
    applied = apply_merges(groups, merges)
    if applied.skipped:
        # A merge over the variant cap keeps its map groups separate.
        session.log.record({"phase": "reduce_merges_skipped", "count": applied.skipped})
    return applied.groups, "done"


_HEADER_FIELDS: Final = ("title", "failure_mode", "expected_behaviour", "question")


def _scrub(group: ModelGroup, oracle_needle: Callable[[str], bool]) -> ModelGroup:
    """Drop a description or variant label that carries an oracle needle.

    Blank header texts make `project` emit no description. A needled variant
    label is replaced by a neutral one, numbered by sorted label so it stays
    deterministic and keeps distinct variants distinct.
    """

    changes: dict[str, Any] = {}
    key = grouping_field_key([evidence_id for evidence_id, _ in group.members])
    bad_header = False
    for name in _HEADER_FIELDS:
        with check_field(f"{'label' if name == 'title' else 'description'}/{key}"):
            if oracle_needle(getattr(group, name)):
                bad_header = True
                break
    if bad_header:
        changes.update({name: "" for name in _HEADER_FIELDS})
    needled_values = set()
    for evidence_id, variant in group.members:
        with check_field(f"variant/{evidence_id}"):
            if oracle_needle(variant):
                needled_values.add(variant)
    needled = sorted(needled_values)
    if needled:
        renamed = {label: f"redacted-{n}" for n, label in enumerate(needled, start=1)}
        changes["members"] = tuple(
            (evidence_id, renamed.get(variant, variant)) for evidence_id, variant in group.members
        )
    return dataclasses.replace(group, **changes) if changes else group


def _model_strings(group: ModelGroup) -> list[tuple[str, str]]:
    key = grouping_field_key([evidence_id for evidence_id, _ in group.members])
    strings = [
        (f"{'label' if name == 'title' else 'description'}/{key}", getattr(group, name))
        for name in _HEADER_FIELDS
    ]
    strings += [(f"priority/{key}", group.priority)]
    strings += [(f"citation/{key}/{i}", text) for i, text in enumerate(group.citations)]
    strings += [(f"metric/{key}/{i}", name) for i, (name, _) in enumerate(group.metrics)]
    strings += [(f"variant/{evidence_id}", variant) for evidence_id, variant in group.members]
    return strings


async def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        result = close()
        if asyncio.iscoroutine(result):
            await result


async def _close_client(client: Any, *, family14: bool) -> None:
    """`_close`, with family 14's `exception` for an `OSError` of the close.

    Closing the provider client is part of the `chat` role call but outside
    the retry layer, so in family 14 its `OSError` is the Grouper's
    `exception / unknown` (ADR-0034, F0 R47); families 9-13 propagate it.
    In family 14 it is called only after the role call finished normally: an
    exception already leaving `run_model_grouping` suppresses the close's
    `OSError` instead, so it keeps its own route. Families 9-13 call it on
    every exit, a failing one included, where the close's `OSError` still
    replaces the exception leaving, as before.
    """

    try:
        await _close(client)
    except OSError as exc:
        if not family14:
            raise
        raise GrouperRoleFailure("exception", "unknown") from exc


def _provider_failure(reason: str) -> GrouperRoleFailure:
    """The family-14 Grouper failure of a provider exhaustion or the deadline.

    ``provider_rejected`` stays itself; every other exhaustion
    (``provider_unavailable``, ``deadline_exhausted``) and an operation
    deadline that passes before a request are ``provider_exhausted``, as
    `chat_editor` maps them for the Editor.
    """

    if reason == "provider_rejected":
        return GrouperRoleFailure("provider_rejected", "provider")
    return GrouperRoleFailure("provider_exhausted", "provider")


def admit_model_groups(
    registry: GroupingRegistry,
    groups: Sequence[ModelGroup],
    fallback: Fallback | None,
    *,
    family14: bool,
    failing: frozenset[str],
    policy_identity: str,
    fallback_features: Sequence[FieldFeature] | None,
    forbidden_secret: Callable[[str], bool],
    oracle_needle: Callable[[str], bool],
    log: AttemptLog,
    calls: dict[str, Any],
    describe_fields: bool = False,
    family15: bool = False,
) -> ModelGroupingOutcome:
    """Admit the model's groups, or the fallback that replaces them.

    ``failing`` is the failing available observations that were sent. With
    ``fallback=None`` the fallback is derived from coverage: incomplete
    coverage of ``failing`` (including an empty answer) is
    ``("uncovered", "invalid_output")``. A run secret in any model text is an
    integrity failure; a description or variant label with an oracle needle
    is dropped first (`_scrub`). Groups admission refuses are not trusted and
    become ``("all", "invalid_output")``.

    ``family14`` (ADR-0034 amendment): an ``uncovered`` fallback whose
    groups cover none of ``failing`` is recorded as scope ``all``, since no
    admitted model group covers a failing observation. Families 9-13 pass
    ``False`` and keep their outcome.

    ``family15`` (a `/16` run, ADR-0057) projects each model group's
    description whole, trims with the family-15 trimmed form and admits
    descriptions up to `FAMILY15_DESCRIPTION_CHARS`.
    """

    for group in groups:
        for location, text in _model_strings(group):
            with check_field(location):
                if forbidden_secret(text):
                    raise GroupingExecutionError("secret_detected")
    groups = tuple(_scrub(group, oracle_needle) for group in groups)
    if fallback is None and covered(groups) != failing:
        fallback = ("uncovered", "invalid_output")
    if (
        family14
        and fallback is not None
        and fallback[0] == "uncovered"
        and not covered(groups) & failing
    ):
        fallback = ("all", fallback[1])
    predicates: tuple[tuple[str, Callable[[str], bool]], ...] = (("oracle_leak", oracle_needle),)
    if describe_fields:
        predicates += (("secret_detected", forbidden_secret),)
    full: dict[int, Mapping[str, str]] = {}

    def collect(mapping: Mapping[int, Mapping[str, str]]) -> None:
        full.update(mapping)

    try:
        grouped, outcome = project(
            registry,
            groups,
            policy_identity=policy_identity,
            fallback_features=fallback_features,
            fallback=fallback,
            forbidden_string_predicates=predicates,
            on_trim=lambda counts: log.record({"phase": "grouping_trimmed", **counts}),
            describe_fields=describe_fields,
            fields_screens=(oracle_needle, forbidden_secret),
            family15=family15,
            on_full_descriptions=collect if family15 else None,
        )
    except ValueError:
        if fallback is not None and fallback[0] == "all":
            raise
        # Groups admission would refuse are not trusted.
        grouped, outcome = project(
            registry,
            (),
            policy_identity=policy_identity,
            fallback_features=fallback_features,
            fallback=("all", "invalid_output"),
            forbidden_string_predicates=predicates,
            describe_fields=describe_fields,
            fields_screens=(oracle_needle, forbidden_secret),
            family15=family15,
            on_full_descriptions=collect if family15 else None,
        )
    return ModelGroupingOutcome(grouped, outcome, calls, full)


async def run_model_grouping(
    registry: GroupingRegistry,
    *,
    section: ModelGroupingSection,
    prompt: str,
    metric_primary: str,
    policy_identity: str,
    credentials: ChatCredentials,
    log: AttemptLog,
    deadline: float,
    cancelled: Callable[[], bool],
    forbidden_secret: Callable[[str], bool],
    oracle_needle: Callable[[str], bool],
    client_factory: Callable[[ModelGroupingSection, ChatCredentials], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    family14: bool = False,
    attempts: AttemptRecorder | None = None,
) -> ModelGroupingOutcome:
    """Group ``registry``'s failing observations with the grouping model.

    ``deadline`` is an absolute ``clock()`` value bounding the whole operation.
    ``family14`` runs the family-14 `chat` Grouper: a provider exhaustion or
    the deadline raises `GrouperRoleFailure` instead of falling back, and
    `admit_model_groups` applies the scope-``all`` rule. ``attempts``, the
    caller's, sees every provider attempt and the provider failure that ends
    the operation, so the operator's line survives any ending.
    """

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
            family14=family14,
            failing=failing,
            policy_identity=policy_identity,
            fallback_features=fallback_features,
            forbidden_secret=forbidden_secret,
            oracle_needle=oracle_needle,
            log=log,
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
        # No failing available observation: nothing to send.
        return finish((), None, frozenset())
    budget = section.max_input_chars - len(map_system) - len(MAP_LEAD) - REPAIR_RESERVE
    try:
        if budget <= BUNDLE_OVERHEAD:
            raise InputTooLarge("input_budget")
        # `build_bundle` rescans the registry for its case's records; the
        # cost is one pass per failing case and needs no API change here.
        bundles = [
            build_bundle(profile, registry, max_chars=budget - BUNDLE_OVERHEAD)
            for profile in profiles
        ]
    except InputTooLarge:
        return finish((), ("all", "input_too_large"), frozenset())
    chunks = chunk_bundles(bundles, max_chars=budget)
    failing = frozenset(evidence_id for bundle in bundles for evidence_id in bundle.failing_ids)
    case_of = {record.evidence_id: record.case_id for record in registry.records}

    factory = client_factory or new_grouping_client
    try:
        client = factory(section, credentials)
    except OSError as exc:
        # Building the client (an SDK loading a CA bundle, say) is part of the
        # role call but outside the retry layer: in family 14 its `OSError` is
        # the Grouper's `exception / unknown` (F0 R47), never the attempt
        # log's run-directory fault the executor lets through.
        if not family14:
            raise
        raise GrouperRoleFailure("exception", "unknown") from exc
    session = _Session(
        client,
        section,
        log,
        deadline,
        cancelled,
        forbidden_secret,
        clock,
        sleep,
        rng,
        recorder=attempts if attempts is not None else AttemptRecorder(),
    )
    failed = False
    try:
        try:
            groups: tuple[ModelGroup, ...] = resolve_overlaps(
                await _map_all(session, chunks, system=map_system, case_of=case_of)
            )
            if len(chunks) > 1 and len(groups) > 1:
                merged, calls["reduce"] = await _reduce(session, groups, system=reduce_system)
                groups = resolve_overlaps(merged)
        except RoleCallExhausted as exc:
            if family14:
                session.recorder.end(exc)
                raise _provider_failure(exc.reason) from exc
            return finish((), ("all", exc.reason), failing)
        except _DeadlineElapsed as exc:
            if family14:
                session.recorder.fail("deadline_exhausted")
                raise _provider_failure("deadline_exhausted") from exc
            return finish((), ("all", "deadline_exhausted"), failing)
        finally:
            calls.update(
                map_requests=session.map_requests,
                attempts=session.attempts,
                repairs=session.repairs,
            )
    except BaseException:
        failed = True
        raise
    finally:
        if failed and family14:
            # Family 14: an exception (or cancellation) already leaving keeps
            # its own route; a close `OSError` must not replace it.
            with suppress(OSError):
                await _close(client)
        else:
            await _close_client(client, family14=family14)
    return finish(groups, None, failing)
