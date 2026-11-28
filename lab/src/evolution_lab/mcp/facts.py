"""Read and render the facts of an asking MCP action (ADR-0071)."""
from __future__ import annotations

from dataclasses import dataclass
import json

from evolution_lab.commands import mutations, operations, project_mutations, resources
from evolution_lab.commands.errors import LocalFailure
from evolution_lab.execution_policy import ExecutionPolicy, PolicyInvalid, lab_filled_names, parse_policy
from evolution_lab.mcp.transport import INVALID_HINT

_RESOURCES = {
    "delete_results": ("Delete results", "The operation's stored results will be deleted."),
    "release": ("Release resource", "Lab will release linked reservations based on the human report of manual cleanup."),
    "cleanup": ("Clean up resource", "Lab will stop remaining processes it owns for this resource, including its target where applicable."),
}
_ATTESTATIONS = frozenset({"project_confirm", "project_accept_commit", "policy_confirm"})


@dataclass(frozen=True)
class CostFacts:
    expected_calls: str
    policy_bound: bool
    gateway: tuple[int, str] | None
    uncapped_secrets: list[str]
    auto_resumes: int | None
    routes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Facts:
    action: str
    target: str
    observed_start: int | None
    cost: CostFacts | None
    lost: str | None
    findings: list[dict]
    commits: list[str]
    secret_names: list[str]
    needs_author: bool


def _start_number(status: dict) -> int:
    starts = status["starts"]
    if not starts:
        raise LocalFailure("invalid_service_response", hint=INVALID_HINT)
    return starts[-1]["start"]


def _expected_calls(prepared: dict) -> str:
    plan = prepared["evaluation"]
    if plan is None:
        return "unknown"
    count = len(plan["case_ids"]) * len(plan["repeat_seeds"])
    return f"{count} conversations of at most {plan['max_turns']} target turns each. Model calls unknown"


def _cost(policy: ExecutionPolicy | None, expected: str, names: set[str], *,
          bound: bool, is_campaign: bool, used: int = 0, show_routes: bool = False) -> CostFacts:
    gateway = None if policy is None or policy.gateway is None else (policy.gateway.limit_units, policy.gateway.currency)
    direct_names = names if policy is None else names - lab_filled_names(policy)
    auto = None
    routes: tuple[str, ...] = ()
    if policy is not None:
        if is_campaign and policy.auto_resume is not None:
            auto = max(0, policy.auto_resume.max_resumes - used)
        if show_routes:
            routes = tuple(
                f"{name}: {route.client_model} → {route.model} at {route.endpoint}"
                + (f" (provider key name {route.api_key_secret})" if route.api_key_secret else "")
                for name, route in sorted(policy.routes.items())
            )
    return CostFacts(expected, bound, gateway, sorted(direct_names), auto, routes)


async def _target_policy(transport, target: dict) -> ExecutionPolicy | None:
    status = await transport.call(resources.target_status(target["id"], target["owner_generation"]))
    digest = status.get("execution_policy")
    if digest is None:
        return None
    record = await transport.call(resources.execution_policy_show(digest))
    return parse_policy(record["policy"])


async def _finding_facts(transport, operation: str, observed: int, selected: list[dict], reason: str | None) -> list[dict]:
    if not selected:
        return []
    refusal = await transport.call(operations.operation_refusal(operation))
    if refusal["start"] != observed:
        raise LocalFailure("state_changed", "The operation's refusal changed while reading approval facts.",
                           hint="Read the current operation and refusal before trying again.", stage="before_start")
    by_identity = {(row["code"], row["instance_digest"]): row for row in refusal["findings"]}
    result = []
    for acceptance in selected:
        row = by_identity.get((acceptance["code"], acceptance["digest"]))
        descriptions = dict(zip(row["effects"], row["effect_texts"])) if row is not None else {}
        effect = acceptance["effect"]
        result.append({"code": acceptance["code"], "effect": effect,
                       "description": descriptions.get(effect), "reason": reason})
    return result


def _project(arguments: dict) -> str:
    name = f"Project {arguments['project_id']}"
    if arguments.get("implementation") is not None:
        name += f", implementation {arguments['implementation']}"
    return name


async def gather(transport, tool: str, arguments: dict) -> Facts:
    """Observe the bound inputs before elicitation, including for clients without forms."""
    observed = cost = lost = None
    findings: list[dict] = []
    commits: list[str] = []
    supplied = set(arguments.get("secrets", {}))
    if tool in {"operation_start", "project_start", "operation_resume"}:
        target = arguments["target_ref"]
        target_name = f"target {target['id']} in Owner generation {target['owner_generation']}"
        prepared_digest = arguments.get("prepared_digest")
        used = 0
        if tool == "operation_resume":
            operation = arguments["operation_id"]
            status = await transport.call(operations.operation_status(operation))
            observed = _start_number(status)
            prepared_digest = prepared_digest or status["prepared_digest"]
            used = status["auto_resume"]["used"]
            action = "Resume operation"
            subject = f"Operation {operation}, start {observed}, on {target_name} with prepared {prepared_digest}."
            findings = await _finding_facts(transport, operation, observed, arguments.get("accept") or [],
                                            arguments.get("reason"))
        prepared = await transport.call(resources.prepared_show(prepared_digest))
        if tool != "operation_resume":
            action = f"Start {prepared['kind']}"
            prefix = f"{_project(arguments)}. " if tool == "project_start" else ""
            subject = f"{prefix}Prepared {prepared['kind']} {prepared_digest} on {target_name}."
        policy = await _target_policy(transport, target)
        cost = _cost(policy, _expected_calls(prepared), set(prepared["secret_names"]) | supplied,
                     bound=policy is not None, is_campaign=prepared["kind"] == "campaign", used=used)
    elif tool in _RESOURCES:
        action, lost = _RESOURCES[tool]
        ref = ({"kind": "operation", "id": arguments["operation_id"]} if tool == "delete_results" else arguments["ref"])
        subject = f"{ref['kind'].capitalize()} {ref['id']}"
        if ref["kind"] == "operation":
            status = await transport.call(operations.operation_status(ref["id"]))
            observed = _start_number(status)
            subject += f", start {observed}"
        if tool == "release":
            subject += f". Reason: {arguments['reason']}"
    elif tool == "project_sync":
        action = "Sync project"
        sha = arguments["upstream"]
        subject = f"{_project(arguments)}, upstream commit {sha}"
        # The internal fact read uses the same check as the CLI. It takes no mutation-table slot.
        checked = await transport.call(project_mutations.project_sync_check(None, None, arguments["project_id"], rev=sha))
        if checked["upstream"]["sha"] != sha:
            raise LocalFailure("usage_error", hint="Use the exact upstream SHA returned by project_sync_check.")
        commits = [f"{commit['sha']} {commit['subject']}" for commit in checked["commits"]]
        if checked["commits_truncated"]:
            commits.append("The service truncated the incoming commit list.")
    elif tool == "policy_confirm":
        action = "Confirm execution policy"
        subject = f"Execution policy {arguments['expected_digest']}"
        try:
            policy = parse_policy(arguments["policy"])
        except PolicyInvalid:
            raise LocalFailure("usage_error", hint="Check the execution policy document.") from None
        cost = _cost(policy, "unknown", set(), bound=False, is_campaign=True, show_routes=True)
    elif tool == "project_confirm":
        action = "Confirm project claim"
        subject = f"{_project(arguments)}. Claim: {arguments['claim']}"
    elif tool == "project_accept_commit":
        action = "Accept project facts"
        expected = [json.dumps(row, ensure_ascii=True, sort_keys=True) for row in arguments["expected"]]
        subject = f"{_project(arguments)}. Reason: {arguments['reason']}\nFacts to accept:\n" + "\n".join(expected)
    elif tool in {"project_revoke_confirmation", "project_revoke_acceptance"}:
        kind = tool.removeprefix("project_revoke_")
        action = f"Revoke project {kind}"
        subject = f"{_project(arguments)}. {kind.capitalize()} {arguments[kind + '_id']}. Reason: {arguments['reason']}"
        lost = f"The recorded {kind} will no longer apply."
    else:
        raise ValueError(f"no approval facts for {tool}")
    return Facts(action, subject, observed, cost, lost, findings, commits, sorted(supplied), tool in _ATTESTATIONS)


def _render_cost(cost: CostFacts, *, confirming_policy: bool) -> list[str]:
    lines = [f"Expected calls: {cost.expected_calls}."]
    if confirming_policy:
        lines.append("Proposed execution policy.")
        lines.extend(cost.routes)
    else:
        lines.append("Execution policy bound." if cost.policy_bound else "No execution policy bound.")
    if cost.gateway is None:
        lines.append("Spending: no Lab spending limit.")
    else:
        units, currency = cost.gateway
        lines.append(f"Lab gateway limit: {units} {currency} in ledger units, for only routed calls.")
        if cost.uncapped_secrets:
            lines.append("Secret names that may allow calls outside the gateway: " + ", ".join(cost.uncapped_secrets))
    if cost.auto_resumes is not None:
        lines.append(f"Automatic resumes allow {cost.auto_resumes} further starts.")
    return lines


def render(facts: Facts) -> tuple[str, dict]:
    lines = [f"{facts.action}?", facts.target]
    if facts.cost is not None:
        lines.extend(_render_cost(facts.cost, confirming_policy=facts.action == "Confirm execution policy"))
    if facts.lost is not None:
        lines.append(facts.lost)
    if facts.findings:
        lines.append("Accept these Core refusal findings:")
        for row in facts.findings:
            effect = row["effect"] or "no effect selected"
            description = f" ({row['description']})" if row["description"] else ""
            lines.append(f"{row['code']}: {effect}{description}.")
        reason = facts.findings[0]["reason"]
        if reason:
            lines.append(f"Reason: {reason}")
    if facts.commits:
        lines.extend(["Incoming commits:", *facts.commits])
    if facts.secret_names:
        lines.append("Secret names supplied: " + ", ".join(facts.secret_names))
    properties = {"confirm": {"type": "boolean", "title": "Confirm this action"}}
    if facts.needs_author:
        properties["author"] = {"type": "string", "title": "Author name", "minLength": 1}
    return "\n".join(lines), {"type": "object", "properties": properties, "required": list(properties)}
