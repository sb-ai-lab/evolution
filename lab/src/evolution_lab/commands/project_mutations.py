"""Project mutation requests shared by the CLI and MCP (ADR-0070, ADR-0071)."""
from __future__ import annotations

from evolution_lab.commands._core import Effect, Request, command, mutation_key
from evolution_lab.commands.projects import _project


@command("project_bundle", effect=Effect.MUTATION, schema="lab-project-bundle-result/1")
def project_bundle(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None = None,
) -> Request:
    payload = {"schema": "lab-service-project-bundle-request/1", "implementation": implementation}
    return Request("POST", _project(project_id, "bundles"), "lab-project-bundle-result/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_prepare", effect=Effect.MUTATION, schema="lab-project-prepared-result/1")
def project_prepare(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None, body: dict,
) -> Request:
    """Build the versioned project request from an unversioned prepare document."""
    settings = {"sample_retries", "min_coverage", "concurrency", "scorer_concurrency",
                "max_consecutive_unmeasured"}
    if body.get("kind") == "evaluation" and settings.intersection(body):
        schema = "lab-service-project-prepare-request/3"
        # Keep the CLI's insertion order when filling in its nullable settings.
        body = {**dict.fromkeys(settings | {"split"}), **body}
    else:
        schema = "lab-service-project-prepare-request/2" if "split" in body else "lab-service-project-prepare-request/1"
    payload = {"schema": schema, "implementation": implementation, **body}
    return Request("POST", _project(project_id, "prepared-executions"), "lab-project-prepared-result/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_target", effect=Effect.MUTATION, schema="lab-project-target-result/1")
def project_target(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None,
    bundle_digest: str, secrets: dict,
) -> Request:
    payload = {"schema": "lab-service-project-target-request/1", "implementation": implementation,
               "bundle_digest": bundle_digest, "secrets": secrets}
    return Request("POST", _project(project_id, "targets"), "lab-project-target-result/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_sync_check", effect=Effect.MUTATION, schema="lab-project-sync-check/1")
def project_sync_check(
    request_id: str | None, owner_generation: str | None, project_id: str, rev: str | None = None,
) -> Request:
    payload = {"schema": "lab-service-project-sync-check-request/1", "rev": rev}
    return Request("POST", _project(project_id, "sync", "check"), "lab-project-sync-check/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_sync", effect=Effect.MUTATION, schema="lab-project-sync-result/1", asks=True)
def project_sync(
    request_id: str | None, owner_generation: str | None, project_id: str, upstream: str,
    approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-project-sync-request/1", "upstream": upstream}
    if approval is not None:
        payload.update(schema="lab-service-project-sync-request/2", approval=approval)
    return Request("POST", _project(project_id, "sync"), "lab-project-sync-result/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_start", effect=Effect.MUTATION, schema="lab-project-start-result/5", asks=True)
def project_start(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None,
    prepared_digest: str, target_ref: dict, secrets: dict, approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-project-start-request/1", "implementation": implementation,
               "prepared_digest": prepared_digest, "target_ref": target_ref, "secrets": secrets}
    if approval is not None:
        payload.update(schema="lab-service-project-start-request/2", approval=approval)
    return Request("POST", _project(project_id, "operations"), "lab-project-start-result/5", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_confirm", effect=Effect.MUTATION, schema="lab-project-confirmation/1", asks=True)
def project_confirm(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None,
    claim: str, attests: dict, linked: list[dict], channel: str, author: str, note: str | None,
    approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-confirm-request/1", "implementation": implementation, "claim": claim,
               "attests": attests, "linked": linked, "channel": channel, "author": author, "note": note}
    if approval is not None:
        payload.update(schema="lab-service-confirm-request/2", approval=approval)
    return Request("POST", _project(project_id, "confirmations"), "lab-project-confirmation/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_accept", effect=Effect.MUTATION, schema="lab-project-acceptance/1", asks=True)
def project_accept(
    request_id: str | None, owner_generation: str | None, project_id: str, implementation: str | None,
    only: list[str] | None, across_pairs: bool, reason: str, channel: str, author: str,
    dry_run: bool, expected: list[dict] | None, approval: dict | None = None,
) -> Request:
    """Build the CLI preview or the acceptance commit, which is the asking action."""
    payload = {"schema": "lab-service-accept-request/2", "implementation": implementation, "only": only,
               "across_pairs": across_pairs, "reason": reason, "channel": channel, "author": author,
               "dry_run": dry_run, "expected": expected}
    if approval is not None:
        payload.update(schema="lab-service-accept-request/3", approval=approval)
    return Request("POST", _project(project_id, "acceptances"), "lab-project-acceptance/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("project_revoke_confirmation", effect=Effect.IRREVERSIBLE, schema="lab-project-confirmation/1", asks=True)
def project_revoke_confirmation(
    request_id: str | None, owner_generation: str | None, project_id: str, confirmation_id: str,
    reason: str, approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-revoke-request/1", "reason": reason}
    if approval is not None:
        payload.update(schema="lab-service-revoke-request/2", approval=approval)
    return Request("POST", _project(project_id, "confirmations", confirmation_id, "revoke"),
                   "lab-project-confirmation/1", payload, mutation_key=mutation_key(request_id, owner_generation))


@command("project_revoke_acceptance", effect=Effect.IRREVERSIBLE, schema="lab-project-acceptance/1", asks=True)
def project_revoke_acceptance(
    request_id: str | None, owner_generation: str | None, project_id: str, acceptance_id: str,
    reason: str, approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-revoke-request/1", "reason": reason}
    if approval is not None:
        payload.update(schema="lab-service-revoke-request/2", approval=approval)
    return Request("POST", _project(project_id, "acceptances", acceptance_id, "revoke"),
                   "lab-project-acceptance/1", payload, mutation_key=mutation_key(request_id, owner_generation))
