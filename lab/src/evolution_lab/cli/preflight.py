"""Terminal adapter for the offline context report."""

from __future__ import annotations

from pathlib import Path

from .errors import UsageError
from .output import write_json


def run_preflight(args, *, stdout, stderr) -> int:
    from evolution_lab.commands.offline import context_preflight
    from evolution_lab.context_report import render_table
    from evolution_lab.context_scenario import ContextScenarioError
    from evolution_lab.preparation import PreparationError

    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise UsageError("lab preflight reads local state; use a prepared directory or --state")
    if args.state is None:
        directory = Path(args.prepared)
        if directory.parent.name != "prepared":
            raise UsageError("pass STATE/prepared/DIGEST or specify PREPARED_DIGEST --state STATE")
        state, digest = directory.parent.parent, directory.name
    else:
        state, digest = args.state, args.prepared
    try:
        report = context_preflight(
            state,
            digest,
            operation=args.operation,
            trace=args.trace,
            baseline=args.baseline,
            scenario=args.scenario,
        )
    except PreparationError as error:
        write_json(
            stderr,
            {"error": {"code": error.code, "stage": "preflight", "message": error.failure.message}},
        )
        return 1
    except ContextScenarioError as error:
        write_json(
            stderr,
            {
                "error": {
                    "code": "invalid_context_scenario",
                    "stage": "preflight",
                    "message": str(error),
                }
            },
        )
        return 1
    except (ValueError, OSError):
        write_json(
            stderr,
            {
                "error": {
                    "code": "invalid_context_input",
                    "stage": "preflight",
                    "message": "Check the prepared campaign, evidence and scenario inputs.",
                }
            },
        )
        return 1
    if args.machine:
        write_json(stdout, report.as_dict())
    else:
        stdout.write(render_table(report))
    return 0
