"""Offline `lab new-project`; it never opens the service socket."""
from __future__ import annotations

from pathlib import Path
from typing import TextIO

from evolution_lab._ownership import operator_line, select_mode

from .output import write_json


class QuickstartUsage(Exception):
    pass


_NEXT_STEPS = """Created the project skeleton in {directory}

Next steps:
  1. Replace the stub in agent/my_agent.py with your agent and update agent/adapter.py to call it;
     {directory}/README.md says how.
  2. Put your questions and expected answers in {directory}/corpus.jsonl.
  3. Measure it: lab evaluate {directory} --wheel LAB_WHEEL --wheel CORE_WHEEL
     LAB_WHEEL and CORE_WHEEL are the wheel files you installed Lab and the core from;
     or, in the Lab checkout: just evaluate {directory}
"""


def run_new_project(args, *, stdout: TextIO, stderr: TextIO) -> int:
    from evolution_lab.quickstart import QuickstartError, new_project
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise QuickstartUsage("lab new-project does not accept --socket, --home or --timeout")
    try:
        directory = new_project(Path(args.directory))
    except QuickstartError as error:
        write_json(stderr, {"error": {"code": error.code, "stage": "new-project", "message": str(error),
                                       "file": error.file, "field": error.field}})
        return error.exit_code
    except Exception as error:  # not a project mistake Lab knows: exit 3, never the service-client error
        return _unexpected(stderr, error, "new-project")
    stdout.write(_NEXT_STEPS.format(directory=directory))
    stdout.flush()
    return 0


_WHEEL_HINT = ("lab evaluate needs the wheel files Lab and the core were installed from in this Python: "
               "--wheel LAB_WHEEL --wheel CORE_WHEEL (just evaluate DIR passes them from .lab/wheels/)")


def _quickstart_error(stderr: TextIO, error, stage: str) -> int:
    payload = {"code": error.code, "stage": stage, "message": str(error), "file": error.file,
               "field": error.field, "role": error.role}
    payload.update({key: value for key in ("state", "run_dir")
                    if (value := getattr(error, key, None)) is not None})
    write_json(stderr, {"error": payload})
    return error.exit_code


def _unexpected(stderr: TextIO, error: Exception, stage: str, **where) -> int:
    """An error no step expected: exit 3 with what it was and where the state and the run are."""
    payload = {"code": "unexpected_error", "stage": stage, "message": f"{type(error).__name__}: {error}"}
    payload.update({key: str(value) for key, value in where.items() if value is not None})
    write_json(stderr, {"error": payload})
    return 3


def run_evaluate(args, *, stdout: TextIO, stderr: TextIO) -> int:
    """`lab evaluate DIR`: Ctrl+C stops this run's target and task, prints the state path and exits 130."""
    import asyncio

    from evolution_lab.quickstart import QuickstartError, evaluate
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise QuickstartUsage("lab evaluate does not accept --socket, --home or --timeout")
    if not args.wheels:
        raise QuickstartUsage(_WHEEL_HINT)
    try:
        result = asyncio.run(evaluate(Path(args.directory), wheels=[Path(path) for path in args.wheels],
                                      python=None if args.python is None else Path(args.python)))
    except QuickstartError as error:
        return _quickstart_error(stderr, error, "evaluate")
    except KeyboardInterrupt:
        # evaluate() has stopped its target and task and printed where the state is.
        return 130
    except Exception as error:  # evaluate() wraps its own; this is anything around it
        return _unexpected(stderr, error, "evaluate")
    try:
        if getattr(args, "machine", False):
            write_json(stdout, result.summary)
        else:
            from evolution_lab.quickstart._readout import render_readout
            stdout.write(render_readout(result.summary, result.run_dir, rows=result.rows,
                                        agent_stderr=result.agent_stderr,
                                        ownership_line=operator_line(select_mode())))
            stdout.flush()
    except Exception as error:
        return _unexpected(stderr, error, "evaluate", state=result.summary.get("state"), run_dir=result.run_dir)
    return result.exit_code


def run_campaign(args, *, stdout: TextIO, stderr: TextIO) -> int:
    import asyncio

    from evolution_lab.quickstart import QuickstartError
    from evolution_lab.quickstart._campaign import campaign
    from evolution_lab.quickstart._readout import render_campaign_readout
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise QuickstartUsage("lab campaign DIR does not accept --socket, --home or --timeout")
    if not args.wheels:
        raise QuickstartUsage("lab campaign needs --wheel LAB_WHEEL --wheel CORE_WHEEL (just campaign DIR passes them)")
    from .acceptance import acceptances
    accepted = acceptances(args.accept, args.effect)
    if (accepted or args.reason is not None or args.prepared is not None or args.reprepare) and not args.continue_run:
        raise QuickstartUsage('--accept, --reason, --prepared and --reprepare require --continue')
    def warn(message):
        stderr.write(message + "\n")
        stderr.flush()

    try:
        result = asyncio.run(campaign(Path(args.operation), wheels=[Path(p) for p in args.wheels],
                                      python=None if args.python is None else Path(args.python),
                                      continue_run=args.continue_run, accept=accepted, reason=args.reason,
                                      prepared=args.prepared, reprepare=args.reprepare, linked_from=args.linked_from,
                                      baseline=args.baseline, baseline_from=args.baseline_from,
                                      seed=args.seed, on_baseline_warning=warn))
    except QuickstartError as error:
        return _quickstart_error(stderr, error, "campaign")
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        return _unexpected(stderr, error, "campaign")
    try:
        if getattr(args, "machine", False):
            write_json(stdout, result.summary)
        else:
            stdout.write(render_campaign_readout(result.summary, result.run_dir,
                                                ownership_line=operator_line(select_mode())))
            stdout.flush()
    except Exception as error:
        return _unexpected(stderr, error, "campaign", state=result.summary.get("state"), run_dir=result.run_dir)
    return result.exit_code


def run_trace(args, *, stdin, stdout: TextIO, stderr: TextIO) -> int:
    import asyncio
    from .parser import _secrets_value
    from .output import write_value
    from evolution_lab.owner.traces import trace_summary_payload
    from evolution_lab.quickstart import QuickstartError
    from evolution_lab.quickstart._campaign import trace_project
    if args.socket is not None or args.timeout is not None or args.home is not None:
        raise QuickstartUsage('lab trace DIR does not accept --socket, --home or --timeout')
    secrets = _secrets_value(args, stdin)
    try:
        summary = asyncio.run(trace_project(Path(args.operation), operation=args.selected_operation,
            out=args.out.absolute(), secrets=secrets, disclose_private=args.disclose_private,
            measurements=args.measurements, max_field_bytes=args.max_field_bytes))
    except QuickstartError as error:
        return _quickstart_error(stderr, error, 'trace')
    except Exception as error:
        return _unexpected(stderr, error, 'trace')
    write_value(stdout, trace_summary_payload(summary), machine=args.machine)
    return 0
