"""Project reads as commands: each equals what ``cli/project.py:request`` builds for the same argv."""
from __future__ import annotations

import pytest

from evolution_lab.cli import project
from evolution_lab.cli.parser import build_parser
from evolution_lab.commands import REGISTRY, Effect

CASES = [
    (["list"], "projects_list", {}),
    (["list", "--archived"], "projects_list", {"archived": True}),
    (["show", "p q"], "project_show", {"project": "p q"}),
    (["operations", "a@b"], "project_operations", {"project": "a@b"}),
    (["operations", "p", "--implementation", "i x"], "project_operations", {"project": "p", "implementation": "i x"}),
    (["operations", "p", "--summary"], "project_operations_summary", {"project": "p"}),
    (["operations", "p", "--implementation", "i", "--summary"], "project_operations_summary",
     {"project": "p", "implementation": "i"}),
    (["facts", "p"], "project_facts", {"project": "p"}),
    (["facts", "p", "--implementation", "i/j"], "project_facts", {"project": "p", "implementation": "i/j"}),
    (["status", "p"], "project_status", {"project": "p"}),
    (["status", "p", "--implementation", "i"], "project_status", {"project": "p", "implementation": "i"}),
    (["status", "p", "--scenario", "connect"], "project_status", {"project": "p", "scenario": "connect"}),
    (["status", "p", "--implementation", "i", "--scenario", "connect"], "project_status",
     {"project": "p", "implementation": "i", "scenario": "connect"}),
    (["approvals", "p"], "project_approvals", {"project_id": "p"}),
    (["confirmations", "p"], "project_confirmations", {"project": "p"}),
    (["confirmations", "p", "--revoked"], "project_confirmations", {"project": "p", "revoked": True}),
    (["tree", "p", "--implementation", "i", "--path", "a/b"], "project_tree",
     {"project": "p", "implementation": "i", "path": "a/b"}),
    (["tree", "p", "--implementation", "i", "--path", "a/b c", "--text"], "project_tree",
     {"project": "p", "implementation": "i", "path": "a/b c", "text": True}),
]


@pytest.mark.parametrize(("argv", "name", "kwargs"), CASES)
def test_project_reads_match_cli_project_request(argv, name, kwargs):
    args = build_parser().parse_args(["project", *argv])
    cli_request = project.request(args, None, None, None)
    request = REGISTRY[name].build(**kwargs)
    assert request == cli_request
    assert request.payload is None and REGISTRY[name].effect is Effect.READ


def test_project_reads_are_registered_with_their_schema():
    names = {name for _, name, _ in CASES}
    assert len(names) == 9
    for name in names:
        assert REGISTRY[name].needs_service and REGISTRY[name].effect is Effect.READ and REGISTRY[name].schema


@pytest.mark.parametrize(("argv", "name", "kwargs"), [
    (["show", ""], "project_show", {"project": ""}),
    (["status", ""], "project_status", {"project": ""}),
    (["facts", ""], "project_facts", {"project": ""}),
    (["tree", "", "--implementation", "i", "--path", "a"], "project_tree",
     {"project": "", "implementation": "i", "path": "a"}),
])
def test_empty_project_reproduces_the_old_cli_request(argv, name, kwargs):
    args = build_parser().parse_args(["project", *argv])
    cli_request = project.request(args, None, None, None)
    request = REGISTRY[name].build(**kwargs)
    assert request == cli_request
    assert request.path.startswith("/v1/projects")


@pytest.mark.parametrize(("name", "kwargs", "path"), [
    ("projects_list", {"archived": True}, "/v1/projects?archived=1"),
    ("project_confirmations", {"project": "p", "revoked": True}, "/v1/projects/p/confirmations?revoked=1"),
    ("project_tree", {"project": "p", "implementation": "i", "path": "a/b c", "text": True},
     "/v1/projects/p/tree?implementation=i&path=a%2Fb%20c&text=1"),
    ("project_show", {"project": "p q"}, "/v1/projects/p%20q"),
])
def test_literal_project_paths_as_the_old_cli_built_them(name, kwargs, path):
    request = REGISTRY[name].build(**kwargs)
    assert (request.method, request.path) == ("GET", path)


def test_project_command_schema_equals_the_request_schema():
    for _, name, kwargs in CASES:
        assert REGISTRY[name].schema == REGISTRY[name].build(**kwargs).schema
