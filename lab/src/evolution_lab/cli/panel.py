"""``lab panel``: the read-only panel in the foreground or background, ``status`` and ``stop``."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from evolution_lab.panel.runtime import PanelError
from evolution_lab.projects.endpoint import resolve_socket
from evolution_lab.projects.home import default_home

from .errors import UsageError
from .output import write_json


def _port(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a port number") from error
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be a port number")
    return number


def add_parser(commands) -> None:
    from .parser import _absolute, _positive  # parser.py imports this module at load time

    panel = commands.add_parser("panel")
    # --home/--socket also exist globally; SUPPRESS keeps a global value when the subcommand
    # does not repeat it (the same arrangement as ``serve --socket``).
    panel.add_argument("--home", type=_absolute, default=argparse.SUPPRESS)
    panel.add_argument("--socket", type=_absolute, default=argparse.SUPPRESS)
    panel.add_argument("--port", type=_port, default=0)
    panel.add_argument("--open", action="store_true", dest="open_browser")
    panel.add_argument("--background", action="store_true")
    sub = panel.add_subparsers(dest="panel_command")
    status = sub.add_parser("status")
    status.add_argument("--home", type=_absolute, default=argparse.SUPPRESS)
    stop = sub.add_parser("stop")
    stop.add_argument("--home", type=_absolute, default=argparse.SUPPRESS)
    stop.add_argument("--wait-seconds", type=_positive, default=10.0)


def _text(value: dict) -> str:
    schema = value["schema"]
    if schema == "lab-panel-runtime/1":
        return f"panel: {value['url']}"
    if value["state"] == "not_running":
        return "panel: not running"
    if schema == "lab-panel-stop/1":
        return f"panel: stopped (pid {value['pid']})"
    record = value["panel"]
    if record is None:
        return "panel: running (no readable panel.json)"
    return f"panel: running {record['url']} (pid {record['pid']}, since {record['started_at']})"


def _emit(stdout: TextIO, value: dict, machine: bool) -> None:
    if machine:
        write_json(stdout, value)
    else:
        stdout.write(_text(value) + "\n")
        stdout.flush()


def _open_in_thread(url: str) -> None:
    # Foreground only: webbrowser.open can block (a slow launcher), and this runs from the ready
    # callback, before the server starts serving. A daemon thread keeps serving unblocked.
    import threading
    import webbrowser
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True, name="panel-open-browser").start()


def run_panel_command(args, *, stdout: TextIO, stderr: TextIO) -> int:
    from evolution_lab.panel import background

    home = Path(args.home) if args.home is not None else default_home()
    try:
        if args.panel_command == "status":
            from evolution_lab.panel.runtime import STATUS_SCHEMA, existing_panel_home, runtime_status
            checked = existing_panel_home(home)
            value = runtime_status(checked) if checked is not None else {
                "schema": STATUS_SCHEMA, "state": "not_running", "panel": None}
            _emit(stdout, value, args.machine)
            return 0
        if args.panel_command == "stop":
            _emit(stdout, background.stop(home, wait_seconds=args.wait_seconds), args.machine)
            return 0
        try:
            socket_path = resolve_socket(args.socket, args.home)
        except ValueError as error:
            raise UsageError(str(error)) from None
        if args.background:
            explicit = [] if args.socket is None else ["--socket", args.socket]
            record = background.start(home=home, argv_tail=["panel", "--port", str(args.port), *explicit])
            _emit(stdout, record, args.machine)
            if args.open_browser:
                # Synchronously: this parent serves nothing, and a daemon thread would die with
                # the CLI's exit before the browser opened.
                import webbrowser
                webbrowser.open(record["url"])
            return 0

        def ready(record: dict) -> None:
            _emit(stdout, record, args.machine)
            if args.open_browser:
                _open_in_thread(record["url"])

        from evolution_lab.panel.server import run_panel
        return run_panel(home=home, socket_path=socket_path, port=args.port, ready=ready)
    except PanelError as error:
        write_json(stderr, error.payload())
        return 1
