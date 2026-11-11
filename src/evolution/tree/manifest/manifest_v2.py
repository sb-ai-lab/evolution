"""Strict native `/15` and `/16` campaign loader and conformance input builder.

ADR-0046 admits only those two file schemas. Both parse the full workflow;
`conformance_manifest` builds workflow-less input through the shared service
section parsers without admitting a historical file schema.

After file decoding and schema admission, validation order is unchanged:
1. Top-level missing keys, target/artifact missing keys, then unknown keys at
   the root, target (including timeouts), and artifacts, in that order.
2. Target fields (loopback last), artifact policy, then seed directory.
3. Campaign, environment, scorer, editor, reflector, Grouper, visibility,
   optional verbatim answers, corpus, then linked_from, each a closed section.

ManifestError exposes a fixed code, key path and optional domain statement,
never document contents or filesystem paths. Relative paths share the real
manifest directory; program containment and explicit role-file checks remain
here, while command binding owns executable admission. Shared service, command,
environment and corpus parsers also serve standalone evaluation manifests.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

from evolution.llm.backend import is_agent_model
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    CampaignPolicyV12,
    PolicyError,
)
from evolution.tree.execution.check_modes import CheckModes, parse_checks
from evolution.tree.execution.command_binding import argv_operands
from evolution.tree.execution.config import (
    DEFAULT_INPUT_TEXT_KEY,
    CommandSection,
    EnvironmentSection,
    ManifestError,
)
from evolution.tree.execution.evidence_settings import (
    EvidenceSettings,
    SettingsError,
    parse_evidence,
)
from evolution.tree.execution.interaction import SeedMode
from evolution.tree.execution.wire_v2 import IDENTIFIER, SEED_MODES
from evolution.tree.manifest.manifest_v15_help import (
    CHECKS_EXPECTED,
    COMPACTION_EXPECTED,
    EVIDENCE_EXPECTED,
    GROUPER_MODEL_KIND,
    REFLECTOR_GROUP_TEXT_CHARS,
    REFLECTOR_GROUPING_V15,
    REQUIRE_PARAMETERS_EXPECTED,
    SECRET_ORACLE_PATHS_EXPECTED,
    TOP_LEVEL_V15_EXPECTED,
    VERBATIM_ANSWERS_EXPECTED,
    connection_expected,
    grouper_expected,
)
from evolution.tree.roles.config import (
    _DEFAULT_BUDGETS,
    DEFAULT_CONTEXT_COMPACT_CHARS,
    DEFAULT_CONTEXT_MESSAGE_MAX_CHARS,
    GROUPING_V2_MINIMUMS,
    AgentCliEditorSection,
    AgentCliGrouperSection,
    ChatEditorSection,
    CommandGroupingSection,
    EditorSection,
    FamilySection,
    FieldsGroupingSection,
    GrouperSection,
    ModelGroupingSection,
    ReflectorSection,
    SelectionSection,
)
from evolution.tree.roles.grouping.reflect_grouping import FieldFeature
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.workspace import Budgets
from evolution.tree.storage.envelope import is_identifier
from evolution.tree.target.config import (
    DEFAULT_CONNECT_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MIN_INPUT_BYTES,
    DEFAULT_READ_SECONDS,
    ProbeSection,
    TargetBinding,
    TargetBindingV16,
    Timeouts,
    _normalized_loopback,
    _validate_probe_path,
)
from evolution.tree.target.connection_contract import (
    ConnectionContract,
    ContractError,
    MetricContract,
    RoleVisibility,
    SecretOraclePaths,
    VerbatimAnswers,
    parse_metric_contract,
    parse_role_visibility,
    parse_secret_oracle_paths,
    parse_verbatim_answers,
)

SCHEMA_V15: Literal["evolution-v2-manifest/15"] = "evolution-v2-manifest/15"
SCHEMA_V16: Literal["evolution-v2-manifest/16"] = "evolution-v2-manifest/16"


_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ROOT = "$"
#: The document's own file, for a manifest that cannot be read at all.
_DOCUMENT = "manifest"

_CONFORMANCE_KEYS = frozenset({"seed_root", "target", "artifacts"})
_TOP_LEVEL_KEYS_V15 = _CONFORMANCE_KEYS | {
    "schema",
    "campaign",
    "environment",
    "scorer",
    "editor",
    "corpus",
    "reflector",
    "role_visibility",
    "grouper",
}
_OPTIONAL_TOP_LEVEL_KEYS_V15 = frozenset({"linked_from", "verbatim_answers"})
_OPTIONAL_TOP_LEVEL_KEYS_V16 = _OPTIONAL_TOP_LEVEL_KEYS_V15 | {
    "secret_oracle_paths",
    "evidence",
    "checks",
}
_LINKED_FROM_V6_REQUIRED = frozenset({"run_dir", "baseline"})
_LINKED_FROM_V6_BASELINES = frozenset({"fresh", "inherit"})
_TARGET_REQUIRED = frozenset({"base_url", "seed_mode"})
_TARGET_OPTIONAL = frozenset(
    {"contract_digest", "settings_hash", "max_response_bytes", "min_input_bytes", "timeouts"}
)
_TIMEOUTS_OPTIONAL = frozenset({"connect_seconds", "read_seconds"})
_ARTIFACTS_REQUIRED = frozenset({"excludes", "dependency_paths", "writable_paths"})

_ENVIRONMENT_KINDS = frozenset({"static", "scripted"})
_ENVIRONMENT_REQUIRED = frozenset({"kind"})
_ENVIRONMENT_ALLOWED = _ENVIRONMENT_REQUIRED | {"input_text_key"}
_COMMAND_REQUIRED = frozenset({"argv", "cwd", "dependency_paths", "allowed_env"})
_CORPUS_REQUIRED = frozenset({"path"})

#: `editor.kind` by kind: `subprocess` keeps
#: `_COMMAND_REQUIRED`'s shape; `chat` and `agent_cli` are new closed shapes
#: dispatched by `_parse_editor`.
EDITOR_KINDS = ("subprocess", "chat", "agent_cli")
_EDITOR_KINDS = frozenset(EDITOR_KINDS)

REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

_CHAT_REQUIRED = frozenset({"kind", "model", "base_url"})
_CHAT_OPTIONAL = frozenset(
    {
        "api_key_env",
        "system_prompt",
        "proxy_env",
        "reasoning_effort",
        "provider_only",
        "quantizations",
        "max_output_tokens",
        "verify_ssl_certs",
        "max_turns",
        "context_compact_chars",
        "context_message_max_chars",
        "max_changed_files",
        "max_written_bytes",
        "max_payload_bytes",
    }
)

#: Conversation-context limits of a `chat` role, in characters of the
#: JSON-rendered message list: the size at which the tool loop folds the middle
#: of the conversation into a summary (to half this size), and the size to which
#: each message kept in the tail is truncated when it does. Defaults are the
#: values the loop used before the keys existed.

_AGENT_REQUIRED = frozenset({"kind", "model"})
_AGENT_OPTIONAL = frozenset(
    {
        "permission",
        "agent_bin",
        "allowed_env",
        "system_prompt",
        "reasoning_effort",
        "max_turns",
        "max_changed_files",
        "max_written_bytes",
        "max_payload_bytes",
    }
)

#: `reflector.kind`: the same three shapes
#: `editor.kind` selects, plus two keys every kind shares (`_REFLECTOR_COMMON`).
REFLECTOR_KINDS = EDITOR_KINDS
_REFLECTOR_KINDS = frozenset(REFLECTOR_KINDS)
_REFLECTOR_COMMON = frozenset({"environment_docs", "probe"})
#: The reflector never writes to the tree, so its `chat`/`agent_cli` role
#: drops the two write-admission budgets from the editor's optional surface.
_CHAT_REFLECTOR_OPTIONAL = _CHAT_OPTIONAL - {"max_changed_files", "max_written_bytes"}
_AGENT_REFLECTOR_OPTIONAL = _AGENT_OPTIONAL - {"max_changed_files", "max_written_bytes"}

_PROBE_REQUIRED = frozenset({"base_url"})
_PROBE_OPTIONAL = frozenset(
    {"health_path", "methods", "budget", "request_timeout_seconds", "description"}
)
_PROBE_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_MAX_PROBE_DESCRIPTION_BYTES = 8192
_GROUPING_FIELDS_REQUIRED = frozenset({"kind", "features"})
_GROUPING_COMMAND_REQUIRED = _COMMAND_REQUIRED | {"kind", "timeout_seconds", "config"}
#: `reflector.grouping.kind = "model"` by contract.
_GROUPING_MODEL_REQUIRED = frozenset(
    {
        "kind",
        "backend",
        "model",
        "base_url",
        "timeout_seconds",
        "concurrency",
        "max_input_chars",
        "max_output_tokens",
        "retry",
    }
)
_GROUPING_MODEL_OPTIONAL = frozenset(
    {
        "api_key_env",
        "proxy_env",
        "verify_ssl_certs",
        "reasoning_effort",
        "provider_only",
        "system_prompt",
        "representatives_per_case",
        "fallback",
    }
)
#: Mirrors `role_call.RoleCallRetry.from_payload`'s own closed key set, kept
#: local rather than importing a private name across modules: it identifies
#: the offending key for `retry.unknown`/`retry.missing` cheaply, and the
#: shape is frozen by the `role_call_retry/1` version string, not by this
#: loader, so the two copies cannot silently drift out from under a document.
_ROLE_CALL_RETRY_KEYS = frozenset({"version", "max_attempts", "total_budget_seconds", "backoff"})
_SELECTION_ALLOWED = frozenset({"focus_cases", "representatives_per_case", "context_page_size"})
_FAMILIES_NONE = frozenset({"kind"})
_FAMILIES_METADATA = frozenset({"kind", "path"})

#: `grouper.kind`: the `/15` Grouper's four kinds.
GROUPER_KINDS = ("fields", "command", "chat", "agent_cli")
_GROUPER_KINDS = frozenset(GROUPER_KINDS)
#: an `agent_cli` Grouper's `timeout_seconds` bounds the whole grouping
#: operation, the repair call included.
_AGENT_GROUPER_TIMEOUT = (10, 7200)


#: The caps admission enforces over one editor's changes, at the defaults
#: Both implementations use both `chat` and `agent_cli`.


#: `WorkflowSections.editor`'s type: which one a document selects with
#: `editor.kind` (rule R1: a `subprocess` document keeps parsing to a
#: `CommandSection`, unchanged).


#: `WorkflowSections.grouper`'s type: `chat` reuses the family-9 grouping
#: model's section under the kind name `chat`.


def effective_full_fallbacks(section: GrouperSection) -> int:
    return (
        2
        if section.max_consecutive_full_fallbacks is None
        else section.max_consecutive_full_fallbacks
    )


def effective_skips(section: ReflectorSection) -> int:
    return 2 if section.max_consecutive_skips is None else section.max_consecutive_skips


#: the limit an absent `reflector.group_text_chars` means.
_ABSENT_GROUP_TEXT_CHARS: Final[Mapping[str, int]] = MappingProxyType(
    {SCHEMA_V15: 512, SCHEMA_V16: 4096}
)


def effective_group_text_chars(section: ReflectorSection, schema: str) -> int | None:
    """The Reflector group text limit a `/15` or `/16` document puts in force:
    the declared value (`None` for no per-field limit), else 512 for `/15` and
    4,096 for `/16`. Any other schema has no such limit (`KeyError`)."""

    if section.group_text_chars_declared:
        return section.group_text_chars
    return _ABSENT_GROUP_TEXT_CHARS[schema]


@dataclass(frozen=True)
class WorkflowSections:
    campaign: CampaignPolicyV11
    environment: EnvironmentSection
    scorer: CommandSection
    editor: EditorSection
    reflector: ReflectorSection
    corpus_path: Path
    #: `/15` only (R5): the Scorer's `metric_contract/1`, kept beside the
    #: `CommandSection` the Editor and grouping share rather than in it.
    metric_contract: MetricContract | None = None
    #: `/15` only (R6): the Reflector's and the Editor's readable paths.
    role_visibility: RoleVisibility | None = None
    #: /15 only (R8): the top-level grouper section.
    grouper: GrouperSection | None = None
    #: /15 only (ADR-0038): the optional canned answers, `None` when undeclared.
    verbatim_answers: VerbatimAnswers | None = None
    secret_oracle_paths: SecretOraclePaths | None = None
    evidence: EvidenceSettings | None = None
    checks: CheckModes | None = None

    @property
    def connection(self) -> ConnectionContract | None:
        """`/15`'s two declarations together, or `None` for every older schema."""

        if self.metric_contract is None or self.role_visibility is None:
            return None
        return ConnectionContract(
            self.metric_contract,
            self.role_visibility,
            self.verbatim_answers,
            self.secret_oracle_paths,
            self.checks,
        )


@dataclass(frozen=True)
class ManifestV2:
    #: Native /15 or /16; the conformance builder uses an in-memory /1 label.
    schema: str
    path: Path
    seed_root: Path
    target: TargetBinding
    policy: ArtifactPolicyV1
    workflow: WorkflowSections | None = None
    #: Dotted document field -> the path's source spelling exactly as written in
    #: the JSON, or `None` for a declared `null` (`editor.system_prompt`,
    #: `editor.agent_bin`, `reflector.environment_docs`). Filled by the loader
    #: for exactly the sections it parses (`seed_root` for every schema, the
    #: workflow's path fields only when `workflow` is parsed); empty for a
    #: hand-built document. Data about the document, not part of campaign
    #: identity; read by `manifest_inventory`.
    declared: Mapping[str, str | None] = field(default_factory=dict)
    #: The linked_from.run_dir spelling, or None when no link is declared.
    linked_from: str | None = None
    #: linked_from.baseline, or fresh when no link is declared.
    linked_baseline: Literal["fresh", "inherit"] = "fresh"

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared", MappingProxyType(dict(self.declared)))

    @property
    def policy_digest(self) -> str:
        return self.policy.digest


def _path(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _dict_or_raise(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("bad_type", path)
    return value


def _check_missing(value: dict[str, Any], required: frozenset[str], prefix: str) -> None:
    missing = required - value.keys()
    if missing:
        raise ManifestError("missing_key", _path(prefix, sorted(missing)[0]))


def _check_unknown(value: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise ManifestError("unknown_key", _path(prefix, sorted(unknown)[0]))


def _str_or_raise(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    return value


def _optional_identifier(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or IDENTIFIER.match(value) is None:
        raise ManifestError("bad_type", path)
    return value


def _positive_int(value: object, path: str) -> int:
    if type(value) is not int or value <= 0:
        raise ManifestError("bad_type", path)
    return value


def _positive_number_to_float(value: object, path: str) -> float:
    """A finite, strictly positive number. `math.isfinite` is not redundant with
    `value <= 0`: `NaN <= 0` is false, so a NaN would otherwise pass every
    comparison here and reach `Timeouts` as a timeout that never elapses."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError("bad_type", path)
    if not math.isfinite(value) or value <= 0:
        raise ManifestError("bad_type", path)
    return float(value)


def _str_list(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError("bad_type", path)
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ManifestError("bad_type", f"{path}[{index}]")
        items.append(item)
    return tuple(items)


def _validate_structure(
    obj: dict[str, Any],
    keys: frozenset[str],
    optional: frozenset[str] = frozenset(),
    *,
    schema: str = SCHEMA_V15,
) -> None:
    """Shared structure precedence for campaigns, evaluation and conformance.

    Check all required top-level keys first, then target and artifact missing
    keys, then unknown keys at those same levels (timeouts under target).
    `optional` participates only in the top-level unknown-key check. Workflow
    sections close their own keys after all shared per-field checks, never here.

    A level whose own type is wrong (e.g. `target` is a string, not an
    object) contributes no missing/unknown checks here — that malformed
    value is reported as `bad_type` in stage 2 instead, once every
    structural check elsewhere in the document has cleared.
    """
    _check_missing(obj, keys, "")

    target = obj.get("target")
    if isinstance(target, dict):
        _check_missing(target, _TARGET_REQUIRED, "target")

    artifacts = obj.get("artifacts")
    if isinstance(artifacts, dict):
        _check_missing(artifacts, _ARTIFACTS_REQUIRED, "artifacts")

    _check_unknown(obj, keys | optional, "")

    if isinstance(target, dict):
        target_optional = _TARGET_OPTIONAL | (
            {"preflight_sample_retries"} if schema == SCHEMA_V16 else set()
        )
        _check_unknown(target, _TARGET_REQUIRED | target_optional, "target")
        timeouts = target.get("timeouts")
        if isinstance(timeouts, dict):
            _check_unknown(timeouts, _TIMEOUTS_OPTIONAL, "target.timeouts")

    if isinstance(artifacts, dict):
        _check_unknown(artifacts, _ARTIFACTS_REQUIRED, "artifacts")


def _parse_timeouts(value: object, prefix: str) -> Timeouts:
    if value is None:
        return Timeouts()
    obj = _dict_or_raise(value, prefix)
    connect = (
        _positive_number_to_float(obj["connect_seconds"], _path(prefix, "connect_seconds"))
        if "connect_seconds" in obj
        else DEFAULT_CONNECT_SECONDS
    )
    read = (
        _positive_number_to_float(obj["read_seconds"], _path(prefix, "read_seconds"))
        if "read_seconds" in obj
        else DEFAULT_READ_SECONDS
    )
    return Timeouts(connect, read)


def _parse_target(value: object, prefix: str, *, schema: str) -> TargetBinding:
    # `missing_key`/`unknown_key` for this level (and `target.timeouts`) are
    # already checked by `_validate_structure` (pass 1); this function only
    # does per-field `bad_type` and, last, the `not_loopback` check (pass 2).
    obj = _dict_or_raise(value, prefix)

    base_url = _str_or_raise(obj["base_url"], _path(prefix, "base_url"))
    raw_seed_mode = obj["seed_mode"]
    if not isinstance(raw_seed_mode, str) or raw_seed_mode not in SEED_MODES:
        raise ManifestError("bad_type", _path(prefix, "seed_mode"))
    seed_mode = cast(SeedMode, raw_seed_mode)
    contract_digest = _optional_identifier(
        obj.get("contract_digest"), _path(prefix, "contract_digest")
    )
    settings_hash = _optional_identifier(obj.get("settings_hash"), _path(prefix, "settings_hash"))
    max_response_bytes = (
        _positive_int(obj["max_response_bytes"], _path(prefix, "max_response_bytes"))
        if "max_response_bytes" in obj
        else DEFAULT_MAX_RESPONSE_BYTES
    )
    min_input_bytes = (
        _positive_int(obj["min_input_bytes"], _path(prefix, "min_input_bytes"))
        if "min_input_bytes" in obj
        else DEFAULT_MIN_INPUT_BYTES
    )
    timeouts = _parse_timeouts(obj.get("timeouts"), _path(prefix, "timeouts"))
    preflight_sample_retries = _optional_count(obj, "preflight_sample_retries", prefix, 0)
    # Construction is where the loopback rule runs, and it runs last, after every
    # per-field `bad_type` in this level (pass 2's documented order).
    binding = TargetBindingV16 if schema == SCHEMA_V16 else TargetBinding
    optional = (
        {"preflight_sample_retries": preflight_sample_retries} if schema == SCHEMA_V16 else {}
    )
    try:
        return binding(
            base_url=base_url,
            seed_mode=seed_mode,
            contract_digest=contract_digest,
            settings_hash=settings_hash,
            max_response_bytes=max_response_bytes,
            min_input_bytes=min_input_bytes,
            timeouts=timeouts,
            **optional,
        )
    except ValueError as exc:
        raise ManifestError("not_loopback", _path(prefix, "base_url")) from exc


def _parse_artifacts(value: object, prefix: str) -> ArtifactPolicyV1:
    # `missing_key`/`unknown_key` for this level are already checked by
    # `_validate_structure` (pass 1); this function only does per-field
    # `bad_type` (pass 2).
    obj = _dict_or_raise(value, prefix)
    excludes = _str_list(obj["excludes"], _path(prefix, "excludes"))
    dependency_paths = _str_list(obj["dependency_paths"], _path(prefix, "dependency_paths"))
    writable_paths = _str_list(obj["writable_paths"], _path(prefix, "writable_paths"))
    try:
        return ArtifactPolicyV1(excludes, dependency_paths, writable_paths)
    except ValueError as exc:
        raise ManifestError("bad_type", prefix) from exc


def _identifier(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    if not is_identifier(value):
        raise ManifestError("bad_value", path)
    return value


def _positive_seconds(value: object, path: str) -> float:
    """A finite, strictly positive number: `bad_type` for the wrong Python
    type, `bad_value` for a well-typed number that is not admissible (unlike
    `_positive_number_to_float`, which the `/1` `target.timeouts` fields use
    and which reports both as `bad_type` — the `/2` sections follow the
    project-wide convention instead, spelled out in `campaign_policy`'s
    module docstring)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError("bad_type", path)
    if not math.isfinite(value) or value <= 0:
        raise ManifestError("bad_value", path)
    return float(value)


def _argv(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError("bad_type", path)
    if not value:
        raise ManifestError("bad_value", path)
    items: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str):
            raise ManifestError("bad_type", item_path)
        if not item.strip():
            raise ManifestError("bad_value", item_path)
        items.append(item)
    return tuple(items)


def _env_names(value: object, path: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise ManifestError("bad_type", path)
    names: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str):
            raise ManifestError("bad_type", item_path)
        if not item.strip() or "=" in item:
            raise ManifestError("bad_value", item_path)
        names.append(item)
    return frozenset(names)


def _argv_program_paths(argv: tuple[str, ...], cwd: Path) -> list[tuple[int, Path]]:
    """Indices into `argv` and the resolved path of every `argv[1:]` entry
    that currently names an existing file — `command_binding.argv_operands`'s
    walk (skip `-c`/`-m` and its operand, skip a flag), each operand resolved
    against `cwd`, minus resolving `argv[0]`: the bound executable is
    `CommandBinding`'s concern, not this loader's static check."""
    found: list[tuple[int, Path]] = []
    for index, argument in argv_operands(argv):
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            if candidate.is_file():
                found.append((index, candidate.resolve()))
        except OSError:
            continue
    return found


def _campaign_error(exc: PolicyError, prefix: str) -> ManifestError:
    """A campaign policy refusal at its manifest path, with its expected text."""

    return ManifestError(
        exc.code, _path(prefix, exc.path) if exc.path else prefix, expected=exc.expected
    )


def _parse_campaign_v11(value: object, prefix: str) -> CampaignPolicyV11:
    """`/15`'s campaign (family 14): `/14`'s plus the four `/15` keys (R2)."""

    obj = _dict_or_raise(value, prefix)
    try:
        return CampaignPolicyV11.from_payload(obj)
    except PolicyError as exc:
        raise _campaign_error(exc, prefix) from exc


def _parse_campaign_v12(value: object, prefix: str) -> CampaignPolicyV12:
    obj = _dict_or_raise(value, prefix)
    try:
        return CampaignPolicyV12.from_payload(obj)
    except PolicyError as exc:
        raise _campaign_error(exc, prefix) from exc


def _contract_refusal(exc: ContractError, prefix: str) -> ManifestError:
    """A connection declaration's refusal at its full path, with its sentence (R7)."""

    detail = _path(prefix, exc.path) if exc.path else prefix
    section = None
    if exc.section is not None:
        section = _path(prefix, exc.section) if exc.section else prefix
    return ManifestError(
        exc.code, detail, expected=connection_expected(exc.code, detail, section=section)
    )


def _parse_scorer_v15(
    value: object, manifest_dir: Path, seed_root: Path
) -> tuple[CommandSection, MetricContract]:
    """`/15`'s `scorer` (R5): the `/14` command keys plus the required
    `metric_contract`, checked as one closed set, then the command, then the
    contract. A missing contract carries its sentence; every other key keeps
    the plain `/14` refusal."""

    obj = _dict_or_raise(value, "scorer")
    keys = _COMMAND_REQUIRED | {"timeout_seconds", "metric_contract"}
    try:
        _check_missing(obj, keys, "scorer")
    except ManifestError as exc:
        if exc.detail == "scorer.metric_contract":
            raise ManifestError(
                exc.code, exc.detail, expected=connection_expected(exc.code, exc.detail)
            ) from exc
        raise
    _check_unknown(obj, keys, "scorer")
    command_value = dict(obj)
    raw_contract = command_value.pop("metric_contract")
    command = _parse_command(
        command_value, "scorer", manifest_dir, seed_root, with_timeout=True, require_kind=False
    )
    try:
        contract = parse_metric_contract(raw_contract)
    except ContractError as exc:
        raise _contract_refusal(exc, "scorer.metric_contract") from exc
    return command, contract


def _parse_role_visibility(value: object) -> RoleVisibility:
    """`/15`'s top-level `role_visibility` (R6)."""

    try:
        return parse_role_visibility(value)
    except ContractError as exc:
        raise _contract_refusal(exc, "role_visibility") from exc


def _parse_verbatim_answers(value: object) -> VerbatimAnswers:
    """`/15`'s optional top-level `verbatim_answers` (ADR-0038)."""

    try:
        return parse_verbatim_answers(value)
    except ContractError as exc:
        detail = _path("verbatim_answers", exc.path) if exc.path else "verbatim_answers"
        raise ManifestError(exc.code, detail, expected=VERBATIM_ANSWERS_EXPECTED) from exc


def _parse_secret_oracle_paths(value: object) -> SecretOraclePaths:
    try:
        return parse_secret_oracle_paths(value)
    except ContractError as exc:
        detail = _path("secret_oracle_paths", exc.path) if exc.path else "secret_oracle_paths"
        raise ManifestError(exc.code, detail, expected=SECRET_ORACLE_PATHS_EXPECTED) from exc


def _parse_checks(value: object) -> CheckModes:
    try:
        return parse_checks(value)
    except SettingsError as exc:
        detail = _path("checks", exc.path) if exc.path else "checks"
        raise ManifestError(exc.code, detail, expected=CHECKS_EXPECTED) from exc


def _parse_evidence(value: object) -> EvidenceSettings:
    try:
        return parse_evidence(value)
    except SettingsError as exc:
        detail = _path("evidence", exc.path) if exc.path else "evidence"
        raise ManifestError(exc.code, detail, expected=EVIDENCE_EXPECTED) from exc


def _parse_linked_from_v6(obj: dict[str, Any]) -> tuple[str | None, Literal["fresh", "inherit"]]:
    """Optional link: a closed `run_dir`/`baseline` object, absent means fresh."""

    if "linked_from" not in obj:
        return None, "fresh"
    value = obj["linked_from"]
    section = _dict_or_raise(value, "linked_from")
    _check_missing(section, _LINKED_FROM_V6_REQUIRED, "linked_from")
    _check_unknown(section, _LINKED_FROM_V6_REQUIRED, "linked_from")
    run_dir = _str_or_raise(section["run_dir"], "linked_from.run_dir")
    if run_dir == "":
        raise ManifestError("bad_value", "linked_from.run_dir")
    baseline = _str_or_raise(section["baseline"], "linked_from.baseline")
    if baseline not in _LINKED_FROM_V6_BASELINES:
        raise ManifestError("bad_value", "linked_from.baseline")
    return run_dir, cast(Literal["fresh", "inherit"], baseline)


def _parse_environment(value: object, prefix: str) -> EnvironmentSection:
    obj = _dict_or_raise(value, prefix)
    _check_missing(obj, _ENVIRONMENT_REQUIRED, prefix)
    _check_unknown(obj, _ENVIRONMENT_ALLOWED, prefix)

    kind_raw = obj["kind"]
    if not isinstance(kind_raw, str):
        raise ManifestError("bad_type", _path(prefix, "kind"))
    if kind_raw not in _ENVIRONMENT_KINDS:
        raise ManifestError("bad_value", _path(prefix, "kind"))
    kind = cast(Literal["static", "scripted"], kind_raw)

    if kind == "scripted":
        if "input_text_key" in obj:
            raise ManifestError("unknown_key", _path(prefix, "input_text_key"))
        return EnvironmentSection(kind="scripted")

    input_text_key = _identifier(
        obj.get("input_text_key", DEFAULT_INPUT_TEXT_KEY), _path(prefix, "input_text_key")
    )
    return EnvironmentSection(kind="static", input_text_key=input_text_key)


def _parse_command(
    value: object,
    prefix: str,
    manifest_dir: Path,
    seed_root: Path,
    *,
    with_timeout: bool,
    require_kind: bool,
) -> CommandSection:
    obj = _dict_or_raise(value, prefix)
    keys = _COMMAND_REQUIRED
    if require_kind:
        keys = keys | {"kind"}
    if with_timeout:
        keys = keys | {"timeout_seconds"}
    _check_missing(obj, keys, prefix)
    _check_unknown(obj, keys, prefix)

    if require_kind:
        kind = obj["kind"]
        if not isinstance(kind, str):
            raise ManifestError("bad_type", _path(prefix, "kind"))
        if kind != "subprocess":
            raise ManifestError("bad_value", _path(prefix, "kind"))

    argv = _argv(obj["argv"], _path(prefix, "argv"))
    cwd_raw = _str_or_raise(obj["cwd"], _path(prefix, "cwd"))
    cwd = (manifest_dir / cwd_raw).resolve()
    dependency_raw = _str_list(obj["dependency_paths"], _path(prefix, "dependency_paths"))
    dependency_paths = tuple((manifest_dir / dep).resolve() for dep in dependency_raw)
    allowed_env = _env_names(obj["allowed_env"], _path(prefix, "allowed_env"))
    timeout_seconds: float | None = None
    if with_timeout:
        timeout_seconds = _positive_seconds(
            obj["timeout_seconds"], _path(prefix, "timeout_seconds")
        )

    # Static seed containment, last (pass 2's documented convention: the
    # per-field type/value checks above run first, the derived check last).
    seed_real = Path(os.path.realpath(seed_root))

    def _refuse_if_inside_seed(candidate: Path, field_path: str) -> None:
        real = Path(os.path.realpath(candidate))
        if real == seed_real or seed_real in real.parents:
            raise ManifestError("program_inside_seed", field_path)

    _refuse_if_inside_seed(cwd, _path(prefix, "cwd"))
    for index, dependency in enumerate(dependency_paths):
        _refuse_if_inside_seed(dependency, f"{_path(prefix, 'dependency_paths')}[{index}]")
    for index, program_path in _argv_program_paths(argv, cwd):
        _refuse_if_inside_seed(program_path, f"{_path(prefix, 'argv')}[{index}]")

    return CommandSection(
        argv=argv,
        cwd=cwd,
        dependency_paths=dependency_paths,
        allowed_env=allowed_env,
        timeout_seconds=timeout_seconds,
    )


def _refuse_program_inside_seed(candidate: Path, field_path: str, seed_root: Path) -> None:
    """The `_refuse_if_inside_seed` closure pattern of `_parse_command`, as a
    standalone function for the new `chat`/`agent_cli` single-path fields
    (`system_prompt`, `agent_bin`), each checked once rather than in a loop."""
    seed_real = Path(os.path.realpath(seed_root))
    real = Path(os.path.realpath(candidate))
    if real == seed_real or seed_real in real.parents:
        raise ManifestError("program_inside_seed", field_path)


def _existing_program_file(
    value: object, path: str, manifest_dir: Path, *, require_executable: bool
) -> Path:
    """A `system_prompt` or `agent_bin` value: a path that resolves against
    `manifest_dir` and must exist and name a regular file -- `bad_path`. A
    symlink to one is accepted (fix round 1, finding 3): `/usr/local/bin/codex`
    as a symlink is the normal install shape for an agent CLI, and seed
    containment (the caller's job, checked last) already resolves through
    `realpath` regardless of what this function returns. The returned path is
    the manifest-dir-joined form as declared, not `realpath`'d -- this
    function does not itself resolve symlinks away. `agent_bin` additionally
    requires the executable bit, also `bad_path`."""
    raw = _str_or_raise(value, path)
    candidate = Path(raw)
    resolved = candidate if candidate.is_absolute() else manifest_dir / candidate
    if not resolved.is_file():
        raise ManifestError("bad_path", path)
    if require_executable and not os.access(resolved, os.X_OK):
        raise ManifestError("bad_path", path)
    return resolved


def _env_name(value: object, path: str) -> str:
    """A single environment-variable name for `api_key_env` or `proxy_env`:
    `_env_names`' own per-item rule (non-blank, no `=`), applied to one value
    and reported at its own path rather than an index (fix round 1, finding
    1 -- W1's `_env_names` grammar is lowercase-permissive by design, and
    `allowed_env` keeps exactly that same rule for the same reason: both
    forward a name the operator's environment already owns)."""
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    if not value.strip() or "=" in value:
        raise ManifestError("bad_value", path)
    return value


def _optional_env_name(obj: dict[str, Any], key: str, prefix: str) -> str | None:
    """An optional environment-variable name: omitted or `null` is undeclared
    (`None`, an unauthenticated chat role); a declared value keeps
    `_env_name`'s rules."""
    value = obj.get(key)
    return None if value is None else _env_name(value, _path(prefix, key))


def _optional_count(obj: Mapping[str, Any], key: str, prefix: str, minimum: int) -> int | None:
    if key not in obj:
        return None
    value = obj[key]
    if type(value) is not int:
        raise ManifestError("bad_type", _path(prefix, key))
    if value < minimum:
        raise ManifestError("bad_value", _path(prefix, key))
    return value


def _positive_count(value: object, path: str) -> int:
    """A positive integer count (`max_turns`, `max_output_tokens`, the three
    budgets): `bad_type` for a non-`int` or a `bool` (`_positive_seconds`'
    convention), `bad_value` for an `int` below `1`."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError("bad_type", path)
    if value < 1:
        raise ManifestError("bad_value", path)
    return value


def _require_parameters(
    obj: Mapping[str, Any],
    prefix: str,
    provider_only: tuple[str, ...] | None,
    provider_order: tuple[str, ...] | None,
) -> bool | None:
    if "require_parameters" not in obj:
        return None
    value = _bool_or_raise(obj["require_parameters"], _path(prefix, "require_parameters"))
    if provider_only is None and provider_order is None:
        raise ManifestError(
            "bad_value",
            _path(prefix, "require_parameters"),
            expected=REQUIRE_PARAMETERS_EXPECTED,
        )
    return value


def _bool_or_raise(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError("bad_type", path)
    return value


def _ranged_int(value: object, path: str, low: int, high: int) -> int:
    """An `int` (never a `bool`) within `[low, high]`: `bad_type` for the
    wrong Python type, `bad_value` for a well-typed integer out of range
    (`reflector.grouping.kind: model`'s `timeout_seconds`, `concurrency`,
    `max_input_chars`, `max_output_tokens` and `representatives_per_case`)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError("bad_type", path)
    if not low <= value <= high:
        raise ManifestError("bad_value", path)
    return value


def _reasoning_effort(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    if value not in REASONING_EFFORTS:
        raise ManifestError("bad_value", path)
    return value


def _optional_nonempty_str_list(value: object, path: str) -> tuple[str, ...] | None:
    """`provider_only`/`quantizations`: `None`, or a non-empty list of
    non-empty strings (`_str_list` gives `bad_type` on the wrong shape; the
    emptiness checks here are `bad_value`)."""
    if value is None:
        return None
    items = _str_list(value, path)
    if not items or any(not item.strip() for item in items):
        raise ManifestError("bad_value", path)
    return items


def _provider_routing(
    obj: dict[str, Any], prefix: str, provider_only: tuple[str, ...] | None
) -> tuple[tuple[str, ...] | None, bool | None]:
    order = _optional_nonempty_str_list(obj.get("provider_order"), _path(prefix, "provider_order"))
    if "provider_order" in obj and (
        order is None or provider_only is not None or len(set(order)) != len(order)
    ):
        raise ManifestError("bad_value", _path(prefix, "provider_order"))
    fallbacks = obj.get("allow_fallbacks")
    if "allow_fallbacks" in obj and (type(fallbacks) is not bool or order is None):
        raise ManifestError("bad_value", _path(prefix, "allow_fallbacks"))
    return order, fallbacks


def _is_syntactically_valid_hostname(hostname: str) -> bool:
    """`urlsplit`'s `hostname` already unwraps a bracketed IPv6 literal to its
    bare form (so it contains `:`); such a value is valid only if
    `ipaddress.ip_address` parses it. Anything else is a dotted name, valid
    only if every label matches `_HOSTNAME_LABEL_RE`."""
    if ":" in hostname:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return True
    return all(_HOSTNAME_LABEL_RE.match(label) for label in hostname.split("."))


def _chat_base_url(value: object, path: str) -> str:
    """`https` or `http` to any host; a non-empty host, a port that parses, and
    no userinfo, query or fragment -- else `bad_value` (distinct
    from `/1` `target.base_url`'s stricter `_normalized_loopback`, which also
    requires loopback and an empty path).

    Plain `http` is no longer limited to a loopback host (PDR-0026 and
    PDR-0005 amendments 2026-10-21): inside a closed network the model API is
    often plain HTTP on an internal host, and the operator owns that network.
    The cost is named, not refused: `evo tree evolve` warns about every chat
    role on `http` to a host outside `_LOOPBACK_HOSTS`
    (`evolve.unencrypted_model_roles`). Any other scheme is refused.

    The whole URL is decided here, at load, because every refusal this misses
    becomes an `httpx.InvalidURL` or a `provider_error` while the client is
    built or the request is sent -- after the campaign and the attempt log
    exist. So the host must be non-empty (`https:` and `https://` are
    path-only, not endpoints), the port must be a number in range (`parts.port`
    raises otherwise, which is why it is read inside the `try`), and any
    userinfo at all is refused, the empty `https://@host` form included:
    `parts.username` is `""` there, which is falsy but is still a credential
    position in the authority. Any whitespace or Unicode control character
    (`str.isspace` or a `unicodedata.category` starting `C`) anywhere in the
    raw string is refused before the URL is even parsed, since one can hide
    inside a value that otherwise reads as a plain hostname. And the hostname
    itself must be syntactically valid per `_is_syntactically_valid_hostname`
    -- a bracketed IPv6 literal `ipaddress.ip_address` parses, or a dotted name
    whose labels are all valid DNS labels (a bare `localhost` and a dotted
    IPv4 address both qualify).
    """
    raw = _str_or_raise(value, path)
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in raw):
        raise ManifestError("bad_value", path)
    try:
        parts = urlsplit(raw)
        # Both are properties that parse: `port` raises on a non-numeric or
        # out-of-range value, and `hostname` on a malformed IPv6 authority.
        hostname, _port = parts.hostname, parts.port
    except ValueError:
        raise ManifestError("bad_value", path) from None
    if not hostname or "@" in parts.netloc or parts.query or parts.fragment:
        raise ManifestError("bad_value", path)
    if not _is_syntactically_valid_hostname(hostname):
        raise ManifestError("bad_value", path)
    if parts.scheme in ("https", "http"):
        return raw
    raise ManifestError("bad_value", path)


def _parse_editor_budgets(obj: dict[str, Any], prefix: str) -> Budgets:
    raw_max_changed = obj.get("max_changed_files")
    max_changed_files = (
        None
        if raw_max_changed is None
        else _positive_count(raw_max_changed, _path(prefix, "max_changed_files"))
    )
    max_written_bytes = (
        _positive_count(obj["max_written_bytes"], _path(prefix, "max_written_bytes"))
        if "max_written_bytes" in obj
        else _DEFAULT_BUDGETS.max_written_bytes
    )
    max_payload_bytes = (
        _positive_count(obj["max_payload_bytes"], _path(prefix, "max_payload_bytes"))
        if "max_payload_bytes" in obj
        else _DEFAULT_BUDGETS.max_payload_bytes
    )
    return Budgets(
        max_changed_files=max_changed_files,
        max_written_bytes=max_written_bytes,
        max_payload_bytes=max_payload_bytes,
    )


def _parse_chat_editor(
    obj: dict[str, Any],
    prefix: str,
    manifest_dir: Path,
    seed_root: Path,
    *,
    schema: str,
    optional: frozenset[str] = _CHAT_OPTIONAL,
) -> ChatEditorSection:
    # Missing/unknown first (the closed-section convention); per-field checks
    # next; the path-resolution and seed-containment checks last. `optional`
    # is `_CHAT_OPTIONAL` for `editor.kind: chat` and `_CHAT_REFLECTOR_OPTIONAL`
    # for `reflector.kind: chat` (the reflector drops the two write budgets).
    # Both native schemas require retry, but its missing check stays after
    # per-field/path checks to preserve the established refusal precedence.
    allowed_optional = optional | {"retry"}
    if schema == SCHEMA_V16:
        allowed_optional |= {
            "max_protocol_repairs",
            "max_consecutive_tool_failures",
            "provider_order",
            "allow_fallbacks",
            "require_parameters",
            "nearest_paths",
        }
    if not _CHAT_REQUIRED <= obj.keys():
        raise ManifestError("missing_key", prefix)
    _check_unknown(obj, _CHAT_REQUIRED | allowed_optional, prefix)

    # chat roles state their compaction before per-field checks.
    for key in ("context_compact_chars", "context_message_max_chars"):
        if key not in obj:
            raise ManifestError(
                "missing_key", _path(prefix, key), expected=COMPACTION_EXPECTED[key]
            )

    model = _str_or_raise(obj["model"], _path(prefix, "model"))
    if not model.strip():
        raise ManifestError("bad_value", _path(prefix, "model"))
    base_url = _chat_base_url(obj["base_url"], _path(prefix, "base_url"))
    api_key_env = _optional_env_name(obj, "api_key_env", prefix)

    proxy_env_raw = obj.get("proxy_env")
    proxy_env = (
        None if proxy_env_raw is None else _env_name(proxy_env_raw, _path(prefix, "proxy_env"))
    )

    reasoning_effort = _reasoning_effort(
        obj.get("reasoning_effort"), _path(prefix, "reasoning_effort")
    )

    provider_only = _optional_nonempty_str_list(
        obj.get("provider_only"), _path(prefix, "provider_only")
    )
    provider_order, allow_fallbacks = _provider_routing(obj, prefix, provider_only)
    require_parameters = _require_parameters(obj, prefix, provider_only, provider_order)
    quantizations = _optional_nonempty_str_list(
        obj.get("quantizations"), _path(prefix, "quantizations")
    )
    if quantizations is not None and provider_only is None and provider_order is None:
        raise ManifestError("bad_value", _path(prefix, "quantizations"))

    max_output_tokens = (
        _positive_count(obj["max_output_tokens"], _path(prefix, "max_output_tokens"))
        if "max_output_tokens" in obj
        else 8192
    )
    verify_ssl_certs = (
        _bool_or_raise(obj["verify_ssl_certs"], _path(prefix, "verify_ssl_certs"))
        if "verify_ssl_certs" in obj
        else True
    )
    max_turns = (
        _positive_count(obj["max_turns"], _path(prefix, "max_turns")) if "max_turns" in obj else 64
    )
    context_compact_chars = (
        _positive_count(obj["context_compact_chars"], _path(prefix, "context_compact_chars"))
        if "context_compact_chars" in obj
        else DEFAULT_CONTEXT_COMPACT_CHARS
    )
    context_message_max_chars = (
        _positive_count(
            obj["context_message_max_chars"], _path(prefix, "context_message_max_chars")
        )
        if "context_message_max_chars" in obj
        else DEFAULT_CONTEXT_MESSAGE_MAX_CHARS
    )
    # A kept message may not exceed the compaction target (half the trigger):
    # otherwise one truncated message could keep the context above the trigger.
    if context_message_max_chars > context_compact_chars // 2:
        raise ManifestError("bad_value", _path(prefix, "context_message_max_chars"))
    budgets = _parse_editor_budgets(obj, prefix)

    system_prompt: Path | None = None
    if obj.get("system_prompt") is not None:
        system_prompt = _existing_program_file(
            obj["system_prompt"],
            _path(prefix, "system_prompt"),
            manifest_dir,
            require_executable=False,
        )
        _refuse_program_inside_seed(system_prompt, _path(prefix, "system_prompt"), seed_root)

    if "retry" not in obj:
        raise ManifestError("missing_key", _path(prefix, "retry"))
    retry = _parse_role_call_retry(obj["retry"], prefix)

    return ChatEditorSection(
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        system_prompt=system_prompt,
        proxy_env=proxy_env,
        reasoning_effort=reasoning_effort,
        provider_only=provider_only,
        provider_order=provider_order,
        allow_fallbacks=allow_fallbacks,
        require_parameters=require_parameters,
        nearest_paths=_optional_count(obj, "nearest_paths", prefix, 0),
        quantizations=quantizations,
        max_output_tokens=max_output_tokens,
        verify_ssl_certs=verify_ssl_certs,
        max_turns=max_turns,
        context_compact_chars=context_compact_chars,
        context_message_max_chars=context_message_max_chars,
        budgets=budgets,
        retry=retry,
        max_protocol_repairs=_optional_count(obj, "max_protocol_repairs", prefix, 0),
        max_consecutive_tool_failures=_optional_count(
            obj, "max_consecutive_tool_failures", prefix, 1
        ),
    )


def _parse_agent_cli_editor(
    obj: dict[str, Any],
    prefix: str,
    manifest_dir: Path,
    seed_root: Path,
    *,
    schema: str,
    optional: frozenset[str] = _AGENT_OPTIONAL,
) -> AgentCliEditorSection:
    # `optional` is `_AGENT_OPTIONAL` for `editor.kind: agent_cli` and
    # `_AGENT_REFLECTOR_OPTIONAL` for `reflector.kind: agent_cli`. `retry` is
    # never admitted here (PDR-0019, ADR-0023: retry is a chat-only addition).
    if not _AGENT_REQUIRED <= obj.keys():
        raise ManifestError("missing_key", prefix)
    _check_unknown(obj, _AGENT_REQUIRED | optional, prefix)

    model = _str_or_raise(obj["model"], _path(prefix, "model"))
    if not is_agent_model(model):
        raise ManifestError("bad_value", _path(prefix, "model"))

    permission_raw = obj.get("permission", "safe")
    if not isinstance(permission_raw, str):
        raise ManifestError("bad_type", _path(prefix, "permission"))
    if permission_raw not in ("safe", "unsafe"):
        raise ManifestError("bad_value", _path(prefix, "permission"))
    permission = cast(Literal["safe", "unsafe"], permission_raw)

    allowed_env = (
        _env_names(obj["allowed_env"], _path(prefix, "allowed_env"))
        if "allowed_env" in obj
        else frozenset()
    )

    reasoning_effort = _reasoning_effort(
        obj.get("reasoning_effort"), _path(prefix, "reasoning_effort")
    )

    max_turns_raw = obj.get("max_turns")
    max_turns = (
        None
        if max_turns_raw is None
        else _positive_count(max_turns_raw, _path(prefix, "max_turns"))
    )
    if max_turns is not None and model.partition("/")[0] == "hermes":
        raise ManifestError("bad_value", _path(prefix, "max_turns"))

    budgets = _parse_editor_budgets(obj, prefix)

    system_prompt: Path | None = None
    if obj.get("system_prompt") is not None:
        system_prompt = _existing_program_file(
            obj["system_prompt"],
            _path(prefix, "system_prompt"),
            manifest_dir,
            require_executable=False,
        )
        _refuse_program_inside_seed(system_prompt, _path(prefix, "system_prompt"), seed_root)

    agent_bin: Path | None = None
    if obj.get("agent_bin") is not None:
        agent_bin = _existing_program_file(
            obj["agent_bin"], _path(prefix, "agent_bin"), manifest_dir, require_executable=True
        )
        _refuse_program_inside_seed(agent_bin, _path(prefix, "agent_bin"), seed_root)

    return AgentCliEditorSection(
        model=model,
        permission=permission,
        agent_bin=agent_bin,
        allowed_env=allowed_env,
        system_prompt=system_prompt,
        reasoning_effort=reasoning_effort,
        max_turns=max_turns,
        budgets=budgets,
    )


def _parse_editor(
    value: object,
    prefix: str,
    manifest_dir: Path,
    seed_root: Path,
    *,
    schema: str,
) -> CommandSection | ChatEditorSection | AgentCliEditorSection:
    """Dispatch `editor` by `kind` to one parser per kind by kind.

    `kind` missing is `missing_key at editor` (the section itself, not a
    per-key path -- the same collapse a missing *other* required key gets
    inside `_parse_chat_editor`/`_parse_agent_cli_editor`, because which keys
    are required depends on a `kind` that, in that case, is not yet known to
    be well-formed); a non-string `kind` is `bad_type at editor.kind`; a
    string outside `EDITOR_KINDS` is `bad_value at editor.kind`.
    """
    obj = _dict_or_raise(value, prefix)
    if "kind" not in obj:
        raise ManifestError("missing_key", prefix)
    kind = obj["kind"]
    if not isinstance(kind, str):
        raise ManifestError("bad_type", _path(prefix, "kind"))
    if kind not in _EDITOR_KINDS:
        raise ManifestError("bad_value", _path(prefix, "kind"))
    if kind == "subprocess":
        return _parse_command(
            obj, prefix, manifest_dir, seed_root, with_timeout=False, require_kind=True
        )
    if kind == "chat":
        return _parse_chat_editor(obj, prefix, manifest_dir, seed_root, schema=schema)
    return _parse_agent_cli_editor(obj, prefix, manifest_dir, seed_root, schema=schema)


def _parse_environment_docs(
    value: object, path: str, manifest_dir: Path, seed_root: Path
) -> Path | None:
    """`reflector.environment_docs`: `None`, or a directory outside the seed.

    Mirrors `_resolve_seed_root`'s own existence/non-symlink checks (`bad_path`
    for anything else -- missing, a file, a symlink), then the seed-containment
    check every other reflector/editor path gets (`program_inside_seed`).
    """
    if value is None:
        return None
    raw = _str_or_raise(value, path)
    candidate = Path(raw)
    declared = candidate if candidate.is_absolute() else manifest_dir / candidate
    try:
        info = declared.lstat()
    except OSError as exc:
        raise ManifestError("bad_path", path) from exc
    if stat.S_ISLNK(info.st_mode) or not declared.is_dir():
        raise ManifestError("bad_path", path)
    resolved = declared.resolve()
    _refuse_program_inside_seed(resolved, path, seed_root)
    return resolved


def _probe_health_path(value: object, path: str) -> str:
    if value is None:
        return "/health"
    return _validate_probe_path(value, path)


def _probe_methods(value: object, path: str) -> tuple[str, ...]:
    if value is None:
        return ("GET", "POST")
    items = _str_list(value, path)
    if not items:
        raise ManifestError("bad_value", path)
    seen: set[str] = set()
    for item in items:
        if item not in _PROBE_METHODS or item in seen:
            raise ManifestError("bad_value", path)
        seen.add(item)
    return items


def _probe_description(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ManifestError("bad_value", path) from None
    if len(encoded) > _MAX_PROBE_DESCRIPTION_BYTES:
        raise ManifestError("bad_value", path)
    return value


def _parse_probe_config(
    value: object, path: str, *, target_base_url: str, schema: str
) -> ProbeSection:
    """Parse a complete non-null probe configuration."""
    obj = _dict_or_raise(value, path)
    _check_missing(obj, _PROBE_REQUIRED, path)
    _check_unknown(
        obj,
        _PROBE_REQUIRED
        | _PROBE_OPTIONAL
        | ({"string_body", "on_unavailable"} if schema == SCHEMA_V16 else set()),
        path,
    )

    base_url = _str_or_raise(obj["base_url"], _path(path, "base_url"))
    try:
        base_url = _normalized_loopback(base_url)
    except ValueError as exc:
        raise ManifestError("not_loopback", _path(path, "base_url")) from exc
    # All currently admitted hosts denote a loopback interface. Comparing the
    # explicit numeric port therefore catches the conservatively equivalent
    # localhost/IPv4/IPv6 spellings without widening URL admission.
    if urlsplit(base_url).port == urlsplit(target_base_url).port:
        raise ManifestError("bad_value", _path(path, "base_url"))

    health_path = _probe_health_path(obj.get("health_path"), _path(path, "health_path"))
    methods = _probe_methods(obj.get("methods"), _path(path, "methods"))
    budget = _positive_count(obj["budget"], _path(path, "budget")) if "budget" in obj else 32
    request_timeout_seconds = (
        _positive_seconds(obj["request_timeout_seconds"], _path(path, "request_timeout_seconds"))
        if "request_timeout_seconds" in obj
        else 60.0
    )
    description = _probe_description(obj.get("description"), _path(path, "description"))

    string_body = None
    if "string_body" in obj:
        raw_mode = _str_or_raise(obj["string_body"], _path(path, "string_body"))
        if raw_mode not in {"send", "parse"}:
            raise ManifestError("bad_value", _path(path, "string_body"))
        string_body = cast(Literal["send", "parse"], raw_mode)

    on_unavailable = None
    if "on_unavailable" in obj:
        raw_availability = obj["on_unavailable"]
        if raw_availability not in ("suspend", "continue"):
            raise ManifestError("bad_value", _path(path, "on_unavailable"))
        on_unavailable = cast(Literal["suspend", "continue"], raw_availability)

    return ProbeSection(
        base_url=base_url,
        health_path=health_path,
        methods=methods,
        budget=budget,
        request_timeout_seconds=request_timeout_seconds,
        description=description,
        string_body=string_body,
        on_unavailable=on_unavailable,
    )


def _parse_probe(
    value: object, path: str, *, target_base_url: str, schema: str
) -> ProbeSection | None:
    """Parse optional bounded access to a distinct disposable environment API."""
    if value is None:
        return None
    return _parse_probe_config(value, path, target_base_url=target_base_url, schema=schema)


def _parse_reflection_config(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("bad_type", path)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestError("bad_type", path) from exc
    if len(encoded) > 16 * 1024 or _json_depth(value) > 8:
        raise ManifestError("bad_value", path)
    return MappingProxyType(json.loads(encoded))


def _json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _parse_fields_grouping(obj: dict[str, Any], path: str) -> FieldsGroupingSection:
    """The closed fields Grouper shape, also shared by role fallbacks."""
    _check_missing(obj, _GROUPING_FIELDS_REQUIRED, path)
    _check_unknown(obj, _GROUPING_FIELDS_REQUIRED, path)
    features = obj["features"]
    if not isinstance(features, list):
        raise ManifestError("bad_type", _path(path, "features"))
    if not 1 <= len(features) <= 16:
        raise ManifestError("bad_value", _path(path, "features"))
    parsed: list[FieldFeature] = []
    names: set[str] = set()
    for index, raw in enumerate(features):
        feature_path = f"{path}.features[{index}]"
        feature = _dict_or_raise(raw, feature_path)
        allowed = frozenset({"name", "source", "path", "description"})
        _check_missing(feature, allowed, feature_path)
        _check_unknown(feature, allowed, feature_path)
        name = feature["name"]
        source = feature["source"]
        pointer = feature["path"]
        description = feature["description"]
        if not isinstance(name, str) or IDENTIFIER.fullmatch(name) is None:
            raise ManifestError("bad_value", _path(feature_path, "name"))
        if not isinstance(source, str):
            raise ManifestError("bad_type", _path(feature_path, "source"))
        sources = {"case", "public", "observation", "feedback", "diagnostics"}
        if source not in sources:
            raise ManifestError("bad_value", _path(feature_path, "source"))
        if not isinstance(pointer, str):
            raise ManifestError("bad_type", _path(feature_path, "path"))
        if not isinstance(description, str):
            raise ManifestError("bad_type", _path(feature_path, "description"))
        try:
            parsed_feature = FieldFeature(name, source, pointer, description)  # type: ignore[arg-type]
        except ValueError as exc:
            failed = "description" if len(description) > 512 else "path"
            raise ManifestError("bad_value", _path(feature_path, failed)) from exc
        if name in names:
            raise ManifestError("bad_value", _path(feature_path, "name"))
        names.add(name)
        parsed.append(parsed_feature)
    return FieldsGroupingSection(tuple(parsed))


def _parse_role_call_retry(value: object, path: str) -> RoleCallRetry:
    """The shared `role_call_retry/1` provider-call retry policy (
    `/11` PDR-0019, ADR-0023 §the chat Reflector/Editor `retry` key), used for
    `reflector.grouping.kind: model`'s `retry` and, from `/11`, a chat
    Reflector's/Editor's own `retry`. `RoleCallRetry.from_payload` raises
    `ValueError(path)` with `path` already spelled from `"retry"` (e.g.
    `"retry.max_attempts"`); every case but two maps straight to `bad_value`
    at that path under the caller's own section `path`. `"retry.unknown"`/
    `"retry.missing"` name no key by themselves, so the offending key is
    found here, cheaply, against `_ROLE_CALL_RETRY_KEYS` -- falling back to
    the same generic `bad_value` if that ever fails to find one (it cannot,
    given how `from_payload` itself decides those two cases)."""
    try:
        return RoleCallRetry.from_payload(value)
    except ValueError as exc:
        suffix = str(exc)
        if suffix == "retry.unknown" and isinstance(value, Mapping):
            offending = sorted(set(value) - _ROLE_CALL_RETRY_KEYS)
            if offending:
                raise ManifestError("unknown_key", _path(path, f"retry.{offending[0]}")) from exc
        if suffix == "retry.missing" and isinstance(value, Mapping):
            missing = sorted(_ROLE_CALL_RETRY_KEYS - set(value))
            if missing:
                raise ManifestError("missing_key", _path(path, f"retry.{missing[0]}")) from exc
        raise ManifestError("bad_value", _path(path, suffix)) from exc


def _parse_grouping_fallback(obj: dict[str, Any], path: str) -> FieldsGroupingSection | None:
    """A grouping model's optional `fallback` (R8: shared by `chat` and `agent_cli`):
    absent or `null` (singleton groups), or a closed `fields` grouping."""

    fallback_raw = obj.get("fallback")
    if fallback_raw is None:
        return None
    fallback_path = _path(path, "fallback")
    fallback_obj = _dict_or_raise(fallback_raw, fallback_path)
    fallback_kind = fallback_obj.get("kind")
    if not isinstance(fallback_kind, str):
        raise ManifestError("bad_type", _path(fallback_path, "kind"))
    if fallback_kind != "fields":
        raise ManifestError("bad_value", _path(fallback_path, "kind"))
    return _parse_fields_grouping(fallback_obj, fallback_path)


def _parse_model_grouping(
    obj: dict[str, Any], path: str, manifest_dir: Path, seed_root: Path, *, schema: str
) -> ModelGroupingSection:
    """The chat Grouper's model role, independent of the Reflector and Editor."""
    _check_missing(obj, _GROUPING_MODEL_REQUIRED, path)
    optional = _GROUPING_MODEL_OPTIONAL | (
        {
            "max_repairs",
            "provider_order",
            "allow_fallbacks",
            "require_parameters",
            *GROUPING_V2_MINIMUMS,
        }
        if schema == SCHEMA_V16
        else set()
    )
    _check_unknown(obj, _GROUPING_MODEL_REQUIRED | optional, path)

    backend = obj["backend"]
    if not isinstance(backend, str):
        raise ManifestError("bad_type", _path(path, "backend"))
    if backend != "openai_chat":
        raise ManifestError("bad_value", _path(path, "backend"))

    model = _str_or_raise(obj["model"], _path(path, "model"))
    if not model.strip() or len(model) > 256:
        raise ManifestError("bad_value", _path(path, "model"))

    base_url = _chat_base_url(obj["base_url"], _path(path, "base_url"))
    api_key_env = _optional_env_name(obj, "api_key_env", path)

    timeout_seconds = _ranged_int(obj["timeout_seconds"], _path(path, "timeout_seconds"), 10, 7200)
    concurrency = _ranged_int(obj["concurrency"], _path(path, "concurrency"), 1, 16)
    max_input_chars = _ranged_int(
        obj["max_input_chars"], _path(path, "max_input_chars"), 8000, 2_000_000
    )
    max_output_tokens = _ranged_int(
        obj["max_output_tokens"], _path(path, "max_output_tokens"), 256, 65_536
    )

    retry = _parse_role_call_retry(obj["retry"], path)

    proxy_env_raw = obj.get("proxy_env")
    proxy_env = (
        None if proxy_env_raw is None else _env_name(proxy_env_raw, _path(path, "proxy_env"))
    )

    verify_ssl_certs = (
        _bool_or_raise(obj["verify_ssl_certs"], _path(path, "verify_ssl_certs"))
        if "verify_ssl_certs" in obj
        else True
    )

    reasoning_effort = _reasoning_effort(
        obj.get("reasoning_effort"), _path(path, "reasoning_effort")
    )

    provider_only = _optional_nonempty_str_list(
        obj.get("provider_only"), _path(path, "provider_only")
    )
    provider_order, allow_fallbacks = _provider_routing(obj, path, provider_only)
    require_parameters = _require_parameters(obj, path, provider_only, provider_order)

    system_prompt: Path | None = None
    if obj.get("system_prompt") is not None:
        system_prompt = _existing_program_file(
            obj["system_prompt"],
            _path(path, "system_prompt"),
            manifest_dir,
            require_executable=False,
        )
        _refuse_program_inside_seed(system_prompt, _path(path, "system_prompt"), seed_root)

    representatives_per_case = (
        _ranged_int(obj["representatives_per_case"], _path(path, "representatives_per_case"), 1, 8)
        if "representatives_per_case" in obj
        else 3
    )

    fallback = _parse_grouping_fallback(obj, path)

    def v2_count(key: str) -> int | None:
        # Absent unless declared: /15 refused each key as unknown above.
        return _optional_count(obj, key, path, GROUPING_V2_MINIMUMS[key])

    return ModelGroupingSection(
        backend=cast(Literal["openai_chat"], backend),
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        max_input_chars=max_input_chars,
        max_output_tokens=max_output_tokens,
        retry=retry,
        proxy_env=proxy_env,
        verify_ssl_certs=verify_ssl_certs,
        reasoning_effort=reasoning_effort,
        provider_only=provider_only,
        provider_order=provider_order,
        allow_fallbacks=allow_fallbacks,
        require_parameters=require_parameters,
        system_prompt=system_prompt,
        representatives_per_case=representatives_per_case,
        fallback=fallback,
        max_repairs=_optional_count(obj, "max_repairs", path, 0),
        group_output_tokens=v2_count("group_output_tokens"),
        glimpse_chars=v2_count("glimpse_chars"),
        max_set_members=v2_count("max_set_members"),
        read_chars=v2_count("read_chars"),
        max_turns=v2_count("max_turns"),
        card_examples=v2_count("card_examples"),
        max_reduce_levels=v2_count("max_reduce_levels"),
        max_requests=v2_count("max_requests"),
    )


def _parse_command_grouping(
    obj: dict[str, Any], path: str, manifest_dir: Path, seed_root: Path
) -> CommandGroupingSection:
    """A `command` grouping (R8: shared by `reflector.grouping` and `grouper`):
    the command keys plus `timeout_seconds` and `config`, sealed as a command."""

    _check_missing(obj, _GROUPING_COMMAND_REQUIRED, path)
    _check_unknown(obj, _GROUPING_COMMAND_REQUIRED, path)
    command_value = dict(obj)
    command_value.pop("config")
    command_value["kind"] = "subprocess"
    command = _parse_command(
        command_value, path, manifest_dir, seed_root, with_timeout=True, require_kind=True
    )
    return CommandGroupingSection(
        command, _parse_reflection_config(obj["config"], _path(path, "config"))
    )


def _parse_selection(value: object, path: str, *, schema: str) -> SelectionSection:
    obj = _dict_or_raise(value, path)
    _check_unknown(
        obj, _SELECTION_ALLOWED | ({"min_claim_cases"} if schema == SCHEMA_V16 else set()), path
    )
    values = {
        "focus_cases": obj.get("focus_cases", 16),
        "representatives_per_case": obj.get("representatives_per_case", 2),
        "context_page_size": obj.get("context_page_size", 32),
    }
    limits = {"focus_cases": 64, "representatives_per_case": 8, "context_page_size": 64}
    for name, item in values.items():
        if type(item) is not int:
            raise ManifestError("bad_type", _path(path, name))
        if not 1 <= item <= limits[name]:
            raise ManifestError("bad_value", _path(path, name))
    minimum = obj.get("min_claim_cases")
    if "min_claim_cases" in obj:
        if type(minimum) is not int:
            raise ManifestError("bad_type", _path(path, "min_claim_cases"))
        if minimum < 1:
            raise ManifestError("bad_value", _path(path, "min_claim_cases"))
    return SelectionSection(**values, min_claim_cases=minimum)


def _parse_families(value: object, path: str) -> FamilySection:
    obj = _dict_or_raise(value, path)
    kind = obj.get("kind")
    if not isinstance(kind, str):
        raise ManifestError("bad_type", _path(path, "kind"))
    if kind == "none":
        _check_missing(obj, _FAMILIES_NONE, path)
        _check_unknown(obj, _FAMILIES_NONE, path)
        return FamilySection("none")
    if kind != "metadata":
        raise ManifestError("bad_value", _path(path, "kind"))
    _check_missing(obj, _FAMILIES_METADATA, path)
    _check_unknown(obj, _FAMILIES_METADATA, path)
    pointer = obj["path"]
    if not isinstance(pointer, str):
        raise ManifestError("bad_type", _path(path, "path"))
    try:
        FieldFeature("family", "case", pointer, "")
    except ValueError as exc:
        raise ManifestError("bad_value", _path(path, "path")) from exc
    return FamilySection("metadata", pointer)


def _parse_reflector(
    value: object,
    prefix: str,
    manifest_dir: Path,
    seed_root: Path,
    *,
    schema: str,
    target_base_url: str,
) -> ReflectorSection:
    """Dispatch `reflector` by `kind`, mirroring `_parse_editor`:
    `kind` missing is `missing_key at reflector`; a non-string `kind` is
    `bad_type at reflector.kind`; a string outside `REFLECTOR_KINDS` is
    `bad_value at reflector.kind`. Once `kind` is well-formed, the two keys
    every kind shares (`environment_docs`, `probe`) must both be present too,
    collapsed to the same bare `missing_key at reflector` -- which keys the
    role itself requires is not yet known until `kind` is. The two shared keys
    are then split off (`common`) from the role's own keys (`role_obj`, which
    keeps `kind`) before dispatching to the same per-kind parsers `_parse_editor`
    uses, with the reflector's own optional sets for `chat`/`agent_cli`.
    """
    obj = _dict_or_raise(value, prefix)
    if "kind" not in obj:
        raise ManifestError("missing_key", prefix)
    kind = obj["kind"]
    if not isinstance(kind, str):
        raise ManifestError("bad_type", _path(prefix, "kind"))
    if kind not in _REFLECTOR_KINDS:
        raise ManifestError("bad_value", _path(prefix, "kind"))
    # R9: `/15` moves grouping to the top-level `grouper`; its Reflector
    # requires only `selection` and `families` beside the shared keys, and
    # `reflector.grouping` is refused here, before the role parser, which would
    # otherwise refuse it with no text (or, for `chat`, as the collapsed
    # `missing_key at reflector`).
    adaptive_keys = frozenset({"selection", "families"})
    required = _REFLECTOR_COMMON | adaptive_keys
    if not required <= obj.keys():
        raise ManifestError("missing_key", prefix)
    if "grouping" in obj:
        raise ManifestError(
            "unknown_key", _path(prefix, "grouping"), expected=REFLECTOR_GROUPING_V15
        )

    # `/11` (PDR-0019, ADR-0023): `max_consecutive_failures` is required for
    # every `reflector.kind`, checked at its own path (not collapsed into the
    # bare `missing_key at reflector` above, unlike the shared/adaptation
    # keys, because unlike them it is not part of the role's own required
    # surface -- it stands beside `kind`, one key on its own).
    mcf_path = _path(prefix, "max_consecutive_failures")
    if "max_consecutive_failures" not in obj:
        raise ManifestError("missing_key", mcf_path)
    mcf_value = obj["max_consecutive_failures"]
    if isinstance(mcf_value, bool) or not isinstance(mcf_value, int) or not 1 <= mcf_value <= 20:
        raise ManifestError("bad_value", mcf_path)
    max_consecutive_failures = mcf_value

    # an optional `group_text_chars` beside `max_consecutive_failures`;
    # `null` is a value, so presence is kept apart.
    group_text_chars: int | None = None
    group_text_chars_declared = "group_text_chars" in obj
    if group_text_chars_declared:
        limit = obj["group_text_chars"]
        if limit is not None:
            try:
                group_text_chars = _positive_count(limit, _path(prefix, "group_text_chars"))
            except ManifestError as exc:
                raise ManifestError(
                    exc.code, exc.detail, expected=REFLECTOR_GROUP_TEXT_CHARS
                ) from exc

    common = {key: obj[key] for key in _REFLECTOR_COMMON}
    excluded = _REFLECTOR_COMMON | adaptive_keys | {"max_consecutive_failures", "group_text_chars"}
    if schema == SCHEMA_V16:
        excluded |= {"max_consecutive_skips"}
    role_obj = {key: val for key, val in obj.items() if key not in excluded}

    role: CommandSection | ChatEditorSection | AgentCliEditorSection
    if kind == "subprocess":
        role = _parse_command(
            role_obj, prefix, manifest_dir, seed_root, with_timeout=False, require_kind=True
        )
    elif kind == "chat":
        role = _parse_chat_editor(
            role_obj,
            prefix,
            manifest_dir,
            seed_root,
            optional=_CHAT_REFLECTOR_OPTIONAL,
            schema=schema,
        )
    else:
        role = _parse_agent_cli_editor(
            role_obj,
            prefix,
            manifest_dir,
            seed_root,
            optional=_AGENT_REFLECTOR_OPTIONAL,
            schema=schema,
        )

    environment_docs = _parse_environment_docs(
        common["environment_docs"], _path(prefix, "environment_docs"), manifest_dir, seed_root
    )
    probe = _parse_probe(
        common["probe"], _path(prefix, "probe"), target_base_url=target_base_url, schema=schema
    )

    return ReflectorSection(
        role=role,
        environment_docs=environment_docs,
        probe=probe,
        selection=_parse_selection(obj["selection"], _path(prefix, "selection"), schema=schema),
        families=_parse_families(obj["families"], _path(prefix, "families")),
        max_consecutive_failures=max_consecutive_failures,
        group_text_chars=group_text_chars,
        group_text_chars_declared=group_text_chars_declared,
        max_consecutive_skips=_optional_count(obj, "max_consecutive_skips", prefix, 1),
    )


def _parse_agent_cli_grouper(
    obj: dict[str, Any], path: str, manifest_dir: Path, seed_root: Path, *, schema: str
) -> AgentCliGrouperSection:
    """`grouper.kind: agent_cli`. `timeout_seconds` is checked
    for presence first, at its own path; the agent keys are parsed as the agent
    Reflector's (`_AGENT_REFLECTOR_OPTIONAL`, the `hermes` + `max_turns`
    refusal included); then the timeout's range, then the fallback."""

    timeout_path = _path(path, "timeout_seconds")
    if "timeout_seconds" not in obj:
        raise ManifestError("missing_key", timeout_path)
    role_obj = {key: val for key, val in obj.items() if key not in ("timeout_seconds", "fallback")}
    role = _parse_agent_cli_editor(
        role_obj,
        path,
        manifest_dir,
        seed_root,
        optional=_AGENT_REFLECTOR_OPTIONAL,
        schema=schema,
    )
    timeout_seconds = _ranged_int(obj["timeout_seconds"], timeout_path, *_AGENT_GROUPER_TIMEOUT)
    fallback = _parse_grouping_fallback(obj, path)
    return AgentCliGrouperSection(role=role, timeout_seconds=timeout_seconds, fallback=fallback)


def _parse_grouper_section(
    value: object, path: str, manifest_dir: Path, seed_root: Path, *, schema: str
) -> GrouperSection:
    obj = _dict_or_raise(value, path)
    if "kind" not in obj:
        raise ManifestError("missing_key", path)
    kind = obj["kind"]
    if not isinstance(kind, str):
        raise ManifestError("bad_type", _path(path, "kind"))
    if kind == "model":
        raise ManifestError("bad_value", _path(path, "kind"), expected=GROUPER_MODEL_KIND)
    if kind not in _GROUPER_KINDS:
        raise ManifestError("bad_value", _path(path, "kind"))
    if kind == "fields":
        return _parse_fields_grouping(obj, path)
    if kind == "command":
        return _parse_command_grouping(obj, path, manifest_dir, seed_root)
    if kind == "chat":
        return _parse_model_grouping(obj, path, manifest_dir, seed_root, schema=schema)
    return _parse_agent_cli_grouper(obj, path, manifest_dir, seed_root, schema=schema)


def _parse_grouper(
    value: object, manifest_dir: Path, seed_root: Path, *, schema: str
) -> GrouperSection:
    """`/15`'s required top-level `grouper`: the four kinds reuse the
    existing parsers. Every manifest-code refusal inside it carries a fixed
    sentence (`grouper_expected`); none repeats a value."""

    kind = value.get("kind") if isinstance(value, dict) else None
    try:
        # The chain limit belongs to every top-level Grouper, never its fallback.
        section_value = value
        limit = None
        if schema == SCHEMA_V16 and isinstance(value, dict):
            limit = _optional_count(value, "max_consecutive_full_fallbacks", "grouper", 1)
            section_value = {
                key: item for key, item in value.items() if key != "max_consecutive_full_fallbacks"
            }
        section = _parse_grouper_section(
            section_value, "grouper", manifest_dir, seed_root, schema=schema
        )
        return replace(section, max_consecutive_full_fallbacks=limit)
    except ManifestError as exc:
        if exc.expected is not None:
            raise
        expected = grouper_expected(exc.code, exc.detail, kind)
        if expected is None:
            raise
        raise ManifestError(exc.code, exc.detail, expected=expected) from exc


def _parse_corpus(value: object, prefix: str, manifest_dir: Path) -> Path:
    obj = _dict_or_raise(value, prefix)
    _check_missing(obj, _CORPUS_REQUIRED, prefix)
    _check_unknown(obj, _CORPUS_REQUIRED, prefix)
    raw = _str_or_raise(obj["path"], _path(prefix, "path"))
    return (manifest_dir / raw).resolve()


def _declared_spellings(obj: dict[str, Any], *, linked_from: str | None) -> dict[str, str | None]:
    """The source spelling of every path field the loader parsed, keyed by the
    dotted field `ManifestError` would name.

    Runs after the native campaign parsers over the validated document, so it
    records rather than checks: a string is kept verbatim, a declared `null`
    is kept as `None`. The workflow-less builder records only `seed_root`.

    `linked_from` is the already-parsed run-directory string (or `None`),
    never `obj["linked_from"]` directly: the recorded spelling is the run-dir
    string, not the link object by contract.

    This dotted-key vocabulary is also written by `_Walk.spelling` in
    `manifest_inventory.py` and must stay identical to it; a mismatch
    degrades silently into `declared=None` plus `declared_spellings_unavailable`.
    """

    declared: dict[str, str | None] = {"seed_root": obj["seed_root"]}
    if linked_from is not None:
        declared["linked_from"] = linked_from

    def command(section: dict[str, Any], prefix: str) -> None:
        declared[f"{prefix}.cwd"] = section["cwd"]
        for index, dep in enumerate(section["dependency_paths"]):
            declared[f"{prefix}.dependency_paths[{index}]"] = dep

    def role(section: dict[str, Any], prefix: str) -> None:
        kind = section["kind"]
        if kind == "subprocess":
            command(section, prefix)
            return
        declared[f"{prefix}.system_prompt"] = section.get("system_prompt")
        if kind == "agent_cli":
            declared[f"{prefix}.agent_bin"] = section.get("agent_bin")

    command(obj["scorer"], "scorer")
    role(obj["editor"], "editor")
    reflector = obj["reflector"]
    role(reflector, "reflector")
    declared["reflector.environment_docs"] = reflector["environment_docs"]
    # R11: `/15`'s Grouper. `_Walk` in `manifest_inventory` writes the same keys.
    grouper = obj.get("grouper")
    if isinstance(grouper, dict):
        kind = grouper.get("kind")
        if kind == "command":
            command(grouper, "grouper")
        elif kind == "chat":
            declared["grouper.system_prompt"] = grouper.get("system_prompt")
        elif kind == "agent_cli":
            declared["grouper.system_prompt"] = grouper.get("system_prompt")
            declared["grouper.agent_bin"] = grouper.get("agent_bin")
    declared["corpus.path"] = obj["corpus"]["path"]
    return declared


def _reject_constant(name: str) -> float:
    """`json.loads` accepts `NaN`, `Infinity` and `-Infinity` by default; they
    are Python's extension to JSON, not JSON, and PDR-0003 binds the preview to
    a strict manifest. The document is refused as a whole (`bad_type` at the
    root), because the token is a decoding failure and not a field's value."""
    raise ValueError(f"non-standard JSON constant: {name}")


def _resolve_seed_root(manifest_dir: Path, seed_root: str) -> Path:
    candidate = Path(seed_root)
    resolved = candidate if candidate.is_absolute() else manifest_dir / candidate
    try:
        info = resolved.lstat()
    except OSError as exc:
        raise ManifestError("bad_path", "seed_root") from exc
    if stat.S_ISLNK(info.st_mode) or not resolved.is_dir():
        raise ManifestError("bad_path", "seed_root")
    return resolved.resolve()


def load_manifest_v2(path: Path) -> ManifestV2:
    """Load a native `/15` or `/16` campaign document, including its workflow."""

    return _load_manifest(path)


def conformance_manifest(document: Mapping[str, object], *, base: Path) -> ManifestV2:
    """Build workflow-less service input from exactly seed_root, target, artifacts.

    Uses the historical three-field validation and the native loader's shared
    parsers, including their ManifestError codes, key paths and precedence.
    Unknown keys (schema included) are refused. Relative seed paths resolve
    against base.resolve(); artifact paths remain seed-relative policy strings.

    The result's schema='evolution-v2-manifest/1' is an in-memory label only:
    load_manifest_v2 and admit_manifest still refuse /1. Its path is the
    synthetic base.resolve() / 'manifest.json', never read, created or checked
    by this function; path.parent supplies the dependency inventory's base.
    The workflow=None result supports run_conformance, ArtifactStore.create
    and HttpSessionAdapter through its parsed seed, policy and target.
    """
    if not isinstance(document, Mapping):
        raise ManifestError("bad_type", _ROOT)
    obj = dict(document)
    _validate_structure(obj, _CONFORMANCE_KEYS)
    target = _parse_target(obj["target"], "target", schema=SCHEMA_V15)
    policy = _parse_artifacts(obj["artifacts"], "artifacts")
    seed_root_value = _str_or_raise(obj["seed_root"], "seed_root")
    manifest_dir = base.resolve()
    seed_root = _resolve_seed_root(manifest_dir, seed_root_value)
    return ManifestV2(
        schema="evolution-v2-manifest/1",
        path=manifest_dir / "manifest.json",
        seed_root=seed_root,
        target=target,
        policy=policy,
        declared={"seed_root": seed_root_value},
    )


def _load_manifest(path: Path) -> ManifestV2:
    try:
        resolved_dir = path.resolve().parent
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # The document's own file, named as a key path like every other detail:
        # the resolved path and the OS message stay on `__cause__`, out of every
        # string a caller prints.
        raise ManifestError("bad_path", _DOCUMENT) from exc

    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:  # `JSONDecodeError` and `_reject_constant` alike
        raise ManifestError("bad_type", _ROOT) from exc
    if not isinstance(obj, dict):
        raise ManifestError("bad_type", _ROOT)

    schema = obj.get("schema")
    if schema not in (SCHEMA_V15, SCHEMA_V16):
        raise ManifestError(
            "manifest_schema", "schema", expected="expected evolution-v2-manifest/15 or /16"
        )
    top_level_keys = _TOP_LEVEL_KEYS_V15
    optional_top_level_keys = (
        _OPTIONAL_TOP_LEVEL_KEYS_V16 if schema == SCHEMA_V16 else _OPTIONAL_TOP_LEVEL_KEYS_V15
    )

    try:
        _validate_structure(obj, top_level_keys, optional_top_level_keys, schema=schema)
    except ManifestError as exc:
        # `/15`'s two new required top-level keys say what
        # they expect; every other structural refusal keeps its plain form.
        if exc.code == "missing_key" and exc.detail in TOP_LEVEL_V15_EXPECTED:
            raise ManifestError(
                exc.code, exc.detail, expected=TOP_LEVEL_V15_EXPECTED[exc.detail]
            ) from exc
        raise

    # One real base for seed, programs and corpus, even through a manifest link.
    manifest_dir = resolved_dir

    target = _parse_target(obj["target"], "target", schema=schema)
    policy = _parse_artifacts(obj["artifacts"], "artifacts")

    seed_root_value = _str_or_raise(obj["seed_root"], "seed_root")
    seed_root = _resolve_seed_root(manifest_dir, seed_root_value)

    # Keep native validation order: campaign, environment, scorer, editor,
    # reflector, Grouper, visibility, optional answers, corpus, then link.
    campaign = (
        _parse_campaign_v12(obj["campaign"], "campaign")
        if schema == SCHEMA_V16
        else _parse_campaign_v11(obj["campaign"], "campaign")
    )
    environment = _parse_environment(obj["environment"], "environment")
    scorer, metric_contract = _parse_scorer_v15(obj["scorer"], manifest_dir, seed_root)
    editor = _parse_editor(obj["editor"], "editor", manifest_dir, seed_root, schema=schema)
    reflector = _parse_reflector(
        obj["reflector"],
        "reflector",
        manifest_dir,
        seed_root,
        target_base_url=target.base_url,
        schema=schema,
    )
    grouper = _parse_grouper(obj["grouper"], manifest_dir, seed_root, schema=schema)
    role_visibility = _parse_role_visibility(obj["role_visibility"])
    verbatim_answers = (
        _parse_verbatim_answers(obj["verbatim_answers"]) if "verbatim_answers" in obj else None
    )
    secret_oracle_paths = (
        _parse_secret_oracle_paths(obj["secret_oracle_paths"])
        if "secret_oracle_paths" in obj
        else None
    )
    checks = _parse_checks(obj["checks"]) if "checks" in obj else None
    evidence = _parse_evidence(obj["evidence"]) if "evidence" in obj else None
    corpus_path = _parse_corpus(obj["corpus"], "corpus", manifest_dir)
    workflow = WorkflowSections(
        campaign=campaign,
        environment=environment,
        scorer=scorer,
        editor=editor,
        reflector=reflector,
        corpus_path=corpus_path,
        metric_contract=metric_contract,
        role_visibility=role_visibility,
        grouper=grouper,
        verbatim_answers=verbatim_answers,
        secret_oracle_paths=secret_oracle_paths,
        evidence=evidence,
        checks=checks,
    )
    linked_from, linked_baseline = _parse_linked_from_v6(obj)

    return ManifestV2(
        schema=schema,
        path=path.resolve(),
        seed_root=seed_root,
        target=target,
        policy=policy,
        workflow=workflow,
        declared=_declared_spellings(obj, linked_from=linked_from),
        linked_from=linked_from,
        linked_baseline=linked_baseline,
    )
