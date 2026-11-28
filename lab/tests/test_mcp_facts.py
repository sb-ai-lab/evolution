"""Approval forms describe the state Lab read and disclose secret names only."""
import pytest

from evolution_lab.mcp import facts
from test_execution_policy import policy_payload

D = "a" * 64
POLICY = "b" * 64
TARGET = {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "generation"}
TARGET_PATH = "/v1/targets/target?owner_generation=generation"
PREPARED_PATH = f"/v1/prepared-executions/{D}"
POLICY_PATH = f"/v1/execution-policies/{POLICY}"
AUTO_POLICY = {"schema": "lab-execution-policy/2", "auto_resume": {
    "reasons": ["editor_failing"], "max_resumes": 3, "delay_seconds": 0,
}}


class FactsTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def call(self, request):
        self.requests.append(request)
        return self.responses[request.path]


def start_reads(policy=None, *, kind="evaluation", names=()):
    target = {"id": "target", "schema": "lab-owner-target/2"}
    if policy is not None:
        target["execution_policy"] = POLICY
    responses = {
        TARGET_PATH: target,
        PREPARED_PATH: {"schema": "lab-prepared-summary/1", "kind": kind, "secret_names": list(names),
                        "evaluation": {"case_ids": ["case-a", "case-b"], "repeat_seeds": [11, 12, 13],
                                       "max_turns": 4} if kind == "evaluation" else None},
    }
    if policy is not None:
        responses[POLICY_PATH] = {"schema": "lab-execution-policy-record/1", "policy": policy}
    return responses


@pytest.mark.parametrize("policy_kind", ["routed", "auto_only", "unbound"])
async def test_start_form_states_scale_spending_and_further_starts(policy_kind):
    policy = policy_payload() if policy_kind == "routed" else AUTO_POLICY if policy_kind == "auto_only" else None
    if policy_kind == "routed":
        policy["gateway"]["limit_units"] = 5000000
    transport = FactsTransport(start_reads(policy, kind="campaign" if policy_kind == "auto_only" else "evaluation",
                                          names=("ROUTER_KEY", "SCORER_TOKEN")))
    detail = await facts.gather(transport, "operation_start", {
        "prepared_digest": D, "target_ref": TARGET, "secrets": {"ROUTER_KEY": "secret-fixture-value"},
    })
    message, _ = facts.render(detail)
    assert "free" not in message.lower()
    assert "secret-fixture-value" not in message and "ROUTER_KEY" in message
    assert D in message and "target" in message
    if policy_kind == "routed":
        assert "5000000 RUB" in message and "only routed calls" in message and "outside" in message
        assert detail.cost.uncapped_secrets == ["ROUTER_KEY"]
    else:
        assert "no Lab spending limit" in message
    if policy_kind == "auto_only":
        assert "3 further starts" in message
        assert detail.cost.expected_calls == "unknown"
    else:
        assert "6 conversations" in message and "4 target turns" in message


async def test_delete_form_names_the_operation_and_the_observed_start():
    transport = FactsTransport({"/v1/operations/op-a": {"starts": [{"start": 1}, {"start": 2}]}})
    detail = await facts.gather(transport, "delete_results", {"operation_id": "op-a"})
    message, _ = facts.render(detail)
    assert detail.observed_start == 2
    assert "op-a" in message and "start 2" in message.lower()
    assert "results" in message.lower()


async def test_resume_form_names_each_selected_finding_effect_and_reason():
    transport = FactsTransport({
        **start_reads(AUTO_POLICY, kind="campaign"),
        "/v1/operations/op-a": {"starts": [{"start": 1}, {"start": 2}], "prepared_digest": D,
                                "auto_resume": {"used": 1}},
        "/v1/operations/op-a/refusal": {"start": 2, "findings": [
            {"code": "different_project", "instance_digest": "c" * 64, "effects": ["keep", "replace"],
             "effect_texts": ["Preserve the old baseline", "Replace the baseline"]},
            {"code": "different_tree", "instance_digest": "d" * 64, "effects": ["rebuild"],
             "effect_texts": ["Rebuild the tree"]},
            {"code": "unselected", "instance_digest": "e" * 64, "effects": [], "effect_texts": []},
        ]},
    })
    detail = await facts.gather(transport, "operation_resume", {
        "operation_id": "op-a", "target_ref": TARGET, "reason": "keep this campaign going",
        "accept": [{"code": "different_project", "digest": "c" * 64, "effect": "keep"},
                   {"code": "different_tree", "digest": "d" * 64, "effect": "rebuild"}],
    })
    message, _ = facts.render(detail)
    assert detail.observed_start == 2
    for text in ("different_project", "Preserve the old baseline", "different_tree", "Rebuild the tree",
                 "keep this campaign going", "2 further starts"):
        assert text in message
    assert "Replace the baseline" not in message and "unselected" not in message


@pytest.mark.parametrize("tool, arguments, needs_author", [
    ("project_confirm", {"project_id": "project", "implementation": None, "claim": "checked"}, True),
    ("project_accept_commit", {"project_id": "project", "reason": "reviewed",
                               "expected": [{"id": "fact", "from": {}, "to": {}}]}, True),
    ("policy_confirm", {"policy": AUTO_POLICY, "expected_digest": D}, True),
    ("project_revoke_acceptance", {"project_id": "project", "acceptance_id": "acceptance", "reason": "obsolete"}, False),
    ("project_revoke_confirmation", {"project_id": "project", "confirmation_id": "confirmation", "reason": "obsolete"}, False),
])
async def test_form_requires_author_for_exactly_the_attestations(tool, arguments, needs_author):
    detail = await facts.gather(FactsTransport({}), tool, arguments)
    _, schema = facts.render(detail)
    assert detail.needs_author is needs_author
    assert schema["required"] == (["confirm", "author"] if needs_author else ["confirm"])
    assert schema["properties"]["confirm"]["type"] == "boolean"
    if needs_author:
        assert schema["properties"]["author"]["type"] == "string"
    else:
        assert "author" not in schema["properties"]


async def test_policy_confirmation_shows_the_routes_and_limit():
    policy = policy_payload()
    detail = await facts.gather(FactsTransport({}), "policy_confirm", {"policy": policy, "expected_digest": POLICY})
    message, _ = facts.render(detail)
    for text in ("editor", "upstream-model", "ROUTER_KEY", "target", "1000000 RUB", POLICY):
        assert text in message


async def test_sync_form_checks_the_exact_sha_without_recording_a_mutation_key():
    sha = "c" * 40
    transport = FactsTransport({"/v1/projects/project/sync/check": {
        "upstream": {"sha": sha}, "commits": [{"sha": sha, "subject": "Improve docs"}], "commits_truncated": True,
    }})
    detail = await facts.gather(transport, "project_sync", {
        "project_id": "project", "upstream": sha, "request_id": "sync", "owner_generation": "generation",
    })
    message, _ = facts.render(detail)
    assert sha in message and "Improve docs" in message and "truncated" in message
    (request,) = transport.requests
    assert request.payload["rev"] == sha
    assert request.mutation_key is None
