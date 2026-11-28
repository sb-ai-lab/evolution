"""Argument parsing and explicit command-to-route mapping for ``lab``."""
from __future__ import annotations

import argparse
from dataclasses import replace
from functools import partial
import math
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Callable, TextIO
from urllib.parse import quote

from evolution_lab import commands
from evolution_lab.commands import mutations
from evolution_lab.durability import DurabilityConfigError, check
from evolution_lab.projects.endpoint import resolve_socket
from evolution_lab._ownership import operator_line, select_mode
from evolution_lab.service.codecs import (
    PREPARE_REQUEST_SCHEMA, decode_bundle_request, decode_digest, decode_identifier, decode_measurement_id,
    decode_core_id, decode_attempt_id, decode_reflection_id,
    decode_json_object, decode_prepare_request, decode_resource_ref, decode_runtime_policy,
    decode_policy_confirm_request,
    decode_secret_map, decode_target_query, decode_target_ref, decode_task_query,
)
from evolution_lab.service.mutations import REQUEST_ID

from .client import (AcceptanceUnknown, ApiError, ClientError, RequestInterrupted,
                     RequestTimeout, ServiceClient)
from .download import publish_bytes, publish_export
from .errors import UsageError
from .output import local_error, write_json, write_value
from .panel import add_parser as add_panel_parser
from .probe import MUTATIONS as PROBE_MUTATIONS, add_parser as add_probe_parser, check as probe_check, request as probe_request
from .project import MUTATIONS as PROJECT_MUTATIONS, add_parser as add_project_parser, request as project_request

#: `_request_file` picks the prepare request version by the file's keys.
PREPARE_REQUEST_PREFIX = "lab-service-prepare-request"


class PolicyRequestInvalid(ValueError):
    def __init__(self, pointer: str):
        self.pointer = pointer


def _parse_policy_document(document):
    from evolution_lab.execution_policy import PolicyInvalid, parse_policy, safe_policy_pointer
    try:
        return parse_policy(document)
    except PolicyInvalid as error:
        if isinstance(document, dict) and document.get("schema") == "lab-execution-policy/2":
            raise PolicyRequestInvalid(safe_policy_pointer(error.path)) from None
        raise


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)

    def _print_message(self, message, file=None):
        return super()._print_message(message, getattr(self, "_output", None) if file is None else file)

    def print_help(self, file=None):
        self._print_message(self.format_help(), file or getattr(self, "_output", sys.stdout))


def _absolute(value: str) -> str:
    if not Path(value).is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return value


def _positive(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _interval(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer of at least 1") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be an integer of at least 1")
    return number


def _wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true")


_COMMAND_GROUPS = (
    ('Start here', ('new-project', 'evaluate', 'campaign', 'trace')),
    ('Run step by step', ('bundle', 'prepare', 'prepared', 'preflight', 'connect', 'check', 'conformance', 'start', 'resume', 'stop')),
    ('Follow and inspect', ('list', 'status', 'events', 'watch', 'report', 'view', 'suspensions', 'sample', 'attempt', 'reflection', 'measurements', 'diff')),
    ('Results', ('export', 'export-content', 'delete-results')),
    ('Agent processes and policies', ('agent', 'exec-policy', 'routing', 'probe')),
    ('Projects', ('project',)),
    ('Service and maintenance', ('serve', 'mcp', 'health', 'panel', 'job', 'cleanup', 'release-operation', 'guides', 'scenarios')),
)


def _command_help() -> str:
    import textwrap
    groups = [heading + ':\n' + textwrap.fill(', '.join(names), width=78,
              initial_indent='  ', subsequent_indent='  ') for heading, names in _COMMAND_GROUPS]
    groups.append('Old names:\n  target → agent; policy → exec-policy; operation routing → routing;\n'
                  '  task → job; release operation ID → release-operation ID;\n'
                  '  target probe → probe; campaign OPERATION → view OPERATION.')
    return '\n\n'.join(groups)


def _named_command(commands, primary, old):
    """One argument parser and existing handler for both public names."""
    command = commands.add_parser(primary)
    command.set_defaults(command=old)
    commands.choices[old] = command
    return command


def build_parser() -> Parser:
    parser = Parser(prog="lab", add_help=True, formatter_class=argparse.RawDescriptionHelpFormatter,
                    usage="%(prog)s [options] COMMAND ...", epilog=_command_help())
    parser.add_argument("--socket", type=_absolute)
    parser.add_argument("--home", type=_absolute)
    parser.add_argument("--json", action="store_true", dest="machine")
    parser.add_argument("--timeout", type=_positive)
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--request-id")
    key.add_argument("--no-request-id", action="store_true")
    parser.add_argument("--generation", dest="request_generation")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND", help=argparse.SUPPRESS)

    preflight = commands.add_parser("preflight", description="Inspect the context budget of a prepared /16 campaign offline.")
    preflight.add_argument("prepared", help="prepared directory under STATE/prepared, or a digest with --state")
    preflight.add_argument("--state", type=Path, help="Lab state directory containing the prepared campaign")
    preflight.add_argument("--operation", help="read compatible recorded role turns from this operation")
    preflight.add_argument("--trace", type=Path, help="compatible local trace export")
    preflight.add_argument("--baseline", type=Path, help="compatible saved training evaluation")
    preflight.add_argument("--scenario", type=Path, help="JSON with hypothetical route, styles and token window")
    preflight.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)

    approvals = commands.add_parser("approvals")
    approval_filter = approvals.add_mutually_exclusive_group(required=True)
    approval_filter.add_argument("--operation", default=argparse.SUPPRESS)
    approval_filter.add_argument("--policy", dest="digest")
    approvals.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)

    mcp = commands.add_parser("mcp")
    mcp.add_argument("--socket", type=_absolute, default=argparse.SUPPRESS)
    mcp.add_argument("--home", type=_absolute, default=argparse.SUPPRESS)
    mcp.add_argument("--timeout", type=_positive, default=argparse.SUPPRESS)
    mcp.add_argument("--autonomy", choices=("ask", "yolo"), default="ask")
    mcp.add_argument("--approval-timeout", type=_positive, default=600.0, metavar="SECONDS")
    serve = commands.add_parser("serve")
    serve.add_argument("--socket", type=_absolute, default=argparse.SUPPRESS)
    serve.add_argument("--state", required=True, type=_absolute)
    serve.add_argument("--shutdown-wait-seconds", type=_positive, default=30)
    serve.add_argument(
        "--durability", choices=("strict", "relaxed"), default=None,
        help="Process durability (default: EVOLUTION_DURABILITY, else relaxed)",
    )
    serve.add_argument(
        "--scratch", type=_absolute,
        help="absolute private root for temporary target/snapshot/staging files (default: existing paths)",
    )
    serve.add_argument("--execution-wheel", action="append", type=_absolute,
        default=[], dest="execution_wheels")
    serve.add_argument("--execution-runtime-file", action="append", type=_absolute,
        default=[], dest="execution_runtime_files")
    serve.add_argument("--home", type=_absolute, default=argparse.SUPPRESS)

    trace = commands.add_parser('trace')
    trace.add_argument('operation'); trace.add_argument('selected_operation', nargs='?')
    trace.add_argument('--out', type=Path, required=True)
    trace.add_argument('--disclose-private', action='store_true', required=True)
    trace.add_argument('--measurement', action='append', default=[], dest='measurements')
    trace.add_argument('--max-field-bytes', type=int, default=65536)
    trace.add_argument('--json', action='store_true', dest='machine', default=argparse.SUPPRESS)
    _secrets(trace)

    bundle = commands.add_parser("bundle").add_subparsers(dest="bundle_command", required=True)
    bundle_create = bundle.add_parser("create"); bundle_create.add_argument("--request", required=True, type=_request_path)
    bundle_show = bundle.add_parser("show"); bundle_show.add_argument("digest")
    bundle_measurements = bundle.add_parser("measurements"); bundle_measurements.add_argument("digest")
    prepare = commands.add_parser("prepare"); prepare.add_argument("--request", required=True, type=_request_path)
    prepared = commands.add_parser("prepared").add_subparsers(dest="prepared_command", required=True)
    prepared_show = prepared.add_parser("show"); prepared_show.add_argument("digest")
    connect = commands.add_parser("connect"); connect.add_argument("bundle_digest")

    policy = _named_command(commands, "exec-policy", "policy").add_subparsers(dest="policy_command", required=True)
    policy_confirm = policy.add_parser("confirm")
    policy_confirm.add_argument("file")
    policy_confirm.add_argument("--expect", required=True)
    policy_confirm.add_argument("--author", required=True)
    policy_confirm.add_argument("--channel", required=True)
    policy_confirm.add_argument("--note")
    policy.add_parser("list")
    policy_show = policy.add_parser("show"); policy_show.add_argument("digest")
    policy_digest_command = policy.add_parser("digest"); policy_digest_command.add_argument("file")

    routing = commands.add_parser('routing')
    routing.add_argument('operation')
    routing.add_argument('--json', action='store_true', dest='machine', default=argparse.SUPPRESS)
    routing.set_defaults(command='operation', operation_command='routing')

    operation = commands.add_parser("operation").add_subparsers(dest="operation_command", required=True)
    operation_routing = operation.add_parser("routing")
    operation_routing.add_argument("operation")
    operation_routing.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)

    target = _named_command(commands, "agent", "target").add_subparsers(dest="target_command", required=True)
    target_start = target.add_parser("start"); target_start.add_argument("connection"); _secrets(target_start); _wait(target_start); target_start.add_argument("--execution-policy")
    add_probe_parser(commands)
    probe = commands.choices['probe']
    probe.set_defaults(command='target', target_command='probe')
    target.choices['probe'] = probe
    target_list = target.add_parser("list"); target_list.add_argument("--bundle"); target_list.add_argument("--ready", action="store_true")
    for name in ("status", "stop"):
        item = target.add_parser(name); item.add_argument("id"); item.add_argument("--generation", required=True)
        if name == "stop": _wait(item)
    check = commands.add_parser("check"); check.add_argument("connection"); _target_ref(check); _wait(check)
    conf = commands.add_parser("conformance"); conf.add_argument("connection"); _target_ref(conf); conf.add_argument("--runtime-policy", type=_absolute); _wait(conf)
    task = _named_command(commands, "job", "task").add_subparsers(dest="task_command", required=True)
    task_status = task.add_parser("status"); task_status.add_argument("id"); task_status.add_argument("--generation", required=True)
    task_stop = task.add_parser("stop"); task_stop.add_argument("id")
    start = commands.add_parser("start"); start.add_argument("prepared"); _target_ref(start); _secrets(start)
    start_condition = start.add_mutually_exclusive_group()
    start_condition.add_argument("--linked-from")
    start_condition.add_argument("--baseline-from", metavar="OPERATION")
    start.add_argument('--baseline', choices=('inherit', 'fresh'))
    start.add_argument('--seed', choices=('champion', 'project'))
    resume = commands.add_parser("resume"); resume.add_argument("operation"); _target_ref(resume); _secrets(resume)
    resume.add_argument('--accept', action='append', default=[]); resume.add_argument('--effect')
    resume.add_argument('--reason'); resume.add_argument('--prepared')
    listing = commands.add_parser("list"); listing.add_argument("--summary", action="store_true")
    for name in ("status", "events", "stop", "report", "campaign", "view", "suspensions", "diff", "delete-results", "export"):
        item = commands.add_parser(name); item.add_argument("operation")
        if name == "campaign":
            follow = item.add_mutually_exclusive_group()
            follow.add_argument('--continue', action='store_true', dest='continue_run')
            follow.add_argument('--from', dest='linked_from')
            follow.add_argument('--baseline-from', metavar='RUN')
            item.add_argument('--accept', action='append', default=[]); item.add_argument('--effect')
            item.add_argument('--reason')
            inputs = item.add_mutually_exclusive_group()
            inputs.add_argument('--prepared')
            inputs.add_argument('--reprepare', action='store_true')
            item.add_argument('--baseline', choices=('inherit', 'fresh'))
            item.add_argument('--seed', choices=('champion', 'project'), default='champion')
            item.add_argument("--wheel", action="append", default=[], dest="wheels", type=Path)
            item.add_argument("--python", type=Path)
            item.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)
        if name in {"delete-results", "export"}: _wait(item)
        if name == "suspensions":
            item.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)
        if name == "report":
            item.add_argument("--markdown", action="store_true"); item.add_argument("--output", type=_absolute)
        if name == "diff":
            item.add_argument("--from", dest="tree_from"); item.add_argument("--to", dest="tree_to")
    watch = commands.add_parser("watch"); watch.add_argument("operation")
    watch.add_argument("--interval", type=_interval, default=10)
    sample = commands.add_parser("sample"); sample.add_argument("operation"); sample.add_argument("measurement")
    sample.add_argument("case"); sample.add_argument("sample"); sample.add_argument("--disclose-private", action="store_true")
    attempt = commands.add_parser("attempt"); attempt.add_argument("operation"); attempt.add_argument("attempt")
    attempt.add_argument("--disclose-private", action="store_true")
    reflection = commands.add_parser("reflection"); reflection.add_argument("operation"); reflection.add_argument("reflection")
    reflection.add_argument("--disclose-private", action="store_true")
    measurements = commands.add_parser("measurements"); measurements.add_argument("operation"); measurements.add_argument("measurement", nargs="?")
    content = commands.add_parser("export-content"); content.add_argument("operation"); content.add_argument("export_id"); content.add_argument("--output", required=True, type=_absolute)
    cleanup = commands.add_parser("cleanup"); cleanup.add_argument("kind"); cleanup.add_argument("id"); cleanup.add_argument("--wait-seconds", type=_positive); _wait(cleanup)
    release_operation = commands.add_parser('release-operation')
    release_operation.add_argument('id'); release_operation.add_argument('--reason', required=True)
    release_operation.set_defaults(command='release', kind='operation')
    release = commands.add_parser("release"); release.add_argument("kind"); release.add_argument("id"); release.add_argument("--reason", required=True)
    commands.add_parser("health")
    new_project = commands.add_parser("new-project"); new_project.add_argument("directory", metavar="DIR")
    evaluate = commands.add_parser("evaluate"); evaluate.add_argument("directory", metavar="DIR")
    evaluate.add_argument("--wheel", action="append", default=[], dest="wheels", metavar="PATH")
    evaluate.add_argument("--python", metavar="PATH")
    evaluate.add_argument("--json", action="store_true", dest="machine", default=argparse.SUPPRESS)
    guides = commands.add_parser("guides").add_subparsers(dest="guides_command", required=True)
    guides.add_parser("path")
    guides_list = guides.add_parser("list"); guides_list.add_argument("--json", action="store_true", dest="guides_json")
    guides_req = guides.add_parser("requirements"); guides_req.add_argument("--json", action="store_true", dest="guides_json")
    scenarios = commands.add_parser("scenarios").add_subparsers(dest="scenarios_command", required=True)
    scenarios.add_parser("path")
    scenarios_list = scenarios.add_parser("list"); scenarios_list.add_argument("--json", action="store_true", dest="scenarios_json")
    scenarios_show = scenarios.add_parser("show"); scenarios_show.add_argument("scenario")
    scenarios_show.add_argument("--json", action="store_true", dest="scenarios_json")
    scenarios_schema = scenarios.add_parser("schema"); scenarios_schema.add_argument("scenario")
    scenarios_schema.add_argument("schema")
    add_panel_parser(commands)
    add_project_parser(commands)
    return parser


def _target_ref(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True)
    parser.add_argument("--generation", required=True)


def _request_path(value: str) -> str:
    return value if value == "-" else _absolute(value)


def _secrets(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--secrets-file", type=_absolute)
    group.add_argument("--secrets-fd", type=int)


def _read_json(path: str, stdin) -> dict[str, Any]:
    if path == "-":
        raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read().encode()
    else:
        raw = Path(path).read_bytes()
    return decode_json_object(raw)


def _request_file(path: str, schema: str, stdin) -> dict[str, Any]:
    """``schema`` is a full version, or the prefix ``lab-service-prepare-request``: a prepare request file gets
    ``/5`` when an evaluation names the stop threshold, ``/4`` for earlier settings,
    ``/3`` for ``split``, and ``/2`` otherwise. Missing nullable settings are filled in."""
    value = _read_json(path, stdin)
    if "schema" in value:
        raise UsageError("request file must not contain schema")
    if schema == PREPARE_REQUEST_PREFIX:
        settings = {"sample_retries", "min_coverage", "concurrency", "scorer_concurrency"}
        if value.get("kind") == "evaluation" and "max_consecutive_unmeasured" in value:
            schema = PREPARE_REQUEST_SCHEMA
            value = {**dict.fromkeys(settings | {"split", "max_consecutive_unmeasured"}), **value}
        elif value.get("kind") == "evaluation" and settings.intersection(value):
            schema = PREPARE_REQUEST_PREFIX + "/4"
            value = {**dict.fromkeys(settings | {"split"}), **value}
        else:
            schema = PREPARE_REQUEST_PREFIX + ("/3" if "split" in value else "/2")
    value = {"schema": schema, **value}
    if schema.startswith("lab-service-bundle-request/"):
        decode_bundle_request(value)
    else:
        decode_prepare_request(value)
    return value


def _secrets_value(args, stdin) -> dict[str, str]:
    if args.secrets_file is not None:
        return decode_secret_map(_read_json(args.secrets_file, stdin))
    if args.secrets_fd is not None:
        if args.secrets_fd < 0:
            raise UsageError("secret file descriptor must be non-negative")
        with os.fdopen(os.dup(args.secrets_fd), "rb") as stream:
            return decode_secret_map(decode_json_object(stream.read()))
    return {}


def _secrets_value_from(path: str | None, stdin) -> dict[str, str]:
    """``lab project target|start --secrets-file``: no ``--secrets-fd`` for project commands."""
    return decode_secret_map(_read_json(path, stdin)) if path else {}


def _query(path: str, **values: str) -> str:
    return path + "?" + "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in values.items())


def _path(*segments: str) -> str:
    return "/" + "/".join(quote(segment, safe="") for segment in segments)


def _call(client, method, path, *, payload=None, mutation=False, expected_schema=None, timeout=None, key=None):
    values = {"payload": payload, "mutation": mutation, "expected_schema": expected_schema}
    if timeout is not None:
        values["timeout"] = timeout
    if key is not None:
        values["mutation_key"] = key
    return client.request_json(method, path, **values)


def _read(client, request: commands.Request):
    """Send one command-layer read (ADR-0070) the way this CLI always has."""
    if request.kind != "json":
        return client.get_bytes(request.path, request.content_type)
    return _call(client, request.method, request.path, expected_schema=request.schema)


def _mutate(client, request: commands.Request):
    return _call(client, request.method, request.path, payload=request.payload, mutation=True,
                 expected_schema=request.schema, key=request.mutation_key)


_MUTATIONS = frozenset({
    ("policy", "confirm"),
    ("bundle", "create"), ("prepare", None), ("connect", None), ("target", "start"), ("target", "stop"),
    ("check", None), ("conformance", None), ("task", "stop"), ("start", None), ("resume", None), ("stop", None),
    ("trace", None), ("delete-results", None), ("export", None), ("cleanup", None), ("release", None),
})


def _is_mutation(args) -> bool:
    if getattr(args, "command", None) == "project":
        return args.project_command in PROJECT_MUTATIONS
    if getattr(args, "command", None) == "target" and getattr(args, "target_command", None) == "probe":
        return args.probe_command in PROBE_MUTATIONS
    sub = next((getattr(args, name) for name in ("bundle_command", "prepared_command", "target_command",
                "task_command", "policy_command") if getattr(args, name, None)), None)
    return (getattr(args, "command", None), sub) in _MUTATIONS


def _validate_key_args(args) -> None:
    given = args.request_id is not None or args.request_generation is not None or args.no_request_id
    if args.command == "serve" or not _is_mutation(args):
        if given:
            raise UsageError("request key options apply only to mutating commands")
        return
    if args.no_request_id and args.request_generation is not None:
        raise UsageError("--no-request-id cannot be combined with --generation")
    if (args.request_id is None) != (args.request_generation is None):
        raise UsageError("--request-id and --generation must be given together")
    if args.request_id is not None:
        if REQUEST_ID.fullmatch(args.request_id) is None:
            raise UsageError("invalid request id")
        try:
            decode_identifier(args.request_generation)
        except ValueError as error:
            raise UsageError("invalid generation") from error
    if (args.command == "project" and args.project_command == "accept"
            and not args.yes and args.request_id is not None):
        # _dispatch_project's two-call accept flow (dry_run preview, then the real write)
        # needs two distinct mutation keys; an explicit --request-id would be replayed
        # verbatim on both calls, and the write's body (dry_run: false) differs from the
        # preview's, so the server would refuse it with request_conflict (R18) *after* a
        # successful preview. Refuse up front instead, before any service call.
        raise UsageError("an explicit --request-id needs --yes: the preview and the write are two different requests")


def _mutation_key(args, client) -> tuple[str, str] | None:
    if not _is_mutation(args) or args.no_request_id:
        return None
    if args.request_id is not None:
        return args.request_id, args.request_generation
    health = _call(client, "GET", "/v1/health", expected_schema="lab-service-health/2")
    return "r-" + secrets.token_urlsafe(24), health["owner_generation"]


def _wait_for_task(client, *, timeout: float | None, stdout: TextIO,
                   stderr: TextIO, machine: bool) -> int:
    task = client._accepted_task
    ref = client._accepted_task_ref
    write_value(stdout, ref, machine=machine)
    started = time.monotonic()
    while True:
        if timeout is not None and time.monotonic() - started >= timeout:
            write_json(stderr, local_error("wait_timeout", "The accepted task did not finish before the timeout.",
                acceptance="accepted", task_ref=ref))
            return 124
        try:
            remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            status = _call(client, "GET", _query(f"/v1/tasks/{quote(task.id, safe='')}", owner_generation=task.owner_generation),
                expected_schema="lab-task-status/1", timeout=remaining)
        except RequestTimeout:
            write_json(stderr, local_error("wait_timeout", "The accepted task did not finish before the timeout.",
                acceptance="accepted", task_ref=ref))
            return 124
        except (RequestInterrupted, KeyboardInterrupt):
            write_json(stderr, local_error("wait_interrupted", "Local waiting for the accepted task was interrupted.",
                acceptance="accepted", task_ref=ref))
            return 130
        if status.get("state") in {"succeeded", "failed", "unknown"}:
            write_value(stdout, status, machine=machine)
            persistence = status.get("persistence", {})
            return 0 if status["state"] == "succeeded" and persistence.get("state") != "failed" else 1
        try:
            time.sleep(0.5 if timeout is None else min(0.5, max(0.0, timeout - (time.monotonic() - started))))
        except KeyboardInterrupt:
            write_json(stderr, local_error("wait_interrupted", "Local waiting for the accepted task was interrupted.",
                acceptance="accepted", task_ref=ref))
            return 130


def _validate_args(args) -> None:
    """Reject route identifiers and closed local request shapes before client creation."""
    if args.command == "project":
        # ``lab project`` reuses attribute names (``target``, ``id``, ``prepared``) with
        # different shapes than the general routes (no paired ``--generation`` for
        # ``project start``'s ``--target``); its own identifiers are validated by
        # cli/project.py and, fully, by the closed server-side codecs.
        return
    if args.command == "report":
        if args.output is not None and not args.markdown:
            raise UsageError("--output needs --markdown")
        if args.markdown and args.machine and args.output is None:
            raise UsageError("--markdown prints Markdown, not JSON; add --output to get a JSON result")
    if args.command == "start" and args.baseline_from is not None and (args.baseline, args.seed) != (None, None):
        raise UsageError("--baseline and --seed apply to --linked-from, not --baseline-from")
    if args.command == "watch" and args.machine:
        raise UsageError("lab watch is text-only; use lab events for JSON")
    for name in ("digest", "bundle_digest", "prepared", "export_id"):
        if getattr(args, name, None) is not None:
            decode_digest(getattr(args, name))
    for name in ("connection", "operation", "id", "target"):
        if hasattr(args, name):
            decode_identifier(getattr(args, name))
    if getattr(args, "measurement", None) is not None:
        decode_measurement_id(args.measurement)
    if args.command == "sample":
        decode_core_id(args.case, "case_id")
        decode_core_id(args.sample, "sample_id")
    elif args.command == "attempt":
        decode_attempt_id(args.attempt)
    elif args.command == "reflection":
        decode_reflection_id(args.reflection)
    if hasattr(args, "target"):
        decode_target_ref(_target_reference(args))
    if args.command == "target" and args.target_command == "probe":
        probe_check(args)  # ids are checked by the service; the options only by shape
        return
    if args.command == "target" and args.target_command in {"status", "stop"}:
        decode_target_query(args.id, args.generation)
    if args.command == "task" and args.task_command == "status":
        decode_task_query(args.id, args.generation)
    if args.command in {"cleanup", "release"}:
        decode_resource_ref({"kind": args.kind, "id": args.id})


def _target_reference(args) -> dict[str, str]:
    return {"schema": "lab-target-ref/1", "id": args.target, "owner_generation": args.generation}


def _project_target_ref(client):
    """``lab project start --target ID`` has no paired ``--generation``: the CLI takes the
    live owner generation from ``GET /v1/health``, same source as a freshly generated
    mutation key."""
    def build(target_id: str) -> dict[str, Any]:
        health = _call(client, "GET", "/v1/health", expected_schema="lab-service-health/2")
        return {"schema": "lab-target-ref/1", "id": target_id, "owner_generation": health["owner_generation"]}
    return build


def _project_call(args, client, request: commands.Request):
    # Read local inputs and build the target reference before resolving the mutation key.
    key = args.resolved_key = _mutation_key(args, client)
    if _is_mutation(args):
        return _mutate(client, replace(request, mutation_key=key))
    return _read(client, request)


def _dispatch_project(args, client, *, stdin, stdout, stderr) -> int:
    """``lab project <sub>``: a dedicated flow, kept out of the generic dispatcher below
    because ``accept`` without ``--yes`` needs two calls under two different mutation keys
    (a ``dry_run: true`` preview, then the real submission on ``y``) — every other project
    command needs exactly one."""
    read_json = partial(_read_json, stdin=stdin)
    read_secrets = partial(_secrets_value_from, stdin=stdin)
    target_ref = _project_target_ref(client)
    request = project_request(args, read_json, read_secrets, target_ref)
    if args.project_command == "accept" and not args.yes:
        preview = _project_call(args, client, request)
        write_value(stdout, preview, machine=args.machine)
        stderr.write("apply? [y/N] ")
        stderr.flush()
        if stdin.readline().strip().lower() != "y":
            return 0
        expected = [{key: fact[key] for key in ("id", "from", "to")} for fact in preview["facts"]]
        request = project_request(args, read_json, read_secrets, target_ref, expected=expected)
    value = _project_call(args, client, request)
    if args.project_command == "open" and not args.machine:
        path = Path(value["path"])
        stdout.write((path / "AGENTS.md").read_text(encoding="utf-8") if args.print_agents else f"{path}\n")
        return 0
    write_value(stdout, value, machine=args.machine or getattr(args, "status_json", False) or getattr(args, "tree_json", False))
    return 0


def _dispatch_probe(args, client, *, stdin, stdout, stderr) -> int:
    """``lab probe <sub>``: one call. ``start|say|reset|close`` carry a mutation key, ``history|list`` none."""
    method, path, payload, schema, timeout = probe_request(args, stdin.read, lambda text: stderr.write(text + "\n"))
    mutation = _is_mutation(args)
    key = args.resolved_key = _mutation_key(args, client)
    value = _call(client, method, path, payload=payload, mutation=mutation, expected_schema=schema,
                  timeout=timeout, key=key)
    write_value(stdout, value, machine=args.machine)
    return 0


def _dispatch(args, client, *, stdin, stdout, stderr) -> int:
    command = args.command
    if command == "project":
        return _dispatch_project(args, client, stdin=stdin, stdout=stdout, stderr=stderr)
    if command == "target" and args.target_command == "probe":
        return _dispatch_probe(args, client, stdin=stdin, stdout=stdout, stderr=stderr)
    # Local inputs are read and validated before the key (which may GET /v1/health),
    # so their errors keep S5 precedence over transport failures.
    request_body = None
    if command == "bundle" and args.bundle_command == "create":
        request_body = _request_file(args.request, "lab-service-bundle-request/2", stdin)
    elif command == "prepare":
        request_body = _request_file(args.request, PREPARE_REQUEST_PREFIX, stdin)
    elif command == "policy" and args.policy_command == "confirm":
        document = _read_json(args.file, stdin)
        _parse_policy_document(document)
        policy_request = mutations.policy_confirm(
            None, None, document, args.expect, args.author, args.channel, args.note)
        decode_policy_confirm_request(policy_request.payload)
    secrets_value = _secrets_value(args, stdin) if command in {"start", "resume", "trace"} or (
        command == "target" and args.target_command == "start") else None
    policy = decode_runtime_policy(_read_json(args.runtime_policy, stdin)).to_payload() if (
        command == "conformance" and args.runtime_policy is not None) else None
    from .acceptance import acceptances
    accepted = acceptances(args.accept, args.effect) if command == 'resume' else ()
    if accepted and (args.reason is None or not args.reason.strip()):
        raise UsageError('--accept requires --reason')
    if any(item.effect is None for item in accepted):
        record = _call(client, 'GET', _path('v1', 'operations', args.operation, 'refusal'),
                       expected_schema='lab-operation-refusal/1')
        from evolution_lab._manifest_schema import _printable
        for item in accepted:
            if item.effect is not None:
                continue
            finding = next((f for f in record['findings']
                            if f['code'] == item.code and f['instance_digest'] == item.digest), None)
            if finding is not None and finding['effects']:
                stderr.write(_printable(f"authorization {item.code}:{item.digest}: default means {finding['effects'][0]}") + '\n')
    key = args.resolved_key = _mutation_key(args, client)
    key_fields = key if key is not None else (None, None)
    if command == "policy":
        if args.policy_command == "confirm":
            value = _mutate(client, replace(policy_request, mutation_key=key))
        elif args.policy_command == "list":
            value = _read(client, commands.resources.execution_policies_list())
        else:
            value = _read(client, commands.resources.execution_policy_show(args.digest))
        write_json(stdout, value)
        return 0
    elif command == "bundle":
        if args.bundle_command == "create":
            value = _mutate(client, mutations.bundle_create(*key_fields, request_body))
        elif args.bundle_command == "measurements":
            value = _read(client, commands.resources.bundle_measurements(args.digest))
        else: value = _read(client, commands.resources.bundle_show(args.digest))
    elif command == "prepare":
        value = _mutate(client, mutations.prepare(*key_fields, request_body))
    elif command == "prepared": value = _read(client, commands.resources.prepared_show(args.digest))
    elif command == "approvals":
        value = _read(client, commands.resources.approvals_list(
            operation_id=getattr(args, "operation", None), policy_digest=args.digest))
    elif command == "connect":
        value = _mutate(client, mutations.connect(*key_fields, args.bundle_digest))
    elif command == "target":
        if args.target_command == "start":
            try:
                value = _mutate(client, mutations.target_start(
                    *key_fields, args.connection, secrets_value, execution_policy=args.execution_policy))
            except ApiError as error:
                refusal = error.payload.get("error") if isinstance(error.payload, dict) else None
                if not args.machine and isinstance(refusal, dict) and refusal.get("code") == "target_slot_held":
                    for line in _slot_holders(client):
                        stdout.write(line + "\n")
                raise
        elif args.target_command == "list":
            value = _read(client, commands.resources.targets_list(bundle=args.bundle, ready=args.ready))
        elif args.target_command == "status": value = _read(client, commands.resources.target_status(args.id, args.generation))
        else:
            value = _mutate(client, mutations.target_stop(*key_fields, args.id, target_generation=args.generation))
    elif command == "check":
        value = _mutate(client, mutations.check(*key_fields, args.connection, _target_reference(args)))
    elif command == "conformance":
        value = _mutate(client, mutations.conformance(
            *key_fields, args.connection, _target_reference(args), runtime_policy=policy))
    elif command == "task":
        if args.task_command == "status": value = _read(client, commands.resources.task_status(args.id, args.generation))
        else: value = _mutate(client, mutations.task_stop(*key_fields, args.id))
    elif command == 'trace':
        value = _call(client, 'POST', _path('v1', 'operations', args.operation, 'trace'),
            payload={'schema': 'lab-service-trace-request/1', 'out': str(args.out.absolute()),
                     'secrets': secrets_value, 'disclose_private': args.disclose_private,
                     'measurements': args.measurements, 'max_field_bytes': args.max_field_bytes},
            mutation=True, expected_schema='lab-trace-summary/1', key=key)
    elif command == "start":
        value = _mutate(client, mutations.operation_start(
            *key_fields, args.prepared, _target_reference(args), secrets_value,
            linked_from=args.linked_from, baseline=args.baseline, seed=args.seed, baseline_from=args.baseline_from))
    elif command == "resume":
        value = _mutate(client, mutations.operation_resume(
            *key_fields, args.operation, _target_reference(args), secrets_value,
            accept=[item.to_payload() for item in accepted], reason=args.reason, prepared_digest=args.prepared))
    elif command == "list" and args.summary: value = _read(client, commands.operations.operations_summary())
    elif command == "list": value = _read(client, commands.operations.operations_list())
    elif command == "status": value = _read(client, commands.operations.operation_status(args.operation))
    elif command == "report" and args.markdown:
        data = _read(client, commands.operations.operation_report_markdown(args.operation))
        text = data.decode("utf-8")
        if args.output is None:
            stdout.write(text)
            stdout.flush()
            return 0
        written = publish_bytes(args.output, data)
        if args.machine:
            write_json(stdout, written)
        else:
            stdout.write(f"{written['path']} {written['bytes']} bytes\n")
            stdout.flush()
        return 0
    elif command == "report": value = _read(client, commands.operations.operation_report(args.operation))
    elif command == "operation": value = _read(client, commands.operations.operation_routing(args.operation))
    elif command in {"campaign", "view"}: value = _read(client, commands.operations.operation_campaign(args.operation))
    elif command == "suspensions": value = _read(client, commands.operations.operation_suspensions(args.operation))
    elif command == "sample":
        build = (commands.operations.operation_sample_disclosed if args.disclose_private
                 else commands.operations.operation_sample)
        value = _read(client, build(args.operation, args.measurement, args.case, args.sample))
    elif command == "attempt":
        build = (commands.operations.operation_attempt_disclosed if args.disclose_private
                 else commands.operations.operation_attempt)
        value = _read(client, build(args.operation, args.attempt))
    elif command == "reflection":
        build = (commands.operations.operation_reflection_disclosed if args.disclose_private
                 else commands.operations.operation_reflection)
        value = _read(client, build(args.operation, args.reflection))
    elif command == "measurements":
        value = _read(client, commands.operations.operation_measurements(args.operation) if args.measurement is None
                      else commands.operations.operation_measurement(args.operation, args.measurement))
    elif command == "diff":
        value = _read(client, commands.operations.operation_diff(args.operation, args.tree_from, args.tree_to))
    elif command == "stop":
        value = _mutate(client, mutations.operation_stop(*key_fields, args.operation))
    elif command == "delete-results":
        value = _mutate(client, mutations.delete_results(*key_fields, args.operation))
    elif command == "export":
        value = _call(client, "POST", _path("v1", "operations", args.operation, "export"), payload={},
                      mutation=True, expected_schema="lab-task-ref/1", key=key)
    elif command == "cleanup":
        value = _mutate(client, mutations.cleanup(*key_fields, {"kind": args.kind, "id": args.id}, args.wait_seconds))
    elif command == "release":
        value = _mutate(client, mutations.release(*key_fields, {"kind": args.kind, "id": args.id}, args.reason))
    elif command == "health": value = _read(client, commands.operations.health())
    elif command == "export-content":
        value = publish_export(args.output, client.get_bytes(_path("v1", "operations", args.operation, "exports", args.export_id, "content"), "application/zip"), args.export_id)
    elif command == "events":
        terminal = False
        for event in client.stream_events(_path("v1", "operations", args.operation, "events")):
            write_json(stdout, event["data"])
            if event.get("event") == "error": return 1
            data = event.get("data", {})
            terminal = terminal or data.get("payload", {}).get("control_state") in {"finished", "orphaned", "cleanup_required", "released"}
        if terminal:
            return 0
        write_json(stderr, local_error("interrupted_observation", "The service event observation ended before a terminal status."))
        return 1
    elif command == "watch":
        from .watch import Watch
        return Watch(client, args.operation, interval=args.interval, stdout=stdout, stderr=stderr,
                     ready_target=lambda value: _ready_target(client, value)).run()
    else: raise UsageError("unknown command")
    if getattr(args, "wait", False):
        return _wait_for_task(client, timeout=args.timeout, stdout=stdout, stderr=stderr, machine=args.machine)
    write_value(stdout, value, machine=args.machine,
                ready_target=_ready_target(client, value) if command == "status" and not args.machine else None)
    if command == 'status' and not args.machine:
        ownership = operator_line(select_mode())
        if ownership is not None:
            stdout.write(ownership + '\n')
        if (value.get('failure') or {}).get('code') == 'lab_error':
            try:
                record = _read(client, commands.operations.operation_failure(args.operation))
                exception = record['exception']
                code = '' if exception['code'] is None else f" (code {exception['code']})"
                stdout.write(f"worker exception: {exception['class']}{code}\n")
                stdout.write(f"traceback: {record['log']} (in the service's state directory)\n")
            except ClientError:
                pass
        if value.get('refusal') is not None:
            try:
                record = _read(client, commands.operations.operation_refusal(args.operation))
                from .output import finding_lines
                target = value['target_ref']
                command_line = ['lab', 'resume', args.operation, '--target', target['id'], '--generation', target['owner_generation']]
                stdout.write('\n'.join(finding_lines(record, command=command_line)) + '\n')
            except ClientError:
                stdout.write('Full refusal findings unavailable.\n')
        if len(value.get('starts', ())) > 1:
            try:
                history = _read(client, commands.operations.operation_inputs(args.operation))
                if len({row['prepared_digest'] for row in history['starts']}) > 1:
                    for row in history['starts']:
                        stdout.write(f"start {row['start']}: prepared {row['prepared_digest']} bundle {row['bundle_digest']}\n")
            except (ClientError, KeyError):
                pass
    return 0


def _slot_holders(client) -> list[str]:
    """Ready targets holding the Owner's one target slot, for a text-mode target_slot_held refusal (PDR-0025);
    a failed lookup prints nothing — the refusal stands as it is."""
    try:
        listed = _read(client, commands.resources.targets_list(ready=True))
        return [f"held by: {t['id']} (generation {t['owner_generation']}) — lab agent stop {t['id']} "
                f"--generation {t['owner_generation']} --wait" for t in listed["targets"]]
    except Exception:
        return []


def _ready_target(client, value):
    """A ready fresh target for the continuation line; a failed lookup only drops the substitution."""
    from evolution_lab.owner.resume_rules import pick_ready_target
    resume = value.get("resume") if isinstance(value, dict) else None
    target = resume.get("target") if isinstance(resume, dict) else None
    if not isinstance(target, dict):
        return None
    try:
        listed = _read(client, commands.resources.targets_list())
        return pick_ready_target(listed["targets"], target.get("excluded") or ())
    except Exception:
        return None


def main(argv=None, *, stdout=None, stderr=None, stdin=None, client_factory: Callable[..., Any] | None = None) -> int:
    """Run one explicit command; optional injections exist solely for controlled tests."""
    stdout, stderr, stdin = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    try:
        check()
    except DurabilityConfigError as error:
        stderr.write(str(error) + "\n")
        return 2
    parser = build_parser()
    args = None
    client = None
    parser._output = stdout
    try:
        args = parser.parse_args(argv)
        args.resolved_key = None
        _validate_key_args(args)
        if args.command == "policy" and args.policy_command == "digest":
            from evolution_lab.execution_policy import policy_digest
            document = _read_json(args.file, stdin)
            _parse_policy_document(document)
            stdout.write(policy_digest(document) + "\n")
            return 0
        if args.command == "serve":
            if args.execution_runtime_files and not args.execution_wheels:
                raise UsageError("--execution-runtime-file requires --execution-wheel")
            from evolution_lab.service import server
            return server.run_service(socket_path=args.socket, state_directory=args.state,
                shutdown_wait_seconds=args.shutdown_wait_seconds,
                execution_wheels=tuple(args.execution_wheels),
                execution_runtime_files=tuple(args.execution_runtime_files),
                home_directory=None if args.home is None else Path(args.home),
                durability=args.durability,
                scratch_directory=None if args.scratch is None else Path(args.scratch), stderr=stderr)
        if args.command == "mcp":
            from .mcp import run_mcp
            return run_mcp(args, stderr=stderr)
        if args.command == "preflight":
            from .preflight import run_preflight
            return run_preflight(args, stdout=stdout, stderr=stderr)
        if args.command == "guides":
            from .guides import GuidesUsage, run_guides
            try:
                return run_guides(args, stdout=stdout, stderr=stderr)
            except GuidesUsage as error:
                raise UsageError(str(error)) from None
        if args.command == "new-project":
            from .quickstart import QuickstartUsage, run_new_project
            try:
                return run_new_project(args, stdout=stdout, stderr=stderr)
            except QuickstartUsage as error:
                raise UsageError(str(error)) from None
        if args.command == "evaluate":
            from .quickstart import QuickstartUsage, run_evaluate
            try:
                return run_evaluate(args, stdout=stdout, stderr=stderr)
            except QuickstartUsage as error:
                raise UsageError(str(error)) from None
        if args.command == 'trace':
            if args.max_field_bytes < 1 or str(args.out) == '-':
                raise UsageError('lab trace needs an output file path and a positive --max-field-bytes')
            if Path(args.operation).is_dir():
                from .quickstart import QuickstartUsage, run_trace
                try:
                    return run_trace(args, stdin=stdin, stdout=stdout, stderr=stderr)
                except QuickstartUsage as error:
                    raise UsageError(str(error)) from None
            if args.selected_operation is not None:
                raise UsageError('an optional operation identifier requires a project directory')
        if args.command == "campaign":
            directory = Path(args.operation)
            if directory.is_dir() and any((directory / name).is_file() for name in ("campaign-launch.json", "launch.json")):
                from .quickstart import QuickstartUsage, run_campaign
                try:
                    return run_campaign(args, stdout=stdout, stderr=stderr)
                except QuickstartUsage as error:
                    raise UsageError(str(error)) from None
            stderr.write("Old name: lab campaign OPERATION reads the campaign view; use lab view OPERATION.\n")
        if args.command == "scenarios":
            from .scenarios import ScenariosUsage, run_scenarios
            try:
                return run_scenarios(args, stdout=stdout, stderr=stderr)
            except ScenariosUsage as error:
                raise UsageError(str(error)) from None
        if args.command == "panel":
            from .panel import run_panel_command
            return run_panel_command(args, stdout=stdout, stderr=stderr)
        try:
            socket_path = str(resolve_socket(args.socket, args.home))
        except ValueError as error:
            raise UsageError(str(error)) from None
        try:
            _validate_args(args)
        except UsageError:
            raise
        except ValueError as error:
            raise UsageError("invalid command input") from error
        factory = ServiceClient if client_factory is None else client_factory
        client = factory(socket_path=socket_path, timeout=args.timeout)
        try:
            return _dispatch(args, client, stdin=stdin, stdout=stdout, stderr=stderr)
        finally:
            close = getattr(client, "close", None)
            if callable(close): close()
    except SystemExit as error:
        return int(error.code)
    except UsageError as error:
        stderr.write(f"usage: {error}\n")
        return 2
    except AcceptanceUnknown as error:
        action = "unknown" if args is None else " ".join(str(item) for item in (
            args.command, getattr(args, "bundle_command", None), getattr(args, "prepared_command", None),
            getattr(args, "target_command", None), getattr(args, "task_command", None),
            getattr(args, "project_command", None),
        ) if item)
        resolved = None if args is None else getattr(args, "resolved_key", None)
        extra = {} if resolved is None else {"request_id": resolved[0], "owner_generation": resolved[1]}
        write_json(stderr, local_error("acceptance_unknown", "The mutation acceptance is unknown.",
            acceptance="unknown", action=action, cause=error.cause, **extra))
        return 125
    except RequestTimeout:
        write_json(stderr, local_error("request_timeout", "The service request timed out."))
        return 124
    except RequestInterrupted:
        write_json(stderr, local_error("request_interrupted", "The service request was interrupted."))
        return 130
    except KeyboardInterrupt:
        ref = None if client is None else getattr(client, "_accepted_task_ref", None)
        if ref is not None:
            write_json(stderr, local_error("wait_interrupted", "Local waiting for the accepted task was interrupted.",
                acceptance="accepted", task_ref=ref))
        else:
            write_json(stderr, local_error("request_interrupted", "The service request was interrupted."))
        return 130
    except ApiError as error:
        payload = error.payload
        if isinstance(payload.get("error"), dict) and payload["error"].get("code") == "request_table_full":
            payload = {**payload, "hint": "retry with --no-request-id to send this mutation without a request key"}
        write_json(stderr, payload)
        if (args.command == "project" and args.project_command == "accept"
                and isinstance(payload.get("error"), dict) and payload["error"].get("code") == "digest_mismatch"):
            stderr.write("hint: the facts changed since the preview; run lab project accept again\n")
        return 1
    except (ClientError, OSError, ValueError) as error:
        if isinstance(error, PolicyRequestInvalid):
            detail = f" field: {error.pointer}" if error.pointer else ""
            write_json(stderr, local_error("invalid_request", f"The policy is invalid.{detail}"))
            return 1
        write_json(stderr, local_error("client_error", "The local service client failed."))
        return 1
