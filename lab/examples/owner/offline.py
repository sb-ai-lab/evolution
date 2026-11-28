"""Run one prepared neutral campaign through the local Python Owner S2 API."""
from __future__ import annotations

import argparse
import asyncio
from getpass import getpass
import json
from pathlib import Path

from evolution_lab.owner import Owner, ResourceRef, TargetRef
from evolution_lab.preparation import PreparationStore


_ACTIVE = {"reserved", "running", "stopping"}
_BLOCKED = {"cleanup_required", "orphaned"}


def _print(label: str, payload) -> None:
    print(json.dumps({label: payload}, ensure_ascii=False, sort_keys=True))


def _prompt_secrets(target_names: list[str], execution_names: list[str]):
    """Prompt once per name; do not put secret values in argv or ambient env."""
    values = {name: getpass(f"secret {name}: ") for name in dict.fromkeys([*target_names, *execution_names])}
    return (
        {name: values[name] for name in target_names},
        {name: values[name] for name in execution_names},
    )


def _successful_task(status, action: str) -> None:
    if status.state != "succeeded":
        raise RuntimeError(f"{action} did not succeed: {status.to_payload()!r}")


async def _poll(owner: Owner, ref: ResourceRef, seconds: float):
    value = await owner.status(ref)
    while value.control_state in _ACTIVE:
        _print("operation", value.to_payload())
        await asyncio.sleep(seconds)
        value = await owner.status(ref)
    _print("operation", value.to_payload())
    return value


async def run(args) -> None:
    # S1 preparation is deliberately separate and synchronous. This consumes an
    # existing Bundle; see examples/preparation_s1 for neutral Bundle creation.
    prepared = PreparationStore(args.state_directory).prepare_campaign(args.bundle_digest)
    _print("prepared", prepared.to_payload())

    target_secrets, execution_secrets = _prompt_secrets(
        args.target_secret, args.execution_secret
    )

    async with Owner.open(args.state_directory) as owner:
        connection = await owner.bind(prepared.bundle_digest)
        _print("connection", connection.to_payload())

        startup_ref = await owner.start_target(connection, secrets=target_secrets)
        startup = await owner.wait(startup_ref)
        _print("target_start", startup.to_payload())
        _successful_task(startup, "target_start")
        if startup.ref is None:
            raise RuntimeError("target_start succeeded without a target resource")

        target_ref = TargetRef(startup.ref.id, startup.owner_generation)
        _print("target", (await owner.target(target_ref)).to_payload())

        if args.check:
            check_ref = await owner.check(connection, target_ref=target_ref)
            checked = await owner.wait(check_ref)
            _print("check", checked.to_payload())
            _successful_task(checked, "check")

        operation = await owner.start(
            prepared.digest,
            target_ref=target_ref,
            secrets=execution_secrets,
        )
        operation_ref = ResourceRef("operation", operation.id)
        _print("accepted_operation", operation.to_payload())

        if args.request_stop:
            stop = await owner.stop(operation_ref)
            _print("stop", stop.to_payload())

        operation = await _poll(owner, operation_ref, args.poll_seconds)

        report = await owner.report(operation_ref)
        # A full report is private integration data. This example does not print
        # report.report; the caller may handle it in an appropriately private sink.
        _print(
            "report",
            {
                "schema": report.schema,
                "operation_id": report.operation_id,
                "state": report.state,
                "reason": report.reason,
            },
        )

        if operation.control_state in _BLOCKED and args.cleanup_on_blocker:
            cleanup_ref = await owner.cleanup(operation_ref)
            cleanup = await owner.wait(cleanup_ref)
            _print("cleanup", cleanup.to_payload())
            _successful_task(cleanup, "cleanup")
            operation = await owner.status(operation_ref)
            _print("operation_after_cleanup", operation.to_payload())

        target = await owner.target(target_ref)
        if target.state == "ready":
            target_stop_ref = await owner.stop_target(target_ref)
            target_stop = await owner.wait(target_stop_ref)
            _print("target_stop", target_stop.to_payload())
            _successful_task(target_stop, "target_stop")

        _print("health", (await owner.health()).to_payload())


async def manual_release(args) -> None:
    # Reaching this command is an explicit operator assertion. It must follow
    # external cleanup of the exact remnants and fixed port; it is not a PID kill.
    async with Owner.open(args.state_directory) as owner:
        ref = ResourceRef(args.kind, args.id)
        receipt = await owner.release_after_manual_cleanup(ref, reason=args.reason)
        _print("manual_release", receipt.to_payload())
        _print("health", (await owner.health()).to_payload())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="prepare and run one new campaign attempt")
    run_parser.add_argument("--state-directory", type=Path, required=True)
    run_parser.add_argument("--bundle-digest", required=True)
    run_parser.add_argument("--target-secret", action="append", default=[], metavar="NAME")
    run_parser.add_argument("--execution-secret", action="append", default=[], metavar="NAME")
    run_parser.add_argument("--check", action="store_true", help="run explicit H1 preflight")
    run_parser.add_argument("--request-stop", action="store_true", help="request cooperative Stop after acceptance")
    run_parser.add_argument("--cleanup-on-blocker", action="store_true", help="explicitly clean a terminal blocked operation")
    run_parser.add_argument("--poll-seconds", type=float, default=0.5)

    manual = commands.add_parser("manual-release", help="attest externally completed cleanup")
    manual.add_argument("--state-directory", type=Path, required=True)
    manual.add_argument("--kind", choices=("operation", "target", "task"), required=True)
    manual.add_argument("--id", required=True)
    manual.add_argument("--reason", required=True)
    manual.add_argument(
        "--attest-external-cleanup-complete",
        action="store_true",
        required=True,
        help="assert that exact external remnants and the fixed port are clear",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    if args.command == "run":
        if args.poll_seconds <= 0:
            raise SystemExit("--poll-seconds must be positive")
        asyncio.run(run(args))
    else:
        asyncio.run(manual_release(args))


if __name__ == "__main__":
    main()
