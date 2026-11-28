"""Combine authenticated Lab inputs and Core request measurements into a context report."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from evolution_lab.context_report import (
    ContextReport,
    Measurement,
    Metric,
    ReportRow,
    Route,
    calculate_token_budget,
)
from evolution_lab.preparation.context_preflight import resolve_preflight_inputs


def _number(value, unit, provenance, source):
    if value is None:
        return Measurement.unknown(unit=unit, source=source)
    return Measurement.exact(value, unit=unit, provenance=provenance, source=source)


def _stages(inputs):
    for stage in inputs.stages:
        status = stage["status"]
        source = stage["source"]
        field = stage["setting"]
        unit = {"characters": "chars", "count": "items"}.get(stage["unit"], stage["unit"])
        metrics = []
        reason = None
        if unit == "kind":
            kind = stage["value"] or "undeclared"
            reason = f"{field} = {kind}"
        elif type(stage["value"]) is int:
            provenance = (
                "configuration"
                if source
                in {"bundle_launch", "bundle_manifest", "core_default", "redacted_trace_scenario"}
                else "measured"
            )
            metrics.append(Metric(field, _number(stage["value"], unit, provenance, source)))
        if any(stage[key] is not None for key in ("before", "after", "lost")):
            for key in ("before", "after", "lost"):
                provenance = "derived" if key == "lost" else "measured"
                metrics.append(Metric(key, _number(stage[key], unit, provenance, source)))
        elif not metrics and not reason:
            reason = field
        basis = {
            "non_chat": "skipped",
            "disabled": "skipped",
            "scenario": "hypothetical",
            "observed_prefix": "observed",
            "observed_log": "observed",
        }.get(status, status)
        if status == "observed_prefix":
            reason = "Count covers the available journal prefix."
        if source == "redacted_trace_scenario":
            reason = "Evidence selection applied to the disclosed trace; hidden fields cannot be recovered."
        yield ReportRow(stage["stage"], basis, metrics=tuple(metrics), reason=reason)

    if not any(row["stage"] == "recorded_episode" for row in inputs.stages):
        yield ReportRow(
            "recorded_episode",
            "unavailable",
            reason="Supply a compatible trace or training evaluation to measure the recorded episode.",
        )


def _observed(inputs, role, scenario):
    from evolution_lab.context_scenario import observed_window

    config = inputs.routes[role]
    for turn in inputs.turns:
        if turn["role"] != role:
            continue
        route = Route(
            model=turn["upstream_model"],
            upstream=turn["provider"],
            request_style=turn["request_style"],
            policy_version=turn.get("policy_version"),
            policy_digest=turn.get("policy_digest"),
            source="saved operation role and policy records",
        )
        window = observed_window(route, scenario)
        if scenario and window.lower is not None and scenario.tokenizer:
            route = replace(route, tokenizer=scenario.tokenizer, tokenizer_source=scenario.source)
        usage = _number(
            turn["input_tokens"], "tokens", "provider_usage", "role response usage.prompt_tokens"
        )
        reserve = _number(
            config["reserve_tokens"], "tokens", "configuration", f"{role}.max_output_tokens"
        )
        metrics = tuple(
            Metric(key, _number(turn[key], unit, "measured", "disclosed role log"))
            for key, unit in (
                ("message_count", "items"),
                ("tool_count", "items"),
                ("redacted_messages_bytes", "bytes"),
            )
            if turn.get(key) is not None
        )
        reason = "Saved role telemetry. The exact wire body cannot be reconstructed from redacted request logs."
        yield ReportRow(
            "role_request",
            "observed",
            role=role,
            turn=turn["turn"],
            session=turn.get("session"),
            reason=reason,
            route=route,
            metrics=metrics,
            budget=calculate_token_budget(window, usage, reserve),
        )
        if (
            scenario
            and scenario.reserve_tokens is not None
            and window.lower is not None
            and turn["input_tokens"] is not None
        ):
            alternative = _number(
                scenario.reserve_tokens, "tokens", "configuration", "scenario.reserve_tokens"
            )
            yield ReportRow(
                "role_request",
                "hypothetical",
                role=role,
                turn=turn["turn"],
                session=turn.get("session"),
                scenario="same observed input with the specified response reserve",
                route=route,
                budget=calculate_token_budget(window, usage, alternative),
            )


def _scenario_rows(inputs, role, section, scenario):
    from evolution.tree import measure_manifest_chat_scenario

    styles = scenario.styles if scenario else ("modern",)
    page_chars = scenario.page_chars if scenario else None
    prompt = section.system_prompt.read_text(encoding="utf-8") if section.system_prompt else None
    workflow = inputs.manifest.workflow
    for style in styles:
        if scenario:
            route = scenario.report_route(style)
            window = scenario.window_measurement()
        else:
            route = Route(
                model=section.model,
                request_style=style,
                source="prepared client model; modern style assumed for this scenario",
            )
            window = Measurement.unknown(
                unit="tokens", source="supply the upstream route and window"
            )
        reserve_value = section.max_output_tokens
        reserve_source = f"{role}.max_output_tokens"
        if scenario and scenario.reserve_tokens is not None:
            reserve_value, reserve_source = scenario.reserve_tokens, "scenario.reserve_tokens"
        reserve = _number(reserve_value, "tokens", "configuration", reserve_source)
        measurements = measure_manifest_chat_scenario(
            inputs.manifest,
            role=role,
            request_style=style,
            prompt_text=prompt,
            page_chars=page_chars,
            model=scenario.model if scenario else None,
            max_output_tokens=reserve_value,
        )
        phases = ("first",) if role == "grouper" else ("first", "after_page", "after_compaction")
        for phase in phases:
            sample = measurements.get(phase)
            if sample is None:
                yield ReportRow(
                    "role_request",
                    "unavailable",
                    role=role,
                    scenario=phase,
                    route=route,
                    reason="The scripted history did not reach this turn within the scenario limits.",
                )
                continue
            metrics = []
            for key, unit in (
                ("serialized_chars", "chars"),
                ("serialized_bytes", "bytes"),
                ("before_compaction_chars", "chars"),
                ("after_compaction_chars", "chars"),
                ("protected_exchange_chars", "chars"),
                ("tool_count", "items"),
                ("message_count", "items"),
                ("requested_page_chars", "chars"),
                ("page_chars", "chars"),
                ("portion_observations", "items"),
                ("portion_input_chars", "chars"),
                ("portion_budget_chars", "chars"),
            ):
                if key in sample:
                    metrics.append(
                        Metric(key, _number(sample[key], unit, "measured", "Core scenario request"))
                    )
            for name, count in sample.get("components", {}).items():
                metrics.append(
                    Metric(
                        name, _number(count, "chars", "measured", "Core first-request breakdown")
                    )
                )
            nbytes = sample.get("serialized_bytes")
            estimate = (
                scenario.estimate_input(nbytes)
                if scenario and nbytes is not None
                else Measurement.unknown(
                    unit="tokens", source="supply calibrated route tokenization"
                )
            )
            reason = "Scripted role input and evidence; sizes refer to this scenario."
            if role == "reflector" and workflow.grouper is not None and not inputs.grouping_saved:
                reason = "Grouping output is unavailable. This scenario uses synthetic evidence."
            if sample.get("limitations"):
                reason += " " + ", ".join(sample["limitations"]) + "."
            yield ReportRow(
                "role_request",
                "hypothetical" if nbytes is not None else "unavailable",
                role=role,
                turn=sample.get("scenario_turn"),
                scenario=phase,
                reason=reason,
                metrics=tuple(metrics),
                route=route,
                budget=calculate_token_budget(window, estimate, reserve),
            )


def inspect_context(
    state: Path, prepared: str, *, operation=None, trace=None, baseline=None, scenario=None
):
    from evolution_lab.context_scenario import load_context_scenario

    inputs = resolve_preflight_inputs(
        Path(state), prepared, operation_id=operation, trace=trace, baseline=baseline
    )
    scenarios = load_context_scenario(Path(scenario)).roles if scenario is not None else {}
    rows = list(_stages(inputs))
    workflow = inputs.manifest.workflow
    for role, section in (
        ("grouper", workflow.grouper),
        ("reflector", workflow.reflector.role),
        ("editor", workflow.editor),
    ):
        if role not in inputs.routes:
            continue
        route = scenarios.get(role)
        if operation or trace:
            if not any(turn["role"] == role for turn in inputs.turns):
                rows.append(
                    ReportRow(
                        "role_request",
                        "unavailable",
                        role=role,
                        reason="No matching request was recorded for this role.",
                    )
                )
        rows.extend(_observed(inputs, role, route))
        rows.extend(_scenario_rows(inputs, role, section, route))
    return ContextReport(
        tuple(rows),
        prepared_digest=inputs.prepared_digest,
        bundle_digest=inputs.bundle_digest,
        core_version=inputs.producing_version,
        operation=inputs.operation_id,
        baseline_operation=inputs.baseline_operation_id,
        baseline_version=inputs.baseline_version,
    )
