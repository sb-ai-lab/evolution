"""Resolve an admitted /16 manifest into inputs for a role request measurement."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Literal

from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.execution.check_modes import ENFORCE_ALL
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
from evolution.tree.gate_scope import DEFAULT_MIN_CLAIM_CASES
from evolution.tree.manifest.manifest_v2 import SCHEMA_V16, ManifestV2, effective_group_text_chars
from evolution.tree.roles.config import ChatEditorSection, ModelGroupingSection
from evolution.tree.roles.context_preflight import (
    Json,
    TokenCounter,
    measure_chat_scenario,
    measure_grouping_scenario,
)
from evolution.tree.roles.reflector.reflect_budget import PROBE_URL_PLACEHOLDER
from evolution.tree.roles.reflector_contracts import ProbeAccess
from evolution.tree.roles.role_visibility import RoleVisibilityView


def measure_manifest_chat_scenario(
    manifest: ManifestV2,
    *,
    role: Literal["editor", "reflector", "grouper"],
    request_style: str,
    page_chars: int | None = None,
    max_output_tokens: int | None = None,
    model: str | None = None,
    count_tokens: TokenCounter | None = None,
    seed_files: Sequence[str] | None = None,
    prompt_text: str | None = None,
) -> dict[str, Json]:
    """Measure a role scenario for an admitted, verified /16 Bundle.

    Optional seed_files are regular-file names relative to the verified seed.
    A custom system prompt must be supplied from the verified Bundle. Only
    numbers, setting names and scenario provenance leave this function.
    """
    workflow = manifest.workflow
    if manifest.schema != SCHEMA_V16 or workflow is None:
        raise ValueError("context preflight requires a /16 workflow")
    if request_style not in {"modern", "modern_single", "modern_auto", "legacy"}:
        raise ValueError("unknown direct chat request style")
    if role not in {"editor", "reflector", "grouper"}:
        raise ValueError("unknown direct chat role")
    reflector = workflow.reflector
    section = {"grouper": workflow.grouper, "reflector": reflector.role, "editor": workflow.editor}[
        role
    ]
    if not isinstance(section, (ChatEditorSection, ModelGroupingSection)):
        raise ValueError("context preflight requires a direct chat role")
    if max_output_tokens is not None:
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        section = replace(section, max_output_tokens=max_output_tokens)
    if model is not None:
        section = replace(section, model=model)
    if section.system_prompt is not None and prompt_text is None:
        raise ValueError("custom role prompt requires the verified prompt text")
    if isinstance(section, ModelGroupingSection):
        return measure_grouping_scenario(
            section, request_style, workflow.campaign.metric_primary, prompt_text, count_tokens
        )
    assert role in {"editor", "reflector"}
    view = RoleVisibilityView.for_campaign(workflow.connection, manifest.policy)
    assert view is not None
    visible: list[str] = []
    if role == "editor":
        if seed_files is None:
            scan = scan_tree(manifest.seed_root, manifest.policy)
            seed_files = [entry.path for entry in scan.artifact if entry.kind == "file"]
        for path in seed_files:
            relative = PurePosixPath(path)
            if (
                view.visible(path)
                and not manifest.policy.is_excluded(relative, False)
                and not any(
                    manifest.policy.is_excluded(parent, True) for parent in relative.parents
                )
            ):
                visible.append(path)
    probe = reflector.probe if role == "reflector" else None
    access = (
        None
        if probe is None
        else ProbeAccess(
            PROBE_URL_PLACEHOLDER, probe.budget, probe.methods, probe.description, probe.string_body
        )
    )
    selection = reflector.selection
    editor_budget = getattr(workflow.editor, "budgets", None)
    rows = measure_chat_scenario(
        section,
        role=role,
        request_style=request_style,
        writable_paths=manifest.policy.writable_paths,
        visible_files=visible,
        page_chars=page_chars,
        prompt_text=prompt_text,
        probe=access,
        editor_max_changed_files=editor_budget.max_changed_files if editor_budget else None,
        count_tokens=count_tokens,
        operator_context=workflow.campaign.reflector_operator_context
        if role == "reflector"
        else workflow.campaign.editor_context,
        check_modes=workflow.checks or ENFORCE_ALL,
        evidence_pin=EvidencePin(
            workflow.evidence or DEFAULT_EVIDENCE, (workflow.checks or ENFORCE_ALL).input_modes()
        ),
        group_text_chars=effective_group_text_chars(reflector, manifest.schema),
        documents=reflector.environment_docs is not None,
        selection=selection,
        min_claim_cases=(
            selection.min_claim_cases
            if selection and selection.min_claim_cases is not None
            else DEFAULT_MIN_CLAIM_CASES
        ),
        families_expand=reflector.families is not None and reflector.families.kind == "metadata",
    )
    assumptions = ["empty_history", "synthetic_evidence_catalog", "no_provider_continuations"]
    if role == "editor":
        assumptions.append("synthetic_diagnosis")
    if role == "reflector" and workflow.grouper is not None:
        assumptions.append("no_observed_grouping_input")
    if probe:
        assumptions.append("probe_assumed_available")
    for row in rows.values():
        row.update(
            provenance="synthetic",
            limitations=[*assumptions, *row.get("limitations", ())],
            max_output_tokens=section.max_output_tokens,
            scenario_turn=row.get("scenario_turn", 1),
        )
    return rows
