"""Installed worker for the public core conformance runner."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys

from .conformance import (
    MAX_CONFORMANCE_REPORT_BYTES,
    validate_conformance_worker_config,
)
from .protocol import MAX_CONFIG_BYTES, read_frame
from .receipts import publish_json


async def _run(config):
    from evolution.tree import load_manifest_v2, run_conformance
    from evolution_lab.preparation.internal_manifest import load_internal_manifest

    path = Path(config["manifest_path"])
    # Owner supplies the same state root for artifacts and its content-addressed
    # bundles. Only a Bundle root's .lab entries are internal: campaign filenames
    # are user-controlled and remain under that Bundle's inputs/<alias>/ tree.
    bundle_root = path.parent.parent
    internal_entry = (
        bundle_root.parent == Path(config["allowed_artifact_root"]).parent / "bundles"
        and path.relative_to(bundle_root).as_posix() in {".lab/conformance.json", ".lab/probe-policy.json"}
    )
    manifest = (
        load_internal_manifest(path)
        if internal_entry
        else load_manifest_v2(path)
    )
    task = asyncio.create_task(
        run_conformance(manifest, workdir=Path(config["workdir"]))
    )
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, task.cancel)
    try:
        report = await task
    except asyncio.CancelledError:
        return 1
    finally:
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)
    publish_json(
        Path(config["report_path"]), report.to_payload(),
        max_bytes=MAX_CONFORMANCE_REPORT_BYTES,
    )
    return 0 if report.ok else 1


def main() -> None:
    chosen = 1
    config = None
    try:
        raw_fd = os.environ.pop("LAB_CONFORMANCE_CONFIG_FD")
        config_fd = int(raw_fd)
        os.set_inheritable(config_fd, False)
        try:
            config = validate_conformance_worker_config(
                read_frame(config_fd, max_bytes=MAX_CONFIG_BYTES)
            )
        finally:
            os.close(config_fd)
        chosen = asyncio.run(_run(config))
    except BaseException:
        chosen = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except BaseException:
            pass
    os._exit(chosen)


if __name__ == "__main__":
    main()
