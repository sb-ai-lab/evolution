"""Fixed expected sentences for manifest `/15` refusals outside `campaign`.

A
`/15` refusal of a new declaration names its key path and a fixed sentence
that states the accepted domain; no sentence repeats a value. The campaign's
own `/15` texts live in `campaign_policy_help`. Every text here is a
constant.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: The one fixed sentence for every `verbatim_answers` shape refusal (ADR-0038).
VERBATIM_ANSWERS_EXPECTED: Final = (
    'expected an object {"version": "verbatim_answers/1", "strings": [...]} with 1 to 64 '
    "distinct strings of 1 to 4096 UTF-8 bytes each; omit the key to declare none"
)

SECRET_ORACLE_PATHS_EXPECTED: Final = (
    'expected an object {"version": "secret_oracle_paths/1", "pointers": [...]} with 0 to 64 '
    "distinct RFC 6901 JSON Pointers of at most 1024 UTF-8 bytes each; "
    "an empty list disables oracle-string checks, run-secret checks stay on"
)

REQUIRE_PARAMETERS_EXPECTED: Final = (
    "require_parameters applies only with provider_only or provider_order"
)

_INDEX = re.compile(r"\[\d+\]")

#: `missing_key`, `bad_type` and `bad_value` at a connection path, keyed by the
#: full dotted path with every list index written `[]`.
CONNECTION_EXPECTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "scorer.metric_contract": (
            "expected an object with version 'metric_contract/1', kind 'pointwise' and "
            "target_fields, at most 64 KiB as canonical JSON"
        ),
        "scorer.metric_contract.version": "expected exactly 'metric_contract/1'",
        "scorer.metric_contract.kind": (
            "expected exactly 'pointwise'; collection metrics need a later manifest schema"
        ),
        "scorer.metric_contract.target_fields": (
            "expected a list of at most 64 target fields; an empty list is valid"
        ),
        "scorer.metric_contract.target_fields[]": (
            "expected an object with event, pointer and value whose event and pointer pair "
            "no earlier entry has"
        ),
        "scorer.metric_contract.target_fields[].event": (
            "expected a non-blank event type of 1 to 128 UTF-8 bytes"
        ),
        "scorer.metric_contract.target_fields[].pointer": (
            "expected an RFC 6901 JSON Pointer of at most 1024 UTF-8 bytes, such as /content"
        ),
        "scorer.metric_contract.target_fields[].value": (
            "expected exactly 'nonempty_string', 'string', 'finite_number', 'boolean', "
            "'object', 'array' or 'finite_json'"
        ),
        "role_visibility": (
            "expected an object with version 'role_visibility/1' and readable_paths; "
            "an empty list is valid"
        ),
        "role_visibility.version": "expected exactly 'role_visibility/1'",
        "role_visibility.readable_paths": (
            "expected a list of artifact path patterns; an empty list is valid"
        ),
        "role_visibility.readable_paths[]": (
            "expected a relative artifact path pattern such as src/**/*.py, listed once, not "
            "starting with '/' or '!', and with no empty, '.' or '..' segment"
        ),
    }
)

#: `unknown_key` inside a connection object, keyed by the object's normalized path.
CONNECTION_UNKNOWN: Final[Mapping[str, str]] = MappingProxyType(
    {
        "scorer.metric_contract": "expected only version, kind and target_fields",
        "scorer.metric_contract.target_fields[]": "expected only event, pointer and value",
        "role_visibility": "expected only version and readable_paths",
    }
)


def _normal(path: str) -> str:
    return _INDEX.sub("[]", path)


def connection_expected(code: str, detail: str, *, section: str | None = None) -> str:
    """The sentence for a `metric_contract` or `role_visibility` refusal (R7).

    `detail` is the full dotted path the refusal names; `section`, for an
    `unknown_key`, the full path of the object that holds the key. A path no
    table knows is a bug in the parser, so it raises `KeyError`.
    """

    if code == "unknown_key" and section is not None:
        return CONNECTION_UNKNOWN[_normal(section)]
    return CONNECTION_EXPECTED[_normal(detail)]


#: `missing_key at grouper`, and the Grouper section's own sentence.
GROUPER_SECTION: Final = (
    "expected an object with kind 'fields', 'command', 'chat' or 'agent_cli' and that "
    "kind's keys; /15 groups failing observations here, not under reflector.grouping"
)

#: `missing_key` at a `/15` top-level key.
TOP_LEVEL_V15_EXPECTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "grouper": GROUPER_SECTION,
        "role_visibility": CONNECTION_EXPECTED["role_visibility"],
    }
)

#: R9: `reflector.grouping` present in a `/15` document.
REFLECTOR_GROUPING_V15: Final = (
    "expected no grouping under reflector in /15; declare it in the top-level grouper section"
)

#: `bad_type` or `bad_value` at `reflector.group_text_chars` (`/15` and `/16`).
REFLECTOR_GROUP_TEXT_CHARS: Final = (
    "expected an integer of at least 1, the most characters each Reflector group text field "
    "may hold, or null for no per-field limit; omit the key for 512 in /15 and 4096 in /16"
)


GROUPER_KINDS_TEXT: Final = "'fields', 'command', 'chat' or 'agent_cli'"
GROUPER_KIND: Final = f"expected exactly {GROUPER_KINDS_TEXT}"
#: `kind: model` (R8): the grouping model's `/15` name is `chat`.
GROUPER_MODEL_KIND: Final = (
    "expected 'chat' for a grouping model: /15 names the kind 'chat' (it was 'model' under "
    "reflector.grouping)"
)
GROUPER_TIMEOUT: Final = (
    "expected an integer from 10 to 7200: the seconds the whole grouping operation may take"
)
GROUPER_COMMAND_TIMEOUT: Final = (
    "expected a positive number: the seconds the grouping command may run"
)
GROUPER_FALLBACK: Final = (
    "expected null (each failing case its own group) or an object with kind 'fields' and features"
)
#: Every other refusal inside `grouper`, by the section's kind.
GROUPER_BY_KIND: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fields": (
            "expected grouper.kind 'fields' with only features: 1 to 16 objects with name, "
            "source, path and description"
        ),
        "command": (
            "expected grouper.kind 'command' with only argv, cwd, dependency_paths, "
            "allowed_env, timeout_seconds and config, as a grouping command declares them"
        ),
        "chat": (
            "expected grouper.kind 'chat' with backend 'openai_chat', model, base_url, "
            "timeout_seconds (10 to 7200), concurrency (1 to 16), max_input_chars "
            "(8000 to 2000000), max_output_tokens (256 to 65536) and retry "
            "(role_call_retry/1), and optionally api_key_env, proxy_env, verify_ssl_certs, reasoning_effort, "
            "provider_only, system_prompt, representatives_per_case (1 to 8) and fallback"
        ),
        "agent_cli": (
            "expected grouper.kind 'agent_cli' with an agent CLI model and timeout_seconds "
            "(10 to 7200), and optionally permission, agent_bin, allowed_env, system_prompt, "
            "reasoning_effort, max_turns (not with a hermes model), max_payload_bytes and "
            "fallback"
        ),
    }
)
_MANIFEST_CODES = frozenset({"missing_key", "unknown_key", "bad_type", "bad_value"})


def grouper_expected(code: str, detail: str, kind: object) -> str | None:
    """The sentence for a refusal inside `grouper`.

    Only the four manifest codes get one; a path code (`bad_path`,
    `program_inside_seed`) keeps its plain form, as for every role. `kind` is
    the section's `kind` as written, which picks the per-kind sentence.
    `timeout_seconds` is a positive number for `command` and an integer from 10
    to 7200 for `chat` and `agent_cli` (H1a review F1).
    """

    if code not in _MANIFEST_CODES:
        return None
    if detail == "grouper":
        return GROUPER_SECTION
    if detail == "grouper.kind":
        return GROUPER_KIND
    if detail.startswith("grouper.fallback."):
        return GROUPER_FALLBACK  # every code inside the fallback object, unknown_key included
    if code != "unknown_key":
        if detail == "grouper.timeout_seconds":
            return GROUPER_COMMAND_TIMEOUT if kind == "command" else GROUPER_TIMEOUT
        if detail == "grouper.fallback":
            return GROUPER_FALLBACK
    if isinstance(kind, str) and kind in GROUPER_BY_KIND:
        return GROUPER_BY_KIND[kind]
    return GROUPER_SECTION


#: `missing_key` at a `/15` chat role's compaction key. Both keys
#: count characters; the recommended values assume an input window of at
#: least about 400 000 tokens.
COMPACTION_EXPECTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "context_compact_chars": (
            "expected a positive integer number of characters at which the conversation is "
            "folded; 800000 is recommended for a model whose input window holds at least about "
            "400000 tokens"
        ),
        "context_message_max_chars": (
            "expected a positive integer number of characters, at most half of "
            "context_compact_chars; 64000 is recommended"
        ),
    }
)


EVIDENCE_EXPECTED: Final = (
    'expected an object {"version": "evidence/1", ...} with view "full" or "allowlist" '
    "and integer limits at or above their minimums; omit the key for the defaults"
)


CHECKS_EXPECTED: Final = (
    'expected an object {"version": "checks/1", "modes": {...}} with known check names '
    'and modes "enforce", "shadow" or "off"; unlisted checks enforce'
)
