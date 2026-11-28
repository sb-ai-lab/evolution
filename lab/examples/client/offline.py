"""Installed H1 client walkthrough with a synthetic local Evolution target."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from threading import Thread
from time import monotonic

from evolution.tree import load_manifest_v2, serve_reference_target
from evolution.tree.reference_target_cli import bind_reference_target, scaffold
from evolution_lab._scaffold_manifest import as_manifest_15
from evolution_lab.client import check, connect, create_run, prepare, report, status, stop


def _absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise argparse.ArgumentTypeError("expected an absolute path to an existing file")
    return path


def _positive_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _slow_neutral_scorer(path: Path) -> None:
    """Leave time to observe and stop a live operation in this walkthrough."""
    source = path.read_text(encoding="utf-8")
    marker = "def main() -> None:\n"
    replacement = marker + "    import time\n    time.sleep(2.0)\n"
    if source.count(marker) != 1:
        raise RuntimeError("unexpected public scaffold scorer")
    path.write_text(source.replace(marker, replacement), encoding="utf-8")


def _configure_recipe(root: Path, *, python: Path, host: str, port: int) -> Path:
    sample = scaffold(root)
    manifest_path = sample.manifest
    _slow_neutral_scorer(root / "scorer" / "score.py")
    # Core scaffolds manifest /15; Lab writes /15 with core's observe smoke profile (gate not enforced: wiring only).
    raw = as_manifest_15(json.loads(manifest_path.read_text(encoding="utf-8")))
    raw["target"]["base_url"] = f"http://{host}:{port}"
    scripts = {
        "scorer": root / "scorer" / "score.py",
        "editor": root / "editor" / "edit.py",
        "reflector": root / "reflector" / "reflect.py",
    }
    for role, script in scripts.items():
        raw[role]["argv"] = [str(python), str(script.resolve())]
        raw[role]["cwd"] = str(script.parent.resolve())
    manifest_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


async def _wait_for_campaign(operation, task: asyncio.Task, timeout: float):
    deadline = monotonic() + timeout
    latest = await status(operation)
    while latest.campaign_availability != "available" and not task.done():
        if monotonic() >= deadline:
            raise TimeoutError("campaign projection did not become available")
        await asyncio.sleep(0.02)
        latest = await status(operation)
    return latest


async def main(args: argparse.Namespace) -> None:
    output = Path(args.output)
    if not output.is_absolute():
        raise ValueError("--output must be absolute")
    if Path(args.python) != Path(sys.executable):
        raise ValueError("--python must name the interpreter running this script")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)

    manifest_path = _configure_recipe(
        output / "recipe", python=args.python, host=args.target_host,
        port=args.target_port,
    )
    manifest = load_manifest_v2(manifest_path)
    target = bind_reference_target(manifest, allowed_root=output)
    http = serve_reference_target(
        target, host=args.target_host, port=args.target_port
    )
    thread = Thread(
        target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        connection = connect(manifest_path)
        checked = await check(connection)
        if checked.state != "passed":
            raise RuntimeError(f"explicit check failed: {checked.public()}")

        environment = dict(os.environ)
        environment["LANG"] = args.locale
        environment["LC_ALL"] = args.locale
        prepared = prepare(
            connection,
            wheel_paths=tuple(args.wheel),
            extra_paths=(),
            env=environment,
        )

        run_directory = output / "run"

        def allow_synthetic_dispatch() -> None:
            # This empty admission is valid only for this no-provider fixture.
            return None

        operation = create_run(
            prepared,
            run_directory,
            dispatch_guard=allow_synthetic_dispatch,
            grace_seconds=args.grace_seconds,
        )
        if operation.result is not None or run_directory.exists():
            raise RuntimeError("create_run was not inert")
        created = await status(operation)

        events: list[dict] = []
        task = asyncio.create_task(operation.run(on_event=events.append))
        active = await _wait_for_campaign(operation, task, args.status_timeout)
        receipt = stop(operation)
        result = await asyncio.wait_for(task, timeout=args.run_timeout)
        final = await status(operation)

        report_value = report(run_directory)
        report_value.write(output / "report")
        print(json.dumps({
            "connection": connection.public(),
            "check": checked.public(),
            "prepared": prepared.public(),
            "created_status": created.public(),
            "observed_status": active.public(),
            "stop": receipt.public(),
            "events": events,
            "result_in_memory": result.public(),
            "final_status": final.public(),
            "report": report_value.data,
            "paths": {
                "run": str(run_directory),
                "lab_logs": str(run_directory.with_name(run_directory.name + "-lab-logs")),
                "report": str(output / "report"),
            },
        }, ensure_ascii=False, indent=2))
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--python", required=True, type=_absolute_file)
    parser.add_argument("--wheel", required=True, action="append", type=_absolute_file)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", required=True, type=_positive_port)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--grace-seconds", required=True, type=float)
    parser.add_argument("--status-timeout", type=float, default=10.0)
    parser.add_argument("--run-timeout", type=float, default=60.0)
    asyncio.run(main(parser.parse_args()))
