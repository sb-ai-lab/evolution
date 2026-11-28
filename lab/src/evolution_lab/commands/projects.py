"""Project reads as commands: each builds the request ``lab project`` sends today (ADR-0070).

Project ids, implementation ids and paths are quoted, not validated: the service owns their grammar and refuses a
malformed one, and a local refusal here would change the CLI's behaviour for the same input.
"""
from __future__ import annotations

from evolution_lab.commands._core import Effect, Request, command, segment_path, with_query


def _project(project: str, *segments: str) -> str:
    # An empty id drops its segment, as ``lab project`` always has (``/v1/projects/status`` for ``status ""``).
    return segment_path("v1", "projects", *([project] if project else []), *segments)


@command("projects_list", effect=Effect.READ, schema="lab-project-list/2")
def projects_list(archived: bool = False) -> Request:
    return Request("GET", with_query("/v1/projects", archived="1" if archived else None), "lab-project-list/2")


@command("project_show", effect=Effect.READ, schema="lab-project/2")
def project_show(project: str) -> Request:
    return Request("GET", _project(project), "lab-project/2")


@command("project_status", effect=Effect.READ, schema="lab-project-status/6")
def project_status(project: str, implementation: str | None = None, scenario: str | None = None) -> Request:
    path = with_query(_project(project, "status"), implementation=implementation or None,
                      scenario=scenario or None)
    return Request("GET", path, "lab-project-status/6")


@command("project_facts", effect=Effect.READ, schema="lab-project-facts/5")
def project_facts(project: str, implementation: str | None = None) -> Request:
    return Request("GET", with_query(_project(project, "facts"), implementation=implementation or None),
                   "lab-project-facts/5")


@command("project_approvals", effect=Effect.READ, schema="lab-approval-list/1")
def project_approvals(project_id: str) -> Request:
    return Request("GET", _project(project_id, "approvals"), "lab-approval-list/1")


@command("project_confirmations", effect=Effect.READ, schema="lab-project-confirmation-list/1")
def project_confirmations(project: str, revoked: bool = False) -> Request:
    return Request("GET", with_query(_project(project, "confirmations"), revoked="1" if revoked else None),
                   "lab-project-confirmation-list/1")


@command("project_tree", effect=Effect.READ, schema="lab-project-tree/1")
def project_tree(project: str, implementation: str, path: str, text: bool = False) -> Request:
    query = with_query(_project(project, "tree"), implementation=implementation, path=path,
                       text="1" if text else None)
    return Request("GET", query, "lab-project-tree/1")


@command("project_operations", effect=Effect.READ, schema="lab-project-operation-list/1")
def project_operations(project: str, implementation: str | None = None) -> Request:
    return Request("GET", with_query(_project(project, "operations"), implementation=implementation or None),
                   "lab-project-operation-list/1")


@command("project_operations_summary", effect=Effect.READ, schema="lab-project-operation-list/2")
def project_operations_summary(project: str, implementation: str | None = None) -> Request:
    path = with_query(_project(project, "operations"), implementation=implementation or None, view="summary")
    return Request("GET", path, "lab-project-operation-list/2")
