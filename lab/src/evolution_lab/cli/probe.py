"""``lab probe``: thin CLI over the probe session routes (spec 2026-10-02 §5.2).

Like ``cli/project.py``: :func:`request` maps one command to ``(method, path, payload, schema, timeout)``;
the dispatcher in ``parser.py`` resolves the mutation key, makes the call and prints the form.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .errors import UsageError

MUTATIONS = frozenset({"start", "say", "reset", "close"})
# One turn may take as long as the session's turn timeout (at most 600 s, spec §3.1); an explicit
# ``--timeout`` shorter than that would drop the reply of a turn the service still runs.
TURN_CLIENT_TIMEOUT = 610.0


def _count(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _absolute(value: str) -> str:
    if not Path(value).is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return value


def _timeout(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number of seconds") from error
    if not 0 < number <= 600:
        raise argparse.ArgumentTypeError("must be above 0 and at most 600")
    return number


def add_parser(target) -> None:
    probe = target.add_parser("probe").add_subparsers(dest="probe_command", required=True)
    start = probe.add_parser("start")
    start.add_argument("project"); start.add_argument("implementation")
    start.add_argument("--max-turns", type=_count, required=True)
    start.add_argument("--max-tokens", type=_count)
    start.add_argument("--max-cost")
    start.add_argument("--budget-ledger", type=_absolute)
    start.add_argument("--budget-role", action="append", default=[])
    start.add_argument("--budget-currency", choices=("RUB", "USD"))
    start.add_argument("--turn-timeout", type=_timeout)
    start.add_argument("--seed", type=int)
    say = probe.add_parser("say")
    say.add_argument("session"); say.add_argument("text", help="the message, or - to read it from stdin")
    for name in ("history", "reset", "close"):
        probe.add_parser(name).add_argument("session")
    listing = probe.add_parser("list")
    listing.add_argument("project"); listing.add_argument("--implementation")


def _path(*segments: str) -> str:
    return "/" + "/".join(quote(segment, safe="") for segment in segments)


def _budget(args) -> dict | None:
    if args.budget_ledger is None:
        if args.max_cost is not None or args.budget_role or args.budget_currency is not None:
            raise UsageError("--max-cost, --budget-role and --budget-currency need --budget-ledger")
        return None
    if not args.budget_role or args.budget_currency is None:
        raise UsageError("--budget-ledger needs --budget-role (at least one) and --budget-currency")
    return {"ledger_path": args.budget_ledger, "roles": list(args.budget_role), "currency": args.budget_currency}


def check(args) -> None:
    """Local option rules, before any service call (usage errors exit 2)."""
    if args.probe_command == "start":
        _budget(args)


def request(args, read_stdin: Callable[[], str], notice: Callable[[str], None] = lambda _: None) -> tuple[str, str, dict | None, str, float | None]:
    command = args.probe_command
    if command == "start":
        ceiling = {"max_turns": args.max_turns}
        if args.max_tokens is not None:
            ceiling["max_tokens"] = args.max_tokens
        if args.max_cost is not None:
            ceiling["max_cost"] = args.max_cost
        body = {"schema": "lab-service-project-probe-request/1", "implementation": args.implementation,
                "ceiling": ceiling, "budget": _budget(args), "turn_timeout_seconds": args.turn_timeout,
                "seed": args.seed}
        return "POST", _path("v1", "projects", args.project, "probes"), body, "lab-probe-session/1", None
    if command == "say":
        text = read_stdin() if args.text == "-" else args.text
        if args.text == "-" and text.endswith("\n"):
            text = text[:-1]  # the newline that ends piped input is not part of the message
        if not text:
            raise UsageError("the message is empty")
        timeout = None if args.timeout is None else max(args.timeout, TURN_CLIENT_TIMEOUT)
        if timeout is not None and timeout != args.timeout:
            notice(f"lab: --timeout raised to {TURN_CLIENT_TIMEOUT:.0f} s for say (turns may take up to 600 s)")
        return ("POST", _path("v1", "probes", args.session, "turns"),
                {"schema": "lab-service-probe-turn-request/1", "content": text}, "lab-probe-turn/1", timeout)
    if command == "history":
        return "GET", _path("v1", "probes", args.session), None, "lab-probe-session/1", None
    if command == "list":
        path = _path("v1", "projects", args.project, "probes")
        if args.implementation is not None:
            path += "?implementation=" + quote(args.implementation, safe="")
        return "GET", path, None, "lab-probe-session-list/1", None
    if command == "reset":
        return "POST", _path("v1", "probes", args.session, "reset"), {}, "lab-probe-session/1", None
    return "DELETE", _path("v1", "probes", args.session), None, "lab-probe-session/1", None
