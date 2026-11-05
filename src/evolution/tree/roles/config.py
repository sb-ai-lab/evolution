"""Declared role values consumed by role bindings and implementations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from evolution.tree.execution.config import CommandSection
from evolution.tree.roles.grouping.reflect_grouping import FieldFeature
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.workspace import Budgets
from evolution.tree.target.config import ProbeSection

DEFAULT_CONTEXT_COMPACT_CHARS = 128_000


DEFAULT_CONTEXT_MESSAGE_MAX_CHARS = 12_000


_DEFAULT_BUDGETS = Budgets(
    max_changed_files=None, max_written_bytes=16 * 1024 * 1024, max_payload_bytes=1024 * 1024
)


@dataclass(frozen=True)
class ChatEditorSection:
    """`editor.kind: chat` -- an OpenAI-compatible tool-loop editor."""

    model: str
    base_url: str
    api_key_env: str | None
    system_prompt: Path | None = None
    proxy_env: str | None = None
    reasoning_effort: str | None = None
    provider_only: tuple[str, ...] | None = None
    quantizations: tuple[str, ...] | None = None
    max_output_tokens: int = 8192
    verify_ssl_certs: bool = True
    max_turns: int = 64
    context_compact_chars: int = DEFAULT_CONTEXT_COMPACT_CHARS
    context_message_max_chars: int = DEFAULT_CONTEXT_MESSAGE_MAX_CHARS
    budgets: Budgets = _DEFAULT_BUDGETS
    #: `/11` (PDR-0019, ADR-0023): required on a `/11` chat Editor/Reflector,
    #: the shared `role_call_retry/1` provider-call retry policy; `None` for
    #: every earlier schema, which never admits the key at all.
    retry: RoleCallRetry | None = None
    max_protocol_repairs: int | None = None
    max_consecutive_tool_failures: int | None = None
    provider_order: tuple[str, ...] | None = None
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    nearest_paths: int | None = None


@dataclass(frozen=True)
class AgentCliEditorSection:
    """`editor.kind: agent_cli` -- a coding-agent CLI editor."""

    model: str
    permission: Literal["safe", "unsafe"] = "safe"
    agent_bin: Path | None = None
    allowed_env: frozenset[str] = frozenset()
    system_prompt: Path | None = None
    reasoning_effort: str | None = None
    max_turns: int | None = None
    budgets: Budgets = _DEFAULT_BUDGETS


EditorSection = CommandSection | ChatEditorSection | AgentCliEditorSection


@dataclass(frozen=True)
class FieldsGroupingSection:
    features: tuple[FieldFeature, ...]
    max_consecutive_full_fallbacks: int | None = None


@dataclass(frozen=True)
class CommandGroupingSection:
    command: CommandSection
    config: Mapping[str, Any]
    max_consecutive_full_fallbacks: int | None = None


@dataclass(frozen=True)
class ModelGroupingSection:
    """Configuration for a grouping-model role independent of Reflector and Editor.

    The role shares provider retries through `role_call_retry/1`.
    `fallback` stores the optional closed fields-grouping policy."""

    backend: Literal["openai_chat"]
    model: str
    base_url: str
    api_key_env: str | None
    timeout_seconds: int
    concurrency: int
    max_input_chars: int
    max_output_tokens: int
    retry: RoleCallRetry
    proxy_env: str | None = None
    verify_ssl_certs: bool = True
    reasoning_effort: str | None = None
    provider_only: tuple[str, ...] | None = None
    system_prompt: Path | None = None
    representatives_per_case: int = 3
    max_repairs: int | None = None
    fallback: FieldsGroupingSection | None = None
    max_consecutive_full_fallbacks: int | None = None
    provider_order: tuple[str, ...] | None = None
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    #: `/16` only (b16 spec §10, ADR-0055): the `model_grouping/2` portion
    #: limits. `None` is an undeclared key; `effective_grouping_v2` resolves it.
    group_output_tokens: int | None = None
    glimpse_chars: int | None = None
    max_set_members: int | None = None
    read_chars: int | None = None
    max_turns: int | None = None
    card_examples: int | None = None
    max_reduce_levels: int | None = None
    max_requests: int | None = None


GroupingSection = FieldsGroupingSection | CommandGroupingSection | ModelGroupingSection


@dataclass(frozen=True)
class AgentCliGrouperSection:
    """`grouper.kind: agent_cli`: an agent CLI over a read-only
    projection of grouping inputs. `role` holds the agent keys, parsed as the
    agent Reflector's are; `timeout_seconds` and `fallback` are the Grouper's
    own, split off first as `_parse_reflector` splits its common keys."""

    role: AgentCliEditorSection
    timeout_seconds: int
    fallback: FieldsGroupingSection | None = None
    max_consecutive_full_fallbacks: int | None = None


GrouperSection = (
    FieldsGroupingSection | CommandGroupingSection | ModelGroupingSection | AgentCliGrouperSection
)


@dataclass(frozen=True)
class SelectionSection:
    focus_cases: int = 16
    representatives_per_case: int = 2
    context_page_size: int = 32


@dataclass(frozen=True)
class FamilySection:
    kind: Literal["none", "metadata"]
    path: str | None = None


@dataclass(frozen=True)
class ReflectorSection:
    """`reflector`: `role` is whichever of the
    three `reflector.kind` shapes the document selects (mirroring
    `WorkflowSections.editor`'s union); `environment_docs` and `probe` are the
    two keys every kind shares."""

    role: CommandSection | ChatEditorSection | AgentCliEditorSection
    environment_docs: Path | None
    probe: ProbeSection | None
    grouping: GroupingSection | None = None
    selection: SelectionSection | None = None
    families: FamilySection | None = None
    #: `/11` (PDR-0019, ADR-0023): required for every `reflector.kind`, an
    #: integer `1..20`; `None` for every earlier schema, which never admits
    #: the key at all.
    max_consecutive_failures: int | None = None
    #: `/15` and `/16`: the optional per-field limit of the five
    #: adaptive group text fields, an integer `>= 1` or `None` for no limit.
    #: `None` is also the value of an absent key, so `group_text_chars_declared`
    #: tells the two apart; `effective_group_text_chars` resolves the absent one.
    group_text_chars: int | None = None
    group_text_chars_declared: bool = False
    max_consecutive_skips: int | None = None


def effective_protocol_repairs(section: ChatEditorSection) -> int:
    return 2 if section.max_protocol_repairs is None else section.max_protocol_repairs


def effective_require_parameters(section: ChatEditorSection | ModelGroupingSection) -> bool:
    return True if section.require_parameters is None else section.require_parameters


def effective_nearest_paths(section: ChatEditorSection) -> int:
    return 5 if section.nearest_paths is None else section.nearest_paths


def effective_tool_failures(section: ChatEditorSection) -> int:
    return (
        5
        if section.max_consecutive_tool_failures is None
        else section.max_consecutive_tool_failures
    )


def effective_grouper_repairs(section: ModelGroupingSection) -> int:
    return 2 if section.max_repairs is None else section.max_repairs


#: The `/16` chat Grouper's portion keys with their minimums (b16 spec §10).
#: None has an upper bound (PDR-0044); `max_requests` has no default.
GROUPING_V2_MINIMUMS: Mapping[str, int] = MappingProxyType(
    {
        "group_output_tokens": 1,
        "glimpse_chars": 1,
        "max_set_members": 2,
        "read_chars": 1,
        "max_turns": 1,
        "card_examples": 0,
        "max_reduce_levels": 1,
        "max_requests": 1,
    }
)


@dataclass(frozen=True)
class GroupingV2Limits:
    """The effective `model_grouping/2` limits of one chat Grouper."""

    group_output_tokens: int = 256
    glimpse_chars: int = 800
    max_set_members: int = 64
    read_chars: int = 16_000
    max_turns: int = 8
    card_examples: int = 2
    max_reduce_levels: int = 4
    max_requests: int | None = None


def effective_grouping_v2(section: ModelGroupingSection) -> GroupingV2Limits:
    """`section`'s declared portion keys over `GroupingV2Limits`' defaults."""

    default = GroupingV2Limits()

    def pick(declared: int | None, fallback: int) -> int:
        return fallback if declared is None else declared

    return GroupingV2Limits(
        group_output_tokens=pick(section.group_output_tokens, default.group_output_tokens),
        glimpse_chars=pick(section.glimpse_chars, default.glimpse_chars),
        max_set_members=pick(section.max_set_members, default.max_set_members),
        read_chars=pick(section.read_chars, default.read_chars),
        max_turns=pick(section.max_turns, default.max_turns),
        card_examples=pick(section.card_examples, default.card_examples),
        max_reduce_levels=pick(section.max_reduce_levels, default.max_reduce_levels),
        max_requests=section.max_requests,
    )
