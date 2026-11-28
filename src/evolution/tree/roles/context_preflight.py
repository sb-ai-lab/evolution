"""Read-only measurement of explicit /16 direct-chat scenarios at the SDK boundary.

A prepared campaign has no episode, accepted diagnosis, or negotiated route yet.
The default briefs and the page below are synthetic inputs. A caller may instead
supply an already role-visible brief and a page from its projection. Neither case
reconstructs a provider's hidden continuations from an operator transcript.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any, Literal

from evolution.prompts import (
    TREE_EDITOR_SYSTEM_V4,
    TREE_GROUPING_MODEL_V3_SYSTEM,
    TREE_REFLECTOR_SYSTEM_V8,
)
from evolution.tree.execution.check_modes import ENFORCE_ALL, CheckModes, repeated_read_check
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
from evolution.tree.roles.chat_history import ToolReads, message_chars
from evolution.tree.roles.config import (
    ChatEditorSection,
    ModelGroupingSection,
    SelectionSection,
    effective_grouping_v2,
    effective_protocol_repairs,
)
from evolution.tree.roles.editor.chat_editor import (
    EVIDENCE_CHUNK_CHARS,
    _compact_messages,
    _protocol_correction,
    _protocol_options,
    _read_arguments,
    chat_request_options,
    chat_tool_results,
    editor_first_messages,
    editor_tools,
    editor_writable,
    offered_editor_tools,
)
from evolution.tree.roles.editor.editor_brief import render_catalog
from evolution.tree.roles.editor_contracts import (
    EMPTY_HISTORY,
    Diagnosis,
    EditBrief,
    EditBriefCase,
    brief_version_for,
)
from evolution.tree.roles.grouping import grouping_portions as portions
from evolution.tree.roles.grouping.grouping_tools import grouping_tools
from evolution.tree.roles.grouping.model_grouping import build_case_profiles
from evolution.tree.roles.grouping.model_grouping_runtime import split_prompt
from evolution.tree.roles.grouping.model_grouping_v2 import input_budget
from evolution.tree.roles.grouping.portion_session import chat_section, portion_messages
from evolution.tree.roles.grouping.reflect_grouping import GroupingRecord, build_registry
from evolution.tree.roles.limits import adaptive_limits
from evolution.tree.roles.read_progress import ReadProgress
from evolution.tree.roles.reflection_reader import CONTENT_CHARS
from evolution.tree.roles.reflector.chat_reflector import (
    chat_first_messages,
    reflect_brief_tools,
    reflect_turn_limit,
)
from evolution.tree.roles.reflector.reflect_aggregate import aggregate_forms
from evolution.tree.roles.reflector.reflect_brief import (
    DIAGNOSTICS_INLINE_CHARS,
    AdaptiveReflectBrief,
    ReflectBrief,
)
from evolution.tree.roles.reflector.reflect_budget import (
    RequestBudget,
    chat_first_request,
)
from evolution.tree.roles.reflector.reflect_history import (
    HistorySource,
    initial_layout,
    render_history,
)
from evolution.tree.roles.reflector.reflection_inputs import _page as evidence_text_page
from evolution.tree.roles.reflector_contracts import (
    HISTORY_MAX_CHARS,
    REFLECT_LIMITS_V1,
    ProbeAccess,
)

Json = dict[str, Any]
TokenCounter = Callable[[Json], int]
# Preflight bounds its own simulated data, independently of the campaign limits.
_MAX_PAGE_CHARS = 1024 * 1024
_MAX_HISTORY_CHARS = 4 * 1024 * 1024
_MAX_SCENARIO_TURNS = 256


def _scenario_reflector(
    writable_paths: Sequence[str],
    pin: EvidencePin,
    group_text_chars: int | None,
    documents: bool,
    operator_context: str | None,
    min_claim_cases: int | None,
    families_expand: bool,
    selection: SelectionSection,
    page_id: str,
) -> AdaptiveReflectBrief:
    history = render_history(
        HistorySource((), predecessor_rounds=0, aggregate=aggregate_forms((), ())),
        initial_layout(0),
    )
    # Scenario values only. The renderer receives no Target cases, oracle or scorer output.
    payload: Json = {
        "version": "reflect_brief/6",
        "champion_digest": "0" * 64,
        "editable_paths": list(writable_paths),
        "operator_context": operator_context,
        **pin.payload(),
        "limits": {
            key: value
            for key, value in {
                **REFLECT_LIMITS_V1,
                **adaptive_limits(group_text_chars),
                "history_chars": HISTORY_MAX_CHARS,
                "diagnostics_inline_chars": DIAGNOSTICS_INLINE_CHARS,
            }.items()
            if value is not None
        },
        "registry_digest": "0" * 64,
        "group_count": 1,
        "observation_count": 1,
        "failing_count": 1,
        "unavailable_count": 0,
        "cycle": 1,
        "cycle_reset": False,
        "focus_cases": selection.focus_cases,
        "context_page_size": selection.context_page_size,
        "groups": [
            {"group_id": "scenario-group", "observations": 1, "case_count": 1, "description": None}
        ],
        "portion": [{"group_id": "scenario-group", "case_id": "scenario", "exemplars": [page_id]}],
        "families": [],
        "history": history,
        "evidence_catalog": {
            "entries": [
                {
                    "case_id": "scenario",
                    "relation": "catalog",
                    "available": 1,
                    "failing": 1,
                    "passed": 0,
                    "unknown": 0,
                }
            ],
            "total": 1,
            "next_offset": None,
        },
    }
    if documents:
        payload["documents"] = {"entries": [], "total": 0, "next_offset": None}
    return AdaptiveReflectBrief(
        "0" * 64,
        operator_context,
        EMPTY_HISTORY,
        tuple(writable_paths),
        {},
        payload,
        {page_id: "scenario"},
        frozenset({page_id}),
        frozenset() if documents else None,
        version="reflect_brief/6",
        min_claim_cases=min_claim_cases,
        families_expand=families_expand,
    )


def _measure(
    section: ChatEditorSection,
    messages: list[Json],
    tools: list[Json],
    style: str,
    *,
    before: int,
    protected: int,
    compacted: bool,
    count_tokens: TokenCounter | None,
) -> Json:
    options = chat_request_options(
        section, list(messages), _protocol_options(section, tools, style)
    )
    # OpenAI-compatible SDKs merge extra_body into the JSON root before HTTP serialization.
    extra = options.pop("extra_body", {})
    body = {**options, **extra}
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return {
        "serialized_chars": len(encoded),
        "serialized_bytes": len(encoded.encode()),
        "message_count": len(messages),
        "tool_count": len(tools),
        "before_compaction_chars": before,
        "after_compaction_chars": message_chars(messages),
        "protected_exchange_chars": protected,
        "compacted": compacted,
        "input_tokens": count_tokens(body) if count_tokens is not None else None,
    }


def measure_chat_scenario(
    section: ChatEditorSection,
    *,
    role: Literal["reflector", "editor"],
    request_style: str,
    writable_paths: Sequence[str],
    page_chars: int | None = None,
    prompt_text: str | None = None,
    visible_files: Sequence[str] = (),
    probe: ProbeAccess | None = None,
    editor_max_changed_files: int | None = None,
    count_tokens: TokenCounter | None = None,
    brief: AdaptiveReflectBrief | ReflectBrief | EditBrief | None = None,
    page: Json | None = None,
    page_id: str | None = None,
    traces: bool = True,
    history_tools: bool = True,
    evidence_pin: EvidencePin | None = None,
    check_modes: CheckModes = ENFORCE_ALL,
    group_text_chars: int | None = 512,
    documents: bool = False,
    operator_context: str | None = None,
    min_claim_cases: int | None = None,
    families_expand: bool = False,
    selection: SelectionSection | None = None,
) -> dict[str, Json]:
    """Measure the first role turn, a one-page turn, and the first reachable compaction.

    The latter repeats explicit quotations of the same page within the declared
    turn and repair limits. Absent keys correspond to unreachable turns. A known
    route can count the assembled body using its own chat serialization.
    """
    if request_style not in {"modern", "modern_single", "modern_auto", "legacy"}:
        raise ValueError("unknown direct chat request style")
    if page_chars is None:
        page_chars = CONTENT_CHARS if role == "reflector" else EVIDENCE_CHUNK_CHARS
    if type(page_chars) is not int or page_chars < 1:
        raise ValueError("page_chars must be positive")
    if prompt_text is None and section.system_prompt is not None:
        raise ValueError("custom role prompt requires the verified prompt text")
    pin = evidence_pin or EvidencePin(DEFAULT_EVIDENCE, ENFORCE_ALL.input_modes())
    if page_id is None:
        page_id = "obs:scenario:1" if role == "reflector" else "case:scenario"
    components = {}
    turn_limit = section.max_turns
    if role == "reflector":
        if brief is None:
            brief = _scenario_reflector(
                writable_paths,
                pin,
                group_text_chars,
                documents,
                operator_context,
                min_claim_cases,
                families_expand,
                selection or SelectionSection(),
                page_id,
            )
        if not isinstance(brief, (AdaptiveReflectBrief, ReflectBrief)):
            raise ValueError("reflector scenario needs a reflector brief")
        prompt = TREE_REFLECTOR_SYSTEM_V8 if prompt_text is None else prompt_text
        turn_limit = reflect_turn_limit(section, probe)
        messages = chat_first_messages(
            prompt,
            brief,
            editable_paths=writable_paths,
            environment_docs=None,
            probe=probe,
            editor_max_changed_files=editor_max_changed_files,
            session_limits=(turn_limit, effective_protocol_repairs(section)),
        )
        tools = reflect_brief_tools(
            brief,
            probe=probe is not None,
            history=history_tools,
            result_repair=True,
            contracts=True,
        )
        if isinstance(brief, AdaptiveReflectBrief):
            components = dict(
                chat_first_request(
                    prompt,
                    messages,
                    tools,
                    brief,
                    probe=probe,
                    budget=RequestBudget(
                        section.context_compact_chars, section.context_message_max_chars
                    ),
                ).components
            )
        finish = "finish_reflection"
    elif role == "editor":
        if brief is None:
            brief = EditBrief(
                "0" * 64,
                operator_context,
                (EditBriefCase("scenario", "scenario", "scenario", (), {}, ()),),
                Diagnosis("absent", None, None, (), None),
                pin=pin,
                version=brief_version_for(pin, feedback=False),
            )
        if not isinstance(brief, EditBrief):
            raise ValueError("editor scenario needs an editor brief")
        messages = editor_first_messages(
            TREE_EDITOR_SYSTEM_V4 if prompt_text is None else prompt_text,
            brief,
            render_catalog(brief),
            writable_paths,
            section,
            family15=True,
        )
        tools = offered_editor_tools(
            editor_tools(traces=traces, family15=True),
            (p for p in visible_files if editor_writable(p, writable_paths)),
        )
        finish = "finish_edit"
    else:
        raise ValueError("unknown direct chat role")
    if turn_limit < 2:
        return {}
    first = _measure(
        section,
        messages,
        tools,
        request_style,
        before=message_chars(messages),
        protected=0,
        compacted=False,
        count_tokens=count_tokens,
    )
    first.update(components=components, requested_page_chars=page_chars, page_chars=0)
    rows = {"first": first}
    if turn_limit < 3:
        first["limitations"] = ["model_turn_limit"]
        return rows
    if page_chars > _MAX_PAGE_CHARS:
        first["limitations"] = ["synthetic_page_size_limit"]
        return rows
    if page is None:
        content = "x" * page_chars
        if isinstance(brief, AdaptiveReflectBrief):
            page = evidence_text_page(content, 0)
        else:
            page = {
                "id": page_id,
                "content": content,
                "next_offset": None,
                "total_chars": page_chars,
                "content_digest": hashlib.sha256(content.encode()).hexdigest(),
            }
    page_text = page["content"]
    if not isinstance(page_text, str):
        raise ValueError("evidence page must have text content")
    reads = ReadProgress(
        finish,
        max_chars=section.context_compact_chars,
        message_chars=section.context_message_max_chars,
    )
    results = ToolReads()
    check = repeated_read_check(check_modes)
    repairs_left = effective_protocol_repairs(section)
    before = first["after_compaction_chars"]
    protected = 0
    for turn in range(2, min(turn_limit, _MAX_SCENARIO_TURNS)):
        arguments: Json = {"evidence_id": page_id}
        if turn > 2:
            arguments.update(offset=0, reread=True)
        reads.begin_turn()
        read_args, reread = _read_arguments(reads, "read_evidence", arguments)
        delivered = reads.deliver("read_evidence", read_args, page, reread=reread)
        repair = reads.repair_message(check)
        encoded = json.dumps(arguments)
        call = {"name": "read_evidence", "arguments": encoded}
        assistant = (
            None
            if request_style == "legacy"
            else {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": call,
                    }
                ],
            }
        )
        correction = None
        if repair is None:
            exchange = chat_tool_results("read_evidence", delivered, assistant)
            if assistant is None:
                results.add(exchange[0], "read_evidence", arguments)
        else:
            if repairs_left == 0:
                first["limitations"] = ["protocol_repair_limit"]
                break
            repairs_left -= 1
            exchange = (
                chat_tool_results("read_evidence", {"error": repair}, assistant)
                if assistant is not None
                else []
            )
            correction = {
                "role": "user",
                "content": _protocol_correction(
                    repair, [SimpleNamespace(**call)], tools, family15=True
                ),
            }
        messages.extend(exchange)
        if exchange:
            protected = message_chars(exchange)
            before += protected
        if correction is not None:
            messages.append(correction)
            before += message_chars([correction])
        compacted = before > section.context_compact_chars
        if compacted:
            _compact_messages(
                messages,
                trigger_chars=section.context_compact_chars,
                message_max_chars=section.context_message_max_chars,
                reads=reads,
                results=results,
            )
        if turn == 2 or compacted:
            row = _measure(
                section,
                messages,
                tools,
                request_style,
                before=before,
                protected=protected,
                compacted=compacted,
                count_tokens=count_tokens,
            )
            row.update(
                scenario_turn=turn,
                page_chars=len(delivered.get("content", "")) if repair is None else 0,
                requested_page_chars=page_chars,
            )
            if len(page_text) < page_chars:
                row["limitations"] = ["reflector_page_limit"]
            if turn == 2:
                rows["after_page"] = row
            if compacted:
                rows["after_compaction"] = row
                break
        if before >= _MAX_HISTORY_CHARS:
            first["limitations"] = ["synthetic_history_size_limit"]
            break
    else:
        first["limitations"] = [
            "model_turn_limit" if turn_limit <= _MAX_SCENARIO_TURNS else "synthetic_turn_limit"
        ]
    return rows


def measure_grouping_scenario(
    section: ModelGroupingSection,
    style: str,
    metric: str,
    prompt: str | None,
    count_tokens: TokenCounter | None,
) -> dict[str, Json]:
    """The first map turn for one synthetic failing observation with no prior groups."""
    record = GroupingRecord(
        "scenario-observation",
        "scenario-case",
        1,
        True,
        True,
        {
            "case": {},
            "public": {},
            "feedback": {},
            "observation": {
                "terminal": "rollout_finished",
                "primary_score": 0.0,
                "critical": False,
                "verdicts": [],
                "failed_criteria": [],
                "answer_text": [],
            },
        },
    )
    registry = build_registry("0" * 64, [record])
    limits = effective_grouping_v2(section)
    (profile,) = build_case_profiles(
        registry, metric_primary=metric, representatives_per_case=section.representatives_per_case
    )
    table = portions.reference_table(registry)
    reps = {record.evidence_id: portions.representative_payload(record)}
    glimpses = {record.evidence_id: portions.glimpse(record, chars=limits.glimpse_chars)}
    units = portions.build_units(
        registry,
        table,
        glimpses,
        max_set_members=limits.max_set_members,
        representatives=frozenset(reps),
    )
    header = portions.case_header(profile, provenance=None, unknown=1, chars=limits.glimpse_chars)
    portion = portions.MapPortion("p1", tuple(units), (record.case_id,))
    rendered = portions.render_portion(
        portion, table.with_sets(units), {record.case_id: header}, glimpses, reps, {}
    )
    system, _ = split_prompt(TREE_GROUPING_MODEL_V3_SYSTEM if prompt is None else prompt)
    budget, _ = input_budget(section, limits, system)
    size = portions.portion_input_size(rendered)
    if size > budget:
        return {}
    messages = portion_messages(system, rendered)
    row = _measure(
        chat_section(section),
        messages,
        grouping_tools(traces=True),
        style,
        before=message_chars(messages),
        protected=0,
        compacted=False,
        count_tokens=count_tokens,
    )
    row.update(
        provenance="synthetic",
        limitations=["synthetic_grouping_observation"],
        scenario_turn=1,
        portion_observations=1,
        portion_input_chars=size,
        portion_budget_chars=budget,
        max_output_tokens=section.max_output_tokens,
        components={},
    )
    return {"first": row}
