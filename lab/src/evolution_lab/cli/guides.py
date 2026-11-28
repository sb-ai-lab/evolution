"""Offline `lab guides` commands; they never open the service socket."""
from __future__ import annotations

from typing import TextIO

from .output import write_json


class GuidesUsage(Exception):
    pass


def run_guides(args, *, stdout: TextIO, stderr: TextIO) -> int:
    from evolution_lab import commands, guides
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise GuidesUsage("lab guides does not accept --socket, --home or --timeout")
    machine = bool(args.machine or getattr(args, "guides_json", False))
    try:
        if args.guides_command == "path":
            stdout.write(f"{guides.root()}\n")
        elif args.guides_command == "list":
            found = commands.offline.guides_list()
            if machine:
                write_json(stdout, found)
            else:
                for g in found:
                    stdout.write(f"{g['name']}\t{g['path']}\t{g['description']}\n")
        else:
            write_json(stdout, commands.offline.requirements())
    except (ValueError, RuntimeError, OSError) as error:
        write_json(stderr, {"error": {"code": "guides_invalid", "stage": "guides", "message": str(error)}})
        return 1
    stdout.flush()
    return 0
