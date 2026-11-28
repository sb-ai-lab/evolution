"""Offline input resolution for the family-15 chat context report."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evolution.tree import ManifestError, load_manifest_v2
from evolution_lab.owner import core_raw

from .models import PreparationError
from .storage import PreparationStorage


@dataclass
class PreflightInputs:
    prepared_digest: str
    bundle_digest: str
    manifest: Any = field(repr=False)
    stages: list[dict] = field(default_factory=list)
    routes: dict[str, dict] = field(default_factory=dict)
    turns: list[dict] = field(default_factory=list)
    grouping_saved: bool = False
    producing_version: str | None = None
    baseline_version: str | None = None
    operation_id: str | None = None
    baseline_operation_id: str | None = None


def _stage(result, stage, unit, setting, value, source, *, before=None, after=None, status="configured"):
    result.stages.append(dict(stage=stage, unit=unit, before=before, after=after,
                              lost=before - after if before is not None and after is not None else None,
                              setting=setting, value=value, source=source, status=status))


def _settings(result, document, launch):
    workflow = result.manifest.workflow
    evidence, grouping_limits = core_raw.preflight_limits(workflow)
    _stage(result, "target_output", "bytes", "limits.max_output_bytes", launch["limits"]["max_output_bytes"],
           "bundle_launch")
    _stage(result, "target_output", "bytes", "target.max_response_bytes", result.manifest.target.max_response_bytes,
           "bundle_manifest" if "max_response_bytes" in document["target"] else "core_default")
    for key, unit in (("turns", "turns"), ("events_per_turn", "events"), ("input_chars", "characters"),
                      ("text_chars", "characters"), ("trace_page_chars", "characters"),
                      ("cited_chars", "characters"), ("feedback_keys", "keys"),
                      ("feedback_value_chars", "characters")):
        _stage(result, "evidence", unit, f"evidence.{key}", evidence[key],
               "bundle_manifest" if key in document.get("evidence", {}) else "core_default")
    selection = workflow.reflector.selection
    if selection is not None:
        for key in ("focus_cases", "representatives_per_case", "context_page_size"):
            _stage(result, "selection", "count", f"reflector.selection.{key}", getattr(selection, key),
                   "bundle_manifest" if key in document["reflector"].get("selection", {}) else "core_default")
    for role, section in (("grouper", workflow.grouper), ("reflector", workflow.reflector.role),
                          ("editor", workflow.editor)):
        raw = document.get(role, {})
        _stage(result, role, "kind", f"{role}.kind", raw.get("kind"), "bundle_manifest",
               status="configured" if raw.get("kind") == "chat"
               else "non_chat" if section else "disabled")
        if raw.get("kind") != "chat":
            continue
        result.routes[role] = dict(client_model=section.model, upstream_model=None, endpoint=None,
                                   policy_digest=None, policy_version=None, reserve_tokens=section.max_output_tokens,
                                   source="bundle_manifest")
        settings = {"max_output_tokens": (section.max_output_tokens, "tokens")}
        if role != "grouper":
            settings.update(context_compact_chars=(section.context_compact_chars, "characters"),
                            context_message_max_chars=(section.context_message_max_chars, "characters"))
        else:
            limits = grouping_limits
            settings.update(max_input_chars=(section.max_input_chars, "characters"),
                            glimpse_chars=(limits.glimpse_chars, "characters"), read_chars=(limits.read_chars, "characters"),
                            group_output_tokens=(limits.group_output_tokens, "tokens"))
        for key, (value, unit) in settings.items():
            _stage(result, role, unit, f"{role}.{key}", value, "bundle_manifest" if key in raw else "core_default")


def _message_sizes(record, previous):
    """Sizes of the disclosed delta only. A broken prefix can recover at the next full log entry."""
    after, messages, count = (record.get(key) for key in ("messages_after", "messages", "message_count"))
    if (type(after) is not int or after < 0 or type(count) is not int or not isinstance(messages, list)
            or after + len(messages) != count or not all(isinstance(message, dict) for message in messages)
            or after and (previous is None or after > len(previous))):
        return None
    sizes = previous[:after] if after else []
    for message in messages:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sizes.append(len(encoded))
    return sizes


def role_turns(records, *, role, client_model, grouping_saved):
    """Project negotiated request styles and response usage from matching native log records."""
    pending = {}
    sizes = []
    rows = []
    for record in records:
        turn, style = record.get("turn"), record.get("request_style")
        if type(turn) is not int or turn < 0 or style not in ("modern", "modern_single", "modern_auto", "legacy"):
            continue
        if record.get("phase") == "model_request":
            row = dict(role=role, turn=turn, request_style=style, input_tokens=None, upstream_model=None,
                       provider=None, grouping_saved=grouping_saved, source="role_request_log")
            sizes = _message_sizes(record, sizes)
            tools = record.get("tool_names")
            row.update(message_count=None if sizes is None else len(sizes),
                       redacted_messages_bytes=None if sizes is None else 2 + sum(sizes) + max(0, len(sizes) - 1),
                       tool_count=len(tools) if isinstance(tools, list) else None)
            pending[turn, style] = row
            rows.append(row)
        elif record.get("phase") == "model_response" and record.get("requested_model") == client_model:
            row = pending.pop((turn, style), None)
            if row is None:
                continue
            usage = record.get("usage")
            tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            for key, name in (("upstream_model", "returned_model"), ("provider", "returned_provider")):
                value = record.get(name)
                if isinstance(value, str) and value and value.isprintable() and "[REDACTED]" not in value:
                    row[key] = value
            if type(tokens) is int and tokens >= 0:
                row.update(input_tokens=tokens, source="provider_usage")
    return rows


def resolve_preflight_inputs(state_directory: Path, prepared_digest: str, *, operation_id: str | None = None,
                             trace: Path | None = None, baseline: Path | None = None) -> PreflightInputs:
    """Reopen a prepared /16 campaign from existing state, without preparing or starting it."""
    storage = PreparationStorage(state_directory, read_only=True)
    prepared = storage.get_prepared(prepared_digest)
    bundle = storage.get_bundle(prepared.descriptor.bundle_digest)
    if prepared.descriptor.evaluation is not None or bundle.descriptor.source_kind != "campaign":
        raise PreparationError("unsupported_input", "read", "A prepared campaign is required.")
    path = bundle.path / bundle.descriptor.entrypoint
    try:
        manifest = load_manifest_v2(path)
        if manifest.schema != "evolution-v2-manifest/16" or manifest.workflow is None:
            raise PreparationError("unsupported_schema", "read")
        result = PreflightInputs(prepared_digest, bundle.digest, manifest)
        _settings(result, json.loads(path.read_bytes()), json.loads((bundle.path / bundle.descriptor.launch).read_bytes()))
    except (OSError, ManifestError, KeyError, TypeError, core_raw.CoreRawUnavailable):
        raise PreparationError("artifacts_invalid", "read") from None
    if trace is not None or baseline is not None or operation_id is not None:
        from .preflight_observations import read_observations
        read_observations(result, storage, operation_id, trace, baseline)
    return result
