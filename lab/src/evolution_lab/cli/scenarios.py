"""Offline `lab scenarios` commands (spec §4.3): like `lab guides`, never the service socket."""
from __future__ import annotations

from typing import TextIO

from .output import write_json


class ScenariosUsage(Exception):
    pass


def _write_entry(stdout: TextIO, entry: dict) -> None:
    descriptor = entry["descriptor"]
    stdout.write(f"{descriptor['id']}\t{descriptor['version']}\t{descriptor['title']}\t{entry['skill_path']}\n")


def _not_in_catalogue(error: LookupError) -> bool:
    """The layer refuses an unknown scenario or schema with a plain ``LookupError``; a ``KeyError`` is a bug."""
    return type(error) is LookupError


def run_scenarios(args, *, stdout: TextIO, stderr: TextIO) -> int:
    from evolution_lab import commands, guides
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise ScenariosUsage("lab scenarios does not accept --socket, --home or --timeout")
    machine = bool(args.machine or getattr(args, "scenarios_json", False))
    try:
        if args.scenarios_command == "path":
            stdout.write(f"{guides.root() / 'scenarios'}\n")
        elif args.scenarios_command == "list":
            listed = commands.offline.scenarios_list()
            if machine:
                write_json(stdout, listed)
            else:
                for entry in listed["scenarios"]:
                    _write_entry(stdout, entry)
        elif args.scenarios_command == "schema":
            try:
                text = commands.offline.scenario_schema(args.scenario, args.schema)
            except LookupError as error:
                if not _not_in_catalogue(error):
                    raise
                write_json(stderr, {"error": {"code": "not_found", "stage": "guides",
                                              "message": "The scenario or its schema is not in the catalogue."}})
                return 1
            stdout.write(text)
        else:
            try:
                entry = commands.offline.scenario_show(args.scenario)
            except LookupError as error:
                if not _not_in_catalogue(error):
                    raise
                write_json(stderr, {"error": {"code": "not_found", "stage": "guides",
                                              "message": "The scenario is not in the catalogue."}})
                return 1
            if machine:
                write_json(stdout, entry)
            else:
                _write_entry(stdout, entry)
                for step in entry["descriptor"]["steps"]:
                    parts = [f"step {step['id']} {step['kind']}", "human_only" if step["human_only"] else "",
                             "optional" if step.get("optional", False) else "",
                             "requires=" + ",".join(step["requires"]) if step["requires"] else ""]
                    stdout.write(" ".join(part for part in parts if part) + "\n")
    except (ValueError, RuntimeError, OSError) as error:
        write_json(stderr, {"error": {"code": "guides_invalid", "stage": "guides", "message": str(error)}})
        return 1
    stdout.flush()
    return 0
