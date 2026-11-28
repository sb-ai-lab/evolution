"""``lab project``: thin CLI over the project routes (spec §4.3, service Task 9).

The dispatcher in ``parser.py`` supplies the local input readers and the target
reference for ``start``. It resolves the mutation key once the request has been
built, then performs the HTTP call and writes output.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Callable
from urllib.parse import quote

from evolution_lab.commands import Request, project_mutations, projects
from evolution_lab.projects.clone import is_url

from .errors import UsageError


def parse_impl(spec: str) -> dict:
    head, *options = spec.split(",")
    if "=" not in head:
        raise UsageError("--impl expects id=place[,package=…][,name=…][,origin=…]")
    item_id, place = head.split("=", 1)
    value = {"id": item_id, "place": place}
    for option in options:
        key, _, raw = option.partition("=")
        if key == "package":
            value["package_entry"] = raw
        elif key == "name":
            value["name"] = raw
        elif key == "origin":
            if raw.startswith("ported_from:"):
                value["origin"] = {"kind": "ported_from", "implementation": raw.split(":", 1)[1]}
            elif raw in ("connected", "created"):
                value["origin"] = {"kind": raw}
            else:
                raise UsageError("origin is connected, created or ported_from:<id>")
        else:
            raise UsageError(f"unknown --impl option {key}")
    return value


def add_parser(commands) -> None:
    project = commands.add_parser("project").add_subparsers(dest="project_command", required=True)
    create = project.add_parser("create")
    create.add_argument("id", nargs="?")
    create.add_argument("--name", required=True)
    where = create.add_mutually_exclusive_group(required=True)
    where.add_argument("--path")
    where.add_argument("--clone")
    create.add_argument("--rev")
    create.add_argument("--root", required=True)
    create.add_argument("--impl", action="append", default=[], type=parse_impl)
    create.add_argument("--exclude", action="append", default=[])
    demo = project.add_parser("demo")
    demo.add_argument("id", nargs="?")
    listing = project.add_parser("list")
    listing.add_argument("--archived", action="store_true")
    for name in ("show", "archive"):
        sub = project.add_parser(name)
        sub.add_argument("project")
        if name == "archive":
            sub.add_argument("--reason")
    open_ = project.add_parser("open")
    open_.add_argument("project")
    open_.add_argument("--print", action="store_true", dest="print_agents")
    open_.add_argument("--autonomy", choices=("ask", "yolo"), default="ask")
    rename = project.add_parser("rename")
    rename.add_argument("project"); rename.add_argument("name")
    setter = project.add_parser("set")
    setter.add_argument("project")
    choice = setter.add_mutually_exclusive_group(required=True)
    choice.add_argument("--root"); choice.add_argument("--working-copy"); choice.add_argument("--exclude", nargs="+")
    impl = project.add_parser("impl").add_subparsers(dest="impl_command", required=True)
    impl_add = impl.add_parser("add"); impl_add.add_argument("project"); impl_add.add_argument("spec", type=parse_impl)
    impl_rename = impl.add_parser("rename"); impl_rename.add_argument("project"); impl_rename.add_argument("implementation"); impl_rename.add_argument("name")
    impl_move = impl.add_parser("move"); impl_move.add_argument("project"); impl_move.add_argument("implementation"); impl_move.add_argument("place")
    impl_package = impl.add_parser("set-package"); impl_package.add_argument("project"); impl_package.add_argument("implementation")
    impl_package.add_argument("package_entry", nargs="?"); impl_package.add_argument("--none", action="store_true")
    impl_archive = impl.add_parser("archive"); impl_archive.add_argument("project"); impl_archive.add_argument("implementation"); impl_archive.add_argument("--reason")
    for name in ("bundle", "operations", "facts"):
        sub = project.add_parser(name); sub.add_argument("project"); sub.add_argument("--implementation")
        if name == "operations":
            sub.add_argument("--summary", action="store_true")
    tree = project.add_parser("tree"); tree.add_argument("project")
    tree.add_argument("--implementation", required=True); tree.add_argument("--path", required=True)
    tree.add_argument("--text", action="store_true"); tree.add_argument("--json", action="store_true", dest="tree_json")
    status = project.add_parser("status"); status.add_argument("project"); status.add_argument("--implementation")
    status.add_argument("--scenario"); status.add_argument("--json", action="store_true", dest="status_json")
    prepare = project.add_parser("prepare"); prepare.add_argument("project"); prepare.add_argument("--request", required=True); prepare.add_argument("--implementation")
    target = project.add_parser("target"); target.add_argument("project"); target.add_argument("--bundle", required=True)
    target.add_argument("--secrets-file"); target.add_argument("--implementation")
    start = project.add_parser("start"); start.add_argument("project"); start.add_argument("--prepared", required=True)
    start.add_argument("--target", required=True); start.add_argument("--secrets-file"); start.add_argument("--implementation")
    confirm = project.add_parser("confirm"); confirm.add_argument("project"); confirm.add_argument("--claim", required=True)
    confirm.add_argument("--path", required=True); confirm.add_argument("--link", action="append", default=[])
    confirm.add_argument("--digest"); confirm.add_argument("--channel", default="cli"); confirm.add_argument("--author", required=True)
    confirm.add_argument("--note"); confirm.add_argument("--implementation")
    confirmations = project.add_parser("confirmations"); confirmations.add_argument("project"); confirmations.add_argument("--revoked", action="store_true")
    for name in ("revoke-confirmation", "revoke-acceptance"):
        sub = project.add_parser(name); sub.add_argument("project"); sub.add_argument("record"); sub.add_argument("--reason", required=True)
    accept = project.add_parser("accept"); accept.add_argument("project"); accept.add_argument("--reason", required=True)
    accept.add_argument("--only", action="append"); accept.add_argument("--across-pairs", action="store_true")
    accept.add_argument("--yes", action="store_true"); accept.add_argument("--author", default="cli")
    accept.add_argument("--channel", default="cli"); accept.add_argument("--implementation")
    port = project.add_parser("port"); port.add_argument("project"); port.add_argument("--from", required=True, dest="source")
    port.add_argument("--template", required=True); port.add_argument("--id", dest="port_id"); port.add_argument("--into")
    sync = project.add_parser("sync"); sync.add_argument("project")
    sync_mode = sync.add_mutually_exclusive_group()
    sync_mode.add_argument("--rev"); sync_mode.add_argument("--apply", dest="upstream")

    # Keep the existing project help synopsis while adding an approval read.
    project.metavar = "{" + ",".join(project.choices) + "}"
    approvals = project.add_parser("approvals")
    approvals.add_argument("project")
    approvals.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)


MUTATIONS = frozenset({"create", "rename", "set", "impl", "archive", "bundle", "prepare", "target", "start",
                       "confirm", "revoke-confirmation", "accept", "revoke-acceptance", "open", "demo", "port", "sync"})


def _link(value: str) -> dict:
    kind, _, ref = value.partition(":")
    if not ref:
        raise UsageError("--link expects KIND:REF")
    return {"kind": kind, "ref": ref}


def _local_path(value: str) -> str:
    """The service resolves paths in its own directory, not the caller's: a relative
    ``--path``/``--working-copy`` is made absolute here (final review M1), with ``..``
    normalised so one place has one spelling (R-d). An empty value stays empty (the service
    refuses it) instead of becoming the current directory."""
    return os.path.abspath(value) if value else value


def _clone_source(value: str) -> str:
    """A ``--clone`` source is local by its syntax, as git reads it, not by what is on disk:
    no ``scheme://`` and not scp-like ``host:path`` (a ``:`` with no ``/`` before it). A local
    source (a directory, a bundle file, ...) is sent absolute; a remote one as given (R-d)."""
    return value if not value or is_url(value) else _local_path(value)


def split_template(value: str) -> tuple[str, str | None]:
    """``<src>[@<ref>]`` (spec 2026-10-01 §3): the ref follows the last ``@`` after the source's path begins, so
    ``user@host`` and URL credentials are never read as a ref. A local path whose name holds ``@`` names its ref
    explicitly (``…@HEAD``). No ref: ``None``, and the service takes ``HEAD``."""
    start = 0
    if is_url(value):
        if "://" in value:
            slash = value.find("/", value.index("://") + 3)
            start = len(value) if slash < 0 else slash
        else:  # scp-like host:path
            start = value.index(":") + 1
    at = value.rfind("@")
    if start < at < len(value) - 1:
        return value[:at], value[at + 1:]
    return value, None


def _segment(value: str) -> str:
    return quote(value, safe="")


def request(
    args,
    read_json: Callable[[str], dict[str, Any]],
    read_secrets: Callable[[str | None], dict[str, str]],
    target_ref: Callable[[str], dict[str, Any]],
    *,
    expected: list[dict] | None = None,
) -> Request:
    """Build one project request before mutation-key resolution."""
    c, pid = args.project_command, getattr(args, "project", None)
    base = f"/v1/projects/{_segment(pid)}" if pid else "/v1/projects"

    def update(mutation):
        return Request("PATCH", base, "lab-project/2", {
            "schema": "lab-service-project-update/1", "mutation": mutation, "expected_record_version": None})
    if c == "create":
        source = ({"kind": "path", "path": _local_path(args.path)} if args.path
                  else {"kind": "clone", "repo": _clone_source(args.clone), "rev": args.rev})
        return Request("POST", "/v1/projects", "lab-project/2", {
            "schema": "lab-service-project-create/1", "id": args.id, "name": args.name,
            "materials_root": args.root, "implementations": args.impl, "source": source, "exclude": args.exclude})
    if c == "demo":
        # `demo` has an `id` attribute, not `project`: `base` above stays unused for it.
        return Request("POST", "/v1/projects/demo", "lab-project/2",
                       {"schema": "lab-service-project-demo-request/1", "id": args.id})
    if c == "list":
        return projects.projects_list(archived=args.archived)
    if c == "show":
        return projects.project_show(pid)
    if c == "rename":
        return update({"op": "rename", "name": args.name})
    if c == "set":
        if args.root:
            return update({"op": "set_root", "materials_root": args.root})
        if args.working_copy:
            return update({"op": "set_working_copy", "path": _local_path(args.working_copy)})
        return update({"op": "set_exclude", "exclude": args.exclude})
    if c == "archive":
        return update({"op": "archive", "reason": args.reason})
    if c == "open":
        return Request("POST", base + "/workspace", "lab-project-workspace/1",
                       {"schema": "lab-service-project-workspace-request/1", "autonomy": args.autonomy})
    if c == "impl":
        i = args.impl_command
        if i == "add":
            return update({"op": "add_implementation", "implementation": args.spec})
        if i == "rename":
            return update({"op": "rename_implementation", "implementation": args.implementation, "name": args.name})
        if i == "move":
            return update({"op": "move_implementation", "implementation": args.implementation, "place": args.place})
        if i == "set-package":
            if args.none == (args.package_entry is not None):
                raise UsageError("give a package entry or --none")
            return update({"op": "set_package", "implementation": args.implementation, "package_entry": args.package_entry})
        return update({"op": "archive_implementation", "implementation": args.implementation, "reason": args.reason})
    if getattr(args, "implementation", None):
        # Every later sub-command refuses an unencodable value here, before a request file or the service (PDR-0055).
        quote(args.implementation, safe='')
    if c == "tree":
        return projects.project_tree(pid, args.implementation, args.path, text=args.text)
    if c == "bundle":
        return project_mutations.project_bundle(None, None, pid, args.implementation)
    if c == "prepare":
        body = read_json(args.request)
        if "schema" in body:
            raise UsageError("request file must not contain schema")
        return project_mutations.project_prepare(None, None, pid, args.implementation, body)
    if c == "target":
        return project_mutations.project_target(
            None, None, pid, args.implementation, args.bundle, read_secrets(args.secrets_file))
    if c == "start":
        return project_mutations.project_start(
            None, None, pid, args.implementation, args.prepared, target_ref(args.target),
            read_secrets(args.secrets_file))
    if c == "operations":
        build = projects.project_operations_summary if args.summary else projects.project_operations
        return build(pid, args.implementation)
    if c == "facts":
        return projects.project_facts(pid, args.implementation)
    if c == "status":
        return projects.project_status(pid, args.implementation, args.scenario)
    if c == "confirm":
        attests = {"path": args.path, **({"digest": args.digest} if args.digest else {})}
        return project_mutations.project_confirm(
            None, None, pid, args.implementation, args.claim, attests, [_link(value) for value in args.link],
            args.channel, args.author, args.note)
    if c == "approvals":
        return projects.project_approvals(pid)
    if c == "confirmations":
        return projects.project_confirmations(pid, revoked=args.revoked)
    if c == "revoke-confirmation":
        return project_mutations.project_revoke_confirmation(None, None, pid, args.record, args.reason)
    if c == "revoke-acceptance":
        return project_mutations.project_revoke_acceptance(None, None, pid, args.record, args.reason)
    if c == "accept":
        return project_mutations.project_accept(
            None, None, pid, args.implementation, args.only, args.across_pairs, args.reason, args.channel, args.author,
            dry_run=not args.yes and expected is None, expected=expected)
    if c == "port":
        template, ref = split_template(args.template)
        return Request("POST", base + "/ports", "lab-project/2", {
            "schema": "lab-service-project-port-request/1", "from": args.source, "template": _clone_source(template),
            "ref": ref, "id": args.port_id, "into": None if args.into is None else _local_path(args.into)})
    if c == "sync":
        if args.upstream is not None:
            return project_mutations.project_sync(None, None, pid, args.upstream)
        return project_mutations.project_sync_check(None, None, pid, args.rev)
    raise UsageError("unknown project command")
