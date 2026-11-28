"""Every CLI read variant is served by a command-layer read, or excluded with a reason (ADR-0070)."""
from __future__ import annotations

import argparse

import pytest

from evolution_lab.cli.parser import _MUTATIONS, PROBE_MUTATIONS, PROJECT_MUTATIONS, build_parser
from evolution_lab.commands import REGISTRY, Effect

#: CLI variant (public command path, plus a result-changing option or positional) -> layer command name.
CLI_READS: dict[tuple[str, ...], str] = {
    ("bundle", "show"): "bundle_show",
    ("bundle", "measurements"): "bundle_measurements",
    ("prepared", "show"): "prepared_show",
    ("preflight",): "context_preflight",
    ("approvals",): "approvals_list",
    ("exec-policy", "list"): "execution_policies_list",
    ("exec-policy", "show"): "execution_policy_show",
    ("routing",): "operation_routing",
    ("agent", "list"): "targets_list",
    ("agent", "status"): "target_status",
    ("job", "status"): "task_status",
    ("list",): "operations_list",
    ("list", "--summary"): "operations_summary",
    ("status",): "operation_status",
    ("report",): "operation_report",
    ("report", "--markdown"): "operation_report_markdown",
    ("view",): "operation_campaign",
    ("suspensions",): "operation_suspensions",
    ("diff",): "operation_diff",
    ("sample",): "operation_sample",
    ("sample", "--disclose-private"): "operation_sample_disclosed",
    ("attempt",): "operation_attempt",
    ("attempt", "--disclose-private"): "operation_attempt_disclosed",
    ("reflection",): "operation_reflection",
    ("reflection", "--disclose-private"): "operation_reflection_disclosed",
    ("measurements",): "operation_measurements",
    ("measurements", "MEASUREMENT"): "operation_measurement",
    ("health",): "health",
    ("guides", "list"): "guides_list",
    ("guides", "requirements"): "requirements",
    ("scenarios", "list"): "scenarios_list",
    ("scenarios", "show"): "scenario_show",
    ("scenarios", "schema"): "scenario_schema",
    ("project", "list"): "projects_list",
    ("project", "show"): "project_show",
    ("project", "status"): "project_status",
    ("project", "facts"): "project_facts",
    ("project", "approvals"): "project_approvals",
    ("project", "confirmations"): "project_confirmations",
    ("project", "tree"): "project_tree",
    ("project", "operations"): "project_operations",
    ("project", "operations", "--summary"): "project_operations_summary",
}

#: CLI variant -> why it is not a command-layer read.
EXCLUDED: dict[tuple[str, ...], str] = {
    ("mcp",): "the MCP server itself, not a read command",
    ("events",): "an SSE stream, not one request",
    ("watch",): "a text-only follower over status and the campaign view",
    ("export-content",): "writes a local archive",
    ("report", "--markdown", "--output"): "writes a local file",
    ("probe", "history"): "probe dialog; stays in cli/probe.py",
    ("probe", "list"): "probe dialog; stays in cli/probe.py",
    ("guides", "path"): "prints a local path",
    ("scenarios", "path"): "prints a local path",
    ("exec-policy", "digest"): "a local computation",
    ("panel",): "starts the panel, not a service read",
    ("panel", "status"): "the panel's own state, not a service read",
    ("panel", "stop"): "stops the panel, not a service read",
    ("serve",): "runs the service",
    ("new-project",): "writes a local project",
    ("evaluate",): "a quickstart run, not a service read",
    ("campaign", "DIR"): "a quickstart run, not a service read",
    ("trace", "DIR"): "a quickstart trace of a project directory, not a service read",
}

#: Old names that reach the same parser and handler as a public name: they are not variants of their own.
ALIASES = {("policy",), ("target",), ("task",), ("campaign",), ("operation",), ("agent", "probe")}

#: Variants an option or a positional adds to a command path (checked against the parser below).
OPTION_VARIANTS = {
    ("list", "--summary"), ("report", "--markdown"), ("report", "--markdown", "--output"),
    ("sample", "--disclose-private"), ("attempt", "--disclose-private"), ("reflection", "--disclose-private"),
    ("measurements", "MEASUREMENT"), ("project", "operations", "--summary"), ("campaign", "DIR"), ("trace", "DIR"),
}


def _children(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def _is_mutation(path: tuple[str, ...], internal: str) -> bool:
    if internal == "project":
        return len(path) > 1 and path[1] in PROJECT_MUTATIONS
    if path[0] == "probe":
        return len(path) > 1 and path[1] in PROBE_MUTATIONS
    return (internal, path[1] if len(path) > 1 else None) in _MUTATIONS


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...], internal: str):
    if path in ALIASES or _is_mutation(path, internal):
        return
    children = _children(parser)
    if children is None:
        yield path
        return
    if not children.required:
        yield path
    for name, child in children.choices.items():
        yield from _walk(child, path + (name,), internal)


def cli_variants() -> set[tuple[str, ...]]:
    top = _children(build_parser())
    found: set[tuple[str, ...]] = set()
    for name, parser in top.choices.items():
        internal = parser._defaults.get("command", name)
        found.update(_walk(parser, (name,), internal))
    return found


def _parser_at(path: tuple[str, ...]) -> argparse.ArgumentParser:
    parser = build_parser()
    for name in path:
        parser = _children(parser).choices[name]
    return parser


@pytest.mark.parametrize("variant", sorted(OPTION_VARIANTS))
def test_option_variants_name_real_options(variant):
    names = [part for part in variant if not part.startswith("--") and not part.isupper()]
    parser = _parser_at(tuple(names))
    for part in variant[len(names):]:
        if part.startswith("--"):
            assert part in parser._option_string_actions, (variant, part)
        else:
            assert any(not action.option_strings and action.dest not in {"help"} for action in parser._actions)


def test_every_cli_read_variant_is_mapped_or_excluded():
    expected = cli_variants() | OPTION_VARIANTS
    mapped = set(CLI_READS) | set(EXCLUDED)
    assert not set(CLI_READS) & set(EXCLUDED)
    assert expected - mapped == set(), "unmapped CLI variants"
    assert mapped - expected == set(), "mapped variants the CLI does not have"


@pytest.mark.parametrize(("variant", "name"), sorted(CLI_READS.items()))
def test_reads_never_issue_non_get(variant, name):
    from tests.test_commands_cli_parity import CASES  # lazy: that module imports this one

    command = REGISTRY[name]
    assert command.effect is Effect.READ
    if not command.needs_service:
        assert command.build is None and command.run is not None
        return
    kwargs = next(kwargs for case_variant, _, kwargs in CASES if case_variant == variant)
    request = command.build(**kwargs)
    assert request.method == "GET" and request.payload is None and request.mutation_key is None


#: Command -> what its value is compared with (spec section 2, "Comparison point").
#: ``cli-json``: the CLI's ``--json`` output (or its printed form) carries the value;
#: ``service-response``: the service response itself, which the CLI reads but does not print as JSON;
#: ``shipped-file``: the shipped guide or catalogue file, for offline artifacts.
AUTHORITY: dict[str, str] = {
    "health": "cli-json",
    "operations_list": "cli-json",
    "operations_summary": "cli-json",
    "operation_status": "cli-json",
    "operation_failure": "service-response",
    "operation_refusal": "service-response",
    "operation_inputs": "service-response",
    "operation_report": "cli-json",
    "operation_report_markdown": "service-response",
    "operation_campaign": "cli-json",
    "operation_routing": "cli-json",
    "operation_suspensions": "cli-json",
    "operation_measurements": "cli-json",
    "operation_measurement": "cli-json",
    "operation_sample": "cli-json",
    "operation_sample_disclosed": "cli-json",
    "operation_attempt": "cli-json",
    "operation_attempt_disclosed": "cli-json",
    "operation_reflection": "cli-json",
    "operation_reflection_disclosed": "cli-json",
    "operation_diff": "cli-json",
    "projects_list": "cli-json",
    "project_show": "cli-json",
    "project_status": "cli-json",
    "project_facts": "cli-json",
    "project_approvals": "cli-json",
    "project_confirmations": "cli-json",
    "project_tree": "cli-json",
    "project_operations": "cli-json",
    "project_operations_summary": "cli-json",
    "bundle_show": "cli-json",
    "bundle_measurements": "cli-json",
    "prepared_show": "cli-json",
    "approvals_list": "cli-json",
    "execution_policies_list": "cli-json",
    "execution_policy_show": "cli-json",
    "targets_list": "cli-json",
    "target_status": "cli-json",
    "task_status": "cli-json",
    "service_scenarios_list": "service-response",
    "service_scenario_show": "service-response",
    "service_scenario_schema": "service-response",
    "service_requirements": "service-response",
    "context_preflight": "cli-json",
    "guides_list": "cli-json",
    "guide_read": "shipped-file",
    "requirements": "cli-json",
    "scenarios_list": "cli-json",
    "scenario_show": "cli-json",
    "scenario_schema": "shipped-file",
}


def _read_names() -> set[str]:
    return {name for name, command in REGISTRY.items() if command.effect is Effect.READ}


def test_every_read_has_a_comparison_authority():
    assert set(AUTHORITY) == _read_names()
    assert set(AUTHORITY.values()) <= {"cli-json", "service-response", "shipped-file"}


def test_registry_reads_are_exactly_the_tested_tables_and_build_get():
    from tests.test_commands_offline import OFFLINE  # lazy: avoid import cycles between test modules
    from tests.test_commands_projects import CASES as PROJECT_CASES
    from tests.test_commands_requests import READS

    request_names = {name for name, _, _ in READS}
    project_names = {name for _, name, _ in PROJECT_CASES}
    assert request_names | project_names | set(OFFLINE) == _read_names()
    for name, kwargs in [(n, k) for n, k, _ in READS] + [(n, k) for _, n, k in PROJECT_CASES]:
        request = REGISTRY[name].build(**kwargs)
        assert request.method == "GET" and request.payload is None and request.mutation_key is None
